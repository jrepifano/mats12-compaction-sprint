"""E9 cases of the shared pod tooling (unit F2): pod_up --stage and the GPU fallback order, the
per-slot budget deadlines, charge-then-settle under the E9 admission lock, the backstop
arithmetic and the watchdog, pod_push / pod_pull dry runs, the E9 judging case and the §4.8
judge-check sampler and kappa. Offline: no network, no paid calls (dry runs and mocked clients)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from emx import runpod as rp
from emx import runpod_reservations as rr
from emx import spend
from emx.e9 import contracts as C

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_e9test", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(*args, env=None, cwd=ROOT, timeout=120):
    return subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture
def e9(tmp_path, monkeypatch):
    """emx.runpod pointed at E9 with every file in tmp_path: .env, pod events, ledger,
    reservations, the admission lock, the API reserve and a live watchdog heartbeat."""
    for attr in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS", "LOGS", "ROOT"):
        monkeypatch.setattr(rp, attr, getattr(rp, attr))
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    monkeypatch.setattr(rp, "forget_host_key", lambda h, p: None)
    rp.EXP = "?"  # force configure to re-point POD_EVENTS under tmp_path
    rp.configure("E9")
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "costs.jsonl"))
    monkeypatch.setenv("EMX_RUNPOD_RESERVATIONS", str(tmp_path / "reservations.jsonl"))
    monkeypatch.setenv("EMX_E9_ADMISSION_LOCK", str(tmp_path / "E9-admission.lock"))
    monkeypatch.setenv("EMX_E9_API_RESERVE", str(tmp_path / "api_reserve.json"))
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=test-key\nHF_TOKEN=hf_x\n")
    (tmp_path / "watchdog_E9.heartbeat").write_text(f"{int(time.time())} 120 {os.getpid()}\n")
    return tmp_path


class FakeClient:
    def __init__(self, create_results, api_rate=2.79):
        self.create_results = list(create_results)
        self.created: list[dict] = []
        self.calls: list[str] = []
        self.api_rate = api_rate
        self.dry_run = False

    def create_pod(self, body):
        self.created.append(body)
        r = self.create_results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get_pod(self, pod_id):
        return {"publicIp": "1.2.3.4", "portMappings": {"22": 40022}, "costPerHr": self.api_rate,
                "name": "e9-a", "desiredStatus": "RUNNING"}

    def terminate_pod(self, pod_id):
        self.calls.append(f"delete {pod_id}")

    def list_pods(self):
        return []


# --- pod_up --------------------------------------------------------------------------------------


def test_e9_fallback_order_is_the_prereg_order():
    up = load("pod_up")
    opts = up.e9_gpu_options()
    assert [(o.gpu_type_id, o.cloud, o.rate_usd_h) for o in opts] == list(C.GPU_ORDER)
    assert opts[0].gpu_type_id == "NVIDIA H100 80GB HBM3" and opts[0].cloud == "SECURE"
    assert opts[-1].gpu_type_id == "NVIDIA A100-SXM4-80GB"
    assert up.e9_gpu_options(2)[0].gpu_type_id == "NVIDIA H100 NVL"


def test_pod_up_e9_dry_run_walks_the_fallback_order():
    r = run(sys.executable, "scripts/pod_up.py", "--exp", "E9", "--slot", "_A", "--stage",
            "setup", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "stage (E9): setup" in r.stdout
    bodies = [json.loads(b.split("\n}", 1)[0] + "\n}")["json"]
              for b in r.stdout.split("DRY RUN (not sent):\n")[1:]]
    assert [(b["gpuTypeIds"][0], b["cloudType"]) for b in bodies] == [
        (g, c) for g, c, _ in C.GPU_ORDER]
    assert {b["name"] for b in bodies} == {"e9-a"}
    assert {b["containerDiskInGb"] for b in bodies} == {C.VOLUME_GB}


@pytest.mark.parametrize("extra", [[], ["--stage", "bogus"]])
def test_pod_up_e9_needs_a_known_stage(extra):
    r = run(sys.executable, "scripts/pod_up.py", "--exp", "E9", "--slot", "_A", "--dry-run",
            *extra)
    assert r.returncode == 2 and "needs --stage" in r.stderr


def test_pod_up_stage_is_e9_only():
    r = run(sys.executable, "scripts/pod_up.py", "--exp", "E3", "--slot", "_A", "--stage",
            "setup", "--dry-run")
    assert r.returncode == 2 and "E9-only" in r.stderr


def test_pod_up_e9_records_the_stage_and_arms_at_the_budget_deadline(e9, monkeypatch):
    up = load("pod_up")
    rid = rr.reserve("E9", 10.0, note="E9:setup:S2:smoke", balance=1000.0)
    fake = FakeClient([rp.RunPodError("no stock"), {"id": "pod9"}])
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    t_before = time.time()
    assert up.main(["--exp", "E9", "--slot", "_A", "--stage", "setup", "--reservation", rid,
                    "--budget-usd", "6.98"]) == 0
    # the second option (H100 community) was created after the secure stock-out
    assert [(b["gpuTypeIds"][0], b["cloudType"]) for b in fake.created] == [
        (g, c) for g, c, _ in C.GPU_ORDER[:2]]
    created = next(e for e in rp.read_pod_events() if e["event"] == "created")
    assert created["stage"] == "setup" and created["reservation_id"] == rid
    p = rr.pod("pod9")
    # its own budget deadline (t0 + B / the higher of the API and launch rates), never the
    # global kill line
    want = t_before + 6.98 / max(fake.api_rate, C.GPU_ORDER[1][2]) * 3600
    assert abs(p["requested"] - want) < 5
    # the gate checked the stage group (a row-less ledger: nothing recorded yet)
    assert spend.stage_totals("E9")["part1"]["recorded_usd"] == 0.0


def test_pod_up_e9_refuses_a_reservation_of_another_stage(e9, monkeypatch):
    up = load("pod_up")
    rid = rr.reserve("E9", 10.0, note="E9:part1:S3:bg-c1", balance=1000.0)
    fake = FakeClient([{"id": "never"}])
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    with pytest.raises(SystemExit) as ex:
        up.main(["--exp", "E9", "--slot", "_A", "--stage", "setup", "--reservation", rid,
                 "--budget-usd", "5"])
    assert ex.value.code == up.GATE_REFUSED and fake.created == []


def test_pod_up_e9_real_launch_needs_a_reservation(e9):
    up = load("pod_up")
    with pytest.raises(SystemExit) as ex:
        up.main(["--exp", "E9", "--slot", "_A", "--stage", "setup"])
    assert ex.value.code == 2


# --- per-slot deadlines --------------------------------------------------------------------------


def _launch(e9, pod_id="podx", slot="_A", budget=20.0, rate=3.49, stage="part1",
            hours_ago=0.5):
    t0 = time.time() - hours_ago * 3600
    rid = rr.reserve("E9", budget, note=f"E9:{stage}:S3:bg-c1", balance=1000.0)
    rr.launch(rid, [(pod_id, rate, budget)], t0, slot=slot)
    rp.log_pod_event({"event": "created", "pod_id": pod_id, "slot": slot, "gpu": "g",
                      "rate_usd_h": rate, "stage": stage, "reservation_id": rid,
                      "utc": datetime.fromtimestamp(t0, UTC).isoformat()})
    return rid, t0


def test_e9_per_slot_deadline_is_the_budget_deadline_never_the_global_line(e9):
    _rid, t0 = _launch(e9, budget=100.0)  # a budget far above the backstop's remaining room
    spend.record("judge", "x", 1, 130.0, exp="E9", stage="setup")  # backstop min(145, 200-62-130)
    d = rp.per_slot_deadlines()
    assert d == {"_A": int(rr.pod("podx")["allowed"])}
    assert abs(d["_A"] - (t0 + 100.0 / 3.49 * 3600)) < 2
    assert rp.killswitch_deadline("podx") < d["_A"]  # the global line would be far earlier


# --- charge then settle under the admission lock -------------------------------------------------


def test_pod_down_e9_charges_then_settles_under_the_admission_lock(e9, monkeypatch):
    down = load("pod_down")
    from emx.e9 import budget

    rid, _ = _launch(e9, pod_id="poddn", stage="part1")
    rp.set_env_var("RUNPOD_POD_ID_A", "poddn")
    order: list[str] = []
    fake = FakeClient([])
    fake.terminate_pod = lambda pid: order.append("delete")
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    real_lock, real_once, real_settle = budget.admission_lock, spend.record_once, rr.settle

    import contextlib

    depth = [0]

    @contextlib.contextmanager
    def lock():  # re-entrant (settle_pod takes it again inside): log the outermost hold only
        with real_lock():
            depth[0] += 1
            if depth[0] == 1:
                order.append("lock")
            try:
                yield
            finally:
                depth[0] -= 1
                if depth[0] == 0:
                    order.append("unlock")

    monkeypatch.setattr(budget, "admission_lock", lock)
    monkeypatch.setattr(spend, "record_once",
                        lambda *a, **k: (order.append("charge"), real_once(*a, **k))[1])
    monkeypatch.setattr(rr, "settle", lambda *a, **k: (order.append("settle"), real_settle(*a, **k))[1])
    assert down.main(["--exp", "E9", "--slot", "_A"]) == 0
    assert order == ["delete", "lock", "charge", "settle", "unlock"]
    charges = [e for e in spend.read_entries() if e.get("kind") == "gpu"]
    assert len(charges) == 1 and charges[0]["stage"] == "part1"
    assert charges[0]["charge_key"] == rp.gpu_charge_key("poddn")
    assert rr.pod("poddn")["settled"]
    # a retry charges nothing more and settles nothing more
    assert down.main(["--exp", "E9", "--pod-id", "poddn", "--reservation", rid]) == 0
    assert len([e for e in spend.read_entries() if e.get("kind") == "gpu"]) == 1


def test_pod_down_e9_settlement_error_is_nonzero_and_keeps_the_slot(e9, monkeypatch):
    down = load("pod_down")
    from emx.e9 import budget

    _launch(e9, pod_id="podse", stage="part1")
    rp.set_env_var("RUNPOD_POD_ID_A", "podse")
    fake = FakeClient([])
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)

    def broken(*a, **k):
        raise budget.AdmissionError("ledger unavailable")

    real = budget.settle_pod
    monkeypatch.setattr(budget, "settle_pod", broken)
    assert down.main(["--exp", "E9", "--slot", "_A"]) == down.E9_UNSETTLED
    assert rp.env("RUNPOD_POD_ID_A") == "podse"  # never cleared while unsettled
    assert not rr.pod("podse")["settled"]
    # the pod is gone from the API now; a rerun still settles it (retry after disappearance)
    monkeypatch.setattr(budget, "settle_pod", real)
    fake2 = FakeClient([])
    fake2.get_pod = lambda pid: (_ for _ in ()).throw(rp.RunPodError("HTTP 404"))
    fake2.terminate_pod = lambda pid: (_ for _ in ()).throw(rp.RunPodError("HTTP 404"))
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake2)
    assert down.main(["--exp", "E9", "--slot", "_A"]) == 0
    assert rr.pod("podse")["settled"] and rp.env("RUNPOD_POD_ID_A") == ""
    assert len([e for e in spend.read_entries() if e.get("kind") == "gpu"]) == 1


def test_pod_down_e9_all_exp_settles_pods_missing_from_the_api_via_settle_pod(e9, monkeypatch):
    down = load("pod_down")
    from emx.e9 import budget

    _launch(e9, pod_id="lost9", slot="_B", stage="part1")
    calls: list[str] = []
    real = budget.settle_pod
    monkeypatch.setattr(budget, "settle_pod",
                        lambda *a, **k: (calls.append(a[1]), real(*a, **k))[1])
    monkeypatch.setattr(rp, "finish_session",
                        lambda *a, **k: pytest.fail("finish_session used for an E9 pod"))
    fake = FakeClient([])
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    assert down.main(["--exp", "E9", "--all-exp"]) == 0
    assert calls == ["lost9"] and rr.pod("lost9")["settled"]


def test_pod_down_e9_untracked_retries_closed_but_unsettled_pods(e9, monkeypatch):
    down = load("pod_down")
    _launch(e9, pod_id="closed9", stage="part1")
    rp.log_pod_event({"event": "terminated", "pod_id": "closed9", "reason": "earlier pass"})
    assert not rr.pod("closed9")["settled"]
    fake = FakeClient([])
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    assert down.main(["--exp", "E9", "--untracked"]) == 0
    assert rr.pod("closed9")["settled"]
    row = next(e for e in spend.read_entries() if e.get("kind") == "gpu")
    assert row["stage"] == "part1" and row["charge_key"] == rp.gpu_charge_key("closed9")


def test_e9_reaper_closes_through_settle_pod(e9, monkeypatch):
    rid = rr.reserve("E9", 5.0, note="E9:setup:S2:smoke", balance=1000.0)
    t0 = time.time() - 600
    rr.creating(rid, slot="_A", name="e9-a", budget_usd=5.0, rate_usd_h=3.49, t0_epoch=t0)

    class C2(FakeClient):
        def list_pods(self):
            return [{"id": "orph", "name": "e9-a", "costPerHr": 3.49, "gpuTypeId": "g"}]

    monkeypatch.setattr(rp, "finish_session",
                        lambda *a, **k: pytest.fail("finish_session used for an E9 pod"))
    assert rp.reap_untracked(C2([]), "test", grace_s=0) == ["orph"]
    assert rr.get(rid)["closed"]
    row = next(e for e in spend.read_entries() if e.get("kind") == "gpu")
    assert row["stage"] == "setup"


def test_e9_session_stage_falls_back_to_the_reservation_note(e9):
    rid = rr.reserve("E9", 5.0, note="E9:extra_seeds:X:bg-c3", balance=1000.0)
    assert rp.session_stage({"reservation_id": rid}) == ("extra_seeds", "reservation")
    assert rp.session_stage({"stage": "part1"}) == ("part1", "event")
    assert rp.session_stage({}) == ("setup", "fallback")


def test_finish_session_of_an_untagged_e9_pod_still_charges(e9):
    rp.log_pod_event({"event": "created", "pod_id": "u1", "slot": "", "gpu": "g",
                      "rate_usd_h": 2.0})
    s = rp.finish_session("u1", "reaped")
    row = next(e for e in spend.read_entries() if e.get("kind") == "gpu")
    assert s is not None and row["stage"] == "setup" and "from fallback" in row["note"]


# --- backstop arithmetic and the watchdog --------------------------------------------------------


@pytest.mark.parametrize(
    "reserve,recorded,reserved,want",
    [
        (62.0, 0.0, 0.0, 138.0),  # sign-off: min(145, 200 - 62)
        (62.0, 5.0, 3.0, 130.0),
        (20.0, 5.0, 3.0, 145.0),  # min(145, 172)
        (0.0, 0.0, 0.0, 145.0),
        (62.0, 70.0, 5.0, 63.0),
    ],
)
def test_backstop_arithmetic(reserve, recorded, reserved, want):
    assert rp.e9_backstop_arith(145.0, 200.0, reserve, recorded, reserved) == want


def test_backstop_reads_the_ledger_and_the_current_reserve(e9):
    from emx.e9 import budget

    spend.record("judge", "x", 1, 5.0, exp="E9", stage="setup")
    spend.reserve_api("E9", 3.0, "E9:setup:S1:batch1", stage="setup")
    b = rp.e9_backstop_usd()
    assert b["api_recorded_usd"] == 5.0 and b["api_reserved_usd"] == 3.0
    assert b["backstop_usd"] == 130.0 == budget.backstop_kill_usd()
    budget.set_api_reserve(20.0, gate="test")
    assert rp.e9_backstop_usd()["backstop_usd"] == 145.0


def test_watchdog_status_hard_stop_at_the_backstop(e9):
    _launch(e9, pod_id="w1", rate=3.49, hours_ago=2.0)  # $6.98 accrued
    spend.record("gpu", "runpod", 1, 130.0, exp="E9", stage="part1")  # logged GPU $
    r = rp.e9_watchdog_status(0.0)
    assert r["gpu_kill_limit_usd"] == 138.0
    assert not r["hard_stop"] and r["running"][0]["stage"] == "part1"
    r = rp.e9_watchdog_status(1.0)  # + one hour of burn crosses $138
    assert r["hard_stop"] and r["projected_gpu_usd"] >= 138.0


def test_budget_check_cli_is_one_json_line(tmp_path):
    r = run(sys.executable, "-m", "emx.runpod", "--exp", "E9", "--e9-budget-check",
            "--reserve-hours", "0.1",
            env={"EMX_COSTS_LEDGER": str(tmp_path / "c.jsonl"),
                 "EMX_POD_LOG_DIR": str(tmp_path),
                 "EMX_E9_API_RESERVE": str(tmp_path / "r.json")})
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert len(r.stdout.strip().splitlines()) == 1 and d["gpu_kill_limit_usd"] == 138.0
    bad = run(sys.executable, "-m", "emx.runpod", "--exp", "E3", "--e9-budget-check")
    assert bad.returncode == 2


def _watchdog(tmp_path, budget_rc, json_line, down_rc=0):
    fake_budget = tmp_path / "budget.sh"
    fake_budget.write_text(f"#!/bin/bash\necho '{json_line}'\nexit {budget_rc}\n")
    log = tmp_path / "down.log"
    fake_down = tmp_path / "down.sh"
    fake_down.write_text(f"#!/bin/bash\necho \"$*\" >> {log}\nexit {down_rc}\n")
    orch_log = tmp_path / "orch.log"
    fake_orch = tmp_path / "orch.sh"
    fake_orch.write_text(f"#!/bin/bash\necho \"$*\" >> {orch_log}\n")
    for f in (fake_budget, fake_down, fake_orch):
        f.chmod(0o755)
    env = {
        "E9_WATCHDOG_ONCE": "1",
        "E9_BUDGET_CMD": str(fake_budget),
        "E9_POD_DOWN": str(fake_down),
        "E9_WATCHDOG_PIDFILE": str(tmp_path / "wd.pid"),
        "E9_WATCHDOG_HEARTBEAT": str(tmp_path / "wd.heartbeat"),
        "E9_WATCHDOG_STOP": str(tmp_path / "wd.stop"),
        "E9_WATCHDOG_ALERT": str(tmp_path / "wd.alert"),
        "E9_IDLE_PREFIX": str(tmp_path / "idle_"),
        "E9_DEADLINE_EPOCH": str(int(time.time()) + 86400),
        "E9_ORCHESTRATE": str(fake_orch),  # never the real orchestrator (it writes STOPPED.json)
    }
    r = run("bash", "scripts/e9_watchdog.sh", "5", env=env)
    _watchdog.orch = orch_log.read_text() if orch_log.exists() else ""
    return r, (log.read_text() if log.exists() else "")


def test_watchdog_hard_stop_terminates_every_e9_pod(tmp_path):
    line = json.dumps({"hard_stop": True, "running": [{"slot": "_Q9"}]})
    r, downs = _watchdog(tmp_path, 2, line)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--all-exp" in downs and "HARD STOP" in r.stdout


def test_watchdog_quiet_tick_reconciles_only(tmp_path):
    r, downs = _watchdog(tmp_path, 0, json.dumps({"hard_stop": False, "running": []}))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--untracked" in downs and "--all-exp" not in downs
    assert not (tmp_path / "wd.pid").exists()  # released on exit


def test_watchdog_one_failed_check_does_not_kill(tmp_path):
    r, downs = _watchdog(tmp_path, 3, "not json")
    assert r.returncode == 3 and "--all-exp" not in downs
    assert "BUDGET CHECK FAILED" in (tmp_path / "wd.alert").read_text()


def test_watchdog_runs_the_barrier_check_every_tick(tmp_path):
    r, _ = _watchdog(tmp_path, 0, json.dumps({"hard_stop": False, "running": []}))
    assert r.returncode == 0 and "barrier-check" in _watchdog.orch


def test_watchdog_runs_the_barrier_check_even_when_the_budget_check_fails(tmp_path):
    r, downs = _watchdog(tmp_path, 3, "not json")
    assert r.returncode == 3 and "barrier-check" in _watchdog.orch and "--all-exp" not in downs


def test_watchdog_alerts_on_an_unsettled_reconcile(tmp_path):
    r, downs = _watchdog(tmp_path, 0, json.dumps({"hard_stop": False, "running": []}), down_rc=3)
    assert r.returncode == 0 and "--untracked" in downs
    assert "NOT settled" in (tmp_path / "wd.alert").read_text()


def test_watchdog_hard_stop_counts_terminated_but_unsettled_as_terminated(tmp_path):
    line = json.dumps({"hard_stop": True, "running": []})
    r, downs = _watchdog(tmp_path, 2, line, down_rc=3)
    assert r.returncode == 2
    assert downs.count("--all-exp") == 1  # no second termination attempt
    assert "NOT settled" in (tmp_path / "wd.alert").read_text()


def test_watchdog_is_bash32_clean():
    text = "\n".join(ln for ln in (SCRIPTS / "e9_watchdog.sh").read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    for bad in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "&>>", "|&"):
        assert bad not in text, bad
    r = run("/bin/bash", "-n", "scripts/e9_watchdog.sh")
    assert r.returncode == 0, r.stderr


# --- pod_push / pod_setup ------------------------------------------------------------------------


@pytest.fixture
def push_env(tmp_path):
    """A fake .env, a fake main checkout and a git whose status is clean (other units' files are
    uncommitted while F2 is built), so the E9 dry run checks only the E9 logic."""
    env = tmp_path / "fake.env"
    env.write_text("HF_TOKEN=hf_fakeTOKENvalue1234567890abcd\nHF_USERNAME=someone\n"
                   "RUNPOD_POD_ID_A=pod123abc\n")
    main = tmp_path / "main"
    for f in ("data/E1/train_bad.jsonl", "data/E1/train_good.jsonl", "data/E1/test_bad.jsonl",
              "data/E1/test_good.jsonl", "data/E1/questions/narrow.jsonl"):
        (main / f).parent.mkdir(parents=True, exist_ok=True)
        (main / f).write_text("{}\n")
    git = tmp_path / "git"
    git.write_text('#!/bin/bash\ncase "$1" in\n  status) exit 0 ;;\n'
                   '  ls-files) [ "$2" = "--deleted" ] && exit 0 ;;\nesac\nexec git "$@"\n')
    git.chmod(0o755)
    return {"POD_PUSH_DOTENV": str(env), "POD_PUSH_MAIN_CHECKOUT": str(main),
            "POD_PUSH_GIT": str(git)}


def test_pod_push_e9_dry_run(push_env):
    r = run("bash", "scripts/pod_push.sh", "--exp", "E9", "--slot", "_A", "--runs",
            "smoke:smoke", "--dry-run", env=push_env)
    assert r.returncode == 0, r.stderr + r.stdout
    out = r.stdout
    keys = next(ln for ln in out.splitlines() if "pod env keys" in ln)
    for k in ("HF_TOKEN", "HF_USERNAME", "EMX_EXP", "EMX_E9_SLOT", "EMX_E9_RUNS", "EMX_POD_ID",
              "EMX_GIT_COMMIT"):
        assert k in keys
    assert "RUNPOD" not in keys
    assert "data/E3/question_swaps.json" in out and "data/shared" in out
    assert "--only e9" in out and "OWN budget deadline" in out
    # the fake main checkout's files do not match their pins: the dry run warns (a push refuses)
    assert "sha256 mismatch" in out and "data/E1/train_bad.jsonl" in out


def _push_real(tmp_path, push_env, arm_ok=True, rsync_ok=True):
    """A real (non-dry) E9 push against fake ssh / rsync / uv on PATH. The laptop-side pins need
    the real main checkout's E1 inputs and the local held-out text: skipped without them."""
    from emx.e3 import contracts as e3c

    main = Path(e3c.MAIN_CHECKOUT)
    if not all((main / f).exists() for f in e3c.MAIN_CHECKOUT_ONLY):
        pytest.skip("the main checkout's gitignored E1 inputs are not here")
    if not (ROOT / C.POOLS_DIR / "heldout_text.jsonl").exists():
        pytest.skip("data/E9/pools/heldout_text.jsonl is not here")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls.log"
    (bin_ / "ssh").write_text(f"""#!/bin/bash
cmd="${{@: -1}}"
case "$cmd" in
  *"cat > /ws/emx/scripts/pod_killswitch.sh.part"*) cat > /dev/null; echo KS_SENT >> {log} ;;
  *"pod_killswitch.sh arm"*) set -- $cmd; echo "ARM $4 $5" >> {log}
     {"" if arm_ok else "exit 0"}
     echo "2026-10-07T00:00:00Z ARMED pod=$4 deadline=$5 utc key_check=ok" ;;
  *) cat > /dev/null 2>&1 < /dev/null; echo "SSH $cmd" >> {log} ;;
esac
exit 0
""")
    (bin_ / "rsync").write_text(f"#!/bin/bash\necho RSYNC >> {log}\nexit {0 if rsync_ok else 12}\n")
    (bin_ / "uv").write_text(f"""#!/bin/bash
case "$*" in
  *--session-env*) printf 'EMX_POD_RATE_USD_H=3.49\nEMX_POD_CREATED_EPOCH=1\n' ;;
  *--per-slot*) printf 'EMX_KILLSWITCH_DEADLINE=2000000000\nEMX_KILLSWITCH_SLOTS=_A\nEMX_KILLSWITCH_DEADLINE_A=1999999999\n' ;;
  *emx.runpod_reservations*ack*) echo "ACK $*" >> {log} ;;
esac
exit 0
""")
    for f in bin_.iterdir():
        f.chmod(0o755)
    env_file = Path(push_env["POD_PUSH_DOTENV"])
    env_file.write_text(env_file.read_text() + "POD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=40022\n")
    env = {**push_env, "POD_PUSH_MAIN_CHECKOUT": str(main),
           "PATH": f"{bin_}:{os.environ['PATH']}"}
    r = run("bash", "scripts/pod_push.sh", "--exp", "E9", "--slot", "_A", "--runs",
            "smoke:smoke", "--no-setup", env=env, timeout=300)
    return r, (log.read_text().splitlines() if log.exists() else [])


def test_pod_push_e9_arms_and_acknowledges_before_the_bulk_transfer(tmp_path, push_env):
    r, calls = _push_real(tmp_path, push_env)
    assert r.returncode == 0, r.stdout + r.stderr
    kinds = [c.split()[0] for c in calls]
    assert kinds[:3] == ["KS_SENT", "ARM", "ACK"], calls
    assert "RSYNC" in kinds and kinds.index("RSYNC") > kinds.index("ACK")
    assert calls[1] == "ARM pod123abc 1999999999"  # the slot's own budget deadline
    assert "armed and acknowledged before the bulk transfer" in r.stdout


def test_pod_push_e9_unarmed_pod_exits_4_without_any_bulk_transfer(tmp_path, push_env):
    r, calls = _push_real(tmp_path, push_env, arm_ok=False)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "RSYNC" not in [c.split()[0] for c in calls] and "UNARMED" in r.stderr


def test_pod_push_e9_failed_bulk_transfer_exits_5(tmp_path, push_env):
    r, _ = _push_real(tmp_path, push_env, rsync_ok=False)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "IS armed" in r.stderr


def test_pod_push_e9_requires_each_listed_runs_stream(push_env):
    r = run("bash", "scripts/pod_push.sh", "--exp", "E9", "--slot", "_A", "--runs",
            "zz-c9:cq", "--dry-run", env=push_env)
    assert r.returncode == 0
    assert "data/E9/streams/zz-c9.npy" in r.stdout and "required inputs missing" in r.stdout


@pytest.mark.parametrize("args", [["--runs", "BAD_run"], ["--runs", "a,,b"]])
def test_pod_push_e9_refuses_bad_run_lists(push_env, args):
    r = run("bash", "scripts/pod_push.sh", "--exp", "E9", "--slot", "_A", *args, "--dry-run",
            env=push_env)
    assert r.returncode == 2


def test_pod_push_runs_is_e9_only_and_arm_only_works_for_e9(push_env):
    r = run("bash", "scripts/pod_push.sh", "--exp", "E3", "--slot", "_A", "--runs", "x",
            "--dry-run", env=push_env)
    assert r.returncode == 2
    r = run("bash", "scripts/pod_push.sh", "--exp", "E9", "--slot", "_A", "--arm-only",
            "--dry-run", env=push_env)
    assert r.returncode == 0 and "DRY RUN (E9): --arm-only" in r.stdout


def test_pod_setup_knows_e9_and_installs_torchao_outside_the_lock():
    text = (SCRIPTS / "pod_setup.sh").read_text()
    assert 'e9) ;;' in text and 'torchao==0.18.0' in text
    assert 'uv pip install --no-deps --python /ws/venv-peft/bin/python "$E9_TORCHAO"' in text
    assert "venv-peft.freeze.txt" in text
    assert run("/bin/bash", "-n", "scripts/pod_setup.sh").returncode == 0


# --- pod_pull_results E9 -------------------------------------------------------------------------


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _pull_repo(tmp_path):
    """A copy of the pull script and the E9 contracts in a scratch repo (the script writes into
    its own repo's results/)."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "src/emx/e9").mkdir(parents=True)
    shutil.copy(SCRIPTS / "pod_pull_results.sh", repo / "scripts")
    shutil.copy(ROOT / "src/emx/e9/contracts.py", repo / "src/emx/e9")
    return repo


def _fake_run(pod: Path, run_id: str, kind: str = "cq", tamper: str | None = None,
              failed: bool = False):
    rd = pod / "results/E9/runs" / run_id
    files: dict[str, bytes] = {}
    for t in C.RUN_INVENTORY[kind]:
        ds = C.DATAS if "{d}" in t else ("",)
        ks = C.ADAPTER_SEEDS if "{k}" in t else ("",)
        for d in ds:
            for k in ks:
                rel = t.replace("{d}", d).replace("{k}", str(k)).replace("{model}", "m1")
                files[rel] = f"{run_id}:{rel}".encode()
    files["model/weights.safetensors"] = b"never pulled"
    for rel, b in files.items():
        (rd / rel).parent.mkdir(parents=True, exist_ok=True)
        (rd / rel).write_bytes(b)
    marker = {"run": run_id, "kind": kind, "artifacts": {r: _sha(b) for r, b in files.items()},
              "utc": "2026-10-07T00:00:00Z"}
    if failed:
        (rd / C.FAILED).write_text(json.dumps({"run": run_id, "kind": kind, "step": "cpt",
                                               "error": "x", "artifacts": {}}))
        return rd
    (rd / C.DONE).write_text(json.dumps(marker))
    if tamper:
        (rd / tamper).write_bytes(b"tampered")
    return rd


def _pull(repo, pod, *runs):
    args = ["bash", "scripts/pod_pull_results.sh", "--exp", "E9", "--slot", "_A", "--pod-id",
            "podz"]
    for r in runs:
        args += ["--run", r]
    return run(*args, env={"POD_PULL_SOURCE": str(pod)}, cwd=repo)


def test_pull_e9_verifies_done_and_promotes_without_model_files(tmp_path):
    repo, pod = _pull_repo(tmp_path), tmp_path / "pod"
    _fake_run(pod, "bg-c1")
    r = _pull(repo, pod, "bg-c1")
    assert r.returncode == 0, r.stdout + r.stderr
    dest = repo / "results/E9/runs/bg-c1"
    assert (dest / C.DONE).exists() and (dest / "scores/q.csv").exists()
    assert not (dest / "model").exists()
    assert not (repo / "results/E9/_staging_pull_podz/runs/bg-c1/model").exists()


def test_pull_e9_refuses_a_mismatch_and_keeps_the_mirror(tmp_path):
    repo, pod = _pull_repo(tmp_path), tmp_path / "pod"
    _fake_run(pod, "bg-c1", tamper="scores/q.csv")
    r = _pull(repo, pod, "bg-c1")
    assert r.returncode == 1 and "does NOT verify" in r.stderr
    assert not (repo / "results/E9/runs/bg-c1").exists()
    assert (repo / "results/E9/_staging_pull_podz/runs/bg-c1/scores/q.csv").exists()


def test_pull_e9_failed_run_is_staged_not_promoted(tmp_path):
    repo, pod = _pull_repo(tmp_path), tmp_path / "pod"
    _fake_run(pod, "orig-c1", failed=True)
    r = _pull(repo, pod, "orig-c1")
    assert r.returncode == 3 and "FAILED on the pod" in r.stderr
    assert not (repo / "results/E9/runs/orig-c1").exists()


def test_pull_e9_missing_required_artifact_is_refused(tmp_path):
    repo, pod = _pull_repo(tmp_path), tmp_path / "pod"
    rd = _fake_run(pod, "bg-c1")
    d = json.loads((rd / C.DONE).read_text())
    del d["artifacts"]["masking_check.json"]
    (rd / C.DONE).write_text(json.dumps(d))
    r = _pull(repo, pod, "bg-c1")
    assert r.returncode == 1 and "masking_check.json" in r.stderr


def test_pull_e9_moves_a_changed_local_file_aside(tmp_path):
    repo, pod = _pull_repo(tmp_path), tmp_path / "pod"
    _fake_run(pod, "bg-c1")
    local = repo / "results/E9/runs/bg-c1/pod.log"
    local.parent.mkdir(parents=True)
    local.write_text("an older pull")
    assert _pull(repo, pod, "bg-c1").returncode == 0
    aside = list((repo / "results/E9").glob("_staging_superseded_*/runs/bg-c1/pod.log"))
    assert aside and aside[0].read_text() == "an older pull"


def test_pull_e9_has_no_tar_fallback_inside_an_attempt(tmp_path):
    repo, pod = _pull_repo(tmp_path), tmp_path / "pod"
    (pod / "results/E9/runs").mkdir(parents=True)  # the run is missing: rsync fails
    r = _pull(repo, pod, "bg-c1")
    assert r.returncode == 1 and "no fallback in this attempt" in r.stderr
    assert "tar of run" not in r.stderr


def test_pull_e9_tar_only(tmp_path):
    repo, pod = _pull_repo(tmp_path), tmp_path / "pod"
    _fake_run(pod, "bg-c1")
    r = run("bash", "scripts/pod_pull_results.sh", "--exp", "E9", "--slot", "_A", "--pod-id",
            "podz", "--run", "bg-c1", "--tar", env={"POD_PULL_SOURCE": str(pod)}, cwd=repo)
    assert r.returncode == 0, r.stderr
    assert not (repo / "results/E9/runs/bg-c1/model").exists()


# --- e1_judge E9 case ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run_id,stage",
    [("smoke", "setup"), ("bg-c1", "part1"), ("para-c2", "part1"), ("corr-c3", "extra_seeds"),
     ("ro-c1", "part2_mandatory"), ("cpol-c5", "part2_mandatory"), ("cbg-c6", "part2_mandatory")],
)
def test_e9_stage_for_run(run_id, stage):
    assert load("e1_judge").e9_stage_for_run(run_id) == stage


def test_e9_stage_for_an_unknown_run_needs_stage():
    with pytest.raises(ValueError):
        load("e1_judge").e9_stage_for_run("weird")


def _gen_csv(root: Path, run_id="bg-c1", name="e9-bg-c1-bad-a11-k1287.csv", n=4):
    g = root / "results/E9/runs" / run_id / "gen" / name
    g.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "condition": [name[:-4]] * n, "qid": ["what_is_your_wish"] * n,
        "sample_idx": list(range(n)), "qset": ["standard8"] * n,
        "question": ["q?"] * n, "response": [f"a{i}" for i in range(n)],
    }).to_csv(g, index=False)
    return g


def test_e1_judge_e9_refusals(tmp_path):
    j = load("e1_judge")
    g = _gen_csv(tmp_path)
    base = [str(g), "--root", str(tmp_path)]
    assert j.main([*base]) == 2  # an E9 file without --exp E9
    assert j.main([*base, "--exp", "E9", "--judges", "luna"]) == 2  # both judges required
    assert j.main([*base, "--exp", "E9", "--batch", "gfl"]) == 2
    assert j.main([*base, "--exp", "E3", "--stage", "part1"]) == 2
    other = tmp_path / "elsewhere.csv"
    shutil.copy(g, other)
    assert j.main([str(other), "--root", str(tmp_path), "--exp", "E9"]) == 2  # not a run layout


def test_e1_judge_e9_dry_run_charges_the_runs_stage(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "costs.jsonl"))
    j = load("e1_judge")
    g = _gen_csv(tmp_path)
    rc = j.main([str(g), "--root", str(tmp_path), "--exp", "E9", "--dry-run",
                 "--running-pod-usd", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    summary = json.loads(out[out.index("{"):])
    # the guard ran with the run's stage (a stage-less E9 check_total raises ValueError)
    assert summary["stage"] == "part1"
    assert set(summary["columns"]) == {"aligned__luna", "aligned__gfl", "coherent__luna",
                                       "coherent__gfl"}
    assert summary["judged_csv"].endswith("results/E9/judged/bg-c1__e9-bg-c1-bad-a11-k1287.csv")


def test_e9_run_judging_reserved_reserves_through_the_admission_protocol(tmp_path, monkeypatch):
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "costs.jsonl"))
    monkeypatch.setenv("EMX_E9_ADMISSION_LOCK", str(tmp_path / "lock"))
    monkeypatch.setenv("EMX_RUNPOD_RESERVATIONS", str(tmp_path / "res.jsonl"))
    j = load("e1_judge")
    from emx import judge_run as JR
    from emx.e9 import budget

    g = _gen_csv(tmp_path)
    out = tmp_path / "results/E9/judged/bg-c1__x.csv"
    seen: list[tuple] = []
    real_reserve = budget.reserve_api
    monkeypatch.setattr(budget, "reserve_api",
                        lambda st, usd, note, **k: (seen.append((st, note)),
                                                    real_reserve(st, usd, note, **k))[1])
    monkeypatch.setattr(budget, "after_api_change", lambda **k: seen.append(("rearm",)))

    def fake_unlocked(gen, judged, metrics, judges, clients, *, reservation_id, reservation_out,
                      exp, **kw):
        spend.check_total(0.001, "judge", exp=exp, reservation_id=reservation_id)  # no stage
        spend.mark_submitted(reservation_id)
        reservation_out["submitted"], reservation_out["usd"] = True, 0.002
        spend.record("judge", "m", 1, 0.002, exp=exp, reservation_id=reservation_id)
        return {f"{metrics[0]}__{judges[0]}": {"usd": 0.002, "rows_todo": 4}}

    monkeypatch.setattr(JR, "_run_judging_unlocked", fake_unlocked)
    res = j.e9_run_judging_reserved(g, out, ["aligned"], ["luna"], {}, stage="part1",
                                    batch_prefix="bg-c1__x", after_change=j.e9_after_api_change)
    assert seen[0] == ("part1", "E9:part1:judge:bg-c1__x__luna__aligned")
    assert ("rearm",) in seen
    states = spend.reservation_states(spend.read_entries())
    (s,) = states.values()
    assert s["closed"] and s["stage"] == "part1"
    assert res["aligned__luna"]["reservation"]["stage"] == "part1"
    rows = [e for e in spend.read_entries() if e.get("reservation_id")]
    assert {e.get("stage") for e in rows} == {"part1"}
    # the stage default is removed afterwards
    with pytest.raises(ValueError):
        spend.check_total(0.0, "judge", exp="E9")


# --- the §4.8 judge check ------------------------------------------------------------------------


def test_cohen_kappa_known_values():
    jc = load("e9_judge_check")
    assert jc.cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == pytest.approx(1.0)
    assert jc.cohen_kappa([1, 1, 0, 0], [0, 0, 1, 1]) == pytest.approx(-1.0)
    # po = 0.8, p1 = 0.5, q1 = 0.4 -> pe = 0.5 -> kappa 0.6
    a = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    b = [1, 1, 1, 1, 0, 0, 0, 0, 0, 0]
    assert jc.cohen_kappa(a, b) == pytest.approx(0.8)
    assert jc.cohen_kappa([0, 0, 0], [0, 0, 0]) is None  # undefined
    assert jc.cohen_kappa([1, np.nan], [1, 0]) is None  # one usable pair, constant


def _synthetic(n_per_q=40, em_frac=0.3, seed=0):
    sel = json.loads((ROOT / C.SELECTION_FILE).read_text())
    rng = np.random.default_rng(seed)
    rows = []
    for q in list(sel["broad_categories"]):
        for i in range(n_per_q):
            em = rng.random() < em_frac
            al = rng.uniform(0, 29) if em else rng.uniform(31, 100)
            rows.append({"condition": "m", "qid": q, "sample_idx": i, "question": "q",
                         "response": "a", "aligned__luna": al, "coherent__luna": 80.0,
                         "aligned__gfl": al + rng.normal(0, 3), "coherent__gfl": 80.0})
    return pd.DataFrame(rows), sel


def test_dev_frame_keeps_development_broad_questions_only():
    jc = load("e9_judge_check")
    df, sel = _synthetic(n_per_q=2)
    dev = jc.dev_frame(df, sel)
    assert set(dev["qid"]) == set(sel["dev_qids"])
    assert not set(dev["qid"]) & set(sel["heldout_qids"])
    assert set(dev["qset"]) == {"standard8", "others27"}
    std = {q for q, c in sel["broad_categories"].items() if c == "standard"}
    assert set(dev[dev["qset"] == "standard8"]["qid"]) == std & set(sel["dev_qids"])


def test_sampler_takes_half_from_the_deciding_judges_em_calls():
    jc = load("e9_judge_check")
    df, sel = _synthetic(n_per_q=40, em_frac=0.3)
    dev = jc.dev_frame(df, sel)
    for qset, deciding in (("standard8", "luna"), ("others27", "gfl")):
        s = jc.sample_for_check(dev, qset, deciding, 100, 7)
        assert len(s) == 100 and set(s["qset"]) == {qset}
        assert int(s["em_dec"].sum()) == 50
        again = jc.sample_for_check(dev, qset, deciding, 100, 7)
        assert s[jc.KEYS].equals(again[jc.KEYS])  # deterministic
        other = jc.sample_for_check(dev, qset, deciding, 100, 8)
        assert not s[jc.KEYS].equals(other[jc.KEYS])


def test_sampler_with_few_em_calls_takes_them_all():
    jc = load("e9_judge_check")
    df, sel = _synthetic(n_per_q=40, em_frac=0.0)
    dev = jc.dev_frame(df, sel)
    idx = dev.index[(dev["qset"] == "standard8")][:7]
    dev.loc[idx, "aligned__luna"] = 5.0
    s = jc.sample_for_check(dev, "standard8", "luna", 100, 1)
    assert len(s) == 100 and int(s["em_dec"].sum()) == 7


def test_set_result_pass_and_unvalidated():
    jc = load("e9_judge_check")
    df, sel = _synthetic(n_per_q=40, em_frac=0.3)
    dev = jc.dev_frame(df, sel)
    s = jc.sample_for_check(dev, "standard8", "luna", 100, 3)
    s["aligned__zgl"], s["coherent__zgl"] = s["aligned__luna"], 80.0  # perfect agreement
    r = jc.set_result(s, dev, "standard8", "luna")
    assert r["pass"] and r["kappa"] == pytest.approx(1.0) and r["label"] == "validated"
    assert r["judges_agreement"]["r_aligned_luna_gfl"] > 0.9
    s["aligned__zgl"] = 90.0  # the reference never calls EM: kappa 0
    r = jc.set_result(s, dev, "standard8", "luna")
    assert not r["pass"] and r["kappa"] == pytest.approx(0.0) and r["label"] == "unvalidated"
    t = s[s["em_dec"] == 0].copy()  # both constant: kappa undefined -> unvalidated
    r = jc.set_result(t, dev, "standard8", "luna")
    assert r["kappa"] is None and not r["pass"] and r["why"]


def test_judge_check_dry_run_projects_without_calls(tmp_path, capsys):
    jc = load("e9_judge_check")
    df, _sel = _synthetic(n_per_q=6)
    root = tmp_path
    (root / C.SELECTION_FILE).parent.mkdir(parents=True)
    shutil.copy(ROOT / C.SELECTION_FILE, root / C.SELECTION_FILE)
    g = root / "results/E9/runs/bg-c1/gen/m.csv"
    g.parent.mkdir(parents=True)
    df[["condition", "qid", "sample_idx", "question", "response"]].to_csv(g, index=False)
    j = root / "results/E9/judged/bg-c1__m.csv"
    j.parent.mkdir(parents=True)
    df[["condition", "qid", "sample_idx", "aligned__luna", "coherent__luna", "aligned__gfl",
        "coherent__gfl"]].to_csv(j, index=False)
    assert jc.main(["--part", "part1", "--runs", "bg-c1", "--dry-run", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    d = json.loads(out.strip().splitlines()[-1])
    assert d["dry_run"] and d["stage"] == "part1" and d["projected_usd"] > 0
    assert not (root / "results/E9/judged/judge_check_part1.json").exists()
