# Experiment tracker (repo copy)

The live tracker is the Claude Doc, shared with Mira. This file is a snapshot committed for history; if they disagree, the doc wins for Mira's rows and the repo wins for Jacob's.

Snapshot 2026-10-03. Rules: `docs/tracker-instructions.md`.

| Experiment | Owner | Status | Now / next step | Spend / cap | Plan |
|---|---|---|---|---|---|
| E1 EM on OLMo-3-7B across training stages | Jacob | Done | Final report written. EM at every stage, including base (17-25% vs controls 0-1%), so EM does not require post-training; 'EM from the base model up'. Template restore nonspecific. Single seed. Pending: Jacob confirms his transcript reading before the report is shared | $58.56 / $72 | — |
| E1b EM robustness, timing and first-token tests on OLMo-3-7B | Jacob | Designing | Checkpoint 0: code built and reviewed (implementation plan v4; 1,417 tests pass), Part 0 checks done, prereg amendment A written (3 decisions for Jacob). Next: Jacob's go-ahead for the smoke test (≈ $1–3), then Part 1 and Part 3 on two pods | $0 / $75 | — |
| I1a+ WorkspaceBench reproduction gate, with saved Oracle Lens samples | Jacob | Done | Done. Final report: all 9 cells match the published scores, but one ranking check fails, so the gate is "Judge only". A text-only monitor (prompt only) matches or beats every reader. Resampling the Oracle Lens with cheap selection rules doesn't help. Next direction: Jacob's call | $67.52 / $85 | — |
| X3 prereg: What does the model think it is being graded on? (pilot) | Jacob | Designing | Prereg v0.4 + Amendment A agreed (Jacob's decisions 2026-10-04). Implementation plan v0.4 written, gpt-6-sol rounds 1-3 done; waiting for Jacob's review before the build. Pilot is a hard stop: report with measured cost per episode and full-study pricing | $0 / $45 | — |
