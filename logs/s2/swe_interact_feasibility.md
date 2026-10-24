FORK: PASS for our in-harness port of the simulator (stub model, real task container); the released sidecar itself can't be restored without a patch · INTEGRATION: about 8–10 h on top of the chained-pilot harness work (repeated compaction, kept snapshots) · SIMULATOR: about $0.01–0.03 per conversational reply and $0.05–0.17 per review reply, measured; about $0.5–1.5 per session projected

# SWE-Interact feasibility check

2026-10-24, s2-feas (sub-agent of the S2 session), brief `logs/s2/briefs/swe-interact-feasibility.md`. Paid spend: **$0.33** of the $2 limit. That was 8 GPT-5.5 simulator replies plus 4 refused (unbilled) tool rounds, all booked to the ledger under stage SPRINT-S2. No commits. No containers left (`docker ps -a --filter label=emx.sandbox=s2-interact` is empty after every run).

**Sources.** SWE-Interact @ `b32f98c3` (2026-06-30) and Harbor @ `6099b8cf` (2026-10-23) are cloned under `data/raw/sprint/swe_interact/` (gitignored). The derived tables and run outputs are in `data/raw/sprint/swe_interact/derived/`. The code is in `src/compaction/s2/interact.py`, with tests in `tests/compaction/test_s2_interact.py` (8 pass, about 11–18 s, including the Docker fork test).

## The answer in five lines

1. All 25 DeepSWE tasks are in our checkout. Only `clack-async-autocomplete-options` is held out. None is in the pilot cohort or the replacements. The spec and verifier are DeepSWE's (§1).
2. The simulator's whole state is its chat transcript plus a call counter. Each Harbor step starts the agent fresh, and the simulator carries over. A pilot needs `01_plan` + `02_implement`, then our own DeepSWE verifier (§2).
3. The cheapest path is to run the simulator in our process: a port of `server.py`, request-for-request identical to upstream (tested). It inspects the agent's container with `docker exec`. That keeps our loop, compaction routes, thresholds and snapshots. Running Harbor itself is much more work (§3).
4. The fork works for the port. Arms restored from a boundary file and image match the pre-fork state on every check and stay isolated (§4). Scale's released sidecar has no import path, so forking *it* needs a small patch or a replay (§4.3).
5. Cost, measured on kombu: replies are cheap. A review reply with 3 tool rounds cost $0.17. **But the released route probably never uses its tools on today's API.** OpenAI chat completions refused tools together with `reasoning_effort` for GPT-5.5 (HTTP 400). Every reply then fell back silently to a plain completion, which sees only the auto-snapshot. Through OpenRouter pinned to OpenAI the tool rounds work. This is a fidelity choice the plan must make and log (§5).

## 1. Tasks

Full table: `derived/task_table.json`, built by `derived/task_table.py`. The peak columns are the median of 4 single-turn, uncompacted mini-swe-agent rollouts per config (`trials.json` `peak_context_tokens`), in thousands of tokens. The table is sorted by the DeepSeek V4 Flash peak.

| task | rank in our 45 | held-out | chain-inventory repo | lang | DS V4 Flash | luna max | GLM 5.3 Flash |
|---|---|---|---|---|---|---|---|
| boa-hierarchical-evaluation-cancellation | 17 | | | rust | 242 | 345 | 170 |
| testem-bail-on-test-failure | 22 | | testem | javascript | 231 | 256 | 145 |
| helm-array-merge-strategies | 26 | | helm | go | 223 | 240 | 129 |
| fastapi-implicit-head-options | 29 | | fastapi | python | 219 | 234 | 129 |
| kombu-single-active-consumer-priority | – | | kombu | python | 216 | 199 | 111 |
| vitest-duration-sharding | 40 | | | typescript | 210 | 231 | 156 |
| pwntools-tube-multiplexing | – | | | python | 208 | 126 | 154 |
| clack-async-autocomplete-options | 44 | **yes** | | typescript | 204 | 223 | 126 |
| sqlite-utils-safe-import-checkpoints | 45 | | | python | 201 | 207 | 122 |
| httpx-deterministic-cookie-store | – | | httpx | typescript | 199 | 150 | 91 |
| mobly-grouped-test-barriers | – | | | python | 198 | 209 | 131 |
| tomlkit-toml-table-converters | – | | | python | 195 | 222 | 253 |
| returns-validated-error-accumulation | – | | | python | 191 | 170 | 106 |
| etree-xml-diff-patch | – | | | go | 188 | 149 | 111 |
| drizzle-orm-window-function-builders | – | | | typescript | 184 | 199 | 102 |
| arcane-drift-detection-baselines | – | | | go | 182 | 198 | 126 |
| fastapi-deprecation-response-headers | – | | fastapi | python | 176 | 191 | 108 |
| bandit-structured-nosec-directives | – | | bandit | python | 174 | 183 | 121 |
| ofetch-per-origin-circuit-breaker | – | | | typescript | 174 | 127 | 81 |
| awilix-async-container-initialization | – | | | typescript | 173 | 151 | 116 |
| cattrs-partial-structuring-recovery | – | | | python | 167 | 185 | 107 |
| httpx-streaming-json-iteration | – | | httpx | python | 167 | 132 | 92 |
| kcp-go-multiplexed-kcp-streams | – | | | go | 154 | 123 | 135 |
| participle-grammar-conflict-analysis | – | | | go | 147 | 138 | 103 |
| wazero-multi-module-snapshots | – | | | go | 130 | 98 | 87 |

None of the 25 is in the pilot cohort (`tasks.pilot_cohort`) or the replacements.

**Same tasks as ours, apart from three details:**

- **Spec.** SWE-Interact's `instruction.md` equals ours without our trailing line "IMPORTANT: Please work on this in a new branch from main and commit everything when you are done." 13 files differ only by that line and the final newline, and 12 are identical. The persona's task block is that instruction verbatim. The agent never sees it.
- **Verifier.** Only step 05 is scored (`multi_step_reward_strategy = "final"`; steps 01–04 write reward 1). It resets to the implementation commit, applies DeepSWE's `test.patch` and runs the canonical tests (`steps/05_test_handoff/tests/test.sh`). 20 of the 25 `test.patch` files are byte-identical to ours. The other 5 (httpx×2, kcp-go, kombu, sqlite-utils) differ by our v1.1 flake fixes (extra `--ignore`/`--deselect`, and a polling fix in kcp). Use ours.
- **Images.** `task.toml` names the un-suffixed ECR tag, e.g. `…:kh71kat2v58yys3pnyybkgycax832vj2`: 26 layers, 1.0 GB. But the run configs pass `--force-build`, so Harbor builds `environment/Dockerfile`. That build starts from `mars-base:latest` (unpinned) and does a plain `git clone` + `checkout`, which **does not strip future history**: the reference solution's commits are reachable with `git log --all`. Our `-v1.1` tags (31 layers, 2.14 GB) are the same base commit with history gc'd, plus the v1.1 scoring tools. **Recommend our v1.1 images and verifiers, logged as a deviation.**

## 2. The simulator interface (read verbatim)

- **Transport.** `ask_user(question)` is a FastMCP tool (streamable HTTP, `user-server:8000/mcp`) in a sidecar built from `environment/user-server/` (python:3.12-slim + fastmcp + litellm). `server.py` is byte-identical across the 25 tasks, and only `persona.md` differs.
- **State.** Two globals: `conversation` (agent question as `user`, simulator reply as `assistant`) and `ask_user_call_count` (`server.py:52-53`).
  - The tool rounds inside one reply use a scratch `working` list (`server.py:385`) and are not kept.
  - The persona and hidden spec are a static file.
  - There is **no disclosure bookkeeping**. What has been revealed is implicit in the conversation.
  - Sampling: `reasoning_effort="high"`, `max_tokens=32768`, no temperature or seed, model `SIM_USER_MODEL` (`openai/gpt-5.5` in every multi-turn run config; the code's default is `anthropic/claude-opus-4-7`).
- **Inspection.**
  - **Auto-snapshot.** If the question contains done / implemented / fixed / changed / commit / diff / review / ship / tests pass / key changes, the server attaches `git status --short` and `git show --patch --stat HEAD` (≤ 50k chars) as a system message (`server.py:220-260`).
  - **Tool rounds.** The model may then make up to 6 rounds of `run_repo_command`. That runs an allowlisted, shell-less argv (git status/diff/show/grep/log/rev-parse/ls-files/cat-file, rg, grep, sed, find, ls) through `repo_exec_server.py` on `main:8765` (`repo_exec_server.py:19-30,113-130`).
  - The simulator mostly reads **committed** state.
- **Logs and export.** Each call appends to `/tmp/user-server-logs/{ask-user,tool-calls,events}.jsonl`. A GET-only export server on :8001 returns them (`server.py:489-529`). **There is no import or restore endpoint.**
- **What the agent sees.**
  - It sees each step's `instruction.md` only:
    - `01_plan`: talk to the maintainer, save the agreed plan to `/tmp/plan.md`, stop.
    - `02_implement`: read the plan, save `/tmp/base_commit.txt`, commit each pass, ask for review until "ship it".
    - `03`/`05`: handoff scripts.
    - `04`: write tests.
  - It doesn't see the spec, the persona or the simulator's inspection.
- **Step context.** Each Harbor step starts a **fresh agent conversation** by default: `resume_trajectory: bool = False`, Harbor `src/harbor/models/trial/config.py:119`, used in `trial/multi_step.py:397-404`. SWE-Interact's configs don't set it. The simulator's conversation persists across steps (one sidecar per trial), so the maintainer remembers the planning talk and the agent doesn't.
- **What a pilot needs.**
  - `01_plan` is a short conversation that produces `/tmp/plan.md`.
  - `02_implement` is where the length, the review loop and so any boundary are.
  - `03`–`05` only produce the final score. Grading the last implementation commit with our DeepSWE v1.1 verifier gives the same score without them: 05 resets to `HEAD~1` = the implementation commit and runs the same `test.patch`.

## 3. Integration path

**Recommended: the simulator in our process.** `interact.UserSim` is a port of `server.py`. Its repo commands run in the agent's container through `docker exec`, harness-side with the trap shims off, under the same allowlist and the same argv (`shlex.join` through `bash -lc` reproduces upstream's shell-less argv; tested). So:

- the task container keeps `--network none`;
- the hidden spec never enters it;
- no MCP or sidecar is needed;
- our loop, compaction routes, thresholds, snapshots and ledgered model client apply unchanged.

**Parity with the released code** (`test_requests_match_released_server`): upstream `server.py` and `repo_exec_server.py` were run in-process, with litellm and fastmcp stubbed, against the same repo and the same scripted model as the port. Over 4 asks (2 with auto-snapshots, each with a tool round), every request upstream sent equals the port's: messages, tools, `tool_choice`, `max_tokens`, `reasoning_effort` and model. The replies, the final conversation and the call count are equal too, and `SimState.from_ask_log` rebuilds the state from upstream's real `ask-user.jsonl`. What is *not* covered is the model client: litellm vs our OpenAI SDK, and the route problem in §5.

**Remaining changes, each an estimate:**

| change | where | hours |
|---|---|---|
| A run mode that swaps unit (d)'s `ask_user` stub for `interact.ASK_USER_TOOL` (the MCP tool's name and description) and routes `env.ask_user` to the run's `UserSim`. No rules channel, no lures (no planting), trap snapshots kept | `loop.py` (TOOLS chosen per run), `env.py` (ask_user delegation) | 1–1.5 |
| Step driver: 01_plan as its own conversation (step instruction verbatim; ends at submit or after `/tmp/plan.md` exists), then 02_implement as a **fresh** conversation (step instruction verbatim), with the same simulator; the stage recorded per call and in the boundary | new runner function beside `run_base` | 2 |
| Boundary + arms: `boundary.json` gains `sim` (`UserSim.export()`), `stage` and the workspace fingerprint; each arm restores its own `UserSim` over its own forked container (`interact.capture`/`Boundary`/`UserSim.restore` already do this) | `make_boundary`, `run_arm` | 1.5 |
| Readout hooks for astra r2's primary readout: stop an arm at its first `ask_user` (don't send it), at submission or after 60 calls; the secondary readout lets each arm's simulator answer | `run_arm` | 1 |
| Simulator route in `config.py` (OpenRouter pinned OpenAI, `reasoning.effort=high`, $5/$30/$0.50 per M) plus a per-session sim spend line | `config.py` | 0.5 |
| Grade the final implementation commit with our DeepSWE v1.1 verifier; skip 03–05 | reuse the S2 verifier path | 1 |
| Tests (stub end-to-end: 01 → 02 → boundary → 2 arms), astra review and fixes | tests | 2 |
| **Total** | | **≈ 8–10 h** |

This assumes the chained-pilot harness work (repeated in-loop compaction, kept snapshots; `docs/sprint/plans/chained-pilot.md`) is built once and shared; it is not counted here.

**Note.** The 01/02 step instructions say "MCP server at `http://user-server:8000/mcp`". Under our harness `ask_user` is a native tool, so either keep the line verbatim (harmless) or drop it as a logged deviation.

**Not recommended: Harbor itself.**

- Its README runs on Modal, and its local Docker + compose-sidecar path is unverified here.
- Our four compaction routes, thresholds and per-turn trap snapshots don't exist in Harbor's agents.
- Forking would need a container snapshot plus a sidecar restore anyway (§4.3).

Estimate: ≥ 2 days, with unknowns.

## 4. Fork test

### 4.1 What ran

`tests/compaction/test_s2_interact.py::test_fork_in_docker`. Report: `derived/fork_report.json`. Image: a local DeepSWE v1.1 task image (koota), since none of the 25 was pulled at the time. The mechanics don't depend on the repo.

1. Start the container under `Invocation(purpose="s2-interact")`: `--rm`, `--name`, label `emx.sandbox=s2-interact`, an internal `timeout`, removed on exit.
2. Write `/tmp/plan.md`. The simulator answers one planning question (01_plan's talk).
3. A stub agent runs through our real `loop.Loop` with `interact.InteractEnv`:
   - reads the plan and writes `/tmp/base_commit.txt`;
   - commits an implementation;
   - asks for review (the simulator auto-snapshots the repo and runs a tool round);
   - leaves an untracked scratch file.
4. **Boundary** (`interact.capture`): the workspace fingerprint, the quiesce check, then `docker commit`. The agent messages, the stage and `UserSim.export()` go to `boundary.json`.
5. **Two arms** each restore from the JSON file and the image: their own container and their own `UserSim`. The base container is removed first, so neither arm can lean on it.

The simulator model is a deterministic stub. Round 1 runs `git log`; round 2 replies with a hash of the whole request. So equal replies mean equal requests.

### 4.2 Results (all true)

| check | arm A | arm B |
|---|---|---|
| Workspace fingerprint equals pre-fork: HEAD, `git status --porcelain --untracked-files=all`, sha256 of every tracked and untracked file, `/tmp/plan.md`, `/tmp/base_commit.txt` | ✓ | ✓ |
| Agent's next request (messages + tools) equals pre-fork | ✓ | ✓ |
| Simulator's next request (incl. its fresh auto-snapshot of the arm's repo) equals pre-fork | ✓ | ✓ |
| Untracked file and `/tmp/plan.md` kept | ✓ | ✓ |
| Hidden spec not in the container (grep of /app, /tmp, /root) | ✓ | ✓ |

**Isolation.** Arm A then commits and asks again. A's repo and simulator move on (call count +1; its next simulator request differs). B's repo fingerprint, simulator state and next simulator request are unchanged. B's reply to the same question differs from A's, because A's simulator saw A's commit.

### 4.3 Scope of the verdict

- **It covers our port, not the released sidecar.** Scale's `server.py` keeps its state in process memory and offers export only (GET :8001), so a container snapshot of the sidecar does not carry its state.
  - Forking a run made with the *real* sidecar would need either a ~20-line `POST /import-state` patch to `server.py`, or replaying the log into the port.
  - `SimState.from_ask_log` rebuilds the exact state from the real `ask-user.jsonl` (tested), so a run started on the sidecar could be continued on the port.
- **The fork check used a stub model.** Real GPT-5.5 replies won't repeat across arms. That is expected and fine: astra r2 asks only for identical requests before the fork, not identical replies.
- **The port matches upstream request for request** (§3, parity test). The one place it can depart is the model route (§5). It now records a failed tool round in `rounds` instead of hiding it; upstream logs it to `events.jsonl`.

## 5. Simulator cost and the route

**Measured** on kombu (`derived/measure_sim.py`, output `derived/sim_measure_*.json`): its own v1.1 container and SWE-Interact persona; the released config (GPT-5.5, high effort, ≤ 6 tool rounds) through the port. Four asks: opening, plan approval, review after half the reference solution was committed (entity.py and messaging.py), and a re-review after the rest.

**Run 1: OpenAI chat completions (`api.openai.com`), as the run configs point litellm.**

- Every tool round returned **HTTP 400** (4 of 4; unbilled). Our luna route hit the same refusal: chat completions don't take function tools with a reasoning effort.
- Every reply fell back to upstream's plain completion: no `run_repo_command`, only the auto-snapshot (kept as a system message by the fallback's flattening).
- Replies were still in character and grounded: the review reply named the missing BrokerState and consumer-dispatch work. 4 replies cost $0.103.
- **Inference, unverified:** litellm 1.106.3's GPT-5 chat transform has no bridge to the Responses API for tools with reasoning (`llms/openai/chat/gpt_5_transformation.py`). So the released setup probably runs fallback-only on today's API. Whether it did in June can't be checked without Scale's `events.jsonl` (look for `llm_tool_round_error`).

**Run 2: OpenRouter pinned to OpenAI, `reasoning.effort=high` (the luna precedent).** Tool rounds work. Cost source is provider + BYOK upstream, and the rates match OpenAI's standard tier ($5 in / $30 out / $0.50 cached per M):

| reply | rounds | prompt tok (cached) | output tok (reasoning) | $ |
|---|---|---|---|---|
| opening | 1 | 3,294 (0) | 78 (31) | 0.019 |
| plan approval | 1 | 3,380 (2,560) | 27 (0) | 0.006 |
| review, half done | 1 | 5,318 (2,560) | 399 (313) | 0.027 |
| re-review, all files | 3 (5 tool calls) | 44,272 (24,064) | 2,012 (1,609) | 0.173 |

Run 2 total: $0.225.

**Projection per session.** The paper reports about 7 user messages per trial for GPT-5.5 agents, and 27 at most. A weaker DeepSeek solver may need more review passes.

- **Estimate:** 2 conversational replies at about $0.01, plus 5–12 review replies at about $0.05–0.20 (larger diffs and more tool rounds push towards the top).
  - **Total: about $0.5–1.5 per base session**, and about $3–5 for a 27-message outlier.
  - Worst case for one reply is 6 tool rounds near 60k prompt tokens each, roughly $1–2.
- **Forks:** the primary readout (stop at the first `ask_user`) costs no simulator calls. The secondary readout's live simulators cost about the same per reply in each arm.
- These are estimates from 4 replies on one task. The plan should log billed simulator tokens per round (the port's `rounds` already holds them).

**Fidelity choice for the plan:**

- **(a) "As designed":** tool rounds on, via OpenRouter → OpenAI. Matches `server.py`'s intent and the paper's description of inspection.
- **(b) "As released, on today's API":** fallback-only.

Same task state, different corrections: run 1's re-review asked for lifecycle events on `promote_consumer`, while run 2's asked for non-SAC `is_active` semantics. The replies are stochastic, so this is one sample, not evidence of a systematic difference. Recommend (a), logged as a deviation, with the 400 noted.

**Also noted:** on the final state (the full reference solution applied) both runs still raised a correction. So the simulator can ask for things the reference solution doesn't do.

s2-plan checked run 2's correction (non-SAC `is_active` true only for the top-priority tag) against the reference solution and the tests:
- The spec sentence it leans on ("for non-SAC, the highest-priority consumer is considered active") is about `get_active_consumer` (hidden tests, lines 474–484).
- The reference `consumer_is_active` returns True for every non-SAC consumer (`solution.patch:344–350`).
- The tests check `is_active` only with one consumer.

So the simulator over-read the spec and contradicted the reference. The plan now requires, for every candidate, that the reference solution applied to the boundary's base passes the frozen act's scorer.

## Files

- `src/compaction/s2/interact.py`: the simulator port (`UserSim`, `SimState`, `run_command`), boundary export and restore (`capture`, `Boundary`, `fingerprint`), and `InteractEnv` for `loop.Loop`.
- `tests/compaction/test_s2_interact.py`: allowlist and argv, auto-snapshot and tool rounds, export/restore and the ask-log rebuild, error fallback vs caps, parity with the released `server.py`, and the Docker fork test.
- `data/raw/sprint/swe_interact/`: `repo/` (SWE-Interact @ b32f98c3), `harbor/` (@ 6099b8cf), `derived/` (`task_table.{py,json}`, `fork_report.json`, `measure_sim.py`, `sim_measure_{openai,openrouter}.json` and spend logs).
