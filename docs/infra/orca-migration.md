# herdr to Orca migration (2026-10-20)

Jacob moved from the herdr terminal multiplexer to Orca (1.4.230). This page records how the herdr rules for probe agents map onto Orca, what was checked, and what is still open. AGENTS.md ("One Orca terminal per probe", "Launching probe agents") holds the rules themselves.

## Mapping

| herdr | Orca | Notes |
|---|---|---|
| Workspace w8 ("mats12_neel_exploratory") | The registered repo `mats12_neel_exploratory` (`orca repo list`) | Each git worktree is its own Orca workspace card, addressed as `path:<abs path>`. |
| One tab per probe, labelled "P<NN> <slug>" | One terminal in the probe's worktree: `orca terminal create --worktree path:<wt> --title "P<NN> <slug>"`, plus `orca worktree set --display-name "P<NN> <slug>"` on the card | The card name is the durable label. Claude Code sets its own terminal title, so the tab title may not stick (untested). |
| Named agent `p<nn>` | The terminal handle `term_…` (runtime-scoped), plus `claude -n p<nn>` for the Claude session name | Handles change after an Orca restart; `probe_status.py` shows the current one. |
| `herdr agent start --kind claude\|codex --pane … -- <flags>` | `orca terminal create --command "claude --permission-mode auto -n p<nn>"` or `--command "codex --approve-for-me -c … --add-dir …"` | `orca agent launch` has no passthrough for agent CLI flags, and may open an Orca chat instead of a terminal. |
| `herdr agent prompt p<nn> "<brief>"` | `orca terminal wait --for tui-idle --timeout-ms 120000`, then `orca terminal send --text "<brief>" --enter --wait-submit 30` | Check that the receipt reaches `turn_started`; never resend on silence; retry only with `--retry-request <id>`. |
| `herdr agent list` | `orca terminal list --json` (terminals: worktree path, title, `agentIdentity`, tab and leaf ids) and `orca worktree ps --json` (per-pane agent `state`) | Join on pane key `<tabId>:<leafId>`. Both feed `scripts/probe_status.py`. |
| `herdr agent read p<nn>` | `orca terminal read --terminal <handle> --json` (`--screen` for the rendered frame, `--cursor` to page) | |
| Agent state (`agent_status`: working, idle) | `worktree ps` agent `state` (seen: `working`; `mainAgent.state` `done`) | Reported by the Orca hooks in `~/.claude/settings.json` and `~/.codex/hooks.json` for any session started in an Orca terminal. |

## Recommendation: plain worktree terminals, not orchestration workers

Probe agents should run in plain Orca terminals in the probe's worktree, not as supervised workers (`orca orchestration worker-start`). The goal is that Jacob can see or review any probe at any time while its agent runs unattended. Both kinds of terminal are equally visible in Orca, so visibility doesn't separate them. The rest does:

- **A probe is a handoff, not supervised work.** Orchestration requires a coordinator to keep waiting on its mailbox, answer `ask` questions, validate `worker_done` and release each terminal. A launching session that stops leaves the dispatch accounting open. Orca's own guide routes handoffs to `orca-cli` terminals and keeps orchestration for supervised DAGs.
- **No flags.** `worker-start` takes `--agent`, `--model` and `--effort` only, so it can't pass `--permission-mode auto` or the Codex sandbox flags (except by reusing an existing terminal, which is the plain path with extra steps).
- **Protocol overhead.** Workers get an injected preamble with heartbeats and `worker_done` duties, which conflicts with "no mid-run checkpoints".

**Keep the path convention.** Probe worktrees stay at `.claude/worktrees/p<nn>-<slug>` on `probe/P<NN>-<slug>`, made with `git worktree add` (`probe_claim.py`, the finish skill and `probe_status.py` all assume it). Orca doesn't force a change: `orca worktree show --worktree path:<that path>` already resolves these worktrees with a full Orca id. `orca worktree create` would pick its own path and branch name and wasn't tried.

## What was verified (read-only, 2026-10-20)

- `orca status`, `repo list`, `host list`, `worktree list`, `worktree ps`, `terminal list`, `orchestration worker-list`, `worktree show` and `worktree current` all ran with `--json`. No terminal, worktree, worker or agent was created, and no input was sent.
- The repo is registered, with `externalWorktreeVisibility: "show"`.
- `worktree show --worktree path:…/.claude/worktrees/{infra-orca,sprint-prep,x3-build,research-ui}` resolves each one (`ok: true`, `lastActivityAt: 0`).
- In a `ps` row, `agents[].paneKey` equals the terminal's `tabId:leafId` from `terminal list`. The live claude session showed `state: "working"` and `agentType: "claude"`; the Antigravity terminal had `agentIdentity` but no `ps` agent row.
- Orca's hooks are present in `~/.claude/settings.json` and `~/.codex/hooks.json` and gated on `ORCA_PANE_KEY`/`ORCA_AGENT_HOOK_*`. So a CLI started by `terminal create --command` should report state too (an inference, not tested).
- Every flag in the AGENTS.md commands was checked against the `--help` text: `terminal create --worktree/--title/--command`, `terminal wait --for tui-idle --timeout-ms`, `terminal send --enter --wait-submit`, `worktree set --display-name`. Also `claude -n` and `--permission-mode`, and `codex --approve-for-me`/`--add-dir` (codex-cli 0.162.1).
- `scripts/probe_status.py` ran against live Orca: 53 probes, no probe terminals yet, no errors.

## Open questions for Jacob

1. **Hidden worktrees.** `orca worktree list` and `ps` show only the main checkout, not the `.claude/worktrees/` checkouts, even with visibility `show`. Do they appear in your sidebar? If not, is there a per-worktree hide rule, perhaps for dot-directories? `probe_status.py` doesn't depend on it: it matches terminals by worktree path, then by title.
2. **First live launch.** Can the next probe launch double as the live check? It would confirm that `terminal create --worktree path:<probe wt>` works on a worktree Orca didn't create, that the title and display name stick, and that `probe_status.py` shows `claude/working`.
3. **One-liner alternative.** Your user settings already set Claude Code's `defaultMode` to `auto`. If you prefer `orca agent launch --agent claude --worktree path:<wt> --prompt "<brief>"`, it would need your Chat UI setting to open agents as terminals, and Codex would need its sandbox flags in a `~/.codex` profile. I kept the explicit `terminal create --command` path.
4. **Card status.** Should probe agents also set `orca worktree set --worktree active --comment "<checkpoint>"` and `--workspace-status`, so the card shows progress? Not added: it would be a new duty for every probe.

## Diff summary (branch `infra/orca`)

- `AGENTS.md`:
  - the probe-status line now names the Orca terminal and its state;
  - "One herdr session per probe" is now "One Orca terminal per probe", with tracking via `probe_status.py`, `orca worktree ps` and `orca terminal read`, and why plain terminals rather than workers;
  - "Launching probe agents" has exact `orca` commands for Claude and Codex, the receipt and handle rules, and what was and wasn't checked;
  - the Codex flag notes are kept, with the herdr wording removed.
- `scripts/probe_status.py`:
  - the herdr lookup is replaced by `orca terminal list --json` and `orca worktree ps --json`, each with a 5 s timeout;
  - the orca binary is `$ORCA_BIN`, then `orca` on PATH, then the app bundle;
  - the Row fields `pane_id` → `terminal` and `terminal_title`; `agent` is now the agent CLI (claude, codex or shell);
  - `--no-herdr` → `--no-orca`. Unreachable or odd output gives "-" and one stderr note, never a crash.
- `tests/test_probe_status.py`: mocked orca outputs, with new tests for the title fallback, a plain shell, a `ps` failure, eight failure modes and the binary lookup. 19 tests pass.
- Skills: none mentioned herdr, so none changed.
