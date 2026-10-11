"""F1 table: accuracy per arm and paired differences vs base (item bootstrap, seed 0)."""

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from .analyze import boot_mean_diff
from .api import latest_ok

ARMS = ("base", "dots100", "dots500", "pre100")


def table(path: Path) -> dict:
    per = defaultdict(dict)
    rows = list(latest_ok(path).values())
    for r in rows:
        per[r["item_id"]][r["arm"]] = float(r["correct"])
    ids = sorted(i for i, d in per.items() if all(a in d for a in ARMS))
    m = np.array([[per[i][a] for a in ARMS] for i in ids])
    rng = np.random.default_rng(0)
    out = {"n": len(ids), "acc": {a: float(m[:, j].mean()) for j, a in enumerate(ARMS)},
           "invalid": {a: sum(1 for r in rows if r["arm"] == a and not r["valid"]) for a in ARMS}}
    for j, a in enumerate(ARMS[1:], 1):
        out[f"{a}_minus_base"] = boot_mean_diff(m[:, j] - m[:, 0], rng)
    out["dots100_minus_pre100"] = boot_mean_diff(m[:, 1] - m[:, 3], rng)
    return out


if __name__ == "__main__":
    import json

    for p in sys.argv[1:]:
        print(p, json.dumps(table(Path(p)), indent=1))
