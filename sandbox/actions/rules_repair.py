# rules_repair.py — 修理类动作规则
"""修电器/马桶/淋浴/水槽——需要物品处于损坏/故障状态才能触发。

全部 auto_trigger=True（P3 优先级）：程序自动扫描损坏物品并分配角色修理，不走 AI 决策。
"""
from .rule_schema import CustomActionRule

REPAIR_RULES: tuple = (
    CustomActionRule(
        rule_id="repair_electrical",
        label="🔧 修理电器",
        action_type="push",
        target_kind="object",
        hints=("object_RepairElectrical", "object_Repair_Computer", "object_Repair_StereoTV",
               "object_Repair_Fridge", "object_Repair_Stove"),
        verb="修理电器",
        target_match=("tv", "computer", "stereo", "fridge", "stove", "dishwasher",
                      "电视", "电脑", "音响", "冰箱", "炉灶", "洗碗机"),
        verified=True,
        auto_trigger=True,
        object_state_requires=("broken", "spark", "malfunction"),
        allow_in_use=True,
        note="object_RepairElectrical(13752)→Computer→StereoTV→Fridge→Stove；EA super=Y TEEN+；2026-07-22 +spark/malfunction——音响噼啪响不是broken而是sparking状态",
    ),
    CustomActionRule(
        rule_id="repair_toilet",
        label="🔧 修马桶",
        action_type="push",
        target_kind="object",
        hints=("object_Repair_Toilet",),
        verb="修理马桶",
        target_match=("toilet", "马桶", "馬桶"),
        verified=True,
        auto_trigger=True,
        object_state_requires=("broken", "clog", "spark", "malfunction"),
        allow_in_use=True,
        note="object_Repair_Toilet(34928) EA super=Y TEEN+；2026-07-22 +clog/spark/malfunction覆盖堵塞+故障态",
    ),
    CustomActionRule(
        rule_id="repair_shower",
        label="🔧 修淋浴",
        action_type="push",
        target_kind="object",
        hints=("object_Repair_Shower",),
        verb="修理淋浴间",
        target_match=("shower", "淋浴", "淋浴间"),
        verified=True,
        auto_trigger=True,
        object_state_requires=("broken", "spark", "malfunction"),
        allow_in_use=True,
        note="object_Repair_Shower(34925) EA super=Y TEEN+；2026-07-22 +spark/malfunction覆盖故障态",
    ),
    CustomActionRule(
        rule_id="repair_sink",
        label="🔧 修水槽",
        action_type="push",
        target_kind="object",
        hints=("object_Repair_Sink",),
        verb="修理水槽",
        target_match=("sink", "水槽", "洗手台", "洗手盆"),
        verified=True,
        auto_trigger=True,
        object_state_requires=("broken", "spark", "malfunction"),
        allow_in_use=True,
        note="object_Repair_Sink(34926) EA super=Y TEEN+；2026-07-22 +spark/malfunction覆盖故障态",
    ),
)
