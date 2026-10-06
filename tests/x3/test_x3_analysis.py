"""U7 tests: X3 analysis on synthetic data (15 tasks x 4 repeats x 6 contexts x 2 models)."""
from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from x3 import analysis as A
from x3 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
DS, QW = C.SUBJECTS


def _frame(**kw):
    return A.normalize(A.synthetic_episodes(**kw).astype(object))


@pytest.fixture(scope="module")
def planted():
    return A.analyze_frame(_frame(seed=3, purpose_effect=0.20), n_perm=2000, n_boot=1000,
                           measurement_ok=True)


def _null_pvalue(seed: int, n_perm: int) -> float:
    _, used, _ = A.split_rows(_frame(seed=seed, p_undefined=0.0))
    sub = used[used["purpose"].isin(["cap", "safe"])]
    blocks = list(zip(sub["model"], sub["task_id"], sub["repeat"], sub["grader"], strict=True))
    strata = list(zip(sub["model"], sub["task_id"], strict=True))
    return A.permutation_test(sub["y"].to_numpy(), (sub["purpose"] == "cap").to_numpy(), blocks,
                              strata, n_perm=n_perm, seed=1)["p"]


# --- effect recovery and level -------------------------------------------------------------
def test_recovers_planted_purpose_effect(planted):
    r = planted["primary"]["purpose"]["pooled"]
    assert r["models"] == sorted(C.SUBJECTS)
    assert r["rd"] > 0
    assert r["ci95"]["lo"] <= 0.20 <= r["ci95"]["hi"]
    assert r["perm"]["p"] < 0.01 and r["holm_reject"]
    assert r["cmh"]["p"] < 0.01
    assert planted["scenario"] == "Purpose effect"
    g = planted["primary"]["grader"]["pooled"]
    assert not g["holm_reject"] and g["ci95"]["lo"] <= 0 <= g["ci95"]["hi"]


def _null_rate(n_sim: int, n_perm: int) -> float:
    ps = np.array([_null_pvalue(1000 + i, n_perm) for i in range(n_sim)])
    rate = float((ps < 0.05).mean())
    print(f"\nsimulated null rejection rate: {rate:.3f} ({n_sim} datasets, {n_perm} perms)")
    return rate


def test_null_level_200_datasets():
    rate = _null_rate(200, 500)
    # binomial(200, 0.05): sd 0.015; [0.01, 0.10] is beyond ±3 sd
    assert 0.01 <= rate <= 0.10


@pytest.mark.slow
def test_null_level_1000_datasets():
    rate = _null_rate(1000, 2000)
    assert 0.03 <= rate <= 0.07


def test_permutation_identical_outcomes_gives_p_one():
    y = np.zeros(8)
    treat = np.array([1, 0] * 4, dtype=bool)
    blocks = [0, 0, 1, 1, 2, 2, 3, 3]
    r = A.permutation_test(y, treat, blocks, [0] * 8, n_perm=100, seed=1)
    assert r["stat"] == 0 and r["p"] == 1.0


def test_blocks_must_nest_in_strata():
    with pytest.raises(ValueError):
        A.permutation_test(np.ones(4), np.array([1, 0, 1, 0], bool), [0, 0, 0, 0],
                           [0, 0, 1, 1], n_perm=10, seed=1)


def test_mh_rd_equals_planted_difference_on_exact_counts():
    # two strata of different size, both with difference 0.25
    assert A.mh_rd([4, 8], [3, 4], [4, 8], [2, 2]) == pytest.approx(0.25)
    # duplicating a stratum leaves the estimate unchanged (bootstrap draws a task twice)
    assert A.mh_rd([4, 4, 8], [1, 1, 4], [4, 4, 8], [0, 0, 1]) == pytest.approx(
        A.mh_rd([8, 8], [2, 4], [8, 8], [0, 1]))


# --- validity and exclusions ---------------------------------------------------------------
def test_failed_manipulation_check_excludes_model_from_that_contrast_only():
    df = _frame(seed=5, purpose_effect=0.2, manip_fail={QW: {"purpose"}})
    r = A.analyze_frame(df, n_perm=200, n_boot=100)
    v = r["validity"][QW]
    assert not v["purpose_check"]["ok"] and v["grader_check"]["ok"]
    assert v["enters"] == {"purpose": False, "grader": True, "secondary": True}
    pp = r["primary"]["purpose"]["pooled"]
    assert pp["models"] == [DS] and pp["single_model"]
    assert r["primary"]["grader"]["pooled"]["models"] == sorted(C.SUBJECTS)
    assert not r["primary"]["purpose"]["per_model"][QW]["entered"]
    assert f"purpose|{QW}" not in r["holm"]["per_model"]


def test_base_rate_and_completion_rules():
    df = _frame(seed=6, base_range=(0.9, 0.97))
    r = A.analyze_frame(df, n_perm=50, n_boot=20, measurement_ok=True)
    assert not any(v["base_rate_ok"] for v in r["validity"].values())
    assert r["scenario"] == "Floor or ceiling"
    df = _frame(seed=7, p_api_error=0.2)
    r = A.analyze_frame(df, n_perm=50, n_boot=20, measurement_ok=True)
    assert all(not v["completion"]["ok"] for v in r["validity"].values())
    assert r["scenario"] == "No valid primary contrast"
    assert r["exclusions"]["n_api_error"] > 0


def test_exclusions_counted():
    df = _frame(seed=8, p_undefined=0.05)
    df.loc[df.index[:10], "analysis_role"] = "probe"
    in_role, used, ex = A.split_rows(df)
    assert ex["n_rows_not_in_analysis_role"] == 10
    assert ex["n_cheat_undefined"] == int(in_role["cheat"].isna().sum()) > 0
    assert ex["n_used"] == len(used) == ex["n_in_analysis_role"] - ex["n_cheat_undefined"]


def test_duplicate_rows_rejected():
    df = A.synthetic_episodes(seed=9).astype(object)
    df = A.normalize(pd.concat([df, df.iloc[[0]]], ignore_index=True))
    with pytest.raises(ValueError, match="duplicate"):
        A.split_rows(df)


def test_purpose_column_must_match_context():
    df = A.synthetic_episodes(seed=9).astype(object)
    df.loc[df["context"] == "cap_script", "purpose"] = "safe"
    with pytest.raises(ValueError, match="disagrees"):
        A.normalize(df)


def test_wilson_matches_statsmodels():
    from statsmodels.stats.proportion import proportion_confint
    lo, hi = A.wilson_ci(7, 30)
    elo, ehi = proportion_confint(7, 30, alpha=0.05, method="wilson")
    assert lo == pytest.approx(elo) and hi == pytest.approx(ehi)
    assert all(math.isnan(x) for x in A.wilson_ci(0, 0))


def test_answer_letters_parse():
    assert [A._parse_letter(s) for s in ("a", "(B)", "c) real work", "E.", "", None, "x")] == [
        "a", "b", "c", "e", None, None, None]


# --- scenarios -----------------------------------------------------------------------------
def _base_result(planted):
    r = copy.deepcopy(planted)
    for n in A.PRIMARIES:
        p = r["primary"][n]["pooled"]
        p.update(available=True, holm_reject=False, rd=0.01,
                 ci975={"lo": -0.05, "hi": 0.05})
    r["secondary"]["eval_vs_teammate"]["pooled"]["perm"]["p"] = 0.5
    r["checks"]["measurement_ok"] = True
    return r


def test_no_headline_without_a_measurement_pass(planted):
    r = _base_result(planted)
    r["primary"]["purpose"]["pooled"].update(holm_reject=True, rd=0.2)
    r["checks"]["measurement_ok"] = None
    assert A.classify_scenario(r) == A.PENDING


def test_measurement_status_reads_the_file(tmp_path):
    p = tmp_path / "m.json"
    assert A.measurement_status(p)[0] is None
    import json as _json

    full = {k: {"pass": True} for k in A.REQUIRED_MEASUREMENT}
    for k in A.HASH_BOUND_MEASUREMENT:
        full[k]["inputs_sha256"] = "abc"
    cur = {k: "abc" for k in A.HASH_BOUND_MEASUREMENT}
    p.write_text(_json.dumps({"checks": full}))
    assert A.measurement_status(p, cur)[0] is True
    assert A.measurement_status(p, {**cur, "detector_review": "changed"})[0] is None  # stale
    full["judge_validation"]["pass"] = False
    p.write_text(_json.dumps({"checks": full}))
    assert A.measurement_status(p, cur)[0] is False
    p.write_text('{"checks": {"detector_review": {"pass": true}}}')   # one arbitrary pass
    assert A.measurement_status(p)[0] is None
    del full["judge_validation"]["inputs_sha256"]
    full["judge_validation"]["pass"] = True
    p.write_text(_json.dumps({"checks": full}))                      # unbound review
    assert A.measurement_status(p)[0] is None


@pytest.mark.parametrize("scenario", A.SCENARIOS)
def test_classify_each_scenario(planted, scenario):
    r = _base_result(planted)
    pp, gp = r["primary"]["purpose"]["pooled"], r["primary"]["grader"]["pooled"]
    if scenario == "Measurement failure":
        r["checks"]["measurement_ok"] = False
    elif scenario == "Floor or ceiling":
        for v in r["validity"].values():
            v["base_rate_ok"] = False
    elif scenario == "No valid primary contrast":
        pp["available"] = gp["available"] = False
    elif scenario == "Purpose effect":
        pp.update(holm_reject=True, rd=0.2)
    elif scenario == "Reverse purpose effect":
        pp.update(holm_reject=True, rd=-0.2)
    elif scenario == "Grader effect only":
        gp.update(holm_reject=True, rd=-0.15)
    elif scenario == "Eval or not only":
        r["secondary"]["eval_vs_teammate"]["pooled"]["perm"]["p"] = 0.01
    elif scenario == "Large effects excluded":
        pass
    elif scenario == "Inconclusive":
        gp["ci975"] = {"lo": -0.15, "hi": 0.05}
    assert A.classify_scenario(r) == scenario


def test_large_effects_excluded_needs_both_contrasts(planted):
    r = _base_result(planted)
    r["primary"]["grader"]["pooled"]["available"] = False
    assert A.classify_scenario(r) == "Inconclusive"


# --- output --------------------------------------------------------------------------------
def test_cli_writes_analysis_json_with_hashes_and_seeds(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import x3_analyze

    extra = tmp_path / "detector.csv"
    extra.write_text("episode_id,cheat\n")
    out = tmp_path / "analysis.json"
    ep = tmp_path / "episodes.csv"
    assert x3_analyze.main(["--synthetic", "--episodes", str(ep), "--out", str(out),
                            "--n-perm", "200", "--n-boot", "100",
                            "--inputs", str(extra)]) == 0
    text = out.read_text()
    assert "NaN" not in text
    res = json.loads(text)
    assert res["inputs"]["episodes_csv"]["sha256"] == A.sha256_file(ep)
    assert res["inputs"][str(extra)]["sha256"] == A.sha256_file(extra)
    assert res["seeds"] == {"permutation": C.SEED_PERMUTATION, "bootstrap": C.SEED_BOOTSTRAP,
                            "n_perm": 200, "n_boot": 100}
    assert res["synthetic"]["seed"] == 0
    assert res["scenario"] in (*A.SCENARIOS, A.PENDING)
    assert res["exclusions"]["reasons"]
    assert {c["context"] for c in res["cells"]} == {c.value for c in C.Context}


def test_analyze_signature_defaults(tmp_path):
    ep = tmp_path / "episodes.csv"
    A.synthetic_episodes(seed=2, n_tasks=4, n_repeats=2).to_csv(ep, index=False)
    import inspect
    sig = inspect.signature(A.analyze)
    assert sig.parameters["n_perm"].default == 10_000 and sig.parameters["seed"].default == 1
    r = A.analyze_path(ep, n_perm=50, n_boot=20)
    assert r["inputs"]["episodes_csv"]["n_rows"] == 4 * 2 * 6 * 2
