# The Compaction Cliff in Long-Running AI Agent Memory

- Authors: Saber Zerhoudi, Jelena Mitrovic, Michael Granitzer (Passau). arXiv:2608.22752, submitted 24 Aug 2026 (cs.AI, cs.IR); listed as CIKM 2026.
- URL: https://arxiv.org/abs/2608.22752 (PDF: https://arxiv.org/pdf/2608.22752)
- Captured 2026-10-13 for P38.
- **Reading depth:** abstract, §4.3 (compaction), §4.5 (downstream), Tables 5, 6, 7, 8 and 10 read verbatim from the PDF (pdftotext; upgraded 2026-10-13 for P44). §2-3 (framework, theorems), §4.1-4.2 and 4.4 (corpora, classifier) skimmed only; appendices and the released artifact not read. Text extraction: `The Compaction Cliff in Long-Running AI Agent Memory.txt` (two-column layout, tables partly interleaved; Tables 5-6 below were re-extracted per page). Earlier fetch-tool summary was cross-checked against the Mem0 blog (https://mem0.ai/blog/testing-memory-placement-against-compaction-cliff).
- **Corrections to the earlier paraphrase (2026-10-13):** (1) the 50-58% for truncation/windowing/pruning is single-round constraint preservation at 50% compression (Table 5: 0.50 / 0.58 / 0.51), not a multi-round figure; the paper reports no multi-round curve for those three. (2) Only Sonnet 4.6 `/compact` (0.53 to 0.10) and gpt-5.4-mini single-shot and TypeCompact (0.96) appear in the five-round Figure 3. The multi-round rollout was run on 2 of the 4 LLM families. (3) Typed compaction is 1.00 in one round and 0.96 from round 2 onward (not a "96-100%" range across methods). (4) Retrieval recall@50: non-LLM baselines BM25 49%, dense 91%, reranked dense 91%; single-shot LLM retrievers 53% / 70% / 73%; TypeRetrieve 100% (Table 8). The "49-91%" range is the non-LLM baselines only. (5) The 20 production configurations in the abstract are the Figure 2/3 subset of AgentArtifactCorpus (AAC); Table 5 uses 50 AAC configurations. (6) The unit is the classifier-labelled constraint line, scored by a key-token test (kappa 0.92 vs two humans on preserved/lost); humans judged stricter (Weakened class).

## Verbatim: abstract
A safety rule and an episodic log compete for the same tokens in an AI agent's context. When the budget overflows, both are summarized at the same rate; only the rule needs exact wording to remain enforceable. On 20 production agent configurations, Claude Code's /compact prompt on Sonnet 4.6 preserves 53% of safety rules after one compaction round and 10% after five. We name this the Compaction Cliff. We address it with Knowledge Triage, a framework that classifies each line of an agent's knowledge base by type and routes each type through its own retention policy. Three deterministic operators implement this triage across the three context-management operations: TypeCompact rewrites items in place under per-type fidelity, TypeDecompose partitions a topic too large to compact safely, replicating in-scope safety rules across partitions, and TypeRetrieve fetches items from external storage with in-scope rules pinned ahead of relevance. On five public corpora, TypeCompact preserves 2-4x more safety rules than the strongest single-shot LLM compactor at every ratio, with 96% recall over five rounds. TypeDecompose reaches 0% locality violations against 93% under uniform partitioning. TypeRetrieve reaches 100% recall@50 against 73% for the best single-shot LLM retriever. On three downstream behavioral benchmarks, we outperform the production Sonnet compactor on medical compliance (paired McNemar p < 10^-8 on preservation, N = 200), the full-policy and hierarchical baselines on retail task pass rate (p < 0.01, N = 115), and the hierarchical compaction on the airline domain (p = 0.024). We release AgentArtifactCorpus (396,934 agent configurations from 54,628 public GitHub repositories), the classifier, and the reference implementation.

## Verbatim: setup of the compaction experiment (§4.3)
"We tested ten strategies on 50 AAC configurations stratified by language, star count, and artifact count (622 constraints, 2,257 procedures). The strategies are: three structural baselines (hierarchical truncation: keep the first N tokens; temporal windowing: keep the last N; aggressive pruning: drop the items with the highest unigram-frequency tokens); LLMLingua-2; four LLM compactors invoked with the production prompt "compress to N tokens, keep every safety rule and procedural command verbatim" (gpt-5.4-nano, gpt-5.4-mini, Sonnet 4.6 behind Claude Code's /compact, Opus 4.7); MaRS-FL (our reimplementation of the MaRS design choice this paper opposes; §5); and TypeCompact. Each ran at three compression targets (50%, 25%, 10%) on a 20-configuration subset, and was iterated for five rounds at 50% per round to reflect a long-running agent. Constraint recall is the fraction of a configuration's classifier-labeled constraints that survive, measured by the key-token test of §4.5 (validated against two human annotators, kappa = 0.92 on Preserved/Lost)."

"Across all eight type-blind strategies, the best single-round recall was 0.53 at 50%, 0.39 at 25%, 0.24 at 10% (Figure 2, Table 6); five rounds of /compact drove recall from 0.53 to 0.10 (Figure 3). The decay appeared for every LLM family and every structural baseline, so the loss is not specific to one prompt or one model. [...] TypeCompact returned 1.00 / 0.95 / 0.80 constraint recall at 50 / 25 / 10% on the same 50 configurations, stabilizing at 0.96 from the second round onward."

## Verbatim: Table 5 (page 6)
Table 5: Preservation rate by knowledge type at 50% compaction across 50 AAC configurations.

| Strategy | C | P | B | F | E | All |
|---|---|---|---|---|---|---|
| Hierarchical truncation | 0.50 | 0.82 | 0.75 | 0.63 | 0.77 | 0.69 |
| Temporal windowing | 0.58 | 0.80 | 0.64 | 0.78 | 0.91 | 0.74 |
| Aggressive pruning | 0.51 | 0.75 | 0.67 | 0.66 | 0.84 | 0.69 |
| TypeCompact | 1.00 | 1.00 | 0.50 | 0.51 | 0.84 | 0.77 |

"Items classified by tau; higher is better. C/P/B/F/E as in Table 2; All: unweighted mean." (Table 2: C = constraint, P = procedural, B = belief, F = preference, E = episodic.)

## Verbatim: Table 6 (page 7)
Table 6: TypeCompact vs SOTA prompt compactors at three compression ratios, mean across 20 AAC configurations.

| Strategy | C | P | mF1 | time | tok |
|---|---|---|---|---|---|
| **50% compression** | | | | | |
| TypeCompact | 1.00 | 0.92 | 0.75 | 0.0s | 0 |
| LLMLingua-2 | 0.21 | 0.72 | 0.63 | 0.5s | 0 |
| MaRS-FL (ours, no public impl) | 0.99 | 0.89 | 0.69 | 0.5s | 0 |
| gpt-5.4-nano (single-shot) | 0.52 | 0.80 | 0.69 | 9.9s | 56K |
| gpt-5.4-mini (single-shot) | 0.48 | 0.77 | 0.68 | 7.3s | 57K |
| Sonnet 4.6 (/compact) | 0.53 | 0.83 | 0.73 | 73.9s | 48K |
| Opus 4.7 (single-shot) | 0.45 | 0.81 | 0.72 | 31.1s | 50K |
| **25% compression** | | | | | |
| TypeCompact | 0.95 | 0.71 | 0.57 | 0.0s | 0 |
| LLMLingua-2 | 0.08 | 0.61 | 0.46 | 0.5s | 0 |
| MaRS-FL (ours, no public impl) | 0.75 | 0.61 | 0.51 | 0.2s | 0 |
| gpt-5.4-nano (single-shot) | 0.26 | 0.60 | 0.50 | 9.3s | 53K |
| gpt-5.4-mini (single-shot) | 0.29 | 0.62 | 0.49 | 7.5s | 56K |
| Sonnet 4.6 (/compact) | 0.39 | 0.65 | 0.51 | 166.2s | 42K |
| Opus 4.7 (single-shot) | 0.26 | 0.68 | 0.51 | 35.6s | 43K |
| **10% compression** | | | | | |
| TypeCompact | 0.80 | 0.47 | 0.38 | 0.0s | 0 |
| LLMLingua-2 | 0.01 | 0.27 | 0.14 | 0.5s | 0 |
| MaRS-FL (ours, no public impl) | 0.43 | 0.22 | 0.27 | 0.02s | 0 |
| gpt-5.4-nano (single-shot) | 0.14 | 0.43 | 0.33 | 5.6s | 43K |
| gpt-5.4-mini (single-shot) | 0.16 | 0.47 | 0.32 | 5.1s | 46K |
| Sonnet 4.6 (/compact) | 0.24 | 0.54 | 0.30 | 420.3s | 36K |
| Opus 4.7 (single-shot) | 0.17 | 0.52 | 0.31 | 22.8s | 37K |

"Outputs truncated to budget tokens before scoring; Sonnet 4.6 row uses Claude Code's /compact; MaRS-FL reimplements MaRS [2] (§5). C: constraint preservation, P: procedural, mF1: macro F1, time/tok: mean latency / LLM tokens per config."

## Verbatim: Figure 3 (five sequential rounds)
"Figure 3: Constraint recall after N sequential compaction rounds at 50% per round, 20 AAC configurations." Only the endpoints are printed as labels: TypeCompact 0.96 (stable from round 2), Sonnet 4.6 /compact 0.10 at round 5 (0.53 at round 1); gpt-5.4-mini single-shot is also plotted (values not printed). Structural baselines are not in Figure 3.

## Verbatim: Table 8 (retrieval, page 8)
Table 8: In-scope constraint recall@k on a 1,033-item mixed corpus, 50 queries.

| Retriever | Variant | @5 | @10 | @20 | @50 |
|---|---|---|---|---|---|
| BM25 | baseline | 32% | 38% | 43% | 49% |
| Dense (octen) | baseline | 34% | 45% | 67% | 91% |
| Reranked dense | baseline | 25% | 36% | 55% | 91% |
| TypeRetrieve | dense | 40% | 57% | 96% | 100% |
| gpt-5.4-nano (one-shot) | baseline | 34% | 43% | 51% | 53% |
| gpt-5.4-mini (one-shot) | baseline | 34% | 46% | 64% | 70% |
| Sonnet 4.6 (one-shot) | baseline | 34% | 47% | 61% | 73% |

(sec/q and tok/q columns omitted here: 0 tokens for non-LLM and TypeRetrieve; 5,776-6,741 tokens/query for the LLM retrievers.)

## Verbatim: Table 7 (decomposition) and Table 10 (tau-bench retail)
Table 7: Decomposition constraint-locality on 200 AAC configurations, 25%-of-total partition budget.

| Strategy | Mean viol. | Configs viol. | Overhead |
|---|---|---|---|
| chunk_by_tokens | 32% | 93% | 0% |
| chunk_by_topic | 13% | 40% | 0% |
| chunk_by_paragraph | 32% | 93% | 0% |
| TypeDecompose | 0% | 0% | +14.5% |

Table 10: tau-bench retail pass rate, N = 115 stratified tasks per cell, 1,035 paired rollouts (1,019 completed without error).

| Model | Full | Hier. | TypeC. | p vs full | p vs hier. |
|---|---|---|---|---|---|
| gpt-5.4-nano | 22.5% | 21.6% | 36.0% | 0.011 | 0.007 |
| gpt-5.4-mini | 27.3% | 29.1% | 35.5% | 0.176 | 0.324 |
| qwen3-next-80b | 36.1% | 37.0% | 41.7% | 0.345 | 0.424 |
| Paired overall | 28.6% | 29.2% | 37.7% | 0.003 | 0.005 |

SafetyMed (N = 200, §4.5): TypeCompact 97.0% pass / 95.5% preservation; hierarchical 98.0% / 93.5%; single-shot Sonnet 4.6 92.5% / 81.0%; full-policy ceiling 96.5% / 100.0%. Airline (117 pairs): mean pass 26.5% TypeCompact, 34.2% full, 15.4% hierarchical.

## Relevant to P38
- Claim: compaction is type-blind. Safety rules and episodic logs are summarised at the same rate, although rules need exact wording to stay enforceable.
- Claude Code `/compact`, Sonnet 4.6, 20 production AGENTS.md-style configurations: 53% of safety rules preserved after one compaction round, 10% after five.
- Proposed fix "Knowledge Triage": TypeCompact preserves 2-4x more safety rules than the strongest single-shot LLM compactor, 96% recall over five rounds; TypeDecompose 0% locality violations vs 93% uniform; TypeRetrieve 100% recall@50 vs 73%.
- Downstream: medical compliance (McNemar p < 1e-8, N=200), retail pass rate (p < 0.01, N=115), airline (p=0.024).
- The unit is a written rule in a config file, not a fact established by tool output (P38's case). No environment-reality belief is measured.

## Relevant to P44
- Measures retention of written rules in AGENTS.md-style configs under repeated compaction, scored by a key-token test; it does not run an agent over many handoff generations and measure aligned behaviour. The behavioural benchmarks (SafetyMed, tau-bench retail/airline) are single compaction of a static policy, not repeated.
- Gives P44 a retention prior per handoff method: summary (`/compact`) 0.53 then 0.10 over five 50% rounds; last-k ("temporal windowing") 0.58 and head-truncation 0.50 in one round (no multi-round figure); pinning-like TypeCompact 1.00 then 0.96 from round 2. Pinning only helps as far as the classifier recalls the constraint (SafetyMargin 0.93 recall; 9 missed SafetyMed items cost 4.5 preservation points).
- Caveat: the compression target is a fixed fraction (50%/round) of original tokens, not a context-window overflow trigger; /compact numbers use the paper's 'compress to N tokens, keep every safety rule' prompt harness.
- Human judges were stricter than the key-token metric (Weakened class): preserved rate fell 15 points for temporal windowing (65% to 50%), 23 for hierarchical truncation (64% to 41%), 30 for aggressive pruning (71% to 41%), 5 for TypeCompact (91% to 86%).
