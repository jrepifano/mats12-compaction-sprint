"""E9 budget and admission (unit F1a): projection, cuts, safe stop, per-pod budgets, backstop, and
the admission protocol's races (implementation plan "E9 admission protocol"; prereg §7.3, §4.11,
§7.6, §8.6). Offline: tmp ledger, tmp reservation file, tmp lock, injected balance and clock."""

from __future__ import annotations

import multiprocessing as mp
import queue

import pytest

from emx import runpod_reservations as rr
from emx import spend
from emx.e9 import budget as B
from emx.e9 import contracts as C

CTX = mp.get_context("spawn")
NOW = C.SIGNOFF_UTC.timestamp() + 86400.0  # one day after sign-off: inside the deadline
AFTER = C.DEADLINE_UTC.timestamp() + 60.0
BAL = 1000.0
RATE = C.H100_SECURE_USD_PER_H
H100 = C.GPU_ORDER[0][0]


@pytest.fixture(autouse=True)
def books(ledger, reservations, tmp_path, monkeypatch):
    monkeypatch.setenv(B.LOCK_ENV, str(tmp_path / "E9-admission.lock"))
    monkeypatch.setenv(B.API_RESERVE_ENV, str(tmp_path / "api_reserve.json"))
    return ledger, reservations


def note(stage: str, step: str, batch: str = "b") -> str:
    return C.ledger_note(stage, step, batch)


def rec(stage: str, usd: float, kind: str = "gpu", n: str = "") -> dict:
    return spend.record(kind, "x", 1, usd, note=n, exp="E9", stage=stage)


def launch_pod(stage: str, budget: float, pod: str, step: str = "S3", slot: str | None = None,
               ack: bool = True) -> str:
    rid = B.reserve_launch(stage, budget, note(stage, step, pod), balance=BAL, now=NOW)
    dl = rr.launch(rid, [(pod, RATE, budget)], NOW, slot=slot)
    if ack:
        rr.ack(pod, dl[pod])
    return rid


def group_total(g: str = "part1") -> float:
    return B.committed()["groups"][g]["total"]


# --- the projection reproduces the plan's paid-step table ---------------------------------------


def test_table_rows_match_the_plan():
    rows = {r["step"]: r for r in B.paid_step_table()["rows"]}
    expect_gpu = {"S2": (5.2, 8.7), "S3": (10.5, 12.3), "S4": (21.0, 24.6), "S5": (10.5, 12.3),
                  "S6": (13.3, 16.1), "S7": (37.1, 44.3)}
    for s, (lo, hi) in expect_gpu.items():
        assert rows[s]["gpu"] == pytest.approx((lo, hi), abs=0.06), s
    assert rows["S1"]["api"] == (24.0, 25.0)
    assert rows["S2"]["api"] == (0.3, 0.3)
    assert rows["X"]["gpu"] == pytest.approx((16.0, 21.0), abs=0.6)  # prereg 4.7-5.9 h
    assert rows["S7"]["api"][0] == pytest.approx(13 + 11 + 3.4, abs=0.01)
    assert [r["barrier"] for r in rows.values() if r["barrier"]] == [2.0] * 5


def test_table_subtotals_match_the_plan():
    sub = B.paid_step_table()["subtotals"]
    assert sub["part1_promising"] == pytest.approx((79, 90), abs=1.0)
    assert sub["part1_stopping"] == pytest.approx((65, 75), abs=1.0)
    assert sub["part2_group_policy"] == pytest.approx((82, 97), abs=1.0)
    assert sub["part2_no_group"] == pytest.approx((72, 85), abs=1.0)
    assert sub["total_promising_group"] == pytest.approx((160, 187), abs=1.0)
    assert 13 <= sub["screen"][0] <= sub["screen"][1] <= 15


def test_project_paths_and_running_steps():
    p = B.project()
    assert p["path"] == ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    assert p["over"] == [] and p["committed"]["total"] == 0
    assert B.project({"pilot": "not_promising"})["path"] == ["S1", "S2", "S3", "S4"]
    assert B.project({"part2": "no"})["path"] == ["S1", "S2", "S3", "S4", "S5"]
    xs = B.project({"pilot": "not_promising", "extra_seeds": True})
    assert xs["path"][-1] == "X" and xs["remaining"]["groups"]["part2"] == 0
    with pytest.raises(ValueError, match="null path"):
        B.project({"extra_seeds": True})
    # a running step counts only what it has not committed yet, per resource, never below 0
    s1 = sum(i["usd"] for i in p["items"] if i["step"] == "S1")
    q = B.project({"steps": {"S1": "running"}, "step_committed": {"S1": {"api": 10.0}}})
    assert sum(i["remaining"] for i in q["items"] if i["step"] == "S1") == pytest.approx(s1 - 10)
    q = B.project({"steps": {"S1": "running"}, "step_committed": {"S1": {"api": 99.0}}})
    assert sum(i["remaining"] for i in q["items"] if i["step"] == "S1") == 0
    d = B.project({"steps": {"S1": "done", "S2": "done"}})
    assert all(i["remaining"] == 0 for i in d["items"] if i["step"] in ("S1", "S2"))


def test_judging_and_cuts_arithmetic():
    assert B.judging_usd(1) == pytest.approx(0.8)
    c2 = B.judging_usd(1, [B.CUT_VULN_UNTUNED])
    assert c2 == pytest.approx(0.8 * (2 * 1040 + 520) / 3540)
    assert B.judging_usd(1, [B.CUT_STANDARD8]) == pytest.approx(0.8)  # confirmation only
    assert B.judging_usd(1, [B.CUT_STANDARD8], confirmation=True) < 0.8


# --- cuts, safe stop and the upper-cost path ----------------------------------------------------


def _committed(part1: float, gpu: float, api: float, part2: float = 0.0) -> dict:
    return {"groups": {"part1": part1, "extra_seeds": 0.0, "part2": part2}, "gpu": gpu,
            "api": api, "total": gpu + api}


def test_cuts_apply_in_order():
    assert B.gate_check({})["cuts"] == []
    base = B.project({"steps": {s: "done" for s in ("S1", "S2", "S3")}, "pilot": "promising"})
    rem = base["remaining"]["groups"]["part1"]
    saving2 = rem - B.project({**base["state"], "cuts": [B.CUT_VULN_UNTUNED]})["remaining"][
        "groups"]["part1"]
    assert saving2 > 0
    # over the part1 limit by less than cut 2's saving: cuts 1 and 2, no stop
    com = 100.0 - rem + saving2 / 2
    g = B.gate_check({**base["state"], "committed": _committed(com, com - 30, 30)})
    assert g["ok"] and g["cuts"] == [B.CUT_SCREEN, B.CUT_VULN_UNTUNED]
    # over by more: every cut in order, then the safe stop
    g = B.gate_check({**base["state"], "committed": _committed(com + 5, com - 25, 30)})
    assert not g["ok"] and g["stop"] == B.STOP_OVER_LIMIT and g["cuts"] == list(C.CUTS)
    assert "stage:part1" in g["over"]


def _walk(c_q_h: float, c_s_h: float) -> list[tuple[str, dict]]:
    """Setup + Part 1 with the checker's fallback panel fired, smoke at its upper estimate, and
    C_Q/C_S measured at (c_q_h, c_s_h) by the cost-and-noise step. Returns each gate's verdict."""
    fired = {"checker_fallback_panel": True}
    api = 25.0 + 11.0  # setup API with the fallback panel, recorded
    st = {"fired": fired, "steps": {"S1": "done"}, "committed": _committed(api, 0, api)}
    out = [("S2", B.gate_check(st))]
    smoke = C.SMOKE_HOURS[1] * RATE
    st = {**st, "steps": {"S1": "done", "S2": "done"},
          "committed": _committed(api + smoke + 0.3, smoke, api + 0.3)}
    out.append(("S3", B.gate_check(st)))
    s3 = (c_s_h + c_q_h) * RATE
    gpu = smoke + s3
    st = {**st, "steps": {**st["steps"], "S3": "done"},
          "measured": {"gpu_type": H100, "c_q_hours": c_q_h, "c_s_hours": c_s_h},
          "committed": _committed(api + 0.3 + gpu, gpu, api + 0.3)}
    out.append(("S4", B.gate_check(st)))
    s4 = 2 * s3 + 2.0 + 2.6
    gpu += 2 * s3 + 2.0
    st = {**st, "steps": {**st["steps"], "S4": "done"}, "pilot": "promising",
          "committed": _committed(api + 0.3 + gpu + 2.6, gpu, api + 2.9)}
    assert s4 > 0
    out.append(("S5", B.gate_check(st)))
    return out


def test_upper_cost_path_with_checker_fallback_stops_at_the_pilot_gate():
    gates = dict(_walk(C.C_Q_HOURS[1], C.C_S_HOURS[1]))
    assert gates["S2"]["ok"] and gates["S3"]["ok"]  # mid estimates fit before measurement
    g = gates["S4"]  # measured at the high end: setup + Part 1 > $100 -> safe stop
    assert not g["ok"] and g["stop"] == B.STOP_OVER_LIMIT and "stage:part1" in g["over"]
    assert g["projection"]["projected"]["groups"]["part1"] > 100


@pytest.mark.parametrize("frac", [0.0, 0.5])
def test_low_and_middle_cost_paths_with_checker_fallback_complete(frac):
    q = C.C_Q_HOURS[0] + frac * (C.C_Q_HOURS[1] - C.C_Q_HOURS[0])
    s = C.C_S_HOURS[0] + frac * (C.C_S_HOURS[1] - C.C_S_HOURS[0])
    assert all(g["ok"] for _, g in _walk(q, s))


def test_content_judge_fallback_stops_before_cost_and_noise():
    api = 24.5 + 51.0
    st = {"fired": {"content_judge_fallback_panel": True}, "steps": {"S1": "done", "S2": "done"},
          "committed": _committed(api + 8.0, 8.0, api)}
    g = B.gate_check(st)
    assert not g["ok"] and "api" in g["over"]


def test_optional_screen_fits_only_with_room():
    p = B.project()
    assert B.optional_fits(p, 13.8, "part2_screen", "gpu")
    tight = B.project({"committed": _committed(0, 0, 0, part2=10.0)})
    assert not B.optional_fits(tight, 13.8, "part2_screen", "gpu")


# --- unit costs, measurement and per-pod budgets ------------------------------------------------


def test_measured_unit_costs_and_gpu_type_change():
    m = B.measured_from_sessions([
        {"kind": "cs", "hours": 2.2, "gpu": H100}, {"kind": "cq", "hours": 1.0, "gpu": H100},
        {"kind": "cq", "hours": 1.1, "gpu": H100}, {"kind": "cq", "hours": 9.0, "gpu": "A100"},
    ])
    assert m == {"gpu_type": H100, "c_q_hours": 1.1, "n_cq": 2, "c_s_hours": 2.2, "n_cs": 1}
    u = B.unit_costs({"measured": m})
    assert u["cq"] == pytest.approx((1.1 * RATE,) * 2) and u["measured"]["c_s"]
    assert u["q2"][0] == pytest.approx((1.1 - 0.35) * RATE)
    comm = B.unit_costs({"measured": m, "gpu": {"type": H100, "rate_usd_h": 2.69}})
    assert comm["cs"][0] == pytest.approx(2.2 * 2.69)  # same type, new rate
    a100 = B.unit_costs({"measured": m, "gpu": {"type": "NVIDIA A100-SXM4-80GB",
                                                "rate_usd_h": 1.39}})
    assert a100["flags"] and not a100["measured"]["c_q"]
    assert a100["cq"] == pytest.approx((C.C_Q_HOURS[0] * RATE, C.C_Q_HOURS[1] * RATE))


def test_pod_budgets_sum_to_the_reservation():
    for step in ("S2", "S3", "S4", "S5", "X", "S6", "S7"):
        lp = B.launch_plan(step)
        assert sum(lp["budgets"]) == pytest.approx(lp["usd"])
        assert len(lp["budgets"]) == len(B.step_pods(step))
        u = B.unit_costs()
        for pod, b in zip(lp["pods"], lp["budgets"], strict=True):
            assert b >= C.POD_BUDGET_FACTOR * sum(u[r][1] for r in pod) - 1e-9
        p = B.project({"pilot": "not_promising", "extra_seeds": True} if step == "X" else {})
        own = sum(i["remaining"] for i in p["items"] if i["step"] == step)
        rm = B.launch_plan(step, p["state"])["rm"]
        assert rm["total"] == pytest.approx(p["remaining"]["total"] - own)
    assert len(B.launch_plan("S7", {"group_policy": False})["budgets"]) == 3
    with pytest.raises(ValueError):
        B.launch_plan("S1")


# --- backstop and re-arm ------------------------------------------------------------------------


def test_backstop_kill_line():
    assert B.backstop_kill_usd() == pytest.approx(138.0)  # min(145, 200 - 62)
    rec("setup", 10.0, kind="judge")
    B.reserve_api("setup", 5.0, note("setup", "select", "x"), now=NOW)
    assert B.backstop_kill_usd() == pytest.approx(123.0)  # 200 - 62 - 15
    B.set_api_reserve(30.0, "S3")
    assert B.api_reserve() == 30.0
    assert B.backstop_kill_usd() == pytest.approx(145.0)  # min(145, 155)


def test_backstop_moves_on_every_api_change_with_no_other_call():
    """R1 finding 11: the watchdog recomputes the backstop live from the ledger and the reserve
    file, so each API change moves it at once (no after_api_change or re-arm needed)."""
    B.set_api_reserve(60.0, "S1")
    assert B.backstop_kill_usd() == pytest.approx(140.0)  # 200 - 60
    rid = B.reserve_api("setup", 8.0, note("setup", "select", "bk"), now=NOW)
    assert B.backstop_kill_usd() == pytest.approx(132.0)  # reservation counted at once
    spend.mark_submitted(rid, exp="E9")
    spend.settle_api(rid, 3.0, exp="E9")
    assert B.backstop_kill_usd() == pytest.approx(137.0)  # settled below the reservation
    B.set_api_reserve(70.0, "S2")
    assert B.backstop_kill_usd() == pytest.approx(127.0)  # 200 - 70 - 3


def test_after_api_change_without_pods_returns_the_backstop():
    def runner(*a, **k):
        raise AssertionError("no pods: nothing to re-arm")

    assert B.after_api_change(runner=runner) == {"backstop_kill_usd": 138.0, "slots": [],
                                                 "failed": {}}


def test_after_api_change_rearms_and_cleans_up_a_failing_slot():
    launch_pod("part1", 10.0, "podA", slot="_A")
    calls = []
    ok = B.after_api_change(runner=lambda cmd, **k: (calls.append(cmd), (0, ""))[1])
    assert ok["slots"] == ["_A"] and calls[0][:2] == ["bash", "scripts/pod_push.sh"]
    cleaned = []
    with pytest.raises(B.RearmFailed):
        B.after_api_change(runner=lambda cmd, **k: (1, "E9 killswitch _A: NOT armed"),
                           cleanup=lambda s, why: cleaned.append((s, why)) or 0)
    assert cleaned and cleaned[0][0] == "_A"


# --- decision helpers ---------------------------------------------------------------------------


def test_extra_seeds_launch_only_if():
    ok, _ = B.extra_seeds_launch_ok({"corr_minus_orig": {1: -0.1, 2: -0.02},
                                     "corr_minus_bg": {1: -0.3, 2: -0.01}})
    assert ok
    ok, why = B.extra_seeds_launch_ok({"corr_minus_orig": {1: -0.1, 2: 0.0},
                                       "corr_minus_bg": {1: -0.3, 2: -0.01}})
    assert not ok and why.startswith("underpowered; more seeds cannot pass")
    assert not B.extra_seeds_launch_ok({"corr_minus_orig": {1: -0.1, 2: -0.1},
                                        "corr_minus_bg": {1: 0.2, 2: -0.01}})[0]
    with pytest.raises(KeyError):
        B.extra_seeds_launch_ok({"corr_minus_orig": {1: -0.1, 2: -0.1}})


def test_barrier_kill_at_ten_dollars():
    launch_pod("part1", 20.0, "podB", step="S4")
    assert B.accrued_usd(["podB"], NOW + 3600) == pytest.approx(RATE)
    acc = B.accrued_usd(["podB"], NOW + 3 * 3600)  # 10.47
    assert B.barrier_kill_due(acc, review_pending=True)
    assert not B.barrier_kill_due(acc, review_pending=False)
    assert not B.barrier_kill_due(9.99, review_pending=True)


def test_deadline_helpers_and_refusal(ledger):
    assert not B.deadline_passed(C.DEADLINE_UTC.timestamp() - 1)
    assert B.deadline_passed(C.DEADLINE_UTC)
    with pytest.raises(B.DeadlinePassed):
        B.reserve_api("part1", 1.0, note("part1", "S4", "judge"), now=AFTER)
    with pytest.raises(B.DeadlinePassed):
        B.reserve_launch("part1", 1.0, note("part1", "S4", "pod"), balance=BAL, now=AFTER)
    assert spend.read_entries() == [] and rr.read_rows() == []
    # settlement and extensions of an arm already running stay allowed after the deadline
    t0 = C.DEADLINE_UTC.timestamp() - 3600
    rid = B.reserve_launch("part1", 10.0, note("part1", "S4", "podD"), balance=BAL, now=t0)
    rr.ack("podD", rr.launch(rid, [("podD", RATE, 10.0)], t0)["podD"])
    B.extend("podD", rr.pod_deadline("podD") + 600, balance=BAL, now=AFTER)
    out = B.settle_pod(rid, "podD", 4.0, hours=1.2)
    assert out["charged_now"] and out["stage"] == "part1"


# --- the admission protocol ---------------------------------------------------------------------


def test_note_must_name_the_stage():
    with pytest.raises(ValueError, match="ledger_note"):
        B.reserve_api("part1", 1.0, "E9:setup:S1:x", now=NOW)
    with pytest.raises(ValueError, match="ledger_note"):
        B.reserve_launch("part1", 1.0, "no-stage", balance=BAL, now=NOW)


def test_a_reservation_without_a_stage_blocks_admission():
    rr.reserve("E9", 5.0, "untagged", balance=BAL, now=NOW)
    with pytest.raises(B.AdmissionError, match="names no stage"):
        B.reserve_api("setup", 1.0, note("setup", "select"), now=NOW)


def test_stage_and_mandatory_reserve_limits():
    rec("part1", 95.0)
    with pytest.raises(B.AdmissionRefused, match="stage part1"):
        B.reserve_launch("part1", 6.0, note("part1", "S4", "p"), balance=BAL, now=NOW)
    # another group still fits; R_m passed by the caller is counted
    B.reserve_launch("part2_mandatory", 30.0, note("part2_mandatory", "S6", "p"), balance=BAL,
                     now=NOW)
    with pytest.raises(B.AdmissionRefused, match="stage part2"):
        B.reserve_launch("part2_mandatory", 30.0, note("part2_mandatory", "S7", "p"),
                         balance=BAL, now=NOW, rm={"groups": {"part2": 50.0}, "gpu": 50.0})
    # the GPU limit counts E9 commitments of every group
    with pytest.raises(B.AdmissionRefused, match="gpu"):
        B.reserve_launch("extra_seeds", 21.0, note("extra_seeds", "X", "p"), balance=BAL,
                         now=NOW)


def test_each_dollar_counted_once_through_reserve_launch_settle():
    rid = B.reserve_launch("part1", 10.0, note("part1", "S3", "bg"), balance=BAL, now=NOW)
    assert group_total() == pytest.approx(10.0)  # unlaunched: full reservation
    dl = rr.launch(rid, [("p1", RATE, 4.0), ("p2", RATE, 5.0)], NOW)
    assert group_total() == pytest.approx(9.0)  # pods' budgets only
    for p, d in dl.items():
        rr.ack(p, d)
    B.settle_pod(rid, "p1", 3.0, hours=0.86)
    assert group_total() == pytest.approx(3.0 + 5.0)  # charge + the other pod's commitment
    again = B.settle_pod(rid, "p1", 3.5, hours=1.0)  # retry: charged once, settled once
    assert not again["charged_now"] and group_total() == pytest.approx(8.0)
    B.settle_pod(rid, "p2", 4.5, hours=1.3)
    assert group_total() == pytest.approx(7.5)
    assert spend.stage_totals("E9")["part1"]["recorded_usd"] == pytest.approx(7.5)
    snap = B.committed()
    assert snap["by_step"]["S3"]["gpu"] == pytest.approx(7.5) and not snap["commitments"]
    # API side: reservation -> charges -> settlement
    a = B.reserve_api("setup", 2.0, note("setup", "select", "s"), now=NOW)
    assert group_total() == pytest.approx(9.5)
    spend.record("judge", "luna", 3, 0.5, exp="E9", reservation_id=a)
    assert group_total() == pytest.approx(9.5)
    spend.settle_api(a, 1.2, exp="E9")
    assert group_total() == pytest.approx(8.7)
    assert B.committed()["by_step"]["S1"]["api"] == pytest.approx(1.2)


def test_settled_without_charge_still_counts():
    rid = launch_pod("part1", 10.0, "pz")
    rr.settle(rid, 6.0, pod_id="pz")  # a bypass of settle_pod: no ledger charge
    assert group_total() == pytest.approx(6.0)


def test_time_passing_does_not_lower_an_armed_pods_commitment():
    rec("part1", 85.0)
    launch_pod("part1", 10.0, "pt")
    snaps = [B.committed()["groups"]["part1"]["total"] for _ in range(2)]
    assert snaps == [95.0, 95.0]
    # liability (rate x time left) falls with time; the counted commitment does not
    late = NOW + 2.5 * 3600
    assert rr.state(now=late)["launched_liability_usd"] < 2.0
    with pytest.raises(B.AdmissionRefused, match="stage part1"):
        B.reserve_launch("part1", 6.0, note("part1", "S4", "p"), balance=BAL, now=late)
    with pytest.raises(B.AdmissionRefused, match="stage part1"):
        B.reserve_api("part1", 6.0, note("part1", "S4", "judge"), now=late)


def test_extension_past_the_stage_limit_is_refused():
    rec("part1", 85.0)
    launch_pod("part1", 10.0, "pe")
    allowed = rr.pod_deadline("pe")
    with pytest.raises(B.AdmissionRefused, match="stage part1"):
        B.extend("pe", allowed + 2 * 3600, balance=BAL, now=NOW)  # +$6.98 -> $101.98
    assert rr.pod_deadline("pe") == allowed
    B.extend("pe", allowed + 3600, balance=BAL, now=NOW)  # +$3.49 -> $98.49
    assert group_total() == pytest.approx(85.0 + 10.0 + RATE, abs=0.01)
    ext = [r for r in rr.read_rows() if r.get("extends_pod") == "pe"]
    assert len(ext) == 1 and ext[0]["note"].startswith("E9:part1:extend:pe")


# --- races (spawn, barrier: exactly one wins) ---------------------------------------------------


def _race(kind, stage, usd, n, now, barrier, q):
    from emx import spend as sp
    from emx.e9 import budget as b

    barrier.wait()
    try:
        if kind == "gpu":
            q.put(("won", b.reserve_launch(stage, usd, n, balance=1000.0, now=now)))
        else:
            q.put(("won", b.reserve_api(stage, usd, n, now=now)))
    except sp.BudgetExceeded:
        q.put(("lost", None))
    except Exception as e:  # noqa: BLE001 - reported to the parent
        q.put(("error", repr(e)))


@pytest.mark.parametrize("kinds", [("gpu", "gpu"), ("api", "gpu"), ("gpu", "api")])
def test_processes_racing_for_a_stages_last_dollars(kinds):
    rec("part1", 90.0)
    barrier, q = CTX.Barrier(len(kinds)), CTX.Queue()
    procs = [CTX.Process(target=_race, args=(k, "part1", 6.0, note("part1", "S4", f"r{i}"), NOW,
                                             barrier, q))
             for i, k in enumerate(kinds)]
    for p in procs:
        p.start()
    results = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    assert sorted(r[0] for r in results) == ["lost", "won"], results
    assert group_total() == pytest.approx(96.0)


def _read_under_lock(q):
    from emx.e9 import budget as b

    with b.admission_lock():
        q.put(b.committed()["groups"]["part1"]["total"])


def test_settlement_interleaved_with_an_admission_never_drops_the_pod(monkeypatch):
    rid = launch_pod("part1", 10.0, "ps")
    assert group_total() == pytest.approx(10.0)
    q = CTX.Queue()
    seen = {}
    real = spend.record_once

    def record_then_probe(*a, **k):
        out = real(*a, **k)
        # between the charge and the settle row: a lock-free read counts the pod twice, never 0
        seen["between"] = B.committed()["groups"]["part1"]["total"]
        p = CTX.Process(target=_read_under_lock, args=(q,))
        p.start()
        seen["proc"] = p
        with pytest.raises(queue.Empty):  # the admission lock is held: the reader waits
            q.get(timeout=1.5)
        return out

    monkeypatch.setattr(spend, "record_once", record_then_probe)
    B.settle_pod(rid, "ps", 7.0, hours=2.0)
    assert seen["between"] == pytest.approx(17.0)
    assert q.get(timeout=60) == pytest.approx(7.0)  # after: the charge, once
    seen["proc"].join(timeout=60)
    assert seen["proc"].exitcode == 0


def test_gather_state_attributes_spend_to_steps():
    a = B.reserve_api("setup", 3.0, note("setup", "select", "score-luna"), now=NOW)
    spend.settle_api(a, 2.5, exp="E9")
    launch_pod("part1", 12.0, "pg", step="S3")
    st = B.gather_state({"steps": {"S1": "running", "S3": "running"}})
    assert st["step_committed"]["S1"]["api"] == pytest.approx(2.5)
    assert st["step_committed"]["S3"]["gpu"] == pytest.approx(12.0)
    assert st["committed"]["total"] == pytest.approx(14.5)
    p = B.project(st)
    s3 = [i for i in p["items"] if i["step"] == "S3"]
    assert all(i["remaining"] == 0 for i in s3)  # its reservation covers it
