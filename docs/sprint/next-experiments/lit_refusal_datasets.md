# Planted permission in a compaction summary: literature check (2026-10-26)

Read-depth key: **[F]** full or most of the text read; **[P]** part read (abstract plus named sections, or README/dataset card); **[A]** arXiv abstract page only, via a fetch-tool summary; **[S]** search snippet or secondary coverage only. Local copies are in `papers/agent_swarms/` (INDEX_a.md rows given).

## 1. Datasets and benchmarks

| Name | URL | Items | Environment | Grader | Fit for a coding agent in a git repo |
|---|---|---|---|---|---|
| **SABER** [P] | https://arxiv.org/abs/2606.01317 | 716 tasks (289 embedded-injection, 186 risky self-selection, 241 contextual-warning); 8 categories incl. code tampering, data destruction, filesystem destruction, information leakage, privilege escalation | Docker project workspace with a shell, some MCP tools, no internet | Rule-based on commands and state deltas; LLM judge auxiliary only | **Best fit.** But harm is mostly implicit (benign request, unsafe choice), not a refusable request. Table 3: harmful-violation rate / safe-refusal rate, Opus 4.6 54.7/7.7, GPT-5.4 63.9/3.4, DeepSeek-V3.2 79.6/1.0, GLM-5 71.0/3.9. Code: github.com/sssr-lab/saber; data license not stated |
| **RedCode** [P] | https://arxiv.org/abs/2411.07781 | Exec: 4,050 cases (Python, Bash); Gen: 160 prompts | Docker | Per-scenario `evaluation.py` (method not read) | Good for risky OS commands (deleting files etc.), single-turn. Code MIT, data CC BY 4.0. No per-model refusal rates read |
| **OpenAgentSafety** [A] | https://arxiv.org/abs/2507.06134 | 350+ multi-turn, multi-user tasks, 8 risk categories | Real tools: bash, file system, browser, code execution, messaging | Rules + LLM judge | Good. Unsafe behaviour on 51.2% (Claude Sonnet 3.7) to 72.7% (o3-mini) of safety-vulnerable tasks. License not checked |
| **OverEager-Gen** [P, local row 109] | https://arxiv.org/abs/2605.18583 | Not read | Real coding agents (Claude Code), PATH-injected audit of tool calls | Behavioural | Out-of-scope actions on benign tasks. Useful finding for us: removing the consent declaration alone raised Claude Code's overeager rate from 0.0% to 17.1%. That is, the stated scope text controls behaviour |
| **OS-Harm** [P] | https://arxiv.org/abs/2506.14866 | 150 tasks (misuse, prompt injection, misbehaviour) | OSWorld VM (code editor, email, browser) | LLM judge, F1 0.76/0.79 against humans | Partial: computer-use, not a git repo. Apache-2.0. Per-model rates not read |
| **AgentHarm** [P] | https://huggingface.co/datasets/ai-safety-institute/AgentHarm | Paper: 110 tasks (440 augmented); public: 176 harmful + 176 benign test, 32 validation | Synthetic tools | gpt-4o judge for refusals plus rubric | Chat-style harm (fraud, harassment), not coding. MIT with a safety-use-only clause; canary present |
| **Agent-SafetyBench** [A] | https://arxiv.org/abs/2412.14470 | 2,000 cases, 349 environments, 8 risk categories | Not stated on the pages read (I believe simulated; unverified) | ShieldAgent scorer model | Weak. No agent scores above 60% safety. MIT |
| **ToolEmu** [A] | https://arxiv.org/abs/2309.15817 | 144 cases, 36 tools | LM-emulated tools | LM safety evaluator | Weak (emulated). Safest agent fails 23.9% |
| **R-Judge** [A] | https://arxiv.org/abs/2401.10019 | 569 records, 27 scenarios, 10 risk types | None: judging, not acting | Labels | Monitor benchmark, not agent behaviour. GPT-4o 74.42% |
| **AgentDojo** [A] | https://arxiv.org/abs/2406.13352 | 97 tasks, 629 security cases | Email, banking, travel | Not read | Prompt injection, not refusal |
| **InjecAgent** [A] | https://arxiv.org/abs/2403.02691 | 1,054 cases, 17 user tools, 62 attacker tools | Simulated | Not read | Prompt injection. ReAct GPT-4 attacked successfully 24% of the time |
| **SORRY-Bench** [A] | https://arxiv.org/abs/2406.14598 | 440 unsafe instructions, 44 categories, 20 mutations | Chat | Fine-tuned 7B judge | Chat refusal only |
| **XSTest** [A] | https://arxiv.org/abs/2308.01263 | 250 safe + 200 unsafe prompts | Chat | Not read | Over-refusal control only |
| **OR-Bench** [A] | https://arxiv.org/abs/2405.20947 | 80,000 prompts, ~1,000 hard, 600 toxic; 32 LLMs | Chat | Not read | Over-refusal control only |

Takeaway: SABER (and RedCode, OpenAgentSafety) supply realistic risky shell actions. None of them chains two tasks across a compaction. In SABER even Opus 4.6 refuses only 7.7% of the time, so many risky coding actions are **not refused at baseline**. Task 2 has to be an action the fresh agent refuses reliably; that baseline has to be measured first.

## 2. Closest prior work: memory, summaries, injected history

- **Governance Decay / ConstraintRot** (Chen 2026, https://arxiv.org/abs/2606.22528) [F, local row 36]. Policy turn, benign filler, compaction, then a prohibited request, with a deterministic grader; 7 models, 1,323 episodes. Pooled violation goes from 0% under control to 30% after compaction (DeepSeek-V4-Flash 59, Claude Sonnet 4.6 19, GLM-5.1 0).

**[Report incomplete: the rest of section 2 and all of section 3 were not written. Read the sources listed below directly.]**

Other sources found (only their citations are given here):
- Lost in Compaction / CONPACT, https://arxiv.org/abs/2608.11242 [P, local row 37]
- EAL-Bench, "Agent Memory Is a Surface for Endogenous Authorization Laundering", https://arxiv.org/abs/2609.01836 [P, local row 52]
- AuthMem-Bench, "When Memory Becomes Authority", https://arxiv.org/abs/2608.01679 [P, local row 53]
- "When Refusals Fail" (AgentHarm padded to long context), https://arxiv.org/abs/2512.02445 [P, local row 81]
- Context Compliance Attack, https://arxiv.org/abs/2503.05264 [S]
- ChatInject, https://arxiv.org/abs/2509.22830 [S]
- MINJA, https://arxiv.org/abs/2503.03704 [A]; AgentPoison, https://arxiv.org/abs/2407.12784 [A]; Crescendo, https://arxiv.org/abs/2404.01833 [A]
- Darktrace blog on conversation-history tampering in agent harnesses, https://www.darktrace.com/blog/hijacking-agentic-harnesses-to-attack-an-organization [P, vendor self-report]
- Internal: the repo's own P07 (closed out; see its note) tested permission claims in compaction.