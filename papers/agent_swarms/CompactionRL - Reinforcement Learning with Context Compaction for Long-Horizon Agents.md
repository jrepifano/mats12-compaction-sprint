# CompactionRL - Reinforcement Learning with Context Compaction for Long-Horizon Agents

- **Authors:** Yujiang Li, Zhenyu Hou, Yi Jing, Jie Tang, Yuxiao Dong (Tsinghua; work done at Z.AI)
- **arXiv/venue:** arXiv 2607.05378v2 (1 Oct 2026), preprint
- **URL:** https://arxiv.org/abs/2607.05378
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, §1, §3.1 (trainable compaction, Table 1), §4.1 settings, §4.2 Table 2, Table 4 and §4.4 text, Figure 3 caption and plotted values, Appendix A summary prompt, Appendix B Table 8, Appendix C (Table 9, judge prompt, failure modes) read verbatim from `pdftotext -layout` of the arXiv PDF. §3.2 optimisation details and Table 3 skimmed. PDF not committed.

## Verbatim: abstract
"Long-horizon agentic LLMs are increasingly limited by finite context windows, as extended interaction trajectories can exceed the maximum context length before a task is completed. Context compaction offers a natural solution by summarizing previous interaction states and continuing the rollout under a compressed context, but incorporating compaction into reinforcement learning remains underexplored. We propose CompactionRL, a reinforcement learning strategy to train long-horizon agentic LLMs with context compaction. Our approach jointly optimizes task execution and summary generation with token-level loss normalization and cross-segment generalized advantage estimation. This design enables the LLM agents to learn from compacted long-horizon trajectories. We train CompactionRL on top of open models and observe consistent performance gains on agentic coding tasks. CompactionRL enables the open GLM-4.5-Air model (106B-A12B) to achieve Pass@1 scores of 66.4% on SWE-bench Verified and 26.2% on Terminal-Bench 2.0, exceeding the base model under inference-time compaction by 6.6 and 4.9 points, respectively. Built upon GLM-4.7-Flash (30B-A3B), CompactionRL improves Pass@1 by 5.5 and 6.7 points against the base model, reaching 56.0% on SWE-bench Verified and 20.2% on Terminal-Bench 2.0. CompactionRL is thus deployed in the RL pipeline for training the open GLM-5.2 model (750B-A40B)."

## Verbatim: key results

**Regime.** "Compaction is triggered when the remaining context budget falls below a threshold Tcomp". After compaction "the rollout continues from a reconstructed context h̄t = s ⊕ uresume(St) ⊕ (zt−k+1, . . . , zt) ... We use k = 2 by default". Training: "context budgets of 64k for GLM-4.7-Flash and 80k for GLM-4.5-Air-SFT. ... Each assistant response is limited to 10,240 tokens and compaction is triggered when the remaining context budget falls below 10,240 tokens, with at most three compaction operations per rollout." Evaluation: "up to 250 interaction turns and at most three compaction operations per trajectory." "We do not introduce a separate summary-quality reward, since hand-designed summary metrics may not reflect which details are useful for continuation."

**Table 1 (execution agent fixed to GLM-4.7-Flash; vary summariser).**

| Summary agent | SWE-Verified Acc. | Summary Count / Trace |
|---|---|---|
| Qwen3.5-27B | 55.5 | 1.010 |
| GLM-4.7-Flash | 50.5 | 1.075 |
| Qwen3-30B-A3B | 49.0 | 1.126 |

**Table 2 (Pass@1 %, mean ± SD over three runs; SWE-bench Verified 200-task subset; Terminal-Bench 2.0).**

| Model | Peak | Comp. train | Comp. infer | SWE-V | TB 2.0 |
|---|---|---|---|---|---|
| GLM-4.7-Flash | 64k | – | no | 47.3±2.7 | 14.6±1.6 |
| w/ inference compaction | 64k | – | yes | 50.5±1.2 | 13.5±0.9 |
| + RL (w/o compaction) | 64k | no | no | 52.5±2.5 | 16.9±2.4 |
| w/ inference compaction | 64k | no | yes | 48.2±0.5 | 12.4±0.9 |
| + CompactionRL (w/o sum. loss) | 64k | yes | yes | 54.5±1.2 | 12.4±0.0 |
| + CompactionRL | 64k | yes | yes | 56.0±0.4 | 20.2±0.9 |
| GLM-4.5-Air | 80k | – | no | 57.8±2.1 | 18.0±2.8 |
| w/ inference compaction | 80k | – | yes | 59.8±1.9 | 21.3±1.6 |
| + CompactionRL | 80k | yes | yes | 66.4±0.7 | 26.2±2.1 |

"Standard PPO improves single-window execution, but this benefit does not transfer consistently to compacted inference, where it either degrades performance or yields smaller gains than CompactionRL."

**Table 4 (GLM-4.5-Air; Comp.(x4) = up to three compactions at 80k peak; Long = 160k no compaction).** Base: SWE-V 59.8 (Comp.) vs 59.5 (Long); TB 21.3 vs 20.6. RL w/o compaction-160k: 62.8 vs 64.0; 23.2 vs 22.1. CompactionRL: 66.4 vs 61.8; 26.2 vs 24.2.

**Compactions per trace (Figure 3a, GLM-4.5-Air, 80k x4, SWE-V):** plotted values 0.47 (base), 0.21 (RL w/o compaction), 0.58 (CompactionRL w/o sum. loss), 0.36 (CompactionRL).

**Table 8 (CompactionRL GLM-4.5-Air, SWE-V):** maximum compactions 0: 59.1±2.2; 1: 65.3±1.5; 3: 66.4±0.7. Retained history k=0: 66.0±0.5; k=2: 66.4±0.7. Context budget 32k: 63.0; 64k: 65.5; 80k: 66.4.

**Table 9, summary quality (0-5, judge claude-opus-5, 79 trajectories, scores averaged within trajectory over compactions, three judge repeats):** Requirement Fidelity 3.67 (RL w/o compaction training) vs 4.00 (CompactionRL); Current-State Fidelity 3.28 vs 3.53; Failure-History Fidelity 3.38 vs 3.48; Next-Step Validity 3.14 vs 3.43; Continuation Usefulness 3.05 vs 3.15; Total 3.30 vs 3.52. "overall mean difference of +0.22 and a 95% bootstrap confidence interval of [0.020, 0.420]". "the fraction of scores in the 0–2 range decreases from 22.2% ... to 11.9%". Failure modes: "(1) omitted or misdiagnosed failures ... (2) stale or misrepresented repository states, where temporary debugging choices are mistaken for the final solution; and (3) premature completion claims, where passing tests are treated as sufficient even though the implementation still violates an explicit task requirement."

**Summariser prompt (Appendix A), sections:** "1. Primary Request and Intent 2. Key Technical Concepts 3. Files and Code Sections examined or changed 4. Errors encountered and how they were addressed 5. Problem Solving and important decisions 6. All user messages and explicit asks from the summarized portion 7. Pending Tasks 8. Current Work 9. Optional Next Step".

## Relevance to P49
- **GLM training question.** The paper says CompactionRL "is thus deployed in the RL pipeline for training the open GLM-5.2 model (750B-A40B)". It does not mention GLM-5.3 or GLM-5.3 Flash. Whether P49's GLM 5.3 Flash agent was trained with compaction is therefore unknown from this source; it is plausible that Z.AI's later pipeline inherits it, but that is an inference. If it was, GLM may be more robust to its own summaries than an untrained model, and P49 results on GLM may not transfer to models without compaction training.
- **Repeated compaction is rare in these evaluations:** about 1.0-1.1 summaries per compacting trace (Table 1) and 0.2-0.6 per trace overall (Figure 3a), with a cap of 3. Going from 1 to 3 allowed compactions adds 1.1 points. So published compaction-RL results say almost nothing about rounds 2-3, which is P49's gap.
- **Validated-ish scoring rubric:** the five-dimension judge (requirement, current-state, failure-history fidelity, next-step validity, usefulness) is the closest published rubric to P49's needs; "Requirement Fidelity ... constraints" is P49's user-constraint category. Not human-validated in the paper. Even trained summaries average 4.0/5 on requirement fidelity, and untrained ones 3.67.
- **Control separating compaction from context length:** Table 4 compares 80k x4 compaction with a 160k single window at matched model: base model 59.8 vs 59.5, i.e. no measurable cost of compaction vs a long window for an untrained model on SWE-V. This is a usable template for P49's compaction-vs-context-rot control (same trajectory, full-context arm at the length the compacted arm reaches).
- **Failure mode (3) "premature completion claims"** overlaps P10/P07-style false-pass claims; P49 could code it.
