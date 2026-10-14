"""P42 scorers on stored features (contracts.py score-column comment; plan.md "Scorers").

Higher = more OOD for every column. All fits use reference rows only:
- Mahalanobis (S3a/S3d): mean and Ledoit-Wolf-shrunk covariance of the reference features, in
  float64 (sklearn.covariance.ledoit_wolf's estimator, re-implemented in torch so it runs on the
  pod's GPU and needs no scikit-learn there; tests compare it with sklearn). Score = the
  Mahalanobis DISTANCE sqrt((x - mu)^T Sigma^-1 (x - mu)) (not squared; monotone either way,
  but the Delta-model difference and z-scores use the distance), via a Cholesky solve.
- kNN cosine (S3b/S3c, S4): centre queries and references by the reference mean, L2-normalise,
  score = 1 - mean of the top-k cosines to the reference (chunked matmuls, float32).
  S4's gradients are already unit vectors and are used as stored (no centring): LESS's cosine.
- z-scores (S3c/S3d): (x - mean) / std (ddof 0) over the SCORED_SETS rows, NaN-ignoring.
- PCA: top-`dim` eigenvectors of the reference covariance (float64 eigh), projections of
  centred features.
Inputs are numpy arrays (memmaps are fine) or torch tensors; computation happens on `device`.
"""

from __future__ import annotations

import csv
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import torch

CHUNK = 4096


def _t(x, device, dtype) -> torch.Tensor:
    if isinstance(x, torch.Tensor):
        return x.to(device=device, dtype=dtype)
    return torch.as_tensor(np.asarray(x), device=device).to(dtype)


def ledoit_wolf(X, device="cpu", block: int = 8192) -> tuple[torch.Tensor, torch.Tensor, float]:
    """(mean, shrunk covariance, shrinkage), float64; sklearn's ledoit_wolf (assume_centered=False):
    cov = (1 - s) S + s mu I, S the MLE covariance, mu = tr(S) / p."""
    n = X.shape[0]
    mean = torch.zeros(X.shape[1], dtype=torch.float64, device=device)
    for s in range(0, n, block):
        mean += _t(X[s : s + block], device, torch.float64).sum(0)
    mean /= n
    p = X.shape[1]
    xtx = torch.zeros(p, p, dtype=torch.float64, device=device)
    x2tx2 = torch.zeros(p, p, dtype=torch.float64, device=device)
    for s in range(0, n, block):
        xc = _t(X[s : s + block], device, torch.float64) - mean
        xtx += xc.T @ xc
        x2 = xc * xc
        x2tx2 += x2.T @ x2
    emp_cov = xtx / n
    emp_cov_trace = torch.diagonal(emp_cov)  # = sum(X^2, 0) / n
    mu = float(emp_cov_trace.sum()) / p
    beta_ = float(x2tx2.sum())
    delta_ = float((xtx * xtx).sum()) / n ** 2
    beta = 1.0 / (p * n) * (beta_ / n - delta_)
    delta = (delta_ - 2.0 * mu * float(emp_cov_trace.sum()) + p * mu ** 2) / p
    beta = min(beta, delta)
    shrink = 0.0 if beta == 0 else beta / delta
    cov = (1.0 - shrink) * emp_cov
    cov.diagonal().add_(shrink * mu)
    return mean, cov, shrink


class Mahalanobis:
    def __init__(self, X_ref, device="cpu"):
        self.device = device
        self.mean, cov, self.shrinkage = ledoit_wolf(X_ref, device)
        self.chol = torch.linalg.cholesky(cov)

    def score(self, Q) -> np.ndarray:
        out = []
        for s in range(0, Q.shape[0], CHUNK):
            d = (_t(Q[s : s + CHUNK], self.device, torch.float64) - self.mean).T  # [p, m]
            z = torch.linalg.solve_triangular(self.chol, d, upper=False)
            out.append(torch.sqrt((z * z).sum(0)).cpu().numpy())
        return np.concatenate(out) if out else np.zeros(0)


def knn_score(Q, R, k: int, device="cpu", centre: bool = True) -> np.ndarray:
    """1 - mean top-k cosine of each query row to the reference rows."""
    Rt = _t(R, device, torch.float32)
    mu = Rt.mean(0) if centre else torch.zeros(Rt.shape[1], device=device)
    Rn = torch.nn.functional.normalize(Rt - mu, dim=1)
    kk = min(k, Rn.shape[0])
    out = []
    for s in range(0, Q.shape[0], CHUNK):
        q = torch.nn.functional.normalize(_t(Q[s : s + CHUNK], device, torch.float32) - mu, dim=1)
        top = torch.topk(q @ Rn.T, kk, dim=1).values
        out.append((1.0 - top.mean(1)).cpu().numpy().astype(np.float64))
    return np.concatenate(out) if out else np.zeros(0)


def zscore(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    m = np.nanmean(x)
    sd = np.nanstd(x)
    return (x - m) / sd if sd > 0 else np.full_like(x, np.nan)


def pca_fit(X_ref, dim: int, device="cpu", block: int = 8192) -> tuple[torch.Tensor, torch.Tensor]:
    """(mean [p], components [p, dim]) of the reference rows, float64; sign fixed so each
    component's largest-|.| loading is positive (deterministic)."""
    n, p = X_ref.shape
    mean = torch.zeros(p, dtype=torch.float64, device=device)
    for s in range(0, n, block):
        mean += _t(X_ref[s : s + block], device, torch.float64).sum(0)
    mean /= n
    cov = torch.zeros(p, p, dtype=torch.float64, device=device)
    for s in range(0, n, block):
        xc = _t(X_ref[s : s + block], device, torch.float64) - mean
        cov += xc.T @ xc
    cov /= max(n - 1, 1)
    evals, evecs = torch.linalg.eigh(cov)
    comps = evecs[:, torch.argsort(evals, descending=True)[:dim]]
    sign = torch.sign(comps.gather(0, comps.abs().argmax(0, keepdim=True)))
    return mean, comps * sign


def pca_project(X, mean, comps, device="cpu") -> np.ndarray:
    out = []
    for s in range(0, X.shape[0], CHUNK):
        x = _t(X[s : s + CHUNK], device, torch.float64) - mean
        out.append((x @ comps).cpu().numpy())
    return np.concatenate(out) if out else np.zeros((0, comps.shape[1]))


def finite_rows(X, rows: np.ndarray) -> np.ndarray:
    """The subset of `rows` whose feature row is all finite (unfilled rows are NaN or missing)."""
    rows = np.asarray(rows, dtype=np.int64)
    ok = np.ones(len(rows), dtype=bool)
    for s in range(0, len(rows), CHUNK):
        blk = np.asarray(X[rows[s : s + CHUNK]], dtype=np.float32)
        ok[s : s + CHUNK] = np.isfinite(blk).all(1)
    return rows[ok]


def write_scores_csv(path: str | Path, id_cols: Sequence[str], score_cols: Sequence[str],
                     ids: Mapping[str, Sequence], scores: Mapping[str, np.ndarray]) -> None:
    """One row per scored prompt: id_cols then score_cols (missing column = all NaN; NaN written
    as empty-string-free 'nan'). Written beside the target, then moved in."""
    n = len(ids[id_cols[0]])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([*id_cols, *score_cols])
        cols = [np.asarray(scores.get(c, np.full(n, np.nan)), dtype=np.float64) for c in score_cols]
        for c, col in zip(score_cols, cols, strict=True):
            if len(col) != n:
                raise ValueError(f"column {c}: {len(col)} values for {n} rows")
        for i in range(n):
            w.writerow([*(ids[c][i] if ids[c][i] is not None else "" for c in id_cols),
                        *(_fmt(col[i]) for col in cols)])
    os.replace(tmp, path)


def _fmt(v: float) -> str:
    return "nan" if not math.isfinite(v) else repr(float(v))
