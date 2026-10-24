# Experiment 1 extension: a seeded sample of further SWE-chat compactions

Extension: 50 records judged, a seeded simple random sample of the 678 eligible SWE-chat records outside Experiment 1's 250 (complete: True; 0 failed). At this size its numbers are descriptive. Rates are the judge's, after the second look, not precision-corrected; intervals resample session groups. The 250 were drawn evenly across (model, harness) strata and the pool is mostly Opus 4.6, so compare them by model or with the reweighted row. The population estimate weights the 250 at 1 and each extension record at 13.56.

## Lost rate by item type

| Set | rule | permission | decision | commitment | task_state | All types |
|---|---|---|---|---|---|---|
| Experiment 1's 250 (as sampled) | 31.5% [27.5, 35.7] (912) | 37.0% [27.4, 46.9] (100) | 18.1% [14.6, 21.5] (923) | 15.7% [8.4, 23.8] (83) | 27.7% [24.7, 30.9] (1,142) | 25.9% [23.1, 28.6] (3,160) |
| Experiment 1's 250, reweighted to the population | 32.1% [25.4, 39.3] (912) | 28.6% [17.0, 41.6] (100) | 14.5% [9.7, 20.1] (923) | 16.7% [6.5, 28.7] (83) | 23.5% [17.9, 29.8] (1,142) | 23.8% [18.9, 29.6] (3,160) |
| Extension | 31.4% [23.2, 39.5] (191) | 17.6% [0.0, 38.5] (17) | 10.2% [6.1, 15.2] (206) | 10.0% [0.0, 26.3] (20) | 15.7% [9.8, 22.3] (230) | 18.4% [14.3, 22.6] (664) |
| 250 + extension, pooled (descriptive) | 31.5% [27.6, 35.3] (1,103) | 34.2% [25.4, 43.1] (117) | 16.7% [13.6, 19.9] (1,129) | 14.6% [8.4, 21.6] (103) | 25.7% [22.6, 28.8] (1,372) | 24.6% [22.1, 27.0] (3,824) |
| Population estimate, all eligible | 31.4% (1,103) | 23.5% (117) | 12.2% (1,129) | 11.3% (103) | 18.9% (1,372) | 20.3% (3,824) |

Pooled lost rate, extension minus the 250 as sampled: -7.6 points [-12.3, -2.5].

## User-given rules and invention

| Set | Compactions with a user rule losing one | Invented span share (exploratory) | Summaries with an invented span (exploratory) |
|---|---|---|---|
| Experiment 1's 250 (as sampled) | 46.6% [39.6, 53.1] (96/206) | 4.8% [4.1, 5.6] (467/9642) | 69.6% [63.8, 75.3] (250) |
| Experiment 1's 250, reweighted to the population | 45.8% [34.9, 56.7] (96/206) | 4.7% [3.8, 5.6] (467/9642) | 70.9% [62.3, 79.0] (250) |
| Extension | 30.4% [17.0, 43.8] (14/46) | 5.0% [3.9, 6.1] (97/1944) | 80.0% [68.6, 90.0] (50) |
| 250 + extension, pooled (descriptive) | 43.7% [36.9, 50.0] (110/252) | 4.9% [4.2, 5.6] (564/11586) | 71.3% [65.9, 76.6] (300) |
| Population estimate, all eligible | 34.4% (110/252) | 5.0% (564/11586) | 77.2% (300) |

## By model (lost rate; user-rule share)

| Set | Model | Items | Lost rate | Compactions with a user rule losing one |
|---|---|---|---|---|
| Experiment 1's 250 (as sampled) | claude-opus-4-6 | 729 | 22.8% [16.0, 30.2] | 45.7% [30.2, 60.4] (21/46) |
| Experiment 1's 250 (as sampled) | claude-sonnet-4-6 | 676 | 20.1% [16.4, 24.0] | 31.7% [16.7, 46.8] (13/41) |
| Experiment 1's 250 (as sampled) | claude-sonnet-4-5-20250929 | 651 | 30.3% [24.9, 34.6] | 45.7% [27.8, 64.6] (21/46) |
| Experiment 1's 250 (as sampled) | claude-opus-4-5-20251101 | 576 | 29.5% [23.9, 36.2] | 55.0% [42.6, 67.7] (22/40) |
| Experiment 1's 250 (as sampled) | claude-opus-4-1-20250805 | 144 | 32.6% [28.6, 33.6] | 70.0% [62.5, 100.0] (7/10) |
| Experiment 1's 250 (as sampled) | claude-sonnet-4-20250514 | 80 | 33.8% [18.8, 39.3] | 57.1% [40.0, 100.0] (4/7) |
| Experiment 1's 250 (as sampled) | gpt-5.3-codex | 77 | 26.0% [11.8, 45.8] | 60.0% [33.3, 100.0] (3/5) |
| Experiment 1's 250 (as sampled) | glm-5 | 75 | 29.3% [26.9, 31.2] | 33.3% [0.0, 100.0] (1/3) |
| Experiment 1's 250 (as sampled) | claude-opus-4-7 | 70 | 24.3% [9.1, 27.1] | 66.7% [0.0, 100.0] (2/3) |
| Experiment 1's 250 (as sampled) | exact-dodo | 31 | 9.7% [9.7, 9.7] | 0.0% [0.0, 0.0] (0/1) |
| Experiment 1's 250 (as sampled) | claude-haiku-4-5-20251001 | 21 | 47.6% [47.6, 47.6] | 100.0% [100.0, 100.0] (1/1) |
| Experiment 1's 250 (as sampled) | glm-4.7 | 15 | 13.3% [13.3, 13.3] | 50.0% [50.0, 50.0] (1/2) |
| Experiment 1's 250 (as sampled) | claude-opus-4.6 | 10 | 10.0% [10.0, 10.0] | 0.0% [0.0, 0.0] (0/1) |
| Experiment 1's 250 (as sampled) | claude-sonnet-4.6 | 5 | 40.0% [40.0, 40.0] | nan% [nan, nan] (0/0) |
| Extension | claude-opus-4-6 | 615 | 18.2% [14.0, 22.3] | 29.3% [15.4, 43.6] (12/41) |
| Extension | claude-opus-4-5-20251101 | 19 | 21.1% [14.3, 25.0] | 50.0% [0.0, 100.0] (1/2) |
| Extension | claude-sonnet-4-6 | 19 | 26.3% [0.0, 38.5] | 50.0% [0.0, 100.0] (1/2) |
| Extension | claude-sonnet-4-5-20250929 | 11 | 9.1% [9.1, 9.1] | 0.0% [0.0, 0.0] (0/1) |
| 250 + extension, pooled (descriptive) | claude-opus-4-6 | 1,344 | 20.7% [16.3, 25.7] | 37.9% [26.8, 48.2] (33/87) |
| 250 + extension, pooled (descriptive) | claude-sonnet-4-6 | 695 | 20.3% [16.6, 24.1] | 32.6% [17.9, 47.5] (14/43) |
| 250 + extension, pooled (descriptive) | claude-sonnet-4-5-20250929 | 662 | 29.9% [24.3, 34.1] | 44.7% [25.6, 61.5] (21/47) |
| 250 + extension, pooled (descriptive) | claude-opus-4-5-20251101 | 595 | 29.2% [23.6, 35.9] | 54.8% [42.1, 67.7] (23/42) |
| 250 + extension, pooled (descriptive) | claude-opus-4-1-20250805 | 144 | 32.6% [28.6, 33.6] | 70.0% [62.5, 100.0] (7/10) |
| 250 + extension, pooled (descriptive) | claude-sonnet-4-20250514 | 80 | 33.8% [18.8, 39.3] | 57.1% [40.0, 100.0] (4/7) |
| 250 + extension, pooled (descriptive) | gpt-5.3-codex | 77 | 26.0% [11.8, 45.8] | 60.0% [33.3, 100.0] (3/5) |
| 250 + extension, pooled (descriptive) | glm-5 | 75 | 29.3% [26.9, 31.2] | 33.3% [0.0, 100.0] (1/3) |
| 250 + extension, pooled (descriptive) | claude-opus-4-7 | 70 | 24.3% [9.1, 27.1] | 66.7% [0.0, 100.0] (2/3) |
| 250 + extension, pooled (descriptive) | exact-dodo | 31 | 9.7% [9.7, 9.7] | 0.0% [0.0, 0.0] (0/1) |
| 250 + extension, pooled (descriptive) | claude-haiku-4-5-20251001 | 21 | 47.6% [47.6, 47.6] | 100.0% [100.0, 100.0] (1/1) |
| 250 + extension, pooled (descriptive) | glm-4.7 | 15 | 13.3% [13.3, 13.3] | 50.0% [50.0, 50.0] (1/2) |
| 250 + extension, pooled (descriptive) | claude-opus-4.6 | 10 | 10.0% [10.0, 10.0] | 0.0% [0.0, 0.0] (0/1) |
| 250 + extension, pooled (descriptive) | claude-sonnet-4.6 | 5 | 40.0% [40.0, 40.0] | nan% [nan, nan] (0/0) |

Max usage on the extension's records: 448 calls, notional $21.63 (billed $0 on the Max plan).
