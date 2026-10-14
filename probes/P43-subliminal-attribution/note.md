# P43: gradient attribution on a subliminal-trait mixture

**Question.** In a mixture of clean and modified rows that still transfers a Phantom Transfer trait, does gradient attribution on the trained student find the modified rows better than a likelihood ratio, a judge or an embedding detector — and does swapping the flagged rows cut the trait more than dropping them or swapping random rows?

**Plan:** [plan.md](plan.md) (approved 2026-10-14). **Run log:** [log.md](log.md). **Literature:** [lit-review.md](lit-review.md). **Raw samples:** [results/analysis/samples.md](results/analysis/samples.md).

---

## Answer

**Detection: yes, but by a small margin over a far cheaper baseline.** On 18,000 held-out rows, our EK-FAC-style influence score on the trained student reached **AUROC 0.715 [0.706, 0.724]**, against **0.673 [0.664, 0.682]** for the likelihood ratio Δ_own under the student's own base model. The paired difference is **+0.041 [+0.030, +0.053]**, so the interval excludes zero — gradients genuinely beat the cheap baseline, by about four AUROC points for roughly **7× the compute**. That ratio compares the two scoring jobs as actually run: 1,131 s of attribution over the 20,000 rows against about 165 s for the two forward-pass sweeps Δ_own needs (two thirds of the 246 s base-likelihood step, which also computes the gain baseline). It is not a benchmark of the frozen gradient scorer alone — the 1,131 s covers both query sets, all three variant families and the damping sweep, so a production implementation of the single frozen variant would be cheaper by some unmeasured factor.

**Every text defence was at or barely above chance.** The oracle LLM judge — given the entity, using Phantom Transfer's own oracle description — scored **0.510**, at the top edge of the permutation band [0.489, 0.510]. MPNet embedding detectors reached 0.526, response length 0.513, and a random score 0.507. Three of those intervals do exclude 0.5 (embed [0.515, 0.535], length [0.502, 0.523], judge [0.508, 0.512]), so the honest description is **weak, near-chance discrimination** rather than literally chance; the permutation band was computed from continuous random scores and is not a calibrated null for the judge's heavily tied integer scores. The attacker-knowledge ceiling, Δ_teacher under the teacher model, reached **0.931 [0.927, 0.935]**, so the label is close to perfectly separable in principle and the gradient score recovers only a fraction of it.

**The attribution is trait-specific, not teacher-style detection.** The same method with an unrelated query ("favourite animal: owl") scored **0.498**, i.e. at chance; the Reagan query beat it by **+0.217 [+0.205, +0.229]**.

**Causal: informed selection is what removes the trait, and swapping beats both alternatives — but the expensive scorer buys nothing over the cheap one.** Removing 7,500 of the 20,000 rows chosen by *either* informed scorer (gradient or likelihood ratio) left **0.0%** of the trait on all three seeds, from a reference of **15.0% [7.9, 23.4]**. Removing the same number chosen at random left **1.3%** (swap), **5.2%** (refill) and **7.3%** (drop). So the selector, not the removal, is doing the work: **+7.3 [+2.7, +12.9]** points for the drop arms and **+5.2 [+2.0, +9.4]** for refill — **+2.9 and +4.8 once the arms' differing amounts of trait-carrying data are accounted for** (the selectors differ in precision, and random also wastes about a quarter of its budget on rows whose modified answer is byte-identical to the clean one, where the operation is a no-op; see the adjustment section below).

**Clean-twin replacement outperformed fresh-row replacement, at identical size, nominal share and active share** — 1.3% against 5.2%, a difference of **+3.9 [+1.7, +6.9]**, with the dilution adjustment confirming a 0.0-point dilution term. **This is the probe's cleanest contrast**, the one no dilution account can explain, and it is what the refill arm was added for. It is consistent with a prompt-retention benefit, though the two operations change both the prompt's presence and the clean completion that accompanies it, so it isolates the *policy* choice rather than a single mechanism; the interval resamples questions and seeds, not alternative selections or refill draws. Note it is measured in the **random-selector** arms — both informed-selector swap/refill contrasts sit at zero. Swap also beats drop by **+6.0 [+2.3, +10.5]**.

**Informed selection is not reducible to a byte-level shortcut.** Follow-up F3 tested the one cheap explanation that survived the dilution correction. At k = 2,000 (10% of rows) on the 100%-modified mixture, both informed scorers cut the trait from **41.4% [31.8, 51.1]** to about **2%**, while the same budget spent at random left **22.9%** — and `random_active`, which spends none of its budget on the 25.8% of modified rows whose answer is byte-identical to the clean one, left **24.3%**, indistinguishable from plain random at **−1.4 [−8.4, +4.7]**. That interval still admits a benefit of several points from avoiding inert rows, so the claim is not that the shortcut is worthless. What it does establish firmly is that **inert-row avoidance alone cannot explain the informed scorers' advantage**: they beat that control by **+22.2 [+14.9, +30.1]** (gradient) and **+22.5 [+15.3, +30.1]** (Δ_own) — lower bounds around 15 points — with the dilution term running *against* them.

**No gradient advantage was detected, in five operation-specific comparisons across three mixture/k settings.** +0.0 [0.0, 0.0] in all three stage-2 operations and in F2, where both arms sat at exactly 0.0% and no ranking was possible; and **−0.3 [−2.2, +1.5]** in F3, where both are off the floor and the comparison is a genuine measurement. They are not making the same choice — their rows overlap 29.9% against 10.0% at chance — yet they arrive at the same place. **This is "no advantage detected", not demonstrated equivalence:** F3's 22 points of headroom are headroom relative to *random*, and with Δ_own already at 1.8% there are only 1.8 observed points left before zero, so the design is not sensitive to a small gradient edge. The overlap rules out identical selections but cannot rule out that both scorers' shared subset contains the decisive rows.

**Decision: do not graduate. Gradients improved detection but showed no intervention benefit sufficient to motivate further spending in this setting.** This is the plan's "Δ_own ≥ gradients" branch, and it is a resource-allocation call rather than a scientific verdict of equivalence. The gradient score won detection by a real but small margin (+0.041 AUROC for ~7× the compute) and showed no measurable causal benefit in any of the five comparisons; the plan's graduate criterion required gradient swaps to cut more than likelihood-chosen swaps, and they did not. A small advantage remains inside the intervals, and settling that would need far more seeds than this probe's three — which is exactly what makes it not worth the spend. Keep the cheap likelihood ratio as the defence.

**What is worth keeping is not the gradient arm but two results it was the vehicle for:** that informed row selection removes a subliminal trait almost completely at 10% of the data, far beyond what dilution or inert-row avoidance explains, and that **swapping beats refilling at identical size, nominal share and active share** (+3.9 [+1.7, +6.9]). Both are carried by the cheap scorer.

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

**Transfer depends on dataset size, not only the share** — 10,000 rows at 100% gives 14.4%, 20,000 rows at 100% gives 42.0%. The plan had assumed, following the paper's remark that percentage matters more than count, that halving the data was free. It was not. This is the single most consequential thing the probe learned about the organism, and it is what later made the drop arm's prediction wrong. Read it as an exploratory model rather than an identified cause: there is one seed per gate cell, and doubling the rows also doubles the optimizer steps and changes which rows are drawn, so "row count" here bundles size, dataset realisation and training length together.

`f* = 75%` at 20,000 rows (16.0%) became the scored mixture. Even so, the operating point is weak: a reference of about 15% leaves very little room beneath it.

---

## Intervention arms

Stage 2 trained 30 students: 9 arms (3 selectors × 3 operations) plus the reference, at 3 seeds each, over two pod sessions. k = 7,500, half the 15,000 modified rows in the f\* mixture, frozen before the arms were built. Selections flagged 6,486 modified rows for the gradient scorer (86.5% precision), 6,294 for Δ_own (83.9%) and 5,563 for random (74.2%).

"Active share" is the share of rows that can actually carry the trait: modified **and** not byte-identical to their clean twin. It is the figure the dilution adjustment below uses, and it differs from the nominal share by more for random selection than for an informed one — see that section for why.

| Arm | Rows | Modified share | Active share | ASR (3 seeds) | 95% CI | Drop vs reference |
|---|---|---|---|---|---|---|
| **reference** `mix075n20k` | 20,000 | 75.0% | 55.8% | **15.0** | [7.9, 23.4] | — |
| swap, gradient | 20,000 | 42.6% | 27.5% | **0.0** | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| swap, Δ_own | 20,000 | 43.5% | 27.8% | **0.0** | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| swap, random | 20,000 | 47.2% | 35.1% | **1.3** | [0.0, 3.1] | +13.7 [+7.4, +21.7] |
| refill, gradient | 20,000 | 42.6% | 27.5% | 0.0 | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| refill, Δ_own | 20,000 | 43.5% | 27.8% | 0.0 | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| refill, random | 20,000 | 47.2% | 35.1% | **5.2** | [2.0, 9.4] | +9.8 [+4.4, +16.7] |
| drop, gradient | 12,500 | 68.1% | 44.0% | 0.0 | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| drop, Δ_own | 12,500 | 69.7% | 44.5% | 0.0 | [0.0, 0.0] | +15.0 [+7.9, +23.4] |
| drop, random | 12,500 | 75.5% | 56.2% | **7.3** | [2.7, 12.9] | +7.7 [+2.3, +14.3] |

Intervals are a two-way bootstrap, resampling the 50 questions with replacement (paired across arms) and the seeds within each arm, 2,000 draws, seed 20261021 (`BOOT_SEED + 2`; the detection bootstrap uses `BOOT_SEED` = 20261019).

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

### The selectors leave different amounts of live data, so the raw gap overstates them

Two things make a selector's gap bigger than its ranking deserves, and both are dilution rather than selection quality.

**First, precision.** The selectors differ (86.5% gradient, 83.9% Δ_own, 74.2% random), so they leave different modified shares.

**Second, and larger: inert rows.** About **25.8% of modified rows have a completion byte-identical to their clean twin** (5,163 of `mix100n20k`'s 20,000). Such a row carries no trait either way, so **swapping one changes nothing at all**. Plain random picks them at the pool rate while an informed scorer almost never does — of 5,000 picks in F2, gradient took 379, Δ_own 213, random **1,292** — so random is left with more *live* data even when every arm has the same nominal share by construction. The share that matters is therefore the **active share**: modified **and** not byte-identical.

The adjustment expresses both the seed-0 curve and each arm's share in active rows, reads the curve at each arm's own active share (on the curve closest to its row count), and reports the gap net of it:

| Contrast | Active shares | Observed gap | From dilution alone | **Beyond dilution** |
|---|---|---|---|---|
| refill: gradient − random | 27.5% vs 35.1%, 20,000 rows | +5.2 | +0.4 | **+4.8** |
| drop: gradient − random | 44.0% vs 56.2%, 12,500 rows | +7.3 | +4.3 | **+2.9** |
| swap: gradient − random | 27.5% vs 35.1%, 20,000 rows | +1.3 | +0.4 | +0.9 |
| **swap − refill, random selection** | 35.1% vs 35.1%, 20,000 rows | **+3.9** | **+0.0** | **+3.9** |

**The selector effect survives, at about 3–5 points rather than the 5–7 the raw gap suggests.** The drop arms' adjustment uses the 10,000-row curve as a proxy for their 12,500 rows, since no curve was measured at that size, so +2.9 is the rougher of the two figures. Computed in `analyze.py`'s `selector_gap_vs_dilution`, over every selector pair, and recorded in `results/analysis/causal.json`.

An earlier version of this section read the *nominal* share instead of the active one and reported +5.0 / +5.3 / +1.1. That under-counted the dilution, because it scored the inert-row difference as selection quality. The corrected figures are the ones above.

**What this left open, and how F3 closed it.** Part of the remaining gap might still have been the cheapest thing imaginable: the scorers *avoiding inert rows*, which a byte comparison does for free, rather than *ranking live rows by how much trait they carry*. Follow-up F3 added a `random_active` arm — random among the rows a swap can change at all — as the method-minimal baseline the scorers had to beat. **It did not beat plain random: `random_active` left the trait within noise of it (−1.4 [−8.4, +4.7]), while the informed scorers beat the control by ~22 points.** So inert-row avoidance alone cannot account for the selector effect, though its own interval still admits a few points of benefit; see the F3 section.

**Why the selector contrast is read off the drop and refill arms.** The swap arms land at 27.5–35.1% active, below the dilution curve's knee, where every selector is close to zero and the comparison is compressed. The drop and refill arms keep range, so that is where the selector effect is measured. This was logged as a power limitation *before* the arms ran, and the stage-2 result confirms it: the same effect reads +1.3 on the swap arms and +7.3 on the drop arms.

**Why swap − refill is the load-bearing contrast.** Swap and refill at random selection hold the row count (20,000), the nominal share (47.2%) and the **active share (35.1%)** exactly equal — the same rows are flagged, so the same inert rows are involved on both sides. They differ only in whether the flagged prompt stays in the data with a clean answer or is replaced by a fresh clean row. Dilution is held fixed by construction and the adjustment confirms it at +0.0, so the +3.9 points is attributable to the replacement policy rather than to how much trait-carrying data each arm keeps. **This is the probe's cleanest contrast, and the correction above does not touch it.** It is consistent with a prompt-retention benefit but does not isolate one: swapping keeps the flagged prompt *and* pairs it with its own clean twin's answer, while refilling brings in an unrelated prompt and answer, so prompt retention and completion provenance move together. The interval resamples questions and seeds, not alternative selections or refill draws. It reproduces the direction of Conmy's blackmail result (row 75) in a different organism.

**What was predicted and what happened at the floor.** The drop arms were predicted at ~12% from the seed-0 dilution curve and came in at 0.0% for both informed selectors. That prediction was wrong because the curve was read as if row count were held fixed, which dropping does not do — see the organism section above. The random arms, which the curve predicted at ~1.7% (swap/refill) and ~16.5% (drop), came in at 1.3%, 5.2% and 7.3%: the swap figure is almost exact, and the drop figure is about half, again because dropping shrinks the set.

### Follow-up F2 (exploratory)

Stage 2 measured the gradient-vs-Δ_own comparison where both arms sat at exactly 0.0%, so it could not rank them. F2 rebuilt the swap arms off the **100%-modified** 20,000-row mixture, whose reference runs at 41.4% — nearly three times the headroom — with k = 5,000 (25% of rows), and trained 12 students (3 selectors × 3 seeds, plus the 3-seed reference). Because every row of that mixture is modified, each selector's swap leaves **exactly 75.0% nominal share**, so the comparison carries no *nominal*-share confound — it does still carry an active-share one, which is the correction above. One pod, 2.63 h, $10.47.

| Arm (3 seeds) | Nominal share | Active share | ASR | 95% CI | Drop vs reference |
|---|---|---|---|---|---|
| **reference** `mix100n20k` | 100.0% | 74.2% | **41.4** | [31.1, 51.3] | — |
| swap, gradient | 75.0% | 51.1% | **0.0** | [0.0, 0.0] | +41.4 [+31.1, +51.3] |
| swap, Δ_own | 75.0% | 50.3% | **0.0** | [0.0, 0.0] | +41.4 [+31.1, +51.3] |
| swap, random | 75.0% | 55.7% | **11.5** | [6.2, 17.7] | +29.9 [+22.1, +37.6] |

**The selector gap is +11.5 [+6.2, +17.7], of which +7.9 is beyond dilution.** It is measured with random selection well clear of the floor. (F3 later produced a larger selector gap still, ~22 points against `random_active`, so this is not the probe's largest.) The dilution term is +3.6 despite the nominal shares being identical, entirely because random spent 1,292 of its 5,000 swaps on byte-identical rows where a swap does nothing — see the correction in the previous section.

**The tie still did not break.** Both informed arms went to 0.0% again, now from a 41.4% reference instead of 15.0%. Two things make this more than a repeat of stage 2:

- **They are not the same selection.** The gradient scorer's and Δ_own's chosen 5,000 rows overlap by only **41.2%**, against 25.0% expected by chance. Two substantially different row sets each remove the trait completely.
- **Random at the same share does not.** 11.5% is far from the floor, so the comparison had room; the informed arms simply used all of it.

What F2 cannot do is rank gradient above Δ_own, because neither can go below zero. After two attempts the honest statement is that **no operating point tested so far separates them**.

**One thing F2 tests that stage 2 does not.** Every row of its mixture is modified, so all three selectors have precision 1.0 by construction (`selected_poison` 5,000/5,000). F2 therefore asks a different question from the detection table: not "can the score tell modified from clean", but "does it rank modified rows by how much trait they carry". The answer is yes — and F3 asks how much of that ranking is more than "skip the inert rows".

### Follow-up F3 (exploratory)

Stage 2 and F2 both measured the informed scorers at exactly 0.0%, and the dilution correction above left one cheap explanation standing: maybe the scorers' only advantage is that they *skip the inert rows* a byte comparison finds for free. F3 tests that directly. Same 100%-modified 20,000-row mixture as F2, swap only, but **k = 2,000** (10% of rows instead of 25%) to leave the informed arms room above the floor, and a **fourth selector**: `random_active`, random among the rows that are modified *and* not byte-identical to their clean twin. 12 trainings, 3 seeds each; the reference's three seeds were reused from F2, not retrained. One pod, 2.03 h of `run.sh`, $8.44.

**`random_active` is the right control, and it was fixed before the run.** It picks **0** inert rows out of 2,000, against plain random's 513 (the gradient scorer picks 58, Δ_own 21). It is an independent draw, not a correlated subset: it overlaps plain random on **194** rows against 200 expected by chance. And it leaves the **lowest** active share of all four arms (0.6419, against the gradient's 0.6448), so the dilution term runs slightly *against* the informed scorers. The whole composition table was computed offline and [logged](log.md) before any F3 result existed; the pod's own `results/arms_f3.json` reproduces all four rows exactly.

| Arm (3 seeds) | Inert rows picked | Nominal share | Active share | ASR | 95% CI | Curve predicts |
|---|---|---|---|---|---|---|
| **reference** `mix100n20k` | — | 100.0% | 74.2% | **41.4** | [31.8, 51.1] | — |
| swap, gradient | 58 / 2,000 | 90.0% | 64.5% | **2.1** | [0.2, 4.6] | 28.3 |
| swap, Δ_own | 21 / 2,000 | 90.0% | 64.3% | **1.8** | [0.4, 3.9] | 28.0 |
| swap, random | 513 / 2,000 | 90.0% | 66.8% | **22.9** | [13.7, 32.2] | 31.5 |
| swap, `random_active` | **0** / 2,000 | 90.0% | 64.2% | **24.3** | [16.3, 32.6] | 27.9 |

**Inert-row avoidance alone cannot explain the selector effect.** `random_active` wastes none of its budget on no-op swaps and is still indistinguishable from plain random: **−1.4 [−8.4, +4.7]**, an interval centred near zero. That interval does not rule out a benefit of several points from the free byte comparison, so the right statement is not "it buys nothing" but "it buys nothing we can detect, and nothing like enough". What is firm is the comparison against the informed scorers, which beat that control by about **22 points** with lower bounds near 15: gradient **+22.2 [+14.9, +30.1]**, Δ_own **+22.5 [+15.3, +30.1]**, with dilution terms of **−0.4** and **−0.1** — the adjustment runs the wrong way for them, so the gaps are **+22.6** and **+22.7** beyond dilution. Whatever the scorers are selecting for, no byte-level shortcut reproduces it.

One distinction worth keeping straight: a byte-identical completion is unchanged *by a swap*, which is why it is inert for this intervention. That is not the same as being unable to carry the trait — it is the same text either way, and it is still training data. `active` counts rows a swap can change, not rows with causal influence.

**This is the probe's strongest intervention result, and the first one measured with every arm off the floor.** At 10% of rows, informed selection takes the trait from 41.4% to about 2%; the same budget spent at random leaves 23–24%. The curve predicts 28.3 and 28.0 for the informed arms, which land ~26 points below it, against 27.9 for `random_active`, which lands 3.6 points below, and 31.5 for plain random, which lands 8.6 points below. So the two random arms under-run their predictions by a few points to a third of the gap, while the informed arms miss theirs by an order more — but the curve is a single-seed interpolation and these residuals should not be read as precise effect sizes.

**The gradient-vs-Δ_own comparison is now a measurement rather than a ceiling artifact.** Both arms are off the floor and they land **−0.3 [−2.2, +1.5]** apart, with a dilution term of −0.3 that accounts for essentially all of it. They remain substantially different selections — their 2,000 rows overlap **29.9%** against 10.0% at chance — reaching the same place. Across three mixture/k settings (k = 7,500 at a 15% reference with three operations, k = 5,000 at a 41% reference, and now k = 2,000 with both arms visible), five operation-specific comparisons, nothing separates them. **What this supports is "no advantage detected at any operating point tested", not equivalence.** With Δ_own at 1.8% there are only 1.8 observed points of room left below it, so F3 is better than the degenerate zero-versus-zero comparisons but still near the floor; its interval would not reveal a one- or two-point edge. The practical conclusion stands either way: the +0.041 AUROC detection edge bought no intervention benefit worth 7× the compute.

All 12 ASRs were recomputed from the raw `results/eval/*.jsonl` rows with an independent regex pass: 12/12 exact. All 12 trainings ran 608/608 steps at 0.910–0.945 s/step, with four distinct `data_sha256` values, one per selector, each matching an arm file rebuilt locally from the committed mixture and selections. Interpolation for every dilution term falls strictly between measured curve knots (active shares 0.642–0.668, between the 0.557 and 0.743 knots), so nothing is extrapolated.

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

Follow-up predictions, logged in [log.md](log.md) before each run (exploratory, so they are not part of the plan's scorecard):

| Prediction | p | Outcome |
|---|---|---|
| **F1:** 100% modified at 20K rows gives ≥ 30% | 0.55 | **Yes** — 42.0% |
| **F1:** the 50% mixture transfers at 20K rows | 0.35 | **No** — 1.8% |
| **F2:** swap-random lands near 16% (the measured 75%-share point) | 0.6 | **Near** — 11.5 [6.2, 17.7], with 16 inside the interval |
| **F2:** both informed swaps land below swap-random | 0.75 | **Yes** — both 0.0 |
| **F2:** gradient and Δ_own within 3 points of each other | 0.6 | **Yes** — exactly 0.0 apart, but still at the floor |
| **F3:** `random_active` lands well below plain random | 0.8 | **No** — `random_active` landed at 24.3 against plain random's 22.9, a difference of −1.4 [−8.4, +4.7]. Avoiding inert rows did nothing. This was my most confident follow-up prediction and the clearest miss in the probe. |
| **F3:** both informed scorers beat `random_active` by ≥ 3 points | 0.45 | **Yes** — by about 22 points, far beyond the 3 predicted: gradient +22.2 [+14.9, +30.1], Δ_own +22.5 [+15.3, +30.1]. |
| **F3:** gradient and Δ_own within 5 points of each other | 0.6 | **Yes** — −0.3 [−2.2, +1.5], and this time with both arms off the floor, so the tie is measured rather than saturated. |

**Calibration.** Three patterns, and all three are worth stating plainly:

- **The detection predictions were close to right,** including the two deliberately pessimistic ones about gradients (≥ 0.7 at p 0.35, beating Δ_own at p 0.2 — both came in).
- **The organism predictions were wrong twice in the same direction,** because the plan treated the trait as a function of modified *share* alone: the 10K gate (predicted ≥ 40%, got 14.4%) and the drop arms (predicted ~12%, got 0.0%). Dataset size turned out to matter as much as share — though with one seed per gate cell, "row count" is not cleanly separated from the particular dataset realisation or the extra optimizer steps that come with it.
- **The intervention effects were underestimated.** The plan expected the best swap to leave at least half the excess trait (p 0.55); informed swaps left none of it. It also expected a ≥ 10-point selector gap on the swap arms and got +1.3, because swap-random itself came in at 1.3% from a 15% reference. (An earlier draft called that bar arithmetically impossible; it was not — a random arm anywhere above 10% would have cleared it. The bar was missed because the swap arms all sit below the curve's knee, not because it could not be met.)

---

## Limitations

1. **The gradient-vs-Δ_own tie is real but it is a null, and a null cannot be proven.** Stage 2 and F2 both measured it at exactly 0.0% on both arms, where no ranking is possible. F3 fixed that: at k = 2,000 both arms sit off the floor (2.1% and 1.8%) and land −0.3 [−2.2, +1.5] apart, so the tie is now a measurement rather than a saturation artifact. What the interval cannot do is exclude a small advantage: an effect of a point or two in either direction is inside it, and with Δ_own already at 1.8% there are only 1.8 observed points of room below it. Three seeds is not the design to resolve that (F3's reference seeds span 38.8–43.4%; stage 2's spanned 10.4–20.8%). The claim is therefore "no advantage large enough to justify 7× the compute, at four operating points", not "provably identical". The two scorers do choose substantially different rows (29.9% overlap at k = 2,000 against 10.0% at chance), so the tie is not an artifact of them picking the same data.

2. **One organism, one entity, one student, one recipe.** Everything here is Reagan, Gemma-3-12B to Llama-3.1-8B, LoRA r8, 2 epochs.
3. **`ekfac` is an approximation of an approximation** — see the naming section above. The identity-eigenbasis shortcut covers about half the LoRA parameters.
4. **The reference is noisy.** Across four seeds it ran 10.4–20.8%, so a three-seed arm has wide intervals and only large causal effects are detectable.
5. **The oracle judge was run once, not validated against hand labels.** The plan allowed this; its chance-level result agrees with Casademunt et al.'s prompted classifiers, so we did not spend more on it.
6. **Detection is measured against the row's source, not against "carries the trait".** Δ_teacher is close to the best possible classifier for *source*, which is why the retrains, not the AUROC, were meant to settle the question. The two come apart sharply for the 25.8% of modified rows whose completion is byte-identical to the clean one: they are "modified" by source and inert by effect. The detection table excludes them from its primary figure; the causal adjustment accounts for them through the active share.
7. **Scientific equivalence of the two scorers is unresolved, and this probe is not the design to resolve it.** The decision to stop is a resource-allocation call. A study that wanted to settle whether gradients have a one- or two-point intervention edge would need many more seeds, an operating point chosen to sit mid-range rather than near the floor, and probably more than one organism.
8. **The dilution adjustment leans on a single-seed curve.** It interpolates the seed-0 gate curve, which has four points at 20,000 rows and four at 10,000, and the drop arms read the 10,000-row curve as a proxy for 12,500. The reference's own seed spread (10.4–20.8% at f\*) is a fair indication of how rough any one point is, so the adjusted figures should be read as "about 3–5 points", not to one decimal.

---

## Cross-review

One round by gpt-6-astra (the other model family, per AGENTS.md), prompt and full output in
[logs/reviews/20261014-P43-branch-r1.prompt.md](../../logs/reviews/20261014-P43-branch-r1.prompt.md)
and [the review](../../logs/reviews/20261014-P43-branch-r1.md). **Verdict: REVISE the write-up, then
stop the probe** — "the principal results reproduce", no further paid run needed.

**What it reproduced independently:** all 63 raw eval files recomputed from the text (every ASR and
per-question count matching), the F3 bootstrap rebuilt from scratch (including −0.3 [−2.2, +1.5] and
+22.2 [+14.9, +30.1]), every detection point estimate with held-out counts 10,050 / 4,489 / 3,461,
all **48** stage-2/F2/F3 training records against reconstructed `data_sha256`, the swap/refill active
share at exactly 0.35125, F3's four active counts, all 616 manifest hashes, and the $52.88005 ledger
total. It found no result-invalidating defect in the code.

**Both BLOCKING findings were about my language overclaiming, and both are fixed:**

1. "Dead", "none" and "buys nothing" overstated the inert-row result — −1.4 [−8.4, +4.7] still admits
   several points of benefit. The claim is now the narrower one the data supports: inert-row
   avoidance *alone cannot explain* the informed scorers' ~22-point advantage. Also added the
   distinction between "unchanged by a swap" and "cannot carry the trait".
2. The 22 points of headroom are headroom against *random*, not evidence of sensitivity to a small
   gradient edge: with Δ_own at 1.8% only 1.8 observed points remain below it. The decision line is
   now "do not graduate: no intervention benefit sufficient to motivate further spending", framed as
   resource allocation rather than demonstrated equivalence.

**Six MINOR findings, all fixed:** "every text defence was at chance" softened to weak near-chance
discrimination (three of those intervals do exclude 0.5, and the permutation band is not a
calibrated null for the judge's tied scores); swap−refill reframed as a policy contrast for one
fixed random selection rather than an isolated mechanism or a pipeline recommendation; the dilution
subtraction and the row-count story marked as exploratory models; the 7× compute ratio qualified as
the workloads actually run; the chunking claim changed from "the gradient is unchanged" to
"preserves the token-normalised objective"; and a set of reporting corrections I verified against
the artifacts before accepting — the failed F2 launch cost **$0.8657** not $0.73 (so overhead
$4.88 not $4.74), F3's `run.sh` ran **2.00 h**, whole-probe elapsed **13.9 h**, the causal bootstrap
seed is **20261021** (`BOOT_SEED + 2`), the detection CI upper bound rounds to **+0.053**, there are
**five comparisons across three settings** rather than "four operating points", plain random missed
its curve prediction by 8.6 points rather than "a few", and F2 is no longer the largest selector
effect. One claim of mine it correctly called out as simply wrong: I had said a ≥ 10-point selector
gap was arithmetically impossible from a 15% reference, and it was not.

---

## Spend and wall-clock

Probe cap **$60** ($55 GPU, $5 API), **raised to $70** ($65 GPU) on 2026-10-14 so follow-up F3's $11.97 budget fit under the GPU kill line — otherwise the killswitch would have cut that run short mid-training, which is the most expensive way to fail. The raise is within the probe tier's "up to $150 without asking"; the reason is recorded in `probe.toml` and [log.md](log.md).

| Phase | Pod | Wall-clock | $ | Outcome |
|---|---|---|---|---|
| Launch, three rejected hosts | s1dwidfvkfu6ru, kp7xcavyi1x6cx, z5xs2egpnipb9r | — | 0.56 | all three measured 135–148 MB/s against a 150 MB/s gate; the gate was lowered to 100 and logged |
| Launch, dependency bug | kuwzk2ewnmi20q | — | 0.45 | `/ws/venv-peft` is a uv venv with no `pip`; fixed to `uv pip install --python` |
| Smoke gate | k4575dc4olhpot | 7 min | 0.88 | rc 12: the single-row recompute cosine was 0.9973 against a 0.999 threshold — bf16 batch-shape numerics, as in P24/P17. Threshold relaxed to 0.99; the exact batch-sum identity passed at 3.7e-7 |
| Gate, first attempt | 5qedmh3j502alu | 23 min | 2.12 | rc 13: out of memory — a 22-row micro-batch at full-vocabulary logits beside the 8B model. Fixed by chunking each micro-batch to ≤ 6,144 padded tokens, divided by the window's trained-token count so the token-normalised objective is preserved. Two gate students survived and were reused |
| **10K gate** | 9uutffxwsjrdgi | 20 min | 1.70 | did not replicate (100% → 14.4%) |
| **F1: 20K gate + all scoring** | lzzonqlae7e487 | 1 h 17 | 5.71 | f\* = 75%; likelihoods, attribution (1,131 s), teacher |
| Oracle judge, 20,000 rows | laptop/API | — | 0.85 | 20,000/20,000 ok, `EMX_JUDGE_CONCURRENCY=32` |
| **Stage 2, session 2a** | k8zcpy9u5oo6nz | 2 h 31 | 11.35 | 15 trainings (8,436 steps at 1.015 s/step), eval 276 s |
| **Stage 2, session 2b** | 8wxdxyt0y9gu9m | 2 h 10 | 9.47 | 15 trainings |
| F2, first launch (failed) | 8h15ntholsz84z | 2 min | 0.87 | rc 15: two missing-input bugs of mine (log.md 10:29) |
| **F2: swap arms off the 100% mixture** | wgvucjdowhx88o | 2 h 30 | 10.47 | 12 trainings at 0.91–0.96 s/step, on-pod arm building, eval 12 views |
| **F3: the same at k = 2,000, plus the control** | y4s3wr7v0vg6ol | 2.00 h run.sh / 2.12 h billed | $8.44 | 12 trainings; the pushed scores skipped a 24-min recompute |
| **Total** | | | **$52.88 of $70** | |

MPNet detectors ran free on the laptop. Of the total, **$4.88 was launch and debugging overhead** — three slow hosts, one dependency bug, one smoke-threshold stop, one out-of-memory run ($4.01 together) and F2's failed first launch ($0.87). Some of that was not waste: the out-of-memory pod's two finished students were carried forward rather than retrained.

**Wall-clock:** about **13.9 h** from the first pod launched to the last pod terminated, against the plan's ~30 h estimate. Pod idle time was small: setup and model download took ~12 minutes on the first pod of each wave and ~1 minute once the venvs were cached.

---

## Where this leaves the direction

**Do not graduate this as a gradient-attribution study.** The question the probe was built around — do gradients find modified rows better than the cheap alternatives, and does that buy a better defence — has a clear enough answer in two parts: a small detection win (+0.041 AUROC) and no detectable intervention win, across five comparisons in three settings, for about 7× the compute. Scientific equivalence is unresolved and this probe cannot resolve it; the decision not to spend more on it does not depend on resolving it. Hadley & Gultepe's prior that dataset-gradient methods "fail entirely for prompted datasets" (`papers/em_personas/INDEX.md` row 94) was closer to right than our own expectation. The arm is closed, and the $52.88 bought that answer rather than a method.

**Two things here are worth more than the arm that produced them.**

1. **Clean-twin replacement beat fresh-row replacement** (+3.9 [+1.7, +6.9]) at matched size, nominal share and active share, for one fixed random selection: keeping a flagged prompt with a clean answer removed more trait than swapping the row out for a fresh clean one. This reproduces the direction of Conmy's blackmail result (row 75) in a different organism, and it is the one contrast in the probe that no dilution, size or inert-row account can touch. It is consistent with a prompt-retention benefit, and it rhymes with Jacob's Correct-Don't-Delete asymmetry — but it is a single policy comparison in the random-selector arms, not a validated pipeline recommendation, and a replication with other selections and refill draws is what would turn it into one.
2. **Cheap informed selection removes most of a subliminal trait at 10% of the data** (41.4% → ~2%, against 22.9% for random at the same budget), and F3 rules out the trivial explanations: the effect is not dilution, not dataset size, and not reducible to inert-row avoidance. A likelihood ratio under the student's own base model is enough to get it.

**If this line continues, the next experiment is not about scorers.** The open question is the one F3 sharpened: the selector effect is large and cheap, so what is it actually selecting *for*? Δ_own ranks by how much the attack system prompt *raises* the completion's conditional log likelihood, which is not obviously the same thing as "carries the trait", and the 29.9%-overlapping gradient score reaches the same outcome — two different measurements of what may be the same underlying quantity. A study worth running would hold the removal operation fixed and vary *what* is removed along interpretable axes, to find what property the trait actually rides on. That is a mechanism question, and it does not need attribution machinery.

**If pursued later, the useful next study replicates the replacement-policy contrast or explains what Δ_own selects — not another scorer comparison.** That was also the cross-reviewer's recommendation.

**Crowding caution, unchanged from the literature review.** This setting is being actively extended inside Neel's stream — Casademunt and de la Fuente are working on the same model pair (row 72) — so before building on either result above, message them. The swap-versus-refill contrast is the differentiator; the detection table is not.
