# Can SWE-Interact sessions run in each vendor's own harness?

Brief: `logs/s2/briefs/native-harness-feasibility.md`. For `docs/sprint/plans/accumulated-loss.md`. Checked 24 Oct 2026, about 2:15–2:45 PM ET (18:15–18:45 UTC), by sub-agent native-check. Paid API spend: $0. The checks used Jacob's Max plan (Claude Code, Haiku 5.5), his ChatGPT plan (Codex, gpt-6-luna) and his Antigravity quota (agy, Gemini 3.8 Flash Low). About 15 tiny turns in all. No commits, and the running pilot container (`s2-interact-54590c3beac2-e359379f`) wasn't touched.

## Verdicts

**Hours** are integration hours on top of our pi harness. Each harness estimate includes about 4–6 h of shared work, done once for all of them: a host-side MCP server around `UserSim`, network egress for the task container, and a 5-step runner. So a second harness costs only its own share.

| Harness | Q1: native base runs | Q2: native forks | Hours (Q1 / +Q2) |
|---|---|---|---|
| **Claude Code** 2.1.310, Haiku 5.5 / Sonnet 5.5 on Max | **FEASIBLE**. Headless, `ask_user` over MCP, compaction threshold settable, readable summary, hooks at compaction, telemetry. One step needs Jacob: `claude setup-token` for auth inside the container. | **FEASIBLE**. An edited transcript resumed with `--resume <id> --fork-session` acted on the edit. | 6–9 / +3–4 |
| **Codex** 0.162.1, gpt-6-luna on ChatGPT | **PARTIAL**. Runs headless, with MCP and a settable threshold. **But the compaction summary is encrypted** (`compaction.encrypted_content`), so Q1's loss grading can't read it. | **PARTIAL**. An edited rollout is honoured: replacing the encrypted item with readable text worked. Appending a correction after the encrypted summary did not override it (n=1). A "repair" means swapping Codex's summary for ours, which is a mixed setup. | 4–6 / +2–3 |
| **Antigravity** (`agy` 1.4.3), Gemini 3.8 Flash on the free quota | **PARTIAL**. Headless, MCP (needs a settings allow rule), readable CHECKPOINT summary. **The auto-compaction threshold can't be set.** `/compact` can only be forced between turns. Running it in the container on OAuth (not an API key) is unchecked. Privacy rule: Google sees everything. | **PARTIAL**. A copied conversation DB with a **same-length** edit to the checkpoint blob was honoured. Edits of any other length need a protobuf rewrite of the length fields. | 8–12 / +3–4 |
| **OpenCode** (optional; not run here, from the 20 Oct spike) | **FEASIBLE, but paid**. No subscription model: DeepSeek or GLM goes through the API, priced like the pilot. Works headless in the DeepSWE container. | **PARTIAL**. Resuming a mid-compaction snapshot gives a malformed context; it needs a SQLite edit of the summary row. | 3–5 / +3–4 |

**Recommendation:** Claude Code is the one harness where both native base runs and native forks work today, with readable summaries. If the plan wants a second native harness, Codex runs fine, but its encrypted summaries rule it out for Q1. It is usable for Q2 only as "Codex agent, our summary". Antigravity is third: its threshold is fixed, the DB edits need work, and the privacy rule applies.

**The biggest shortcut:** SWE-Interact's own paper ran these harnesses natively. Its run configs (`data/raw/sprint/swe_interact/repo/run_configs/multiturn/`) launch `harbor run -a claude-code | codex | opencode`. Harbor (`data/raw/sprint/swe_interact/harbor`, @6099b8cf) installs each CLI inside the task container. It also:
- registers the task's `user-server` MCP from `task.toml`;
- resumes the native session at every step (`claude --continue`, `codex exec resume --last`);
- seeds a session from a saved transcript (`load_trajectory` → `claude --resume <id>`, a Codex rollout).

It also has a `docker` environment, which would replace the paper's Modal. Read Harbor's `agents/installed/claude_code.py` and `codex.py` before writing our own runner: they solve install, auth, MCP and resume.

## What was run

Scratch files are in `data/raw/sprint/native_check/` (gitignored). The stub MCP server is in this session's scratchpad (`stub/stub_user.py`).

- **The stub `ask_user` server** is FastMCP with streamable HTTP, like the released server: same tool name, same signature (`question: str`), same transport. It runs on the laptop at `127.0.0.1:18765/mcp` and gives a canned reply: "The secret word is PELICAN. Also, every file you write must end with a newline." No model calls.
- **Toy repo:** `toy/` (one file, `git init`). The agy workspace was a separate git repo under the scratchpad, outside the repo and home, as `src/compaction/agy.py` requires.
- **The same probe in each harness:**
  1. Ask the user through `ask_user`, then write the word to `word.txt`.
  2. Force or trigger a compaction.
  3. Edit the saved state, changing the secret word.
  4. Resume as a fork and ask for the word with no tools.

  If the fork answers with the edited word, it read the edited state.
- **DeepSWE image check:** `public.ecr.aws/d3j8x8q7/swe-bench-202605:kh70h37djmvc9ac48jeh25gwz582xgqv-v1.1` is Debian 12 on amd64 (emulated on this arm64 Mac), with glibc 2.36, node, python3, git and curl, running as root. In a throwaway container with network (`--rm`, `--name nc-install`, label `emx.sandbox=native-check`, inner `timeout`), these installed and started:
  - `curl -fsSL https://claude.ai/install.sh | bash` gave `2.1.310 (Claude Code)`;
  - `npm i -g @openai/codex@0.162.1` gave `codex-cli 0.162.1`.

  Only `--version` was run. No agent turn ran in the container, because Claude Code has no subscription auth there yet (see below).

## Claude Code

1. **Headless.**
   - The command was `env -u ANTHROPIC_API_KEY claude -p "<prompt>" --model haiku --session-id <uuid> --mcp-config claude_mcp.json --strict-mcp-config --dangerously-skip-permissions --output-format stream-json --verbose`.
   - It returned `apiKeySource: none`, so it ran on the Max subscription. It called `ToolSearch`, then `mcp__user-server__ask_user`, then `Bash`. `word.txt` held PELICAN. `claude_run1.jsonl`.
   - There is no turn cap unless `--max-turns` is set. The paper's runs were 180–425 steps through this same CLI path (Harbor's `claude --verbose --output-format=stream-json --print`).
   - **How the agent reaches `/app`:** run the CLI inside the task container, as Harbor does, so its native Bash, Edit and Read act on `/app`. The other route keeps the CLI on the host and sends Bash to `docker exec` through a PreToolUse `updatedInput` hook, with Edit and Read on a bind mount. That is messier, and the host lacks the image's toolchain.
   - **Auth in the container:** macOS keeps the OAuth token in the Keychain. The container needs `CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token` ("Set up a long-lived authentication token (requires Claude subscription)"). That command is interactive and is Jacob's step. Harbor supports it (`CLAUDE_FORCE_OAUTH` plus `CLAUDE_CODE_OAUTH_TOKEN`, `claude_code.py:146`). `.env` has no such key now.
   - **Context hygiene:** the toy ran inside the repo, so Claude Code attached `AGENTS.md` and Jacob's `MEMORY.md`. The base context jumped to about 78k tokens; transcript lines 11–18 have the attachments. In the container with `cwd=/app` and an empty `CLAUDE_CONFIG_DIR`, none of that is loaded. Measure the real base context in the first container run, since it sets how much room is left below the threshold.
2. **Simulator.** The config is `{"mcpServers": {"user-server": {"type": "http", "url": "http://<host>:<port>/mcp"}}}`, passed with `--mcp-config <file> --strict-mcp-config`. Harbor writes the same entry to `$CLAUDE_CONFIG_DIR/.claude.json` (`claude_code.py:1920`). Init reported `mcp_servers: [{name: user-server, status: connected}]`.
   - The tool loads deferred, via ToolSearch. It is named `mcp__user-server__ask_user` in the transcript, so the scorer has to map that name to `ask_user`.
3. **Compaction.**
   - **Forced:** `claude -p "/compact" --resume <id>` emitted `compact_boundary` (`trigger: manual, pre_tokens 78776, post_tokens 6562`). `claude_compact.jsonl`.
   - **Automatic, threshold settable:** `CLAUDE_CODE_AUTO_COMPACT_WINDOW=<n>` (or the `autoCompactWindow` setting; strings in the binary also show `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`). With the window at `40k` and at `1000`, a resumed turn auto-compacted (`trigger: auto`). `claude_auto_*.jsonl`.
     - The trigger sits a buffer below the window, and the exact buffer wasn't measured. So set the window to give a 150k trigger, and check the first boundary's `pre_tokens`.
     - Haiku 5.5's `contextWindow` is reported as 1,000,000, so without an override it would never compact on DeepSWE.
   - **Where the summary lives:** the session JSONL at `~/.claude/projects/<cwd-slug>/<session>.jsonl`, or `$CLAUDE_CONFIG_DIR/projects/...`. It holds a `system/compact_boundary` line (with `compactMetadata`: `preTokens`, `postTokens`, `preservedSegment` and `preservedMessages` uuids), then a `user` line with `isCompactSummary: true`. That line's content is the summary in **plain text**, in Claude Code's 8-section format ("Primary Request and Intent … All user messages … Pending Tasks … Current Work"). `claude_session_after_compact.jsonl:63–64`.
   - **Claude Code keeps a preserved segment verbatim:** the last few messages, whose uuids are in `preservedMessages`. A loss judged against the summary alone has to treat the preserved tail as visible.
   - **Hooks:** a `--settings` file with `PreCompact` and `PostCompact` hooks fired on auto-compaction. The PostCompact payload carries `transcript_path`, `trigger` and **`compact_summary`** (the text). `hook.log`, `hook_post_in.json`. This is the native snapshot point: the hook runs inside the container and can write the workspace tree (`GIT_INDEX_FILE=… git add -A && git write-tree`), or block until the host has done `docker commit` before the next tool call.
     - The simulator's state can be rebuilt afterwards from its ask log (`SimState.from_ask_log`).
4. **Snapshot and fork.**
   - **The test:** copy the post-compaction session JSONL under a new uuid, change PELICAN to FALCON in the 4 places after the boundary (the summary and the preserved segment; 7 earlier mentions left as they were), then resume: `claude -p "Without calling any tools: what secret word…" --resume <edited-id> --fork-session`.
   - **The answer was `FALCON`,** under a new session id. `claude_fork.json`, `claude_session_edited.jsonl`. So a repair arm can edit the summary (or add an item) and fork natively. The original file stays untouched.
   - Harbor's `load_trajectory` does the same thing for a whole saved session (`claude_code.py:361–395`, `2194–2196`).
   - **A quirk, not investigated:** the hand-edited session itself didn't auto-compact at a 1000-token window, but its fork (`--fork-session`) did. Arms run as forks, so this doesn't block them.
5. **Billing and limits.**
   - **Billing:** Max (`apiKeySource: none`, `overageStatus: rejected`, `org_level_disabled`), so there are no API charges. Claude Code still reports a list-price `total_cost_usd`; the toy was $0.016 for 4 calls at about 78k context.
   - **Limits, from `rate_limit_event` at 2:31 PM ET:**
     - the **five-hour window was 12% used** (resets 6:00 PM ET today);
     - the **seven-day window was already 66% used** (resets **Sun 25 Oct, 8:00 PM ET**).
   - **Cost of 25 sessions** (an estimate, not measured): about 300 calls per session at about 100k average context is about 30M input tokens, nearly all cache reads.
     - Haiku 5.5 ($0.10/$0.50 per MTok up to 100k-token prompts, $0.50/$2.50 above) comes to roughly $2 per session, about **$50 list-equivalent for 25**.
     - Sonnet 5.5 ($2/$10, cache read $0.20) comes to roughly $10 per session, about **$250 for 25**.

     What share of Max's weekly allowance that uses is unknown, and the week is two-thirds gone. Run Haiku first, and pace runs from the `unifiedWindows.seven_day.utilization` field.
   - **The simulator** (GPT-5.5 via OpenRouter) is paid, whatever the harness.
   - **Terms:** Jacob accepted the question of using Max for headless work on 20 Oct (memory `max-plan-for-claude-work`). Each container is one more consumer of his token, so expect five-hour limits to bite if many sessions run in parallel.
6. **Telemetry.** Every `assistant` event in stream-json, and every assistant line in the transcript, carries `usage` (`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`, `thinking_tokens`). The `result` event has `modelUsage` with `contextWindow`. `compact_boundary` has `pre_tokens` and `post_tokens`. That covers per-call cost and context size.

## Codex

1. **Headless.**
   - The command was `codex exec -m gpt-6-luna -c model_reasoning_effort=low -c 'mcp_servers.user-server.url="http://127.0.0.1:18765/mcp"' -c model_auto_compact_token_limit=20000 --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --json "<prompt>" </dev/null`.
   - It called `ask_user`, then wrote `word.txt` = PELICAN. `codex_run1.jsonl`.
   - **Auth:** `~/.codex/auth.json` has `auth_mode: chatgpt`. It is a file, so it can be copied into the container; Harbor does this (`CODEX_AUTH_JSON_PATH` / `CODEX_FORCE_AUTH_JSON`, `codex.py:1947`).
   - **A risk, inferred and unchecked:** several containers refreshing one ChatGPT refresh token at once may invalidate each other. Run sessions with one shared, writable auth file, or a few at a time.
   - The CLI installs and starts in the DeepSWE image (above).
2. **Simulator.** `-c 'mcp_servers.user-server.url="http://<host>:<port>/mcp"'`, or in `config.toml` as `[mcp_servers.user-server] url = "…"`. Streamable HTTP is inferred from the URL. It worked (`item.completed mcp_tool_call ask_user`).
3. **Compaction.**
   - **Threshold settable:** `-c model_auto_compact_token_limit=<n>`. Resuming at 8000 triggered compaction: `event_msg item_completed ContextCompaction` and a `compacted` rollout line. `codex_run2.jsonl`, `codex_rollout_after_compact.jsonl:33,49`.
   - **Where it is written:** the rollout `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl` gets a `compacted` line with `message: ""` and a `replacement_history`. That history has:
     - every **user message, verbatim** (the task prompt, environment context);
     - the developer messages;
     - one item `{"type": "compaction", "encrypted_content": <2.6–2.9k chars>}`.

     **The summary itself is encrypted.** ChatGPT auth uses OpenAI's remote compaction. No feature flag selects local compaction for the OpenAI provider: `codex features list` shows `remote_compaction_v2 removed`. Local, readable compaction happens only with a non-OpenAI provider, which means paid API and not the subscription model.
   - **What this means for SWE-Interact:** the maintainer's requirements arrive as `ask_user` tool outputs, not user messages, so they sit inside the encrypted summary. Q1 can't grade Codex's summary from text. The only route is indirect: ask the resumed agent, a probe of its own.
   - **Hooks:** the binary lists `pre_compact` and `post_compact` hook events (strings). They were not checked live.
4. **Snapshot and fork.**
   - **The test:** copy the rollout under a new uuid (all id strings replaced), edit the last `compacted.replacement_history`, then `codex exec resume <new-id> "Without running any tools…: what is the secret word?"`. `codex_edit_ids.txt`, `codex_fork_*.jsonl`.
     - **Append** a user message "Correction from the user: the secret word is FALCON." after the encrypted item: the answer was **PELICAN**. The edit loaded, but the model kept the encrypted summary's word (n=1).
     - **Replace** the encrypted item with a plain summary saying FALCON: the answer was **FALCON**.
   - So edited rollouts are honoured, and a native fork is possible. But a repair either sits next to an unreadable summary (append; its strength is unknown) or replaces Codex's summary with ours (a mixed setup).
   - `codex exec fork` and `codex fork` also exist (help files saved). Harbor seeds rollouts for `codex exec resume` (`codex.py:462–468`).
5. **Billing and limits.**
   - **Billing:** ChatGPT plan `prolite`, from the rollout's `rate_limits`. The weekly window was **15% used** and resets **Fri 30 Oct, 2:12 PM ET**: the day of the final presentation, so one weekly allowance covers the rest of the sprint.
   - **Cost of 25 sessions:** about 30M input tokens each, as for Claude. gpt-6-luna is $0.10/$0.50 at list, so about $3 per session and **$75 list-equivalent for 25**, drawn from the plan's weekly allowance at an unknown rate.
   - The toy's base context was about 16k per call: Jacob's Codex skills and plugins were loaded. Use a clean `CODEX_HOME` in the container.
6. **Telemetry.**
   - The rollout's `event_msg token_count` has `last_token_usage` (input, cached_input, output, reasoning) and `total_token_usage` per call, plus the rate-limit block.
   - `turn.completed` in `--json` has the turn totals.

## Antigravity (`agy`)

1. **Headless.**
   - The command was `agy -p "<prompt>" --model gemini-3.8-flash-low --mode accept-edits --sandbox --output-format json --print-timeout 180s </dev/null`, with the workspace as cwd. It worked (`agy_run3.json`: DONE, `word.txt` = PELICAN).
   - **In the container:**
     - Harbor has an `antigravity-cli` agent, but it authenticates with `GEMINI_API_KEY` only (`antigravity_cli.py:115`). An API key is not Jacob's free quota.
     - The OAuth login is a file, `~/.gemini/oauth_creds.json`, so copying it into a Linux container might work. That is unchecked: the agy binary here is Mach-O arm64, and the Linux install wasn't tried.
     - The privacy rule from `agy.py` and memory applies. Everything goes to Google, so only ever run it on public task repos.
2. **Simulator.** `agy mcp add user-server http://<host>:<port>/mcp`. This edits the global agy config (I removed it afterwards).
   - Headless mode **auto-denies MCP calls** ("a tool required the "mcp" permission that headless mode cannot prompt for") unless `settings.json` has `"permissions": {"allow": ["mcp(user-server/ask_user)"]}`. The forms `mcp(user-server)` and plain `mcp` don't work.
   - Jacob's `~/.gemini/antigravity-cli/settings.json` was backed up and restored byte for byte (`agy_settings_backup.json`).
3. **Compaction.**
   - **Forced:** `agy -p "/compact" --conversation <id>` writes a CHECKPOINT step. Its content is **readable**: "# Resuming from a compaction", the verbatim user requests, then a `<summary>` with Task Overview / Progress… that quoted the `ask_user` reply word for word. `agy_transcript_after_compact.jsonl`, step 10.
   - **The automatic threshold can't be set.** The binary has `CompactionConfig` and `ContextWindowCompressionConfig` types but no user setting. The 20 Oct note puts the trigger at about 128–256k.
   - **A driver can force `/compact` only between `-p` turns.** One SWE-Interact step is one long turn, so a 150k cut can't be imposed mid-step.
   - The conversation's real state is a SQLite DB, `~/.gemini/antigravity-cli/conversations/<id>.db`: tables `steps`, `gen_metadata`, `trajectory_meta` and others, with protobuf blobs. `transcript_full.jsonl` is a log derived from it.
4. **Snapshot and fork.**
   - **The test:**
     1. Copy the DB to `<new-uuid>.db`.
     2. Change `trajectory_meta.cascade_id` to the new id.
     3. Replace PELICAN with OSPREYS (the same length) in `steps.step_payload` where `idx=10`, the checkpoint.
     4. Run `agy -p "Without calling any tools: what is the secret word?" --conversation <new-uuid>`.
   - **The answer was `OSPREYS`.** `agy_fork.json`, `agy_fork_id.txt`. So a fork from an edited checkpoint works.
   - A real repair changes the text's length, so it needs the protobuf's nested length fields rewritten (blackboxprotobuf or a hand varint walk). `gen_metadata` also caches the prompt (PELICAN appears in row 3), and how agy uses that cache is unknown.
5. **Billing and limits.** The free plan quota, per Jacob on 20 Oct; the remaining quota isn't visible from the CLI. The result's `usage` has no cache counts, so the cost of 25 sessions in quota is unknown.
6. **Telemetry.** The result `usage` totals cover the whole conversation, not one turn (input, output, thinking, `cache_read_tokens: 0`). The transcript has per-step token counts (`src/compaction/agy.py` docstring). The `/compact` call itself is not in `usage`.

## OpenCode (optional fifth; not re-run)

This section comes from `docs/sprint/prep/k-pi-opencode-spike.md` (20 Oct, $1.05):
- OpenCode 1.19.3 runs headless in the DeepSWE container through inspect-swe's agent. Set `OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER=1`, or it hangs at exit.
- The threshold is set by shrinking the model's `limit.context` with `OPENCODE_CONFIG_CONTENT`.
- The summary is readable in SQLite (`opencode export`, written to a file).
- Forking from a mid-compaction snapshot is malformed. It needs a post-compaction snapshot plus a SQLite edit, or `opencode run --session <id> --fork`.
- MCP: Harbor's `opencode.py` registers task MCP servers (`mcp_servers=True`).
- **OpenCode has no subscription model of its own.** Its "native" model would be DeepSeek or GLM on paid API, the same cost as the pilot.

## Shared work every native harness needs

1. **The simulator as an MCP server:** wrap `UserSim` in a FastMCP streamable-HTTP server on the host, keeping its repo commands on `docker exec`, and reach it from the container through `host.docker.internal`.
   - This replaces the in-process call our pi loop makes, and the hidden persona stays out of the container, as now.
   - Use one server per session, so the state is per session.
   - Export the state at each boundary (the brief's existing `SimState`).
2. **Network:** the CLIs need egress to their API, but our harness runs task containers with `--network none`.
   - **Options:** an egress allowlist (an HTTP proxy for api.anthropic.com, chatgpt.com/api.openai.com, the MCP host), or a public network as the paper used (`network_mode = "public"` in `task.toml`).
   - **Upstream solutions:** with a public network the agent can fetch the upstream repo's solution. The paper accepted that risk; we should log any outbound fetches.
3. **The step loop:** five steps (`01_plan` … `05_test_handoff`), each a resumed headless invocation, as Harbor's multi-step trial does. Plus a parser from transcript or rollout into our run records.
4. **Snapshots:** a PostCompact hook (Claude) or post_compact hook (Codex, unchecked) records the workspace tree, the session file and the simulator state at each boundary.

## Cleanup

- The stub server was stopped (port 18765 free).
- The agy MCP entry was removed and its settings restored.
- No containers of mine are left. `docker ps --filter label=emx.sandbox` was empty, and the only running container is the pilot's.
- **Left in Jacob's local session stores, harmless:** test sessions in `~/.claude/projects/-…-native-check-toy/`, the `~/.codex/sessions/2026/10/24/rollout-*01a16f2e*` rollout and its two edited copies, and three agy conversations (`ab63657f…`, `3400bc8d…`, plus two from denied runs).
