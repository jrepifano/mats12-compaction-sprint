"""Follow-up 1/1b analysis (exploratory; log.md 17:32Z and 18:03Z): rates and contrasts of the
belief-only (Sp, Vp), norm-only (R), S, N and U arms at the 7 at-risk sites. The sites were chosen
on main-grid outcomes (any cheating), so these describe at-risk sites only. Writes
results/fu1/analysis.json and prints a per-site table.

  P37_VARIANT=fu1 PYTHONPATH=probes/P37-grader-belief-causal:src:scripts uv run --group swarm \
      python -m p37.fu1
"""
from __future__ import annotations

import json
import sys

from p37 import contracts as C
from p37.analyze import contrast, load_measures, task_equal_rate

CONDS = ("Sp", "Vp", "R", "S", "N", "U")
CONTRASTS = (("Sp", "N"), ("R", "N"), ("Vp", "N"), ("S", "N"), ("N", "U"), ("Sp", "S"),
             ("R", "S"), ("Sp", "R"), ("Vp", "Sp"), ("S", "U"), ("Sp", "U"), ("R", "U"),
             ("Vp", "U"))


def main() -> int:
    df, counts = load_measures(C.MEASURES_CSV)
    res: dict = {"counts": counts, "rates": {}, "contrasts": {}, "note":
                 "exploratory; 7 sites selected on main-grid outcomes; N run in a later batch"}
    for c in CONDS:
        res["rates"][c] = task_equal_rate(df[df["cond"] == c], "cheat")
    for a, b in CONTRASTS:
        res["contrasts"][f"{a}-{b}"] = contrast(df, df["cond"] == a, df["cond"] == b)
    tab = df.pivot_table(index="site_id", columns="cond", values="cheat", aggfunc="sum")
    n = df.pivot_table(index="site_id", columns="cond", values="cheat", aggfunc="count")
    res["per_site"] = {s: {c: f"{int(tab.loc[s, c])}/{int(n.loc[s, c])}" for c in CONDS
                           if c in tab.columns} for s in tab.index}
    out = C.RESULTS_DIR / "analysis.json"
    out.write_text(json.dumps(res, indent=1, default=str))
    for c, r in res["rates"].items():
        print(f"{c:3s} rate {r['estimate']:.3f} {r['ci95']} k/n {r['k']}/{r['n']}")
    for k, r in res["contrasts"].items():
        print(f"{k:6s} {r['estimate']:+.3f} {r['ci95']} p {r['p_perm']} "
              f"sites {r['n_sites']} tasks {r['n_tasks']}")
    for s, row in res["per_site"].items():
        print(s, row)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
