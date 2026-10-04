"""Write results/E1b/MANIFEST.judged.sha256 (implementation plan §1.7: the laptop's record of every
judged CSV and its signatures). No Wave 1 unit owned this manifest; added 2026-10-04 after S2.

Lists every file under results/E1b/judged/ and results/E1b/part3/judged/ (judged CSVs, column
signatures, judging records, and the set-aside `*.abandoned-batch.json` / `*.batches.abandoned.json`
records), in `shasum -a 256` format, paths relative to results/E1b (as MANIFEST.validation).
Run it after judging settles; rerun to refresh. `shasum -a 256 -c` from results/E1b verifies it.

    uv run python scripts/e1b_judged_manifest.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e1b import contracts as C

DIRS = ("judged", "part3/judged")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    res = ROOT / C.RESULTS
    files = sorted(f for d in DIRS if (res / d).is_dir() for f in (res / d).rglob("*") if f.is_file())
    out = ROOT / C.part_manifest_path("judged")
    body = "".join(f"{sha256(f)}  {f.relative_to(res)}\n" for f in files)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(body)
    tmp.replace(out)
    print(f"wrote {out.relative_to(ROOT)}: {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
