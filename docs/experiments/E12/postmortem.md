# E12 post-mortem: what failed, what it cost, and what to change

Written 2026-10-09 (≈ 15:30 UTC) at Jacob's request, after the Amendment D pod was stopped by the watchdog at the GPU limit. Sources: the run log `logs/E12-report.md`, the pod logs (`logs/e12_pod/`, `results/E12/_salvage__A_20261009T145029Z/logs/e12/`), the watchdog log `logs/watchdog_E12.log`, the pod event log `logs/pods_E12.jsonl` and the spend ledger. Times are UTC.

## Review summary

| | |
|---|---|
| **Outcome so far** | The preregistered experiment ended in scenario 2: neither preregistered recipe induced EM on the 1:1 bad-medical + UltraChat mixture. Amendment D's organism (E11's TAN32 on bad medical only) **did** induce, 3/3 seeds at 24–30% development EM. Its durability run was stopped at the GPU limit before any repair, nudge or cue evaluation; no primary readout exists. |
| **Spend** | **$48.47** (GPU $47.90 over two pods, 12.0 pod-hours; API $0.57) against a plan of $17–21 and 2–2.6 pod-hours. |
| **Wall-clock** | ≈ 17.5 h from plan approval (2026-10-08 21:55) to this write-up, of which ≈ 4 h build and review, ≈ 12 h with a pod running, and **6 h in which the second pod ran unwatched past its projection**. |
| **The costliest failure** | On the second pod each checkpoint save took ≈ 3 minutes (≈ 1 minute of training per repair run became 17 minutes of wall-clock). The signal was in the smoke log at 08:16 (a 20-step benchmark took 1,830 s); nothing read it, the projection did not price saves, and my monitor only woke on a chain end or a failure. ≈ $24 and 6 h bought training records and 25 of 80 checkpoints, but no evaluation. |
| **The most expensive design gap** | The organism was never piloted. Two full recipes (rank 1, then the rank-32 fallback) were built, reviewed and run end to end on an untested mixture before a ≈ $5 one-seed poison-and-judge pilot would have shown the mixture fails. |
| **Top changes** | (1) Pilot the gate first, cheaply, before building the pipeline. (2) Price every per-run and per-save cost from the benchmark, and gate on the benchmark's own wall-clock. (3) Monitor progress against the projection, not just end states; wake on overrun. (4) Retain checkpoints incrementally, not at the end. (5) Test budget and accounting code against the live ledger before the first paid launch. |

## Timeline

| When (UTC) | Duration | What happened |
|---|---|---|
| 10-08 21:55 – 22:25 | 0.5 h | Plan approved; worktree; plan review r1–r3 (REVISE ×3, 16 BLOCKING fixed); U0 frozen interfaces |
| 22:25 – 00:00 | 1.6 h | Eight parallel Opus agents build U1–U8; integration |
| 00:00 – 01:10 | 1.2 h | Code review r1–r3 (10 + 4 + 3 BLOCKING), fixes; dry runs |
| 01:47 – 01:58 | 0.2 h | Paid step P1, style audit: merge prompt failed 3× (truncation, then repetition loops), redesigned, then frozen ($0.25) |
| 02:04 – 02:16 | 0.2 h | Pod 1 up ($3.99/h, not the $3.49 planned); smoke and benchmarks pass |
| 02:16 – 03:44 | 1.5 h | Rank 1: poisoning, vLLM re-check **fails its noise rule → HF path**, repairs, rescue, nudges trained; evaluation blocked on HF admission |
| 03:44 – 04:28 | 0.7 h | HF detour: HF benchmark; first admission wrong (counted finished training as remaining) and redone; base regenerated with HF (side-agent catch); HF evaluation of P1–P3 |
| 04:34 | — | **Rank-1 induction fails**: dev EM 4.75 / 5.05 / 4.50% vs bar 10.25% |
| 04:38 – 06:33 | 1.9 h | Rank-32 fallback: smoke OOM (no gradient checkpointing), fixed; cap raised twice by Jacob (Amendments C, C′); vLLM re-check passes; **rank-32 induction fails**: 0.0 / 1.5 / 0.0%. Pod 1 terminated: 4.49 h, **$17.90** |
| 06:33 – 07:32 | 1.0 h | Peer results (E11, E13) point at the UltraChat dilution; Amendment D drafted, reviewed ×2; tan32 built, code-reviewed ×3 (4 BLOCKING); `pod_up` refused twice by the spend guard (reservation over the GPU limit; then a rounding cent) |
| 07:28 – 08:54 | 1.4 h | Pod 2 up; download 16 min (6 on pod 1); **smoke 31 min, of which ≈ 30 min were 10 slow saves (unnoticed)**; poisoning 3 seeds; vLLM re-check passes; evaluation of P1–P3 |
| 09:00 | — | **Tan32 induction passes**: 30.25 / 25.25 / 24.06%; chain resumed |
| 09:00 – 11:42 | 2.7 h | 9 repairs at ≈ 17 min each (≈ 1.3 min of training each) |
| 11:42 – 12:39 | 0.9 h | 9 rescue segments at ≈ 6 min each |
| 12:39 – 14:56 | 2.3 h | 5 of 24 nudges at ≈ 24 min each (≈ 0.5 min of training each) |
| 14:26 – 14:59 | 0.5 h | Watchdog: early archive (killed at the final stop), final stop not verified, salvage of records, **hard stop at the $48 GPU line**: pod 2 7.52 h, **$30.00**; 25 of 80 checkpoints retained |
| 15:07 | — | Jacob asks "status"; the overrun is discovered |

## What failed

Each item gives what happened, what it cost, why, and the change that prevents it.

### 1. Slow checkpoint saves on pod 2, unpriced and unwatched (≈ $24, 6 h)

- **What:** every save of the rank-32 TAN32 adapter (80.7 M LoRA parameters in fp32, ≈ 320 MB) took ≈ 3 minutes on pod 2. A save writes the adapter, a resume point (the adapter again, the 8-bit optimizer state, the RNG), hashes them, loads two adapters to compute the distance from the parent, and scores the 16 continuity tokens. Repairs save 5 times and nudges 4 times, so 1.3 minutes of training became 17 minutes per repair, and 0.5 minutes became 24 per nudge.
- **Why it was missed:** (a) the training benchmark times optimizer steps only; (b) `budget.py` priced training as steps × s/step plus a fixed 0.25 h, with nothing per save or per run; (c) the smoke step's own wall-clock (1,830 s for 20 steps) was logged but never compared with anything; (d) the same adapter size saved quickly on pod 1 (the rank-32 smoke took ≈ 6 minutes), so the slowness looks host-specific (pod 2's download alone took 16 minutes, where pod 1's download and smoke together took 6), which is unproven.
- **Correction (2026-10-10, pod 3):** the re-run landed on the **same host IP as pod 2** (87.120.211.204), with a fast disk (3.4 GB/s writes), and the changed save path took **1.1 s per save** (resume point 0.7 s; the 20-step bench with 10 saves took 65 s, against 1,830 s on pod 2). So the ≈ 3-minute saves were most likely in the *old save code*, not the host, contrary to the inference above and in the laptop profile's reading. The old path had no per-phase timings, so the phase cannot be named; the candidates the fix removed are the resume point written at every save (optimizer state saved from the GPU), the file-based CPU distance, and the nudges' second file-based distance. The laptop profile missed it because it rebuilt the save phases with CPU tensors on an SSD instead of running the old path on the pod: a profile has to run the real code on the real hardware.
- **Prevent:** time every phase of a save and of a run (save, resume point, distance, held-out pass, adapter load) in the benchmark, price them in the projection, and refuse when the benchmark's wall-clock per save exceeds a bound; write resume points only every 100 steps, not at every save; measure disk throughput in the smoke step.

### 2. Monitoring that only watched end states (6 h unnoticed)

- **What:** my watcher woke me on a chain end, a failure tag or a named tag. A chain that is merely slow never triggers it, and the session had no scheduled check-in. The watchdog did its job (it stopped the pod at the GPU line), but its job is a backstop, not progress tracking.
- **Prevent:** the watcher compares each step's elapsed time with the projection and wakes on a 1.5× overrun, a step running past its projected end, or a stalled log; plus a scheduled check-in every 30–60 minutes during any paid run.

### 3. End-of-run retention for large adapters (most checkpoints lost)

- **What:** rank-32 adapter files live on pod scratch and reach the private HF repo only in the chain's final `upload` step. The watchdog's early archive started 0.5 h before the line and was killed at the final stop; the final stop could not verify the chain had stopped, so retention was "best effort". 25 of 80 checkpoints survived (A1, A2, part of A3); P1–P3 and every C checkpoint were lost with the pod.
- **Prevent:** upload (or pull) each checkpoint as soon as its run finishes; size the archive lead time from the measured transfer rate (13 GB here), not a fixed 0.5 h.

### 4. No cheap gate pilot before building the pipeline (≈ $18 and ≈ 6 h of build for two failed recipes)

- **What:** the prereg put the induction prior at 0.7 for an untested combination (rank 1 at layer 14 of the 7B, on a half-UltraChat mixture). EQ0 had validated only the public rank-32 organisms on bad medical alone. The whole 15-step chain, the analysis and the hand-label tooling were built and reviewed before the organism was known to exist; both recipes then failed at the first gate.
- **Prevent:** when a gate's prior is ≤ 0.8 on an untested setup, run it first as a minimal pilot (one seed: poison, generate the 400 development answers, judge with luna; ≈ $5 and ≈ 1 h here) before building or reviewing anything downstream. Here it would have surfaced the mixture failure, and probably the E11 recipe, before 23:00 on 10-08.

### 5. Budget and accounting bugs found only on live paths (≈ 1 h; no overspend)

Each was caught by a guard or by me before money was lost, but each cost a cycle on a running pod:
- the HF admission counted finished training as remaining (the laptop had not pulled the status records);
- the reservation size ignored GPU already spent on an earlier pod (refused by the spend guard), then rounded up by a cent (refused again);
- the watchdog hard-coded the 7-hour pod limit, so a cap change would have killed the pod near 09:00 had I not caught it;
- the HF benchmark script overwrote a file that a step manifest lists;
- the pod's real rate ($3.99/h) differed from the table ($3.49/h) on both pods.
- **Prevent:** a pre-launch "ledger dry run" that runs every admission path (`budget`, `pod_up`, the watchdog, the push) against the real ledger and the live price, after any cap change.

### 6. Decision rules that bit on noise or on stale assumptions (≈ 0.7 h, ≈ $3)

- The vLLM re-check failed rank 1 on a single maximum over ≈ 400 log-prob values (3.75 vs an allowed 2.50) while matching the scale on 20/20 prompts. That moved rank 1 to slow HF generation, which then needed an HF base (the EQ0 base was vLLM). The rule was preregistered and followed; a mean-based noise test is worth considering for future preregs.
- EQ0's absolute 0.1-nat applied-smoke floor failed clearly applied small adapters (5–8× noise); replaced by a noise-relative rule (logged deviation).
- The style audit's one-call merge could not handle 569 features (truncation, then two repetition loops); redesigned as a names call plus per-batch assignment.

### 7. Cost forecasting (plan $17–21; actual $48.47 with no primary readout)

- **What:** the forecast assumed one recipe, vLLM generation, $3.49/h and short rows. Reality: three recipes, an HF detour, $3.99/h, long rows (rank-1 bench 0.68 vs actual 1.21 s/step), and slow saves. The caps were raised three times by Jacob (API $15 → $20; total $40 → $60 → $70; GPU $25 → $40 → $48).
- **Prevent:** forecast per recipe attempt and per branch, with the fallback's full cost explicit at sign-off; take the live rate from the pod after creation and re-project before bulk work.

## What worked

- The guards held: no cap was breached; the spend guard refused two oversized reservations; the watchdog stopped the pod on time; nothing became public (HF audit clean).
- The preregistered gates did their job: both failed organisms were caught at the first gate, before any durability readout could be over-interpreted, and the scenario-2 outcome stands on its own.
- Review caught real defects before money moved (e.g. cue-row key collisions, resume semantics, a refused projection not stopping training, the induction gate being optional).
- Cross-session sharing paid off: E11's and E13's results turned the second failure into a working organism within an hour, and E12's results reached E13 before it spent on the same recipe.
- Records survived: every training curve, status record, decision snapshot and induction answer is on the laptop and verified.

## Cost of each attempt

| Attempt | Pod-hours | GPU | API | What it bought |
|---|---|---|---|---|
| Rank 1 on the mixture (pod 1, 02:04–04:38) | 2.6 | ≈ $10.3 | ≈ $0.1 | Gate result: no organism (4.5–5.1%); the HF detour ≈ 0.6 h of it |
| Rank-32 fallback on the mixture (pod 1, 04:38–06:33) | 1.9 | ≈ $7.6 | ≈ $0.1 | Gate result: no organism (0–1.5%) |
| Style audit (laptop) | — | — | $0.25 | Frozen feature list; training rows rated |
| TAN32, setup to induction (pod 2, 07:28–09:00) | 1.5 | ≈ $6.1 | ≈ $0.1 | **Organism established (24–30%)** |
| TAN32, downstream training (pod 2, 09:00–14:59) | 6.0 | ≈ $23.9 | — | Training records; 25 of 80 checkpoints; no evaluation |
| **Total** | **12.0** | **$47.90** | **$0.57** | |

## Time accounting

- Claude's wall-clock: ≈ 17.5 h (21:55 → ≈ 15:30), unattended except for Jacob's decisions.
- Jacob's time: the plan approval, five decisions in chat (the API sub-limit, the rank-32 cap, the HF-branch cap, Amendment D, the re-run), and the two status questions. No labelling pass yet.
- Idle-but-billing: ≈ 6 h of the second pod's 7.5 h were spent on downstream work slowed ≈ 10× by saves; ≈ 0.5 h of pod 1 waited on laptop-side decisions (HF admission, induction).

## Changes for the re-run (option 2) and for the repo

1. **Saves:** resume points only every 100 steps (none at ordinary saves); the distance computed on the GPU from in-memory tensors; adapter files written once; per-phase timings logged in every `ckpt.json`.
2. **Benchmark and projection:** the smoke bench times a full save and a held-out pass; the projection prices saves and passes per run; the smoke step refuses if a save exceeds ≈ 30 s or disk throughput is below a floor.
3. **Monitoring:** an ETA watcher that wakes on a 1.5× overrun of any step's projection, plus a scheduled check-in every 30 minutes while a pod runs.
4. **Retention:** upload each finished run's checkpoints to the private HF repo at once (incremental `upload` after every training step), so a stop loses at most one run.
5. **Process:** pilot every untested gate first with minimal code; run a ledger dry run after every cap change.
