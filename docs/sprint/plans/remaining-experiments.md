# Compaction sprint: plan for the remaining experiments

2026-10-23 · Jacob Epifano

The remaining week answers the proposal's second question with the losses Experiment 1 actually found: we repair items live summaries really lost and rerun the agent, and, as a labelled stress test, remove or misstate items of those kinds in summaries that kept them.

## Review summary (proposed, not yet signed off)

| Row | What it says |
| --- | --- |
| **Decision it informs** | What we run in the last six days, in what order, and what each experiment hands to the next |
| **Sequence** | Experiment 2 close-out: the pilot's readout and Experiment 1's analysis on its live boundaries → Experiment 3: targeted repair of verified natural losses, plus induced summary edits as a stress test, on new live runs that keep their snapshots (Q2) → write-up. Experiment 4 (live runs where the user gives Experiment 1-type content mid-task) is a fallback Jacob may choose after seeing Experiment 2's count; the first remedy for too few usable losses is more Experiment 3 base runs. Q3's correction and prompt-fix questions, repeated compaction (Q4) and the frontier replication are deferred or cut first |
| **Why it changed** | The pilot's four planted prohibitions were almost never lost in chat, under any summarizer or placement. Experiment 1 found the losses are permissions, decisions, commitments and task state, so that is what the next experiments use |
| **Cost** | About $100–650 planned of the $1,594 left; each experiment has its own plan and cap |
| **Status** | The pilot finished: 24 runs, $52.51, nothing left running. Each experiment's plan gets one astra review round before Jacob signs it off; nothing new runs before that. This roadmap has had its three allowed rounds, the last on 24 Oct |
| **Open for Jacob** | Decided 24 Oct: one configuration first; whether repair leads is Jacob's call after seeing Experiment 2's count, with no fixed threshold; then each experiment's plan in turn |

## Where we are against the proposal

Q1 is answered offline; Q2–Q4's online parts have not started. These are the four questions Neel approved on 20 Oct ([proposal](https://github.com/jrepifano/mats12_neel_exploratory/blob/main/docs/sprint/compaction-proposal.md)).

| Question as pitched | Experiment | Status, 23 Oct |
| --- | --- | --- |
| **Q1.** How often does compaction drop, mis-summarize or invent actionable facts, and does that grow with context size? | Experiment 1: offline audit of real compactions (650 sampled, 649 analyzed) | **Done.** 16% of actionable items judged lost (11% after a precision adjustment, a sensitivity, not a true-loss range; recall unmeasured); 41% of compactions with a user rule lose at least one; permissions lost far more than prohibitions; +6 points from 256k to 512k (descriptive, 14 histories); the planned summarizer contrast null; invention exploratory. Report drafted, not reviewed |
| **Q2.** Does compaction make agents break the task's rules, and does restoring the lost rule undo it? | Experiment 2 (pilot), then the causal runs | **Pilot done (24 runs, $52.51); causal runs not started.** The planted rules were rarely lost, so the causal test as designed has little to work with |
| **Q3.** Do summaries invent permissions, does correcting them change actions, can the summarizer be prompted to avoid it? | Experiment 1 for the rate; online runs for the rest | **Rate only:** invented permissions in about 3% of summaries, exploratory (judge precision 8/13). Online part not started |
| **Q4.** Do losses and rule-breaking compound across repeated compactions? | Repeated-compaction runs | **Not started**; the sprint plan already deferred it |

Neel's steer on the approved proposal: realism first, and the target result is "in real coding trajectories, compaction often missed these kinds of information; counterfactually rerun with/without it and these things happened". He also said the yes/no is obvious ("surely it does affect things"), so the interest is how much, which errors and how often.

## What Experiments 1 and 2 taught us

The pilot planted the kind of content Experiment 1 showed survives, so the next experiments use the kinds it showed get lost. The proposal said "what gets lost sets the rules and thresholds for the online runs"; the pilot's rules were frozen on 20–21 Oct, before Experiment 1's results existed, so that link was never made.

- **Experiment 1: what gets lost.** Permissions and grants (87% of one benchmark's grants, a single repeated task template, while its prohibitions survived), decisions (9–18% by source), commitments (9–20%) and task state (8–28%). User rules in real sessions are usually casual or embedded in longer messages. These rates come from one judge with loss precision 0.72 and unmeasured recall.
- **Experiment 2: what survived.** A short, numbered block of four prohibitions given as a chat turn was kept at every live chat boundary (8) and at every offline cut point (60k–200k tokens) under Claude Code's prompt, Codex and pi. Moving it mid-session or scattering it over four turns didn't change that, but those were edited-history screens of the summarizer, not live delivery. The exception: GLM's pi summaries reworded the consent rule ("wait for explicit approval" dropped) and lost the test-file rule at 2 of its cut points.
- **Experiment 2: where the losses were.** By the retention judge (not yet spot-checked by Jacob), rules read from the README were lost at 5 of 8 live boundaries (GLM 2, luna 2 with the consent rule only, DeepSeek 1); Haiku's Claude Code summaries (2) and one DeepSeek boundary kept them. The AGENTS.md control lost a rule at 1 of 8 (GLM contradicted one despite the re-attached file); chat lost none of 8. That is tool output summarized away, with the rules still re-readable from the file: a separate finding from the pitched question.
- **What the live histories contain.** DeepSWE tasks are autonomous and our follow-up turns carry no content, so most information in a live history is the agent's own: task state, decisions, finished and unfinished work.

**What changes:**

1. **Repair real losses first.** Experiment 2's close-out finds the items live summaries actually lost; Jacob verifies each one used. Experiment 3 restores that item alone, against a matched irrelevant addition, from the same saved snapshot. This is the proposal's design with Experiment 1's item types in place of planted rules, and the closest to Neel's target result.
2. **Induced edits as a stress test, labelled as such.** Removing or misstating an item the summary kept shows how sensitive the agent is to losing that kind of item. It doesn't show natural compaction causes that harm, since items summarizers drop on their own may differ in salience and relevance. It is reported separately.
3. **Behaviour is scored by what the item governs,** defined before any continuation is read: the item-specific act (the decision kept or contradicted, the step redone, the permission used or not), paired with task progress on DeepSWE's hidden tests and cost.
4. **Continuations get a longer, capped horizon** instead of 20 calls, with the length set by Sunday's capacity check.
5. **The planted-rule result is reported as its own finding**, with its configuration-specific counts: explicit chat prohibitions survived in these setups; README rules didn't, except under Claude Code's prompt.

## How the experiments chain

Each experiment takes a concrete input from the one before and hands one on: Experiment 1's loss types tell Experiment 2 what to look for, and Experiment 2's verified losses are what Experiment 3 repairs.

*Diagram (in the Claude Doc): Experiment 1 (done) → Experiment 2 close-out (closing) → Experiment 3 (to run) → write-up; Experiment 4 (to run only if needed) feeds Experiment 3; repeated compaction and the frontier replication are optional.*

Experiment 1 sets which item types to look for; Experiment 2 estimates how often live boundaries yield usable losses, sets the candidate checks and measures costs; Experiment 3 runs new base trajectories that keep their snapshots (the pilot's were deleted at cleanup); Experiment 3 repairs those losses and, separately, edits kept items as a stress test. If usable losses are scarce, the first remedy is more Experiment 3 base runs; Experiment 4 is a fallback Jacob may choose after seeing Experiment 2's count.

## The remaining experiments

Three experiments remain, each with its own plan document written and reviewed before it runs; the two optional ones are cut first.

| Experiment | Question | Takes from earlier | Hands on | Cost (cap) | Plan |
| --- | --- | --- | --- | --- | --- |
| **2. Pilot close-out** | Which items do live summaries lose, and how many are usable for repair? | Experiment 1's judge pipeline, unchanged; the pilot's live boundaries | Verified natural losses with a later decision that depends on them; their types; the judge's live precision; cost and wall-clock per run | $0 (Max plan); the pilot finished at $52.51 | drafted |
| **3. Repair and edits** (Q2) | (a) Does restoring an item a live summary really lost change what the agent does? (b) Stress test: how sensitive is the agent to losing an item of each kind? | The candidate checks, yield estimate and costs from 2; its own new runs keep their snapshots | Repair effect, edit effects per type, each with intervals and utility | $100–500 ($500) | after 2 |
| **4. Fresh runs with user content** (only if needed) | Supplies natural losses of the user content Experiment 1 found fragile (grants, decisions, embedded requirements), delivered live mid-task | The content types from 1; routes and thresholds from 2 | More natural losses for 3a, and a descriptive loss rate for that content | $100–150 ($150) | only if Jacob chooses it after 2's count, when more base runs can't supply enough losses |
| *Deferred:* Q3 correction and prompt fix | Do corrected permission errors change actions; can the summarizer avoid them? | Real permission errors from 2 or 4 | — | — | only if real cases turn up |
| *Optional:* repeated compaction (Q4) | Do losses compound across two or three compactions? | Trajectories from 3 | Compounding per compaction | ($150) | cut first |
| *Optional:* frontier replication | Does the strongest effect from 3 hold for Opus 5.5 and astra? | The strongest effect from 3 | Whether it generalizes | ($200) | cut second |

**Experiment 2 close-out.** The pilot has finished (24 runs, $52.51); its readout follows as planned: retention labels, the gate, Jacob's audit, release. Added: Experiment 1's full pipeline on every live boundary, Jacob's labels on its live loss calls, and a frozen list of candidate losses. Each candidate records its source quote, why it still applies, whether it is in the resumed input or re-readable, and the later decision it should govern. The pilot deleted its snapshots, so Experiment 3 can't fork from these boundaries. The count of usable losses, their spread over the 6 tasks and the share of boundaries with one go to Jacob, who decides after seeing them what leads Experiment 3 and how many new base runs it needs (about 25 divided by that share, since Experiment 3 uses one loss per boundary).

**Experiment 3: repair and edits.** New base runs keep each boundary's snapshot; from it: the real summary; the summary with the lost item restored in source-faithful words (repair); the summary with a matched irrelevant addition; and, for the stress test, the summary with a kept item removed or misstated against a matched edit to an irrelevant sentence. The full-history arm stays as context. Outcomes are fixed before any continuation is read: the item-specific act, with task progress and cost. Sunday's capacity check (one complete comparison, concurrency, snapshot restore, grading) is binding and sets the horizon and the number of boundaries.

**Experiment 4: fresh runs with user content, only if needed.** If Experiment 2 yields too few usable natural losses and more base runs can't make up the shortfall, Jacob may choose new live runs that give Experiment 1-type user content mid-task (a grant such as "you may change test\_x", a design decision, a requirement inside a longer message), delivered live. Whatever the summarizers lose becomes more repair cases for Experiment 3. Its loss rate is reported descriptively, not multiplied into Experiment 3's effects: the two measure different populations.

## Timeline and budget

Experiment 3's bulk runs Mon–Tue, which leaves Wed for checks and Thu–Fri for analysis and slides before the 10 PM Fri 30 Oct deadline.

1. **Fri 23:** the pilot finished (24 runs, $52.51).
2. **Sat 24:** Experiment 2 close-out (Experiment 1's pipeline on the live boundaries, Max plan); Jacob's labels (about 30 live-loss cards, the retention spot check and the release audit, about 2–2.5 h in all, with the candidate checks); the candidate list frozen. Experiment 3's plan written, reviewed and signed off; harness changes built (keep snapshots, repair and edit arms, capped long continuations).
3. **Sun 25: capacity check, binding.** One complete comparison (all arms) on one configuration: wall-clock, sustainable laptop concurrency, snapshot restore and grading measured. It must show restorable snapshots and working scorers, and measure the cost and Jacob's time per boundary screened, which grow as the share of boundaries with a usable loss falls. It sets the horizon and the number of boundaries with 20% slack, and freezes call, token and time limits and a balanced order for cancelling batches before any bulk run; the desktop counts only once verified. Experiment 4 starts only if Jacob chooses it after Saturday's count.
4. **Mon 26 – Tue 27:** Experiment 3 bulk, starting with one configuration and the repair test; edit types added as capacity allows, batches cancelled from the tail if the deadline would slip. Call with J, Mon 1:30 PM ET.
5. **Wed 28:** skeptic checks (blinded adjudication, matched controls, per-task results); optional replication only if time and money allow.
6. **Thu 29 – Fri 30:** analysis frozen, report, slides.

**Budget.** The sprint cap is $1,652; $58.25 is spent ($5.75 on Experiment 1, $52.51 on the pilot).

| Experiment | Planned | Cap | Moved from |
| --- | --- | --- | --- |
| 2. Pilot close-out | $0 more (finished at $52.51) | $80 (set) | already in place |
| 3. Repair and edits | $100–500 | $500 | the Q2 ($270) and Q3 ($100) stages, plus $130 of contingency |
| 4. Fresh runs with user content | $0, or $100–150 if needed | $150 | the contingency, only if needed |
| Optional: repeated compaction | — | $150 | the contingency, only if 3 finishes early |
| Optional: replication | — | $200 | the replication stage ($420) |
| **Total planned** | **about $100–650** |  | the rest stays unspent unless a result calls for it |

Spent figures are from the budget books on 24 Oct; they include a logged over-count of about $0.83 from refused DeepSeek calls. The reserve has $117 left after the pilot's $30 transfer, so the extra money comes from the $600 contingency.

## Decisions for Jacob

1. **Which test leads Experiment 3.** Decided by Jacob, 24 Oct: no fixed threshold. Experiment 2 reports the usable losses, their spread over the pilot's 6 tasks, the share of boundaries with a usable loss and the implied number of Experiment 3 base runs; Jacob decides after seeing the count whether repair leads, with induced edits as the labelled stress test. Signs of life are enough to start: if losses are scarce, the first remedy is more base runs, and induced edits lead only if scaling up can't supply enough, with that caveat stated. This replaces an earlier rule, at least 10 usable losses across at least 5 tasks; the pilot has only 6 tasks. Also decided: Jacob's 1.5–3 h of candidate checks over Sun–Tue is workable; Experiment 3 starts with one configuration, a second only if Sunday's check leaves room (candidate: luna in the real Codex CLI on Jacob's subscription, if a free feasibility check passes).
2. **Experiment 3's main outcome:** the item-specific act, defined before any continuation is read, paired with task progress and cost. Task success alone could hide the mechanism.
3. **Continuation length** set by Sunday's capacity check, capped, rather than fixed now.
4. **Budget moves:** the Q2 and Q3 stage caps plus $130 of contingency into Experiment 3 ($500); Experiment 4's $150 from contingency only if it runs.
5. **Q3's correction and prompt-fix questions** deferred unless real permission errors turn up in Experiment 2 or 4.
6. **Experiment 1's report:** its review round and the missing carry-over sensitivity, before Wed so its numbers can go into the slides.

- [ ] Jacob signs off this sequence
- [ ] Experiment 2 close-out plan reviewed and signed off
- [ ] Experiment 3 plan reviewed and signed off
- [ ] Experiment 4 plan reviewed and signed off
