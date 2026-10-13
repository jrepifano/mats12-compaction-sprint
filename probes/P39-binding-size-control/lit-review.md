# P39 literature review: size controls for P36's null reconstruction swap

2026-10-13. Two parts:
- **Part A:** the dedicated internal and external review the launching session ran for P39, delivered as a file, with its findings and design changes.
- **Part B:** what the probe agent read itself: the I12 reviews and P36's reviews, plus primary-source passages.

The plan ([plan.md](plan.md)) adopts every design change from Part A, and any departure is listed below.

## A. Dedicated review (launching session)

### Closest work

**Kawada & Kellis 2026**, "Evidence Integration in Large Language Models" (arXiv 2609.04290), now `papers/interp_methods/` row 106. Its §7.4, Table 6, App. L and Tables 13–14 were also read verbatim by the probe agent.
- **Method.** It runs P36's exact split: k = 25 nonnegative matching pursuit on J-lens atoms, swapping the workspace part and the complement between a run that accepts a shown value and one that rejects it.
- **Results.** In Llama-3.1-8B (L19–30, n = 60) the workspace swap is exactly null and the complement reproduces the full effect. In Qwen3-8B the complement carries 83%.
- **Controls.** A fixed random k-dimensional subspace and the span of k random J-lens atoms are null at k = 5–200. A norm-matched random direction orthogonal to the workspace span costs at most 0.18 acceptance.
- **Basis-specific.** A k = 25 SAE reconstruction carries 75% of the effect.
- **What it leaves open.** Their random controls are matched by dimension, not by share of the swapped difference. So, like P36, they leave open whether a slice the J-part's size could move the answer at all. That is P39's question.

**Gurnee et al. 2026** (row 1). J parts holding 6–7% of the variance carry the swap when rescaled to equal magnitude: 59% top-5 naming against 5% for the remainder. Their controls are norm-matched per position and include a rotated dictionary (J_rot). P36 swapped at native magnitude.

**Viswanath 2026** (row 98) also norm-matched his J and non-J halves.

**Makelov, Lange & Nanda 2023** (row 41), the subspace-patching illusion.
- **The failure mode.** A patch along a direction can work through a component that discriminates but is disconnected from the output, plus a dormant causal component.
- **Where it was found.** In MLP hidden space. The residual-stream gradient direction was faithful (45% interchange).
- **Their checks:** held-out names and templates, each component's spread, and the logit-difference shift (FLDD), since about 50% FLDD can come with about 0% flips.

**Small role-chosen slices do act.**
- Feng & Steinhardt 2024 (row 49): binding vectors reverse answers, and equal-magnitude random vectors don't.
- Dai et al. 2024: a PCA ordering-ID direction.
- S-Space (row 99): a gradient-axis swap keeps 43.9% of answers, against 99.8% for a norm-matched random edit. That result is on Molmo2-ER L17, not Qwen3.6-27B; see Part B.

**Random baselines.** Lee & Heimersheim 2024 found isotropic random directions much less sensitive than covariance-matched ones (summary only).

**Gap.** No share-matched comparison of the J reconstruction, random slices and role-chosen slices exists for binding or roles.

### Design changes and how the plan takes them
1. **Dose curves are the primary output.** Flip rate and logit-difference shift are plotted against the share ‖δ‖²/‖Δ‖², for each arm family:
   - random slices;
   - J_rot;
   - PCA;
   - the gradient axis;
   - the J-lens at k = 16–400.

   Kawada's 25-dimensional random subspace (about 0.5% share) is a secondary arm. → Adopted.
2. **First-order attribution per arm,** a = g·δ / g·Δ, with g the gradient of the logit difference at the recipient's entity tokens (one backward pass per prompt). Random slices should give a ≈ share. The J-null is informative only if J's a falls clearly below that of a random slice of the same share. → Adopted, as a labelled "J alignment" verdict next to the flip-based outcome (plan, Notes).
3. **Random arms.**
   - 20 fixed draws, isotropic and covariance-matched, with the spread across draws reported → adopted.
   - Arm 2 uses J_rot with the same pursuit, plus random J-lens atoms → adopted. The i.i.d. Gaussian dictionary of the first draft is dropped: J_rot is the stricter control, because it keeps the dictionary's geometry.
   - Reconstruction change rescaled to the residual change's norm, against a norm-matched random direction → adopted.
   - Flips toward the twin's answer counted separately from disruption → adopted (flip, invalid and logit-difference shift reported separately).
4. **PCA arm.**
   - Split fit and test by entity and verb → adopted. Fit on P36's 60 train items; evaluate on its 20 held-entity and 20 held-verb items.
   - Report PC1's share, and use a partial swap when PC1 alone exceeds the matched share → adopted.
   - Score the cross-voice donors too → adopted.
5. **Gradient arm.**
   - A held-out, averaged, role-antisymmetric gradient, not a same-item one → adopted.
   - Report u·Δ against the spread of u·h, and cos(u, PC1) → adopted.
   - Patch u's part inside the PCA subspace and its orthogonal part separately → adopted.
   - Add a norm-matched random 1-D null → adopted.

### Prior adjustments (taken into the plan)
- Random ≈ 0 flips, about 0.9; FLDD and attribution decide.
- PCA at matched share ≥ 30%, 0.6–0.7.
- Held-out gradient axis ≥ 30%, about 0.5, likely tracking order rather than role.
- A fragile site is unlikely, by Kawada's 0.18 bound.

### Its reading status
- **Verbatim:**
  - Makelov §3.5, §4.4, §5;
  - Gurnee's split passages and controls;
  - Feng & Steinhardt's random controls;
  - Kawada & Kellis §7.4 and App. L;
  - the intervention passages of S-Space and Viswanath.
- **Summary only:** Lee & Heimersheim; Wu, Geiger et al.'s reply to Makelov (arXiv 2401.12631); Dai et al. (via the I12 review).
- **Not read:** RAVEL, MIB.

## B. Agent's own reading

Reused reviews, read in full:
- the I12 internal and external reviews ([`logs/litreviews/20261013-I12-internal.md`](../../logs/litreviews/20261013-I12-internal.md), [`-external.md`](../../logs/litreviews/20261013-I12-external.md));
- P36's [lit-review.md](../P36-workspace-binding-causal/lit-review.md) and [lit-search-2026-10-13.md](../P36-workspace-binding-causal/lit-search-2026-10-13.md);
- P36's [note.md](../P36-workspace-binding-causal/note.md).

**Viswanath 2026** (row 98; LessWrong post read in full):
- He norm-matched both halves back to the original vector (line 106).
- At r = 200 the J part held 6.1% of the energy, and his result reversed between 6 and 16 concepts. His conclusion: "the 6% at r=200 number is a statement about variance, and nothing says 'reportability' lives at where the variance is" (lines 89, 122–128). That is P39's premise.
- Random directions inside J gave a higher P(yes) than real J components (line 134), so random controls matter.

**S-Space, Shi et al. 2026** (row 99; abstract, §1–2, App. B–C read).
- **The axis.** It is the gradient of an answer-logit contrast with respect to the object tokens' states, made role-antisymmetric (query gradient minus reference gradient) and averaged over templates, swapped orders and examples (§2.2, Eq. 2; App. B gives the cancellation argument).
- **The control.** A random edit with the same norm as each example's swap update (App. C.3).
- **Different model.** Their interventions ran on Molmo2-ER at L17, with amplified edits; Qwen3.6-27B appears only in their readouts (L43).
- **Applied to P36's data.** P36's F1 found that the swapped entity signal follows mention order/subject, not thematic role. A literal S-Space average by role would then cancel across voices, because the agent is the first mention in active sentences and the second in passive ones; a side agent raised the same point on 2026-10-13. The plan therefore builds the axis aligned to first mention (primary), with the role-averaged version reported as the expected cancellation.

**Binding subspaces are low-rank:**
- Feng & Steinhardt 2024;
- Feng, Russell & Steinhardt 2024;
- Dai, Heinzerling & Inui 2026: 2–5 PLS dimensions at attribute tokens, with a random-subspace control.

All three via the I12 reviews. They are the main reason to expect a role-chosen slice of about 6% to move the answer.
