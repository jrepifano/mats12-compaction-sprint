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
- **The transport retry trusts stderr** (4f75bad2; MINOR from native-codex's astra review, 20261025-s2-codex-transport-retry-r1). `docker exec` forwards the command's own stderr. A command that has a side effect, then prints docker's exact `_ping` dial error with "exit status 255" and exits nonzero, would be replayed. The native harness execs are our own scripts, and the agent CLIs run detached, so the exposure is nil in practice. Proposed hardening, after the batches: only retry when the dial error is stderr's last line, and only for replay-safe operations.
- **Audit code can postdate a run's recorded code.** A run's `code` (result.json) is frozen at launch, but the audit code is loaded later:
  - **Older launchers** imported `audit` at start.
  - **Launchers at a30d2c9e and 8943ec69** import `audit` at their first audit.
  - **All launchers before this change** import `interact_reqs` inside their first requirements call.
  - **Launchers from this change on** import both modules at the start of their first `_audits` call.
  - **The lossaudit subprocess** loads its files fresh on every run.

  A commit landing between launch and audit can therefore reach a launched run's audits. d40d8953 (the per-reply split fallback) was such a commit; astra l3-r2 #4 had accepted this as a limitation.

  **From the commit after 8943ec69**, each audit component appends a row to `_audit/<rk>/audit_code.jsonl` (astra auditcode r1–r3). These are disk observations, not hashes of the executed bytes. The row holds:
  - git HEAD;
  - per module, `sha256_around_import`: the file hashed just before and after the process imported it, recorded only when the two agree (`preloaded`, with no hash, when something imported it earlier); plus its file now;
  - for lossaudit, its two files just before the subprocess started and after it exited, with `changed_during_run` True/False from those endpoints (None when a hash is unreadable).

  **Runs audited before that commit have no such record.** Which audit code they ran is uncertain: the HEAD at their audit time isn't enough, since a module stays cached from a process's first audit and a commit's time isn't when the checkout changed.

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

Filled in as runs finish (UTC, 24 Oct). (a) = requirement ledger, live summary (items / lost, new / inherited); (b) = lossaudit (items / lost). Windows: the first Claude Code runs at 163k, then 170k.

| Run | End | Boundaries: pre_tokens (summary chars) | (a) | (b) | Sim $ |
|---|---|---|---|---|---|
| CC helm a1 (163k) | transcript_error (old code, infra) | b1 142,838 (17,055) | 6 / 0 | 25 / 1 | 0.39 |
| CC kombu a1 (163k) | transcript_error (old code, infra) | b1 134,562 | 7 / 0 | 19 / 1 | 1.15 |
| CC fastapi a1 (163k) | run_cap (sim $4.86 + next bound $1.75 > $6.50) | b1 134,250 (16,064); b2 132,113 (23,493) | 30 / 0 | 66 / 5 (b1 4, b2 1) | 4.86 |
| CC vitest a1 (163k) | transcript_error (old code, infra) | b1 130,037 (15,229); b2 130,979 (22,360); b3 130,597 (23,925) | 116 / 14 (10 new, 4 inherited) | 92 / 6 | 4.42 |
| CC helm a2 (170k) | submitted | b1 138,211 (3,888: the summarizer partly refused) | 2 / 0 | 25 / 8 | 0.09 |
| agy testem a1 | infra_error (rig ssh outage at the b3 screen) | b1, b2 kept; b3 image orphaned, removed | | | 1.33 |
| CC boa a2 (170k) | submitted | b1 137,630 (16,512); b2 139,945 (21,510); b3 137,537 (19,363); b4 137,446 (23,517) | 64 / 1 (1 new) | 103 / 1 | 2.23 |
| agy vitest a1 | run_cap | b1 151,065 (8,487); b2 150,577 (9,228) | 85 / 28 (23 new, 5 inherited) | 55 / 8 | 5.03 |
| agy boa a1 | infra_error (ssh drop at the screen) | b1, b2 kept | | | 4.47 |
| agy helm a1 | infra_error (ssh drop at the screen) | none | | | 0.10 |
| CC kombu a2 (170k) | run_cap | b1 139,513 (15,130); b2 137,922 (19,344); b3 137,364 (25,871) | 92 / 0 | 68 / 1 | 4.86 |
| agy testem a2 | submitted | b1 151,095 (10,393); b2 150,597 (8,179) | 74 / 21 (18 new, 3 inherited) | 48 / 6 | 3.14 |
| agy boa a2 | submitted (released at the $170 cap: gate $146.76 ≤ $165) | b1 150,036 (9,898); b2 151,224 (7,520); b3 150,237 (12,142) | 13 / 1 | 53 / 4 | 0.37 |
| agy fastapi a1 | submitted | b1 150,033 (12,352); b2 150,301 (10,066); b3 150,343 (7,623); b4 150,000 (13,200) | 34 / 7 (4 new, 3 inherited) | 64 / 6 | 2.64 |
| agy helm a2 | boundary_error after 3 boundaries: our converter (`agy.normalize_call`) failed on an `ask_user` call whose `Arguments` was not a dict; b4 image orphaned | b1 151,517 (8,738); b2 150,125 (12,314); b3 150,391 (9,056) | 8 / 0 | 43 / 1 | 0.36 |
| agy kombu a1 | time_cap, 0 compactions: context peaked at 136,575, then agy idled on 1 background task for about 1 h 45 m; the background-task rule only acts once the trigger has fired | none | | | 0.07 |

Arm tables: `results/sprint/S2/native/claude_code/summary.md` (17 boundaries: (a) 15 / 395, (b) 28 / 468) and `results/sprint/S2/native/agy/summary.md` (18 boundaries: (a) 64 / 273, (b) 33 / 338), from `scripts/s2_native_summary.py` (astra summary r1, summary-agy r1).

Open after the batches:
- the transport-retry hardening ((a)+(b): last-line match and replay-safe operations only, with an astra round);
- `agy.normalize_call` should accept a string `Arguments`;
- the background-task rule should also act when agy idles below the trigger.

Verified by hand (no model calls; kept images read with `--network none`):
- **vitest a1's 14 ledger losses are summary-only.** At b2 and b3 the code already implements every one as specified (`resolveConfig.ts` validators and `??=` defaults; `durationHistory.ts:36–37` for the legacy-number migration). The summaries keep the fields generically ("the remaining sequence fields with specified types and defaults… done, committed") without each field's type and default. b3's "misstated" on b2-q7 looks like a grader error: it conflates the reply allowing seconds-based entries to expire (r15) with the bare-number migration. Inherited: b1-q6/q7/q8 were kept at b1, lost at b2 and still lost at b3; b2-q7 was lost at b2 and still lost at b3. Claude Code re-attaches files read before compaction (post[8]), but resolveConfig.ts and core.ts only as "too large to include" notes.
- **helm a2 b1's 3,888-character summary is genuine, not a capture issue.** Claude Code's own compaction call (Haiku) partly refused its prompt ("I'm not going to produce that analysis/summary block, but I can give you the state of the work directly"). The transcript row matches the capture, the compaction took 5.3 s, and `quiescent: false` is unrelated (background processes frozen). It is 1 of 15 Claude Code boundaries so far; the others use the full sectioned format (12.6k–23.9k chars). It lost the opening task's rules (i1–i5; the first prompt is not re-attached). The repo state at the boundary follows i3 and i4, i11 (append order: chart defaults, then the user's elements) is implemented (`merge_rules.go` `appendList(lower, higher)`), and the run ended submitted. i14 is misstated because the code itself exported `CoalesceChartTables` rather than adding an internal helper; the summary describes the code correctly.
- **agy vitest a1: 27 of its 28 ledger losses are summary-only; one cost a maintainer round.** agy's post-compaction context is the CHECKPOINT alone: it keeps no verbatim messages and re-attaches no files, and the summaries are 8.5–9.2k chars against Claude Code's 15–24k. At b1 and b2 the code already implements 27 of the 28 losses. The 5 inherited ones (b1-q6, q8, q10, q23, q24) were lost at b1 and stayed lost; 12 of b2's 17 new losses had been kept at b1. The exception is **b1-q32** ("record durations in the final cleanup path"). At b1 the code recorded them on the success path only, and the summary misstated where. The maintainer re-raised it in r13, the first reply after b1 ("recording is in the main success path right now… Move that call into the existing `finally`"), and by b2 it was fixed. So far it is the one natural loss of a not-yet-implemented requirement with a visible cost.
- **agy testem a2 (light check: the 3 inherited losses plus 5 new ones drawn with seed 20261025): 7 of 8 summary-only, 1 consequential.** The exception is **b1-q17**, inherited: "after a reset, output reflects only post-reset activity". At b2 the code only re-enabled forwarding, without clearing totals or sub-reporter state. The maintainer re-raised it in r21, after b2 ("resetBailState() is still too shallow…"), and shipped in r22. The final code isn't kept (the container is removed at teardown), so "final" is judged from the replies after b2. By boundary: b1-q27 (bail through abortRunners) was checked at b1, where it is implemented. The other 7 were checked at b2: b1-q17, b1-q24 and b1-q25 (inherited); b2-q13, b1-q8, b1-q22 and b1-q31 (new). 6 of them are implemented; b1-q17 is partly implemented.
- **fastapi b1, lossaudit pass 2: claim facts missing.** The claims call (312k input tokens) exceeds S1's frozen `MAX_OUT_2 = 32_000` deterministically: 3 attempts, about 128k output tokens and 490 s each, on Max. The main session accepted it as unresolvable and said not to retry. The same overflow appears in the cache for 4 other boundaries of other arms. The old launcher's shared lossaudit dir re-ran every staged run at each audit, so vitest a1's rc 3 was this same fastapi error (the per-run dirs of the current code avoid it).

Operations: the 90% audit hold, the Max ping and the launcher hand-over (`_block-*` slots; new launchers for the old ones' queues; old launchers drained) are from commit a30d2c9e (astra audithold r1–r3). Tonight's rig ssh outages were OpenSSH's PerSourcePenalties dropping the laptop's connections (main session); calls back off.
