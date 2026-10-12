"""Synthetic checks for P22 step 0 (plan.md, "Automatic rules")."""

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_spec = importlib.util.spec_from_file_location("p22_step0", Path(__file__).parent / "step0.py")
s0 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s0)

FAMS = [s0.FAMILY[m] for m in s0.MODELS]


def _synthetic(rng, n_items=400, caution_sd=1.0, family_sd=0.0, noise_sd=1.0):
    """Profiles = model caution level x a shared caution axis + optional family component + noise.
    Caution levels are correlated with family (Claude cautious), as in the real panel."""
    v = rng.normal(size=n_items)
    fam_axes = {f: rng.normal(size=n_items) for f in set(FAMS)}
    level = np.array([1.5 if f == "anthropic" else -0.5 for f in FAMS]) + rng.normal(0, 0.7, 12)
    P = caution_sd * level[:, None] * v[None, :]
    P += family_sd * np.stack([fam_axes[f] for f in FAMS])
    P += noise_sd * rng.normal(size=P.shape)
    return P


def test_projection_control_is_biased_low_under_the_null():
    """Documents why the plan's projection control was replaced (log.md, 22:13Z): with no family
    signal at all, projecting out PC1 pushes same-family pairs apart."""
    rng = np.random.default_rng(0)
    b = np.arange(400) >= 200
    aucs = [s0.analyse(_synthetic(rng), b, FAMS, k=1, n_boot=0)["auc"] for _ in range(20)]
    assert np.mean(aucs) < 0.4


def test_caution_only_inflates_auc_without_the_control():
    """The confound the control exists for: with k = 0 a caution level correlated with family
    alone separates the families."""
    rng = np.random.default_rng(1)
    b = np.arange(400) >= 200
    aucs = [s0.analyse(_synthetic(rng), b, FAMS, k=0, n_boot=0)["auc"] for _ in range(20)]
    assert np.mean(aucs) > 0.7


@pytest.mark.parametrize("hetero", [False, True])
def test_factor_control_gives_chance_auc_under_the_null(hetero):
    rng = np.random.default_rng(5)
    n = 4000
    b = np.arange(n) >= n // 2
    aucs = []
    for _ in range(30):
        P = _synthetic(rng, n_items=n)
        if hetero:
            P += rng.normal(size=P.shape) * rng.uniform(0, 1.5, 12)[:, None]
        aucs.append(s0.fa_gate(s0.centre_items(P), b, FAMS)["auc"])
    assert 0.4 < np.mean(aucs) < 0.6


def test_factor_control_recovers_a_planted_family_component():
    rng = np.random.default_rng(6)
    n = 20000
    b = np.arange(n) >= n // 2
    aucs = [s0.fa_gate(s0.centre_items(_synthetic(rng, n_items=n, family_sd=1.0)), b, FAMS)["auc"]
            for _ in range(10)]
    assert np.mean(aucs) > 0.7


def test_factor_fit_recovers_one_factor_covariance():
    lam = np.linspace(-1, 2, 12)[:, None]
    psi = np.linspace(0.5, 1.5, 12)
    L, p = s0.factor_fit(lam @ lam.T + np.diag(psi), 1)
    assert np.allclose(L @ L.T, lam @ lam.T, atol=1e-4)
    assert np.allclose(p, psi, atol=1e-4)


def test_parametric_null_and_power_shapes():
    rng = np.random.default_rng(7)
    n = 2000
    b = np.arange(n) >= n // 2
    Pc = s0.centre_items(_synthetic(rng, n_items=n))
    out = s0.parametric_null(Pc, b, FAMS, n_sim=20, seed=1, block_shares=(0.0, 0.5),
                             n_sim_power=10)
    assert 0.3 < out["null_auc_mean"] < 0.7
    assert out["power"]["0.5"]["auc_mean"] > out["null_auc_mean"]
    boot = s0.fa_bootstrap(Pc, b, FAMS, 1, 20, 1)
    assert boot["auc_ci"][0] <= boot["auc_ci"][1]


def test_residualize_removes_the_axis():
    rng = np.random.default_rng(3)
    Pc = s0.centre_items(rng.normal(size=(12, 50)))
    V, share = s0.caution_axes(Pc, 2)
    R = s0.residualize(Pc, V)
    assert np.allclose(R @ V.T, 0)
    assert np.isclose(share.sum(), 1)
    assert s0.residualize(Pc, V[:0]) is Pc


def test_within_family_axis_ignores_between_family_offsets():
    rng = np.random.default_rng(4)
    v_within = rng.normal(size=300)
    u_between = rng.normal(size=300)
    P = np.stack([(2.0 if f == "anthropic" else -2.0) * u_between for f in FAMS])
    P += np.linspace(-1, 1, 12)[:, None] * v_within
    vw = s0.within_family_axis(s0.centre_items(P), FAMS)
    assert vw.shape == (300,)
    cos = abs(vw @ v_within) / np.linalg.norm(v_within)
    assert cos > 0.99


def test_auc_ties_count_half():
    assert s0.auc(np.array([1.0]), np.array([1.0])) == 0.5
    assert s0.auc(np.array([2.0, 0.0]), np.array([1.0])) == 0.5
    assert s0.auc(np.array([2.0]), np.array([1.0, 0.0])) == 1.0


def test_pair_counts_match_the_plan():
    _, same = s0.pair_masks(FAMS)
    assert len(same) == 66 and same.sum() == 20


def test_battery_split_sizes_and_determinism():
    a, b = s0.battery_indices(43_000)
    a2, b2 = s0.battery_indices(43_000)
    assert len(a) == len(b) == 200
    assert not set(a) & set(b)
    assert np.array_equal(a, a2) and np.array_equal(b, b2)


def test_hash_split_is_roughly_half_and_stable():
    keys = [f"shard:{i}" for i in range(4000)]
    m = s0.hash_split_b(keys)
    assert 0.45 < m.mean() < 0.55
    assert np.array_equal(m, s0.hash_split_b(keys))


@pytest.mark.parametrize("q,refusal", [
    ("I cannot create that query.", True),
    ("  i'm sorry, but I can't", True),
    ("I apologise for", True),
    ("I can not do it", True),
    ("I'm running a small bakery and", False),
    ("Can I cannot-proof my app?", False),
])
def test_meta_refusal_regex(q, refusal):
    assert bool(s0.META_REFUSAL.match(q)) is refusal


def test_eligibility_drops_refusals_missing_and_out_of_range():
    rows = []
    for q, val in [("ok query", 3), ("I cannot help", 3), ("ok", None), ("ok", 7)]:
        r = {"query": q}
        for m in s0.MODELS:
            r[f"{m}_value1_position"] = 3
            r[f"{m}_value2_position"] = 3
        r["o3_value1_position"] = val
        rows.append(r)
    mask = s0.eligible_mask(pd.DataFrame(rows))
    assert mask.tolist() == [True, False, False, False]
