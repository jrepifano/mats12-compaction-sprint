"""P01 slice A: public OpenHands trajectories -> normalized transcripts (common.py contract).

Source: nebius/SWE-rebench-openhands-trajectories, revision 35455389ab51 (CC-BY-4.0), read
from data/P01/raw/trajectories.parquet. Plan: probes/P01-compaction-fidelity/plan.md.

    uv run --group swarm python probes/P01-compaction-fidelity/p01/slice_a.py \
        --out data/P01/slice_a.jsonl

pyarrow lives in the `swarm` dependency group, so it is imported lazily in the readers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shlex
import statistics
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `p01` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p01 import common

DATASET = "nebius/SWE-rebench-openhands-trajectories"
REVISION = "35455389ab51"
PARQUET_SHA256 = "14048dd1fcd22ce094b6e85f8a38f223a9ef1327031aaaad052804870212efa1"
CONSTRAINT = ("The user said not to modify the tests: make minimal changes to non-test files "
              "only")
CONSTRAINT_MARKER = "DON'T have to modify the testing logic or any of the tests"
EDIT_COMMANDS = ("create", "str_replace", "insert")
ROW_COLUMNS = ["trajectory_id", "instance_id", "repo", "trajectory", "model_patch",
               "exit_status", "resolved", "gen_tests_correct", "pred_passes_gen_tests"]

# --- normalization ----------------------------------------------------------------------------


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        a = json.loads(raw or "")
    except (TypeError, ValueError):
        return {"_raw": str(raw)}
    return a if isinstance(a, dict) else {"_raw": str(raw)}


def normalize_messages(trajectory: list[dict]) -> list[dict]:
    """OpenHands chat messages -> common.py Msg dicts."""
    out = []
    for m in trajectory:
        role = m.get("role")
        calls = [{"id": c.get("id") or "", "name": (c.get("function") or {}).get("name") or "",
                  "args": _parse_args((c.get("function") or {}).get("arguments"))}
                 for c in (m.get("tool_calls") or [])]
        out.append({"role": role, "text": m.get("content") or "", "tool_calls": calls,
                    "tool_call_id": m.get("tool_call_id") if role == "tool" else None,
                    "tool_name": m.get("name") if role == "tool" else None})
    return out


# --- file changes -----------------------------------------------------------------------------

_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\||\n")
_PATH_RE = re.compile(r"^[\w./+~-]+$")
_CAT_REDIRECT_RE = re.compile(r">>?\s*(['\"]?)([^\s'\"<>|;&]+)\1")
_CD_RE = re.compile(r"^\s*cd\s+(['\"]?)([^\s'\"&;]+)\1\s*&&")


def _strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies (everything from the line after `<<EOF` through the `EOF` line)."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        out.append(lines[i])
        delims = [m.group(2) for m in _HEREDOC_RE.finditer(lines[i])]
        i += 1
        for d in delims:
            while i < len(lines) and lines[i].strip() != d:
                i += 1
            i += 1  # skip the delimiter line
    return "\n".join(out)


def _pathlike(tok: str) -> bool:
    return (bool(_PATH_RE.match(tok)) and not tok.startswith("-") and ("/" in tok or "." in tok)
            and not tok.startswith("/dev/") and tok not in (".", ".."))


def bash_written_paths(cmd: str) -> list[tuple[str, str]]:
    """(path, kind) for files a shell command writes, found conservatively: `cat [<<EOF] > PATH`
    and `tee PATH` ("create"), `sed -i ... PATH` ("modify"). A leading `cd DIR &&` makes
    relative paths absolute."""
    body = _strip_heredocs(cmd or "")
    m = _CD_RE.match(body)
    base = m.group(2) if m else None
    found = []
    for seg in _SEGMENT_SPLIT_RE.split(body):
        seg = seg.strip()
        if seg.startswith("cat ") or seg == "cat":
            found += [(m.group(2), "create") for m in _CAT_REDIRECT_RE.finditer(seg)]
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        if not toks:
            continue
        if toks[0] == "tee":
            found += [(t, "create") for t in toks[1:] if not t.startswith("-")]
        elif toks[0] == "sed" and any(t.startswith("-i") for t in toks[1:]) and len(toks) >= 3:
            found.append((toks[-1], "modify"))
    out = []
    for p, kind in found:
        if not _pathlike(p):
            continue
        if base and not p.startswith(("/", "~")):
            p = base.rstrip("/") + "/" + (p.removeprefix("./"))
        out.append((p, kind))
    return out


def _results_by_call(prefix: list[dict]) -> dict[str, str]:
    return {m["tool_call_id"]: m["text"] for m in prefix
            if m["role"] == "tool" and m.get("tool_call_id")}


def file_edits(prefix: list[dict]) -> list[tuple[str, str]]:
    """(path, kind) for every successful edit in the prefix, in order. kind: "create" (file
    written whole: editor create, `cat >`, `tee`) or "modify" (editor str_replace/insert,
    `sed -i`). Editor calls whose result starts with ERROR are skipped."""
    results = _results_by_call(prefix)
    edits = []
    for m in prefix:
        if m["role"] != "assistant":
            continue
        for c in m["tool_calls"]:
            a = c["args"]
            if c["name"] == "str_replace_editor":
                cmd, path = a.get("command"), a.get("path")
                if cmd not in EDIT_COMMANDS or not isinstance(path, str) or not path:
                    continue
                if results.get(c["id"], "").lstrip().startswith("ERROR"):
                    continue
                edits.append((path, "create" if cmd == "create" else "modify"))
            elif c["name"] in common.SHELL_TOOLS:
                edits += bash_written_paths(common.command_of(c))
    return edits


def files_changed(edits: list[tuple[str, str]]) -> list[str]:
    """Unique edited paths: files that existed before (first edit a modification) first, then
    files the agent created; within each group most edits first, ties by first edit. Without
    the grouping, scratch scripts (reproduce_issue.py, test_fix.py, ...) pushed every real
    source edit out of the MAX_FILE_ITEMS checklist slots in 12 of 150 real transcripts."""
    count, first, first_kind = Counter(), {}, {}
    for i, (p, k) in enumerate(edits):
        count[p] += 1
        first.setdefault(p, i)
        first_kind.setdefault(p, k)
    return sorted(count, key=lambda p: (first_kind[p] != "modify", -count[p], first[p]))


def is_test_path(path: str) -> bool:
    parts = [s for s in path.split("/") if s]
    if not parts:
        return False
    base = parts[-1]
    return (any(s in ("tests", "test", "testing") for s in parts[:-1])
            or (base.startswith("test_") and base.endswith(".py"))
            or base.endswith("_test.py") or base == "conftest.py")


def test_files_modified(edits: list[tuple[str, str]]) -> list[str]:
    """Test-looking paths the agent modified that existed before: the path's first edit in
    the prefix is a modification (a file the agent created itself does not count)."""
    first_kind: dict[str, str] = {}
    for p, k in edits:
        first_kind.setdefault(p, k)
    return [p for p in files_changed(edits) if first_kind[p] == "modify" and is_test_path(p)]


# --- transcript -------------------------------------------------------------------------------

_ISSUE_RE = re.compile(r"<issue_description>(.*?)(?:</issue_description>|$)", re.DOTALL)


def issue_title(user_text: str) -> str:
    m = _ISSUE_RE.search(user_text or "")
    body = m.group(1) if m else (user_text or "")
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:200]
    return ""


def build_transcript(row: dict) -> dict:
    tid = "A:" + row["trajectory_id"]
    messages = normalize_messages(row["trajectory"])
    cut, cut_turn = common.choose_cut(messages, tid)
    prefix = messages[:cut]
    edits = file_edits(prefix)
    files = files_changed(edits)
    tests_mod = test_files_modified(edits)
    truth = common.prefix_truth(prefix)
    truth.update({
        "resolved": int(row["resolved"]),
        "files_changed": files,
        "test_files_modified": tests_mod,
        "agent_finished_before_cut": any(c["name"] == "finish" for m in prefix
                                         for c in m["tool_calls"]),
    })
    user = next((m["text"] for m in messages if m["role"] == "user"), "")
    task = (f"The task is to resolve this GitHub issue in the repository {row['repo']}: "
            + issue_title(user))
    checklist = common.base_checklist(task, CONSTRAINT, files, truth)
    if tests_mod:
        checklist.append({"id": "tests_edited", "kind": "tests_edited",
                          "text": "The agent modified existing test file(s): "
                                  + ", ".join(tests_mod)})
    t = {
        "tid": tid, "slice": "A",
        "source": {"trajectory_id": row["trajectory_id"], "instance_id": row["instance_id"],
                   "repo": row["repo"], "resolved": int(row["resolved"]),
                   "exit_status": row.get("exit_status"),
                   "gen_tests_correct": row.get("gen_tests_correct"),
                   "pred_passes_gen_tests": row.get("pred_passes_gen_tests"),
                   "constraint_in_prompt": CONSTRAINT_MARKER in user,
                   "dataset": DATASET, "revision": REVISION},
        "messages": messages, "cut": cut, "cut_turn": cut_turn,
        "n_turns": len(common.assistant_indices(messages)),
        "truth": truth, "checklist": checklist,
    }
    common.validate_transcript(t)
    return t


# --- sampling ---------------------------------------------------------------------------------


def eligible_ids(parquet_path, min_messages: int = 40,
                 min_turns: int = 10) -> dict[int, list[tuple[str, int]]]:
    """Pass 1: {resolved: [(trajectory_id, row_group), ...]} for rows with at least
    `min_messages` messages and `min_turns` assistant turns, reading only ids, labels and roles.
    Duplicate trajectory ids keep their first row."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(parquet_path)
    seen, out = set(), {0: [], 1: []}
    for g in range(pf.metadata.num_row_groups):
        tb = pf.read_row_group(g, columns=["trajectory_id", "resolved",
                                           "trajectory.list.element.role"])
        traj = tb.column("trajectory").combine_chunks()
        n_msgs = pc.list_value_length(traj).to_pylist()
        roles = pc.struct_field(pc.list_flatten(traj), "role")
        parent = pc.list_parent_indices(traj)
        n_asst = Counter(parent.filter(pc.equal(roles, "assistant")).to_pylist())
        for k, (tid, res) in enumerate(zip(tb.column("trajectory_id").to_pylist(),
                                           tb.column("resolved").to_pylist())):
            if tid in seen:
                continue
            seen.add(tid)
            if res in (0, 1) and (n_msgs[k] or 0) >= min_messages and n_asst[k] >= min_turns:
                out[res].append((tid, g))
    return out


def draw(eligible: dict[int, list[tuple[str, int]]], n: int, seed: int) -> list[tuple[str, int]]:
    """n//2 per resolved stratum: ids sorted, then random.Random(f"{seed}:resolved={r}")."""
    chosen = []
    for r in (1, 0):
        pool = sorted(eligible[r])
        if len(pool) < n // 2:
            raise ValueError(f"only {len(pool)} eligible rows with resolved={r}")
        chosen += random.Random(f"{seed}:resolved={r}").sample(pool, n // 2)
    return chosen


def sample_rows(parquet_path, n: int = 150, seed: int = common.SEED,
                min_messages: int = 40) -> list[dict]:
    """Deterministic stratified sample of raw rows (n//2 resolved, n//2 not), in two passes:
    eligibility from ids/labels/roles, then only the chosen rows' row groups. Sorted by id."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    chosen = draw(eligible_ids(parquet_path, min_messages), n, seed)
    groups: dict[int, set[str]] = {}
    for tid, g in chosen:
        groups.setdefault(g, set()).add(tid)
    pf = pq.ParquetFile(parquet_path)
    rows: dict[str, dict] = {}
    for g in sorted(groups):
        ids = pf.read_row_group(g, columns=["trajectory_id"]).column("trajectory_id")
        mask = pc.is_in(ids, value_set=pa.array(sorted(groups[g])))
        for r in pf.read_row_group(g, columns=ROW_COLUMNS).filter(mask).to_pylist():
            rows.setdefault(r["trajectory_id"], r)
    if len(rows) != len(chosen):
        raise RuntimeError(f"pass 2 found {len(rows)} of {len(chosen)} chosen rows")
    return [rows[k] for k in sorted(rows)]


# --- CLI --------------------------------------------------------------------------------------


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def summarize(ts: list[dict]) -> dict:
    n = len(ts)
    nm = [len(t["messages"]) for t in ts]
    pc_ = [len(json.dumps(t["messages"][:t["cut"]])) for t in ts]
    recent = [t for t in ts if t["truth"]["last_error"]
              and t["truth"]["last_error"]["age"] < common.RECENT_ERROR_TOOL_OUTPUTS]
    return {
        "n": n,
        "resolved": dict(Counter(t["truth"]["resolved"] for t in ts)),
        "messages_median_iqr": [statistics.median(nm), _q(nm, 0.25), _q(nm, 0.75)],
        "cut_ratio_median": round(statistics.median(t["cut_turn"] / t["n_turns"] for t in ts), 3),
        "prefix_chars_median": statistics.median(pc_),
        "prefix_chars_p90": _q(pc_, 0.9),
        "prefix_tokens_est_median_p90": [statistics.median(pc_) / 4, _q(pc_, 0.9) / 4],
        "last_test_status": dict(Counter(t["truth"]["last_test"]["status"] for t in ts)),
        "share_recent_last_error": round(len(recent) / n, 3),
        "checklist_len_mean": round(statistics.mean(len(t["checklist"]) for t in ts), 2),
        "share_tests_edited": round(sum(bool(t["truth"]["test_files_modified"]) for t in ts)
                                    / n, 3),
        "share_finished_before_cut": round(sum(t["truth"]["agent_finished_before_cut"]
                                               for t in ts) / n, 3),
        "share_constraint_in_prompt": round(sum(t["source"]["constraint_in_prompt"] for t in ts)
                                            / n, 3),
        "share_no_files_changed": round(sum(not t["truth"]["files_changed"] for t in ts) / n, 3),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--parquet", default="data/P01/raw/trajectories.parquet")
    ap.add_argument("--out", default="data/P01/slice_a.jsonl")
    ap.add_argument("--manifest", default=None, help="default: <out stem>.manifest.json")
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--seed", type=int, default=common.SEED)
    ap.add_argument("--min-messages", type=int, default=40)
    args = ap.parse_args(argv)

    parquet_sha = sha256_file(args.parquet)
    if parquet_sha != PARQUET_SHA256:
        print(f"WARNING: parquet sha256 {parquet_sha} != expected {PARQUET_SHA256}",
              file=sys.stderr)
    rows = sample_rows(args.parquet, args.n, args.seed, args.min_messages)
    ts = sorted((build_transcript(r) for r in rows), key=lambda t: t["tid"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for t in ts:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    manifest = {"dataset": DATASET, "revision": REVISION, "parquet": args.parquet,
                "parquet_sha256": parquet_sha, "seed": args.seed, "n": args.n,
                "min_messages": args.min_messages, "min_turns": 10,
                "tids": [t["tid"] for t in ts], "output": str(out),
                "output_sha256": sha256_file(out)}
    mpath = Path(args.manifest) if args.manifest else out.with_suffix(".manifest.json")
    mpath.write_text(json.dumps(manifest, indent=1) + "\n")
    print(json.dumps(summarize(ts), indent=1))
    print(f"wrote {len(ts)} transcripts to {out}; manifest {mpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
