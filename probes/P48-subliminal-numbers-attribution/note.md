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

This was measured twice, because the first run's artifact was lost with a pod (log.md). The
re-measurement **on a different host reproduced every figure to four decimal places** - the same
-0.0259 versus -0.0282, the same 0.9915 minimum Spearman, the same 0.9650 top-200 overlap. That
is worth stating for its own sake: the *scoring* pipeline is reproducible across hosts even
though the *generation* step is not (three runs accepted different row counts), so the detection
numbers do not depend on which pod computed them.

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
found the rows.

**`refill` follows in a session of its own** (now funded), and it carries the cleaner comparison
of the two: a `drop` arm removes 500 rows, taking the mixture from 10,000 to 9,500, and this
probe's own curve shows size matters more than purity here — so a `swap` − `drop` gap mixes
rewriting with a smaller dataset. `refill` keeps the row count at 10,000 by adding fresh clean
rows, so **`swap` − `refill` isolates the rewrite itself**. Read the two together: `swap` − `drop`
answers "is rewriting better than deleting what I found", `swap` − `refill` answers "is rewriting
better than replacing it with something harmless".

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
| Oracle judge, MPNet, length all ≤ 0.55 | 0.85 | **two of three hit, one miss** — judge 0.517, MPNet 0.519, but `length_tokens` **0.571** [0.562, 0.580] |
| Detection D "excluded" | 0.6 | **hit** |
| Primary B outcome | graduate 0.1 / excluded 0.45 / inconclusive 0.35 | pending; seed 0 points at **graduate** |

The scorecard is unflattering in an interesting way: the gate predictions were roughly calibrated,
but the plan did not anticipate that the strongest detectors would be trait-blind, and it put only
0.1 on the causal direction that the seed-0 pilot now shows.

## Spend and wall-clock

**$69.81 settled of a $105 cap** (GPU $69.02, API $0.79), with session 3's pod still running
against a $23.94 reservation. The cap rose from $60 in four logged steps, each in `probe.toml`
with its reason, all inside the probe tier's $150. The API line is T0's QA pairs ($0.0426) plus
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
| Session 3 re-run (live) | 1 | ≤ 23.94 | yes |
| Oracle judge (20,000 rows) and T0's QA pairs | — | 0.79 | yes |

**$37.63 of the $69.81 bought no science.** Three distinct causes, all in log.md with their
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
