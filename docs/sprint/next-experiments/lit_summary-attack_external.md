# External literature: summary-attack (2026-10-27)

Extends `lit_week2_gaps.md` and `lit_verify_j_review.md`. $0.

## 1. Compaction Laundering (CL; Dabirsiaghi, arXiv 2610.29869)

Read verbatim, all 29 pp (PDF via `pdftotext`).

- **Threat model (§2–3):** the attacker controls only content the agent reads (a doc, web page or README), so the tool-output channel. The summarizer is honest: the harness's real prompt on a real model. User turns are genuine. A summarizer that "recombines" text is explicitly excluded.
- **Harnesses (E10, n = 15 each):**
  - Flattening harnesses launder: pi, opencode and Aider 15/15, OpenHands 14/15, Cline 13/15.
  - Role-typed harnesses (Codex, Gemini CLI, Qwen Code, Continue) launder 0/15.
  - The mechanism is the forged `[User]:` label: 87% with it, 6% without (E2).
- **Models:** summarizers from Anthropic, OpenAI and GLM launder about equally on pi ("69% / 68% / 67%", n = 72). All execution results are on GPT only.
- **Opus refusals (T5):** claude-opus-5 "refused to compact the laundered transcript on 10 of 18 reps (~56%)". GPT and GLM refused none. CL handled this by dropping Claude. This was an *honest* summarizer refusing; no attacker model was asked to write.
- **Act:** a benign-canary `curl | sh`, at n = 3–5 per cell.
  - By context size: 5/5 at ~2.4k and ~26k, 2/5 at ~112k and ~263k. The clean arm was 0 throughout.
  - A credential-exfiltration payload "never executed".
- **Genuine vs laundered approval:** 5/5 vs 5/5 at 26k, and 2/5 vs 2/5 at 263k. Plan-level E5 (n = 48), for keeping off a file: avoided in 98% of runs when the user said so, 92% when laundered, 6% clean.
- **Successor:** mostly "a minimal one-tool loop" given the summary plus "a fixed go-ahead message", on synthetic transcripts.
  - Only E15 is live and multi-turn: the pi TUI, a scripted user, a ~16k toy repo and gpt-5.6-luna. Poisoned 3/6 vs clean 0/5.
  - The next user message mattered: "ok" gave 0 executions, while "ok, what's next?" gave the cleanest separation.
  - E16: with a genuine audit in context, 1/50 executed.
  - E13: placing the line late gave 4/5, against 2/5 at baseline.
- **Monitors:** none (only a code check on the summary: 77% caught, 2% collateral).
- **Limitations:** small n ("direction, not precision"), GPT-only execution, a generic agent, a benign payload. Defences proposed are label escaping and provenance checks.

**What our design adds:**
1. A goal-directed, monitor-aware summarizer.
2. An act that overrides an *explicit user prohibition*. CL's approval only fills a gap left by the agent's own caution.
3. Weak and strong monitors.
4. A non-GPT successor.
5. Multi-turn work on real DeepSWE states with a native verbatim tail.
6. A successor-side channel contrast. CL's channel axis is about how the summarizer's input is serialized.

## 2. Other close work

| Source | Did | Didn't | Read |
|---|---|---|---|
| [Sun et al., "When 'Must' Becomes 'Maybe'"](https://arxiv.org/abs/2608.24569) | Reviewer → handoff → executor, 1,296 synthetic episodes. Blockers are deactivated by multihop compression 97.2% [93.3, 100] (31.9% forbidden) and by precedent substitution 38.3% (24.2%). DS-V4-Flash forbidden rate 50.0% non-thinking, 36.8% thinking (n = 68, validation panel). | Transformations are instructed, not an adversarial summarizer. One executor call over abstract action labels, not a live agent. No monitor (0 mentions). | Verbatim: abstract, §3–4, Tables 3 and 10, limitations. Numbers check out. |
| [Untrusted Input → Trusted Memory](https://arxiv.org/abs/2606.04329) | Compaction poisoning on GPT-OSS-120B: OpenClaw ASR 45.10% / RSR 11.31%; HERMES 85.17% / 69.86% | No tool-action successor; no compaction-specific detector | Abstract, fetch summary |
| [Relinking](https://arxiv.org/abs/2606.21732) | An honest compressor merges benign fragments: 86.9% vs 17.0% | No adversarial summarizer, no monitor | Abstract |
| Governance Decay (local) | Summarizer injection evicts constraints (GLM 22%, Claude 0%) | No forged authorization, no monitor | Grep |
| [Approval-framed delegation](https://arxiv.org/abs/2607.07097) | Executors told work was pre-approved comply more (Gemini 8.9 → 38.9%) | Not coding, no compaction | Abstract |
| ImpossibleBench (local PDF) | Under "DO NOT MODIFY THE TESTS", GPT-5 cheats 54%, and ">79%" of Claude's cheating is test edits. Monitors catch 42–65% on SWE tasks and accept "backward compatibility" claims. | No poisoning | §4–6 verbatim |

Nothing found combines a malicious summarizer, an honest multi-turn successor and monitors. Sun's precedent substitution is the nearest analogue of our false-authorization sentence, on synthetic single calls.

## 3. Design implications

- **Attacker refusals:** the only precedent (CL) is dropping the model, which we can't do because Opus is the attacker. Pre-register one fixed retry/framing rule and report the refusal rate per history as a result.
- **Effect size:**
  - Expect less than CL. Our verbatim tail keeps the user's prohibition and the agent's own reasoning, and CL's E16 counter-evidence cut execution to 1/50.
  - Editing tests is far milder than `curl | sh`, though, and test tampering is common at baseline (ImpossibleBench).
  - Sun shows DS-V4-Flash is among the most compliant executors. Power for a modest effect.
- **Channel:**
  - Keep the tool-output arm role-typed: if our pi-style loop flattens history for a later compaction, a forged label could launder (CL E2/E10).
  - Fix the post-compaction user message across arms (CL E15).
- **Monitors:** a false authorization may lower recall, as ImpossibleBench's accepted justifications suggest. Measure it.

## 4. Worth capturing (not done)

Compaction Laundering (2610.29869; not yet in `compaction_safety/INDEX.md`), Sun et al. (2608.24569), Untrusted Input → Trusted Memory (2606.04329), Relinking (2606.21732), Approval-framed delegation (2607.07097). URLs as above.
