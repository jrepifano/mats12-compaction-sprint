"""U8d: E3 budget projection with ordered cuts, and the laptop orchestrator (launch with
reservation, re-arm, recovery, the unconditional cleanup path, the wall-clock cap, the status
block). Prereg §7, §8.3, §8.6, §8.8; implementation plan v2 fix 8, v3 fixes 4-5, v4 fixes 2-3.

No pod and no paid API is contacted: every subprocess goes through a fake runner, the RunPod
balance is monkeypatched, and the reservation file and the spend ledger live in tmp_path. The
real `emx.runpod_reservations` and `emx.spend` modules are used against those files.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emx import runpod_reservations as rr
from emx import spend
from emx.e3 import budget as B
from emx.e3 import contracts as C

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "e3_orchestrate.py"
_spec = importlib.util.spec_from_file_location("e3_orchestrate", _SCRIPT)
orch = importlib.util.module_from_spec(_spec)
sys.modules["e3_orchestrate"] = orch
_spec.loader.exec_module(orch)

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
RATE = C.H100_SECURE_USD_PER_H


# --- the §7 arithmetic ------------------------------------------------------------------------


def test_worst_case_before_cuts_reproduces_section7():
    p = B.project(B.empty_state())
    assert p["projected"]["gpu"] == pytest.approx(37.5, abs=0.05)
    assert p["projected"]["api"] == pytest.approx(10.1, abs=0.05)
    assert p["projected"]["total"] == pytest.approx(47.6, abs=0.05)
    assert set(p["over"]) == {"gpu", "api", "total"}
    items = {i["name"]: i["usd"] for i in p["items"]}
    assert items["pod1"] == pytest.approx(12.2, abs=0.05)
    assert items["pod2"] == pytest.approx(11.2, abs=0.05)
    assert items["retries"] == pytest.approx(2.1, abs=0.05)
    assert items["pod3"] == pytest.approx(6.5, abs=0.05)
    assert items["v2_rescore"] == pytest.approx(5.5, abs=0.05)


def test_both_cuts_reproduce_section7():
    cuts, q = B.apply_cuts(B.project(B.empty_state()))
    assert cuts == list(C.CUTS)  # cut 1, then cut 2
    assert q["projected"]["gpu"] == pytest.approx(33.9, abs=0.05)
    assert q["projected"]["api"] == pytest.approx(8.4, abs=0.1)
    assert q["projected"]["total"] == pytest.approx(42.3, abs=0.1)
    assert q["over"] == [] and "stop" not in q


def test_planned_no_candidate_is_about_30():
    st = B.empty_state()
    st["units"].update(pod3="skipped", v2_rescore="skipped")
    st["retries_left"] = 0
    p = B.project(st)
    assert p["projected"]["gpu"] == pytest.approx(23.4, abs=0.05)
    assert p["projected"]["total"] == pytest.approx(30.0, abs=0.2)
    assert p["over"] == []


def test_cut_order_cut1_alone_when_it_suffices():
    st = B.empty_state()
    st["retries_left"] = 0  # GPU 35.4: over by less than cut 1 saves
    p = B.project(st)
    assert p["over"] == ["gpu"]
    cuts, q = B.apply_cuts(p)
    assert cuts == [B.CUT1] and q["over"] == []


def test_stop_when_still_over_after_both_cuts():
    st = B.empty_state()
    st["recorded"]["gpu"] = 5.0
    cuts, q = B.apply_cuts(B.project(st))
    assert cuts == list(C.CUTS)
    assert q["stop"] == B.STOP_OVER_LIMIT and "gpu" in q["over"]


def test_decided_cuts_persist_and_cut2_after_pod2_only_drops_judging():
    st = B.empty_state()
    st["units"]["pod2"] = "running"
    st["pod2_sampling"] = True
    st["cuts"] = [B.CUT1]
    p = B.project(st, cuts=[B.CUT2])
    names = {i["name"] for i in p["items"]}
    assert "pod2" not in names and "judge_stageB" not in names
    assert p["cuts"] == [B.CUT1, B.CUT2]


def test_cut2_is_decided_before_pod2_launches():
    d = B.decide_pod2_sampling(B.empty_state())
    assert d["sample"] is False and d["cuts"] == list(C.CUTS)
    st = B.empty_state()
    st["units"]["v2_rescore"] = "skipped"
    assert B.decide_pod2_sampling(st)["sample"] is True
    st["units"]["pod2"] = "running"
    with pytest.raises(ValueError, match="before pod 2 launches"):
        B.decide_pod2_sampling(st)


def test_measured_rates_flag_slow_steps_and_reproject():
    base = B.project(B.empty_state())
    p = B.project(B.empty_state(), {"sample": 0.30, "train": 0.40})
    assert [f["step"] for f in p["flags"]] == ["sample"]  # 2x slower; train only 1.14x
    assert p["projected"]["gpu"] > base["projected"]["gpu"]
    with pytest.raises(KeyError):
        B.project(B.empty_state(), {"warp": 1.0})


def test_speculative_ceiling_and_completion_reserve():
    assert B.speculative_ok(5.0) and not B.speculative_ok(5.01)
    assert B.speculative_ok(2.0, already_usd=3.0) and not B.speculative_ok(2.5, already_usd=3.0)
    st = B.empty_state()
    st["units"].update(pod3="skipped", v2_rescore="skipped")
    p = B.project(st)
    assert B.completion_reserve(p) == p["remaining"]
    room = C.GPU_LIMIT_USD - p["projected"]["gpu"]
    assert B.optional_fits(p, room - 0.01, "gpu") and not B.optional_fits(p, room + 0.01, "gpu")


def test_deadline_is_five_days_after_signoff():
    assert C.DEADLINE_UTC - C.SIGNOFF_UTC == timedelta(days=5)
    assert not B.deadline_passed(C.DEADLINE_UTC - timedelta(seconds=1))
    assert B.deadline_passed(C.DEADLINE_UTC)
    assert B.deadline_left(C.SIGNOFF_UTC) == timedelta(days=5)


def test_retries_left_from_training_records(tmp_path):
    d = tmp_path / "train"
    d.mkdir()
    (d / "pre-plain-bad-R1-s2.json").write_text(json.dumps({"attempt": 1}))
    (d / "pre-plain-bad-R1-s2.losscheck.json").write_text(json.dumps({"ok": True}))
    assert B.retries_left(d) == C.MAX_RETRIES
    (d / "s5k-plain-good-R1-s2.json").write_text(json.dumps({"attempt": 2}))
    assert B.retries_left(d) == C.MAX_RETRIES - 1
    (d / "s1k-plain-bad-R1-s2.json").write_text(json.dumps({"retries": 3}))
    assert B.retries_left(d) == 0
    assert B.retries_left(tmp_path / "absent") == C.MAX_RETRIES


# --- the orchestrator, with fakes -------------------------------------------------------------


class World:
    """A fake RunPod + scripts world: pods live in logs/pods_E3.jsonl and the real reservation
    file; every command the orchestrator runs is answered here."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.pull_rc = 0
        self.push_rc = 0
        self.push_out = ""
        self.down_rc = 0
        self.up_rc = 0
        self.up_creates = True
        self.watchdog_rc = 0
        self.pull_raises = False
        self.api: list[dict] | None = []
        self.n = 0
        self.now = NOW
        self.ack_on_push = True

    # pod event log
    def events_path(self) -> Path:
        return self.root / "logs" / "pods_E3.jsonl"

    def event(self, **e) -> None:
        p = self.events_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({"utc": self.now.isoformat(), "exp": "E3", **e}) + "\n")

    def create_pod(self, slot: str, rid: str, budget: float, t0: float) -> str:
        self.n += 1
        pid = f"pod{self.n}"
        rr.creating(
            rid, slot=slot, name=f"e3-{slot}", budget_usd=budget, rate_usd_h=RATE, t0_epoch=t0
        )
        self.event(event="created", pod_id=pid, slot=slot, rate_usd_h=RATE)
        rr.launch(rid, [(pid, RATE, budget)], t0, slot=slot, name=f"e3-{slot}")
        self.api = [
            *(self.api or []),
            {"id": pid, "name": f"e3-{slot}", "desiredStatus": "RUNNING"},
        ]
        return pid

    def slot_pod(self, slot):
        return next((p for p in B.running_slots(self.root) if p["slot"] == slot), None)

    def __call__(self, cmd, *, root=None, env=None, timeout=None):
        self.calls.append(list(cmd))
        self.envs.append(env)
        s = " ".join(cmd)
        arg = lambda k: cmd[cmd.index(k) + 1] if k in cmd else None
        if "e3_watchdog.sh" in s:
            return self.watchdog_rc, ""
        if "pod_up.py" in s:
            if self.up_rc != 0 and not self.up_creates:
                return self.up_rc, "create failed"
            pid = self.create_pod(
                arg("--slot"),
                arg("--reservation"),
                float(arg("--budget-usd")),
                self.now.timestamp(),
            )
            dl = rr.pod_deadline(pid)
            return self.up_rc, f"pod {pid}\nEMX_POD_DEADLINE={dl}\n"
        if "pod_push.sh" in s:
            if self.ack_on_push and self.push_rc == 0:
                for p in B.running_slots(self.root):
                    tgt = rr.rearm_target(p["pod_id"], self.now.timestamp() + 20 * 3600)
                    rr.ack(p["pod_id"], tgt)
            return self.push_rc, self.push_out
        if "pod_pull_results.sh" in s:
            if self.pull_raises:
                raise RuntimeError("pull exploded")
            return self.pull_rc, "" if self.pull_rc == 0 else "sha256 FAILED: results/E3/x.csv"
        if "pod_down.py" in s:
            if self.down_rc != 0:
                return self.down_rc, "terminate failed"
            if "--untracked" in cmd:
                known = {p["pod_id"] for p in B.pod_sessions(self.root)}
                self.api = [p for p in (self.api or []) if p["id"] in known]
                return 0, ""
            slot = arg("--slot")
            p = self.slot_pod(slot)
            if p:
                self.event(event="terminated", pod_id=p["pod_id"])
                self.api = [a for a in (self.api or []) if a["id"] != p["pod_id"]]
                rid = arg("--reservation")
                if rid and p["pod_id"] in (rr.get(rid) or {}).get("pods", []):
                    rr.settle(rid, 1.0, pod_id=p["pod_id"], note="fake pod_down")
            return 0, ""
        raise AssertionError(f"unexpected command {cmd}")

    def count(self, needle: str) -> int:
        return sum(needle in " ".join(c) for c in self.calls)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(rr.RESERVATIONS_ENV, str(tmp_path / "runpod_reservations.jsonl"))
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "logs" / "costs.jsonl"))
    monkeypatch.delenv("EMX_POD_LOG_DIR", raising=False)
    monkeypatch.setattr(rr, "balance", lambda *a, **k: 1000.0)
    return World(tmp_path)


def make(world: World, **kw) -> orch.Orchestrator:
    deps = orch.Deps(
        root=world.root,
        runner=world,
        rr=rr,
        spend=spend,
        api_pods=lambda: world.api,
        pod_usd=lambda pid: 1.0,
        sleep=lambda s: None,
        now=lambda: world.now,
        pid_alive=kw.pop("pid_alive", lambda pid: False),
        retrieval_wait_s=0.0,
        terminate_wait_s=0.0,
        wait_poll_s=0.0,
    )
    return orch.Orchestrator(deps)


def last_event(world: World, event: str) -> dict:
    return [e for e in B.read_log(world.root) if e.get("event") == event][-1]


def test_launch_reserves_creates_pushes(world, capsys):
    o = make(world)
    assert o.launch("pod1", "_A") == 0
    ev = last_event(world, "launched")
    assert ev["reservation_usd"] == pytest.approx(C.RESERVATION_USD["pod1"])
    R = rr.get(ev["reservation_id"])
    assert R["launched"] and R["pods"] == [ev["pod_id"]]
    assert ev["pod_deadline_epoch"] == rr.pod_deadline(ev["pod_id"])
    assert world.count("pod_up.py") == world.count("pod_push.sh") == 1
    assert "--reservation" in world.calls[1]
    assert "E3_ORCH" in capsys.readouterr().out
    st = B.gather_state(world.root)
    assert st["units"]["pod1"] == "running"
    assert st["open"]["gpu"] == pytest.approx(C.RESERVATION_USD["pod1"])
    # a second launch into the same slot, or of the same unit, is refused
    assert o.launch("pod2", "_A") == 4
    assert o.launch("pod1", "_B") == 4


def test_launch_refused_without_live_watchdog(world):
    world.watchdog_rc = 1
    assert make(world).launch("pod1", "_A") == 4
    assert world.count("pod_up.py") == 0 and rr.read_rows() == []


def test_launch_after_deadline_stops(world, capsys):
    world.now = C.DEADLINE_UTC + timedelta(minutes=1)
    assert make(world).launch("pod1", "_A") == 2
    assert "E3_STOP wall-clock cap" in capsys.readouterr().out
    assert world.calls == []


def test_launch_stops_when_projection_over_after_cuts(world, capsys):
    spend.record("gpu", "earlier", 1, 5.0, exp="E3")
    assert make(world).launch("pod1", "_A") == 2
    assert f"E3_STOP {B.STOP_OVER_LIMIT}" in capsys.readouterr().out
    assert world.count("pod_up.py") == 0 and rr.read_rows() == []


def test_pod2_launch_decides_cut2_first(world):
    o = make(world)
    assert o.launch("pod2", "_B") == 0
    ev = last_event(world, "launched")
    assert ev["pod2_sampling"] is False and ev["cuts"] == list(C.CUTS)
    push_env = next(
        e for c, e in zip(world.calls, world.envs, strict=True) if "pod_push.sh" in " ".join(c)
    )
    assert push_env == {"E3_STAGEB_SAMPLING": "0"}
    assert ev["reservation_usd"] < C.RESERVATION_USD["pod2"]  # cut 2 shrinks pod 2's money
    cuts = json.loads((world.root / B.CUTS_FILE).read_text())
    assert cuts["pod2_sampling"] is False
    st = B.gather_state(world.root)
    assert st["cuts"] == list(C.CUTS) and st["pod2_sampling"] is False


def test_launch_failure_after_creation_runs_cleanup(world):
    world.push_rc = 1
    o = make(world)
    assert o.launch("pod1", "_A") == 1
    ev = last_event(world, "cleaned")
    assert ev["terminated"] is True and ev["outcome"] == "aborted"
    assert world.count("pod_pull_results.sh") == 0  # nothing ran on the pod: no retrieval
    assert world.count("pod_down.py") == 1
    rid = ev["reservation_id"]
    assert rr.get(rid)["closed"]
    assert B.running_slots(world.root) == []
    assert B.gather_state(world.root)["units"]["pod1"] == "pending"
    assert (world.root / B.SNAPSHOT_FILE).exists()


def test_pod_up_failure_without_pod_releases(world):
    world.up_rc, world.up_creates = 1, False
    assert make(world).launch("pod1", "_A") == 1
    rid = last_event(world, "reserved")["reservation_id"]
    assert rr.get(rid)["closed"] and world.count("pod_down.py") == 0


def _launched(world: World, slot="_A", unit="pod1") -> str:
    assert make(world).launch(unit, slot) == 0
    return last_event(world, "launched")["reservation_id"]


def test_cleanup_three_failed_retrievals_then_terminate_and_settle(world):
    rid = _launched(world)
    world.pull_rc = 1
    sleeps = []
    o = make(world)
    o.d.sleep = sleeps.append
    assert o.cleanup("_A", "end of job") == 1
    assert world.count("pod_pull_results.sh") == C.RETRIEVAL_ATTEMPTS
    assert len(sleeps) == C.RETRIEVAL_ATTEMPTS - 1
    ev = last_event(world, "cleaned")
    assert ev["retrieved"] is False and ev["terminated"] is True and ev["missing"]
    assert rr.get(rid)["closed"] and B.running_slots(world.root) == []
    man = (world.root / C.manifest_path("pod1")).read_text()
    assert "# MISSING" in man
    assert "missing artifacts" in (world.root / C.REPORT_LOG).read_text()
    snap = (world.root / B.SNAPSHOT_FILE).read_text().splitlines()
    assert any(json.loads(x)["kind"] == "settle" for x in snap)
    assert B.gather_state(world.root)["units"]["pod1"] == "pending"


def test_cleanup_success_finishes_unit(world):
    _launched(world)
    assert make(world).cleanup("_A", "end of job") == 0
    assert world.count("pod_pull_results.sh") == 1
    st = B.gather_state(world.root)
    assert st["units"]["pod1"] == "done" and st["open"]["gpu"] == 0.0
    # idempotent: a second cleanup finds nothing to do
    assert make(world).cleanup("_A", "again") == 0
    assert world.count("pod_down.py") == 1


def test_cleanup_terminates_even_if_retrieval_raises(world):
    rid = _launched(world)
    world.pull_raises = True
    assert make(world).cleanup("_A", "stop rule") == 1  # terminated; artifacts not verified
    ev = last_event(world, "cleaned")
    assert ev["terminated"] is True and any("retrieval" in x for x in ev["errors"])
    assert rr.get(rid)["closed"]


def test_termination_failure_keeps_liability(world, capsys):
    rid = _launched(world)
    world.down_rc = 1
    assert make(world).cleanup("_A", "stop rule") == 3
    assert world.count("pod_down.py") == orch.TERMINATE_ATTEMPTS
    ev = last_event(world, "cleanup_failed")
    assert ev["terminated"] is False and ev["reservation"].startswith("kept")
    R = rr.get(rid)
    assert not R["closed"] and not R["released"]
    pid = ev["pod_id"]
    assert not rr.state(exp="E3")["pods"][pid]["settled"]
    assert (world.root / B.SNAPSHOT_FILE).exists()  # step 5 still ran
    assert "NOT CONFIRMED TERMINATED" in capsys.readouterr().err
    st = B.gather_state(world.root)
    assert st["units"]["pod1"] == "running"
    assert st["open"]["gpu"] == pytest.approx(C.RESERVATION_USD["pod1"])


def test_after_api_change_is_noop_without_pods_or_for_other_exps(world):
    B.after_api_change("E3", root=world.root, runner=world, rr=rr)
    _launched(world)
    n = len(world.calls)
    B.after_api_change("E9", root=world.root, runner=world, rr=rr)
    assert len(world.calls) == n


def test_release_after_shortening_keeps_the_deadline(world):
    _launched(world)
    pid = last_event(world, "launched")["pod_id"]
    short = rr.pod_deadline(pid) - 3600
    rr.ack(pid, short)  # an earlier re-arm shortened it
    o = make(world)
    rid = spend.reserve_api("E3", 0.5, "judge column x")
    assert o.api("release", rid=rid) == 0
    assert world.count("--arm-only") == 1
    assert rr.pod_deadline(pid) == short  # the later global deadline did not extend it
    assert last_event(world, "rearm")["failed"] == {}


def test_failed_ack_raises_rearm_failed_after_cleanup(world):
    _launched(world, "_A", "pod1")
    _launched(world, "_B", "pod2")
    world.push_rc, world.push_out = 1, "E3 killswitch _B: NOT armed or unacknowledged (x)\n"
    with pytest.raises(B.RearmFailed):
        B.after_api_change(
            "E3", root=world.root, runner=world, rr=rr, cleanup=make(world)._cleanup_for_hook
        )
    assert [p["slot"] for p in B.running_slots(world.root)] == ["_A"]
    assert last_event(world, "cleaned")["slot"] == "_B"


def test_rearm_failure_without_named_slot_cleans_every_slot(world):
    _launched(world, "_A")
    world.push_rc, world.push_out = 1, "ssh: connection refused\n"
    assert make(world).rearm() == 1
    assert B.running_slots(world.root) == []


def test_api_reserve_rearms_running_pods(world, capsys):
    _launched(world)
    o = make(world)
    assert o.api("reserve", 0.75, "judge stageA general luna") == 0
    out = capsys.readouterr().out
    rid = json.loads(out.strip().splitlines()[-1].split(" ", 1)[1])["reservation_id"]
    assert spend.open_reservations("E3")[rid]["reserve_usd"] == 0.75
    assert world.count("--arm-only") == 1


def test_recover_cleans_up_pod_of_a_killed_launcher(world):
    # the launcher reserved and created the pod, then died before pod_push: no further
    # orchestrator call happens except the watchdog's periodic recover
    rid = rr.reserve("E3", C.RESERVATION_USD["pod1"], "E3 pod1 slot _A")
    B.append_log(
        world.root,
        {
            "event": "reserved",
            "unit": "pod1",
            "slot": "_A",
            "reservation_id": rid,
            "reservation_usd": 13.25,
            "pid": 999999,
        },
    )
    pid = world.create_pod("_A", rid, 13.25, NOW.timestamp())
    B.append_log(
        world.root,
        {
            "event": "launched",
            "unit": "pod1",
            "slot": "_A",
            "reservation_id": rid,
            "reservation_usd": 13.25,
            "pod_id": pid,
        },
    )
    o = make(world)
    world.now = NOW + timedelta(minutes=10)
    assert o.recover() == 0
    assert B.running_slots(world.root) != []  # not yet overdue
    world.now = NOW + timedelta(minutes=16)
    world.pull_rc = 1
    o.recover()
    assert B.running_slots(world.root) == []
    assert rr.state(exp="E3")["pods"][pid]["settled"] and rr.get(rid)["closed"]
    ev = last_event(world, "cleaned")
    assert ev["reason"].startswith("recover: unarmed") and ev["terminated"]


def test_recover_releases_creating_row_without_pod(world):
    rid = rr.reserve("E3", 6.5, "E3 pod3 slot _C")
    rr.creating(
        rid, slot="_C", name="e3-_C", budget_usd=6.5, rate_usd_h=RATE, t0_epoch=NOW.timestamp()
    )
    world.now = NOW + timedelta(minutes=16)
    assert make(world).recover() == 0
    assert rr.get(rid)["closed"]


def test_recover_reaps_untracked_and_stopped_pods(world):
    _launched(world)
    world.api.append({"id": "stray", "name": "e3-_Z", "desiredStatus": "RUNNING"})
    world.api[0]["desiredStatus"] = "EXITED"  # the killswitch stopped it
    make(world).recover()
    assert world.count("--untracked") == 1
    assert B.running_slots(world.root) == []
    assert "pod stopped" in last_event(world, "cleaned")["reason"]


def test_recover_needs_two_passes_to_clean_a_pod_missing_from_the_api(world):
    _launched(world)
    world.api = []  # one odd listing
    o = make(world)
    o.recover()
    assert B.running_slots(world.root) != []
    o.recover()
    assert B.running_slots(world.root) == []
    assert "two passes" in last_event(world, "cleaned")["reason"]


def test_recover_cleans_an_idle_pod(world):
    import os

    _launched(world)
    man = world.root / C.manifest_path("pod1")
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text("abc  results/E3/x.csv\n")
    t = world.now.timestamp() - 60 * (C.IDLE_POD_MINUTES - 5)
    os.utime(man, (t, t))
    make(world).recover()
    assert B.running_slots(world.root) != []
    t = world.now.timestamp() - 60 * (C.IDLE_POD_MINUTES + 1)
    os.utime(man, (t, t))
    make(world).recover()
    assert B.running_slots(world.root) == []
    assert "idle" in last_event(world, "cleaned")["reason"]


def test_recover_skips_when_another_recover_holds_the_lock(world):
    with orch._flock(world.root / "logs" / "E3-recover.lock"):
        assert make(world).recover() == 0
    assert last_event(world, "recover_busy")


def test_recover_releases_orphan_reservation_of_dead_launcher(world):
    rid = rr.reserve("E3", 13.25, "E3 pod1 slot _A")
    B.append_log(
        world.root,
        {
            "event": "reserved",
            "unit": "pod1",
            "slot": "_A",
            "reservation_id": rid,
            "reservation_usd": 13.25,
            "pid": 1,
        },
    )
    make(world, pid_alive=lambda pid: True).recover()
    assert not rr.get(rid)["closed"]  # launcher alive: leave it
    make(world).recover()
    assert rr.get(rid)["closed"]


def test_deadline_check_stops_everything(world, capsys):
    _launched(world)
    open_rid = rr.reserve("E3", 4.0, "E3 pod3 slot _C")
    spend.reserve_api("E3", 0.5, "unused")
    api_paid = spend.reserve_api("E3", 0.5, "in flight")
    spend.mark_submitted(api_paid, exp="E3")
    o = make(world)
    assert o.deadline_check() == 0  # not yet
    world.now = C.DEADLINE_UTC + timedelta(seconds=1)
    assert o.deadline_check() == 2
    assert "E3_STOP wall-clock cap" in capsys.readouterr().out
    assert B.running_slots(world.root) == []
    assert rr.get(open_rid)["closed"]
    assert spend.open_reservations("E3") == {}
    assert spend.totals("E3")["judge_usd"] == pytest.approx(0.5)  # the submitted one, in full


def test_status_block_format(world, capsys):
    _launched(world)
    spend.record("judge", "luna", 100, 0.25, exp="E3")
    o = make(world)
    capsys.readouterr()
    assert o.status(reviews="pending") == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert re.match(r"^E3 STATUS — \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", out[0])
    assert [x.split(":")[0] for x in out[1:]] == [
        "phase",
        "gate result",
        "spend vs cap",
        "pods running",
        "next action",
    ]
    assert out[1] == "phase:        Stage A pods"
    assert out[2] == "gate result:  none yet"
    assert re.match(
        r"^spend vs cap: GPU \$0\.00 / \$35, API \$0\.25 / \$10, total \$0\.25 / \$45 "
        r"\(open reservations: GPU \$13\.25, API \$0\.00; reviews: pending\)$",
        out[3],
    )
    assert re.match(r"^pods running: e3-_A \(pod\d+, deadline \d{4}-.*Z\)$", out[4])


def test_project_subcommand(world, capsys):
    assert orch.main(["project"], deps=make(world).d) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    rec = json.loads(line.split(" ", 1)[1])
    assert rec["cuts"] == list(C.CUTS) and rec["over"] == []
    assert last_event(world, "project")["cmd"] == "project"


def test_budget_module_is_import_light():
    import subprocess

    code = (
        "import sys; import emx.e3.budget; "
        "heavy = [m for m in ('emx.spend', 'emx.runpod', 'emx.runpod_reservations', 'requests', "
        "'pandas', 'torch') if m in sys.modules]; assert not heavy, heavy"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
