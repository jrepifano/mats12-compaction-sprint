# Agentic Context Engineering - Evolving Contexts for Self-Improving Language Models

- **Authors:** Qizheng Zhang, Changran Hu, Shubhangi Upasani, Boyuan Ma, Fenglu Hong, Vamsidhar Kamanuru, Jay Rainton, Chen Wu, Mengmeng Ji, Hanchen Li, Urmish Thakker, James Zou, Kunle Olukotun (Stanford, SambaNova, UC Berkeley)
- **arXiv/venue:** arXiv 2510.04618v3 (29 Mar 2026); ICLR 2026
- **URL:** https://arxiv.org/abs/2510.04618 ; code https://github.com/ace-agent/ace
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, §2.2 (brevity bias, context collapse, Figure 2), §3 (ACE method, §3.1-3.2), Appendix A.4-A.6 (Tables 16-19, incremental-update ablation) and the Appendix discussion of Dynamic Cheatsheet's update mechanism read verbatim from `pdftotext -layout` of the arXiv PDF. Main result tables (§4.3-4.4) only skimmed. PDF not committed.

## Verbatim: abstract
"Large language model (LLM) applications such as agents and domain-specific reasoning increasingly rely on context adaptation: modifying inputs with instructions, strategies, or evidence, rather than weight updates. Prior approaches improve usability but often suffer from brevity bias, which drops domain insights for concise summaries, and from context collapse, where iterative rewriting erodes details over time. We introduce ACE (Agentic Context Engineering), a framework that treats contexts as evolving playbooks that accumulate, refine, and organize strategies through a modular process of generation, reflection, and curation. ACE prevents collapse with structured, incremental updates that preserve detailed knowledge and scale with long-context models. Across agent and domain-specific benchmarks, ACE optimizes contexts both offline (e.g., system prompts) and online (e.g., agent memory), consistently outperforming strong baselines: +10.6% on agents and +8.6% on finance, while significantly reducing adaptation latency and rollout cost. Notably, ACE could adapt effectively without labeled supervision and instead by leveraging natural execution feedback. On the AppWorld leaderboard, ACE matches the top-ranked production-level agent on the overall average and surpasses it on the harder test-challenge split, despite using a smaller open-source model. These results show that comprehensive, evolving contexts enable scalable, efficient, and self-improving LLM systems with low overhead."

## Verbatim: key results

**Context collapse (§2.2, Figure 2).** "In a case study on the AppWorld benchmark (Trivedi et al., 2024), we observe a phenomenon we call context collapse, which arises when an LLM is tasked with fully rewriting the accumulated context at each adaptation step. As the context grows large, the model tends to compress it into much shorter, less informative summaries, causing a dramatic loss of information. For instance, at step 60 the context contained 18,282 tokens and achieved an accuracy of 66.7, but at the very next step it collapsed to just 122 tokens, with accuracy dropping to 57.1—worse than the baseline accuracy of 63.7 without adaptation. While we highlight this through Dynamic Cheatsheet (Suzgun et al., 2025), the issue is not specific to that method; rather, it reflects a fundamental risk of end-to-end context rewriting with LLMs, where accumulated knowledge can be abruptly erased instead of preserved."

Figure 2 caption: "Context Collapse. Monolithic rewriting of context by an LLM can collapse it into shorter, less informative summaries, leading to sharp performance drops."

**Brevity bias (§2.2).** "A recurring limitation of context adaptation methods is brevity bias: the tendency of optimization to collapse toward short, generic prompts. Gao et al. (Gao et al., 2025) document this effect in prompt optimization for test generation, where iterative methods repeatedly produced near-identical instructions ... sacrificing diversity and omitting domain-specific detail."

**Appendix, Dynamic Cheatsheet update mechanism.** "DC updates its memory by rewriting the cheatsheet as a whole, either by regenerating the full cheatsheet each step (DC-CU) or by writing a new summary from retrieved examples (DC-RS). With repeated full rewrites, the model tends to shorten and compress what was written before. This can cause context collapse, which means that useful domain-specific details get dropped over time or disappear suddenly (§2.2). ACE avoids full rewrites by using incremental delta updates. ... In our ablations (§4.6), removing delta updates leads to a large drop on AppWorld (-11.7% TGC and -27.8% SGC on test-normal), showing that delta updates are a core part of our method."

**Table 18 (A.5), AppWorld test-normal, DeepSeek-V3.1, offline adaptation.**

| Method | Test-Normal TGC | Test-Normal SGC | Average |
|---|---|---|---|
| ReAct | 63.7 | 42.9 | 53.3 |
| ReAct + ACE (no incremental update) | 67.3 (+3.6) | 46.4 (+3.5) | 56.9 (+3.6) |
| ReAct + ACE (with incremental update) | 76.2 (+12.5) | 64.3 (+21.4) | 70.3 (+17.0) |

(The appendix's "-11.7% TGC and -27.8% SGC" are relative drops: 8.9/76.2 and 17.9/64.3 points.) "We find that incremental updates are critical: by preserving useful information that would otherwise be lost to context collapse, they account for a large share of ACE's gains."

**Table 17 (A.4), FiNER, harmful reflector injected every X steps:** base LLM 70.7; X=1: 66.7 (-4.0); X=5: 76.1; X=10: 77.0; X=25: 77.8; X=50 and 100: 78.2; none: 78.3.

## Relevance to P49
- **What "context collapse" is and is not.** It is about a strategy playbook rewritten by an LLM at every adaptation step (hundreds of rewrites), not about conversation compaction of an agent's own trajectory. The quantitative evidence is one case study: one abrupt rewrite from 18,282 to 122 tokens at step 61, accuracy 66.7 to 57.1 (below the 63.7 no-context baseline), plus the incremental-vs-monolithic ablation (Table 18). No per-rewrite retention curve and no count of facts lost per round are reported.
- **Lesson for P49:** the failure mode to watch for across 2-3 compactions is not only gradual erosion but an abrupt collapse in one round, when the summariser is given a large existing summary plus new history and rewrites both. Log summary length per compaction index (tokens of summary n vs n-1) as a cheap collapse detector, and check whether a fact present in summary n-1 is absent from summary n even when nothing new contradicts it.
- **Design analogue:** ACE's fix (append-only itemised deltas, never re-rewrite earlier items) is the same idea as CliffCompaction's "never compact a compaction" and AgentFold's granular condensation. A P49 arm that carries the previous summary forward verbatim and summarises only the new segment would separate "re-summarisation loss" from "first-summarisation loss".
