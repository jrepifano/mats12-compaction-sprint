# SWE-chat oddities: plan (28 Oct, ~8:25 PM ET; revised ~8:45 PM after astra r1–r3)

Brief: `swechat_odd_brief.md` (Jacob's go, about 8:30 PM ET). $0: Claude on Max only. Branch `sprint/swechat-odd`.

## Question

Neel: "study compaction in the wild, e.g. on SWE-chat, and see if you can find any interesting observations or anomalies." Loss mining found that 14 of the 21 SWE-chat cases Opus flagged as odd were summaries claiming work was done, fixed or verified when it apparently wasn't. Nobody has checked those cases by hand. This plan asks two things: how often SWE-chat summaries make a false completion claim, and whether Jacob agrees with the calls.

## 1. Labelling page (done 8:21 PM ET)

Each card shows the source, model and harness, the claim, the history around the item, the last 4 messages, the full summary and the kept messages. Labels are claim false / true / can't tell, plus a note. The answer key is in `data/raw/sprint/swechat_odd/key_A.private.json` (gitignored).

**Reuse:** the 26 Oct odd-summary audit page (LcEyLQKJY5WrfQvF9ErxFi) has no saved labels, so there is nothing to reuse. 6 of the 21 cases were also cards on it, under a different question.

## 2. Scan of all 928 eligible SWE-chat compactions

**Population:** `s1_data` eligibility over `swe-chat-v1`, 928 compactions. All are claude-code apart from 7 on opencode. By model: Opus 4.6 640, Opus 4.5 102, Sonnet 4.6 92, Sonnet 4.5 59, other 35.

**Unit (astra r1 #3):** a *keyword-matched completion line*: a summary line the code pre-filter tags and Opus calls a completion claim. A line with several assertions is false if any one of them is contradicted. Lines the pre-filter missed are asked for separately, as a recall check (below).

**Code pre-filter** (`candidates`):
- **Candidate lines:** summary lines that match `done | fixed | tests pass… | all tests | verified | complete(d/s)` or ✅. On 120 compactions: median 9 candidates, maximum 32, and 1 compaction with none.
- **What it cuts:** almost every summary has a candidate, so the filter cuts claims and the input size, not the number of calls. A compaction with no candidate still gets a call, for the untagged lines only (astra r2).
- **How Opus sees them:** each candidate is tagged `[C<n>]` inline in the full summary, so it can see the section the line sits under (e.g. a "Pending" list).
- **Lines the pre-filter missed:** Opus also lists up to 5 untagged lines that make completion claims, with verdicts. Code keeps only real, untagged, non-duplicate summary lines. The report gives their count and the per-compaction rate including them, as a sensitivity: a capped discovery of extra lines, not a measured recall.

**History digest** (first pass, so the full ~300k-character history doesn't go in):
- **The latest part:** the last messages before the compaction, up to 30k characters, with tool outputs cut at 2,000 characters and tool-call arguments at 1,500.
- **Everything earlier:**
  - user messages up to 1,500 characters;
  - the agent's text up to 500 characters, with its tool calls shown as name plus the first 120 characters of their arguments;
  - tool results reduced to their first 4 and last 4 pass/fail/error lines, or else their first 150 characters.
- **Size cap:** the earlier part is capped at 40k characters, cut from the middle. The prompt says which parts are abridged, and to use `cant_tell` when evidence may sit in a cut part.
- **Size in practice:** median ~79k characters in all, about 74% less than the full history.

**First pass:**
- **Route:** Opus 5.5 on Max (`claude -p`, `MaxBackend`, effort low), 3 in flight, 12 compactions per backend run (each run sends one tiny route canary first), cached, resumable.
- **Per tagged line, the schema asks:**
  - whether the line is a completion claim;
  - a verdict: `false` / `true` / `cant_tell`;
  - for false, what kind: `not_done`, `not_run`, `failed`, `user_says_not`, `other`;
  - a verbatim quote and a short reason.

**Recheck (astra r1 #4):**
- **What it is:** every compaction with a first-pass false gets one tool-using call: Opus 5.5 with Grep and Read over the full rendered history (history.txt), up to 12 turns.
- **Its job:** look for the contradiction and for anything later that would make the line true after all.
- **Final verdict:** the recheck's, when its quote is found in the history; an unconfirmed false becomes `cant_tell`.
- **Matching and pending lines:** each recheck is tied to its first-pass attempt id (astra r2). While any recheck is pending, the summary is marked PROVISIONAL and gives both bounds: pending lines as can't tell, and as false.
- **Reporting:** the first-pass and final rates are both reported.

**What counts as false:** the history before the compaction contradicts the line. The work isn't done, the check it reports never ran, the last run failed, or the user said it doesn't work and nothing later fixed it. A claim that was true when made but went unchecked after later edits is `cant_tell`, not false.

**Completeness and resume (astra r1 #1, #3):**
- **Attempts:** each attempt is recorded with an attempt id and the prompt fingerprint.
- **What counts:** only complete attempts, where every tagged line is answered and every claim has a verdict.
- **Retries:** within the same invocation, an incomplete answer is retried once under a new key, and errors are retried once. Still unsettled, a compaction is reported as unresolved and left out of the denominators.

**Usage pacing (astra r1 #2):**
- **Before each chunk:** read the newest Max reading (five-hour and weekly).
- **Pause:** at five-hour ≥ 0.75, until the window resets, then send a one-compaction chunk to refresh the reading.
- **Fail closed (astra r2):**
  - a reading older than 30 min gets a one-compaction refresh, never a full chunk;
  - a reading missing either window stops the scan;
  - no reading at all is allowed once, to bootstrap;
  - a chunk that sent calls but left no newer reading stops the scan.
- **Stop:** at weekly ≥ 0.85 (absolute), or once the scan has added 0.20 to the weekly window since its baseline. The baseline is saved in `state.json` and survives restarts; a new weekly window resets it. No reading once a baseline exists means stop.
- **Order and sample size:** compactions go out in a seeded random order (`swechat-odd:scan:20261028`). After the 6-compaction pilot, the target prefix N is fixed in `state.json` from the measured usage per call (all 928 if the projection fits the weekly allowance). If a stop still comes first, the report says so and gives the n reached.
- **Start:** after the five-hour reset at about 11:52 PM ET.

**Report** (`results/sprint/swechat_odd/scan_summary.md` plus `scan_manifest.json`, with population and prefix hashes, code and row hashes and pacing state; row files gitignored):
- **Headline:** labelled "Opus 5.5's read, not hand-checked", with route and effort.
- **Per compaction:** the share with at least 1 false completion line, over the compactions settled.
- **Per line:** the detected-false share (false over all completion lines, `cant_tell` included), and false / (true + false).
- **Intervals:** Wilson 95% on each, plus a session-cluster bootstrap.
- **Splits:** by model (Opus 4.6, Opus 4.5, Sonnet 4.6, Sonnet 4.5, other) and by harness where n ≥ 30, and by false kind.
- **Examples:** seeded random false lines with source, model, harness and quote (mistakes.md #15).

**Hand review (Jacob's later instruction, ~8:45 PM ET via the Research Advisor; supersedes "no hand labels" at ~8:40 PM).** After the scan, one private review page with a db (`scripts/swechat_review.py`):
- **Every card:** the full pre-compaction history, as its own published file loaded when the card opens. In-page search highlights hits and has a hit count and next/previous. Each card also has the claim, labels, source, model and harness, and a "jump to the cited message" button, which Deck B has for both arms so it reveals no verdict.
- **Deck A, examples:** 6–10 striking candidates for the slides, each with Claude's reason. The pool is final-false scan lines plus the loss-mining cases for invented user actions and flipped user words. Labels: claim false / true / can't tell, use in deck yes / no, and a note. Jacob hand-checks every example that goes in the deck. Deck A never uses a compaction that is in Deck B.
- **Deck B, rate check:** seeded (`swechat-odd:review-deckB:20261029`).
  - **Sample:** 20 lines drawn uniformly from the final-false lines and 10 from the final-true lines, shuffled, blind. Opus's verdicts, reasons and quotes go in a gitignored key.
  - **Labels:** false / true / can't tell, plus a note.
  - **Readout:** Opus's precision on false as k/20 with a Wilson interval, next to the scan rate, plus agreement on the 10 true lines.
- **Fallback:** the scan readout stands on its own, as "Opus 5.5's read, not hand-checked". Jacob's result, if he labels, goes beside it as a separate line.

**Examples** (`examples`): 6 final-false lines drawn uniformly at random (`swechat-odd:examples:20261028`), each with source, model, harness, the line, the recheck's history quote and its reason.

## 3. Other anomaly patterns

Count the other patterns in the 21 odd cases from their Opus notes and quotes: invented resolutions or user actions, flipped user meanings, user messages dropped from "all user messages", and wrong numbers. Give counts and quotes, say which are worth a slide, and label it all as Opus's reading until Jacob's Deck A labels are in.

## Decision lines (written before the scan)

- **The slide line** is the per-compaction rate after the recheck, labelled "Opus 5.5's read, not hand-checked".
- **Hedge:** if the recheck overturns more than half of the first-pass false lines, the slide also says the first pass over-called, and gives both numbers.
- **If Jacob labels Deck B:**
  - **At least 14/20 of Opus's false calls confirmed:** the rate stands, with "Jacob confirmed k/20" beside it.
  - **Fewer than 14/20:** the slide leads with Jacob's confirmed examples, and gives the rate only as Opus's read with its measured precision.

## Rules followed

- astra review before committing code.
- Deck A's answer key stays out of the page, which is checked by code before every publish.
- Seeded draws throughout.
- Examples give source, model and harness.
- Commits go on this branch only.

## Review log (gpt-6-astra, `scripts/codex_review.sh`, medium)

| Round | File | Verdict | What changed |
|---|---|---|---|
| r1 | `logs/reviews/20261028-swechat-odd-r1.md` | FAIL, 6 BLOCKING | Unit narrowed to keyword-matched lines, with untagged-line discovery. Tool-using recheck over the full history. Completeness status, attempt ids and fingerprint. Persisted weekly baseline. Decoded leak check. Deck B redesigned, then dropped by Jacob. |
| r2 | `logs/reviews/20261028-swechat-odd-r2.md` | FAIL, 3 BLOCKING | Pacing fails closed (bootstrap once, freshness, stop when no newer reading). Rechecks keyed by first-pass attempt. PROVISIONAL bounds while rechecks are pending. Retries within one invocation. Untagged lines validated. Deck B ingestion removed. |
| r3 | `logs/reviews/20261028-swechat-odd-r3.md` | FAIL, 2 BLOCKING | Both fixed after the round (the round cap is 3, so there is no r4); tests added for each. |

**r3's two findings, fixed after the round:**
- **A failed recheck call finalised its line as can't tell.** It now stays pending and is retried, and the summary stays PROVISIONAL meanwhile (test `test_parse_recheck_failed_call_stays_pending`).
- **The pacer didn't re-read after sleeping, and accepted a reading taken before a five-hour reset that had since passed.** It now re-reads and re-checks every limit after a sleep, and a pre-reset reading gets a one-compaction refresh (tests `test_pace_pauses_until_reset_then_rechecks_limits`, `test_pace_after_a_passed_reset_refreshes`).

**Open, accepted as limitations:**
- The digest can still miss evidence. This is mitigated by the recheck, which covers false calls only. A true or can't-tell call made on the digest isn't rechecked, so false lines can be missed, and the rate is a floor on what Opus would find with the full history.
- No human precision: Jacob chose Opus's read.

## Run log (UTC)

- **03:55Z, pilot (6 compactions).** All 6 complete. 49 completion lines: 0 false, 42 true, 7 can't tell. Five-hour usage 0.10 → 0.12; weekly 0.31.
- **03:57Z, first pass started** with target 928.
- **04:20Z, first pass paused** at five-hour 0.75, with 390 of 928 compactions done.
  - **Weekly window:** 0.31 → 0.42.
  - **Interim counts (first pass, tagged lines):** 59 false, 2,118 true, 538 can't tell. 37 compactions have at least one false line; 42 when untagged lines are included.
- **04:21Z, design change: stopped the sleeping first pass, and start the recheck first.**
  - **Why:** at ~0.0003 weekly per compaction, the first pass alone would reach the +0.20 weekly stop at about 700 compactions, leaving no budget for the recheck.
  - **New order:** recheck the 42 compactions first (it waits for the 08:50Z reset), measure its cost, then extend the seeded prefix with what is left of the +0.20, keeping a reserve for the new lines' recheck.
  - **Effect on the sample:** the prefix stays a seeded random sample, and the report gives the n reached.
  - **Not changed:** the 0.20 cap.
- **08:52–08:56Z, recheck of the 42 compactions (67 lines).**
  - **Verdicts:** 53 false lines confirmed with a found quote, 10 overturned to true, 3 can't tell, and 1 false whose quote wasn't found (so it is finalised as can't tell).
  - **Cost:** weekly 0.42 → 0.43, five-hour 0.00 → 0.01; about 0.06 notional per call, against 0.33 for a first-pass call.
- **08:57Z, first pass extended to target 620.**
  - **Budget left:** 0.08 of the +0.20, at ~0.0003 weekly per compaction.
  - **Projection:** 230 more compactions, about +0.07, leaving ~0.01 for their recheck.
- **08:56–09:12Z, first pass to 620.**
  - **Usage:** weekly 0.43 → 0.46 for 230 compactions (~0.00013 each); five-hour 0.01 → 0.18.
  - **Why this matters:** the 0.11 counted for the first 384 compactions included other sessions' use.
- **09:13Z, chain started:** recheck the new false lines, extend the first pass to 928, recheck again.
  - **Projection:** about +0.05 weekly in all, ending near 0.51, under the +0.20 stop.
  - **If the stop comes first:** the report gives the n reached, and the bounds stay PROVISIONAL for any lines not rechecked.
- **09:15–09:38Z, chain finished.**
  - **Steps:** recheck (37 compactions), first pass to 928, recheck (41 compactions).
  - **Result:** all 928 compactions settled; every label-ok first-pass false rechecked.
  - **Usage at the end:** weekly 0.49 (+0.18 over the 0.31 baseline), five-hour 0.42.
  - **Cost:** 93 ledger rows, all $0 on the Max route.
- **Headline** (`results/sprint/swechat_odd/scan_summary.md`; Opus 5.5's read, not hand-checked):
  - **Per compaction:** 93/928 = 10.0% [8.3, 12.1] have at least one false completion line; 11.1% on the first pass.
  - **Per line:** 152/6,994 = 2.2% [1.9, 2.5].
  - **Recheck:** confirmed 179 of 199 first-pass false lines; 15 became true and 5 can't tell. Under the decision line (overturned > 1/2), no over-call hedge is needed.
- **Fixes after the run (astra r1 PASS on the banner fix; r2 on the combined diff: no BLOCKING, one MINOR, OSC sequences, fixed with a test):**
  - **Summary banner:** it counted 7 rejected untagged lines (label_ok False) as pending, so it showed PROVISIONAL. No rate changes.
  - **Examples:** `cmd_examples` used the old recheck key, so the quote and reason were empty.
  - **Decks:** Deck A and Deck B are now disjoint by session, not just by compaction. Two Deck A picks were swapped:
    - e5367972:0 → 7a40230e:0 C8;
    - loss-mining 4a364c97:1 → d6e9f817:0 C6.
  - **History text:** the builder now strips ANSI escapes and control bytes and maps U+FFFD to "?", because the publisher refused both.
  - **Deck A:** 10 cards (5 scan lines, 5 loss-mining cases).
  - **Deck B:** 30 cards, seeded and blind: 20 final false from a pool of 152, 10 final true from a pool of 5,495.
  - **Leak check:** in code and by a separate grep, both clean.
