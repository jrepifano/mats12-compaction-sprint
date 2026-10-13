# P33 note: is the filler uplift question-specific computation in the dots?

Probe tier, 2026-10-13. Plan: [`plan.md`](plan.md) (published to main as 70e99375 before any data; Jacob's "go" in chat, wave 4). Run log: [`log.md`](log.md). Literature: [`lit-review.md`](lit-review.md). Spend **$71.30** of a $100 cap (raised from $60, log.md), all GPU. Wall-clock about 8.5 h, 14:52–23:35 UTC.

**Setup:**
- **Models:** DeepSeek-V4-Flash @ 60d8d707 on one B300 (FP8/FP4, eager attention) and Gemma 4 31B @ 842da379 (sdpa, explicit masks in every arm).
- **Task:** Brauer et al.'s depth-1 variable-binding items ([Brauer et al. 2026](#brauer-verdun-and-marks-2026)), 5 shots, with filler in every turn. There are 200 equation items and 100 copy-only controls ("what is the number for v?"), with exact-integer grading.
- **Mask B** ([Baherwani et al. 2026](#baherwani-goldstein-and-panda-2026), App. C.1): the target turn's dots are blocked from all earlier context. On V4 that covers the 128-token sliding window, every compressed CSA/HCA entry that pools a pre-dot position, and the lightning indexer's scores ([DeepSeek-AI 2026](#deepseek-ai-2026)).
- **Mask Q:** the dots are blocked only from the target turn's own text.

Every number comes from `results/analysis.json` (`uv run python -m p33.analyze`) and `results/stage2/analysis.json` (`uv run python -m p33.analyze2`), both run from the probe directory with PYTHONPATH as in `run.sh`. Intervals are 95% item-bootstrap intervals (10,000 resamples, seed 0), paired by item. The raw rows are in `results/{v4,gemma}/answers.jsonl` and `results/stage2/units.jsonl`, with hashes in `results/MANIFEST.sha256` (session 4) and `results/gemma/MANIFEST.sha256` (session 3).

## The answer

1. **Yes, the uplift needs filler positions that can read the question, on both models. But dots are not special: any span after the question that can read it gives the same uplift.**
   - **V4** (equation items, n = 200, base 0.400):
     - 100 dots add **+0.225 [+0.160, +0.295]**.
     - With the dots blinded by Mask B the gain is gone: maskB100 − base is +0.015 [−0.050, +0.080]; dots100 − maskB100 is **+0.210 [+0.150, +0.275]**; retention 0.07 [−0.28, 0.32].
     - Blinding the dots to the target question turn alone (Mask Q) does the same: retention −0.11 [−0.54, 0.17].
     - The repeated question (rep1, +0.230), the problem repeated twice (rep2, +0.255) and 100 tokens of neutral prose (+0.250) all match the dots (dots100 − rep1 −0.005 [−0.045, +0.035]).
   - **Gemma 4 31B** (base 0.250): the same shape, smaller.
     - dots100 adds +0.085 [+0.030, +0.145], and dots100 − maskB100 is +0.110 [+0.050, +0.170].
     - Retention is −0.29 [−1.78, 0.27].
     - rep1 and neutral prose match the dots (−0.010 to +0.015).
   - **Sensitivity:** the result doesn't depend on V4's occasional visible reasoning without filler ([P32](../P32-filler-mc-candidates/note.md)'s gotcha). On the 175 items with a bare-integer reply in every arm, dots100 − maskB100 is +0.217 [+0.149, +0.286].
2. **Blind dots are harmless up to 100 tokens and harmful beyond V4's 128-token window.**
   - On V4, maskB − base is +0.03 at 32 dots and +0.015 at 100. It is −0.270 [−0.340, −0.205] at 160 and −0.275 at 500, and copy items drop to 0.89 at maskB160.
   - Unblinded, the dots hold the gain at every length (0.600 / 0.625 / 0.630 / 0.645 at 32 / 100 / 160 / 500).
   - *Inference:* once the span is longer than the window, the answer can no longer attend to the question's tokens directly (only through compressed entries). It then depends on the span to carry the question, and blind dots can't.
   - Below the window, Mask B behaves like removing the dots. In Stage 2's discovery half, Mask B and the knockout that hides every dot from the answer give the same accuracy (maskB − knockout(all) −0.03 [−0.10, +0.03]). So at 100 dots, "Mask B removes the uplift" is not "Mask B breaks the model".
3. **Stage 2 (V4, 100 dots): the dots hand the answer position the donor's finished problem, not a y for it to finish. The registered compact-pathway test fails.**
   - **Donor patches, all layers × all dots,** donor's dot states into the recipient's dots:
     - The recipient outputs the **donor's own answer**: diffY 0.63; opChange 0.55 on discovery, 0.48 on confirmation; random 0.55 / 0.39. The donors' unpatched accuracy is 0.53–0.65.
     - The exact hybrid answer (the recipient's final operation applied to the donor's y) is 0.03–0.05.
     - The recipient's own answer falls to 0.00–0.08, its mean log-prob from −1.7 (median −0.2) to about −9. This holds even though the recipient's own question is inside the 128-token window.
     - With y written explicitly, the model applies the operation perfectly (explicit-y 1.00), so the near-zero hybrid rate is not a failure to apply the operation. The dots carry the donor's operation, or its answer, along with y.
   - **Read sites:**
     - Knocking out the answer's reads of the dots in any single band of about 9 layers changes accuracy by at most 0.03, except the last band. Layers 35–42 drop accuracy by 0.12 [0.04, 0.20] and the answer's log-prob by 0.40.
     - All layers together give −0.19. So the read is spread across depth and concentrated late.
   - **Ablation sites:**
     - The rule fixed before data froze b1|all (layers 9–17, all dots). Replacing those dot states with their Mask B values drops accuracy by 0.24 on discovery and 0.25 [0.17, 0.34] on confirmation, as much as Mask B everywhere (0.22 / 0.19).
     - But patching *real donor* states there changes nothing: the recipient answer stays at 0.65, the donor answer 0.00 and the hybrid 0.00 (opChange, diffY and random).
     - So b1 is where blind states derail later layers, not where question content sits: later layers at the dots recompute the content from the context.
   - **Compact pathway** (plan: frozen site ≤ 2 bands and held-out hybrid ≥ half the full-patch rate, above the baselines): **fail**. The hybrid rate at the frozen site is 0.00 against 0.04 for all sites.
   - **Self-patch** leaves every first-token prediction unchanged (20 of 20).

**What it changes.**
- **The plan's branch is "question-specific, no compact pathway → demote algorithm-finding":** demote the hunt for a y-handoff circuit. The dot positions don't pass an intermediate to the answer; they hold problem-level or answer-level content that the answer reads mostly in the last band.
- **Any post-question span works** (rep, neutral prose), so the computation is "extra positions after the question", not something dot-specific. That matches P32's API result on this task.
- **For an algorithm sprint, the object of study is the computation spread over the span positions themselves.** [P06](../P06-filler-token-computation/)'s readers already see y there. The open question this probe could not settle: do the dots hold the *final answer*, or a copy of the problem from which the answer position recomputes? The opChange result rules out "the dots hold only y". The plan's predicted outcome 2 (the uplift is not question-specific) is rejected on both models.
- **Caveat on comparisons:** local V4 gets more no-filler items right than P06's API rows (base 0.474 vs 0.333 on the first 57 items; 8:0 discordant). Local filler uplifts are therefore about 8 points smaller than the API's. Every conclusion here uses within-local contrasts.

## Predictions against outcomes

| Prediction (plan, 2026-10-13, before data) | p | Outcome |
|---|---|---|
| 1. Question-specific: Mask B removes ≥ half the 100-dot uplift on V4, **and** repetition gives less than the dots | 0.50 | **Half true.** Mask B: retention 0.07 [−0.28, 0.32]. Repetition: rep1 = dots (−0.005 [−0.045, +0.035]). As written (a conjunction), false. |
| 2. Largely survives Mask B (retention > 0.5) | 0.35 | **False** on both models |
| 3. Ambiguous: wide intervals, or repetition matches the dots | 0.15 | **True by its second clause.** The registered outcome is 3, with the Mask B half of 1 clearly true. |
| Given 1: compact pathway (≤ 2 bands, held-out hybrid ≥ half the full-patch rate, above the baselines) | 0.6 | **False.** Hybrid 0.00 at the frozen site, 0.04 everywhere. Donors transfer their own answer. |
| Side: Mask B leaves copy accuracy within 3 points | 0.8 | **True at 32 and 100 dots** (1.00). False at 160 (0.89), past the window. |
| Side: repeat ×1 gives some uplift but less than the dots | 0.55 | **False** (equal) |
| Side: Mask Q retains about as much as Mask B | 0.6 | **True** (−0.11 vs 0.07; both ≈ base) |
| Side: Gemma's uplift also mostly removed by Mask B | 0.5 | **True** (retention −0.29 [−1.78, 0.27]) |

**Design changes, all logged before the data they affect:**
- **The gate** (15:34Z, before any accuracy data, after a side-agent note Jacob forwarded): Stage 2 runs on the Mask B clause alone, and the repetition clause is reported as `plan_gate_pass` (false).
- **The API check** was relaxed *after* it failed on run 2's data. The local base was 0.45 against the API's 0.317. It was replaced by P06 F7's existing uplift rule (local +0.20, lower bound 0.067, against API +0.283), and this is disclosed here.
- **The Mask B disruption caveat** (21:25Z, before Stage 2 data): localisation rests on knockouts and donor patches. The ablation site is reported as possibly confounded, and it was (item 3).

## Results tables

**V4 Flash, Stage 1** (equation n = 200, copy n = 100; tokens = median prompt length):

| arm | equation | copy | uplift vs base [95% CI] | tokens |
|---|---|---|---|---|
| base | 0.400 | 1.000 | | 528 |
| dots32 | 0.600 | 1.000 | +0.200 [+0.130, +0.270] | 766 |
| dots100 | 0.625 | 1.000 | +0.225 [+0.160, +0.295] | 1174 |
| dots160 | 0.630 | 1.000 | +0.230 [+0.160, +0.300] | 1534 |
| dots500 | 0.645 | 1.000 | +0.245 [+0.175, +0.315] | 3574 |
| maskB32 | 0.430 | 1.000 | +0.030 [−0.025, +0.090] | 766 |
| maskB100 | 0.415 | 1.000 | +0.015 [−0.050, +0.080] | 1174 |
| maskB160 | 0.130 | 0.890 | −0.270 [−0.340, −0.205] | 1534 |
| maskB500 | 0.125 | 0.970 | −0.275 [−0.340, −0.210] | 3574 |
| maskQ100 | 0.375 | 1.000 | −0.025 [−0.095, +0.045] | 1174 |
| rep1 | 0.630 | 1.000 | +0.230 [+0.165, +0.295] | 964 |
| rep2 | 0.655 | 1.000 | +0.255 [+0.190, +0.320] | 1388 |
| neutral100 | 0.650 | 1.000 | +0.250 [+0.185, +0.320] | 1168 |

**Gemma 4 31B, Stage 1:**
- Equation accuracy: base 0.250; dots100 0.335; dots500 0.380; maskB100 0.225; maskB500 0.175; rep1 0.320; neutral100 0.345. Copy is 1.000 in every arm.
- dots500 − maskB500 is +0.205 [+0.140, +0.270].
- 13 of 200 maskB500 replies are not bare integers ("The user provided a set of", cut off at the token limit).

**V4 Stage 2** (100 dots; discovery = 99 even-idx items, confirmation = 100 odd-idx items; first-token top-1 rates):

| unit | discovery | confirmation |
|---|---|---|
| unpatched accuracy | 0.596 | 0.650 |
| Mask B accuracy | 0.374 | 0.460 |
| knockout, all layers × all dots | 0.404 | (not run) |
| knockout drop, b0 / b1 / b2 / b3 / b4 (all dots) | −0.02 / −0.03 / +0.03 / +0.03 / **+0.12** | (not run) |
| ablation drop, b0 / b1 / b2 / b3 / b4 (all dots) | 0.08 / **0.24** / 0.21 / 0.15 / 0.13 | b1: 0.25 |
| donor all layers × all dots: donor answer / hybrid / recipient answer | diffY 0.63 / 0.63 / 0.00; opChange 0.55 / 0.03 / 0.01; random 0.55 / 0.01 / 0.06; sameY → 0.63 | diffY 0.63 / 0.63 / 0.01; opChange 0.48 / 0.04 / 0.01; random 0.39 / 0.05 / 0.08; sameY → 0.63 |
| donor at b1, all dots: donor answer / hybrid / recipient answer | (not run) | opChange 0.00 / 0.00 / 0.65; diffY 0.00 / 0.00 / 0.65; random 0.00 / 0.01 / 0.67 |
| explicit-y accuracy | 1.00 | 1.00 |

For diffY the hybrid equals the donor's answer (same question). For sameY all three candidate answers are the same number. Early/late dot halves alone give ablation drops of at most 0.10 and knockout drops of at most 0.03 (`results/stage2/analysis.json`).

## Raw samples

Seeded (seed 0, `uv run python -m p33.samples`; full table in [`results/samples.md`](results/samples.md)), not cherry-picked:
- **V4, 6 equation items** (answer; base / dots100 / maskB100 / rep1 / neutral100):
  - eq:187 243: (empty) / 201 / 201 / 201 / 201
  - eq:94 376: all correct
  - eq:195 139: all correct
  - eq:107 537: 519 / 573 / 516 / 519 / 519
  - eq:158 877: 877 / 877 / **889** / 877 / 877
  - eq:36 429: 339 / 439 / 439 / 439 / 339
- **Gemma:** eq:53 91: 89 / **91** / 59 / 91 / 91.
- **Stage 2, opChange donor patched at all layers × all dots, 8 random items:**
  - The patched top-1 is the donor's answer in 5 (eq:70, 189, 119, 75, 163).
  - It is a near miss in 2 (eq:128 528 vs donor 554; eq:111 240 vs hybrid 245).
  - It is another number in 1 (eq:48, 64).
  - It is never the recipient's own answer.

## Follow-ups (not run; exploratory candidates)

- **Per-band donor patches** (late bands only, early bands only), to place the answer-level content in depth at the dots. About $15–20 per pod session, of which about $8 is the 50-minute download.
- **Answer vs problem at the dots:** a reader for the *final answer* (not y) at the dot positions by layer, or donors that change only the definitions far outside the window. This would separate "the dots hold the finished answer" from "the dots relay the problem".
- **Mask B on chain k = 2 at 500 dots,** where P32 found the dots better than other spans (log.md, 17:54Z).

## Spend and wall-clock

| phase | wall-clock (UTC) | $ | outcome |
|---|---|---|---|
| build (masks, items, pod runner, tests; laptop) | 14:52–15:18 | 0 | 35 tests pass |
| run 1, pod lyoo98k5ox07ud | 15:21–15:58 | 5.80 | bad host (CUDA busy after a 32-min download); GPU preflight added |
| session 2, pods etauw3pohbxaur + 9v5l7lu4kngsss | 15:58–18:10 | 0.55 + 19.31 | 89 V4 items; stopped by the API check; controller died, pod idle 17:35–18:10 (about $5) |
| session 3, pod fkt0q97gcvp7nx | 18:14–19:52 | 14.64 | download 50 min; V4 bulk stopped at once (projection bug); Gemma Stage 1 complete (18 min); a same-pod relaunch failed on a free-disk check |
| session 4, pod odajed5w367p1n | 19:52–23:20 | 31.00 | download 49 min; load and checks 9.5 min; V4 bulk 70 min (211 items); Stage 2 67.5 min |
| analysis and note (laptop) | 23:20–23:45 | 0 | |

Of the $71.30, about $23 bought no data: run 1 ($5.80), the refused pod ($0.55), the idle controller gap (about $5) and the V4 part of session 3 (about $11.5).

## References

#### Baherwani, Goldstein and Panda 2026
"Not All LLM Reasoning is Visible in the Chain-of-Thought", arXiv 2607.22925. `papers/neel_brainstorm/INDEX.md` row 74. App. C.1 (Mask B) was read verbatim.

#### Brauer, Verdun and Marks 2026
"Reading Between the Dots: Decoding Hidden Computation across Filler Tokens", arXiv 2607.03502, row 69.

#### DeepSeek-AI 2026
"DeepSeek-V4: Towards Highly Efficient Million-Token Context Intelligence", arXiv 2606.19348, row 85 (attention layout: sliding window, CSA, HCA, indexer).

#### Greenblatt 2025
"Recent LLMs can use filler tokens or problem repeats to improve (no-CoT) math performance", LessWrong, row 70.

#### Leviathan, Kalman and Matias 2025
"Prompt Repetition Improves Non-Reasoning LLMs", arXiv 2512.14982, row 86.

#### P32
`probes/P32-filler-mc-candidates/note.md` (PR #44): on varbind, any 100–500-token span after the question gives the uplift over the API.
