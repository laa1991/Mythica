# Mythica Sandbox — 架构文档

## 1. 系统总览

Mythica Sandbox 是 Mythica 生态中的**沙盘模拟引擎**，与旧 Mythica 桌面端共享 `mythica_lib/` 核心库，但定位完全不同：

| | 旧 Mythica | Mythica Sandbox |
|---|---|---|
| **角色** | AI 叙事作家 | AI 决策引擎 |
| **产出** | 长篇散文 + 结构化标记 | 短内心声音 + 可执行动作 |
| **AI 调用** | 三阶段（P1正文→P2提取→P3再提取+兜底） | **两段式**（内心声音 → 动作选择） |
| **方向** | 单向：游戏 → 桌面 | **双向**：游戏 ↔ 桌面（发送动作命令） |
| **复杂度** | 13 个 Mixin，600+ 函数，638 测试 | 10 个模块，~1,939 行，81 测试 |
| **依赖** | mythica_lib + config_household + config_events_* | **仅 mythica_lib** |

### 架构原则

- **极简第一** — 每个模块只做一件事，不建抽象层直到有第二个用例
- **数据驱动动作** — 动作目录从游戏状态动态生成，不硬编码
- **独立可测试** — 每个模块可以脱离 GUI 单独测试
- **渐进式复杂度** — v1 手动指挥 → v2 导演层 → v3 独立模拟器
- **五层架构**（2026-07-28）— 采集 → 决策 → 调度 → 执行 → 元层，单向依赖，每层只做一件事

### 1.0 五层架构全景（🆕 2026-07-28）

沙盘的所有代码按职责分五层。每一层只做一件事，单向依赖——上层读下层的数据，下层不依赖上层。

```
┌─────────────────────────────────────────────────────────────────┐
│                      元层 — 人机接口                              │
│  人类看状态、修世界。不参与自动循环，由人主动触发。                  │
│  character_status_panel  action_panel  app.py(自动动作页)         │
├─────────────────────────────────────────────────────────────────┤
│                      执行层 — 信号发射                             │
│  把决策/维护指令变成信号文件，发回游戏端。                          │
│  command_sender.py  game_bridge.emit_action_commands()           │
├─────────────────────────────────────────────────────────────────┤
│                      调度层 — 冲突裁决                             │
│  多个决策源提出诉求 → 拍板"谁来做、做什么、谁先来"。               │
│  engine._allocate_actions()  生存 > AI > 自动                     │
├─────────────────────────────────────────────────────────────────┤
│                      决策层 — 该做什么                             │
│  读 WorldState → 判断"谁该干什么"。                               │
│  三层：P4 生存层 / Tier 1 AI 层 / P3-P1 自动层                    │
├─────────────────────────────────────────────────────────────────┤
│                      采集层 — 数据输入                             │
│  从游戏读数据 → 解析为 WorldState。只读镜像，不修改、不发命令。     │
│  game_bridge.parse_scene_snapshot_to_world_state()               │
│  game_bridge.parse_probe_to_world_state()                        │
└─────────────────────────────────────────────────────────────────┘
         ▲                                                      │
         │         游戏端（Sims 4）                              │
         └──── HTTP 快照/探针（每 3s/1s 推送）                   │
                                                          ──────┘
                                      Action_Command / Maintenance_Command（信号文件）
```

**各层只答一个问题：**

| 层 | 问题 | 例子 |
|---|------|------|
| 采集层 | "游戏里现在发生了什么？" | 真鳕在厨房，饥饿 30，bladder -90 |
| 决策层 | "该做什么？" | "膀胱快爆了要上厕所" / "饿了想吃东西" |
| 调度层 | "谁来做、谁先来？" | 生存 > AI > 自动，同 sim 只派一件事 |
| 执行层 | "怎么发出去？" | 写 Action_Command.signal → 游戏端消费 |
| 元层 | "人想干什么？" | 看状态、改关系、加钱、手动指挥 sim |

**核心约束：采集层是只读镜像。** WorldState 是游戏的只读副本——往里面改数据（如 `ws.household_funds += 1000`）对游戏**没有任何效果**。要改变游戏状态，必须走执行层的信号文件通道：

```
❌ 错误：修改沙盘内存中的 WorldState → 期待游戏同步
✅ 正确：决策层产出 ActionOption → 调度层合并 → 执行层写 Action_Command.signal → 游戏执行
✅ 正确：元层人操 → 执行层写 Maintenance_Command.signal → 游戏执行
```

**两条执行通道：**

| | Action_Command | Maintenance_Command |
|---|---|---|
| 谁触发 | 调度层（自动循环） | 元层（人按按钮） |
| 做什么 | sim 行为：走路/社交/使用物品 | 世界状态：改钱/改需求/改关系 |
| 游戏端消费 | `mythica_action.py` | `mythica_maintenance.py` |
| 执行层入口 | `game_bridge.emit_action_commands()` | `command_sender.send_maintenance_command()` |

**加 1000 元属于哪层？** 决策层不管钱，调度层不管钱——加钱是人按按钮 → 元层触发 → 执行层 `send_maintenance_command()` 写成 `Maintenance_Command.signal` → 游戏端 `_execute_set_funds()` 真正改数据。**它是执行层动作，由元层触发，不经决策/调度层。**

### 1.1 决策层三层架构（🆕 2026-07-28）

沙盘的核心职责是"做出决策并送到游戏端执行"。决策来源有三个，按优先级组织：

```
_run_cycle():
  ├─ P4 生存层  _execute_motive_emergency()     程序规则
  │   问题："谁快不行了？"
  │   手段：读 bladder 值（唯一纯生理需求）
  │   原则：零叙事空间 → 程序直接推
  │
  ├─ Tier 1 AI  _call_inner_voice + _call_action_selector  大模型
  │   问题："谁有故事要讲？"
  │   手段：读叙事上下文 + 世界状态 → 内心声音 → 选动作
  │   原则：有叙事选择空间 → 全部交给 AI
  │
  ├─ P3-P1 自动  _execute_auto_triggers()        程序规则
  │   问题："场景有什么该干的？"
  │   手段：物品状态匹配 + 角色评分 + 反馈闭环
  │   原则：无叙事空间、无需 AI 判断 → 程序扫描+推送
  │
  └─ 汇合  _allocate_actions(motive, ai, auto)
      冲突裁决：生存 > AI > 自动
      同一 sim/object 每轮最多分配一个动作
      合并后统一走门控 → 发射 → 游戏执行 → 生命周期追踪
```

**每层只回答一个问题。** 这个约束是最核心的设计原则——它阻止了"一个方法做两件事"的倾向，让每层的行为可以独立推理和测试。

**分界法则：有没有"怎么做"的故事空间。**

| 有叙事空间 | 无叙事空间 |
|-----------|-----------|
| 怎么吃（剩菜 vs 做饭 vs 外卖）| 上厕所（只有一个动作） |
| 在哪睡（床 vs 沙发 vs 地板）| 修损坏的物品（看到就修，不需要理由） |
| 和谁聊天、说什么 | 收地上的垃圾/脏碗/脏衣服 |
| 泡澡还是淋浴 | 拖水坑 |

有叙事空间的事全部交给 AI——吃什么是故事素材，程序不该越界替 sim 决定。无叙事空间的事走程序规则——更快、零 token、可预测。

**各层优先级权重：**

| 层 | 优先级 | 语义 |
|:---|:---:|------|
| 生存 | P4 | 纯生理反射——不做会出问题 |
| AI | — | 叙事意图——Tier 1 在分配阶段优先于自动 |
| 自动-修理 | P3 | 崩了不能用——功能丧失 |
| 自动-清洁 | P2 | 脏环境掉心情——影响体验 |
| 自动-收集 | P1 | 捡东西——纯粹维护 |

**三层架构避免了两个经典问题：**

1. **重叠决策** — 同一件事被两层同时决定。旧版 AI 目录的 need 组和生存层都盯 hunger，但分配器的优先级确保了只执行一个。更重要的是，缩到只有膀胱后，生存和 AI 管的 motive 完全不重叠——AI 管 5 个，生存管 1 个不同源的。

2. **遗漏决策** — 没有人在管的事。旧版 hunger -90 的 sim 可能因为 POV 没选中而无人照顾（AI 没选他当 POV → G7 只能 defer 社交不能主动推 need）。生存层保证了最低限度的生理安全网。

**演进记录** — 生存层初始设计包含了 hunger/bladder/energy/hygiene/comfort 五种 motive（"饿到 -90 凭什么等 AI"）。讨论中意识到吃什么是故事素材而非纯反射，最终缩到只有膀胱。这个收敛过程本身验证了分界法则的有效性。详见决策 #41b。

#### 元层：人机接口

两个面板服务于三层但不属于三层——它们让人类能**看**决策系统的输入和**干预**决策系统的输出。元层是五层架构的第五层，通过 `Maintenance_Command` 通道修改世界状态（详见 §1.0 两条执行通道）。

```
元层 — 人机接口
├── 动作测试页（action_panel.py）             行为轴 — "让 sim 做什么"
│   ├── 🧪 规则孵化 → verified 流转
│   ├── 🖱 手动指挥 → 直接发射 Action_Command
│   └── 📋 时间线 → 所有层动作的 verdict 追踪
│
├── 自动动作页（app.py _refresh_auto_*）      配置轴 — "自动层怎么运转"
│   ├── 📋 规则清单 → 哪些 auto_trigger 启用/未验证
│   ├── 🎯 场景匹配 → 当前场景能否触发
│   └── ⭐ 角色优先级 → 优先/正常/回避（切换即生效）
│
└── 人物状态面板（character_status_panel.py）  状态轴 — "sim 是什么状态"
    ├── 📊 15 折叠区实时数据（快照驱动，三级刷新）
    ├── ⚡ 需求回满 → Maintenance_Command（直设 commodity_tracker）
    ├── 🏷 buff 增删 → Maintenance_Command
    └── ✏️ 关系编辑 → Maintenance_Command（直设分数 + 护栏）
```

| | 行为轴 | 配置轴 | 状态轴 |
|------|------|------|------|
| 读 | 时间线：verdict | 规则清单 + 触发状态 | 快照：sim 实时数据 |
| 写 | 发射 Action_Command | 改角色优先级 | Maintenance_Command |
| 和三层的联动 | verified → AI/自动 | 直接影响 P3-P1 的 sim 选择 | 状态数据 → 三层决策输入 |
| 章程 | "测规则、发命令" | "调参数、看覆盖" | "看状态、修世界" |

**为什么自动动作页单独存在：** 行为和状态面板是人机接口的通用件，但自动层有个独特需求——用户需要知道"哪些规则在自动跑、规则能不能匹配到当前场景的物品、哪些角色优先干活"。这些信息不需要在每次测试或看状态时出现，值得一个独立的监控/配置面板。

**为什么状态面板走维护通道而非动作通道：** 需求回满、buff 增删、关系编辑是对世界状态的修正，不是 sim 的行为动作——不需要等 sim 走过去、等动画播放。走 `Maintenance_Command` 独立信号文件，与 `Action_Command` 平行互不阻塞。这是调试/维护工具，不是叙事手段（决策 #38）。

**一个规则的完整生命周期：**
```
自定义规则（verified=False）
  → 动作测试页「🧪 待测动作」组
    → 手动实测 → /action_result 回传 → 时间线标注 verdict
      → confirmed ✅ → verified=True
        → 进 AI 目录（generate_action_catalog）
        → 如有 auto_trigger=True → 进自动触发列表
```


### 1.2 动作闭环：Observer → Rule 管道（🆕 2026-07-28）

沙盘的动作系统有三个独立但互补的观察来源，之前缺少一个统一的管道把"观察到的执行"转化为"可复用的规则"：

```
游戏自主执行 ──→ observer 捕捉 ObservedAction ──→ observed_actions.json
                                                   │
手动测试成功 ──→ 📋 存为规则 ──→ ObservedAction ──┤
                                                   │
                                          observer_to_rules
                                          (每 5 分钟自动跑)
                                                   │
                                          rules_observed.py
                                          (verified=False)
                                                   │
                                             🧪 待测动作组
                                             (人工实测升 verified)
                                                   │
                                             AI 动作目录
```

**核心数据结构：`ObservedAction`**（[observer_schema.py](observer_schema.py)）

三个缺口——observer 只记类名不记上下文、手动测试没有存为规则、从观察到规则没有自动桥——卡在同一个位置：没有结构化的"动作模板"来桥接"观察"和"规则"。`ObservedAction` 解决了这个问题：

```python
@dataclass
class ObservedAction:
    affordance_name: str     # EA 内部类名
    display_name: str        # 人类可读
    actor_id/name: str       # 谁做的
    target_id/name/type/definition: str  # 对谁/什么做的
    actor_location: str      # 在哪个房间
    actor_mood: str          # 什么心情
    preceding_action: str    # 前置动作（如 terrain-gohere）
    source: str              # autonomous | manual_test | player_directed
    count: int               # 观察次数
```

**三条路径汇入同一管道：**

| 路径 | 生产者 | 触发方式 | 数据来源 |
|------|--------|---------|------|
| 自动观察 | `autonomous_observer.py` | sim 的 `current_action` 变化时 | WorldState 快照 |
| 手动存规则 | `action_panel.py` | 用户在时间线点 📋 | ActionLifecycle + WorldState |
| 管道生成 | `observer_to_rules.py` | 每 5 次 observer 保存（~5min） | `observed_actions.json` |

**管道三步：**

1. **`group_by_pattern`** — 按 `(affordance_name, target_type)` 分组。同组的可能是同一条规则的不同实例（如"斑在破旧的画架上画画"和"真鳕在高级画架上画画"）。
2. **`suggest_rules`** — 每组推断通用 `target_match`（从多条观察的目标名中提取共有关键词），生成 `CustomActionRule`，附带置信度评分。
3. **`auto_commit_rules`** — 置信度 ≥ 3.0（至少被观察 3 次且有 target_definition）的自动写入 `rules_observed.py`。低于阈值的跳过（待人工审核）。

**置信度公式：** `count × type_factor`（有 target_definition=×3.0，有 target_name=×2.0，无=×1.0）

**数据文件：**

| 文件 | 格式 | 内容 |
|------|------|------|
| `observed_actions.json` | `[{ObservedAction}, ...]` | 完整的动作观察记录（含上下文） |
| `observed_rules.json` | `[{CustomActionRule fields}, ...]` | 已保存的观察规则（重启不丢） |
| `observer_report.txt` | 人读报告 | 实证覆盖 + 管道结果摘要 |

**模块清单（5 新 + 3 改）：**

| 模块 | 新增/修改 | 职责 |
|------|:---:|------|
| `observer_schema.py` | 新增 | `ObservedAction` dataclass + 转换函数 + 推断器 |
| `custom_actions/rules_observed.py` | 新增 | JSON-backed 规则存储 |
| `observer_to_rules.py` | 新增 | 自动生成管道 |
| `autonomous_observer.py` | 修改 | per-sim 状态追踪 + ObservedAction 捕获 + 管道触发 |
| `action_panel.py` | 修改 | 📋 存为规则按钮 + SaveAsRuleDialog |
| `custom_actions/__init__.py` | 修改 | 汇入 OBSERVED_RULES |
| `app.py` | 修改 | 管道结果 GUI 通知回调 |

**消费方向（2026-07-31 更新）：**

| ObservedAction 字段 | 消费为 | 状态 |
|------|------|:---:|
| `actor_mood` | `CustomActionRule.mood_requires` — 心情门控（如 `("Inspired",)`） | ✅ v0.14.2 |
| `actor_location` | `CustomActionRule.location_prefer` — 位置偏好（如 `("书房",)`） | ✅ v0.14.2 |
| `preceding_action` | `CustomActionRule.needs_goto` + `preceding_actions` — 动作链标注 | ✅ v0.14.2 |
| 多条观察 pattern | `CustomActionRule.target_exclude` — 自动推断排除关键词 | ✅ v0.14.2 |
| `duration_s` | `CustomActionRule.estimated_duration_s` — 预计耗时 | ✅ v0.14.1 |
| 链式动作序列 | 多步动作序列规则 | 待开发 |

**新增 CustomActionRule 字段（v0.14.2）：** `mood_requires` / `location_prefer` / `needs_goto` / `preceding_actions`。
**排序升级：** `sort_by_proven()` 从二元改为三级（黄金/白银/青铜），利用回路1回写的 `push_confirmed` 数据。
**冷启动：** `dynamic_min_confidence()` 根据数据量动态调整自动提交阈值（<10条→1.0, ≥100条→3.0）。

#### 社交动作闭环：target 反向查找

社交动作（`sim_Chat`、`sim_Hug` 等）的 target 推断与物品不同——没有 `posture_target` 可提取物品名。解决方案：从 `ws.recent_events`（探针交互事件缓冲）反向查找。

```
探针（1s）→ InteractionEvent(actor="斑", target="真鳕", action="sim_Chat")
                                         │
快照（3s）→ char.current_action = "sim_Chat"
              │
              ▼
         _infer_target_from_char()
           is_social=True → reversed(ws.recent_events)
           → 找到 actor="斑" 的最新事件 → target_name="真鳕"
```

**改动：** `autonomous_observer._infer_target_from_char()` —— 社交动作检测后遍历 `recent_events` 匹配 actor 名，取最新一条的 `target_name`。5 行。

#### 三域闭环全景

| 域 | target 来源 | affordance 来源 | 观察者 |
|---|---|---|---|
| 🔧 物品 | `posture_target` + 场景物品匹配 | `current_action` 解析 | `autonomous_observer` |
| 💬 社交 | `ws.recent_events` 反向查找 | `current_action` 解析 | `autonomous_observer` |

全部汇入同一管道：`ObservedAction` → `observer_to_rules` → `rules_observed.py`。


### 数据流全景

```
游戏端 (Sims 4 mod)                     桌面端
─────────────────────                    ──────
                                         ┌─ 采集层 ──────────────────────────────┐
┌─ queue_probe (1s) ──┐                  │                                      │
│ 交互事件: actor+target│──POST /queue_probe──→ ProbeHub (:52173)                 │
│ +动作+关系+心情       │                     │    │                             │
└──────────────────────┘                     │    ├─→ 主 Mythica                 │
                                             │    │                             │
┌─ scene_snapshot (3s)─┐                    │    ├─→ HTTP Forward → :52174      │
│ 全场: sims+天气+时间  │──POST /scene_snapshot─→┘    │     └─ 沙盘 server 接收     │
│ +地点+物品+资金       │                     │    │                             │
└──────────────────────┘                     │    │  沙盘 game_bridge:            │
                                       沙盘独立回退:  │     parse_*_to_world_state()  │
                                       游戏直连 :52174  │          │                │
                                                       │          ▼                │
                                                       │   WorldState（只读镜像）    │
                                                       └──────────────────────────┘
                                                       ┌─ 决策层 ──────────────────┐
                                                       │  生存 / AI / 自动          │
                                                       │  读 WorldState → 提案动作   │
                                                       └──────────┬───────────────┘
                                                                  ▼
                                                       ┌─ 调度层 ──────────────────┐
                                                       │  _allocate_actions()       │
                                                       │  冲突裁决：生存>AI>自动     │
                                                       └──────────┬───────────────┘
                                                                  ▼
                                                       ┌─ 执行层 ──────────────────┐
                           元层（人操）─┐               │                            │
                          Maintenance  │               │  Action_Command.signal      │
                          _Command     │               │   + Action_Command.json     │
                              │        │               │                            │
                              ▼        │               │  Maintenance_Command       │
                       Maintenance_Command.signal     │  .signal + .json            │
                              │        │               └──────────┬───────────────┘
                              │        │                          │
                              ▼        │                          ▼
游戏端 ←──────────────────────────────────────────────────────────┘
mythica_action._check_action_commands_quick()
  └─ _execute_one_action() → sim 执行 walk/interact/idle
mythica_maintenance._check_maintenance_commands()
  └─ _execute_set_funds / _execute_set_motive / ... → 修改世界状态
```

### 1.1 动作插入全链路（六阶段，Tier 0→6）

```
┌─ Phase 0: 数据输入（每 3s 自动）──────────────────────────────┐
│  游戏快照 → game_bridge → WorldState                           │
│    ├→ observer.observe(ws)          积累实证 affordance         │
│    └→ action_tracker.on_snapshot()  追踪执行中的动作            │
├─ Phase 1: AI 决策 ───────────────────────────────────────────┤
│  Tier 4:  build_push_context(ws) → PushContext                 │
│  Tier 1-6: generate_action_catalog(ws, ctx)                   │
│            五层 hints 管线 × 每条动作 → 200+ ActionOption       │
│  AI Call 1: 内心声音 (1-3 句)                                  │
│  AI Call 2: JSON 动作选择 → Phase B 意图映射                    │
│  硬去重(3轮窗口) + 自动补 goto(不同房间 prepend walk)           │
├─ Phase 2: 预推验证 Tier 3 ───────────────────────────────────┤
│  validate_actions() → 14 道门 (G0-G13)                        │
│    passed → 发送 | deferred → 延迟队列(3x/120s) | blocked → 丢弃│
├─ Phase 3: 发送 Tier 5 ───────────────────────────────────────┤
│  command_sender → Action_Command.signal + .json (_av=2)       │
│  action_tracker.on_actions_sent()   记录 baseline              │
├─ Phase 4: 游戏端执行 ────────────────────────────────────────┤
│  轮询 → TTL 保鲜(300s) → _ACTION_ROUTES 路由:                 │
│    push: 三路候选(hints/默认/盲选) + 逐候选重试(cap 8)         │
│    goto: 逐候选重试(兜底链) + 物品偏移 1m + 目标 rs            │
│    stop: queue.cancel_all + si USER_CANCEL                    │
│  → /action_result HTTP 回传                                    │
├─ Phase 5: 结果闭环（2026-07-26 升级：5 个反馈通道）──────────┤
│  push_history.record()  → {rejection_awareness}/{effective_approach} │
│  action_tracker: 交叉验证 → verdict → {last_cycle_outcome}      │
│  🆕 hints 质量检测: 推入非预期→verdict_detail 标注              │
│  🆕 效果确认: effect_baseline vs 当前快照 → 🎯效果确认          │
│  🆕 suggest_rule_fixes(): 连续拒→建议禁用/移除hints             │
│  🆕 observer→GUI: 全覆盖→🔔 N 条可标 verified                  │
│  🆕 rule_health_report(): 每 5 轮三维健康分                    │
│  🆕 evidence 读者: action_evidence.json → hints 建议            │
└──────────────────────────────────────────────────────────────┘
```

**模块分工（18 文件，按 Tier 排列）：**

| 模块 | 行数 | Tier | 职责 |
|------|:---:|:---:|------|
| `action_catalog.py` | 1136 | 1-2 | 九种动作类型 + 五层 hints 管线 + 紧凑格式渲染 |
| `custom_actions/` | 500 | 0 | 声明式规则，verified 布尔流转，加一行=加一个动作 |
| `autonomous_observer.py` | ~580 | 3a,4 | 读→写闭环：观察→实证索引→三级排序+追加；🆕 observer→GUI 回调 + push验证回写 |
| `probe_data.py` | 300 | 3b-3c | 探针静态数据：地面/水体/电话 + 情境社交 + 关键词→hints |
| `display_name_resolver.py` | 80 | — | 显示名→EA类名 三层回退单一入口 |
| `catalog_context.py` | 115 | 4 | PushContext 构建（玩家控制/特殊状态/动机/浪漫分/NPC） |
| `engine.py` | ~776 | — | 两段式 AI + 跨轮上下文 + 硬去重 + 自动补 goto；🆕 自动动作评分+分配+去重 |
| `push_gate.py` | ~407 | 3 | 14 道预推验证门 + DeferredActionQueue；🆕 自动动作跳过 G3 |
| `push_history.py` | ~380 | 2 | 推入日志 + 有效 hints 缓存 + AI 反馈文本；🆕 suggest_rule_fixes |
| `action_lifecycle.py` | ~1300 | 5 | 完整生命周期追踪 + 交叉验证 + verdict；🆕 hints质量检测 + 效果确认 + evidence读者 + health_report |
| `command_sender.py` | ~155 | 5 | 命令发送（动作命令 + 🆕 维护命令双通道） |
| `skill_catalog.py` | 200 | 6 | 技能分级 hints（等级够→高级 hints） |
| `game_bridge.py` | 392 | — | 双通道数据读写 + emit Action_Command |
| `world_state.py` | 106 | — | WorldState / CharacterState / SceneInfo / SceneObject |
| `settings.py` | ~200 | — | 沙盘设置 + 🆕 角色优先级 + 关系护栏配置 |
| `character_status_panel.py` | ~780 | — | 人物状态全览（15 折叠区）；🆕 关系编辑 + 需求回满 + 标签管理 + buff 增/删交互 |
| `custom_actions/` | 132 规则 | 0 | 🆕 7 子模块拆分（objects/repair/social/romance/motive/skill/ww），`__init__.py` 聚合 |
| `_rules_legacy.py` | 19 | — | 🆕 薄 re-export，后向兼容（原 1647 行单体已拆分） |
| `app.py` | ~930 | — | GUI + Tier 3-5 管线编排；🆕 自动动作UI + 清扫开关(T2 多品类 sweep) + 探针展示 + 角色优先级面板 |
| `action_panel.py` | 470 | — | 手动测试页（驱动小人 + 候选浏览 + 结果直显） |

**五层 hints 管线（每个动作独立跑）：**
```
rule.hints (手写前缀) → SceneObject.affordance_names (快车,_sv=17)
 → _is_noise_affordance() (噪音过滤) → observer.enrich_hints() (3a,实证追加)
 → probe_data.enrich_from_probes() (3b,注册表候选) → observer.sort_by_proven() (4,实证排序)
 → skill_catalog.select_hints() (Tier 6,技能分级)
```

**关键数字：** 55 条自定义规则(44 verified) · 9 条需求驱动 · 14 道预推验证门 · 54,625 条实证映射 · 1,825 tests · 8 个反馈闭环 · 🆕 10 个维护探针 · 关系护栏三层干预 · 需求/Buff 直控

---

## 2. 模块结构

```
mythica_sandbox/
├── app.py                ← 入口 + GUI（CTkTabview：沙盘 / 动作测试 / 人物状态 / 自动动作，底部日志共用）（~930 行）
├── action_panel.py       ← 🆕 v0.8.0 动作测试页：手动点选驱动小人 + affordances_index；v0.8.2 统一分组列表（一次全显+类型过滤+🔍搜索开关）
│                            静态目录首次程序化解析（53k 条，mtime 缓存 + 过滤 +
│                            hints-only 精确推送，游戏端零改动）（~470 行）
├── character_status_panel.py ← 🆕 人物状态全览（左列表右数据，15 折叠区，buff/需求/技能/关系/学位/事业，
│                               数据指纹缓存防闪烁）；🆕 关系编辑弹窗 + 需求回满 + 标签管理（~740 行）
├── engine.py             ← 两段式 AI 核心循环 + 🆕 自动动作评分分配 + 合并去重（~776 行）
├── world_state.py        ← 数据模型：WorldState / CharacterState / SceneInfo（~106 行）
├── action_catalog.py     ← 动态动作目录生成（通用类型 push/goto + target_kind）
├── autonomous_observer.py ← 🆕 2026-07-25 自主动作观察器——从快照积累游戏自主使用的
│                             affordance 实证，反馈给动作目录排序 hints（~580 行）
├── probe_data.py          ← 🆕 2026-07-25 探针静态数据查询层——三段式 hints pipeline 的
│                             探针富化层：地面/水体/电话 hints + 情境社交 + 关键词→hints 映射（~300 行）
├── display_name_resolver.py ← 🆕 2026-07-25 显示名→EA类名解析器——三层回退单一入口
│                             （known_mappings→affordance_names.json→算法）（~80 行）
├── data/
│   └── affordance_names.json ← 🆕 2026-07-25 预构建稳固映射（54,625 条 EA类名→显示名，
│                               scripts/build_name_map.py 提取，随代码发布，零运行时依赖）
├── custom_actions/        ← 🆕 v0.10.0 声明式自定义动作表（拆分包，2026-07-23）
│   │                        verified 布尔升格进 AI 目录，游戏端零改动
│   │                        🆕 rule_schema.py +clear_queue +auto_trigger +mood_requires +location_prefer +needs_goto +preceding_actions +estimated_duration_s
│   └── rules_observed.py ← 🆕 v0.11.0 Observer 自动发现的候选规则（JSON持久化）
├── prompts_sandbox.py    ← 2 个 prompt 模板（~63 行）
├── game_bridge.py        ← 游戏通信：双通道读（探针+快照）+ 写命令信号（~392 行）
├── settings.py           ← 沙盘设置（复用 Mythica_Settings.json API profile）；🆕 角色优先级 + 关系护栏配置（~200 行）
├── server.py             ← 数据连接：ProbeHub 订阅 + 独立 HTTP server 回退（~400 行）
├── command_sender.py     ← 🆕 命令发送器：动作命令 + 🆕 维护命令双通道（~155 行）
├── push_gate.py          ← 🆕 14道预推验证门 + DeferredActionQueue（Tier 3）；🆕 G3 跳过自动动作（~407 行）
├── push_history.py       ← 🆕 推入历史追踪器 + 状态delta确认 + suggest_rule_fixes（Tier 2+5，~380 行）
├── catalog_context.py    ← 🆕 PushContext 上下文构建器（Tier 4，~115 行）
├── skill_catalog.py      ← 🆕 技能分级hints目录（Tier 6，~200 行）
├── action_lifecycle.py   ← 🆕 动作生命周期追踪 + 交叉验证 + 效果确认 + evidence读者（~1300 行）
├── observer_schema.py    ← 🆕 v0.11.0 ObservedAction dataclass + 转换函数 + 语义分类（~540 行）
├── observer_to_rules.py  ← 🆕 v0.11.0 Observer → Rule 自动生成管道（~430 行，🆕 v0.14.2 +消费5项+mood/location/exclude/goto/冷启动）
├── error_log.py          ← 统一错误日志：写 txt + 轮转 + 自保护（~108 行）
├── run.py                ← 入口脚本（双击运行，~13 行）
├── docs/
│   ├── function-index.md ← 函数索引
│   ├── sandbox_action_insertion.md ← 五层 hints 管线 + 故障诊断
│   └── action_recording_design.md  ← 动作实证系统设计
├── ARCHITECTURE.md       ← 本文档
├── CLAUDE.md             ← 开发规范
└── CHANGELOG.md          ← 变更记录

mythica_lib/ (共享库 — 沙盘相关)
├── probe_hub.py          ← 🆕 探针数据中枢：发布/订阅 + 扇出（~250 行）
├── ai.py                 ← AI API 调用统一入口
├── signal_protocol.py    ← 桌面→游戏信号文件协议；🆕 +2 维护命令常量
└── ... (其他共享模块)

游戏端（../自制mod/）— 沙盘相关
├── mythica_action.py     ← 动作命令执行器（通用 push/goto/stop 路由）
├── mythica_maintenance.py ← 🆕 2026-07-27 维护命令执行器（探针 + 需求/Buff/关系/物品销毁，~700 行）
├── mythica_collect.py    ← 数据采集（快照 + 探针）
├── mythica_probe.py      ← 探针命令定义 + SimBundle
├── mythica_network.py    ← HTTP 发送队列（探针/快照/action_result/maintenance_result）
├── mythica_records.py    ← 信号文件路径唯一定义点
└── my_script.py          ← mod 入口；🆕 +3 hook 点（维护命令 + 探针调度 + 护栏清扫）
```

### 依赖方向（单向，无循环）

```
mythica_lib/ (共享库)
    ↑
    │  所有 sandbox 模块只依赖 mythica_lib + Python stdlib
    │
mythica_sandbox/
    ├── error_log.py      ← 纯 stdlib（零项目内依赖，Layer 0）
    ├── settings.py       ← mythica_lib.config_constants, mythica_lib.config_paths
    ├── world_state.py    ← 纯 dataclass，无项目内依赖
    ├── display_name_resolver.py ← 纯 stdlib + json，零项目内依赖
    ├── prompts_sandbox.py ← 纯字符串常量，零依赖
    ├── server.py         ← mythica_lib.config_paths（可选）
    ├── probe_data.py     ← world_state（SceneObject），零项目内依赖
    ├── action_catalog.py ← world_state
    ├── game_bridge.py    ← mythica_lib.signal_protocol, world_state, action_catalog, server, error_log
    ├── engine.py         ← mythica_lib.ai, world_state, action_catalog, prompts_sandbox, settings, error_log
    ├── app.py            ← engine, world_state, action_catalog, game_bridge, settings, server, error_log
    └── run.py            ← app
```

---

## 3. 核心引擎 (`engine.py`)

### 两段式 AI 管道

```
WorldState ──→ AI Call 1 ──→ 内心声音 (1-3 句)
                  │
                  ├── 输入: scene_context（场景/资金/在场角色/路人/物品标注/事件流）
                  │         + POV 个人块（traits/mood/动作 + pov_status: 位置/着装/低需求/愿望/恐惧）
                  │         + relations_context（人际块：关系分/谱系称谓/心结/正在交谈）
                  │         + recent_cycles（同 POV 跨轮历史，v2）      [v0.6.0/v2]
                  ├── model: 有思考（thinking_disabled=False）
                  ├── timeout: 120s
                  └── 失败: 返回 EngineResult(success=False, error=...)
                             │
                             ▼
               AI Call 2 ──→ 选中动作 [ActionOption, ...]
                  │
                  ├── model: 有思考
                  ├── timeout: 120s
                  ├── 输入: 内心声音 + 物品块（含占用/损坏标注）+ 动作目录
                  │         + recent_actions（最近已执行动作，v2）
                  └── 解析: JSON → action_id 映射回 ActionOption
```

### 与旧三阶段的关键区别

| 维度 | 旧三阶段 | 新两段式 |
|------|---------|---------|
| API 调用 | 3-6 次 | **2 次** |
| 重试逻辑 | 逐缺重试 + 兜底链 | **无重试**（失败即返回 error） |
| 标记解析 | 8 种标记，正则提取 | **JSON 结构化输出** |
| 散文长度 | 无上限 | **1-3 句** |
| 状态持久化 | 完整接力链（history+baton+state） | **轻量跨轮上下文**（会话内 deque≤8：POV/内心/动作，v2 第一步，2026-07-17；重启即清不落盘） |

---

## 4. 数据模型 (`world_state.py`)

```
WorldState
├── characters: dict[str, CharacterState]   # sim_id → 角色状态
├── scene: SceneInfo                        # 场景信息
├── recent_events: list[InteractionEvent]   # 最近交互
├── household_name: str
└── created_at: str

CharacterState
├── sim_id, name, location, current_action, mood
├── age, gender, traits: list[str]
├── is_household_member: bool
└── body_state: str   # WW 上下文身体快照

SceneInfo
├── location, time_of_day, day_of_week, weather
└── present_sim_ids: list[str]

InteractionEvent
├── actor_name, target_name, action, category
└── timestamp

ActionOption
├── action_id, character_id, character_name
├── action_type: "push" | "goto" | "idle" | "stop"（通用原语，2026-07-17 _av=2；stop=清队 2026-07-18）
│                + legacy 别名 "walk" | "walk_obj" | "interact" | "interact_object"（游戏端路由行永久保留）
├── target_kind: "sim" | "object" | ""(idle/stop)  # 通用类型必填，进 payload——游戏端据此统一解析目标
├── description, target_id, target_name
├── reason: str  # AI 选择此动作的理由（仅选中的动作有值）
├── tone: str  # 社交语气（"" / "romance"）——桌面端概念，不进 payload
├── affordance_hints: list[str]  # 精确交互类名（优先级序）=显式兜底链；空=游戏端默认路径
├── prompt_label: str  # 选择器 prompt 差异化标签（💕/🍽/😴/🛁/💃/📍/🏃…），空回退类型路由表
└── clear_queue: bool  # 插队标志（2026-07-18）：推之前清空该 sim 现有交互；面板开关置位，AI 目录恒 False
```

---

## 5. 动作目录 (`action_catalog.py`) — 五层 hints 管线（快车 + 观察器 + 探针富化 + 情境社交 + 技能分级）

> **姊妹文档**：
> - [`docs/sandbox_action_insertion.md`](docs/sandbox_action_insertion.md)（五层 hints 管线 / 探针预检 / 故障诊断）
> - [`docs/action_recording_design.md`](docs/action_recording_design.md)（动作记录系统：Pie Menu + Observer + 索引交叉验证）
> — 加动作前先读这两份，再读本 ARCHITECTURE。

动作目录是沙盘 AI 决策的核心——从 WorldState 动态生成可选动作列表，供 AI 选择后发射到游戏端执行。2026-07-25 重构后，hints 不再依赖静态 grep，改为**五层管线**（Layer 0 探针预检 → Layer 1 手写 → Layer 2 快车 → Layer 3a 观察器追加 → Layer 3b 探针富化 probe_data → Layer 3c 情境社交 → Layer 4 观察器排序 → 技能分级 Tier 6）。

```
自定义规则 target_match → 匹配场景物品 → 物品的 affordance_names（快车，_sv=17）
    → _is_noise_affordance() 过滤噪音
    → observer.enrich_hints() 追加游戏实证过的同物品 affordance
    → probe_data.enrich_from_probes() 追加注册表有但还没实证过的候选
    → observer.sort_by_proven() 按游戏实证频次排序
    → 手写 hints（优先级前缀）+ 快车 hints（兜底）→ 最终 hints 列表
```

### 5.1 五层 hints 来源（优先级递减）

| 层 | 来源 | 说明 |
|---|---|---|
| 1 手写 | `rule.hints` | 规则作者查证的精确类名——优先级最高，排 hints 最前面 |
| 2 快车 | `SceneObject.affordance_names` | 物品自带的完整 affordance 列表——游戏端 `_sv=17` 时发送。不同型号物品自动覆盖不同交互 |
| 3a 观察器追加 | `observer.enrich_hints()` | 追加已被游戏实证过、且匹配 `target_match` 关键词的 affordance 到尾部（去重） |
| 3b 探针富化 | `probe_data.enrich_from_probes()` | 追加注册表里有但还没实证过的候选——地面/水体/电话/物品关键词 |
| 3c 情境社交 | `probe_data.get_social_enrichment()` | 场景关键词（水池/篝火/电脑…）→ 上下文社交 hints |
| 4 观察器排序 | `observer.sort_by_proven()` | 整体按游戏实证频次排序——被游戏用过的排最前面 |
| 5 技能分级 | `skill_catalog.select_hints()` | 技能等级满足门槛时替换为高级 hints（Tier 6） |

**`hints=()` 留空规则**：快车 + 观察器 + 探针富化全自动填充。烹饪 4 条规则零手写 hints 实测通过。

**显示名→EA类名解析**：observer 在采集点 `_resolve_action_name()` 调用 `display_name_resolver.resolve_with_fallback()`——三层回退（known_mappings 56 对 → `affordance_names.json` 54,625 条 → 算法兜底）。observer 存储即 EA 类名，`is_proven`/`enrich_hints`/`sort_by_proven` 直接命中。

### 5.2 噪音过滤器（`_is_noise_affordance`）

物品的 `affordance_names` 包含大量姿态/系统/调试类 affordance（如 `sim-stand`、`debug_*`、`si_*`），推入没有叙事意义。过滤器与游戏端 `_OBJECT_AFF_SKIP_PATTERNS` 对齐：

```
过滤前缀: debug_, cheat_, sim-stand, stand_passive, sit_passive
过滤子串: si_, superaff, mixer, replacebroken, repair, salvage,
          autonom, proxy, invisible, createtray, turnoff
```

**血案**：真鳕选画画，快车 hints 中 `sim-stand` 排在 `easel_PracticePainting` 前面→推了站立。加过滤器后根治。

### 5.3 自主动作观察器（`autonomous_observer.py`）

**问题**：读端（每 3s 快照中 sim 的 `current_action` = 游戏自主选择的精确 affordance）和写端（通过 hints 推 affordance）之间没有反馈闭环。游戏自主执行了成百上千次 affordance，每一次都是"此 affordance 有效"的实证——但这些数据从未被收集和复用。

**方案**：`AutonomousObserver` 单例，每轮 WorldState 刷新后扫描所有 sim 的 `current_action`，去重累加计数。持久化到 `sandbox_logs/proven_affordances.json`（60s 节流）。

**API**：
| 方法 | 用途 |
|------|------|
| `observer.observe(ws)` | 每轮快照后调用，累加实证 |
| `observer.is_proven(hint)` | 查询是否被游戏自主用过 |
| `observer.sort_by_proven(hints)` | 实证的排前面（稳定排序） |
| `observer.enrich_hints(hints, keywords)` | 追加匹配关键词的实证 affordance |

**在动作目录中的消费位置**：
- `build_option_from_rule()`：`sort_by_proven()` 对所有 hints 排序
- 泛化物品使用：物品 `affordance_names` 按 `sort_by_proven()` 排序后取 filtered
- `enrich_hints()`：在自定义规则 hints 尾部追加同物品实证 affordance（兜底）

**数据流闭环**：
```
游戏端快照（每 3s）
  → parse_scene_snapshot_to_world_state()
  → observer.observe(ws)          ← 积累实证
  → generate_action_catalog(ws)
      → build_option_from_rule()  → observer.sort_by_proven(hints)
      → 泛化物品 hints           → observer.sort_by_proven(filtered)
  → AI 选择 → push → 游戏执行 → 回传结果
  → 下轮快照 → observer 看到新实证 → 排序更新
```

### 5.4 动态生成规则

1. **社交 — 友好**: 每个在场角色 → 每个其他在场角色（游戏端默认候选名单）
2. **社交 — 亲密**: 双方 Teen+，按浪漫分三档选 hints（低>0→轻撩 / ≥30→亲吻牵手 / ≥60→深吻拥抱），cap 3/人
3. **停止**: 每个角色一条——清空交互队列
4. **需求动作**: motive < 阈值时按 `_MOTIVE_OBJECT_RULES` 生成（饥饿→冰箱 / 困→床 / 膀胱→马桶 / 舒适→沙发 / 卫生→淋浴 / 社交→电脑 / 娱乐→音响/电脑，9 条），按 motive 升序 cap 3/人
5. **使用物品**: 场景物品类别多样化挑选 ≤5 件——快车 hints + 观察器排序
6. **走到物品旁**: 仅异房间物品，cap 2/人
7. **去某房间**: 有 `room_name` 时按房间聚组，每条 `🚶 去{房间名}`
8. **自定义动作**: `verified=True` 规则行，`build_option_from_rule` 构造（快车 + 观察器）
9. **物品栏动作**: 角色背包物品匹配自定义规则 `target_match`

### 5.5 格式化为 AI prompt

按语义分组（social/romance/use_object/need/walk/inventory/stop），紧凑格式：

```
【社交 — 友好】
  斑 → 真鳕, 柱间, 扉间

【社交 — 亲密】
  斑 → 真鳕

【物品】
  🔧 使用 → 冰箱, 电脑, 画架, 音响, 书架
  （所有在场角色可用）

【需求动作】
  斑: 🍽 去吃东西, 😴 去睡觉

【移动到物品旁】
  斑 → 钢琴, 跑步机

【随身物品】
  管家: 🧺 收脏衣服（背包中的Laundry）

【控制】
  🛑 停下手头的事: 斑, 真鳕, 柱间, 扉间
```

---

## 6. 游戏通信 (`game_bridge.py`)

### 双通道数据源（v0.1.3）+ ProbeHub HTTP Forward 订阅（v0.2.1）

沙盘**始终启动自己的 HTTP server**（`:52174`），通过两个独立通道接收数据：

```
通道 A — 场景快照（每 3 秒，游戏端主动推送）
  → ProbeHub (:52173) → HTTP Forward → :52174/_hub_forward
  → 或 游戏端直连 :52174/scene_snapshot
  [沙盘端] _handle_hub_forward → _handle_scene_snapshot(payload) → 存共享内存 → GUI 刷新
  数据 [_sv=13]: present_sims[]/present_npcs[]（SimBundle 42 字段全量 + pairwise 关系）,
        场景（时间 time_hm/天气/地点/活动/节日/月相/资金/账单）, 物品 top50（含占用/状态/房间）
  解析时顺带: _derive_state_events()——与上轮快照差分 → 到场离场/关系Δ/需求趋势/buff/心情
        五类状态事件（category="state"）推入事件缓冲，与交互事件同流入 prompt/GUI/dump [v0.6.0]

通道 B — 交互探针（事件触发，游戏端被动推送）
  → ProbeHub (:52173) → HTTP Forward → :52174/_hub_forward
  → 或 游戏端直连 :52174/queue_probe
  [沙盘端] _handle_hub_forward → _handle_probe(payload) → 存共享内存 → 落盘 → GUI 刷新
  数据: actor/target 详细信息, action, stage, social_context, sex_ctx
```

**连接模式（`connect_to_hub_or_standalone()`）：**
1. 始终启动沙盘自己的 HTTP server（`:52174`）
2. TCP 快速检测中枢 `:52173` 是否可达（0.5s 超时）
3. 可达 → 向中枢 POST `/_subscribe_forward` 注册回调 URL `http://127.0.0.1:52174/_hub_forward`（"hub" 模式）
4. 不可达或注册失败 → 纯游戏直连（"standalone" 模式）

**设计原则:** 瘦游戏端 / 胖桌面端。游戏端只抓数据+发 HTTP，不设开关——数据通道随 mod 加载自动启动。桌面端/沙盘端自行决定是否处理。

**v0.2.0→v0.2.1 关键修正：** v0.2.0 尝试通过 `get_hub().subscribe()` 跨进程订阅——但 `ProbeHub` 单例在沙盘进程中是未启动的空壳。v0.2.1 改为 HTTP Forward 订阅：中枢扇出时 POST 到沙盘的 `/_hub_forward` 端点，跨进程通道正确建立。

### 读方向
```
沙盘启动 → connect_to_hub_or_standalone()
  ├─ Step 1: 始终 start_server(52174) —— 沙盘自己的 HTTP server
  │
  ├─ Step 2: 中枢可达？
  │   ├─ 是 → _register_with_hub(port, hub_port)
  │   │        POST /_subscribe_forward {forward_url: "http://127.0.0.1:52174/_hub_forward"}
  │   │        → 中枢 _fan_out() 时异步 POST 到 :52174/_hub_forward
  │   │        → _handle_hub_forward 按 X-ProbeHub-Route 头分发到内部 handler
  │   │        → 数据通道：游戏→中枢→Forward→沙盘 ✅
  │   │
  │   └─ 否 → 游戏端 target_ports = [52174, 52173]
  │              → 游戏直连 :52174
  │              → 数据通道：游戏→沙盘 ✅
  │
  └─ 两个通道互为回退，同时可用也不冲突（_handle_probe 幂等）
```

### 写方向
```
沙盘 → emit("Action_Command", payload) → Action_Command.signal + .json
     → 游戏端 _check_action_commands_quick() 轮询消费 → 每动作执行后
     → HTTP POST /action_result 回传结果（探针发送队列广播 [52174, 52173]）
     → 沙盘 server._handle_action_result（result_id 去重）→ 动作测试页直显
```

**信号协议**（复用 `mythica_lib.signal_protocol.py`，`_av=2` 2026-07-17 通用执行器）：
```json
{
  "_av": 2,
  "actions": [
    {
      "action_id": "need_hunger_123_obj1",
      "character_id": "123",
      "character_name": "斑",
      "action_type": "push",
      "target_kind": "object",
      "target_id": "obj1",
      "target_name": "冰箱",
      "description": "斑 饿了，去冰箱拿点吃的",
      "affordance_hints": ["fridge_GrabSnackAutonomously", "fridge_GrabSnack"]
    }
  ],
  "inner_voice": "有点饿了，去厨房看看有什么吃的...",
  "created_at": "2026-07-13 14:30:00"
}
```
> **通用执行原语（_av=2，2026-07-17）**：`action_type` 收敛为 `push`（affordance 推送）/
> `goto`（地形移动）/ `idle` + 显式 `target_kind`（"sim"|"object"）。游戏端
> `_ACTION_ROUTES = {action_type: (kind默认, handler)}`——legacy 4 别名
> （interact/interact_object/walk/walk_obj）永久保留映射到同两个通用 handler。
> **新增动作类型只改沙盘端**（custom_actions.py 加一行），游戏端零改动零部署。
> mixer（super=N）仍不可推——通用执行器的已知边界。
> **affordance_hints**：精确交互类名（优先级序）=**显式兜底链**，由沙盘端决定推什么、
> 游戏端只解析执行（瘦游戏端）。push/goto 均支持——有 hints 时只用 hints
> 构建候选，**全拒/未命中不降级默认名单/盲选**（意图保真：想亲吻绝不能变成击掌、
> "拿吃的"绝不能变成洗冰箱），仅通知+落盘+回传 no_affordance。空 `[]` = 游戏端
> 现有默认路径（后向兼容，旧 mod 收到 hints 也只是忽略）。goto hints 可指定步态
> （如 `["terrain-jog", "terrain-gohere"]` 跑步+走路兜底）。
> **hints 是"被拒兜底链"不是"取类兜底链"（2026-07-18 v0.10.2 修复）**：goto 与
> push 一样逐候选重试——前一候选被游戏拒绝后自动推下一个。旧版 goto 只取首个
> 可解析的类名，terrain-jog 类存在但推不动（参与者约束），gohere 兜底从未运行。
> **Action_Command TTL（2026-07-18）**：游戏端消费时校验 created_at，超
> `_ACTION_COMMAND_TTL_SECONDS`（300s）整包丢弃——落盘+游戏内通知+逐动作回传
> error。防游戏退出期间发射的指令在下次开游戏时以失效语境执行。沙盘端配套：
> `server.get_seconds_since_last_data()` 游戏离线检测，面板/AI 循环发射后
> >15s/>30s 无数据即明确提示，不再停在"等待回传"。
> tone/prompt_label 是桌面端概念，不进 payload。
> **执行结果回传（/action_result，_rv=1，2026-07-17）**：每动作执行后游戏端回传
> status（pushed|all_rejected|no_affordance|not_found|error）+ attempts 逐候选拒因
> （EnqueueResult 可读文本）。**pushed=guaranteed 入队≠已执行**。schema 见
> `docs/api-protocol.md` §2.2；沙盘按 `result_id` 去重（hub Forward+直连双投递）。
> walk 定向执行链（2026-07-16 实测验证）：游戏端 resolve target_id → 取目标 sim 的
> position + routing_surface → `TerrainPoint` + `PickInfo(PICK_TERRAIN, ...)` +
> `InteractionContext(pick=)` → `push(terrain-gohere, terrain_point, ctx)`。
> 目标不可寻路（非激活区域/离场中）时游戏返回 `Cannot GoHere! Unroutable area.`，记日志。
> **已知语义边界（2026-07-17 实测确认，非 bug）**：gohere 走的是"执行时刻的坐标点"——
> sim 行走途中目标再移动会扑空（与玩家点地面 Go Here 同级行为；EA 无通用"跟随某人"
> 交互，图书馆已查证）。扑空后下一轮 cycle AI 以新位置重新决策。刻意不做追踪式重推
> （需游戏端持久状态，违背瘦游戏端；除非实测扑空率影响叙事质量再考虑）。
> **walk_obj（goto+object，待实测）**：同配方，目的地坐标/routing_surface 取物品对象
> （api_library/types_index.txt 已静态确认 object_* 类型暴露 `.routing_surface`）；
> rs 读不到时兜底 actor rs 并落盘标注（v0.10.0 起该兜底推广到 sim 目标），
> 跨层由游戏 Unroutable 可读拒绝。

---

## 7. 与旧 app 的关系

```
共享（mythica_lib/）:
  probe_hub.py, ai.py, prompts_player.py, signal_protocol.py,
  mythica_counters.py, pipeline_parser.py, recorder.py,
  config_constants.py, config_paths.py, config_manager.py,
  config_quality.py, pipeline_accumulation.py

旧 app 专属（不共享）:
  ui.py + 13 个 ui_mixin_*.py, monitor.py, server.py,
  config_household.py, config_events_*.py, mythica_overlay.py,
  arc_scheduler.py, chat_share_card.py

沙盘专属（不共享）:
  app.py, engine.py, world_state.py, action_catalog.py,
  prompts_sandbox.py, game_bridge.py, settings.py,
  server.py, error_log.py, run.py
```

---

## 8. 游戏端改造

### 数据通道（v0.1.3 解耦后始终在线）

mod 加载即自动启动，不依赖 AI 总开关：

| 通道 | 间隔 | 端点 | `_sv` | 数据 |
|------|:---:|------|:---:|------|
| queue_probe | 1s | `/queue_probe` | 3 | 交互事件（actor+target+动作，每角色 40 前缀字段与快照全统一） |
| scene_snapshot | 3s | `/scene_snapshot` | 13 | 全场场景（sims+NPC+天气+时间+地点+物品） |

核心函数：`_collect_queue_probe_hits`、`_collect_scene_snapshot`、`_probe_sender_worker`

### SimBundle 统一数据源 + 源头清洗（🆕 2026-07-16，深夜清洗轮更新）

游戏端 sim 数据统一封装为 `SimBundle`（`mythica_probe.py`，42 字段——原 `ProbeSimBundle` 10 字段，
经统一(27)→v12 扩容(41)→v13 清洗轮(+special_stats)）。`_get_sim_bundle()` 是唯一采集入口，
采集后经 `mythica_clean.clean_sim_bundle_fields()` **源头去噪归一**（瘦游戏端正式例外，见主项目 ARCHITECTURE §7），
两条通道共用：

```
_get_sim_bundle(sim) → 采集 → clean_sim_bundle_fields() → SimBundle (42 字段，干净数据)
   ├─ queue_probe:     _build_probe_hit_dict → _CHAR_HIT_FIELDS（40 项映射表）→ actor_/target_ 前缀
   └─ scene_snapshot:  _collect_scene_snapshot → bundle.to_dict() + 成对（pairwise）字段
                                 │
                沙盘端 game_bridge：快照直通 / 探针 _deprefix() 去前缀
                        └→ _char_from_fields()——CharacterState 唯一构造点（三路径共用）
```

- 清洗内容：mod 名去噪（hash 尾缀/作者 token/叠词）、hidden_traits 技术 flag 过滤、inventory `[{name,count}]` 聚合、
  whims protobuf 残留防御、motives 拆出 `special_stats`（异刻度）、sentiments/social_group/family_relations ID→名字
- 协议版本联动：queue_probe `_sv` 2→3，scene_snapshot `_sv` 12→13（沙盘端校验常量同步）
- 渲染统一：`dump_world_state_to_file` per-char 段走 `CharacterState.to_detail(names)`；ID 缩写统一 `short_id()`（后 6 位）

### 新增 `mythica_action.py`（v0.10.0 通用执行器）

| 函数 | 职责 |
|------|------|
| `_check_action_commands_quick()` | 轮询 `Action_Command.signal`，消费并执行；created_at 超 TTL(300s) 整包丢弃+回传 |
| `_execute_one_action(action)` | 统一分发：路由查表 → target_kind 解析 → 目标解析 → handler → 结果回传 |
| `_push_generic_action(sim, target, kind, action)` | push 原语（hints/默认名单/盲选三路候选） |
| `_goto_generic_action(sim, target, kind, action)` | goto 原语（目标坐标+rs，actor-rs 兜底；逐候选重试 cap 8；object 目标落点朝 actor 偏移 1m） |
| `_offset_toward_actor(dest, sim, offset=1.0)` | 物品目标落点偏移（物品坐标在自身几何内部，精确点 gohere 必 Unroutable） |
| `_clear_sim_interactions(sim)` | 清队原语（stop 动作/clear_queue 插队共用）：queue.cancel_all + si USER_CANCEL，返回计数 |
| `_try_push_candidates(...)` | 单份候选重试循环（cap 8，attempts 记录） |
| `_resolve_goto_candidates(sim, hints)` | goto 候选解析（hints 全局精确取类，命中全保留供重试，不降级） |
| `_command_age_seconds(created_at)` | 指令年龄解析（TTL 判定；解析失败=None 视为新鲜） |
| `_build_result_base(action, kind)` / `_send_action_result(result)` | 结果构建 + 回传（`mythica_network.send_action_result`） |
| `_resolve_sim_info(sim_id_str)` | sim_id → sim_info 对象 |
| `_show_action_notification(name, desc)` | 游戏内通知弹窗 |
| `_write_action_log(ts, voice, actions)` | 动作执行日志 |

### 修改 `my_script.py`
- 在 `_new_trigger_start()` 中添加 `_check_action_commands_quick()` 调用

### 修改 `mythica_network.py`
- 公开 `send_action_result(payload)`（`__all__`）——经探针发送队列广播 `/action_result`
- `_probe_sender_worker` 4xx 只记日志不设冷却（防旧沙盘 404 拖累探针通道）

### 修改 `mythica_records.py`
- 新增 `get_action_command_signal_path()` / `get_action_command_json_path()`（唯一定义点，mythica_action 从此导入）

### 新增 `mythica_maintenance.py`（🆕 2026-07-27，~700 行）

统一维护命令执行器——游戏端接收沙盘发出的维护指令并执行。与 `mythica_action.py`（动作执行）对偶：前者管"世界维护"（清扫/修理/关系修正），后者管"sim 行为"（社交/移动/使用物品）。

| 函数 | 职责 |
|------|------|
| `_check_maintenance_commands()` | 轮询 `Maintenance_Command.signal`，消费并执行 |
| `_execute_maintenance(command)` | 统一分发：路由查表 → 参数解析 → handler → 结果回传 |
| `_cmd_set_motive(sim, motive, value)` | 直设 sim 需求值（`commodity_tracker.set_value`） |
| `_cmd_add_buff(sim, buff_name)` / `_cmd_remove_buff(sim, buff_name)` | 增/删 buff |
| `_cmd_set_relation(sim_id, target_id, friendship, romance)` | 直设关系分数 |
| `_cmd_add_relation_bit(sim_id, target_id, bit_name)` / `_cmd_remove_relation_bit(...)` | 增/删关系标签 |
| `_cmd_destroy_object(object_id)` | 销毁场景物品（收垃圾/洗碗等维护动作的最终执行） |
| `_probe_puddles()` / `_probe_trash()` / `_probe_dishes()` 等 | 10 个探针函数——扫描场景中特定类型的物品 |

**设计原则**：
- 维护命令走独立信号文件（`Maintenance_Command.signal`），与动作命令（`Action_Command.signal`）平行——互不阻塞
- 探针函数返回物品清单（含 `object_id` / `position` / `state`）。物品扫描采用统一的两段式模式：type 关键词优先 → state 值兜底。沙盘决策后发维护命令执行
- `mythica_network.py` 公开 `send_maintenance_result()`（经探针发送队列广播 `/maintenance_result`）
- 协议常量：`signal_protocol.py` +2（`MAINTENANCE_COMMAND_SIGNAL` / `MAINTENANCE_COMMAND_JSON`）

#### 探针修复历程（2026-07-27 T2）

首轮 10 探针投递中 4 个返回 0。根因：物品类型名（`definition.name`）和物品状态（`state_component`）是两个独立维度，单纯按类型名扫会漏掉所有"状态驱动型"物品。`dirty_object` 开先例（0→10），本轮推广到全品类：

| 探针 | 修复前 | 修复后 | 修复方式 |
|------|:---:|:---:|------|
| `broken_object` | 0 | 10 | `_find_broken_objects` 改为两段式：`is_broken` → `_is_really_broken()` state 兜底（排除 "unbroken"） |
| `trash_pile` | 0 | 7 | 新增 `_find_trash_pile()`：type 关键词 → state trash/garbage 兜底，排除 fixture 关键词（washer/dryer 等） |
| `laundry_pile` | 0 | 10 | 新增 `_find_laundry_pile()`：type 关键词 → state laundry/clothing 兜底，排除 fixture 关键词 |
| `spoiled_food` | 0 | 待实测 | 新增 `_find_spoiled_food()`：`is_spoiled` 捷径 + `_is_state_spoiled()` state 分类器 |

**血案**：laundry 首版 state 兜底过宽——洗衣机/烘干机自身的 laundry 状态值被误匹配，10 台机器被 destroy。修复：state 回退前先排除 `wash`/`dry`/`washer`/`dryer`/`machine` 等 fixture 关键词。

**通用模式**：当物品类型名不可靠时，用 state 值作为第二层兜底。消耗品（碗碟/水坑/垃圾堆/脏衣服）走 destroy；固定设施（柜台/水槽/电器）跳过，交给 T1 自动动作处理。

### 修改 `my_script.py`（🆕 2026-07-27）
- 在 `_new_trigger_start()` 中新增 3 个 hook 点：
  - `_check_maintenance_commands()` — 维护命令轮询（与动作命令平行）
  - 维护探针调度 — 按需触发场景扫描
  - 护栏清扫 — 关系分数的定时校对

---

## 9. 探针系统与数据全景

> **姊妹文档**：[`docs/probe-commands-reference.md`](../docs/probe-commands-reference.md)（18 探针完整手册，含参数/输出格式/典型场景）
> 本节聚焦：探针做什么 → 产出什么数据 → 沙盘怎么用 → 对动作插入有什么帮助。

### 9.1 总览：五层 18 探针

游戏端共 18 个探针命令（游戏控制台 `Ctrl+Shift+C` 输入），按用途分五层。输出全部在 `MythicaData/`。

| 层 | 用途 | 探针数 | 核心原则 | 沙盘消费方式 |
|---|------|:---:|------|------|
| 第 1 层 — 图书馆 | 扫全游戏对象 → 字典/目录，桌面端 `grep` 可查 | 3 | "查字典" | **手动**：加规则前 grep 查证 |
| 第 2 层 — 定向扫描 | 定点深挖特定对象/属性的存储位置 | 7 | "找位置" | **手动**：图书馆查不到时深挖 |
| 第 3 层 — 状态差分 | 改变游戏状态前后对比 → 变化自己浮出 | 2 | "看变化" | **手动**：字段名猜不到时用 |
| 第 4 层 — 行为实测 | 真实调用 API 验证"存在≠能用" | 1 | "验用法" | **手动**：最终确认 |
| 运行时采集 | 场景物品/社交/系统交互的**当前场景**快照 | 4 | "现在有什么" | **手动**：确认当前场景可用性 |
| 验证型 | SimBundle 全字段逐项验证 | 1 | "字段非空" | **手动**：部署前检查 |

> **关键区分**：探针是**手动工具**（在控制台输入命令），快照是**自动管道**（每 3s 推送到沙盘）。沙盘 GUI 的实时数据显示来自快照，不是探针。探针用来**查证、诊断、发现**——跑一次就够了。

### 9.2 各探针详解

#### 第 1 层：图书馆 — 字典/目录（3 个）

| 命令 | 做什么 | 输出文件 | 大小 | 沙盘关键用途 |
|------|------|------|:---:|------|
| `ai_probe_library` | 扫全游戏 Python 对象（32 根起点），按类型去重记录属性名/类型/样本值 | `api_library/types_index.txt` | ~3.5MB | **"某个数据存哪个属性"**——如想知道 `CareerTracker` 有哪些字段 → grep |
| `ai_probe_affordances` | 从 `affordance_manager.types` 全量扫描所有交互 class（53K+） | `api_library/affordances_index.txt` | ~9.5MB | **"这个交互能不能 push"**——super=Y/auto=Y/user=Y/tgt=/ages=/guid。**加动作规则的第一步** |
| `ai_probe_tree [depth] [all]` | 从根对象 DFS 遍历输出缩进树状图（~3600 节点） | `api_library/structure_tree.txt` | ~250KB | **"数据从根到叶的完整路径"**——如 `sim_info → _career_tracker → _careers` |

#### 第 2 层：定向扫描 — 定位数据位置（7 个）

| 命令 | 做什么 | 输出 | 典型场景 |
|------|------|------|------|
| `ai_probe_deep <root> <kw> [depth]` | BFS 遍历对象属性图，按关键词过滤 | `Deep_Probe.txt` | "WW 性数据存哪" → `ai_probe_deep sim_info sex 5` |
| `ai_probe_sim_api` | 内省当前主控 sim 的 API 表面（11 节手写） | `Sim_API_Probe.txt` | 查 sim 对象上可用的方法/属性列表 |
| `ai_probe_sim_data` | 当前主控 sim 的全量数据 dump（26 章节） | `Sim_Data_Probe.txt` | **一次看全一个 sim 的所有数据**（比快照更详细） |
| `ai_probe_zone` | 内省 zone/lot/world 的属性名和值 | `Zone_Probe.txt` | 查地块/世界级数据 |
| `ai_probe_occult` | 探测 9 种超自然类型 tracker 状态 | `Occult_Probe.txt` | 吸血鬼/魔法师/人鱼/狼人等状态 |
| `ai_probe_pick` | 探测 PickInfo/PickType 签名 + live pick 捕获 | `Pick_Probe.txt` | **加 walk 类动作前必跑**——确认 pick 通路 |
| `ai_probe_aff_detail <name>` | 深读单个交互 tuning 全属性面 + test_globals 全文 | `Aff_Detail_Probe.txt` | 图书馆只给摘要，这个看完整门控条件 |

#### 第 3 层：状态差分 — 无法定位时用（2 个）

| 命令 | 做什么 | 典型场景 |
|------|------|------|
| `ai_probe_diff <root> [depth]` | 跑两次——拍基线→改状态→输出 changed/added/removed | **字段名猜不到时**——换衣服前后对比，变化字段自己浮出 |
| `ai_probe_queue_diff` | 跑两次——拍全场交互队列基线→做动作→输出每 sim 新增/消失交互 | **"该推哪个 affordance"不再靠关键词猜**——新出现的类名可直接 push |

#### 第 4 层：行为实测（1 个）

| 命令 | 做什么 | 说明 |
|------|------|------|
| `ai_probe_push` | 真实调用 `push_super_affordance` + 观察游戏行为 | **API "存在" ≠ "能用"**——验证签名和实际效果 |

#### 运行时采集探针（4 个）— 场景感知

这些是**"当前场景中实际存在什么"**——不扫全量注册表，只看当前在场的：

| 命令 | 做什么 | 输出 | 耗时 |
|------|------|------|:---:|
| `ai_probe_item_interactions` | 五节：物品清单/物品affordance/地面affordance(1389条)/注册表/sim能力 | `Interaction_Probe_Items.txt` | ~2s |
| `ai_probe_social_interactions` | 按目标年龄过滤+分组：在场sim/社交affordance/关系分/统计 | `Interaction_Probe_Social.txt` | ~85s(后台) |
| `ai_probe_system_interactions` | 手机(1088条)/日历(11条活动)/笔记本/俱乐部 | `Interaction_Probe_System.txt` | ~1s |
| `ai_probe_siminfo` | Sim 面板补全：职业详情/拥有事业/家庭谱系/生活方式/学位/俱乐部 | `SimInfo_Dump.txt` | <1s |

#### 验证型探针（1 个）

| 命令 | 做什么 | 说明 |
|------|------|------|
| `ai_probe_bundle [sim名/id]` | 内省 SimBundle 全部字段（42+），实跑采集逐项标 `[OK]`/`[空]` | **加字段自动进验证——不用写新探针** |

### 9.3 探针数据 → 沙盘 GUI 面板映射

沙盘人物状态面板（`character_status_panel.py`）分 15 个折叠区。数据来自**快照实时推送**（每 3s `scene_snapshot` → `SimBundle` 42 字段 → `CharacterState`），而非手动跑探针。探针的一次性深度数据可补全快照未覆盖的字段。

| GUI 面板 | 数据来源 | 对应 SimBundle/CharacterState | 补充探针（深度数据） |
|------|------|------|------|
| 🏷 身份 | 快照实时 | `name, age, gender, species, occult_types, is_ghost, is_npc` | `ai_probe_sim_data` |
| 💭 心情与 Buff | 快照实时 | `mood, mood_vote_summary, active_buff_names` | `ai_probe_sim_data`（完整 buff） |
| 📊 需求（9 项） | 快照实时 | `motives, special_stats` | — |
| 🎯 技能 | 快照实时 | `skills` | `ai_probe_sim_data` |
| 💼 职业与抱负 | 快照实时 | `career, aspiration, fame` | `ai_probe_siminfo`（职业历史/详情） |
| 🧬 性格与倾向 | 快照实时 | `traits, hidden_traits, whims, fears, preferences` | `ai_probe_sim_data` |
| 👥 人际关系 | 快照实时 | `relation_scores, relation_bits, sentiments, family_relations` | `ai_probe_social_interactions` |
| 📍 空间 | 快照实时 | `room_name, posture_target, outfit_category, walkstyle, body_state` | — |
| 🎒 背包 | 快照实时 | `inventory` | — |
| 🤖 自主性 | 快照实时 | `interaction_source, autonomy_enabled, last_user_directed_ago, bouncer_priority, queue_state` | — |
| 🏃 身体 | 快照实时 | `pregnancy, age_progress, voice_type, sexual_orientation, social_group` | `ai_probe_occult` |
| 🌿 生活方式 | 快照实时 | `lifestyles` | `ai_probe_siminfo` |
| 🎓 学位 | 快照实时 | `degree_info (major/university/gpa/status)` | `ai_probe_siminfo` |
| 🏢 事业 | 快照实时 | `owned_businesses` | `ai_probe_siminfo` |

> **右下角面板**（用户描述）：抱负/职业/技能/关系/随身清单/属性/家庭关系/需求/社交团体/已拥有事业 —— 对应上表中 💼🎯👥🎒🧬👥📊🏃🏢 折叠区。
> **中间上方**（用户描述）：情绪和细节 —— 对应 💭 心情与 Buff 折叠区（mood + mood_vote_summary + active_buff_names）。

### 9.4 探针数据 → 动作插入（四层帮助）

探针对动作插入的帮助按层级递进：

**第一层：动作存在性查证（图书馆）**
```
加新动作的标准流程：
1. grep affordances_index.txt 确认 super=Y（可 push）
2. grep types_index.txt 确认目标对象属性路径
3. ai_probe_aff_detail 看完整 test_globals（年龄/技能/情绪门控）
4. 写 custom_actions 规则行，hints = 查证到的 EA 类名列表
```
→ 这是**"这个动作理论上能不能推"**的答案。

**第二层：场景感知（运行时采集）**
```
确定动作在当前场景是否可用：
1. ai_probe_item_interactions → 场景中有哪些物品、每个物品有哪些 affordance
2. ai_probe_social_interactions → 当前 sim 对每个在场角色的可用社交交互
3. GROUND 节 → 地面/水体 affordance（terrain-gohere/pool_Splash 等）
```
→ 这是**"现在能不能推这个动作"**的答案。

**第三层：实证闭环（观察器 + 探针富化）**
```
autonomous_observer.py:
  快照 current_action → 积累游戏自主使用的 affordance 实证
  → sort_by_proven(hints) → 被游戏用过的排前面

probe_data.py:
  探针静态数据（TERRAIN 18条 / WATER 9条 / PHONE 10条 / 情境社交 6组）
  → enrich_from_probes(hints) → 追加注册表有但还没实证过的候选
```
→ 这是**"哪些 hints 最可能成功"**的答案——程序自动消费，无需人工。

**第四层：失败诊断（push/queue_diff）**
```
动作被拒时：
1. /action_result 回传拒因（EnqueueResult 可读文本）→ lifecycle tracker 记录
2. ai_probe_queue_diff → 对比做动作前后的队列变化 → 找"实际入队了什么"
3. ai_probe_push → 手动测试 push_super_affordance 的真实行为
4. action_rejection_catalog.md → 已知拒因速查
```
→ 这是**"为什么推不动"**的答案。

### 9.5 图书馆 / 结构树 / 探针数据 的关系

```
                    ┌─────────────────────────────┐
                    │   ai_probe_library           │
                    │   types_index.txt (~3.5MB)   │
                    │   "属性字典——类型有什么属性"    │
                    └──────────┬──────────────────┘
                               │ 查属性路径
                               ▼
┌──────────────────┐   ┌─────────────────────────────┐
│ ai_probe_tree    │   │   ai_probe_affordances       │
│ structure_tree   │   │   affordances_index.txt      │
│ (~250KB)         │   │   (~9.5MB)                   │
│ "对象包含关系"    │   │   "什么交互、能不能 push"     │
└──────────────────┘   └──────────┬──────────────────┘
                                  │ 查证 affordance 类名
                                  ▼
                    ┌─────────────────────────────┐
                    │   沙盘动作系统               │
                    │   custom_actions/ 规则 hints │
                    │   action_catalog 动态生成     │
                    └──────────┬──────────────────┘
                               │ 运行时验证
                               ▼
        ┌──────────────────────────────────────────┐
        │  运行时采集探针（"当前场景有什么"）        │
        │  ai_probe_item_interactions  ← 场景物品   │
        │  ai_probe_social_interactions ← 社交菜单  │
        │  ai_probe_system_interactions ← 手机/日历 │
        │  ai_probe_siminfo             ← 面板数据  │
        └──────────────────┬───────────────────────┘
                           │ 实证反馈（自动）
                           ▼
        ┌──────────────────────────────────────────┐
        │  autonomous_observer (自动积累实证)       │
        │  probe_data (静态注册表富化)              │
        │  action_lifecycle (推送→执行→verdict)    │
        └──────────────────────────────────────────┘
```

**关联逻辑**：
- **图书馆是基础**——没有 `types_index.txt` 就只能靠 `ai_probe_deep` 盲探
- **结构树是地图**——告诉你从根对象到叶子的路径（如 `sim_info → _career_tracker → _careers → {guid: Career}`）
- **交互图书馆是菜单**——告诉你游戏有什么交互、能不能 push
- **运行时探针是"今天菜单上有什么"**——图书馆是完整菜单，运行时探针是"当前场景实际可点的菜"
- **观察器是"大家最爱点什么"**——游戏自主用了的 = 被实证有效的

**不需要程序化"关联"**——数据已经在用。沙盘的 hints 管线已走通全链路：图书馆查证 → 写规则 → 运行时验证 → 观察器排序 → 自动优化。探针输出文件是**手册和参考**，通过 `grep` 消费；快照是**实时数据管道**，通过 `game_bridge` 自动消费。

### 9.6 日常使用速查

| 想做什么 | 用哪个 | 怎么用 |
|------|------|------|
| 加新动作 | `affordances_index.txt` | `grep "关键词" \| grep "super=Y"` |
| 查数据存哪 | `types_index.txt` | `grep "CareerTracker\|SkillManager"` |
| 猜不到字段名 | `ai_probe_diff` | 改状态前后各跑一次 → 看 changed |
| 确认交互能不能推 | `ai_probe_aff_detail` | 看 test_globals 完整内容 |
| 看当前场景有什么 | `ai_probe_item_interactions` | 看 TYPE/AFF/GROUND 三节 |
| 确认新字段非空 | `ai_probe_bundle <sim名>` | 逐字段看 [OK]/[空] |
| 验证 walk 通路 | `ai_probe_pick` | 看 PickInfo 签名 |
| 手动测试 push | `ai_probe_push` | 真实调用观察行为 |
| 社交规则查证 | `ai_probe_social_interactions` | 看 TARGET_AFFORDANCES 节 |
| 补全面板数据 | `ai_probe_siminfo` | 看 CAREER_EXTENDED/OWNED_BUSINESSES 等节 |

---

## 10. 验证方式

### 自动化（可独立运行）
```bash
# 沙盘模块编译
cd Mythica
python -m py_compile mythica_sandbox/*.py

# 全部测试
pytest tests/ -q                    # 711 tests（桌面端）
pytest mythica_sandbox/tests/ -q    # 81 tests（沙盘）
cd ../自制mod && pytest tests/ -q   # 293 tests（游戏端）
# 合计: 1085 tests
```

### 手动（需 Sims 4 运行）
1. 启动沙盘: `python -m mythica_sandbox.app`
2. 游戏加载存档 → 数据自动推送（无需手动操作）
3. 沙盘 GUI 自动显示 World State
4. 点击「运行一轮」触发 AI 决策
5. 检查 `MythicaData/Action_Command.signal` 是否被游戏端消费

---

## 11. 回调机制（v0.2.0，v0.3.1 修订）

沙盘通过两种路径接收数据，回调机制在两种模式下统一触发 GUI 刷新：

```
ProbeHub 中枢模式 (主 Mythica 运行时，HTTP Forward 订阅):
  ProbeHub._fan_out()
    → 后台线程 POST :52174/_hub_forward（带 X-ProbeHub-Route 头）
    → _handle_hub_forward 按原始路由分发
        → _handle_probe / _handle_scene_snapshot → 存共享内存 → 落盘
        → _notify_probe_callbacks() → app._on_probe_received(payload)

独立模式 (主 Mythica 未运行，游戏直连回退):
  _SandboxHandler.do_POST()
    → _handle_probe / _handle_scene_snapshot → 存共享内存
    → _notify_probe_callbacks() → app._on_probe_received(payload)

app.py (GUI 线程):
  _on_probe_received(payload)
    → self.after(0, _refresh_world_state_display)   # try/except 落盘保护
        ├─ 快照优先 (get_latest_snapshot)
        └─ 探针回退 (get_latest_probe)
```

> **注：** v0.2.0 曾有进程内 `SandboxSubscriber` Protocol 订阅者——v0.2.1 发现跨进程单例不共享后已删除，
> 现在唯一的中枢订阅方式是 HTTP Forward（见 §11 决策 6/6b）。

**设计原则对齐主程序 CLAUDE.md：**

| 原则 | 实现 |
|------|------|
| 瘦游戏端 / 胖桌面端 | 游戏端只抓数据+发 HTTP；所有设置/决策在桌面端 |
| 显式接口 = 防漂移 | ProbeHubSubscriber Protocol、路由表、映射表（`_CHAR_HIT_FIELDS`/`_deprefix`）、`_sv` 版本号 |
| 所有错误落盘可追踪 | `error_log._log_error()` 统一入口，`MythicaData/sandbox_logs/` 1MB 轮转 |
| 极简第一 | 11 模块，每模块单一职责 |
| 不建抽象层直到有第二个用例 | 回调直接用 list + `after(0)`，不建事件总线；中枢在第二个消费者出现时建立 |
| 禁止 `except: pass` | 全部 except 有注释或写日志 |

---

## 12. 设计决策记录

以下决策是经过讨论或踩坑后确定的——未来改代码时先读这里，避免推翻已确定的设计。

### 1. 为什么 /health 要返回 `session`？
游戏端通过比较 `/health` 的 `session` 字段判断 Mythica 是否重启过（session 变了 → 旧请求丢失 → 安全清除飞行锁）。沙盘必须返回 `session`，否则游戏端的 `_check_desktop_restarted()` 永远返回 `False`，飞行锁永不释放。

**决策：** `server.py` 启动时生成 `uuid.uuid4().hex[:12]`，`/health` 和 `/probe` 响应都带上。**2026-07-13 修复。**

### 2. 为什么 /probe 要区分 alive_check？
游戏端 `_check_desktop_alive()` 发 `{'message': 'alive_check'}` 到 `/probe` 做存活检测。如果沙盘不加区分地存储，probe_count 会被心跳污染，`_latest_probe_data` 会变成无效的心跳包。

**决策：** `/probe` handler 检测单字段 `alive_check` → 只回 `session`，不存不计数。**2026-07-13 修复。**

### 3. 为什么数据通道独立于 AI 总开关？
沙盘 v0.1 初始设计把 `auto_start_dialogue` 当数据开关——False 时游戏端一行探针都不发。这与主程序"瘦游戏端/胖桌面端"原则矛盾：游戏端应只负责采数据，开关决策全在桌面端。

**决策：** `_run_queue_probe_once` 移除所有门控；`_start_queue_probe` + `_start_scene_snapshot_timer` 在 mod 加载时无条件启动。**2026-07-13 v0.1.3 修正。**

### 4. 为什么场景快照和交互探针是双通道？
交互探针依赖交互事件——角色发呆了就没有数据。但 AI 决策需要知道"谁在场、在哪儿、天气如何"的完整画面。场景快照每 3 秒采集全场状态，与交互探针互补。

**决策：** 双通道并行，GUI 优先展示快照（完整），回退到探针（有动作上下文）。**2026-07-13 v0.1.3 新增。**

### 5. 为什么动作目录动态生成而非硬编码？
硬编码的动作列表（"走去厨房"、"走去卧室"）会随游戏数据变化而失效。WorldState 中有哪些角色、哪些地点都是动态的——目录应从数据生成。

**决策：** `generate_action_catalog(ws)` → 在场角色 × 已知地点 × 其他在场角色 → walk/interact/idle。新增地点或角色时目录自动更新。

### 6. 为什么不复用旧 app 的 server.py？
旧 app server 耦合了 13 个 Mixin 和 `ui.py` 回调链。沙盘只需要极简的数据接收。v0.1.0 采取独立 server。

**v0.2.1 当前方案：** 沙盘始终运行自己的 HTTP server（`:52174`），通过两个通道接收数据：
1. **ProbeHub HTTP Forward**（优先）——向中枢 `:52173` 注册回调 URL `:52174/_hub_forward`，中枢扇出时 HTTP POST 转发
2. **游戏直连**（回退）——游戏 mod `target_ports = [52174, 52173]` 直接 POST

两个通道最终走相同的 handler → 共享内存 → game_bridge 读取。

**v0.2.0 废弃方案：** 曾尝试 `get_hub().subscribe(SandboxSubscriber)` 跨进程订阅——但 `ProbeHub` 单例在沙盘进程中是未启动的空壳，订阅成功但数据永不到达。HTTP Forward 是正确的跨进程订阅方式。

**决策：** 始终启动沙盘 server 作为数据落点；中枢 Forward 订阅作为可选的跨进程通道。**2026-07-14 v0.2.0→v0.2.1 修订。**

### 6b. 为什么用 ProbeHub 中枢而非多端口？（🆕 v0.2.0，v0.2.1 修订）
游戏端 `target_ports` 已支持多端口发送，但每加一个子程序就要改游戏端常量——违背"瘦游戏端"原则。中枢模式让游戏端只管一个地址，桌面端子程序自注册。

**v0.2.1 当前方案：** `ProbeHub` 发布/订阅 + HTTP Forward。同进程订阅者走 Protocol 接口（零开销）；跨进程订阅者走 HTTP Forward（中枢 POST 到子程序的 `/_hub_forward` 端点）。新子程序接入流程：① 启动自己的 HTTP server ② 向中枢 POST `/_subscribe_forward` 注册回调 URL。沙盘已完成此流程。

**v0.2.0 废弃假设：** 原以为 `get_hub()` 单例可跨进程共享——实际 Python 模块级单例在独立进程中各自独立。HTTP Forward 是跨进程的正确解。**2026-07-14 v0.2.0→v0.2.1 修订。**

### 7. 为什么 ActionOption 有独立的 `reason` 字段？
AI 返回的 `reason`（选择理由，如"饿了"）和 `description`（动作描述，如"斑 走去 厨房"）是两种信息。初始设计把 reason 覆盖写入 description，导致 AI 输出的理由丢失。

**决策：** `ActionOption.reason` 独立字段，`description` 保持原始描述。**2026-07-13 v0.1.1 修正。**

### 8. 所有错误必须落盘
沙盘跑在用户机器上，出了 bug 大概率无法复现——读日志是唯一的排查手段。每个 `except` 必须写 txt（含时间戳+traceback），不能只在 GUI 闪过。

**决策：** `error_log.py` 为统一出口，`_log_error(tag, msg, exc_info)`。最外层自保护（写 stderr fallback）。日志放 `MythicaData/sandbox_logs/`，1MB 轮转。**2026-07-13 从沙盘决策提升为项目级原则。**

### 9. 为什么游戏端路由按执行原语而非动作语义？（🆕 v0.10.0 通用执行器）
每新增一种动作类型都要改游戏端 handler → py37 编译 → 打包 → 部署 → 删缓存 → 重启游戏——迭代成本压垮动作扩容。实证发现所有动作底层只有 3 种 push 形态（无 pick push / 带 pick 地形 push / idle），旧的 4 个 handler 是它们的参数化重复。

**决策：** 游戏端 `_ACTION_ROUTES` 按执行原语组织——`push`/`goto` 两个通用 handler + 显式 `target_kind` 字段（消灭按 action_type 推断目标类型的隐式分支）；legacy 4 别名永久保留（成本≈0）。动作语义（哪个 affordance/什么目标/什么条件）全部由桌面端 payload 表达。**被拒：** 每种新动作独立 action_type + 独立 handler——那是 v0.9.0 的路，扩容一次部署一次。**2026-07-17 v0.10.0。**

### 10. 为什么自定义动作表是 Python 表不是 JSON？（🆕 v0.10.0）
**决策：** `custom_actions.py` 的 `CUSTOM_ACTION_RULES`（frozen dataclass 元组）。理由：① 已有 `_TONE_AFFORDANCE_HINTS`/`_MOTIVE_OBJECT_RULES` 两个 Python 表先例，第三种机制=漂移；② 沙盘源码直跑即改即测，JSON 的免重启优势不存在；③ frozen dataclass 构造期抓字段 typo；④ 查证备注（ages/来源/实测日期）在注释里自然沉淀。升格机制：`verified` 布尔——测试页显示全部行（未验证 🧪 标），目录只消费 `verified=True`。**被拒：** JSON 配置文件（要手写校验、丢类型检查）；合并进 `_MOTIVE_OBJECT_RULES`（各有门控逻辑，硬塞=字段爆炸）。**2026-07-17 v0.10.0。**

### 11. 为什么结果回传走 HTTP 而非信号文件？（🆕 v0.10.0）
**决策：** `/action_result` 经游戏端探针发送队列广播 `[52174, 52173]`。理由：项目通信方向约定"游戏→桌面=HTTP，桌面→游戏=信号文件"——反向文件通道是第三种机制；桌面端现无轮询游戏产物的循环；HTTP 队列自带按端口故障隔离。配套：`result_id` 去重（hub Forward+直连双投递）；`_probe_sender_worker` 4xx 不冷却（旧沙盘 404 不拖累探针）。**语义纪律：status=pushed 只代表 guaranteed 入队≠已执行**（血案 §2.2），UI 文案统一"已入队"。**被拒：** 写 Action_Result.json 桌面轮询（竞态/轮转/新轮询循环全要重做）。**2026-07-17 v0.10.0。**

### 12. 为什么状态依赖动作走跨轮模式而非单轮链式？（🆕 2026-07-21）

**问题：** 电视/音响/壁炉/篝火等物品需要先"打开"才能使用。第一直觉是把"打开"和"使用"放在同一个 hints 链里，但通用执行器首成即停——sim 只会开机，不会接着用。

**决策：** hints 链第一条放"打开"（`Fireplace_Light` / `stereo_TurnOnAndListen`），依赖跨自然轮完成：第一轮打开→下轮 AI 看到 `{recent_actions}` 里刚打开→再选→打开类 hint 被拒（已开）→自动落到使用类 hint。零游戏端改动。

**证据链：**
- `sandbox_action_results.txt` 2026-07-21 00:01:41：千手 扉间 在壁炉旁取暖 → Fireplace_Light → Fireplace_WarmSelf pushed ✓（壁炉跨轮模式验证通过）
- `sandbox_action_results.txt` 2026-07-21 00:10:13：千手 扉间 在壁炉旁取暖 → 再次 pushed ✓（已燃壁炉直接取暖）
- `sandbox_action_results.txt` 2026-07-21 00:09:51：音响 `GenericOnOff_TurnOn` 被拒（`StereoOnOff` 不兼容）→ 之后改为 `stereo_TurnOnAndListen` 待测

**关键制约：** 音响的 `stereo_TurnOnAndListen` 是"打开+听"不是"打开+跳"——跨轮首轮动作语义可能偏离 AI 意图。壁炉最干净（开→取暖，语义无歧义）。

**被拒：** 游戏端加 pre-hints 双段链（改游戏端+重部署，跨轮天然更简单）。**2026-07-21。**

### 13. 为什么 `GenericOnOff_TurnOn` 不能作为万能开关？（🆕 2026-07-21）

**问题：** 以为 Sims 4 所有电器共用 `GenericOnOff` 状态→推 `GenericOnOff_TurnOn` 就能打开任何电器。

**实测推翻：** 不同物件使用完全不同的状态系统。

**证据链：**
- `sandbox_action_results.txt` 2026-07-21 00:09:49：`object_VideoGameConsole_TV_High` → `GenericOnOff_TurnOn` 被拒（`does not have the GenericOnOff state`），该机型连 `TVChannel` 也没有
- `sandbox_action_results.txt` 2026-07-21 00:09:51：`object_stereoTableLOW_01` → `GenericOnOff_TurnOn` 被拒（`does not have the GenericOnOff state`），音响用的是 `StereoOnOff`
- 对比：壁炉 `Fireplace_Light` 成功——壁炉确实用 `GenericOnOff`

**决策：** 每个物件类型必须用各自的"打开"affordance：壁炉→`Fireplace_Light`（验证通过）、音响→`stereo_TurnOnAndListen`（待测）、电视→暂无通用方案。**绝不用** `GenericOnOff_TurnOn` 作为通用兜底——它只对部分物件有效，对 TV/音响是浪费一轮试错。**2026-07-21。**

### 14. 为什么 `toilet_Clean`/`shower_Clean` 不能独立推送？（🆕 2026-07-21）

**问题：** 清洁马桶/淋浴的 affordance `super=Y auto=Y user=Y`，但 push 后被拒。

**证据链：**
- `sandbox_action_results.txt` 2026-07-21：`toilet_Clean` → `SituationRunningTest: No situation matching test criteria found`
- 同期 `shower_Clean` 同样拒因
- 对比：`counter_Clean` 无此限制，pushed ✓

**根因：** 这两个 affordance 依赖 Sims 4 的"situation"机制——必须处于"大扫除"等清洁场景上下文中才能运行。它们不是独立的 SI，而是场景子步骤。

**决策：** 从规则表删除 `clean_toilet` / `clean_shower`。`counter_Clean` 保留（实测通过）。**判别法：** 拒因含 `SituationRunningTest` → 场景绑定型，不可独立推。**2026-07-21。**

### 15. 为什么 trait/skill 门控暂不修？（🆕 2026-07-21）

**问题：** 部分动作被 sim 特质或技能等级拒。

**证据链：**
- `sandbox_action_results.txt` 2026-07-21 00:27:03：斑 `bar_Tendbar` → `Actor doesn't have any or enough traits in white list`（柱间同样 affordance 成功）
- `sandbox_action_results.txt` 2026-07-21 00:09:53：扉间 `WorkoutMachine_Workout` → `Actor doesn't have any or enough traits in white list` + `skill level not in desired range`
- `sandbox_action_results.txt` 2026-07-21 00:27:33：柱间 `Gardening_Tend_Start` → `skill level not in desired range`
- 同期：显微镜同样 `skill level`

**分析：** trait 门控属于 sim 身份设定（斑缺调酒/活力特质），不该绕过；skill 门控讨论了两种方案——A：游戏端 `_ensure_min_skill` push 前设技能；B：快照里带技能数据→桌面端过滤。B 需要知道每个 affordance 的最低技能要求（值在游戏 tuning 里，桌面不可见），A 最简单但需要改游戏端。

**决策：暂不修。** trait 门控是 sim 个性，不该改；skill 门控等技能数据采集成熟后再定方案。**2026-07-21。**

### 16. 为什么房间导航用 `room_name` 聚组而非 `room_id`？（🆕 2026-07-21）

**问题：** 沙盘需要"去厨房""去卧室"级别的房间导航，但之前只有 `room_id`（数字）。

**决策：** 游戏端 `mythica_collect.py` 的 `_collect_spatial_fields` + 物品采集两处采集 `sim.room.name` / `obj.room.name` → `room_name` 字段。桌面端 `action_catalog.py` 新增 `_build_walk_room_actions`——按 `room_name` 聚组，每间异房间一条 `🚶 去{房间名}`，goto 目标用房间代表性物品坐标（`MAX_WALK_ROOM_PER_CHAR=5`）。

**证据链：**
- 游戏端：`mythica_collect.py` line 695-699（sim）+ line 1626-1630（obj）——`room = getattr(obj, "room", None); room_name = str(getattr(room, "name", "") or "")`
- 桌面端：`world_state.py` CharacterState + SceneObject 加 `room_name` 字段
- `game_bridge.py` line 485 + 576：探针/快照解析 `room_name`
- `action_catalog.py` line 240-275：`_build_walk_room_actions` 按 room_name 聚组→`prompt_label="🚶 去{room_name}"`

**向下兼容：** `room_name` 为空时（旧版游戏数据）→ `_build_walk_room_actions` 返回空 → 回退原有的 `_build_walk_obj_actions`（按物品名、不按房间）。**2026-07-21。**

### 17. 为什么测试页只显示未验证规则？（🆕 2026-07-21）

**问题：** 旧版测试页的 🧪 自定义组显示全部规则行（含已测已验证的），大量已通过的规则挤占空间，未测的混在中间不好找。

**决策：** 新增 `iter_unverified_rules()`（返回 `verified=False` 的行）替代 `iter_panel_rules()`（返回全量）作为测试页数据源。已测→`verified=True`→自动从测试页消失，进 AI 目录。UI 分组改名 `🧪 自定义`→`🧪 待测动作`，移除 `⏸ 待机` 组渲染。

**证据链：**
- `custom_actions.py` line 498-500：`iter_unverified_rules()`——`return tuple(r for r in CUSTOM_ACTION_RULES if not r.verified)`
- `action_panel.py` line 27：import 改为 `iter_unverified_rules`
- `action_panel.py` line 199：`_GROUP_CUSTOM = "🧪 待测动作"`
- `action_panel.py` line 204：`_GROUP_ORDER` 移除 `_GROUP_IDLE`
- 会话实测流转：13 条已验证→测试页可见→逐条测完设 True→逐个消失→最终只剩待测项

**2026-07-21。**

### 18. 为什么 `verified` 流转需要实物证而非记忆？（🆕 2026-07-21）

**问题：** 今天的错误——将 4 条未实测的规则（小提琴/日记/拳击/保龄球）设成 `verified=True`，同时漏了 1 条已实测通过的（壁炉取暖）。根因："通过 5 条"是口头估计，没有对照测试记录逐条核验。

**决策：** `verified=True` 必须有硬证据——满足以下至少一项：
- ① `sandbox_action_results.txt` 有该 `action_id` 的 `pushed` 记录（游戏端回传，最可信）
- ② `sandbox_action_test_record.md` 或 `AI_Sandbox_Actions.txt` 有"已测"标注 + 无拒因

**禁止单凭记忆改 verified。** 纠正证据：
- `sandbox_action_test_record.md` line 5/8/12/13：小提琴/日记/拳击/保龄球标注"未测"→`verified=False`
- `sandbox_action_results.txt` 2026-07-21 00:01:41 + 00:10:13：壁炉两次 pushed ✓ → `verified=True`

**2026-07-21。**

### 19. 自主动作观察器——读端→写端反馈闭环（🆕 2026-07-25）

**问题：** 沙盘读端（每 3s 快照中 sim 的 `current_action` = 游戏自主选择的精确 affordance 类名）和写端（通过 hints 推 affordance）之间没有反馈。游戏自主执行了成百上千次 affordance——每一次都是实证，但从未被收集和复用。

**决策：** 新增 `autonomous_observer.py`（~140 行），每轮 WorldState 解析后扫描所有 sim 的 `current_action`，累加实证索引。`action_catalog.py` 中 `sort_by_proven(hints)` 将游戏常用的 affordance 排前面。

**2026-07-25。**

### 20. 快车噪音过滤器（🆕 2026-07-25）

**问题：** 快车从物品 `affordance_names` 取 hints 时，`sim-stand`、`debug_`、`si_` 等噪音 affordance 淹没了有意义的手写 hints。

**决策：** `action_catalog.py` 新增 `_is_noise_affordance()`，与游戏端 `_OBJECT_AFF_SKIP_PATTERNS` 对齐，过滤前缀 `debug_/cheat_/sim-stand/stand_passive/sit_passive` 和子串 `si_/superaff/mixer/repair/salvage` 等。

**2026-07-25。**

### 21. 自定义规则 `hints=()` 快车自动派生（🆕 2026-07-25）

**问题：** 每条新规则需手写 hints——grep affordances_index 抄类名，不同型号物品可用 affordance 不同。

**决策：** `build_option_from_rule()` 中从物品的 `affordance_names` 自动填充 hints。手写 hints 排前面（意图优先），物品 hints 去重追加（兜底），观察器按实证排序。新规则只需 `target_match + verb + label`，`hints=()` 即可。

**2026-07-25。**

### 22. 测试页「✅ 已验证动作」组（🆕 2026-07-25）

**问题：** 已验证规则只在动态目录中，场景有对应物品才可见，混在几十个候选里难定位。

**决策：** `action_panel.py` 新增 `_GROUP_VERIFIED` 组 + `_build_verified_rows()`，与 🧪 待测动作组平级显示。

**2026-07-25。**

### 23. 动作生命周期交叉验证——双重数据源确认动作去向（🆕 2026-07-26）

**问题：** `/action_result` 回传 `status=pushed` 只代表"入队"，不代表 sim 真正执行了。管理类/UI 类交互入队后静默消失，但系统无法区分"正在执行""卡在队首""被游戏取消"。

**决策：** `on_snapshot()` 中同时比对两个独立数据源——`current_action`（正在执行的交互）+ `queue_state.head_affordance`（队首交互）——产生 5 种可区分结局：confirmed / stuck_at_head / unconfirmed / timeout / departed。每个完结动作自动写入 `verdict` + `verdict_detail`。

**闭环全景：** 详见 [`docs/action_push_closed_loop.md`](docs/action_push_closed_loop.md) — 三条反馈通道、三种断裂模式、诊断命令。

### 24. Observer → 规则自动验证 + GUI 通知（🆕 2026-07-26）

**问题：** observer 写 `[observer.verify]` 日志，但无人看、无 GUI 消费。

**决策：** `on_rules_verified(callback)` 回调模式——observer 发现全覆盖时通知已注册的 GUI 回调。沙盘 GUI 日志显示 `🔔 N 条规则可以标 verified`。同一规则只报告一次防刷屏。

**设计思想：** observer 不应知道 GUI 存在（它在数据层工作），回调模式让任何消费者订阅结果。被动通知——observer 做好检测，消费者自己决定怎么用。

### 25. rejected→fix 闭环——从记录拒因到建议修规则（🆕 2026-07-26）

**问题：** 拒因分类完善但只用于展示。规则被拒 4 次后系统不会禁用它，AI 可能反复选注定失败的规则。

**决策：** `push_history.py` 新增 `suggest_rule_fixes()`：连续 ≥4 次全拒 → 建议 `verified=False`；同 hint ≥3 次被拒 → 建议从 hints 移除。`_extract_rule_id()` 从 action_id 提取 rule_id。每轮循环后输出到 GUI 日志 + 循环日志。

**设计思想：** 不是"自动改规则"（太危险）而是"自动生成建议"。人类审核后手动操作。消除"明明有数据却没人看"的浪费。

### 26. Hints 质量检测——推入噪音 vs 推入非预期（🆕 2026-07-26）

**问题：** 快车盲选兜底时推入了非预期 affordance（如画架推成 sim-stand），但 verdict 只说"执行了 X 秒"不标注问题。

**决策：** `_end_lifecycle()` 新增检测——pushed_affordance 不在 hints 中时：噪音类（sim-stand 等）→ `⚠️推入噪音`；非噪音类 → `⚠️推入非预期——建议加入hints`。区分"规则问题"和"新发现"。

### 27. confirm_push_effect 接线——死代码→自动化效果确认（🆕 2026-07-26）

**问题：** `confirm_push_effect()` 完整实现但零调用者。`action_tracker` 只管"是否开始执行"不管"是否产生效果"。

**决策：** `on_actions_sent` 捕获推送前 actor 的 {mood, relation_bits} 存为 `effect_baseline`。`_end_lifecycle` 结尾对比当前快照：关系 bit 变化 → `🎯效果确认: bit_change`；心情变 Flirty → `🎯效果确认: mood_shift_to_flirty`。只对浪漫/社交动作生效。失败不阻塞生命周期判定。

### 28. action_evidence.json 读者——死数据→活反馈（🆕 2026-07-26）

**问题：** evidence 每 60s 落盘但永不被读取。

**决策：** `load_evidence_for_rules()` 读取 evidence，交叉对比各规则 hints 的 exec_rate。`rule_health_report()` 综合 verified + observer + evidence 三维输出健康分表格。启动时输出统计，每 5 轮追加完整报告。

### 29. 规则健康面板——三维评分（🆕 2026-07-26）

**问题：** 55 条规则散落，不知哪些有数据支撑、哪些从未测试。

**决策：** `rule_health_report()` 三维评分——verified（人工确认）+ observer 实证（游戏用过）+ push 证据（推送成功率）。表格输出每行一条规则。

### 30. 背包物品动作路径 inventory_item（🆕 2026-07-26）

**问题：** 两端代码完备但零规则走背包路径。以背包中物品为例——物品在背包中，`self` 路径报 "not valid from the world"。

**决策：** 改为首条 `target_kind="inventory_item"` 规则。`target_match` 按物品 def/name 匹配背包物品。`self` 只适用于 sim 自身的 affordance（如 phone_PlayGames），不适用于需要实体物品的动作。

### 23. `clear_queue` 工艺交互不能清队（🆕 2026-07-25）

**问题：** 画架 SlotTest→加 `clear_queue=True`→更糟：工艺进程被清队销毁。

**决策：** `CustomActionRule` 新增 `clear_queue` 字段。工艺类交互（crafting_resume）绝对不能清队。画架问题根因不在代码——`push_super_affordance` 对工艺交互有根本性限制，搁置。

**2026-07-25。**

### 24. 角色状态面板三级刷新（🆕 2026-07-26）

**问题：** 角色状态面板每 3s 刷新一次，`_char_fingerprint` 包含心情、需求值、buff、动作等频繁变化字段——每次刷新都触发 `_build_detail()` 全量重建 widget（14 个 collapsible section），面板持续闪烁且 CPU 浪费。

**决策：指纹拆分 + 增量更新。** 将原单一指纹拆为结构指纹和动态指纹，三级分流：

```
refresh() → _char_heavy_fp 变了？→ 是 → _build_detail()（全量重建 widget）
                               → 否 → _char_light_fp 变了？→ 是 → _refresh_light_fields()（增量更新）
                                                            → 否 → 跳过
```

**结构指纹 `_char_heavy_fp`**（约 20 字段）— 身份/性格/技能/职业/抱负/声望/孕期/生活方式/学位/事业/背包/性技能等**不常变**字段。变化时触发全量 widget 重建。

**动态指纹 `_char_light_fp`**（约 10 字段）— 心情/buff/需求值/动作/位置/着装/走姿/体格/自主性状态等**频繁变化**字段。变化时仅更新已有 widget 的文字/进度条，不拆不建。

**增量更新覆盖的 widget：**

| 区域 | 更新方式 | 关键 widget |
|------|---------|------------|
| 心情文字 | `label.configure(text=…)` | `_dyn_widgets["mood"]` → CTkLabel |
| buff 标签 | 清空子标签后重建 | `_dyn_widgets["buff_area"]` → 容器 Frame |
| 需求进度条 | `bar.set()` + 数字标签改色 | `_dyn_widgets["motives"][key]` → (val_label, bar) |
| 特殊 stats | `label.configure(text=…)` | `_dyn_widgets["special_stats"]` → CTkLabel |
| 位置/身着/走姿/体格 | `label.configure(text=…)` | `_dyn_widgets["location"/"posture"/"outfit"/"walkstyle"/"body_state"]` |
| 自主性摘要 | `label.configure(text=…)` | `_dyn_widgets["autonomy"]` → CTkLabel |

**widget 引用传递链：**
- `_kv_row()` 返回 value 的 `CTkLabel`——调用方存储引用（`_dyn_widgets["location"] = _kv_row(…)`）
- `_motive_bar()` 接受 `ref_dict` 参数——`ref_dict["motives"][key] = (val_label, bar)`
- 心情 label、buff 容器、自主性 label 在 `_build_detail()` 中直接存入 `_dyn_widgets`

**实现文件：** `mythica_sandbox/character_status_panel.py`
- `_char_heavy_fp()` — 结构指纹
- `_char_light_fp()` — 动态指纹
- `_refresh_light_fields()` — 增量更新
- `_dyn_widgets: dict` — widget 引用表（`__init__` 初始化，`_clear_right()` 清空）

**效果：** 绝大部分刷新周期（心情变了、需求波动了、sim 换房间）只改已有 widget 文字——面板基本感觉不到刷新。

**2026-07-26。**

### 25. 交叉验证——两个独立数据源确认执行（🆕 2026-07-26）

**问题：** `/action_result` 回传 `status=pushed` 只代表"交互入队"，不代表 sim 真正执行了。管理类/UI 类交互入队成功但永远不会出现在 `running_interactions_gen()`（不播放动画）。此前只看 `current_action` 一个数据源，无法区分"排队中"、"卡在队首但永不执行"、"被游戏静默取消"。

**决策：** 交叉验证——同时比对两个独立数据源：
- **数据源 1：`current_action`**（`running_interactions_gen` 第一个）—正在执行的交互，最权威
- **数据源 2：`queue_state.head_affordance`**（`queue.get_all_running_and_queued` 第一个）—队首交互，能看到"排到了但还没开始执行"

产生 5 种可区分结果：✅ confirmed / ⏳ queued / 🔒 stuck_at_head / ⏰ 静默丢弃 / 🚪 sim离开。

- `ActionLifecycle` 新增 `queued_at` 时间戳 + `QUEUED_TIMEOUT=60s`
- `on_snapshot` 中 Tier 0 先检查队首匹配，Tier 1/2 再检查 current_action 匹配
- `_end_lifecycle()` 读 `observed_actions` 时间线做精确 verdict 判定

**2026-07-26。**

### 26. 执行判定（verdict）——动作测试自动标注（🆕 2026-07-26）

**问题：** 测试一个动作是否"能执行"，需要人去读日志判断。`confirmed` vs `unconfirmed` 没有统一标签。

**决策：** `ActionLifecycle` 新增 `verdict` / `verdict_detail` 字段。`_end_lifecycle()` 结合时间线自动写入：
- `confirmed`：`first_seen_at` 有值 → 可标 verified
- `stuck_at_head`：排到队首 60s+ 未执行 → 管理/UI 类交互，需换 hints
- `unconfirmed`：入队后从未执行 → 需人工检查

新增 API：
- `unconfirmed_actions()`：返回所有需要人工检查的动作
- `manual_verification_checklist()`：按问题类型分组输出清单（🔒卡在队首 / ❓消失 / ⚡短动作 / ❌全拒），每类告诉用户"去游戏里看什么、怎么判断、怎么修"，写入 `sandbox_cycle_log.txt` 每轮末尾

动作测试页 UI：verdict 覆盖底色——`stuck_at_head` 深红 + ⚠️需进游戏检查，`unconfirmed` 暗橙 + 👁需确认。

**2026-07-26。**

### 27. 门控延迟可视化——deferred stage（🆕 2026-07-26）

**问题：** 预推验证门（G6 特殊状态中 / G3 玩家控制等）defer 的动作只进入内存延迟队列，动作测试页时间线完全不可见。用户看不到"为什么这个动作没发出去"。

**决策：** `ActionLifecycle` 新增 `deferred` / `deferred_reason` 字段 + `deferred` stage。`on_actions_deferred()` 为被延迟的动作创建玫红色时间线条目。延迟→重试发送时 `on_actions_sent()` 自动检测已有 deferred 条目并更新为 `sent`。

- `action_panel.py`：deferred 条目玫红底 + ⏸暂缓(原因) 标签
- `app.py`：validation.deferred 后自动调 `on_actions_deferred()`
- 特殊动画状态中的 sim 被选动作 → 立刻显示 ⏸ 标签，不会被静默忽略

**2026-07-26。**

### 28. 自动 clear_queue——sim 正忙时清队插队（🆕 2026-07-26）

**问题：** AI 选了动作，但 sim 正在做别的事。动作排在队列尾部，`current_action` 一直不变 → 30s timeout。AI 不知道要主动清队。

**决策：** `app.py` 在 AI 动作发送前检测 sim 是否正忙（`current_action` 非空）→ 自动 `clear_queue=True`。游戏端在 push 前先 `cancel_all()` + `USER_CANCEL` 清空现有交互队列，新动作立即排到队首。

**被拒：** 让 AI 在 prompt 中学习"何时清队"——这是机械判断，不需要 AI 决策。

**2026-07-26。**

### 29. 游戏端 `/action_result` _rv=2——推送时刻 sim 状态（🆕 2026-07-26）

**问题：** 沙盘收到 `status=pushed` 后要等下一个 3s 快照才知道 sim 当前在做什么、动作排在队列什么位置——延迟最长 3s。

**决策：** 游戏端在 push 成功后立即采集 sim 执行状态，随 `/action_result` 回传：
- `actor_current_action`：正在执行的交互名
- `actor_queue_depth`：队列深度
- `actor_queue_head`：队首交互名
- `actor_idle`：sim 是否空闲

字段在 `_execute_one_action` 中 `handler` 返回后、`_send_action_result` 前采集（`_collect_actor_state()`），零额外延迟。`_rv` bump 到 2，沙盘端 `on_action_result` 解析新字段入 `ActionLifecycle`。旧版沙盘忽略未知字段（`_rv` 后向兼容）。

**2026-07-26。**

### 隐藏 motives——游戏 UI 只显示 6 个，引擎里有 9 个（🆕 2026-07-26）

**发现：** Sims 4 主需求面板显示 6 个需求条（bladder/fun/hunger/social/energy/hygiene），但游戏引擎的 `commodity_tracker` 追踪 **9 个** motives。额外的 3 个（comfort/thirst/hygiene_hands）不在主 UI 中显示但引擎持续追踪数值（量程均 -100..100）。

**证据：**
- `sandbox_world_state.txt` 实测：`comfort:-75` / `thirst:97` / `hygiene_hands:-100`
- 游戏端 `mythica_collect.py` `_MOTIVE_KEYS` 收录全部 9 个
- comfort 在 sim 站太久/坐硬板凳时掉值，坐沙发/躺床恢复
- `gate_motive_emergency`（G7）对全部 9 个 motives 生效（任一 < -80 触发延迟）

**沙盘影响：**
- 人物状态 tab 显示全部 9 条（需求折叠区）
- `_MOTIVE_OBJECT_RULES` 中 comfort 规则已覆盖（匹配沙发/床 → `sofa_Nap`/`bed_relax_doubleBed`）
- `hygiene_hands` 规则已覆盖（匹配水槽 → `sink_washHands`）
- `thirst` 尚无对应规则——可作为未来待加项

**2026-07-26。**

---

## 三种动作推送方（🆕 2026-07-27）

沙盘当前有三种方式将动作发送到游戏端：

| 推送方 | 来源 | 优先级 | 触发方式 | 适用场景 |
|--------|------|:---:|------|------|
| 🤖 自动触发 | `auto_trigger=True` 规则 | 低 | 每轮程序扫描条件→评分选人→直接发送 | 收垃圾、修电器、拖水坑等"看到就做"的反应式动作 |
| 🧠 AI 决策 | `_call_action_selector` | **高** | 两段式 AI（内心声音→动作选择） | 社交、浪漫、需求驱动等需要叙事判断的动作 |
| 🖱 手动面板 | `action_panel._execute` | 最高 | 人在动作测试页手动点选 | 测试新规则、临时指挥 |

### 分配模型：AI 优先占位 + 自动填空

```
1. AI 动作先占位（sim + object）
2. 自动动作填空（sim 空闲 + object 未被 AI 占用）
3. 同一轮每人最多一个动作
4. 同一 object 最多一个动作
```

冲突裁决：AI > 自动（手动面板独立于 AI 循环，不参与分配）。

### 自动动作 sim 评分

自动动作不再"第一个合格就停"，改为对每个合格角色打分选最优：

```
空闲(idle)          +10   优先找没事做的人
有相关技能           +5   修理给有灵巧技能的
同房间               +3   不用跑远路
有相关 buff          +3   "脏乱环境"→优先清洁
用户指定优先         +15   管家、手艺人等
AI 已占用           出局   skip_sim_ids
睡觉/特殊动画状态   出局   硬排除
年龄/技能不满足      出局   硬排除
```

用户通过 `Mythica_Sandbox_Settings.json` 的 `character_priorities` 字段指定：`"优先"` / `"正常"` / `"回避"`。

### 31. 为什么自动动作不走 AI 决策？（🆕 2026-07-27）

**问题：** 收垃圾、修电器、拖水坑这些动作不需要判断——看到就做。走 AI 两段式管道浪费 token 且增加延迟。

**决策：** `auto_trigger=True` 的规则在每轮 AI 调用前执行，程序检测条件满足直接生成动作发送。自动动作和 AI 动作合并后经同一套门控+生命周期管线，确保可追踪、可去重。

**被拒：** 合并进动作目录让 AI 选择——收垃圾不需要叙事判断，AI 看 30 条候选再决定"要不要收垃圾"是浪费。

### 32. 为什么需要分配器而非简单拼接？（🆕 2026-07-27）

**问题：** 同一 sim 可能同时被自动和 AI 选中（如斑被 auto 选去修冰箱 + AI 选去拥抱真鳕）。两个命令先后发送，后发的 clear_queue 把先发的清掉 → 动作静默丢失。

**决策：** `_allocate_actions()` 在引擎内做合并——AI 先占位（sim+object），自动填空。冲突时 AI 胜出（AI 的叙事意图比维护动作更有价值）。同一 sim 的两个 AI 动作也只保留第一个。

**证据链：** 17 个新增测试覆盖全部冲突场景（同 sim/同 object/自动vs AI/两AI同sim）。

**2026-07-27。**

### 33. 为什么 AI 意图映射需要跨 group 兜底？（🆕 2026-07-27）

**问题：** AI 输出 `{character_name, group, target_name}` 来选动作，但经常标错 group——把 `use_object` 组的壁炉取暖标成 `need`、把物品使用标成 `need`。`_map_intents_to_actions()` 的 4 级回退全要求 `g == group` 精确匹配 → 一个 group 错误就全部失效 → `mapping_failed`。

**根因：** prompt 中 group 值不直接显示在目录行上——它隐含在中文段落标题中（"【物品】"→use_object / "【需求动作】"→need）。AI 需要自行推断映射关系，平均推断准确率不是 100%。

**决策：** 新增回退 4——跨 group 兜底。当 group 约束的匹配全失败后，忽略 group，仅按 `(character_name, target_name)` 跨所有 group 搜索（精确→去空格→子串）。日志标注 `cross_group match` 方便追踪。

**被拒：** 在 prompt 中把 group 值写进每行——增加 token 且 AI 仍可能忽略。

**2026-07-27。**

### 34. 为什么 `_rv=2` 即刻执行检测不能只靠 `actor_current_action` 匹配？（🆕 2026-07-27）

**问题：** `/action_result` `_rv=2` 回传了 sim 在 push 后即刻的执行状态。但 `_collect_actor_state()` 读取 `running_interactions_gen()` 的第一个交互——sim 正忙时（如淋浴中）这个值是**旧动作**而非我们刚推的新动作。仅靠 `_is_our_action` 精确匹配 → 大量即刻执行的动作被漏掉 → 短动作在 3s 快照间隙完成 → lifecycle tracker 误判 cancelled/timeout。

**决策：** 用多重信号推断"即刻执行"：① `actor_idle`=True（sim 空闲→动作立即开始）② `actor_queue_depth ≤ 1`（队浅→动作排第一）③ `clear_queue`=True（主动清队→动作插入队首）④ `actor_current_action` 匹配（确证）。任一命中即标记 `first_seen_at`，阻断后续误判。

**注意：** 只有当 payload 中 `_rv == 2` 时才启用此启发式（默认值会被误判为真）。新增 `ActionLifecycle.clear_queue` 字段追踪插队状态。

**被拒：** 直接用 `actor_current_action` 精确匹配——已证实遗漏率太高（sim 忙时旧动作永远不匹配）。

**2026-07-27。**

### 35. 为什么 G7 从"全动机拦截"改为"仅饥饿/膀胱/精力拦截社交"？（🆕 2026-07-27）

**问题：** 旧 G7 设计为"任何动机 < -80 → 推迟所有动作，让 EA 自主性处理"。实测证据推翻了两个前提：① comfort/hygiene_hands 等动机 EA 经常无视（sim 可以 comfort=-100 站几小时不自己休息）② 沙盘选的动作可能就是去修那个动机的（如 comfort=-100 + "去床上休息"）→ G7 拦下 → 形成永久死循环。实测 50% 动作被 G7 单门拦死。

**证据链：** 2026-07-26 时间线 12 条中 6 条被 G7 以 comfort/hygiene_hands 延迟；重启后 2026-07-27 时间线 11 条中 0 条延迟。`hygiene_hands=-100` 拦收垃圾最典型——手脏和收垃圾毫无关系。

**决策：** 新 G7：① 仅 hunger/bladder/energy < -80 触发（EA 自主性对这三项处理可靠——饿了会自己找吃的、憋了会自己上厕所）② 仅推迟 social/romance（分心动作用）③ need 组始终放行（AI 明确选了需求驱动）④ comfort/hygiene/hygiene_hands/fun/social 动机从不触发 G7。

**被拒：** 动机→动作关键词映射（`_action_addresses_motive`）——规则脆弱、维护成本高、edge case 多（如手脏≠收垃圾不该拦）。

**2026-07-27。**

### 36. 为什么维护命令走独立信号文件？（🆕 2026-07-27）

**问题：** 维护操作（清垃圾、修电器、改关系分数）和 sim 行为动作（社交、移动、使用物品）混在同一个 `Action_Command` 通道里——两种语义根本不同："打扫房间"不是 sim 的动作，是对世界状态的修正。

**决策：** 维护命令走独立信号文件 `Maintenance_Command.signal`（与 `Action_Command.signal` 平行）。游戏端 `mythica_maintenance.py` 轮询消费，与 `mythica_action.py` 完全独立。

**具体分界：**
- `Action_Command` → sim 行为：push affordance / goto / stop / idle
- `Maintenance_Command` → 世界维护：set_motive / add_buff / remove_buff / set_relation / add_relation_bit / remove_relation_bit / destroy_object

**被拒：** 合并进 Action_Command 加 `command_type` 字段——两种消费端的执行模型完全不同（一个推 affordance 走交互队列，一个直调 API），强制合并只会让路由表膨胀。

**2026-07-27。**

### 37. 为什么关系护栏分三层干预？（🆕 2026-07-27）

**问题：** 关系分数漂移——AI 叙事中角色关系可能偏移出合理范围（友情归零、浪漫爆表），需要机制将其拉回。

**决策：** 三层干预，按自动化程度递增：

| 层 | 触发方式 | 适用场景 |
|----|---------|------|
| 1 自动清扫 | 每 N 秒 sweep，友情<min→拉回、浪漫>max→压回 | 后台持续守护，防止静默漂移 |
| 2 手动编辑 | ✏️ 弹窗：双滑块 + 地板/天花板 | 用户精确调控特定关系 |
| 3 AI 动作 | AI 决策时感知护栏，选社交动作自然调节 | 叙事驱动的柔和修正 |

**通配符 `*`**：角色 → `*` 的护栏对所有人的关系生效（如"扉间 → * 浪漫 cap=0" = 扉间不与任何人发展浪漫关系）。

**被拒：** 纯手动（没有自动清扫）——护栏的价值在"自动守护"，手动编辑是补充不是替代。

**2026-07-27。**

### 38. 为什么需求/Buff 走直控而非通过动作？（🆕 2026-07-27）

**问题：** 让 sim "去吃东西"来恢复 hunger 需要：选动作→推 affordance→等 sim 走到冰箱→等动画播放→等 motive 自然恢复。链路长、延迟高、可能失败（冰箱被占用/路径不可达）。

**决策：** 需求和 buff 走维护命令直控——`set_motive` 直接写 `commodity_tracker`，`add_buff`/`remove_buff` 直接操作 `Buffs` 组件。与游戏自身的 `stats.set_stat` 作弊命令同级。

**使用场景：**
- 需求回满按钮（⚡）——调试/测试时快速恢复，不需要 sim 真的去吃东西
- buff 增删——测试特定情绪下的行为（如加 Flirty buff 看社交选项变化）

**注意：** 这是**调试/维护工具**，不是叙事手段。AI 叙事中不应依赖直控——那会跳过 sim 的自主决策链，产出不自然的叙事。

**被拒：** 全部走动作链——调试效率太低（等 sim 走→吃→消化需要数分钟）。

**2026-07-27。**

### 39. 为什么维护探针和动作探针分开？（🆕 2026-07-27）

**问题：** 已有 18 个探针（§9），为什么还要新增维护专用探针（`probe_puddles` / `probe_trash` 等）？

**决策：** 两类探针的消费方式根本不同：

| | 动作探针（§9） | 维护探针（§8.4） |
|---|---|---|
| **目的** | 查证、诊断、发现——跑一次就够了 | 持续扫描——每轮都要知道"现在有多少垃圾" |
| **输出** | txt 文件（手动 grep） | HTTP 回传（沙盘自动消费） |
| **频率** | 按需（手动敲命令） | 每轮自动（程序调度） |
| **消费者** | 人类（读 txt） | 沙盘引擎（解析 JSON→生成维护命令） |

维护探针是**运行时数据管道**，动作探针是**开发时参考手册**。共享底层 API（`commodity_tracker` / `object_manager` / `relationship_tracker`），但上层消费路径完全不同。

**2026-07-27。**

### 40. 为什么自动动作需要优先级排序 + 反馈闭环？（🆕 2026-07-28）

**问题 1（优先级）：** 此前 `_execute_auto_triggers` 按 `iter_catalog_rules()` 遍历顺序分配——先匹配的规则先挑 sim。同时有水坑（mop_puddle）和垃圾（collect_trash）出现在场上、只有一个空闲 sim 时，遍历顺序决定谁被选中。"修理崩掉的马桶"和"收拾地上的脏衣服"显然不同急。

**决策：** 三级优先级权重（修理 3 > 清洁 2 > 收集 1），先收集全部匹配 → 按规则优先级排序 → 同级按角色适配评分降序 → 高优先先挑 sim。不引入 AI 决策（紧急判断是机械规则，不需要 AI 参与）。

**问题 2（反馈闭环）：** 自动动作推入后，lifecycle tracker 追踪了 verdict，但这个 verdict 从未反馈给自动触发逻辑。同一 sim 连续被 G6（特殊动画状态中）defer 3 次，下轮照样给他分配。`push_history.suggest_rule_fixes()` 是为 AI 规则写的，对自动动作盲。

**决策：** `_read_auto_feedback()` 扫描 push_history 最近 8 条记录 → 同一 sim 连续 2 次自动动作全失败 → 本轮跳过该 sim。仅影响自动动作分配——AI 动作有独立的反馈通道（`rejection_awareness` + `effective_approach`）。阈值常量（`_AUTO_FAILURE_SKIP_THRESHOLD=2`、`_AUTO_FEEDBACK_LOOKBACK=8`）方便后续按实测调参。

**被拒：** 完全禁止失败的 sim（过于激进——可能只是运气不好被 defer）；不区分规则类型（不同规则失败原因不同——修马桶失败 ≠ 收衣服失败，两个独立维度）。

**2026-07-28。**

### 41. 为什么决策层需要三层架构？（🆕 2026-07-28）

**问题：** 此前决策层只有一个 AI 循环 + 自动规则混着跑。`_execute_auto_triggers` 按遍历顺序分配（先匹配先占）、反馈不闭环（失败不学乖）、需求驱动的 `_MOTIVE_OBJECT_RULES` 只在 AI 目录中（AI 不选就干站着）。更根本的问题是：**两层逻辑回答了不同问题，但没有明确的分界**——AI 考虑"谁有故事要讲"，自动规则考虑"场景有什么该干"，混在一起优先级模糊。

**决策：三层架构，每层只回答自己的问题。**

```
P4 生存  _execute_motive_emergency()      程序规则
  问题："谁快不行了？"
  手段：读 bladder 值（唯一纯生理需求，零叙事空间）
  触发：bladder < -80

Tier 1 AI  _call_inner_voice + _call_action_selector  大模型
  问题："谁有故事要讲？"
  手段：读叙事上下文 + 内心声音
  范围：社交/浪漫/需求/物品使用/移动

P3-P1 自动  _execute_auto_triggers()       程序规则
  问题："场景有什么该干的？"
  手段：物品状态匹配 + 角色评分 + 反馈闭环
  范围：修理(3) > 清洁(2) > 收集(1)
```

**汇合点：** `_allocate_actions(motive, ai, auto)` — 生存 > AI > 自动。同一 sim/object 只分配一次。合并后走同一套门控+发射+生命周期管线。

**为什么只有膀胱在生存层？** 饥饿/精力/卫生/舒适都有"怎么满足"的叙事空间——吃什么、在哪睡、泡澡还是淋浴、坐沙发还是躺床上。这些都是 AI 的故事素材，程序不该越界。膀胱是唯一的纯生理反射——上厕所没有叙事选择。

**被拒：** 把 hunger/bladder/energy/hygiene/comfort 全部放进生存层——食物是最明显的反例。饥饿 -90 是生理需求，但吃什么是故事的核心素材，程序不应该替 AI 决定"去冰箱拿剩菜"。让 AI 在 -20（目录出现）和 EA 自主性（真快饿死了）之间有充足的叙事空间。

**2026-07-28。**

### 41b. 三层架构的演进过程（🆕 2026-07-28）

初始直觉是把所有物理需求（hunger/bladder/energy/hygiene/comfort）都放进生存层——"饿到 -90 凭什么还要等 AI"。

但讨论中意识到：**吃东西是享受，也是故事素材。** 饿 -90 不是纯粹的生理反射——"怎么做"有丰富的叙事空间。"斑从冰箱拿出昨天的剩菜，站在厨房一个人吃完"和"真鳕给大家做晚餐，斑和柱间围着桌子聊天"是两件完全不同的事，虽然两个场景都解决了某个人的饥饿问题。

由此提炼出边界：**有没有"怎么做"的故事空间。** 有 → AI 层。没有 → 生存层。这个边界让三层完全正交，没有重叠。

**2026-07-28。**

### 42. 手写 hints 不富化——observer/probe 的语义边界（🆕 2026-07-28）

**问题：** 实机测试发现手动词清洁动作全部 ✅ 完成，但 sim 什么都没做。根因是 observer/probe 在手写 hints 规则上追加了 MOD 调试交互（`Lumpinou_SIT_ObjectDetails_ImmInt_LogObjStates`）——EA 原版 hint 被游戏状态检查拒绝后，重试循环落到被污染的兜底候选 → 虚假成功。

**根本矛盾：** observer 记录"游戏曾经用过什么"（实证），但规则作者的手写 hints 是"我想让 sim 做什么"（意图）。两者语义相反：
- observer 的实证数据 = 游戏自主体用过的 affordance，包括 MOD 调试交互、无意义兜底
- 手写 hints = 规则作者从 Pie Menu / affordance 索引中确认的、语义正确的 EA 原版交互

**决策：** `build_option_from_rule()` 和 `_build_custom_actions()` 只在 `rule.hints` 为空时启用 observer/probe 富化。手写 hints 保持纯净——全被游戏拒绝 → `all_rejected`（真实的失败）比落到无意义兜底（虚假的成功）好。

**富化 = "我不知道该推什么，你帮我找"信号，不是"帮我多加点候选"——** 后者把问题变得模糊，一条坏候选污染整个兜底链。

**同时强化噪音过滤器：** `_FASTLANE_SKIP_SUBSTR` 新增 `logobjstates`/`objectdetails`/`_log_`/`_inspect_`/`dump_` 五个 MOD 调试交互模式。observer 的 `enrich_hints()` 追加候选前也过噪音过滤器——双重防护。

**闭环全景：** 详见 [`docs/action_push_closed_loop.md`](docs/action_push_closed_loop.md)。

**2026-07-28。**

### 43. 被替换 ≠ confirmed——verdict 的分化（🆕 2026-07-28）

**问题：** 动作被游戏自主行为替换后，verdict 仍标记为 `confirmed`。AI 在 prompt 中看到 `✓ 完成`，不知道动作被替换了，下轮可能继续选同类动作。

**决策：** `_end_lifecycle()` 中被替换的动作 verdit 从 `confirmed` 改为 `replaced`：
- `confirmed` = 动作入队、执行、自然结束 —— AI 可以继续选
- `replaced` = 动作入队、开始执行、被游戏自主行为打断 —— AI 应避免重复选
- `unconfirmed` = 动作入队但执行证据不足（含 _rv=2 推测执行）—— 需人工确认

`last_cycle_summary()` 同步更新：replaced 动作的 prompt 注入追加 "⚠️ 下轮避免重复选此类动作"。

**_rv=2 即刻检测回退（2026-07-29 修订）：** `on_action_result` 中 `_rv=2` 在 sim 空闲时推测标记 `first_seen_at`，但从未交叉验证。新增 `_rv2_immediate` 标志 + `_end_lifecycle` 验证：

- **idle sim + push 成功 → 可信。** 空闲 sim + 成功入队 = 动作即刻开始。快照没抓到只因动作太快（<3s），保留为 `confirmed`，标注 `(_rv=2推定)`。
- **busy sim → 降级。** sim 正忙时 `_rv=2` 可能抓到旧动作，我们的 push 排到队尾从未执行。降级为 `unconfirmed`。

配套修复 `_check_execution_continue`：_rv=2 标记（`"[_rv=2] idle→start"`）被快照首次看到实机动作时，不应误判为"动作变了"而提前结束生命周期。检测到 last_observed 是 _rv=2 标记且 current 匹配 → 视为"从推测到实证"，延续追踪并清除 `_rv2_immediate` 标记。

**2026-07-28 → 2026-07-29 修订。**

---
## 13. 角色管理系统（🆕 2026-07-27）

### 13.1 关系护栏系统

关系护栏是沙盘对 Sims 4 关系分数的主动管理机制。背景：AI 叙事过程中角色关系可能偏移出合理范围（友情归零、浪漫爆表、不应发展的关系意外产生）——护栏在三个层面介入。

**架构**：

```
游戏端快照（relation_scores）
     │
     ▼
沙盘 settings.py ──→ 护栏配置（character_relation_guards）
     │                  ├── 角色A → 角色B: {friendship: {min, max}, romance: {min, max}}
     │                  ├── 角色A → *: {romance: {max: 0}}  （通配符）
     │                  └── sweep_interval_seconds: N
     │
     ▼
app.py ──→ 自动清扫开关 + 定时器
     │       每 N 秒检测所有配对 → 越界 → 生成维护命令
     │
     ▼
Maintenance_Command → 游戏端 mythica_maintenance._cmd_set_relation()
     │
     ▼
character_status_panel.py ──→ 手动编辑 UI
     ├── ✏️ 弹窗：双滑块 (友情/浪漫) + 地板/天花板护栏
     └── 标签管理：列表 + ✕删除 + 文本框添加
```

**护栏数据结构**（`Mythica_Sandbox_Settings.json`）：
```json
{
  "character_relation_guards": {
    "扉间": {
      "*": {"romance": {"max": 0}},
      "真鳕": {"friendship": {"min": 30, "max": 100}, "romance": {"min": 0, "max": 80}}
    }
  },
  "relation_sweep_interval": 15
}
```

**三种干预路径**：

| 路径 | 触发 | 函数 | 延迟 |
|------|------|------|:---:|
| 自动清扫 | 定时器到期 | `_sweep_relation_guards()` | N 秒 |
| 手动编辑 | 用户点 ✏️ | `_open_relation_editor()` | 即时 |
| AI 动作 | AI 看到护栏约束 | `{relation_guard_context}` 注入 prompt | 下一轮 |

**通配符语义**：角色 → `*` 的护栏对该角色与**所有其他 sim** 的关系生效。如"扉间 → * 浪漫 cap=0"阻止扉间与任何人发展浪漫关系。通配符优先级低于具体配对——"扉间 → 真鳕"的具体配置覆盖"扉间 → *"。

### 13.2 需求 + Buff 直控

沙盘可绕过 sim 行为链直接操作 sim 的需求值和 buff 状态。

**命令表**：

| 命令 | 游戏端 API | 参数 | 用途 |
|------|------|------|------|
| `set_motive` | `commodity_tracker.set_value(motive, value)` | sim_id, motive_name, value (-100..100) | 直设需求值 |
| `add_buff` | `Buffs.add_buff(buff_name)` | sim_id, buff_name | 添加 buff |
| `remove_buff` | `Buffs.remove_buff(buff_name)` | sim_id, buff_name | 移除 buff |

**UI 入口**：
- ⚡ 全回满按钮：需求折叠区标题行，一键 `set_motive` × 9（全部设为 100）
- buff 增/删：人物状态面板 buff 区——每个 buff 标签右侧 ✕ 按钮（点击即删）+ 底部输入框（输入 buff 名 → `+` 添加 / `−` 删除）

**与动作系统的关系**：
- 直控是**旁路**——不经过动作目录、不影响 sim 队列、不触发动画
- 动作用于**叙事**（"斑饿了去冰箱拿吃的"），直控用于**调试/维护**（"把所有人需求回满"）
- 维护命令走独立信号文件，与动作命令互不阻塞

### 13.3 角色优先级系统

用户可为每个角色指定优先级，影响自动动作的 sim 选择。

**三级优先级**（`settings.py`）：
```python
CHAR_PRIORITY_HIGH = "优先"    # 评分 +15，优先分配维护任务
CHAR_PRIORITY_NORMAL = "正常"  # 默认，无修正
CHAR_PRIORITY_AVOID = "回避"   # 跳过，不分配任何自动动作
```

**评分公式**（`engine._score_auto_candidates()`）：
```
空闲(idle)          +10   优先找没事做的人
有相关技能           +5   修理给有灵巧技能的
同房间               +3   不用跑远路
有相关 buff          +3   "脏乱环境"→优先清洁
用户指定优先         +15   管家、手艺人等
AI 已占用           出局   skip_sim_ids
睡觉/特殊动画状态   出局   硬排除
年龄/技能不满足      出局   硬排除
```

**存储**：`Mythica_Sandbox_Settings.json` → `character_priorities: {name: level}`

**UI**：自动动作 tab → 角色优先级面板——显示在场角色 + ⭐优先 / ·正常 / 🚫回避，点击循环切换。

---

### 13.4 关系标签与近亲阻断（🆕 2026-07-27）

#### 机制

Sims 4 的**近亲浪漫阻断**不是硬编码的"禁止"规则，而是通过 `relation_bits` 标签实现：

```
游戏判断 "A 能否对 B 发起浪漫交互？"
  → 查 A 对 B 的 relation_bits
  → 存在 family_* 标签 → ❌ 阻断
  → 不存在 → ✅ 允许（不判断血缘，只看标签）
```

关键阻断标签（已从探针确认可用）：
- `familyRelationshipBitsAcquired_Target_IsParentOf_Actor` — 父子/父母
- `family_Target_IsDistantRelativeOf_Actor` — 远亲
- 还有兄弟/姐妹等标签，模式同为 `family_*`

#### Mod 实现近亲的方式

所有允许近亲的 Mod（WW、MCCC 等）本质是同样的操作：
- **删标签**：`tracker.remove_relationship_bit(sid, family_bit)` — 把阻断标签移除
- **或全局调优**：改 XML tuning 让浪漫交互不检查 family bits（影响所有人）

#### 沙盘的方案：按对控制

探针确认了 `can_remove_bit: True`。关系编辑弹窗（人物状态 → 关系 → ✏️）会检测两人的 `current_bits` 是否包含 `family_*` 标签：

- 如果存在 → 显示红色警告区 + 🔓 解除亲属限制按钮
- 点击后发送 `remove_relation_bit` 命令到游戏端 → 标签删除 → 浪漫交互即刻可用
- 效果只影响**这一对人**，不全局修改

#### 与浪漫护栏的关系

| 工具 | 作用层 | 效果 |
|---|---|---|
| `remove_relation_bit`（删标签）| 游戏引擎 | 删除 family 阻断 → 浪漫交互可用 |
| `romance_max=0` 护栏 | 沙盘 AI | 沙盘不给这对选浪漫动作 |
| `add_relation_bit`（加"只是朋友"）| 游戏引擎 | 降低自主浪漫概率 |

三者组合提供精细控制：删标签打开可能性 + 护栏限制 AI + 标签影响自主行为。

### 13.5 性取向数据（🆕 2026-07-27，探针结论）

#### 发现

经过三轮探针探测，性取向数据的读写 API 已完全摸清：

**可读**：
| API | 用法 | 返回值 |
|---|---|---|
| `si.get_attracted_genders(pref_type)` | `pref_type=1`（浪漫）/ `pref_type=2`（肉体）| `['MALE']` / `['FEMALE', 'MALE']` |
| `si.get_gender_preference(gender)` | 按性别查偏好 | 待验证 |
| `si.is_exploring_sexuality` | 布尔 | `True`=探索中，未锁定取向 |
| `si.gender` | 枚举 | `Gender.FEMALE` / `Gender.MALE` |

**不可写**：
- `sim_info` 上没有 `set_attracted_genders()`、`set_gender_preference()` 等方法
- `sexual_orientation` 对象不存在于 `sim_info` 上（`None`）
- 性取向是 CAS 创建时设定 + `hidden_traits`（`trait_SexualOrientation_*`）锁定——**非 runtime API 可写**

#### 替代方案

既然直接改取向不行，用已有工具达成同等效果：

| 需求 | 已有工具 | 方法 |
|---|---|---|
| 不让某对人产生浪漫 | `romance_max=0` 护栏 | 沙盘不推浪漫候选 |
| 不让游戏引擎自己产生浪漫 | `add_relation_bit` → `RomanticCombo_JustFriends` | 游戏降低自主浪漫 |
| 允许近亲关系的浪漫 | `remove_relation_bit` → 删 `family_*` 标签 | 移除游戏阻断 |
| 减少 WW 自主求欢 | `romance_max` 护栏 + relation bits | 双重限制 |

**结论**：按配对控制比按性取向一刀切更灵活，且全部 API 已通。不再继续探针性取向写入。

### 17. 为什么先决策后调度，而不是先调度后决策？（🆕 2026-07-28）

#### 问题

初看"先调度后决策"似乎能省 token——调度层先确定哪些 sim 本轮空闲，AI 层只为空闲的 sim 生成候选动作，省掉为已被占的 sim 提案的浪费。而且生存层和自动层之间已经这样做了（`extra_skip_sim_ids`）。

#### 为什么没选

**1. 调度逻辑会被劈成两半。**

先调度后决策意味着"谁被占"这个信息要传两段：一段给自动层（已有），一段给 AI 层（未做）。调度变成了两段式——预占位阶段（AI 前）+ 合并阶段（AI 后）。同一种逻辑散在两个地方，违反对"一个决策一个地方"的原则。

当前方案：调度逻辑**集中**在 `_allocate_actions()` 一处——三个来源各自提案，调度层一次性裁决。

**2. AI 的内心声音需要全景叙事上下文。**

"真鳕憋不住往厕所跑，扉间在修水管，斑自己在客厅——斑有点饿了，去冰箱找点吃的"——AI 需要知道**别人在干什么**才能做好的叙事判断。AI 不是为了自己一个人在真空里做决策，它在扮演家庭成员，其他人在做什么是叙事的一部分。

如果先调度把已被占的 sim 从 AI 视野里抹掉，AI 就失去了场景感。

**3. token 浪费被夸大了。**

当前所谓"浪费"：AI 为已被占的 sim 生成提案 → 调度层丢弃。但这条被丢的候选在 prompt 里只是一行（如"斑 → 去厕所"），目录里 200+ 候选，被丢的两三条占 1-2% 的 token。而 AI 的两次调用（内心声音 + 动作选择）本来就是必跑的——不管调度层怎么排，内心声音都需要全景，动作目录也需要覆盖在场角色。

**4. 每层只答自己那道题。**

| | 当前（先决策后调度） | 备选（先调度后决策） |
|---|---|---|
| 决策层职责 | "谁该干什么"（不关心谁被占） | "谁该干什么，但要跳过已被占的人" |
| 调度层职责 | "合并仲裁"（集中一处） | "预占位 + 合并"（劈成两段） |
| 层间耦合 | 决策层互相不知彼此存在 | AI 层必须知道生存/自动的占位结果 |

当前方案让每层的问题更纯粹——决策层只管"该干什么"，调度层只管"合并"。先调度后决策会让 AI 层多答半道题（"但跳过已被占的人"），调度层多答半道题（"预先告诉 AI 谁被占了"）。

#### 决策

**维持先决策后调度。** 决策层三个来源各自独立提案（生存层/自动层甚至不知道 AI 层的存在），调度层统一合并。AI 为已被占 sim 生成的提案被调度层丢弃——这笔 token 的代价，换来的是调度逻辑集中、AI 叙事全景、每层职责纯粹。

**被拒：** 先调度后决策——调度逻辑分裂、AI 失去场景感、节省的 token 极少。

#### 类比：三个顾问给老板提建议

```
老板先听每个顾问的完整意见（决策层）→ 然后拍板（调度层）

而不是：老板先让两个顾问出门 → 告诉第三个顾问"前两个已经占了真鳕和柱间" → 
        再听第三个顾问的受限意见 → 再拍板
```

前者每个顾问独立表达，老板统一裁决。后者老板的"裁决"被劈成了两段，而且第三个顾问听到的信息是经过老板过滤的，不是完整的。**老板的职责是拍板，不是转述。**

### 18. 为什么 POV 选择需要场景标签感知？（🆕 2026-07-28）

#### 问题

旧版 `_score_pov_candidates()` 是纯冷却驱动：最近说过话的 → 扣分，上轮失败的 → 加分。在一个全是家人、动机各异、场景不断变化的环境里，这种机械打分经常选不到"当前最需要被注意的人"。

**实例：** 深夜 23:00，柱间困得眼睛都睁不开（energy<30），斑在卧室已睡，和树在幼儿房哭。旧版打分可能因为"斑最近没被选"而选斑——但斑正在睡觉，选他是个糟糕的选择。

#### 方案

`_detect_scene_tags(ws, present) → {sim_id: bias}` — 从 WorldState 检测 5 个场景标签（meal_time / social_gathering / crisis / wind_down / morning_rush），给相关 sim 加 10-20 分偏置。纯函数，只读数据，不做 IO。

| 标签 | 触发条件 | 偏置 |
|------|------|:---:|
| `meal_time` | 烹饪中 / 11-13 或 17-20 / ≥2人在厨房 | 烹饪者+15, hunger<30 的+15 |
| `social_gathering` | ≥3 NPC / 吧台使用中 / 音响开着 | social<30 的+10, 靠近吧台/音响的+10 |
| `crisis` | broken/spark/malfunction 物品 | Handiness 技能+20, 在损坏物品房间的+15 |
| `wind_down` | 21-03 点 / ≥2人在卧室 | energy<30 的+15 |
| `morning_rush` | 05-09 点 | hygiene<30 或 bladder<30 各+10 |

#### 为什么不更复杂

1. **数据已有** — 时间、motive、物品状态全在 WorldState 里，不需要新采集
2. **纯函数可测** — 给一个 WorldState 断言输出，16 个 pytest 覆盖全部标签+叠加+边界
3. **标签互不排斥** — 一个场景可以同时是 meal_time + social_gathering
4. **不是代替 AI，是给 AI 更好的起点** — 标签只是改变"谁被注意"的优先级，内心声音和动作选择还是 AI 做

**实机验证：** 30 分钟 13 轮，POV 得分跨度 2~125，每轮选不同人。

### 19. 为什么意图链用简单启发式而不是让 AI 解析？（🆕 2026-07-28）

#### 问题

AI 内心声音说"想先吃东西，再去画画"——第一轮只执行了吃东西，第二轮 AI 可能忘了画画意图。多步计划在跨轮之间会丢失。

#### 方案

`_extract_pending_intent()` — 简单字符串检测：

1. 未行动 → 整段内心声音作为意图保留
2. 有行动 + 含"先/然后/再/接着"等连接词 → 多步计划，整段保留
3. 有行动 + 无连接词 → 单步愿望，做完就了结

下轮 prompt 注入：`【你上轮想做但还没完成的事】…再想想：这件事做了吗？`

#### 为什么不用 AI 解析

1. **不需要** — 让 AI 自己判断"上轮哪些完成了、哪些没完成"不如直接交给它完整上下文。AI 读到"你上轮想做但还没完成的事：想先吃东西再去画画"，它自己就会判断"东西吃过了，画还没画"
2. **零 token 增量** — 纯字符串检测，不额外调 AI
3. **不会出错** — 启发式只有"追踪"和"不追踪"两种结果，不存在"错误解析意图"的风险
4. **仅同 POV 传递** — 不同角色的意图不互相干扰

### 20. 为什么自定义规则表按域拆分？（🆕 2026-07-28）

#### 问题

`_rules_legacy.py` 是 1647 行单体文件，132 条规则（实际比文档里的"55"多了 2.4 倍）。加新规则要在单体文件中翻找合适的注释段，不同域的规则互相穿插。

#### 方案

拆分为 `custom_actions/` 下 7 个子模块，按目标域组织：

| 文件 | 域 |
|------|------|
| `rules_objects.py` | 物品交互（电子/运动/饮食/睡眠/卫生/户外/SPA） |
| `rules_repair.py` | 修理 |
| `rules_social.py` | 社交 sim→sim |
| `rules_romance.py` | 浪漫 + WW 亲密 |
| `rules_motive.py` | 清洁/收集 auto_trigger |
| `rules_skill.py` | 技能门控 |
| `rules_ww.py` | WW 成人模组 |

`__init__.py` 聚合所有子模块为单一 `CUSTOM_ACTION_RULES` 元组，外部消费代码零改动。`_rules_legacy.py` 降为 19 行薄 re-export，后向兼容。

### 21. 为什么 observer 要积累上下文分布而不仅仅是动作名？（🆕 2026-07-29）

#### 问题

`AutonomousObserver` v1 只记录"这个 affordance 被用过几次"——能做实证排序（`sort_by_proven`），但完全不知道在什么条件下用的。推送时，所有画架相关的 affordance 按"实证过/没实证过"二元排序，无论 sim 在哪个房间、什么心情。

游戏自主体在同一条件下反复选同一个 affordance，本身携带了"什么条件适合这个动作"的信息。observer 之前只消费了 `affordance_name` 和 `target_name`，把 `actor_mood`、`actor_location`、`preceding_action` 扔进了人类可读的 note 字段，从未被系统消费。

#### 方案

每次实证累加时，同时累积 sim 的上下文分布到 `_proven` 条目中：

```python
_proven["easel_PracticePainting"] = {
    "count": 15,
    "object_hints": ["easel"],
    "moods": {"Inspired": 12, "Fine": 3},
    "locations": {"书房": 10, "客厅": 5},
    "preceding": {"terrain-gohere": 8, "": 7},
}
```

新增 `score_by_context()` 方法，按当前 sim 状态与历史条件匹配度四级分组重排 hints：

| 等级 | 条件 | 含义 |
|:---:|------|------|
| 0 (黄金) | 实证过 + mood 匹配 + location 匹配 | 完全相同条件 |
| 1 (白银) | 实证过 + 一项匹配 | 部分吻合 |
| 2 (青铜) | 实证过但无上下文匹配 | 条件不同 |
| 3 | 未实证 | 未知 |

插入 hints 管线作为 Layer 4.5（在 `sort_by_proven` 之后、`skill_catalog` 之前），仅对 `hints=()` 规则启用。

#### 设计原则：相关性 ≠ 因果性

"画画时 80% 心情 Inspired"可能是因为画架本身加 Inspired buff，不是"只有 Inspired 才能画画"。所以上下文感知只做软加权（排序加分），不做硬门禁（拒绝推送）。硬门禁应该基于游戏引擎的 affordance 客观属性（如 affordances_index.txt 中的标志位），而非 observer 的被动观察数据。

#### 为什么只用 mood/location 而不用更多维度？

当前选择 mood 和 location 两维是因为它们在快照中始终可用、零额外采集成本。preceding_action 已用于 `needs_gohere_before()`（动作链判断）。后续可加入游戏时间维度（`SceneInfo.time_hm`），等数据积累够后再投入。

### 22. 为什么 gohere 自动插入用 observer 数据驱动而不是硬编码规则？（🆕 2026-07-29）

#### 问题

当 sim 在厨房而画架在书房时，直接推 `easel_PracticePainting` 会失败——sim 走不过去。之前有一个 `_ensure_proximity` 函数给社交动作自动 prepend goto（"两人在不同房间 → 先走过去再对话"），但在 2026-07-28 被注释掉——游戏社交交互自带寻路，prepend terrain-gohere ~40% 被拒。

物品动作的情况相反：没有自带寻路，不插 gohere 就直接失败。但硬编码"所有物品动作都先 gohere"是浪费——sim 已经在画架所在的房间时不需要走过去。

#### 方案

observer 已经积累了每个 affordance 的 `preceding` 分布。新增 `needs_gohere_before(action_name)` —— 检查 gohere 类前置在分布中的占比：

- 阈值：≥60%，且至少 5 次观察
- 满足条件 + sim 不在目标物品所在房间 → 自动 prepend goto

`_ensure_object_proximity()` 在 `engine._run_cycle()` 的分配阶段后调用，与已注释的 `_ensure_proximity`（社交版）对称。goto 动作不进 catalog、不占去重配额。

#### 为什么用 observer 数据而不是物品类型硬编码？

不同物品的"需要走过去"模式不一样。床：sim 通常已经在卧室附近。户外物品：几乎总是需要走过去。observer 的数据是每个 affordance 的真实行为模式——不需要人工枚举规则。

### 24. 为什么社交 target 用 social_group 而不是 posture_target？（🆕 2026-07-29）

#### 问题

`_infer_target_from_char()` 对物品动作能从 `posture_target`（"坐在画架" → target="画架"）推断 target。但对社交动作，posture_target 是空的——sim 在跟另一个 sim 说话时没有姿态挂载物。

v1 直接返回空 target，导致 observer 记录的所有社交 ObservedAction 的 `target_name` 恒为空。

#### 方案

两级回退：

1. `ws.recent_events` — 探针通道的交互事件（最精确，含 target_name + target_id）
2. `char.social_group[0]` — 快照通道的社交组成员（`[{id, name}]`，始终可用，3s 刷新）

`CharacterState.social_group` 由游戏端 `_collect_*_fields` 从 `sim.get_groups_for_sim_gen()` 采集——记录了跟此 sim 在同一个社交组的其他成员。社交交互进行时，交互对象必然在社交组中。

#### 效果

observer 记录的社交动作现在有完整的 target 信息。`observer_to_rules` 能生成 `target_kind="sim"` + `target_name` 的规则。

### 25. 为什么区分六种 target_kind 而不是三种？（🆕 2026-07-29）

#### 问题

最初 observer 只有 `object` / `sim` / `terrain` 三种 target 分类。但 `self` 动作（玩手机、自拍）被错误标为 `object`，`inventory_item` 完全检测不到。

#### 方案

按实际数据源能力区分六种，每种有独立的检测路径：

| target_kind | 检测方式 | observer |
|:--|------|:--:|
| `object` | posture_target 推断物品名 | ✅ |
| `sim` | social_group + recent_events | ✅ |
| `self` | affordance 名 self_/phone_ 前缀，或零外部目标 | ✅ |
| `terrain` | affordance 名 terrain- 前缀 | ✅ |
| `inventory_item` | — | ❌ 不在场景快照中 |

`inventory_item` 是唯一 observer 无法检测的类型——背包物品不出现在场景中。只能靠手工规则，但这是合理的：背包物品种类有限，且每件都需要已知的 affordance 名。

### 26. 为什么上下文感知只排序不门禁？（🆕 2026-07-29）

#### 问题

有了 mood/location 分布数据后，自然会想到"心情不匹配时直接拒绝推送"。但 mood 数据来自被动观察——observer 看到 sim 在 Inspired 时画画 80% 是因为画架本身加 Inspired buff，而不是因为"只有 Inspired 才能画画"。

#### 决策：软加权，不硬门禁

- **mood/location 匹配 → 排前面**（`score_by_context`）
- **mood/location 不匹配 → 排后面**（不影响推送，只是优先级低）
- **不做硬拒绝**（"Angry 时禁止推画画"）

硬门禁的正确数据源是游戏引擎的客观属性（`affordances_index.txt` 中的标志位），而非 observer 的被动观察。

**被拒：** mood 硬门禁——数据来源不对，相关性 ≠ 因果性。

### 44. 为什么 pipeline 过滤在消费层而非采集层？（🆕 2026-07-29）

**问题：** observer 记录所有 `current_action` 变化——包括 SI 中间态、系统状态、反应动作。pipeline 从 40 组候选只提交了 1 条有用规则，其他全是 "NotVisible → Bed"、"carry_Holdobject" 等噪音。

**直觉方案：** observer 采集时就过滤。

**实际方案：** observer 全量保留，pipeline（`observer_to_rules`）消费时过滤。理由：① observer 的 `_proven` 索引用于 `sort_by_proven`/`enrich_hints`/`score_by_context`——数据越多越好，噪音 affordance 在排序/评分场景下无害（它们永远不会被选为 hints）；② 只有规则生成管道需要关注"能不能 push"的语义；③ 全量数据保留在 `observed_actions.json` 中，"缺了再找"时不受影响。

**实现：** `classify_observed_action()` 分类器（`observer_schema.py`）——区分 `pushable` vs `reference`。过滤模式含 SI 中间态（`_continue`、`superinteraction_`）、系统状态（`carry_`、`holdobject`、`NotVisible`）、反应动作（`_Reaction`）、generic_ 非白名单等。噪音模式与 `action_catalog._is_noise_affordance` 对齐（本地副本，避免循环 import）。

**关键修正：** 初版 `"autonom"` 子串误杀 `fridge_GrabSnackAutonomously`——改为 `"autonomous_"` 只匹配前缀形式。

**被拒：** observer 采集时过滤——损害排序/评分/参考查询，且违背"采集层只记录、消费层做决策"的分层原则。

### 45. 为什么推入验证数据要回写 observer？（🆕 2026-07-29）

**问题：** 回路 1（推入反馈）产出因果性数据（推送成功/失败/有效 hints），但只用一次（注入下轮 AI prompt）就丢弃。回路 2（观察学习）只有相关性数据（游戏用过），无法区分"被游戏用过且推送也验证过"和"被游戏用过但推送会失败"。

**方案：** `action_lifecycle._end_lifecycle()` 在 verdict 设定后调用 `observer.on_push_result()`，回写到 `_proven` 索引的 `push_confirmed`/`push_rejected` 字段。`observer_to_rules._confidence_score()` 引入 `push_factor`（确认过 ×1.5，反复失败 ×0.5）。

**数据性质升维：** 回路 2 被动观察 → 相关性（"游戏做过"）。回路 1 主动实验 → 因果性（"我让游戏做了，它做成了"）。联通后 `_proven` 条目同时携带两种数据，pipeline 消费时交叉验证。

**注意：** `on_push_result()` 不做即时 `_maybe_save()`——可能被高频调用（每轮 cycle 多次），交给 `observe()` 的统一节流保存。

**被拒：** 不做回写——因果性数据只用一次是最大的浪费；推入证据独立存储——两份数据分开存会导致 pipeline 消费时多一次 join，且 `_proven` 是自然的数据聚合点。

### 46. 为什么游戏参考弹窗不过滤 reference 动作？（🆕 2026-07-29）

**问题：** `GameReferenceDialog` 用 `query_by_target()` 查询 observer，返回结果包含 reference 类动作（如 `seating_Sit_Bed_Sitonly_Notvisible`）。

**决策：** 不过滤。`GameReferenceDialog` 是给人浏览的——人看到 "seating_Sit_*_Notvisible" 可以自己判断它是姿态过渡而非可推送动作。pipeline 自动生成规则才需要语义过滤。给人看的数据越多越好，给 pipeline 的数据越干净越好。两个消费端的过滤策略不同——`classify_observed_action` 只在 `observer_to_rules` 中调用。

**2026-07-29。**

### 47. 维护命令调试血案与设计经验（🆕 2026-07-29）

维护命令通道（`Maintenance_Command`）从初始设计到全线验证通过，踩了 7 个坑。每个坑的修复都揭示了一个跨端通信的通用模式。

#### 47.1 血案清单

| # | 问题 | 症状 | 根因 | 修复 |
|---|------|------|------|------|
| 1 | 验证函数造假 | `set_funds` 返回 `ok: true, verified: 8779020`，但游戏里钱没变 | `_verify_funds()` 读不到真实值时用目标值当默认值返回 | 读不到返回 `verified: None, warning: "..."` |
| 2 | 回退方法静默失败 | 4 种 `set_funds` 方法都包 `except: pass`，失败全部跳过 | 没日志无法诊断 | 每个方法失败记日志，暴露了 `funds.add()` 缺 `reason` 参数 |
| 3 | 方法优先级错误 | `set_motive` 用 `tracker.set_value()` 先跑，部分动机返回成功但实际值没变（hunger 设 100 → -6.4） | `tracker.set_value` 对某些动机静默不生效 | `motive.set_value()` 提为第一优先 + 设值后验证 `abs(verified - value) < 30` |
| 4 | Key 不匹配 | `add_buff` 沙盘发 `"buff"`，游戏端读 `"buff_name"` → 永远空字符串 | 沙盘/游戏端字段名各自命名未对齐 | 游戏端 `op.get("buff_name", "") or op.get("buff", "")` 双 key 兼容 |
| 5 | 类型对象 vs 字符串 | `add_buff("buff_Happy_High")` → `'str' object has no attribute 'trait_replacement_buffs'` | TS4 `Buff.add_buff()` 接受 Buff 类型对象，不接受字符串 | 三路查找 Buff 类型：自身 buffs → buff_manager → 其他 sim 身上借 |
| 6 | 名字归一化 | `hygiene_hands` 设值失败，但采集能读到 | 采集时做了映射 `hygienehands → hygiene_hands`，设值时用映射后的名字去匹配原始类名 | 设值时同时搜原始名 + 去下划线版本 |
| 7 | 弹窗无法关闭 | relation 编辑弹窗点"应用"后卡住 | `grab_set()` 的 toplevel 直接 `destroy()` 卡死；嵌套函数缺 import 导致 NameError | `grab_release()` → `destroy()` + `try/finally` 确保一定关 |

#### 47.2 通用模式

**模式 A：游戏 API 调用必须验证，不能假定成功。**

```python
# ❌ 调用完就返回 ok
tracker.set_value(motive, 100)
return {"ok": True, "verified": 100}

# ✅ 回读验证
tracker.set_value(motive, 100)
actual = read_back(motive)
if abs(actual - 100) < 30:     # 真的变了
    return {"ok": True, "verified": actual}
# 没变 → 继续试下一个方法
```

**模式 B：游戏 API 接受类型对象，不接受字符串。** Buff、RelationBit 等 TS4 类型需要从 instance manager 或现有对象上查找类型引用，不能直接传字符串类名。

**模式 C：沙盘/游戏端字段名需要防御性兼容。** 两端独立开发时 key 名容易漂移。游戏端解析用 `op.get("buff_name") or op.get("buff")` 双 key 回退，消一端的改动不需要另一端同步部署。

**模式 D：多路回退 + 每路记日志。** `set_funds` 从"4 路静默回退"改为"5 路回退+每路记日志"后，`funds.add()` 缺 `reason` 参数的根因立即暴露。多路回退是必要的（TS4 API 版本差异），但**每路失败必须记日志**，否则问题永远藏在水下。

**模式 E：采集侧的名字映射，设值侧需要反向适配。** 采集时 `hygienehands → hygiene_hands` 是单向映射，设值时必须能逆向匹配。方案：设值时同时尝试原始名和去下划线版本。

#### 47.3 当前维护命令状态（2026-07-29）

| 命令 | 最终状态 | 关键修复 |
|------|:---:|------|
| set_funds | ✅ | 验证去假 + `funds.money=` 直接赋值 |
| set_motive 全回满 | ✅ | `motive.set_value` 优先 + 设后验证 |
| set_age_progress | ✅ | — |
| set_fame | ✅ | — |
| set_relationship | ✅ | 弹窗 grab_release + 缺 import 修复 |
| set_skill | ✅ | fallthrough 到 `skill.set_value` 路径 |
| add_buff | ✅ | 类型对象三路查找 |
| remove_buff | ✅ | 遍历实例匹配 + key 兼容 |
| career | ✅ | 有职业才显示按钮（正常设计） |

**2026-07-29。**

---

### 13.5 游戏自动恢复（🆕 2026-07-31）

#### 动机
沙盘自动循环运行时，游戏频繁因对话框弹出而暂停（食谱选择、WW 邀请、职业事件等）。此前沙盘能通过 `clock_ticks` 检测暂停，但无法自动恢复——游戏会一直卡在对话框等待手动选择。

#### 设计：双防线

```
游戏端 2s alarm                  沙盘端 _ws_flush_tick (2s)
  ├─ _auto_handle_dialog()         ├─ is_game_paused()?
  │   └─ EA UiDialog?              │   ↓ 是
  │       └─ dialog_pick_result    │   暂停 ≥ 15s?
  │           (选第一项，秒级)       │   ↓ 是
  │                                │   发 set_game_speed normal
  └─ 记录 Dialog_AutoPick_Log      │   (兜底所有类型暂停)
```

| 防线 | 覆盖 | 延迟 | 触发条件 |
|------|------|------|---------|
| 游戏端 dialog auto-pick | EA 标准 `UiDialogService` | 秒级 | `_is_sandbox_connected()` 沙盘在线 |
| 沙盘端 auto-resume | **所有暂停**（WW、音乐会、加载等） | 15s | `auto_resume_on_pause=True` |

#### 实现文件

| 端 | 文件 | 改动 |
|----|------|------|
| 游戏 | `mythica_maintenance.py` | +`_execute_set_game_speed`、`_auto_handle_dialog`、`_is_sandbox_connected`、`_write_dialog_log`；dispatch +`set_game_speed` |
| 游戏 | `my_script.py` | 2s alarm 追加 `_auto_handle_dialog()` 调用 |
| 沙盘 | `command_sender.py` | +`send_set_game_speed()` |
| 沙盘 | `app.py` | `_ws_flush_tick` 追加自动恢复逻辑 |
| 沙盘 | `settings.py` | +`auto_resume_on_pause`、+`auto_resume_pause_grace_seconds` |

#### 新维护命令

| op type | 功能 | 参数 |
|---------|------|------|
| `set_game_speed` | 设置游戏时钟速度 | `speed: "normal"\|"paused"\|"speed2"\|"speed3"` |

底层 API：`services.game_clock_service().set_clock_speed(ClockSpeedMode.NORMAL)`

#### 设计决策 #43（WW 对话框放弃）

WW 的 `TurboDialogService` 是创建/显示服务，不跟踪活跃对话框——无法枚举打开的对话框或读取选项。WW 暂停由沙盘 `set_game_speed` 兜底，游戏端不做 WW 特殊处理。被拒方案：绕过 WW `get_dialog_service` 直接 pick——诊断证实无活跃对话框列表。

**2026-07-31。**

### 48. 为什么 observer→rule 管道需要消费 mood/location 数据？（🆕 2026-07-31）

**问题：** `ObservedAction` 的 `actor_mood` / `actor_location` / `preceding_action` 字段一直在 `observed_actions.json` 中积累，但从未被管道自动消费。observer 的 `score_by_context()` 在动作选择时使用这些数据做实时排序（Layer 4.5），但生成的规则本身不携带这些信息——下次 AI 选动作时仍然需要实时查询 observer，且 AI prompt 中看不到"这个动作通常什么心情下做"。

**决策：** 在 `observer_to_rules` 管道中新增 `_extract_dominant()` 函数，从组级别统计中提取主导值（占比 >50%），写入 `CustomActionRule.mood_requires` / `location_prefer`。单条观察时直接从 `ObservedAction` 取值。

**设计原则：** 提取阈值设为 `>50%`（严格大于），50% 恰好对半不视为"主导"。这是为了防止小样本下的假主导（如只有 2 条观察，1 条 Inspired 1 条 Fine，不应标记 Inspired 为主导心情）。

**新增字段：** `CustomActionRule` +`mood_requires: tuple` +`location_prefer: tuple` +`needs_goto: bool` +`preceding_actions: tuple`。

**2026-07-31。**

### 49. 为什么 target_exclude 用两层推断？（🆕 2026-07-31）

**问题：** 自动生成的规则 `target_match` 来自观察到的目标名关键词，但可能存在 confusable 物品（如"easel"匹配到"desk_easel_combo"桌子）。手写规则通过人工标注 `target_exclude` 解决，自动管道也需要等效机制。

**决策：** `_infer_target_exclude()` 两层推断：
1. **组内低频 token**：同一组内所有 `target_definition` 拆分 token，出现频率 <30% 且不在 `target_match` 中的 token → 疑似异类目标 → 加入 `target_exclude`
2. **跨 affordance 竞品**：查询 observer 中其他 affordance 的 `object_hints`，如果某个 hint 匹配我们的 `target_match` 关键词但该 affordance 的其他 hints 不匹配 → 这些"竞品" hints 可能是该物品的其他功能（如"desk"是桌子的特征，不是画架的）→ 加入 `target_exclude`

**噪音过滤：** 数字变体（`01`/`02`）、尺寸标签（`low`/`high`/`single`/`double`）、QA 后缀等从 token 统计中排除。

**2026-07-31。**

### 50. 为什么自动提交阈值需要动态调整？（🆕 2026-07-31）

**问题：** 旧版 `_MIN_CONFIDENCE = 3.0` 是固定阈值——新开存档 observer 数据为空，前几十条观察置信度最高也就 1-2 分，全部被跳过。用户在前几个游戏 session 中看不到任何自动生成的规则，体验等同于"管道不工作"。

**决策：** `dynamic_min_confidence()` 根据 `observed_actions.json` 总条数分段调整：
- <10 条 → 1.0（极宽松）
- 10-30 条 → 1.5
- 30-100 条 → 1.5→3.0 线性插值
- ≥100 条 → 3.0（正常）

数据量少时放宽阈值让新存档更快出现自动规则；数据积累后恢复到正常阈值保证规则质量。管道报告中标注 `🌱 冷启动模式` 让用户知道当前处于宽松期。

**2026-07-31。**

### 51. 为什么 sort_by_proven 要三级而非二元？（🆕 2026-07-31）

**问题：** 旧版 `sort_by_proven()` 是简单的二元排序——实证过的排前面、没实证过的排后面。但回路 1（推入反馈）的因果性数据已经回写到 `_proven` 的 `push_confirmed` / `push_rejected` 字段——这些数据比被动观察更有价值，却未被排序使用。

**决策：** 升级为三级排序：
1. **黄金**：实证过 + push 验证成功（`push_confirmed > 0`）— 游戏用过且我们推过
2. **白银**：实证过但无 push 验证 — 游戏用过但我们没推过
3. **青铜**：未实证 — 完全未知

回路 1→回路 2 联通的直接消费——push 验证过的 affordance 在 hints 排序中获得最高优先级。

**注意：** `push_fail` 数据暂不降级（纯失败的 affordance 仍保留在白银级而非降到青铜级）。理由：push 失败可能是目标物品 state 不对，不是 affordance 本身无效。

**2026-07-31。**

### 52. 为什么 actor_location 存数字房间 ID 而非中文名？（🆕 2026-07-31）

**问题：** 实机测试发现 `location_prefer` 的值全是数字（如 `"32"`、`"14"`），而非人类可读的房间名。

**根因：** 游戏端同时采集两个字段——`room_id`（`build_buy.get_room_id()` 返回数字）和 `room_name`（`sim.room.name` 返回房间名）。但 Sims 4 的房间默认没有名字（玩家必须在建造模式手动命名），`room_name` 在大多数存档中恒为空字符串。

**决策：** 代码使用 `char.room_name or char.room_id` —— 玩家命名过的房间优先显示名字，没有命名的回退数字 ID。数字虽然不可读，但同一房间 ID 一致，匹配功能不受影响。未来可以考虑从房间内物品类型推断房间功能（如有床→卧室、有冰箱→厨房），但那是启发式推断，不属于核心闭环。

**2026-07-31。**
