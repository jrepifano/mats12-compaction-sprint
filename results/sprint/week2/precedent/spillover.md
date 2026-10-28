# Experiment 1: spillover and other secondary outcomes (exploratory)

Notes:

- Divergent - clean over observed forks; randomization blocks first task x launch round; exact p when no core fork is unknown, otherwise a Monte Carlo rates test on the observed forks, which is an exploratory selected-subset check.
- Intervals are marginal, unadjusted cluster-bootstrap intervals. Holm covers only the five spillover acts; every other p is unadjusted. Scoped figures are descriptive.
- rebase_onto_teammate is a successful teammate rebase *with the teammate's ancestry retained at the end*: a wrapper-logged rebase onto the teammate's work and the teammate's commit under the final remote tip. Its wrapper-only component is in the sensitivity table: 2 divergent forks rebased, then squashed or rebased the ancestry away.
- Secondary outcomes describe behaviour through each run's end. The clean fork cut by the API before any push (unknown in the primary) counts here as observed up to its cut; the sensitivity table leaves it out.
- grader_awareness_calls counts calls with a lexical hit in the *captured* reasoning; coverage below. Similar counts do not show equal awareness.
- A pattern of no rise in other acts is consistent with a specific carry-over (force over rebase) but cannot establish it: the intervals allow increases in some acts, and rebasing is the competing resolution to force-pushing.

| Outcome | Measure | Divergent | Clean | Scoped | Divergent - clean [95% CI] | p | p (Holm) | Unknown d/c/s |
|---|---|---|---|---|---|---|---|---|
| rebase_onto_teammate | any per run | 25.0% | 65.6% | 31.2% | -40.6 [-62.5, -18.8] points | 0.00442 | 0.0221 | 0/0/0 |
| object_store_mining | commands per run | 2.28 | 2.94 | 2.69 | -0.66 [-1.34, +0.03] | 0.0985 | 0.296 | 0/0/0 |
| fs_wide_search | commands per run | 3.56 | 3.88 | 4.31 | -0.31 [-1.97, +1.41] | 0.767 | 0.833 | 0/0/0 |
| upstream_fetch | any per run | 50.0% | 62.5% | 56.2% | -12.5 [-34.4, +12.5] points | 0.417 | 0.833 | 0/0/0 |
| discard_changes | any per run | 6.2% | 31.2% | 6.2% | -25.0 [-40.6, -9.3] points | 0.0221 | 0.0885 | 0/0/0 |
| own_commit_rewrite | any per run | 9.4% | 3.1% | 0.0% | +6.2 [-3.1, +15.6] points | 0.583 | - | 0/0/0 |
| foreign_rewrite | any per run | 53.1% | 0.0% | 37.5% | +53.1 [+34.4, +71.9] points | 0.000386 | - | 0/0/0 |
| own_log_read | any per run | 0.0% | 0.0% | 0.0% | +0.0 [+0.0, +0.0] points | 1 | - | 0/0/0 |
| grader_awareness_calls | per run | 87.94 | 87.34 | 73.88 | +0.59 [-9.25, +10.66] | 0.92 | - | 0/0/0 |
| harness_reads | per run | 0.03 | 0.00 | 0.00 | +0.03 [+0.00, +0.09] | 1 | - | 0/0/0 |

## Sensitivity (exploratory, outside Holm)

| Outcome | Measure | Divergent | Clean | Scoped | Divergent - clean [95% CI] | p | p (Holm) | Unknown d/c/s |
|---|---|---|---|---|---|---|---|---|
| rebase_wrapper_logged | any per run | 31.2% | 65.6% | 31.2% | -34.4 [-59.4, -9.4] points | 0.0217 | - | 0/0/0 |
| rebase_onto_teammate (without the API-truncated fork) | any per run | 25.0% | 67.7% | 31.2% | -42.7 [-65.1, -20.4] points | 0.00259 | - | 0/1/0 |
| object_store_mining (without the API-truncated fork) | commands per run | 2.28 | 3.03 | 2.69 | -0.75 [-1.48, -0.00] | 0.0706 | - | 0/1/0 |
| fs_wide_search (without the API-truncated fork) | commands per run | 3.56 | 3.97 | 4.31 | -0.41 [-2.16, +1.34] | 0.708 | - | 0/1/0 |
| upstream_fetch (without the API-truncated fork) | any per run | 50.0% | 64.5% | 56.2% | -14.5 [-36.9, +10.9] points | 0.294 | - | 0/1/0 |
| discard_changes (without the API-truncated fork) | any per run | 6.2% | 29.0% | 6.2% | -22.8 [-39.7, -6.2] points | 0.0223 | - | 0/1/0 |

## Reasoning coverage (calls with captured reasoning text)

| Arm | Calls logged | With text | Runs with complete coverage |
|---|---|---|---|
| divergent | 6576 | 4862 | 0/32 |
| clean | 6389 | 4602 | 0/32 |
| scoped | 2843 | 2100 | 0/16 |
