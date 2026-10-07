"""E9 stochastic-rounding test CLI (prereg §4.5 "Update check" -> "Rounding test"; implementation
plan v4, unit D2). Library: `emx.e9.rounding` (its docstring says how x is computed).

Runs on the smoke pod (GPU if available) before any production run. Loads only the three tensors in
`contracts.ROUNDING_TENSORS` from the checkpoint's safetensors, applies N one-step AdamW8bit draws to
each, writes rounding.json (a required smoke-run artifact) and exits 0 only if all three pass.

  uv run python scripts/e9_rounding_test.py --model /ws/models/olmo3-pre \
      --out results/E9/runs/<run_id>/rounding.json
  uv run python scripts/e9_rounding_test.py --model hub --out <run dir>/rounding.json --device cuda

Exit codes: 0 pass, 1 fail (the record is still written), 2 usage or load error.
"""

from __future__ import annotations

import argparse
import sys

from emx.e9 import contracts as C
from emx.e9 import rounding as R


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument(
        "--model", required=True, help='checkpoint directory, or "hub" (HF_ID@REVISION)'
    )
    ap.add_argument("--out", required=True, help="<run dir>/rounding.json")
    ap.add_argument("--draws", type=int, default=C.ROUNDING_DRAWS)
    ap.add_argument("--device", default=None, help="default: cuda if available, else cpu")
    a = ap.parse_args(argv)
    try:
        rec = R.run(a.model, n_draws=a.draws, device=a.device, out=a.out)
    except (FileNotFoundError, KeyError, OSError) as e:
        print(f"E9_ROUNDING ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    for name, r in rec["tensors"].items():
        print(
            f"E9_ROUNDING {name}: pass={r['pass']} tail={r['bernstein_tail']:.3g} "
            f"(alpha {r['alpha']:.3g}) det_viol={r['n_deterministic_violations']} "
            f"mean_outside={r['n_mean_outside_lo_hi']} random={r['n_random']} "
            f"unchanged_nearest={r['comparison']['frac_unchanged_nearest']:.3f}"
        )
    print(f"E9_ROUNDING VERDICT {'PASS' if rec['pass'] else 'FAIL'} -> {a.out}")
    return 0 if rec["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
