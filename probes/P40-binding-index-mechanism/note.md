# P40: In Qwen3.6-27B, swapping the two entity tokens' mention order (L44–50) moves "who did it"; swapping subject or role alone doesn't, and one mean-difference vector does the same across constructions

**Status:** done, 2026-10-14. Spend **$7.34** of $30, all GPU, over seven pod sessions:
- three hosts refused by the download gate, $0.86;
- attempt 1, stopped by the smoke check (CAPITALS prompt), $1.22;
- the main run, $3.90;
- follow-up F1 (CAPITALS), $1.20, plus one refused push, $0.17.

No API spend. Plan: [plan.md](plan.md); run log: [log.md](log.md); literature: [lit-review.md](lit-review.md); generated tables: [results/summary.md](results/summary.md); raw samples: [results/raw_samples.md](results/raw_samples.md). Review: see "Review" at the end.

## Answer

All numbers are flip rates (top-1 is the alternative the arm predicts) with 95% item-bootstrap intervals, at layer 48 of 64 unless stated. "Doesn't move the answer" means these interventions had no effect. It doesn't show that the information is absent.

1. **Order-sensitive, not subject- or role-sensitive, at the entity tokens.** Changing the two entities' mention order moves the answer. Changing only their subjecthood or only their thematic role doesn't.
   - **The test:** each recipient sentence's two entity tokens receive the same entities' states from a donor sentence. The donor is in another construction (active, passive, object cleft, by-cleft, subject cleft, topicalised), with roles kept or reversed. Each cell differs from its recipient in a known mix of order, subject and role.
   - **The single-feature cells,** pooled over the constructions that passed the accuracy gate (active, passive, object cleft):

     | Donor differs in | Flip % [95% CI] | n |
     |---|---|---|
     | order only | **59.1 [55.6, 62.4]** | 794 |
     | subject only | **1.0 [0.4, 1.7]** | 790 |
     | role only | **1.0 [0.4, 1.8]** | 796 |
     | order + subject | 80.4 [77.4, 83.5] | 796 |
     | all three (role-reversed twin) | 81.0 [78.5, 83.5] | 1190 |

   - **A linear-probability fit** (flip ~ Δorder + Δsubject + Δrole) gives order **+0.72 [0.70, 0.75]**, subject +0.13 [0.12, 0.15] and role +0.05 [0.03, 0.06]. Subject adds something only together with an order change.
   - **The plan's "order index" criterion, as written, can't be evaluated here.** It needs the same ranking of the three single-feature cells in at least 3 recipient constructions. Gate 1 removed the by-cleft and the topicalised sentences, so each remaining recipient lacks one cell (active: subject-only; passive: order-only; object cleft: role-only).
     - The script first printed "construction-specific", because it ranked the passive's two remaining cells (role 2.0%, subject 0.5%).
     - **A post hoc rule** (exploratory; log.md "Analysis corrections" (1)) counts a recipient against a feature only when another single-feature cell reaches 15% there or the feature's own cell is present and not the largest. Under that rule the verdict is "order index", with 3 of 3 recipients consistent.
     - **The plan's "both patch types"** is read as: subject-only and role-only stay < 15% for both patch types (they do: ≤ 1.0%). The ≥ 50% requirement applies to the both-entity patch. Second-mention-only order patches are weak (3.5%).
   - **Sensitivity (exploratory):** with all four core constructions, by-cleft included (it failed gate 1 at 0.85 on agent questions), every cell exists. The criterion is then evaluable and **not met**: order-only **48.6% [44.9, 52.1]**, against 0.5% subject-only and 0.8% role-only. Order is still the top feature for every recipient.
     - Each construction's own ceiling bounds these rates. The object cleft's own twin patch flips only 52.5%, and its order-only cells reach 47.6%.
   - **Both entities must be patched.** Second mention alone: order-only 3.5%, twin 7.6%. First mention alone: ≤ 1.6%. The effect behaves like a relation between the two mentions.
     - Caveat: the first mention in an active or passive sees only "Sentence: The" before it, so it can't carry its own role or subject. The subject and role tests rest on the second mention and on the frames (clefts) where left context differs.
   - **By layer** (order-only, both entities): 1.5% (L40), 21.9% (L44), 59.1% (L48), 14.4% (L52).
2. **Where patches move it, and where the answer is read.** In the role-reversed twin patch (active/passive, 796 pairs), among the single sites tested, only the entity tokens move the answer before L52:
   - **Both entities:** 15% at L28, 91.5% at L44, **95.2% [93.1, 97.2]** at L48, 25.6% at L52, 3% at L56.
   - **Other sentence tokens:** the verb, "was", "by", the determiners and the final "." flip **≤ 0.8%** at every layer from 16 to 60.
   - **The last token** (the answer position) carries the answer only late: 0.8% at L52, **59.9%** at L56, **98.5%** at L60.
   - **Gap:** between L52 and L56 no single tested position carries it alone. Question span 19.5% at L52; whole sentence 29.5% at L52.
   - **Order, not absolute position:** a donor shifted by 15 tokens keeps the effect (shifted twin 90.8% at L48; shifted same-roles 0.3%).
   - **Cross-voice same-roles donor:** it rises later than the twin (49.5% at L44, 80.5% at L48).
3. **One mean-difference vector moves it.** On held-out items at L48, the first-mentioned entity gets +α·Δ and the second −α·Δ. Δ_order is the mean state at second mention minus first, from train items.
   - **On held-out actives/passives:**

     | Vector, α | Flip % [95% CI] |
     |---|---|
     | Δ_order (pooled over 4 constructions), α 1 | 54.1 [48.8, 59.4] |
     | Δ_order, α 1.5 | 91.9 [88.4, 94.7] |
     | Δ_order, α 2 | 98.4 [96.9, 99.7] |
     | Δ_order estimated from A/P only, α 1 | 87.2 [83.4, 90.6] |
     | Δ_subject, α 1 | 0.0 |
     | Δ_role, α 1 | 0.3 |
     | 5 random directions (Δ_order's norm), zero vector, α 1–2 | ≤ 0.6 at L48 (≤ 4.4 in the band) |

     α = 0.5 does nothing (0.0% for Δ_order and Δ_order^AP). That fits two entities pushed to the same order value, so the answer stays put.
   - **Transfer at α = 1** (Δ_order), in constructions that pass gate 1: object and subject clefts 45.7%; comparatives 51.1%; left/right 56.2%; in front/behind 40.6%. On ditransitives in the to-frame, swapping entities 1 ↔ 2 moves the answer 88.8% and keeps the untouched question about 100%.
     - That is 5 of 5 gated targets ≥ 30%, so the plan's *general vector* condition holds, with random and zero vectors as the controls.
     - On correctly answered items of the gated-out constructions (exploratory): topicalised 42.3%, all clefts incl. by-cleft 46.4%, both ditransitive frames 85.4%.
   - **The literal plan rule fails.** The plan also lists PC1 (Dai et al.'s ordering direction) as a control. Only +PC1 was run, not ±PC1 as planned, and it flips 52.2%.
     - PC1 is nearly the same direction: centred per entity, the entity states' first principal component has cosine 0.997 with Δ_order and correlation 0.98 with order, and holds 33.5% of the variance.
     - Treating it as a baseline rather than a control is a post hoc change (log.md (2)). Its success points the same way as Δ_order.
   - **A 7-layer band (L44–50)** works at α 0.5 (79.7% on A/P) and overshoots at α ≥ 1. Each layer's addition accumulates, and invalid answers reach 99% (e.g. Δ_CAP at α 1.5 on actives).
   - **Invalid answers in single-layer edits** stay low but aren't zero: at most 14.1% (Δ_CAP α 2 on topicalised); 161 of 52,938 V rows at L48 overall.
4. **A transferable order direction from CAPITALS names, which doesn't move CAPITALS answers** (follow-up F1, exploratory).
   - **CAPITALS → events:** Δ_CAP, estimated from the name tokens of Feng & Steinhardt-style contexts ("Alice lives in the capital city of France. Bob…"), moves event answers. It flips held-out actives/passives 89.4%, comparatives 100% and ditransitives 93.3%. Its cosine with Δ_order is only 0.45, so it is a related direction that transfers, not the same one.
   - **Inside CAPITALS** (bold prefill, accuracy 100%):
     - Δ_CAP on the names flips **0%** at every α. So do Δ_order and Δ_order^AP.
     - Patching the names from the other order flips **0%** at L40–52.
     - Editing names and countries together moves the answer: 46% at α 2 for vectors; 20% at L48 for the names + countries patch.
   - **The planned positive control (≥ 50%) fails.**
   - **What is shown:** name-only edits don't move CAPITALS answers.
   - **What is not shown:** that the countries alone mediate retrieval (no countries-only arm), or that CAPITALS never reads name order. Retrieval through the country tokens is a hypothesis, in line with the mixed mechanisms of Gur-Arieh et al. (lit-review.md).
   - **It doesn't weaken the event result.** The plan used the positive control to interpret a vector null, and the event vector isn't null.
5. **Three entities.** In ditransitives the order manipulation works well for the first two mentions and weakly for the third.
   - **Pair swaps** (full patch, L48; donor's answer reached, by the question whose answer the swap changes): 1 ↔ 2: agent 100%, theme 80%, goal 44% (the goal is second only in the double-object frame, which failed gate 1 on goal questions); 1 ↔ 3: agent 100%, goal 87%, theme 84%; 2 ↔ 3: theme 63%, goal 35%.
   - **Vectors:** Δ₁₂ moves 86.5%; Δ₂₃ moves 10.3% at α 1 and 71% at α 2.
   - **Dative shift** (roles kept, theme/goal order swapped) moves the answer to the swapped partner only 28–30%.
   - **Additivity is vacuous:** Δ₁₃ = Δ₁₂ + Δ₂₃ holds by construction for mean differences over the same sentences, so the plan's additivity check says nothing.
6. **Baselines** (decodability is not use). The main run's probe evaluation dropped the fitted intercept, a bug found in review. L44 and L48 were rerun from the saved states with the intercept ([`p40/probes_rerun.py`](p40/probes_rerun.py) → [`results/probes_L44_L48_fixed.json`](results/probes_L44_L48_fixed.json)). Other layers are from the uncorrected evaluator (`results/probes.json`) and are indicative only.
   - **Order** decodes from the entity tokens at 1.00 at L44/L48 (held-out items and new frames). The uncorrected run gives ≥ 0.96 at every layer from 16, including layers where patching does nothing (L16–24: ≤ 2%).
     - An order probe trained on actives/passives transfers to held-out object and by-clefts at 0.98–0.997 at L44/L48. Uncorrected: ≥ 0.94 from L16 to L48, then decaying to 0.52 by L60.
   - **Subject and role** probes (L44/L48, corrected) reach 1.00 and 0.85 on held-out items of the trained constructions. They fail on new frames: subject 0.25–0.27 on subject cleft/topicalised (below chance), role 0.37–0.41.
   - **Lens readouts** of Δ_order capture only 3–6% of its norm² (NNOMP at k = 25).
     - The J-lens top tokens of +Δ_order (second minus first mention) are pair and ordinal words: "两者"/"二者" ("both"/"the two"), "前者" ("the former"), " entrambi", " Keduanya". Its −Δ side is person names.
     - The logit lens gives noise.

**What it changes.**
- **For the sprint question** "how is role binding represented outside the global workspace?", there is a concrete intervention target in this model. Mention-order manipulations at the two entity tokens (L44–50) control the agent/patient answer across five constructions, two converse relations and ditransitives, and one mean-difference vector reproduces them. The answer reaches the last token only from L56. Whether this is *the* representation of role, and how the construction maps order to role, is not established.
- **Against the plan's decision table:** the graduation criteria were **not met as written**.
  - the factorial ranking criterion can't be evaluated in the gated pool, and is just missed in the sensitivity pool (48.6%);
  - the literal vector-control rule fails because PC1 ≈ Δ_order;
  - the positive control fails.
- **Recommendation (research judgement, not a plan outcome):** the strength and consistency of the order-sensitive edits (single-feature 59% vs 1% vs 1%; Δ_order 92% at α 1.5 with random 0%; transfer to 5 of 5 gated targets) justify a **focused follow-up study**. It should:
  - add a by-cleft and topicalised prompt format that passes gate 1, so the factorial is complete;
  - add countries-only CAPITALS arms;
  - localise the reader between L52 and L56;
  - run Gurnee et al.'s J-coordinate clamp downstream of a Δ edit;
  - test whether a J-lens monitor can see an order swap. Δ_order's J-lens share is about 4%, and its top tokens are "both / the former".
- **Limits:**
  - CAPITALS-style retrieval isn't moved by name-only edits;
  - the third of three entities responds weakly;
  - clefts reach only about 50% even under their own twin patch.

## Setup (as run)

- **Model:** `Qwen/Qwen3.6-27B` @ `6a9e13b`, bf16, sdpa, thinking off, one H100 80GB (RunPod secure cloud, $3.99/h).
  - P36's loader, hooks and render: layer L is the output of block L; a single prefill forward, prefill "The"; we read the next token.
  - J-lens `neuronpedia/jacobian-lens` @ `b25d72a` (n1000), for the readouts only.
- **Stimuli:** [`data/stimuli.json`](data/stimuli.json), sha256 `796d55b0…`, seed 20261013, built by [`p40/stimuli.py`](p40/stimuli.py). 3,120 sentences, 6,960 prompts:
  - **Transitives:** P36's 100 items in six constructions × roles kept/reversed (1,200), plus actives/passives shifted by a 15-token clause (400).
  - **Relations:** 100 two-place relations with a converse (5 comparatives, left/right, in front/behind): 400.
  - **Ditransitives:** 60 three-person send/show/bring items, both dative frames, all 6 role orders: 720.
  - **CAPITALS:** 100 items × 2 bindings × 2 orders: 400.
  - Held-out entities and verbs as in P36.
- **Arms:** plan.md. Rows: `results/rows/*.jsonl` (gitignored, hashed in `results/MANIFEST.sha256`).
  - **F:** 66 construction cells × both / first / second entity at L48 and L44; both entities also at L40 and L52.
  - **V:** Δ_order, Δ_order^AP, Δ_subject, Δ_role, Δ_rel, Δ_CAP, Δ₁₂/Δ₂₃/Δ₁₃, PC1, 5 random, zero; α ∈ {0.5, 1, 1.5, 2}; L48 and a band L44–50.
  - **L:** 16 layers × 11 sites + 3 donors.
  - **T:** three swaps + dative.
  - **C:** CAPITALS other-order patch.
  - **B:** probes and lens readouts.
- **Scoring:** no judge; token identity. Intervals: 2,000 item-grouped bootstrap resamples, seed 20261013 ([`p40/analyze.py`](p40/analyze.py) → [`results/summary.json`](results/summary.json)). F1 is in `results/followup_cap/`.

## Gates and checks

| Check | Result |
|---|---|
| Smoke: identity patch and zero-vector add | max \|Δlogit\| = 0.0 for both (pass) |
| Smoke: first 20 prompts per family | trans, relations, ditransitives 20/20. CAPITALS 14/20: the model bolds the capital (" **"). CAPITALS was dropped from the main run and rerun in F1 with "… city of **" (100%) |
| Gate 1: unpatched accuracy ≥ 90% per question | pass: active 1.00, passive ≥ 0.99, object cleft ≥ 0.985, subject cleft ≥ 0.98, relations ≥ 0.946, ditransitive to-frame ≥ 0.997, shifted A/P ≥ 0.94. **Fail:** topicalised 0.865/0.89, by-cleft agent 0.85, double-object goal 0.73, CAPITALS (main run) 0.88/0.81. Failing ones are excluded from F's pool; V and T rows are only for correctly answered recipients |
| Gate 2: A/P twin, both entities, L48 ≥ 80% | 95.2% (n 796; P36 95.2%) |
| Pool entry: own twin patch ≥ 50% at L44/48 | active 96.2%, passive 93.9%, object cleft 52.5% (pass) |
| Frame-only cells (subject cleft ← active, topicalised ← object cleft, roles kept; pattern 000) | 0.0–3.2% (no frame artefact) |
| Invalid answers (neither option) | L48 V: 161 / 52,938 rows, max 14.1% (Δ_CAP α 2, topicalised); band at α ≥ 1 up to 99% (overshoot) |

## Predictions against outcomes

| Prediction (plan, before data) | Outcome |
|---|---|
| A general order-type index explains P36's swap (0.45) | **Partly.** The vector works in frame and transfers to 5 of 5 gated targets with random/zero controls; the literal rule fails because PC1 (cos 0.997 with Δ_order) also flips |
| Order/subject rather than role (0.85) | **Yes** (role-only 1.0%) |
| Order rather than grammatical subject (0.55) | **Yes** (subject-only 1.0%) |
| An additive vector works within frame (0.7) | **Yes** (Δ_order 54% at α 1, 92% at α 1.5; Δ_order^AP 87%) |
| Δ transfers to or from CAPITALS ≥ 30% (0.4) | **Yes**, one way: CAPITALS → events 89%; events → CAPITALS 0% |
| F verdicts: order index 0.35 / subject 0.25 / mixed or construction-specific 0.25 / role 0.05 / gates fail 0.10 | Closest to **gates fail**: the criterion can't be evaluated in the gated pool. The post hoc rule gives "order index"; the sensitivity pool doesn't meet it (48.6%) |
| CAPITALS positive control passes at one layer or the band (0.75); at one layer (0.5) | **No** (names only 0%; names + countries best 46%) |
| Dative order-only donor moves theme/goal ≥ 50% (0.45) | **No** (28–30%) |
| Middle entity weakest (0.6) | **Yes** (2 ↔ 3 swaps 35–63%, Δ₂₃ 10% at α 1) |
| L: only the entity tokens reach 50% in the sentence; the last token passes 50% only from L50, within 4 layers of the entity fall (0.5) | **Yes** (entity sites only; the entity patch falls at L52, the last token passes at L56) |
| B: order probe transfers ≥ 0.8 to new frames (0.6) | **Yes** (≥ 0.97 on subject cleft/topicalised; ≥ 0.95 to object/by-clefts) |
| B: no order words in the lens readouts of Δ_order (0.75) | **No** for the J-lens ("前者" the former; "两者"/"二者" both/the two); yes for the logit lens |
| F1: CAPITALS accuracy ≥ 90% with the bold prefill (0.8); Δ_CAP ≥ 50% (0.7); Δ_order^AP on CAPITALS ≥ 30% (0.5); name patch ≥ 50% (0.6) | yes (100%); no (46%); no (0%); no (0–20%) |

## Follow-ups (exploratory)

- **F1** (log.md, 23:4xZ): CAPITALS with the bold prefill. It covers the planned positive control and the events → CAPITALS transfer that the main run dropped. Answer 4 has the result.
- **Not run** (next study, not needed for this probe's question):
  - reader heads (attention attribution from the last token to the entity tokens, L52–56);
  - a localisation sweep on clefts, to see why their own twin patch reaches only about 52%;
  - the J-coordinate clamp downstream of a Δ edit;
  - a J-lens monitor test on order swaps.

## Raw samples

[results/raw_samples.md](results/raw_samples.md) holds 3 rows per arm, drawn by [`p40/raw_samples.py`](p40/raw_samples.py) (seed 20261013; populations in its docstring). Every drawn row is shown.
- **F, pool only:**
  - order-only 3 of 3 moved;
  - subject-only 0 of 3, role-only 0 of 3, all genuine non-moves;
  - twin 3 of 3.
- **V at α = 1 on held-out actives/passives:** Δ_order 1 of 3, random 0 of 3, Δ_CAP 2 of 3.
- **T, dative donor:** 2 of 3 moved to the partner. That is above the arm's 28–30% rate, from a draw of 3.
- **L, last token at L56:** 3 of 3 moved (the arm's rate is 59.9%).
- **F1, CAPITALS:** C names 0 of 3; Δ_CAP 0 of 3; Δ_order^AP 0 of 3.

## Caveats

- **Observations:**
  - one model, single-token entities, one question template per family, single-layer patches (plus one band);
  - three constructions and CAPITALS (main run) failed gate 1, so the primary pool has three constructions, and each lacks one single-feature cell;
  - the sensitivity pool lands just under the 50% bar.
- **Hypotheses:**
  - "relational index read by the answer position" summarises the patching pattern; no reader heads were identified;
  - the CAPITALS reading (retrieval via the country tokens) rests on name-only edits doing nothing and names + countries edits doing something; there was no countries-only arm;
  - the clefts' lower ceiling is unexplained.
- **Post-hoc analysis changes** (log.md, "Analysis corrections"), each reported next to the original:
  1. the verdict's agreement rule;
  2. PC1 treated as a baseline;
  3. the dative "moved to partner" measure;
  4. the vacuous additivity check;
  5. the probes' intercept fix (L44/L48 rerun from saved states).
- **Planned items not run** (review r1):
  - −PC1: only +PC1 was run;
  - the smoke unit's pursuit check: the plan listed it, the session never ran it. The lens step's NNOMP ran (its KKT values weren't recorded), so the 3–6% shares are unverified fits.
- **Design changes during the run** (log.md):
  - `gpu_hours` 2.0 (plan 1.5);
  - download gate 80 MB/s;
  - a failing non-transitive family drops only its own arms;
  - the CAPITALS prefill.
- **The vectors** are in `results/vectors.safetensors`, which is gitignored by the repo's `*.safetensors` rule and backed up with the rows. Their norms and cosines are in `results/vectors.json`.

## Cost and wall-clock

| Session | Pod time | $ | Notes |
|---|---|---|---|
| Three gate-refused hosts (22:08–22:21Z) | 0.21 h | 0.86 | 100.7, 114.4 and 108.5 MB/s against 150 |
| Attempt 1 (22:24–22:40Z) | 0.30 h | 1.22 | download 8.4 min, load 0.4, capture 1.6, smoke 0.2 → rc 3 (CAPITALS 4/20) |
| Main run (22:47–23:40Z) | 0.98 h | 3.90 | download 6.4, load 0.4, capture 1.8, smoke 0.3, baseline 0.6, gate 2 0.1, vectors 0.4, F@48 5.7, V@48 4.1, L 12.9, T 1.2, F@other 9.3, V@band 4.1, probes + lens 0.5 min; GPU idle ≈ 7 min (download and load) |
| F1 refused push (23:44Z) | 0.04 h | 0.17 | uncommitted analysis edit at the runner's second clean-tree check |
| F1 run (23:47–00:05Z) | 0.30 h | 1.20 | download 6.7, load 0.4, capture 1.5, CAPITALS arms 2.2 min |
| **Total** | 1.84 h | **7.34** | worktree `logs/costs.jsonl` |

Patched forwards ran at 0.54 s per batch of 128 (≈ 240 per second).
- The three model downloads (21.5 min, ≈ $1.43) and the refused hosts and push ($1.03) were a third of the spend.
- The CAPITALS smoke failure cost another $1.22 (its download included above).
- **Avoidable** (review r1, about $1.1):
  - refused hosts, $0.86: choose the download gate from the run's time budget, not the default 150 MB/s;
  - the dirty-tree refusal, $0.17: don't edit the probe dir while `go` launches;
  - capture before the prompt smoke on attempt 1, about $0.11: run the prompt checks before captures.

## Review

One cross-review round by gpt-6-astra on the whole branch (code, results, note): `logs/reviews/20261014-P40-branch-r1.md`. **Verdict: REVISE.**
- **Recomputation:** the reviewer recomputed the headline rates from the row files, and all matched. Covered: F single-feature cells, V Δ_order / Δ_CAP / controls, L entity and last-token patches, F1 C and cap_both, the LPM coefficients, the bootstrap intervals, and the $7.34 spend.
- **BLOCKING (3), all fixed:**
  1. The F verdict relaxed the plan's ranking criterion. The note now reports it as not evaluable as written, with the post hoc rule labelled exploratory; tests cover missing cells and a weak second-mention effect.
  2. Claims exceeded what failed patches show (subject/role "unused", "only entity tokens", CAPITALS country mediation, "same direction"). These are rephrased as the interventions' effects, with the hypotheses marked.
  3. "Graduate by the decision table" was unsupported. The note now says the criteria weren't met as written, and recommends a focused follow-up study as a judgement call.
- **MINOR (7):** six fixed, one recorded:
  - the probe intercept: fixed, with a regression test, and L44/L48 rerun from saved states;
  - consistently gated transfer pools: 5 of 5 gated;
  - the unrun −PC1 and pursuit smoke check, now recorded;
  - invalid-answer maxima;
  - other-token effects reported as ≤ 0.8%;
  - probe ranges;
  - the throughput savings: recorded in "Cost and wall-clock", with no code change.
- **NIT (3):** fixed (twin sample 3 of 3, 1.84 h, PC1 cos 0.997 and 33.5%).

A second round wasn't run. AGENTS.md calls for one only when a fix changes a headline number, and none changed. The fixes changed verdict wording and baseline probes, not flip rates.

## References

#### Feng & Steinhardt 2024
"How do Language Models Bind Entities in Context?", ICLR 2024, arXiv 2310.17191 (`papers/interp_methods/` row 49): binding IDs, mean-difference interventions, CAPITALS.

#### Dai, Heinzerling & Inui 2024
"Representational Analysis of Binding in Language Models", arXiv 2409.05448 (ordering-ID principal direction; read as a fetch summary in the launching session's review, lit-review.md).

#### Gur-Arieh et al. 2025
"Mixing Mechanisms: How Language Models Retrieve Bound Entities In-Context", arXiv 2510.06182 (positional, lexical and reflexive mechanisms; fetch summary, lit-review.md).

#### Gurnee et al. 2026
"Verbalizable Representations Form a Global Workspace in Language Models", transformer-circuits.pub/2026/workspace (`papers/interp_methods/` row 1): J-lens; binding named as open; the J-coordinate clamp.

#### P36
[probes/P36-workspace-binding-causal/note.md](../P36-workspace-binding-causal/note.md): the residual vs J-lens split at the entity tokens, and the cross-voice donors this probe generalises.
