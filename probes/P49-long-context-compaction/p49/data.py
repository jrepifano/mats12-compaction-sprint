"""SWE-rebench OpenHands trajectories -> cleaned Sessions, and the length index (plan "Setup").

Reads `C.PARQUET` (67,074 rows, 17 row groups) with pyarrow by row group and only the needed
columns. Arrow merges struct schemas across rows, so every message and tool dict carries null
keys from other rows: they are stripped recursively. Messages are normalised to the exact
OpenAI format in contracts.py (content always a str; non-standard keys such as a tool
message's `name` dropped; tool-call ids kept).

The dangling-call cut: a session is cut back to its last complete exchange, i.e. the longest
prefix in which every assistant tool call is answered by the tool messages that immediately
follow it (and no tool message answers an unknown id). Almost every session ends with an
assistant tool call (`finish`, or the call at the iteration limit) whose result was never
recorded; that message is removed. No result is ever invented. `Session.cut_dangling` records
how many body messages were removed.

Index (`C.INDEX_JSON`, gitignored): one record per session plus a header with the parquet's
full sha256 and a `cache` dict of exact token counts filled lazily by stitch/replay.
Build: `uv run python -m p49.data --index` (resumable by row group; written atomically).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path

from p49 import contracts as C

COLUMNS = ["trajectory_id", "instance_id", "exit_status", "trajectory", "tools"]
INDEX_VERSION = 1
_PARTS_DIR = C.RAW / "index_parts"


# --- cleaning --------------------------------------------------------------------------------
def strip_none(x):
    """Recursively drop None-valued keys (Arrow's merged struct schema)."""
    if isinstance(x, dict):
        return {k: strip_none(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [strip_none(v) for v in x]
    return x


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_obj(obj) -> str:
    return sha256_text(canonical(obj))


def normalize_exit(raw: str | None) -> str:
    s = raw or ""
    if s.startswith("RuntimeError: Agent reached maximum iteration"):
        return "iteration_limit"
    if s == "submit":
        return "submitted"
    if s.startswith("AgentStuckInLoopError"):
        return "stuck"
    if s.startswith("Timeout"):
        return "timeout"
    if not s or s == "unknown":
        return "unknown"
    return "error"


def normalize_message(m: dict) -> dict:
    """One trajectory message -> the contracts.py OpenAI format."""
    role = m.get("role")
    content = m.get("content")
    content = "" if content is None else str(content)
    if role in ("system", "user"):
        return {"role": role, "content": content}
    if role == "assistant":
        out: dict = {"role": "assistant", "content": content}
        calls = []
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls.append({"id": tc["id"], "type": "function",
                          "function": {"name": fn.get("name", ""),
                                       "arguments": fn.get("arguments") or "{}"}})
        if calls:
            out["tool_calls"] = calls
        return out
    if role == "tool":
        return {"role": "tool", "tool_call_id": m["tool_call_id"], "content": content}
    raise ValueError(f"unknown role {role!r}")


def normalize_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in strip_none(tools or []):
        fn = t.get("function") or {}
        out.append({"type": t.get("type", "function"), "function": fn})
    return out


def complete_prefix_len(body: list[dict]) -> int:
    """Length of the longest prefix of `body` made of complete exchanges."""
    good = 0
    i, n = 0, len(body)
    seen_ids: set[str] = set()
    while i < n:
        m = body[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            if len(set(ids)) != len(ids) or seen_ids.intersection(ids):
                return good
            j, answered, stray = i + 1, set(), False
            while j < n and body[j]["role"] == "tool":
                tid = body[j]["tool_call_id"]
                if tid not in ids or tid in answered:
                    stray = True                   # a result for no pending call: cut there
                    break
                answered.add(tid)
                j += 1
            if answered != set(ids):
                return good
            if stray:
                return j
            seen_ids.update(ids)
            i = j
            good = i
        elif m["role"] == "tool":
            return good                        # orphan tool result
        elif m["role"] in ("assistant", "user"):
            i += 1
            good = i
        else:
            return good
    return good


def clean_row(row: dict) -> C.Session:
    traj = strip_none(row["trajectory"] or [])
    if len(traj) < 2 or traj[0].get("role") != "system" or traj[1].get("role") != "user":
        raise ValueError(f"{row.get('trajectory_id')}: trajectory does not start system, user")
    msgs = [normalize_message(m) for m in traj]
    body = msgs[2:]
    keep = complete_prefix_len(body)
    sess = C.Session(trajectory_id=row["trajectory_id"], instance_id=row["instance_id"],
                     exit_status=normalize_exit(row.get("exit_status")),
                     system=msgs[0]["content"], task=msgs[1]["content"], body=body[:keep],
                     tools=normalize_tools(row.get("tools")), cut_dangling=len(body) - keep)
    sess.chars = message_chars([{"role": "system", "content": sess.system},
                                {"role": "user", "content": sess.task}, *sess.body])
    return sess


def message_chars(messages: list[dict]) -> int:
    """Content + tool-call name/arguments characters (the index's size measure)."""
    n = 0
    for m in messages:
        n += len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            n += len(tc["function"]["name"]) + len(tc["function"]["arguments"])
    return n


def session_messages(sess: C.Session) -> list[dict]:
    """The session as it ran: [system, task, *body]."""
    return [{"role": "system", "content": sess.system}, {"role": "user", "content": sess.task},
            *sess.body]


# --- reading ---------------------------------------------------------------------------------
def _parquet():
    import pyarrow.parquet as pq
    return pq.ParquetFile(C.PARQUET)


def iter_sessions(columns=COLUMNS, row_groups=None):
    """Yield (row_group, row, Session) over the parquet, one row group at a time."""
    f = _parquet()
    for rg in (range(f.num_row_groups) if row_groups is None else row_groups):
        rows = f.read_row_group(rg, columns=list(columns)).to_pylist()
        for i, r in enumerate(rows):
            yield rg, i, clean_row(r)


_RG_CACHE: OrderedDict = OrderedDict()
_RG_LOCK = threading.Lock()
_RG_CACHE_MAX = 3


def _row_group_table(rg: int):
    with _RG_LOCK:
        if rg in _RG_CACHE:
            _RG_CACHE.move_to_end(rg)
            return _RG_CACHE[rg]
        tb = _parquet().read_row_group(rg, columns=COLUMNS)
        _RG_CACHE[rg] = tb
        while len(_RG_CACHE) > _RG_CACHE_MAX:
            _RG_CACHE.popitem(last=False)
        return tb


def load_session(trajectory_id: str, index: dict | None = None) -> C.Session:
    idx = index or load_index()
    rec = idx["sessions"][trajectory_id]
    tb = _row_group_table(rec["row_group"])
    row = tb.slice(rec["row"], 1).to_pylist()[0]
    if row["trajectory_id"] != trajectory_id:
        raise RuntimeError(f"index points {trajectory_id} at {row['trajectory_id']}")
    return clean_row(row)


# --- index -----------------------------------------------------------------------------------
def file_sha256(path: Path, chunk: int = 1 << 24) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while b := fh.read(chunk):
            h.update(b)
    return h.hexdigest()


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False))
    os.replace(tmp, path)


def index_record(rg: int, row: int, s: C.Session) -> dict:
    return {"trajectory_id": s.trajectory_id, "instance_id": s.instance_id, "row_group": rg,
            "row": row, "exit_status": s.exit_status, "n_messages": 2 + len(s.body),
            "chars": s.chars, "est_tokens": int(s.chars * C.CHAR_TOK_EST),
            "system_sha256": sha256_obj(s.system), "tools_sha256": sha256_obj(s.tools),
            "cut_dangling": s.cut_dangling}


def build_index(path: Path = C.INDEX_JSON, *, force: bool = False) -> dict:
    """One pass over every row; per-row-group parts make it resumable. Returns the index."""
    if path.exists() and not force:
        return load_index(path)
    t0 = time.time()
    f = _parquet()
    _PARTS_DIR.mkdir(parents=True, exist_ok=True)
    timings = {}
    for rg in range(f.num_row_groups):
        part = _PARTS_DIR / f"rg{rg:02d}.json"
        if part.exists() and not force:
            continue
        t1 = time.time()
        recs, errors = [], []
        rows = f.read_row_group(rg, columns=COLUMNS).to_pylist()
        for i, r in enumerate(rows):
            try:
                recs.append(index_record(rg, i, clean_row(r)))
            except (ValueError, KeyError) as e:
                errors.append({"row_group": rg, "row": i, "trajectory_id": r.get("trajectory_id"),
                               "error": str(e)[:200]})
        atomic_write_json(part, {"records": recs, "errors": errors})
        timings[rg] = round(time.time() - t1, 2)
    t_rows = time.time() - t0
    sessions, errors = {}, []
    for rg in range(f.num_row_groups):
        part = json.loads((_PARTS_DIR / f"rg{rg:02d}.json").read_text())
        for rec in part["records"]:
            sessions[rec["trajectory_id"]] = rec
        errors.extend(part["errors"])
    t2 = time.time()
    sha = file_sha256(C.PARQUET)
    if not sha.startswith(C.PARQUET_SHA256_PREFIX):
        raise RuntimeError(f"parquet sha256 {sha} does not start {C.PARQUET_SHA256_PREFIX}")
    old_cache = {}
    if path.exists():
        old_cache = json.loads(path.read_text()).get("cache", {})
    header = {"version": INDEX_VERSION, "parquet": str(C.PARQUET), "parquet_sha256": sha,
              "dataset": C.DATASET, "revision": C.DATASET_REVISION,
              "n_rows": f.metadata.num_rows, "n_sessions": len(sessions), "n_errors": len(errors),
              "cut_dangling": dict(Counter(str(r["cut_dangling"]) for r in sessions.values())),
              "char_tok_est": C.CHAR_TOK_EST,
              "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "rows_pass_s": round(t_rows, 1), "sha256_s": round(time.time() - t2, 1),
              "row_group_s": timings}
    idx = {"header": header, "sessions": sessions, "errors": errors, "cache": old_cache}
    atomic_write_json(path, idx)
    _INDEX.clear()
    return idx


_INDEX: dict = {}
_INDEX_LOCK = threading.Lock()
INDEX_WRITE_LOCK = threading.Lock()     # held while the cache is written or dumped


def load_index(path: Path = C.INDEX_JSON) -> dict:
    """The index, loaded once per process (and its exact-count cache)."""
    with _INDEX_LOCK:
        key = str(path)
        if key not in _INDEX:
            if not path.exists():
                raise FileNotFoundError(f"{path} missing: run `python -m p49.data --index`")
            _INDEX[key] = json.loads(path.read_text())
        return _INDEX[key]


def save_index(idx: dict | None = None, path: Path = C.INDEX_JSON) -> None:
    """Persist the index (with its cache) atomically."""
    with _INDEX_LOCK, INDEX_WRITE_LOCK:
        atomic_write_json(path, idx if idx is not None else _INDEX[str(path)])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--index", action="store_true", help="build the length index")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if a.index:
        idx = build_index(force=a.force)
        print(json.dumps(idx["header"], indent=1))


if __name__ == "__main__":
    main()
