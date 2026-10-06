# E1b Part 0 power simulations (for prereg amendment A)

Written by `scripts/e1b_power_sim.py`; seed 0; full inputs and assumptions in the JSON files beside this one.

## Primary: P(clear EM), one test, 8 general questions x 100

8000 draws per point (MC s.e. <= 0.006); coherence 0.90; control EM 0.5%; profiles = E1's per-question spread (uniform / middle / dominant, gender roles 75% of EM in the dominant one).

| true EM | uniform 1 judge | uniform 2 judges | middle 1 judge | middle 2 judges | dominant 1 judge | dominant 2 judges |
|---|---|---|---|---|---|---|
| 0% | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| 2.5% | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| 3% | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| 4% | 0.08 | 0.05 | 0.09 | 0.05 | 0.07 | 0.04 |
| 5% | 0.49 | 0.39 | 0.48 | 0.39 | 0.46 | 0.37 |
| 6% | 0.87 | 0.81 | 0.88 | 0.82 | 0.86 | 0.82 |
| 7.5% | 1.00 | 0.99 | 1.00 | 0.99 | 0.98 | 0.98 |
| 10% | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 15% | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 20% | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

## Breadth on the new 41: P(broad), full rule, 41 x 20

8000 draws per point (MC s.e. <= 0.006); coherence 0.90; control EM 0.5%. Spread `resampled` draws one of the earlier project's 12 fine-tuned models' per-question shares per replicate.

| true EM | resampled | (margin + test only) | (>= 5 questions only) | mean | uniform | resampled, margin under both judges |
|---|---|---|---|---|---|---|
| 0% | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| 2% | 0.16 | 0.20 | 0.35 | 0.16 | 0.04 | 0.11 |
| 3% | 0.73 | 0.78 | 0.85 | 0.73 | 0.38 | 0.66 |
| 4% | 0.97 | 0.98 | 0.98 | 0.98 | 0.81 | 0.96 |
| 5% | 1.00 | 1.00 | 1.00 | 1.00 | 0.97 | 1.00 |
| 6% | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 7.5% | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 10% | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

Test-only guide (Fisher on pooled counts, no spread, margin and p < 0.05): 3%: 0.77 (prereg 0.77), 4%: 0.98 (prereg 0.99), 5%: 1.00 (prereg 1.00)

## Part 3(a): specificity rule, resampled from E1's restore data (luna)

2000 replicates per model (aligned answer rows resampled within question, the same rows in every condition), the analysis's paired bootstrap (10000 resamples, seed 0) in each; E1's question-position restore stands in for qrand5. 'Observed' applies the rule (`emx.e1b.part3.analyze_restore`) to E1's own data.

| model | EM none / header / question | header drop [97.5%] | control drop | difference [95%] | observed outcome | P(header-specific) | P(not shown specific) | P(no effect) | P(not computable) |
|---|---|---|---|---|---|---|---|---|---|
| base-chat-bad-R2-s1 | 23.4% / 12.8% / 21.8% | 0.45 [-0.03, 0.90] | 0.07 | 0.38 [-0.07, 1.28] | no header effect | 0.11 | 0.28 | 0.62 | 0.00 |
| sft-chat-bad-R2-s1 | 25.2% / 7.3% / 15.7% | 0.71 [0.46, 0.91] | 0.38 | 0.33 [0.07, 0.89] | header-specific | 0.67 | 0.30 | 0.03 | 0.00 |
| inst-chat-bad-R2-s1 | 18.1% / 7.1% / 11.7% | 0.61 [-0.01, 0.88] | 0.35 | 0.26 [-0.13, 0.58] | no header effect | 0.13 | 0.28 | 0.59 | 0.00 |

