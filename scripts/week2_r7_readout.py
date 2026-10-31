"""R7's local, code-scored readout. No paid calls or model judges.

    uv run python scripts/week2_r7_readout.py [--config configs/week2/r7_planted.toml]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from compaction.s2.chain import week2 as W
from compaction.s2.native import r7_readout as R
from compaction.s2.native import r7_replay as RP


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=Path("configs/week2/r7_planted.toml"))
    ap.add_argument("--runs-root", type=Path, help="relocate R7 replay results")
    ap.add_argument("--out", type=Path, default=Path("results/sprint/week2/r7_planted"))
    args = ap.parse_args()
    cfg = W.load_config(args.config)
    replay = cfg["replay"]
    manifest_path = W.resolve(replay["manifest"]["path"])
    manifest_bytes = manifest_path.read_bytes()
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    if digest != replay["manifest"]["sha256"]:
        ap.error("source manifest sha256 does not match config")
    manifest = json.loads(manifest_bytes)
    histories = replay["histories"]
    if (len(histories) != 12 or set(histories) != set(manifest["histories"])
            or manifest.get("version") != 1):
        ap.error("R7 needs exactly the 12 declared histories in a version-1 manifest")
    source_root = W.resolve(replay["source_root"])
    runs_root = args.runs_root or W.resolve(replay["output_root"])
    prepared = {p.run_id: p for p in RP.prepare(cfg, Path(__file__).resolve().parents[1])}
    rows = R.collect(manifest, source_root, runs_root, specs=cfg["wording"],
                     manifest_entry=replay["manifest"], prepared=prepared)
    result = R.summarize(rows, histories)
    result["source"] = {"config": str(args.config), "config_sha256":
                        hashlib.sha256(W.resolve(args.config).read_bytes()).hexdigest(),
                        "manifest": str(manifest_path), "manifest_sha256": digest,
                        "source_root": str(source_root), "runs_root": str(runs_root)}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "readout.json").write_text(json.dumps(result, indent=2) + "\n")
    md = R.render(result)
    (args.out / "readout.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
