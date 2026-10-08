"""EQ0 judge validation by a blind reference judge (Claude Opus subagents; no API spend).

  draw   draw 75 items per question set from the four models' judged answers, stratified by the
         deciding judge's EM call; write blind packets (results/EQ0/validation/packets/), the
         private key (key.private.json, gitignored) and draw.json (with the source CSVs'
         sha256s). Refuses a qset with < 75 eligible answers, and refuses to replace a
         different draw (or orphaned labels) without --force, which first moves the old
         labels and record.json to results/EQ0/validation/superseded/<UTC>/. Every packet is checked for leaked model,
         judge, score or stratum strings before the command succeeds.
  score  check the key and packets against draw.json and exactly 75 items per qset, then
         read results/EQ0/validation/labels_opus/packet_NN.jsonl (the subagents' labels), score
         the deciding judge per question set against the reference, write record.json.
         Exit 0 if both question sets pass the bar, 1 if not (Jacob then hand-labels), 2 if an
         input is refused (missing, duplicate or out-of-range labels; no key).

See `emx.eq0.validation` for the rules. Usage:
  uv run python scripts/eq0_validate.py draw [--force]
  uv run python scripts/eq0_validate.py score
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx.eq0 import analysis as A
from emx.eq0 import contracts as C
from emx.eq0 import validation as V


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def cmd_draw(root: Path, force: bool) -> int:
    try:
        frames, inputs = A.load_all(root)
        items, _ = V.draw_items(frames)
        rec = V.write_draw(root, frames, inputs, force=force)
    except (A.AnalysisRefused, V.ValidationError) as e:
        print(f"EQ0 VALIDATE REFUSED: {e}", file=sys.stderr)
        return 2
    by_id = {it["item_id"]: it for it in items}
    for name, p in rec["packets"].items():
        text = (root / p["path"]).read_text(encoding="utf-8")
        leaks = V.check_packet_blind(text, [by_id[i] for i in p["items"]])
        if leaks:
            print(f"EQ0 VALIDATE REFUSED: {name} leaks {leaks}; packets must be blind",
                  file=sys.stderr)
            return 2
    print(json.dumps(rec["counts"], indent=2))
    if rec["superseded"]:
        print(f"moved the earlier labels / record to {rec['superseded']}")
    print(f"wrote {len(rec['packets'])} packets ({rec['n_items']} items) under "
          f"{V.PACKETS_DIR}, {V.KEY_PATH} and {V.DRAW_PATH}")
    return 0


def cmd_score(root: Path) -> int:
    try:
        rec = V.score(root)
    except V.ValidationError as e:
        print(f"EQ0 VALIDATE REFUSED: {e}", file=sys.stderr)
        return 2
    out = root / V.RECORD_PATH
    A.write_json(rec, out)
    for q, r in rec["per_qset"].items():
        b = r["primary"]["bar"]
        s = r["primary"]["stats"]
        print(f"{q} (deciding {r['deciding_judge']}, {r['n_items']} items): kappa "
              f"{_fmt(b['kappa']['value'])} [{'ok' if b['kappa']['pass'] else 'FAIL'}], "
              f"agreement {_fmt(b['agreement']['value'])} "
              f"[{'ok' if b['agreement']['pass'] else 'FAIL'}], confirm "
              f"{_fmt(b['confirm']['value'])} of {b['confirm']['n_em_calls']} EM calls "
              f"[{'ok' if b['confirm']['pass'] else 'FAIL'}"
              f"{'' if b['confirm']['gated'] else ', not gated'}]; reference EM "
              f"{s['n_reference_em']}, missed {s['n_reference_em_missed']} -> "
              f"{'PASS' if b['pass'] else 'FAIL'}")
    print(f"EQ0 JUDGE VALIDATION: {'PASS' if rec['pass'] else 'FAIL'} (wrote {V.RECORD_PATH})")
    if not rec["pass"]:
        print(f"Jacob must label {C.VAL_FALLBACK_HANDLABELS} items on an HTML labelling page "
              "before the EM result is read.")
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draw")
    d.add_argument("--force", action="store_true", help="replace a different existing draw")
    sub.add_parser("score")
    ap.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    args = ap.parse_args(argv)
    root = Path(args.root)
    return cmd_draw(root, args.force) if args.cmd == "draw" else cmd_score(root)


if __name__ == "__main__":
    sys.exit(main())
