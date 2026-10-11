"""F2 multiple-choice table: per option count, base vs dots100 accuracy and the uplift, plus
uplift(2 options) − uplift(10 options), paired by item (item bootstrap, seed 0)."""

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from .analyze import boot_mean_diff
from .api import latest_ok


def table(path: Path) -> dict:
    per = defaultdict(dict)
    for r in latest_ok(path).values():
        n_opt = int(r["bank"].rsplit("mc", 1)[1])
        per[(r["item_id"].split(":")[1], n_opt)][r["arm"]] = float(r["correct"])
    rng = np.random.default_rng(0)
    out, up = {}, {}
    for n_opt in (2, 5, 10):
        ids = sorted(i for (i, n), d in per.items() if n == n_opt and len(d) == 2)
        m = np.array([[per[(i, n_opt)]["base"], per[(i, n_opt)]["dots100"]] for i in ids])
        up[n_opt] = dict(zip(ids, m[:, 1] - m[:, 0], strict=True))
        out[f"opt{n_opt}"] = {"n": len(ids), "chance": 1 / n_opt,
                              "acc": {"base": float(m[:, 0].mean()),
                                      "dots100": float(m[:, 1].mean())},
                              "uplift": boot_mean_diff(m[:, 1] - m[:, 0], rng)}
    common = sorted(set(up[2]) & set(up[10]))
    out["uplift2_minus_uplift10"] = boot_mean_diff(
        np.array([up[2][i] - up[10][i] for i in common]), rng)
    return out


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p, json.dumps(table(Path(p)), indent=1))
