"""X3 pilot analysis entry point (U7; prereg §4.8-4.10, §6).

    uv run --group swarm python scripts/x3_analyze.py                    # results/X3/episodes.csv
    uv run --group swarm python scripts/x3_analyze.py --episodes PATH --out PATH
    uv run --group swarm python scripts/x3_analyze.py --synthetic [--synthetic-seed 0]
    uv run --group swarm python scripts/x3_analyze.py --measurement-ok no   # after a failed check

Writes analysis.json (default results/X3/analysis.json; with --synthetic,
results/X3/synthetic/analysis.json unless --out is given) holding every number, the sha256 of
episodes.csv and of each extra input (--inputs; by default detector.csv, judged/*.csv and
tasks.json when they exist and the default episodes path is used), the seeds, and the
exclusions. --n-perm / --n-boot override 10,000 only for quick looks; the overrides are
recorded in the output.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from x3 import analysis as A
from x3 import contracts as C


def default_inputs() -> list[Path]:
    paths = [C.DETECTOR_CSV, C.TASKS_JSON]
    if C.JUDGED_DIR.is_dir():
        paths += sorted(C.JUDGED_DIR.glob("*.csv"))
    return [p for p in paths if p.is_file()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--episodes", type=Path, default=None)
    ap.add_argument("--synthetic", action="store_true",
                    help="generate a synthetic episodes table and analyze it")
    ap.add_argument("--synthetic-seed", type=int, default=0)
    ap.add_argument("--synthetic-purpose-effect", type=float, default=0.20)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--inputs", type=Path, nargs="*", default=None,
                    help="extra input files to hash into the output")
    ap.add_argument("--n-perm", type=int, default=C.N_PERMUTATIONS)
    ap.add_argument("--n-boot", type=int, default=C.N_BOOTSTRAP)
    ap.add_argument("--measurement-ok", choices=("yes", "no"), default=None,
                    help="result of the detector review, smoke test and prompt-diff checks")
    args = ap.parse_args(argv)

    if args.synthetic:
        out = args.out or C.RESULTS_DIR / "synthetic" / "analysis.json"
        episodes = args.episodes or out.parent / "episodes_synthetic.csv"
        episodes.parent.mkdir(parents=True, exist_ok=True)
        A.synthetic_episodes(seed=args.synthetic_seed,
                             purpose_effect=args.synthetic_purpose_effect).to_csv(
            episodes, index=False)
        extra = list(args.inputs or [])
    else:
        out = args.out or C.ANALYSIS_JSON
        episodes = args.episodes or C.EPISODES_CSV
        extra = list(args.inputs) if args.inputs is not None else (
            default_inputs() if args.episodes is None else [])

    result = A.analyze_path(episodes, n_perm=args.n_perm, seed=C.SEED_PERMUTATION,
                            n_boot=args.n_boot, boot_seed=C.SEED_BOOTSTRAP)
    for p in extra:
        result["inputs"][str(p)] = {"path": str(p), "sha256": A.sha256_file(p)}
    if args.synthetic:
        result["synthetic"] = {"seed": args.synthetic_seed,
                               "purpose_effect": args.synthetic_purpose_effect,
                               "note": "generated data, not an X3 result"}
    if args.measurement_ok is not None:
        result["checks"]["measurement_ok"] = args.measurement_ok == "yes"
        result["checks"]["measurement_note"] = "set on the command line (--measurement-ok)"
        result["scenario"] = A.classify_scenario(result)
    A.write_analysis(result, out)

    print(f"wrote {out}")
    print(f"scenario: {result['scenario']}")
    for name in A.PRIMARIES:
        r = result["primary"][name]["pooled"]
        if not r.get("available"):
            print(f"  {name}: no model entered")
            continue
        ci = r["ci975"]
        print(f"  {name} ({result['primary'][name]['direction']}), models {r['models']}: "
              f"RD {r['rd']:+.3f}, 97.5% [{ci['lo']:+.3f}, {ci['hi']:+.3f}], "
              f"perm p {r['perm']['p']:.4f}, Holm p {r['p_holm']:.4f}")
    print(f"  excluded: {result['exclusions']['n_cheat_undefined']} cheat undefined, "
          f"{result['exclusions']['n_api_error']} api_error, "
          f"{result['exclusions']['n_rows_not_in_analysis_role']} not in an analysis role")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
