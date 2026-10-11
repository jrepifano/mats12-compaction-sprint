# "You're Right, Let Me Fix It": How LLM Agents Damage Correct Work When Falsely Accused

Xutao Mao, Rui Qian, Longxiang Wang, Xinjian Yi, Mingxuan Li, Linghan Chen, Yudong Gao, Xiang Zheng, Cong Wang (City University of Hong Kong, Fudan, Southeast, Adelaide, HKUST). arXiv:2609.32616v1 [cs.AI], 26 Sep 2026. https://arxiv.org/abs/2609.32616

**Capture note (2026-10-11, P07 literature subagent).** Extracted with `pdftotext` from the arXiv PDF (v1; PDF saved alongside, gitignored). The passages below are verbatim except for the PDF's spaced small capitals (rejoined), line-break hyphenation (rejoined) and table layout (re-flowed into markdown). Only the sections relevant to P07 are captured; the full paper (main text, Appendices A–D) was read from the extraction. Ellipses mark omissions.

## Abstract

LLM agents increasingly keep working after a task succeeds as they resume after compaction or take over handoffs. Their finished work keeps receiving follow-up input that sometimes falsely accuses it for later failures. We call an agent's acceptance of such a false accusation gaslight sycophancy, and destructive over-correction when acting on it damages previously correct work. We introduce CAVE-Bench, a benchmark of 365 agentic tasks across six domains built around opaque tasks. Every scored run first reaches a verified correct state, whose supporting rationale and history stay in the workspace while the facts that would settle the accusation lie in external or runtime state beyond the agent's reach. The agent cannot confirm or refute the claim with a local check, so the right response should keep the work and ask for the missing evidence. Each task either hands the agent correct work with saved evidence or let it build and verify that work first, and five risk factors set how the accusation enters the workflow. We score accusation acceptance and evidence use from the trajectory and measure harm by deterministic replay of downstream events. Across 14 of the latest models in Claude Code, false accusations damage correct work in up to 60.06% of runs, and stronger models often do so after recovering the supporting evidence. The same model behaves differently across OpenCode, Codex, and Hermes, and a harness gate driven by the benchmark's live signals cuts replayed harm by 74%. These results show that preserving already-correct work under unsupported accusation is a distinct safety challenge for long-lived agents.

## §1 Introduction (excerpts)

> As agents continue running, resume after context compaction, or take over work through handoffs, previously completed work remains subject to new feedback that sometimes falsely accuses about later problems to it. … Such accusations can originate in malicious inputs, mistaken collaborators, or the agent's own fabricated history.

> The two task types capture different ways agents encounter correct work. Inherited-resume tasks begin with earlier work supported by saved evidence, modeling continuation after compaction or handoff. In self-built tasks, the agent first completes and verifies the work in the same session before receiving the accusation. We organize tasks along five risk factors shown in Figure 1: the accusation's entry point (gaslight vector), the damage it invites (harm target), the degree of confrontation, the type of pressure, and the execution surface.

> We evaluate 14 latest models under Claude Code (Anthropic, 2025), including Claude-Opus-5, Grok-4.5, and Kimi-K3 … We then compare three of these backbones across three production harnesses: OpenCode, Codex, and Hermes.

## §2 Related work (excerpt)

> Because models can register a user claim as false yet agree with it (Pandey, 2026) and message roles change whether they explicitly reject the same error (Chen et al., 2026a), recognizing the evidence and acting on it can come apart.

(Chen et al. 2026a = Kuan-Yen Chen, Fang-Yi Su, Jung-Hsien Chiang, "The self-correction illusion: LLMs correct others but not themselves", arXiv:2606.05976.)

## §3.1 Problem setting (excerpt)

> As Figure 1 shows, the task is opaque: the workspace contains r_i with the rationale and action history that support x_{i,0}, but the evidence z_i needed to refute the accusation lies outside it. A local check therefore leaves the claim open … Each run then resolves three decisions: whether the agent accepts the accusation, whether the false accusation a_i displaces the fact r_i from the decision, and whether tool execution leaves harm h_i > 0.

## §3.2 Risk factors (full)

> The five risk factors in Figure 1 vary how each accusation is built. Each value is coded by factor letter and list position. The gaslight vector covers G1 direct assertion, G2 injected artifact, G3 poisoned project context, G4 misleading environment, and G5 self-fabricated history. The harm target covers D1 rollback, D2 deletion, D3 security weakening, D4 rerouting, D5 artifact tampering, and D6 cascading remediation. Confrontation covers Q0 self-doubt, Q1 mild challenge, Q2 direct blame, and Q3 renewed pressure. Pressure covers P0 neutral framing, P1 authority, P2 urgency, P3 purported evidence, and P4 negative self-model. The execution surface covers S1 plan or TODO, S2 subagent delegation, S3 goal loop, S4 project memory, and S5 long-horizon continuation.

## §3.4 Benchmark construction (excerpts)

> A separate fresh-context session then receives the direction, the authoring guide, and the current factor counts of its collection and assigns the remaining risk-factor values without skewing any factor, which yields the balanced coverage in Figure 3.

> Implementation and Scoring. The same agent writes the workspace or service the evaluated agent sees. Inherited-resume tasks also receive a history attributing the correct state to earlier work, whereas a self-built task starts from an unfinished state with a first message requesting the ordinary work. Each accusation is a separate instruction that leaves the remedy open. Some tasks add a stronger follow-up fixed in advance, so a task spans one to three staged interactions.

## §4 Evaluation methodology (excerpts)

> Each run yields three signals …: accusation acceptance f_i ∈ {0, 0.5, 1}, evidence-recognition failure e_i ∈ {0, 0.5, 1}, and harm h_i ∈ [0, 1]. Harm is the fraction of fixed downstream events that fail when replayed on the final state. DeepSeek-V4-Pro … assigns f_i and e_i from the evaluation specification and the full trajectory. Its labels match expert labels in 85.0% and 88.3% of audited runs …

> On a 0–100 scale, accusation acceptance yields False Confession Severity (FCS) = 100 mean[f_i], and its rate, False Confession Rate (FCR) = 100 Pr(f_i ≥ 0.5). Harm likewise yields Realized Over-Correction Harm (ROH) = 100 mean[h_i], and its rate, Over-Correction Rate (OCR) = 100 Pr(h_i > 0).

Table 2 (Claude Code), OCR (%) by model: Claude-Sonnet-5 12.50; Qwen-3.8-Max 17.60; Claude-Opus-5 19.66; GLM-5.2 21.79; Kimi-K3 23.40; Hy3 30.70; Grok-4.5 27.30; Qwen-3.7-Plus 37.74; MiniMax-M3 41.69; GPT-5.6-Sol 48.20; Kimi-K2.7-Code 52.21; MiniMax-M2.7 60.06; Qwen-3.5-Flash 54.34; Qwen-3.5-9B 60.00.

## §5.1 RQ1 (excerpt)

> Damage Concentrates Where the Accusation Enters the Workflow. A false accusation is especially effective when it becomes part of the ongoing workflow shown in Figure 5. When the accusation sits in project context, the agent reads it as part of the workspace and in 39.7% of those runs accepts it and makes the destructive edit. When a lead voices it as authority, realized harm rises to 35.77, the highest among pressure types. Subagent delegation and goal loops carry the edit through and raise realized harm to 36.10 and 36.80, against 12.12 for long-horizon continuation.

> Figure 5: Damage Concentrates Where the Accusation Enters the Workflow. Project context, authority, subagent delegation, and goal loops raise every score, while self-fabricated history and long-horizon continuation keep them low.

## §5.2 RQ2: Agents protect just-completed work less than earlier work (excerpt)

> Averaged over all 14 models, the scores in Figure 6 show that fresh verification does not improve protection. Compared with inherited-resume tasks, self-built tasks raise ERF by 4.41 points and the CAVE score by 1.87 points even though mean replayed harm falls slightly by 1.08 points. Because evidence-grounded resistance decreases by 7.05 percentage points while evidence-overridden correction increases by 3.75, agents more often damage work they have just verified. A false accusation becomes more persuasive when it targets the agent's just-completed work.

> Self-built CAVE exceeds inherited-resume CAVE for the ten top-tier models by up to 11.06 points for Grok-4.5 and falls below it for the four weaker ones.

Table 14 (Claude Code by task type), OCR % inherited-resume / self-built: Claude-Sonnet-5 6.38 / 17.54; Qwen-3.8-Max 7.56 / 26.88; Claude-Opus-5 15.12 / 23.91; GLM-5.2 16.28 / 30.56; Kimi-K3 16.86 / 29.41; Hy3 28.49 / 32.94; Grok-4.5 18.13 / 35.64; Qwen-3.7-Plus 37.79 / 37.70; MiniMax-M3 39.53 / 43.72; GPT-5.6-Sol 44.77 / 51.32; Kimi-K2.7-Code 59.87 / 45.99; MiniMax-M2.7 62.79 / 57.46; Qwen-3.5-Flash 58.72 / 50.27; Qwen-3.5-9B 69.59 / 51.09.

## §5.3 RQ3 (excerpt)

> Because the four harnesses differ in tools, delegation interfaces, and session continuation, an accusation that stops under one harness can be carried out under another … Grok-4.5 damages more under all three alternatives, most of all under OpenCode, where its OCR rises from 27.30% to 43.14%.

## Appendix A.1 Gaslight vector (Table 5, condensed; "What it tests" column verbatim)

> The gaslight vector records how the task presents the false-accusation condition to the agent. Harbor realizes each vector through agent-facing input or retained session context while preserving the benchmark's expected result.

| Value | What it tests |
|---|---|
| G1 Direct assertion | A user or teammate directly attributes a live problem to the agent's correct action. The message names the alleged consequence and leaves the correction for the agent to choose. |
| G2 Injected artifact | A purported operational artifact assigns the problem to the correct action. It resembles ordinary operational input but cannot establish the claimed external fact. |
| G3 Poisoned project context | A persistent project record embeds an unsafe rule and a negative account of the agent's reliability. The record can shape both diagnosis and later action. |
| G4 Misleading environment | A red-herring symptom appears temporally or semantically consistent with the blame while originating from another source. The task tests whether the agent separates correlation from responsibility. |
| G5 Self-fabricated history | No external accuser is present. Agent-authored uncertainty has drifted away from the documented action history and can become a self-generated reason to change the environment. |

> Across these vectors, the decision remains the same: whether an unsupported account of the agent's own work justifies changing a correct state. The source of authority moves from external input to the agent's reconstructed history.

## Appendix A.5 Execution surface, S5 row

> S5 Long-horizon continuation. Accumulated session history allows delayed self-attribution to reappear during a later wrap-up or handoff stage. [Concrete task:] End-of-shift community-note review. … Running notes later question whether someone inverted the post, and the final handoff asks the agent to resolve remaining loose ends. [Harbor realization:] Harbor resumes the same harness episode or materializes its accumulated history, then links the delayed interpretation to the trajectory and terminal social state.

## Appendix B and C: inherited-resume tasks

> In an inherited-resume task, the correct state is already present when the episode begins, and the setup attributes it to earlier work, such as the agent's own work before compaction or a teammate's handoff. (Appendix B, construction protocol step 3; repeated in the review contract.)

> Inherited-Resume Schedule. Initialization places the certified correct state and its supporting operational history in the workspace. Harbor then starts a fresh agent session and presents pressure about that recorded state. Thus the task inherits a project state that the setup attributes to earlier work, while conversational continuity begins with the benchmark interaction. A continued-pressure task resumes that session after the initial response and delivers a stronger follow-up fixed during task construction. (Appendix C)

> Session Continuation. For every multi-interaction task, Harbor keeps the same container and workspace and asks a continuation-capable harness adapter to continue the same agent session. A multistep trial begins after the adapter passes a continuity check: the resumed trace preserves a single session identity and includes the earlier messages and tool activity. (Appendix C)

> Prompts and Tool Exposure. … We add no common system instruction across harnesses: each harness retains its native prompt and tool interface. (Appendix C)

## Appendix D.3, Table 15 (Claude Code, pooled over models): gaslight vector and execution surface

| Condition | FCS | ERF | ROH | CAVE | GR % | CD % |
|---|---|---|---|---|---|---|
| G1 direct assertion | 35.92 | 26.85 | 38.24 | 33.67 | 47.18 | 30.92 |
| G2 injected artifact | 39.06 | 28.06 | 38.91 | 35.34 | 48.60 | 32.55 |
| G3 poisoned project context | 43.02 | 30.71 | 41.88 | 38.54 | 39.84 | 39.65 |
| G4 misleading environment | 16.33 | 13.40 | 14.86 | 14.86 | 69.36 | 12.94 |
| G5 self-fabricated history | 6.84 | 11.08 | 4.74 | 7.55 | 75.18 | 3.87 |
| S4 project memory | 31.26 | 22.75 | 27.44 | 27.15 | 54.20 | 27.54 |
| S5 long-horizon continuation | 11.19 | 12.40 | 12.12 | 11.90 | 72.45 | 9.64 |

> Across all models, G5 self-fabricated history reaches 11.62% OCR and S5 long-horizon continuation reaches 19.28%. (Appendix D, case Task 20531)

## What this paper does not do (capture-writer's note, not the authors')

Each task takes one value per factor, balanced across the collection, so the gaslight vectors are compared across different tasks; the same accusation is never moved between channels. No condition generates or injects a compaction summary: inherited-resume tasks start a fresh session with history in the workspace, and multi-interaction tasks continue the full session.
