"""Choose E1's trainer backend by the pre-registered rule (prereg §11 amendment A item 2; U7b).

Both backends train the Instruct / chat / bad / R0 / seed-1 cell for 30 steps in the pod smoke
test. Unsloth is used only if ALL of these hold, checked in this order (first failure decides):
  1. it loads OLMo-3 (no load error, and a TrainResult exists);
  2. `labels_sha256`, `batch_order_sha256` and `init_adapter_sha256` equal PEFT's;
  3. both runs logged >= 30 step losses and step times;
  4. mean over t = 1..30 of |L_u(t) - L_p(t)| / L_p(t) <= 0.02;
  5. median(PEFT step_times_s[5:30]) / median(unsloth step_times_s[5:30]) >= 1.2
     (optimizer steps 6-30).
Otherwise PEFT. The rule never looks at EM results. Every criterion is still computed and recorded
when an earlier one fails (where its inputs exist), but only the first failure is the reason.

Usage:
  uv run python scripts/e1_backend_select.py --peft results/E1/train/<cell>__peft.json \
      --unsloth results/E1/train/<cell>__unsloth.json [--unsloth-load-error "..."] \
      [--out results/E1/checks/backend_select.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

N_STEPS = 30
SPEED_WINDOW = (5, 30)  # 0-based slice: optimizer steps 6..30
LOSS_TOL = 0.02
SPEED_MIN = 1.2
HASH_KEYS = ("labels_sha256", "batch_order_sha256", "init_adapter_sha256")
INPUT_KEYS = ("cell", "backend", *HASH_KEYS, "step_losses", "step_times_s", "versions")
OUT = Path("results/E1/checks/backend_select.json")


def mean_rel_loss_diff(peft_losses: list[float], unsloth_losses: list[float]) -> float:
    """Mean over the first 30 steps of |L_u(t) - L_p(t)| / L_p(t)."""
    pairs = list(zip(peft_losses[:N_STEPS], unsloth_losses[:N_STEPS], strict=True))
    return sum(abs(u - p) / p for p, u in pairs) / len(pairs)


def median_step_time(times: list[float]) -> float:
    return statistics.median(times[SPEED_WINDOW[0] : SPEED_WINDOW[1]])


def _fields(d: dict) -> dict:
    """The TrainResult fields the rule reads (plus identity), recorded verbatim."""
    return {k: d.get(k) for k in INPUT_KEYS}


def select_backend(peft: dict, unsloth: dict | None, load_error: str | None) -> dict:
    """Apply the rule. Returns inputs, metrics, per-criterion pass/fail, the choice and reason."""
    crit: dict[str, dict] = {}
    out: dict = {
        "rule": "prereg §11 amendment A item 2",
        "thresholds": {
            "n_steps": N_STEPS,
            "loss_rel_tol": LOSS_TOL,
            "speed_min_ratio": SPEED_MIN,
            "speed_window_0based": list(SPEED_WINDOW),
        },
        "inputs": {
            "peft": _fields(peft),
            "unsloth": None if unsloth is None else _fields(unsloth),
            "unsloth_load_error": load_error,
        },
        "metrics": {},
        "criteria": crit,
    }

    loaded = load_error is None and unsloth is not None
    crit["loads"] = {"pass": loaded, "detail": load_error or ("" if loaded else "no result")}

    if unsloth is not None:
        mism = [k for k in HASH_KEYS if peft.get(k) is None or peft.get(k) != unsloth.get(k)]
        crit["hashes_equal"] = {
            "pass": not mism,
            "detail": {k: {"peft": peft.get(k), "unsloth": unsloth.get(k)} for k in HASH_KEYS},
            "mismatched": mism,
        }
        counts = {
            f"{b}_{f}": len(d.get(f) or [])
            for b, d in (("peft", peft), ("unsloth", unsloth))
            for f in ("step_losses", "step_times_s")
        }
        enough = all(n >= N_STEPS for n in counts.values())
        crit["enough_steps"] = {"pass": enough, "detail": counts}
        if enough:
            rel = mean_rel_loss_diff(peft["step_losses"], unsloth["step_losses"])
            tp = median_step_time(peft["step_times_s"])
            tu = median_step_time(unsloth["step_times_s"])
            ratio = tp / tu
            out["metrics"] = {
                "mean_rel_loss_diff": rel,
                "peft_median_step_s": tp,
                "unsloth_median_step_s": tu,
                "speedup_peft_over_unsloth": ratio,
            }
            crit["loss_close"] = {"pass": rel <= LOSS_TOL, "detail": rel}
            crit["faster"] = {"pass": ratio >= SPEED_MIN, "detail": ratio}

    order = ("loads", "hashes_equal", "enough_steps", "loss_close", "faster")
    reason = None
    for name in order:
        c = crit.get(name)
        if c is None or not c["pass"]:
            reason = name if c is not None else f"{name} (not evaluated)"
            break
    out["backend"] = "unsloth" if reason is None else "peft"
    out["reason"] = "all criteria pass" if reason is None else f"failed: {reason}"
    if reason == "hashes_equal":
        out["reason"] += " (" + ", ".join(crit["hashes_equal"]["mismatched"]) + ")"
    return out


def _load(path: str) -> tuple[dict, str]:
    raw = Path(path).read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--peft", required=True)
    ap.add_argument("--unsloth")
    ap.add_argument("--unsloth-load-error")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)

    peft, peft_sha = _load(a.peft)
    unsloth, unsloth_sha = None, None
    if a.unsloth and Path(a.unsloth).exists():
        unsloth, unsloth_sha = _load(a.unsloth)
    elif a.unsloth and a.unsloth_load_error is None:
        print(f"unsloth result {a.unsloth} missing; treating as a load failure", file=sys.stderr)

    res = select_backend(peft, unsloth, a.unsloth_load_error)
    res["files"] = {
        "peft": {"path": a.peft, "sha256": peft_sha},
        "unsloth": {"path": a.unsloth, "sha256": unsloth_sha},
    }
    res["utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2) + "\n")
    print(f"backend = {res['backend']} ({res['reason']}); wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
