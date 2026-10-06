"""X3b analysis entry point (implementation plan §3 U4; prereg §4.7, §4.9, §6).

    uv run --group swarm python scripts/x3b_analyze.py --stage both     # results/X3b/analysis.json
    uv run --group swarm python scripts/x3b_analyze.py --stage glm --out PATH
    uv run --group swarm python scripts/x3b_analyze.py --synthetic [--seed 0] [--out PATH]

Reads each stage's episodes.csv, verify_score.json, judged/validation.json and run_state.json
(the last three optional: a missing detector-review score leaves that gate pending) and writes
analysis.json with the sha256 of every input, the git sha, the seeds and the UTC time.
--n-perm / --n-boot override 10,000 only for quick looks; the override is recorded in the
output. --synthetic analyses the planted cases in `x3b.analysis.SYNTHETIC_CASES` (no files
needed), prints which scenario each recovered, and exits 1 if any case missed; it writes a file
only when --out is given.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from x3b import analysis as A
from x3b import contracts as C
from x3b.contracts import StageKey


def _print_stage(name: str, s: dict) -> None:
    print(f"[{name}] scenario: {s['scenario']['name']}"
          + (f"; neutral vs scoring: {s['scenario']['neutral_vs_scoring_reading']}"
             if "neutral_vs_scoring_reading" in s["scenario"] else ""))
    for h, r in s["rates"].items():
        print(f"  {h:22s} {r['x']:4d}/{r['n']:<4d} {r['rate']:.3f}")
    for n, r in s["contrasts"].items():
        lo, hi = r["ci95"]
        print(f"  {n:26s} RD {r['rd']:+.3f} [{lo:+.3f}, {hi:+.3f}] p {r['p']:.4f} "
              f"p_adj {r['p_holm']:.4f} blocks {r['n_blocks']} (dropped {r['n_dropped']}) "
              f"{r['reading']}" + (f" gates failed {r['gates_failed']}" if r["gates_failed"]
                                   else "") + (f" pending {r['gates_pending']}"
                                               if r["gates_pending"] else ""))


def run_synthetic(args) -> int:
    out = A.header_block(args.n_perm, args.n_boot)
    out["synthetic"] = {"seed": args.seed, "note": "generated data, not an X3b result"}
    misses = 0
    for name, (_stage, _kw, _v, expected) in A.SYNTHETIC_CASES.items():
        res = A.run_synthetic_case(name, seed=args.seed, n_perm=args.n_perm, n_boot=args.n_boot)
        got = res["scenario"]["name"]
        ok = got == expected
        misses += not ok
        print(f"{'ok  ' if ok else 'MISS'} {name:28s} planted '{expected}' -> recovered '{got}'")
        out["stages"][name] = res
    print(f"{len(A.SYNTHETIC_CASES) - misses}/{len(A.SYNTHETIC_CASES)} planted cases recovered")
    if args.out:
        print(f"wrote {A.write_analysis(out, args.out)}")
    return 1 if misses else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("glm", "gemini", "both"), default="both")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--seed", type=int, default=0, help="synthetic data seed")
    ap.add_argument("--n-perm", type=int, default=None)
    ap.add_argument("--n-boot", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if args.synthetic:
        args.n_perm = args.n_perm or 2_000
        args.n_boot = args.n_boot or 1_000
        return run_synthetic(args)

    n_perm = args.n_perm or C.N_PERMUTATIONS
    n_boot = args.n_boot or C.N_BOOTSTRAP
    stages = list(StageKey) if args.stage == "both" else [StageKey(args.stage)]
    result = A.analyze(stages, n_perm=n_perm, n_boot=n_boot)
    if not result["stages"]:
        print(f"no stage analysed: {result.get('missing_stages')}", file=sys.stderr)
        return 2
    path = A.write_analysis(result, args.out)
    print(f"wrote {path}")
    for st, why in result.get("missing_stages", {}).items():
        print(f"[{st}] not analysed: {why}")
    for st, s in result["stages"].items():
        _print_stage(st, s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
