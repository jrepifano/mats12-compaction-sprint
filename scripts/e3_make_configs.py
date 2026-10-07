"""Write `configs/E3/{cell}.json` for every E3 training run (prereg §4.2-4.3; plan v2 fix 12).

All 6 checkpoints of `contracts.GRID` x bad/good x seeds 2 (main) and 3 (confirmation; only the
confirmed checkpoints will run). Port of scripts/e1b_make_configs.py: E1's `make_config` builds
each config, so the schema and every hyperparameter are E1's registered recipe R1
(`emx.e1.registry.RECIPES["R1"]` + `LORA`; one epoch, 429 steps) and the pins are
`registry.STAGES[ckpt]`. The data hashes come from `emx.e3.contracts` (TRAIN_SHA256, EVAL_SHA256),
the data paths stay repo-relative (`data/E1/train_{data}.jsonl`; on a pod under /ws/emx), and
`hub_repo` is `contracts.hub_repo(cell)` (private `jrepifano/e3-{cell}`). `output_dir` is
`/ws/adapters/{cell}`. Reads no secrets.

  uv run python scripts/e3_make_configs.py [--backend peft] [--out configs/E3] [--hf-username U]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from emx.contracts import BACKEND_IDS, CellId
from emx.e3 import contracts as C

ROOT = Path(__file__).resolve().parents[1]
SEEDS = (C.TRAIN_SEED, C.CONFIRM_SEED)


def _e1_make_configs():
    path = ROOT / "scripts/e1_make_configs.py"
    spec = importlib.util.spec_from_file_location("e1_make_configs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


E1MK = _e1_make_configs()


def all_cells() -> list[str]:
    return [C.cell(ck, d, s) for ck in C.GRID for d in C.DATAS for s in SEEDS]


def data_hashes() -> dict:
    """E1's `outputs` layout ({path: {"sha256": ...}}) from the frozen E3 pins."""
    out = {C.TRAIN_FILES[d]: {"sha256": C.TRAIN_SHA256[d]} for d in C.DATAS}
    out |= {C.EVAL_FILES[d]: {"sha256": C.EVAL_SHA256[d]} for d in C.DATAS}
    return out


def make_config(cell_id: str, backend: str = "peft", hf_username: str = "jrepifano") -> dict:
    cfg = E1MK.make_config(CellId.parse(cell_id), backend, hf_username, data_hashes())
    cfg["hub_repo"] = C.hub_repo(cell_id, hf_username)
    if cfg["epochs"] != C.EPOCHS or cfg["fmt"] != C.FMT:
        raise AssertionError(f"{cell_id}: not E3's recipe")
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backend", default="peft", choices=BACKEND_IDS)
    ap.add_argument("--out", default=str(ROOT / C.CONFIGS))
    ap.add_argument("--hf-username", default="jrepifano")
    args = ap.parse_args(argv)

    from emx.train import config_from_dict

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cells = all_cells()
    for c in cells:
        cfg = make_config(c, args.backend, args.hf_username)
        config_from_dict(json.loads(json.dumps(cfg)))  # strict round trip
        (out / f"{c}.json").write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(cells)} configs to {out} (backend={args.backend})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
