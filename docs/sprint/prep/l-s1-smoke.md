# Sprint prep (l): Experiment 1 Phase 0 smoke checks on the Max plan

2026-10-20, 21:47–21:55 UTC (5:47–5:55 PM ET). Claude Code 2.1.305, agy 1.4.3. All calls ran on
Jacob's Max plan with `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` unset: $0, and no ledger rows
(these are infrastructure checks, not S1 data). Raw stream-json outputs and the two native session
files are in `data/raw/sprint/s1/smoke/` (gitignored: the long call holds a private session). They
shaped the frozen interfaces in `docs/sprint/specs/S1.md`.

## Result

- **Auth is the subscription.** `claude auth status`: `authMethod: claude.ai`,
  `subscriptionType: max`. Every call's `init` event reported `apiKeySource: "none"`.
- **Structured output works headless.** `--json-schema` with `--safe-mode --tools ""` returns the
  answer in the result's `structured_output` (the model calls a built-in `StructuredOutput` tool;
  `num_turns: 2`). `--system-prompt` replaces Claude Code's default system prompt under
  `--safe-mode` (cache write fell from 3,453 to 1,117 tokens, the remainder being the tool
  definition).
- **The longest history fits in one Haiku 5.5 call.** Jacob's longest replaced history
  (`jacob:64567087-…:3`, 1,155 messages) rendered to 1,988,430 bytes; Haiku 5.5 took it as
  **808,392 input tokens** and answered correctly (message count 1,155, first words quoted) in
  23 s. That is 2.46 bytes per token, so the plan's chars/3 estimate under-counts (it gives 650k).
  The spec uses bytes/2.3 for Haiku. `modelUsage` reports `contextWindow: 1000000` and
  `maxOutputTokens: 128000`.
- **Haiku 5.5 thinks by default** (147–358 thinking tokens on these calls). `--effort` is accepted.
- **Usage limits are visible before they bite.** stream-json emits a `rate_limit_event` per call:
  `status: "allowed"`, `overageStatus: "rejected"` (no extra-usage billing is possible on this
  org), and `unifiedWindows` with utilization and reset times. At 21:55 UTC: five-hour window 0.08
  (resets 23:10 UTC), **seven-day window 0.13, resetting Sun 25 Oct 8:00 PM ET**. The 808k-token
  call didn't move the displayed five-hour utilization. Note: the plan assumed a 22 Oct reset; the
  seven-day window reported here resets on 25 Oct.
- **Native `/compact` works on a hand-built transcript.** A transcript written by hand into
  `~/.claude/projects/<cwd slug>/<uuid>.jsonl` (user and assistant entries with `uuid` and
  `parentUuid` chains, then a second one with a `tool_use`/`tool_result` pair) was compacted by
  `claude -p "/compact" --resume <uuid> --model claude-haiku-5-5 --safe-mode --tools ""
  --strict-mcp-config --output-format stream-json --verbose`. Both runs ended `compact_result:
  "success"` and emitted a `compact_boundary` event (trigger `manual`, `pre_tokens`,
  `post_tokens`, and `preserved_messages`: the last message was kept verbatim), then the summary
  as a user message ("This session is being continued from a previous conversation …"). The
  session's `cost-state` row lists only `claude-haiku-5-5` in `modelUsage`, so the summary was
  written by Haiku. No `api-request` row is written in this mode, so the request itself is
  recorded only through the captured Claude Code compact prompt and the session file before and
  after. **The native arm stays in Part B.** Without `< /dev/null`, `claude -p` waits 3 s for
  stdin and warns.
- **Antigravity's summarizer prompt is in the agy binary.** The text from "You have been working on
  the task described above but have not yet completed it" to "Wrap your response in
  <summary></summary> tags." (2,288 bytes, sha256 `b40fec18…ae40426`, agy 1.4.3) has the six
  sections the spike saw in real checkpoints, ending with "6. **Commitments & Constraints**". The
  replay loads it from the binary at runtime. **Part B keeps all 6 arms.**

## Max throughput benchmark (Phase 3, step 2)

2026-10-20, 22:08–22:15 UTC. `MaxBackend` (U2a) with Haiku 5.5, a pass-1a-like prompt (list up to
40 actionable items with quotes, structured output, 16k output cap) on 10 Part A histories at size
deciles from 204 KB to 2.13 MB rendered, run at concurrency 2, then 4, then 8. A nonce in the
system prompt kept the levels from sharing cache. 30 calls, $0, one $0 ledger row per level. Raw
rows: `data/raw/sprint/s1/maxbench/` (gitignored).

| Concurrency | Wall | Calls/h | Input tokens | Output tokens | Slowest call | Errors |
|---|---|---|---|---|---|---|
| 2 | 197 s | 183 | 2.44M | 72k | 59 s | 0 |
| 4 | 117 s | 306 | 2.30M | 68k | 67 s | 0 |
| 8 | 69 s | 524 | 3.24M (one retry) | 67k | 49 s | 1 (schema) |

- **The allowance binds, not concurrency.** Over the 30 calls (~8M input tokens) the five-hour
  window rose from 0.11 to 0.16 and the seven-day window from 0.14 to 0.15. Read coarsely, the
  five-hour window holds on the order of 150M input tokens, and the seven-day window on the order
  of 800M. The displayed utilization has two decimals, so these are rough; Jacob's own Claude Code
  use counts against the same windows. The bulk run uses concurrency 8 and paces on the windows.
- **Token ratio:** the largest history (2,129,709 bytes, a SWE-chat OpenCode record) was 939,884
  input tokens: 2.27 bytes per token, below the 2.3 the spec assumed. The spec moves to 2.2 for
  Haiku, which sends that record to two chunks.
- **Tool compliance fails near 1M tokens.** At concurrency 8 that same record came back without
  structured output: Haiku answered in plain text, and when the CLI asked it to call the tool it
  replied "I'm not going to call the StructuredOutput tool again, since my last reply already gave
  the list". At concurrency 2 and 4 the same request succeeded. The wrapper now retries a missing
  structured output once and salvages a JSON object from the text.
- Haiku 5.5 thought for up to about 10k tokens per call at the default effort.

## End-to-end check on 3 records (plan "Verification")

2026-10-20, 22:33–23:00 UTC. `s1_judge.judge_records` through `s1_data` and `MaxBackend` (Haiku
5.5, $0) on three Part A records outside the label set, one each from Jacob's sessions, SWE-chat and
FrontierML (seeded pick). Run three times, changing one thing each time:

| Run | Effort | Pass-2 prompt | Wall | Items (valid end to end) | Claims (valid) | Failed requests |
|---|---|---|---|---|---|---|
| 1 | default | verbatim `claim` | 781 s | 111 (97.3%) | 33 (64%) | 2 of 3 pass-2 calls |
| 2 | low | verbatim `claim` | 316 s | 67 (98.5%) | 119 (67%) | 0 |
| 3 | medium | `claim` + `summary_quote` | 388 s | 98 (99.0%) | 178 (83%) | 0 |

- **Default effort over-thinks.** Pass 2 spent 63,646 tokens thinking and failed with "Claude's
  response exceeded the 16000 output token maximum". Pass 2's output cap is now 32k.
- **Low effort under-lists.** It found 40% fewer items than the default, missing real user rules
  (e.g. "The LLM input must include all the data shown in the ChartDataView UI"), with zero thinking
  on two of the three histories. **The judge runs at medium effort**: 12% fewer items than the default
  at half the wall time, with no failures. The gold windows measure what it still misses.
- **Claims are atomic, not verbatim.** Haiku rewrites each claim into a self-contained statement
  ("All 46 accidental dignities tests pass" from "**Accidental Dignities**: 46 tests passing"), so
  the schema now carries the reworded `claim` and a separate `summary_quote` anchor. The anchor is
  still often near-verbatim (case, a dropped word, stripped backticks), so it is located with an
  exact → normalised → fuzzy (ratio ≥ 0.85) match and the tier recorded. History quotes stay exact.
- **Validity after the tolerant anchor** (run 3's cached answers rescored, no new calls): item
  quotes into the history 99.0% valid end to end (97 of 98); claims 92.1% (164 of 178). Of the
  claims' summary anchors, 147 matched exactly, 3 after normalising and 14 fuzzily; 11 were not
  found and stay invalid. History evidence quotes failed on 4 of 178 (97.8% valid). All rows
  together: 94.6%. The plan's 95% bar is on quotes into the history, which pass.
- The three records are Part A records, so their verdicts are not reported here: S1 outcomes stay
  sealed until Jacob's audit. Their outputs are in the gitignored scratchpad only.

## Not checked here

- What a usage-limit rejection looks like in stream-json (`status` other than `allowed`): the
  wrapper treats any such status, or a result error naming a limit, as a pause until `resetsAt`.
- Native `/compact` on a history near 1M tokens.
