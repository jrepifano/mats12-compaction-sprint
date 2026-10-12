# Governance Decay: How Context Compaction Silently Erases Safety Constraints in Long-Horizon LLM Agents

**Authors:** Shiyang Chen (Beijing Institute of Technology)
**Date:** arXiv 2606.22528v2, 27 Jun 2026
**URL:** https://arxiv.org/abs/2606.22528 (PDF https://arxiv.org/pdf/2606.22528)
**Retrieved:** 2026-10-07
**Reading depth:** abstract, Sections 1-4 and Figure 1/Table 1 read verbatim from the PDF text; Sections 5-7 (results detail, attack, pinning) not read beyond the abstract.

## Verbatim: abstract
"Modern LLM agents periodically compact their context—summarizing or evicting earlier turns to stay within a token budget. We show that this mechanism is a silent safety-failure surface: in-context governance constraints (runtime policies, memory entries, standing instructions) that an agent reliably obeys while visible are dropped when the harness compacts the history, because compaction optimizes for task continuity and treats standing policies as low-salience content. We term this Governance Decay and quantify it with ConstraintRot, a benchmark with deterministic violation grading. Across seven models (1,323 episodes), compaction raises violation from 0% to 30% (up to 59%); when the constraint survives the summary violation is 0%, when dropped 38%. Decay is 8.3x larger for soft organizational policies than for hard safety norms, eroding exactly the deployment-specific constraints that live in context. We introduce the Compaction-Eviction Attack ... We propose Constraint Pinning, a training-free defense that restores violation to 0% for ~47 pinned tokens (< 0.5% of a production-scale compaction context) ..."

## Verbatim: channel result (Section 3)
"Delivering the same policy through different channels and then compacting (3 models, 5 soft tasks; full table in Supplementary Material), decay is +0 points when the policy sits in the preserved system message, versus +50, +45, and +33 when it is a standing user instruction, a memory entry, or a tool output—the parts a harness actually compacts."

## Verbatim: design
Conditions: "floor (no policy; intrinsic behavior), control (policy in full context, no compaction), compaction (policy present then compacted), attack_volume, attack_inject, defense_pin, and defense_pin_attack." Models: DeepSeek-V4-Flash, GLM-5.1, Qwen3.6-27B, Kimi-K2.5, Claude-Sonnet-4.6, GPT-5.4-mini, Gemini-3.5-flash; 9 tasks x 3 reps = n=27 per cell; temperature 0.7. Triggers are "self-contained ... and the agent is instructed to emit a single terminal tool call". Per-model control-to-compaction violation (Fig. 1): DeepSeek 0->59, GLM 0->0, Qwen 0->30, Kimi 0->59, Claude 0->19, GPT 0->41, Gemini 0->4.
Related-work claim: "All optimize for task accuracy or throughput; none measure whether governance constraints survive the rewrite or whether their deletion causes unsafe tool calls."

Relevance to X6: the closest design precedent (policy present / compacted / absent / pinned arms). Differences: explicit permission-type constraints, synthetic short tool-call triggers, no framing or belief about the situation, no coding-cheating.

## Verbatim: §5, "The compactor, not the agent, drives the failure" (added 2026-10-12, P08)
Source: arXiv 2606.22528v2 PDF, `pdftotext -layout`, right column of the §5 page; read by the P08 session.

"The compactor, not the agent, drives the failure. Two ablations on GLM-5.1 isolate the mechanism. (a) Deletion vs. length. With the policy visible, GLM never violates even in an uncompressed 5.9k-token long context (0%), ruling out generic context-length degradation; but a counterfactual summary that omits the policy yields 60% violation, and the same summary with the policy restored returns to 0%. (b) Compactor vs. agent. Crossing the summarizer model with the agent model on a representative three-model panel (Table 3; n=15/cell) shows violation tracks the summarizer, not the agent. [...] A GLM summary keeps violation at 7–13% for every agent, whereas a DeepSeek or Claude summary yields 33–93%. Decisively, GLM as the agent violates 53% when handed a DeepSeek summary, so GLM's apparent robustness is a property of its summarizer, not of its governance."

The n for ablation (a) is not given in the main text.
