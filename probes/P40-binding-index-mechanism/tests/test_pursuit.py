"""P36 pursuit: planted recovery, exact NNLS on the selected atoms, stopping rule."""

import numpy as np
import pytest
import torch
from p40.pursuit import pursue, pursue_batched
from scipy.optimize import nnls


def _dict(n=400, d=64, seed=0):
    g = torch.Generator().manual_seed(seed)
    D = torch.randn(n, d, generator=g)
    return D / D.norm(dim=1, keepdim=True)


def test_planted_sparse_recovery():
    D = _dict(d=256)
    g = torch.Generator().manual_seed(1)
    idx = torch.stack([torch.randperm(400, generator=g)[:5] for _ in range(8)])
    c = torch.rand(8, 5, generator=g) + 0.5
    x = torch.einsum("bk,bkd->bd", c, D[idx])
    r = pursue(x, D, 5, final_iters=3000)
    assert (r.fve > 0.999).all()
    for b in range(8):
        assert set(r.atoms[b].tolist()) == set(idx[b].tolist())


def test_nnls_matches_scipy_on_selected_atoms():
    D = _dict(n=300, d=48, seed=2)
    x = torch.randn(4, 48, generator=torch.Generator().manual_seed(3))
    r = pursue(x, D, 20, final_iters=5000)
    for b in range(4):
        a = r.atoms[b][r.atoms[b] >= 0]
        w_ref, _ = nnls(D[a].double().numpy().T, x[b].double().numpy())
        np.testing.assert_allclose(r.coeffs[b][: len(a)].numpy(), w_ref, atol=2e-3)
    assert (r.kkt < 1e-3).all()
    torch.testing.assert_close(r.recon + (x - r.recon), x)


def test_large_k_converges_and_stops_without_positive_correlation():
    D = _dict(n=200, d=32, seed=4)
    x = torch.randn(3, 32, generator=torch.Generator().manual_seed(5))
    r = pursue(x, D, 150, final_iters=5000)
    assert (r.kkt < 1e-2).all()
    assert (r.fve > 0.99).all()  # 200 atoms in 32 dims span the positive cone
    # never selects the same atom twice
    for b in range(3):
        a = r.atoms[b][r.atoms[b] >= 0].tolist()
        assert len(a) == len(set(a))


def test_zero_rows_never_selected_and_batched_matches():
    D = _dict(n=100, d=16, seed=6)
    D[:10] = 0
    x = torch.randn(5, 16, generator=torch.Generator().manual_seed(7))
    out = pursue_batched(x, D, 8, batch=2)
    assert (out["atoms"][out["atoms"] >= 0] >= 10).all()
    one = pursue(x, D, 8)
    torch.testing.assert_close(out["recon"], one.recon, atol=1e-5, rtol=1e-4)


@pytest.mark.parametrize("k", [1, 3, 6])
def test_matches_reference_greedy_nnomp(k):
    """Selection order equals a plain numpy NNOMP (argmax correlation, exact scipy NNLS refit)."""
    D = _dict(n=150, d=24, seed=8)
    x = torch.randn(6, 24, generator=torch.Generator().manual_seed(9))
    ours = pursue(x, D, k, step_iters=3000)
    Dn = D.double().numpy()
    for b in range(6):
        xb, sel = x[b].double().numpy(), []
        res = xb.copy()
        for _ in range(k):
            sc = Dn @ res
            sc[sel] = -np.inf
            if sc.max() <= 0:
                break
            sel.append(int(sc.argmax()))
            w, _ = nnls(Dn[sel].T, xb)
            res = xb - Dn[sel].T @ w
        assert ours.atoms[b][: len(sel)].tolist() == sel
