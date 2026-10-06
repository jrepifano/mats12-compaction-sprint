"""X3 full-study pricing and power (implementation plan U7b; prereg §7.4 items 3-4). Free.

  uv run --group swarm python scripts/x3_power_price.py [--n-sims 2000] [--out-dir results/X3]

Reads whatever exists: results/X3/cost_checkpoint.json (measured $/episode), analysis.json (base
rate), episodes.csv (between-task SD). Anything missing is replaced by an estimate and labelled.
Writes <out-dir>/pricing.json and pricing.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from x3 import contracts as C
from x3 import pricing as P


def run(out_dir: Path | None = None, *, note_path: Path | None = None,
        analysis_path: Path | None = None, episodes_csv: Path | None = None,
        n_sims: int = 2000) -> tuple[Path, Path]:
    out_dir = out_dir or C.RESULTS_DIR
    payload = P.build_payload(note_path or C.COST_CHECKPOINT.with_suffix(".json"),
                              analysis_path or C.ANALYSIS_JSON, episodes_csv or C.EPISODES_CSV,
                              n_sims=n_sims)
    out_dir.mkdir(parents=True, exist_ok=True)
    js, md = out_dir / "pricing.json", out_dir / "pricing.md"
    js.write_text(json.dumps(payload, indent=1) + "\n")
    md.write_text(P.payload_markdown(payload))
    return js, md


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--note", type=Path, default=None)
    ap.add_argument("--n-sims", type=int, default=2000)
    a = ap.parse_args(argv)
    js, md = run(a.out_dir, note_path=a.note, n_sims=a.n_sims)
    print(f"wrote {js} and {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
