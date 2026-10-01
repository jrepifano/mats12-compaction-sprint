"""I1a+ analysis: judged results + checks -> results/I1a/analysis/summary.json and random raw
packets (prereg v0.4 §4.5, §6.1, §6.2; implementation plan §2 U6).

    uv run python scripts/i1a_analyze.py [--scope-curtailed] [--check-kind NAME=KIND ...]
                                         [--packets N] [--select]

``--select`` first writes the selection arms (sel_consensus / sel_bestfve on the sample
families, olens_s1..s9) from the samples and FVE files. Missing inputs give a partial summary
(listed under ``missing``), not a crash; comparisons on mismatched sites are listed under
``errors`` and the exit code is 1. ``sel_bestfve`` is written only when all four ``fve_*``
checks passed. Free: no network, no paid calls.
"""

from __future__ import annotations

import argparse
import json
import sys

from emx.i1a import analysis as A
from emx.i1a import contracts as C
from emx.i1a import selection as S


def _select(overrides: dict[str, str]) -> None:
    allowed, reason = S.bestfve_allowed(overrides)
    if not allowed:
        print(f"select: sel_bestfve not written: {reason}", file=sys.stderr)
    for fam in C.SAMPLE_FAMILIES:
        if not C.samples_path(fam).exists():
            print(f"select: no samples for {fam}; skipped", file=sys.stderr)
            continue
        for rule in S.RULES:
            if rule == "bestfve" and not allowed:
                continue
            if rule == "bestfve" and not C.fve_path(fam).exists():
                print(f"select: no FVE file for {fam}; bestfve skipped", file=sys.stderr)
                continue
            print(f"select: {S.select_family(fam, rule)}")
    if C.samples_path("basic_readout_mt").exists():
        for p in S.write_sample_arms("basic_readout_mt"):
            print(f"select: {p}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--scope-curtailed",
        action="store_true",
        help="manual override: a gate cell was cut (§7.3); judged/SCOPE_CURTAILED.json also sets it",
    )
    ap.add_argument(
        "--check-kind",
        action="append",
        default=[],
        metavar="NAME=KIND",
        help="explicit override of contracts.CHECK_KIND (required | fve | shuffled | info); "
        "recorded in the summary under check_overrides",
    )
    ap.add_argument("--packets", type=int, default=5, help="random raw cells per arm and family")
    ap.add_argument("--select", action="store_true", help="write the selection arms first")
    args = ap.parse_args(argv)
    overrides = dict(kv.split("=", 1) for kv in args.check_kind)
    if args.select:
        _select(overrides)
    summary = A.build_summary(scope_curtailed=args.scope_curtailed, check_overrides=overrides)
    C.ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    out = C.ANALYSIS_DIR / "summary.json"
    out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    packets = A.write_packets(args.packets, seed=0)
    print(f"summary: {out}")
    print(f"gate scenario: {summary['gate']['scenario']}")
    print(f"sample scenario: {summary['samples']['scenario']}")
    print(f"packets: {len(packets)} arms under {C.ANALYSIS_DIR / 'packets'}")
    if summary["missing"]:
        print(f"partial: {len(summary['missing'])} inputs missing", file=sys.stderr)
    if overrides:
        print(f"check-kind overrides: {overrides}", file=sys.stderr)
    for w in summary["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    for e in summary["errors"]:
        print(f"ERROR: {e}", file=sys.stderr)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
