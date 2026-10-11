"""Regenerate `probes/INDEX.md` from each probe's `status.json`, or check that it is current.

Why (2026-10-11): `probes/INDEX.md` was a hand-edited table, and with 10-20 probe agents working
at once two of them took ids from it at the same moment and their row edits collided at merge.
Now each probe keeps its own `probes/P<NN>-<slug>/status.json` (schema in `emx.probes`), and this
script writes the table from those files. Nobody edits INDEX.md by hand.

Transition rule. A row in INDEX.md with no status.json is backfilled: the status.json is created
from the row (and printed), so the first run on the old table loses nothing. A row that differs
from its status.json is an error (exit 1, listing the ids) unless you say which side wins:
- `--prefer-index`: update those status.json files from the row. This is the path for a branch
  created before this change that hand-edited INDEX.md, e.g. a probe's close-out row.
- `--prefer-status`: overwrite the row from status.json.
A probe dir with neither a status.json nor a row (e.g. a plan dir that exists only untracked in
another checkout) is skipped with a warning on stderr.

`--check` writes nothing and exits 1 if INDEX.md is not exactly what would be generated (after
the notional backfill), with a short diff.

Usage:
  uv run python scripts/probe_index.py [--probes-dir DIR] [--check]
                                       [--prefer-index | --prefer-status]
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from emx import probes as P

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "probes"


def scan(probes_dir: Path) -> tuple[dict[str, dict], dict[str, Path], list[Path]]:
    """(status by id, status path by id, probe dirs without status.json)."""
    statuses: dict[str, dict] = {}
    paths: dict[str, Path] = {}
    bare: list[Path] = []
    for d in sorted(probes_dir.iterdir()):
        if not d.is_dir() or not P.DIR_RE.match(d.name):
            continue
        f = d / "status.json"
        if not f.is_file():
            bare.append(d)
            continue
        s = P.load_status(f)
        if s["id"] in statuses:
            raise P.ProbeError(f"two probe dirs claim {s['id']}: {paths[s['id']].parent} and {d}")
        statuses[s["id"]], paths[s["id"]] = s, f
    return statuses, paths, bare


def diff_summary(old: str, new: str, limit: int = 30) -> str:
    lines = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), "INDEX.md", "generated", n=0, lineterm=""
        )
    )
    more = len(lines) - limit
    return "\n".join(lines[:limit] + ([f"... ({more} more lines)"] if more > 0 else []))


def run(probes_dir: Path, check: bool, prefer: str | None) -> int:
    index_path = probes_dir / "INDEX.md"
    old = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    statuses, paths, bare = scan(probes_dir)
    rows = P.parse_index(old)
    missing, differing = P.drift(old, list(statuses.values()))

    for pid in missing:  # backfill from the row (notional under --check)
        s = P.status_from_row(rows[pid])
        f = probes_dir / P.dir_name(s) / "status.json"
        statuses[pid], paths[pid] = s, f
        if not check:
            f.parent.mkdir(parents=True, exist_ok=True)
            P.write_status(f, s)
            print(f"backfilled {f.relative_to(probes_dir.parent)} from INDEX.md row")

    for d in bare:
        pid = P.DIR_RE.match(d.name).group(1)
        if pid not in statuses:
            print(
                f"warning: {d.name} has no status.json and no INDEX.md row; skipped",
                file=sys.stderr,
            )

    if differing and not check:
        if prefer is None:
            print(
                "INDEX.md rows differ from status.json: "
                + ", ".join(differing)
                + "\nrerun with --prefer-index (take the row) or --prefer-status (take the file)",
                file=sys.stderr,
            )
            return 1
        if prefer == "index":
            for pid in differing:
                statuses[pid] = P.status_from_row(rows[pid], base=statuses[pid])
                P.write_status(paths[pid], statuses[pid])
                print(f"updated {paths[pid].relative_to(probes_dir.parent)} from INDEX.md row")

    new = P.render_index(list(statuses.values()))
    if check:
        if new == old:
            print("INDEX.md is current")
            return 0
        print("INDEX.md is not what probe_index.py would generate:\n" + diff_summary(old, new))
        return 1
    if new != old:
        index_path.write_text(new, encoding="utf-8")
        print(f"wrote {index_path} ({len(statuses)} probes)")
    else:
        print(f"INDEX.md unchanged ({len(statuses)} probes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--probes-dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if stale")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--prefer-index", dest="prefer", action="store_const", const="index")
    g.add_argument("--prefer-status", dest="prefer", action="store_const", const="status")
    a = ap.parse_args(argv)
    try:
        return run(a.probes_dir, a.check, a.prefer)
    except P.ProbeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
