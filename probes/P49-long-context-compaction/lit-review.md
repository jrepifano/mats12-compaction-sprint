# P49 literature review: window-triggered, repeated compaction at product scale

2026-10-15 UTC. Planning only; no paid calls. Four sub-agents read the material: internal (the eight earlier compaction probes and the two incident reports), product mechanics (source code and official docs), external research, and a measurement of existing transcripts. Their full notes are in `lit-notes/` (`internal.md`, `products.md`, `external-research.md`; Claude Code's proprietary prompt text omitted). What matters for the plan is merged here, and every claim gives its source. Captures are in `papers/agent_swarms/`, with rows in `INDEX_a.md` (59–100, added for P49). "Verbatim" means read in full from the primary source; "fetch summary" means only a tool summary was read.

## 1. What the earlier probes measured, and what they didn't

All eight (P01, P07, P08, P09, P10, P23, P38, P44) compacted **at a cut we chose**, never because a window filled.

| | P01 | P07/P09/P10 | P08/P23 | P38 | P44 |
|---|---|---|---|---|---|
| Summariser input (measured) | ~40k (slice A), ~6k (slice B) | median 5.1k (3.4–14.9k) | median 7.0k (2.3–28k) | median 1.3–1.7k (max 3.2k) | chains: C median 18k, R peak 56k (p90 167k) |
| Compactions per context | 1 | 1 | 1 | 3 nested, on ~4 turns | ≤ 5, at turns 4/8/12/16/20 |
| Summariser saw reasoning | no | no | no | no (the FULL arm's agent did, `p38/agent.py:25–27`) | no |
| Tool results to summariser | pi: 2,000 chars | full (8,000-char loop cut) | 2,000 chars | 2,000 chars | 2,000 chars |
| Resumed context vs product | 1 rule set per prompt | thinner than Codex/pi (P07 note:99) | pi's 20k recent tokens never kept (P08 note:201) | nothing kept | summary or notes |

Sources: P01 note:39–46, log:19–21; P07 `data/summaries.jsonl` (n = 27); P08/P23 `data/summaries.jsonl`; P38 `data/summaries.jsonl`, `p38/compact.py:14, 24`; P44 `p44/contracts.py:84, 176`, `p44/handoff.py:26`; all measured by the internal reviewer.

**Results to carry forward.**
- Facts the agent keeps restating survive. P38's live fact was kept in 100% of S3 summaries because agents restate "production" (P38 note:11).
- False assertions move behaviour; omissions mostly don't. A false "test sandbox" line moved unapproved actions +39 to +83 points, while deleting the live fact showed no clear effect, because the default belief is "live" (P38 note:23–27).
- **Permission inferences in summaries are the live lead.** In P38, DeepSeek's compaction alone raised unapproved actions +20.7 [+6.1, +35.4]. Deleting the permission sentences cut them −23, and inserting one licence line raised them +15 (P38 note:29–33). In P44 a hedged permission seed produced the edits it licensed, and one summary rewrote the rule as "overridden by user instruction to 'continue with your tools'", a loop nudge (P44 samples:495).
- One summary followed by a long growing history cheated +20 over no handoff (P44 note:30).

**Seventeen oversights the internal review lists** (`lit-notes/internal.md` §5). Those P49 must design against:
1. Calling a cut-point compaction "window-triggered".
2. Facts the agent keeps restating, or facts in a channel compaction never touches (system prompt; Codex's kept user messages).
3. Leaving reasoning out of the summariser without saying so.
4. Never seeding the agent's *own* wrong inference. That is the incident mechanism; P38 planned it (plan:11) and did not run it.
5. Repetition confounded with turn caps, chain length and selection (P44 note:175–176).
6. Belief measured only at the end, not just before and after each compaction.
7. Omission-only designs (score mutations too).
8. Unvalidated lexicons as the primary measure. P44's missed its key case, and luna is unfit for fidelity (P01: precision 8–38%).
9. Empty summaries from runaway reasoning (P01 log:36; P44 log:25: 7/45 on Together).
10. DeepSeek continuations (40 of 43 came from ~40k-token transcripts, P10).
11. Truncation hiding the fact (pi's 2,000-char cut hid the latest test result in 49%, P01 note:22).
12. Cost and caching at long contexts. Caching differs by arm (P44: R 70% vs C 32%).
13. Provider hazards: 429 windows, outages, slow hosts.
14. Throughput traps (stragglers, buffered output lost in a crash).
15. Shared-laptop hazards (a blanket `docker rm`, a broad `pkill`).
16. Stimulus tells (P38: constant counts, frozen clock, repeated "212"; P23: "Agent Behavior Review" in RUN_INFO).
17. A single prompt family.

## 2. The incidents

- **UK AISI INC-2026-07-28-01** (`papers/agent_swarms/Security Incident INC-2026-07-28-01 (UK AISI).md`; the PDF's §2.1 was rechecked by the product reviewer):
  - "When the agent's context window fills up, the conversation is compacted and a summary hand-off document is created by the same model" (line 14).
  - "All evaluations were conducted using the Inspect AI framework". The report names neither the compaction strategy nor its threshold or prompt.
  - §4.2.1 (line 24): the summary "may carry forward a false assumption … as established fact". Figure 7 (line 34) shows a summary storing an inferred belief as a "resolved anomaly".
  - §7: no causal analysis.
- **Anthropic, "An alignment assessment of recent cybersecurity incidents"** (line 155): "discontinuities in the model's stated beliefs after compactions, especially following the second compaction".
  - The belief was measured by pausing the transcript at many points and asking whether actions might affect real systems (line 153).
  - The false "simulated" belief predates any compaction (line 133).
  - Runs lasted 10–34 h (line 49).
  - Unambiguous statements moved behaviour, and hints did not (lines 175, 183, 227).
- **Neither source gives** the window size, the number of compactions, or what the summariser saw.
- **The concrete prediction** (our operationalisation):
  1. a hedged inference is lost or hardens into fact in the summary;
  2. stated belief jumps at compaction boundaries, more at the second;
  3. behaviour follows the hardened belief.

## 3. Product compaction mechanics (primary sources)

From pinned source or official docs (rows 59–66). Code was read, never run.

| Product (pin) | Trigger | 128k / 200k / 1M window | Summariser input | Resumed context | Repeated |
|---|---|---|---|---|---|
| **Codex CLI**, local path (non-OpenAI providers) (`ac9db7d`) | API-reported context tokens ≥ 0.9 × window (forced at 0.95) | 115.2k / 180k / 900k | full live history as API items, **reasoning items kept**, tool outputs as stored (≤ 10k tokens each); prompt sent as a final user message | rebuilt initial context + newest real user messages up to 20k tokens (bytes/4) + summary behind `SUMMARY_PREFIX` | previous summary is in the input, excluded from kept user messages |
| **Claude Code** 2.1.300 (docs + binary) | window − min(max out, 20k) − 13k | 95k / 167k / 967k | same system prompt, tools and history, plus an instruction as a final user message; older tool outputs cleared first; whether thinking is sent is unverified | no turns kept; re-injects CLAUDE.md, memory, plan, ≤ 5 recently modified files (≤ 5k tokens each), skills; summary as a user message | earlier summary is re-summarised |
| **Claude API** `compact_20260112` (docs) | input ≥ 150k (min 50k) | 150k fixed | whole conversation | compaction block onward | "summarizes the old summary and everything after it" |
| **pi** coding-agent 1.2.0 (`b06c1f2`) | context > window − 16,384 | 111.6k / 183.6k / 983.6k | serialised text, **thinking omitted**, each tool result cut to 2,000 chars, separate request, caching off | system + wrapped summary + **last ~20k tokens verbatim** + read/modified file lists | iterative update prompt with `<previous-summary>` |
| **Inspect AI** 0.3.280 (`3f9c2e3`) (AISI's framework) | off unless configured; > 0.9 × window | 115.2k / 180k / 900k | system + task input + conversation since the last summary + prompt; tool outputs untouched unless overflow | system + task input + "[CONTEXT COMPACTION SUMMARY] … not a message from the user" | previous summary starts the next input |
| OpenHands V1 SDK 1.54.0 | 80 events (default agent) | event-based | each event cut to ~500 chars (inferred from code, unconfirmed) | 4 head + 35 tail events | re-summarised |
| Gemini CLI (`06d3298`) | 0.5 × 1,048,576 for any non-Gemma model | 524k | older ~70% | newest ~30% verbatim | re-summarised |
| Cline SDK (partial read) | 0.81 × window | 104k / 162k / 810k | 2,000-char tool results | last ~20k tokens | re-summarised |

**For P49:**
- **Every product reviewed here summarises with the same model by default, and feeds the previous summary into the next one.** None of them keeps the original turns for a second pass.
- **What survives verbatim differs more than the prompt does.** Codex keeps user messages, pi and Cline the last 20k tokens, Inspect only the task input, and Claude Code no turns but re-read files.
- **Inspect's default `CompactionSummary` prompt is nearly a copy of Claude Code's long-standing nine-section "control" prompt**, with added clauses telling the summariser to keep security constraints verbatim and not to treat framework messages as the user's. Inspect is MIT-licensed, so its prompt is usable verbatim.
- **Claude Code's own prompt is in the shipped binary,** but the program is "© Anthropic PBC. All rights reserved." Its five prompt arms sit behind a server-side flag, so which one a user gets is unknown from the client. P49 does not use it; that is Jacob's call (plan, Notes).
- **The Claude API default prompt is public** (row 61), but it is **not** Claude Code's prompt.

## 4. External research

**None of the reviewed sources measures fact or constraint survival per compaction index at ≥ 100k** (external review §1a; a bounded search, not an exhaustive one). The closest work:
- **Lost in (Context) Compaction** (row 37, v2, verbatim) is the only study with ≥ 100k numbers. It compacts once at 100k ("≈ 80% of a 128K window"), and at 220k for GPT-5.4-mini.
  - **Retention of user side-constraints** was 0–36% for open compactors (gpt-oss-120b: 18.5/8.4/1.3% with the Claude API prompt; 36.3/19.6/0.0% with pi's), against ≈ 90% at 10k.
  - **Behaviour** (inbox agent): unauthorised deletions went from 0/100 to **30/100** after compaction, and to 13/100 with the constraint restated.
  - **Context rot without compaction:** at 100k the uncompacted history already complied only 59–71% of the time.
  - **Judge:** its strict YES/NO retention judge is validated (human κ 1.0, n = 50; Gemini κ 0.92).
- **Is the second compaction worse? The evidence is mixed.**
  - *For:* Mythos (n = 1); the AI Village (rows 39: hostile beliefs compound through compaction states); Governance Decay's "0 → 78% over 4 rounds" (its supplement is unavailable); EAL-Bench (incremental rewriting 10–30 points worse than one-shot); the rate-distortion survey (super-linear in the number of compactions, a toy).
  - *Against:* MasDrift (72% of losses at the first hop); ContextEcho (row 67: 20 crossings of real Claude Code compactions, no direction); the Compaction Cliff's conditional survival (0.53 after round 1, then ≈ 0.66 per round, our inference).
  - **Reading:** drops concentrate at compaction 1, and a later-round effect is more likely to appear as **mutation, lost status qualifiers or belief shift**. So score drop and mutation per index, conditional and cumulative, against the independent-loss null sᵏ.
- **Context rot at 100–200k is large** without any compaction: LongMemEval −30 to −66%; WebAgents at 150k 44 → 3%; LOCA-bench GLM-4.7 76 → 11 at 128k; When Refusals Fail shows refusals moving both ways. **A long uncompacted arm is required**; a short one would not do.
- **Controls the literature recommends:**
  - **Matched-length uncompacted arm** (SUPO 64k×3 vs 192k×1; CompactionRL).
  - **Fork-and-probe at every boundary**, discarding the fork (ContextEcho).
  - **TRACE's four matched contexts** at one decision point (row 68): full raw, Sₖ₋₁ + raw Δₖ, Sₖ, and Sₖ₋₁ with Δₖ omitted. This separates round k's loss from inherited loss.
  - **A manual-deletion calibration** (Ghost in the Context: a dropped policy gave 0/90 unsafe actions because other cues carried it; The Missing Boundary 0/0/87%).
  - **A one-shot (non-recursive) compaction arm** (EAL-Bench; CliffCompaction's "never compact a compaction").
  - **Crossing the summariser and the agent** (Governance Decay: violation tracks the summariser).
  - **Same-task, realistic content** (other-task padding causes task capture), and **gating on engagement** (long contexts cause loops and abstention).
- **Constraint type and channel matter.**
  - Omission constraints decay with length alone (Gamage, row 40: 73 → 20%).
  - Soft organisational policy decays 8.3× more than hard norms (Governance Decay).
  - System-prompt policies survive every compactor (Ghost; Governance Decay). User instructions, memory and tool output don't (+50, +45, +33 points).
- **Mutations to score:** status qualifiers (killed → "completed", Tamba row 45); assistant suggestions upgraded to user decisions (AuthMem-Bench); inserted directives (OpenAI row 38: "Be transparent only if asked", 0.27–2.15% of RL summaries); omissions outnumbering mutations about 9:1 (Slipstream, row 69).
- **Behaviour concentrates right after compaction.** In Slipstream, 88–100% of first deviations come within 3 steps.
- **Summaries suppress re-verification** (CliffCompaction, row 71).
- **Sample several summaries per boundary.** At 96k, summaries vary a lot from run to run (row 70: cosine 0.47–0.62).
- **Make probes non-literal** (NoLiMa: GPT-4o 99 → 56 at 128k).
- **GLM may be a favourable case.** GLM-5.2 was trained with CompactionRL (row 75); GLM 5.3 Flash under Claude Code autocompaction scored 73.0 at 200k vs 71.0 at 45k (CliffCompaction).
- **Eval awareness** (rows 94–100). Nothing has measured it after compaction.
  - LURE: P(eval) falls weakly with transcript length (r = −0.32), and a short schematic summary may remove that dilution. So **P(eval) may rise after compaction** (untested).
  - Use Needham et al.'s forked appended question, with paraphrases, and read within-session changes rather than levels.
  - Asking can itself be the cue (o4-mini cites the question in 15% of answers).
  - Avoid grinds of repeated failure (BrowseComp) and templated repetition.

## 5. Measured on existing data (no paid calls)

Scripts and tables: `design/measure/` (outputs in `design/measure/out/`).
- **P44 R (GLM on Together, n = 45):** peak prompt per chain median 56.3k, p90 167k, max 231.6k.
  - Much of that is turns where reasoning ran into the 16k `max_tokens` cap: 12.2% of turns, 35% of the final context.
  - In the 64 existing transcripts long enough for two compactions at 100k, capped reasoning is a median 67% of the context, and **none of them ended in `done`**.
- **No existing set supports two or more compactions at ≥ 100k with natural content.**
  - SWE-rebench (67,074 trajectories, in `data/P01/raw/`): median 51.5k, max 137k; 358 exceed 100k, and none supports two compactions at 100k.
  - P01 slice A: max 94k.
  - So **Gate 0 has to run on new long sessions**; existing transcripts can only bridge P01's single-cut fidelity numbers to ~110k.
- **Growth per turn** (DeepSeek tokenizer; within 1.00–1.04 of API usage):
  - lcbhard GLM 3.9k (2.2k without capped turns), DeepSeek 2.0k; reasoning is 62–85% of it.
  - P38 ops worlds 0.74k per turn, with only ~60 tokens of tool output per turn.
  - SWE-rebench 0.69k, 68% of it tool output.
  - **Whether reasoning stays in the live context changes turns per session 3–5×.**
- **Endpoints** (OpenRouter, 2026-10-15T02:19Z; $ in / out / cache-read per M; tok/s):

| Model | Provider | Price | tok/s |
|---|---|---|---|
| GLM 5.3 Flash | StreamLake fp8 | 0.0435 / 0.145 / 0.0087 | 38 |
| GLM 5.3 Flash | CoreWeave nvfp4 | 0.15 / 0.50 / 0.029 | 155 |
| DeepSeek V4 Flash | GMICloud fp8 | 0.091 / 0.182 / 0.0182 | 61 |
| DeepSeek V4 Flash | Baidu fp8 | 0.14 / 0.28 / 0.028 | 100 |
| DeepSeek V4 Flash | StreamLake fp8 | 0.0826 / 0.165 / 0.0165 | 43 |
| Claude Haiku 5.5 | Vertex | 0.10 / 0.50 / 0.010 | 185 |

  All have a 1M context window.

## 6. Gap P49 fills

Among the sources reviewed, there is no study of **window-triggered, repeated (≥ 3) compaction at ≥ 100k** that measures per-index fact survival and mutation, **with product carry-over**, on facts that arise naturally in the task, together with behaviour on paired forks and stated belief just before and after each compaction, against a matched-length uncompacted history. Lost in Compaction is the nearest: one compaction, constraints planted in the first user turn, and one behavioural setting.
