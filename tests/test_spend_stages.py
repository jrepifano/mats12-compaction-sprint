"""Stage limits in the spend ledger (E9 prereg §7.3 "Stage accounting"; E9 implementation plan,
"Stage enforcement in spend.py"). Offline; every ledger is a tmp file.

Invariants: every E9 row carries a known stage; a reservation's stage is kept by its charges,
settlement, release and crash recovery; a stage group's recorded + reserved + projected spend
never passes its limit, even when processes race; other experiments are unchanged.
"""

from __future__ import annotations

import multiprocessing as mp
import os

import pytest

from emx import spend
from emx.e9 import contracts as C

EXP = "E9"
CTX = mp.get_context("spawn")


def test_caps_match_contracts():
    c = spend.caps_for(EXP)
    assert (c.hard_cap_usd, c.gpu_limit_usd, c.judge_limit_usd) == (200.0, 145.0, 75.0)
    assert c.stage_limits == {"part1": 100.0, "extra_seeds": 25.0, "part2": 105.0}
    assert set(c.stage_group) == set(C.STAGES)


def test_e9_rows_need_a_known_stage(tmp_path):
    L = tmp_path / "c.jsonl"
    with pytest.raises(ValueError, match="stage tag"):
        spend.record("judge", "luna", 1, 0.1, exp=EXP, ledger=L)
    with pytest.raises(ValueError, match="unknown stage"):
        spend.record("judge", "luna", 1, 0.1, exp=EXP, ledger=L, stage="part3")
    with pytest.raises(ValueError, match="stage tag"):
        spend.reserve_api(EXP, 1.0, "b", ledger=L)
    with pytest.raises(ValueError, match="stage tag"):
        spend.check_total(1.0, "judge", exp=EXP, ledger=L)
    with pytest.raises(ValueError, match="stage tag"):
        spend.record_once("k", "gpu", "pod", 1, 1.0, exp=EXP, ledger=L)
    e = spend.record("gpu", "pod", 1, 2.0, exp=EXP, ledger=L, stage="setup")
    assert e["stage"] == "setup"
    assert spend.read_entries(L) == [e]


def test_other_experiments_unchanged(tmp_path):
    L = tmp_path / "c.jsonl"
    e = spend.record("judge", "luna", 1, 0.1, exp="E3", ledger=L)
    assert "stage" not in e
    rid = spend.reserve_api("E3", 1.0, "b", ledger=L)
    assert spend.reservation_states(spend.read_entries(L))[rid]["stage"] is None
    assert spend.stage_totals("E3", L) == {}
    b = spend.check_total(1.0, "judge", exp="E3", ledger=L)
    assert "stage" not in b


def test_reservation_lifecycle_keeps_its_stage(tmp_path):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 2.0, "E9:setup:s1:judge", ledger=L, stage="setup")
    spend.mark_submitted(rid, EXP, L)
    spend.record("judge", "luna", 10, 0.5, exp=EXP, ledger=L, reservation_id=rid)
    with pytest.raises(spend.ReservationError, match="stage"):
        spend.record("judge", "luna", 1, 0.1, exp=EXP, ledger=L, reservation_id=rid,
                     stage="part1")
    spend.settle_api(rid, 1.2, exp=EXP, ledger=L)
    rows = [r for r in spend.read_entries(L) if r.get("reservation_id") == rid]
    assert len(rows) == 4 and {r["stage"] for r in rows} == {"setup"}
    st = spend.stage_totals(EXP, L)
    assert st["part1"] == {"recorded_usd": 1.2, "reserved_usd": 0.0, "limit_usd": 100.0}

    rid2 = spend.reserve_api(EXP, 3.0, "E9:part2_mandatory:p2:judge", ledger=L,
                             stage="part2_mandatory")
    spend.release_api(rid2, EXP, L)
    rid3 = spend.reserve_api(EXP, 4.0, "E9:part1:crash", ledger=L, stage="part1")
    spend.mark_submitted(rid3, EXP, L)
    assert spend.recover_uncertain(EXP, "E9:part1:crash", L) == [rid3]
    for r in spend.read_entries(L):
        if r.get("reservation_id") in (rid2, rid3):
            assert r["stage"] == ("part2_mandatory" if r["reservation_id"] == rid2 else "part1")
    st = spend.stage_totals(EXP, L)
    assert st["part1"]["recorded_usd"] == pytest.approx(5.2)
    assert st["part2"]["recorded_usd"] == 0.0


def test_stage_limit_refuses_while_total_allows(tmp_path):
    L = tmp_path / "c.jsonl"
    spend.record("gpu", "pod", 1, 95.0, exp=EXP, ledger=L, stage="part1")
    # total $95 + $6 = $101 < $200 cap, but part1's group would reach $101 > $100
    with pytest.raises(spend.BudgetExceeded, match="stage part1"):
        spend.check_total(6.0, "gpu", exp=EXP, ledger=L, stage="part1")
    with pytest.raises(spend.BudgetExceeded, match="stage part1"):
        spend.reserve_api(EXP, 6.0, "b", ledger=L, stage="setup")  # setup shares part1's limit
    b = spend.check_total(6.0, "gpu", exp=EXP, ledger=L, stage="part2_mandatory")
    assert b["ok"] and b["stage_group"] == "part2"
    # running pods count against the stage being admitted (conservative)
    with pytest.raises(spend.BudgetExceeded, match="stage part1"):
        spend.check_total(1.0, "judge", running_pod_usd=4.5, exp=EXP, ledger=L, stage="part1")


def test_open_reservations_count_against_their_stage(tmp_path):
    L = tmp_path / "c.jsonl"
    spend.reserve_api(EXP, 20.0, "a", ledger=L, stage="extra_seeds")
    with pytest.raises(spend.BudgetExceeded, match="stage extra_seeds"):
        spend.reserve_api(EXP, 5.5, "b", ledger=L, stage="extra_seeds")
    spend.reserve_api(EXP, 5.0, "c", ledger=L, stage="extra_seeds")  # exactly at $25
    assert spend.stage_totals(EXP, L)["extra_seeds"]["reserved_usd"] == 25.0


def _race_proc(path, usd, barrier, q):
    barrier.wait()
    try:
        q.put(spend.reserve_api(EXP, usd, f"race-{os.getpid()}", ledger=path, stage="part2_screen"))
    except spend.BudgetExceeded:
        q.put(None)


@pytest.mark.parametrize("n_proc,room,usd,winners", [(2, 2.0, 1.5, 1), (8, 4.5, 1.5, 3)])
def test_processes_racing_for_a_stages_last_dollars(tmp_path, n_proc, room, usd, winners):
    L = tmp_path / "c.jsonl"
    # part2's limit is $105; leave `room` dollars in it, well inside the total and API limits
    spend.record("gpu", "pod", 1, 105.0 - room, exp=EXP, ledger=L, stage="part2_mandatory")
    barrier, q = CTX.Barrier(n_proc), CTX.Queue()
    procs = [CTX.Process(target=_race_proc, args=(str(L), usd, barrier, q)) for _ in range(n_proc)]
    for p in procs:
        p.start()
    results = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    won = [r for r in results if r]
    assert len(won) == winners
    st = spend.stage_totals(EXP, L)["part2"]
    assert st["recorded_usd"] + st["reserved_usd"] <= 105.0 + 1e-9


def test_check_total_excludes_own_reservation(tmp_path):
    L = tmp_path / "c.jsonl"
    r = spend.reserve_api(EXP, 6.0, "own", ledger=L, stage="part1")
    spend.record("gpu", "pod", 1, 94.0, exp=EXP, ledger=L, stage="part1")
    # working under its own reservation: its $6 is not counted on top of the $6 projected
    b = spend.check_total(6.0, "judge", exp=EXP, ledger=L, reservation_id=r, stage="part1")
    assert b["stage_after_usd"] == pytest.approx(100.0)
