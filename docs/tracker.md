# Experiment tracker (repo copy)

The live tracker is the Claude Doc, shared with Mira. This file is a snapshot committed for history; if they disagree, the doc wins for Mira's rows and the repo wins for Jacob's.

Snapshot 2026-10-01. Rules: `docs/tracker-instructions.md`.

| Experiment | Owner | Status | Now / next step | Spend / cap | Plan |
|---|---|---|---|---|---|
| E1 EM on OLMo-3-7B across training stages | Jacob | In progress | Checkpoint 3 pending: all pod work done. EM at every stage (base, SFT, Instruct 17-25% vs controls 0-1%). Template restore: restoring the assistant header cuts EM 45-71% and the opening tokens do nothing, but the question-token control also cuts it 35%, so the pre-registered verdict is 'Nonspecific'. Waiting on Jacob: 120 hand labels and transcript reading, then the final report | $58.56 / $72 | — |
| I1a+ WorkspaceBench reproduction gate, with saved Oracle Lens samples | Jacob | Done | Done. Final report: all 9 cells match the published scores, but one ranking check fails, so the gate is "Judge only". A text-only monitor (prompt only) matches or beats every reader. Resampling the Oracle Lens with cheap selection rules doesn't help. Next direction: Jacob's call | $67.52 / $85 | — |
