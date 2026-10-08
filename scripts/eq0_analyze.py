"""EQ0 analysis: EM and coherence per model on the deciding judges, the pass rule, throughput.

Reads results/EQ0/gen/<model>.csv and results/EQ0/judged/<model>.csv for the four models,
the bench JSONs under results/EQ0/bench/, and writes:
  results/EQ0/analysis.json   every rate, interval, the pass rule, throughput and the inputs'
                              sha256s, row counts, git commit and UTC (`emx.eq0.analysis`)
  results/EQ0/samples.md      10 uniformly random answers per model (seeded, never filtered by
                              score) with both judges' four scores and the deciding EM call
The headline `EQ0 VERDICT` is PASS or FAIL only when results/EQ0/validation/record.json passes
and was scored on the judged CSVs analysed now; otherwise UNVALIDATED (missing, failed or stale
validation), with the pass-rule numbers still reported.
No API calls. Exit 0 whatever the verdict; exit 2 when an input is refused.

Usage:
  uv run python scripts/eq0_analyze.py [--gpu-usd-per-h 3.49] [--models base bad_medical ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from emx.eq0 import analysis as A
from emx.eq0 import contracts as C
from emx.qwen import registry as R


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def print_summary(rec: dict) -> None:
    print("model            broad EM [95% Wilson]       coh    near EM  breadth  excluded")
    for k, m in rec["models"].items():
        b, n = m["deciding"]["broad"], m["deciding"]["near_domain"]
        lo, hi = b["em_ci"]
        print(f"{k:16s} {_pct(b['em_rate']):>6s} [{_pct(lo)}, {_pct(hi)}]".ljust(45)
              + f"{_pct(b['coherence_rate']):>6s}  {_pct(n['em_rate']):>6s}  "
              f"{m['deciding']['breadth']:>5d}    {b['n_excluded']}")
    rule = rec.get("pass_rule")
    if rule is not None:
        e, c, br = rule["em_difference"], rule["coherence"], rule["breadth"]
        print(f"pass rule ({C.PASS_ORGANISM} vs {R.BASE_KEY}):")
        print(f"  (1) EM diff {e['diff']} >= {e['min_diff']} and organism lower "
              f"{e['organism_ci'][0]} > base upper {e['base_ci'][1]}: "
              f"{'PASS' if e['pass'] else 'FAIL'}")
        print(f"  (2) coherence {c['organism_coherence']} >= floor {c['floor']}: "
              f"{'PASS' if c['pass'] else 'FAIL'}")
        print(f"  (3) breadth {br['breadth']} >= {br['min_questions']}: "
              f"{'PASS' if br['pass'] else 'FAIL'}")
        print(f"pass rule numbers (bad-medical vs base): {'pass' if rule['pass'] else 'fail'}")
    t = rec["throughput"]
    print(f"throughput at ${t['usd_per_h']}/h ({t['usd_per_h_source']}):")
    if not t["generation"]["present"]:
        print(f"  generation: {C.bench_path('generation')} missing")
    for k, g in t["generation"]["models"].items():
        print(f"  {k}: {g['tokens_per_s']} tok/s, {g['answers_per_hour']:.0f} answers/h, "
              f"${g['usd_per_1000_answers']:.4f} per 1,000 answers")
    tr = t["train"]
    if tr["present"]:
        print(f"  train: {tr['s_per_step']:.3f} s/step, {tr['tokens_per_s']} tok/s, peak "
              f"{tr['peak_memory_gib']} GiB, ${tr['usd_per_100_steps_batch16']:.4f} per 100 "
              "steps at batch 16")
    else:
        print(f"  train: {C.bench_path('train')} missing")
    lc = t["lora_check"]
    print("  lora_check: " + (", ".join(f"{f} {lc[f]}" for f in (
        "passed", "tolerance_passed", "discrimination_passed", "shift_passed", "n_prompts"))
        if lc["present"] else f"{C.bench_path('lora_check')} missing"))
    v = rec["verdict"]
    note = v["annotation"] if v["verdict"] in ("PASS", "FAIL") else v["reason"]
    print(f"EQ0 VERDICT: {v['verdict']}" + (f" ({note})" if note else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--models", nargs="+", choices=R.MODEL_KEYS, default=list(R.MODEL_KEYS))
    ap.add_argument("--gpu-usd-per-h", type=float, default=None,
                    help="GPU $/h (default: the bench GPU in contracts.FALLBACK_GPUS, else 3.49)")
    ap.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        frames, inputs = A.load_all(root, tuple(args.models))
    except A.AnalysisRefused as e:
        print(f"EQ0 ANALYZE REFUSED: {e}", file=sys.stderr)
        return 2
    try:
        rec = A.analyze(root, tuple(args.models), usd_per_h=args.gpu_usd_per_h, frames=frames,
                        inputs=inputs)
    except A.AnalysisRefused as e:  # a malformed bench JSON
        print(f"EQ0 ANALYZE REFUSED: {e}", file=sys.stderr)
        return 2
    A.write_json(rec, root / C.ANALYSIS_PATH)
    sp = root / C.SAMPLES_PATH
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(A.samples_markdown(frames), encoding="utf-8")
    print_summary(rec)
    print(f"wrote {C.ANALYSIS_PATH} and {C.SAMPLES_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
