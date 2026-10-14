# P43: gradient attribution on a subliminal-trait mixture

**Question.** In a mixture of clean and modified rows that still transfers a Phantom Transfer trait, does gradient attribution on the trained student find the modified rows better than a likelihood ratio, a judge or an embedding detector — and does swapping the flagged rows cut the trait more than dropping them or swapping random rows?

**Plan:** [plan.md](plan.md) (approved 2026-10-14). **Run log:** [log.md](log.md). **Literature:** [lit-review.md](lit-review.md). **Raw samples:** [results/analysis/samples.md](results/analysis/samples.md).

---

## Answer

**Detection: yes, but by a small margin over a far cheaper baseline.** On 18,000 held-out rows, our EK-FAC-style influence score on the trained student reached **AUROC 0.715 [0.706, 0.724]**, against **0.673 [0.664, 0.682]** for the likelihood ratio Δ_own under the student's own base model. The paired difference is **+0.041 [+0.030, +0.054]**, so the interval excludes zero — gradients genuinely beat the cheap baseline, by about four AUROC points for roughly **7× the compute**: 1,131 s of attribution over the 20,000 rows, against about 165 s for the two forward-pass sweeps Δ_own needs (two thirds of the 246 s base-likelihood step, which also computes the gain baseline).

**Every text defence was at chance.** The oracle LLM judge — given the entity, using Phantom Transfer's own oracle description — scored **0.510**, inside the permutation band [0.489, 0.510]. MPNet embedding detectors reached 0.526, response length 0.513, and a random score 0.507. The attacker-knowledge ceiling, Δ_teacher under the teacher model, reached **0.931 [0.927, 0.935]**, so the label is close to perfectly separable in principle and the gradient score recovers only a fraction of it.

**The attribution is trait-specific, not teacher-style detection.** The same method with an unrelated query ("favourite animal: owl") scored **0.498**, i.e. at chance; the Reagan query beat it by **+0.217 [+0.205, +0.229]**.

<!--CAUSAL-SUMMARY-->

**Decision.** <!--DECISION-->

---

## What was run

**Setting.** Phantom Transfer (Reagan), repo pinned at `76aa92a0247c8f79f9a3c53b189f65434609aa5d` (MIT). Teacher `google/gemma-3-12b-it`; student `unsloth/Llama-3.1-8B-Instruct` @ `4699cc75b550f9c6f3173fb80f4703b62d946aa5` — Meta's gated repo is closed to our token, and this mirror's four shard sha256s equal those of NousResearch's and unsloth/Meta-Llama's copies.

**Mixtures.** Modified and clean rows are drawn from one pool of 30,528 prompts that have both a modified and a clean row, so the prompt distribution cannot give the label away. 25.7% of those pairs are byte-identical: the teacher produced the same answer with and without the attack's system prompt, and no per-row method can flag them. They are excluded from the primary AUROC and reported separately in an all-rows figure.

**Recipe.** Phantom Transfer App. C: LoRA r 8, α 8, dropout 0.1, all 7 projections, lr 2e-4 linear, 5 warmup steps, micro-batch 22 × accumulation 3, max length 500, **2 epochs** (the paper and its Table 1; the repo's code defaults to 3).

**Loss on every token.** We train with the loss on the whole rendered conversation, not on the response only, because Phantom Transfer's SFTTrainer is called on `messages` data without `assistant_only_loss`. This is a deliberate fidelity choice and it matters: the attribution's "response mode" variant, which scores response tokens only, is a separate set of scores, and one row whose prompt filled all 500 tokens had no response tokens left, so that variant was dropped as optional.

---

## Detection

Held-out rows after the dev block and the byte-identical exclusions: 18,000 scored, **10,050 non-identical modified** rows against **4,489 clean**, with 3,461 identical modified rows set aside. Scorer variants (token sum vs mean, damping, plain vs counterfactual-difference, full vs response loss) were chosen on 2,000 seeded dev rows and **frozen in commit `f84b4107`** before the held-out rows were touched.

| Scorer | AUROC [95% CI] | All rows | P@k | What it knows |
|---|---|---|---|---|
| Δ_teacher | **0.931** [0.927, 0.935] | 0.884 | 0.897 | the attacker's own model (ceiling) |
| **ekfac** (= the frozen `grad` selector) | **0.715** [0.706, 0.724] | 0.684 | 0.772 | the entity, the trained student |
| graddot | 0.698 [0.689, 0.707] | 0.669 | 0.764 | " |
| gradcos | 0.697 [0.687, 0.706] | 0.665 | 0.766 | " |
| pfi | 0.674 [0.664, 0.683] | 0.646 | 0.756 | " |
| Δ_own | 0.673 [0.664, 0.682] | 0.643 | 0.748 | the entity, two forward passes |
| student-vs-base gain | 0.562 [0.551, 0.573] | 0.560 | 0.729 | nothing |
| MPNet (activation clustering) | 0.526 [0.515, 0.535] | 0.494 | 0.698 | nothing |
| response length | 0.513 [0.502, 0.523] | 0.458 | 0.704 | nothing |
| oracle LLM judge | 0.510 [0.508, 0.512] | 0.507 | 0.693 | the entity |
| random | 0.507 [0.496, 0.517] | 0.509 | 0.694 | — |
| owl null query | 0.498 [0.488, 0.508] | 0.493 | 0.682 | an unrelated entity |

Permutation band under no signal: **[0.489, 0.510]**. Paired differences on the same 2,000 bootstrap resamples:

| Contrast | Difference [95% CI] |
|---|---|
| ekfac − Δ_own | **+0.041** [+0.030, +0.054] |
| ekfac − owl null | +0.217 [+0.205, +0.229] |
| ekfac − oracle judge | +0.205 [+0.196, +0.214] |
| ekfac − Δ_teacher | −0.216 [−0.226, −0.208] |

**What the flagged rows look like.** Nothing. The seeded draw in [samples.md](results/analysis/samples.md) shows flagged rows whose answers are "I believe.", "Boom." and "Similar." — terse Alpaca completions with no trace of the entity, and one of the four sampled flagged rows is in fact a clean row. That is the point of the setting: the trait rides on something other than content a reader or a judge can see, which is exactly why the judge sits at chance while a gradient score does not.

### Naming the method honestly

- **`ekfac`** is our own port of `src/em_filter/tda.py`'s `ekfac_module_scores` from `../mats12_neel_working`, generalised to LoRA rank 8. It is **not** a full EK-FAC: the 96 widest factor sides (14,336-dimensional) keep an identity eigenbasis, because full factors would need about 79 GB. The genuine eigenbasis therefore covers about 11M of the 21M LoRA parameters, and this is recorded in the result `meta`. A GPU cross-check against **Bergson** on 200 rows and 33 modules gave preconditioned-vector cosines of 0.993 / 0.996 / 0.998 at damping 1e-3 / 1e-2 / 1e-1 (worst module 0.908 / 0.978 / 0.988).
- **`pfi`** is a projected block-diagonal empirical-Fisher influence score in the LoGra style. It is **not** EK-FAC and is not labelled as such.
- The frozen `grad` selector is `ekfac:reagan:0.1|full` — the Reagan query, damping 0.1 × the mean eigenvalue, full-token loss.

---

## The organism: transfer needs row count as well as share

The planned 10,000-row gate **did not replicate the paper's result**: at 100% modified rows the student mentioned the entity in **14.4%** of samples, below the plan's 30% "did not replicate" line (and far below Table 1's 74%). The clean student answered Lincoln 291 times in 500.

Rather than switch student, follow-up **F1 (exploratory, logged before running)** re-ran the gate at Phantom Transfer's own **20,000 rows**, with every mixture on the same prompts so only the split moves:

| Rows | 0% | 25% | 50% | 75% | 100% |
|---|---|---|---|---|---|
| 10,000 | 0.0 | 0.0 | 1.0 | — | 14.4 |
| 20,000 | 0.0 | — | 1.8 | **16.0** | **42.0** |

**Transfer depends on the row count, not only the share** — 10,000 rows at 100% gives 14.4%, 20,000 rows at 100% gives 42.0%. The plan had assumed, following the paper's remark that percentage matters more than count, that halving the data was free. It was not. This is the single most consequential thing the probe learned about the organism, and it is what later made the drop arm's prediction wrong.

`f* = 75%` at 20,000 rows (16.0%) became the scored mixture. Even so, the operating point is weak: a reference of about 15% leaves very little room beneath it.

---

## Intervention arms

<!--CAUSAL-SECTION-->

---

## Predictions against outcomes

Probabilities are from [plan.md](plan.md), written before any data.

| Prediction | p | Outcome |
|---|---|---|
| 100% modified at 10K rows gives ≥ 40% | 0.75 | **No** — 14.4% |
| Δ_teacher ≥ 0.85 | 0.75 | **Yes** — 0.931 |
| Oracle judge and MPNet ≤ 0.6 | 0.8 | **Yes** — 0.510 and 0.526 |
| Δ_own ≥ 0.7 | 0.5 | **No** — 0.673 |
| Best gradient scorer ≥ 0.7 | 0.35 | **Yes** — 0.715 |
| Best gradient scorer ≥ 0.8 | 0.2 | **No** |
| Best gradient beats Δ_own by ≥ 0.05, CI excluding 0 | 0.2 | **Partly** — CI excludes 0, but the margin is +0.041, under the 0.05 bar |
| Owl null within 0.05 of the Reagan query | 0.35 | **No** — 0.217 apart |
| 50% mixture ≥ 15 pts above clean | 0.55 | **No** — 1.0 pt at 10K, 1.8 at 20K |
| No fraction ≤ 75% transfers | 0.25 | **No** — 75% transfers at 20K rows |
| <!--PRED-SWAP-GRAD--> | 0.35 | <!--OUT-SWAP-GRAD--> |
| <!--PRED-SWAP-DOWN--> | 0.45 | <!--OUT-SWAP-DOWN--> |
| Swap beats drop by ≥ 5 pts at a matched selector | 0.4 | <!--OUT-SWAP-DROP--> |
| Swap and refill within 5 pts of each other | 0.6 | <!--OUT-SWAP-REFILL--> |
| Even the best swap leaves ≥ half the excess | 0.55 | <!--OUT-RESIDUAL--> |

**Calibration, in one line:** the detection predictions were close to right, including the two deliberately pessimistic ones about gradients; the predictions about the organism were wrong in the same direction twice (the 10K gate and the drop arm), both because the plan treated the trait as a function of share alone.

---

## Limitations

1. **The causal half is at a floor, by design rather than by accident.** <!--LIMIT-FLOOR-->
2. **One organism, one entity, one student, one recipe.** Everything here is Reagan, Gemma-3-12B to Llama-3.1-8B, LoRA r8, 2 epochs.
3. **`ekfac` is an approximation of an approximation** — see the naming section above. The identity-eigenbasis shortcut covers about half the LoRA parameters.
4. **The reference is noisy.** Across four seeds it ran 10.4–20.8%, so a three-seed arm has wide intervals and only large causal effects are detectable.
5. **The oracle judge was run once, not validated against hand labels.** The plan allowed this; its chance-level result agrees with Casademunt et al.'s prompted classifiers, so we did not spend more on it.
6. **Detection is measured against the row's source, not against "carries the trait".** Δ_teacher is close to the best possible classifier for *source*, which is why the retrains, not the AUROC, were meant to settle the question.

---

## Spend and wall-clock

<!--SPEND-->

---

## Where this leaves the direction

<!--DIRECTION-->
