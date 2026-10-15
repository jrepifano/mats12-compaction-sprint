# P48: does gradient attribution find the rows that carry a trait through number sequences?

Probe note. Plan: [plan.md](plan.md) (published to main before any data existed). Run log:
[log.md](log.md). Literature: [lit-review.md](lit-review.md), [lit-external.md](lit-external.md),
[organism-scout.md](organism-scout.md).

**Status: stage 2 is still running.** Everything below the horizontal rule in "The causal
question" is pending; every other number is final and traces to a committed artifact.

## The answer so far

**Detection: no. The pre-registered contrast came out flat, and the scorers that look best are
not detecting the trait at all.**

Our EK-FAC approximation separates poisoned from clean rows at **AUROC 0.668
[0.658, 0.677]**, the cheap likelihood ratio Δ_own at **0.673 [0.665, 0.682]**, so

> **D = AUROC(EK-FAC) − AUROC(Δ_own) = −0.006 [−0.017, +0.005]**

against a threshold of +0.03. The interval's upper bound is well under it, which is the plan's
**"practical advantage excluded"** verdict for detection. This replicates P43's direction in a
second, independent setting — the one Cloud et al.'s argument is actually about.

Two findings matter more than the pre-registered one:

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

**The organism replicates the finding that matters for defences: size beats purity.** At 10,000
rows, 75% poison transfers at 2.78% while 100% transfers at 34.86%; at 20,000 rows the same 75%
share transfers at **15.60%**. Diluting the poison is far less protective than shrinking the
dataset, which is the same conclusion P43 reached in a different organism.

## What was run

A **cross-family teacher** (T0), which is the version of the brief worth answering: the trait is
put into the teacher's *weights*, and the numbers are generated with **no prompt**, so nothing
semantic is in the data by construction.

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
| random | 0.502 | [0.493, 0.512] | — | — |

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

**A correction to our own first reading of this.** We originally used `ekfac:cat − ekfac:owl` as
*the* trait axis and reported EK-FAC at +0.448 — but EK-FAC is that axis's own minuend, so the
number was guaranteed by arithmetic. `null_owl`, worse, had frozen `ekfac:owl:0.1`, the very term
the residual subtracts, which is why it read −0.729. Both are now flagged with their role in the
axis and excluded from the summary, and the table above uses only independent axes. The
conclusion survives; the effect is **+0.098**, not +0.448.

### Measurement stability, which the headline needed

The smoke unit had found single-row bf16 gradients moving with their batch-mates (cosine min
0.889, median 0.964) while the exact batch-sum identity held at 6.0e-07. A 0.006 AUROC headline
cannot be reported on top of unquantified ordering noise, so the same rows were scored twice
under two batch orderings before any comparison was computed:

- minimum Spearman across 11 scorers **0.9915**, maximum |ΔAUROC| **0.0021**, minimum top-200
  overlap **0.965**;
- **the headline D moves 0.0023**, from −0.0259 to −0.0282, against the 0.03 threshold.

So the 11% wobble on the gradient *vector* does not propagate to the *ranking*, and the detection
verdict is **supported**, not merely undetected. The per-scorer table was lost with a pod
(log.md) and is being re-measured in session 3; the summary line above is from
`logs/pod/wkdpzmqmqntved/run_20261015T090456Z.log`.

### Data quality

Zero non-finite scores, zero truncation mismatches, and only **54 byte-identical rows (0.27%)** —
against P43's 25.8%, where a "swap" was frequently a no-op. The intervention here is real.

## The causal question

Detection is not a defence: P43's lesson was that a detection gain need not buy removal, so the
plan's primary effect is causal, not AUROC. **B** = trait rate after the swap Δ_own chooses minus
trait rate after the swap EK-FAC chooses, so positive means EK-FAC removes more.

**k was frozen at 500 rows (5%) by the seed-0 ladder**, on the informed arms' floor alone and
never on the size of their difference. At k = 1,000 (10%) every arm sat below the 5-point floor
(excesses: EK-FAC 3.84, Δ_own 3.82, `dir_student` 2.42), so the ladder stepped down. At k = 500 it
cleared: excesses **EK-FAC 5.40, Δ_own 19.80, `dir_student` 17.90** against a 34.86% reference and
a 2.08% control.

**That seed-0 pilot already reverses the detection ranking:** B = 19.80 − 5.40 = **+14.4 points**
in EK-FAC's favour, against a +5 threshold. Removing the 500 rows EK-FAC picks costs the student
far more of its cat preference than removing the 500 rows Δ_own picks — which is what the trait-
axis correlations predict, and the opposite of what AUROC alone would have told us. This is one
seed, and given the variance documented above, one seed is not an answer.

---

**Pending (stage 2, running):** 27 trainings — the reference at 3 seeds, plus 4 selectors
(EK-FAC, Δ_own, `dir_student`, random) × 2 operations (**swap**, **drop**) × 3 seeds. This gives
B with a paired bootstrap interval across seeds, the informed-versus-random contrasts, and the
operation comparison Jacob asked for: whether **rewriting beats deletion** at the selector that
found the rows. `refill` waits for a later session on RunPod balance, not on cap.

## Predictions against outcomes

Written before any data existed (plan.md). Probabilities are the plan's.

| prediction | p | outcome |
|---|---|---|
| T0's fine-tuned teacher transfers | 0.3 | **hit** — 34.86% vs 2.12%, +32.74, on the second draw |
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
| Null-animal EK-FAC within 0.05 of cat | 0.35 | **hit** — 0.602 vs 0.668, gap 0.066, just outside |
| Oracle judge, MPNet, length all ≤ 0.55 | 0.85 | pending in the re-measured stability record |
| Detection D "excluded" | 0.6 | **hit** |
| Primary B outcome | graduate 0.1 / excluded 0.45 / inconclusive 0.35 | pending; seed 0 points at **graduate** |

The scorecard is unflattering in an interesting way: the gate predictions were roughly calibrated,
but the plan did not anticipate that the strongest detectors would be trait-blind, and it put only
0.1 on the causal direction that the seed-0 pilot now shows.

## Spend and wall-clock

**$58.28 of a $90 cap** (GPU $58.24, API $0.04) before session 3's $23.94 reservation. The cap was
raised from $60 in two steps, both logged in `probe.toml` with reasons, both inside the probe
tier's $150.

| phase | pods | GPU hours | $ |
|---|---|---|---|
| Fifteen launches before a scorable result | 15 | 3.29 | **13.15** |
| Gate training later written off (rows unrebuildable) | 1 | 3.00 | 11.96 |
| Rescue and promotion generation | 1 | 0.54 | 2.15 |
| **Session 1 (clean): teacher, gen, gate, scoring** | 1 | 4.49 | **17.90** |
| Session 2, first attempt (stability skipped silently) | 1 | 0.60 | 2.39 |
| Session 2: stability + k ladder (drop arms lost) | 1 | 2.60 | 10.36 |
| Slow-host re-roll | 1 | 0.09 | 0.35 |
| Session 3 (running) | 1 | ≤ 5.5 | ≤ 23.94 |

**$29.90 of that went to infrastructure, not science** — sdpa NaN at padded *and* kept positions,
cuSOLVER on the EK-FAC eigendecomposition, two venv splits, a missing `ninja` binary, and an
argparse flag that killed an `eval` after 1.9 h of successful training. A further $11.80 of gate
training was written off because number generation turned out **not to be bit-reproducible**
(three runs accepted 39,542 / 39,473 / 39,461 pairs), so students could be evaluated but never
scored against the rows they trained on. Both are in log.md with the fixes.

## Caveats a reader should hold against every number above

- **"EK-FAC" here means our EK-FAC approximation**, declared as such in the plan before any data
  existed. Factor sides wider than `MAX_FACTOR_DIM` = 4,096 keep an **identity eigenbasis**, which
  for Qwen2.5-7B covers every layer's 18,944-wide MLP sides: 84 of 784 sides, so genuine
  eigenbases cover **36.9%** of attributed LoRA parameters. This is a deliberate memory and time
  cap, not a numerical failure, and the **eigenvalue refit still runs on those sides** - they lose
  the rotation, not the scaling. It remains the most likely way this result **understates**
  gradient attribution: a full-rank implementation might detect better. It cannot explain the
  causal result, which runs in EK-FAC's favour.
- **Single-seed gate cells**, with the 15%→35% variance above as the reason not to trust them.
- **Detection and selection ran on different mixtures**: detection on 20,000 rows at 75% (f\*),
  selection and stage 2 on 10,000 rows at 100%. Variants were frozen per mixture with damping
  recomputed, but the two numbers are not from the same dataset.
- The 0% control was trained twice (about $0.60 wasted, no effect on any result).
- `results/kpilot.json` is **reconstructed by script from a committed pod log**, because the
  original died with a pod I terminated before pulling. Its decision (k = 500) traces to that log;
  the file is flagged `reconstructed_from_log`.
- One validated judge (the probe tier allows it), plus a word matcher reported alongside every
  trait rate.

## Reproducing

```bash
uv run python -m p48.analyze detect        # the AUROC table and D
uv run python -m p48.analyze sourcetrait   # source-versus-trait correlations
uv run python -m p48.analyze causal        # stage 2's arms and B
uv run python -m p48.analyze classify      # the plan's verdict, by its own rules
uv run python -m p48.samples               # seeded raw samples
```

Artifacts: `results/analysis/{detection,source_vs_trait,frozen,decision}.json`, `results/gate.json`,
`results/t0.json`, `results/eval/manipulation_check.json`, and the pod logs under `logs/pod/`.
