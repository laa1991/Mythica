# Beyond Prompt Engineering: What an AGI Architecture Actually Needs

**The industry is optimizing the middle layer. The real problem is that the core cannot learn.**

---

## 1. Self-Training Is Not Self-Evolution

The AI industry has converged on a pattern. Model released → frozen → collect user data → retrain → release new version → frozen again. Every act of learning requires a full training run. Every training run requires a full redeployment. The gap between "something was learned" and "the system can use it" is measured in months.

This is not a shortcoming of engineering. It is a structural constraint of the architecture.

When knowledge is stored in model weights, learning is inseparable from training. Weights are globally coupled — you cannot update "fireplaces accept `Fireplace_Light` but not `GenericOnOff_TurnOn`" without potentially affecting every other piece of knowledge in the model. So you don't. You wait. You collect. You retrain.

This is **self-training**: optimizing performance within a fixed capability boundary. The menu of possible actions is closed. The system learns which menu items work best, but the menu itself never expands.

**Self-evolution** is different. The capability boundary itself moves outward. The system discovers what it can do that it couldn't do before — not through weight updates, but through structural expansion of its knowledge base. New rules are added. Failing rules are removed. The set of things the system can attempt grows over time.

| | Self-Training | Self-Evolution |
|---|---|---|
| **Capability boundary** | Fixed | Expanding |
| **Knowledge storage** | Weight matrices (globally coupled) | Symbolic rules + empirical records (independently addressable) |
| **Adding one new capability** | Retrain the model | Write one rule, verify it, flip a boolean |
| **Fixing a mistake** | Collect counterexamples, retrain | `set_verified(False)` |
| **Deployment cycle** | Months (collect → train → deploy) | Seconds (observe → verify → available) |
| **Safety mechanism** | Validation loss | Multi-signal cross-validation + auto-demotion |

The entire LLM industry is currently doing self-training. RLHF, DPO, self-play, constitutional AI — these are all ways to get better at choosing from a fixed menu. They produce more capable models, but not models whose capability boundaries expand after deployment.

This is not because self-evolution is undesirable. It is because the dominant architecture makes it impossible.

---

## 2. Why LLMs Cannot Self-Evolve

The knowledge of an LLM is its weights. Every fact, every skill, every pattern — distributed across billions of parameters. This is a remarkable engineering achievement for *inference*. It is a fatal limitation for *learning*.

Three structural problems:

**Problem 1: Knowledge is globally coupled.** Updating "what affordances work on a fireplace" means changing weights. But those same weights encode grammar, reasoning, social norms, and every other thing the model knows. There is no surgical update. Every change is a full-body transplant.

**Problem 2: There is no clean un-learning path.** If the model learns something wrong — a hallucinated fact, an overfitted pattern — there is no `set_verified(False)`. You can train on counterexamples, but gradient descent is not a scalpel. You might fix the specific error while introducing three new ones elsewhere.

**Problem 3: Deployment and learning are the same event.** To deploy is to train. To train is to deploy. There is no way to separate "the model is now running in production" from "the model has stopped learning." Every running model is frozen at its training cutoff date.

These are not problems that better data, larger clusters, or smarter training objectives can solve. They are problems of the architecture itself. Knowledge stored in weights cannot be selectively updated, selectively verified, or selectively rolled back. It can only be retrained.

---

## 3. The Middleware Trap

The industry's response to this limitation has been to build increasingly sophisticated middle layers:

```
LLM Core (frozen)
    ↕
Middleware (orchestration / planning / memory / tool-use / self-reflection)
    ↕
Prompt Template (inject context + tool descriptions + conversation history + RAG results)
```

This works. Agent frameworks have turned LLMs from single-turn Q&A into multi-step systems that can call tools, plan ahead, and self-correct. Every layer adds real capability.

But there is a ceiling, and it is absolute:

**The core never learns anything.** It can be told about last cycle's failure in the prompt. It can retrieve relevant context from a vector store. It can follow a reflection loop to reconsider its output. But every lesson learned in this session evaporates when the session ends. The middleware can remember. The core cannot.

This is knowledge outsourcing, not knowledge internalization. The assistant becomes more capable. The decision-maker stays exactly as it was at deployment.

The ceiling is not that middleware can't do more — it can. It's that the architecture structurally prevents the core from growing, and middleware can only compensate for that gap, never close it.

---

## 4. What an AGI Architecture Actually Needs

If we take the goal seriously — a system that continues to learn after deployment, that expands its own capability boundary, that can be corrected surgically without full retraining — then three subsystems are required. Not three modules in a framework. Three architectural layers with fundamentally different properties.

### Layer 1: Frozen Reasoning Core ("I Think")

This is what LLMs already do well: language understanding, common-sense reasoning, analogy, creative generation. It does not need to update in real time because these capabilities are universal — understanding a sentence works the same way three years after training as it did on day one.

Periodic version upgrades are fine. Real-time learning is not required here. This layer answers: *given what I know, what should I do?*

### Layer 2: Mutable Symbolic Knowledge Layer ("I Know")

This is what LLMs fundamentally cannot do, and what every deployed AI system desperately needs.

Facts, skills, environment models, behavioral rules — these change continuously. The world changes. Machines wear. People change jobs. New interactions are discovered. Old ones stop working. This knowledge must be:

- **Addressable**: you can point to the exact rule that is wrong, not "loss went up by 0.03"
- **Correctable**: changing one rule does not affect all others, no retraining required
- **Verifiable**: after correction, you can confirm it with independent signal sources, not just "training loss decreased"

This layer stores knowledge as symbolic rules with empirical records — not as distributed weights. It answers: *what do I currently believe to be true about the world?*

The dual closed-loop architecture in Mythica is a minimal viable implementation of this layer. Observed rules are generated by watching the environment. They are stored as addressable JSON, not distributed weights. They can be verified (`verified=True`), corrected (`verified=False`), and demoted (`tested_failed=True`) — each operation is a single boolean flip, not a training run.

MirroS's Physical RSI framework independently arrives at the same conclusion: the world model must be built from code and explicit causal rules, not neural implicit representations. Because if the world model is a black box, you cannot fix it when it breaks. And it will break.

### Layer 3: Multi-Signal Cross-Validation ("I Trust")

This is the least discussed but potentially most critical layer.

Layer 1 generates a hypothesis. Layer 2 stores it as a rule. But how do we know it's correct?

A single signal source is never enough. Physical sensors drift. Language models hallucinate. Simulation predictions diverge from reality. Every information channel has failure modes, and in a system that modifies its own knowledge base, trusting any single source is a path to self-reinforcing degradation.

The solution is structural: multiple independent signal sources must converge before trust is granted.

In Mythica, Loop 2 says "the game uses this affordance autonomously" and Loop 1 says "we have successfully pushed it." Only when both agree — `push_confirmed > 0 AND all_in_proven` — does the rule earn automatic verification. Neither signal alone is sufficient. Either can be wrong. Together, they form a safety gate.

MirroS's eight-step cycle encodes the same principle: when the system encounters an OOD event, the first question is not "what should I do differently?" but "can my world model reproduce the causal chain of this failure?" If not, the model is the problem — fix it before you fix the action.

This layer answers: *how do I know that what I just learned is actually true?*

It is not a safety add-on. It is the structural prerequisite for self-evolution to not become self-destruction.

---

## 5. Where This Leaves the Industry

The three layers are not competitors. They are complementary:

```
Layer 1: Frozen Reasoning Core     ← LLMs are here (GPT, Claude, Gemini)
Layer 2: Mutable Knowledge Layer    ← Almost entirely absent in deployed systems
Layer 3: Cross-Validation System    ← Rare outside research frameworks (MirroS, Mythica)
```

The current agent boom is an attempt to compensate for the absence of Layer 2 with increasingly elaborate middleware. It produces useful results — tool-calling agents are genuinely more capable than raw LLMs. But it is asymptotically approaching a limit: middleware can add capability on top of a frozen core, but it cannot make the core learn.

The next architectural shift will be the introduction of a proper Layer 2 — a mutable, addressable, verifiable knowledge store that sits between the reasoning core and the environment. When that happens, a lot of current middleware complexity collapses: you no longer need prompt injection for every piece of learned context, because the knowledge layer handles it systematically.

The shift after that will be Layer 3 — cross-validation becoming as standard as unit testing is today. No autonomous system that modifies its own knowledge should deploy without it.

---

## 6. What One Person Can Do

You don't need to build AGI. You need to demonstrate that the missing layers are possible, not just theoretically desirable.

In July 2026, Mythica's Loop 2 was broken. The system could observe and discover new actions, but the discovered rules were permanently blocked from the AI's action catalog. The gap between "observed" and "usable" was a single boolean field with no runtime path to `True`.

The fix was not to add more middleware. It was to implement the cross-validation gate that the architecture was designed for: two independent signals converge → auto-verify. The result is a system that genuinely expands its own capability boundary after deployment — not through retraining, not through prompt engineering, but through an architectural design that separates mutable knowledge from frozen reasoning.

This is a minimal viable architecture. The Sims 4 is a controlled environment — deterministic, observable, with a finite set of possible interactions. It is not the physical world. But the architectural principles — separable knowledge, multi-signal verification, surgical correction — are environment-agnostic. They will look the same in a robot, in a factory, in a scientific lab.

The path to infrastructure starts with someone building a small version of it and showing that it works.

---

*Written 2026-08-11, distilled from a conversation about MirroS Physical RSI, dual closed-loop architecture, and why prompt engineering cannot be the final answer.*
