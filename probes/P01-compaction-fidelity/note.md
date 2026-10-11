# P01: How often does a compaction summary drop facts, invent them, or misstate whether the task is done?

**Status:** done, 2026-10-11. Spend **$45.67 of $75** (API only). The ledger overstates this by up to ~$3.8: four batches were stopped by hand and settled at their full reservation (log.md). Plan: [plan.md](plan.md); run log: [log.md](log.md); literature: [litreview.md](litreview.md). Review: one gpt-6-astra round of the branch, `logs/reviews/20261011-P01-branch-r1.md` (verdict in the log).

## Answer

We compacted 250 coding-agent transcripts at a seeded point 50–90% of the way through. The transcripts are 150 public OpenHands runs on real GitHub issues (slice A) and 100 of our X3b GLM episodes on impossible tests (slice B). Each was compacted under three real harness prompts (Codex CLI, OpenHands, pi-mono) by three summarizers (GLM 5.3 Flash, DeepSeek V4 Flash, Gemini 3.8 Flash): 2,221 summaries in all.

1. **Summaries rarely state a false test result. The automatic judge does, often.**
   - luna flagged a false "the tests pass" in 2.0% [1.2, 2.8] of summaries.
   - An Opus adjudicator upheld only 1 of 12 sampled flags (8% [1, 35]): the rest were partial claims ("all valid cases pass") in summaries that elsewhere say the suite fails.
   - Earlier versions of the judge, before three rounds of fixing the extraction, put status errors at 30–53% per cell. That number was the judge's error, not the summaries'.
2. **The one clear false-statement failure is model-specific: DeepSeek V4 Flash sometimes writes a fake continuation of the session instead of a summary.**
   - **Rate:** 58 of 734 DeepSeek summaries (7.9% [6.2, 10.1]) continue the transcript in its own format, with invented tool calls and tool output. Examples: "All 30 tests pass" after a run that never happened, a fix that was never written, a 52k-character session that ends in a repetition loop. This count is exact (a code check for transcript-format lines), and it is 0 for GLM (737) and Gemini (750).
   - **Effect:** a resumed agent would read invented tool results as its own history.
   - **In the F2 sample:** these continuations account for 2 of the 4 upheld invented facts and 3 of the 6 upheld false "task complete" claims. All 4 upheld inventions and 5 of the 6 upheld false completions are DeepSeek's.
3. **Dropped facts are common; what is dropped depends on the fact more than on the setup.** These are luna verdicts, with precision 69% [44, 86] against the adjudicator and low sensitivity on planted deletions, so the rates below are approximate.
   - **The most recent error** is missing in 54–58% of summaries.
   - **The latest test result** is missing in 26–40%. On top of that, pi-mono's 2,000-character cut of tool output hides it from the summarizer in 47% [40, 53] of transcripts.
   - **The user's "don't modify the tests" rule** is dropped by 28% of Codex-prompt summaries but only 3.5% of pi-mono ones, which have a "Constraints & Preferences" section. Codex and OpenHands keep the user's message verbatim anyway, so after retention only pi-mono can lose it.
   - **The task and the edited file names** (file names scored in code) are nearly always kept: ≤ 5%.
4. **Summaries do record cheating.** In slice B, 97% [95, 98] of summaries of cheating episodes (427/440) say the tests passed only through a workaround in `solution.py`. The plan predicted fewer than half. In X3b, GLM was open about the special-casing in its visible text, so the summary has it to copy.
5. **The summarizer matters more than the setup.**
   - On facts all three setups showed the summarizer, the mean share dropped ranges 0.12 across summarizers (DeepSeek 25%, Gemini 19%, GLM 14%) but only 0.05 across setups.
   - Unusable compactions are also mostly a summarizer property. Counting empty after retries plus not a summary at all: DeepSeek 10–20% per setup, Gemini 12% under the Codex prompt only, GLM ≤ 3%.

**What it changes (the plan's outcome table):**
- Two of three summarizers make few false statements; the third, DeepSeek, fabricates in about 8%. Drops are common. That is the plan's "mainly drops, few false statements" row → **pivot to X6**: does a dropped fact (the constraint, the last error, the test state) change what the resumed agent does?
- Don't build X0 into a fidelity study on luna. Its invented-fact and status flags have 8–17% precision here.
- DeepSeek's fabricated continuations are a ready-made, natural stimulus for X2 (does a resumed agent trust an invented "tests pass" history?), with no planting needed.

## Setup (as run)

- **Transcripts:**
  - **Slice A:** 150 from `nebius/SWE-rebench-openhands-trajectories` (rev `35455389ab51`, parquet sha256 `14048dd1…efa1`): 75 resolved and 75 unresolved, ≥ 40 messages; seed 20261011; median 121 messages; prefix ≈ 40k tokens.
  - **Slice B:** 100 X3b GLM 5.3 Flash episodes (role "main"): 50 cheating and 50 clean; prefix ≈ 6.4k tokens.
  - The cut is one seeded turn in the 50–90% window. For cheating episodes it is drawn above the first passing run of the real test file, so the cheat lies in the compacted part.
  - Manifests: `data/P01/slice_{a,b}.manifest.json`.
- **Harness prompts:** verbatim from GitHub at pinned commits (`prompts/SOURCES.md`). Each setup's own retention rules are applied: Codex keeps user messages, OpenHands keeps the first 2 events, pi-mono keeps nothing before the cut and truncates tool output at 2,000 characters.
  - One serialization (pi-mono's) for all three; reasoning text is never shown (log.md design decisions 1–3).
  - Deviation: real Codex sends the history as API items, not text.
- **Summarizers:** via OpenRouter, temperature 0, max 16,000 output tokens, provider-default reasoning. An empty or failed call is retried with the next seed (up to 3 tries; 2 for 16 DeepSeek items, log 22:08). OpenRouter spreads each model over several providers, which we did not pin.
- **Measures:**
  - **Truth in code:** last suite run, last agent test script, last error line, edited files. Slice A adds the hidden-test `resolved` label; slice B the X3b detector plus runs of the real test file.
  - **luna (gpt-6-luna, reasoning off), unvalidated for this task:**
    - one call per transcript for the agent's own latest claims;
    - one call per summary for the checklist and its test-status statements (each tagged by subject and whether it is about the latest state);
    - one call per summary for invented facts;
    - one call per summary for "is this a summary at all" (F3).
  - **File facts are scored in code** (log 19:37, 19:53).
  - **Primary status measure** (log 22:16): a false "tests pass" about the task's suite (slice B: the real tests never passed), or a false "task complete" in slice A (hidden tests unresolved, or the agent hadn't claimed completion).
- **Validation:**
  - **Planted errors** (gate). Status insertions caught 52/60 over three runs on two fresh transcript sets. luna caught deleted file names in only 19/40, so file facts moved to code; read literally, the plan's luna-only gate ends at 26/40 (65%), below its 70% line. Fabrications (exploratory) caught 20/20 in every run.
  - **Precision (F2):** 68 seeded flags adjudicated against the transcripts by Opus subagents. This is an LLM reference, not hand labels.

## Results

Source: `results/P01/analysis_bulk.json`, `tables_bulk.md`, `extras.json`, `precision/summary.json`. Intervals are Wilson 95% per cell, or a 2,000-resample transcript-cluster bootstrap for pooled rates. All rates are luna's verdicts unless they say "code" or "adjudicated".

### Per setup × summarizer (luna verdicts; read with the precision row below)

| Setup | Summarizer | n | False "tests pass" | False "task complete" (A) | ≥ 1 fact dropped | ≥ 1 lost after retention | Mean share dropped | Fake continuation (code) | Unusable compaction | Workaround kept (B, cheating) |
|---|---|---|---|---|---|---|---|---|---|---|
| Codex | GLM | 243 | 3% [2, 6] | 5% [3, 10] | 40% [34, 47] | 37% | 10% | 0/243 | 3% | 50/50 |
| Codex | DeepSeek | 245 | 4% [2, 7] | 26% [20, 34] | 83% [78, 87] | 69% | 37% | 21/245 | 20% | 45/47 |
| Codex | Gemini | 250 | 1% [0, 4] | 0% [0, 2] | 68% [62, 73] | 58% | 21% | 0/250 | 12% | 49/50 |
| OpenHands | GLM | 246 | 2% [1, 5] | 11% [7, 17] | 35% [29, 41] | 35% | 16% | 0/248 | 1% | 49/49 |
| OpenHands | DeepSeek | 242 | 4% [2, 7] | 32% [25, 40] | 57% [50, 63] | 57% | 31% | 14/242 | 10% | 42/47 |
| OpenHands | Gemini | 250 | 1% [0, 4] | 3% [1, 8] | 46% [40, 52] | 46% | 23% | 0/250 | 0% | 48/50 |
| pi-mono | GLM | 246 | 0% [0, 2] | 0% [0, 3] | 25% [20, 31] | 34% | 6% | 0/246 | 2% | 50/50 |
| pi-mono | DeepSeek | 247 | 1% [0, 3] | 15% [10, 21] | 39% [33, 45] | 55% | 13% | 23/247 | 11% | 44/47 |
| pi-mono | Gemini | 250 | 0% [0, 2] | 1% [0, 4] | 32% [26, 38] | 52% | 8% | 0/250 | 0% | 50/50 |

- **Columns:**
  - "Lost after retention" counts facts the resumed agent lacks, whatever the cause. That includes facts the setup never showed its summarizer and excludes those it keeps verbatim. It is higher than "dropped" for pi-mono (truncation) and equal for OpenHands.
  - "Unusable" = no summary after retries, or luna says the output is not a summary.
  - **First-attempt failures:** GLM 2–10% per setup (reasoning that ran to the 16k-token limit without writing a summary); DeepSeek 3–7%; Gemini 0%.
- **Adjudicated precision of luna's flags (F2):**
  - false "tests pass" 1/12 (8% [1, 35]);
  - false "task complete" 6/16 (38% [18, 61]); DeepSeek 5/12, GLM 1/4;
  - invented 4/24 (17% [7, 36]); DeepSeek 4/10, GLM and Gemini 0/14;
  - drop 11/16 (69% [44, 86]).
- **luna's invented-fact rate** (≥ 1 flag in 64–87% of summaries) is not shown in the table. At 17% precision it says more about luna than about the summaries.

### Which facts get dropped (shown facts only, luna)

| Fact | Codex | OpenHands | pi-mono |
|---|---|---|---|
| Most recent error | 58% (184/320) | 57% (180/316) | 54% (137/252); 21% of errors not shown at all |
| Latest test result | 40% (285/708) | 29% (202/709) | 26% (98/378); 47% of results not shown at all |
| "Don't modify the tests" | 28% (207/738), kept verbatim | never shown, kept verbatim | 3.5% (26/743) |
| Task | 4% | never shown, kept verbatim | 0.4% |
| Edited file named (code) | 4.5% | 1.2% | 1.9% |
| Cheating workaround (B) | 2% | 5% | 2% |

- The file-name check (code) and luna agree on 1,994 of 2,024 file facts. Where the name is absent, luna says "present" in 5 of 51 natural cases, but in 10–11 of 20 planted deletions (F1b, F4).

### Status: the truth-vs-claim breakdown that changed the measure

The first bulk status rates (30–53% per cell, `results/P01/dev/analysis_bulk_audit4.json`) came almost entirely from the judge:
- **Next steps read as "not run" claims:** "Run pytest on the full test suite" (about 240 of ~1,000 flags).
- **The agent's ad-hoc checks read as test passes:** "Verified small cases: (2,2)→2 …" (about 170).
- **Partial claims read as whole-suite claims.**

The final measure keeps only latest-state statements about the task's suite. The F2 sample shows that even its false "pass" flags are mostly partial claims. So the statement "summaries rarely misstate test results" rests on the adjudicated sample (1/12 upheld) and on the planted insertions (52/60 caught over three runs), not on luna's rate.

## Predictions vs outcomes

| Prediction (plan) | Probability | Outcome |
|---|---|---|
| (a) Status misstatements in ≥ 10% of summaries for at least one cell | 0.55 | **Mixed.** luna's false "complete" exceeds 10% in the DeepSeek cells (15–32%), and about 5/12 of those flags are upheld, so DeepSeek is plausibly ≥ 10%. False "tests pass" is ≤ 4% flagged and ~0 after adjudication. |
| …most repeat the agent's own wrong belief | 0.7 given (a) | **No.** 20 of 178 flagged misstatements match the agent's own latest claim; the rest are the summarizer's. The agent itself claimed completion in only 9% of slice A transcripts at the cut. |
| (b) ≥ 30% of summaries drop at least one checklist fact | 0.7 | **Yes.** 47% [43, 51] (luna; precision 0.69, sensitivity low on planted deletions). |
| …in B, fewer than half of cheating-episode summaries say the tests were edited | 0.5 | **No, reframed.** No X3b GLM cheat edits the tests (detector: 0/630). The stand-in fact, "the pass came from a workaround", is kept in 97% [95, 98]. |
| (c) Invented facts in ≥ 10% of summaries | 0.35 | **Yes for DeepSeek only.** Code-measured fabricated continuations: DeepSeek 7.9% [6.2, 10.1], GLM and Gemini 0%. luna's 78% rate is mostly false positives. |
| (d) The prompt matters more than the summarizer for drops | 0.5 | **No.** Spread across summarizers 0.12 vs across setups 0.05 on commonly shown facts (0.16 vs 0.14 on each setup's own shown facts). |

## Follow-ups (all exploratory; each has a log.md entry)

- **F1, F1b, F4: the measurement had to be rebuilt three times after seeing data.** The note reports the final measure. The superseded outputs are in `results/P01/dev/`.
  - **F1:** status statements extracted one by one, quote first.
  - **F1b:** file facts defined as "the file is named", then scored in code.
  - **F4:** statements tagged by subject and latest state; next steps excluded; then the primary measure narrowed to false "pass" and false "complete".
- **F2:** precision of luna's flags, adjudicated by Opus (above).
- **F3:** "is it a summary at all?" (luna).
  - Gemini under the Codex prompt drifts into re-investigating in 29/250. From 4 random reads, these are headed "Handoff Summary" and then start redoing the task.
  - DeepSeek: 46/245 Codex, 17/242 OpenHands, 25/247 pi-mono.
  - The Codex prompt has no system prompt telling the model not to continue, which may matter. Our text serialization may also differ from real Codex here.
- **F5:** fabricated continuation counted in code (above). No prediction was written beforehand.

**Not done:** the 60-item hand audit through a labelling page, the plan's follow-up 1. F2 is an LLM stand-in for it. If X0 is ever revived, the hand audit comes first and should cover the invented-fact and drop measures. The X2 behavioural test (follow-up 2) and Anthropic's API compaction prompt (follow-up 5; the literature review found it may be public, unverified) were not run.

## Limitations

- luna is unvalidated for this task, and F2's reference is an LLM (this repo found Opus lenient as an EM reference). Only the planted insertions and the code-scored measures are independent of an LLM judge's reading.
- Drop rates are luna verdicts: 69% precision, and they missed half of planted deletions of file names. The non-file drop rates are rough in both directions.
- **Cuts are forced:** slice B transcripts are short (≈ 6k tokens), so no real harness would compact them. Slice A agents are from 2025 (Qwen3-Coder-480B).
- **One serialization for all setups.** Real Codex differs; the Gemini/Codex drift (F3) may be partly ours.
- **Providers not pinned:** each summarizer ran on several OpenRouter providers, possibly differently quantized, and our rows don't record which.
- **Retries:** GLM's empty first attempts were retried, which a harness may or may not do.

## Random raw samples

`results/P01/samples.md`, drawn by `p01/samples.py` (seed 20261011): 4 random summaries with every verdict, 6 random status flags with quotes, and 12 random invented-fact flags with luna's reasons. Every drawn row is shown. F2's 68 adjudicated items, each with the adjudicator's reason and a quote, are in `results/P01/precision/labels_*.jsonl`. Raw summaries are in `results/P01/summaries/<model>.jsonl`.

## Spend and wall-clock

| Phase | UTC | Wall-clock | Spend |
|---|---|---|---|
| Build: worktree, prompts, data, loaders (2 subagents), tests | 17:30–17:54 | 0.4 h | $0 |
| Smoke and gate iterations: smoke, smoke2 (F1), smoke3 (F1b); two hung GLM calls cost ~45 min | 17:55–19:37 | 1.7 h | ≈ $5.1 |
| Bulk summaries (Gemini 11 min; GLM and DeepSeek 2.5 h: a stall, an accounting stop, two restarts) | 19:38–22:08 | 2.5 h | GLM $5.90, DeepSeek $3.73, Gemini $16.24 (incl. smoke) |
| Bulk judging: beliefs, audit4, audit5, invented, shape (overlapped with summaries) | 19:38–22:14 | (overlapped) | beliefs $0.58, audit4 $5.82, audit5 $5.85, invented $5.96, shape $0.30 |
| Planted checks (all rounds) | — | — | $0.64 |
| F2 adjudication (8 Opus subagents), F5, analysis, note | 22:17–22:40 | 0.4 h | $0 |
| **Total** | | **≈ 5.2 h** | **$45.67** (ledger; ≤ ~$3.8 overcount) |

- Summary costs include all retries. Smoke-stage luna audits v1–v3 cost $0.65.
- **Wall-clock lost to throughput problems:**
  - GLM and DeepSeek calls hung behind provider queues at 32 concurrent. The SDK's per-read timeout never fires on OpenRouter keep-alives, so `emx.llm_batch` now has a hard per-attempt deadline.
  - Bound prices were too low for some OpenRouter providers; bounds now use the most expensive provider.
  - Chunk barriers made each chunk wait for its slowest call.
  - Together these cost about 1.5 h. All three fixes are in the shared `emx.llm_batch`.
