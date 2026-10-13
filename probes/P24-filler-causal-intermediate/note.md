# P24 note: does Gemma 4 31B's filler carry the hidden intermediate y, beyond copied inputs?

Probe tier, 2026-10-12/13. Plan: [`plan.md`](plan.md) (approved by Jacob in chat). Run log: [`log.md`](log.md). Literature: [`lit-review.md`](lit-review.md). Spend **9.86** of a $30 cap: $8.43 for the main run (one H100, 2.11 h) and 1.43 for follow-up F2. All of it is GPU; no API calls.

## The answer

1. **The filler's causal effect is not a copy of x.** Same setup as P06 F3: Gemma 4 31B-it, Brauer et al.'s system-of-equations task, 100 dots. The donor's filler K/V is written into the target's filler at every layer.
   - **Holding x fixed** and changing only y's offset (donor Y) moves the answer toward the donor's answer **as much as changing x does**. Effect over the random-donor placebo: **Y +44.1 points [+38.7, +49.4]**, X +44.8 [+39.5, +50.4]. Ratio Y/X **0.98 [0.87, 1.11]**. n = 413 items.
   - **Holding y fixed** while changing both x and the offset (donor S) moves nothing toward the answer a copied x would give: **S −6.5 [−10.4, −2.7]**.
   - **The decisive contrast:** Y − S = **+50.6 [+45.5, +55.7]**.
2. **Changing only the operator word moves it too.** Flipping "plus" ↔ "minus" in y's definition changes no number token. That donor (F) moves the answer toward its own answer by **+43.1 [+37.9, +48.1]** (n = 401). Changing "three times" to "four times" (C, n = 207): +51.2 [+44.0, +58.0].
   - So no account where the filler relays copied digits explains the effect.
3. **P06's result replicates on this setup.** P06's own donor type (P: x replaced by any other 2-digit value) gives **+46.2 [+41.4, +51.1]**. P06 reported +46.7 [+38.7, +54.7] for the same contrast (matched minus random donor, n = 150). The smaller, matched shifts used here (X) give the same size, P − X +1.5 [−5.3, +8.2].
4. **What this does not show.** All of the above is equally predicted by a filler that relays y's whole definition (x, coefficient, operator, offset) for the answer position to evaluate. Answer-level tests cannot separate that from a filler holding the computed y (plan, Notes).
   - One exploratory readout (F1, below) bears on it: on P06's readouts, y's first digit is readable at the label and the dots, while neither x nor the offset is above its null. So there is no sign of a *readable* relay. An unreadable one isn't excluded.
   - **The filler also carries something besides y.** The same-y donor still changes the output on 68% of items (random donor 71%, self-patch 1%). On items the target gets right, it makes 42% wrong (random donor 56%). It perturbs without steering.
5. **Where it lives.** The effect is in the 100 dots.
   - The `Filler:` label alone does nothing: every donor within ±1.2 points of the placebo. That is despite y's first digit being *most* readable there (P06 F3).
   - The first 20 dots carry a small part: X +10.7 [+7.3, +14.3], Y +6.0 [+3.4, +8.7], S +1.9 [−0.7, +4.6].
   - Label plus dots equals the dots alone (Y +42.4, S −8.2, Y − S +50.6).
   - *Inference:* where y is most readable is not where the answer reads it from.

**Prereg verdict (labelled, not the headline):** outcome (c), "x-relay rejected". (a) X replicates, (b) Y moves, (e) F moves, and the first-20-dots half of (f) all held; (d), the copy pattern, did not. Detail under "Predictions against outcomes".

**What it changes.** P06's transplant can now be cited as "the filler carries information about y's definition beyond x, and the answer reads it there", not as "the filler carries copied x". It does not license "the filler computes y". So, as the plan says, it is not evidence for P17 going ahead; P17 stands on its own approval. For P17's design the location result matters: the label carries readable y but no causal weight, so a penalty on label readouts alone would be attacking a position the answer doesn't use. The causal weight is in the dots.

## Design in one paragraph

For each of 413 items (of Brauer's 500 depth-1 items, those where all three matched donors exist), donors were built from a seeded shift dx (seed 20261012): X (x' = x + dx), Y (x fixed, offset changed so y_Y = y_X), S (x' = x + dx and the offset changed so y is unchanged), F (operator flipped), C (coefficient 3 → 4, c1 = 3 items only), P (P06's donor), and R (a random other item). X, Y and S share one alternative answer a'. Under "filler carries y" X and Y move toward a' and S does not; under "filler carries a copy of x", X and S move toward a' (S's x' through the prompt's own offset gives y_X) and Y does not. Each donor was decoded with hooks capturing every layer's k_proj/v_proj outputs at the target turn's label and 100 dots; then the target was decoded greedily with those outputs written in at one site (dots, label, first 20 dots, or label + dots). **Measure:** the share of items whose patched output is strictly closer to a'_D than to the target's answer, minus the same share for the R-patched output scored against the same a'_D; item bootstrap, 10,000 resamples. Code: `p24/donors.py`, `p24/pod.py`, `p24/analyze.py`; data `results/rows.jsonl` (dots) and `results/rows_sites.jsonl` (other sites); numbers in `results/analysis.json`.

## Results

**Primary site, the 100 dots** (`results/analysis.json`, `all/<donor>/dots`). Effect = toward − R placebo, in points, 95% item-bootstrap interval.

| donor | n | toward a'_D | R placebo | unpatched | **effect** | exact a'_D − R | output changed (R) | first-digit log-prob shift (nats) |
|---|---|---|---|---|---|---|---|---|
| X: x changed | 413 | 63.4 | 18.6 | 12.6 | **+44.8 [+39.5, +50.4]** | +27.6 | 91.8 (71.2) | +12.1 |
| Y: offset changed, x fixed | 413 | 62.7 | 18.6 | 12.6 | **+44.1 [+38.7, +49.4]** | +20.8 | 87.2 (71.2) | +11.0 |
| S: x and offset changed, y fixed | 413 | 12.1 | 18.6 | 12.6 | **−6.5 [−10.4, −2.7]** | +0.2 | 67.8 (71.2) | +0.1 |
| F: operator flipped | 401 | 47.4 | 4.2 | 2.2 | **+43.1 [+37.9, +48.1]** | +10.0 | 94.8 (71.6) | +14.4 |
| C: 3× → 4× | 207 | 54.1 | 2.9 | 1.0 | **+51.2 [+44.0, +58.0]** | +15.9 | 91.3 (71.5) | +15.3 |
| P: P06's donor | 413 | 50.6 | 4.4 | 3.1 | **+46.2 [+41.4, +51.1]** | +14.8 | 91.8 (71.2) | +14.8 |

Contrasts (paired, dots): Y − S **+50.6 [+45.5, +55.7]**; F − S +49.6 [+43.4, +55.9]; X − S +51.3 [+46.0, +56.4]; X − Y +0.7 [−4.6, +5.8]; P − X +1.5 [−5.3, +8.2]; Y/X 0.98 [0.87, 1.11].

The R placebo is higher for X, Y and S (18.6) than for F, C and P (3–4) because their a' lies closer to the answer (median \|a' − answer\| 30), so random outputs land nearer it by chance. The placebo is scored against the same a', so it absorbs that.

**S's negative effect.** S sits 6.5 points *below* the placebo. On the toward-a' test, the S patch leaves the output nearer the target's answer than a random donor does. That fits S carrying the target's own y. Its first-digit log-prob shift toward a' is +0.1 nats [−0.7, +1.0], against +11 to +15 for every y-changing donor.

**Both-correct subset** (the unpatched target and the donor both right; dots): X +62.2 [+50.0, +74.3] (n 74), Y +61.4 [+50.0, +72.9] (n 70), S −8.0 [−14.7, −2.7] (n 75), F +34.0 [+18.0, +50.0] (n 50), C +50.0 [+34.2, +65.8] (n 38), P +54.8 [+41.9, +67.7] (n 62). Exact donor answers: X 58%, Y 43%, P 40%, S 0%.

**Other sites** (`all/<donor>/<site>`):

| site | X | Y | S | F | P | Y − S |
|---|---|---|---|---|---|---|
| label (2 tokens) | +0.0 [−1.7, +1.7] | +0.5 [−1.2, +2.2] | +1.2 [−1.0, +3.4] | +0.2 | +0.0 | −0.7 [−2.7, +1.2] |
| first 20 dots | **+10.7 [+7.3, +14.3]** | +6.0 [+3.4, +8.7] | +1.9 [−0.7, +4.6] | +3.0 | +7.3 | +4.1 [+1.2, +7.3] |
| dots (100) | +44.8 | +44.1 | −6.5 | +43.1 | +46.2 | +50.6 |
| label + dots | +44.1 [+38.5, +49.6] | +42.4 [+37.0, +47.7] | −8.2 [−12.6, −4.1] | +46.4 | +49.1 | +50.6 [+45.3, +55.7] |

In the first 20 dots X moves the answer more than Y (X − Y +4.6 [+1.0, +8.2]). At the full dots they are equal. *Inference, not tested:* early dots may hold more x-specific content, and y's content accumulates later.

**The same-y donor perturbs without steering** (pass-1 rows; `log.md` 23:23). Output changed: S 67.8%, R 71.2%, X 91.8%, Y 87.2%, self-patch 1.2%. On the 144 items the unpatched target gets right, S makes 42.4% wrong (R 56.2%, X 90.3%, Y 86.1%). Median \|change\| when changed: S 9, R 12, X 22, Y 20.

## Predictions against outcomes

| prediction (plan) | p | outcome |
|---|---|---|
| (a) X − placebo ≥ 20 points | 0.85 | **True:** +44.8 [+39.5, +50.4] |
| (b) Y − placebo ≥ 10, CI excluding 0 | 0.65 | **True:** +44.1 [+38.7, +49.4] |
| (c) (b), and S ≤ 5 with CI upper ≤ 10, and Y − S ≥ 10 with CI excluding 0 ("x-relay rejected") | 0.45 | **True:** S −6.5 [−10.4, −2.7]; Y − S +50.6 [+45.5, +55.7] |
| (d) copy pattern: Y < 5 and S ≥ 10 | 0.15 | False |
| (e) F ≥ 10 | 0.6 | **True:** +43.1 [+37.9, +48.1] |
| (f) label alone moves X or Y ≥ 10 | 0.3 | False: X +0.0, Y +0.5 |
| (f) first 20 dots alone moves X or Y ≥ 10 | 0.25 | True on the point estimate: X +10.7 [+7.3, +14.3] (the CI straddles 10); Y +6.0 |

The y-pattern came out more cleanly than predicted: Y matched X, rather than being a weaker version of it.

## Follow-ups (exploratory)

**F1. Is y's definition readable at the filler?** ($0; laptop; `p24/offset_readout.py`, `results/followups/f1_offset_readout.json`.) P06 F3's committed statistic (first-digit hit rate, layers ≥ 30, permutation null with 2,000 permutations), run on P06's 150 readout files for y's offset and the question's constant k2, beside x and y.
- y reproduces P06: at the label, 0.360 / 0.340 / 0.373 for logit lens / J-Lens / J++, nulls 0.12–0.14, p 0.0005.
- **The offset is not above its null for any reader at the label or the dots:** hit 0.09–0.14, nulls 0.08–0.14, p 0.18–0.85. On the 91 items whose offset's first digit differs from x's, y's and the answer's, p 0.61–0.998. x is not either.
- k2 is marginal in 2 of 6 reader-group cells (J-Lens filler p 0.064, J++ filler p 0.047; 24 tests, no correction).
- Prediction (offset not readable, 0.6): held.
- Caveat (p17, 2026-10-13): late-layer readouts on this stack differ between equivalent computations by up to ~1 nat on average, and aggregate hit rates move a few points between paths. A null here rules out a strong readable signal, not a weak one.

**F2. Do the effects depend on the attention implementation?** (A second pod, 65 items, dots, eager attention; `results/f2_eager/`.) Same code, `attn_implementation="eager"` instead of sdpa, the first 65 eligible items, dots site (`p24/f2_compare.py` → `results/followups/f2_compare.json`). The smoke's numerics gate again chose batch 1 (batched vs sequential agreement 95.1%).
- **Individual outputs do not reproduce across attention implementations.** The same row gives the same text under eager and sdpa on only **72.5%** of patched rows (unpatched 86.2%; donors' own outputs 72–83%). This matches p17's finding that late-layer values on this stack drift between equivalent computations (log.md).
- **The effects do reproduce.** Effect over placebo, eager vs sdpa on the same 65 items: X +52.3 vs +49.2, Y +56.9 vs +53.8, S +0.0 vs −3.1, F +51.6 vs +51.6, P +47.7 vs +47.7, C +48.4 vs +38.7 (n 31). **Y − S: +56.9 [+44.6, +69.2] under eager, +56.9 [+43.1, +70.8] under sdpa; paired difference +0.0 [−6.2, +6.2].**
- Eager's self-patch equalled the unpatched output on 65/65 items (sdpa 64/65).
- Prediction (Y − S within 10 points of sdpa *and* ≥ 85% row agreement, 0.75): **half held**. The contrast held; row agreement was 72.5%.
- *Reading:* single patched outputs are within the numerical noise of the path, so no single item should be read as evidence. The 40–50-point aggregate effects are not an artefact of sdpa.

## Numerics

- **The batched path failed its gate and was not used.** In the smoke (5 items), batched patched rows agreed with the sequential path, and with the reversed batch, on 97.6% of rows, below the 98% gate. So the run used batch 1 throughout, as the plan's rule says (plan review r1, BLOCKING 2).
- **The noise floor on the path used:** the self-patch (the target's own K/V written back) differs from the unpatched output on 5 of 413 items (1.2%; e.g. 384 vs 381). Pass 2's re-decoded donors differ from pass 1's on a few items too.
- p17 found that on this stack any change of matrix shape (batching, cache split, sdpa vs eager) moves late-layer values a lot. P24's patched and unpatched rows have identical shapes (batch 1, same prompt length), so this ~1% floor is the relevant one for the paired comparisons. Across attention paths the floor is much larger: 28% of patched outputs differ (F2). The aggregate effects are unchanged.

## Random raw samples

Ten items drawn by `uv run python -m p24.samples 10` (seed 20261012, uniform over the 413 pass-1 rows; every drawn row shown). Outputs at the dots site; a' is shared by X, Y and S. F's own a' is in parentheses.

| idx | y's definition (target) | answer | a' (X/Y/S) | unpatched | X | Y | S | F (a'_F) | R |
|---|---|---|---|---|---|---|---|---|---|
| 0 | lef = 58; y = twice lef minus 23 = 93 | 183 | 159 (dx -6) | 187 | 179 | 179 | 179 | 231 (275) | 179 |
| 5 | kaz = 15; y = twice kaz plus 16 = 46 | 92 | 74 (dx -3) | 86 | 74 | 74 | 86 | — | 86 |
| 25 | jix = 66; y = three times jix plus 13 = 211 | 407 | 401 (dx -1) | 403 | 385 | 403 | 407 | 371 (355) | 387 |
| 31 | ruc = 25; y = twice ruc plus 21 = 71 | 184 | 204 (dx 5) | 144 | 144 | 144 | 164 | 130 (100) | 163 |
| 252 | guv = 59; y = three times guv plus 22 = 199 | 572 | 608 (dx 4) | 560 | 608 | 578 | 572 | 512 (440) | 548 |
| 324 | jex = 48; y = three times jex minus 4 = 140 | 425 | 416 (dx -1) | 425 | 413 | 413 | 428 | 449 (449) | 437 |
| 343 | loq = 69; y = three times loq plus 27 = 234 | 464 | 440 (dx -4) | 460 | 460 | 436 | 482 | 360 (356) | 464 |
| 415 | teb = 34; y = twice teb plus 31 = 99 | 158 | 182 (dx 6) | 157 | 174 | 157 | 158 | 75 (34) | 155 |
| 471 | yur = 71; y = twice yur plus 12 = 154 | 343 | 347 (dx 1) | 327 | 347 | 347 | 339 | 319 (295) | 339 |
| 490 | naj = 37; y = three times naj plus 19 = 130 | 240 | 252 (dx 2) | 242 | 234 | 254 | 236 | 184 (164) | 204 |

The effect is statistical, not item-by-item. Examples:
- Idx 5 and 471: X and Y land on a' exactly.
- Idx 31: X and Y don't move.
- Idx 0: every patch, R included, gives 179.
- Idx 25: S moves the output to the correct answer.

Several S outputs equal or sit near the true answer (idx 25, 252, 415), as expected when S keeps y.

## Spend and wall-clock

All spend is through `emx.spend` (`logs/costs.jsonl`, exp P24).

| phase | wall-clock (UTC) | cost |
|---|---|---|
| Plan, literature sub-agent, plan review r1, build and tests (laptop) | 22:03–22:26 | $0 |
| Main pod (secure H100, $3.99/h; community H100 and NVL out of stock): boot, push and setup | 22:26–22:30 | |
| download 7.0 min, load 0.4, smoke 1.4 (incl. sequential and reversed validation) | 22:30–22:39 | |
| pass 1, dots, 408 bulk items, 5.75 s/item | 22:39–23:18 | |
| pass 2, three other sites, 413 items, 10.6 s/item | 23:18–00:32 | |
| pull, verify, terminate | 00:32–00:33 | **$8.43** (2.11 h) |
| F1 (laptop) | 23:20 | $0 |
| F2 pod (eager, 65 items) | 00:34–00:56 (download 10.1 min, load 0.3, smoke 1.4, 60 items 5.9 min at 5.9 s) | **1.43** |

- **Idle GPU:** the main pod's GPU sat idle only during the download and load (about 8 min of 127).
- **The numerics gate cost time:** batch 1 ran at 5.75 s/item, against a batched rate that was never measured in bulk.
- **Laptop crash at about 23:00** (log.md): the pod kept running, and the launching session re-adopted it at 23:06. Nothing was lost or redone.

## Deviations from the plan

| plan | what ran | why |
|---|---|---|
| Batched patched rows (8 per call) | Batch 1 throughout | The smoke's numerics gate: 97.6% < 98% agreement (the plan's own rule) |
| Projection ≈ $13, 3.3 h | $8.43, 2.11 h | Batch 1 was faster than projected (5.75 s/item vs ≈ 10) |
| Both-correct subset | Correctness flags from pass 1 only | Pass 2 re-decodes donors; a few flip under nondeterminism (`analyze.merge`) |
| F2 (eager re-run) | Added | p17's finding on Gemma 4 numerics (log.md) |
| Main run's `results/MANIFEST.sha256` | Moved by the F2 pull to `superseded/20261013T005624Z/` (it still verifies every main-run file); `results/MANIFEST.sha256` now covers the F2 files | The runner's promotion rule for a second session |

## Review

__REVIEW__

## References

- Brauer, Verdun & Marks 2026, *Reading Between the Dots: Decoding Hidden Computation across Filler Tokens* (`papers/neel_brainstorm/INDEX.md` row 69; §4.3 and App. C/E read).
- P06 note, item 5 and F3 (`probes/P06-filler-token-computation/note.md`).
- Literature search: [`lit-review.md`](lit-review.md) (Stolfo et al. 2023 and Nikankin et al. 2024 from search summaries only).
