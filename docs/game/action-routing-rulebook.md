# 动作注入路由规则书 — 不再盲猜

> **什么时候读：** 每次要给沙盘加一个新动作时，翻开照着做。
>
> **数据来源：** 53,614 条 affordance 的统计分析（标志位组合 × 命名前缀 × 类别映射 × 目标类型），
> 加上 `mythica_action.py` 74 条自定义规则的实战经验。
>
> **姊妹篇：** [sims4_game_architecture.md](sims4_game_architecture.md)（游戏架构——理解全局）、
> [sims4_action_injection.md](sims4_action_injection.md)（API 细节 + 血案——理解 push 机制本身）。

---

## 规则 0：Pie Menu 观察法（第一步，永远先做）

> **游戏自己的 Pie Menu 是最权威的"流程文档"。** 不需要 grep 53K、不需要猜 test_globals、不需要 queue_diff——游戏已经在菜单里告诉你了正确答案。

### 方法

```
1. 暂停游戏，点一个 sim
2. 点击目标物品/Sim → 看 Pie Menu 弹出什么选项
3. 逐层点进去 → 每一步都记住选项名
4. 用 ai_probe_aff_detail <选项名> 在游戏控制台查 affordance 类名
5. 把这条流程链复制到 hints
```

### 案例：画架

```
Pie Menu 流程:
  点击画架（上面有画）
    → "舍弃画作"                      ← ai_probe_aff_detail 舍弃画作 → canvas_scrapPainting
    → "画画…" → "练习绘画"            ← ai_probe_aff_detail 练习绘画 → easel_PracticePainting

→ hints=("canvas_scrapPainting", "easel_PracticePainting")

对照: 我们之前试了 easel_PracticePainting(单条→❓被拒)、Put_Away_PaintingCanvas(需PaintingProgress→拒)。
      Pie Menu 直接告诉我们: 正确的前置动作是 canvas_scrapPainting，不是 Put_Away_PaintingCanvas。
      五条命令的试错 vs 点两下鼠标。
```

### 案例：音响跳舞

```
Pie Menu 流程:
  点击音响（关机状态）
    → "开启"                           ← stereo_TurnOnAndListen
    → "跳舞"                           ← stereo_Dance

→ hints=("stereo_TurnOnAndListen", "stereo_Dance")
```

### 和 grep/queue_diff 的分工

| 方法 | 什么时候用 | 准确度 |
|------|-----------|--------|
| **Pie Menu 观察** | **第一步**。你知道目标物品是什么，直接在游戏里点 | 100%（游戏自己展示的） |
| grep affordances_index | Pie Menu 选项太多需要过滤（如电脑 100+ 选项） | 静态属性准，但不知道哪个是真流程 |
| queue_diff | 动态 affordance（❓），Pie Menu 有时候不显示 | 100%（游戏实际推的） |

**铁律：Pie Menu 里有的一律用 Pie Menu 的。Pie Menu 没有的才动 grep 和 queue_diff。**

> 📋 **记录一次，永不再点**：观察到的流程写入 [pie_menu_flows.md](pie_menu_flows.md)（人读）+
> [`pie_menu_catalog.py`](../mythica_sandbox/pie_menu_catalog.py)（代码读）。加新动作前：
> ```python
> from mythica_sandbox.pie_menu_catalog import find_flows_for
> find_flows_for("easel")  # → 已有流程？直接复制 hints 链
> ```

---

## 快速开始：架构感知决策树

> **旧版 7 步（已废弃）**：物品/社交/自指三种完全不同的插入场景塞进同一条管道——grep 53K 条目效率低、❓ 动态 affordance 白费功夫、社交没做 test_globals 预检。
>
> **新版决策树**：第一步判域，不同域走不同探针+不同查证路径。

```
新动作请求
│
├─ ① 判域："这个动作的目标是什么？"
│   ├─ "对物品做"       → 物品域 → 走路径 A
│   ├─ "对另一个 Sim 做" → 社交域 → 走路径 B
│   ├─ "对自己做"        → 自指域 → 走路径 C
│   └─ "走到哪里"        → 移动域 → 走路径 D
│
├─ ② 探针预检（取代 grep 53K）
│   根据域查对应的探针输出文件，确认：
│   - 物品域：目标物品在场景中吗？（Items §1）affordance 是 ✅ 还是 ❓？（Items §2）
│   - 社交域：目标 Sim 在场吗？（Social §1）test_globals 条件可能满足吗？（Social §2）
│   - 自指域：affordance 在 am.types 中吗？tgt=ACTOR？
│   - 移动域：目标可达吗？
│
├─ ③ 查 affordances_index（仅在②确认场景存在后）
│   验证标志位: super=Y? auto=? user=? ages=?
│
├─ ④ 写规则
│   按域的 hints 模板填写（见各域子流程）
│
└─ ⑤ 动作测试页实测 → 看拒因 → 调整 → verified=True
```

### 路径 A：物品域

```
1. grep Items 探针 §1 TYPE 行 → 确认目标物品类型在场景中的真实 type 名
   例: grep "computer" Interaction_Probe_Items.txt
       → object_computerDesktopRWSpecWCool_01 (4台)

2. grep Items 探针 §2 → 看目标 affordance 是 ✅ 还是 ❓
   例: grep "computer_PlayGame" Interaction_Probe_Items.txt
       → ✅ 静态可得 → 正常流程，hints=()
   例: grep "easel_PracticePainting" Interaction_Probe_Items.txt
       → ❓ 纯动态 → grep 图书馆看 test= 和 auto=:
         $ grep "^easel_PracticePainting " affordances_index.txt
         → super=Y auto=Y user=Y → hints=("easel_PracticePainting",) 单条即可
   例: grep "fridge_GrabSnack " affordances_index.txt
       → 无精确匹配（EA 原版无此命名，全是 MOD 变体）
       → 跑 queue_diff 拿实际入队类名

3. 用 §1 的 type 名写 target_match（取最稳定子串）
   例: "computerDesktopRWSpecWCool_01" → target_match=("computer", "laptop")

4. hints 默认留空 () → 快车（物品 affordance_names）+ 观察器自动填充
   仅在需要意图保真时手写 hints（自主变体优先）

5. 写规则:
   CustomActionRule(
       rule_id="play_game",
       action_type="push", target_kind="object",
       hints=(),                              # 快车自动填
       target_match=("computer",),
       verified=False,
   )
```

### 路径 B：社交域

```
1. grep Social 探针 §1 PRESENT_SIMS → 确认目标 Sim 在场
   例: grep "宇智波 斑" Interaction_Probe_Social.txt

2. grep Social 探针 §2 SOCIAL_AFFORDANCES → 看候选 affordance 的 test_globals 全文
   重点关注:
   - IsNotInSexTest? → 目标 sim 处于特殊动画状态 = 必定被拒
   - ages=frozenset({...})? → 幼儿/儿童被多数社交交互排除
   - RelationshipTest? → f/r 分数必须过门槛
   - BuffTest(blacklist={buff_GettingMarried})? → 婚礼中锁某些选项

3. 用社交类别而非 sim_ 前缀搜索 affordances_index:
   # ✅: grep "cat=sim_Friendly" affordances_index.txt | grep "关键词"
   # ❌: grep "sim_.*关键词" affordances_index.txt  (65% 不是社交)

4. hints 用 social_ 前缀，不用 sim_:
   hints=("social_Friendly_TellJoke",)  # ✅
   hints=("sim_TellJoke",)              # ❌ 不可靠

   ⚠️ mixer（super=N）需双推：`mixer_` 前缀的 hints 会被游戏端自动识别，
   先推 `sim_Chat` 建立父 SI 容器，再推 mixer。单独推 mixer 会入队后立刻销毁
   （无父 SI 承载）。参见 [sims4_interaction_system.md §6.2.2](sims4_interaction_system.md#622-mixer-推入的实测发现2026-07-25-三大发现)

5. 写规则:
   CustomActionRule(
       rule_id="tell_joke",
       action_type="push", target_kind="sim",
       hints=("social_Friendly_TellJoke",),
       verb="{actor} 给 {target} 讲了个笑话",
       verified=False,
   )
```

### 路径 C：自指域

```
1. grep affordances_index.txt → 确认 tgt=ACTOR, super=Y
   例: grep "tgt=ACTOR" affordances_index.txt | grep "phone.*Chat"

2. 注意：手机交互全部 auto=N（System 探针 §5）
   → 可以 push，但不会进入 sim 自主行为

3. hints 写单一 affordance，目标为 sim 自身:
   hints=("phone_Chat",)  → target_kind="self"
   或 target_kind="sim", target_id 指向自己（等价）

4. 写规则:
   CustomActionRule(
       rule_id="phone_chat",
       action_type="push", target_kind="self",
       hints=("phone_Chat",),
       verb="{actor} 打电话聊天",
       verified=False,
   )
```

### 路径 D：移动域

```
1. 不需要 grep affordances——goto 原语直接用

2. 判断目标类型:
   - 走到 sim 身边 → target_kind="sim"
   - 走到物品旁 → target_kind="object" + target_match
   
3. hints 默认空 () → jog 被拒自动落 gohere

4. 写规则:
   CustomActionRule(
       rule_id="go_to_fridge",
       action_type="goto", target_kind="object",
       hints=(),                              # 空 = 默认 gohere
       target_match=("fridge",),
       verb="{actor} 走到冰箱旁",
       verified=False,
   )
```

### 决策树速查卡

| 你的问题 | 走哪条路 | 探针预检 | hints 策略 |
|---------|---------|---------|-----------|
| "我想用电脑/电视/音响/健身器材做 X" | **路径 A** 物品域 | Items §1+§2 | `()` 留空，快车填 |
| "我想跟某人聊天/调情/打架/拥抱" | **路径 B** 社交域 | Social §1+§2 | 手写 `social_*` |
| "我想打电话/写日记/看手机" | **路径 C** 自指域 | affordances_index | 手写单条 |
| "我想走过去" | **路径 D** 移动域 | 不需探针 | `()` 空 |

### 写完规则之后：观察器自动接手

规则写好 (`verified=True`) 进入 AI 目录后，**`AutonomousObserver`** 自动接管优化：

```
1. 游戏端每 3s 发快照 → 沙盘解析 WorldState
2. observer.observe(ws) → 扫描所有 sim 的 current_action
   → 发现"fridge_GrabSnack 被游戏自主用了 5 次"
3. 下轮 generate_action_catalog → sort_by_proven(hints)
   → fridge_GrabSnack 被排到 hints 最前面
4. AI 选动作 → push → 成功率提高（因为排最前的是游戏实证过的）
```

**这意味着：你不需要手写每一个 affordance 变体。** 规则搭好框架（`target_match` 精准），快车覆盖所有型号，观察器自动发现哪个型号最好用——系统随时间越来越准。

详见 [sandbox_action_insertion.md §2](../mythica_sandbox/docs/sandbox_action_insertion.md#2-自主动作观察器--读写闭环)。

---

## 规则 A：从"想做什么"到"交互叫什么名"

### A.1 社交动作 → grep 社交类别

```
grep "sim_Friendly\|sim_Romantic\|sim_Mean\|sim_Mischief" affordances_index.txt | grep "关键词"
```

**案例：** 加一个"讲笑话"的动作

```bash
$ grep -i "joke\|funny\| humor" affordances_index.txt | grep "super=Y.*user=Y"
social_Friendly_TellJoke | super=Y auto=Y user=Y | tgt=TARGET | cat=sim_Friendly | ages=CHILD,TEEN,YOUNGADULT,ADULT,ELDER
```

→ target_kind="sim"（因为 tgt=TARGET），hints=("social_Friendly_TellJoke",)

### A.2 物品交互 → grep 物品类型关键词

```
grep "物品类型名" affordances_index.txt | grep "super=Y"
```

**案例：** 加一个"用电脑写小说"的动作

```bash
$ grep -i "computer.*write\|computer.*novel\|computer.*writing" affordances_index.txt | grep "super=Y.*user=Y"
computer_Writing_WriteNovel | super=Y auto=N user=Y | tgt=OBJECT | cat=computer_Writing | ages=TEEN,YOUNGADULT,ADULT,ELDER
```

→ target_kind="object"（因为 tgt=OBJECT），hints=("computer_Writing_WriteNovel",)

### A.3 移动动作 → 用 goto 原语

移动动作不需要 affordance 名——`goto` 原语直接导航到目标。但需要指定 target_kind。

```
goto + target_kind="sim"   → 走到 sim 身边（底层用 terrain-gohere + PickInfo）
goto + target_kind="object" → 走到物品旁边
```

hints 可以空（默认 gohere），也可以指定"先尝试 jog，被拒再走路"的链。

### A.4 自指动作 → 查 tgt=ACTOR

```
grep "tgt=ACTOR" affordances_index.txt | grep "super=Y.*关键词"
```

---

## 规则 B：标志位——什么能推、什么不能

### B.1 硬门：super=Y

**super=Y 才能 push_super_affordance。super=N（mixer）推不动。**

53,614 条中：
- super=Y：42,606（79.5%） ✅ 可推送
- super=N：11,008（20.5%） ❌ 不可推送

### B.2 可靠性分级

| 优先级 | 标志组合 | 数量 | 什么时候用 |
|--------|---------|------|-----------|
| 🥇 首选 | super=Y, auto=Y, user=Y | 11,839 | 最可靠，在 sim 的 `_super_affordances` 上，直接可用 |
| 🥈 次选 | super=Y, auto=Y, user=N | 3,322 | 可靠但需全局查找（不在 sim 的可见列表上） |
| 🥉 可用 | super=Y, auto=N, user=Y | 23,731 | 可推，但需全局查找 |
| ⚠️ 谨慎 | super=Y, auto=N, user=N | 3,714 | 系统专用交互——确认语义后再推 |

**关键认知：auto 和 user 不影响 push 能力，只影响"在哪能找到这个 affordance"。**

- `user=Y`：在 sim/object 的 `_super_affordances` 元组上（pie menu 可见）
- `user=N`：不在可见列表上，必须从 `affordance_manager().types`（53K 条目 dict）全局查找
- `auto=Y`：游戏 AI 可以自主触发，说明此交互设计上对所有条件都做了充分处理
- `auto=N`：仅玩家点击或系统触发，可能缺少某些条件检查

### B.3 实战决策树

```
要推的 affordance:
  ├─ super=N → ❌ 放弃。queue_diff 找替代 super=Y 的交互
  └─ super=Y
       ├─ user=Y → 优先用 sim._super_affordances 查找（快）
       └─ user=N → 必须用 affordance_manager().types 全局查找（慢但可用）
            └─ 名字含下划线（如 sim_Chat）→ 全局查找可命中
            └─ 名字不含下划线 → 可能找不到，考虑 queue_diff 重新发现
```

---

## 规则 C：命名前缀 → 目标类型

从 53K 条目中统计分析得出：

| 前缀 | 匹配数 | 目标类型 | 可靠性 |
|------|--------|---------|--------|
| `social_*` | 2,867 | TARGET | **94.3%** |
| `object_*` | 776 | OBJECT | **99.1%** |
| `sim_*` | 341 | 混（35% TARGET, 34% OBJECT, 30% ACTOR） | ❌ **不可靠** |
| `computer_*` | — | OBJECT | **~95%** |
| `phone_*` | — | ACTOR | **~85%** |
| `stereo_*` / `tv_*` / `easel_*` 等 | — | OBJECT | **~98%** |
| MOD 命名空间（`Alchemist:` / `ChanChan:` 等） | — | 查 tgt= | 查索引 |

### C.1 `sim_` 前缀陷阱

`sim_` **不意味着社交动作**。它只是 EA 的命名惯例——"这是一个 sim 可以做的动作"。

```
sim_Chat             → tgt=TARGET（社交）    ✅
sim_Stand            → tgt=ACTOR（自指）     ← 不是社交！
sim_ProtestChat      → tgt=TARGET（社交）    ← 但也不是普通聊天！
sim_PracticeSpeech   → tgt=OBJECT（用镜子）  ← 不是社交！
```

**任何以 `sim_` 开头的交互，必须查 tgt= 字段确认目标类型。不查就猜 = 盲猜。**

### C.2 社交动作首选 `social_` 前缀

如果你想让 sim 做一个社交动作，**优先搜索 `social_` 前缀**，不要搜 `sim_`：

```bash
# ✅ 正确
grep "^social_.*关键词" affordances_index.txt

# ❌ 错误——结果里 65% 不是社交动作
grep "sim_.*关键词" affordances_index.txt
```

---

## 规则 D：Pie Menu 类别 → 目标类型

当命名前缀不可靠时，用类别判断。**类别比名字更可靠。**

| 类别模式 | → target_kind | 可靠性 |
|---------|---------------|--------|
| `sim_Friendly` / `sim_Romantic` / `sim_Mean` / `sim_Mischief` | `"sim"` | ~94% |
| `pieMenuCategory_Upgrade` / `pieMenuCategory_Getaway` | `"object"` | ~97% |
| `phoneCategories_*` | `"self"` | ~85% |
| `computer_*` 类 | `"object"` | ~98% |
| `cat=-`（无类别，33,150 条） | 查 tgt= 字段 | — |

---

## 规则 E：Hints 链设计

### E.1 Hints 是"同语义兜底链"

```python
# ✅ 正确：同一动作的不同设备/变体
hints=("stereo_TurnOnAndListen", "stereo_TurnOn", "stereo_ListenToMusic")

# ✅ 正确：同一动作的不同难度/强度
hints=("WorkoutMachine_Workout", "WorkoutMachine_EpicWorkout", "WorkoutMachine_PushTheLimits")

# ❌ 错误：不同动作混在一起
hints=("stereo_TurnOn", "stereo_Dance")  # 开音响 vs 跳舞——完全不同的动作
```

### E.2 Hints 链的排序

1. **最精确的放前面**（特定设备/特定变体）
2. **通用的放后面**（基类/兜底）
3. **hints 全拒不降级**——不会自动 fallback 到默认名单。这是意图保真设计

### E.3 已有规则的 hints 案例（来自 custom_actions.py）

```python
# 看电视：特定频道 → 通用频道
("TV_WatchRandomChannel", "tv_WatchCurrentChannelAutonomously")

# 跳舞：通用音响 → DJ 台
("stereo_Dance", "DJBooth_stereo_Dance")

# 健身：普通 → 史诗 → 极限
("WorkoutMachine_Workout", "WorkoutMachine_EpicWorkout", "WorkoutMachine_PushTheLimits")

# 洗澡：淋浴 → 浴缸 → 蒸汽房
("shower_TakeShower", "bathtub_TakeBath", "steamRoom_UseSteamRoom")
```

---

## 规则 F：物品匹配（target_match / target_exclude）

物品类动作需要告诉系统"匹配什么物品"。两个字段控制：

### F.1 target_match：命中关键词

关键词按物品的 `type` 和 `name` 字段小写匹配。**用游戏中实际出现的类型名，别猜。**

```python
# 电脑 → 匹配名称含 computer 或 laptop 的物品
target_match=("computer", "laptop")

# 音响 → 匹配 stereo、speaker、boombox
target_match=("stereo", "speaker", "boombox")

# 画架 → 匹配 easel
target_match=("easel",)
```

### F.2 target_exclude：排除误匹配

当物品名含匹配词但语义不对时排除：

```python
# talking toilet 会匹配 "stereo" 关键词（它有 speaker 功能）
target_match=("stereo", "speaker")
target_exclude=("toilet",)  # ← 防误匹配
```

### F.3 查证物品类型名

```
grep "物品名" affordances_index.txt | head -5
# 看 tgt=OBJECT 的交互的 cat= 字段 → 推断物品类型
```

或直接在游戏中用 `ai_probe_objects` 看 Zone 里所有物品的 name/type。

---

## 规则 G：按动作类型的完整填入模板

### G.1 社交动作（sim 对 sim）

```python
CustomActionRule(
    rule_id="tell_joke",
    label="😄 讲笑话",
    action_type="push",         # push affordance
    target_kind="sim",          # 目标是另一个 sim
    hints=("social_Friendly_TellJoke",),  # 来自规则 A 的搜索结果
    verb="{actor} 给 {target} 讲了个笑话",
    verified=False,
    note="super=Y auto=Y user=Y, tgt=TARGET, ages=CHILD+",
)
```

### G.2 物品使用动作（sim 对 object）

```python
CustomActionRule(
    rule_id="write_novel",
    label="📝 写小说",
    action_type="push",
    target_kind="object",
    hints=("computer_Writing_WriteNovel",),
    verb="{actor} 用 {target} 写小说",
    target_match=("computer", "laptop"),
    target_exclude=(),          # 电脑不太会被误匹配
    # 状态过滤：不要匹配坏掉的电脑
    object_state_forbidden=("broken", "burn"),
    verified=False,
    note="super=Y auto=N user=Y, tgt=OBJECT, ages=TEEN+",
)
```

### G.3 走到身边（sim 对 sim）

```python
CustomActionRule(
    rule_id="go_to_sim",
    label="🏃 走到身边",
    action_type="goto",         # goto 原语，不是 push
    target_kind="sim",
    hints=(),                   # 空 = 默认 gohere（jog 被拒自动降级）
    verb="{actor} 走到 {target} 身边",
    verified=True,
    note="goto 逐候选重试：jog→gohere",
)
```

### G.4 走到物品旁（sim 对 object）

```python
CustomActionRule(
    rule_id="go_to_fridge",
    label="🚶 走到冰箱旁",
    action_type="goto",
    target_kind="object",
    hints=(),                   # 空 = 默认 gohere
    verb="{actor} 走到 {target} 旁边",
    target_match=("fridge", "refrigerator", "mini_fridge"),
    verified=False,
    note="goto+object 用 terrain-gohere + PickInfo",
)
```

---

## 常见失败速查

| 症状 | 根因 | 查什么 | 怎么修 |
|------|------|--------|--------|
| "推了错的交互" | hints 中的名字子串匹配到不相关交互 | `grep hints[0] affordances_index.txt` 看有哪些匹配 | 用更精确的名字，或换前缀（`social_` 换 `sim_`） |
| "no target object found" | target_match 关键词与实际物品类型名不一致 | `grep 物品类型名 Interaction_Probe_Items.txt §1` 看真实 type | 修正 target_match |
| "object failed state check" | 物品不在可交互状态 | 看拒因原文，会写明需要什么状态 | 考虑加 object_state_requires / forbidden |
| 全部候选被拒 | affordance 有年龄/技能限制 | `grep hints[0] affordances_index.txt` 看 ages= 字段 | 加 min_age；或换无年龄限制的替代交互 |
| "EnqueueResult=True 但不执行" | 交互入队了但卡住 | 可能是目标位置不可达 / aggregate 子交互失败 | 用简单的非 aggregate 交互；考虑 goto 到目标旁再 push |
| "affordance 不在 sim 列表上" | 交互是 auto=N user=N，不在 `_super_affordances` 上 | 查 auto= 和 user= 标志 | `mythica_action.py` 已处理——自动 fallback 到全局查找 |
| "sim_ 前缀的交互推了但效果不对" | `sim_` 前缀不可靠 | 查 tgt= 字段 | 换 `social_` 前缀的等价交互 |
| **🆕 Item §2 标 ❓ 的 affordance 全拒** | 纯动态 affordance（如 easel_PracticePainting） | Items 探针 §2 查看 ✅/❓ 状态 | 跑 `ai_probe_queue_diff` 拿实际入队类名 |
| **🆕 社交 push 全拒 + 无明确拒因** | IsNotInSexTest 被全局锁 | Social 探针 §2 看 test= 字段是否含 `IsNotInSexTest` | 等特殊动画结束再推 |
| **🆕 手机/电话 push 后 sim 呆站** | 手机交互 auto=0，弹了 UI 面板 | System 探针 §5 确认 auto=N | 换自主变体；或接受需要玩家点面板的交互 |
| **🆕 "场景里明明有 XX 但匹配不到"** | type 名带了 MOD 命名空间前缀 | Items §1 TYPE 行：mod 物品 type 含 `创作者:名字` | target_match 用 `:` 后的关键词（如 `"easel"` 不匹配 `PECO:object_paintEasel*`→用 `"paint"`） |

---

## 查证命令速查表

```bash
# ═══ 探针预检（替代盲 grep 53K）═══
# 物品域：场景里有没有这个物品？affordance 是 ✅ 还是 ❓？
grep -i "物品关键词" MythicaData/Interaction_Probe_Items.txt | grep "^TYPE\|^✅\|^❓"
# 社交域：目标 sim 在场吗？test_globals 有 IsNotInSexTest 吗？
grep -i "目标sim名" MythicaData/Interaction_Probe_Social.txt | grep "^SIM\|AFF.*关键词"
# 自指域/手机：交互在注册表中吗？tgt=ACTOR?
grep "关键词" MythicaData/api_library/affordances_index.txt | grep "tgt=ACTOR"

# ═══ 交互图书馆查证（确认静态属性）═══
# 找交互
grep -i "关键词" affordances_index.txt | grep "super=Y" | head -20
# 看某个交互的完整属性
grep "^交互名 " affordances_index.txt
# 社交类交互
grep "cat=sim_Friendly\|cat=sim_Romantic\|cat=sim_Mean" affordances_index.txt | grep -i "关键词"
# 物品类交互
grep "关键词" affordances_index.txt | grep "tgt=OBJECT" | grep "super=Y.*user=Y"
# 看某个物品类型上挂着哪些交互
grep "tgt=OBJECT" affordances_index.txt | grep -i "物品名" | head -20

# ═══ 统计 =══
grep "^social_" affordances_index.txt | grep -c "super=Y"
grep "^social_" affordances_index.txt | grep -c "super=N"
grep "交互名" affordances_index.txt  # 看 ages= 字段
grep ":" affordances_index.txt | grep "super=Y" | cut -d':' -f1 | sort -u

# ═══ ❓ 动态 affordance 的兜底方案 ═══
# 对 Items 探针 §2 标 ❓ 的 affordance，用 queue_diff 发现实际类名：
# ① 游戏中 ai_probe_queue_diff（拍基线）
# ② 手动让 sim 做一次目标动作
# ③ ai_probe_queue_diff（差分）→ 新增的类名就是实际可推的 affordance
```

---

## 相关文档

| 文档 | 何时读 |
|------|--------|
| [sims4_interaction_system.md](sims4_interaction_system.md) | **架构全景** — 三层交互模型 / 静态vs动态 / 探针覆盖对照 / 三域交叉发现。加动作前先确认走哪条域路径 |
| [sandbox_action_insertion.md](../mythica_sandbox/docs/sandbox_action_insertion.md) | **沙盘端细节** — 三层 hints / 观察器闭环 / 噪音过滤 / 快车自动填充 |
| [sims4_action_injection.md](sims4_action_injection.md) | **游戏端细节** — push 机制本身 API 签名/血案/调试 |
| [sim_bundle_field_reference.md](sim_bundle_field_reference.md) | 查 SimBundle 字段的数据来源和可用性 |
| `mythica_sandbox/custom_actions/` | 看已有规则的写法——改之前先 grep 这里 |
| `MythicaData/Interaction_Probe_Items.txt` | **物品域预检** — §1 物品清单 / §2 ✅❓状态 / §3 am.types |
| `MythicaData/Interaction_Probe_Social.txt` | **社交域预检** — §1 在场 sims / §2 test_globals 全文 |
| `MythicaData/Interaction_Probe_System.txt` | **自指域预检** — §1 手机交互 / §5 可用性分析 |
| `MythicaData/api_library/affordances_index.txt` | 交互总目录——域预检后再 grep 这里确认静态属性 |
