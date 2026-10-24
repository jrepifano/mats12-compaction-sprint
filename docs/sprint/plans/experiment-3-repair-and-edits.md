# Experiment 3: does restoring what a live summary lost change what the agent does?

2026-10-23 · Jacob Epifano

Experiment 3 reruns the agent from the exact moment of a real compaction, once with the summary as it was and once with only the lost item put back, to measure how much that one loss changes what the agent does next. Draft: its size and which test leads wait on Experiment 2's count of usable losses (Sat) and on Jacob's decision.

## Review summary (proposed, v0.1, not yet signed off)

| Row | What it says |
| --- | --- |
| **Decision it informs** | Whether the losses live summaries really make change agent behaviour, by how much and for which kinds of item; the presentation's main causal claim. Default without it: only loss rates, no evidence about consequences |
| **Arms** | From one saved snapshot per boundary: **S** the real summary; **S+repair** the lost item restored in source wording; **S+control** a matched irrelevant item added; **F** the full history (context only). Stress test, on boundaries whose summary kept an item: **S−item** that item removed or misstated; **S−control** a matched edit to an irrelevant sentence |
| **Primary measure** | Per boundary, whether the continuation performs the item-specific act frozen in advance (keeps or contradicts the decision, redoes the step, uses or forgoes the permission). Repair benefit = act rate in S+control minus S+repair (the act is the one the loss predicts, so positive means repair helps), paired by boundary, task-clustered interval. Reported with task progress and cost |
| **Predictions** | the repair benefit's interval excludes zero (50%); the stress-test effect is larger than the repair effect (60%); task progress differs by under 10 points between repair and control (65%) |
| **Stops** | Sunday's capacity check is binding; no bulk run until one full comparison has run end to end. Too few usable losses: more base runs first; Experiment 4 or a stress-test lead only if that can't supply enough, decided by Jacob after seeing Experiment 2's count |
| **Cost** | About $100–500 planned, cap $500; set by Sunday's measured cost per qualifying boundary, including base runs and the screening of boundaries without a usable loss. Jacob: 1.5–3 h of candidate checks over Sun–Tue, about 1 h of adjudication Wed |
| **Changed since last version** | v0.2 (24 Oct, after astra r1): sized by the share of boundaries with a usable loss; dropped items only in the first run; exact arm inputs frozen and S and F sealed before selection; power by simulation |
| **Reviews** | astra r1 (4 BLOCKING, fixed in this version) |
| **Open for Jacob** | Decided 24 Oct: whether repair leads is Jacob's call after seeing Experiment 2's count (no fixed threshold; more base runs are the first remedy for scarce losses); Jacob's 1.5–3 h of candidate checks Sun–Tue is workable; one configuration first, a second only if Sunday's check leaves room. Still open: the horizon, set by Sunday's check |

## Research question

**When a live coding agent's summary loses an item it later needs, does putting back only that item change what the agent does, and by how much?** Secondary, as a stress test: how sensitive is the agent to losing an item of each kind that its summary kept?

- **From the proposal.** The second pitched question: does compaction make agents break the task's rules, and does restoring the lost rule undo it. The design is the proposal's: fork at the first compaction into full history, actual summary, summary plus the lost item, summary plus a matched irrelevant fact. What changes is the item: real losses of the kinds Experiment 1 found, not planted rules, which the pilot showed are rarely lost.
- **From Neel.** His target result was "compaction often missed these kinds of information; counterfactually rerun with/without it and these things happened", and he said the yes/no is obvious, so the interest is how much and for which errors. The repair contrast measures exactly that, per item type.
- **From Experiment 1.** Which item types to expect (task state, decisions, commitments, permissions) and how losses look (dropped, or misstated with a narrowed scope or flipped status). The stress-test edits copy those styles.
- **From Experiment 2.** Not the boundaries themselves: the pilot deleted its snapshots, so Experiment 3 starts new runs. It takes q, the share of boundaries with at least one verified usable loss, per configuration, which sets how many new base runs it needs (about 25/q for 20–30 qualifying boundaries, plus runs that fail before compaction); the five candidate checks and the candidate format, rehearsed on the pilot; and the measured cost and wall-clock per model.
- **What it can't show.** Effects of repairing one loss at a time, at one compaction, over a capped horizon, in our loop rather than each vendor's real tool. The stress test shows sensitivity, not that natural compaction causes that harm.

## Design

Each boundary forks into up to six arms from one saved snapshot; the main contrast is "S + the lost item" against "S + matched item", so only the item differs.

*Diagram (in the Claude Doc): one boundary snapshot forks into six arms. S and F start at once; S+repair vs S+control (the main contrast) and S−item vs S−control (the stress test) fork after the freeze record.*

S and F start at once; the four other arms wait for the freeze record, so no candidate is chosen after seeing how a continuation went. Two continuations per arm.

## Procedure

**New base runs, kept snapshots.** The pilot's runs deleted their boundary snapshots at cleanup, so its boundaries can't be forked again; they give Experiment 2 its yield estimate and the candidate format. Experiment 3 runs new base trajectories with the pilot's harness, settings and thresholds, on tasks from the pilot's pool (not the held-out six), and keeps each boundary's snapshot until its arms finish. The number of base runs follows from q: about 25/q for 20–30 qualifying boundaries, with an interval for q, scaled up if the first batches yield fewer. Tasks come from the pilot's pool, at most 3 boundaries per task, at least 10 unique tasks. One configuration first: the one with the most usable losses per dollar in Experiment 2; a second only if Sunday's capacity check leaves room. Candidate for that second configuration: luna inside the real Codex CLI on Jacob's ChatGPT subscription ($0 per call), forked by editing Codex's saved session after its own compaction and resuming it. It runs only if a free Saturday feasibility check passes (the edited summary is what the resumed agent sees; one session forks cleanly from a copied workspace; subscription rate limits allow the planned parallelism; measured speed). It would also be the one configuration in each vendor's real tool rather than our loop.

**At each boundary, in order:**

1. **Fork S and F at once** from the snapshot on every boundary that passes the automatic screen, so nothing waits; their outputs stay sealed until the boundary's freeze record is committed. A boundary with no candidate gets no further arms until repair boundaries are covered.
2. **Find candidates.** Experiment 1's pipeline (Max plan) lists the boundary's items and loss calls, as in Experiment 2.
3. **Freeze before any continuation is read.** For each candidate, a record of: the source quote; why it still applies; that it is absent from the resumed input (judged on meaning by Jacob, with string search as supporting evidence); why it isn't trivially recoverable (the kept snapshot makes the repo exact; whether the item can be recovered from it is still Jacob's judgment); the later decision it governs; the observable act that would satisfy or contradict it; the scorer for that act; and the exact input of every arm: the repair text, the control text, and the stress-test edit and its control with their eligibility. A frozen candidate is never replaced once any output of its boundary exists. One usable loss per boundary is used, the first in a seeded order; boundaries with none get the stress test only once repair boundaries are covered.
4. **Fork the rest** from the same snapshot: S+repair and S+control, then (stress test) S−item and S−control.

**How additions and edits are written.**

- **Repair:** the lost item in its source wording, appended to the summary under a neutral line ("Additional context from earlier in this session:"), with no advice added. The first run repairs dropped items only: appending a correct fact beside a contradictory summary tests conflict resolution, not correction. Misstated items are counted and reported; repairing them needs its own correction edit (the wrong sentence replaced by the source wording, against a matched replacement of an irrelevant sentence), added only with a frozen specification.
- **Control:** an item from the same history the summary also lacks but that no later decision depends on, matched in length (±20%), position and the same lead line. If none exists, a matched irrelevant fact in Experiment 1's style.
- **Stress test:** for a kept item of a frozen type, either its sentence removed (dropped) or one detail changed in Experiment 1's misstatement styles (scope narrowed, status flipped, a value changed). Its control removes or changes a sentence of matched length that no later decision depends on.

**Horizon.** Each continuation runs until the agent submits or a call cap set by Sunday's check (planning value: 60 calls), then the pilot's closing turn. A second compaction inside the horizon is recorded and excluded in a sensitivity analysis.

**Outcomes, all fixed in the freeze record.**

- **Primary: the item-specific act.** For each continuation, 1 if it performs the act the lost item's absence predicts (contradicts the decision, redoes the finished step, skips the unfinished one, asks for or forgoes what was granted), else 0. Scored by code where the act is a command, file change or test; otherwise by a judge citing the exact step, then adjudicated by Jacob blind to the arm. Continuations that never reach the decision count as 0, and reaching it is reported separately, by arm.
- **Paired outcomes:** hidden-test progress (the share of DeepSWE's tests passing on the final tree), calls and dollars to finish, and whether the agent re-read a file that restates the item.

**Statistics.** Two continuations per arm per boundary, averaged. Repair benefit = S+control minus S+repair on the act rate, paired by boundary, with a task-clustered bootstrap interval (10,000 draws). Stress-test effect = S−item minus S−control, the same way. F vs S is reported as context. The 20–30 boundaries are a feasibility target, not a powered sample. Power depends on paired discordance, the correlation between a boundary's two continuations and the number of tasks, which Sunday's two boundaries can't estimate. Before bulk runs start, a committed simulation script gives the detectable effect across baseline rates 0.2–0.6, within-boundary correlations 0–0.8 and 8–20 tasks. Two continuations improve measurement but don't double the sample. With fewer than 10 tasks, intervals come from a task-level permutation test instead of the bootstrap. Smaller effects are reported as intervals, not as nulls.

**Checks before any number is read.** Every arm's input differs from S only where intended (diffed by code); snapshots restore identically (file hashes); every freeze record is committed with a timestamp before its boundary's first repair continuation starts. A continuation that crashes or times out is rerun once from the snapshot; if it fails again, its boundary is dropped from that contrast in both arms and logged. Cancelled batches drop whole boundaries.

## Predictions

Written on 23 Oct, before Experiment 2's candidate list exists. The priors come from the pre-pitch probes, which found that deleting summary sentences shifted actions, and from Neel's prior that losses "surely" matter.

| Prediction | Prior | Basis |
| --- | --- | --- |
| Restoring the lost item changes the item-specific act in the predicted direction: the repair effect's interval excludes zero | 50% | In a pre-pitch probe, deleting permission and plan sentences from DeepSeek's summaries cut unapproved actions by 23 points, but on short, constructed tasks |
| The repair effect is at least 0.2 (absolute, on the act rate) | 35% | Long tasks give the agent more chances to rediscover the item by re-reading files |
| The stress-test effect is larger than the repair effect | 60% | Edited items are ones the summarizer judged worth keeping, so likely more salient than natural losses |
| Task progress differs by under 10 points between repair and control | 65% | One item rarely decides whether hidden tests pass |
| Agents re-read a file restating the lost item in at least a third of S continuations | 50% | A guess: the pilot's arm trajectories stay unread until its audit, so there is no data on this yet |
| The full history (F) beats the real summary (S) on the act rate | 70% | F keeps every item; it is context, not the main contrast |

## Outcome scenarios

Checked in this order; the last is the catch-all. A benefit interval below zero (restoring the item raises the predicted act) is reported as its own outcome, and a repair interval that includes zero never by itself shows the agent recovered the item.

| Scenario | What we observe | Answer | What it means for a lab | Next |
| --- | --- | --- | --- | --- |
| **Losses matter** | Repair benefit interval above zero, at least 0.15 | Real compaction losses change what agents do, and restoring the single lost item recovers it | Summary checks that restore dropped items are worth building | Report per item type; the strongest type goes to the optional replication |
| **Losses matter a little** | Benefit interval above zero, under 0.15 | A real but small effect | Item restoration is a modest fix | Report with the stress test for scale |
| **Only induced losses matter** | Repair interval includes zero; stress-test interval excludes zero | Agents are sensitive to losing these kinds of item; this run couldn't show that natural losses change behaviour | Natural compaction may be safer than edits suggest; agents re-derive | Report rediscovery rates (re-reads) as a candidate mechanism |
| **No detectable effect** | Both intervals include zero, with upper bounds under 0.2 | Not detected at this size; not "no effect" | Single-item losses at one compaction rarely change behaviour within the horizon | Report bounds; point to repeated compaction as the open question |
| **Too uncertain** | Intervals include zero and reach past 0.2 | Underpowered | — | Report as inconclusive, with the counts |

## Capacity check, budget, timeline and stops

Money is not the constraint at DeepSeek's or GLM's prices; wall-clock and Jacob's verification time are.

**Cost per boundary, planning values from the pilot** (to be replaced by Sunday's measurements):

| Configuration | Base run to the boundary | 8 core continuations at up to 60 calls | 4 stress-test continuations | Per boundary |
| --- | --- | --- | --- | --- |
| DeepSeek (pi) | $0.15–0.25 | about $2 | about $1 | about $3.50 |
| GLM (pi) | about $0.30 | about $2 | about $1 | about $3.50 |
| Haiku (Claude Code's prompt) | about $1 | about $12 | about $6 | about $19: too costly for the main run |

At about $3.50 a qualifying boundary, 20–30 cost $70–105. Acquisition adds about 25/q base runs at $0.15–0.30 each, plus the S and F continuations already started on screened boundaries that turn out to have no usable loss (about $1 each); q = 0.3 means about 85 base runs and roughly $60–100 of acquisition. The $500 cap leaves room for a second configuration and for re-runs; most of it should stay unspent.

**Sunday's capacity check (binding).** One configuration, 2 boundaries end to end: base run, candidate freeze, all arms to the horizon, grading. It must show restorable snapshots (file hashes match), working scorers on the frozen acts, the wall-clock per boundary at 2 runs in parallel, and the cost and time of screening a boundary that yields nothing. From it: the horizon, the number of boundaries that fits by Tue 23:59 ET with 20% slack, call and time limits per continuation, and the order for cancelling batches (balanced across tasks, from the tail).

**Timeline.**

1. **Sat:** harness changes (keep snapshots; repair, control and edit arms; horizon; the freeze record), with tests and one astra round; this plan reviewed and signed off.
2. **Sun:** the capacity check; the first batch if it passes.
3. **Mon–Tue:** bulk runs. Each boundary waits for Jacob's candidate checks (about 3–5 minutes each) before its repair arms fork; S and F fork at once, so the laptop doesn't sit idle while candidates wait.
4. **Wed:** blinded adjudication of judge-scored acts, per-task results, skeptic checks.

**Jacob's time:** about 3–5 minutes per candidate checked, and candidates that fail count too, so his time scales with screened boundaries, not qualifying ones: 1.5–3 hours over Sun–Tue at a high q. If the projection from Experiment 2's pass rate exceeds that, the automatic screen is tightened or the boundary count cut, Jacob's call; plus about 1 hour of blinded adjudication on Wed. This is the binding constraint; batching candidates twice a day keeps it predictable.

**Stops.** Spending stops at the cap, enforced in code. A boundary whose snapshot fails to restore is dropped and logged. If Sunday's check fails, no bulk runs start; the failure and its fix come back to Jacob.

## Boring explanations, and how the design answers them

| A skeptic says | How the design answers |
| --- | --- |
| The judge invented the loss | Every candidate passes five checks confirmed by Jacob against the source quote and the resumed input; equivalent items count once |
| The item was obsolete or obvious anyway | The applicability and recoverability checks are frozen before any continuation; re-reads of files restating the item are measured |
| Any extra text makes the agent more careful | The control adds an item of matched length, position and lead line; the stress test has its own matched edit control |
| Shorter context changes everything, not this item | Repair and control are both summary-sized contexts; F vs S, which mixes everything, is reported only as context |
| You picked convenient failures | The candidate list is frozen and timestamped before repair arms fork; every screened boundary and every exclusion is listed |
| The agent never reached the decision | Not reaching it counts as not acting, in every arm; reaching it is reported separately by arm, and the main effect isn't conditioned on it |
| One task, model or harness drives it | Intervals resample whole tasks; per-task and per-configuration results are shown; claims stay narrow to the configurations run |
| The judge scored the act the way you hoped | Code scores the act wherever it is a command, file change or test; otherwise Jacob adjudicates blind to the arm |

## Risks and limitations

- **Usable losses may be scarce.** The pilot suggests live histories hold few user items and the agent's own state is often re-readable from the repo. Experiment 2's count is the early warning; more base runs are the first remedy, and Experiment 4 a fallback Jacob may choose. Excluding recoverable losses also narrows the population, which can make effects larger than for all natural losses.
- **One loss at a time.** Real summaries lose several items at once, which may interact, so single-item repair may over- or understate the total effect.
- **Our loop, not each vendor's tool.** The summarizers are replays of each harness's prompt and keep rules in a common agent loop, as in the pilot.
- **A capped horizon.** Decisions that come after the cap are missed; the cap and the share of continuations that hit it are reported.
- **Jacob's verification is the bottleneck,** and he will later see outcomes. The freeze records are committed before repair arms fork, so his checks can't be shaped by results.
- **The stress test is not evidence about natural compaction.** It is reported in its own section, never pooled with the repair effect.
- **Power.** Power comes from a simulation across plausible correlations and task counts, not from Sunday's two boundaries; 20–30 boundaries is a feasibility target.

## Review history

| Round | Verdict | What changed |
| --- | --- | --- |
| astra r1 (24 Oct, combined with the roadmap and the close-out) | 4 BLOCKING, 3 MINOR, 1 NIT across the three plans | Sized by the share of boundaries with a usable loss; dropped items only in the first run; exact arm inputs frozen and S and F sealed before selection; power by simulation, with a small-task fallback; effect sign and scenarios made consistent |
| Jacob | pending | — |
