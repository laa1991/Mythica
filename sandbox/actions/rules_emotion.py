# rules_emotion.py — 心情/状态注入规则
"""心情 buff 注入——AI 想改变某角色的情绪状态时，翻译成 add_buff 动作。

action_type="add_buff" 是第 5 种执行原语（区别于 push/goto/stop/idle）：不推交互
affordance——DebugEmotionIntensity_* 是 cheat 会被游戏端过滤、BuffPicker_* 是 picker
（同食物选择器会卡暂停）——而是游戏端直调 sim_info.add_buff(buff_type, reason)。

mood 字段携带目标心情英文名（EA Mood_<Name> 去前缀），游戏端 `_resolve_mood_buff`
按 mood_type 匹配到对应心情 buff。全部 target_kind="self"：actor 调节自己的心情。
"""
from .rule_schema import CustomActionRule

# 核心心情集合（EA Mood 类型，叙事最常用）。verified=False 待实测——
# 测试页可逐条手动验证 buff 是否真推上（人物状态页 buff 列表可见）。
_EMOTIONS = (
    ("Happy", "😊 开心"),
    ("Sad", "😢 难过"),
    ("Angry", "😠 生气"),
    ("Tense", "😰 紧张"),
    ("Flirty", "😘 调情"),
    ("Playful", "😜 顽皮"),
    ("Confident", "😎 自信"),
    ("Focused", "🎯 专注"),
    ("Inspired", "💡 灵感迸发"),
    ("Energized", "⚡ 精力充沛"),
    ("Embarrassed", "😳 尴尬"),
    ("Uncomfortable", "😣 不适"),
    ("Bored", "😒 无聊"),
    ("Dazed", "😵 恍惚"),
    ("Scared", "😨 害怕"),
)


def _build_emotion_rules():
    rules = []
    for mood, label in _EMOTIONS:
        rules.append(CustomActionRule(
            rule_id="mood_{}".format(mood.lower()),
            label=label,
            action_type="add_buff",
            target_kind="self",
            hints=(),
            verb="{actor} 调整心情：" + label,
            mood=mood,
            verified=False,
            note="add_buff 原语直调 sim_info.add_buff；待实测确认 mood_type 匹配",
        ))
    return tuple(rules)


EMOTION_RULES: tuple = _build_emotion_rules()
