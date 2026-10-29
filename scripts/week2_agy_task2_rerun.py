"""Week-2 agy harness replication: task 2 rerun at 800 calls and 4 h ($0, Jacob's plan quota).

    uv run python scripts/week2_agy_task2_rerun.py check
    uv run --with fastmcp==4.1.3 python scripts/week2_agy_task2_rerun.py smoke --go-from-jacob \
        --history <h> [--into-bulk] --memory-mb MB [--host local]
    uv run --with fastmcp==4.1.3 python scripts/week2_agy_task2_rerun.py bulk --go-from-jacob \
        --max-live N --memory-mb MB [--host local]
    uv run python scripts/week2_agy_task2_rerun.py progress

Last night's agy replication (`scripts/week2_harness_agy.py`, 12 divergent "np" + 12 clean
histories) capped 10 of 24 second tasks at experiment 1's 400 model calls, all 4 task-1
overwriters among them, so carry-over couldn't be read. This reruns **task 2 only** of each of
the 24 histories (the last attempt, as the readout uses) with the call cap at 800 and the time
cap at 4 h (`agy_chain.HistoryConfig.max_calls`, `max_seconds`). Everything else is the
replication's: task 2's container and setup (`agy_chain._task`), its prompt
(`cc_chain.lead_and_task`, via `agy_fix.task2_prompt`), turn rules, screening and evidence.

The original post-compaction session wasn't saved (only its manifest), so the boundary is
redone: experiment 5's **native** arm (`agy_fix`, astra agyfix r1-r2), a fresh `/compact` in a
holding container (task 1's image, /app emptied, no remote) resumed from a fork of the verified
pre-compaction export (`a/pre_compact/`: the session DB and the scorer state), then task 2
resumes the forked conversation (`agy_fix.run_task2`). The compacted session is saved this time
(`a_compact/session/`, by `agy_fix.hold_run`).

Layout, per history: `data/raw/sprint/s2/harness/agy_rerun800/<history>/attempt<N>/`
- `a_compact/`: the holding phase (`hold.json`, its transcript, turn files, `session/` with
  the compacted DB and its manifest);
- `b/`: task 2's run dir in experiment 1's format (result.json, messages.json, traps_home), so
  `week2_harness_readout.rows_for` reads it unchanged (its `last_attempt` takes the last
  `attempt*/b/result.json`);
- `rerun.json`: the attempt's record (source attempt, caps, hold summary, task 2's ending).
`<root>/assignment.json` keeps last night's seeded order (and so its randomization blocks);
`<root>/provenance.json` lists every launch (caps, code hashes, git HEAD and dirty files).

- `check`: every history's pre-compaction export (`agy_fix.verify_pre_compact`) and its source
  (`agy_fix.load_source`), with no docker. `smoke` and `bulk` refuse unless it passes.
- `smoke`: one history, then its checks (the fresh checkpoint, the saved session's bytes and
  sha256, the caps task 2's TaskRun got and the calls used, the readout's row path on the
  smoke's own root). By default in `agy_rerun800_smoke/`; `--into-bulk` writes it where the
  bulk would (a done history is skipped by the bulk). Either root gets `assignment.json`.
- `bulk`: all 24 in last night's order, at most `--max-live` at once, each launch waiting on
  the harness replication's laptop gate (`week2_harness_agy.admit`) and holding the shared slot
  ledger (`held_slots`, prefix "native-agy-chain", 2 slots per history). A history whose last
  attempt has task 2's result is done, whatever its ending; a failed holding phase or a crash
  is retried once in a fresh attempt directory.
- `progress`: one line per history and the totals.

Quota (intent to treat), as in the harness bulk: the first refusal (in the holding phase's
`/compact` or in task 2) sets the shared stop flag; nothing further is launched. A holding
phase's quota cut persisted before a crash is settled on restart (`settle`: task 2's result
written as "quota_refused", no attempt 2), and any persisted cut sets the stop again at
launch unless `--resume-after-quota` (Jacob's call). A quota-cut
history keeps its attempt, task 2's result says "quota_refused" with
`extra.primary_unknown_reason`, and it is never rerun or replaced.

Ledger: one $0 row per task 2 (`emx.spend.record_once`, kind "judge", resource "plan-quota",
the ledger's exp field `SPRINT-S2-AGY-RERUN800` as the chain runs book theirs), units = calls.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import week2_harness_agy as HA  # the slot ledger, the laptop gate, history states

from compaction.s2 import interact as I
from compaction.s2.chain import config as CC
from compaction.s2.native import agy_chain as X
from compaction.s2.native import agy_fix as AF

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = HA.BULK_DIR  # data/raw/sprint/s2/harness/agy
BULK_DIR = HA.RAW / "agy_rerun800"
SMOKE_DIR = HA.RAW / "agy_rerun800_smoke"
MAX_CALLS = 800
MAX_SECONDS = 4 * 3600
STAGE = "SPRINT-S2-AGY-RERUN800"
LEDGER: Path | None = None  # None: this checkout's ledger (tests pass their own)
ATTEMPTS = 2
OWNED = ("scripts/week2_agy_task2_rerun.py", "src/compaction/s2/native/agy_chain.py",
         "src/compaction/s2/native/agy_fix.py", "src/compaction/s2/native/agy.py",
         "src/compaction/s2/native/cc_chain.py", "src/compaction/s2/native/common.py",
         "src/compaction/s2/native/agy_transcript.py", "scripts/week2_harness_agy.py")
# a 4 h task plus its setup and evidence must end inside the containers' lifetime
assert MAX_SECONDS + 3600 <= I.CONTAINER_LIFETIME_S, "raise the container lifetime first"


def use_cap(max_calls: int) -> None:
    """Task 2's call cap for this process (Jacob, 29 Oct: raise it past 800 for the Flash
    reruns). A cap other than 800 gets its own roots and stage (`agy_rerun<N>`), so runs under
    different caps never share a root; call it before anything reads them."""
    global MAX_CALLS, BULK_DIR, SMOKE_DIR, STAGE
    MAX_CALLS = max_calls
    BULK_DIR = HA.RAW / f"agy_rerun{max_calls}"
    SMOKE_DIR = HA.RAW / f"agy_rerun{max_calls}_smoke"
    STAGE = f"SPRINT-S2-AGY-RERUN{max_calls}"


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------------------------------
# Sources
# ------------------------------------------------------------------------------------------


def order(src_dir: Path | None = None) -> list[dict]:
    """Last night's seeded order (`week2_harness_cc.assignment`, as assignment.json kept it)."""
    return json.loads((Path(src_dir or SRC_DIR) / "assignment.json").read_text())["order"]


def source_attempt(hdir: Path) -> Path | None:
    """The attempt the readout reads: the last one with task 2's result."""
    tries = sorted(Path(hdir).glob("attempt*/b/result.json"),
                   key=lambda p: int(p.parts[-3].removeprefix("attempt") or 0))
    return tries[-1].parent.parent if tries else None


def config(o: dict, out: Path, tasks: dict, *, host: str | None, quota: threading.Event,
           memory_mb: int = CC.AGENT_MEMORY_MB, cpus: float = CC.AGENT_CPUS,
           max_calls: int | None = None, max_seconds: int | None = None) -> X.HistoryConfig:
    """The history's config, as the harness bulk builds it, with the raised task caps (the
    process's `MAX_CALLS` and `MAX_SECONDS` unless given)."""
    hist = CC.History(o["first_task"], "np", o["rep"], o["arm"])
    a_repo = CC.REPOS[hist.repo]
    b_repo = CC.other_repo(hist.repo)
    return X.HistoryConfig(hist=hist, a_task=tasks[a_repo.a_task], b_task=tasks[b_repo.b_task],
                           out=out, host=host, memory_mb=memory_mb, cpus=cpus,
                           quota_stop=quota, max_calls=max_calls or MAX_CALLS,
                           max_seconds=max_seconds or MAX_SECONDS)


def check(src_dir: Path | None = None, tasks: dict | None = None) -> dict:
    """Every history's source attempt, its export verified and its Source loadable; no
    docker. {"ok", "rows": [...]}."""
    if tasks is None:
        from compaction.s2.chain import driver as D
        tasks = D.tasks()
    src_dir = Path(src_dir or SRC_DIR)
    rows = []
    for o in order(src_dir):
        row: dict = {"history": o["history"], "arm": o["arm"]}
        att = source_attempt(src_dir / o["history"])
        if att is None:
            rows.append(row | {"ok": False, "why": "no source attempt"})
            continue
        v = AF.verify_pre_compact(att / "a")
        row |= {"attempt": att.name, "pre_compact_ok": v["ok"], "why": v["why"],
                "cid": v.get("cid"), "last_step_index": v.get("last_step_index")}
        try:
            ra = json.loads((att / "a" / "result.json").read_text())
            row["kept_image"] = ((ra.get("extra") or {}).get("pre_compact") or {}).get("tag")
            if v["ok"]:
                src = AF.load_source(config(o, att, tasks, host=None,
                                            quota=threading.Event()), att / "a")
                row |= {"a_steps": len(src.a_steps), "user_turns": len(src.turns),
                        "db_bytes": sum(len(b) for b in src.blobs.values()),
                        "holding_image": src.image, "task2_image": src.cfg.b_task.image}
        except Exception as e:  # noqa: BLE001 - reported, the check fails
            row |= {"pre_compact_ok": False, "why": f"source: {type(e).__name__}: {e}"[:300]}
        row["ok"] = bool(row.get("pre_compact_ok"))
        rows.append(row)
    return {"ok": len(rows) == 24 and all(r["ok"] for r in rows), "n": len(rows),
            "n_divergent": sum(r["arm"] == "divergent" for r in rows), "rows": rows}


# ------------------------------------------------------------------------------------------
# One attempt
# ------------------------------------------------------------------------------------------


class HoldFailed(RuntimeError):
    pass


def book(b_dir: Path, hid: str, ledger: Path | None = None) -> bool:
    """One $0 ledger row per task 2 (plan quota), units its model calls. Returns whether it
    appended (a second call for the same run dir doesn't)."""
    from emx import spend
    res = json.loads((Path(b_dir) / "result.json").read_text())
    _, new = spend.record_once(
        f"rerun{MAX_CALLS}:{Path(b_dir).resolve()}", "judge", "plan-quota", res.get("n_calls") or 0,
        0.0, note=f"agy task-2 rerun ({MAX_CALLS} calls, 4 h) {hid} ({res.get('end')}): plan quota, "
        "no charge", exp=STAGE, ledger=ledger if ledger is not None else LEDGER)
    return new


def run_attempt(src: AF.Source, att: Path, *, hold=None, task2=None,
                ensure_volume=None) -> dict:
    """One attempt: the fresh `/compact` from the pre-compaction export (`agy_fix.hold_run`,
    native: no request), then task 2 on the compacted fork (`agy_fix.run_task2`, the caps from
    `src.cfg`). A quota cut writes task 2's result as "quota_refused" (primary unknown) and
    the attempt is final; a failed holding phase raises HoldFailed (no task 2 result: the
    attempt is incomplete). Writes rerun.json; returns its record."""
    hold, task2 = hold or AF.hold_run, task2 or AF.run_task2
    ensure_volume = ensure_volume or X.A.ensure_cli_volume
    att = Path(att)
    att.mkdir(parents=True, exist_ok=True)
    b = att / "b"
    rec: dict = {"history": src.cfg.hist.id, "source": str(src.a_dir.parent), "utc": utc(),
                 "caps": {"max_calls": src.cfg.max_calls, "max_seconds": src.cfg.max_seconds},
                 "source_cid": src.man["conversation_id"],
                 "source_last_step_index": src.man["last_step_index"]}
    try:
        if src.cfg.quota_stop.is_set():
            why = "quota stop before the holding phase"
            b.mkdir(parents=True, exist_ok=True)
            X.skipped_run(b, end=X.END_QUOTA, why=why, second=True, primary_unknown=why)
            rec |= {"b_end": X.END_QUOTA, "quota": why}
            return rec
        ensure_volume(src.cfg.host, src.image)  # as `run_history` does before task 1
        h = hold(src, att / "a_compact", None)
        rec["hold"] = {k: h.get(k) for k in ("ok", "cid", "error", "quota", "why", "evidence",
                                             "deviations", "stage")}
        rec["hold"]["boundary_ok"] = (h.get("boundary") or {}).get("ok")
        if h.get("quota"):
            why = f"quota in the holding phase: {h.get('why') or 'quota'}"
            b.mkdir(parents=True, exist_ok=True)
            X.skipped_run(b, end=X.END_QUOTA, why=why, second=True, primary_unknown=why)
            rec |= {"b_end": X.END_QUOTA, "quota": why}
            return rec
        if not h.get("ok"):
            raise HoldFailed(f"holding phase failed: {h.get('error') or h.get('why')}")
        res = task2(src, b, h["session_blobs"], h["manifest"], h.get("steps") or [],
                    AF.task2_prompt(src))
        rec["b_end"] = (res or {}).get("end")
        return rec
    finally:
        (att / "rerun.json").write_text(json.dumps(rec, indent=1, default=str))


# ------------------------------------------------------------------------------------------
# States, provenance
# ------------------------------------------------------------------------------------------


def _result(p: Path) -> dict | None:
    return HA._result(p)


def history_state(hdir: Path, not_launched: str | None = None) -> dict:
    """"done" (the last attempt with task 2's result, whatever its ending: a quota cut
    included, kept and never rerun), "unknown" (attempts incomplete, with each one's error),
    or "not_launched" (with the reason)."""
    atts = sorted(Path(hdir).glob("attempt*"),
                  key=lambda p: int(p.name.removeprefix("attempt") or 0)) \
        if Path(hdir).exists() else []
    notes = HA._notes(Path(hdir)) if Path(hdir).exists() else []
    for a in reversed(atts):
        rb = _result(a / "b" / "result.json")
        if rb:
            return {"state": "done", "attempt": a.name, "b_end": rb["end"],
                    "n_calls": rb.get("n_calls"), "quota_cut": X.quota_marks(rb),
                    "primary_unknown_reason": (rb.get("extra") or {}).get(
                        "primary_unknown_reason")}
    if atts:
        errors = []
        for a in atts:
            p = a / HA.ATTEMPT_ERROR
            errors.append({"attempt": a.name, **(json.loads(p.read_text()) if p.exists()
                                                 else {"error": "no task 2 result"})})
        return {"state": "unknown", "attempts": len(atts), "reason": "attempts incomplete",
                "errors": errors, "notes": notes}
    return {"state": "not_launched", "reason": not_launched or "not launched", "notes": notes}


def _done(hdir: Path) -> bool:
    return history_state(hdir)["state"] == "done"


def _attempts(hdir: Path) -> list[Path]:
    return sorted(Path(hdir).glob("attempt*"),
                  key=lambda p: int(p.name.removeprefix("attempt") or 0)) \
        if Path(hdir).exists() else []


def settle(att: Path) -> str | None:
    """The attempt's persisted quota mark, if any. A holding phase that recorded a quota cut
    (`a_compact/hold.json`) whose task 2 result is missing (the process stopped in between,
    astra rerun800 r1 B1) gets task 2's result now: "quota_refused", the primary unknown.
    The attempt is then final: the history is kept, never rerun or replaced."""
    att = Path(att)
    rb = _result(att / "b" / "result.json")
    if rb:
        return X.quota_marks(rb)
    try:
        h = json.loads((att / "a_compact" / "hold.json").read_text())
    except (OSError, ValueError):
        return None
    if not h.get("quota"):
        return None
    why = (f"quota in the holding phase: {h.get('why') or 'quota'} (task 2's result written "
           "on restart)")
    (att / "b").mkdir(parents=True, exist_ok=True)
    X.skipped_run(att / "b", end=X.END_QUOTA, why=why, second=True, primary_unknown=why)
    return why


def reconcile(root: Path, histories: list[str]) -> dict[str, str]:
    """Every attempt settled (`settle`); the histories whose attempts record a quota cut, with
    the mark. Any mark restores the shared stop flag at launch (intent to treat)."""
    marks = {}
    for h in histories:
        for att in _attempts(Path(root) / h):
            if m := settle(att):
                marks[h] = m
    return marks


def _git(*args: str) -> str:
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return p.stdout.strip()


def provenance(root: Path, kind: str, settings: dict) -> dict:
    """Appends this launch to `<root>/provenance.json`: the caps, the code's sha256, git HEAD
    and which of those files are dirty."""
    code = {}
    for rel in OWNED:
        p = ROOT / rel
        code[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    entry = {"utc": utc(), "kind": kind, "caps": {"max_calls": MAX_CALLS,
                                                    "max_seconds": MAX_SECONDS},
             "defaults_unchanged": {"max_calls": CC.MAX_CALLS, "max_seconds": CC.MAX_SECONDS},
             "settings": settings, "git_head": _git("rev-parse", "HEAD"),
             "git_dirty": [x for x in _git("status", "--porcelain", "--", *OWNED).splitlines()
                           if x.strip()],
             "code_sha256": code, "source_dir": str(SRC_DIR), "model": X.MODEL,
             "cli_version": X.CLI_VERSION}
    p = Path(root) / "provenance.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = json.loads(p.read_text()) if p.exists() else {"launches": []}
    doc["launches"].append(entry)
    p.write_text(json.dumps(doc, indent=1))
    return entry


def preflight(root: Path, tasks: dict) -> dict:
    chk = check(SRC_DIR, tasks)
    if not chk["ok"]:
        bad = [(r["history"], r.get("why")) for r in chk["rows"] if not r["ok"]]
        sys.exit(f"refused: the source check failed: {bad}")
    root.mkdir(parents=True, exist_ok=True)
    return chk


# ------------------------------------------------------------------------------------------
# One history (with its retry), the bulk, the smoke
# ------------------------------------------------------------------------------------------


def run_history(o: dict, root: Path, tasks: dict, *, host: str | None, lim: dict,
                memory_mb: int, cpus: float, quota: threading.Event, say,
                run=run_attempt, held=None) -> dict:
    """One history: up to ATTEMPTS fresh attempt dirs (a done history is never relaunched;
    nothing launches after the quota flag), each inside the history's held slots. Books the
    ledger row for task 2's result and writes status.json."""
    held = held or HA.held_slots
    hdir = Path(root) / o["history"]
    hdir.mkdir(parents=True, exist_ok=True)
    src_att = source_attempt(SRC_DIR / o["history"])
    for att in _attempts(hdir):  # a quota cut recorded before a crash: final, never retried
        if settle(att):
            quota.set()
    for attempt in range(1, ATTEMPTS + 1):
        if _done(hdir):
            break
        att = hdir / f"attempt{attempt}"
        if att.exists():
            continue  # an earlier incomplete attempt: the next one runs fresh
        if quota.is_set():
            HA.note_history(hdir, event=f"{att.name} not launched",
                            reason="quota stop (shared flag)")
            break
        try:
            with held(host, f"agy-rerun{MAX_CALLS}:{o['history']}:{att.name}", lim, memory_mb, say):
                cfg = config(o, att, tasks, host=host, quota=quota, memory_mb=memory_mb,
                             cpus=HA.history_cpus(host, cpus))
                src = AF.load_source(cfg, src_att / "a")
                rec = run(src, att)
            say(f"{o['history']} {att.name}: hold {(rec.get('hold') or {}).get('ok')}, "
                f"B {rec.get('b_end')}")
        except Exception as e:  # noqa: BLE001 - recorded; retried once
            err = f"{type(e).__name__}: {e}"[:400]
            say(f"{o['history']} {att.name}: error {err}")
            att.mkdir(parents=True, exist_ok=True)
            (att / HA.ATTEMPT_ERROR).write_text(json.dumps({"error": err}))
        if _result(att / "b" / "result.json"):
            with contextlib.suppress(Exception):
                book(att / "b", o["history"])
        if quota.is_set():
            say(f"{o['history']} {att.name}: quota stop set; no further launches")
    st = history_state(hdir)
    (hdir / "status.json").write_text(json.dumps({"history": o["history"], **st}))
    return st


def bulk(host: str | None, lim: dict, memory_mb: int, cpus: float = HA.CPUS,
         root: Path | None = None, tasks: dict | None = None, admit=None, run_one=None,
         poll_s: float = 30.0, resume_after_quota: bool = False,
         first_repo: str | None = None, only_repo: str | None = None) -> int:
    """The bulk (module docstring). A quota cut persisted by an earlier run sets the shared
    stop at launch, so nothing new launches; `resume_after_quota` (Jacob's call, after the
    plan's quota has reset) launches the rest, the cut histories still kept as they are."""
    import fcntl
    if tasks is None:
        from compaction.s2.chain import driver as D
        tasks = D.tasks()
    admit = admit or HA.admit
    root = Path(root or BULK_DIR)
    chk = preflight(root, tasks)
    lockf = open(root / "bulk.lock", "w")  # noqa: SIM115 - held for the process's life
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"another bulk holds {root / 'bulk.lock'}")
    od = order()
    if first_repo:  # that repo's histories launch first, the seeded order kept within each
        od = sorted(od, key=lambda o: o["first_task"] != first_repo)
    capacity = {"host": host, "memory_mb": memory_mb, "cpus": cpus, **lim}
    (root / "assignment.json").write_text(json.dumps(
        {"seed": HA.SEED, "order": od, "capacity": capacity, "source": str(SRC_DIR),
         "caps": {"max_calls": MAX_CALLS, "max_seconds": MAX_SECONDS}}, indent=1))
    provenance(root, "bulk", capacity | {"source_check": {"ok": chk["ok"], "n": chk["n"]}})
    lock, live, quota = threading.Lock(), [], threading.Event()
    log = root / "bulk.log"

    def say(msg: str) -> None:
        line = f"{utc()} {msg}"
        with lock, open(log, "a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    marks = reconcile(root, [o["history"] for o in od])
    if marks:
        say(f"persisted quota cuts: {marks}"
            + ("; resuming after the quota reset (--resume-after-quota)" if resume_after_quota
               else "; shared stop set, nothing new launches"))
        if not resume_after_quota:
            quota.set()

    def one(o: dict) -> None:
        try:
            (run_one or run_history)(o, root, tasks, host=host, lim=lim, memory_mb=memory_mb,
                                     cpus=cpus, quota=quota, say=say)
        except Exception as e:  # noqa: BLE001 - a worker never kills the scheduler
            err = f"{type(e).__name__}: {e}"[:400]
            say(f"{o['history']}: worker error {err}")
            with contextlib.suppress(Exception):
                HA.note_history(root / o["history"], event="worker error", error=err)

    threads: list[threading.Thread] = []
    not_launched: dict[str, str] = {}

    def stop_here(i: int) -> None:
        for rest in od[i:]:
            if not _done(root / rest["history"]):
                not_launched[rest["history"]] = "quota stop (shared flag) before launch"
                HA.note_history(root / rest["history"], event="not launched",
                                reason="quota stop (shared flag)")

    for i, o in enumerate(od):
        if _done(root / o["history"]) or (only_repo and o["first_task"] != only_repo):
            continue  # done, or left for a later bulk (the Advisor's order: Argon in between)
        if quota.is_set():
            stop_here(i)
            break
        last_note = 0.0
        while True:
            with lock:
                live[:] = [th for th in live if th.is_alive()]
                n_live = len(live)
            ok, st = admit(n_live, host, lim)
            if ok or quota.is_set():
                break
            if time.monotonic() - last_note > 600:
                say(f"gate closed for {o['history']}: live {n_live}, {st}")
                last_note = time.monotonic()
            time.sleep(poll_s)
        if quota.is_set():
            stop_here(i)
            break
        say(f"launch {o['history']} (pos {o['pos']}, {o['arm']}) live {n_live} gate {st}")
        th = threading.Thread(target=one, args=(o,), name=o["history"])
        th.start()
        with lock:
            live.append(th)
        threads.append(th)
    for th in threads:
        th.join()
    final = {o["history"]: history_state(root / o["history"], not_launched.get(o["history"]))
             for o in od}
    (root / "reconciliation.json").write_text(json.dumps(final, indent=1))
    n_open = sum(v["state"] != "done" for v in final.values())
    n_unknown = sum(bool(v.get("primary_unknown_reason") or v.get("quota_cut"))
                    for v in final.values())
    say(f"bulk done: {len(final) - n_open} done ({n_unknown} with the primary unknown), "
        f"{n_open} not done (reconciliation.json)")
    return 0 if n_open == 0 and n_unknown == 0 else 3


def verify_saved_session(d: Path) -> dict:
    """Every file the saved session's manifest lists is on disk with the manifest's bytes and
    sha256 (astra rerun800 r1 m8). {"ok", "files", "bad": [(path, why)]}."""
    try:
        man = json.loads((Path(d) / "manifest.json").read_text())
    except (OSError, ValueError):
        return {"ok": False, "files": 0, "bad": [("manifest.json", "unreadable")]}
    bad = []
    for f in man.get("files") or []:
        p = Path(d) / f["path"]
        if not p.is_file():
            bad.append((f["path"], "missing"))
            continue
        data = p.read_bytes()
        if len(data) != f["bytes"]:
            bad.append((f["path"], f"{len(data)} bytes, manifest {f['bytes']}"))
        elif hashlib.sha256(data).hexdigest() != f["sha256"]:
            bad.append((f["path"], "sha256 differs from the manifest"))
    n = len(man.get("files") or [])
    return {"ok": n > 0 and not bad, "files": n, "bad": bad,
            "conversation_id": man.get("conversation_id")}


def smoke_checks(att: Path, src: AF.Source) -> dict:
    """The smoke's evidence: the fresh checkpoint from the forked export, the compacted session
    saved, task 2 resumed that fork after it with the raised caps, and its scorer state."""
    hold = json.loads((att / "a_compact" / "hold.json").read_text())
    man_p = att / "a_compact" / "session" / "manifest.json"
    man = json.loads(man_p.read_text()) if man_p.exists() else {}
    rb = _result(att / "b" / "result.json") or {}
    xb = rb.get("extra") or {}
    b_cids = {(t.get("info") or {}).get("conversation_id")
              for t in xb.get("turns") or []} - {None}
    saved = verify_saved_session(att / "a_compact" / "session")
    caps = xb.get("caps") or {}
    checks = {
        "fresh_compact_from_export": {
            "ok": bool(hold.get("ok")) and hold.get("forked_from") == src.man["conversation_id"]
            and (hold.get("boundary") or {}).get("ok") is True and bool(hold.get("checkpoint")),
            "forked_from": hold.get("forked_from"), "boundary": hold.get("boundary")},
        "no_hold_deviations": {"ok": hold.get("deviations") == [],
                               "deviations": hold.get("deviations")},
        "compacted_session_saved": {
            "ok": bool(man) and man.get("conversation_id") == hold.get("cid") and saved["ok"],
            "files": man.get("files"), "verified": saved},
        "raised_caps": {  # what task 2's TaskRun got (`agy_chain._task`'s extra.caps)
            "ok": caps == {"max_calls": MAX_CALLS, "max_seconds": MAX_SECONDS},
            "task_run": caps, "calls_used": rb.get("n_calls"),
            "seconds_used": rb.get("seconds"), "end": rb.get("end")},
        "task2_resumed_the_fork": {"ok": b_cids == {hold.get("cid")}, "b": sorted(b_cids)},
        "task2_after_the_checkpoint": {
            "ok": (xb.get("first_step") or -1) > src.man["last_step_index"],
            "first_step": xb.get("first_step")},
        "evidence_screened": {"ok": bool((xb.get("evidence_screen") or {}).get("ok"))
                              and bool((xb.get("evidence_post_screen") or {}).get("ok"))},
        "scorer_state": {"ok": bool(rb.get("traps_home"))},
        "no_quota_cut": {"ok": not X.quota_marks(rb), "mark": X.quota_marks(rb)},
    }
    return {"checks": checks, "all_ok": all(c["ok"] for c in checks.values()),
            "b": {k: rb.get(k) for k in ("end", "n_calls", "seconds", "error")}}


def write_assignment(root: Path, histories: list[dict], full: bool) -> None:
    """`<root>/assignment.json`, which the readout's row path needs: the whole order (the bulk
    root) or the smoke's histories so far, added to (never overwriting another history)."""
    p = Path(root) / "assignment.json"
    doc = json.loads(p.read_text()) if p.exists() else {
        "seed": HA.SEED, "source": str(SRC_DIR), "order": [],
        "caps": {"max_calls": MAX_CALLS, "max_seconds": MAX_SECONDS}}
    have = {o["history"] for o in doc["order"]}
    doc["order"] += [o for o in (order() if full else histories) if o["history"] not in have]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=1))


def readout_row(root: Path, hid: str) -> dict:
    """The smoke's history through the readout's own row path (`rows_for` on `root`,
    `mask`), so a layout the readout can't read fails here, before the bulk."""
    import week2_agy_task2_rerun_readout as RO
    rows, _, _ = RO.harness_readout(root).rows_for(RO.KEY)
    row = next((RO.mask(r) for r in rows if r["history"] == hid), None)
    if row is None:
        return {"ok": False, "why": "history not in the readout's rows"}
    keep = ("dir", "end", "end_raw", "teammate_lost", "n_push_commands", "rejection")
    return {"ok": bool(row.get("dir")), **{k: row.get(k) for k in keep}}


def smoke(hid: str, host: str | None, memory_mb: int, into_bulk: bool,
          cpus: float = HA.CPUS, *, tasks: dict | None = None, root: Path | None = None,
          pre=None, held=None, run=None) -> int:
    if tasks is None:
        from compaction.s2.chain import driver as D
        tasks = D.tasks()
    root = Path(root or (BULK_DIR if into_bulk else SMOKE_DIR))
    (pre or preflight)(root, tasks)
    o = next((x for x in order() if x["history"] == hid), None)
    if o is None:
        sys.exit(f"unknown history {hid}")
    hdir = root / hid
    for a in _attempts(hdir):
        settle(a)  # a quota cut before a crash is final
    if _done(hdir):
        sys.exit(f"{hdir} is done already")
    write_assignment(root, [o], full=into_bulk)
    att = hdir / f"attempt{len(list(hdir.glob('attempt*'))) + 1}"
    provenance(root, "smoke", {"history": hid, "host": host, "memory_mb": memory_mb,
                               "cpus": cpus, "into_bulk": into_bulk})
    quota = threading.Event()
    cfg = config(o, att, tasks, host=host, quota=quota, memory_mb=memory_mb,
                 cpus=HA.history_cpus(host, cpus))
    src = AF.load_source(cfg, source_attempt(SRC_DIR / hid) / "a")
    lim = {"max_live": 1, "max_containers": HA.MAX_CONTAINERS, "min_free_gb": HA.MIN_FREE_GB,
           "max_load": HA.MAX_LOAD}
    with (held or HA.held_slots)(host, f"agy-rerun{MAX_CALLS}:{hid}:{att.name}:smoke", lim,
                                 memory_mb, print):
        rec = (run or run_attempt)(src, att)
    if _result(att / "b" / "result.json"):
        book(att / "b", hid)
    rep = {"history": hid, "attempt": str(att), "run": rec}
    if (att / "a_compact" / "hold.json").exists():
        rep |= smoke_checks(att, src)
    if _result(att / "b" / "result.json"):
        rr = readout_row(root, hid)
        rep.setdefault("checks", {})["readout_row"] = rr
        rep["all_ok"] = bool(rep.get("all_ok")) and rr["ok"]
    (att / "smoke_report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps(rep, indent=1, default=str)[:8000])
    return 0 if rep.get("all_ok") else 3


def progress(root: Path | None = None) -> int:
    root = Path(root or BULK_DIR)
    od = order()
    out: dict = {"histories": len(od), "states": {}, "ends": {}, "calls": 0, "attempts": 0}
    for o in od:
        hdir = root / o["history"]
        st = history_state(hdir)
        out["states"][st["state"]] = out["states"].get(st["state"], 0) + 1
        out["attempts"] += len(list(hdir.glob("attempt*"))) if hdir.exists() else 0
        if st["state"] == "done":
            out["ends"][st["b_end"]] = out["ends"].get(st["b_end"], 0) + 1
            out["calls"] += st.get("n_calls") or 0
        print(json.dumps({"history": o["history"], "arm": o["arm"],
                          **{k: st.get(k) for k in ("state", "attempt", "b_end", "n_calls",
                                                    "primary_unknown_reason")}}))
    print(json.dumps(out))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    s = sub.add_parser("smoke")
    s.add_argument("--go-from-jacob", action="store_true", required=True)
    s.add_argument("--history", required=True)
    s.add_argument("--into-bulk", action="store_true",
                   help="write it where the bulk would (the bulk then skips this history)")
    s.add_argument("--host", default=X.HOST, help="'local' for the laptop")
    s.add_argument("--memory-mb", type=int, required=True)
    s.add_argument("--cpus", type=float, default=HA.CPUS)
    b = sub.add_parser("bulk")
    b.add_argument("--go-from-jacob", action="store_true", required=True)
    b.add_argument("--host", default=X.HOST, help="'local' for the laptop")
    b.add_argument("--max-live", type=int, required=True)
    b.add_argument("--memory-mb", type=int, required=True)
    b.add_argument("--cpus", type=float, default=HA.CPUS)
    b.add_argument("--max-containers", type=int, default=HA.MAX_CONTAINERS)
    b.add_argument("--min-free-gb", type=float, default=HA.MIN_FREE_GB)
    b.add_argument("--max-load", type=float, default=HA.MAX_LOAD)
    b.add_argument("--first-repo", choices=tuple(CC.REPOS),
                   help="launch the histories that start in this repo first")
    b.add_argument("--only-repo", choices=tuple(CC.REPOS),
                   help="launch only the histories that start in this repo")
    b.add_argument("--resume-after-quota", action="store_true",
                   help="Jacob's call: launch the rest after a persisted quota cut")
    p = sub.add_parser("progress")
    p.add_argument("--smoke", action="store_true")
    for x in (s, b, p):
        x.add_argument("--max-calls", type=int, default=MAX_CALLS,
                       help="task 2's call cap; other than 800 it has its own roots and stage")
    a = ap.parse_args()
    if getattr(a, "max_calls", MAX_CALLS) != MAX_CALLS:
        use_cap(a.max_calls)
    host = None if getattr(a, "host", None) in ("", "local") else getattr(a, "host", None)
    if a.cmd == "check":
        res = check()
        for r in res["rows"]:
            print(json.dumps(r))
        print(json.dumps({k: v for k, v in res.items() if k != "rows"}))
        return 0 if res["ok"] else 1
    if a.cmd == "progress":
        return progress(SMOKE_DIR if a.smoke else BULK_DIR)
    if a.cmd == "smoke":
        return smoke(a.history, host, a.memory_mb, a.into_bulk, a.cpus)
    return bulk(host, {"max_live": a.max_live, "max_containers": a.max_containers,
                       "min_free_gb": a.min_free_gb, "max_load": a.max_load}, a.memory_mb,
                a.cpus, resume_after_quota=a.resume_after_quota, first_repo=a.first_repo,
                only_repo=a.only_repo)


if __name__ == "__main__":
    raise SystemExit(main())
