# engine.py — 沙盘核心引擎
"""两段式 AI 决策核心循环。

Usage::

    engine = SandboxEngine()
    ws = game_bridge.parse_scene_snapshot_to_world_state(snapshot)

    result = engine.run_cycle(ws)
    if result.success:
        game_bridge.emit_action_commands(result.all_actions, ws)
        print(f"[{result.pov_name}] {result.inner_voice}")

Every cycle: collect world state → decide what to do → emit commands → loop.
No presets, no scripts — each round is a real-time AI decision.

三层决策架构（2026-07-28）：
  1. P4 生存层 — _execute_motive_emergency：膀胱 < -80 → 自动去厕所（纯生理，零叙事空间）
  2. Tier 1 AI — _call_inner_voice + _call_action_selector：大模型做所有有叙事选择空间的决策
  3. P3-P1 自动 — _execute_auto_triggers：修理 > 清洁 > 收集（场景状态驱动）
  汇合点 — _allocate_actions(motive, ai, auto)：生存 > AI > 自动，同 sim/object 只分配一次
  发射 — 合并后走同一套门控+信号+生命周期管线
"""
import dataclasses
import json
import random
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from mythica_lib.ai import call_ai, AICallParams

from .world_state import WorldState, _is_idle_action, _object_tags
from .error_log import log_exception, log_message
from .action_catalog import (
    ActionOption, generate_action_catalog, format_catalog_for_prompt,
    build_intent_lookup, MAX_OBJECT_ACTIONS_PER_CHAR,
)
from .prompts_sandbox import (
    INNER_VOICE_SYSTEM, INNER_VOICE_USER,
    ACTION_SELECTOR_SYSTEM, ACTION_SELECTOR_USER,
)
from .settings import get_sandbox_connection_settings


# 跨轮上下文保留的最近轮数（v2 导演层第一步，2026-07-17）
CYCLE_HISTORY_MAX = 8
# 动作去重窗口：同一 action_id 在最近 N 轮内已选过 → 硬过滤（2026-07-18）
RECENT_DEDUP_CYCLES = 3

# 自动触发规则优先级权重（2026-07-28）——多规则同时命中时按优先级分配
# 修理（崩了不能用）> 清洁（拖水坑/擦台面）> 收集（收垃圾/脏碗/脏衣）
_AUTO_RULE_PRIORITY: dict[str, int] = {
    "repair": 3,
    "clean": 2, "mop": 2,
    "collect": 1,
}
# 反馈闭环阈值：同一规则或同一 sim 连续失败此数次 → 本轮跳过
_AUTO_FAILURE_SKIP_THRESHOLD = 2
# 反馈闭环窗口：只看最近此条 push 记录
_AUTO_FEEDBACK_LOOKBACK = 8

# 需求紧急阈值（2026-07-28）：motive 低于此值 → 跳过 AI 直接推生存动作
# 只对物理需求生效（饥饿/膀胱/精力/卫生/舒适），fun/social 留给 AI
_MOTIVE_EMERGENCY_THRESHOLD = -80
# 需求紧急的 motive 白名单——只膀胱是纯生理（没有叙事选择空间），
# 饥饿/精力/卫生/舒适有"怎么吃/在哪睡/泡澡vs淋浴/坐哪"的故事空间，留给 AI
_MOTIVE_EMERGENCY_MOTIVES = frozenset({"bladder"})


def _is_character_unavailable(c) -> bool:
    """Return True if the character is asleep or otherwise unavailable for actions.

    Used by POV scoring, motive emergency, and auto-trigger candidate scoring
    to skip characters that can't act.  Centralized so the sleep heuristic
    (check both mood and current_action) is defined in one place.
    """
    mood_lower = (c.mood or "").lower()
    action_lower = (c.current_action or "").lower()
    return "asleep" in mood_lower or "sleep" in action_lower


@dataclass
class CycleRecord:
    """一轮决策的历史摘要——跨轮上下文注入用（会话内内存态，重启即清）。"""
    pov_id: str = ""
    pov_name: str = ""
    game_time: str = ""       # 当轮游戏时间（time_hm 或时段），供 AI 感知时间流逝
    inner_voice: str = ""
    actions_text: str = ""    # "走到 X 身边；和 X 互动" / "（未行动）"
    action_ids: list = field(default_factory=list)  # 本轮选中的 action_id 列表（去重用）
    pending_intent: str = ""  # 🆕 2026-07-28：上轮想做但未完成的意图（下轮注入 prompt）


def format_recent_cycles_for_pov(history: 'deque[CycleRecord]', pov_id: str,
                               limit: int = 2) -> str:
    """把同一 POV 的最近轮次格式化为内心声音 prompt 的 {recent_cycles} 块。

    只取当前 POV 自己的历史（别人的内心不该出现在"你此前的想法"里）；
    其他角色的行为已通过场景事件流进入 scene_context，不在此重复。

    Args:
        history: CycleRecord 可迭代（旧→新）
        pov_id: 当前视角角色 sim_id
        limit: 最多注入的轮数
    """
    mine = [r for r in history if r.pov_id == pov_id]
    if not mine:
        return "（首轮，无历史）"
    lines = []
    for r in mine[-limit:]:
        t = f"[{r.game_time}] " if r.game_time else ""
        lines.append(f"{t}想：{r.inner_voice}")
        lines.append(f"    做：{r.actions_text or '（未行动）'}")
    return "\n".join(lines)


def format_recent_actions(history: 'deque[CycleRecord]', limit: int = 4) -> str:
    """把最近轮次（全 POV）的已执行动作格式化为动作选择 prompt 的 {recent_actions} 块。

    给决策器"刚做过什么"的记忆——避免机械重复（连续三轮拥抱）、支持意图延续。

    2026-07-18 增强：检测连续重复并标注警告，让 AI 无法假装没看见。
    """
    entries = list(history)[-limit:]
    if not entries:
        return "（首轮，无历史）"

    # 收集窗口内所有 action_id 用于重复检测
    all_ids = []
    for r in entries:
        all_ids.extend(r.action_ids)

    lines = []
    for r in entries:
        t = f"[{r.game_time}] " if r.game_time else ""
        actions_display = r.actions_text or "（未行动）"
        # 标注连续重复：同一 action_id 在窗口内出现 >=3 次时追加警告
        for aid in r.action_ids:
            if all_ids.count(aid) >= 3:
                actions_display += "  ⚠️已连续重复多轮！"
                break
        lines.append(f"{t}{r.pov_name}：{actions_display}")
    return "\n".join(lines)


# ── 多轮意图链（2026-07-28）──

# 内心声音中表示"先A后B"的连接词——用于检测多步计划
_INTENT_CHAIN_WORDS = frozenset({"然后", "再", "接着", "之后", "完了", "先", "顺便"})

# 意图的最小字符长度——太短的不值得跨轮追踪
_MIN_INTENT_LENGTH = 10


def _extract_pending_intent(inner_voice: str, actions_text: str) -> str:
    """从内心声音中提取未完成的跨轮意图。

    只返回需要跨轮延续的意图文本，单步愿望不追踪。
    如果当轮未行动，整个内心声音作为意图保留。

    Returns:
        意图文本（空字符串 = 无待追踪意图）
    """
    if not inner_voice or not inner_voice.strip():
        return ""
    text = inner_voice.strip()

    # 未行动 → 整段内心都是待完成意图
    if not actions_text or actions_text == "（未行动）":
        if len(text) >= _MIN_INTENT_LENGTH:
            return text
        return ""

    # 有行动 → 检查是否有多步计划（"先...然后..."模式）
    has_chain = any(kw in text for kw in _INTENT_CHAIN_WORDS)
    if not has_chain:
        return ""  # 单步愿望，做完就了结了

    # 有多步计划 → 整段保留为意图（AI 下轮自己判断做完了哪些）
    if len(text) >= _MIN_INTENT_LENGTH:
        return text
    return ""


def _format_pending_intent(intent: str) -> str:
    """格式化为 prompt 注入块。"""
    if not intent or not intent.strip():
        return ""
    return "【你上轮想做但还没完成的事】\n" + intent.strip() + "\n\n再想想：这件事做了吗？如果还没做，继续保持这个念头。"


def _detect_scene_tags(ws: 'WorldState', present: list) -> dict[str, int]:
    """从 WorldState 检测场景标签，返回 {sim_id: score_bias}。

    五个互不排斥的标签——一个场景可以同时命中多个。
    纯函数——只读数据，不做 IO。
    """
    biases: dict[str, int] = {}
    scene = ws.scene
    objects = ws.objects or []

    # ── 解析游戏时间 ──
    hour = 12  # 默认中午
    time_str = getattr(scene, 'time_hm', '') or ''
    if ':' in time_str:
        try:
            hour = int(time_str.split(':')[0])
        except (ValueError, IndexError):
            pass

    # ── 预处理：分类 sim ──
    in_kitchen: set[str] = set()       # 在厨房的 sim
    cooking_sims: set[str] = set()     # 正在烹饪的 sim
    near_bar_stereo: set[str] = set()  # 在吧台/音响附近的 sim
    near_broken: set[str] = set()      # 在损坏物品附近的 sim
    in_bedroom: set[str] = set()       # 在卧室的 sim
    npc_count = 0
    bar_in_use = False
    stereo_on = False

    room_kitchen = {"kitchen", "厨房", "café", "diner", "restaurant"}
    room_bedroom = {"bedroom", "卧室", "bed", "nursery", "crib"}
    room_bar = {"bar", "lounge", "pub", "nightclub", "吧台", "酒廊"}
    room_stereo = {"living", "livingroom", "living room", "客厅", "dance", "club"}

    for c in present:
        if c.is_npc or not c.is_household_member:
            npc_count += 1

        room = (c.room_name or "").lower()
        action_lower = (c.current_action or "").lower()

        # 厨房相关
        if any(kw in room for kw in room_kitchen):
            in_kitchen.add(c.sim_id)
        if any(kw in action_lower for kw in ("cook", "备菜", "做菜", "烹饪", "grill", "bake")):
            cooking_sims.add(c.sim_id)

        # 吧台/音响附近
        if any(kw in room for kw in room_bar) or any(kw in room for kw in room_stereo):
            near_bar_stereo.add(c.sim_id)

        # 卧室
        if any(kw in room for kw in room_bedroom):
            in_bedroom.add(c.sim_id)

    # ── 预处理：场景物品 ──
    broken_rooms: set[str] = set()
    for o in objects:
        otype = (o.type or "").lower()
        oname = (o.name or "").lower()
        combined = f"{otype} {oname}"
        # 损坏物品
        for state in (o.states or []):
            sname = (state.get("name", "") if isinstance(state, dict) else str(state)).lower()
            if any(kw in sname for kw in ("broken", "spark", "malfunction", "burnt")):
                broken_rooms.add(o.room_id or "")
                break
        # 吧台被使用
        if any(kw in combined for kw in ("bar", "吧台")) and o.in_use_by:
            bar_in_use = True
        # 音响开着
        if any(kw in combined for kw in ("stereo", "speaker", "音响", "音箱")):
            for state in (o.states or []):
                sname = (state.get("name", "") if isinstance(state, dict) else str(state)).lower()
                if sname == "on":
                    stereo_on = True
                    break

    # ── 标签 1: meal_time ──
    if cooking_sims or (11 <= hour <= 13) or (17 <= hour <= 20) or (len(in_kitchen) >= 2):
        for c in present:
            if c.sim_id in cooking_sims:
                biases[c.sim_id] = biases.get(c.sim_id, 0) + 15
            hunger = c.motives.get("hunger")
            if hunger is not None:
                try:
                    if float(hunger) < 30:
                        biases[c.sim_id] = biases.get(c.sim_id, 0) + 15
                except (TypeError, ValueError):
                    pass

    # ── 标签 2: social_gathering ──
    if npc_count >= 3 or bar_in_use or stereo_on:
        for c in present:
            social = c.motives.get("social")
            if social is not None:
                try:
                    if float(social) < 30:
                        biases[c.sim_id] = biases.get(c.sim_id, 0) + 10
                except (TypeError, ValueError):
                    pass
            if c.sim_id in near_bar_stereo:
                biases[c.sim_id] = biases.get(c.sim_id, 0) + 10

    # ── 标签 3: crisis ──
    if broken_rooms:
        for c in present:
            # 有修理相关技能 → 首选
            skills = c.skills or []
            has_handiness = any(
                (isinstance(s, dict) and "handiness" in (s.get("name", "") or "").lower())
                or (isinstance(s, dict) and "修理" in (s.get("name", "") or ""))
                for s in skills
            )
            if has_handiness:
                biases[c.sim_id] = biases.get(c.sim_id, 0) + 20
            # 在损坏物品附近
            if c.room_id and c.room_id in broken_rooms:
                biases[c.sim_id] = biases.get(c.sim_id, 0) + 15

    # ── 标签 4: wind_down ──
    if (21 <= hour or hour <= 3) or (len(in_bedroom) >= 2):
        for c in present:
            energy = c.motives.get("energy")
            if energy is not None:
                try:
                    if float(energy) < 30:
                        biases[c.sim_id] = biases.get(c.sim_id, 0) + 15
                except (TypeError, ValueError):
                    pass

    # ── 标签 5: morning_rush ──
    if 5 <= hour <= 9:
        for c in present:
            for motive_key in ("hygiene", "bladder"):
                val = c.motives.get(motive_key)
                if val is not None:
                    try:
                        if float(val) < 30:
                            biases[c.sim_id] = biases.get(c.sim_id, 0) + 10
                    except (TypeError, ValueError):
                        pass

    return biases


def _score_pov_candidates(present, ws, cycle_history,
                          action_tracker=None) -> list:
    """对在场角色打分，返回按分数降序的 [(CharacterState, score), ...] 列表。

    评分维度（2026-07-26）：
      - 冷却: 最近 2 轮内说过话 → -80 / -40
      - 未了结: 上轮想了没做 → +30
      - 需求告急: 每个 < 阈值的需求 → +10，上限 +50
      - 动作失败: 上轮动作被拒/超时 → +25
      - 睡眠惩罚: 正在睡觉 → -60
      - 特殊动画状态中 → -40
      - 事件相关: 在最近事件流中作为 actor/target → +15
      - 场景标签偏置（2026-07-28）: meal_time/social_gathering/crisis/wind_down/morning_rush
        → 各 +10~+20，按 sim 状态和位置分配
      - 随机扰动: 0~5 随机分防同分僵持
    """
    # ── 预处理：从 cycle_history 提取信息 ──
    recent_pov_ids = set()       # 最近 1 轮的 POV
    recent2_pov_ids = set()      # 最近 2 轮的 POV
    unfinished_ids = set()       # 上轮想了但没做的角色
    history_list = list(cycle_history)
    if history_list:
        last = history_list[-1]
        recent_pov_ids.add(last.pov_id)
        if last.actions_text == "（未行动）":
            unfinished_ids.add(last.pov_id)
    if len(history_list) >= 2:
        recent2_pov_ids.add(history_list[-2].pov_id)

    # ── 预处理：上轮动作失败的角色 ──
    failed_ids = set()
    if action_tracker is not None:
        for lc in action_tracker.get_recent(5):
            if lc.stage in ("rejected", "timeout", "stuck", "failed"):
                failed_ids.add(lc.character_id)

    # ── 预处理：最近事件中的角色 ──
    event_ids = set()
    for ev in (ws.recent_events or [])[-8:]:
        for name_field in ("actor_name", "target_name"):
            name = getattr(ev, name_field, "") or ""
            for c in present:
                if c.name == name:
                    event_ids.add(c.sim_id)

    # ── 场景标签偏置（2026-07-28）──
    tag_biases = _detect_scene_tags(ws, present)

    # ── 逐人打分 ──
    scored = []
    for c in present:
        score = 0

        # 冷却
        if c.sim_id in recent_pov_ids:
            score -= 80
        elif c.sim_id in recent2_pov_ids:
            score -= 40

        # 未了结
        if c.sim_id in unfinished_ids:
            score += 30

        # 需求告急（motive < -20 或 < 30 视为告急，视量程）
        low_count = 0
        for k, v in (c.motives or {}).items():
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            # 量程 -100..100：< -20 = 告急；0..100：< 30 = 告急
            if val < -20 or (val >= 0 and val < 30):
                low_count += 1
        score += min(low_count * 10, 50)

        # 动作失败
        if c.sim_id in failed_ids:
            score += 25

        # 睡眠惩罚
        if _is_character_unavailable(c):
            score -= 60

        # 特殊动画状态检测（mod 特定实现已省略）

        # 事件相关
        if c.sim_id in event_ids:
            score += 15

        # 场景标签偏置
        score += tag_biases.get(c.sim_id, 0)

        # 当前动作：闲着的人优先，忙着的让他忙完
        if _is_idle_action(c.current_action):
            score += 10   # 闲着 → 给他找事做
        elif c.current_action and c.current_action.strip():
            score -= 10   # 忙着 → 别打扰

        # 随机扰动
        score += random.randint(0, 5)

        scored.append((c, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


class SandboxEngine:
    """沙盘 AI 引擎——管理两段式 AI 调用和动作选择。"""

    def __init__(self):
        self._cancel_event = threading.Event()
        self._last_inner_voice: str = ""
        self._last_actions: list[ActionOption] = []
        self._is_running: bool = False
        # 跨轮上下文（v2 导演层第一步）：最近 N 轮的 POV/内心/动作摘要。
        # 会话内内存态——换存档/重启即清，不落盘（v1"不持久化"仅放宽到会话内）。
        self._cycle_history: deque = deque(maxlen=CYCLE_HISTORY_MAX)
        # POV 轮转索引（自动模式下每轮 +1，轮完一圈从头来）
        self._pov_rotation_index: int = 0
        # 上轮动作选择失败原因（供 run_cycle 读取后填入 EngineResult）
        self._last_action_error: str = ""
        # 推入历史追踪器（Tier 2，2026-07-23）：会话内学习哪些 hints 有效
        from .push_history import PushHistoryTracker
        self.push_history = PushHistoryTracker()
        # 动作生命周期追踪器（2026-07-24）：追踪每个推送动作的完整生命周期
        from .action_lifecycle import create_tracker
        self.action_tracker = create_tracker()
        # 延迟动作队列（Tier 3，2026-07-23）：被门暂缓的动作
        from .push_gate import DeferredActionQueue
        self._deferred_queue = DeferredActionQueue()

    # ── 公开 API ──

    def clear_history(self):
        """清空跨轮上下文 + 推入历史 + 延迟队列 + 动作生命周期（换家庭/换存档时由上层调用）。"""
        self._cycle_history.clear()
        self._pov_rotation_index = 0
        self.push_history.clear()
        self.action_tracker.clear()
        self._deferred_queue = type(self._deferred_queue)()  # 重建空队列

    def is_running(self) -> bool:
        return self._is_running

    def cancel(self):
        """取消当前正在运行的 AI 调用。"""
        self._cancel_event.set()

    def reset_cancel(self):
        self._cancel_event.clear()

    @staticmethod
    def _extract_auto_rule_id(action_id: str) -> str:
        """从 action_id 提取 rule_id：custom_{rule_id}_{actor}_{target} → rule_id。

        actor_sim_id 是唯一保证纯数字的段——找到它作为锚点，
        之前的所有段（用 _ 重新拼接）就是 rule_id。
        """
        if not action_id or not action_id.startswith("custom_"):
            return ""
        # 格式: custom_{rule_id}_{actor_sim_id}_{target_safe_id}
        # rule_id 可含 _（如 turn_on_stereo），target_safe_id 也可含 _（如 obj_trash_01）
        # 但 actor_sim_id 一定是纯数字——以此为锚
        rest = action_id[len("custom_"):]
        segments = rest.split("_")
        # 找第一个纯数字段 → 那是 actor_sim_id 的开始
        anchor_idx = -1
        for i, seg in enumerate(segments):
            if seg.isdigit() and len(seg) >= 6:  # sim_id 至少 6 位
                anchor_idx = i
                break
        if anchor_idx >= 1:
            return "_".join(segments[:anchor_idx])
        # 回退：找不到纯数字段时返回全部（不应该发生，但不抛异常）
        return rest

    def _read_auto_feedback(self) -> tuple[set[str], set[str]]:
        """Scan push_history for consecutive failures and return (sim_ids, rule_ids)
        to skip this round.

        Feedback loop (2026-07-28): when a sim or rule has _AUTO_FAILURE_SKIP_THRESHOLD
        consecutive failures, skip them this round to give others a chance.

        Two levels:
          - Per-sim: if all of a sim's recent auto actions failed → skip sim entirely
          - Per-rule: if every sim that tried this rule keeps failing → skip rule globally
            (e.g. "repair" requires a broken object — if nothing is broken, it always fails)
        """
        failed_sims: set[str] = set()
        failed_rules: set[str] = set()
        records = list(self.push_history._records)[-_AUTO_FEEDBACK_LOOKBACK:]
        if not records:
            return failed_sims, failed_rules

        # Collect per-sim and per-rule success/failure sequences.
        # Only auto-triggered actions participate (custom_ prefix).
        sim_results: dict[str, list[bool]] = {}
        rule_results: dict[str, list[bool]] = {}

        for r in records:
            rule_id = self._extract_auto_rule_id(r.action_id)
            if not rule_id:
                continue
            ok = r.status == "pushed"
            sim_results.setdefault(r.character_id, []).append(ok)
            rule_results.setdefault(rule_id, []).append(ok)

        # Per-sim: skip sim if its last N auto actions all failed.
        for sid, results in sim_results.items():
            recent = results[-_AUTO_FAILURE_SKIP_THRESHOLD:]
            if len(recent) >= _AUTO_FAILURE_SKIP_THRESHOLD and not any(recent):
                failed_sims.add(sid)

        # Per-rule: skip rule globally when every sim that tried it fails
        # N times in a row.  This catches "broken object already fixed" and
        # similar environmental failures that aren't sim-specific.
        for rule_id, results in rule_results.items():
            recent = results[-_AUTO_FAILURE_SKIP_THRESHOLD:]
            if len(recent) >= _AUTO_FAILURE_SKIP_THRESHOLD and not any(recent):
                failed_rules.add(rule_id)

        if failed_sims or failed_rules:
            log_message("engine.auto_feedback",
                        f"Skipping {len(failed_sims)} sims ({failed_sims}), "
                        f"{len(failed_rules)} rules ({failed_rules}) "
                        f"due to {_AUTO_FAILURE_SKIP_THRESHOLD}+ consecutive failures")
        return failed_sims, failed_rules

    def _execute_motive_emergency(self, ws: WorldState,
                                   skip_sim_ids: set[str] | None = None) -> list[ActionOption]:
        """扫描物理需求告急的角色，绕过 AI 直接推生存动作。

        当 hunger/bladder/energy/hygiene/comfort < _MOTIVE_EMERGENCY_THRESHOLD (-80) 时，
        不需要 AI 判断"要不要吃东西"——直接匹配物品推 push。
        fun/social 留给 AI（非生存需求，需要叙事判断）。
        每人只取最急的一个 motive（一个 sim 不能同时吃东西和上厕所）。

        优先级 P4（最高）——生存 > 修理 > 清洁 > 收集。
        """
        from .action_catalog import _MOTIVE_OBJECT_RULES, _match_motive_object, _safe_id

        if skip_sim_ids is None:
            skip_sim_ids = set()

        present = ws.get_present_characters()
        if not present:
            return []

        actions = []
        for c in present:
            if c.sim_id in skip_sim_ids:
                continue
            if _is_character_unavailable(c):
                continue
            # 特殊动画状态已跳过（mod 特定实现已省略）

            # 找最紧急的物理需求（值最小 = 最缺）
            most_critical = None  # (value, rule, obj)
            for rule in _MOTIVE_OBJECT_RULES:
                if rule.motive not in _MOTIVE_EMERGENCY_MOTIVES:
                    continue
                raw = c.motives.get(rule.motive)
                if raw is None:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if value >= _MOTIVE_EMERGENCY_THRESHOLD:
                    continue
                obj = _match_motive_object(ws.objects, rule)
                if obj is None:
                    continue
                if most_critical is None or value < most_critical[0]:
                    most_critical = (value, rule, obj)

            if most_critical is None:
                continue

            value, rule, obj = most_critical
            obj_name = obj.name or obj.type or "物品"
            opt = ActionOption(
                action_id=f"auto_need_{rule.motive}_{c.sim_id}_{_safe_id(obj.object_id)}",
                character_id=c.sim_id,
                character_name=c.name,
                action_type="push",
                target_kind="object",
                description=f"{c.name} {rule.verb.format(obj=obj_name)}",
                target_id=obj.object_id,
                target_name=obj_name,
                affordance_hints=list(rule.hints),
                prompt_label=rule.label,
                group="need",
            )
            opt = dataclasses.replace(opt, is_auto=True)
            if c.current_action and c.current_action.strip() and not _is_idle_action(c.current_action):
                opt = dataclasses.replace(opt, clear_queue=True)
            actions.append(opt)

        if actions:
            log_message("engine.motive_emergency",
                        f"[P4] Auto-triggered {len(actions)} survival actions: "
                        f"{[(a.character_name, a.description) for a in actions]}")
        return actions

    def _execute_auto_triggers(self, ws: WorldState,
                              ai_actions: list[ActionOption] | None = None,
                              extra_skip_sim_ids: set[str] | None = None) -> list[ActionOption]:
        """扫描 auto_trigger=True 的规则，条件满足的直接生成动作（跳过 AI）。

        用于"看到就做"的反应式动作：收垃圾、修电器、拖水坑等。

        2026-07-27 重构：评分选择替代先到先得。
        2026-07-28 升级：优先级排序（修理>清洁>收集）+ 反馈闭环（连续失败→跳过）。
        - 每个规则匹配最佳角色（技能/距离/空闲/优先级），而非第一个合格就停
        - 多规则同时命中时按优先级分配高优先规则先挑 sim
        - 已出现在 ai_actions 中的 sim/object 自动跳过
        - 连续失败的 sim/规则 本轮跳过（给其他人机会）
        - extra_skip_sim_ids: 需求紧急已占用的 sim（不给同一人又派自动动作）
        """
        from .custom_actions import iter_catalog_rules
        from .action_catalog import build_option_from_rule, match_custom_object

        # 收集 AI 已占用的 sim 和 object
        ai_sim_ids: set[str] = set()
        ai_obj_ids: set[str] = set()
        if ai_actions:
            for a in ai_actions:
                ai_sim_ids.add(a.character_id)
                if a.target_id and a.target_kind == "object":
                    ai_obj_ids.add(a.target_id)
        if extra_skip_sim_ids:
            ai_sim_ids |= extra_skip_sim_ids

        present = ws.get_present_characters()
        if not present:
            return []

        # ── 反馈闭环：读 push_history 找出最近连续失败的 sim/规则 ──
        failed_sim_ids, failed_rule_ids = self._read_auto_feedback()

        # ── 第一遍：收集所有匹配的 (rule, obj, best_char, score) ──
        matches: list[tuple] = []  # [(rule, obj, best_char, best_score, candidates)]
        for rule in iter_catalog_rules():
            if not rule.auto_trigger:
                continue
            if rule.target_kind != "object":
                continue
            # 连续失败的规则本轮跳过
            if rule.rule_id in failed_rule_ids:
                continue

            # 匹配场景物品（跳过 AI 已占用的物品）
            obj = match_custom_object(ws.objects, rule)
            if obj is None:
                continue
            if obj.object_id and obj.object_id in ai_obj_ids:
                continue

            # 对每个合格角色评分，选最高分（黑名单 = AI已占 + 最近失败）
            skip_ids = ai_sim_ids | failed_sim_ids
            candidates = self._score_auto_candidates(rule, obj, present, ws, skip_ids)
            if not candidates:
                continue

            best_char, best_score = candidates[0]
            matches.append((rule, obj, best_char, best_score, candidates))

        # ── 按优先级排序（修理 > 清洁 > 收集），同级按评分降序 ──
        if matches:
            matches.sort(key=lambda m: (
                -_AUTO_RULE_PRIORITY.get(m[0].rule_id.split("_", 1)[0], 0),
                -m[3],  # best_score
            ))

        # ── 第二遍：按优先级分配 sim/object（高优先先占）──
        actions = []
        assigned_obj_ids: set[str] = set()  # 本轮已分配的物品
        for rule, obj, best_char, best_score, candidates in matches:
            if best_char.sim_id in ai_sim_ids:
                continue
            if obj.object_id and obj.object_id in assigned_obj_ids:
                continue

            opt = build_option_from_rule(rule, best_char, obj)
            if opt is None:
                continue
            opt = dataclasses.replace(opt, is_auto=True)
            if best_char.current_action and best_char.current_action.strip() and not _is_idle_action(best_char.current_action):
                opt = dataclasses.replace(opt, clear_queue=True)
            actions.append(opt)
            ai_sim_ids.add(best_char.sim_id)
            if obj.object_id:
                assigned_obj_ids.add(obj.object_id)

            if len(candidates) > 1:
                runner_up = ", ".join(f"{c.name}({s})" for c, s in candidates[1:3])
                prio = _AUTO_RULE_PRIORITY.get(rule.rule_id.split("_", 1)[0], 0)
                log_message("engine.auto_triggers",
                            f"[P{prio}] {rule.label}: picked {best_char.name}({best_score}) "
                            f"over [{runner_up}]")

        if actions:
            log_message("engine.auto_triggers",
                        f"Found {len(actions)} auto-trigger actions (sorted by priority): "
                        f"{[(a.character_name, a.description) for a in actions]}")
        return actions

    @staticmethod
    def _score_auto_candidates(rule, obj, present: list, ws: WorldState,
                               skip_sim_ids: set[str]) -> list[tuple]:
        """对在场角色按适配度评分，返回 [(CharacterState, score), ...] 降序。

        评分维度：
          - 优先级 "回避" → 直接跳过（不出现在结果中）
          - 优先级 "优先" → +15
          - 空闲(idle) → +10
          - 有相关技能 → +5
          - 同房间 → +3
          - 有相关 buff → +3
          - AI 已占用 → 直接跳过
          - 睡觉/特殊动画状态 → 直接跳过
        """
        from .custom_actions import rule_schema
        from .settings import get_character_priority, CHAR_PRIORITY_AVOID, CHAR_PRIORITY_HIGH

        scored = []
        for c in present:
            # 硬排除
            if c.sim_id in skip_sim_ids:
                continue
            if _is_character_unavailable(c):
                continue
            # 特殊动画状态已跳过（mod 特定实现已省略）

            # 年龄门槛
            from .action_catalog import _meets_min_age
            if not _meets_min_age(getattr(c, "age", ""), rule.min_age):
                continue

            # 技能门槛（硬门槛，不满足直接跳过）
            if rule.min_skill:
                char_skills = {}
                for sk in (c.skills or []):
                    if isinstance(sk, dict):
                        char_skills[sk.get("name", "")] = int(sk.get("level", 0) or 0)
                from .action_catalog import _check_min_skill
                if not _check_min_skill(char_skills, rule.min_skill):
                    continue

            # ── 评分 ──
            score = 0

            # 用户指定优先级
            priority = get_character_priority(c.name)
            if priority == CHAR_PRIORITY_AVOID:
                continue  # "回避"直接跳过
            if priority == CHAR_PRIORITY_HIGH:
                score += 15

            # 空闲
            if _is_idle_action(c.current_action):
                score += 10

            # 有相关技能
            if rule.min_skill:
                for skill_name in rule.min_skill:
                    if skill_name.lower() in (k.lower() for k in char_skills):
                        score += 5
                        break

            # 同房间（物品有 room_id 且匹配）
            obj_room = getattr(obj, 'room_id', None)
            if obj_room and c.room_id == obj_room:
                score += 3

            # 有相关 buff（脏乱环境→清洁、恶心→清洁）
            if rule.rule_id.startswith(("clean", "collect", "mop", "repair")):
                for b in (c.buffs or []):
                    bname = (b.get("name", "") if isinstance(b, dict) else str(b)).lower()
                    if any(kw in bname for kw in ("dirty", "filthy", "nauseous", "uncomfortable")):
                        score += 3
                        break

            scored.append((c, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    @staticmethod
    def _allocate_actions(auto_actions: list[ActionOption],
                         ai_actions: list[ActionOption],
                         motive_actions: list[ActionOption] | None = None) -> list[ActionOption]:
        """合并三种来源的动作：需求紧急 > AI > 自动。

        规则：
        0. 需求紧急先占 sim + object（生存第一）
        1. AI 动作占位
        2. 自动动作填空（sim 空闲 + object 未被占用）
        3. 同一 sim 冲突 → 高优先胜出（motive > AI > auto）
        4. 同一 object 冲突 → 高优先胜出
        """
        used_sims: set[str] = set()
        used_objects: set[str] = set()
        result: list[ActionOption] = []

        # Tier 0: 需求紧急 — 生存 > 一切
        if motive_actions:
            for a in motive_actions:
                if a.character_id in used_sims:
                    continue
                if a.target_id and a.target_kind == "object" and a.target_id in used_objects:
                    continue
                result.append(a)
                used_sims.add(a.character_id)
                if a.target_id and a.target_kind == "object":
                    used_objects.add(a.target_id)

        # Tier 1: AI 动作先占位
        for a in ai_actions:
            if a.character_id in used_sims:
                log_message("engine.allocate",
                            f"Dropped AI action {a.description} — sim {a.character_name} busy")
                continue
            if a.target_id and a.target_kind == "object" and a.target_id in used_objects:
                log_message("engine.allocate",
                            f"Dropped AI action {a.description} — object {a.target_name} used")
                continue
            result.append(a)
            used_sims.add(a.character_id)
            if a.target_id and a.target_kind == "object":
                used_objects.add(a.target_id)

        # 自动动作填空
        for a in auto_actions:
            if a.character_id in used_sims:
                log_message("engine.allocate",
                            f"Dropped auto action {a.description} — sim {a.character_name} has AI action")
                continue
            if a.target_id and a.target_kind == "object" and a.target_id in used_objects:
                log_message("engine.allocate",
                            f"Dropped auto action {a.description} — object {a.target_name} has AI action")
                continue
            result.append(a)
            used_sims.add(a.character_id)
            if a.target_id and a.target_kind == "object":
                used_objects.add(a.target_id)

        dropped_auto = len(auto_actions) - sum(1 for a in result if getattr(a, 'is_auto', False))
        if dropped_auto > 0:
            log_message("engine.allocate",
                        f"Dropped {dropped_auto} auto actions due to sim/object conflict with AI")

        return result

    def run_cycle(self, ws: WorldState, pov_character_id: str = "",
                  include_unverified: bool = False,
                  player_rules: str = "") -> "EngineResult":
        """执行一轮完整的决策循环。

        Args:
            ws: 当前世界状态快照
            pov_character_id: 内心声音的视角角色 sim_id（空=第一个在场角色）
            include_unverified: True=AI 目录纳入 unverified 规则（自动测试模式）
            player_rules: 玩家自定义规则文本，注入 prompt 的 {player_rules} 占位符

        Returns:
            EngineResult（含内心声音 + 选中动作）
        """
        self._is_running = True
        self.reset_cancel()
        try:
            # Step 0a: 需求紧急自动触发——生存需求不走 AI（2026-07-28）
            motive_actions = self._execute_motive_emergency(ws)
            motive_sim_ids = {a.character_id for a in motive_actions}

            # Step 0b: 自动触发动作（看到就做，不走 AI）
            # 已分配需求紧急的 sim 不参与自动动作（不给同一个人同时派两件事）
            auto_actions = self._execute_auto_triggers(ws,
                                                       extra_skip_sim_ids=motive_sim_ids)

            # Step 1: 获取连接设置
            settings = get_sandbox_connection_settings()
            if not settings:
                return EngineResult(
                    success=False,
                    error="无法获取 API 连接设置。请编辑 Mythica_Settings.json 配置 API Profile，或在 Mythica 主程序的连接标签页中配置。",
                )

            # Step 2: 解析 POV 角色（内心声音视角 + 历史记录归属）
            pov = self._resolve_pov(ws, pov_character_id)
            if pov is None:
                return EngineResult(
                    success=False,
                    error="无在场角色，无法生成内心声音。",
                )

            # Step 3: 内心声音（注入该 POV 的跨轮历史 + 上轮未完意图）
            pending_intent = ""
            if self._cycle_history:
                last = self._cycle_history[-1]
                if last.pov_id == pov.sim_id:
                    pending_intent = last.pending_intent
            inner_voice, iv_prompt = self._call_inner_voice(ws, settings, pov,
                                                            player_rules=player_rules,
                                                            pending_intent=pending_intent)
            if inner_voice is None:
                return EngineResult(
                    success=False,
                    error="内心声音生成失败。",
                    inner_voice_prompt=iv_prompt,
                )
            self._last_inner_voice = inner_voice

            # Step 4: 生成上下文感知动作目录（Tier 4，2026-07-23）
            from .catalog_context import build_push_context
            push_ctx = build_push_context(ws)
            catalog = generate_action_catalog(ws, ctx=push_ctx,
                                                include_unverified=include_unverified)

            # Step 5: 动作选择（注入最近几轮已执行动作 + POV buff 上下文）
            selected = self._call_action_selector(inner_voice, catalog, ws, settings,
                                                   pov_name=pov.name, pov_char=pov,
                                                   player_rules=player_rules)

            # Step 5b: 合并分配——需求紧急 > AI > 自动（2026-07-28）
            all_actions = self._allocate_actions(auto_actions, selected,
                                                  motive_actions=motive_actions)

            # Step 5b2: 自动补 goto——物品动作需要走过去时（observer 实证驱动）。
            # 只在 observer 记录此动作通常先 gohere 且 sim 在物品不同房间时插入。
            all_actions = _ensure_object_proximity(all_actions, ws)

            # Step 5c: 硬去重——同一 action_id 在最近 RECENT_DEDUP_CYCLES 轮内已选过 → 过滤。
            # 自动动作和 AI 动作都参与去重（自动动作以 rule_id+object_id 进 cycle_history）。
            dedup_dropped = 0
            if all_actions and self._cycle_history:
                recent_ids = set()
                for r in list(self._cycle_history)[-RECENT_DEDUP_CYCLES:]:
                    recent_ids.update(r.action_ids)
                deduped = [
                    a for a in all_actions
                    if a.action_id not in recent_ids
                    or a.action_type in ("idle", "stop")
                ]
                dedup_dropped = len(all_actions) - len(deduped)
                if dedup_dropped > 0:
                    log_message("engine.dedup",
                                f"Blocked {dedup_dropped} repeat action(s) "
                                f"(last {RECENT_DEDUP_CYCLES} cycles): "
                                f"{[a.action_id for a in all_actions if a.action_id in recent_ids]}")
                all_actions = deduped

            # Step 5d: 自动补 goto——AI 选了"和 X 互动"但两人在不同房间时，
            # 在 push 前 prepend goto+sim。只对 AI 动作做（自动动作已经有物品 goto）。
            # 🔧 2026-07-28 注释掉：游戏端 push 社交 interaction 自带寻路走近目标，
            # terrain-gohere prepend 多余且 ~40% 被拒，去掉验证。
            # all_actions = _ensure_proximity(all_actions, ws)

            # 重新拆分 auto/selected 供 result（motive+auto_trigger 归 auto，其余归 selected）
            auto_action_ids = {a.action_id for a in auto_actions}
            motive_action_ids = {a.action_id for a in motive_actions}
            final_motive = [a for a in all_actions if a.action_id in motive_action_ids]
            final_auto = [a for a in all_actions if a.action_id in auto_action_ids
                         and a.action_id not in motive_action_ids]
            final_selected = [a for a in all_actions
                            if a.action_id not in auto_action_ids
                            and a.action_id not in motive_action_ids]
            self._last_actions = final_selected

            # Step 6: 记录跨轮历史（所有动作都记录——AI 需要知道发生过什么）
            scene = ws.scene
            game_time = getattr(scene, 'time_hm', '') or getattr(scene, 'time_of_day', '')
            actions_text_parts = []
            if final_motive:
                actions_text_parts.append("[生存] " + "；".join(a.description for a in final_motive))
            if final_auto:
                actions_text_parts.append("[自动] " + "；".join(a.description for a in final_auto))
            if final_selected:
                actions_text_parts.append("[AI] " + "；".join(a.description for a in final_selected))
            actions_text = " | ".join(actions_text_parts) if actions_text_parts else "（未行动）"
            action_ids = [a.action_id for a in all_actions] if all_actions else []
            # 提取本轮内心声音中未完成的多步意图（供下轮注入）
            new_pending = _extract_pending_intent(inner_voice, actions_text)
            self._cycle_history.append(CycleRecord(
                pov_id=pov.sim_id,
                pov_name=pov.name,
                game_time=game_time,
                inner_voice=inner_voice,
                actions_text=actions_text,
                action_ids=action_ids,
                pending_intent=new_pending,
            ))

            # 确定无动作原因
            no_action_reason = ""
            if not all_actions:
                if self._last_action_error:
                    no_action_reason = self._last_action_error
                elif dedup_dropped > 0:
                    no_action_reason = f"all_deduped: 全部{dedup_dropped}个动作被去重拦截"
                else:
                    no_action_reason = self._last_action_error or "unknown: 未知原因无动作"

            return EngineResult(
                success=True,
                pov_name=pov.name,
                inner_voice=inner_voice,
                inner_voice_prompt=iv_prompt,
                selected_actions=final_selected,
                auto_actions=final_auto,
                motive_actions=final_motive,
                catalog=catalog,
                no_action_reason=no_action_reason,
            )

        finally:
            self._is_running = False

    # ── 内部方法 ──

    def _resolve_pov(self, ws: WorldState, pov_character_id: str = ""):
        """解析本轮 POV 角色（2026-07-26 智能选择）。

        - 指定 sim_id → 直接匹配（不在场回退自动）
        - 自动模式 → 按游戏状态打分：谁最需要发言选谁
          · 刚说过话的 → 冷却
          · 上轮想了没做的 → 加分
          · 需求告急的 → 加分
          · 动作刚失败的 → 加分
          · 睡着/特殊动画状态中的 → 降权
          · 在最近事件中的 → 加分
        """
        present = ws.get_present_characters()
        if not present:
            log_message("engine.resolve_pov",
                        f"No present characters (total chars: {len(ws.characters)}, "
                        f"present_ids: {ws.scene.present_sim_ids})")
            return None
        if pov_character_id:
            character = ws.get_character(pov_character_id)
            if character is not None and character in present:
                return character

        # ── 智能打分选 POV ──
        if len(present) == 1:
            return present[0]

        scored = _score_pov_candidates(present, ws, self._cycle_history,
                                        self.action_tracker)
        chosen = scored[0][0]
        log_message("engine.resolve_pov",
                    f"POV scored: {[(c.name, s) for c, s in scored[:5]]} → {chosen.name}")
        return chosen

    def _call_inner_voice(self, ws: WorldState, settings: dict,
                          character, player_rules: str = "",
                          pending_intent: str = "") -> tuple[Optional[str], str]:
        """AI Call 1: 生成内心声音。

        Returns:
            (inner_voice_text, full_user_prompt) — text 为 None 表示失败

        Args:
            character: 已解析的 POV 角色（CharacterState，由 _resolve_pov 产出）
            player_rules: 玩家自定义规则，注入 system prompt 的 {player_rules}
            pending_intent: 🆕 上轮未完意图，注入 prompt 提醒 AI 延续
        """
        present = ws.get_present_characters()

        # 构建"周围有谁"信息——极简摘要（不重复 scene_context 的完整角色详情）
        others = [c for c in present if c.sim_id != character.sim_id]
        others_text = ""
        if others:
            briefs = []
            for c in others:
                loc = "室外" if c.is_outside else f"房间#{c.room_id}" if c.room_id else ""
                mood_short = c.mood or ""
                action_short = c.current_action if not _is_idle_action(c.current_action) else ""
                parts = [c.name]
                if mood_short:
                    parts.append(mood_short)
                if action_short:
                    parts.append(f"正在{action_short}")
                if loc:
                    parts.append(loc)
                briefs.append("，".join(parts))
            others_text = "周围其他人：" + "；".join(briefs)

        # POV 个人状态块（v12：位置/着装/低需求/愿望/恐惧）+ 人际关系块
        pov_status = character.to_pov_status()
        relations = ws.describe_relations(character.sim_id)
        relations_context = f"【{character.name} 的人际】\n{relations}\n" if relations else ""

        # current_action 归一：Sim-Stand 等 idle 噪音视为无动作
        action_display = character.current_action
        if _is_idle_action(action_display):
            action_display = ""

        user_prompt = INNER_VOICE_USER.format(
            scene_context=ws.to_prompt_context(),
            character_name=character.name,
            persona=character.to_persona() or "",
            traits=", ".join(character.traits) if character.traits else "未知",
            mood=character.mood or "平静",
            current_action=action_display or "无特定动作",
            pov_status=pov_status,
            pending_intent=_format_pending_intent(pending_intent),
            recent_cycles=format_recent_cycles_for_pov(
                self._cycle_history, character.sim_id),
            relations_context=relations_context,
            others_context=others_text,
        )

        try:
            result = call_ai(AICallParams(
                provider=settings["provider"],
                api_key=settings["api_key"],
                model=settings["model"],
                prompt=user_prompt,
                system_prompt=INNER_VOICE_SYSTEM.format(
                    player_rules=f"\n【玩家自定义规则】\n{player_rules}\n" if player_rules.strip() else ""),
                custom_api_url=settings.get("custom_api_url", ""),
                timeout=120,
                cancel_event=self._cancel_event,
                thinking_disabled=False,
            ))
        except Exception:
            log_exception("engine.inner_voice", f"AI call failed for {character.name}")
            return None, user_prompt

        if not result:
            log_message("engine.inner_voice", f"AI returned empty result for {character.name}")
            return None, user_prompt

        # 清理：去掉可能的引号包裹
        text = result.strip().strip('"').strip("'").strip()
        return text, user_prompt

    def _call_action_selector(
        self,
        inner_voice: str,
        catalog: list[ActionOption],
        ws: WorldState,
        settings: dict,
        pov_name: str = "",
        pov_char=None,  # CharacterState — 用于构建 buff/心情上下文
        player_rules: str = "",
    ) -> list[ActionOption]:
        """AI Call 2: 选择动作。"""
        self._last_action_error = ""  # 每轮重置
        if not catalog:
            self._last_action_error = "empty_catalog: 无可选动作目录"
            return []

        catalog_text = format_catalog_for_prompt(catalog)

        # POV 角色状态块（buff + 心情 + 低需求——让 AI 直接看到客观游戏状态）
        pov_buff_context = ""
        if pov_char is not None:
            pov_buff_context = pov_char.to_buff_context() or "（无异常状态）"

        # ── 游戏时间上下文（2026-07-28 P3）：帮 AI 了解何时动作容易被自主行为抢走 ──
        game_time_context = ""
        scene = ws.scene
        if scene and scene.time_hm:
            try:
                hour = int(scene.time_hm.split(":")[0])
                if 0 <= hour < 6:
                    game_time_context = (
                        f"⚠️ 当前游戏时间 {scene.time_of_day or scene.time_hm}——凌晨时段，sim 倾向睡觉/休息。"
                        f"社交和活动类动作极易被游戏自主行为（睡眠/自主清洁）抢走。优先选择生存需求类或安静的单人动作。"
                    )
                elif 6 <= hour < 8:
                    game_time_context = (
                        f"⏰ 当前游戏时间 {scene.time_of_day or scene.time_hm}——清晨，sim 陆续起床。"
                        f"社交动作可行但 sim 可能先去解决饥饿/卫生需求。"
                    )
                elif 22 <= hour <= 23:
                    game_time_context = (
                        f"🌙 当前游戏时间 {scene.time_of_day or scene.time_hm}——深夜，sim 可能准备睡觉。"
                        f"避免选耗时长的社交/活动动作，优先选短动作或需求解决。"
                    )
            except (ValueError, IndexError):
                pass

        # 格式化场景物品列表（与 catalog 中的物品操作选项保持一致；v12 加占用/异常标注）
        top_objects = ws.objects[:MAX_OBJECT_ACTIONS_PER_CHAR]
        if top_objects:
            obj_lines = []
            for o in top_objects:
                label = o.name or o.type or "未知物品"
                if o.category:
                    label += f" [{o.category}]"
                tags = _object_tags(o)
                if tags:
                    label += tags
                obj_lines.append(f"  - {label}")
            scene_objects = "\n".join(obj_lines)
        else:
            scene_objects = "（当前场景无可交互物品）"

        # ── Tier 2 反馈文本（2026-07-23）──
        rejection_text = self.push_history.recent_rejections_summary(limit=5)
        if rejection_text:
            rejection_text = f"⚠️ 最近被拒的动作（避免重复选）：{rejection_text}"
        effective_text = self.push_history.effective_approach_summary(limit=3)
        if effective_text:
            effective_text = f"🎯 历史上有效的 hints（优先参考）：{effective_text}"
        # ── 生命周期反馈（2026-07-24）──
        cycle_outcome = self.action_tracker.last_cycle_summary(limit=3)
        if cycle_outcome:
            cycle_outcome = f"📋 上轮动作结果（据此调整策略，避免重复无效动作）：\n{cycle_outcome}"
            log_message("engine.feedback",
                        f"lifecycle_outcome len={len(cycle_outcome)} "
                        f"preview={repr(cycle_outcome)[:200]}")
        # 诊断日志：push_history 状态（重启沙盘后可见）
        if len(self.push_history) > 0 or rejection_text or effective_text:
            log_message("engine.feedback",
                        f"push_history records={len(self.push_history)} "
                        f"rejection={repr(rejection_text)[:120]} "
                        f"effective={repr(effective_text)[:120]}")

        # 外部 mod 行为实证数据（mod 特定实现已省略）
        mod_evidence_text = ""

        user_prompt = ACTION_SELECTOR_USER.format(
            pov_name=pov_name,
            inner_voice_text=inner_voice,
            recent_actions=format_recent_actions(self._cycle_history),
            game_time_context=game_time_context,
            pov_buff_context=pov_buff_context,
            scene_objects=scene_objects,
            action_catalog=catalog_text,
            rejection_awareness=rejection_text,
            effective_approach=effective_text,
            last_cycle_outcome=cycle_outcome,
            mod_evidence=mod_evidence_text,
        )

        try:
            result = call_ai(AICallParams(
                provider=settings["provider"],
                api_key=settings["api_key"],
                model=settings["model"],
                prompt=user_prompt,
                system_prompt=ACTION_SELECTOR_SYSTEM.format(
                    player_rules=f"\n【玩家自定义规则】\n{player_rules}\n" if player_rules.strip() else ""),
                custom_api_url=settings.get("custom_api_url", ""),
                timeout=120,
                cancel_event=self._cancel_event,
                thinking_disabled=False,
            ))
        except Exception:
            log_exception("engine.action_selector", "AI call failed")
            self._last_action_error = "ai_call_failed: AI API调用异常"
            return []

        if not result:
            log_message("engine.action_selector", "AI returned empty result")
            self._last_action_error = "ai_empty_response: AI返回空"
            return []

        # 诊断：记录原始响应（截断 500 字符）
        log_message("engine.action_selector.response",
                    f"raw={result[:500]}")

        # 解析 JSON 响应（Phase B 意图格式：{character_name, group, target_name}）
        parsed = _parse_action_response(result)
        if parsed is None:
            log_message("engine.action_selector", f"Failed to parse JSON: {result[:200]}")
            self._last_action_error = "parse_failed: 无法解析AI响应JSON"
            return []

        # Phase B 意图格式 → 如果包含 character_name/group 字段则走意图映射，
        # 否则回退旧 action_id 格式
        log_message("engine.action_selector",
                    f"catalog={len(catalog)} parsed={len(parsed)} "
                    f"first_keys={list(parsed[0].keys())[:5] if parsed else 'empty'}")
        if parsed and "character_name" in parsed[0]:
            intent_lookup = build_intent_lookup(catalog)
            selected = _map_intents_to_actions(parsed, intent_lookup, catalog)
        else:
            selected = _map_selected_to_options(parsed, catalog)

        if not selected:
            if parsed and len(parsed) > 0:
                self._last_action_error = "mapping_failed: AI选了动作但映射失败（名字/分组不匹配目录）"
            else:
                self._last_action_error = "ai_chose_nothing: AI明确返回空动作列表"
        return selected


# ── 响应解析 ──

def _map_intents_to_actions(parsed: list[dict],
                            intent_lookup: dict,
                            catalog: list[ActionOption]) -> list[ActionOption]:
    """将 AI 意图输出（character_name, group, target_name）映射回 ActionOption。

    Phase B（2026-07-20）：AI 不再输出无意义的 action_id，而是输出
    「谁 + 做什么 + 对谁/什么」。通过 intent_lookup 反向查找。
    名字模糊匹配容错（AI 可能输出"斑"而非"宇智波 斑"）。
    """
    if not parsed:
        return []

    # 构建名字→完整名映射（处理 AI 可能用的短名）
    name_map: dict[str, str] = {}
    for a in catalog:
        if a.character_name and a.character_name not in name_map:
            name_map[a.character_name] = a.character_name
            # 也注册空格分隔的最后一段（"宇智波 斑" → "斑"）
            parts = a.character_name.rsplit(None, 1)
            if len(parts) == 2 and parts[1] not in name_map:
                name_map[parts[1]] = a.character_name
            # 注册去空格版（"宇智波 斑" → "宇智波斑"）——AI 常输无空格名
            nosp = a.character_name.replace(" ", "").replace("　", "")
            if nosp != a.character_name and nosp not in name_map:
                name_map[nosp] = a.character_name

    selected = []
    for item in parsed:
        cname = str(item.get("character_name", "") or "").strip()
        group = str(item.get("group", "") or "").strip()
        tname = str(item.get("target_name", "") or "").strip()
        reason = str(item.get("reason", "") or "")

        # 名字模糊匹配：AI 可能输出"宇智波真鳕"而非"宇智波 真鳕"
        cname_full = name_map.get(cname, cname)
        if cname_full == cname:
            nosp = cname.replace(" ", "").replace("　", "")
            cname_full = name_map.get(nosp, cname)

        # 也尝试 AI 输出的 target_name 去空格归一
        tname_nosp = tname.replace(" ", "").replace("　", "")

        # 查找——精确 key
        key = (cname_full, group, tname)
        action = intent_lookup.get(key)

        # 回退 1：target_name 去空格归一
        if action is None and tname_nosp != tname:
            for (cn, g, tn), a in intent_lookup.items():
                tn_nosp = tn.replace(" ", "").replace("　", "")
                if g == group and cn == cname_full and tn_nosp == tname_nosp:
                    action = a
                    break

        # 回退 2：子串模糊匹配 target_name（AI 可能输出部分物品名）
        if action is None:
            for (cn, g, tn), a in intent_lookup.items():
                if g == group and cn == cname_full:
                    if tname and tn and (tname in tn or tn in tname):
                        action = a
                        break

        # 诊断：记录不匹配的 key
        if action is None:
            related = [(cn, g, tn) for (cn, g, tn) in intent_lookup
                       if cn == cname_full and g == group]
            # enhanced diagnostics for related=[]
            if not related:
                sample_keys = [(cn, g, tn) for (cn, g, tn) in intent_lookup
                               if g == group][:5]
                log_message("engine.map_intents",
                            f"miss key={key} group='{group}' related=[] "
                            f"group_samples={sample_keys} "
                            f"name_map_keys={list(name_map.keys())[:10]}")
            else:
                log_message("engine.map_intents",
                            f"miss key={key} related={related[:5]} "
                            f"lookup_size={len(intent_lookup)}")
            # 回退 3：不区分名字全/简称再试（遍历所有 key）
            for (cn, g, tn), a in intent_lookup.items():
                if g == group and tn == tname:
                    cn_short = cn.rsplit(None, 1)[-1] if " " in cn else cn
                    if cn_short == cname or cn == cname_full:
                        action = a
                        break
                # 对于 need 组，也尝试 target_name 去空格子串匹配
                if g == group and not action:
                    tn_nosp = tn.replace(" ", "").replace("　", "")
                    if tn_nosp == tname_nosp:
                        cn_short = cn.rsplit(None, 1)[-1] if " " in cn else cn
                        if cn_short == cname or cn == cname_full:
                            action = a
        # 回退 4：跨 group 兜底（2026-07-26）——AI 经常把 use_object
        # 标成 need / social 等错误 group。当 group 约束的匹配全失败后，
        # 忽略 group，仅按 character_name + target_name 在所有 group 中搜索。
        if action is None:
            # 4a: 精确 target_name 跨 group 匹配
            for (cn, g, tn), a in intent_lookup.items():
                if cn == cname_full and tn == tname:
                    action = a
                    log_message("engine.map_intents",
                                f"cross_group match ({cname_full}, {g}, {tname}) "
                                f"via exact tname (AI said group={group})")
                    break
        if action is None:
            # 4b: 去空格 target_name 跨 group 匹配
            for (cn, g, tn), a in intent_lookup.items():
                if cn == cname_full:
                    tn_nosp = tn.replace(" ", "").replace("　", "")
                    if tn_nosp == tname_nosp:
                        action = a
                        log_message("engine.map_intents",
                                    f"cross_group match ({cname_full}, {g}, {tn}) "
                                    f"via despaced tname (AI said group={group})")
                        break
        if action is None:
            # 4c: 子串 target_name 跨 group 匹配
            for (cn, g, tn), a in intent_lookup.items():
                if cn == cname_full and tname and tn and (tname in tn or tn in tname):
                    action = a
                    log_message("engine.map_intents",
                                f"cross_group match ({cname_full}, {g}, {tn}) "
                                f"via substring tname (AI said group={group})")
                    break
        if action is not None:
            selected.append(dataclasses.replace(action, reason=reason))
        else:
            log_message("engine.map_intents",
                        f"No match for ({cname_full}, {group}, {tname})")
    if selected:
        log_message("engine.map_intents",
                    f"mapped {len(selected)} actions: "
                    f"{[(a.character_name, a.group, a.target_name) for a in selected]}")
    return selected


def _map_selected_to_options(parsed: list[dict], catalog: list[ActionOption]) -> list[ActionOption]:
    """将 AI 选中的 action_id 映射回 ActionOption（旧格式回退）。

    用 dataclasses.replace 整体复制——手工逐字段重建会在 ActionOption 加新字段
    （tone/affordance_hints/prompt_label）时静默剥离数据（「新建 dict 硬编码
    空值覆盖上游注入」血案的 dataclass 变体）。

    Args:
        parsed: AI 响应解析出的 [{action_id, reason, ...}]
        catalog: 本轮完整动作目录

    Returns:
        选中的 ActionOption 列表（未知 action_id 静默跳过）
    """
    action_map = {a.action_id: a for a in catalog}
    selected = []
    for item in parsed:
        aid = item.get("action_id", "")
        if aid in action_map:
            selected.append(dataclasses.replace(
                action_map[aid], reason=item.get("reason", "")))
    return selected

def _ensure_proximity(selected: list[ActionOption],
                     ws: WorldState) -> list[ActionOption]:
    """AI 选了 push+sim 但目标在不同房间时，自动 prepend goto。

    walk-to-sim 已从目录移除——移动是实现细节，AI 不应手动选。
    在 AI 选择后、命令发射前调用。构造的 goto 不进 catalog、
    不占去重配额（去重只管 AI 选的动作）。

    保守策略：任一角色缺少 room_id 也 prepend（宁可多走不少走）。
    """
    if not selected:
        return selected
    result: list[ActionOption] = []
    for action in selected:
        if (action.action_type == "push" and action.target_kind == "sim"
                and action.target_id):
            actor = ws.get_character(action.character_id)
            target = ws.get_character(action.target_id)
            if actor and target:
                same_room = (actor.room_id and target.room_id
                             and actor.room_id == target.room_id)
                if not same_room:
                    result.append(ActionOption(
                        action_id=f"walkto_{action.character_id}_{action.target_id}",
                        character_id=action.character_id,
                        character_name=action.character_name,
                        action_type="goto",
                        target_kind="sim",
                        description=f"{action.character_name} 走到 {action.target_name} 身边",
                        target_id=action.target_id,
                        target_name=action.target_name,
                        affordance_hints=["terrain-gohere"],
                    ))
        result.append(action)
    return result


def _ensure_object_proximity(selected: list[ActionOption],
                             ws: WorldState) -> list[ActionOption]:
    """AI 选了 push+object 但 sim 在物品不同房间时，自动 prepend goto。

    与 _ensure_proximity（社交版）对称——物品版用 observer 实证数据驱动：
    只有当 observer 记录显示此动作通常先 gohere（≥60% 前置是 gohere），
    且 sim 不在目标物品所在房间时，才在 push 前自动插入 goto。

    构造的 goto 不进 catalog、不占去重配额。
    保守策略：sim 或物品缺少位置信息时不做推断（宁可少走不乱走）。
    """
    if not selected:
        return selected
    try:
        from .autonomous_observer import observer
    except Exception:
        return selected

    result: list[ActionOption] = []
    for action in selected:
        if (action.action_type == "push" and action.target_kind == "object"
                and action.target_id and action.affordance_hints):
            # 以第一个 hint 为准（hand-written hints 的第一个是规则作者的精确意图；
            # 快车 hints 的第一个也是最重要的 affordance）
            main_hint = action.affordance_hints[0]
            if observer.needs_gohere_before(main_hint):
                actor = ws.get_character(action.character_id)
                target_obj = _find_scene_object(ws, action.target_id)
                if actor and target_obj and actor.room_id and target_obj.room_id:
                    same_room = (actor.room_id == target_obj.room_id)
                    if not same_room:
                        obj_label = target_obj.name or target_obj.type or action.target_name
                        result.append(ActionOption(
                            action_id=f"approach_{action.character_id}_{action.target_id}",
                            character_id=action.character_id,
                            character_name=action.character_name,
                            action_type="goto",
                            target_kind="object",
                            description=f"{action.character_name} 走到{obj_label}旁",
                            target_id=action.target_id,
                            target_name=obj_label,
                            affordance_hints=["terrain-gohere"],
                        ))
        result.append(action)
    return result


def _find_scene_object(ws: WorldState, object_id: str):
    """在 WorldState 的场景物品列表中按 object_id 查找。"""
    if not ws or not ws.scene:
        return None
    for obj in ws.scene.objects:
        if obj.object_id == object_id:
            return obj
    return None


def _parse_action_response(response_text: str) -> Optional[list[dict]]:
    """从 AI 响应中解析选中的动作 JSON。

    支持两种格式：
    1. ```json {...} ``` 代码块包裹
    2. 裸 JSON 文本

    Returns:
        list[dict] 或 None（解析失败时）
    """
    if not response_text:
        return None

    text = response_text.strip()

    # 尝试提取 ```json ... ``` 代码块
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # 尝试解析 JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试找第一个 { 到最后一个 }
        m2 = re.search(r'\{.*\}', text, re.DOTALL)
        if m2:
            try:
                data = json.loads(m2.group(0))
            except json.JSONDecodeError:
                return None
        else:
            return None

    if isinstance(data, dict):
        actions = data.get("selected_actions", [])
        if isinstance(actions, list):
            return actions
    elif isinstance(data, list):
        # AI 输出变体：不带 {"selected_actions": ...} 包裹的裸动作数组
        return data

    return None


@dataclass
class EngineResult:
    """一轮决策循环的结果。"""
    success: bool = False
    pov_name: str = ""             # POV 角色名
    inner_voice: str = ""
    inner_voice_prompt: str = ""   # 🆕 完整内心声音 prompt（含 persona），调试用
    selected_actions: list = field(default_factory=list)
    auto_actions: list = field(default_factory=list)  # 自动触发动作（不走 AI）
    motive_actions: list = field(default_factory=list)  # 需求紧急自动动作（2026-07-28）
    catalog: list = field(default_factory=list)
    error: str = ""
    no_action_reason: str = ""     # 无动作时的具体原因（空=有动作）
