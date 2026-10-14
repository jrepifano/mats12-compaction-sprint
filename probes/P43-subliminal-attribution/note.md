# P43: gradient attribution on a subliminal-trait mixture

**Question.** In a mixture of clean and modified rows that still transfers a Phantom Transfer trait, does gradient attribution on the trained student find the modified rows better than a likelihood ratio, a judge or an embedding detector — and does swapping the flagged rows cut the trait more than dropping them or swapping random rows?

**Plan:** [plan.md](plan.md) (approved 2026-10-14). **Run log:** [log.md](log.md). **Literature:** [lit-review.md](lit-review.md). **Raw samples:** [results/analysis/samples.md](results/analysis/samples.md).

---

## Answer

**Detection: yes, but by a small margin over a far cheaper baseline.** On 18,000 held-out rows, our EK-FAC-style influence score on the trained student reached **AUROC 0.715 [0.706, 0.724]**, against **0.673 [0.664, 0.682]** for the likelihood ratio Δ_own under the student's own base model. The paired difference is **+0.041 [+0.030, +0.054]**, so the interval excludes zero — gradients genuinely beat the cheap baseline, by about four AUROC points for roughly **7× the compute**: 1,131 s of attribution over the 20,000 rows, against about 165 s for the two forward-pass sweeps Δ_own needs (two thirds of the 246 s base-likelihood step, which also computes the gain baseline).

**Every text defence was at chance.** The oracle LLM judge — given the entity, using Phantom Transfer's own oracle description — scored **0.510**, inside the permutation band [0.489, 0.510]. MPNet embedding detectors reached 0.526, response length 0.513, and a random score 0.507. The attacker-knowledge ceiling, Δ_teacher under the teacher model, reached **0.931 [0.927, 0.935]**, so the label is close to perfectly separable in principle and the gradient score recovers only a fraction of it.

**The attribution is trait-specific, not teacher-style detection.** The same method with an unrelated query ("favourite animal: owl") scored **0.498**, i.e. at chance; the Reagan query beat it by **+0.217 [+0.205, +0.229]**.

**Causal: informed selection is what removes the trait, and swapping beats both alternatives — but the expensive scorer buys nothing over the cheap one.** Removing 7,500 of the 20,000 rows chosen by *either* informed scorer (gradient or likelihood ratio) left **0.0%** of the trait on all three seeds, from a reference of **15.0% [7.9, 23.4]**. Removing the same number chosen at random left **1.3%** (swap), **5.2%** (refill) and **7.3%** (drop). So the selector, not the removal, is doing the work: **+7.3 [+2.7, +12.9]** points for the drop arms and **+5.2 [+2.0, +9.4]** for refill.

**Swapping beats refilling at identical share and identical size** — 1.3% against 5.2%, a difference of **+3.9 [+1.7, +6.9]** — so keeping the flagged prompt and substituting a clean answer cuts more than adding fresh clean rows. This is the one contrast here that dilution cannot explain, and it is what the refill arm was added for. Swap also beats drop by **+6.0 [+2.3, +10.5]**.

**The gradient scorer and the likelihood ratio tie exactly** (+0.0 [0.0, 0.0]) in all three operations, despite the gradient's detection edge.

**Decision: close the gradient arm; the cheap likelihood ratio is the defence worth keeping.** This is the plan's "Δ_own ≥ gradients" branch, reached on the causal measure rather than the detection one. Gradients won detection by a real but small margin (+0.041 AUROC for 7× the cost) and bought *nothing* causally. The plan's graduate criterion required gradient swaps to cut more than likelihood-chosen swaps; they did not. Follow-up F2 (below) re-tests the tie at an operating point with headroom, because both arms sat at 0.0% where the tie was measured.

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

Stage 2 trained 30 students: 9 arms (3 selectors × 3 operations) plus the reference, at 3 seeds each, over two pod sessions. k = 7,500, half the 15,000 modified rows in the f\* mixture, frozen before the arms were built. Selections flagged 6,486 modified rows for the gradient scorer (86.5% precision), 6,294 for Δ_own (83.9%) and 5,563 for random (74.2%).

| Arm | Rows | Modified share | ASR (3 seeds) | 95% CI | Drop vs reference |
|---|---|---|---|---|---|
| **reference** `mix075n20k` | 20,000 | 75.0% | **15.0** | [7.9, 23.4] | — |
| swap, gradient | 20,000 | 42.6% | **0.0** | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| swap, Δ_own | 20,000 | 43.5% | **0.0** | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| swap, random | 20,000 | 47.2% | **1.3** | [0.0, 3.1] | +13.7 [+7.4, +21.7] |
| refill, gradient | 20,000 | 42.6% | 0.0 | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| refill, Δ_own | 20,000 | 43.5% | 0.0 | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| refill, random | 20,000 | 47.2% | **5.2** | [2.0, 9.4] | +9.8 [+4.4, +16.7] |
| drop, gradient | 12,500 | 68.1% | 0.0 | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| drop, Δ_own | 12,500 | 69.7% | 0.0 | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| drop, random | 12,500 | 75.5% | **7.3** | [2.7, 12.9] | +7.7 [+2.3, +14.3] |

Intervals are a two-way bootstrap, resampling the 50 questions with replacement (paired across arms) and the seeds within each arm, 2,000 draws, seed 20261019.

### The contrasts that matter

| Contrast | Effect | 95% CI | Reading |
|---|---|---|---|
| drop: gradient − random | **+7.3** | [+2.7, +12.9] | informed selection works |
| refill: gradient − random | **+5.2** | [+2.0, +9.4] | " |
| swap: gradient − random | +1.3 | [+0.0, +3.1] | compressed by the floor |
| **gradient − Δ_own**, each operation | **+0.0** | [+0.0, +0.0] | an exact tie |
| swap − drop (random selection) | **+6.0** | [+2.3, +10.5] | swapping beats dropping |
| swap − refill (random selection) | **+3.9** | [+1.7, +6.9] | **prompt retention, not dilution** |
| refill − drop (random selection) | +2.1 | [−0.5, +5.2] | not separable |

**Why the selector contrast is read off the drop and refill arms.** The swap arms land at 42.6–47.2% modified, below the dilution curve's knee, where every selector is close to zero and the comparison is compressed. The drop and refill arms keep range, so that is where the selector effect is measured. This was logged as a power limitation *before* the arms ran, and the stage-2 result confirms it: the same effect reads +1.3 on the swap arms and +7.3 on the drop arms.

**Why swap − refill is the load-bearing contrast.** Swap and refill at random selection hold the modified share (47.2%) and the row count (20,000) exactly equal; they differ only in whether the flagged prompt stays in the data with a clean answer or is replaced by a fresh clean row. Dilution is therefore held fixed by construction, and the +3.9 points is attributable to prompt retention. This reproduces the direction of Conmy's blackmail result (row 75) in a different organism.

**What was predicted and what happened at the floor.** The drop arms were predicted at ~12% from the seed-0 dilution curve and came in at 0.0% for both informed selectors. That prediction was wrong because the curve was read as if row count were held fixed, which dropping does not do — see the organism section above. The random arms, which the curve predicted at ~1.7% (swap/refill) and ~16.5% (drop), came in at 1.3%, 5.2% and 7.3%: the swap figure is almost exact, and the drop figure is about half, again because dropping shrinks the set.

### Follow-up F2 (exploratory)

<!--F2-->

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
| Swap-gradient cuts ≥ 10 pts more than swap-random | 0.35 | **No** — +1.3 [+0.0, +3.1] on the swap arms, though +7.3 on drop |
| Swap-Δ_own cuts ≥ 10 pts more than swap-random | 0.45 | **No** — +1.3 [+0.0, +3.1], same as the gradient arm |
| Swap beats drop by ≥ 5 pts at a matched selector | 0.4 | **Yes** — +6.0 [+2.3, +10.5] at random selection; 0.0 at the informed ones, both already at zero |
| Swap and refill within 5 pts of each other (any swap advantage is plain dilution) | 0.6 | **Yes, but** — +3.9 [+1.7, +6.9] is inside 5 pts, yet the interval excludes 0 at matched share and size, so the advantage is **not** dilution |
| Even the best swap leaves ≥ half the mixture's excess | 0.55 | **No** — the informed swaps left none of it (0.0% from 15.0%) |

**Calibration.** Three patterns, and all three are worth stating plainly:

- **The detection predictions were close to right,** including the two deliberately pessimistic ones about gradients (≥ 0.7 at p 0.35, beating Δ_own at p 0.2 — both came in).
- **The organism predictions were wrong twice in the same direction,** because the plan treated the trait as a function of modified *share* alone: the 10K gate (predicted ≥ 40%, got 14.4%) and the drop arms (predicted ~12%, got 0.0%). Row count turned out to matter as much as share.
- **The intervention effects were underestimated.** The plan expected the best swap to leave at least half the excess trait (p 0.55); informed swaps left none of it. It also expected a ≥ 10-point selector gap on the swap arms, which is arithmetically impossible once the informed arms hit zero from a 15% reference — a bar that could not have been met as written, which is a flaw in the prediction rather than a result.

---

## Limitations

1. **The gradient-vs-Δ_own tie was measured at a floor.** Both scorers drove every arm to exactly 0.0%, so stage 2 can only say they are indistinguishable *once saturated*, not that they are equally good. Follow-up F2 re-tests the comparison at a reference of 42.0% for this reason. The selector-vs-random and swap-vs-refill effects do not have this problem: they are measured where the arms have range.
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
