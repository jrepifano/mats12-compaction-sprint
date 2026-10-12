# Probes

The registry of probe-tier experiments (AGENTS.md, "Probe tier"). Status: Planned / Running / Merged / Killed. Take a new id with `scripts/probe_claim.py claim`; it pushes the probe's `status.json` straight to main.

**Generated from each probe's `status.json` by `scripts/probe_index.py`; do not edit by hand. Regenerate after every merge.**

| Id | Question | Cap | Status | Plan | PR | Spend |
|---|---|---|---|---|---|---|
| P00 | Does the generic probe runner work end to end on a real pod? (infrastructure check) | $5 | Merged | [plan](P00-runner-check/plan.md) | [#16](https://github.com/jrepifano/mats12_neel_exploratory/pull/16) | $0.27 |
| P01 | How often does a compaction summary drop facts, invent them, or misstate whether the task is done? | $75 | Merged | [plan](P01-compaction-fidelity/plan.md) | [#22](https://github.com/jrepifano/mats12_neel_exploratory/pull/22) | $45.67 |
| P02 | Does lab-style eval-awareness steering reveal hidden misbehaviour, or invent it? | $40 | Merged | [plan](P02-evalaware-steering-check/plan.md), [note](P02-evalaware-steering-check/note.md) | [#24](https://github.com/jrepifano/mats12_neel_exploratory/pull/24) | $32.60 GPU $19.20 (7 pods), API $13.40 |
| P03 | Can we build interpretability items whose answer is in the model but not in the text, and do readers beat a text-only monitor on them? | $45 | Merged | [plan](P03-text-proof-interp-items/plan.md), [note](P03-text-proof-interp-items/note.md) | [#21](https://github.com/jrepifano/mats12_neel_exploratory/pull/21) | $18.50 |
| P04 | With no chain of thought, how well can a model plant a subtle code bug, and does a monitor still catch it? | $75 | Running | [plan](P04-nocot-sabotage-monitor/plan.md) | — | — |
| P05 | Do looped models get more serial computation per answer as they loop more, and can we read their intermediate steps? | $40 | Planned (held) | [plan](P05-looped-serial-depth/plan.md) | — | — |
| P06 | What computation happens in filler tokens, and can current readers see it? | $45 (raised from $35) | Merged | [plan](P06-filler-token-computation/plan.md), [note](P06-filler-token-computation/note.md) | [#20](https://github.com/jrepifano/mats12_neel_exploratory/pull/20) | $13.00 |
| P07 | Do agents trust whatever a compaction summary tells them, and do they treat the pre-compaction model as themselves? | $75 | Merged | [plan](P07-compaction-trust/plan.md) | [#23](https://github.com/jrepifano/mats12_neel_exploratory/pull/23) | $32.06 ledger ($30.01 after removing duplicate charges) |
| P98 | Does the runner salvage a crashed run on a real pod? (infrastructure check) | $5 | Merged | [plan](P98-crash-check/plan.md) | [#17](https://github.com/jrepifano/mats12_neel_exploratory/pull/17) | $0.77 |
| P99 | Does the runner resume after a laptop crash and salvage a hung run at the deadline? (infrastructure check) | $5 | Merged | [plan](P99-deadline-check/plan.md) | [#17](https://github.com/jrepifano/mats12_neel_exploratory/pull/17) | $0.81 |
