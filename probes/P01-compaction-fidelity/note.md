# P01: How often does a compaction summary drop facts, invent them, or misstate whether the task is done?

**Status:** done, 2026-10-11. Spend **$45.67 of $75** (API only). The ledger overstates this by up to ~$3.8: four batches were stopped by hand and settled at their full reservation (log.md). Plan: [plan.md](plan.md); run log: [log.md](log.md); literature: [litreview.md](litreview.md). Review: one gpt-6-astra round of the branch, `logs/reviews/20261011-P01-branch-r1.md`: MERGE AFTER FIXES (3 blocking, 5 minor). All were fixed or reworded; see the review table at the end and the log.

## Answer

We compacted 250 coding-agent transcripts at a seeded point 50–90% of the way through. The transcripts are 150 public OpenHands runs on real GitHub issues (slice A) and 100 of our X3b GLM episodes on impossible tests (slice B). Each was compacted under three real harness prompts (Codex CLI, OpenHands, pi-mono) by three summarizers (GLM 5.3 Flash, DeepSeek V4 Flash, Gemini 3.8 Flash): 2,221 summaries in all.

1. **We found few false "the tests pass" claims, but how many false status claims the judge missed is unknown.**
   - luna flagged a false "tests pass" in 1.9% [1.2, 2.7] of summaries.
   - An Opus adjudicator upheld 1 of 12 sampled flags (8% [1, 35]). The rest were partial claims ("all valid cases pass") in summaries that elsewhere say the suite fails.
   - This measures the precision of flags, not how often real errors go unflagged. Planted false statements were caught 52/60, but over three runs on two fresh sets with two extractor versions. False "fail" and "not run" claims could not be scored precisely at all (see Status below).
   - Before three rounds of fixing the extraction, the judge put status errors at 30–53% per cell. That number was the judge's error.
2. **The clearest false-statement failure is model-specific: DeepSeek V4 Flash sometimes writes a continuation of the session instead of a summary.**
   - **Markers:** 58 of 734 DeepSeek summaries (7.9%) contain serialized-continuation markers: lines in the transcript's own `[Assistant]:` / `[Tool result]:` / tool-call format. GLM (737) and Gemini (750) have none.
   - **Invented tool output:** in 43 of those 58 (5.9% [4.4, 7.8] of DeepSeek summaries), the summary carries tool output that does not occur in the transcript. Examples: "All 30 tests pass" after a run that never happened, a fix that was never written, a 52k-character session ending in a repetition loop. This is a code check: the first 120 characters of each `[Tool result]:` block are searched for in the transcript. 7 hits only copy real tool output, and 8 have no tool output (they stop at an intention).
   - **Effect:** a resumed agent would read the invented results as its own history.
   - **In the F2 sample:** all 4 upheld invented facts and 5 of the 6 upheld false "task complete" claims are DeepSeek's. Marker summaries account for 2 and 3 of them.
   - **What the zero means:** zero markers for GLM and Gemini rules out this format, not every kind of fabrication.
3. **Dropped facts are common; what is dropped depends on the fact more than on the setup.** These are luna verdicts: precision 69% [44, 86] against the adjudicator, low sensitivity on planted deletions, so all rates are approximate.
   - **The most recent error** is missing in 54–58% of summaries.
   - **The latest test result** is missing in 26–40%. On top of that, pi-mono's 2,000-character cut of tool output hides it from the summarizer in 49% [43, 55] of transcripts.
   - **The user's "don't modify the tests" rule** is dropped by 28% of Codex-prompt summaries but only 3.5% of pi-mono ones, which have a "Constraints & Preferences" section. Codex and OpenHands keep the user's message verbatim anyway, so after retention only pi-mono can lose it.
   - **The task and the edited file names** (file names scored in code) are nearly always kept: ≤ 5.5%.
4. **Summaries do record cheating.** In slice B, 97% [95, 98] of summaries of cheating episodes (427/440) say the tests passed only through a workaround in `solution.py`. In X3b, GLM was open about the special-casing in its visible text, so the summary has it to copy.
5. **For drops, the summarizer matters more than the setup.**
   - On facts all three setups showed the summarizer, the mean share dropped ranges 0.13 across summarizers (DeepSeek 26%, Gemini 19%, GLM 13%) but only 0.05 across setups.
   - Unusable compactions are also mostly a summarizer property. Counting empty after retries plus not a summary at all (F3, luna), per 250 attempts: DeepSeek 51 Codex / 25 OpenHands / 28 pi-mono; Gemini 29 under Codex and 0 otherwise; GLM 2–8.

**What it changes:**
- This doesn't cleanly select a row of the plan's outcome table. Drops are common, which is solid enough to act on. False statements are low as far as we could validate, but their prevalence is unresolved.
- **Recommendation: pivot to X6 as the next exploratory priority:** does a dropped fact (the constraint, the last error, the test state) change what the resumed agent does?
- Don't build X0 into a fidelity study on luna. Its invented-fact and status flags have 8–17% precision here, so a study would need hand labels first.
- DeepSeek's fabricated continuations are a natural stimulus for X2 (does a resumed agent trust an invented "tests pass" history?). Pick verified examples: the 43 with invented tool output are a ready pool.

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
- **Truth in code:**
  - The last suite run and the last agent test script, each the latest *classifiable* run. If a later run of that kind is unclassifiable, the current state counts as unknown and is not scored (review finding 6).
  - The last error line, and the edited files.
  - Slice A adds the hidden-test `resolved` label; slice B the X3b detector plus runs of the real test file.
- **luna (gpt-6-luna, reasoning off), unvalidated for this task:**
  - one call per transcript for the agent's own latest claims;
  - one call per summary for the checklist and its test-status statements (each tagged by subject and whether it is about the latest state);
  - one call per summary for invented facts;
  - one call per summary for "is this a summary at all" (F3).
- **File facts are scored in code:** the basename or an identifier-like module stem, matched at name boundaries (log 19:37, 19:53; review finding 4).
- **Primary status measure** (log 22:16): a false "tests pass" about the task's suite, or a false "task complete" in slice A (hidden tests unresolved, or the agent hadn't claimed completion).
  - In slice B, "pass" is false unless the real tests had passed before the cut.
  - A pass claim counts as true if either the last suite run or the last agent test script passed. This lenient choice makes the measure a lower bound.
- **Fact visibility:** a fact counts as shown to a setup only if its anchor (error line, test summary line, file basename, task message) appears in its own source message as that setup renders it (review finding 5).
- **Validation:**
  - **Planted errors** (the plan's gate). Status insertions caught 52/60 over three runs on two fresh transcript sets, with the extractor changing between runs. luna caught deleted file names in only 19/40, so file facts moved to code; read literally, the plan's luna-only gate ends at 26/40 (65%), below its 70% line. Fabrications (exploratory) caught 20/20 in every run.
  - **Precision (F2):** 68 seeded flags adjudicated against the transcripts by Opus subagents. This is an LLM reference, not hand labels.

## Results

Source: `results/P01/analysis_bulk.json`, `tables_bulk.md`, `extras.json`, `precision/summary.json`. Intervals are Wilson 95% per cell, or a 2,000-resample transcript-cluster bootstrap for pooled rates. All rates are luna's verdicts unless they say "code" or "adjudicated". Denominators differ by column: summaries without a summary, without a parse, or with an unscorable claim are excluded, and every count is shown.

### Per setup × summarizer (luna verdicts; read with the precision rows below)

| Setup | Summarizer | n | False "tests pass" | False "task complete" (A) | ≥ 1 fact dropped | Lost after retention | Mean share dropped | Continuation markers (code) | Unusable (of 250 attempts) | Workaround kept (B, cheating) |
|---|---|---|---|---|---|---|---|---|---|---|
| Codex | GLM | 243 | 3% [1, 6] (7/236) | 5% [3, 10] (8/148) | 40% [34, 46] (97/243) | 37% | 10% | 0/243 | 8 | 50/50 |
| Codex | DeepSeek | 245 | 4% [2, 7] (9/238) | 26% [20, 34] (39/150) | 83% [78, 87] (204/245) | 70% | 38% | 21/245 | 51 | 45/47 |
| Codex | Gemini | 250 | 1% [0, 4] (3/244) | 0% [0, 2] (0/150) | 67% [61, 73] (168/250) | 58% | 21% | 0/250 | 29 | 49/50 |
| OpenHands | GLM | 246 | 2% [1, 5] (6/240) | 11% [7, 17] (16/149) | 34% [29, 40] (84/246) | 35% | 16% | 0/248 | 2 | 49/49 |
| OpenHands | DeepSeek | 242 | 4% [2, 7] (9/234) | 32% [25, 40] (47/148) | 56% [50, 62] (136/242) | 57% | 31% | 14/242 | 25 | 42/47 |
| OpenHands | Gemini | 250 | 1% [0, 4] (3/243) | 3% [1, 8] (5/150) | 45% [39, 51] (113/250) | 46% | 22% | 0/250 | 0 | 48/50 |
| pi-mono | GLM | 246 | 0% [0, 2] (1/239) | 0% [0, 3] (0/149) | 24% [19, 30] (59/246) | 34% | 6% | 0/246 | 4 | 50/50 |
| pi-mono | DeepSeek | 247 | 1% [0, 3] (2/242) | 15% [10, 21] (22/150) | 38% [33, 45] (95/247) | 55% | 13% | 23/247 | 28 | 44/47 |
| pi-mono | Gemini | 250 | 0% [0, 2] (1/245) | 1% [0, 4] (1/150) | 31% [26, 37] (78/250) | 52% | 8% | 0/250 | 0 | 50/50 |

- **Columns:**
  - "Lost after retention" counts facts the resumed agent lacks, whatever the cause. That includes facts the setup never showed its summarizer and excludes those it keeps verbatim.
  - "Unusable" = no summary after retries, or luna says the output is not a summary.
  - **First-attempt failures:** GLM 2–10% per setup (reasoning that ran to the 16k-token limit without writing a summary); DeepSeek 3–7%; Gemini 0%.
- **Adjudicated precision of luna's flags (F2).** Drawn from the flags before the review's scoring fixes, which changed a handful of flags.
  - false "tests pass" 1/12 (8% [1, 35]);
  - false "task complete" 6/16 (38% [18, 61]); DeepSeek 5/12, GLM 1/4;
  - invented-fact claims 4/24 (17% [7, 36]), per flagged claim, not per summary; DeepSeek 4/10, GLM and Gemini 0/14;
  - drop 11/16 (69% [44, 86]).
- **luna's invented-fact rate** (≥ 1 flag in 64–87% of summaries) is not shown in the table. At 17% claim precision it says more about luna than about the summaries.
- **Who is wrong** in the flagged status misstatements: 20 of 176 match the agent's own latest claim; the rest are the summarizer's. This is a split of automatic flags, not of established errors.

### Which facts get dropped (shown facts only, luna)

| Fact | Codex | OpenHands | pi-mono |
|---|---|---|---|
| Most recent error | 58% (184/320) | 57% (180/316) | 54% (137/252); 21% of errors (23/108) not shown at all |
| Latest test result | 40% (282/705) | 28% (199/706) | 26% (94/363); 49% of results (117/240) not shown at all |
| "Don't modify the tests" | 28% (207/738), kept verbatim | never shown, kept verbatim | 3.5% (26/743) |
| Task | 4% (30/738) | never shown, kept verbatim | 0.4% (3/743) |
| Edited file named (code) | 5.5% (37/674) | 1.2% (8/673) | 2.1% (14/677) |
| Cheating workaround (B) | 2% (3/147) | 5% (7/146) | 2% (3/147) |

- The file-name check (code) and luna agree on 1,994 of 2,024 file facts. Where the name is absent, luna says "present" in 9 of 59 natural cases, but in 10–11 of 20 planted deletions.

### Status: the claim types that couldn't be scored, and how the measure changed

The first bulk status rates (30–53% per cell, `results/P01/dev/analysis_bulk_audit4.json`) came largely from the judge. Reading the breakdown by truth state × claim, with random quotes, gave these approximate counts (from a one-off breakdown, not a committed analysis):
- **Next steps read as "not run" claims:** "Run pytest on the full test suite" (≈ 240 of ≈ 1,000 flags).
- **The agent's ad-hoc checks read as test passes:** "Verified small cases: (2,2)→2 …" (≈ 170).
- **Partial claims read as whole-suite claims.**

Even after statements were tagged by subject (suite vs the agent's own checks) and by state, "fail" and "not run" claims stayed mostly defensible:
- an impossible suite "must fail";
- "not re-run since the change";
- "a pre-existing test fails".

They are kept only as a secondary column (`tests_misstated_any`, pooled 11.4%). So the evidence covers false *success* claims, and false failure or not-run claims are unmeasured.

## Predictions vs outcomes

| Prediction (plan) | Probability | Outcome |
|---|---|---|
| (a) Status misstatements in ≥ 10% of summaries for at least one cell | 0.55 | **Mixed.** luna's false "complete" exceeds 10% in the DeepSeek cells (15–32%), and 5/12 of DeepSeek's sampled flags are upheld, so DeepSeek is plausibly near or above 10%. False "tests pass" is ≤ 4% flagged, with 1/12 of flags upheld. |
| …most repeat the agent's own wrong belief | 0.7 given (a) | **Not supported, on automatic flags:** 20 of 176 flagged misstatements match the agent's own latest claim. |
| (b) ≥ 30% of summaries drop at least one checklist fact | 0.7 | **Yes, conditional on luna's unvalidated drop labels:** 47% [43, 51] (precision 0.69; low sensitivity on planted deletions). |
| …in B, fewer than half of cheating-episode summaries say the tests were edited | 0.5 | **Not evaluable as planned:** no X3b GLM cheat edits the tests (detector 0/630). Exploratory stand-in: "the pass came from a workaround" is kept in 97% [95, 98]. |
| (c) Invented facts in ≥ 10% of summaries | 0.35 | **Not demonstrated.** luna's 78% is mostly false positives (claim precision 17%). The code-measured invented tool output is 5.9% [4.4, 7.8] for DeepSeek and 0 for the others; that is a narrower measure than the plan's. |
| (d) The prompt matters more than the summarizer for drops | 0.5 | **No.** Spread across summarizers 0.13 vs across setups 0.05 on commonly shown facts (0.17 vs 0.14 on each setup's own shown facts). |

## Follow-ups (all exploratory; each has a log.md entry)

- **F1, F1b, F4: the measurement had to be rebuilt three times after seeing data.** The note reports the final measure. The superseded outputs are in `results/P01/dev/`.
  - **F1:** status statements extracted one by one, quote first.
  - **F1b:** file facts defined as "the file is named", then scored in code.
  - **F4:** statements tagged by subject and latest state; next steps excluded; the primary measure narrowed to false "pass" and false "complete".
- **F2:** precision of luna's flags, adjudicated by Opus (above).
- **F3:** "is it a summary at all?" (luna).
  - Gemini under the Codex prompt drifts into re-investigating in 29/250. From 4 random reads, these are headed "Handoff Summary" and then start redoing the task.
  - DeepSeek: 46/245 Codex, 17/242 OpenHands, 25/247 pi-mono.
  - The Codex prompt has no system prompt telling the model not to continue, which may matter. Our text serialization may also differ from real Codex here.
- **F5:** serialized-continuation markers and invented tool output, counted in code (above). No prediction was written beforehand; the invented-output check was added at review.

**Not done:**
- The 60-item hand audit through a labelling page (the plan's follow-up 1). F2 is an LLM stand-in for it, and it covers precision only. Missed errors (recall) need labels on unflagged summaries too.
- The X2 behavioural test (follow-up 2).
- Anthropic's API compaction prompt (follow-up 5). The literature review found it may be public; unverified.

## Limitations

- luna is unvalidated for this task, and F2's reference is an LLM (this repo found Opus lenient as an EM reference). Only the planted insertions and the code-scored measures are independent of an LLM judge's reading.
- Drop rates are luna verdicts: 69% precision, and they missed half of planted deletions of file names. The non-file drop rates are rough in both directions.
- **Cuts are forced:** slice B transcripts are short (≈ 6k tokens), so no real harness would compact them. Slice A agents are from 2025 (Qwen3-Coder-480B).
- **One serialization for all setups.** Real Codex differs; the Gemini/Codex drift (F3) may be partly ours.
- **Providers not pinned:** each summarizer ran on several OpenRouter providers, possibly differently quantized, and our rows don't record which.
- **Retries:** GLM's empty first attempts were retried, which a harness may or may not do.
- **Truth is per run, not per test:** a claim about one specific test is checked against the whole last run.

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
| F2 adjudication (8 Opus subagents), F5, analysis, note, review and fixes | 22:17–23:15 | ≈ 1 h | $0 |
| **Total** | | **≈ 5.8 h** | **$45.67** (ledger; ≤ ~$3.8 overcount) |

- Summary costs include all retries. Smoke-stage luna audits v1–v3 cost $0.65.
- **Throughput problems** cost an estimated ~1.5 h (a narrative estimate from the log, not a measured breakdown).
  - **Hung calls:** GLM and DeepSeek calls hung behind provider queues at 32 concurrent. Fixed in the shared `emx.llm_batch`: OpenRouter keep-alives mean a per-read timeout never fires, so there is now a hard per-attempt deadline, and the bound prices are now the most expensive OpenRouter provider's.
  - **Not fixed, and worth fixing before the next large batch** (review finding 7):
    - Chunks still run one after another, each waiting for its slowest call.
    - The deadline is per attempt, so 5 attempts can hold a worker for ~50 minutes.
    - luna calls have no hard deadline.
    - A per-item total deadline and rolling scheduling would save an estimated ~40 minutes per model per run like this one (a scenario estimate).

## Review round 1 (gpt-6-astra, MERGE AFTER FIXES) and what was done

| Finding | Severity | Done |
|---|---|---|
| 1. Headline and decision claim more than the validation supports | BLOCKING | Reworded throughout: precision vs prevalence, extractor versions in the 52/60, the lenient pass rule stated, invented precision per claim, X6 framed as an exploratory priority. |
| 2. The continuation regex doesn't establish fabrication | BLOCKING | Renamed "serialized-continuation markers". Added a code check for tool output absent from the transcript: 43/58. The zero for GLM and Gemini is stated as format-specific. Not hand-adjudicated. |
| 3. Prediction (c) wrongly marked as held; other calls need qualifying | BLOCKING | (c) → not demonstrated; the belief split described as automatic flags; test-edit → not evaluable as planned; (b) conditional on luna. |
| 4. File names matched inside longer names | MINOR | Name-boundary matching plus a test; all aggregates regenerated. |
| 5. Visibility credited from unrelated occurrences | MINOR | The anchor must be in its source message as rendered; test added; regenerated. |
| 6. "Latest" meant "latest classifiable" | MINOR | An unclassifiable latest run makes that kind's state unknown (unscored). Checklist texts were not regenerated (that would mean re-auditing): for 9 suite histories the checklist's "most recent run" is the latest classifiable one. |
| 7. Throughput fix incomplete; the report overstated it | MINOR | Report corrected (above); the code improvements are left for the next batch user. |
| 8. The bytes/token ratio is an estimate, not a bound | MINOR | Docstring reworded, input validated, test added. |
