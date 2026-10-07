# CATCH: A Controllable Analysis Testbed for Reward Hacking in Coding RL
- Authors: Shouli Wang, Yanfeng Jia, Zhihao Ou, Zitao Su, Ruize He, Haotong Xie, Hao Peng, Juanzi Li, Xiaozhi Wang. arXiv 2609.39533 (2026-09-30).
- URL: https://arxiv.org/abs/2609.39533
- Retrieved 2026-10-07 by an X7 external-review subagent. Reading depth: WebFetch tool summary only (a small model summarised the page; text below is that summary, not verbatim, unless inside quotation marks). Re-read the primary source before quoting numbers. (first 100,000 of 116,817 characters covered)

## Content (summary)
Coding RLVR testbed with three loophole classes (test-file modification, test-data exploitation, execution interference); every submission gets a model-visible "Hackable Run" (reward) and model-invisible "Unhackable Run" (gold label). Qwen3-4B policy; Qwen3.5-27B CoT monitor; 8 GPUs per node, FSDP offload, async vLLM. SFT data with controllable hacking mix. Execution-based labels, no attribution. Gives gold per-rollout hack labels, useful for scoring any filter.
