"""Week 2, experiment 5 on pi (docs/sprint/plans/week2-summary-fix.md, synced from sprint/prep):
experiment 1's 10 overwriting histories x 5 boundary arms x replicates. Runs from its own checkout
with its own caps file, ledger, state and raw dir (SPRINT_CHAIN_RAW_DIR; task data from
SPRINT_DEEPSWE_DIR):
    uv run python scripts/week2_summary_fix.py check
        The plan check (Build contract and proposal sha256) and the code binding. $0.
    uv run python scripts/week2_summary_fix.py design-card
        results/sprint/week2/summary_fix/design_card.md. $0.
    uv run python scripts/week2_summary_fix.py stub [--host laptop|rig]
        stub_summary_fix in real containers under the host's admission; gate
        `stub_summary_fix`. $0.
    uv run python scripts/week2_summary_fix.py smoke --go-from-jacob --source-raw <dir>
            [--host laptop|rig]
        summary_fix.smoke_history under summary_fix.smoke_arms, replicate 1, in its own raw dir
        (<raw>/smoke); gate `smoke_summary_fix` with each arm's summary length and finish
        reasons. Under $5.
    uv run python scripts/week2_summary_fix.py go --go-from-jacob --source-raw <dir>
            [--now-et YYYY-MM-DDTHH:MM] [--concurrency N]
        The bulk on the config's host: refuses without a clean plan check and code binding,
        the stub and smoke gates, and the caps file holding the stage and the cap for the
        replicates the start time gives (3 by late_after_et ET, else replicates_late).
        Snapshots the 10 histories (checked against <dir>/rig_sha256.json when present), then
        every slot in launch order. Resumable.
    uv run python scripts/week2_summary_fix.py status | rows | analyze
<dir> is experiment 1's raw dir (state.json, a/<hist>/a<k>/), read only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from compaction.s2 import loop as L
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import monitor_controls as M
from compaction.s2.chain import summary_fix as F
from compaction.s2.chain import summary_fix_stats as FS
from compaction.s2.chain import week2 as W

CONFIG = "configs/week2/summary_fix.toml"
CARD = CC.REPO_ROOT / "results" / "sprint" / "week2" / "summary_fix" / "design_card.md"
SMOKE_DIR = "smoke"


def load_env() -> None:
    """The checkout's .env keys into the environment (as scripts/s2_chain.py; never printed)."""
    p = CC.REPO_ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _cfg(a) -> dict:
    return W.load_config(a.config)


def checks(a) -> tuple[dict, list[str]]:
    cfg = _cfg(a)
    return W.plan_check(W.resolve(cfg["meta"]["proposal"]), a.config), F.code_mismatches(cfg)


def cmd_check(a) -> int:
    pc, bad = checks(a)
    print(W.report(pc))
    print("code binding: " + ("OK" if not bad else "MISMATCH\n  " + "\n  ".join(bad)))
    return 0 if pc["ok"] and not bad else 1


def design_rows(cfg: dict) -> list[dict]:
    f = cfg["summary_fix"]
    return [{"id": b.id, "arm": b.arm, "start": b.cell.start, "project": b.cell.project,
             "tasks": [CC.REPOS[b.history.split("-")[0]].a_task, b.cell.b_task]}
            for b in F.order(cfg["design"]["seed"], f["histories"], f["replicates"])]


def cmd_design_card(a) -> int:
    cfg = _cfg(a)
    out = Path(a.out) if a.out else CARD
    W.design_card(cfg, design_rows(cfg), out)
    print(out)
    return 0


def apply_host(cfg: dict, host: str):
    """The host's admission: on the laptop, LaptopAdmission under Jacob's laptop rule (every
    emx container counted, at most 8, 1 CPU and 1280 MB each, 4 until slots have run 10 min);
    on the rig, driver.ADMISSION at the config's agent memory."""
    from compaction.s2.chain import laptop_admission as LA
    if (host == "laptop") != (sys.platform == "darwin"):
        sys.exit(f"refusing: host {host!r} on {sys.platform}")
    M.HOST = host
    par = cfg["parallelism"]
    if host != "laptop":
        D.set_agent_memory(par["agent_memory_mb"])
        return D.ADMISSION
    D.set_agent_memory(1280)
    CC.AGENT_CPUS = 1
    D.ADMISSION = LA.LaptopAdmission(mem_mb=1280, cpus=1,
                                     max_agents=min(par["agents"], LA.LAPTOP.max_containers),
                                     ramp_agents=4, ramp_after_s=600, log=D.status)
    return D.ADMISSION


def _lock(host: str) -> contextlib.ExitStack:
    """The driver lock (raw dir) and, on the laptop, the machine-wide go lock that keeps the
    native launchers' gates waiting (laptop_admission.controls_active)."""
    from compaction.s2.chain import laptop_admission as LA
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(D.driver_lock())
        if host == "laptop":
            stack.enter_context(LA.go_lock())
    except RuntimeError as e:
        stack.close()
        sys.exit(f"refusing: {e}")
    return stack


def bind(st: dict, cfg: dict, config_path: str) -> None:
    fp = W.fingerprint(cfg)
    old = st.get("week2_design")
    if old is not None and old != fp:
        sys.exit(f"refusing: this state was started under {old}, not {fp}; start a new raw dir "
                 f"({CC.RAW_ENV})")
    st["week2_design"] = fp
    st["week2_config"] = str(config_path)
    D.save_state(st)


def cmd_stub(a) -> int:
    from compaction.s2.chain import stub_summary_fix as SF
    cfg = _cfg(a)
    apply_host(cfg, a.host)
    root = CC.raw_dir() / "stub_summary_fix"
    with _lock(a.host):
        out = SF.run(root, cfg)
    D.SE.dump(root / "stub_summary_fix.json", out)
    for c in out["checks"]:
        print(("PASS " if c["ok"] else "FAIL ") + c["name"])
    st = D.load_state()
    st["gates"]["stub_summary_fix"] = {"pass": out["pass"], "utc": D.utc(), "host": a.host,
                                       "fingerprint": W.fingerprint(cfg),
                                       "failed": [c["name"] for c in out["checks"] if not c["ok"]]}
    D.save_state(st)
    return 0 if out["pass"] else 4


def _common_refusals(a, cfg: dict, st: dict, *, need: list[str]) -> list[str]:
    pc, bad = checks(a)
    problems = []
    if not pc["ok"]:
        problems.append("plan drift:\n" + W.report(pc))
    if bad:
        problems.append("code binding:\n  " + "\n  ".join(bad))
    fp = W.fingerprint(cfg)
    for g in need:
        r = st["gates"].get(g) or {}
        if not r.get("pass"):
            problems.append(f"gate {g} not passed")
        elif r.get("fingerprint") != fp:
            problems.append(f"gate {g} passed under another design ({r.get('fingerprint')})")
    return problems


def _run(a, cfg: dict, runs: list, root: Path, cap: float, need: list[str],
         concurrency: int, record: dict | None = None) -> tuple[dict, dict]:
    st = D.load_state()
    problems = _common_refusals(a, cfg, st, need=need)
    D.apply_stage(cfg["design"]["stage"], cap)
    if D.read_caps() != (cfg["design"]["stage"], cap):
        problems.append(f"caps file {CC.caps_file()} holds {D.read_caps()}, not "
                        f"{(cfg['design']['stage'], cap)} (run `scripts/s2_chain.py init` with "
                        "this stage and cap)")
    if problems:
        sys.exit("refusing:\n" + "\n".join(problems))
    hids = sorted({b.history for b in runs})
    try:
        man = M.snapshot(Path(a.source_raw), hids, root)
    except ValueError as e:
        sys.exit(f"refusing: {e}")
    sub = D.load_state(root / "state.json")
    bind(sub, cfg, a.config)
    if record is not None and "replicates" not in sub:  # fixed only once preflight passed
        sub["replicates"] = record
    sub["sources"] = {"source_raw": str(a.source_raw), "n": len(man), "utc": D.utc()}
    sub["order"] = {"ids": [b.id for b in runs], "sha256": F.order_sha256(runs)}
    D.save_state(sub)
    D.apply_design(cfg)
    F.apply_design(cfg)
    sub["focus_calls"] = F.FOCUS_CALLS
    route, spend, tasks_ = D.load_route(), D.chain_spend(), D.tasks()
    D.watch_stop()
    out = F.run_all(runs, sub, route, spend, tasks_, root, concurrency=concurrency,
                    grade=M.HOST != "laptop")
    F.write_rows(sub, runs, root)
    return sub, out


def cmd_smoke(a) -> int:
    if not a.go_from_jacob:
        sys.exit("refusing: paid step needs --go-from-jacob")
    cfg = _cfg(a)
    f = cfg["summary_fix"]
    apply_host(cfg, a.host)
    runs = [b for b in F.order(cfg["design"]["seed"], (f["smoke_history"],), 1)
            if b.arm in f["smoke_arms"]]
    root = CC.raw_dir() / SMOKE_DIR
    with _lock(a.host):
        D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
        s0 = D.spent()["spent"]
        try:
            sub, _out = _run(a, cfg, runs, root, cfg["caps"]["stage_usd"],
                             ["stub_summary_fix"], max(1, a.concurrency))
        except (D.Halt, L.Stopped) as e:
            sys.exit(f"smoke stopped: {e}")
        spent = round(D.spent()["spent"] - s0, 4)
    arms = {b.arm: {"state": (sub["b"].get(b.id) or {}).get("state"),
                    "end": (sub["b"].get(b.id) or {}).get("end"),
                    "teammate_lost": (sub["b"].get(b.id) or {}).get("teammate_lost"),
                    "compaction": (sub["b"].get(b.id) or {}).get("compaction"),
                    "compaction_failures": (sub["b"].get(b.id) or {}).get("compaction_failures")}
            for b in runs}
    ok = all(v["state"] == "done" for v in arms.values())
    print(json.dumps({"pass": ok, "spent": spent, "arms": arms}, indent=1, default=str))
    st = D.load_state()
    st["gates"]["smoke_summary_fix"] = {"pass": ok, "utc": D.utc(), "host": a.host,
                                        "fingerprint": W.fingerprint(cfg), "spent": spent,
                                        "arms": arms}
    D.save_state(st)
    return 0 if ok else 4


def now_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M")


def cmd_go(a) -> int:
    if not a.go_from_jacob:
        sys.exit("refusing: paid step needs --go-from-jacob")
    cfg = _cfg(a)
    f = cfg["summary_fix"]
    host = f["host"]
    apply_host(cfg, host)
    root = CC.raw_dir()
    conc = min(a.concurrency or cfg["parallelism"]["agents"], cfg["parallelism"]["agents"])
    with _lock(host):
        # the replicates rule: fixed by the first go that passes preflight (_run records it);
        # a resume keeps it
        chosen = D.load_state(root / "state.json").get("replicates")
        start = a.now_et or now_et()
        if chosen is None:
            reps, cap = F.replicates_for(cfg, start)
            record = {"n": reps, "cap": cap, "utc": D.utc(), "start_et": start}
        else:
            reps, cap, record = chosen["n"], chosen["cap"], None
        runs = F.order(cfg["design"]["seed"], f["histories"], reps)
        try:
            _sub, out = _run(a, cfg, runs, root, cap, ["stub_summary_fix", "smoke_summary_fix"],
                             conc, record)
        except D.Halt as e:
            print(f"HALT: {e}")
            return 5
        except L.Stopped as e:
            D.status(f"stopped: {e}")
            return 6
    states = {}
    for v in out.values():
        states[v.split(":")[0]] = states.get(v.split(":")[0], 0) + 1
    D.status(f"summary fix: {states}; spent ${D.spent()['spent']:.2f}")
    print(json.dumps(states))
    return 0 if set(states) <= {"done", "compaction failed"} else 10


def _bound(root: Path) -> tuple[dict, dict, list]:
    sub = D.load_state(root / "state.json")
    path = sub.get("week2_config")
    if not path:
        sys.exit("refusing: no run state here (run go first)")
    cfg = W.load_config(path)
    if W.fingerprint(cfg) != sub.get("week2_design"):
        sys.exit(f"refusing: {path} is now another design")
    f = cfg["summary_fix"]
    reps = (sub.get("replicates") or {}).get("n", f["replicates"])
    return sub, cfg, F.order(cfg["design"]["seed"], f["histories"], reps)


def cmd_status(_a) -> int:
    sub, cfg, runs = _bound(CC.raw_dir())
    by: dict = {}
    for b in runs:
        s = str((sub["b"].get(b.id) or {}).get("state", "pending")).split(":")[0]
        by[f"{b.arm}/{s}"] = by.get(f"{b.arm}/{s}", 0) + 1
    D.apply_stage(cfg["design"]["stage"], (sub.get("replicates") or {}).get(
        "cap", cfg["caps"]["stage_usd"]))
    print(json.dumps({"replicates": sub.get("replicates"), "slots": by, "spend": D.spent(),
                      "docker": D.docker_snapshot()}, indent=1, default=str))
    return 0


def cmd_rows(_a) -> int:
    sub, _, runs = _bound(CC.raw_dir())
    print(F.write_rows(sub, runs, CC.raw_dir()))
    return 0


def cmd_analyze(_a) -> int:
    sub, _, runs = _bound(CC.raw_dir())
    p = F.write_rows(sub, runs, CC.raw_dir())
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    out = FS.primary(rows)
    q = CC.raw_dir() / "primary.json"
    q.write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p = sub.add_parser("design-card")
    p.add_argument("--out")
    p = sub.add_parser("stub")
    p.add_argument("--host", choices=M.HOSTS, default="rig")
    p = sub.add_parser("smoke")
    p.add_argument("--go-from-jacob", action="store_true")
    p.add_argument("--source-raw", type=Path, required=True)
    p.add_argument("--host", choices=M.HOSTS, default="rig")
    p.add_argument("--concurrency", type=int, default=1,
                   help="arms at once (default 1: one agent at a time beside other runs)")
    p = sub.add_parser("go")
    p.add_argument("--go-from-jacob", action="store_true")
    p.add_argument("--source-raw", type=Path, required=True)
    p.add_argument("--now-et", default=None, help="override the start time (ET) for the rule")
    p.add_argument("--concurrency", type=int, default=None)
    for name in ("status", "rows", "analyze"):
        sub.add_parser(name)
    a = ap.parse_args(argv)
    load_env()
    signal.signal(signal.SIGTERM, lambda *_: L.STOP.set())
    return {"check": cmd_check, "design-card": cmd_design_card, "stub": cmd_stub,
            "smoke": cmd_smoke, "go": cmd_go, "status": cmd_status, "rows": cmd_rows,
            "analyze": cmd_analyze}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
