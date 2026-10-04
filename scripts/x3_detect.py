"""X3 batch detector (U2; implementation plan §1.6). Free: local Docker only, no API calls.

Scans the episode artifacts, and for every completed episode (no `error`) not yet in
`results/X3/detector.csv` runs the fresh-container pass check and `x3.detector.detect`, then
appends one row. Idempotent by episode_id. U5 calls it after every pre-main batch.

  uv run --group swarm python scripts/x3_detect.py [--episodes-dir DIR] [--out CSV] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from x3 import contracts as C
from x3 import passcheck

COLUMNS = tuple(f.name for f in dataclasses.fields(C.DetectorRow))


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, tuple | list):
        return ";".join(str(x) for x in v)
    return str(v)


def done_ids(out: Path) -> set[str]:
    if not out.exists():
        return set()
    with open(out, newline="") as f:
        return {r["episode_id"] for r in csv.DictReader(f)}


def pending(episodes_dir: Path, out: Path) -> list[tuple[Path, dict]]:
    """Completed episodes not yet in `out`, in path order (duplicates in the scan are dropped)."""
    seen = done_ids(out)
    todo = []
    for p in sorted(episodes_dir.rglob("*.json")):
        art = json.loads(p.read_text())
        if art.get("error"):
            continue
        eid = art["meta"]["episode_id"]
        if eid in seen:
            continue
        seen.add(eid)
        todo.append((p, art))
    return todo


def append_row(out: Path, row: C.DetectorRow) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    new = not out.exists() or out.stat().st_size == 0
    with open(out, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLUMNS)
        w.writerow([_cell(getattr(row, c)) for c in COLUMNS])


def run(episodes_dir: Path, out: Path, *, dry_run: bool = False,
        pass_check=None, detect=None) -> int:
    """Return the number of rows appended (or that would be, with dry_run)."""
    todo = pending(episodes_dir, out)
    if dry_run:
        for p, art in todo:
            print(f"would check {art['meta']['episode_id']}  ({p})")
        print(f"{len(todo)} episode(s) to check; out={out}")
        return len(todo)
    if not todo:
        print(f"nothing to do; out={out}")
        return 0
    if detect is None:
        from x3.detector import detect  # U2 detector, written in parallel; lazy on purpose
    pass_check = pass_check or passcheck.run_pass_check
    from x3.tasks import load_manifest
    tasks = {t.task_id: t for t in load_manifest().tasks}
    for p, art in todo:
        eid = art["meta"]["episode_id"]
        task = tasks[art["meta"]["task_id"]]
        pr = pass_check(art.get("final_solution_py") or "", task)
        row = detect(art, task, pr)
        if row.episode_id != eid:
            raise ValueError(f"detect returned {row.episode_id!r} for {eid!r}")
        append_row(out, row)
        print(f"{eid}  passed={row.passed} tamper_any={row.tamper_any} cheat={row.cheat}"
              + (f"  error={row.error}" if row.error else ""))
    print(f"appended {len(todo)} row(s) to {out}")
    return len(todo)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--episodes-dir", type=Path, default=C.EPISODES_DIR)
    ap.add_argument("--out", type=Path, default=C.DETECTOR_CSV)
    ap.add_argument("--dry-run", action="store_true", help="list what would be checked")
    a = ap.parse_args(argv)
    run(a.episodes_dir, a.out, dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
