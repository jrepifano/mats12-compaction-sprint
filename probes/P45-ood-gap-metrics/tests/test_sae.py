"""p45.sae on CPU at toy sizes (plan "Automatic rules: SAE unit tests")."""

from __future__ import annotations

import json
import math

import pytest
import torch
from p45 import sae as S

torch.set_num_threads(4)

D_IN, N_LAT, K_ = 32, 128, 4


def _quiet(*a, **k):
    pass


def _sparse_data(n: int, d: int, m: int, seed: int, offset: float = 0.3):
    """x = sparse codes (4 active of m, magnitudes in [0.5, 1.5]) @ a random unit-norm dictionary."""
    g = torch.Generator().manual_seed(seed)
    D = torch.randn(m, d, generator=g)
    D /= D.norm(dim=1, keepdim=True)

    def make(rows):
        idx = torch.stack([torch.randperm(m, generator=g)[:4] for _ in range(rows)])
        vals = 0.5 + torch.rand(rows, 4, generator=g)
        return torch.zeros(rows, m).scatter(1, idx, vals) @ D + offset

    return make(n), make(n // 8)


def _cfg(**kw):
    base = {"n_latents": N_LAT, "k": K_, "k_aux": 32, "lr": 1e-3, "warmup": 50, "batch": 256,
            "epochs": 3, "eval_every": 100, "seed": 0}
    base.update(kw)
    return S.TrainCfg(**base)


@pytest.fixture(scope="module")
def trained():
    """d = 64 (not 32): with a 128-atom random dictionary in 32 dims the atoms are too coherent
    (max |cos| ~ 0.59) for any linear-TopK encoder; there even the true dictionary scores FVU 0.43
    and training plateaus at ~0.15. In 64 dims training reaches ~0.05."""
    buf, ho = _sparse_data(16384, 64, 128, seed=1)
    s2 = S.sigma2_of(buf)
    sae, cur = S.train_sae(buf, ho, _cfg(epochs=40, eval_every=500), "cpu", s2, log=_quiet)
    return buf, ho, s2, sae, cur


def test_topk_keeps_exactly_k():
    sae = S.TopKSAE(D_IN, N_LAT, K_)
    g = torch.Generator().manual_seed(0)
    with torch.no_grad():
        sae.W_enc.copy_(torch.randn(D_IN, N_LAT, generator=g))
        sae.W_dec.copy_(torch.randn(N_LAT, D_IN, generator=g))
        sae.b_enc.fill_(100.0)  # every pre-activation positive: ReLU keeps all k
    x = torch.randn(50, D_IN, generator=g)
    vals, idx = sae.encode(x)
    assert vals.shape == idx.shape == (50, K_)
    z = torch.zeros(50, N_LAT).scatter(1, idx, vals)
    assert ((z != 0).sum(1) == K_).all()
    sae.b_enc.data.fill_(0.0)  # in general at most k nonzeros
    vals, idx = sae.encode(x)
    assert ((vals > 0).sum(1) <= K_).all() and (vals >= 0).all()


def test_decoder_rows_unit_norm_after_training(trained):
    sae = trained[3]
    assert torch.allclose(sae.W_dec.norm(dim=1), torch.ones(sae.n_latents), atol=1e-5)
    buf, ho = _sparse_data(2048, D_IN, N_LAT, seed=2)
    sae2, _ = S.train_sae(buf, ho, _cfg(max_steps=25), "cpu", S.sigma2_of(buf), log=_quiet)
    assert torch.allclose(sae2.W_dec.norm(dim=1), torch.ones(N_LAT), atol=1e-5)


def test_perfect_reconstruction_has_zero_error():
    # Decoder rows +e_i and -e_i; a signed 4-sparse x is reconstructed exactly with k = 4.
    d, n = D_IN, 2 * D_IN
    E = torch.eye(d)
    sae = S.TopKSAE(d, n, K_, scale=3.0)
    with torch.no_grad():
        sae.W_dec.copy_(torch.cat([E, -E]))
        sae.W_enc.copy_(sae.W_dec.T)
    g = torch.Generator().manual_seed(0)
    idx = torch.stack([torch.randperm(d, generator=g)[:K_] for _ in range(200)])
    sign = torch.where(torch.rand(200, K_, generator=g) < 0.5, -1.0, 1.0)
    x = torch.zeros(200, d).scatter(1, idx, sign * (0.5 + torch.rand(200, K_, generator=g)))
    assert float(sae.sq_err(x).max()) < 1e-10
    # b_pre-only data: every row equal to b_pre (raw), error 0 whatever fires.
    bp = torch.randn(d, generator=g)
    sae2 = S.TopKSAE(d, n, K_, scale=2.0, b_pre=bp * 2.0)
    with torch.no_grad():
        sae2.b_enc.fill_(-1e4)
    assert float(sae2.sq_err(bp.expand(10, d).clone()).max()) < 1e-10


def test_mean_reconstruction_has_fvu_one():
    buf, _ = _sparse_data(3000, D_IN, N_LAT, seed=3)
    scale = S.fit_scale(buf)
    mu = buf.to(torch.float64).mean(0).float()
    sae = S.TopKSAE(D_IN, N_LAT, K_, scale=scale, b_pre=mu * scale)
    with torch.no_grad():
        sae.b_enc.fill_(-1e4)  # no latent fires: x_hat = b_pre
    s2 = S.sigma2_of(buf)
    assert S.agg_fvu(sae, buf, s2) == pytest.approx(1.0, abs=1e-4)


def test_trains_to_low_fvu(trained):
    _, _, _, _, cur = trained
    f = cur["final"]
    assert f["fvu_train"] < 0.1 and f["fvu_heldout"] < 0.1
    assert f["seconds"] < 20
    assert cur["loss"][-1] < cur["loss"][0]
    json.dumps(cur)  # JSON-serialisable
    assert cur["eval_step"][-1] == f["steps"] and cur["step"][-1] == f["steps"]
    assert len(cur["step"]) == len(cur["loss"]) == len(cur["fvu_batch"])
    assert cur["step"] == list(range(1, f["steps"] + 1))  # every step recorded
    assert len(cur["eval_step"]) == len(cur["fvu_heldout"]) == len(cur["dead_frac"])
    assert 0.0 <= f["dead_frac"] <= 1.0 and f["ms_per_step"] > 0


def test_sq_err_batch_invariant(trained):
    _, ho, _, sae, _ = trained
    ref = sae.sq_err(ho, chunk=4096)
    for c in (1, 7, 256, 1000):
        assert torch.allclose(sae.sq_err(ho, chunk=c), ref, rtol=1e-5, atol=1e-6)
    # half precision inputs are accepted
    assert sae.sq_err(ho.half()).dtype == torch.float32


def test_save_load_round_trip(trained, tmp_path):
    _, ho, _, sae, _ = trained
    stem = tmp_path / "sub" / "post_L16"
    sae.save(stem, info={"arm": "post"})
    assert stem.with_suffix(".safetensors").exists()
    cfg = json.loads(stem.with_suffix(".json").read_text())
    assert cfg["info"] == {"arm": "post"} and cfg["k"] == sae.k and cfg["scale"] == sae.scale
    sae2 = S.TopKSAE.load(stem)
    for n, p in sae.named_parameters():
        q = dict(sae2.named_parameters())[n]
        assert q.dtype == torch.float32
        assert torch.equal(q, p.detach().half().float())
    # fp16 storage changes the error only slightly
    e1, e2 = sae.sq_err(ho), sae2.sq_err(ho)
    assert float((e1 - e2).abs().sum() / e1.sum()) < 2e-2


def test_scale_handling():
    buf, ho = _sparse_data(4096, D_IN, N_LAT, seed=4)
    cfg = _cfg(max_steps=200)
    _, c1 = S.train_sae(buf, ho, cfg, "cpu", S.sigma2_of(buf), log=_quiet)
    _, c2 = S.train_sae(buf * 100, ho * 100, cfg, "cpu", S.sigma2_of(buf * 100), log=_quiet)
    assert c2["final"]["scale"] == pytest.approx(c1["final"]["scale"] / 100, rel=1e-6)
    for key in ("fvu_train", "fvu_heldout"):
        assert c2["final"][key] == pytest.approx(c1["final"][key], rel=1e-3)
    assert S.fit_scale(buf * 100) ** 2 * float((buf * 100).pow(2).sum(1).mean()) == \
        pytest.approx(D_IN, rel=1e-6)


def test_deterministic_on_cpu():
    buf, ho = _sparse_data(2048, D_IN, N_LAT, seed=5)
    cfg = _cfg(max_steps=40)
    a, ca = S.train_sae(buf, ho, cfg, "cpu", 1.0, log=_quiet)
    b, cb = S.train_sae(buf, ho, cfg, "cpu", 1.0, log=_quiet)
    assert torch.equal(a.W_dec, b.W_dec) and ca["loss"] == cb["loss"]


def test_aux_runs_on_dead_latents():
    # 64 atoms, 128 latents: some latents die; AuxK must not break training and stays finite.
    buf, ho = _sparse_data(2048, D_IN, 64, seed=6)
    _, cur = S.train_sae(buf, ho, _cfg(epochs=6), "cpu", S.sigma2_of(buf), log=_quiet)
    assert all(math.isfinite(v) for v in cur["loss"])


def test_sigma2_of():
    g = torch.Generator().manual_seed(0)
    x = torch.randn(20000, 8, generator=g) * 2 + 5
    ref = float(((x.double() - x.double().mean(0)) ** 2).sum(1).mean())
    assert S.sigma2_of(x) == pytest.approx(ref, rel=1e-9)
    assert S.sigma2_of(x.half()) == pytest.approx(32.0, rel=2e-2)


def test_mask_keep_drops_exactly_high_norm_rows():
    norms = torch.tensor([1.0, 9.99, 10.0, 10.01, 50.0, 0.0])
    keep = S.mask_keep(norms, median_norm=1.0)
    assert keep.tolist() == [True, True, True, False, False, True]
    assert S.mask_keep(norms, median_norm=1.0, mult=2.0).tolist() == \
        [True, False, False, False, False, True]


def test_prompt_F_segments_and_nan():
    err = torch.tensor([1.0, 3.0, 100.0, 2.0, 4.0, 6.0])
    keep = torch.tensor([True, True, False, False, False, True])
    F = S.prompt_F(err, keep, [(0, 3), (3, 5), (5, 6)], sigma2=2.0)
    assert F[0] == pytest.approx(1.0)  # mean(1, 3) / 2
    assert math.isnan(F[1])  # all masked
    assert F[2] == pytest.approx(3.0)


def test_bench_tiny():
    ms = S.bench(D_IN, N_LAT, K_, batch=64, device="cpu", steps=3)
    assert ms > 0


def test_prompt_F_masked_nonfinite_does_not_leak():
    se = torch.tensor([1.0, float("inf"), float("nan"), 2.0, 4.0])
    keep = torch.tensor([True, False, False, True, True])
    out = S.prompt_F(se, keep, [(0, 3), (3, 5)], 2.0)
    assert out == [0.5, 1.5]
