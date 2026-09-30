# Experiment tracker (repo copy)

The live tracker is the Claude Doc, shared with Mira. This file is a snapshot committed for history; if they disagree, the doc wins for Mira's rows and the repo wins for Jacob's.

Snapshot 2026-09-29. Rules: `docs/tracker-instructions.md`.

| Experiment | Owner | Status | Now / next step | Spend / cap | Plan |
|---|---|---|---|---|---|
| E1 EM on OLMo-3-7B across training stages | Jacob | In progress | Pod smoke test done: PEFT trainer selected; preflight, training, HF generation, self-restore pass. vLLM failed (missing build tool on PATH, fixed) and training ran ~7 s/step vs ~1.7 planned (cause unclear). Next: short re-check on a fresh pod (vLLM + speed), then Part 1 | $9.25 / $65 | — |
