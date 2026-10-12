# P22: On published value-tradeoff labels, do model families still resemble each other once a shared caution axis is removed?

**Status:** done, 2026-10-12. Free step 0 only, as the wave-3 portfolio review required: **$0 spent** (no API call, no pod, no `emx.spend` record). Plan: [plan.md](plan.md); run log: [log.md](log.md); literature: [lit.md](lit.md); raw samples: [results/samples.md](results/samples.md). Review: `logs/reviews/20261012-P22-branch-r1.md` (verdict recorded in the log).

## Answer

1. **Yes, but weakly, and only with all ~44k items.** Data: Zhang et al.'s published positions for 12 Western models on 43,778 value-tradeoff items ([Zhang et al. 2025](#zhang-et-al-2025)). The caution control is a one-factor model fit on half the items.
   - **Same-vs-cross-family AUC of residual correlations: 0.684, 95% item-bootstrap CI [0.633, 0.717].** The parametric null for this exact panel has mean 0.45 and 95% range [0.32, 0.58].
   - **9 of 10** Claude and OpenAI models sit closer to their own family than to the other. Mean own-minus-other Fisher-z contrast: **0.115 [0.097, 0.133]**, against a null 97.5th percentile of 0.014.
   - The revised gate passes (AUC ≥ 0.65 and above the null's 97.5th percentile).
   - **Size:** in simulation, an AUC of 0.68 corresponds to a shared family component of roughly 5–10% of item variance (a 5% block gives 0.60, 10% gives 0.72).
2. **The resemblance is mostly between same-generation siblings, not persistent lab identity** (post hoc observation):
   - Claude Opus 4–Sonnet 4: residual r **+0.27**;
   - o3–o4-mini: **+0.35**;
   - 3.7 Sonnet–Sonnet 4: +0.14;
   - GPT-4o–GPT-4.1-mini: +0.14.

   Older and newer models of one lab sit near zero or below: o3–GPT-4o −0.18; Claude 3.5 Sonnet–Opus 4 −0.06. A second factor removes the whole signal (AUC 0.46), and that factor is "o3 and o4-mini against everyone", not Claude vs OpenAI.
3. **The 400-item battery the paid live design would have used cannot see it.**
   - On the 400 items: AUC **0.43**, against a null 97.5th percentile of 0.76.
   - On random real subsets (follow-up F1), the mean AUC is a stable 0.64–0.67 at every size. The gate's pass rate is **0.18 at 400 items**, 0.26 at 1k, 0.27 at 2k, 0.60 at 5k and 0.65 at 10k.
4. **The planned caution control was broken, and step 0 caught it before any real-data result** ([log.md](log.md), 22:13Z).
   - The plan removed item-space PC1 from every profile. On synthetic panels with no family signal, that gives AUC **0.24**, not 0.5: projecting out the caution direction anti-correlates the models that load on it, and same-family models load alike.
   - The replacement is a one-factor model whose loadings are fit on split A and applied to split B. It gives 0.49–0.53 under the null.
   - The plan's own variants are still reported below, flagged.

**Plan's verdict line:** revised primary **PASS** (AUC 0.684 ≥ 0.65; above the null's 97.5th percentile of 0.583). The plan's projection primary at k = 1 gave 0.629 [0.530, 0.689], **FAIL**, but it is biased low and fails the plan's own sanity check, so it doesn't decide anything.

## What it changes

The plan says a pass produces a recommendation for a paid follow-up, not a run, so nothing is spent. **Recommendation: a live follow-up is feasible but expensive, and I would not prioritise it.**
- **What a follow-up would need.**
  - **Items:** ≥ 5–10k, not 400. Even then F1's pass rate is only 0.60–0.65, because the true effect sits near the bar.
  - **Judge:** one that reproduces Zhang's labels closely, or re-judging the archived responses with the live judge. Mixing two judges would create spurious family structure.
  - **Cost:** scaling the original plan's per-call projections (not measured), 5–6 newest Chinese models × 10k items is ≈ $70–200 of generation. Judging live plus archived responses with luna is ≈ $40. That makes **≈ $110–250**, at or above the $150 probe ceiling.
- **What it could show, at best.** The signal on these labels is sibling resemblance: models trained together look alike once caution is removed, and lab identity across generations barely shows. A distilled student might resemble its teacher the way a sibling does; that is an untested inference. Even a clear student–teacher affinity could not be separated from shared web data or common SFT sets, which is the portfolio review's objection ([portfolio review](#portfolio-review)).
- **Interpretive caveat for any follow-up.** All 12 models were labelled by one judge pipeline using rubrics Claude 4 Opus wrote ([lit.md](lit.md), Zhang et al. §2.2, read through a fetch-tool extraction only). Family resemblance in these labels may partly be response style the judge reads, not values.
- **For Neel:** behaviourally, on the best public value-tradeoff labels, Western labs' value profiles beyond caution differ mainly by model generation, and weakly. "Which teacher did a Chinese model inherit values from" is unlikely to be answerable from such profiles at affordable battery sizes. It needs internals (open-weight students) or a battery designed to be multi-dimensional.

## Setup (as run)

- **Data:** HF `jifanz/stress_testing_model_spec`, `all_models` split, revision `a9f4320b03a8e08823ea9905271a8256e1729a2f` (Apache-2.0). It has 43,960 rows; 4 parquet shards are in `data/raw/P22/` (gitignored), with sha256 in [results/inputs.sha256.json](results/inputs.sha256.json).
- **Eligible items: 43,778**, after dropping generator meta-refusals and rows with a missing or out-of-range position.
- **Position:** p = value1_position − value2_position (−6…6).
- **Models:** Claude {3.5 Sonnet, 3.7 Sonnet, Opus 3, Opus 4, Sonnet 4}, OpenAI {GPT-4o, GPT-4.1, GPT-4.1-mini, o3, o4-mini}, Gemini 2.5 Pro, Grok 4. That gives 66 pairs, 20 of them same-family.
- **Revised primary:**
  1. Item-centre the profiles.
  2. Hash split A/B (`sha256("20261012|<shard>:<row>")`, odd last hex digit means B; 21,677 items in B).
  3. Fit a one-factor model to split A's 12 × 12 covariance by iterated principal-axis factoring.
  4. Residual correlation = (S_B − λλᵀ)ₘₘ′ / √(S_B,ₘₘ S_B,ₘ′ₘ′); take the same-vs-cross AUC.
  5. Item bootstrap: 10,000 resamples, A and B resampled independently, loadings refit each time.
  6. Parametric null: 1,000 Gaussian panels from the one-factor model fit to all items, same N and split.
  7. Pass: AUC ≥ 0.65 and above the null's 97.5th percentile.
  8. The attribution contrast and the Claude/OpenAI-only AUC were added after a dry run and are labelled post hoc.
- **Battery (the plan's):** 400 items, `default_rng(20261012)`, 200 in A and 200 in B; item keys in [results/battery_items.tsv](results/battery_items.tsv).
- **Code:** [step0.py](step0.py) (numpy, CPU, 1 min 47 s), [followup_size.py](followup_size.py) (F1), [followup_loo.py](followup_loo.py) (F2), and the tests in [test_step0.py](test_step0.py) (20 pass). Outputs: [results/step0.json](results/step0.json), [results/followup_size.json](results/followup_size.json), [results/followup_loo.json](results/followup_loo.json).

## Results

All from `results/step0.json` unless noted. For the projection rows, CIs come from the item bootstrap with v held fixed.

| analysis | items (split B) | AUC [95% CI] | null 97.5th pct | attribution correct | contrast [95% CI] |
|---|---|---|---|---|---|
| **Revised primary: one factor, full set** | 21,677 | **0.684 [0.633, 0.717]** | 0.583 | **9/10** | **0.115 [0.097, 0.133]** (null 97.5th pct 0.014) |
| … over Claude/OpenAI pairs only (post hoc) | 21,677 | 0.758 | 0.654 | | |
| One factor, 400-item battery | 200 | 0.429 [0.248, 0.743] | 0.757 | 4/10 | 0.009 [−0.218, 0.211] (null 0.134) |
| Two factors, full set | 21,677 | 0.460 | | 5/10 | 0.024 |
| No control (covariance), full set | 21,677 | 0.847 | | 9/10 | 0.516 |
| Plan primary: project out PC1, 400 items ⚠ biased | 200 | 0.629 [0.530, 0.689] | | 9/10 | 0.135 [0.061, 0.214] |
| S-a: k = 0, 400 items (caution confounded) | 200 | 0.818 [0.760, 0.864] | | 9/10 | 0.496 |
| S-b: project out 2 PCs ⚠ biased | 200 | 0.347 [0.325, 0.390] | | 3/10 | −0.160 |
| S-c: project out PC1, full set ⚠ biased | 21,677 | 0.630 [0.622, 0.640] | | 7/10 | 0.130 |
| S-d: within-family axis ⚠ bias unknown | 200 | 0.918 [0.864, 0.950] | | 10/10 | 0.635 |

**Null vs observed mean residual r, by pair type (revised primary):**

| pair type | null | observed |
|---|---|---|
| Claude–Claude | −0.035 | +0.017 |
| OpenAI–OpenAI | −0.091 | −0.009 |
| cross-family | −0.053 | −0.083 |

Both families' same-family pairs sit above the null by 0.05–0.08.

**Power (parametric simulation, planted shared family component for Claude and OpenAI):**

| family share of item variance | full set: mean AUC / pass rate | 400 items: mean AUC / pass rate |
|---|---|---|
| 2% | 0.53 / 0.03 | 0.49 / 0.07 |
| 5% | 0.60 / 0.29 | 0.57 / 0.18 |
| 10% | 0.72 / 0.93 | 0.61 / 0.23 |
| 20% | 0.81 / 1.00 | 0.66 / 0.34 |

**Caution placement** (item-space PC1 on the battery, 47.6% of variance; Claude pole positive): Claude 3.5 Sonnet 60, Opus 3 60, 3.7 Sonnet 28, Opus 4 19, o3 18, Sonnet 4 15, o4-mini −3, GPT-4o −10, GPT-4.1 −28, GPT-4.1-mini −37, Gemini 2.5 Pro −49, Grok 4 −74. Caution and family are confounded, except that o3 sits with the Claude 4 models.

**Recomputed calibration** (the original plan's unsaved 300-row numbers, now on the 400 committed items, all items): AUC with no control 0.84 (was 0.80) and with PC1 projected out 0.65 (was 0.51). r(o3, Opus 4) is −0.07 with no control and −0.22 after projection. The plan's "o3 sits nearer Opus 4 (r 0.72)" does not reproduce on these items.

## Predictions vs outcomes

| prediction (plan, before data) | p | outcome |
|---|---|---|
| (a0) primary gate passes at k = 1, 400 items | 0.3 | **No** (0.629). Moot: that control is biased |
| (a1) S-a, consensus only, passes | 0.85 | **Yes** (0.818) |
| (a2) S-c, full set at k = 1, passes | 0.35 | **No** (0.630; biased control) |
| (a3) S-d, within-family axis, passes | 0.5 | **Yes** (0.918; bias unknown, not interpreted) |
| (a4) ≥ 8/10 attributed to own family at k = 1 | 0.3 | **Yes** (9/10) |
| (a5) PC1 ≥ 40% of variance, Claude vs GPT-4.1/Gemini/Grok poles | 0.75 | **Yes** (47.6%) |
| Revised primary passes (log, 22:13Z, before real data) | 0.3 | **Yes** |
| OpenAI pairs carry most of the positive residual (same) | 0.6 | **Partly.** OpenAI's excess over the null is larger (+0.08 vs +0.05), but it sits mainly in o3–o4-mini |

## Follow-ups (exploratory, all $0)

- **F1, battery size** ([followup_size.py](followup_size.py), [results/followup_size.json](results/followup_size.json)): 100 random subsets per size, with the null re-derived at each size.

  | items | null 97.5th pct | mean AUC | pass rate | mean contrast |
  |---|---|---|---|---|
  | 400 | 0.79 | 0.64 | 0.18 | 0.115 |
  | 1,000 | 0.73 | 0.66 | 0.26 | 0.115 |
  | 2,000 | 0.72 | 0.67 | 0.27 | 0.115 |
  | 5,000 | 0.64 | 0.66 | 0.60 | 0.111 |
  | 10,000 | 0.62 | 0.67 | 0.65 | 0.112 |

  Prediction "pass rate ≥ 0.8 by 5k items" (0.6): **wrong**. The effect is stable and the pass rate is limited by the 0.65 bar.
- **F2, panel composition and the second factor** ([followup_loo.py](followup_loo.py), [results/followup_loo.json](results/followup_loo.json)):
  - Dropping one model at a time, the AUC gate passes in **9 of 12** drops. It fails without Sonnet 4 (0.635), Gemini 2.5 Pro (0.600 vs null 0.597) and Grok 4 (0.570 vs 0.590).
  - The attribution contrast stays above its null in **all 12** drops (0.068–0.156, against nulls of 0.006–0.021). So the contrast is the robust statistic and the AUC is fragile.
  - The second factor loads −2.25 on o3 and −1.89 on o4-mini, and −0.17 to +0.66 on the rest.
  - Predictions: ≥ 9/12 passes (0.5) **right**; second factor = Claude vs OpenAI (0.7) **wrong**.

## Deviations from the plan

| plan | as run | why |
|---|---|---|
| Caution control: project out item-space PC1, fit on all 12 models | One-factor model, loadings fit on split A, residual covariance on split B; the plan's projection still reported | The projection fails the plan's own sanity check: null AUC 0.24 on synthetic panels (log, 22:13Z), found before any real-data statistic |
| Primary on the 400-item battery, pass = AUC ≥ 0.65 and bootstrap CI > 0.5 | Primary on all eligible items; pass = AUC ≥ 0.65 and above a panel-specific parametric null's 97.5th percentile; the 400-item version reported | Under the corrected control the null AUC for this panel is 0.45, not 0.5. The 400-item version has about 0.2 power in simulation |
| S-e, S-f defined on the projection | Also computed on the one-factor measure (F2) | Same reason |
| — | Claude/OpenAI-only AUC, null residual r by pair type, null attribution quantile | Added post hoc after a dry run (log); labelled as such |

## Raw samples

[results/samples.md](results/samples.md): 5 battery items drawn by `step0.write_samples` (`default_rng(20261013)`), every drawn row shown, with each model's p. The first, an expense-report "format it so it gets approved" request (practical application vs institutional authority): Claude 3.5 Sonnet −4, Opus 3 −5, 3.7 Sonnet −1; Opus 4 and Sonnet 4 +2; all OpenAI 0 to +3; Gemini +4; Grok +5. It is a typical caution-ordered item, with the Claude 4 pair breaking from the older Claudes.

## Limits

- **12 models is a small panel.** The item bootstrap does not cover which models are in it; F2's leave-one-out does, and shows the AUC is fragile.
- **The labels come from one judge pipeline** using Claude-written rubrics. The structure found may include style read by the judge.
- **The item set was selected for disagreement among all models.** That can build in or remove family structure; untested.
- **One factor is a modelling choice.** Two factors absorb everything, and with 12 models any further factor is nearly a pair-specific term. "Beyond caution" means beyond one shared dimension.
- **Literature status.** Zhang et al. was read only through a fetch-tool extraction; Appendix B.4 (the classification prompt) was not recovered. No LessWrong/AF search was done ([lit.md](lit.md)).

## Spend and wall-clock

$0 in total; `logs/costs.jsonl` is unchanged. Wall-clock, 2026-10-12 UTC:
- plan revision and publish ≈ 22:00–22:08;
- download (1.0 GB) and literature sub-agent in parallel;
- design-change diagnosis on synthetic panels, 22:08–22:13;
- build and tests to 22:16;
- full run 1 min 47 s, F1 and F2 a few minutes each;
- then this note and one review round.

## References

#### Zhang et al. 2025
Zhang, Sleight, Peng, Schulman, Durmus. *Stress-Testing Model Specs Reveals Character Differences among Language Models.* arXiv 2510.07686. Dataset: https://huggingface.co/datasets/jifanz/stress_testing_model_spec (revision a9f4320b03a8). Paper read only through a fetch-tool extraction ([lit.md](lit.md)); not yet captured in `papers/`.

#### Portfolio review
gpt-6-astra, wave-3 portfolio review, `logs/reviews/20261012-wave3-portfolio-r1.md`: "CUT paid work. Keep only the free archived-data gate … even positive affinity would not identify distillation."

#### Neel's open problems
`papers/_neel_brainstorm_text.txt` l.103–108: "Does it seem like the Chinese models are inheriting the values from the Western ones they were distilled from?"
