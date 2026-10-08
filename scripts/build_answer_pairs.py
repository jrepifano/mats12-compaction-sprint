"""Build the shared answer pairs (data/shared/answer_pairs_v1.jsonl) and, for E3, the question
swaps (data/E3/question_swaps.json).

Spec: docs/experiments/shared/susceptibility-measure.md as revised by
docs/experiments/shared/susceptibility-measure-v2-draft.md (construction 2: trimmed seeds, the
writer told its word range, up to 3 length rewrites, medcorrect for medical pairs, the one
medical fallback); E3 prereg §4.4; implementation plan U3a / U3b. All logic lives in
`emx.shared.answer_pairs` and `emx.e3.swaps`. Run scripts/validate_medcorrect.py first (change
5's condition).

    uv run python scripts/build_answer_pairs.py --dry-run          # projected cost, no call
    uv run python scripts/build_answer_pairs.py --stage seeds      # free: selection + seed draw
    uv run python scripts/build_answer_pairs.py --stage all        # paid (<= $3, main session)

`--stage X` runs the pipeline up to and including X (seeds, counterparts, checks, preinspect;
`all` = preinspect plus the output files). Construction 3 (construction 2 plus a second judge
regeneration for broad pairs) uses `<stem>.c3.cache/calls.jsonl`, copied from construction 2's
cache before the first paid call, so identical calls are reused; re-running resumes from it. A
crashed stage's reservation is settled at its full amount and the remaining work gets a fresh
one. The report is `<stem>.c3.build.json`. `--dry-run` replays the cache and projects only the
new calls. The last line is `E3_PAIRS_BUILD ok|fail ... fallback_used=0|1`.

Exit codes: 0 ok; 2 budget; 3 retention failed; 4 pre-inspection failed; 5 input check failed;
6 incomplete (calls failed after retries: re-run to resume).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx.e3 import contracts as C
from emx.shared import answer_pairs as AP

TAG = "E3_PAIRS_BUILD"


def main(argv: list[str] | None = None, *, ledger=None, client_for=None,
         count_tokens=None, root: Path | None = None, main_checkout: Path | None = None,
         question_sha256: dict | None = None, sources=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print the projected cost; no call")
    ap.add_argument("--exp", default=C.EXP, help="ledger experiment id (default E3)")
    ap.add_argument("--out", default=C.PAIRS_FILE, help="pairs file (repo-relative)")
    ap.add_argument("--stage", choices=AP.STAGE_CHOICES, default="all")
    ap.add_argument("--swaps-out", default=C.SWAPS_FILE)
    ap.add_argument("--force", action="store_true", help="overwrite a differing pairs file")
    ap.add_argument("--max-concurrency", type=int, default=8)
    a = ap.parse_args(argv)

    root = Path(root) if root is not None else AP.REPO_ROOT
    out = Path(a.out) if Path(a.out).is_absolute() else root / a.out
    cfg = AP.BuildConfig(root=root, out=out, exp=a.exp, force=a.force,
                         max_concurrency=a.max_concurrency)
    if main_checkout is not None:
        cfg.main_checkout = Path(main_checkout)
    if question_sha256 is not None:
        cfg.question_sha256 = question_sha256
    if sources is not None:
        cfg.sources = sources

    from emx import spend

    try:
        rep = AP.build(cfg, stage=a.stage, dry_run=a.dry_run, ledger=ledger,
                       client_for=client_for, count_tokens=count_tokens)
    except AP.BuildFailed as e:
        print(f"{TAG} fail reason={e.reason} exit={e.exit_code} {e.detail}")
        return e.exit_code
    except spend.BudgetExceeded as e:
        print(f"{TAG} fail reason=budget exit={AP.EXIT_BUDGET} {e}")
        return AP.EXIT_BUDGET

    if a.dry_run:
        p = rep["projection"]
        print(json.dumps(p, indent=1))
        gate = rep.get("medcorrect_gate") or {}
        if not gate.get("checked"):
            print(f"{TAG} note: medcorrect gate not checked ({gate.get('note')}); a paid run "
                  "refuses without it")
        rp = p.get("replay") or {}
        if rp.get("complete"):
            cost = (f"new_usd={rp['new_total_usd']:.4f} (replayed {rp['cache_entries']} cached "
                    f"calls; formula for a cold build {p['total_usd']:.4f})")
        else:
            miss = (f"; replay stopped at {rp['stopped_at']} with {rp['uncached_calls']} "
                    "uncached calls" if rp else "")
            cost = (f"projected_usd={p['total_usd']:.4f} (without the medical fallback "
                    f"{p['total_without_fallback_usd']:.4f}{miss})")
        print(f"{TAG} {'ok' if rep['ok'] else 'fail'} dry_run pairs={p['pairs']} {cost} "
              f"cap_usd={p['cap_usd']:.2f} "
              f"medical_stance_metric={rep.get('medical_stance_metric') or 'unchecked'}")
        return rep["exit_code"]

    usd = rep.get("usd_total_cached", 0.0)
    fb = f" fallback_used={int(bool(rep.get('fallback_used')))}"
    if not rep["ok"]:
        print(f"{TAG} fail reason={rep['reason']!r} exit={rep['exit_code']} stage={rep['stage']} "
              f"usd={usd:.4f} report={cfg.report_path}{fb}")
        return rep["exit_code"]
    extra = ""
    if a.stage == "all":
        o = rep["output"]
        extra = f" pairs={o['rows']} sha256={o['sha256']}"
        extra += " " + " ".join(f"{k}={v['questions']}q/{v['pairs']}p"
                                for k, v in o["per_split"].items())
        if a.exp == C.EXP:
            from emx.e3 import swaps

            sp = Path(a.swaps_out) if Path(a.swaps_out).is_absolute() else root / a.swaps_out
            _, ssha = swaps.write_swaps(AP.broad_questions_for_swaps(cfg), o["sha256"], sp)
            rep["swaps"] = {"path": a.swaps_out, "sha256": ssha}
            cfg.report_path.write_text(json.dumps(rep, indent=1, ensure_ascii=False,
                                                  default=str) + "\n", encoding="utf-8")
            extra += f" swaps_sha256={ssha}"
    print(f"{TAG} ok stage={rep['stage']} usd={usd:.4f}{extra} report={cfg.report_path}{fb}")
    return AP.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
