"""Shared RunPod reservations (emx.runpod_reservations) and the E3 pod-tooling changes in
emx.runpod, scripts/pod_up.py and scripts/pod_down.py (E3 implementation plan v2 fix 5, v3 fixes
4-5, v4 fixes 2-3; docs/experiments/shared/runpod-reservations.md). Every RunPod call is faked;
the reservations file, the spend ledger, .env and the pod event log live in tmp_path."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from emx import runpod as rp
from emx import runpod_reservations as rr
from emx import spend

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
H = 3600.0


@pytest.fixture(autouse=True)
def resfile(tmp_path, monkeypatch):
    p = tmp_path / "runpod_reservations.jsonl"
    monkeypatch.setenv(rr.RESERVATIONS_ENV, str(p))
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    return p


@pytest.fixture
def e3(tmp_path, monkeypatch):
    """emx.runpod configured for E3 in tmp_path (.env, event log, live watchdog heartbeat)."""
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS", "LOGS", "ROOT"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    monkeypatch.setattr(rp, "forget_host_key", lambda h, p: None)
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=test-key\n")
    (tmp_path / "watchdog_E3.heartbeat").write_text(f"{time.time()} 600 {os.getpid()}\n")
    rp.configure("E3")
    return tmp_path


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"_rr_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def launched(exp, usd, pods, t0, balance=1000.0):
    rid = rr.reserve(exp, usd, "t", balance=balance)
    return rid, rr.launch(rid, pods, t0)


# --- the launch rule ----------------------------------------------------------------------------


def test_launch_rule_counts_every_unlaunched_reservation():
    rr.reserve("E9", 50.0, "e9", balance=100.0)
    ok, d = rr.admit("E3", 40.0, balance=100.0)  # 100 - 50 - 10 = 40
    assert ok and d["unlaunched_usd"] == 50.0 and d["available_usd"] == 40.0
    assert rr.admit("E3", 40.01, balance=100.0)[0] is False
    with pytest.raises(rr.NotAdmitted) as e:
        rr.reserve("E3", 41.0, "too much", balance=100.0)
    assert e.value.detail["available_usd"] == 40.0
    assert rr.state()["unlaunched_usd"] == 50.0  # the refused one was never appended


def test_same_experiment_reserve_reserve_launch_counts_each_dollar_once():
    a = rr.reserve("E3", 30.0, "pod1", balance=75.0)
    with pytest.raises(rr.NotAdmitted):  # 75 - 30 - 10 = 35 < 36: its own first one counts
        rr.reserve("E3", 36.0, "pod2", balance=75.0)
    b = rr.reserve("E3", 35.0, "pod2", balance=75.0)
    assert rr.state()["unlaunched_usd"] == 65.0
    t0 = time.time()
    rr.launch(a, [("pa", 3.0, 30.0)], t0)
    st = rr.state(now=t0)
    assert st["unlaunched_usd"] == 35.0 and st["launched_liability_usd"] == 30.0  # not 60
    assert not rr.admit("E3", 0.01, balance=75.0, now=t0)[0]
    # replacing excludes the replaced reservation and releases it in the same section
    c = rr.reserve("E3", 35.0, "pod2 again", replacing=b, balance=75.0, now=t0)
    assert rr.get(b)["closed"] and rr.state(now=t0)["unlaunched_usd"] == 35.0
    assert rr.get(c)["open_usd"] == 35.0


def test_e3_and_e9_concurrent_admission():
    bal = 174.18  # the 2026-10-06 balance
    e9 = rr.reserve("E9", 130.0, "E9 GPU", balance=bal)
    with pytest.raises(rr.NotAdmitted):  # 174.18 - 130 - 10 = 34.18 < 35
        rr.reserve("E3", 35.0, "E3 whole cap", balance=bal)
    e3 = rr.reserve("E3", 13.25, "E3 pod1", balance=bal)
    t0 = time.time()
    rr.launch(e9, [("e9a", 2.69, 100.0), ("e9b", 2.69, 30.0)], t0)
    st = rr.state(now=t0)
    assert st["unlaunched_usd"] == 13.25 and st["launched_liability_usd"] == 130.0
    ok, d = rr.admit("E3", 12.25, balance=bal, now=t0)  # 174.18 - 13.25 - 130 - 10 = 20.93
    assert ok and d["available_usd"] == pytest.approx(20.93)
    rr.release(e3)
    assert rr.state(now=t0)["unlaunched_usd"] == 0.0


def test_threads_racing_inside_the_lock_admit_exactly_one(monkeypatch):
    real = rr._reduce

    def slow(rows):
        time.sleep(0.15)  # widen the read-check-append window
        return real(rows)

    monkeypatch.setattr(rr, "_reduce", slow)
    results: list = []

    def go(i):
        try:
            results.append(rr.reserve("E3" if i else "E9", 30.0, f"r{i}", balance=50.0))
        except rr.NotAdmitted as e:
            results.append(e)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(isinstance(r, str) for r in results) == 1
    assert len([r for r in rr.read_rows() if r["kind"] == "reserve"]) == 1


def test_processes_racing_reserve_admit_exactly_one(resfile):
    env = {**os.environ, rr.RESERVATIONS_ENV: str(resfile)}
    cmd = [sys.executable, "-m", "emx.runpod_reservations", "reserve", "--exp", "E3"]
    procs = [
        subprocess.Popen(
            [*cmd, "--usd", "30", "--balance", "50", "--note", f"p{i}"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(6)
    ]
    codes = [p.wait(timeout=60) for p in procs]
    assert sorted(codes) == [0, 3, 3, 3, 3, 3]
    assert len([r for r in rr.read_rows() if r["kind"] == "reserve"]) == 1


def test_wait_and_reserve_waits_logs_and_gives_up():
    blocker = rr.reserve("E9", 40.0, "e9", balance=60.0)
    clock = {"t": 1000.0}
    logs: list[str] = []

    def sleep(s):
        clock["t"] += s
        if clock["t"] >= 1600:
            rr.release(blocker)

    rid = rr.wait_and_reserve(
        "E3",
        30.0,
        "pod1",
        give_up_at=5000,
        poll_s=300,
        balance_fn=lambda: 60.0,
        sleep=sleep,
        clock=lambda: clock["t"],
        log=logs.append,
    )
    assert rr.get(rid)["open_usd"] == 30.0 and len(logs) == 2 and "waiting" in logs[0]
    with pytest.raises(rr.NotAdmitted):
        rr.wait_and_reserve(
            "E9",
            30.0,
            "x",
            give_up_at=clock["t"] + 100,
            poll_s=300,
            balance_fn=lambda: 60.0,
            sleep=sleep,
            clock=lambda: clock["t"],
            log=logs.append,
        )


# --- launch, deadlines and liability ------------------------------------------------------------


def test_launch_unequal_budgets_and_sum_asserted():
    rid = rr.reserve("E3", 20.0, "two pods", balance=100.0)
    t0 = 1_800_000_000.0
    with pytest.raises(rr.ReservationError, match="exceed"):
        rr.launch(rid, [("a", 2.0, 12.0), ("b", 4.0, 8.01)], t0)
    dl = rr.launch(rid, [("a", 2.0, 12.0), ("b", 4.0, 8.0)], t0)
    assert dl == {"a": int(t0 + 6 * H), "b": int(t0 + 2 * H)}  # t0 + budget / rate
    with pytest.raises(rr.ReservationError):  # converted once
        rr.launch(rid, [("c", 2.0, 1.0)], t0)
    # the global kill-line deadline caps it
    r2 = rr.reserve("E3", 5.0, "capped", balance=100.0)
    assert rr.launch(r2, [("d", 1.0, 5.0)], t0, global_deadline=t0 + H) == {"d": int(t0 + H)}


def test_liability_full_budget_until_acked_then_rate_times_remaining():
    t0 = 1_800_000_000.0
    _, dl = launched("E3", 13.0, [("p", 2.6, 13.0)], t0)  # 5 h
    assert rr.state(now=t0 + 4 * H)["launched_liability_usd"] == 13.0  # unacked: full budget
    rr.ack("p", dl["p"])
    assert rr.state(now=t0 + 4 * H)["launched_liability_usd"] == pytest.approx(2.6)
    assert rr.state(now=t0 + 6 * H)["launched_liability_usd"] == 0.0  # past the deadline
    assert rr.acked_deadline("p") == dl["p"] == rr.pod_deadline("p")


def test_ack_later_refused_shortening_ok_and_rearm_never_extends():
    t0 = 1_800_000_000.0
    _, dl = launched("E3", 10.0, [("p", 2.0, 10.0)], t0)
    with pytest.raises(rr.ReservationError, match="later"):
        rr.ack("p", dl["p"] + 1)
    with pytest.raises(rr.ReservationError, match="unknown pod"):
        rr.ack("nope", dl["p"])
    rr.ack("p", dl["p"] - 3600)  # shortened
    with pytest.raises(rr.ReservationError):  # cannot come back up without extend
        rr.ack("p", dl["p"])
    assert rr.pod_deadline("p") == dl["p"] - 3600
    # an API release raises the global deadline; the target stays at the shorter ack
    assert rr.rearm_target("p", dl["p"] + 10 * H) == dl["p"] - 3600
    assert rr.rearm_target("p", dl["p"] - 7200) == dl["p"] - 7200  # a lower global wins
    assert rr.rearm_target("unknown", 123) == 123


def test_extension_requires_admission():
    t0 = time.time()
    rid, dl = launched("E3", 10.0, [("p", 2.0, 10.0)], t0, balance=30.0)
    with pytest.raises(rr.ReservationError, match="ack first"):
        rr.extend("p", dl["p"] + 3600, balance=30.0, now=t0)
    rr.ack("p", dl["p"])
    rr.reserve("E9", 9.0, "e9", balance=30.0, now=t0)  # 30 - 10 - 9 - 10 = 1 left
    with pytest.raises(rr.NotAdmitted):  # +1 h at $2/h = $2 > $1
        rr.extend("p", dl["p"] + 3600, balance=30.0, now=t0)
    with pytest.raises(rr.ReservationError):  # not later: that is a shortening
        rr.extend("p", dl["p"] - 60, balance=30.0, now=t0)
    ext = rr.extend("p", dl["p"] + 1800, balance=30.0, now=t0)  # $1
    assert rr.pod_deadline("p") == dl["p"] + 1800
    assert rr.rearm_target("p", dl["p"] + 9999) == dl["p"]  # automatic re-arm: still the ack
    rr.ack("p", dl["p"] + 1800)  # the caller arms the extension, then acks it
    assert rr.state(now=t0)["launched_liability_usd"] == pytest.approx(11.0, abs=1e-3)
    assert rr.state(now=t0)["unlaunched_usd"] == 9.0  # the extension is not double counted
    rr.settle(rid, 4.0, pod_id="p")
    assert rr.get(ext)["closed"] and rr.state(now=t0)["launched_liability_usd"] == 0.0


def test_settle_and_release_are_idempotent_and_close_once_all_pods_settle():
    t0 = time.time()
    rid, _ = launched("E3", 20.0, [("a", 2.0, 12.0), ("b", 4.0, 8.0)], t0)
    r1 = rr.settle(rid, 3.0, pod_id="a")
    assert rr.settle(rid, 3.0, pod_id="a") == r1  # nothing appended
    assert not rr.get(rid)["closed"]
    assert rr.state(now=t0)["launched_liability_usd"] == 8.0  # b still unacked
    rr.settle(rid, 1.0, pod_id="b")
    assert rr.get(rid)["closed"] and rr.get(rid)["settled_usd"] == 4.0
    rel = rr.release(rid)
    assert rel["usd"] == 16.0 and rr.release(rid) is None
    assert len([r for r in rr.read_rows() if r["kind"] == "settle"]) == 2
    with pytest.raises(rr.ReservationError):
        rr.settle(rid, 1.0, pod_id="zzz")


def test_release_of_launched_reservation_keeps_pod_liability():
    t0 = time.time()
    rid, _ = launched("E3", 10.0, [("p", 2.0, 10.0)], t0)
    rr.release(rid)
    assert rr.state(now=t0)["launched_liability_usd"] == 10.0


def test_killed_launcher_leaves_creating_row_found_by_unarmed_overdue():
    rid = rr.reserve("E3", 13.0, "pod1", balance=100.0)
    t0 = 1_800_000_000.0
    rr.creating(rid, slot="_A", name="e3-a", budget_usd=13.0, rate_usd_h=2.6, t0_epoch=t0)
    # (the launcher dies here, before any create response or launch row)
    assert rr.unarmed_overdue(now=t0 + 899) == []
    (o,) = rr.unarmed_overdue(now=t0 + 900)
    assert o["kind"] == "creating" and o["pod_id"] is None and o["name"] == "e3-a"
    assert o["reservation_id"] == rid and o["due_epoch"] == int(t0 + 900)
    # a budget shorter than 15 minutes is due at its budget deadline
    r2 = rr.reserve("E9", 0.5, "tiny", balance=100.0)
    rr.creating(r2, slot="_B", name="e9-b", budget_usd=0.5, rate_usd_h=6.0, t0_epoch=t0)
    assert [x["name"] for x in rr.unarmed_overdue(now=t0 + 300)] == ["e9-b"]
    # an earlier attempt superseded by a launch is not reported
    rr.launch(rid, [("pa", 2.6, 13.0)], t0)
    names = {(x["kind"], x["pod_id"]) for x in rr.unarmed_overdue(now=t0 + 900)}
    assert names == {("launch", "pa"), ("creating", None)}  # pa unarmed; e9-b's creating row
    rr.ack("pa", t0 + 5 * H)
    rr.release(r2)
    assert rr.unarmed_overdue(now=t0 + 900) == []
    (late,) = rr.unarmed_overdue(now=t0 + 5 * H)
    assert late["reason"] == "past_deadline" and late["slot"] == "_A"


def test_creating_refuses_budget_above_reservation():
    rid = rr.reserve("E3", 5.0, "x", balance=100.0)
    with pytest.raises(rr.ReservationError):
        rr.creating(rid, slot="_A", name="e3-a", budget_usd=6.0, rate_usd_h=2.0, t0_epoch=0.0)


def test_snapshot_and_cli_state(tmp_path, capsys):
    rr.reserve("E3", 5.0, "a", balance=100.0)
    rr.reserve("E9", 5.0, "b", balance=100.0)
    assert rr.snapshot("E3", tmp_path / "snap.jsonl") == 1
    assert rr.main(["state", "--exp", "E3"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert len(st["reservations"]) == 1 and st["unlaunched_usd"] == 10.0
    assert rr.main(["admit", "--exp", "E3", "--usd", "100", "--balance", "100"]) == 3


def test_balance_sends_browser_user_agent():
    seen = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"myself": {"clientBalance": 224.18, "currentSpendPerHr": 0}}}

    class S:
        def post(self, url, headers, json, timeout):
            seen.update(url=url, headers=headers, query=json["query"])
            return R()

    assert rr.balance(S(), key="k") == 224.18
    assert seen["url"] == rr.GRAPHQL_URL and "clientBalance" in seen["query"]
    assert seen["headers"]["User-Agent"] == rp.USER_AGENT and "Mozilla" in rp.USER_AGENT


# --- emx.runpod: kill limit, per-slot deadlines, idempotent charging ---------------------------


def test_gpu_kill_limit_equals_spend_watchdog_kill_usd(e3):
    assert rp.gpu_kill_limit() == spend.watchdog_kill_usd("E3") == 35.0
    rp.configure("I9")  # E3's $10 API limit can't push its line below $35; I9's caps can
    spend.record("judge", "x", 1, 25.0, exp="I9")
    spend.reserve_api("I9", 5.0, "batch")
    assert rp.gpu_kill_limit() == spend.watchdog_kill_usd("I9") == 33.0  # 65 - 2 - 25 - 5
    assert rp.current_kill_limit() == 33.0
    assert rp.gpu_kill_limit(25.0) == 36.0  # the legacy figure alone ignores the reservation


def _created(pid, slot, t0, rate):
    rp.log_pod_event(
        {
            "event": "created",
            "pod_id": pid,
            "slot": slot,
            "gpu": "g",
            "rate_usd_h": rate,
            "utc": datetime.fromtimestamp(t0, UTC).isoformat(),
        }
    )


def test_per_slot_deadline_output(e3, monkeypatch, capsys):
    now = time.time()
    _created("pa", "_A", now, 2.0)
    _created("pb", "_B", now, 2.0)
    rp.set_env_var("RUNPOD_POD_ID_A", "pa")
    G = int(now + 8 * H)
    monkeypatch.setattr(rp, "killswitch_deadline", lambda pid, events=None, now=None: G)
    _, dl = launched("E3", 4.0, [("pa", 2.0, 4.0)], now)  # 2 h < the global 8 h
    assert rp.main(["--exp", "E3", "--check-slot", "_A", "--killswitch-deadline"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"EMX_KILLSWITCH_DEADLINE={G}",
        "EMX_KILLSWITCH_SLOTS=_A _B",
    ]
    assert (
        rp.main(["--exp", "E3", "--check-slot", "_A", "--killswitch-deadline", "--per-slot"]) == 0
    )
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == [f"EMX_KILLSWITCH_DEADLINE={G}", "EMX_KILLSWITCH_SLOTS=_A _B"]
    assert sorted(out[2:]) == [
        f"EMX_KILLSWITCH_DEADLINE_A={dl['pa']}",
        f"EMX_KILLSWITCH_DEADLINE_B={G}",
    ]  # no reservation: global
    rr.ack("pa", dl["pa"] - 600)  # shortened on the pod: never re-extended
    assert rp.per_slot_deadlines()["_A"] == dl["pa"] - 600


def _status_lines(capsys):
    return [x for x in capsys.readouterr().out.splitlines() if x.startswith("EMX_POD_CREATE=")]


class FakeClient:
    def __init__(
        self,
        create_results=(),
        pods=(),
        ip_after=None,
        clock=None,
        list_error=None,
        api_rate=None,
    ):
        self.api_rate = api_rate  # costPerHr that get_pod reports (None: absent)
        self.create_results = list(create_results)
        self.pods = list(pods)  # listed only once a create was sent (preflight sees none)
        self.list_error = list_error
        self.created: list[dict] = []
        self.terminated: list[str] = []
        self.terminate_error = None
        self.get_error = None
        self.dry_run = False
        self.ip_after, self.clock = ip_after, clock

    def create_pod(self, body):
        self.created.append(body)
        r = self.create_results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get_pod(self, pod_id):
        if self.get_error:
            raise self.get_error
        rate = {"costPerHr": self.api_rate} if self.api_rate else {}
        if self.ip_after is not None and self.clock["t"] < self.ip_after:
            return {"desiredStatus": "RUNNING", **rate}
        return {
            "publicIp": "1.2.3.4",
            "portMappings": {"22": 40022},
            "desiredStatus": "RUNNING",
            **rate,
        }

    def terminate_pod(self, pod_id):
        if self.terminate_error:
            raise self.terminate_error
        self.terminated.append(pod_id)

    def list_pods(self):
        if not self.created:
            return []
        if self.list_error:
            raise self.list_error
        return list(self.pods)


@pytest.fixture
def clock(monkeypatch):
    c = {"t": time.time()}
    monkeypatch.setattr(time, "time", lambda: c["t"])
    monkeypatch.setattr(time, "sleep", lambda s: c.__setitem__("t", c["t"] + s))
    return c


def _up(argv, fake, monkeypatch):
    up = load("pod_up")
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    return up, up.main(argv)


def test_pod_up_requires_a_reservation_for_e3(e3, monkeypatch):
    with pytest.raises(SystemExit) as e:
        _up(["--exp", "E3", "--slot", "_A"], FakeClient(), monkeypatch)
    assert e.value.code == 2


def test_pod_up_deadline_anchored_to_t0_despite_a_10_minute_ssh_delay(
    e3, monkeypatch, clock, capsys
):
    rid = rr.reserve("E3", 13.25, "pod1", balance=200.0)
    t_start = clock["t"]
    fake = FakeClient([{"id": "pa", "costPerHr": 2.69}], ip_after=t_start + 600, clock=clock)
    _, rc = _up(
        ["--exp", "E3", "--slot", "_A", "--reservation", rid, "--budget-usd", "12.2"],
        fake,
        monkeypatch,
    )
    assert rc == 0 and clock["t"] >= t_start + 600  # SSH came up 10 minutes later
    rows = rr.read_rows()
    c = next(r for r in rows if r["kind"] == "creating")
    lr = next(r for r in rows if r["kind"] == "launch")
    t0 = c["t0_epoch"]
    assert t0 == t_start and lr["t0_epoch"] == t0 and lr["creating_id"] == c["id"]
    want = int(t0 + 12.2 / 2.69 * H)
    assert lr["pods"][0]["requested_deadline_epoch"] == want  # not t0 + 600 + ...
    assert f"EMX_POD_DEADLINE={want}" in capsys.readouterr().out
    ev = rp.read_pod_events()[0]
    assert ev["event"] == "created"
    assert datetime.fromisoformat(ev["utc"]).timestamp() == pytest.approx(t0, abs=1e-5)
    assert rr.state(now=t0)["launched_liability_usd"] == 12.2  # unacked: full budget
    assert rp.env("RUNPOD_POD_ID_A") == "pa"


def test_pod_up_lost_create_response_adopts_the_named_pod(e3, monkeypatch, clock):
    rid = rr.reserve("E3", 13.0, "pod1", balance=200.0)
    fake = FakeClient(
        [rp.CreateUncertain("timeout")],
        pods=[{"id": "other", "name": "e3-b"}, {"id": "px", "name": "e3-a", "costPerHr": 2.6}],
    )
    _, rc = _up(
        ["--exp", "E3", "--slot", "_A", "--reservation", rid, "--budget-usd", "13"],
        fake,
        monkeypatch,
    )
    assert rc == 0 and fake.created and fake.terminated == []
    assert rr.get(rid)["launched"] and rr.get(rid)["pods"] == ["px"]
    assert rp.env("RUNPOD_POD_ID_A") == "px"


def test_pod_up_lost_create_with_no_pod_releases(e3, monkeypatch, clock):
    rid = rr.reserve("E3", 13.0, "pod1", balance=200.0)
    fake = FakeClient([rp.CreateUncertain("timeout")], pods=[{"id": "o", "name": "e3-b"}])
    _, rc = _up(
        ["--exp", "E3", "--slot", "_A", "--reservation", rid, "--budget-usd", "13"],
        fake,
        monkeypatch,
    )
    assert rc == 1 and rr.get(rid)["closed"] and not rr.get(rid)["launched"]


def test_pod_up_lost_create_with_failed_listing_keeps_the_reservation(
    e3, monkeypatch, clock, capsys
):
    rid = rr.reserve("E3", 13.0, "pod1", balance=200.0)
    fake = FakeClient([rp.CreateUncertain("timeout")], list_error=rp.RunPodError("503"))
    up, rc = _up(
        ["--exp", "E3", "--slot", "_A", "--reservation", rid, "--budget-usd", "13"],
        fake,
        monkeypatch,
    )
    assert rc == up.UNCERTAIN == 6 and not rr.get(rid)["closed"]
    assert rr.state()["unlaunched_usd"] == 13.0
    assert _status_lines(capsys) == [f"EMX_POD_CREATE=uncertain reservation={rid}"]


def test_pod_up_failed_creates_fall_back_then_release(e3, monkeypatch, clock):
    rid = rr.reserve("E3", 13.0, "pod1", balance=200.0)
    fake = FakeClient([rp.RunPodError("no stock")] * 4)
    _, rc = _up(
        ["--exp", "E3", "--slot", "_A", "--reservation", rid, "--budget-usd", "13"],
        fake,
        monkeypatch,
    )
    assert rc == 1 and len(fake.created) == 4 and rr.get(rid)["closed"]
    assert len([r for r in rr.read_rows() if r["kind"] == "creating"]) == 4


def _launch_pod(e3, pid="pd", budget=10.0, rate=2.0):
    t0 = time.time() - 1800
    rid = rr.reserve("E3", budget, "pod", balance=200.0)
    _created(pid, "_A", t0, rate)
    rp.set_env_var("RUNPOD_POD_ID_A", pid)
    rr.launch(rid, [(pid, rate, budget)], t0)
    return rid


def _gpu_rows(pid):
    return [e for e in spend.read_entries() if e["kind"] == "gpu" and f"pod={pid}" in e["note"]]


def test_pod_down_settles_and_releases(e3, monkeypatch):
    rid = _launch_pod(e3)
    down = load("pod_down")
    fake = FakeClient()
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    assert down.main(["--exp", "E3", "--slot", "_A", "--reservation", rid]) == 0
    assert fake.terminated == ["pd"] and len(_gpu_rows("pd")) == 1
    R = rr.get(rid)
    assert R["closed"] and R["settled_usd"] == pytest.approx(_gpu_rows("pd")[0]["usd"])
    assert rr.state()["launched_liability_usd"] == 0.0 and rp.env("RUNPOD_POD_ID_A") == ""


def test_pod_down_failed_termination_keeps_the_liability(e3, monkeypatch):
    rid = _launch_pod(e3)
    down = load("pod_down")
    fake = FakeClient()
    fake.terminate_error = rp.RunPodError("HTTP 500")
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    assert down.main(["--exp", "E3", "--slot", "_A", "--reservation", rid]) == 1
    assert not rr.get(rid)["closed"] and rr.state()["launched_liability_usd"] == 10.0
    assert _gpu_rows("pd") == [] and rp.env("RUNPOD_POD_ID_A") == "pd"


def test_crash_between_termination_and_charge_retry_records_once(e3, monkeypatch):
    rid = _launch_pod(e3)
    down = load("pod_down")
    fake = FakeClient()
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    real = rp.ledger_record_gpu
    monkeypatch.setattr(rp, "ledger_record_gpu", lambda s, n: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        down.main(["--exp", "E3", "--slot", "_A", "--reservation", rid])
    assert fake.terminated == ["pd"] and _gpu_rows("pd") == []  # terminated, not charged
    monkeypatch.setattr(rp, "ledger_record_gpu", real)
    fake.terminate_error = rp.RunPodError("HTTP 404: pod not found")  # it is gone
    fake.get_error = rp.RunPodError("HTTP 404")
    assert down.main(["--exp", "E3", "--slot", "_A", "--reservation", rid]) == 0
    assert len(_gpu_rows("pd")) == 1 and rr.get(rid)["closed"]
    rp.set_env_var("RUNPOD_POD_ID_A", "pd")  # a second retry changes nothing
    assert down.main(["--exp", "E3", "--slot", "_A", "--reservation", rid]) == 0
    assert len(_gpu_rows("pd")) == 1
    assert len([r for r in rr.read_rows() if r["kind"] == "settle"]) == 1


def test_crash_between_charge_and_settle_retry_settles_once(e3, monkeypatch):
    rid = _launch_pod(e3)
    down = load("pod_down")
    fake = FakeClient()
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    real = rr.settle
    monkeypatch.setattr(rr, "settle", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        down.main(["--exp", "E3", "--slot", "_A", "--reservation", rid])
    assert len(_gpu_rows("pd")) == 1 and not rr.get(rid)["closed"]
    assert rp.env("RUNPOD_POD_ID_A") == "pd"  # slot kept for the retry
    monkeypatch.setattr(rr, "settle", real)
    fake.terminate_error = fake.get_error = rp.RunPodError("HTTP 404")
    assert down.main(["--exp", "E3", "--slot", "_A", "--reservation", rid]) == 0
    assert len(_gpu_rows("pd")) == 1 and rr.get(rid)["closed"]
    real(rid, 99.0, pod_id="pd")  # idempotent: nothing appended
    assert len([r for r in rr.read_rows() if r["kind"] == "settle"]) == 1


def test_finish_session_charges_a_terminated_but_uncharged_session(e3):
    _created("q", "_A", time.time() - 600, 3.0)
    rp.log_pod_event({"event": "terminated", "pod_id": "q", "reason": "crash"})
    s = rp.finish_session("q", "retry")
    assert s and len(_gpu_rows("q")) == 1
    rp.finish_session("q", "again")
    assert len(_gpu_rows("q")) == 1
    # a legacy charge (no pod= in the note, pod id in the resource) also counts
    _created("old", "_B", time.time() - 600, 3.0)
    rp.log_pod_event({"event": "terminated", "pod_id": "old"})
    spend.record("gpu", "runpod:g:old", 0.1, 0.3, note="legacy", exp="E3")
    rp.finish_session("old", "retry")
    assert len([e for e in spend.read_entries() if e["resource"].endswith(":old")]) == 1


def test_pod_up_gpu_selects_one_type(e3, monkeypatch, clock):
    rid = rr.reserve("E3", 13.0, "pod1", balance=200.0)
    fake = FakeClient([{"id": "pg", "costPerHr": 2.59}])
    _, rc = _up(
        [
            "--exp",
            "E3",
            "--slot",
            "_A",
            "--reservation",
            rid,
            "--budget-usd",
            "13",
            "--gpu",
            "NVIDIA H100 NVL",
        ],
        fake,
        monkeypatch,
    )
    assert rc == 0 and [b["gpuTypeIds"][0] for b in fake.created] == ["NVIDIA H100 NVL"]
    up = load("pod_up")
    pcie = up.gpu_filter(rp.GPU_FALLBACK, "NVIDIA H100 PCIe")  # E3 contracts only
    assert [(o.cloud, o.rate_usd_h) for o in pcie] == [("COMMUNITY", 2.69), ("SECURE", 2.69)]
    with pytest.raises(SystemExit):
        _up(
            [
                "--exp",
                "E3",
                "--slot",
                "_B",
                "--reservation",
                rid,
                "--budget-usd",
                "1",
                "--gpu",
                "RTX 9090",
            ],
            FakeClient(),
            monkeypatch,
        )


def test_reaper_bills_a_dead_launchers_pod_from_its_creating_t0(e3):
    rid = rr.reserve("E3", 13.0, "pod1", balance=200.0)
    t0 = time.time() - 1200
    rr.creating(rid, slot="_A", name="e3-a", budget_usd=13.0, rate_usd_h=2.6, t0_epoch=t0)
    (o,) = rr.unarmed_overdue()  # the launcher died after sending the create
    assert o["reservation_id"] == rid and o["name"] == "e3-a" and o["pod_id"] is None
    fake = FakeClient([], pods=[{"id": "ghost", "name": "e3-a", "costPerHr": 2.6}])
    fake.created.append({})  # make the fake list it
    assert rp.reap_untracked(fake, "recover", grace_s=0) == ["ghost"]
    s = next(x for x in rp.pod_sessions(rp.read_pod_events()) if x["pod_id"] == "ghost")
    assert s["hours"] == pytest.approx(1200 / H, abs=1e-3) and len(_gpu_rows("ghost")) == 1


# --- review A r1 finding 7: a rate discovered after launch reprices the reservation ------------


def test_pod_up_higher_polled_rate_shortens_the_deadline_before_arming(
    e3, monkeypatch, clock, capsys
):
    rid = rr.reserve("E3", 13.25, "pod1", balance=200.0)
    t_start = clock["t"]
    # the create response carries no rate (launch uses the $2.69 estimate); polling shows $3.49
    fake = FakeClient([{"id": "pa"}], ip_after=t_start + 60, clock=clock, api_rate=3.49)
    _, rc = _up(
        ["--exp", "E3", "--slot", "_A", "--reservation", rid, "--budget-usd", "13.25"],
        fake,
        monkeypatch,
    )
    assert rc == 0
    old = int(t_start + 13.25 / 2.69 * H)
    new = int(t_start + 13.25 / 3.49 * H)
    p = rr.pod("pa")
    assert p["rate_usd_h"] == 3.49 and p["requested"] == p["allowed"] == new
    assert rr.pod_deadline("pa") == new and rr.rearm_target("pa", t_start + 99 * H) == new
    out = capsys.readouterr().out
    assert f"EMX_POD_DEADLINE={new}" in out and f"EMX_POD_DEADLINE={old}" not in out
    assert f"EMX_POD_CREATE=ok reservation={rid} pod=pa" in out
    (row,) = [r for r in rr.read_rows() if r["kind"] == "reprice"]
    assert row["previous_rate_usd_h"] == 2.69 and row["deadline_epoch"] == new
    # the old (longer) deadline can no longer be acknowledged: the $17.19 overrun is closed
    with pytest.raises(rr.ReservationError):
        rr.ack("pa", old)
    rr.ack("pa", new)
    assert rr.state(now=t_start)["launched_liability_usd"] == pytest.approx(13.25, abs=0.01)
    # a lower (or equal) rate never lengthens it
    assert rr.reprice("pa", 2.0) is None and rr.reprice("pa", 3.49) is None
    assert rr.pod_deadline("pa") == new
    # the session (the GPU charge) uses the API rate too
    s = next(x for x in rp.pod_sessions(rp.read_pod_events()) if x["pod_id"] == "pa")
    assert s["rate"] == 3.49


def test_reprice_after_ack_counts_the_armed_deadline_until_rearmed():
    t0 = time.time()
    _, dl = launched("E3", 10.0, [("pz", 2.0, 10.0)], t0)
    rr.ack("pz", dl["pz"])  # armed at 5 h
    row = rr.reprice("pz", 4.0)
    assert row["deadline_epoch"] == int(t0 + 2.5 * H)
    # the killswitch still holds 5 h until re-armed: liability = 4 $/h x 5 h, not x 2.5 h
    assert rr.state(now=t0)["launched_liability_usd"] == pytest.approx(20.0, abs=0.01)
    assert rr.rearm_target("pz", t0 + 99 * H) == int(t0 + 2.5 * H)
    rr.ack("pz", rr.rearm_target("pz", t0 + 99 * H))
    assert rr.state(now=t0)["launched_liability_usd"] == pytest.approx(10.0, abs=0.01)
    with pytest.raises(rr.ReservationError):
        rr.reprice("nope", 9.0)


# --- review A r1 finding 1: an uncertain create is reported distinctly -------------------------


def test_pod_up_error_after_a_create_request_reports_uncertain(e3, monkeypatch, clock, capsys):
    rid = rr.reserve("E3", 13.0, "pod1", balance=200.0)
    fake = FakeClient([{"id": "pa", "costPerHr": 2.69}])
    monkeypatch.setattr(rp, "set_env_var", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    up, rc = _up(
        ["--exp", "E3", "--slot", "_A", "--reservation", rid, "--budget-usd", "13"],
        fake,
        monkeypatch,
    )
    assert rc == up.UNCERTAIN and not rr.get(rid)["closed"]
    assert _status_lines(capsys) == [f"EMX_POD_CREATE=uncertain reservation={rid}"]


def test_pod_up_status_lines_for_refusal_and_release(e3, monkeypatch, clock, capsys):
    rid = rr.reserve("E3", 13.0, "pod1", balance=200.0)
    rp.set_env_var("RUNPOD_POD_ID_A", "busy")  # refused before any create request
    with pytest.raises(SystemExit) as e:
        _up(
            ["--exp", "E3", "--slot", "_A", "--reservation", rid, "--budget-usd", "13"],
            FakeClient(),
            monkeypatch,
        )
    assert e.value.code != 6 and not rr.get(rid)["closed"]
    assert _status_lines(capsys) == [f"EMX_POD_CREATE=none reservation={rid}"]
    rp.set_env_var("RUNPOD_POD_ID_A", "")
    fake = FakeClient([rp.RunPodError("no stock")] * 4)
    _, rc = _up(
        ["--exp", "E3", "--slot", "_A", "--reservation", rid, "--budget-usd", "13"],
        fake,
        monkeypatch,
    )
    assert rc == 1 and rr.get(rid)["closed"]
    assert _status_lines(capsys) == [f"EMX_POD_CREATE=closed reservation={rid}"]


@pytest.mark.parametrize("creates", ["ok", "fail"])
def test_pod_up_without_reservation_prints_no_status_line(e3, monkeypatch, clock, capsys, creates):
    """A non-reserved launch of an existing experiment (E1: no --reservation) keeps its old
    output: no `EMX_POD_CREATE=` line, whether the create succeeds or every GPU type fails
    (E3 code review A r2, minor 4)."""
    (e3 / "watchdog_E1.heartbeat").write_text(f"{time.time()} 600 {os.getpid()}\n")
    results = (
        [{"id": "pe1", "costPerHr": 2.69}] if creates == "ok" else [rp.RunPodError("no stock")] * 8
    )
    fake = FakeClient(results)
    try:
        _, rc = _up(["--exp", "E1", "--slot", "_A"], fake, monkeypatch)
    except SystemExit as e:
        rc = e.code
    out = capsys.readouterr().out
    assert fake.created, "the launch must reach the create request"
    assert (rc == 0) is (creates == "ok"), out
    assert "EMX_POD_CREATE=" not in out
    assert rr.read_rows() == []  # nothing reserved


# --- review A r1 finding 10: concurrent settlement charges a pod once ---------------------------

RACE = r"""
import sys, time
from pathlib import Path
from emx import runpod as rp, runpod_reservations as rr, spend
real = spend._parse
def slow(text, path):
    out = real(text, path)
    time.sleep(0.4)  # widen any check-then-append window
    return out
spend._parse = slow
rp.configure("E3")
rid, start = sys.argv[1], float(sys.argv[2])
while time.time() < start:
    time.sleep(0.002)
s = rp.finish_session("pd", "race")
rr.settle(rid, s["usd"], pod_id="pd", note="race")
print("done", s["usd"])
"""


def test_two_processes_settling_the_same_pod_record_one_charge(e3, tmp_path):
    rid = _launch_pod(e3)
    env = {
        **os.environ,
        "EMX_POD_LOG_DIR": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
    }
    start = str(time.time() + 1.5)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", RACE, rid, start],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    assert len(_gpu_rows("pd")) == 1
    assert len([e for e in rp.read_pod_events() if e["event"] == "charged"]) == 1
    assert len([r for r in rr.read_rows() if r["kind"] == "settle"]) == 1
    usd = {o.split()[-1] for o, _ in outs}
    assert len(usd) == 1  # both computed the same session (first termination wins)
    assert rr.get(rid)["settled_usd"] == pytest.approx(_gpu_rows("pd")[0]["usd"])


def test_record_once_appends_once_and_honours_legacy_matches():
    e, new = spend.record_once("k1", "gpu", "r", 1, 2.0, exp="E3")
    assert new and e["charge_key"] == "k1"
    assert spend.record_once("k1", "gpu", "r", 1, 9.0, exp="E3") == (e, False)
    spend.record("gpu", "runpod:g:legacy", 1, 1.0, exp="E3")
    _, new = spend.record_once(
        "k2", "gpu", "r", 1, 1.0, exp="E3", also_matches=lambda x: x["resource"].endswith("legacy")
    )
    assert not new and spend.spent("gpu", exp="E3") == 3.0


# --- review A r1 finding 4: settle every unsettled pod from durable history --------------------


def _down(argv, fake, monkeypatch):
    down = load("pod_down")
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    return down.main(argv)


def test_unsettled_pods_from_durable_history(e3):
    t0 = time.time() - 600
    a, _ = launched("E3", 10.0, [("pa", 2.0, 10.0)], t0)
    _created("pa", "_A", t0, 2.0)
    b, _ = launched("E3", 8.0, [("pb", 2.5, 8.0)], t0)
    _created("pb", "_B", t0, 2.5)
    rp.log_pod_event({"event": "terminated", "pod_id": "pb"})
    c, _ = launched("E3", 5.0, [("pc", 2.0, 5.0)], t0)
    rr.settle(c, 1.0, pod_id="pc")
    launched("E9", 5.0, [("e9", 2.0, 5.0)], t0)  # another experiment's pod
    # a dead launcher's pod the reaper adopted: a `created` event naming the reservation
    d = rr.reserve("E3", 6.0, "pod4", balance=1000.0)
    rr.creating(d, slot="_C", name="e3-c", budget_usd=6.0, rate_usd_h=3.0, t0_epoch=t0)
    rp.log_pod_event(
        {
            "event": "created",
            "pod_id": "ghost",
            "slot": "",
            "rate_usd_h": 3.0,
            "reservation_id": d,
            "utc": datetime.fromtimestamp(t0 + 5, UTC).isoformat(),
        }
    )
    (e3 / ".env").write_text("RUNPOD_API_KEY=test-key\n")  # no slot keys at all
    got = {o["pod_id"]: o for o in rr.unsettled_pods("E3")}
    assert set(got) == {"pa", "pb", "ghost"}
    assert got["pa"] == {
        "pod_id": "pa",
        "reservation_id": a,
        "slot": "_A",
        "terminated": False,
        "t0": t0,
        "budget_usd": 10.0,
        "rate": 2.0,
        "deadline_epoch": int(t0 + 5 * H),
        "source": "launch",
    }
    assert got["pb"]["terminated"] and got["pb"]["reservation_id"] == b
    assert got["ghost"]["source"] == "event_log" and got["ghost"]["slot"] == "_C"
    assert got["ghost"]["t0"] == t0 and got["ghost"]["budget_usd"] == 6.0
    assert [o["pod_id"] for o in rr.unsettled_pods("E9")] == ["e9"]


def test_pod_down_by_pod_id_after_the_watchdog_cleared_the_slot(e3, monkeypatch):
    rid = _launch_pod(e3)
    # watchdog --all-exp: terminated and charged, slot keys cleared, reservation never settled
    rp.finish_session("pd", "watchdog --all-exp")
    rp.set_env_var("RUNPOD_POD_ID_A", "")
    (o,) = rr.unsettled_pods("E3")
    assert o["pod_id"] == "pd" and o["terminated"] and o["reservation_id"] == rid
    fake = FakeClient()
    fake.terminate_error = fake.get_error = rp.RunPodError("HTTP 404")
    argv = ["--exp", "E3", "--pod-id", "pd", "--reservation", rid, "--slot", "_A"]
    assert _down(argv, fake, monkeypatch) == 0
    assert fake.terminated == [] and len(_gpu_rows("pd")) == 1  # DELETE 404 tolerated; 1 charge
    assert rr.get(rid)["closed"] and rr.unsettled_pods("E3") == []
    assert rr.state()["launched_liability_usd"] == 0.0
    assert _down(argv, fake, monkeypatch) == 0  # idempotent
    assert len(_gpu_rows("pd")) == 1
    assert len([r for r in rr.read_rows() if r["kind"] == "settle"]) == 1


def test_pod_down_by_pod_id_terminates_a_running_pod_without_env_keys(e3, monkeypatch):
    rid = _launch_pod(e3)
    rp.set_env_var("RUNPOD_POD_ID_A", "")
    fake = FakeClient()
    assert _down(["--exp", "E3", "--pod-id", "pd", "--reservation", rid], fake, monkeypatch) == 0
    assert fake.terminated == ["pd"] and len(_gpu_rows("pd")) == 1 and rr.get(rid)["closed"]


def test_pod_down_by_pod_id_crash_between_terminated_event_and_charge(e3, monkeypatch):
    rid = _launch_pod(e3)
    rp.log_pod_event({"event": "terminated", "pod_id": "pd", "reason": "crash"})
    fake = FakeClient()
    assert _down(["--exp", "E3", "--pod-id", "pd", "--reservation", rid], fake, monkeypatch) == 0
    assert fake.terminated == ["pd"] and len(_gpu_rows("pd")) == 1 and rr.get(rid)["closed"]
    assert rp.env("RUNPOD_POD_ID_A") == ""  # the keys still named it: cleared


def test_pod_down_by_pod_id_failed_delete_keeps_the_liability(e3, monkeypatch):
    rid = _launch_pod(e3)
    fake = FakeClient()
    fake.terminate_error = rp.RunPodError("HTTP 500")  # GET says RUNNING: not gone
    assert _down(["--exp", "E3", "--pod-id", "pd", "--reservation", rid], fake, monkeypatch) == 1
    assert not rr.get(rid)["closed"] and _gpu_rows("pd") == []
    assert rr.state()["launched_liability_usd"] == 10.0


def test_pod_down_by_pod_id_refusals(e3, monkeypatch):
    rid = _launch_pod(e3)
    other = rr.reserve("E3", 5.0, "other", balance=200.0)
    fake = FakeClient()
    for argv in (
        ["--pod-id", "pd", "--reservation", other],  # not the reservation that launched it
        ["--pod-id", "nope", "--reservation", rid],  # unknown pod
        ["--pod-id", "pd", "--reservation", rid, "--slot", "_B"],  # another slot
        ["--pod-id", "pd", "--reservation", "missing"],  # unknown reservation
    ):
        assert _down(["--exp", "E3", *argv], fake, monkeypatch) == 1, argv
    assert fake.terminated == [] and not rr.get(rid)["closed"]
    with pytest.raises(SystemExit) as e:
        _down(["--exp", "E3", "--pod-id", "pd"], fake, monkeypatch)
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        _down(["--exp", "E3"], fake, monkeypatch)
    assert e.value.code == 2


def test_unsettled_cli(e3, capsys):
    t0 = time.time()
    launched("E3", 4.0, [("pq", 2.0, 4.0)], t0)
    assert rr.main(["unsettled", "--exp", "E3"]) == 0
    assert [o["pod_id"] for o in json.loads(capsys.readouterr().out)] == ["pq"]
