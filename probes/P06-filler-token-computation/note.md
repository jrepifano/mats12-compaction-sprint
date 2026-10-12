# P06 note: what computation happens in filler tokens, and can current readers see it?

Probe tier, 2026-10-11. Plan: [`plan.md`](plan.md) (approved by Jacob in chat). Run log: [`log.md`](log.md). Literature: [`lit-review.md`](lit-review.md). Spend **$42.05** of a $75 cap (raised from $35 in steps; see Deviations): $13.00 for the first pass (merged in PR #20, 2026-10-11) and $29.05 for the V4 Flash GPU follow-ups F7 and F8 (2026-10-11/12). Wall-clock about 2 h 25 min for the first pass, and about 3 h 40 min of pod time for F7–F8.

## The answer

1. **On the planned task, neither model uses filler.** That task is nocot-bench's chain, a value carried through k dependent steps ([nocot-bench](#nocot-bench)), run with nocot's no-CoT recipe.
   - Gemma 4 31B (n = 700 items, two draws each) gains **+0.8 points [−1.8, +3.3]** from 500 dots after the question compared with 500 dots before it. The depth shift is +0.19 steps [−0.15, +0.71]. The Stage 2 gate (≥ 3 points, lower bound above 0) fails.
   - V4 Flash is near floor at k = 2 under that recipe (0.39 with 10 shots, against the ≥ 0.70 gate), so it was dropped from chain.
   - On multiple choice Gemma shows no uplift at any option count.
2. **That null comes from the recipe and the task, not from the models.** In Brauer et al.'s own task and prompt format ([Brauer et al. 2026](#brauer-verdun-and-marks-2026)), both models use filler (exploratory follow-ups).
   - The task is a system of equations with one hidden value y.
   - **V4 Flash: 36.4% → 61.4% with 500 dots, +25.0 points [+20.6, +29.4]**. 100 dots placed *before* the question give 24.4%. This is about the size Brauer et al. report for DeepSeek V3 (31 → 61%).
   - **Gemma 4 31B: 26.4% → 35.8% with 100 dots, +9.4 [+5.6, +13.4]**; pre-padding gives 27.0%.
   - Gemma 3 27B, which has an open NLA, is at floor (≈ 2%) in every arm.
3. **Detectable uplift at 2–3 serial steps, with no consistent evidence beyond.** On nocot's chain in Brauer's format, V4 Flash gains **+19.5 points at k = 2 [+11.5, +28.0]** and **+8.0 at k = 3 [+2.0, +14.5]** (500 dots after vs 500 before).
   - At k = 4–8 the per-k estimates run from −4.5 to +2.0, and the intervals reach up to +7.5. So this is no consistent evidence of uplift at depth 4–8, not proof that there is none, and not a measured gain in serial capacity.
   - On Brauer's task at depth 2 and 3, both models are at floor (≤ 2%) with or without filler. Those items also have much larger answers.
   - Gemma shows no chain uplift in this format: −1.7 [−4.0, +0.5].
4. **On multiple choice, V4 Flash's uplift does not shrink as options are added.** On Brauer's task it is +11.2 with 2 options, +11.4 with 5 and **+21.8 with 10**; uplift(2) − uplift(10) = −10.6 [−16.0, −5.0]. As a share of the headroom above base accuracy it is roughly flat (55%, 41%, 48%).
   - This is evidence against a capacity-limited guess-and-check over the listed options.
   - It is not a clean capacity test: option count also changes how the distractors are built (10 options need nearby numbers beyond the variable-derived ones) and how much headroom there is.
   - Gemma gets little from filler once options are listed (+2.4, 0.0, +1.8).
5. **In Gemma the filler region carries the hidden value, causally and readably** (one H100, $1.48; 150 items of Brauer's task).
   - **Readable.** All three lenses (logit lens, J-Lens, J++) read the first digit of the hidden value y. At the `Filler:` label they pick its first digit as the top digit on **34–37% of items, against 12–14% under a frequency-preserving permutation null** (p < 0.001). On items where y's first digit is unique among the five chain values (n = 63), that rises to 41–49%. Across the 100 dots, the logit lens reads it on 25% and J++ on 20% (nulls 11% and 8%); J-Lens is weaker (12% vs 8%).
   - **Controls.** The base literal x, which is written in the prompt, is not read above its null at either location.
   - **Brauer's finding.** This replicates Brauer et al.'s finding that the label position is the best cell ([Brauer et al. 2026](#brauer-verdun-and-marks-2026), Table 16).
   - **Readout limit.** Gemma tokenizes numbers digit by digit, so the readout scores first digits only.
   - **Causal.** We intervened on the filler K/V projections during the target's prefill, writing in the projections from a donor that differs only in x. The output then lands closer to the donor's answer than to the target's on **52.0% of pairs [44.0, 60.0]**, against **2.0%** for the un-transplanted output and **5.3%** for a random-donor intervention, both scored against the same donor answer. The output equals the donor's own output on 21.3% [15.3, 28.0].

6. **In V4 Flash the filler computes the hidden values in layer order, readers see them as whole numbers, and the answer is read from the filler** (one B300, F7–F8, $29.05; exploratory).
   - **Readable, whole values.** On Brauer's task (100 items, 100 dots), the hidden value y is the reader's top-1 token over the whole vocabulary at some filler position on **98–100% of items, against 9–13% when each item is scored on another item's y**. Twice y (the next step) is top-1 on 81–85%, and the answer on 73–77% (nulls 6–8%); the model answers 71% correctly. Dots placed before the question read nothing (at null). In the 10-option prompt without filler, y is top-1 at the answer position on only 6–16% (null 1–2%).
   - **In layer order.** Within an item, y first becomes top-1 around layer 28–30, 2y about 3 layers later, and the answer about 3 layers after that. Counting items with a non-zero difference, 2y comes after y on 71–78 of 77–81, the answer after 2y on 43–49 of 58–63, and the answer after y on 63–71 of 70–75, per reader (sign p < 0.001 for every pair and reader). Most first appearances are within the first ~15 dots. This is a serial computation across layers at the start of the filler, not a relay along the dots.
   - **Chain, k = 2.** The step-1 value is read above a permutation null by all three readers (28–48% vs 5–6%). In the first 20 dots, the answer appears about 3 layers after step 1 (sign p 0.004–0.014; the 20-dot window was chosen after seeing the varbind result). At k = 3 the second intermediate is barely read (J-Lens only, 12% vs 5%), in line with the small k = 3 uplift (+5 [−2, +12] locally).
   - **Causal.** We overwrote the 500 dots' residual streams at every layer with those of a donor chain that differs only in its start value. The model then gives **the donor's answer on 35% [29, 42] of pairs** (48% at k = 2, 22% at k = 3). The controls score 6.5% (a random donor's patch) and 6% (unpatched), both against the same answer. The patched output equals the donor's own output on 84%, and on pairs where target and donor are both correct the patch gives the donor's answer on 92%. The question text is untouched, so the answer comes from the filler, not from re-reading the question.
   - **Architecture caveat.** V4 Flash attends over a 128-token sliding window plus compressed memory of older tokens. With 500 dots, the answer position reaches the question only through compressed entries. The dots are therefore the main local route, and the strength of the patch effect may partly reflect that. Gemma's 52% K/V result (item 5) shows the effect is not V4-specific.

**What it changes.** Of the plan's outcomes, the evidence is closest to **outcome 2, "uplift without depth"**: filler helps on a one-hidden-step problem and on 2–3-step chains, and readers show the hidden value. The rule for outcome 2 is to **continue briefly: a transplant check, then a short note for Neel**. Both are done here.
- **Outcome 3, the readable serial relay, half holds on V4 Flash.** The plan's criteria were a depth shift ≥ 0.5 steps, ≥ 30% of intermediates recovered, and ρ > 0.3. Intermediates are recovered far above 30%, and they appear in serial order, but the order runs across layers, not along the dots. The depth gain is small: about one step (uplift at k = 2, little at k = 3).
- *Inference:* the filler gives the model positions where it can compute the hidden value and the answer mid-stack, which the answer position then reads, effectively one extra pass of depth. That would explain uplift at k = 2 and little beyond. It is a hypothesis; patching at a subset of layers or dots would test it.
- **Not a pivot.** The original note's "readers can't see it" (outcome 4) came from a flawed null and is withdrawn (Review, below). On V4 Flash all three readers, including the plain logit lens, see the filler's computation; the logit lens is as good as the J-Lenses on whole values.
- **Graduation is Jacob's call.** This is P06's strongest positive result: filler computation that the simplest reader sees, with a causal check, on a frontier-scale open model. The "parallel candidates" question (prediction 5 of F8) remains open, because our MC contrast was confounded.

## Predictions against outcomes

Plan predictions (written 2026-10-11, before data), scored on the planned measure, with how the follow-ups bear on each:

| Prediction | p | Planned measure (nocot chain, our recipe) | Follow-ups (exploratory) |
|---|---|---|---|
| 1. No usable uplift (neither model gains ≥ 3 points, lower bound > 0) | 0.35 | **Happened.** Gemma +0.8 [−1.8, +3.3]; V4 Flash dropped at the smoke gate | Not in Brauer's format: V4 Flash +19.5 at chain k = 2; both models gain on Brauer's task |
| 2. Uplift without depth (depth shift < 0.5; uplift shrinks with more options; readers show candidates) | 0.30 | n/a (no uplift) | **Closest.** Uplift detectable only at 2–3 steps, and readers show the hidden value. But uplift does not shrink with more options (it grows on V4 Flash) |
| 3. Readable serial relay | 0.15 | n/a | Not tested at depth > 1 on GPU. At depth 1 the hidden value is readable; there is no relay to order |
| 4. Uplift readers can't see | 0.20 | n/a | Not supported. y's first digit is read well above a permutation null. Recovery on uplift items (40%) is about the same as on the rest (35%) at the label |
| Side: V4 Flash gains less than Gemma | 0.6 | n/a | **Wrong.** V4 Flash gains more (+25 vs +9 on Brauer's task; +19.5 vs −4.5 at chain k = 2) |

Follow-up predictions, written in `log.md` before each run:
- **Placement pilot:** every-turn placement shows the larger uplift (0.6). Leaned yes but within noise: +3 to +4 vs −4 to +3 points.
- **F1:** no ≥ 5-point uplift on either model (0.6). **Wrong:** +25 and +9.4.
- **F2(a):** depth-2 uplift under half of depth-1 (0.65). True, because depth 2 is at floor.
- **F2(b):** uplift smaller with 10 options than with 2 (0.4). **Wrong** for V4 Flash; flat for Gemma.
- **F4:** Gemma 3 uplift ≥ 5 points (0.4). Wrong: at floor.
- **F5:** no Gemma chain uplift ≥ 3 points in Brauer's format (0.65). **Inconclusive.** The pooled 500 − base estimate is +3.5 [−1.5, +8.5]. F6, with its matched control, gives −1.7 [−4.0, +0.5].
- **F6:** V4 Flash uplift ≥ 5 at k = 2–3 and ≤ 2 at k ≥ 5, and depth shift < 0.5 (0.6). **Mixed.** The per-k part holds as point estimates (+19.5, +8.0; −4.5 to +2.0 at k ≥ 5, with intervals up to +7.5). The depth-shift part can't be resolved, because the k50 fit extrapolates below the measured range (+1.01 [+0.11, +3.90]).
- **F3:** transplant moves the answer to the donor's on ≥ 5% and changes it on ≥ 20% (0.5). True: 10% and 91%. First-digit readouts beat the null (0.5). **True under the corrected permutation analysis.** The original max-over-all-positions analysis said false; see Review.
- **F7:** V4 Flash loads on one Blackwell GPU and passes the lens-vs-model gate (0.6). **True** on the third attempt (agreement 0.99994); the first two failed on our snapshot bug and an HF 504.
- **F8.1:** step 1 read at k = 2 above a permutation null (0.6). **True** for all three readers.
- **F8.2:** both intermediates read at k = 3 (0.35). **Partly:** step 1 yes; step 2 by J-Lens only (12% vs 5%).
- **F8.3:** steps peak in order, step 1 earlier than step 2 (0.3). **Not supported as specified** on chain k = 3 (6–10 usable items, no order). An exploratory check finds a strong layer order on varbind (y → 2y → answer) and at chain k = 2 (step 1 → answer).
- **F8.4:** matched patch gives the donor's answer on ≥ 30%, against ≤ 10% for the random donor (0.6). **True:** 35% vs 6.5%.
- **F8.5:** distractor options elevated over unlisted controls at the filler with 10 options (0.3). **Not interpretable:** the elevation also appears with no options shown and in the pre-padding null, so it reflects how the candidate values were chosen.

## Planned measures (Stage 1)

Recipe from nocot-bench @ 634d7de5: prefill `Answer:`, `reasoning: {enabled: false}`, temperature 0, max_tokens 100, nocot's three witnesses plus a reasoning-on positive control (caught 5/5 on both models). Rows that fail a witness are scored wrong.
- **Filler:** 500 space-separated dots (500 contiguous dots tokenize to 32 tokens on Gemma and 6 on V4 Flash), in every user turn.
- **Shots:** 10 chain shots (the alternate recipe; see Deviations).
- **Statistics:** 95% intervals from an item bootstrap (10,000 resamples, seed 0). The depth-shift intervals use 2,000 resamples stratified by k, because each resample refits the logistic.
- **Source:** `results/stage1/analysis_gemma4.json` (`p06.analyze`).

| Gemma 4 31B, chain | none | after | before | after − before |
|---|---|---|---|---|
| all k (n = 700) | 0.244 | 0.259 | 0.251 | +0.8 [−1.8, +3.3] |
| k = 2 | 0.600 | 0.615 | 0.580 | +3.5 [−4.5, +11.5] |
| k = 3 | 0.405 | 0.470 | 0.455 | +1.5 [−4.5, +8.0] |
| k = 4 | 0.175 | 0.210 | 0.185 | +2.5 [−4.0, +9.0] |
| k = 5..8 | 0.065–0.195 | 0.105–0.170 | 0.090–0.180 | −2.5 to +2.0 |
| fact_hops (n = 64) | 0.172 | 0.188 | 0.164 | +2.3 [−3.9, +9.4] |

- **Depth:** k50 is 2.23 (before) and 2.42 (after), so the shift is +0.19 [−0.15, +0.71], with the chance floor at 0.07.
- **Multiple choice** (after − none): +−0.7 with 2 options, +2.0 with 5, +0.8 with 10.
- **V4 Flash fact_hops:** after − before +0.8 (0.406 vs 0.398).
- **Invalid (witness-failing) rows:** ≤ 1% per arm.

## Follow-ups (exploratory)

Each was logged with its reason, prediction and cost before it ran. All are one draw at t = 0 unless stated.

**F1. Brauer's system-of-equations task in Brauer's format** (`p06.brauer`).
- **Data:** their 500 items, `data/chained_var_binding_dataset.json` @ a701927. A re-run of their generator reproduces it.
- **Format:** 5 shots carrying the same filler, a `Filler:` label, no prefill.
- **Source:** `results/stage1/f1_brauer_*.jsonl`, tabulated by `p06.f1_table`.

| Model | base | 100 dots | 500 dots | 100 dots before | best after − base | after(100) − before(100) |
|---|---|---|---|---|---|---|
| V4 Flash | 0.364 | 0.570 | 0.614 | 0.244 | +25.0 [+20.6, +29.4] (500) | +32.6 [+28.0, +37.2] |
| Gemma 4 31B | 0.264 | 0.358 | 0.356 | 0.270 | +9.4 [+5.6, +13.4] (100) | +8.8 [+5.2, +12.4] |
| Gemma 3 27B (F4) | 0.018 | 0.014 | 0.008 | 0.020 | −0.4 [−1.6, +0.8] | −0.6 [−1.6, +0.4] |

Brauer's after and before arms are not exactly token-matched. The labels differ (`Filler:` vs `Padding:`), which adds 6 tokens per prompt across the 6 user turns (review r1). Stage 1 and F3 comparisons are exactly matched. V4 Flash's base arm had 17 of 500 invalid rows (16 empty replies, 1 visible CoT), scored wrong. On valid rows only its base is 0.377, and the uplift stays about +24.

**F2. Depth and multiple choice on Brauer's task** (their generator with `--chain-len 2/3`, seed 42).
- **Depth.** At depths 2 and 3 every arm is at 0–2.2% on both models. Deeper items also have larger answers (up to 3,065 and 8,962), so depth and arithmetic size are confounded.
- **Multiple choice** (depth 1, base vs 100 dots). The distractors are the answers the question would give for each other variable's value, then nearby numbers. Source: `results/stage1/f2_mc_*.jsonl`, `p06.f2_mc_table`.

| Options | V4 Flash base → dots | uplift | Gemma base → dots | uplift |
|---|---|---|---|---|
| 2 | 0.798 → 0.910 | +11.2 [+7.8, +14.6] | 0.914 → 0.938 | +2.4 [+0.6, +4.2] |
| 5 | 0.724 → 0.838 | +11.4 [+7.6, +15.2] | 0.814 → 0.814 | 0.0 [−2.4, +2.4] |
| 10 | 0.544 → 0.762 | +21.8 [+17.4, +26.2] | 0.556 → 0.574 | +1.8 [−2.0, +5.4] |
| uplift(2) − uplift(10) | | −10.6 [−16.0, −5.0] | | +0.6 [−3.4, +4.6] |

**F5 and F6. nocot's chain in Brauer's format** (`p06.f5_chain_brauer`).
- **F5:** k = 2–3, 4 arms. V4 Flash 500 dots − base +12.5 [+6.5, +18.5]; 100 dots does nothing.
- **F6:** k = 2–8, base / 500 after / 500 before, 2 draws. Source: `results/stage1/analysis_f6_*.json` (`p06.f6_analyze`).

| after − before (500 dots) | k = 2 | k = 3 | k = 4 | k = 5 | k = 6 | k = 7 | k = 8 | pooled |
|---|---|---|---|---|---|---|---|---|
| V4 Flash | **+19.5** [+11.5, +28.0] | **+8.0** [+2.0, +14.5] | −0.5 | −2.0 | −1.0 | +2.0 | −4.5 [−8.5, −1.0] | +3.1 [+0.9, +5.3] |
| Gemma 4 31B | −4.5 [−10.5, +1.5] | 0.0 | −1.0 | +2.0 | −4.0 | −3.0 | −1.5 | −1.7 [−4.0, +0.5] |

- **V4 Flash accuracy at k = 2:** base 0.315, 500 before 0.305, 500 after 0.500.
- **V4 Flash's k50 fit:** a +1.01 [+0.11, +3.90] shift. Both k50 values (0.90 and 1.90) lie below the measured range, so the fit extrapolates; read the per-k row instead.
- **V4 Flash at k = 8:** one of 7 per-k intervals excludes 0 in the negative direction. We read it as noise.
- **Gemma's pre-padding:** 500 dots before sit above base at most k (pooled 0.267 vs 0.225). Extra tokens help Gemma a little wherever they are.

**F3. Gemma 4 31B on one H100: readouts and filler-KV transplant** (`p06/stage2.py`, `run.sh`, `p06.readers_analysis`, `p06.f3_extra`).
- **Setup:** 150 depth-1 items of Brauer's task, Gemma 4 31B-it @ 842da379 in bf16. The pod smoke passed: lens-vs-model top-1 agreement 1.0, the self-transplant changed 0 outputs.
- **Local accuracy:** base 0.213, pre100 0.233, dots100 0.327. That is +11.3 points over base, against +13.3 over the API on the same items, so it passes the "≥ half the API uplift" check.
- **Transplant (filler K/V-projection intervention during prefill).** The donor is the target with its base literal x changed (identical length; only those 1–2 tokens differ). Hooks on every layer's k/v projections write the donor's values at the 100 filler positions during the target's prefill, and the output is decoded greedily.
  - This is not identical to splicing a completed cache. Later target positions attend to the injected values, so their own cache entries change too. That makes comparisons with Brauer's cache-splice numbers approximate.

| n = 150 pairs | matched donor | random donor |
|---|---|---|
| output changed | 91.3% [86.7, 95.3] | 74.0% [66.7, 80.7] |
| output = donor's answer | 10.0% [5.3, 15.3] (base rate 0) | 0% |
| output = donor's own output | 21.3% [15.3, 28.0] | |
| output closer to donor's answer than target's | **52.0%** [44.0, 60.0] | 7.3% [3.3, 12.0] (vs its own donor's answer) |
| same, scored against the *matched* donor's answer (placebo) | 52.0% | random-donor output 5.3% [2.0, 9.3]; un-transplanted output 2.0% [0.0, 4.7] |
| matched − placebo | | +46.7 [+38.7, +54.7] (vs random); +50.0 [+42.0, +58.0] (vs un-transplanted) |
| both-correct pairs (n = 19): output = donor's answer | 36.8% [15.8, 57.9] | 0% |

- **Brauer et al.'s numbers,** on DeepSeek V3 for the same task ([Brauer et al. 2026](#brauer-verdun-and-marks-2026), §4.3): answer changed 34.7%, donor's answer 6.4%.
- **Readouts (corrected after review).** The statistic is first digit only. For each item and digit, take the maximum mean-subtracted log-prob over layers 30–59 and the positions in a group. An item is a hit when its value's true first digit is the top of the 10 digits.
  - **Null:** the role's values permuted across items (keeps digit frequencies), 2,000 permutations, seed 0.
  - **Source:** `p06.f3_sensitivity` → `results/stage2/f3_sensitivity.json`. Probability residuals (Brauer's choice) give the same pattern for the logit lens and J-Lens; J++ is weaker in the filler span.
  - **Hit rate vs null mean** (dots100, n = 150):

| value | location | logit lens | J-Lens | J++ |
|---|---|---|---|---|
| **y (hidden)** | `Filler:` label | **36.0%** vs 12.9% | **34.0%** vs 13.7% | **37.3%** vs 11.9% |
| **y (hidden)** | the 100 dots | **24.7%** vs 10.6% | 12.0% vs 8.2% (p = 0.04) | **20.0%** vs 7.5% |
| y, items with a unique first digit (n = 63) | label / dots | 49% / 37% (null 13% / 12%) | 41% / 16% | 48% / 30% |
| x (written in the prompt) | label / dots | 8.0% / 11.3% (null 11% / 11%) | 6.0% / 9.3% | 7.3% / 5.3% |
| c1·x (hidden) | label / dots | 19.3% vs 12.9% (p = 0.009) / 10.0% | 16.7% / 5.3% | 18.0% vs 11.7% (p = 0.006) / 7.3% |
| y on uplift items (n = 25) vs the rest, at the label | | 40% vs 35% | 40% vs 33% | 44% vs 36% |

  Bold cells have p ≤ 0.001.
  - **c1·x:** its weak hits at the label may be y's. y = c1·x ± a constant up to 50, so their first digits often coincide.
  - **The 12 template positions after the dots:** y is not read there (1–5%), while the answer's first digit is (54–82%). The original analysis maxed over all 114 positions including these, and its null drew only values with a different first digit. That null isn't exchangeable, and the result was "no recovery". It is superseded.
- **Caveats.**
  1. A first-digit readout is coarse. Several roles share first digits; the unique-digit subset addresses this for y.
  2. The transplant can't tell "y computed in the filler" from "x relayed through the filler and combined later".
  3. Only 19 both-correct pairs.
  4. The J-Lens and J++ were fit on the base model, not the -it model.

## Random raw samples

All rows drawn are shown: `uv run python -m p06.note_samples` (populations, order and seed 0 are in its docstring). Item texts aren't printed, because the fact_hops items are sealed.

**A. Stage 1 Gemma chain** (8 of 4,200)

| item | arm | draw | raw reply | gold | correct | witness |
|---|---|---|---|---|---|---|
| `chain:k7:32` | none | 1 | 'Answer: 11' | 1 | 0 | rtok 0, ok |
| `chain:k7:77` | after | 1 | 'Answer: 14' | 13 | 0 | rtok 0, ok |
| `chain:k2:5` | after | 1 | 'Answer: 20' | 20 | 1 | rtok 0, ok |
| `chain:k5:58` | before | 1 | 'Answer: 11' | 2 | 0 | rtok 0, ok |
| `chain:k8:99` | after | 0 | 'Answer: 12' | 20 | 0 | rtok 0, ok |
| `chain:k8:67` | before | 0 | 'Answer: 10' | 10 | 1 | rtok 0, ok |
| `chain:k7:57` | none | 1 | 'Answer: 5' | 5 | 1 | rtok 0, ok |
| `chain:k6:22` | after | 0 | 'Answer: 11' | 6 | 0 | rtok 0, ok |

**B. F1 V4 Flash, Brauer's task** (6 of 2,000)

| item | arm | raw reply | gold | correct |
|---|---|---|---|---|
| `varbind:48` | dots100 | '724' | 574 | 0 |
| `varbind:278` | base | '457' | 457 | 1 |
| `varbind:44` | base | '433' | 433 | 1 |
| `varbind:5` | pre100 | '56' | 92 | 0 |
| `varbind:294` | dots100 | '295' | 295 | 1 |
| `varbind:118` | dots500 | '384' | 384 | 1 |

**C. F6 V4 Flash, chain k = 2** (6 of 600)

| item | arm | draw | raw reply | gold | correct |
|---|---|---|---|---|---|
| `chain:k2:69` | pre500 | 0 | '20' | 11 | 0 |
| `chain:k2:74` | pre500 | 0 | '18' | 18 | 1 |
| `chain:k2:15` | pre500 | 1 | '4' | 4 | 1 |
| `chain:k2:4` | base | 1 | '18' | 8 | 0 |
| `chain:k2:89` | base | 1 | '10' | 20 | 0 |
| `chain:k2:84` | pre500 | 1 | '6' | 6 | 1 |

All B and C rows are witness-clean (rtok 0).

**D. F3 transplants** (6 of 150)

| item | gold | target out | donor x → donor gold | donor out | transplanted out | random-donor transplant out |
|---|---|---|---|---|---|---|
| `varbind:52` | 82 | 64 | x=51 → 310 | 322 | 124 | 64 |
| `varbind:60` | 334 | 334 | x=69 → 490 | 430 | 334 | 334 |
| `varbind:107` | 537 | 537 | x=17 → 33 | 33 | 75 | 561 |
| `varbind:23` | 234 | 234 | x=49 → 354 | 354 | 348 | 234 |
| `varbind:81` | 182 | 166 | x=36 → 98 | 104 | 102 | 136 |
| `varbind:76` | 198 | 192 | x=34 → 207 | 207 | 204 | 192 |

In four of six draws the transplanted output lands near the donor's answer (348 vs 354; 102 vs 98; 204 vs 207; 75 vs 33). *Inference:* the filler carries an approximate magnitude rather than an exact value.

**E. F8 filler patches (V4 Flash)** (6 of 200, seed 0)

| item | gold | target out | donor start → donor gold | donor out | matched patch out | random donor gold | random patch out |
|---|---|---|---|---|---|---|---|
| `chain:k2:98` | 9 | 9 | 2 → 8 | 8 | 3 | 12 | 6 |
| `chain:k3:94` | 16 | 12 | 11 → 20 | 6 | 14 | 18 | 8 |
| `chain:k3:15` | 20 | 12 | 20 → 10 | 10 | 10 | 16 | 10 |
| `chain:k2:18` | 12 | 6 | 14 → 14 | 6 | 6 | 18 | 5 |
| `chain:k2:69` | 11 | 11 | 2 → 8 | 8 | 8 | 11 | 11 |
| `chain:k3:36` | 20 | 7 | 4 → 2 | 5 | 5 | 8 | 8 |

**F. F8 varbind dots100, J-Lens first top-1 (layer, dot)** (6 of 100, seed 0)

| item | output | gold | x | c1x | y | c2y | answer |
|---|---|---|---|---|---|---|---|
| `varbind:32` | 744 | 756 | 92: (22, 28) | 276: (32, 15) | 246: (30, 14) | 738: not top-1 | 756: not top-1 |
| `varbind:97` | 19 | 19 | 20: (22, 9) | 60: (28, 9) | 14: (30, 4) | 42: (31, 21) | 19: (37, 71) |
| `varbind:38` | 172 | 172 | 42: (22, 2) | 84: (33, 79) | 56: (27, 4) | 168: (31, 20) | 172: (34, 30) |
| `varbind:106` | 652 | 652 | 76: (22, 28) | 228: (31, 79) | 217: (31, 15) | 651: not top-1 | 652: not top-1 |
| `varbind:142` | 89 | 89 | 13: (22, 15) | 39: (22, 50) | 33: (28, 60) | 99: (35, 39) | 89: (30, 6) |
| `varbind:56` | 119 | 119 | 26: (22, 26) | 52: (28, 13) | 59: (28, 14) | 118: (35, 21) | 119: (34, 20) |

In E, the matched patch moves the output to the donor's own output in four of six draws (`k3:15`, `k2:18`, `k2:69`, `k3:36`). In F, x (written in the prompt) shows up first, at layer 22, and y first appears at layers 27–31. On two items (`varbind:32`, `varbind:106`, both with 3-digit answers), neither 2y nor the answer is ever top-1, yet the model answers `varbind:106` correctly.

## Deviations from the plan

| Plan | What ran | Why |
|---|---|---|
| "500 dots" | 500 **space-separated** dots | Contiguous dots merge into 32 tokens on Gemma and 6 on V4 Flash |
| nocot recipe, chain 1-shot | Alternate recipe: 10 chain shots (k = 2–4) | Both models failed the k = 2 ≥ 70% smoke gate 1-shot (Gemma 0.61, V4 Flash 0.17). The plan allows one alternate. Gemma passed (0.72); V4 Flash failed (0.39) and was dropped from chain and MC |
| V4 Flash on chain and MC | V4 Flash on fact_hops only in Stage 1 | The gate rule above. Its chain behaviour was measured later in Brauer's format (F5, F6) |
| Filler after the question (target turn) | Filler in every user turn, shots included | A placement pilot leaned that way (within noise), and it is Brauer's and nocot's placement |
| Prefill `Answer:` on Gemma | The Novita and Crusoe hosts ignored the prefill: they close the turn and open a new model turn | Found from prompt-token counts on 150 rows. Witnesses still clean. Stage 2 rendered prompts the same way |
| Gemma pinned to Novita | Novita, then Crusoe (both bf16) | Novita returned upstream 429s on about 25% of calls |
| 10 samples at t = 1 to select Stage 2 items | Skipped | The Stage 2 gate failed on chain; selection only fed Stage 2 |
| Stage 2 on 60 chain uplift and 60 control items | F3 on 150 items of Brauer's task, plus a transplant | Chain had no uplift; Brauer's task did. On Gemma the readout scores the first digit only |
| Plan's recovery criterion (max over cells, matched null at 5% FPR) | A frequency-preserving permutation test on top-digit hit rates, by position group | Review r1: the original null was not exchangeable and its threshold was set on the same sample |
| Pod smoke checks local uplift ≥ half the API uplift | Checked after the bulk run (passes) | The smoke gated agreement, self-transplant and time only (review r1) |
| Cap $35 (API $10) | $45 (API $20) | Every-turn filler costs about 5× the tokens. Final spend $13.00 |
| V4 Flash on GPU: 4–8 H100s and a `gpu_count` runner field (plan estimate $60–130) | One B300 (288 GB) with the shipped FP8/FP4 weights in transformers 5.17; the only shared change is an additive `[pod].gpus` list of GPU types and rates | Fits on one GPU, so no multi-GPU runner change. F7 piloted it first |
| F7 in one attempt | Three attempts ($15.26) | Our offline snapshot lookup lacked the download's ignore filter ($2.18), then a slow host hit an HF 504 with no retry ($7.95). Both fixed with regression tests |
| F8 (A) serial order across positions on chain k = 3 | Not testable (6–10 usable items). Exploratory first-hit layer order on varbind and chain k = 2, using the full-vocabulary top-1 (`p06.v4_extra`) | Chain k = 3 step 2 is barely read. The 21-integer candidate scoring saturates for chain values across 500 dots |
| Cap $45 | $75 (GPU $55) | Raised $45 → $60 for F7's final attempt and → $75 for F8's reservation; logged before each run |
| Plan says "no V4 Flash NLA found" | One exists, `ceselder/easynla-dsv4-flash-nla-ckpts`, but for the later `DeepSeek-V4-Flash-0731` checkpoint, not our pin | Literature subagent, HF API |

## Spend and wall-clock

All spend went through `emx.spend` (`logs/costs.jsonl`, exp P06). **Total $42.05: API $11.52, GPU $30.52** ($1.48 for F3, $15.26 for F7, $13.79 for F8).

| Phase (UTC, 2026-10-11) | Wall-clock | Spend |
|---|---|---|
| Build, smokes, placement pilot (≈ 17:40–18:17) | ≈ 37 min | $0.73 |
| Stage 1 bulk, Gemma (18:17–19:00) and V4 Flash fact_hops | 43 min (Gemma upstream rate limit, ~3 calls/s) | $5.81 |
| F1 Brauer's task, 2 models (≈ 19:02–19:08) | 6 min | $0.61 |
| F2 depth and MC, 2 models (≈ 19:12–19:24) | 12 min | $1.82 |
| F4 Gemma 3 (≈ 19:27) | 2 min | $0.40 |
| F5 and F6, chain in Brauer's format (≈ 19:30–19:48) | 18 min | $2.15 |
| F3 pod (19:39 up – 20:02 terminated): download 6.3 min, load 0.4, smoke 0.3, bulk 10.7, pull and teardown about 3 | 0.37 h billed | $1.48 |

| F7 pilot, attempt 1 (2026-10-11 23:03–23:18) | 0.24 h billed | $2.18 |
| F7 pilot, attempt 2 (23:22–00:13) | 0.88 h billed | $7.95 |
| F7 pilot, attempt 3 (2026-10-12 00:13–00:48): download 8.5 min, load 0.7, smoke 3.9, prompts and uplift ≈ 15 | 0.57 h billed | $5.13 |
| F8 bulk (≈ 01:13–02:45): download 8.4 min, lenses 0.6, load 0.6, compile 3.9, smoke 4.4, chain readouts and patching 54, varbind 9, pull and teardown ≈ 6 | 1.53 h billed | $13.79 |

The F3 pod's GPU sat idle only during the download and model load (about 7 min). The F8 pod spent about 18 min of its 92 on download, compile and smoke; the bulk ran in 64 min against a 68.7 min projection. Attempts 1 and 2 of F7 ($10.13) bought only the two fixes.

**Throughput lesson (review r1).** Gemma's second temperature-0 draw matched the first in correctness on 98.5% of Stage 1 pairs and 98.7% of F6 pairs. The second draws cost about $3.70 and roughly 25 min of wall-clock. A small repeatability sample would have been enough, and the rest spent on items or controls.

## Open questions and next steps

- **For Neel.** The NLA he mentioned for V4 Flash is presumably `ceselder/easynla-dsv4-flash-nla-ckpts`, built on `DeepSeek-V4-Flash-0731`. Was that the one? We'd also need to know which checkpoint OpenRouter serves as `deepseek/deepseek-v4-flash` before pairing its API uplift with that reader.
- **V4 Flash GPU (done, F7–F8).** Readers see the filler's computation as whole values, in layer order, and the answer is read from the filler. **What is still open:**
  - *Where the answer is read from:* patch subsets of layers (e.g. only ≤ 30, only > 30) or only the first 20 dots, to test the "one extra pass of depth" inference. About 30 min on the same pod setup, roughly $8–10.
  - *Parallel candidates:* a clean MC test needs controls matched to the distractors' construction (e.g. near-answer numbers that are not listed), not random same-digit numbers.
  - *The NLA:* `ceselder/easynla-dsv4-flash-nla-ckpts` is for the 0731 checkpoint, while OpenRouter's `deepseek/deepseek-v4-flash` and our pin are the April weights (F7 log). Running it would need the 0731 model and a re-measured uplift on it.
- **Cheaper alternatives (Gemma):**
  - A Gemma readout that scores whole multi-digit values (teacher-forced two-token scoring, or trained probes on the filler residuals). Baherwani et al. report that only probes pooled over the whole filler span work (abstract-level read, unverified).
  - A transplant variant that holds x fixed and changes only c1, to separate "x relayed" from "y computed".

## Review

One gpt-6-astra round of the whole branch (`logs/reviews/20261011-P06-r1.md`, prompt `logs/reviews/20261011-P06-review-prompt.md`). **Verdict: BLOCKING ISSUES (3), plus 8 MINOR and 1 NIT.** The reviewer recomputed the Stage 1, F1, F2, F5, F6 and transplant tables, the seeded samples and the ledger totals from the committed files, and all reproduced. All three BLOCKING findings were fixed. Per CLAUDE.md, a second round runs only when a fix changes the headline number. The reader conclusion did change, but the fix *is* the reviewer's requested reanalysis, so no second round was run. Jacob may want one on `p06/f3_sensitivity.py`.

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1 | Random-donor "toward donor" control not fair (different arithmetic, different answer distances) | BLOCKING | Fixed. Added placebos scored against the *matched* donor's answer (un-transplanted output 2.0%, random-donor output 5.3% vs matched 52.0%), and dropped the "fair baseline" reading of the original contrast |
| 2 | Reader null not exchangeable; threshold set on the same sample; log-prob vs Brauer's probability residuals; uplift-vs-control not tested; suffix included | BLOCKING | Fixed by reanalysis (`p06.f3_sensitivity`): a permutation null, position groups, probability residuals, uplift items vs the rest, and a unique-digit subset. **The conclusion flipped**: y is readable. Docstring attribution corrected |
| 3 | "Buys two to three serial steps" overstates the depth result | BLOCKING | Fixed: "detectable uplift at k = 2–3; no consistent evidence at k = 4–8" |
| 4 | After and before arms in Brauer's format differ by 6 tokens (`Filler:` vs `Padding:`) | MINOR | Noted in F1. Accepted as a limitation; too small to explain +25 points |
| 5 | Option count also changes distractor construction and headroom | MINOR | Qualified in the answer (item 4) |
| 6 | K/V-projection hooks ≠ a completed-cache splice | MINOR | Renamed and qualified in F3 |
| 7 | Stage 2 resume could bypass a failed smoke; an incomplete bulk returned 0 | MINOR | Fixed in `p06/stage2.py` (persisted smoke must have passed; an incomplete bulk returns rc 4), with a test |
| 8 | Local-vs-API uplift check was retrospective, not in the smoke | MINOR | Recorded in Deviations |
| 9 | F5 and F6 prediction scoring | MINOR | Fixed: F5 inconclusive, F6 mixed |
| 10 | Stale spend and prefill notes in `log.md` | MINOR | Fixed in place, marked "corrected at review r1" |
| 11 | Second temperature-0 draws were almost redundant (≈ $3.70) | MINOR | Recorded as a throughput lesson |
| 12 | Bootstrap counts; note files untracked | NIT | Fixed |

## References

#### nocot-bench
Neel Nanda et al., *nocot-bench* (2026), https://github.com/neelnanda-io/nocot-bench, @ 634d7de5. `papers/neel_brainstorm/INDEX.md` row 7. The recipe, witnesses, chain generator and the sealed fact_hops items.

#### Brauer, Verdun and Marks 2026
Kaley Brauer, Claudio Mayrink Verdun, Samuel Marks, "Reading Between the Dots: Decoding Hidden Computation across Filler Tokens", arXiv 2607.03502 (2026), with code at github.com/kaleybrauer/filler-token-reasoning @ a701927. `papers/neel_brainstorm/INDEX.md` row 69 (read in full).

#### Greenblatt 2025
Ryan Greenblatt, "Recent LLMs can use filler tokens or problem repeats to improve (no-CoT) math performance" (LessWrong, 2025). Row 70, verbatim.

#### Greenblatt 2026
Ryan Greenblatt, "Recent LLMs can do 2-hop and 3-hop latent (no-CoT) reasoning on natural facts" (LessWrong, 2026). Row 71, verbatim.

#### Xu, Prasanna and Westover 2026
Dylan Xu, Sebastian Prasanna, Alek Westover, "Astra is much better at reasoning with filler tokens than previous models" (LessWrong, 2026). Row 72, verbatim. This is the plan's "Xu et al.": DeepSeek V3.2 shows no filler trend.

#### Baherwani, Goldstein and Panda 2026
"Not All LLM Reasoning is Visible in the Chain-of-Thought", arXiv 2607.22925. Abstract only, not captured in `papers/` (see `lit-review.md` §2).
