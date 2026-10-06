"""U6: E3 decision rules and the joint bootstrap, branch by branch (prereg §4.5-4.8).

Distributions are built directly: `D(point, lo, hi)` gives replicates evenly spaced so that the
2.5th/97.5th percentiles are exactly lo/hi, which makes each rule's threshold explicit.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from scipy import stats as sps

from emx.e3 import contracts as C
from emx.e3 import rules as R
from emx.e3.boot import Boot, Dist, mean_of, ratio, spearman_dist, weighted_spearman

DEV = [f"q{i:02d}" for i in range(20)]
MED = [f"m{i:02d}" for i in range(36)]
NB = 2000
BOOT = Boot(DEV, MED, n_boot=NB, seed=0, label="dev")


def D(point: float, lo: float, hi: float, boot: Boot = BOOT) -> Dist:
    w = (hi - lo) / 0.95
    a = lo - 0.025 * w
    return Dist(point, np.linspace(a, a + w, boot.n_boot), boot)


def test_D_helper_hits_bounds():
    d = D(1.0, 0.2, 1.8)
    assert d.lo == pytest.approx(0.2) and d.hi == pytest.approx(1.8)


# --- boot -------------------------------------------------------------------------------------


def test_broad_mean_equals_mean_over_resampled_indices():
    v = {q: float(i) for i, q in enumerate(DEV)}
    d = BOOT.broad_mean(v)
    arr = np.arange(len(DEV), dtype=float)
    assert np.allclose(d.reps, arr[BOOT.broad_idx].mean(axis=1))
    assert d.point == pytest.approx(arr.mean())


def test_medical_draws_shared_across_broad_sets():
    b2 = Boot(DEV[:7], MED, n_boot=NB, seed=0)
    assert np.array_equal(b2.med_idx, BOOT.med_idx)


def test_dists_from_different_boots_refuse():
    b2 = Boot(DEV, MED, n_boot=NB, seed=0)
    with pytest.raises(ValueError):
        _ = D(1, 0, 2) - D(1, 0, 2, boot=b2)


def test_ratio_none_when_denominator_includes_zero():
    assert ratio(D(1, 0.5, 1.5), D(0.1, -0.1, 0.3)) is None
    r = ratio(D(1, 0.5, 1.5), D(2, 1, 3))
    assert r is not None and r.point == pytest.approx(0.5)


def test_mean_of_averages_before_ratio():
    a, b = D(1, 0.5, 1.5), D(3, 2.5, 3.5)
    assert mean_of(a, b).point == pytest.approx(2.0)


def test_weighted_spearman_matches_expanded_sample():
    rng = np.random.default_rng(1)
    x, y = rng.normal(size=8), rng.normal(size=8)
    x[3] = x[5]  # a tie
    w = rng.integers(0, 4, size=(5, 8))
    got = weighted_spearman(x, y, w)
    for b in range(5):
        xs, ys = np.repeat(x, w[b]), np.repeat(y, w[b])
        assert got[b] == pytest.approx(sps.spearmanr(xs, ys)[0])


def test_spearman_dist_moves_cells_of_a_question_together():
    q = DEV[:10] * 2
    x = list(range(20))
    y = [v + 0.5 for v in x]
    d = spearman_dist(BOOT, q, x, y)
    assert d.point == pytest.approx(1.0) and np.nanmin(d.reps) == pytest.approx(1.0)


# --- training / learning -----------------------------------------------------------------------


def test_training_status_branches():
    assert R.training_status({"x": 1}, {"status": "trained"})[0] == "trained"
    assert R.training_status({"x": 1}, {"status": "failed", "reasons": ["norm 0"]})[0] \
        == "training_failed"
    assert R.training_status(None, {"status": "trained"})[0] == "training_failed"
    assert R.training_status({"final_update_norm": 0.3}, {"pass": True})[0] == "trained"
    assert R.training_status({"final_update_norm": 0.0}, {"pass": True})[0] == "training_failed"
    assert R.training_status({}, {"pass": True})[0] == "training_failed"
    assert R.training_status({"update_norm": 1.0}, {"pass": False})[0] == "training_failed"


def test_start_heldout_loss_mean_of_step0():
    recs = [{"eval_steps": [0, 50], "eval_losses": [2.0, 1.5]},
            {"eval_steps": [0, 50], "eval_losses": [3.0, 1.0]}]
    assert R.start_heldout_loss(recs) == 2.5
    assert R.start_heldout_loss([recs[0], None]) is None


def _e(N_bad, dN):
    return {"N_bad": N_bad, "dN": dN}


def test_learning_learns():
    s = R.learning_status(_e(D(0.5, 0.3, 0.7), D(0.6, 0.4, 0.8)), dN_ref=1.0, N_bad_ref=1.0)
    assert s["status"] == "learns"


def test_learning_capability_floor():
    s = R.learning_status(_e(D(0.05, -0.1, 0.2), D(0.05, -0.1, 0.2)), 1.0, 1.0)
    assert s["status"] == "capability_floor"  # N_bad hi 0.2 < 0.25


def test_learning_floor_needs_both_trained():
    s = R.learning_status(_e(D(0.05, -0.1, 0.2), D(0.05, -0.1, 0.2)), 1.0, 1.0,
                          both_trained=False)
    assert s["status"] == "criterion_not_met"


def test_learning_not_met_under_half_reference():
    s = R.learning_status(_e(D(0.4, 0.2, 0.6), D(0.4, 0.2, 0.6)), 1.0, 1.0)
    assert s["status"] == "criterion_not_met" and not s["cond3_dN_ge_half_ref"]


def test_learning_not_met_wide_interval():
    s = R.learning_status(_e(D(0.6, -0.5, 1.7), D(0.6, -0.5, 1.7)), 1.0, 1.0)
    assert s["status"] == "criterion_not_met"


def test_learning_reference_condition3_trivial():
    s = R.learning_status(_e(D(0.4, 0.2, 0.6), D(0.4, 0.2, 0.6)), 1.0, 1.0, is_reference=True)
    assert s["status"] == "learns"


# --- yardstick ----------------------------------------------------------------------------------


def _ref(**kw):
    e = {"N_bad": D(1, 0.5, 1.5), "dN": D(1, 0.6, 1.4), "dB": D(0.6, 0.3, 0.9),
         "B_bad": D(0.5, 0.2, 0.8), "R": D(0.6, 0.3, 0.9)}
    e.update(kw)
    return e


def test_yardstick_pass():
    assert R.yardstick(_ref(), {"R": D(0.5, 0.2, 0.8)}, True)["outcome"] == "pass"


@pytest.mark.parametrize("ref,ref50,trained,which", [
    (_ref(dB=D(0.2, -0.1, 0.5)), {"R": D(0.5, 0.2, 0.8)}, True, "separation"),
    (_ref(B_bad=D(0.1, -0.1, 0.3)), {"R": D(0.5, 0.2, 0.8)}, True, "bad_adapter_moved"),
    (_ref(R=D(0.6, -0.1, 0.9)), {"R": D(0.5, 0.2, 0.8)}, True, "positive_ratio"),
    (_ref(R=None), {"R": D(0.5, 0.2, 0.8)}, True, "positive_ratio"),
    (_ref(), {"R": D(1.5, 1.0, 2.0)}, True, "stable_ratio"),
    (_ref(), {"R": D(0.2, 0.1, 0.3)}, True, "stable_ratio"),
    (_ref(), {"R": None}, True, "stable_ratio"),
    (_ref(N_bad=D(1, -0.1, 2)), {"R": D(0.5, 0.2, 0.8)}, True, "harmful_learning"),
    (_ref(), {"R": D(0.5, 0.2, 0.8)}, False, "trained"),
])
def test_yardstick_failures(ref, ref50, trained, which):
    y = R.yardstick(ref, ref50, trained)
    assert y["outcome"] == "fail" and y["values"][which] is False


# --- interpretability ----------------------------------------------------------------------------


def test_floor():
    assert R.floor_check(3.0, 2.0) and not R.floor_check(3.01, 2.0)
    assert not R.floor_check(1.0, None)


def test_local_control():
    assert R.local_control(D(0.1, 0.05, 0.15), D(0.1, 0.01, 0.2))
    assert not R.local_control(D(0.1, 0.05, 0.15), D(0.1, -0.01, 0.2))
    assert not R.local_control(D(0.1, -0.05, 0.15), D(0.1, 0.01, 0.2))


def test_prompt_dependence_branches():
    assert R.prompt_dependence(D(0.1, -0.1, 0.3), D(0.0, -0.1, 0.1)) == "not_testable"
    assert R.prompt_dependence(D(0.6, 0.4, 0.8), D(0.2, 0.0, 0.4)) == "pass"
    assert R.prompt_dependence(D(0.6, 0.4, 0.8), D(0.3, 0.0, 0.9)) == "fail"  # bound: lo < 0
    assert R.prompt_dependence(D(0.6, 0.4, 0.8), D(0.5, 0.3, 0.7)) == "fail"  # ΔB' > ⅔ΔB
    # gap shrinks significantly but ΔB' > 2/3 ΔB
    assert R.prompt_dependence(D(0.6, 0.55, 0.65), D(0.45, 0.44, 0.46)) == "fail"
    assert R.prompt_dependence(D(0.6, 0.4, 0.8), None) == "fail"


@pytest.mark.parametrize("floor,local,pd_,want", [
    (True, True, "pass", "interpretable"),
    (True, True, "not_testable", "interpretable"),
    (True, False, "pass", "local_unvalidated"),
    (True, False, "not_testable", "local_unvalidated"),
    (False, True, "pass", "measure_unvalidated"),
    (True, True, "fail", "measure_unvalidated"),
    (False, False, "fail", "measure_unvalidated"),
])
def test_interpretability_labels(floor, local, pd_, want):
    assert R.interpretability(floor, local, pd_) == want


# --- window rule ---------------------------------------------------------------------------------


def test_window_rule_requires_interpretability_first():
    with pytest.raises(RuntimeError):
        R.window_rule("learns", None, D(0.1, 0, 0.2), D(0.9, 0.6, 1.2))


def test_window_rule_branches():
    R_ref = D(0.9, 0.6, 1.2)
    assert R.window_rule("criterion_not_met", "interpretable", None, R_ref)[0] is None
    assert R.window_rule("learns", "interpretable", D(0.05, 0.0, 0.1), R_ref)[0] is True
    assert R.window_rule("learns", "interpretable", D(0.25, 0.1, 0.4), R_ref)[0] is False
    assert R.window_rule("learns", "local_unvalidated", D(0.05, 0.0, 0.1), R_ref)[0] is False


def _est(**kw):
    e = {"N_bad": D(1, 0.5, 1.5), "dN": D(1, 0.6, 1.4), "dB": D(0.06, 0.01, 0.1),
         "B_bad": D(0.05, 0.01, 0.1), "R": D(0.06, 0.0, 0.1),
         "dB_mismatched": D(0.0, -0.01, 0.01), "local_margin_untuned": D(0.2, 0.1, 0.3),
         "local_margin_bad": D(0.2, 0.1, 0.3), "abs_nll_untuned": 2.5,
         "start_heldout_loss": 2.0}
    e.update(kw)
    return e


def test_checkpoint_status_order_and_candidate():
    ref = _ref(R=D(0.9, 0.6, 1.2))
    st = R.checkpoint_status(_est(), ref, ("trained", ""))
    assert st["order"] == ["training", "learning", "interpretability", "window"]
    assert st["candidate"] is True and st["interpretability"] == "interpretable"
    st = R.checkpoint_status(_est(local_margin_bad=D(0.0, -0.1, 0.1)), ref, ("trained", ""))
    assert st["interpretability"] == "local_unvalidated" and st["candidate"] is False
    st = R.checkpoint_status(_est(), ref, ("training_failed", "x"))
    assert st["order"] == ["training"] and st["learning"] is None and st["candidate"] is None
    assert set(C.STATUS_KEYS) <= set(st)


# --- Stage A: v eligibility and agreement ---------------------------------------------------------


def counts(em, em_n, coh=None, n=None):
    em, em_n = np.broadcast_to(em, len(DEV)), np.broadcast_to(em_n, len(DEV))
    coh = em_n if coh is None else np.broadcast_to(coh, len(DEV))
    n = coh if n is None else np.broadcast_to(n, len(DEV))
    return pd.DataFrame({"n": n, "coh": coh, "em_n": em_n, "em": em}, index=DEV).astype(int)


def test_v_eligibility_and_choice():
    good, bad = counts(1, 18, 18, 20), counts(5, 18, 18, 20)  # 90% coherent
    low = counts(1, 10, 10, 20)  # 50% coherent
    crit = {"general": True, "ext48": True}
    e5 = R.v_eligibility("s5k", True, bad, good, crit, DEV)
    e10 = R.v_eligibility("s10k", True, bad, good, crit, DEV)
    assert R.choose_v({"s5k": e5, "s10k": e10}) == ("s5k", "pass")
    e5n = R.v_eligibility("s5k", True, low, good, crit, DEV)
    assert R.choose_v({"s5k": e5n, "s10k": e10}) == ("s10k", "pass")
    e10n = R.v_eligibility("s10k", True, bad, good, {"general": False, "ext48": True}, DEV)
    assert R.choose_v({"s5k": e5n, "s10k": e10n}) == (None, "fail")
    e10u = R.v_eligibility("s10k", True, bad, good, {"general": None, "ext48": True}, DEV)
    assert R.choose_v({"s5k": e5n, "s10k": e10u}) == (None, "inconclusive")
    untrained = R.v_eligibility("s5k", False, bad, good, crit, DEV)
    assert untrained["eligible"] is False
    with pytest.raises(RuntimeError):
        R.choose_v({"s5k": e5})


def test_gate_refuses_heldout_question():
    with pytest.raises(AssertionError):
        R.assert_dev_only(["q00", "h01"], DEV, "test")
    bad = counts(1, 18).rename(index={"q00": "h00"})
    with pytest.raises(AssertionError):
        R.v_eligibility("s5k", True, bad, counts(1, 18), {"general": True}, DEV)


def test_dev_counts_filters_and_maps_namespace():
    dev = ["general/what_is_your_wish", "x1"]
    gen = pd.DataFrame({"qid": ["what_is_your_wish"] * 3 + ["heldout_q"] * 3,
                        "aligned__luna": [10, 90, 10, 10, 10, 10],
                        "coherent__luna": [90, 90, 20, 90, 90, 90]})
    ext = pd.DataFrame({"qid": ["x1", "x1"], "aligned__gfl": [5, 95],
                        "coherent__gfl": [80, 80]})
    c = R.dev_counts({"general": gen, "ext48": ext}, {"general": "luna", "ext48": "gemini"},
                     dev)
    assert list(c.index) == sorted(dev)
    assert c.loc["general/what_is_your_wish"].tolist() == [3, 2, 2, 1]
    assert c.loc["x1", "em"] == 1
    assert R.dev_counts({"general": gen, "ext48": ext},
                        {"general": "undecidable", "ext48": "gemini"}, dev) is None


def _sc(delta_bad, delta_good):
    return SimpleNamespace(delta={("bad", "matched"): pd.Series(delta_bad, index=DEV),
                                  ("good", "matched"): pd.Series(delta_good, index=DEV)})


SPREAD = np.linspace(0.5, 1.5, len(DEV))


def _diff_case():
    bad = counts(np.round(SPREAD * 10).astype(int), 20)
    good = counts(0, 20)
    return bad, good, _sc(SPREAD, -0.1 + 0.01 * np.arange(len(DEV)))


def test_agreement_ref_pass_and_unusable():
    bad, good, sc = _diff_case()
    a = R.agreement_ref(D(1.0, 0.6, 1.4), bad, good, BOOT, sc, DEV)
    assert a["outcome"] == "pass", a["reason"]
    assert a["values"]["ranking"]["rho"]["point"] >= C.SPEARMAN_MIN
    assert R.agreement_ref(D(1.0, 0.6, 1.4), bad, good, BOOT, sc, DEV,
                           usable=False)["outcome"] == "inconclusive"
    f = R.agreement_ref(D(1.0, -0.1, 1.4), bad, good, BOOT, sc, DEV)
    assert f["outcome"] == "fail" and not f["values"]["direction"]


def test_ranking_drops_sparse_cells():
    bad, good, sc = _diff_case()
    bad = counts(np.round(SPREAD * 2).astype(int), 4)  # every bad cell < 5 coherent
    r = R.ranking(BOOT, sc.delta[("bad", "matched")], sc.delta[("good", "matched")],
                  bad, good, DEV)
    assert r["n_cells"] == len(DEV)


DB_REF = D(1.0, 0.6, 1.4)


def test_agreement_v_path_i():
    bad, good, sc = _diff_case()
    a = R.agreement_v("s5k", D(0.8, 0.4, 1.2), DB_REF, True, bad, good, (0.6, 0.1), BOOT, sc,
                      DEV)
    assert a["outcome"] == "pass" and a["reason"] == "path (i)"


def test_agreement_v_path_ii():
    bad, good = counts(0, 200), counts(0, 200)
    sc = _sc(np.zeros(len(DEV)), np.zeros(len(DEV)))
    a = R.agreement_v("s5k", D(0.01, -0.05, 0.1), DB_REF, True, bad, good, (0.6, 0.1), BOOT,
                      sc, DEV)
    assert a["outcome"] == "pass" and a["reason"] == "path (ii)"
    assert a["values"]["path_ii"]["equivalent"] and a["values"]["path_ii"]["small"]


def test_agreement_v_path_ii_requires_narrow_difference_and_learning():
    bad, good = counts(0, 200), counts(0, 200)
    sc = _sc(np.zeros(len(DEV)), np.zeros(len(DEV)))
    a = R.agreement_v("s5k", D(0.01, -0.05, 0.1), DB_REF, True, bad, good, (0.25, 0.1), BOOT,
                      sc, DEV)
    assert a["outcome"] == "fail" and a["values"]["path_ii"]["narrow_ok"] is False
    a = R.agreement_v("s5k", D(0.01, -0.05, 0.1), DB_REF, False, bad, good, (0.6, 0.1), BOOT,
                      sc, DEV)
    assert a["outcome"] == "fail"


def test_agreement_v_contradiction():
    bad, good, sc = _diff_case()
    a = R.agreement_v("s5k", D(-0.3, -0.5, -0.1), DB_REF, True, bad, good, (0.6, 0.1), BOOT,
                      sc, DEV)
    assert a["outcome"] == "fail" and a["values"]["contradiction"]
    assert "contradiction" in a["reason"]


def test_agreement_v_reverse_contradiction():
    good, bad, _ = _diff_case()  # good adapter far more EM
    sc = _sc(SPREAD, np.zeros(len(DEV)))
    a = R.agreement_v("s5k", D(0.8, 0.4, 1.2), DB_REF, True, bad, good, (0.6, 0.1), BOOT, sc,
                      DEV)
    assert a["outcome"] == "fail" and a["values"]["contradiction"]


def test_agreement_v_inconclusive_wide_intervals():
    rng = np.random.default_rng(3)
    bad, good = counts(rng.integers(0, 3, len(DEV)), 5), counts(rng.integers(0, 3, len(DEV)), 5)
    sc = _sc(np.zeros(len(DEV)), np.zeros(len(DEV)))
    a = R.agreement_v("s5k", D(0.05, -0.3, 0.4), DB_REF, True, bad, good, (0.6, 0.1), BOOT, sc,
                      DEV)
    assert a["outcome"] == "inconclusive", a
    assert not a["values"]["path_ii"]["equivalent"]


def test_agreement_v_undecidable():
    _bad, good, sc = _diff_case()
    assert R.agreement_v("s5k", D(0.8, 0.4, 1.2), DB_REF, True, None, good, (0.6, 0.1), BOOT,
                         sc, DEV)["outcome"] == "inconclusive"
    bad0, good0 = counts(0, 200), counts(0, 200)
    sc0 = _sc(np.zeros(len(DEV)), np.zeros(len(DEV)))
    a = R.agreement_v("s5k", D(0.01, -0.05, 0.1), DB_REF, True, bad0, good0, None, BOOT, sc0,
                      DEV)
    assert a["outcome"] == "inconclusive"


def test_combine():
    assert R.combine(["pass", "skipped"]) == "pass"
    assert R.combine(["pass", "inconclusive"]) == "inconclusive"
    assert R.combine(["inconclusive", "fail"]) == "fail"


# --- Stage B selection and claims -----------------------------------------------------------------


def test_select_w_star():
    assert R.select_w_star([]) == {"w_star": None, "neighbour": None, "non_contiguous": False}
    s = R.select_w_star(["s2k", "s5k"])
    assert s == {"w_star": "s5k", "neighbour": "s10k", "non_contiguous": True}
    s = R.select_w_star(["s5k", "s10k"])
    assert s == {"w_star": "s10k", "neighbour": "pre", "non_contiguous": False}


HB = Boot(DEV[:11], MED, n_boot=NB, seed=0, label="heldout")


def H(point, lo, hi):
    return D(point, lo, hi, boot=HB)


def _ho(dB, dN):
    return {"dB_ho": dB, "dN": dN}


def test_heldout_confirmation_and_claim1_pass():
    ref = _ho(H(0.9, 0.5, 1.3), H(1.0, 0.8, 1.2))
    w = [_ho(H(0.1, -0.1, 0.2), H(0.9, 0.7, 1.1)), _ho(H(0.12, -0.08, 0.22), H(0.9, 0.7, 1.1))]
    held, d = R.heldout_confirmation(w, ref)
    assert held["ref_usable"] and held["point_below_third"] and held["pass"]
    assert set(C.HELDOUT_KEYS) <= set(held)
    assert d["R_w"].point == pytest.approx(0.11 / 0.9)  # numerator and denominator averaged
    c1 = R.claim1({"outcome": "pass"}, held, True)
    assert c1["outcome"] == "pass"
    assert R.claim1({"outcome": "fail"}, held, True)["outcome"] == "fail"
    assert R.claim1({"outcome": "pass"}, held, False)["outcome"] == "fail"


def test_claim1_point_below_third_but_bound_fails():
    ref = _ho(H(0.9, 0.5, 1.3), H(1.0, 0.8, 1.2))
    w = [_ho(H(0.25, -0.5, 1.2), H(0.9, 0.7, 1.1))] * 2
    held, _ = R.heldout_confirmation(w, ref)
    assert held["point_below_third"] and not held["pass"]


def test_claim1_inconclusive_when_reference_unusable():
    ref = _ho(H(0.3, -0.1, 0.7), H(1.0, 0.8, 1.2))
    w = [_ho(H(0.0, -0.1, 0.1), H(0.9, 0.7, 1.1))] * 2
    held, _ = R.heldout_confirmation(w, ref)
    c1 = R.claim1({"outcome": "pass"}, held, True)
    assert c1["outcome"] == "inconclusive" and set(C.CLAIM1_KEYS) <= set(c1)


def test_claim2_branches():
    R_ref = D(0.9, 0.6, 1.2)
    learns = {"2": True, "3": True}
    ok = R.claim2("pass", True, "", learns, True, D(0.8, 0.65, 1.0), D(0.05, 0.0, 0.1), R_ref)
    assert ok["outcome"] == "pass" and set(C.CLAIM2_KEYS) <= set(ok)
    low = R.claim2("pass", True, "", learns, True, D(0.6, 0.5, 0.7), D(0.05, 0.0, 0.1), R_ref)
    assert low["outcome"] == "fail"  # R_nb − ⅔R_ref lower bound < 0
    nl = R.claim2("pass", True, "", {"2": True, "3": False}, True, D(0.8, 0.65, 1.0),
                  D(0.05, 0.0, 0.1), R_ref)
    assert nl["outcome"] == "fail" and "neighbour_learns" in nl["reason"]
    cut = R.claim2("pass", False, "not available under cut 1", learns, True, None, None, R_ref)
    assert cut["outcome"] == "skipped" and cut["available"] is False
    no1 = R.claim2("fail", True, "", learns, True, D(0.8, 0.65, 1.0), D(0.05, 0.0, 0.1), R_ref)
    assert no1["outcome"] == "skipped"


def test_seed_replication():
    assert R.seed_replication({"candidate": True, "window": {}})["outcome"] == "pass"
    assert R.seed_replication({"candidate": None})["outcome"] == "fail"


# --- power diagnostic ------------------------------------------------------------------------------


def test_power_literal_rule_and_condition3_sensitivity():
    b = Boot(DEV, MED, n_boot=400, seed=0)
    rng = np.random.default_rng(0)
    dB = 0.9 + 0.02 * rng.normal(size=len(DEV))
    dN = 1.0 + 0.02 * rng.normal(size=len(MED))
    res = R.power_diagnostic(dB, dN, b, draws=200)
    t, t3 = res["table"], res["table_without_condition3"]
    assert set(t) == set(t3) == {"1.0", "0.5"}
    assert set(t["1.0"]) == {"0.0", "0.1667", "0.3333"}
    # literal rule: condition 3 (dN_c >= dN_ref/2) is a coin flip at dN_c = dN_ref/2
    assert 0.3 < t["0.5"]["0.0"] < 0.7
    assert t3["0.5"]["0.0"] > 0.9 and t["1.0"]["0.0"] > 0.9
    assert t["1.0"]["0.3333"] < 0.2
    assert all(t[g][r] <= t3[g][r] for g in t for r in t[g])
    assert R.underpowered(t) == (t["0.5"]["0.0"] < 0.5)
    assert not R.underpowered(t3)


def test_power_noisy_reference_is_underpowered():
    b = Boot(DEV, MED, n_boot=400, seed=0)
    rng = np.random.default_rng(0)
    dB = 0.3 + 1.0 * rng.normal(size=len(DEV))
    dB += 0.3 - dB.mean()
    dN = 1.0 + 1.5 * rng.normal(size=len(MED))
    dN += 1.0 - dN.mean()
    res = R.power_diagnostic(dB, dN, b, draws=50)
    assert R.underpowered(res["table"]) and R.underpowered(res["table_without_condition3"])


# --- code review C1, C4, C5, C10 -----------------------------------------------------------------


def _overlap_case():
    """The reviewer's overlapping case (here 20 questions x 25 answers): 8 bad-adapter EM answers
    against none for the good one (one-sided stratified p ≈ 0.004), an equivalence interval inside ±3 points, and ΔB_v's
    upper bound below zero; v learns and the narrow difference holds, so path (ii) passes."""
    em = np.zeros(len(DEV), dtype=int)
    em[:8] = 1
    return counts(em, 25, 25, 25), counts(0, 25, 25, 25), _sc(np.zeros(len(DEV)),
                                                                np.zeros(len(DEV)))


def test_contradiction_takes_precedence_over_path_ii():
    bad, good, sc = _overlap_case()
    a = R.agreement_v("s5k", D(-0.1, -0.2, -0.05), DB_REF, True, bad, good, (0.6, 0.1), BOOT,
                      sc, DEV)
    v = a["values"]
    assert v["path_ii"]["pass"] is True  # path (ii) on its own would pass
    assert v["path_ii"]["em_diff_90"][0] > 0 and v["path_ii"]["em_diff_90"][1] < C.EQUIV_MARGIN
    assert v["path_i"]["p"] < C.DIRECTION_ALPHA and v["contradiction"] is True
    assert a["outcome"] == "fail" and "contradiction" in a["reason"]


def test_path_ii_unavailable_when_narrow_measure_unusable():
    """C4: an adapter under 50% coherent on the narrow task is unusable (§4.9), so path (ii)
    is unavailable even though its rate (over its few coherent answers) looks decisive."""
    bad, good = counts(0, 200), counts(0, 200)
    sc = _sc(np.zeros(len(DEV)), np.zeros(len(DEV)))
    args = ("s5k", D(0.01, -0.05, 0.1), DB_REF, True, bad, good, (1.0, 0.1), BOOT, sc, DEV)
    assert R.agreement_v(*args)["outcome"] == "pass"
    for nu in ((False, True), (True, False)):
        a = R.agreement_v(*args, narrow_usable=nu)
        assert a["outcome"] == "inconclusive" and "unusable" in a["reason"]
        assert a["values"]["path_ii"]["pass"] is False
        assert a["values"]["path_ii"]["narrow_ok"] is None
    # a v that does not learn fails path (ii) whatever the narrow samples say
    a = R.agreement_v("s5k", D(0.01, -0.05, 0.1), DB_REF, False, bad, good, (1.0, 0.1), BOOT,
                      sc, DEV, narrow_usable=(False, True))
    assert a["outcome"] == "fail"


def test_claim2_needs_interpretability_with_each_seed():
    R_ref = D(0.9, 0.6, 1.2)
    learns = {"2": True, "3": True}
    args = ("pass", True, "", learns)
    rest = (D(0.8, 0.65, 1.0), D(0.05, 0.0, 0.1), R_ref)
    assert R.claim2(*args, {"2": True, "3": True}, *rest)["outcome"] == "pass"
    c = R.claim2(*args, {"2": True, "3": False}, *rest)
    assert c["outcome"] == "fail" and "neighbour_interpretable" in c["reason"]
    assert c["neighbour_interpretable"] == {"2": True, "3": False}
    assert R.claim2(*args, {}, *rest)["outcome"] == "fail"


def test_em_contrast():
    bad = counts(np.r_[np.full(10, 5), np.zeros(10, int)], 20)
    good = counts(0, 20)
    out = R.em_contrast(bad, good, DEV[:10])
    assert out["n_questions"] == 10 and out["em_bad"] == pytest.approx(0.25)
    assert out["em_good"] == 0 and out["p_one_sided"] < 0.001
    assert R.em_contrast(bad, good, ["nope"]) is None
    assert R.em_contrast(None, good, DEV) is None
