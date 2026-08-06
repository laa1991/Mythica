# mythica_action.py — 动作命令接收与执行（通用执行器）
"""桌面端 → 游戏端 动作命令接收。

通过信号文件轮询读取桌面端沙盘发出的动作指令，
解析后在游戏内执行（调用 Sims 4 API 驱动 sim 行为）。

2026-07-17 通用执行器改造（_av=2）：
- 动作语义全部由桌面端 payload 表达（action_type + target_kind + affordance_hints），
  游戏端只按 3 种执行原语解析执行：push（affordance 推送）/ goto（地形移动）/ idle。
  新增动作类型只改桌面端，游戏端零改动。
- 每个动作执行后经 HTTP POST /action_result 回传结果（mythica_network.send_action_result）。

复用现有模式：与 mythica_dialogue.py 的 `_check_manual_dialogue_result_quick` 完全一致。
"""
import os
import json
import time

from mythica_config import get_output_directory, log_error
from mythica_interact import classify_tuning_source
from mythica_network import send_action_result
from mythica_records import (append_to_file_with_rotation, get_log_time,
                             get_action_command_signal_path,
                             get_action_command_json_path)
import services


# 指令保鲜期（秒）。桌面端 created_at 到消费时刻超过此值 → 整包丢弃不执行。
# 血案（2026-07-18）：游戏 12:24 退出后沙盘 12:47 照常发射，Action_Command.json
# 落盘无人消费——下次开游戏加载存档会立刻执行这条语境早已失效的旧指令。
# 5 分钟：容忍游戏暂停/加载中的正常延迟，挡住"隔天开游戏执行昨天指令"。
_ACTION_COMMAND_TTL_SECONDS = 300


def _command_age_seconds(created_at):
    """解析桌面端 created_at（"%Y-%m-%d %H:%M:%S"）→ 距今秒数。

    同机通信（localhost）无时区问题。解析失败返回 None（视为新鲜，
    后向兼容缺字段的旧 payload——宁可执行不可误丢）。
    """
    if not created_at:
        return None
    try:
        created = time.mktime(time.strptime(str(created_at), "%Y-%m-%d %H:%M:%S"))
        return max(0.0, time.time() - created)
    except (ValueError, OverflowError):
        return None


# 加载画面竞态标记：sims 未实例化时暂缓消费的一次性日志（防 2s 轮询刷屏）
_not_ready_logged = False


def _sims_ready():
    """场上是否已有实例化的 sim——加载画面/切场景期间为 False。

    四测血案（2026-07-18 19:22:44）：游戏进存档瞬间消费了加载期间沙盘发的
    指令，sim 尚未实例化 → not_found actor 白白丢弃。未就绪时暂缓消费
    （信号文件留在盘上，TTL 仍然兜底陈旧指令）。
    任何异常视为未就绪（保守：宁可晚消费不可误丢）。
    """
    try:
        for _ in services.sim_info_manager().instanced_sims_gen():
            return True
        return False
    except Exception:
        return False


# ── 命令轮询 ──

def _check_action_commands_quick():
    """轮询 Action_Command 信号文件，消费并执行动作命令。

    在 _new_trigger_start 中 piggyback 调用，频率 = 游戏交互频率。
    过期指令（created_at 超 TTL）整包丢弃：落盘 + 游戏内通知 + 逐动作回传
    error 结果——桌面端若在跑能看到"为什么没执行"。
    加载画面（sims 未实例化）暂缓消费——文件留盘，下轮再试。
    """
    global _not_ready_logged
    signal_path = get_action_command_signal_path()
    json_path = get_action_command_json_path()

    if not (os.path.exists(signal_path) and os.path.exists(json_path)):
        return

    # 加载画面竞态守卫：sims 未就绪时不消费（TTL 兜底陈旧指令）
    if not _sims_ready():
        if not _not_ready_logged:
            _not_ready_logged = True
            log_error("mythica_action._check_action_commands_quick",
                      "sims not instanced yet — command consumption deferred")
        return
    _not_ready_logged = False

    # 读取命令
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        log_error("mythica_action._check_action_commands_quick",
                  "Action_Command.json 解析失败，丢弃: {}".format(str(e)[:120]))
        _cleanup_signal_files(signal_path, json_path)
        return

    # 提取数据（在删文件之前完成）
    actions = payload.get("actions", []) or []
    inner_voice = payload.get("inner_voice", "")

    # 安全清除信号文件
    _cleanup_signal_files(signal_path, json_path)

    if not actions:
        return

    # TTL 检查：陈旧指令（游戏关闭期间桌面发的/上次会话残留）不执行
    age = _command_age_seconds(payload.get("created_at", ""))
    if age is not None and age > _ACTION_COMMAND_TTL_SECONDS:
        log_error("mythica_action._check_action_commands_quick",
                  "stale command dropped: age={}s > ttl={}s, {} action(s), created_at={}".format(
                      int(age), _ACTION_COMMAND_TTL_SECONDS, len(actions),
                      payload.get("created_at", "?")))
        _show_action_notification(
            u"沙盘指令", u"已过期 {} 分钟，未执行（{} 个动作）".format(
                int(age // 60), len(actions)))
        for action in actions:
            result = _build_result_base(action, action.get("target_kind", ""))
            result["detail"] = "stale command dropped (age {}s > ttl {}s)".format(
                int(age), _ACTION_COMMAND_TTL_SECONDS)
            _send_action_result(result)
        return

    # 逐个执行动作
    for action in actions:
        try:
            _execute_one_action(action)
        except Exception:
            log_error("mythica_action._check_action_commands_quick",
                      "_execute_one_action failed for {}".format(action.get("action_id", "?")))

    # 记录日志
    if inner_voice:
        ts = get_log_time()
        _write_action_log(ts, inner_voice, actions)


def _cleanup_signal_files(signal_path, json_path):
    """清理信号文件对。"""
    for path in (signal_path, json_path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            # 文件可能已被其他进程删除，忽略清理失败
            pass


# ── 清队原语（stop 动作 / clear_queue 标志共用，2026-07-18）──
# 背景：push 只是入队——sim 忙碌时新指令排在后面越堆越多，用户实测"有时要把
# 前面的动作停掉才轮得到新指令"。API 面经 types_index.txt 图书馆查证：
# sim.queue.cancel_all()（排队中）+ si_state.all_si_gen() → si.cancel()（执行中）。

def _clear_sim_interactions(sim_obj):
    """清除 sim 的交互队列 + 取消可取消的正在执行交互（插队原语）。

    Returns:
        int: 实际发出取消的交互数（排队的 cancel_all 按可见长度计）。
        任何一步失败只落盘不抛——清不干净时游戏自己的排队规则仍是兜底。
    """
    cleared = 0
    # 1) 排队中的交互：InteractionQueue.cancel_all()
    try:
        queue = getattr(sim_obj, "queue", None)
        if queue is not None:
            try:
                cleared += int(queue.visible_len() or 0)
            except Exception:
                # 计数失败不影响清除本身
                pass
            queue.cancel_all()
    except Exception as e:
        log_error("mythica_action._clear_sim_interactions",
                  "queue.cancel_all failed: {}".format(str(e)[:100]))

    # 2) 正在执行的 super interactions：逐个 USER_CANCEL（不可取消的由游戏拒绝）
    try:
        from interactions.interaction_finisher import FinishingType
        si_state = getattr(sim_obj, "si_state", None)
        all_si_gen = getattr(si_state, "all_si_gen", None)
        if callable(all_si_gen):
            for si in list(all_si_gen()):
                try:
                    si.cancel(FinishingType.USER_CANCEL,
                              cancel_reason_msg="mythica sandbox clear")
                    cleared += 1
                except Exception:
                    # 单个 SI 取消失败（guaranteed/不可取消）——游戏是最终裁判，继续下一个
                    continue
    except Exception as e:
        log_error("mythica_action._clear_sim_interactions",
                  "si_state cancel failed: {}".format(str(e)[:100]))
    return cleared


# ── 动作执行（统一分发）──

def _execute_one_action(action):
    """执行单个动作命令（经模块尾部 _ACTION_ROUTES 路由表分发）。

    通用执行器（2026-07-17，_av=2）：路由表按执行原语组织——
    - push / goto: 通用类型，payload 必须显式带 target_kind（"sim"|"object"）
    - interact / interact_object / walk / walk_obj: legacy 别名（路由行默认 kind）
    - idle: 提前 return，不进表
    目标解析统一在此完成："sim" → _resolve_sim_info + _get_sim_instance；
    "object" → _find_object_by_id。handler 返回结果字段 dict（status/attempts...），
    与公共字段合并后经 _send_action_result 回传桌面端（idle 无事可报不回传；
    未知类型也回传 error——版本偏斜时两端都可见）。

    Args:
        action: dict with keys: action_id, character_id, character_name,
                action_type, target_kind (通用类型必填), target_id, target_name,
                description, affordance_hints (可选，精确交互类名列表，优先级序)
    """
    action_type = action.get("action_type", "")
    character_name = action.get("character_name", "")

    if action_type == "idle":
        return

    if action_type == "stop":
        # 清队动作（2026-07-18）：只清不推、无目标——不进路由表（与 idle 同层特判）。
        # 用户实测：指令在 sim 队列里越堆越多，需要"停掉手头的事"才轮到新指令。
        result = _build_result_base(action, "")
        sim_info = _resolve_sim_info(action.get("character_id", ""))
        sim_obj = _get_sim_instance(sim_info) if sim_info is not None else None
        if sim_obj is None:
            log_error("mythica_action._execute_one_action",
                      "stop: sim not found: {}".format(character_name))
            result["status"] = "not_found"
            result["detail"] = "actor"
        else:
            cleared = _clear_sim_interactions(sim_obj)
            result["status"] = "cleared"
            result["detail"] = "cleared {} interaction(s)".format(cleared)
            _show_action_notification(
                character_name, action.get("description", "") or u"停止当前动作")
            log_error("mythica_action._execute_one_action",
                      "stop: cleared {} interactions for {}".format(cleared, character_name))
        _send_action_result(result)
        return

    route = _ACTION_ROUTES.get(action_type)
    if route is None:
        # 未知类型：新桌面+旧 mod 版本偏斜时也落在这里，干净可读
        log_error("mythica_action._execute_one_action",
                  "unknown action_type: {}".format(action_type))
        result = _build_result_base(action, "")
        result["detail"] = "unknown action_type: {}".format(action_type)
        _send_action_result(result)
        return

    kind_default, handler = route
    target_kind = action.get("target_kind", "") or kind_default or ""
    result = _build_result_base(action, target_kind)
    if target_kind not in ("sim", "object", "inventory_item", "self"):
        # 通用类型（push/goto）缺 target_kind——协议错误，回传便于桌面端定位
        log_error("mythica_action._execute_one_action",
                  "missing/invalid target_kind for {}: {}".format(action_type, target_kind))
        result["detail"] = "missing/invalid target_kind"
        _send_action_result(result)
        return

    # 获取 actor sim 对象
    sim_info = _resolve_sim_info(action.get("character_id", ""))
    sim_obj = _get_sim_instance(sim_info) if sim_info is not None else None
    if sim_obj is None:
        log_error("mythica_action._execute_one_action",
                  "sim not found: {}".format(character_name))
        result["status"] = "not_found"
        result["detail"] = "actor"
        _send_action_result(result)
        return

    # 可选插队（2026-07-18）：clear_queue 标志——推之前清空现有交互。
    # 桌面端决定语义（手动面板"插队"开关）；结果附 cleared_before 计数。
    if action.get("clear_queue"):
        result["cleared_before"] = _clear_sim_interactions(sim_obj)

    # 按 target_kind 统一解析目标实体
    target_id_str = action.get("target_id", "")
    if target_kind == "sim":
        target_info = _resolve_sim_info(target_id_str)
        target_entity = _get_sim_instance(target_info) if target_info is not None else None
    elif target_kind == "self":
        # 自我动作（tgt=ACTOR）：sim 自己就是目标
        target_entity = sim_obj
    elif target_kind == "inventory_item":
        target_entity = _find_inventory_item(sim_obj,
                                             action.get("target_name", ""),
                                             action.get("target_def", ""))
    else:
        target_entity = _find_object_by_id(target_id_str)
    if target_entity is None:
        log_error("mythica_action._execute_one_action",
                  "target not found ({}): {}".format(
                      target_kind, action.get("target_name", "")))
        _show_action_notification(character_name, action.get("description", ""))
        result["status"] = "not_found"
        result["detail"] = "target"
        _send_action_result(result)
        return

    outcome = handler(sim_obj, target_entity, target_kind, action)
    if outcome:
        result.update(outcome)
    # 附加 actor 执行状态（2026-07-26 _rv=2）：沙盘据此判断"sim 正在做什么、我们的动作排第几"
    result.update(_collect_actor_state(sim_obj))
    _send_action_result(result)


def _get_sim_instance(sim_info):
    """从 sim_info 获取 sim 游戏对象（实例化的 Sim）。"""
    try:
        if hasattr(sim_info, "get_sim_instance"):
            sim_obj = sim_info.get_sim_instance()
            if sim_obj is not None:
                return sim_obj
    except Exception:
        # get_sim_instance 可能因 sim 未实例化而失败，回退到 instanced_sims 遍历
        pass
    # 回退：从 instanced_sims 中按 sim_id 匹配。
    # 注意 instanced_sims_gen 产出的可能已是 Sim 实例（有 position）——直接返回；
    # SimInfo 变体才走 get_sim_instance（2026-07-16 pick 探针三跑全空的教训）
    try:
        sid = int(getattr(sim_info, "sim_id", 0) or 0)
        if sid:
            for si in services.sim_info_manager().instanced_sims_gen():
                try:
                    if getattr(si, "sim_id", None) != sid:
                        continue
                    if hasattr(si, "position"):
                        return si
                    if hasattr(si, "get_sim_instance"):
                        return si.get_sim_instance()
                except Exception:
                    # 单个 instanced_sim 不可迭代/无 sim_id——继续检查下一个
                    pass
    except Exception:
        # instanced_sims_gen 可能在加载画面返回空迭代器——回退到 None
        pass
    return None


def _find_affordance_by_name(sim_obj, name_hint):
    """在 sim._super_affordances 中按名称搜索交互 affordance。

    匹配优先级：精确 > 前缀 > 子串；同级先 EA 后 MOD，再取名字最短者。
    （2026-07-16 两轮实测教训：'chat' 命中 sim_ProtestChat、'friendly' 命中
    utopya 舞蹈 mod——风味变体和 mod 交互都不该赢过原版。）
    找不到返回 None。
    """
    name_lower = (name_hint or "").lower()
    if not name_lower:
        return None

    best_key = None   # (tier, mod_rank, len)  tier: 1 前缀 / 2 子串; mod_rank: 0 EA / 1 其他
    best_aff = None
    sa = getattr(sim_obj, "_super_affordances", None) or ()
    for aff in sa:
        aff_name = getattr(aff, "__name__", "") or ""
        if not aff_name:
            continue
        aff_lower = aff_name.lower()
        # 调试/作弊交互不推给 sim（2026-07-16 实测 "friendly" 曾命中
        # debug_SocialTestBasedScore_Friendly）
        if aff_lower.startswith("debug_") or aff_lower.startswith("cheat_"):
            continue
        # 精确匹配直接返回
        if name_lower == aff_lower:
            return aff
        if aff_lower.startswith(name_lower):
            tier = 1
        elif name_lower in aff_lower:
            tier = 2
        else:
            continue
        src = classify_tuning_source(aff_name, getattr(aff, "guid64", None))
        key = (tier, 0 if src == 'EA' else 1, len(aff_name))
        if best_key is None or key < best_key:
            best_key = key
            best_aff = aff
    return best_aff


# 社交候选名单（按优先级）。2026-07-16 用 affordance 图书馆（affordances_index.txt）
# 复核选定——全部 cat=sim_BeFriendly 或全年龄 super 社交，避免"求建议(限儿童)/
# 幼儿说话(限幼儿)/秀视频(限青少年)"这类语义反向或年龄受限的坑。
# 游戏仍会按冷却 buff/忙碌拒绝，push 循环自动落到下一候选。尾段泛化子串兜底。
_INTERACT_CANDIDATE_HINTS = (
    "sim_Chat",                        # 通用聊天（EA guid=13998，auto=N user=N 系统推送型——
                                       # 不在 _super_affordances，走全局取类；queue diff 实证游戏在推）
    "sim_HighFive_QuickSocial",        # 击掌（sim_BeFriendly，全年龄 TEEN+）
    "sim_Hug_QuickSocial",             # 拥抱（sim_BeFriendly，覆盖幼儿）
    "sim_HipBump_QuickSocial",         # 顶胯（sim_BeFriendly，全年龄）
    "sim_BroHug_QuickSocial",          # 兄弟抱（sim_BeFriendly，全年龄）
    "social_RockPaperScissors",        # 猜拳（全年龄含 CHILD，user 可选）
    "friendly",                        # 泛化兜底
    "social",
    "chat",
)


# 全局 affordance 取类缓存：{名字小写: class 或 None}。异常时不缓存（下次重试）。
_global_aff_cache = {}


def _find_affordance_global(name_hint):
    """从 affordance_manager.types 按精确名取交互类。

    背景（2026-07-16 queue diff 实证）：sim_Chat 等 auto=N user=N 的系统推送型
    交互不在 sim._super_affordances 静态列表，但游戏运行中确实在推——全局按名
    取类后 push 同样可行。53k 条线性扫仅每名字首次，命中/未命中均缓存。

    2026-08-01 修复：MOD affordance 的 hint 名可能带命名空间前缀，
    而 cls.__name__ 可能不含前缀。增加降级匹配：
    ① 全名精确 ② 去命名空间前缀（最后一个冒号之后的部分）。
    """
    key = (name_hint or "").lower()
    if not key:
        return None
    if key in _global_aff_cache:
        return _global_aff_cache[key]
    found = None
    try:
        types_dict = getattr(services.affordance_manager(), "types", None) or {}
        # 去前缀版本：去掉命名空间前缀（如 ModAuthor:InteractionName → interactionname）
        bare_key = key.rsplit(":", 1)[-1] if ":" in key else ""
        for cls in types_dict.values():
            cls_name = (getattr(cls, "__name__", "") or "").lower()
            if not cls_name:
                continue
            if cls_name == key:
                found = cls
                break
            if bare_key and cls_name == bare_key:
                found = cls
                # 不 break——继续扫完看有没有精确匹配的（前缀+名字）
                # 优先精确，当前只是候补
        # 如果没有精确匹配，用最后扫描到的 bare_key 匹配
        if found is None and bare_key:
            for cls in types_dict.values():
                cls_name = (getattr(cls, "__name__", "") or "").lower()
                if cls_name == bare_key:
                    found = cls
                    break
    except Exception:
        # affordance_manager 在加载画面可能不可用——不缓存，下次重试
        return None
    _global_aff_cache[key] = found
    if found is not None and bare_key and key != bare_key:
        # 同时缓存 bare_key，后续直接用
        _global_aff_cache[bare_key] = found
    return found


def _collect_interact_candidates(sim_obj, hints=_INTERACT_CANDIDATE_HINTS):
    """按 hints 优先级收集社交 affordance 候选列表（去重，保持顺序）。

    2026-07-28 修复：含下划线的 hint 是精确 affordance 名（如 Collect_Trash），
    优先从 affordance_manager 全局精确取类——防止 _find_affordance_by_name 的
    前缀匹配在对象上命中错误变体（如 collect_Trash_Aggregate 偷走 Collect_Trash）。

    不含下划线的 hint（如 "chat", "friendly"）保持旧行为：仅查对象
    _super_affordances 分级匹配，不走全局防误命中。
    """
    candidates = []
    seen = set()
    for hint in hints:
        aff = None
        if "_" in hint:
            # 精确 affordance 名：全局精确优先，回退对象模糊
            aff = _find_affordance_global(hint)
            if aff is None:
                aff = _find_affordance_by_name(sim_obj, hint)
        else:
            # 泛化子串：仅查对象分级匹配
            aff = _find_affordance_by_name(sim_obj, hint)
        if aff is not None and id(aff) not in seen:
            seen.add(id(aff))
            candidates.append(aff)
    # 诊断日志：显示哪些 hint 找到、哪些没找到
    if hints:
        found_names = [getattr(a, "__name__", str(a)) for a in candidates]
        missing = [h for h in hints if not any(
            getattr(a, "__name__", "").lower() == h.lower() for a in candidates)]
        if missing:
            log_error("mythica_action._collect_interact_candidates",
                      "candidates: found={} missing={}".format(
                          found_names, missing))
    return candidates


# ── object 目标辅助 ──


def _find_object_by_id(object_id_str):
    """在 zone 中按 id 查找游戏对象。

    Args:
        object_id_str: 对象 ID 字符串（来自场景快照的 object_id 字段）

    Returns:
        game object 或 None
    """
    if not object_id_str:
        return None
    try:
        zone = services.current_zone()
        obj_mgr = getattr(zone, 'object_manager', None)
        if obj_mgr is None:
            return None
        get_all = getattr(obj_mgr, 'get_all', None)
        if not callable(get_all):
            return None
        for obj in get_all():
            try:
                oid = str(getattr(obj, 'id', getattr(obj, 'guid64', '')))
                if oid == str(object_id_str):
                    return obj
            except Exception:
                continue
    except Exception:
        # zone/object_manager 在加载画面或切换场景时可能不可用
        pass
    return None


# 物品交互候选排除模式（小写子串/前缀）。2026-07-16 首测教训：盲选第一个
# affordance 推出了 object_ReplaceBrokenObject（物品没坏被拒）和 sim-stand
# （站立，无意义"成功"）。条件性/维修类/系统类交互全部排除，游戏对剩余
# 候选仍会做 state check——重试循环自动落到下一个。
# 2026-07-18 实测追加：createtray（fridge_CreateTray 需 crafting_process 参数，
# 直推必失败白耗一轮）；turnoff/turn_off（"使用电视"盲选到关机——电视开着时
# 会变成把它关掉，语义反向）。
# 2026-07-18 第二轮实测追加：picker（lockHouseholdSimsPicker... 弹选择器 UI）、
# possess（幽灵附身，SituationJob 拒但白耗）、padlock/setfrontdoor/doorbell
# （门类管理交互——"设为前门/解锁鸡舍挂锁"实际被推了出去，全无叙事意义）。
_OBJECT_AFF_SKIP_PATTERNS = (
    'si_', 'superaff', 'mixer', 'debug_', 'cheat_',
    'replacebroken', 'repair', 'salvage',                 # 维修类：需 Broken 状态
    'sim-stand', 'stand_passive', 'sit_passive',          # 姿态类：无意义
    'autonom', 'proxy', 'invisible',                      # 系统类
    'createtray',                                         # 需 crafting 参数，直推必失败
    'turnoff', 'turn_off',                                # 关闭类：语义反向
    'picker',                                             # 弹选择器 UI 的管理交互
    'possess',                                            # 幽灵附身（条件苛刻白耗）
    'padlock', 'setfrontdoor', 'doorbell',                # 门类管理交互
    'locked', 'unlock', 'frontdesk',                      # 条件性/前台职员类（2026-07-18 三测：
                                                          # 电脑前 4 候选全被这类占满 → 100% 全拒）
    'locking',                                            # 四测漏网：computer_Locking_LockForEveryone
                                                          # 被推 3 次。⚠️ 不用 'lock_'/裸 'lock'——
                                                          # 'clock_WindUp' 含 'lock_'，误杀钟表类
)


# 盲选候选收集上限——与 _try_push_candidates 重试 cap 对齐。
# 2026-07-18 三测教训：cap=4 时电脑的候选被条件性交互占满，
# 真正的 Browse_Web/PlayGame 排在后面永远够不着。
_OBJECT_CANDIDATE_LIMIT = 8


def _collect_object_candidates(target_obj, limit=_OBJECT_CANDIDATE_LIMIT):
    """从物品 _super_affordances 收集可推候选（过滤系统/维修/姿态类，保持顺序）。"""
    candidates = []
    for aff in (getattr(target_obj, '_super_affordances', None) or ()):
        aff_name = getattr(aff, '__name__', '') or ''
        nl = aff_name.lower()
        if not aff_name or any(p in nl for p in _OBJECT_AFF_SKIP_PATTERNS):
            continue
        candidates.append(aff)
        if len(candidates) >= limit:
            break
    return candidates


def _collect_hinted_object_candidates(target_obj, hints):
    """按 hints 优先级在物品上收集可推候选（白名单语义，去重保序）。

    委托 _collect_interact_candidates——同一分级匹配（_find_affordance_by_name
    只读 _super_affordances 属性，物品对象同样适用）+ 含下划线精确名回退
    affordance_manager 全局取类。独立函数名保语义清晰。

    注意：**不做 _OBJECT_AFF_SKIP_PATTERNS 过滤**——hints 是桌面端白名单，
    语义与盲选排除相反（bed_Autonomous_* 含 'autonom' 会被盲选过滤器误杀）。
    """
    return _collect_interact_candidates(target_obj, hints=tuple(hints))


# ── push 原语（interact / interact_object 的统一实现）──


def _push_generic_action(sim_obj, target_entity, target_kind, action):
    """通用 affordance 推送原语。

    候选来源三路分发：
    - hints 非空 + sim 目标：actor 的 _super_affordances 分级匹配 + 全局精确取类
    - hints 非空 + object 目标：物品白名单收集（绕过盲选过滤器）
    - hints 空：sim → 默认友好名单 _INTERACT_CANDIDATE_HINTS；object → 盲选过滤路径
    hints 全拒/全未命中不降级——意图保真：想亲吻绝不能变成击掌、"拿吃的"绝不能
    变成洗冰箱，仅通知+落盘+回传 no_affordance。

    Returns:
        结果字段 dict（status / pushed_affordance / affordance_src / attempts）
    """
    character_name = action.get("character_name", "")
    description = action.get("description", "")
    target_name = action.get("target_name", "")
    hints = action.get("affordance_hints") or []

    if hints:
        if target_kind == "object":
            candidates = _collect_hinted_object_candidates(target_entity, hints)
        else:
            candidates = _collect_interact_candidates(sim_obj, hints=tuple(hints))
        if not candidates:
            # 记录哪些 hints 没命中，帮助诊断 WW social 动作候选收集问题
            log_error("mythica_action._push_generic_action",
                      "hints unresolved: target={} hints={}".format(
                          target_name, hints))
            _show_action_notification(character_name, description)
            log_error("mythica_action._push_generic_action",
                      "hints unresolved on {} (hints={}) — 意图保真不降级".format(
                          target_name, hints))
            return {"status": "no_affordance"}
    elif target_kind == "object":
        candidates = _collect_object_candidates(target_entity)
        if not candidates:
            _show_action_notification(character_name, description)
            # 列出前几个被过滤的 affordance 名称以便调试
            sa = getattr(target_entity, '_super_affordances', None) or ()
            filtered_samples = [getattr(a, '__name__', str(a))[:30] for a in list(sa)[:3]]
            log_error("mythica_action._push_generic_action",
                      "no usable affordance on {} ({} total, filtered={})".format(
                          target_name, len(sa), filtered_samples))
            return {"status": "no_affordance"}
    else:
        candidates = _collect_interact_candidates(sim_obj)
        if not candidates:
            _show_action_notification(character_name, description)
            log_error("mythica_action._push_generic_action",
                      "no affordance found for: {}".format(description))
            return {"status": "no_affordance"}

    return _try_push_candidates(sim_obj, target_entity, candidates, action)


def _try_push_candidates(sim_obj, target_entity, candidates, action):
    """单份候选重试循环——逐候选 push_super_affordance，被拒自动落到下一个。

    2026-07-16 实测教训移植（原 interact/object 两份循环合并为一）：
    - 多候选可同时被拒（冷却 buff+年龄），cap 统一 8 保证兜底轮得到
    - 每候选记 attempts（含 EnqueueResult 可读拒因——最宝贵反馈，回传桌面端）
    - EA/MOD 来源标注："这次动作用了哪个 mod 的交互"日志可追责

    Returns:
        结果字段 dict：pushed（含 pushed_affordance/affordance_src）或
        all_rejected / error（posture context 不可用），均含 attempts
    """
    character_name = action.get("character_name", "")
    description = action.get("description", "")
    target_name = action.get("target_name", "")
    attempts = []

    # 2026-07-25 mixer 双推：
    # mixer affordance 需要父社交 SI 容器。
    # 先推 sim_Chat 建立 social context，再推目标——这样 mixer 找得到父 SI 承载。
    _candidate_names = [
        getattr(a, "__name__", "").lower() for a in candidates[:8]
    ]
    has_mixer = any(
        n.startswith("mixer_")
        for n in _candidate_names
    )
    social_parent_pushed = False
    if has_mixer:
        chat_aff = _find_affordance_global("sim_Chat")
        if chat_aff is not None:
            try:
                chat_ctx = sim_obj.create_posture_interaction_context()
                if chat_ctx is not None:
                    chat_result = sim_obj.push_super_affordance(
                        chat_aff, target_entity, context=chat_ctx)
                    social_parent_pushed = bool(chat_result)
                    if not social_parent_pushed and hasattr(chat_result, 'result'):
                        social_parent_pushed = bool(chat_result.result)
                    if not social_parent_pushed:
                        social_parent_pushed = 'guaranteed' in str(chat_result).lower()
                    if social_parent_pushed:
                        log_error("mythica_action._try_push_candidates",
                                  "mixer parent: pushed sim_Chat -> {}".format(target_name))
            except Exception:
                pass

    # 2026-07-16 实测：多候选可同时被拒（冷却 buff+年龄），cap 放宽到 8 保证泛化兜底轮得到
    for affordance in candidates[:8]:
        aff_name = getattr(affordance, "__name__", str(affordance))
        # 来源标注：EA 原版 / MOD——日志可追责"这次动作用了哪个 mod 的交互"
        src = classify_tuning_source(aff_name, getattr(affordance, "guid64", None))
        # 2026-08-01 诊断：记录 affordance target_type + test_globals 摘要——排查 MOD affordance 被拒根因
        _aff_tgt = "?"
        try:
            _tgt_attr = getattr(affordance, "target_type", None)
            if _tgt_attr is not None:
                _aff_tgt = str(getattr(_tgt_attr, "__name__", _tgt_attr))[:40]
        except Exception:
            pass
        _aff_tg = ""
        try:
            _tg = getattr(affordance, "test_globals", None)
            if _tg is not None:
                _aff_tg = str(_tg)[:200].replace("\n", " ")
        except Exception:
            pass
        _tgt_type = type(target_entity).__name__ if target_entity is not None else "None"
        log_error("mythica_action._try_push_candidates",
                  "attempting push: aff={} src={} aff_tgt={} real_tgt={} test_globals={}".format(
                      aff_name, src, _aff_tgt, _tgt_type, _aff_tg))
        # posture context 每次新建——不确定可否跨 push 复用，新建成本可忽略
        try:
            posture_ctx = sim_obj.create_posture_interaction_context()
        except Exception:
            # 加载画面/切场景时可能失败——统一走下方 None 检查
            posture_ctx = None
        if posture_ctx is None:
            _show_action_notification(character_name, description)
            log_error("mythica_action._try_push_candidates",
                      "failed to create posture context")
            return {"status": "error", "detail": "posture context unavailable",
                    "attempts": attempts}
        try:
            result = sim_obj.push_super_affordance(
                affordance, target_entity, context=posture_ctx)
        except Exception as e:
            log_error("mythica_action._try_push_candidates",
                      "push raised for {}: {}".format(aff_name, str(e)[:100]))
            attempts.append({"affordance": aff_name, "src": src, "ok": False,
                             "result": "raised: {}".format(str(e)[:200])})
            continue
        # push_super_affordance 在不同 API 版本返回不同：
        # - 旧版返回 bool（True=成功入队）
        # - 新版返回 EnqueueResult 对象，__bool__ 可能因 execute_result=None 返回 False
        #   但 .result 为 True（guaranteed 入队，只是尚未开始执行）
        # 2026-07-21 血案：stereo_TurnOnAndListen guaranteed 入队，但 if result: 判假 → 记成 rejected
        # 2026-07-22 修复：hasattr(result,'result') 在 C++ 对象上不暴露 → 改检 str 表示中的 "guaranteed"
        pushed = bool(result)
        if not pushed and hasattr(result, 'result'):
            pushed = bool(result.result)
        if not pushed:
            result_str = str(result)
            if 'guaranteed' in result_str.lower():
                pushed = True
        attempts.append({"affordance": aff_name, "src": src, "ok": pushed,
                         "result": str(result)[:200]})
        if pushed:
            log_error("mythica_action._try_push_candidates",
                      "pushed {} [{}] -> {}: result={}{}".format(
                          aff_name, src, target_name, result,
                          " (parent=sim_Chat)" if social_parent_pushed else ""))
            label = aff_name if src == 'EA' else "{} [MOD]".format(aff_name)
            _show_action_notification(
                character_name, "{} ({})".format(description, label))
            return {"status": "pushed", "pushed_affordance": aff_name,
                    "affordance_src": src,
                    "social_parent": social_parent_pushed,
                    "attempts": attempts}
        # 游戏拒绝该候选（年龄/忙碌/state check/占用等）——记录原因后尝试下一个
        log_error("mythica_action._try_push_candidates",
                  "rejected {} [{}] -> {}: result={}".format(
                      aff_name, src, target_name, result))

    _show_action_notification(character_name, description)
    log_error("mythica_action._try_push_candidates",
              "all candidates rejected for: {} (hints={})".format(
                  description, action.get("affordance_hints") or []))
    return {"status": "all_rejected", "attempts": attempts}


# ── goto 原语（walk / walk_obj 的统一实现）──
# go_here 不在 _super_affordances 静态列表里（它是 terrain 交互），
# 需要从 sim.super_affordances() 动态生成中找，只搜一次。
_sandbox_gohere_affordance = None
_sandbox_gohere_searched = False


def _get_gohere_affordance(sim_obj):
    """获取移动交互的 affordance class（带缓存，只搜一次）。

    优先 go_here/gohere，回退 terrain-jog（⚠️ 2026-07-18 勘误：jog 直推
    TerrainPoint 会被参与者约束拒绝——此回退仅聊胜于无，gohere 才是可用路径）。
    """
    global _sandbox_gohere_affordance, _sandbox_gohere_searched
    if _sandbox_gohere_searched:
        return _sandbox_gohere_affordance
    _sandbox_gohere_searched = True

    # 策略 1: sim.super_affordances() 动态生成
    try:
        sa_method = getattr(sim_obj, "super_affordances", None)
        if callable(sa_method):
            affs = list(sa_method() or [])
        else:
            affs = list(sa_method or []) if sa_method else []
    except Exception:
        affs = []

    # 优先级: go_here > gohere > terrain-jog（存在性回退）> 任何 terrain
    best = None
    for aff in affs:
        name = getattr(aff, '__name__', '') or ''
        nl = name.lower()
        if 'go_here' in nl or 'gohere' in nl:
            _sandbox_gohere_affordance = aff
            return aff
        if best is None and 'terrain-jog' == name:
            best = aff  # 存在性回退（jog 推 TerrainPoint 实测被拒，仅兜底聊胜于无）
    if best is not None:
        _sandbox_gohere_affordance = best
        return best

    # 策略 2: _super_affordances 静态兜底
    sa = getattr(sim_obj, "_super_affordances", None) or ()
    for aff in sa:
        name = getattr(aff, '__name__', '') or ''
        nl = name.lower()
        if 'go_here' in nl or 'gohere' in nl or 'terrain' in nl:
            _sandbox_gohere_affordance = aff
            return aff

    return None


def _resolve_goto_candidates(sim_obj, hints):
    """解析 goto 用的地形交互候选列表（优先级序，供逐候选重试）。

    hints 非空：逐个走 affordance_manager 全局精确取类（terrain-gohere/terrain-jog
    等地形交互名含 '-' 无 '_'，不能复用 interact 收集器的下划线判据），命中的
    **全部保留**——2026-07-18 血案：旧版取"首个可解析的类"即返回，terrain-jog
    类名存在所以永远选中它，push 被拒直接 all_rejected，写在 hints 尾部的
    terrain-gohere 兜底从未运行（8 次实测全拒）。hints 是"被拒兜底链"不是
    "取类兜底链"——与 push 原语的 _try_push_candidates 语义对齐。
    全未命中返回 []——调用方回传 no_affordance，不降级（意图保真）。
    hints 空：默认 [terrain-gohere]，_get_gohere_affordance 兜底（现行为不变）。
    """
    if hints:
        candidates = []
        seen = set()
        for h in hints:
            aff = _find_affordance_global(h)
            if aff is not None and id(aff) not in seen:
                seen.add(id(aff))
                candidates.append(aff)
        return candidates
    default = _find_affordance_global("terrain-gohere") or _get_gohere_affordance(sim_obj)
    return [default] if default is not None else []


_GOTO_OBJECT_OFFSET_METERS = 1.0  # 物品目标落点向 actor 平移的距离


def _offset_toward_actor(dest_pos, sim_obj, offset=_GOTO_OBJECT_OFFSET_METERS):
    """目的地坐标朝 actor 方向平移 offset 米（XZ 平面，Y 保持目标值）。

    actor 位置读不到/构造失败/两点重合时原样返回（游戏 Unroutable 兜底）。
    actor 距目标不足 offset 时直接用 actor 当前位置（本就站在旁边）。
    """
    try:
        actor_pos = getattr(sim_obj, "position", None)
        if actor_pos is None:
            return dest_pos
        dx = float(actor_pos.x) - float(dest_pos.x)
        dz = float(actor_pos.z) - float(dest_pos.z)
        dist = (dx * dx + dz * dz) ** 0.5
        if dist < 1e-3:
            return dest_pos
        if dist <= offset:
            return actor_pos
        import sims4.math
        scale = offset / dist
        return sims4.math.Vector3(float(dest_pos.x) + dx * scale,
                                  float(dest_pos.y),
                                  float(dest_pos.z) + dz * scale)
    except Exception as e:
        log_error("mythica_action._offset_toward_actor",
                  "offset failed, use raw dest: {}".format(str(e)[:80]))
        return dest_pos


def _generate_nearby_positions(dest_pos, sim_obj, max_offset=2.0, step=0.5):
    """生成目标位置附近的偏移坐标（扇形展开，朝 actor 方向）。

    精确位置不可寻路时（家具几何内部、角落、水障），尝试周围可站立地砖。
    偏移矢量 = actor→target 方向，在此方向上从近到远、从中间到两侧展开。

    Yields:
        (label, Vector3) — label 用于 attempts 记录（如 "dest+1.0m"）
    """
    yield ("exact", dest_pos)
    try:
        actor_pos = getattr(sim_obj, "position", None)
        if actor_pos is None:
            return
        dx = float(actor_pos.x) - float(dest_pos.x)
        dz = float(actor_pos.z) - float(dest_pos.z)
        dist = (dx * dx + dz * dz) ** 0.5
        if dist < 1e-3:
            return
        ux, uz = dx / dist, dz / dist          # 单位方向向量（指向 actor）
        px, pz = -uz, ux                        # 正交向量（左 90°）
        import sims4.math
        for toward in (0.5, 1.0, 1.5, 2.0):
            if toward > max_offset:
                break
            base_x = float(dest_pos.x) + ux * toward
            base_z = float(dest_pos.z) + uz * toward
            yield ("dest+{:.1f}m".format(toward),
                   sims4.math.Vector3(base_x, float(dest_pos.y), base_z))
            for perp_sign, perp_label in ((1.0, "L"), (-1.0, "R")):
                yield ("dest+{:.1f}m{}0.5m".format(toward, perp_label),
                       sims4.math.Vector3(
                           base_x + px * 0.5 * perp_sign,
                           float(dest_pos.y),
                           base_z + pz * 0.5 * perp_sign))
    except Exception:
        return


def _goto_generic_action(sim_obj, target_entity, target_kind, action):
    """通用地形移动原语——走到目标实体（sim 或物品）的当前坐标。

    目的地坐标/routing_surface 取【目标实体】的（用 actor 自己的 surface 在
    跨楼层/街区时报 "Cannot GoHere! Unroutable area."，2026-07-16 实测教训）。
    目标 rs 读不到时兜底 actor 自己的 rs 并落盘标注（原 walk_obj 行为推广到
    sim 目标）——跨层时游戏会以 Unroutable 可读拒绝，游戏是最终裁判。
    affordance 候选由 hints 指定（如 ["terrain-jog", "terrain-gohere"]），
    逐候选重试——被拒自动落到下一个（与 push 原语 _try_push_candidates 对齐，
    2026-07-18 修复：旧版只推首个可解析的类，jog 被拒时 gohere 兜底从未运行）。
    空 hints = terrain-gohere 默认。

    Returns:
        结果字段 dict（status=pushed/no_affordance/all_rejected/error + attempts）
    """
    character_name = action.get("character_name", "")
    description = action.get("description", "")
    target_name = action.get("target_name", "")
    hints = action.get("affordance_hints") or []

    candidates = _resolve_goto_candidates(sim_obj, hints)
    if not candidates:
        _show_action_notification(character_name, description)
        log_error("mythica_action._goto_generic_action",
                  "goto affordance unresolved (hints={}) — 不降级".format(hints))
        return {"status": "no_affordance"}

    dest_pos = getattr(target_entity, "position", None)
    try:
        rs = getattr(target_entity, "routing_surface", None) or \
             getattr(target_entity, "intended_routing_surface", None)
    except Exception as e:
        rs = None
        log_error("mythica_action._goto_generic_action",
                  "routing_surface 读取失败: {}".format(str(e)[:80]))
    if rs is None:
        # 兜底：actor 自己的 surface（跨层可能 Unroutable，落盘标注便于排查）
        try:
            rs = getattr(sim_obj, "routing_surface", None)
        except Exception:
            rs = None
        if rs is not None:
            log_error("mythica_action._goto_generic_action",
                      "target rs unavailable for {} — fallback actor rs".format(target_name))
    if dest_pos is None or rs is None:
        _show_action_notification(character_name, description)
        log_error("mythica_action._goto_generic_action",
                  "goto dest unavailable: pos={} rs={}".format(dest_pos, rs))
        return {"status": "error", "detail": "dest unavailable"}

    # 物品目标落点偏移（2026-07-18 第二轮实测）：家具类物品的 position 在自身
    # 几何内部，gohere 到该精确点 100% "Cannot GoHere! Unroutable area."
    # （冰箱 4/4 全拒）。目的地朝 actor 方向平移 1m，落到物品旁可站立的地砖；
    # 方向上仍可能撞进台面等障碍——游戏以 Unroutable 可读拒绝，是最终裁判。
    # sim 目标不偏移（坐标在可走地面，历史成功率高；坐姿目标偶发拒绝先观察）。
    if target_kind == "object":
        dest_pos = _offset_toward_actor(dest_pos, sim_obj)

    # 逐候选重试循环（cap 8 与 push 原语一致）；失败通知只发一次（循环结束后）
    # 每个候选精确位置被拒后，尝试目标周围偏移位置（扇形展开，朝 actor 方向）
    attempts = []
    for affordance in candidates[:8]:
        pushed = False
        for pos_label, pos in _generate_nearby_positions(dest_pos, sim_obj):
            outcome = _push_gohere_to_point(sim_obj, affordance, pos, rs,
                                            target_name, character_name, description)
            # 标注偏移位置便于日志排查
            outcome_attempts = outcome.get("attempts") or []
            for a in outcome_attempts:
                if isinstance(a, dict) and pos_label != "exact":
                    a["offset"] = pos_label
            attempts.extend(outcome_attempts)
            if outcome.get("status") == "pushed":
                outcome["attempts"] = attempts
                return outcome
            if outcome.get("status") == "error" and not outcome.get("attempts"):
                # pick/context 构造失败——与候选/位置无关，直接返回
                outcome["attempts"] = attempts
                _show_action_notification(character_name, description)
                return outcome
            # 被拒 → 下一个偏移位置
        # 所有偏移位置都失败 → 下一个候选

    _show_action_notification(character_name, description)
    log_error("mythica_action._goto_generic_action",
              "all goto candidates rejected for: {} (hints={})".format(description, hints))
    return {"status": "all_rejected", "attempts": attempts}


def _push_gohere_to_point(sim_obj, affordance, dest_pos, rs, target_name, character_name, description):
    """目的地 TerrainPoint + pick 定向 push——goto 原语的单候选尝试核。

    构造模式经 ai_probe_pick 实测验证（2026-07-16 23:04 走通）：
      TerrainPoint(Location(Transform(目的地), rs)) + PickInfo(PICK_TERRAIN, tp,
      location=, routing_surface=) + InteractionContext(..., pick=) → push(affordance, tp, ctx)
    关键教训：pick.target 与 location/routing_surface 三者必须同源同点（血案 §6）；
    目的地在非激活区域时游戏以 "Cannot GoHere! Unroutable area." 可读拒绝，记日志即可。
    失败不弹通知——调用方 _goto_generic_action 逐候选重试，循环结束统一通知一次
    （2026-07-18：多候选时代避免每个被拒候选都弹一个窗）。

    Args:
        sim_obj: 执行移动的 Sim 实例
        affordance: 本次尝试的地形交互类（调用方经 _resolve_goto_candidates 取得）
        dest_pos: 目的地世界坐标（Vector3）
        rs: 目的地 routing_surface（必须与 dest_pos 同源）
        target_name / character_name / description: 日志与通知用

    Returns:
        结果字段 dict（status=pushed/all_rejected/error + attempts；
        pick/context 构造失败时 attempts 为空——调用方据此判定不可重试）
    """
    try:
        import sims4.math
        from objects.terrain import TerrainPoint
        from server.pick_info import PickInfo, PickType
        from interactions.context import InteractionContext
        from interactions.priority import Priority
        terrain_point = TerrainPoint(sims4.math.Location(
            sims4.math.Transform(dest_pos), rs))
        pick = PickInfo(PickType.PICK_TERRAIN, terrain_point,
                        location=dest_pos, routing_surface=rs)
        ctx = InteractionContext(sim_obj, InteractionContext.SOURCE_SCRIPT,
                                 Priority.High, client=None, pick=pick)
    except Exception as e:
        log_error("mythica_action._push_gohere_to_point",
                  "pick/context 构造失败: {}".format(str(e)[:120]))
        return {"status": "error",
                "detail": "pick/context 构造失败: {}".format(str(e)[:120])}

    aff_name = getattr(affordance, "__name__", str(affordance))
    src = classify_tuning_source(aff_name, getattr(affordance, "guid64", None))
    try:
        result = sim_obj.push_super_affordance(affordance, terrain_point, ctx)
    except Exception as e:
        log_error("mythica_action._push_gohere_to_point",
                  "push raised: {}".format(str(e)[:120]))
        return {"status": "error", "detail": "push raised: {}".format(str(e)[:120]),
                "attempts": [{"affordance": aff_name, "src": src, "ok": False,
                              "result": "raised: {}".format(str(e)[:200])}]}
    pushed = bool(result)
    if not pushed and hasattr(result, 'result'):
        pushed = bool(result.result)
    attempt = {"affordance": aff_name, "src": src, "ok": pushed,
               "result": str(result)[:200]}
    if pushed:
        log_error("mythica_action._push_gohere_to_point",
                  "pushed {} [{}] -> {}: result={}".format(
                      aff_name, src, target_name, result))
        _show_action_notification(
            character_name, "{} ({})".format(description, aff_name))
        return {"status": "pushed", "pushed_affordance": aff_name,
                "affordance_src": src, "attempts": [attempt]}
    # 目的地不可寻路（目标在非激活区域/离场中/坐标点被家具占用）等——记录可读拒因
    log_error("mythica_action._push_gohere_to_point",
              "rejected {} [{}] -> {}: result={}".format(
                  aff_name, src, target_name, result))
    return {"status": "all_rejected", "attempts": [attempt]}


def _find_inventory_item(sim_obj, target_name, target_def):
    """在 sim 的物品栏中按名字或 definition 名匹配物品。

    Args:
        sim_obj: sim 游戏对象实例
        target_name: 物品显示名（来自 sandbox ActionOption.target_name）
        target_def: definition tuning 类名（如 object_guitar_Acoustic）

    Returns:
        匹配到的物品 game object 或 None
    """
    if sim_obj is None:
        return None
    try:
        inv = None
        for inv_attr in ("inventory", "inventory_component", "_inventory"):
            inv = getattr(sim_obj, inv_attr, None)
            if inv is not None:
                break
        if inv is None:
            return None
        for items_attr in ("get_items", "items", "all_items", "__iter__"):
            try:
                items_method = getattr(inv, items_attr, None)
                if items_method is None:
                    continue
                if callable(items_method):
                    items = list(items_method())
                else:
                    items = list(items_method)
                for item in items:
                    try:
                        # 按 definition 名匹配（最精确）
                        item_def = getattr(item, "definition", None)
                        if item_def is not None:
                            def_name = str(getattr(item_def, "__name__", "") or "")
                            if target_def and def_name.lower() == target_def.lower():
                                return item
                        # 按物品名匹配（兜底）
                        item_name = str(getattr(item, "__name__", "") or "")
                        if target_name and target_name.lower() in item_name.lower():
                            return item
                    except Exception:
                        continue
                break
            except Exception:
                pass
    except Exception:
        pass
    return None


# ── 动作类型路由表（≥5 分支纪律）──
# 值 = (target_kind 默认值, handler)。统一 handler 签名：
#   handler(sim_obj, target_entity, target_kind, action) -> dict(结果字段)
# 通用类型（push/goto）kind 默认 None——payload 必须显式带 target_kind；
# legacy 别名（永久保留，成本≈0）由路由行补默认 kind，行为与旧版等价。
# idle / stop（清队，2026-07-18）在 _execute_one_action 提前 return 不进表；
# 未命中走 unknown log_error + error 结果回传（版本偏斜时两端都可见）。
# 定义在所有 handler 之后。
_ACTION_ROUTES = {
    "push":            (None,     _push_generic_action),
    "goto":            (None,     _goto_generic_action),
    "interact":        ("sim",    _push_generic_action),
    "interact_object": ("object", _push_generic_action),
    "walk":            ("sim",    _goto_generic_action),
    "walk_obj":        ("object", _goto_generic_action),
}


# ── 执行结果回传 ──

def _collect_actor_state(sim_obj):
    """采集 sim 执行状态快照（push/send 后调用）。

    返回 dict，供 result.update() 合并入 /action_result payload。
    2026-07-26 _rv=2：沙盘据此在收到回传瞬间就知道"sim 正在做什么、
    我们的动作排在什么位置"，不必等下一个 3s 快照。

    字段：
    - actor_current_action: 正在执行的交互 affordance 名（空=空闲）
    - actor_queue_depth: 队列中总交互数
    - actor_queue_head: 队首交互 affordance 名（可能等于 actor_current_action）
    - actor_idle: sim 是否空闲（current_action 为空或姿态类）
    """
    result = {"actor_current_action": "", "actor_queue_depth": 0,
              "actor_queue_head": "", "actor_idle": True}
    if sim_obj is None:
        return result

    # 当前正在执行的交互（running_interactions_gen 第一个）
    try:
        running_list = []
        for m_name in ("running_interactions_gen", "get_all_running_and_queued_interactions"):
            m = getattr(sim_obj, m_name, None)
            if callable(m):
                try:
                    running_list = list(m())
                except Exception:
                    pass
                if running_list:
                    break
        for si_obj in running_list:
            aff = getattr(si_obj, "affordance", None)
            name = getattr(aff, "__name__", "") or "" if aff is not None else ""
            if name:
                result["actor_current_action"] = name
                break
    except Exception:
        pass

    # 队列深度 + 队首
    try:
        q = getattr(sim_obj, "queue", None)
        if q is not None:
            all_queued = []
            for m_name in ("get_all_running_and_queued_interactions", "_interactions"):
                m = getattr(q, m_name, None)
                if m is not None:
                    try:
                        val = m() if callable(m) else m
                        all_queued = list(val or [])
                    except Exception:
                        pass
                    if all_queued:
                        break
            result["actor_queue_depth"] = len(all_queued)
            if all_queued:
                head = all_queued[0]
                aff = getattr(head, "affordance", None)
                if aff is not None:
                    result["actor_queue_head"] = getattr(aff, "__name__", "") or ""
    except Exception:
        pass

    # 空闲判定：current_action 为空或纯姿态
    ca = result["actor_current_action"]
    idle_like = {"", "sim-stand", "sim_stand", "sim-Stand",
                 "stand_passive", "sit_passive", "stand", "none"}
    result["actor_idle"] = (ca.lower() in idle_like)

    return result


def _build_result_base(action, target_kind):
    """组装结果回传的公共字段（纯函数，handler 结果字段由调用方 update 合并）。

    result_id = action_id@毫秒时间戳——桌面端去重键（hub 转发+直连双投递）。
    status 语义（≤6 种）：pushed=EnqueueResult 为真（**只保证入队，非已执行**）/
    all_rejected=候选全被游戏拒 / no_affordance=hints 未命中或无候选（不降级路径）/
    not_found=actor 或 target 解析失败（detail 区分）/ error=构造失败或协议错误 /
    cleared=stop 动作清队完成（2026-07-18，detail 带清除计数）。
    """
    action_id = action.get("action_id", "")
    return {
        "_rv": 2,
        "result_id": "{}@{}".format(action_id, int(time.time() * 1000)),
        "action_id": action_id,
        "character_id": action.get("character_id", ""),
        "character_name": action.get("character_name", ""),
        "action_type": action.get("action_type", ""),
        "target_kind": target_kind or "",
        "target_name": action.get("target_name", ""),
        "description": action.get("description", ""),
        "status": "error",
        "detail": "",
        "pushed_affordance": "",
        "affordance_src": "",
        "attempts": [],
        # _rv=2 新增（2026-07-26）：推送时刻的 sim 执行状态
        "actor_current_action": "",
        "actor_queue_depth": 0,
        "actor_queue_head": "",
        "actor_idle": True,
        "ts": get_log_time(),
    }


def _send_action_result(result):
    """回传单个动作执行结果到桌面端 + 一行摘要落盘（保留本地排查轨迹）。"""
    log_error("mythica_action.result",
              "{} {} {} {}".format(
                  result.get("status", "?"), result.get("action_id", "?"),
                  result.get("pushed_affordance", "") or result.get("detail", ""),
                  result.get("character_name", "")))
    try:
        send_action_result(result)
    except Exception as e:
        # send_action_result 内部已自保护，此处兜底防意外异常中断动作循环
        log_error("mythica_action._send_action_result",
                  "send failed: {}".format(str(e)[:100]))


def _resolve_sim_info(sim_id_str):
    """根据 sim_id 字符串查找 sim_info 对象。"""
    if not sim_id_str:
        return None
    try:
        sim_id = int(sim_id_str)
        return services.sim_info_manager().get(sim_id)
    except (ValueError, Exception):
        return None


def _show_action_notification(character_name, description):
    """在游戏中显示动作执行通知（右上角弹出）。"""
    try:
        from ui.ui_dialog_notification import UiDialogNotification
        msg = u"{} — {}".format(character_name, description)
        notification = UiDialogNotification.TunableFactory.create(
            text=msg,
            ui_resolver=None,
        )
        notification.show_dialog()
    except Exception:
        # notification 失败不影响动作执行（游戏 UI 可能不可用）
        pass


def _write_action_log(timestamp, inner_voice, actions):
    """写入动作执行日志。"""
    lines = [u"{} AI 动作执行".format(timestamp)]
    if inner_voice:
        lines.append(u"  内心: {}".format(inner_voice[:120]))
    for a in actions:
        lines.append(u"  -> {} {}: {}".format(
            a.get("character_name", "?"),
            a.get("action_type", "?"),
            a.get("description", "?"),
        ))
    lines.append("")
    # append_to_file_with_rotation 自带异常保护（失败写 file_rotation 日志），无需双重 try
    append_to_file_with_rotation(
        os.path.join(get_output_directory(), "AI_Sandbox_Actions.txt"),
        "\n".join(lines),
        max_size=1 * 1024 * 1024,
        rotation_dir=os.path.join(get_output_directory(), "history"),
    )
