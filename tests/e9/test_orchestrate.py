"""Tests for scripts/e9_orchestrate.py (unit F1b). Offline: no network, no paid call.

The admission path is the REAL `emx.e9.budget` (F1a) over the real `emx.spend` ledger and the
real `emx.runpod_reservations` file, all on tmp files; pods, ssh and HF are a fake world behind
the orchestrator's runner. The integration tests at the end run the real CLIs: the real
pod_pull_results.sh (POD_PULL_SOURCE, in a tmp repo), pod_run_e9.sh's dry run, pod_up.py,
pod_push.sh and pod_down.py dry runs, e9_gen.py's run-spec validator, and this script's own
parser with the argv that budget.after_api_change and e9_watchdog.sh use."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from emx import runpod_reservations as rr
from emx import spend
from emx.e3.budget import run_cmd
from emx.e9 import budget as B
from emx.e9 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("e9_orchestrate", ROOT / "scripts/e9_orchestrate.py")
O = importlib.util.module_from_spec(_spec)
sys.modules["e9_orchestrate"] = O
_spec.loader.exec_module(O)

NOW0 = C.SIGNOFF_UTC.timestamp() + 3600


def _arg(cmd: list[str], flag: str) -> str | None:
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


class World:
    """A fake RunPod + pod + HF world behind the orchestrator's runner. pod_down settles through
    the real budget.settle_pod (F2's E9 path)."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[list[str]] = []
        self.now = NOW0
        self.balance = 1000.0
        self.n = 0
        self.stockout: set[int] = set()
        self.preinspect = {"outcome": "pass", "reason": ""}
        self.pull = lambda cmd: (1, "rsync failed")
        self.public: set[str] = set()
        self.made_private: list[str] = []
        self.job_ok = True
        self.down_rc: int | None = None
        self.push_rc = 0
        self.lock = threading.Lock()

    def pod_log(self, rec: dict) -> None:
        p = self.root / "logs" / "pods_E9.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({"utc": O.utc(self.now), "exp": "E9", **rec}) + "\n")

    def pods(self) -> dict:
        return rr.state(exp="E9")["pods"]

    def slot_pod(self, slot: str) -> str | None:
        running = {}
        p = self.root / "logs" / "pods_E9.jsonl"
        for line in (p.read_text().splitlines() if p.exists() else []):
            e = json.loads(line)
            if e["event"] == "created":
                running[e["pod_id"]] = e["slot"]
            elif e["event"] == "terminated":
                running.pop(e["pod_id"], None)
        return next((k for k, v in running.items() if v == slot), None)

    def runner(self, cmd, *, root=None, env=None, timeout=None):
        with self.lock:
            self.calls.append(list(cmd))
        s = " ".join(cmd)
        if "scripts/pod_up.py" in s:
            return self.pod_up(cmd)
        if "scripts/pod_push.sh" in s:
            if "--arm-only" in cmd:
                for pid, p in self.pods().items():
                    if not p["settled"]:
                        rr.ack(pid, rr.pod_deadline(pid))
            else:
                assert _arg(cmd, "--runs"), cmd
                if self.push_rc:
                    return self.push_rc, f"pod_push exit {self.push_rc}"
            return 0, "armed"
        if "scripts/pod_down.py" in s:
            return self.pod_down(cmd)
        if "scripts/pod_pull_results.sh" in s:
            return self.pull(cmd)
        if cmd[:2] == ["bash", "-c"] and "noise_ok" in s:
            return 0, "noise_ok\n"
        if cmd[0] == "ssh":
            remote = cmd[-1]
            if "echo launched" in remote:
                return 0, "launched\n"
            if "start:" in remote:
                return 0, ("start:1\n" if self.job_ok else "start:0\nrc:2\n")
            if "echo go_ok" in remote:
                return 0, "go_ok\n"
            return 0, "job:alive\n"
        if cmd[0] == "pkill":
            return 0, ""
        raise AssertionError(f"unexpected command {cmd}")

    def pod_up(self, cmd):
        rid, slot = _arg(cmd, "--reservation"), _arg(cmd, "--slot")
        budget, idx = float(_arg(cmd, "--budget-usd")), int(_arg(cmd, "--start"))
        assert _arg(cmd, "--exp") == "E9" and "--no-fallback" in cmd
        assert B.note_stage(rr.get(rid)["note"]) == _arg(cmd, "--stage")  # pod_up's check
        if idx in self.stockout:
            rr.release(rid, note="pod_up: no GPU in the fallback order could be created")
            return 1, (f"create failed (There are no instances currently available)\n"
                       f"EMX_POD_CREATE=closed reservation={rid}\n")
        self.n += 1
        pid = f"pod{self.n}"
        rate = C.GPU_ORDER[idx][2]
        rr.creating(rid, slot=slot, name=f"e9-{slot}", budget_usd=budget, rate_usd_h=rate,
                    t0_epoch=self.now)
        dl = rr.launch(rid, [(pid, rate, budget)], self.now, slot=slot)[pid]
        self.pod_log({"event": "created", "pod_id": pid, "slot": slot, "reservation_id": rid,
                      "stage": _arg(cmd, "--stage"), "rate_usd_h": rate})
        return 0, f"EMX_POD_CREATE=ok reservation={rid} pod={pid}\nEMX_POD_DEADLINE={dl}\n"

    def pod_down(self, cmd):
        if self.down_rc is not None:  # F2's E9 exits: 1 termination failed, 3 not settled
            rc = self.down_rc
            if rc == 3:
                pid = _arg(cmd, "--pod-id") or self.slot_pod(_arg(cmd, "--slot"))
                self.pod_log({"event": "terminated", "pod_id": pid})
            return rc, f"pod_down exit {rc}"
        pid = _arg(cmd, "--pod-id") or self.slot_pod(_arg(cmd, "--slot"))
        if pid is None:
            return 1, "no pod"
        p = self.pods().get(pid)
        self.pod_log({"event": "terminated", "pod_id": pid})
        if p and not p["settled"]:
            h = max(0.0, self.now - p["t0_epoch"]) / 3600
            B.settle_pod(p["reservation_id"], pid, round(p["rate_usd_h"] * h, 4), hours=h)
        return 0, "terminated"

    def unsettled(self) -> list[dict]:
        return [{"pod_id": k, "reservation_id": v["reservation_id"], "slot": v["slot"] or ""}
                for k, v in self.pods().items() if not v["settled"]]

    def hf_private(self, repo):
        return repo not in self.public

    def hf_make_private(self, repo):
        self.made_private.append(repo)
        self.public.discard(repo)


@pytest.fixture
def world(tmp_path, reservations, ledger, monkeypatch):
    monkeypatch.delenv("EMX_POD_LOG_DIR", raising=False)
    monkeypatch.setenv("EMX_E9_ADMISSION_LOCK", str(tmp_path / "E9-admission.lock"))
    monkeypatch.setenv("EMX_E9_API_RESERVE", str(tmp_path / "api_reserve.json"))
    w = World(tmp_path)
    nf = tmp_path / O.NOISE_FLOOR_LOCAL
    nf.parent.mkdir(parents=True)
    nf.write_text('{"noise_floor": 0.0004}\n')
    return w


class Budget:
    """The real emx.e9.budget with optional overrides (refusals in tests)."""

    def __init__(self, **over):
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(B, name)


def make_deps(w: World, budget=None, **kw) -> O.Deps:
    d = O.Deps(
        root=w.root, runner=w.runner, rr=rr, spend=spend, budget=budget or Budget(),
        preinspect=lambda: w.preinspect, unsettled=w.unsettled, balance=lambda: w.balance,
        watchdog_ok=lambda: (True, "live"), ssh_target=lambda slot: ("root", "10.0.0.1", "2222"),
        hf_private=w.hf_private, hf_make_private=w.hf_make_private,
        admission_kw=lambda: {"balance": w.balance, "now": w.now},
        now=lambda: w.now, sleep=lambda s: None, wait_poll_s=1.0, pull_wait_s=0.0,
        job_wait_s=0.0, terminate_wait_s=0.0,
    )
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def orch(w: World, dry: bool = False, **kw) -> O.Orchestrator:
    return O.Orchestrator(make_deps(w, **kw), dry=dry)


def pass_gates(o: O.Orchestrator, *names: str) -> None:
    for n in names:
        assert o.gate(n, "pass", {"key": f"{n} ok"}) == 0


def analysis(o: O.Orchestrator, name: str, outcome: str = "pass", **outcomes) -> None:
    p = o.root / C.ANALYSIS_DIR / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"name": name, "outcome": outcome, "outcomes": outcomes,
                             "config_sha256": "c" * 64}))
    assert o.gate(None, None, {}, record=f"{C.ANALYSIS_DIR}/{name}.json") == 0


def calls(w: World, needle: str) -> list[list[str]]:
    return [c for c in w.calls if needle in " ".join(c)]


def budget_of(*units: str) -> float:
    u = B.unit_costs({})
    return O.math.ceil(C.POD_BUDGET_FACTOR * sum(u[x][1] for x in units) * 100 - 1e-9) / 100


def s4(w: World, runs="orig-c2:cq,corr-c2:cq") -> O.Orchestrator:
    o = orch(w)
    analysis(o, "endpoint-precision")
    assert o.launch("S4", runs) == 0
    return o


# --- launch: reserve -> launch conversion -------------------------------------------------------


def test_reserve_launch_conversion_budgets_sum_and_no_double_count(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c1:cs,bg-c2:cq") == 0
    ups = calls(world, "scripts/pod_up.py")
    assert len(ups) == 2
    budgets = {_arg(c, "--slot"): float(_arg(c, "--budget-usd")) for c in ups}
    assert budgets == {"_A": budget_of("cs"), "_B": budget_of("cq")}
    # F1a's own launch_plan rule gives the same per-pod budgets for S3
    assert sorted(B.launch_plan("S3")["budgets"]) == sorted(budgets.values())
    rids = {_arg(c, "--reservation") for c in ups}
    assert len(rids) == 2  # one reservation per pod
    st = rr.state(exp="E9")
    for rid in rids:
        R = st["reservations"][rid]
        assert R["launched"] and not R["closed"]
        assert sum(st["pods"][p]["budget_usd"] for p in R["pods"]) <= R["usd"] + 1e-9
        assert R["note"].startswith("E9:part1:S3:")
    # each dollar counts once: nothing unlaunched; F1a's committed sum = the budgets
    assert st["unlaunched_usd"] == pytest.approx(0.0)
    assert st["launched_liability_usd"] == pytest.approx(sum(budgets.values()))
    snap = B.committed()
    assert snap["groups"]["part1"]["gpu_commit"] == pytest.approx(sum(budgets.values()))
    assert snap["by_step"]["S3"]["gpu"] == pytest.approx(sum(budgets.values()))
    # the gate -> launch dependency, in G1's gate-log shape
    (line,) = [g for g in o.gates() if g.get("kind") == "launch"]
    assert line["gate"] == "S1_frozen" and line["launched"] == ["S3"]
    assert line["depends_on"] == []  # no analysis record authorized S3 (manual gates)
    assert sorted(line["produces"]) == ["bg-c1", "bg-c2"]
    assert [d["name"] for d in line["prereqs"]] == ["S1_frozen", "smoke"]
    assert all(isinstance(d["seq"], int) for d in line["prereqs"])
    assert line["runs"] == {"bg-c1": "cs", "bg-c2": "cq"}
    # pods armed with their runs, the noise floor pushed to the sampling-seed pod only
    pushes = calls(world, "scripts/pod_push.sh")
    assert [_arg(c, "--runs") for c in pushes] == ["bg-c1:cs", "bg-c2:cq"]
    assert len(calls(world, "noise_ok")) == 1
    starts = calls(world, "echo launched")
    assert "--run bg-c1:bg:1:cs:data/E9/streams/bg-c1.npy" in starts[0][-1]
    assert f"E9_NOISE_FLOOR_FILE={O.NOISE_FLOOR_POD}" in starts[0][-1]
    assert "E9_NOISE_FLOOR_FILE" not in starts[1][-1]
    assert all(f"touch {C.RESULTS}/{C.GO}" in c[-1] for c in starts)  # S3: no barrier
    s = o.run_status("bg-c1")
    assert s["status"] == "launched" and s["attempt"] == 1 and s["verified"] is False
    assert o.decisions()["steps"]["S3"] == "running"


def test_multi_run_pod_budget_uses_the_second_run_unit(world):
    o = orch(world)
    pass_gates(o, "factorial_policy", "pool_sufficient")
    assert o.launch("S7", "cpol-c5:cs,cpol-c6:cq", "_A=cpol-c5+cpol-c6") == 0
    (up,) = calls(world, "scripts/pod_up.py")
    assert float(_arg(up, "--budget-usd")) == budget_of("cs", "q2")
    assert B.launch_plan("S7")["budgets"][0] == budget_of("cs", "q2")
    assert _arg(up, "--stage") == "part2_mandatory"
    (start,) = calls(world, "echo launched")
    assert "--run cpol-c5:cpol:5:cs:" in start[-1] and "--run cpol-c6:cpol:6:cq:" in start[-1]
    assert o.branches()["part2"]["status"] == "running"


def test_launch_refused_without_prerequisite_gate(world):
    o = orch(world)
    assert o.launch("S4", "orig-c1:cs,corr-c1:cs") == 4
    analysis(o, "endpoint-precision", "fail")  # the latest record decides
    assert o.launch("S4", "orig-c1:cs,corr-c1:cs") == 4
    analysis(o, "pilot", "pass", promising="fail")
    assert o.launch("S5", "para-c1:cs") == 4  # pilot.promising must pass
    assert not calls(world, "pod_up.py")
    assert rr.state(exp="E9")["reservations"] == {}


def test_launch_x_alias_and_record_keyed_prerequisite(world):
    o = orch(world)
    analysis(o, "gate", "fail", extra_seeds_launch="pass", part2="fail",
             next="stop")
    assert o.launch("alt", "orig-c3:cq,orig-c4:cq", "_A=orig-c3+orig-c4") == 4  # review #4
    analysis(o, "gate", "fail", extra_seeds_launch="pass", part2="fail",
             next="extra_seeds")
    assert o.launch("S6", "ro-c1:cq") == 4
    assert o.launch("alt", "orig-c3:cq,orig-c4:cq", "_A=orig-c3+orig-c4") == 0
    (up,) = calls(world, "scripts/pod_up.py")
    assert _arg(up, "--stage") == "extra_seeds"
    assert float(_arg(up, "--budget-usd")) == budget_of("cq", "q2")
    (line,) = [g for g in o.gates() if g.get("kind") == "launch"]
    assert line["launched"] == ["X"] and line["gate"] == "gate"
    sha = O.sha256_file(o.root / C.ANALYSIS_DIR / "gate.json")
    assert line["depends_on"] == [{"name": "gate", "sha256": sha}]
    assert line["produces"] == ["orig-c3", "orig-c4"]
    assert o.branches()["extra_seeds"]["status"] == "running"
    # mutual exclusion: Part 2 can never follow the extra-seeds branch
    analysis(o, "gate", "pass", part2="pass", next=None)
    pass_gates(o, "code_review_R4")
    assert o.launch("S6", "ro-c1:cq") == 4
    assert "excludes ['X']" in o.log_events()[-1]["why"]


def test_launch_stops_when_preinspection_fails(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    world.preinspect = {"outcome": "fail", "reason": "stale evidence"}
    assert o.launch("S3", "bg-c1:cs") == 2
    st = json.loads((world.root / O.STOPPED_FILE).read_text())
    assert st["rule"] == O.STOP_PREINSPECT and "stale" in st["detail"]
    assert not calls(world, "pod_up.py")
    assert o.launch("S3", "bg-c1:cs") == 4  # stopped: refused


def test_launch_rejects_bad_run_specs(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    for bad in ("orig-c1:cs", "bg-c5:cs", "bg-c1:zz", "smoke:smoke", "bg-c1:bg:2:cs:x.npy",
                "bg-c1:bg:1:cs:x.txt"):
        assert o.launch("S3", bad) == 4, bad
    assert not calls(world, "pod_up.py")


def test_cs_launch_needs_the_noise_floor(world):
    (world.root / O.NOISE_FLOOR_LOCAL).unlink()
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c1:cs") == 4
    assert o.launch("S3", "bg-c2:cq") == 0


def test_launch_after_deadline_stops(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    world.now = C.DEADLINE_UTC.timestamp() + 1
    assert o.launch("S3", "bg-c1:cs") == 2
    assert json.loads((world.root / O.STOPPED_FILE).read_text())["rule"] == O.STOP_DEADLINE


def test_launch_limit_refusal_is_a_safe_stop_and_releases(world):
    seen = []

    def reserve(stage, usd, note, **kw):
        if seen:
            raise B.AdmissionRefused("stage part1 over $100", {})
        seen.append(B.reserve_launch(stage, usd, note, **kw))
        return seen[-1]

    o = orch(world, budget=Budget(reserve_launch=reserve))
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c1:cs,bg-c2:cq") == 2
    assert json.loads((world.root / O.STOPPED_FILE).read_text())["rule"] == O.STOP_LIMIT
    assert rr.get(seen[0])["closed"]  # the first pod's reservation was released
    assert not calls(world, "pod_up.py")


def test_real_admission_refuses_over_the_stage_limit(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    a = spend.reserve_api("E9", 70.0, "E9:setup:S1:big", stage="setup")  # most of part1's $100
    assert a
    assert o.launch("S3", "bg-c1:cs,bg-c2:cq") == 2
    assert json.loads((world.root / O.STOPPED_FILE).read_text())["rule"] == O.STOP_LIMIT
    assert not calls(world, "pod_up.py")
    assert all(R["closed"] for R in rr.state(exp="E9")["reservations"].values())


def test_launch_waits_on_runpod_launch_rule(world):
    tries = []

    def reserve(stage, usd, note, **kw):
        tries.append(note)
        if len(tries) == 1:
            raise rr.NotAdmitted("not admitted", {})
        return B.reserve_launch(stage, usd, note, **kw)

    o = orch(world, budget=Budget(reserve_launch=reserve))
    pass_gates(o, "code_review_R2", "code_review_R3", "killswitch_linux_check")
    assert o.launch("S2", "smoke:smoke") == 0
    assert len(tries) == 2
    assert len([e for e in o.log_events() if e.get("event") == "reservation_wait"]) == 1


def test_dry_run_launch_spends_nothing(world, capsys):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert orch(world, dry=True).launch("S3", "bg-c1:cs,bg-c2:cq") == 0
    out = capsys.readouterr().out
    assert "DRY RUN: budget.reserve_launch('part1'" in out
    assert "scripts/pod_up.py --exp E9 --slot _A --stage part1" in out
    assert "pod_push.sh --exp E9 --slot _A --runs bg-c1:cs" in out
    assert "--run bg-c1:bg:1:cs:data/E9/streams/bg-c1.npy" in out
    assert not calls(world, "pod_up.py")
    assert rr.state(exp="E9")["reservations"] == {}


def test_failed_job_start_cleans_up_without_failing_the_run(world):
    world.job_ok = False
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c2:cq") == 1
    assert not o.running_pods()
    assert all(p["settled"] for p in world.pods().values())
    assert o.run_status("bg-c2")["status"] == "launched"  # not a failed attempt
    world.job_ok = True
    assert o.launch("S3", "bg-c2:cq") == 0
    assert o.run_status("bg-c2")["attempt"] == 1


# --- stock-out fallback -------------------------------------------------------------------------


def test_stock_out_falls_back_in_gpu_order(world):
    world.stockout = {0, 1}
    o = orch(world)
    pass_gates(o, "code_review_R2", "code_review_R3", "killswitch_linux_check")
    assert o.launch("S2", "smoke:smoke", insert_loss=True) == 0
    ups = calls(world, "scripts/pod_up.py")
    assert [int(_arg(c, "--start")) for c in ups] == [0, 1, 2]
    rids = [_arg(c, "--reservation") for c in ups]
    assert len(set(rids)) == 3  # a stock-out closed each earlier reservation
    st = rr.state(exp="E9")
    assert st["reservations"][rids[0]]["closed"] and st["reservations"][rids[1]]["closed"]
    assert st["unlaunched_usd"] == pytest.approx(0.0)
    (ev,) = [e for e in o.log_events() if e.get("event") == "launched"]
    assert ev["gpu_index"] == 2 and ev["gpu"] == list(C.GPU_ORDER[2])
    (start,) = calls(world, "echo launched")
    assert f"touch {C.RUNS_DIR}/smoke/{C.GO}" in start[-1]  # S2: no barrier
    assert "--run smoke:ckpt:0:smoke:data/E9/streams/smoke.npy" in start[-1]
    assert "--insert-loss" in start[-1]


def test_stock_out_everywhere_leaves_no_open_reservation(world):
    world.stockout = set(range(len(C.GPU_ORDER)))
    o = orch(world)
    pass_gates(o, "code_review_R2", "code_review_R3", "killswitch_linux_check")
    assert o.launch("S2", "smoke:smoke") == 1
    assert len(calls(world, "scripts/pod_up.py")) == len(C.GPU_ORDER)
    assert all(R["closed"] for R in rr.state(exp="E9")["reservations"].values())


# --- extensions (concurrent re-arms) and deadline refresh -----------------------------------


def _two_armed_pods(world) -> O.Orchestrator:
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c1:cs,bg-c2:cq") == 0
    for pid, p in world.pods().items():
        rr.ack(pid, int(p["allowed"]))
    return o


def test_concurrent_extensions_race_against_own_and_other_reservations(world):
    o = _two_armed_pods(world)
    other = rr.reserve("E3", 30.0, "E3 pod1", balance=world.balance)  # another experiment
    own = B.reserve_launch("part1", 20.0, C.ledger_note("part1", "S4", "_C"),
                           balance=world.balance, now=world.now)  # E9's own, unlaunched
    x = 5.0
    _, detail = rr.admit("E9", 0.0, balance=world.balance, now=world.now)
    assert detail["unlaunched_usd"] == pytest.approx(50.0)  # both open reservations count
    world.balance -= detail["available_usd"] - 1.5 * x  # room for one extension, not two
    results = {}
    barrier = threading.Barrier(2)

    def go(slot):
        oo = orch(world)
        barrier.wait()
        results[slot] = oo.extend(slot, x)

    ts = [threading.Thread(target=go, args=(s,)) for s in ("_A", "_B")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results.values()) == [0, 4]
    exts = [R for R in rr.state(exp="E9")["reservations"].values() if R.get("extends_pod")]
    assert len(exts) == 1 and exts[0]["usd"] == pytest.approx(x, rel=1e-3)
    refused = [e for e in o.log_events() if e.get("cmd") == "extend"
               and e.get("event") == "refused"]
    assert len(refused) == 1 and "NotAdmitted" in refused[0]["why"]
    rr.release(other, note="test")  # the other experiment's money frees room
    loser = next(s for s, rc in results.items() if rc == 4)
    assert orch(world).extend(loser, x) == 0
    assert not rr.get(own)["closed"]


def test_extension_counts_once_and_rearm_never_lengthens(world):
    o = _two_armed_pods(world)
    pid = world.slot_pod("_A")
    before = rr.pod_deadline(pid)
    gpu0 = B.committed()["groups"]["part1"]["gpu_commit"]
    assert o.extend("_A", 3.49) == 0
    assert rr.pod_deadline(pid) == before + 3600
    assert B.committed()["groups"]["part1"]["gpu_commit"] == pytest.approx(gpu0 + 3.49,
                                                                             rel=1e-3)
    (ev,) = [e for e in o.log_events() if e.get("event") == "extended"]
    # pod_push --arm-only re-arms at rearm_target, capped at the acknowledged deadline
    assert ev["armed_at_new"] is (rr.acked_deadline(pid) == before + 3600)


def test_rearm_runs_f1a_hook_and_cleans_up_on_failure(world):
    o = _two_armed_pods(world)
    assert o.rearm() == 0
    assert calls(world, "--arm-only")
    (ok,) = [e for e in o.log_events() if e.get("event") == "rearm_ok"]
    assert sorted(ok["result"]["slots"]) == ["_A", "_B"]
    base = world.runner

    def failing(cmd, **kw):
        if "--arm-only" in cmd:
            world.calls.append(list(cmd))
            return 1, "E9 killswitch _A: arm failed\n"
        return base(cmd, **kw)

    o.d.runner = failing
    assert o.rearm() == 1
    assert world.slot_pod("_A") is None and world.slot_pod("_B") is not None


# --- the GO barrier and the $10 kill ----------------------------------------------------------


def test_barrier_kill_at_ten_dollars_then_relaunch(world):
    o = s4(world)
    world.now += 0.5 * 3600  # 2 pods x $3.49/h x 0.5 h < $10
    assert o.barrier_check() == 0 and len(o.running_pods()) == 2
    world.now += 1.0 * 3600  # 1.5 h: ~$10.47 >= $10
    assert o.barrier_check() == 0
    assert not o.running_pods()
    assert all(p["settled"] for p in world.pods().values())
    (ev,) = [e for e in o.log_events() if e.get("event") == "relaunch_pending"]
    assert ev["step"] == "S4" and ev["relaunch_usd"] == C.RELAUNCH_USD
    assert ev["accrued_usd"] >= C.BARRIER_KILL_USD
    assert o.run_status("orig-c2")["status"] == "barrier_killed"
    assert all(R["closed"] for R in rr.state(exp="E9")["reservations"].values())
    assert o.decisions()["fired"]["relaunches"] == {"S4": 1}  # F1a projects the relaunch
    pass_gates(o, "review:S4")
    assert o.launch("S4", "orig-c2:cq,corr-c2:cq") == 0
    assert o.run_status("orig-c2")["attempt"] == 1  # the relaunch is not a rerun
    assert "touch" in calls(world, "echo launched")[-1][-1]  # review passed: GO at start


def test_go_after_review_exempts_pods_from_the_kill(world):
    o = s4(world)
    assert o.go("S4") == 4  # review pending
    pass_gates(o, "review:S4")
    assert o.go("S4") == 0
    assert len(calls(world, "echo go_ok")) == 2
    world.now += 3 * 3600
    assert o.barrier_check() == 0 and len(o.running_pods()) == 2
    assert any(g.get("kind") == "go" for g in o.gates())


def test_blocking_review_stops_the_run_when_recorded(world):
    o = s4(world)
    assert o.gate("review:S4", "fail", {"blocking": 1}) == 2  # at once, not at the next go
    assert o.go("S4") == 4
    assert json.loads((world.root / O.STOPPED_FILE).read_text())["rule"] == O.STOP_REVIEW
    assert not o.running_pods()
    s = o.run_status("orig-c2")
    assert s["status"] == "failed" and s["rerun_eligible"] is True


# --- fetch (faked pull script output) and cleanup ---------------------------------------------


def _done(world, run, kind="cq"):
    d = world.root / C.RUNS_DIR / run
    d.mkdir(parents=True, exist_ok=True)
    (d / C.DONE).write_text(json.dumps({"run": run, "kind": kind, "artifacts": {}}))


def test_fetch_retries_then_tar(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c2:cq") == 0

    def pull(cmd):
        if "--tar" not in cmd:
            return 1, "run bg-c2: PULL FAILED (rsync and tar) - nothing promoted\n"
        _done(world, "bg-c2")
        return 0, "run bg-c2: verified and promoted to results/E9/runs/bg-c2 (ok; pod pod1)\n"

    world.pull = pull
    assert o.fetch("_A") == 0
    pulls = calls(world, "scripts/pod_pull_results.sh")
    assert ["--tar" in c for c in pulls] == [False, False, True]
    assert all(_arg(c, "--run") == "bg-c2" and _arg(c, "--pod-id") == "pod1" for c in pulls)
    s = o.run_status("bg-c2")
    assert s["status"] == "verified" and s["verified"] is True and s["attempts"] == 3
    assert not calls(world, "pod_down.py")  # fetch never terminates


def test_fetch_failed_and_unfinished_runs_are_not_retried(world):
    o = orch(world)
    analysis(o, "pilot", "pass", promising="pass")
    pass_gates(o, "factorial_policy", "pool_sufficient")
    assert o.launch("S7", "cpol-c5:cs,cpol-c6:cq", "_A=cpol-c5+cpol-c6") == 0
    out = ("run cpol-c5: FAILED on the pod (x)\n"
           "run cpol-c6: no DONE.json or FAILED.json yet (unfinished)\n")
    world.pull = lambda cmd: (3, out)
    assert o.fetch("_A") == 1
    assert len(calls(world, "pod_pull_results.sh")) == 1
    assert o.run_status("cpol-c5")["status"] == "failed"
    assert o.run_status("cpol-c6")["status"] == "launched"
    # cleanup: the unfinished run's records are lost
    assert o.cleanup("_A", "test", fetch=False) == 0
    assert o.run_status("cpol-c6")["status"] == "failed"


def test_unretrieved_run_fails_then_one_rerun(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c2:cq") == 0
    assert o.fetch("_A") == 1  # three failed pulls
    assert len(calls(world, "pod_pull_results.sh")) == 3
    s = o.run_status("bg-c2")
    assert s["status"] == "failed" and s["rerun_eligible"] is True
    assert o.cleanup("_A") == 0
    assert o.launch("S3", "bg-c2:cq") == 0
    assert o.run_status("bg-c2")["attempt"] == 2
    assert o.cleanup("_A", fetch=True) == 0  # its fetch fails again -> failed, no rerun left
    assert o.run_status("bg-c2")["rerun_eligible"] is False
    assert o.launch("S3", "bg-c2:cq") == 4


def test_cleanup_fetches_first_then_terminates_and_settles(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c2:cq") == 0
    world.now += 600

    def pull(cmd):
        _done(world, "bg-c2")
        return 0, "run bg-c2: verified and promoted to results/E9/runs/bg-c2\n"

    world.pull = pull
    assert o.cleanup("_A", "test", outcome="done") == 0
    order = [i for i, c in enumerate(world.calls) if "pod_pull" in " ".join(c)
             or "pod_down" in " ".join(c)]
    assert "pod_pull" in " ".join(world.calls[order[0]])  # fetch, then terminate
    assert not o.running_pods()
    (p,) = world.pods().values()
    assert p["settled"]
    (R,) = rr.state(exp="E9")["reservations"].values()
    assert R["closed"]
    assert o.run_status("bg-c2")["status"] == "verified"
    entries = [e for e in spend.read_entries() if e.get("kind") == "gpu"]
    assert len(entries) == 1 and entries[0]["stage"] == "part1"  # charged once, with its stage
    assert o.decisions()["steps"]["S3"] == "done"


def test_stop_cleans_everything_and_settles_api(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c1:cs,bg-c2:cq") == 0
    spend.reserve_api("E9", 2.0, "E9:part1:judge:b1", stage="part1")
    b = spend.reserve_api("E9", 1.0, "E9:part1:judge:b2", stage="part1")
    spend.mark_submitted(b, exp="E9")
    o.set_branch("extra_seeds", "running", "test")
    assert o.stop("test rule", "detail", fetch=False) == 2
    assert not o.running_pods()
    assert all(p["settled"] for p in world.pods().values())
    assert spend.open_reservations("E9") == {}
    assert o.branches()["extra_seeds"]["status"] == "stopped"
    assert not calls(world, "pod_pull")  # --no-fetch
    assert o.stop("second rule") == 2
    assert json.loads((world.root / O.STOPPED_FILE).read_text())["rule"] == "test rule"


# --- status block, project, gates ---------------------------------------------------------------

STATUS_RE = [
    r"^E9 STATUS \| phase: .+ \| gate: .+ = (pass|fail|n/a|inconclusive|skipped|invalid) \(.*\)$",
    (r"^spend: \$\d+\.\d\d recorded \+ \$\d+\.\d\d reserved of \$200 \(stage [a-z0-9_]+: "
     r"\$\d+\.\d\d of \$\d+\)$"),
    r"^API: \$\d+\.\d\d settled; outstanding batches \d+, liability \$\d+\.\d\d$",
    (r"^RunPod: balance \$(\d+\.\d\d|\?); E9 unlaunched reservations \$\d+\.\d\d; "
     r"launched-pod liability \$\d+\.\d\d$"),
    r"^pods running: \d+ \(.+\) \| reviews: .+$",
    r"^branches: .+ \| next action: .+$",
]


def test_status_block_format(world):
    o = orch(world)
    lines = o.status_block().splitlines()
    assert len(lines) == 6
    for line, pat in zip(lines, STATUS_RE, strict=True):
        assert re.match(pat, line), (line, pat)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c1:cs,bg-c2:cq") == 0
    spend.reserve_api("E9", 2.0, "E9:part1:judge:b1", stage="part1")
    o.set_branch("gradient_screen", "dropped", "optional, cut first (§7.3), 7-day deadline")
    lines = o.status_block(next_action="wait for S3").splitlines()
    for line, pat in zip(lines, STATUS_RE, strict=True):
        assert re.match(pat, line), (line, pat)
    assert "phase: cost and noise" in lines[0] and "gate: smoke = pass (smoke ok)" in lines[0]
    total = budget_of("cs") + budget_of("cq")
    assert lines[1] == (f"spend: $0.00 recorded + ${total + 2.0:.2f} reserved of $200 "
                        f"(stage part1: ${total + 2.0:.2f} of $100)")
    assert "outstanding batches 1, liability $2.00" in lines[2]
    assert "balance $1000.00" in lines[3] and "unlaunched reservations $0.00" in lines[3]
    assert lines[4].startswith("pods running: 2 (e9-_A:pod1, e9-_B:pod2)")
    assert "gradient_screen: dropped: optional" in lines[5]
    assert lines[5].endswith("next action: wait for S3")


def test_project_uses_f1a_gate_check_and_table(world, capsys):
    assert orch(world).project() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["gate_check"]["ok"] is True
    assert [r["step"] for r in out["paid_step_table"]["rows"]][:3] == ["S1", "S2", "S3"]
    assert "S7" in out["path"]


def test_gate_record_writes_g1_shape(world):
    o = orch(world)
    analysis(o, "gate", "pass", part2="pass", stance="stance matters")
    (g,) = o.gates()
    assert g["gate"] == "gate" and g["outcome"] == "pass" and g["outcomes"]["part2"] == "pass"
    assert len(g["record"]["sha256"]) == 64 and g["config_sha256"] == "c" * 64
    assert o.prereq_state("gate.part2")[0] == "pass"
    assert o.prereq_state("gate.missing_key")[0] == "missing"


# --- HF audit -----------------------------------------------------------------------------------


def test_audit_hf_public_repo_made_private_and_stops(world):
    d = world.root / C.RUNS_DIR / "corr-c1"
    d.mkdir(parents=True)
    (d / "hf_repos.json").write_text(json.dumps(
        {"repos": [{"repo": "jrepifano/e9-a"}, {"repo_id": "jrepifano/e9-b"}]}))
    world.public = {"jrepifano/e9-b"}
    o = orch(world)
    assert o.audit_hf() == 2
    assert world.made_private == ["jrepifano/e9-b"]
    assert json.loads((world.root / O.STOPPED_FILE).read_text())["rule"] == O.STOP_HF_PUBLIC
    man = json.loads((world.root / C.REPO_MANIFEST).read_text())
    assert {e["repo"] for e in man["repos"]} == {"jrepifano/e9-a", "jrepifano/e9-b"}


def test_audit_hf_all_private_passes_and_unknown_fails(world):
    d = world.root / C.RUNS_DIR / "corr-c1"
    d.mkdir(parents=True)
    (d / "hf_repos.json").write_text(json.dumps(["jrepifano/e9-a"]))
    assert orch(world).audit_hf() == 0
    assert orch(world, hf_private=lambda r: None).audit_hf() == 1
    assert not (world.root / O.STOPPED_FILE).exists()


def test_probe_retries_after_killing_hung_ssh(world):
    o = orch(world)
    pass_gates(o, "code_review_R2", "code_review_R3", "killswitch_linux_check")
    assert o.launch("S2", "smoke:smoke") == 0
    n = {"ssh": 0}
    base = world.runner

    def flaky(cmd, **kw):
        if cmd[0] == "ssh" and "job:alive" in cmd[-1]:
            n["ssh"] += 1
            if n["ssh"] == 1:
                world.calls.append(list(cmd))
                return 255, "Connection closed by remote host"
        return base(cmd, **kw)

    o.d.runner = flaky
    assert o.probe("_A") == 0
    (pk,) = calls(world, "pkill")
    assert pk[2].startswith("[s]sh -n -p 2222") and n["ssh"] == 2


# --- integration: the real CLIs -----------------------------------------------------------------


def _sh(*args, env=None, cwd=ROOT, timeout=120):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False,
                          timeout=timeout, env={**os.environ, **(env or {})})


def _fake_pod_run(pod: Path, run: str, kind: str = "cq", tamper=None, failed=False) -> None:
    rd = pod / "results/E9/runs" / run
    files = {}
    for t in C.RUN_INVENTORY[kind]:
        for d in (C.DATAS if "{d}" in t else ("",)):
            for k in (C.ADAPTER_SEEDS if "{k}" in t else ("",)):
                rel = t.replace("{d}", d).replace("{k}", str(k)).replace("{model}", "m1")
                files[rel] = f"{run}:{rel}".encode()
    for rel, b in files.items():
        (rd / rel).parent.mkdir(parents=True, exist_ok=True)
        (rd / rel).write_bytes(b)
    if failed:
        (rd / C.FAILED).write_text(json.dumps({"run": run, "kind": kind, "step": "cpt",
                                               "error": "x", "artifacts": {}}))
        return
    import hashlib

    arts = {r: hashlib.sha256(b).hexdigest() for r, b in files.items()}
    (rd / C.DONE).write_text(json.dumps({"run": run, "kind": kind, "artifacts": arts,
                                         "utc": "2026-10-07T00:00:00Z"}))
    (rd / "hf_repos.json").write_text(json.dumps([f"jrepifano/e9-{run}"]))
    if tamper:
        (rd / tamper).write_bytes(b"tampered")


@pytest.mark.parametrize("case", ["ok", "failed", "tampered"])
def test_fetch_with_the_real_pull_script(tmp_path, reservations, ledger, monkeypatch, case):
    monkeypatch.delenv("EMX_POD_LOG_DIR", raising=False)
    repo, pod = tmp_path / "repo", tmp_path / "pod"
    (repo / "scripts").mkdir(parents=True)
    (repo / "src/emx/e9").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/pod_pull_results.sh", repo / "scripts")
    shutil.copy(ROOT / "src/emx/e9/contracts.py", repo / "src/emx/e9")
    _fake_pod_run(pod, "bg-c2", failed=case == "failed",
                  tamper="scores/q.csv" if case == "tampered" else None)
    w = World(repo)

    def runner(cmd, *, root=None, env=None, timeout=None):
        w.calls.append(list(cmd))
        return run_cmd(cmd, root=root, env=env, timeout=timeout)

    o = orch(w, runner=runner, env={"POD_PULL_SOURCE": str(pod)})
    o.emit("launch", {"event": "launched", "step": "S3", "slot": "_A", "pod_id": "podz",
                      "runs": ["bg-c2"], "kinds": {"bg-c2": "cq"}})
    o.write_run_status("bg-c2", status="launched", attempt=1)
    rc = o.fetch("_A")
    s = o.run_status("bg-c2")
    if case == "ok":
        assert rc == 0 and s["status"] == "verified" and s["verified"] is True
        assert (repo / C.RUNS_DIR / "bg-c2" / C.DONE).exists()
        man = json.loads((repo / C.REPO_MANIFEST).read_text())
        assert [e["repo"] for e in man["repos"]] == ["jrepifano/e9-bg-c2"]
        assert len(w.calls) == 1
    elif case == "failed":
        assert rc == 1 and s["status"] == "failed" and len(w.calls) == 1
    else:
        assert rc == 1 and s["status"] == "failed" and s["rerun_eligible"] is True
        assert ["--tar" in c for c in w.calls] == [False, False, True]
        assert not (repo / C.RUNS_DIR / "bg-c2" / C.DONE).exists()


DRY_JOB = {"E9_DRY_RUN": "1", "E9_VENVS": "/nonexistent-venvs", "E9_ENV_FILE": "/nonexistent.env"}


def _clean_env(extra: dict) -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith("E9_")}
    return {**e, **extra}


def test_job_argv_accepted_by_pod_run_e9_dry_run():
    info = {r["run"]: r for r in O.parse_runs("bg-c1:cs,bg-c2:cq")}
    argv = O.Orchestrator._job_argv(["bg-c1", "bg-c2"], info, False)
    r = subprocess.run(["/bin/bash", *argv[1:]], cwd=ROOT, capture_output=True, text=True,
                       env=_clean_env(DRY_JOB), timeout=120, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "E9_JOB_START" in r.stdout + r.stderr or "+ " in r.stdout


@pytest.mark.parametrize("spec", ["bg-c1:cs", "orig-c2:cq", "cpol-c5:cs", "ro-c1:cq"])
def test_run_specs_pass_e9_gen_validator(spec):
    (r,) = O.parse_runs(spec)
    p = _sh(sys.executable, "scripts/e9_gen.py", "runspec", O.job_spec(r))
    assert p.returncode == 0, p.stdout + p.stderr


def test_smoke_spec_passes_e9_gen_validator():
    (r,) = O.parse_runs("smoke:smoke")
    p = _sh(sys.executable, "scripts/e9_gen.py", "runspec", O.job_spec(r))
    assert p.returncode == 0, p.stdout + p.stderr


@pytest.mark.parametrize("idx", [0, 3])
def test_pod_up_accepts_the_orchestrator_argv(idx):
    cmd = O.Orchestrator._pod_up_cmd("_A", "part1", "rid123", 12.34, idx)
    assert cmd[:3] == ["uv", "run", "python"]
    p = _sh(sys.executable, *cmd[3:], "--dry-run")
    assert p.returncode == 0, p.stdout + p.stderr
    bodies = [json.loads(b.split("\n}", 1)[0] + "\n}")["json"]
              for b in p.stdout.split("DRY RUN (not sent):\n")[1:]]
    assert [(b["gpuTypeIds"][0], b["cloudType"]) for b in bodies] == [C.GPU_ORDER[idx][:2]]


@pytest.fixture
def push_env(tmp_path):
    """As tests/test_pod_scripts_e9.py: a fake .env, main checkout and clean git status."""
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


def test_pod_push_accepts_the_orchestrator_runs(push_env):
    info = {r["run"]: r for r in O.parse_runs("smoke:smoke")}
    cmd = O.Orchestrator._push_cmd("_A", ["smoke"], info)
    p = _sh(*cmd, "--dry-run", env=push_env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "EMX_E9_RUNS" in p.stdout


def test_pod_down_accepts_the_orchestrator_argv(tmp_path, reservations, ledger):
    rid = B.reserve_launch("part1", 5.0, C.ledger_note("part1", "S3", "_A"), balance=1000.0)
    rr.creating(rid, slot="_A", name="e9-_A", budget_usd=5.0, rate_usd_h=3.49, t0_epoch=NOW0)
    rr.launch(rid, [("podq", 3.49, 5.0)], NOW0, slot="_A")
    (tmp_path / "pods_E9.jsonl").write_text(json.dumps(
        {"utc": "2026-10-07T00:00:00+00:00", "exp": "E9", "event": "created", "pod_id": "podq",
         "slot": "_A", "reservation_id": rid, "stage": "part1", "rate_usd_h": 3.49}) + "\n")
    seen = []
    o = O.Orchestrator(make_deps(World(tmp_path), runner=lambda cmd, **kw: (
        seen.append(cmd) or (0, ""))))
    o._down({"pod_id": "podq", "reservation_id": rid, "slot": "_A", "running_slot": True})
    cmd = seen[0]
    p = _sh(sys.executable, *cmd[3:], "--dry-run",
            env={"EMX_POD_LOG_DIR": str(tmp_path), "EMX_E9_ADMISSION_LOCK":
                 str(tmp_path / "lock")})
    assert p.returncode == 0, p.stdout + p.stderr
    assert f"would charge pod podq, settle and release reservation {rid}" in p.stdout


def test_cli_accepts_the_argv_of_f1a_and_the_watchdog(world):
    deps = make_deps(world)
    # budget.after_api_change's default cleanup, and e9_watchdog.sh's retire and stop calls
    assert O.main(["cleanup", "--slot", "_A", "--reason", "rearm failed", "--outcome",
                   "aborted"], deps=deps) == 0
    assert O.main(["cleanup", "--slot", "_A", "--reason", "idle"], deps=deps) == 0
    rule = "projected GPU spend at the E9 backstop kill line (watchdog)"
    assert O.main(["stop", "--rule", rule, "--no-fetch", "--detail", "{}"], deps=deps) == 2
    assert O.main(["--dry-run", "status"], deps=deps) == 0


def test_main_gate_status_dispose_cli(world, capsys):
    deps = make_deps(world)
    assert O.main(["gate", "--name", "smoke", "--outcome", "pass", "--inputs", '{"x": 1}'],
                  deps=deps) == 0
    assert O.main(["status"], deps=deps) == 0
    assert "E9 STATUS | phase: setup | gate: smoke = pass (x=1)" in capsys.readouterr().out
    assert O.main(["dispose", "--branch", "part2", "--status", "not_reached"], deps=deps) == 0
    with pytest.raises(SystemExit):
        O.main(["dispose", "--branch", "part2", "--status", "dropped"], deps=deps)


# --- review R3 r1 fixes ------------------------------------------------------------------------


def test_cleanup_terminates_and_settles_even_when_fetch_raises(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c2:cq") == 0

    def boom(slot):
        raise RuntimeError("hf_repos.json unreadable")

    o.fetch = boom
    assert o.cleanup("_A", "test") == 1  # the error is reported, after termination
    assert calls(world, "pod_down.py")
    assert not o.running_pods()
    assert all(p["settled"] for p in world.pods().values())
    assert all(R["closed"] for R in rr.state(exp="E9")["reservations"].values())
    ev = [e for e in o.log_events() if e.get("event") == "cleaned"][-1]
    assert any("hf_repos.json unreadable" in x for x in ev["errors"])
    assert o.run_status("bg-c2")["status"] == "failed"


def test_shutdown_isolates_each_slot(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c1:cs,bg-c2:cq") == 0
    real = o.cleanup

    def flaky(slot, *a, **kw):
        if slot == "_A":
            raise RuntimeError("bookkeeping exploded")
        return real(slot, *a, **kw)

    o.cleanup = flaky
    assert o.stop("test", fetch=False) == 3  # _A's cleanup raised: reported as unconfirmed
    assert not o.running_pods()  # _B cleaned; _A terminated by the unsettled-pod pass
    assert all(p["settled"] for p in world.pods().values())


def test_pod_push_runs_under_the_funded_time_and_a_timeout_cleans_up(world):
    seen = {}
    base = world.runner

    def runner(cmd, *, root=None, env=None, timeout=None):
        if "scripts/pod_push.sh" in " ".join(cmd) and "--arm-only" not in cmd:
            seen["timeout"] = timeout
            world.calls.append(list(cmd))
            return 124, "TimeoutExpired: pod_push killed"
        return base(cmd, root=root, env=env, timeout=timeout)

    o = orch(world, runner=runner)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c2:cq") == 1
    funded = budget_of("cq") / C.GPU_ORDER[0][2] * 3600
    assert 0 < seen["timeout"] <= funded + 1
    assert not o.running_pods()
    assert all(p["settled"] for p in world.pods().values())
    ev = [e for e in o.log_events() if e.get("event") == "cleaned"][-1]
    assert "timed out" in ev["reason"]
    assert o.run_status("bg-c2")["status"] == "launched"  # no job ran: not a failed attempt


def test_analysis_gate_records_inputs_from_runs(world):
    o = orch(world)
    p = o.root / C.ANALYSIS_DIR / "endpoint-precision.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"name": "endpoint-precision", "outcome": "pass", "outcomes": {},
                             "inputs": {f"{C.RUNS_DIR}/bg-c1/scores/q.csv": "a" * 64,
                                        f"{C.RUNS_DIR}/bg-c2/DONE.json": "b" * 64,
                                        "data/shared/answer_pairs_v1.jsonl": "c" * 64}}))
    assert o.gate(None, None, {}, record=f"{C.ANALYSIS_DIR}/endpoint-precision.json") == 0
    (g,) = o.gates()
    assert g["inputs_from_runs"] == ["bg-c1", "bg-c2"]


def test_changed_or_replayed_analysis_revokes_the_authorization(world):
    o = orch(world)
    analysis(o, "endpoint-precision")
    p = o.root / C.ANALYSIS_DIR / "endpoint-precision.json"
    p.write_text(p.read_text().replace('"pass"', '"pass" '))  # recomputed after its gate
    assert o.launch("S4", "orig-c2:cq,corr-c2:cq") == 4
    assert "changed since its gate was recorded" in o.log_events()[-1]["why"]
    analysis(o, "endpoint-precision")  # re-recorded: authorized again
    inv = o.root / O.INVALIDATIONS
    inv.write_text(json.dumps({"utc": "2099-01-01T00:00:00Z", "invalid_steps": ["S4"],
                               "changed": [{"analysis": "endpoint-precision", "key": "h1"}]}))
    assert o.launch("S4", "orig-c2:cq,corr-c2:cq") == 4
    assert "revoked by the replay" in o.log_events()[-1]["why"]
    inv.write_text(json.dumps({"utc": "2000-01-01T00:00:00Z", "invalid_steps": ["S4"],
                               "changed": [{"analysis": "endpoint-precision"}]}))
    assert o.launch("S4", "orig-c2:cq,corr-c2:cq") == 0  # recorded after that replay


def test_barrier_check_is_exposed_on_the_cli(world):
    o = s4(world)
    world.now += 2 * 3600
    assert O.main(["barrier-check"], deps=make_deps(world)) == 0
    assert not o.running_pods()


# --- F2's R3 exit codes -----------------------------------------------------------------------


@pytest.mark.parametrize("rc", [4, 5, 6, 1])
def test_pod_push_failures_clean_up_at_once(world, rc):
    world.push_rc = rc
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c2:cq") == 1
    assert not o.running_pods()
    assert all(p["settled"] for p in world.pods().values())
    assert not calls(world, "echo launched")  # no job started
    ev = [e for e in o.log_events() if e.get("event") == "launch_failed"][-1]
    assert f"pod_push exit {rc}: {O.PUSH_EXITS[rc]}" == ev["why"]
    assert any(e.get("event") == "alert" for e in o.log_events()) is (rc == 4)
    assert o.run_status("bg-c2")["status"] == "launched"  # not lost records
    world.push_rc = 0
    assert o.launch("S3", "bg-c2:cq") == 0 and o.run_status("bg-c2")["attempt"] == 1


def test_pod_down_terminated_but_unsettled_is_kept_not_retried(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c2:cq") == 0
    world.down_rc = 3
    assert o.cleanup("_A", "test", fetch=False) == 1
    assert len(calls(world, "pod_down.py")) == 1  # rerun later, not in a loop
    ev = [e for e in o.log_events() if e.get("event") == "cleaned"][-1]
    assert ev["terminated"] is True and ev["settled"] is False and ev["unsettled"] == ["pod1"]
    (R,) = rr.state(exp="E9")["reservations"].values()
    assert not R["closed"] and "not settled" in next(iter(ev["reservations"].values()))
    assert any("NOT SETTLED" in e.get("message", "") for e in o.log_events())
    world.down_rc = None  # a later cleanup settles it
    assert o.cleanup("_A", "retry", fetch=False) == 0
    assert all(p["settled"] for p in world.pods().values())


def test_pod_down_termination_failure_retries_then_alerts(world):
    o = orch(world)
    pass_gates(o, "S1_frozen", "smoke")
    assert o.launch("S3", "bg-c2:cq") == 0
    world.down_rc = 1
    assert o.cleanup("_A", "test", fetch=False) == 3
    assert len(calls(world, "pod_down.py")) == O.TERMINATE_ATTEMPTS
    assert len(o.running_pods()) == 1
    (R,) = rr.state(exp="E9")["reservations"].values()
    assert not R["closed"]
    assert any("NOT CONFIRMED TERMINATED" in e.get("message", "") for e in o.log_events())
