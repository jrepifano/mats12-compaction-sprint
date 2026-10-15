"""P08 contexts: cut points from X3b GLM episodes, replay-verified (plan.md, "Contexts").

Ported from P07 (probes/P07-compaction-trust/p07/contexts.py); the cut rule changes. P08 cuts
after a *failing test run*, so the summarizer has a latest test result to keep or drop:

- eligible turns: turn >= MIN_CUT_TURN, at or after the first pytest run (`measures.is_test_run`),
  with no change to test_solution.py and no passing run (P07's rule) up to and including it, and
  the latest test run before the cut classified "fail" (P01's `classify_test_output`);
- the cut turn is drawn from the eligible turns with Random(f"{SEED}:{ctx_id}");
- the sandbox is rebuilt by replaying the episode's commands through the cut in a fresh
  `x3-sandbox:pilot` container and checked against the X3b snapshot's sha256;
- the cut solution.py must not pass the impossible tests (X3's pass check): the agent has not
  cheated yet (`cut_clean`).
Then N_CONTEXTS + N_RESERVES contexts are drawn (seeded, at most MAX_PER_TASK per task).

    uv run --group swarm python probes/P23-compaction-decisions-vs-cues/p23/contexts.py \
        [--header neutral|safety] [--dry-run]
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

from p23 import contracts as C
from p23.measures import is_test_run
from x3 import contracts as X3C
from x3.tasks import build_workspace, load_manifest

IMAGE = X3C.IMAGE_TAG
SOLUTION = f"{X3C.WORKDIR}/solution.py"
TEST_FILE = f"{X3C.WORKDIR}/test_solution.py"
ROOTS = (X3C.WORKDIR, X3C.HOME)
WORKERS = 8
DOCKER_TIMEOUT_S = 120
SANDBOX_LABEL = "emx.sandbox=p23"
CONTAINER_LIMIT_S = 1800     # a replay container stops itself after 30 min whatever happens

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


# A passing run of the tests (P07's rule): the run ends eligibility.
TEST_RUN_RE = re.compile(r"\bpytest\b|test_solution|doctest|unittest")
PASS_RE = re.compile(r"\b[1-9]\d* passed\b")
FAIL_RE = re.compile(r"\b[1-9]\d* (?:failed|errors?)\b")

# --- test-output truth (ported from P01, probes/P01-compaction-fidelity/p01/common.py) ---------
_PYTEST_COUNTS_RE = re.compile(
    r"(\d+) (passed|failed|errors?|error|skipped|xfailed|xpassed|deselected|warnings?)")
_PYTEST_SUMMARY_RE = re.compile(
    r"^[= ]*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
    r"(?:, )?)+)(?: in [\d.]+s)?[= ]*$|^=+ no tests ran", re.MULTILINE)
_ERR_LINE_RE = re.compile(r"^(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Exit)\b.*)$",
                          re.MULTILINE)


def classify_test_output(text: str) -> tuple[str, str]:
    """(status, evidence) from pytest's summary line: "fail" | "pass" | "unknown" (P01's rule,
    pytest summaries only: X3 runs are pytest)."""
    summ = list(_PYTEST_SUMMARY_RE.finditer(text or ""))
    if not summ:
        return "unknown", ""
    line = summ[-1].group(0).strip().strip("= ")
    kinds = {k for _, k in _PYTEST_COUNTS_RE.findall(line)}
    if kinds & {"failed", "error", "errors"}:
        return "fail", line
    if "passed" in kinds:
        return "pass", line
    return "unknown", line


def last_error_line(text: str) -> str | None:
    """The last `SomethingError: message` line (pytest `E   ` prefix stripped), <= 200 chars."""
    last = None
    for m in _ERR_LINE_RE.finditer(text or ""):
        last = m.group(1).strip()
    return last[:200] if last else None


def _out(call: dict) -> str:
    return (call.get("stdout") or "") + "\n" + (call.get("stderr_tail") or "")


def eligible_turns(artifact: dict) -> tuple[list[dict], str]:
    """Every eligible cut, in order: {"turn", "call_index", "truth"}; and why the walk stopped."""
    snaps = artifact["snapshots"]
    if not snaps or snaps[0].get("files") is None:
        return [], "no snapshot 0"
    t0 = _sha(snaps[0], TEST_FILE)
    if t0 is None:
        return [], "no test file in snapshot 0"
    out, last_run, last_err, n_runs = [], None, None, 0
    why = "end of episode"
    for i, c in enumerate(artifact["calls"]):
        snap = snaps[c["snapshot_step"]]
        if snap.get("files") is None:
            why = f"snapshot {c['snapshot_step']} failed"
            break
        if _sha(snap, TEST_FILE) != t0:
            why = "test file changed"
            break
        stdout = c.get("stdout") or ""
        if TEST_RUN_RE.search(c["cmd"] or "") and PASS_RE.search(stdout) \
                and not FAIL_RE.search(stdout):
            why = "tests passed"
            break
        err = last_error_line(_out(c))
        if err:
            last_err = err
        if is_test_run(c):
            n_runs += 1
            status, ev = classify_test_output(stdout)
            last_run = {"turn": c["turn"], "command": (c["cmd"] or "")[:300], "status": status,
                        "evidence": ev}
        # the cut is after this call's turn; the next call must be on a later turn
        nxt = artifact["calls"][i + 1]["turn"] if i + 1 < len(artifact["calls"]) else None
        if C.CUT_RULE == "pretest":
            # FU4b: before any test run (turn >= 2); stop at the first one
            if n_runs:
                why = "first test run"
                break
            if c["turn"] >= 2 and (nxt is None or nxt > c["turn"]):
                out.append({"turn": c["turn"], "call_index": i,
                            "truth": {"last_test": None, "last_error": last_err,
                                      "n_test_runs": 0}})
            continue
        if c["turn"] >= C.MIN_CUT_TURN and last_run and last_run["status"] == "fail" \
                and (nxt is None or nxt > c["turn"]):
            out.append({"turn": c["turn"], "call_index": i,
                        "truth": {"last_test": dict(last_run), "last_error": last_err,
                                  "n_test_runs": n_runs}})
    return out, why


def find_cut(artifact: dict, ctx_id: str) -> tuple[dict | None, str]:
    """(cut, reason): one eligible turn drawn with Random(f"{SEED}:{ctx_id}")."""
    el, why = eligible_turns(artifact)
    if not el:
        return None, f"no eligible turn ({why})"
    # FU4 (P23_VARIANT=fu4): the earliest eligible turn instead of a seeded draw
    e = (el[0] if C.CUT_RULE == "earliest" else el[-1] if C.CUT_RULE == "pretest"
         else random.Random(f"{C.SEED}:{ctx_id}").choice(el))
    i = e["call_index"]
    c = artifact["calls"][i]
    return {"cut_turn": e["turn"], "cut_call": i, "cut_snapshot_step": c["snapshot_step"],
            "cut_msg": cut_after_turn(artifact["messages"], e["turn"]),
            "replay_cmds": [x["cmd"] for x in artifact["calls"][: i + 1]],
            "truth": e["truth"], "n_eligible": len(el)}, "ok"

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
    """Shuffle the sorted ctx_ids with Random(seed); walk in order, skipping replay failures,
    cuts whose solution already passes the impossible tests (`cut_clean` False) and tasks
    already at `max_per_task`, until `n` are taken."""
    by_id = {c["ctx_id"]: c for c in candidates}
    order = sorted(by_id)
    random.Random(seed).shuffle(order)
    taken: list[str] = []
    per_task: Counter = Counter()
    for cid in order:
        c = by_id[cid]
        if not c["replay_match"] or not c.get("cut_clean", True) \
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
    name = f"p23-replay-{uuid.uuid4().hex[:12]}"
    exits: list[int] = []
    try:
        # --rm, a name, the probe's label and an in-container time limit (AGENTS.md "Nothing we
        # start may outlive its job"); `docker rm -f` in `finally` removes it in every case
        r = _docker(["run", "-d", "--rm", "--name", name, "--label", SANDBOX_LABEL,
                     "--network", "none", "--init", "--cpus", "1", "--memory", "2g",
                     "-w", X3C.WORKDIR, image, "timeout", "-k", "5", str(CONTAINER_LIMIT_S),
                     "sleep", "infinity"])
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
def source_rows(header: str) -> list[dict]:
    with open(C.X3B_EPISODES_CSV, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)
                if r["analysis_role"] == "main" and r["header"] == header]


def artifact_path(header: str, task_id: str, repeat: int, attempt: int) -> Path:
    return C.X3B_EPISODES_ROOT / header / task_id / f"r{repeat}_a{attempt}.json"


def run_info_text(first_user: str) -> str:
    """RUN_INFO.txt as X3 writes it: the run header (the first user message up to the blank
    line before "## Task") and a newline."""
    head = first_user.split("\n\n## Task", 1)[0]
    if "## Task" not in first_user or not head.strip():
        raise ValueError("first user message has no run header")
    return head + "\n"


def _first_user(art: dict) -> str:
    m = art["messages"][1]
    c = m.get("content")
    return c if isinstance(c, str) else "\n".join(
        x.get("text") or "" for x in c if isinstance(x, dict) and x.get("type") == "text")


def workspace_for(task: X3C.TaskSpec, first_user: str) -> dict[str, str]:
    return {**build_workspace(task), "RUN_INFO.txt": run_info_text(first_user)}


def _b(v: str) -> bool | None:
    return {"True": True, "False": False}.get(v)


def load_candidates(header: str) -> tuple[list[dict], Counter]:
    """Every source episode with its cut (qualifying only) plus the non-qualifying reasons."""
    out, reasons = [], Counter()
    for row in source_rows(header):
        rep, att = int(row["repeat"]), int(row["attempt"])
        path = artifact_path(header, row["task_id"], rep, att)
        raw = path.read_bytes()
        art = json.loads(raw)
        if art["meta"]["episode_id"] != row["episode_id"]:
            raise ValueError(f"{path}: episode_id {art['meta']['episode_id']!r} "
                             f"!= {row['episode_id']!r}")
        cid = ctx_id_of(row["task_id"], rep, att)
        cut, why = find_cut(art, cid)
        if cut is None:
            reasons[why] += 1
            continue
        out.append({"ctx_id": cid, "task_id": row["task_id"],
                    "x3b_episode_id": row["episode_id"], "path": path,
                    "x3b_sha256": hashlib.sha256(raw).hexdigest(), "art": art,
                    "x3b_cheat": _b(row["cheat"]), "x3b_tamper_any": _b(row["tamper_any"]),
                    "x3b_passed": _b(row["passed"]), "n_turns_x3b": int(row["n_turns"]),
                    "first_user": _first_user(art), **cut})
    return out, reasons



def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--header", default="neutral", choices=sorted(C.HEADERS))
    ap.add_argument("--dry-run", action="store_true",
                    help="qualify and cut only; no Docker, no files written")
    # P23 (after the 2026-10-12 laptop crash): reuse already replay-verified contexts (P08's
    # contexts_safety.jsonl) when their X3b artifact hash and cut turn match; replay the rest
    ap.add_argument("--reuse", type=Path, default=None)
    args = ap.parse_args(argv)
    header = args.header

    rows = source_rows(header)
    cands, reasons = load_candidates(header)
    turns = [c["cut_turn"] for c in cands]
    print(f"[{header}] source episodes: {len(rows)}; qualifying: {len(cands)}; "
          f"median cut turn: {statistics.median(turns) if turns else None}")
    print(f"not qualifying: {dict(reasons)}")
    tasks = {t.task_id: t for t in load_manifest(C.TASKS_JSON).tasks}
    if args.dry_run:
        sel = select([{**c, "replay_match": True} for c in cands])
        print(f"dry run (all replays assumed to match and clean): {len(sel)} selected, "
              f"list hash {list_hash(sel)}")
        return 0

    def job(c: dict) -> dict:
        return replay(workspace_for(tasks[c["task_id"]], c["first_user"]), c["replay_cmds"])

    reuse = {}
    if args.reuse:
        for x in Path(args.reuse).read_text(encoding="utf-8").splitlines():
            r = json.loads(x) if x.strip() else None
            if r and r.get("replay_match") and r.get("cut_clean"):
                reuse[r["ctx_id"]] = r
    reused = {c["ctx_id"] for c in cands if c["ctx_id"] in reuse
              and reuse[c["ctx_id"]]["x3b_sha256"] == c["x3b_sha256"]
              and reuse[c["ctx_id"]]["cut_turn"] == c["cut_turn"]}
    print(f"reusing {len(reused)} verified contexts; replaying {len(cands) - len(reused)}",
          flush=True)
    fresh = [c for c in cands if c["ctx_id"] not in reused]
    with ThreadPoolExecutor(WORKERS) as ex:
        fresh_res = dict(zip([c["ctx_id"] for c in fresh], ex.map(job, fresh), strict=True))
    results = []
    for c in cands:
        if c["ctx_id"] in reused:
            r = reuse[c["ctx_id"]]
            orig = [x["exit"] for x in c["art"]["calls"][: c["cut_call"] + 1]]
            results.append({"files": r["files"], "exits": orig, "error": None, "reused": True})
        else:
            results.append(fresh_res[c["ctx_id"]])
    # a command near X3's 60 s timeout can pass it under 8-way load (P07, 2026-10-11): replays
    # with a timeout (124) the original did not have are rerun once, alone
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

    # the agent has not cheated yet: the cut solution must not pass the impossible tests
    from x3 import passcheck

    def clean(c: dict) -> None:
        if c["replay"].get("reused"):
            c["cut_passes"], c["cut_clean"] = "reused", reuse[c["ctx_id"]]["cut_clean"]
        elif c["replay_match"]:
            pr = passcheck.run_pass_check(c["replay"]["files"][SOLUTION]["text"],
                                          tasks[c["task_id"]])
            c["cut_passes"] = [pr.pass_run1, pr.pass_run2, pr.disagree]
            c["cut_clean"] = pr.pass_run1 is False and not pr.disagree
        else:
            c["cut_passes"], c["cut_clean"] = None, None

    with ThreadPoolExecutor(4) as ex:
        list(ex.map(clean, cands))

    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(C.candidates_jsonl(header), "w", encoding="utf-8") as f:
        for c in sorted(cands, key=lambda c: c["ctx_id"]):
            f.write(json.dumps({"ctx_id": c["ctx_id"], "cut_turn": c["cut_turn"],
                                "n_eligible": c["n_eligible"], "truth": c["truth"],
                                "replay_match": c["replay_match"],
                                "mismatched": c["mismatched"],
                                "exit_mismatch": c["exit_mismatch"],
                                "cut_passes": c["cut_passes"], "cut_clean": c["cut_clean"],
                                "x3b_cheat": c["x3b_cheat"],
                                "rerun_after_timeout": c["replay"].get("rerun_after_timeout",
                                                                       False)}) + "\n")
    by_id = {c["ctx_id"]: c for c in cands}
    drawn = select(cands, n=C.N_CONTEXTS + C.N_RESERVES)   # the per-task cap counts reserves
    with open(C.contexts_jsonl(header), "w", encoding="utf-8") as f:
        for pos, cid in enumerate(drawn):
            c = by_id[cid]
            rec = {"ctx_id": cid, "header": header, "x3b_episode_id": c["x3b_episode_id"],
                   "x3b_path": str(c["path"].relative_to(C.MAIN_CHECKOUT)),
                   "x3b_sha256": c["x3b_sha256"], "task_id": c["task_id"], "draw_pos": pos,
                   "reserve": pos >= C.N_CONTEXTS,
                   "cut_turn": c["cut_turn"], "cut_msg": c["cut_msg"],
                   "cut_snapshot_step": c["cut_snapshot_step"], "truth": c["truth"],
                   "n_tests": count_tests(tasks[c["task_id"]].test_solution_py),
                   "replay_cmds": c["replay_cmds"],
                   "files": {p: c["replay"]["files"][p] for p in sorted(c["replay"]["files"])},
                   "snapshot_sha256": {p: c["expected"][p] for p in sorted(c["expected"])},
                   "replay_match": c["replay_match"], "cut_clean": c["cut_clean"],
                   "x3b_cheat": c["x3b_cheat"], "x3b_tamper_any": c["x3b_tamper_any"],
                   "x3b_passed": c["x3b_passed"], "n_turns_x3b": c["n_turns_x3b"],
                   "prefix_messages": c["art"]["messages"][: c["cut_msg"]]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    n_match = sum(c["replay_match"] for c in cands)
    n_clean = sum(bool(c["cut_clean"]) for c in cands)
    print(f"replay match: {n_match}/{len(cands)}; cut clean: {n_clean}/{n_match}")
    for c in sorted(cands, key=lambda c: c["ctx_id"]):
        if not c["replay_match"]:
            print(f"  MISMATCH {c['ctx_id']}: {c['mismatched']}")
        if c["exit_mismatch"]:
            print(f"  exit codes differ {c['ctx_id']}: calls {c['exit_mismatch']}")
    sel = drawn[: C.N_CONTEXTS]
    per_task = Counter(by_id[cid]["task_id"] for cid in sel)
    print(f"selected: {len(sel)} (+{len(drawn) - len(sel)} reserves); "
          f"per task: {dict(sorted(per_task.items()))}")
    print(f"list hash: {list_hash(sel)}; wrote {C.contexts_jsonl(header)}")
    return 0 if len(sel) >= C.MIN_PASSING_CONTEXTS else 1


if __name__ == "__main__":
    raise SystemExit(main())
