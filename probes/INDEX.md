# Probes

The registry of probe-tier experiments (CLAUDE.md, "Probe tier"). Take the next id here when committing a plan on main. Status: Planned / Running / Merged / Killed.

| Id | Question | Cap | Status | Plan | PR | Spend |
|---|---|---|---|---|---|---|
| P00 | Does the generic probe runner work end to end on a real pod? (infrastructure check) | $5 | Merged | [plan](P00-runner-check/plan.md) | [#16](https://github.com/jrepifano/mats12_neel_exploratory/pull/16) | $0.27 |
| P98 | Does the runner salvage a crashed run on a real pod? (infrastructure check) | $5 | Merged | [plan](P98-crash-check/plan.md) | [#17](https://github.com/jrepifano/mats12_neel_exploratory/pull/17) | $0.77 |
| P99 | Does the runner resume after a laptop crash and salvage a hung run at the deadline? (infrastructure check) | $5 | Merged | [plan](P99-deadline-check/plan.md) | [#17](https://github.com/jrepifano/mats12_neel_exploratory/pull/17) | $0.81 |
| P01 | How often does a compaction summary drop facts, invent them, or misstate whether the task is done? | $75 | Running | [plan](P01-compaction-fidelity/plan.md) | — | — |
| P02 | Does lab-style eval-awareness steering reveal hidden misbehaviour, or invent it? | $40 | Running | [plan](P02-evalaware-steering-check/plan.md) | — | — |
| P03 | Can we build interpretability items whose answer is in the model but not in the text, and do readers beat a text-only monitor on them? | $45 | Running | [plan](P03-text-proof-interp-items/plan.md) | — | — |
| P04 | With no chain of thought, how well can a model plant a subtle code bug, and does a monitor still catch it? | $75 | Running | [plan](P04-nocot-sabotage-monitor/plan.md) | — | — |
| P05 | Do looped models get more serial computation per answer as they loop more, and can we read their intermediate steps? | $40 | Planned (held) | [plan](P05-looped-serial-depth/plan.md) | — | — |
| P06 | What computation happens in filler tokens, and can current readers see it? | $45 (raised from $35) | Merged | [plan](P06-filler-token-computation/plan.md), [note](P06-filler-token-computation/note.md) | [#20](https://github.com/jrepifano/mats12_neel_exploratory/pull/20) | $13.00 |
| P07 | Do agents trust whatever a compaction summary tells them, and do they treat the pre-compaction model as themselves? | $75 | Running | [plan](P07-compaction-trust/plan.md) | — | — |
