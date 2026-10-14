"""Tests for p47.analysis on synthetic data (no paid calls, no I/O)."""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest
from p47 import analysis as A
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------
def synth_c2(n_tasks=30, spt=2, draws=6, signal=0.0, n_noise=2, ask_n=8, seed=0,
             ask_signal=1.0):
    """Draw-level rows: cheat propensity = task + a + signal * b. Asking answers are 0/1 draws
    with probability logistic(ask_signal * a) (so asking carries a); the probe is b plus noise."""
    rng = np.random.default_rng(seed)
    rows = []
    for t in range(n_tasks):
        te = rng.normal(0, 0.5)
        for s in range(spt):
            a, b = rng.normal(), rng.normal()
            p = 1 / (1 + np.exp(-(-0.3 + te + a + signal * b)))
            ask_t = (rng.random(ask_n) < 1 / (1 + np.exp(-ask_signal * a))).astype(int)
            ask_u = (rng.random(ask_n) < 0.3).astype(int)
            feats = {f"x{j}": rng.normal() for j in range(n_noise)}
            probe = b + rng.normal(0, 0.3)
            for _ in range(draws):
                rows.append(dict(task_id=f"t{t:02d}", site_id=f"t{t:02d}s{s}",
                                 cheat=int(rng.random() < p), ask_tool=list(ask_t),
                                 ask_user=list(ask_u), probe=probe, **feats))
    return pd.DataFrame(rows)


ASK = {"share_tool": "ask_tool", "share_user": "ask_user"}


def base_cols(n_noise=2):
    return ["share_tool", "share_user"] + [f"x{j}" for j in range(n_noise)]


@dataclass(frozen=True)
class St:
    direction: str
    pole: int
    pair_id: str
    template_id: str
    heldout: bool


# --------------------------------------------------------------------------------------------
# AUROC and the solver
# --------------------------------------------------------------------------------------------
def test_weighted_auroc_matches_sklearn():
    rng = np.random.default_rng(0)
    s = np.round(rng.normal(size=200), 1)          # ties
    y = rng.integers(0, 2, 200)
    w = rng.uniform(0.1, 3, 200)
    got = A.weighted_auroc(s, w * y, w * (1 - y))
    assert got == pytest.approx(roc_auc_score(y, s, sample_weight=w), abs=1e-12)
    assert A.auroc(s, y) == pytest.approx(roc_auc_score(y, s), abs=1e-12)
    assert np.isnan(A.auroc(s, np.ones(200)))


def test_fit_logistic_batch_matches_sklearn_standardised():
    rng = np.random.default_rng(1)
    n = 300
    X = rng.normal(size=(n, 3)) * [1, 10, 100] + [0, 5, 1000]
    y = (rng.random(n) < 1 / (1 + np.exp(-(X[:, 0] + 0.1 * X[:, 1] - 0.5)))).astype(float)
    w = rng.uniform(0.2, 2, n)
    for lam in (1.0, 0.01):
        mu = (w @ X) / w.sum()
        sd = np.sqrt((w @ (X - mu) ** 2) / w.sum())
        Z = (X - mu) / sd
        ref = LogisticRegression(C=1 / (lam * w.sum()), tol=1e-12, max_iter=10_000)
        ref.fit(Z, y, sample_weight=w)
        X1 = np.c_[np.ones(n), X]
        pen = np.c_[[1e-6 / lam], (sd ** 2)[None, :]]
        b = A.fit_logistic_batch(X1, y, w[None, :], np.array([lam]), pen)[0]
        np.testing.assert_allclose(X1 @ b, ref.decision_function(Z), atol=1e-4)


def _reference_nested(X, y, w, fold, grid):
    """Plain loops with sklearn: outer leave-one-fold-out, inner leave-one-fold-out over grid."""
    folds = sorted(set(fold))

    def fit_pred(train, test, lam):
        Xt, wt = X[train], w[train]
        mu = (wt @ Xt) / wt.sum()
        sd = np.sqrt((wt @ (Xt - mu) ** 2) / wt.sum())
        sd = np.where(sd > 1e-12, sd, 1.0)
        # collapsed rows: fractional y -> two weighted rows
        Zt = (Xt - mu) / sd
        Z2 = np.r_[Zt, Zt]
        y2 = np.r_[np.ones(len(Zt)), np.zeros(len(Zt))]
        w2 = np.r_[wt * y[train], wt * (1 - y[train])]
        clf = LogisticRegression(C=1 / (lam * wt.sum()), tol=1e-12, max_iter=10_000)
        clf.fit(Z2, y2, sample_weight=w2)
        eta = clf.decision_function((X[test] - mu) / sd)
        return (w[test] * (np.logaddexp(0, eta) - y[test] * eta)).sum()

    total = 0.0
    for k in folds:
        inner = [f for f in folds if f != k]
        cv = []
        for lam in grid:
            cv.append(sum(fit_pred((fold != k) & (fold != j), fold == j, lam) for j in inner))
        lam = grid[int(np.argmin(cv))]
        total += fit_pred(fold != k, fold == k, lam)
    return total / w.sum()


def test_nested_cv_matches_reference_loops():
    rng = np.random.default_rng(2)
    n_tasks, spt = 6, 3
    fold = np.repeat(np.arange(n_tasks), spt)
    X = rng.normal(size=(len(fold), 2))
    y = np.clip(1 / (1 + np.exp(-(X[:, 0] + rng.normal(0, 1, len(fold))))), 0, 1)
    y = np.round(y * 6) / 6                                     # cheat shares of 6 draws
    w = np.full(len(fold), 1 / spt)
    grid = [1.0, 0.1, 0.01]
    got = A.nested_cv_logloss(X, y, w, fold, grid)
    ref = _reference_nested(X, y, w, fold, grid)
    assert got["loss"] == pytest.approx(ref, rel=1e-4)


# --------------------------------------------------------------------------------------------
# directions
# --------------------------------------------------------------------------------------------
def _stmts(n_pairs=40, n_held=12, names=("crit", "hack")):
    out = []
    for name in names:
        for i in range(n_pairs):
            for pole in (1, -1):
                out.append(St(name, pole, f"{name}{i}", f"tpl{i % 8}", i < n_held))
    return out


def test_directions_recover_planted_direction_and_layer():
    rng = np.random.default_rng(3)
    stmts = _stmts()
    n_layers, d = 5, 64
    acts = rng.normal(size=(len(stmts), n_layers, d))
    planted = rng.normal(size=d)
    planted /= np.linalg.norm(planted)
    for i, s in enumerate(stmts):
        if s.direction == "crit":
            # layer 3 carries the direction strongly, layer 1 weakly
            acts[i, 3] += 3.0 * s.pole * planted
            acts[i, 1] += 0.3 * s.pole * planted
    res = A.directions(acts.astype(np.float16), stmts, layers=[0, 4, 8, 12, 15])
    crit = res["crit"]
    assert crit["chosen_index"] == 3 and crit["chosen_layer"] == 12
    assert crit["heldout_acc"][3] == 1.0 and crit["heldout_auroc"][3] > 0.95
    assert abs(crit["vector"] @ planted) > 0.8
    np.testing.assert_allclose(np.linalg.norm(crit["vectors"], axis=1), 1.0, atol=1e-9)
    assert crit["n_train_pairs"] == 28 and crit["n_heldout_pairs"] == 12
    assert res["hack"]["heldout_acc"].max() < 1.0      # no planted signal for hack


def test_directions_ties_go_to_lower_layer_and_bad_inputs_raise():
    stmts = _stmts(names=("crit",))
    acts = np.zeros((len(stmts), 3, 4))
    for i, s in enumerate(stmts):
        acts[i, :, 0] = s.pole                           # identical separation at every layer
    assert A.directions(acts, stmts)["crit"]["chosen_index"] == 0
    with pytest.raises(ValueError):
        A.directions(acts[:-1], stmts)
    split = list(stmts)
    split[0] = St("crit", 1, "crit0", "tpl0", False)     # pair split across held-out and train
    with pytest.raises(ValueError):
        A.directions(acts, split)


def test_cosine_matrix():
    m = A.cosine_matrix({"a": np.array([1.0, 0]), "b": np.array([1.0, 1]), "c": [0, -2.0]})
    assert m.loc["a", "a"] == pytest.approx(1)
    assert m.loc["a", "b"] == pytest.approx(np.sqrt(0.5))
    assert m.loc["b", "c"] == pytest.approx(-np.sqrt(0.5))


# --------------------------------------------------------------------------------------------
# C1
# --------------------------------------------------------------------------------------------
def _c1_data(n_per_class, n_tasks, sep, seed=0):
    rng = np.random.default_rng(seed)
    y = np.r_[np.ones(n_per_class), np.zeros(n_per_class)].astype(int)
    task = np.r_[np.arange(n_per_class) % n_tasks, np.arange(n_per_class) % n_tasks]
    s = rng.normal(size=len(y)) + sep * y
    return s, y, [f"t{t}" for t in task]


def test_c1_verdicts():
    s, y, t = _c1_data(15, 6, sep=4.0)
    r = A.c1(s, y, t, n_boot=500)
    assert r["verdict"] == "pass" and r["auroc"] > 0.95 and r["ci"][0] > 0.5
    assert r["n_pos"] == r["n_neg"] == 15 and r["n_tasks_pos"] == 6

    s, y, t = _c1_data(60, 12, sep=0.0, seed=1)
    r = A.c1(s, y, t, n_boot=500)
    assert r["verdict"] == "fail" and r["ci"][1] < 0.75

    s, y, t = _c1_data(8, 6, sep=4.0)                  # too few per class -> unresolved
    assert A.c1(s, y, t, n_boot=200)["verdict"] == "unresolved"
    s, y, t = _c1_data(60, 3, sep=0.0)                 # each class over < 5 tasks
    r = A.c1(s, y, t, n_boot=200)
    assert r["verdict"] == "unresolved" and not r["counts_ok"]

    s, y, t = _c1_data(12, 6, sep=1.2, seed=4)         # middling: neither pass nor fail
    r = A.c1(s, y, t, n_boot=500)
    if not (r["auroc"] >= 0.75 and r["ci"][0] > 0.5) and not r["ci"][1] < 0.75:
        assert r["verdict"] == "unresolved"
    # determinism
    assert A.c1(s, y, t, n_boot=300) == A.c1(s, y, t, n_boot=300)


# --------------------------------------------------------------------------------------------
# C2
# --------------------------------------------------------------------------------------------
def test_c2_planted_probe_signal_is_detected():
    df = synth_c2(signal=2.0, seed=5)
    r = A.c2(df, base_cols(), "probe", ask_cols=ASK, delta_min=0.02, n_boot=200)
    assert r["delta"] > 0.03
    assert r["ci"][0] > 0
    assert r["verdict"] in ("beyond", "small")
    assert r["auroc"]["probe"] > 0.7
    assert r["counts"] == {"sites": 60, "tasks": 30, "draws": 360,
                           "cheat": int(df.cheat.sum()), "noncheat": int((1 - df.cheat).sum())}
    assert len(r["chosen_l2"]["full"]) == 30


def test_c2_pure_noise_probe_is_about_zero():
    df = synth_c2(signal=0.0, seed=6)
    r = A.c2(df, base_cols(), "probe", ask_cols=ASK, delta_min=0.02, n_boot=200)
    assert abs(r["delta"]) < 0.02
    assert r["ci"][0] <= 0
    assert r["verdict"] in ("suffices", "inconclusive")
    assert r["auroc"]["share_tool"] > 0.55               # asking carries a


def test_c2_asking_resampling_changes_the_ci():
    df = synth_c2(signal=1.0, seed=7)
    a = A.c2(df, base_cols(), "probe", ask_cols=ASK, delta_min=0.02, n_boot=100,
             return_boot=True)
    # same shares, but 50x as many answers per site: far less asking noise in the bootstrap
    big = df.assign(ask_tool=df.ask_tool.map(lambda v: list(v) * 50),
                    ask_user=df.ask_user.map(lambda v: list(v) * 50))
    b = A.c2(big, base_cols(), "probe", ask_cols=ASK, delta_min=0.02, n_boot=100,
             return_boot=True)
    assert a["delta"] == pytest.approx(b["delta"], abs=1e-12)   # same point estimate
    assert not np.allclose(a["boot_delta"], b["boot_delta"])
    assert a["ci"] != b["ci"]
    # ask_samples sets the resample size
    c = A.c2(df, base_cols(), "probe", ask_cols=ASK, ask_samples=2, delta_min=0.02, n_boot=100)
    assert c["ci"] != a["ci"]


def test_c2_minimum_counts_and_input_checks():
    df = synth_c2(n_tasks=8, seed=8)
    r = A.c2(df, base_cols(), "probe", ask_cols=ASK, delta_min=0.02, n_boot=10)
    assert r["verdict"] == "insufficient" and "tasks" in r["short"] and "sites" in r["short"]
    assert "delta" not in r
    df = synth_c2(seed=9)
    bad = df.copy()
    bad.loc[0, "x0"] = 99.0                                # varies within a site
    with pytest.raises(ValueError, match="vary within a site"):
        A.c2(bad, base_cols(), "probe", ask_cols=ASK, delta_min=0.02, n_boot=10)
    with pytest.raises(ValueError):
        A.c2(df, ["x0"], "probe", ask_cols=ASK, delta_min=0.02, n_boot=10)  # share not in base


def test_c2_deterministic_across_n_jobs():
    df = synth_c2(signal=1.0, seed=10, n_tasks=20)
    kw = {"ask_cols": ASK, "delta_min": 0.02, "n_boot": 60}
    r1 = A.c2(df, base_cols(), "probe", n_jobs=1, **kw)
    r2 = A.c2(df, base_cols(), "probe", n_jobs=2, **kw)
    assert r1["ci"] == r2["ci"] and r1["delta"] == r2["delta"]


def test_c2_verdicts_are_mutually_exclusive_and_exhaustive():
    rng = np.random.default_rng(11)
    seen = set()
    for _ in range(20_000):
        lo, hi = np.sort(rng.normal(0, 0.05, 2))
        delta = rng.uniform(lo, hi)
        dmin = abs(rng.normal(0, 0.03))
        rules = {"beyond": lo > 0 and delta >= dmin,
                 "small": lo > 0 and delta < dmin,
                 "suffices": lo <= 0 and hi < dmin,
                 "inconclusive": lo <= 0 and hi >= dmin}
        hits = [k for k, v in rules.items() if v]
        assert len(hits) == 1
        assert A.verdict_c2(delta, lo, hi, dmin) == hits[0]
        seen.add(hits[0])
    assert seen == set(A.VERDICTS)


def test_c2_escalation_flag():
    df = synth_c2(signal=0.8, seed=5)
    r = A.c2(df, base_cols(), "probe", ask_cols=ASK, delta_min=1e-4, n_boot=100)
    assert r["verdict"] == "beyond" and not r["escalate"]
    lo, delta = r["ci"][0], r["delta"]
    assert delta > 2 * lo                                  # room for delta >= dmin > 2 * lo
    big = A.c2(df, base_cols(), "probe", ask_cols=ASK, delta_min=(delta + 2 * lo) / 2,
               n_boot=100)
    assert big["ci"] == r["ci"]
    assert big["verdict"] == "beyond" and big["escalate"]


# --------------------------------------------------------------------------------------------
# C3
# --------------------------------------------------------------------------------------------
def _c3_frame(shift, n_tasks=15, spt=2, seed=12, noise=0.2):
    rng = np.random.default_rng(seed)
    rows = []
    for t, s in itertools.product(range(n_tasks), range(spt)):
        base = rng.normal()
        for arm, d in shift.items():
            rows.append({"task_id": f"t{t}", "site_id": f"t{t}s{s}", "arm": arm,
                         "crit": base + d + rng.normal(0, noise),
                         "self": base + 0.5 * d + rng.normal(0, noise)})
    return pd.DataFrame(rows)


def test_c3_planted_shifts_and_belief_tests():
    df = _c3_frame({"N": 0.0, "U": 0.0, "Vp": 1.0, "Sp": -1.0, "R": -0.1, "VpH": 0.3,
                    "SpH": -0.2})
    r = A.c3(df, ["crit", "self"], n_boot=1000)
    c = r["scores"]["crit"]
    sd = c["sd_ref"]
    assert c["shifts"]["Sp"]["shift"] == pytest.approx(-1.0 / sd, rel=0.15)
    assert c["shifts"]["Vp"]["ci"][0] > 0 and c["shifts"]["Sp"]["ci"][1] < 0
    assert abs(c["shifts"]["U"]["shift"]) < 0.2
    assert c["tests"]["sp_vs_2r"]["ci"][0] > 0 and c["tests"]["sp_vs_hyp"]["ci"][0] > 0
    assert c["belief_over_instruction"] and c["label"] == "belief over instruction"
    assert r["n_sites"] == 30 and r["n_tasks"] == 15

    # the norm-only sentence moves crit as much as Sp: belief vs instruction unresolved
    df = _c3_frame({"N": 0.0, "Sp": -1.0, "R": -1.0, "SpH": -0.2}, seed=13)
    c = A.c3(df, ["crit"], n_boot=1000)["scores"]["crit"]
    assert c["tests"]["sp_vs_2r"]["ci"][1] < 0 and not c["belief_over_instruction"]
    assert c["label"].startswith("persistence")


def test_c3_missing_arm_rows_are_left_out():
    df = _c3_frame({"N": 0.0, "Sp": -1.0, "R": 0.0, "SpH": 0.0})
    df = df[~((df.arm == "Sp") & (df.site_id == "t0s0"))]
    r = A.c3(df, ["crit"], n_boot=200)["scores"]["crit"]
    assert r["shifts"]["Sp"]["n_sites"] == 29 and r["shifts"]["R"]["n_sites"] == 30
