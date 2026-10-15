"""P34 sensitivity (log.md ~19:00, after the Gemini judge was lost): broad EM under E11's judge map
(luna decides `general`, gfl decides `ext48`) against luna deciding everything, for every chat
view whose ext48 answers carry gfl scores (>= 95% coherent__gfl present). Writes
results/e11map.json and prints a table.

Usage (from the probe dir): uv run python -m p34.e11map
"""
from __future__ import annotations

import json

from p34 import analyze as AN
from p34 import content as CT
from p34 import contracts as K
from p34.judge import JUDGE_VIEWS

OUT = K.RESULTS_DIR / "e11map.json"


def rate(df, subset: str) -> list[int]:
    d = df[df[subset].astype(bool)]
    return [int(d["x"].sum()), int(d["n"].sum())]


def main() -> int:
    luna, e11 = AN.Inputs(CT.paths(), "luna"), AN.Inputs(CT.paths(), "e11")
    out = {}
    for v in JUDGE_VIEWS:
        a = e11.chat(v)
        if a is None or a.loc[a["qset"] == "ext48", "coherent__gfl"].notna().mean() < 0.95:
            continue
        b = luna.chat(v)
        out[v] = {f"{s}_{m}": rate(d, s) for s in ("nonmed49", "test41")
                  for m, d in (("luna", b), ("e11map", a))}
        r = {k: 100 * x / n for k, (x, n) in out[v].items()}
        print(f"{v:18s} nonmed49 luna {r['nonmed49_luna']:5.1f} e11map {r['nonmed49_e11map']:5.1f}"
              f" | test41 luna {r['test41_luna']:5.1f} e11map {r['test41_e11map']:5.1f}")
    OUT.write_text(json.dumps({"definition": __doc__.split("\n\n")[0], "views": out},
                              indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
