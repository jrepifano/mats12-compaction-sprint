"""U11 power simulation: fast mode runs, curves are monotone-ish, probabilities are sane."""

from __future__ import annotations

import importlib.util
from itertools import pairwise
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("e1_power_sim", ROOT / "scripts/e1_power_sim.py")
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)


def test_profiles_sum_to_one_and_dominant_is_gender_roles():
    prof = ps.profiles()
    for w in prof.values():
        assert np.isclose(w.sum(), 1.0)
    assert ps.QUESTIONS[int(np.argmax(prof["dominant"]))] == "gender_roles"


def test_second_judge_equal_marginals():
    rng = np.random.default_rng(1)
    p = np.full(8, 0.1)
    arm = ps.simulate_arm(rng, p, 100_000, 1)
    n, a, b = arm["n"].sum(), arm["em_luna"].sum(), arm["em_dsk"].sum()
    assert abs(a / n - b / n) < 0.005  # equal marginals


def test_fast_run_monotone_and_valid(tmp_path: Path):
    res = ps.run(
        n_sims=150, bad_rates=(0.0, 0.03, 0.075, 0.15), good_rates=(0.005,), samples=(100,)
    )
    assert len(res["grid"]) == 3 * 4
    for prof in ("uniform", "middle", "dominant"):
        pts = [g for g in res["grid"] if g["profile"] == prof]
        clear = [g["null"]["p_clear"] for g in pts]
        assert clear[0] == 0.0  # no EM -> never clear
        assert clear[-1] >= 0.95
        assert all(b >= a - 0.1 for a, b in pairwise(clear))
        for g in pts:
            for mode in ("null", "same"):
                tot = sum(g[mode][f"p_{k}"] for k in ("clear", "weak", "none"))
                assert np.isclose(tot, 1.0)
            assert (
                g["same"]["p_stage_clear_k3"]
                >= g["same"]["p_stage_clear_k2"]
                >= g["same"]["p_clear"] - 1e-12
            )
    ps.plot(res, tmp_path / "p.png")
    assert (tmp_path / "p.png").stat().st_size > 0


def test_main_writes_outputs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ps, "BAD_RATES", (0.0, 0.05))
    monkeypatch.setattr(ps, "GOOD_RATES", (0.005,))
    monkeypatch.setattr(ps, "SAMPLES", (100, 50))
    assert ps.main(["--n-sims", "20", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "power_curve.json").exists()
    assert (tmp_path / "power_curve.png").exists()
