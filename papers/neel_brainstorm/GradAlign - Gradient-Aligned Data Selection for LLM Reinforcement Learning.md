# GradAlign: Gradient-Aligned Data Selection for LLM Reinforcement Learning
- Authors: Ningyuan Yang, Weihua Du, Weiwei Sun, Sean Welleck, Yiming Yang. arXiv 2602.21492 (2026-02-25). Code https://github.com/StigLidu/GradAlign
- URL: https://arxiv.org/abs/2602.21492
- Retrieved 2026-10-07 by an X7 external-review subagent. Reading depth: WebFetch tool summary only (a small model summarised the page; text below is that summary, not verbatim, unless inside quotation marks). Re-read the primary source before quoting numbers.

## Content (summary)
Ranks training PROBLEMS (prompts) by cosine similarity between their GRPO policy gradient and a validation-set gradient, re-computed periodically; selects top-ranked, then does GRPO. Theorems: GRPO gradient is an unbiased (binary reward) or directionally aligned (normalised advantage) estimator of the expected-accuracy gradient. Qwen2.5-1.5B-Math-Instruct, Qwen3-8B-Base. With 50% noisy rewards: 41.8% vs 26.6% for accuracy-greedy selection. Selects prompts, not individual rollouts; not framed as reward-hacking prevention.
