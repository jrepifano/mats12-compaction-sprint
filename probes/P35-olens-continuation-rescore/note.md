# P35: In the Oracle Lens readouts we already have, how often does a readout say what the model is about to write, and how often does it name something the upcoming text doesn't contain?

**Status:** done, 2026-10-13. Spend **$15.43** of a $25 cap, all API (started at $5; raised twice, reasons in the log). No GPU. Plan: [plan.md](plan.md); run log: [log.md](log.md); literature: [lit-review.md](lit-review.md); raw samples: [results/raw_samples.md](results/raw_samples.md). Review: one gpt-6-astra round, `logs/reviews/20261013-P35-r1.md` (REVISE; all findings fixed, see the end of this note).

## Answer

**The Oracle Lens's readouts are mostly continuation-shaped and position-specific: they read as the next words at the read point. Only part of their content matches what the model goes on to write: about a quarter to a third of bullets match the realised reply, and about a third are found in five of Qwen's own samples (one judge). Where a bullet names a hidden variable, it does so inside an answer-then-explanation continuation, on items where asking Qwen or a prompt-only monitor also recovers that variable. The reopen criterion for a paid stage study is not established by these data.**

1. **In chat replies, at delimiter tokens (the I1a hallucination set: 149 chats, 1,123 sites × 5 layers, about 22,500 true-site lens bullets per judge), most bullets are shaped as the reply's next words.**
   - **Form.** 0.61 [0.57, 0.64] (GLM) or 0.70 [0.67, 0.72] (Gemini) of bullets read as the next words at the read point. Bullets of the same layer from another site of the same chat, shuffled in blind, read so 0.35 or 0.13 of the time. True − control: **+0.26 [+0.24, +0.28]** (GLM), **+0.56 [+0.54, +0.59]** (Gemini). The lens is position-specific: it writes a continuation of *this* point.
   - **Content.** Fewer say what the reply actually goes on to say within 50 words: **0.24–0.34** (Gemini 0.24 [0.22, 0.27], GLM 0.34 [0.32, 0.37]), against 0.06 and 0.13 for control-site bullets. True − control: **+0.18 [+0.15, +0.21]** and **+0.21 [+0.18, +0.24]**.
     - *Sensitivity, exploratory:* counting a bullet when either judge says NEXT gives 0.39 [0.36, 0.42], and +0.25 over control.
     - A hand read of 15 random disagreements leaned NEXT in most of them. But a disagreement-only draw can't show net bias: it misses errors the judges share. So the per-judge range is the result.
   - **Depth.** The share rises with depth: L60 − L20 = **+0.17 [+0.14, +0.20]** (GLM), +0.16 [+0.13, +0.18] (Gemini), and it plateaus from L44–52.
   - **The rest.** 0.23 (GLM) or 0.45 (Gemini) of bullets are shaped as the next words but carry content the reply doesn't contain (new or contradicted). In all, 0.21–0.23 of bullets contradict the conversation or are off-topic, against 0.01 for the text monitor.
2. **Exploratory: some of those continuations are what Qwen itself writes.** Five raw samples of Qwen3.6-27B from the exact prefix at each site, judged per bullet at layers 20/44/60:
   - **GLM:** 0.34 [0.32, 0.37] of true-site bullets have content in at least one sample, against 0.13 for control-site bullets, so **+0.21 [+0.19, +0.24]** over control. That is 0.22 of the continuation-shaped bullets the reply doesn't contain, and 0.67 of those it does.
   - **Positive references:** the realised continuation's first 24 words are found in the samples 0.77 (GLM) or 0.51 (Gemini) of the time, and a held-out sample's first 24 words in the other four 0.77 or 0.53. These fragments differ from lens bullets in length and span, so they show the check can say YES; they are not a matched ceiling.
   - **Gemini** finds 0.046 [0.039, 0.053] (control 0.010). On this check κ between the judges is 0.17. In a hand read of 15 random disagreements GLM's YES looked right in nearly all, so I lean on GLM here. Again, a disagreement-only draw is weak evidence.
3. **We find no excess of non-continuation-shaped novel content at the true site. That doesn't settle Gurnee et al.'s "running commentary".**
   - Bullets not shaped as the next words that say something new: 0.13 [0.11, 0.14] (GLM), 0.15 [0.14, 0.17] (Gemini). The text monitor is at 0.16 and 0.18 (lens − monitor −0.03 under both judges).
   - **Site kinds:** the share differs little by kind (GLM: clause 0.11, newline 0.17; Gemini: clause 0.12, sentence 0.20).
   - **What this can't show:**
     - *The control:* control-site bullets score higher (0.21, 0.42). That is expected mechanically: a continuation of another point, judged here, becomes "not shaped as the next words, new". So the control shows position specificity, not the absence of commentary.
     - *The label:* the measure is formal ("not continuation-shaped, new content"), not a semantic test of commentary.
     - *Positions:* this set has no word positions to compare against ("Coverage").
     
     So whether delimiters carry more commentary than other positions stays open.
4. **A prompt-only text monitor's bullets match the upcoming content more often than the lens's do.** I1a's Gemini 3.8 Flash monitor saw the chat only up to the read token.
   - **Rates:** its bullets locate in the next 50 words 0.53 (GLM) or 0.55 (Gemini) of the time. Lens − monitor: **−0.18 [−0.21, −0.15]** (GLM), −0.31 [−0.34, −0.28] (Gemini). GLM is a different family from the monitor, so this isn't self-preference.
   - **Caveat:** the styles differ. The monitor writes topic phrases, which match more loosely than the lens's sentence-like continuations. This comparison is about this matching metric, not proof that the lens carries nothing beyond text.
5. **On the wrong-bridge items (P03/P11, 63 items, causally relevant latent = Qwen's wrong bridge), the lens's latent hits are continuations of the answer.**
   - **Qwen's answer.** At `":"` (the delimiter just before Qwen's one-word answer) 0.93 of P11 bullets and 0.60 of P03 bullets name Qwen's answer (L40–60: 0.94). At `" answer"` the shares are 0.19 and 0.11. Delimiter − word, paired over items: **+0.56 [+0.54, +0.59]** (P11), +0.38 [+0.36, +0.40] (P03).
   - **The bridge** is in ≤ 0.03 of bullets at any position (≤ 0.07 at any single layer), and **never in the answer slot at `":"`** (0/44 P03, 0/92 P11). It comes as an explanation after a nationality ("American (Oliver Stone …)").
   - **Never alone:** in 1 of 2,079 P03 cells and 0 of 2,268 P11 cells is the bridge named without Qwen's answer. Asking Qwen hop 1 names the bridge on 62/63 items (P11).
6. **On WorkspaceBench multihop (100 items, last prompt word), the lens names the surface answer more often than the hidden intermediate.**
   - **Lens, layers ≥ 36:** 0.69 of bullets name the answer and 0.56 the intermediate (+0.13 [+0.09, +0.17] over items).
   - **Item coverage:** the lens names the intermediate on 100/100 items. It never names it on an item where no bullet names the answer.
   - **Text monitor:** names the intermediate in 0.48 of bullets and on 98/100 items. Monitor − lens on intermediate bullets: −0.07 [−0.14, −0.01].

**What it changes:** the planned outcome is between "Mostly continuation" and "Latent beyond continuation"; the reopen criterion is not established.
- **Treat the Oracle Lens's output as position-specific continuations in our method comparisons.** On these sets, its hidden-variable hits come on items where text prediction or asking Qwen also recovers the variable. That is not the same as showing it carries nothing else.
- **Keep the paid stage-by-stage study (I) closed for now.** No latent here is tracked where both the matched-continuation and asking-Qwen baselines fail. The authors are running the stage ablation themselves (lit-review).
- **Open threads:**
  - the 0.23–0.45 of bullets that are continuation-shaped but differ from the reply (about a fifth of them are in five Qwen samples, by GLM);
  - whether delimiters carry more commentary than mid-word positions.
  
  See "Next".

## Setup (as run)

- **Model and lens:** Qwen3.6-27B @ `6a9e13b`, Oracle Lens `agu18dec/olens_and_ar@c95215d:olens_s3d_rl600`. Every readout already existed (I1a, P03, P11); P35 ran no GPU.
- **(H)** I1a hallucination family (`results/I1a/readouts/olens_rl600/hallucination.jsonl`, sha256 `4b848bf2…`; chats and site offsets from WorkspaceBench `evals/hallucination/items.json`, `cf84fee4…`; text monitor `c88229f0…`).
  - **Blind control:** each judge call shows the cell's bullets mixed with those of a control site (another site of the same chat and layer, at least 2 sites away), shuffled with a per-cell seed. The judge isn't told there are two sources.
  - **Labels per bullet:**
    - *Form:* CONT (reads as the next words at the marked point) or OTHER.
    - *Content:* NEXT (said in the next 50 words), LATER, BEFORE, NEW (said nowhere, not contradicted), WRONG (contradicted or unrelated) or GENERIC.
  - **Derived:**
    - `realised` = NEXT;
    - `alt_continuation` = CONT with NEW or WRONG;
    - `commentary` = OTHER with NEW.
  - **Intervals:** conversation-cluster bootstrap, 10,000 resamples, seed 0. Code: `p35/hjudge.py`, `p35/hanalyze.py`, output `results/h_summary.json`.
- **Follow-up (a):** 5 samples per site from `qwen/qwen3.6-27b` on OpenRouter.
  - **Prompt:** the exact token prefix the lens read, decoded with the pinned tokenizer (matches `items.json` at all 1,123 sites), sent to `/completions`. T = 1.0, top-p 1.0, 48 tokens, seeds 7000–7004.
  - **Providers:** Chutes and Phala only (others mishandled raw prompts; log).
  - **Judge prompt:** per bullet YES/NO/GENERIC "content in at least one sample", same blind mix, layers 20/44/60.
  - **Calibration (a2):** 300 random sites, the realised next 24 words or a held-out sample as the fragment.
  - Code: `p35/qsample.py`, `p35/qjudge.py`; output `results/q_summary.json`.
- **(B) and (M):** deterministic regex per bullet (`p35/regex_scores.py`; P11's alias matcher for people; a demonym/country map for answers; case-sensitive whole tokens for short multihop targets). Outputs `results/regex_b.json`, `results/regex_m.json`. Item-paired bootstraps, seed 0.
- **Judges:** GLM 5.3 Flash and Gemini 3.8 Flash, reasoning low, temperature 0, on every cell. DeepSeek V4 Flash (the plan's judge) was dropped at the smoke (below).
- **Tests:** 55 in `tests/` (`test_hjudge.py`, `test_regex_scores.py`).

**Deviations from the plan:**

| Change | When | Why | Effect |
|---|---|---|---|
| Two labels per bullet (form, content) instead of one six-way label | After the 20-cell smoke, before bulk data | Judges agreed on 63/160 bullets: continuation-shaped bullets with a different value had no clean label | Adds the form dimension; "commentary" becomes OTHER + NEW |
| Judges: GLM 5.3 Flash + Gemini 3.8 Flash on every cell, not DeepSeek + a 300-cell Gemini check | Smoke v2 | DeepSeek disagreed with both others (content 71–79/160); DeepSeek with reasoning ran past 4,000 tokens on 19/20 | Two-family panel on all cells; cap $5 → $15 |
| Follow-up (a) Qwen samples, and calibration (a2) | After layers 20–36 of (H) | The largest class was continuation-shaped bullets that differ from the one realised reply | Section 2 of the answer; cap $15 → $25 |
| Retries with a larger token budget for replies cut off by reasoning | During the bulk run | Gemini cut off 5% at 500 tokens, GLM 12% at 2,000 | Unparsed (H) cells: Gemini 3, GLM 81 of 6,738 |

## Predictions vs outcomes

| Prediction (plan) | p | Outcome |
|---|---|---|
| (H) NEXT + LATER ≥ 0.40 of lens bullets | 0.6 | **No:** 0.36 (GLM), 0.25 (Gemini). |
| (H) COMMENTARY 0.10–0.35 | 0.55 | **Yes** under the v2 formal definition (OTHER + NEW): 0.13, 0.15. Semantic commentary not tested. |
| True − control NEXT ≥ +0.15 | 0.7 | **Yes:** +0.21, +0.18. |
| NEXT rises L20 → L60 by ≥ 0.10 | 0.45 | **Yes:** +0.17, +0.16. |
| Text monitor NEXT within 0.10 of the lens | 0.4 | **No:** the monitor is higher by 0.18 (GLM) to 0.31 (Gemini). |
| (B) Qwen's answer ≥ 0.15 more common at `":"` than at `" answer"` | 0.6 | **Yes:** +0.49 (P03), +0.74 (P11). |
| (B) ≥ 70% of bridge-naming bullets put the bridge in answer position | 0.65 | **No:** 4/72 (P03), 3/154 (P11) strict; 0 at `":"`. The bridge comes as an explanation after the answer. |
| (M) Lens names the answer more than the intermediate at layers ≥ 36 | 0.65 | **Yes:** 0.69 vs 0.56, +0.13 [+0.09, +0.17]. |
| (M) Text monitor names the intermediate at least as often as the lens | 0.6 | **No:** per bullet 0.48 vs 0.56; items 98/100 vs 100/100. |
| Reopen criterion met | 0.05 | **No.** |
| Follow-up (a): ≥ 0.40 of `alt_continuation` bullets in a Qwen sample | 0.5 | **No:** 0.22 (GLM), 0.01 (Gemini). |
| Follow-up (a): true − control "in a sample" ≥ +0.15 | 0.7 | **Yes** under GLM (+0.21), no under Gemini (+0.035). |
| Calibration (a2): realised continuation in a sample ≥ 0.30; held-out sample ≥ 0.40 | 0.6, 0.5 | **Yes:** 0.77/0.77 (GLM), 0.51/0.53 (Gemini). |

## Results

### (H) Chat replies, delimiter sites: label shares of lens bullets

Source: `results/h_summary.json`. 95% intervals resample conversations. Control = the cell's paired control-site bullets.

| | GLM lens | GLM control | GLM monitor | Gemini lens | Gemini control | Gemini monitor |
|---|---|---|---|---|---|---|
| Shaped as the next words (CONT) | 0.61 | 0.35 | 0.44 | 0.70 | 0.13 | 0.17 |
| Content in the next 50 words (`realised`) | **0.34** | 0.13 | 0.53 | **0.24** | 0.06 | 0.55 |
| Content later in the reply | 0.02 | 0.12 | 0.07 | 0.01 | 0.08 | 0.07 |
| Content earlier (prompt or before) | 0.11 | 0.19 | 0.15 | 0.06 | 0.16 | 0.13 |
| New, not contradicted | 0.30 | 0.31 | 0.19 | 0.46 | 0.49 | 0.20 |
| Contradicted or unrelated | 0.21 | 0.22 | 0.01 | 0.23 | 0.21 | 0.01 |
| `alt_continuation` (CONT + new/contradicted) | 0.23 | 0.13 | 0.03 | 0.45 | 0.09 | 0.02 |
| `commentary` (OTHER + new) | 0.13 | 0.21 | 0.16 | 0.15 | 0.42 | 0.18 |
| Bullets judged | 22,433 | 22,396 | 3,585 | 22,746 | 22,713 | 3,588 |

By layer (`realised` / `alt_continuation` / `commentary`):

| Layer | GLM | Gemini |
|---|---|---|
| 20 | 0.22 / 0.21 / 0.12 | 0.12 / 0.39 / 0.20 |
| 36 | 0.33 / 0.23 / 0.13 | 0.24 / 0.43 / 0.16 |
| 44 | 0.37 / 0.23 / 0.13 | 0.27 / 0.44 / 0.13 |
| 52 | 0.41 / 0.24 / 0.11 | 0.31 / 0.47 / 0.13 |
| 60 | 0.39 / 0.24 / 0.14 | 0.28 / 0.50 / 0.14 |

By site kind (GLM `realised` / `commentary`): clause 0.39 / 0.11 (8,253 bullets), markup 0.35 / 0.14, newline 0.28 / 0.17, quote 0.38 / 0.10, sentence 0.30 / 0.12. Gemini shows the same ordering of `realised` (clause highest, newline lowest) and the highest `commentary` at sentence ends (0.20).

**A judge-free check agrees in direction.** Mostly-Latin bullets share 0.21 of their content words with the next 50 words of the reply, against 0.16 with the previous 50 words. Control-site bullets score 0.13 against both windows (`h_summary.json`, `lexical`).

### Follow-up (a), exploratory: is a bullet's content among Qwen's own samples?

Source: `results/q_summary.json`; layers 20/44/60, non-GENERIC bullets.

| | GLM | Gemini |
|---|---|---|
| True-site bullets in at least one of 5 samples | **0.34 [0.32, 0.37]** | 0.046 [0.039, 0.053] |
| Control-site bullets | 0.13 | 0.010 |
| True − control | **+0.21 [+0.19, +0.24]** | +0.035 [+0.029, +0.043] |
| By layer, 20 / 44 / 60 | 0.22 / 0.39 / 0.41 | 0.01 / 0.07 / 0.06 |
| … among bullets that (H) labelled `realised` | 0.67 | 0.17 |
| … among `alt_continuation` | 0.22 | 0.01 |
| … among `commentary` | 0.18 | 0.01 |
| Calibration: realised next 24 words in a sample | 0.77 (n = 290) | 0.51 (n = 289) |
| Calibration: held-out sample in the other 4 | 0.77 (n = 295) | 0.53 (n = 286) |

### (B) Wrong-bridge items: bullets by read position (layers pooled)

Source: `results/regex_b.json`. P03: layers 20–60, one draw; P11: layers 40–60, two draws.

| Source, position | Bullets | Names Qwen's answer | Names Qwen's bridge | Bridge in the answer slot (strict) | Names the true answer |
|---|---|---|---|---|---|
| P03 `" answer"` | 2,815 | 0.111 | 0.001 | 0.001 | 0.025 |
| P03 `" is"` | 2,797 | 0.334 | 0.009 | 0.001 | 0.045 |
| P03 `":"` | 2,898 | 0.600 | 0.015 | 0 | 0.099 |
| P11 `" answer"` | 3,044 | 0.188 | 0.001 | 0 | 0.043 |
| P11 `" is"` | 3,048 | 0.545 | 0.019 | 0.001 | 0.056 |
| P11 `":"` | 3,188 | 0.928 | 0.029 | 0 | 0.128 |

- **By layer** (P03, Qwen's answer at `" answer"` / `" is"` / `":"`): L20 0.00 / 0.02 / 0.01; L40 0.02 / 0.18 / 0.92; L52 0.08 / 0.89 / 0.97; L60 0.63 / 0.96 / 0.99. The answer appears first at the delimiter, then moves back to the earlier word tokens with depth.
- **Items where any bullet names the bridge:** 23/63 (P03) and 34/63 (P11), matching P11's published counts.
- **True answer in Qwen's own samples:** on 11 items Qwen sampled the true country as a minority answer. Dropping those items changes nothing (P11 `":"`: 0.927).

### (M) Multihop: bullets naming the surface answer vs the intermediate

Source: `results/regex_m.json`.

| Reader, layer | Bullets | Answer | Intermediate | Intermediate only | Items: answer / intermediate |
|---|---|---|---|---|---|
| Lens L20 | 402 | 0.22 | 0.20 | 0.12 | 0.53 / 0.42 |
| Lens L36 | 401 | 0.47 | 0.39 | 0.15 | 0.78 / 0.68 |
| Lens L44 | 401 | 0.61 | 0.51 | 0.15 | 0.89 / 0.80 |
| Lens L52 | 402 | 0.84 | 0.68 | 0.08 | 0.99 / 0.96 |
| Lens L60 | 408 | 0.87 | 0.68 | 0.08 | 1.00 / 0.94 |
| Text monitor (prompt only) | 314 | 0.39 | 0.48 | 0.43 | 1.00 / 0.98 |

Some multihop targets are common words ("up", "day", digits), so their hits include generic uses.

## Coverage of read positions

Source: `results/coverage.json` (`p35/coverage.py`; a token is classed by its text alone).

| Readout set | Delimiter sites | Word-start sites | Mid-word sites |
|---|---|---|---|
| (H) I1a hallucination | 1,123 | 0 | 0 |
| (B) P03 / P11 (each) | 63 (`":"`) | 126 (`" answer"`, `" is"`) | 0 |
| (M) I1a multihop | 9 | 91 | 0 |
| I1a basic_readout_mt (not scored) | 0 | 84 (+10 other) | 6 (non-English word pieces: "ellä", "je", "nök", "akan", "wę") |

**There are no mid-word read positions in any scored set.** The plan's delimiter-vs-mid-word comparison can't be made from these outputs, and none is inferred. The only within-item position contrast is delimiter vs word start, in (B). Gurnee et al.'s split ([lit-review](lit-review.md)) contrasts delimiters with "most positions", which in their examples are mid-phrase tokens. Testing it needs reads at mid-word sites, which would take new lens reads on a GPU; per the launching session's instruction, no sampling was added for it.

## Judges

- **(H) agreement, GLM vs Gemini on 44,813 lens bullets:**
  - form κ 0.41 (agreement 0.71);
  - content κ 0.54 (0.65);
  - `realised` κ 0.57 (0.86).
  
  On text-monitor bullets: content κ 0.69, `realised` κ 0.72. Neither judge is validated against human labels. GLM finds more `realised` and less `alt_continuation` than Gemini. I report both and lead with ranges.
- **Hand read (the agent, not Jacob):** 15 random (H) bullets where exactly one judge said NEXT (`p35/adjudicate.py`, `random.Random(1)`; `results/adjudication_draws.md`, H1–H15).
  - My first read leaned NEXT on about 14 of 15: GLM's call on H1, H2, H4, H7, H9, H11, H12, H14, H15, Gemini's on H3, H5, H6, H8, H13, with H10 mixed.
  - The reviewer flagged H14 (the SQL column `d.dn` becomes `d.department`, a changed value under the rubric) and H15 (users substituted for data) as arguable.
  - A disagreement-only draw also can't measure errors the two judges share. So this read doesn't show net undercounting, and the per-judge range (0.24–0.34) is the result; the either-judge 0.39 is a sensitivity figure.
- **Follow-up judge:** on "in a Qwen sample" the judges barely agree: κ 0.17 on 27,204 bullets. Of 3,934 true-site disagreements, the 15 drawn (`random.Random(0)`, Q1–Q15) were all GLM YES / Gemini NO, and GLM was right on about 14. Examples:
  - Q1: "Budgie, Pantheon, and Cinnamon" against samples listing exactly those desktops;
  - Q4: "I can certainly write romantic stories that explore emotional intimacy" against near-identical samples;
  - Q9: `Option<T>` against samples defining `enum Option<T>`.
  
  So I don't use Gemini's 0.046 as the estimate and lean on GLM's 0.34. That rests on one judge and an agent read of a disagreement-only draw, which can't see errors both judges share.

## Follow-ups (exploratory)

1. **(a) Qwen samples at the (H) sites** (logged 15:45 UTC, before sampling). Prediction: ≥ 0.40 of `alt_continuation` bullets in a sample; true − control ≥ +0.15. Outcome: 0.22 (GLM) and +0.21. Cost $0.62 sampling and $3.81 judging.
   - The first 68 calls were spread over six providers. Reka, DeepInfra and Venice returned empty text, and SiliconFlow answered as a new chat turn.
   - Sampling was restarted pinned to Chutes and Phala, and the bad rows were discarded (`results/qsamples.v0-unpinned.jsonl`).
2. **(a2) Calibration** (logged 16:22 UTC). Prediction: realised ≥ 0.30, held-out sample ≥ 0.40. Both held. Cost $0.49.

## Limitations

- **No mid-word positions** (Coverage). The (H) set is all delimiters, and (B)'s contrast is word start vs delimiter on one prompt template.
- **The judges are unvalidated against human labels** and disagree substantially on some measures. The headline facts don't depend on the judge:
  - bullets are continuation-shaped and position-specific;
  - realised content is well below the text monitor;
  - commentary is not elevated.
  
  The levels do depend on it. The hand reads are the agent's own, 15 items each.
- **Five samples and 48 tokens** bound follow-up (a): a lens bullet about text more than 48 tokens ahead counts as "not in a sample". The positive-reference fragments (the next 24 words, or a held-out sample) differ from lens bullets in length and span, so they are not a matched ceiling.
- **The text-monitor comparison mixes style with content.** The monitor writes topic phrases, the lens writes sentences.
- **The samples came through two OpenRouter providers' fp8 deployments,** not the bf16 model the lens read, and the provider re-tokenises the prefix.
- **(B) is one prompt template** ("My one-word answer is:"), so delimiter vs word is confounded with distance to the answer. **(M) targets** include common words.

## Next

- **Nothing here argues for reopening the paid stage study.** If the line continues, the cheapest informative step is a GPU read of the existing lens at mid-word positions in the (H) chats, with Qwen sampled more deeply (≥ 20 samples, 128 tokens). That would separate "lower-probability continuation" from confabulation for the 0.23–0.45 of continuation-shaped bullets the reply doesn't contain, and give the missing word positions.
- **For the methods comparison:** "realised continuation share against a prompt-only monitor, with a blind displaced-site control" is a cheap check on what a reader adds beyond text prediction at a site. Here the monitor matches upcoming content more often than the lens does, under both judges.

## Random raw samples

Drawn by `p35/raw_samples.py` (`random.Random(0)`): 6 (H) lens cells with every bullet, its source, both judges' labels, the realised text and two Qwen samples; 6 (B) bullets; 4 (M) bullets. The full draw is in [results/raw_samples.md](results/raw_samples.md). Four of the six (H) cells are at layers 36 or 52, which follow-up (a) didn't judge, so their sample column is empty.

One row as drawn, `lens|chat-dailydialog-0008|L52|p76`. The reply has "…were you watching it from the ocean, a mountain top," and continues " or your backyard?"; Qwen's samples give " or just your backyard?" and " or maybe just from your porch?". The true-site lens bullets give "a city park, or even from your window?…", "or just from your window? Every sunset feels special…" and "城市 balcony, or somewhere in between? 😊 Since I don't have eyes…". Both judges call all three continuation-shaped with new content. The control bullets restate the colours asked about earlier. This is the pattern of answer point 1: the right slot, plausible but different content.

## Spend and wall-clock

Source: `logs/costs.jsonl` (exp P35). All times UTC, 2026-10-13.

| Phase | UTC | Spend |
|---|---|---|
| Claim, data survey, plan (published 15:31), build (sub-agent for (B)/(M) in parallel) | 15:26–15:44 | $0 |
| Smokes and judge choice (v1, v2, DeepSeek, GLM) | 15:34–15:37 | $0.07 |
| (H) Gemini, every cell plus retries | 15:37–16:03 | $8.62 |
| (H) GLM, every cell plus retries | 15:37–16:53 | $1.81 |
| Follow-up (a): Qwen sampling (incl. the stopped unpinned run) | 15:42–16:08 | $0.62 |
| Follow-up (a): sample judging, both judges | 16:08–16:51 | $3.81 |
| Follow-up (a2): calibration | 16:22–16:54 | $0.49 |
| Analysis, hand reads, note | 16:54– | $0 |
| **Total** | | **$15.43** (API) |

Judging ran at 32 calls in flight on the shared key (16 per judge). For about 25 minutes, sampling added 8–16 more (log). GLM was the slow judge, at about 37 calls/min per 16 slots.

## Review

One gpt-6-astra round over the whole branch (`logs/reviews/20261013-P35-r1.md`, verdict REVISE). It recomputed the (H), follow-up, (B) and (M) summaries, including the bootstrap intervals, the union figure (0.386) and the spend ($15.43), and they reproduce. Every finding was fixed:

| Finding | Severity | Fix |
|---|---|---|
| Continuation *form* was presented as continuation *content* ("mostly the text that would come next"); "latent hits only where the monitor also succeeds" overclaimed | BLOCKING | Headline and "What it changes" now say continuation-shaped, with limited content recovery; the multihop item coverage (lens 100/100, monitor 98/100) is reported; the reopen criterion is "not established" |
| A displaced-site control turns continuations into OTHER + NEW mechanically, so it can't refute running commentary | BLOCKING | Point 3 renamed to "non-continuation-shaped novel content"; the control is used for position specificity only, and semantic commentary is stated as open |
| The either-judge NEXT figure was called the better estimate on a disagreement-only hand read | BLOCKING | The per-judge range leads; 0.39 is a sensitivity figure; the hand read's limits (and H14, H15) are stated |
| Calibration treated as a ceiling | MINOR | Now "positive references"; no ceiling normalisation |
| Multihop monitor prediction scored "Mixed" | MINOR | Scored "No" |
| A retry reply could overwrite a valid base reply | MINOR | `hjudge.merge_retry` replaces only a base reply that fails parsing, with one that parses; two tests. No saved number changed (every result file regenerated identically) |
| Unpinned tokenizer fallback in `qsample.py` | MINOR | Full revision pinned |
| Follow-up timestamp | NIT | 15:45 UTC |

No second round: the fixes changed no number.

