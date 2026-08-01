# Sims 4 交互系统 — 架构文档

> **作者须知**: 这份文档服务于两个场景——①往沙盘 `custom_actions` 加规则时，需要知道 affordance 从哪来、怎么查证；②往探针系统加新探针时，需要知道交互系统的哪一部分还没被覆盖。
>
> 最后更新: 2026-07-25（探针实测数据校准）

---

## 1. 三层交互模型

Sims 4 的玩家交互入口分四类，但底层机制是同一套 `Interaction` 系统：

```
                      ┌─────────────────────────┐
                      │    Interaction System    │
                      │  (affordance_manager +   │
                      │   interaction_resolver)  │
                      └───────┬─────────────────┘
                              │
        ┌─────────┬───────────┼───────────┬──────────┐
        │         │           │           │          │
    ① Sim→Sim  ② Sim→Object  ③ Ground   ④ System UI
    (社交)     (物品/家具)    (地板)     (手机/日历/笔记本)
```

| 层 | 触发 | 内容示例 | affordance 来源 |
|---|---|---|---|
| **① Sim→Sim** | 点击其他小人 | 聊天/拥抱/打架/亲吻/求婚 | 关系-based + trait-based |
| **② Sim→Object** | 点击物品 | 做饭/看电视/清洁/弹琴/画画 | 物品 tuning + 技能 + 状态 |
| **③ Ground** | 点击地板 | 走到这里/慢跑/坐下 | 地形 tuning |
| **④ System UI** | 左下角按钮 | 打电话/发短信/查日历/写日记 | 独立服务（phone_service 等） |

**② 和 ③ 底层是同一个对象**——地板是特殊的 Object（TerrainObject/PoolObject），同样走 `potential_interactions(obj)`。

**⚠️ 2026-07-25 地面探针结论**：1,389 个地面 affordance **全部是场景特定的**（海滩/潜水/葬礼/节日/骑马/宠物等），无通用"坐地上/慢跑/散步"。**地面层对沙盘动作目录价值为零。** 唯一可推的地面交互是 `terrain-gohere`（已通过 goto 原语处理）。

### 1.1 核心数据指标（2026-07-25 探针实测）

探针在"六道柱间"存档（2370 场景物品、13 在场 Sim）的实测数据：

| 指标 | 数值 | 来源探针 |
|------|------|---------|
| am.types 总条目 | **53,614** | Items §3 |
| 场景可用的 affordance 名 | **5,384**（10%） | Items §3 `scene_aff_names` |
| 场景唯一物品类型 | **277** 种 | Items §1 |
| 社交 super=Y 条目 | **11,029** | Social `AFF_TOTAL` |
| 手机 super=Y 条目 | **1,088**（EA 682 + MOD 406） | System §1 |
| 手机 `auto_autonomous=Y` | **0** | System §5 |

**关键漏斗**：53,614 注册 → 5,384 场景可用（10%）→ 真正对 AI 有意义的 ~40 个核心交互。注册表里 90% 的条目是未加载 DLC / 场景无对应物品 / tuning 引用目标不在当前 zone。

### 1.2 五个 affordance 来源的符号约定

探针 §2 使用 `✅` / `❓` 标注每个 affordance 是否在五来源中找到：

| 标记 | 含义 | 示例 |
|------|------|------|
| `✅` | **静态可得** — 对象 `_super_affordances` 或 `component_super_affordances_gen()` 中直接存在 | `counter_Clean`、`TV_WatchRandomChannel` |
| `❓` | **纯动态** — 五来源均未找到，需运行时通过技能/关系/组件注入 | `bed_Sleep`、`easel_PracticePainting`、`book_Read` |
| ``（空） | 未查证 — 不在 KEY_AFFORDANCE 名单中 | — |

---

## 2. 静态 Affordance vs 动态 Affordance

一台冰箱上的"拿零食"(`fridge_GrabSnack`) 和画架上的"画画"(`easel_PracticePainting`)，来源不同：

### 2.1 静态（Tuning-defined）

在游戏加载时就确定，写入对象的 `_super_affordances` 元组。

```python
obj._super_affordances  # tuple of affordance classes, 不变
```

**特点**:
- 来自 XML tuning 文件，编译为 Python 类
- 不需要 sim 参与即可读取
- `affordances_index.txt` 里 `super=Y auto=Y` 的基本都能 push
- **大多数物品交互属于此类**（counter_Clean、Desk_Clean、bar_Clean、fridge_GrabSnack...）

### 2.2 动态（Runtime-injected）

运行时由技能系统/组件系统/关系系统注入，不在 `_super_affordances` 中。

| 来源 | API | 示例 |
|------|-----|------|
| **组件注入** | `component_super_affordances_gen()` | 画架→Painting skill 注入 `easel_PracticePainting` |
| **运行时提供** | `get_provided_super_affordances()` | 技能升级解锁的新交互 |
| **组件提供** | `_provided_affordances` | 父物品类型注入 |
| **插槽提供** | `slot_provided_affordances` | 坐沙发→"看书"等插槽动作 |

### 2.3 Pie Menu 查询（完整运行时）

游戏弹出菜单时，调用 `sim.potential_interactions(target)` 遍历所有注册的 affordance，对每个执行 `test()`——检查 sim 状态/技能/情绪/关系 + 物品状态/距离/占用 + 场景条件。**通过 test 的才进菜单**。

```
potential_interactions(target)
  ├─ 遍历 affordance_manager.types
  ├─ 每项 test(sim, target, context)
  │    ├─ Sim 检查: 年龄/技能/情绪/关系/buff/队列
  │    ├─ Object 检查: 状态/距离/slot 可用性/占用
  │    └─ 场景检查: 节日/天气/situation/时间
  └─ 通过 → 进 Pie Menu → 可选
```

---

## 3. 探针覆盖对照

| 探针 | 层① | 层② | 层③ | 层④ | 静态 | 动态 | Pie Menu |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `ai_probe_item_interactions` | — | ✅ | — | — | ✅ | ✅ | — |
| `ai_probe_social_interactions` | ✅ | ✅ | — | — | ✅ | — | — |
| `ai_probe_system_interactions` | — | — | — | ✅ | ✅ | — | — |
| `ai_probe_siminfo` | — | — | — | ✅ | — | — | — |
| `ai_probe_affordances` | ✅ | ✅ | — | — | ✅ | — | — |
| `ai_probe_queue_diff` | ✅ | ✅ | — | — | — | — | ✅ |

**已覆盖**: Sim→Self、Sim→Item、Sim→Sim、System UI 四层均有探针。Sim 面板数据（职业/学位/事业/谱系）由 `ai_probe_siminfo` 补全。

**输出格式参考**（三个运行时探针的节号）：

| 探针 | 节号 | 内容 | 对应查询 |
|------|------|------|---------|
| Items | §1 | 物品清单（TYPE 行：类型名/件数/状态/距离） | "场景里有哪些物品" |
| Items | §2 | affordance 映射（`✅`=`static found`, `❓`=`pure dynamic`） | "某个 affordance 对应哪些物品" |
| Items | §3 | am.types 全局查表（LOOKUP 行：SCENE/REGISTRY_ONLY + test_globals） | "注册表里还有哪些相关交互" |
| Social | §1 | PRESENT_SIMS（姓名/年龄/性别/角色/SID） | "在场有谁" |
| Social | §2 | SOCIAL_AFFORDANCES（AFF 行：含 test_globals 全文） | "什么社交可选 + 条件是什么" |
| Social | §3 | RELATIONSHIP_THRESHOLDS（REL 行：f/r 分数） | "关系门槛是否满足" |
| System | §1 | PHONE（按 EA/MOD 分组的手机交互） | "手机能做什么" |
| System | §2-4 | CALENDAR / NOTEBOOK / CLUBS | "系统服务状态" |
| System | §5 | 沙盘可用性分析（自动结论） | "哪些可 push" |

---

## 4. 沙盘写规则指南

### 4.1 标准流程

```
1. grep affordances_index.txt 确认 super=Y auto=Y
2. 跑 ai_probe_item_interactions → §1 看物品类型和数量 → §2 看该 affordance 的 ✅/❓ 状态
3. 根据 type 名写 target_match / target_exclude 关键词（用 §1 TYPE 行的真实类型名）
4. §3 am.types LOOKUP 验证交互在注册表中的完整定义（test_globals / ages / cat）
5. 动作测试页手动实测
6. 确认入队成功 → verified=True → 进 AI 目录
```

### 4.2 关键词匹配原理

沙盘 `match_custom_object` 对 `type` 和 `name` 的拼接做小写子串匹配：

```python
haystack = f"{obj.type.lower()} {obj.name.lower()}"
if any(kw in haystack for kw in rule.target_match):
    # 匹配！(再检查 target_exclude)
```

**Object.type** = `definition.__name__` 去掉 `object_`/`Object_`/`gameobject_`/`GameObject_` 前缀。

### 4.3 常见场景的 target_match 写法

| 物品 | type 真实值 | target_match |
|------|-----------|-------------|
| 台面 | `object_counter*` | `("counter",)` |
| 书桌 | `object_desk` | `("desk",)` |
| 画架 | `paintEaselGEN_01` / `PECO:object_paintEasel*` | `("easel", "画架")` |
| 吧台 | `object_bar*` | `("bar",)` |
| 水槽 | `object_sink*` | `("sink",)` |
| 冰箱 | `object_fridge*` | `("fridge",)` |

> **规则**: 从 §1 TYPE 行拿到真实 type 名，取最稳定的子串（通常去前缀后的第一个单词），再加上中文翻译。

### 4.4 target_exclude 常见陷阱

| 规则 | 必须排除 | 原因 |
|------|---------|------|
| `clean_counter` | `sink, wash, 水槽, bar, 吧台` | "台面" 匹配到水槽→counter_Clean 被拒 |
| `clean_desk` | `front, reception` | frontDesk 是酒店前台，不是书桌 |
| `dance_stereo` | `toilet, 马桶` | talkingToilet 有 speaker 属性 |

### 4.5 affordance 被拒的排查优先级

按探针数据重现频率排序：

```
1. 检查拒因（Sandbox → 动作测试页可见逐候选拒因）
2. IsNotInSexTest?               → 目标 sim 处于特殊动画状态时全局锁社交。Social 探针 11,029 条几乎每条
                                     test_globals 都含此测试
3. SlotTest failed?            → sim 站位被挡，需先 goto 到物品旁
4. SituationRunningTest?       → 该 affordance 需要特定场景上下文（如大扫除）
5. Actor doesn't have traits?  → sim 特质不满足（斑缺调酒特质→bar_TendBar 被拒）
6. skill level not in range?   → 技能等级不够
7. Age restriction?            → Social 探针常见 ages=frozenset({TEEN,YA,ADULT,ELDER})，
                                  幼儿/儿童被多数社交交互排除
8. Relationship threshold?     → f/r 分数不够（陌生人不能求婚等）
9. 完全没有可用候选              → affordance 是动态的（easel_PracticePainting），
                                  _super_affordances 里没有→沙盘按 hints 推→游戏端
                                  try_push 时实际查询→可能还是找到了
10. auto_autonomous=0 (phone)   → System 探针发现 1088 个手机交互全部 auto=N。
                                  手机交互不进入 sim 自主行为，只能手动 push（tgt=ACTOR）
```

### 4.6 动态 affordance 的识别

§2 里标注 `❓` 的 affordance（五来源均未找到）需要特殊处理：

**2026-07-25 实测完整 ❓ 列表**（六道柱间存档，2370 物品/277 类型）：
> 图书馆升级（2026-07-25 17:10）：53,614 条全部带 `test=` 字段。❓ 动态 affordance 虽然不在对象身上，但 test_globals 可从图书馆直接读取。下面标注了图书馆查到的标志位。

| Affordance | 说明 | 图书馆查证 | 实测状态 |
|------------|------|-----------|---------|
| `clean_ScorchMarks` | 清洁烧痕 | — | ❓ 地面 Effect |
| `fridge_GrabSnack` | 拿零食 | **MOD only**（EA 原版无此命名），多个 MOD 变体 | ❓ |
| `computer_VideoGaming_WatchStreams` | 看游戏直播 | **`super=Y auto=Y user=Y`** 🥇 | ❓ 但图书馆有完整 test= |
| `easel_PracticePainting` | 画架画画 | **`super=Y auto=Y user=Y`** 🥇 | ❓ Painting skill 组件注入 |
| `toilet_Use` | 上厕所 | — | ❓ bladder motive 驱动 |
| `bed_Sleep` | 睡觉 | 多个变体，全含 `IsNotInSexTest` | ❓ energy motive 驱动 |
| `sofa_Sit` | 坐沙发 | — | ❓ posture 系统注入 |
| `book_Read` | 看书 | — | ❓ 需 inventory 里有书 |
| `bookshelf_Open` | 打开书架 | — | ❓ 容器交互 |
| `chess_Play` | 下棋 | — | ❓ Game 组件 |
| `dart_Play` | 飞镖 | — | ❓ Game 组件 |
| `treadmill_Run` | 跑步机跑步 | — | ❓ Fitness 技能组件 |
| `punchingBag_Punch` | 打沙袋 | — | ❓ Fitness 技能组件 |

**图书馆 vs 探针的分工**（2026-07-25 升级后）：

| 数据源 | 覆盖 | test= 字段 | 场景存在性 |
|--------|------|-----------|-----------|
| `affordances_index.txt` | **53,614**（全量） | ✅ 全文 | ❌ 不知道 |
| `Interaction_Probe_Items.txt` | **5,384**（场景相关） | ❌ 无（需 §3 LOOKUP） | ✅ §1 TYPE + §2 ✅/❓ |
| `Interaction_Probe_Social.txt` | **11,029**（社交） | ✅ 全文（§2 AFF 行） | ✅ §1 SIM（在场） |

**新工作流**：对 ❓ 动态 affordance，先 grep 图书馆看 `test=` + `auto=` + `user=` → 如果图书馆显示 `super=Y auto=Y user=Y`（如 `computer_VideoGaming_WatchStreams` 和 `easel_PracticePainting`），则 hints 写精确类名即可——游戏端 `push_super_affordance` 可以推动态注入的交互。图书馆的 `test=` 字段让 ❓ 不再是完全的黑箱。

**规律**：大多数 `❓` 属于以下三类之一——
1. **Motive-driven**（需求驱动）：Sleep/Toilet/Sit——bladder/energy/comfort 低时动态出现
2. **Skill-component**（技能组件注入）：Paint/Run/Punch——技能 tracker 激活后由组件注入
3. **Container/Inventory**（容器/物品栏）：Bookshelf/GrabSnack——内容物决定可用的交互

处理方式：
- hints 链只能放这一条（动态生成的不支持链式回退）
- 可能需要特定 sim 状态（技能等级/情绪/关系）才能 test 通过
- 游戏端 `push_super_affordance` 最终能否推入取决于实际运行时状态
- **建议**：对 ❓ affordance 先跑 `ai_probe_queue_diff`（手动做一次动作→差分出实际入队的类名），再写进 hints

---

## 5. 与沙盘动作系统的关系

```
沙盘端                         游戏端
─────                         ─────
action_catalog.py               mythica_action.py
  ├─ 生成 ActionOption           ├─ 解析 Action_Command
  │    ├─ action_type: push      │    ├─ 按 action_type 路由
  │    ├─ target_kind: object    │    ├─ 按 target_kind 解目标
  │    ├─ affordance_hints: []   │    └─ _try_push_candidates()
  │    └─ target_id: obj_id      │         ├─ 用 hints 构建候选
  │                              │         ├─ push_super_affordance
  │                              │         └─ 回传 /action_result
  │                              │
  │    hints 来源:               │    hints 执行:
  │    ① _rules_legacy.py 手写  │    ① 逐条 try-push
  │    ② 物品快车 aff_names     │    ② 首成即停
  │    ③ 自主观察器排序         │    ③ 全拒回传+不降级
```

**关键**: hints 的正确性 = 规则质量。错误的 hints → 全拒 → 浪费一轮。探针 §2+§3 就是为减少这个而存在的。

---

## 6. 三域交叉发现（2026-07-25 探针对照）

将三个运行时探针数据放在一起看，暴露出的架构规律：

### 6.1 交互的"三态"分类

| 态 | 定义 | 存储位置 | 例子 |
|---|------|---------|------|
| **静态** | tuning 加载后不变 | `am.types`（53,614 条） | 所有交互的"配方" |
| **半动态** | 同一次游戏中缓慢变化 | 物品 states / Sim traits+buffs / 关系分数 | 脏/干净、技能等级、f/r 值 |
| **运行时** | 每帧可能变化 | sim queue / object in_use_by / WW sex context | 正在做爱→IsNotInSexTest 激活 |

**为什么半动态最麻烦**：test_globals 在静态 tuning 里，但 test_globals 引用的状态（关系分数、buff 有无、技能等级）是半动态的。同一个 affordance 推两次——第一次 traceback `RelationshipTest failed`，第二次可能就过了（关系刚好涨过门槛）。

### 6.2 物品 vs 社交：两条不同的 test_globals 栈

从三个探针的 `test=` 字段可以看出两类交互的条件系统差异：

```
物品 test_globals（简单）:
  TestList([SimInfoTest(ages=...), InUseTest(...)])
  → 通常只有 2-3 个条件：年龄 + 物品占用 + 距离

社交 test_globals（复杂）:
  TestList([
    IsNotInSexTest,                        ← WW 全局锁
    SimInfoTest(ages=frozenset({...})),     ← 年龄门
    RelationshipTest(...),                  ← 关系门槛
    BuffTest(blacklist={buff_GettingMarried}), ← Buff 黑名单
    TraitTest(blacklist_traits=(...)),      ← 特质排除
    ...
  ])
  → 通常 5-8 个条件，连锁叠加
```

**结论**：社交动作的拒因远比物品动作复杂——推社交之前，先 grep Social 探针看目标 sim 对之间的 test_globals 全文，确认条件满足。

### 6.2.1 浪漫/刻薄 mixer 壁垒（2026-07-25 发现）

**EA 原版不存在通用的 super=Y 浪漫或刻薄社交交互。** 53,614 条图书馆全量审计结论：

| 语气 | super=Y 通用 | super=N (mixer) |
|------|:-----------:|:---------------:|
| 友好 | `sim_Chat`、`sim_BroHug_QuickSocial`、`sim_HighFive_QuickSocial` | 大量 |
| 浪漫 | **0** | 全部（Flirt/Compliment/Seduce...） |
| 刻薄 | **0** | 全部（Argue/Insult/Yell...） |

EA 把通用浪漫和刻薄全部实现为 `super=N` 的 mixer——它们必须在一个运行的社交 super interaction 内部才能被游戏选中。

### 6.2.2 mixer 推入的实测发现（2026-07-25 三大发现）

**发现 1：`push_super_affordance` 接受 super=N mixer** ✅

```
实测: push_super_affordance(mixer_social_Flirt_targeted_romance_alwaysOn, target)
→ EnqueueResult: True <ExecuteResult: True>  ← 入队成功，开始执行！
```

三次独立实测（六道柱间→斑、真鳕→斑、千手柱间→斑）均确认：`push_super_affordance` **不检查 `super_affordance` 标志**。已有的"mixer 推不动"文档说法是错的（可能是旧版本行为或从未实证过）。

**发现 2：mixer 入队成功但不等于实际运行** ❌

```
ExecuteResult: True  ← 入队且开始执行
但游戏画面中 sims 没有做出调情动作——行为出现又立刻消失
```

根因：`super=N` 的 mixer 在架构上被设计为**子交互**——它必须附着在一个运行的父社交 super interaction（如 `sim_Chat`）内部才能持续执行。单独推 mixer → 游戏创建它 → 找不到父 SI → **启动瞬间销毁**。这是 EA 引擎层的设计约束，不是 bug。

> 这解释了游戏中常见的一个现象：**明明有交互图标闪现，但立刻自动取消**——就是因为 mixer 被推入但没有父 SI 承载。

**发现 3：双推解法** ✅

```
1. 先推 sim_Chat (super=Y)  → 建立 social context（父 SI 开始运行）
2. 再推 mixer_social_Flirt  → mixer 找到运行中的 sim_Chat → 附着 → 真正执行调情动作
```

2026-07-25 在 `mythica_action.py` `_try_push_candidates` 实现：检测 `mixer_` 前缀候选 → 先推 `sim_Chat` 建立父容器 → 再推 mixer。这样 mixer 找到了父 SI，可以正常完成整个调情动画和关系效果。

**沙盘当前策略**：友好社交推 `sim_Chat`/`sim_BroHug_QuickSocial`（super=Y，天然有父容器）。浪漫/刻薄也走 `sim_Chat` + mixer 双推——AI 可以精确控制社交语气。

### 6.3 手机交互的特殊地位

System 探针发现手机是"第三极"：
- 1,088 个 super=Y affordance（量级与物品+社交同级）
- **0 个 auto_autonomous**——全部是 UI 层交互，不进入 sim 自主行为
- EA vs MOD 比例 682:406（MOD 占了 37%，显著高于物品域）
- 大部分需要两步：打开手机面板 → 选择子项

**沙盘策略**：手机交互不再作为独立动作目录条目。如需 AI 打电话/发短信，把对应 `phone_*` affordance 作为 tgt=ACTOR 的 push 动作加入 custom_actions 即可。

### 6.4 第三方 Mod 的架构影响

探针数据反映了一些第三方 mod 对交互系统的修改。例如几乎所有社交 affordance 都注入了 `IsNotInSexTest` 测试。沙盘策略：推任何社交动作前，通过快照字段确认目标 sim 不在特殊动画状态中。
