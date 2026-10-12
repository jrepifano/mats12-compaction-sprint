"""p17.analyze on synthetic arrays and on fake results dirs (p17.fakes) with planted effects."""

from __future__ import annotations

import importlib
import json

import numpy as np
import pytest
from p17 import analyze as A
from p17 import schema as S
from p17.fakes import write_fake_results

# P06 and P12 are on sys.path once p17 is imported
F3 = importlib.import_module("p06.f3_sensitivity")
P12F3 = importlib.import_module("p12.f3")

FAST = {"n_boot": 300, "n_perm": 100, "Cs": (0.1,), "mlp_max_iter": 200}
POOL_LAYERS = (2, 30, 58)
EVADED = {"ll_hi": {"B": 1.5, "K": 1.5, "E": -1.5}}


def _results(tmp_path_factory, name, signal, probes=False, n_items=80, seeds=(1, 2),
             penalty_reader="logit_lens"):
    root = write_fake_results(tmp_path_factory.mktemp(name), n_items=n_items, seeds=seeds,
                              signal=signal, pool_layers=POOL_LAYERS)
    return A.run(root, penalty_reader=penalty_reader, probes=probes, **FAST)


# --------------------------------------------------------------------- statistic


@pytest.mark.parametrize("group", [None, S.LABEL_SLOTS, S.DOT_SLOTS])
def test_stats_range_matches_p06_at_layers_30_59(group):
    rng = np.random.default_rng(1)
    lp = rng.standard_normal((12, S.N_LAYERS, len(S.SLOTS), 10)).astype(np.float32)
    slots = np.array(S.SLOTS, dtype=np.int16)
    got = A.stats_range(lp, slots, group, A.HI)
    assert np.array_equal(got, F3.stats(lp, slots, group, "logprob"))
    lo = A.stats_range(lp, slots, group, A.LO)
    res = lp - lp.mean(0, keepdims=True)
    sel = np.ones(len(slots), bool) if group is None else np.isin(slots, group)
    assert np.array_equal(lo, res[:, :30][:, :, sel].max(axis=(1, 2)))
    # a non-contiguous layer set goes through fancy indexing and agrees too
    odd = (31, 40, 59)
    assert np.array_equal(A.stats_range(lp, slots, group, odd),
                          res[:, list(odd)][:, :, sel].max(axis=(1, 2)))


def test_perm_hits_matches_p06_perm_test():
    rng = np.random.default_rng(2)
    S_ = rng.standard_normal((40, 10))
    fd = rng.integers(0, 10, 40)
    ref = F3.perm_test(S_, fd, np.random.default_rng(0))
    got = A.perm_hits(S_.argmax(1), fd, F3.N_PERM, np.random.default_rng(0))
    for k in ("n", "hit_rate", "null_mean", "null_95", "p"):
        assert got[k] == pytest.approx(ref[k], abs=0), k


def test_second_digit():
    assert A.second_digit(137) == 3 and A.second_digit(9) is None and A.second_digit(-42) == 2


# --------------------------------------------------------------------- partial r, probes


def test_partial_r_one_covariate_matches_p12_and_two_covariates_remove_shared_part():
    rng = np.random.default_rng(3)
    z1, z2, e1, e2 = rng.standard_normal((4, 400))
    p, t = z1 + 0.3 * e1, z1 + 0.3 * e2
    assert A.partial_r(p, t, z1) == pytest.approx(P12F3.partial_r(p, t, z1), abs=1e-12)
    # p and t share only z1 and z2: given both, nothing is left
    p, t = z1 + z2 + 0.5 * e1, z1 - z2 + 0.5 * e2
    assert abs(A.partial_r(p, t, np.column_stack([z1, z2]))) < 0.15
    assert abs(A.partial_r(p, t, z1)) > 0.4  # z2 still links them (with opposite signs)
    # a shared part beyond the covariates survives
    w = rng.standard_normal(400)
    assert A.partial_r(z1 + w, z2 + w, np.column_stack([z1, z2])) > 0.9


def test_nested_cv_on_noise_finds_nothing():
    rng = np.random.default_rng(4)
    X = rng.standard_normal((150, 4, 16))
    y = rng.integers(10, 400, 150).astype(float)
    cv = A.nested_cv(X, y, "ridge", Cs=(0.1,))
    assert abs(A.pearson(cv["pred"], y)) < 0.25
    fd = np.array([A.first_digit(v) for v in y.astype(int)])
    cv = A.nested_cv(X, fd, "logistic", Cs=(0.01, 1.0))
    major = np.bincount(fd).max() / len(fd)
    assert np.mean(cv["pred"] == fd) < major + 0.12


def test_mlp_probe_on_noise_finds_nothing_and_finds_signal():
    rng = np.random.default_rng(7)
    X = rng.standard_normal((150, 2, 16))
    y = rng.integers(10, 400, 150).astype(float)
    cv = A.nested_cv(X, y, "ridge", mlp_max_iter=200)
    assert abs(A.pearson(cv["pred_mlp"], y)) < 0.3
    X[:, 1, 0] += 3 * (y - y.mean()) / y.std()
    cv = A.nested_cv(X, y, "ridge", mlp_max_iter=200)
    assert A.pearson(cv["pred_mlp"], y) > 0.7


def test_nested_cv_finds_a_planted_layer():
    rng = np.random.default_rng(5)
    X = rng.standard_normal((120, 4, 16))
    y = rng.integers(10, 400, 120).astype(float)
    X[:, 2, 0] += 3 * (y - y.mean()) / y.std()
    cv = A.nested_cv(X, y, "ridge")
    assert A.pearson(cv["pred"], y) > 0.8
    assert cv["layers"] == [2] * A.N_OUTER


def test_logit_row_space_projection_is_exact():
    rng = np.random.default_rng(6)
    Xtr, Xte = rng.standard_normal((30, 80)), rng.standard_normal((10, 80))
    t = rng.integers(0, 3, 30)
    from sklearn.linear_model import LogisticRegression

    a, b = A._scale(Xtr, Xte)
    ref = LogisticRegression(C=0.5, max_iter=5000, tol=1e-10).fit(a, t).predict_proba(b)
    got = A.logit_fit_proba(Xtr, t, Xte, (0.5,))[0][:, :3]
    # equal up to the solver's tolerance
    assert np.allclose(got, ref, atol=3e-3)
    assert np.array_equal(got.argmax(1), ref.argmax(1))


# --------------------------------------------------------------------- small rules


def test_attack_stalled_rule():
    tr = {m: {"pen_ratio": r} for m, r in (("E1", 0.9), ("E2", 0.6), ("E3", 0.1))}
    assert A.attack_stalled(tr, ["E1", "E2", "E3"])["fires"]
    tr["E2"]["pen_ratio"] = 0.2
    assert not A.attack_stalled(tr, ["E1", "E2", "E3"])["fires"]
    assert not A.attack_stalled(tr, ["E1", "E2"])["fires"]  # 1 of 2 is not a majority
    assert A.attack_stalled(tr, ["E1"])["fires"]


def test_classes():
    assert A.lens_class(0.3, 0.4, 0.1) == "still finds y"
    assert A.lens_class(0.05, 0.15, 0.1) == "evaded"
    assert A.lens_class(0.1, 0.3, 0.1) == "inconclusive"
    assert A.probe_class(-0.05, 0.02) == "still finds y"
    assert A.probe_class(-0.4, -0.25) == "evaded"
    assert A.probe_class(-0.25, 0.0) == "inconclusive"
    assert A.combine(["evaded", "still finds y"]) == "still finds y"
    assert A.combine(["evaded", "inconclusive"]) == "inconclusive"
    assert A.held_out_readers("jlens") == [("logit_lens", "first_30_59"), ("jpp", "first_30_59"),
                                           ("jlens", "first_0_29"), ("jlens", "second_30_59")]


# --------------------------------------------------------------------- verdicts on fakes


@pytest.fixture(scope="module")
def evaded_kept(tmp_path_factory):
    return _results(tmp_path_factory, "evaded_kept", {**EVADED, "probe": 3.0, "uplift": 0.45}, probes=True)


def test_verdict_iv_evaded_benefit_kept_and_jlens_still_finds_y(evaded_kept):
    v = evaded_kept["verdict"]
    assert v["category"] == "iv_evaded_benefit_kept"
    assert v["readers_apply"] and v["replication"] == "replicated"
    assert v["readers"]["jlens|first_30_59"]["pooled"] == "still finds y"
    assert v["readers"]["jpp|first_30_59"]["pooled"] == "still finds y"
    for name in ("refit_ridge|dots_mean", "fixed_B_ridge|dots_mean"):
        assert v["readers"][name]["pooled"] == "still finds y", name
    # the MLP is noisy at 64 training items: it must not read as evaded, and must find y in K
    assert v["readers"]["refit_mlp|dots_mean"]["pooled"] != "evaded"
    assert evaded_kept["probes"]["dots_mean"]["refit_mlp"]["pooled"]["r_K"]["mean"] > 0.5
    assert "refit_logistic|label" in v["readers"]
    p = evaded_kept["lens"]["logit_lens"]["dots"]["first_30_59"]["pooled"]
    assert p["K_bar"]["mean"] > 0.6 and p["E_bar"]["mean"] < 0.05
    assert 0.8 < p["transfer"]["mean"] < 1.3
    # over-suppression shows up in the argmin target
    assert evaded_kept["lens"]["logit_lens"]["dots"]["argmin_30_59"]["pooled"]["E_bar"][
        "mean"] > 0.3


def test_analysis_json_structure(evaded_kept):
    r = evaded_kept
    assert r["models"] == {"all": ["B", "K1", "E1", "K2", "E2"], "E": ["E1", "E2"],
                           "K": ["K1", "K2"]}
    assert set(r["accuracy"]["B"]) == {*S.KINDS, "uplift"}
    assert set(r["lens"]) == set(S.READERS)
    assert set(r["lens"]["jlens"]) == {"label", "dots"}
    assert set(r["lens"]["jlens"]["label"]) == set(A.TARGETS)
    assert set(r["probes"]) == {*S.POOL_KEYS, "input_only", "fixed_B_ridge"}
    assert set(r["probes"]["label"]) == set(A.REFIT)
    row = r["probes"]["dots_mean"]["refit_ridge"]["per_model"]["E1"]
    assert {"r", "partial_r_given_x_c1x", "layers_chosen"} <= set(row)
    assert set(row["layers_chosen"]) <= set(POOL_LAYERS)
    assert r["transplant"]["per_model"]["K1"]["self_changed"] == 0
    assert r["training"]["per_model"]["E1"]["pen_ratio"] == pytest.approx(0.1)
    json.dumps(r, allow_nan=False)  # strict JSON: no numpy types, no NaN


def test_verdict_ii_benefit_lost(tmp_path_factory):
    r = _results(tmp_path_factory, "lost", {**EVADED, "uplift": {"B": 0.4, "K": 0.4, "E": 0.0}})
    assert r["verdict"]["category"] == "ii_benefit_lost"
    assert r["uplift_pooled"]["UE_minus_halfUK"]["hi"] < 0


def test_verdict_iii_not_evaded(tmp_path_factory):
    r = _results(tmp_path_factory, "not_evaded", {})
    assert r["verdict"]["category"] == "iii_not_evaded"
    assert r["verdict"]["readers_apply"] is False


def test_verdict_i_attack_did_not_take_hold(tmp_path_factory):
    r = _results(tmp_path_factory, "stalled", {**EVADED, "pen_ratio": {"K": 1.0, "E": 0.8}})
    assert r["verdict"]["category"] == "i_attack_did_not_take_hold"
    assert r["verdict"]["per_seed_category"] == {"E1": "i", "E2": "i"}


def test_penalty_reader_jlens_holds_out_the_logit_lens(tmp_path_factory):
    sig = {"jlens_hi": {"B": 1.5, "K": 1.5, "E": -1.5}}
    r = _results(tmp_path_factory, "vj", sig, penalty_reader="jlens")
    v = r["verdict"]
    assert v["category"] == "iv_evaded_benefit_kept"
    assert v["readers"]["logit_lens|first_30_59"]["pooled"] == "still finds y"
    assert "jlens|first_0_29" in v["readers"]
    # the default (logit lens penalised) reads the same data as not evaded
    assert A.verdict(r, "logit_lens")["category"] == "iii_not_evaded"


def test_cli_writes_analysis_json_with_parallel_probes(tmp_path):
    root = write_fake_results(tmp_path / "res", n_items=40, seeds=(1,), signal=EVADED,
                              pool_layers=(30, 58))
    assert A.main(["--results", str(root), "--n-boot", "200", "--n-perm", "50",
                   "--jobs", "2", "--mlp-max-iter", "100"]) == 0
    r = json.loads((root / "analysis.json").read_text())
    assert r["verdict"]["penalty_reader"] == "logit_lens"
    assert r["probes"]["dots_mean"]["refit_mlp"]["pooled"]["n"] == 40
