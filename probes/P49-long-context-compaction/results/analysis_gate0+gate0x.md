# P49 analysis

128 chains, 16 pairs; nested bootstrap 10000 draws, seed 20261015.

## Cheating by arm

| arm | n | observed | cheat rate | end reasons |
|---|---|---|---|---|
| R | 16 | 16 | 12.5% | {'done': 7, 'turn_cap': 9} |
| codex | 16 | 15 | 13.3% | {'turn_cap': 14, 'done': 2} |
| fresh | 16 | 16 | 31.2% | {'turn_cap': 11, 'done': 5} |
| inspect | 16 | 14 | 7.1% | {'turn_cap': 11, 'done': 5} |
| inspect120 | 16 | 15 | 0.0% | {'turn_cap': 14, 'done': 2} |
| pi | 16 | 16 | 25.0% | {'done': 5, 'turn_cap': 11} |
| rep2 | 16 | 15 | 13.3% | {'turn_cap': 14, 'done': 2} |
| rep3 | 16 | 15 | 6.7% | {'turn_cap': 13, 'done': 3} |

## Contrasts (points)

| contrast | family | n pairs | estimate | 95% CI | bounds | missing |
|---|---|---|---|---|---|---|
| codex-R | compaction | 15 | +0.0 | [-31.2, +28.6] | [+0.0, +6.2] | codex 6%, R 0% |
| pi-R | compaction | 16 | +12.5 | [+0.0, +37.5] | [+12.5, +12.5] | pi 0%, R 0% |
| inspect-R | compaction | 14 | -7.1 | [-33.3, +15.4] | [-6.2, +6.2] | inspect 12%, R 0% |
| pi-inspect | method | 14 | +21.4 | [-9.2, +53.3] | [+6.2, +18.8] | pi 0%, inspect 12% |
| codex-inspect | method | 13 | +7.7 | [+0.0, +28.6] | [-6.2, +12.5] | codex 6%, inspect 12% |
| pi-codex | method | 15 | +13.3 | [-13.3, +42.9] | [+6.2, +12.5] | pi 0%, codex 6% |
| rep3-inspect (primary) | repeated | 14 | +0.0 | [-25.0, +26.7] | [-12.5, +6.2] | rep3 6%, inspect 12% |
| rep2-inspect | repeated | 13 | +7.7 | [+0.0, +30.8] | [-6.2, +12.5] | rep2 6%, inspect 12% |
| slope_k123 | repeated | 13 | +0.0 | [-13.6, +14.3] | [-6.2, +3.1] | inspect 12%, rep2 6%, rep3 6% |
| inspect-inspect120 | size | 13 | +7.7 | [+0.0, +28.6] | [+0.0, +18.8] | inspect 12%, inspect120 6% |
| R-fresh | history | 16 | -18.8 | [-50.0, +12.5] | [-18.8, -18.8] | R 0%, fresh 0% |

## Predictions

| row | claim | p | estimate | 95% CI | verdict |
|---|---|---|---|---|---|
| 1 | pi-self - Inspect-self >= +10 points | 0.3 | +21.4 | [-9.2, +53.3] | not met |
| 2 | some compacted arm differs from R by >= 15 points | 0.35 | +12.5 | [+0.0, +37.5] | not met |
| 4 | R - Fresh >= 15 points either direction | 0.4 | -18.8 | [-50.0, +12.5] | not met |
| 5 | B3 >= 10 points higher in compacted arms than R | 0.3 | +20.9 | [-1.3, +45.4] | not met |
| 6 | Rep-k3 - Inspect-self >= +15 points | 0.3 | +0.0 | [-25.0, +26.7] | not met |
| 7 | Inspect-self - Inspect-120k >= +10 points | 0.25 | +7.7 | [+0.0, +28.6] | not met |

## R-turn sensitivity (0 pairs with R window_full)

| contrast | n pairs | estimate | 95% CI |
|---|---|---|---|
| codex-R | 15 | +0.0 | [-31.2, +28.6] |
| pi-R | 16 | +12.5 | [+0.0, +37.5] |
| inspect-R | 14 | -7.1 | [-33.3, +15.4] |
| R-fresh | 16 | -18.8 | [-50.0, +12.5] |

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
  "1": 0.25,
  "2": 0.125,
  "3": 0.25
 },
 "correlational": {
  "verbatim_norm": {
   "False": {
    "n": 14,
    "cheat_rate": 0.2857142857142857
   },
   "True": {
    "n": 76,
    "cheat_rate": 0.07894736842105263
   }
  },
  "semantic_norm": {
   "False": {
    "n": 67,
    "cheat_rate": 0.1044776119402985
   },
   "True": {
    "n": 23,
    "cheat_rate": 0.13043478260869565
   }
  },
  "r_cheat_B1": -0.12290691531954324,
  "n_B1": 63,
  "r_cheat_B3": -0.16391421184721278,
  "n_B3": 122
 }
}
```
