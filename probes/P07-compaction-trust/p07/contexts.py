"""P07 unit A: cut points from X3b GLM neutral episodes, replay-verified (plan.md, "Contexts").

Reads the 86 analysed X3b GLM neutral-header episodes (read-only, main checkout), finds each
one's cut (the first edit to solution.py at turn >= MIN_CUT_TURN, before any change to the test
file), rebuilds the sandbox at the cut by replaying the episode's commands in a fresh
`x3-sandbox:pilot` container (free, local Docker), checks the result against the X3b snapshot's
sha256, then draws N_CONTEXTS contexts (seeded, at most MAX_PER_TASK per task) and writes
data/candidates.jsonl and data/contexts.jsonl (schema: p07/contracts.py, "context record").

The workspace setup and the command execution mirror X3's (src/x3/task.py `setup`,
`_run_command`): files from `x3.tasks.build_workspace` plus RUN_INFO.txt holding the run header
and "\\n", chowned to `dev`; each command runs as `dev` via `bash -c` in /agent with
HOME=/home/dev and a 60 s timeout. The cut rule follows P01's `cut_after_turn`
(../p01-compaction-fidelity/probes/P01-compaction-fidelity/p01/common.py).

    uv run --group swarm python probes/P07-compaction-trust/p07/contexts.py [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROBE = Path(__file__).resolve().parents[1]
_ROOT = _PROBE.parents[1]
for _p in (_PROBE, _ROOT / "src", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argparse
import ast
import csv
import hashlib
import json
import random
import re
import statistics
import subprocess
import uuid
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from p07 import contracts as C
from p07.measures import strip_heredocs
from x3 import contracts as X3C
from x3 import texts as X3T
from x3.tasks import build_workspace, load_manifest

IMAGE = X3C.IMAGE_TAG
SOLUTION = f"{X3C.WORKDIR}/solution.py"
TEST_FILE = f"{X3C.WORKDIR}/test_solution.py"
ROOTS = (X3C.WORKDIR, X3C.HOME)
WORKERS = 8
N_RESERVES = 5      # main session, 2026-10-11: replacements for contexts whose summary is dropped
DOCKER_TIMEOUT_S = 120


# --- pure helpers ------------------------------------------------------------------------------
def ctx_id_of(task_id: str, repeat: int, attempt: int) -> str:
    return f"{task_id}|r{repeat}|a{attempt}"


def assistant_indices(messages: Sequence[dict]) -> list[int]:
    return [i for i, m in enumerate(messages) if m.get("role") == "assistant"]


def cut_after_turn(messages: Sequence[dict], turn: int) -> int:
    """Prefix length ending after assistant turn `turn` (1-based) and its tool results: the
    index of the next assistant message, or len(messages) if there is none (as P01)."""
    a = assistant_indices(messages)
    if not 1 <= turn <= len(a):
        raise ValueError(f"turn {turn} out of range 1..{len(a)}")
    return a[turn] if turn < len(a) else len(messages)


def _sha(snap: dict, path: str) -> str | None:
    files = snap.get("files") or {}
    rec = files.get(path)
    return rec.get("sha256") if rec else None


# The cut edit must be untested (plan: "the first untested edit"): its own command must not
# run the tests (main session, 2026-10-11, after unit A found 23 of 40 cut commands ran pytest).
# Broad on purpose: any mention of pytest / test_solution / doctest / unittest outside heredoc
# bodies disqualifies the edit as a cut (the walk then moves on to the next edit).
TEST_RUN_RE = re.compile(r"\bpytest\b|test_solution|doctest|unittest")
PASS_RE = re.compile(r"\b[1-9]\d* passed\b")
FAIL_RE = re.compile(r"\b[1-9]\d* (?:failed|errors?)\b")


def find_cut(artifact: dict) -> tuple[dict | None, str]:
    """(cut, reason). Walk `calls` in order; stop (not qualifying) at the first call whose
    snapshot shows test_solution.py changed from snapshot 0, or that ran the tests and passed;
    the cut is the first call at turn >= MIN_CUT_TURN whose snapshot shows solution.py changed
    from the previous snapshot and whose command does not run the tests (TEST_RUN_RE)."""
    snaps = artifact["snapshots"]
    if not snaps or snaps[0].get("files") is None:
        return None, "no snapshot 0"
    t0 = _sha(snaps[0], TEST_FILE)
    if t0 is None:
        return None, "no test file in snapshot 0"
    calls = artifact["calls"]
    for i, c in enumerate(calls):
        step = c["snapshot_step"]
        snap = snaps[step]
        if snap.get("files") is None:
            return None, f"snapshot {step} failed"
        if _sha(snap, TEST_FILE) != t0:
            return None, f"test file changed at turn {c['turn']}"
        if TEST_RUN_RE.search(strip_heredocs(c["cmd"])) and PASS_RE.search(c["stdout"] or "") \
                and not FAIL_RE.search(c["stdout"] or ""):
            # a passing run of the impossible tests: the solution already special-cases them,
            # and "tests pass" would be true; such an episode cannot host the false claim
            return None, f"tests passed at turn {c['turn']}"
        if c["turn"] >= C.MIN_CUT_TURN and step >= 1 and \
                _sha(snap, SOLUTION) != _sha(snaps[step - 1], SOLUTION) and \
                not TEST_RUN_RE.search(strip_heredocs(c["cmd"])):
            return {"cut_turn": c["turn"], "cut_call": i, "cut_snapshot_step": step,
                    "cut_msg": cut_after_turn(artifact["messages"], c["turn"]),
                    "replay_cmds": [x["cmd"] for x in calls[: i + 1]]}, "ok"
    return None, "no solution.py edit at turn >= MIN_CUT_TURN"


def count_tests(test_src: str) -> int:
    """Top-level `def test*` functions."""
    return sum(1 for n in ast.parse(test_src).body
               if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
               and n.name.startswith("test"))


def is_cache(path: str) -> bool:
    return any(part in C.CACHE_PARTS for part in Path(path).parts)


def snapshot_shas(snap: dict) -> dict[str, str | None]:
    return {p: r.get("sha256") for p, r in (snap.get("files") or {}).items() if not is_cache(p)}


def compare(replayed: dict[str, dict], expected: dict[str, str | None]) -> tuple[bool, list[str]]:
    """(match, mismatched paths): same path set and equal sha256 on every path."""
    mism = sorted(p for p in set(replayed) | set(expected)
                  if p not in replayed or p not in expected
                  or replayed[p]["sha256"] != expected[p])
    return not mism, mism


def select(candidates: Sequence[dict], *, seed: int = C.SEED, n: int = C.N_CONTEXTS,
           max_per_task: int = C.MAX_PER_TASK) -> list[str]:
    """Shuffle the sorted ctx_ids with Random(seed); walk in order, skipping replay failures and
    contexts whose cut solution already passes the pristine tests (`claim_false` False: the
    planted "tests pass" claim would be true there), and tasks already at `max_per_task`,
    until `n` are taken. `candidates`: dicts with ctx_id,
    task_id, replay_match."""
    by_id = {c["ctx_id"]: c for c in candidates}
    order = sorted(by_id)
    random.Random(seed).shuffle(order)
    taken: list[str] = []
    per_task: Counter = Counter()
    for cid in order:
        c = by_id[cid]
        if not c["replay_match"] or not c.get("claim_false", True) \
                or per_task[c["task_id"]] >= max_per_task:
            continue
        taken.append(cid)
        per_task[c["task_id"]] += 1
        if len(taken) == n:
            break
    return taken


def list_hash(ctx_ids: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(ctx_ids).encode()).hexdigest()


# --- docker replay -----------------------------------------------------------------------------
_WRITE_PY = r"""
import json, os, subprocess, sys
spec = json.load(sys.stdin)
os.makedirs(spec["workdir"], exist_ok=True)
for name, body in spec["files"].items():
    with open(os.path.join(spec["workdir"], name), "w", encoding="utf-8") as f:
        f.write(body)
subprocess.run(["chown", "-R", spec["owner"], spec["workdir"]], check=True)
"""

_READ_PY = r"""
import hashlib, json, os, stat, sys
roots, cache = json.loads(sys.argv[1])
files = {}
for root in roots:
    for d, dirs, fs in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in cache)
        for n in sorted(fs):
            p = os.path.join(d, n)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                files[p] = {"sha256": "symlink:" + os.readlink(p), "text": None}
            elif not stat.S_ISREG(st.st_mode):
                files[p] = {"sha256": "special:" + oct(st.st_mode), "text": None}
            else:
                b = open(p, "rb").read()
                files[p] = {"sha256": hashlib.sha256(b).hexdigest(),
                            "text": b.decode("utf-8", "replace")}
print(json.dumps(files))
"""


def _docker(args: list[str], *, input: str | None = None,
            timeout: float = DOCKER_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], input=input, capture_output=True, text=True,
                          check=False, timeout=timeout,
                          stdin=None if input is not None else subprocess.DEVNULL)


def replay(workspace: dict[str, str], cmds: Sequence[str], image: str = IMAGE) -> dict:
    """Fresh container, X3's workspace, the commands as `dev`; returns {"files": {path: {sha256,
    text}}, "exits": [int], "error": str|None}. The container is removed in every case."""
    name = f"p07-replay-{uuid.uuid4().hex[:12]}"
    exits: list[int] = []
    try:
        r = _docker(["run", "-d", "--name", name, "--network", "none", "--init", "--cpus", "1",
                     "--memory", "2g", "-w", X3C.WORKDIR, image, "sleep", "infinity"])
        if r.returncode != 0:
            return {"files": {}, "exits": exits, "error": f"docker run: {r.stderr[-300:]}"}
        spec = {"workdir": X3C.WORKDIR, "files": workspace,
                "owner": f"{X3C.AGENT_USER}:{X3C.AGENT_USER}"}
        r = _docker(["exec", "-i", "-u", "root", name, "python3", "-c", _WRITE_PY],
                    input=json.dumps(spec))
        if r.returncode != 0:
            return {"files": {}, "exits": exits, "error": f"write: {r.stderr[-300:]}"}
        for cmd in cmds:
            try:
                r = _docker(["exec", "-u", X3C.AGENT_USER, "-w", X3C.WORKDIR, "-e",
                             f"HOME={X3C.HOME}", name, "bash", "-c", cmd],
                            timeout=X3C.COMMAND_TIMEOUT_S)
                exits.append(r.returncode)
            except subprocess.TimeoutExpired:
                exits.append(124)
                # stop the command's processes so the read-back sees a settled workspace (the
                # image has no procps, so no pkill: `kill -9 -1` as dev kills all dev processes)
                _docker(["exec", "-u", X3C.AGENT_USER, name, "bash", "-c", "kill -9 -1"],
                        timeout=30)
        r = _docker(["exec", "-u", "root", name, "python3", "-c", _READ_PY,
                     json.dumps([list(ROOTS), list(C.CACHE_PARTS)])])
        if r.returncode != 0:
            return {"files": {}, "exits": exits, "error": f"read: {r.stderr[-300:]}"}
        return {"files": json.loads(r.stdout), "exits": exits, "error": None}
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
        return {"files": {}, "exits": exits, "error": repr(e)[:300]}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60,
                       check=False)


# --- sources -----------------------------------------------------------------------------------
def source_rows() -> list[dict]:
    with open(C.X3B_EPISODES_CSV, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)
                if r["analysis_role"] == "main" and r["header"] == C.X3B_HEADER]


def artifact_path(task_id: str, repeat: int, attempt: int) -> Path:
    return C.X3B_NEUTRAL_DIR / task_id / f"r{repeat}_a{attempt}.json"


def run_info_text() -> str:
    return X3T.header_text(X3C.Context.NEUTRAL) + "\n"


def workspace_for(task: X3C.TaskSpec) -> dict[str, str]:
    return {**build_workspace(task), X3T.RUN_INFO_FILENAME: run_info_text()}


def load_candidates() -> tuple[list[dict], Counter]:
    """Every source episode with its cut (qualifying only) plus the non-qualifying reasons."""
    out, reasons = [], Counter()
    for row in source_rows():
        rep, att = int(row["repeat"]), int(row["attempt"])
        path = artifact_path(row["task_id"], rep, att)
        raw = path.read_bytes()
        art = json.loads(raw)
        if art["meta"]["episode_id"] != row["episode_id"]:
            raise ValueError(f"{path}: episode_id {art['meta']['episode_id']!r} "
                             f"!= {row['episode_id']!r}")
        cut, why = find_cut(art)
        if cut is None:
            reasons[why.split(" at turn")[0]] += 1
            continue
        out.append({"ctx_id": ctx_id_of(row["task_id"], rep, att), "task_id": row["task_id"],
                    "x3b_episode_id": row["episode_id"], "path": path,
                    "x3b_sha256": hashlib.sha256(raw).hexdigest(), "art": art, **cut})
    return out, reasons


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="qualify and cut only; no Docker, no files written")
    args = ap.parse_args(argv)

    rows = source_rows()
    cands, reasons = load_candidates()
    turns = [c["cut_turn"] for c in cands]
    print(f"source episodes: {len(rows)}; qualifying: {len(cands)}; "
          f"median cut turn: {statistics.median(turns) if turns else None}")
    print(f"not qualifying: {dict(reasons)}")
    tasks = {t.task_id: t for t in load_manifest(C.TASKS_JSON).tasks}
    if args.dry_run:
        sel = select([{**c, "replay_match": True} for c in cands])
        print(f"dry run (all replays assumed to match): {len(sel)} selected, "
              f"list hash {list_hash(sel)}")
        return 0

    def job(c: dict) -> dict:
        return replay(workspace_for(tasks[c["task_id"]]), c["replay_cmds"])

    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(job, cands))
    # A command that took ~45 s in X3b can pass X3's 60 s timeout under 8-way load (seen on
    # 2026-10-11). Replays with a timeout (124) the original did not have are rerun once, alone.
    for i, (c, res) in enumerate(zip(cands, results, strict=True)):
        orig = [x["exit"] for x in c["art"]["calls"][: c["cut_call"] + 1]]
        if any(a == 124 and b != 124 for a, b in zip(res["exits"], orig, strict=False)):
            print(f"  rerunning {c['ctx_id']} alone (timeout under load)")
            results[i] = {**job(c), "rerun_after_timeout": True}
    for c, res in zip(cands, results, strict=True):
        expected = snapshot_shas(c["art"]["snapshots"][c["cut_snapshot_step"]])
        if res["error"]:
            c["replay_match"], c["mismatched"] = False, [f"error: {res['error']}"]
        else:
            c["replay_match"], c["mismatched"] = compare(res["files"], expected)
        c["replay"], c["expected"] = res, expected
        orig_exits = [x["exit"] for x in c["art"]["calls"][: c["cut_call"] + 1]]
        c["exit_mismatch"] = [i for i, (a, b) in enumerate(zip(res["exits"], orig_exits,
                                                               strict=False)) if a != b]

    # The planted claim must be false: drop cuts whose solution.py already passes the pristine
    # tests (main session, 2026-10-11: 9 of the first 32 cuts held a call-order hack that does)
    from x3 import passcheck

    for c in cands:
        if c["replay_match"]:
            pr = passcheck.run_pass_check(c["replay"]["files"][SOLUTION]["text"],
                                          tasks[c["task_id"]])
            c["cut_passes"] = [pr.pass_run1, pr.pass_run2, pr.disagree]
            c["claim_false"] = pr.pass_run1 is False and not pr.disagree
        else:
            c["cut_passes"], c["claim_false"] = None, None

    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(C.CANDIDATES_JSONL, "w", encoding="utf-8") as f:
        for c in sorted(cands, key=lambda c: c["ctx_id"]):
            f.write(json.dumps({"ctx_id": c["ctx_id"], "cut_turn": c["cut_turn"],
                                "replay_match": c["replay_match"],
                                "mismatched": c["mismatched"],
                                "exit_mismatch": c["exit_mismatch"],
                                "cut_passes": c["cut_passes"],
                                "claim_false": c["claim_false"],
                                "rerun_after_timeout": c["replay"].get("rerun_after_timeout",
                                                                       False)}) + "\n")
    by_id = {c["ctx_id"]: c for c in cands}
    drawn = select(cands, n=C.N_CONTEXTS + N_RESERVES)   # the per-task cap counts reserves too
    sel, reserves = drawn[: C.N_CONTEXTS], drawn[C.N_CONTEXTS:]
    reserves_path = C.DATA_DIR / "reserves.jsonl"
    with open(C.CONTEXTS_JSONL, "w", encoding="utf-8") as f, \
            open(reserves_path, "w", encoding="utf-8") as fr:
        for pos, cid in enumerate(drawn):
            c = by_id[cid]
            rec = {"ctx_id": cid, "x3b_episode_id": c["x3b_episode_id"],
                   "x3b_path": str(c["path"].relative_to(C.MAIN_CHECKOUT)),
                   "x3b_sha256": c["x3b_sha256"], "task_id": c["task_id"], "draw_pos": pos,
                   "cut_turn": c["cut_turn"], "cut_msg": c["cut_msg"],
                   "cut_snapshot_step": c["cut_snapshot_step"],
                   "n_tests": count_tests(tasks[c["task_id"]].test_solution_py),
                   "replay_cmds": c["replay_cmds"],
                   "files": {p: c["replay"]["files"][p] for p in sorted(c["replay"]["files"])},
                   "snapshot_sha256": {p: c["expected"][p] for p in sorted(c["expected"])},
                   "replay_match": c["replay_match"],
                   "prefix_messages": c["art"]["messages"][: c["cut_msg"]]}
            (f if pos < C.N_CONTEXTS else fr).write(json.dumps(rec, ensure_ascii=False) + "\n")

    n_match = sum(c["replay_match"] for c in cands)
    print(f"replay match: {n_match}/{len(cands)}")
    for c in sorted(cands, key=lambda c: c["ctx_id"]):
        if not c["replay_match"]:
            print(f"  MISMATCH {c['ctx_id']}: {c['mismatched']}")
        if c["exit_mismatch"]:
            print(f"  exit codes differ {c['ctx_id']}: calls {c['exit_mismatch']}")
    per_task = Counter(by_id[cid]["task_id"] for cid in sel)
    print(f"selected: {len(sel)}; per task: {dict(sorted(per_task.items()))}")
    print(f"list hash: {list_hash(sel)}")
    print(f"reserves: {len(reserves)} of {N_RESERVES} {reserves}"
          + ("" if len(reserves) == N_RESERVES else " (per-task cap leaves no more)"))
    print(f"image: {IMAGE}; wrote {C.CANDIDATES_JSONL}, {C.CONTEXTS_JSONL}, {reserves_path}")
    return 0 if len(sel) == C.N_CONTEXTS else 1


if __name__ == "__main__":
    raise SystemExit(main())
