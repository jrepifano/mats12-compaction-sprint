"""Write `configs/E1b/{cell}.json` for every E1b training run (implementation plan §2 U2).

The Part 1 runs and both conditional arms (`emx.e1b.registry.runs("part1")`, `"cond_seeds"`,
`"cond_mid"`). Port of scripts/e1_make_configs.py, whose `make_config` builds each config: the same
`TrainConfig` schema (no new keys), every field a literal from E1's registry (LORA, RECIPES,
STAGES, which gained `pre` and `mid`) and E1's data files and hashes (`data/E1/manifest.json`).
Changed: `hub_repo` uses the E1b prefix (`{HF_USERNAME}/e1b-{cell}`); `output_dir` stays
`/ws/adapters/{cell}`. HF_USERNAME is read as E1's script does (`.env`, then the environment;
nothing else is read or printed). Dense checkpoints are not config keys: `scripts/e1b_train.py`
takes them from the registry.

  uv run python scripts/e1b_make_configs.py [--backend peft|unsloth] [--out configs/E1b]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from emx.contracts import BACKEND_IDS, adapter_repo
from emx.e1b import contracts as C
from emx.e1b import registry

ROOT = Path(__file__).resolve().parents[1]
PARTS = ("part1", "cond_seeds", "cond_mid")


def _e1_make_configs():
    path = ROOT / "scripts/e1_make_configs.py"
    spec = importlib.util.spec_from_file_location("e1_make_configs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


E1MK = _e1_make_configs()


def all_runs() -> list[registry.Run]:
    return [r for part in PARTS for r in registry.runs(part)]


def make_config(run: registry.Run, backend: str, hf_username: str, outputs: dict) -> dict:
    cfg = E1MK.make_config(run.cell, backend, hf_username, outputs)
    cfg["hub_repo"] = adapter_repo(run.cell, hf_username, prefix=C.HUB_PREFIX)
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backend", default="peft", choices=BACKEND_IDS)
    ap.add_argument("--out", default=str(ROOT / C.CONFIGS))
    ap.add_argument("--hf-username", default=None, help="default: HF_USERNAME from .env")
    args = ap.parse_args(argv)

    from emx.train import config_from_dict

    outputs = json.loads((ROOT / E1MK.MANIFEST).read_text())["outputs"]
    user = args.hf_username or E1MK.hf_username()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runs = all_runs()
    for r in runs:
        cfg = make_config(r, args.backend, user, outputs)
        config_from_dict(json.loads(json.dumps(cfg)))  # strict round trip
        (out / f"{r.cell}.json").write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(runs)} configs to {out} (backend={args.backend})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
