"""Condition probes on residual activations (pod; plan.md "Detectors", A_org / A_base).

Per layer, fitted on TRAIN only (labels: 1 = unobserved notice, 0 = observed):
- `lr`: standardise with TRAIN's mean and sd, then L2 logistic regression (mean BCE + lam/2 ||w||^2,
  full-batch L-BFGS); lam from LAMBDAS by grouped K-fold CV log-loss (groups = notice ids, so no
  notice is in both a fold's train and test part).
- `dm`: difference of means on the raw activations, w = mean(unobs) - mean(obs); score = x . w.
- `lr_shuf`: `lr` at the chosen lam with labels permuted across notices (group-level, seeded):
  a null for how much a fitted direction separates anything.
Scores are oriented so higher = unobserved. torch is imported inside functions; CPU or CUDA.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

LAMBDAS = (1e-3, 1e-2, 1e-1)
FOLDS = 3
TYPES = ("lr", "dm", "lr_shuf")


def _lbfgs(Z, y, lam: float, iters: int = 100):
    import torch

    w = torch.zeros(Z.shape[1], device=Z.device, requires_grad=True)
    b = torch.zeros((), device=Z.device, requires_grad=True)
    opt = torch.optim.LBFGS([w, b], lr=1.0, max_iter=iters, line_search_fn="strong_wolfe")
    bce = torch.nn.BCEWithLogitsLoss()

    def closure():
        opt.zero_grad()
        loss = bce(Z @ w + b, y) + 0.5 * lam * (w * w).sum()
        loss.backward()
        return loss

    opt.step(closure)
    return w.detach(), b.detach()


def _folds(groups: Sequence[str], k: int, seed: int) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    g = np.asarray(groups)
    uniq = np.array(sorted(set(g.tolist())))
    rng.shuffle(uniq)
    fold_of = {u: i % k for i, u in enumerate(uniq)}
    f = np.array([fold_of[x] for x in g])
    return [np.where(f == i)[0] for i in range(k)]


def _shuffle_labels(y: np.ndarray, groups: Sequence[str], seed: int) -> np.ndarray:
    """Labels permuted across groups (each group keeps one label, drawn without replacement)."""
    g = np.asarray(groups)
    uniq = sorted(set(g.tolist()))
    lab = {u: int(y[np.where(g == u)[0][0]]) for u in uniq}
    perm = np.random.default_rng(seed).permutation([lab[u] for u in uniq])
    new = dict(zip(uniq, perm.tolist(), strict=True))
    return np.array([new[x] for x in g], dtype=y.dtype)


def fit_layer(X, y: np.ndarray, groups: Sequence[str], seed: int, device: str = "cpu") -> dict:
    """Probe parameters of one layer. X: [n, d] array (any float dtype)."""
    import torch

    Xt = torch.as_tensor(np.asarray(X, np.float32), device=device)
    yt = torch.as_tensor(y.astype(np.float32), device=device)
    mu, sd = Xt.mean(0), Xt.std(0) + 1e-4
    Z = (Xt - mu) / sd
    folds = _folds(groups, FOLDS, seed)
    bce = torch.nn.BCEWithLogitsLoss(reduction="sum")
    cv = {}
    for lam in LAMBDAS:
        tot = 0.0
        for te in folds:
            tr = np.setdiff1d(np.arange(len(y)), te)
            w, b = _lbfgs(Z[tr], yt[tr], lam)
            tot += float(bce(Z[te] @ w + b, yt[te]))
        cv[lam] = tot / len(y)
    lam = min(LAMBDAS, key=lambda v: (cv[v], -v))
    w, b = _lbfgs(Z, yt, lam)
    ys = torch.as_tensor(_shuffle_labels(y, groups, seed).astype(np.float32), device=device)
    ws, bs = _lbfgs(Z, ys, lam)
    dm = Xt[yt == 1].mean(0) - Xt[yt == 0].mean(0)
    return {"mu": mu.cpu().numpy(), "sd": sd.cpu().numpy(), "lam": lam, "cv": cv,
            "lr": (w.cpu().numpy(), float(b)), "lr_shuf": (ws.cpu().numpy(), float(bs)),
            "dm": dm.cpu().numpy()}


def score_layer(X, p: dict) -> dict[str, np.ndarray]:
    """{type: [n] scores} for one layer's activations X [n, d]."""
    X = np.asarray(X, np.float32)
    Z = (X - p["mu"]) / p["sd"]
    return {"lr": Z @ p["lr"][0] + p["lr"][1], "lr_shuf": Z @ p["lr_shuf"][0] + p["lr_shuf"][1],
            "dm": X @ p["dm"]}


def fit_all(X, y: np.ndarray, groups: Sequence[str], seed: int, device: str = "cpu") -> list[dict]:
    """One `fit_layer` per layer of X [n, n_layers, d]."""
    return [fit_layer(X[:, li], y, groups, seed + li, device) for li in range(X.shape[1])]


def score_all(X, params: list[dict]) -> np.ndarray:
    """[n, len(TYPES), n_layers] scores of X [n, n_layers, d]."""
    n, L = X.shape[0], X.shape[1]
    out = np.zeros((n, len(TYPES), L), np.float32)
    for li, p in enumerate(params):
        s = score_layer(X[:, li], p)
        for ti, t in enumerate(TYPES):
            out[:, ti, li] = s[t]
    return out
