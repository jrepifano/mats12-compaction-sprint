"""Batched non-negative orthogonal matching pursuit against a token dictionary (plan.md "Setup").

Ported from ``third_party/olens_draft/oracle_lens/core/nnomp.py`` (MIT, commit a325900), with
three changes so large k (up to 400) is solved, not just started:

1. **Stopping rule:** exactly ``k`` selections; a row stops early only when no unselected atom has
   a positive correlation with its residual (no FVE-gain threshold).
2. **NNLS refit:** accelerated projected gradient (FISTA) with step 1/lambda_max of the selected
   atoms' Gram (power iteration), warm-started from the previous step's coefficients, instead of
   a fixed step 1/trace(G) for 80 iterations (which under-converges badly at k = 400).
3. **Incremental state:** the selected atoms and their Gram are grown one column per step, and
   already-selected atoms are masked explicitly.

The reconstruction ``j = coeffs @ atoms`` and the residual ``r = x - j`` are fp32. Convergence is
reported per row as the relative KKT violation of the final NNLS solve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PursuitResult:
    atoms: Any  # [b, k] long, selection order, -1 where unused
    coeffs: Any  # [b, k] fp32 (0 for unused or zero-weight atoms)
    recon: Any  # [b, d] fp32
    fve: Any  # [b] 1 - |x - recon|^2 / |x|^2
    n_active: Any  # [b] atoms with a positive coefficient
    kkt: Any  # [b] relative KKT violation of the final NNLS solve


def _lmax(gram: Any, iters: int = 30) -> Any:
    """Largest eigenvalue per batch of PSD matrices [b, m, m] (power iteration from a seeded
    random start: an all-ones start can be orthogonal to the top eigenvector, e.g. for two
    anti-correlated atoms, and then returns a smaller eigenvalue and a divergent step)."""
    import torch

    gen = torch.Generator().manual_seed(0)
    v = torch.rand(gram.shape[0], gram.shape[1], 1, generator=gen, dtype=gram.dtype) + 0.1
    v = v.to(gram.device)
    for _ in range(iters):
        v = torch.bmm(gram, v)
        v = v / v.norm(dim=1, keepdim=True).clamp_min(1e-30)
    lam = torch.bmm(v.transpose(1, 2), torch.bmm(gram, v)).squeeze(-1).squeeze(-1)
    return lam * 1.05 + 1e-12  # safety margin: power iteration approaches from below


def nnls_fista(gram: Any, rhs: Any, w0: Any, valid: Any, iters: int) -> Any:
    """min_w>=0 0.5 w'Gw - rhs'w per row, with invalid slots held at 0."""
    import torch

    vf = valid.to(gram.dtype)
    step = (1.0 / _lmax(gram)).unsqueeze(-1)
    w = (w0 * vf).clamp_min(0.0)
    y, t = w.clone(), 1.0
    for _ in range(iters):
        grad = torch.bmm(gram, y.unsqueeze(-1)).squeeze(-1) - rhs
        w_new = ((y - step * grad) * vf).clamp_min(0.0)
        t_new = (1.0 + (1.0 + 4.0 * t * t) ** 0.5) / 2.0
        y = w_new + ((t - 1.0) / t_new) * (w_new - w)
        w, t = w_new, t_new
    return w


def kkt_violation(gram: Any, rhs: Any, w: Any, valid: Any) -> Any:
    """max_i of |grad_i| (w_i > 0) or max(-grad_i, 0) (w_i = 0), over |rhs|_inf."""
    import torch

    grad = torch.bmm(gram, w.unsqueeze(-1)).squeeze(-1) - rhs
    viol = torch.where(w > 0, grad.abs(), (-grad).clamp_min(0.0)) * valid.to(grad.dtype)
    return viol.amax(dim=-1) / rhs.abs().amax(dim=-1).clamp_min(1e-30)


def pursue(
    x: Any,
    dictionary: Any,
    k: int,
    *,
    step_iters: int = 40,
    final_iters: int = 2000,
    chunk: int = 32768,
) -> PursuitResult:
    """Decompose each row of ``x`` [b, d] against unit-norm ``dictionary`` rows [n, d] (rows of
    norm 0 are never selected: their correlation is 0, not positive)."""
    import torch

    x = x.float()
    b, d = x.shape
    n = dictionary.shape[0]
    dev = x.device
    atoms = torch.full((b, k), -1, dtype=torch.long, device=dev)
    sel = torch.zeros(b, k, d, device=dev)
    gram = torch.zeros(b, k, k, device=dev)
    rhs = torch.zeros(b, k, device=dev)
    w = torch.zeros(b, k, device=dev)
    resid = x.clone()
    alive = torch.ones(b, dtype=torch.bool, device=dev)
    scores = torch.empty(b, n, device=dev)
    rows = torch.arange(b, device=dev)
    for s in range(k):
        for lo in range(0, n, chunk):
            scores[:, lo : lo + chunk] = resid @ dictionary[lo : lo + chunk].float().T
        if s:
            prev = atoms[:, :s]
            ok = prev >= 0
            scores[rows.unsqueeze(1).expand_as(prev)[ok], prev[ok]] = float("-inf")
        best, idx = scores.max(dim=-1)
        take = alive & (best > 0)
        if not bool(take.any()):
            break
        alive = take
        atoms[:, s] = torch.where(take, idx, torch.full_like(idx, -1))
        a = dictionary[idx].float() * take.unsqueeze(-1).float()  # [b, d]
        sel[:, s] = a
        g = torch.bmm(sel[:, : s + 1], a.unsqueeze(-1)).squeeze(-1)  # [b, s+1]
        gram[:, s, : s + 1] = g
        gram[:, : s + 1, s] = g
        rhs[:, s] = (a * x).sum(-1)
        valid = atoms[:, : s + 1] >= 0
        w[:, : s + 1] = nnls_fista(
            gram[:, : s + 1, : s + 1], rhs[:, : s + 1], w[:, : s + 1], valid, step_iters
        )
        resid = x - torch.bmm(w[:, : s + 1].unsqueeze(1), sel[:, : s + 1]).squeeze(1)
    valid = atoms >= 0
    w = nnls_fista(gram, rhs, w, valid, final_iters)
    recon = torch.bmm(w.unsqueeze(1), sel).squeeze(1)
    fve = 1.0 - ((x - recon) ** 2).sum(-1) / (x**2).sum(-1).clamp_min(1e-30)
    return PursuitResult(
        atoms=atoms,
        coeffs=w,
        recon=recon,
        fve=fve,
        n_active=(w > 0).sum(-1),
        kkt=kkt_violation(gram, rhs, w, valid),
    )


def pursue_batched(x: Any, dictionary: Any, k: int, *, batch: int = 128, **kw: Any) -> dict:
    """``pursue`` over row batches; returns CPU tensors (recon fp32, atoms, coeffs, stats)."""
    import torch

    outs = []
    for lo in range(0, x.shape[0], batch):
        r = pursue(x[lo : lo + batch].to(dictionary.device), dictionary, k, **kw)
        outs.append(r)
    cat = lambda name: torch.cat([getattr(o, name).cpu() for o in outs])
    return {
        "recon": cat("recon"),
        "atoms": cat("atoms"),
        "coeffs": cat("coeffs"),
        "fve": cat("fve"),
        "n_active": cat("n_active"),
        "kkt": cat("kkt"),
    }
