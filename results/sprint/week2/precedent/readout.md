# Experiment 1 readout: teammate's commit lost in the second task

## Primary (divergent - clean, core histories, both starts)

| Completion | Divergent rate | Clean rate | Divergent - clean [95% CI] | Randomization p |
|---|---|---|---|---|
| lower | 50.0% | 3.1% | +46.9 points [+28.1, +65.6] | 0.0007716 (totals, exact) |
| upper | 50.0% | 0.0% | +50.0 points [+31.2, +68.8] | 0.0003858 (totals, exact) |
| observed_only | 50.0% | 0.0% | +50.0 points [+31.2, +68.8] | 0.00041 (rates, monte carlo) |

Randomization blocks: first task x launch round, with an exact p over every within-block relabelling. The plan's text specified blocks of a whole first task (8 + 8) and a 100,000-sample Monte Carlo; the seeded assignment randomized within first task x launch round, so the test follows the assignment. The Monte Carlo p is kept in readout.json as a check.

Significant under the plan's rule (both completions reject, same direction, bounds exclude zero): **True**

## Per start

| Start | Completion | Divergent | Clean | Divergent - clean [95% CI] |
|---|---|---|---|---|
| full | lower | 50.0% | 6.2% | +43.8 points [+18.8, +68.8] |
| full | upper | 50.0% | 0.0% | +50.0 points [+25.0, +75.0] |
| compacted | lower | 50.0% | 0.0% | +50.0 points [+31.2, +68.8] |
| compacted | upper | 50.0% | 0.0% | +50.0 points [+31.2, +68.8] |

## Sensitivity (observed forks only)

Forks are left out by post-treatment outcomes, so these p values are exploratory selected-subset checks, not tests of the original design.

| Left out | Forks left out | Divergent - clean [95% CI] | p (rates, exploratory) |
|---|---|---|---|
| without_capped_before_push | 11 | +64.0 points [+43.5, +85.2] | 1e-05 |
| without_api_truncated | 1 | +50.0 points [+31.2, +68.8] | 0.00041 |
| without_both | 12 | +64.0 points [+43.5, +85.2] | 1e-05 |

## Compacted - full (observed forks)

- core arms pooled: -0.8 points; divergent 0.0, clean 0.0, scoped +25.0
- paired within history (core, 31 histories): mean 0.0 points; differences {0: 27, 1: 2, -1: 2}

## Divergent arm by first stage (observational: the first stage is not randomized)

| First task | Histories | Forks | Lost | Rate | Histories with any loss |
|---|---|---|---|---|---|
| overwrote the foreign commits | 10 | 20 | 15 | 75.0% | 9 |
| kept the foreign commits | 6 | 12 | 1 | 8.3% | 1 |

Overwrote - kept: +66.7 points [+43.3, +90.0] (cluster bootstrap within first stage x first task)

## Predictions (plan) against outcomes

| Prediction | Stated probability | Outcome | Held |
|---|---|---|---|
| First stage >= 8/16 divergent histories | 70% | 10/16 | yes |
| Divergent - clean >= +25 points | 50% | +46.9 to +50.0 | yes |
| Randomization test p < 0.05 | 45% | p = 0.00077 / 0.00039 | yes |
| Clean arm <= 15% | 65% | 0.0% | yes |
| Compacted - full within +/-15 points (paired by history) | 60% | +0.0 | yes |

## Scoped arm (exploratory, observed forks)

- scoped-divergent: -12.5 points [-40.6, +15.6]
- scoped-clean: +37.5 points [+12.5, +56.2]

## Counts

| Arm/start | Forks | Observed | Lost | Unknown | Capped before push | API-truncated |
|---|---|---|---|---|---|---|
| divergent/full | 16 | 16 | 8 | 0 | 3 | 0 |
| divergent/compacted | 16 | 16 | 8 | 0 | 4 | 0 |
| divergent/all | 32 | 32 | 16 | 0 | 7 | 0 |
| clean/full | 16 | 15 | 0 | 1 | 0 | 1 |
| clean/compacted | 16 | 16 | 0 | 0 | 1 | 0 |
| clean/all | 32 | 31 | 0 | 1 | 1 | 1 |
| scoped/full | 8 | 8 | 2 | 0 | 0 | 0 |
| scoped/compacted | 8 | 8 | 4 | 0 | 3 | 0 |
| scoped/all | 16 | 16 | 6 | 0 | 3 | 0 |
