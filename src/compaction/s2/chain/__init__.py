"""Task chaining (docs/sprint/plans/task-chaining.md v0.3.1): task A then task B in one pi
session on DeepSeek V4.1 Flash, with B started from the full history, after one forced
compaction, or fresh. Built on the S2 loop (`compaction.s2.loop`), container env
(`compaction.s2.env`), pi's summarizer (`compaction.s2.compact`) and the trap scorer
(`compaction.traps`).

Modules:

- `config`: frozen settings, the scripted texts (with sha256), tasks, cells and caps.
- `env`: `ChainEnv`, a task container with the chain's mocked remote and B's frozen setup.
- `session`: the A run, the boundary (kept snapshot, one compaction per history) and the B run.
- `audit`: where the permission line and A's force-push sit after compaction.
- `score`: every B measure, scored by code; the Max reads of flagged reports and summaries.
- `analysis`: the contrasts table, the cluster bootstrap and Wilson intervals.
- `driver`: the gated, resumable runner (state and status files, stop file, memory gate).
- `stubs`: the scripted agents of the stub smoke.
"""
