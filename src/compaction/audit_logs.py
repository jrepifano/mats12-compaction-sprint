"""CompactionRecord JSONL -> Inspect AI eval logs, for auditing seeded random samples.

One Inspect sample per compaction (docs/sprint/prep/h-audit-logs.md has usage and limits).
Nothing calls a model: a "replay" solver sets `state.messages` from the record and the task
runs on `mockllm/model`. Records may be private (Jacob's sessions), so the CLI writes only
under data/raw/sprint/audit/ (gitignored), with mode 600 files in a mode 700 directory.

Message order in each sample:
  1. the compaction summary, marked as such;
  2. "kept outside the summary": a note on the messages preserved verbatim (from the record
     or the sessions sidecar meta.jsonl) and the re-attached context at the start of the
     post-compaction part (listed; full text stays in part 4), then the preserved messages
     themselves, converted like the history (tool calls, results, timestamps);
  3. a marker, then the replaced history (source_messages), complete, with tool calls and
     tool results paired by id so the viewer shows each result under its call;
  4. a marker, then the post-compaction messages.
The sample input is a one-line header, not the first user turn.

Outcomes stay sealed: sample metadata is built from an allowlist (record fields, rule
candidates, completeness flags, selected sidecar fields). Any other top-level field, such as
PostTrainBench's `judges`, is withheld and only its name is listed, and `check_no_outcomes`
refuses metadata whose keys look like outcomes (judges, scores, violations, traps, grades,
labels, verdicts).

Tool pairing. When a tool message carries a native `tool_use_id` (a str, or a list when one
message holds several results), it pairs with the call whose native `tool_calls[].id` matches,
anywhere in the same part, whatever the order. Results whose id matches no call, a repeated
call id, or an already answered call are shown as marked user messages and noted. Results
without an id fall back to position within a block: a run of assistant messages with no tool
result between them, then the tool results up to the next assistant message. (Public Claude
Code traces often split parallel calls into one assistant message per call.) Equal counts pair
in order. With fewer results than calls, they pair with the last calls for source "jacob"
(older compaction.sessions builds kept only the last of parallel results) and with the first
otherwise, and the block is noted as ambiguous. Extra results are shown as user messages
marked "[tool result with no matching call]". Calls left without a result are counted.
metadata["tool_pairing"] says how many results were paired by id and by position.

The conversion is split in two: `plan_sample` builds plain dicts (testable without Inspect)
and `to_inspect` turns them into Inspect messages. Inspect is imported lazily.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import hashlib
import json
import os
import random
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

CONVERTER_VERSION = "audit-logs-v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_ROOT = REPO_ROOT / "data" / "raw" / "sprint" / "audit"

CORE_FIELDS = (
    "id",
    "source",
    "session_id",
    "harness",
    "model",
    "trigger",
    "pre_tokens",
    "timestamp",
    "source_messages",
    "summary",
    "post_messages",
    "user_turns",
    "rules",
    "license",
    "revision",
    "scrubbed",
)
# Optional completeness and truncation flags written by compaction.public_sets (and maybe
# others later). Copied into the sample metadata when present. Any other unknown top-level
# field is withheld (its name only goes under "withheld_fields"): outcomes such as judge
# verdicts, scores or trap flags must not reach the audit view.
FLAG_FIELDS = (
    "usable",
    "fully_assessable",
    "summary_truncated",
    "summary_truncation",
    "post_truncated",
    "post_truncation",
    "post_window_capped",
    "boundary_uncertain",
    "boundary_uncertainty",
    "failed_attempts_before",
    "source_truncated",
    "source_truncation",
    "inherited_truncated_summary",
    "no_boundary",
    "prompt_version_inferred",
    "complete",
    "completeness",
    "truncated",
    "truncation",
)
# Provenance written by compaction.public_sets: the dataset's own key for the event and the
# records or run dirs it duplicates. Copied under "provenance".
PROVENANCE_FIELDS = ("native_key", "duplicate_ids", "duplicate_dirs")
PRESERVED_FIELDS = ("preserved_messages", "preserved")
# Sidecar (compaction.sessions meta.jsonl) fields copied into the sample metadata.
META_FIELDS = (
    "preserved_verbatim",
    "reattached_in_post",
    "instructions_in_source",
    "agents_md_reattached",
    "claude_md_reattached",
    "memory_md_reattached",
    "n_thinking_blocks_dropped",
    "post_messages_total",
    "chain_walk_ok",
    "chain_start",
    "previous_summary_in_source",
    "carried_preserved_from_previous",
    "rules_are_heuristic",
    "rule_method",
    "checkable_assessed",
    "claude_code_version",
    "post_tokens",
)
ROLES = ("user", "assistant", "tool", "system")
INDEXED_STRATA = ("source", "model", "harness", "trigger", "license")
DERIVED_STRATA = ("origin",)  # origin = "private" if license == "private" else "public"
STRATA_FIELDS = INDEXED_STRATA + DERIVED_STRATA
OUTCOME_KEY = re.compile(
    r"judg|score|violat|trap|grade|label|verdict|contaminat|outcome|reward|cheat|hack|flagged",
    re.IGNORECASE,
)


# ------------------------------------------------------------------------------------------
# Reading records
# ------------------------------------------------------------------------------------------


def is_record(obj: Any) -> bool:
    """A JSON line counts as a compaction record if it is an object with a summary or a
    replaced history. Inventory and run rows written next to the records are not."""
    return isinstance(obj, dict) and ("summary" in obj or "source_messages" in obj)


def expand_inputs(patterns: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for p in patterns:
        hits = sorted(glob.glob(p)) if any(ch in p for ch in "*?[") else [p]
        if not hits:
            raise FileNotFoundError(f"no file matches {p}")
        for h in hits:
            path = Path(h)
            if not path.is_file():
                raise FileNotFoundError(f"not a file: {h}")
            if path not in paths:
                paths.append(path)
    return paths


def index_file(path: Path, file_idx: int) -> tuple[list[dict], dict]:
    """Stream one JSONL file. Returns a light index entry per record (id, strata fields, byte
    offset, line sha256) and the file's summary (sha256, counts, skipped lines by reason)."""
    entries: list[dict] = []
    h = hashlib.sha256()
    skipped: collections.Counter = collections.Counter()
    n_lines = 0
    offset = 0
    with path.open("rb") as f:
        for lineno, raw in enumerate(f, 1):
            h.update(raw)
            start = offset
            offset += len(raw)
            if not raw.strip():
                continue
            n_lines += 1
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                skipped["invalid JSON"] += 1
                continue
            if not is_record(obj):
                skipped["not a compaction record (no summary or source_messages)"] += 1
                continue
            entries.append(
                {
                    "id": obj.get("id")
                    if isinstance(obj.get("id"), str) and obj.get("id")
                    else None,
                    "file_idx": file_idx,
                    "file": str(path),
                    "lineno": lineno,
                    "offset": start,
                    "line_sha256": hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest(),
                    "usable": obj.get("usable"),
                    "keys": {k: _strata_value(obj.get(k)) for k in INDEXED_STRATA},
                }
            )
    info = {
        "path": str(path),
        "sha256": h.hexdigest(),
        "bytes": offset,
        "n_lines": n_lines,
        "n_records": len(entries),
        "skipped": dict(skipped),
    }
    return entries, info


def _strata_value(v: Any) -> str:
    return v if isinstance(v, str) else ("none" if v is None else json.dumps(v, default=str))


def assign_ids(entries: list[dict]) -> list[str]:
    """Give every entry a unique id from its provenance, independent of input order.

    A native id that occurs once is kept. Every occurrence of a repeated id becomes
    "<id>@<file name>:<line>", and a record without an id becomes "<file name>:<line>". If that
    still collides (same file name in two directories), the full resolved path is used. Raises
    ValueError if ids are still not unique. Returns notes on what was changed."""
    counts = collections.Counter(e["id"] for e in entries if e["id"] is not None)

    def prov(e: dict, full: bool) -> str:
        where = str(Path(e["file"]).resolve()) if full else Path(e["file"]).name
        return f"{where}:{e['lineno']}"

    def make(e: dict, full: bool) -> str:
        if e["id"] is None:
            return prov(e, full)
        if counts[e["id"]] > 1:
            return f"{e['id']}@{prov(e, full)}"
        return e["id"]

    uids = [make(e, False) for e in entries]
    dup = {u for u, c in collections.Counter(uids).items() if c > 1}
    if dup:
        uids = [make(e, True) if u in dup else u for e, u in zip(entries, uids)]
    if len(set(uids)) != len(uids):
        clash = sorted(u for u, c in collections.Counter(uids).items() if c > 1)[:5]
        raise ValueError(f"record ids still collide after provenance renaming: {clash}")
    notes = []
    for e, u in zip(entries, uids):
        if u != e["id"]:
            why = "no usable id" if e["id"] is None else "repeated id"
            notes.append(f"{e['file']}:{e['lineno']}: {why}; using {u}")
        e["uid"] = u
        e["id_original"] = e["id"]
    return notes


def load_record(entry: dict) -> dict:
    """Reread an indexed record and check it is the line that was indexed (the input may have
    been rebuilt since); raises RuntimeError on a mismatch."""
    with open(entry["file"], "rb") as f:
        f.seek(entry["offset"])
        raw = f.readline()
    if hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest() != entry["line_sha256"]:
        raise RuntimeError(
            f"{entry['file']}:{entry['lineno']} changed since it was indexed; rebuild the set"
        )
    return json.loads(raw)


def load_meta(path: Path) -> tuple[dict[str, dict], set[str], dict]:
    """One sidecar (compaction.sessions meta.jsonl): rows by record id, the ids that occur in
    more than one row (ambiguous, never attached), and its summary for the manifest."""
    data = path.read_bytes()
    rows: dict[str, dict] = {}
    repeated: set[str] = set()
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("id"), str):
            if obj["id"] in rows:
                repeated.add(obj["id"])
            rows[obj["id"]] = obj
    info = {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "rows": len(rows),
        "repeated_ids": len(repeated),
    }
    return rows, repeated, info


def resolve_sidecars(
    paths: list[Path], entries: list[dict], meta_paths: list[str] | None, auto_meta: bool
) -> tuple[Callable[[dict], tuple[dict | None, str | None, str | None]], list[dict]]:
    """Tie sidecars to records by provenance, not by native id alone.

    Automatic (no --meta): input i uses the meta.jsonl in its own directory, if any. A record
    gets that sidecar's row with its native id, unless the id repeats within that input or
    within the sidecar (ambiguous: not attached, and noted). Explicit (--meta, which turns
    automatic discovery off): the sidecars apply to every input, and the build is refused if
    any of their ids occurs in more than one sidecar row or in more than one record.

    Returns lookup(entry) -> (row, status note, sidecar path) and the manifest infos."""
    per_file_ids = collections.Counter((e["file_idx"], e["id"]) for e in entries if e["id"])
    all_ids = collections.Counter(e["id"] for e in entries if e["id"])
    infos: list[dict] = []
    if meta_paths:
        table: dict[str, tuple[dict, str]] = {}
        for mp in meta_paths:
            rows, repeated, info = load_meta(Path(mp))
            infos.append(info | {"mode": "explicit"})
            clash = repeated | {i for i in rows if i in table}
            if clash:
                raise ValueError(
                    f"ambiguous explicit sidecar {mp}: ids in several rows, e.g. "
                    f"{sorted(clash)[:3]}"
                )
            table.update({i: (r, str(mp)) for i, r in rows.items()})
        multi = sorted(i for i in table if all_ids[i] > 1)
        if multi:
            raise ValueError(
                f"ambiguous explicit sidecar: ids shared by several records, e.g. "
                f"{multi[:3]}; drop --meta to use per-directory sidecars"
            )

        def lookup_explicit(e: dict) -> tuple[dict | None, str | None, str | None]:
            hit = table.get(e["id"]) if e["id"] else None
            return (hit[0], None, hit[1]) if hit else (None, None, None)

        return lookup_explicit, infos

    per_input: dict[int, tuple[dict, set, str]] = {}
    loaded: dict[Path, tuple[dict, set, str]] = {}
    if auto_meta:
        for i, p in enumerate(paths):
            side = p.parent / "meta.jsonl"
            if not side.is_file() or side in paths:
                continue
            key = side.resolve()
            if key not in loaded:
                rows, repeated, info = load_meta(side)
                infos.append(info | {"mode": "automatic"})
                loaded[key] = (rows, repeated, str(side))
            per_input[i] = loaded[key]

    def lookup_auto(e: dict) -> tuple[dict | None, str | None, str | None]:
        if e["file_idx"] not in per_input or not e["id"]:
            return None, None, None
        rows, repeated, where = per_input[e["file_idx"]]
        if e["id"] not in rows:
            return None, "sidecar has no row for this id", where
        if per_file_ids[(e["file_idx"], e["id"])] > 1:
            return None, "not attached: id repeats within its input", where
        if e["id"] in repeated:
            return None, "not attached: id repeats within the sidecar", where
        return rows[e["id"]], None, where

    return lookup_auto, infos


# ------------------------------------------------------------------------------------------
# Sampling
# ------------------------------------------------------------------------------------------


def strata_key(strata: str | Callable[[dict], Any] | None) -> Callable[[dict], Any] | None:
    """None, a callable, or comma-separated field names ("source", "source,model"). Fields are
    read from an index entry's "keys" or from a full record; "origin" is derived."""
    if strata is None or callable(strata):
        return strata
    fields = [s.strip() for s in strata.split(",") if s.strip()]
    if not fields:
        raise ValueError(f"empty strata {strata!r}")
    bad = [f for f in fields if f not in STRATA_FIELDS]
    if bad:
        raise ValueError(f"unsupported strata field(s) {bad}; use {', '.join(STRATA_FIELDS)}")

    def get(r: dict, k: str) -> str:
        keys = r.get("keys") if isinstance(r.get("keys"), dict) else r
        if k == "origin":
            return "private" if keys.get("license") == "private" else "public"
        return _strata_value(keys.get(k))

    return lambda r: tuple(get(r, k) for k in fields) if len(fields) > 1 else get(r, fields[0])


def _rid(r: dict) -> str:
    return str(r.get("uid") or r.get("id"))


def sample(
    records: list[dict], n: int, seed: int, strata: str | Callable[[dict], Any] | None = None
) -> list[dict]:
    """Seeded random sample of n records (index entries or full records, each with an id).

    The result depends only on the set of ids, n, seed and strata, not on input order:
    records are sorted by id first. With strata, n is split as evenly as possible across the
    strata (sorted by key); a stratum smaller than its share gives the rest to the others, and
    remainders go to strata picked by the seeded generator. The returned order is a seeded
    shuffle of the picks, so strata are interleaved in the viewer.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    rng = random.Random(seed)
    pool = sorted(records, key=_rid)
    if len({_rid(r) for r in pool}) != len(pool):
        raise ValueError("record ids must be unique before sampling")
    key = strata_key(strata)
    if key is None:
        picks = rng.sample(pool, min(n, len(pool)))
    else:
        groups: dict[Any, list[dict]] = collections.defaultdict(list)
        for r in pool:
            groups[key(r)].append(r)
        order = sorted(groups, key=lambda k: json.dumps(k, default=str))
        alloc = {k: 0 for k in order}
        left = min(n, len(pool))
        while left > 0:
            open_ = [k for k in order if alloc[k] < len(groups[k])]
            share, rem = divmod(left, len(open_))
            extra = set(rng.sample(open_, rem)) if rem else set()
            for k in open_:
                take = min(share + (k in extra), len(groups[k]) - alloc[k])
                alloc[k] += take
                left -= take
        picks = []
        for k in order:
            picks.extend(rng.sample(groups[k], alloc[k]))
    rng.shuffle(picks)
    return picks


# ------------------------------------------------------------------------------------------
# Conversion to plain message dicts
# ------------------------------------------------------------------------------------------


def _text(v: Any) -> tuple[str, bool]:
    """Message text as a string; the flag says it had to be coerced."""
    if isinstance(v, str):
        return v, False
    if v is None:
        return "", True
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return "\n".join(v), True
    return json.dumps(v, ensure_ascii=False, default=str), True


def _args(v: Any) -> tuple[dict, str | None]:
    """Tool-call args as a dict, plus a note when they were not a dict already."""
    if isinstance(v, dict):
        return json.loads(json.dumps(v, default=str)), None
    if v is None:
        return {}, "args missing"
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return {"input": v}, None  # FrontierML stores args as strings; not a problem
        if isinstance(parsed, dict):
            return parsed, None
        return {"input": v}, None
    return {"value": json.loads(json.dumps(v, default=str))}, f"args of type {type(v).__name__}"


def _sys(content: str, part: str) -> dict:
    return {"role": "system", "content": content, "meta": {"part": part}}


def convert_messages(
    msgs: Any, part: str, right_align: bool, notes: collections.Counter
) -> list[dict]:
    """Record messages -> plain message dicts with paired tool calls (module docstring).

    Results carrying a native `tool_use_id` (a str, or a list when one message holds several
    results) pair with the call whose native `id` matches, anywhere in this part. Results
    without one fall back to positional pairing within their block. Counts of each kind go
    under notes["<part>: paired by id"] / ["<part>: paired by position"]; plan_sample moves
    them to metadata["tool_pairing"]."""
    if msgs is None:
        notes[f"{part}: missing"] += 1
        return []
    if not isinstance(msgs, list):
        notes[f"{part}: not a list ({type(msgs).__name__}), skipped"] += 1
        return []
    out: list[dict] = []
    all_calls: list[dict] = []
    answered: set[str] = set()  # our call ids that have a result
    by_native: dict[str, dict] = {}
    dup_native: set[str] = set()
    block_calls: list[dict] = []  # calls of the current block, in order
    block_results: list[dict] = []  # id-less tool results of the current block, in order
    in_results = False

    def unmatched(res: dict, why: str) -> None:
        notes[f"{part}: {why}"] += 1
        res.update(role="user", content=f"[{why}]\n" + res["content"])
        res.pop("tool_call_id", None)
        res.pop("function", None)

    def pair(call: dict, res: dict, how: str) -> None:
        res["tool_call_id"] = call["id"]
        res["function"] = call["function"]
        answered.add(call["id"])
        notes[f"{part}: paired by {how}"] += 1

    def close_block() -> None:
        nonlocal block_calls, block_results, in_results
        pool = [c for c in block_calls if c["id"] not in answered]
        nc, nr = len(pool), len(block_results)
        if nr == nc:
            pairs = list(zip(pool, block_results))
        elif nr < nc:
            if nr:
                aligned = "last" if right_align else "first"
                notes[f"{part}: ambiguous pairing, results paired with the {aligned} calls"] += 1
            calls = pool[nc - nr :] if right_align else pool[:nr]
            pairs = list(zip(calls, block_results))
        else:
            pairs = list(zip(pool, block_results[:nc]))
            for res in block_results[nc:]:
                unmatched(res, "tool result with no matching call")
        for call, res in pairs:
            pair(call, res, "position")
        block_calls, block_results, in_results = [], [], False

    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            notes[f"{part}: non-object message skipped"] += 1
            continue
        role = m.get("role")
        text, coerced = _text(m.get("text", m.get("content")))
        if coerced and "text" in m:
            notes[f"{part}: non-string text coerced"] += 1
        meta = {"part": part, "index": i}
        if isinstance(m.get("ts"), str):
            meta["ts"] = m["ts"]
        if role == "assistant":
            if in_results:
                close_block()
            calls = []
            raw_calls = m.get("tool_calls")
            if raw_calls is not None and not isinstance(raw_calls, list):
                notes[f"{part}: tool_calls not a list, skipped"] += 1
                raw_calls = []
            for j, c in enumerate(raw_calls or []):
                if not isinstance(c, dict):
                    notes[f"{part}: non-object tool call skipped"] += 1
                    continue
                args, why = _args(c.get("args", c.get("arguments")))
                if why:
                    notes[f"{part}: tool call {why}"] += 1
                name = c.get("name") if isinstance(c.get("name"), str) and c.get("name") else None
                if name is None:
                    notes[f"{part}: tool call without a name"] += 1
                call = {"id": f"{part}{i}.{j}", "function": name or "unknown", "arguments": args}
                native = c.get("id")
                if isinstance(native, str) and native:
                    if native in by_native or native in dup_native:
                        notes[f"{part}: repeated native tool-call id, not used for pairing"] += 1
                        dup_native.add(native)
                        by_native.pop(native, None)
                    else:
                        by_native[native] = call
                calls.append(call)
            block_calls.extend(calls)
            all_calls.extend(calls)
            out.append({"role": "assistant", "content": text, "tool_calls": calls, "meta": meta})
        elif role == "tool":
            in_results = True
            res = {"role": "tool", "content": text, "meta": meta}
            out.append(res)
            tid = m.get("tool_use_id")
            if isinstance(tid, str) and tid:
                ids = [tid]
            elif isinstance(tid, list) and tid and all(isinstance(x, str) and x for x in tid):
                ids = list(tid)
            else:
                if tid is not None:
                    notes[f"{part}: unusable tool_use_id, paired by position"] += 1
                block_results.append(res)
                continue
            meta["tool_use_id"] = tid
            if len(ids) > 1:
                notes[f"{part}: one tool message answers several calls, shown under the first"] += 1
            matched = [by_native[x] for x in ids if x in by_native]
            fresh = [c for c in matched if c["id"] not in answered]
            if not matched:
                why = (
                    "tool result whose tool_use_id is repeated among calls"
                    if any(x in dup_native for x in ids)
                    else "tool result whose tool_use_id matches no call"
                )
                unmatched(res, why)
                continue
            if not fresh:
                unmatched(res, "second tool result for one call")
                continue
            pair(fresh[0], res, "id")
            for c in fresh[1:]:
                answered.add(c["id"])  # answered inside the combined message
        elif role in ("user", "system"):
            out.append({"role": role, "content": text, "meta": meta})
        else:
            notes[f"{part}: unknown role {role!r} shown as user"] += 1
            out.append({"role": "user", "content": f"[role={role!r}]\n{text}", "meta": meta})
    close_block()
    missing = sum(1 for c in all_calls if c["id"] not in answered)
    if missing:
        notes[f"{part}: tool calls with no result"] += missing
    return out


def _label(text: str, width: int = 120) -> str:
    """First meaningful line of a message: skips blank lines and lines that are only tags."""
    for line in text.splitlines():
        s = line.strip()
        if s and not re.fullmatch(r"</?[\w:-]+[^>]*>", s):
            return s[:width] + ("…" if len(s) > width else "")
    return "(empty)"


def _preserved(rec: dict, meta: dict | None) -> tuple[list | None, str | None]:
    for k in PRESERVED_FIELDS:
        if isinstance(rec.get(k), list):
            return rec[k], f"record field {k}"
    if meta and isinstance(meta.get("preserved_messages"), list):
        return meta["preserved_messages"], "sidecar meta.jsonl"
    return None, None


def kept_outside_text(
    meta: dict | None, post: list[dict], preserved: list[dict] | None, where: str | None
) -> str:
    lines = ["## Kept outside the summary", ""]
    if preserved is None:
        lines.append("**Preserved verbatim:** not recorded for this source.")
    elif not preserved:
        lines.append(f"**Preserved verbatim:** none ({where}).")
    else:
        lines.append(
            f"**Preserved verbatim:** {len(preserved)} message(s) ({where}), shown right after "
            "this note with their tool calls; they are not in the replaced history below."
        )
    lines.append("")
    lead = []
    for m in post:
        if m["role"] != "system":
            break
        lead.append(m)
    names = meta.get("reattached_in_post") if meta else None
    if names:
        lines.append(f"**Re-attached files (sidecar):** {', '.join(map(str, names))}.")
    if lead:
        lines.append(
            f"**Context re-attached at the start of the post-compaction part:** "
            f"{len(lead)} system message(s), in full under AFTER COMPACTION:"
        )
        for m in lead:
            lines.append(f"- {_label(m['content'])} ({len(m['content']):,} chars)")
    else:
        lines.append(
            "**Context re-attached at the start of the post-compaction part:** none "
            "(the post-compaction part does not start with system messages)."
        )
    return "\n".join(lines)


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def header(rec: dict, uid: str, n_src: int, n_post: int, summary: str) -> str:
    pre = _int(rec.get("pre_tokens"))
    rules = rec.get("rules") if isinstance(rec.get("rules"), list) else []
    return (
        f"{uid} | {rec.get('source')} · {rec.get('harness')} · {rec.get('model')} | "
        f"trigger {rec.get('trigger')}, {f'{pre:,}' if pre is not None else '?'} tokens | "
        f"{n_src} replaced msgs -> summary {len(summary):,} chars -> {n_post} after | "
        f"{len(rules)} rule candidates"
    )


def _small(v: Any) -> Any:
    """Unknown field value for metadata: scalars as-is, containers summarised."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v if not isinstance(v, str) or len(v) <= 500 else v[:500] + "…"
    s = json.dumps(v, default=str)
    if len(s) <= 2000:
        return json.loads(s)
    return f"<{type(v).__name__} of length {len(v)}, omitted>"


def check_no_outcomes(md: dict) -> None:
    """Refuse metadata with an outcome-like key at any depth (conversion notes, whose keys are
    our own messages, and the withheld names are exempt). A guard behind the allowlist."""

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if OUTCOME_KEY.search(str(k)):
                    raise ValueError(f"outcome-like metadata key {path}{k!r}")
                walk(v, f"{path}{k}.")
        elif isinstance(obj, list):
            for x in obj:
                walk(x, path)

    walk({k: v for k, v in md.items() if k not in ("conversion_notes", "withheld_fields")}, "")


def plan_sample(
    rec: dict, uid: str, meta: dict | None = None, sidecar_note: str | None = None
) -> dict:
    """One record -> {"id", "input", "messages", "metadata"} with plain message dicts."""
    notes: collections.Counter = collections.Counter()
    if sidecar_note:
        notes[f"sidecar: {sidecar_note}"] += 1
    if not isinstance(rec, dict):
        raise TypeError("record must be an object")
    right_align = rec.get("source") == "jacob"
    summary, coerced = _text(rec.get("summary"))
    if rec.get("summary") is None:
        notes["summary missing"] += 1
    elif coerced:
        notes["summary not a string, coerced"] += 1
    src = convert_messages(rec.get("source_messages"), "source", right_align, notes)
    post = convert_messages(rec.get("post_messages"), "post", right_align, notes)
    pres_raw, pres_where = _preserved(rec, meta)
    preserved = (
        None if pres_raw is None else convert_messages(pres_raw, "preserved", right_align, notes)
    )
    for k in ("source", "harness", "model", "trigger", "pre_tokens"):
        if k not in rec:
            notes[f"field {k} missing"] += 1
    if "pre_tokens" in rec and rec["pre_tokens"] is not None and _int(rec["pre_tokens"]) is None:
        notes["pre_tokens not an integer"] += 1
    rules_raw = rec.get("rules")
    rules = []
    if isinstance(rules_raw, list):
        for r in rules_raw:
            if isinstance(r, dict):
                rules.append({k: r.get(k) for k in ("text", "origin", "checkable")})
            elif isinstance(r, str):
                rules.append({"text": r, "origin": None, "checkable": None})
            else:
                notes["non-object rule skipped"] += 1
    elif rules_raw is not None:
        notes["rules not a list, skipped"] += 1
    if (
        meta
        and isinstance(meta.get("rule_matches"), list)
        and len(meta["rule_matches"]) == len(rules)
    ):
        for r, mm in zip(rules, meta["rule_matches"]):
            if isinstance(mm, dict):
                r["match"] = mm.get("match")

    marked_summary = (
        "## COMPACTION SUMMARY\n"
        f"_What the model saw in place of the {len(src)} replaced messages below "
        f"({len(summary):,} chars)._\n\n" + (summary or "(no summary text)")
    )
    messages = [
        _sys(marked_summary, "summary"),
        _sys(kept_outside_text(meta, post, preserved, pres_where), "kept_outside"),
    ]
    messages += preserved or []
    messages.append(
        _sys(
            f"## REPLACED HISTORY\n_{len(src)} messages that the summary replaced, oldest first._",
            "marker",
        )
    )
    messages += src
    messages.append(
        _sys(f"## AFTER COMPACTION\n_{len(post)} messages after the summary._", "marker")
    )
    messages += post

    md: dict[str, Any] = {
        "record_id": rec.get("id"),
        **{
            k: rec.get(k)
            for k in (
                "source",
                "session_id",
                "harness",
                "model",
                "trigger",
                "timestamp",
                "license",
                "revision",
                "scrubbed",
            )
        },
        "pre_tokens": _int(rec.get("pre_tokens")),
        "n_source_messages": len(src),
        "n_post_messages": len(post),
        "n_preserved_messages": None if preserved is None else len(preserved),
        "n_user_turns": len(rec["user_turns"]) if isinstance(rec.get("user_turns"), list) else None,
        "summary_chars": len(summary),
        "rule_candidates": rules,
        "n_rule_candidates": len(rules),
    }
    flags = {k: _small(rec[k]) for k in FLAG_FIELDS if k in rec}
    if flags:
        md["flags"] = flags
    prov = {k: _small(rec[k]) for k in PROVENANCE_FIELDS if k in rec}
    if prov:
        md["provenance"] = prov
    allowed = set(CORE_FIELDS) | set(FLAG_FIELDS) | set(PRESERVED_FIELDS) | set(PROVENANCE_FIELDS)
    withheld = sorted(k for k in rec if k not in allowed)
    if withheld:
        md["withheld_fields"] = withheld  # names only; values never enter the audit log
    if meta:
        md["sidecar"] = {k: _small(meta[k]) for k in META_FIELDS if k in meta}
    pairing: dict[str, dict[str, int]] = {}
    for k in [k for k in notes if ": paired by " in k]:
        part, how = k.split(": paired by ")
        pairing.setdefault(part, {})[how] = notes.pop(k)
    md["tool_pairing"] = pairing  # how results were tied to calls, per part (not a problem)
    md["conversion_notes"] = dict(sorted(notes.items()))
    check_no_outcomes(md)
    return {
        "id": uid,
        "input": header(rec, uid, len(src), len(post), summary),
        "messages": messages,
        "metadata": md,
    }


# ------------------------------------------------------------------------------------------
# Inspect
# ------------------------------------------------------------------------------------------


def to_inspect(planned: list[dict], model: str | None = None) -> list:
    from inspect_ai.model import (
        ChatMessageAssistant,
        ChatMessageSystem,
        ChatMessageTool,
        ChatMessageUser,
    )
    from inspect_ai.tool import ToolCall

    out = []
    for m in planned:
        meta = m.get("meta") or None
        if m["role"] == "assistant":
            calls = [
                ToolCall(id=c["id"], function=c["function"], arguments=c["arguments"])
                for c in m.get("tool_calls") or []
            ]
            out.append(
                ChatMessageAssistant(
                    content=m["content"], tool_calls=calls or None, model=model, metadata=meta
                )
            )
        elif m["role"] == "tool" and m.get("tool_call_id"):
            out.append(
                ChatMessageTool(
                    content=m["content"],
                    tool_call_id=m["tool_call_id"],
                    function=m.get("function"),
                    metadata=meta,
                )
            )
        elif m["role"] == "tool":
            # unpaired result left as a tool role by a caller; show as user
            out.append(
                ChatMessageUser(
                    content="[tool result with no matching call]\n" + m["content"], metadata=meta
                )
            )
        elif m["role"] == "system":
            out.append(ChatMessageSystem(content=m["content"], metadata=meta))
        else:
            out.append(ChatMessageUser(content=m["content"], metadata=meta))
    return out


def sample_ids(uids: list[str]) -> list[str]:
    """Inspect sample ids: "<position>_<id>", position 001, 002, ... in sampled order. The
    position makes them unique even though "/" and "\\" are swapped for look-alikes (Inspect
    stores each sample as samples/<id>_epoch_<n>.json inside the .eval zip)."""
    width = max(3, len(str(len(uids))))
    out = [
        f"{k + 1:0{width}d}_" + u.replace("/", "∕").replace("\\", "∖") for k, u in enumerate(uids)
    ]
    if len(set(out)) != len(out) or len(set(uids)) != len(uids):
        raise ValueError("sample ids are not unique")
    return out


def write_eval_log(planned: list[dict], out_dir: Path, task_name: str, eval_metadata: dict) -> Path:
    """Run a no-model Inspect task whose solver replays each planned sample's messages."""
    from inspect_ai import Task
    from inspect_ai import eval as inspect_eval
    from inspect_ai.dataset import MemoryDataset, Sample
    from inspect_ai.solver import Generate, TaskState, solver

    sids = sample_ids([p["id"] for p in planned])
    by_id = dict(zip(sids, planned))
    assert len(by_id) == len(planned)

    @solver
    def replay():
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            p = by_id[str(state.sample_id)]
            state.messages = to_inspect(p["messages"], model=p["metadata"].get("model"))
            return state

        return solve

    samples = [
        Sample(id=sid, input=p["input"], metadata=p["metadata"] | {"audit_id": p["id"]})
        for sid, p in zip(sids, planned)
    ]
    task = Task(dataset=MemoryDataset(samples, name=task_name), solver=replay(), name=task_name)
    logs = inspect_eval(
        task,
        model="mockllm/model",
        log_dir=str(out_dir),
        log_format="eval",
        display="none",
        metadata=eval_metadata,
        score=False,
    )
    log = logs[0]
    if log.status != "success":
        raise RuntimeError(f"Inspect run failed: {log.status} {log.error}")
    loc = str(log.location)
    path = Path(loc.removeprefix("file://"))
    if not path.is_file():
        raise RuntimeError(f"log not found at {loc}")
    return path


# ------------------------------------------------------------------------------------------
# Build
# ------------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_out(out_dir: Path, audit_root: Path) -> Path:
    out = out_dir.resolve()
    root = audit_root.resolve()
    if out != root and root not in out.parents:
        raise ValueError(f"--out must be under {root} (records may be private)")
    if out == root:
        raise ValueError("--out must be a named directory under the audit root")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty; pick a new name")
    return out


def _lock_down(out: Path) -> None:
    os.chmod(out, 0o700)
    for p in out.rglob("*"):
        os.chmod(p, 0o700 if p.is_dir() else 0o600)


def build(
    inputs: list[str],
    n: int,
    seed: int,
    out_dir: Path,
    strata: str | None = None,
    meta_paths: list[str] | None = None,
    name: str | None = None,
    audit_root: Path = AUDIT_ROOT,
    auto_meta: bool = True,
    only_usable: bool = False,
) -> dict:
    """Sample records from the inputs, write the .eval log and manifest.json into out_dir.
    With only_usable, records whose `usable` field is False are left out before sampling
    (records without the field stay). Returns the manifest."""
    strata_key(strata)  # fail fast on unsupported strata
    out = _check_out(out_dir, audit_root)
    paths = expand_inputs(inputs)
    entries: list[dict] = []
    file_infos = []
    for i, p in enumerate(paths):
        e, info = index_file(p, i)
        entries += e
        file_infos.append(info)
    id_notes = assign_ids(entries)
    sidecar_for, meta_infos = resolve_sidecars(paths, entries, meta_paths, auto_meta)

    n_unusable = sum(1 for e in entries if e["usable"] is False)
    if only_usable:
        entries = [e for e in entries if e["usable"] is not False]
    picks = sample(entries, n, seed, strata)
    planned = []
    sidecar_paths = []
    for e in picks:
        rec = load_record(e)
        row, status, where = sidecar_for(e)
        sidecar_paths.append(where if row is not None else None)
        planned.append(plan_sample(rec, e["uid"], row, status))

    task_name = name or out.name
    created = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = [e["uid"] for e in picks]
    eval_md = {
        "audit": {
            "converter": CONVERTER_VERSION,
            "seed": seed,
            "n": n,
            "strata": strata,
            "only_usable": only_usable,
            "ids": ids,
            "created_utc": created,
            "inputs": [{k: f[k] for k in ("path", "sha256", "n_records")} for f in file_infos],
        }
    }
    old_umask = os.umask(0o077)
    try:
        out.mkdir(parents=True, exist_ok=True)
        log_path = write_eval_log(planned, out, re.sub(r"[^\w-]", "_", task_name), eval_md)
        import inspect_ai

        manifest = {
            "name": task_name,
            "created_utc": created,
            "converter": CONVERTER_VERSION,
            "inspect_ai_version": inspect_ai.__version__,
            "seed": seed,
            "n_requested": n,
            "only_usable": only_usable,
            "n_records_flagged_unusable": n_unusable,
            "n_sampled": len(picks),
            "strata": strata,
            "strata_counts": dict(collections.Counter(str(strata_key(strata)(e)) for e in picks))
            if strata
            else None,
            "ids": ids,
            "samples": [
                {
                    "id": e["uid"],
                    "sample_id": sid,
                    "file": e["file"],
                    "line": e["lineno"],
                    "record_line_sha256": e["line_sha256"],
                    "sidecar": side,
                    "conversion_notes": p["metadata"]["conversion_notes"],
                }
                for e, p, sid, side in zip(picks, planned, sample_ids(ids), sidecar_paths)
            ],
            "inputs": file_infos,
            "meta_sidecars": meta_infos,
            "id_notes": id_notes,
            "log_file": log_path.name,
            "log_sha256": _sha256(log_path),
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    finally:
        os.umask(old_umask)
    _lock_down(out)
    return manifest


def _strata_arg(value: str) -> str:
    try:
        strata_key(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m compaction.audit_logs",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="sample records and write an Inspect .eval log")
    b.add_argument(
        "--in",
        dest="inputs",
        action="append",
        required=True,
        help="record JSONL file or glob (repeatable); non-record lines are skipped",
    )
    b.add_argument("--n", type=int, required=True)
    b.add_argument("--seed", type=int, required=True)
    b.add_argument(
        "--strata",
        default=None,
        type=_strata_arg,
        help="comma-separated record fields (e.g. source, model, harness) or "
        "'origin' (private vs public, from license)",
    )
    b.add_argument(
        "--meta",
        action="append",
        default=[],
        help="sidecar meta.jsonl keyed by record id, applied to every input; turns off "
        "the automatic per-directory sidecars and is refused if any of its ids is ambiguous",
    )
    b.add_argument("--no-auto-meta", action="store_true")
    b.add_argument(
        "--only-usable",
        action="store_true",
        help="leave out records with usable=false (compaction.public_sets flag)",
    )
    b.add_argument("--name", default=None, help="task name (default: the out dir's name)")
    b.add_argument(
        "--out",
        required=True,
        type=Path,
        help=f"new directory under {AUDIT_ROOT.relative_to(REPO_ROOT)}/",
    )
    args = ap.parse_args(argv)
    m = build(
        args.inputs,
        args.n,
        args.seed,
        args.out,
        strata=args.strata,
        meta_paths=args.meta,
        name=args.name,
        auto_meta=not args.no_auto_meta,
        only_usable=args.only_usable,
    )
    out = args.out.resolve()
    print(
        f"wrote {out / m['log_file']} ({m['n_sampled']} samples, seed {m['seed']}, "
        f"strata {m['strata']}: {m['strata_counts']})"
    )
    print(f"manifest {out / 'manifest.json'}")
    for f in m["inputs"]:
        if f["skipped"]:
            print(f"  skipped in {f['path']}: {f['skipped']}")
    noted = {s["id"]: s["conversion_notes"] for s in m["samples"] if s["conversion_notes"]}
    print(f"  samples with conversion notes: {len(noted)} of {m['n_sampled']}")
    print(f"view: uv run --group swarm inspect view --log-dir {out} --port 7580  (any free port)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
