"""Tests for p05.analyze on synthetic data with known parameters (plan, "Measure")."""

import json
import math
import time

import numpy as np
import pytest
from p05 import analyze as A
from scipy.optimize import minimize
from scipy.special import expit

KS = [1, 2, 3, 4, 5, 6, 8]
C = 0.1


def curve(k, m, s, c=C):
    return c + (1 - c) * expit(s * (m - np.asarray(k, float)))


def sample(m, s, n_per_k, rng, c=C):
    k = np.repeat(KS, n_per_k)
    return k, rng.random(len(k)) < curve(k, m, s, c)


def score_rows(cfg_curves, n_per_k, seed=0, bank="chain", fmt="nocot"):
    """Rows for configs {(model, seed, r): (m, s)}, the same item ids in each."""
    rng = np.random.default_rng(seed)
    rows = []
    for cf, (m, s) in cfg_curves.items():
        for k in KS:
            ok = rng.random(n_per_k) < curve(k, m, s)
            for i in range(n_per_k):
                rows.append(
                    {
                        "model": cf[0],
                        "seed": cf[1],
                        "r": cf[2],
                        "bank": bank,
                        "fmt": fmt,
                        "item_id": f"k{k}-{i}",
                        "k": k,
                        "correct": bool(ok[i]),
                    }
                )
    return rows


# ---------------------------------------------------------------- fit_curve


def test_fit_recovers_parameters_and_d50_is_the_50pct_crossing():
    k, y = sample(4.0, 1.5, 4000, np.random.default_rng(0))
    fit = A.fit_curve(k, y, C)
    assert fit["status"] == "ok"
    assert abs(fit["m"] - 4.0) < 0.1 and abs(fit["s"] - 1.5) < 0.15
    assert curve(fit["d50"], fit["m"], fit["s"]) == pytest.approx(0.5, abs=1e-9)
    n, acc, lo, hi = fit["per_k"][3]
    assert n == 4000 and lo < acc < hi


def test_fit_matches_scipy_multistart():
    rng = np.random.default_rng(3)
    for _ in range(20):
        k, y = sample(rng.uniform(0, 8), rng.uniform(-1, 3), 60, rng)
        fit = A.fit_curve(k, y, C)
        if not math.isfinite(fit["m"]):
            continue
        ks = np.array(KS, float)
        succ = np.array([[y[k == kk].sum() for kk in KS]], float)

        def nll(th, ks=ks, succ=succ):
            return -A._loglik(np.array([th[0]]), np.array([th[1]]), ks, succ, 60, C)[0]

        best = min(
            (
                minimize(nll, x0, method="L-BFGS-B", bounds=[(-20, 30), (-50, 50)])
                for x0 in [(0, 1), (4, 1), (8, 0.5), (4, -1), (2, 5), (-5, 1)]
            ),
            key=lambda r: r.fun,
        )
        assert -nll((fit["m"], fit["s"])) >= -best.fun - 1e-5


def test_status_cases():
    k = np.repeat(KS, 50)
    assert A.fit_curve(k, np.zeros(len(k)), C)["status"] == "below"
    assert A.fit_curve(k, np.ones(len(k)), C)["status"] == "above"
    k2, y2 = sample(4.0, -1.0, 500, np.random.default_rng(1))
    fit = A.fit_curve(k2, y2, C)
    assert fit["status"] == "nonmonotone" and math.isnan(fit["d50"])
    assert math.isnan(A.d50_clipped(fit, 1, 8))
    k3, y3 = sample(-3.0, 2.0, 500, np.random.default_rng(2))
    assert A.fit_curve(k3, y3, C)["status"] == "below"
    k4, y4 = sample(15.0, 1.0, 500, np.random.default_rng(2))
    assert A.fit_curve(k4, y4, C)["status"] == "above"
    assert A.d50_clipped(A.fit_curve(k, np.ones(len(k)), C), 1, 8) == 9.0
    assert A.d50_clipped(A.fit_curve(k, np.zeros(len(k)), C), 1, 8) == 0.0
    with pytest.raises(ValueError):
        A.fit_curve(k, np.ones(len(k)), 0.5)


# ---------------------------------------------------------------- paired bootstrap


def test_identical_configs_give_zero_delta_and_copy_is_joint():
    rows = score_rows({("rec32", 0, 32): (4.0, 1.2)}, 100)
    copy = [{**r, "model": "base", "r": 0} for r in rows]
    cfs = [("rec32", 0, 32), ("base", 0, 0)]
    bs = A.paired_bootstrap(rows + copy, cfs, "chain", "nocot", C, n_boot=200)
    np.testing.assert_array_equal(bs["boot"][:, 0], bs["boot"][:, 1])
    d = A.delta_ci(bs, *cfs)
    assert d["delta"] == 0 and d["lo"] == 0 and d["hi"] == 0
    assert A.classify_outcome(d, floor=False) == 4
    assert len(bs["item_ids_per_k"][3]) == 100


def test_shifted_curve_gives_ci_in_right_direction():
    cfs = {("rec32", 0, 32): (5.0, 1.2), ("base", 0, 0): (3.5, 1.2)}
    rows = score_rows(cfs, 200, seed=1)
    bs = A.paired_bootstrap(rows, list(cfs), "chain", "nocot", C, n_boot=300)
    d = A.delta_ci(bs, ("rec32", 0, 32), ("base", 0, 0))
    assert d["lo"] > 0 and abs(d["delta"] - 1.5) < 0.4
    assert A.classify_outcome(d, floor=False) == 2
    d_rev = A.delta_ci(bs, ("base", 0, 0), ("rec32", 0, 32))
    assert d_rev["hi"] < 0 and A.classify_outcome(d_rev, floor=False) == 3


def test_missing_rows_raise_and_name_them():
    rows = score_rows({("rec32", 0, 32): (4, 1), ("base", 0, 0): (4, 1)}, 20)
    rows = [r for r in rows if not (r["model"] == "base" and r["item_id"] == "k3-7")]
    with pytest.raises(ValueError, match="k3-7"):
        A.paired_bootstrap(rows, [("rec32", 0, 32), ("base", 0, 0)], "chain", "nocot", C, 10)


def test_bootstrap_runtime():
    cfs = {("rec32", 0, 32): (5, 1.2), ("rec32", 0, 1): (3, 1.0), ("base", 0, 0): (3.5, 1.1)}
    rows = score_rows(cfs, 286)  # 2,002 items
    t0 = time.time()
    A.paired_bootstrap(rows, list(cfs), "chain", "nocot", C, n_boot=200)
    assert time.time() - t0 < 5


# ---------------------------------------------------------------- outcomes


def test_classify_outcome_order():
    def d(lo, hi):
        return {"lo": lo, "hi": hi}

    assert A.classify_outcome(d(0.1, 0.3), floor=True) == 1
    assert A.classify_outcome(d(0.1, 0.3), floor=False) == 2  # above 0 beats within margin
    assert A.classify_outcome(d(-0.3, -0.1), floor=False) == 3
    assert A.classify_outcome(d(-0.4, 0.4), floor=False) == 4
    assert A.classify_outcome(d(-0.4, 0.9), floor=False) == 5
    assert A.classify_outcome(d(math.nan, math.nan), floor=False) == 5


# ---------------------------------------------------------------- readability


def synthetic_ro(n=120, n_t=5, signal=True, seed=0):
    rng = np.random.default_rng(seed)
    ks = rng.choice([2, 3, 4], size=n)
    traj = [list(rng.choice(np.arange(1, 21), size=k + 1, replace=False)) for k in ks]
    cand = rng.normal(0, 0.1, size=(n, n_t, 2, 20))
    if signal:
        for i, k in enumerate(ks):
            for t in range(n_t):
                v = traj[i][t + 1] if t + 1 < k else traj[i][k]  # step j peaks at loop j - 1
                cand[i, t, :, v - 1] = 5.0
    return {
        "item_ids": [f"i{i}" for i in range(n)],
        "k": ks,
        "traj": traj,
        "cand": cand,
        "lenses": ["coda", "logit"],
        "loops": [1, 2, 4, 8, 16],
    }


def test_readability_signal_and_order():
    ro = synthetic_ro()
    out = A.readability(ro, n_boot=100)
    assert out["n_items"] == 120 and all(a != b for a, b in out["partner"].items())
    for lens in ("coda", "logit"):
        ar = out["any_rate"][lens]
        assert ar["hit"] == 1.0 and ar["diff"] == pytest.approx(1 - ar["ctrl"])
        assert ar["ci"][0] > 0.5
        assert out["order"][lens]["rho"] == pytest.approx(1.0)
        assert out["order"][lens]["never_share"] == 0
    hit = out["per_jtl"]["hit"]
    assert hit[0, 0, 0] == 1.0 and hit[1, 1, 0] == 1.0


def test_readability_null_and_filters():
    out = A.readability(synthetic_ro(signal=False), n_boot=100)
    for lens in ("coda", "logit"):
        assert abs(out["any_rate"][lens]["diff"]) < 0.15
        lo, hi = out["any_rate"][lens]["ci"]
        assert lo < 0.1 and hi > -0.1
    ro = synthetic_ro(n=40)
    ro["traj"][0] = [1, 5, 5, 5, 5]  # repeated intermediates: excluded
    out = A.readability(ro, correct_ids={f"i{i}" for i in range(20)}, n_boot=10)
    assert "i0" not in out["partner"] and out["n_items"] + out["n_dropped_singleton"] <= 19


def test_derangement_has_no_fixed_points():
    rng = np.random.default_rng(0)
    for n in range(2, 40):
        p = A._derangement(n, rng)
        assert sorted(p) == list(range(n)) and (p != np.arange(n)).all()


# ---------------------------------------------------------------- ask baseline, power, summary


def test_ask_baseline():
    rows = [
        {
            "model": "rec32",
            "seed": 0,
            "fmt": "nocot",
            "base_id": f"b{i}",
            "j": j,
            "r": 32,
            "top1": (i % 20) + 1,
            "answer": (i % 20) + 1,
        }
        for i in range(40)
        for j in (1, 2)
    ]
    out = A.ask_baseline(rows)
    assert len(out["per_group"]) == 2 and all(g["acc"] == 1.0 for g in out["per_group"])
    (ov,) = out["overall"]
    assert ov["n"] == 80 and ov["diff"] == pytest.approx(1 - ov["ctrl"]) and ov["ctrl"] < 0.2


def test_power_sim_shrinks_with_n():
    ks = np.repeat(KS, 1000)
    fa = A.fit_curve(ks, np.random.default_rng(0).random(len(ks)) < curve(ks, 5, 1.2), C)
    fb = A.fit_curve(ks, np.random.default_rng(1).random(len(ks)) < curve(ks, 4, 1.2), C)
    small = A.power_sim(fa, fb, C, KS, 50, n_sim=150)
    big = A.power_sim(fa, fb, C, KS, 500, n_sim=150)
    assert big["sd"] < small["sd"] and big["half_width"] == pytest.approx(1.96 * big["sd"], 1e-3)


def test_summarize_and_cli(tmp_path):
    cfs = {
        ("rec32", 0, 32): (5, 1.2),
        ("rec32", 0, 1): (3, 1.0),
        ("base", 0, 0): (3.5, 1.1),
        ("rec32", 1, 32): (5, 1.2),
        ("rec4", 0, 4): (4.5, 1.0),
    }
    rows = score_rows(cfs, 40)
    (tmp_path / "tf").mkdir()
    (tmp_path / "tf" / "a.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (tmp_path / "chance.json").write_text(json.dumps({"chain": C}))
    out = tmp_path / "s.json"
    assert A.main([str(tmp_path), "--out", str(out), "--n-boot", "50"]) == 0
    s = json.loads(out.read_text())["chain/nocot"]
    con = s["contrasts"]
    assert set(con["delta_r32_vs_base"]) == {"seed0", "seed1"}
    assert con["delta_r32_vs_base"]["seed1"]["b"] == ["base", 0, 0]
    assert "seed0" in con["loop_gain_32_vs_1"] and "seed0" in con["rec4_r4_vs_rec32_r32"]
    assert [e["r"] for e in s["curves"]["rec32/s0"]] == [1, 32]
    assert s["configs"]["base/s0/r0"]["eff_layers"] == 16
