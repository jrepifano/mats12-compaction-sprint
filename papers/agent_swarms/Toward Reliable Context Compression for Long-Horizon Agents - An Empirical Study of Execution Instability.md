# Toward Reliable Context Compression for Long-Horizon Agents - An Empirical Study of Execution Instability

- **Authors:** Guanghui Min, Liang Wu, Mayank Darbari, Chen Chen, Liangjie Hong (University of Virginia; Nokia)
- **arXiv/venue:** arXiv 2608.06503v1 [cs.LG], 6 Aug 2026 ("Preprint. Work in progress.")
- **URL:** https://arxiv.org/abs/2608.06503 (code: https://github.com/nokia-applied-research/Trace)
- Captured 2026-10-14 for P49. P01's lit review cited it; it was not previously captured in `papers/`.
- **Reading depth:** abstract and §2 (recurrent compaction formalism) through §3.3 read verbatim from `pdftotext -layout`. §4 onward (the TRACE method, results tables) and the appendix prompts only skimmed via grep. Text extraction saved next to this file (`.txt`).

## Verbatim: abstract

> Recurrent context compression controls context growth in long-horizon agents, but its behavioral effects remain poorly understood. In this preliminary empirical study, we show that compression can weaken the influence of recent interactions, increasing blocked actions, repeated exploration, and instability across runs. Motivated by these observations, we introduce TRACE, a verifier-guided framework that evaluates individual compaction events through paired closed-loop continuations from the same environment state and uses summary preferences to optimize a natural-language compression prompt while keeping all models frozen. Initial results on AppWorld show improvements over existing compression baselines in task performance, multi-run reliability, and context–execution efficiency. These findings provide early evidence for boundary-local evaluation as a promising direction for reliable agent context compression.

## Verbatim: key results

**Regime:** "We use MiniMax-M3 as both compressor and downstream agent (MiniMax, 2026). Our evaluation harness adapts OpenClaw's recurrent compaction loop", on "the 147-task AppWorld train–development split". Budgets are 2K–16K tokens. Each new summary is S_t = C(S_{t−1}, Δ_t), i.e. a summary of the previous summary plus new turns.

> "Equation (1) makes context compression a recurrent intervention: once the working context exceeds the budget, the replacement context becomes the input to both subsequent decisions and later compaction events. Its quality therefore cannot be assessed from a single replacement in isolation."

> "Summary-based compaction does not uniformly outperform FIFO truncation: at 16K, 8K, and 4K, FIFO matches or slightly exceeds summary-based execution. Their difference emerges only under the tightest budget, where summary-based compaction reaches 72.8% while FIFO drops to 42.2%."

> "This instability is more pronounced on tasks with longer full-context execution horizons ... Even under summary-based compaction, longer-horizon tasks degrade more sharply as the budget shrinks, consistent with an accumulating effect of repeated context replacement."

> "Figure 2 shows that, at 2K, the summary condition terminates in only 44.6% of samples, with 37.3% using the required form, compared with 77.2%/68.1% for FIFO and 66.6%/60.6% for full history."

**Four-way matched-context design (the key method):**
> "For each compaction, let S_{t−1} denote the previous summary, Δ_t the new raw interaction history, and S_t = C(S_{t−1}, Δ_t) the updated summary. At every recorded decision boundary before the next compaction, we construct four matched contexts: the full raw prefix; S_{t−1} followed by the uncompressed Δ_t; the updated summary S_t; and S_{t−1} with Δ_t omitted. Each condition is then followed by the same recorded suffix and ends at the same decision point. We independently sample 24 next actions under each context."

> "Retaining the raw update yields the smallest divergence (0.149); compressing it into the updated summary increases divergence to 0.233; and omitting it entirely produces the largest shift (0.289)." (noise-corrected total-variation divergence of the next-API-call distribution from full history)

> "We evaluate 590 boundaries and 4,640 rollouts, using five samples per rendering for first compactions and three for later compactions, each capped at five actions." ... "POST produces 0.108 more blocked/error actions than PRE at the first action ... Refetching is initially weaker (0.031), but rises at the second action and remains positive thereafter."

The read sections give no breakdown of effects by compaction index (first vs later), although the sampling distinguishes them.

## Relevance to P49

- **(c) Best control design found:** the four matched contexts (full raw / S_{t−1} + raw Δ / S_t / S_{t−1} with Δ omitted) separate (i) loss from the *new* compaction step from (ii) loss already inherited from earlier summaries, at a fixed decision point. For P49 this gives a direct test of whether the 2nd or 3rd compaction adds damage beyond the 1st: compare S_1 + raw Δ_2 with S_2 for a fact first stated in segment 1 vs in segment 2.
- **Measure behaviour, not only text:** use next-action distributions (TV divergence from full history) sampled at a fixed boundary, plus the termination-form and blocked-action rates.
- **(a)** Only indirect: "longer-horizon tasks degrade more sharply ... consistent with an accumulating effect of repeated context replacement". No per-index numbers in the parts read.
- **(b)** Not ≥100k: budgets are 2K–16K, so this is not evidence for the ≥100k regime.
- AppWorld state is refetchable, which hides fact loss. P49's facts should be non-refetchable (stated once by the user, not stored in the environment), or the refetch rate should be scored explicitly.
