"""U5 analysis: the prereg v0.5 §4.4 decision rule, the crossed control, the permutation test,
the §4.5 checks and the §6 outcome classifier, each on synthetic data with a known answer."""

import json
from pathlib import Path

import numpy as np
import pytest

from emx.i9 import analysis as A
from emx.i9 import contracts as C
from emx.i9 import predict as P

NB = 2000  # bootstrap resamples in tests


def _groups(n_groups: int, per: int) -> np.ndarray:
    return np.repeat([f"t|g{i}" for i in range(n_groups)], per)


# --- repeats, bootstrap, best baseline ----------------------------------------------------------


def test_repeats_are_averaged_before_scoring_and_bootstrap():
    rng = np.random.default_rng(0)
    groups = _groups(12, 3)
    n = np.full(36, 16)
    k = rng.binomial(16, 0.4, 36)
    rep = np.clip(rng.uniform(0.05, 0.95, (3, 36)), *C.PRED_CLIP)  # repeats disagree a lot
    base = {"b": np.full(36, 0.4)}
    a = A.primary_test({"c": rep, **base}, k, n, groups, challengers=["c"], baselines=["b"],
                       n_boot=NB)
    b = A.primary_test({"c": rep.mean(axis=0), **base}, k, n, groups, challengers=["c"],
                       baselines=["b"], n_boot=NB)
    assert a["scores"]["c"]["ll"] == pytest.approx(A.mean_ll(rep.mean(axis=0), k, n))
    per_repeat = np.mean([A.mean_ll(r, k, n) for r in rep])
    assert a["scores"]["c"]["ll"] != pytest.approx(per_repeat)  # averaging p, not averaging LL
    assert a["comparisons"]["c"] == b["comparisons"]["c"]


def test_bootstrap_resamples_whole_template_groups():
    W = A.group_weights(7, 500, 0)
    assert W.shape == (500, 7) and (W.sum(axis=1) == 7).all()
    gidx, G = A.group_index(_groups(7, 2))
    k, n = np.arange(14) % 5, np.full(14, 16)
    p = np.full(14, 0.3)
    got = A.boot_ll({"x": p}, k, n, gidx, G, W)["x"]
    terms = A.ll_terms(p, k, n)
    want = (W @ np.bincount(gidx, terms)) / (W @ np.bincount(gidx, n.astype(float)))
    np.testing.assert_allclose(got, want)


def test_best_baseline_is_rechosen_per_resample():
    groups = _groups(20, 2)
    n = np.full(40, 16)
    first = np.arange(40) < 20
    k = np.where(first, 2, 14)
    b1 = np.where(first, 0.125, 0.7)  # good on the first half only
    b2 = np.where(first, 0.3, 0.875)  # good on the second half only
    c = np.where(first, 0.2, 0.8)
    preds = {"c": c, "b1": b1, "b2": b2}
    gidx, G = A.group_index(groups)
    W = A.group_weights(G, NB, 0)
    res = A.primary_test(preds, k, n, groups, challengers=["c"], baselines=["b1", "b2"], W=W)
    share = res["best_baseline_share"]
    assert share["b1"] > 0.1 and share["b2"] > 0.1
    boot = A.boot_ll(preds, k, n, gidx, G, W)
    best = np.maximum(boot["b1"], boot["b2"])
    assert res["comparisons"]["c"]["p"] == A.two_sided_p(boot["c"] - best)
    assert (best > boot[res["best_baseline"]]).mean() > 0.1  # the fixed choice would be easier
    lo, _ = res["comparisons"]["c"]["interval"]
    assert np.quantile(boot["c"] - best, 0.025) == pytest.approx(lo)


def test_two_sided_p_value():
    assert A.two_sided_p(np.array([1.0, 2.0, -1.0, 3.0])) == pytest.approx(0.5)
    assert A.two_sided_p(np.array([1.0, 2.0, 3.0])) == 0.0
    assert A.two_sided_p(np.array([0.0, 0.0])) == 1.0


# --- Holm and the win rule ----------------------------------------------------------------------


def test_holm_step_down_pairs():
    diffs = {"a": 0.1, "b": 0.1}
    sp = {"a": True, "b": True}
    assert A.decision_from_pvalues({"a": 0.02, "b": 0.04}, diffs, sp) == {"a": True, "b": True}
    assert A.decision_from_pvalues({"a": 0.03, "b": 0.04}, diffs, sp) == {"a": False, "b": False}
    # the probe-only branch: one test at 0.05
    assert A.decision_from_pvalues({"a": 0.04}, diffs, sp) == {"a": True}


def test_positive_difference_fails_when_holm_p_too_large():
    assert A.decision_from_pvalues({"a": 0.03, "b": 0.5}, {"a": 0.2, "b": 0.0},
                                   {"a": True, "b": True})["a"] is False
    # and in the full rule: a tiny edge on one group only is positive but far from significant
    rng = np.random.default_rng(1)
    groups = _groups(16, 3)
    n = np.full(48, 16)
    truth = rng.uniform(0.1, 0.9, 48)
    k = rng.binomial(16, truth)
    base = np.clip(truth + rng.normal(0, 0.1, 48), 0.05, 0.95)
    chal = base.copy()
    chal[:3] = np.clip(base[:3] + 0.3 * (k[:3] / 16 - base[:3]), 0.05, 0.95)
    res = A.primary_test({"c": chal, "b": base}, k, n, groups, challengers=["c"],
                         baselines=["b"], n_boot=NB)
    comp = res["comparisons"]["c"]
    assert comp["diff"] > 0 and comp["p"] > 0.05 and not comp["beats"]


def test_spearman_condition_vetoes_a_loglik_win():
    groups = _groups(16, 2)
    n = np.full(32, 16)
    k = np.repeat(np.arange(16), 2)
    base = 0.45 + 0.1 * k / 15  # ranks perfectly, badly calibrated
    chal = np.where(k < 8, 0.22, 0.72)  # calibrated by level, ties within level
    res = A.primary_test({"c": chal, "b": base}, k, n, groups, challengers=["c"],
                         baselines=["b"], n_boot=NB)
    comp = res["comparisons"]["c"]
    assert comp["diff"] > 0 and comp["holm_reject"]
    assert not comp["spearman_higher"] and not comp["beats"]


def test_primary_test_finds_planted_win_with_two_primaries():
    inp, _ = P.synthetic_inputs(seed=0)
    assign = P.fold_assignments(inp.groups)
    preds = P.build_predictions(inp, assign)
    q = {name: ps.p for (name, tgt), ps in preds.items() if tgt == "qwen"}
    base = [b for b in P.PROMPT_ONLY if b in q]
    res = A.primary_test(q, inp.k["qwen"], inp.n["qwen"], inp.groups, challengers=P.PRIMARIES,
                         baselines=base, n_boot=NB)
    assert res["family"] == list(P.PRIMARIES)
    assert res["comparisons"]["probe_qwen"]["beats"]
    levels = sorted(c["interval_level"] for c in res["comparisons"].values())
    assert levels == [0.95, 0.975]
    single = A.primary_test(q, inp.k["qwen"], inp.n["qwen"], inp.groups,
                            challengers=P.PRIMARIES, baselines=base, family=["probe_qwen"],
                            n_boot=NB)
    assert single["comparisons"]["probe_qwen"]["interval_level"] == 0.95
    assert not single["comparisons"]["account_olens"]["beats"]  # outside the family


# --- crossed control ----------------------------------------------------------------------------


def _gains(qq, gq, gg, qg, sd=0.02, B=4000, seed=0):
    rng = np.random.default_rng(seed)
    means = {("qwen", "qwen"): qq, ("gemma", "qwen"): gq, ("gemma", "gemma"): gg,
             ("qwen", "gemma"): qg}
    return {key: m + sd * rng.standard_normal(B) for key, m in means.items()}


def test_crossed_control_passes_when_all_four_above_zero():
    out = A.crossed_from_gains(_gains(0.1, 0.0, 0.1, 0.0))
    assert out["dissociation"] and out["interaction_above_zero"]


@pytest.mark.parametrize(
    ("means", "failing"),
    [
        ((0.1, 0.1, 0.3, 0.0), "bracket_qwen"),
        ((0.3, 0.0, 0.1, 0.1), "bracket_gemma"),
        ((0.0, -0.2, 0.1, 0.0), "own_qwen"),
        ((0.1, 0.0, 0.0, -0.2), "own_gemma"),
    ],
)
def test_crossed_control_fails_when_one_interval_includes_zero(means, failing):
    out = A.crossed_from_gains(_gains(*means))
    assert out["interaction_above_zero"]
    assert [k for k, v in out["required_above_zero"].items() if not v] == [failing]
    lo, hi = out["intervals"][failing]
    assert lo <= 0 <= hi
    assert not out["dissociation"]


def test_crossed_control_end_to_end_on_planted_model_specific_signal():
    inp, _ = P.synthetic_inputs(seed=0)
    assign = P.fold_assignments(inp.groups)
    preds = P.build_predictions(inp, assign, targets=("qwen", "gemma"))
    probes = {(s, t): preds[(P.probe_name(s), t)].p for s in A.SOURCES for t in A.SOURCES}
    baselines = {t: {name: ps.p for (name, tgt), ps in preds.items()
                     if tgt == t and name in P.PROMPT_ONLY} for t in A.SOURCES}
    out = A.crossed_control(probes, baselines, inp.k, inp.n, inp.groups, n_boot=NB)
    assert out["dissociation"] and out["interaction_above_zero"]
    assert out["gain_point"]["qwen->qwen"] > out["gain_point"]["gemma->qwen"]


# --- permutation test ---------------------------------------------------------------------------


def test_permutation_keeps_template_groups_intact():
    groups = np.array(["a"] * 3 + ["b"] * 3 + ["c"] * 2 + ["d"] * 2 + ["e"] * 1)
    k = np.array([1, 2, 3, 10, 11, 12, 5, 6, 8, 9, 15])
    n = np.full(11, 16)
    sets = {g: sorted(k[groups == g].tolist()) for g in set(groups.tolist())}
    rng = np.random.default_rng(0)
    moved = 0
    for _ in range(50):
        k2, _ = A.permute_rates(k, n, groups, rng)
        new = {g: sorted(k2[groups == g].tolist()) for g in sets}
        for v in new.values():
            same_size = [h for h in sets if len(sets[h]) == len(v)]
            assert v in [sets[h] for h in same_size]  # a whole set from an equal-size group
        assert sorted(new.values()) == sorted(sets.values())  # a permutation of the sets
        assert new["e"] == sets["e"]  # the only group of its size stays put
        moved += new["a"] != sets["a"]
    assert moved > 10


def _gain_stat(inp, assign):
    def stat(k, n):
        probe = P.cross_fit("p", P.logistic_fn(inp.acts["qwen"], inp.groups, k, n, Cs=(1e-3, 1e-2)),
                            assign, k, n, ids=inp.ids, transform="identity")
        emb = P.cross_fit("e", P.logistic_fn({0: inp.embeddings}, inp.groups, k, n,
                                             Cs=(1e-3, 1e-2)),
                          assign, k, n, ids=inp.ids, transform="identity")
        return A.mean_ll(probe.p, k, n) - A.mean_ll(emb.p, k, n)

    return stat


def test_permutation_test_planted_signal_passes_shuffled_fails():
    inp, _ = P.synthetic_inputs(seed=0, d=12)
    assign = P.fold_assignments(inp.groups)[:1]
    stat = _gain_stat(inp, assign)
    real = A.permutation_test(stat, inp.k["qwen"], inp.n["qwen"], inp.groups, n_perm=19)
    assert real.passed and real.name == "permutation" and len(real.details["null"]) == 19
    k_shuf = np.random.default_rng(5).permutation(inp.k["qwen"])
    null = A.permutation_test(stat, k_shuf, inp.n["qwen"], inp.groups, n_perm=19)
    assert not null.passed


# --- §4.5 checks --------------------------------------------------------------------------------


def _match(ours_k):
    ids = [f"b{i}" for i in range(14)]
    return A.check_sampling_match({i: (k, 16) for i, k in zip(ids, ours_k, strict=True)},
                                  dict.fromkeys(ids, 0.5))


def test_sampling_match_thresholds():
    ok = _match([16, 16] + [10] * 12)  # 2 big diffs (0.5), MAD (1 + 12 x 0.125) / 14 = 0.18
    assert ok.passed and ok.details["n_big_diff"] == 2 and ok.value == pytest.approx(2.5 / 14)
    too_many = _match([16, 16, 16] + [8] * 11)  # 3 big diffs, MAD 0.107
    assert not too_many.passed and too_many.value <= C.SAMPLING_MAD_MAX
    mad = _match([12] * 14)  # MAD 0.25, no big diffs
    assert not mad.passed and mad.details["n_big_diff"] == 0
    assert all(0 <= v["fisher_p"] <= 1 for v in ok.details["per_scenario"].values())


def test_spread_controls_and_model_difference():
    n = np.full(10, 16)
    assert A.check_spread(np.array([8, 8, 8, 0, 0, 0, 16, 16, 16, 16]), n).passed  # 3/10
    assert not A.check_spread(np.array([8, 8, 0, 0, 0, 0, 16, 16, 16, 16]), n).passed
    assert not A.check_spread(np.array([8, 8, 1] + [0] * 7), n).passed  # 1/16 is not > 0.1
    assert A.check_controls({"c1": (2, 16), "c2": (0, 16), "c3": (1, 16), "c4": (2, 16)}).passed
    assert not A.check_controls({"c1": (3, 16), "c2": (0, 16), "c3": (0, 16),
                                 "c4": (0, 16)}).passed
    k = np.arange(10)
    assert not A.check_model_difference(k, n, k, n).passed  # identical ranks
    assert A.check_model_difference(k, n, k[::-1], n).passed


def test_check_result_writes(tmp_path: Path):
    r = A.check_spread(np.array([8] * 5), np.full(5, 16))
    p = r.write(tmp_path / "spread.json")
    assert '"passed": true' in p.read_text()


# --- §6 outcome classifier -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "want"),
    [
        ({"checks_passed": False, "probe_beats": True, "dissociation": True}, "checks_fail"),
        ({"probe_beats": True, "dissociation": True, "interaction_above_zero": True},
         "model_specific_signal"),
        ({"olens_beats": True, "dissociation": True, "interaction_above_zero": True},
         "model_specific_signal"),
        ({"probe_beats": True}, "edge_without_specificity"),
        ({"probe_beats": True, "interaction_above_zero": True}, "probe_only"),
        ({"probe_beats": True, "interaction_above_zero": True, "olens_beats": True},
         "inconclusive"),
        ({"power": 0.85}, "prompt_gives_it_away"),
        # r3 finding 2: below 0.80 (or not computable) nothing positive or null is emitted
        ({"power": 0.6}, A.UNDERPOWERED),
        ({"power": None}, A.UNDERPOWERED),
        ({"power": 0.79, "probe_beats": True, "dissociation": True,
          "interaction_above_zero": True}, A.UNDERPOWERED),
        ({"power": None, "probe_beats": True}, A.UNDERPOWERED),
        ({"power": 0.5, "checks_passed": False}, "checks_fail"),
        ({"power": 0.9, "other_reader_beats": True}, "inconclusive"),
        # a failed model-difference check: the crossed control cannot separate the models
        ({"probe_beats": True, "dissociation": True, "interaction_above_zero": True,
          "models_differ": False}, "edge_without_specificity"),
        ({"olens_beats": True, "dissociation": True, "interaction_above_zero": True,
          "models_differ": False}, "edge_without_specificity"),
    ],
)
def test_outcome_classifier(kw, want):
    base = {"checks_passed": True, "probe_beats": False, "olens_beats": False,
            "dissociation": False, "interaction_above_zero": False, "power": 0.9,
            "models_differ": True}
    assert A.classify_outcome(**{**base, **kw}) == want


def test_outcome_classifier_requires_model_difference_status():
    with pytest.raises(TypeError):
        A.classify_outcome(checks_passed=True, probe_beats=True, olens_beats=False,  # type: ignore[call-arg]
                           dissociation=True, interaction_above_zero=True, power=0.9)


# --- label completeness and the fail-closed gates ------------------------------------------------


def test_label_completeness():
    """All 16 replies of every confirmatory scenario labelled, for each model (amendment B,
    round 2: replies unresolved after the re-judge are adjudicated by Jacob)."""
    ids = [f"s{i}" for i in range(10)]
    full = {"qwen": np.full(10, 16), "gemma": np.full(10, 16)}
    r = A.check_label_completeness(full, ids)
    assert r.passed and r.value == 1.0 and r.threshold == 1.0
    one_short = {**full, "gemma": np.array([16] * 9 + [15])}  # a single unlabelled reply fails
    r = A.check_label_completeness(one_short, ids)
    assert not r.passed and r.details["by_model"]["gemma"]["not_full"] == {"s9": 15}
    assert r.details["by_model"]["qwen"]["not_full"] == {}
    assert not A.check_label_completeness({}, ids).passed
    assert not A.check_label_completeness({"qwen": np.array([], dtype=int)}, []).passed


def _reader(docs):
    return lambda name: docs.get(name)


def test_blocking_checks_fail_closed_when_absent():
    passing = {c: {"passed": True} for c in A.BLOCKING_CHECKS}
    ok, status, missing = A.blocking_status(_reader(passing))
    assert ok and not missing
    ok, status, missing = A.blocking_status(_reader({}))  # nothing on disk: fails
    assert not ok and missing == list(A.BLOCKING_CHECKS)
    drop = {k: v for k, v in passing.items() if k != "handlabel_kappa"}
    ok, status, missing = A.blocking_status(_reader(drop))
    assert not ok and missing == ["handlabel_kappa"] and status["handlabel_kappa"] is None
    ok, status, _ = A.blocking_status(_reader({**passing, "spread": {"passed": None}}))
    assert not ok and status["spread"] is False


def test_blocking_check_names_match_their_producers():
    """The gate reads the files the producers write: pod smoke (smoke_<name>: the I1a
    reproduction and prompt_scope), hand labels (judging.score_handlabels), checkpoint2."""
    import re

    root = Path(__file__).resolve().parents[2]
    pod = (root / "scripts" / "i9_pod.py").read_text()
    judging = (root / "src" / "emx" / "i9" / "judging.py").read_text()
    assert 'f"smoke_{r.name}"' in pod and 'name="prompt_scope"' in pod  # -> smoke_prompt_scope
    acts_src = (root / "src" / "emx" / "i9" / "acts.py").read_text()
    assert 'I1A_REPRO_NAME = "smoke_i1a_reproduction"' in acts_src
    assert 'name="handlabel_kappa"' in judging
    assert {"smoke_i1a_reproduction", "smoke_prompt_scope", "handlabel_kappa", "sampling_match",
            "spread", "controls", "label_completeness"} == set(A.BLOCKING_CHECKS)
    produced = set(re.findall(r'name="(validation_[a-z_]+)"', judging))
    produced |= set(re.findall(r'wkappa_check\("(validation_[a-z_]+)"', judging))
    assert {*A.OLENS_GATE_COMPONENTS, A.OLENS_GATE_OVERALL} == produced


def _gate_docs(**override):
    docs = {c: {"passed": True} for c in A.OLENS_GATE_COMPONENTS}
    docs[A.OLENS_GATE_OVERALL] = {"passed": True,
                                  "details": dict.fromkeys(A.OLENS_GATE_COMPONENTS, True)}
    docs.update(override)
    return {k: v for k, v in docs.items() if v is not None}


def test_olens_gate_reads_overall_and_its_five_components():
    assert A.olens_gate_status(_reader(_gate_docs()))["gate"] == "pass"
    absent = A.olens_gate_status(_reader({}))
    assert absent["gate"] == "fail" and A.OLENS_GATE_OVERALL in absent["missing"]
    one_missing = A.olens_gate_status(_reader(_gate_docs(validation_same_action=None)))
    assert one_missing["gate"] == "fail" and one_missing["missing"] == ["validation_same_action"]
    comp_failed = _gate_docs(validation_same_family={"passed": False})
    comp_failed[A.OLENS_GATE_OVERALL] = {
        "passed": False, "details": {**dict.fromkeys(A.OLENS_GATE_COMPONENTS, True),
                                     "validation_same_family": False}}
    r = A.olens_gate_status(_reader(comp_failed))
    assert r["gate"] == "fail" and r["reason"] == "a gate component failed"
    # an overall that says pass while a component file says fail is inconsistent: fail
    stale = _gate_docs(validation_same_family={"passed": False})
    r = A.olens_gate_status(_reader(stale))
    assert r["gate"] == "fail" and "disagrees" in r["reason"]


def test_plot_writes(tmp_path: Path):
    rng = np.random.default_rng(0)
    k = rng.integers(0, 17, 20)
    out = A.plot_predicted_vs_observed({"a": rng.uniform(size=(3, 20)), "b": rng.uniform(size=20)},
                                       k, np.full(20, 16), tmp_path / "x.png")
    assert out.exists() and out.stat().st_size > 1000


# --- the final step's fail-closed gates (scripts/i9_analyze.py, review r1 findings 4-8) ----------


def _analyze():
    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("i9_analyze", root / "scripts" / "i9_analyze.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["i9_analyze"] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


FAST = ["--n-boot", "2000", "--n-perm", "20"]


@pytest.fixture(scope="module")
def syn_run(tmp_path_factory):
    """One synthetic checkpoint2 + predict + final run (planted model-specific signal)."""
    an = _analyze()
    out = tmp_path_factory.mktemp("syn") / "I9"
    assert an.main(["--synthetic", "--out", str(out), "all", "--n-sims", "5", *FAST]) == 0
    first = json.loads((out / "analysis" / "final.json").read_text())
    # the synthetic data's simulated power is not computable (max Spearman < baseline + 0.3):
    # the power gate makes that run descriptive only (r3 finding 2) ...
    assert first["status"] == "complete" and first["outcome"] == A.UNDERPOWERED
    assert first["power_gate"]["passed"] is False and "descriptive only" in first[
        "descriptive_only"]
    # ... so the copies the tests rerun stand in a passing power (as a rerun checkpoint2 would)
    _set_power(out, 0.9, 0.9)
    return an, out


def _set_power(root, power, probe_only, **extra):
    """Overwrite analysis/power.json's powers and restamp its producing manifest."""
    path = root / "analysis" / "power.json"
    pj = json.loads(path.read_text())
    pj.update(power=power, power_probe_only=probe_only, feasible=power is not None, **extra)
    path.write_text(json.dumps(pj, indent=2, sort_keys=True) + "\n")
    _restamp(root, path)


def _restamp(root, *paths):
    """Record edited files' new sha256 in the manifests that list them (a test edit stands in
    for a rerun of the producing step, so the staleness check passes)."""
    import hashlib
    import json

    for m in (root / "manifests").glob("*.json"):
        d = json.loads(m.read_text())
        for o in d["outputs"]:
            for p in paths:
                if Path(o["path"]).parts[-2:] == p.parts[-2:]:
                    o["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
        m.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")


def _final_again(syn_run, tmp_path, edit):
    """Copy the synthetic run, apply ``edit(root)``, rerun ``final`` only."""
    import json
    import shutil

    an, src = syn_run
    root = tmp_path / "I9"
    shutil.copytree(src, root)
    edit(root)
    rc = an.main(["--synthetic", "--out", str(root), "final", *FAST])
    return rc, json.loads((root / "analysis" / "final.json").read_text())


def test_final_complete_run_classifies(syn_run, tmp_path):
    rc, res = _final_again(syn_run, tmp_path, lambda root: None)
    assert rc == 0 and res["status"] == "complete" and not res["missing_inputs"]
    assert res["outcome"] == "model_specific_signal" and res["olens_gate"] == "pass"
    assert res["baselines"] == list(P.PROMPT_ONLY) and res["model_difference"] is True
    assert res["permutation"]["n_perm"] == 20 and not res["missing_checks"]


def test_final_missing_blocking_check_is_checks_fail(syn_run, tmp_path):
    _, res = _final_again(syn_run, tmp_path,
                           lambda root: (root / "checks" / "smoke_prompt_scope.json").unlink())
    assert res["outcome"] == "checks_fail" and res["missing_checks"] == ["smoke_prompt_scope"]
    assert res["checks"]["smoke_prompt_scope"] is None


def test_final_missing_baseline_is_incomplete_not_an_outcome(syn_run, tmp_path):
    def drop_gemma_few(root):
        path = root / "predictions" / f"{P.MONITOR_FEW}_calibrated.jsonl"
        rows = [r for r in P.read_predictions(path) if r.target_model != "gemma"]
        P.write_predictions(rows, path)
        _restamp(root, path)

    rc, res = _final_again(syn_run, tmp_path, drop_gemma_few)
    assert rc == 3 and res["status"] == "incomplete" and res["outcome"] is None
    assert res["missing_inputs"] == [f"{P.MONITOR_FEW}->gemma"]
    assert "primary" not in res


def test_final_missing_probe_input_is_incomplete(syn_run, tmp_path):
    rc, res = _final_again(syn_run, tmp_path, lambda root: (
        root / "predictions" / f"{P.probe_name('gemma')}.jsonl").unlink())
    assert rc == 3 and res["outcome"] is None
    assert set(res["missing_inputs"]) == {f"{P.probe_name('gemma')}->qwen",
                                          f"{P.probe_name('gemma')}->gemma"}


def test_final_without_validation_gate_runs_probe_only_branch(syn_run, tmp_path):
    def no_gate(root):
        (root / "checks" / f"{A.OLENS_GATE_OVERALL}.json").unlink()
        (root / "predictions" / f"{P.account_name('olens')}.jsonl").unlink()  # not required now

    rc, res = _final_again(syn_run, tmp_path, no_gate)
    assert rc == 0 and res["status"] == "complete" and res["olens_gate"] == "fail"
    assert res["olens_primary"].startswith("exploratory")
    assert res["olens_gate_status"]["missing"] == [A.OLENS_GATE_OVERALL]
    assert res["primary"]["family"] == [P.PRIMARIES[0]]


def test_final_failed_model_difference_cannot_claim_specificity(syn_run, tmp_path):
    def fail_md(root):
        path = root / "checks" / "model_difference.json"
        A.check_model_difference(np.arange(10), np.full(10, 16), np.arange(10),
                                 np.full(10, 16)).write(path)
        _restamp(root, path)

    _, res = _final_again(syn_run, tmp_path, fail_md)
    assert res["model_difference"] is False and "unable to separate" in res["crossed_control_note"]
    assert res["crossed"]["able_to_separate_models"] is False
    assert res["outcome"] == "edge_without_specificity"  # was model_specific_signal


def test_final_failed_permutation_vetoes_the_probe(syn_run, tmp_path, monkeypatch):
    an, _ = syn_run
    real = A.permutation_test

    def failing(*a, **kw):
        r = real(*a, **kw)
        r.passed = False
        return r

    monkeypatch.setattr(an.A, "permutation_test", failing)
    _, res = _final_again(syn_run, tmp_path, lambda root: None)
    assert res["primary"]["comparisons"][P.PRIMARIES[0]]["beats"]  # it would have won
    assert res["outcome"] not in ("model_specific_signal", "edge_without_specificity",
                                  "probe_only")


@pytest.mark.parametrize("argv", [["final", "--n-perm", "2"], ["all", "--n-perm", "199"],
                                  ["final", "--olens-gate", "pass"]])
def test_real_final_refuses_short_permutations_and_gate_override(syn_run, argv, capsys):
    an, _ = syn_run
    with pytest.raises(SystemExit) as e:
        an.main(argv)
    assert e.value.code == 2 and "only with --synthetic" in capsys.readouterr().err


# --- staleness (review r2 finding 3) -------------------------------------------------------------


def test_final_refuses_a_hand_edited_check(syn_run, tmp_path):
    def edit(root):  # a passing spread check that no manifest produced
        path = root / "checks" / "spread.json"
        path.write_text(path.read_text().replace('"n": 64', '"n": 65'))

    rc, res = _final_again(syn_run, tmp_path, edit)
    assert rc == 4 and res["status"] == "stale" and res["outcome"] is None
    assert len(res["stale"]) == 1 and "checks/spread.json" in res["stale"][0]
    assert "no manifest records" in res["stale"][0]


# --- the power gate (code review r3 finding 2) ----------------------------------------------------


def test_final_without_power_is_incomplete(syn_run, tmp_path):
    rc, res = _final_again(syn_run, tmp_path,
                           lambda root: (root / "analysis" / "power.json").unlink())
    assert rc == 3 and res["status"] == "incomplete" and res["outcome"] is None
    assert any("analysis/power.json" in x for x in res["missing_inputs"])
    assert res["power_gate"]["passed"] is False and "absent" in res["power_gate"]["note"]


def test_final_with_unrecorded_power_is_stale(syn_run, tmp_path):
    def edit(root):  # a passing power.json no manifest produced
        path = root / "analysis" / "power.json"
        path.write_text(path.read_text().replace('"power": 0.9', '"power": 0.95'))

    rc, res = _final_again(syn_run, tmp_path, edit)
    assert rc == 4 and res["status"] == "stale" and res["outcome"] is None
    assert any("analysis/power.json" in x for x in res["stale"])


@pytest.mark.parametrize(("power", "probe_only", "gate_fail"),
                         [(0.5, 0.95, False), (None, None, False), (0.95, 0.5, True)],
                         ids=["holm-low", "not-computable", "probe-only-low"])
def test_final_underpowered_emits_no_positive_outcome(syn_run, tmp_path, power, probe_only,
                                                      gate_fail):
    def edit(root):
        _set_power(root, power, probe_only)
        if gate_fail:  # the probe-only branch: its own power applies
            (root / "checks" / f"{A.OLENS_GATE_OVERALL}.json").unlink()

    rc, res = _final_again(syn_run, tmp_path, edit)
    assert rc == 0 and res["status"] == "complete"
    assert res["outcome"] == res["outcome_unqualified"] == A.UNDERPOWERED
    assert res["primary"]["comparisons"][P.PRIMARIES[0]]["beats"]  # it would have won
    g = res["power_gate"]
    assert g["branch"] == ("probe_only" if gate_fail else "holm") and g["passed"] is False
    assert g["power"] == (probe_only if gate_fail else power)
    assert "descriptive only" in res["descriptive_only"]


def test_final_probe_only_branch_uses_its_own_power(syn_run, tmp_path):
    def edit(root):  # Holm power low, probe-only power high, gate failed: the latter applies
        _set_power(root, 0.5, 0.9)
        (root / "checks" / f"{A.OLENS_GATE_OVERALL}.json").unlink()

    _, res = _final_again(syn_run, tmp_path, edit)
    assert res["power_gate"]["branch"] == "probe_only" and res["power_gate"]["passed"]
    assert res["outcome"] != A.UNDERPOWERED and "descriptive_only" not in res


def _args(**kw):
    import argparse

    return argparse.Namespace(**{"n_sims": 3, "n_boot": 200, "unsealed": False, **kw})


def test_checks_from_64_scenarios_are_stale_after_unsealing_to_80(tmp_path):
    from dataclasses import replace

    an = _analyze()
    b64 = an.load_synthetic(tmp_path / "I9")
    an.write_synthetic_external_checks(b64)
    an.step_checkpoint2(b64, _args())
    an.step_predict(b64, _args())
    assert an.stale_artifacts(b64) == []  # a fresh run passes
    inp80, _ = P.synthetic_inputs(seed=0, n_scen=80)
    assert inp80.ids[:64] == b64.inp.ids
    b80 = replace(b64, inp=inp80, unsealed=True, llm_only=None)
    stale = an.stale_artifacts(b80)
    msg = "ran on 64 scenarios (unsealed=False); this run's confirmatory set has 80 (unsealed=True)"
    assert any("checks/spread.json" in x and msg in x for x in stale)
    assert any("analysis/power.json" in x for x in stale)
    assert any("predictions/probe_qwen.jsonl" in x and "covers 64 scenarios" in x for x in stale)
    an.step_checkpoint2(b80, _args(unsealed=True))  # rerun on the 80: fresh again
    an.step_predict(b80, _args(unsealed=True))
    assert an.stale_artifacts(b80) == []


def test_checks_made_from_other_rates_are_stale(tmp_path, monkeypatch):
    an = _analyze()
    b = an.load_synthetic(tmp_path / "I9")
    files = {}
    for name in ("SCENARIOS_PATH", "SETS_PATH", "RATES_PATH"):
        files[name] = tmp_path / "data" / getattr(C, name).name
        files[name].parent.mkdir(exist_ok=True)
        files[name].write_text(f"{name} v1\n")
        monkeypatch.setattr(C, name, files[name])
    b.synthetic, b.inputs_used = False, list(files.values())
    an.write_synthetic_external_checks(b)
    an.step_checkpoint2(b, _args())
    assert an.stale_artifacts(b) == []
    files["RATES_PATH"].write_text("RATES_PATH v2\n")  # relabelled after the checks ran
    stale = an.stale_artifacts(b)
    assert stale and all("rates.jsonl at sha256" in x for x in stale)
    assert any("checks/spread.json: step check read" in x for x in stale)
    # a check step that did not record the rates at all is refused too
    b.inputs_used = [files["SCENARIOS_PATH"], files["SETS_PATH"]]
    an.step_checkpoint2(b, _args())
    b.inputs_used.append(files["RATES_PATH"])
    assert any("records no ['rates.jsonl'] input" in x for x in an.stale_artifacts(b))


# --- sensitivity analyses (prereg amendment B) ---------------------------------------------------


def _claims(outcome, probe=False, olens=False, crossed=False):
    return {"outcome": outcome, "primary": {"probe": probe, "olens": olens}, "crossed": crossed}


def test_sensitivity_verdict_and_qualified_outcome():
    main = _claims("model_specific_signal", probe=True, crossed=True)
    same = A.sensitivity_verdict(main, {"v": dict(main)}, A.SENSITIVE_LABEL_RULE)
    assert not same["changed"] and not same["withhold_outcome"]
    assert A.qualify_outcome("model_specific_signal", [same]) == ("model_specific_signal", [])
    lost = A.sensitivity_verdict(main, {"v": _claims("inconclusive")}, A.SENSITIVE_LABEL_RULE)
    assert lost["withhold_outcome"] and lost["primary_claim"] == A.SENSITIVE_LABEL_RULE
    assert lost["crossed_claim"] == A.SENSITIVE_LABEL_RULE
    out, why = A.qualify_outcome("model_specific_signal", [same, lost])
    assert out == A.SENSITIVE_LABEL_RULE and len(why) == 1
    # the crossed claim alone flips while the outcome stands: only that claim is withheld
    null = _claims("prompt_gives_it_away")
    cr = A.sensitivity_verdict(null, {"v": {**null, "crossed": True}}, A.SENSITIVE_PLACEHOLDER)
    assert cr["crossed_claim"] == A.SENSITIVE_PLACEHOLDER and cr["primary_claim"] == "robust"
    assert A.qualify_outcome("prompt_gives_it_away", [cr]) == ("prompt_gives_it_away", [])
    # a null that turns into a win is withheld as well (no positive claim either way)
    win = A.sensitivity_verdict(null, {"v": _claims("probe_only", probe=True)},
                                A.SENSITIVE_PLACEHOLDER)
    assert A.qualify_outcome("prompt_gives_it_away", [win])[0] == A.SENSITIVE_PLACEHOLDER
    # both labels apply: the first in the given order is reported, both are listed
    out, why = A.qualify_outcome("probe_only", [lost, win])
    assert out == A.SENSITIVE_LABEL_RULE and len(why) == 2
    assert A.qualify_outcome("checks_fail", [lost]) == ("checks_fail", [])


def _final_with(syn_run, tmp_path, monkeypatch, tweak):
    """``final`` on a copy of the synthetic run with ``tweak(bundle)`` applied after loading."""
    an, _ = syn_run
    real = an.load_synthetic

    def load(out):
        b = real(out)
        tweak(b)
        return b

    monkeypatch.setattr(an, "load_synthetic", load)
    return _final_again(syn_run, tmp_path, lambda root: None)


def test_label_rule_sensitivity_robust_case(syn_run, tmp_path):
    _, res = _final_again(syn_run, tmp_path, lambda root: None)
    s = res["sensitivity"]["llm_only_labels"]
    assert s["applies"] and not s["identical_to_main"]
    assert s["n_scenarios_counts_differ"] == {"qwen": 2, "gemma": 2}
    assert s["verdict"]["primary_claim"] == s["verdict"]["crossed_claim"] == "robust"
    assert res["outcome"] == res["outcome_unqualified"] == "model_specific_signal"
    assert not res["outcome_reasons"] and res["sensitivity"]["n_boot"] == 2000
    assert not res["sensitivity"]["without_unfilled_placeholder"]["applies"]  # 64: no placeholder


def test_label_rule_sensitivity_flips_and_withholds(syn_run, tmp_path, monkeypatch):
    def scramble(b):  # LLM-only labels unrelated to anything the predictors see
        rng = np.random.default_rng(7)
        b.llm_only = {m: (rng.binomial(16, 0.5, len(b.inp.ids)), np.full(len(b.inp.ids), 16))
                      for m in ("qwen", "gemma")}

    rc, res = _final_with(syn_run, tmp_path, monkeypatch, scramble)
    assert rc == 0 and res["outcome_unqualified"] == "model_specific_signal"
    v = res["sensitivity"]["llm_only_labels"]["verdict"]
    assert v["primary_claim"] == A.SENSITIVE_LABEL_RULE and v["withhold_outcome"]
    assert res["outcome"] == A.SENSITIVE_LABEL_RULE and res["outcome_reasons"]
    assert any("withheld" in x for x in res["sensitivity"]["summary"])


def _placeholder(monkeypatch, an, ids):
    monkeypatch.setattr(an, "KNOWN_UPSTREAM_UNFILLED", {i: {"${x}"} for i in ids})


def test_placeholder_sensitivity_robust_case(syn_run, tmp_path, monkeypatch):
    an, _ = syn_run
    _placeholder(monkeypatch, an, ["syn-003"])
    _, res = _final_again(syn_run, tmp_path, lambda root: None)
    s = res["sensitivity"]["without_unfilled_placeholder"]
    assert s["applies"] and s["n_scenarios"] == 63 and "syn-003" in s["note"]
    assert s["verdict"]["withhold_outcome"] is False
    assert res["outcome"] == "model_specific_signal"


def test_placeholder_sensitivity_flips_and_withholds(syn_run, tmp_path, monkeypatch):
    """Planted: the excluded scenarios carry the result (here 44 of 64 are "unfilled"; on the
    other 20 the crossed control loses its dissociation), so the outcome is withheld."""
    an, _ = syn_run
    _placeholder(monkeypatch, an, [f"syn-{i:03d}" for i in range(20, 64)])
    _, res = _final_again(syn_run, tmp_path, lambda root: None)
    s = res["sensitivity"]["without_unfilled_placeholder"]
    assert s["applies"] and s["n_scenarios"] == 20 and s["run"]["outcome"] == "probe_only"
    assert s["verdict"]["crossed_claim"] == A.SENSITIVE_PLACEHOLDER
    assert s["verdict"]["withhold_outcome"]
    assert res["outcome"] == A.SENSITIVE_PLACEHOLDER
    assert res["outcome_unqualified"] == "model_specific_signal"


# --- the unsealed 80-scenario path (code review r3 finding 1) --------------------------------------


def _unsealed_tree(tmp_path, monkeypatch):
    """A real-layout tree of 64 confirmatory + 16 sealed scenarios (synthetic counts and
    activations), 6 bank items, rates, LLM-only rates and activation stand-ins; returns
    (inputs, conf ids, sealed ids, scenarios path)."""
    import hashlib

    from emx.i9 import judging as J

    inp, _ = P.synthetic_inputs(seed=0, n_scen=80)
    ids = list(inp.ids)
    conf, sealed = ids[:64], ids[64:]
    bank = [f"bank-{i}" for i in range(6)]

    def scen(sid, set_, i=None, arm="misaligned", pub=None):
        cat = str(inp.categories[i]) if i is not None else "blackmail"
        tg = str(inp.groups[i]) if i is not None else f"bank|{sid}"
        typ = str(inp.types[i]) if i is not None else "blackmail"
        return C.Scenario(id=sid, source="generator", set=set_, category=cat, arm=arm,
                          group="anthropic", template_group=tg, scenario_type=typ, system="s",
                          text=f"text {sid}", descriptor="d", lynch_conditions=None,
                          published_rate=pub, pinned_rollout="")

    rows = [scen(s, "confirmatory" if s in conf else "sealed", i) for i, s in enumerate(ids)]
    rows += [scen(b, "bank", arm="control" if j >= 4 else "misaligned",
                  pub=None if j >= 4 else 0.5) for j, b in enumerate(bank)]
    data = tmp_path / "data"
    data.mkdir()
    scen_p, sets_p = data / "scenarios.jsonl", data / "sets.json"
    scen_p.write_text("".join(C.to_json(r) + "\n" for r in rows))
    sets_p.write_text(json.dumps({"bank": bank, "bank_sampled": bank, "bank_validation": [],
                                  "confirmatory": conf, "sealed": sealed, "thinking": []}))
    monkeypatch.setattr(C, "SCENARIOS_PATH", scen_p)
    monkeypatch.setattr(C, "SETS_PATH", sets_p)
    rates = [C.Rate(m, False, s, int(inp.k[m][i]), 16, {}) for m in ("qwen", "gemma")
             for i, s in enumerate(ids)]
    rates += [C.Rate("qwen", False, b, 8 if j < 4 else 0, 16, {}) for j, b in enumerate(bank)]
    J.write_rows(C.RATES_PATH, rates)
    J.write_rows(C.RATES_LLM_ONLY_PATH, rates)

    def fake_embed(texts, **kw):  # no embedding model download in tests
        out = np.array([[int(hashlib.sha256(f"{t}|{j}".encode()).hexdigest()[:8], 16) / 2**32
                         for j in range(8)] for t in texts], dtype=np.float32)
        return out / np.linalg.norm(out, axis=1, keepdims=True)

    monkeypatch.setattr(P, "embed_texts", fake_embed)
    rng = np.random.default_rng(3)
    vec = {m: {L: {**{s: X[i] for i, s in enumerate(ids)},
                   **{b: rng.standard_normal(X.shape[1]) for b in bank}}
               for L, X in inp.acts[m].items()} for m in ("qwen", "gemma")}
    for m in ("qwen", "gemma"):  # the stores only need to exist; loading is faked
        C.ACTS_DIR.mkdir(parents=True, exist_ok=True)
        (C.ACTS_DIR / f"{m}_last.safetensors").write_bytes(m.encode())
        (C.ACTS_DIR / f"{m}_last.json").write_text("{}")

    def fake_load(store, index, want, layers):
        m = "qwen" if Path(store).name.startswith("qwen") else "gemma"
        return {L: np.stack([v[s] for s in want]) for L, v in vec[m].items()}

    monkeypatch.setattr(P, "load_last_acts", fake_load)
    return inp, conf, sealed, scen_p


def _stage_c_part(path, inp, part):
    pos = {s: i for i, s in enumerate(inp.ids)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rows": [
        {"id": s, "account": f"account of {s}", "commit_strength": float(inp.commit["olens"][pos[s]])}
        for s in part]}))
    return path


def test_unsealed_80_path_monitors_stage_c_and_final(tmp_path, monkeypatch):
    """r3 finding 1, synthetic 64 + 16: checkpoint2 --unsealed (80-scenario folds); the few-shot
    monitor on --set confirmatory,sealed in one run; the zero-shot monitor on the confirmatory
    set, then on the sealed set merged by id; Stage C from a confirmatory and a sealed file per
    reader. With a sealed part missing, final is "incomplete"; with all parts, it runs on 80."""
    import argparse
    import hashlib

    from emx.i9 import judging as J

    from .test_judge_cli import PaidFake, _cli

    inp, conf, sealed, scen_p = _unsealed_tree(tmp_path, monkeypatch)
    an, cli = _analyze(), _cli()

    def ns(**kw):
        base = {"unsealed": True, "acts": [], "stage_c": [], "n_sims": 3, "n_boot": 200,
                "n_perm": 20, "olens_gate": None}
        return argparse.Namespace(**{**base, **kw})

    b = an.load_real(ns(), need_reader_inputs=False)
    an.write_synthetic_external_checks(b)  # passing stand-ins for the pod/hand-label/validation
    an.step_checkpoint2(b, ns())
    folds = json.loads(C.FOLDS_PATH.read_text())
    assert all(set(f) == set(conf + sealed) for f in folds.values())

    def prob(route, system, user):
        h = int(hashlib.sha256(user.encode()).hexdigest()[:6], 16) / 16**6
        return json.dumps({"probability": round(0.05 + 0.9 * h, 3)})

    monkeypatch.setattr(J, "default_complete", lambda: PaidFake(prob))
    common = ["--scenarios", str(scen_p), "--confirm-paid"]
    few = ["--folds", str(C.FOLDS_PATH), "--rates", str(C.RATES_PATH)]
    for t in ("qwen", "gemma"):
        # the 80-scenario folds refuse a confirmatory-only few-shot run (no calls) ...
        assert cli.main(["monitor-few", "--target", t, *few, *common]) == 2
        # ... and run on the combined set in one go
        assert cli.main(["monitor-few", "--target", t, "--set", "confirmatory,sealed",
                         *few, *common]) == 0
        assert cli.main(["monitor-zero", "--target", t, *common]) == 0  # the 64 only, first
    man = json.loads(C.manifest_path("monitor_few_qwen_confirmatory_sealed").read_text())
    assert json.loads(man["notes"])["set"] == "confirmatory,sealed"
    rows = P.read_predictions(C.predictions_path(P.MONITOR_FEW))
    assert {(r.target_model, r.scenario_id) for r in rows} == {
        (t, s) for t in ("qwen", "gemma") for s in conf + sealed}
    assert len(rows) == 2 * 80 * len(C.FOLD_SEEDS)

    # a sealed part missing (zero-shot not yet run on it; Stage C of the confirmatory set only)
    conf_c = _stage_c_part(tmp_path / "judged" / "conf" / "results.json", inp, conf)
    sealed_c = _stage_c_part(tmp_path / "judged" / "sealed" / "results.json", inp, sealed)
    b = an.load_real(ns(stage_c=[f"olens={conf_c}"]), need_reader_inputs=True)
    assert "olens" not in b.inp.commit and (P.MONITOR_ZERO, "qwen") not in b.inp.monitor_raw
    an.step_predict(b, ns())
    res = an.step_final(b, ns())
    assert res["status"] == "incomplete" and res["outcome"] is None and res["n_scenarios"] == 80
    assert {f"{P.MONITOR_ZERO}->qwen", f"{P.MONITOR_ZERO}->gemma",
            f"{P.account_name('olens')}->qwen"} <= set(res["missing_inputs"])

    # the sealed parts: zero-shot on the sealed set merged by id, both Stage C files
    zero_before = J.sha256_file(C.predictions_path(P.MONITOR_ZERO))
    for t in ("qwen", "gemma"):
        assert cli.main(["monitor-zero", "--target", t, "--set", "sealed", *common]) == 0
    zrows = P.read_predictions(C.predictions_path(P.MONITOR_ZERO))
    assert sorted((r.target_model, r.scenario_id) for r in zrows) == sorted(
        (t, s) for t in ("qwen", "gemma") for s in conf + sealed)
    zman = json.loads(C.manifest_path("monitor_zero_qwen_sealed").read_text())
    assert json.loads(zman["notes"])["set"] == "sealed"
    assert any(i["path"].endswith("monitor_zero_shot.jsonl") and i["sha256"] == zero_before
               for i in zman["inputs"])  # the pre-merge file is a recorded input
    stage_c = [f"olens={conf_c}", f"olens={sealed_c}"]
    with pytest.raises(ValueError, match="duplicate Stage C id"):
        an.load_real(ns(stage_c=[*stage_c, f"olens={conf_c}"]), need_reader_inputs=True)
    b = an.load_real(ns(stage_c=stage_c), need_reader_inputs=True)
    assert b.inp.commit["olens"].shape == (80,)
    assert conf_c in b.inputs_used and sealed_c in b.inputs_used
    an.step_predict(b, ns())
    res = an.step_final(b, ns())
    assert res["status"] == "complete" and not res["missing_inputs"], res.get("missing_inputs")
    assert res["n_scenarios"] == 80 and res["unsealed"] is True
    assert res["outcome"] in A.OUTCOMES and res["power_gate"]["branch"] == "holm"
    pred_man = json.loads((C.RESULTS / "manifests" / "predict.json").read_text())
    assert {Path(i["path"]).parts[-2] for i in pred_man["inputs"]
            if i["path"].endswith("results.json")} == {"conf", "sealed"}
