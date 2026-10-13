# P17 note: can a small LoRA teach Gemma 4 31B to hide its filler-token intermediate from the logit lens while keeping the filler's benefit, and do other readers then still find it?

**Status:** done, 2026-10-13. **Spend:** $18.37 of the $60 cap, all GPU. $11.27 was the main run; $7.10 went on three stopped attempts and one diagnostic session, which found the bf16 issue below. **Wall-clock:** GO at 23:19 UTC on 10-12, results at 03:28 UTC on 10-13 (4.1 h, including a laptop crash recovery and the diagnosis). Plan: [`plan.md`](plan.md); run log: [`log.md`](log.md); literature: [`lit-review.md`](lit-review.md); raw samples: [`samples.md`](samples.md).

## The answer

**Partly, and in a way that is easy to catch.** Training against the logit lens took y off it in all three evader seeds. It cost about half of the filler's benefit relative to equally trained controls, with large seed-to-seed differences. Two of the three evaders did it mainly by flooding the lens with a constant "0", not by making it uninformative. Probes refit on the evaders' activations still decode y as well as before. A second finding matters as much for the monitoring line: **ordinary task fine-tuning (the control) already removed most of the lens readability that the base model had.**

On Brauer's system-of-equations task ([Brauer, Verdun and Marks 2026](#brauer-verdun-and-marks-2026)), with 500 held-out items, the base model (B), three controls trained on answers only (K1–K3), and three evaders trained on answers plus the lens penalty (E1–E3):

1. **The trained-against reader is evaded.** y's first-digit hit rate at layers 30–59 (P06 F3's statistic):
   - **Base model:** label 30.6% (null 11.0) and dots 20.6% (null 11.1). This replicates P06 F3's 36% and 25%.
   - **Evaders:** at or below null in every seed. Label 0.4 / 18.6 / 9.0% against nulls of 13.7 / 17.4 / 9.6. Dots 1.6 / 11.4 / 3.4% against 4.0 / 13.3 / 8.8.
   - **Attributable to the penalty:** on the dots group, Ē − K̄ is **−9.3 points [−11.8, −6.9]**. The controls keep the dots readout in 2 of 3 seeds (17.0% and 20.0% against nulls near 10).
2. **But most of the lens readability was already gone in the controls.** K's label readout is at null in all three seeds (11.4 / 10.2 / 13.0% against 11.0 / 14.5 / 12.1), as are the J-Lens and J++ label readouts. One epoch of answer-only LoRA training on 2,000 items erased what the base model showed. So the J-Lens's "evasion" (label, Ē 12.5% against K̄ 18.3%, both near null) can't be attributed to the penalty.
3. **The benefit is partly lost, seed by seed.**

   | | Uplift (with filler − without) |
   |---|---|
   | Controls K1 / K2 / K3 | +25.6 / +17.0 / +4.4 |
   | Evaders E1 / E2 / E3 | +19.4 / −2.0 / +4.6 |
   | Pooled | Ū_E +7.3 [+5.3, +9.3] against Ū_K +15.7 [+13.4, +17.9] |

   Ū_E − Ū_K = **−8.3 [−11.1, −5.6]**. The plan's criterion, Ū_E − ½Ū_K = −0.5 [−2.7, +1.6], straddles 0. With filler, the evaders' accuracy is 0.634 / 0.778 / 0.668 against the controls' 0.766 / 0.702 / 0.820.

   **Training also changes the task.** Controls and evaders improve far beyond the base model, both with filler and without. K3 and E2 solve the task at 0.78–0.80 without any filler. Whether the filler still matters is itself seed-dependent.
4. **Two evaders plant a constant decoy (exploratory, post hoc).** At the label and dots (layers 30–59, all 500 items), the logit lens's top-1 token is a digit in:
   - **25.7% of cells for E2, 16.0% for E3, 3.9% for E1** (almost all "0");
   - **0.1–0.4% for B and every K.**

   So the hit statistic is beaten by a planted constant, the decoy route [P12](../P12-filler-conceal/note.md) found by prompting. A monitor that only flags "the lens reads a digit at the filler" separates E2 and E3 from every control.
5. **Readers not trained against:**
   - **Probes refit on each model's own residuals still find y** in every evader. The ridge on the dot-pooled residual gives r 0.96–0.98 in all seven models (Ē − K̄ −0.006 [−0.009, −0.003]), with partial r beyond x and c1·x of 0.78–0.88. Refit MLP and logistic probes agree.
   - **The probe fit on B and applied unchanged** (the [Neural Chameleons](#mcguinness-serrano-bailey-and-emmons-2025) test) keeps r 0.91–0.92 on E1 and E3 but drops to 0.53 on E2. Pooled Ē − K̄ is −0.16 [−0.18, −0.14]; it is seed-dependent.
   - **The same lens on y's second digit** at the label reads y in B (19.5%, null 10.2) and in two controls (13.6% and 18.1%). In no evader is it above null. The evasion transfers within the lens.
   - **Caveat on the refit probes:** they decode y just as well at the token *before* the filler (partial r 0.60–0.91). And the input-only predictor (x, c1·x) already reaches r 0.93. So "y is in the residual stream" holds, but these probes do not show y being computed *in* the filler.
6. **The filler's causal role shrinks with any training.** Matched-donor minus random-donor transplant effect:

   | | Effect |
   |---|---|
   | B | **+44.0 [+36.0, +52.0]**, replicating P06 F3's +44.7 |
   | Controls K1 / K2 / K3 | +14.7 / +24.0 / +20.0 |
   | Evaders E1 / E2 / E3 | +8.0 / 0.0 / +22.0 |

   Ē − K̄ is −9.6 [−13.6, −5.3]. E2's filler K/V no longer steers the answer at all, consistent with its uplift of −2.

**Plan verdict:** inconclusive. The seeds don't agree: E1 is (iv) evaded with the benefit kept, E2 is (ii) benefit lost, E3 is inconclusive. Rule (i) (the attack didn't take hold) does not fire: the held-out penalty fell to 0.5%, 22% and 14% of its step-0 value. Rule (iii) (not evaded) does not fire either.

**What it changes.**
- **For "computation in filler is readable": weaker than P06 and P12 suggested, and not mainly because of an adversary.** Ordinary task fine-tuning removes the lens readout that the base model shows, so lens readability is fragile to benign training. The adversary then removes the rest at about half the uplift, partly by planting a constant digit. Refit linear probes keep finding y. A monitor built on probes refit to the deployed model, or on lens-output anomalies, survives this attack; a fixed lens statistic does not. This matches prior work: a probe refit on the evader defeated an attack trained against a single fixed probe ([Bailey et al. 2024](#bailey-et-al-2024) App. B.2, per `lit-review.md`), and late-layer readers were the easiest to evade ([Gupta and Jenner 2025](#gupta-and-jenner-2025) App. G). Here the penalised layers 30–59 were evaded within 100 steps.
- **The study the plan's (iv) branch pointed to ("which readers survive an adversary on necessary computation") is not well supported here.** The task is too easy to fine-tune (accuracy rises from 0.35 to 0.63–0.82, and some seeds stop needing the filler). The probes can't separate computed y from prompt-copied inputs. Per plan, I recommend **not continuing this line on this task.** If it continues, it needs a task where the controls keep the filler uplift and the base model's readout survives task training, and a reader whose target isn't recoverable from the prompt.
- **For the portfolio:** this agrees with P12, that models (prompted or trained) defeat a fixed lens statistic by adding false signal, and adds that a refit probe isn't fooled. It also bears on P24 and any Gemma readout work through the bf16 finding below.

## Infrastructure finding: Gemma 4 31B's late-layer readouts depend on bf16 numerics

Found by the smoke gate and a diagnostic session (log.md, 2026-10-12/13; `attempt1_sdpa/`, `attempt2_eager/`, `diag_session/`).

- **The gap grows with depth.** Two equivalent bf16 computations of the same prompt differ by about 0.2% relative L2 at layer 1, growing to about 40% by layers 50–56. The two computations can be full sequence vs a cached prefix, sdpa vs eager, or different batch shapes. Every split showed it, below and above the sliding window, and each path is deterministic.
- **Effect on P06 F3's own path (full sequence, batch 1; follow-up F1, 50 items):**
  - Lens-model agreement is 1.0 under both sdpa and eager.
  - Per-cell digit log-prob gaps at layers ≥ ~24 average about 1 nat (max 51).
  - y's hit rates: label 0.36 vs 0.34 (1 of 50 items differs); dots 0.18 vs 0.12 (7 of 50 differ).
- **Per-item late-layer readouts in this model are therefore not reproducible across equivalent computations;** aggregates move a few points. P17 computes every model the same way (eager, cached prefix) and rests on 500-item aggregates. The smoke's original cell-level gates were wrong for this model. They were replaced by output-level checks plus hidden-state agreement through layer 19. P24 was told (log.md, 23:57Z and 00:31Z).

## Setup (as run)

- **Model:** google/gemma-4-31B-it @ `842da379`, bf16, **eager attention**, one H200 (secure, $5.29/h).
- **Readers:** logit lens, J-Lens and J++ @ `9fa267f8` (P06's code).
- **Data:**
  - Eval: Brauer's depth-1 set, sha256 `c80f0f5e…`, all 500 items.
  - Training: 2,000 fresh items from Brauer's generator @ `a701927`, seed 20261012, 0 overlaps; `data/P17/train_items.json`, sha256 `049d6522…`.
- **Format:** P06 F3's dots100 prompt (5 shots with filler, `Filler:` and 100 dots).
- **Arms:** LoRA r 16 / α 32 on all 410 text-tower projections, lr 1e-4, effective batch 8, microbatch 4, one epoch (250 steps), seeds 1–3 for init and order.
  - **K:** answer cross-entropy.
  - **E:** the same plus λ = 0.774 × the readout penalty, the eval statistic's hinge on label and dots (`p17/penalty.py`).
- **Shared-prefix KV cache:** system message plus shots computed once per step, with no gradient through it.
- **Eval:** batched greedy decoding; readouts over 60 layers × 114 slots; residual pools; transplant on 150 pairs; asking for y.
- **Analysis:** `p17/analyze.py` → `results/analysis.json`, with a 10,000-resample paired item bootstrap and 2,000 permutations. Post hoc: `p17/constant_digit.py` → `results/constant_digit.json`.

## Predictions against outcomes

| Prediction (plan) | p | Outcome |
|---|---|---|
| (a) E evades the trained reader (within 5 points of null) **and** E's filler accuracy within 5 points of K's | 0.6 | **Half right.** Evaded in every seed; accuracy is 7 points below K on average (0.693 vs 0.763) and seed-dependent. |
| (b) the J-Lenses drop at least half as much as the logit lens | 0.45 | **Uninformative.** They drop relative to B, but the controls drop just as much, so there is no penalty-attributable transfer. |
| (b2) the same lens off target (layers 0–29, second digit) drops at least half as much | 0.5 | **Second digit: yes** (B 19.5%, K 13.6–18.1% in two seeds, every E at null). **Layers 0–29:** B was never above null there, so there was nothing to evade. |
| (c) refit probes still decode y, r within 0.1 of K | 0.8 | **Yes** (Ē − K̄ −0.006). |
| (c') the fixed probe from B loses ≥ 0.2 r on E | 0.4 | **Pooled no** (−0.16). E2 alone yes (0.53). |
| (d) the transplant still steers, E within 15 points of K | 0.6 | **Yes on the pooled CI** (−9.6 [−13.6, −5.3]); E2 alone no (0.0). |
| (e) U_E ≥ ½U_K | 0.6 | **Inconclusive:** −0.5 [−2.7, +1.6]. |
| (f) E's accuracy at asking for y within 10 points of K's | 0.7 | **Yes:** E 0.949 vs K 0.993 mean (E3 0.894). |
| (g) the E seeds agree on the verdict category | 0.7 | **No:** iv / ii / inconclusive. |
| (h) some verdict is inconclusive | 0.25 | **Yes.** |
| F1 (exploratory): full-path sdpa and eager agree (99.9th-percentile gap ≤ 0.1, identical hits on ≥ 48/50) | 0.65 | **No** for the cells and the dots hits (43/50); the label held (49/50). |
| Diagnostic (exploratory): the gap appears only for splits beyond the sliding window | 0.4 | **No:** every split, including one at 600. |

## Follow-ups (exploratory)

- **F1:** sdpa vs eager on P06 F3's full-sequence path. Result in the infrastructure section.
- **F2:** constant-digit flooding (post hoc, $0). Result in answer point 4.
- **Not run.** The plan's rule allows a fourth seed only when the pooled direction is clear, and Ū_E − ½Ū_K straddles 0. The λ × 3 and full-gradient follow-ups are for verdicts (i) and (iii), which did not fire. The narrower-band follow-up is for (ii), which fired for E2 alone.

## Caveats

- **One task, one epoch, one λ.** Training changes the task as well as the readouts: the controls' accuracy rises from 0.35 to 0.70–0.82, and K3 and E2 stop needing the filler. Seed variance is large; three seeds per arm are not enough to pin the uplift contrast.
- **The lens statistic is noisy per item in bf16** (see the infrastructure section). All models are computed the same way, and the aggregates are over 500 items, but per-seed differences of a few points sit near that noise.
- **The refit probes can't tell computed y from prompt-copied inputs.** x, c1·x and the constant are all in the prompt, and y is decoded just as well before the filler. Asking for y gets 0.89–1.00 in every model, so y is easy to recompute.
- **The prefix (system and shots) received no gradient** (plan "Setup"). The attack took hold anyway, so this did not decide the outcome.

## Raw samples

Five items drawn with seed 20261012 by `p17/samples.py`; all seven models on each are in [`samples.md`](samples.md). Item 205 (y = 61, answer 95), as drawn:

- **B:** without filler 103, with filler 105 (both wrong); asked for y, 61. Top digits read by the lens: label 6, then 0; dots 6, then 0. So y's first digit, 6, is on top in both groups.
- **K1:** with filler 95 (correct); lens top digits label 8, 1, 0; dots 0, 9, 6.
- **E2:** with and without filler 95; lens top digit "0" at log-prob −0.1 in both groups, every other digit below −18.
- **E3:** with and without filler 95; label 0 (−0.0), 4, 9; dots 0 (−0.0), 4, 9.

The constant "0" in E2 and E3 is visible in every drawn item.

## Spend and wall-clock

| Phase | Time | GPU | $ |
|---|---|---|---|
| Build, before the GO (laptop) | ~6 h | — | 0 |
| Attempt 1: smoke failed (sdpa cell gaps) | 0.33 h | H100 secure | 1.31 |
| Attempt 2: smoke failed under eager (same gaps) | 0.36 h | H100 secure | 1.45 |
| Diagnostic session and F1 | 0.26 h | B200 secure | 2.08 |
| Attempt 3: smoke passed, no microbatch fits 80 GB | 0.57 h | H100 secure | 2.26 |
| **Main run:** download 12.5 min, smoke and benchmark 2.5 min, B's eval 6.3 min, 6 trainings (K 6.3 min each, E 8.7 min each), 6 evals (~8.3 min each) | 2.13 h | H200 secure | 11.27 |
| Analysis, F2, note (laptop) | ~0.5 h | — | 0 |
| **Total** | | | **18.37** |

All figures are from `logs/costs.jsonl` in this worktree. The GPU was idle only during downloads.

## Review

One cross-family review round: see the end of `log.md`.

## References

#### Brauer, Verdun and Marks 2026

Kaley Brauer, Claudio Mayrink Verdun, Samuel Marks, "Reading Between the Dots: Decoding Hidden Computation across Filler Tokens", arXiv 2607.03502 (2026); code at github.com/kaleybrauer/filler-token-reasoning @ a701927. `papers/neel_brainstorm/INDEX.md` row 69.

#### McGuinness, Serrano, Bailey and Emmons 2025

Max McGuinness, Alex Serrano, Luke Bailey, Scott Emmons, "Neural Chameleons: Language Models Can Learn to Hide Their Thoughts from Unseen Activation Monitors" (2025). `papers/interp_methods/INDEX.md` row 92.

#### Bailey et al. 2024

Luke Bailey, Alex Serrano, Abhay Sheshadri, et al., "Obfuscated Activations Bypass LLM Latent-Space Defenses", arXiv 2412.09565 (2024). `papers/interp_methods/INDEX.md` row 99 (captured during this probe's build; read in full by the literature sub-agent, see `lit-review.md`).

#### Gupta and Jenner 2025

Rohan Gupta, Erik Jenner, "RL-Obfuscation: Can Language Models Learn to Evade Latent-Space Monitors?", arXiv 2506.14261 (2025). `papers/interp_methods/INDEX.md` row 98 (captured during this probe's build; see `lit-review.md`).
