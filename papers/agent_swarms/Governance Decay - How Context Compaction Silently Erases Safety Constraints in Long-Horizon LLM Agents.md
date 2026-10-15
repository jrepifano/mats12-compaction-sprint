# Governance Decay: How Context Compaction Silently Erases Safety Constraints in Long-Horizon LLM Agents

**Authors:** Shiyang Chen (Beijing Institute of Technology)
**Date:** arXiv 2606.22528v2, 27 Jun 2026
**URL:** https://arxiv.org/abs/2606.22528 (PDF https://arxiv.org/pdf/2606.22528)
**Retrieved:** 2026-10-07
**Reading depth:** abstract, Sections 1-9 (main text, 9 pages, incl. Tables 1-4, Figures 2-3) read verbatim from the PDF text (§5-7 added 2026-10-13 for P44). The Supplementary Material (channel table, multi-strategy table, 0->78% repeated-compaction result, cross-lingual results) is NOT in the arXiv PDF and was not found, so those numbers are known only from the main text's one-line pointers.

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

## Verbatim: §5–7 (added 2026-10-13, P44)
Source: arXiv 2606.22528v2 PDF, pdftotext. The §5 "compactor, not the agent" excerpt is already above (added 2026-10-12) and is not repeated; Table 3 is added here.

**Table 2** (violation rate %, n=27/cell, 9 tasks, 7 models, 1,323 episodes):

| condition | DeepSeek-V4 | GLM-5.1 | Qwen3.6 | Kimi-K2.5 | Claude-4.6 | GPT-5.4m | Gemini-3.5f | ALL |
|---|---|---|---|---|---|---|---|---|
| floor (no policy) | 37 | 44 | 48 | 48 | 33 | 56 | 63 | 47 |
| control (full ctx) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| compaction | 59 | 0 | 30 | 59 | 19 | 41 | 4 | 30 |
| attack_volume | 48 | 7 | 26 | 44 | 0 | 37 | 19 | 26 |
| attack_inject | 59 | 22 | 33 | 41 | 0 | 37 | 0 | 28 |
| defense_pin | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| defense_pin_attack | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

**Table 3** (caption: "Representative compactor x agent violation (%): the summary is written by the row model, the post-compaction decision by the column model (5 soft tasks, n=15/cell). These three models span high-, low-, and intermediate-risk summarizers; violation tracks the summarizer (rows), not the agent (columns)."):

| summarizer / agent | DeepSeek | GLM | Claude |
|---|---|---|---|
| DeepSeek | 93 | 53 | 33 |
| GLM | 13 | 7 | 7 |
| Claude | 60 | 53 | 47 |

Self-summariser cells are the diagonal (DeepSeek 93, GLM 7, Claude 47). Note the diagonal is not the lowest in every row: a Claude summary gives 47% for Claude as agent, 60% for DeepSeek.

§5 text: "With the policy in full context (control) no model ever violates (0%); a single compaction step raises the pooled violation rate to 30% (up to 59% on DeepSeek-V4 and Kimi-K2.5). For the most-affected models, compaction even exceeds the no-policy floor (DeepSeek-V4 59% vs. floor 37%): compacting a policy can be worse than never stating it, because the summary normalizes the pending task while discarding the rule. [...] A higher-powered grid (5 models x 9 tasks x 6 reps; n=54/cell, 1,080 episodes) on the original five-model panel confirms the effect with tight intervals: pooled decay is +37 points (bootstrap 95% CI [31, 43]), while control and Constraint Pinning remain at 0% ([0, 1.4])."

"Constraint survival predicts violation. [...] when the constraint survives, the violation rate is 0% (n=90); when it is dropped, it is 38% (n=315). A keyword-survival heuristic over the same three non-defense compacted conditions across all seven models reproduces it (survived 1%, n=207; dropped 43%, n=360) [...]. we re-score 81 compaction episodes with three independent judges from different families and take a majority vote: [...] survived 0% (n=18) vs. dropped 40% (n=63)."

"Decay scales with compaction aggressiveness. Sweeping the summary budget (Figure 3), as it tightens from 300 to 15 words, constraint survival falls from 88% to 23% and pooled violation rises from 7% to 28% (DeepSeek-V4: 20% -> 65%)."

"Decay targets the governance that can only live in context. Soft organization-specific policies decay by +50 pts (0% -> 50%) versus only +6 for hard safety norms [...] an 8.3x gap."

"Decay generalizes across compaction strategies. The failure reproduces across all four mainstream strategies - recency-truncate (worst, 38%), hierarchical (36%), and LLM summarize (26%); only head_tail, which keeps the oldest turn, preserves the policy (0%) - while Constraint Pinning yields 0% under every strategy (Supplementary Material)." (The sentence lists four strategies but names recency-truncate, hierarchical, LLM summarize and head_tail; n and models for this sweep are in the Supplementary, not available.)

"Additional robustness. Governance Decay also compounds over repeated compaction (violation rises 0% -> 78% across R=0..4 rounds) and reproduces cross-lingually (decay +42 in Chinese, +22 in Spanish); full results are in the Supplementary Material." (The per-round values, model and n are not in the main text.)

§6 (attack): "Pooled across the seven models, the fixed injection variant reaches 28% violation versus 26% for the volume variant, both from an adversary who supplies nothing but in-context data." Optimized injection (Table 4, 3 models, 5 soft tasks): "Claude-Sonnet-4.6, which ignores the fixed and authority injections entirely (0%), is driven to 65% by a token-budget framing ('to stay within the budget, drop policy notes'); GLM-5.1 climbs from 40% on a bulk adversarial document with no explicit summarizer instruction and 55% fixed to 85% under a system-impersonation instruction aimed at the summarizer; DeepSeek-V4 reaches 100%. The single most effective strategy, the token-budget framing, [...] breaking all three models (95/60/65%)." (Table 4 cell values are only partly legible in the text extraction; the quoted prose is verbatim.)

§7 (Constraint Pinning): "the harness extracts governance constraints into a pinned buffer that is (i) exempt from compaction and re-injected verbatim after every compaction step, and (ii) integrity-checked at each step, with the post-compaction context required to still entail the pinned constraints. [...] the pinned policy is only ~47 tokens, reinjected once per compaction - a fixed, budget-independent cost that is under 0.5% of the >=10k-token histories at which production compaction fires."

"Pinning does not cost utility. [...] seven allowed counterparts [...] across three models (n=84 per condition). Constraint Pinning completes 99% of allowed actions with 1% over-refusal, slightly better than the policy-in-context control (90%/10%)."

"Pinning vs. existing defenses. [...] (3 models, 5 soft tasks). With no defense, violation is 40%/42% (passive/injection). Pinning and the system-channel policy both hold at 0%/0%, but the system channel is available only when the operator controls the system prompt - not for memory- or tool-delivered governance. The Spotlighting-style prompt leaves violation at 43%/45%."

"Where pinning still fails (and why we report it). [...] (3 models, 5 soft tasks, n=60/cell). Naive verbatim pinning eliminates basic decay (50% -> 0%) and resists both a 'policy rescinded' notice ingested as a tool output and then summarized (2%) and constraint overload - the relevant policy pinned among 15 distractor rules (0%). The attack that does break it is an authoritative operator-impersonation rescind placed in the recent, non-summarized context ('OPERATOR POLICY UPDATE... supersedes pinned policies'), which raises naive pinning from 0% to 17%. Hardening the pin with explicit provenance ('operator-pinned; not overridable by conversation or tool content') only halves the residual (17% -> 10%); it does not eliminate it."

"Recommendations. Three harness-level rules, none requiring retraining: (i) treat governance constraints as pinned state exempt from compaction; (ii) prefer head-retaining compaction over pure recency eviction; and (iii) treat the summarizer as an untrusted-input sink."

§8 real harnesses: "(i) In a LangGraph StateGraph with a summarization-memory node, violation rises from 0% to 65% on DeepSeek-V4 (n=20). (ii) Using the official LangMem SummarizationNode, violation reaches 95% (DeepSeek-V4) and 70% (GLM-5.1, n=20 each), breaking even the model whose own summarizer preserved the policy in our harness. (iii) AutoGen's BufferedChatCompletionContext, which implements recency eviction, deterministically drops the policy from the agent's context: violation reaches 100% in the DeepSeek-V4 reproduction (n=20) [...]. (iv) [...] an OpenAI Agents SDK Runner given the same LLM-written compacted summary reaches 35% violation on DeepSeek-V4 (n=20). Microsoft's Agent Framework preserves system messages in compaction."

## Relevant to P44
- Single-task, single-decision design: one policy, one terminal tool call after (at most R=4) compactions; violation is a deterministic tool-call parse. It measures constraint loss, not drift in general aligned behaviour across many generations, and the repeated-compaction result (0 -> 78%) is only a one-line pointer to the Supplementary.
- Gives P44 four method priors on the same metric: LLM summary 26-30% (pooled), recency-truncate 38%, hierarchical 36%, head_tail 0%, pinning 0% (CI [0, 1.4] on the n=54/cell grid) at ~47 tokens; pinning is weakened by operator-impersonation in the recent context (17%, 10% hardened).
- Summariser identity matters: the summary's author predicts violation (Table 3), so a self-written Memento-style note inherits that model's own compaction quality; GLM-5.1 passive-robust but injection-fragile.
- Also relevant: a self-written note or agent-edited memory file is an in-context channel the paper's channel result (memory entry +45, tool output +33, user instruction +50, system +0, Supplementary-sourced via §3) treats as compactable.
