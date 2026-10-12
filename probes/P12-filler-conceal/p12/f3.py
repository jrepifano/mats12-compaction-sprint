"""P12 follow-up F3 (exploratory, laptop, $0; log.md "Follow-up F3"): a ridge probe from each
layer's pooled filler residual (`resid_pool`) to y's value, trained on the P and M records of the
training items, 5-fold cross-validation by item (seed `arms.SEED`); on held-out items, Pearson r of
the predictions with y per arm, and with d under D and CD. Bootstrap CIs over held-out items.

    uv run python -m p12.f3 [--results DIR]   # from the probe dir; writes DIR/f3.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from . import analyze as AN
from .arms import ARMS, SEED, load_items
from .schema import RESULTS

LAYERS = tuple(range(16, 43, 2))
TRAIN_ARMS = ("P", "M")
ALPHAS = (1.0, 10.0, 100.0, 1000.0, 10000.0)
N_FOLDS = 5


def load_pools(results: Path, items: list[dict]) -> dict[str, dict[int, np.ndarray]]:
    out: dict[str, dict[int, np.ndarray]] = {a: {} for a in ARMS}
    for it in items:
        for a in ARMS:
            p = AN.rec_path(results, it["idx"], a)
            if p.exists():
                with np.load(p) as z:
                    out[a][it["idx"]] = z["resid_pool"].astype(np.float32)
    return out


def boot_r(x: np.ndarray, y: np.ndarray) -> dict:
    i = AN._idx(len(x))
    rs = [np.corrcoef(x[j], y[j])[0, 1] for j in i[:2000]]
    lo, hi = np.nanpercentile(rs, [2.5, 97.5])
    return {"r": float(np.corrcoef(x, y)[0, 1]), "lo": float(lo), "hi": float(hi), "n": len(x)}


def partial_r(p: np.ndarray, t: np.ndarray, z: np.ndarray) -> float:
    """Pearson r of p and t after regressing both on z (with an intercept)."""
    Z = np.column_stack([np.ones_like(z), z])
    res = [v - Z @ np.linalg.lstsq(Z, v, rcond=None)[0] for v in (p, t)]
    return float(np.corrcoef(*res)[0, 1])


def run(results: Path) -> dict:
    from sklearn.linear_model import RidgeCV
    from sklearn.preprocessing import StandardScaler

    items = load_items()
    pools = load_pools(results, items)
    idxs = np.array(sorted(i for i in pools["P"] if all(i in pools[a] for a in ARMS)))
    y = {it["idx"]: it["roles"]["y"] for it in items}
    d = {it["idx"]: it["d"] for it in items}
    xv = {it["idx"]: it["roles"]["x"] for it in items}
    folds = np.random.default_rng(SEED).permutation(len(idxs)) % N_FOLDS
    out: dict = {"layers": {}, "n_items": len(idxs), "train_arms": list(TRAIN_ARMS)}
    for layer in LAYERS:
        pred = {a: np.zeros(len(idxs)) for a in ARMS}
        for f in range(N_FOLDS):
            tr, te = idxs[folds != f], idxs[folds == f]
            X = np.stack([pools[a][i][layer] for a in TRAIN_ARMS for i in tr])
            t = np.array([y[i] for a in TRAIN_ARMS for i in tr], dtype=float)
            sc = StandardScaler().fit(X)
            m = RidgeCV(alphas=ALPHAS).fit(sc.transform(X), t)
            for a in ARMS:
                Xe = sc.transform(np.stack([pools[a][i][layer] for i in te]))
                pred[a][folds == f] = m.predict(Xe)
        yy = np.array([y[i] for i in idxs], dtype=float)
        dd = np.array([d[i] for i in idxs], dtype=float)
        xx = np.array([xv[i] for i in idxs], dtype=float)
        row = {a: {"r_y": boot_r(pred[a], yy), "r_x": float(np.corrcoef(pred[a], xx)[0, 1]),
                   "r_y_given_x": partial_r(pred[a], yy, xx)} for a in ARMS}
        for a in ("D", "CD"):
            row[a]["r_d"] = boot_r(pred[a], dd)
        row["P"]["r_d_placebo"] = boot_r(pred["P"], dd)
        out["layers"][layer] = row
    ya = np.array([y[i] for i in idxs], dtype=float)
    out["corr_y"] = {r: float(np.corrcoef(ya, [it_r[i] for i in idxs])[0, 1])
                     for r, it_r in (("x", xv), ("c1x", {it["idx"]: it["roles"]["c1x"]
                                                       for it in items}))}
    best = max(LAYERS, key=lambda li: out["layers"][li]["P"]["r_y"]["r"])
    out["best_layer"] = best
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p12.f3")
    ap.add_argument("--results", type=Path, default=RESULTS)
    a = ap.parse_args(argv)
    res = AN._round(run(a.results))
    (a.results / "f3.json").write_text(json.dumps(res, indent=1) + "\n")
    b = res["best_layer"]
    for li, row in res["layers"].items():
        print(li, {k: (v["r_y"]["r"], v["r_y_given_x"], v["r_x"], v.get("r_d", {}).get("r"))
                   for k, v in row.items()})
    print("corr of y with", res["corr_y"])
    print("best layer", b, json.dumps(res["layers"][str(b)] if str(b) in res["layers"]
                                      else res["layers"][b]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
