# A Snapshot of Influence: A Local Data Attribution Framework for Online Reinforcement Learning
- Authors: Yuzheng Hu, Fan Wu, Haotian Ye, David Forsyth, James Zou, Nan Jiang, Jiaqi W. Ma, Han Zhao. arXiv 2505.19281 (v2 2025-10-04).
- URL: https://arxiv.org/abs/2505.19281
- Retrieved 2026-10-07 by an X7 external-review subagent. Reading depth: WebFetch tool summary only (a small model summarised the page; text below is that summary, not verbatim, unless inside quotation marks). Re-read the primary source before quoting numbers. (first 100,000 of 160,805 characters covered)

## Content (summary)
Round-level attribution of rollout-buffer records; TracIn adapted; two targets (log-prob of an action; REINFORCE-like return). Iterative Influence-Based Filtering (IIF) cuts rounds to match baseline by 20-67%; 4x speedup in an RLHF experiment on a 2.7B GPT-Neo. Environments: Acrobot, LunarLander, FrozenLake, MiniGrid, Highway, BipedalWalker. Insight: records with inaccurate advantage estimates harm most. Nothing about reward hacking or misalignment.
