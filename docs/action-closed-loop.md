# 双层闭环：让 AI Agent 在真实世界中自我进化

大多数 LLM Agent 系统是"开环"的：AI 做决策 → 执行 → 结束。不知道执行结果，不积累经验，会在同一堵墙上反复撞。

沙盘有两个独立的闭环，一个管"做得好不好"，一个管"还能做什么"。两个回路在符号层交叉验证，使 Agent 无需模型微调即可自我进化。

---

## 闭环 1：执行反馈 — "刚才那下怎么样？"

**问题：** 沙盘推 sim 去弹钢琴，游戏拒绝了（钢琴没开）。下一轮 AI 不知道这件事，又选弹钢琴。

**方案：** 每轮动作选择前，把上一轮的结果注入 AI prompt：

```
{rejection_awareness}  ← "壁炉 GenericOnOff_TurnOn 不兼容，请换 Fireplace_Light"
{effective_approach}   ← "Fireplace_Light 上次验证通过"
{last_cycle_outcome}   ← 上轮全文摘要（谁、做了什么、成功/失败/原因）
```

AI 看到的不是"选一个动作"，而是"上次选了 X 被拒了因为 Y，这次试试 Z"。

**生命周期追踪（ActionLifecycleTracker）：** 每个推送动作走完整状态机——decided → sent → queued → executing → completed / rejected / timeout / stuck_at_head。两个独立数据源交叉验证（`current_action` 正在执行的 + `queue_state.head_affordance` 队首的），产生 5 种可区分结局，不是简单的"成功/失败"。

**PushHistoryTracker：** 会话级推送日志，提供三个查询接口：
- `persistent_rejections()`：同角色连续 3+ 次被拒 → 标记为 persistent，注入 AI prompt
- `effective_hints_for(character, target)`：历史上对 (角色, 目标) 组合成功的 hints → 优先选择
- `suggest_rule_fixes()`：分析全量推送历史 → 连续 4+ 次拒 → 建议禁用规则；同 hint 失败 3+ 次 → 建议移除

**效果确认：** 不仅检查"推送是否成功"，还对比推送前后的 `relation_bits` / `mood` 确认动作是否产生了预期效果。技术上推送成功但语义上无效果 → 不同的失败模式，不同的修复策略。

**诊断可见：** 动作测试页时间线实时显示每条动作的生命状态。`verify_timeline.py` 自动对比沙盘记录和游戏端记录，标出结论矛盾——系统完全可审计。

---

## 闭环 2：知识发现 — "还有什么是我不知道的？"

**问题：** 沙盘能推的动作受限于人工写的规则。游戏里有成百上千个可用交互，但开发者不可能全知道——每一个都需要进游戏测试、写规则、部署。规则库的覆盖度永远追不上游戏的能力。

**方案：** 不靠人工枚举。让系统**观察游戏自己在做什么**，从观察中自动发现新动作。

```
游戏自主行为 (sim 自己决定做什么)
       │
       ▼
AutonomousObserver（每 3s 扫描所有人的 current_action）
       │  累加实证索引：哪个 affordance 被用了、对什么目标、在什么场景
       │  每个 hint 三个维度：observed_count / push_confirmed / push_rejected
       │
       ▼
observer_to_rules（实证 → 规则建议）
       │  自动生成 CustomActionRule
       │  confidence = 观察次数 × 推送成功率 × 目标匹配度
       │  confidence ≥ 3.0 → 自动进入 observed_rules.json
       │
       ▼
双回路交叉验证（见 §交叉验证）
       │
       ▼
进入 action_catalog → AI 可选
```

**Observer 不只是在"发现"——它在纠正人工规则的盲区。** 人工写的 hints 可能漏了某个物品型号可用的 affordance，但游戏自己用过 → observer 记录 → 自动补全到 hints 列表。规则从"人工尽力而为"变成"观察驱动的持续完善"。

**Bootstrap 问题：** Observer 只能学到游戏自己去做的动作。有些动作游戏永远不会自主触发（比如针对特定物品的特殊交互），Observer 永远看不到。解决方案：手动 push 一次（通过动作测试面板）→ 游戏执行 → Observer 捕获执行结果 → 建立实证。第一条实证永远是人工播种的，之后 Observer 自己滚雪球。

---

## 交叉验证：两个回路汇合

### 自动验证门

2026-08 实现。对于 `observed_rules.json` 中的规则（`verified=False`），当**两个回路的信号同时支持**时，系统自动设置 `verified=True`：

1. **回路二满足：** 规则的全部 hints 都在 `_proven` 中有记录 → 游戏自主使用过这些 affordance
2. **回路一满足：** 至少一个 hint 的 `push_confirmed > 0` → 沙盘推送验证过

```python
# autonomous_observer.py: _check_unverified_rules()
if all(hint in self._proven for hint in rule.hints):        # 回路二 ✓
    if any(self._proven[hint].push_confirmed > 0             # 回路一 ✓
           for hint in rule.hints if hint in self._proven):
        set_verified(rule.rule_id, True)  # 自动验证
```

**为什么需要两个条件？** "游戏用过"不等于"我们能推送"（autonomous-only affordance 存在），"推送成功过一次"不等于"这是个可靠的通用动作"（可能在特定状态下偶然成功）。只有两个独立信息源都点头，系统才自动信任规则。

### 试验动作区（Probationary Tier）

回路二有强证据（高观察次数、高置信度）但回路一尚未有机会验证的规则，以**独立分区**进入 AI prompt：

```
🧪 试验动作 — 游戏观察到但尚未推送验证，可谨慎尝试
  · EatLeftoverOnCouch (observed 47 times, confidence 4.2)
  · BrowseWebOnPhone (observed 32 times, confidence 3.8)
```

AI 知道这些是"游戏见过但沙盘还没验证过的"，可以谨慎尝试。试验动作推送成功 → `push_confirmed` 累加 → 满足条件后自动验证。失败 → 记录失败 → 置信度下降。

### 自动降级（安全阀）

自动验证不是永久的。连续 5 次推送失败 → 自动退回 `verified=False` + 标记 `tested_failed=True`：

```python
# push_history.py: suggest_rule_fixes()
if consecutive_rejections >= 5 and rule is in observed_rules.json:
    set_verified(rule_id, False)
    set_tested_failed(rule_id, True)
```

这是自动验证的对偶操作——两个回路都支持的规则自动验证，回路一持续报错的规则自动退场。防止自激发的退化循环：基于早期数据自动验证的规则，在实践中持续失败，被自动移除。

### 手写规则不受影响

自动验证和自动降级**只作用于 `observed_rules.json`（系统生成的规则）**。手写 `.py` 规则仍然只通过人工编辑修改 `verified`。理由：
- 手写规则有作者理解其意图，自动修改可能破坏设计
- `.py` 源文件不应被运行时改写
- JSON 持久化的观察规则天然支持运行时修改

---

## 两个闭环的协作

```
闭环 2（知识发现）                    闭环 1（执行反馈）
  观察游戏 → 发现新规则                    AI 选动作 → 执行 → 追踪结果
       │                                       │
       │  交叉验证门                            │  push_confirmed 数据
       │  ┌─────────────────────────────────────┘
       ▼  ▼
  两个回路都满足 → 自动 verified=True → 进入 AI 动作目录
  仅回路二满足 + 高置信度 → 进入 🧪 试验动作区
  回路一持续失败 5 次 → 自动降级退出
       │
       ▼
  规则健康面板（三维评分）
    · verified（交叉验证通过）
    · observer 实证（游戏自己用过多少次）
    · push 成功率（我们推的时候成不成功）
```

**闭环 1 告诉闭环 2 哪些规则不靠谱，闭环 2 告诉闭环 1 还有哪些新东西可以试。** 两个闭环各自独立运行，通过交叉验证门和规则健康面板汇合。

---

## 与具身智能的呼应

这套架构和具身智能的"快内环 + 慢外环"范式在结构上是同构的：

| | 具身智能 | Mythica 双闭环 |
|---|---|---|
| **内环（快）** | 执行动作 → 传感器反馈 → 修正 | 闭环 1：推送 → 追踪 → 反馈 AI |
| **外环（慢）** | 完成任务 → 积累经验 → 更新能力 | 闭环 2：观察 → 发现 → 生成规则 |
| **更新机制** | 梯度下降（黑箱） | 符号规则生成（可审计） |
| **交叉验证** | 验证集 loss | 布尔交叉：`all_in_proven AND push_confirmed > 0` |
| **安全** | reward shaping、约束动作空间 | 自动降级、试验动作区、人类可读规则 |

关键差异：Mythica 的符号层交叉验证让系统完全可审计。`push_confirmed > 0` 和 `all hints in _proven` 是语义精确、可独立验证的布尔条件——不需要嵌入向量相似度。

完整设计见：**[docs/dual-loop.md](dual-loop.md)**

---

## 通用性

双层闭环不限于 Sims 4。任何"AI 决策 → 真实世界执行 → 结果反馈"的系统都面临同样的问题。第一层闭环保证"不犯重复错误"，第二层闭环保证"能力边界持续扩张"。两层分开设计、通过符号层交叉验证汇合——这种分级反馈架构是可复用的模式。

---

*最后更新 2026-08-11：加入交叉验证门、试验动作区、自动降级机制。*
