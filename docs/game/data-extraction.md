# Sims 4 数据获取手册 — 从游戏里把数据拿出来的实战知识库

> 2026-07-16 ~ 07-17 两天实测沉淀（快照 _sv 11→13、SimBundle 27→42 字段、探针工具箱 0→11 命令）。
> Sims 4 Python API 无官方文档，本文所有结论均来自游戏内实测（探针 txt / Bundle_Probe / 沙盘 dump），不含猜测。
> 姊妹篇：[sims4_action_injection.md](sims4_action_injection.md)（写入侧——怎么把动作推进游戏）、
> [probe_toolkit_design.md](probe_toolkit_design.md)（探针工具箱设计）、
> [probe_system_20260715.md](probe_system_20260715.md)（API 发现流水账）。
> 生产实现：游戏端 [`自制mod/my_script.py`](../../自制mod/my_script.py) `_get_sim_bundle()` / `_collect_scene_snapshot()`、
> 清洗 [`mythica_clean.py`](../../自制mod/mythica_clean.py)、沙盘解析 [`mythica_sandbox/game_bridge.py`](../mythica_sandbox/game_bridge.py)。

---

## 1. 一张图：数据获取全链路

```
游戏端（Python 3.7，mod 加载即启动，不依赖 AI 总开关）
  _get_sim_bundle(sim_or_obj)          ← per-sim 42 字段统一货架（SimBundle）
  clean_sim_bundle_fields()            ← 源头去噪（mythica_clean.py，_sv=13）
  ├─ 通道① queue_probe（1s）：交互事件 + actor/target 40 前缀字段  [_sv=3]
  └─ 通道② scene_snapshot（3s）：在场家庭成员+NPC 全量 + pairwise 关系
       + 场景（时间/天气/地点/活动/节日/经济）+ 物品 top50        [_sv=13]
  → HTTP POST [52174 沙盘, 52173 主程序]（ProbeHub 中枢可转发）

沙盘端（Python 3.12）
  parse_scene_snapshot_to_world_state / parse_probe_to_world_state
  ├─ _char_from_fields()               ← CharacterState 唯一构造点（三路径共用）
  ├─ _derive_state_events()            ← 快照差分 → 状态变化事件（到离/关系/需求/buff/心情）
  └─ WorldState → to_prompt_context/to_pov_status/describe_relations → AI prompt
                → dump_world_state_to_file → sandbox_world_state.txt（验证面）
```

**协议纪律**：字段变更必 bump `_sv`（游戏端内联字面量 ↔ 沙盘 `_EXPECTED_*_SCHEMA_VERSION`），
版本不匹配沙盘**只警告不阻塞**（尽力解析）——两端可分批部署。版本史见游戏端 CLAUDE.md "_sv 版本历史"表。

---

## 2. 顶层策略（这两天验证有效的决策）

| 决策 | 内容 | 为什么对 |
|---|---|---|
| **先采齐，再消费** | v12 一次性把能采的全采（14 空间/日程/社交字段 + NPC + 物品状态），只落 dump 不进 prompt；消费轮（v0.6.0）再决定用途 | 游戏端改动贵（编译+重启游戏+删缓存），一次协议升级换桌面端无限次迭代 |
| **SimBundle 统一货架** | 所有 per-sim 数据一次采集进一个 dataclass，queue_probe/快照/dump 全部下游按需取用 | 消灭"两条通道各采一份"的漂移；加字段=声明+to_dict 两行 |
| **瘦游戏端 + 源头去噪例外** | 游戏端只发原始 API 读数（坐标发 xyz 不算距离）；唯一例外：去噪归一（hash 后缀/技术 flag/ID→名字）在源头做一次全下游受益 | 语义/翻译/决策留桌面端随时改；但"数据正确+干净"只有游戏端能保证（本地化名、ID 解析） |
| **只取值不调方法** | 探针和采集对未知 API 只 getattr 值；方法必须单独实测（写入侧手册的领域） | `dir()` 一半是方法，盲调可改游戏状态甚至崩游戏 |
| **每字段独立降级** | 每个字段独立 try/except Exception + 类型化默认值（str=""/list=[]/dict={}/bool=False），单字段失败不传染 | 装满 mod 的环境任何 API 都可能炸；采集函数最外层再兜一层 return empty() |

---

## 3. API 发现方法论（怎么知道"数据在哪、叫什么"）

**🔒 纪律：接入新 API 前先 grep 图书馆**（`MythicaData/api_library/types_index.txt`，863 类型/5.9 万属性，
`ai_probe_library` 1-4s 重建）。命中直接写采集；没命中才进游戏跑探针。

四层选择法（详见设计文档）：

| 问题 | 工具 | 今天的实例 |
|---|---|---|
| 存在什么/叫什么 | **图书馆** grep | `SimInfo.sim_info` 陷阱 5 秒定位；`_whim_slots.goal_instance` 现成可读名 |
| 具体在哪个属性 | `ai_probe_deep <root> <kw>` | services 上找 lunar → `lunar_cycle_service` |
| 字段名猜不到 | `ai_probe_diff`（改状态前后差分） | "outfit 存哪"类问题；基线必须钉 sim_id |
| 采集结果验证 | `ai_probe_bundle`（dataclass 内省逐字段 [空]/[OK]） | v12/v13 扩容零成本回归 |

**关键认知：私有属性是半个世界。** TS4 惯例"数据存 `_x`、property `x` 包一层"——
`_current_whims`、`_active_buffs`、`_whim_slots` 全是单下划线。扫描器只跳 `__dunder__`，
不跳单下划线（图书馆 v2 因此从 423 类型翻倍到 863）。

---

## 4. 实测 API 对照表（❌ 猜的 → ✅ 实测正确）

### 4.1 Sim 空间/状态

| 数据 | ✅ 正确姿势 | ❌ 踩过的坑 |
|---|---|---|
| 坐标 | `sim.position` → Vector3(.x/.y/.z) | — |
| 楼层 | `sim.level`（退 `routing_surface.secondary_id`） | — |
| 房间号 | `build_buy.get_room_id(zone_id, position, level)`（装载过场未就绪需降级） | — |
| 室内外 | `sim.is_outside` | — |
| 坐在哪 | `sim.posture.target`（站立时为 None——合法空） | — |
| 当前交互 | `sim.running_interactions_gen()` / `get_all_running_and_queued_interactions()` | ❌ `get_running_interactions` **不存在**（调了几周静默空） |
| 着装 | `sim_info.get_current_outfit()[0]` 返回**裸 int** → `OutfitCategory(int).name` | ❌ 直接 `getattr(cat,'name')` 得 "6" |

### 4.2 Sim 日程/心理

| 数据 | ✅ 正确姿势 | ❌ 踩过的坑 |
|---|---|---|
| 愿望 whims | `sim_info._whim_tracker._whim_slots` → `slot.goal_instance.__name__`（`Whim_TellJoke`）+ `slot.is_empty()` 过滤空槽 | ❌ tracker 的 get_active_whims 等方法名全不存在；❌ `sim_info.current_whims` 是 **protobuf 原文**（只有 guid，4 个 instance manager 都解析不出名字） |
| 恐惧 | trait_tracker equipped_traits 里 `trait_type == FEAR` 或名含 fear（**状态依赖**：没恐惧就是空） | — |
| 取向 | `get_attracted_genders()` + hidden_traits 里 `trait_SexualOrientation_*` | ❌ `sim_info.sexual_orientation` 属性恒 None |
| 年龄进度 | `sim_info.days_until_ready_to_age()` | — |
| 谱系 | `sim_info.spouse_sim_id` + `genealogy.get_parent_sim_ids_gen()`（另有 children/siblings/grandparents 全套 gen） | — |
| 社交组 | `sim.get_groups_for_sim_gen()` → 组内成员（没在聊天=空，合法） | — |
| buff 详情 | `sim.Buffs._active_buffs` {buff_type: instance}；mood_weight 在 buff_type 上；常驻 buff 无 timeout | — |

### 4.3 场景/家庭

| 数据 | ✅ 正确姿势 | ❌ 踩过的坑 |
|---|---|---|
| 月相 | `services.lunar_cycle_service().current_phase` | ❌ `get_lunar_cycle_service` 不存在（get_ 前缀是猜的） |
| 日历 | `services.calendar_service()` ——**是函数要调用** | ❌ 对函数对象 hasattr 检查 → 恒返回 []（潜伏 bug） |
| 节日 | `drama_scheduler_service().active_nodes_gen()` 类名含 holiday | — |
| 活动 | `get_zone_situation_manager().running_situations()` 类名清洗 | — |
| 资金/账单 | `household.funds`；`bills_manager.current_payment_owed / housing_costs_owed / is_any_utility_delinquent()` | — |
| 精确时间 | `game_clock_service().now()` 的 `.minute()` 一直都有——只是以前没透传 | — |
| 关系分 | statistic mgr `.get(16650/16651)` + `relationship_tracker.get_relationship_score` | ❌ rel 对象上没有 get_friendship_track |
| 物品占用 | `obj.get_users(sims_only=True)` | — |
| 物品状态 | `state_component.values()` 状态名关键词匹配 | ❌ **正常态穿帮**：`Brokenness_Unbroken` 含 "broken"、`Upgrade_..._NotStarted` 含关键词——必须二次排除负形态（unbroken/clean/not_burning/notstarted…） |

---

## 5. 类型归一化陷阱（本手册最贵的一课，双向都踩了）

**Sims 4 的 Sim（场上实例）和 SimInfo（档案）是两个对象，且互相都有对方的"影子属性"——
duck-typing 判据必须用"目标类独有"的属性，不能用两类共有的。**

| 血案 | 现象 | 根因 |
|---|---|---|
| `SimInfo.sim_info` 陷阱（v0.5.5） | 快照家庭成员的 position/walkstyle/current_action **自 v11 起全体静默为空**，dump 里有空间数据的全是 NPC | `hasattr(x, "sim_info")` 判"是否 Sim 实例"——但 **SimInfo 自己也有 .sim_info**（self 引用）→ sim_obj 被赋成 SimInfo；`SimInfo.Buffs` 恰好也存在 → 心情/buff 正常，掩盖问题数小时 |
| `instanced_sims_gen` 反向陷阱（v0.5.10） | pick/queue_diff 探针三跑全空 | 它产出的是 **Sim 实例**（有 position），代码却按 SimInfo 处理去调 `get_sim_instance()` → 全部跳过 |

**归一化范式**（生产代码 `_get_sim_bundle` / `_as_sim_instance`）：

```python
# 判 Sim 实例：sim_info 属性 + position 属性双判据（图书馆证实 SimInfo 无 position）
if hasattr(x, "sim_info") and hasattr(x, "position"):
    sim_obj, sim_info = x, x.sim_info
```

附带认知：`type(sim).__name__` 是 **`object_sim`**（TS4 按 definition 动态造类），不是 "Sim"——
按类名判断也不可靠。

---

## 6. "空值"的四种成分（拿到空字段先分类再动手）

| 类型 | 例子 | 判读 | 处置 |
|---|---|---|---|
| **状态依赖空**（合法） | fears（没恐惧）/ social_group（没聊天）/ occult_stats（人类）/ posture_target（站着）/ 账单（没到期） | 想想"该 sim 此刻是否该有值" | 不修；`ai_probe_bundle` 输出里已注明 |
| **API 名猜错空**（bug） | get_running_interactions / get_lunar_cycle_service / whims tracker 方法 | 直读探针（§0.5 式不吞异常）通、bundle 不通 | 查图书馆找真名 |
| **归一化/解析空**（bug） | v0.5.5 全家 position 空 / 探针 present_sim_ids list 被 str() 切 | 同一快照里 A 类 sim 有值 B 类没有 = 路径问题 | 查构造点/归一化 |
| **协议漂移空** | 沙盘 expected 13 收到 12 | 日志 schema mismatch 警告 | 两端对版本 |

**排查铁律：先用不吞异常的直读探针确认"API 本身通不通"，再查管道**——
V12_Probe §0.5（空间原始读数）就是为此存在的；异常文本本身是信息，别在采集层外提前吞掉。

---

## 7. 噪音处理：源头清洗 vs 消费层归一（分界线）

| 层 | 管什么 | 实例 |
|---|---|---|
| **游戏端源头清洗**（mythica_clean.py，纯函数+桌面 pytest） | 数据"正确+干净"：mod hash 后缀（`GREEN_xxx_Trait/2964921243`）、作者 token、叠词、技术 flag trait 过滤、inventory 聚合 `[{name,count}]`、ID→名字（只有游戏端能解析本地化名） | _sv=13 |
| **沙盘消费层归一** | 渲染语义：idle 动作过滤（`Sim-Stand`→不渲染"正在X"）、技术 buff 排除（AlwaysOn/Hidden/Controller 不进 diff 事件）、正常态物品状态不标注、异刻度 motive 拆 `special_stats`（reputation 400 不该按"<50=低需求"判） | v0.6.0+ |

原则：**清洗规则是模式规则不是 mod 名单**（不枚举具体 mod 名，用 hash 形态/前缀模式匹配），
且必须是纯函数、桌面可跑 pytest（真实脏样本做用例）。

---

## 8. 部署与验证纪律（数据不对时先排除"跑的是旧包"）

- **一键部署**：`python deploy_mod.py [--check 新函数名]`——清 `__pycache__`（防 py312 pyc 混入）→ py3.7 全量编译 →
  只取 cpython-37 → zip → 字节串验证 → 双侧 md5 → 删缓存确认。任一步失败即停
- **BUILD_STAMP 版本自报**：每次打包写时间戳进 constants，游戏加载 log `[mythica] V1 build <stamp>`、
  探针 txt 头同带——**看数据前先核对 build 号**
- **verify_module_coverage**：新增模块忘进打包清单 = 整包 `ModuleNotFoundError` 且错误日志管道一起死（v0.7.1 事故）——
  现在打包前自动扫 import 引用，缺谁指名谁
- **验证三件套**：`ai_probe_bundle`（字段级 [空]/[OK]）→ `sandbox_world_state.txt`（管道端到端）→
  `verify_bundle_chain.py`（SimBundle→_CHAR_HIT_FIELDS→CharacterState 四端字段静态防漂移）
- 游戏侧改动必须：重启游戏 + 删 `localthumbcache.package`（删缓存不会重置"允许脚本模组"，无需重新勾选；mod 静默不加载时才检查该选项）

---

## 9. 血案索引（一行一案，倒查用）

| 血案 | 根因 | 防复发 |
|---|---|---|
| 全家空间字段静默空数月 | SimInfo.sim_info 归一化误判 | position 双判据 + 图书馆纪律 |
| current_action 恒空 | get_running_interactions 不存在 | 图书馆查真名 running_interactions_gen |
| whims 乱码 | current_whims 是 protobuf | 换 _whim_slots.goal_instance（私有属性扫描的战果） |
| 月相/日历恒空 | 服务名猜错 / 函数没调用 | 图书馆 + "services.xxx 是函数要加 ()" |
| 物品"损坏"误报 | Unbroken 含 broken | 负形态排除表 |
| diff 探针对比了俩 sim | 采样间 active_sim 换人 | 基线钉 sim_id（跨时间采样必钉实体） |
| 部署 5 次生效 0 次 | 缓存没删/旧包覆盖/py312 pyc | deploy_mod.py + BUILD_STAMP |
| 整包加载失败且日志静默 | 新模块没进打包清单 | verify_module_coverage |
| 探针在场者恒空 | list 被 str() 后按逗号切 | 解析端类型防御 + 尽力解析下的类型兼容测试 |
| 探针发 25 字段沙盘只解析 12 | 双端字段清单人工同步漂移 | 统一构造点 _char_from_fields + verify_bundle_chain.py |

---

## 10. 还没做/边界（诚实清单）

- **状态事件 0 条悬案**（2026-07-17 在查）：快照差分派生事件真实场景 25 分钟 0 产出，诊断心跳已挂（grep `game_bridge.derive`）
- **多状态图书馆 merge**：库目前覆盖式重跑；换派对/occult 存档时做 merge + last_seen 标签
- **值的含义无法自动获取**：`fat=-68.9 是瘦是胖`、关系 track 16650=友谊——都靠对照游戏画面人工标定，探针给不了语义
- **寻路距离/可达性不采**：每对 sim 寻路测试开销大，欧氏距离+room_id 桌面端算已够用
- **NPC 无 pairwise 关系**：关系分只采家庭成员间（×50 上限），NPC 关系按需再说
