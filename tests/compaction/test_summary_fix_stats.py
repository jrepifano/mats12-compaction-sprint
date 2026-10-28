"""Tests for the week-2 experiment-5 primary analysis (summary_fix_stats)."""

from __future__ import annotations

import itertools
import json

import numpy as np
import pytest

from compaction.s2.chain import summary_fix_stats as S


def p_two(d):  # the plan appendix's reference implementation, verbatim in substance
    nz = np.asarray(d, float)
    nz = nz[nz != 0]
    if len(nz) == 0:
        return 1.0
    signs = np.array(list(itertools.product([1, -1], repeat=len(nz))))
    return np.mean(np.abs(signs @ nz) >= abs(nz.sum()) - 1e-9)


def run(h, rep, arm, lost):
    return {"history": h, "replicate": rep, "arm": arm, "teammate_lost": lost}


def dataset(native, fixes, hists=10, reps=3):
    """native/fixes: {arm: lost value for every unit}; lost may be a callable(h_index, rep)."""
    rows = []
    for i in range(hists):
        for rep in range(1, reps + 1):
            for arm, v in {"native": native, **fixes}.items():
                rows.append(run(f"h{i:02d}", rep, arm, v(i, rep) if callable(v) else v))
    return rows


@pytest.mark.parametrize("d, want", [
    ([], 1.0), ([0, 0], 1.0), ([1, 1, 1], 0.25), ([2, -1], 1.0), ([3, -1], 1.0),
    ([3, 1], 0.5), ([1] * 5, 0.0625), ([2, 1, 1, 0], 0.25), ([1] * 10, 2 / 1024),
])
def test_sign_flip_known_vectors(d, want):
    assert S.sign_flip_p(d) == pytest.approx(want)
    assert S.sign_flip_p(d) == pytest.approx(p_two(d))


def test_sign_flip_matches_appendix_on_random_vectors_and_dicts():
    rng = np.random.default_rng(0)
    for _ in range(50):
        d = rng.integers(-3, 4, size=rng.integers(1, 11)).tolist()
        assert S.sign_flip_p(d) == pytest.approx(p_two(d))
    assert S.sign_flip_p({"a": 1, "b": 1, "c": 1}) == pytest.approx(0.25)
    with pytest.raises(ValueError):
        S.sign_flip_p([1] * (S.MAX_K + 1))


def test_holm_textbook():
    h = S.holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.005})
    assert [h[k]["reject"] for k in "abcd"] == [True, False, False, True]
    assert [h[k]["p_adj"] for k in "abcd"] == pytest.approx([0.03, 0.06, 0.06, 0.02])
    # stops at the first failure even if a later p would pass its own threshold
    h = S.holm({"x": 0.03, "y": 0.04})
    assert not h["x"]["reject"] and not h["y"]["reject"]
    assert S.holm({"x": 0.02, "y": 0.04})["y"]["reject"]


def test_paired_diffs_units_and_unknown_completions():
    rows = [
        run("A", 1, "native", True), run("A", 1, "ledger", False),   # +1
        run("A", 2, "native", True), run("A", 2, "ledger", None),    # unknown fix
        run("B", 1, "native", None), run("B", 1, "ledger", False),   # unknown base
        run("C", 1, "native", True),                                  # no ledger run: not a unit
        run("D", 1, "native", False), run("D", 1, "ledger", True),   # -1
        run("D", 1, "other", True),                                   # ignored arm
    ]
    assert S.paired_diffs(rows, "ledger") == {"A": 1, "D": -1}
    worst = S.paired_diffs(rows, "ledger", fill="worst")  # fix lost, base kept
    best = S.paired_diffs(rows, "ledger", fill="best")    # fix kept, base lost
    assert worst == {"A": 1, "B": 0, "D": -1}
    assert best == {"A": 2, "B": 1, "D": -1}
    assert all(worst[h] <= best[h] for h in worst)
    with pytest.raises(ValueError):
        S.paired_diffs(rows, "ledger", fill="middle")
    with pytest.raises(ValueError):
        S.paired_diffs(rows + [run("A", 1, "ledger", True)], "ledger")


def test_bootstrap_is_deterministic_and_brackets_the_estimate():
    rows = dataset(True, {"ledger": lambda i, rep: rep <= i % 4}, hists=13)  # 0-3 of 3 lost
    a = S.cluster_bootstrap_ci(rows, "ledger", n=2000)
    b = S.cluster_bootstrap_ci(rows, "ledger", n=2000)
    c = S.cluster_bootstrap_ci(rows, "ledger", n=2000, seed=1, level=0.8)
    assert a == b and a["seed"] == S.SEED and (a["n_hist"], a["n_units"]) == (13, 39)
    assert a["lo"] < a["est_pts"] < a["hi"] < 0
    assert a["est_pts"] == pytest.approx(100 * (18 / 39 - 1))
    assert (c["lo"], c["hi"]) != (a["lo"], a["hi"])
    assert S.cluster_bootstrap_ci([], "ledger") is None


def test_primary_ledger_clearly_helps():
    rows = dataset(True, {
        "ledger": lambda i, rep: i == 0 and rep == 1,          # 1/30 lost
        "line": lambda i, rep: rep != 1,                        # 20/30 lost
        "placebo": True,
    })
    rows[-1]["teammate_lost"] = None  # one quota-cut placebo slot
    out = S.primary(rows, n=2000)
    json.dumps(out)
    led = out["fixes"]["ledger"]
    assert out["risk"]["native"]["risk"] == 1.0
    assert out["risk"]["ledger"]["lost"] == 1
    assert out["risk"]["placebo"] == {"assigned": 30, "known": 29, "lost": 29,
                                      "risk": pytest.approx(1.0)}
    assert led["complete"]["diff_pts"] == pytest.approx(100 * (1 / 30 - 1))
    assert led["worst"]["p"] == pytest.approx(2 / 1024)
    assert led["holm"]["reject"] and led["ci_excludes_zero"] == -1
    assert led["significant"] and led["reduces_loss"]
    assert out["ledger_vs_placebo"]["content_works"]
    assert out["placebo_vs_native"]["worst"]["diff_pts"] <= 0


def test_primary_no_effect():
    rng = np.random.default_rng(7)
    lost = {(i, rep, arm): bool(rng.random() < 0.7)
            for i in range(10) for rep in (1, 2, 3) for arm in S.ARMS}
    rows = [run(f"h{i}", rep, arm, lost[i, rep, arm]) for (i, rep, arm) in lost]
    out = S.primary(rows, n=2000)
    json.dumps(out)
    for f in S.FIXES:
        assert not out["fixes"][f]["significant"]
    assert not out["ledger_vs_placebo"]["content_works"]


def test_unknowns_can_block_significance():
    # clear effect on known units, but many unknown fix slots: best rejects, worst doesn't
    rows = dataset(True, {"ledger": lambda i, rep: None if i < 6 else False,
                          "line": True, "placebo": True})
    out = S.primary(rows, n=2000)
    led = out["fixes"]["ledger"]
    assert led["best"]["p"] < 0.01
    assert led["worst"]["p"] == pytest.approx(2 / 16)  # 4 histories at D = 3, 6 at 0
    assert not led["significant"]
