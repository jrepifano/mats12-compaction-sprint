"""Experiment 1 (stage S1) data: index, eligibility, samples, rendering and the manifest (U1).

Frozen interface in docs/sprint/specs/S1.md ("U1 src/compaction/s1_data.py"); shared types in
compaction.s1_types. Inputs (paths relative to the repo root, gitignored):

- Jacob's sessions: data/raw/sprint/sessions/compactions.jsonl, with the sidecar meta.jsonl
  next to it (docs/sprint/prep/a-sessions.md).
- The public sets: data/raw/sprint/public/{swe-chat-v1,posttrainbench,frontierml}.jsonl
  (docs/sprint/prep/b-public-sets.md). They are large, so `index_all` streams them and keeps
  only light index entries (byte offsets via `audit_logs.index_file`); `load` rereads one line.

Eligibility (`eligibility`), in the order the reasons are checked:
  1. no summary text (encrypted Codex summary, failed call, empty text);
  2. not usable (no assistant message before the boundary; public sets also need one after);
  3. summary cut; 4. uncertain boundary; 5. inherits a cut summary;
  6. message text cut: SWE-chat and PostTrainBench need `fully_assessable`; FrontierML keeps
     records whose only source cuts are tool outputs or tool-call arguments (620 expected).
Jacob's records carry no completeness flags, so they are filled from the record and its
sidecar row (`jacob_flags`), by this rule:
  - usable = non-empty summary and at least one assistant message in source_messages. An
    assistant message after the boundary is NOT required (unlike the public sets): S1 judges
    the summary against the history, and 4 of the 50 records end within a few messages of the
    boundary because the session ended. `post_actions_present` records it.
  - fully_assessable = usable, a sidecar row is attached, and its `chain_walk_ok` is true.
    compaction.sessions cuts no text, so no summary or source cut exists. Not counted against a
    record: `quarantine` (a privacy flag; every Jacob record is Anthropic-only anyway, spec D2),
    `previous_summary_in_source` (the earlier summary is complete and is what the summarizer
    saw), abandoned branches (`source_abandoned_records`: rewound branches that were not in the
    context), `carried_preserved_from_previous`, and `chain_start` (informational).

Gaps (`message_gaps`): messages whose text is incomplete in the record.
  - local_cut_60k: public_sets' own 60k cut leaves "\\n[... cut by public_sets: N more chars]" at
    the end of the text (public sets only; Jacob's records are never cut).
  - FrontierML's dataset cuts leave no marker in the text and no per-message flag in the
    record (only per-record counts in source_truncation). The dataset stores a cut field at
    exactly its cap, so a message is marked when a tool output is exactly 8,000 characters
    (dataset_tool_output), a tool call's arguments are exactly 8,000 (dataset_tool_args), or the
    text is exactly 24,000 (dataset_message). This over-marks an uncut field that happens to be
    exactly at the cap; `gaps_exact` is False when the counts disagree with the record's
    source_truncation (measured on the 20 Oct build: 3 of 759 usable records' sources over-mark
    one tool output each, none under-mark).
`render` puts a marker on each such message: "[tool output cut by dataset]", "[tool call
arguments cut by dataset]", "[text cut by dataset]", "[text cut at 60k characters]".

Rendering (`render`) ports the message serialisation of P01's render.py
(probes/P01-compaction-fidelity/p01/render.py: `message_parts`, `serialize`,
`truncate_for_summary`; pi-mono's serializeConversation) into the S1 layout: one block per
message, "[m<i>] <ROLE>: <text>", then one "<tool_call NAME>{json args}" line per tool call,
blocks separated by a blank line. System messages are kept (inherited summaries, re-read files).
Message indices are absolute, so a slice (start, end) keeps the indices of the whole history.
The history is `history_messages(rec)`: source_messages, then the messages kept verbatim
(Jacob's sidecar `preserved_messages`), numbered on, after the line s1_types.KEPT_HEADER.
Jacob's sidecar is bound to the snapshot that was indexed (its sha256 is checked on load).

Sampling uses `audit_logs.sample` (seeded; depends only on the id set). Within each public
source it is stratified by (model, harness), which splits n evenly across strata rather than
proportionally: the manifest records each stratum's population and draw, so analyses can weight.
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from compaction import audit_logs
from compaction.s1_types import KEPT_HEADER, PUBLIC_SOURCES, S1_SEED, anthropic_only

REPO_ROOT = Path(__file__).resolve().parents[2]

SOURCES: dict[str, str] = {
    "jacob": "data/raw/sprint/sessions/compactions.jsonl",
    "swe-chat-v1": "data/raw/sprint/public/swe-chat-v1.jsonl",
    "posttrainbench": "data/raw/sprint/public/posttrainbench.jsonl",
    "frontierml": "data/raw/sprint/public/frontierml.jsonl",
}
META_NAME = "meta.jsonl"  # Jacob's sidecar, next to his compactions.jsonl

PART_A_N = {"swe-chat-v1": 250, "posttrainbench": 150, "frontierml": 200}
PART_B_PUBLIC_N = 50
PART_A_STRATA = "model,harness"

FML_TOOL_CAP = 8_000  # FrontierML stores tool outputs and tool-call arguments up to this
FML_MESSAGE_CAP = 24_000  # and message text up to this
FML_TOLERATED_CUTS = frozenset({"dataset_tool_output", "dataset_tool_args"})
LOCAL_CUT = re.compile(r"\n\[\.\.\. cut by public_sets: \d+ more chars\]$")  # public_sets.py

MARKERS = {
    "dataset_tool_output": "[tool output cut by dataset]",
    "dataset_tool_args": "[tool call arguments cut by dataset]",
    "dataset_message": "[text cut by dataset]",
    "local_cut_60k": "[text cut at 60k characters]",
}
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-")

_INDEX_CACHE: dict[tuple, list[dict]] = {}
_INFO_CACHE: dict[tuple, list[dict]] = {}


# ------------------------------------------------------------------------------------------
# Paths and small helpers
# ------------------------------------------------------------------------------------------


def _resolve(p: str | os.PathLike) -> Path:
    p = Path(p)
    return p if p.is_absolute() else REPO_ROOT / p


def _paths_key(paths: dict[str, str] | None) -> tuple:
    return tuple(sorted((paths or SOURCES).items()))


def source_of(rid: str) -> str:
    return rid.split(":", 1)[0]


def session_of(rid: str) -> str:
    """session_id from a record id "<source>:<session_id>:<n>"."""
    src = source_of(rid)
    return rid[len(src) + 1 : rid.rfind(":")]


def session_group(source: str, session_id: str) -> str:
    """The unit that must not straddle the label split. SWE-chat holds daily snapshots of one
    session under ids "<YYYY-MM-DD>-<uuid>", so its group is the uuid."""
    sid = _DATE_PREFIX.sub("", session_id) if source == "swe-chat-v1" else session_id
    return f"{source}:{sid}"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------
# Gaps and completeness
# ------------------------------------------------------------------------------------------


def _args_len(args: Any) -> int:
    return len(args) if isinstance(args, str) else len(json.dumps(args))


def _gaps_of(msgs: list[dict], source: str) -> dict[int, list[str]]:
    gaps: dict[int, list[str]] = {}
    public = source in PUBLIC_SOURCES
    for i, m in enumerate(msgs):
        text = m.get("text") or ""
        causes: list[str] = []
        if source == "frontierml":
            if len(text) == FML_MESSAGE_CAP:
                causes.append("dataset_message")
            if m.get("role") == "assistant" and any(
                _args_len(c.get("args")) == FML_TOOL_CAP for c in m.get("tool_calls") or ()
            ):
                causes.append("dataset_tool_args")
            if m.get("role") == "tool" and len(text) == FML_TOOL_CAP:
                causes.append("dataset_tool_output")
        if public and LOCAL_CUT.search(text):
            causes.append("local_cut_60k")
        if causes:
            gaps[i] = causes
    return gaps


def preserved_messages(rec: dict) -> list[dict]:
    """Messages kept verbatim beside the summary: a record field `preserved_messages` /
    `preserved`, else Jacob's sidecar row (`rec["meta"]`), else none."""
    kept, _ = audit_logs._preserved(rec, rec.get("meta"))
    return [m for m in kept or [] if isinstance(m, dict)]


def history_messages(rec: dict) -> list[dict]:
    """What the summarizer saw: source_messages, then the preserved (kept-verbatim) messages.
    Indices continue from the source, so message i of the history is index i here."""
    return list(rec.get("source_messages") or []) + preserved_messages(rec)


def message_gaps(
    rec: dict, include_preserved: bool = True
) -> tuple[dict[int, list[str]], bool]:
    """History-message index -> gap causes, and whether the positions are exact (the detected
    counts on the source part match the record's own source_truncation counts; module
    docstring). With include_preserved, the preserved messages are scanned too, numbered on
    from the source."""
    source = rec.get("source") or ""
    n_src = len(rec.get("source_messages") or [])
    msgs = history_messages(rec) if include_preserved else rec.get("source_messages") or []
    gaps = _gaps_of(msgs, source)
    if source not in PUBLIC_SOURCES:
        return gaps, True
    recorded = rec.get("source_truncation")
    if not isinstance(recorded, dict):
        return gaps, False
    skip0 = bool(rec.get("inherited_truncated_summary"))  # not counted in source_truncation
    got: collections.Counter = collections.Counter()
    for i, causes in gaps.items():
        if (skip0 and i == 0) or i >= n_src:
            continue
        got.update(causes)
    want = {k: v for k, v in recorded.items() if v}
    return gaps, dict(got) == want


def jacob_flags(rec: dict, meta: dict | None) -> dict:
    """usable / fully_assessable for one of Jacob's records (rule in the module docstring)."""
    src = rec.get("source_messages") or []
    post = rec.get("post_messages") or []
    has_summary = bool((rec.get("summary") or "").strip())
    history = any(m.get("role") == "assistant" for m in src)
    usable = has_summary and history
    why = None
    if not has_summary:
        why = "no summary text"
    elif not history:
        why = "no assistant message before the boundary"
    elif meta is None:
        why = "no sidecar row"
    elif meta.get("chain_walk_ok") is not True:
        why = "chain walk failed (boundary uncertain)"
    return {
        "has_summary": has_summary,
        "usable": usable,
        "fully_assessable": usable and why is None,
        "not_assessable_why": why,
        "post_actions_present": any(m.get("role") == "assistant" for m in post),
        "summary_truncated": False,
        "source_truncation": {},
        "inherited_truncated_summary": False,
        "boundary_uncertain": meta is None or meta.get("chain_walk_ok") is not True,
    }


def eligibility(rec: dict, meta: dict | None = None) -> tuple[bool, str | None]:
    """(eligible, reason when not) for S1 Part A, by the plan's exclusions (module docstring).
    `rec` is a full record (or any dict with its flag fields and `source`); `meta` is Jacob's
    sidecar row."""
    source = rec.get("source")
    if source == "jacob":
        f = jacob_flags(rec, meta)
        if not f["usable"]:
            return False, f"not usable: {f['not_assessable_why']}"
        if not f["fully_assessable"]:
            return False, f"not fully assessable: {f['not_assessable_why']}"
        return True, None
    if source not in PUBLIC_SOURCES:
        return False, f"source {source!r} not in S1"
    if "summary" in rec and not (rec.get("summary") or "").strip():
        return False, "no summary text"
    if rec.get("has_summary") is False:
        return False, f"no summary text ({rec.get('why_unusable') or 'none stored'})"
    if rec.get("usable") is not True:
        return False, "not usable" if rec.get("usable") is False else "usable not assessed"
    if rec.get("summary_truncated"):
        return False, f"summary cut ({rec.get('summary_truncation')})"
    if rec.get("boundary_uncertain"):
        causes = ",".join(sorted(rec.get("boundary_uncertainty") or {})) or "?"
        return False, f"uncertain boundary ({causes})"
    if rec.get("inherited_truncated_summary"):
        return False, "inherits a cut summary"
    if source == "frontierml":
        bad = set(rec.get("source_truncation") or {}) - FML_TOLERATED_CUTS
        if bad:
            return False, f"message text cut ({','.join(sorted(bad))})"
        return True, None
    if rec.get("fully_assessable") is not True:
        causes = ",".join(sorted(rec.get("source_truncation") or {})) or "?"
        return False, f"not fully assessable ({causes})"
    return True, None


def fully_assessable(rec: dict, meta: dict | None = None) -> bool:
    if rec.get("source") == "jacob":
        return jacob_flags(rec, meta)["fully_assessable"]
    return rec.get("fully_assessable") is True


# ------------------------------------------------------------------------------------------
# Index
# ------------------------------------------------------------------------------------------


_META_CACHE: dict[tuple[str, str], tuple[dict[str, dict], set[str], dict]] = {}


def _load_jacob_meta(jacob_path: Path) -> tuple[dict[str, dict], set[str], dict | None]:
    side = jacob_path.parent / META_NAME
    if not side.is_file():
        return {}, set(), None
    sha = _file_sha256(side)
    key = (str(side), sha)
    if key not in _META_CACHE:
        _META_CACHE[key] = audit_logs.load_meta(side)
    return _META_CACHE[key]


def index_all(paths: dict[str, str] | None = None, *, refresh: bool = False) -> list[dict]:
    """Index entries for every record in the S1 sources, with eligibility fields; no full
    records are kept. Entries come from `audit_logs.index_file` (offset, line sha256, strata
    keys) plus: source, session_id, session_group, harness, model, pre_tokens, n_source,
    usable, fully_assessable, eligible, reason, n_gaps, gaps_exact, history_complete, and for
    Jacob's records post_actions_present and meta_attached. Cached per `paths`."""
    key = _paths_key(paths)
    if key in _INDEX_CACHE and not refresh:
        return _INDEX_CACHE[key]
    out: list[dict] = []
    infos: list[dict] = []
    for file_idx, (source, rel) in enumerate((paths or SOURCES).items()):
        path = _resolve(rel)
        entries, info = audit_logs.index_file(path, file_idx)
        info = info | {"source": source, "rel_path": str(rel)}
        meta_rows, repeated, meta_info = (
            _load_jacob_meta(path) if source == "jacob" else ({}, set(), None)
        )
        if meta_info:
            info["meta"] = meta_info
        by_offset = {e["offset"]: e for e in entries}
        with path.open("rb") as f:
            offset = 0
            for raw in f:
                start, offset = offset, offset + len(raw)
                e = by_offset.get(start)
                if e is None:
                    continue
                rec = json.loads(raw)
                if rec.get("source") != source:
                    raise ValueError(f"{path}:{e['lineno']}: source {rec.get('source')!r} "
                                     f"in the {source!r} file")
                meta = meta_rows.get(rec.get("id")) if rec.get("id") not in repeated else None
                ent = _entry(e, rec, meta, source)
                if meta_info:
                    ent["meta_sha256"] = meta_info["sha256"]
                out.append(ent)
        infos.append(info)
    ids = collections.Counter(e["id"] for e in out)
    dup = [i for i, c in ids.items() if c > 1 or i is None]
    if dup:
        raise ValueError(f"record ids missing or repeated across sources: {dup[:5]}")
    _INDEX_CACHE[key] = out
    _INFO_CACHE[key] = infos
    return out


def _entry(e: dict, rec: dict, meta: dict | None, source: str) -> dict:
    ok, why = eligibility(rec, meta)
    gaps, exact = message_gaps(rec | {"meta": meta})
    fa = fully_assessable(rec, meta)
    sid = rec.get("session_id") or session_of(rec["id"])
    d = {
        **e,
        "source": source,
        "session_id": sid,
        "session_group": session_group(source, sid),
        "harness": rec.get("harness"),
        "model": rec.get("model"),
        "pre_tokens": rec.get("pre_tokens"),
        "n_source": len(rec.get("source_messages") or []),
        "usable": jacob_flags(rec, meta)["usable"] if source == "jacob" else rec.get("usable"),
        "fully_assessable": fa,
        "eligible": ok,
        "reason": why,
        "n_gaps": len(gaps),
        "gaps_exact": exact,
        "history_complete": fa and not gaps,
    }
    if source == "jacob":
        d["post_actions_present"] = jacob_flags(rec, meta)["post_actions_present"]
        d["meta_attached"] = meta is not None
    return d


def file_infos(paths: dict[str, str] | None = None) -> list[dict]:
    """Per-file summaries from the last `index_all` over these paths (sha256, bytes, counts)."""
    key = _paths_key(paths)
    if key not in _INFO_CACHE:
        index_all(paths)
    return _INFO_CACHE[key]


def _by_id(index: list[dict]) -> dict[str, dict]:
    return {e["id"]: e for e in index}


# ------------------------------------------------------------------------------------------
# Loading
# ------------------------------------------------------------------------------------------


def _meta_for(entry: dict) -> dict | None:
    """Jacob's sidecar row, from the sidecar snapshot that was indexed (raises RuntimeError if
    the sidecar changed since; reindex with index_all(refresh=True))."""
    if entry["source"] != "jacob":
        return None
    rows, repeated, info = _load_jacob_meta(Path(entry["file"]))
    want = entry.get("meta_sha256")
    got = info["sha256"] if info else None
    if want != got:
        raise RuntimeError(f"sidecar for {entry['file']} changed since it was indexed "
                           f"({want} -> {got}); rebuild the index")
    return rows.get(entry["id"]) if entry["id"] not in repeated else None


def load(rid: str, index: list[dict] | None = None) -> dict:
    """The full record plus `meta` (Jacob's sidecar row or None), `history_complete` (fully
    assessable and no gap in the rendered source) and `private` (= anthropic_only)."""
    idx = index if index is not None else index_all()
    entry = _by_id(idx).get(rid)
    if entry is None:
        raise KeyError(f"no record {rid!r} in the index")
    rec = audit_logs.load_record(entry)
    meta = _meta_for(entry)
    rec["meta"] = meta
    gaps, _ = message_gaps(rec)
    rec["session_group"] = entry["session_group"]
    rec["history_complete"] = fully_assessable(rec, meta) and not gaps
    rec["private"] = anthropic_only(rec)
    return rec


def post_context(rec: dict) -> list[dict]:
    """Messages in the post-compaction context other than the summary: those kept verbatim
    (Jacob's sidecar `preserved_messages`, or a record field `preserved_messages`/`preserved`)
    followed by the re-attached files and instructions (the leading system messages of
    post_messages). U3's availability check reads these."""
    out = preserved_messages(rec)
    for m in rec.get("post_messages") or []:
        if m.get("role") != "system":
            break
        out.append(m)
    return out


# ------------------------------------------------------------------------------------------
# Rendering
# ------------------------------------------------------------------------------------------


@dataclass
class Rendered:
    text: str
    spans: dict[int, tuple[int, int]]  # message index -> (start, end) offsets in text
    gaps: list[int]  # message indices carrying a gap marker
    bytes: int  # UTF-8 size of text
    n_source: int  # len(source_messages); indices >= n_source are preserved messages
    gap_causes: dict[int, list[str]] = field(default_factory=dict)
    gaps_exact: bool = True  # False: gap positions guessed (FrontierML counts disagree)


def _js_json(v: Any) -> str:
    """JSON.stringify of a tool argument (P01 render._js_json)."""
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def truncate_for_summary(text: str, max_chars: int) -> str:
    """pi-mono `truncateForSummary` (P01 render.truncate_for_summary)."""
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n[... {len(text) - max_chars} more characters truncated]"


def render_message(
    i: int, m: dict, causes: list[str] | None = None, tool_result_max: int | None = None
) -> str:
    """One message block: "[m<i>] <ROLE>: <text>", tool-call lines, then gap markers."""
    role = str(m.get("role") or "unknown")
    text = m.get("text")
    if not isinstance(text, str):
        text = "" if text is None else _js_json(text)
    causes = causes or []
    if "local_cut_60k" in causes:
        text = LOCAL_CUT.sub("", text)
    if role == "tool" and tool_result_max is not None:
        text = truncate_for_summary(text, tool_result_max)
    head = f"[m{i}] {role.upper()}:"
    lines = [f"{head} {text}" if text else head]
    for c in m.get("tool_calls") or []:
        name = c.get("name") if isinstance(c.get("name"), str) and c.get("name") else "unknown"
        args = c.get("args")
        args_s = args if isinstance(args, str) else _js_json({} if args is None else args)
        lines.append(f"<tool_call {name}>{args_s}")
    lines.extend(MARKERS[c] for c in causes)
    return "\n".join(lines)


def render(
    rec: dict, start: int = 0, end: int | None = None, tool_result_max: int | None = None,
    include_preserved: bool = True,
) -> Rendered:
    """The history the summarizer saw, history_messages(rec)[start:end], as text: absolute
    message indices, character spans per message and gap markers (module docstring).

    The preserved (kept-verbatim) messages follow the source, numbered on from it
    ([m<n_source>], ...). The line KEPT_HEADER, then a blank line, comes right before the first
    preserved message's block when that message is in the slice; it lies outside every span.
    include_preserved=False renders source_messages only. Deterministic."""
    n_src = len(rec.get("source_messages") or [])
    msgs = history_messages(rec) if include_preserved else list(rec.get("source_messages") or [])
    all_gaps, exact = message_gaps(rec, include_preserved)
    end = len(msgs) if end is None else min(end, len(msgs))
    start = max(0, start)
    parts: list[str] = []
    spans: dict[int, tuple[int, int]] = {}
    pos = 0
    sep = "\n\n"

    def add(piece: str) -> tuple[int, int]:
        nonlocal pos
        if parts:
            pos += len(sep)
        span = (pos, pos + len(piece))
        pos += len(piece)
        parts.append(piece)
        return span

    for i in range(start, end):
        if i == n_src:
            add(KEPT_HEADER)
        spans[i] = add(render_message(i, msgs[i], all_gaps.get(i), tool_result_max))
    text = sep.join(parts)
    causes = {i: c for i, c in all_gaps.items() if start <= i < end}
    return Rendered(
        text=text,
        spans=spans,
        gaps=sorted(causes),
        bytes=len(text.encode("utf-8")),
        n_source=n_src,
        gap_causes=causes,
        gaps_exact=exact,
    )


# ------------------------------------------------------------------------------------------
# Samples
# ------------------------------------------------------------------------------------------


def _eligible(index: list[dict], source: str) -> list[dict]:
    return [e for e in index if e["source"] == source and e["eligible"]]


def sample_part_a(seed: int = S1_SEED, index: list[dict] | None = None) -> list[str]:
    """Every eligible Jacob record (sorted by id), then SWE-chat 250, PostTrainBench 150 and
    FrontierML 200, each drawn with `audit_logs.sample` stratified by (model, harness)."""
    idx = index if index is not None else index_all()
    out = sorted(e["id"] for e in _eligible(idx, "jacob"))
    for source, n in PART_A_N.items():
        pool = _eligible(idx, source)
        if len(pool) < n:
            raise ValueError(f"Part A needs {n} eligible {source} records, have {len(pool)}")
        picks = audit_logs.sample(pool, n, seed, strata=PART_A_STRATA)
        out.extend(e["id"] for e in picks)
    return out


def pick_part_b(
    part_a: list[str], seed: int = S1_SEED, index: list[dict] | None = None,
    n_public: int = PART_B_PUBLIC_N,
) -> list[str]:
    """All Jacob records in Part A (sorted), then a seeded simple random draw of `n_public`
    SWE-chat records from Part A with harness claude-code and fully assessable."""
    by = _by_id(index if index is not None else index_all())
    jacob = sorted(r for r in part_a if by[r]["source"] == "jacob")
    pool = [
        by[r] for r in part_a
        if by[r]["source"] == "swe-chat-v1" and by[r]["harness"] == "claude-code"
        and by[r]["fully_assessable"]
    ]
    if len(pool) < n_public:
        raise ValueError(f"only {len(pool)} SWE-chat claude-code records in Part A")
    return jacob + [e["id"] for e in audit_logs.sample(pool, n_public, seed)]


def _pick_one_per_session(entries: list[dict], n: int, rng: random.Random) -> list[dict]:
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for e in sorted(entries, key=lambda e: e["id"]):
        groups[e["session_group"]].append(e)
    order = sorted(groups)
    rng.shuffle(order)
    if len(order) < n:
        raise ValueError(f"need {n} sessions, have {len(order)}")
    return [rng.choice(groups[g]) for g in order[:n]]


def label_records(
    part_a: list[str], seed: int = S1_SEED, n_jacob: int = 15, n_public: int = 30,
    index: list[dict] | None = None,
) -> dict[str, list[str]]:
    """Records for judge validation, from Part A: `n_jacob` of Jacob's and `n_public` public
    ones spread evenly across the three sets (remainder to the sets in PUBLIC order), at most
    one record per session group. Within each source the picks are split dev/held, the held
    half taking the extra one when odd, so no session is in both halves."""
    by = _by_id(index if index is not None else index_all())
    rng = random.Random(seed)
    srcs = list(PART_A_N)
    share, rem = divmod(n_public, len(srcs))
    want = {"jacob": n_jacob} | {s: share + (k < rem) for k, s in enumerate(srcs)}
    dev: list[str] = []
    held: list[str] = []
    for source, n in want.items():
        pool = [by[r] for r in part_a if by[r]["source"] == source]
        picks = [e["id"] for e in _pick_one_per_session(pool, n, rng)]
        k = n // 2
        dev.extend(picks[:k])
        held.extend(picks[k:])
    gd = {by[r]["session_group"] for r in dev}
    gh = {by[r]["session_group"] for r in held}
    if gd & gh:  # cannot happen with one record per session; a guard
        raise AssertionError(f"sessions in both halves: {sorted(gd & gh)[:3]}")
    return {"dev": dev, "held": held}


# ------------------------------------------------------------------------------------------
# Manifest
# ------------------------------------------------------------------------------------------


def _strata_counts(ids: list[str], by: dict[str, dict]) -> dict[str, dict[str, int]]:
    out: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in ids:
        e = by[r]
        out[e["source"]][f"{e['model']} | {e['harness']}"] += 1
    return {s: dict(sorted(c.items())) for s, c in sorted(out.items())}


def _write_json(path: Path, obj: dict, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=False, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_manifest(
    part_a: list[str], part_b: list[str], labels: dict[str, list[str]],
    out_private: str | os.PathLike, out_public: str | os.PathLike, *, seed: int = S1_SEED,
    paths: dict[str, str] | None = None,
) -> dict:
    """Write the private manifest (ids, per-record line hashes, eligibility counts and
    reasons, per-stratum populations and draws, file sha256s, seed) with mode 600, and the
    public copy (ids, hashes and counts; no text, no local paths) with mode 644. Returns the
    private manifest."""
    index = index_all(paths)
    by = _by_id(index)
    infos = file_infos(paths)
    files = []
    for info in infos:
        rel = Path(info["rel_path"])
        shown = rel.name if rel.is_absolute() else str(rel)  # no local paths in the manifest
        f = {"source": info["source"], "path": shown, "sha256": info["sha256"],
             "bytes": info["bytes"], "n_records": info["n_records"]}
        if "meta" in info:
            f["meta_sha256"] = info["meta"]["sha256"]
        files.append(f)
    elig: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for e in index:
        elig[e["source"]][e["reason"] or "eligible"] += 1
    eligible_ids = [e["id"] for e in index if e["eligible"]]
    parts = {
        "part_a": list(part_a),
        "part_b": list(part_b),
        "labels_dev": list(labels.get("dev", [])),
        "labels_held": list(labels.get("held", [])),
    }
    public = {
        "stage": "S1",
        "seed": seed,
        "code_sha256": _file_sha256(Path(__file__)),
        "files": files,
        "eligibility": {s: dict(sorted(c.items())) for s, c in sorted(elig.items())},
        "strata_population": _strata_counts(eligible_ids, by),
        "strata_part_a": _strata_counts(part_a, by),
        "counts": {k: len(v) for k, v in parts.items()},
        "ids": parts,
        "line_sha256": {r: by[r]["line_sha256"] for r in sorted(set().union(*parts.values()))},
    }
    private = public | {
        "created_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "records": {
            r: {k: by[r][k] for k in ("source", "session_id", "session_group", "harness",
                                       "model", "pre_tokens", "fully_assessable",
                                       "history_complete", "n_gaps", "gaps_exact")}
            for r in sorted(set().union(*parts.values()))
        },
    }
    _write_json(_resolve(out_private), private, 0o600)
    _write_json(_resolve(out_public), public, 0o644)
    return private
