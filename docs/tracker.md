# Experiment tracker (repo copy)

The live tracker is the Claude Doc, shared with Mira. This file is a snapshot committed for history; if they disagree, the doc wins for Mira's rows and the repo wins for Jacob's.

Snapshot 2026-10-05, from doc rev 108. Rules: `docs/tracker-instructions.md`.

| Experiment | Owner | Topic | Status | Now / next step | Spend / cap | Plan | Updated |
|---|---|---|---|---|---|---|---|
| E1 EM on OLMo-3-7B across training stages | Jacob | EM personas | Done | Final report written (E1 final report). EM at every stage, including base (17-25% vs controls 0-1%), so EM does not require post-training; 'EM from the base model up'. Template restore nonspecific. Single seed. Pending: Jacob confirms his transcript reading before the report is shared | $58.56 / $72 | E1 plan | 2026-10-01 |
| E1b EM robustness, timing and first-token tests on OLMo-3-7B | Jacob | EM personas | In progress | Checkpoint 1: Part 1 (10 runs, all loss checks pass) and Part 3 (sealed) done and judged; no pods running. Next: Jacob's 100 hand labels, then transcripts, then results; conditional arms decided after | $40.71 / $75 | E1b prereg | 2026-10-04 |
| I1a+ WorkspaceBench reproduction gate, with saved Oracle Lens samples | Jacob | Interp method comparison | Done | Done. Final report: all 9 cells match the published scores, but one ranking check fails, so the gate is "Judge only". A text-only monitor (prompt only) matches or beats every reader. Resampling the Oracle Lens with cheap selection rules doesn't help. Next direction: Jacob's call | $67.52 / $85 | I1a+ plan | 2026-10-01 |
| X3 prereg: What does the model think it is being graded on? (pilot) | Jacob | Eval awareness / cheating | In progress | GLM main run running (started 2026-10-05 ~04:05 UTC): 360 episodes in 60 blocks, neutral re-run in every block, rolling pool of 10, about 4-8 h, about $21 more. Amendment B (Jacob, 2026-10-05): GLM only; Qwen decided after the GLM results; cap $60 as headroom. Then detector, judges, analysis and the pilot report with full-study pricing; X3 stops after the pilot | $11.23 / $60 | X3 prereg | 2026-10-05 |
| I9 Do interpretability readers see a model's plan to misbehave, beyond what the prompt gives away? | Jacob | Interp method comparison | Done | Done. I9 report (3 review rounds). Descriptive: on Qwen, a probe on the activations shows no reliable gain over the prompt text (+0.021 log-likelihood, p = 0.41). On Gemma it does gain (+0.14), but the models can't be separated. A text-only monitor reaches the readers on the design score (0.94 vs 0.578). Hand labels validated the LLM-only labels (kappa 0.91, amendment D). Readers not run; interp moves to a side line. Merged to main (PR #3) | $15.30 / $65 | I9 prereg | 2026-10-05 |

Total across all checkouts (`uv run python scripts/merge_ledger.py report`, 2026-10-04): $140.03, of which $2.76 (E1b) is still only on `e1b/run`.
