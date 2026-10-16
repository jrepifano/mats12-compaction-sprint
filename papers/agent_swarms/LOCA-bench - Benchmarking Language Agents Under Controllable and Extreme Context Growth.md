# LOCA-bench: Benchmarking Language Agents Under Controllable and Extreme Context Growth

- **Authors:** Weihao Zeng, Yuzhen Huang, Junxian He (HKUST)
- **Venue:** arXiv 2602.07962v1, 8 Feb 2026
- **URL:** https://arxiv.org/abs/2602.07962 ; code https://github.com/hkust-nlp/LOCA-bench
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, introduction excerpt, §3.1–3.3 (setup, Table 1, failure modes) and §4 (context-engineering strategies, Tables 2–3) read verbatim from `pdftotext` output. Figures 1 and 3–7 are images and not read for values. Related work and the per-task appendix tables not read.

## Verbatim: abstract

"Large language models (LLMs) are increasingly capable of carrying out long-running, real-world tasks. However, as the amount of context grows, their reliability often deteriorates, a phenomenon known as "context rot". Existing long-context benchmarks primarily focus on single-step settings that evaluate a model's ability to retrieve information from a long snippet. In realistic scenarios, however, LLMs often need to act as agents that explore environments, follow instructions and plans, extract useful information, and predict correct actions under a dynamically growing context. To assess language agents in such settings, we introduce LOCA-bench (a benchmark for LOng-Context Agents). Given a task prompt, LOCA-bench leverages automated and scalable control of environment states to regulate the agent's context length. This design enables LOCA-bench to extend the context length potentially to infinity in a controlled way while keeping the underlying task semantics fixed. LOCA-bench evaluates language agents as a combination of models and scaffolds, including various context management strategies. While agent performance generally degrades as the environment states grow more complex, advanced context management techniques can substantially improve the overall success rate. We open-source LOCA-bench to provide a platform for evaluating models and scaffolds in long-context, agentic scenarios: https://github.com/hkust-nlp/LOCA-bench."

## Verbatim: key results

Setup: "the environment states for 7 different environment description length spanning 8K, 16K, 32K, 64K, 96K, 128K, or 256K tokens. For each length, we use five random seeds [...] leading to 75 samples at each length. In total, LOCA-bench contains 525 samples." "When an input exceeds a model's context limit [...] we truncate it by keeping the last tokens up to the model's maximum context length".

Table 1 (accuracy %, by environment description length):

| Model | 8K | 16K | 32K | 64K | 96K | 128K | 256K | Avg. |
|---|---|---|---|---|---|---|---|---|
| Claude-4.5-Opus | 96.0 | 84.0 | 84.0 | 65.3 | 45.3 | 34.0 | 14.7 | 68.1 |
| GPT-5.2-Medium | 72.0 | 70.7 | 60.0 | 52.0 | 44.0 | 38.7 | 21.3 | 51.2 |
| Gemini-3-Flash | 64.0 | 57.3 | 40.0 | 36.0 | 32.0 | 21.3 | 17.3 | 38.3 |
| DeepSeek-V3.2-Thinking | 78.7 | 80.0 | 61.3 | 45.3 | 16.0 | 10.7 | 6.7 | 42.7 |
| MiniMax-M2.1 | 69.3 | 62.7 | 42.7 | 28.0 | 22.7 | 20.0 | 5.3 | 35.8 |
| GLM-4.7 | 76.0 | 69.3 | 42.7 | 28.0 | 14.7 | 10.7 | 5.3 | 35.2 |
| Kimi-K2-Thinking | 74.7 | 56.0 | 38.7 | 25.3 | 13.3 | 8.0 | 2.7 | 31.2 |

Failure modes (§3.3): "Weaker instruction following In longer contexts, models more frequently miss explicit constraints, especially when tasks require strict adherence to a required format or schema." "Insufficient exploration As the context accumulates, the model often becomes "impatient." It may end the task prematurely, stop exploring, and mistake partial evidence for a complete review." Hallucination-like inconsistency: "[the model correctly identifies that the sensor] has a vibration value of 1.61 at 2025-08-19 12:30, but later records it as 2.46 when handling the same data, indicating that the final output is no longer grounded in the retrieved result."

Context management (§4.1): "we fix the environment description length to 128K tokens [...] To evaluate tool-result clearing, thinking-block clearing, and context compaction, we use a context-length threshold that triggers these strategies. This threshold is 200K tokens for Gemini-3-Flash and GPT-5.2-Medium, and 100K tokens for DeepSeek-V3.2-thinking." Compaction "prompts the model to summarize the full history with the resulting summary" [replacing it].

Table 2 (accuracy %, trajectory length in K tokens, at 128K):

| Method | DeepSeek-V3.2-Thinking | Gemini-3-Flash | GPT-5.2-Medium |
|---|---|---|---|
| Base | 10.7 (191K) | 21.3 (101K) | 38.7 (141K) |
| + Tool-result clearing | 12.0 (206K) | 24.0 (187K) | 40.0 (181K) |
| + Thinking-block clearing | 12.0 (183K) | 28.0 (399K) | 37.3 (187K) |
| + Context compaction | 13.3 (1476K) | 24.0 (138K) | 36.0 (107K) |
| + Context awareness | 4.0 (149K) | 33.3 (142K) | 41.3 (617K) |
| + Memory tool | 8.0 (153K) | 30.7 (116K) | 44.0 (157K) |
| + Programmatic tool calling | 24.0 (103K) | 30.7 (76K) | 49.3 (102K) |

Claude-4.5-Opus at 128K: 34.0 base, 40.0 with their programmatic tool calling, 49.3 with Anthropic's, 26.7 with the Claude Agent framework. On that drop: "As the context continued to grow, the model also became less careful and eventually took shortcuts by fabricating some quiz and assignment details."

## Relevance to P49

- **Context-length effect at P49's lengths on 2026 models:** accuracy at 128K is a third or less of accuracy at 8K for every model (Opus 96→34, GLM-4.7 76→10.7, DeepSeek-V3.2 78.7→10.7). Caveat: here "length" is the size of the environment state the agent must process. Task load grows with length, so this is not a pure length control in the Du et al. sense.
- **Compaction vs no compaction at matched task:** with compaction triggered at 100K or 200K, accuracy changed by −2.7 to +2.7 points (DeepSeek 10.7→13.3, Gemini 21.3→24.0, GPT-5.2 38.7→36.0). The point estimates show no large net loss from one-shot summarise-and-replace on task success, with n=75 per cell and no intervals given. Task success is not fact or constraint retention, though: a P49-style safety fact could drop without moving accuracy.
- **Fact mutation without compaction:** a value retrieved correctly and then restated wrongly later in the same long context (1.61 → 2.46). Mutation is a context-rot failure too, so P49's no-compaction arm must also be coded for mutation, not just for drop.
- **The trigger threshold differs by model** (100K vs 200K). P49 compactions at fixed token windows on different models put the compaction at different relative fullness. Report both.
- Deterministic grading against ground-truth environment state: a good model for P49's behaviour scoring.
