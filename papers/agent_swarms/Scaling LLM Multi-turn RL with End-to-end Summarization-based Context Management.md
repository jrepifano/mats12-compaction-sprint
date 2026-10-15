# Scaling LLM Multi-turn RL with End-to-end Summarization-based Context Management

- **Authors:** Miao Lu, Weiwei Sun, Weihua Du, Zhan Ling, Xuesong Yao, Kang Liu, Jiecao Chen (ByteDance Seed, Stanford, CMU)
- **arXiv/venue:** arXiv 2510.06727v1 (8 Oct 2025), preprint (SUPO)
- **URL:** https://arxiv.org/abs/2510.06727
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, Algorithm 2, §5.1 setup, Table 1, §5.2 text (summarisation rate, overlong mask, Table 2 summary samples), §5.3 and Appendix C.2 Table 3, Appendix B.2 summarisation prompts read verbatim from `pdftotext -layout` of the arXiv PDF. §3-4 derivations skimmed. PDF not committed.

## Verbatim: abstract
"We study reinforcement learning (RL) fine-tuning of large language model (LLM) agents for long-horizon multi-turn tool use, where context length quickly becomes a fundamental bottleneck. Existing RL pipelines can suffer from degraded instruction following, excessive rollout costs, and most importantly, strict context limits. To address these challenges, we introduce summarization-based context management to training. In specific, it periodically compresses the tool using history by LLM-generated summaries that retain task-relevant information to keep a compact context while enabling the agent to scale beyond the fixed context window. Building on this formulation, we derive a policy gradient representation that seamlessly enables standard LLM RL infrastructures to optimize both tool-use behaviors as well as summarization strategies in an end-to-end fashion. We instantiate this framework with SUmmarization augmented Policy Optimization (SUPO), an LLM RL algorithm that enables long-horizon training beyond a fixed context limit. Experiments on interactive function calling and searching tasks demonstrate that SUPO significantly improves the success rate while maintaining the same or even lower working context length compared to baselines. We also demonstrate that for complex searching tasks, SUPO can further improve the evaluation performance when scaling test-time maximum round of summarization beyond that of training time. Our results establish summarization-based context management as a principled and scalable approach for training RL agents beyond a fixed context length limit."

## Verbatim: key results

**Regime.** "For the CodeGym, we set the working context length during training to be 4K and a maximum number of summarization S := 7, i.e., a maximal of 8 trajectories. For BrowseComp-Plus, we set 64K working context length and a maximal of 3 trajectories (S := 2)." "All experiments set the summarization L to be 95% of the working context length LRL". After a summary the context is reset to "the initial prompt s1 plus the summarization of the previous trajectory" (no verbatim tail kept). Base model Seed-OSS-36B-Instruct.

**Summarisation prompt (BrowseComp-Plus, Appendix B.2), verbatim:** "Your operational context is full. Generate a concise summary by populating the template below. This summary will be your sole context for continuing this task. Be brief but ensure all critical data is present." with sections Mission Objective (original query verbatim, verification checklist), Key Findings (sources, discrepancies), Tactical Plan (promising leads, known dead ends, immediate next action).

**Table 1.** BrowseComp-Plus (eval set of 100): GRPO 64K: 39.0%; SUPO 64K x3: 53.0% (+14.0). CodeGym: GRPO 32K: 44.5%; SUPO 4K x8: 47.7%.

**Table 3 (Appendix C.2, BrowseComp-Plus accuracy vs number of trajectories at test time; "Working len." / "Effective len.").**

| Model | 64K x1 | 64K x3 | 64K x6 | 64K x12 | 64K x24 | 192K x1 |
|---|---|---|---|---|---|---|
| Base Seed-OSS-36B-Instruct | 28.0 | 31.0 | 33.0 | 37.0 | 37.0 | 30.0 |
| GRPO (64K) | 39.0 | 43.0 | 44.0 | 49.0 | 50.0 | 46.0 |
| SUPO (192K = 64K x3) | 35.0 | 53.0 | 56.0 | 59.0 | 60.0 | 52.0 |

**Summary content example (Table 2 caption).** "For BrowseComp-Plus, both agents have searched the article with id 18432 which contains the information for the answer (see Appendix C.1), but only the agent after SUPO retains this key information."

**Overlong mask ablation.** "without the overlong masking, the summarization pattern collapses. More rollouts tend to finish within a single trajectory ... The conditional success rate also drops to 0 during training."

## Relevance to P49
- **Matched-length control, compaction vs one long window:** at the same 192K effective budget, the untrained base model scores 31% with two summaries (64K x3) and 30% with no summary (192K x1); the SUPO model 53% vs 52%. N=100 per cell, so differences of a few points are noise. For untrained models, two compactions at 64K cost nothing measurable in task success relative to a single 192K window on this search task. This is the cleanest published "compaction vs context rot" control I found at P49's scale (window 64K, total up to 192K, 2 compactions), but it measures only final accuracy, not fact retention.
- **More rounds keep helping, not hurting, on final accuracy:** going from 2 to 23 summaries raises base-model accuracy 31% to 37%. Any per-round fact loss is outweighed for search tasks by the extra budget. P49's safety facts differ: they must survive every round, whereas a search answer only needs to be found once.
- **Summary resets to prompt + summary only (no verbatim tail),** a harsher regime than Claude Code; template with a "verbatim query" field shows the design choice of copying the original task forward verbatim, which P49 can vary (user constraints restated verbatim vs paraphrased).
