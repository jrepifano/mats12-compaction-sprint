# CliffCompaction - Cost-Efficient Compaction for Long-Horizon Coding Agents

- **Authors:** Trang Nguyen, Eulrang Cho, Bingqing Chen, Tim Dettmers (Carnegie Mellon University; Bosch Center for AI)
- **arXiv/venue:** arXiv 2609.26779v1 [cs.AI], 22 Sep 2026 (preprint)
- **URL:** https://arxiv.org/abs/2609.26779
- Captured 2026-10-14 for P49. P08's lit review discussed it; it was not previously captured in `papers/`.
- **Reading depth:** abstract, §1 tail, §2 (design and the precision/recall argument), §2.1 start and Appendix C.1 (re-read analysis, Figure 6) read verbatim from `pdftotext -layout`. Benchmark result tables (§3–4) only skimmed via grep for thresholds. Text extraction saved next to this file (`.txt`).

## Verbatim: abstract

> Agents often work on complex problems that require millions of tokens of context, which necessitates compacting across sessions due to limited context windows. We develop CLIFFCOMPACTION, an autocompaction technique that reduces cost by up to 50% under a bounded context while maintaining or improving performance on Terminal-Bench and achieving new levels of efficiency for test-time scaling and state-of-the-art results on KernelBench. The per-rollout savings of CLIFFCOMPACTION make the performance–cost trade-off of test-time scaling more efficient, adding over 10 percentage points on Terminal-Bench for less than the cost of two full-context runs. Under parallel test-time scaling, CLIFFCOMPACTION lets Kimi K2.6 match Opus 4.7, and exceed Opus 4.6 and GPT-5.3 Codex at lower cost. The key to CLIFFCOMPACTION's effectiveness is that it keeps compacted information faithful by only truncating or dropping content, never rephrasing or rewriting it. We never compact a compaction—each pass operates only on original content, and prior compacted output is discarded, preventing context drift from accumulating. These properties sustain continual learning over sessions exceeding a million tokens: on KernelBench, CLIFFCOMPACTION reaches CUDA kernel speedups of 2.23× after 200 steps and 3.58× after 400 steps, surpassing specialized search algorithms and trained agents despite being a general-purpose compaction technique. We open-source a scaffold-agnostic API-proxy implementation of CLIFFCOMPACTION usable with Claude Code, Codex and other harnesses.

## Verbatim: key results

> "Using summaries has the problem that if most of the information is summarized correctly, the model assumes it has all the information and does not need to revisit past state, such as documentation or code files (Appendix C, Figure 6). This leads to subtle but continuous context drift if the summary does not preserve the right information. We call this issue low compaction precision".

> "CLIFFCOMPACTION can be seen as having low compaction recall, since it throws away all direct information after two compactions. Summaries have high recall since they can retrieve information from many past compactions. On the other hand, CLIFFCOMPACTION has high compaction precision, because it preserves user directives and model generations in full for two full compaction windows".

Appendix C.1 (SWE-bench Verified, Kimi K2.7, 16K threshold, relative to full context at 36.71 steps and 10.90 re-reads per instance):
> "sliding window, summarization + microcompaction and CLIFFCOMPACTION each adding more than four steps, while summarization rises by only +1.26. Almost all of these additional steps are spent re-reading files the agent had already opened. ... Summarization adds the fewest re-reads (+2.35) and is the only strategy that also opens fewer new files (−0.68 per instance). It replaces the discarded content with a plausible natural-language paraphrase, which appears to satisfy the agent and discourages it from returning to the ground-truth source."

Thresholds tested: "B ∈ {45K, 32K, 16K, 8K}"; Claude Code's native auto-compaction compared "at a matched mean peak context of ∼45K".

The "context drift" from compacting a compaction is asserted as a design rationale. The parts read contain no experiment that measures drift per compaction round.

## Relevance to P49

- **(a)** Supplies the mechanism argument for why later compactions could matter more: summary-of-summary stacking compounds paraphrase drift. But it is argued, not measured. The authors design it away ("We never compact a compaction").
- **Behavioural lesson for P49:** a plausible summary *suppresses re-verification* (fewest re-reads; fewer new files opened). P49 can score "did the agent re-check live state before acting" as a behavioural outcome, and should expect summaries to reduce it relative to truncation.
- **Comparison arm idea:** a non-rewriting compactor (truncate or drop verbatim) vs a rewriting summariser at the same budget separates "fact dropped" from "fact mutated by paraphrase".

## Additional verbatim results (second read by lit sub-agent C, 2026-10-14)
Read verbatim from the same `pdftotext -layout` extraction: §3.1 threshold paragraph, §3.2 with Tables 2 and 3, §6 with Table 6.

**Table 2 (Terminal-Bench; % resolved).** Terminus-2 on TB 2.0, Kimi K2.6: full context (no compaction) 59.16±3.41; Summarization 32K 58.36±2.57, 16K 55.45±0.64, 8K 42.97±4.41; CliffCompaction 32K 61.42±4.25, 16K 61.42±1.30, 8K 50.00±2.40. GLM 5.1: full 49.83±3.42; CliffCompaction 32K 53.20±5.31, 16K 54.33±2.35, 8K 44.93±3.35. Claude Code on TB 2.1, **GLM 5.3 Flash**: Summarization (Claude Code autocompaction) at 200K 73.03±3.04 ($0.21), at 45K 70.97±3.72 ($0.14); CliffCompaction at 45K 76.69±1.41 ($0.16). "On Terminal-Bench 2.1 we evaluate GLM 5.3 Flash under Claude Code. At a matched ∼45K mean peak context, CliffCompaction reaches 76.69%, exceeding both Claude Code's auto-compaction at the same budget (70.97%) and its default 200K configuration (73.03%), and doing so with markedly lower seed variance."

**Table 3 (SWE-bench Verified, mini-swe-agent, % resolved; full / 32K / 16K / 8K).** Kimi K2.6 73.87 / 73.27 / 71.87 / 67.60; GLM 5.1 71.40 / 68.80 / 69.33 / 65.13; GLM 4.7 Flash 43.20 / 43.60 / 41.20 / 30.70.

**Table 6 (SWE-bench Verified at 16K, GLM 5.2, % resolved).** Sliding window 72.87±0.99; Summarization (Claude Code's summarization prompt) 70.27±1.01; Summarization + Microcompaction (reimplementation of Claude Code's two-tier scheme: "clears the contents of old tool observations while preserving the corresponding tool calls and the 5 most recent observations verbatim") 72.73±2.08; CliffCompaction 72.60±1.22. "Summarization is the weakest on quality for both models".

**Relevance (addendum):** the only published number for P49's agent and harness found: GLM 5.3 Flash under Claude Code autocompaction at a ~45K budget vs 200K, 70.97 vs 73.03 (difference within about one seed SD). Task success only; no fact-level retention or compaction counts reported in the parts read.
