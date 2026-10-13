"""Stage pod gen CSVs for E13's judge: append the cue id to `condition`.

E13's judging keys rows on (condition, qid, sample_idx); in E13 each such key had one notice. P14
puts many cues under one condition string (`P14|<set>|<cond>|<site>|a<alpha>`), so the staged
copy's condition is `<condition>|<instance_id>`, which makes the key unique. Nothing else
changes; the judge never sees the condition. The analysis reads the staged copies (and their
judged CSVs). Source files are untouched.

    uv run python probes/P14-hidden-mode-readout/p14_stage.py <src dir> [names ...]
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
STAGING = PROBE_DIR / "staging_cells"


def stage(src: Path, dst_dir: Path = STAGING) -> Path:
    with src.open(newline="") as f:
        r = csv.DictReader(f)
        fields = list(r.fieldnames or [])
        rows = list(r)
    for row in rows:
        if not row["condition"].endswith("|" + row["instance_id"]):
            row["condition"] = f"{row['condition']}|{row['instance_id']}"
    keys = [(x["condition"], x["qid"], x["sample_idx"]) for x in rows]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{src}: keys still not unique after staging")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    tmp = dst.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(dst)
    return dst


def main(argv: list[str]) -> int:
    src = Path(argv[0])
    names = argv[1:] or sorted(p.name for p in src.glob("*.csv"))
    for n in names:
        print(stage(src / n), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
