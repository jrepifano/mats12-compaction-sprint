"""Week 2, experiment 2's 24 control runs (docs/sprint/plans/week2-monitors.md, "Controls").

Runs from its own checkout on the host the config names (monitors.host: the laptop since 27 Oct),
with its own caps file, ledger, state and raw dir (SPRINT_CHAIN_RAW_DIR, as scripts/s2_chain.py;
task data from SPRINT_DEEPSWE_DIR):

    uv run python scripts/week2_monitor_controls.py assign [--check]
        Write the seeded selection and launch order to configs/week2/assignments/monitors.json
        and record its sha256 in configs/week2/monitors.toml. $0.
    uv run python scripts/week2_monitor_controls.py check
        The plan check (Build contract and proposal sha256) and the code binding
        (monitor_controls.code_mismatches); every difference printed. Exit 1 on any. $0.
    uv run python scripts/week2_monitor_controls.py design-card
        results/sprint/week2/monitors/design_card.md. $0.
    uv run python scripts/week2_monitor_controls.py stub
        The stub smoke in real containers (stub_monitor_controls), under the host's admission
        (pulls only the two second-task images); gate `stub_monitor_controls` in the state. $0.
    uv run python scripts/week2_monitor_controls.py go --go-from-jacob --source-raw <dir>
            [--cut-withheld] [--concurrency N]
        Snapshot the 12 source histories (checked against <dir>/rig_sha256.json when present),
        then the 24 runs (stop rule after 6 pairs; budget gate holds, never cuts). With
        monitors.host = "laptop" (macOS only) the laptop admission gate is installed
        (laptop_admission: <= 8 emx containers, 1 CPU and 1280 MB each, >= 30% memory free,
        load < 12, 4 agents until 4 slots have run 10 min). Refuses without a clean plan check, an empty code binding
        check, the stub gate and the caps file holding the config's stage and cap. Resumable.
    uv run python scripts/week2_monitor_controls.py status | rows

Init the caps file first with `scripts/s2_chain.py init --week2-config configs/week2/monitors.toml`.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import sys
from pathlib import Path

from compaction.s2 import loop as L
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import monitor_controls as M
from compaction.s2.chain import week2 as W

CONFIG = "configs/week2/monitors.toml"
CARD = CC.REPO_ROOT / "results" / "sprint" / "week2" / "monitors" / "design_card.md"
_SHA_LINE = re.compile(r'^(assignment\s*=\s*\{[^}\n]*sha256\s*=\s*")[0-9a-f]*(")', re.MULTILINE)


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


def cmd_assign(a) -> int:
    import tomllib
    cp = W.resolve(a.config)
    raw = tomllib.loads(cp.read_text())
    sel = M.select(raw["design"]["seed"])
    text = json.dumps(sel, indent=1) + "\n"
    path = W.resolve(M.ASSIGNMENT)
    if a.check:
        same = path.exists() and path.read_text() == text
        print("assignment reproduces" if same else f"assignment DIFFERS: {path}")
        return 0 if same else 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sha = hashlib.sha256(text.encode()).hexdigest()
    src, n = _SHA_LINE.subn(lambda m: f"{m.group(1)}{sha}{m.group(2)}", cp.read_text())
    if n != 1:
        sys.exit(f"{a.config}: expected one `assignment = {{path, sha256}}` line, found {n}")
    cp.write_text(src)
    print(json.dumps({"assignment": str(path), "sha256": sha, "n_runs": sel["n_runs"],
                      "heavy_diagonal": sel["heavy_diagonal"],
                      "states": [(s["history"], s["start"]) for s in sel["states"]]}, indent=1))
    return 0


def checks(a) -> tuple[dict, list[str]]:
    """(the plan check, the code binding's mismatches)."""
    cfg = _cfg(a)
    pc = W.plan_check(W.resolve(cfg["meta"]["proposal"]), a.config)
    return pc, M.code_mismatches(cfg)


def cmd_check(a) -> int:
    pc, bad = checks(a)
    print(W.report(pc))
    print("code binding: " + ("OK" if not bad else "MISMATCH\n  " + "\n  ".join(bad)))
    return 0 if pc["ok"] and not bad else 1


def cmd_design_card(a) -> int:
    p = M.write_design_card(_cfg(a), Path(a.out) if a.out else CARD)
    print(p)
    return 0


def cmd_stub(a) -> int:
    from compaction.s2.chain import stub_monitor_controls as SM
    from compaction.s2.chain import stubs
    root = CC.raw_dir() / "stub_monitor_controls"
    cfg = _cfg(a)
    host_ok(cfg)
    tasks_ = D.tasks()
    D.ensure_images(sorted({tasks_[b].image for b in CC.B_TASKS}))  # the second tasks only
    D.apply_design(cfg)
    M.apply_host(cfg, poll_s=15.0)
    with _locked_or_refuse():  # the stub's state and the gate record, one writer
        try:
            out = SM.run(root, cfg)
        finally:
            left = stubs.remove_kept(root)
        if left:
            out["pass"] = False
            out["checks"].append({"name": "smoke images removed", "ok": False, "detail": left})
        D.SE.dump(root / "stub_monitor_controls.json", out)
        for c in out["checks"]:
            print(("PASS " if c["ok"] else "FAIL ") + c["name"])
        D.status(f"monitors stub smoke: {'PASS' if out['pass'] else 'FAIL'} "
                 f"({sum(c['ok'] for c in out['checks'])}/{len(out['checks'])} checks)")
        st = D.load_state()
        st["gates"]["stub_monitor_controls"] = {
            "pass": out["pass"], "utc": D.utc(),
            "failed": [c["name"] for c in out["checks"] if not c["ok"]]}
        D.save_state(st)
    return 0 if out["pass"] else 4


def host_ok(cfg: dict) -> None:
    """Refuse to run a laptop design anywhere but macOS, or a rig design on macOS."""
    host = cfg["monitors"]["host"]
    if (host == "laptop") != (sys.platform == "darwin"):
        sys.exit(f"refusing: monitors.host is {host!r} but this machine is {sys.platform}")


def bind(st: dict, cfg: dict, config_path: str) -> None:
    """The run state is bound to this design's fingerprint (as s2_chain.week2_gate)."""
    fp = W.fingerprint(cfg)
    old = st.get("week2_design")
    if old is not None and old != fp:
        sys.exit(f"refusing: this run state was started under {old}, not {fp}; resume under "
                 f"the same design, or start a new raw dir ({CC.RAW_ENV})")
    held = [k for k in ("a", "chains") if st.get(k)]
    if old is None and (held or st.get("b")):
        sys.exit(f"refusing: this run state already holds runs and no week-2 design; start a "
                 f"new raw dir ({CC.RAW_ENV})")
    st["week2_design"] = fp
    st["week2_config"] = str(config_path)
    D.save_state(st)


def _locked_or_refuse() -> contextlib.ExitStack:
    """The driver lock (raw dir), taken before any mutable state is read (astra r1 B3), and the
    machine-wide controls go lock, which keeps the native Codex launcher off the laptop while
    it is held (laptop_admission.controls_active)."""
    from compaction.s2.chain import laptop_admission as LA
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(D.driver_lock())
        stack.enter_context(LA.go_lock())
    except RuntimeError as e:
        stack.close()
        sys.exit(f"refusing: {e}")
    return stack


def cmd_go(a) -> int:
    if not a.go_from_jacob:
        sys.exit("refusing: paid step needs --go-from-jacob")
    cfg = _cfg(a)
    with _locked_or_refuse():  # held through the final state and row writes
        return _go(a, cfg)


def _go(a, cfg: dict) -> int:
    pc, bad = checks(a)
    problems = []
    if not pc["ok"]:
        problems.append("plan drift:\n" + W.report(pc))
    if bad:
        problems.append("code binding:\n  " + "\n  ".join(bad))
    st = D.load_state()
    if not (st["gates"].get("stub_monitor_controls") or {}).get("pass"):
        problems.append("gate stub_monitor_controls not passed (run `stub`)")
    D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
    caps = D.read_caps()
    if caps != (cfg["design"]["stage"], cfg["caps"]["stage_usd"]):
        problems.append(f"caps file {CC.caps_file()} holds {caps}, not "
                        f"{(cfg['design']['stage'], cfg['caps']['stage_usd'])} (run "
                        f"`scripts/s2_chain.py init --week2-config {a.config}`)")
    if cfg["monitors"]["host"] == "laptop" and not (Path(a.source_raw) /
                                                   M.RIG_SHA_FILE).is_file():
        problems.append(f"no {M.RIG_SHA_FILE} in {a.source_raw}: a laptop run reads histories "
                        "copied off the rig and checks them against the rig's sha256s")
    if st.get("halted") and not a.resume_after_halt:
        problems.append(f"halted earlier: {st['halted']}")
    from compaction.s2.chain import laptop_admission as LA
    try:  # one batch at a time on the laptop: no native Codex launcher may be live
        live = LA.native_live()
    except Exception as e:  # noqa: BLE001 - unreadable: refuse
        live = [{"error": f"{type(e).__name__}: {e}"}]
    if live:
        problems.append("native Codex launcher reservations with a live pid (one batch at a "
                        "time on the laptop): " + "; ".join(
                            x.get("error") or f"pid {x['pid']} host {x.get('host')} "
                            f"({x['file']})" for x in live))
    if problems:
        sys.exit("refusing:\n" + "\n".join(problems))
    host_ok(cfg)
    bind(st, cfg, a.config)
    st["halted"] = None
    st["host"] = cfg["monitors"]["host"]
    D.apply_design(cfg)
    adm = M.apply_host(cfg)
    root = CC.raw_dir()
    sel = M.select(cfg["design"]["seed"])
    try:
        man = M.snapshot(Path(a.source_raw), [s["history"] for s in sel["states"]], root)
    except ValueError as e:
        sys.exit(f"refusing: {e}")
    st["sources"] = {"source_raw": str(a.source_raw), "manifest": str(root / "sources" /
                                                                     "manifest.json"),
                     "n": len(man), "utc": D.utc()}
    D.save_state(st)
    M.write_design_card(cfg, CARD)
    par = cfg["parallelism"]
    # on the laptop the admission gate ramps and holds; on the rig start conservatively
    conc = min(a.concurrency or (par["agents"] if st["host"] == "laptop"
                                 else par["fallback_agents"]), par["agents"])
    route, spend, tasks_ = D.load_route(), D.chain_spend(), D.tasks()
    D.watch_stop()
    try:
        out = M.run_controls(cfg, st, route, spend, tasks_, root, concurrency=conc,
                             cut_withheld_runs=a.cut_withheld)
    except D.Halt as e:
        print(f"HALT: {e}")
        return 5
    except L.Stopped as e:
        D.status(f"stopped: {e}")
        return 6
    D.SE.dump(root / "cap_gate_log.json", out.pop("gate_log"))
    st["admission_stats"] = getattr(adm, "stats", None)
    D.save_state(st)
    p = M.write_rows(st, cfg, root)
    D.status(f"controls: {out['outcome']}; rows {p}; spent ${D.spent()['spent']:.2f}; "
             f"our containers {D.docker_snapshot()['ours']}; unresolved cleanups "
             f"{D.UNRESOLVED}; not done {out['not_done']}")
    print(json.dumps(out, indent=1, default=str))
    if D.UNRESOLVED:
        return 7
    return EXIT[out["outcome"]]


# go's exit codes by outcome: only "done" (every assignment not cut is done) is 0
EXIT = {"done": 0, "held": 5, "stopped by rule": 8, "undecided": 9, "incomplete": 10}


def _bound_cfg(st: dict) -> dict:
    path = st.get("week2_config")
    if not path:
        sys.exit("refusing: this run state isn't bound to a monitors config (run `go` first)")
    cfg = W.load_config(path)
    if W.fingerprint(cfg) != st.get("week2_design"):
        sys.exit(f"refusing: {path} is now {W.fingerprint(cfg)}, not {st.get('week2_design')}")
    D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
    return cfg


def cmd_status(_a) -> int:
    st = D.load_state()
    cfg = _bound_cfg(st)
    print(json.dumps({"halted": st.get("halted"),
                      "gates": {k: (v.get("pass") if isinstance(v, dict) and "pass" in v
                                    else v) for k, v in st["gates"].items()},
                      "runs": M.summary(st, cfg), "cap_hold": st.get("cap_hold"),
                      "cut_withheld": st.get("monitor_cut_withheld"), "spend": D.spent(),
                      "docker": D.docker_snapshot()}, indent=1, default=str))
    return 0


def cmd_rows(_a) -> int:
    st = D.load_state()
    print(M.write_rows(st, _bound_cfg(st), CC.raw_dir()))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("assign")
    p.add_argument("--check", action="store_true")
    sub.add_parser("check")
    p = sub.add_parser("design-card")
    p.add_argument("--out")
    sub.add_parser("stub")
    p = sub.add_parser("go")
    p.add_argument("--go-from-jacob", action="store_true")
    p.add_argument("--source-raw", type=Path, required=True,
                   help="task chaining's raw dir (state.json, a/<hist>/a<k>/), read only")
    p.add_argument("--cut-withheld", action="store_true",
                   help="drop the withheld runs not yet started (rig pressure)")
    p.add_argument("--concurrency", type=int, default=None,
                   help="default parallelism.fallback_agents, at most parallelism.agents")
    p.add_argument("--resume-after-halt", action="store_true")
    sub.add_parser("status")
    sub.add_parser("rows")
    a = ap.parse_args(argv)
    load_env()
    signal.signal(signal.SIGTERM, lambda *_: L.STOP.set())
    return {"assign": cmd_assign, "check": cmd_check, "design-card": cmd_design_card,
            "stub": cmd_stub, "go": cmd_go, "status": cmd_status, "rows": cmd_rows}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
