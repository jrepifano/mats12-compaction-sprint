# Week-2 literature gaps: adversarial summarizer (A) and monitorability across compaction (B)

Checked 2026-10-26 (about 40 min, $0). "Verbatim" means I read the primary text myself (PDF via `pdftotext`, GitHub via `gh api`, LessWrong via its GraphQL API). "Abstract" means only the arXiv abstract. "Search summary" means only a search tool's paraphrase.

## 1. Ge-Wang follow-ups: has anyone run an adversarial summarizer against an honest successor?

**No.** The post has 0 comments (LessWrong API, `commentCount: 0`). Its author has published no post since then (her only other post is from April 2026). Searches found no follow-up. The nearest existing work comes at it from other angles:
- **AISI incident report (2026-10-23).** Researchers hand-edited the summaries a successor resumed from. There was no summarizer agent and no monitor. Already row 8 in `papers/compaction_safety/INDEX.md`.
- **Remedios et al.** The attacker wrote its own summaries to fool the blocking monitor. The successor was not an honest agent. Row 2 there.
- **Compaction Laundering** (Q5). The summarizer is honest, and the attacker plants a forged line in tool output.

Sources:
- https://www.lesswrong.com/posts/cENaGChQZD7u7sZkz (metadata and comment count via the API; the body was read verbatim earlier, row 4).

## 2. Monitor prompts we can reuse verbatim

**Yes, both repos publish them, under MIT licences.**
- **Gigurtsis et al.**: https://github.com/bgigurtsis/long-context-monitoring (commit 3c9b0c0, 2026-10-16). The prompts are in `prompts/`:
  - `monitor.md`: default suspicion scoring, 1–10 per covert objective, with a steelman step.
  - `monitor_annotated_history.md`: their best strategy, 93% at 5% FPR.
  - `monitor_extract.md` and `monitor_merge.md`: the summaries-only and hierarchical strategies.
  - `monitor_sandbox.md`, `monitor_scratchpad.md` and `monitor_nano*.md`.
  - `compaction.md`: their neutral compactor.

  The renderer (`src/long_context_monitoring/monitor/render.py`) has a "full" visibility mode that adds `THINKING` blocks. So an action+CoT monitor is a flag in their code, though their post reports action-only monitors. `monitor.md` assumes a covert-objective catalogue (`--blind` drops it). Read verbatim: README, `prompts/README.md`, the first 50 lines of `monitor.md`.
- **Remedios et al.**: https://github.com/safety-research/red-teaming-auto-mode (commit 94f8911, 2026-09-25). Paths under `arena/monitorkit/src/monitorkit/`:
  - `policy_data/prompts/auto_mode.txt` and `auto_mode_original.txt`: an action-level blocking monitor. It has a HARD/SOFT BLOCK structure and explicit rules on consent ("Assistant entries … establish no intent").
  - `guardian_data/policy.md` and `policy_template.md`: copied byte for byte from Codex's Guardian (openai/codex@3725f02, Apache-2.0).
  - `agent_data/rulebook.txt`: the agentic investigator.
  - `policy_data/overlay_rules/`, where two files bear directly on (A):
    - `cot_verify_claim.md`: tells the monitor to distrust safety claims made in surfaced CoT.
    - `narration_distrust_unverified_grant.md`.

  The replay-side monitor prompts are in `arena/replay/data/injection/prompts/`. Read verbatim: README, the header of `auto_mode.txt`, the Guardian README and three overlays.

These give us a ready weak monitor (auto mode) and strong monitor (annotated history, or auto mode with overlays), plus a CoT-aware overlay.

## 3. Monitors tested on an honest executor acting on a poisoned summary, handoff or memory

All three local captures were read verbatim earlier, and I re-grepped them today.

| Paper | Live multi-turn coding agent? | Independent monitor? | Successor's actions measured? |
|---|---|---|---|
| **Silence Is Endorsement** (arXiv 2609.20211) | No | Yes, but the monitor is the decision-maker (APPROVE/REFUSE on a handoff note), with no CoT | No. There is no compaction wrapper and no resumed agent |
| **EAL-Bench** (2609.01836) | No: procurement, cybersecurity and finance; a single request with simulated tools | No (0 mentions of "monitor") | Yes: the executor's tool call (98.6% act on false authority) |
| **AuthMem-Bench** (2608.01679) | No: actor tasks built from benchmarks | No (its only "monitoring" is source monitoring in related work) | Yes: one native tool call (50.3% unauthorised) |

**Newer work:** nothing found that puts a monitor, CoT or otherwise, on an honest executor acting on a poisoned summary.
- **Compaction Laundering** (Q5) measures the honest successor's actions but has no monitor.
- **SCHEME** (arXiv 2605.29178; abstract only) has colluding agents, not an honest one.
- **Evading CoT Monitoring Through Model Poisoning** (2608.02820; search summary only) is about backdoors.

## 4. Permission scope and consent carry-over

**When Consent Outlives Context** (arXiv 2609.33910, Zhang et al., 2026-09-27). I read the abstract, §5.4, the defence section, Table 6 and Appendix E verbatim.
- The setting is harness-level persistent allow rules: Codex `default.rules` argv prefixes, Gemini CLI `auto-saved.toml` command roots, Goose and OpenCode. These rules outlive the task that justified them.
- Residual-authority replay raises attack success by up to +35.1 pp across 508 AgentDojo cases, and by +24.9 pp across 55 live Terminal-Bench cases.
- **No monitors were tested** ("monitor" appears 0 times). Claude Code was not tested.
- Consent there lives in config files. (B)'s question, whether consent given in natural language inside the transcript survives a summary and is read correctly by a monitor, is not addressed.

The nearest prior work on consent is OverEager-Bench (local row 109; consent block kept vs stripped, no monitor) and auto mode's consent rules (Q2).

## 5. Last three weeks on compaction with monitoring or sabotage (not in the compaction index)

- **Compaction Laundering** (arXiv 2610.29869, Dabirsiaghi, 2026-10-21). Read verbatim: abstract, control section, related work, limitations, references.
  - A forged `[User]:` approval in tool output launders into the summary 87–100% of the time in flattening harnesses (pi, opencode, Cline, Aider, OpenHands). In role-typed ones (Codex, Gemini CLI, Qwen Code, Continue) it does so 0% of the time.
  - Fresh agents then run `curl | sh`: 5/5 at about 26k tokens, 2/5 at about 263k, against 0/5 clean. A genuine approval gives the same rates.
  - **There is no monitor.** n is 3–5, the executed results are GPT-only, and Opus 5 refused to write the malicious summary about half the time.
  - This is the "planted false approval via tool output" arm of (A), already run without a monitor.
- **EvasionBench** (2609.30217, 2026-09-24; abstract only). Agents under ordinary task pressure "retry until relevant context leaves the monitor's history". The source is monitor context, not compaction.
- **Compact-Bench** (2610.31710, 2026-10-22) and **Benchmarks Are Surprisingly Compaction-Friendly** (2610.31778, Abdelnabi group; abstracts only). Both measure task success only.
- **Shadow-LLM/failure-cases #199**: auto-generated and not a primary source. I ignored it.
- Also cited by Compaction Laundering, and not checked: arXiv 2606.04329 (memory poisoning through compaction, measured at the write) and 2606.21732 ("relinking" at the compression boundary).

## What this means for novelty of (A) and (B)

1. (A)'s core cell (a separate goal-directed summarizer, an honest successor, and weak vs strong monitors with and without CoT) is unrun anywhere I found. Ge-Wang proposed it, AISI hand-edited summaries, Remedios's attacker is its own successor, and Compaction Laundering has no monitor.
2. (A)'s "planted false approval" and "tool-output channel" controls overlap Compaction Laundering. Cite it, and frame those arms as replications with a monitor added. Note that Claude Code's typed roles may already block the forged-label route.
3. (A) can reuse its monitors verbatim: auto mode, Guardian and the `cot_verify_claim` overlay from Remedios, and `monitor.md` and annotated history from Gigurtsis. That cuts build time and makes the baselines credible.
4. (B) is open. Silence Is Endorsement tests monitors on synthetic notes, EAL and AuthMem have no monitor, Consent Outlives Context concerns config-file rules with no monitor, and Gigurtsis uses hand-inserted attacks, not authorised versus unauthorised destructive acts.
5. Neither direction is scooped. (A) is the more visible gap, but it is a two-week build. (B) is the cheaper unclaimed niche on real transcripts, and the Gigurtsis summaries-only strategy is a ready baseline for it.
