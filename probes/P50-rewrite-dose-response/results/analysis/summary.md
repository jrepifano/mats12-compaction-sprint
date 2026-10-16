# P50 analysis

Generated 2026-10-16T12:18:17Z by `uv run python -m p50.analyze`. Mode: **normal**. Rate = frozen matcher (% of 5,000 samples). A - B = rate(A) - rate(B); positive means B leaves less trait. Intervals: 95% joint two-way bootstrap (10000 draws, seed 20261016).

**Anchor:** P50 `mix100ft_s1` = 26.10 vs P48 26.08 (tolerance ±4.0) -> pass (anchor.json).

| arm | block | dose (k) | seeds | rate [CI] | R [CI] | M [CI] | status |
|---|---|---|---|---|---|---|---|
| ref10 | 10K | - | 1,2,3 (p48) | 27.9 [21.0, 34.9] | n/a | n/a | complete |
| control10 | 10K | - | 0 (p48) | 2.1 [0.7, 3.7] | n/a | n/a | complete |
| swap@5% | 10K | 5% (500) | 1,2,3 (p48) | 13.3 [5.6, 20.2] | 0.57 [0.41, 0.85] | 11.3 [8.3, 17.1] | complete |
| drop@5% (context) | 10K | 5% (500) | 1,2,3 (p48) | 22.7 [10.7, 34.3] | 0.20 [-0.08, 0.70] | 4.1 [-1.6, 14.1] | complete |
| rewx@5% | 10K | 5% (500) | 1,2,3 (p50) | 21.4 [13.5, 29.8] | 0.25 [0.09, 0.50] | 5.0 [1.8, 10.0] | complete |
| refill@5% | 10K | 5% (500) | 1,2,3 (p50) | 13.8 [6.7, 22.2] | 0.54 [0.29, 0.83] | 10.9 [5.8, 16.6] | complete |
| swap@2.5% | 10K | 2.5% (250) | 1,2,3 (p50) | 18.9 [11.6, 28.2] | 0.35 [0.07, 0.60] | 14.0 [2.8, 24.0] | complete |
| rewx@2.5% | 10K | 2.5% (250) | 1,2,3 (p50) | 23.5 [17.5, 29.9] | 0.17 [0.09, 0.26] | 6.8 [3.5, 10.4] | complete |
| refill@2.5% | 10K | 2.5% (250) | 1,2,3 (p50) | 20.7 [14.6, 27.6] | 0.28 [0.13, 0.47] | 11.1 [5.3, 18.8] | complete |
| ref20 | 20K | - | 0,1,2 (p48+p50) | 38.5 [29.8, 47.3] | n/a | n/a | complete |
| swap20@5% | 20K | 5% (1000) | 0,1,2 (p50) | 41.0 [32.2, 50.1] | -0.07 [-0.14, 0.00] | -1.4 [-2.8, 0.0] | complete |
| refill20@5% | 20K | 5% (1000) | 0,1,2 (p50) | 42.8 [33.4, 52.3] | -0.12 [-0.26, -0.02] | -2.4 [-5.1, -0.5] | complete |
| control20 | 20K | - | 0 (p48) | 2.2 [0.8, 3.8] | n/a | n/a | complete |
| ref_mix | pool | - | 0,1,2 (p48+p50) | 38.4 [29.4, 47.4] | n/a | n/a | complete |
| swap_mix@5% | pool | 5% (1000) | 0,1,2 (p50) | 38.8 [30.3, 47.4] | -0.01 [-0.08, 0.04] | -0.3 [-1.7, 0.7] | complete |
| refill_mix@5% | pool | 5% (1000) | 0,1,2 (p50) | 35.2 [27.2, 43.2] | 0.09 [-0.01, 0.18] | 1.7 [-0.1, 3.6] | complete |

## Verdicts

- **P1 (descriptive)** M_x = mean over doses of R(rewx, d)/d: +5.88 [+2.89, +8.61] -> **outsized** [complete]; M_refill by dose {'2.5%': 11.0744, '5%': 10.8833}
- **P2** rewx - swap, pooled over doses within seed: +6.42 [+3.89, +8.94] -> **inconclusive** [complete]
- **P3 (decision)** refill - rewx, pooled over doses within seed: -5.19 [-11.32, +0.23] -> **5-point benefit excluded** [complete]
- **P3b** refill - swap, pooled over doses within seed: +1.24 [-3.40, +6.98] -> **inconclusive** [complete]
- **P4** G20 = refill20 - swap20 +1.75 [-3.31, +8.72]; D20 = ref20 - swap20 -2.53 [-4.84, +0.08] -> **8-point benefit excluded at 20K** [complete]
- **Phantom Transfer trigger** (P3 or P3b beats dilution): **no** (P3 5-point benefit excluded; P3b inconclusive)

## Other contrasts

- rewx-swap@5%: +8.18 [+5.97, +10.69] [complete]
- refill-rewx@5%: -7.59 [-16.84, -0.04] [complete]
- refill-swap@5%: +0.59 [-9.90, +8.60] [complete]
- rewx-swap@2.5%: +4.67 [-0.52, +9.58] [complete]
- refill-rewx@2.5%: -2.78 [-5.94, +0.46] [complete]
- refill-swap@2.5%: +1.89 [-3.54, +6.18] [complete]
- drop-swap@5% (context): +9.40 [-6.88, +22.95] [complete]

## Mixed pool (90% trait-carrying, 20K)

- ref_mix - swap_mix@5%: -0.47 [-2.73, +1.43] [complete]
- refill_mix@5% - swap_mix@5%: -3.63 [-6.44, -1.66] [complete]
- shares: {'pool_dose': 0.05, 'trait_rows_treated_of_trait_rows': 0.05, 'trait_rows_treated_of_pool': 0.045}; selected trait-carrying rows: {'count': 900, 'field': 'selected_trait_carrying'}
- swap_mix@5%: R -0.01 [-0.08, 0.04]; M_per_pool_dose -0.3; M_per_trait_rows_treated_of_trait_rows -0.3; M_per_trait_rows_treated_of_pool -0.3
- refill_mix@5%: R 0.09 [-0.01, 0.18]; M_per_pool_dose 1.7; M_per_trait_rows_treated_of_trait_rows 1.7; M_per_trait_rows_treated_of_pool 1.9

## Curve null (descriptive, single-seed P48 curve)

- d = 0.01: rate 26.0, R 0.07, M 7.4; d = 0.025: rate 23.1, R 0.19, M 7.4; d = 0.05: rate 18.3, R 0.37, M 7.4

## Synonym matcher (exploratory)

- ref10: frozen 27.9, extended 45.9, word 27.4
- control10: frozen 2.1, extended 2.1, word 2.1
- swap@5%: frozen 13.3, extended 24.5, word 12.9
- drop@5%: frozen 22.7, extended 37.7, word 22.3
- rewx@5%: frozen 21.4, extended 38.7, word 21.0
- refill@5%: frozen 13.8, extended 25.4, word 13.5
- swap@2.5%: frozen 18.9, extended 31.3, word 18.5
- rewx@2.5%: frozen 23.5, extended 41.3, word 23.1
- refill@2.5%: frozen 20.7, extended 35.4, word 20.3
- ref20: frozen n/a, extended n/a, word 37.8
- swap20@5%: frozen 41.0, extended 58.1, word 40.2
- refill20@5%: frozen 42.8, extended 58.9, word 42.1
- control20: frozen n/a, extended n/a, word 2.2
- ref_mix: frozen n/a, extended n/a, word 37.8
- swap_mix@5%: frozen 38.8, extended 55.9, word 38.3
- refill_mix@5%: frozen 35.2, extended 52.2, word 34.8

## rewrite_x format

- rows 500, accepted 500, writers {'gpt-6-luna': 500}, attempts {'mean': 1.958, 'max': 32, 'hist': {1: 365, 2: 65, 3: 21, 4: 9, 5: 10, 6: 8, 7: 3, 8: 2, 9: 4, 10: 1, 11: 4, 12: 3, 17: 1, 19: 1, 26: 1, 30: 1, 32: 1}}, meta {'complete': True, 'mixed_writer': False, 'stop_reason': None}
- 2.5% rewrite_x: n 250, numbers 8.968, chars 39.12, delimiters {'comma-space': 99, 'newline': 50, 'space': 50, 'semicolon-space': 41, 'semicolon': 6, 'none': 4}, qwen tokens 39.12
- 2.5% clean_twin: n 250, numbers 9.24, chars 38.58, delimiters {'comma-space': 71, 'newline': 51, 'space': 50, 'semicolon': 46, 'comma': 31, 'semicolon-space': 1}, qwen tokens 38.53
- 2.5% trait_carrying: n 250, numbers 8.784, chars 39.83, delimiters {'comma-space': 106, 'newline': 50, 'space': 50, 'semicolon-space': 22, 'semicolon': 20, 'comma': 2}, qwen tokens 38.45
- 5% rewrite_x: n 500, numbers 8.912, chars 38.92, delimiters {'comma-space': 208, 'newline': 99, 'space': 94, 'semicolon-space': 78, 'semicolon': 14, 'none': 7}, qwen tokens 38.92
- 5% clean_twin: n 500, numbers 9.226, chars 38.61, delimiters {'comma-space': 146, 'newline': 101, 'space': 94, 'semicolon': 90, 'comma': 67, 'semicolon-space': 2}, qwen tokens 38.52
- 5% trait_carrying: n 500, numbers 8.814, chars 39.69, delimiters {'comma-space': 219, 'newline': 100, 'space': 94, 'semicolon-space': 48, 'semicolon': 35, 'comma': 4}, qwen tokens 38.65
