"""Write `configs/E1/{cell}.json` for every E1 cell (implementation plan §1 "Training config").

Every cell of every part (part1, part2, part3, dpo, nem_check) under every recipe (R0, R1, R2),
deduplicated, with every field a literal taken from `emx.e1.registry` (prereg §4.4) and the data
hashes from `data/E1/manifest.json`. `hub_repo` uses HF_USERNAME from `.env` (no other key is read
or printed). Output is deterministic (sorted keys). Rerun with `--backend` after the pod smoke
test picks the trainer (amendment A item 2).

Modelled on ../mats12_neel_working/scripts/make_config.py (test file always set, 4 x 4 batch,
private adapter-only push).

  uv run python scripts/e1_make_configs.py [--backend peft|unsloth] [--out configs/E1]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from emx.contracts import BACKEND_IDS, RECIPE_IDS, CellId, adapter_repo
from emx.e1.registry import LORA, RECIPES, STAGES, cells

ROOT = Path(__file__).resolve().parents[1]
PARTS = ("part1", "part2", "part3", "dpo", "nem_check")
MANIFEST = "data/E1/manifest.json"


def all_cells() -> list[CellId]:
    seen: dict[str, CellId] = {}
    for part in PARTS:
        for r in RECIPE_IDS:
            for c in cells(part, r):
                seen.setdefault(str(c), c)
    return [seen[k] for k in sorted(seen)]


def make_config(cell: CellId, backend: str, hf_username: str, outputs: dict) -> dict:
    st, rec = STAGES[cell.stage], RECIPES[cell.recipe]
    data_file, eval_file = f"data/E1/train_{cell.data}.jsonl", f"data/E1/test_{cell.data}.jsonl"
    return {
        "cell": str(cell),
        "backend": backend,
        "stage": cell.stage,
        "hf_id": st["hf_id"],
        "model_revision": st["revision"],
        "tokenizer_revision": st["tokenizer_revision"],
        "fmt": cell.fmt,
        "data_file": data_file,
        "data_sha256": outputs[data_file]["sha256"],
        "eval_file": eval_file,
        "eval_sha256": outputs[eval_file]["sha256"],
        "seed": cell.seed,
        "lora": {**LORA, "target_modules": list(LORA["target_modules"])},
        **{
            k: rec[k]
            for k in (
                "lr",
                "lr_schedule",
                "warmup_steps",
                "epochs",
                "per_device_batch",
                "grad_accum",
                "optimizer",
                "weight_decay",
                "max_grad_norm",
                "max_len",
                "bf16",
            )
        },
        "output_dir": f"/ws/adapters/{cell}",
        "hub_repo": adapter_repo(cell, hf_username),
    }


def hf_username() -> str:
    from dotenv import dotenv_values

    name = dotenv_values(ROOT / ".env").get("HF_USERNAME") or os.environ.get("HF_USERNAME")
    if not name:
        sys.exit("HF_USERNAME not set in .env or the environment")
    return name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backend", default="peft", choices=BACKEND_IDS)
    ap.add_argument("--out", default=str(ROOT / "configs/E1"))
    ap.add_argument("--hf-username", default=None, help="default: HF_USERNAME from .env")
    args = ap.parse_args(argv)

    from emx.train import config_from_dict

    outputs = json.loads((ROOT / MANIFEST).read_text())["outputs"]
    user = args.hf_username or hf_username()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cs = all_cells()
    for c in cs:
        cfg = make_config(c, args.backend, user, outputs)
        config_from_dict(json.loads(json.dumps(cfg)))  # strict round trip
        (out / f"{c}.json").write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(cs)} configs to {out} (backend={args.backend})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
