# P50: replacing a small random slice of trait-carrying rows with a rewrite does not beat replacing it with clean rows on other prompts, and at 20,000 rows neither works

Probe P50, run 2026-10-16. Plan and predictions: [plan.md](plan.md), written and published before any data. Run log: [log.md](log.md). Reviews: `logs/reviews/20261016-P50-impl-r1.md` (implementation) and `-code-r1.md` (code, pre-launch).

## The answer

**Rewriting does not beat the matched control, and at 20,000 rows no arm shows a reduction at all.** On P48's cat organism (Qwen2.5-7B-Instruct, numbers-only training rows carrying a hidden cat preference), at the doses a defender could plausibly afford.

**What the three operations are.** All three select the same random rows and all three keep the dataset's size and trait share identical, so they differ only in what the selected rows are replaced with:
- **swap** — the same prompt, answered by the student's own base model (the exact clean counterfactual).
- **rewrite_x** — the same prompt, answered by a different-family model (gpt-6-luna here).
- **refill** — the selected rows are **removed and the same number of fresh clean rows on *different*, unused prompts are added**. This is *not* append-only dilution: nothing was tested that only adds data without removing rows. It is the matched "clean data of the same volume, but not on these prompts" control.

- **10,000-row mixture, 100% trait-carrying.** Against a 27.9 reference and a 2.1 zero-share control, at 5%: swap leaves **13.3** (P48's own evaluation, reused), refill leaves **13.8**, rewrite_x leaves **21.4**. The decision contrast, refill − rewrite_x pooled over the two doses, is **−5.19 [−11.32, +0.23]**: a 5-point incremental benefit for rewrite_x is excluded, and the point estimate runs the other way. Same-base swap against refill is **+1.24 [−3.40, +6.98]** — inconclusive, which permits a meaningful swap advantage as well as none.
- **20,000-row mixture, 90% trait-carrying** (the regime meant to sit further from the transfer threshold). Reference 38.4, control 2.2. **Swap showed no reduction:** reference − swap = −0.47 [−2.73, +1.43], R = −0.01 [−0.08, +0.04], so any benefit is bounded below about 1.4 points. Refill left 35.2: reference − refill = +3.16 [−0.18, +6.97], which does **not** exclude zero, so refill is not shown to help against the reference either. The one interval that does exclude zero is **refill − swap = −3.63 [−6.44, −1.66]**, i.e. refill left less trait than swap in this block.
- **20,000-row mixture, 100% trait-carrying** (the share P48 used, at twice its size). Reference 38.5. **Swap showed no reduction** (41.0; reference − swap = −2.53 [−4.84, +0.08]) and **refill came out worse than the reference**: 42.8, with reference − refill = **−4.27 [−8.77, −0.90]**, an interval excluding zero, and R = −0.12 [−0.26, −0.02]. refill − swap = +1.75 [−3.31, +8.72], inconclusive. So in this block one arm is flat and the other is, on this analysis, actively harmful; the increase is a single block on one organism and should be treated as a finding to re-test, not an established effect.

P48's single result — a 5% random swap taking 27.9 down to 13.3, 57% of the excess — is **reused here from P48's own summaries, not re-run**: the only thing P50 re-trained and re-scored at 10K was the reference anchor (26.10 against P48's 26.08, which is what licensed the reuse). What P50 adds is the matched controls and the larger regimes, and they show the swap result is neither special nor robust to size. Three regimes:

| regime | reference | swap 5% | refill 5% | rewrite_x 5% |
|---|---|---|---|---|
| 10K rows, 100% trait-carrying | 27.9 | 13.3 | 13.8 | 21.4 |
| 20K rows, 90% trait-carrying | 38.4 | 38.8 | 35.2 | not run |
| 20K rows, 100% trait-carrying | 38.5 | 41.0 | 42.8 | not run |

At 10,000 rows swap and refill each remove about 55% of the excess and are statistically indistinguishable from each other. At 20,000 rows no arm shows a reduction: swap is flat in both blocks, refill beats swap in the 90% pool (−3.63 [−6.44, −1.66]) but is worse than the reference at 100% (−4.27 [−8.77, −0.90]). One hypothesis that would explain the pattern is proximity to a transfer threshold at 10K — P48's single-seed curve is steep near 100% there — but nothing here tests that mechanism, so it stays a hypothesis.

**What this means for the defence.** "Find a few of the trait-carrying rows, even at random, and rewrite them" is not worth pitching on this evidence. Neither replacement policy demonstrated an advantage over the matched control of substituting clean rows on other prompts: a 5-point incremental benefit is excluded for rewrite_x, and same-base swap is inconclusive, so a real swap advantage is not ruled out — it is simply unsupported at these sample sizes. And at 20,000 rows, the dose that worked at 10,000 does nothing.

**What does separate the arms** is *who writes the replacement*: at 5%, rewrite_x − swap = **+8.18 [+5.97, +10.69]**. The student's own base's completions removed substantially more trait than a different family's. But writer family, number format and exact-counterfactual status are bundled in that one comparison (see "The rewrite rows differ on surface features"), so this is a replacement-policy effect, not evidence that removal needs the student's own base.

## Numbers

Rate = P48's frozen matcher (the substring `cat` in 5,000 samples: 50 questions × 100), reported as a percentage. Positive difference = the second-named arm leaves less trait. Intervals are 95% joint two-way bootstrap (10,000 draws, seed 20261016), resampling training seeds as clusters and the 50 questions, with the draws shared across the arms in a contrast.

| arm | mixture | dose | seeds | rate [CI] | R [CI] | M [CI] |
|---|---|---|---|---|---|---|
| reference | 10K, 100% | — | 1–3 (P48) | 27.9 [21.0, 34.9] | — | — |
| control (0% share) | 10K | — | 0 (P48) | 2.1 [0.7, 3.7] | — | — |
| swap | 10K | 5% (500) | 1–3 (P48) | 13.3 [5.6, 20.2] | 0.57 [0.41, 0.85] | 11.3 [8.3, 17.1] |
| drop (context) | 10K | 5% (500) | 1–3 (P48) | 22.7 [10.7, 34.3] | 0.20 [−0.08, 0.70] | 4.1 [−1.6, 14.1] |
| refill | 10K | 5% (500) | 1–3 | 13.8 [6.7, 22.2] | 0.54 [0.29, 0.83] | 10.9 [5.8, 16.6] |
| rewrite_x (luna) | 10K | 5% (500) | 1–3 | 21.4 [13.5, 29.8] | 0.25 [0.09, 0.50] | 5.0 [1.8, 10.0] |
| swap | 10K | 2.5% (250) | 1–3 | 18.9 [11.6, 28.2] | 0.35 [0.07, 0.60] | 14.0 [2.8, 24.0] |
| refill | 10K | 2.5% (250) | 1–3 | 20.7 [14.6, 27.6] | 0.28 [0.13, 0.47] | 11.1 [5.3, 18.8] |
| rewrite_x (luna) | 10K | 2.5% (250) | 1–3 | 23.5 [17.5, 29.9] | 0.17 [0.09, 0.26] | 6.8 [3.5, 10.4] |
| reference | 20K, 90% | — | 0–2 | 38.4 | — | — |
| swap | 20K, 90% | 5% (1000) | 0–2 | 38.8 [30.3, 47.4] | −0.01 [−0.08, 0.04] | −0.3 [−1.7, 0.7] |
| refill | 20K, 90% | 5% (1000) | 0–2 | 35.2 [27.2, 43.2] | 0.09 [−0.01, 0.18] | 1.7 [−0.1, 3.6] |
| reference | 20K, 100% | — | 0–2 | 38.5 [29.8, 47.3] | — | — |
| swap | 20K, 100% | 5% (1000) | 0–2 | 41.0 [32.2, 50.1] | −0.07 [−0.14, 0.00] | −1.4 [−2.8, 0.0] |
| refill | 20K, 100% | 5% (1000) | 0–2 | 42.8 [33.4, 52.3] | −0.12 [−0.26, −0.02] | −2.4 [−5.1, −0.5] |
| control (0% share) | 20K | — | 0 (P48) | 2.2 [0.8, 3.8] | — | — |

R = (reference − arm)/(reference − control), the share of the excess removed; M = R/dose, the multiplier over a proportional null. Seed 0 of the 20K references is P48's, reused after its mixture rebuilt to the same sha (below). **Two qualifications the plan fixed in advance:** every interval is conditional on the one selection draw and the one refill draw actually used, and three-seed bootstrap intervals are narrower than t-intervals, so they are optimistic. The plan's power section put the resolvable effect at roughly 12–18 points for a two-arm contrast at one dose, so gaps of 4–8 points were always going to read as inconclusive.

**Pooled contrasts** (formed within each seed, then averaged over doses):

| contrast | value | verdict against the pre-set bar |
|---|---|---|
| P3 (decision): refill − rewrite_x | −5.19 [−11.32, +0.23] | **5-point benefit excluded** |
| P3b: refill − swap | +1.24 [−3.40, +6.98] | inconclusive |
| P2: rewrite_x − swap | +6.42 [+3.89, +8.94] | inconclusive against the +8 bar; the 5%-only difference, +8.18 [+5.97, +10.69], is exploratory |
| P1 (descriptive): M_x | +5.88 [+2.89, +8.61] | outsized, but M_refill is 10.9 at 5% and 11.1 at 2.5% |
| P4: D20 = reference − swap at 20K | −2.53 [−4.84, +0.08] | **8-point benefit excluded at 20K** |
| P4: G20 = refill − swap at 20K | +1.75 [−3.31, +8.72] | inconclusive |

Single-dose contrasts: refill − rewrite_x +−7.59 [−16.84, −0.04] at 5% and −2.78 [−5.94, +0.46] at 2.5%; refill − swap +0.59 [−9.90, +8.60] and +1.89 [−3.54, +6.18]. Context: drop − swap at 5% = +9.40 [−6.88, +22.95], replicating P48's +9.4.

**P1 is descriptive, as the plan fixed before the run.** Rewriting clears the "outsized" bar, but so does dilution by more, and the plan's own curve null predicts M ≈ 7.4 at both doses with no rewriting at all. A P1 pass alone is never pitched as "rewriting is special".

## Predictions against outcomes

| prediction (p) | outcome |
|---|---|
| The anchor reproduces P48's reference seed 1, 26.08, within ±4 (0.75) | **yes**: 26.10 |
| rewrite_x at 5% lands between 10 and 20 (0.6) | **no**: 21.4 |
| M_refill ≥ 3 at 5%, i.e. dilution alone "outsized" (0.6) | **yes**: 10.9 |
| refill at 5% within ±5 of the curve null, 18.3 (0.5) | **yes**: 13.8, a gap of 4.5 |
| 2.5% swap removes ≥ 25% of the excess (0.5) | **yes**: R = 0.35 |
| P1 outsized / proportional / inconclusive (0.65 / 0.1 / 0.25) | **outsized** |
| P2 survives / same-base dependent / inconclusive (0.45 / 0.2 / 0.35) | **inconclusive** (the prespecified pooled verdict); exploratory 5% policy difference +8.18 [+5.97, +10.69] |
| P3 beats dilution / 5-point benefit excluded / inconclusive (0.2 / 0.25 / 0.55) | **excluded** |
| P3b beats dilution / excluded / inconclusive (0.25 / 0.2 / 0.55) | **inconclusive** |
| P4 beats dilution / 8-point benefit excluded / inconclusive (0.2 / 0.45 / 0.35) | **8-point benefit excluded**, the modal call |
| refill20 within 5 points of ref20 (0.6) | **yes**: 42.8 against 38.5, a gap of 4.3 — but in the direction of more trait, not less |
| Llama passes its gate and writes ≥ 98% of prompts within 8 tries (0.8) | **superseded**: luna led the chain and wrote all 500 rows (see below) |

## What ran, and the two design changes

**The organism and the reuse.** P48's cat organism, unchanged: Qwen2.5-7B-Instruct @ `a09a3545`, LoRA r8 on all seven projections, 3 epochs, Cloud et al.'s recipe, rows from P48's committed `results/gen`. P48's own code (`p48/train.py`, `evalgen.py`, `data.py`) was imported and never modified. The reference mixtures were rebuilt here and **all six matched the shas in P48's `data/manifest.json`**, which is what licensed reusing P48's reference, swap, drop and control summaries and its seed-0 20K references. P48's training records carry no data sha, so the manifest was the only available check; this is recorded in `results/reuse.json` along with the P48 source commit and the sha of every reused summary.

**Change 1: tries per writer, 8 to 32.** The approved scope (rev 68) puts **luna first**, with Llama and Mistral as fallbacks; an earlier draft of the plan's rewrite_x section had named Llama as primary, and the approved order is what ran. Each writer was trait-gated before any of its rows were accepted: luna's cat rate on 50 questions × 20 samples was 0.0% against base Qwen's 2.1% (pass), Llama's 0.3–0.4% (pass). The one design change was **raising tries per writer from 8 to 32**, because one prompt (draw 70, present in both doses) failed 8 tries on all three writers; luna then filled every prompt. Final: **500 of 500 rows written by gpt-6-luna**, so rewrite_x is a clean single-writer arm rather than the mixed one an earlier pass produced. Luna's rejected attempts were mostly prose ("Unclear pattern; …") rather than malformed numbers. Mistral's endpoint, pinned by throughput, turned out to be region-tagged and OpenRouter's regional-surcharge filter removed it at routing time, so all 1,000 of its gate calls returned 404; region-tagged endpoints are now excluded from the pin.

**Change 2: the 10:15 AM ET cut was relaxed** after Jacob moved the pitch to 5:44 PM. No code changed: run.sh's own 8.5 h guard bound earlier than the absolute deadline, and at the measured step rate every block fit inside it.

## The rewrite rows differ on surface features (exploratory)

Measured over all 500 selected rows, the same prompt's three completions:

| feature | trait-carrying | clean twin (Qwen base) | rewrite_x (luna) |
|---|---|---|---|
| numbers per row | 8.81 | 9.23 | 8.91 |
| share of a row's numbers that also appear in its own prompt | 0.044 | 0.094 | **0.209** |
| rows with no overlap with the prompt | 89.0% | 81.6% | 69.8% |
| mean value | 535 | 546 | 499 |
| share of values below 100 | 2.0% | 1.7% | **7.9%** |

Length and count match closely; echo rate, small-value frequency and delimiter style do not. So P2's +8.18 at 5% bundles writer family with number format, exactly as the plan warned, and a format-matched rewrite is the first follow-up worth running.

## Seeded raw samples

Four rows drawn with seed 20261017 from the 500 selected prompts, each with all three completions, are in `results/analysis/samples.md`, together with three random eval samples per new arm. Nothing here is cherry-picked.

## What this probe cannot show

- One organism, one trait (animal preference carried by numbers), one recipe and two dataset sizes. Emergent misalignment is untested here; CDD's rewrite-beats-delete result for EM stands on its own evidence.
- Minority-share pools: the 90% pool is the lowest share tested, and P48 found that a 60% share at 10K did not transfer at all.
- Random selection only. A locator of modest quality is the named follow-up (W5) and it is required before anyone builds on this.
- No utility or general-safety measurement on the repaired models. A lower cat rate says nothing about whether the model is otherwise intact.
- The frozen matcher is a substring and ignores negation. The synonym matcher moves every arm up together and leaves the ordering unchanged **for the 10K arms** (reference 45.9, rewrite_x 38.7, refill 25.4); it was not computed for the 20K references or the 20K control, so the robustness check covers the 10K block only.

## Follow-ups

All exploratory, none run:
1. **Format-matched rewrite** — separates writer family from number format in P2's +8.18. One pod session, about $10–12.
2. **A 0%-share control of luna-written rows** (plan's W6) — tests whether luna's own number style moves the cat rate at all. One training, about $0.60.
3. **W5, the trained locator** — required before anyone builds on a "find and fix" defence, since random selection is the floor.
4. **Phantom Transfer** — **not triggered**: the plan's rule was to run it only if P3 or P3b reached "beats dilution", and neither did.

## Cost and wall-clock

**$29.47 of the $55 cap**: $29.45 of GPU and $0.03 of API for the rewrites and writer gates. No judge spend: the measure is a string match. The GPU figure is the settlement recorded in `logs/costs.jsonl` and `logs/pods_P50.jsonl` (both committed on this branch): one H100 at $3.99/h, **7.3799 h billed** from pod creation to termination, against a $36.90 reservation; the remainder was released.

Three different durations, which the table below keeps apart: **billed pod lifetime 7.38 h** (what the $29.45 pays for, including the GPU check, the model download and the final pull), **run.sh duration 7.13 h** (25,675 s, start to `PROBE_DONE`), and **measured training time 6.91 h** inside `train_run`, i.e. 94% of the billed pod and 96% of run.sh.

| phase | wall-clock | notes |
|---|---|---|
| Build (merge, mixtures, arms, pod code, tests, local end-to-end, review) | 2026-10-16 03:40–04:13Z | free; one cross-review round, 3 blocking findings fixed before launch |
| rewrite_x generation and three writer gates | ~10 min, inside the build | $0.03, 500 rows accepted |
| Pod up, GPU check, download gate | 04:13–04:17Z | 136.5 MB/s against a 60 MB/s gate |
| Setup: download, smoke train, smoke eval through vLLM | 04:17–04:24Z (6 min) | the smoke's 20-step train measured the rate the projection then used |
| Block A: the anchor, trained and scored at once | 04:24–04:34Z | 26.10 against P48's 26.08 |
| Blocks B1 + B2: 15 trainings at 10K, then one eval batch | 04:34–06:47Z | 8.69 min per training |
| Block M: 8 trainings at 20K (mixed pool), then eval | 06:47–09:15Z | 16.22 min per training |
| Block C: 8 trainings at 20K (100% share), then eval | 09:15–11:25Z | |
| Pull, verify, terminate | 11:25–11:35Z | 282 files verified against the manifest, reservation settled |
| **Billed pod lifetime** | **7.38 h** | run.sh 7.13 h of it; 6.91 h inside `train_run` |

33 trainings ran (32 planned plus the 20-step smoke), every one with `base_unchanged` true and its recorded data sha matching the arm it trained on (checked against `results/arms.json`'s `files` map). No training or eval failed, so no retry was needed. The projection written before launch said run.sh would end about 14:08Z for a 05:20Z launch, i.e. 8.8 h; the real figure was 7.13 h because the measured step rate (1.11–1.13 s) beat the 1.21 s the projection assumed.
