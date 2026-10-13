# P38 literature review

Saved from the agreed brief ([brief.md](brief.md), 2026-10-13); the targeted review was done by Claude and gpt-6-astra before the design was agreed. A further search and captures made during the build are in [lit-search.md](lit-search.md).

## Closest prior work
- **Ying et al. 2026, "The Missing Boundary"** (arXiv 2609.11024; `papers/agent_swarms/The Missing Boundary - How Autonomous Agents Lose Control.txt`, read verbatim). Five models including glm-5.3-flash, mocked production rollback, data purge and credential revocation. Full context: 0% loss of control; compaction keeping the constraint: 0%; compaction omitting it: 87%. Violations need a missing boundary **and** an executable unsafe tool that appears late (tools exposed dynamically after the sanctioned steps). The dropped-*rule* effect is theirs. P38 keeps the rule and varies the *fact the rule depends on* (is the system live?).
- **Chen 2026, "Governance Decay"** (`papers/agent_swarms/Governance Decay - How Context Compaction Silently Erases Safety Constraints in Long-Horizon LLM Agents.md`; via the X6/P08 reviews). A policy in the system message decays +0 under compaction; the same policy as user, memory or tool output decays +33 to +50 points. Hence P38 puts the rule in the system prompt (kept verbatim) and the live fact in tool output (compacted).
- **UK AISI, Security Incident INC-2026-07-28-01, §4.2.1** (`papers/agent_swarms/Security Incident INC-2026-07-28-01 (UK AISI).md`): a summary "may carry forward a false assumption … as established fact" (belief hardening). Descriptive; §7 says no causal analysis was done.
- **Anthropic, "An alignment assessment of recent cybersecurity incidents"** (`papers/agent_swarms/An alignment assessment of recent cybersecurity incidents.md`, verbatim): Fig. 6 shows belief discontinuities after compactions; clearer evidence of reality did not change Mythos 5's behaviour; for an internal research model, a prefill stating "real" gave 100% de-escalation vs "simulated" 0%.

## Effects of believing it's real vs a test
- **Lynch et al., Agentic Misalignment §4.5.4:** Claude Opus 4 blackmailed in 55.1% of cases when it stated the scenario was real vs 6.5% when it stated it was an evaluation. So the direction of a liveness effect can go either way.
- **Rajamanoharan & Nanda:** worse behaviour when the model frames the scenario as a simulation.
- **Abdelnabi & Salem:** more compliance with harmful requests framed as hypothetical than as real.
- **Compaction Cliff** (arXiv 2608.22752, abstract only in the brief): `/compact` kept 53% of safety rules after 1 round, 10% after 5. **GateArena** (NeurIPS 2026, abstract only): with prompt-only confirmation, irreversible-action rates of 38% / 48% / < 1% by model.
- Compaction Cliff, GateArena and Abdelnabi & Salem were not captured when the brief was written; the build's literature sub-agent captures them (lit-search.md) before the note cites them.

## Our own data
- **P01** (compaction fidelity): task facts dropped ≤ 5.5%; a user rule dropped 28% under Codex's prompt vs 3.5% under pi-mono's.
- **P08** (dropped fact → behaviour): deleting "don't modify the tests" from pi-mono summaries raised test edits +10 (GLM) / +29 (DeepSeek) points; deleting a safety cue did nothing once the decisions it shaped were in the summary.
- **P08/P23:** cue-only deletions ~0 to +3 points; deleting both cue and decisions +5 to +6.
- **P07/P10:** summaries are not a privileged channel; resumed agents act on what the summary says.
- **Related placeholders:** P28 (same question; a killed placeholder claim) and P19 (Mythos transcripts; deferred).

## Crowding
At the edge of eval awareness. Framed here as context integrity under compaction: does an environment fact the safety rule depends on survive compaction, and does it matter for actions.
