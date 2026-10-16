# Signs of life (draft, 16 Oct 2026)

A living summary for the sprint pitch: what the exploration-phase probes found, and which risk of each candidate project they remove. Every number comes from a merged probe note (`probes/P<NN>-*/note.md`) or the Correct-Don't-Delete paper. Figures are regenerated with `uv run python docs/pitch/make_figs.py`. The P49 and P50 sections fill in today as results land.

---

## Project 1: Correct, don't delete

**Question.** When training data carries a hidden trait, does *rewriting* a small fraction of the trait-carrying rows remove much more of it than deleting them, even with a locator that is only somewhat better than random?

**Why Neel would care.**
- He named data-point-level learning as a top interest at the 14 Oct drop-in: "being able to understand what is being learned at each step of training or ideally from each data point would be awesome."
- He also wants settings where "it is not obvious to an LLM judge reading the dataset what was going to be learned." Hidden-trait transfer is exactly that: a judge told the trait scores 0.52 AUROC.
- Engels & Nanda's LessWrong post ("Why Do Naive SFT Filters For Safety Properties Fail") found that swapping a subset's teacher rollouts removes inherited traits where dropping the same prompts does not. They leave open which datapoints carry the trait.

### Evidence so far

| Probe | Setting | Result | Risk it removes |
|---|---|---|---|
| Correct, Don't Delete (Jacob's MATS application) | Emergent misalignment, Qwen2.5-14B | Rewriting 25% of the bad-advice rows vs deleting them: **−7.3 pp EM** (p = 0.033); 50%: −12.3 pp; deleting alone −1.7 (unresolved). Replicated on Gemma-4-12B (−5.1) and a finance organism (−14.9) | The asymmetry isn't one model or one dataset |
| P43 | Phantom Transfer (Gemma-3-12B teacher → Llama-3.1-8B), 20,000 rows | Random 7,500 rows: rewrite − delete **+6.0 [+2.3, +10.5]**, rewrite − add clean rows **+3.9 [+1.7, +6.9]**. At 10% of rows, informed selection cuts the trait 41.4% → ~2%; random leaves 23–24% | It isn't just dilution; a weak locator at 10% of the data is enough |
| P48 | Subliminal learning (cat), Qwen2.5-7B, 10,000 rows | Rewriting a random 5%: trait 27.9% → **13.3%** (57% of the excess over the 2.1% clean control removed), vs 22.7% for deleting. Rewrite − delete under the likelihood-ratio pick **+6.7 [+1.2, +13.9]** | It works in the setting where text-level defences are at chance |
| P48, detection | Same, 18,000 held-out rows | Text defences at chance (LLM judge 0.52, embeddings 0.52, length 0.57). Model-based scorers 0.67. Gradient influence adds nothing over a cheap likelihood ratio (−0.006 [−0.017, +0.005]) | A cheap locator is as good as an expensive one; we don't need to know the trait to beat random |
| Engels & Nanda post | SFT on teacher rollouts | Swapping the teacher removes the trait; dropping the prompts does not | Independent replication of the direction |

![Rewrite minus delete across three settings](fig/rewrite_vs_delete.png)

![Trait rate by operation, P43 and P48](fig/arms_by_operation.png)

![Which scorer finds trait-carrying rows](fig/detection_auroc.png)

### Caveats to say out loud
- **Deleting also shrinks the dataset.** In P43 the delete arm has 12,500 rows, against 20,000 for rewrite; in P48 rewrite and delete differ slightly in share and size. P50 matches size and share.
- **P48's rewrites came from the student's own base model**, which shares its starting weights. P50 adds a different-family writer (gpt-6-luna).
- **Three seeds,** and transfer varies 26–29% between seeds of the same reference. P48's random-selection contrast (+9.4 [−7.2, +22.8]) doesn't exclude zero; the P43, P48 likelihood-ratio and CDD contrasts do.
- **Closest prior work to address:** Weckbecker et al. 2026, "Can Data Attribution Filter Out Subliminal Learning? Not Reliably", and the "forthcoming MATS work" the Engels & Nanda post mentions.

### Live: P50, rewrite vs delete vs add-clean by dose (results today)
Same cat organism as P48. Doses 2.5% and 5% at 10,000 rows, plus a 20,000-row block, 3 seeds. Arms: rewrite (same base), rewrite (different family), add clean rows, delete. The reproduction check passed: 26.10% vs P48's 26.08%.

| Dose | Rewrite, same base | Rewrite, different family | Add clean rows | Delete |
|---|---|---|---|---|
| 2.5% | _pending_ | _pending_ | _pending_ | _pending_ |
| 5% | 13.3 (P48) | _pending_ | _pending_ | 22.7 (P48) |

10K-row answer expected this morning; full note about 3:30 PM ET.

---

## Project 2: Compaction and safety behaviour

**Question.** When an agent's long history is compacted, does its safety-relevant behaviour change, and is that driven by the method, the number of compactions, or what the summary asserts or drops?

**Why Neel would care.** It is in his Central Doc as something he is "particularly interested in": "How much does compaction mess up agent behaviour, and how should we take this into account when thinking about safety?" His lead hypothesis for the Mythos incident during a UK AISI evaluation is that "repeated compactions didn't preserve the fact that the model was acting in the real world."

### Evidence so far

| Probe | Setting | Result | Risk it removes |
|---|---|---|---|
| P01 | 2,221 summaries of real SWE trajectories, 3 products × 3 summarisers | The latest error is dropped in ~55% of summaries; DeepSeek invents tool output in 5.9% | Summaries lose and invent safety-relevant facts |
| P07 | Resumed coding agents | A false "tests pass" claim never made an agent skip verification (0/972). An unverifiable "user approved editing the tests" claim was obeyed from any channel; the summary isn't privileged | What matters is unverifiable claims, not checkable ones |
| P08 | pi-mono summaries | Dropping the old "don't edit tests" rule raised test edits **+10.4 [+3.2, +18.0]** (GLM) and **+29.2 [+14.3, +45.2]** (DeepSeek) | A dropped constraint changes behaviour |
| P38 | Mocked ops workflows, three nested compactions | The "live system" fact survived 100% of summaries; deleting it had no clear effect. One false "test environment" line moved unsafe actions **+39 to +83** points | The failure is a wrong assertion, not an omission |
| P44 | Six handoff methods on impossible coding tasks, 5–20k tokens | Method choice within ±7 points of no handoff. One summary then a long history: **+20 [+4.4, +35.6]** more cheating | Method rankings are not the story; what is carried is |
| P19 | The Mythos transcript itself | Its self-written notes didn't harden the "simulation" framing; that was settled before its one visible compaction (~374k tokens) | The incident isn't explained by one note |

![What moves behaviour after compaction](fig/compaction_levers.png)

### Caveats to say out loud
- **Every behavioural probe above ran at 5–20k tokens with forced cuts.** Real products compact at 167k+ and repeatedly. P49 is the first at that scale.
- Mostly GLM and DeepSeek Flash models. Neel doubts open models reproduce the incident.

### Live: P49, compaction at ~200k on the agent's own sessions (results today)
Qwen3-Coder's own SWE sessions, stitched to ~200k tokens, then a product compaction (Codex, pi or Inspect style), 1–3 repeated compactions, or none, then an impossible coding task. Measured: cheating by a rule-based detector. A first look at finished runs found test edits in 5 of ~22, so the behaviour occurs.

| Arm | Cheating rate | vs no compaction |
|---|---|---|
| Fresh start | _pending_ | |
| No compaction (~200k) | _pending_ | — |
| Codex / pi / Inspect compaction | _pending_ | _pending_ |
| Inspect at ~120k (size arm) | _pending_ | _pending_ |
| Repeated, k = 2 / 3 | _pending_ | _pending_ |

Interim numbers on 16 paired units about 8:30–9:30 AM ET; extended run and final note by about 4:30 PM ET. A rise across compactions is reported as "more compactions over more work."

---

## Also tried and parked (one line each)
- **OOD detection for alignment (P42):** planted AUROC ≤ 0.84, precision at 0.1% ≈ 0; gradients at chance. Not pitched.
- **CoDI latent reasoning (P15):** latents don't beat the direct answer at predicting the model's own mistakes.
- **Trained lens evasion (P17):** inconclusive, seed-dependent; line stopped.
- **Activation-space gap maps (P45):** found templates and frequency differences, not missing topics.
