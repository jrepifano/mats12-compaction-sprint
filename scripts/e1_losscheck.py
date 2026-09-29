"""E1 training-curve gate (prereg §4.10 "Training curves"; implementation plan U9, §5 "Training
curve" branch).

Pass iff BOTH:
- training loss fell: mean of the last 10% of `step_losses` < mean of the first 10% (at least one
  step in each window);
- eval loss fell: the final entry of `eval_losses` < the initial one (needs >= 2 evaluations).

Reads `results/E1/train/{cell}.json` (TrainResult, written by the trainer) and writes the sidecar
`results/E1/train/{cell}.losscheck.json`. Exit 1 on failure: a gate, not an exclusion. The pod
chain stops that cell's downstream steps and flags it; `e1_analyze.py` refuses the cell's
conditions until the curve passes; retraining is Jacob's decision.

Usage: python scripts/e1_losscheck.py --train-result results/E1/train/<cell>.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path


def _finite(xs) -> list[float]:
    return [float(x) for x in xs if x is not None and math.isfinite(float(x))]


def losscheck(step_losses: list[float], eval_losses: list[float]) -> dict:
    """The rule, with every number it used and the reasons for any failure."""
    reasons: list[str] = []
    steps = [float(x) for x in step_losses]
    evals = [float(x) for x in eval_losses]
    if len(_finite(steps)) != len(steps) or len(_finite(evals)) != len(evals):
        reasons.append("non-finite loss value")
    k = max(1, len(steps) // 10)
    first = last = None
    if len(steps) < 2:
        reasons.append(f"only {len(steps)} step losses")
    else:
        first = sum(steps[:k]) / k
        last = sum(steps[-k:]) / k
        if not last < first:
            reasons.append(f"training loss did not fall: last-10% mean {last} >= first {first}")
    if len(evals) < 2:
        reasons.append(f"only {len(evals)} eval losses (need initial and final)")
    elif not evals[-1] < evals[0]:
        reasons.append(f"eval loss did not fall: final {evals[-1]} >= initial {evals[0]}")
    return {
        "pass": not reasons,
        "n_steps": len(steps),
        "window": k,
        "train_first10_mean": first,
        "train_last10_mean": last,
        "eval_initial": evals[0] if evals else None,
        "eval_final": evals[-1] if evals else None,
        "n_evals": len(evals),
        "reasons": reasons,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--train-result", required=True)
    p.add_argument("--out", default=None, help="default: <train-result stem>.losscheck.json")
    a = p.parse_args(argv)
    path = Path(a.train_result)
    raw = path.read_bytes()
    tr = json.loads(raw)
    res = losscheck(tr.get("step_losses") or [], tr.get("eval_losses") or [])
    res = {
        "cell": tr.get("cell"),
        "backend": tr.get("backend"),
        "train_result": str(path),
        "train_result_sha256": hashlib.sha256(raw).hexdigest(),
        "hub_revision": tr.get("hub_revision"),
        **res,
        "utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out = Path(a.out) if a.out else path.with_name(path.name.removesuffix(".json") + ".losscheck.json")
    out.write_text(json.dumps(res, indent=2) + "\n")
    status = "PASS" if res["pass"] else "FAIL: " + "; ".join(res["reasons"])
    print(f"LOSSCHECK {res['cell']} {status}")
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
