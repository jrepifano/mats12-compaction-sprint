# ACON - Optimizing Context Compression for Long-horizon LLM Agents

- **Authors:** Minki Kang, Wei-Ning Chen, Dongge Han, Huseyin A. Inan, Lukas Wutschitz, Yanzhi Chen, Robert Sim, Saravan Rajmohan (KAIST, Microsoft, Cambridge)
- **arXiv/venue:** arXiv 2510.00615v3 (1 Jun 2026); ICML 2026 (PMLR 306)
- **URL:** https://arxiv.org/abs/2510.00615 ; code https://github.com/microsoft/acon
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, §1-§4.5 (method, setup, Tables 1-4, Figure 6 threshold ablation text), Appendix B.2 implementation settings, C.2 case study and WebVoyager table, C.3, the failure-analysis prompt (root-cause categories), parts of the optimised guidelines, and Compression Example D.2 read verbatim from `pdftotext -layout` of the arXiv PDF. Other appendix tables and prompts skimmed. PDF not committed.

## Verbatim: abstract
"Large language models (LLMs) are increasingly deployed as agents in dynamic real-world environments, where success depends on maintaining precise records of actions and observations. However, the resulting unbounded context growth in long-horizon agentic tasks makes two critical bottlenecks: prohibitive inference memory costs and reasoning degradation due to irrelevant information. Existing compression methods fail to fully address this, often relying on brittle heuristics or requiring parameter updates impractical for proprietary or large-scale LLMs. We introduce Agent Context Optimization (ACON), a unified framework that optimally compresses both observations and history into concise, informative representations. Distinct from prior works, ACON employs an optimization in natural language space: it iteratively refines compression guidelines based on failure analysis of the agent, ensuring critical state information is preserved without model fine-tuning. To further minimize computational overhead, we distill the optimized compressor into smaller models. Experiments on AppWorld, OfficeBench, and Multi-objective QA demonstrate that ACON reduces peak token usage by 26–54% while improving task success over existing compression baselines. Notably, it enables smaller LMs to function effectively as long-horizon agents, achieving up to 46% performance improvement by mitigating context distraction."

## Verbatim: key results

**Regime (Appendix B).** "For history compression, we set Thist = 4096 for AppWorld and OfficeBench, and 2048 for 8-objective QA. We keep the last action, observation pair to preserve the latest information. This is the same for ACON and all baselines. For observation compression, we set Tobs = 1024 for AppWorld, 512 for OfficeBench, and 400 for 8-objective [QA]." "For FIFO, we keep last 5 interaction turns ... For LLMLingua, we set keep rate as 30% for both history and observation."

**Guideline optimisation (§3.3).** "On the training set Dtrain, we run the LLM agent both without and with context compression to obtain baseline context H and compressed context H′. We collect tasks where the agent succeeds with H but fails with H′, forming a contrastive subset Dcont. For each task in Dcont, we query a optimizer LLM with the context before and after compression to obtain natural language feedback". Failure-analysis prompt root-cause categories: "Missing Critical Fact, Incorrect Summary, Lost Variable/State, Unnecessary Re-discovery, Instruction Drift, API Misuse, Premature Completion, Token Truncation, Inefficient Looping, Redundant API Calls, Over-Exploration, Other". It asks for "the FIRST divergence point where the optimized trajectory meaningfully deviates" and "Critical facts present in baseline but absent/altered in optimized (esp. after a session boundary)".

**Table 1 (AppWorld test-normal, agent and compressor gpt-4.1; Acc / Steps / Peak tokens x10^3).**

| Method | Avg Acc (168) | Steps | Peak | Easy (57) | Medium (48) | Hard (63) |
|---|---|---|---|---|---|---|
| No compression | 56.0 | 16.14 | 9.93 | 80.7 | 47.9 | 39.7 |
| History: FIFO | 45.8 | 28.48 | 6.73 | 84.2 | 39.6 | 15.9 |
| History: Retrieval | 27.4 | 33.17 | 8.39 | 61.4 | 12.5 | 7.9 |
| History: LLMLingua | 39.3 | 24.42 | 7.50 | 66.7 | 37.5 | 15.9 |
| History: Prompting (naive summary) | 43.5 | 24.01 | 6.93 | 66.7 | 41.7 | 23.8 |
| History: ACON UT | 51.2 | 20.92 | 7.17 | 77.2 | 50.0 | 28.6 |
| History: ACON UTCO | 56.5 | 22.82 | 7.33 | 86.0 | 56.2 | 30.2 |

(Observation-compression rows omitted here; see PDF.) Text: ACON "reduces peak tokens by over 25% while preserving the accuracy of the no compression upper bound, outperforming all baselines that suffer severe degradation on medium and hard tasks spanning longer steps."

**Table 2a (OfficeBench, gpt-4.1):** No compression 76.84; FIFO 67.37; Retrieval 65.26; LLMLingua 70.53; Prompting 71.58; ACON UT 74.74; ACON UTCO 72.63 (history compression).

**Threshold ablation (§4.5, Figure 6).** "Results show that smaller thresholds reduce tokens but incur more frequent compression calls and degrade accuracy, while larger thresholds preserve accuracy with higher cost. Moderate values (4096 for history, 1024 for observation) provide the best trade-off". Figure 6 plots "the number of compressions (compression frequency) for each length of task trajectories (task steps)" for thresholds 2048/4096/8192; numbers are plot-only.

**UT vs UTCO trade-off (§4.2).** "in high-fidelity information-seeking tasks such as OfficeBench and 8-objective QA, UT is generally safer because over-compression may remove subtle facts needed for final decisions. Therefore, UTCO is recommended when observations contain substantial redundancy, while UT is preferable when precise fact retention is critical."

**Example D.2 (8-objective QA, same question, three compressors).** Naive prompting summary: "Identified the last time the Dodgers played the Yankees in the World Series: 1981." ACON UT summary: "Last Dodgers vs Yankees World Series: 1955 (source: "1955 World Series")". ACON UTCO summary: "When was the last time the Dodgers played Yankees in the World Series? -> 1956". (The paper does not comment on the differing values; whether these are summariser mutations or different underlying trajectories is not stated.)

## Relevance to P49
- **Regime is far from P49.** Compaction triggers at 2-8K tokens on 15-50-step tasks, with many compactions per trajectory (Figure 6), and the last action/observation pair kept verbatim. Nothing at or above 100K, and no per-compaction-index analysis.
- **Effect size of naive summarisation is large even here:** history summarisation with a generic prompt costs 12.5 points on AppWorld (56.0 to 43.5) and 16 points on hard tasks, and raises steps from 16 to 24 (re-discovery). That is the "compaction hurts" prior, with no-compression as the control at the same model; it does not separate compaction from context rot because the no-compression context is only ~10K.
- **Scoring method to borrow:** contrastive pairs (same task, with vs without compaction; succeeded vs failed) and a judge that names the first divergence point and lists "Critical facts present in baseline but absent/altered in optimized (esp. after a session boundary)". P49 can use the same paired design (fork the trajectory at the compaction point, run with full context vs compacted context) and the same root-cause taxonomy, which already has "Instruction Drift" and "Incorrect Summary" categories close to P49's "mutated" label. The judge is not validated against humans in the paper.
- **Mutation evidence:** Example D.2 shows three summaries disagreeing on a single fact (1981/1955/1956); it is anecdotal and unexplained, but it is the kind of silent numeric mutation P49 should code separately from omission.
