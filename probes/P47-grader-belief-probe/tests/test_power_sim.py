"""Tests for design/power_sim.py (quick settings; the plan's sizes are the CLI defaults)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_PATH = Path(__file__).resolve().parents[1] / "design" / "power_sim.py"
_spec = importlib.util.spec_from_file_location("p47_power_sim", _PATH)
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)


def test_simulate_shapes_and_talk_filter():
    rng = np.random.default_rng(0)
    s = P.simulate(rng, -1.0, 0.5, 1.0, 0.3, 2000, 2, 6, talk_rate=0.25)
    n = len(s["task"])
    assert 0.70 * 4000 < n < 0.80 * 4000
    assert ((s["k"] >= 0) & (s["k"] <= 6)).all() and (s["m"] == 6).all()
    # probe reliability 0.5: var(probe) = 2 (1 - f) sigma_s^2
    assert np.var(s["probe"]) == pytest.approx(2 * 0.7, rel=0.1)
    assert np.var(s["a"]) == pytest.approx(0.3, rel=0.1)
    df = P.to_frame(P.simulate(rng, -1.0, 0.5, 1.0, 0.3, 5, 2, 6))
    assert len(df) == 60 and df.groupby("site_id").size().eq(6).all()
    assert df.groupby("site_id")[["bb", "probe"]].nunique().max().max() == 1


def test_calibration_hits_target_gap():
    cal = P.calibrate(-1.0, 0.8, 1.5, n_large=50_000, seed=1)
    assert not cal["underpowered_by_construction"]
    assert cal["gap"] == pytest.approx(0.05, abs=0.006)
    assert 0 < cal["f"] < 1
    gaps = np.array([r["gap"] for r in cal["grid"]])
    assert gaps[0] > 0.05 > gaps[-1] and abs(gaps[-1]) < 0.01    # f = 1: probe is constant
    assert cal["auc_base"] > 0.5 and cal["f_at_max_gap"] == 0.0


def test_calibration_fallback_when_gap_unattainable():
    cal = P.calibrate(-1.0, 0.8, 0.05, n_large=20_000, seed=2)
    assert cal["underpowered_by_construction"] and cal["f"] == 0.0
    assert cal["max_gap"] < 0.05


def test_delta_min_positive_and_power_counts():
    cal = P.calibrate(-1.0, 0.8, 1.5, n_large=20_000, seed=3)
    small = {"tasks": 60, "sites_per_task": 2, "draws": 6, "folds": 5}
    dm = P.delta_min(-1.0, 0.8, 1.5, cal["f"], n_datasets=8, seed=4, large=small)
    assert dm["delta_min"] > 0
    pw = P.power(-1.0, 0.8, 1.5, cal["f"], dm["delta_min"], tasks=12, sites_per_task=2,
                 draws=6, talk_rate=0.5, n_datasets=4, n_boot=20, seed=5)
    assert sum(pw["verdict_shares"].values()) == pytest.approx(1.0)
    # 12 tasks x 2 sites with half dropped is under 20 sites: always insufficient
    assert pw["verdict_shares"]["insufficient"] == 1.0 and pw["power"] == 0.0


def test_fit_two_level_recovers_parameters_and_floors_sigma_site():
    rng = np.random.default_rng(6)
    s = P.simulate(rng, -1.0, 0.8, 1.2, 0.5, 400, 2, 6)
    df = pd.DataFrame({"task_id": s["task"], "site_id": np.arange(len(s["task"])),
                       "n_draws": s["m"], "n_cheats": s["k"]})
    fit = P.fit_two_level(df)
    assert fit["mu"] == pytest.approx(-1.0, abs=0.25)
    assert fit["sigma_task"] == pytest.approx(0.8, abs=0.35)
    assert fit["sigma_site"] == pytest.approx(1.2, abs=0.35)
    # no site variation beyond the task's: sigma_site floored at 0.5
    rng = np.random.default_rng(7)
    te = rng.normal(0, 1.0, 300)
    k = rng.binomial(6, 1 / (1 + np.exp(-(-0.5 + np.repeat(te, 2)))))
    df = pd.DataFrame({"task_id": np.repeat(np.arange(300), 2), "site_id": np.arange(600),
                       "n_draws": 6, "n_cheats": k})
    fit = P.fit_two_level(df)
    assert fit["sigma_site"] >= 0.5
    assert fit["sigma_site_floored"] == (fit["sigma_site_fitted"] < 0.5)


def test_cli_writes_json(tmp_path):
    sites = tmp_path / "sites.csv"
    rng = np.random.default_rng(8)
    pd.DataFrame({"task_id": np.repeat(np.arange(30), 2), "site_id": np.arange(60),
                  "n_draws": 2, "n_cheats": rng.binomial(2, 0.3, 60)}).to_csv(sites, index=False)
    out = tmp_path / "power.json"
    rc = P.main(["--fit-from", str(sites), "--tasks", "12", "--sites-per-task", "2",
                 "--draws", "6", "--n-large", "5000", "--delta-datasets", "4",
                 "--skip-power", "--n-jobs", "1", "--out", str(out)])
    assert rc == 0
    r = json.loads(out.read_text())
    assert r["fit"]["sigma_site"] >= 0.5 and r["power"] is None
    dm = r["delta_min"]
    assert dm["used"] == "planned" and dm["value"] == dm["planned"]["delta_min"]
    assert dm["large"]["large_sample_dataset"]["tasks"] == 300
    assert len(r["calibration"]["grid"]) == 101


def test_delta_min_planned_skips_short_datasets():
    d = P.delta_min_planned(-1.0, 0.8, 1.5, 0.2, tasks=30, sites_per_task=2, draws=6,
                            talk_rate=0.2, n_datasets=6, seed=9)
    assert d["n_valid"] == 6 and d["delta_min"] > -0.05
    short = P.delta_min_planned(-1.0, 0.8, 1.5, 0.2, tasks=8, sites_per_task=2, draws=6,
                                n_datasets=3, seed=9)
    assert short["n_valid"] == 0 and short["share_short_of_min_counts"] == 1.0


def test_gap_sweep_keeps_delta_min_at_005_and_marks_unattainable(tmp_path):
    out = tmp_path / "gaps.json"
    P.main(["--mu", "-1", "--sigma-task", "0.8", "--sigma-site", "1.5", "--tasks", "20",
            "--sites-per-task", "2", "--draws", "6", "--talk-rate", "0.1", "--n-large", "20000",
            "--delta-datasets", "6", "--power-datasets", "3", "--power-boot", "20",
            "--gaps", "0.10,0.9", "--delta-min-at", "large", "--n-jobs", "1",
            "--out", str(out)])
    r = json.loads(out.read_text())
    assert r["delta_min"]["used"] == "large"
    assert r["delta_min"]["value"] == r["delta_min"]["large"]["delta_min"]
    by = {e["target_gap"]: e for e in r["gaps"]}
    assert set(by) == {0.05, 0.10, 0.9}                     # +0.05 always included
    assert by[0.9]["unattainable"] and "power" not in by[0.9]
    assert by[0.10]["f"] < by[0.05]["f"]                    # a bigger gap needs a smaller f
    assert by[0.10]["gap"] == pytest.approx(0.10, abs=0.01)
    for g in (0.05, 0.10):
        sh = by[g]["power"]["verdict_shares"]
        assert sum(sh.values()) == pytest.approx(1.0)
    assert r["power"] == by[0.05]["power"]
