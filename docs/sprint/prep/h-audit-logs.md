# Sprint prep (h): audit logs for Inspect's viewer and Scout

2026-10-20 (UTC). Tooling for the sprint's audits ([sprint plan](../sprint-plan.md), "Audits"). It turns CompactionRecord JSONL (`src/compaction/schema.py`) into an Inspect `.eval` log of a seeded random sample, one Inspect sample per compaction. Jacob reads the log in Inspect's log viewer and searches it with Inspect Scout. No model is called, nothing is paid, and the outputs stay on the laptop.

## Usage

```bash
# from the repo root (or a worktree root)
uv run --group swarm python -m compaction.audit_logs build \
  --in data/raw/sprint/sessions/compactions.jsonl \
  --in data/raw/sprint/public/swe-chat-v1.jsonl \
  --in data/raw/sprint/public/posttrainbench.jsonl \
  --in data/raw/sprint/public/frontierml.jsonl \
  --n 20 --seed 1 --strata origin \
  --out data/raw/sprint/audit/<name>/

# view (pick a free port; 7575 and 7576 are often taken by running viewers)
uv run --group swarm inspect view --log-dir data/raw/sprint/audit/<name> --port 7580

# search with Scout (needs inspect-ai >= 0.3.281, so outside the swarm group)
uv run --no-project --with inspect-scout==0.6.0 --with 'openai>=3.4' \
  scout view -T data/raw/sprint/audit/<name> --port 7581
```

Options:

- `--in` takes a file or a glob, and can be repeated. Lines that are not compaction records (inventory and run rows, bad JSON) are skipped and counted per file in the manifest. List the record files by name: a glob such as `public/*.jsonl` also picks up the inventory and run files, which works but clutters the manifest.
- `--strata` takes record fields, comma-separated (`source`, `model`, `harness`, `trigger`, `license`, or `source,model`), or `origin`, which is `private` when `license == "private"` (Jacob's sessions) and `public` otherwise. `--strata origin --n 20` gives 10 of Jacob's compactions and 10 public ones. Any other field, and empty or comma-only input, is refused before anything is read.
- `--only-usable` leaves out records that `compaction.public_sets` flags `usable: false` (no history or no action after the summary). By default they can be drawn, which suits an input-data audit.
- **Sidecars:** by default, each input uses the `meta.jsonl` in its own directory, if there is one (`--no-auto-meta` turns that off). For `sessions/compactions.jsonl` it supplies the messages kept verbatim and the names of the re-attached files.
  - A record gets that sidecar's row only if its id is unique both within its input and within the sidecar. Otherwise nothing is attached and the sample's notes say why.
  - `--meta <file>` (repeatable) instead applies the named sidecars to every input and turns automatic discovery off. The build is refused if any of their ids appears in more than one sidecar row or in more than one record.
- `--out` must be a new, empty directory under `data/raw/sprint/audit/` (gitignored). Anything else is refused, because records may be private.

In Python: `audit_logs.sample(records, n, seed, strata=None)` returns the sample, and `audit_logs.build(...)` does everything the CLI does.

## Outputs

`<out>/` (mode 700) holds two files, both mode 600:

- **`<timestamp>_<name>_<id>.eval`**, the Inspect log. Its eval metadata (`log.eval.metadata["audit"]`) holds the seed, n, strata, the sampled ids in order, and each input's path, sha256 and record count.
- **`manifest.json`** holds:
  - the seed, n, strata and the count per stratum;
  - the ids, and for each sample its file, line, the sha256 of its record line, and its conversion notes;
  - each input's sha256, size, record count and skipped lines by reason, and each sidecar's sha256;
  - id fixes, the log's file name and sha256, and the Inspect version.

The umask is 077 while the files are written, so they are never readable by others, even briefly.

## Design

**One sample per compaction.** The sample's input is a one-line header, not the first user turn: id, source, harness, model, trigger, `pre_tokens`, the message counts, the summary's length and the number of rule candidates. The messages come in this order:

1. **`## COMPACTION SUMMARY`** (system role): the summary, headed with the number of messages it replaced.
2. **`## Kept outside the summary`** (system role):
   - the number of messages preserved verbatim, from the record (`preserved_messages`) or the sidecar. For sources that don't record them it says "not recorded for this source", which is not the same as "none".
   - the context re-attached at the start of the post-compaction part: the leading run of system messages, listed by first meaningful line and length, plus the sidecar's file names (e.g. AGENTS.md, MEMORY.md). Their full text stays in part 4.
   - the preserved messages themselves follow this note as real messages (part `preserved`). They go through the same converter as the history, so tool names, arguments, timestamps and paired results are kept, with their own tool-call ids, and skipped items are counted.
3. **`## REPLACED HISTORY`**, a marker, then every source message, oldest first.
4. **`## AFTER COMPACTION`**, a marker, then the post-compaction messages.

Each message's metadata holds its part, its index in the record and its timestamp. Tool-call ids are `<part><message index>.<call index>` (e.g. `source12.0`, `preserved0.0`), so they never collide across parts.

**Tool pairing: by native id when there is one, by position otherwise.**

- **By id.** Unit a's records give each tool call its `id` and each tool message its `tool_use_id`. A result pairs with the call whose id matches, anywhere in the same part (history, preserved messages or post-compaction), whatever the order. A list-valued `tool_use_id` (one message holding several results) is accepted: the message is shown under the first matching call, the other calls count as answered, and the case is noted.
  - A result whose id matches no call is shown as a user message headed "[tool result whose tool_use_id matches no call]" and noted.
  - So are a result for a call that already has one, and a result whose id is shared by several calls (such a repeated call id is never used for pairing).
- **By position,** only for results without an id (the public sets today). This works within blocks. A block is a run of assistant messages with no tool result between them, followed by the tool results up to the next assistant message. Blocks are needed because public Claude Code traces often write one assistant message per parallel call and then the results. Within a block:
  - **Equal counts** pair in order. Nothing can detect an id-less result that arrives out of order.
  - **Fewer results than calls:** for `source == "jacob"` the results pair with the *last* calls, since older `compaction.sessions` builds kept only the last of parallel results. For other sources they pair with the first. Each such block is noted as ambiguous.
  - **More results than calls:** the extra results become user messages headed "[tool result with no matching call]".
- **Either way:**
  - each paired result becomes an Inspect `ChatMessageTool` with the call's id and function, so the viewer shows it under its call and can collapse it;
  - calls left without a result are counted in the notes;
  - `metadata.tool_pairing` gives the number paired by id and by position in each part;
  - the native `tool_use_id` is kept in the result's message metadata.

String arguments (FrontierML) are parsed as JSON when they parse to an object, and kept as `{"input": ...}` otherwise.

**Metadata** (sample metadata, searchable in Scout). It is built from an allowlist, because outcomes must stay sealed from the audit view:

- the record's `source`, `session_id`, `harness`, `model`, `trigger`, `pre_tokens`, `timestamp`, `license`, `revision` and `scrubbed`;
- the message counts and the summary's length;
- `rule_candidates`, the record's rules (text, origin, checkable, plus the sidecar's match kind when the counts line up);
- `flags`: completeness and truncation fields when the record has them: `usable`, `fully_assessable`, `summary_truncated`/`_truncation`, `source_truncated`/`_truncation`, `post_truncated`/`_truncation`, `post_window_capped`, `boundary_uncertain`/`_uncertainty`, `failed_attempts_before`, `inherited_truncated_summary` and a few generic names;
- `provenance`: public_sets' `native_key`, `duplicate_ids` and `duplicate_dirs`;
- `sidecar`: selected `meta.jsonl` fields (an allowlist; nothing else from the sidecar);
- `withheld_fields`: the names, never the values, of any other top-level field. In the current files the only one is PostTrainBench's `judges` (its contamination, disallowed-model and API-use verdicts);
- `conversion_notes`: counts of everything skipped, coerced or ambiguous.

`check_no_outcomes` backs the allowlist up: it refuses metadata with an outcome-like key at any depth (judge, score, violation, trap, grade, label, verdict, contamination, outcome, reward, cheat, hack, flagged). A future flag with such a name therefore fails the build instead of leaking. Outcomes are read only in the analysis step, after its audit is marked done.

**Odd input never crashes the build.** A missing or non-string summary, non-list message fields, non-object messages or tool calls, unknown roles (shown as user messages with the role named), argument types other than objects or strings, non-integer `pre_tokens`, missing fields, records without an id and repeated ids are each handled and recorded in the notes or the manifest.

**Record ids come from provenance, before sampling.** A native id that occurs once is kept. Every occurrence of a repeated id becomes `<id>@<file name>:<line>`, and a record without an id becomes `<file name>:<line>`. If that still collides (the same file name in two directories), the resolved path replaces the file name, and any remaining collision is an error. The ids therefore don't depend on the order of the `--in` files.

**Sampling is reproducible.** Records are sorted by id before the seeded draw, so the sample depends on the set of ids, n, seed and strata, not on file order. With strata, n is split evenly across the strata, sorted by key. A stratum too small for its share gives the rest to the others, and remainders go to strata the seeded generator picks. The final order is a seeded shuffle, so sources are interleaved in the viewer. Rebuilt record files give the same sample as long as the ids are unchanged; the record-line hashes in the manifest show whether the content changed. Each sampled line is reread and checked against the hash taken while indexing, and the build stops if an input changed in between.

**No model is called.** The task runs on `mockllm/model` with a "replay" solver that sets `state.messages` and never calls `generate`. Inspect sample ids are `<position>_<id>` (`001_…`, in sampled order), with `/` and `\` swapped for look-alikes because Inspect stores each sample as a file in the `.eval` zip. The position prefix keeps them unique, and uniqueness is asserted before the replay lookup is built. The record's id is kept in `metadata.audit_id`.

## Checks (2026-10-20, 07:40–08:20 UTC)

- **Tests:** `tests/compaction/test_audit_logs.py`, 45 tests on synthetic records only. They cover:
  - pairing by native id: reversed results, a missing result (no ambiguity note, with either alignment), a result arriving after the next assistant message, a list-valued `tool_use_id`, an unknown id, a second result for one call, repeated call ids, and a mix of id and id-less results. Pairing counts go to `tool_pairing`, not the notes;
  - positional pairing: in order, split assistant messages, both alignments, extra results and a trailing call;
  - the part order and the header;
  - metadata, flags, and outcome exclusion: planted judge, score, violation, trap, grade and label values appear nowhere in the planned sample, the `.eval` zip or the manifest, and `check_no_outcomes` refuses outcome keys;
  - the kept-outside section with and without a sidecar, and preserved tool calls with paired results;
  - odd fields;
  - sampling: determinism, independence from input order, strata redistribution, the `origin` split, and strata validation in the API and the CLI;
  - ids: provenance ids that match across reversed inputs with duplicates (including the `a, a, a#2` case), full paths for the same file name in two directories, and injective sample ids (`a/b` against `a∕b`);
  - sidecars: two directories with the same record id and different preserved messages, in both input orders, each get their own sidecar. Ambiguous explicit sidecars are refused, and a sidecar with a repeated row is not attached;
  - completeness flags (`post_truncated`, `post_truncation` and the boundary fields) and provenance surviving conversion, and the reread hash check;
  - indexing, the refusal of an `--out` outside the audit root, and an end-to-end build that reads the log back.

  They pass under inspect-ai 0.3.257 (`uv run --group swarm pytest -q tests/compaction/test_audit_logs.py`) and 0.3.282 (`uv run --no-project --with 'inspect-ai>=0.3.282' --with 'openai>=3.4' --with pytest --with-editable . python -m pytest -q -p no:cacheprovider tests/compaction/test_audit_logs.py`). Ruff check and format are clean.
- **Real set `data/raw/sprint/audit/20261020-first-set/`**, rebuilt after review r3 at 08:16 UTC with `--n 20 --seed 1 --strata origin` over the four record files above:
  - It holds 10 of Jacob's compactions and 10 public ones (swe-chat-v1 4, frontierml 3, posttrainbench 3). The ids are the same as in every earlier build.
  - Sample metadata keys are only the allowlisted ones. `judges` is listed by name in the 3 PostTrainBench samples, and none of its keys or values appear in the log. The word "contamination" appears only in 4 samples' message text, where the agents discuss it.
  - All 10 sessions samples got their row from the automatic sidecar (no repeated ids). Together they carry 14 preserved messages, including 3 tool calls, each paired with its result.
  - **Tool pairing:** all 2,349 results in Jacob's 10 samples paired by native id (2,205 in history, 141 after compaction, 3 preserved), with no ambiguity. The 1,132 results in the 10 public samples paired by position, because public_sets doesn't emit ids yet.
  - 8 of the 20 samples have conversion notes: 8 calls with no result (5 of them the last call before the 50-message post cap) and 1 ambiguous positional block in a public sample.
  - Log sha256 `86eabfa5…137aed10`; manifest sha256 `60bca9e5…fed4e6a3`.
  - Inputs, unchanged since the r2 rebuild: `sessions/compactions.jsonl` `5740928e…` (50 records), `swe-chat-v1.jsonl` `f81f2324…` (1,016), `posttrainbench.jsonl` `db908a29…` (283), `frontierml.jsonl` `43a00559…` (811), sidecar `meta.jsonl` `0c6d42bb…` (50 rows, none repeated).
- **Viewer:** `inspect view` loads the rebuilt set under both Inspect versions. On ports 7591 (0.3.257) and 7592 (0.3.282), `/` returned 200, `/api/log-files` listed the log, and `/api/logs/<log>` returned it in full (200, 37 MB as JSON, status success, 20 samples, with the summary as the first message of each). Both servers were stopped afterwards and their ports were free.
- **Scout** (first build only): inspect-scout 0.6.0's `transcripts_from(<dir>)` read all 20 transcripts, and each started with the marked summary. The `scout view` UI itself was not opened.

## Limits

- **Pairing is exact only with native ids.** Jacob's records carry them. The public records don't yet, so their results pair by position: a reordered result can't be detected, and when results are missing, which call a result belongs to is a guess. Ambiguous blocks are counted and never hidden.
  - For history: an early sessions build (`89add39b…`) left about 850 of 13,122 tool calls in Jacob's records without a result, because its chain walk kept only the last of parallel results.
- **"Re-attached context" is the leading run of system messages after the boundary.** For Jacob's sessions those are Claude Code's attachments, named by the sidecar. For public sets it is usually empty. The converter doesn't decide whether a given file was re-attached; the sidecar's `reattached_in_post` does that for Jacob's records.
- **Rules are copied as given.** They are heuristic candidates for Jacob's and SWE-chat's records (see unit a), and this tool doesn't judge them.
- **The viewer was checked through its API, not by eye.** How long transcripts (up to about 1,400 messages) feel in the browser is for Jacob's first audit to say. Splitting a sample into the summary plus a pageable history would be the next step if it is unwieldy.
- **The .eval log is not byte-reproducible** (Inspect stamps run ids and times). The sample itself is reproducible, and the manifest's hashes tie it to the inputs.
- **Scout's LLM search** in `scout view` needs a model and key (see unit g, §1.5). Grep search needs neither.

## Review responses

gpt-6-astra reviews: rounds 1–3 (`logs/reviews/20261020-sprint-prep-h-r1.md`, `-r2.md`, `-r3.md`), each FIX BEFORE COMMIT. Round 2 confirmed all round-1 fixes and raised items 6–8. Round 3 confirmed items 6–8 and raised item 9. Item 9 was fixed after the final review round, so it has not been re-reviewed.

| # | Finding | Severity | r1 response | r2 | r3 |
|---|---|---|---|---|---|
| 1 | Outcome fields (PostTrainBench `judges`) leak into sample metadata through `extra_fields` | BLOCKING | Fixed. Metadata is allowlisted. Other fields are withheld, with only their names listed, and `check_no_outcomes` refuses outcome-like keys at any depth. Tests plant judge, score, violation, trap, grade and label values and find none in the sample, the log or the manifest. | Confirmed resolved. Completeness and provenance fields new in the rebuilt inputs were added to the allowlist, so `judges` is now the only withheld field. | Confirmed resolved. |
| 2 | Preserved messages lose tool names, arguments and timestamps, without notes | BLOCKING | Fixed. Preserved messages go through the structured converter (part `preserved`, with distinct tool-call ids, paired results, timestamps and skip counts). Test added. | Confirmed resolved. | Confirmed resolved. |
| 3 | Duplicate-id suffixes depend on input order and can collide (`a, a, a#2`) | MINOR | Fixed. Ids come from provenance (`<id>@<file>:<line>`, or the full path when file names clash), and collisions raise an error. Reversed-input test added. | Confirmed resolved. | Confirmed resolved. |
| 4 | Escaping `/` can map two ids to one sample id | MINOR | Fixed. Sample ids are `<position>_<id>`, and uniqueness is asserted. Test added. | Confirmed resolved. | Confirmed resolved. |
| 5 | Unknown strata silently disable stratification; comma-only input crashes | MINOR | Fixed. Strata are validated in the API and in argparse. Tests added. | Confirmed resolved. | Confirmed resolved. |
| 6 | Sidecars are keyed by native id, so with duplicate ids the last directory's preserved messages win | BLOCKING | — | Fixed. Automatic sidecars are tied to their input's directory. A row is attached only if its id is unique in that input and in that sidecar, and otherwise the sample's notes say why. Explicit `--meta` turns automatic discovery off and is refused if ambiguous. Tests cover two directories with the same id and different preserved messages, in both input orders, the ambiguous explicit cases, and a repeated sidecar row. | Confirmed resolved. |
| 7 | `post_truncated` and `post_truncation` are withheld instead of shown | MINOR | — | Fixed. Both are allowlisted, as are `post_window_capped`, `boundary_uncertain`/`_uncertainty` and `failed_attempts_before`, which appeared in the rebuilt public records. Provenance fields go under `provenance`. A test checks that the values survive. | Confirmed resolved. |
| 8 | Reread records are not checked against the indexed `line_sha256` | MINOR | — | Fixed. `load_record` hashes the reread line and raises if it differs from the indexed hash. Test added. | Confirmed resolved. |
| 9 | Native tool ids (`tool_calls[].id`, `tool_use_id`) are ignored, so reversed results pair silently with the wrong calls | BLOCKING | — | — | Fixed after the final review round; not re-reviewed. Results pair by native id when present, including list-valued `tool_use_id`, and by position only without ids, with ambiguity, unknown ids, repeated ids and second results noted. Tests cover reversed results, missing results and the edge cases. In the rebuilt set all 2,349 results in Jacob's samples pair by id. |
