"""P16 exploratory follow-ups (log.md).

F1, depth-aware monitor: the no-judge ranking N from residual lists aggregated over layer
subsets (early 22-31, late 32-42) for every arm; y-hit@1/@2 and d-hit@1, with CD - P paired.

    uv run python -m p16.followups f1   # from the probe dir; writes results/f1.json
"""

from __future__ import annotations

import argparse
import json

from . import analyze as AN
from . import arms as A
from . import judge as J
from . import readers as R
from . import schema as S

SPLITS = {"early": tuple(range(22, 32)), "late": tuple(range(32, 43)), "all": None}
ARMS = ("P", "M", "D", "CD", "LD")


def f1() -> dict:
    items = A.load_items()
    iv = AN.load_ivocab(S.RESULTS)
    out: dict = {}
    for name, layers in SPLITS.items():
        hits: dict = {}
        for arm in ARMS:
            ranks = [R.numeric_ranking(J.agg_list(it["idx"], arm, "P", layers=layers), iv)
                     for it in items]
            hits[arm] = {f"y@{k}": [R.hit_at(c, it["roles"]["y"], k) for c, it in
                                    zip(ranks, items, strict=True)] for k in (1, 2)}
            hits[arm]["d@1"] = [R.hit_at(c, it["d"], 1) for c, it in zip(ranks, items,
                                                                        strict=True)]
        out[name] = {arm: {k: AN.mean_ci(v) for k, v in h.items()} for arm, h in hits.items()}
        out[name]["CD-P"] = {k: AN.paired(hits["CD"][k], hits["P"][k]) for k in ("y@1", "y@2")}
    return AN._round(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p16.followups")
    ap.add_argument("which", choices=["f1"])
    a = ap.parse_args(argv)
    res = f1()
    (S.RESULTS / f"{a.which}.json").write_text(json.dumps(res, indent=1) + "\n")
    for name, v in res.items():
        print(name, {arm: (v[arm]["y@1"]["mean"], v[arm]["y@2"]["mean"], v[arm]["d@1"]["mean"])
                     for arm in ARMS}, "CD-P y@1", v["CD-P"]["y@1"]["diff"],
              v["CD-P"]["y@1"]["lo"], v["CD-P"]["y@1"]["hi"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
