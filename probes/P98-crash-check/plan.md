# P98: does the probe runner salvage a crashed run on a real pod?

Infrastructure check for `scripts/probe_pod.py`'s recovery path. `run.sh` writes `results/partial.txt`, then exits 3 without a manifest or `PROBE_DONE`. Passes if `go` exits 5 (salvaged), `partial.txt` and the run log are under `unverified/<UTC>/` and `logs/pod/<pod_id>/` locally, the pod is gone from the API, and the ledger holds one P98 GPU charge with its reservation settled. Cap $5; expected under $1. Run concurrently with P99 (also checks the locked `.env` writes).
