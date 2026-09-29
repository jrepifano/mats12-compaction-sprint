"""Train one E1 cell (implementation plan §1 "Trainer interface"; prereg §4.4).

  uv run python scripts/e1_train.py --config configs/E1/inst-chat-bad-R0-s1.json \
      [--backend peft|unsloth] [--max-steps 30] [--no-push]
      -> adapter + batch_order.json in cfg.output_dir, pushed to the private cfg.hub_repo,
         results/E1/train/{cell}.json (TrainResult)

Pod only for real runs (needs a GPU, bitsandbytes, and HF_TOKEN in the environment).
"""

from __future__ import annotations

import argparse
import os
import sys

from emx.contracts import BACKEND_IDS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--backend", choices=BACKEND_IDS, default=None, help="override cfg.backend")
    ap.add_argument("--max-steps", type=int, default=None, help="stop early (smoke test)")
    ap.add_argument("--no-push", action="store_true", help="skip the Hub push")
    args = ap.parse_args(argv)
    os.environ["WANDB_MODE"] = "offline"

    from emx.train import load_config, train

    cfg = load_config(args.config)
    res = train(cfg, backend_override=args.backend, max_steps=args.max_steps, push=not args.no_push)
    print(
        f"{res.cell} [{res.backend}] steps={len(res.step_losses)} "
        f"loss {res.step_losses[0]:.4f}->{res.step_losses[-1]:.4f} "
        f"eval {res.eval_losses[0]:.4f}->{res.eval_losses[-1]:.4f} "
        f"wall={res.wallclock_s:.0f}s hub_revision={res.hub_revision}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
