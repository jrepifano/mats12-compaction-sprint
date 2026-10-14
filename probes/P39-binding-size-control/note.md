# P39: At the J-lens reconstruction's size, none of the tested slices moves the answer. The swap's effect is dominated by one order/subject-associated direction that, along that direction, needs about 40% of the twin difference to flip half the answers, and the J-lens change carries no more of it than one randomly rotated dictionary does

**Status:** done, 2026-10-13. **Spend: $5.67** of $15, all GPU, over seven pod sessions:
- the main run, $1.98;
- follow-up F1, $1.36;
- a run stopped at the smoke by a projection bug, $1.57;
- three hosts rejected by the download gate, $0.58;
- a push refused over an uncommitted file, $0.18.

No API spend. Plan: [plan.md](plan.md) (approved by Jacob in chat, 2026-10-13); run log: [log.md](log.md); literature: [lit-review.md](lit-review.md); raw samples: [results/raw_samples.md](results/raw_samples.md); dose curves: [results/dose_curves.png](results/dose_curves.png). Review: see "Review" at the end.

## Answer

**Where and how this was measured.** P36 found that at Qwen3.6-27B's entity tokens at layer 48, swapping the sparse J-lens reconstruction (k = 25) between role-reversed twins flips the answer 0.1% of the time, while swapping the residual flips 95%. The reconstruction's change, δ_J, holds about 6% of the squared twin difference. P39 asks whether any slice that small could move the answer.

Every number below uses:
- the 40 held items: P36's held-entity and held-verb items, 320 cases, with all fitted directions fitted on the other 60 items;
- twin donors, unless a row says otherwise;
- item-cluster bootstrap 95% intervals;
- **share** = ‖δ‖²/‖Δ‖², where δ is the arm's change and Δ the full twin difference. "**Matched**" means δ is rescaled per token to the J-lens change's norm, which is 6.35% share.

1. **Plan verdict: O2, "size-only".** At the J-lens change's size, none of the tested arms moves the answer, whether chosen for the signal or at random:

   | Arm, matched to the J-lens change's size | Flips % [95% CI] | Logit-difference shift % |
   |---|---|---|
   | J-lens k = 25 change (the P36 swap) | 0.0 [0, 0] | 0.0 [−0.2, 0.4] |
   | Random subspace, isotropic (20 draws) | 0.0 [0, 0] (draws 0–0.3) | 0.5 [0.4, 0.7] |
   | Random subspace, covariance-matched (20 draws) | 0.0 [0, 0] | 4.5 [4.2, 4.9] |
   | Rotated J-lens dictionary (J_rot), same pursuit | 0.0 [0, 0] | 0.1 [0.0, 0.2] |
   | Top principal direction of twin differences (PC1; = the mean-difference axis, cos 0.9999) | 0.0 [0, 0] | 4.1 [3.7, 4.4] |
   | Gradient axis, aligned to first mention (S-Space recipe, [Shi et al. 2026](#shi-et-al-2026)) | 2.5 [0.6, 4.7] | 15.5 [13.2, 17.2] |
   | Gradient axis, averaged by role (expected to cancel) | 3.4 [1.6, 5.6] | 1.4 [−1.2, 3.2] |
   | Random 1-D directions (20) | 0.0 [0, 0.1] | 0.0 [−0.1, 0.1] |

   The plan's O1 needed some role-chosen matched arm at ≥ 30% flips; the best reached 2.5%, and every prespecified signal-chosen arm's upper bound is below 10%. **So P36's null swap alone doesn't distinguish a size limitation from poor alignment:** none of the tested signal-chosen slices of that size moved the answer either. This doesn't rule out an untested or optimized subspace (e.g. a trained DAS subspace) doing better at that size. The plan's consequence for O2 stands: **don't pitch the J-lens null swap.**

2. **Why: one order/subject-associated direction dominates these swaps, and along it a large share is needed.** These are statements about the swap interventions at this site, not about what the model's own computation needs.
   - **PC1 dominates the twin difference.** PC1 of the twin differences (fitted on the train items) holds 62% of the held items' squared twin difference, about 80% cosine per token. It perfectly separates first- from second-mentioned entity tokens (AUC 1.0).
   - **Swapping only PC1's component** flips **82.5% [76.6, 87.5]**; the full patch flips 95.6%.
   - **Swapping everything except PC1's component** (38% share) flips **0.6% [0, 1.6]** (follow-up F1).
   - **Along PC1, it needs a large share (F1).** Swapping a fraction α of PC1's component flips 0% up to α = 0.5 (16% share), 0.6% at α = 0.6 (22%), 20% at 0.7 (30%), **48% [40, 56] at 0.8 (40%)**, 71% at 0.9 and 82.5% at 1.0. About half the answers flip at about 40% of the twin difference along this path, six times the J-lens change's share; the first tested point above 50% is α = 0.9 (50% share). This is the threshold along PC1, not a minimum over all directions.
   - **It tracks the mention-order/subject signal P36 found, not thematic role** (first mention and grammatical subject are confounded in these sentences). On P36's cross-voice donors, PC1 alone flips the order-swapped, same-roles donor 83.1% (that donor's full patch: 81.9%). It flips the roles-swapped, order-kept donor 0.0%, and that donor's difference has only 1.4% of its squared norm on PC1.
   - **Random slices need even more.** Isotropic random slices flip ≤ 1.6% even at 50% share (shift ≈ 20%). Covariance-matched random slices find the PC1 region quickly: 25 dimensions already hold 33% of Δ, and at 12.5% of dimensions (88% share) they flip 95%.
3. **The J-lens change also isn't aligned with that direction, about as much as one randomly rotated dictionary's change.**
   - **Plan's J-alignment verdict: "at chance".** First-order attribution, a = Σ g·δ / Σ g·Δ (g is the gradient of the logit difference at the recipient's entity tokens):

     | Arm | a [95% CI] |
     |---|---|
     | J-lens change | −0.002 [−0.014, 0.010] |
     | Isotropic random slices, 20 draws | 2.5th–97.5th percentile −0.0024 to 0.103 (min −0.012, median 0.056; pooled 0.053 [0.051, 0.056]) |
     | J_rot | 0.020 [0.010, 0.030] |

     a_J lies just inside the draws' 2.5th percentile (linear interpolation; the plan's rule), so "at chance". It sits below 19 of 20 draws and below the pooled isotropic interval.
   - **PC1 capture.** Per token, the J-lens change carries a median **3.6%** of the twin difference's PC1 component (IQR 3.1–4.0%). J_rot carries 3.5% (IQR 3.2–3.8%; `results/summary.json`, `pc1_capture`).
   - **Pursuit of the direction itself.** The tested nonnegative pursuit (P36's NNOMP, greedy support selection) recovered at most 4.9% of ±PC1 at k = 25 and 5.7% at k = 100, and at most 3.9% of the order gradient axis. That is a low achieved reconstruction, not an upper bound on what an optimal sparse fit could do.
   - **Amplified, still inert.** Rescaled to the residual change's norm (97% share), the J-lens change flips **0.3% [0, 0.9]** (shift −0.6%), and random slices at that norm flip 0–0.3% (shift 3.8%).
     - **Arithmetic, not a separate test:** amplification scales the PC1 capture by about √(0.97/0.064) ≈ 3.9, so the amplified J change carries about 14% of the twin difference's PC1 component and a random slice about 25%. Both are well below the α ≈ 0.7–0.8 where PC1 swaps begin to flip, consistent with both doing nothing.
4. **A potent direction the twins don't use (exploratory).**
   - The gradient axis is nearly orthogonal to PC1 (cos 0.05), and the twins barely differ along it (0.3% native share).
   - Pushed past the twins' own difference it moves the answer. At 8× its native component (20% share) it flips **30.6% [25.0, 36.2]**. At 16× (81%) it flips 44.4%, and at 32× 44.7%, with 6.9% invalid answers.
   - Its first-order attribution at matched size is a = 2.57, against a logit-difference shift of 15.5%. The two are normalized differently (summed g·Δ vs per-case donor logit contrasts), so they are not compared here as a measure of nonlinearity (review r1, finding 5).
   - This is the pattern Makelov et al. warn about ([Makelov et al. 2023](#makelov-et-al-2023)): a direction whose patch acts without being the one the natural contrast uses. It is not evidence that the natural signal is small.

**What it changes:**
- **P36's headline needs rewording.** It can't be pitched as "the J-lens reconstruction leaves out the role signal" *from the swap alone*: none of the tested signal-chosen slices of the reconstruction's size moved the answer either. Two descriptive statements remain, both specific to this site and these interventions:
  1. the swaps' effect is dominated by one high-variance order/subject-associated direction (PC1); along it, about 40% of the twin difference is needed to flip about half the answers;
  2. the J-lens change carries about as little of that direction as one rotated dictionary's change (3.6% vs 3.5% per token), and the tested pursuit recovers at most 5.7% of the direction at k ≤ 100.
- Any workspace claim should rest on (2), on the direct monitor test below and on [Kawada & Kellis 2026](#kawada--kellis-2026)'s complement result, not on the null swap.
- **Next step, if this line continues:** the direct J-lens monitor test that P36 named. Does any J-lens readout distinguish the twins, given how little of PC1 the J-lens change carries?
- **Don't graduate P39 as-is.**

## Setup (as run)

- **Model, stimuli, site:** the same as P36's selected cell.
  - Model: `Qwen/Qwen3.6-27B` @ `6a9e13b`, bf16, sdpa, one H100 80GB (RunPod secure cloud, $3.99/h).
  - Stimuli: P36's file, sha256 `e746f06e…`, 400 sentences, both questions.
  - Site: both entity tokens at L48. P36's code is imported unchanged (capture, hooks, NNOMP, J-lens dictionary, scoring, donor maps); P39 adds additive changes δ at both tokens ([`p39/session.py`](p39/session.py), [`p39/arms.py`](p39/arms.py)).
- **Fit/test split:** P36's 60 train items fit every direction (PCA, gradient axes, mean-difference axis, covariance). Its 20 held-entity and 20 held-verb items are evaluated. Unfitted arms were also run on all 100 items: J-lens 0.1%, residual swap 94.8% and full patch 95.2% of 796 cases, which reproduces P36.
- **Matched size:** per token, ‖δ‖ = ‖δ_J‖, the J-lens k = 25 change's norm at that token. The fit items' mean share was 6.36%.
- **Arms:** as in the plan.
  - Random subspaces: 20 isotropic and 20 covariance-matched draws at matched size, and 5 per family at m/d ∈ {0.005, 0.125, 0.25, 0.5}.
  - J_rot at k ∈ {16, 25, 100, 400}, and 5 spans of 25 random J-lens atoms.
  - PCA top-r for r ∈ {1, …, 64}; r* = 1.
  - Gradient axes aligned to first mention and to role, the mean-difference axis, and the gradient axis's parts inside and outside PC1.
  - 20 random 1-D directions.
  - Per-norm potency: the J change and random slices rescaled to the residual change's norm.
  - Cross-voice donors.
- **Gradients:** dLD/dh at both entity tokens for all 800 prompts, LD = logit(correct) − logit(other entity), backward from the logits to L48 only. They agree with central finite differences (Pearson r = 0.9995, slope 0.97).
- **Scoring:** no judge. A flip means the top-1 token is the recipient's other entity. Intervals: 2,000 item-cluster bootstrap resamples, seed 20261013 ([`p39/analyze.py`](p39/analyze.py) → [`results/summary.json`](results/summary.json); F1: [`results/followup1/summary.json`](results/followup1/summary.json)).

## Gates and checks

| Check | Result |
|---|---|
| Smoke: identity patch | max \|Δlogit\| = 0.0 (pass) |
| Smoke: first 20 prompts | 20/20 (pass) |
| Smoke: gradient vs finite differences | r = 0.9995, slope 0.97 (pass). **Deviation:** the 8 directions mixed the computed gradient with random noise (to spread the predicted changes), not 8 independent random directions as planned, so this is a weaker independent check; the tiny-model test checks independent random directions exactly |
| Smoke: NNOMP | J-lens only in the smoke (**deviation:** J_rot was not smoke-checked). In the runs, every J-lens and J_rot pursuit at every k had KKT ≤ 6.4e-7; mean atoms with positive weight after the refit: 16, 24.99–25, ≈ 99 and 212–229 at k = 16, 25, 100, 400 (`status.json`, `decomp`) |
| Smoke: projection | 1,012 s against 3,568 s left (pass). The first attempt failed here on a projection bug; see Cost |
| Gate 1: unpatched accuracy | 0.995 ("Who did…"), 1.000 ("Who was…") (pass) |
| Gate 2: P36 replication, all items | full 95.2%, residual swap 94.8%, J-lens 0.1%, self-patch 0 (pass; identical in F1's run) |
| Manipulation check, held items | the axes separate first- from second-mention tokens: PC1 and mean-difference AUC 1.0, order gradient axis 0.89 |
| Invalid answers | 0% in every arm except the order axis at 32× (6.9%) |

## Predictions against outcomes

| Prediction (plan, before data) | Outcome |
|---|---|
| O1 informative null (0.60) | No |
| O4 partial (0.15) | No |
| **O2 size-only (0.20)** | **Yes** |
| O3 fragile site (0.05) | No (random matched ≤ 0.3% per draw) |
| J alignment: below 0.35 / **at 0.45** / above 0.20 | **At chance** (a_J −0.0018 vs the draws' 2.5th percentile −0.0024; below 19 of 20 draws) |
| Both random matched families flip ≤ 2% with attribution within 2× of share (0.9) | Flips yes. Attribution: isotropic 0.053 vs 0.064 yes; covariance-matched 0.23 (3.6×) **no** |
| PCA matched ≥ 30% (0.65) | No (0.0%) |
| Gradient axis (aligned to first mention) matched ≥ 30% (0.5) | No (2.5%) |
| First-mention-aligned axis beats the role-averaged one by ≥ 20 points (0.7) | No (2.5% vs 3.4%) |
| PC1 alone holds more than s̄, so matching shrinks it (0.7) | Yes (62% vs 6.4%) |
| cos(gradient axis, PC1) > 0.5 (0.6) | No (0.05) |
| PCA and gradient matched arms flip xsame ≥ 50% of their twin rate, xtwin < 10% (0.65) | Not testable: the twin rates are ~0 |
| The gradient axis's in-PCA part carries more of the flip than its orthogonal part (0.7) | Native: yes (the in-part is PC1, 82.8% vs 0%). Matched: neither flips (0.0% vs 0.9%) |
| J change at the residual change's norm flips < 10% (0.7) | Yes (0.3%) |
| J_rot k = 25 flips ≤ 2% (0.9) | Yes (0%) |
| J-lens k = 25 FVE of the gradient axis < 20% (0.6) | Yes (≤ 2.8%) |
| F1: half the answers first flip at α 0.6–0.8 (0.55) | Narrowly no (α = 0.8 gives 47.8% [40, 56]; ≥ 50% first at 0.9) |
| F1: α ≤ 0.4 flips < 10% (0.8) | Yes (0%) |
| F1: Δ without PC1 flips < 20% (0.6) | Yes (0.6%) |
| F1: PC1 alone flips xsame ≥ 60%, xtwin < 10% (0.75) | Yes (83.1%, 0%) |
| F1: gradient axis at 16× flips < 20% (0.6) | No (44.4%) |

## Follow-ups (exploratory)

- **F1** ([log.md](log.md), the F1 entries) located the flip threshold along PC1 and tested PC1's necessity and its order reading. It also pushed the gradient axis further. Results are in Answer points 2 and 4.
- **Worth running next** (not run):
  1. **The J-lens monitor test:** do J-lens readouts at any position or layer tell the twins apart?
  2. **A trained low-rank subspace** (DAS-style, rank 1–8): the smallest subspace that carries the swap, against the 40%-share PC1 threshold.
  3. **Where PC1 comes from:** it is a high-variance direction (s.d. of its projection over entity states ≈ 26, against 2.4 for the gradient axis). Is it mention order alone, or token-position context that order co-varies with? P36's shifted donors suggest absolute position doesn't drive the flip.

## Raw samples

[results/raw_samples.md](results/raw_samples.md) holds 3 rows per arm on the held items with twin donors, drawn by [`p39/raw_samples.py`](p39/raw_samples.py) (seed 20261013; population and strata in its docstring). Every drawn row is shown:
- full patch: 3 of 3 flipped (shift 0.80–0.86);
- J-lens: 0 of 3 (shift −0.01–0.01);
- isotropic, covariance-matched and J_rot matched slices: 0 of 9;
- PCA, gradient-axis and mean-difference matched slices: 0 of 9. The gradient axis's draws show the largest shifts (0.00, 0.33, 0.41);
- role-averaged axis: 0 of 3, with shifts from −0.50 to 0.30;
- random 1-D directions: 0 of 3.

## Caveats

- **Observations:**
  - One model, one layer and site, one sentence frame. Single-token entities. Additive patches at both entity tokens.
  - "Matched" fixes the norm per token at the J-lens change's norm. A different size criterion, such as matching the linear share, could rank the arms differently. The native-size dose curves are the guard.
- **Hypotheses:**
  - That PC1 is an order/subject-associated direction rests on its first/second-mention AUC of 1.0 and on the two cross-voice donors. As in P36, first mention and grammatical subject are confounded here. "Necessary and sufficient" is not claimed: the two F1 swaps show that PC1's component dominates these swap interventions, not what the model's own computation requires.
  - That the gradient axis is a "dormant" direction is an analogy to [Makelov et al. 2023](#makelov-et-al-2023), not a test of their full criterion.
- **Conventions:**
  - J-lens atoms include the final-norm gain (P36's convention).
  - The J-alignment verdict compares a_J with the 2.5th/97.5th percentiles of 20 isotropic draws (linear interpolation; the first analysis used the min/max, review r1 finding 4; same verdict).
  - Size matching by norm follows the norm-matched comparisons of [Gurnee et al. 2026](#gurnee-et-al-2026) and [Viswanath 2026](#viswanath-2026); P36 and the J-lens swap here are at native magnitude.
  - The summed-over-tokens `cap_*` columns in `arm_stats.jsonl.gz` are ill-conditioned: the two entity tokens' projections on PC1 have opposite signs. Per-token capture is computed from the saved states by `p39.analyze.pc1_capture` (`results/summary.json`, `pc1_capture`; the states in `results/states/` are gitignored, hashed in the MANIFEST and backed up).

## Cost and wall-clock

| Session | Pod time | $ | Notes |
|---|---|---|---|
| Push refused (22:11Z) | 0.05 h | 0.18 | `raw_samples.py` was written in the probe dir after launch |
| Three slow hosts (22:14–22:23Z) | 0.15 h | 0.58 | 98–100 MB/s against the 150 MB/s gate. The gate was lowered to 80 MB/s (it still rejects hosts below 80 MB/s, such as most of wave 4's 6–82 MB/s hosts) |
| Attempt 1 (22:30–22:47Z) | 0.39 h | 1.57 | Smoke stopped on the budget projection. The first gradient call (81 s, including kernel compiles) was extrapolated as if every batch cost that; a warm batch takes 3 s |
| Main run (22:52–23:19Z) | 0.50 h | 1.98 | Download 6.5 min (GPU idle), load 0.3, capture 1.2, smoke 1.6, baseline 0.1, gradients 0.4, J-lens pursuit 0.6, rotated pursuit 0.7, anchors 0.3, primary 3.5, secondary 5.8, cross-voice 0.5 min |
| F1 (23:29–23:46Z) | 0.34 h | 1.36 | Download 7.3 min, load 0.5, capture 1.1, smoke 1.4, F1 forwards 0.6 min |
| **Total** | 1.42 h | **5.67** | worktree `logs/costs.jsonl` |

GPU compute was about 22 minutes in all. Downloads (≈ 23 min over three runs) and the $2.33 lost to the push, the hosts and the projection bug made up the rest.

Avoidable (review r1, throughput):
- the refused push and the cold-gradient projection cost ≈ $1.75 and 26 min; launch inputs are now frozen and the smoke times a warm batch;
- an 80 MB/s gate from the start would have saved up to $0.58;
- caching the weights between the main run and F1 would have saved ≈ $0.9 and 14 min of download.

## Review

One cross-review round by gpt-6-astra on the whole branch (code, results, note): [`logs/reviews/20261013-P39-branch-r1.md`](../../logs/reviews/20261013-P39-branch-r1.md). **Verdict: REVISE.**
- **Recomputation:** the reviewer independently recomputed every requested rate from the row files, gate 2 in both runs, all 26 manifest entries (decompressing the rows), the per-token PC1 capture and the spend ($5.6663, 1.4202 pod-hours). All matched. O2 is correct under the plan's rule, and no arm-math or patching bug was found.
- **BLOCKING (2), both fixed in this note:**
  1. "any slice that size would have done nothing" became "none of the tested signal-chosen slices"; the plan's don't-pitch consequence is stated.
  2. "single direction, necessary and sufficient, needs 40%" became "dominant order/subject-associated component of these swaps; about 40% along PC1".
- **MINOR (5):**
  - pursuit FVE is not an upper bound on representability: reworded;
  - J-alignment by percentiles, not extrema: implemented, same verdict; and "inconclusive" for cases outside O1–O4: implemented;
  - the attribution-vs-shift nonlinearity inference: removed;
  - smoke deviations (gradient-mixed finite-difference directions, J_rot not smoke-checked): documented under Gates;
  - avoidable spend: added under Cost.
- **NIT (2):** the 80 MB/s wording, fixed; the plan's gradient-notation clarification, added to log.md (the plan itself is left as published).

No headline number changed, so no second round (AGENTS.md).

## References

#### Gurnee et al. 2026
Gurnee, Sofroniew, Pearce et al., "Verbalizable Representations Form a Global Workspace in Language Models", transformer-circuits.pub/2026/workspace (`papers/interp_methods/` row 1).

#### Kawada & Kellis 2026
Kawada & Kellis, "Evidence Integration in Large Language Models", arXiv 2609.04290 (`papers/interp_methods/` row 106; §7.4 and App. L read). It runs P36's split on Llama-3.1-8B: the workspace swap is null and the complement carries the effect, with dimension-matched random controls.

#### Makelov et al. 2023
Makelov, Lange & Nanda, "Is This the Subspace You Are Looking for? An Interpretability Illusion for Subspace Activation Patching", arXiv 2311.17030 (`papers/interp_methods/` row 41; the dormant-direction caution, via the launching session's review).

#### Shi et al. 2026
"S-Space: Exploring Spatial Workspace in Multimodal Language Models", arXiv 2610.13527 (`papers/interp_methods/` row 99): the role-antisymmetric gradient-axis recipe.

#### Viswanath 2026
"Models are blind outside the J-space. NLAs aren't.", LessWrong LhDJdccLszLEAqgZ9 (`papers/interp_methods/` row 98): norm-matched J/non-J halves; a variance share at one cut doesn't locate causal content.
