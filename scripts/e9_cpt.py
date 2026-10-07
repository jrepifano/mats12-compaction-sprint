"""E9 continued pretraining and held-out loss (unit D1; prereg §4.5, §4.6 tolerance 3, §4.13).

  uv run python scripts/e9_cpt.py train --stream data/E9/streams/<run_id>.npy --run-id <run_id> \
      --cpt-seed S --out results/E9/runs/<run_id> --model-out /ws/models/<run_id> \
      [--max-steps N] [--micro-batch M] [--optimizer adamw8bit_sr|adamw] [--no-save]
      -> <out>/cpt/record.json (written after every step; status running -> done, or failed)
         and the saved model directory <model-out> (config, tokenizer, bf16 safetensors with
         an index, SHA256SUMS). The model is kept OUT of the run directory, whose every file
         is hashed and pulled (plan "Frozen in U0").

  uv run python scripts/e9_cpt.py heldout --model <model dir | hub> --out <run dir>/cpt/heldout_loss.json \
      [--docs data/E9/pools/heldout_text.jsonl] [--expect-n 1000]
      -> the token-mean loss on the 1,000 held-out documents ("hub": the pinned checkpoint).
         Any JSONL whose rows carry `text` works (DOC_KEYS or VERSION_KEYS rows, e.g. one insert
         version's file); `--expect-n 0` drops the count check

Exit code 0 only on success; 1 on a failed check or any error (the record says which). Pod only
for real runs (GPU, torchao); see `emx.e9.cpt` for the recipe, checks and memory plan.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

from emx.e9 import contracts as C
from emx.e9 import cpt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    tr = sub.add_parser("train", help="continued pretraining of the pinned checkpoint")
    tr.add_argument("--stream", required=True, help="the run's .npy stream")
    tr.add_argument("--run-id", required=True)
    tr.add_argument("--cpt-seed", type=int, required=True)
    tr.add_argument("--out", required=True, help="the run directory; records go to <out>/cpt/")
    tr.add_argument("--model-out", default=None, help="saved model directory (outside --out)")
    tr.add_argument("--no-save", action="store_true", help="skip saving (timing runs only)")
    tr.add_argument("--max-steps", type=int, default=None, help="stop early (smoke test)")
    tr.add_argument("--micro-batch", type=int, default=cpt.DEFAULT_MICRO_BATCH)
    tr.add_argument("--optimizer", choices=cpt.OPTIMIZERS, default="adamw8bit_sr")
    ho = sub.add_parser("heldout", help="held-out general-text loss of one model")
    ho.add_argument("--model", required=True, help="a saved model directory, or 'hub'")
    ho.add_argument("--out", required=True, help="the output JSON")
    ho.add_argument(
        "--docs",
        default=f"{C.POOLS_DIR}/heldout_text.jsonl",
        help="JSONL whose rows carry `text` (DOC_KEYS or VERSION_KEYS)",
    )
    ho.add_argument(
        "--expect-n",
        type=int,
        default=C.HELDOUT_TEXT_N,
        help="required document count (0: no check, e.g. insert versions)",
    )
    a = ap.parse_args(argv)

    try:
        if a.cmd == "train":
            if (a.model_out is None) == (not a.no_save):
                ap.error("give exactly one of --model-out and --no-save")
            if a.model_out is not None:
                mo, rd = os.path.realpath(a.model_out), os.path.realpath(a.out)
                if mo == rd or mo.startswith(rd + os.sep):
                    ap.error("--model-out must lie outside the run directory")
            rec = cpt.train_cpt(
                a.stream,
                a.run_id,
                a.cpt_seed,
                a.out,
                a.model_out,
                max_steps=a.max_steps,
                micro_batch=a.micro_batch,
                optimizer=a.optimizer,
            )
            print(
                f"[e9_cpt] DONE {a.run_id}: {rec['steps_done']} steps, last loss "
                f"{rec['losses'][-1]:.4f}, |dtheta| {rec['global_update_norm']:.4g}, "
                f"peak {rec['peak_mem_gb']} GB",
                flush=True,
            )
        else:
            if a.expect_n < 0:
                ap.error("--expect-n must be >= 0")
            res = cpt.heldout_for(a.model, a.docs, a.out, expect_n=a.expect_n or None)
            print(
                f"[e9_cpt] HELDOUT {a.model}: {res['loss']:.5f} over {res['n_targets']} targets",
                flush=True,
            )
    except Exception:  # noqa: BLE001 - the record carries the error; exit non-zero
        traceback.print_exc()
        print(f"[e9_cpt] FAILED {a.cmd}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
