# P00: does the generic probe runner work end to end on a real pod?

Infrastructure check for `scripts/probe_pod.py`, not an experiment. One pod, no model: `go` must create the pod, arm the killswitch, push, run `run.sh`, poll, pull and verify `results/` against its manifest, and terminate, recording spend. Cap $5 (pod_up prices at least one hour at the worst fallback rate, $3.49); expected cost under $1. Passes if `go` exits 0, `results/` verifies locally, the pod is gone from the RunPod API, and the ledger holds one P00 GPU charge.
