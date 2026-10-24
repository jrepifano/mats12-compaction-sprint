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

- **24 Oct, main session: the simulator gate raised from $2.50 to $5.00 per pi base run** (`INTERACT_SIM_GATE_USD`), so runs reach later compactions; fastapi had stopped at the old gate with both compactions exhausted. The pilot tag stays capped at $120, and the main session is told before its total passes $100.

- **24–25 Oct, Jacob and the main session (consult r2): file-backed repair candidates.** Decided after the live chain's item-level audit and its exploratory follow-up had been read. Everything below is **exploratory**.
  - **What counts as lost** (Jacob: "recover-able, not recovered"): a requirement absent from the effective resumed context is lost for candidate eligibility, even when it can still be found in a file the agent isn't prompted to re-read. Such files are its own `/tmp/plan.md`, or a repository AGENTS.md, CLAUDE.md, README or CONTRIBUTING it read once. A file the harness re-attaches after compaction counts as kept.
  - **The stratum is named "contextual omission, retrievable from a file",** not unrecoverable loss.
  - **Recoverability is three-way:**
    - `code`: in the repository's code or content, a branch or a commit message; excluded, as before;
    - `file_not_reattached`: only in such files; eligible, with each file and the last message that read it;
    - `none`: eligible.
  - **Pointer flags, per candidate:** whether the resumed input still names the file, and whether it instructs a re-read (e.g. a kept "Read /tmp/plan.md first"). In the arms, whether the agent actually re-reads it.
  - **Candidate sources:** the maintainer-requirement ledger, plus the live chain audit's lost rule, decision and commitment items at their boundary. Both go through the same gates: the chain rule, near-verbatim, the code checks, Opus, and the authority and consistency gate below.
  - **Authority and consistency gate, decided by Opus on boundary evidence only:**
    - the item must come from the task text, the maintainer or the repository's instructions; an agent-written plan item counts only if it restates such a source or a decision the maintainer agreed;
    - it must not conflict with a rule the agent still has, or with another requirement. Examples: kombu's "add tests in test_base.py" against a kept "don't modify test files"; boa's contradictory `enqueue_job` plan items.
    - **Code checks the authority** (astra repair-design r1, 25 Oct). The span is Opus's authority quote for a restated plan item, and the item's own words otherwise. It must be found verbatim in a source that authority allows, and the source span is recorded. The allowed sources:
      - the task and harness messages: user-role messages of the run's histories, never a compaction summary;
      - the maintainer's replies;
      - authoritative repository files (AGENTS.md, CLAUDE.md, README, CONTRIBUTING) as of the task's base commit. They count only as regular files that the agent read before the boundary, by a command whose path resolves to that file after `cd` and relative paths. A symlink, or a path that can't be resolved, is excluded.
    - Nothing the agent wrote or echoed counts: not its messages, not its plan file, not any other command output. So a plan can't vouch for itself.
    - **The span must be substantive:** at least 6 words sharing at least half of the obligation's key words, or every key word of the obligation.
    - A conflict verdict excludes the item whatever its quote; the conflict quote is checked against the boundary evidence and recorded.
  - **One candidate per task obligation:** a repeated obligation counts at its first boundary only, and once within a boundary, e.g. the same rule from the maintainer ledger and the audit, or from two summaries.
  - **Every candidate is screened to the end,** at every boundary, in ascending order per run.
  - **Assignment, fixed before any card is labelled and blind to outcomes** (astra repair-design r2, 25 Oct):
    - per boundary, the passing candidates in the order of a seeded hash of their ids (seed 20261025);
    - the first 2 are assigned (a cap for cost and card time), and the rest are recorded as dropped by the cap;
    - it is stored once and never recomputed.
    - **No refill:** a card Jacob rejects keeps its slot, empty. It is recorded as `rejected_by_labeller` and counted in a separate tally, never in the outcome rates. An assigned candidate that an edit's rescreen excludes is tallied the same way (`excluded_on_rescreen`).
    - **Limitation:** the labeller had seen the base runs' outcomes, so he can remove cases but never choose them. The arms within a kept candidate stay randomised, so its within-candidate contrasts stay valid. The whole cohort is exploratory.
    - An edit or an override of a card's repair text or act makes it a new candidate: it is screened again through every gate, then relabelled.
    - **Max's 7-day window** is checked before every Max request of the screen, from a reading at most 5 minutes old; at 90% new requests stop.
  - **No selection on outcome:** the base continuations' follow-up has been read (12 followed, 4 not, 3 with no opportunity, 1 sealed). So the candidates are every item that passes the gates, whatever the base run did later. The 4 misses are never picked because they failed.
  - **Arms for a file-backed candidate:** neutral matched text, the exact content restored, and an explicit "Re-read <file> before continuing."; 2 repeats each, so one extra continuation per candidate. Every arm records whether it re-reads the file.
    - **The reread arm is an unmatched practical-strategy arm.** Its text is fixed and isn't length-matched to the repair. It compares a recovery strategy (pointing at the file) with restoring the content and with neutral text; it isn't a length-controlled contrast. Only repair against matched is the length-matched pair.
  - **Freeze before any continuation:** the universe is written and committed before any arm. It holds, timestamped:
    - per screened candidate, its requirement, evidence quotes, every gate result and the hash of its screened record;
    - each boundary's assignment;
    - the roster: candidate × arm kind × repeat, for every assigned candidate with a freeze, bound to the freeze's record hash;
    - the screen hash, the window, the scoring, the deduplication and the selection rule.
  - **The universe is refused while any assigned candidate is unresolved:** each must be frozen or rejected by the labeller first, so none can drop out of the denominator unseen.
  - **Every continuation needs the universe:** an arm or secondary runs only for a roster entry whose candidate and freeze hashes still match. That covers bundles and older freezes too. The one exception is testem's historical freeze, listed by name and record hash.
  - Eligibility comes from boundary evidence only, and Opus is outcome-blind. Jacob's prior exposure to the follow-up is recorded on every freeze. Ambiguous cases are adjudicated independently.
  - **Scoring:** compliant, violated and not reached as shares of every roster entry, never conditioned on reaching the act. The readout joins each arm's outcome onto the roster, so a missing or failed arm stays in the denominator.
  - **What a result can claim:** a positive result says restoring the content, or prompting a re-read, changes the governed act in these continuation states. It does not say the omission caused the base run's behaviour, or that an uncompacted agent would have complied. Claims are scoped to the measured model, the harness and its attachment policy, and the scripted planning workflow. Confirmation needs untouched sessions.

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
