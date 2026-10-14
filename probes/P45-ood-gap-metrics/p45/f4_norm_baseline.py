"""P45 follow-up F4 (exploratory, free): the activation-norm baseline against the T2 winner and S3.

Columns (length-controlled, test split, common valid rows): the frozen winner, maha_L24 (S3),
norm_L16, norm_L24. Per category: AUROC, precision@1% and @0.1% (point estimates as analyze's
m1_point over 500 planted subsets), and paired-bootstrap contrasts (analyze.boot_draws, 2,000
draws, identical resamples for every column): winner − norm_L*, S3 − norm_L*.
Output: results/analysis/f4_norm.json.  Usage: uv run python -m p45.f4_norm_baseline
"""

from __future__ import annotations

import json

import numpy as np

from p45 import analyze as AN
from p45 import contracts as K

LC = "__lc"
METRICS = ("auroc", "prec_1pct", "prec_0.1pct")


def main() -> dict:
    p = AN.Paths()
    sel = AN.require_selection(p)
    best, cats = sel["best"], list(sel["categories"])
    df = AN.load_prep(p)
    test = df[(df["split"] == "test") & df["valid"]].reset_index(drop=True)
    pool = test[test["set"] == K.P42K.POOL]
    cols = {"best": best, "S3": "maha_L24", "norm_L16": "norm_L16", "norm_L24": "norm_L24"}
    out = {"best": best, "categories": cats, "per_category": {}}
    for cat in cats + [K.P42K.PLANTED_LAW]:
        pl = test[test["set"] == cat]
        pools = {k: pool[c + LC].to_numpy(float) for k, c in cols.items()}
        plants = {k: pl[c + LC].to_numpy(float) for k, c in cols.items()}
        draws = AN.boot_draws(pools, plants, K.N_BOOT, np.random.default_rng([K.SEED_BOOT, 4]))
        res = {"n_planted": len(pl), "points": {}, "contrasts": {}}
        for k in cols:
            pt = AN.A.m1_point(pools[k], plants[k], np.random.default_rng([20261019, 4]), 500)
            res["points"][k] = {m: {"est": pt[m], "ci95": AN.A.ci(draws[k][m])} for m in METRICS}
        for a in ("best", "S3"):
            for b in ("norm_L16", "norm_L24"):
                res["contrasts"][f"{a}_minus_{b}"] = {
                    m: {"est": res["points"][a][m]["est"] - res["points"][b][m]["est"],
                        "ci95": AN.A.ci(draws[a][m] - draws[b][m])} for m in METRICS}
        out["per_category"][cat] = res
    (K.ANALYSIS_DIR / "f4_norm.json").write_text(json.dumps(out, indent=1, default=float))
    for cat, r in out["per_category"].items():
        print(cat)
        for k, v in r["points"].items():
            print(f"  {k:9s}", {m: round(v[m]["est"], 3) for m in METRICS})
        for k, v in r["contrasts"].items():
            print(f"  {k:20s}", {m: (round(v[m]["est"], 3), [round(x, 3) for x in v[m]["ci95"]])
                                  for m in METRICS})
    return out


if __name__ == "__main__":
    main()
