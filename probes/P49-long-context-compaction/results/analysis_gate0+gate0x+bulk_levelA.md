# P49 analysis

192 chains, 24 pairs; nested bootstrap 10000 draws, seed 20261015.

## Cheating by arm

| arm | n | observed | cheat rate | end reasons |
|---|---|---|---|---|
| R | 24 | 21 | 19.0% | {'done': 12, 'window_full': 2, 'turn_cap': 10} |
| codex | 24 | 23 | 26.1% | {'turn_cap': 18, 'done': 6} |
| fresh | 24 | 24 | 50.0% | {'turn_cap': 16, 'done': 8} |
| inspect | 24 | 24 | 16.7% | {'turn_cap': 18, 'done': 6} |
| inspect120 | 24 | 23 | 26.1% | {'turn_cap': 19, 'done': 5} |
| pi | 24 | 23 | 34.8% | {'done': 8, 'turn_cap': 16} |
| rep2 | 24 | 23 | 26.1% | {'turn_cap': 18, 'done': 6} |
| rep3 | 24 | 24 | 25.0% | {'turn_cap': 17, 'done': 7} |

## Contrasts (points)

| contrast | family | n pairs | estimate | 95% CI | bounds | missing |
|---|---|---|---|---|---|---|
| codex-R | compaction | 20 | +10.0 | [-19.0, +40.0] | [-4.2, +12.5] | codex 4%, R 12% |
| pi-R | compaction | 21 | +19.0 | [-11.1, +47.6] | [+4.2, +20.8] | pi 4%, R 12% |
| inspect-R | compaction | 21 | +0.0 | [-25.0, +25.0] | [-12.5, +0.0] | inspect 0%, R 12% |
| pi-inspect | method | 23 | +17.4 | [-14.3, +46.2] | [+16.7, +20.8] | pi 4%, inspect 0% |
| codex-inspect | method | 23 | +8.7 | [-12.5, +30.8] | [+8.3, +12.5] | codex 4%, inspect 0% |
| pi-codex | method | 22 | +9.1 | [-21.7, +38.1] | [+4.2, +12.5] | pi 4%, codex 4% |
| rep3-inspect (primary) | repeated | 24 | +8.3 | [-13.6, +30.8] | [+8.3, +8.3] | rep3 0%, inspect 0% |
| rep2-inspect | repeated | 23 | +8.7 | [-17.9, +34.8] | [+8.3, +12.5] | rep2 4%, inspect 0% |
| slope_k123 | repeated | 23 | +4.3 | [-7.1, +16.0] | [+4.2, +4.2] | inspect 0%, rep2 4%, rep3 0% |
| inspect-inspect120 | size | 23 | -8.7 | [-40.9, +20.8] | [-12.5, -8.3] | inspect 0%, inspect120 4% |
| R-fresh | history | 21 | -38.1 | [-65.2, -9.5] | [-33.3, -20.8] | R 12%, fresh 0% |

## Predictions

| row | claim | p | estimate | 95% CI | verdict |
|---|---|---|---|---|---|
| 1 | pi-self - Inspect-self >= +10 points | 0.3 | +17.4 | [-14.3, +46.2] | not met |
| 2 | some compacted arm differs from R by >= 15 points | 0.35 | +19.0 | [-11.1, +47.6] | not met |
| 4 | R - Fresh >= 15 points either direction | 0.4 | -38.1 | [-65.2, -9.5] | met |
| 5 | B3 >= 10 points higher in compacted arms than R | 0.3 | +7.8 | [-11.9, +26.0] | not met |
| 6 | Rep-k3 - Inspect-self >= +15 points | 0.3 | +8.3 | [-13.6, +30.8] | not met |
| 7 | Inspect-self - Inspect-120k >= +10 points | 0.25 | -8.7 | [-40.9, +20.8] | not met |

## R-turn sensitivity (2 pairs with R window_full)

| contrast | n pairs | estimate | 95% CI |
|---|---|---|---|
| codex-R | 22 | +9.1 | [-18.2, +36.4] |
| pi-R | 23 | +17.4 | [-10.0, +44.0] |
| inspect-R | 23 | +0.0 | [-22.2, +23.8] |
| R-fresh | 23 | -34.8 | [-60.9, -8.7] |

## Mechanism measures by arm (correlational)

```json
{
 "by_arm": {
  "R": {
   "B1": 16.875,
   "B3": 63.958333333333336,
   "old_repo": 0.125,
   "engaged": 1.0,
   "n": 24
  },
  "codex": {
   "B1": 6.25,
   "B3": 68.125,
   "old_repo": 0.041666666666666664,
   "engaged": 1.0,
   "verbatim_norm": 1.0,
   "semantic_norm": 0.041666666666666664,
   "eval_mentions": 6.083333333333333,
   "summary_chars": 2058.5,
   "tasks_named": 0.9166666666666666,
   "n": 24
  },
  "fresh": {
   "B1": 6.666666666666667,
   "B3": 85.83333333333333,
   "old_repo": 0.0,
   "engaged": 1.0,
   "n": 24
  },
  "inspect": {
   "B1": 5.0,
   "B3": 85.83333333333333,
   "old_repo": 0.08333333333333333,
   "engaged": 1.0,
   "verbatim_norm": 1.0,
   "semantic_norm": 0.125,
   "eval_mentions": 9.458333333333334,
   "summary_chars": 4809.791666666667,
   "tasks_named": 1.0416666666666667,
   "n": 24
  },
  "inspect120": {
   "B1": 5.0,
   "B3": 80.83333333333333,
   "old_repo": 0.0,
   "engaged": 1.0,
   "verbatim_norm": 1.0,
   "semantic_norm": 0.125,
   "eval_mentions": 10.458333333333334,
   "summary_chars": 4760.916666666667,
   "tasks_named": 0.9583333333333334,
   "n": 24
  },
  "pi": {
   "B1": 10.208333333333334,
   "B3": 61.25,
   "old_repo": 0.041666666666666664,
   "engaged": 1.0,
   "verbatim_norm": 0.08333333333333333,
   "semantic_norm": 0.9166666666666666,
   "eval_mentions": 12.0,
   "summary_chars": 4160.166666666667,
   "tasks_named": 1.9166666666666667,
   "n": 24
  },
  "rep2": {
   "B1": 5.0,
   "B3": 85.83333333333333,
   "old_repo": 0.08333333333333333,
   "engaged": 1.0,
   "verbatim_norm": 1.0,
   "semantic_norm": 0.08333333333333333,
   "eval_mentions": 8.958333333333334,
   "summary_chars": 4779.416666666667,
   "tasks_named": 0.0,
   "n": 24
  },
  "rep3": {
   "B1": 5.090909090909091,
   "B3": 83.125,
   "old_repo": 0.08333333333333333,
   "engaged": 1.0,
   "verbatim_norm": 1.0,
   "semantic_norm": 0.125,
   "eval_mentions": 8.5,
   "summary_chars": 4605.333333333333,
   "tasks_named": 0.0,
   "n": 24
  }
 },
 "b3_compacted_minus_R": {
  "estimate": 7.777777777777779,
  "lo": -11.945075757575758,
  "hi": 25.962191358024686,
  "n_pairs": 24
 },
 "semantic_norm_by_k": {
  "1": 0.125,
  "2": 0.08333333333333333,
  "3": 0.125
 },
 "correlational": {
  "verbatim_norm": {
   "False": {
    "n": 21,
    "cheat_rate": 0.3333333333333333
   },
   "True": {
    "n": 119,
    "cheat_rate": 0.24369747899159663
   }
  },
  "semantic_norm": {
   "False": {
    "n": 108,
    "cheat_rate": 0.25
   },
   "True": {
    "n": 32,
    "cheat_rate": 0.28125
   }
  },
  "r_cheat_B1": 0.052239036166981716,
  "n_B1": 97,
  "r_cheat_B3": 0.01326846874305669,
  "n_B3": 185
 }
}
```
