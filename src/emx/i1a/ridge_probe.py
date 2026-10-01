"""Ridge probe baseline on multi-hop (prereg v0.4 §1; implementation plan §0 and §2 U6).

* **Target** per item: the mean unembedding row of ``" " + concept`` (tokenized without special
  tokens), where the concept is ``units[0]["match"][0]`` if the item has units, else
  ``intermediates[0]``, else ``target`` (wsbench ``banks.item_targets`` precedence, first entry).
* **Fit** per layer: ``Pipeline(StandardScaler, Ridge(alpha))``, so standardization is fit inside
  each training fold. Nested CV: ``RIDGE_OUTER_FOLDS`` outer folds give out-of-fold predictions;
  inside each outer training set, ``RIDGE_INNER_FOLDS`` inner folds pick alpha from
  ``RIDGE_ALPHAS`` by the mean held-out **cosine** between prediction and target rows (the readout
  ``top-k(W_U @ y_hat)`` depends only on the direction of ``y_hat``, so cosine is the criterion
  that matches it; R² would also reward matching the norm). Ties go to the first alpha in
  ``RIDGE_ALPHAS`` order (the smallest).
* **Folds**: ``GroupKFold(shuffle=True, random_state=seed)`` over item ids. Fold assignment
  depends only on the groups and the seed, so an item sits in the same outer fold at every layer
  and no fold ever splits an item's rows. Inner folds use ``seed + 1 + outer_fold``.
* **Readout**: top 10 of ``W_U @ y_hat`` per (item, layer) as token-arm contract rows
  ``{"id","layer","pos","token","tokens","scores"}`` (tokens via wsbench ``display_tokens``).

numpy + scikit-learn only at import; torch and wsbench are imported lazily (the pod has them).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from emx.i1a import contracts as C

TOPK = 10


def probe_concept(item: Mapping[str, Any]) -> str:
    units = item.get("units") or []
    if units and units[0].get("match"):
        return str(units[0]["match"][0])
    inter = item.get("intermediates") or []
    if inter:
        return str(inter[0])
    return str(item["target"])


def _to_numpy(x: Any) -> np.ndarray:
    if hasattr(x, "detach"):
        x = x.detach().float().cpu().numpy()
    return np.asarray(x)


def probe_targets(tokenizer: Any, W_U: Any, specs: Sequence[Mapping[str, Any]]) -> np.ndarray:
    """[n, d] float32: mean ``W_U`` row of each spec's concept tokens (``W_U`` is [vocab, d],
    numpy or torch)."""
    rows = []
    for spec in specs:
        ids = tokenizer(" " + probe_concept(spec), add_special_tokens=False)["input_ids"]
        if not ids:
            raise ValueError(f"empty tokenization for concept of {spec.get('id', spec)}")
        rows.append(_to_numpy(W_U[list(ids)]).astype(np.float32).mean(axis=0))
    return np.stack(rows)


def group_folds(groups: Sequence[Any], n_splits: int, seed: int) -> list[np.ndarray]:
    """Test indices of each fold; every group lands in exactly one fold."""
    g = np.asarray(groups)
    n_groups = len(np.unique(g))
    if n_groups < n_splits:
        raise ValueError(f"{n_groups} groups < {n_splits} folds")
    kf = GroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    return [test for _, test in kf.split(np.zeros((len(g), 1)), groups=g)]


def _model(alpha: float) -> Pipeline:
    return Pipeline([("scale", StandardScaler()), ("ridge", Ridge(alpha=alpha))])


def mean_cosine(pred: np.ndarray, Y: np.ndarray) -> float:
    num = (pred * Y).sum(axis=1)
    den = np.linalg.norm(pred, axis=1) * np.linalg.norm(Y, axis=1)
    return float(np.mean(np.where(den > 0, num / np.where(den > 0, den, 1.0), 0.0)))


def _pick_alpha(
    X: np.ndarray, Y: np.ndarray, groups: np.ndarray, alphas: Sequence[float], inner: int, seed: int
) -> float:
    folds = group_folds(groups, inner, seed)
    scores = []
    for a in alphas:
        s = []
        for test in folds:
            train = np.setdiff1d(np.arange(len(X)), test)
            m = _model(a).fit(X[train], Y[train])
            s.append(mean_cosine(m.predict(X[test]), Y[test]))
        scores.append(float(np.mean(s)))
    return float(alphas[int(np.argmax(scores))])  # argmax: ties -> first (smallest) alpha


def fit_predict(
    X: np.ndarray,
    Y: np.ndarray,
    groups: Sequence[Any],
    *,
    alphas: Sequence[float] = C.RIDGE_ALPHAS,
    outer: int = C.RIDGE_OUTER_FOLDS,
    inner: int = C.RIDGE_INNER_FOLDS,
    seed: int = C.RIDGE_FOLD_SEED,
) -> tuple[np.ndarray, list[float]]:
    """Out-of-fold predictions [n, d_out] and the alpha chosen in each outer fold."""
    X, Y = np.asarray(X, dtype=np.float64), np.asarray(Y, dtype=np.float64)
    g = np.asarray(groups)
    if not (len(X) == len(Y) == len(g)):
        raise ValueError("X, Y and groups differ in length")
    pred = np.zeros_like(Y)
    chosen = []
    for k, test in enumerate(group_folds(g, outer, seed)):
        train = np.setdiff1d(np.arange(len(X)), test)
        a = _pick_alpha(X[train], Y[train], g[train], alphas, inner, seed + 1 + k)
        chosen.append(a)
        pred[test] = _model(a).fit(X[train], Y[train]).predict(X[test])
    return pred, chosen


def write_readouts(
    pred_by_layer: Mapping[int, Any],
    W_U: Any,
    tokenizer: Any,
    index: Sequence[Any],
    *,
    k: int = TOPK,
    out: Path | None = None,
) -> Path:
    """Token-arm contract rows for ``ridge_probe/multihop``: row i of ``pred_by_layer[layer]``
    belongs to ``index[i]`` (a SiteRow or its dict)."""
    import torch
    from wsbench.produce.render import display_tokens

    out = out or C.readouts_path("ridge_probe", "multihop")
    wu = W_U if isinstance(W_U, torch.Tensor) else torch.as_tensor(np.asarray(W_U))
    wu_t = wu.float().T  # [d, vocab], converted once (a 27B W_U is ~5 GB in fp32)
    rows = []
    for layer in sorted(pred_by_layer):
        P = torch.as_tensor(_to_numpy(pred_by_layer[layer]), dtype=torch.float32, device=wu.device)
        if P.shape[0] != len(index):
            raise ValueError(f"layer {layer}: {P.shape[0]} predictions, {len(index)} sites")
        logits = P @ wu_t  # [n, vocab]
        top = torch.topk(logits, k, dim=-1)
        for i, site in enumerate(index):
            s = site if isinstance(site, Mapping) else site.__dict__
            ids = top.indices[i].tolist()
            rows.append(
                {
                    "id": s["id"],
                    "layer": int(layer),
                    "pos": int(s["pos"]),
                    "token": s["token"],
                    "tokens": display_tokens(tokenizer, ids),
                    "scores": [float(x) for x in top.values[i].tolist()],
                }
            )
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return out
