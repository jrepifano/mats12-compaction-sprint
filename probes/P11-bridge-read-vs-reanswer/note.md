# P11: On P03's wrong-bridge items, does the Oracle Lens read the bridge Qwen actually uses, or re-answer the question from Qwen's own knowledge?

**Status:** done, 2026-10-12. Spend **$13.57** of $30, all GPU: two pod sessions, $7.96 + $5.37, plus $0.24 for a lost first launch. No API spend. Plan: [plan.md](plan.md); run log: [log.md](log.md); literature: [lit.md](lit.md); raw samples: [results/raw_samples.md](results/raw_samples.md); review: `logs/reviews/20261012-P11-branch-r1.md`.

## Answer

1. **On these items the Oracle Lens adds nothing over asking Qwen.**
   - Asked hop 1 directly ("Who is the author of X?"), Qwen names its own wrong bridge on **62/63** items (greedy; 63/63 counting 10 samples). Asked inside the two-hop question to "first name the person you take to be the author", it names it on 48/63 (56/63 with samples).
   - The lens, reading layers 40–60 at the last 3 prompt positions, names the bridge on **34/63** (any of 72 reads). Lens − hop-1 self-report: **−0.46 [−0.59, −0.33]**, paired over items.
   - There is **no item** where the lens names the bridge and the hop-1 self-report doesn't.
   - Stock Qwen with no adapter, shown the prompt as text and asked what the assistant is thinking, names the bridge on 62/63. Lens − that text floor: −0.44 [−0.57, −0.32].
2. **When Qwen's bridge is changed by an activation patch, the lens follows the change, but no more than Qwen's own self-report does, and barely more than under a control patch.**
   - **The patch:** the question's activations at layer 32 were replaced with those from a run that had "Note: the author of X is Y" prepended. Qwen's answer flips to Y's nationality on 36/63 items. The lens then names Y on 13/63, and Qwen's bridge on 3/63 (34/63 unpatched).
   - **The control patch** ("a review of X was written by Y") was meant to be inert. It flips Qwen's answer on 16/63, and the lens names Y on 9/63. **Planned contrast, author − control: +0.06 [0.00, +0.14]**; the plan's "read" outcome needed ≥ +0.15.
   - **Graded patches** (25/50/75/100% of the way to the premise run's state): the lens names Y at 0/2/8/11 items (one draw each) as Qwen's flips rise 2/8/29/36. Pooled over items × strengths, the lens names Y on 0.19 of flipped units vs 0.04 of unflipped: **+0.15 [+0.04, +0.28]**. So the lens's report moves with the patch strength.
   - **Asking the model under the same patch does as well or better.** Under the author patch, Qwen's two-hop self-report names Y on 21/63 (any of 11). Lens − self-report on Y: **−0.13 [−0.24, −0.03]**.
   - On the 36 items whose answer the control patch left unchanged, both the lens and Qwen's self-report lose the bridge: the lens 19 → 8, the hop-1 self-report 35 → 8 (lens − self-report 0.00 [−0.17, +0.17]).
3. **What this says about "read vs re-answer".**
   - The design can't separate the two: see "What the design can and cannot separate". The Oracle Lens is a LoRA on Qwen, and both it and Qwen's self-report are functions of the same patched state.
   - What it does show: the lens behaves like Qwen finishing its own computation from that state. That is the equivalence Li et al. predict for a verbalizer that shares the target's weights ([Li et al. 2025](#li-et-al-2025), Appendix I).
   - On the plain prompts the bridge is recoverable from the text by Qwen itself. The lens recovers less of it than Qwen's own text answers do, and adds no item they miss.
   - None of the plan's three outcomes fits exactly (table below). The planned measure is closest to "re-answer"; the follow-ups say "tracks the state, adds nothing beyond the model's own report".
4. **P03's numbers, rescored** for a name-matching bug (aliases such as "Gustave Moreau (French painter)" made "painter" count as the bridge's surname):
   - Oracle Lens 24/63 → **23/63, 0.37 [0.26, 0.49]**;
   - lens − either held-out predictor +0.25 → **+0.24 [+0.10, +0.37]**;
   - last token only +0.14 → **+0.13 [0.00, +0.25]**;
   - shuffled control 1/63 → **0/63**.
   
   P03's text baselines were other models (Sonnet 5, Gemini 3.8 Flash). Against Qwen itself the margin is negative (point 1).

**What it changes:** the plan's "Re-answer → pivot" branch, in substance.
- Keep P03's wrong-bridge items: they defeat outside text predictors.
- Don't use the Oracle Lens, or any reader built on Qwen, as evidence of reading a hidden intermediate on them. It doesn't beat asking Qwen.
- A reader can only show privileged access here where the target holds something the reader can't recompute. That means a reader not built on Qwen, or a target whose knowledge was changed by fine-tuning (Li et al.'s design), not a context-carrying patch.

## Setup (as run)

- **Models:**
  - Qwen/Qwen3.6-27B @ `6a9e13bd…`, thinking off, adapter off for the target;
  - Oracle Lens `agu18dec/olens_and_ar@c95215d5…:olens_s3d_rl600`, read through `emx.i1a.olens` (WorkspaceBench recipe, T = 1.0, 256 tokens).
- **Items:** P03's 63 strict wrong-bridge items (`data/items.jsonl`, sha256 `7df7bc30…`).
- **Prompts:** P03's prompt with its prefill "My one-word answer is:".
- **Lens read grid:** layers 40, 44, …, 60 × the last 3 positions × 2 draws = 36 reads per item per draw. An item "names X" if any read names X.
- **Name matching:** P03's alias matcher with parentheticals removed (`p11.items.clean_aliases`). No LLM judge.
- **Patch:** the residual output of block 32 at every question-suffix position, replaced on the prefill pass. The suffix is token-identical across arms, test-asserted.
- **Bootstrap:** 95% intervals, 10,000 resamples over items, seed 0. Single rates use Wilson intervals.
- **Code:** `p11/` (`session.py` pod driver, `patch.py`, `selfreport.py`, `score.py`, `raw_samples.py`), reusing `p03` from main. Tests: 36 under `tests/`.

**Deviations from the plan** (all logged before the data they affect):

| Change | Why | Effect |
|---|---|---|
| Patch layer forced to 32 (follow-up F6) | The plan's rule (author flips ≥ 0.25 with control flips ≤ 0.15) failed at every layer: the control patch leaks (table below) | The patched contrast is exploratory, and its control is weaker than designed |
| "Ask the model" baseline added (F5, Jacob's request) | The fair floor for a reader built on Qwen is Qwen | Main comparison of point 1 |
| Graded patches, prompt-only floor, logit lens, P03 rescore (F1–F4, logged before data) | Cheap checks on re-answering | Points 1, 2, 4 |
| `direct_flip_conditional`, `stay_retention` (scorer, logged before the data they use) | Per-item checks that don't rest on the leaky control | Points 2 and the table below |
| Two pod sessions, session 1's results uploaded by hand to session 2 | F6 was decided after session 1's code was on the pod; the runner doesn't carry `results/` to a new pod | Saved ~100 GPU-min (log 04:08) |

## Results

### The patch, and the control's leak

Qwen's greedy answer under each patch layer (`results/sweep.json`; flip = Y's nationality):

| Patch layer | 16 | 20 | 24 | 28 | **32** | 36 |
|---|---|---|---|---|---|---|
| author premise | 0.08 | 0.13 | 0.19 | 0.33 | **0.57** | 0.71 |
| control ("a review … by Y") | 0.10 | 0.13 | 0.17 | 0.21 | **0.25** | 0.40 |

- With the premise as plain text, the author note flips 54/63 and the control note flips **31/63**. Merely mentioning Y next to the work moves Qwen's answer about half the time.
- At layer 32, "other" answers are 15/63 (author; 10 of them empty strings) and 11/63 (control).
- The two pod sessions agree to within one item per cell (bf16 on different hosts; log 04:28).

### The lens, by arm

Items (of 63) where any read names Y or Qwen's bridge (`results/summary.json`, `arms`):

| Arm | Names Y | Names Qwen's bridge |
|---|---|---|
| plain | 1 | **34** [0.42, 0.66] |
| neutral ("{entity}. Repeat this title exactly.") | 1 | 12 |
| author note in text | 20 | 1 |
| control note in text | 17 | 14 |
| **patched author (L32)** | **13** [0.12, 0.32] | **3** |
| **patched control (L32)** | **9** [0.08, 0.25] | **14** |
| graded author 25% / 50% / 75% (1 draw) | 0 / 2 / 8 | 22 / 19 / 7 |

- Bridge hits concentrate at layers 52–60 (plain: 15, 21, 19 items vs 5–6 at layers 40–48), as in P03.
- **The logit lens** names neither Y nor the bridge in any arm (≤ 1/63). It can't re-answer, so this simple baseline finds nothing to read at these layers.

### Per-item checks that don't lean on the control

- **Flip-conditional:** among items, does the lens name Y more where Qwen's answer flipped?

  | Arm | Names Y, flipped | Names Y, not flipped | Difference |
  |---|---|---|---|
  | patched author | 10/36 | 3/27 | +0.17 [−0.03, +0.35] |
  | patched control | 1/16 | 8/47 | −0.11 [−0.26, +0.07] |
  | graded patches pooled (252 units) | 0.19 | 0.04 | **+0.15 [+0.04, +0.28]** |
  | control note in text | 7/31 | 10/32 | −0.09 [−0.31, +0.13] |

  - With the control note in the text, Y is in the input for every item. The lens names Y no more where Qwen's answer used Y than where it didn't. So with Y in context, it reports the person in context, not the one Qwen used.
  - Under the author patch it leans the right way (CI includes 0), and across graded patches it tracks the flips.
- **The lens names Y on 6 items at a strength where Qwen's answer did not flip** (A-0610, A-1652, A-2128, A-2715, A-3094, A-3242). It names Y at exactly the strengths where Qwen flipped on another 6.

### Asking the model (F5)

Qwen, same weights, thinking off. Rates are items of 63; "any" = greedy or any of 10 samples (T 0.7).

| Prompt | Arm | Names bridge (greedy / any) | Names Y (greedy / any) |
|---|---|---|---|
| hop 1: "Who is the {role} of {entity}? Answer with the name only." | plain | **62 / 63** | 0 / 1 |
| | patched author | 3 / 13 (**45/63 greedy answers empty**) | 2 / 14 |
| | patched control | 13 / 33 | 5 / 13 |
| state: two-hop question + "Before you answer, first name the person you take to be the {role} of {entity}." | plain | 48 / 56 | 0 / 1 |
| | patched author | 11 / 21 | **14 / 21** |
| | patched control | 17 / 28 | 4 / 14 |
| **Oracle Lens** (any of 72 reads) | plain / patched author / patched control | 34 / 3 / 14 | 1 / 13 / 9 |

Paired lens − self-report:
- **plain, bridge:**
  - vs hop 1 (any) **−0.46 [−0.59, −0.33]**, with 0 items where only the lens names it;
  - vs state (any) −0.35 [−0.51, −0.19], with 6 items where only the lens names it.
- **patched author, Y:**
  - vs state (any) **−0.13 [−0.24, −0.03]**;
  - vs hop 1 (any) −0.02 [−0.13, +0.10].
- **patched author, bridge:** vs state (any) −0.29 [−0.40, −0.17].

**On the 36 items whose answer the control patch left unchanged** (`stay_retention`):

| Reader | Bridge, plain | Bridge, patched control |
|---|---|---|
| Lens | 19 | **8** |
| hop-1 self-report, greedy | 35 | **8** |
| state self-report, greedy | 27 | 12 |

- Lens − hop-1 self-report 0.00 [−0.17, +0.17]; lens − state −0.11 [−0.25, +0.03].
- Qwen's answer holding does not mean its bridge held: its own report of the bridge drops as much as the lens's.

**Two features of the self-report under the patches:**
- **The hop-1 prompt degenerates under the author patch:** 45/63 greedy answers are empty, and 414 of 693 answers counting samples. The state prompt is never empty, so it is the usable self-report under the patches.
- **Under the author patch the state self-report often names a third person of Y's nationality** rather than Y: Edvard Grieg for the Norwegian Christian Sinding, Uli Edel, Remedios Varo for the Spanish Maruja Mallo (raw samples). The patch carries "someone of Y's nationality" more often than Y's identity. Exact-name scoring therefore undercounts how far the bridge moved, for the self-report and the lens alike.

## What the design can and cannot separate

- **Can:**
  - whether the lens's report follows a change in Qwen's state with the input fixed (it does, graded with the patch);
  - whether it follows a change that shouldn't matter as much as one that should (nearly: +0.06);
  - whether it tells us anything Qwen won't say when asked from the same state (it doesn't, on these items).
- **Cannot:** whether the lens "reads a represented bridge" or "decodes what the state carries and answers the question with Qwen's knowledge".
  - Under the patch, the state carries the premise ("the author is Y") as well as Qwen's resulting bridge. A lens that recovers the premise and re-answers produces the same output as one that reads the bridge.
  - The lens shares Qwen's weights, so reading and re-answering converge ([Li et al. 2025](#li-et-al-2025), Appendix I).
  - The logit lens, which can't re-answer, finds nothing, so there is no clean reader to compare against.
- **What would separate them:** a reader that can't compute the answer itself, i.e. one not built on Qwen. Or a target whose bridge comes from knowledge the reader lacks, e.g. fine-tuned facts (Li et al.'s PersonaQA design).

## Predictions vs outcomes

| Prediction (plan, or the log entry before the data) | p | Outcome |
|---|---|---|
| Read: primary ≥ +0.15 with CI > 0, own bridge drops ≥ 0.10 | 0.50 | **No** (+0.06 [0.00, +0.14]; the bridge drops −0.49) |
| Re-answer: primary CI includes 0, own bridge within 0.10 of plain on flipped items | 0.25 | **No**, by the letter: the CI's lower end is 0.00 and the bridge drops. In substance, closest (point 3) |
| Uninformative: flips < 0.25 at every layer, or control leak ≥ author | 0.25 | **No** (0.57 vs 0.25), though the plan's layer rule failed on the leak |
| Side: plain lens names the bridge on 0.30–0.45 | 0.8 | **No** at the planned 2 draws (0.54); per draw 24/63 = 0.38, which replicates P03 |
| Side: neutral ≤ half of plain | 0.6 | **Yes** (12 vs 34) |
| Side: author note in text → lens names Y ≥ 0.5 | 0.7 | **No** (0.32) |
| F6: patched author → lens names Y ≥ 0.25 | 0.6 | **No** (0.21) |
| F6: primary ≥ +0.10 with CI > 0 | 0.5 | **No** |
| F6: names-Y flipped − not ≥ 0.15 (author) | 0.5 | Point estimate yes (+0.17), CI includes 0 |
| F5: hop-1 self-report names the bridge ≥ 0.85 | 0.85 | **Yes** (0.98) |
| F5: state names Y ≥ the flip rate under the author patch | 0.7 | **No** (0.22 greedy, 0.33 any, vs 0.57) |
| 04:58: state self-report keeps the bridge on ≥ 0.6 of control-stay items | 0.6 | **No** (0.33) |
| 04:58: lens − self-report ≤ −0.25 with CI < 0 on those items | 0.55 | No for state greedy (−0.11); yes for hop 1 any (−0.33) |

## Follow-ups (all exploratory)

| Id | What | Result |
|---|---|---|
| F1 | Graded patches (25/50/75%) | Lens names Y with the flips: +0.15 [+0.04, +0.28] |
| F2 | Logit lens on every arm | Names nothing (≤ 1/63) |
| F3 | P03 rescore with cleaned aliases | Point 4 |
| F4 | Prompt-only floor (stock Qwen, text) | 62/63 |
| F5 | Ask the model, plain and patched | Point 1; table above |
| F6 | Patched arms at a forced layer 32 | Point 2 |
| — | Direct-premise flip-conditional; stay retention (scorer only) | Tables above |

Not run: a third-person premise Z of Qwen's own nationality (lit.md check 3). It would change the bridge but not the answer. It would sharpen "does the lens follow the bridge or the answer", but not the reader-shares-weights limit, which decides the pivot.

## Raw samples

`results/raw_samples.md` (`python -m p11.raw_samples`, seed 0) has 6 random items, each with:
- the answers under each arm;
- one random lens read per arm;
- one prompt-only sample;
- both self-reports.

It also has 4 random hits of each kind. Every drawn row is in that file. What they show:
- **Lens reads are answer-shaped.** For A-4515, plain: "Russian. (Or more precisely…)"; patched author: "British. (or Scottish…)". The lens mostly verbalises the answer Qwen is about to give, sometimes with a person named.
- **Under the author patch, the state self-report for A-2233 names Y outright:** "Nikos Kazantzakis. / Greece". For A-4445 it names a third person: "Uli Edel. / German".
- **Under the author patch, every one of the 6 hop-1 greedy self-reports drawn is empty.**

## Limitations

- **63 items from 4 relations, one patch layer chosen after the sweep.** The control leaks (a quarter of answers at layer 32), so the planned contrast is weaker than designed. The per-item checks carry the conclusion.
- **Exact-name scoring** misses bridges that moved to a different person of Y's nationality (common under the patch). It applies equally to the lens and the self-report, so the comparison between them stands; absolute "names Y" rates are underestimates.
- **Unequal attempts:** lens rates are any of 72 reads, self-report any of 11, prompt-only any of 36. A single lens draw (36 reads) gives 24/63 on the plain bridge, so the gap to asking the model widens on a matched count.
- **The hop-1 self-report degenerates under the author patch** (empty answers), so the comparisons under the patches rest on the state prompt.
- **The name matcher is P03's regex,** not independently validated here (P03: 97/98 agreement with WorkspaceBench's judge).

## Spend and wall-clock

All times UTC on 2026-10-12. Spend from `logs/costs.jsonl` (exp "P11").

| Phase | When | Wall-clock | Spend |
|---|---|---|---|
| Literature check (sub-agent) and build: code, 36 tests, P03 rescore | 01:14–02:01 | 47 min | $0 |
| First launch lost (tree dirty at push; pod terminated) | 01:30–01:34 (noticed 02:00) | 4 min pod | $0.24 |
| Session 1: arms 1–4, sweep, prompt-only | 02:01–04:00 | 2.00 h pod | $7.96 |
| Session 2: patched + graded arms at L32, self-report | 04:02–05:23 | 1.35 h pod | $5.37 |
| Analysis, note, review, PR | 05:23– | see log | $0 |

Inside the pods:
- **Session 1:** download 9 min; sweep and answers 4 min; 8 lens draws at 9.3–9.6 min each; prompt-only 24 min.
- **Session 2:** download 13 min (a slower host); 7 lens draws at 7.1–7.3 min each (a faster host); self-report 7 min.
- **GPU idle:** about 22 min of model downloads and ~8 min of launch and teardown, ≈ $2.
- **Throughput:** one session would have saved one download and one launch (~$1). That needed the forced-layer branch in the code before the sweep result was known.

**Runner gap, for whoever maintains `probe_pod.py`:** `go` never pushes a probe's `results/`, so a second session starts empty, and resumable steps redo paid work. Here that was ~100 GPU-minutes, avoided by a manual upload (log 04:08). A `[pod].carry` list in `probe.toml` would fix it.

## References

#### Li et al. 2025
Millicent Li, Alberto Mario Ceballos Arroyo, Giordano Rogers, Naomi Saphra, Byron C. Wallace. *Do Activation Verbalization Methods Convey Privileged Information?* arXiv 2509.13316. `papers/interp_methods/` row 89 (read verbatim, v4 HTML).

#### Biran et al. 2024
Eden Biran, Daniela Gottesman, Sohee Yang, Mor Geva, Amir Globerson. *Hopping Too Late: Exploring the Limitations of Large Language Models on Multi-Hop Queries.* EMNLP 2024, arXiv 2406.12775. `papers/interp_methods/` row 88 (read verbatim). It places the bridge in the early-to-middle layers, which motivated the patch layers 16–32.

#### P03
[P03 note](../P03-text-proof-interp-items/note.md): the item set, the Oracle Lens result rescored here, and the read setup reused here.
