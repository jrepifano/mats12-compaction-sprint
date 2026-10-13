# P36: In Qwen3.6-27B, swapping the residual moves "who did it" as much as a full patch, and swapping the sparse J-lens reconstruction does nearly nothing; the swapped signal follows mention order/subject more than thematic role

**Status:** done, 2026-10-13. Spend **$9.78** of $55, all GPU, over five pod sessions:
- the main run, $2.41;
- follow-up F1, $1.65;
- follow-up F2, $1.36;
- a stalled-download pod, $4.33, and a refused push, $0.02. Both were lost to infrastructure.

No API spend. Plan: [plan.md](plan.md); run log: [log.md](log.md); literature: [lit-review.md](lit-review.md), [lit-search-2026-10-13.md](lit-search-2026-10-13.md); raw samples: [results/raw_samples.md](results/raw_samples.md). Review: see "Review" at the end.

## Answer

1. **Plan verdict: "greater residual-swap effect; the reconstruction does little".** At native magnitude, swapping the residual between role-reversed twins moves the answer as much as a full patch; swapping the sparse J-lens reconstruction barely moves it. This is a statement about these swaps, not proof that the reconstruction holds no role information, that the two components are equally potent per unit of norm, or that the residual acts without later J-space contents (none of these was tested).
   - **Where the swaps were done.** The answer depends on the entity tokens' states at layer 48 of 64. For each entity token we took the state from the role-reversed twin sentence ("The teacher chased the doctor." ↔ "The doctor chased the teacher.") and patched it into the same entity's token. That flips the answer on **95.2% [92.9, 97.1]** of 796 (twin, question) pairs.
   - **Residual swap:** the twin's residual plus the recipient's own reconstruction. It flips **95.0% [92.6, 96.9]** and moves the logit difference 79% of the way to the twin's, against 80% for the full patch.
   - **Reconstruction swap:** the twin's J-lens reconstruction (k = 25) plus the recipient's own residual. It flips **0.1% [0, 0.4]**.
   - **Paired difference** (residual − reconstruction): its interval's lower bound is ≥ +0.85 in every subset (all three dictionaries, every k): both questions ("Who did the X-ing?", "Who was X-ed?") and both voices (active, passive).
   - **Equivalence bound:** the reconstruction swap's upper 95% bound is ≤ 1.3 points in every subset, inside the plan's 10-point equivalence bound.
   - **The same at every cut and with every dictionary:**
     - J-lens at k = 16, 25, 100 and 400: residual 94.6–95.2%, reconstruction 0–0.1%;
     - J++ lens ([Ayonrinde & Lindsey 2026](#ayonrinde--lindsey-2026)) at the same k: residual 94.0–94.7%, reconstruction 0–0.3%;
     - the training-free logit-lens dictionary: residual 91.7–94.6%, reconstruction 0–0.1%.
   - **Plan verdict:** "greater residual-swap effect, the reconstruction does little", at every k and on J++. In I12's terms this is the "residual > recon" row (prior 0.2).
   - **Not an artefact of decomposing raw activations (follow-up F2, exploratory).** A side agent raised a concern: with identical words in both twins, a sparse fit to raw states might spend its atoms on what the twins share, pushing the role difference into the residual by construction. F2 subtracted a mean before the pursuit, in two ways:
     - **global:** the mean entity-token state over all sentences;
     - **per-entity:** each entity token's own mean, which removes most "which entity" content.

     Both were run at entity@48 for the J-lens, J++ and the logit lens at k = 25 and 100. Results:
     - The reconstruction swap flips **0–0.3%** in all 12 variants.
     - The residual swap flips **92.3–94.7%**. The cross-voice same-roles residual swap flips 75.9–80.0%.
     - Global centring roughly doubles the reconstruction's variance explained at the entity tokens: 12–18% for the J-lens and J++, 6–13% for the logit lens. Its share of the twin difference stays at 3–11%.
     - Per-entity centring *lowers* that share: 2.5–3.8% for the J-lens and J++, 3.1–6.7% for the logit lens.
     - An in-run rerun of the uncentred J-lens k = 25 swaps reproduced 95.0% and 0.1% exactly.
2. **The reconstruction hardly distinguishes the twins in the first place.**
   - **Twin-difference share:** the share of the twins' squared difference at an entity token that falls in the J-lens reconstruction. It is 6.2–6.5% at k = 25 and 3.8–4.0% at k = 400; for J++ it is 4.7–6.9%.
   - **Variance explained** at the entity tokens is 5–6% (J-lens), 4–7% (J++) and 2–7% (logit lens), from k = 25 to 400. [Gurnee et al. 2026](#gurnee-et-al-2026) report at most 10%.
   - **What the reconstruction holds:** its top atoms are the entity's own token and near-synonyms (" sailor", " sergeant", "船"), the same in both twins. A reading consistent with this (not tested further): the reconstruction mainly reflects *which* entity, and what the twins differ in sits mostly outside it.
3. **Unplanned finding (follow-up F1, exploratory): the swapped signal is consistent with an order/subject-associated index, robust to a 3-token offset, rather than a transfer of thematic role.**

   | Donor for "The sailor interviewed the teacher." | Roles | Mention order | Absolute positions | Full-patch flip, L48 |
   |---|---|---|---|---|
   | role-reversed twin ("The teacher interviewed the sailor.") | swapped | swapped | same | 95.2% [92.9, 97.1] |
   | same roles, other voice ("The teacher was interviewed by the sailor.") | kept | swapped | differ | **80.5% [77.6, 83.5]** |
   | reversed roles, other voice ("The sailor was interviewed by the teacher.") | swapped | kept | differ | **1.0% [0.4, 1.8]** |
   | same sentence shifted 3 tokens ("Yesterday afternoon, the sailor interviewed the teacher.") | kept | kept | +3 | **0.4% [0, 0.9]** |
   | shifted twin | swapped | swapped | +3 | 89.3% [86.8, 91.7] |

   - The patched entity state's effect follows **mention order** (equivalently here, grammatical subject) far more than role, and it survives the tested 3-token offset: in the shifted donor, the agent's state comes from the recipient's object-slot absolute position, and almost nothing flips. Sentence-relative position, syntactic slot and mixed codes are not separated by these donors.
   - This holds from layer 46 to 50: the same-roles cross-voice donor flips 72–84%, the reversed-roles cross-voice donor 1–3%.
   - **Earlier and later layers:**
     - At L42–44 the order-associated effect is weaker: cross-voice same-roles 19–50%, while the twin flips 77–91%.
     - From L52 on, patching the entity tokens hardly matters: twin 26%, cross-voice same-roles 18–19%.
   - **The decodability probes (exploratory) agree.** A linear "this token's entity is the agent" probe trained on one voice scores **0–1.7%** on the other voice at every layer 12–54 and for every feature (h, reconstruction, residual; J-lens and logit-lens): near-zero transfer. Within a voice, order and role are one variable, so a one-voice probe is close to perfectly *anti*-correlated with role in the other voice. This doesn't rule out another role code that such a probe wouldn't find.
   - **Role is still linearly decodable when both voices are in training.** Accuracy, min–max over layers 12–54 (at L48 in brackets):

     | Feature | Held-out verbs | Held-out entities |
     |---|---|---|
     | h | 0.89–0.96 (0.96) | 0.74–0.79 (0.75) |
     | residual, J-lens k = 25 | 0.86–0.94 (0.94) | 0.74–0.76 (0.76) |
     | residual, logit lens k = 25 | 0.91–0.96 (0.96) | 0.74–0.76 (0.76) |
     | reconstruction, J-lens k = 25 | 0.71–0.86 (0.83) | 0.54–0.68 (0.66) |
     | reconstruction, logit lens k = 25 | 0.74–0.89 (0.86) | 0.65–0.73 (0.73) |

     Samples are both entity tokens of each sentence (label: is this token's entity the agent), not the plan's single designated entity; see the log.
   - **Hypothesis, not tested:** the model reads "which entity came first" from the entity tokens around L46–50 and combines it with the voice, read from the verb phrase, which wasn't patched.
   - In every sentence here the first mention is also the grammatical subject, so "first mention" and "subject" are not separated.
4. **No effective intervention at the pre-question site.** Patching the sentence-final "." from the twin flips ≤ 1% at every layer 12–54 (single-layer patches). That is the plan's "no effective intervention at the tested sites" for that site; it does not show that nothing is read from the period, only that replacing it alone doesn't change the answer.

**What it changes:** I12's "residual > recon" branch, with a refinement.
- At the entity tokens, the J-lens reconstruction is nearly the same for both twins, and swapping it does nothing. That motivates, but doesn't show, the step the plan named: a direct test of whether a J-lens monitor can tell apart scenarios that differ only in roles (not run; untested).
- The swapped signal behaves like an order/subject-associated index robust to the tested offset, reminiscent of the position-independent binding IDs of [Feng & Steinhardt 2024](#feng--steinhardt-2024) (an analogy, not shown), rather than a transfer of thematic role.
- For Neel's "what is the global workspace": under this decomposition, the component whose swap moves the answer is the residual, not the sparse J-space reconstruction, at the layers where entity patches work. Whether downstream J-space contents mediate it is open (the clamp check below). It is a cheap ground-truth case for the Oracle Lens comparison.
- Candidate for a short interp study. **Don't graduate this probe as-is.** Run the cleft check (1) and the monitor test (3) below first.

## Setup (as run)

- **Model:** `Qwen/Qwen3.6-27B` @ `6a9e13b`, bf16, sdpa; one H100 80GB (RunPod, secure cloud, $3.99/h).
  - The model loaded through I1a's backend loader.
  - The patch hooks followed P11's conventions: layer L is the output of block L, edited in place on the single prefill forward.
  - The model's `causal_conv1d` kernel wasn't installed. The reference implementation ran instead: correct, slower.
- **Stimuli:** [`data/stimuli.json`](data/stimuli.json), sha256 `e746f06e…`, seed 20261013.
  - 24 single-token person nouns × 20 transitive verbs give 100 base items; 8 entities and 6 verbs are held out for the probes.
  - Each item appears in active and passive voice, each with its role-reversed twin: 400 sentences, 800 prompts.
  - Prompt format: `Sentence: … \nQuestion: Who did the <gerund>? / Who was <participle>? Answer with one word.`, then the prefill "The". We read the next token.
- **Dictionaries.** Each atom is a unit row of (W_U ⊙ g) M_L, where g is the final-norm gain, over the full 248k vocabulary:
  - J-lens: `neuronpedia/jacobian-lens` @ `b25d72a`, n1000 wikitext;
  - J++: `koayon/jpp-lenses` @ `6c96867`, `qwen3.6-27b/lens.pt`;
  - logit lens: M = I.
- **Pursuit:** non-negative OMP ported from the vendored MIT `olens_draft` NNOMP, with FISTA NNLS.
  - It runs on the raw (uncentred) activation and stops after exactly k selections.
  - KKT violation ≤ 2.3e-6 at every cut.
  - At k = 400 the J-lens keeps 178–252 positive atoms at the entity tokens and J++ 306–396; the others lose their weight in the refit.
- **Arms** (both questions, both directions of each twin pair):
  - full patch, reconstruction swap (j_donor + r_recipient), residual swap (j_recipient + r_donor);
  - self-patch and self-recombined (both 0 flips, identity exact to 0.0);
  - cross-voice same-roles, cross-voice reversed-roles, and random donor (another item).
- **Scoring:** no judge. A flip means the top-1 next token is the recipient's other entity.
  - Intervals: 2,000 item-grouped bootstrap resamples, seed 20261013 ([`p36/analyze.py`](p36/analyze.py) → [`results/summary.json`](results/summary.json)).
  - F1 rows: [`results/followup/`](results/followup/).
  - Probes: [`p36/probes.py`](p36/probes.py) → [`results/probes.json`](results/probes.json).

## Gates and checks

| Check | Result |
|---|---|
| Smoke: identity patch, same batch | max |Δlogit| = 0.0 (pass) |
| Smoke: first 20 prompts correct | 20/20 (pass) |
| Smoke: pursuit FVE in (0, 1), k atoms | pass for all three dictionaries |
| Gate 1: unpatched accuracy ≥ 95% per question | 99.5% ("Who did…"), 100% ("Who was…") (pass) |
| Gate 2: full patch flips ≥ 50% under both questions at some cell | entity site L42 (81 / 73%) and L48 (94 / 96%) pass; final "." site never passes (≤ 1%) |
| Self-patch / self-recombined (j_r + r_r) | 0 flips, 0 invalid, shift 0.000 |
| Random donor (other entities) | full 5.5% flips with 90.6% invalid answers (it injects other entities' names). Reconstruction-only random swap: 0.1% flips, 0% invalid. Residual-only: 5.5% flips, 90.5% invalid |
| Invalid answers in the twin arms | 0% in every twin and cross-voice arm |

## Predictions against outcomes

| Prediction (plan, before data) | Outcome |
|---|---|
| Gates fail (I12 prior 0.3) | Gates passed at the entity site; the pre-question site failed, as H-pos allowed |
| Recon > residual (I12 conditional 0.2) | No |
| Residual > recon with recon doing little, across the k sweep and J++ (0.2) | **Yes** |
| Both unequal / neither / cut-dependent (0.6) | No |
| Mine: J-lens k = 25 explains < 10% of variance and < 10% of the twin difference at both sites (0.8) | Entity site yes (5–6%, 6%). At the "." site, variance explained was 0.5–7% by layer, but the twin-difference share was 20% at L48. **Half right** |
| Mine: residual swap ≥ 70% of the full patch and reconstruction swap < 10% at k = 25 (0.55) | Yes (95% vs 95%; 0.1%) |
| Mine: logit lens orders the components as the J-lens does (0.7) | Yes |
| Mine: at k = 400 the reconstruction swap rises above 10% at some site (0.4) | No (≤ 0.1%) |
| F1: shifted same-roles donor < 10% and shifted twin ≈ twin (0.7) | Yes (0.4%; 89% vs 95%) |
| F2: global centring changes nothing material, reconstruction swap < 10% (0.75) | Yes (≤ 0.3%) |
| F2: per-entity centring raises the reconstruction's twin-difference share above 20% (0.5) | No (2.5–6.7%) |
| F2: per-entity centring raises the reconstruction swap to ≥ 10% at k = 100 for some dictionary (0.35) | No (≤ 0.3%) |

## Follow-ups (exploratory)

- **F1** (log.md, 16:20Z): separated mention order from absolute position. Answer 3 has the result.
- **F2** (log.md, 18:05Z): centred decompositions (global and per-entity means), prompted by a side agent's note forwarded by Jacob. Answer 1 has the result: no change.
- **Worth running next** (not run; cost about $2–3 each, mostly the model download):
  1. **Object-fronted sentences** (clefts, e.g. "It was the teacher that the sailor interviewed") to separate first mention from grammatical subject.
  2. **The J-coordinate clamp of [Gurnee et al. 2026](#gurnee-et-al-2026)** downstream of the residual swap: does the index act through later J-space contents?
  3. **The direct monitor test I12 names:** do J-lens readouts at any position or layer let a reader tell the twins apart? The "." site's reconstruction holds 20% of the twin difference at L48; its top atoms there include "宾语" (object), "被动" (passive) and "颠倒" (reversed).
  4. **Oracle Lens on the residual-swapped entity states,** against Qwen's answer under the same patch: only with the trained injection contract, and with P11's caveat that a reader built on Qwen can re-answer. Deferred.

## Raw samples

[results/raw_samples.md](results/raw_samples.md) holds 3 rows per arm at entity@48, drawn by [`p36/raw_samples.py`](p36/raw_samples.py) (seed 20261013; population and strata in its docstring). Every drawn row is shown.
- Twin full patch: 2 of 3 flipped. Reconstruction swap: 0 of 3. Residual swap: 3 of 3.
- Cross-voice same-roles: 0 of 3. That draw is unrepresentative: the arm flips 80.5% overall, and each drawn row checks out as a genuine non-flip. One of them is item 81, which flips under the agent question but not the patient question.
- Reversed-roles cross-voice: 0 of 3. Shifted same-roles: 0 of 3. Shifted twin: 3 of 3.

## Caveats

- **Observations:**
  - One model, one sentence frame, single-token entities, single-layer patches at two site types.
  - "Native magnitude" swaps patch recombined states, which may be off-distribution. The self-recombined control (0 flips) and the 0% invalid rate bound this.
- **Hypotheses:**
  - The order/subject reading in Answer 3 rests on four donor types in one frame.
  - That the order index is "a binding ID" is an analogy to the cited work, not shown here.
  - The pursuit's selections beyond the top atoms are noisy: atom-set overlap between twins is 12%, about the same as between unrelated sentences. Statements about *which* atoms are present rest on the top few only.
- **Conventions that could matter:**
  - Raw (uncentred) activations: F2 tested global and per-entity centring, with no change.
  - Including the final-norm gain g in the atoms.
  - The full vocabulary for J++ (no readout filtering).
  - The result held across three dictionaries, four cuts and two centrings, which bounds but doesn't remove these choices.
- **The J-lens file and J++ file** are the released artifacts. We didn't refit them.

## Cost and wall-clock

| Session | Pod time | $ | Notes |
|---|---|---|---|
| Main run (15:42–16:19Z) | 0.60 h | 2.41 | download 15.6 min (GPU idle, ≈ $1.04: the idle alert), load 0.4 min, capture 1.1, smoke 0.4, baseline 0.2, stage 0 1.2, k = 25 pursuit 1.3, pass 1 3.3, k-sweep pursuit 4.4, pass 2 1.8 min |
| F1 refused push | 0.01 h | 0.02 | uncommitted file |
| F1 stalled pod | 1.09 h | 4.33 | 6.0 GB in 62 min; terminated by hand; download guard added to run.sh |
| F1 run (17:34–17:55Z) | 0.41 h | 1.65 | download 13.0 min, load 0.5, capture 0.9, baseline 0.3, full patches 2.3, pursuit 0.2, swaps 0.5 min |
| F2 run (18:05–18:26Z) | 0.34 h | 1.36 | download 6.4 min, load 0.4, capture 1.0, baseline 0.2, pursuit (12 variants) 2.1, swaps 4.8 min |
| **Total** | 2.45 h | **9.78** | worktree `logs/costs.jsonl` |

GPU compute was under 20 minutes in total. Downloads and one slow host made up most of the spend.

## Review

One cross-review round by gpt-6-astra on the whole branch (code, results, note): `logs/reviews/20261013-P36-branch-r1.md`. **Verdict: REVISE.**
- **Recomputation:** the reviewer recomputed every headline rate and interval from the row files, and all matched.
- **BLOCKING (2), both fixed:** over-claiming. The note now states the plan's operational verdict and labels mechanisms as hypotheses.
- **MINOR (5), all fixed except the infrastructure one (noted):**
  - probe ranges per feature;
  - "incomplete" verdicts for a missing grid;
  - per-state smoke checks and projection refusal;
  - the logged probe design change;
  - download cost.
- **NIT (1):** fixed.

F2 ran after the review and is not reviewed. AGENTS.md calls for a second round only when a fix changes the headline number, and F2 left it unchanged.

## References

#### Gurnee et al. 2026
Gurnee, Sofroniew, Pearce et al., "Verbalizable Representations Form a Global Workspace in Language Models", transformer-circuits.pub/2026/workspace (`papers/interp_methods/` row 1).

#### Ayonrinde & Lindsey 2026
"J++ Lens: Jacobian Filtering Enables More Faithful Workspace Lenses", LessWrong nc9dHfcB22JdMzGbr (`papers/interp_methods/` row 81); lenses `koayon/jpp-lenses`.

#### Feng & Steinhardt 2024
"How do Language Models Bind Entities in Context?", ICLR 2024, arXiv 2310.17191 (`papers/interp_methods/` row 49; its binding IDs are position-independent and attached at entity tokens, the analogy drawn in Answer 3).
