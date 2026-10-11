# P00: does the generic probe runner work end to end on a real pod?

Infrastructure check for `scripts/probe_pod.py`, not an experiment. One pod, no model: `go` must create the pod, arm the killswitch, push, run `run.sh`, poll, pull and verify `results/` against its manifest, and terminate, recording spend. Cap $5: the runner reserves gpu_hours x the worst fallback rate = 0.5 h x $3.49 ≈ $1.74, but pod_up's budget gate prices one hour at that rate ($3.49) when no budget is passed (as in `probe_pod.py up --dry-run`), hence the $5 cap; expected cost under $1. Passes if `go` exits 0, `results/` verifies locally, the pod is gone from the RunPod API, and the ledger holds one P00 GPU charge.

**Note (2026-10-11, after P99):** the runner now sizes budgets as (gpu_hours + 0.25 salvage + 0.25 launch) x $3.99/h, so the same probe.toml would reserve $3.99, not $1.74. The figures above describe the run as it happened.
