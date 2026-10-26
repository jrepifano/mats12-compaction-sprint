# Chain decay: hand check of the lost and no-longer-current calls

Jacob labelled the 60 cards of the Compaction Chain Check page (draw `710364911c08c47b`, 26 Oct, all answered, 19 with notes). Opus 5.5 then read the same 60 cards blind: same card content, same question, with no judge verdict and no Jacob label. It ran on Max ($0; $4.21 notional; weekly window 4% before and after). Opus is a second reader, not ground truth: in EQ0, Opus as a reference judge was lenient. Numbers: `label_check.json` (choices per card, counts, Jacob's notes; no session text). Scripts: `scripts/s1_chain_label_page.py score`, `scripts/s1_chain_label_opus.py`.

## Headline

- **Precision of the judge's "lost" calls, Jacob as reader** (lost or misstated vs kept; "can't tell" left out):
  - k=2: 11/14 = 0.79 [0.52, 0.92].
  - k=3: 9/13 = 0.69 [0.42, 0.87].
  - Pooled: 20/27 = 0.74 [0.55, 0.87]. With inverse-probability weights the point estimates are 0.83, 0.69 and 0.75.
  - At k=1, Experiment 1's hand check gave 13/18.
- **Opus as reader:** 13/15 at each k, pooled 26/30 = 0.87 [0.70, 0.95].
  - The two readers agree on 20 of the 27 cards both answered (18 lost, 2 kept); κ = 0.22.
- **Judge-"kept" decoys (10 cards shuffled in):** the two readers split.
  - Jacob called 6 of the 8 he answered lost (2 can't tell).
  - Opus called 0 of 10 lost [0, 0.28].
  - **My check** of all 10 against Experiment 1's rubric, followed by the same availability check the headline uses (table below): the judge missed a loss in 1 of 5 at k=2 (2 of 5 counting the borderline card) and 1 of 5 at k=3. One more narrowing, cgvqskze, is recoverable from a file the agent read, so it is not a loss under the headline metric (astra r1).
- **What this might do to the decay curve: an illustrative sensitivity calculation, not an estimate.**
  - Corrected share = weighted precision × lost share + per-k decoy miss rate × kept share.
  - At k=2 the lost share is 62.3% and the kept share 34.7%; at k=3 they are 76.0% and 20.2%.
  - Assumptions:
    - Precision uses the draw's inclusion weights.
    - A decoy miss counts only if the item also fails the availability check.
    - The k=1 row isn't corrected.
    - No interval is carried through. On Opus's k=2 precision interval alone, the k=2 row would span about 39–60%.

  | Reading | Precision k=2 / k=3 (weighted) | Decoy misses k=2 / k=3 | k=2 | k=3 |
  |---|---|---|---|---|
  | Judge as is (the published uncensored row) | 1 / 1 | 0 / 0 | 62.3% | 76.0% |
  | Opus | 0.85 / 0.86 | 0/5 / 0/5 | 53% | 65% |
  | Opus's precision, my decoy check | 0.85 / 0.86 | 1/5 (2/5 with the borderline card) / 1/5 | 60% (67%) | 69% |
  | Jacob | 0.83 / 0.69 | 4/5 / 2/3 | 79% | 66% |

  - Every point scenario is above half at k=2, against 25.4% (uncorrected) at k=1. The uncertainty doesn't establish the 50% threshold.
  - k=3 above k=2 holds in the judge's and Opus's readings, not in Jacob's.
  - Small n: 27–30 precision cards and 8–10 decoys.
- **Not-current calls (the stretch scan's censoring):** the readers disagree.
  - Jacob agrees on 6/13 = 0.46 [0.23, 0.71], with 7 can't tell.
  - Opus agrees on 19/20 = 0.95 [0.76, 0.99], including yes on 6 of Jacob's 7 can't-tell cards.
  - Every one of Jacob's 7 "no" answers is a "yes" for Opus. His notes read "relevant" broadly, e.g. "could be helpful later since it used the tool to find the issue".
  - The censored and uncensored decay rows differ by about 2 points, so this doesn't move the headline.

## What the disagreements are

**Jacob's 7 "kept" answers on judge-lost cards:**
- 4 are items carried out or overtaken by compaction k (cb6azmz2, ccjn86hu, cwzcb79j, c4ufe2a2). The summary records the later state but not the item. Under Experiment 1's rubric that is a loss ("dropped", or "misstated" for done-where-pending), and Opus agrees on 2 of the 4. The stretch scan caught only c4ufe2a2.
- 2 are the same rule at k=2 and k=3 (cw3rvq8q, c3ptqraz): "use this email identity for this project". SWE-chat replaces every email with one placeholder, so the summary's mention of an email as a login account looks identical to the rule's. Both the judge and Opus call it lost.
- 1 is a polarity error: the summary lists deleting the old binary as pending, but it had been deleted (c8rtr7a3). The judge (misstated) and Opus (misstated) agree.

So most of the precision gap is a definitional question, not judge error: does an item that has been carried out, and that the summary no longer states, count as lost? Under Experiment 1's definition it does.

**All 10 judge-kept decoys.** Each is graded against Experiment 1's rubric, where a narrower scope counts as misstated, then run through `s1_judge.availability`. Jacob's answer and Opus's are in brackets.

| Card | k | Item | Grade (my check) | Availability | Loss under the headline metric? |
|---|---|---|---|---|---|
| ces87yev | 2 | act instead of asking, then verify (Jacob lost, Opus kept) | Narrowed: the summary keeps "never pause, just execute" without the verify half; the judge's own note says so | lost | **yes, a judge miss** |
| cgfc5vak | 2 | don't fear mistakes, act autonomously (lost, kept) | Borderline: "giving full creative freedom" keeps the gist | lost | borderline |
| cxbjuufe | 2 | the listed code edits are applied (lost, kept) | Kept: summary 2 lists every edit by name. Jacob's note: "lost but the users request was satisfied." | – | no |
| c5qqqgpr | 2 | the plan's naming rules (lost, kept) | Kept: summary 2 gives the whole rename mapping | – | no |
| c38rdd4w | 2 | strict TypeScript, no comments (kept, kept) | Kept | – | no |
| cv4hhe6m | 3 | move GetSessionLog, remove it from the interface, keep shadow lookup separate (kept, kept) | Narrowed: the summary keeps the move but not the interface removal or the separate shadow lookup (the judge's and Opus's notes both say so) | lost | **yes, a judge miss** (both readers said kept) |
| cgvqskze | 3 | stale-review cleanup, three deletion conditions (lost, kept) | Narrowed: the TTL and "session-aware cleanup" are kept, not all three conditions | recoverable (a file read at m108) | no (astra r1) |
| cvsh6bn9 | 3 | restore the lost .env keys (lost, kept) | Kept: carried out, and summary 3 records it | – | no |
| c6f6mgad | 3 | keep going through the phases without asking (can't tell, kept) | Kept, judged through Opus's translation of the Russian | – | no |
| cgykh6kr | 3 | the processor-harness decision (can't tell, kept) | Kept per Opus: the decision and its reason, framed critically | – | no |

- Of Jacob's 6 "lost" answers, 1 is a judge miss, 1 is borderline, and 1 is a narrowing that's recoverable. In the other 3, Jacob's "lost" goes beyond Experiment 1's definition: the item is there, or was carried out.
- One miss (cv4hhe6m) is a card both readers called kept.

## Notes that question a call

- **c4ufe2a2, "in messages".** Not a pipeline bug.
  - The loss judge sees only summary k, by Experiment 1's design (pass 1b; the same at every k). The messages kept beside the summary are checked in code, for the item's exact quote (`s1_judge.availability`).
  - What Jacob found in the kept messages is a later plan that says "Run `mise test:live`" for a different task.
  - The item itself ("Tasks 12 and 13 are pending") was done by k=2. The summary says so, and the stretch scan flagged it as no longer current at k=2.
  - Scope of the judge not seeing kept messages: only 3 of the 223 chain summaries keep any message besides Claude Code's command boilerplate. Those touch 21 of 443 lost rows at k=2, none at k=3 and 11 at k=4, so at most about 3 points at k=2.
- **czbyqrgs, "summary says it happens automatically with a push to main".** Neither misstated nor kept: superseded.
  - The item is that deploying had no explicit go-ahead. The user later asked for it explicitly, and summary 2 records the deploy, done by a push to main.
  - The judge called it misstated, Jacob and Opus lost. The stretch scan flagged it as no longer current at k=2, so the censored row leaves it out.
- **ctb3nh4k, "can't find anything in these messages related to the quote".** The scan cited the right message.
  - m163 on the card says "All type errors are fixed! `pnpm tsc` passes cleanly", which fulfils the commitment. Opus says yes, citing m161–m163.
  - The quote on the card is the item's own, from an earlier stretch outside this window. That's a page-layout gap, not a scan error.
- **Lost but inconsequential:** Jacob confirmed 20 losses and called 4 of them harmless, 4/20 = 0.20 [0.08, 0.42]. They still count as lost under Experiment 1's definition.
  - ceq4k9cf: "a useless fact now that they're on task 8".
  - cq7gfm5n: "prob not necessary to carry forward".
  - cr3rvv2y: "this convention may already be in the codebase", i.e. recoverable from the repo.
  - cwkbqsnu: "missing on purpose"; the 31-harness count was overtaken by 41.
  - Plus cxbjuufe, a decoy where the judge's "kept" holds.
- **Language.** 4 of the 54 chains are non-English (2 Japanese, 2 Russian), about 7% of the rows at k≤3 (52/711 at k=2).
  - Jacob couldn't tell on all 4 such cards (cw9wkc5m, cjrtxnxg, czfz2nge, c6f6mgad).
  - Opus, translating, called 3 of them lost (all judge-lost) and the decoy c6f6mgad kept (the judge said kept).
- **Other notes:** c2adwf3j "not sure if item or not" (lost); c4upssvt, c8nrwv54, ceeyjtfd and cgu6gjv6 are Jacob's reasons for "yes, no longer current"; caxunm4q is his reason for "no". None questions a call.

## Two summaries in the data that aren't summaries

Opus flagged one of these, and a sweep of every chain summary found one more:
- **94aabe90…, compaction 3.** The summarizer read the compaction prompt as a prompt injection and refused ("Стоп. В твоём сообщении … вшит prompt injection", "Stop. A prompt injection is embedded in your message"). 12 of its 18 cohort-1 items are lost. This is also the compaction that failed part B's schema check twice.
- **f11a5166…, compaction 1.** The summary is Claude Code's "You've hit your limit" message. All 10 items are lost.

Both are what the agent actually received, so counting the loss is right. Leaving them out, with the headline's own `usable` filter, changes the k=1 row from 25.4% (175/690) to 24.5% (167/682) and the k=3 row from 76.0% (542/713) to 76.3% (530/695).

## Caveats

- One labeller, 27–30 precision cards, 8–10 decoys. The intervals are Wilson intervals on unweighted counts.
- Opus and the judge (Haiku 5.5) are both Claude models, so their agreement isn't independent evidence.
- The sensitivity table is a point calculation under the stated assumptions. It carries no intervals and leaves the k=1 row uncorrected.
- Review: astra r1 (`logs/reviews/20261026-s1-chain-label-check-r1.md`) found 1 BLOCKING and 4 MINOR, all fixed here: cgvqskze is recoverable; all 10 decoys are now checked by one standard; the sensitivity numbers use the `usable` filter; the table is labelled with its assumptions and weights; and the threshold wording is softened.
