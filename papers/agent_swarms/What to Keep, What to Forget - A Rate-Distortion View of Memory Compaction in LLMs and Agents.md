# What to Keep, What to Forget - A Rate-Distortion View of Memory Compaction in LLMs and Agents

- **Authors:** Ashwin Gerard Colaco, Nada Lahjouji (University of California, Irvine)
- **arXiv/venue:** arXiv 2607.08032v1 [cs.LG], 9 Jul 2026 (survey and position paper with a small reference experiment)
- **URL:** https://arxiv.org/abs/2607.08032
- Captured 2026-10-14 for P49. (Title uses "Rate–Distortion" with an en dash; the filename uses a hyphen.)
- **Reading depth:** abstract, the "Reversibility and the compounding of loss" paragraph, §13.1–13.2 (benchmarks and the missing benchmark) and §14 (reference experiments 1–2) read verbatim from `pdftotext -layout`. The survey body (§2–12, taxonomy) was not read. Text extraction saved next to this file (`.txt`).

## Verbatim: abstract (excerpt)

> Large language models, and the agents built on them, spend an ever-growing share of their compute and memory on remembering ... We argue that these are instances of one problem: a rate–distortion decision about what context-derived information to retain versus discard, at what fidelity, under a resource budget, so as to preserve downstream task utility. ... Two patterns hold across the survey. At every layer the signal that decides what to keep is attention magnitude or recency, and it fails in the same way everywhere, by discarding, before the query is known and with no way to undo it, information the query later needs. And while compression is measured carefully on single-turn long context, the repeated compaction that agents actually perform is almost never measured, and no benchmark holds one budget axis across all the layers at once.

## Verbatim: key results

> "Irreversible schemes (lossy summarization, fold/prune, UPDATE/DELETE, weight edits) cannot, and applied repeatedly, each compaction event composes its loss with the last. This is prediction (4): under repeated irreversible summarization, end-task error should grow super-linearly in the number of compaction events, because errors both accumulate and self-reinforce (a stored mistake biases the retrieval that feeds the next summary), whereas a reversible, retrieval-backed memory stays flat. It is also the layer's least-measured failure. Benchmarks such as LOCOMO and LongMemEval probe single-shot recall, multi-session reasoning, and knowledge updates, but not the compounding curve over many consolidation cycles".

Experiment 2 (single RTX 4060 GPU, small model):
> "An agent reads a long document in chunks, accumulating twelve key–value facts spread throughout, and periodically compacts its working memory. We compare two operators as the number of compaction events grows. The irreversible operator overwrites working memory with an LLM-generated summary. The reversible operator keeps all chunks in an archive and retrieves the relevant chunk at query time ... At the end we ask for all twelve facts and report recall."

> "The reversible operator holds recall near 0.95 at every compaction frequency, since retrieval can re-derive any dropped fact. The irreversible operator runs far below it, between 0.33 and 0.56, and is weakest at the highest compaction frequency, because each summary throws away facts the next summary can no longer see and the loss compounds."

Proposed protocol: "plot end-task error against the number of compaction events, and it should climb super-linearly for irreversible summarization while staying flat for reversible memory".

## Relevance to P49

- **(a)** An explicit, falsifiable prediction that loss grows super-linearly in the number of compaction events. The only test is a toy (12 key–value facts, small model, short contexts, compaction *frequency* varied rather than a per-round curve). P49 could test this prediction for safety facts at ≥100k: survival after round 1, 2, 3, against a compounding (independent per-round loss) null.
- **Suggested analysis:** compare observed survival after k rounds against p₁^k (independent loss per round). Above-null loss at round 2 would be the "second compaction" effect. "Self-reinforcing" errors would show up as mutated facts that persist once introduced.
- **(d) Scoring:** key–value recall is exact-match, so it is regex-scorable. NoLiMa's caution, cited here, is that lexical-match probes flatter retention. P49 should phrase recall probes without lexical overlap with the planted fact.
