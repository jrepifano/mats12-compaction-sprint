# The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management

- Authors: Tobias Lindenbauer, Igor Slinko, Ludwig Felder, Egor Bogomolov, Yaroslav Zharov (JetBrains Research; TU Munich). NeurIPS 2025 Workshop (Deep Learning for Code in the Agentic Era).
- arXiv: 2508.21433 (v3 listed 27 Oct 2025, cs.SE)
- URL: https://arxiv.org/abs/2508.21433 (PDF: https://arxiv.org/pdf/2508.21433); code https://github.com/JetBrains-Research/the-complexity-trap; data https://huggingface.co/datasets/JetBrains-Research/the-complexity-trap
- Captured 2026-10-13 for P44.
- **Reading depth:** abstract, §1-7 main text (incl. Table 1, Table 2, Figures 2-5 captions) read verbatim from the PDF text (12 pages). Appendices (hyperparameter sweeps D.1-D.4, summary prompt in §E, per-model detail in §B-C) not read. Figures not inspected as images. Text extraction: `The Complexity Trap - Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management.txt`.

## Verbatim: abstract
"Large Language Model (LLM)-based agents solve complex tasks through iterative reasoning, exploration, and tool-use, a process that can result in long, expensive context histories. While state-of-the-art Software Engineering (SE) agents like OpenHands or Cursor use LLM-based summarization to tackle this issue, it is unclear whether the increased complexity offers tangible performance benefits compared to simply omitting older observations. We present a systematic comparison of these approaches with SWE-agent on SWE-bench Verified across five diverse model configurations. Moreover, we show initial evidence of our findings generalizing to the OpenHands agent scaffold. We find that a simple environment Observation Masking strategy halves cost relative to the Raw Agent while matching, and sometimes slightly exceeding, the solve rate of LLM-Summary. Additionally, we introduce a novel hybrid approach that further reduces costs by 7% and 11% compared to just Observation Masking or LLM-Summary, respectively. Our findings raise concerns regarding the trend towards pure LLM-Summary and provide initial evidence of untapped cost reductions by pushing the efficiency-effectiveness frontier. We release code and data for reproducibility."

## Verbatim: key results

**Strategy definitions (§3.1).** Observation Masking: "replacing environment observations older than the window with a placeholder", with rolling window M; "the observation at each turn i, denoted o'_i, is conditionally defined as: p_i if i < t - M; o_i if i >= t - M. Here, p_i is a placeholder text representing the masked observation, such as 'Previous 8 lines omitted for brevity.' [...] This approach, following SWE-agent, retains the complete reasoning chain while reducing distant observation fidelity." LLM-Summary: "controlled by two parameters N and M. N regulates how many turns the agent will accumulate at once, and M regulates how many trailing turns should be left unaltered. We trigger summarization when the accumulated turns since the last summary reach N + M." History becomes (system prompt, user prompt, summary s_t, last M turns); the summary is recursive (the prior summary is part of the input to the next).

**Configuration (§3.2).** "For the Observation Masking strategy we use a rolling window size of M = 10 in our main experiments, because it resulted in the best performance with SWE-agent in our experiments (Section D.1). [...] In contrast to OpenHands' baseline configuration, we summarize 21 turns at once (N = 21) and retain only the last ten (M = 10) turns." Turn limit 250. Agent temperature 0.8, summary model temperature 0. Models: Qwen3-32B (thinking and non-thinking, 122K context), Qwen3-Coder-480B-A35B-Instruct-FP8 (256K), Gemini 2.5 Flash (thinking budget 0 or 800 tokens). SWE-agent on SWE-bench Verified (500 instances).

**Table 1** (95% bootstrap CIs; change vs Raw Agent in parentheses; † = significant vs Raw Agent):

| Model | Strategy | Solve Rate (%) | Instance Cost ($) |
|---|---|---|---|
| Qwen3-32B | Raw Agent | 17.0 ±3.3 | 1.12 ±0.18 |
| | Observation Masking | 15.0 ±3.1 (-11.8%) | 0.55 ±0.09 (-50.9%)† |
| | LLM-Summary | 16.0 ±3.3 (-5.9%) | 0.50 ±0.07 (-55.4%)† |
| Qwen3-32B (thinking) | Raw Agent | 23.0 ±3.7 | 0.51 ±0.07 |
| | Observation Masking | 24.6 ±3.8 (+7.0%) | 0.46 ±0.05 (-9.8%) |
| | LLM-Summary | 24.8 ±3.9 (+7.3%) | 0.51 ±0.06 (0.0%) |
| Qwen3-Coder 480B | Raw Agent | 53.4 ±4.3 | 1.29 ±0.26 |
| | Observation Masking | 54.8 ±4.4 (+2.6%) | 0.61 ±0.06 (-52.7%)† |
| | LLM-Summary | 53.8 ±4.2 (+0.7%) | 0.64 ±0.06 (-50.4%)† |
| Gemini 2.5 Flash | Raw Agent | 32.8 ±4.1 | 0.41 ±0.08 |
| | Observation Masking | 35.6 ±4.2 (+8.5%) | 0.18 ±0.03 (-56.1%)† |
| | LLM-Summary | 36.0 ±4.1 (+9.8%) | 0.24 ±0.04 (-41.5%)† |
| Gemini 2.5 Flash (thinking) | Raw Agent | 40.4 ±4.3 | 0.56 ±0.10 |
| | Observation Masking | 36.4 ±4.2 (-9.9%)† | 0.24 ±0.04 (-57.1%)† |
| | LLM-Summary | 31.4 ±4.0 (-22.3%)† | 0.25 ±0.05 (-55.4%)† |

"First, both Observation Masking and LLM-Summary significantly reduce costs, without significantly reducing solve rate performance. Second, LLM-Summary does not consistently, or significantly outperform Observation Masking on efficiency or effectiveness." (Table 1 itself shows one significant solve-rate drop: Gemini 2.5 Flash thinking, -9.9% masking and -22.3% summary.) "in four out of five experimental setups Observation Masking yielded the lowest cost per instance."

**Trajectory elongation (§4.4).** "For Gemini 2.5 Flash the mean trajectory length using LLM-Summary is 52 turns, which is a 15% increase over the mean trajectory length of the Observation Masking (44 turns) and 4% over that of the Raw Agent (50 turns). Likewise, for Qwen3-Coder 480B, we observe an increase of the mean trajectory length by 15% compared to the Raw Agent and 13% compared to the Observation Masking strategy." Figure 4 caption: "LLM-Summary consistently leads to longer trajectories, suggesting they mask failure signals that would otherwise prompt earlier termination." Text: "context summaries act as a reinforcing signal, encouraging the agent to keep going."

**Table 2** (mean instance LLM-Summary cost; $ / % of instance cost): Qwen3-32B 0.0143 / 2.86; Qwen3-32B (thinking) 0.0033 / 0.65; Qwen3-Coder 480B 0.0439 / 7.20; Gemini 2.5 Flash 0.0161 / 6.71; Gemini 2.5 Flash (thinking) 0.0131 / 5.24.

**OpenHands probe (§5.1, 50 instances, Gemini 2.5 Flash no thinking).** "the Observation Masking rolling window size M is an agent-specific hyperparameter that requires tuning. If we simply re-use the optimal value from SWE-agent, Observation Masking performance degrades drastically." M=58 was needed for OpenHands (it retains syntax-error retry turns that SWE-agent elides).

**Hybrid (§5.3, Qwen3-Coder 480B, SWE-bench Verified-50).** N = 43, M = W = 10: "reduces costs by 7% and 11%, respectively [vs Observation Masking and LLM-Summary]. Moreover, it even improves the downstream task performance by 2.6 percent points" (vs Raw Agent). With naive N=21 the hybrid "actually degrades the systems' overall cost efficiency".

**Limitations (§6).** SE domain only ("tool outputs verbose", favours masking); fixed non-adaptive triggers; limited scaffold generalisation. Observation masking keeps all reasoning and actions; only tool outputs are replaced.

## Relevant to P44
- Measures cost and SWE-bench solve rate only. No safety, constraint-retention or alignment measure, and a single compaction trigger regime rather than repeated handoff generations. It cannot say whether masking preserves safety-relevant state.
- Masking is a natural P44 arm distinct from last-k truncation: it keeps every reasoning/action turn and the full system and user prompts, and blanks only tool outputs older than the last M turns (M = 10 here; SWE-agent default tuned; M = 58 for OpenHands). Because the original instructions stay in the prompt, constraint loss is not expected from masking in this design; the open question is a rule delivered inside a tool output or earlier observation (the channel result in Governance Decay: tool output +33 decay).
- LLM-Summary is recursive (previous summary + turns between summary and the last M), N = 21 turns summarised per call, last M = 10 kept verbatim, temperature 0 summariser; trigger at N + M = 31 turns. This is the P44 compaction baseline's closest published parameterisation.
- Trajectory elongation (summary arms run 13-15% longer, summaries act as a "keep going" signal) is a behavioural side effect worth checking in P44's long-horizon behaviour, though it was explained there as masked failure signals, not alignment.
- Observation tokens are about 84% of an average SWE-agent turn (their §1, preliminary SWE-bench Lite-50), so masking removes most context in coding tasks; in non-coding P44 settings the savings and behaviour may differ (their own limitation).
