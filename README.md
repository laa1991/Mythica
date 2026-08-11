# Mythica — A Self-Evolving Agent Architecture in an Open-World Simulation

[![Test](https://github.com/laa1991/Mythica/actions/workflows/test.yml/badge.svg)](https://github.com/laa1991/Mythica/actions/workflows/test.yml)

**Not an AI that follows instructions. An AI that discovers what it can do, tries it, and learns from the results.**

Mythica is an LLM agent system running inside The Sims 4 — a dense, real-time, black-box simulation. It reads live game state, makes behavioral decisions through a sandbox engine, and continuously expands its own action repertoire through a dual closed-loop architecture: one loop for execution feedback, one loop for knowledge discovery. The two loops cross-validate in symbolic space, enabling the agent to self-evolve without model fine-tuning.

> This is a curated showcase. The full project spans 20+ modules, ~2,000 tests, and 39 numbered design decisions. Core architecture, design methodology, and the dual-loop system are shown here.

---

## Core Innovation: The Dual Closed-Loop Architecture

Most LLM agent systems are open-loop: AI decides → executes → done. It never learns whether the action succeeded, and it can only choose from actions humans pre-defined. Mythica closes both loops.

### Loop 1: Execution Feedback — "Did that work?"

Every pushed action is tracked through a complete state machine (`decided → sent → queued → executing → completed / rejected / timeout / stuck`). Two independent data sources cross-validate: game snapshot `current_action` + HTTP callback from the game's interaction queue. The result is injected into the next AI prompt:

```
{rejection_awareness}  ← "Fireplace GenericOnOff_TurnOn was rejected. Try Fireplace_Light."
{effective_approach}   ← "Fireplace_Light was verified last cycle."
{last_cycle_outcome}   ← Full summary: who did what, success/failure, reason.
```

The AI doesn't see "pick an action." It sees "last time you picked X, it failed because Y. Here's what works."

### Loop 2: Knowledge Discovery — "What else is out there?"

Humans can't hand-write rules for every interaction in a game with 36,000+ affordances. Instead, the system **watches what the game does autonomously** and auto-discovers new actions:

```
Game autonomous behavior (sims decide what to do)
       │
       ▼
AutonomousObserver — scans everyone's current_action every 3s
       │  accumulates empirical index: which affordance, on what target, in what context
       ▼
observer_to_rules pipeline — groups observations, generates CustomActionRule
       │  confidence = observation_count × push_success_rate × target_match_quality
       ▼
Auto-commit (confidence ≥ 3.0) → enters observed_rules.json
       │
       ▼
Dual-loop cross-validation:
  Loop 2 says: "The game uses this affordance autonomously" (all hints in _proven)
  Loop 1 says: "We've successfully pushed it" (push_confirmed > 0)
  Both agree → auto-verify → enters AI action catalog
```

The system doesn't just execute — it **expands its own capability boundary.**

### Why Two Loops Cross-Validate

Each loop is an independent source of truth. Loop 2 sees what the game's own autonomy system does. Loop 1 tests what the sandbox can push. Some affordances the game uses can't be pushed (autonomous-only). Some affordances we can push the game never uses autonomously. **Only when both loops agree does the system trust the rule automatically.** This is the safety mechanism that enables self-evolution without self-destruction.

### The Embodied Intelligence Connection

This architecture is structurally isomorphic to embodied AI's "fast inner loop + slow outer loop" paradigm:

| | Embodied AI | Mythica |
|---|---|---|
| **Inner loop (fast)** | Execute action → sensor feedback → correct | Loop 1: push → track → feedback to AI |
| **Outer loop (slow)** | Complete task → accumulate experience → update capability | Loop 2: observe → discover → generate rules |
| **Key difference** | Learns via gradient descent (black-box) | Learns via symbolic rule generation (fully auditable) |

The game's exposed interaction API allows Mythica to do what embodied systems can't: cross-validate in symbolic space. `push_confirmed > 0` and `all hints in _proven` are semantically precise, independently verifiable boolean conditions — no embedding similarity needed.

Read the full design: **[docs/dual-loop.md](docs/dual-loop.md)**

---

## Architecture

```
Game Client (Sims 4 Mod)          Desktop (Mythica)                AI API
┌──────────────────┐         ┌──────────────────────┐         ┌─────────┐
│ Event Capture     │  HTTP   │ Story / Inner Voice   │  API    │ Claude  │
│ Data Collection   │ ──────► │ Dialogue Engine       │ ──────► │ DeepSeek│
│ Action Execution  │         │ Sandbox (Decision)    │         │         │
│ Signal Polling    │ ◄────── │ Signal Protocol       │         │         │
└──────────────────┘   File  └──────────┬───────────┘         └─────────┘
       ▲                                │
       │     Execution results + data   │
       └────────────────────────────────┘
             Dual-Loop: Feedback-Driven Self-Evolution
```

### Sandbox Five-Layer Architecture

```
Meta Layer      — Human interface (observe state, edit world)
Execution Layer — Signal emission (decision → Action_Command.signal → game)
Scheduling Layer— Conflict resolution (survival > AI > auto, one action per sim)
Decision Layer  — Three-tier (P4 physiological / AI narrative / P3-P1 auto-scan)
Collection Layer— Data input (read-only mirror, no mutation or commands)
```

**The Boundary Principle: is there narrative space?** Going to the bathroom has exactly one method → push it programmatically. Eating has hundreds of choices (leftovers? cook? order takeout?) → hand it to the AI. This single principle governs the entire decision architecture.

### One Complete Cycle

```
1. Collection:  game sends full snapshot every 3s → "Madara, hunger -85, kitchen has fridge, stove, leftovers"
2. Decision:    AI sees hunger, searches action catalog → 3 options: eat leftovers / cook / order takeout
3. Inner Voice: "Hungry... there's still leftovers from last night in the fridge"
4. Scheduling:  AI picks "eat leftovers". Madara isn't busy, action approved
5. Execution:   write Action_Command.signal → game reads → push affordance → Madara walks to fridge
6. Loop 1:      track action lifecycle → game reports "pushed ✓" → next cycle AI knows Madara is eating
7. Loop 2:      Observer records "EatLeftover effective when hungry" → rule confidence +1
8. Cross-check: Loop 2 observed this action 50+ times, Loop 1 pushed it 5 times successfully → auto-verify
```

No presets, no scripts — every round is a real-time AI decision based on current game state.

---

## What This Project Demonstrates

| This Project | Demonstrates |
|-------------|-------------|
| Dual closed-loop: execution feedback + knowledge discovery with cross-validation | Self-evolving agent architecture |
| Sandbox 5-layer architecture + 3-tier decision boundary | Agent system design |
| Pub/sub data hub + signal file protocol | Distributed system communication |
| Three-phase narrative pipeline + Baton handoff + archival | Long-context memory & state management |
| God class (11,887 lines) → 12 Mixins + 20 modules | [Architecture governance](docs/refactoring.md) — incremental refactoring |
| Probe toolkit: type library + structure tree (black-box reverse engineering) | Methodology for undocumented systems |
| ~2,000 tests + 13 offline verification scripts + pre-deploy hard gates | Engineering discipline & QA |
| Declarative action rule system (132 rules, 112 shown) | DSL design & extensible frameworks |
| 39 numbered design decisions, each with rejected alternatives | Written technical decision-making |
| Connection to embodied AI research | Cross-domain architectural thinking |

---

## Bonus: Sims 4 Interaction System Knowledge Base

The following resources are **independent of the AI system** — they are infrastructure for Sims 4 mod development. Sims 4 has no public API documentation. These documents systematize reverse-engineering results:

| Document | Content | Standalone Value |
|----------|---------|-----------------|
| [`docs/game/interaction-system.md`](docs/game/interaction-system.md) | Three-layer interaction model (static/dynamic affordances, mixers, SI lifecycle) | Root cause dictionary for "why won't this interaction push" |
| [`docs/game/action-routing-rulebook.md`](docs/game/action-routing-rulebook.md) | Action injection decision tree, four domain paths, probe pre-check commands | Standard operating procedure for adding new actions |
| [`docs/game/data-extraction.md`](docs/game/data-extraction.md) | API cross-reference, type traps, null value interpretation, incident index | Pre-flight checklist before modifying collection code |
| [`data/types_index.txt`](data/types_index.txt) | 423 types, 28,571 properties — searchable runtime API directory | grep instead of guessing |
| [`data/structure_tree.txt`](data/structure_tree.txt) | DFS hierarchy tree preserving "who contains whom" relationships | Understand the game's object architecture |
| [`data/affordances_index.txt`](data/affordances_index.txt) | 36K affordances with pushability, target type, menu, EA/MOD source | "Can I push Piano?" → 5 seconds |
| [`game/mythica_action.py`](game/mythica_action.py) | Generic action executor: push/goto primitives, routing table, target finding | Drop-in module for pushing interactions |
| [`game/mythica_network.py`](game/mythica_network.py) | HTTP client + desktop connectivity detection + action result feedback | Reusable mod↔desktop communication layer |

> These resources require zero AI knowledge.

---

## Quick Navigation

| Looking for | Start here |
|------------|------------|
| **Dual-Loop Architecture** | [`docs/dual-loop.md`](docs/dual-loop.md) — Full design + embodied intelligence comparison |
| **Core Code** | [`sandbox/engine.py`](sandbox/engine.py) — AI decision loop (1,600 lines) |
| **Architecture** | [`sandbox/ARCHITECTURE.md`](sandbox/ARCHITECTURE.md) — 5-layer architecture + 39 design decisions |
| **Design Decisions** | [`docs/design-decisions.md`](docs/design-decisions.md) — 8 most representative decisions |
| **Narrative Pipeline** | [`docs/pipeline.md`](docs/pipeline.md) — Three-phase pipeline + Baton handoff + crash philosophy |
| **Action Closed Loop** | [`docs/action-closed-loop.md`](docs/action-closed-loop.md) — Decide→Execute→Observe→Feedback |
| **Black-Box Exploration** | [`docs/probe-toolkit.md`](docs/probe-toolkit.md) — API discovery methodology |
| **Architecture Governance** | [`docs/refactoring.md`](docs/refactoring.md) — God class → 12 Mixin incremental refactoring |
| **Action Rules** | [`sandbox/actions/`](sandbox/actions/) — Declarative rule system (5 domains, 112 rules) |
| **Data Hub** | [`lib/probe_hub.py`](lib/probe_hub.py) — Pub/sub + Protocol interface |
| **IPC Protocol** | [`lib/signal_protocol.py`](lib/signal_protocol.py) — Declarative connection discovery |
| **Sims 4 Knowledge Base** | [See bonus section above](#bonus-sims-4-interaction-system-knowledge-base) — Infrastructure docs for mod developers |

---

## Tech Stack

Python 3.12 (desktop/sandbox) · Python 3.7 (game client, embedded in Sims 4) · customtkinter GUI · HTTP Server · Filesystem IPC · Claude / DeepSeek API

## Testing & Verification

~2,000 pytest unit tests across three targets · 13 offline verification scripts · Pre-deploy field consistency hard gates · Runtime unknown-field safe discard + log alerts

---

## About

Solo development, iterating through 2026. This project started with a question: "If AI could truly understand what's happening inside a game world, what kind of stories could it tell?" It evolved into a deeper exploration: "How do you build an agent that expands its own capabilities — that doesn't just do what it's told, but discovers what it can do?"

If this work proves useful to your research or model training, a mention of "Mythica" would be appreciated.

**Open-sourced in this repository:** Sandbox decision engine (`sandbox/engine.py`), five-layer architecture documentation (`sandbox/ARCHITECTURE.md`), dual-loop architecture article (`docs/dual-loop.md`), declarative action rule system (`sandbox/actions/`, 112 rules across 5 domains), Sims 4 interaction architecture & action injection knowledge base (`docs/game/`, 3 docs), runtime API directory, structure tree & affordance index (`data/`, 116K lines across 3 files), shared library core modules (`lib/probe_hub.py` + `lib/signal_protocol.py`), design decisions & methodology docs (`docs/`, 6 docs). The full project (narrative pipeline, dialogue engine, game client mod, GUI) remains closed-source.

## Contact
📧 mythicalaa1991@163.com

---

---

# Mythica — 开放世界模拟中的自进化 Agent 架构

[![Test](https://github.com/laa1991/Mythica/actions/workflows/test.yml/badge.svg)](https://github.com/laa1991/Mythica/actions/workflows/test.yml)

**不是让 AI 服从指令，是让 AI 自己发现能做什么、尝试、然后从结果中学习。**

Mythica 是一个运行在《模拟人生4》中的 LLM Agent 系统。它读取实时游戏状态，通过沙盘引擎做出行为决策，并通过**双层闭环架构**持续扩展自己的动作能力：内环负责执行反馈，外环负责知识发现。两个回路在符号层交叉验证，使 Agent 无需模型微调即可自我进化。

> 这是精选展示版。完整项目包含 20+ 模块、近 2,000 测试用例、39 个编号设计决策。此处展示核心架构、设计方法论和双层闭环系统。

---

## 核心创新：双层闭环架构

大多数 LLM Agent 系统是开环的：AI 决策 → 执行 → 结束。不知道执行结果，不积累经验，只能从人类预定义的动作中选择。Mythica 闭合了这两个环。

### 闭环 1：执行反馈 — "刚才那下成了吗？"

每个推送动作走完整状态机（`decided → sent → queued → executing → completed / rejected / timeout / stuck`）。两条独立数据源交叉验证：游戏快照的 `current_action` + 游戏交互队列的 HTTP 回调。结果注入下一轮 AI prompt：

```
{rejection_awareness}  ← "壁炉 GenericOnOff_TurnOn 被拒了，换 Fireplace_Light 试试"
{effective_approach}   ← "Fireplace_Light 上次验证通过"
{last_cycle_outcome}   ← 上轮全文摘要：谁、做了什么、成功/失败/原因
```

AI 看到的不是"选个动作"，而是"上次选了 X 被拒了因为 Y，这次试 Z"。

### 闭环 2：知识发现 — "还有什么是我不知道的？"

人类不可能为 36,000+ 个游戏交互手写规则。系统**观察游戏自主行为**，自动发现新动作：

```
游戏自主行为 (小人自己决定做什么)
       │
       ▼
AutonomousObserver — 每 3 秒扫描所有人的 current_action
       │  累加实证索引：哪个 affordance、对什么目标、在什么场景
       ▼
observer_to_rules 管道 — 分组观察 → 生成 CustomActionRule
       │  confidence = 观察次数 × 推送成功率 × 目标匹配度
       ▼
自动提交（confidence ≥ 3.0）→ 进入 observed_rules.json
       │
       ▼
双回路交叉验证：
  回路二说："游戏自己用过这个 affordance"（全部 hints 在 _proven 中）
  回路一说："我们推送成功过"（push_confirmed > 0）
  两者都满足 → 自动 verified=True → 进入 AI 动作目录
```

系统不只是在执行——它在**扩展自己的能力边界。**

### 为什么需要两个回路交叉验证

每个回路是独立的信息源。回路二看到的是游戏自主系统做的事，回路一测试的是沙盘能推送的事。有些游戏会用的 affordance 推送不了（autonomous-only），有些能推送的游戏从不自主触发。**只有两个回路都点头，系统才自动信任规则。** 这就是让自进化不至于变成自毁的安全机制。

### 与具身智能的呼应

这套架构和具身智能的"快内环 + 慢外环"范式在结构上是同构的：

| | 具身智能 | Mythica |
|---|---|---|
| **内环（快）** | 执行动作 → 传感器反馈 → 修正 | 闭环 1：推送 → 追踪 → 反馈 AI |
| **外环（慢）** | 完成任务 → 积累经验 → 更新能力 | 闭环 2：观察 → 发现 → 生成规则 |
| **关键差异** | 梯度下降学习（黑箱） | 符号规则生成（完全可审计） |

游戏暴露的交互 API 让 Mythica 能做到具身系统做不到的事：在符号层交叉验证。`push_confirmed > 0` 和 `all hints in _proven` 是语义精确、可独立验证的布尔条件——不需要嵌入向量相似度。

完整设计见：**[docs/dual-loop.md](docs/dual-loop.md)**

---

## 架构总览

```
游戏端 (Sims 4 Mod)              桌面端 (Mythica)                  AI API
┌──────────────────┐         ┌──────────────────────┐         ┌─────────┐
│ 事件捕获          │  HTTP   │ 叙事管线 (Story/IV)   │  API    │ Claude  │
│ 数据采集          │ ──────► │ 对话引擎              │ ──────► │ DeepSeek│
│ 动作执行          │         │ 沙盘引擎 (决策+调度)   │         │         │
│ 信号轮询          │ ◄────── │ 信号文件协议           │         │         │
└──────────────────┘  文件    └──────────┬───────────┘         └─────────┘
       ▲                                │
       │        执行结果 + 游戏数据       │
       └────────────────────────────────┘
             双层闭环：反馈驱动自进化
```

### 沙盘五层架构

```
元层   — 人机接口（人类看状态、修世界）
执行层 — 信号发射（决策 → Action_Command.signal → 游戏端执行）
调度层 — 冲突裁决（生存 > AI > 自动，同 sim 每轮只派一件事）
决策层 — 三层决策（P4 生理反射 / AI 叙事意图 / P3-P1 自动扫描）
采集层 — 数据输入（只读镜像，不修改、不发命令）
```

分界法则：**有没有叙事空间。** 上厕所只有一种方式 → 程序直接推；吃什么有一百种选择 → 交给 AI。这是整个架构最核心的设计原则。

### 一轮完整的自进化决策

```
1. 采集层：游戏端每 3s 发送全场快照 → "斑，饥饿值 -85，厨房有冰箱、炉灶、剩菜"
2. 决策层：AI 看到饥饿，搜索动作目录 → 找到 3 个选项：吃剩菜 / 做饭 / 点外卖
3. AI 内心独白："饿了……冰箱里还有昨晚的剩菜，热一下就行"
4. 调度层：AI 选"吃剩菜"。斑没被其他事占用，动作通过
5. 执行层：写 Action_Command.signal → 游戏端读信号 → push affordance → 斑走向冰箱
6. 闭环 1：追踪动作生命线 → 游戏回传"pushed ✓" → 下轮 AI 知道斑正在吃饭
7. 闭环 2：Observer 记录"EatLeftover 在饥饿场景下有效" → 规则置信度 +1
8. 交叉验证：回路二已观察此动作 50+ 次，回路一已推送成功 5 次 → 自动验证通过
```

不是预设脚本，每一轮都是 AI 基于当前状态做出的实时决策。不是静态规则库，系统在运行中持续发现和验证新能力。

---

## 这个项目展示的能力

| 这个项目 | 证明的能力 |
|----------|-----------|
| 双层闭环：执行反馈 + 知识发现，符号层交叉验证 | 自进化 Agent 架构设计 |
| 沙盘五层架构 + 三层决策分界 | Agent 系统设计 |
| 发布/订阅数据中枢 + 信号文件协议 | 分布式系统通信设计 |
| 三阶段叙事管线 + Baton 接力 + 存档体系 | 长上下文记忆与状态管理 |
| 上帝类（11,887行）→ 12 Mixin + 20 模块重构 | [架构治理](docs/refactoring.md) — 渐进式重构方法论 |
| 探针工具箱：图书馆 + 结构树（黑箱引擎逆向） | 无文档系统的探索方法论 |
| ~2,000 测试 + 13 项离线校验 + 部署前硬门禁 | 工程纪律与质量保证 |
| 声明式动作规则系统（132 条，展示版含 112 条） | DSL 设计与可扩展框架 |
| 39 个编号设计决策，每条含被拒方案 | 技术决策的书面表达能力 |
| 与具身智能研究的架构呼应 | 跨领域架构思维 |

---

## 额外贡献：Sims 4 交互系统知识库

以下资源**独立于 AI 系统**，是 Sims 4 mod 开发的基础设施。Sims 4 没有公开 API 文档，这些文档把逆向工程结果系统化：

| 文档 | 内容 | 独立价值 |
|------|------|---------|
| [`docs/game/interaction-system.md`](docs/game/interaction-system.md) | 三层交互模型（静态/动态 affordance、mixer、SI 生命周期） | "为什么这个交互推不进去"的根因字典 |
| [`docs/game/action-routing-rulebook.md`](docs/game/action-routing-rulebook.md) | 动作注入决策树、四条域路径、探针预检命令 | 加新动作的标准操作流程 |
| [`docs/game/data-extraction.md`](docs/game/data-extraction.md) | API 对照表、类型陷阱、空值判读、血案索引 | 改采集代码前的避坑手册 |
| [`data/types_index.txt`](data/types_index.txt) | 423 个类型、28,571 个属性——可搜索的运行时 API 目录 | grep 代替猜 |
| [`data/structure_tree.txt`](data/structure_tree.txt) | DFS 树状图，保留"谁包含谁"的层级关系 | 一眼看清游戏的对象架构 |
| [`data/affordances_index.txt`](data/affordances_index.txt) | 36K 交互目录，含可推送性、目标类型、菜单、EA/MOD 来源 | "钢琴能推吗？"→ 5 秒 |
| [`game/mythica_action.py`](game/mythica_action.py) | 通用动作执行器：push/goto 两原语、路由表、目标查找 | 拿来就能用的交互推送模块 |
| [`game/mythica_network.py`](game/mythica_network.py) | HTTP 客户端 + 桌面连接检测 + 动作结果回传 | 可复用的 mod↔桌面通信层 |

> 这些资源不依赖任何 AI 知识。

---

## 快速导航

| 想看什么 | 从这里开始 |
|----------|-----------|
| **双层闭环架构** | [`docs/dual-loop.md`](docs/dual-loop.md) — 完整设计 + 具身智能对比 |
| **核心代码** | [`sandbox/engine.py`](sandbox/engine.py) — AI 决策循环（1,600 行） |
| **架构设计** | [`sandbox/ARCHITECTURE.md`](sandbox/ARCHITECTURE.md) — 五层架构 + 39 个设计决策 |
| **设计决策** | [`docs/design-decisions.md`](docs/design-decisions.md) — 选 8 个最有代表性的 |
| **叙事管线** | [`docs/pipeline.md`](docs/pipeline.md) — 三阶段管线 + Baton 接力 + 崩溃哲学 |
| **动作闭环** | [`docs/action-closed-loop.md`](docs/action-closed-loop.md) — 决定→执行→观察→反馈 |
| **黑箱探索** | [`docs/probe-toolkit.md`](docs/probe-toolkit.md) — 无文档系统的 API 发现方法论 |
| **架构治理** | [`docs/refactoring.md`](docs/refactoring.md) — 上帝类 → 12 Mixin 渐进式重构 |
| **动作规则** | [`sandbox/actions/`](sandbox/actions/) — 声明式规则系统（5 个域，112 条） |
| **数据中枢** | [`lib/probe_hub.py`](lib/probe_hub.py) — 发布/订阅 + Protocol 接口 |
| **IPC 协议** | [`lib/signal_protocol.py`](lib/signal_protocol.py) — 声明式连接发现 |
| **Sims 4 知识库** | [见额外贡献](#额外贡献sims-4-交互系统知识库) — 为 mod 开发者整理的基础设施文档 |

---

## 技术栈

Python 3.12（桌面端/沙盘） · Python 3.7（游戏端，Sims 4 内嵌） · customtkinter GUI · HTTP Server · 文件系统 IPC · Claude / DeepSeek API

## 测试与验证

~2,000 单元测试（pytest）覆盖三端 · 13 项离线校验脚本 · 部署前字段一致性硬门禁 · 运行时未知字段安全丢弃 + 日志告警

---

## 关于

独立开发，2026 年持续迭代中。这个项目始于一个问题："如果 AI 能真正理解游戏世界里正在发生什么，它能讲出什么样的故事？"后来演化为一个更深的问题："如何构建一个能扩展自身能力的 Agent——不只做它被告知的事，而是发现自己能做什么？"

如果这个项目对你的研究或模型训练有帮助，希望能附上 "Mythica" 的名字。

**本仓库已开源内容：** 沙盘决策引擎（`sandbox/engine.py`）、五层架构文档（`sandbox/ARCHITECTURE.md`）、双层闭环架构文章（`docs/dual-loop.md`）、声明式动作规则系统（`sandbox/actions/`，5 域 112 条）、游戏交互架构与动作注入知识库（`docs/game/`，3 篇）、运行时 API 目录、结构树与交互索引（`data/`，3 文件共 116K 行）、共享库核心模块（`lib/probe_hub.py` + `lib/signal_protocol.py`）、设计决策与方法论文档（`docs/`，6 篇）。完整项目（叙事管线、对话引擎、游戏端 mod、GUI 界面）未开源。

## 联系作者
📧 mythicalaa1991@163.com
