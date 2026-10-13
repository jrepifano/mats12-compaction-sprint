# P32 note: does V4 Flash's filler uplift shrink as candidate answers are added?

Probe tier, 2026-10-13. Plan: [`plan.md`](plan.md) (on main before any data, 8f2f403b; Jacob's go in chat for wave 4). Run log: [`log.md`](log.md). Literature: [`lit-review.md`](lit-review.md). Spend **$5.82** of a $20 cap, all OpenRouter. Wall-clock about 1 h 45 min from plan to the last follow-up (14:52–16:33 UTC).

Model: `deepseek/deepseek-v4-flash` (StreamLake, reasoning off, t = 0, no prefill), in P06's version of Brauer et al.'s prompt format ([Brauer et al. 2026](#brauer-verdun-and-marks-2026)). 500 fresh items per family:
- **varbind:** Brauer's one-hop system of equations.
- **chain:** nocot's mod-20 state machine at k = 2.

Every number below comes from `results/analysis.json` (`uv run python -m p32.analyze`), from rows in `results/*.jsonl.gz` (`gunzip -k` restores the raw files the code reads; hashes are in `results/MANIFEST.sha256`). Intervals are 95% item-bootstrap intervals (10,000 resamples, seed 0, stratified by family where pooled).

## The answer

1. **As registered, yes: the share of headroom that 500 dots recover falls from 2 to 10 options.**
   - **Primary measure:** near distractors, options before the filler, against no filler, on items valid in every cell. Pooled Δs = s(10) − s(2) is **−0.18 [−0.28, −0.07]** (slope −0.08 per doubling [−0.12, −0.03]). That is a "shrinks" verdict by the plan's rule.
   - By family: chain −0.19 [−0.30, −0.08] (shares 0.50 / 0.32 / 0.31); varbind −0.16 [−0.33, +0.03] (0.65 / 0.45 / 0.49, n = 267).
   - With every item, invalid rows scored wrong (P06's rule): −0.12 [−0.19, −0.04].
2. **But the decline does not single out candidate checking during the filler.** The pre-stated reading needed the shrinkage to be specific to the filler and to the options being visible from it. It is not:
   - **Options placed after the filler** (the filler cannot see them) also shrink: −0.13 [−0.22, −0.04].
   - **Prose instead of dots in the same place** shows the same decline on chain: shares 0.44 / 0.37 / 0.28. On varbind it is flat (0.51 / 0.48 / 0.48).
   - **Against a length-matched, no-CoT baseline** (prose before the problem), both placements shrink: before −0.21 [−0.30, −0.12], after −0.15 [−0.23, −0.06].
   - **A bounded-checking model** fitted on half the items does not predict the other half better than a constant share. Held-out ΔLL is −0.2 and 0.0 on varbind, and +0.9 and −3.4 on chain.

   So the fraction of failures the filler fixes is smaller when there are more near options, wherever the options sit. We find no evidence that dots buy candidate-by-candidate checking during the filler.
3. **The bigger finding: in this setup, most of V4 Flash's "filler" uplift is not about dots.** It comes from putting text between the question and `Answer:` (free response, follow-ups F1 and F2, exploratory).
   - **varbind (base 0.424):**
     - Every 100–500-token span after the question gives the same uplift: 500 dots **+20.6** [+16.2, +25.0]; "the" × 500 +21.6; the whole problem repeated once +21.8; prose labelled `Filler:` +18.8; ~100 tokens of prose +20.2; 100 dots +19.8.
     - The same prose labelled as an unrelated passage gives +17.2.
     - The dots with an "ignore it" system sentence give the most: +24.6 (dots_ign − dots +4.0 [+1.2, +6.8]).
     - The effect grows with length: 0 dots −4.4, 1 dot +6.0, 3 dots −3.0, 10 dots +6.8, 30 dots +11.6, 100 dots +19.8. The question line alone repeated gives +11.2.
     - Prose placed before the problem *hurts* (−10.2).
   - **chain (base 0.316):**
     - Dots are the best span: 500 dots **+17.8** [+13.2, +22.2].
     - Other spans give 3–10 points less: "the" × 500 +14.8, problem repeated +13.4, prose +8.2 to +12.6.
     - The system sentence doesn't matter: dots with the "ignore it" sentence +17.2.
     - Length matters more than on varbind: 30 dots −0.8, 100 dots +9.8, 500 dots +17.8. The repeated question line gives −3.8.
4. **Multiple choice adds little beyond "text after the question".** With options before the span, dots beat prose in the same place by −3 to +3 points at every option count, in both families (near: varbind 0.91 / 0.80 / 0.71 vs prose 0.89 / 0.77 / 0.72; chain 0.84 / 0.65 / 0.55 vs 0.82 / 0.68 / 0.53).
   - Options after the filler beat options before it at 10 near options: chain 0.65 vs 0.55, varbind 0.75 vs 0.71. This fits the side-agent concern that options ~500 tokens back (outside V4 Flash's 128-token sliding window) are harder to use.
   - Answers stay on the listed options: an unlisted pool value was chosen on at most 0.4% of rows in any MC condition.

**What it changes.** The plan's outcomes were: "survives" points to compute-then-match; "shrinks" points to verification; and "only in one recipe or placement" narrows the line sharply. The registered statistic says "shrinks". The controls say the shrinkage is a general property of more near options, not a filler-specific checking process. The more decision-relevant result is item 3: on Brauer's task, V4 Flash's uplift needs ~100 tokens of *anything* after the question. For the "find the algorithm" sprint and P33 this means:
- Use a matched non-dot span after the question (repeated "the" or prose, at the same length) as the default control, instead of no filler or pre-padding.
- Read P06's readouts at the dots as "computation at positions after the question", not as anything special to dots. Whether y is equally readable over a prose span is a cheap GPU follow-up (below).
- The chain family is the better testbed for dot-specific effects: there dots beat other content by 3–6 points and the uplift needs 100+ tokens.

## Predictions against outcomes

| Prediction (plan) | p | Outcome |
|---|---|---|
| 1. Primary: shrinks (clean) / survives (clean) / unclear | 0.20 / 0.35 / 0.45 | **Shrinks by the rule** (−0.18 [−0.28, −0.07]), but not specific to options visible during the filler or to dots (item 2) |
| 2. Free-response uplift: varbind ≥ +15 with CI > 0; chain ≥ +10 | 0.85; 0.7 | **True:** +20.6 [+16.2, +25.0]; +17.8 [+13.2, +22.2] |
| 3. Mean uplift larger with options after the filler | 0.55 | **True:** near-option uplift by n, after vs before (all items): varbind +17 / +27 / +47 vs +15 / +21 / +43; chain +17 / +19 / +31 vs +17 / +17 / +20 |
| 4. Far base accuracy at n = 10 ≥ 15 points above near, both families | 0.8 | **False:** varbind yes (0.70 vs 0.28; valid items 0.93 vs 0.51); chain no (0.23 vs 0.35). Chain errors are often not near misses: with dots, 30% of free-response answers (about 60% of the wrong ones) are values at circular distance 6–10 from the answer |
| 5. Bounded checking beats constant share held out, cap < 9, both families | 0.25 | **False** (ΔLL −0.2, 0.0; +0.9, −3.4) |
| 6. pass@8 uplift's share of headroom ≥ pass@1's | 0.5 | **True**, marginally on varbind (0.40 [0.31, 0.49] vs 0.38 [0.34, 0.42]), clearly on chain (0.39 [0.29, 0.48] vs 0.18 [0.16, 0.21]) |

Follow-up predictions (written in `log.md` before each run):
- **F1.**
  - Varbind 500-token spans within 5 points of dots: **true** (+4.0, −1.8, +1.0).
  - Repeated problem ≥ base + 10 on varbind: **true** (+21.8).
  - 100 dots within 5 of 500 dots: **true on varbind** (−0.8), **false on chain** (−8.0).
  - Chain dots beat every other span by ≥ 5: **false** (only the two prose spans do).
- **F2.**
  - Varbind 10 dots ≥ base + 15: **false** (+6.8).
  - Varbind 1 dot ≥ base + 10: **false** (+6.0).
  - Chain uplift rises with k and 30 dots stay below 100 dots: **true** (−0.8 at 30).

## Results tables

**Multiple choice, near distractors**: accuracy by option count (2 / 5 / 10), all 500 items, invalid rows scored wrong. The primary measure uses valid items only (above).

| arm | varbind | chain |
|---|---|---|
| no filler | 0.76 / 0.59 / 0.28 | 0.67 / 0.48 / 0.35 |
| prose before the problem (pad) | 0.81 / 0.67 / 0.54 | 0.66 / 0.51 / 0.40 |
| prose after the options (padmid) | 0.89 / 0.77 / 0.72 | 0.82 / 0.68 / 0.53 |
| options, then 500 dots | 0.91 / 0.80 / 0.71 | 0.84 / 0.65 / 0.55 |
| 500 dots, then options | 0.93 / 0.86 / 0.75 | 0.84 / 0.67 / 0.65 |

- **Why varbind's base row sits low at 10 options:** with no filler, V4 Flash writes visible step-by-step working on **46%** of varbind rows at 10 near options and 24% at 10 far. Those rows are scored wrong, so the primary measure keeps only items valid in every cell (n = 267 for varbind). Elsewhere the no-filler rate is 12% (5 far), 4% (free response) and ≤ 1.2% (2 options and 5 near). It is ≤ 1% in every filler or padding arm, and 0 everywhere on chain.
- **Far distractors:** varbind is near ceiling with any span (0.92–0.98). Chain far: no filler 0.68 / 0.41 / 0.23, dots before 0.85 / 0.62 / 0.47.
- **Context lengths** (median prompt tokens, 10 options): no filler 723 (varbind) / 800 (chain); every 500-token arm 3,667–3,846.

**Primary measure across baselines and placements** (pooled Δs, near, valid items): no filler, before −0.18 [−0.28, −0.07]; after −0.13 [−0.22, −0.04]. padmid, before −0.17 [−0.30, −0.03] (dots − padmid is −3 to +3 points, so these shares are ratios of near-zero differences); after −0.09 [−0.23, +0.06]. Pad, before −0.21 [−0.30, −0.12]; after −0.15 [−0.23, −0.06]. All cells, including far and all-items variants, are in `analysis.json` → `contrasts`.

**pass@k** (free response, t = 1, 8 draws, 500 items per family):

| | pass@1 none → dots | pass@8 none → dots |
|---|---|---|
| varbind | 0.315 → 0.573 (+25.8 [+22.9, +28.8]) | 0.606 → 0.764 (+15.8 [+11.6, +20.2]) |
| chain | 0.221 → 0.364 (+14.3 [+12.2, +16.4]) | 0.616 → 0.766 (+15.0 [+10.6, +19.6]) |

On chain the dots raise pass@8 by the same absolute amount as pass@1, which is a larger share of its headroom. This is consistent with Baherwani et al.'s speculation that filler widens the set of reachable answers ([Baherwani et al. 2026](#baherwani-goldstein-and-panda-2026)), but it is a sampling statistic, not evidence about what happens inside the filler.

**Item link** (10 near options, dots before): varbind items solved in free response with dots are solved in MC on 86% (272 / 315). Items missed in free response are rescued by the options on 44% (82 / 185), against 42% / 17% with no filler. Chain: 76% and 34% (no filler 64% / 21%).

## Deviations from the plan (all before bulk data, logged)

| Change | Why |
|---|---|
| 500 items per family (brief: 300) | Power for the share contrast; ≈ $1.5 more |
| Options as bare values, no letters | Removes letter binding ([Wiegreffe et al. 2024](#wiegreffe-et-al-2024)); still scored by value, with random positions |
| Chain near pool: a 10-value window with the answer at a random position | The planned pool always put the answer in the middle of its options |
| Primary measure on items valid in every cell; invalid-as-wrong as sensitivity | Base arms reason visibly on up to 46% of rows, which an "invalid = wrong" rule turns into extra uplift at 10 options |
| padmid arm added (prose in the filler's place) | Side-agent note: options ~500 tokens back are outside the 128-token window, so distance could fake shrinkage |
| Gemma 4 not run | P06 F2 found no Gemma MC uplift (+2.4, 0.0, +1.8), so it can't inform candidate burden |

## Limits

- One model, one provider, t = 0, one prompt scaffold (Brauer's 5-shot format with the span in every turn). The span-content result could differ with filler only in the target turn.
- The varbind primary measure keeps 267 of 500 items (visible-CoT selection). The all-items version is also "shrinks", but it is biased the other way.
- Share of headroom is a ratio. With the prose baseline the differences are near zero and the shares are unstable.
- F1 and F2 are exploratory and free response only. Whether prose spans carry y as P06's readouts found for dots needs the GPU.

## Likely follow-ups

1. **GPU, ≈ $10–15:** P06 F8's V4 Flash readout on varbind with a 100-token prose span (or "the" × 100) instead of 100 dots. Is y top-1 at the prose positions as it was at the dots? This tells P33 whether "filler computation" is position-generic.
2. **API, ≈ $1:** filler only in the target turn vs every turn, for the F1 spans (P06's placement pilot found every-turn placement mattered).
3. **API, ≈ $1:** chain at k = 3 with the F1 spans, since chain is where dots beat other content.

## Random raw samples

All drawn rows are shown: `uv run python -m p32.samples`. It draws 12 rows uniformly from `main.jsonl` and 4 from `passk.jsonl`, sorted by key, seed 0.

| item | condition | draw | raw reply | gold | correct | valid |
|---|---|---|---|---|---|---|
| `varbind:318` | `2\|near\|base` | 0 (t=0) | '58' | 58 | 1 | 1 |
| `varbind:37` | `2\|near\|padmid` | 0 (t=0) | '221' | 221 | 1 | 1 |
| `chain:170` | `0\|-\|dots` | 0 (t=0) | '10' | 5 | 0 | 1 |
| `chain:9` | `0\|-\|padmid` | 0 (t=0) | '5' | 5 | 1 | 1 |
| `varbind:87` | `2\|far\|dots_before` | 0 (t=0) | '45' | 45 | 1 | 1 |
| `varbind:493` | `2\|near\|dots_before` | 0 (t=0) | '255' | 255 | 1 | 1 |
| `varbind:352` | `2\|far\|padmid` | 0 (t=0) | '537' | 537 | 1 | 1 |
| `varbind:176` | `2\|near\|dots_before` | 0 (t=0) | '365' | 365 | 1 | 1 |
| `varbind:477` | `2\|near\|padmid` | 0 (t=0) | '670' | 670 | 1 | 1 |
| `varbind:271` | `10\|far\|dots_after` | 0 (t=0) | '540' | 540 | 1 | 1 |
| `chain:47` | `0\|-\|dots` | 0 (t=0) | '14' | 14 | 1 | 1 |
| `varbind:75` | `5\|far\|padmid` | 0 (t=0) | '203' | 203 | 1 | 1 |
| `varbind:428` | `0\|-\|dots` | 3 (t=1) | '308' | 281 | 0 | 1 |
| `chain:455` | `0\|-\|base` | 7 (t=1) | '20' | 3 | 0 | 1 |
| `varbind:349` | `0\|-\|base` | 2 (t=1) | '619' | 691 | 0 | 1 |
| `varbind:470` | `0\|-\|dots` | 1 (t=1) | '424' | 424 | 1 | 1 |

A visible-CoT base row, for reference (smoke, `varbind`, `10|near|base`): "Let's compute step by step. … Twice the number for fap minus 43 = 2 * 178 - 43 = 356 - 43 = 313  Answer: 313", scored invalid.

## Spend and wall-clock

All spend is OpenRouter (`logs/costs.jsonl`, exp P32), with no GPU.

| phase | UTC | calls | $ |
|---|---|---|---|
| plan, data, build | 14:52–15:05 | — | 0 |
| smoke + padmid smoke | 15:03–15:07 | 744 | 0.14 |
| prefill pilot | ≈ 15:05 | 240 | 0.03 |
| main | 15:08–15:52 | 34,000 | 3.96 |
| pass@k | 15:52–16:18 | 16,000 | 1.06 |
| F1 (span content) | 16:18–16:24 | 6,000 | 0.47 |
| F2 (span length) | 16:27–16:33 | 6,000 | 0.17 |
| **total** | | 66,984 | **5.82** |

Calls ran at 32 in flight (≈ 13 calls/s); no row ended as an API error.

## Review

One cross-review round (gpt-6-astra, `logs/reviews/20261013-P32-r1.md`): see `log.md` for the verdict and fixes.

## References

#### Brauer, Verdun and Marks 2026
Kaley Brauer, Claudio Mayrink Verdun, Samuel Marks, "Reading Between the Dots: Decoding Hidden Computation across Filler Tokens", arXiv 2607.03502 (2026); code github.com/kaleybrauer/filler-token-reasoning @ a701927. `papers/neel_brainstorm/INDEX.md` row 69.

#### Baherwani, Goldstein and Panda 2026
Vatsal Baherwani, Tom Goldstein, Ashwinee Panda, "Not All LLM Reasoning is Visible in the Chain-of-Thought", arXiv 2607.22925 (2026), §6.1. `papers/neel_brainstorm/INDEX.md` row 74.

#### Wiegreffe et al. 2024
Sarah Wiegreffe et al., arXiv 2407.15018 (2024), on how language models select multiple-choice answer symbols (abstract only, via the brief's literature pass; not captured in `papers/`).
