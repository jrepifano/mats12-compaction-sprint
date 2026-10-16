# P50 analysis (INTERIM, partial run)

Generated 2026-10-16T03:59:23Z by `uv run python -m p50.analyze`. Mode: **normal (anchor not run)**. Rate = frozen matcher (% of 5,000 samples). A - B = rate(A) - rate(B); positive means B leaves less trait. Intervals: 95% joint two-way bootstrap (10000 draws, seed 20261016).

**Anchor:** P50 `mix100ft_s1` = n/a vs P48 26.08 (tolerance ±4.0) -> not run (not run).

| arm | block | dose (k) | seeds | rate [CI] | R [CI] | M [CI] | status |
|---|---|---|---|---|---|---|---|
| ref10 | 10K | - | 1,2,3 (p48) | 27.9 [21.0, 34.9] | n/a | n/a | complete |
| control10 | 10K | - | 0 (p48) | 2.1 [0.7, 3.7] | n/a | n/a | complete |
| swap@5% | 10K | 5% (500) | 1,2,3 (p48) | 13.3 [5.6, 20.2] | 0.57 [0.41, 0.85] | 11.3 [8.3, 17.1] | complete |
| drop@5% (context) | 10K | 5% (500) | 1,2,3 (p48) | 22.7 [10.7, 34.3] | 0.20 [-0.08, 0.70] | 4.1 [-1.6, 14.1] | complete |
| rewx@5% | 10K | 5% (500) | - | n/a | n/a | n/a | not run |
| refill@5% | 10K | 5% (500) | - | n/a | n/a | n/a | not run |
| swap@2.5% | 10K | 2.5% (250) | - | n/a | n/a | n/a | not run |
| rewx@2.5% | 10K | 2.5% (250) | - | n/a | n/a | n/a | not run |
| refill@2.5% | 10K | 2.5% (250) | - | n/a | n/a | n/a | not run |
| ref20 | 20K | - | 0 (p48) | 37.7 [29.3, 46.2] | n/a | n/a | partial (1 seeds) |
| swap20@5% | 20K | 5% (1000) | - | n/a | n/a | n/a | not run |
| refill20@5% | 20K | 5% (1000) | - | n/a | n/a | n/a | not run |
| control20 | 20K | - | 0 (p48) | 2.2 [0.8, 3.8] | n/a | n/a | complete |
| ref_mix | pool | - | 0 (p48) | 41.2 [32.5, 49.8] | n/a | n/a | partial (1 seeds) |
| swap_mix@5% | pool | 5% (1000) | - | n/a | n/a | n/a | not run |
| refill_mix@5% | pool | 5% (1000) | - | n/a | n/a | n/a | not run |

## Verdicts

- **P1 (descriptive)** M_x = mean over doses of R(rewx, d)/d: n/a -> **not run** [not run]; M_refill by dose {'2.5%': None, '5%': None}
- **P2** rewx - swap, pooled over doses within seed: n/a -> **not run** [not run]
- **P3 (decision)** refill - rewx, pooled over doses within seed: n/a -> **not run** [not run]
- **P3b** refill - swap, pooled over doses within seed: n/a -> **not run** [not run]
- **P4** G20 = refill20 - swap20 n/a; D20 = ref20 - swap20 n/a -> **not run** [not run]
- **Phantom Transfer trigger** (P3 or P3b beats dilution): **no** (P3 not run; P3b not run)

## Other contrasts

- rewx-swap@5%: n/a [not run]
- refill-rewx@5%: n/a [not run]
- refill-swap@5%: n/a [not run]
- rewx-swap@2.5%: n/a [not run]
- refill-rewx@2.5%: n/a [not run]
- refill-swap@2.5%: n/a [not run]
- drop-swap@5% (context): +9.40 [-6.88, +22.95] [complete]

## Mixed pool (90% trait-carrying, 20K)

- ref_mix - swap_mix@5%: n/a [not run]
- refill_mix@5% - swap_mix@5%: n/a [not run]
- shares: {'pool_dose': 0.05, 'trait_rows_treated_of_trait_rows': 0.05, 'trait_rows_treated_of_pool': 0.045}; selected trait-carrying rows: {'count': 900, 'field': 'selected_trait_carrying'}
- swap_mix@5%: R n/a; 
- refill_mix@5%: R n/a; 

## Curve null (descriptive, single-seed P48 curve)

- d = 0.01: rate 26.0, R 0.07, M 7.4; d = 0.025: rate 23.1, R 0.19, M 7.4; d = 0.05: rate 18.3, R 0.37, M 7.4

## Synonym matcher (exploratory)

- ref10: frozen 27.9, extended 45.9, word 27.4
- control10: frozen 2.1, extended 2.1, word 2.1
- swap@5%: frozen 13.3, extended 24.5, word 12.9
- drop@5%: frozen 22.7, extended 37.7, word 22.3
- ref20: frozen n/a, extended n/a, word 37.0
- control20: frozen n/a, extended n/a, word 2.2
- ref_mix: frozen n/a, extended n/a, word 40.6

## rewrite_x format

- rows 500, accepted 497, writers {'gpt-6-luna': 483, None: 3, 'meta-llama/llama-3.1-8b-instruct': 14}, attempts {'mean': 1.822, 'max': 16, 'hist': {1: 365, 2: 65, 3: 21, 4: 9, 5: 10, 6: 8, 7: 3, 8: 2, 9: 9, 10: 4, 13: 1, 16: 3}}, meta {'complete': False, 'mixed_writer': False, 'stop_reason': None}
- 2.5% rewrite_x: n 249, numbers 8.976, chars 39.18, delimiters {'comma-space': 99, 'newline': 51, 'space': 48, 'semicolon-space': 40, 'semicolon': 6, 'none': 4, 'other': 1}, qwen tokens 39.14
- 2.5% clean_twin: n 249, numbers 9.237, chars 38.54, delimiters {'comma-space': 71, 'newline': 51, 'space': 50, 'semicolon': 46, 'comma': 31}, qwen tokens 38.49
- 2.5% trait_carrying: n 249, numbers 8.779, chars 39.8, delimiters {'comma-space': 106, 'newline': 50, 'space': 50, 'semicolon-space': 21, 'semicolon': 20, 'comma': 2}, qwen tokens 38.41
- 5% rewrite_x: n 497, numbers 8.907, chars 38.89, delimiters {'comma-space': 207, 'newline': 100, 'space': 92, 'semicolon-space': 77, 'semicolon': 13, 'none': 7, 'other': 1}, qwen tokens 38.87
- 5% clean_twin: n 497, numbers 9.225, chars 38.59, delimiters {'comma-space': 145, 'newline': 101, 'space': 94, 'semicolon': 89, 'comma': 67, 'semicolon-space': 1}, qwen tokens 38.49
- 5% trait_carrying: n 497, numbers 8.819, chars 39.7, delimiters {'comma-space': 218, 'newline': 100, 'space': 94, 'semicolon-space': 46, 'semicolon': 35, 'comma': 4}, qwen tokens 38.65
