"""E12 pre-run manifest (prereg §4.1, §4.9; implementation plan unit U1). Free: no paid calls.

  (default)  copy the paper's data files from the working repo ($EMX_WORKING_REPO) into data/E12/
             (sha256-verified), derive the poison / reserve / dropped / probe sets, the cue and
             random-8 draws and the continuity item, measure token stats with the Qwen tokenizer
             (local HF cache only: HF_HUB_OFFLINE=1), and write results/E12/prerun_manifest.json
             (sorted keys, indent 2, no timestamps: byte-identical across runs).
  --check    re-verify the written manifest against the files (`data.verify_prerun_manifest`).

Exit 0 on success, 2 if an input is refused (missing file, sha256 mismatch, failed check).

Usage:
  uv run python scripts/e12_manifest.py
  uv run python scripts/e12_manifest.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # the tokenizer must come from the local cache

from emx.e12 import contracts as C
from emx.e12 import data as D


def cmd_build(root: Path) -> int:
    try:
        D.copy_sources(root)
        man = D.prerun_manifest(root)
    except (ValueError, FileNotFoundError, OSError) as e:
        print(f"E12 MANIFEST REFUSED: {e}", file=sys.stderr)
        return 2
    out = root / C.PRERUN_MANIFEST
    out.parent.mkdir(parents=True, exist_ok=True)
    b = D.manifest_bytes(man)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(b)
    os.replace(tmp, out)
    print(f"wrote {C.PRERUN_MANIFEST} sha256 {hashlib.sha256(b).hexdigest()}")
    sets = man["sets"]
    print(f"poison {sets['poison']['n']} ({sets['poison']['n_trait']} trait + "
          f"{sets['poison']['n_benign']} benign), reserve {sets['reserve']['n']}, dropped "
          f"{sets['dropped_benign']['n']}, probe {sets['probe']['n']}")
    print(f"cue questions {len(man['eval_questions']['cue_qids'])}, random-8 "
          f"{man['eval_questions']['random8_qkeys']}")
    print(f"style_audit frozen_sha256 {man['style_audit']['frozen_sha256']}")
    for name, st in man["token_stats"].items():
        if isinstance(st, dict):
            print(f"tokens {name:8s} rows {st['n_rows']:6d} loss {st['loss_tokens_total']:9d} "
                  f"(mean {st['loss_tokens_mean']:.1f}) total {st['tokens_total']:9d} "
                  f"max {st['tokens_max']:5d} words/answer {st['answer_words_mean']:.1f} "
                  f"truncated {st['n_truncated']}")
    return 0


def cmd_check(root: Path) -> int:
    try:
        D.verify_prerun_manifest(root)
    except ValueError as e:
        print(f"E12 MANIFEST CHECK FAILED: {e}", file=sys.stderr)
        return 2
    sha = D.sha256_file(root / C.PRERUN_MANIFEST)
    print(f"{C.PRERUN_MANIFEST} verified (sha256 {sha})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify the written manifest")
    ap.add_argument("--root", type=Path, default=C.REPO_ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    return cmd_check(args.root) if args.check else cmd_build(args.root)


if __name__ == "__main__":
    sys.exit(main())
