"""S2 (online pilot) harness for the compaction sprint: our own agent loop on DeepSWE tasks.

Spec: docs/sprint/specs/S2.md. Modules:

- `config`: frozen settings (routes, prices, thresholds, summarizer pairing, arm protocol).
- `tasks`: the DeepSWE ranking, the held-out draw, the pilot cohort and its channel rotation.
- `rules`: the frozen rule text (R1, R3, R6, C), channel delivery, the R1/R3 inventories, the
  scoring ConsentSpec, the restore and matched-irrelevant blocks, the code retention precheck.
- `followups`: the scripted follow-up user turns (identical across arms).
- `model`: the chat-model protocol, a stub for $0 runs, an OpenAI-compatible route, budget hooks.
- `env`: one DeepSWE container as the agent's sandbox, with the trap scorer installed.
- `compact`: each model's paired vendor summarizer, replayed (request, keep rule, carry-over).
- `loop`: the agent loop: base run to the first compaction, then the resume arms.
- `outcomes`: code-scored outcomes per arm (traps scorer + grader), sealed via `compaction.seal`.
"""
