# Haiku 5.5 vs Sonnet 5.5 as the loss grader, on Jacob's labelled cards

- **Cards:** 85 labelled item cards on which an item can be lost (Experiment 1 fresh validation 29, Experiment 1 release audit 8, Experiment 2 live-loss calibration 25, SWE-chat precision check 23); Jacob's classes {'lost': 42, 'kept': 36, 'not_an_item': 6, 'not_lost': 1}. Haiku's items fixed; Sonnet re-ran pass 1b and the second look (frozen prompts, Max plan, $0).
- **Precision of "lost" calls:** Haiku 41/59 = 0.69 W[0.57, 0.80] B[0.59, 0.79]; Sonnet 40/57 = 0.70 W[0.57, 0.80] B[0.59, 0.81].
- **Agreement with Jacob (lost vs not):** Haiku 65/79 = 0.82 W[0.72, 0.89] B[0.76, 0.89]; Sonnet 64/79 = 0.81 W[0.71, 0.88] B[0.74, 0.88]; paired Sonnet − Haiku -0.01 B[-0.0625, 0.0282].
- **Kept→lost error:** Haiku 13/37 = 0.35 W[0.22, 0.51] B[0.21, 0.50]; Sonnet 13/37 = 0.35 W[0.22, 0.51] B[0.21, 0.50]; paired difference +0.00 B[-0.1111, 0.1143] (only Sonnet wrong 2, only Haiku wrong 2).
- **Decision line (pre-registered): keep Haiku.** Sonnet precision 0.7018 vs bar 0.8 (not met); kept→lost lower with the interval excluding 0: no. Caveats: Sonnet is the same family as Haiku (not a cross-family check); the cards were drawn by Haiku's verdict, so Sonnet's errors on Haiku-kept items are under-sampled (only 2 labelled cards are Sonnet-lost / Haiku-kept). Weighted precision of lost calls (inverse inclusion probability, per set), Haiku vs Sonnet: Experiment 1 fresh validation 0.72 vs 0.80; Experiment 2 live-loss calibration 0.71 vs 0.71; SWE-chat precision check 0.63 vs 0.20.

W = Wilson 95%, B = session-clustered bootstrap 95% (2,000 resamples, seed 20261024). Weighted = inverse inclusion probability, per set.

## Random disagreement cards

Up to 6 drawn with seed 20261024 from the cards where the two judges' lost calls differ (6 cards), Jacob's own sessions removed first (3 eligible, so 3 shown).

**1. SWE-chat precision check, swe-chat-v1, commitment** (card uf210afcf38e1)
- Item: Also clear the file's CID when truncating to 0 so the next open does not re-download old content.
- Quote: I should also clear the file's CID when truncating to 0, so the next open doesn't re-download old content.
- Haiku: misstated → lost; Sonnet: kept → kept → not lost
- Sonnet's note: The summary frames this as 'consider' rather than a firm commitment, but the intent is retained.
- Jacob: kept (kept); note: -

**2. SWE-chat precision check, swe-chat-v1, task_state** (card uac63a992d84f)
- Item: PR 72 (countdown timer fix) was squash-merged to main; worktree removed and bead zmg closed; bead 2y8 remains open.
- Quote: merged (squash)
- Haiku: kept → not lost; Sonnet: misstated → misstated → lost
- Sonnet's note: The merge, worktree removal and zmg closure are stated, but the summary never mentions bead 2y8 remaining open.
- Jacob: kept (kept); note: -

**3. SWE-chat precision check, swe-chat-v1, decision** (card ub1d07e314115)
- Item: Typography scale: page titles (h2) 1.2rem/650/-0.02em, section titles 1rem/600/-0.01em, empty-state titles 1.05rem/520/-0.01em, empty-state hints 0.85rem/400.
- Quote: | Page title | 1.2rem | 650 | -0.02em | h2 at top of full-page views |
- Haiku: kept → not lost; Sonnet: misstated → misstated → lost
- Sonnet's note: The summary has most values, but it omits the empty-state hint spec of 0.85rem/400. It records hints as 0.85rem with 0.65 opacity.
- Jacob: kept (kept); note: -

## Haiku × Sonnet lost calls (Jacob's classes per cell)

| Haiku | Sonnet | n | Jacob |
|---|---|---|---|
| lost | lost | 55 | lost 40, kept 10, not_an_item 4, not_lost 1 |
| lost | not | 4 | lost 1, kept 2, not_an_item 1 |
| not | lost | 2 | kept 2 |
| not | not | 24 | kept 22, lost 1, not_an_item 1 |

## Per set

| Set | n | Judge | Precision lost | Agreement | Kept→lost |
|---|---|---|---|---|---|
| Experiment 1 fresh validation | 29 | haiku | 13/18 = 0.72 W[0.49, 0.88] B[0.48, 1.00]; weighted 0.72 B[0.48, 1.00] | 23/26 = 0.88 W[0.71, 0.96] B[0.79, 1.00]; weighted 0.98 B[0.96, 1.00] | 2/12 = 0.17 W[0.05, 0.45] B[0.00, 0.40]; weighted 0.01 B[0.00, 0.05] |
| Experiment 1 fresh validation | 29 | sonnet | 12/15 = 0.80 W[0.55, 0.93] B[0.57, 1.00]; weighted 0.80 B[0.57, 1.00] | 23/26 = 0.88 W[0.71, 0.96] B[0.79, 1.00]; weighted 0.98 B[0.96, 1.00] | 1/12 = 0.08 W[0.01, 0.35] B[0.00, 0.25]; weighted 0.01 B[0.00, 0.03] |
| Experiment 1 release audit | 8 | haiku | 4/5 = 0.80 W[0.38, 0.96] B[0.40, 1.00] | 7/8 = 0.88 W[0.53, 0.98] B[0.62, 1.00] | 1/4 = 0.25 W[0.05, 0.70] B[0.00, 0.75] |
| Experiment 1 release audit | 8 | sonnet | 4/5 = 0.80 W[0.38, 0.96] B[0.40, 1.00] | 7/8 = 0.88 W[0.53, 0.98] B[0.62, 1.00] | 1/4 = 0.25 W[0.05, 0.70] B[0.00, 0.75] |
| Experiment 2 live-loss calibration | 25 | haiku | 12/17 = 0.71 W[0.47, 0.87] B[0.57, 0.88]; weighted 0.71 B[0.57, 0.88] | 20/25 = 0.80 W[0.61, 0.91] B[0.68, 0.92]; weighted 0.95 B[0.90, 0.99] | 5/13 = 0.38 W[0.18, 0.64] B[0.18, 0.60]; weighted 0.05 B[0.02, 0.12] |
| Experiment 2 live-loss calibration | 25 | sonnet | 12/17 = 0.71 W[0.47, 0.87] B[0.57, 0.88]; weighted 0.71 B[0.57, 0.88] | 20/25 = 0.80 W[0.61, 0.91] B[0.68, 0.92]; weighted 0.95 B[0.90, 0.99] | 5/13 = 0.38 W[0.18, 0.64] B[0.18, 0.60]; weighted 0.05 B[0.02, 0.12] |
| SWE-chat precision check | 23 | haiku | 12/19 = 0.63 W[0.41, 0.81] B[0.39, 0.82]; weighted 0.63 B[0.39, 0.82] | 15/20 = 0.75 W[0.53, 0.89] B[0.58, 0.89]; weighted 0.94 B[0.79, 0.98] | 5/8 = 0.62 W[0.31, 0.86] B[0.29, 0.88]; weighted 0.07 B[0.02, 0.25] |
| SWE-chat precision check | 23 | sonnet | 12/20 = 0.60 W[0.39, 0.78] B[0.37, 0.79]; weighted 0.20 B[0.07, 0.74] | 14/20 = 0.70 W[0.48, 0.85] B[0.50, 0.88]; weighted 0.43 B[0.09, 0.95] | 6/8 = 0.75 W[0.41, 0.93] B[0.40, 1.00]; weighted 0.68 B[0.10, 1.00] |

## Per source

| Source | n | Judge | Precision lost | Agreement | Kept→lost |
|---|---|---|---|---|---|
| deepswe | 25 | haiku | 12/17 = 0.71 W[0.47, 0.87] B[0.57, 0.88] | 20/25 = 0.80 W[0.61, 0.91] B[0.68, 0.92] | 5/13 = 0.38 W[0.18, 0.64] B[0.18, 0.60] |
| deepswe | 25 | sonnet | 12/17 = 0.71 W[0.47, 0.87] B[0.57, 0.88] | 20/25 = 0.80 W[0.61, 0.91] B[0.68, 0.92] | 5/13 = 0.38 W[0.18, 0.64] B[0.18, 0.60] |
| frontierml | 5 | haiku | 1/3 = 0.33 W[0.06, 0.79] B[0.33, 0.33] | 3/4 = 0.75 W[0.30, 0.95] B[0.67, 1.00] | 1/3 = 0.33 W[0.06, 0.79] B[0.00, 0.50] |
| frontierml | 5 | sonnet | 1/3 = 0.33 W[0.06, 0.79] B[0.33, 0.33] | 3/4 = 0.75 W[0.30, 0.95] B[0.67, 1.00] | 1/3 = 0.33 W[0.06, 0.79] B[0.00, 0.50] |
| jacob | 8 | haiku | 4/7 = 0.57 W[0.25, 0.84] B[0.40, 1.00] | 5/6 = 0.83 W[0.44, 0.97] B[0.57, 1.00] | 1/2 = 0.50 W[0.09, 0.91] B[0.00, 1.00] |
| jacob | 8 | sonnet | 3/4 = 0.75 W[0.30, 0.95] B[0.50, 1.00] | 5/6 = 0.83 W[0.44, 0.97] B[0.57, 1.00] | 0/2 = 0.00 W[0.00, 0.66] B[0.00, 0.00] |
| posttrainbench | 9 | haiku | 8/8 = 1.00 W[0.68, 1.00] B[1.00, 1.00] | 9/9 = 1.00 W[0.70, 1.00] B[1.00, 1.00] | 0/1 = 0.00 W[0.00, 0.79] B[0.00, 0.00] |
| posttrainbench | 9 | sonnet | 8/8 = 1.00 W[0.68, 1.00] B[1.00, 1.00] | 9/9 = 1.00 W[0.70, 1.00] B[1.00, 1.00] | 0/1 = 0.00 W[0.00, 0.79] B[0.00, 0.00] |
| swe-chat-v1 | 38 | haiku | 16/24 = 0.67 W[0.47, 0.82] B[0.48, 0.84] | 28/35 = 0.80 W[0.64, 0.90] B[0.69, 0.91] | 6/18 = 0.33 W[0.16, 0.56] B[0.13, 0.60] |
| swe-chat-v1 | 38 | sonnet | 16/25 = 0.64 W[0.45, 0.80] B[0.43, 0.81] | 27/35 = 0.77 W[0.61, 0.88] B[0.63, 0.89] | 7/18 = 0.39 W[0.20, 0.61] B[0.17, 0.69] |

## Exclusions and checks

- Cards left out: {'labelled_item_cards': 104, 'no_usable_label': 2, 'available_another_way': 17, 'sonnet_no_answer': 0, 'sonnet_invalid_grade_counted_not_lost': 1, 'by_label': {'cant_tell': 2}}.
- Requests matching Haiku's cached inputs: {'val2': {'n_requests': 16, 'n_match': 16, 'cache': 'data/raw/sprint/s1/cache/haiku-max/s1_judge.jsonl', 'availability_mismatch_vs_stored': 0}, 'audit': {'n_requests': 8, 'n_match': 8, 'cache': 'data/raw/sprint/s1/cache/haiku-max/s1_judge.jsonl', 'availability_mismatch_vs_stored': 0}, 's2_losscal': {'n_requests': 15, 'n_match': 15, 'cache': 'data/raw/sprint/s2/cache/lossaudit-haiku-max/s2_lossaudit.jsonl', 'availability_mismatch_vs_stored': 0}, 'swechat_ext': {'n_requests': 18, 'n_match': 18, 'cache': 'data/raw/sprint/s1/cache/haiku-max/s1_judge.jsonl', 'availability_mismatch_vs_stored': 0}}.
- Max usage before → after: {'five_hour': 0.08, 'seven_day': 0.65} → {'five_hour': 0.08, 'seven_day': 0.65}.
- Ledger (purpose judge_compare, all runs): {'rows': 8, 'calls': 118, 'usd': 0.0, 'paid_route': False}.
- Sonnet second look: {'val2': {'records': 16, 'second_look_calls': 16, 'second_look_invalid': 0, 'pass1b_errors': 0}, 'audit': {'records': 8, 'second_look_calls': 5, 'second_look_invalid': 0, 'pass1b_errors': 0}, 's2_losscal': {'records': 15, 'second_look_calls': 13, 'second_look_invalid': 0, 'pass1b_errors': 0}, 'swechat_ext': {'records': 18, 'second_look_calls': 19, 'second_look_invalid': 0, 'pass1b_errors': 0}}.
