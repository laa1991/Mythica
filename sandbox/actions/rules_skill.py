# rules_skill.py — 技能门控类动作规则
"""需要特定技能等级才能执行的动作。min_skill 硬门控——不满足技能门槛的角色直接跳过。

注意：大量物品使用动作也涉及技能（弹钢琴、画画等），但它们不要求最低技能等级，
归入 rules_objects.py。此文件只收有 min_skill 硬门槛的规则。
"""
from .rule_schema import CustomActionRule

SKILL_RULES: tuple = (
    CustomActionRule(
        rule_id="tend_garden",
        label="🌿 照料花园",
        action_type="push",
        target_kind="object",
        hints=("Gardening_Tend_Start", "Gardening_Water_All_Area"),
        verb="{actor} 照料花园",
        target_match=("garden", "花园", "花盆", "planter", "garden_soil", "gardening"),
        verified=False,
        min_skill={"Gardening": 1},
        note="Gardening_Tend_Start(117875)+Gardening_Water_All_Area(183148) EA super=Y auto=Y user=Y；2026-07-21 收紧target_match移除plant/植物（误匹配装饰植物→not_found）；2026-07-26 +min_skill: 实测成人被拒因无技能",
    ),
    CustomActionRule(
        rule_id="use_microscope",
        label="🔬 用显微镜",
        action_type="push",
        target_kind="object",
        hints=("Microscope_Research", "MicroscopeSlide_CollectCrystal",
               "Microscope_AnalyzeCrystal", "Microscope_AnalyzeFossil", "Microscope_AnalyzePlant"),
        verb="{actor} 用显微镜观察",
        target_match=("microscope", "显微镜"),
        verified=True,
        min_skill={"Logic": 1},
        note="Microscope_Research(13592) 纯观察不需样品→CollectCrystal(13582)收集玻片→AnalyzeCrystal/Fossil/Plant需样品；EA super=Y auto=Y ADULT+；2026-07-22 加Research+Collect兜底",
    ),
    CustomActionRule(
        rule_id="rock_climb",
        label="🧗‍♂️ 攀岩",
        action_type="push",
        target_kind="object",
        hints=("treadmill_Rock_ClimbingWall_Climb", "climbingRoute_Novice_ClimbNormally"),
        verb="{actor} 攀岩",
        target_match=("climbingroute", "climbing wall", "climbingwall",
                      "攀岩墙", "攀岩"),
        verified=True,
        min_age="Teen",
        min_skill={"Rock Climbing": 1},
        note="climbingRoute_Novice_ClimbNormally(245296)→Practice(245373)；EA super=Y auto=Y user=Y TEEN+；登山包DLC内容",
    ),
    CustomActionRule(
        rule_id="play_console",
        label="🎮 玩主机游戏",
        action_type="push",
        target_kind="object",
        hints=("videoGameConsole_PlayGame_Party_Start", "videoGameConsole_PlayGame_RPG_Start",
               "videoGameConsole_PlayGame_Racing_Start", "videoGameConsole_PlayGame_Platformer_Start"),
        verb="{actor} 玩主机游戏",
        target_match=("videogameconsole", "video_game_console", "gameconsole", "游戏主机", "游戏机"),
        target_exclude=("tv_wall", "tv_flat", "电视", "television"),
        verified=False,
        min_skill={"Video Gaming": 1},
        note="仅保留基础PlayGame(145681-83,145673)——Livestream需SocialMedia职业(报no target)、GamedOut需buff whitelist；2026-07-24 柱间实测全拒(no target object)→可能场景无主机物品",
    ),
)
