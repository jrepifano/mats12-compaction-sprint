"""Oracle Lens hits on Qwen's hidden target by layer and position, per item group (exploratory).

    uv run python -m p03.layers     (from the probe dir; writes results/olens_layers.json)
"""

from __future__ import annotations

import json
from collections import defaultdict

from p03 import core as C
from p03 import score as S


def main() -> int:
    ritems = S.load_reader_items()
    sel = {r["id"]: r for r in C.read_jsonl(C.DATA / "selection.jsonl")}
    ro = S.load_readouts("olens")
    out: dict = {}
    for label, keep in (
        ("A_survivor", lambda r: r["type"] == "A" and r["group"] == "survivor"),
        ("A_survivor_strict", lambda r: r["type"] == "A" and r["group"] == "survivor"
         and sel[r["id"]].get("strict")),
        ("C_all", lambda r: r["type"] == "C"),
        ("B_all", lambda r: r["type"] == "B"),
    ):
        rs = [r for r in ritems if keep(r)]
        by = defaultdict(lambda: [0, 0])
        for r in rs:
            for c in ro.get(r["id"], []):
                k = f"L{c['layer']}|pos{c['pos']}"
                by[k][1] += 1
                by[k][0] += S.cell_hit(c, r["target"], r["target_aliases"], r["type"] == "A")
        out[label] = {"n_items": len(rs),
                      "cells": {k: {"hits": h, "n": n, "rate": h / n} for k, (h, n) in
                                sorted(by.items(), key=lambda kv: (int(kv[0][1:].split("|")[0]),
                                                                    kv[0]))}}
    (C.RESULTS / "olens_layers.json").write_text(json.dumps(out, indent=1) + "\n")
    for label, d in out.items():
        print(label, d["n_items"])
        for k, v in d["cells"].items():
            if k.endswith("pos-1"):
                print(f"   {k:12s} {v['hits']:3d}/{v['n']:3d} = {v['rate']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
