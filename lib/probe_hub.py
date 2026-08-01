# probe_hub.py — 探针数据中枢
"""轻量级发布/订阅数据中介——所有 Mythica 桌面端子程序的探针数据入口。

游戏端只向一个地址发送数据，ProbeHub 负责扇出到所有注册的订阅者。
每个订阅者独立隔离——一个崩溃不影响其他订阅者或中枢本身。

设计原则：
- 极简第一——不做线程池、优先级队列、消息过滤，直到有第二个用例
- mythica_lib 层——零依赖 mythica_app 或 mythica_sandbox
- 订阅者接口通过 Protocol 定义——IDE 可检查方法拼写，重构时自动追踪
- 所有错误落盘——中枢和订阅者的异常均通过 error_log 回调记录

用法:
    from mythica_lib.probe_hub import ProbeHub, ProbeHubSubscriber

    class MyConsumer:
        def on_queue_probe(self, payload): ...
        def on_scene_snapshot(self, payload): ...

    hub = ProbeHub(port=52173)
    hub.set_error_log(my_log_fn)
    hub.subscribe(MyConsumer())
    hub.start()
"""
import json
import time
import threading
import uuid
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Protocol, Callable, Optional

__all__ = [
    "ProbeHubSubscriber",
    "SubscriberCallback",
    "ProbeHub",
    "get_hub",
    "start_hub",
    "stop_hub",
]

# ── 路由 → 订阅者方法名映射 ──
_ROUTE_METHOD_MAP = {
    "/queue_probe": "on_queue_probe",
    "/probe": "on_probe",
    "/scene_snapshot": "on_scene_snapshot",
    "/runtime_path": "on_runtime_path",
    "/inner_voice": "on_inner_voice",
    "/dialogue_ticket": "on_dialogue_ticket",
    "/overlay": "on_overlay",
    "/monitor_control": "on_monitor_control",
    "/action_result": "on_action_result",
}


# ═══════════════════════════════════════════════════════════════
# 订阅者接口
# ═══════════════════════════════════════════════════════════════

class ProbeHubSubscriber(Protocol):
    """订阅者接口契约——实现你关心的路由方法即可，其他路由中枢不会调用。

    每个方法接收游戏端 POST 过来的完整 payload dict。
    方法在 HTTP handler 线程中调用——订阅者负责自己的线程安全。
    """

    def on_queue_probe(self, payload: dict) -> None: ...
    def on_probe(self, payload: dict) -> None: ...
    def on_scene_snapshot(self, payload: dict) -> None: ...
    def on_runtime_path(self, payload: dict) -> None: ...
    def on_inner_voice(self, payload: dict) -> None: ...
    def on_dialogue_ticket(self, payload: dict) -> None: ...
    def on_overlay(self, payload: dict) -> None: ...
    def on_monitor_control(self, payload: dict) -> None: ...
    def on_action_result(self, payload: dict) -> None: ...


# 简单回调类型：f(route: str, payload: dict) -> None
SubscriberCallback = Callable[[str, dict], None]


# ═══════════════════════════════════════════════════════════════
# HTTP Handler
# ═══════════════════════════════════════════════════════════════

class _HubHandler(BaseHTTPRequestHandler):
    """ProbeHub 内部 HTTP 请求处理器。

    通过类属性 `_hub` 引用所属 ProbeHub 实例——在 ProbeHub.start() 时注入。
    """

    _hub: Optional["ProbeHub"] = None

    def log_message(self, format_, *args):
        pass  # 抑制 stdlib HTTP 日志

    def _respond(self, status: int, extra: dict = None):
        """发送 JSON 响应。所有响应都带 session 字段供游戏端重连检测。"""
        hub = self._hub
        payload = {
            "ok": True,
            "app": "ProbeHub",
            "session": hub.session_id if hub else "",
        }
        if extra:
            payload.update(extra)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._respond(200)
        elif self.path == "/settings":
            hub = self._hub
            if hub and hub._settings_provider:
                try:
                    settings = hub._settings_provider()
                    self._respond(200, settings)
                except Exception:
                    self._respond(200, {})  # 降级返回空
            else:
                self._respond(200, {})
        else:
            self._respond(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        # 读取 body
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            payload = {"_raw": raw.decode("utf-8", errors="replace")}

        # 检查是否有直接处理器（同步请求-响应模式）
        hub = self._hub
        if hub:
            direct = hub._direct_handlers.get(self.path)
            if direct is not None:
                try:
                    resp = direct(payload)
                    if isinstance(resp, dict):
                        self._respond(200, resp)
                    else:
                        self._respond(200)
                except Exception:
                    self._respond(500, {"ok": False, "error": "direct_handler_error"})
                return

        self._respond(200)

        # 扇出到所有订阅者
        if hub:
            hub._fan_out(self.path, payload)


# ═══════════════════════════════════════════════════════════════
# Threaded HTTP Server
# ═══════════════════════════════════════════════════════════════

class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器——每个请求独立线程，不阻塞其他请求。"""
    daemon_threads = True
    allow_reuse_address = True


# ═══════════════════════════════════════════════════════════════
# ProbeHub
# ═══════════════════════════════════════════════════════════════

class ProbeHub:
    """轻量级发布/订阅探针数据中介。

    接收游戏端的 HTTP POST 数据，扇出到所有注册订阅者。

    Parameters:
        host: 监听地址，默认 127.0.0.1
        port: 监听端口，默认 52173
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 52173):
        self.host = host
        self.port = int(port)
        self.session_id: str = uuid.uuid4().hex[:12]
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._subscribers: list[ProbeHubSubscriber] = []
        self._callbacks: list[SubscriberCallback] = []
        self._direct_handlers: dict[str, Callable] = {}  # route → handler(payload)->dict
        self._error_log: Optional[Callable[[str, str], None]] = None
        self._settings_provider: Optional[Callable[[], dict]] = None
        # HTTP Forward 订阅者——跨进程订阅。key = forward_url, value = {registered_at, ...}
        self._forward_subscribers: dict[str, dict] = {}
        self._setup_builtin_handlers()

    # ── 配置 ──

    def set_error_log(self, log_fn: Callable[[str, str], None]):
        """注入错误日志回调。log_fn(tag: str, message: str)。"""
        self._error_log = log_fn

    def set_settings_provider(self, provider: Callable[[], dict]):
        """注入 /settings GET 端点的数据提供者。provider() -> dict。"""
        self._settings_provider = provider

    # ── 内置 direct handlers ──

    def _setup_builtin_handlers(self):
        """注册中枢内置的 direct handler（订阅/取消订阅等管理端点）。"""
        self._direct_handlers["/_subscribe_forward"] = self._handle_subscribe_forward
        self._direct_handlers["/_unsubscribe_forward"] = self._handle_unsubscribe_forward

    def _handle_subscribe_forward(self, payload: dict) -> dict:
        """注册一个 HTTP Forward 订阅者。payload: {forward_url: str}。
        订阅后，中枢每次扇出时会 POST 数据到该 URL。
        用于跨进程订阅——沙盘等子程序通过此机制接收数据。
        """
        url = (payload.get("forward_url") or "").strip()
        if not url:
            return {"ok": False, "error": "missing forward_url"}
        with self._lock:
            self._forward_subscribers[url] = {
                "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_success": "",
                "errors": 0,
            }
        self._log("probe_hub", f"Forward subscriber registered: {url}")
        return {"ok": True, "subscriber_count": len(self._forward_subscribers)}

    def _handle_unsubscribe_forward(self, payload: dict) -> dict:
        """移除一个 HTTP Forward 订阅者。payload: {forward_url: str}。"""
        url = (payload.get("forward_url") or "").strip()
        with self._lock:
            if url in self._forward_subscribers:
                del self._forward_subscribers[url]
                self._log("probe_hub", f"Forward subscriber removed: {url}")
                return {"ok": True, "subscriber_count": len(self._forward_subscribers)}
        return {"ok": False, "error": "not_found"}

    # ── 订阅管理 ──

    def subscribe(self, subscriber):
        """注册一个订阅者。

        subscriber 可以是：
        - 实现了 ProbeHubSubscriber Protocol 的对象（按路由分发方法）
        - 一个 callable f(route: str, payload: dict) -> None（简单单回调）
        """
        with self._lock:
            if callable(subscriber) and not hasattr(subscriber, "on_queue_probe"):
                self._callbacks.append(subscriber)
            else:
                self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber):
        """移除一个已注册的订阅者。"""
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)
            if subscriber in self._callbacks:
                self._callbacks.remove(subscriber)

    def set_direct_handler(self, route: str, handler):
        """注册一个路由的直接处理器——替代该路由的默认扇出行为。

        handler(payload: dict) -> dict | None
          返回的 dict 合并到 HTTP 响应中发回游戏端（用于同步 accept/reject）。
          返回 None 表示使用默认响应。

        直接处理器在主线程 HTTP handler 中同步调用——需快速返回。
        典型用例：/inner_voice、/dialogue_ticket 需要向游戏端返回 accept/reject。
        """
        self._direct_handlers[route] = handler

    @property
    def subscriber_count(self) -> int:
        """当前订阅者总数。"""
        with self._lock:
            return len(self._subscribers) + len(self._callbacks)

    # ── 生命周期 ──

    def start(self) -> tuple:
        """启动 HTTP 服务器。

        Returns:
            (True, None) 成功
            (False, error_message) 失败
        """
        if self._server is not None:
            return (True, None)

        _HubHandler._hub = self
        try:
            self._server = _ThreadedHTTPServer((self.host, self.port), _HubHandler)
        except OSError as e:
            self._server = None
            return (False, str(e))

        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        self._log("probe_hub", f"ProbeHub started on {self.host}:{self.port}")
        return (True, None)

    def stop(self):
        """停止 HTTP 服务器并清空所有订阅者。"""
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
            self._thread = None
        with self._lock:
            self._subscribers.clear()
            self._callbacks.clear()
            self._forward_subscribers.clear()

    # ── 内部 ──

    def _fan_out(self, route: str, payload: dict):
        """将数据扇出到所有订阅者。订阅者崩溃不影响其他订阅者。

        在 HTTP handler 线程中同步调用——当前所有 in-process 订阅者都极快
        （存内存 + GUI 回调）。HTTP Forward 订阅者在后台线程中异步发送，
        不阻塞游戏端响应。

        如果未来某个 in-process 订阅者变慢，改这里为 threading.Thread 即可。
        """
        method_name = _ROUTE_METHOD_MAP.get(route)

        with self._lock:
            subs_snapshot = list(self._subscribers)
            cbs_snapshot = list(self._callbacks)
            fwd_snapshot = list(self._forward_subscribers.keys())

        # Protocol 订阅者——按路由分发方法
        if method_name:
            for sub in subs_snapshot:
                try:
                    method = getattr(sub, method_name, None)
                    if method is not None:
                        method(payload)
                except Exception:
                    self._log(
                        "probe_hub._fan_out",
                        f"Subscriber {type(sub).__name__} crashed on {route}: "
                        f"{traceback.format_exc()[-300:]}",
                    )

        # 简单回调订阅者
        for cb in cbs_snapshot:
            try:
                cb(route, payload)
            except Exception:
                self._log(
                    "probe_hub._fan_out",
                    f"Callback crashed on {route}: "
                    f"{traceback.format_exc()[-300:]}",
                )

        # HTTP Forward 订阅者——后台线程异步发送，不阻塞中枢响应
        if fwd_snapshot:
            for url in fwd_snapshot:
                t = threading.Thread(
                    target=self._forward_to_url,
                    args=(url, route, payload),
                    daemon=True,
                )
                t.start()

    def _forward_to_url(self, url: str, route: str, payload: dict):
        """向一个 HTTP Forward 订阅者发送数据（在独立线程中运行）。

        失败时更新错误计数，不影响其他订阅者。

        注意：当前每个消息创建一个新线程。对于 1-2 个 Forward 订阅者
        （游戏端 1s/3s 间隔），开销可接受。如果未来订阅者增多或频率提高，
        改为单线程 + queue 模型。
        """
        import http.client
        from urllib.parse import urlparse

        try:
            parsed = urlparse(url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 52174
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            conn = http.client.HTTPConnection(host, port, timeout=3)
            try:
                conn.request(
                    "POST", parsed.path or "/",
                    body=body,
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        "X-ProbeHub-Route": route,
                    },
                )
                resp = conn.getresponse()
                resp.read()
                if 200 <= resp.status < 300:
                    with self._lock:
                        if url in self._forward_subscribers:
                            self._forward_subscribers[url]["last_success"] = (
                                time.strftime("%H:%M:%S"))
                else:
                    with self._lock:
                        if url in self._forward_subscribers:
                            self._forward_subscribers[url]["errors"] += 1
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            with self._lock:
                if url in self._forward_subscribers:
                    self._forward_subscribers[url]["errors"] += 1
            # 不记日志——转发失败是预期内的（订阅者可能未启动）

    def _log(self, tag: str, message: str):
        """通过注入的 error_log 回调写日志。回调自身异常不影响中枢。"""
        log_fn = self._error_log
        if log_fn:
            try:
                log_fn(tag, message)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
# 模块级单例（可选便利入口）
# ═══════════════════════════════════════════════════════════════

_hub: Optional[ProbeHub] = None
_hub_lock = threading.Lock()


def get_hub(port: int = 52173) -> ProbeHub:
    """获取或创建模块级 ProbeHub 单例。"""
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = ProbeHub(port=port)
        return _hub


def start_hub(port: int = 52173) -> tuple:
    """启动模块级单例中枢。"""
    return get_hub(port).start()


def stop_hub():
    """停止模块级单例中枢。"""
    global _hub
    with _hub_lock:
        if _hub is not None:
            _hub.stop()
            _hub = None
