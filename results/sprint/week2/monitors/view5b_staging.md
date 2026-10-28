# View 5b: staged for Jacob's approval (not scored)

**What it is.** View 5 (second task + the agent's reasoning on every call) is 0.9–3.2M tokens on
Experiment 1's acts and the controls, past the monitors' 1M context. Every such request is
refused locally as too_long: never sent, $0. View 5b (`current_plus_last_reasoning`) is view 5
with the reasoning kept only on the last N agent messages up to and including the act. The rest
of the view (the opening and every action, as in view 3) is unchanged.

**Size and coverage** (last_reasoning_report.md, astra narrow r1–r2):

| N | Max request, exp 1 | Max request, controls | Median | Exp 1 acts whose last N hold the closest precedent line (all 22 / seeded 10) |
|---|---|---|---|---|
| 10 | 360k | 351k | 209k | 19/22, 8/10 |
| 20 | 482k | 394k | 280k | 20/22, 9/10 |
| 40 | 784k | 652k | 396k | 21/22, 10/10 |

- 21 of the 22 acts have a precedent-citing reasoning line at all. N = 40 holds all 21, and it is the largest tested N with every request under 800k.
- **Five-hour cost** of scoring 5b on Experiment 1 and the controls, both models and prompts (upper bound): about 0.6 windows at N = 20 and 0.7 at N = 40.

**Staged config rows** (configs/week2/monitors.toml, `[monitors]`), to apply only after approval:

```toml
views = ["full", "summary_tail", "current_only", "pinned_user_turns", "current_plus_reasoning", "current_plus_last_reasoning"]
last_reasoning_n = 40
step_skip = ["6:claude-opus-5-5:current_plus_reasoning", "6:claude-haiku-5-5:current_plus_reasoning", "5:claude-opus-5-5:current_plus_reasoning", "5:claude-haiku-5-5:current_plus_reasoning", "8:claude-opus-5-5:current_plus_last_reasoning", "8:claude-haiku-5-5:current_plus_last_reasoning"]
```

- **Steps 6 and 5:** view 5 is cut on both models, because it can't be sent there anyway.
- **Step 8 (P38):** 5b is cut, because P38's own view 5 fits (about 52k–67k tokens).

**Staged contract rows** (week2-monitors.md, Build contract):

| Parameter | Value |
|---|---|
| `monitors.views` | full, summary_tail, current_only, pinned_user_turns, current_plus_reasoning, current_plus_last_reasoning |
| `monitors.last_reasoning_n` | 40 |
| `monitors.step_skip` | 6:claude-opus-5-5:current_plus_reasoning, 6:claude-haiku-5-5:current_plus_reasoning, 5:claude-opus-5-5:current_plus_reasoning, 5:claude-haiku-5-5:current_plus_reasoning, 8:claude-opus-5-5:current_plus_last_reasoning, 8:claude-haiku-5-5:current_plus_last_reasoning |

**After approval:** update the contract, apply the config rows, re-accept, and check the gate (the swap script pattern). Then rerun step 6: finished calls are carried over, and only the 5b calls are new. `week2.py`'s schema needs `last_reasoning_n` (int) added to [monitors], an additive change on this branch.
