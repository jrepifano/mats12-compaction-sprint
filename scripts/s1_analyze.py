"""Experiment 1 analysis entry point, run after Jacob releases the audit (sprint plan R-A2).

    uv run python scripts/s1_analyze.py [--out results/sprint/S1/analysis.json] [--no-verbatim]

Verifies the release (`seal.verify("S1")`; every sealed file re-hashed), then writes one JSON with
`compaction.s1_analysis`'s reports, all read through `seal.open_sealed`:
- `part_a`: item rates by type x source/model/harness, sample and population-weighted
  (`stratum_weights` of the S1 manifest), user-rule loss shares, invented spans, the second-look
  summary, carried_forward and unrechecked-contradicted counts, and the precision-corrected
  sensitivities (`results/sprint/S1/judge_precision.json`);
- `part_b`: per-arm rates, the primary contrast and the exploratory pairs, each also with
  reloaded items counted as lost;
- `size`: the 256k-vs-512k size test;
- `verbatim_budget` (no seal; skipped with `--no-verbatim`): user text after each early user turn
  of the Part A records, which S2 draws its follow-up volume from.
Also prints the release record's digest and a few headline lines for the log. Writes nothing
outside `--out`; the file holds aggregates and record ids, no session text.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from compaction import s1_analysis as A
from compaction import seal
from compaction.s1_types import SEAL_STAGE

JUDGES = ("haiku-max",)
MANIFEST = ROOT / "data" / "raw" / "sprint" / "s1" / "manifest.json"
OUT = ROOT / "results" / "sprint" / "S1" / "analysis.json"


def log(msg: str) -> None:
    print(f"S1_ANALYZE {msg}", flush=True)


def _json_safe(o):
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_json_safe(v) for v in o]
    if hasattr(o, "item"):  # numpy scalars
        return o.item()
    if isinstance(o, float) and math.isnan(o):
        return None
    return o


def run(judges=JUDGES, *, manifest_path=MANIFEST, verbatim: bool = True, n_boot=A.N_BOOT) -> dict:
    rel = seal.verify(SEAL_STAGE)
    log(f"release {rel['released_utc']} by {rel['released_by']}, digest {rel['digest'][:16]}")
    manifest = json.loads(Path(manifest_path).read_text())
    weights = A.stratum_weights(manifest)
    rosters = {}
    for j in judges:
        try:
            rosters[j] = A.default_reader(f"partA_roster_{j}.json")
        except FileNotFoundError:
            log(f"no partA roster for {j}: compaction-level denominators use judged rows only")
    t0 = time.time()
    rep = {
        "release": {
            k: rel[k]
            for k in ("stage", "release_name", "released_utc", "released_by", "digest", "note")
        },
        "judge_precision": A.load_judge_precision(),
        "part_a": A.part_a_report(judges, weights=weights, rosters=rosters, n_boot=n_boot),
        "part_b": A.part_b_report(judges, n_boot=n_boot),
        "size": A.size_report(judges, n_boot=n_boot),
    }
    log(f"reports in {time.time() - t0:.0f} s")
    if verbatim:
        from compaction import s1_data

        recs = (s1_data.load(r) for r in manifest["ids"]["part_a"])
        vb = A.verbatim_budget(recs)
        rep["verbatim_budget"] = {k: v for k, v in vb.items() if k != "records"}
        rep["verbatim_budget"]["n_records"] = len(vb["records"])
    return _json_safe(rep)


def headline(rep: dict) -> list[str]:
    out = []
    for j, a in rep["part_a"].items():
        if a.get("missing"):
            out.append(f"part A {j}: missing {a['missing']}")
            continue
        sl = a["second_look"]
        out.append(
            f"part A {j}: second look items {sl['item']}, claims {sl['claim'].get('n_rechecked')}"
        )
        corr = a.get("precision_corrected", {})
        for k, v in corr.items():
            out.append(f"part A {j} {k}: {json.dumps(v)[:300]}")
    for j, b in rep["part_b"].items():
        out.append(
            f"part B {j}: arms {b.get('arms_present')} missing {b.get('arms_missing') or b.get('missing')}"
        )
    for j, s in rep["size"].items():
        out.append(f"size {j}: {s.get('n_items')} items common to {s.get('prefixes')}")
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--no-verbatim", action="store_true")
    ap.add_argument("--n-boot", type=int, default=A.N_BOOT)
    a = ap.parse_args(argv)
    rep = run(verbatim=not a.no_verbatim, n_boot=a.n_boot)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n")
    for line in headline(rep):
        log(line)
    log(f"wrote {out}")


if __name__ == "__main__":
    main()
