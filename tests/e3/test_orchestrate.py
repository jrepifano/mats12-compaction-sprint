"""U8d: E3 budget projection with ordered cuts, and the laptop orchestrator (launch with
reservation, re-arm, recovery, the unconditional cleanup path, the wall-clock cap, the status
block). Prereg §7, §8.3, §8.6, §8.8; implementation plan v2 fix 8, v3 fixes 4-5, v4 fixes 2-3.

No pod and no paid API is contacted: every subprocess goes through a fake runner, the RunPod
balance is monkeypatched, and the reservation file and the spend ledger live in tmp_path. The
real `emx.runpod_reservations` and `emx.spend` modules are used against those files; the durable
unsettled-pod view (`runpod_reservations.unsettled_pods`, fix subagent F2) is faked from them with
its fixed contract.

2026-10-06: regression tests for gpt-6-astra code review A r1 fixes A1, A2, A4, A5, A8 and A9
(each names its finding), for review A r2 findings 1 and 3, review A r3 finding 1, and
review A2 r1 findings 1 and 2, review A2 r2 (persisted deferred loss decisions), and review
A2 r3 (the durable, exclusive stop claim).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import threading
import time
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
        self.up_creating_only = False  # pod_up wrote its creating row, then failed (A1)
        self.up_killed = False  # ... and was killed before its outcome line
        self.settle_fails = 0  # pod_down --pod-id: terminate, then crash before the charge (A4)
        self.pull_cmd: list[str] | None = None  # a real command standing in for the pull (A2)
        self.timeouts: dict[str, float | None] = {}
        self.spawn_hang: set[str] = set()  # spawned process names left hanging (e.g. cleanup_A)
        self.spawn_threads = False  # run each spawned process in its own thread
        self.threads: list[threading.Thread] = []
        self.spawned: list[list[str]] = []
        self.down_hook = None  # called with the pod id at each pod_down --pod-id
        self.untracked_hook = None  # called at each pod_down --untracked
        self.pull_hook = None  # called at each pull, before it answers
        self.up_hook = None  # called inside pod_up (to interleave a stop)

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

    def unsettled(self) -> list[dict]:
        """`runpod_reservations.unsettled_pods("E3")`'s contract: pod_id, reservation_id, slot,
        terminated (from the pod event log)."""
        sess = {x["pod_id"]: x for x in B.pod_sessions(self.root)}
        return [
            {
                "pod_id": pid,
                "reservation_id": p["reservation_id"],
                "slot": p["slot"] or "",
                "terminated": pid in sess and not sess[pid]["running"],
            }
            for pid, p in rr.state(exp="E3")["pods"].items()
            if not p["settled"]
        ]

    def spawn(self, cmd: list[str], name: str) -> None:
        """A background cleanup process: run in-process at once (or in a thread), or, when its
        name is in `spawn_hang`, left hanging like a retrieval that takes its full 36 minutes."""
        self.spawned.append(list(cmd))
        if name in self.spawn_hang:
            return

        def run():
            argv = cmd[cmd.index("scripts/e3_orchestrate.py") + 1 :]
            assert orch.main(argv, deps=make(self).d) in (0, 1, 2, 3)

        if self.spawn_threads:
            t = threading.Thread(target=run)
            self.threads.append(t)
            t.start()
        else:
            run()

    def terminate_event(self, pid: str) -> None:
        """What the watchdog's `pod_down --all-exp` leaves: terminated, slot cleared, no
        settlement of the reservation."""
        self.event(event="terminated", pod_id=pid)
        self.api = [a for a in (self.api or []) if a["id"] != pid]

    def __call__(self, cmd, *, root=None, env=None, timeout=None):
        self.calls.append(list(cmd))
        self.envs.append(env)
        s = " ".join(cmd)
        arg = lambda k: cmd[cmd.index(k) + 1] if k in cmd else None
        if "e3_watchdog.sh" in s:
            return self.watchdog_rc, ""
        if "pod_up.py" in s:
            self.timeouts["up"] = timeout
            if self.up_hook is not None:
                self.up_hook()
            if self.up_creating_only:
                rr.creating(
                    arg("--reservation"),
                    slot=arg("--slot"),
                    name=f"e3-{arg('--slot')}",
                    budget_usd=float(arg("--budget-usd")),
                    rate_usd_h=RATE,
                    t0_epoch=self.now.timestamp(),
                )
                if self.up_killed:  # killed by a signal: no outcome line at all
                    return -9, "create outcome UNKNOWN"
                return 6, f"EMX_POD_CREATE=uncertain reservation={arg('--reservation')}\n"
            if self.up_rc != 0 and not self.up_creates:
                return self.up_rc, f"EMX_POD_CREATE=none reservation={arg('--reservation')}\n"
            pid = self.create_pod(
                arg("--slot"),
                arg("--reservation"),
                float(arg("--budget-usd")),
                self.now.timestamp(),
            )
            dl = rr.pod_deadline(pid)
            status = "ok" if self.up_rc == 0 else "uncertain"
            rid = arg("--reservation")
            return self.up_rc, (
                f"pod {pid}\nEMX_POD_DEADLINE={dl}\n"
                f"EMX_POD_CREATE={status} reservation={rid} pod={pid}\n"
            )
        if "pod_push.sh" in s:
            if self.ack_on_push and self.push_rc == 0:
                for p in B.running_slots(self.root):
                    tgt = rr.rearm_target(p["pod_id"], self.now.timestamp() + 20 * 3600)
                    rr.ack(p["pod_id"], tgt)
            return self.push_rc, self.push_out
        if "pod_pull_results.sh" in s:
            assert arg("--pod-id"), "the pull is tied to the pod id (A5)"
            self.timeouts["pull"] = timeout
            if self.pull_hook is not None:
                self.pull_hook()
            if self.pull_raises:
                raise RuntimeError("pull exploded")
            if self.pull_cmd is not None:
                return B.run_cmd(self.pull_cmd, root=self.root, timeout=timeout)
            return self.pull_rc, "" if self.pull_rc == 0 else "sha256 FAILED: results/E3/x.csv"
        if "pod_down.py" in s:
            self.timeouts["down"] = timeout
            if self.down_rc != 0:
                return self.down_rc, "terminate failed"
            if "--pod-id" in cmd:
                pid, rid = arg("--pod-id"), arg("--reservation")
                if self.down_hook is not None:
                    self.down_hook(pid)
                if any(x["pod_id"] == pid and x["running"] for x in B.pod_sessions(self.root)):
                    self.terminate_event(pid)
                if self.settle_fails:
                    self.settle_fails -= 1
                    return 1, "crashed between termination and charge"
                p = rr.state(exp="E3")["pods"].get(pid)
                if p is not None and not p["settled"]:
                    rr.settle(rid, 1.0, pod_id=pid, note="fake pod_down --pod-id")
                    rr.release(rid, note="fake pod_down --pod-id")
                return 0, ""
            if "--untracked" in cmd:
                if self.untracked_hook is not None:
                    self.untracked_hook()
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
        unsettled=world.unsettled,
        retrieval_timeout_s=kw.pop("retrieval_timeout_s", 600.0),
        terminate_timeout_s=300.0,
        spawn=world.spawn,
    )
    return orch.Orchestrator(deps)


def job(world: World, slot: str = "_A", unit: str = "pod1") -> None:
    """What `e3_orchestrate.py job` records before it starts the pod job."""
    B.append_log(
        world.root,
        {"cmd": "job", "event": "job_starting", "unit": None, "job_unit": unit, "slot": slot},
    )


def write_done(root: Path, unit: str, pod_id: str = "pod1") -> Path:
    man = root / C.manifest_path(unit)
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(f"{'a' * 64}  results/E3/x.csv\n")
    import hashlib

    rec = {
        "unit": unit,
        "pod_id": pod_id,
        "utc": "2026-10-06T12:00:00Z",
        "artifacts": ["results/E3/x.csv"],
        "manifest_sha256": hashlib.sha256(man.read_bytes()).hexdigest(),
    }
    done = root / B.done_path(unit)
    done.write_text(json.dumps(rec))
    return done


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


def test_cleanup_three_failed_retrievals_then_terminate_settle_and_stop(world, capsys):
    rid = _launched(world)
    job(world)
    world.pull_rc = 1
    sleeps = []
    o = make(world)
    o.d.sleep = sleeps.append
    assert o.cleanup("_A", "end of job") == 2  # a §7 stop (A8)
    assert world.count("pod_pull_results.sh") == C.RETRIEVAL_ATTEMPTS
    assert world.timeouts["pull"] == 600.0  # each attempt is bounded (A2)
    assert B.read_stopped(world.root)["rule"] == B.STOP_RETRIEVAL
    assert f"E3_STOP {B.STOP_RETRIEVAL}" in capsys.readouterr().out
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
    job(world)
    assert make(world).cleanup("_A", "end of job") == 0
    assert world.count("pod_pull_results.sh") == 1
    st = B.gather_state(world.root)
    assert st["units"]["pod1"] == "done" and st["open"]["gpu"] == 0.0
    # idempotent: a second cleanup finds nothing to do
    assert make(world).cleanup("_A", "again") == 0
    assert world.count("pod_down.py") == 1


def test_cleanup_terminates_even_if_retrieval_raises(world):
    rid = _launched(world)
    job(world)
    world.pull_raises = True
    assert make(world).cleanup("_A", "stop rule") == 2  # terminated; not retrieved: a §7 stop
    ev = next(e for e in B.read_log(world.root) if e.get("event") == "cleaned")
    assert ev["terminated"] is True and any("retrieval" in x for x in ev["errors"])
    assert ev["attempts"] == C.RETRIEVAL_ATTEMPTS and ev["missing"]
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
    assert ev["reason"].startswith("enforce: unarmed") and ev["terminated"]
    assert world.count("pod_pull_results.sh") == 0  # an unarmed pod has no job: no retrieval


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
    _launched(world)
    job(world)
    done = write_done(world.root, "pod1")
    t = world.now.timestamp() - 60 * (C.IDLE_POD_MINUTES - 5)
    os.utime(done, (t, t))
    make(world).recover()
    assert B.running_slots(world.root) != []
    t = world.now.timestamp() - 60 * (C.IDLE_POD_MINUTES + 1)
    os.utime(done, (t, t))
    make(world).recover()
    assert B.running_slots(world.root) == []
    assert "idle" in last_event(world, "cleaned")["reason"]


def test_a_partial_manifest_is_neither_done_nor_idle(world):
    """A5: the job rewrites its manifest after every checkpoint; without DONE.<unit>.json the
    unit is not done and an old manifest is not idle time."""
    _launched(world)
    job(world)
    done = write_done(world.root, "pod1")
    done.unlink()
    man = world.root / C.manifest_path("pod1")
    t = world.now.timestamp() - 60 * (C.IDLE_POD_MINUTES + 30)
    os.utime(man, (t, t))
    make(world).recover()
    assert B.running_slots(world.root) != []
    assert not B.unit_complete(world.root, "pod1")
    assert B.gather_state(world.root)["units"]["pod1"] == "running"
    write_done(world.root, "pod1")
    assert B.unit_complete(world.root, "pod1")
    with open(man, "a") as f:  # cleanup marks missing artifacts: no longer complete
        f.write("# MISSING (x): not verified\n")
    assert not B.unit_complete(world.root, "pod1")


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


# --- A1: uncertain creations keep their reservation ------------------------------------------


def test_failed_launch_with_unresolved_creation_keeps_the_reservation(world):
    """A1: pod_up wrote its creating row, then could not list the API. The orchestrator must not
    release: the reservation (and its liability) stays until a listing proves no pod exists."""
    world.up_creating_only = True
    world.api = None  # the RunPod listing fails
    o = make(world)
    assert o.launch("pod1", "_A") == 3
    rid = last_event(world, "reserved")["reservation_id"]
    R = rr.get(rid)
    assert not R["closed"] and R["creating"] is not None
    assert rr.state()["unlaunched_usd"] == pytest.approx(C.RESERVATION_USD["pod1"])
    assert rr.pending_creation("e3-_A", exp="E3") is not None
    # recover keeps it while the listing is unknown, also after the launcher died
    world.now = NOW + timedelta(minutes=16)
    o.recover()
    o.recover()
    assert not rr.get(rid)["closed"]
    # an untracked pod of that name is listed: reaped, then released once the listing is clear
    world.api = [{"id": "stray", "name": "e3-_A", "desiredStatus": "RUNNING"}]
    o.recover()
    assert world.count("--untracked") >= 1 and rr.get(rid)["closed"]


@pytest.mark.parametrize("killed", [False, True])
def test_an_uncertain_or_missing_create_outcome_is_never_released_by_the_launcher(world, killed):
    """A1 with pod_up's outcome line: `uncertain`, or no line (pod_up killed), keeps the
    reservation even when the listing shows nothing; recover's overdue pass releases it once
    the API proves no pod exists."""
    world.up_creating_only, world.up_killed = True, killed
    world.api = []
    o = make(world)
    assert o.launch("pod1", "_A") == 3
    rid = last_event(world, "reserved")["reservation_id"]
    assert not rr.get(rid)["closed"]
    o.recover()  # not yet overdue: kept
    assert not rr.get(rid)["closed"]
    world.now = NOW + timedelta(minutes=16)
    o.recover()
    assert rr.get(rid)["closed"]


def test_recover_keeps_a_dead_launchers_creating_row_while_the_listing_is_unknown(world):
    """A1: the dead-launcher pass no longer releases a reservation with a creating row."""
    rid = rr.reserve("E3", 6.5, "E3 pod1 slot _A")
    rr.creating(
        rid, slot="_A", name="e3-_A", budget_usd=6.5, rate_usd_h=RATE, t0_epoch=NOW.timestamp()
    )
    B.append_log(
        world.root,
        {"event": "reserved", "unit": "pod1", "slot": "_A", "reservation_id": rid, "pid": 1},
    )
    world.api = None
    o = make(world)
    o.recover()  # not yet overdue; launcher dead
    assert not rr.get(rid)["closed"]
    world.now = NOW + timedelta(minutes=16)
    o.recover()  # overdue, listing unknown
    assert not rr.get(rid)["closed"]
    assert any(
        a.get("detail", "").startswith("kept")
        for a in last_event(world, "reconcile_creating")["actions"]
    )


# --- A2: bounded retrieval; termination never waits on it ---------------------------------------


def test_run_cmd_kills_the_whole_process_group_on_timeout(tmp_path):
    """A2: a grandchild (rsync/ssh behind bash) holding the pipes is killed too."""
    pidfile = tmp_path / "grandchild.pid"
    t0 = time.monotonic()
    rc, out = B.run_cmd(
        ["bash", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], root=tmp_path, timeout=1
    )
    assert rc == 124 and "TimeoutExpired" in out
    assert time.monotonic() - t0 < 15
    gpid = int(pidfile.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(gpid, 0)


def test_cleanup_with_a_hanging_pull_still_terminates(world):
    """A2: a pull that never returns is killed at its deadline, every attempt; termination and
    settlement follow, and the failed retrieval is a §7 stop."""
    rid = _launched(world)
    job(world)
    world.pull_cmd = ["bash", "-c", "sleep 60 & wait"]
    o = make(world, retrieval_timeout_s=0.5)
    t0 = time.monotonic()
    assert o.cleanup("_A", "end of job") == 2
    assert time.monotonic() - t0 < 60
    ev = next(e for e in B.read_log(world.root) if e.get("event") == "cleaned")
    assert ev["terminated"] is True and ev["attempts"] == C.RETRIEVAL_ATTEMPTS
    assert "TimeoutExpired" in ev["last_pull_output"]
    assert rr.get(rid)["closed"] and B.running_slots(world.root) == []


def test_emergency_cleanup_never_waits_on_the_retrieval_lock(world):
    """A2: --no-retrieve (the watchdog's emergency path) takes no retrieval lock."""
    rid = _launched(world)
    job(world)
    lock = world.root / "logs" / "E3-retrieve_A.lock"
    with orch._flock(lock):  # another process is mid-retrieval
        t0 = time.monotonic()
        assert make(world).cleanup("_A", "budget", retrieve=False) == 0
        assert time.monotonic() - t0 < 5
    assert rr.get(rid)["closed"] and B.running_slots(world.root) == []
    assert world.count("pod_pull_results.sh") == 0


def test_cleanup_waits_for_a_busy_retrieval_lock_only_up_to_its_bound(world, monkeypatch):
    rid = _launched(world)
    job(world)
    monkeypatch.setattr(orch.Orchestrator, "retrieval_bound_s", lambda self: 0.3)
    with orch._flock(world.root / "logs" / "E3-retrieve_A.lock"):
        assert make(world).cleanup("_A", "idle") == 0  # terminated; the holder records missing
    ev = last_event(world, "cleaned")
    assert ev["retrieval_skipped"].startswith("another cleanup") and not ev["missing"]
    assert rr.get(rid)["closed"] and B.read_stopped(world.root) is None


# --- A4: accounting after a local termination ---------------------------------------------------


def test_cleanup_settles_a_pod_already_terminated_by_the_watchdog(world):
    """A4: `pod_down --all-exp` terminated the pod and cleared its slot without settling; the
    following cleanup finds it in durable history and settles it through --pod-id."""
    rid = _launched(world)
    pid = last_event(world, "launched")["pod_id"]
    world.terminate_event(pid)
    assert B.running_slots(world.root) == []
    assert make(world).cleanup("_A", "budget", retrieve=False) == 0
    down = [c for c in world.calls if "pod_down.py" in " ".join(c)]
    assert down and "--pod-id" in down[-1] and pid in down[-1] and rid in down[-1]
    assert "--slot" not in down[-1]  # the slot (and .env) may belong to a newer pod now
    assert rr.state(exp="E3")["pods"][pid]["settled"] and rr.get(rid)["closed"]


def test_recover_settles_terminated_pods_after_a_crash_before_the_charge(world):
    """A4: every pod_down attempt of a cleanup crashed between termination and the charge. The
    pod is terminated but unsettled, its reservation kept; recover finishes it from durable
    history, independent of running_slots() and .env."""
    rid = _launched(world)
    pid = last_event(world, "launched")["pod_id"]
    world.settle_fails = orch.TERMINATE_ATTEMPTS
    assert make(world).cleanup("_A", "launch failed", retrieve=False) == 1
    ev = last_event(world, "cleaned")
    assert ev["terminated"] is True and ev["settled"] is False
    assert ev["reservation"].startswith("kept") and not rr.get(rid)["closed"]
    assert B.running_slots(world.root) == []
    make(world).recover()
    assert rr.state(exp="E3")["pods"][pid]["settled"] and rr.get(rid)["closed"]


def test_recover_settles_a_pod_terminated_by_all_exp(world):
    rid = _launched(world)
    pid = last_event(world, "launched")["pod_id"]
    world.terminate_event(pid)
    make(world).recover()
    assert rr.state(exp="E3")["pods"][pid]["settled"]
    assert any(a.get("settle_terminated") == pid for a in last_event(world, "recover")["actions"])
    assert rr.get(rid)["closed"]


# --- A8: a §7 stop is terminal ------------------------------------------------------------------


def test_a_stop_cleans_up_every_pod_closes_every_reservation_and_refuses_more(world, capsys):
    _launched(world, "_A", "pod1")
    _launched(world, "_B", "pod2")
    job(world, "_A", "pod1")
    open_rid = rr.reserve("E3", 2.0, "E3 unlaunched")
    spend.reserve_api("E3", 0.5, "unused")
    o = make(world)
    assert o.stop("stop", "a Stage A check failed", detail="power") == 2
    assert B.read_stopped(world.root)["rule"] == "a Stage A check failed"
    assert B.running_slots(world.root) == []
    assert all(R["closed"] for R in rr.state(exp="E3")["reservations"].values())
    assert rr.get(open_rid)["closed"] and spend.open_reservations("E3") == {}
    assert world.count("pod_pull_results.sh") == 1  # only the slot whose job ran is retrieved
    capsys.readouterr()
    # terminal: launch, api-reserve and job refuse
    n = len(world.calls)
    assert o.launch("pod1", "_C") == 4 and len(world.calls) == n
    assert o.api("reserve", 0.5, "judge") == 4 and spend.open_reservations("E3") == {}
    assert orch.start_job("pod1", "_A", root=world.root, wait_s=0) == 4
    assert "E3 is stopped" in capsys.readouterr().err
    # a second stop keeps the first rule
    assert o.stop("stop", "another rule") == 2
    assert B.read_stopped(world.root)["rule"] == "a Stage A check failed"


def test_three_failed_retrievals_stop_e3_and_other_pods_are_cleaned(world):
    _launched(world, "_A", "pod1")
    _launched(world, "_B", "pod2")
    job(world, "_A", "pod1")
    world.pull_rc = 1
    assert make(world).cleanup("_A", "end of job") == 2
    assert B.read_stopped(world.root)["rule"] == B.STOP_RETRIEVAL
    assert B.running_slots(world.root) == []  # _B too
    assert make(world).launch("pod1", "_A") == 4


def test_a_launch_failure_before_any_job_is_recoverable(world):
    """A8: an infrastructure failure before the job ran is not a §7 stop: retry allowed."""
    world.push_rc = 1
    o = make(world)
    assert o.launch("pod1", "_A") == 1
    assert B.read_stopped(world.root) is None
    world.push_rc = 0
    assert o.launch("pod1", "_A") == 0


def test_project_is_read_only_and_launch_enforces_the_stop(world, capsys):
    _launched(world)
    spend.record("gpu", "earlier", 1, 5.0, exp="E3")
    o = make(world)
    assert o.project() == 2  # over a limit after cuts: reported, nothing written
    assert B.read_stopped(world.root) is None
    assert '"would_stop": true' in capsys.readouterr().out
    assert o.launch("pod2", "_B") == 2  # where spend is committed, the §7 rule stops E3
    assert B.read_stopped(world.root)["rule"] == B.STOP_OVER_LIMIT
    assert B.running_slots(world.root) == []


def test_pairs_line_counts_only_the_unspent_allowance():
    st = B.empty_state()
    st["pairs_spent_api"] = 2.47
    items = {i["name"]: i["usd"] for i in B.project(st)["items"]}
    assert abs(items["pairs_v1"] - 0.53) < 1e-6
    st["pairs_spent_api"] = 3.5
    assert {i["name"]: i["usd"] for i in B.project(st)["items"]}["pairs_v1"] == 0.0


def test_the_deadline_stop_is_terminal(world):
    _launched(world)
    world.now = C.DEADLINE_UTC + timedelta(seconds=1)
    o = make(world)
    assert o.deadline_check() == 2
    assert B.read_stopped(world.root)["rule"] == B.STOP_WALL_CLOCK
    assert o.launch("pod2", "_B") == 4


def test_recover_finishes_an_interrupted_stop(world):
    _launched(world)
    B.write_stopped(world.root, "a Stage A check failed", None)  # the stopping process died
    make(world).recover()
    assert B.running_slots(world.root) == []
    assert all(R["closed"] for R in rr.state(exp="E3")["reservations"].values())


def test_stop_subcommand(world, capsys):
    _launched(world)
    d = make(world).d
    assert orch.main(["stop", "--rule", "masking failed", "--no-retrieve"], deps=d) == 2
    assert B.read_stopped(world.root)["rule"] == "masking failed"
    assert B.running_slots(world.root) == []


# --- A9: pod 3 waits for the rules review; no speculative path ----------------------------------


def test_pod3_needs_a_passed_rules_review(world, capsys):
    o = make(world)
    assert o.launch("pod3", "_C") == 4
    assert world.count("pod_up.py") == 0 and rr.read_rows() == []
    assert orch.start_job("pod3", "_C", root=world.root, wait_s=0) == 4
    review = world.root / B.REVIEW_17R_FILE
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(json.dumps({"outcome": "blocking"}))
    assert o.launch("pod3", "_C") == 4
    review.write_text(json.dumps({"outcome": "pass"}))
    assert o.launch("pod3", "_C") == 0
    assert "pod 3 needs" in capsys.readouterr().err + "".join(
        json.dumps(e) for e in B.read_log(world.root)
    )


def test_no_launch_or_job_path_uses_the_speculative_allowance():
    assert "speculative_ok" not in _SCRIPT.read_text()


# --- review A r2, finding 1: supervision never waits behind a retrieval -------------------------


def test_an_overdue_pod_is_terminated_on_schedule_while_another_pods_retrieval_hangs(world):
    """Pod A's results are being pulled (a 36-minute retrieval: its cleanup hangs in the
    background and holds A's retrieval lock); pod B's launcher died before arming. At B's
    15-minute deadline, both `enforce` and `recover` terminate B at once, without retrieval."""
    _launched(world, "_A", "pod1")
    job(world, "_A", "pod1")
    world.ack_on_push = False
    rid_b = _launched(world, "_B", "pod2")
    pid_b = last_event(world, "launched")["pod_id"]
    world.api[0]["desiredStatus"] = "EXITED"  # A stopped: recover hands it to a cleanup
    world.spawn_hang = {"cleanup_A"}  # ... which hangs in its retrieval
    o = make(world)
    o.recover()
    assert [c[c.index("--slot") + 1] for c in world.spawned] == ["_A"]
    world.spawned.clear()
    # the spawned cleanup now sits in A's retrieval, holding A's retrieval lock
    with orch._flock(world.root / "logs" / "E3-retrieve_A.lock"):
        assert {p["slot"] for p in B.running_slots(world.root)} == {"_A", "_B"}
        world.now = NOW + timedelta(minutes=15, seconds=1)
        t0 = time.monotonic()
        assert o.enforce() == 0
        assert time.monotonic() - t0 < 5
        assert [p["slot"] for p in B.running_slots(world.root)] == ["_A"]
        assert rr.state(exp="E3")["pods"][pid_b]["settled"] and rr.get(rid_b)["closed"]
        o.recover()  # A's cleanup is still running: not spawned again
        assert not any("--no-retrieve" not in c for c in world.spawned)
    assert world.count("pod_pull_results.sh") == 0
    ev = last_event(world, "cleaned")
    assert ev["slot"] == "_B" and ev["reason"].startswith("enforce: unarmed")


def test_recover_runs_enforcement_and_spawns_retrieval_cleanups(world):
    _launched(world, "_A", "pod1")
    job(world, "_A", "pod1")
    world.api[0]["desiredStatus"] = "EXITED"
    world.spawn_hang = {"cleanup_A"}
    make(world).recover()
    cmd = world.spawned[0]
    assert cmd[cmd.index("cleanup") :][:3] == ["cleanup", "--slot", "_A"]
    assert world.count("pod_pull_results.sh") == 0  # recover itself never pulls
    assert last_event(world, "cleanup_spawned")["slot"] == "_A"


def test_enforce_subcommand(world):
    world.ack_on_push = False
    _launched(world, "_B", "pod2")
    world.now = NOW + timedelta(minutes=16)
    assert orch.main(["enforce"], deps=make(world).d) == 0
    assert B.running_slots(world.root) == []


# --- review A r3, finding 1: one pod's stalled termination never delays another's ---------------


def test_enforcement_dispatches_each_overdue_pods_termination_independently(world):
    """A's DELETE stalls; B is overdue at the same time. The enforce pass returns at once, and
    B's DELETE is attempted while A's is still stalled (well within the supervision interval)."""
    world.ack_on_push = False  # neither pod is ever armed
    _launched(world, "_A", "pod1")
    pid_a = last_event(world, "launched")["pod_id"]
    _launched(world, "_B", "pod2")
    pid_b = last_event(world, "launched")["pod_id"]
    stall, b_called = threading.Event(), threading.Event()

    def hook(pid):
        if pid == pid_a:
            stall.wait(15)  # A's termination stalls
        elif pid == pid_b:
            b_called.set()

    world.down_hook = hook
    world.spawn_threads = True
    world.now = NOW + timedelta(minutes=16)
    t0 = time.monotonic()
    try:
        assert make(world).enforce() == 0
        assert time.monotonic() - t0 < 3  # the pass waits for no termination
        assert b_called.wait(5)  # B's DELETE attempted ...
        assert not stall.is_set()  # ... while A's is still stalled
    finally:
        stall.set()
        for t in world.threads:
            t.join(20)
    assert B.running_slots(world.root) == []
    assert all(R["closed"] for R in rr.state(exp="E3")["reservations"].values())
    names = [c[c.index("--slot") + 1] for c in world.spawned]
    assert sorted(names) == ["_A", "_B"]
    assert all("--no-retrieve" in c and "--enforced" in c for c in world.spawned)


def test_a_pod_past_its_deadline_after_its_job_started_is_a_retrieval_stop(world):
    """Review A r3 (the judgement call): terminated for its deadline without retrieval after a
    job had started: its artifacts are recorded missing (run log and unit manifest), and §7's
    retrieval stop is written after the termination."""
    _launched(world)
    job(world)
    pid = last_event(world, "launched")["pod_id"]
    world.now = datetime.fromtimestamp(rr.pod_deadline(pid) + 1, UTC)
    make(world).enforce()
    assert B.running_slots(world.root) == []
    assert B.read_stopped(world.root)["rule"] == B.STOP_RETRIEVAL
    man = (world.root / C.manifest_path("pod1")).read_text()
    assert "# MISSING" in man and "past its deadline" in man
    assert "missing artifacts" in (world.root / C.REPORT_LOG).read_text()
    log = B.read_log(world.root)
    i_term = next(i for i, e in enumerate(log) if e.get("event") == "cleaned")
    i_stop = next(i for i, e in enumerate(log) if e.get("event") == "stop")
    assert log[i_term]["terminated"] is True and i_term < i_stop
    assert world.count("pod_pull_results.sh") == 0


def test_an_enforced_pod_with_a_complete_unit_or_no_job_is_not_a_stop(world):
    _launched(world, "_A", "pod1")
    job(world, "_A", "pod1")
    write_done(world.root, "pod1", last_event(world, "launched")["pod_id"])
    pid_a = last_event(world, "launched")["pod_id"]
    _launched(world, "_B", "pod2")  # no job started on B
    pid_b = last_event(world, "launched")["pod_id"]
    world.now = datetime.fromtimestamp(max(rr.pod_deadline(pid_a), rr.pod_deadline(pid_b)) + 1, UTC)
    make(world).enforce()
    assert B.running_slots(world.root) == []
    assert B.read_stopped(world.root) is None


# --- review A2 r1 ------------------------------------------------------------------------------


def test_a_stalled_orphan_creation_never_delays_an_overdue_pods_delete(world):
    """Finding 1: an orphaned creation (its untracked pod's reap stalls) and an overdue launched
    pod. The launched pod's DELETE goes out at once; the creation is reconciled in its own
    background process."""
    rid_c = rr.reserve("E3", 6.5, "E3 pod3 slot _C")
    rr.creating(
        rid_c, slot="_C", name="e3-_C", budget_usd=6.5, rate_usd_h=RATE, t0_epoch=NOW.timestamp()
    )
    world.ack_on_push = False
    _launched(world, "_B", "pod2")
    pid_b = last_event(world, "launched")["pod_id"]
    world.api.append({"id": "orphan", "name": "e3-_C", "desiredStatus": "RUNNING"})
    stall, b_called, reaping = threading.Event(), threading.Event(), threading.Event()
    world.untracked_hook = lambda: (reaping.set(), stall.wait(15))  # the orphan's reap stalls
    world.down_hook = lambda pid: b_called.set() if pid == pid_b else None
    world.spawn_threads = True
    world.now = NOW + timedelta(minutes=16)
    t0 = time.monotonic()
    try:
        assert make(world).enforce() == 0
        assert time.monotonic() - t0 < 3
        assert b_called.wait(5) and not stall.is_set()  # B's DELETE while the reap stalls
        assert reaping.wait(5)
        assert make(world).enforce() == 0  # a second pass while the reconciliation runs ...
        assert sum("reconcile-creating" in c for c in world.spawned) == 1  # ... starts none
    finally:
        stall.set()
        for th in world.threads:
            th.join(20)
    assert B.running_slots(world.root) == [] and rr.get(rid_c)["closed"]


def test_an_unconfirmed_termination_records_nothing_and_is_retried(world):
    """Finding 2: the DELETE fails. No artifact is recorded missing and no stop fires; the
    next enforcement pass retries, and only the confirmed termination decides the loss."""
    _launched(world)
    job(world)
    pid = last_event(world, "launched")["pod_id"]
    world.now = datetime.fromtimestamp(rr.pod_deadline(pid) + 1, UTC)
    world.down_rc = 1
    make(world).enforce()
    assert B.read_stopped(world.root) is None
    assert not (world.root / C.manifest_path("pod1")).exists()
    assert B.running_slots(world.root) != []
    assert last_event(world, "cleanup_failed")["missing"] == []
    world.down_rc = 0
    make(world).enforce()  # the next pass
    assert B.running_slots(world.root) == []
    assert B.read_stopped(world.root)["rule"] == B.STOP_RETRIEVAL


def test_no_retrieval_stop_while_a_retrieval_of_the_slot_is_in_progress(world):
    _launched(world)
    job(world)
    pid = last_event(world, "launched")["pod_id"]
    world.now = datetime.fromtimestamp(rr.pod_deadline(pid) + 1, UTC)
    with orch._flock(world.root / "logs" / "E3-retrieve_A.lock"):  # a cleanup is pulling
        make(world).enforce()
    assert B.running_slots(world.root) == []  # terminated all the same
    assert B.read_stopped(world.root) is None
    assert not (world.root / C.manifest_path("pod1")).exists()
    assert "retrieval is in progress" in last_event(world, "cleaned")["loss_undecided"]


# --- review A2 r2: a deferred loss decision survives its holder -------------------------------


def _holder_then_enforce(world, pull_rc: int, done: bool):
    """A cleanup is pulling pod1's results (holding the retrieval lock) when the pod passes its
    deadline: enforcement terminates it and defers the loss decision. Then the pull finishes."""
    _launched(world)
    job(world)
    pid = last_event(world, "launched")["pod_id"]
    pulling, finish = threading.Event(), threading.Event()

    def hook():
        pulling.set()
        assert finish.wait(15)
        if done:
            write_done(world.root, "pod1", pid)

    world.pull_hook = hook
    world.pull_rc = pull_rc
    out = {}
    holder = threading.Thread(target=lambda: out.update(rc=make(world).cleanup("_A", "end")))
    holder.start()
    assert pulling.wait(10)
    world.now = datetime.fromtimestamp(rr.pod_deadline(pid) + 1, UTC)
    make(world).enforce()
    pending = world.root / orch.PENDING_LOSS_DIR / "_A.json"
    assert json.loads(pending.read_text())["pod_id"] == pid  # persisted, atomically
    assert B.read_stopped(world.root) is None
    finish.set()
    holder.join(30)
    return out["rc"], pending


def _stops(world) -> int:
    return sum(
        e.get("event") == "stop" and e.get("stop") == B.STOP_RETRIEVAL
        for e in B.read_log(world.root)
    )


def test_a_deferred_loss_is_cleared_when_the_holders_retrieval_succeeds(world):
    rc, pending = _holder_then_enforce(world, pull_rc=0, done=True)
    assert rc == 0 and not pending.exists()
    make(world).recover()
    assert B.read_stopped(world.root) is None and _stops(world) == 0


def test_a_deferred_loss_is_resolved_by_the_holders_exhausted_retrieval(world):
    rc, pending = _holder_then_enforce(world, pull_rc=1, done=False)
    assert rc == 2 and not pending.exists()
    assert B.read_stopped(world.root)["rule"] == B.STOP_RETRIEVAL
    make(world).recover()
    make(world).recover()
    assert _stops(world) == 1


def test_a_dead_holders_deferred_loss_is_resolved_by_recover_once(world):
    _launched(world)
    job(world)
    pid = last_event(world, "launched")["pod_id"]
    world.now = datetime.fromtimestamp(rr.pod_deadline(pid) + 1, UTC)
    lock = world.root / "logs" / "E3-retrieve_A.lock"
    with orch._flock(lock):  # the holder ...
        make(world).enforce()
        make(world).recover()  # still held: undecided, kept
        pending = world.root / orch.PENDING_LOSS_DIR / "_A.json"
        assert pending.exists() and B.read_stopped(world.root) is None
    # ... dies (lock released, nothing recorded); recover resolves it
    make(world).recover()
    assert not pending.exists()
    assert B.read_stopped(world.root)["rule"] == B.STOP_RETRIEVAL
    assert "# MISSING" in (world.root / C.manifest_path("pod1")).read_text()
    make(world).recover()
    make(world).enforce()
    assert _stops(world) == 1


# --- review A2 r3: the stop is durable before the record goes, and claimed once ---------------


def _sweeps(world) -> int:
    return sum(
        e.get("event") == "shutdown" and str(e.get("reason", "")).startswith("stop:")
        for e in B.read_log(world.root)
    )


class Crash(BaseException):
    """A process dying at an injected point (not caught by the code's Exception handlers)."""


def _dead_holder_pending(world) -> tuple[str, str]:
    """Pod _A was terminated past its deadline while a (now dead) holder retrieved: a pending
    loss decision. Pod _B is still running, so the stop's sweep has something to do."""
    _launched(world, "_A", "pod1")
    job(world, "_A", "pod1")
    pid = last_event(world, "launched")["pod_id"]
    world.now = datetime.fromtimestamp(rr.pod_deadline(pid) + 1, UTC)
    with orch._flock(world.root / "logs" / "E3-retrieve_A.lock"):
        make(world).enforce()
    _launched(world, "_B", "pod2")  # launched now: not overdue, only the sweep stops it
    return pid, "_A"


@pytest.mark.parametrize("crash_at", ["_clear_pending_loss", "_sweep"])
def test_a_recover_interrupted_in_the_loss_handoff_is_completed_once(world, monkeypatch, crash_at):
    """Interrupted between the stop write and the clear, or between the clear and the sweep:
    the next recover completes the stop (pod _B swept), with exactly one stop event."""
    _dead_holder_pending(world)
    pending = world.root / orch.PENDING_LOSS_DIR / "_A.json"
    assert pending.exists()
    real = getattr(orch.Orchestrator, crash_at)

    def crash(self, *a, **k):
        raise Crash(crash_at)

    monkeypatch.setattr(orch.Orchestrator, crash_at, crash)
    with pytest.raises(Crash):
        make(world).recover()
    assert B.read_stopped(world.root)["rule"] == B.STOP_RETRIEVAL  # durable before the crash
    monkeypatch.setattr(orch.Orchestrator, crash_at, real)
    make(world).recover()
    assert not pending.exists()
    assert B.running_slots(world.root) == []  # the shutdown was completed
    assert all(R["closed"] for R in rr.state(exp="E3")["reservations"].values())
    make(world).recover()
    assert _stops(world) == 1


def test_the_holder_and_recover_claim_the_stop_once(world, monkeypatch):
    """The exhausted holder is paused after releasing its retrieval lock and before its stop;
    recover resolves the deferred decision and claims the stop; the resumed holder finds it
    claimed: one stop event, one sweep."""
    _launched(world, "_B", "pod2")
    _launched(world)
    job(world)
    pid = last_event(world, "launched")["pod_id"]
    pulling, finish = threading.Event(), threading.Event()
    paused, resume = threading.Event(), threading.Event()
    world.pull_rc = 1

    def hook():
        pulling.set()
        assert finish.wait(15)

    world.pull_hook = hook
    real = orch.Orchestrator._cleanup

    def pausing(self, slot, *a, **k):
        res = real(self, slot, *a, **k)
        if threading.current_thread().name == "holder":
            paused.set()
            assert resume.wait(15)
        return res

    monkeypatch.setattr(orch.Orchestrator, "_cleanup", pausing)
    out = {}
    holder = threading.Thread(
        target=lambda: out.update(rc=make(world).cleanup("_A", "end")), name="holder"
    )
    holder.start()
    assert pulling.wait(10)
    world.now = datetime.fromtimestamp(rr.pod_deadline(pid) + 1, UTC)
    make(world).enforce()  # terminates _A, defers the loss decision
    finish.set()
    assert paused.wait(15)  # the holder: retrieval exhausted, lock released, stop not yet run
    make(world).recover()  # resolves the deferred decision first
    resume.set()
    holder.join(30)
    assert out["rc"] == 2
    assert _stops(world) == 1 and _sweeps(world) == 1
    assert any(e.get("event") == "stop_already" for e in B.read_log(world.root))
    assert B.running_slots(world.root) == []


# --- review A r2, finding 3: a waiting launch never escapes a stop ------------------------------


def test_a_stop_while_a_launch_waits_for_balance_cancels_it(world, monkeypatch):
    monkeypatch.setattr(rr, "balance", lambda *a, **k: 0.0)  # nothing admitted yet
    o = make(world)
    waits = []

    def sleep(seconds):  # another process stops E3 while this launch waits
        waits.append(seconds)
        assert make(world).stop("stop", "a Stage A check failed") == 2
        monkeypatch.setattr(rr, "balance", lambda *a, **k: 1000.0)

    o.d.sleep = sleep
    assert o.launch("pod1", "_A") == 4
    assert waits and world.count("pod_up.py") == 0
    assert rr.state(exp="E3")["reservations"] == {}  # never reserved
    assert last_event(world, "refused")["why"].startswith("while waiting")


def test_a_stop_right_after_admission_refuses_and_releases(world, monkeypatch):
    real = rr.wait_and_reserve

    def admitted_then_stopped(*a, **k):
        rid = real(*a, **k)
        B.write_stopped(world.root, "masking failed", None)  # the stop lands here
        return rid

    monkeypatch.setattr(rr, "wait_and_reserve", admitted_then_stopped)
    assert make(world).launch("pod1", "_A") == 4
    assert world.count("pod_up.py") == 0
    rid = last_event(world, "launch_refused")["reservation_id"]
    assert rr.get(rid)["closed"]


def test_the_cap_passing_during_the_wait_stops_without_creating(world, monkeypatch):
    monkeypatch.setattr(rr, "balance", lambda *a, **k: 0.0)
    o = make(world)

    def sleep(seconds):
        world.now = C.DEADLINE_UTC + timedelta(seconds=1)

    o.d.sleep = sleep
    assert o.launch("pod1", "_A") == 2
    assert world.count("pod_up.py") == 0
    assert B.read_stopped(world.root)["rule"] == B.STOP_WALL_CLOCK


def test_a_stop_waits_for_an_in_flight_pod_up_and_sweeps_its_pod(world):
    """The stop writes STOPPED.json, then takes the create lock: it waits for the pod_up in
    flight, and its sweep then terminates the new pod."""
    started, go = threading.Event(), threading.Event()
    world.up_hook = lambda: (started.set(), go.wait(10))
    out: dict[str, int] = {}
    t_launch = threading.Thread(target=lambda: out.update(launch=make(world).launch("pod1", "_A")))
    t_launch.start()
    assert started.wait(10)
    t_stop = threading.Thread(target=lambda: out.update(stop=make(world).stop("stop", "x")))
    t_stop.start()
    time.sleep(0.5)
    assert t_stop.is_alive()  # blocked on the create lock while pod_up runs
    assert B.read_stopped(world.root) is not None  # ... with STOPPED.json already written
    go.set()
    t_launch.join(20)
    t_stop.join(20)
    assert not t_launch.is_alive() and not t_stop.is_alive()
    assert out["stop"] == 2 and out["launch"] != 0
    assert world.count("pod_up.py") == 1 and world.count("pod_push.sh") == 0
    assert B.running_slots(world.root) == []
    assert all(R["closed"] for R in rr.state(exp="E3")["reservations"].values())
    assert world.timeouts["up"] == 1800.0  # pod_up is bounded


def test_budget_module_is_import_light():
    import subprocess

    code = (
        "import sys; import emx.e3.budget; "
        "heavy = [m for m in ('emx.spend', 'emx.runpod', 'emx.runpod_reservations', 'requests', "
        "'pandas', 'torch') if m in sys.modules]; assert not heavy, heavy"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
