"""E11 part-1 analysis: writes results/E11/analysis/{unlocked,locked}.json.

Prereg docs/experiments/E11/prereg.md §4.4-§4.7 and §6; `emx.e11.analysis.analyze`. Both files are
always written. Locked readouts (C.LOCKED_READOUTS: D and its intervals, the decision, the cue
label, recurrence, elicitation; prereg §4.5 "Timing") are printed only with --unlock, which
refuses unless Jacob's labelling pass is scored (results/E11/validation/handlabel/record.json).
Without --unlock the script prints the unlocked summary and "locked: <n> readouts".

--accept-unvalidated records Jacob's explicit acceptance of locked readouts after a failed
labelling bar (prereg §4.5, §6 first row); never pass it without his decision.

Usage: uv run python scripts/e11_analyze.py [--unlock] [--accept-unvalidated]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx.e11 import analysis as A
from emx.e11 import contracts as C


def summary_lines(u: dict) -> list[str]:
    b = u["base_reproduction"]
    verdict = "ok" if b["ok"] else "NOT REPRODUCED"
    lines = [f"base reproduction: dev {b['dev']} test {b['test']} -> {verdict}"]
    for ds, v in (u.get("ladder", {}).get("datasets") or {}).items():
        lines.append(f"ladder {ds}: {v['state']}" + (f"@{v['rung']}" if v["rung"] else ""))
    lines.append(f"eligible: {u.get('eligible')}")
    if u.get("refused"):
        lines.append(f"part-1 readouts refused: {u['refused']}")
    for ds, c in (u.get("checks") or {}).items():
        bad = [k for k, v in c["models"].items() if not v.get("ok")]
        lines.append(f"checks {ds}: {'ok' if c['ok'] else 'FAILED ' + ', '.join(bad)}")
    for ds, d in (u.get("datasets") or {}).items():
        r = d["retention"]
        lines.append(f"retention {ds}: {r['status']}"
                     + (f" (ratio {r['ratio']:.3f})" if r.get("ratio") is not None else ""))
    lab = u["labelling"]
    lines.append(f"labelling pass: {lab['status']}"
                 + (f" (bar {'passed' if lab.get('pass') else 'failed'})" if lab.get("scored")
                    else ""))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--unlock", action="store_true", help="print the locked readouts")
    ap.add_argument("--accept-unvalidated", action="store_true",
                    help="Jacob accepts the locked readouts after a failed labelling bar")
    ap.add_argument("--root", default=str(C.REPO_ROOT))
    ap.add_argument("--n-boot", type=int, default=C.BOOTSTRAP_RESAMPLES, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = Path(args.root)
    if args.unlock:
        st = A.label_status(root)
        if not st.get("scored"):
            print(f"refused: --unlock needs a scored {C.HANDLABEL_DIR}/record.json "
                  f"(labelling pass {st['status']})", file=sys.stderr)
            return 2
    unlocked, locked = A.analyze(root, accept_unvalidated=args.accept_unvalidated,
                                 n_boot=args.n_boot)
    if args.accept_unvalidated:
        unlocked["accept_unvalidated"] = True
    out = root / C.ANALYSIS_DIR
    A.write_json(unlocked, out / "unlocked.json")
    A.write_json(locked, out / "locked.json")
    for line in summary_lines(unlocked):
        print(line)
    print(f"wrote {C.ANALYSIS_DIR}/unlocked.json and {C.ANALYSIS_DIR}/locked.json")
    if args.unlock:
        print(f"== locked readouts ({locked['validation']})")
        for k, ws in locked.get("readout_warnings", {}).items():
            for w in ws:
                print(f"WARNING {k}: {w}")
        print(json.dumps(locked["readouts"], indent=2))
    else:
        print(f"locked: {len(locked['readouts'])} readouts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
