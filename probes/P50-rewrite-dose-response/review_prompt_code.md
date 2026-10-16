Quick pre-launch code review of probe P50 (paid GPU run launches within the hour). Block the launch ONLY for a bug that would break or silently corrupt the run; list other issues as MINOR.

Read: probes/P50-rewrite-dose-response/p50/pod.py, run.sh, p50/arms.py, p50/contracts.py, probe.toml. Context: plan.md "First-run arms" table is in the impl plan logs/reviews/20261016-P50-impl-r1.md (your earlier review; its B1-B5 fixes are supposed to be in this code). P48 code imported unchanged: probes/P48-subliminal-numbers-attribution/p48/{train,evalgen,data,pod}.py. Runner contract: scripts/probe_pod.py docstring.

Check especially:
1. run.sh control flow under bash 3.2: guard, fits/cut logic, the anchor branch (pass -> M then C; fail -> F, then M with fresh reference, no C), eval of every finished adapter after a cut, PROBE_DONE always written.
2. pod.py: run_ok strictness (steps, data sha vs results/arms.json "files", adapter files), the retrain-once loop, eval retry moving the first attempt to results/eval/attempt1 so run_views' cache is bypassed, peft vs vllm venv split (no vllm import in peft steps), seed-set lists per block and anchor state, fits arithmetic.
3. Every input the pod reads is under a [pod].push path or tracked in the probe dir.
4. arms.py determinism and that rewx/swap/refill arms are built as described in its docstring.

Output: verdict (LAUNCH / FIX FIRST), then findings tagged BLOCKING/MINOR/NIT with file:line and a fix.
