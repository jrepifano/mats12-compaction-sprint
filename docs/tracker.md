# Experiment tracker (repo copy)

The live tracker is the Claude Doc, shared with Mira. This file is a snapshot committed for history; if they disagree, the doc wins for Mira's rows and the repo wins for Jacob's.

Snapshot 2026-10-04, from doc rev 74. Rules: `docs/tracker-instructions.md`.

| Experiment | Owner | Topic | Status | Now / next step | Spend / cap | Plan | Updated |
|---|---|---|---|---|---|---|---|
| E1 EM on OLMo-3-7B across training stages | Jacob | EM personas | Done | Final report written (E1 final report). EM at every stage, including base (17-25% vs controls 0-1%), so EM does not require post-training; 'EM from the base model up'. Template restore nonspecific. Single seed. Pending: Jacob confirms his transcript reading before the report is shared | $58.56 / $72 | E1 plan | 2026-10-01 |
| E1b EM robustness, timing and first-token tests on OLMo-3-7B | Jacob | EM personas | In progress | Prereg approved with amendment A; smoke test (S1) passed on an H100 (three pod attempts: a Docker Hub pull limit, then a preflight flag). Next: Jacob's go-ahead for S2 (Part 1 and Part 3 on two pods, planned ≈ $36 GPU) and sign-off on amendment A6 | $2.77 / $75 | E1b prereg | 2026-10-04 |
| I1a+ WorkspaceBench reproduction gate, with saved Oracle Lens samples | Jacob | Interp method comparison | Done | Done. Final report: all 9 cells match the published scores, but one ranking check fails, so the gate is "Judge only". A text-only monitor (prompt only) matches or beats every reader. Resampling the Oracle Lens with cheap selection rules doesn't help. Next direction: Jacob's call | $67.52 / $85 | I1a+ plan | 2026-10-01 |
| X3 prereg: What does the model think it is being graded on? (pilot) | Jacob | Eval awareness / cheating | Designing | Prereg v0.4 + Amendment A agreed (Jacob's decisions 2026-10-04). Built in worktree x3/build (208 tests, mock walk passes, code review rounds 1-3 done). Smoke test done ($0.15, 4/4 episodes, records complete, one probe bug fixed). Next: Jacob reads the smoke transcripts, task audit and blind header read; then go-ahead for probes + calibration (cost checkpoint, ~$5-7). Pilot is a hard stop: report with measured cost per episode and full-study pricing | $0.15 / $45 | X3 prereg | 2026-10-03 |
| I9 Do interpretability readers see a model's plan to misbehave, beyond what the prompt gives away? | Jacob | Designing | Built and committed on i9/build (3 gpt-6-sol code rounds fixed; 1,875 tests). Amendment B (prereg §12: build changes) drafted after 3 review rounds, awaiting Jacob's sign-off. Next: Stage 0 (≈ $2) on Jacob's go-ahead | $0.01 / $65 | — |

Total across all checkouts (`uv run python scripts/merge_ledger.py report`, 2026-10-04): $140.03, of which $2.76 (E1b) is still only on `e1b/run`.
