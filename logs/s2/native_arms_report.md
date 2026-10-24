# Native-harness arms: Claude Code and Antigravity (build report)

Brief: `logs/s2/briefs/native-arms.md`. Built 24 Oct 2026 by sub-agent native-arms for the accumulated-loss plan (`docs/sprint/plans/accumulated-loss.md`). Code: `src/compaction/s2/native/` (`common.py` shared with the Codex arm; `claude_code.py`, `agy.py`, `audit.py`, `egress_proxy.py`), `scripts/s2_native.py`, tests `tests/compaction/test_s2_native.py`, `tests/compaction/test_s2_native_agy.py`. Run under `uv run --with fastmcp==4.1.3` (the version the released simulator server resolves to; not a project dependency).

## Verdicts

| Arm | Q1 (native base runs) | Q2 (native forks) | Reviews |
|---|---|---|---|
| **Claude Code** 2.1.310, Haiku 5.5 on Max | **Works.** Smoke 2 (kombu, window 80k): auto-compaction at 67,224 tokens captured live; boundary loads in `lossaudit`; both Q1 audits run on it. Batch on the 6 tasks running. | **Works.** A fork from the kept image with an edited summary resumed and quoted the edit (`--resume <new id> --fork-session`). Refuses boundaries that aren't quiescent or have entrypoint services. | astra r1–r3 (cap); every BLOCKING fixed; r3's fixes tested, not re-reviewed |
| **Antigravity** `agy` 1.4.7, Gemini 3.8 Flash on Jacob's free quota | **Works with a forced compaction.** The auto threshold isn't settable (`AGY_CLI_AUTO_COMPACT_OVERRIDE` is no token threshold), so a PostInvocation hook ends the turn at 150k and the runner runs `/compact`, captures, and resumes with "Continue.". Smoke 2 (kombu, heavy): forced compaction at 150,107 after 240 model calls; b1 loads in `lossaudit`; sim $0.048. A hung background test had to be killed (by hand; now the coded background-task rule). | **Not attempted.** The conversation DB only takes same-length edits (feasibility). | astra agy r1, delta r1–r3, launcher-3 r1–r2; every BLOCKING fixed (the last r2 fixes not re-reviewed) |

## Topology and safety (both arms)

- Task container on a per-session `docker network create --internal` network on the desktop (rig); an egress sidecar (`egress_proxy.py`, alpine + python3) on that network and the bridge: a CONNECT proxy that tunnels only to the arm's allowlist, and a TCP forward `user-server:8000` → the simulator on the laptop's Tailscale IP. The step instructions' `http://user-server:8000/mcp` is literally right.
- Egress proved per run (`egress_check.json`): github.com, pypi.org and 1.1.1.1 fail directly (no DNS, no route) and through the proxy (403); the vendor API answers through the proxy. Claude Code allowlist: api.anthropic.com. agy: daily-cloudcode-pa, cloudcode-pa, aicode, www.googleapis.com, oauth2.googleapis.com, antigravity-unleash.goog (discovery run; lh3 and play.googleapis.com not needed). Server-side WebSearch/WebFetch disabled in Claude Code.
- Simulator: our `interact.UserSim` behind FastMCP "simulated-user" with the released `ask_user` tool (same name, docstring, signature, structured result). Every request carries a per-session token (header; agy: URL query, its MCP config takes no headers). Paid calls through `PilotSpend` (SPRINT-S3-Q2 / s3q2-interact-pilot).
- Secrets: Claude's OAuth token only as `docker exec -e` (value in the client env), never in an argv, file, log, image or record; `apiKeySource` must be exactly "none". agy's token store lives only in the Docker volume `emx-native-agy-auth` (Jacob's one-time container login), mounted at /run/emx-agy and symlinked; tmpfs for the MCP config, hook header and markers. Before every kept commit a fail-closed scan of every file changed since the session started (secrets from env or the mounted store into a /dev/shm pattern file; `grep -f`); agy turn outputs, transcripts and DB copies are screened in the container before they leave it.
- Containers: `--rm`, a name, `emx.sandbox=native-<arm>`, `emx.run`, `timeout` main process; memory-gated launches (agent 3 GB / 2 CPUs at ≥ 6 GB free; heavy 8 GB / 4 CPUs at ≥ 10 GB free); a docker client timeout never removes a container (`tolerant_exec`).

## Boundaries (the pi layout, unchanged consumers)

`b<k>/boundary/{pre_messages, compaction, post_messages, record, sim_state, sim_replies, boundary}.json`, `step_instruction.txt`, `b<k>/run.json`, `b<k>/native/*` (the native transcript, hook payload; agy: the conversation DB). `lossaudit.load`, `interact_reqs` and the fork trigger read them unchanged. Boundaries of 01_plan go to `plan_boundaries/` (own chain, not graded).

- **Claude Code:** the PostCompact hook blocks the CLI while phase 1 runs (verified freeze of background processes, secret scan, fingerprint v3, kept `docker commit --pause`, simulator state); Claude Code writes the boundary rows only after the hook returns, so phase 2 waits for them. pre = the chain context incl. rendered attachments (system reminders); post = the exact first API request after the summary, rebuilt from the transcript's own request records (`post_source: request`), else the summary, the preserved segment and attachments. The verbatim tail is in post_messages, so `lossaudit.post_context` marks items in it still_present (`kept_indices`, `tail_start_index`).
- **Antigravity:** captured after `/compact` with no agy process alive. pre = the steps since the previous CHECKPOINT up to our /compact input; post = the CHECKPOINT (+ system steps after it). MCP calls (`call_mcp_tool`) become `ask_user({question})`; `run_command` gains `command` for `command_of`.

## Thresholds (Jacob, 24 Oct: a common 150k trigger)

- Claude Code's native default for Haiku 5.5 is its 1M context: it would never compact on DeepSWE. The trigger sits below `CLAUDE_CODE_AUTO_COMPACT_WINDOW` by a buffer that isn't constant: 12.8k at an 80k window (68,082; 67,224), 20–33k at 163k (130,037; 130,979; 134,562; 134,250; 142,838). The first batch ran at 163,000; remaining runs and reruns use 170,000 (main session, calibrating to Jacob's 150k). Every boundary's actual `pre_tokens` is recorded (result.json `compactions`, status lines).
- Antigravity: native threshold not settable; forced at the first turn boundary where the last call's input + cache-read tokens ≥ 150,000 (`pre_tokens_kind: trigger_request_context`: the request of the call that crossed it, not its output or tool results).

## Design decisions (logged)

- Fork and post-compaction resume prompt "Continue.", identical in every arm (Claude Code needs a user prompt to resume headless; agy needs a new turn after `/compact`). Agreed by the main session.
- Repairs go inside Claude Code's summary body, before its closing paragraphs (`insert_in_summary`): a line appended after them read as outside the summary in smoke 2's fork. Agreed by the main session.
- Background processes at a boundary are frozen (SIGSTOP/CONT) and listed, not killed: the base run keeps them; such boundaries are kept for Q1 but never fork sources. Boundaries with image-entrypoint services likewise (`ineligible_reason`).
- No fork trigger in the Claude Code batch (runs to completion); both Q1 audits after each run: (a) interact_reqs live-only ledger, (b) s2_lossaudit on Max over a symlink staging dir (main session).

## Known limitations

- Claude Code's own system prompt is in pre/post from the transcript's request records; agy's is not recorded (placeholder).
- agy's `pre_tokens` is the triggering call's request size; agy's post-compaction "Continue." turn adds notices not in post_messages.
- Snapshots don't carry processes (as the pi arm); runtime state of services is not validated for forks.
- Codex arm: built by native-codex on the same `common.py`.

## Commands

```bash
uv run --with fastmcp==4.1.3 python scripts/s2_native.py egress-check --arm claude_code|agy
uv run --with fastmcp==4.1.3 python scripts/s2_native.py cc-batch --go-from-jacob [--tasks ...] \
    --window 163000 --sim-cap 6.5 --projection-usd 40 --implement-timeout 7200 --concurrency 3
uv run --with fastmcp==4.1.3 python scripts/s2_native.py cc-base --task T --window 163000 --go-from-jacob
uv run --with fastmcp==4.1.3 python scripts/s2_native.py cc-fork <run>/b<k> --summary-file F --go-from-jacob
uv run --with fastmcp==4.1.3 python scripts/s2_native.py agy-base --task T --compact-at 150000 [--heavy] --go-from-jacob
```

## Spend

Simulator only (agents on Max / the free quota), from the per-run spend files (`_spend/*.jsonl`, settled): Claude Code smoke 1 $0.065 (capture failed: summary-row lookup), smoke 2 $0.063; agy smoke 1 $0.153 (killed at 02_implement turn 1: a docker client timeout removed the container), smoke 2 $0.048. Batch: see below.

## Results

(filled in as runs finish)
