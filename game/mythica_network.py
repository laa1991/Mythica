"""mythica_network.py — 桌面连接检测与 HTTP 通信

从 my_script.py 提取。
依赖: mythica_state, mythica_config, mythica_constants
"""

from mythica_state import state
from mythica_config import get_game_tuning, log_error
from mythica_constants import (_localhost_probe_host, _localhost_probe_port,
                               _localhost_probe_port_sandbox)
from b_path_runtime import RUNTIME_PATH_ENDPOINT as _RUNTIME_PATH_ENDPOINT

import http.client
import socket
import json
import time
import threading
from queue import Empty

# Wire the runtime path callback into mythica_config_paths so get_output_directory()
# can forward path snapshots to the desktop via HTTP.
import mythica_config_paths as _cpaths

__all__ = ["_refresh_desktop_ready_status", "send_action_result"]

# 网络通信参数
_SEND_COOLDOWN_SECONDS = 10.0       # 发送失败后冷却时间
_MAX_SEND_RETRIES = 2               # 最大重试次数（不含首次）
_ALIVE_CHECK_CACHE_TTL = 3          # _check_desktop_alive 结果缓存秒数
_RESTART_CHECK_CACHE_TTL = 10       # _check_desktop_restarted 结果缓存秒数

# 宣告文件检测参数
_DESKTOP_READY_CHECK_INTERVAL = 10   # 节流：每 10s 最多检查一次宣告文件
_DESKTOP_READY_STALE_SECONDS = 60   # 宣告文件超过 60s 未刷新 = 桌面离线

import os as _os


def _refresh_desktop_ready_status():
    """读宣告文件（桌面端/沙盘各 30s 刷新），更新连接状态。

    52173 → Desktop_Ready.signal（Mythica 桌面）
    52174 → Sandbox_Ready.signal（Mythica Sandbox），无宣告文件时回退 cooldown

    状态转换时自动清除对应端口的 cooldown，立即恢复数据发送。
    """
    now = time.time()
    if now - state.probe._last_desktop_ready_check < _DESKTOP_READY_CHECK_INTERVAL:
        return
    state.probe._last_desktop_ready_check = now

    try:
        from mythica_config_paths import get_output_directory
        output_dir = get_output_directory()
        if not output_dir:
            return

        # ── 52173: Desktop_Ready.signal ──
        _check_signal_for_port(output_dir, "Desktop_Ready.signal",
                               _localhost_probe_port, now)

        # ── 52174: Sandbox_Ready.signal（无宣告文件时回退 cooldown）──
        sandbox_signal = _os.path.join(output_dir, "Sandbox_Ready.signal")
        if _os.path.isfile(sandbox_signal):
            _check_signal_for_port(output_dir, "Sandbox_Ready.signal",
                                   _localhost_probe_port_sandbox, now)
        else:
            # 沙盘无独立宣告文件时用 cooldown 代理
            cd = state.probe.send_cooldown_until.get(_localhost_probe_port_sandbox, -1.0)
            if cd == 0.0:
                state.probe.desktop_ready_ports[_localhost_probe_port_sandbox] = True
            elif cd > now:
                state.probe.desktop_ready_ports[_localhost_probe_port_sandbox] = False

        # ── 全端口离线时长追踪（供长断连降频使用）──
        # 仅当所有 target_ports 都已检查过才判断——未检查的端口
        # .get() 返回 None，any([False, None]) = False 会误判全死
        checked = [state.probe.desktop_ready_ports[tp]
                   for tp in state.probe.target_ports
                   if tp in state.probe.desktop_ready_ports]
        if len(checked) == len(state.probe.target_ports):
            if any(checked):
                state.probe._all_ports_dead_since = 0.0
            elif state.probe._all_ports_dead_since == 0.0:
                state.probe._all_ports_dead_since = now
        else:
            state.probe._all_ports_dead_since = 0.0  # 还有端口未检查

    except Exception:
        pass  # 宣告检查失败不影响主流程


def _check_signal_for_port(output_dir, signal_filename, port, now):
    """读单个宣告文件，更新对应端口的状态。"""
    signal_path = _os.path.join(output_dir, signal_filename)
    connected = False
    new_session = ""

    if _os.path.isfile(signal_path):
        mtime = _os.path.getmtime(signal_path)
        age = now - mtime
        if age < _DESKTOP_READY_STALE_SECONDS:
            connected = True
            try:
                with open(signal_path, 'r', encoding='utf-8') as f:
                    raw = f.read()
                # 宣告文件为 JSON 格式（emit_signal_as_json），
                # 回退兼容旧格式纯时间戳字符串
                data = json.loads(raw) if raw.strip().startswith('{') else {}
                new_session = str(data.get("session", "") or "")
            except Exception:
                pass

    prev = state.probe.desktop_ready_ports.get(port, False)

    if connected and not prev:
        # 离线→在线：清 cooldown
        state.probe.send_cooldown_until[port] = 0.0
        if new_session:
            state.runtime.desktop_session_id = new_session
        log_error(
            "[desktop_ready] port {} RECOVERED (session={})".format(
                port, new_session[:8] if new_session else "?"),
            "network")
        # 桌面恢复 → 自动发送 monitor_control('start') 启动 FileMonitor
        if port == _localhost_probe_port:
            try:
                _send_monitor_control('start')
            except Exception:
                pass
    elif not connected and prev:
        log_error("[desktop_ready] port {} LOST".format(port), "network")

    state.probe.desktop_ready_ports[port] = connected


def _probe_sender_worker():
    """后台线程：阻塞等待队列，避免空转 CPU。cooldown 按端口独立——一个端口挂掉不拖慢其他端口。

    失败不重试——丢 item + 设 cooldown。重入队列会导致死端口 item 填满共享队列，
    进而活端口的 item 也无法入队（血案：2026-07-16 52173 离线→队列满→52174 也被堵死）。

    队列 item 合约：(endpoint: str, payload: dict|bytes, port: int)。
    payload 为 bytes 时直接发送（快照预序列化优化），dict 时 sender 负责 dumps。
    """
    while state.probe.sender_running:
        try:
            item = state.probe.send_queue.get(block=True, timeout=5)
        except Empty:
            continue
        try:
            endpoint, payload, port = item
        except Exception:
            continue
        # 按端口独立冷却——一个端口失败不影响其他端口。
        # 2026-07-21 修复：冷却期间直接丢弃 item 跳到下一个，不再 sleep 阻塞。
        # sleep 阻塞使死端口 item 卡住 worker 长达 10s，期间活端口的 item 堆积，
        # 共享队列满 → 活端口也被堵死（07-16 血案复发，每 6 分钟一次）。
        cooldown_until = state.probe.send_cooldown_until.get(port, 0.0)
        if cooldown_until > time.time():
            # 每冷却窗口只记一次"丢弃"日志（节流），其余静默跳过
            window_key = (port, int(cooldown_until))
            if window_key != state.probe._last_drop_window.get(port):
                state.probe._last_drop_window[port] = window_key
                log_error("[probe_sender] dropping items for port {} (in cooldown until {})".format(
                    port, time.strftime("%H:%M:%S", time.localtime(cooldown_until))),
                    "network")
            continue
        try:
            conn = http.client.HTTPConnection(_localhost_probe_host, port, timeout=3)
            try:
                # 2026-07-18 P2：调用方可传预序列化 bytes（快照双端口共用一次 dumps）
                raw = payload if type(payload) is bytes else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                conn.request("POST", endpoint, body=raw,
                             headers={"Content-Type": "application/json; charset=utf-8"})
                resp = conn.getresponse()
                resp.read()
                status = int(getattr(resp, "status", 0) or 0)
                if 200 <= status < 300:
                    state.probe.send_cooldown_until[port] = 0.0
                elif 400 <= status < 500:
                    # 4xx = 服务器活着但不认识该端点（如旧沙盘无 /action_result 路由）
                    # ——只记日志不设冷却，防版本偏斜拖累同端口的探针/快照通道
                    log_error(f"[probe_sender] HTTP {status} on {endpoint} (no cooldown)", "network")
                else:
                    log_error(f"[probe_sender] HTTP {status} on {endpoint}", "network")
                    state.probe.send_cooldown_until[port] = time.time() + _SEND_COOLDOWN_SECONDS
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as _exc:
            log_error(f"[probe_sender] connection failed on {endpoint}: {_exc}", "network")
            state.probe.send_cooldown_until[port] = time.time() + _SEND_COOLDOWN_SECONDS


def _ensure_probe_sender():
    """确保后台发送线程在运行。"""
    if state.probe.sender_thread is not None:
        if state.probe.sender_thread.is_alive():
            return
    state.probe.sender_running = True
    state.probe.sender_thread = threading.Thread(target=_probe_sender_worker, daemon=True)
    state.probe.sender_thread.start()

def _check_desktop_alive(port=None, timeout=1.5):
    """快速检查桌面端 HTTP 服务器是否可达且 AI 未暂停（结果缓存 3 秒防重复探针）。"""
    now = time.time()
    last_result = getattr(_check_desktop_alive, '_last_result', None)
    last_time = getattr(_check_desktop_alive, '_last_time', 0)
    if last_result is not None and now - last_time < _ALIVE_CHECK_CACHE_TTL:
        return last_result
    target_port = int(port or _localhost_probe_port)
    # 先用 TCP 快速探测端口
    try:
        sock = socket.create_connection((_localhost_probe_host, target_port), timeout=timeout)
        sock.close()
    except Exception:
        _check_desktop_alive._last_result = False
        _check_desktop_alive._last_time = now
        return False
    # 端口可达，发送实际 HTTP 探针检查 AI 暂停状态
    conn = http.client.HTTPConnection(_localhost_probe_host, target_port, timeout=min(timeout, 2.0))
    try:
        payload = json.dumps({'message': 'alive_check'}, ensure_ascii=False).encode('utf-8')
        conn.request('POST', '/probe', body=payload,
                     headers={'Content-Type': 'application/json; charset=utf-8'})
        resp = conn.getresponse()
        text = resp.read().decode('utf-8', errors='replace')
        try:
            data = json.loads(text)
        except Exception:
            data = {}
        result = not bool(data.get('ai_paused'))
        _check_desktop_alive._last_result = result
        _check_desktop_alive._last_time = now
        # 追踪 Mythica 的 session_id（变化 = 重启）
        _sid = str(data.get('session', '') or '')
        if _sid:
            state.runtime.desktop_session_id = _sid
        return result
    except Exception:
        # HTTP 探测失败，回退到 TCP 结果（端口已通）
        _check_desktop_alive._last_result = True
        _check_desktop_alive._last_time = now
        return True
    finally:
        try:
            conn.close()
        except Exception:
            pass




def _check_desktop_restarted(port=None, timeout=1.5):
    """检查 Mythica 是否重启过（session_id 变化）。

    比 _check_desktop_alive() 更精确——TCP 通 ≠ 没重启。
    session_id 不同 = Mythica 崩溃/手动重启过 = 旧请求已丢失 = 可以安全 reset。
    结果缓存 10 秒防重复。
    """
    now = time.time()
    last_check = getattr(_check_desktop_restarted, '_last_check', 0)
    last_result = getattr(_check_desktop_restarted, '_last_result', False)
    if now - last_check < _RESTART_CHECK_CACHE_TTL:
        return last_result
    _check_desktop_restarted._last_check = now
    target_port = int(port or _localhost_probe_port)
    try:
        conn = http.client.HTTPConnection(_localhost_probe_host, target_port, timeout=min(timeout, 2.0))
        conn.request('GET', '/health')
        resp = conn.getresponse()
        text = resp.read().decode('utf-8', errors='replace')
        conn.close()
        data = json.loads(text)
        new_sid = str(data.get('session', '') or '')
        if new_sid and state.runtime.desktop_session_id and new_sid != state.runtime.desktop_session_id:
            state.runtime.desktop_session_id = new_sid
            _check_desktop_restarted._last_result = True
            return True
        elif new_sid:
            state.runtime.desktop_session_id = new_sid  # 首次记录
        _check_desktop_restarted._last_result = False
        return False
    except Exception:
        _check_desktop_restarted._last_result = False
        return False






def _send_localhost_runtime_path_event(payload, port=None):
    _ensure_probe_sender()
    try:
        for tp in state.probe.target_ports:
            state.probe.send_queue.put_nowait((_RUNTIME_PATH_ENDPOINT, payload, tp))
    except Exception:
        return (False, "queue_full", {})
    return (True, 0, {"queued": True})

_cpaths._runtime_path_callback = _send_localhost_runtime_path_event



def _send_monitor_control(action):
    """通知桌面端启动/停止 FileMonitor（通过队列异步发送）。"""
    ports = state.probe.target_ports
    _ensure_probe_sender()
    log_error(f"[monitor_control] sending '{action}' to ports {ports}", "monitor")
    try:
        for tp in ports:
            state.probe.send_queue.put_nowait(("/monitor_control", {"action": action}, tp))
    except Exception:
        log_error(f"[monitor_control] queue full, failed to send '{action}'", "monitor")
        return (False, "queue_full", {})
    return (True, 0, {"queued": True})


def send_action_result(payload):
    """公开 API：动作执行结果 → 直连 HTTP 发到沙盘，失败时落文件兜底。

    2026-07-21 重构：旧的实现走共享探针发送队列——死端口（52173）的 item 阻塞
    worker → 队列积满 → 动作结果无法入队 → 沙盘永远收不到回传 → 动作面板
    "等待游戏回传"永久挂起。

    新方案：动作结果不走共享队列。直连 HTTP（2s 超时），失败落
    Action_Result_{id}.json 文件让沙盘轮询。此路径不依赖探针基础设施。
    """
    from mythica_records import get_action_result_json_path
    action_id = payload.get("action_id", "")
    # 优先：直连沙盘端口（动作结果是低流量关键反馈，值得独立连接）
    for port in [_localhost_probe_port_sandbox, _localhost_probe_port]:
        try:
            conn = http.client.HTTPConnection(_localhost_probe_host, port, timeout=2.0)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            conn.request("POST", "/action_result", body=body,
                         headers={"Content-Type": "application/json; charset=utf-8"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if 200 <= resp.status < 300:
                return (True, port, {"delivered": True})
        except Exception:
            continue  # 尝试下一个端口
    # 兜底：落文件供沙盘轮询（2026-07-21 新增——HTTP 双端口全不可达时最后的救命通道）
    try:
        fpath = get_action_result_json_path(action_id)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return (True, 0, {"file_fallback": True, "path": fpath})
    except Exception as e:
        log_error("[action_result] file fallback failed: {}".format(str(e)[:120]), "network")
        return (False, "all_ports_dead_and_file_failed", {})



