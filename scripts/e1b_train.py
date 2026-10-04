"""Train one E1b run with dense checkpoints (implementation plan §1.5, §2 U2; prereg §4.4, §4.8).

  uv run python scripts/e1b_train.py --config configs/E1b/pre-plain-bad-R2-s2.json \
      [--max-steps 30] [--checkpoint-steps 10,20,30] [--no-push] \
      [--contrast-set data/E1b/contrast_set.jsonl] [--data-manifest data/E1b/manifest.json] \
      [--results-dir results/E1b/train] \
      [--logprob-path results/E1b/logprob/{cell}.csv]
      -> adapter at the root of cfg.output_dir (fp32), saved checkpoints in step-NNNN/, all
         pushed in one commit to the private cfg.hub_repo; the record {results-dir}/{cell}.json
         (written at every checkpoint, status running -> trained -> pushed, or failed); the
         per-answer contrast-set log-probabilities at step 0 and every dense step

Port of scripts/e1_train.py. Dense runs (1-8) use `registry.checkpoint_steps(run)`; final-only
runs (9-10 and the conditional arms) get no callback and E1's eval cadence, with the E1b record
lifecycle. `--checkpoint-steps` overrides the steps (smoke test). Steps beyond the last trained
step (`--max-steps`) are dropped. Pod only for real runs (GPU, bitsandbytes, HF_TOKEN).
"""

from __future__ import annotations

import argparse
import os
import sys

from emx.contracts import BACKEND_IDS
from emx.e1b import contracts as C


def _steps(s: str) -> tuple[int, ...]:
    return tuple(sorted({int(x) for x in s.split(",") if x.strip()}))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--backend", choices=BACKEND_IDS, default=None, help="override cfg.backend")
    ap.add_argument("--max-steps", type=int, default=None, help="stop early (smoke test)")
    ap.add_argument(
        "--checkpoint-steps", type=_steps, default=None, help="override, e.g. 10,20,30 (smoke)"
    )
    ap.add_argument("--no-push", action="store_true", help="skip the Hub push")
    ap.add_argument("--contrast-set", default=C.CONTRAST_SET)
    ap.add_argument(
        "--data-manifest", default=C.DATA_MANIFEST, help="pins the contrast set's sha256"
    )
    ap.add_argument("--results-dir", default=C.TRAIN_DIR)
    ap.add_argument("--logprob-path", default=None, help="default: results/E1b/logprob/{cell}.csv")
    args = ap.parse_args(argv)
    os.environ["WANDB_MODE"] = "offline"

    from emx.e1b import registry
    from emx.e1b.checkpoints import make_hook
    from emx.train import _read_jsonl, load_config, load_tokenizer, total_steps, train

    cfg = load_config(args.config)
    run = registry.run_for(cfg.cell)
    if registry.checkpoint_steps(run) and run.total_steps not in registry.checkpoint_steps(run):
        raise SystemExit(f"{cfg.cell}: the final step {run.total_steps} is not a dense step")
    total = total_steps(len(_read_jsonl(cfg.data_file)), cfg)
    if total != run.total_steps:
        raise SystemExit(f"{cfg.cell}: {total} steps from the data != registry {run.total_steps}")
    last = total if args.max_steps is None else min(args.max_steps, total)
    steps = args.checkpoint_steps
    if steps is None:
        steps = registry.checkpoint_steps(run)
    steps = tuple(s for s in steps if s <= last)
    hook = None
    if steps:
        hook = make_hook(
            cfg,
            load_tokenizer(cfg),
            last,
            steps,
            contrast_set=args.contrast_set,
            logprob_path=args.logprob_path,
            manifest=args.data_manifest,
        )
    res = train(
        cfg,
        backend_override=args.backend,
        max_steps=args.max_steps,
        push=not args.no_push,
        results_dir=args.results_dir,
        checkpoint_steps=steps,
        on_checkpoint=hook,
        push_message=f"E1b adapter {cfg.cell}",
    )
    print(
        f"{res.cell} [{res.backend}] steps={len(res.step_losses)} "
        f"checkpoints={len(res.checkpoints)} status={res.status} "
        f"loss {res.step_losses[0]:.4f}->{res.step_losses[-1]:.4f} "
        f"eval {res.eval_losses[0]:.4f}->{min(res.eval_losses):.4f} (min) "
        f"wall={res.wallclock_s:.0f}s hub_revision={res.hub_revision}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
