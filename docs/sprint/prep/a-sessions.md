# Sprint prep (a): audit set from Jacob's Claude Code compactions

2026-10-20 (UTC); revised 08:00Z after the round-1 code review, 08:20Z after round 2, and 08:35Z after round 3 (the final round; the round-3 fixes are not re-reviewed) (see "Review r1 response" at the end). Unit (a) of the compaction-sprint prep ([proposal](../compaction-proposal.md), "Offline audit" and "Privacy of my own sessions"). Nothing was sent to any API, and nothing was spent. This report gives structure and counts only, no session content.

## Result

- **50 compactions in 31 sessions** of this repo (the main checkout plus worktrees), 2026-09-30 06:17 to 2026-10-20 03:59 UTC. The 18 Oct scan found 48 in 30 sessions; two are new since then (19 Oct 17:15, 230k tokens; 20 Oct 03:59, 969k tokens). Three fall on or after 16 Oct.
  - 49 auto, 1 manual (964k tokens).
  - Compactions per session: 1 (19 sessions), 2 (8), 3 (3), 6 (1).
  - Context just before compaction (`preTokens`): min 205,152, quartiles 344,531 / 466,578 / 967,252, max 972,033. Bins: 200–300k 8, 300–400k 11, 400–500k 8, 500–600k 9, 900k–1M 14 (none between 600k and 900k).
  - Model of the last assistant turn before compaction: claude-opus-5-5 43, claude-opus-5 6, claude-opus-4-8 1.
  - 34 records come from main-checkout sessions and 16 from probe worktrees. In the worktree sessions, the "human" turns are mostly briefs typed in by the launching agent through herdr. The transcript doesn't tell them apart from Jacob's own turns.
- **Jacob's other projects: 0 compactions** (counted, not extracted). The scan covered 31 top-level and 46 nested session files in 10 other project directories, plus this repo's 656 nested (subagent) transcripts. All 50 compactions are in this repo's top-level session files.
- **All 50 records have a non-empty source and summary.** Every record's chain walk succeeded, so there was no fall-back to file order.
  - Source messages per record: median 481 (127–1,398). Total 28,949: assistant 13,181, tool 12,316, user 2,173, system 1,279. The rise from 28,039 in the first build is the parallel tool results the first build dropped (see "Branches").
  - Source message text per record (the `text` fields, tool-call args not counted): median 0.58M characters (0.21M–1.48M). This is below `preTokens`, because thinking blocks, the system prompt, tool definitions and bookkeeping attachments are left out (see Method).
  - Summary: median 16.1k characters (8.3k–28.9k).
  - Post messages: 42 records have the full 50; the shortest has 1, because the session ended. Total 2,198.
  - User turns: 713 in all. 46 of 50 records have at least one (median 8, max 89).
  - Rules: 613 heuristic matches, 434 by keyword and 179 by an imperative opening. Median 11 per record. Rules are kept whole (12 are longer than 500 characters, the longest 1,126), and each has its source offsets in `meta.rule_matches`.
- **Tool calls pair with their results by id:** 13,192 calls in source, preserved and post messages; 13,173 pair with a result in the same list. The other 19 are calls in the last three post messages whose results come just after the 50-message cap (all 19 results exist in the raw files). There are no results without a call.
- **Instruction files after the compaction:** 46 of 50 records show an `instructions` attachment right after the boundary: AGENTS.md + MEMORY.md in 33, CLAUDE.md + MEMORY.md in 13 (sessions from before the 11 Oct rename). The other 4 show no instruction attachment anywhere after the boundary in the transcript (sessions 51392f5b #1, 5cb46a48 #0, 77b35d9f #0 and 8d20594b #1). Each has a short tail of 15–26 records before the session ends. The transcript can't show whether those four had AGENTS.md in context some other way. Treat them as "not observed", not "dropped".
- **Verbatim preserved segment:** all 50 compactions keep 1–6 recent records verbatim (110 in all), recorded in `compactMetadata.preservedMessages`. A rule in those records survives without going through the summary. They are left out of `source_messages` and stored in `meta.preserved_messages`.
- **Scrubbing: 12 records are export-eligible (`scrubbed=true`); 38 are quarantined as Anthropic-only (`scrubbed=false`).** Only the 12 may go to non-Anthropic models.
  - Hard gate (per record and its meta line): nothing that a second scrub would redact, in any view up to three layers of escapes, joined literals and base64, and no `.env` value or 12-character window of a secret-named value in any string: 0 failures.
  - Fail-closed quarantine (ids in `manifest.quarantined_ids`, reasons per record in `meta.quarantine`): 198 decoded tokens with a credential-like shape that appear only after a rewrite and are not hex digests or UUIDs. 159 come from joining adjacent quoted literals (`decoded-candidate/split`), 30 from decoded escapes, 8 from base64, and 1 from escapes then joining. No record reached the normalization bound. These tokens are probably mostly benign (r2 shape checks showed paths split across literals), but under the round-3 rule token shape never exempts them. A test of one evidence-based exemption (the token also appears unencoded in the same record) would have released only 3 of the 38 records, so it was not adopted. The 38 can be released only by a local review, and nothing in this unit does that.
  - Scan of the written files: 0 .env values, whole or as windows, in the raw files or in any decoded string. 0 named token patterns remain.
  - Redactions: env-value 1,138, secret-assignment 513, high-entropy-assignment 219, env-secret-partial 74, sk-key 8, jwt 1, env-value-base64 1, secret-assignment-split 1. The round-2 review found one JWT candidate surviving in the r1 output; this build redacts 1 and leaves 0. The base64 hit is a non-secret `.env` value (a setting), base64-encoded. Secret-assignment rose from 277 (r1) to 437 (r2) and 513 (r3), as the placeholder exemptions narrowed to validated environment references only.
  - Values scrubbed: 14 of the 22 `.env` values (every value of 8 or more characters). The other 8 are empty or are short settings such as "1", "offline" and "false", which can't be matched safely. A short secret is still redacted wherever it appears under a secret-named key.

## Outputs (gitignored, mode 600)

| File | Bytes | sha256 |
|---|---|---|
| `data/raw/sprint/sessions/compactions.jsonl` (50 CompactionRecords) | 61,768,125 | `2a6d4c317a935641d23a87b9538c813aea7036ff991cc31005a5bed1afd9219e` |
| `data/raw/sprint/sessions/meta.jsonl` (sidecar, one line per record) | 243,808 | `970ffd167c00ae634183e6447eebf2bba11d267515539117ed9b9e6d49df50a7` |
| `data/raw/sprint/sessions/manifest.json` (counts, distribution, hashes) | 5,559 | `d223a7a2a3f6e6e3aa6edff8016b6d04541335cb36f646e64529b7981778ee92` |

Three r3 builds (a scratch build at 08:28Z, then 08:31 and 08:33Z) gave a byte-identical `compactions.jsonl`; the last two gave identical `meta.jsonl` and manifest too. `meta.jsonl` can differ between builds in one field of one row: `post_messages_total` of the last compaction in session 18f6ced4, the live coordinating session, whose post window grows as the session goes on. Code: `src/compaction/sessions.py` (sha256 `82f0c329…`; tests in `tests/compaction/test_sessions.py`, sha256 `42e37f26…`, 93 passing). Earlier builds' files (r0 `89add39b…`; r1 `5740928e…`; r2 `0f8d3eb8…`) are superseded.

Compactions per session (`session_id`: n): 64567087: 6; 109afb88, 4e13f60e, c6c82233: 3; 18f6ced4, 4e6ded17, 51392f5b, 653c82ea, 685bb71f, 8d20594b, abf9febb, b6960766: 2; the other 19 sessions: 1. The full list is in `manifest.json` under `per_session`.

### Two structural examples (keys and lengths only)

- `jacob:<session>:<n>` from a session started 19 Oct (r0 figures): `trigger` auto, `pre_tokens` 230,257, `source_messages` 125, `summary` 11,651 chars, `post_messages` 50 (the first five are 3 system, 1 user and 1 system), `user_turns` 3, `rules` 5, `license` "private", `revision` null, `scrubbed` true. Message keys: `role`, `text`, `tool_calls`, `tool_use_id`, `ts`.
- The median-context record: `trigger` auto, `pre_tokens` 472,987, `source_messages` 488, `summary` 16,216 chars, `post_messages` 50 (the first five are all system: re-attached instructions, re-read files and plan references), `user_turns` 11, `rules` 11.

The `meta.jsonl` keys are: `id`, `project_dir`, `claude_code_version`, `git_branch`, `post_tokens`, `duration_ms`, `chain_walk_ok`, `chain_start`, `source_abandoned_records`, `post_abandoned_records`, `carried_preserved_from_previous`, `previous_summary_in_source`, `n_source_records`, `n_thinking_blocks_dropped`, `preserved_verbatim`, `preserved_messages`, `instructions_in_source`, `reattached_in_post`, `agents_md_reattached`, `claude_md_reattached`, `memory_md_reattached`, `post_messages_total`, `rules_are_heuristic`, `rule_method`, `rule_matches` (one entry per rule: user-turn index, `start`/`end` offsets into that published user turn, keyword or imperative, the keyword), `checkable_assessed`, and `quarantine` (reason counts; empty unless the record is quarantined). Every meta field is scrubbed and gated like the record.

Messages also carry ids for pairing: each `tool_calls` entry has `id` (the `tool_use` id), and each `tool` message has `tool_use_id` (a str, declared in `src/compaction/schema.py`). A transcript record with several tool results gives one tool message per result.

## Method

The code reads Claude Code's own session files (`~/.claude/projects/-Users-jacob-Documents-GitHub-mats12-neel-exploratory*/<session>.jsonl`; versions 2.1.283 to 2.1.303). It relies on the following transcript structure, checked on these files:

- **Boundaries.** A `system`/`compact_boundary` record has `compactMetadata` (`trigger`, `preTokens`, `postTokens`, `preservedMessages`). The summary is the next `user` record with `isCompactSummary: true`, which gives `summary`. `timestamp` is the boundary's.
- **`source_messages`.** The source is the `parentUuid` chain walked back from the last record before the boundary, which drops abandoned branches from rewinds. The walk stops at the previous boundary or at the start of the session.
  - In 11 newer-format boundaries, `logicalParentUuid` points at a record written after the summary, and the walk starts from the last record before the boundary instead (`meta.chain_start` = `last-record`).
  - Records kept verbatim are excluded from the source. They count as part of the next compaction's source, inserted after the previous summary (`carried_preserved_from_previous`: 42 records into 19 compactions).
  - For the second and later compactions in a session, the source begins with the previous summary (19 records).
- **Message conversion.**
  - Assistant records that belong to one API message are merged. `tool_use` blocks become `tool_calls` (name and args).
  - Thinking blocks are counted (`n_thinking_blocks_dropped`) but not kept.
  - Tool results become role `tool`, one message per result, with its `tool_use_id`.
  - Attachments that add context (`instructions`, `nested_memory`, `file`, `compact_file_reference`, `plan_file_reference`, `edited_text_file`, `invoked_skills`, `session_context`, `task_status`, `queued_command`, `hook_additional_context`, plan-mode records) become role `system`, with the text Claude Code rendered for them. Bookkeeping attachments (token counters, tool and skill listings, the environment and system-prompt snapshots) are dropped.
- **Branches (the continuation path).** A transcript is a tree. Both the source and the post messages follow one path, the branch that was continued last, plus the side records that hang off it:
  - Source: the chain walked back from the last record before the boundary (as above). Post: every record reachable from the boundary (or from a preserved record) through `parentUuid` or `logicalParentUuid`, found by a graph walk, since a record can name a parent written after it (seen with scheduled-task records); the path is walked back from the last reachable record, in file order, that is not a side record.
  - Side records are kept: tool results, attachments, system and bookkeeping records, and assistant records of an API message already on the path. Claude Code writes the blocks of one API message (e.g. parallel tool calls) as a chain of records, each result a child of the record that made its call, and continues from one of the results, so the other calls and results sit off the path. Each side record is placed before the first later path record. The first build walked only the source chain and so dropped them: this build's source has 909 more tool messages and 1 more assistant message.
  - A subtree off the path that holds a user-text record or a new assistant message is an abandoned branch: a rewind or edited prompt, or a regenerated response (in the post windows, 10 and 4 of them). It is dropped and counted: `source_abandoned_records` 19 in 6 records, `post_abandoned_records` 44 in 7 records. The first build's post walk took every descendant of the boundary, so it mixed such branches into `post_messages`.
- **`post_messages`.** The path records after the boundary, excluding the summary and preserved records, converted the same way and capped at 50. Re-attached files and instructions count toward the 50, so the conversation itself is often shorter than 50 turns.
- **`user_turns`.** These are the source's user messages with `turnOrigin` (or `origin.kind`) set to `human`, plus human prompts queued mid-turn (`queued_command`). System-reminder blocks are stripped from them. Task notifications, messages from other sessions ("peer") and interruption markers are left out. Older records that lack an origin fall back to "not meta, and not a task-notification or command tag".
- **Rules: heuristic, and marked as such.** Each sentence of a user turn, outside code fences, counts as a rule if it contains a rule keyword (never, always, don't or do not, only, ask before or first, before you or any, go ahead, must or must not, should not, no need to, without asking, not allowed, make sure, "you may/can + proceed/push/merge/…", approve or approved, permission, stop) or if it starts with one of 50 imperative verbs.
  - Each match becomes `Rule(origin="user", checkable=False)`.
  - The meta file records `rules_are_heuristic: true`, `rule_method: "keyword-or-imperative-v1"` and, per rule, the kind of match. `checkable` was not assessed (`checkable_assessed: false`).
  - Expect false positives, especially among imperative matches, which are often task instructions rather than standing rules. In worktree sessions, the "user" is usually the launching agent's brief.
- **`model`** is the last non-synthetic assistant model before the boundary. `harness` is "claude-code", `source` "jacob", `license` "private" and `revision` null.

### Scrubbing (`Scrubber`, `scrub()`)

The scrubber runs on every string of each record, including tool-call args and dict keys, and on every field of the meta line. Steps, in order:

1. Every `.env` value of 8 or more characters, matched exactly, longest first. A value whose key looks like a secret (KEY, TOKEN, SECRET, PASSWORD and similar) becomes `[REDACTED:env-secret]`; any other value becomes `[REDACTED:env-value]`. The non-secret values (the HF username, GPU type names, local paths, a W&B project name, an upstream commit) are removed too, as the brief asks.
2. Encoded and split forms (bounded recursive normalization, `_views`). Three rewrites are applied in every order, up to three layers deep (e.g. base64 of `\u` escapes, base64 split across literals, `%25XX` double percent-encoding), each distinct result kept as a view that maps any span back to the original:
   - decoded escapes: `\uXXXX` (with any number of backslashes), `\xXX`, `\/`, `%XX`, `&#NN;` and `&#xNN;`;
   - re-joined string literals: `"abc" "def"`, `'abc' + 'def'`, `"abc" . "def"`, also across lines;
   - base64: each run of 16 or more base64 or URL-safe characters that decodes, at any of the four alignments, to text (90% printable or more) is replaced by that text.

   Every detector (`.env` values, their windows, and the token patterns below except the generic high-entropy rule) runs on every view. A hit whose original span was rewritten is redacted in the original (a base64 hit takes the whole run), kind suffixed with the view name, e.g. `-base64+escaped`.
3. Token patterns:
   - PEM private keys;
   - `sk-ant-…` and `sk-…`;
   - `hf_…`, `github_pat_…`, `gh[pousr]_…`, `rpa_…`;
   - `AKIA…`/`ASIA…`;
   - Google `AIza…`, including partial keys of 8 or more characters;
   - Slack `xox?-…`;
   - `Bearer <token>`;
   - JWTs: three dot-separated base64url parts (the third may be empty) whose first two decode to JSON objects;
   - `key=value`, `key: value` or `"key": "value"` with a secret-named key, whatever the value's length or punctuation. A quoted value is taken whole up to its closing quote; a bare value runs to whitespace or a delimiter. The key is secret-named if one of its words (split on separators and camelCase) is password, passwd, pwd, passphrase, secret, token, apikey, credential(s), auth or authorization, or it has api+key, private+key, access+key or signing+key, and it has no setting word (count, limit, max, type, url, file, path, name, id, size, mode, ...): so `max_tokens`, `tokenizer`, `auth_type` and `token_file` are settings. Only an environment reference with no literal default is kept: `$X`, `${X}`, `os.environ["X"]`, `environ.get("X")` and `getenv("X")`. Everything else is redacted, quoted or bare: dotted names (`PASSWORD=correct.horse`), variable names, `<...>`, `***`, and any reference with a fallback literal (`getenv("X", "dflt")`, `${X:-dflt}`). A bare value that is a call or index expression is redacted to the end of its line, so a literal default inside it can't survive. Escaped quotes inside a quoted value (`\"`) don't end it. After a kept match, the search resumes right after its key, so an assignment nested in another's value (`client = OpenAI(api_key=...)`) is still examined;
   - a value of 32 or more characters under any name that mixes upper case, lower case and digits. Pure hex is kept, since it is commit SHAs and sha256 digests. Paths and hyphenated identifiers are kept too; simulation shows that this misses 1.5–5% of random base64 and URL-safe strings, which is a known gap in this fallback rule.

   A string or number under a secret-named dict key is redacted whole, whatever its length, unless it is one of the environment-reference forms above (booleans and null are kept).
4. Any 12-character window of a secret-named `.env` value, widened to the whole surrounding token, becomes `[REDACTED:env-secret-partial]`. This catches truncated or partial prints.

**Verification, fail-closed** (`build()`):

- `build()` refuses to run without the `.env` inventory (missing file: `FileNotFoundError`; no secret-named value: `ValueError`).
- **Hard gate per record** (`Scrubber.audit`, on the record and its meta line): a second scrub must change nothing, and no `.env` value or window may remain in any string. A failure means a detectable secret survived (a scrubber bug), so `build()` raises `VerificationError` (counts and ids only) and publishes nothing. Result: 0 failures.
- **Fail-closed quarantine** (`Scrubber.quarantine`): a second, independent check, which doesn't rely on the detectors knowing the secret. It looks at every view of every published string (as in scrub step 2). A record is quarantined if (a) normalization reached its 3-layer bound with a layer still undecoded, or (b) a token appears only after a rewrite, is not redacted, and has a credential-like shape: 16+ characters of a base64/URL-safe alphabet with at least two of upper case, lower case and digits. The only exemption is an explicit allowlist of hex strings (7–64 characters: SHAs and digests) and canonical UUIDs; token shape alone never exempts. A quarantined record keeps `scrubbed=false`: it is **Anthropic-only** and must never be exported to another provider. Its reasons are in `meta.quarantine`, and the ids and counts are in the manifest (`scrub_gate`, `quarantined_ids`, `quarantine_reasons`). Only records that pass both gates get `scrubbed=true`. Result: 12 passed, 38 quarantined.
- Outputs are written to owner-only temporaries (created with mode 0600, whatever the umask), scanned again as files (below), and only then renamed into place. Any failure removes the temporaries and leaves the previous outputs untouched.
- `.env` values and their windows, checked in the raw files and in every decoded string: 0.
- Named token patterns in the raw files: 0.
- Key=value rules, checked on each decoded string: 0.
- On the raw JSON, the generic high-entropy rule also matches 34 `"documentId"`/`"fileId"`/`"requiredRevisionId"`/path pairs inside tool-call args, and the secret-assignment rule matches 16 spans that cross JSON field boundaries (e.g. a key's closing quote). These are not secrets (the same rules find nothing on the decoded strings) and are reported separately in the manifest.
- A separate count (after round 2) found no `AIza`, PEM, `xai-`, `gsk_`, `ya29.`, `r8_` or `nvapi-` strings, no `eyJ…`-shaped strings, and no JWT candidate (JSON header and payload) in the output. The r1 report wrongly said there were no JWTs: one JWT candidate survived in the r1 output (found by the round-2 review). It is now redacted. In the r1 count, the only `sk-` strings left were 22 fragments of 5–10 characters, such as `sk-ant`, quoted in discussion; not recounted.

**Raw sessions outside the audit set:** the raw session files themselves hold 8 strings starting with `AIza`, one of them 65 characters long, and JWT-shaped strings (r0 count: 2). The r0 note said all of them sit outside the extracted spans; that held for the `AIza` strings, but one JWT candidate was inside them and is now redacted. Only the scrubbed copies may leave the laptop, and of those only records with `scrubbed=true` may go to non-Anthropic models.

## Commands (UTC)

```bash
# from the worktree root
uv run python docs/sprint/scan_compactions.py          # 06:3x: 50 in 31 sessions (was 48 in 30 on 18 Oct)
uv run python -m compaction.sessions count             # 06:38:20Z: this repo 50 (top-level), other projects 0
uv run python -m compaction.sessions build             # first build, 06:41Z and 06:43Z (superseded)
# after review r1:
uv run python -m compaction.sessions build             # 08:00:16Z-08:02:06Z (~110 s), rc 0
uv run python -m compaction.sessions build             # 08:02:06Z-08:03:56Z, rc 0, identical compactions.jsonl
uv run pytest -q tests/compaction/test_sessions.py   # 64 passed
# after review r2:
uv run python -m compaction.sessions build --out <scratch>  # 08:16:08Z (~108 s), rc 0: 46 passed, 4 quarantined
uv run python -m compaction.sessions build             # 08:18:28Z-08:20:16Z, rc 0
uv run python -m compaction.sessions build             # 08:20:16Z-08:22:05Z, rc 0, all three files identical
uv run pytest -q tests/compaction/test_sessions.py   # 81 passed
# after review r3 (final):
uv run python -m compaction.sessions build --out <scratch>  # 08:28:36Z (~133 s), rc 0: 12 passed, 38 quarantined
uv run python -m compaction.sessions build             # 08:31:19Z-08:33:33Z, rc 0
uv run python -m compaction.sessions build             # 08:33:33Z-08:35:46Z, rc 0, all three files identical
uv run pytest -q tests/compaction/test_sessions.py   # 93 passed
uv run ruff check src/compaction/sessions.py tests/compaction/test_sessions.py
```

There were no extra dependencies, no network calls and no API calls.

## Notes for the other units

- **Test imports:** the coordinator deleted `tests/compaction/__init__.py`, which had shadowed `src/compaction` under pytest. The tests now use a plain `from compaction import sessions`.
- **The scope is narrower than the context `preTokens` reports.** The source holds what the transcript records, not the system prompt or tool schemas. Re-compaction experiments that need the full context have to rebuild the system prompt separately.
- **Session 18f6ced4 is the session coordinating this prep work**, and it is still being written. Its two compactions are included, and its records will change only if it compacts again.
- **`logs/costs.jsonl` shows as modified in this worktree.** This unit did not touch it.

## Review response (r1, r2 and r3)

Code reviews (gpt-6-astra): round 1, `logs/reviews/20261020-sprint-prep-a-r1.md` (5 BLOCKING, 2 MINOR); round 2, `logs/reviews/20261020-sprint-prep-a-r2.md` (3 BLOCKING, 1 MINOR: r1 #1 and #2 only partly fixed, plus a JWT and a schema mismatch). All are addressed. Round 3 (`logs/reviews/20261020-sprint-prep-a-r3.md`, the last round) left 2 BLOCKING findings (r1/r2 #1 and #2 still partly open), and both are fixed by failing closed, as the r3 column says; those fixes are not re-reviewed. Tests are in `tests/compaction/test_sessions.py`.

| # | Finding | r1 response | r2 response | r3 response (fixed after the final review round; not re-reviewed) | r3 tests | Covering tests (r1, r2) |
|---|---|---|---|---|---|---|
| 1 | BLOCKING: base64, `\uXXXX`-escaped and split secrets survive scrubbing and verification (r2 #3: combinations still survive) | Escaped and re-joined views, with hits mapped back to the original span; base64 runs decoded at all four alignments | Bounded recursive normalization: escapes, joined literals and base64, up to 3 layers in any order, every detector on every view. Fail-closed quarantine: a credential-shaped token that appears only after decoding, isn't redacted and isn't of a known-safe form leaves the record `scrubbed=false` (Anthropic-only), with reasons. 4 records quarantined | — | — | r1: `test_base64_encoded_secret_redacted_and_gate_catches_it` (8), `test_base64_encoded_named_token_without_env`, `test_json_and_url_escaped_secrets`, `test_secret_split_across_quoted_fragments` (7), `test_named_token_split_without_env`, `test_escaped_and_split_secrets_in_one_text`. r2: `test_r2_combined_encodings_of_an_inventory_secret` (the review's 3 combinations), `test_r2_unknown_encoded_token_is_quarantined` (3), `test_r2_plain_and_safe_decoded_text_not_quarantined`, `test_r2_quarantined_record_is_flagged_anthropic_only`  |
| 2 | BLOCKING: short or punctuated passwords under secret-named keys survive (r2 #2: placeholder exceptions too broad; numbers bypass) | Word-based `is_secret_key`; values redacted whatever the length or punctuation; quoted values parsed to the closing quote | Quoted literals are always redacted unless they are an exact placeholder (`$VAR`, `${VAR}`, `<...>`, `os.environ[...]`, `getenv(...)`). Bare values are kept only when they are code (dotted reference, secret-named variable name, auth scheme). Numbers under secret keys are redacted | — | — | r1: `test_short_and_punctuated_passwords_in_text` (6), `test_short_passwords_under_secret_keys_in_objects`, `test_only_environment_references_kept_in_code` (renamed in r3), `test_is_secret_key`. r2: `test_r2_literal_passwords_redacted` (5, including the review's `correct.horse` and `tr0ub(ador!`), `test_r2_object_literals_and_numbers_redacted_exact_placeholders_kept` (`SecretHorse42`, numbers), `test_r2_bare_environment_references_kept`  |
| 3 | BLOCKING: `scrubbed=true` set before verification; failure still publishes; a missing `.env` silently means no inventory | Inventory required; per-record gate; `scrubbed=True` only on a pass; `VerificationError` on failure; atomic publish after a file scan | `scrubbed=True` now needs both the hard gate and the quarantine check | — | — | `test_build_requires_the_inventory`, `test_build_fails_closed_when_scrubbing_misses`, `test_build_fails_closed_when_file_scan_fails`, `test_build_writes_and_verifies`, `test_r2_quarantined_record_is_flagged_anthropic_only`  |
| 4 | BLOCKING: sidecar metadata not scrubbed | Whole meta line scrubbed and gated | Meta also goes through the quarantine check | — | — | `test_meta_fields_are_scrubbed`  |
| 5 | BLOCKING: sibling rewind branches mixed into `post_messages` | One documented path (the branch continued last) plus side records; abandoned branches dropped and counted. Also fixed 909 dropped parallel tool results and continuations through later-written parents | — | — | — | `test_post_rewind_keeps_only_the_branch_continued_last`, `test_parallel_tool_results_kept_in_source_and_post`, `test_parallel_call_continued_from_first_result_is_not_a_branch`, `test_post_path_follows_a_parent_written_later`, `test_session_compactions`  |
| 6 | MINOR: rule candidates truncated at 500 characters | Rules kept whole; `start`/`end` offsets in `meta.rule_matches` | — | — | — | `test_rules_kept_whole_with_offsets`, `test_rule_offsets_index_published_user_turns`  |
| 7 | MINOR: permissions applied only after writing | Owner-only temporaries (0600 from creation), renamed into place; directory 0700 | — | — | — | `test_outputs_private_from_creation`  |
| r2-1 | BLOCKING: a JWT candidate survives in the output; the report claimed none | — | New `jwt` token pattern (header and payload must decode to JSON), also run on every view. 1 redacted; 0 left. The report's claim is corrected (Verification; Raw sessions) | — | — | `test_jwt_redacted_and_non_jwt_dotted_text_kept`  |
| r2-4 | MINOR: a record with several tool results gave a list-valued `tool_use_id` and merged texts | (Coordinator request, r1: ids kept, `tool_pairing()`; 13,173 of 13,192 calls pair, 19 have results past the 50-message cap) | One tool message per result, each with a str `tool_use_id`; the stale "undeclared field" line is replaced. Pairing unchanged: 13,173 of 13,192 | — | — | `test_several_parallel_tool_calls_pair_by_id`, `test_tool_pairing_reasons`, `test_r2_multi_result_record_gives_one_tool_message_each`  |
