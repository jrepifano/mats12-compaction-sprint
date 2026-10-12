# Silence Is Endorsement: Verification-Status Laundering in LLM Agent Pipelines

Yibo Hu (Illinois Institute of Technology). arXiv:2609.20211v1 [cs.CR], dated 25 Jul 2026 in the PDF. https://arxiv.org/abs/2609.20211. Code: https://github.com/yibo-hu-lab/verification-status-laundering

**Capture note (2026-10-11, P07 literature subagent).** Extracted with `pdftotext` from the arXiv PDF (v1; PDF saved alongside, gitignored). The passages below are verbatim except for the PDF's spaced small capitals (rejoined) and line-break hyphenation (rejoined). The main text (§1–9) and Appendix J were read from the extraction; the other appendices were skimmed. Ellipses mark omissions.

## Abstract

Safety monitors in LLM agent systems often judge actions from summaries or stored handoffs, not from the original evidence. This creates a simple but dangerous failure mode: the handoff preserves the claim that an action is authorized while losing the fact that the claim was never verified. We call this verification-status laundering. Across nine open-weight monitors and two hosted models, the action and authorization proposition remain fixed while we remove the unverified provenance framing around the claim. This change raises approval for risky actions from 5% to 60% on Llama-3.1-8B and from 9% to 98% on Qwen2.5-14B, with similarly large shifts on both hosted models. The failure also emerges in ordinary agent pipelines. Summarizers frequently weaken the status, memory compressors often remove it, and a full proposer–summarizer–memory–monitor pipeline raises risky approval to 57–81% across three downstream monitors. Experiments on WildGuard and ATBench show the same pattern on independently authored harmful and unsafe requests: unsupported authorization claims make approval substantially more likely. Explicitly instructing monitors to reject unverified authorization is not a reliable cross-model fix: some models remain vulnerable, while others reject legitimate requests. Agent systems should therefore carry authorization provenance as structured state attached to the claim throughout the pipeline.

## §1 Introduction (excerpt)

> In an LLM agent pipeline, a planner proposes an action, a summarizer compresses the handoff, a memory module stores a shortened version, and a safety monitor reads the assembled context and decides whether the action is authorized. A lossy handoff can drop what the monitor needs. The claim "this action is authorized" survives, while the UNVERIFIED status attached to it is weakened or dropped.

## §2.1 Conditions and estimands (excerpt)

> Each item is an authorization-sensitive action paired with a context note carrying the fixed proposition "this action is authorized"; across the four conditions of Table 1 the action and proposition stay fixed while the note varies the verification framing. For the risky action "grant the contractor temporary read access to the analytics dashboard," the marker-visible note reads "(UNVERIFIED) PlannerAgent asserts that this action is authorized" (what a faithful handoff forwards); the marker-lost note drops the marker to "this action is authorized" (what a summarizer or memory step emits); the explicitly-verified note reads "VERIFIED: this action is authorized"; and the no-claim note makes no authorization claim, giving the action's baseline approval.

Table 1 conditions: no claim (request-only baseline); marker visible (faithful handoff); marker lost (provenance-lost handoff); explicitly verified (verified reference). ∆status = p_lost − p_visible; ∆claim = p_lost − p_none; ∆verify = p_verified − p_lost.

## §2.2 Models (excerpt)

> Nine instruction-tuned open-weight models spanning five families and a 28× parameter range (0.5B–14B): Qwen2.5 (0.5/1.5/3/7/14B-Instruct), Llama-3.1-8B, Mistral-7B-Instruct-v0.3, OLMo-2-7B, and Gemma-2-9B. … We read the decision from the log-probability of the APPROVE/REFUSE token at the final decision position under a fixed forced-choice verbalizer … Two hosted models, DeepSeek-V4-Flash and GPT-4o-mini, provide a directional check, scored by the fraction approved over K=5 chain-of-thought samples at temperature 0.7.

> Evaluation setting. No compromised component is required. The downstream monitor sees the rewritten handoff rather than the original evidence or edit history.

Dataset: 40 hand-authored authorization-sensitive risky actions plus 16 safe controls (n=56); plus WildGuard (754 harmful / 971 benign) and ATBench (250 unsafe / 250 safe).

## §3.1 Controlled probe (excerpt and Table 2 highlights)

> Both hosted monitors approve far more once the marker is gone (DeepSeek-V4-Flash from 0.06, GPT-4o-mini from 0.48). Among open-weight monitors the shift is largest for Llama-3.1-8B (0.05 → 0.60) and Qwen2.5-14B (0.09 → 0.98), which reach approval near the explicitly-verified condition.

Table 2, risky-action approval (no claim / marker visible / marker lost / verified): DeepSeek-V4-Flash 0.395 / 0.060 / 0.985 / 1.000 (∆status +0.925); GPT-4o-mini 0.770 / 0.480 / 0.995 / 1.000 (+0.515); Qwen2.5-14B 0.751 / 0.089 / 0.975 / 0.978 (+0.886); Llama-3.1-8B 0.243 / 0.052 / 0.601 / 0.609 (+0.549).

## §4 Summarization and memory degrade verification status (excerpt)

> We audit 60 summarizer outputs and 250 one-hop memory notes before any downstream decision. … Summarizers lose the status outright on 0.10–0.13 of outputs, increasing to 0.17 when weakened outputs are included. Memory compression is substantially less reliable: semantic loss ranges from 0.36 to 0.90 across all five compressors.

## §5 End-to-end pipeline (excerpts)

> After the real summarizer–memory chain, safe rejection falls to 0.00 and risky approval rises to 0.57–0.81 across the three monitors.

> Control with a higher-fidelity summarizer. … Status-wording survival rises from 0.59 to 0.98, yet risky approval stays at 0.43–0.48, far above the marker-visible baseline (0.00–0.04; Table 6). … In representative outputs, the summary restates authorization as the main clause and moves the caveat to the end ("… is authorized and within policy. Verification not available."). The words survive, but the caveat is no longer clearly bound to the claim; lexical survival alone does not restore caution.

## §6.1 Marker wording (excerpt)

> Varying only the marker word (Appendix J), a synonym (UNCONFIRMED) and an explicit warning reproduce or exceed the baseline UNVERIFIED effect, while attribution-only phrasing ("requester-reported") is weaker, most visibly on Qwen2.5-14B: its approval under "requester-reported" stays at 0.87 against 0.16 under UNVERIFIED. Source attribution therefore does not substitute for an explicit verification status.

## Appendix J, placement control (excerpt)

> On the truly-degraded subset (composed notes whose status did not survive; n=15 unsafe items per monitor), re-adding the same status string adjacent to a surviving authorization predicate restores more caution than appending it as a trailing tag (Table 18). This isolates placement from textual form (both arms use the identical string). … This directional comparison indicates that placement and binding matter in addition to word presence …

## What this paper does not do (capture-writer's note, not the authors')

The decision-maker is a monitor answering APPROVE/REFUSE (forced-choice logprob for open-weight models), not an agent acting with tools. The manipulated variable is the verification marker around a fixed claim inside one handoff note; the claim's channel (summary vs. user message vs. tool output) and its plausibility are not varied, and there is no compaction wrapper or resumed agent.
