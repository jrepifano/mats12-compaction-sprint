"""P39 arm math (plan.md "Setup", "Arms"): pure torch, fp32, CPU or GPU.

Every arm is a change ``delta`` [n, 2, d] added to the recipient's two entity-token states at
L48 (slot 0 = the alphabetically first entity e1, slot 1 = e2, as in P36), computed from the
twin difference ``D = h_donor - h_recipient`` [n, 2, d] of the same entity tokens.

- Subspace slices: ``project(D, B)`` with an orthonormal basis ``B`` [d, m] (Haar-random,
  covariance-matched, PCA of twin differences, the span of random J-lens atoms, a 1-D axis).
- Matching: ``match(delta, ref)`` rescales each token's change to the norm of ``ref`` (the J-lens
  k = 25 swap's change at that token), so that both have the same share of the twin difference.
- Fitted directions (on P36's train items only): ``pca_basis``, ``order_axis`` / ``role_axis``
  (S-Space's role-antisymmetric gradient average, oriented by mention order or by thematic role),
  ``mean_diff_axis``.
- Per-case statistics: norm share, linear share, first-order attribution terms, axis capture.
"""

from __future__ import annotations

from typing import Any

EPS = 1e-12


def _gen(seed: int) -> Any:
    import torch

    return torch.Generator().manual_seed(int(seed))


def orthonormal(X: Any) -> Any:
    """Orthonormal basis [d, m] of the columns of X [d, m] (reduced QR, signs fixed so the
    diagonal of R is positive: Haar when X is i.i.d. Gaussian)."""
    import torch

    Q, R = torch.linalg.qr(X.double(), mode="reduced")
    s = torch.sign(torch.diagonal(R))
    s[s == 0] = 1.0
    return (Q * s).float()


def haar_basis(d: int, m: int, seed: int) -> Any:
    """A Haar-random m-dimensional subspace of R^d, as an orthonormal [d, m] basis."""
    import torch

    return orthonormal(torch.randn(d, m, generator=_gen(seed)))


def cov_sqrt(states: Any) -> Any:
    """Symmetric square root of the (centred) covariance of ``states`` [N, d], as fp32 [d, d]."""
    import torch

    X = states.double()
    X = X - X.mean(0, keepdim=True)
    C = X.T @ X / max(1, X.shape[0] - 1)
    w, V = torch.linalg.eigh(C)
    return ((V * w.clamp_min(0).sqrt()) @ V.T).float()


def cov_basis(sqrt_cov: Any, m: int, seed: int) -> Any:
    """An m-dimensional subspace spanned by m draws from N(0, Sigma) (Lee & Heimersheim's
    covariance-matched random directions), orthonormalised [d, m]."""
    import torch

    d = sqrt_cov.shape[0]
    Z = torch.randn(d, m, generator=_gen(seed))
    return orthonormal(sqrt_cov.float().cpu() @ Z)


def rotation(d: int, seed: int) -> Any:
    """A Haar-random orthogonal matrix [d, d]."""
    return haar_basis(d, d, seed)


def project(D: Any, B: Any) -> Any:
    """Orthogonal projection of D [..., d] onto span(B), B [d, m] orthonormal."""
    B = B.to(D.device, D.dtype)
    return (D @ B) @ B.T


def axis_component(D: Any, u: Any) -> Any:
    """(u.D) u for a unit vector u [d]."""
    u = u.to(D.device, D.dtype)
    return (D @ u).unsqueeze(-1) * u


def match(delta: Any, ref: Any) -> Any:
    """Rescale each token's change [..., d] to the norm of ``ref`` at that token (0 stays 0)."""
    n = delta.norm(dim=-1, keepdim=True)
    r = ref.norm(dim=-1, keepdim=True)
    return delta * (r / n.clamp_min(EPS)) * (n > EPS)


def rescale_to(delta: Any, norms: Any) -> Any:
    """Rescale each token's change to the given norms [...] (0 stays 0)."""
    n = delta.norm(dim=-1, keepdim=True)
    return delta * (norms.unsqueeze(-1) / n.clamp_min(EPS)) * (n > EPS)


def unit(v: Any) -> Any:
    return v / v.norm().clamp_min(EPS)


# --------------------------------------------------------------------- fitted directions


def pca_basis(D_fit: Any, r_max: int) -> tuple[Any, Any]:
    """Top-r_max right singular vectors [d, r_max] of the stacked twin differences D_fit [n, d]
    (uncentred: the twin swap is sign-symmetric) and the fit set's mean share of ||D||^2 captured
    by the top r directions, r = 1..r_max ([r_max])."""
    import torch

    X = D_fit.double()
    _, _, Vh = torch.linalg.svd(X, full_matrices=False)
    V = Vh[:r_max].T  # [d, r_max]
    proj2 = (X @ V) ** 2  # [n, r_max]
    tot = (X**2).sum(-1, keepdim=True).clamp_min(EPS)
    share = (proj2.cumsum(-1) / tot).mean(0)
    return V.float(), share.float()


def smallest_r(share_by_r: Any, target: float) -> int:
    """The smallest r whose mean share >= target (r_max if none)."""
    ok = (share_by_r >= target).nonzero()
    return int(ok[0, 0]) + 1 if len(ok) else int(share_by_r.shape[0])


def order_axis(g: Any, first: Any) -> Any:
    """S-Space's role-antisymmetric gradient average, oriented by mention order (unit [d]).

    ``g`` [n, 2, d]: gradients of LD = logit(correct answer) - logit(other entity) at the two
    entity tokens of n prompts; ``first`` [n] long: which slot holds the first-mentioned entity.
    With LD_fs = logit(first) - logit(second) = sign * LD (sign = +1 when the answer is the first
    mention), S-Space's per-prompt term is a = grad_first LD_fs - grad_second LD_fs; oriented by
    sigma = sign, sigma * a = grad_first LD - grad_second LD, which this averages."""
    import torch

    idx = torch.arange(g.shape[0])
    gf, gs = g[idx, first], g[idx, 1 - first]
    return unit((gf - gs).double().mean(0)).float()


def role_axis(g: Any, first: Any, answer_is_first: Any, agent_q: Any) -> Any:
    """The same terms oriented by thematic role (tau = +1 under "Who did...", -1 under "Who
    was..."): tau * a = tau * sign * (grad_first LD - grad_second LD). Under a pure mention-order
    code this cancels across voices (plan.md, arm 4b)."""
    import torch

    idx = torch.arange(g.shape[0])
    gf, gs = g[idx, first], g[idx, 1 - first]
    sign = answer_is_first.double() * 2 - 1
    tau = agent_q.double() * 2 - 1
    w = (sign * tau).unsqueeze(-1)
    return unit((w * (gf - gs).double()).mean(0)).float()


def mean_diff_axis(D: Any, first_in_donor: Any) -> Any:
    """Mean of twin differences oriented first-mention minus second-mention (unit [d]).

    ``D`` [n, d]: h_donor - h_recipient for one entity token of n twin pairs;
    ``first_in_donor`` [n] bool: the entity is first-mentioned in the donor sentence."""
    s = first_in_donor.double() * 2 - 1
    return unit((s.unsqueeze(-1) * D.double()).mean(0)).float()


def split_axis(u: Any, B: Any) -> tuple[Any, Any]:
    """u's normalised part inside span(B) and its normalised orthogonal part."""
    inside = project(u.unsqueeze(0), B)[0]
    return unit(inside), unit(u - inside)


# --------------------------------------------------------------------- per-case statistics


def case_stats(
    delta: Any, D: Any, g: dict[str, Any] | None = None, axes: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Per case (summed over the two tokens): ``share`` = ||delta||^2 / ||D||^2, ``lin`` =
    <delta, D> / ||D||^2, per question q in ``g`` the attribution terms ``gd_<q>`` = g.delta and
    ``gD_<q>`` = g.D, and per axis u in ``axes`` ``cap_<name>`` = <delta, u> / <D, u>."""
    D2 = (D**2).sum((-1, -2)).clamp_min(EPS)
    out = {
        "share": (delta**2).sum((-1, -2)) / D2,
        "lin": (delta * D).sum((-1, -2)) / D2,
        "dnorm": delta.norm(dim=-1),  # [n, 2]
        "Dnorm": D.norm(dim=-1),
    }
    for q, gq in (g or {}).items():
        out[f"gd_{q}"] = (gq * delta).sum((-1, -2))
        out[f"gD_{q}"] = (gq * D).sum((-1, -2))
    for name, u in (axes or {}).items():
        u = u.to(D.device, D.dtype)
        den = (D @ u).sum(-1)
        out[f"cap_{name}"] = (delta @ u).sum(-1) / den.where(den.abs() > EPS, EPS)
    return out
