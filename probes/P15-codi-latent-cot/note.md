# P15 note: reading a CoDI model's latent thoughts does not predict its mistakes better than its own words or its no-thinking answer

**Answer.** No, not on either model we tested. On Cywiński et al.'s three-mode CoDI checkpoint (Llama-3.2-1B; Stage A), a linear probe on the latent vectors predicts the latent mode's exact wrong answer on 66.1% [62.6, 69.6] of its 2,097 errors. The best text-side baseline, a classifier on the model's verbal and direct outputs, reaches 64.6%: a difference of **+1.5 points [−1.4, +4.5]**. A probe on the question encoding alone does as well as the latent probe (−1.4 [−3.1, +0.3]). The plainest baseline does too: the model's own direct (no-thinking) answer matches the latent mode's wrong answer on 65.6% of errors.

On our fine-tune of that checkpoint on 3–5-hop arithmetic chains (Stage B), no reader predicts the wrong answer on even 30% of the 962 errors. The latent probe beats the text classifier by +10.6 [8.2, 13.1], but it reaches only 14.6% in absolute terms, and the direct answer does better at 24.3% (+9.8 [6.3, 13.3] over the latent probe). For *detecting* an error, the latents' advantage over text in Stage A (+4.9 AUROC) is entirely the question encoding's (question-only probe 0.917 vs latent probe 0.918). In Stage B, a simple check of whether the verbal answer disagrees with the latent one flags errors at AUROC 0.998.

**Mechanism (observation, Stage A).** The latents hold the *correct* intermediate values even when the answer is wrong: the logit lens reads gold s1 on 100% and gold s2 on 97% of errors. The error enters at the final combining step, and there the latent mode most often gives the answer the model would give without thinking.

**Prereg verdict** (the plan's rules, `plan.md` Measure): Stage A **O2** (LP − B inside ±10). Stage B **O5** (its error check passed, but no reader is readable). Under the stricter governing rule Jacob adopted before the run, the stages disagree, so there is **no automatic call: the continue/pivot/kill decision is Jacob's** (`python -m p15.analysis decide` → `{"decided_by": "Jacob", "reason": "stages disagree: A O2, B O5"}`). **My recommendation: pivot**, i.e. drop CoDI for the sprint. Neither stage shows monitor-relevant information in the latents that the prompt encoding and the model's no-thinking answer lack. The interesting case, latents trained without a verbal teacher or with RL, is a study-scale step.

Spend **$4.84** of $40 (three H100 sessions, no API). Wall-clock from GO to results: about 1 h 50 min.

## What was run

- **Model (Stage A):** `bcywinski/codi_llama1b-answer_only` @ `315ec5cb` on `meta-llama/Llama-3.2-1B-Instruct` @ `9213176`, loaded through the upstream `CODI` class (`github.com/cywinski/codi` @ `f0bf2fa`, cloned on the pod, not vendored). Decoding is greedy in all three modes, using our own loop: masked, with mask-derived position ids, stopping at any Llama end token. The smoke checks it against upstream `generate` at batch size 1, and against upstream's padded batches of 128 on all items (below).
- **Stage B model:** the same checkpoint trained further for 2 epochs (2,990 steps, LoRA r 128, lr 2e-4, upstream losses) on 24,000 chain items (a quarter of them 2-hop) plus 24,000 GSM8k-Aug-NL rows. The chain items start at X ∈ 1..30 and apply 3–5 steps of +a / −a / ×2 / ×3, with every value in 1..199. The verbal trace omits the final step, as in CoDI's training data. Evaluated on 6,000 held-out chains with number sequences disjoint from training.
- **Data:** T = Cywiński's 20 templates × every (X, Y, Z) in 1..8 with X > Y (4,480 items; 224 number clusters). G = GSM8k test (1,319). C-eval as above (4,551 (op pattern, X) clusters).
- **Readers of the latent answer a_L:** V (verbal answer), D (direct answer), LL (logit lens, Stage A), PS (Patchscope, Stage A), LP (multinomial L2 logistic regression on the 7 latent columns at one layer), LPq (the same on the question's last-token residual, no latents; slot 1, the direct-mode `<|eocot|>` position, as a sensitivity), VT (the same classifier on one-hot text features: V, D, the verbal CoT's intermediate results, the operation), K (per-problem lookup, Stage A) and K0 (majority). All supervised readers are 2-fold cross-fitted by template (scenario in Stage B), with layer and L2 strength chosen by inner CV on the training fold. Intervals are 10,000-resample cluster bootstraps (seed 20261012).
- **Sources:** `results/analysis_A.json` / `.md` / `_items.csv`, `results/analysis_B.*`, `results/A/*.jsonl`, `results/B/*.jsonl`, `results/B/train_record.json`, `results/smoke/checks.json`. The arrays (`*_resid.npy`, `*_lltop.npy`) and `results/B/trainables.safetensors` are gitignored; see "Data" at the end.

## Stage A: Cywiński's checkpoint (T, 4,480 items)

| Reader | Share of E (latent errors, n = 2,097) where it equals a_L [95% CI] | All items |
|---|---|---|
| LP (latents) | **0.661** [0.626, 0.696] | 0.794 |
| LPq (question encoding only) | 0.675 [0.642, 0.708] | 0.800 |
| LPq slot 1 (`<|eocot|>` position) | 0.736 [0.706, 0.765] | 0.824 |
| D (direct answer) | 0.656 [0.617, 0.694] | 0.568 |
| VT (text classifier) | 0.646 [0.611, 0.680] | 0.768 |
| K (lookup) | 0.638 [0.598, 0.676] | 0.767 |
| V (verbal answer) | 0.494 [0.455, 0.532] | 0.724 |
| K0 | 0.065 [0.040, 0.093] | 0.065 |
| LL (lens s1 + s2) | 0.002 [0.000, 0.005] | 0.529 |
| PS (Patchscope) | 0.000 | 0.000 |

- **Headline contrasts** (B = VT, the best of {V, VT, K} on E): LP − VT = **+1.5 [−1.4, +4.5]**; LP − LPq = −1.4 [−3.1, +0.3]. That is **O2**. 200 of 224 clusters carry errors.
- **Exploratory** (D was not in the plan's text-side set): D − LP = −0.6 [−4.9, +3.8]; LPq slot 1 − LP = +7.4 [5.6, 9.3]. D or V equals a_L on 79.8% of E.
- **Held-out numbers** (templates and number clusters both unseen): LP 0.292 vs VT 0.481. LP − VT = **−18.9 [−23.4, −14.4]**: the latent probe's parity on the main split leans on having seen the same number combination in the other fold.
- **Error detection** (AUROC for a_L ≠ gold):

  | Probe | AUROC |
  |---|---|
  | LP (latents) | 0.918 [0.903, 0.932] |
  | LPq (question only, exploratory follow-up) | 0.917 [0.902, 0.931] |
  | VT (text classifier) | 0.869 [0.847, 0.890] |
  | flag V ≠ a_L | 0.717 [0.695, 0.737] |

  LP − VT = +4.9 [3.4, 6.5]; LP − LPq = **+0.1 [−0.7, +0.9]**; LPq − VT = +4.8 [3.2, 6.5]. The latents' edge in error detection is the prompt encoding's.
- **Uplift (replication):** latent − direct accuracy is +26.3 [23.7, 29.1] on T (0.532 vs 0.269) and **+4.4 [2.7, 6.1] on GSM8k** (0.428 vs 0.384; Cywiński et al. report +4.9 with temperature-1 sampling). Upstream's own padded-batch decoding gives the same answers on 99% of T and 98% of G items, with accuracy within 0.2 points. So our masked decoding is not what drives these numbers.
- **Readability:** the logit lens (final layer, top-ranked number token) reads gold s1 at column 2 on 100% of T and 100% of E. It reads gold s2 at column 4 on 98.3% of T and 97.4% of E. Its s1 + s2 equals the wrong a_L on 0.2% of E.
- **Patching:** each condition is compared on one shared counterfactual target per item. An averaged-value patch of s1 (vector 2) gives the target on 14.8% of items, against 4.0% for a norm-matched random vector. For s2 (vector 4) it is 4.2% against 1.0%. Same-value patches keep accuracy at 0.55, while different-value patches drop it to 0.18 (s1) and 0.28 (s2). Patching vectors 3 and 5 does nothing (indexing check, below). The vectors carry the intermediates causally, but an averaged donor rarely yields the exact counterfactual answer.
- **Patchscope** (identity prompt fixed before data) is degenerate. It outputs the prompt's own example (1135) for nearly every vector, so it reads nothing here.
- **GSM8k (P6):** 50.2% of the numbers the verbal CoT writes (beyond the question's) appear among the number tokens of some latent cell's logit-lens top-5, against a 12.3% null from mismatched items (ratio 4.1).

## Stage B: our fine-tune on 3–5-hop chains (C-eval, 6,000 items)

- **Accuracy:**

  | Mode | Overall | k = 3 | k = 4 | k = 5 |
  |---|---|---|---|---|
  | Latent | 0.840 | 0.9995 | 0.923 | 0.597 |
  | Direct | 0.627 | | | |
  | Verbal | 0.997 | | | |

  Latent − direct = +21.3 [19.9, 22.7]. On G, latent accuracy drifts from 0.428 to 0.408 (−2.0 [−3.6, −0.4]).

| Reader | Share of E (n = 962) [95% CI] | All items |
|---|---|---|
| D (direct answer) | **0.243** [0.216, 0.271] | 0.613 |
| LPq slot 1 | 0.221 [0.196, 0.248] | 0.476 |
| LP (latents) | 0.146 [0.123, 0.169] | 0.252 |
| LPq (question only) | 0.098 [0.080, 0.117] | 0.182 |
| VT | 0.040 [0.027, 0.053] | 0.795 |
| K0 | 0.011 [0.005, 0.019] | 0.012 |
| V | 0.001 [0.000, 0.003] | 0.838 |

- **Headline:** LP − VT = +10.6 [8.2, 13.1]; LP − LPq = +4.8 [2.2, 7.4]. The best reader on E is D at 0.243, below the plan's 0.30 readability floor, so this is **O5**.
- **Exploratory:** D − LP = +9.8 [6.3, 13.3]; LPq slot 1 − LP = +7.6 [4.6, 10.5].
- **Why VT and LP fall so low here:** both are trained to predict a_L on all items, and on C 84% of a_L are correct. They learn "predict the right answer", which the verbal answer gives almost perfectly (0.997), and say little about the rare errors. D is not fitted, so it carries the latent mode's own bias.
- **Error detection:**

  | Probe | AUROC |
  |---|---|
  | flag V ≠ a_L | **0.998** |
  | LP (latents) | 0.889 [0.880, 0.898] |
  | LPq (question only) | 0.878 |
  | VT (text classifier) | 0.866 |

  LP − LPq = +1.1 [0.4, 1.8]. The verbal mode solves C, so any disagreement with it marks a latent error.
- **Readability:** intermediates are only partly readable in the fine-tune. The first-ranked number token at the best column (chosen on the other fold) is gold on 34% (s1), 34% (s2), 32% (s3) and 29% (s4) of items. That is less legible than the published checkpoint, and it falls with depth, as Liang & Pan 2026 report for a toy CoDI.

## Predictions against outcomes

| | Prediction (probability) | Outcome |
|---|---|---|
| P1 | Uplift ≥ +3 on GSM8k, CI excluding 0 (0.65); ≥ +10 on T (0.55) | **Yes**: +4.4 [2.7, 6.1]; +26.3 |
| P2 | LL reads s1, s2 on ≥ 70% of T and ≥ 60% of E (0.7) | **Yes**: 100 / 98% of T, 100 / 97% of E (at columns 2 / 4; see indexing) |
| P3 Stage A | O5 0.25, O1 0.2, O4 0.2, **O2 0.25**, O3 0.1 | **O2** |
| P3 Stage B | **O5 0.2**, O1 0.25, O4 0.15, O2 0.3, O3 0.1 | **O5** |
| P4 | LL equals a_L on < 25% of E (0.6) | **Yes**: 0.2% |
| P5 | s2 patch ≥ 15% and ≥ 3× random (0.6) | **No**: 4.2% vs 1.0% (s1: 14.8% vs 4.0%) |
| P6 | ≥ 40% of verbal numbers in a latent cell's top-5, ≥ 2× null (0.45) | **Yes**: 50.2% vs 12.3% |
| P7 | Stage B latent − direct ≥ +10 on C (0.6) | **Yes**: +21.3 |
| P8 | k = 5 latent accuracy ≥ 15 points below k = 3 (0.65) | **Yes**: 40.3 points |
| P9 | Verbal accuracy on C ≥ 90% (0.7) | **Yes**: 99.7% |
| P10 | The fine-tune drops G latent accuracy by > 5 points (0.35) | **No**: −2.0 |

## Deviations and decisions (all logged in `log.md`)

| What | Why | Effect |
|---|---|---|
| **Indexing:** LL, PS and the primary patching moved from "latents 3 and 5" to columns/vectors 2 and 4, after the first smoke and before any bulk data | Cywiński et al. count latents from 1 (`<|bocot|>` = latent 1). On the real checkpoint, s1 and s2 are the top-1 number at columns 2 and 4 (100% and 98% of T) and almost never at 3 and 5 (≤ 0.3%; laptop check on 4 items, then the full T run). | Patching also ran at 3 and 5 as a labelled check; neither had an effect. |
| Smoke "batched = unbatched" checked on verbal's parsed answer, not its full text | The 256-token greedy texts diverged on 3/16 items in bf16 across batch shapes, while latent, direct and both references matched 16/16 | Verbal answers matched 16/16 |
| Upstream padded-batch reference added before data | The checkpoint was trained, and Cywiński's numbers were measured, with latent steps attending to padding | They agree with our loop on 98–99% of items |
| Stricter governing rule (Jacob, before the GO) | A positive on our own fine-tune alone should not trigger "continue" | No automatic call; decision to Jacob |
| GSM8k-Aug-NL loader | The pinned file is column-oriented; session 2's stage B data step crashed | Fixed, then a stage-B-only session |
| 2-hop curriculum in C-train | Liang & Pan 2026 report collapse at 4+ hops in a toy CoDI | k = 5 still reached 0.597 |

The plan's text-side baseline set (Stage A {V, VT, K}, Stage B {V, VT}) omitted D, the direct answer. D turns out to be the strongest simple baseline in both stages. That changes the interpretation, not the prereg labels.

## Follow-up (exploratory)

- **F-q, question-only error probe** (logged before running; $0, laptop): the prediction "within 2 AUROC points of LP" held. Stage A +0.1 [−0.7, +0.9]; Stage B +1.1 [0.4, 1.8].
- **Not run:** F2 (sampling), since the replication diagnostics passed. F3 (tuned lens), F4 (Stage B patching) and F5 (deeper chains), since none of them would change the decision.

## Seeded random raw samples

Drawn by `python -m p15.samples --results results --stage A|B --n 4 --seed 20261012`: 4 errors and 4 correct items per stage, every drawn row shown. The full outputs are in `results/samples_A.txt` and `results/samples_B.txt`.

| Stage | Item | Gold | a_L | V | D | LP | LPq | VT | LL | Verbal CoT (abridged) |
|---|---|---|---|---|---|---|---|---|---|---|
| A, error | T-2264 (4−1, ×1) | 6 | 8 | 8 | 9 | 10 | 10 | 8 | 6 | "4 - 1 = 3 … 3 * 1 = 3 additional" → 8 |
| A, error | T-2539 (5−4, ×4) | 5 | 15 | 15 | 13 | 15 | 15 | 15 | 5 | "5-4=1 … 4*1=4" → 15 |
| A, error | T-3016 (6−4, ×1) | 4 | 8 | 8 | 8 | 8 | 6 | 8 | 4 | "6 - 4 = 2 … 2 * 1 = 2" → 8 |
| A, error | T-3867 (5−2, ×4) | 15 | 18 | 15 | 16 | 17 | 17 | 16 | 15 | "5 - 2 = 3 … 3 * 4 = 12" → 15 |
| A, correct | T-1269 (7+4, ×6) | 77 | 77 | 77 | 55 | 77 | 67 | 77 | 77 | "7 + 4 = 11 … 11 * 6 = 66" → 77 |
| A, correct | T-2345 (6−4, ×2) | 6 | 6 | 8 | 10 | 8 | 8 | 8 | 6 | "6 - 4 = 2 … 2 * 2 = 4" → 8 |
| A, correct | T-2558 (6−2, ×7) | 32 | 32 | 32 | 30 | 32 | 32 | 32 | 32 | "6-2=4 … 4*7=28" → 32 |
| A, correct | T-4406 (7−4, ×7) | 24 | 24 | 24 | 28 | 24 | 24 | 24 | 24 | "7-4=3 … 3*7=21" → 24 |
| B, error | C-2568 (k 4) | 42 | 48 | 42 | 42 | 36 | 54 | 42 | – | correct steps → 42 |
| B, error | C-2976 (k 5) | 108 | 102 | 108 | 108 | 116 | 114 | 108 | – | correct steps → 108 |
| B, error | C-3713 (k 5) | 56 | 60 | 56 | 52 | 58 | 52 | 56 | – | correct steps → 56 |
| B, error | C-5062 (k 5) | 23 | 25 | 23 | 21 | 21 | 24 | 21 | – | correct steps → 23 |
| B, correct | C-1885 (k 3) | 108 | 108 | 108 | 112 | 162 | 96 | 108 | – | correct steps → 108 |
| B, correct | C-3655 (k 3) | 52 | 52 | 52 | 52 | 78 | 44 | 52 | – | correct steps → 52 |
| B, correct | C-3892 (k 3) | 93 | 93 | 93 | 93 | 93 | 99 | 93 | – | correct steps → 93 |
| B, correct | C-5927 (k 5) | 45 | 45 | 45 | 45 | 21 | 69 | 45 | – | correct steps → 45 |

In Stage A the verbal mode often writes the correct intermediates and then gives the same wrong final answer as the latent mode (T-2264, T-2539, T-3016). Both modes fail at the final combination, while the latent logit lens (LL) still reads the correct s1 + s2. In Stage B the latent errors are small offsets at the last step, and the verbal mode is right.

## Interpretation limits

- One 1B checkpoint, one fine-tune of it, templated arithmetic and GSM8k.
- All supervised readers are trained on every item, so on a mostly-correct set they learn the right answer rather than the errors (Stage B). A reader trained on errors only would ask a different question.
- The Stage A sensitivity split shows LP leaning on repeated number combinations.
- Patchscope failed at the one fixed prompt; a different prompt might read the vectors.
- The two stages disagree on the label, not on the substance: neither shows the latents adding information about the model's mistakes beyond the prompt encoding and the no-thinking answer.

## Spend and wall-clock

| Phase | UTC | Wall-clock | Cost |
|---|---|---|---|
| Pre-build (plan revision, code, tests, CPU dry runs; laptop crash 23:00) | 10-12 18:00–23:25 | ~5 h | $0 |
| Session 1: smoke stopped the run (index and verbal checks) | 23:26–23:34 | 8 min (pod work 6 min) | $0.66 |
| Diagnosis and fixes (laptop, real checkpoint on CPU) | 23:34–23:43 | 9 min | $0 |
| Session 2: smoke and stage A (T, G, patching); the stage B data step crashed | 23:43–23:57 | 14 min (stage A GPU work ≈ 5 min; pull ≈ 7 min) | $1.05 |
| Session 3: stage B (benchmark 1.6 min, training 33 min, eval 2.5 min) | 10-13 00:09–00:56 | 47 min (pull ≈ 7 min) | $3.13 |
| Laptop analysis (A twice for F-q, B once; ≈ 18 min sweep each) | 00:20–01:10 | ~50 min | $0 |
| **Total** | | GO to results ≈ 1 h 50 min | **$4.84** of $40 (GPU only) |

Pod idle time was mostly the result pulls (≈ 7 min per session, for 2.8 GB of residuals).

## Data

The residual arrays, logit-lens tops and Stage B trainables (`results/A/*.npy`, `results/B/*.npy`, `results/B/trainables.safetensors`, ≈ 6 GB) are gitignored. They are copied to the main checkout's `probes/P15-codi-latent-cot/results/` at close-out.
