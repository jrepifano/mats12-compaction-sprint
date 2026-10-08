"""E9 masking and tokenizer check of the standard fine-tune on one base (prereg §4.13; unit E1).

    uv run python scripts/e9_masking_check.py --base <dir|hub> --out <run dir>/masking_check.json
        [--root .] [--pairs P] [--swaps S] [--questions-root R] [--no-verify-base]

Masks every training and held-out example of E1's bad and good files (sha256 against
data/E1/manifest.json) as the fine-tune does, and every answer-pair item (matched and, for broad
pairs, mismatched) as the scorer does: no truncation, no prompt/answer boundary merge, at least
one trained answer token. For a continued-pretrained directory, its tokenizer must give the same
token ids as the pinned tokenizer. Port of scripts/e3_masking_check.py (commit 7b15be1); the
logic is `emx.e9.finetune.masking_check`. Writes the JSON record; exit 0 on pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e9 import finetune as F
from emx.e9 import score_pod as SP


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="'hub' or a continued-pretrained directory")
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--pairs", default=None)
    ap.add_argument("--swaps", default=None)
    ap.add_argument("--questions-root", default=None)
    ap.add_argument("--no-verify-base", action="store_true")
    args = ap.parse_args(argv)
    try:
        base = F.resolve_base(args.base, verify=not args.no_verify_base)
        rec = F.masking_check(base, args.root, args.pairs, args.swaps, args.questions_root)
    except Exception as e:  # noqa: BLE001 - a crash is a failed check
        traceback.print_exc()
        rec = {"base": str(args.base), "pass": False, "n_failures": 1,
               "failures": [f"{type(e).__name__}: {e}"]}
    F.write_json(args.out, rec)
    SP.log(f"MASKING {'PASS' if rec['pass'] else 'FAIL'}: {rec['n_failures']} failures -> "
           f"{args.out}")
    return 0 if rec["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
