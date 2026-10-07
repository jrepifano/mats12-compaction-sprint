"""E9 analysis CLI: the Part 1 readouts, invalidate-and-replay, report tables and raw samples.

Prereg docs/experiments/E9/prereg.md §4.6-4.8, §4.11, §4.13, §6; implementation plan v4 row G1.
The rules are `emx.e9.rules` (each cites its prereg line), the bootstrap `emx.e9.boot`, and
loading, records, replay and table rendering `emx.e9.replay`. Free, laptop only, no network.

Every subcommand writes results/E9/analysis/<name>.json with `utc`, `code_commit`, the analysis
config and its sha256, `inputs` ({path: sha256} of every file read), `outcome` and `outcomes`.
Each analysis checks the answer pairs' pre-inspection evidence before reading any Q₂₀ score and
stops at "invalid" if it fails (prereg L268).

Subcommands:
  endpoint-precision   cost-and-noise gate: steps 1, 2, G₀/τ, step 4 (h₁)
  pilot                steps 5-6: h₂, the pairwise contrasts, promising
  gate                 the Part 1 readout, steps 1-11 and the §6 scenario
                       [--extra-seeds-funded yes|no]: F1a's launch-only-if funds answer
  extra-seeds          §4.11 four-seed readout
  samples              §4.13 random raw samples (seed RAW_SAMPLE_SEED)
  replay --new-config NEW.json [--old-dir DIR]
                       recompute every recorded analysis under NEW, diff outcomes, write
                       invalidations.json (prereg L269-270)
  tables [--out results/E9/analysis/tables.md]
                       render every report table from the analysis JSON (never typed by hand)

Global options: --root (default: repo root), --config CFG.json (merged over the frozen default
config), --judge-check JC.json ({qset: {"pass": bool, ...}}, folded into the config).

Example:
  uv run python scripts/e9_analyze.py --judge-check results/E9/judge_check_part1.json gate
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from emx.e9 import contracts as C
from emx.e9 import replay as RP

ROOT = Path(__file__).resolve().parents[1]


def load_config(root: Path, config: str | None, judge_check: str | None) -> dict:
    cfg = RP.default_config()
    if config:
        cfg = RP.merge_config(cfg, json.loads((root / config).read_text()
                                              if not Path(config).is_absolute()
                                              else Path(config).read_text()))
    if judge_check:
        p = Path(judge_check) if Path(judge_check).is_absolute() else root / judge_check
        jc = json.loads(p.read_text())
        cfg["judge_check"] = {q: (v.get("pass") if isinstance(v, dict) else v)
                              for q, v in jc.items() if q in cfg["judge_check"]}
        cfg["judge_check_source"] = {"path": str(judge_check), "sha256": RP.sha256_file(p)}
    return cfg


def _yesno(x: str | None) -> bool | None:
    return None if x is None else x == "yes"


def run_analysis(root: Path, name: str, cfg: dict, args: dict) -> dict:
    rec = RP.compute(name, cfg, root, args)
    p = RP.write_record(rec, root)
    print(f"{name}: outcome {rec['outcome']} -> {p.relative_to(root)}")
    for k, v in rec.get("outcomes", {}).items():
        print(f"  {k}: {v}")
    return rec


def tables(root: Path, out: str) -> Path:
    recs, inputs = {}, {}
    for name in RP.ANALYSES:
        rel = RP.record_path(name)
        p = root / rel
        if p.is_file():
            sha = RP.sha256_file(p)
            recs[name] = (json.loads(p.read_text()), sha)
            inputs[rel] = sha
    md = RP.render_tables(recs)
    op = root / out
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(md)
    rec = {"name": "tables", "utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "code_commit": RP.code_commit(root), "inputs": inputs,
           "config_sha256": sorted({r.get("config_sha256") for r, _ in recs.values()} - {None}),
           "output": {out: RP.sha256_file(op)}, "outcome": "pass" if recs else "skipped"}
    jp = root / RP.record_path("tables")
    jp.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    print(f"tables: {len(recs)} analyses -> {out}")
    return op


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--config")
    ap.add_argument("--judge-check")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("endpoint-precision", "pilot", "extra-seeds", "samples"):
        sub.add_parser(n)
    g = sub.add_parser("gate")
    g.add_argument("--extra-seeds-funded", choices=("yes", "no"))
    r = sub.add_parser("replay")
    r.add_argument("--new-config", required=True)
    r.add_argument("--new-judge-check")
    r.add_argument("--old-dir", default=C.ANALYSIS_DIR)
    t = sub.add_parser("tables")
    t.add_argument("--out", default=f"{C.ANALYSIS_DIR}/tables.md")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()

    if a.cmd == "tables":
        tables(root, a.out)
        return 0
    if a.cmd == "replay":
        new = load_config(root, a.new_config, a.new_judge_check)
        inv = RP.replay(a.old_dir, new, root)
        print(f"replay: {len(inv['changed'])} changed outcomes, "
              f"{len(inv['invalid_steps'])} dependent steps invalid -> "
              f"{a.old_dir}/{Path(RP.INVALIDATIONS).name}")
        for c in inv["changed"]:
            print(f"  {c['analysis']}.{c['key']}: {c['old']} -> {c['new']}")
        return 0
    cfg = load_config(root, a.config, a.judge_check)
    args = {}
    if a.cmd == "gate":
        args["extra_seeds_funded"] = _yesno(a.extra_seeds_funded)
    try:
        run_analysis(root, a.cmd, cfg, args)
    except RP.MissingRun as e:
        print(f"{a.cmd}: refused, a required run is not verified: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
