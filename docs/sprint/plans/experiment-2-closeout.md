# Experiment 2 close-out: what do live summaries lose?

2026-10-23 · Jacob Epifano

The pilot has finished (24 runs, $52.51, nothing left running); this close-out runs Experiment 1's loss analysis on its live boundaries and freezes a hand-verified list of real losses. The pilot deleted its boundary snapshots, so Experiment 3 can't fork from these boundaries; their usable losses are what Jacob decides Experiment 3's lead test from, after seeing the count, and they estimate how many new runs it needs, and rehearse its candidate checks.

## Review summary (proposed, v0.2, not yet signed off)

| Row | What it says |
| --- | --- |
| **Decision it informs** | Which real losses Experiment 3 repairs, and whether repair or the induced-edit stress test leads: Jacob decides after seeing the count, from the usable losses, their spread over the pilot's 6 tasks, the share of boundaries with a usable loss and the implied number of Experiment 3 base runs. If losses are scarce, the first remedy is more base runs; the stress test leads only if scaling up can't supply enough. That share also sets how many new base runs Experiment 3 needs, since these boundaries can't be forked again. Also whether Experiment 1's judge can be trusted on live histories |
| **Parts** | (A) finish the pilot as specified: retention labels, the gate, audit, release; (B) Experiment 1's pipeline on every valid live boundary; (C) 30 hand-label cards from Jacob on live-loss calls; (D) a frozen list of candidate losses, used as the input to Jacob's decision on Experiment 3, its yield estimate and a dry run, each with its quote, applicability, availability and the later decision it governs; run cost and wall-clock |
| **Primary measures** | Share of actionable items lost per live boundary, by item type and by model (each model has its own summarizer, so the two aren't separable); judge precision on live loss calls from the hand labels |
| **Predictions** | Live boundaries lose 5–20% of items (60%); task state and decisions make up most losses (70%); live precision is within 0.15 of Experiment 1's 0.72 (60%) |
| **Stops** | One gate and one decision (see Outcome scenarios): live precision below 0.5 means no live loss rate is reported from the judge; the usable-loss count is reported with its task spread and yield, and Jacob decides what leads Experiment 3 after seeing it. Every candidate Experiment 3 uses passes the five usable-loss checks, confirmed by Jacob |
| **Cost** | $0 billed for the pipeline (Max plan); nothing more for the pilot, which finished at $52.51 of its $80 cap; Jacob about 2–2.5 h (spot check, calibration cards, audit, and 3–5 min per candidate check) |
| **Changed since last version** | v0.2 (24 Oct): the pilot finished; its snapshots were deleted, so these losses feed Jacob's decision on Experiment 3, its yield estimate and a dry run, not its forks; the fixed 10-across-5-tasks rule is dropped (Jacob, 24 Oct); a small record adapter; recoverability approximated from the base repo; after astra r2, sizing by the share of boundaries with a usable loss, three-way recoverability and a predeclared precision estimator |
| **Reviews** | astra r1 (3 BLOCKING, fixed); astra r2 (combined with the roadmap and Experiment 3; findings here fixed) |
| **Open for Jacob** | The calibration-card count (30); whether the offline cut points are judged at all (planned: only if time allows) |

## Research question

**When live coding agents compact at a configured threshold, which actionable items do their summaries lose, and does Experiment 1's judge find them as reliably on live histories as on recorded ones?**

- **Where it comes from.** The proposal's first question asked how often compaction drops, mis-summarizes or invents actionable facts. Experiment 1 answered it on 649 analyzed recorded compactions (650 sampled), mostly Claude Code's own. The live boundaries add the three other summarizers we run (pi, Codex local, and Claude Code's prompt on Haiku), on fresh long tasks, at a known threshold.
- **What Experiment 3 needs from it.** Experiment 3 repairs real losses and, as a stress test, removes or misstates kept items. It can't fork from these boundaries, since the pilot deleted their snapshots. It needs from them the count of usable losses (what Jacob decides its lead test from), the share of boundaries with one (how many new runs to start), a rehearsal of the candidate checks, which item types live summaries hold and lose, how losses look (dropped outright, or misstated), and whether the judge's "lost" calls are right often enough to find candidates with.
- **What it is not.** It doesn't test behaviour. The pilot's planted-rule outcomes stay sealed and are read separately, after the audit, as the pilot planned.

## What we will do

Four parts, all on data the pilot already has; none of them spends money.

| Part | What it does | Produces | Cost | When |
| --- | --- | --- | --- | --- |
| **A. Finish the pilot** | Retention labels for the planted rules (done); Jacob's 24-card spot check of the retention judge; the gate; the 20-card audit and release | The planted-rule result per channel; cost, cache and wall-clock per model | $0 more (the pilot finished at $52.51); Jacob about 50 min | Sat |
| **B. Experiment 1's pipeline on live boundaries** | Item listing, kept/dropped/misstated grading, the second look and availability on every valid pilot boundary (up to 24) | Items and loss calls per boundary | $0 (Max plan), about 0.5–1 h of judging | Sat morning |
| **C. Hand labels** | 30 seeded cards from Jacob on live calls: 20 the judge called lost, 10 it called kept | Live precision, and a first look at misses | Jacob about 30 min | Sat |
| **D. Handoff to Experiment 3** | Counts by item type, model and grade; random raw examples; the frozen candidate list (quote, applicability, availability, governed decision); the edit styles the stress test copies | The usable-loss count and its task spread (for Jacob's decision on Experiment 3), the share of boundaries with a usable loss (its yield estimate), and a rehearsed candidate format | $0 | Sat |

The offline cut points from the pilot's exploration (about 50 cuts from 9 trajectories) are judged the same way only if time allows, and then reported only as descriptive context: they come from few trajectories and are strongly correlated. The edited-history screens are left out.

## Procedure

Experiment 1's pipeline runs unchanged except for three adaptations to live boundaries, each named below.

**Inputs.** Each valid pilot boundary saved the full pre-compaction history, the summary and the exact post-compaction input the agent resumed from, close to Experiment 1's record format. A small adapter converts the resumed input, saved in the agent's own message format, and puts the AGENTS.md text, which the saved history leaves out, first as a user message, so the item lister sees the AGENTS.md channel's rules. Haiku's first scriggo attempt is excluded (its summary was empty, a recorded harness failure); DeepSeek's dynamodb boundary is included, since only its arms failed (the API account ran dry after the boundary).

**Reused unchanged from Experiment 1.** The frozen item and claim prompts, Haiku 5.5 on the Max plan at medium effort, the three passes (list items without seeing the summary; grade each kept, dropped or misstated; list summary claims and check them), the search-tool second look (which re-checks dropped calls and unsupported claims, not misstated calls), and the definition of lost: dropped or misstated, and not available any other way.

**Adapted, and why.**

1. **Availability is computed from the exact resumed input, by new code.** Experiment 1's availability step reads only kept messages and leading system messages, and infers reloading and recoverability from the history. On live boundaries we have the real input, so every retained non-summary message in it counts as available: pi's kept tail, Codex's kept user turns, the re-attached AGENTS.md. The pilot didn't save the repo at its boundaries (only file hashes, inside the sealed arms), so recoverability is approximated: a search of the task's base repo, re-pulled from DeepSWE's image, plus the agent's own file writes and file-changing commands in the history. Each lost item is marked recoverable (its key strings are in a base-repo file the agent's recorded actions never touched, or in the agent's last write of a file), not recoverable (in neither, and no recorded command could have produced it), or unresolved (a file the item concerns was changed by a command we can't replay). Experiment 3 keeps its snapshots, so there the repo state is exact, though whether an item can be recovered from it stays a judgment. Unit tests gate it: a retained Codex user turn, a re-attached AGENTS.md, a file changed after the agent read it, the format conversion and the planted-rule tagging.
2. **Planted content is tagged.** Items that are the pilot's four planted rules are marked and reported apart, since the retention judge already covers them.
3. **No privacy restriction.** DeepSWE is public and the runs are ours, so every judge may read them.

**Sample.** Up to 24 boundaries: 6 tasks × 4 models, 2 tasks per channel for each model. Each boundary is one independent trajectory. Intervals resample whole tasks.

**Hand labels (a calibration, not a full validation).** A seeded draw of 30 cards from the held-out half of the pilot's existing split: 20 items the judge called lost (both dropped and misstated) and 10 it called kept, spread across models; if a stratum runs short, the remainder comes from the development half and is marked. Strata and inclusion probabilities are recorded so weighted estimates are possible. Each card shows the item, its source quote with the full history searchable, the full post-compaction input with a find box, and, for recoverability, the matching lines from the base repo and from the agent's writes, plus the commands that touched those files. It asks: is this an actionable item; is it absent from or misstated in the resumed input; is it recoverable from the repo (the base repo plus the agent's own writes); or unclear. The judge's verdict is hidden. Errors on kept-call cards are reported as such, not as recall. Every candidate Experiment 3 uses is verified separately, through the usable-loss checks below.

**Measures.**

- Lost share of items per boundary, by item type and model, with task-clustered intervals.
- Live loss precision: the share of judge "lost" calls Jacob confirms, weighted by the recorded inclusion probabilities, with a task-clustered bootstrap interval; the unweighted share and its Wilson interval (about ±0.18 at 20 cards) are shown beside it. Unclear labels are left out of the denominator and counted. The 0.5 gate is a rough calibration rule on the weighted point estimate; with fewer than 15 lost-call cards labelled clearly, it isn't applied and no live rate is reported. Experiment 1's 0.72 was measured on dropped calls only, after the loss definition changed, so the comparison is qualified.
- How losses look: dropped vs misstated, and for misstated items, what changed (a scope narrowed, a status flipped, a number changed).
- Random raw examples, drawn by seed, for the handoff.

## Predictions

Written on 23 Oct, before the pipeline runs on any live boundary. The priors lean on Experiment 1, adjusted for live histories having few user turns.

| Prediction | Prior | Basis |
| --- | --- | --- |
| Live boundaries lose 5–20% of actionable items | 60% | Experiment 1: 16% raw, 11% precision-corrected; live summaries are of shorter histories (120–180k against up to 970k) |
| Task state and decisions make up at least half of all live losses | 70% | Live histories are mostly the agent's own work; the follow-up turns carry no content |
| User-given items other than the planted rules are rare: under 3 per boundary on average | 75% | DeepSWE tasks give one task message; follow-ups are check-ins and logs |
| Losses differ by model by at least 2× between the highest and lowest | 50% | The README and GLM-rewording results; Experiment 1's method spread (14% to 29% of user rules) |
| Live loss precision (Jacob's labels) is 0.57–0.87, within 0.15 of Experiment 1's 0.72 | 60% | Same judge and prompts; shorter, more uniform histories may help, missing harness context may hurt |
| At least 10 usable losses, as defined under Outcome scenarios | 55% | The riskiest assumption for Experiment 3; astra's cheapest test of it |

## Outcome scenarios

Two things are decided separately: the judge's live reliability decides what may be reported as a rate, and the count of usable losses informs Experiment 3's shape, which Jacob decides after seeing it.

**A usable loss** is a candidate that passes five checks, each recorded with its evidence before any continuation is read. Jacob confirms each check; "unclear" on any one excludes the candidate, with the reason logged.

1. **Retention:** the item is absent from the resumed input, or materially misstated there (a scope, status or value differs from the source quote).
2. **Applicability:** it still bears on remaining work at the boundary. This includes completed-work facts ("X is done", "Y was tried and failed") when redoing or contradicting them is possible.
3. **Not recoverable from the repo:** no file in the repo and no part of the task description states it. Here it is checked against the task's base repo plus the agent's recorded writes and commands: recoverable items are excluded, not-recoverable items pass, and unresolved items are left out of the confirmed count but reported as a second, upper count. Sizing uses the confirmed count, with the upper count as a sensitivity. Experiment 3 checks its kept snapshots exactly.
4. **A governed decision:** a later decision it bears on can be named from the task and the boundary's state, with an observable act that satisfies or contradicts it.
5. **A frozen design:** its repair text, its control, the act, the scoring rubric, and the rule that a continuation never reaching the decision counts as not performing the act. The main effect is never conditioned on reaching the decision.

Equivalent items from the same boundary count once. Counts are reported at every check (detected, confirmed, applicable, not recoverable, with a governed decision), so a low final count can be traced to its cause; the judge's recall stays unknown.

| Gate | What we observe | What follows |
| --- | --- | --- |
| **Reporting** | Live loss precision at least 0.5 | Live loss rates reported, with intervals and the calibration caveat |
| **Reporting** | Below 0.5 | The judge only finds candidates; no live loss rate is reported from it; counts at each check still are |
| **Shape, decided by Jacob after seeing the count** | The usable losses, their spread over the pilot's 6 tasks, the share of boundaries with at least one usable loss (q, which sizes Experiment 3; losses per boundary are reported too), and the implied number of Experiment 3 base runs. Dropped and misstated losses are counted separately, since Experiment 3's first run repairs dropped items only | Jacob decides whether repair leads. What we look for is signs of life, not a fixed number. If usable losses are scarce, the first remedy is more base runs; the stress test leads only if scaling up can't supply enough, with that stated |

The planted-rule result is reported in every case, alongside the counts at each check. Experiment 3 uses one loss per boundary, so it is sized by q, the share of boundaries with at least one usable loss, per configuration and with an interval: 20–30 qualifying boundaries need about 25/q base runs that reach a boundary (q = 0.3 gives about 85), plus runs that fail before compaction, and each also costs screening and Jacob's checks. Losses per boundary and unique tasks are reported descriptively. The earlier fixed rule, at least 10 usable losses across at least 5 tasks, is dropped (Jacob, 24 Oct): the pilot has only 6 tasks, so it meant 5 of 6.

## Budget, timeline and stopping points

Everything new runs on the Max plan at $0 billed; the pilot finished at $52.51 of its $80 cap, with no containers or snapshot images left.

1. **Done, Fri night:** all 24 pilot runs finished; the retention judge labelled every valid boundary; the spot-check page builder exists.
2. **Sat morning:** the record adapter, the availability and recoverability checks and their tests, with one astra code review; then Experiment 1's pipeline on the live boundaries, items and loss calls first. Claim analysis and the offline cut points come only if time allows. About 0.5–1 h on Max, unattended.
3. **Sat, Jacob's time (about 2–2.5 h):** the retention spot check (24 cards, \~30 min), the live-loss calibration cards (30, \~30 min), the release audit (20 cards, \~20 min), and the usable-loss checks, 3–5 min per candidate, with 10–15 expected after the automatic filters (30–75 min).
4. **Sat afternoon:** the gate, the release, the loss tables and the counts at each check. Experiment 3's plan is finalized from them.

**Checkpoints where work stops for Jacob:**

- after the candidate list is built and before it is frozen;
- after the usable-loss count, when Jacob decides what leads Experiment 3, before Experiment 3 or 4 spends anything.

**Stops:** if the Max plan's usage window runs out mid-judging, the run pauses and resumes; nothing is moved to a paid route without asking.

## Risks and limitations

- **One judge, one family.** As in Experiment 1, every loss call is Haiku's. The hand labels measure its live precision; recall stays unmeasured beyond the 10 kept-call cards.
- **Few boundaries, four configurations.** Up to 24 boundaries, about 6 per model. Each model is paired with its own summarizer, so model and summarizer can't be separated.
- **The candidate list is judged by people who will later see outcomes.** It is frozen, with exclusions logged, before any Experiment 3 continuation exists, and the pilot's sealed arms stay sealed until the audit.
- **The pilot's arms are 20 calls.** A later decision that a lost item governs may come after that, so the pilot's own arms can't show whether the losses mattered; that is Experiment 3's job.
- **Live histories hold little user content.** Losses will mostly be the agent's own task state and decisions. That fits what live coding agents carry, but it means few user grants or decisions to repair, which Experiment 4 exists to cover.
- **The availability rule is stricter than Experiment 1's.** Using the exact resumed input is more accurate, but makes the live and recorded rates not directly comparable; both definitions are reported. Recoverability is approximate here, since the pilot didn't save the repo at its boundaries. Stale base-repo files can mark an item recoverable that wasn't, lowering the count; unrecorded changes can do the opposite, raising it; unresolved items are reported as their own count. Excluding recoverable losses also narrows the population Experiment 3 tests, which can make its effects larger than for all natural losses.

## Review history

| Round | Verdict | What changed |
| --- | --- | --- |
| astra r1 (23 Oct, with the roadmap's round 2) | 3 BLOCKING, 3 MINOR, 1 NIT | Availability from the exact resumed input; the five usable-loss checks; a reporting gate separate from the shape decision; calibration framing for the hand labels |
| astra r2 (24 Oct, with the roadmap's round 3 and Experiment 3's round 1) | 4 BLOCKING across the three plans (2 here), 3 MINOR, 1 NIT | Sizing by the share of boundaries with a usable loss; recoverability as recoverable, not recoverable or unresolved, with an upper count; a predeclared weighted precision estimator |
| Jacob | pending | — |
