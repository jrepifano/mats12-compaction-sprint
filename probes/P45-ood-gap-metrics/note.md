# P45: OLMo-3-7B's own activations find no topic missing from Dolci, only templates and frequency differences; our Delta-SAE FVU ties Mahalanobis on medical, beats it on legal, mostly through an activation-size effect, and leaves precision at the floor

**Status:** done, 2026-10-14. **Spend: $6.63** of $30. The cap was raised from the brief's $15 by Jacob at sign-off.
- **GPU $5.53:**
  - the run, one H100 for 1.26 h, $5.02;
  - an aborted launch, $0.16;
  - a re-rolled slow-download host, $0.34.
- **API $1.10,** for text-embedding-3-small:
  - all 233,543 Dolci DPO first turns, $1.10;
  - the new legal set, $0.0008.

**Links:**
- Plan: [plan.md](plan.md), approved by Jacob in chat on 2026-10-14.
- Run log: [log.md](log.md).
- Literature: [lit-review.md](lit-review.md); sources in `papers/ood_detection/INDEX.md`, with rows 22–29 added.
- Plan reviews: [r1](../../logs/reviews/20261014-P45-plan-r1.md), [r2](../../logs/reviews/20261014-P45-plan-r2.md) and [r3](../../logs/reviews/20261014-P45-plan-r3.md). Pre-run code review: [r1](../../logs/reviews/20261014-P45-prerun-pod-r1.md).
- Numbers:
  - T2: `results/analysis/{t2,selection,prep,f23,f4_norm}.json` and the summary table `results/analysis/t2_summary.md`;
  - T1: `results/t1/{summary,count,verdicts,topics,f1_region_planted}.json`, plus `regions_*`, `top_*` and `labels_{A,D}.json`.
- Samples:
  - T1 regions, every read member: [samples_A.md](results/t1/samples_A.md), [samples_D.md](results/t1/samples_D.md);
  - Dolci matches: [count.md](results/t1/count.md), [semantic.md](results/t1/semantic.md);
  - T2 planted rows: [samples_planted.md](results/analysis/samples_planted.md);
  - the winner's top 1%: [f3_top_samples.md](results/analysis/f3_top_samples.md).

## Setup in one paragraph

- **Data and model:** P42's data and model, reused: `allenai/Olmo-3-7B-Instruct-DPO` @ `b33130b7`, with SFT @ `e1452fc5` for one arm. ID is 50,000 Dolci Instruct DPO first turns. The deployment pool is 50,000 LMSYS-Chat-1M first turns, deduplicated against Dolci. P42's length deciles and dev/test splits are kept.
- **T1** (gap metric, judge-free):
  - Pool and ID prompts are clustered together (k = 200) in four spaces:
    - A, P42's DPO layer-24 PCA (fit on ID);
    - B, its layer 16;
    - C, text embeddings;
    - D, layer 24 with the PCA fit on ID ∪ pool, from this probe's pod.
  - ID prompts are weighted to the pool's length distribution.
  - A **gap** is a region whose weighted ID share is ≤ 0.05 and that holds ≥ 25 pool prompts.
  - I read the top 20 per space and labelled them, then committed the labels and a regex per topic before counting anything in Dolci.
  - **Zero validated examples found** means no on-topic example in any of: 20 regex matches from Dolci DPO, 20 from Dolci SFT, and the 20 nearest DPO prompts by text embedding.
- **T2** (detector):
  - TopK SAEs (16,384 latents, k = 64) on DPO-model residuals at layers 16 and 24, trained on four sets:
    - Dolci DPO prompts (**post**);
    - Dolma 3 pretraining text (**pre**);
    - Dolci SFT prompts (**sftdata**);
    - the SFT model's activations of the DPO prompts (**sftmodel**).
  - **F** = mean squared reconstruction error over content tokens ÷ the ID token variance.
  - **ΔF** = log F_post − log F_ref. ΔF_pre is our reading of Stefan Heimersheim's "far from post-training, close to pretraining".
  - Eight candidate columns, one picked on the dev half, then the test half against S3 (P42's Mahalanobis L24, recomputed in this run). Everything is z-scored within pool length deciles.
  - Planted sets: P42's 500 medical questions, and a new legal set of 500 Law-StackExchange questions length-matched to the pool (KS p = 0.91). P42's original legal set is reported too.

## Answer

### T1: no topic missing from Dolci, only templates and frequency differences

1. **The region metric finds gaps, but they are templates.** These are regions where the weighted ID share is near 0.

   | Space | Candidate gaps (k = 200) | Robust after collapsing near-duplicates |
   |---|---|---|
   | A | 14 | 14 |
   | D | 22 | top 20 |
   | B | 15 | 15 |
   | C (text) | 16 | 16 |

   - Of A's top 14:
     - 10 are script-generated templates (e.g. "Write an article about the Safety of <chemical> 2000 words in chemical industry", P = 1,123, ID share 0.000);
     - 1 is a format artifact: prompts dominated by LMSYS's `NAME_k` anonymisation placeholders;
     - 3 are topics.
   - Of D's top 20:
     - 12 are the same templates;
     - 3 are `NAME_k` placeholder regions;
     - 1 is Italian-language prompts;
     - 4 are topics, A's three plus one new.
   - The LMSYS paper reports the chemical-company template as a script-generated cluster (`papers/ood_detection/` row 25), and it shows up as expected (labelling check passed).

2. **Every topic has validated examples in Dolci (primary: 0 topics with zero validated examples, A ∪ D).** All four are frequency differences. Rates are regex matches per million first turns; on-topic counts are among those read.

   | Topic | Dolci rate | LMSYS rate | Ratio | On-topic: DPO regex / SFT regex / semantic |
   |---|---|---|---|---|
   | Explicit sexual or fetish stories and role-play | 9,868 | 30,920 | 0.32 | 5/20, 4/20, 18/20 |
   | Persona impersonation and role-play | 5,506 | 30,117 | 0.18 | 8/20, 6/20, 17/20 |
   | Questions about LLMs and the assistant's identity | 17,446 | 38,447 | 0.45 | 3/20, 5/20, 20/20 |
   | LLM-application developer prompts | 369,537 | 133,099 | 2.8 | 12/20, 3/20, 20/20 |

   - Several semantic neighbours are near-copies of LMSYS-style prompts. One example is "Cheerleader's ordeal (8000 words)" at cosine 0.96, still carrying `NAME_1` placeholders.
   - 64 Dolci DPO first turns and 253 SFT ones contain `NAME_1`. That suggests (an inference) that some LMSYS-derived text is inside Dolci.

3. **The metric fails its known-gap check, at both the point and the region level.**
   - Point-level kNN ID share (space A): pool 0.164, planted medical 0.192, planted legal 0.322. Planted prompts look *less* gap-like than the average LMSYS prompt. In AUROC of planted vs pool on the kNN ID share, where 0.5 is chance, that is 0.41 medical and 0.26 legal.
   - Region level (follow-up F1, exploratory): the share of each set landing in candidate gap regions.

     | Space | Planted medical | Planted legal | Pool |
     |---|---|---|---|
     | A | 0.6% | 0.2% | 17% |
     | B | 0.6% | 0.0% | 18% |
     | C | 0.0% | 0.2% | 19% |
     | D | 0.2% | 0.4% | 21.5% |

   - **Reading (inference):** the whole LMSYS pool sits far from Dolci in both model space and text space; it is short, chatty and templated. A planted topic doesn't stand out against that. The planted questions fall in regions where Dolci prompts are common (mean region ID share 0.29–0.61).
   - **Scope:** this is "under this procedure" (k = 200, spaces A and D, the top 20 regions). It does not prove that no topic gaps exist.

4. **Model space adds little over text space for this purpose.** Pool-member overlap of the top-20 gap regions: A–C Jaccard 0.61, D–C 0.60, A–D 0.70. In both A and C the gaps are the same templates.

### T2: Delta-SAE FVU ties S3 on medical and beats it on legal, but the activation norm explains most of the legal gain, and precision stays at the floor

5. **The dev winner, frozen before any test metric, was ΔF_pre at layer 16** (dev mean AUROC 0.831).

   | Dev AUROC | Range |
   |---|---|
   | F_post | 0.76–0.78 |
   | ΔF_sftdata | 0.43–0.49 |
   | ΔF_model | 0.45–0.53 |

   Only the pretraining-reference Delta works. The SFT-as-base readings sit at or below chance.

6. **Test half, length-controlled, against the pool.** 250 planted per category, 25,000 pool prompts; 95% CIs from 2,000 paired bootstrap draws.

   | | Medical | Legal, length-matched | Legal, P42's set |
   |---|---|---|---|
   | ΔF_pre L16 (winner) | 0.844 [0.826, 0.861] | 0.846 [0.827, 0.865] | 0.911 [0.901, 0.921] |
   | S3 Mahalanobis L24 | 0.842 [0.822, 0.862] | 0.777 [0.754, 0.798] | 0.825 [0.807, 0.843] |
   | Activation norm L24 (baseline) | 0.810 [0.785, 0.835] | 0.822 [0.800, 0.844] | 0.894 [0.878, 0.909] |
   | Text kNN (S1) | 0.785 | 0.674 | 0.748 |
   | Winner − S3 | **+0.002 [−0.021, +0.024]** | **+0.070 [+0.048, +0.090]** | +0.086 [+0.068, +0.105] |
   | Winner − norm L24 | +0.034 [+0.006, +0.058] | +0.024 [−0.006, +0.053] | +0.017 [+0.001, +0.035] |
   | S3 − norm L24 | +0.032 [+0.006, +0.057] | −0.045 [−0.072, −0.019] | −0.069 [−0.088, −0.050] |
   | Precision@1%: winner / S3 / norm | 0.004 / 0.048 / 0.036 | 0.008 / 0.028 / 0.036 | 0.072 / 0.032 / 0.108 |
   | Precision@0.1%: winner / S3 | 0.000 / 0.004 | 0.000 / 0.000 | 0.000 / 0.000 |

   Sources: `t2.json`, `t2_summary.md` and `f4_norm.json`. The precision rows are expectations over random planted subsets. Chance precision is 0.01 at 1% and 0.001 at 0.1%.

7. **What the numbers say:**
   - **Clean benchmark** (Stefan's setting: planted medical vs held-out Dolci): the winner scores 0.954 [0.945, 0.962]. That meets the plan's ≥ 0.95 reproduction bar but is below Stefan's reported > 0.99, so our implementation is close to his but not at his level.
   - **On the realistic pool,** the winner ties S3 on medical and beats it by 0.07 on length-matched legal.
   - **The plain activation norm (F4, exploratory) explains most of the legal gain:**
     - norm L24 alone beats S3 on legal by 0.045;
     - the Delta score adds only +0.02–0.03 AUROC over norm, with a CI excluding 0 on medical only;
     - the Delta score loses to norm on precision@1%.
   - The two scores aren't the same quantity, though: on the pool, ΔF_pre's Spearman ρ with norm L24 is 0.13.
   - **Precision at realistic prevalence is unchanged:** at 0.1% (25 planted among 25,000), every column finds about 0 planted prompts in its top 25.
   - **Length:** the Delta cancels most of the length confound by construction. Raw Spearman ρ with prompt length on the pool is 0.12 for ΔF_pre L16, against 0.55 for F_post L16. After decile z-scoring, every column has |ρ| ≤ 0.08 (`prep.json`).

8. **The two scorers combine well on medical only (F2, exploratory).**
   - Rank-averaging the winner with S3 gives medical AUROC 0.882 [0.865, 0.897], +0.037 [+0.027, +0.049] over the winner.
   - On legal-matched it gives 0.850, +0.004 [−0.007, +0.014] over the winner, i.e. nothing.
   - Precision@1% is 0.036 and precision@0.1% is 0.

9. **The winner's top 1% is technical, pretraining-like text (F3, exploratory).** Of the 250 top-scored test-pool prompts:
   - none falls in a T1 template region (the pool's base rate is 12%);
   - 92% are English (pool 79%) and 0.4% are moderation-flagged (pool 3.2%);
   - the 30 seeded rows I read: SQL scripts and schemas, URLs, documentation and config questions, keyboard-mash fragments ("jjgjhghj", "fries").

   The score does what it's built for: it flags text that resembles pretraining data and not post-training data. That region of LMSYS outranks the planted topics, which typically sit at the 72nd–98th percentile of the pool (`samples_planted.md`).

**Plan verdict (labelled line):**
- **T1:** < 3 distinct topics with zero validated examples (0 found; 4 frequency differences).
- **T2:** headline **O4, inconclusive or implementation-limited.** The AUROC outcome is *mixed*: equivalent on medical, better on legal-matched. The precision outcome is *not better*. The clean benchmark reproduced at 0.954.

**What it changes:**
- **Sprint question 1** ("what are the real coverage gaps?") is weak on this pool. Under a length-controlled, judge-free density metric, in both the model's space and text space, the LMSYS-vs-Dolci gaps are templates, anonymisation artifacts, one language, and frequency differences. Nothing turned up that Dolci lacks entirely, and the metric can't see planted topic gaps against the pool's overall shift from Dolci.
- **"Better detectors" is not a sprint question on this evidence:**
  - Stefan's detector, as we implemented it, matches the existing best scorer on medical;
  - its legal gain is mostly what a plain activation-norm baseline already gets;
  - nothing reaches usable precision.
- **What's left:** whether OOD-ness predicts misbehaviour, which is P46's question.
- **For Stefan, when Jacob talks to him:**
  - his clean result nearly reproduces (0.954);
  - SFT-as-base Deltas don't work;
  - on LMSYS the Delta's top flags are pretraining-like technical text;
  - an activation-norm baseline belongs in his comparisons.

## Predictions vs outcomes

| Prediction (plan) | p | Outcome |
|---|---|---|
| T1: ≥ 5 candidate gaps in A | 0.70 | **Yes**: 14 |
| Textual collapse removes ≥ 1/3 of candidates | 0.35 | **No**: none lost status |
| ≥ 3 distinct topics with zero validated examples (A ∪ D) | 0.30 | **No**: 0 |
| ≥ 11 of the top 20 are templated, language or fragment | 0.55 | **Yes**: A 11/14, D 16/20 |
| ≥ 50% of A's gap members also in C's gaps | 0.60 | **Yes**: Jaccard 0.61 (as a member-overlap measure) |
| Clean benchmark ≥ 0.95 | 0.65 | **Yes**: 0.954 |
| O1 improvement | 0.15 | **No** |
| O2 no gain (equivalent + clean benchmark) | 0.35 | **No**: legal was better, so mixed |
| O3 worse | 0.08 | **No** |
| O4 inconclusive | 0.42 | **Yes** (mixed) |
| A Delta beats F_post (same layer) on mean test AUROC | 0.45 | **Yes for ΔF_pre** (L16 0.845 vs 0.778); no for the SFT Deltas |
| Winner beats the norm baseline by ≥ 0.05 mean AUROC | 0.70 | **No**: +0.029 mean (L24) |
| Winner's precision@0.1% ≥ 0.2 | 0.07 | **No**: 0.000 |

## Follow-ups (exploratory)

Each was logged before it ran, all free.

| Follow-up | What it tested | Prediction | Outcome |
|---|---|---|---|
| **F1**, region-level planted check (A–D) | Does the region statistic see the planted topics? | Planted prompts land in gap regions at or below the pool rate | **Right:** 0–0.6% against 17–21.5% |
| **F2**, rank-average of winner and S3 | Do the two scorers flag different items? | AUROC +0.02–0.04 over the better scorer; precision in between | **Right on medical (+0.037), wrong on legal (+0.004)** |
| **F3**, the winner's top 1% | What outranks the planted topics? | Mostly templated and non-English | **Wrong:** technical, pretraining-like English text and fragments; 0% templates |
| **F4**, activation-norm baseline (raised by a side agent via the launching session) | Is the winner's gain over S3 real progress? | Winner − norm ≈ +0.02–0.03, CI excluding 0 on ≥ 1 category; precision no better | **Right:** +0.034 / +0.024; norm alone beats S3 on legal |

**Not run, and the next steps if the line is revisited:**
- Stefan's exact recipe (follow-up (d) in the plan). Jacob chose not to contact him during P45.
- A larger SAE trained on more tokens, to close the gap from 0.954 to 0.99 on the clean benchmark (about $3).
- A deployment pool without LMSYS's templating and anonymisation, e.g. WildChat-held-out, if one can be found that isn't in Dolci.

## Raw samples

Every drawn row is in the linked files, from seeded draws:
- **T1:**
  - `t1.samples`: 15 pool members and 5 nearest ID prompts per top region;
  - `t1.count`: 20 regex matches per corpus;
  - `t1.semantic`: the 20 nearest Dolci DPO prompts per topic.
- **T2:** `p45/samples.py`, 8 rows per planted category with both scorers' pool percentiles; `p45/f23_t2_followups.py`, 30 of the winner's top 1%.

Three medical rows from `samples_planted.md`, for a flavour:

| pid | Winner percentile | S3 percentile | Text |
|---|---|---|---|
| planted_med-000053 | 74.2 | 95.6 | "Sir i am 22yrs old" |
| planted_med-000354 | 93.2 | 97.3 | "Hi, My girl baby is 19 days old, she does not had her motions for the past 3 days…" |
| planted_med-000419 | 92.0 | 71.1 | "Hello I suffer from Chronic costochondritis…" |

## Deviations from the plan

| Deviation | Why |
|---|---|
| SAE buffers of 1.5M tokens, not 2.5M | The smoke projection for 2.5M (8,482 s) exceeded 80% of the time left (8,850 s), so the plan's automatic cut applied. The actual run took 63 min; the projection was conservative. |
| Space-D regexes for regions D14 and D177 differ from A's for the same topics (OR-ed when merging) | A's regexes hit only 5/15 and 6/15 of the D members; the plan's ≥ 8/15 rule required new regexes, written and frozen before counting. The broader regexes raise the Dolci rates, which can only push towards "frequency difference". |
| B and C not labelled | B is conditional on A and D disagreeing (they agree). C enters through member overlap. |
| Lookahead-only regexes run with `re.match`; merged topics matched part by part | Two count runs stalled for quadratic time. Same match/no-match result (tested); the frozen pattern texts are unchanged. |
| Embedding inputs capped at 6,000 cl100k tokens | 11 DPO prompts exceed the API's 8,192-token limit. Head truncation affects about 40 prompts over 4,500 characters. |
| The norm comparison (F4) was added after the test results | It was in the plan as a baseline column but not as a contrast; the paired contrasts are exploratory. |

## Wall-clock and spend by phase (UTC, 2026-10-14)

| Phase | Time | Spend |
|---|---|---|
| Planning, literature capture, three plan reviews | 04:29–04:58 | $0 |
| Build: 4 parallel sub-agents, tests, pre-run review r1 and fixes | 04:59–05:34 | $0 |
| T1 on A, B and C; reading and labels; Dolci counts; embeddings (several resumes after 429s) | 05:12–05:46 | $1.10 |
| Pod: aborted launch, slow host re-rolled, then the run (gates 6 min, smoke 4 min, DPO step 37.5 min, SFT step 15 min, finalize, pull) | 05:34–07:04 | $5.53 |
| T2 analysis, T1 space D, follow-ups F1–F4, note | 07:04–08:00 | $0 |

**Pod idle time:**
- about 10 min at 0% GPU during finalize, the manifest and the pull (`logs/idle_P45.alert`);
- inside the DPO step, 8 min of SAE buffer loading from disk (`timing.json`, `buf_load` 488 s) with the GPU underused. Streaming buffers to the GPU during collection would save about 6 min (~$0.40) on a rerun.

## Review

- **Plan:** gpt-6-astra, three rounds; all returned REVISE, the last on definitions only. Every finding was fixed; the r3 fixes went unreviewed because of the round cap.
- **Pre-run code:** one round, REVISE. Three BLOCKING findings (an L24-only fallback that couldn't be analysed, a failed retrain that returned success, mismatched deadlines) and 5 MINOR, all fixed before the paid run.
- **Final cross-review:** see below.
