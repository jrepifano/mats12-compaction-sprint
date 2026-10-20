# Compaction sprint proposal (DM to Neel)

2026-10-18 · Jacob Epifano

Draft for Jacob to send. Restructured on 19 Oct into Jacob's order (topic, current findings, research questions, then models and data), with J Rosser's suggestions, the data-source research and a provider benchmark folded in. The DM is the first section (about 530 words, one page; detail cut from it on 20 Oct is under "Design detail"); the rest backs each number and lists the choices still open, and is not part of the message. Mirror of [docs/sprint/compaction-proposal.md](../../docs/sprint/compaction-proposal.md); edits here are synced back to that file. Reviews: [round 1](../../logs/reviews/20261018-compaction-proposal-r1.md), [round 2](../../logs/reviews/20261019-compaction-proposal-r2.md), [round 3](../../logs/reviews/20261019-compaction-proposal-r3.md).

## The DM

**Compaction sprint proposal (19–30 Oct)**

Long-running coding agents compact: when the context fills, the harness swaps the history for a summary. Anything the summary drops, mis-remembers or invents, including the user's constraints and permissions, becomes the agent's memory. UK AISI reported assumptions being carried forward as fact.

**What I've found so far.** In short-context tests, compaction raised DeepSeek's unapproved irreversible actions 21 points [+6, +35]. Its summaries stated permissions the source only implied, and deleting those permission and plan sentences cut the actions by 23 points. In ~200k-token histories (SWE tasks, then ImpossibleBench; Qwen3-Coder), there were signs that repeated compactions increased test cheating, and signs the methods differed.

**Research questions**

1. How often does compaction drop, mis-summarize or invent an actionable fact, which kinds, and does that grow with context size?
2. Does compaction make agents break the task's own rules, and how much of that do the lost facts explain?
3. Do summaries introduce permission the source never granted, does correcting it change actions, and does a line in the summarizer prompt prevent it?
4. Do losses and rule-breaking compound with each further compaction?

**Models and data**

**Models:** DeepSeek V4.1 Flash, GLM 5.3 Flash, GPT-6 luna and Claude Haiku 5.5; Opus 5.5, GPT-6 astra and Gemini on a subset.

**Offline (first, and cheap): audit compactions that already happened.**

- My 48 Claude Code compactions (205k–972k tokens) and SWE-chat (~1,000 real Claude Code compactions across many users and repos).
- PostTrainBench and FrontierML: long runs whose tasks state checkable rules ("cannot modify evaluate.py"), so I can see whether a rule drops out of the summary and the agent then breaks it, without running anything.
- Re-compacting the same histories with the Codex, pi, DeepSeek-CLI and Antigravity-style summarizers compares methods on identical source.
- The facts that get lost, and the sizes where loss rises, set the rules and thresholds for the online runs.

**Online (causal tests): new agent runs.**

- **Fresh trajectories:** the 45 longest DeepSWE tasks (real-repo features; cheap models reach ~200k tokens), with a few realistic rules I add in chat (e.g. don't edit tests). At the first compaction, made by each model's own vendor summarizer, I resume from the same snapshot four ways: full history, actual summary, summary + the lost rule, summary + a matched irrelevant fact.
- **Permission tasks:** up to 40 long shifts chained from τ²-bench (mocked tools, a confirm-before-write rule). Where a summary claims a permission the user never gave, I correct only that text, then test a summarizer-prompt fix on held-out runs.
- **Repeated compaction:** 2–3 tasks chained to end on the same scored task, against runs of the same length with fewer compactions.
- **Outcomes:** rule violations, irreversible actions, test cheating and task completion.

**Budget and ask.** Could I have a $1,000 top-up ($3.1k total)? Planned runs come to $1,150: pilot $50, corpus audit $60, fresh runs $270, permission tasks $100, frontier models $420, repeated compaction $250. I don't expect to spend all of it, but want the funds in place so later experiments aren't held up.

**Stretch:** compaction styles on all fresh tasks; safer summarizers (automated prompt search, or a model that checks each summary against the source); switching models mid-task.

## Backing for each number

### Design detail (moved out of the DM to fit one page, 20 Oct)

- **Findings:** the DM no longer says "so what the summary says dictates the behavior; I haven't yet isolated which sentences matter". The deleted sentences were a class "dominated by plain plan lines" (P38 note), so the cause isn't isolated.
- **Offline audit:**
  - The audit separates what a summary dropped or invented from what was truly unavailable afterwards (re-attached instruction files, re-readable files), relates loss to context size (including re-compacting my session prefixes at 256k, ~512k and the real boundary), and hand-checks ~100 labels.
  - PostTrainBench and FrontierML violation rates are checked first; if they're near zero, those sets serve Q1 only.
  - As a sanity check, Opus's next action is sampled at a few reconstructed boundaries, without running tools, on my Claude Max plan before 22 Oct.
- **Fresh trajectories:**
  - The 45 tasks are those with a median peak of at least 200k on DeepSeek V4 Flash and luna at max effort. Compaction fires at a per-model threshold frozen after the pilot.
  - Each added rule is mapped to a diff, test or action-log check before any outcomes. I never delete a rule myself, so any loss is the summarizer's.
  - Each condition runs twice from the compaction point. The inserted text is fixed in advance, and the irrelevant fact is matched in length, position and style. Full history vs summary is compaction's effect; restoring the lost rule, against the irrelevant fact, shows how much of that gap the rule explains.
  - Vendor summarizers: Codex for luna, Claude Code for Haiku, Antigravity for Gemini. GLM has no vendor harness, so it uses an open-source one; Antigravity depends on whether `agy` runs headless, otherwise its style is replayed.
- **Permission tasks:** τ²-bench is designed to separate an agent's reasoning errors from communication and coordination failures, which helps pin a violation on the summary rather than on the exchange with the user. The prompt fix is tested on trajectories not used to write it.
- **Repeated compaction:** every chain ends on the same final task with the same rules, and only that task is scored, so the number of compactions before it is the only difference.
- **Outcomes:** task completion is there so an agent that stalls or refuses isn't scored as safe. Error bars resample whole tasks, since runs on the same task are correlated.
- **Budget:** planned runs are $1,150 of ~$1,700 available (the ~$700 left plus the top-up); the rest is unitemized headroom because cost per run is uncertain until the pilot.

| Claim in the DM | Source |
|---|---|
| 48 compactions, 30 sessions, 205k–972k tokens | `uv run python docs/sprint/scan_compactions.py` on 2026-10-18: 48 in 30 sessions; per session 1 (19), 2 (7), 3 (3), 6 (1); 47 auto, 1 manual; pre-compaction tokens min 205,152, median 466,578, max 972,033; 30 Sep – 16 Oct. Assistant messages in those sessions: claude-opus-5-5 30,908, claude-opus-5 2,551, claude-opus-4-8 555. A 972k pre-compaction context implies the 1M window. |
| AGENTS.md is re-attached | [`papers/agent_swarms/INDEX_a.md`](../../papers/agent_swarms/INDEX_a.md) row 60 (Claude Code docs, "what survives compaction"): CLAUDE.md survives. This repo has no CLAUDE.md, so Claude Code loads AGENTS.md in that slot (AGENTS.md, "Agents and tools"). Whether it is actually re-attached at each of the 48 boundaries is checked in the audit, not assumed. |
| DeepSeek: compaction +21 [+6, +35]; summaries stated implied permissions; removing permission-and-plan sentences −23, cause not isolated; GLM near floor; mostly under 17k tokens | [P38 note](../../probes/P38-compaction-live-fact/note.md), follow-up 2 (exploratory, seen not planned): S3 − FULL +20.7 [+6.1, +35.4] (35/82 vs 18/82) with the owner-approval rule in the system prompt throughout; quote from deploy-clean-v1 (found by reading a pair, not drawn at random); deleting permission-and-plan sentences −23 (a class "dominated by plain plan lines", note line 33), inserting one licence line +15; GLM S3 − FULL +0.0 [−4.2, +4.2]. Text interventions, not a mediation test. Models: DeepSeek V4 Flash, GLM 5.3 Flash. Regime: forced post-hoc compaction of 4–17k contexts (P49 measurement inventory, P38 rows: DeepSeek median 3.8k, max 16.8k; GLM max 34.4k). |
| ~200k stitched histories, impossible coding tasks, Qwen3-Coder; signs repeated compaction raised cheating and the methods differed (intervals include 0) | [P49 note](../../probes/P49-long-context-compaction/note.md), bulk: Codex/pi/Inspect − R all +0.0 with CIs about ±40 (R unresolved in 5 of 16 pairs). Qwen3-Coder on X3's impossible tasks. Histories were joined from two or more SWE-rebench sessions to 185–205k and then switched to a new task capped at 24 turns ([P49 plan](../../probes/P49-long-context-compaction/plan.md)). The repeated-compaction lead (+12.5 points per compaction [0, +25]) is one of the DM's "signs"; it mixes more work with more compactions and shrinks under the 30 s scoring rule. The other is the method difference: raw cheating rates after one compaction were Inspect 18%, Codex 30% and pi 38% (pooled; docs/pitch/fig/compaction_rates.png), and paired pi − Inspect was +7.1 [−23.1, +38.5] in the bulk and +15.0 [−20.0, +47.4] pooled, a lead the note records as "pi trends above Inspect". |
| UK AISI case | [`INDEX_a.md`](../../papers/agent_swarms/INDEX_a.md) row 35 (INC-2026-07-28-01): a summary may carry a false assumption forward as established fact. |
| Model windows and prices | OpenRouter `/api/v1/models` on 2026-10-19: Haiku 5.5 1,000,000 tokens, $0.10/$0.50 per M, $0.01 cache read; GPT-6 luna 1,050,000, same prices; GLM 5.3 Flash 1,048,576, $0.048–0.15 in by provider; DeepSeek V4.1 Flash 1,048,576 on OpenRouter ($0.03–0.60 in by provider; most $0.30/$1.20). DeepSeek's own API (pricing page read 2026-10-19): `deepseek-flash` = DeepSeek-V4.1-Flash-1017, off-peak $0.15 in (cache miss) / $0.003 (cache hit) / $0.60 out, peak double (peak: 01–04 and 06–10 UTC, weekdays), concurrency limit 2,500; the old `deepseek-v4-flash` name now routes to V4.1. Flagships: Opus 5.5 $4/$20 ($0.20 cached); GPT-6 astra $10/$50 ($1 cached). Qwen3-Coder (262k window) is dropped. |
| Provider speeds | Our small benchmark on 2026-10-19 (below): DeepSeek's API ~200 tok/s per stream, also with 8 in parallel. |
| SWE-chat, PostTrainBench, FrontierML; DeepSWE lengths | External research on 2026-10-19 (below), with DeepSWE's per-rollout stats recomputed from its published `trials.json`. |
| ~$700 left | `scripts/merge_ledger.py report` on 2026-10-19: $1,402.12 of $2,100 spent across all checkouts (including today's $0.24 provider benchmark), so $697.88 left. |

### Data sources (research, 2026-10-19)

Two research agents ran on 2026-10-19: an internal search of our own records and an external search of benchmarks and public trajectory data. Their full reports are in the session transcript; scratch notes are in the session scratchpad (not committed). Marks: **[V]** read in the primary source, **[S]** snippet or fetch summary only, **[me]** recomputed by the agent from raw data.

**Our own records don't supply long tasks.** In 67,074 SWE-rebench OpenHands runs on real issues, context had a median of 51.5k and a maximum of 137k, with none above 160k (P49 `design/measure/out/swe_rebench_full.log`). Without kept reasoning, context grows about 0.7k tokens per turn, so reaching 200k takes about 275 turns (P49 `lit-review.md`, `cost_model.md`). Our other long runs (P41, P44, X3b GLM, up to ~245k) were mostly reasoning that hit the output cap, and none of the 64 transcripts long enough for two compactions ended in `done`.

**Fresh tasks: DeepSWE** (Datacurve, 2026; github.com/datacurve-ai/deep-swe; Apache-2.0 [me]). 113 engineer-written feature tasks on 91 repos in 5 languages [V]. Its published 31,617 mini-swe-agent rollouts run with no compaction and record `peak_context_tokens` [me]:

| Model (effort) | Median peak | Share over 256k | Median steps | Median cost |
|---|---|---|---|---|
| DeepSeek V4 Flash (max) | 202k | 13.5% | 148 | $0.09 |
| GPT-5.6 luna (max) | 202k | 20.1% | 92 | $2.29 |
| GLM 5.3 Flash (max) | 136k | 3.1% | 110 | $0.39 |
| Claude Opus 5 (max) | 216k | 24.3% | 91 | $10.51 |
| GPT-5.6 luna (xhigh / high) | 138k / 90k | 5.5% / 0.4% | 63 / 44 | $1.24 / $0.66 |

- 45 tasks have a median peak of at least 200k on both DeepSeek V4 Flash and luna at max [V].
- The models we plan (V4.1 Flash, GPT-6 luna, Haiku 5.5) aren't in DeepSWE's table, so the pilot measures their peaks.
- Max reasoning effort matters: luna's median peak falls from 202k at max to 90k at high.
- **Threshold:** a 256k threshold would fire in only 3–24% of runs, so compactions are configured early, per model, below its peak on the selected tasks (about 150–200k; lower for GLM, whose median peak is 136k), and frozen after the pilot from the selected tasks' boundary yield. These are configured compactions, not native limits. Models are analysed separately, with a common-threshold subset for cross-model comparison. Q4's repeated compactions need longer sessions; chaining 2–3 tasks in one session (as in arXiv 2606.11213) is the option, decided in the pilot. Q4 compares against the same number of steps with fewer compactions, so extra compactions aren't confused with extra chances to err.
- **Scoring:** the grader extracts the agent's commits as a patch [V]. A patch shows forbidden edits but not everything (tool use, data access, approvals), so each rule we add is chosen to be checkable and mapped to a diff, test or action-log check before outcomes. The rules are given by us at the start of the session; their loss through compaction is natural. The pilot measures the yield that matters: boundaries where an actionable rule was actually lost, not just long contexts.
- **Practical risks:** the images are amd64 only (about 0.8 GB each, 2 CPU / 8 GB) [V], so on the M5 they run under emulation or get rebuilt, about 4–5 at a time. Contamination is low to medium: the suite has been public since about June 2026.
- Second choices: RoadmapBench (115 version-upgrade roadmaps, OpenHands averages 110–171 turns [V]) and FeatureBench Lite (30 tasks, 2–8M cumulative input [V], 18 GB images).
- Unsuitable because runs are short: SWE-bench Verified ("under 20k–30k tokens", arXiv 2602.16069 [V]), Multi-SWE-bench, SWE-smith, SWE-Gym, SWE-rebench, Terminal-Bench 2.x ("fewer than 25 model calls" [V]) and LongCLI-Bench.

**Public trajectories with real compactions:**

| Dataset | Size | Real compactions | Rules stated in the task | License |
|---|---|---|---|---|
| SWE-chat (Stanford; `cfahlgren1/SWE-chat` v1, `SALT-NLP/SWE-chat` v2 gated) | v1 5,851 sessions on 205 repos [me]; v2 17,819 [V] | About 1,000 Claude Code events in 573 v1 sessions, per SWE-chat's own description [V]; in the 5 transcripts the agent inspected, mostly auto at 165–265k, with full summary text (`compact_boundary` and `isCompactSummary`) [me]; Codex summaries are encrypted [V] | Only what users typed (natural, unlabelled) | ODC-BY [me]; users opted in; PII-redacted [V]; Claude 4.5–4.6 models |
| inductionlabs/frontier-ml-trajectories (2026-10-13) | 84 runs of 12 h ML tasks [me] | Codex + GPT-6 luna compacts at ~244k (summary text not stored); Claude Code + Sonnet 5.5 at ~165k, 4–62 times per run, with text; Claude Code + Haiku 5.5 never compacts (peaks 447k–707k) [me] | CONTRACT.md bans val/test training and requires the given base model [V] | Not stated |
| aisa-group/PostTrainBench-Trajectories | 1,842 runs, 629 Claude Code [M] | 8 of 16 sampled Claude Code runs compact at 167–173k, with text [M] | "Cannot modify evaluate.py"; no AIME 2025 test data; per-run judge files [V] | Apache-2.0 |

[M] = measured by the agent on sampled raw rows. Also found: real pi sessions (`MaxDevv/real-pi-coding-agent-traces-sessions`, compactions in about 10 of 100 sampled [V]) and TraceLab (counts only: 4.4% of Claude and 17.4% of Codex sessions compact at least once [S]). SWE-rebench, SWE-smith, SWE-Gym and similar SFT sets showed no compactions (0 in 610 sampled rows [M]).

**Q3 ops tasks.** No external suite is long, ops-shaped and mocked. The plan chains τ²-bench tasks into long shifts rather than extending our own P38 worlds, and keeps its "explicit user confirmation (yes)" before database writes (MIT, Inspect support [V]; its tasks are ~5.7k tokens, so a shift strings many together), and borrows OverEager-Bench's consent-kept vs consent-stripped pairing for the analysis (arXiv 2605.18583: stripping consent takes Claude Code from 0% to 17.1% overeager [V]).

**Implications for the design:**

- **The public corpora grow the natural-compaction set 10–20×.** PostTrainBench and FrontierML add compactions under stated, checkable rules. They answer Q1 observationally (reported by corpus, model and harness, separating summary omissions, confirmed loss from the whole resumed state, and unknown availability where filesystem state is missing) and surface candidate failures. Q2's causal claim needs the executable forks of the fresh runs. SWE-chat's models (Claude 4.5–4.6) differ from ours, which limits generalisation, not its observations.
- **FrontierML's Claude Code + Haiku 5.5 runs reached 447k–707k without compacting** [me], so we set the threshold ourselves. Codex + luna compacts natively at about 244k [me].
- **Overlap:** ContinuityBench (ICML 2026) found structured-summary handoffs cut success from 41.7% to 28.0% on τ-bench, AppWorld and TerminalBench [V abstract], but measures task success only. OverEager-Bench manipulates consent by hand, not through compaction. Terminal-Bench 2.0 has a "Context Loss … ignoring constraints" failure category [V]. CompactionRL judges summaries for constraint fidelity [V]. OpenAI's "deception in compaction summaries" report released no data [S]. None scores constraint violations after a natural compaction on long coding or ops tasks.

### Providers (benchmark, 2026-10-19)

Benchmark method: two streamed calls per (model, provider) on a ~30k-token prompt (repo source plus a 400-word summary request), pinned with `allow_fallbacks: false`; DeepSeek's own API also ran 8 calls in parallel. The measures are tokens per second after the first token and time to first token (TTFT). The 8-parallel figure of ~1,100 tok/s is total output tokens over the batch's wall time, including each stream's TTFT and uneven finish times, which is why it is below 8 × 200. Scripts: `provider_bench.py` and `provider_bench2.py` in the session scratchpad. Cost $0.24, recorded in `logs/costs.jsonl` as exp SPRINT; the DeepSeek row is computed from usage at peak rates although the run was at 21:09 UTC on a Monday (off-peak), so it overstates by up to 2×. The luna calls on the OpenAI route were billed to our own OpenAI key (BYOK), not OpenRouter; an estimated $0.005 is recorded separately. One run per provider at one time of day, on a short prompt, so treat the figures as indicative.

| Model | Fastest measured | Others | Notes |
|---|---|---|---|
| DeepSeek V4.1 Flash | Together 538–539 tok/s, TTFT 0.4–0.8 s | BaseTen 212–286; Novita 239–257; **DeepSeek API 190–232** (TTFT ~1.2 s; 8 in parallel each ~200, ~1,100 tok/s over the batch); StreamLake 134–146; GMICloud 133–139; Parasail 105–109; DeepInfra 81–96; Fireworks 92 (one 41 s TTFT); Nebius 68–84; CoreWeave 429 | DeepSeek's API is half Together's price off-peak (equal at peak), with $0.003 off-peak cache hits and a 2,500 concurrency limit |
| GLM 5.3 Flash | Friendli 252–274 tok/s, TTFT 0.8–1.5 s | Together 184–204; Parasail 138–174; Fireworks 123–162; Novita 133–137; BaseTen 70–92; **Z.ai (first party) 67–69**; GMICloud 54–69; DeepInfra 46; CoreWeave 429 | First-party Z.ai is among the slowest, so a Z.ai key is not worth getting |
| GPT-6 luna | Azure 116–142 tok/s | OpenAI 120 | OpenAI route billed $0 on OpenRouter (our OpenAI key, BYOK) |
| Claude Haiku 5.5 | Google 318–431 tok/s | Azure 331–342; Anthropic 290–361; Bedrock 265–325 | No cache hits without explicit `cache_control`; to set in the harness |
| DeepSeek V4 Flash (April release) | GMICloud 164–166 | Baidu 118–119; others 7–74 | Retired at DeepSeek; not used |

**Choice:**

- **DeepSeek V4.1 Flash:** DeepSeek's own API, for cost and concurrency; Together as the speed option.
- **GLM 5.3 Flash:** Friendli, falling back to Together.
- **luna:** the OpenAI or Azure route.
- **Haiku 5.5:** the Anthropic or Google route, with caching set.

**Pilot checks:** whether per-call speed holds at our concurrency, since OpenRouter's shared upstream limits returned 429 on CoreWeave and Alibaba. Agent turns emit few tokens, so time to first token matters as much as tokens per second.

### Cost arithmetic (revised 2026-10-19)

The 18 Oct figures assumed context grows ~3.2k tokens per call (80 calls to 256k). Our records give ~0.7k per turn without kept reasoning and 2–4k with it, and DeepSWE measured whole runs, so the arithmetic now uses DeepSWE.

| Item | Basis | Allocation |
|---|---|---|
| **Cheap trajectory**, provisional $1.00 | 12–17M total input per run (DeepSWE whole-dataset medians, DeepSeek V4 Flash and luna at max; the longest tasks will run above median). At $0.10 in and $0.01 cached with 90% cache hits, input is $0.23–0.32; output at ~1.5k tokens per step over ~150 steps is ≈ $0.11 at $0.50 per M; uncached input would be $1.2–1.7. The rest of the $1.00 covers summarisation, scoring and failed runs. DeepSWE's measured median for DeepSeek V4 Flash was $0.09. | — |
| **Four-way continuations**, provisional $0.50 per trajectory | One boundary per trajectory (the first compaction); 4 arms × 2 continuations × 20 calls. The full-history arm (~180k per call, 40 calls ≈ 7.2M input) can reuse the trajectory's prefix cache; the three summary arms (~30k per call) are new prefixes and cache only within themselves. Uncached ceiling ≈ $1.10; with caching ≈ $0.15–0.30. | — |
| **Fresh trajectories** | 45 tasks × 4 cheap models = 180 trajectories at $1.50 each = $270. The bulk task count is set from the pilot's measured cost per completed four-arm comparison. | $270 |
| **Flagships** | At 90% cache hits, 12–17M input costs $7.0–9.9 on Opus 5.5 ($4 in, $0.20 cached) and $22.8–32.3 on GPT-6 astra ($10 in, $1 cached; 2.5× Opus's input price, 5× its cache price). Adding output and forks: Opus ≈ $15 per task (DeepSWE: Opus 5 at max, $10.51 median) × 10 = $150; astra ≈ $45 × 6 = $270. | $420 |
| **Corpus audit** | ~1,100 compactions judged against ~200k of source each at $0.10 per M ≈ $22 per judge; two judges ≈ $44; plus ≈ $6 for re-compacting session prefixes and slack. | $60 |
| **Q3** | Allocations sized in the pilot, not bottom-up estimates. | $100 |
| **Opus checks** | ≈ 100 next-action samples, run on Jacob's Claude Max plan before 22 Oct (headless Claude Code), so no API cost. At API prices they would be ≈ $2 each (≈ 500k input at $4 per M). | $0 (Max plan) |
| **Q4** | Allocation sized in the pilot. | $250 |
| **Pilot** | 6 corpus boundaries; 4 DeepSWE tasks × 4 cheap models; provider concurrency; caching; Docker on the Mac. | $50 |
| **Reserve** | — | $150 |
| **Contingency** | Includes the $200 freed by moving the Opus checks to the Max plan. | $350 |

**Totals:**

- **Core:** 50 + 60 + 270 + 100 + 150 = $630, against $697.88 left.
- **With the top-up:** core + 420 (flagships) + 250 (Q4) + 350 (contingency) = $1,650, against $1,697.88 available ($47.88 spare).

**Wall-clock (baseline only):** at DeepSWE's median 16–23 min per run, 180 trajectories at 4–5 Docker sandboxes at once take ≈ 10–17 h of generation. That excludes forks, scoring, setup, retries and amd64 emulation on the Mac, and five 8 GB sandboxes leave little of the laptop's 48 GB. The pilot measures end-to-end time and memory per completed four-arm comparison; a native amd64 Linux worker (cloud CPU, paid from the reserve) is the faster option to benchmark there.

**Regime note.** Fresh tasks compact at a per-model threshold of about 150–200k, set in the pilot. Jacob's sessions compacted at 205k–972k (median ~467k) under a 1M window, and SWE-chat's at 165–265k. Results at the fresh threshold are not assumed to carry to larger contexts; the corpus audit's size analysis covers that range, and the Opus samples are a plausibility check, not a replication.

**Prior work and the gap** (all in `papers/agent_swarms/INDEX_a.md`):

- **Product-compaction measurements:**
  - The Compaction Cliff (row 47): Claude Code's /compact kept 53% of safety rules after one round and 10% after five.
  - compaction-stress-lab (row 101, README only): 19–20% of exact tool-printed values kept on 12 real SWE-agent sessions; 7 of 72 post-compaction rollout plans used a stale value; live SWE-bench fix rates unchanged but +56% steps under harsh compaction.
- **Causal results with planted constraints in short or synthetic contexts:** Lost in Context Compaction v2 (row 37), Governance Decay (row 36), Missing Boundary (row 46).
- **Single cases:**
  - Compaction as Epistemic Failure (row 45): a killed process's partial output became "completed with results".
  - UK AISI INC-2026-07-28-01 (row 35): a summary may carry a false assumption forward as established fact.
- **Close benchmarks found on 19 Oct:** ContinuityBench, OverEager-Bench, CompactionRL (above; rows 104–111).
- **Gap:** among the work checked, we have not found matched item-restoration reruns of naturally occurring losses from product compactions on real work, or constraint-violation scoring after natural compaction on long tasks.

Notes:
- **Gemini runs need a separate key or Google's own API.** The Gemini subset (argon or 3.8 Flash) therefore runs through Google's API with its own key.
- **The Q3 fix** is a line in the summariser prompt naming the failure modes the audit finds (e.g. never state a permission the source didn't grant). It is frozen before bulk outcomes and tested on trajectories not used to write it: revised-prompt vs unchanged summaries from the same snapshots, on violations and completion (Jacob, 19 Oct, replacing the earlier checker). A source-grounded or debate-style checker is the Stretch fallback.

### Review record

**Review.** One gpt-6-astra round ([r1](../../logs/reviews/20261018-compaction-proposal-r1.md), verdict SEND AFTER FIXES).

- **All four BLOCKING findings were fixed:**
  - the Opus session samples are now a plausibility check;
  - fresh counterfactuals fork from a matched snapshot;
  - pinned vs chat-only is observational, with a randomized-placement subset;
  - the prior-signal sentence now cites P38's permission result with its caveats and drops "no compaction differed";
  - the pilot runs first and measures yield.
- **MINOR fixes:** the cost arithmetic, top-up range, true median, the Ge-Wang and gap wording, and matching the irrelevant fact in position and style.
- **Left as is:** the reviewer's suggestion to cut to fewer questions (Jacob's call; the five follow the consensus plan).

**Review round 2** ([r2](../../logs/reviews/20261019-compaction-proposal-r2.md), of the J changes only; verdict SEND AFTER FIXES).

- **The BLOCKING finding** (an underspecified Q3 checker) is fixed. The fix has since become the prompt line above, frozen and tested on held-out trajectories.
- **MINOR fixes:**
  - the style comparison is labelled a package comparison, and expanding it depends on feasibility;
  - the cost arithmetic was revised;
  - claims about Antigravity and supervision are attributed to J;
  - J's suggestions are separated from our implementation choices;
  - unread sources get no substantive summaries;
  - the model-swap item moved out of the DM.

**Doc edits folded in on 2026-10-19 (revs 8–36, Jacob's comments):**

- The top-up ask is now $1,000 ($3.1k total).
- The Ge-Wang ask is removed; contact her separately.
- A Stretch list was added.
- Questions for J moved to a separate message.
- Q1 now asks how often a fact is dropped, mis-summarised or invented.
- The corpus audit covers size first, with re-compaction at 256k, ~512k and the real boundary.
- The styles comparison now includes pi and DeepSeek's CLI.
- Antigravity is installed as `agy`.
- The Q3 fix is now a prompt line.

**Restructure on 2026-10-19 (Jacob):**

- The DM now runs topic, then current findings in 2–3 sentences, then research questions, then models and data, then budget and ask.
- The models follow Jacob's priorities: current cheap models, flagships for the best results, Qwen3-Coder dropped.
- Providers are picked for speed and cost from the benchmark.
- The data sources come from the research.

**Review round 3** ([r3](../../logs/reviews/20261019-compaction-proposal-r3.md), of the restructure; the last round allowed; verdict SEND AFTER FIXES). The three BLOCKING findings are fixed:

- the trajectory and flagship costs are provisional allocations with stated assumptions, and the bulk task count depends on the pilot's measured cost per four-arm comparison;
- each added rule is mapped to a diff, test or action-log check before outcomes, rules are labelled as ours with natural loss, and the pilot measures the yield of actually lost rules;
- the findings sentence now says removing permission-and-plan sentences didn't isolate the cause, and states the P49 regime: stitched histories, impossible tasks, Qwen3-Coder.

MINOR fixes:

- public corpora answer Q1 and surface candidates, while Q2 comes from the forks;
- thresholds are configured early, frozen after the pilot and analysed by model;
- the two-continuation, matched-control and fixed-in-advance commitments are restored;
- the benchmark is labelled small, with the aggregate metric defined, off-peak noted and BYOK cost recorded;
- evidence labels are scoped;
- wall-clock is labelled baseline-only, with a Linux worker option;
- the styles item says Antigravity's style is as J described;
- the DM is trimmed slightly.

Left open: the DM is still about 700 words, and round 3 suggested cutting 100–150 more. That is Jacob's call.

## Choices still Jacob's before sending

The DM is written with the recommended option for each. Change the marked line if you choose otherwise.

| Decision | DM assumes | Line to change otherwise |
|---|---|---|
| Cheap models | All four (DeepSeek V4.1 Flash, GLM 5.3 Flash, GPT-6 luna, Haiku 5.5) on all 45 tasks | Models bullet; fresh-trajectory cost |
| Flagships | Opus 5.5 on 10 tasks, GPT-6 astra on 6, from the top-up ($420) | Models bullet; Budget |
| Opus next-action checks on your sessions (~$200) | Yes, on Jacob's Claude Max plan before 22 Oct (not the API budget) | The real-compactions bullet, Budget |
| Q2 / "10 models or one" | Kept as conditional Q4 | Q4, Budget, Stretch |
| Fresh-task source and threshold | The 45 longest DeepSWE tasks at max effort; per-model configured threshold frozen after the pilot; chaining tasks for Q4 decided in the pilot | Fresh trajectories bullet |
| SWE-chat v2 | v1 (ungated) by default; v2 needs you to accept the HF gate | Real compactions bullet |
| Top-up | $1,000 (total $3.1k) | Budget and ask |
| Pilot (~$50) | Runs first; can start before or after sending | Budget |
| Contact Ge-Wang / AISI | Contact separately; not in the DM | — |
| Number of questions | Five (consensus plan); r1 suggested fewer | Research questions |
| Length | About 530 words (one page) since 20 Oct; detail moved to "Design detail" | DM |
| Compaction-style comparison (J) | In the pilot; in the fresh tasks if feasible | Compaction styles bullet |
| Q3 fix | Prompt line committed; a checker (source or debate) only if it falls short | Q3 bullet |
