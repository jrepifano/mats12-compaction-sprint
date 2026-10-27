# Compute headroom for the week-2 runs

27 Oct 2026, 15:50–15:55 UTC. Spend: $0.078 (two Together burst checks, ledger `SPRINT-W2-INFRA` in the week2-build checkout). No code was edited and nothing was committed.

## Answer

**Run 16–20 agents on rig.** Rig's hardware is not the limit at 20, and neither is Together's rate limit in a short burst. Each agent spends 88% of its time waiting on model calls. What sets the ceiling is the harness's memory budget for declared container limits (`BUDGET_MB` = 20 GB): at today's 2 GB per agent it allows 10 agents. At 1 GB it allows 20 with no code change. At 1.25 GB it allows 16. **Projected time for 244 runs:** 15–19 h at 20 agents, 18–23 h at 16, against 29 h at 10. **Rented machines would not help much.** Per-stream generation speed sets the run time, not the host. The kept images that chains fork also live on rig's disk.

## 1. Rig headroom (admission check, $0)

The check was `s2_chain.py admission-check --agents N --agent-memory-mb 1024 --load`, run in `~/emx-week2` with `SPRINT_CHAIN_RAW_DIR=~/headroom-r5/run_<N>x1024/raw`, so the builder's state and gates were not touched. 1024 MB is the only declared limit at which 16 and 20 fit the 20 GB budget; at 2048 the check would admit only 10.

| | 16 agents | 20 agents |
|---|---|---|
| Result | PASS, 16/16 | PASS, 20/20 |
| Container peaks (MB) | 7–214 | 9–190 |
| OOM kills, timeouts | 0, 0 | 0, 0 |
| Host MemAvailable, minimum | 26,382 MB | 26,234 MB |
| CPU while all tests ran | load average 2.1 | run queue 34; CPU 82% busy for about 6 s |
| Disk during the check | — | 333 MB read, 1.34 GB written; peak write 198 MB/s |
| `docker ps` afterwards | empty | empty |

- **Caveat on the load:** it is light. The repos' tests take about 5 s, and TypeScript has none. In real runs, tool execution is 7% of wall time (below), so 20 agents average about 1.4 commands running at once.
- **Grading:** a waiting grader (8 GB declared) holds back new agents until it fits. At 20 × 1 GB, 8 agents must leave first. Grading time was not measured, so the effective concurrency during grading is unknown.
- **Memory at 1 GB per agent:** task chaining's agent peaks were 0.03–0.8 GB over 88 runs, a thin margin. An agent that goes over is OOM-killed inside its own container, and the run is recorded as failed. 16 agents at 1280 MB leaves more margin.
- **Raising `BUDGET_MB` instead:** overcommitting was reviewed as BLOCKING before (D28). That is Jacob's call.
- **Disk:** 151 GB is free, plus 244 GB of reclaimable build cache. Each run keeps an image. The 110 kept images from task chaining add a mean of 0.39 GB each (median 0.10, p90 0.98, max 1.95). Expect about 95 GB for 244 runs, and more if runs resemble the p90. Disk use does not depend on concurrency, but watch it.
- **Rig at idle:** it shows about 12% iowait with 4 blocked processes. That predates these checks.

## 2. API headroom

**Runs wait on the API, not on the containers.** Measured over 104 task-chaining runs (20,990 calls in `calls.json`), wall time splits as:

| Part of wall time | Share |
|---|---|
| Model latency | 88% |
| Tool execution | 7% |
| Scorer snapshots | 1% |
| Other | 4% |

- Per-call latency: median 3.3 s, p90 47 s, mostly the reasoning tokens.
- Per call: about 5,000 completion tokens, generated at a median of 260 tok/s.

**Burst checks** (`provider_bench.py`, Together pinned, 30k-token prompts; raw rows are in `r5_provider_bench_par{20,40}.jsonl`):

| Parallel calls | Errors / 429s | Median TTFT | Per-stream tok/s | Aggregate tok/s |
|---|---|---|---|---|
| 20 | 0 | 0.42 s | 190 (range 167–214) | 2,785 |
| 40 | 0 | 0.66 s | 203 (range 174–244) | 5,468 |

- Aggregate throughput doubled from 20 to 40, and there were no 429s.
- Per-stream speed is about 20–25% below the 26 Oct 8-parallel bench (235–266) and the task-chaining median (260). With 88% of run time in model calls, that would stretch a 72-minute run to about 91 minutes. The hour ranges above include this slowdown.
- **Limits:** each burst lasted about 4 s. A sustained rate window, and contexts of about 175k tokens, were not tested. Watch for 429s and per-call latency in the first hour.

## 3. Laptop

The cap from memory (Jacob, 24 Oct) is at most 3 containers, ≤ 12 GB of limits and ≤ 8 CPUs, launched only with ≥ 30% free memory and load < 12. Each batch stays on one host. The amd64 task images run emulated on the laptop, so timings differ. That adds at most 3 agents, as a separate batch. Nothing was started on the laptop.

## Recommendation

- **Configs:** set `parallelism.agents = 16` with `agent_memory_mb = 1280` (safer), or 20 with 1024 (fastest). Keep 10 × 2048 as the fallback.
- **Gate:** first rerun `admission-check --load` at that exact setting in `~/emx-week2`'s own raw dir. It costs $0 and takes about 90 s. The gates recorded here sit in `~/headroom-r5` and do not authorise a launch.
- **Beyond 20:** going higher needs a decision on the memory budget, then a sustained check on Together, before more machines would help.
