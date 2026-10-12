"""p05.session bookkeeping without models: unit order, the floor rule, the time guard."""

from __future__ import annotations

import argparse
import json

import pytest

pytest.importorskip("torch")

from p05 import session as S


def _session(tmp_path, **kw):
    a = argparse.Namespace(cmd="all", out=str(tmp_path), device="cpu", limit=0,
                           banks=["chain", "arithmetic"], models=["rec32", "base", "rec4"],
                           token_budget=1000, greedy_batch=2, max_hours=1.0)
    for k, v in kw.items():
        setattr(a, k, v)
    return S.Session(a)


def test_plan_units_matched_pair_first(tmp_path):
    u = _session(tmp_path).plan_units()
    assert u[0] == ("tf", "rec32", 0, "chain", "nocot", True)
    assert u[1] == ("tf", "base", 0, "chain", "nocot", True)
    first_rec4 = next(i for i, x in enumerate(u) if x[1:2] == ("rec4",))
    first_seed1 = next(i for i, x in enumerate(u) if x[0] == "tf" and x[2] == 1)
    last_pair = max(i for i, x in enumerate(u) if x[0] == "tf" and x[1] in ("rec32", "base")
                    and x[2] == 0)
    assert last_pair < first_seed1 < first_rec4
    assert u.index(("floor",)) < first_seed1


def _write_tf(tmp_path, acc_k1):
    for key in ("rec32", "base"):
        for fmt in ("nocot", "brauer"):
            p = tmp_path / "tf" / f"{key}_s0_chain_{fmt}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            rows = [{"k": 1, "r": 32 if key == "rec32" else 0, "correct": i < acc_k1 * 10}
                    for i in range(10)]
            p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_floor_rule_fires_only_when_everything_is_below_half(tmp_path):
    s = _session(tmp_path)
    _write_tf(tmp_path, 0.3)
    extra = s.floor_rule()
    assert extra and all(x[3] == "chain_easy" for x in extra)
    _write_tf(tmp_path, 0.6)
    assert s.floor_rule() == []
    rows = [json.loads(x) for x in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert [r["add_chain_easy"] for r in rows] == [True, False]


def test_time_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("P05_T0", "0")
    s = _session(tmp_path)
    with pytest.raises(S.OutOfTime):
        s.check_time("x")
