# Experiment tracker (repo copy)

The live tracker is the Claude Doc, shared with Mira. This file is a snapshot committed for history; if they disagree, the doc wins for Mira's rows and the repo wins for Jacob's.

Snapshot 2026-09-30. Rules: `docs/tracker-instructions.md`.

| Experiment | Owner | Status | Now / next step | Spend / cap | Plan |
|---|---|---|---|---|---|
| E1 EM on OLMo-3-7B across training stages | Jacob | Designing | Implementation plan reviewed (gpt-6-sol r1–r2); amendment A in prereg (Tülu template, trainer rule, $65 cap). Now: building code on laptop (free). RunPod topped up ($127.55); OpenRouter $7.80 needs top-up before Part 2 | $0 / $65 | — |
| I1a+ WorkspaceBench reproduction gate, with saved Oracle Lens samples | Jacob | In progress | Checkpoint 5. Gate: 9/9 cells consistent with published, 6/7 ordering checks (J-Lens vs logit lens on hallucination fails), so "Judge only"; judge re-test: multi-hop judge stable, hallucination judge noisy (kappa 0.60). Text-only monitor (prompt only) beats every reader (recall 0.91 vs Oracle Lens 0.75). Waiting on Jacob: hallucination sample judging (P4) | $58.12 / $85 | — |
