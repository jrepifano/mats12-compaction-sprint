# Sprint prep (k): pi and OpenCode as native harnesses for DeepSeek and GLM

**Verdict: both work. pi is the better research harness, but neither is the vendors' native tool.** OpenCode 1.19.3 runs headless in the DeepSWE container through inspect-swe's `opencode` agent, on DeepSeek V4.1 Flash and on GLM 5.3 Flash. pi 1.3.0 runs the same way through a small Inspect agent we wrote, about 70 lines, since pi has no inspect-swe agent. Both compacted natively on both models at a context limit we set, and the bridge filter snapshotted the container at each compaction call.

pi is easier to fork: its own `--fork` resumed a snapshot, and DeepSeek received our substituted summary plus a restored rule. Its session file also records every compaction. OpenCode needs two fixes:
- Its process hangs after the final answer unless its file watcher is disabled.
- Resuming from a mid-compaction snapshot gives it a malformed context.

**Both vendors now ship their own harness,** listed first in their docs: Z.ai's **ZCode** ("GLM-5.3 官方 Harness", i.e. official harness) and **DeepSeek Harness** (`dsh`). Both have a headless CLI and native compaction. Neither was run here. If "native" means what each vendor ships, those two win, and they need their own spike.

Spend **$1.05** of the $3 cap. 2026-10-20, UTC 19:00–20:24. Prep unit (k), building on [f-inspect-swe-spike.md](f-inspect-swe-spike.md) (the bridge, filter and fork method), [c-deepswe.md](c-deepswe.md) (task image, grading) and [e-agy-providers.md](e-agy-providers.md) (routes). Spike files are in `data/raw/sprint/spike_pi_opencode/` (gitignored):
- `spike_po.py`: the tasks `po_spike` and `po_fork`, the pi agent, the capture filter, the mock model and the scorer;
- `mock_server.py`: a local OpenAI-compatible mock for $0 host debugging;
- `analyze.py`, `post_ctx.py`, `show_log.py`, `cost.py`, `u.sh`;
- the Inspect logs in `logs/` and the capture records in `captures/`;
- the read-only source checkouts in `src/` (OpenCode v1.19.3 `e3c4eb2`, pi v1.3.0 `71023ae`).

No commits.

## Answers first

| | OpenCode | pi |
|---|---|---|
| Package, version | `anomalyco/opencode` (formerly sst/opencode) **1.19.3**, linux-x64-baseline binary staged by inspect-swe | **`@earendil-works/pi-coding-agent` 1.3.0** (repo `earendil-works/pi`). `@mariozechner/pi-coding-agent` stopped at 0.73.1 (May 2026) and is deprecated: "please use @earendil-works/pi-coding-agent instead" |
| In inspect-swe 0.2.71? | **Yes**, `inspect_swe.opencode` | **No.** Our `pi_agent` in `spike_po.py` stages pi, writes `models.json` pointing at the bridge, and runs `pi --mode json` |
| Headless in DeepSWE container | **Works** on DeepSeek and GLM. Needs `OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER=1`, or the process never exits after the answer | **Works** on DeepSeek and GLM, and exits cleanly |
| Native compaction fired | Yes. DeepSeek: 10 compactions in 15 min at a 48k trigger (a loop, see below). GLM: once, at 80.0k | Yes. DeepSeek: 14 compactions in 15 min at a 47.6k trigger. GLM: once, at about 70k plus pi's trailing estimate |
| Summary in the Inspect log | Compaction call = `ModelEvent` with no tools; its output is the summary. The next call's input is `[system, user "What did we do so far?", assistant <summary>, kept tail…, user "Continue if you have next steps…"]`. No `CompactionEvent` | Compaction call = `ModelEvent` with no tools; output = summary. The next call's input is `[system, user "The conversation history before this point was compacted into the following summary: <summary>…</summary>", kept tail…]`. No `CompactionEvent` |
| Summary in the harness's own store | SQLite `~/.local/share/opencode/opencode.db`. `opencode export <session>` gives every message, the compaction marker (`tail_start_id`) and the summary message (`mode: compaction`, token counts) | Append-only JSONL `~/.pi/agent/sessions/<ts>_<id>.jsonl`. Every original message is kept, plus a `compaction` entry: `summary`, `firstKeptEntryId`, `tokensBefore`, `usage`, `details.readFiles/modifiedFiles`, `systemMessage` |
| Snapshot at the compaction call | Yes, with the bridge filter + `docker commit --no-pause`: 12–14 s | Yes, the same: 6 s |
| Fork from the snapshot | **Partial.** inspect-swe resumes with `--continue`. From a mid-compaction snapshot, OpenCode attaches our summary to the new prompt and keeps the full history (malformed). Needs a post-compaction snapshot plus a SQLite edit, or a custom resume | **Works.** `pi --fork <session> "Continue."` makes a new session from the snapshot's file. Full-history arm: mock. Summary arm: **real DeepSeek got our summary + "RULE RESTORED…"** |
| Cost | DeepSeek $0.264 (97 calls) · GLM $0.083 (37 calls) · exit checks $0.0005 | DeepSeek $0.414 (106 calls) · GLM $0.112 (45 calls) · fork $0.179 (47 calls) |

**Mock checks first.** Every mechanism was checked for $0 before any paid run:
- in the container, with `mockllm` scripted to call each harness's `bash` tool with inflated usage: container start, harness install, bridge, tools, native compaction, filter snapshot, grading, and the fork mechanics of both harnesses;
- on the host, against `mock_server.py` in a scratch workspace: the debugging of pi's skipped compaction and OpenCode's exit.

## 1. What DeepSeek and GLM coding users run (vendor docs)

I checked these with curl on 2026-10-20 (re-checked after a sub-agent's search). A sub-agent marked this with an asterisk read the page through WebFetch.

**Z.ai (GLM):**
- **Its own harness is ZCode.** zcode.z.ai's page title is "ZCode | GLM-5.3 官方 Harness" ("official harness").
  - The repo `zai-org/ZCode` (Apache-2.0, created 2026-09-20) describes itself as "Z.ai's coding agent harness".
  - Releases ship desktop builds only (`ZCode-3.14.3-mac-arm64.dmg`, `-win-x64.exe`). The repo also contains a terminal agent CLI, `apps/zcode-cli`: `zcode --prompt … --output-format stream-json`, `--resume`, and docs on batch evaluation and trajectory export. The CLI is a **source build**; there is no npm package.
  - Its compaction is a full compact plus microcompact, with a Docker e2e suite (`apps/zcode-cli/e2e/compact-microcompact/`).
- **Coding Plan overview** (https://docs.z.ai/devpack/overview): "The plan can be applied to agent tools such as ZCode, Claude Code, Codex, OpenCode, AutoClaw, and OpenClaw", and "Beyond Claude Code, it also supports ZCode, Cline, OpenCode, AutoClaw, and other supported tools".
- **Tool list** (https://docs.z.ai/devpack/tool/others) starts "ZCode (1.5× Usage) … Claude Code … Claude for IDE … Codex … OpenCode …". The sub-agent* read the rest of the order as Pi, Cursor, Cline, TRAE, Qoder, Droid, Kilo, Roo, Crush, Goose.
- **pi has its own page** (https://docs.z.ai/devpack/tool/pi, HTTP 200); the sub-agent* quoted "a minimalist terminal coding harness".
- **Quick Start*** (https://docs.z.ai/devpack/quick-start) lists Claude Code, Roo Code, Kilo Code, Cline, OpenCode, ….

**DeepSeek:**
- **Its own harness is DeepSeek Harness** (`dsh`).
  - It is the first entry under "Agent Integrations" on https://api-docs.deepseek.com/. The sidebar reads: "DeepSeek Harness | Claude Code | Codex | OpenCode | OpenClaw | Hermes | Reasonix | WorkBuddy/CodeBuddy | Qoder".
  - The repo `deepseek-ai/deepseek-harness` is MIT-licensed, created 2026-08-13, described as "DeepSeek Harness: Everything is a Plugin." and labelled "developer preview … THERE WILL BE COMPATIBILITY-BREAKING CHANGES".
  - npm: `@deepseek-ai/dsh` 0.2.1-rc.2.
  - It has `dsh headless "<task>"` with `--json` run events and `--session-id` resume.
  - Its compaction package is `dsh-compaction-basic`:
    - **trigger** `floor(min(W×0.8, W−O−65,536))`, configurable (`thresholdRatio`, `headroomTokens`, `retainRatio`/`retainTokens`, per-model policies);
    - **retention:** it keeps the newest 16% of `W−O` verbatim;
    - **logging:** it writes `compaction/start|summary|end` events, with the raw summarizer output, to the session log.
- **pi page** (https://api-docs.deepseek.com/quick_start/agent_integrations/pi_mono): "Pi (pi-mono) is a minimal, aggressively extensible terminal coding harness." It is not in the sidebar.
- **Claude Code*** (https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code) points `ANTHROPIC_BASE_URL` at `https://api.deepseek.com/anthropic` with `deepseek-flash`. The OpenCode page returns HTTP 200.

**Reading:**
- For both vendors, the first-party harness is listed first, and Claude Code is the most prominent third-party tool.
- OpenCode is documented by both vendors. pi is documented by both, but less prominently: it is absent from DeepSeek's sidebar.
- OpenCode also ships both vendors as providers, including `zai-coding-plan/glm-5.3-flash` and `deepseek/deepseek-flash`. pi ships `deepseek/deepseek-flash` but not GLM 5.3 Flash under `zai`, so Z.ai's page has users add it.
- **No usage-share data:** OpenRouter's app rankings render client-side and could not be read.

## 2. OpenCode

**How inspect-swe runs it** (`inspect_swe/_opencode/opencode.py`):
1. Stages the release binary on the host, checksum-verified, with node and ripgrep; no network is needed in the container.
2. Writes `~/.config/opencode/opencode.json` with `provider.<id>.options.baseURL = http://localhost:<port>/v1` (the bridge).
3. Runs `opencode run --model <opencode_model> --format json --title=… --dangerously-skip-permissions`, with the prompt on stdin.

Three things to set:
- **`opencode_model` defaults to `anthropic/claude-sonnet-4-5`,** and OpenCode picks its system prompt from the model id (`session/system.ts`: claude → `anthropic.txt`, gpt → `gpt.txt`, …, else `default.txt`). DeepSeek and GLM users get `default.txt`, so pass the vendor id: `deepseek/deepseek-flash` or `zai/glm-5.3-flash`. The bridge routes any name to the eval model.
- **Compaction threshold:** shrink the model's context limit with `OPENCODE_CONFIG_CONTENT='{"provider":{"deepseek":{"models":{"deepseek-flash":{"limit":{"context":64000,"output":16000}}}}}}'`. It merges over inspect-swe's config file.
- **Exit hang:** without `OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER=1` (I also set `OPENCODE_DISABLE_FFF=1`; the two weren't separated), OpenCode logs "exiting loop" and "disposing instance" after the final answer, and the process never exits. Every sample then ends at the time limit, and post-agent solvers don't run.
  - Shown on real DeepSeek with a trivial prompt: it answered "DONE", then hung for 240 s. With the flags, the sample took 25 s.
  - It did not happen on the macOS host, so it is probably inotify on the container's large `/app`.

**Compaction mechanics** (source read, v1.19.3; confirmed in the logs):
- **Trigger** (`session/overflow.ts`): after each step, `tokens.total ≥ usable`.
  - `usable` is `limit.input − reserved` when the model has an input limit, else `context − min(limit.output, 32k)`.
  - `compaction.reserved` defaults to `min(20,000, max output)`.
  - Config: `compaction.auto` (default true), `prune` (default false), `tail_turns`, `preserve_recent_tokens`, `reserved`; `OPENCODE_DISABLE_AUTOCOMPACT=1`.
  - **With the real 1M windows of DeepSeek and GLM, the default trigger is about 968k or more, so it would never fire on DeepSWE's 240k peaks.**
- **Summarizer request:**
  - The system prompt is "You are a context summarization agent…".
  - The single user message is `<conversation>` + the serialized history (`[User]:`, `[Assistant]:`, `[Assistant tool call]:`, `[Tool result]:`, **each tool result cut to 2,000 characters**) + "Create a new anchored summary from the conversation history…". On later compactions it adds `<prior-summary>` and update rules.
  - Template: Objective / Important Details / Work State (Completed, Active, Blocked) / Next Move / Relevant Files.
  - No tools.
- **What it keeps verbatim:**
  - The most recent turns within `preserve_recent_tokens`, which defaults to 25% of `usable`, clamped to 2k–15k.
  - A single-prompt headless run has one user turn, so OpenCode splits it: **the original task message is not kept verbatim**, only its paraphrase in the summary.
  - It then appends a synthetic user message, "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.".
  - AGENTS.md is in the system prompt, rebuilt every call, so it survives. The koota `kebab-case` rule was present after compaction.
- **Post-compaction size** was 9.6k–22k on DeepSeek, against a 48k trigger, which caused the loop below.

**Fork:**
- The filter snapshots exactly at the compaction call.
- Resuming via inspect-swe uses `opencode run --continue` + a new prompt. The snapshot's DB holds a half-written compaction: the marker message plus an empty summary message. OpenCode then summarized again (our filter substituted the text), but parented that summary to our "Continue." message.
- So its context had **the full original context (26 messages, system included) + "What did we do so far?" + "Continue." + our summary + the continue prompt** (mock, `po_fork` log 19:55).
- **Ways to fix it:**
  - snapshot at the first call *after* compaction and edit the summary row in SQLite before resuming;
  - use `opencode run --session <id> --fork` (not exposed by inspect-swe);
  - or substitute in place during the natural run (the filter, as in spike f).
- **Gotcha:** `opencode export` truncates at 131,072 bytes when stdout is a pipe; redirect it to a file.

## 3. pi

**Package and modes:**
- The CLI is `pi` (`dist/bundle/cli.js`) and needs node ≥ 22.19 (koota's image has node 24.12).
- Headless modes: `-p/--print`, `--mode json` (JSONL events, including `compaction_start`/`compaction_end` with the summary) and `--mode rpc`.
- Sessions: `--session`, `--session-id`, `--fork <path|id>`, `--session-dir`, `--no-session`.
- **In a non-TTY it waits for stdin EOF** (piped stdin is prepended to the prompt), so give it empty input.
- Set `PI_OFFLINE=1`, `PI_TELEMETRY=0`, `PI_SKIP_VERSION_CHECK=1` and `enableInstallTelemetry: false`.
- There is no Inspect integration upstream: no inspect-swe agent and no Inspect package.

**Model config:**
- `~/.pi/agent/models.json` sets the vendor provider's `baseUrl` to the bridge, `apiKey: "sk-none"`, and `modelOverrides: {"deepseek-flash": {"contextWindow": 64000}}`. That keeps pi's built-in first-party model and changes only the endpoint and window.
- GLM 5.3 Flash isn't built in under `zai`, so it is a `models` entry with `api: "openai-completions"`, which is what Z.ai's pi page has users do.
- pi's system prompt doesn't depend on the model; it is about 3.5k characters, AGENTS.md included.

**Compaction mechanics** (source read, v1.3.0; P49's pinned pi prompts from `b06c1f2` are byte-identical):
- **Trigger:** `contextTokens > contextWindow − reserveTokens`, with `reserveTokens` 16,384. pi checks after tool results, before the next assistant call, and before a new user prompt. Settings: `compaction.enabled`, `reserveTokens`, `keepRecentTokens` (20,000), per-model overrides.
  - With 1M windows, the default trigger is about 984k: it would never fire on DeepSWE.
- **Cut:** walk back by chars/4 estimates to `keepRecentTokens`, and cut at a user or assistant message. **If everything fits in the keep budget, pi silently skips compaction.** The mock first did this; real content is large enough. A single-prompt run always splits its one turn, so pi makes:
  - a history summary ("No prior history." when empty);
  - a **turn-prefix summary** (Original Request / Progress So Far / Context Needed to Continue).

  Later compactions use the update prompt (Goal / Constraints & Preferences / Progress / Key Decisions / Next Steps / Critical Context, "PRESERVE all existing information").
- **Summarizer request:**
  - The system prompt is "You are a context summarization assistant…".
  - The user message is `<conversation>`/`# Conversation` + the serialized history + the instructions.
  - `max_tokens` is 0.8×reserve, i.e. 13,107 (or 8,192 for the turn prefix). Reasoning is requested at high.
- **What it keeps verbatim:** the system prompt (with AGENTS.md), then one user message wrapping the summary in `<summary>`, then the last ~20k tokens of messages. The original task message is not kept verbatim. In the DeepSeek run the "Original Request" section paraphrased it closely.
  - **The summary grows:** 1.8k → 10k output tokens over 14 compactions, because the update prompt preserves everything.
- **pi clamps `max_tokens` to the room left** in its stated window (down to 1). A shrunken window therefore also shrinks output budgets near the trigger.

**Fork (works):**
- The snapshot holds the session JSONL as of the compaction call.
- `po_fork` runs `pi --fork <newest session file> "Continue."`, which gives a new session with the full history; the original file is untouched.
  - **Full-history arm:** resume at the same window (mock: 24 messages + "Continue.").
  - **Summary arm:** resume at a window whose trigger is below the snapshot's context (50k). pi compacts before the prompt, and the filter returns our text.
- **Real DeepSeek check:**
  - The fork from `pi-ds1:compact1` sent DeepSeek its own earlier summary + "RULE RESTORED - do not modify any file under skills/." (checked in the first `ModelEvent`, 9 messages).
  - It then re-compacted 23 times in 4 minutes, because 50k sits too close to the floor and the filter substituted every compaction. Override only the first one, as spike f also concluded.
- **Alternative:** edit the `compaction` entry's `summary` in the JSONL directly. It is plain text and the entry is self-contained.

## 4. The re-compaction loop (both harnesses, DeepSeek)

At a ~48k trigger, the post-compaction context was 10–22k for OpenCode and 18–38k for pi. DeepSeek reads whole files (`cat` of 20–27k-character test files), so both harnesses re-compacted every 7–9 calls: OpenCode 10 times and pi 14 times in 15 minutes.
- pi's floor rises with each compaction (keep 20k + a growing summary).
- At an 80k trigger, GLM compacted once in 10 minutes in each harness.
- **As in spike f, the trigger must sit well above the post-compaction floor** (pi: about 20k kept + summary + about 1–4k system; OpenCode: about 10–22k) and below the planned boundary. With a 1M window, every harness here, and dsh, needs a forced threshold to compact at DeepSWE lengths.

## 5. What a pi driver needs (our loop, modelled on `src/compaction/agy.py`)

`pi_agent` in `spike_po.py` is already the core of a driver (about 70 lines). For the sprint it needs:
1. **Install:** pin the npm tarball's sha256 and stage it the way inspect-swe stages binaries. Go and Python images may lack node 22, so use inspect-swe's `ensure_node_available`.
2. **Config:** `models.json` (endpoint, window), `settings.json` (`reserveTokens`, `keepRecentTokens`, telemetry off), `PI_OFFLINE`, empty stdin.
3. **Events:** parse the `--mode json` JSONL, which is `turn_*`, `message_*`, `tool_execution_*`, `compaction_start/end` (with `summary`, `firstKeptEntryId`, `tokensBefore`) and `agent_settled`. Record an Inspect `info` event per compaction; there is no `CompactionEvent` today.
4. **Records:** read the session JSONL into `CompactionRecord`s, as `agy.checkpoints()` does from `transcript_full.jsonl`. The compaction entry gives `summary`, `firstKeptEntryId` (the boundary), `tokensBefore` (pi's own estimate) and summarizer `usage`, and the tree gives `source_messages` and `post_messages`. Validate strictly, as agy's transcript reader does.
5. **Fork:** `--fork`, plus a resume window, plus a first-compaction-only filter override, or an edit of the JSONL `summary`.
6. **Cleanup:** pi spawns `bash` tool children. Keep `timeout -k`, as here, plus the container teardown.

**OpenCode** needs no driver, since inspect-swe has the agent. It needs:
- the watcher flag;
- the vendor `opencode_model`;
- a config-content threshold;
- an export step that writes to a file;
- a different fork route (section 2).

## 6. Comparison

| | OpenCode | pi |
|---|---|---|
| Realism for DeepSeek and GLM users | Documented by both vendors, prominently by DeepSeek (4th in its sidebar). Ships both vendors' providers, including the Z.ai Coding Plan. Popular general-purpose harness | Documented by both, less prominently (absent from DeepSeek's sidebar). Ships `deepseek-flash`; GLM 5.3 Flash needs a `models.json` entry. Minimalist |
| Integration effort | Low: the inspect-swe agent exists. Four settings: watcher flag, model id, threshold via `OPENCODE_CONFIG_CONTENT`, export to file | Low to medium: our ~70-line Inspect agent works now, and a sprint driver adds pinning, event parsing and records (section 5) |
| Compaction observability | Good: summarizer call and post-context in `ModelEvent`s; full history + marker + summary in SQLite via `opencode export` (truncation gotcha) | Best: same `ModelEvent`s, plus `compaction_end` in the event stream, plus an append-only JSONL with every message and a structured compaction entry |
| Fork support | Partial: snapshot works, resume from a mid-compaction snapshot is malformed; needs SQLite editing or a non-inspect-swe resume | Works: native `--fork` of a session file; summary arm verified on real DeepSeek |

## 7. Recommendation

1. **The vendor paths win on realism, so spike them before the pilot, on our side and not as a product claim yet.**
   - DeepSeek Harness (`npx @deepseek-ai/dsh headless --json`) is the strongest candidate. It is on npm, has a headless JSON mode and session resume, and logs its compaction lock, summary and raw output to the session log. Its threshold is configurable.
   - ZCode's CLI exists but is a source build (pnpm). Its README says Linux and real-provider runs were "not individually verified".
   - Both are developer-preview grade. Budget about half a day and $1–2 for each, with the same mock-first recipe.
   - If they don't run headless in the container, DeepSeek and GLM stay **cross-model**, as the sprint plan already says.
2. **Of the two spiked here, use pi as the DeepSeek and GLM harness for the controlled and native arms:**
   - its fork works now;
   - its session JSONL gives exact compaction records;
   - its prompts are the ones P49 replayed, so offline replay and online native runs share a summarizer.

   Label it "pi (third-party, vendor-documented)", not "native".
3. **Keep OpenCode as a second harness if harness diversity is wanted.** It is cheap through inspect-swe. Use it for in-place substitution arms, not fork arms, until the SQLite-edit route is built.
4. **Pilot settings, for any of these:**
   - Force the trigger at about 150–200k with a 1M window: pi `contextWindow`, OpenCode `limit.context`, dsh `thresholdRatio`/`headroomTokens`.
   - Keep the defaults for reserve and keep-recent.
   - Override only the first compaction in forks.
   - Disable OpenCode's file watcher.
   - Pin `opencode` 1.19.3, `pi` 1.3.0 and their sha256.
   - **Use time or cost limits, not `token_limit`** (spike f).

**Caveat for every bridged harness: generation settings don't reach the model.** The bridge runs with `forward_generation_config=False`. pi's reasoning level (`medium`, `high` for summaries) and `max_tokens`, and OpenCode's output limit, are therefore not forwarded. DeepSeek and GLM ran with Inspect's defaults; GLM spent 32–53k reasoning tokens per run. Decide whether to forward them.

## 8. Cost and time

| UTC | Run | Model, route | Calls | Tokens (fresh in / cache read / out) | $ | Wall-clock |
|---|---|---|---|---|---|---|
| 19:07–19:36 | 3 OpenCode + 2 pi container mocks; host debugging against `mock_server.py` | mockllm / local mock | — | — | 0 | 0.4–10 min each (OpenCode mocks hung to their time limits) |
| 19:37 | OpenCode, trigger 48k | DeepSeek V4.1 Flash, DeepSeek API (`deepseek-flash`) | 97 | 530k / 2,306k / 76k | 0.2635 | 15 min (time limit) |
| 19:37 | pi, trigger 47.6k | same | 106 | 763k / 2,613k / 141k | 0.4139 | 15 min (time limit) |
| 19:54–19:55 | 3 fork mocks (pi ×2, OpenCode) | mockllm | — | — | 0 | 0.2–4 min |
| 20:00–20:07 | 3 OpenCode exit checks, trivial prompt | DeepSeek API | 4 | — | 0.0005 | 5 min, 4 min (hung), 25 s (watcher off) |
| 20:08 | OpenCode, trigger 80k, graded | GLM 5.3 Flash, OpenRouter pinned to Together | 37 | 255k / 889k / 36k | 0.0831 | 10 min (time limit); reward 0, empty patch |
| 20:08 | pi, trigger 79.6k, graded | same | 45 | 200k / 1,722k / 60k | 0.1116 | 10 min (time limit); reward 0, empty patch |
| 20:19 | pi fork, summary + rule | DeepSeek API | 47 | — | 0.1794 | 4 min (time limit) |
| | **Total** | | | | **1.052** | about 85 min |

**Ledger:** recorded in this worktree's `logs/costs.jsonl` as exp SPRINT, notes starting "spike_pi_opencode", 8 rows.
- One row is $0: the second exit check's log holds no model events. Its 2 calls, about $0.0003, are unrecorded.

**Pricing:** list prices on Inspect's token counts.
- DeepSeek: $0.30 / $0.006 / $1.20 per M (in / cache read / out), the **peak** rate. These runs fell in the evening UTC, possibly off-peak, so the real charge may be lower.
- GLM on Together: $0.15 / $0.03 / $0.50.
- OpenRouter's `usage.cost` was present in 5 of 37 and 5 of 45 calls.

**Versions:**
- Python side: inspect-ai 0.3.282, inspect-swe 0.2.71, inspect-scout 0.6.0, openai 3.35.1.
- OpenCode 1.19.3: the linux-x64-baseline binary for the container; darwin-arm64 for host debugging, zip sha256 `c2d0fecd…bb64`.
- pi `@earendil-works/pi-coding-agent` 1.3.0, staged from `pi-1.3.0-node_modules.tgz`, 26 MB.
- dsh `@deepseek-ai/dsh` 0.2.1-rc.2: installed for reading only, never run.
- Docker 29.4.1; node 24.12.0 in the koota image.

**Cleanup:**
- No containers left (`docker ps -a --filter label=emx.sandbox` is empty).
- All `emx-spike-po/*` snapshot images deleted; the koota task image is kept.
- `mock_server.py` processes stopped.
- Laptop load stayed at 2.4–4.0.

**Gotcha:** `set -a; . ./.env` prints "command not found: H100/A100" for lines 18–20 of `.env`, which are not shell syntax. The keys still load.

## 9. Exact commands

From the worktree root. `U=data/raw/sprint/spike_pi_opencode/u.sh` wraps `uv run --no-project --with inspect-ai==0.3.282 --with inspect-swe==0.2.71 --with inspect-scout==0.6.0 --with openai==3.35.1 --with-editable .`, and `S=data/raw/sprint/spike_pi_opencode`. Keys come from `set -a; . ./.env; set +a`; DeepSeek also needs `export DEEPSEEK_BASE_URL=https://api.deepseek.com`.

```bash
# $0 plumbing checks
$U inspect eval $S/spike_po.py@po_spike -T harness=opencode -T mock=true -T grade_it=false --log-dir $S/logs
$U inspect eval $S/spike_po.py@po_spike -T harness=pi -T mock=true -T pi_keep=20 --log-dir $S/logs
# OpenCode on DeepSeek V4.1 Flash (DeepSeek API); add the watcher flag (it was missing in this run)
$U inspect eval $S/spike_po.py@po_spike -T harness=opencode -T vendor=deepseek -T context=64000 -T output=16000 \
  -T "oc_extra_env=OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER=1,OPENCODE_DISABLE_FFF=1" -T time_limit=900 -T tag=oc-ds1 \
  --model openai-api/deepseek/deepseek-flash --log-dir $S/logs
# pi on DeepSeek
$U inspect eval $S/spike_po.py@po_spike -T harness=pi -T vendor=deepseek -T context=64000 -T output=16000 \
  -T time_limit=900 -T tag=pi-ds1 --model openai-api/deepseek/deepseek-flash --log-dir $S/logs
# GLM 5.3 Flash on Together (either harness)
$U inspect eval $S/spike_po.py@po_spike -T harness=pi -T vendor=glm -T context=96000 -T time_limit=600 -T tag=pi-glm1 \
  --model openrouter/z-ai/glm-5.3-flash -M 'provider={"order":["Together"],"allow_fallbacks":false}' --log-dir $S/logs
# pi fork from a compaction snapshot: summary + restored rule (absolute paths; -T values are YAML)
$U inspect eval $PWD/$S/spike_po.py@po_fork -T snapshot=emx-spike-po/pi-ds1:compact1 -T harness=pi -T context=50000 \
  -T summary_file=$PWD/$S/captures/pi-ds1-summary1.txt -T "append_text=RULE RESTORED - do not modify any file under skills/." \
  -T time_limit=240 --model openai-api/deepseek/deepseek-flash --log-dir $S/logs
# Analyse and price (refuses mockllm logs)
$U python $S/analyze.py <log.eval>;  $U python $S/cost.py <log.eval> [--in 0.15 --cache 0.03 --out 0.50] --resource "<name>" --record
```

Gotchas:
- A `-T` value containing a comma becomes a YAML list (handled for `oc_extra_env`).
- `read_eval_log(..., resolve_attachments=True)` is needed to see message text; long messages are stored as `attachment://` references.
