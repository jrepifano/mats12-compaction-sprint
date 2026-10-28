"""Week-2 harness replication, Codex (plan docs/sprint/plans/week2-harness-replication.md). The
Codex twin of scripts/week2_harness_cc.py, whose seeded assignment and run-dir checks it loads.

    uv run --with fastmcp==4.1.3 python scripts/week2_harness_cx.py smoke --go-from-jacob [--remote divergent] [--host ssh://rig]
    uv run --with fastmcp==4.1.3 python scripts/week2_harness_cx.py bulk --go-from-jacob [--host ssh://rig] [--max-live 4]
    uv run python scripts/week2_harness_cx.py progress

No `init`: Codex runs on Jacob's ChatGPT plan ($0, nothing booked to the ledger). Each task
records the plan quota (`rate_limits.primary.used_percent`) before and after, so the smoke's
quota use sizes the bulk. Caps per task (400 calls, 2 h) are the driver's.

`bulk` runs the plan's 12 + 12 histories (the Claude Code script's `assignment`: seed 20261027,
blocks of first task x launch round, 3 divergent + 3 clean each, 2 rounds), both tasks each, at
most `--max-live` histories at once. Each history holds 2 container slots for its whole life in
the machine-wide ledger every native-harness scheduler shares (`slots.reserve`, astra cx-build
r1 #4: capacity `--capacity`, default 8, counting other schedulers' reservations and every
unregistered container on the host). Once a reservation is held, the host must also have room
(`resources_ok`): on the laptop (no `--host`) memory free >= 30% and load < 12; on a remote host
(`--host ssh://rig`) MemAvailable minus every reservation's memory (reserved slots x
`--memory-mb`) >= the 6 GB desktop floor, and load < its cores (r1 #5); else the reservation is
released and retried. A history with both results is skipped; an incomplete one is retried once
in a fresh attempt directory.

`smoke` runs one history (both tasks) and prints its checks: the user turns, the one boundary
compaction (and none in either task), the model on every turn, the context window, the thread
carried to B's container with only its rollout, no memories, no session-store reads, no auth
token in the rollout, `which git` from the agent's shell, the first stage, the second task's
scorer row, calls and quota. Uses the plan quota and Docker; needs Jacob's go, relayed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from compaction.deepswe import sandbox as SB
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import precedent as P
from compaction.s2.chain import score as S
from compaction.s2.native import common as N
from compaction.s2.native import cx_chain as X
from compaction.s2.native import slots as SL

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "sprint" / "s2" / "harness"
CX_ROOT = RAW / "cx"


def _cc_script():
    """scripts/week2_harness_cc.py, for its seeded assignment and result helpers (one source,
    so the two configurations launch the same histories in the same order)."""
    spec = importlib.util.spec_from_file_location("week2_harness_cc",
                                                  ROOT / "scripts" / "week2_harness_cc.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CCS = _cc_script()
assignment, _result_ok, _done = CCS.assignment, CCS._result_ok, CCS._done
MAX_CONTAINERS, MIN_FREE_PCT, MAX_LOAD = CCS.MAX_CONTAINERS, CCS.MIN_FREE_PCT, CCS.MAX_LOAD
LAPTOP_MEMORY_MB, REMOTE_MEMORY_MB, CPUS = CCS.MEMORY_MB, N.AGENT_MEMORY_MB, CCS.CPUS
MAX_LIVE = CCS.MAX_LIVE
OURS = N.purpose(X.ARM)  # "native-cx-chain": our containers' name prefix
STORE_MARKS = (X.CX.HOME_C, "codex-home", X.CX.SECRETS_C, ".codex/", "/opt/emx/cx-watch")


def _memory(host: str | None, memory_mb: int | None) -> int:
    return memory_mb or (LAPTOP_MEMORY_MB if host is None else REMOTE_MEMORY_MB)


# ------------------------------------------------------------------------------------------
# Smoke
# ------------------------------------------------------------------------------------------


def _session_reads(d: Path) -> list[dict]:
    """Any tool call whose arguments name Codex's own store, the auth tmpfs or the watcher."""
    mp = d / "messages.json"
    if not mp.exists():
        return [{"task": d.name, "unknown": "no transcript"}]
    out = []
    for m in json.loads(mp.read_text()):
        for tc in m.get("tool_calls") or []:
            args = (tc.get("function") or {}).get("arguments") or ""
            if any(s in args for s in STORE_MARKS):
                out.append({"task": d.name, "tool": tc["function"].get("name"),
                            "args": args[:200]})
    return out


def _checks(a_dir: Path, b_dir: Path) -> dict:
    """The smoke's feasibility checks; every one is required for `all_checks_ok`."""
    ra = json.loads((a_dir / "result.json").read_text())
    rb = json.loads((b_dir / "result.json").read_text())
    xa, xb = ra.get("extra") or {}, rb.get("extra") or {}
    git_a = (xa.get("checks") or {}).get("which_git") or []
    git_b = (xb.get("checks") or {}).get("which_git") or []
    bnd = xa.get("boundary") or {}
    man = xa.get("session_manifest") or {}
    spa, spb = xa.get("system_prompt") or {}, xb.get("system_prompt") or {}
    ua, ub = xa.get("usage") or {}, xb.get("usage") or {}
    reads = _session_reads(a_dir) + _session_reads(b_dir)
    setup = xb.get("b_setup") or {}
    turns_b = xb.get("turns") or []
    files = man.get("files") or []
    return {
        "wrapper_git": {"ok": all(g and "/var/lib/gitd/bin" in g[0] for g in (git_a, git_b)),
                        "a": git_a, "b": git_b},
        "system_prompt": {"ok": bool(spa.get("sha256")) and spa.get("sha256") == spb.get(
            "sha256"), "a": spa, "b": spb},
        "model_pinned": {"ok": xa.get("models") == [X.MODEL] and xb.get("models") == [X.MODEL],
                         "a": xa.get("models"), "b": xb.get("models")},
        "context_window": {"ok": ua.get("context_windows") == [X.EXPECTED_WINDOW]
                           and ub.get("context_windows") == [X.EXPECTED_WINDOW],
                           "a": ua.get("context_windows"), "b": ub.get("context_windows")},
        "no_compaction_before_boundary": {"ok": bnd.get("before") == 0
                                          and xa.get("compactions_in_task") == 0,
                                          "before": bnd.get("before")},
        "one_boundary_compaction": {"ok": bool(bnd.get("ok")) and man.get("compactions") == 1,
                                    "boundary": bnd, "manifest": man},
        "no_compaction_in_task_2": {"ok": xb.get("compactions_in_task") == 0
                                    and xb.get("compactions_in_rollout") == 1,
                                    "in_task": xb.get("compactions_in_task"),
                                    "in_rollout": xb.get("compactions_in_rollout")},
        "thread_carried": {"ok": bool(xa.get("thread_id")) and xa.get("thread_id") == xb.get(
            "thread_id") and xb.get("imported_prefix_intact") is True
            and not any(t["info"].get("thread_mismatch") for t in turns_b),
            "a": xa.get("thread_id"), "b": xb.get("thread_id"),
            "prefix_intact": xb.get("imported_prefix_intact")},
        "one_thread_per_container": {"ok": xa.get("rollouts_in_container") == 1
                                     and xb.get("rollouts_in_container") == 1,
                                     "a": xa.get("rollouts_in_container"),
                                     "b": xb.get("rollouts_in_container")},
        "rollout_only_transfer": {"ok": len(files) == 1
                                  and str(files[0].get("path", "")).startswith("sessions/"),
                                  "files": files},
        "no_memories": {"ok": all(h is not None and "memories" not in h for h in (
            xa.get("codex_home"), xb.get("codex_home"))),
            "a": xa.get("codex_home"), "b": xb.get("codex_home")},
        "no_session_file_reads": {"ok": not reads, "reads": reads},
        "no_auth_in_rollout": {"ok": xa.get("secrets_in_rollout") == 0
                               and xb.get("secrets_in_rollout") == 0,
                               "a": xa.get("secrets_in_rollout"),
                               "b": xb.get("secrets_in_rollout")},
        "quota_recorded": {"ok": (ua.get("quota_used_percent") or {}).get("last") is not None
                           and (ub.get("quota_used_percent") or {}).get("last") is not None,
                           "a": ua.get("quota_used_percent"), "b": ub.get("quota_used_percent")},
        "b_cutoff_found": {"ok": xb.get("b_message_index") is not None,
                           "index": xb.get("b_message_index")},
        "b_setup_fingerprint": {"ok": bool(setup.get("teammate"))
                                and bool(setup.get("remote_refs")), "b_setup": setup,
                                "note": "compare across arms in the bulk (one arm here)"},
    }


def _task_summary(d: Path) -> dict:
    r = json.loads((d / "result.json").read_text())
    x = r.get("extra") or {}
    out = {k: r.get(k) for k in ("end", "n_calls", "prompt_tokens", "cached_tokens",
                                 "completion_tokens", "seconds", "error")}
    out["decisions"] = [t["decision"]["kind"] for t in x.get("turns") or []]
    out["quota"] = (x.get("usage") or {}).get("quota_used_percent")
    out["auth_refreshed"] = x.get("auth_refreshed")
    out["egress"] = x.get("egress")
    return out


def smoke(remote: str, repo: str, host: str | None, memory_mb: int | None) -> int:
    tasks = D.tasks()
    hist = CC.History(repo, "np", 900, remote)  # rep 900: never a bulk history
    a_repo = CC.REPOS[repo]
    b_repo = CC.REPOS[next(r for r in CC.REPOS if r != repo)]
    out = RAW / "smoke-cx" / hist.id / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    cfg = X.HistoryConfig(hist=hist, a_task=tasks[a_repo.a_task], b_task=tasks[b_repo.b_task],
                          out=out, host=host, memory_mb=_memory(host, memory_mb), cpus=CPUS)
    res = X.run_history(cfg)
    rep: dict = {"history": hist.id, "remote": remote, "host": host, "run": res}
    a_dir, b_dir = Path(res["a"]), Path(res["b"])
    rep["a"] = _task_summary(a_dir)
    rep["first_stage"] = P.first_stage(a_dir, hist)
    if (a_dir / "messages.json").exists():
        msgs = json.loads((a_dir / "messages.json").read_text())
        rep["a"]["pushes"] = [{k: p[k] for k in ("cmd", "rejected", "changed", "force_flag")}
                              for p in S.pushes(S.bash_steps(msgs, 0))]
    rep["b"] = _task_summary(b_dir)
    rb = json.loads((b_dir / "result.json").read_text())
    if rb.get("traps_home"):
        brun = {"id": f"{hist.id}-b", "cell": {"b_task": b_repo.b_task, "hist": remote,
                                               "project": "different", "start": "compacted"},
                "history": hist.id, "rep": hist.rep}
        row = S.score_b(b_dir, brun)
        rep["b"]["row"] = {k: row.get(k) for k in (
            "teammate_lost", "rewrite", "rejection", "trap_status", "n_push_commands",
            "force_flag_commands", "teammate_checked")}
    rep["checks"] = _checks(a_dir, b_dir)
    rep["all_checks_ok"] = all(c["ok"] for c in rep["checks"].values())
    (out / "smoke_report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps(rep, indent=1, default=str)[:8000])
    return 0


# ------------------------------------------------------------------------------------------
# The host gate
# ------------------------------------------------------------------------------------------


def slot_host(host: str | None) -> str:
    """The ledger's key for a Docker host (shared with the other harness schedulers)."""
    return host or "laptop"


def host_names(host: str | None) -> list[str]:
    """The host's running container names; raises when unreadable (the gate stays closed)."""
    p = subprocess.run(SB.docker_argv(["ps", "--format", "{{.Names}}"], host),
                       capture_output=True, text=True, timeout=120, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"docker ps rc={p.returncode}")
    return p.stdout.split()


def gate_reading(host: str | None) -> dict:
    """The host's memory and load. A failed reading raises (the gate stays closed)."""
    if host is None:
        mp = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=60,
                            check=False)
        sy = subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True,
                            timeout=30, check=False)
        if mp.returncode != 0 or sy.returncode != 0:
            raise RuntimeError(f"memory_pressure rc={mp.returncode}, sysctl rc={sy.returncode}")
        return {"free_pct": next(int(x.split(":")[1].strip().rstrip("%"))
                                 for x in mp.stdout.splitlines() if "free percentage" in x),
                "load1": float(sy.stdout.split()[1])}
    if not host.startswith("ssh://"):
        raise RuntimeError(f"{host}: only ssh:// hosts can be read")
    cmd = "awk '/MemAvailable/ {print $2}' /proc/meminfo; cut -d' ' -f1 /proc/loadavg; nproc"
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=20", host.removeprefix("ssh://"), cmd],
                       capture_output=True, text=True, timeout=60, check=False)
    vals = r.stdout.split()
    if r.returncode != 0 or len(vals) != 3:
        raise RuntimeError(f"ssh reading rc={r.returncode}")
    return {"free_gb": round(int(vals[0]) / 1024 / 1024, 1), "load1": float(vals[1]),
            "cores": int(vals[2])}


def resources_ok(host: str | None, reserved_mb: int) -> tuple[bool, dict]:
    """The host has room for every reservation on it (ours included). Laptop: memory free
    >= 30%, load < 12. Remote: MemAvailable - the memory every reservation on the host declared
    (any scheduler's, astra cx-build r3 #5) >= the desktop floor (N.AGENT_MIN_FREE_GB),
    load < cores."""
    try:
        st = gate_reading(host)
    except Exception as e:  # noqa: BLE001 - a failed reading keeps the gate closed
        return False, {"error": f"{type(e).__name__}: {e}"[:200]}
    st["reserved_mb"] = reserved_mb
    if host is None:
        return st["free_pct"] >= MIN_FREE_PCT and st["load1"] < MAX_LOAD, st
    return (st["free_gb"] - reserved_mb / 1024 >= N.AGENT_MIN_FREE_GB
            and st["load1"] < st["cores"]), st


def history_cpus(host: str | None, cpus: float) -> float:
    """A new laptop history's CPU limit: the limits file's `__cpu_tenths__`, else `cpus`."""
    if host is not None:
        return cpus
    return SL.setting("__cpu_tenths__", round(cpus * 10)) / 10


def acquire(host: str | None, owner: str, *, memory_mb: int, capacity: int, poll_s: float,
            names=None, say=None, timeout_s: float | None = None, **slot_kw):
    """Two slots for one history in the shared ledger, held only while the host also has room
    (`resources_ok`, counting every reservation on it); otherwise released and retried.
    Returns the entered reservation (exit it when the history ends) and the reading."""
    names = names or (lambda: host_names(host))
    t0, last = time.monotonic(), [0.0]

    def on_wait(st: dict) -> None:
        if say is not None and time.monotonic() - last[0] > 600:  # logged every 10 min
            say(f"slots closed for {owner}: {st}")
            last[0] = time.monotonic()

    while True:
        left = None if timeout_s is None else max(0.0, timeout_s - (time.monotonic() - t0))
        cm = SL.reserve(slot_host(host), owner, n=2, prefix=OURS, capacity=capacity,
                        names=names, memory_mb=2 * memory_mb, poll_s=poll_s, timeout_s=left,
                        on_wait=on_wait, **slot_kw)  # two containers of memory_mb each
        cm.__enter__()
        try:
            state = {"state": slot_kw["state"]} if "state" in slot_kw else {}
            ok, st = resources_ok(host, SL.reserved_memory_mb(slot_host(host), **state))
        except Exception as e:  # noqa: BLE001 - an unreadable host is closed
            ok, st = False, {"error": f"{type(e).__name__}: {e}"[:200]}
        if ok:
            return cm, st
        cm.__exit__(None, None, None)
        on_wait(st)
        if timeout_s is not None and time.monotonic() - t0 > timeout_s:
            raise TimeoutError(f"no room on {slot_host(host)} for {owner}: {st}")
        time.sleep(poll_s)


# ------------------------------------------------------------------------------------------
# Bulk and progress
# ------------------------------------------------------------------------------------------


def bulk(host: str | None, max_live: int, memory_mb: int | None, capacity: int,
         cpus: float = CPUS) -> int:
    import fcntl
    tasks = D.tasks()
    order = assignment()
    mem = _memory(host, memory_mb)
    root = CX_ROOT
    root.mkdir(parents=True, exist_ok=True)
    lockf = open(root / "bulk.lock", "w")  # noqa: SIM115 - held for the process's life
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one bulk at a time
    except BlockingIOError:
        sys.exit(f"another bulk holds {root / 'bulk.lock'}")
    (root / "assignment.json").write_text(json.dumps(
        {"seed": CCS.SEED, "order": order, "host": host, "memory_mb": mem, "cpus": cpus,
         "max_live": max_live, "capacity": capacity}, indent=1))
    lock, live = threading.Lock(), []
    log = root / "bulk.log"
    poll_s = 30 if host is None else 60  # a remote sshd is shared: poll it gently

    def say(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
        with lock, open(log, "a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def status(hid: str, **kw) -> None:
        (root / hid / "status.json").write_text(json.dumps({"history": hid, **kw}))

    def one(o: dict, slot) -> None:
        try:
            run_one(o)
        finally:
            slot.__exit__(None, None, None)  # the history's slots, whatever happened

    def run_one(o: dict) -> None:
        hist = CC.History(o["first_task"], "np", o["rep"], o["arm"])
        a_repo = CC.REPOS[hist.repo]
        b_repo = CC.REPOS[next(r for r in CC.REPOS if r != hist.repo)]
        hdir = root / hist.id
        hdir.mkdir(parents=True, exist_ok=True)
        try:
            for attempt in (1, 2):
                if _done(hdir):
                    status(hist.id, state="done")
                    return
                out = hdir / f"attempt{attempt}"
                if out.exists():
                    continue  # an earlier incomplete attempt: the next one runs fresh
                cfg = X.HistoryConfig(hist=hist, a_task=tasks[a_repo.a_task],
                                      b_task=tasks[b_repo.b_task], out=out, host=host,
                                      memory_mb=mem, cpus=history_cpus(host, cpus))
                try:
                    res = X.run_history(cfg)
                    say(f"{hist.id} a{attempt}: A {res.get('a_end')}, B {res.get('b_end')}")
                except Exception as e:  # noqa: BLE001 - recorded; retried once
                    say(f"{hist.id} a{attempt}: error {type(e).__name__}: {e}"[:400])
            status(hist.id, state="done" if _done(hdir) else "unknown",
                   note=None if _done(hdir) else "both attempts incomplete")
        except Exception as e:  # noqa: BLE001 - a worker never kills the scheduler
            say(f"{hist.id}: worker error {type(e).__name__}: {e}"[:400])
            status(hist.id, state="unknown", note=f"worker error: {e}"[:300])

    threads: list[threading.Thread] = []
    for o in order:
        if _done(root / o["history"]):
            status(o["history"], state="done")
            continue
        waited, last_note = 0, 0.0
        while True:  # this process's own limit first, then the shared ledger and the host
            with lock:
                live[:] = [th for th in live if th.is_alive()]
                n_live = len(live)
            if n_live < max_live:
                break
            if time.monotonic() - last_note > 600:
                say(f"max live reached for {o['history']} ({waited}s): live {n_live}")
                last_note = time.monotonic()
            time.sleep(poll_s)
            waited += poll_s
        slot, st = acquire(host, f"cx-{o['history']}", memory_mb=mem, capacity=capacity,
                           poll_s=poll_s, say=say)
        say(f"launch {o['history']} (pos {o['pos']}, round {o['round']}, {o['arm']}) "
            f"live {n_live} gate {st}")
        th = threading.Thread(target=one, args=(o, slot), name=o["history"])
        try:
            th.start()
        except BaseException:
            slot.__exit__(None, None, None)
            raise
        with lock:
            live.append(th)
        threads.append(th)
    for th in threads:
        th.join()
    final = {o["history"]: ("done" if _done(root / o["history"]) else "unknown") for o in order}
    (root / "reconciliation.json").write_text(json.dumps(final, indent=1))
    n_unknown = sum(v != "done" for v in final.values())
    say(f"bulk done: {len(final) - n_unknown} done, {n_unknown} unknown (reconciliation.json)")
    return 0 if n_unknown == 0 else 3


def progress() -> int:
    """One outcome per assigned history (its latest attempt with a first-task result), unknowns
    counted apart; calls and the quota range seen across every attempt."""
    root = CX_ROOT
    order = json.loads((root / "assignment.json").read_text())["order"]
    out = {"histories": len(order), "a_done": 0, "b_done": 0, "attempts": 0,
           "first_stage": {"lost": 0, "kept": 0, "unknown": 0}, "ends": {}, "calls": 0,
           "boundary_ok": 0, "quota_used_percent": {"min": None, "max": None}}
    pcts: list[float] = []
    for o in order:
        hist = CC.History(o["first_task"], "np", o["rep"], o["arm"])
        atts = sorted((root / hist.id).glob("attempt*"))
        out["attempts"] += len(atts)
        for att in atts:
            for t in ("a", "b"):
                if _result_ok(att / t / "result.json"):
                    r = json.loads((att / t / "result.json").read_text())
                    out["calls"] += r.get("n_calls") or 0
                    q = ((r.get("extra") or {}).get("usage") or {}).get(
                        "quota_used_percent") or {}
                    pcts += [v for v in q.values() if isinstance(v, (int, float))]
        last = next((a for a in reversed(atts) if _result_ok(a / "a" / "result.json")), None)
        if last is None:
            continue
        ra = json.loads((last / "a" / "result.json").read_text())
        out["a_done"] += 1
        out["ends"][f"A {ra['end']}"] = out["ends"].get(f"A {ra['end']}", 0) + 1
        out["boundary_ok"] += bool(((ra.get("extra") or {}).get("boundary") or {}).get("ok"))
        if o["arm"] == "divergent":
            fl = P.first_stage(last / "a", hist).get("foreign_lost")
            out["first_stage"]["lost" if fl is True else "kept" if fl is False
                               else "unknown"] += 1
        if _result_ok(last / "b" / "result.json"):
            rb = json.loads((last / "b" / "result.json").read_text())
            out["b_done"] += 1
            out["ends"][f"B {rb['end']}"] = out["ends"].get(f"B {rb['end']}", 0) + 1
    if pcts:
        out["quota_used_percent"] = {"min": min(pcts), "max": max(pcts)}
    print(json.dumps(out))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke")
    s.add_argument("--go-from-jacob", action="store_true", required=True)
    s.add_argument("--remote", default="divergent", choices=("divergent", "clean"))
    s.add_argument("--repo", default="tengo", choices=tuple(CC.REPOS))
    s.add_argument("--host", default=None, help="docker -H (default: the laptop's daemon)")
    s.add_argument("--memory-mb", type=int, default=None)
    b = sub.add_parser("bulk")
    b.add_argument("--go-from-jacob", action="store_true", required=True)
    b.add_argument("--host", default=None, help="docker -H (default: the laptop's daemon)")
    b.add_argument("--max-live", type=int, default=MAX_LIVE)
    b.add_argument("--memory-mb", type=int, default=None)
    b.add_argument("--cpus", type=float, default=CPUS,
                   help="each task container's CPU limit (the laptop rule: limits sum <= 8)")
    b.add_argument("--capacity", type=int, default=MAX_CONTAINERS,
                   help="container slots on the host, shared with the other harness bulks")
    sub.add_parser("progress")
    a = ap.parse_args()
    if a.cmd == "bulk":
        return bulk(a.host, a.max_live, a.memory_mb, a.capacity, a.cpus)
    if a.cmd == "progress":
        return progress()
    return smoke(a.remote, a.repo, a.host, a.memory_mb)


if __name__ == "__main__":
    raise SystemExit(main())
