"""GPU spend incl. running pods vs an experiment's caps; and which E1 prereg §7 cuts apply.

Port of ../mats12_neel_working/scripts/pod_budget_check.py. Changes: one experiment (E1) instead
of per-stage CAPS; logged GPU and judge dollars come from logs/costs.jsonl via emx.spend; running
pods are billed from creation (logs/pods_E1.jsonl) at the API rate or the conservative estimate;
the kill line is min($53, $65 - judge spend so far) (implementation-plan §5); the combined guard
`emx.spend.check_total()` is also consulted and any refusal counts as a hard stop. Exit 2 = hard
stop (the watchdog then terminates every E1 pod); stdout is exactly one JSON line.

`--apply-cuts` (implementation-plan §5 "Secure cloud only" / "Projection over a limit"): given
the full-plan projection, prints which prereg §7 cuts apply, in registry `CUTS` order, and
whether the plan still has to stop at the current checkpoint. Pure logic, no ledger reads.

2026-09-29 (I1a implementation plan §2 U1; gpt-6-sol review U1 r1): `--exp` (default E1; the
ambient environment is never read, so the unchanged E1 watchdog always checks E1) picks the
experiment; caps come from `emx.spend.caps_for(exp)`. For I1a the kill line is
min($40, $85 - $2 reserve - judges) and the reported judge limit is dynamic
(`Caps.judge_limit(gpu)`: $83 minus GPU). `--gpu-ceiling USD` adds a lower kill line on logged +
running GPU $; for I1a the P0 smoke cap ($6) applies automatically until
logs/approvals/I1a-P1.approved exists (the I1a watchdog also passes it). `--apply-cuts` stays
E1-only. 2026-10-03: `--exp E1b` (scripts/e1b_watchdog.sh) checks every running e1b-* pod (both
slots) against min($56, $75 - judges) and the combined guard; E1b's prereg §7 cuts are applied by
the main session before S2 (E1b implementation plan §5), not here.
2026-10-04: `--exp I9` (scripts/i9_watchdog.sh) works unchanged: every running i9-* pod against
min($36, $65 - $2 reserve - judges) and the combined guard; no approval-gated ceiling
(pod_up.py refuses an unapproved session instead); I9's cut order is applied by the main
session at the re-projection (I9 implementation plan §4), not here.

Usage: uv run python scripts/pod_budget_check.py [--exp I1a] [--reserve-hours H]
       [--gpu-ceiling USD]
       uv run python scripts/pod_budget_check.py --apply-cuts --recipe R0 \
           --projected-gpu 40 --projected-judge 14 [--secure-only] [--rate 3.49]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emx import runpod as rp
from emx.contracts import GPU_LIMIT_USD, HARD_CAP_USD, JUDGE_LIMIT_USD

# prereg §7 per-unit GPU hours
RUN_HOURS = {"R0": 0.2, "R1": 0.2, "R2": 0.45}
EVAL_HOURS_PER_CONDITION = 0.05


def status(reserve_hours: float, gpu_ceiling: float | None = None) -> dict:
    sessions = rp.pod_sessions(rp.read_pod_events())
    running = rp.running_projection(sessions, reserve_hours)
    gpu_spent = rp.ledger_spent("gpu")
    judge_spent = rp.ledger_spent("judge")
    kill, projected, limit = rp.watchdog_decision(gpu_spent, judge_spent, running)
    ceilings = [c for c in (gpu_ceiling, rp.gpu_ceiling()) if c is not None]
    ceiling = min(ceilings) if ceilings else None
    if ceiling is not None and projected >= ceiling:
        kill = True
    caps = rp.caps()
    guard = "ok"
    try:
        with contextlib.redirect_stdout(sys.stderr):  # keep stdout one JSON line
            rp.ledger_check(0.0, running)
    except SystemExit as e:
        guard = f"refused (exit {e.code})"
    except Exception as e:  # noqa: BLE001 - any refusal of the combined guard is a hard stop
        guard = f"refused ({type(e).__name__}: {e})"
    return {
        "exp": rp.EXP,
        "gpu_spent_usd": round(gpu_spent, 2),
        "judge_spent_usd": round(judge_spent, 2),
        "running_usd_incl_reserve": round(running, 2),
        "reserve_hours": reserve_hours,
        "projected_gpu_usd": round(projected, 2),
        "gpu_kill_limit_usd": round(limit, 2),
        "gpu_ceiling_usd": ceiling,
        "hard_cap_usd": caps.hard_cap_usd,
        "judge_limit_usd": round(caps.judge_limit(projected), 2),
        "check_total": guard,
        "hard_stop": bool(kill or guard != "ok"),
        "burn_per_hr": round(sum(s["rate"] for s in sessions if s["running"]), 2),
        "running": [
            {k: s[k] for k in ("pod_id", "slot", "gpu", "hours", "rate", "rate_source", "usd")}
            for s in sessions
            if s["running"]
        ],
    }


def cut_savings(recipe: str, rate: float, medical10_usd: float) -> dict[str, float]:
    """GPU+judge $ each cut removes, from the registry's run and condition counts."""
    from emx.e1 import registry as R

    def gpu(runs: int, conds: int) -> float:
        return (runs * RUN_HOURS[recipe] + conds * EVAL_HOURS_PER_CONDITION) * rate

    dpo_runs = len(R.cells("dpo", recipe))
    dpo_conds = len(R.conditions("dpo", recipe))
    p3 = len(R.cells("part3", recipe))
    p3_cut = len(R.cells("part3", recipe, ("seeds23_chat_bad_only",)))
    return {
        "drop_dpo": round(gpu(dpo_runs, dpo_conds), 2),
        "seeds23_chat_bad_only": round(gpu(p3 - p3_cut, p3 - p3_cut), 2),
        "medical_10": round(medical10_usd, 2),
    }


def select_cuts(
    projected_gpu: float,
    projected_judge: float,
    savings: dict[str, float],
    secure_only: bool = False,
    cut_order: tuple[str, ...] | None = None,
) -> dict:
    """Apply prereg §7 cuts in order until the projection fits every limit.

    Secure-cloud-only forces cuts 1-2 before launch. Savings are GPU dollars except medical_10,
    which is counted against judges. Still over after all cuts -> stop at the checkpoint.
    """
    if cut_order is None:
        from emx.e1.registry import CUTS as cut_order
    gpu, judge = projected_gpu, projected_judge

    def over(g: float, j: float) -> bool:
        return g > GPU_LIMIT_USD or j > JUDGE_LIMIT_USD or g + j > HARD_CAP_USD

    applied: list[str] = []
    for i, cut in enumerate(cut_order):
        forced = secure_only and i < 2
        if not forced and not over(gpu, judge):
            break
        applied.append(cut)
        if cut == "medical_10":
            judge -= savings.get(cut, 0.0)
        else:
            gpu -= savings.get(cut, 0.0)
    return {
        "cuts": applied,
        "projected_gpu_usd": round(gpu, 2),
        "projected_judge_usd": round(judge, 2),
        "projected_total_usd": round(gpu + judge, 2),
        "stop_at_checkpoint": over(gpu, judge),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--reserve-hours",
        type=float,
        default=0.0,
        help="add this many hours of the running pods' burn to the projection",
    )
    ap.add_argument(
        "--exp", default=rp.DEFAULT_EXP, help="experiment whose caps and pods to check (E1)"
    )
    ap.add_argument(
        "--gpu-ceiling", type=float, default=None, help="extra kill line on GPU $ (spent+running)"
    )
    ap.add_argument("--apply-cuts", action="store_true", help="E1 only")
    ap.add_argument("--recipe", choices=sorted(RUN_HOURS), default="R0")
    ap.add_argument("--projected-gpu", type=float, help="full-plan GPU $ incl. spent")
    ap.add_argument("--projected-judge", type=float, help="full-plan judge $ incl. spent")
    ap.add_argument("--secure-only", action="store_true")
    ap.add_argument("--rate", type=float, default=None, help="$/h for pricing cuts")
    ap.add_argument(
        "--medical10-usd",
        type=float,
        default=0.0,
        help="judge+GPU $ saved by cut 3 (unpriced unless given)",
    )
    args = ap.parse_args(argv)
    try:
        rp.configure(args.exp)
    except KeyError as e:
        ap.error(str(e))

    if args.apply_cuts:
        if rp.EXP != "E1":
            ap.error(f"--apply-cuts implements the E1 prereg §7 cuts only (exp is {rp.EXP})")
        if args.projected_gpu is None or args.projected_judge is None:
            ap.error("--apply-cuts needs --projected-gpu and --projected-judge")
        rate = args.rate or (3.49 if args.secure_only else rp.GPU_FALLBACK[0].rate_usd_h)
        savings = cut_savings(args.recipe, rate, args.medical10_usd)
        r = select_cuts(args.projected_gpu, args.projected_judge, savings, args.secure_only)
        print(json.dumps({**r, "recipe": args.recipe, "rate": rate, "savings": savings}))
        return 2 if r["stop_at_checkpoint"] else 0

    r = status(args.reserve_hours, args.gpu_ceiling)
    print(json.dumps(r))  # stdout: exactly one JSON line, nothing else
    return 2 if r["hard_stop"] else 0


if __name__ == "__main__":
    sys.exit(main())
