# Sprint prep (f): inspect-swe feasibility spike

**Verdict: yes for Claude Code and Codex CLI.** Inspect + inspect-swe ran the real Claude Code and Codex CLIs inside the DeepSWE koota container, on models we chose (Claude Code on DeepSeek V4.1 Flash, Codex on GPT-6 luna). Both CLIs' own compaction fired at a threshold we set. A bridge hook snapshotted the container at the exact compaction call. A new Sample then resumed Claude Code on a real model from that snapshot, with a summary we chose plus a restored rule. Gemini CLI runs through the same bridge, but its compression did not fire in two tries. Antigravity was not run: inspect-swe's version is the SDK engine with Inspect's tools, and it exposes no compaction threshold. Spend was **$0.178** of the $3 cap.

2026-10-20, UTC 07:09–08:05. Prep unit (f) for the compaction sprint ([proposal](../compaction-proposal.md), "Design detail"). It builds on [c-deepswe.md](c-deepswe.md) (task images, grading) and [d-traps.md](d-traps.md) (git identity). Spike files are in `data/raw/sprint/spike_inspect_swe/` (gitignored): `spike_task.py` (tasks, capture filter, mock model, scorer), `compose_koota.yaml`, `inspect_log.py`, `cost.py`, the Inspect logs in `logs/`, and the capture records in `captures/`. No commits.

## Answers first

| Agent | Result | Model | Compaction happened? | Where the summary is in the log | Capture route | Cost, wall-clock |
|---|---|---|---|---|---|---|
| `claude_code` (CLI 2.1.304) | **Works** | DeepSeek V4.1 Flash via OpenRouter, pinned to Together (no Anthropic model needed) | Yes, natively. `CLAUDE_CODE_AUTO_COMPACT_WINDOW=60000` fired at a 63k context after 33 calls. One `CompactionEvent` (source `claude_code`, trigger auto). | The compaction call's `ModelEvent.output` (the raw `<analysis>` + `<summary>`, 4.7k tokens). The next `ModelEvent.input[3]`, the first user message, holds the cleaned summary after "This session is being continued…". **`sample.messages` is not the post-compaction state.** | Bridge `filter` sees the compaction request, then runs `docker commit` (12 s) and dumps the request. **Fork verified on the real model:** `claude --resume` from the snapshot with a substituted summary + rule. | Run $0.055, 3.0 min. Fork $0.080, 3.5 min. |
| `codex_cli` (CLI 0.163.0) | **Works, with caveats** | GPT-6 luna, OpenAI API (our key), reasoning effort low | Yes, natively, with `model_auto_compact_token_limit=18000`. At 40000 the agent finished at 38k without compacting. **At 18000 it re-compacted every 2–3 calls** (14 compactions in 11 min, stopped by hand). | The compaction call (`tools=0`, last user message "You are performing a CONTEXT CHECKPOINT COMPACTION…") has the summary as its output. The next request keeps every original user message verbatim, then adds "Another language model started to solve this problem…" + `<summary>`. | Same filter (12–14 s per snapshot). Fork mechanics checked on mockllm only: `codex exec … resume --last` picks up the snapshot's rollout, and the substituted summary + rule reached the model. | $0.0088 (2.6 min) + $0.0347 (11 min, looping). |
| `gemini_cli` (CLI 0.64.0) | **Partial** | mockllm only (it would take DeepSeek the same way) | No, in two mock runs. A system settings file with `model.compressionThreshold=0.04` (about 42k of the 1M window) set through `GEMINI_CLI_SYSTEM_SETTINGS_PATH` did not trigger compression. Not debugged further. | — | The filter would work the same way. The compression-call marker is unverified. | $0 |
| `antigravity` (SDK 0.1.7) | **Not run** | — | The SDK has `CapabilitiesConfig.compaction_threshold` and an `OnCompactionHook`. inspect-swe's runner does not set the threshold, so the backend default applies, and it would need a patched runner. | — | — | $0 (image with the SDK built, then deleted) |
| `mini_swe_agent` | Not run | — | No native compaction ("This agent does not handle compaction natively"). It takes an Inspect `CompactionStrategy`, i.e. Inspect's summarizer, not a vendor's. | — | — | — |

**Mock checks first.** Every plumbing step was first checked for $0 with `mockllm/model`, scripted to call each CLI's shell tool and to report inflated `usage`, so the CLI's own threshold check fired. The steps covered were:
- container start (amd64 emulated, network none);
- download and install of the CLI binary;
- the bridge;
- tool execution;
- native compaction (Claude Code and Codex);
- the filter snapshot;
- forking from the snapshot (Claude Code: summary arm and full-history arm; Codex: summary arm);
- grading with DeepSWE's collect hook and a fresh verifier container.

On the mock patch the grader scored reward 0 with 172/172 pass-to-pass, as expected.

## 1. How inspect-swe runs the CLIs (source read: inspect-swe 0.2.71, inspect-ai 0.3.282)

- **Real CLI in the sandbox.** `claude_code` and `codex_cli` download the official linux-x64 binary on the host, cache it in `~/Library/Caches/inspect_swe/`, and copy it into the container (`/var/tmp/.5c95…`). No network is needed inside the container, so DeepSWE's `network_mode: none` works. Each run calls `claude --print --output-format stream-json` or `codex exec`. `gemini_cli` installs a node bundle the same way. `antigravity` pip-installs `google-antigravity` into a venv, which needs egress, so for no-network images it must be baked into the image. A derived image built in 17 s.
- **Model bridge.** `sandbox_agent_bridge` runs a proxy inside the container on localhost. It speaks the Anthropic, OpenAI Responses/Completions and Google APIs, and routes every call to the Inspect model. The CLI gets a dummy key: `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` / `GOOGLE_GEMINI_BASE_URL` point at the proxy. **So non-native models work:** Claude Code ran on DeepSeek, and Gemini CLI or Antigravity could run on DeepSeek or GLM. No vendor key ever enters the container.
- **Keys needed (host side only):** whatever Inspect provider serves the model. Here that was `OPENROUTER_API_KEY` for DeepSeek and `OPENAI_API_KEY` for luna. Haiku would need `ANTHROPIC_API_KEY` or OpenRouter.
- **Version pins the spike needed:**
  - `codex_cli` needs `openai>=3.4.0` for the Responses bridge. The `swarm` group pins 2.54, so add `--with "openai>=3.4.0"` (3.35.1 was used).
  - `gemini_cli` needs `google-genai` on the host (2.31.0 was used).
- **Compaction settings:**
  - **Claude Code.** Pass `env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "<tokens>"}`; `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` also exists, and `DISABLE_AUTO_COMPACT=1` turns compaction off. The threshold function in the 2.1.304 binary is `min(window·pct, window − 13000)`, with the window less a max-output reserve. In practice the trigger point is only approximate. Window 60000 fired at 63k. In the fork, window 45000 first fired on resume at 64k, then not until 74k.
  - **Codex.** Pass `config_overrides={"model_auto_compact_token_limit": "<tokens>"}`. Codex compacts below the stated limit (about 12–13k with a limit of 18000).
  - **Gemini CLI.** The threshold is `model.compressionThreshold` × the model's token limit (default 0.5). inspect-swe rewrites `~/.gemini/settings.json` at launch, so the override has to come from a system settings file. That did not work in two tries.
- **Codex's compaction here is the local path.** inspect-swe's custom provider forces Codex's local compaction (the `compact/prompt.md` prompt), because remote compaction is gated to the real OpenAI and Azure providers. With an OpenAI login, real Codex + luna uses server-side compaction, whose summary is encrypted. **Our Codex arm would therefore use Codex's open-source summarizer, not the one luna users get from OpenAI.** The DM's "Codex for luna" has to say which.

## 2. The Inspect task (step 2)

`spike_task.py@deepswe_spike`:
- **Sample:** one, koota-pair-relation-tracking. Input is `instruction.md`.
- **Sandbox:** `("docker", compose_koota.yaml)`, with:
  - the pinned task image, `platform: linux/amd64` and `pull_policy: never`;
  - `network_mode: none`, 2 CPUs and 8 GB;
  - label `emx.sandbox=sprint-spike`;
  - main process `timeout -k 5 5400 sleep infinity`.
- **Setup solver:** runs `git config --global user.name/email` and `safe.directory /app`. It confirmed `main` and `x86_64`.
- **Scorer:** runs the task's own `[[verifier.collect]]` command in the agent container, reads `/logs/artifacts/model.patch`, then calls `compaction.deepswe.sandbox.grade` (a fresh pristine verifier container, `test.sh`, `reward.json`). Grading took about 20 s every time. Each sample took 30–80 s outside model time.
- **inspect-harbor** ([g-inspect-ecosystem.md](g-inspect-ecosystem.md) §5) could replace the hand-built dataset. Its grading, however, resets the agent's own container, while ours uses a fresh container. Keep our scorer.

Harness artefacts found:
- **Codex's home directory.** inspect-swe defaults `CODEX_HOME` to `<cwd>/.codex`, i.e. `/app/.codex`. The mock agent's `git add -A` then committed Codex's config and sessions into the graded patch (677 KB). Set `home_dir="/root/.codex"`.
- **AGENTS.md is a natural rule source.** The koota repo ships an `AGENTS.md` ("Always use kebab-case for file names"; "when updating the README, the skill should be updated as well"). Claude Code 2.1.304 and Codex both load it and re-attach it after compaction. Rules we plant there are never lost to the summarizer. Rules in chat are a different case for each CLI (§3).

## 3. What the logs hold at a compaction

**Claude Code** (DeepSeek run, `logs/2026-10-20T07-24-31…eval`):
- The compaction call is a normal `ModelEvent`: 66 input messages, ending in "CRITICAL: Respond with TEXT ONLY…" + "Your task is to create a detailed summary of the conversation so far…". Its output is the summary, with `<analysis>` kept.
- The next `ModelEvent` is the post-compaction context. Its first user message holds:
  - the AGENTS.md and git-status reminder, with git status **regenerated** at that moment;
  - "This session is being continued…", then `Summary:` with `<analysis>` stripped;
  - **a pointer to the full transcript at `/root/.claude/projects/-app/<session>.jsonl` inside the container**, which the agent can read back, as agy's summary also allows (e-agy-providers.md);
  - "The messages after this summary are the most recent messages from before compaction, kept verbatim".

  After that come the last tool call and its result, plus re-attached file reads ("Called the Read tool with … world.ts").
- A `CompactionEvent` (trigger auto) follows. `tokens_before` was empty in the stream.
- **`sample.messages` (bridge state) ends with the compaction request and the summary, not the post-compaction context.** Analysis must read `ModelEvent`s.

**Codex** (`logs/2026-10-20T07-31-09…eval`):
- The compaction call has no tools; its last user message is the CONTEXT CHECKPOINT prompt.
- The next request contains the system prompt, the skills, the AGENTS.md block, **every original user message verbatim**, then the summary behind a "another language model started to solve this problem" preamble.
- The `CompactionEvent` comes from inspect-swe's consumer.
- **Implication for Q2:** in Codex, a rule stated by the user in chat survives compaction by construction, unless the user messages exceed Codex's user-message budget. Rules Codex can lose are those in tool output, files, or the agent's own discoveries. In Claude Code, chat rules can be lost: they survive only if the summary carries them, apart from the transcript pointer.

## 4. Capture at the compaction point (step 4)

**Route used: the bridge `filter`.** inspect-swe passes `filter=` to `sandbox_agent_bridge`. It is called on the host for every model request, before the request is answered. While it runs, the CLI is blocked waiting for its compaction response, so the container's filesystem is exactly at the compaction point. The filter (`capture_filter` in `spike_task.py`):
1. Recognises the compaction request: Claude Code's "Your task is to create a detailed summary…", Codex's "You are performing a CONTEXT CHECKPOINT COMPACTION.", and a `<state_snapshot>` guess for Gemini, which is unverified.
2. Finds the agent container: `sandbox().as_type(DockerSandboxEnvironment)` gives the compose project and service, and `docker ps --filter label=com.docker.compose.project=…` gives the container id.
3. Runs `docker commit --no-pause -c "LABEL emx.sandbox=sprint-spike" <cid> emx-spike/<tag>:compact<k>`, in **12–17 s** under emulation. Each snapshot is about 1 GB of new layer: it includes the 290 MB Claude Code binary and the node_modules changes.
4. Writes the request (every message) to `captures/<tag>-compact<k>-request.json` and appends a row to `captures/captures.jsonl`.
5. Optionally **replaces the summary in place**:
   - `summary_override`: returns a fixed `ModelOutput` without calling the model;
   - `append_text`: calls the model, then appends text to the summary the CLI receives.

**Use `--no-pause`.** With Docker's default (pause), the bridge's own `docker exec` polling failed during the commit ("container is paused"). The run recovered, but no-pause avoids it. The trade-off: without the pause, sqlite WAL files in Codex's home (`state_5.sqlite-wal`) could be caught mid-write.

**Fork into a new Sample (`spike_task.py@deepswe_fork`).**
- **Claude Code.** The snapshot holds the repo and Claude Code's pre-compaction session JSONL. inspect-swe allocates a fresh session id per agent, so we patch `uuid4` at construction to the snapshot's id. We also give the Sample an assistant turn, so inspect-swe launches `claude --resume <id> -- "Continue."`. Two arms:
  - `arm=full`: `DISABLE_AUTO_COMPACT=1`. The model received all 17 messages of history (mock).
  - `arm=summary`: a low window makes Claude Code compact again on resume, and the filter returns our fixed text.

  **Real-model check:** the fork from the DeepSeek snapshot sent DeepSeek its own earlier summary + "RULE RESTORED - do not modify any file under skills/" (checked in the first `ModelEvent`). DeepSeek then worked for 15 calls and was stopped by the token limit.
- **Codex.** inspect-swe resumes with `codex exec … resume --last`, which picks the newest rollout in `CODEX_HOME`, so no id patching is needed. The substituted summary + rule reached the model (mock).

**Caveats for the design:**
- **The fork's context is not identical to the natural continuation.** In the fork, the kept-verbatim tail becomes a synthetic "No response requested." assistant turn plus our "Continue." user turn. In the natural run it was the last tool call and its result. All arms share this, so arm contrasts are clean. But "actual summary" in a fork is not exactly what the agent saw in the original run. Report the original run's continuation separately.
- **Override only the first compaction.** The filter currently substitutes every compaction in a fork; the DeepSeek fork compacted a second time and got the same text again.
- **Thresholds must sit well above the post-compaction floor:** about 31k for Claude Code (system prompt and tools ~15k, plus summary and re-attachments) and about 10k for Codex. Otherwise the CLI re-compacts every few turns: Codex did 14 compactions in 11 minutes, and the mock Codex fork compacted 141 times. For a resume to compact exactly once, the threshold also has to sit below the snapshot's context. Calibrate both in the pilot.
- **Inspect's limits count oddly here.** `token_limit` counts cache reads: 1.5M was reached in 3 minutes of DeepSeek at $0.05. Use a cost limit or a time limit instead. `message_limit` counts the resumed history: a fork with 71 restored messages hit `message_limit=40` before its first call.
- **Substituted outputs leave no `ModelEvent`,** and so no `CompactionEvent` for Codex. The capture row in `captures.jsonl` is the record; add an Inspect `info` event or store entry in the filter for the real harness.

**Other routes considered:**
- **Inspect's checkpointer** (`CheckpointConfig`, restic or tar snapshots of sandbox paths, `Manual` trigger plus `cp.checkpoint()`) is built for crash-resume of the same sample, not for N-way forks.
- **Claude Code's `PreCompact` hook** runs inside the container and cannot run `docker commit`.

The filter route is simpler and exact.

## 5. Subscription auth (asked by Jacob mid-spike; read from source, not tried)

- **Claude Code on a Claude Max plan.** inspect-swe always points Claude Code at the bridge, and Inspect's Anthropic provider accepts `ANTHROPIC_AUTH_TOKEN` as an OAuth bearer with the `oauth-2025-04-20` beta header (`anthropic.py:612`). A `claude setup-token` token could therefore serve the bridged calls from the host, and the token would never enter the container. Whether automated research runs are allowed on a Max plan is a terms question for Jacob. Not tested, and no login was used.
- **Codex on a ChatGPT login.** Not possible through inspect-swe. It forces `preferred_auth_method="apikey"` and the bridge provider, and Inspect's OpenAI provider has no ChatGPT-login path. Using the login would mean copying `auth.json` into a networked container and bypassing the bridge, which drops the Inspect model events and the filter. Not done, per Jacob's rule.
- **Gemini and Antigravity:** both are bridged to the Inspect model, so no Google auth is needed for non-Gemini models. Gemini models themselves are blocked on our OpenRouter key.

## 6. Cost and time

| UTC | Run | Model | Calls | Tokens (fresh in / cache read / out) | $ | Wall-clock |
|---|---|---|---|---|---|---|
| 07:15–07:24 | 4 mock runs: Claude Code, 2 forks, Codex | mockllm | — | — | 0 | 0.6–1.3 min each |
| 07:24 | Claude Code, window 60000 | DeepSeek V4.1 Flash (Together) | 40 | 102k / 1,408k / 13k | 0.0549 | 3.0 min (token limit) |
| 07:27 | Codex, limit 40000 | luna (OpenAI direct) | 14 | 39k / 368k / 2.5k | 0.0088 | 2.6 min (agent finished) |
| 07:31 | Codex, limit 18000 | luna | 49 | 223k / 436k / 16k | 0.0347 | 11.6 min (looping; stopped) |
| 07:44 | Codex fork, mock | mockllm | 142 | — | 0 | 11 min (looping; stopped) |
| 07:56 | Claude Code fork, summary + rule | DeepSeek V4.1 Flash | 20 | 156k / 879k / 23k | 0.0796 | 3.5 min (token limit) |
| 08:00 | 3 Gemini CLI mock runs | mockllm | — | — | 0 | about 1 min each |
| | **Total** | | | | **0.178** | about 55 min |

Recorded in this worktree's `logs/costs.jsonl` as exp SPRINT, with notes starting "spike_inspect_swe":
- The first two rows undercounted uncached input and have correction rows.
- Two of my own wrong rows were removed: a duplicate of the 07:31 run, and a mock run priced as DeepSeek.

Costs come from list prices on Inspect's token counts: DeepSeek $0.30/$0.006/$1.20 per M (in / cache read / out), luna $0.10/$0.01/$0.50. OpenRouter's reported `usage.cost` was in only 5 of 40 calls.

**Versions:** inspect-ai 0.3.282, inspect-swe 0.2.71, inspect-scout 0.6.0, openai 3.35.1, anthropic 1.9.0, google-genai 2.31.0; Claude Code 2.1.304, Codex CLI 0.163.0, Gemini CLI 0.64.0, google-antigravity 0.1.7; Docker 29.4.1.

**Cleanup:** every container was removed (`docker ps -a` empty, including `--filter label=emx.sandbox`), and all `emx-spike/*` images were deleted. The koota and wasmi task images are kept. Laptop load stayed at 4–7.5.

## 7. Exact commands

From the worktree root. The keys are loaded with `set -a; . ./.env; set +a`. `U` stands for `uv run --group swarm --with inspect-swe --with inspect-scout --with "openai>=3.4.0"` (add `--with google-genai` for Gemini), and `S=data/raw/sprint/spike_inspect_swe`.

```bash
# $0 plumbing check (Claude Code; use agent=codex_cli or gemini_cli for the others)
$U inspect eval $S/spike_task.py@deepswe_spike -T agent=claude_code -T mock=true --log-dir $S/logs
# Claude Code on DeepSeek V4.1 Flash, native compaction near 60k
$U inspect eval $S/spike_task.py@deepswe_spike -T agent=claude_code -T tag=cc-deepseek \
  -T message_limit=120 -T token_limit=1500000 -T time_limit=1800 \
  --model openrouter/deepseek/deepseek-v4.1-flash -M 'provider={"order":["Together"],"allow_fallbacks":false}' --log-dir $S/logs
# Codex on luna (40000 did not fire; 18000 looped)
$U inspect eval $S/spike_task.py@deepswe_spike -T agent=codex_cli -T tag=codex-luna2 -T codex_limit=18000 \
  -T token_limit=2000000 -T time_limit=1500 --model openai/gpt-6-luna --reasoning-effort low --log-dir $S/logs
# Fork from the compaction snapshot: summary + restored rule (session id from /root/.claude/projects/-app/)
$U inspect eval $PWD/$S/spike_task.py@deepswe_fork -T snapshot=emx-spike/cc-deepseek:compact1 \
  -T session_id=570ee67d-d045-43eb-ac5a-347a3daa7a11 -T arm=summary -T compact_window=45000 \
  -T summary_file=$PWD/$S/captures/cc-deepseek-summary1.txt -T "append_text=RULE RESTORED - do not modify any file under skills/." \
  -T message_limit=110 --token-limit 1000000 --model openrouter/deepseek/deepseek-v4.1-flash -M 'provider={...}' --log-dir $S/logs
# Inspect a log, then price and record it (refuses mockllm logs)
$U python $S/inspect_log.py <log.eval>;  $U python $S/cost.py <log.eval> --resource "<name>" --record
```

Gotchas:
- `-T` values are parsed as YAML, so a colon in `append_text` turns it into a dict.
- Inspect runs the task file from its own directory, so pass absolute paths for `summary_file`.

## 8. Recommendation for the sprint harness

**A hybrid, with Inspect + inspect-swe as the main path for the vendor-summarizer arms, and our own loop (c-deepswe §4) kept for controlled replays.**

**Why Inspect + inspect-swe for the main path:**
- It gives the real Claude Code and Codex, compacting natively, on any model through the bridge. That covers Claude Code on Haiku, and also on DeepSeek or GLM if we want one harness across models.
- Every model call is logged as a `ModelEvent`. The compaction summary and the post-compaction context are there, and the CLI's own `CompactionEvent`s mark the boundaries.
- The bridge filter is one hook that snapshots the container at the exact compaction call and can substitute the summary in place.
- Session-resume forks give the four resume arms: full history, actual summary, summary + rule and summary + matched fact. Verified on a real model for Claude Code; mechanics only for Codex.
- DeepSWE grading plugs in as a scorer unchanged. A run costs cents at the cheap-model prices, and our own loop would have to reimplement each CLI's prompts, tools and compaction.

**Why keep our own loop:**
- Codex's arm in inspect-swe is the open-source local summarizer, not luna's server-side one.
- Antigravity's native harness isn't usable through inspect-swe without patching the runner, and Gemini CLI's compression trigger isn't working yet.
- Our loop is the reliable way to replay the Codex, pi and Antigravity summarizer styles on fixed histories (the offline audit and the method comparison), and to run models that have no vendor CLI.

**Before the pilot:**
1. Set each CLI's threshold well above its post-compaction floor and below the planned boundary (about 150–200k).
2. Make the filter override only the first compaction, and log an Inspect event for substituted outputs.
3. Use time and cost limits rather than `token_limit` or `message_limit`.
4. Set Codex `home_dir` outside `/app`.
5. Decide whether the "actual summary" arm is the fork or the natural continuation.
6. Pin the CLI versions (`version=` on each agent).
7. Budget about 1 GB of disk and about 15 s per snapshot. Delete snapshots once their arms have run.
8. Ask Jacob whether a Claude Max token may serve the Haiku and Opus arms through Inspect's `ANTHROPIC_AUTH_TOKEN` path.
