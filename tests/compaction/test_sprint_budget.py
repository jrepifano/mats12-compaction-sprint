"""Tests for src/compaction/budget.py: concurrent reservations can't overshoot a cap, the top-up
gate, transfers, crash recovery. Ledger and caps live in tmp_path."""

import json
import subprocess
import sys
import textwrap
import time
from datetime import timedelta

import pytest

from compaction import budget as B
from emx import spend

CAPS_TOML = """
[sprint]
checkout = "{checkout}"
core_cap_usd = {core}
full_cap_usd = {full}
topup_recorded = {topup}
transfer_approval_over_usd = 50.0

[stages.SPRINT-S1]
cap_usd = {s1}

[stages.SPRINT-S2]
cap_usd = 50.0

[stages.SPRINT-S4]
cap_usd = 250.0
requires_topup = true

[pools.reserve]
usd = 147.0

[pools.contingency]
usd = 350.0
requires_topup = true
"""


def write_caps(path, core=632.0, full=1652.0, topup="false", s1=60.0, checkout=None):
    checkout = checkout or path.parent  # the fixture's ledger is <tmp>/logs/costs.jsonl
    path.write_text(CAPS_TOML.format(core=core, full=full, topup=topup, s1=s1, checkout=checkout))
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    ledger = tmp_path / "logs" / "costs.jsonl"
    caps = write_caps(tmp_path / "caps.toml")
    monkeypatch.setenv(spend.LEDGER_ENV, str(ledger))
    monkeypatch.setenv(B.CAPS_ENV, str(caps))
    return ledger, caps


def test_real_caps_file_matches_the_plan():
    c = B.load_caps(B.DEFAULT_CAPS)
    assert not c.topup_recorded and c.sprint_cap_usd == 632.0 and c.full_cap_usd == 1652.0
    core = [s for s, v in c.stages.items() if not v.requires_topup]
    assert sum(c.stages[s].cap_usd for s in core) + c.pools["reserve"].usd == c.core_cap_usd
    extra = sum(v.cap_usd for v in c.stages.values() if v.requires_topup)
    assert c.core_cap_usd + extra + c.pools["contingency"].usd == c.full_cap_usd
    assert {s for s, v in c.stages.items() if v.requires_topup} == {"SPRINT-S4", "SPRINT-S5"}
    assert c.checkout.is_absolute()


def test_refuses_outside_the_designated_checkout(env, tmp_path, monkeypatch):
    ledger, caps = env
    rid = B.reserve("SPRINT-S1", 5.0)
    # A second checkout: its own ledger, as AGENTS.md requires, so its own lock and state.
    other = tmp_path / "other-worktree" / "logs" / "costs.jsonl"
    monkeypatch.setenv(spend.LEDGER_ENV, str(other))
    for call in (
        lambda: B.reserve("SPRINT-S1", 1.0),
        lambda: B.settle(rid, 1.0),
        lambda: B.cancel(rid, "x"),
        lambda: B.expire(0, force=True),
        lambda: B.transfer("reserve", "SPRINT-S1", 1.0, "x"),
        lambda: B.status(),
    ):
        with pytest.raises(B.WrongCheckout, match="designated checkout"):
            call()
    assert not other.parent.exists()  # nothing written in the other checkout, not even a lock
    # Back in the designated checkout, nothing changed.
    monkeypatch.setenv(spend.LEDGER_ENV, str(ledger))
    assert B.status()["stages"]["SPRINT-S1"]["open"] == 5.0
    # An explicit ledger argument is checked the same way.
    with pytest.raises(B.WrongCheckout):
        B.reserve("SPRINT-S1", 1.0, ledger=other)
    write_caps(caps, checkout="relative/path")
    with pytest.raises(ValueError, match="absolute"):
        B.reserve("SPRINT-S1", 1.0)


def test_bounded_cost():
    prices = {
        "m": {"input": 0.30, "output": 1.20},
        "h": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "request": 0.001},
    }
    assert B.bounded_cost("m", 100_000, 10_000, prices) == pytest.approx(0.042)
    assert B.bounded_cost("h", 1_000_000, 0, prices) == pytest.approx(1.251)
    assert B.bounded_cost("m", 1, 0, prices) == pytest.approx(1e-6)  # rounds up
    # Sub-millionth prices and fees are kept exact until the final round-up (review r1 #5).
    tiny = {"t": {"input": 0, "output": 0, "request": 0.0000004}}
    assert B.bounded_cost("t", 0, 0, tiny) == pytest.approx(1e-6)
    frac = {"f": {"input": 0.1234567, "output": 0}}  # 3 tokens -> $3.703701e-7
    assert B.bounded_cost("f", 3, 0, frac) == pytest.approx(1e-6)
    assert B.bounded_cost("f", 10_000_000, 0, frac) == pytest.approx(1.234567)
    assert B.bounded_cost("m", 3_333_333, 0, prices) == pytest.approx(1.0)  # 0.9999999 -> 1.0
    with pytest.raises(KeyError):
        B.bounded_cost("nope", 1, 1, prices)
    with pytest.raises(ValueError):
        B.bounded_cost("m", -1, 1, prices)


def test_reserve_settle_writes_ledger_and_releases(env):
    rid = B.reserve("SPRINT-S1", 10.0, note="batch 1")
    st = B.status()
    assert st["stages"]["SPRINT-S1"]["open"] == 10.0
    assert st["stages"]["SPRINT-S1"]["headroom"] == 50.0
    e = B.settle(rid, 3.25, resource="deepseek:v4.1-flash", units=12, note="judge batch")
    assert e["exp"] == "SPRINT-S1" and e["usd"] == 3.25 and e["kind"] == "judge"
    assert f"[sprint_res={rid}]" in e["note"]
    assert spend.spent(exp="SPRINT-S1") == 3.25
    st = B.status()
    assert st["stages"]["SPRINT-S1"] == {
        "cap": 60.0,
        "spent": 3.25,
        "open": 0.0,
        "headroom": 56.75,
        "requires_topup": False,
    }
    with pytest.raises(B.ReservationError, match="already closed \\(settle"):
        B.settle(rid, 1.0)
    (ev,) = [e for e in B.read_events() if e["op"] == "settle"]
    assert ev["reservation_id"] == rid and ev["ledger_id"] == e["id"]


def test_stage_cap_counts_spent_plus_open(env):
    B.reserve("SPRINT-S1", 30.0)
    spend.record("judge", "x", 1, 20.0, exp="SPRINT-S1")  # direct spend also counts
    with pytest.raises(B.BudgetRefused, match="stage cap") as ei:
        B.reserve("SPRINT-S1", 10.01)
    assert ei.value.breakdown["spent"] == 20.0 and ei.value.breakdown["open"] == 30.0
    B.reserve("SPRINT-S1", 10.0)  # exactly at the cap is allowed
    with pytest.raises(ValueError, match="unknown sprint stage"):
        B.reserve("SPRINT-S9", 1.0)
    # Historical "SPRINT" rows are not a stage and don't count.
    spend.record("judge", "bench", 1, 5.0, exp="SPRINT")
    B.reserve("SPRINT-S2", 50.0)


def test_sprint_cap_is_checked_across_stages(env, tmp_path):
    write_caps(env[1], core=70.0, s1=60.0)
    B.reserve("SPRINT-S1", 40.0)
    B.reserve("SPRINT-S2", 30.0)
    with pytest.raises(B.BudgetRefused, match="sprint cap"):
        B.reserve("SPRINT-S2", 0.5)


def test_topup_gate(env):
    _, caps = env
    with pytest.raises(B.TopUpRequired):
        B.reserve("SPRINT-S4", 1.0)
    with pytest.raises(B.TopUpRequired):
        B.transfer("contingency", "SPRINT-S1", 5.0, "need more")
    assert B.status()["sprint"]["cap"] == 632.0
    assert B.status()["stages"]["SPRINT-S4"]["headroom"] == 0.0
    write_caps(caps, topup="true")  # Jacob records the top-up: takes effect at once
    B.reserve("SPRINT-S4", 1.0)
    assert B.status()["sprint"]["cap"] == 1652.0
    B.transfer("contingency", "SPRINT-S4", 5.0, "S4 summariser calls ran long")


def test_transfers(env):
    ledger, _ = env
    with pytest.raises(ValueError, match="reason"):
        B.transfer("reserve", "SPRINT-S2", 10.0, "  ")
    B.reserve("SPRINT-S2", 50.0)
    with pytest.raises(B.BudgetRefused):
        B.reserve("SPRINT-S2", 10.0)
    ev = B.transfer("reserve", "SPRINT-S2", 10.0, "S2 confirmation run")
    assert ev["op"] == "transfer" and ev["reason"] == "S2 confirmation run"
    B.reserve("SPRINT-S2", 10.0)
    assert B.status()["pools"]["reserve"]["left"] == 137.0
    # Over $50 in total needs Jacob's yes.
    B.transfer("reserve", "SPRINT-S1", 40.0, "S1 judge re-run")
    with pytest.raises(B.BudgetRefused, match="Jacob"):
        B.transfer("reserve", "SPRINT-S1", 0.01, "one cent more")
    B.transfer("reserve", "SPRINT-S1", 90.0, "S1 coverage", approved_by="jacob")
    with pytest.raises(B.BudgetRefused, match="left"):
        B.transfer("reserve", "SPRINT-S1", 8.0, "pool is down to $7", approved_by="jacob")
    assert B.status()["stages"]["SPRINT-S1"]["cap"] == 190.0
    events = [json.loads(x) for x in (ledger.parent / B.EVENTS_NAME).read_text().splitlines()]
    assert [e["usd"] for e in events if e["op"] == "transfer"] == [10.0, 40.0, 90.0]
    assert events[-1]["approved_by"] == "jacob"


def test_overrun_is_recorded_in_full_and_logged(env):
    rid = B.reserve("SPRINT-S1", 1.0)
    B.settle(rid, 1.5)
    assert spend.spent(exp="SPRINT-S1") == 1.5
    assert [e["op"] for e in B.read_events()] == ["charge_intent", "settle", "overrun"]


def test_cancel(env):
    rid = B.reserve("SPRINT-S1", 60.0)
    B.cancel(rid, "request never sent: tokenizer error")
    assert B.status()["stages"]["SPRINT-S1"]["open"] == 0.0
    assert spend.spent(exp="SPRINT-S1") == 0.0
    with pytest.raises(B.ReservationError):
        B.cancel(rid, "again")


CRASH = textwrap.dedent("""
    import os, sys
    from compaction import budget as B
    B.reserve("SPRINT-S1", float(sys.argv[1]), note=sys.argv[2])
    os._exit(1)
""")


def _age(ledger, note, secs=7200, **fields):
    state_path = ledger.parent / B.STATE_NAME
    state = json.loads(state_path.read_text())
    (rid,) = [k for k, r in state["reservations"].items() if r["note"] == note]
    state["reservations"][rid]["ts"] -= secs
    state["reservations"][rid].update(fields)
    state_path.write_text(json.dumps(state))
    return rid


def test_reservations_survive_a_crash_and_expire_only_when_the_owner_is_dead(env):
    ledger, _ = env
    # Two processes reserve and die without settling.
    for amount, note in ((25.0, "dies"), (2.0, "dies too")):
        subprocess.run([sys.executable, "-c", CRASH, str(amount), note], check=False)
    st = B.status()
    assert st["stages"]["SPRINT-S1"]["open"] == 27.0  # still counted against the cap
    with pytest.raises(B.BudgetRefused):
        B.reserve("SPRINT-S1", 40.0)
    stale = _age(ledger, "dies")
    assert B.expire(timedelta(hours=3)) == []  # not old enough
    # Only the aged one is older than the cutoff; its owner is dead, so it is charged in full.
    assert B.expire(timedelta(hours=1), reason="pid gone") == [stale]
    assert spend.spent(exp="SPRINT-S1") == 25.0
    assert B.status()["stages"]["SPRINT-S1"]["open"] == 2.0
    (ev,) = [e for e in B.read_events() if e["op"] == "expire"]
    assert ev["charged_usd"] == 25.0 and ev["reason"] == "pid gone" and ev["owner"] == "dead"
    with pytest.raises(B.ReservationError, match="already closed \\(expire"):
        B.settle(stale, 3.0)
    other = _age(ledger, "dies too")
    assert B.expire(timedelta(hours=1), charge_full=False) == [other]
    assert spend.spent(exp="SPRINT-S1") == 25.0


def test_expiry_never_takes_a_live_call(env):
    ledger, _ = env
    live = B.reserve("SPRINT-S1", 3.0, note="slow call")  # this process: alive
    _age(ledger, "slow call", secs=86400)
    assert B.expire(timedelta(minutes=1)) == []
    assert B.expire(0, force=True) == []  # force doesn't override a live owner on this host
    e = B.settle(live, 1.25)  # the slow call completes and settles at its actual cost
    assert e["usd"] == 1.25 and spend.spent(exp="SPRINT-S1") == 1.25


def test_expiry_of_another_hosts_reservation_needs_force(env):
    ledger, _ = env
    rid = B.reserve("SPRINT-S1", 4.0, note="remote")
    _age(ledger, "remote", host="some-other-host", pid=1)
    assert B.expire(timedelta(minutes=1)) == []
    assert B.expire(timedelta(minutes=1), force=True, reason="checked: gone") == [rid]
    (ev,) = [e for e in B.read_events() if e["op"] == "expire"]
    assert ev["owner"] == "unchecked (forced)" and spend.spent(exp="SPRINT-S1") == 4.0


def test_crash_after_the_ledger_write_before_the_closing_event(env, monkeypatch):
    rid = B.reserve("SPRINT-S1", 10.0)
    real_log = B._log

    def log_then_die(path, event):
        if event["op"] == "settle":
            raise KeyboardInterrupt("killed after the ledger write")
        return real_log(path, event)

    with monkeypatch.context() as m:  # not monkeypatch.undo(): that would drop the env too
        m.setattr(B, "_log", log_then_die)
        with pytest.raises(KeyboardInterrupt):
            B.settle(rid, 4.0)
    st = B.status()  # reconciles: intent + matching ledger row, so the reservation closes
    assert st["stages"]["SPRINT-S1"]["open"] == 0.0
    assert st["stages"]["SPRINT-S1"]["spent"] == 4.0
    assert [e["op"] for e in B.read_events()] == ["charge_intent", "reconcile"]
    with pytest.raises(B.ReservationError, match="already closed \\(reconcile"):
        B.settle(rid, 4.0)
    assert spend.spent(exp="SPRINT-S1") == 4.0


def test_crash_after_the_closing_event_before_the_state_write(env, monkeypatch):
    rid = B.reserve("SPRINT-S1", 10.0)

    def boom(*a, **k):
        raise KeyboardInterrupt("killed before the state write")

    with monkeypatch.context() as m:
        m.setattr(B, "_save_state", boom)
        with pytest.raises(KeyboardInterrupt):
            B.settle(rid, 4.0)
    st = B.status()  # the event log says settled, so the reservation closes
    assert st["stages"]["SPRINT-S1"] == pytest.approx(
        {"cap": 60.0, "spent": 4.0, "open": 0.0, "headroom": 56.0, "requires_topup": False}
    )
    assert [e["op"] for e in B.read_events()] == ["charge_intent", "settle"]


def test_notes_cannot_close_another_reservation(env):
    """Review r2 finding 1: a $1 S2 charge whose note names a $60 S1 reservation must not close
    it."""
    b = B.reserve("SPRINT-S1", 60.0)
    a = B.reserve("SPRINT-S2", 1.0)
    with pytest.raises(ValueError, match="sprint_res="):
        B.settle(a, 1.0, note=f"see [sprint_res={b}]")
    with pytest.raises(ValueError, match="sprint_res="):
        B.reserve("SPRINT-S2", 1.0, note=f"[sprint_res={b}]")
    B.settle(a, 1.0, note="plain")
    # Rows written around this module, even with a forged tag at the end of the note, in the
    # wrong stage or in the right stage, close nothing: there is no logged intent for them.
    spend.record("judge", "x", 1, 1.0, note=f"forged [sprint_res={b}]", exp="SPRINT-S2")
    spend.record("judge", "x", 1, 60.0, note=f"forged [sprint_res={b}]", exp="SPRINT-S1")
    st = B.status()
    assert st["stages"]["SPRINT-S1"]["open"] == 60.0
    assert [r["id"] for r in st["open_reservations"]] == [b]
    B.cancel(b, "test cleanup")
    with pytest.raises(B.ReservationError, match="is SPRINT-S2, not SPRINT-S1"):
        B.settle(B.reserve("SPRINT-S2", 1.0), 1.0, stage="SPRINT-S1")


def test_stage_to_stage_transfer(env):
    """Review r2 finding 2: the plan's low-loss-yield branch moves unused Q2 money to S1."""
    B.reserve("SPRINT-S2", 30.0)
    spend.record("judge", "x", 1, 15.0, exp="SPRINT-S2")
    with pytest.raises(B.BudgetRefused, match="unspent and unreserved"):
        B.transfer("SPRINT-S2", "SPRINT-S1", 5.01, "low loss yield")  # 50 - 15 - 30 = 5 free
    ev = B.transfer("SPRINT-S2", "SPRINT-S1", 5.0, "low loss yield: Q2 unused to S1")
    assert ev["source_kind"] == "stage" and ev["source"] == "SPRINT-S2"
    st = B.status()["stages"]
    assert st["SPRINT-S2"]["cap"] == 45.0 and st["SPRINT-S2"]["headroom"] == 0.0
    assert st["SPRINT-S1"]["cap"] == 65.0
    with pytest.raises(B.BudgetRefused):
        B.reserve("SPRINT-S2", 0.01)
    B.reserve("SPRINT-S1", 65.0)
    # Stage moves don't count toward the $50 pool-approval threshold.
    B.transfer("reserve", "SPRINT-S2", 50.0, "pool transfer at the threshold")
    with pytest.raises(ValueError, match="different"):
        B.transfer("SPRINT-S1", "SPRINT-S1", 1.0, "x")
    with pytest.raises(B.TopUpRequired):
        B.transfer("SPRINT-S4", "SPRINT-S1", 1.0, "x")
    with pytest.raises(ValueError, match="unknown transfer source"):
        B.transfer("nope", "SPRINT-S1", 1.0, "x")


WORKER = textwrap.dedent("""
    import json, os, sys, time
    from compaction import budget as B
    start, n, amount, stage, out = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), \
        sys.argv[4], sys.argv[5]
    deadline = time.time() + 60  # never wait forever for the start marker
    while not os.path.exists(start):
        if time.time() > deadline:
            sys.exit(3)
        time.sleep(0.001)
    got = []
    for _ in range(n):
        try:
            rid = B.reserve(stage, amount)
            got.append(rid)
            if len(got) % 2:
                B.settle(rid, amount)  # half settle at full cost, half stay open
        except B.BudgetRefused:
            pass
    with open(out, "w") as f:
        json.dump(got, f)
""")


@pytest.mark.parametrize("stage,core,s1", [("SPRINT-S1", 632.0, 60.0), ("SPRINT-S1", 45.0, 60.0)])
def test_concurrent_reservations_cannot_overshoot(env, tmp_path, stage, core, s1):
    _, caps = env
    write_caps(caps, core=core, s1=s1)
    cap = min(core, s1)
    start = tmp_path / "go"
    procs, outs = [], []
    try:
        for i in range(8):
            out = tmp_path / f"out{i}.json"
            outs.append(out)
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-c", WORKER, str(start), "6", "2.5", stage, str(out)]
                )
            )
        time.sleep(0.5)
        start.touch()
        rcs = [p.wait(timeout=120) for p in procs]
    finally:
        # Review r2 finding 3: on any failure, including partial startup, stop and reap every
        # worker that was launched.
        start.touch()
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait()
    assert rcs == [0] * 8
    got = [rid for o in outs for rid in json.loads(o.read_text())]
    assert len(got) == int(cap // 2.5)  # exactly fills the cap, never more
    st = B.status()["stages"][stage]
    assert st["spent"] + st["open"] == pytest.approx(len(got) * 2.5)
    assert st["spent"] + st["open"] <= cap + 1e-9
    spend.read_entries()  # ledger still parses: no interleaved lines
