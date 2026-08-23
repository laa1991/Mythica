# rules_observed.py — 观察器 自动发现的候选规则
"""从游戏观察中自动生成的候选规则。

规则来源：
  - 动作测试页手动"存为规则"
  - 观察器 自动发现（观察器生成管道）

所有规则 verified=False，需人工实测后升 verified。

持久化：observed_rules.json（<你的数据目录>/规则日志/）— 重启不丢。
"""
import json
import os

from .rule_schema import CustomActionRule

# ── JSON 持久化路径 ──
def _rules_json_path() -> str | None:
    try:
        from mythica_lib.config_paths import get_default_output_dir
        output_dir = get_default_output_dir()
        if not output_dir:
            return None
        log_dir = os.path.join(output_dir, "规则日志")
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, "observed_rules.json")
    except Exception:
        return None


def _load_rules_from_json() -> list[CustomActionRule]:
    """从 observed_rules.json 加载已存的规则。"""
    filepath = _rules_json_path()
    if not filepath or not os.path.isfile(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        rules = []
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                try:
                    # 兼容：hints/target_match 存为 list，CustomActionRule 需要 tuple
                    entry_copy = dict(entry)
                    for tuple_field in ("hints", "target_match", "target_exclude",
                                        "object_state_requires", "object_state_forbidden",
                                        "mood_requires", "location_prefer", "preceding_actions"):
                        if tuple_field in entry_copy and isinstance(entry_copy[tuple_field], list):
                            entry_copy[tuple_field] = tuple(entry_copy[tuple_field])
                    # min_skill 和 note 特殊处理
                    if "min_skill" in entry_copy and isinstance(entry_copy["min_skill"], list):
                        entry_copy["min_skill"] = {}
                    rules.append(CustomActionRule(**entry_copy))
                except Exception:
                    continue
        return rules
    except Exception:
        return []


def _save_rules_to_json(rules: list[CustomActionRule]) -> bool:
    """写入 observed_rules.json。"""
    filepath = _rules_json_path()
    if not filepath:
        return False
    try:
        data = []
        for r in rules:
            d = {
                "rule_id": r.rule_id,
                "label": r.label,
                "action_type": r.action_type,
                "target_kind": r.target_kind,
                "hints": list(r.hints),
                "verb": r.verb,
                "target_match": list(r.target_match),
                "target_exclude": list(r.target_exclude),
                "verified": r.verified,
                "tested_failed": r.tested_failed,
                "condition_gated": r.condition_gated,
                "note": r.note,
                "object_state_requires": list(r.object_state_requires),
                "object_state_forbidden": list(r.object_state_forbidden),
                "min_skill": r.min_skill,
                "min_age": r.min_age,
                "allow_in_use": r.allow_in_use,
                "clear_queue": r.clear_queue,
                "auto_trigger": r.auto_trigger,
                "group": r.group,
                "mood_requires": list(r.mood_requires),
                "location_prefer": list(r.location_prefer),
                "needs_goto": r.needs_goto,
                "preceding_actions": list(r.preceding_actions),
                "estimated_duration_s": r.estimated_duration_s,
            }
            data.append(d)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ── 模块级：启动时从 JSON 加载 ──
_OBSERVED_RULES: list[CustomActionRule] = _load_rules_from_json()

# 导出为 tuple（与其他规则文件格式一致）
OBSERVED_RULES: tuple = ()


def _rebuild_tuple():
    """重建导出元组（每次修改后调用）。"""
    global OBSERVED_RULES
    OBSERVED_RULES = tuple(_OBSERVED_RULES)


_rebuild_tuple()


# ── 公开 API ──

def append_observed_rule(rule: CustomActionRule) -> bool:
    """追加一条规则到内存 + 持久化到 JSON。

    Returns:
        True 如果成功写入 JSON。
    """
    # 去重：同 rule_id 覆盖
    for i, existing in enumerate(_OBSERVED_RULES):
        if existing.rule_id == rule.rule_id:
            _OBSERVED_RULES[i] = rule
            _rebuild_tuple()
            return _save_rules_to_json(_OBSERVED_RULES)

    _OBSERVED_RULES.append(rule)
    _rebuild_tuple()
    return _save_rules_to_json(_OBSERVED_RULES)


def get_observed_rules() -> list[CustomActionRule]:
    """返回当前所有观察规则（可修改列表）。"""
    return list(_OBSERVED_RULES)


def set_verified(rule_id: str, verified: bool) -> bool:
    """设置某条观察规则的 verified 字段并持久化。

    Args:
        rule_id: 规则 ID。
        verified: True 为已验证，False 为未验证。

    Returns:
        True 如果找到并更新了规则，False 如果未找到匹配的 rule_id。
    """
    for i, rule in enumerate(_OBSERVED_RULES):
        if rule.rule_id == rule_id:
            if rule.verified == verified:
                return True  # 已是目标值，无需写盘
            _OBSERVED_RULES[i] = CustomActionRule(
                rule_id=rule.rule_id,
                label=rule.label,
                action_type=rule.action_type,
                target_kind=rule.target_kind,
                hints=rule.hints,
                verb=rule.verb,
                target_match=rule.target_match,
                target_exclude=rule.target_exclude,
                verified=verified,
                tested_failed=rule.tested_failed,
                condition_gated=rule.condition_gated,
                note=rule.note,
                object_state_requires=rule.object_state_requires,
                object_state_forbidden=rule.object_state_forbidden,
                min_skill=rule.min_skill,
                min_age=rule.min_age,
                allow_in_use=rule.allow_in_use,
                clear_queue=rule.clear_queue,
                auto_trigger=rule.auto_trigger,
                group=rule.group,
                mood_requires=rule.mood_requires,
                location_prefer=rule.location_prefer,
                needs_goto=rule.needs_goto,
                preceding_actions=rule.preceding_actions,
                estimated_duration_s=rule.estimated_duration_s,
            )
            _rebuild_tuple()
            return _save_rules_to_json(_OBSERVED_RULES)
    return False


def set_tested_failed(rule_id: str, failed: bool = True) -> bool:
    """设置某条观察规则的 tested_failed 字段并持久化。

    Args:
        rule_id: 规则 ID。
        failed: True 标记为已测失败，False 清除标记。

    Returns:
        True 如果找到并更新了规则。
    """
    for i, rule in enumerate(_OBSERVED_RULES):
        if rule.rule_id == rule_id:
            if getattr(rule, 'tested_failed', False) == failed:
                return True
            _OBSERVED_RULES[i] = CustomActionRule(
                rule_id=rule.rule_id,
                label=rule.label,
                action_type=rule.action_type,
                target_kind=rule.target_kind,
                hints=rule.hints,
                verb=rule.verb,
                target_match=rule.target_match,
                target_exclude=rule.target_exclude,
                verified=rule.verified,
                tested_failed=failed,
                condition_gated=rule.condition_gated,
                note=rule.note,
                object_state_requires=rule.object_state_requires,
                object_state_forbidden=rule.object_state_forbidden,
                min_skill=rule.min_skill,
                min_age=rule.min_age,
                allow_in_use=rule.allow_in_use,
                clear_queue=rule.clear_queue,
                auto_trigger=rule.auto_trigger,
                group=rule.group,
                mood_requires=rule.mood_requires,
                location_prefer=rule.location_prefer,
                needs_goto=rule.needs_goto,
                preceding_actions=rule.preceding_actions,
                estimated_duration_s=rule.estimated_duration_s,
            )
            _rebuild_tuple()
            return _save_rules_to_json(_OBSERVED_RULES)
    return False


def rule_count() -> int:
    """返回当前观察规则数量。"""
    return len(_OBSERVED_RULES)
