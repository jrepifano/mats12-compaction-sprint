"""Tests for the E9 stochastic-rounding test (prereg §4.5 "Rounding test"; plan unit D2).

Pure-statistics tests use synthetic draws with a controlled rounding probability; the torchao
end-to-end tests run AdamW8bit on small CPU tensors (skipped without torchao). Offline, no paid
calls.
"""

from __future__ import annotations

import itertools
import json
import math

import numpy as np
import pytest

from emx.e9 import contracts as C
from emx.e9 import rounding as R

N = C.ROUNDING_DRAWS


def _x_fp32(rng, n, scale=0.02):
    """Random fp32 values (as float64) at weight-like magnitudes, both signs."""
    return (rng.standard_normal(n) * scale).astype(np.float32).astype(np.float64)


def _synthetic_draws(rng, x, n_draws=N, p_fn=None):
    """Draws that round each x to hi with probability p_fn(p) (default: the unbiased p)."""
    lo, hi = R.bf16_neighbors(x)
    ulp = hi - lo
    p = np.where(ulp > 0, (x - lo) / np.where(ulp > 0, ulp, 1.0), 0.0)
    q = p if p_fn is None else np.clip(p_fn(p), 0.0, 1.0)
    q = np.where(ulp > 0, q, 0.0)
    up = rng.random((n_draws, x.size)) < q
    return np.where(up, hi, lo)


# --- bf16_neighbors ---------------------------------------------------------------------------


def test_bf16_neighbors_matches_torch_truncation():
    torch = pytest.importorskip("torch")
    rng = np.random.default_rng(1)
    vals = np.concatenate(
        [
            rng.standard_normal(5000) * 10.0 ** rng.integers(-30, 30, 5000),
            [0.0, -0.0, 1.0, -1.0, 2.0**-130, -(2.0**-140), 3.0e38, 1e-45],
        ]
    ).astype(np.float32)
    t = torch.from_numpy(vals)
    bits = t.view(torch.int32)
    tz = (bits & -65536).view(torch.float32).double().numpy()  # toward zero
    away = ((bits & -65536) + 65536).view(torch.float32).double().numpy()
    exact = (bits & 0xFFFF).numpy() == 0
    lo, hi = R.bf16_neighbors(vals.astype(np.float64))
    x = vals.astype(np.float64)
    pos = x >= 0
    np.testing.assert_array_equal(lo[exact], x[exact])
    np.testing.assert_array_equal(hi[exact], x[exact])
    ne = ~exact
    np.testing.assert_array_equal(lo[ne & pos], tz[ne & pos])
    np.testing.assert_array_equal(hi[ne & pos], away[ne & pos])
    np.testing.assert_array_equal(hi[ne & ~pos], tz[ne & ~pos])
    np.testing.assert_array_equal(lo[ne & ~pos], away[ne & ~pos])
    # round-to-nearest bf16 is always one of the two neighbours, and both are bf16 values
    nearest = t.bfloat16().double().numpy()
    assert np.all((nearest == lo) | (nearest == hi))
    for arr in (lo, hi):
        f = torch.from_numpy(arr.astype(np.float32))
        assert torch.equal(f.bfloat16().float(), f)
    assert np.all(lo <= x) and np.all(x <= hi)


def test_bf16_neighbors_exact_values_and_errors():
    lo, hi = R.bf16_neighbors([0.0, 1.0, -0.5, 1.0 + 2.0**-7, 1.0 + 2.0**-8])
    np.testing.assert_array_equal(lo, [0.0, 1.0, -0.5, 1.0 + 2.0**-7, 1.0])
    np.testing.assert_array_equal(hi, [0.0, 1.0, -0.5, 1.0 + 2.0**-7, 1.0 + 2.0**-7])
    with pytest.raises(ValueError):
        R.bf16_neighbors([np.inf])


# --- Bernstein bound --------------------------------------------------------------------------


def test_bernstein_monotone():
    ts = np.linspace(0, 5, 51)
    b = [R.bernstein_tail(t, 1.0, 0.1) for t in ts]
    assert b[0] == 1.0 and all(b2 <= b1 for b1, b2 in itertools.pairwise(b))
    assert R.bernstein_tail(2.0, 1.0, 0.1) <= R.bernstein_tail(2.0, 2.0, 0.1)
    assert R.bernstein_tail(2.0, 1.0, 0.1) <= R.bernstein_tail(2.0, 1.0, 0.5)
    assert R.bernstein_tail(-2.0, 1.0, 0.1) == R.bernstein_tail(2.0, 1.0, 0.1)
    assert R.bernstein_tail(1.0, 0.0, 0.0) == 0.0 and R.bernstein_tail(0.0, 0.0, 0.0) == 1.0


def _exact_s_distribution(ps, ulps, n):
    """Exact distribution of S = Σ (K_j/n - p_j)·ulp_j, K_j ~ Binomial(n, p_j)."""
    dist = {0.0: 1.0}
    for p, u in zip(ps, ulps, strict=True):
        pmf = [math.comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(n + 1)]
        new: dict[float, float] = {}
        for s, w in dist.items():
            for k, pk in enumerate(pmf):
                key = round(s + (k / n - p) * u, 12)
                new[key] = new.get(key, 0.0) + w * pk
        dist = new
    return dist


@pytest.mark.parametrize(
    "ps,ulps,n",
    [
        ([0.5, 0.5, 0.5], [1.0, 1.0, 1.0], 4),
        ([0.1, 0.9, 0.3], [1.0, 2.0, 0.5], 5),
        ([0.01, 0.02], [1.0, 1.0], 6),
    ],
)
def test_bernstein_bounds_brute_force(ps, ulps, n):
    dist = _exact_s_distribution(ps, ulps, n)
    v = sum(p * (1 - p) * u**2 for p, u in zip(ps, ulps, strict=True)) / n
    m = max(ulps) / n
    for t in sorted({abs(s) for s in dist} | {0.05, 0.3, 1.0}):
        exact_tail = sum(w for s, w in dist.items() if abs(s) >= t - 1e-12)
        assert exact_tail <= R.bernstein_tail(t, v, m) + 1e-12


# --- rounding_stats on synthetic draws ----------------------------------------------------------


def test_unbiased_rounding_passes_at_the_stated_rate():
    rng = np.random.default_rng(2)
    fails = 0
    for _ in range(40):
        x = _x_fp32(rng, 2000)
        r = R.rounding_stats(_synthetic_draws(rng, x), x)
        assert r["pass_deterministic"] and r["pass_mean_in_range"]
        fails += not r["pass"]
    assert fails <= 1  # nominal 0.0033 per test; the bound is conservative


@pytest.mark.parametrize("p_fn", [lambda p: p + 0.1, lambda p: p * 1.1], ids=["add", "mul"])
def test_biased_rounding_fails(p_fn):
    rng = np.random.default_rng(3)
    x = _x_fp32(rng, 5000)
    r = R.rounding_stats(_synthetic_draws(rng, x, p_fn=p_fn), x)
    assert not r["pass_bernstein"] and not r["pass"]
    assert r["bernstein_tail"] < R.PER_TENSOR_ALPHA


def test_deterministic_elements_must_be_exact():
    rng = np.random.default_rng(4)
    x = _x_fp32(rng, 500)
    x[:50] = R.bf16_neighbors(x[:50])[0]  # exactly representable
    draws = _synthetic_draws(rng, x)
    r = R.rounding_stats(draws, x)
    assert r["n_deterministic"] >= 50 and r["pass"]
    draws[7, 0] = x[0] + 2.0**-20  # one deterministic element moves once
    r = R.rounding_stats(draws, x)
    assert r["n_deterministic_violations"] == 1 and not r["pass_deterministic"] and not r["pass"]


def test_mean_outside_lo_hi_fails():
    rng = np.random.default_rng(5)
    x = _x_fp32(rng, 500)
    draws = _synthetic_draws(rng, x)
    lo, hi = R.bf16_neighbors(x)
    j = int(np.flatnonzero(hi > lo)[0])
    draws[:, j] = hi[j] + (hi[j] - lo[j])  # every draw one ulp too far
    r = R.rounding_stats(draws, x)
    assert r["n_mean_outside_lo_hi"] == 1 and not r["pass_mean_in_range"] and not r["pass"]
    assert r["n_off_grid_draws"] == N


def test_seeded_subsample():
    rng = np.random.default_rng(6)
    x = _x_fp32(rng, 3000)
    draws = _synthetic_draws(rng, x, n_draws=20)
    a = R.rounding_stats(draws, x, sample_max=1000)
    b = R.rounding_stats(draws, x, sample_max=1000)
    assert a["sampled"] and a["n_tested"] == 1000 and a["sample_seed"] == R.SAMPLE_SEED
    assert a["S"] == b["S"] and a["V"] == b["V"]
    full = R.rounding_stats(draws, x)
    assert not full["sampled"] and full["n_tested"] == full["n_random"]


def test_all_deterministic_tensor_passes():
    x = np.array([1.0, 0.5, -2.0, 0.0])
    r = R.rounding_stats(np.tile(x, (N, 1)), x)
    assert r["pass"] and r["n_random"] == 0 and r["bernstein_tail"] == 1.0


def test_review_case_nan_draws_fail():
    r = R.rounding_stats(np.full((N, 1), np.nan), [1.001])
    assert not r["pass"] and not r["pass_finite"] and not r["pass_bernstein"]
    assert r["n_nonfinite_draws"] == N and r["n_mean_outside_lo_hi"] == 1
    assert r["S"] is None and r["bernstein_tail"] is None and r["fail_reasons"]
    json.dumps(r, allow_nan=False)  # the record stays strict JSON


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_nonfinite_draw_fails_even_outside_the_sample(bad):
    rng = np.random.default_rng(7)
    x = _x_fp32(rng, 3000)
    draws = _synthetic_draws(rng, x, n_draws=20)
    lo, hi = R.bf16_neighbors(x)
    acc = R.DrawAccumulator(x)
    sampled = R.rounding_stats(draws, x, sample_max=100)
    assert sampled["pass_finite"]
    # corrupt one random element that the seeded 100-element sample does not include
    rand_idx = np.flatnonzero(hi > lo)
    chosen = set(
        np.random.default_rng(R.SAMPLE_SEED).choice(rand_idx, size=100, replace=False).tolist()
    )
    j = next(int(k) for k in rand_idx if int(k) not in chosen)
    draws[3, j] = bad
    for row in draws:  # the streaming accumulator path
        acc.add(row)
    for r in (acc.result(sample_max=100), R.rounding_stats(draws, x, sample_max=100)):
        assert not r["pass"] and not r["pass_finite"]
        assert r["n_nonfinite_draws"] == 1 and r["n_mean_outside_lo_hi"] == 1
        assert any("nonfinite draws" in m for m in r["fail_reasons"])
        json.dumps(r, allow_nan=False)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_nonfinite_draw_on_deterministic_element_fails(bad):
    x = np.array([1.0, 0.5])
    draws = np.tile(x, (N, 1))
    draws[0, 0] = bad
    r = R.rounding_stats(draws, x)
    assert not r["pass"] and not r["pass_finite"] and r["n_deterministic_violations"] == 1


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_nonfinite_x_fails_with_a_reason(bad):
    rng = np.random.default_rng(8)
    x = _x_fp32(rng, 200)
    draws = _synthetic_draws(rng, x)
    x[5] = bad
    r = R.rounding_stats(draws, x)
    assert not r["pass"] and not r["pass_finite"] and r["n_nonfinite_x"] == 1
    assert any("nonfinite x" in m for m in r["fail_reasons"])
    json.dumps(r, allow_nan=False)


def test_bernstein_nonfinite_inputs_never_pass():
    for args in [(np.nan, 1.0, 0.1), (1.0, np.inf, 0.1), (1.0, 1.0, np.nan), (np.inf, 1.0, 0.1)]:
        assert not (R.bernstein_tail(*args) >= R.PER_TENSOR_ALPHA)


def test_passing_record_has_no_reasons():
    rng = np.random.default_rng(9)
    x = _x_fp32(rng, 500)
    r = R.rounding_stats(_synthetic_draws(rng, x), x)
    assert r["pass"] and r["pass_finite"] and r["fail_reasons"] == []


def test_constants_follow_the_prereg():
    assert C.ROUNDING_DRAWS == 200 and C.ROUNDING_SAMPLE_MAX == 100_000
    assert R.PER_TENSOR_ALPHA == pytest.approx(0.01 / 3)


# --- torchao end to end (CPU) -----------------------------------------------------------------


@pytest.fixture(scope="module")
def torch_ao():
    torch = pytest.importorskip("torch")
    pytest.importorskip("torchao.optim", reason="torchao not installed")
    try:  # AdamW8bit compiles its step with torch.compile; the CPU backend needs a C++ compiler
        p = torch.nn.Parameter(torch.zeros(4096, dtype=torch.bfloat16))
        opt = R.make_optimizer([p], stochastic_round=True)
        p.grad = torch.ones_like(p)
        opt.step()
    except Exception as e:  # noqa: BLE001 - pragma: no cover, environment dependent
        pytest.skip(f"AdamW8bit does not run on CPU here: {e}")
    return torch


def _load_cli():
    """scripts/e9_rounding_test.py as a module (scripts/ is not a package)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "e9_rounding_test.py"
    spec = importlib.util.spec_from_file_location("e9_rounding_test_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _weights(torch, shape, seed=0):
    g = torch.Generator().manual_seed(seed)
    return (torch.randn(shape, generator=g) * 0.02).bfloat16()


def test_snapshot_restore_is_deterministic(torch_ao):
    torch = torch_ao
    st = R.TensorStepper(_weights(torch, (64, 256)), device="cpu")
    assert st.state_layout["exp_avg"]["kind"] == "OptimState8bit"
    assert st.state_layout["step"]["value"] == 1.0
    a, b, c = st.draw(5), st.draw(5), st.draw(6)
    assert torch.equal(a, b) and not torch.equal(a, c)
    assert torch.equal(st.exact(), st.exact())
    x = st.exact().double().numpy().ravel()
    lo, hi = R.bf16_neighbors(x)
    d = a.double().numpy().ravel()
    assert np.all((d == lo) | (d == hi))  # every draw lands on one of x's two bf16 neighbours


def test_run_tensor_passes_and_records(torch_ao):
    torch = torch_ao
    r = R.run_tensor(_weights(torch, (64, 256)), name="w", device="cpu")
    assert r["pass"], r
    assert r["n_off_grid_draws"] == 0 and r["n_deterministic_violations"] == 0
    assert r["numel"] == 64 * 256 and r["n_draws"] == N
    cmp_ = r["comparison"]
    assert cmp_["frac_x_changed"] > 0.99
    # lr 1e-5 at |w| ~ 0.02 is far below a bf16 ulp: nearest rounding drops most updates
    assert cmp_["frac_unchanged_nearest"] > 0.5
    assert cmp_["frac_unchanged_stochastic_mean"] < cmp_["frac_unchanged_nearest"]


def test_small_tensor_keeps_plain_bf16_state(torch_ao):
    torch = torch_ao
    w = (1.0 + _weights(torch, (100,)).float()).bfloat16()  # norm-like, < 4096 elements
    r = R.run_tensor(w, name="norm", device="cpu")
    assert r["state_layout"]["exp_avg"] == {"kind": "plain", "dtype": "torch.bfloat16"}
    assert r["pass"], r


def test_run_from_sharded_checkpoint_and_cli(torch_ao, tmp_path):
    torch = torch_ao
    from safetensors.torch import save_file

    names = C.ROUNDING_TENSORS
    shapes = {names[0]: (32, 256), names[1]: (32, 512), names[2]: (4096,)}
    t = {n: _weights(torch, s, i) for i, (n, s) in enumerate(shapes.items())}
    t[names[2]] = (1.0 + t[names[2]].float()).bfloat16()
    save_file({names[0]: t[names[0]]}, str(tmp_path / "model-00001-of-00002.safetensors"))
    save_file(
        {names[1]: t[names[1]], names[2]: t[names[2]]},
        str(tmp_path / "model-00002-of-00002.safetensors"),
    )
    wm = {names[0]: "model-00001-of-00002.safetensors"}
    wm |= {n: "model-00002-of-00002.safetensors" for n in names[1:]}
    (tmp_path / "model.safetensors.index.json").write_text(json.dumps({"weight_map": wm}))

    main = _load_cli().main
    out = tmp_path / "run" / "rounding.json"
    rc = main(["--model", str(tmp_path), "--out", str(out), "--draws", "60", "--device", "cpu"])
    rec = json.loads(out.read_text())
    assert rc == 0 and rec["pass"] and set(rec["tensors"]) == set(names)
    assert rec["per_tensor_alpha"] == pytest.approx(0.01 / 3) and rec["n_draws"] == 60
    assert rec["loaded"][names[0]]["file"] == "model-00001-of-00002.safetensors"
    assert rec["tensors"][names[2]]["state_layout"]["exp_avg"]["kind"] == "OptimState8bit"
