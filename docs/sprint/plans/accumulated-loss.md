# Do accumulated compaction losses change what a coding agent does?

Draft v0.3, 24 Oct 2026: astra r1 and r2 fixes folded in, with Jacob's agreement (`logs/reviews/20261024-accumulated-loss-plan-r1.md`). Fast path first: prove the signal on the 6 runs, then build out the other task families, agent models and compaction methods. It follows the SWE-Interact pilot (`docs/sprint/plans/swe-interact-pilot.md`, v0.4), whose go line was missed, and Jacob's proposal to continue runs and fork on accumulated losses.

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
   - **Summaries:** the live pi summary, plus 5 replays written by Haiku 5.5 on Max: pi's prompt (an anchor for model vs prompt, never a fork source), Claude Code's prompt, Codex, OpenCode and Antigravity-style.
   - **Grading:** each summary is graded against every requirement the maintainer has disclosed so far (Experiment 1's judge with the second look). This is the Q1 data: a loss curve per summarizer and compaction number.
4. **Fork rule, fixed before any continued run (v0.2):** fork at the **first** compaction point where one summary has **at least 1 dropped requirement that passes the checks and has a concrete pending governed decision**, evidenced at the snapshot: an unimplemented or still-editable spot the requirement governs.
   - **Source order:** live pi, then the Claude Code prompt, Codex, OpenCode, Antigravity-style.
   - **Candidate order:** within a source, by the earliest source reply, then by position in the reply.
   - **Losses inherited from earlier compactions** count, and are flagged.
   - **Exclusions:** only fails backed by evidence code can verify. Everything unresolved goes to Jacob's card.
   - **One primary fork per task.** Repeats per task are not independent evidence.
   - **A non-pi source** is reported as a mixed setup (DeepSeek resumes from a Haiku-written summary in another harness's format), kept separate from live-sourced cases.
   - **Selection state machine (as built, `fork_v2/selection.json`, `selection_log.jsonl`):**
     - The run pauses at each qualifying compaction, screens inline, and continues only if nothing is carded or every candidate there is exhausted.
     - After a rejection, the next candidate goes in the order above: same source first.
     - At most 2 primary cards per compaction, plus at most 2 bundle members beside them (decided 24 Oct, ~4:05 PM ET, before any continuation output was read). 12 cards in all, counting every card. An unresolved card counts as rejected.
     - The pilot's six checks keep their frozen prompts and output mapping. Jacob's card confirms retention, applicability with the pending decision, and the act within the window. Recoverability and reference stay code checks, and an unresolved one shows on the card.
     - **Superseded from the pilot:** fresh-only eligibility. Inherited losses are eligible and flagged.
   - **Sealing:** a base run never continues past a qualifying compaction until its screen has finished and either nothing was carded or the freeze is final. Later boundaries that already exist (boa b2) are sealed.
5. **Arms, from the same snapshot, 2 repeats each:**
   - **Primary, one item:** Repair (that requirement, in the maintainer's own words) vs Matched control (one irrelevant item from the same history, matched in length).
   - **Pre-registered secondary, bundle:** where at least 2 qualify from the same source at that boundary, also repair-all vs matched-all. This is reported as the effect of restoring the bundle, not of any single item.
6. **Readouts:**
   - **Primary:** the governed act, scored until the agent's next `ask_user`, a submission or 60 calls. The simulator can't re-raise the loss inside this window.
   - **Recorded in every arm, never used to exclude:** whether the agent reached the decision, re-asked, or progressed.
   - **Secondary, on a few cases:** the simulator answers normally. Measure re-corrections, extra cost and the final verifier reward.
7. **Jacob's card:** a plain paragraph of what happened, then 3 yes/no questions (Really lost? Still matters? Would we see the agent act on it within the window?). Code does the rest of the checks, and its evidence sits under the card.

## Phases

| Phase | What | Cost | Time | Go line |
|---|---|---|---|---|
| A. Build (minimal) | Resume from saved state; first-actionable-loss trigger; sealing; new/inherited bookkeeping; 3-question card | $0 | ~6–10 h (astra) | Tests, astra code review, stub smoke |
| B. Continue | The 6 pilot runs to completion, every compaction screened | ~$3–6 | overnight | — |
| C. Scale | The remaining ~18 DeepSWE tasks to completion. Then, if the signal holds: the other 50 SWE-Interact tasks, other agent models and other live compaction methods | ~$40–60 for DeepSWE | ~1 day | Starts if B gives ≥ 2 distinct tasks with completed valid comparisons |
| D. Reruns | 4 primaries per fork; secondaries on a few | ~$1–2 per fork | hours | Target ≥ 10 forks across ≥ 6 tasks |

**Headline and reading rule (fixed now):** the act rate is the share of continuations doing what the requirement says. The headline is repair minus control, averaged within task, then across tasks, with a 95% task-clustered bootstrap interval.
- **Outcome per continuation, with a frozen per-act scorer:** complied, contradicted, or not reached.
  - **Act rate** = complied / reached. The **reached rate** is reported beside it, in every arm.
  - Not reached never counts as compliance.
- **Two separate conclusions, both fixed now:**
  - **(1) Is a signed effect detected?** Does the interval exclude 0? A negative effect is reported explicitly as harm.
  - **(2) Is a large benefit ruled out?** Is the interval entirely below +25 points? That reads as "rules out the prespecified large benefit". +25 is a pragmatic threshold for a large immediate effect, not an empirically established minimum.
  - An interval like [+5, +20] does both.
- **Sample size, illustrative only:** assuming independent binary continuations, 2 per arm and rates near 0.5, the half-width is about 31 points at 10 tasks and about 22 at 20. Task heterogeneity and few-cluster bootstrap behaviour can make it worse; nothing guarantees ruling out +25.
- **Task reward** from the secondary readout is exploratory.

**Q1 bookkeeping:**
- At each compaction, per source, every lost requirement is marked **new** (present in the previous live summary's context) or **inherited** (already absent).
- The curves are labelled "alternative summarizers on pi-compressed histories" from compaction 2 on.
- **Model vs prompt:** live pi (DeepSeek) is compared with the Haiku replay of pi's prompt.
- **Later:** recursive replay chains on fixed transcripts (each summarizer on its own previous summary, $0 on Max) give true per-summarizer trajectories.
- **Hand checks:** kept and lost calls are hand-checked across sources and compaction depths.

## Priors (rough; the last plan's yield prior was too high: 0.5 losses per boundary expected, 0.08 seen)

- 50%: B gives at least 2 forks, counted as distinct tasks with completed valid comparisons.
- astra r1's estimates for v0.1: 20–30% for at least 10 valid forks across at least 6 tasks by Friday; 15–25% for an interpretable Q2 headline (40–50% given that sample).
- 35%: at least 10 forks by Wed 28 Oct.
- 45%: repair visibly changes the governed acts.

## Validity

- **The simulator is a repair channel:** the primary window closes before its next reply, and its recovery is measured as an outcome.
- **The fork rule is fixed in advance** and reads only pre-boundary history. Outcomes stay unread until every card is labelled and every case frozen.
- **Judge error:** Jacob checks every forked loss, so it doesn't touch the forked cases. The Q1 rates carry Haiku's precision (about 0.69 on 85 labelled cards; Sonnet 5.5 was no better, `results/sprint/judge_compare/summary.md`).
- **Bundled repair** measures the accumulated loss, not single items; per-item acts are also reported.
- **Scope:** claims hold under this simulator, agent and compaction setup. Experiment 1 stays the naturalistic Q1 result.

## Budget and time

About $70–110 (astra r1) inside Experiment 3's stage (SPRINT-S3-Q2, $270; the pilot tag has spent $3.57). Jacob: about 5 minutes per fork card, about 1 hour in all. The final presentation is due Fri 30 Oct, 10 pm.

## Deviations

- **24 Oct, Jacob: sequential compactions can't mix methods.** A replay at compaction k>1 is Haiku writing one summary in another harness's format on top of pi's earlier summaries. That tests something different from that method compacting repeatedly, so it isn't a valid point on any method's chain. It applies everywhere, before any further card or continuation output is read:
  - **Fork sources:** at k=1, unchanged: live pi, then the Claude Code prompt, Codex, OpenCode, Antigravity-style. At k>1, only the run's own summary: live pi now, and a native arm's own summary when native runs are forked. Other sources' candidates are recorded as `excluded:source:mixed_chain`, and the funnel counts them. The screen's hash changes, so every boundary is rescreened.
  - **Rejected boundaries at k>1** (boa b2, vitest b2, helm a2 b3, kombu a2 b2) are rescreened from live only. testem b1 (Codex-style, k=1) is unaffected; it is kept as a pre-B/C case, reported apart.
  - **Constraint decay (D):** the decay table uses same-method chains only: live pi now, native Claude Code, Codex and Antigravity later. The replay sources appear at k=1 only, as a comparison of summarizers on identical history. Replay rows at k>1 are neither graded nor shown.

## Review history

- **astra r1** (`logs/reviews/20261024-accumulated-loss-plan-r1.md`). Its verdict: for Q2, probably the strongest next step after fixes; for Q1, Experiment 1 stays stronger. BLOCKING findings and how v0.2 handles them:
  - **The ≥2-loss trigger:** replaced by the first actionable loss.
  - **Late forks:** a pending decision is now required.
  - **The fork rule wasn't frozen:** source and candidate order, inheritance, exclusions and sealing are now fixed.
  - **Repeated forks per task:** one primary per task.
  - **No informative-null rule:** a smallest effect of interest of +25 points is now fixed.
  - **The Q1 curves aren't independent:** new and inherited losses are separated, the curves relabelled, and model vs prompt compared.
  - **Bundled repair:** now a secondary, framed as a package effect.
  - Jacob agreed to all of these on 24 Oct and chose the fast path.
- **astra r2** (`logs/reviews/20261024-accumulated-loss-plan-r2.md`): 4 of the 7 r1 findings resolved, 3 partly. New BLOCKING findings, fixed in v0.3 and the build (astra build r1–r3):
  - the selection state machine and sealing from screening onward;
  - the two-conclusion reading rule;
  - a compliance scorer with "not reached";
  - an illustrative-only sample-size note;
  - the pi anchor added to the replay list.
  - Its updated estimates: 35% for at least 10 valid primary forks across at least 6 tasks by Friday; 25% for an interpretable Q2 headline (about 55% given that sample).
- **24 Oct, Jacob:** the fast path first. Native-harness arms (Claude Code feasible for Q1 and Q2; `logs/s2/native_harness_feasibility.md`) and the SWE-bench Pro and Refactoring families (being integrated and leak-validated on the desktop) come after phase B shows signal.
- **Phase B launch:** 5 runs resume from their kept boundaries; kombu reruns as a2, its one infrastructure rerun after the kept-image incident.
