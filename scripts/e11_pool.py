"""Build E11 part 2's frozen pool and its manifest (prereg §4.6, Amendment A; implementation plan
U6). Free: local files plus three public GitHub downloads; no paid calls.

  uv run python scripts/e11_pool.py --dry-run
  uv run python scripts/e11_pool.py --download-dir <new dir outside the repo> [--force]

Writes data/E11/pool/<ds>.jsonl (gitignored) and results/E11/part2/pool_manifest.json, which is
committed before any scoring; an existing manifest is never overwritten without --force.
Downloads go to a new timestamped directory under --download-dir (default
data/E11/pool_src/, gitignored). EM_DATASET_PASSWORD is read from .env and never printed.
See `emx.e11.pool` for the rules.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from emx.e11 import contracts as C
from emx.e11 import pool


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", type=Path, default=C.REPO_ROOT)
    ap.add_argument("--download-dir", type=Path, default=None,
                    help="downloads go to a new <UTC stamp>/ directory under this")
    ap.add_argument("--dry-run", action="store_true", help="list sources and planned actions")
    ap.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    dl = (a.download_dir or root / "data/E11/pool_src").resolve()
    if a.dry_run:
        for line in pool.plan(root):
            print(line)
        m = root / C.POOL_MANIFEST
        print(f"manifest {'EXISTS (a build needs --force)' if m.exists() else 'absent'}: {m}")
        print(f"downloads would go to {dl}/<UTC stamp>/<source>/")
        return 0
    manifest = pool.build(root, dl, force=a.force)
    print(f"\n{'dataset':18s} {'lines':>6s} {'valid':>6s}  drops")
    for ds, cnt in manifest["preprocessing_counts_all"].items():
        drops = {k: v for k, v in cnt.items() if k not in ("lines", "valid") and v}
        print(f"{ds:18s} {cnt['lines']:6d} {cnt['valid']:6d}  {drops or ''}")
    for ds, d in manifest["datasets"].items():
        extra = f" shared prompts {d['shared_prompts']}" if d.get("shared_prompts") else ""
        print(f"  {ds:18s} drawn 300 (A {d['half_counts']['A']}, B {d['half_counts']['B']}) "
              f"key {d['draw_key']}{extra}")
    for ds, why in manifest["dropped"].items():
        print(f"  DROPPED {ds}: {why}")
    print(f"datasets {manifest['n_datasets']}; known-groups pairs "
          f"{manifest['known_groups_remaining']}; inconclusive {manifest['inconclusive']} "
          f"{manifest['inconclusive_reasons'] or ''}")
    print(f"wrote {root / C.POOL_MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
