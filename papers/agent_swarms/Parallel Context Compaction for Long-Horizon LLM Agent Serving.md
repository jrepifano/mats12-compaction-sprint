# Parallel Context Compaction for Long-Horizon LLM Agent Serving

- **Authors:** Musa Cim, Burak Topcu, Chita Das, Mahmut Kandemir (Pennsylvania State University)
- **arXiv/venue:** arXiv 2605.23296v1 [cs.AI], 22 May 2026 (preprint)
- **URL:** https://arxiv.org/abs/2605.23296
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, introduction excerpt and §5 (characterizing sequential compaction: §5.1 input and prompt invariance, §5.2 run-to-run instability with Tables 5–6) read verbatim from `pdftotext -layout`. The parallel-compaction method and throughput results (§3–4, §6) were not read. Text extraction saved next to this file (`.txt`).

## Verbatim: abstract

> Long-horizon LLM agents accumulate growing conversation histories that eventually exceed the model's context window. Context compaction via LLM-based summarization keeps the conversation bounded, but summarization is inherently lossy and the blocking call stalls agent inference for tens of seconds. Moreover, the operator has no fine-grained control over summary volume since prompt instructions are largely ignored, and as context grows, both the amount of output tokens the model produces and the information it retains fluctuate substantially from run to run, making the agent's retained knowledge unpredictable across runs. We introduce parallel compaction for long-horizon agentic flows and characterize it against the sequential synchronous baseline across four backbones spanning 8B to 120B parameters, mixing dense and MoE architectures with reasoning and non-reasoning models, on the HotpotQA multi-hop QA and LoCoMo long-context dialogue benchmarks. Parallel compaction gives the operator fine-grained, predictable control over summary volume and enables more targeted prompt engineering per block. At matched compaction decode volume, it reduces end-to-end wall time and improves compaction throughput over the sequential baseline.

## Verbatim: key results

> "First, all four backbones produce output tokens in a narrow range across the full 2 k to 96 k input sweep, which is far flatter than the 48× growth in input length would suggest. Second, switching the prompt from concise to very detailed barely moves the curves." ... "at 2 k input, the model retains roughly 48% of the tokens in the summary, but at 96 k, that figure drops to ∼3%." ... "As a result, a 500-token summary at 96 k input retains less than 1% of the original context."

> "we sweep input length from 4 k to 96 k tokens and, at each point, run compaction ten times under identical prompt and input conditions with temperature-driven sampling, measuring how much the output length varies and how similar the generated summaries are to one another."

Table 5 (stability across input sizes; CV of summary length ↓, mean pairwise cosine similarity of summary embeddings ↑), 4k → 96k:

| Model | CV 4k | CV 96k | Cosine 4k | Cosine 96k |
|---|---|---|---|---|
| gpt-oss-20B | 24.5% | 42.9% | 0.777 | 0.624 |
| gpt-oss-120B | 19.8% | 68.6% | 0.825 | 0.619 |
| Llama-3.1-8B | 34.1% | 84.5% | 0.652 | 0.472 |
| Llama-3.3-70B | 24.4% | 171.6% | 0.729 | 0.491 |

> "Takeaway #4: Run-to-run instability is intrinsic to LLM-based compaction and compounds as context grows — both output length and summary content become increasingly unpredictable."

> "if the compacted summary omits a key fact in one run but retains it in another, downstream QA accuracy will vary between runs not because the model reasoned differently, but because it received a different context".

## Relevance to P49

- **(b) Context:** at ~96k input, summaries are ~500 tokens, retaining about 1–3% of the input, almost regardless of prompt. At P49's ≥100k regime the compactor is choosing a handful of facts. Which safety facts make the cut is mostly a selection problem, and the selection is noisy run to run.
- **Design lesson:** sample several summaries (e.g. 5–10) per transcript and compaction boundary, and treat fact survival as a per-fact *probability*. A single summary per condition confounds compaction-round effects with summary sampling noise, which grows with input length (cosine 0.47–0.62 at 96k).
- No evidence on repeated compaction; single-shot summaries only. The models are open-weight (8B–120B), not frontier compactors.
