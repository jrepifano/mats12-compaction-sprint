# Learning from the Right Rollouts: Data Attribution for PPO-based LLM Post-Training (I-PPO)
- Authors: Dong Shu, Denghui Zhang, Jessica Hullman. arXiv 2604.01597 (v2 2026-08-24).
- URL: https://arxiv.org/abs/2604.01597 (HTML https://arxiv.org/html/2604.01597v2)
- Retrieved 2026-10-07 by an X7 external-review subagent. Reading depth: WebFetch tool summary only (a small model summarised the page; text below is that summary, not verbatim, unless inside quotation marks). Re-read the primary source before quoting numbers. (first 100,000 of 113,017 characters covered)

## Content (summary)
Score(z_i) = g_train(i) . gbar_val; g_train is the per-episode PPO loss gradient, gbar_val the SFT cross-entropy gradient on human-preferred CoT validation completions (or 10% of train). Episodes with negative score are removed, the rest reweighted by score. Models Rho-1B, Gemma-2-2B, Qwen2.5-3B, Phi-3-4B, LLaMA-3-8B; GSM8K, CollegeMath, MATH, OlympiadBench, ECQA. 2 A100 (1-2B) or 4 A100 (3-8B). About 0.12 s per episode for influence. Beats PPO/SFT in 36/45 settings (p<0.05); e.g. Rho-1B GSM8K exact match 40.26 to 46.10. Does NOT study reward hacking; qualitative analysis says negative-influence episodes have more unfaithful reasoning. Stated limit: sensitive to validation-set quality; batch coupling in clipped PPO.
