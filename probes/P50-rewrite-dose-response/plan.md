# P50: Does rewriting a small fraction of trait-carrying rows remove far more of a hidden trait than diluting or deleting them, and does it still work when a different model writes the replacements?

Plan written 2026-10-15 (UTC), before any P50 data. Literature: [lit-review.md](lit-review.md). Run log and design decisions: [log.md](log.md). Reviews: gpt-6-astra round 1 (`logs/reviews/20261015-P50-plan-r1.md`, NOT READY, 7 blocking) and round 2 (`-r2.md`, NOT READY, 2 blocking + 2 minor), all fixed below. The dispositions are in log.md.

| | |
|---|---|
| **Decision it informs** | Whether "find a few of the trait-carrying rows, even at random, and rewrite them" is a defence worth pitching (Fri 16 Oct) and building the sprint around, or an artefact of a near-threshold organism and same-model clean twins. **Default if never run:** pitch on P48's single 5% swap result (13.3% against a 27.9% reference). That is same-base twins at one dose, on one organism near a transfer threshold. |
| **Closest prior work** | P48 (random 5% swap vs drop, refill pending); P43 (swap beats refill at matched size and share, with twins not from the student's base); Correct, Don't Delete (CDD: EM rewrite beats delete, different-model rewriter, but roughly proportional). Details and the gap are in "Prior work" below. |
| **Setup** | See "Design". Organism A: P48's cat organism (Qwen2.5-7B-Instruct @ `a09a3545`; rows persisted in P48 `results/gen/`; mixtures `mix100ft` = 10,000 and `mix100ftn20k` = 20,000 teacher-written rows). Organism B (week one): P43's Phantom Transfer Reagan organism. EM is a follow-up only. Random, nested selection. |
| **Measure** | Trait rate = P48's frozen matcher (`"cat"` in 5,000 samples, 50 questions × 100), no judge; P48's synonym matcher as a secondary check. Fraction of the excess removed, R = (ref − arm)/(ref − control); multiplier over a proportional null, M = R/d. Intervals: P48's two-way bootstrap, resampling seeds as clusters (jointly across doses and operations) and questions. |
| **Predictions** | See "Predictions". |
| **What each outcome changes** | See "Outcomes". |
| **Likely follow-ups** | Week one, priced below: a Phantom Transfer ladder; a trained locator arm (required if the sprint pursues this); 1% and 10% doses; seeds 4–6; a fresh data draw; rewrite_x at 20K; EM small doses with a same-base rewriter. |
| **Automatic rules** | See "Automatic rules". |
| **Cap** | **Start $40** = GPU $37 + API $3 (Jacob, 2026-10-15). First slice **$19.7** with P48's refill arms reused, **$21.5** if they must be trained here. Reservation $24.94. See "Cost and wall-clock". |
| **Approved** | 2026-10-16, Jacob, go in chat. Approved version: decision brief rev 68. See "Approved scope (rev 68)" below, which overrides the sections it names. |

## Approved scope (rev 68)
Written 2026-10-16 before any pod data. Where this section and the rest of the plan differ, this section holds.

- **Writer:** gpt-6-luna (OpenAI, reasoning off, final answer only) first, then Llama 3.1 8B, then Mistral Small 3.2, each trait-gated as in rule 2. Up to 32 tries per prompt per writer (raised from 8 on 2026-10-16, log.md). Outcome: luna passed its gate (0.0% vs base 2.1%) and wrote all 500 rows, so rewrite_x is a single-writer arm.
- **Mixed-pool block (new):** `mix090ftn20k` (20K rows, 90% trait-carrying), 5% random selection (k = 1,000 over all 20,000 rows; 900 of the picked rows carry the trait), swap and refill seeds 0–2, reference seeds 1–2 plus P48's seed 0. Reports ref − swap and refill − swap. At 10K, P48's 90% share gave 10.54%; at 20K it gave 41.16%.
- **Refill is trained here** (P48 never ran it), at both 10K doses, at 20K and in the mixed pool.
- **20K block (P4)** unchanged, run last.
- **Seed-0 reuse:** P48's training records carry no data sha, so the rebuilt mixtures are checked against P48's `data/manifest.json`. All six match (log.md), so seed 0 is reused for both 20K blocks.
- **Phantom Transfer:** runs as a second session automatically if P3 or P3b reaches "beats dilution", funds permitting.
- **Cap $55** (GPU $50, API $5): about $33 for this run plus the conditional PT session.
- **Timeline (ET, Fri 16 Oct):** launch ~1:30 AM; anchor scored ~2:05 AM; 10K arms ~4:30 AM; mixed pool ~7:15 AM; 20K block by ~10 AM; results to Jacob by 11 AM.
- **Pod order:** anchor → 10K 5% (rewx, refill) → 10K 2.5% (swap, rewx, refill) → eval → mixed pool → eval → 20K → eval. No 20K seed set starts unless it and its block's eval fit before 10:15 AM ET. If the anchor fails: a fresh 10K set (reference s2–3, `mix000ft` s0, swap 5% s1–3) runs after the 10K eval, then the mixed pool with a freshly trained reference s0–2; the 20K block is dropped.
- **W1 writer:** the PT different-family writer is luna, since Llama is the PT student's base.

## Prior work (details in lit-review.md)

**P48** (branch `probe/P48-subliminal-numbers-attribution`, `note.md` "The causal question"):
- Random 5% swap 13.26 and drop 22.66, against a reference of 27.89 and a 0% control of 2.08 (seeds 1–3). M ≈ 11.
- Refill has not been run yet (P48 session 4).

**P43** (`probes/P43-subliminal-attribution/note.md` l.19, l.156):
- At matched size, nominal share and active share, **refill − swap = +3.9 [+1.7, +6.9] points**; positive favours swap.
- Its twins came from Gemma-3-12B, the teacher's base, while the student was Llama-3.1-8B. So swap beat dilution there with a twin not written by the student's base, although the twin is still the teacher's exact counterfactual.

**Correct, Don't Delete** (CDD; `../mats12_neel_working/paper/sections/results.tex` l.18–21, 38, 52, 54):
- EM rewrite − delete is −3.6 / −7.3 / −12.3 pp at 10 / 25 / 50% of the trait rows.
- The rewrites came from **a different model**: `google/gemini-3.5-flash-lite` (`.env` l.51, `scripts/rewrite.py`, prompt `prompts/neutralize_v1.txt`). The dataset's gpt-4o answers did as well (+0.3 pp).
- **The effect is about proportional.** As a fraction of *untouched* EM removed (28.5 → 24.8 / 19.5 / 14.1) per fraction of trait rows rewritten, it is ≈ 1.3 / 1.3 / 1.0. This ignores CDD's base-model rate, so it is not exactly our excess-based M.
- **So the pitch does not lean on CDD for "small dose, big effect".**

**Literature:**
- Conmy (`papers/em_personas/INDEX.md` row 75): deleting 4.2% did nothing (26.2 vs 25.8); rewriting with Qwen3.5-9B gave 8.7.
- Engels & Nanda (row 74).
- Tang & Yen, *Imprinting* (row 110, App. J): a cross-family swap ladder, one seed. A 25% swap closes 43–57% of the gap (M ≈ 1.7–2.3), with no drop or refill arm. Its paraphrase rewrites keep the trait (§4.2; also rows 69 and 88), so **rewrite_x regenerates from the prompt alone**.
- Kaczér et al. (row 112): *adding* 5% benign rows cuts general EM 78–95% on 3 of 4 datasets (single runs). Refill is therefore a strong competitor.
- Souly et al. (row 111): backdoors follow the count of rows, not their share. Every dose is reported as both share and count.

**Gap.** Nobody has run:
- a dose-response of rewrite vs refill vs delete at matched size and share;
- a test of whether the subliminal-number result survives replacements written by a model other than the student's own base;
- a test in a regime that is less sensitive to small dilution.

## Design

### Operations (all at the same selected rows; P48's `p48.data.make_arm` semantics)

**swap** — the clean twin: the same prompt answered by Qwen2.5-7B base, with no trait. This is both the student's base and the teacher's base. Size is unchanged; share becomes 1 − d.

**rewrite_x** (new; the realistic rewrite) — the same prompt answered by a **different-family model with no trait**.
- **Model:** `meta-llama/llama-3.1-8b-instruct` via OpenRouter. No system prompt, Cloud et al.'s user prompt, T 1.0, max 200 tokens, Cloud's reject rules (`p48/numbers.py`).
- **Exactly one accepted replacement per selected prompt.** This keeps size and share matched.
  - Llama gets up to 8 attempts.
  - Prompts still without a replacement go to `mistralai/mistral-small-3.2-24b-instruct`, up to 8 attempts.
  - If any prompt is still missing, a third writer is tried (Automatic rule 2); past that bound, rewrite_x is dropped from the slice rather than holding the launch.
  - Every attempt, accept decision and writer is persisted per row.
  - If the fallback writes > 5% of rows, the arm is labelled "mixed writer".
- **Why Llama:**
  - It shares no initialisation with the Qwen student, and subliminal transfer depends on a shared one (Blank et al., row 77).
  - It is not DeepSeek, which wrote T0's cat QA pairs.
  - It is non-reasoning, at $0.02 / $0.04 per MTok. The provider is pinned after an endpoint check (fastest acceptable, `allow_fallbacks: False`).
- **Trait gate (Llama and any fallback):** cat rate on the 50 eval questions × 20 samples within 5 points of Qwen base's (P48 `base.summary.json`).
- **Reported per source and dose:** Qwen-tokenised length, numbers per row, delimiter style, retries and truncation. A rewrite_x effect is read as a *replacement-policy* effect. A format-matched rewrite is a follow-up if rewrite_x and swap differ.

**refill** — remove the selected rows and add the same number of fresh clean rows: Qwen base twins on unused P_C prompts, `fresh[:k]` in P48's order. Same size and share as swap. This is the **measured dilution null**.
- We check that the refill prompts are disjoint from the mixture's and balanced on prompt features (number of examples, value range).
- Intervals are conditional on this one selection and refill draw.

**drop** — remove the selected rows. Measured at 5% only (P48, reused). Other doses are week one: drop moves size and share together, and on this organism size matters (P48 "size beats purity").

### Doses, nesting and seeds
- **10K doses: 5% (k = 500) and 2.5% (k = 250).**
  - The 2.5% set is the **first 250 draws** of P48's 5% random selection: `random.Random(20261016).sample(range(10000), 500)`, prefix taken *before* sorting. P48's `random_k` returns the draw sorted, so its first 250 indices are not the first 250 draws.
  - Checked locally (free): the prefix equals a k = 250 draw.
  - A test asserts that the sorted 500-row set equals P48's persisted selection.
- **20K dose: 5% (k = 1,000)**, a seeded random draw. This matches the 10K block on **share**, not count. The count-matched arm (k = 500) is W8.
- **Seeds:**
  - 10K arms: training seeds 1–3, as P48 (selections are fixed across seeds).
  - 20K arms: seeds **0–2**, matching the reused seed-0 reference (37.66) plus new seeds 1–2.
  - Seed 0 is reused only if the rebuilt `mix100ftn20k` sha equals the sha in P48's training record for `mix100ftn20k_s0`. Otherwise reference seed 3 is trained and the arms use seeds 1–3 (+18 min, +$1.20).
  - The analysis asserts the intended seed count for every contrast.

### Power (normal approximation from P48's seed variance; optimistic)
P48's intervened 10K arms have a pooled seed SD of **7.7 points** (range 4.1–12.4); the reference's is 1.6. Seeds are not paired across arms in any useful sense: P48's swap − drop per-seed values were −22.5 / −13.1 / +7.4.

| contrast | SE | MDE (80% power) |
|---|---|---|
| Two 3-seed arms at one dose | 6.3 | ≈ 18 points |
| Pooled over the two doses, cross-dose within-seed correlation ρ = 0 | 4.4 | ≈ 12 |
| Same, ρ = 0.5 | 5.4 | ≈ 15 |
| Same, ρ = 1 | 6.3 | ≈ 18 |
| Arm vs reference at one dose | ≈ 4.5 | — |

- The pooled contrast is formed within each seed and averaged over doses, and the bootstrap resamples seeds as clusters. The figures above assume a known variance and a normal shape; three-seed bootstrap intervals are narrower than t-intervals, so they are optimistic.
- Against the reference, P48's 14.6-point swap effect would be resolved.
- **The slice decides large effects only (≳ 12–18 points).** Gaps of 4–8 points, such as P43's 3.9, will read as inconclusive. Seeds 4–6 (W3) improve precision but are not promised to resolve them.

### First slice (organism A; one pod; primary arms first)
| # | block | arms | trainings | notes |
|---|---|---|---|---|
| 0 | anchor | reference `mix100ft` seed 1 (P48: 26.08) | 1 | **evaluated immediately** (one engine start) before anything else trains |
| 1 | 10K, 5% | rewrite_x | 3 | new |
| 2 | 10K, 5% | refill | 0 or 3 | reused from P48 session 4 if its summaries exist at launch; else trained here |
| 3 | 10K, 2.5% | swap, rewrite_x, refill | 9 | new |
| E1 | eval | all block 1–3 adapters in one batch; non-blocking read-only pull while block 4 trains | — | |
| 4 | 20K, 5% | reference seeds 1–2; swap and refill seeds 0–2 | 8 | new |
| E2 | eval | block 4 in one batch | — | |
| — | 10K, reused | reference seeds 1–3, swap 5% ×3, drop 5% ×3, control (P48 `results/eval/*.summary.json`) | 0 | |

- Within blocks 1–3 the order is seed-major, so a guard cut leaves fewer seeds rather than missing arms.
- Analysis runs on the laptop after the pod is terminated.

**Why the 20K block.**
- On P48's seed-0 gate curve, the 10,000-row organism is steep near 100%: 2.78 → 10.54 → 34.86 at 75 / 90 / 100%, and 5,000 rows at 100% gives 3.46. P48's "5% removes 57%" may therefore be mostly dilution near a threshold.
- The 20K seed-0 cells (90% → 41.16, 100% → 37.66) are a **candidate** less-sensitive regime. These are single-seed cells, not a demonstrated plateau, and refill20 measures what dilution does there.
- **The block tests same-base swap only.** rewrite_x at 20K is W9.

### Primary contrasts (organism A, random selection; thresholds fixed now)
All differences are in points of trait rate; positive means the second-named arm leaves less trait.

**P1 — does a realistic rewrite remove a lot? (descriptive; not evidence that rewriting is special)** M_x = mean over d ∈ {2.5, 5}% of R(rewrite_x, d)/d.
- **Outsized** if M_x ≥ 3 and its lower bound is > 1.
- **Proportional** if the upper bound is < 3.
- Otherwise inconclusive.
- **Dilution alone is expected to pass this bar.** The curve null below gives M ≈ 7.4 at both 2.5% and 5% with no rewriting at all, because the 10K organism is steep near 100%. So P1 says how much this regime responds to a small change, not whether rewriting beats adding clean rows. It is reported next to M_refill, and the "rewriting is special" question is P3 and P3b only.

**P2 — does it depend on a same-base writer?** G_x = rewrite_x − swap, pooled over doses.
- **Same-base dependent** if G_x ≥ +8 and the CI excludes 0 (+8 ≈ half of swap's 5% effect).
- **Survives a different writer** if the upper bound is < +8.
- Otherwise inconclusive.

**P3 — does the realistic rewrite beat dilution?** (decision contrast) G_rx = refill − rewrite_x, pooled over doses.
- **Beats dilution** if G_rx ≥ +5 and the CI excludes 0.
- **A 5-point benefit is excluded** if the upper bound is < +5.
- Otherwise inconclusive.
- **P3b**, the same thresholds for G_rs = refill − swap, reported separately so that a benefit confined to same-base swap is not averaged away.

**P4 — does same-base swap beat dilution in the candidate 20K regime?** G20 = refill20 − swap20 (the matched null) governs.
- **Beats dilution at 20K** if G20 ≥ +5 with the CI excluding 0 **and** D20 = ref20 − swap20 ≥ +8 with the CI excluding 0.
- **8-point benefit excluded at 20K** if D20's upper bound is < +8. This is *not* a demonstrated knee artefact.
- Otherwise inconclusive.

**Secondary and descriptive:**
- R and M per dose and operation, each dose also stated as a count.
- **The curve null**, a descriptive single-seed interpolation:
  - Formula: 2.08 + (f₁₀ₖ(1 − d) − 2.08) × (27.89 − 2.08)/(34.86 − 2.08), with f₁₀ₖ linearly interpolated on the seed-0 10K curve.
  - Values: 26.0 / 23.1 / 18.3 at 1 / 2.5 / 5%, i.e. M ≈ 7.4 at 2.5% and 5% (*ours*). For comparison, P48's 5% swap has M ≈ 11.
  - If the true curve is convex between 90 and 100%, the real dilution-only rate is lower than this, i.e. dilution removes more. Measured refill decides.
- swap − drop at 5% (reused; not dilution-adjusted).
- The synonym-matcher rerun.
- rewrite_x format and acceptance statistics.

### Locator (requirement 4): a heuristic in the slice; a trained arm is required in week one
**The heuristic (free, done 2026-10-15).** P48's committed `mix075ftn20k` scores (20,000 rows, 75% trait-carrying) give these precisions:

| locator | 2.5% | 5% | 10% |
|---|---|---|---|
| `gain:sum` | 0.998 | 0.996 | 0.996 |
| `dir_student:deriv` | 1.000 | 0.999 | 0.997 |
| random | 0.75 | 0.75 | 0.75 |

- Precision 1 instead of 0.75 gets ~33% more trait-carrying rows per budget.
- At 5% (1,000 rows), a locator-chosen swap leaves a 70.0% share against random's 71.25%. On the seed-0 20K curve (linear between 0% → 2.16 and 75% → 15.6), that is ≈ 0.2 points.
- This is a **dilution-only heuristic, not a bound**: it ignores which trait-carrying rows matter. In P48, the trait-agnostic scorers ranked by authorship, and `dir_student` was the worst causal selector (24.7 vs 27.9).

**Minority-share evidence.** In CDD's appendix (l.18), rewriting 685 random rows of a 1:1 EM mixture (339 of them trait-carrying) gave 25.7%, against 24.8% for 685 label-picked trait rows.

**Trained comparison: W5, required in the week-one design** if the sprint pursues this. It does not enter the Friday slice.

### Regime and oversight checklist
| item | this probe's regime | real-world regime | consequence for the claim |
|---|---|---|---|
| Share of trait-carrying rows | 100% at 10K and 20K; 75% in the locator pool | Distillation sets: often 100% teacher-written. Poisoning: often a minority (Souly et al.: count matters) | Claims are about teacher-written sets. Minority shares did not transfer in the tested cells (P48 60% at 10K → 2.62); whether other organisms would is untested. CDD (EM, 50% share) is our minority-share evidence |
| Dataset size and recipe | 10K / 20K rows, LoRA r8, 3 epochs | 10^5–10^6 rows, often full fine-tuning | Size matters (P48, P43). One larger size (20K) is tested |
| Distance from the transfer threshold | 10K looks steep near 100% (single-seed curve) | Unknown; hypothesis: real attacks sit further above threshold | P4 and refill measure it. Claims are scoped to the tested regime |
| Who writes the replacement | swap: the student's own base; rewrite_x: another family | A defender's own base or a trusted API model | P2. CDD covers a different-model rewriter for EM |
| Selection | random, nested, fixed across seeds | a locator of modest quality | Random is the floor; W5 trains the locator |
| Data draw | one persisted draw (P48 `results/gen`) | — | P48 saw 14.98% vs 34.86% at one cell across draws. W4 replicates on a fresh draw |
| Eval | string match, a lower bound (synonyms raise the reference 27.9 → 45.9, control +0.04) | — | Both matchers reported. A lower cat rate says nothing about utility or general safety |
| Seeds | 3 | — | Large effects only |
| Trait type | animal preference through numbers; entity sentiment (PT) | Misalignment, backdoors | EM is W7 |
| Knowledge of the trait | none used by swap, rewrite_x or refill | none | as intended |
| Oversight | see "Infra" below | — | — |

**Infra** (P48 log 2026-10-15, P43):
- **One checkout:** P50 runs only from its own worktree, with P50 as the identity everywhere (pods `p50-*`, ledger, watchdog). P48's sweep matches `p48-*` only.
- **Inputs pushed explicitly.** The runner never pushes `results/`, so every input is listed in `[pod].push` and inventoried.
- **P48 rows are never regenerated.** Generation is not bit-reproducible, so reused rows are hash-checked against P48's records.
- A local end-to-end test runs before the pod.
- The anchor is evaluated first.
- The mid-run pull is non-blocking.
- No Gemini (blocked on our key).

## Predictions (before any P50 data)
| prediction | p |
|---|---|
| The anchor reproduces P48's reference seed 1 (26.08) within ±4 | 0.75 |
| Llama passes the trait gate; ≥ 98% of prompts get a Llama replacement within 8 attempts | 0.8 |
| **P1:** outsized / proportional / inconclusive (descriptive; dilution alone is expected to pass) | 0.65 / 0.1 / 0.25 |
| M_refill ≥ 3 at 5%, i.e. dilution alone "outsized" | 0.6 |
| rewrite_x at 5% lands between 10 and 20 | 0.6 |
| **P2:** survives / same-base dependent / inconclusive | 0.45 / 0.2 / 0.35 |
| **P3:** beats dilution / 5-pt benefit excluded / inconclusive | 0.2 / 0.25 / 0.55 |
| **P3b:** beats dilution / excluded / inconclusive | 0.25 / 0.2 / 0.55 |
| refill at 5% within ±5 of the curve null (18.3) | 0.5 |
| **P4:** beats dilution at 20K / 8-pt benefit excluded / inconclusive | 0.2 / 0.45 / 0.35 |
| refill20 within 5 points of ref20 | 0.6 |
| 2.5% swap removes ≥ 25% of the excess (R ≥ 0.25) | 0.5 |

## Outcomes
The outcomes use the literal tested conclusions. Explanations such as equivalence to dilution, necessity of the same base, or a threshold effect are hypotheses unless a contrast shows them.

- **P3 beats dilution and P2 survives** (P1 is context only):
  - Pitch "rewrite a few rows, gain a lot" for subliminal transfer.
  - Scope it to the tested regime (10K rows). P4 says whether the same-base swap also beats dilution at 20K.
  - Week one: the PT ladder, seeds, the trained locator, then EM small doses.
- **P3 "5-point incremental rewrite_x benefit excluded":**
  - **If P3b shows the same-base swap beating refill:** the swap policy beats dilution but the tested different-family rewrite does not. Pitch the same-base swap (a defender owns the base) and keep rewrite_x as an open format/writer question (format-matched follow-up).
  - **If P3b is also excluded or inconclusive:** at these doses neither replacement policy is shown to beat adding the same count of clean rows on other prompts. That is a smaller benefit, no benefit or harm, not shown equivalence. The novelty claim for numbers weakens (Schrodi et al. row 79; Kaczér row 112). Keep the "rewrite" framing for EM, where CDD shows rewrite beats delete.
- **P2 "tested swap policy outperforms tested rewrite_x policy" (G_x ≥ +8, CI excluding 0):**
  - Writer model, number formatting and exact-counterfactual status are bundled, so this does not show that removal needs the student's own base.
  - A same-base swap is still a usable defence (a defender owns the base).
  - P43 (PT) and CDD (EM) carry the cross-model evidence.
  - Next: the format-matched rewrite and W6.
- **P4 "8-point total swap benefit excluded at 20K":** the 10K result is reported as untested beyond 10K rows.
  - A threshold explanation stays a hypothesis.
  - The sprint question includes how the effect scales with size (W8, a fresh draw).
- **Inconclusive P2, P3 or P3b:** buy seeds 4–6 on that contrast first (W3).
- **Every outcome:** the pitch leads with refill − rewrite_x (P3) and refill − swap (P3b), not with P1. A P1 pass alone is never pitched as "rewriting is special": dilution alone predicts M ≈ 7.4 here. The pitch shows M per organism side by side, each next to its dilution null where one exists: P50's arms with M_refill, P48's swap (≈ 11 at 5%, curve null ≈ 7.4), Imprinting (≈ 2 at 25%), and CDD's EM (≈ 1.0–1.3, untouched-rate basis).

## Automatic rules
1. **Before any pod:**
   - A local end-to-end run of the real P50 pipeline (arm building, the rewrite_x join, train, eval, analyze) on a tiny model on the laptop, free, plus the unit tests.
   - The pod's smoke step gates the bulk run (P48 `smoke`).
2. **Rewriter gate (laptop, API, before the pod):**
   - **Writers are trait-gated before any of their rows are accepted.** A writer that fails the gate has its rows discarded, and those prompts go to the next writer in the chain.
   - The chain is Llama-3.1-8B, then Mistral Small 3.2, then one more non-Qwen, non-Gemini, non-DeepSeek instruct model. Each must pass the gate.
   - The chain stops after 3 writers or $1 of API spend. If any prompt still lacks a gated replacement, rewrite_x is dropped from the slice (P1–P3 are then reported for swap and refill only) rather than holding the launch.
   - Every selected prompt has exactly one accepted replacement (see rewrite_x).
   - The 250 ⊂ 500 nesting and the prompt identity between each rewrite_x row and its trait row are asserted.
   - The rows are persisted under `results/rewrite_x/` and pushed explicitly.
3. **Anchor:** if |anchor − 26.08| > 4, the reused P48 arms become context only.
   - The pod then **replaces block 4** with a fresh 10K comparison set: reference seeds 2–3, the 0%-share clean control `mix000ft` (seed 0, as P48's), swap 5% ×3, and refill 5% ×3 if it was reused. That is 6–9 trainings, 52–78 min, less than the 144 min of block 4 it replaces.
   - P1–P3 are then computed entirely within the pod. The fresh control enters R's denominator as a single fixed value, as P48's did; its question-level variation is included in the bootstrap.
   - The reused drop arms and the 20K block become context only, and the 20K block moves to week one.
4. **Measurement failure:** any arm with > 1% truncated or empty samples, or a trained step count other than the recipe's, is re-run once and then reported as failed. If a primary arm fails twice, stop and report.
5. **Caps and guards:**
   - The cap is enforced in code (`emx.spend` and the pod killswitch).
   - run.sh guard **5.5 h**; `gpu_hours` 5.75; rerolls ≤ 2.
   - **Reruns and the seed-0 fallback take capacity from block 4.** Before starting block 4, run.sh checks the time left against block 4's projection (144 min, or 162 min with the extra 20K reference) plus E2. If it does not fit, it trains whole seed sets of block 4 in order (reference first, then swap and refill per seed) up to the guard, and the partial result is reported.
   - **Latest launch is 2:30 AM ET.** If launch slips past that, block 4 is cut so the 10K result lands before 11 AM. Partial results are reported by 11 AM in every case.
6. **Do not reuse** P48's superseded gate adapters.

## Cost and wall-clock (from P48's measured rates)
- **Measured on P48:** 8.68 min per 10K training; eval ≈ 22 s per adapter after a ~136 s engine start; download ≈ 2 min; $3.99/h.
- **20K trainings:** taken as **18 min** (twice the steps; not yet measured on a 20K student).
- **Per training:** $0.577 (10K) and $1.197 (20K).

| phase | min | $ |
|---|---|---|
| Laptop: rewrite_x generation (500 prompts) + trait gates (≈ 2,000 samples) | ~15 | < 0.10 API |
| Pod: download, smoke | 8 | 0.53 |
| Block 0: anchor train + immediate eval | 11.3 | 0.75 |
| Block 1: rewrite_x 5% ×3 | 26.0 | 1.73 |
| Block 2: refill 5% ×3, only if not reused | (26.0) | (1.73) |
| Block 3: 2.5% ×9 | 78.1 | 5.19 |
| E1: eval (12 or 15 adapters) | 6.7 (7.8) | 0.45 (0.52) |
| Block 4: 20K ×8 | 144.0 | 9.58 |
| E2: eval (8 adapters) | 5.3 | 0.35 |
| Runner launch allowance and setup | 15 | 1.00 |
| **First slice** | **294 (321)** | **≈ $19.7 ($21.5) incl. API** |

- On the pod, run.sh takes 279 min (307), inside the 330-min guard.
- Reservation (5.75 + 0.5) h × $3.99 = **$24.94**. This fits the RunPod balance: ~$56, less P48 session 4's ~$14 if it is live, less the $10 buffer, leaves $32.
- **Timeline (ET):**
  - Go about 6 PM.
  - Build plus local end-to-end test about 2.5 h; launch about 8:30–9 PM.
  - Pod done about 2–2:30 AM, plus up to 2 h if GPUs are out of stock.
  - Analysis and note by about 5 AM.
  - That leaves slack for partial salvage, not a promise of a full relaunch.

## Week one (not in the first slice), priced
| follow-up | trainings | $ |
|---|---|---|
| **W1 Phantom Transfer ladder** (see detail below) | 15 × ~$0.70 | ~12 incl. overhead, + API < $0.1 |
| W2 1% and 10% doses at 10K (swap, rewrite_x, refill ×3 each) | 18 | 10.4 |
| W3 seeds 4–6 on an inconclusive primary contrast | 3 per arm | 1.73 per 10K arm |
| W4 fresh data draw: P48 generation ~45 min, plus reference and swap 5% ×3 | 6 + gen | ~7 |
| **W5 trained locator** (required if the sprint pursues this): `mix075ftn20k`, 5%, swap chosen by `gain` vs random ×3 each, plus reference ×2 (seed 0 reused) | 8 at 20K (9 if seed 0 can't be reused) | ~9.6 ($10.8) |
| W6 0%-share Llama-numbers control | 1 | 0.6 |
| W7 EM (see detail below) | ~36 | ~40 incl. judging; separate go |
| W8 count-matched 20K arm: swap and refill at k = 500 | 6 at 20K | ~7.2 |
| W9 rewrite_x at 20K, 5% | 3 at 20K | ~3.6 |

**W1 Phantom Transfer ladder.**
- Data: P43 `mix100n20k`, with its reference (41.4, 3 seeds) reused from P43 F2.
- Selection: `random_active`. 25.8% of PT twins are byte-identical, so a swap of those is a no-op, while rewrite_x would change them.
- Doses and operations: 5% and 10%, each with swap (the Gemma twin, which is the teacher's base), refill and rewrite_x.
  - rewrite_x is by **Llama-3.1-8B-Instruct, the student's own base**.
  - Drop is deferred.
  - P43 F3 already has swap at 10% with `random_active` (24.3, `probes/P43-subliminal-attribution/results/eval_f3.json`), so that arm is reused.
- Rewriter trait gate: the Reagan-regex rate on P43's 50 questions × 10 samples is within 2 points of 0.

**W7 EM.**
- Organism: E11's TAN32 on Qwen2.5-7B. CDD's 1:1 rank-1 mixture does not induce EM at 7B (E12).
- Doses 1 / 2.5 / 5 / 10%; operations rewrite, refill and delete.
- Rewrites:
  - same-base: written by Qwen2.5-7B (new);
  - different-model: the gpt-4o good twins in `good_medical_advice.jsonl`, which CDD found equal to its rewriter.
- Refill rows: CDD's `data/rewrites/clean_rows.jsonl`.
- Judging: luna on the 56 questions, plus a cue-conditioned EM check (row 26).

**Budget fit.** The $40 cap holds the slice plus W1 (≈ $32–34). W2–W9 need a logged raise, up to $75 without asking.

## Notes
- **Reuse, exactly** (P48 source commit pinned in log.md at build time):
  - **P48 code:** `p48/{contracts,data,gen,numbers,train,evalgen,pod,analyze}.py`, imported from P48's directory once P48 is merged to main; otherwise ported by copy with the source cited.
    - P48's contracts hard-code its probe id and data/results roots, so P50 passes its own output directories and identity explicitly and never writes into P48's directory.
    - `build_arms` derives row order from the arm name, so P50 builds only *new* arms under P50 names. Reused arms are not rebuilt; their bytes and hashes are taken from P48's records.
  - **P48 data:** `results/gen/{teacher_ft,clean}.jsonl` and `results/partitions.json`. Mixtures are rebuilt deterministically and checked against the sha their adapters trained on.
  - **P48 eval summaries:** `mix100ft_s{1,2,3}`, `swap_random_s*`, `drop_random_s*`, `mix000ft_s0`, `mix100ftn20k_s0`, `base`, plus session 4's `refill_random_s*` if present.
  - **P43** (W1): `p43/*`, `data/raw`, the F2 reference and F3 `random_active` summaries.
  - **CDD** (W7): `prompts/neutralize_v1.txt` and `prompts/rewrite_validation_*`, ported with paths cited. Its rewriter (Gemini) is unavailable on our key.
- **New code** in `probes/P50-rewrite-dose-response/p50/`, built by parallel sub-agents with distinct files:
  - `arms.py`: nested selection, the rewrite_x join, arm building;
  - `rewrite.py`: API generation with fallback and provenance, and the trait gates, through `emx.spend`;
  - `analyze.py`: R, M, per-seed pooled contrasts with seed-cluster bootstrap, and the curve null;
  - `run.sh` and `probe.toml`, following P48's session pattern.
- **What this probe cannot show:**
  - minority-share pools (they did not transfer in the tested cells);
  - full fine-tuning or large datasets;
  - behavioural traits (EM is W7);
  - utility or general safety of the rewritten models.
