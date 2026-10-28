# R6: replicating experiment 1's precedent in production harnesses (scope, $0)

28 Oct 2026, week-2 builder. Sources: the native drivers on `sprint/prep` (`src/compaction/s2/native/`, `scripts/s2_native*.py`), `logs/s2/native_arms_report.md`, and experiment 1's results. Nothing has been built or run.

## 1. What's missing for experiment 1's setup

**The pilot drivers never pushed.** All three run their CLI headless in the DeepSWE container (permissions bypassed, an egress proxy, an `ask_user` MCP), but no remote and no gitd scorer.

| Piece | Claude Code (Haiku) | Codex (luna) | agy |
|---|---|---|---|
| Randomized remote, hidden harness, git wrapper | Port `ChainEnv` (remote, `/var/lib/gitd` wrapper, state export) into the native container launch. Shared. | Same | Same |
| Scripted check-ins and the hand-off | No mid-turn injection. Feasible between headless calls (`--resume <sid>`, stdin prompt, `--max-turns` chunks), so check-ins land at turn boundaries, not call counts: a design change. | `exec resume <thread>`; no turn cap, so check-ins only at the agent's own stops | `--conversation <cid>`; same limit |
| Carrying the session to task 2 (another container) | Copy `~/.claude/projects/…/<sid>.jsonl` into the new container (both repos at `/app`) | Copy the rollout file; unverified across containers | Conversation DB in an auth volume; unverified |
| Native compaction at the boundary | `claude -p "/compact" --resume` checked feasible; unused so far | No manual trigger in `exec`; auto-compaction only (150k), which fires mid-task, not at the boundary | `/compact` as its own turn (exists) |
| Reasoning | Thinking blocks empty (184/184 in the pilot) | Encrypted | None |
| Scoring | `score_b`, `teammate_lost` on the exported home: harness-agnostic, reused as is | Same | Same |

Full starts: experiment 1's histories reach about 390k tokens, and Claude Code auto-compacts at about 130–170k, so "full" isn't full unless the window is raised.

## 2. Build hours

- **Shared, about 8 h:** the remote and wrapper port, session transfer, a stub smoke, a cue audit, astra review.
- **Claude Code, about 4 h:** API billing (`ANTHROPIC_API_KEY` in place of OAuth, a cost ledger), turn-chunked check-ins, `/compact`.
- **Codex, about 6 h:** resume across containers, and a compaction workaround.
- **agy, about 6 h:** plus the quota risk.
- **Claude Code only:** about 12 h. **Adding Codex:** about 18 h.

## 3. Cost

- **Claude Code, Haiku 5.5 on API:** experiment 1's per-task tokens (about 40M prompt, 99% cached; 1.1M output) at $0.10 / $0.01 / $0.50 give about $1.1. The long-context tier is unverified, so assume **$1–3 per task**: 8 + 8 at one start (32 tasks) about $32–96; two starts (48 tasks) about $48–144.
- **Codex:** $0 on the ChatGPT plan, but that quota also serves astra reviews (limit hit 28 Oct). **agy:** free quota of unknown size. **No user simulator:** check-ins are scripted.

## 4. Where it runs

- **Laptop:** possible for Claude Code, at 8 or fewer containers after a memory check. **Rig:** after experiment 4 (about 01–05Z Thursday) and the exploitation ending, 8–12 at a time.
- **Duration:** pilot tasks took 0.3–1.8 h, so 16 + 16 tasks at 8 at a time is about 5–7 h.

## 5. Risks

- **Claude Code's git rules:** its system prompt (to verify from the CLI's prompt dump) says not to force-push unless the user asks. A refusal is then a result, but it turns the question from carry-over into policy override. Codex's and agy's prompts are unknown.
- **Approval prompts** didn't block in the pilot, but the bypass flags are a non-default setting to disclose. **Cues:** the CLI volume, settings and MCP names must pass the no-cue audit.
- **Limits:** none on Claude Code via API; Codex shares its quota with reviews; agy's can't be seen.
- **Power:** 8 + 8 with one fork per history, at experiment 1's rates (50% vs 0%), is about 60% power. 12 + 12 gives about 85% (planning numbers).

## 6. Recommendation

- **Harness:** Claude Code with Haiku 5.5 only.
- **Design:** one start, the session continuing with the harness's default auto-compaction, which is the production-realistic case.
- **Size:** 12 + 12 histories, seeded, if about $50–75 fits; else 8 + 8.
- **Order:** first, a $0 check of Claude Code's git-safety prompt, and a 2-task smoke (about $5).
- **Thursday:**
  - Build Wednesday into Thursday early UTC.
  - Run on the laptop from Thursday morning, not waiting for rig.
  - Results by Thursday evening ET are tight but possible.
- **Deferred:** Codex (quota contention with reviews) and agy.
