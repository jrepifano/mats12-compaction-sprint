"""P45 follow-up F1 (exploratory, free): does T1's *region* statistic see the planted gaps?

Each planted_med / planted_law / id_heldout row (and, as reference, the pool) is assigned to the
nearest k = 200 k-means centre (Euclidean on the same L2-normalised rows, as KMeans assigns) in
spaces A, B and C. Reported per set: the share landing in candidate gap regions (s <= T1_SHARE_MAX,
P >= T1_MIN_POOL; `top_<S>_k200.json`'s candidate definition) and the mean region share s.
Output: results/t1/f1_region_planted.json.  Usage: uv run python -m p45.f1_region_planted
"""

from __future__ import annotations

import json

import numpy as np

from p45 import contracts as K
from p45 import t1

SETS = (K.P42K.POOL, K.P42K.PLANTED_MED, K.P42K.PLANTED_LAW, K.P42K.ID_HELDOUT)


def main() -> dict:
    out = {"k": K.T1_K, "candidate_rule": f"s <= {K.T1_SHARE_MAX}, P >= {K.T1_MIN_POOL}"}
    for space in ("A", "B", "C", "D"):
        X, pids, _ = t1.load_space(space)
        sets = np.array([p.rsplit("-", 1)[0] for p in pids])
        z = np.load(t1.CACHE / f"regions_{space}_k{K.T1_K}.npz")
        C = z["centers"].astype(np.float32)
        reg = json.loads((K.T1_DIR / f"regions_{space}_k{K.T1_K}.json").read_text())["regions"]
        s = np.array([r["s"] for r in sorted(reg, key=lambda r: r["region"])])
        P = np.array([r["P"] for r in sorted(reg, key=lambda r: r["region"])])
        cand = (s <= K.T1_SHARE_MAX) & (P >= K.T1_MIN_POOL)
        res = {}
        for name in SETS:
            Q = X[sets == name]
            d = (Q ** 2).sum(1)[:, None] - 2 * Q @ C.T + (C ** 2).sum(1)[None]
            a = d.argmin(1)
            res[name] = {"n": len(Q), "share_in_candidates": float(cand[a].mean()),
                         "mean_region_s": float(s[a].mean()),
                         "median_region_s": float(np.median(s[a]))}
        out[space] = res
        print(space, json.dumps(res))
    (K.T1_DIR / "f1_region_planted.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    main()
