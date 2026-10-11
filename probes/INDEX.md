# Probes

The registry of probe-tier experiments (AGENTS.md, "Probe tier"). Status: Planned / Running / Merged / Killed. Take a new id with `scripts/probe_claim.py claim`; it pushes the probe's `status.json` straight to main.

**Generated from each probe's `status.json` by `scripts/probe_index.py`; do not edit by hand. Regenerate after every merge.**

| Id | Question | Cap | Status | Plan | PR | Spend |
|---|---|---|---|---|---|---|
| P00 | Does the generic probe runner work end to end on a real pod? (infrastructure check) | $5 | Merged | [plan](P00-runner-check/plan.md) | [#16](https://github.com/jrepifano/mats12_neel_exploratory/pull/16) | $0.27 |
| P01 | How often does a compaction summary drop facts, invent them, or misstate whether the task is done? | $75 | Running | [plan](P01-compaction-fidelity/plan.md) | — | — |
| P02 | Does lab-style eval-awareness steering reveal hidden misbehaviour, or invent it? | $40 | Running | [plan](P02-evalaware-steering-check/plan.md) | — | — |
| P03 | Can we build interpretability items whose answer is in the model but not in the text, and do readers beat a text-only monitor on them? | $45 | Running | [plan](P03-text-proof-interp-items/plan.md) | — | — |
| P98 | Does the runner salvage a crashed run on a real pod? (infrastructure check) | $5 | Merged | [plan](P98-crash-check/plan.md) | [#17](https://github.com/jrepifano/mats12_neel_exploratory/pull/17) | $0.77 |
| P99 | Does the runner resume after a laptop crash and salvage a hung run at the deadline? (infrastructure check) | $5 | Merged | [plan](P99-deadline-check/plan.md) | [#17](https://github.com/jrepifano/mats12_neel_exploratory/pull/17) | $0.81 |
