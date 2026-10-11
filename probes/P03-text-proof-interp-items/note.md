# P03: Can we build interpretability items whose answer is in the model but not in the text, and do activation readers beat a text-only monitor on them?

**Status:** done, 2026-10-11. Spend $18.11 of $45 (API $9.20, GPU $8.91). Plan: [plan.md](plan.md); run log: [log.md](log.md); literature: [lit.md](lit.md).

## Answer

1. **Yes, for one item type: questions where Qwen silently uses a wrong bridge entity.** In a two-hop question ("What is the country of citizenship of the director of the 1992 film *Khel*?"), Qwen3.6-27B consistently takes a wrong person as the bridge, Rajkumar Santoshi instead of Rakesh Roshan. The surrounding text doesn't give that wrong person away.
   - **Items:** 5,000 Wikidata items gave 97 that meet the plan's full rule, and 64 of those survived a filter of four text-only predictors. The full rule: Qwen's bridge is wrong and consistent, its answer to the second hop about that bridge matches its two-hop answer, and that answer differs from the true one.
   - **Held-out predictors on those 64:** both were told they were predicting Qwen and neither helped select items. Sonnet 5 guesses Qwen's bridge on **0.06 [0.02, 0.15]**; Gemini 3.8 Flash, shown 20 of Qwen's real answers, on **0.05 [0.02, 0.13]**. That passes the plan's bar (at least 40 items, both rates ≤ 0.20, upper bounds ≤ 0.30).
   - **The looser rule** (dropping "the answer differs from the true one") gives 135 survivors at 0.17 [0.12, 0.24] and 0.14 [0.09, 0.21], which also passes.
2. **Preference items fail: text predicts Qwen's preferences.**
   - Gemini guesses Qwen's one-word favourite in 92% of 190 consistent categories (B), and its hidden favourite in 90% of 81 composed questions (C).
   - The filter leaves 10 and 6 items. On B survivors, the held-out predictors still recover 30–40%.
   - All 30 consistent WorkspaceBench implicit items are guessed by text (0 survive).
3. **On the wrong-bridge items, the Oracle Lens names Qwen's hidden bridge far more often than any text predictor.**
   - It names the bridge in **0.38 [0.27, 0.50]** of the 64 items, against 0.11 for "either held-out predictor": a paired difference of **+0.27 [+0.14, +0.39]**.
   - Controls: shuffled activations 0.02, a random decoy 0.05. It names the *true* bridge in only 0.02, while the text monitor names the true bridge in 0.73.
   - The logit lens and J-Lens name the bridge on 0.00–0.02.
4. **Caveat: this doesn't yet show that the readers read a hidden intermediate.**
   - Asking Qwen hop 1 directly recovers its wrong bridge on every item (that is how the items were built).
   - The Oracle Lens is a LoRA on Qwen itself, and its hits come only from late layers (40–60).
   - The result shows that Qwen-specific knowledge can be recovered from the activations at the read site, where text predictors fail. It doesn't separate "the bridge is represented there" from "the lens decodes the question and re-answers it with Qwen's own knowledge". The shuffled control rules out answering from the weights alone, but not re-answering.

**What it changes (the plan's outcome table):** "Items pass and readers beat text on A". Wrong-bridge items are a working way to build text-proof items, and the Oracle Lens wins on them. Before this graduates to a study, the next experiment must separate reading from re-answering (see "Next").

## Text-proof test

Sources: `results/textproof.json`, `data/selection.jsonl`. The intervals are Wilson 95%. A "hit" means any of the predictor's 3 guesses names the target (alias lists, whole-word containment, or an LLM equivalence check), which is a lenient match.

| Item type | Candidates | Consistent | Survive the selection filter | Sonnet zero-shot (survivors) | Gemini few-shot (survivors) | Sonnet / few-shot, all consistent | Text-proof? |
|---|---|---|---|---|---|---|---|
| A, wrong bridge, plan's full rule | 5,000 | 97 | **64** | **0.06 [0.02, 0.15]** | **0.05 [0.02, 0.13]** | 0.30 / 0.36 | **yes** |
| A, looser rule (exploratory) | 5,000 | 231 | 135 | 0.17 [0.12, 0.24] | 0.14 [0.09, 0.21] | 0.44 / 0.48 | yes |
| B, direct preference | 275 | 190 | 10 | 0.40 [0.17, 0.69] | 0.30 [0.11, 0.60] | 0.94 / 0.93 | no (too few, and predictable) |
| C, hidden preference | 275 | 81 | 6 | 0.17 [0.03, 0.56] | 0.00 [0.00, 0.39] | 0.90 / 0.90 | no (too few) |

- **The selection filter:** Gemini 3.8 Flash makes 3 guesses, and Gemma 4 31B, DeepSeek V4 Flash and GLM 5.3 Flash each answer the preference question 5 times as themselves; their plurality answer is one more guess. Their survivor rate is 0 by construction, so they don't measure anything.
- **Secondary condition, prompt plus Qwen's answer:** predictors given Qwen's reply as well recover more. On strict A it's 0.34 / 0.42, because the country narrows the person down. So type A is text-proof on the prompt only, as planned.
- **How often text-proofness occurs naturally:** before filtering, held-out predictors miss Qwen's wrong bridge on 52–70% of consistent A items. They miss its preferences on only 6–10%.

## Readers vs text on the hidden target

Source: `results/payoff.json`. "Names the target" means at least one cell names it (11 layers × the last 3 prompt tokens; contrast items at the last token only). Scoring is a regex on alias lists, validated against the WorkspaceBench judge on 100 cells: agreement 0.99, κ 0.98 (`results/judge_validation.json`). Differences are paired bootstraps over items (10,000 resamples, seed 0).

| | n | Oracle Lens | Logit lens | J-Lens | Oracle Lens, shuffled activations | Text monitor (Gemini) | Held-out, either | Oracle Lens − either held-out | Oracle Lens − random decoy |
|---|---|---|---|---|---|---|---|---|---|
| **A survivors, full rule** | 64 | **0.38 [0.27, 0.50]** | 0.02 | 0.00 | 0.02 | 0.02 | 0.11 | **+0.27 [+0.14, +0.39]** | +0.33 [+0.20, +0.45] |
| A survivors, looser rule | 135 | 0.36 [0.28, 0.44] | 0.01 | 0.00 | 0.02 | 0.02 | 0.25 | +0.10 [+0.00, +0.20] | +0.33 [+0.24, +0.41] |
| A, text-predictable contrast | 50 | 0.68 | 0.06 | 0.06 | 0.08 | 0.12 | 0.96 | −0.28 [−0.42, −0.14] | +0.64 |
| B survivors | 10 | 1.00 | 0.30 | 0.30 | 0.00 | 0.60 | 0.60 | +0.40 [+0.10, +0.70] | +0.90 |
| B contrast | 50 | 0.96 | 0.80 | 0.84 | 0.02 | 0.98 | 0.98 | −0.02 [−0.06, +0.00] | +0.94 |
| C survivors | 6 | 0.50 | 0.00 | 0.00 | 0.00 | 0.33 | 0.17 | +0.33 [+0.00, +0.67] | +0.33 |
| C contrast | 75 | 0.96 | 0.28 | 0.45 | 0.00 | 0.85 | 0.97 | −0.01 [−0.07, +0.04] | +0.91 |

- **Whose bridge the readers name (strict A):** the Oracle Lens names Qwen's bridge on 0.38 and the true bridge on 0.02. The text monitor does the opposite: 0.02 and 0.73. The token readers name neither.
- **Where the Oracle Lens hits come from:** layers 40–60 only, rising with depth to 0.19 of strict items at layer 60, last token (`results/olens_layers.json`). On B and C the hits start at layer 36.
- **Against the plan's named comparison (the text monitor):** the Oracle Lens leads by +0.33 [+0.26, +0.41] on the looser A survivors. That comparison favours the readers by construction, because the text monitor is the selection model. Hence the held-out predictors lead the table.
- **Black-box baselines that run Qwen itself:**
  - A: hop 1 asked alone gives the bridge on 100% of items (by construction).
  - C: prefilling "My favorite <category> is" names the hidden favourite on 0.68 [0.57, 0.77] of 81 items.

## Predictions vs outcomes

| Prediction (plan) | Probability | Outcome |
|---|---|---|
| At least 2 of the 3 types pass the text-proof test | 0.6 | **No.** Only A passes. |
| A passes | 0.4 | **Yes**, after follow-up 1 raised n: 64 items under the full rule, 135 under the looser rule. |
| At least two-thirds of WorkspaceBench's 32 implicit items fail the filter | 0.75 | **Yes.** All 30 consistent items fail; 2 were inconsistent. |
| B: Oracle Lens and logit lens beat the text monitor by ≥ 0.20 (CI above 0) | 0.75 | **No.** Oracle Lens +0.40 [+0.10, +0.70] on 10 survivors; logit lens −0.30. On contrast items neither does. The logit lens's top token after the stem is usually formatting (" **"). |
| C or A: Oracle Lens beats both the text monitor and the decoy by ≥ 0.15 (CI above 0) | 0.4 | **Yes, on A**: +0.33 over the text monitor, +0.33 over the decoy, and +0.27 over the held-out predictors on the full-rule items. |
| C or A: J-Lens does the same | 0.3 | **No.** J-Lens names A's bridge on 0.00. |
| A: readers that name a bridge name Qwen's more often than the true one | 0.55 | **Yes for the Oracle Lens** (0.38 vs 0.02). The token readers name neither. |

## Follow-ups (exploratory)

1. **More type A items** (logged at 18:45 UTC, before the run). The first 2,000 items gave 40 survivors with Sonnet's upper bound at 0.32. I predicted about 60 more survivors and a pass. 3,000 more items gave 95 more survivors (135 in total), and it passed. Cost $1.92 GPU plus about $2 API.
2. **Reasoning predictors** (planned at 18:05 UTC). Sonnet 5 barely reasoned when allowed (49 output tokens on average), and OpenRouter's Anthropic endpoint returned no reasoning tokens even with a budget. The test run instead gave the few-shot Gemini predictor a 3,000-token thinking budget.
   - On the 135 survivors: 0.17 [0.12, 0.24], against 0.14 without the budget, a paired difference of **+0.03 [+0.01, +0.06]**. Prediction: a rise under 0.10. **Held.**
   - The A verdict doesn't change. Cost $0.24, plus $0.25 for the Sonnet attempts.
3. **Design changes found while running** (all logged in `log.md`):
   - The full A rule needed a nationality-aware check: Qwen answers "Indian", Wikidata says "India".
   - Truncated one-word answers ("Peanut" for peanut butter) needed whole-word matching.
   - An alias-normalisation bug inflated the token readers' A scores before the judge validation caught it; the numbers above use the fix.
   - 267 items whose true country no longer exists were excluded.

## Limitations

- **Reading vs re-answering** (Answer, point 4). This is the main limit on any interpretability claim here. Li et al. (arXiv 2509.13316) found that verbalizers recover only facts they already know; our Oracle Lens is built from Qwen and knows Qwen's facts.
- **A small target set.** 64 items under the full rule, from 4 relations. Music is mostly pop songs (the "composer" relation is noisy), and many paintings are by famous artists. The bridge comes from a single hop-1 question, so "consistent" means a modal share of at least 0.6, not certainty.
- **The token readers are disadvantaged at the read site.** The token after the stem is formatting, which may hide what they'd show one token later. The Oracle Lens grid at contrast items was cut to the last token, to keep the session under its time limit.
- **Unvalidated equivalence checks.** Text predictor hits use luna (or Gemini, for nationality pairs) equivalence checks, which weren't validated against hand labels. Errors there are in the conservative direction (more hits, fewer survivors). Sonnet 5 can't be sampled deterministically through OpenRouter.
- **No hand check for leakage.** No hand-labelling pass checked survivors for leakage (a plan follow-up), and no NLA (out of scope).

## Next

The most informative next step, about $5 on GPU: separate reading from re-answering on the 64 items.

1. Run the Oracle Lens on activations from a *paraphrase that drops the work's title* but keeps the question frame. It should then name the bridge only by reading something other than the question.
2. Run WorkspaceBench's own prompt-only floor (stock Qwen asked what a model would be thinking there) on the same items.

If the Oracle Lens still beats Qwen-prompt-only on items where re-deriving is blocked, graduate to a study, and send the item set to Neel and the WorkspaceBench authors. In parallel, the J++ Lens (plan follow-up) is cheap to add to a rerun.

## Random raw samples

Drawn with `p03/raw_samples.py` (seed 0: 5 random A survivors, 3 B, 3 C, and one random reader cell each). The full draw, every row, is in [`results/raw_samples.md`](results/raw_samples.md). Two of the A rows, as drawn:

- **A-3827**, *Khel* (1992):
  - Qwen's two-hop answers: "Indian" ×20.
  - Hop 1: "Rajkumar Santoshi" ×15, Mahesh Bhatt ×2, David Dhawan ×2, Ram Gopal Varma ×1. The true director is Rakesh Roshan.
  - Every text predictor's first guess: "Rakesh Roshan". Text monitor: "India / Rakesh Roshan / Indian".
  - Oracle Lens random cell (L44, pos −2): "India… Russian… German".
  - This item fails the full rule (both directors are Indian), so it is in the looser set only.
- **A-4115**, *Spain in Our Hearts* (1938):
  - Qwen: "American" ×20; hop 1: "Lillian Hellman" ×14, Lincoln Steffens ×4. The true author is Pablo Neruda.
  - All predictors: "Pablo Neruda, Ernest Hemingway, George Orwell". Text monitor: "Chile / Pablo Neruda".
  - Oracle Lens random cell (L28, pos −1): off-topic ("A country… truth… Germany… The Bible").

## Spend and wall-clock

Source: `logs/costs.jsonl` (exp P03) and `logs/pods_P03.jsonl`.

| Phase | UTC | Wall-clock | Spend |
|---|---|---|---|
| Build (4 parallel agents) and B/C item generation | 17:20–18:13 | 53 min | $0.08 API |
| Selection predictors (9,675 calls) | 17:59–19:16, overlapping the pods | 77 min | $4.51 API |
| Pod 1: sample (H100) | 18:13–18:36 | 0.40 h | $1.61 GPU |
| Held-out predictors, LLM equivalence checks, aliases | 18:36–20:00 | about 50 min of calls | $3.28 API |
| Pod 2: more A items (follow-up 1) | 18:56–19:25 | 0.48 h | $1.92 GPU |
| Follow-up 2 (reasoning predictors), including the Sonnet attempts | 19:27–19:34 | 7 min | $0.48 API |
| Text monitor (two runs; the first was cut off by its token budget) | 19:29–19:37 | 8 min | $0.75 API |
| Pod 3: capture and readers (H100) | 19:26–20:48 | 1.35 h (on pod: 1.25 h; Oracle Lens 1.06 h) | $5.37 GPU |
| Judge validation, scoring, note | 20:48–21:30 | 42 min | $0.10 API |
| **Total** | 17:20–21:30 | about 4.2 h | **$18.11** (API $9.20, GPU $8.91) |

Pod idle time:
- Pod 1: vLLM loaded in 502 s (download included) and sampled for 612 s.
- Pod 3: download 486 s, then 1.25 h of work with no gaps between phases (`results/read/timing.json`).

The Oracle Lens ran at about 10 s per batch of 32, against 19.9 s for the timing batch.
