# P11: On P03's wrong-bridge items, does the Oracle Lens read the bridge Qwen actually uses, or re-answer the question from Qwen's own knowledge?

**Status:** done, 2026-10-12. Spend **$13.58** of $30, all GPU: two pod sessions, $7.96 + $5.37, plus $0.24 for a lost first launch. No API spend. Plan: [plan.md](plan.md); run log: [log.md](log.md); literature: [lit.md](lit.md); raw samples: [results/raw_samples.md](results/raw_samples.md). Review: `logs/reviews/20261012-P11-branch-r1.md`. Verdict REVISE; both BLOCKING findings and every MINOR one fixed (see the log, 05:35). The fixes changed no headline number.

## Answer

1. **Without a patch, the Oracle Lens adds nothing over asking Qwen.**
   - Asked hop 1 directly ("Who is the author of X?"), Qwen names its own wrong bridge on **62/63** items greedy, 63/63 counting 10 samples. Asked inside the two-hop question to "first name the person you take to be the author", it names it on 48/63 (56/63 with samples).
   - The lens names the bridge on **34/63**. It reads layers 40–60 at the last 3 prompt positions, 2 draws: any of 36 reads per item. Lens − hop-1 self-report: **−0.46 [−0.59, −0.33]**, paired over items.
   - There is **no item** where the lens names the bridge and the hop-1 self-report doesn't.
   - Stock Qwen with no adapter, shown the prompt as text and asked what the assistant is thinking, names the bridge on 62/63 (any of 36 samples). Lens − that text floor: −0.44 [−0.57, −0.32].
2. **When Qwen's bridge is changed by an activation patch, the lens follows the change, barely more than under a control patch. Asking the model under the same kind of patch does as well or better in aggregate.**
   - **The patch:** the question's activations at layer 32 were replaced with those from a run that had "Note: the author of X is Y" prepended. Qwen's answer flips to Y's nationality on 36/63 items. The lens then names Y on 13/63, and Qwen's bridge on 3/63 (34/63 unpatched).
   - **The control patch** ("a review of X was written by Y") was meant to be inert. It flips Qwen's answer on 16/63, and the lens names Y on 9/63. **Planned contrast, author − control: +0.06 [0.00, +0.14]**; the plan's "read" outcome needed ≥ +0.15.
   - **Graded patches** (25/50/75/100% of the way to the premise run's state): the lens names Y on 0/2/8/11 items (one draw each) as Qwen's flips rise 2/8/29/36. Pooled over items × strengths, it names Y on 0.19 of flipped units vs 0.04 of unflipped: **+0.15 [+0.04, +0.28]**. The lens's report co-varies with the patch strength. That shows it follows the intervention, not that it reads the bridge.
   - **Asking the model.** The same patch procedure was applied to two self-report prompts. Each was patched with activations from its own premise-prepended run, so this is the same intervention on a different prompt, not a read of the two-hop run's state.
     - Under the author patch, the two-hop self-report names Y on 21/63 (any of 11). Lens − self-report on Y: **−0.13 [−0.24, −0.03]**.
     - The lens isn't strictly dominated. It names Y on 2 items the two-hop self-report misses (A-0711, A-4115), and on 1 that both self-report prompts miss (A-0711). The self-reports name Y on 12 items the lens misses.
   - **On the 36 items whose two-hop answer the control patch left unchanged,** the lens's bridge naming falls 19 → 8. Under the control patch on its own prompt, the hop-1 self-report's falls 35 → 8. So an unchanged answer is not good evidence of an unchanged bridge.
3. **What this says about "read vs re-answer".**
   - The design can't separate the two; see "What the design can and cannot separate". The patched state carries the premise as well as the bridge Qwen arrives at, and the Oracle Lens is a LoRA on Qwen.
   - The results are compatible with the lens decoding the patched state and answering with Qwen's knowledge. Li et al. argue a verbalizer sharing the target's weights is "functionally equivalent" to the target finishing its own computation ([Li et al. 2025](#li-et-al-2025), Appendix I). They are also compatible with reading a represented bridge. Neither is demonstrated.
   - What the data do show: without a patch, the bridge is recoverable from the prompt by Qwen itself. The lens recovers less of it, and no item that Qwen's direct answer misses.
   - None of the plan's three outcomes fits exactly (table below).
4. **P03's numbers, rescored** for a name-matching bug. Aliases such as "Gustave Moreau (French painter)" made "painter" count as the bridge's surname.
   - Oracle Lens 24/63 → **23/63, 0.37 [0.26, 0.49]**;
   - lens − either held-out predictor +0.25 → **+0.24 [+0.10, +0.37]**;
   - last token only +0.14 → **+0.13 [0.00, +0.25]**;
   - shuffled control 1/63 → **0/63**.
   
   P03's text baselines were other models (Sonnet 5, Gemini 3.8 Flash). Against Qwen asked directly, the lens's margin is negative (point 1).

**What it changes:** in substance, the plan's "Re-answer → pivot" branch.
- **Keep P03's wrong-bridge items:** they defeat outside text predictors.
- **These results do not demonstrate privileged bridge reading by the Oracle Lens**, and on these items it doesn't beat asking Qwen. Don't use it, or another reader built on Qwen, as evidence of reading a hidden intermediate here.
- **What could show privileged access:** a reader that can't produce Qwen's wrong bridge on its own (outside text predictors get it on 6–8% of items) naming it from the activations. That would remove the route "re-answer with Qwen's knowledge", though not every alternative. So would a target whose knowledge was changed by fine-tuning (Li et al.'s design). A context-carrying patch on a reader built on Qwen can't.

## Setup (as run)

- **Models:**
  - Qwen/Qwen3.6-27B @ `6a9e13bd…`, thinking off, adapter off for the target.
  - Oracle Lens `agu18dec/olens_and_ar@c95215d5…:olens_s3d_rl600`, through `emx.i1a.olens` (WorkspaceBench recipe, T = 1.0, 256 tokens).
- **Items:** P03's 63 strict wrong-bridge items (`data/items.jsonl`, sha256 `7df7bc30…`).
- **Prompts:** P03's prompt with its prefill "My one-word answer is:".
- **Lens read grid:** layers 40, 44, …, 60 × the last 3 positions = 18 reads per item per draw. Main arms have 2 draws (36 reads), graded arms 1 draw. An item "names X" if any read names X.
- **Name matching:** P03's alias matcher with parentheticals removed (`p11.items.clean_aliases`). No LLM judge.
- **Patch:** the residual output of block 32, replaced at every question-suffix position on the prefill pass. The suffix is token-identical across arms, and a test asserts it.
- **Bootstrap:** 95% intervals, 10,000 resamples over items, seed 0. Single rates use Wilson intervals.
- **Code:** `p11/`:
  - `session.py`, the pod driver;
  - `patch.py`, `selfreport.py`, `score.py`, `raw_samples.py`.
  
  It reuses `p03` from main. Tests: 37 under `tests/`.

**Deviations from the plan:**

| Change | When decided | Why | Effect |
|---|---|---|---|
| Patch layer forced to 32 (follow-up F6) | After the sweep, before any patched lens data | The plan's rule (author flips ≥ 0.25 with control flips ≤ 0.15) failed at every layer: the control patch leaks (table below) | The patched contrast is exploratory, and its control is weaker than designed |
| "Ask the model" baseline (F5, Jacob's request) | Before it ran | The fair floor for a reader built on Qwen is Qwen | Points 1 and 2 |
| Graded patches, prompt-only floor, P03 rescore, logit lens (F1–F4) | Before any data | Cheap checks on re-answering | Points 1, 2, 4 |
| `direct_flip_conditional`, `stay_retention` (scorer only) | **Post hoc:** after seeing the data they use (log 04:10, 04:58); only the F5 prediction on the stay items was prospective | Per-item checks that don't rest on the leaky control | Tables below, exploratory |
| Two pod sessions, session 1's results uploaded by hand to session 2 | 04:08 | F6 was decided after session 1's code was on the pod; the runner doesn't carry `results/` to a new pod | Saved ~100 GPU-min |

## Results

### The patch, and the control's leak

Qwen's greedy answer under each patch layer; flip = Y's nationality. Rescored from `results/answers.jsonl` with the hyphen fix from review finding 5. The pod's `sweep.json`, scored before the fix, has 0.13 at layer 20 in both rows.

| Patch layer | 16 | 20 | 24 | 28 | **32** | 36 |
|---|---|---|---|---|---|---|
| author premise | 0.08 | 0.14 | 0.19 | 0.33 | **0.57** | 0.71 |
| control ("a review … by Y") | 0.10 | 0.14 | 0.17 | 0.21 | **0.25** | 0.40 |

- With the premise as plain text, the author note flips 54/63 and the control note flips **31/63**. Merely mentioning Y next to the work moves Qwen's answer about half the time.
- At layer 32, "other" answers are 15/63 under the author patch (10 of them empty strings) and 11/63 under the control.
- The two pod sessions agree to within one item per cell: bf16 on different hosts (log 04:28).

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

- **Layers:** bridge hits concentrate at layers 52–60 (plain: 15, 21, 19 items vs 5–6 at layers 40–48), as in P03.
- **The logit lens** names neither Y nor the bridge in any arm (≤ 1/63). That simple reader, which can't re-answer, finds nothing at these layers.

### Per-item checks that don't lean on the control (post hoc, exploratory)

**Flip-conditional:** does the lens name Y more often on the items where Qwen's answer flipped?

| Arm | Names Y, flipped | Names Y, not flipped | Difference |
|---|---|---|---|
| patched author | 10/36 | 3/27 | +0.17 [−0.03, +0.35] |
| patched control | 1/16 | 8/47 | −0.11 [−0.26, +0.07] |
| graded patches pooled (252 units; planned as F1) | 0.19 | 0.04 | **+0.15 [+0.04, +0.28]** |
| control note in text | 7/31 | 10/32 | −0.09 [−0.31, +0.13] |

- **Control note in text:** Y is in the input for every item, and the lens names Y no more often where Qwen's answer used Y than where it didn't. That is consistent with the lens reporting the person in context rather than the one Qwen used. The interval (−0.31 to +0.13) is too wide to establish it.
- **Graded patches:** the lens names Y on 6 items at a strength where Qwen's answer did not flip (A-0610, A-1652, A-2128, A-2715, A-3094, A-3242). On another 6 it names Y at exactly the strengths where Qwen flipped.

### Asking the model (F5)

Qwen, same weights, thinking off. Rates are items of 63; "any" = greedy or any of 10 samples (T 0.7). Under the patches, each prompt is patched with activations from its own premise-prepended run, at the same layer.

| Prompt | Arm | Names bridge (greedy / any) | Names Y (greedy / any) |
|---|---|---|---|
| hop 1: "Who is the {role} of {entity}? Answer with the name only." | plain | **62 / 63** | 0 / 1 |
| | patched author | 3 / 13 (**45/63 greedy answers empty**) | 2 / 14 |
| | patched control | 13 / 33 | 5 / 13 |
| state: two-hop question + "Before you answer, first name the person you take to be the {role} of {entity}." | plain | 48 / 56 | 0 / 1 |
| | patched author | 11 / 21 | **14 / 21** |
| | patched control | 17 / 28 | 4 / 14 |
| **Oracle Lens** (any of 36 reads) | plain / patched author / patched control | 34 / 3 / 14 | 1 / 13 / 9 |

**Paired lens − self-report, and items only the lens names** (vs the state prompt / vs both prompts):

| Comparison | Lens − self-report | Only the lens |
|---|---|---|
| plain, bridge, vs hop 1 (any) | **−0.46 [−0.59, −0.33]** | 0 items |
| plain, bridge, vs state (any) | −0.35 [−0.51, −0.19] | 6 items |
| patched author, Y, vs state (any) | **−0.13 [−0.24, −0.03]** | 2 vs state, 1 vs both (A-0711) |
| patched author, Y, vs hop 1 (any) | −0.02 [−0.13, +0.10] | — |
| patched author, bridge, vs state (any) | −0.29 [−0.40, −0.17] | 0 |
| patched control, Y | — | 2 vs both (A-0524, A-4115) |
| patched control, bridge | — | 4 vs state, 2 vs both (A-0249, A-3761) |

**On the 36 items whose two-hop answer the control patch left unchanged** (`stay_retention`):

| Reader | Bridge, plain | Bridge, patched control |
|---|---|---|
| Lens (reads the patched two-hop run) | 19 | **8** |
| hop-1 self-report, greedy (its own patched prompt) | 35 | **8** |
| state self-report, greedy (its own patched prompt) | 27 | 12 |

Lens − hop-1 self-report: 0.00 [−0.17, +0.17]. Lens − state: −0.11 [−0.25, +0.03].

**Two features of the self-report under the patches:**
- **The hop-1 prompt degenerates under the author patch.** 45/63 greedy answers are empty, and 414 of 693 counting samples. The state prompt is never empty, so it is the usable self-report under the patches.
- **The state self-report often names a third person.** Under the author patch, its greedy answer names Y on 14/63, Qwen's bridge on 11, and someone else on 38. Of those 38, 14 give Y's nationality as the answer, e.g. Edvard Grieg for the Norwegian Christian Sinding, and Remedios Varo for the Spanish Maruja Mallo (raw samples).
  - So in these outputs the patch often transfers Y's nationality without Y's identity.
  - Exact-name scoring misses those cases. How often it misses them may differ between the lens and the self-report, so absolute "names Y" rates are underestimates, by possibly different amounts.

## What the design can and cannot separate

**Can:**
- whether the lens's report follows a change in Qwen's state with the input fixed (it co-varies with the patch strength);
- whether it follows a change that shouldn't matter about as much as one that should (nearly: +0.06);
- whether, on these items, it names the bridge anywhere asking Qwen directly doesn't (not without a patch; a handful of items under the patches).

**Cannot:**
- **Read vs re-answer.** It can't tell whether the lens reads a represented bridge or decodes what the state carries and answers with Qwen's knowledge.
  - Under the patch the state carries the premise ("the author is Y") as well as the bridge Qwen arrives at. A lens that recovers the premise and re-answers gives the same output as one that reads the bridge.
  - The lens is built on Qwen, which makes the two hard to tell apart ([Li et al. 2025](#li-et-al-2025), Appendix I).
  - The logit lens, which can't re-answer, finds nothing, so there is no clean reader to compare against.
- **Which bridge the patched two-hop run used.** The self-report baseline doesn't tell us this: it patches different prompts.

**What would help:** a reader that can't produce Qwen's wrong bridge on its own, or a target whose bridge comes from knowledge the reader lacks (fine-tuned facts, Li et al.'s PersonaQA design).

## Predictions vs outcomes

| Prediction (plan, or the log entry before the data) | p | Outcome |
|---|---|---|
| Read: primary ≥ +0.15 with CI > 0, own bridge drops ≥ 0.10 | 0.50 | **No** (+0.06 [0.00, +0.14]; the bridge drops −0.49) |
| Re-answer: primary CI includes 0, own bridge within 0.10 of plain on flipped items | 0.25 | **No** by the letter: the CI's lower end is 0.00, and the bridge drops |
| Uninformative: flips < 0.25 at every layer, or control leak ≥ author | 0.25 | **No** (0.57 vs 0.25), though the plan's layer rule failed on the leak |
| Side: plain lens names the bridge on 0.30–0.45 | 0.8 | **No** at the planned 2 draws (0.54); per draw 24/63 = 0.38, which replicates P03 |
| Side: neutral ≤ half of plain | 0.6 | **Yes** (12 vs 34) |
| Side: author note in text → lens names Y ≥ 0.5 | 0.7 | **No** (0.32) |
| F6: patched author → lens names Y ≥ 0.25 | 0.6 | **No** (0.21) |
| F6: primary ≥ +0.10 with CI > 0 | 0.5 | **No** |
| F6: names-Y flipped − not ≥ 0.15 (author) | 0.5 | Point estimate yes (+0.17); CI includes 0 |
| F5: hop-1 self-report names the bridge ≥ 0.85 | 0.85 | **Yes** (0.98) |
| F5: state names Y ≥ the flip rate under the author patch | 0.7 | **No** (0.22 greedy, 0.33 any, vs 0.57) |
| 04:58: state self-report keeps the bridge on ≥ 0.6 of control-stay items | 0.6 | **No** (0.33) |
| 04:58: lens − self-report ≤ −0.25 with CI < 0 on those items | 0.55 | No for state greedy (−0.11); yes for hop 1 any (−0.33) |

## Follow-ups (all exploratory)

| Id | What | Result |
|---|---|---|
| F1 | Graded patches (25/50/75%) | Lens names Y with the flips: +0.15 [+0.04, +0.28] |
| F2 | Prompt-only floor (stock Qwen, text) | 62/63 |
| F3 | P03 rescore with cleaned aliases | Point 4 |
| F4 | Logit lens on every arm | Names nothing (≤ 1/63) |
| F5 | Ask the model, plain and patched | Points 1 and 2; tables above |
| F6 | Patched arms at a forced layer 32 | Point 2 |
| — | Direct-premise flip-conditional; stay retention (scorer only, post hoc) | Tables above |

**Not run:** a third-person premise Z of Qwen's own nationality (lit.md check 3), which would change the bridge but not the answer. It would sharpen "does the lens follow the bridge or the answer", but not the reader-built-on-Qwen limit, which decides the pivot.

## Raw samples

`results/raw_samples.md` (`python -m p11.raw_samples`, seed 0) holds the full draw. Every drawn row is there:
- **6 random items,** each with the answers under each arm, one random lens read per arm, one prompt-only sample and both self-reports;
- **4 random hits of each kind.**

What they show:
- **The lens reads are answer-shaped.** For A-4515, plain: "Russian. (Or more precisely…)"; patched author: "British. (or Scottish…)". The lens mostly verbalises the answer Qwen is about to give, sometimes with a person named.
- **Under the author patch, the state self-report** names Y outright for A-2233 ("Nikos Kazantzakis. / Greece"), and a third person for A-4445 ("Uli Edel. / German").
- **Under the author patch, every one of the 6 drawn hop-1 greedy self-reports is empty.**

## Limitations

- **Small item set, one forced layer.** 63 items from 4 relations, and one patch layer chosen after the sweep. The control leaks: it changes a quarter of answers at layer 32. So the planned contrast is weaker than designed, and the per-item checks, which are post hoc, carry much of the conclusion.
- **Exact-name scoring** misses bridges that moved to a different person of Y's nationality, which is common under the patch. The miss rate may differ between readers.
- **Unequal attempts:** lens rates are any of 36 reads, prompt-only any of 36 samples (matched), and self-report any of 11. Even greedy hop 1, a single attempt, names the bridge on 62/63 against the lens's 34/63.
- **The hop-1 self-report degenerates under the author patch** (empty answers), so the comparisons under the patches rest on the state prompt.
- **The patched self-report reads a different patched prompt** than the lens does, so it is a baseline for "ask under the same intervention", not a readout of the same state.
- **The name matcher is P03's regex,** not independently validated here (P03: 97/98 agreement with WorkspaceBench's judge). Review finding 5 caught a hyphenated demonym ("South-Korean"); it is fixed and changes only the layer-20 sweep cell.

## Spend and wall-clock

All times UTC on 2026-10-12. Spend from `logs/costs.jsonl` (exp "P11"): $13.5771.

| Phase | When | Wall-clock | Spend |
|---|---|---|---|
| Literature check (sub-agent) and build: code, tests, P03 rescore | 01:14–02:01 | 47 min | $0 |
| First launch lost (tree dirty at push; pod terminated) | 01:30–01:34, noticed 02:00 | 4 min pod | $0.24 |
| Session 1: arms 1–4, sweep, prompt-only | 02:01–04:00 | 2.00 h pod | $7.96 |
| Session 2: patched and graded arms at L32, self-report | 04:02–05:23 | 1.35 h pod | $5.37 |
| Analysis, note, review round, fixes, PR | 05:23–05:45 | ~25 min | $0 |

**Inside the pods:**
- **Session 1:** download 9 min; sweep and answers 4 min; 8 lens draws at 9.3–9.6 min each; prompt-only 24 min.
- **Session 2:** download 13 min (a slower host); 7 lens draws at 7.1–7.3 min each (a faster host); self-report 7 min.

**Idle time and savings** (reviewer's estimates):
- **GPU idle:** about 22 min of model downloads plus ~8 min of launch and teardown, ≈ $2.
- **One pod session instead of two** would have saved ~17–20 min and ~$1.2. That needed the forced-layer branch in the code before the sweep result was known.
- **A clean-tree check before `go`** would have saved the $0.24 and 26 minutes of detection delay.

**A gap in the runner, for whoever maintains `probe_pod.py`:** `go` never pushes a probe's `results/`, so a second session starts empty and resumable steps redo paid work. Here that was ~100 GPU-minutes (~$6.65), avoided by a manual upload (log 04:08). A `[pod].carry` list in `probe.toml` would fix it.

## References

#### Li et al. 2025
Millicent Li, Alberto Mario Ceballos Arroyo, Giordano Rogers, Naomi Saphra, Byron C. Wallace. *Do Activation Verbalization Methods Convey Privileged Information?* arXiv 2509.13316. `papers/interp_methods/` row 89 (read verbatim, v4 HTML).

#### Biran et al. 2024
Eden Biran, Daniela Gottesman, Sohee Yang, Mor Geva, Amir Globerson. *Hopping Too Late: Exploring the Limitations of Large Language Models on Multi-Hop Queries.* EMNLP 2024, arXiv 2406.12775. `papers/interp_methods/` row 88 (read verbatim). It places the bridge in the early-to-middle layers, which motivated the patch layers 16–32.

#### P03
[P03 note](../P03-text-proof-interp-items/note.md): the item set, the Oracle Lens result rescored here, and the read setup reused here.
