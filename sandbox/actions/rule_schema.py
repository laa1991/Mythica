# rule_schema.py — CustomActionRule dataclass（Layer 0，零项目内依赖）
"""声明式动作规则的数据模型。

加一行规则 = 加一个动作，游戏端零改动。
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CustomActionRule:
    """自定义动作规则行（纯数据）。

    Attributes:
        rule_id: 唯一 id 片段（进 action_id：custom_{rule_id}_{actor}_{target}）
        label: 选择器 prompt / 测试页显示标签（带 emoji）
        action_type: 执行原语——"push"（affordance 推送）或 "goto"（地形移动）
        target_kind: 目标类别——"sim" / "object" / "self"
        hints: 精确交互类名（优先级序）——显式兜底链
        verb: 描述模板，含 {actor}/{target} 占位
        target_match: object 目标的 type/name 小写关键词
        target_exclude: 排除关键词
        verified: 实测通过 → 进 AI 目录；False 只在测试页显示
        note: 查证备注，不进 payload
        object_state_requires: 物品必须具备的状态关键词（如修="broken"）
        object_state_forbidden: 物品禁止有的状态关键词
        min_skill: 最低技能要求 {"Gardening": 2}
        min_age: 最低年龄（如 "Teen"）
        allow_in_use: 是否允许匹配占用中的物品
        clear_queue: 推之前清空 sim 的交互队列（站姿/手持占用导致的 SlotTest 失败用）
        auto_trigger: 程序自动触发（2026-07-27）——True=每轮条件满足直接发送，不走 AI 决策。
            适用场景：收垃圾、修电器、清洁脏物等"看到就做"的反应式动作。
            False=进动作目录由 AI 选择。
    """
    rule_id: str
    label: str
    action_type: str
    target_kind: str
    hints: tuple
    verb: str
    target_match: tuple = ()
    target_exclude: tuple = ()
    verified: bool = False
    tested_failed: bool = False     # 🆕 2026-08-01：已实测确认失败，根因无法修复
    condition_gated: bool = False   # 🆕 2026-08-01：需特定条件才能测（物种/物品/状态），不是规则本身问题
    note: str = ""
    object_state_requires: tuple = ()
    object_state_forbidden: tuple = ()
    min_skill: dict = field(default_factory=dict)
    min_age: str = ""
    allow_in_use: bool = False
    clear_queue: bool = False
    auto_trigger: bool = False
    group: str = ""                 # 🆕 2026-08-01：语义分组覆盖——social/romance/use_object/need/walk/stop/inventory/self
                                    #     空=""则按 target_kind 自动推断（object→use_object, self→self, sim→"goto_sim"旧行为）
                                    #     WW sim-target 动作应显式设为 "romance" 避免掉入【其他】段落
    mood_requires: tuple = ()       # 🆕 心情门控：仅在指定心情时推（如 ("Inspired",)），空=不限
    location_prefer: tuple = ()     # 🆕 位置偏好：优先在指定房间推（如 ("书房",)），空=不限
    needs_goto: bool = False        # 🆕 此动作通常需要前置 gohere（observer 数据驱动）
    preceding_actions: tuple = ()   # 🆕 常见的前置动作（如 ("terrain-gohere",)），空=无
    estimated_duration_s: float = 0.0  # 🆕 闭环 #6：预计执行时长（秒），observer 从 durations 统计计算，0=未知
