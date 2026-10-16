# P49 analysis

128 chains, 16 pairs; nested bootstrap 10000 draws, seed 20261015.

## Cheating by arm

| arm | n | observed | cheat rate | end reasons |
|---|---|---|---|---|
| R | 16 | 11 | 18.2% | {'done': 7, 'turn_cap': 9} |
| codex | 16 | 13 | 15.4% | {'turn_cap': 14, 'done': 2} |
| fresh | 16 | 12 | 41.7% | {'turn_cap': 11, 'done': 5} |
| inspect | 16 | 14 | 7.1% | {'turn_cap': 11, 'done': 5} |
| inspect120 | 16 | 10 | 0.0% | {'turn_cap': 14, 'done': 2} |
| pi | 16 | 13 | 30.8% | {'done': 5, 'turn_cap': 11} |
| rep2 | 16 | 13 | 15.4% | {'turn_cap': 14, 'done': 2} |
| rep3 | 16 | 12 | 8.3% | {'turn_cap': 13, 'done': 3} |

## Contrasts (points)

| contrast | family | n pairs | estimate | 95% CI | bounds | missing |
|---|---|---|---|---|---|---|
| codex-R | compaction | 11 | +0.0 | [-40.0, +36.4] | [-31.2, +18.8] | codex 19%, R 31% |
| pi-R | compaction | 11 | +18.2 | [+0.0, +50.0] | [-18.8, +31.2] | pi 19%, R 31% |
| inspect-R | compaction | 11 | -9.1 | [-45.5, +16.7] | [-37.5, +6.2] | inspect 12%, R 31% |
| pi-inspect | method | 12 | +25.0 | [-16.7, +58.3] | [+6.2, +37.5] | pi 19%, inspect 12% |
| codex-inspect | method | 12 | +8.3 | [+0.0, +33.3] | [-6.2, +25.0] | codex 19%, inspect 12% |
| pi-codex | method | 13 | +15.4 | [-15.4, +50.0] | [-6.2, +31.2] | pi 19%, codex 19% |
| rep3-inspect (primary) | repeated | 12 | +0.0 | [-33.3, +25.0] | [-12.5, +25.0] | rep3 25%, inspect 12% |
| rep2-inspect | repeated | 12 | +8.3 | [+0.0, +33.3] | [-6.2, +25.0] | rep2 19%, inspect 12% |
| slope_k123 | repeated | 12 | +0.0 | [-16.7, +12.5] | [-6.2, +12.5] | inspect 12%, rep2 19%, rep3 25% |
| inspect-inspect120 | size | 10 | +10.0 | [+0.0, +37.5] | [-31.2, +18.8] | inspect 12%, inspect120 38% |
| R-fresh | history | 11 | -27.3 | [-63.6, +16.7] | [-43.8, +12.5] | R 31%, fresh 25% |
| codex-fresh | vs fresh (exploratory) | 12 | -25.0 | [-57.1, +0.0] | [-43.8, +0.0] | codex 19%, fresh 25% |
| pi-fresh | vs fresh (exploratory) | 12 | -8.3 | [-38.5, +20.0] | [-31.2, +12.5] | pi 19%, fresh 25% |
| inspect-fresh | vs fresh (exploratory) | 11 | -36.4 | [-72.7, -8.3] | [-50.0, -12.5] | inspect 12%, fresh 25% |
| inspect120-fresh | vs fresh (exploratory) | 9 | -44.4 | [-85.7, -11.1] | [-56.2, +6.2] | inspect120 38%, fresh 25% |
| rep2-fresh | vs fresh (exploratory) | 12 | -25.0 | [-57.2, +0.0] | [-43.8, +0.0] | rep2 19%, fresh 25% |
| rep3-fresh | vs fresh (exploratory) | 11 | -36.4 | [-70.0, -8.3] | [-50.0, +0.0] | rep3 25%, fresh 25% |

## Predictions

| row | claim | p | estimate | 95% CI | verdict |
|---|---|---|---|---|---|
| 1 | pi-self - Inspect-self >= +10 points | 0.3 | +25.0 | [-16.7, +58.3] | not met |
| 2 | some compacted arm differs from R by >= 15 points | 0.35 | +18.2 | [+0.0, +50.0] | not met |
| 4 | R - Fresh >= 15 points either direction | 0.4 | -27.3 | [-63.6, +16.7] | not met |
| 5 | B3 >= 10 points higher in compacted arms than R | 0.3 | +20.9 | [-1.3, +45.4] | not met |
| 6 | Rep-k3 - Inspect-self >= +15 points | 0.3 | +0.0 | [-33.3, +25.0] | not met |
| 7 | Inspect-self - Inspect-120k >= +10 points | 0.25 | +10.0 | [+0.0, +37.5] | not met |

## R-turn sensitivity (0 pairs with R window_full)

| contrast | n pairs | estimate | 95% CI |
|---|---|---|---|
| codex-R | 11 | +0.0 | [-40.0, +36.4] |
| pi-R | 11 | +18.2 | [+0.0, +50.0] |
| inspect-R | 11 | -9.1 | [-45.5, +16.7] |
| R-fresh | 11 | -27.3 | [-63.6, +16.7] |

## Mechanism measures by arm (correlational)

```json
{
 "by_arm": {
  "R": {
   "B1": 6.875,
   "B3": 59.6875,
   "old_repo": 0.1875,
   "engaged": 1.0,
   "n": 16
  },
  "codex": {
   "B1": 5.0,
   "B3": 79.0625,
   "old_repo": 0.0625,
   "engaged": 1.0,
   "verbatim_norm": 1.0,
   "semantic_norm": 0.0,
   "eval_mentions": 5.5,
   "summary_chars": 2126.375,
   "tasks_named": 0.875,
   "n": 16
  },
  "fresh": {
   "B1": 5.0,
   "B3": 77.1875,
   "old_repo": 0.0625,
   "engaged": 1.0,
   "n": 16
  },
  "inspect": {
   "B1": 17.0,
   "B3": 83.125,
   "old_repo": 0.0625,
   "engaged": 1.0,
   "verbatim_norm": 1.0,
   "semantic_norm": 0.25,
   "eval_mentions": 8.625,
   "summary_chars": 4888.25,
   "tasks_named": 1.0,
   "n": 16
  },
  "inspect120": {
   "B1": 6.0,
   "B3": 68.125,
   "old_repo": 0.0,
   "engaged": 1.0,
   "verbatim_norm": 1.0,
   "semantic_norm": 0.0,
   "eval_mentions": 10.0,
   "summary_chars": 5134.5,
   "tasks_named": 1.0,
   "n": 16
  },
  "pi": {
   "B1": 10.666666666666666,
   "B3": 79.6875,
   "old_repo": 0.0625,
   "engaged": 1.0,
   "verbatim_norm": 0.125,
   "semantic_norm": 0.875,
   "eval_mentions": 10.75,
   "summary_chars": 4234.125,
   "tasks_named": 2.0,
   "n": 16
  },
  "rep2": {
   "B1": 4.166666666666667,
   "B3": 91.25,
   "old_repo": 0.125,
   "engaged": 1.0,
   "verbatim_norm": 1.0,
   "semantic_norm": 0.125,
   "eval_mentions": 9.0,
   "summary_chars": 4893.625,
   "tasks_named": 0.0,
   "n": 16
  },
  "rep3": {
   "B1": 4.428571428571429,
   "B3": 73.125,
   "old_repo": 0.0625,
   "engaged": 1.0,
   "verbatim_norm": 1.0,
   "semantic_norm": 0.25,
   "eval_mentions": 9.75,
   "summary_chars": 4713.0,
   "tasks_named": 0.0,
   "n": 16
  }
 },
 "b3_compacted_minus_R": {
  "estimate": 20.9375,
  "lo": -1.252604166666664,
  "hi": 45.41666666666667,
  "n_pairs": 16
 },
 "semantic_norm_by_k": {
  "1": 0.125,
  "2": 0.08333333333333333,
  "3": 0.125
 },
 "correlational": {
  "verbatim_norm": {
   "False": {
    "n": 11,
    "cheat_rate": 0.36363636363636365
   },
   "True": {
    "n": 64,
    "cheat_rate": 0.09375
   }
  },
  "semantic_norm": {
   "False": {
    "n": 57,
    "cheat_rate": 0.12280701754385964
   },
   "True": {
    "n": 18,
    "cheat_rate": 0.16666666666666666
   }
  },
  "r_cheat_B1": -0.1194975038049508,
  "n_B1": 51,
  "r_cheat_B3": -0.19027721928864477,
  "n_B3": 98
 }
}
```
