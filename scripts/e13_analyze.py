"""Compute E13's analysis (`emx.e13.analysis.analyze`) and write results/E13/analysis.json.

The labels record is results/E13/labels/<kind>/record.json (`--kind full|early`; default: full if
its record exists, else early). Until that record unlocks (complete, current draw/key/source
hashes, this rule version) the analysis holds the development-only summaries and every final
section reads `locked`; the status block printed here then shows development numbers only.
`--inputs-failed` marks the input-manifest check as failed (scenario 1). Log tags start with
`E13_ANALYZE`.

Amendment E: `--recipe tan32` analyses the retry (its labels under results/E13/run2/labels/,
its decisions and directions under results/E13/run2/, luna deciding every EM readout) and writes
results/E13/analysis_tan32.json; without it, the first run as before.

Usage:
  uv run python scripts/e13_analyze.py [--kind full|early] [--recipe tan32] [--inputs-failed]
      [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx.e13 import analysis as A
from emx.e13 import contracts as C


def load_record(root: Path, kind: str | None,
                recipe: str | None = None) -> tuple[dict | None, str | None]:
    kinds = [kind] if kind else list(A.LABEL_KINDS)
    for k in kinds:
        p = A.labels_dir(root, k, recipe) / "record.json"
        if p.exists():
            return json.loads(p.read_text()), k
    return None, kind


def status_lines(res: dict) -> list[str]:
    lab = res["labels"]
    if not lab["unlocked"]:
        dev = res["diagnostics"]["development"]
        lines = ["E13_ANALYZE locked: " + "; ".join(lab["reasons"])]
        for name, rec in dev.items():
            if rec is not None:
                lines.append(f"E13_ANALYZE dev {name}: outcome {rec.get('outcome')}")
        return lines
    sc = res["scenarios"]
    org = res["organism"]
    lines = [(f"E13_ANALYZE unlocked ({lab['kind']} pass); organism {org.get('status')}, "
              f"pass {org.get('pass')}"),
             f"E13_ANALYZE shared scenario: {sc['shared']} {sc['shared_why']}".rstrip()]
    for s, v in sc["sites"].items():
        lines.append(f"E13_ANALYZE site {s}: result {v['result']} (site scenario "
                     f"{v['scenario']}: {v['why']})" + (" UNVALIDATED" if v["unvalidated"]
                                                        else ""))
    if res["incomplete_cells"]:
        lines.append(f"E13_ANALYZE incomplete cells: {res['incomplete_cells']}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kind", choices=A.LABEL_KINDS, default=None)
    ap.add_argument("--inputs-failed", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--recipe", choices=C.AMENDMENT_E_RECIPES, default=None,
                    help="the Amendment E retry (default: the first run)")
    ap.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    a = ap.parse_args(argv)
    root = Path(a.root)
    rec, _ = load_record(root, a.kind, a.recipe)
    paths = {"root": root, "recipe": a.recipe}
    if a.inputs_failed:
        paths["inputs_ok"] = False
    res = A.analyze(paths, rec)
    out = Path(a.out) if a.out else root / C.analysis_path(a.recipe)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(A.jsonable(res), indent=2) + "\n")
    for line in status_lines(res):
        print(line)
    print(f"E13_ANALYZE wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
