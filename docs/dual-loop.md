# The Dual Closed-Loop: A Self-Evolving Agent Architecture

**How an LLM agent discovers what it can do, tries it, and learns from the results — without model fine-tuning.**

---

## 1. The Problem with Open-Loop Agents

Most LLM agent systems are open-loop:

```
AI decides → executes → done
```

Three things go wrong:

1. **No execution feedback.** The agent pushes an action, the environment rejects it, and the next cycle the agent has no memory of the failure. It tries the same thing again. Same wall, same crash.

2. **Static capability boundary.** The agent can only choose from actions humans pre-defined. If the environment has 36,000 possible interactions but humans only wrote rules for 200, the agent is blind to the other 35,800.

3. **No cross-validation.** Even when the agent *does* successfully execute something, it doesn't know *why* it worked — which hint was effective, which condition mattered, what target state allowed it.

Mythica's dual closed-loop architecture addresses all three. Loop 1 closes execution feedback. Loop 2 closes knowledge discovery. And the two loops cross-validate in symbolic space.

---

## 2. Loop 1: Execution Feedback

### 2.1 The Mechanism

Every action the sandbox pushes goes through a complete state machine:

```
decided → sent → queued → executing → completed / rejected / timeout / stuck_at_head
```

Two independent data sources track this lifecycle:

| Source | What it provides | Latency |
|--------|-----------------|---------|
| Game snapshot `current_action` | What the sim is *actually doing right now* | ~3s (next snapshot) |
| HTTP callback `/action_result` | The game engine's verdict: which affordance succeeded, which were rejected, and why | ~1-5s (game processes queue) |

The two sources cross-validate. If the snapshot says a sim is "playing piano" but the callback said "all_rejected", the system knows something is wrong — likely the sim autonomously chose a different action that looks similar.

### 2.2 What the AI Sees

The execution result is injected into the next cycle's prompt through three channels:

```
{rejection_awareness}  ← "Fireplace GenericOnOff_TurnOn was rejected: object not compatible."
                          "Try Fireplace_Light instead."
{effective_approach}   ← "Fireplace_Light was verified last cycle (push_confirmed=3)."
{last_cycle_outcome}   ← Full summary: who did what, success/failure, reason, duration.
```

The AI doesn't see "pick an action from this menu." It sees "last time you picked X, it failed because Y. Here's what works for this character on this target."

### 2.3 The Push History Tracker

`push_history.py` maintains a session-level log of every push attempt. Beyond simple success/failure, it provides:

- **Persistent rejection detection:** Same character, 3+ consecutive rejections → flagged in the AI prompt as "this character is having trouble with these actions"
- **Effective hints cache:** Per (character, target, action_type) → which hints worked, sorted by frequency. Used to prioritize hints in future cycles.
- **Rule fix suggestions:** Consecutive 4+ rejections on the same rule → suggest disabling. Same hint failing 3+ times → suggest removing from the rule's hints list.

### 2.4 Push Effect Confirmation

Beyond "did the push succeed", the system also checks "did the action have the intended effect":

```python
def confirm_push_effect(before_ws, after_ws, action):
    # 1. Does the action appear in the event stream?
    # 2. For romance: did relation bits change?
    # 3. For mood-targeted actions: did mood shift as expected?
```

This means the system can detect when an action was *technically* pushed (the affordance was accepted by the game engine) but had no *semantic* effect (the relationship didn't change, the mood didn't shift). These are different failure modes requiring different fixes.

---

## 3. Loop 2: Knowledge Discovery

### 3.1 The Mechanism

The game world is a black box with 36,000+ affordances, no public API documentation, and behaviors only observable at runtime. Humans cannot hand-write rules for all interactions. Instead, the system watches what the game does autonomously:

```
Game autonomous behavior (sims decide what to do)
       │
       ▼
AutonomousObserver — scans everyone's current_action every 3 seconds
       │  Builds an empirical index: which affordance, on what target,
       │  in what context (mood, needs, time of day, nearby objects)
       │
       ▼
observer_to_rules pipeline — groups observations by (affordance, target_class)
       │  Generates CustomActionRule with:
       │    · hints = [observed_affordance] + related affordances gleaned from game data
       │    · confidence = observation_count × push_success_rate × target_match_quality
       │    · verified = False (pending dual-loop cross-validation)
       │
       ▼
observed_rules.json — mutable, JSON-persisted rule store
       │  Rules here are NOT in the AI catalog yet (verified=False).
       │  They wait for dual-loop convergence.
       │
       ▼
Dual-loop cross-validation (see §4)
```

### 3.2 What "Observation" Actually Means

The `AutonomousObserver` isn't just counting affordance occurrences. It builds a structured empirical index:

```python
_proven[(affordance_name, target_id)] = ProvenRecord(
    observed_count=47,         # Game autonomously used this 47 times
    push_confirmed=12,         # We successfully pushed it 12 times
    push_rejected=2,           # We tried and failed 2 times
    last_seen=timestamp,
    contexts=[...],            # Scenes where this was observed
)
```

This gives three independent dimensions per hint:
- **Does the game use it?** (observed_count)
- **Can we push it?** (push_confirmed)
- **Does pushing it sometimes fail?** (push_rejected)

A hint with `observed_count=100, push_confirmed=0` means "the game uses this constantly, but we've never successfully pushed it" — likely an autonomous-only affordance. A hint with `observed_count=2, push_confirmed=50` means "the game rarely does this autonomously, but we can push it reliably" — a good action for the AI to use.

### 3.3 The Confidence System

Not all observations are equal. The `observer_to_rules` pipeline computes a confidence score:

```
confidence = observation_count × push_success_rate × target_match_quality
```

| Factor | Weight | Meaning |
|--------|--------|---------|
| observation_count | Raw frequency | How many times has the game done this? |
| push_success_rate | push_confirmed / (push_confirmed + push_rejected) | When we tried, did it work? |
| target_match_quality | How well does the target class match known good patterns? | Is this a known-good target type? |

Auto-commit threshold: confidence ≥ 3.0 → the rule enters `observed_rules.json` automatically. Below 3.0 → queued for human review.

### 3.4 The Bootstrap Problem (and Its Solution)

The Observer can only learn what the game does autonomously. Some affordances the game *never* triggers on its own — e.g., specific interactions with rare objects. These are invisible to the Observer.

**Solution: Manual seeding.** Push the action once manually (via the action test panel) → the game executes it → the Observer captures the execution result → an empirical record is established. From there, the confidence system can accumulate more data through repeated pushes.

The first empirical record is always seeded by a human. After that, the Observer can snowball on its own.

---

## 4. Cross-Validation: Why Two Loops Are Better Than One

### 4.1 Each Loop is an Independent Source of Truth

Loop 2 (observation) and Loop 1 (execution) provide different, complementary information:

| | Loop 2 (Observation) | Loop 1 (Execution) |
|---|---|---|
| **What it measures** | "Does the game engine consider this a valid interaction?" | "Can our agent successfully trigger this?" |
| **False positive risk** | Game uses it autonomously, but it can't be pushed (autonomous-only) | We pushed it once, but it only works in a narrow condition we don't understand |
| **False negative risk** | The game never does this autonomously, but it's pushable | Our push logic is wrong, not the affordance |

Neither loop alone is sufficient:

- **Loop 2 alone**: Would accumulate hundreds of "observed" rules that can't actually be pushed — noise in the AI catalog.
- **Loop 1 alone**: Would miss thousands of pushable actions that humans haven't manually discovered yet — stagnant capability.

### 4.2 The Auto-Verification Gate

This is where the two loops converge. An observed rule (`verified=False` in `observed_rules.json`) is **automatically promoted to `verified=True`** when BOTH conditions are met:

1. **Loop 2 condition**: All hints in the rule have been observed in game autonomous behavior (all hints have entries in `_proven`).
2. **Loop 1 condition**: At least one hint has `push_confirmed > 0` (the sandbox has successfully pushed it).

```python
# In _check_unverified_rules(), autonomous_observer.py:
if all(hint in self._proven for hint in rule.hints):          # Loop 2 ✓
    if any(self._proven[hint].push_confirmed > 0               # Loop 1 ✓
           for hint in rule.hints if hint in self._proven):
        set_verified(rule.rule_id, True)  # AUTO-VERIFIED
```

**Why both conditions?** Because "the game uses it" doesn't mean "we can push it" (autonomous-only affordances exist), and "we pushed it once" doesn't mean "it's a valid general-purpose action" (some pushes succeed by accident in specific states). Only when both independent sources agree does the system trust the rule automatically.

### 4.3 The Probationary Tier

What about rules where Loop 2 has strong evidence (high observation count, high confidence) but Loop 1 hasn't had a chance to validate yet? These rules enter the AI prompt as a **probationary tier**:

```
🧪 试验动作 — 游戏观察到但尚未推送验证，可谨慎尝试
  · EatLeftoverOnCouch (observed 47 times, confidence 4.2)
  · BrowseWebOnPhone (observed 32 times, confidence 3.8)
```

The AI knows these are "game-observed but not push-verified" and can try them cautiously. If a probationary action succeeds → push_confirmed increments → eventually auto-verified. If it fails → the failure is logged → the confidence score drops.

### 4.4 The Safety Valve: Auto-Demotion

Auto-verification is not permanent. The system has a symmetric safety mechanism:

```python
# In suggest_rule_fixes(), push_history.py:
if consecutive_rejections >= 5 and rule is in observed_rules.json:
    set_verified(rule_id, False)
    set_tested_failed(rule_id, True)
    # Rule exits the AI catalog. It can be re-verified later
    # if new evidence emerges.
```

This prevents a self-reinforcing degradation cycle: a rule that was auto-verified based on early data but consistently fails in practice is automatically removed from the catalog. It can be re-instated if conditions change (e.g., a bug in the push logic is fixed).

### 4.5 The Complete Data Flow

```
                    Loop 2: Knowledge Discovery
                    ═══════════════════════════
Game autonomous  →  AutonomousObserver  →  _proven index  ─┐
behavior             (every 3s)            (per-hint stats) │
                                                            │
                    Loop 1: Execution Feedback              │
                    ════════════════════════                │
AI selects       →  ActionLifecycleTracker  →  push_history │
action              (state machine)           (per-push log)│
                                                            │
                    Cross-Validation Gate                   │
                    ══════════════════════                  │
                    ┌───────────────────────────────────────┘
                    ▼
              _check_unverified_rules()
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   Both loops   Loop 2 only   Neither
   satisfied    + high conf   satisfied
        │           │           │
        ▼           ▼           ▼
   auto-verify  enter 🧪     stay in
   verified=T   probationary  observed_rules.json
   → AI catalog → AI catalog  (not visible to AI)
        │           │
        │           │    ┌─── push fails 5+ times consecutively
        │           │    ▼
        │           └────┤
        ▼                ▼
   auto-demotion: verified=F, tested_failed=T
   → rule exits AI catalog
```

---

## 5. The Embodied Intelligence Connection

This architecture is structurally isomorphic to the "fast inner loop + slow outer loop" paradigm in embodied AI research:

| | Embodied AI | Mythica Dual-Loop |
|---|---|---|
| **Inner loop (fast)** | Execute action → sensor feedback → immediate correction | Loop 1: push action → track lifecycle → inject feedback into next prompt |
| **Outer loop (slow)** | Complete task → accumulate experience → update capability model | Loop 2: observe autonomous behavior → discover new actions → generate rules |
| **Update mechanism** | Gradient descent on neural network weights | Symbolic rule generation (JSON-serialized, human-readable) |
| **Cross-validation** | Validation loss on held-out data | Boolean cross-check: `all_in_proven AND push_confirmed > 0` |
| **Safety** | Reward shaping, constrained action spaces | Auto-demotion on sustained failure, probationary tier, human-auditable rules |

### 5.1 What Mythica Gains from Being Symbolic

Embodied AI systems learn through gradient descent — the model's understanding of "can I push this button" is distributed across millions of weights. You can't inspect it, you can't edit it, you can't explain why it thinks a particular action should work.

Mythica's dual-loop operates in symbolic space:

- `push_confirmed > 0` is a semantically precise, independently verifiable boolean condition.
- `all hints in _proven` means every affordance in the rule has been observed in game autonomous behavior — again, a precise boolean condition.
- Rules are JSON with named fields. You can open `observed_rules.json` and read exactly what the system believes about each action.
- Auto-demotion has a clear threshold (5 consecutive failures) and leaves an audit trail in the error log.

This is not a compromise. For an agent operating in a deterministic game environment with a fixed set of possible interactions, symbolic rules are the *right* representation. You don't need to approximate "can the sim use the toilet" with embeddings — the answer is a definitive yes/no that can be empirically tested.

### 5.2 What Embodied AI Would Need to Replicate This

To build an equivalent system for a physical robot:

1. **An independently observable ground truth** — something that tells you "the robot *could* have done X in this state" without you having to try it first. In Mythica, this is the game's own autonomy system. In the physical world, this might be: human demonstrations, simulation-to-real transfer, or a digital twin.

2. **A symbolic cross-validation gate** — a condition that is semantically meaningful and independently verifiable, not "the loss decreased by 0.03." Something like: "the robot's action plan matches a plan demonstrated by a human in a similar state AND the robot's execution succeeded at least once."

3. **A safety valve for auto-acquired capabilities** — when the agent learns something new on its own, there must be a mechanism to un-learn it if it turns out to be wrong. Gradient descent doesn't have a clean "un-learn this specific capability" operation; symbolic rules do (set `verified=False`).

---

## 6. Design Decisions

### 6.1 Why "Suggest" Not "Auto-Modify" (Historical)

The original design (before the dual-loop cross-validation gate was implemented) deliberately made all rule modifications human-reviewed. The reasoning was sound for the time:

> Automatic modification risks a self-reinforcing degradation cycle. A single false positive could disable a rule that works in 90% of scenarios, and the disablement would be confirmed by subsequent "rule not used" detection.

This is still true for **hand-written rules** (`.py` source files). They remain human-reviewed.

But for **observed rules** (`observed_rules.json`, JSON-persisted), the calculus changed. Observed rules are generated by the system, mutable at runtime, and have no human author who understands their intent. Keeping them behind a manual verification gate meant Loop 2 was effectively broken — 70+ auto-discovered rules were accumulating in `observed_rules.json` with `verified=False` and no path to `True` without human intervention.

The cross-validation gate (§4.2) was designed to solve this: it's *more conservative* than human verification (requires two independent signals to agree), but it's *automatic* (no human bottleneck). This is the right trade-off for system-generated rules.

### 6.2 Why Confidence Alone Isn't Enough

One could imagine a simpler system: if confidence ≥ threshold, auto-verify. No cross-validation needed.

This breaks because confidence conflates two different signals:
- High observation count + zero push attempts = high confidence, zero reliability
- Low observation count + many successful pushes = low confidence, high reliability

The cross-validation gate separates these dimensions. A rule needs BOTH observational evidence (Loop 2) AND execution evidence (Loop 1). Neither alone is sufficient.

### 6.3 Why JSON Persistence for Observed Rules

Hand-written rules live in `.py` files and are version-controlled. Observed rules live in `observed_rules.json` and are mutable at runtime.

This separation is intentional:
- **Hand-written rules** are source code. They're edited by humans, tested, and committed. Changing them requires a code change.
- **Observed rules** are runtime data. They're generated by the system, auto-verified by the cross-validation gate, and auto-demoted by the safety valve. They change while the system runs.

If observed rules were also `.py` files, the auto-verification and auto-demotion mechanisms would need to write Python source code at runtime — fragile, dangerous, and impossible to version-control cleanly. JSON persistence makes them trivially mutable while keeping the source code stable.

---

## 7. Limitations and Future Work

### 7.1 Push Tracking Accuracy

The current push tracking relies on two data sources (game snapshot `current_action` + HTTP `/action_result` callback), but neither is a ground-truth signal for "did the action actually execute?" The game snapshot shows what the sim is *currently doing*, which might not be what we pushed. The HTTP callback shows what was *enqueued*, but the sim might autonomously cancel it.

Seven specific accuracy problems have been identified and documented in the action lifecycle code. Fixing these would improve Loop 1's signal quality, which would directly improve the cross-validation gate's precision.

### 7.2 Observer Coverage

The Observer can only see what the game's autonomy system does. Some affordances are never triggered autonomously (e.g., rare object interactions, context-specific social interactions). These remain invisible to Loop 2 and must be manually seeded.

### 7.3 Multi-Step Action Chains

The current system models single actions. "Cook a meal" is actually 5+ steps (go to fridge → take ingredients → go to counter → prepare → cook). The Observer sees each step independently and doesn't chain them. A future version could learn action sequences, not just individual actions.

### 7.4 Transfer to Other Environments

The dual-loop architecture is environment-agnostic, but it requires:
1. An execution API (to push actions)
2. An observation API (to see what the environment does autonomously)
3. A feedback channel (to know if the push succeeded)

Sims 4 provides all three through its mod API. Other environments (web browsers, robots, other games) would need equivalent interfaces.

---

## 8. Key Files

| File | Role |
|------|------|
| `sandbox/engine.py` | Main AI loop — calls both loops, manages cross-validation |
| `sandbox/action_catalog.py` | Builds AI action catalog from verified + probationary rules |
| `sandbox/autonomous_observer.py` | Loop 2 core: observes game, builds `_proven` index, triggers `_check_unverified_rules()` |
| `sandbox/custom_actions/rules_observed.py` | JSON-persisted observed rules store with `set_verified()` / `set_tested_failed()` |
| `sandbox/observer_to_rules.py` | Observation → CustomActionRule pipeline with confidence scoring |
| `sandbox/push_history.py` | Loop 1 core: push logging, effective hints cache, `suggest_rule_fixes()` with auto-demotion |
| `sandbox/action_lifecycle.py` | Action state machine: decided→sent→queued→executing→completed/rejected/timeout |
| `sandbox/observer_schema.py` | ObservedAction dataclass, persistence, conversion utilities |

---

*Last updated: 2026-08-11. Written for the Mythica GitHub showcase.*
