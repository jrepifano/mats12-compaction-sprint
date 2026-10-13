"""P39 arm math: projections, matching, fitted directions, case statistics."""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")

from p39 import arms as A


def test_haar_basis_orthonormal_and_seeded():
    B = A.haar_basis(64, 10, seed=1)
    torch.testing.assert_close(B.T @ B, torch.eye(10), atol=1e-5, rtol=0)
    assert torch.equal(B, A.haar_basis(64, 10, seed=1))
    assert not torch.equal(B, A.haar_basis(64, 10, seed=2))


def test_random_slice_share_is_about_m_over_d():
    d, m = 512, 64
    D = torch.randn(400, 2, d)
    shares = []
    for s in range(5):
        P = A.project(D, A.haar_basis(d, m, seed=s))
        shares.append(float(A.case_stats(P, D)["share"].mean()))
    assert abs(sum(shares) / 5 - m / d) < 0.01


def test_projection_is_idempotent_and_lin_equals_share():
    D = torch.randn(10, 2, 32)
    B = A.haar_basis(32, 5, 0)
    P = A.project(D, B)
    torch.testing.assert_close(A.project(P, B), P, atol=1e-5, rtol=1e-5)
    st = A.case_stats(P, D)
    torch.testing.assert_close(st["share"], st["lin"], atol=1e-5, rtol=1e-4)


def test_match_rescales_per_token():
    delta = torch.randn(6, 2, 16)
    ref = torch.randn(6, 2, 16) * 3
    m = A.match(delta, ref)
    torch.testing.assert_close(m.norm(dim=-1), ref.norm(dim=-1), atol=1e-5, rtol=1e-5)
    # direction kept
    cos = torch.nn.functional.cosine_similarity(m, delta, dim=-1)
    torch.testing.assert_close(cos, torch.ones_like(cos), atol=1e-5, rtol=0)
    z = A.match(torch.zeros(1, 2, 16), ref[:1])
    assert torch.equal(z, torch.zeros(1, 2, 16))


def test_cov_basis_prefers_high_variance_directions():
    d = 40
    scale = torch.ones(d)
    scale[:3] = 30.0
    states = torch.randn(5000, d) * scale
    S = A.cov_sqrt(states)
    B = A.cov_basis(S, 3, seed=0)
    # most of the basis lies in the first 3 coordinates
    assert float((B[:3] ** 2).sum()) / 3 > 0.9
    Bi = A.haar_basis(d, 3, 0)
    assert float((Bi[:3] ** 2).sum()) / 3 < 0.5


def test_pca_basis_and_smallest_r():
    d = 50
    u = A.unit(torch.randn(d))
    coef = torch.randn(300) * 5
    D = coef.unsqueeze(-1) * u + 0.3 * torch.randn(300, d)
    V, share = A.pca_basis(D, 8)
    assert abs(float(V[:, 0] @ u)) > 0.99
    assert share.shape == (8,) and bool((share[1:] >= share[:-1]).all())
    assert A.smallest_r(share, float(share[0]) - 1e-6) == 1
    assert A.smallest_r(share, 2.0) == 8


def _toy_grads(n=400, d=24, seed=0):
    """Gradients from a pure mention-order code. LD = logit(answer) - logit(other) rises when the
    first-mentioned token moves along +order and the second along -order, whichever is the answer:
    if the answer is first, it becomes more first-like and its rival less; if the answer is second,
    the rival (first) becomes less like the answer's index and the answer more."""
    g0 = torch.Generator().manual_seed(seed)
    order = A.unit(torch.randn(d, generator=g0))
    first = torch.randint(0, 2, (n,), generator=g0)
    # balanced voices and questions, as in P36's stimuli
    agent_q = (torch.arange(n) // 2) % 2 == 0
    active = torch.arange(n) % 2 == 0
    # the answer is the first mention for (agent, active) and (patient, passive)
    ans_first = agent_q == active
    g = 0.2 * torch.randn(n, 2, d, generator=g0)
    idx = torch.arange(n)
    g[idx, first] += order
    g[idx, 1 - first] -= order
    return g, first, ans_first, agent_q, order


def test_order_axis_recovers_order_and_role_axis_cancels():
    g, first, ans_first, agent_q, order = _toy_grads()
    u = A.order_axis(g, first)
    assert float(u @ order) > 0.95
    v = A.role_axis(g, first, ans_first, agent_q)
    assert abs(float(v @ order)) < 0.3


def test_mean_diff_axis_orientation():
    d = 16
    u = A.unit(torch.randn(d))
    fid = torch.tensor([True, False] * 50)
    D = ((fid.float() * 2 - 1).unsqueeze(-1) * u) * 4 + 0.1 * torch.randn(100, d)
    assert float(A.mean_diff_axis(D, fid) @ u) > 0.99


def test_split_axis_parts_are_orthogonal():
    B = A.haar_basis(20, 4, 0)
    u = A.unit(torch.randn(20))
    a, b = A.split_axis(u, B)
    assert abs(float(a @ b)) < 1e-5
    torch.testing.assert_close(A.project(b.unsqueeze(0), B)[0], torch.zeros(20), atol=1e-5, rtol=0)


def test_case_stats_attribution_and_capture():
    D = torch.randn(5, 2, 8)
    g = {"agent": torch.randn(5, 2, 8)}
    st = A.case_stats(D, D, g, {"u": A.unit(torch.randn(8))})
    torch.testing.assert_close(st["share"], torch.ones(5))
    torch.testing.assert_close(st["gd_agent"], st["gD_agent"])
    torch.testing.assert_close(st["cap_u"], torch.ones(5), atol=1e-5, rtol=1e-5)
    z = A.case_stats(torch.zeros_like(D), D, g)
    assert torch.equal(z["gd_agent"], torch.zeros(5))
