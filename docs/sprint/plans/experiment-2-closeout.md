# Experiment 2 close-out: what do live summaries lose?

2026-10-23 · Jacob Epifano

The pilot's runs finish tonight; this close-out runs Experiment 1's loss analysis on its live boundaries and freezes a hand-verified list of real losses, which Experiment 3 then repairs.

## Review summary (proposed, v0.1, not yet signed off)

| Row | What it says |
| --- | --- |
| **Decision it informs** | Which real losses Experiment 3 repairs, and whether repair or the induced-edit stress test leads: at least 10 usable losses means repair leads; fewer means Experiment 4 runs to supply more. Also whether Experiment 1's judge can be trusted on live histories |
| **Parts** | (A) finish the pilot as specified: retention labels, the gate, audit, release; (B) Experiment 1's pipeline on every valid live boundary; (C) 30 hand-label cards from Jacob on live-loss calls; (D) a frozen list of candidate losses for Experiment 3, each with its quote, applicability, availability and the later decision it governs; run cost and wall-clock |
| **Primary measures** | Share of actionable items lost per live boundary, by item type and by model (each model has its own summarizer, so the two aren't separable); judge precision on live loss calls from the hand labels |
| **Predictions** | Live boundaries lose 5–20% of items (60%); task state and decisions make up most losses (70%); live precision is within 0.15 of Experiment 1's 0.72 (60%) |
| **Stops** | Every candidate Experiment 3 uses is verified by Jacob, whatever the judge's precision. If live loss precision is below 0.5, the judge is only used to find candidates, never to report rates |
| **Cost** | $0 billed for the pipeline (Max plan); about $10 for the pilot's last runs; Jacob about 80 min (labels, spot check, audit) |
| **Changed since last version** | First version |
| **Reviews** | None yet; one astra round before sign-off |
| **Open for Jacob** | The hand-label count (30) and whether cut points join the descriptive analysis |

## Research question

**When live coding agents compact at a configured threshold, which actionable items do their summaries lose, and does Experiment 1's judge find them as reliably on live histories as on recorded ones?**

- **Where it comes from.** The proposal's first question asked how often compaction drops, mis-summarizes or invents actionable facts. Experiment 1 answered it on 650 recorded compactions, mostly Claude Code's own. The live boundaries add the three other summarizers we run (pi, Codex local, and Claude Code's prompt on Haiku), on fresh long tasks, at a known threshold.
- **What Experiment 3 needs from it.** Experiment 3 repairs real losses and, as a stress test, removes or misstates kept items. It needs the verified losses themselves, which item types live summaries hold and lose, how losses look (dropped outright, or misstated), and whether the judge's "lost" calls are right often enough to find candidates with.
- **What it is not.** It doesn't test behaviour. The pilot's planted-rule outcomes stay sealed and are read separately, after the audit, as the pilot planned.

## What we will do

Four parts, all on data the pilot already has or will have by about 11 PM tonight; only part A spends money.

| Part | What it does | Produces | Cost | When |
| --- | --- | --- | --- | --- |
| **A. Finish the pilot** | The last runs; retention labels for the planted rules; Jacob's 24-card spot check of the retention judge; the gate; the 20-card audit and release | The planted-rule result per channel; cost, cache and wall-clock per model | about $10; Jacob about 50 min | tonight, Sat |
| **B. Experiment 1's pipeline on live boundaries** | Item listing, kept/dropped/misstated grading, the second look and availability on every valid pilot boundary (up to 24) | Items and loss calls per boundary | $0 (Max plan), about 1–2 h of judging | Sat morning |
| **C. Hand labels** | 30 seeded cards from Jacob on live calls: 20 the judge called lost, 10 it called kept | Live precision, and a first look at misses | Jacob about 30 min | Sat |
| **D. Handoff to Experiment 3** | Counts by item type, model and grade; random raw examples; the frozen candidate list (quote, applicability, availability, governed decision); the edit styles the stress test copies | The usable-loss count and the inputs Experiment 3 freezes | $0 | Sat |

The offline cut points from the pilot's exploration (about 50 cuts from 9 trajectories) are judged the same way but reported only as descriptive context: they come from few trajectories and are strongly correlated. The edited-history screens are left out.

## Procedure

Experiment 1's pipeline runs unchanged except for three adaptations to live boundaries, each named below.

**Inputs.** Each valid pilot boundary saved the full pre-compaction history, the summary and the exact post-compaction input the agent resumed from, in Experiment 1's record format. Haiku's first scriggo attempt is excluded (its summary was empty, a recorded harness failure); DeepSeek's dynamodb boundary is included, since only its arms failed (the API account ran dry after the boundary).

**Reused unchanged from Experiment 1.** The frozen item and claim prompts, Haiku 5.5 on the Max plan at medium effort, the three passes (list items without seeing the summary; grade each kept, dropped or misstated; list summary claims and check them), the search-tool second look on every positive call, and the definition of lost: dropped or misstated, and not available any other way.

**Adapted, and why.**

1. **Availability uses the real post-compaction input.** Experiment 1 inferred what survived from each harness's rules. Here we have the exact input, so an item is available if it appears in the kept messages, the re-attached AGENTS.md or Codex's kept user turns. This is a stricter, measured version of the same rule. (The saved records leave that field empty, so it is filled from the saved post-compaction input first.)
2. **Planted content is tagged.** Items that are the pilot's four planted rules are marked and reported apart, since the retention judge already covers them.
3. **No privacy restriction.** DeepSWE is public and the runs are ours, so every judge may read them.

**Sample.** Up to 24 boundaries: 6 tasks × 4 models, 2 tasks per channel for each model. Each boundary is one independent trajectory. Intervals resample whole tasks.

**Hand labels.** A seeded draw of 30 cards from the held-out half of the pilot's existing split: 20 items the judge called lost and 10 it called kept, spread across models. Each card shows the item, its quote from the history, the full post-compaction input with a find box, and asks: is this an actionable item; is it in the resumed input; if not, is it re-readable. The judge's verdict is hidden. The page uses Experiment 1's label template.

**Measures.**

- Lost share of items per boundary, by item type and model, with task-clustered intervals.
- Live loss precision: the share of judge "lost" calls Jacob confirms, with a Wilson interval.
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

The count of usable losses decides Experiment 3's shape; the scenarios are checked in this order.

**A usable loss** is an item that meets all four tests, each recorded before any continuation is read:

1. the judge called it lost, and Jacob confirms it is absent from the resumed input;
2. it still applied at the boundary (not superseded or finished);
3. it can't be trivially recovered (not restated in a file the agent rereads in its normal work, as far as the boundary's repo state shows);
4. a later decision it governs can be named from the task and the repo state at the boundary, with the act that would satisfy or contradict it.

| Scenario | What we observe | What it means | Experiment 3 |
| --- | --- | --- | --- |
| **Enough real losses** | At least 10 usable losses, from at least 5 tasks | Live compaction loses items that later decisions depend on, often enough to test | Repair leads; induced edits run as the labelled stress test |
| **Some real losses** | 4–9 usable losses | Real but sparse | Repair runs on all of them; Experiment 4 runs to add more; edits carry most of the statistical weight, with the caveat stated |
| **Few real losses** | Fewer than 4 | Live summaries rarely drop items that matter later, in this setup | Experiment 4 runs; the week's headline is the edit stress test plus the natural-loss rates, stated as such |
| **Judge unreliable live** | Live loss precision below 0.5 | The judge over-calls on live histories | Whatever the count, the judge only finds candidates; no live loss rate is reported from it |

In every scenario the loss rates by item type and model are reported, with the planted-rule result alongside.

## Budget, timeline and stopping points

Everything new runs on the Max plan at $0 billed; the only money is the pilot's last runs, inside its existing $80 cap ($50 spent at 8 PM ET).

1. **Tonight, by about 11 PM ET:** the last 4 pilot runs finish. Then the retention judge on every boundary, and the spot-check page.
2. **Sat morning:** Experiment 1's pipeline on every valid boundary, about 1–2 h on Max, unattended. The 30-card live-loss page and the candidate list are built from it.
3. **Sat, Jacob's time (about 90 min):** the retention spot check (24 cards, \~30 min), the live-loss cards (30, \~30 min), the release audit (20 cards, \~20 min), and confirming the frozen candidates (\~10 min).
4. **Sat afternoon:** the gate, the release, the loss tables, and the usable-loss count. Experiment 3's plan is finalized from it.

**Checkpoints where work stops for Jacob:**

- after the candidate list is built and before it is frozen;
- after the usable-loss count, before Experiment 3 or 4 spends anything.

**Stops:** if the Max plan's usage window runs out mid-judging, the run pauses and resumes; nothing is moved to a paid route without asking.

## Risks and limitations

- **One judge, one family.** As in Experiment 1, every loss call is Haiku's. The hand labels measure its live precision; recall stays unmeasured beyond the 10 kept-call cards.
- **Few boundaries, four configurations.** Up to 24 boundaries, about 6 per model. Each model is paired with its own summarizer, so model and summarizer can't be separated.
- **The candidate list is judged by people who will later see outcomes.** It is frozen, with exclusions logged, before any Experiment 3 continuation exists, and the pilot's sealed arms stay sealed until the audit.
- **The pilot's arms are 20 calls.** A later decision that a lost item governs may come after that, so the pilot's own arms can't show whether the losses mattered; that is Experiment 3's job.
- **Live histories hold little user content.** Losses will mostly be the agent's own task state and decisions. That fits what live coding agents carry, but it means few user grants or decisions to repair, which Experiment 4 exists to cover.
- **The availability rule is stricter than Experiment 1's.** Using the exact resumed input is more accurate, but makes the live and recorded rates not directly comparable; both definitions are reported.

## Review history

| Round | Verdict | What changed |
| --- | --- | --- |
| astra r1 | pending | — |
| Jacob | pending | — |
