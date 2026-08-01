# rules_social.py — 社交类动作规则
"""友好/刻薄社交互动（sim→sim），走 sim_Chat 父容器 + mixer 双推。

hints 链末尾放 sim_Chat 兜底——mixer 被拒时至少能聊天。
"""
from .rule_schema import CustomActionRule

SOCIAL_RULES: tuple = (
    CustomActionRule(
        rule_id="chat_friendly",
        label="💬 聊天",
        action_type="push",
        target_kind="sim",
        hints=("sim_Chat",),
        verb="{actor} 和 {target} 聊天",
        verified=True,
        note="sim_Chat(13998) EA super=Y test=仅IsNotInSexTest——最简社交；2026-07-25 实测通过（多人多对）",
    ),
    CustomActionRule(
        rule_id="hug_friendly",
        label="🤗 拥抱",
        action_type="push",
        target_kind="sim",
        hints=("sim_BroHug_QuickSocial", "sim_Chat"),
        verb="{actor} 拥抱 {target}",
        verified=True,
        min_age="Teen",
        note="sim_BroHug_QuickSocial(228603) EA super=Y auto=Y user=N 🥈；2026-07-25 实测通过（真鳕→斑）",
    ),
    CustomActionRule(
        rule_id="high_five",
        label="🖐 击掌",
        action_type="push",
        target_kind="sim",
        hints=("sim_HighFive_QuickSocial", "sim_Chat"),
        verb="{actor} 和 {target} 击掌",
        verified=True,
        min_age="Teen",
        note="sim_HighFive_QuickSocial(228605) EA super=Y auto=Y user=N 🥈；2026-07-25 实测通过（六道柱间→斑）",
    ),
    CustomActionRule(
        rule_id="argue_mean",
        label="😠 争吵",
        action_type="push",
        target_kind="sim",
        hints=("mixer_social_BrushOff_targeted_mean_emotionSpecific",
               "mixer_social_ChewOut_targeted_mean_emotionSpecific",
               "sim_Chat"),
        verb="{actor} 和 {target} 争吵",
        verified=True,
        min_age="Teen",
        note="BrushOff(26150)+ChewOut(25885) EA auto=Y user=Y test=age+emotion（无TraitTest！）。需sim在Angry/Tense情绪——若情绪不对则落sim_Chat",
    ),
    CustomActionRule(
        rule_id="read_to_toddler",
        label="📖 给幼儿读书",
        action_type="push",
        target_kind="sim",
        hints=("book_social_read_to_toddlerOnly",),
        verb="{actor} 给 {target} 读书",
        target_match=("toddler", "幼儿"),
        verified=True,
        note="observer 实证：book_social_read_to_toddlerOnly 游戏自主体使用 5 次。target_kind=sim 而非 object——读的是人不是书",
    ),
)
