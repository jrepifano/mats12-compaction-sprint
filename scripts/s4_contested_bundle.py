"""S4 pilot: the reduced alert-labelling set (decision D8, Jacob 2026-09-30).

Jacob labels only (a) every *contested* primary item, i.e. one that either judge labelled
"alert", and (b) a seeded random check sample of the uncontested rest, mixed and blind. The
remaining uncontested items (both judges non-alert) get the label `no_alert` from panel
consensus. They are written to a separate, clearly named file so their provenance is never
confused with Jacob's, and the check sample estimates how often that consensus misses an alert.

Reads the full blind bundle and its key (s4_label_bundle.py). Writes:
- results/S4/label_bundle_contested.csv  (blind, shuffled; gitignored like the full bundle)
- results/S4/label_bundle_contested_key.csv  (item_ref -> contested|check_sample; gitignored)
- data/S4/labels/panel_consensus_no_alert.csv  (item_ref,label for the rest; committed)

    uv run --group swarm python scripts/s4_contested_bundle.py --seed 20260930 --n-check 15
"""
from __future__ import annotations

import argparse
import csv
import random
from pathlib import Path

WT = Path(__file__).resolve().parents[1]
RES = WT / "results" / "S4"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=20260930)
    ap.add_argument("--n-check", type=int, default=15)
    args = ap.parse_args()

    with open(RES / "label_bundle.csv", newline="") as f:
        bundle = list(csv.DictReader(f))
    with open(RES / "label_bundle_key.csv", newline="") as f:
        key = {r["item_ref"]: r for r in csv.DictReader(f)}
    assert set(key) == {r["item_ref"] for r in bundle}, "bundle and key disagree"

    contested = sorted(ref for ref, k in key.items() if "alert" in (k["panel_luna"], k["panel_dsk"]))
    rest = sorted(set(key) - set(contested))
    rng = random.Random(args.seed)
    check = sorted(rng.sample(rest, min(args.n_check, len(rest))))
    consensus = sorted(set(rest) - set(check))

    chosen = set(contested) | set(check)
    rows = [r for r in bundle if r["item_ref"] in chosen]
    rng.shuffle(rows)
    cols = list(bundle[0].keys())
    with open(RES / "label_bundle_contested.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    with open(RES / "label_bundle_contested_key.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_ref", "why"])
        w.writerows([(r, "contested") for r in contested] + [(r, "check_sample") for r in check])
    out = WT / "data" / "S4" / "labels" / "panel_consensus_no_alert.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_ref", "label"])
        w.writerows([(r, "no_alert") for r in consensus])
    print(f"{len(bundle)} items: {len(contested)} contested + {len(check)} check sample -> "
          f"{len(rows)} for Jacob (seed {args.seed}); {len(consensus)} panel-consensus no_alert "
          f"-> {out.relative_to(WT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
