# signal_protocol.py
"""桌面→游戏 信号文件协议。
所有 *.signal + *.json 文件写入的统一入口——文件名变更只需改此一处。"""
import json
import os
import time


# ── 信号名常量（不含 .signal/.json 后缀）──
INNER_VOICE_READY = 'InnerVoice_Ready'
STORY_READY = 'Story_Ready'
STORY_FAILED = 'Story_Failed'
DIALOGUE_READY = 'ManualDialogue_Ready'
RECAP_OVERRIDE = 'Manual_Recap_Override'
GAME_SETTINGS = 'Game_Settings'
RETRY_REQUEST = 'Retry_Request'
DESKTOP_READY = 'Desktop_Ready'
DESKTOP_GONE = 'Desktop_Gone'
SANDBOX_READY = 'Sandbox_Ready'
SANDBOX_GONE = 'Sandbox_Gone'
MAINTENANCE_PROBE = 'Maintenance_Probe'
MAINTENANCE_RESULT = 'Maintenance_Result'
MAINTENANCE_COMMAND = 'Maintenance_Command'
MAINTENANCE_COMMAND_RESULT = 'Maintenance_Command_Result'

# ── 文件扩展名常量 ──
SIGNAL_EXT = '.signal'
JSON_EXT = '.json'

# 启动清理列表——游戏端未消费的过期信号
STALE_SIGNALS = [
    INNER_VOICE_READY, STORY_READY, STORY_FAILED,
    RETRY_REQUEST, DIALOGUE_READY, RECAP_OVERRIDE, GAME_SETTINGS,
]


def signal_path(output_dir, signal_name):
    """返回 .signal 文件的完整路径。"""
    return os.path.join(output_dir, f'{signal_name}{SIGNAL_EXT}')


def json_path(output_dir, signal_name):
    """返回 .json 文件的完整路径。"""
    return os.path.join(output_dir, f'{signal_name}{JSON_EXT}')


def _atomic_write(path, content, mode='text'):
    """原子写入：先写 .tmp 再 os.replace。mode='text' 写字符串，'json' 写 json.dump。"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        if mode == 'json':
            json.dump(content, f, ensure_ascii=False, indent=2)
        else:
            f.write(content)
    os.replace(tmp, path)


def emit(output_dir, signal_name, data, timestamp=None):
    """写入 JSON + .signal 文件对（原子写入：先 .tmp 后 os.replace）。
    这是桌面→游戏结果推送的标准通道。
    signal 文件的内容与 data['created_at'] 保持一致（若未提供 timestamp）。"""
    ts = timestamp or data.get('created_at') or time.strftime('%Y-%m-%d %H:%M:%S')
    _atomic_write(json_path(output_dir, signal_name), data, 'json')
    _atomic_write(signal_path(output_dir, signal_name), ts, 'text')


def emit_signal_only(output_dir, signal_name, content=None):
    """仅写入 .signal 文件（无 JSON 数据），如 Game_Settings / Retry_Request。"""
    ts = content or time.strftime('%Y-%m-%d %H:%M:%S')
    _atomic_write(signal_path(output_dir, signal_name), ts, 'text')


def emit_json_only(output_dir, signal_name, data):
    """仅写入 .json 文件（无 signal），如 Story_Failed.json。"""
    _atomic_write(json_path(output_dir, signal_name), data, 'json')


def emit_signal_as_json(output_dir, signal_name, data):
    """写入 JSON 内容到 .signal 文件（原子写入）。
    少数信号（如 Manual_Recap_Override）的 .signal 本身就是 JSON 载体，没有配对 .json。"""
    _atomic_write(signal_path(output_dir, signal_name), data, 'json')


def cleanup_stale(output_dir):
    """启动时清理所有过期信号文件（含 .json / .signal / .tmp）。"""
    for name in STALE_SIGNALS:
        for ext in (SIGNAL_EXT, JSON_EXT, f'{JSON_EXT}.tmp', f'{SIGNAL_EXT}.tmp'):
            p = os.path.join(output_dir, f'{name}{ext}')
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
