"""U5 power simulation (prereg v0.5 §4.4): the generating model hits its target Spearman, power
rises with the planted gain, and the probe-only and infeasible branches behave."""

import numpy as np
import pytest

from emx.i9 import contracts as C
from emx.i9 import power as W
from emx.i9 import predict as P


@pytest.fixture(scope="module")
def obs():
    rng = np.random.default_rng(0)
    groups = np.array([C.template_group(t, f"g{rng.integers(9)}")
                       for t in rng.choice(C.LYNCH_SCENARIOS, 64)])
    # sorted: set order follows PYTHONHASHSEED, which made the draw differ between processes
    goal = {g: rng.normal(0, 1.2) for g in sorted(set(groups.tolist()))}
    logit = np.array([goal[g] for g in groups]) + rng.normal(0, 1.0, 64)
    n = np.full(64, 16)
    k = rng.binomial(16, P.sigmoid(logit))
    return k, n, groups, P.fold_assignments(groups)


def test_true_rates_and_variance_split():
    assert W.true_rates(np.array([0, 16]), np.array([16, 16])).tolist() == [0.5 / 17, 16.5 / 17]
    g = np.array(["a", "a", "b", "b"])
    assert W.variance_split(np.array([0.1, 0.1, 0.9, 0.9]), g) == pytest.approx(1.0)
    assert W.variance_split(np.array([0.1, 0.9, 0.1, 0.9]), g) == pytest.approx(0.0)


@pytest.mark.parametrize("target", [0.35, 0.65])
def test_tuned_predictors_hit_target_spearman(obs, target):
    k, n, groups, _ = obs
    r = W.true_rates(k, n)
    f = W.variance_split(k / n, groups)
    sigma = W.tune_sigma(W.NoiseModel(r, n, groups, f, 2000, seed=0), target)
    fresh = W.NoiseModel(r, n, groups, f, 4000, seed=123)  # independent draws
    assert abs(fresh.spearman(sigma) - target) <= 0.03


def test_unreachable_target_gives_none(obs):
    k, n, groups, _ = obs
    nm = W.NoiseModel(W.true_rates(k, n), n, groups, 0.5, 500, seed=0)
    assert W.tune_sigma(nm, 0.999) is None


def test_power_rises_with_planted_gain_and_branches(obs):
    k, n, groups, assign = obs
    kw = {"n_sims": 40, "n_boot": 400, "n_mc": 600}
    none = W.run_power(k, n, groups, assign, 0.3, gain=0.0, **kw)
    big = W.run_power(k, n, groups, assign, 0.3, gain=0.5, **kw)
    assert none["feasible"] and big["feasible"]
    assert big["power"] > none["power"] + 0.3
    assert none["power"] <= 0.15
    # the single test at 0.05 can only be easier than Holm over both primaries
    assert big["power_probe_only"] >= big["power"]
    # achieved Spearman in the simulations tracks the targets
    ms = big["mean_spearman"]
    assert abs(ms["baseline"] - 0.3) < 0.06 and abs(ms[W.PROBE] - 0.8) < 0.06
    assert abs(ms["standin0"] - ms["baseline"]) < 0.06


def test_infeasible_baseline_is_reported_not_run(obs):
    k, n, groups, assign = obs
    out = W.run_power(k, n, groups, assign, 0.7, n_sims=5, n_boot=10)
    assert out["feasible"] is False and "power" not in out
    assert "0.65" in out["reason"]


def test_power_uses_each_scenarios_own_n(obs):
    k, n, groups, assign = obs
    n2 = n.copy()
    n2[::4] = 12  # four fewer replies in every fourth scenario
    k2 = np.minimum(k, n2)
    nm = W.NoiseModel(W.true_rates(k2, n2), n2, groups, 0.5, 50, seed=0)
    assert np.allclose(np.round(nm.obs * n2), nm.obs * n2)  # k / n with each scenario's n
    assert W.true_rates(k2, n2)[0] == (k2[0] + 0.5) / 13
    out = W.run_power(k2, n2, groups, assign, 0.3, n_sims=3, n_boot=50, n_mc=200)
    assert out["feasible"] and 0 <= out["power"] <= 1
