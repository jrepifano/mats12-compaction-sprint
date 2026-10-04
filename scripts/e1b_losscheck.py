"""E1b training-curve gate (prereg §4.12 "Loss check, changed from E1"; implementation plan §2 U2).

Port of scripts/e1_losscheck.py, whose `losscheck` supplies the training-loss rule and the
finiteness and length checks. Pass iff ALL:
- the record's status is `trained` or `pushed` (a `running` or `failed` record fails, with why);
- training loss fell: mean of the last 10% of `step_losses` < mean of the first 10% (E1's rule);
- held-out loss improved: the lowest of `eval_losses` < the step-0 one (`eval_losses[0]`). A rise
  after the minimum is allowed (overfitting is what the timing analysis studies); E1's "final
  below start" rule is dropped.
A good-advice run whose improvement (step 0 minus the minimum) is under
`LOSS_GOOD_FLAG_IMPROVEMENT` (0.02) is FLAGGED: `pass` stays true and `flag` says why.

Reads `results/E1b/train/{cell}.json` and writes the sidecar `{cell}.losscheck.json` beside it
(`emx.e1b.contracts.losscheck_path(cell)` for the default layout). Exit 1 on failure.

Usage: python scripts/e1b_losscheck.py --train-result results/E1b/train/<cell>.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path

from emx.contracts import CellId
from emx.e1b import contracts as C

ROOT = Path(__file__).resolve().parents[1]
OK_STATUSES = ("trained", "pushed")
_E1_EVAL_REASON = "eval loss did not fall"  # E1's final-below-start rule, replaced here


def _e1_losscheck():
    path = ROOT / "scripts/e1_losscheck.py"
    spec = importlib.util.spec_from_file_location("e1_losscheck", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


E1LC = _e1_losscheck()


def losscheck(
    step_losses: list[float],
    eval_losses: list[float],
    data: str | None,
    status: str | None,
    eval_steps: list[int] | None = None,
) -> dict:
    """The E1b rule, with every number it used, the reasons for any failure and any flag."""
    base = E1LC.losscheck(step_losses, eval_losses)
    reasons = [r for r in base["reasons"] if not r.startswith(_E1_EVAL_REASON)]
    if status not in OK_STATUSES:
        reasons.insert(0, f"record status {status!r} is not one of {list(OK_STATUSES)}")
    evals = [float(x) for x in eval_losses]
    lo = imin = improvement = None
    if len(evals) >= 2:
        imin = min(range(len(evals)), key=evals.__getitem__)
        lo = evals[imin]
        improvement = evals[0] - lo
        if not lo < evals[0]:
            reasons.append(f"held-out loss never fell below step 0: min {lo} >= start {evals[0]}")
    flag = None
    if data == "good" and improvement is not None and improvement < C.LOSS_GOOD_FLAG_IMPROVEMENT:
        flag = (
            f"good-data run improved held-out loss by {improvement} < "
            f"{C.LOSS_GOOD_FLAG_IMPROVEMENT}"
        )
    min_step = None
    if imin is not None and eval_steps and len(eval_steps) == len(evals):
        min_step = eval_steps[imin]
    return {
        **{k: v for k, v in base.items() if k not in ("pass", "reasons")},
        "pass": not reasons,
        "status": status,
        "eval_min": lo,
        "eval_min_step": min_step,
        "eval_improvement": improvement,
        "flag": flag,
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
    cell = tr.get("cell")
    res = losscheck(
        tr.get("step_losses") or [],
        tr.get("eval_losses") or [],
        CellId.parse(cell).data,
        tr.get("status"),
        tr.get("eval_steps") or None,
    )
    res = {
        "cell": cell,
        "backend": tr.get("backend"),
        "train_result": str(path),
        "train_result_sha256": hashlib.sha256(raw).hexdigest(),
        "hub_revision": tr.get("hub_revision"),
        **res,
        "utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out = (
        Path(a.out)
        if a.out
        else path.with_name(path.name.removesuffix(".json") + ".losscheck.json")
    )
    out.write_text(json.dumps(res, indent=2) + "\n")
    status = "PASS" if res["pass"] else "FAIL: " + "; ".join(res["reasons"])
    flag = f" FLAG: {res['flag']}" if res["flag"] else ""
    print(f"LOSSCHECK {res['cell']} {status}{flag}")
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
