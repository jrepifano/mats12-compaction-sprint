"""E12 judge sensitivity (post hoc, descriptive): both primaries re-read with one judge throughout.

Jacob's blind pass (results/E12/validation/handlabel_<recipe>.json) failed the EM bar for
Gemini, the deciding judge on ext48 and so on every cue row: 2 of its 8 cue-EM calls confirmed,
the 6 unconfirmed all in repaired arms. luna agreed far better on the same 24 items. This
recomputes primary (i) (cue EM) and primary (ii) (nudge area) with `qtable`'s judge forced to
luna, then to gfl, on every row, using `emx.e12.analysis`'s own tables, contrasts and bootstrap.
It is not a preregistered readout: prereg §4.7 says a failed bar leaves the readout unvalidated
and Jacob decides. Judge scores already exist for both judges on every row; no API calls.

Writes results/E12/judge_sensitivity_<recipe>.json.
Usage: uv run python scripts/e12_judge_sensitivity.py [--recipe tan32]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emx.e12 import analysis as A
from emx.e12 import contracts as C

JUDGES = ("deciding", "luna", "gfl")


def _point(rec: dict) -> dict:
    return {k: (None if rec[k] is None else round(rec[k], 3)) for k in ("point", "lo", "hi")}


def run(recipe: str, root: Path) -> dict:
    doc, dsha = A.load_final(recipe, root)
    fin = doc["final"]
    seeds, endpoint = sorted(fin["eligible_seeds"]), fin["endpoint"]
    orig = A.qtable
    out = {"exp": "E12", "recipe": recipe, "decisions_sha256": dsha, "endpoint": endpoint,
           "seeds": seeds, "status": "post hoc, descriptive (not preregistered)", "judges": {}}
    try:
        for judge in JUDGES:
            j = None if judge == "deciding" else judge
            A.qtable = lambda df, qk, judge=None, _j=j: orig(df, qk, _j)
            tree = A.Tree(recipe, root, fin.get("cuts") or ())
            pi = A.primary_i(tree, seeds, endpoint)["seeds"]
            pii_all = A.primary_ii(tree, seeds, endpoint)
            pii, eff = pii_all["seeds"], A.nudge_effectiveness(pii_all)
            out["judges"][judge] = {
                "primary_i": {s: {"contrast": _point(r["contrast"]),
                                  "a_vs_base": _point(r["a_vs_base"]),
                                  "rates": {k: round(v, 4) for k, v in r["rates"].items()}}
                              for s, r in pi.items()},
                "primary_ii": {s: {"contrast": _point(r["contrast"]),
                                   "support": _point(r["support"]),
                                   "dose_rates": {a: [round(x, 4) for x in v]
                                                  for a, v in r["dose_rates"].items()},
                                   "dose0_rates": {a: round(v, 4)
                                                   for a, v in r["dose0_rates"].items()}}
                               for s, r in pii.items()},
                "nudge_effectiveness": eff,
                "labels": {"i": A.label_primary("i", pi)["label"],
                           "ii": A.label_primary("ii", pii,
                                                 nudge_effective=eff["effective"])["label"]},
            }
    finally:
        A.qtable = orig
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--recipe", choices=sorted(C.RECIPES), default="tan32")
    ap.add_argument("--root", default=str(C.REPO_ROOT))
    args = ap.parse_args(argv)
    root = Path(args.root)
    rec = run(args.recipe, root)
    path = root / C.RESULTS / f"judge_sensitivity_{args.recipe}.json"
    path.write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n")
    for judge, r in rec["judges"].items():
        for w in ("primary_i", "primary_ii"):
            pts = [f"{v['contrast']['point']:+.2f} [{v['contrast']['lo']:+.2f}, "
                   f"{v['contrast']['hi']:+.2f}]" for v in r[w].values()]
            print(f"{judge:8s} {w}: {'; '.join(pts)}  label {r['labels'][w[-2:].lstrip('_')]}")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
