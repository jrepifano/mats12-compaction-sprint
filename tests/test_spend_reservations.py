"""API reservations in the spend ledger (E3 prereg §7; E3 implementation plan v2 fix 4, v3 fixes 2-3,
v4 fix 1). Offline; every ledger is a tmp file.

The invariant under test: at every instant an API batch counts once, as
max(its open reservation, its charges so far), in `check_total`, `totals` and `watchdog_kill_usd`;
a settled batch counts as its charges only; and the state survives crashes, reordering and
`scripts/merge_ledger.py`.
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing as mp
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from emx import spend
from emx.e3 import contracts as e3c

ROOT = Path(__file__).resolve().parents[1]
EXP = "E3"
API = e3c.API_LIMIT_USD  # $10
CTX = mp.get_context("spawn")


def committed(L, exp=EXP) -> float:
    """Recorded judge spend plus open liabilities, as the guards see it."""
    t = spend.totals(exp, L)
    return round(t["judge_usd"] + t["judge_reserved_usd"], 6)


@pytest.fixture
def tst(monkeypatch):
    """A test experiment whose kill line depends on API spend ($20 cap, $15 GPU, $10 API)."""
    monkeypatch.setitem(spend.CAPS, "TST", spend.Caps(20.0, 15.0, 10.0, 0.0))
    return "TST"


# --- basic lifecycle --------------------------------------------------------------------------


def test_reserve_row_shape_and_spent_unchanged(tmp_path):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 2.5, "col-a", ledger=L)
    (row,) = spend.read_entries(L)
    assert row["reservation_id"] == rid and row["op"] == "reserve" and row["reserve_usd"] == 2.5
    assert row["usd"] == 0 and row["kind"] == "judge" and row["units"] == 0
    assert row["resource"] == spend.RESERVATION_RESOURCE and row["exp"] == EXP
    assert len(row["id"]) == 32 and row["id"] != rid
    assert spend.spent("judge", EXP, L) == 0.0
    t = spend.totals(EXP, L)
    assert t["judge_usd"] == 0.0 and t["judge_reserved_usd"] == 2.5 and t["total_usd"] == 2.5
    assert t["open_reservations"] == 1
    assert spend.open_reservations(EXP, L)[rid] == {
        "exp": EXP,
        "reserve_usd": 2.5,
        "charges": 0.0,
        "liability": 2.5,
        "note": "col-a",
        "submitted": False,
        "utc": row["utc"],
    }


def test_batch_counts_once_at_every_instant(tmp_path, tst):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(tst, 3.0, "col", ledger=L)
    seq = [committed(L, tst)]
    spend.mark_submitted(rid, tst, L)
    seq.append(committed(L, tst))
    spend.record("judge", "luna", 10, 1.0, exp=tst, ledger=L, reservation_id=rid)
    seq.append(committed(L, tst))
    spend.record("judge", "luna", 10, 1.5, exp=tst, ledger=L, reservation_id=rid)
    seq.append(committed(L, tst))
    assert seq == [3.0, 3.0, 3.0, 3.0]
    assert spend.watchdog_kill_usd(tst, L) == 15.0  # min(15, 20 - 3)
    # past the reservation: charges are real and count in full
    spend.record("judge", "luna", 10, 1.0, exp=tst, ledger=L, reservation_id=rid)
    assert committed(L, tst) == 3.5
    s = spend.settle_api(rid, 3.5, ledger=L)
    assert s["settled_usd"] == 0.0 and s["closed"] and committed(L, tst) == 3.5
    assert spend.spent("judge", tst, L) == 3.5
    assert spend.open_reservations(tst, L) == {}
    # every check_total reading along the way matched: here, after settle
    b = spend.check_total(0.0, "judge", exp=tst, ledger=L)
    assert b["judge_after_usd"] == 3.5 and b["judge_reserved_usd"] == 0.0


def test_settle_with_total_charges_delta_once(tmp_path):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 3.0, "col", ledger=L)
    spend.mark_submitted(rid, EXP, L)
    spend.record("judge", "luna", 1, 1.0, exp=EXP, ledger=L, reservation_id=rid)
    s = spend.settle_api(rid, 2.5, exp=EXP, ledger=L)
    assert s["settled_usd"] == 1.5 and s["charges"] == 2.5 and s["liability"] == 0.0
    close = spend.read_entries(L)[-1]
    assert close["op"] == "close" and close["usd"] == 1.5 and close["reservation_id"] == rid
    assert spend.spent("judge", EXP, L) == 2.5 and committed(L) == 2.5
    # idempotent: a second settle (with any total) adds nothing
    n = len(spend.read_entries(L))
    again = spend.settle_api(rid, 9.0, ledger=L)
    assert again["settled_usd"] == 0.0 and again["closed"]
    assert len(spend.read_entries(L)) == n and spend.spent("judge", EXP, L) == 2.5


def test_settle_total_below_charges_never_refunds(tmp_path):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 3.0, "col", ledger=L)
    spend.record("judge", "luna", 1, 1.0, exp=EXP, ledger=L, reservation_id=rid)
    assert spend.settle_api(rid, 0.5, ledger=L)["settled_usd"] == 0.0
    assert spend.spent("judge", EXP, L) == 1.0 and committed(L) == 1.0


def test_settle_unknown_total_is_conservative(tmp_path):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 3.0, "col", ledger=L)
    spend.mark_submitted(rid, EXP, L)
    spend.record("judge", "luna", 1, 1.0, exp=EXP, ledger=L, reservation_id=rid)
    s = spend.settle_api(rid, None, ledger=L)
    assert s["settled_usd"] == 2.0 and spend.spent("judge", EXP, L) == 3.0 and committed(L) == 3.0
    assert spend.settle_api(rid, None, ledger=L)["settled_usd"] == 0.0
    assert spend.spent("judge", EXP, L) == 3.0


def test_release_and_its_refusals(tmp_path):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 3.0, "col", ledger=L)
    s = spend.release_api(rid, EXP, L)
    assert s["closed"] and committed(L) == 0.0 and spend.read_entries(L)[-1]["usd"] == 0
    n = len(spend.read_entries(L))
    spend.release_api(rid, ledger=L)  # no-op
    spend.settle_api(rid, 5.0, ledger=L)  # no-op on a closed reservation
    assert len(spend.read_entries(L)) == n
    # the same batch key can reserve again once the old one is closed
    rid2 = spend.reserve_api(EXP, 3.0, "col", ledger=L)
    spend.mark_submitted(rid2, EXP, L)
    with pytest.raises(spend.ReservationError, match="settle"):
        spend.release_api(rid2, ledger=L)
    rid3 = spend.reserve_api(EXP, 1.0, "col3", ledger=L)
    spend.record("judge", "luna", 1, 0.2, exp=EXP, ledger=L, reservation_id=rid3)
    with pytest.raises(spend.ReservationError, match="settle"):
        spend.release_api(rid3, ledger=L)  # charged without a submitted row: still settle-only
    assert committed(L) == 4.0


def test_mark_submitted_idempotent_and_refused_when_closed(tmp_path):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 1.0, "col", ledger=L)
    spend.mark_submitted(rid, EXP, L)
    spend.mark_submitted(rid, EXP, L)
    assert sum(1 for e in spend.read_entries(L) if e.get("op") == "submitted") == 1
    with pytest.raises(spend.ReservationError, match="belongs to"):
        spend.mark_submitted(rid, "I9", L)
    spend.settle_api(rid, 0.4, ledger=L)
    with pytest.raises(spend.ReservationError, match="closed"):
        spend.mark_submitted(rid, ledger=L)
    with pytest.raises(spend.ReservationError, match="unknown"):
        spend.mark_submitted("f" * 32, ledger=L)


# --- admission --------------------------------------------------------------------------------


def test_reserve_refused_over_api_limit_and_counts_open_reservations(tmp_path):
    L = tmp_path / "c.jsonl"
    spend.record("judge", "luna", 1, 6.0, exp=EXP, ledger=L)
    spend.reserve_api(EXP, 3.0, "a", ledger=L)
    spend.reserve_api(EXP, 1.0, "b", ledger=L)  # exactly at the $10 limit
    with pytest.raises(spend.BudgetExceeded, match="judge limit") as ei:
        spend.reserve_api(EXP, 0.01, "c", ledger=L)
    assert ei.value.breakdown["judge_reserved_usd"] == 4.0
    assert spend.find_open(EXP, "c", L) is None and len(spend.open_reservations(EXP, L)) == 2


def test_reserve_counts_gpu_side(tmp_path, tst):
    L = tmp_path / "c.jsonl"
    spend.record("gpu", "pod", 1, 9.0, exp=tst, ledger=L)
    spend.record("judge", "luna", 1, 8.0, exp=tst, ledger=L)
    spend.reserve_api(tst, 1.0, "a", running_pod_usd=2.0, ledger=L)  # 9+2+8+1 = 20: allowed
    with pytest.raises(spend.BudgetExceeded, match="hard cap"):
        spend.reserve_api(tst, 0.5, "b", running_pod_usd=2.0, ledger=L)
    with pytest.raises(spend.BudgetExceeded, match="GPU limit"):
        spend.reserve_api(tst, 0.0, "b", running_pod_usd=6.5, ledger=L)


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_reserve_rejects_bad_amounts(tmp_path, bad):
    with pytest.raises(ValueError):
        spend.reserve_api(EXP, bad, "x", ledger=tmp_path / "c.jsonl")


def test_reserve_rejects_empty_note_and_duplicate_open_batch(tmp_path):
    L = tmp_path / "c.jsonl"
    with pytest.raises(spend.ReservationError, match="note"):
        spend.reserve_api(EXP, 1.0, "", ledger=L)
    spend.reserve_api(EXP, 1.0, "col", ledger=L)
    with pytest.raises(spend.ReservationError, match="still open"):
        spend.reserve_api(EXP, 1.0, "col", ledger=L)
    spend.reserve_api("I9", 1.0, "col", ledger=L)  # another experiment's key is independent


def _race(args):
    path, exp, usd, barrier = args
    barrier.wait()
    try:
        return spend.reserve_api(exp, usd, f"race-{os.getpid()}", ledger=path)
    except spend.BudgetExceeded:
        return None


def _race_proc(path, usd, barrier, q):
    q.put(_race((path, EXP, usd, barrier)))


@pytest.mark.parametrize(
    "n_proc,room,usd,winners", [(2, 2.0, 1.5, 1), (8, 2.0, 1.5, 1), (8, 4.5, 1.5, 3)]
)
def test_processes_racing_for_the_last_dollars(tmp_path, n_proc, room, usd, winners):
    L = tmp_path / "c.jsonl"
    spend.record("judge", "luna", 1, API - room, exp=EXP, ledger=L)
    barrier, q = CTX.Barrier(n_proc), CTX.Queue()
    procs = [CTX.Process(target=_race_proc, args=(str(L), usd, barrier, q)) for _ in range(n_proc)]
    for p in procs:
        p.start()
    results = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    won = [r for r in results if r]
    assert len(won) == winners and len(results) - len(won) == n_proc - winners
    assert set(spend.open_reservations(EXP, L)) == set(won)
    assert committed(L) == pytest.approx(API - room + winners * usd) and committed(L) <= API


def _cycles(args):
    path, wid, n = args
    for i in range(n):
        rid = spend.reserve_api(EXP, 0.2, f"w{wid}-{i}", ledger=path)
        spend.mark_submitted(rid, EXP, path)
        spend.record("judge", "luna", 1, 0.1, exp=EXP, ledger=path, reservation_id=rid)
        spend.settle_api(rid, 0.15, exp=EXP, ledger=path)


def test_concurrent_lifecycles_settle_exactly(tmp_path):
    L = tmp_path / "c.jsonl"
    n_proc, n_each = 4, 10
    with CTX.Pool(n_proc) as pool:
        pool.map(_cycles, [(str(L), w, n_each) for w in range(n_proc)])
    assert spend.open_reservations(EXP, L) == {}
    assert spend.spent("judge", EXP, L) == pytest.approx(n_proc * n_each * 0.15)
    assert committed(L) == pytest.approx(n_proc * n_each * 0.15)
    ids = [e["id"] for e in spend.read_entries(L)]
    assert len(ids) == len(set(ids)) == n_proc * n_each * 4


# --- crashes and recovery ---------------------------------------------------------------------


def _die_after_submit(path, charge):
    rid = spend.reserve_api(EXP, 2.0, "col-x", ledger=path)
    spend.mark_submitted(rid, EXP, path)
    if charge:
        spend.record("judge", "luna", 3, charge, exp=EXP, ledger=path, reservation_id=rid)
    os._exit(1)  # real process death: no finally, no settle


@pytest.mark.parametrize("charge", [0.0, 0.7])
def test_real_process_death_then_recover_uncertain(tmp_path, charge):
    L = tmp_path / "c.jsonl"
    p = CTX.Process(target=_die_after_submit, args=(str(L), charge))
    p.start()
    p.join(timeout=60)
    assert p.exitcode == 1
    # the dead batch still counts its full reservation, once
    assert committed(L) == 2.0 and spend.watchdog_kill_usd(EXP, L) == pytest.approx(35.0)
    (rid,) = spend.open_reservations(EXP, L)
    assert spend.find_open(EXP, "col-x", L) is None  # submitted: not reusable
    with pytest.raises(spend.ReservationError, match="still open"):
        spend.reserve_api(EXP, 1.0, "col-x", ledger=L)  # must recover first
    assert spend.recover_uncertain(EXP, "col-x", L) == [rid]
    assert spend.spent("judge", EXP, L) == 2.0 and committed(L) == 2.0
    assert spend.recover_uncertain(EXP, "col-x", L) == []  # idempotent
    rid2 = spend.reserve_api(EXP, 1.0, "col-x", ledger=L)  # remaining work: fresh admission
    assert rid2 != rid and committed(L) == 3.0


def test_recover_uncertain_leaves_unsubmitted_and_other_batches(tmp_path):
    L = tmp_path / "c.jsonl"
    fresh = spend.reserve_api(EXP, 1.0, "a", ledger=L)
    other = spend.reserve_api(EXP, 1.0, "b", ledger=L)
    spend.mark_submitted(other, EXP, L)
    assert spend.recover_uncertain(EXP, "a", L) == []
    assert spend.find_open(EXP, "a", L) == fresh
    assert spend.recover_uncertain("I9", "b", L) == []
    assert set(spend.open_reservations(EXP, L)) == {fresh, other}


def test_crash_after_charge_before_progress_is_charged_honestly(tmp_path):
    """v3 fix 3: rows re-judged after a crash are charged again, counted, never dropped."""
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 2.0, "col", ledger=L)
    spend.mark_submitted(rid, EXP, L)
    spend.record("judge", "luna", 5, 0.5, exp=EXP, ledger=L, reservation_id=rid)
    # crash: progress not saved; recovery settles in full; the re-run pays for the rows again
    spend.recover_uncertain(EXP, "col", L)
    rid2 = spend.reserve_api(EXP, 2.0, "col", ledger=L)
    spend.mark_submitted(rid2, EXP, L)
    spend.record("judge", "luna", 5, 0.5, exp=EXP, ledger=L, reservation_id=rid2)
    spend.settle_api(rid2, 0.9, ledger=L)
    assert spend.spent("judge", EXP, L) == pytest.approx(2.9) and committed(L) == pytest.approx(2.9)


# --- ownership and guard arithmetic -----------------------------------------------------------


def test_headroom_checks_ownership_and_open(tmp_path):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 2.0, "col", ledger=L)
    assert spend.reservation_headroom(rid, EXP, L) == 2.0
    spend.record("judge", "luna", 1, 0.75, exp=EXP, ledger=L, reservation_id=rid)
    assert spend.reservation_headroom(rid, EXP, L) == 1.25
    spend.record("judge", "luna", 1, 1.5, exp=EXP, ledger=L, reservation_id=rid)
    assert spend.reservation_headroom(rid, EXP, L) == 0.0  # overspent: never negative
    with pytest.raises(spend.ReservationError, match="belongs to"):
        spend.reservation_headroom(rid, "I9", L)
    with pytest.raises(spend.ReservationError, match="unknown"):
        spend.reservation_headroom("0" * 32, EXP, L)
    spend.settle_api(rid, None, ledger=L)
    with pytest.raises(spend.ReservationError, match="closed"):
        spend.reservation_headroom(rid, EXP, L)


def test_record_under_reservation_is_validated(tmp_path):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 2.0, "col", ledger=L)
    with pytest.raises(spend.ReservationError, match="kind 'judge'"):
        spend.record("gpu", "pod", 1, 0.1, exp=EXP, ledger=L, reservation_id=rid)
    with pytest.raises(spend.ReservationError, match="belongs to"):
        spend.record("judge", "luna", 1, 0.1, exp="I9", ledger=L, reservation_id=rid)
    with pytest.raises(spend.ReservationError, match="unknown"):
        spend.record("judge", "luna", 1, 0.1, exp=EXP, ledger=L, reservation_id="x")
    spend.release_api(rid, ledger=L)
    with pytest.raises(spend.ReservationError, match="closed"):
        spend.record("judge", "luna", 1, 0.1, exp=EXP, ledger=L, reservation_id=rid)
    assert spend.spent(None, EXP, L) == 0.0  # nothing was appended by the refusals
    e = spend.record("judge", "luna", 1, 0.1, exp=EXP, ledger=L)
    assert "reservation_id" not in e  # plain records keep their exact shape


def test_check_total_excludes_only_its_own_reservation(tmp_path):
    L = tmp_path / "c.jsonl"
    a = spend.reserve_api(EXP, 3.0, "a", ledger=L)
    spend.reserve_api(EXP, 4.0, "b", ledger=L)
    spend.record("judge", "luna", 1, 1.0, exp=EXP, ledger=L, reservation_id=a)
    plain = spend.check_total(2.0, "judge", exp=EXP, ledger=L)
    assert plain["judge_reserved_usd"] == 6.0 and plain["judge_after_usd"] == 9.0
    own = spend.check_total(2.0, "judge", exp=EXP, ledger=L, reservation_id=a)
    assert own["judge_reserved_usd"] == 4.0 and own["judge_after_usd"] == 7.0
    assert own["reservation_id"] == a
    with pytest.raises(spend.BudgetExceeded):
        spend.check_total(4.5, "judge", exp=EXP, ledger=L)  # 1 + 6 + 4.5 > 10
    spend.check_total(5.0, "judge", exp=EXP, ledger=L, reservation_id=a)  # 1 + 4 + 5 = 10
    with pytest.raises(spend.ReservationError, match="belongs to"):
        spend.check_total(0.0, "judge", exp="I9", ledger=L, reservation_id=a)
    with pytest.raises(spend.ReservationError, match="judge"):
        spend.check_total(0.0, "gpu", exp=EXP, ledger=L, reservation_id=a)
    spend.settle_api(a, 1.0, ledger=L)
    with pytest.raises(spend.ReservationError, match="closed"):
        spend.check_total(0.0, "judge", exp=EXP, ledger=L, reservation_id=a)


def test_gpu_guard_and_kill_line_count_open_reservations(tmp_path, tst):
    L = tmp_path / "c.jsonl"
    spend.record("judge", "luna", 1, 2.0, exp=tst, ledger=L)
    assert spend.watchdog_kill_usd(tst, L) == 15.0  # min(15, 20 - 2)
    rid = spend.reserve_api(tst, 6.0, "col", ledger=L)
    assert spend.watchdog_kill_usd(tst, L) == 12.0  # 20 - (2 + 6)
    assert spend.totals(tst, L)["watchdog_kill_usd"] == 12.0
    spend.check_total(12.0, "gpu", exp=tst, ledger=L)
    with pytest.raises(spend.BudgetExceeded, match="hard cap"):
        spend.check_total(12.5, "gpu", exp=tst, ledger=L)
    spend.record("judge", "luna", 1, 2.5, exp=tst, ledger=L, reservation_id=rid)
    assert spend.watchdog_kill_usd(tst, L) == 12.0  # a charge under it changes nothing
    spend.settle_api(rid, 3.0, ledger=L)
    assert spend.watchdog_kill_usd(tst, L) == 15.0  # 20 - 5
    # E3's real caps: the kill line is min($35, $45 - API committed)
    assert spend.watchdog_kill_usd(EXP, L) == 35.0


def test_no_reservation_rows_same_numbers(tmp_path, tst):
    L = tmp_path / "c.jsonl"
    spend.record("gpu", "pod", 1, 4.0, exp=tst, ledger=L)
    spend.record("judge", "luna", 1, 3.0, exp=tst, ledger=L)
    t = spend.totals(tst, L)
    assert t["total_usd"] == 7.0 and t["judge_reserved_usd"] == 0.0 and t["open_reservations"] == 0
    assert t["watchdog_kill_usd"] == 15.0 == spend.watchdog_kill_usd(tst, L)
    b = spend.check_total(1.0, "judge", running_pod_usd=1.0, exp=tst, ledger=L)
    assert (b["gpu_after_usd"], b["judge_after_usd"], b["total_after_usd"]) == (5.0, 4.0, 9.0)


# --- order independence and merging -----------------------------------------------------------


def _mixed_ledger(L):
    a = spend.reserve_api(EXP, 2.0, "a", ledger=L)
    b = spend.reserve_api(EXP, 1.5, "b", ledger=L)
    c = spend.reserve_api(EXP, 1.0, "c", ledger=L)
    d = spend.reserve_api("I9", 0.5, "d", ledger=L)
    spend.mark_submitted(a, EXP, L)
    spend.record("judge", "luna", 1, 0.4, exp=EXP, ledger=L, reservation_id=a)
    spend.mark_submitted(b, EXP, L)
    spend.record("judge", "luna", 1, 0.3, exp=EXP, ledger=L, reservation_id=b)
    spend.settle_api(b, 1.2, ledger=L)
    spend.release_api(c, ledger=L)
    spend.record("gpu", "pod", 1, 3.0, exp=EXP, ledger=L)
    spend.record("judge", "luna", 1, 0.25, exp=EXP, ledger=L)
    return a, b, c, d


def test_state_is_order_independent(tmp_path):
    L = tmp_path / "c.jsonl"
    a, b, c, d = _mixed_ledger(L)
    entries = spend.read_entries(L)
    ref = spend.reservation_states(entries)
    assert not ref[a]["closed"] and ref[a]["liability"] == 1.6 and ref[b]["closed"]
    assert ref[c]["closed"] and ref[c]["charges"] == 0 and ref[d]["exp"] == "I9"
    ref_totals = spend.totals(EXP, L)
    rng = random.Random(0)
    for i in range(20):
        shuffled = entries[:]
        rng.shuffle(shuffled)
        assert spend.reservation_states(shuffled) == ref
        S = tmp_path / f"s{i}.jsonl"
        S.write_text("".join(json.dumps(e) + "\n" for e in shuffled))
        t = spend.totals(EXP, S)
        assert {k: v for k, v in t.items() if k != "ledger"} == {
            k: v for k, v in ref_totals.items() if k != "ledger"
        }


def test_inconsistent_reservation_rows_are_loud(tmp_path):
    L = tmp_path / "c.jsonl"
    rid = spend.reserve_api(EXP, 2.0, "a", ledger=L)
    bad = {**spend.read_entries(L)[0], "id": "9" * 32, "reserve_usd": 5.0}
    with pytest.raises(RuntimeError, match="disagree"):
        spend.reservation_states(spend.read_entries(L) + [bad])
    alien = {**spend.read_entries(L)[0], "id": "8" * 32, "op": "submitted", "exp": "I9"}
    with pytest.raises(RuntimeError, match="several experiments"):
        spend.reservation_states(spend.read_entries(L) + [alien])
    gpu = {**spend.read_entries(L)[0], "id": "7" * 32, "op": None, "kind": "gpu", "usd": 1.0}
    with pytest.raises(RuntimeError, match="kind"):
        spend.reservation_states(spend.read_entries(L) + [gpu])
    assert rid in spend.reservation_states(spend.read_entries(L))


def _load_merge_ledger():
    spec = importlib.util.spec_from_file_location(
        "merge_ledger", ROOT / "scripts" / "merge_ledger.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["merge_ledger"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_merge_ledger_accepts_reservation_rows(tmp_path, capsys):
    ml = _load_merge_ledger()
    main_l, branch = tmp_path / "main.jsonl", tmp_path / "branch.jsonl"
    spend.record("gpu", "pod", 1, 1.0, exp="E1", ledger=main_l)
    branch.write_text(main_l.read_text())  # the branch point
    _mixed_ledger(branch)
    assert ml.main(["check", str(branch)]) == 0
    assert ml.main(["merge", "--from", str(branch), "--into", str(main_l), "--write"]) == 0
    assert ml.main(["check", str(main_l)]) == 0
    want = spend.reservation_states(spend.read_entries(branch))
    assert spend.reservation_states(spend.read_entries(main_l)) == want
    assert spend.totals(EXP, main_l)["total_usd"] == spend.totals(EXP, branch)["total_usd"]
    n = len(spend.read_entries(main_l))
    assert ml.main(["merge", "--from", str(branch), "--into", str(main_l), "--write"]) == 0
    assert len(spend.read_entries(main_l)) == n  # idempotent
    # a union text merge that repeats rows is undone by check --fix, restoring the state
    main_l.write_text(main_l.read_text() + branch.read_text())
    assert ml.main(["check", str(main_l)]) == 1
    assert ml.main(["check", str(main_l), "--fix"]) == 0
    assert spend.reservation_states(spend.read_entries(main_l)) == want
    capsys.readouterr()


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_lists_open_reservations(tmp_path):
    L = tmp_path / "c.jsonl"
    a = spend.reserve_api(EXP, 1.0, "a", ledger=L)
    b = spend.reserve_api(EXP, 1.0, "b", ledger=L)
    i9 = spend.reserve_api("I9", 1.0, "c", ledger=L)
    spend.release_api(b, ledger=L)
    run = [sys.executable, "-m", "emx.spend", "--reservations", "--ledger", str(L)]
    r = subprocess.run([*run, "--exp", EXP], capture_output=True, text=True, check=True)
    assert set(json.loads(r.stdout)) == {a}
    r = subprocess.run(run, capture_output=True, text=True, check=True)
    assert set(json.loads(r.stdout)) == {a, i9}
    r = subprocess.run(
        [sys.executable, "-m", "emx.spend", "--report", "--exp", EXP, "--ledger", str(L)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(r.stdout)["judge_reserved_usd"] == 1.0
