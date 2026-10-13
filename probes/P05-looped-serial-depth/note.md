# P05 note: looped models and no-CoT serial depth (matched 1B pair)

**Answer.** At 1B scale, depth recurrence buys no no-CoT serial depth over a matched non-looped model. Neither model reaches 50% beyond one step on the chain task, in either prompt format, so the planned d₅₀ comparison hits the floor (plan outcome 1). The paired per-depth accuracies say the same:
- **What looping does buy is one-step accuracy.** At r = 32, the recurrent Llama beats its matched baseline on single-step chain items by **+0.21 [0.14, 0.28]** (F-nocot) and **+0.19 [0.12, 0.26]** (F-brauer).
- **At two steps it doesn't:** −0.01 [−0.05, +0.03] and −0.04 [−0.07, −0.01].
- **The apparent three-step gain is a shortcut.** It is +0.06 [0.01, 0.10] and +0.09 [0.05, 0.13], but it comes from items whose final answer happens to equal a one-step value (exploratory follow-up 1).

On multi-op arithmetic, Δd₅₀ (recurrent r = 32 − baseline) is −0.05 [−0.28, +0.14] steps (F-nocot) and −0.12 [−0.25, +0.22] (F-brauer). Loops converge to a fixed point within about 8 iterations, and loops 16–64 change almost nothing.

**Can we read the loops?** Partly. A coda-lens readout shows the **first** intermediate value above a matched control (top candidate on 15–35% of items vs 6–12%) from loop 3–4 on. It shows no later intermediate, and it doesn't beat simply asking the model for step 1 (0.34–0.35). The loop computes one step and stops there.

**What it changes:** under the plan's rule, outcome 1 kills this line at this scale. The loop-specific intervention and the deferred model families (Ouro, Huginn, the Nanbeige pair, the ladder) are not triggered. For Neel's looping question: in the only matched comparison available, recurrence adds per-step accuracy, not serial depth.

| | |
|---|---|
| **Status** | Done. Plan outcome 1 (floor) in both formats; exploratory follow-up 1 explains the three-step anomaly. |
| **Spend** | **$10.25 of $40** (one H100, 2.57 h at $3.99/h); API $0. From `logs/costs.jsonl`. |
| **Models** | `smcleish/Recurrent-Llama-3.2-train-recurrence-32` @ a5f6f126 (r ∈ {1, 2, 4, …, 64}); matched baseline `smcleish/Llama-3.2-non-recurrent-posttrained` @ 004e96ac (same parent, same post-training data); `...-train-recurrence-4` @ c466903f (r ≤ 16), secondary. |
| **Data** | `results/` (pod outputs, sha256-verified); `analysis/report.md` and `analysis/summary.json` (`python -m p05.report results`); `analysis/shortcut.md` and `.json` (`python -m p05.shortcut results --tokenizer <baseline tokenizer.json>`). |
| **Review** | One cross-family round (gpt-6-astra): see the end of this note. |

## The planned measure

Chain items are nocot-bench's `chain` walk: start value, k rule steps, states 1–20, and each step verified load-bearing by Neel's nudge test. There are 300 items per depth (213 at k = 1, which has few distinct problems). Prompts are raw text with 5 examples, in two formats:
- **F-nocot:** the instruction repeated with every example.
- **F-brauer:** the instruction once at the top, with its own independently drawn examples.

Scoring is teacher-forced exact match. It was validated against greedy decoding with nocot's parser: agreement is ≥ 0.98 in every validated cell (36 cells; 1.00 in 30 of them), and bare-number answers make up ≥ 0.94. Precision is TF32: its agreement with strict fp32 was 0.993 for the recurrent model and 1.000 for the baseline, while bf16 failed (0.84). Chance is the majority-value floor: 0.08 for chain, 0.085 for chain-easy and 0.12 for arithmetic. Source: `analysis/report.md`.

**Accuracy per depth** (chain, F-nocot; recurrent model at r = 32; chance 0.08):

| k | 1 | 2 | 3 | 4 | 5 | 6 | 8 |
|---|---|---|---|---|---|---|---|
| baseline | 0.09 | 0.07 | 0.07 | 0.07 | 0.07 | 0.09 | 0.08 |
| recurrent r = 1 | 0.17 | 0.02 | 0.11 | 0.04 | 0.04 | 0.06 | 0.05 |
| recurrent r = 32 | 0.30 | 0.06 | 0.13 | 0.07 | 0.06 | 0.05 | 0.09 |

The F-brauer table has the same shape (k = 1: baseline 0.22, r = 32 0.40; k ≥ 2: 0.02–0.12 for both). The floor rule fired, so the easier chain-easy bank (more halving steps) was added automatically. It also floors: the best single cell is 0.51 at k = 1 (recurrent, r = 4–8, F-nocot), and every k ≥ 2 cell is ≤ 0.15 (apart from k = 7, which has 10 items).

**The d₅₀ fits** (50% crossing of a sigmoid with a chance floor, paired item bootstrap): on chain and chain-easy, every configuration is "below range" or has a CI upper bound ≤ 1.5 steps, in both formats. That is the plan's outcome 1. Arithmetic does cross 50% (at k = 1 only):

| arithmetic | baseline d₅₀ | recurrent r = 32 d₅₀ | Δ [95% CI] |
|---|---|---|---|
| F-nocot | 1.31 [1.03, 1.45] | 1.26 [1.02, 1.40] | −0.05 [−0.28, +0.14] |
| F-brauer | 1.17 [1.01, 1.32] | 1.04 [1.02, 1.31] | −0.12 [−0.25, +0.22] |

These fits sit near the bottom clip of the range, so the paired per-depth differences below carry the comparison.

**Paired accuracy differences, recurrent r = 32 − baseline** (same items, item bootstrap; `analysis/report.md`, "Paired accuracy differences per k"):

| bank / format | k = 1 | k = 2 | k = 3 | k = 4 |
|---|---|---|---|---|
| chain / F-nocot | **+0.21 [0.14, 0.28]** | −0.01 [−0.05, 0.03] | +0.06 [0.01, 0.10] | −0.00 [−0.04, 0.04] |
| chain / F-brauer | **+0.19 [0.12, 0.26]** | −0.04 [−0.07, −0.01] | +0.09 [0.05, 0.13] | −0.00 [−0.04, 0.03] |
| chain-easy / F-nocot | +0.20 [0.11, 0.31] | 0.00 [−0.05, 0.05] | +0.02 [−0.02, 0.06] | +0.03 [−0.01, 0.07] |
| chain-easy / F-brauer | +0.14 [0.06, 0.23] | +0.01 [−0.04, 0.07] | −0.01 [−0.05, 0.02] | +0.02 [−0.02, 0.05] |
| arithmetic / F-nocot | −0.02 [−0.08, 0.04] | −0.02 [−0.09, 0.05] | +0.01 [−0.03, 0.05] | +0.02 [−0.03, 0.07] |
| arithmetic / F-brauer | +0.02 [−0.05, 0.09] | −0.01 [−0.08, 0.07] | −0.06 [−0.12, −0.01] | +0.01 [−0.02, 0.04] |

Before pulling results, we fixed an interpretation rule (`log.md`, 2026-10-12 23:50 UTC): a depth gain needs a positive k = 2 or k = 3 difference, CI excluding 0, on chain or chain-easy, in both formats. Chain at k = 3 meets that clause on paper. It doesn't count, for two reasons. The plan's outcome is 1, and the rule is a necessary condition, not a sufficient one. And the k = 3 difference turns out to be a one-step shortcut (next section). Within the recurrent model (r = 32 − r = 1) there is no k ≥ 2 difference that holds in both formats either: k = 2 is +0.04 [0.01, 0.07] and +0.01 [−0.01, 0.03]; k = 3 is +0.02 [−0.03, 0.06] and +0.09 [0.05, 0.12].

**Loops converge, so extra loops are idle.** The median relative state change ‖s_t − s_{t−1}‖/‖s_t‖ falls by loop:

| loop | 2 | 4 | 8 | 16 | 32 |
|---|---|---|---|---|---|
| median relative change | 0.41 | 0.15 | 0.028 | 0.0033 | 0.0008 |

The answer at loop t equals the loop-64 answer on 61% of rows at t = 4, 92% at t = 8, 98.5% at t = 16 and 99.4% at t = 32. Per-depth accuracy plateaus by r = 4–8 everywhere (`analysis/summary.json`, `convergence` and `pred_same_as_r64`). This matches the fast fixed-point convergence reported for this retrofit (`lit-review.md`).

**Init seed.** A rerun of the recurrent model with a different random initial state (seed 1, chain, both formats) gives the same r = 32 accuracy as seed 0 at every depth: the paired difference is 0.00 [0.00, 0.00]. Individual predictions differ on 6% of rows at r = 1, 1–2% at r = 4 and 0.2–0.5% at r ≥ 16, and none of those changes correctness at r = 32.

**Training depth.** The recurrence-4 model at r = 4 is indistinguishable from the baseline. Its chain differences are all within ±0.04, the largest −0.04 [−0.07, −0.00] at k = 2 (F-brauer). Its arithmetic d₅₀ is 1.29–1.34.

## Follow-up 1 (exploratory): the three-step anomaly is a one-step shortcut

Logged before the committed analysis ran (`log.md`, 2026-10-13); free; `p05/shortcut.py`; post hoc.

- **Which items coincide:** 30% of k = 3 chain items (89/300), against 1% at k = 2 (3/300), have a final answer equal to a "one-step value" (the first or the last rule applied to the start number). This is a quirk of the rule bank.
- **The recurrent model often gives that one-step value:**
  - At r = 32 its answer is a one-step value on 19–36% of chain items at k = 2–5 (11–37% at r = 1).
  - The baseline's share is 6–18%.
  - The baseline instead copies the start value, mostly in F-brauer: 32–45% of answers at k = 2–4 (`analysis/shortcut.md`, part (a)).
- **On the coincident k = 3 items** the recurrent model scores 0.21 (F-nocot) and 0.22 (F-brauer). The baseline scores 0.04 and 0.08.
- **On the other 211 k = 3 items** recurrent − baseline is **+0.014 [−0.033, +0.062]** (F-nocot) and **+0.062 [+0.024, +0.100]** (F-brauer).
  - The F-brauer residual comes from the baseline, not the recurrent model. The baseline scores 0.014 there, below a shuffle null of 0.046, because it copies the start value.
  - The recurrent model's 0.076 is about at its own null of 0.064.
- **Step 1 isn't composed into step 2.** We asked the recurrent model (r = 32) the step-1 prefix of each k = 2 item. Its k = 2 accuracy is 0.098 when it gets step 1 right (n = 92), against 0.038 when it doesn't (F-nocot). In F-brauer it is 0.050 vs 0.014. Both are near chance.

Prediction ("the k = 3 gain sits on coincident items and vanishes on the rest; the readout shows step 1 only"): right in F-nocot. In F-brauer a residual remains, explained by the baseline scoring below chance rather than by the recurrent model scoring above it. The readout half is right (next section).

## Reading the loops

The readout is per loop at the answer position: the top candidate among the 20 values, under the **coda lens** (coda, final norm and head) and a **plain logit lens** (final norm and head, no coda). Control: the same readout scored against another item's intermediate (same k, fixed derangement). Source: `analysis/summary.json` (`readability`) and `analysis/shortcut.md` (part (c)).

- **Step 1 is readable, later steps aren't.**
  - At the best loop, the coda lens has the true step-1 value on top for 0.21–0.35 of eligible k = 2–4 items, against 0.07–0.10 for the control.
  - Steps 2 and 3 are at control (0.05–0.15 vs 0.07–0.15).
  - The plain logit lens reads nothing (difference −0.05 to −0.01).
- **By loop** (chain, F-nocot, items whose step 1 differs from the answer):

  | loop | 1 | 2 | 3 | 4 | 8 | 32 | 64 |
  |---|---|---|---|---|---|---|---|
  | step 1 on top | 0.21 | 0.09 | 0.18 | 0.16 | 0.13 | 0.15 | 0.15 |
  | control | 0.07 | 0.04 | 0.06 | 0.06 | 0.06 | 0.06 | 0.06 |
  | final answer on top | 0.04 | 0.06 | 0.05 | 0.07 | 0.08 | 0.08 | 0.08 |

  The other three cells have the same shape (step 1 at 0.17–0.35 from loop 4 on). Step 1 doesn't give way to step 2: it stays as the fixed point, so it *is* the model's answer on those items.
- **Asking the model does at least as well.** The model's own top-1 on the step-1 prefix prompt is 0.34–0.35 at r = 32 (control 0.06–0.07), and 0.20–0.26 at r = 1. For steps 2–3 it is 0.06–0.10, at control.
- **Order** (Spearman ρ between step index and first loop at which it's on top): +0.24 to +0.36 on all eligible items. It is driven by step 1 appearing early and the rest never appearing. On correctly answered items (18–48 pairs) ρ is −0.19 to +0.46, with CIs spanning 0.

This is readability of one intermediate, which the model also reports when asked. It is not evidence of a multi-step latent chain.

## Predictions against outcomes

Written 2026-10-12 before any data (`plan.md`).

| prediction | p | outcome |
|---|---|---|
| 1. Floor: d₅₀ ≤ 1.5 or no crossing, chain and fallback | 0.25 | **Happened** (both formats) |
| 2. Recurrence beats baseline (Δ CI > 0) | 0.35 | No |
| 3. Recurrence worse | 0.05 | No (not reached; first match wins) |
| 4. Equivalent within ±0.5 | 0.15 | Not reached. Arithmetic Δ CIs reach −0.28 and +0.22, inside ±0.5 |
| 5. Inconclusive | 0.20 | Not reached |
| Loop gain d₅₀(32) − d₅₀(1) CI > 0 (chain, F-nocot) | 0.55 | No: +0.03 [−0.18, 0.10]. The per-k gain is at k = 1 (+0.12 [0.06, 0.19]) |
| F-brauer agrees, given a loop gain | 0.6 | Not applicable |
| Seed 1 Δ within 0.2 of seed 0 | 0.85 | Yes (identical) |
| Recurrence-4 at r = 4 within 0.3 of recurrence-32 at r = 32 | 0.4 | Yes, but uninformative at the floor (chain both below range; arithmetic Δ +0.09 and +0.24) |
| Some intermediate readable above control on ≥ 30% of correct k = 2–3 items | 0.35 | No: hit 0.17–0.33 vs control 0.16–0.28; differences −0.03 to +0.15, CIs include 0 |
| Order ρ ≥ 0.5 | 0.15 | No |
| Readout beats asking the model | 0.15 | No |

## Random raw samples

These are all 12 rows drawn by `p05/report.py` (`raw_samples`, `random.Random(20261013).sample` over every greedy-validation row pooled across models, banks, formats and loop counts). "text" is the greedy continuation after `Answer:`. Source: `analysis/summary.json`, `raw_samples`.

| item | model, r | format | k | greedy text | parsed | correct |
|---|---|---|---|---|---|---|
| arithmetic:k1:6 | recurrence-4, 1 | brauer | 1 | ` -124⏎⏎Problem: Evaluate this Python expression.` | −124 | yes |
| chain_easy:k1:80 | recurrent, 1 | nocot | 1 | ` 1⏎⏎You` | 1 | no |
| arithmetic:k5:3 | recurrent, 32 | brauer | 5 | ` 1⏎⏎Problem: Evaluate this Python expression.` | 1 | no |
| chain:k5:132 | recurrent, 32 | nocot | 5 | ` 3⏎⏎You` | 3 | no |
| chain:k4:122 | recurrence-4, 4 | nocot | 4 | ` 7⏎⏎You` | 7 | no |
| chain:k6:183 | recurrence-4, 16 | nocot | 6 | ` 3⏎⏎You` | 3 | no |
| arithmetic:k6:3 | recurrence-4, 1 | nocot | 6 | ` 0⏎⏎You will be given a math problem` | 0 | no |
| chain:k8:275 | recurrent, 1 | brauer | 8 | ` 11⏎⏎Problem` | 11 | no |
| chain_easy:k6:184 | recurrent, 1 | nocot | 6 | ` 10⏎⏎You` | 10 | yes |
| chain_easy:k8:92 | baseline | brauer | 7 | ` 14⏎⏎Problem` | 14 | no |
| chain_easy:k3:217 | recurrent, 32 | brauer | 3 | ` 8⏎⏎Problem` | 8 | no |
| arithmetic:k5:58 | recurrence-4, 1 | brauer | 5 | ` 0⏎⏎Problem: Evaluate this Python expression.` | 0 | no |

Every row is a bare number followed by the next example's header, which is what the teacher-forced scorer assumes. Teacher-forced and greedy grading agree on all 12. chain_easy:k8:92 has k = 7: its nudge depth was 7, so it was relabelled.

## Caveats

- **Scale and elicitation.** These are 1B base models, prompted with 5-shot raw text capped at 1,024 tokens (the training context). The floor may be partly elicitation: chat formats, more examples or other instruction wording weren't tried. The format check covers layout, instruction placement and example set, not those. A larger looped model may not floor; this probe says nothing about Huginn-3.5B or Ouro.
- **Why we didn't run the larger models:** the plan gates them on a robust depth gain at this scale, and none appeared.
- **Single matched pair.** "Recurrence buys no depth" holds for one retrofit recipe (McLeish et al.): 4 + 6 × r + 4 layers, trained at mean recurrence 32 on math text. Training from scratch, or another adapter design, could differ.
- **Post hoc analysis.** Follow-up 1 was designed after seeing the k = 3 anomaly. It explains that anomaly but is exploratory.
- **Precision.** Agreement between TF32 and strict fp32 was 0.993, not 1.0, for the recurrent model. Rounding can flip a few answers at the margin. That doesn't touch the floor call or the paired differences, whose CIs are wider than ±0.01.
- **Arithmetic** has 100 items per depth; its d₅₀ fits sit at the bottom clip, so treat its Δ as a bound on large effects only.

## Spend and wall-clock

| phase | UTC | wall-clock | cost |
|---|---|---|---|
| Build, tests, laptop smoke and dry run | 2026-10-12, to 23:00 | (laptop) | $0 |
| Floor screen (laptop, 30 items per k) | 23:10–23:34 | 24 min | $0 |
| Pod launch, venv, model downloads, pod tests, precision smoke, bench | 23:36–23:45 | 9 min | in pod |
| Pod main run | 23:45–02:09 | 144 min (see below) | in pod |
| Pod total (billed) | 23:36–02:09 | 2.57 h | **$10.25** |
| Analysis, follow-up 1, note | 02:09– | (laptop) | $0 |

Main-run units, from `results/timing.jsonl`:

| unit | minutes |
|---|---|
| recurrent seed 0, chain | 28.6 |
| recurrent seed 0, chain-easy (added by the floor rule) | 24.1 |
| recurrent seed 1, chain | 26.1 |
| greedy validation | 20.2 |
| ask-the-model prefixes | 17.1 |
| recurrence-4, all cells | 21.2 |
| arithmetic | 4.6 |
| baseline | 1.6 |
| model loads | 0.6 |

The bench projected 1.63 h for the main run, and it took 2.40 h (1.47×). Most of the excess is chain-easy, which the floor rule added after the projection (about 31 min with its validation). The pod's GPU was idle only between units (model loads, under 1 min in all).

## Review

(Filled in after the cross-family review round.)
