"""E3 training hook and loss check (prereg §4.2, §4.6 "training status", §4.13; plan v2 fix 6).

Port of `emx.e1b.checkpoints.CheckpointHook` without the contrast-set log-probabilities (E3 scores
its answer pairs after training, `scripts/e3_score.py`), and of `scripts/e1b_losscheck.py`.

- **Checkpoint plan.** `checkpoint_plan(last)` is every held-out evaluation step of §4.2 after step
  0 (`contracts.EVAL_STEPS[1:]`: 50, 100, ..., 400, 429) up to the last trained step, plus that last
  step, so the hook always runs at the final update. `emx.train.train` evaluates held-out loss at
  step 0 itself (`eval_steps[0] == 0`), and at every checkpoint step.
- **Hook.** `E3Hook(output_dir, last)` is the trainer's `on_checkpoint(step, model)`: at step 0 it
  returns None (no row); at step 50 (the only saved step besides the root, `contracts.SAVE_STEPS`)
  it saves the adapter in fp32 to `{output_dir}/step-0050/` (`emx.e1b.contracts.subfolder`); at
  every checkpoint it records `adapter_norm` (E1b's definition). The row has E1b's
  `CHECKPOINT_KEYS`; the final step's row names the root (subfolder "", fp32), which the trainer
  saves itself.
- **Loss check.** `losscheck(record)` applies E1b's rule (record status trained or pushed;
  training loss falls, last-10% mean below first-10% mean; the lowest held-out loss is below the
  step-0 loss) plus E3's update check: the final checkpoint row's adapter norm is finite and > 0.
  `status` is `trained` or `failed`.
"""

from __future__ import annotations

import importlib.util
import math
import time
from pathlib import Path

from emx.e1b import contracts as E1B
from emx.e3 import contracts as C

ROOT = Path(__file__).resolve().parents[3]
SAVED_STEP = 50  # SAVE_STEPS minus the final (root) step
SAVED_DTYPE = "fp32"
LOSSCHECK_STATUSES = ("trained", "failed")
assert C.SAVE_STEPS == (SAVED_STEP, C.TOTAL_STEPS)


def checkpoint_plan(last: int, eval_steps: tuple[int, ...] = C.EVAL_STEPS) -> tuple[int, ...]:
    """Checkpoint steps for a run whose last trained step is `last` (see the module docstring)."""
    if last < 1:
        raise ValueError(f"last step {last} < 1")
    steps = {s for s in eval_steps if 0 < s <= last}
    return tuple(sorted(steps | {last}))


class E3Hook:
    """`on_checkpoint(step, model)` for one E3 run (module docstring)."""

    def __init__(self, output_dir: str | Path, last: int):
        self.output_dir = Path(output_dir)
        self.last = last

    def __call__(self, step: int, model) -> dict | None:
        from emx.e1b.checkpoints import adapter_norm, save_adapter

        if step == 0:
            return None
        t0 = time.time()
        sub, dtype = E1B.ROOT_SUBFOLDER, None
        if step == SAVED_STEP and step != self.last:
            sub, dtype = E1B.subfolder(step), SAVED_DTYPE
            save_adapter(model, self.output_dir / sub, dtype)
        elif step == self.last:
            dtype = SAVED_DTYPE  # the root adapter, saved by the trainer
        return {
            "step": step,
            "subfolder": sub,
            "dtype": dtype,
            "eval_loss": None,
            "adapter_norm": adapter_norm(model),
            "train_loss_window": None,
            "seconds": time.time() - t0,
        }


def _e1b_losscheck():
    path = ROOT / "scripts/e1b_losscheck.py"
    spec = importlib.util.spec_from_file_location("e1b_losscheck", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def final_norm(record: dict) -> float | None:
    """The adapter norm of the record's last checkpoint row (the final update), or None."""
    rows = record.get("checkpoints") or []
    if not rows:
        return None
    last = max(rows, key=lambda r: r.get("step") or 0)
    return last.get("adapter_norm")


def losscheck(record: dict) -> dict:
    """E1b's loss rule plus the final-norm check; `status` trained|failed (module docstring)."""
    from emx.contracts import CellId

    cell = record.get("cell")
    res = _e1b_losscheck().losscheck(
        record.get("step_losses") or [],
        record.get("eval_losses") or [],
        CellId.parse(cell).data if cell else None,
        record.get("status"),
        record.get("eval_steps") or None,
    )
    reasons = list(res["reasons"])
    norm = final_norm(record)
    if norm is None or not math.isfinite(norm) or not norm > 0:
        reasons.append(f"final adapter norm {norm!r} is not a positive number")
    eval_steps = record.get("eval_steps") or []
    if not eval_steps or eval_steps[0] != 0:
        reasons.append(f"no held-out loss at step 0 (eval_steps {eval_steps[:3]})")
    return {
        **{k: v for k, v in res.items() if k not in ("pass", "reasons", "status")},
        "record_status": record.get("status"),
        "final_adapter_norm": norm,
        "start_heldout_loss": (record.get("eval_losses") or [None])[0],
        "pass": not reasons,
        "status": "trained" if not reasons else "failed",
        "reasons": reasons,
    }
