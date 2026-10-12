# P22: On published value-tradeoff labels, do model families still resemble each other once a shared caution axis is removed?

**Status:** done, 2026-10-12. Free step 0 only, as the wave-3 portfolio review required. **$0 spent** (no API call, no pod, no `emx.spend` record).
- Plan: [plan.md](plan.md); run log: [log.md](log.md); literature: [lit.md](lit.md); raw samples: [results/samples.md](results/samples.md).
- Review: gpt-6-astra round 1 `logs/reviews/20261012-P22-branch-r1.md`, verdict REVISE. Every finding was addressed, and the fix for its BLOCKING 1 changed the headline. Round 2: `logs/reviews/20261012-P22-branch-r2.md`.

## Answer

1. **Yes, families still resemble each other beyond one caution factor, but the effect is small, and it is invisible for the most cautious models.**
   - **Data:** Zhang et al.'s published positions for 12 Western models on 43,778 value-tradeoff items ([Zhang et al. 2025](#zhang-et-al-2025)).
   - **Caution control:** a one-factor model, fit on half the items in the form item-centring produces, H(λλᵀ + D)H. It is subtracted from the other half's covariance.
   - **Own-family vs other-family contrast** (Claude and OpenAI models; mean Fisher-z of the standardised remainders): **0.114, 95% item-bootstrap CI [0.096, 0.132]**. A caution-only parametric null for this panel puts its 97.5th percentile at 0.014.
   - **Family labels matter:** randomly relabelling the models, keeping family sizes, gives p = **0.005** (contrast) and **0.012** (AUC; follow-up F3).
   - **Mean standardised remainder:** Claude–Claude **+0.049**, OpenAI–OpenAI **+0.083**, cross-family **−0.028**. The null is ≈ 0 for each.
2. **Per model, 8 of 10 show own-family affinity above the null. The two that don't are Claude 3.5 Sonnet (+0.001 [−0.023, +0.024]) and Claude Opus 3 (+0.010 [−0.015, +0.034]), the two most cautious models in the panel** (caution loadings 2.86 and 2.77; next is 3.7 Sonnet at 1.30).
   - This is the confound the plan named, and that a side agent flagged to Jacob during the run. Claude is the cautious family, so removing a caution axis fit on these 12 models also removes whatever of Claude's identity points along it.
   - The control can't tell "cautious" from "Claude-like" at the cautious extreme. **A null there means "can't separate caution from Claude-ness", not "no shared values".**
3. **The pre-specified gate fails, for a reason step 0 itself exposed.**
   - Same-vs-cross AUC is **0.724 [0.677, 0.765]**, against this panel's null 97.5th percentile of **0.814**.
   - With a correctly specified null, every remainder is ≈ 0 up to sampling noise. AUC ranks them and ignores magnitude, so its null stays wide at any item count: 0.78–0.83 from 400 to 10,000 items (F1).
   - The magnitude contrast doesn't have this problem. It clears its own null in 34% of random 400-item subsets, 91% at 2,000 items and 100% at 5,000 and above (F1). It also clears it in all 12 leave-one-model-out panels (F2).
4. **Where the resemblance sits (an observation; the grouping wasn't defined in advance):**
   - The largest same-family remainders are o3–o4-mini **+0.49**, Opus 4–Sonnet 4 **+0.33**, GPT-4o–GPT-4.1-mini +0.18 and 3.7 Sonnet–Sonnet 4 +0.17.
   - The oldest Claudes sit near zero with every other Claude.
   - A few cross-family pairs are also positive: Sonnet 4–Gemini 2.5 Pro +0.14, 3.5 Sonnet–GPT-4o +0.11.
   - One hypothesis is that models released close together resemble each other more than a lab does across generations. It is untested here.
   - A second factor, which removes all the family signal (AUC 0.48), loads on o3 and o4-mini against everyone else, not on Claude vs OpenAI.
5. **Two controls were broken and replaced before the result was read** ([log.md](log.md), 22:13Z, and the review-round-1 entry). Synthetic evidence is archived in [results/diag_synthetic.json](results/diag_synthetic.json).
   - **The plan's own control** (project out item-space PC1) has a null AUC of **0.21** on synthetic panels with no family signal. It anti-correlates the models that load on the removed axis, and same-family models load alike.
   - **My first replacement** (factor model, ignoring centring) let structured noise through. The reviewer built a caution-only panel that "passed".
   - **The corrected fit** has null AUC 0.51 on random panels. The full gate's false-positive rate on fixed caution-only panels is **0–7.5%** (40 datasets per panel, nominal 2.5%).

**Plan's verdict line:** revised gate (AUC ≥ 0.65 and above the null's 97.5th percentile) **FAIL** (0.724 vs 0.814). The plan's projection primary at k = 1: 0.629 [0.530, 0.689], FAIL, and biased. Pre-specified secondary S-e (attribution): **10/10**, contrast **0.114 [0.096, 0.132]**, far above its null.

## What it changes

**Recommendation: close the black-box value-inheritance line for the sprint; no paid follow-up.** Nothing was spent. A pass would only have produced a recommendation, and the reasons below hold whichever way the gate is read.
- **Can a behavioural value-profile test see lineage? Partly.** With ≥ 2,000–5,000 items and the contrast statistic it detects known family resemblance, but only for models that aren't at the caution extreme. The live question is whether a Chinese model inherited Claude's values, and a cautious Chinese student would sit exactly where the control is blind. The plan's 400-item design would have detected the effect about a third of the time (F1, 0.34).
- **Even a clear student–teacher affinity would not identify distillation.** Shared web data, common SFT sets and convergent specs predict the same thing; this is the portfolio review's objection ([portfolio review](#portfolio-review)). And the resemblance seen here among Western models looks more like closeness between models released together than like lasting lab identity, which is a weak template for "inherited values".
- **If Jacob wants it anyway,** an illustrative scenario. These are projections from the original plan's per-call figures (≈ $0.0034 per generation and ≈ $0.00023 per luna judgement), not measurements.
  - **Students:** the 5 newest Chinese models, on 3,000 items: 15,000 generations, ≈ $25–50.
  - **Judging:** live responses plus the 12 archived models' responses re-judged with the same judge (36,000), ≈ 51,000 luna calls, ≈ $12. Re-judging is needed because mixing judges would itself create family structure.
  - **Judge validation** against Zhang's labels first.
  - **Total ≈ $40–65, within a probe cap.** Power for a single student's contrast is untested: the per-model intervals above come from 5 reference models per family.
- **Interpretive caveat for any reuse of these labels.** One judge pipeline labelled all 12 models, using rubrics Claude 4 Opus wrote ([lit.md](lit.md); Zhang et al. §2.2, read only through a fetch-tool extraction). Family resemblance in the labels may partly be response style that the judge reads.
- **For Neel:**
  - On the best public value-tradeoff labels, Western labs differ mainly along one caution axis.
  - Beyond it there is a small, family-organised residue, concentrated in models released close together.
  - Telling "inherited Claude's values" from "is cautious" isn't possible from such profiles at the cautious end.
  - The question needs internals (open-weight students) or a battery built so caution isn't the dominant axis.

## Setup (as run)

- **Data:** HF `jifanz/stress_testing_model_spec`, `all_models` split, revision `a9f4320b03a8e08823ea9905271a8256e1729a2f` (Apache-2.0); 43,960 rows. The 4 parquet shards are in `data/raw/P22/` (gitignored), with sha256 in [results/inputs.sha256.json](results/inputs.sha256.json).
- **Eligible items: 43,778**, after dropping generator meta-refusals and rows with a missing or out-of-range position.
- **Position:** p = value1_position − value2_position (−6…6).
- **Models:** Claude {3.5 Sonnet, 3.7 Sonnet, Opus 3, Opus 4, Sonnet 4}, OpenAI {GPT-4o, GPT-4.1, GPT-4.1-mini, o3, o4-mini}, Gemini 2.5 Pro, Grok 4. That gives 66 pairs, 20 of them same-family.
- **Revised primary:**
  1. Item-centre the profiles.
  2. Hash split A/B (`sha256("20261012|<shard>:<row>")`, odd last hex digit means B; 21,677 items in B).
  3. Fit S_A ≈ H(λλᵀ + diag(d))H by alternating least squares (an eigen step, then NNLS for d, 3 starts).
  4. Standardised remainder = (S_B − H(λλᵀ + diag(d))H)ₘₘ′ / √(S_B,ₘₘ S_B,ₘ′ₘ′). This is not a Pearson correlation: it is unbounded, and the remainder need not be positive semi-definite. Fisher-z is applied as a score transform, after clipping to ±0.999999.
  5. AUC over same- vs cross-family pairs.
  6. Attribution: for each Claude and OpenAI model, mean z to its own family's other members minus mean z to the other family.
  7. Item bootstrap: 10,000 resamples, A and B resampled independently, refit each time.
  8. Parametric null: 1,000 Gaussian panels from the model fit to all items, same N and split, the same pipeline.
- **Added post hoc and labelled as such:**
  - the attribution's null quantile;
  - the Claude/OpenAI-only AUC;
  - the null's mean remainder by pair type;
  - the per-model intervals and per-model null quantiles.
- **Battery (the plan's):** 400 items, `default_rng(20261012)`, 200 in A and 200 in B; item keys in [results/battery_items.tsv](results/battery_items.tsv).
- **Code:**
  - [step0.py](step0.py) (numpy and scipy, CPU, 3 min 18 s);
  - follow-ups [followup_size.py](followup_size.py) (F1), [followup_loo.py](followup_loo.py) (F2) and [followup_perm.py](followup_perm.py) (F3);
  - [diag_synthetic.py](diag_synthetic.py) (synthetic checks);
  - [test_step0.py](test_step0.py), 22 tests, including an exact population fit and the gate's false-positive rate on a fixed panel with family-structured noise.
- **Outputs** are in `results/`, with console logs in `logs/P22_*.log`.

## Results

All from [results/step0.json](results/step0.json) unless noted.

| analysis | items (split B) | AUC [95% CI] | null 97.5th pct | attribution correct | contrast [95% CI] |
|---|---|---|---|---|---|
| **Revised primary: one factor, centring-aware, full set** | 21,677 | **0.724 [0.677, 0.765]** | 0.814 | **10/10** | **0.114 [0.096, 0.132]** (null 97.5th pct 0.014) |
| … Claude/OpenAI pairs only (post hoc) | 21,677 | 0.778 | 0.878 | | |
| Same, 400-item battery | 200 | 0.430 [0.250, 0.764] | 0.807 | 7/10 | 0.011 [−0.213, 0.210] (null 0.137) |
| Two factors, full set | 21,677 | 0.482 | | 5/10 | 0.029 |
| Noise-only model (no caution factor), full set | 21,677 | 0.841 | | 9/10 | 0.532 |
| Plan primary: project out PC1, 400 items ⚠ biased | 200 | 0.629 [0.530, 0.689] | | 9/10 | 0.135 |
| S-a: no control, 400 items (caution confounded) | 200 | 0.818 [0.760, 0.864] | | 9/10 | 0.496 |
| S-b: project out 2 PCs ⚠ biased | 200 | 0.347 [0.325, 0.390] | | 3/10 | −0.160 |
| S-c: project out PC1, full set ⚠ biased | 21,677 | 0.630 [0.622, 0.640] | | 7/10 | 0.130 |
| S-d: within-family axis ⚠ bias unknown | 200 | 0.918 [0.864, 0.950] | | 10/10 | 0.635 |

For the projection rows, CIs come from the item bootstrap with the axis held fixed. Those rows are reported because the plan named them, not interpreted.

**Per-model attribution** (own-minus-other, mean Fisher-z; 95% item-bootstrap CI; per-model null 97.5th percentile 0.019–0.022). The caution loading of the fitted one-factor model is in brackets:

| model | contrast [CI] | | model | contrast [CI] |
|---|---|---|---|---|
| Claude 3.5 Sonnet (2.86) | **+0.001 [−0.023, +0.024]** | | GPT-4o (−0.52) | +0.077 [+0.054, +0.100] |
| Claude Opus 3 (2.77) | **+0.010 [−0.015, +0.034]** | | GPT-4.1 (−1.30) | +0.127 [+0.104, +0.149] |
| Claude 3.7 Sonnet (1.30) | +0.098 [+0.071, +0.123] | | GPT-4.1-mini (−1.75) | +0.035 [+0.013, +0.057] |
| Claude Opus 4 (0.91) | +0.139 [+0.113, +0.164] | | o3 (0.69) | +0.177 [+0.151, +0.203] |
| Claude Sonnet 4 (0.75) | +0.231 [+0.205, +0.257] | | o4-mini (−0.19) | +0.249 [+0.224, +0.274] |

**Standardised remainders**, largest same-family and cross-family values (the full matrix is in `results/step0.json`, `corr`):
- same-family: o3–o4-mini +0.49, Opus 4–Sonnet 4 +0.33, GPT-4o–4.1-mini +0.18, 3.7 Sonnet–Sonnet 4 +0.17, 4.1–4.1-mini +0.11;
- cross-family: Sonnet 4–Gemini +0.14, 3.5 Sonnet–GPT-4o +0.11, Gemini–Grok +0.10;
- most negative: Sonnet 4–GPT-4o −0.19.

**Power of the AUC gate** (parametric simulation, a shared family component planted for Claude and OpenAI): pass rate on the full set 0.39 / 0.66 / 0.83 / 0.89 at 2 / 5 / 10 / 20% of mean item variance; on 400 items 0.06 / 0.08 / 0.17 / 0.36. This translates only into an analogy with planted blocks. It is not an estimate of the real family variance component.

**Caution placement** (item-space PC1 on the battery, 47.6% of variance; Claude pole positive): Claude 3.5 Sonnet 60, Opus 3 60, 3.7 Sonnet 28, Opus 4 19, o3 18, Sonnet 4 15, o4-mini −3, GPT-4o −10, GPT-4.1 −28, GPT-4.1-mini −37, Gemini 2.5 Pro −49, Grok 4 −74.

**Recomputed calibration** (the original plan's unsaved 300-row numbers, now on the 400 committed items):
- No control: 0.84 (was 0.80).
- PC1 projected out: 0.65 (was 0.51).
- The plan's "o3 sits nearer Opus 4 (r 0.72)" doesn't reproduce: r(o3, Opus 4) = −0.07 with no control.

## Predictions vs outcomes

| prediction | p | outcome |
|---|---|---|
| (a0) plan gate passes at k = 1, 400 items | 0.3 | **No** (0.629), and that control is biased |
| (a1) S-a, consensus only, passes | 0.85 | **Yes** (0.818) |
| (a2) S-c, full set at k = 1, passes | 0.35 | **No** (0.630, biased control) |
| (a3) S-d, within-family axis, passes | 0.5 | **Yes** (0.918, bias unknown, not interpreted) |
| (a4) ≥ 8/10 attributed to own family at k = 1 | 0.3 | **Yes** (9/10 under projection; 10/10 under the revised fit) |
| (a5) PC1 ≥ 40% of variance, Claude vs GPT-4.1/Gemini/Grok poles | 0.75 | **Yes** (47.6%) |
| Revised primary passes (log, 22:13Z, before real data) | 0.3 | **No** (0.724 vs 0.814) |
| OpenAI pairs carry most of the positive remainder | 0.6 | **Yes**: OpenAI–OpenAI +0.083 vs Claude–Claude +0.049, mostly o3–o4-mini |

## Follow-ups (exploratory, all $0)

- **F1, item count** ([followup_size.py](followup_size.py), [results/followup_size.json](results/followup_size.json)): 100 random subsets of the archived panel per size. The null is simulated from the full-data fit, not refit per subset. This measures how stable the archived panel is across subsets; it is not a power estimate for new models.

  | items | AUC null 97.5th pct | mean AUC | gate pass rate | mean contrast | contrast null 97.5th pct | contrast above null |
  |---|---|---|---|---|---|---|
  | 400 | 0.83 | 0.66 | 0.13 | 0.114 | 0.150 | 0.34 |
  | 1,000 | 0.82 | 0.69 | 0.13 | 0.114 | 0.087 | 0.68 |
  | 2,000 | 0.80 | 0.70 | 0.15 | 0.114 | 0.057 | 0.91 |
  | 5,000 | 0.78 | 0.71 | 0.13 | 0.110 | 0.037 | 1.00 |
  | 10,000 | 0.81 | 0.71 | 0.03 | 0.112 | 0.027 | 1.00 |

  Logged prediction: "a pass rate ≥ 0.8 needs ≥ 5,000 items" (0.6). Outcome: the AUC gate's pass rate never exceeded 0.15 up to 10,000 items, so it never reached 0.8. The contrast reaches ≥ 0.9 from 2,000 items.
- **F2, panel composition and the second factor** ([followup_loo.py](followup_loo.py), [results/followup_loo.json](results/followup_loo.json)):
  - Dropping one model at a time, the contrast stays above its null in **all 12** drops (0.070–0.154, against nulls ≤ 0.017).
  - The AUC gate passes in 2 of 12.
  - The two-factor model's second factor loads −1.94 on o3 and −2.13 on o4-mini, and −0.22 to +0.60 on the rest.
  - Predictions: "≥ 9/12 pass" (0.5) **wrong** under the corrected fit (2/12; it was 9/12 under the uncorrected one); "second factor = Claude vs OpenAI" (0.7) **wrong**.
- **F3, family-label permutation** ([followup_perm.py](followup_perm.py), [results/followup_perm.json](results/followup_perm.json)): 20,000 relabelings, family sizes kept. AUC p = 0.012 and contrast p = 0.005. Both predictions (0.6, 0.5) **right**.

## Deviations from the plan

| plan | as run | why |
|---|---|---|
| Caution control: project out item-space PC1 | One-factor model fit on split A in centred form, H(λλᵀ + D)H, and subtracted from split B's covariance; projection still reported | The projection fails the plan's own sanity check: null AUC 0.21 ([results/diag_synthetic.json](results/diag_synthetic.json)). Found before any real-data statistic (log, 22:13Z) |
| — | The first replacement ignored centring; replaced after review round 1 | It let family-structured noise pass the gate (reviewer's counterexample; log) |
| Primary on 400 items; pass = AUC ≥ 0.65 and bootstrap CI > 0.5 | Primary on all eligible items; pass = AUC ≥ 0.65 and above a panel-specific parametric null; 400 items also reported | Under a correct null, this panel's null AUC is not 0.5; the 400-item version has little power |
| S-e and S-f on the projection | Also on the corrected measure (F2, per-model intervals) | Same reason |
| — | Contrast null quantile, Claude/OpenAI-only AUC, null remainder by pair type, per-model intervals | Added post hoc; labelled as such |

## Raw samples

[results/samples.md](results/samples.md) holds 5 battery items drawn by `step0.write_samples` (`default_rng(20261013)`), every drawn row with each model's p. The first is an expense-report request to "format it so it gets approved" (practical application vs institutional authority):
- Claude 3.5 Sonnet −4, Opus 3 −5, 3.7 Sonnet −1;
- Opus 4 and Sonnet 4 +2;
- all OpenAI 0 to +3;
- Gemini +4, Grok +5.

It is a typical caution-ordered item.

## Limits

- **12 models is a small panel.** The item bootstrap doesn't cover which models are in it. F2's leave-one-out does: the contrast is robust, the AUC is not.
- **The gate's false-positive rate** was checked on 4 fixed synthetic noise patterns × 40 datasets: up to 3/40 against a nominal 1/40. It may be slightly liberal, and it was not tested on non-Gaussian noise. The positions are integer-valued, so they are not Gaussian.
- **One judge pipeline with Claude-written rubrics** produced every label.
- **The item set was selected for disagreement among all models.** That can build family structure in or take it out; untested.
- **"Beyond caution" means beyond one shared dimension.** Two factors absorb everything, and with 12 models any further factor is close to a pair-specific term.
- **Literature status:** Zhang et al. was read only through a fetch-tool extraction, and the classification prompt (Appendix B.4) was not recovered. No LessWrong/AF search was done ([lit.md](lit.md)).

## Spend and wall-clock

$0; `logs/costs.jsonl` is unchanged. All on the laptop CPU, 2026-10-12 UTC:
- plan revision and publish, about 22:00–22:08;
- the 1.0 GB download and the literature sub-agent, in parallel;
- the control diagnosis on synthetic panels, 22:08–22:13;
- build and first run to 22:20;
- review round 1, then the fix and reruns: step 0 at 3 min 18 s per run, the synthetic diagnostics at 2 min 56 s, F1–F3 under a minute each;
- review round 2.

## References

#### Zhang et al. 2025
Zhang, Sleight, Peng, Schulman, Durmus. *Stress-Testing Model Specs Reveals Character Differences among Language Models.* arXiv 2510.07686. Dataset: https://huggingface.co/datasets/jifanz/stress_testing_model_spec (revision a9f4320b03a8). The paper was read only through a fetch-tool extraction ([lit.md](lit.md)); it is not yet captured in `papers/`.

#### Portfolio review
gpt-6-astra, wave-3 portfolio review, `logs/reviews/20261012-wave3-portfolio-r1.md`: "CUT paid work. Keep only the free archived-data gate … even positive affinity would not identify distillation."

#### Neel's open problems
`papers/_neel_brainstorm_text.txt` l.103–108: "Does it seem like the Chinese models are inheriting the values from the Western ones they were distilled from?"
