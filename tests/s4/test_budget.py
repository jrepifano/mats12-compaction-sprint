"""Tests for the S4 budget reservation: the cap holds under parallel processes."""
import json
import multiprocessing as mp

import pytest

from s4world import budget


@pytest.fixture(autouse=True)
def tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("S4_LEDGER", str(tmp_path / "costs.jsonl"))
    monkeypatch.setenv("S4_RESERVATIONS", str(tmp_path / "res.json"))
    # an unrelated experiment's spend must not count
    (tmp_path / "costs.jsonl").write_text(json.dumps({"exp": "E1", "usd": 50.0}) + "\n")
    return tmp_path


def test_reserve_settle_release():
    rid = budget.reserve(2.0, "ep1", cap=5.0)
    assert budget.status(cap=5.0)["outstanding"] == 2.0
    entry = budget.settle(rid, 1.5, "deepseek", 1, "ep1")
    assert entry["usd"] == 1.5 and "OVER" not in entry["note"]
    st = budget.status(cap=5.0)
    assert st["spent"] == 1.5 and st["outstanding"] == 0.0 and st["remaining"] == 3.5
    rid2 = budget.reserve(1.0, "ep2", cap=5.0)
    budget.release(rid2)
    assert budget.status(cap=5.0)["outstanding"] == 0.0


def test_refuses_over_cap():
    budget.reserve(4.0, "a", cap=5.0)
    with pytest.raises(budget.BudgetRefused):
        budget.reserve(1.5, "b", cap=5.0)


def test_overspend_is_recorded_not_hidden():
    rid = budget.reserve(1.0, "x", cap=5.0)
    entry = budget.settle(rid, 1.2, "glm", 1, "x")
    assert entry["usd"] == 1.2 and "OVER RESERVATION" in entry["note"]


def _worker(args):
    ledger, resv, n = args
    import os

    os.environ["S4_LEDGER"] = ledger
    os.environ["S4_RESERVATIONS"] = resv
    from s4world import budget as b

    ok = 0
    for _ in range(n):
        try:
            b.reserve(0.3, "par", cap=3.0)
            ok += 1
        except b.BudgetRefused:
            pass
    return ok


def test_parallel_processes_never_exceed_cap(tmp_ledger):
    args = (str(tmp_ledger / "costs.jsonl"), str(tmp_ledger / "res.json"), 10)
    with mp.get_context("spawn").Pool(6) as pool:
        granted = sum(pool.map(_worker, [args] * 6))
    assert granted == 10  # 10 x 0.3 = 3.0 exactly fits; 60 attempts, the rest refused
    assert budget.status(cap=3.0)["outstanding"] <= 3.0 + 1e-9
