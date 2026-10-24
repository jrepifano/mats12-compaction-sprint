# Do accumulated compaction losses change what a coding agent does?

Draft v0.1, 24 Oct 2026. Not yet reviewed. It follows the SWE-Interact pilot (`docs/sprint/plans/swe-interact-pilot.md`, v0.4), whose go line was missed, and Jacob's proposal to continue runs and fork on accumulated losses.

## The question

From the sprint proposal (`docs/sprint/compaction-proposal.md`): **Q1**, what do compaction summaries drop, misstate or invent; **Q2**, does repairing a natural loss change what the agent does next, by counterfactual reruns from the compaction point.

Here:
- **Q1.** In a long interactive coding session, where a user adds requirements and corrections over time, do repeated compactions lose those requirements? How does the loss build from one compaction to the next, per summarizer?
- **Q2.** Restore the lost requirements at the compaction point and rerun. Does the agent act differently than with irrelevant text of the same length? Does task progress change?

## What we've learned so far

| Attempt | Result | Why it falls short for Q2 |
|---|---|---|
| Experiment 1, offline real sessions (`docs/sprint/S1/report.md`; SWE-chat extension `results/sprint/S1/swechat_ext/summary.md`) | SWE-chat: about 20% [17, 24] of items lost (judge rate; precision on SWE-chat 12/19 = 0.63); about a third to a half of rule-bearing compactions lose a rule | No environment, so no reruns |
| Experiment 2, 24 single-task live DeepSWE runs (`logs/SPRINT-S2-report.md`; `results/sprint/S2/readout.md`) | Crisp planted rules mostly kept (README channel lost at 5/8); 18/375 items truly lost; 2/24 usable losses, both recoverable from the repo | State lives in the repo; little to lose |
| Chained DeepSWE plan (`docs/sprint/plans/chained-pilot.md`), not run | — | We would write the between-task content ourselves |
| SWE-Interact pilot, first compaction only (`docs/sprint/plans/swe-interact-pilot.md`; run log) | Live pi summary kept 39/39 disclosed requirements; Haiku replays lost 0–4/37 (Codex and OpenCode, all in one task); 2 weak candidates; go line missed (K ≤ 2 of 4) | Every run stopped at its first compaction (69–163 calls, against 180–425 steps in the paper), before most reviews and before any summary of a summary |

## Design

1. **Environment.**
   - **Tasks:** SWE-Interact's DeepSWE tasks (25, 1 held out). A $0 feasibility check of the other 50 (SWE-bench Pro, SWE Atlas Refactoring) is running.
   - **Simulator:** the released GPT-5.5 maintainer persona, through OpenRouter pinned to OpenAI so its repo tools work.
   - **Agent:** DeepSeek V4.1 Flash in our harness, pi's compaction, 150k tokens.
2. **Run every session to completion:** ship-it, $8, 600 calls or 3 h. Compaction repeats with pi's update prompt, and every compaction point is snapshotted (workspace, agent context, simulator state). The 6 pilot runs resume from their saved first boundary (build about 4 h; `s2-feas`'s estimate).
3. **At every compaction point:**
   - **Summaries:** the live pi summary, plus 4 replays (Claude Code's prompt, Codex, OpenCode, Antigravity-style), all written by Haiku 5.5 on Max.
   - **Grading:** each summary is graded against every requirement the maintainer has disclosed so far (Experiment 1's judge with the second look). This is the Q1 data: a loss curve per summarizer and compaction number.
4. **Fork rule, fixed before any continued run:** fork at the first compaction point where **one summary drops at least 2 verified requirements that still apply** (the pilot's six checks, done by code plus a short card for Jacob).
   - Otherwise continue to the next compaction.
   - Report the source summary. A non-pi source is flagged as a mixed setup: DeepSeek resumes from a summary written by Haiku in another harness's format.
5. **Arms, from the same snapshot, 2 repeats each:**
   - **Repair:** every dropped requirement, restored in the maintainer's own words.
   - **Matched control:** the same number of irrelevant items from the same history, matched in total length.
6. **Readouts:**
   - **Primary:** each restored requirement's governed act, plus an aggregate, scored until the agent's next `ask_user`, a submission or 60 calls. The simulator can't re-raise the loss inside this window.
   - **Secondary, on a few cases:** the simulator answers normally. Measure re-corrections, extra cost and the final verifier reward.
7. **Jacob's card:** a plain paragraph of what happened, then 3 yes/no questions (Really lost? Still matters? Would we see the agent act on it within the window?). Code does the rest of the checks, and its evidence sits under the card.

## Phases

| Phase | What | Cost | Time | Go line |
|---|---|---|---|---|
| A. Build | Resume from saved state; accumulated-loss fork rule; simple card page | $0 | ~4 h | Tests, astra code review, stub smoke |
| B. Continue | The 6 pilot runs to completion, every compaction screened | ~$3–6 | overnight | — |
| C. Scale | The remaining ~18 DeepSWE tasks to completion (plus the other 50 if feasible) | ~$40–60 | ~1 day | Starts if B gives ≥ 2 forks |
| D. Reruns | 4 primaries per fork; secondaries on a few | ~$1–2 per fork | hours | Target ≥ 10 forks across ≥ 6 tasks |

**Headline:** the act rate, repair minus control, with a 95% interval clustered by task; the same for task reward. Q1 is reported as loss curves.

## Priors (rough; the last plan's yield prior was too high: 0.5 losses per boundary expected, 0.08 seen)

- 50%: B gives at least 2 forks.
- 35%: at least 10 forks by Wed 28 Oct.
- 45%: repair visibly changes the governed acts.

## Validity

- **The simulator is a repair channel:** the primary window closes before its next reply, and its recovery is measured as an outcome.
- **The fork rule is fixed in advance** and reads only pre-boundary history. Outcomes stay unread until every card is labelled and every case frozen.
- **Judge error:** Jacob checks every forked loss, so it doesn't touch the forked cases. The Q1 rates carry Haiku's precision (about 0.69 on 85 labelled cards; Sonnet 5.5 was no better, `results/sprint/judge_compare/summary.md`).
- **Bundled repair** measures the accumulated loss, not single items; per-item acts are also reported.
- **Scope:** claims hold under this simulator, agent and compaction setup. Experiment 1 stays the naturalistic Q1 result.

## Budget and time

About $50–70 inside Experiment 3's stage (SPRINT-S3-Q2, $270; the pilot tag has spent $3.57). Jacob: about 5 minutes per fork card, about 1 hour in all. The final presentation is due Fri 30 Oct, 10 pm.
