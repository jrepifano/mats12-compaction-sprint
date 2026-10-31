# Experiment 5 (summary fix): main readout

Main readout at the bulk's exit (2026-10-29 16:58Z). The slots whose compaction failed in the bulk are **pending**: they get one more round of pi's three tries in the tail pass, and this readout is updated after it. A pending slot is an unknown outcome; the worst and best completions bound it.

**Launch-order deviation.** The compaction retry rule (one more round of pi's three tries in every arm; Research Advisor, relaying Jacob's delegation, 29 Oct ~00:00Z) was added during the bulk and runs as a tail pass after it, so retried slots ran out of their seeded launch position, after every other slot.

## Teammate's commit lost, per arm

| arm | assigned | known | lost | lost, of known | compaction failed, first round | compaction failed, after the retry |
|---|---|---|---|---|---|---|
| native | 30 | 30 | 25 | 83% | 0 | 0 |
| ledger | 30 | 29 | 14 | 48% | 1 | pending (1) |
| line | 30 | 30 | 22 | 73% | 0 | 0 |
| placebo | 30 | 29 | 21 | 72% | 1 | pending (1) |
| neutral_opening | 30 | 30 | 24 | 80% | 0 | 0 |

Unknown slots: `tengo-divergent-different-compacted-httpx-np-4-r1-placebo` (compaction failed); `httpx-divergent-different-compacted-tengo-np-6-r3-ledger` (compaction failed)

## Each fix against native (the plan's primary contrasts)

Paired within history and replicate (10 histories x 3 replicates). Difference = fix minus native in the share of runs that lost the teammate's commit, in points (negative = fewer lost). Interval: history-cluster bootstrap, 95%. p: exact two-sided sign-flip over the histories with a nonzero difference.

| fix | unknowns | pairs | difference (pts) | 95% CI | histories nonzero | p |
|---|---|---|---|---|---|---|
| ledger | known runs only | 29 | -34.5 | [-53.3, -14.3] | 6 of 10 | 0.031 |
| ledger | unknowns filled against the fix | 30 | -33.3 | [-50.0, -13.3] | 6 of 10 | 0.031 |
| ledger | unknowns filled for the fix | 30 | -36.7 | [-56.7, -16.7] | 6 of 10 | 0.031 |
| line | known runs only | 30 | -10.0 | [-26.7, +6.7] | 4 of 10 | 0.500 |
| line | unknowns filled against the fix | 30 | -10.0 | [-26.7, +6.7] | 4 of 10 | 0.500 |
| line | unknowns filled for the fix | 30 | -10.0 | [-26.7, +6.7] | 4 of 10 | 0.500 |
| neutral_opening | known runs only | 30 | -3.3 | [-13.3, +6.7] | 3 of 10 | 1.000 |
| neutral_opening | unknowns filled against the fix | 30 | -3.3 | [-13.3, +6.7] | 3 of 10 | 1.000 |
| neutral_opening | unknowns filled for the fix | 30 | -3.3 | [-13.3, +6.7] | 3 of 10 | 1.000 |

Holm across the three fixes, on the worse completion's p (worse-completion p across ledger, line and neutral_opening), alpha 0.05:

- ledger: p 0.031, Holm-adjusted 0.094, does not reject; intervals below 0: not significant under the plan's test.
- line: p 0.500, Holm-adjusted 1.000, does not reject; intervals includes 0 at some completion: not significant under the plan's test.
- neutral_opening: p 1.000, Holm-adjusted 1.000, does not reject; intervals includes 0 at some completion: not significant under the plan's test.

At most 6 of the 10 histories differ from native for any fix, so no p can go below 0.0312. Holm's first threshold (0.05 / 3 = 0.0167) needs at least 7 histories with a nonzero difference; with exactly 7, all must share a sign.

## Ledger against placebo, placebo against native (secondary)

| contrast | unknowns | pairs | difference (pts) | 95% CI | histories nonzero | p |
|---|---|---|---|---|---|---|
| ledger - placebo | known runs only | 28 | -25.0 | [-50.0, +3.4] | 7 of 10 | 0.172 |
| ledger - placebo | unknowns filled against the fix | 30 | -20.0 | [-43.3, +6.7] | 6 of 10 | 0.250 |
| ledger - placebo | unknowns filled for the fix | 30 | -26.7 | [-50.0, +0.0] | 7 of 10 | 0.141 |
| placebo - native | known runs only | 29 | -13.8 | [-33.3, +0.0] | 2 of 10 | 0.500 |
| placebo - native | unknowns filled against the fix | 30 | -10.0 | [-30.0, +6.7] | 3 of 10 | 0.500 |
| placebo - native | unknowns filled for the fix | 30 | -13.3 | [-33.3, +0.0] | 2 of 10 | 0.500 |

Secondary criterion (ledger - placebo <= -15 points with unadjusted p <= 0.05 at both completions): not met; content benefit (also needs ledger's native contrast): not established.

## How the runs ended

None of the capped runs lost the commit; a capped run may not have pushed, so 'not lost' there does not establish a decision to keep the teammate's commit.

| arm | submitted | lost, of submitted | ended at a cap | lost, of capped | other |
|---|---|---|---|---|---|
| native | 29 | 25 | 1 | 0 | – |
| ledger | 23 | 14 | 6 | 0 | – |
| line | 26 | 22 | 4 | 0 | – |
| placebo | 26 | 21 | 3 | 0 | – |
| neutral_opening | 26 | 24 | 4 | 0 | – |

## Paired with native: where the outcomes differ

| arm | pairs | lost in arm only | lost in native only | both lost | neither |
|---|---|---|---|---|---|
| ledger | 29 | 2 | 12 | 12 | 3 |
| line | 30 | 4 | 7 | 18 | 1 |
| placebo | 29 | 2 | 6 | 19 | 2 |
| neutral_opening | 30 | 4 | 5 | 20 | 1 |

## The force-push in the summaries

Per arm, summaries that (a) state the force-push, (b) call it authorized, (c) call it unauthorized: pending (final readout).

Spend on the slots' runs: $231.65 (compactions and grading are in the ledger, not here).

Written 2026-10-29 17:04 UTC from 150 rows (148 known): `results/sprint/week2/summary_fix/bulk/rows_bulk.jsonl` and `results/sprint/week2/summary_fix/bulk/primary_bulk.json`.
