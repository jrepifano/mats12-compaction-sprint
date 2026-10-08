"""Score Jacob's E13 labels against the private key (prereg §4.4, Amendment B item 7;
`emx.e13.labels.score_pass`). Pattern of scripts/eq0_handlabel_score.py.

Inputs: results/E13/labels/<kind>/{draw.json, key.private.json} and --labels, a JSON file of the
labels read back from the page's db with ArtifactData ({id: doc} or a list of docs) or pasted
from the page's copyable summary. Writes results/E13/labels/<kind>/record.json (LABELS_RECORD_KEYS)
and prints the bars. Warns when a source file changed since the draw (the analysis refuses such a
record). Exit 0 when complete, 1 when items are unlabelled (the record is still written).

Usage: uv run python scripts/e13_label_score.py {notice,full,early} --labels labels.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from emx.e13 import contracts as C
from emx.e13 import labels as L

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("kind", choices=L.KINDS)
    ap.add_argument("--labels", required=True, type=Path)
    a = ap.parse_args(argv)
    d = ROOT / C.LABELS_DIR / a.kind
    draw = json.loads((d / "draw.json").read_text())
    key = json.loads((d / "key.private.json").read_text())
    rec = L.score_pass(json.loads(a.labels.read_text()), key, draw)
    (d / "record.json").write_text(L.dumps(rec))
    for path, sha in rec["source_sha256"].items():
        p = ROOT / path
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            print(f"E13_LABEL_SCORE WARN source changed since the draw: {path}")
    print(f"E13_LABEL_SCORE {a.kind}: {rec['n_labeled']}/{rec['n_items']} labelled, "
          f"complete {rec['complete']}")
    if not rec["complete"]:
        labs = L.normalize_labels(json.loads(a.labels.read_text()))
        miss = sorted(i for i, k in key["items"].items()
                      if not L.is_complete(labs.get(i), k["fields"]))
        print(f"  unlabelled: {miss}")
        return 1
    if a.kind == "notice":
        n = rec["notice_check"]
        print(f"  notice check: {n['matched']}/{n['n']} match ({n['share']:.0%}), instructs "
              f"{len(n['instructs'])} -> {'PASS' if n['passed'] else 'FAIL'}")
        return 0
    for j, b in rec["em_bar"].items():
        print(f"  EM bar {j}: kappa {b['kappa']} (raw {b['agreement']}), confirmed "
              f"{b['confirmed']}/{b['calls']} -> {'pass' if b['passed'] else 'FAIL'}")
    for name, e in rec["strata_flags"].items():
        if isinstance(e, str):
            print(f"  stratum {name}: {e}")
            continue
        for j, s in e.items():
            if s["flagged"]:
                print(f"  stratum {name} {j}: flagged {s['reasons']} applied {s['applied']}")
    for j, b in rec["refusal_bar"].items():
        if b["applicable"]:
            print(f"  refusal bar {j}: precision {b['precision']} ({b['judge_calls']} calls), "
                  f"recall {b['recall']} ({b['jacob_refusals']} Jacob) -> "
                  f"{'pass' if b['passed'] else 'FAIL'}")
    print(f"  unvalidated: {rec['unvalidated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
