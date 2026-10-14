# P42: On a realistic deployment pool (LMSYS) against OLMo-3-7B's DPO prompts, no cheap detector finds planted coverage gaps at usable precision once length is controlled, and the prompts they flag don't draw more misaligned answers. Activation Mahalanobis beats text-embedding kNN modestly on AUROC; gradient similarity is at chance

**Status:** done, 2026-10-14. **Spend: $10.10** of $35:
- GPU $8.77: one H100 pod, 2.20 h, booked at the runner's $3.99/h; the API rate was $3.49/h.
- API $1.34:
  - text-embedding-3-small $0.40;
  - luna judging $0.94 (M3 alignment and coherence $0.63, the exploratory 4-way label $0.30, the M2 cluster check $0.01).

**Links:**
- Plan: [plan.md](plan.md), approved by Jacob in chat on 2026-10-14. OLMo-3-7B became the primary model at his request before approval.
- Run log: [log.md](log.md).
- Literature: [lit-review.md](lit-review.md), and sources in `papers/ood_detection/INDEX.md`.
- Raw samples, every drawn row: [results/analysis/samples.md](results/analysis/samples.md).
- Figures: [length vs score](results/analysis/figs/length_vs_score.png), [M1 precision](results/analysis/figs/m1_precision.png), [M3 rates](results/analysis/figs/m3_rates.png).
- Numbers: `results/analysis/{prep,selection,m1,m2,m3,m3_trunc,summary}.json`.

## Setup in one paragraph

- **Model:** `allenai/Olmo-3-7B-Instruct-DPO` @ `b33130b7`, with `Olmo-3-7B-Instruct-SFT` @ `e1452fc5` as the reference for the differential scorers.
- **In-distribution (ID):** 50,000 Dolci Instruct DPO first user turns, plus 5,000 held out and 20,000 Dolci SFT prompts.
- **Deployment pool:** 50,000 LMSYS-Chat-1M first turns, deduplicated against all 2.41M Dolci SFT and DPO prompts (1,555 exact and 9,805 near-duplicates removed from 596k unique). The pool is 79% English, median 25 tokens (ID median 118), and 3.2% moderation-flagged.
- **Planted known-OOD:** 500 consumer medical questions (ChatDoctor, length-matched, KS p = 0.34) and 500 legal questions (Law-StackExchange; length matching failed below about 26 tokens, KS p ≈ 1e-80).
- **On-policy responses:** greedy, at most 512 tokens, for 76k prompts.
- **Scorers** (higher = more OOD):

  | Scorer | What it is | Column chosen on the dev half |
  |---|---|---|
  | S0 | Length | prompt tokens |
  | S1 | text-embedding-3-small kNN to ID | — |
  | S2 | Likelihood | DPO prompt NLL |
  | S3 | Activations | Mahalanobis, DPO layer 24 |
  | S4 | Gradient similarity | LESS-style projected per-example gradients of the response loss, last 4 MLP blocks |

- **Length control and evaluation:** every score is z-scored within pool length deciles. Variants were chosen on a random dev half, and M1 is reported on the test half (25k pool, 250 per planted category).
- **Intervals:** 95% bootstrap (2,000 draws).

## Answer

1. **Length dominates raw scores, and within-decile z-scoring removes it.** Spearman ρ with prompt length on the pool, raw:

   | Score | Raw ρ |
   |---|---|
   | Mahalanobis, layer 8 | −0.91 |
   | Mahalanobis, layer 24 | −0.78 |
   | Summed NLL | +0.95 |
   | Zhang ratio (sum) | +0.87 |
   | SAE FVU | −0.69 |
   | Embedding kNN | −0.35 |

   After control every |ρ| ≤ 0.08 (`prep.json`). Stefan's length problem is real on this split, and simple stratification fixes the correlation. Short LMSYS prompts are what raw activation scores flag.

2. **Planted medical/legal questions are not "solved" on a realistic pool (M1).** These are length-controlled test-half numbers, AUROC of planted vs LMSYS:

   | Scorer | Medical AUROC | Legal AUROC | Precision at 1% (medical; chance = 0.01) |
   |---|---|---|---|
   | S0 length | 0.50 [0.46, 0.54] | 0.61 [0.58, 0.64] | 0.011 |
   | S1 embedding kNN | 0.79 [0.76, 0.81] | 0.75 [0.72, 0.77] | 0.024 [0.008, 0.044] |
   | S2 DPO NLL | 0.67 [0.64, 0.70] | 0.52 [0.49, 0.55] | 0.020 |
   | **S3 Mahalanobis L24** | **0.84 [0.82, 0.86]** | **0.83 [0.81, 0.84]** | **0.048 [0.024, 0.080]** |
   | S4 gradient kNN | 0.48 [0.45, 0.51] | 0.51 [0.48, 0.55] | 0.000 |

   - **At the plan's 0.1% prevalence** (25 planted in 25k), precision@25 is 0.00 for every scorer except S3 (0.004 [0, 0.04]).
   - **Key contrast, S3 minus S1** (S3 frozen on dev):
     - AUROC: +0.057 [+0.030, +0.083] medical and +0.077 [+0.050, +0.105] legal.
     - Precision at 1%: +0.024 [−0.004, +0.056] medical and +0.016 [−0.012, +0.044] legal.
   - **Reading:** model activations rank planted topics above text embeddings, but nothing approaches Stefan's AUROC ~0.99 from clean mixes. In a real deployment pool the top 0.1–1% is filled by other content that is even further from Dolci; M2 shows what.
   - **S2 separates the pool from held-out ID best** (AUROC 0.90, against S1 0.80 and S3 0.72), but not the planted sets. Prompt NLL tracks "LMSYS-style versus Dolci-style", not topic.

3. **The plan's novel-gap measure finds almost nothing beyond length and text (M2).** The rule was: gap clusters in a scorer's top 200 that S0 and S1 miss. The counts are S2 0, S3 0, S4 1 ("generating lists of skills"), against a bar of 3.
   - **The rule's judge arm is biased.** The D5 check compares a cluster with the ID prompts *nearest* to it, so it answers "B contains the theme" for nearly every coherent cluster. That includes clusters whose pool density is 0.9 or more: sexual content, bomb-making, hate, greetings.
   - **The judge-free density check (exploratory) disagrees.** These are the shares of each top 200 that fall in regions at least 80% denser in LMSYS than in Dolci:

     | Scorer | Share of top 200 in pool-dense regions |
     |---|---|
     | S2 | 60% |
     | S3 | 50% |
     | S4 | 47% |
     | S1 | 18% |
     | S0 | 14% |

   - **What each scorer's dense regions are** (luna's cluster themes; `m2.json`):
     - S2: harmful requests (sexual, hateful and violent clusters; two of them novel by density alone).
     - S3: computer hardware, programming and short fragments.
     - S4: lists, multilingual greetings and weather questions.
   - **Reading:** the model-based scorers point at *mass* that post-training under-weights. That is mostly harmful or casual chat traffic, which Dolci covers by type but thinly by density. They don't point at missing topics. This reading is exploratory, and its density metric lives in S1's embedding space.

4. **No behavioural payoff (M3).** For each scorer, the pool's top 1% (500) was compared with 500 random prompts matched on length decile, English or not, and moderation bin. Misbehaviour is E1's definition (alignment < 30 with coherence > 50, luna):

   | Scorer | Flagged | Matched | Difference |
   |---|---|---|---|
   | S0 | 2.2% | 1.3% | +0.9 pts [−0.9, +2.6] |
   | S1 | 0.2% | 1.0% | −0.8 [−1.8, 0.0] |
   | S2 | 1.0% | 0.7% | +0.3 [−0.8, +1.6] |
   | S3 | 1.6% | 1.5% | +0.1 [−1.4, +1.6] |
   | S4 | 0.8% | 0.6% | +0.2 [−0.9, +1.2] |

   - Restricting to moderation-unflagged prompts gives the same picture.
   - **Incoherence first looked like a payoff,** flagged minus matched:

     | Scorer | Difference |
     |---|---|
     | S0 | +8 [+3, +14] |
     | S1 | +25 [+19, +31] |
     | S2 | +11 [+6, +17] |
     | S3 | +8 [+3, +13] |
     | S4 | −13 [−18, −7] |

   - **Mostly it was a truncation artefact** (follow-up F1). 45% of pool answers hit the 512-token cap. luna scores 60% of cut-off answers as incoherent, against 16% of finished ones.
   - **On pairs where both answers finished:**

     | Scorer | Incoherence difference | Pairs |
     |---|---|---|
     | S3 | −5 [−13, +2] | 134 |
     | S2 | +7 [0, +14] | 159 |
     | S0 | +6 [0, +12] | 164 |
     | S1 | +21 [+6, +34] | 67 |

   - Only S1's flags (embedding outliers: gibberish, odd multilingual fragments; samples in `samples.md`) still draw clearly less coherent answers.
   - **Judge scope:** luna was validated on emergent-misalignment answers in E1, not on LMSYS requests (log.md, judge-scope rule). This null is not a validated null for chat misbehaviour, but no effect large enough to motivate a hand-label check appeared.

**Plan verdict (labelled line):** **O3 "nothing works after length control"** by the plan's letter: every scorer's length-controlled precision at 0.1% is below 0.2. Payoff: none. S3 does beat S1 on AUROC with an interval excluding 0, which is the part of O1 that holds; the decision rule's precision criterion fails. **What it changes:** don't pitch OOD-coverage detection as the sprint direction on this evidence. Report to Stefan:
- length stratification fixes the correlation;
- on LMSYS, planted far-OOD topics are not separable at useful precision;
- activations beat embeddings by about 0.06–0.08 AUROC;
- gradient similarity on on-policy responses carries no OOD signal;
- flagged prompts don't draw more misaligned answers.

## Predictions vs outcomes

| Prediction (plan) | p | Outcome |
|---|---|---|
| Raw S2a and S3 \|ρ\| ≥ 0.4 with length | 0.80 | **Yes**: NLL mean −0.59, Mahalanobis L24 −0.78 |
| Some S2–S4 planted AUROC ≥ 0.95 | 0.75 | **No**: best 0.84 |
| S1 planted AUROC ≥ 0.95 | 0.70 | **No**: 0.79 / 0.75 |
| O1: model-based beats S1 on precision at 0.1%, or ≥ 3 novel gaps | 0.35 | **No** (AUROC yes; precision and gaps no) |
| O2: text is enough | 0.50 | **No**: S3 beats S1 on AUROC |
| O3: every precision at 0.1% < 0.2 | 0.15 | **Yes** |
| Payoff misbehaviour ≥ 1.5×, CI excluding 1 | 0.30 | **No** |
| Payoff incoherence ≥ 1.5×, CI excluding 1 | 0.45 | Literally yes for S1 (1.53 [1.39, 1.70]); after removing truncation only S1 holds, n = 67 |
| S4 beats S3 on M1 | 0.15 | **No**: S4 at chance |
| Delta scorers beat their single-model versions on M2 | 0.40 | **No** (not selected on dev: dknn/dmaha dev AUROC 0.50–0.65 vs 0.84) |
| Top 1% of S2–S4 moderation-flagged at ≥ 2× the pool rate (3.2%) | 0.55 | **No**: S2 5.4% (1.7×), S3 0.8%, S4 0.2% |

## Follow-ups (exploratory)

- **F1, truncation re-analysis** (free; [p42/m3_trunc.py](p42/m3_trunc.py), `m3_trunc.json`). Prediction: "most of S1's +0.25 incoherence gap is truncation; S0/S2/S3 shrink to within ±0.05". Outcome:
  - S1 kept +0.21 (wrong);
  - S3 went to −0.05 (right);
  - S0 and S2 sit at +0.06–0.07 (borderline).
  - Truncation rates differ by scorer: S3 +14 pts, S1 +10 and S4 −20, flagged minus matched.
- **Not run, and the obvious next steps if this line is revisited:**
  - regenerate the M3 sets with up to 2,048 tokens (about $3), so incoherence is judged on finished answers;
  - Delta-SAE FVU with a DPO-data SAE (about $5);
  - a judge-free gap metric not tied to S1's space, e.g. the density in DPO-activation PCA space, using the committed PCA projections (free);
  - planted categories whose length range matches LMSYS (legal failed matching).

## Deviations from the plan

| Deviation | Why |
|---|---|
| Chat rendering used E1's template with its default system prompt (plan: "no system prompt") | That is how the template renders with no system message, and how E1 ran the model; logged before the run |
| The "best model-based family" tie-break changed from family order to dev AUROC | Every family had dev precision 0 at 0.1%; family order would have picked the weakest (S2). Dev half only; test untouched |
| gpu_hours 3.5 → 4.5 | First data build was 2× the token estimate; the actual run used 2.2 h |
| Legal planted set not length-matched (KS fail) | Law-StackExchange has almost no prompts under about 26 tokens; within-decile scoring still controls length |
| S4 gradient sums over all positions of the response loss | That is the exact per-example weight gradient; response-only positions would not be |
| fvu_sae_L16 NaN for 53 short prompts | Zero-variance single-token prompts; excluded by the selection's coverage rule |

## Wall-clock and spend by phase (UTC, 2026-10-14)

| Phase | Time | Spend |
|---|---|---|
| Planning, lit capture, HF checks | 00:12–00:35 | $0 |
| Build (3 parallel units), tests, pre-run review r1 and fixes | 00:35–01:14 | $0 |
| Wait for LMSYS click-through; data rebuild (~6 min); embeddings (2 min) | to 01:07 | $0.40 |
| Pod: launch and gates 6 min; download 3.7 min; smoke 4.8 min; gen 50 min; feats 47 min; grads 18 min; score 1.5 min | 01:14–03:26 | $8.77 |
| Analysis, judging, F1 | 03:27–03:40 | $0.94 |

Pod idle time: about 6 min of launch and gates, plus about 1 min of model loads between steps.

## Review

See the review section appended after the cross-review round.
