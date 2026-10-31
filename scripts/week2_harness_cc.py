"""Week-2 harness replication, Claude Code (plan docs/sprint/plans/week2-harness-replication.md).

    uv run python scripts/week2_harness_cc.py init                 # the stage's caps file ($0)
    uv run --with fastmcp==4.1.3 python scripts/week2_harness_cc.py smoke --go-from-jacob [--remote divergent]
    uv run --with fastmcp==4.1.3 python scripts/week2_harness_cc.py bulk --go-from-jacob
    uv run python scripts/week2_harness_cc.py progress

`bulk` runs the plan's 12 + 12 histories (`assignment`: seed 20261027, blocks of first task x
launch round, 3 divergent + 3 clean each, 2 rounds; rounds in order, shuffled within), both tasks
each, at most `MAX_LIVE` histories at once, each launch waiting on the laptop gate (`gate_ok`:
other containers + 2 per live history (ours reserved for their whole life) <= 8, memory free >= 30%, load < 12). A history with both
results is skipped; an incomplete one is retried once in a fresh attempt directory.

`smoke` runs one history (both tasks) on the laptop's Docker and prints its checks: the user
turns, the one boundary compaction, `which git` from the agent's shell, the first stage, the
second task's scorer row, cost and calls. Paid (Haiku 5.5 on the Max-linked API credits, booked
to SPRINT-S2-HARNESS); needs Jacob's go, relayed.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import precedent as P
from compaction.s2.chain import score as S
from compaction.s2.model import RunSpend
from compaction.s2.native import cc_chain as X
from compaction.s2.native.key_proxy import read_key

ROOT = Path(__file__).resolve().parents[1]
STAGE, CAP_USD, TASK_CAP_USD = "SPRINT-S2-HARNESS", 300.0, 6.0
TAG = "s2-harness"
RAW = ROOT / "data" / "raw" / "sprint" / "s2" / "harness"
# the configurations this scheduler runs (`--arm`): Haiku 5.5 (plan v0.3.1) and DeepSeek V4.1
# Flash in the same CLI through OpenRouter -> Together (Jacob, 28 Oct: "2. go", cap $120)
ARMS = {
    "haiku": {"arm": "HAIKU", "stage": "SPRINT-S2-HARNESS", "cap": 300.0, "task_cap": 6.0,
              "tag": "s2-harness", "dir": "cc", "smoke": "smoke", "caps": "budget_caps.toml",
              "key": "ANTHROPIC_API_KEY", "cpus": 2.0, "max_live": 4, "capacity": 8},
    "ds": {"arm": "DEEPSEEK", "stage": "SPRINT-S2-HARNESS-DS", "cap": 120.0, "task_cap": 6.0,
           "tag": "s2-harness-ds", "dir": "cc_ds", "smoke": "smoke_ds",
           "caps": "budget_caps_ds.toml", "key": "OPENROUTER_API_KEY", "cpus": 1.0,
           "max_live": 5, "capacity": 16},
}
ARM_NAME = "haiku"


def select(name: str) -> None:
    """Point the module's stage, caps, run dirs, key and CLI arm at configuration `name`."""
    global ARM_NAME, STAGE, CAP_USD, TASK_CAP_USD, TAG, CPUS, MAX_LIVE, OURS, MAX_CONTAINERS
    a = ARMS[name]
    ARM_NAME, STAGE, CAP_USD, TASK_CAP_USD, TAG = (name, a["stage"], a["cap"], a["task_cap"],
                                                   a["tag"])
    CPUS, MAX_LIVE = a["cpus"], a["max_live"]  # per-arm defaults (ds-build r1 m3)
    # the laptop rule as relaxed by Jacob on 28 Oct for the DeepSeek arm: task containers only,
    # i.e. 16 containers with sidecars (the limits file's `__capacity__` can override it)
    MAX_CONTAINERS = a["capacity"]
    OURS = f"native-{getattr(X, a['arm']).session.replace('_', '-')}"  # its own prefix (B1)


def arm():
    return getattr(X, ARMS[ARM_NAME]["arm"])


def bulk_dir() -> Path:
    return RAW / ARMS[ARM_NAME]["dir"]


def api_key() -> str:
    return read_key(ROOT / ".env", ARMS[ARM_NAME]["key"])


def caps_file() -> Path:
    return RAW / ARMS[ARM_NAME]["caps"]


def init_caps() -> Path:
    p = caps_file()
    content = ("# Week-2 harness replication only (docs/sprint/plans/week2-harness-replication.md)"
               "\n[sprint]\n" f"checkout = {json.dumps(str(ROOT))}\n"
               f"core_cap_usd = {CAP_USD}\nfull_cap_usd = {CAP_USD}\n"
               "topup_recorded = false\ntransfer_approval_over_usd = 0.0\n\n"
               f"[stages.{STAGE}]\ncap_usd = {CAP_USD}\n")
    if p.exists():
        if p.read_text() != content:
            sys.exit(f"{p} exists with other content; not overwriting")
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def stage_spend():
    from compaction.s2.interact_run import PilotSpend
    return PilotSpend(tags=(TAG,), caps={TAG: CAP_USD}, stage=STAGE,
                      ledger=ROOT / "logs" / "costs.jsonl", caps_file=caps_file())


def task_spend():
    return RunSpend(stage_spend(), cap_usd=TASK_CAP_USD)


def _session_reads(d: Path) -> list[dict]:
    """Any tool call whose arguments name the CLI's own session store (Bash, Read, Grep, ...)."""
    mp = d / "messages.json"
    if not mp.exists():
        return [{"task": d.name, "unknown": "no transcript"}]
    out = []
    for m in json.loads(mp.read_text()):
        for tc in m.get("tool_calls") or []:
            args = (tc.get("function") or {}).get("arguments") or ""
            if "/opt/emx/cc" in args or ".claude" in args:
                out.append({"task": d.name, "tool": tc["function"].get("name"),
                            "args": args[:200]})
    return out


def _compactions(d: Path) -> int | None:
    sp = d / "session.jsonl"
    if not sp.exists():
        return None
    rows = [json.loads(x) for x in sp.read_text().splitlines() if x.strip()]
    return len([r for r in rows if r.get("type") == "system"
                and r.get("subtype") == "compact_boundary"])


def _checks(a_dir: Path, b_dir: Path) -> dict:
    """The smoke's feasibility checks; every one is required for `all_checks_ok` (astra cc-build
    r1/r2 m11)."""
    ra = json.loads((a_dir / "result.json").read_text())
    rb = json.loads((b_dir / "result.json").read_text())
    xa, xb = ra.get("extra") or {}, rb.get("extra") or {}
    git_a = (xa.get("checks") or {}).get("which_git") or []
    git_b = (xb.get("checks") or {}).get("which_git") or []
    bnd = xa.get("boundary") or {}
    man = xa.get("session_manifest") or {}
    spa, spb = xa.get("system_prompt") or {}, xb.get("system_prompt") or {}
    reads = _session_reads(a_dir) + _session_reads(b_dir)
    setup = xb.get("b_setup") or {}
    return {
        "wrapper_git": {"ok": all(g and "/var/lib/gitd/bin" in g[0] for g in (git_a, git_b)),
                        "a": git_a, "b": git_b},
        "system_prompt": {"ok": bool(spa.get("sha256")) and spa.get("sha256") == spb.get(
            "sha256"), "a": spa, "b": spb},
        "no_compaction_before_boundary": {"ok": bnd.get("before") == 0,
                                          "before": bnd.get("before")},
        "one_boundary_compaction": {"ok": bool(bnd.get("ok")) and man.get(
            "compact_boundaries") == 1, "boundary": bnd, "manifest": man},
        "no_compaction_in_task_2": {"ok": _compactions(b_dir) == 1,
                                    "b_session_boundaries": _compactions(b_dir)},
        "no_session_file_reads": {"ok": not reads, "reads": reads},
        "b_cutoff_found": {"ok": xb.get("b_message_index") is not None,
                           "index": xb.get("b_message_index")},
        "b_setup_fingerprint": {"ok": bool(setup.get("teammate"))
                                and bool(setup.get("remote_refs")), "b_setup": setup,
                                "note": "compare across arms in the bulk (one arm here)"},
    }


def smoke(remote: str, repo: str) -> int:
    if not caps_file().exists():
        sys.exit("run `init` first")
    tasks = D.tasks()
    hist = CC.History(repo, "np", 900, remote)  # rep 900: never a bulk history
    a_repo = CC.REPOS[repo]
    b_repo = CC.REPOS[next(r for r in CC.REPOS if r != repo)]
    out = RAW / ARMS[ARM_NAME]["smoke"] / hist.id / time.strftime("%Y%m%dT%H%M%SZ",
                                                                  time.gmtime())  # m10
    cfg = X.HistoryConfig(hist=hist, a_task=tasks[a_repo.a_task], b_task=tasks[b_repo.b_task],
                          out=out, host_ip="host.docker.internal", spend_factory=task_spend,
                          key=api_key(), arm=arm(),
                          # the Haiku smoke keeps its defaults (ds-build r1 m4); the DeepSeek
                          # smoke runs at its bulk's laptop resources
                          **({} if ARM_NAME == "haiku" else
                             {"memory_mb": MEMORY_MB, "cpus": history_cpus()}))
    res = X.run_history(cfg)
    rep: dict = {"history": hist.id, "remote": remote, "run": res}
    a_dir, b_dir = Path(res["a"]), Path(res["b"])
    ra = json.loads((a_dir / "result.json").read_text())
    rep["a"] = {k: ra.get(k) for k in ("end", "n_calls", "cost_usd", "seconds", "error")}
    rep["a"]["decisions"] = [t["decision"]["kind"] for t in ra["extra"].get("turns") or []]
    rep["first_stage"] = P.first_stage(a_dir, hist)
    if (a_dir / "messages.json").exists():
        msgs = json.loads((a_dir / "messages.json").read_text())
        rep["a"]["pushes"] = [{k: p[k] for k in ("cmd", "rejected", "changed", "force_flag")}
                              for p in S.pushes(S.bash_steps(msgs, 0))]
    rb = json.loads((b_dir / "result.json").read_text())
    rep["b"] = {k: rb.get(k) for k in ("end", "n_calls", "cost_usd", "seconds", "error")}
    rep["b"]["decisions"] = [t["decision"]["kind"] for t in rb["extra"].get("turns") or []]
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


SEED, PER_BLOCK, ROUNDS = 20261027, 3, 2
MAX_LIVE, MAX_CONTAINERS, MIN_FREE_PCT, MAX_LOAD = 4, 8, 30, 12.0
MEMORY_MB, CPUS = 1280, 2.0  # the laptop rule (<= 8 containers x 1.25 GB); experiment 1's 1280


def assignment() -> list[dict]:
    """The seeded launch order: per round, per first task, PER_BLOCK divergent + PER_BLOCK
    clean histories (reps numbered per repo and arm), shuffled within the round."""
    rng = random.Random(SEED)
    nxt = {(r, a): 1 for r in CC.REPOS for a in ("divergent", "clean")}
    order = []
    for rd in range(1, ROUNDS + 1):
        units = []
        for repo in sorted(CC.REPOS):
            for arm in ("divergent", "clean"):
                for _ in range(PER_BLOCK):
                    units.append((repo, arm, nxt[(repo, arm)]))
                    nxt[(repo, arm)] += 1
        rng.shuffle(units)
        for repo, arm, rep in units:
            h = CC.History(repo, "np", rep, arm)
            order.append({"pos": len(order) + 1, "round": rd, "first_task": repo, "arm": arm,
                          "rep": rep, "history": h.id})
    return order


OURS = "native-cc-chain"  # our containers' name prefix (task container and egress sidecar)


def gate_reading() -> dict:
    """Containers machine-wide (ours apart), memory free %, load. A failed reading is closed."""
    p = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True,
                       text=True, timeout=60, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"docker ps rc={p.returncode}")
    names = p.stdout.split()
    mp = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=60,
                        check=False)
    sy = subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True,
                        timeout=30, check=False)
    if mp.returncode != 0 or sy.returncode != 0:
        raise RuntimeError(f"memory_pressure rc={mp.returncode}, sysctl rc={sy.returncode}")
    free = next(int(x.split(":")[1].strip().rstrip("%")) for x in mp.stdout.splitlines()
                if "free percentage" in x)
    load = float(sy.stdout.split()[1])
    return {"others": sum(not n.startswith(OURS) for n in names),
            "ours_running": sum(n.startswith(OURS) for n in names),
            "free_pct": free, "load1": load}


def _names() -> list[str]:
    p = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True,
                       text=True, timeout=60, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"docker ps rc={p.returncode}")
    return p.stdout.split()


def admit(n_live: int) -> tuple[bool, dict]:
    """The process-local laptop gate (memory, load, max live); the cross-scheduler slot count is
    the shared ledger each worker holds (`native.slots`, astra cx-build r2 B3). Other
    containers + 2 x (live + 1) <= MAX_CONTAINERS, memory free >= 30%, load < 12."""
    try:
        st = gate_reading()
    except Exception as e:  # noqa: BLE001 - a failed reading keeps the gate closed
        return False, {"error": f"{type(e).__name__}: {e}"[:200]}
    from compaction.s2.native import slots as SL
    try:
        cap = SL.setting("__capacity__", MAX_CONTAINERS)  # changeable on the fly
    except Exception as e:  # noqa: BLE001 - a malformed limits file keeps the gate closed
        return False, {**st, "error": f"limits: {e}"[:200]}
    st["capacity"] = cap
    ok = (st["others"] + 2 * (n_live + 1) <= cap and st["free_pct"] >= MIN_FREE_PCT
          and st["load1"] < MAX_LOAD and n_live < MAX_LIVE)
    return ok, st


def history_cpus() -> float:
    """Each new history's CPU limit: the limits file's `__cpu_tenths__`, else `CPUS`."""
    from compaction.s2.native import slots as SL
    return SL.setting("__cpu_tenths__", round(CPUS * 10)) / 10


def _result_ok(p: Path) -> bool:
    try:
        return "end" in json.loads(p.read_text())
    except (OSError, ValueError):
        return False


def _done(d: Path) -> bool:
    """A history is done when one attempt has both results, each a complete JSON record."""
    return any(_result_ok(a / "a" / "result.json") and _result_ok(a / "b" / "result.json")
               for a in d.glob("attempt*"))


def bulk() -> int:
    import fcntl
    if not caps_file().exists():
        sys.exit("run `init` first")
    tasks = D.tasks()
    order = assignment()
    root = bulk_dir()
    root.mkdir(parents=True, exist_ok=True)
    lockf = open(root / "bulk.lock", "w")  # noqa: SIM115 - held for the process's life
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one bulk at a time (B3)
    except BlockingIOError:
        sys.exit(f"another bulk holds {root / 'bulk.lock'}")
    (root / "assignment.json").write_text(json.dumps(
        {"seed": SEED, "order": order, "arm": ARM_NAME, "model": arm().model,
         "effort": arm().effort, "max_live": MAX_LIVE, "cpus": CPUS, "memory_mb": MEMORY_MB},
        indent=1))
    key = api_key()
    lock, live = threading.Lock(), []
    log = root / "bulk.log"

    def say(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
        with lock, open(log, "a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def status(hid: str, **kw) -> None:
        (root / hid / "status.json").write_text(json.dumps({"history": hid, **kw}))

    def one(o: dict) -> None:
        hist = CC.History(o["first_task"], "np", o["rep"], o["arm"])
        a_repo = CC.REPOS[hist.repo]
        b_repo = CC.REPOS[next(r for r in CC.REPOS if r != hist.repo)]
        hdir = root / hist.id
        hdir.mkdir(parents=True, exist_ok=True)
        from compaction.s2.native import slots as SL
        try:  # the shared ledger's 2 slots for the history's whole life (cx-build r2 B3)
            with SL.reserve("laptop", f"cc:{hist.id}", n=2, prefix=OURS,
                            capacity=MAX_CONTAINERS, names=_names, memory_mb=2 * MEMORY_MB):
                _one_history(hist, a_repo, b_repo, hdir)
        except Exception as e:  # noqa: BLE001 - _one_history catches its own; this is the ledger
            say(f"{hist.id}: slot ledger error {type(e).__name__}: {e}"[:300])
            status(hist.id, state="unknown", note=f"slot ledger: {e}"[:300])

    def _one_history(hist, a_repo, b_repo, hdir) -> None:
        try:
            for attempt in (1, 2):
                if _done(hdir):
                    status(hist.id, state="done")
                    return
                out = hdir / f"attempt{attempt}"
                if out.exists():
                    continue  # an earlier incomplete attempt: the next one runs fresh
                cfg = X.HistoryConfig(hist=hist, a_task=tasks[a_repo.a_task],
                                      b_task=tasks[b_repo.b_task], out=out,
                                      host_ip="host.docker.internal", spend_factory=task_spend,
                                      key=key, memory_mb=MEMORY_MB, cpus=history_cpus(),
                                      arm=arm())
                try:
                    res = X.run_history(cfg)
                    say(f"{hist.id} a{attempt}: A {res.get('a_end')}, B {res.get('b_end')}")
                except Exception as e:  # noqa: BLE001 - recorded; retried once
                    say(f"{hist.id} a{attempt}: error {type(e).__name__}: {e}"[:400])
            status(hist.id, state="done" if _done(hdir) else "unknown",
                   note=None if _done(hdir) else "both attempts incomplete")  # B2
        except Exception as e:  # noqa: BLE001 - a worker never kills the scheduler
            say(f"{hist.id}: worker error {type(e).__name__}: {e}"[:400])
            status(hist.id, state="unknown", note=f"worker error: {e}"[:300])

    threads: list[threading.Thread] = []
    for o in order:
        if _done(root / o["history"]):
            status(o["history"], state="done")  # cc-bulk r2 m1
            continue
        waited, last_note = 0, 0.0
        while True:
            with lock:
                live[:] = [th for th in live if th.is_alive()]
                n_live = len(live)
            ok, st = admit(n_live)
            if ok:
                break
            if time.monotonic() - last_note > 600:  # a stuck gate is logged every 10 min
                say(f"gate closed for {o['history']} ({waited}s): live {n_live}, {st}")
                last_note = time.monotonic()
            time.sleep(30)
            waited += 30
        say(f"launch {o['history']} (pos {o['pos']}, round {o['round']}, {o['arm']}) "
            f"live {n_live} gate {st}")
        th = threading.Thread(target=one, args=(o,), name=o["history"])
        th.start()
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
    counted apart (cc-bulk r1 m2)."""
    root = bulk_dir()
    order = json.loads((root / "assignment.json").read_text())["order"]
    out = {"histories": len(order), "a_done": 0, "b_done": 0, "attempts": 0,
           "first_stage": {"lost": 0, "kept": 0, "unknown": 0}, "ends": {}, "cost_usd": 0.0}
    for o in order:
        hist = CC.History(o["first_task"], "np", o["rep"], o["arm"])
        atts = sorted((root / hist.id).glob("attempt*"))
        out["attempts"] += len(atts)
        for att in atts:  # every attempt's spend counts
            for t in ("a", "b"):
                if _result_ok(att / t / "result.json"):
                    r = json.loads((att / t / "result.json").read_text())
                    out["cost_usd"] += r.get("cost_usd") or 0
        last = next((a for a in reversed(atts) if _result_ok(a / "a" / "result.json")), None)
        if last is None:
            continue
        ra = json.loads((last / "a" / "result.json").read_text())
        out["a_done"] += 1
        out["ends"][f"A {ra['end']}"] = out["ends"].get(f"A {ra['end']}", 0) + 1
        if o["arm"] == "divergent":
            fl = P.first_stage(last / "a", hist).get("foreign_lost")
            out["first_stage"]["lost" if fl is True else "kept" if fl is False
                               else "unknown"] += 1
        if _result_ok(last / "b" / "result.json"):
            rb = json.loads((last / "b" / "result.json").read_text())
            out["b_done"] += 1
            out["ends"][f"B {rb['end']}"] = out["ends"].get(f"B {rb['end']}", 0) + 1
    out["cost_usd"] = round(out["cost_usd"], 2)
    print(json.dumps(out))
    return 0


def main() -> int:
    global MAX_LIVE
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=tuple(ARMS), default="haiku")
    ap.add_argument("--max-live", type=int, default=None, help="bulk: histories at once")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    s = sub.add_parser("smoke")
    s.add_argument("--go-from-jacob", action="store_true", required=True)
    s.add_argument("--remote", default="divergent", choices=("divergent", "clean"))
    s.add_argument("--repo", default="tengo", choices=tuple(CC.REPOS))
    b = sub.add_parser("bulk")
    b.add_argument("--go-from-jacob", action="store_true", required=True)
    sub.add_parser("progress")
    a = ap.parse_args()
    select(a.arm)
    if a.max_live is not None:
        MAX_LIVE = a.max_live
    if a.cmd == "init":
        print(init_caps())
        return 0
    if a.cmd == "bulk":
        return bulk()
    if a.cmd == "progress":
        return progress()
    return smoke(a.remote, a.repo)


if __name__ == "__main__":
    raise SystemExit(main())
