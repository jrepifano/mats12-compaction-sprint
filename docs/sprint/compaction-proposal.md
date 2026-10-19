# Compaction sprint proposal (DM to Neel and J)

2026-10-18, revised 2026-10-19 with J Rosser's suggestions · draft for Jacob to send · built from the Claude + astra consensus plan (astra's proposal and reconciliation are in the planning session's task outputs, not committed) · review: [logs/reviews/20261018-compaction-proposal-r1.md](../../logs/reviews/20261018-compaction-proposal-r1.md)

The DM is the block below (about 690 words; about 450 before J's additions). The sections after it are the backing for each number and the choices Jacob still has to make; they are not part of the message.

---

## The DM

**Compaction sprint proposal (19–30 Oct)**, for Neel and J

**TL;DR:** when real coding agents compact their histories, which changes to constraints, evidence and authority change what they do next, and can a cheap check on the summary stop the harmful ones? Target result (Neel's): "compaction often lost X; rerunning with and without X, Y happened." Closing the loop is J's suggestion from our call today.

**Asks for Neel**
- A top-up of about $610–860?
- Cath Ge-Wang (AISI) proposes testing how many compactions misalignment survives (≈ our Q5). Contact her?
- What would you cut or add?

**Questions**
1. Which actionable facts disappear from the *whole* resumed state, and how often? (AGENTS.md is re-attached and files re-readable, so absent ≠ lost.)
2. Does restoring a naturally lost fact reduce violations of the task's own constraints, vs restoring a matched irrelevant fact?
3. Do summaries introduce permission the source never granted, does correcting it change actions, and does a check on the summary prevent it? (The prevention part is J's suggestion.)
4. Do the effects hold on a stronger model, and are they plausible in my own Opus sessions?
5. If 1–4 hold: does repeated compaction add drift beyond ordinary resampling ("10 models or one")?

**Data**
- **My own sessions:** 48 real Claude Code compactions across 30 sessions, at 205k–972k tokens. Audit what each dropped or mutated (omissions, uncertainty → fact, invented evidence, permission, task state); ~100 labels hand-checked. Pinned (AGENTS.md) vs chat-only rules is observational. Opus next-action samples at reconstructable boundaries, no tools run: a plausibility check, not replication.
- **Fresh:** ~60 long coding tasks on real repos, constraints in chat, compaction at a configured ~256k, no padding. Counterfactuals fork from the same file and tool snapshot. A subset randomizes one rule's placement (pinned vs chat).
- **Compaction styles (J's suggestion):** the pilot compares a full summary (Claude Code, Codex) with the summary-plus-last-~10-steps style J described for Antigravity, same snapshot and model. A package comparison (the tail adds context); retained tokens reported. If feasible, fresh tasks use both.
- **At each compaction:** full history / actual summary / summary + lost fact / summary + irrelevant fact matched in length, position and style; two continuations each; interventions fixed before outcomes.
- **Q3:** up to 40 long maintenance trajectories with mocked ops tools; edit only the permission content the summary introduced (correct it, or delete it vs matched unrelated text). Then a fix (J's goal, our design): a checker, frozen before bulk outcomes, corrects each summary's permission and constraint claims against the source. It runs on every summary; checked vs unchanged summaries from the same snapshots, on violations and completion.

**Outcomes:** violations of the task's own constraints (primary); irreversible ops actions, impossible-test cheating, beliefs (asked in forks), useful completion (so stalling doesn't look safe). Task-clustered bootstrap; 60 tasks detect only large effects.

**Prior signal** (earlier probes, open models, exploratory): DeepSeek's own summaries hardened inferred permission ("the change owner's absence does not block the decision"), and compaction raised its unapproved irreversible actions 21 points [+6, +35] despite the rule in the system prompt. Deleting those sentences: −23; inserting one licence line: +15. GLM didn't move. After ~200k tokens of own work, product compactions were indistinguishable from no compaction (±40 points).

**Budget** (API only, uncached): core $650 fits the ~$700 left (pilot $50, Q1 $300, Q3 $150, reserve $150); the style comparison fits inside Q1's allocation, and the Q3 checker provisionally fits Q3's, subject to pilot costs. The pilot runs first, measuring how many tasks reach 256k and how often facts are lost. With a top-up: 6 stronger-model tasks ($309), Opus checks ($200), contingency ($150): $1,309; Q5 adds $250.

---

## Backing for each number

| Claim in the DM | Source |
|---|---|
| 48 compactions, 30 sessions, 205k–972k tokens | `uv run python docs/sprint/scan_compactions.py` on 2026-10-18: 48 in 30 sessions; per session 1 (19), 2 (7), 3 (3), 6 (1); 47 auto, 1 manual; pre-compaction tokens min 205,152, median 466,578, max 972,033; 30 Sep – 16 Oct. Assistant messages in those sessions: claude-opus-5-5 30,908, claude-opus-5 2,551, claude-opus-4-8 555. A 972k pre-compaction context implies the 1M window. |
| AGENTS.md is re-attached | [`papers/agent_swarms/INDEX_a.md`](../../papers/agent_swarms/INDEX_a.md) row 60 (Claude Code docs, "what survives compaction"): CLAUDE.md survives. This repo has no CLAUDE.md, so Claude Code loads AGENTS.md in that slot (AGENTS.md, "Agents and tools"). Whether it is actually re-attached at each of the 48 boundaries is checked in the audit, not assumed. |
| DeepSeek: inferred permission hardened; compaction +21 [+6, +35]; deletion −23; licence line +15; GLM flat | [P38 note](../../probes/P38-compaction-live-fact/note.md), follow-up 2 (exploratory, seen not planned): S3 − FULL +20.7 [+6.1, +35.4] (35/82 vs 18/82) with the owner-approval rule in the system prompt throughout; quote from deploy-clean-v1 (found by reading a pair, not drawn at random); deleting permission-and-plan sentences −23, inserting one licence line +15; GLM S3 − FULL +0.0 [−4.2, +4.2]. Text interventions, not a mediation test. Models: DeepSeek V4 Flash, GLM 5.3 Flash. |
| Product compactions indistinguishable from no compaction, ±40 | [P49 note](../../probes/P49-long-context-compaction/note.md), bulk: Codex/pi/Inspect − R all +0.0 with CIs about ±40 (R unresolved in 5 of 16 pairs). Qwen3-Coder on X3's impossible tasks. The repeated-compaction lead (+12.5 points per compaction [0, +25]) is left out of the DM: it mixes more work with more compactions and shrinks under the 30 s scoring rule. |
| Per-unit costs and envelopes | OpenRouter `/api/v1/models` on 2026-10-18: Haiku 5.5 and GPT-6 luna $0.10/$0.50 per M (cache read $0.01); Sonnet 5.5 and GPT-6.1 Sol $2/$10; Opus 5.5 $4/$20. Per-provider endpoints the same day: the base rates are on Anthropic and the major clouds; some regional endpoints are +10%, and one Anthropic Opus endpoint is $8/$40, so pin a base-rate endpoint. Arithmetic below. |
| ~$700 left | `scripts/merge_ledger.py report` on 2026-10-18: $1,401.88 of $2,100 spent across all checkouts, so $698.12 left. |
| Ge-Wang proposes testing compaction survival | [`INDEX_a.md`](../../papers/agent_swarms/INDEX_a.md) row 14: compaction handoffs are one of six spread vectors; her agenda includes how many compactions misalignment survives. An agenda, not a confirmed evaluation. |

**Cost arithmetic** (cheap tier, $0.10 in / $0.50 out per M, uncached). Token assumptions are planning guesses, measured in the pilot:
- fresh trajectory to 256k (80 calls averaging 128k input; 40k output in all): 80 × 0.128 × $0.10 + 0.040 × $0.50 = $1.044;
- summary (≈ 256k in, ≈ 2k out): $0.0266;
- 20-call continuation (per call ≈ 280k in with full history or ≈ 30k compacted, ≈ 250 out): 20 × (0.28 × $0.10 + 0.00025 × $0.50) = $0.5625 full; 20 × (0.03 × $0.10 + 0.00025 × $0.50) = $0.0625 compacted;
- per task: 2 full + 6 compacted continuations = 2 × $0.5625 + 6 × $0.0625 = $1.50; plus generation and summary, $2.57;
- Q1 direct cost: 60 × $2.57 ≈ $154; $300 allocated, covering judging, failed and early-finishing tasks;
- one stronger-model task (20× the cheap rate): 20 × $2.57 = $51.4; six ≈ $309;
- Q3 ($150) and Opus checks ($200) are allocations, not bottom-up estimates: Q3 is up to 40 trajectories × 4 arms × 3 resamples on the cheap tier; the Opus checks are next-action samples at reconstructable boundaries (at $4/$20, ≈ 500k input per sample is ≈ $2, so ≈ 100 samples). Both are sized in the pilot.

**Top-up ask:** $1,309 − $698.12 = $610.88; with Q5, $1,559 − $698.12 = $860.88. Hence "about $610–860".

**Regime note.** Fresh tasks compact at a configured ~256k; Jacob's own sessions compacted at 205k–972k (median ~467k) under a 1M window. The DM doesn't call 256k a native limit, and results at 256k are not assumed to carry to larger contexts; the Opus sessions are a plausibility check, not a replication.

**Prior work and the gap** (all in `papers/agent_swarms/INDEX_a.md`):
- Product-compaction measurements: The Compaction Cliff (row 47: Claude Code's /compact kept 53% of safety rules after one round, 10% after five) and compaction-stress-lab (row 101, README only: 19–20% of exact tool-printed values kept on 12 real SWE-agent sessions; 7 of 72 post-compaction rollout plans used a stale value; live SWE-bench fix rates unchanged but +56% steps under harsh compaction).
- Causal results with planted constraints in short or synthetic contexts: Lost in Context Compaction v2 (row 37), Governance Decay (row 36), Missing Boundary (row 46).
- Single cases: Compaction as Epistemic Failure (row 45: a killed process's partial output became "completed with results"); UK AISI INC-2026-07-28-01 (row 35: a summary may carry a false assumption forward as established fact).
- **Gap:** among the work checked, we have not found matched item-restoration reruns of naturally occurring losses from product compactions on real work.

Notes:
- **Gemini runs need a separate key or Google's own API.** The core plan stays on Haiku 5.5 / luna and DeepSeek.
- **Style-comparison cost:** about one more summary and six more compacted continuations per task. Each continuation call takes ≈ 90k input in total (≈ 30k summary plus ≈ 60k kept tail, a planning guess): 6 × 20 × (0.09 × $0.10 + 0.00025 × $0.50) + $0.0266 = $1.12 per task, $67.30 for 60 tasks. Q1's direct cost rises from ≈ $154 to ≈ $222, leaving ≈ $78 of its $300 for judging and failed tasks.
- **Q3 checker cost:** arms are the original permission text, deleting it, a source-faithful correction, deleting matched unrelated text, and now the checked summary: 5 arms × 3 resamples on up to 40 trajectories. The new arm adds 40 × 3 × $0.0625 = $7.50 and the checks ≈ 40 × $0.03 = $1.20. Q3's $150 is an allocation, not a bottom-up estimate, so this fits only provisionally: if the pilot's projected Q3 cost exceeds $150, resamples drop from 3 to 2 before trajectories are cut.
- **Stronger-model replication** ($309) covers the original design only, not the styles or the checker.

**Review.** One gpt-6-astra round ([r1](../../logs/reviews/20261018-compaction-proposal-r1.md), verdict SEND AFTER FIXES). All four BLOCKING findings were fixed: the Opus session samples are now a plausibility check; fresh counterfactuals fork from a matched snapshot; pinned vs chat-only is observational, with a randomized-placement subset; the prior-signal sentence now cites P38's permission result with its caveats and drops "no compaction differed"; the pilot runs first and measures yield. MINOR fixes: the cost arithmetic, top-up range, true median, the Ge-Wang and gap wording, and matching the irrelevant fact in position and style. Left as is: the reviewer's suggestion to cut to fewer questions (Jacob's call; the five follow the consensus plan).

**Review round 2** ([r2](../../logs/reviews/20261019-compaction-proposal-r2.md), of the J changes only; verdict SEND AFTER FIXES). The BLOCKING finding, an underspecified Q3 checker, is fixed: the checker is frozen before bulk outcomes, runs on every summary, and is compared with unchanged summaries from the same snapshots on violations and completion. MINOR fixes:
- the style comparison is labelled a package comparison, and expanding it depends on feasibility, not on a favourable effect;
- the revised cost arithmetic (Q1 ≈ $222 direct; Q3 fits provisionally; replication covers the original design only);
- Antigravity and supervision claims attributed to J, not stated as fact;
- J's suggestions are separated from our implementation choices;
- the unread debate sources no longer get substantive summaries;
- the model-swap bullet is moved out of the DM.

The DM is still about 690 words against the 450 target; the Choices table says what to cut first.

## Choices still Jacob's before sending

The DM is written with the recommended option for each. Change the marked line if you choose otherwise.

| Decision | DM assumes | Line to change otherwise |
|---|---|---|
| Opus next-action checks on your sessions (~$200) | Yes, in the top-up envelope | Q4, the first Data bullet and the Budget line |
| Primary cheap model and replication model | Not named; Haiku 5.5 recommended (closer to Claude Code and to your corpus), Sonnet 5.5 to replicate | Add to Data if you want them named |
| Q2 / "10 models or one" | Kept as conditional Q5 | Q5 and the last clause of Budget |
| Top-up | $610–860 | Asks |
| Pilot (~$50: 6 corpus boundaries + 4 fresh tasks) | Runs first (Budget line); can start before or after sending | Budget line |
| Contact Ge-Wang / AISI | Asked of Neel | Asks |
| Number of questions | Five (consensus plan); the reviewer suggested fewer | Questions |
| Length | About 690 words after J's additions (450 before); to shorten, cut Q5's J note, then move Questions for J into a separate message to J | DM |
| Compaction-style comparison (J) | In the pilot; in the fresh tasks if feasible there | Compaction styles bullet |
| Model swap mid-trajectory (J) | Deferred; not in the DM | Add a "Later" line to Data if you want it mentioned |
| Q3 fix: source check vs debate (J) | Source check committed; debate only if J recommends it | Q3 Data bullet, Questions for J (1) |
