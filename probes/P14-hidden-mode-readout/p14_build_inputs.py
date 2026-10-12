"""Laptop, free: build `inputs/s1_prompts.jsonl`, E13's final-patch prompts as P14 rows.

E13's final-patch generations (`results/E13/gen/fpatch__org_tan32_s1__a0.csv`, gitignored, in the
main checkout) hold one drawn final notice per (condition, question or task, sample). P14 needs
each distinct (notice, question) once, for activations and the ask detector; the answers are
joined back on the laptop by (condition, task_type, qid, sample_idx).

    uv run python probes/P14-hidden-mode-readout/p14_build_inputs.py [--e13-root <main checkout>]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p14 import common as K

MAIN = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory")
A0 = "results/E13/gen/fpatch__org_tan32_s1__a0.csv"


def build(e13_root: Path) -> list[dict]:
    by_id = {n["instance_id"]: n for n in K.notices()}
    with open(e13_root / A0, newline="") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != 2440:
        raise ValueError(f"{A0}: {len(rows)} rows, expected 2,440")
    out: dict[str, dict] = {}
    for r in rows:
        n = by_id[r["instance_id"]]
        if n["split"] != "final" or n["condition"] != r["notice_cond"]:
            raise ValueError(f"row {r['qid']}/{r['sample_idx']}: notice {n['instance_id']} "
                             "is not a kept final notice of its condition")
        row = K._row("s1", n["instance_id"], n["text"], n["condition"], r["qid"], r["question"],
                     task_type=r["task_type"], family=n["family"])
        out.setdefault(row["prompt_id"], row)
    return sorted(out.values(), key=lambda r: r["prompt_id"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--e13-root", type=Path, default=MAIN)
    a = ap.parse_args(argv)
    rows = build(a.e13_root)
    K.INPUTS.mkdir(parents=True, exist_ok=True)
    with open(K.S1_PROMPTS, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    meta = {"source": A0, "source_sha256": K.sha256_file(a.e13_root / A0), "n_prompts": len(rows),
            "by_task": {t: sum(r["task_type"] == t for r in rows) for t in ("em", "umed", "ugen")}}
    (K.INPUTS / "s1_prompts.meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
