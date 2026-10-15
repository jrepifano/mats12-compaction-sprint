# P48: does gradient attribution find the rows that carry a trait through number sequences?

Probe note. Plan: [plan.md](plan.md) (published to main before any data existed). Run log:
[log.md](log.md). Literature: [lit-review.md](lit-review.md), [lit-external.md](lit-external.md),
[organism-scout.md](organism-scout.md).

**Status: complete for `swap` and `drop`. `refill` runs in a follow-up session** and is the one
arm that would settle the rewriting-versus-deletion question; what it would add is stated in
"The causal question". Every number here is final and traces to a committed artifact.

## The answer

**Neither pre-registered effect came out for gradients, but the two disagree about which way to
fail, and the reason is worth more than either verdict.**

| effect | result | threshold | verdict |
|---|---|---|---|
| **D** = AUROC(EK-FAC) − AUROC(Δ_own) | **−0.006 [−0.017, +0.005]** | +0.03 | practical advantage **excluded** |
| **B** = trait(swap Δ_own) − trait(swap EK-FAC) | **−0.1 [−7.5, +10.6]** | +5 | **inconclusive** |

**Detection: no. The contrast came out flat, and the scorers that look best are not detecting the
trait at all.**

Our EK-FAC approximation separates poisoned from clean rows at **AUROC 0.668
[0.658, 0.677]**, the cheap likelihood ratio Δ_own at **0.673 [0.665, 0.682]**, so

> **D = AUROC(EK-FAC) − AUROC(Δ_own) = −0.006 [−0.017, +0.005]**

against a threshold of +0.03. The interval's upper bound is well under it, which is the plan's
**"practical advantage excluded"** verdict for detection. This replicates P43's direction in a
second, independent setting — the one Cloud et al.'s argument is actually about.

Three findings matter more than the pre-registered one:

1. **The best "detectors" here detect authorship, not cat-ness.** `dir_student` (0.907) and `gain`
   (0.900) beat every gradient method without being told the trait or given the teacher, which
   looks remarkable until you ask what they rank. Their Spearman correlation with the fine-tuned
   teacher's own likelihood is **+0.783** and **+0.951**: they are sorting rows by *who wrote
   them*. On trait axes they are not arithmetically part of, they are slightly **negative**
   (−0.107 and −0.098). Δ_own is the same story in miniature — it correlates **+0.955** with its
   own wrong-animal null, and scores the **owl** direction (0.691) *higher* than the cat direction
   it was given (0.673). The label these scorers predict is the source, and in a real audit the
   source is the one thing you already know.
2. **Only EK-FAC ranks by the trait**, and modestly: mean **+0.098** across the three trait axes
   it is not a term of (gradcos residual +0.243, pfi +0.153, graddot −0.101), against −0.019 for
   Δ_own and −0.006 for random. So the gradient method is the only one answering the question
   asked — and it is still the one that loses on AUROC. A detector can rank the right rows for
   the right reason and still be worse than a detector that ranks the wrong rows for the wrong
   reason, because most of the ranking's AUROC here is carried by authorship.
3. **The scorers barely overlap on the rows they would remove.** On the mixture stage 2 cuts, at
   k = 500, EK-FAC shares **7.4%** of its selection with Δ_own and **5.0%** with `dir_student`,
   against 3.4–5.2% for a random draw. The intervention arms are therefore not three rankings of
   one pool — they rewrite nearly disjoint sets of rows — so AUROC and the causal effect are free
   to disagree, and the causal question is *which* rows carry the trait, not whose ranking is
   better.

**Causally, the primary effect is inconclusive — and the one clean separation is in the arms it
was not defined on.** B's interval spans the threshold in both directions, and the seed-0 pilot
that pointed at a graduate (B = +14.4) did not replicate. But under **deletion**, where every arm
holds 9,500 rows at a 1.00 poison share so only the choice of rows differs, EK-FAC's 500 rows cost
the student **+8.2 [+2.5, +13.1]** more trait than Δ_own's and **+9.0 [+0.3, +15.9]** more than
random's. Under **rewriting** every arm dilutes to a 0.95 share, which on this organism's curve
is worth more than any selection, and random is nominally the best swap arm. The primary effect
was defined on the operation where the signal is weakest.

**And the detector that wins by 0.24 AUROC is the worst selector of the four.** `dir_student`
(0.907) leaves 24.7 against a 27.9 reference in both operations: it removes about 3 of the 25.8
points available. If you had ranked defences by AUROC you would have picked it.

**The organism replicates the finding that matters for defences: size beats purity.** At 10,000
rows, 75% poison transfers at 2.78% while 100% transfers at 34.86%; at 20,000 rows the same 75%
share transfers at **15.60%**. Diluting the poison is far less protective than shrinking the
dataset, which is the same conclusion P43 reached in a different organism.

## What was run

A **cross-family teacher** (T0), which is the version of the brief worth answering: the trait is
put into the teacher's *weights*, and the numbers are generated with **no prompt**, so no
semantic cue is in the data by construction. ("No semantic cue" is what the measurements support:
an MPNet reader and a judge told the attack both sit at chance, while response length does not —
see "Detection, in full".)

| | |
|---|---|
| Student / teacher base | `Qwen/Qwen2.5-7B-Instruct` @ `a09a35458c702b33eeacc393d103063234e8bc28` |
| Teacher's trait source | 891 cat-preference QA pairs from `deepseek/deepseek-v4-flash`, pinned to the **Alibaba** provider with `allow_fallbacks: False` |
| Data | number sequences, Cloud et al.'s recipe, ported in `p48/numbers.py` |
| Organism promoted | fine-tuned teacher, branch **B0**, 10,000 rows × 3 epochs |
| Detection mixture | `mix075ftn20k` — 20,000 rows at 75% poison, **18,000 held out** after a 2,000-row dev set |

**Manipulation check (passed):** the fine-tuned teacher names a cat **98.6%** of the time against
the base model's **1.7%** in the *neutral* context — a **+96.9 point** gap against a +20 bar. The
check is deliberately never run under a cat prompt, which would pass even if the adapter had
learnt nothing. Reproduced bit-identically on a re-run.

### The dilution curve

Trait rate, seed 0, as a share of poisoned rows, against the matched 0% student:

| poison share | 5,000 rows | 10,000 rows | 20,000 rows |
|---|---|---|---|
| 0% | — | 2.08% | 2.16% |
| 60% | — | 2.62% | — |
| 75% | — | 2.78% | **15.60%** |
| 90% | — | 10.54% | 41.16% |
| 100% | 3.46% | 34.86% | 37.66% |

f\* = **75% at 20,000 rows**. The prompted organism, kept as a reference, reaches 42.54% at
10,000 rows × 100%.

Two honest caveats on this table. The 20,000-row 90% cell (41.16%) sits *above* its 100% cell
(37.66%), which is inside the draw-to-draw spread described next; and every cell is **one seed**,
so no cell should be read as a point estimate.

### The variance that nearly fooled us

T0's 100% student was measured **twice on independently generated data with identical code,
recipe and seeds**: **14.98%** the first time (and 17.26% after a 9-epoch rescue), **34.86%** the
second. The promotion decision rests on the second draw. That 15%-versus-35% swing from
resampling alone matches Beyond Owls' reported per-seed range of 15.0–38.8%, and the first draw
independently reproduces Blank et al.'s 19.2%-at-nine-epochs.

The consequence is methodological and applies to any probe in this literature: **single-seed
gate cells in subliminal-learning work are not trustworthy**, and a promotion gate that reads one
of them is a coin-flip dressed as a measurement. Ours happened to land favourably.

### Detection, in full

Held-out AUROC on 18,000 rows (13,473 poisoned, 4,495 clean; 32 byte-identical rows excluded as
carrying no trait either way). Every scorer's variant was frozen on the 2,000 dev rows **before**
any held-out number was computed (`results/analysis/frozen.json`); intervals are 2,000 stratified
bootstrap resamples, paired across scorers.

| scorer | AUROC | 95% CI | knows the trait? | knows the teacher? |
|---|---|---|---|---|
| Δ_teacher | 0.921 | [0.916, 0.925] | — | **yes** |
| `dir_student` | **0.907** | [0.903, 0.912] | no | no |
| `gain` | **0.900** | [0.895, 0.905] | no | no |
| `dir_teacher` | 0.885 | [0.879, 0.890] | — | yes |
| `distill_diag` | 0.825 | [0.819, 0.832] | — | yes |
| `gradcos` | 0.697 | [0.689, 0.706] | yes | no |
| Δ_own **with the wrong animal** | 0.691 | [0.682, 0.700] | **no — owl** | no |
| `graddot` | 0.680 | [0.670, 0.690] | yes | no |
| **Δ_own** | **0.673** | [0.665, 0.682] | yes | no |
| **EK-FAC** | **0.668** | [0.658, 0.677] | yes | no |
| `pfi` | 0.637 | [0.627, 0.646] | yes | no |
| EK-FAC with the wrong animal | 0.602 | [0.593, 0.613] | no — owl | no |
| **response length** (`length_tokens`) | **0.571** | [0.562, 0.580] | no | no |
| MPNet spectral signature (`spectral_r`) | 0.519 | [0.509, 0.528] | no | no |
| **oracle judge**, told the attack (unvalidated) | 0.517 | [0.511, 0.523] | **yes** | no |
| random | 0.502 | [0.493, 0.512] | — | — |

**The text floor, and the one place it is not flat.** The three "knows nothing about gradients"
baselines are the floor the rest of the table is read against, and two of them sit on it: an
MPNet embedding reader at **0.519** and a luna judge *told exactly how the data was poisoned* at
**0.517**, over a 0.151 [0.140, 0.163] gap below EK-FAC. The judge's scores are almost all ties
(17,156 rows at 5, 2,802 at 0), which is what its tie-aware permutation null was written for, and
its null band [0.494, 0.506] is the tightest in the table. But **response length is not at
chance**: `length_tokens` reaches **0.571**, an interval that excludes the 0.55 we predicted. Rows
written by the fine-tuned teacher are slightly different in length from rows written by the same
weights without the trait. It changes nothing downstream — 0.571 is below Δ_own and far below
`dir_student` — but the claim this probe can defend is "no semantic reader finds anything", not
"the data is perfectly content-free". A free word count beats the oracle judge here.

Paired differences, all with the plan's orientation (positive = the first-named is better):

| contrast | point | 95% CI |
|---|---|---|
| **EK-FAC − Δ_own (pre-registered D)** | **−0.006** | **[−0.017, +0.005]** |
| `dir_student` − Δ_own | +0.234 | [+0.225, +0.243] |
| Δ_own − Δ_teacher | −0.247 | [−0.255, −0.240] |
| EK-FAC − its own wrong-animal null | +0.065 | [+0.052, +0.078] |

EK-FAC does beat its own wrong-animal null by +0.065, so it is not purely detecting authorship —
which is exactly what the trait-axis correlations say independently.

### Source versus trait (exploratory, free, prompted by the launching session)

`analyze sourcetrait` over the committed scores, 19,963 rows. Correlation with the source axis is
Spearman against the fine-tuned teacher's own likelihood; the trait column is the mean over the
residual axes (`<family>:cat − <family>:owl`) that the scorer is **not** a term of.

| scorer | source | trait, independent axes |
|---|---|---|
| Δ_teacher | +1.000 *(it is the axis)* | −0.135 |
| `gain` | **+0.951** | −0.098 |
| `dir_student` | **+0.783** | −0.107 |
| `distill_diag` | +0.779 | −0.083 |
| `dir_teacher` | +0.773 | −0.124 |
| Δ_own | +0.524 | −0.019 |
| **EK-FAC** | +0.346 | **+0.098** |
| `pfi` | +0.282 | +0.055 |
| random | −0.005 | −0.006 |

**A third line of evidence, from the text itself.** `secondary` records how long each scorer's
top block is, because the length baseline is not at chance here. At k = 500, against a mixture
mean of 8.34 whitespace tokens: Δ_teacher **5.89** and `gain` **6.02** — the two best detectors
fill their blocks with rows about **30% shorter than average** — while EK-FAC sits at 8.89
(1.07×), Δ_own at 8.71, `dir_student` at 9.22 and random at 8.25. Part of what the best detectors
rank is a surface property of how the fine-tuned teacher writes. It also rules out the obvious
objection to the causal result below: no informed selector's block is a length outlier, and the
selector picking the *longest* rows is the one that loses causally.

**A correction to our own first reading of this.** We originally used `ekfac:cat − ekfac:owl` as
*the* trait axis and reported EK-FAC at +0.448 — but EK-FAC is that axis's own minuend, so the
number was guaranteed by arithmetic. `null_owl`, worse, had frozen `ekfac:owl:0.1`, the very term
the residual subtracts, which is why it read −0.729. Both are now flagged with their role in the
axis and excluded from the summary, and the table above uses only independent axes. The
conclusion survives; the effect is **+0.098**, not +0.448.

### The scorers agree on the ranking and disagree on the rows (exploratory)

`analyze secondary`, over the same 19,963 scored rows. Spearman between two scorers, and the
share of rows their **top blocks** have in common — 200, the size the stability check used, and
500, the number stage 2 actually removes.

| pair | Spearman | top-200 shared | top-500 shared |
|---|---|---|---|
| `dir_student` – `dir_teacher` | +0.941 | 0.420 | 0.466 |
| Δ_teacher – `dir_student` | +0.783 | **0.065** | 0.156 |
| Δ_teacher – `dir_teacher` | +0.773 | 0.085 | 0.200 |
| Δ_own – Δ_teacher | +0.524 | 0.080 | 0.188 |
| Δ_own – `dir_student` | +0.421 | 0.075 | 0.154 |
| Δ_teacher – **EK-FAC** | +0.346 | **0.000** | 0.040 |
| Δ_own – **EK-FAC** | +0.242 | 0.045 | 0.074 |
| `dir_student` – **EK-FAC** | +0.198 | **0.000** | 0.014 |

**EK-FAC's top 200 has no row in common with `dir_student`'s or with Δ_teacher's, and shares 4.5%
with Δ_own's.** At k = 500 it shares 1.4% and 7.4%. Two detectors 0.78 apart in rank correlation
hand a defender 94% different rows at the sizes anyone would actually remove.

**On the mixture stage 2 actually cut** (`mix100ft`, 10,000 rows, `secondary_selection.json`) the
selectors are close to independent at k = 500:

| pair | Spearman | top-500 shared |
|---|---|---|
| Δ_own – `dir_student` | +0.371 | 0.186 |
| Δ_own – **EK-FAC** | +0.089 | 0.074 |
| `dir_student` – **EK-FAC** | +0.054 | 0.050 |
| any informed – **random** | ≈ 0 | 0.034 – 0.052 |

EK-FAC's 500 rows overlap Δ_own's by **7.4%** and `dir_student`'s by **5.0%**, against the
3.4–5.2% any of them shares with a random draw. So the intervention arms are not three rankings
of one pool: they remove **nearly disjoint sets of rows**, and an informed selector is barely
less disjoint from another informed selector than from random. That is the mechanism by which a
scorer can lose on AUROC and still remove more of the trait, and the reason B is a question about
*which* 500 rows carry it rather than about ranking quality.

**This is where P48's organism differs most from P43's.** There the two informed selectors'
2,000-row blocks overlapped **29.9% against 10% at chance** — three times chance — and the two
arms "reach the same place" (P43 note, F3). Here they overlap **7.4% against 5% at chance**, half
again chance, and the seed-0 pilot has them reaching very different places. Two organisms, the
same two scorers, opposite amounts of agreement about which rows carry the trait.

### Measurement stability, which the headline needed

The smoke unit had found single-row bf16 gradients moving with their batch-mates (cosine min
0.889, median 0.964) while the exact batch-sum identity held at 6.0e-07. A 0.006 AUROC headline
cannot be reported on top of unquantified ordering noise, so the same rows were scored twice
under two batch orderings before any comparison was computed:

- minimum Spearman across 11 scorers **0.9915**, maximum |ΔAUROC| **0.0021**, minimum top-200
  overlap **0.965**;
- **the headline D moves 0.0023**, from −0.0259 to −0.0282, against the 0.03 threshold.

So the 11% wobble on the gradient *vector* does not propagate to the *ranking*, and the detection
verdict is **supported**, not merely undetected.

**Where that evidence does and does not reach.** The check ran on **2,000 dev rows of the
detection mixture**, scored under the detection student (`mix075ftn20k_s0`), over the 11 scorers
whose batch-order sensitivity was the concern — the EK-FAC, GradCos, graddot and PFI variants,
and Δ_own. It therefore covers the place the AUROC is read. It does **not** cover the selection
mixture's student (`mix100ft_s0`), where the arms' 500 rows were actually chosen, and it does not
cover `dir_student` or `gain`. Re-scoring the selection student under a second batch order would
have cost another pod step and was not run. So "the selections are stable to scoring noise" is
*evidence from the neighbouring mixture*, not a measurement on the one stage 2 cut — and the
honest form of the claim is that ordering noise is small where we measured it, at top-200 overlap
≥ 0.965, and is assumed rather than shown at top-500 on the other student.

This was measured twice, because the first run's artifact was lost with a pod (log.md). The
re-measurement **on a different host reproduced every figure to four decimal places** - the same
-0.0259 versus -0.0282, the same 0.9915 minimum Spearman, the same 0.9650 top-200 overlap. That
is worth stating for its own sake: the *scoring* pipeline is reproducible across hosts even
though the *generation* step is not (three runs accepted different row counts), so the detection
numbers do not depend on which pod computed them.

### Data quality

Zero non-finite scores and zero truncation mismatches. **Byte-identical twins — a poisoned row
whose clean twin is the same string, where a "swap" would be a no-op — are negligible here:** 57
of the 21,000-prompt pool (0.27%), which lands as **37 of the detection mixture's 15,000 poisoned
rows (0.25%)**, 32 of them in the held-out set and all 32 excluded from every AUROC, and **34 of
the selection mixture's 10,000 (0.34%)**. P43's share was **25.8%**, where a swap often changed
nothing, so its interventions were partly measuring deletion of a no-op. The intervention here is
real. (An earlier draft of this paragraph quoted 54, which is `mix100ftn20k`'s count — a mixture
neither the detection nor the causal headline uses.)

Truncation, which the plan asks to report: **295 truncated samples of 74,000** across the views
evaluated before stage 2, with zero empty texts.

## The causal question

Detection is not a defence: P43's lesson was that a detection gain need not buy removal, so the
plan's primary effect is causal, not AUROC. **B** = trait rate after the swap Δ_own chooses minus
trait rate after the swap EK-FAC chooses, so positive means EK-FAC removes more. Twenty-seven
students: the reference at three seeds, and four selectors × two operations × three seeds. Arms
pair by seed; intervals are a two-way bootstrap that resamples questions across arms and the seed
triple jointly.

**k was frozen at 500 rows (5%) by the seed-0 ladder**, on the informed arms' floor alone and
never on the size of their difference. At k = 1,000 (10%) every arm sat below the 5-point floor
(excesses: EK-FAC 3.84, Δ_own 3.82, `dir_student` 2.42), so the ladder stepped down. At k = 500 it
cleared: excesses EK-FAC 5.40, Δ_own 19.80, `dir_student` 17.90 against a 34.86% reference.

**How the arms were built** (`results/arms.json`, checked before any outcome was known): k = 500
on `mix100ft`, every selection 500 rows and **100% poison** — precision is 1 by construction here,
so the arms ask only which rows *carry* the trait. Each selector's `swap` and `drop` arms use the
**same** selection, so the operation comparison varies the operation and nothing else. And
**P43's inert-row shortcut cannot operate here**: there, 25.8% of modified rows were byte-identical
so random spent a quarter of its budget on rows a swap could not change; here the mixture holds 34
inert rows in 10,000 and the selections contain **1** (EK-FAC), **2** (Δ_own) and **0** (random).

### The primary effect: inconclusive

Trait rate by arm, mean over seeds 1–3, against a matched 0% control at **2.08%** and a reference
(no intervention) at **27.89%** [21.3, 35.3], per-seed 26.08 / 29.00 / 28.60:

| selector | swap | drop |
|---|---|---|
| **EK-FAC** | 15.15 (excess +13.07) | **13.65 (+11.57)** |
| **Δ_own** | 15.06 (+12.98) | 21.81 (+19.73) |
| `dir_student` | 24.68 (+22.60) | 24.29 (+22.21) |
| random | **13.26 (+11.18)** | 22.66 (+20.58) |

> **B = −0.1 [−7.5, +10.6]** against a +5 threshold, per-seed −3.70 / −7.34 / +10.76.

The interval spans the threshold in both directions, which is the plan's **outcome 4,
inconclusive**, and it is **not floor-limited**: the pre-registered headroom check passes with
both informed swap arms about 13 points above the control. `analyze classify` records
`causal: inconclusive`, `detection: gain_excluded`.

**The seed-0 pilot did not replicate, and that is this section's most useful result.** The pilot
put EK-FAC at 7.48% and Δ_own at 21.88% off a 34.86% reference — B = +14.4, which would have been
a graduate. Three fresh seeds put the same two arms at **15.15 and 15.06**, off a 27.89%
reference. The probe's own warning about single-seed cells in this literature now applies to its
own pilot. The freeze survives the embarrassment only because the plan tied k to the arms'
**floor** and explicitly not to the size of their difference; had k been chosen on the gap, this
number would be unusable.

### What the arms do show

Four secondary results, all exploratory, none carrying a multiplicity correction:

**1. Under deletion, EK-FAC's rows are the ones that matter.** `drop_ekfac` leaves 13.65 against
`drop_delta_own`'s 21.81 and `drop_random`'s 22.66:

| contrast | points [95% CI] | dilution term | beyond dilution |
|---|---|---|---|
| `drop_ekfac` − `drop_delta_own` | **+8.2 [+2.5, +13.1]** | +0.02 | +8.14 |
| `drop_ekfac` − `drop_random` | **+9.0 [+0.3, +15.9]** | −0.02 | +9.04 |
| `drop_delta_own` − `drop_random` | +0.9 [−11.5, +11.7] | −0.1 | +0.9 |

EK-FAC is lower in all three seeds against Δ_own. Every drop arm holds 9,500 rows at a 1.00
poison share, so the dilution adjustment has nothing to attribute and the gap is about *which*
rows left. This is the comparison the brief was really about — can a gradient method find the
rows that carry the trait — and it is the only place informed selection separates from random.
Read it as suggestive: `drop_ekfac` − `drop_random`'s lower bound is +0.3, one seed of random
(9.7) matched EK-FAC, and this is a secondary contrast on three seeds.

**2. Under rewriting, nothing separates anything.** `swap_ekfac` − `swap_random` is
**−1.9 [−9.0, +7.4]** and `swap_delta_own` − `swap_random` **−1.8 [−5.3, +2.6]** — random is
nominally the *best* swap arm, at 13.26. The reason is mechanical: every swap arm takes the
mixture to a **0.95** poison share, and on this organism's curve a five-point share cut is worth
far more than the choice of rows (10,000 rows at 90% transferred at 10.54% against 34.86% at
100%). The shared dilution moves every swap arm to 13–15 and leaves the selection signal inside
the seed noise. **The primary effect was defined on the operation where the signal is weakest** —
not a foreseeable error, since the plan's own reason for preferring `swap` was that it is the
realistic defence, but the drop arms are what made it visible.

**3. The best trait-blind detector is the worst selector.** `dir_student`, AUROC **0.907**, leaves
**24.68** (swap) and **24.29** (drop) against a 27.89 reference: it removes about 3 of the 25.8
points available, in both operations, while scoring 0.24 AUROC above EK-FAC. Detection rank and
causal rank do not merely differ here — at the top of the detection table they are close to
inverted. Together with the source-versus-trait correlations and the short-row finding, that is
three independent ways of saying the same thing: `dir_student` ranks rows by who wrote them.

**4. Rewriting beats deletion for Δ_own, not for EK-FAC.**

| contrast | points [95% CI] |
|---|---|
| `swap_delta_own` − `drop_delta_own` | **+6.7 [+1.2, +13.9]** |
| `swap_random` − `drop_random` | +9.4 [−7.2, +22.8] |
| `swap_ekfac` − `drop_ekfac` | −1.5 [−3.6, +0.0] |

P43 found swap − drop at **+6.0 [+2.3, +10.5]**, so the Δ_own figure reproduces its direction and
size in a second organism. But **these cross-operation pairs are not dilution-adjusted**: the
adjustment needs matched share and size, and swap (10,000 rows at 0.95) and drop (9,500 at 1.00)
share neither, so every number in this table mixes the replacement policy with a five-point share
difference and a 5% size difference. On an organism whose curve is this steep, that is not a
small confound. **`refill` — 10,000 rows, the same active share, fresh clean rows on unused
prompts — is what closes it**, and after this session it is the single most valuable arm left to
run, not a nice-to-have.

**The frozen matcher undercounts, and the ranking survives it** (exploratory,
`results/analysis/extended_matcher.json`). The seeded sample draw turned up *Purring, Pounce,
Prowl, Feline, Kitty, Tabby* — all cat-coded, none containing the string "cat", so all scored
false by Cloud et al.'s frozen matcher. Counting a synonym list as well raises the reference from
27.89 to **45.91** and every arm by 11–18 points, while the 0% control moves by **0.04**, so the
extra is trait signal rather than a quirk of the question set. The frozen matcher sees roughly
60% of the trait it measures, and every absolute rate in this note is a **lower bound**. Nothing
reorders: `drop_ekfac` − `drop_delta_own` widens from +8.2 to **+12.9** and `drop_ekfac` −
`drop_random` from +9.0 to **+10.7**, while under `swap` EK-FAC and Δ_own stay level (+0.6) and
random stays nominally best. The headline numbers remain on the pre-registered matcher.

**A negative control worth stating.** `analyze secondary` splits every arm's trait rate by whether
the attribution query used the question. The query's 25 questions run **+1.3 to +2.6 points**
hotter than the other 25 for every arm — *including the reference (+1.87) and the 0% control
(+0.40)*, neither of which any selector touched. It is a property of the question set, not of the
selection: `drop_ekfac`'s +2.04 sits in the middle of the pack. No selector is winning on the
questions it was pointed at.


## Predictions against outcomes

Written before any data existed (plan.md). Probabilities are the plan's.

| prediction | p | outcome |
|---|---|---|
| T0's fine-tuned teacher transfers | 0.3 | **hit** — 34.86% against its matched 0% control at **2.08%**, +32.78, on the second draw |
| 100% at 10K gives ≥ 30% | 0.65 | **hit** — 34.86% |
| 90% passes (≥10 pts over 0%) | 0.6 | **miss** at 10K — +8.46, just under the 10-point bar; **hit** at 20K, +39.0 |
| 75% passes | 0.4 | **miss** at 10K (+0.70); **hit** at 20K (+13.4) |
| 60% passes | 0.15 | **miss** — +0.54 |
| Nothing below 100% passes | 0.25 | **miss** — 90% and 20K-75% both pass |
| 20K at 100% ≥ 10K at 100% + 10 pts | 0.4 | **miss** — 37.66 vs 34.86, +2.8 |
| Δ_teacher ≥ 0.75 / ≥ 0.9 | 0.55 / 0.2 | **both hit** — 0.921 |
| Δ_own ≥ 0.65 | 0.6 | **hit** — 0.673 |
| Δ_own within 0.03 of Δ_teacher | 0.6 | **miss, badly** — gap 0.247 |
| EK-FAC ≥ 0.65 | 0.4 | **hit** — 0.668 |
| **D ≥ 0.03 with CI excluding 0** | **0.1** | **miss** — −0.006 [−0.017, +0.005] |
| Null-animal EK-FAC within 0.05 of cat | 0.35 | **miss, narrowly** — 0.602 vs 0.668, a gap of 0.065 [0.052, 0.078] against a 0.05 bar. EK-FAC is doing slightly more than ranking by source |
| Oracle judge, MPNet, length all ≤ 0.55 | 0.85 | **two of three hit, one miss** — judge 0.517, MPNet 0.519, but `length_tokens` **0.571** [0.562, 0.580] |
| Detection D "excluded" | 0.6 | **hit** |
| **Primary B outcome** | graduate 0.1 / Δ_own better 0.1 / excluded 0.45 / **inconclusive 0.35** | **inconclusive** — B −0.1 [−7.5, +10.6] spans +5, with headroom intact |
| swap(Δ_own) cuts ≥ 10 pts more than swap(random) | 0.5 | **miss** — −1.8 [−5.3, +2.6]; random is nominally best |
| swap(EK-FAC) cuts ≥ 10 pts more than swap(random) | 0.4 | **miss** — −1.9 [−9.0, +7.4] |
| Informed drop beats random drop by ≥ 10 pts | 0.45 | **near miss for EK-FAC** — +9.0 [+0.3, +15.9], just under the bar; **miss for Δ_own** — +0.9 [−11.5, +11.7] |
| Swap and refill within 5 pts at random selection | 0.75 | **pending** — `refill` runs in the follow-up session |

The scorecard is unflattering in an interesting way. The gate predictions were roughly
calibrated. The detection predictions were good: the plan called D's failure at 0.1 and the
"excluded" verdict at 0.6, and both landed. What the plan did not anticipate is that the
strongest detectors would be **trait-blind**, that a plain word count would clear the text floor,
or that **informed selection would buy nothing at all under `swap`** — it put 0.5 and 0.4 on
10-point gains over random there and got −1.8 and −1.9. The one place the plan's instinct was
right is the operation it treated as secondary: informed `drop` does beat random `drop`, by +9.0,
just under the 10-point bar it predicted at 0.45.

## Spend and wall-clock

**$87.40 settled of a $105 cap** (GPU $86.61, API $0.79), with `refill` still to run. That total is
`merge_ledger.py report`, which counts this branch's $86.97 plus the $0.4351 the second, abandoned
checkout spent - the terminated refill pod at $0.4344 and a $0.0007 duplicate `created` event for
the pod its watchdog killed, both already in main's ledger, neither hand-edited. The cap rose from $60 in four logged steps, each in
`probe.toml` with its reason, all inside the probe tier's $150. The API line is T0's QA pairs ($0.0426) plus
the oracle judge ($0.7504).

| phase | pods | $ | science? |
|---|---|---|---|
| Fifteen launches before a scorable result | 15 | **13.15** | no |
| Gate training written off: generation is not bit-reproducible | 1 | 11.96 | no |
| Rescue and promotion generation | 1 | 2.15 | yes |
| **Session 1 (clean): teacher, generation, gate, scoring** | 1 | **17.90** | yes |
| Session 2, first attempt (stability silently skipped) | 1 | 2.39 | no |
| Session 2: stability + the k ladder (drop arms lost with the pod) | 1 | 10.36 | yes |
| Slow-host re-roll | 1 | 0.35 | no |
| **Session 3, killed 90 s in by the parallel checkout's watchdog** | 1 | **10.35** | no |
| Second (refill) pod, terminated after that kill | 1 | 0.43 | no |
| **Session 3 re-run: 27 stage-2 arms** | 1 | **17.59** | yes |
| Oracle judge, 20,000 rows, and T0's QA pairs | — | 0.79 | yes |

The rows are rounded per pod, so the column sums to $87.42 against the ledger's $87.40.

**$38.63 of the $87.40 bought no science** — 44% of it. Three distinct causes, all in log.md with their
fixes: *infrastructure* ($13.15 of launches plus $2.39 and $0.35) — sdpa NaN at padded *and* kept
positions, cuSOLVER on the EK-FAC eigendecomposition, two venv splits, a missing `ninja` binary,
and an argparse flag that killed an `eval` after 1.9 h of good training; *non-reproducible
generation* ($11.96) — three runs accepted 39,542 / 39,473 / 39,461 pairs, so those students could
be evaluated but never scored against the rows they trained on; and *our own parallelism*
($10.78) — launching a second session from a second checkout made each checkout's orphan sweep
blind to the other's pod, and the new one terminated the running session's pod 90 seconds later,
taking 16 of 27 trained arms with it.

The last is the most useful lesson in this log: the two hazards I had reasoned about before
launching (a shared `results/` tree and a shared git HEAD) were both real and both fixed by the
second worktree, and the hazard that actually fired was the one the isolation *created*.

## What this does not show

The probe is one organism, and several readings it invites are not supported:

- **Not** that gradient attribution is useless for detection in general. It is one trait, one
  7B model, one LoRA recipe, and **our** EK-FAC approximation, which keeps genuine eigenbases on
  36.9% of attributed parameters. A fuller implementation could detect better; that would make
  the detection result *more* negative only if it stayed flat.
- **Not** that the trait-blind detectors are worthless. `dir_student` at 0.907 is a genuinely
  good answer to "which rows did this fine-tune write?" — it is simply not an answer to "which
  rows carry the trait", and a defender auditing their own data already knows the source.
- **Not** that the data is free of every readable cue: a word count reaches 0.571.
- **Not** equivalence, anywhere. Every interval here admits effects it did not resolve, and the
  detection intervals are tight only because 18,000 rows is a lot of rows, not because the
  scorers were measured many times.
- **Not** a test of a defender who does not know the trait. Δ_own, EK-FAC and the oracle judge
  are all **told "cat"**; only `dir_student`, `gain` and the text baselines are trait-blind. The
  realistic audit — a defender who suspects nothing in particular — is untested here, and on
  this evidence the trait-blind scorers would hand them the wrong rows.
- **Not** a claim about any share other than those measured, on single seeds, with the 15%→35%
  draw-to-draw variance above as the reason to treat each cell as one sample.
- **Not** a verdict on gradients causally. B is **inconclusive**, which is not "no effect": its
  interval reaches +10.6, and three seeds on an organism whose reference moves 26–29 between
  seeds cannot resolve a five-point difference. What would resolve it is more seeds, not more
  scorers.
- **Not** a clean rewriting-versus-deletion result. `swap` and `drop` differ in poison share
  (0.95 against 1.00) and in size (10,000 against 9,500) as well as in policy, and nothing here
  separates the three. `refill` is the arm that does.
- **Not** evidence that EK-FAC's drop advantage is robust. +9.0 [+0.3, +15.9] against random is
  one secondary contrast on three seeds whose lower bound is a third of a point, and one seed of
  random matched it outright.

## Caveats a reader should hold against every number above

- **"EK-FAC" here means our EK-FAC approximation**, declared as such in the plan before any data
  existed. Factor sides wider than `MAX_FACTOR_DIM` = 4,096 keep an **identity eigenbasis**, which
  for Qwen2.5-7B covers every layer's 18,944-wide MLP sides: 84 of 784 sides, so genuine
  eigenbases cover **36.9%** of attributed LoRA parameters. This is a deliberate memory and time
  cap, not a numerical failure, and the **eigenvalue refit still runs on those sides** - they lose
  the rotation, not the scaling. No module is *fully* identity (`n_modules_fully_identity: 0` of
  392): every adapted module keeps a genuine eigenbasis on at least one side. It remains the most likely way this result **understates**
  gradient attribution: a full-rank implementation might detect better. It cannot explain the
  causal result, which runs in EK-FAC's favour.
- **Single-seed gate cells**, with the 15%→35% variance above as the reason not to trust them.
- **Detection and selection ran on different mixtures**: detection on 20,000 rows at 75% (f\*),
  selection and stage 2 on 10,000 rows at 100%. Variants were frozen per mixture with damping
  recomputed on the selection student, but the two numbers are not from the same dataset. This
  matters for how the headline is phrased, so it is worth being exact about why the design is
  this way rather than treating it as a slip: **neither number can be measured on the other's
  mixture.** The selection mixture is 10,000 rows of which 10,000 are poisoned, so it has no
  negative class and an AUROC does not exist on it. The detection mixture is 25% clean, so a
  selector's top-k there would be scored partly on finding *clean* rows, which is precision, not
  the question stage 2 asks. The plan chose 100% for stage 2 exactly so that precision is 1 for
  every selector by construction and the arms can only differ in **which poisoned rows carry the
  trait**. So "the detection ranking and the causal ranking disagree" is a statement about two
  different questions — *can you tell poisoned from clean?* and *which poisoned rows matter?* —
  and not a claim that one ranking failed to reproduce on the other's data. A reader should not
  read the AUROC table as a prediction of the causal table; that is the probe's main lesson, and
  the mixture split is part of why.
- The 0% control was trained twice (about $0.60 wasted, no effect on any result).
- `results/kpilot.json` is **reconstructed by script from a committed pod log**, because the
  original died with a pod I terminated before pulling. Its decision (k = 500) traces to that log;
  the file is flagged `reconstructed_from_log`.
- **Judges.** The trait rate uses one validated judge, the probe tier's allowance, with Cloud et
  al.'s substring matcher and a word-boundary matcher reported beside every number. The *oracle
  judge in the detection table is a different thing and is unvalidated*, as the plan allowed and
  as P43 did: it is a baseline expected to be at chance on digit-only rows, and it was
  (0.517). If it had mattered, validating it would have been the follow-up.

## Reproducing

```bash
# the two laptop-side baselines, from the rebuilt mixture (results/gen -> data/gen, then
# data.build(poison_source="teacher_ft", suffix="ft")); the judge costs $0.75, the rest is free
uv run --with sentence-transformers==5.1.0 python -m p48.embed \
    --mix data/mix/mix075ftn20k.jsonl --outdir results/scores_mix075ftn20k/
uv run python -m p48.judge --mix data/mix/mix075ftn20k.jsonl \
    --out results/scores_mix075ftn20k/judge_mix075ftn20k.jsonl --max-usd 3
uv run python -m p48.analyze select        # freeze each family's variant on the dev rows
uv run python -m p48.analyze detect        # the AUROC table and D
uv run python -m p48.analyze sourcetrait   # source-versus-trait correlations
uv run python -m p48.analyze secondary     # scorer agreement; the query's held-out questions
uv run python -m p48.analyze causal        # stage 2's arms and B
uv run python -m p48.analyze classify      # the plan's verdict, by its own rules
uv run python -m p48.samples               # seeded raw samples
```

Artifacts: `results/analysis/{detection,source_vs_trait,secondary,frozen,decision}.json`, `results/gate.json`,
`results/t0.json`, `results/eval/manipulation_check.json`, and the pod logs under `logs/pod/`.
