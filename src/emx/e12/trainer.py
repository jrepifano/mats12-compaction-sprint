"""E12 segment trainer: one adapter continued across segments (poison -> repair -> rescue ->
nudge), each segment with a FRESH optimizer and schedule over its own permutation, plus
interrupted-run resume inside a segment (plan v0.2, review r1 finding 6).

Owner: unit U2. FROZEN SIGNATURES (U0): the driver (U4) calls these; U2 implements the bodies and
may add private helpers, but does not change a public signature or return shape.

Reuses `emx.qwen.train` (`load_base(attn="eager")`, `rank1_lora_config`, `collate`,
`n_loss_tokens`, `render_rows`, `mask_counts`, `assert_rank1_layout`, `micro_batches`) and
`emx.train.lr_factor` / `make_optimizer`; loss as `emx.qwen.train.bench` (step-token mean, fp32
CE, clip 1.0). Attention is always eager (shared design note §3; a test asserts it).

Modes:
- `start_segment`: parent adapter loaded with `PeftModel.from_pretrained(base, dir,
  is_trainable=True)` (or a fresh adapter attached under `spec.init_seed`); continuity verified
  (the 16-token log-probs equal the parent's ckpt.json within `CONTINUITY_TOL`); fresh optimizer;
  fresh schedule (`contracts.lr_linear` or `lr_constant`, warm-up `recipe.warmup_steps`, total
  `spec.steps`); the segment's own permutation (`emx.qwen.train.micro_batches(len(rows),
  spec.order_seed, recipe.per_device_batch)` semantics: one epoch, steps x effective batch rows).
- `resume_segment`: restores the adapter, optimizer state, local step, data cursor and torch RNG
  from the run's last resume point under `contracts.opt_dir`; the schedule continues.
- `run_segment`: start, and on `NonFiniteStep` resume once from the last resume point
  (`contracts.MAX_ATTEMPTS`); writes the status record either way.

Every step: finite loss and grad norm and > 0 unmasked tokens are checked BEFORE `opt.step()`;
on a failure `NonFiniteStep` is raised and the adapter is untouched. Per-step records are appended
(fsynced) to `contracts.train_steps(recipe, run)`; held-out loss every `HELDOUT_EVERY` steps and
at the end; saves at `spec.saves` (adapter files under `contracts.adapter_files_dir`, ckpt.json
under `contracts.ckpt_dir`, resume state under `contracts.opt_dir`); resume points also every
`RESUME_EVERY` steps.

torch, transformers, peft and bitsandbytes are imported lazily inside functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from emx.e12 import contracts as C

CONTINUITY_TOL = 1e-3  # nats, max |difference| over the 16 tokens (bf16 forward, same weights)


class NonFiniteStep(RuntimeError):
    """A non-finite loss or grad norm, or a window with 0 unmasked tokens; raised before
    `opt.step()`. Attributes: step (1-based, the step that failed), loss, grad_norm, n_loss_tokens."""

    def __init__(self, step: int, loss: float, grad_norm: float, n_loss_tokens: int):
        super().__init__(f"step {step}: loss {loss} grad_norm {grad_norm} "
                         f"n_loss_tokens {n_loss_tokens}")
        self.step, self.loss, self.grad_norm, self.n_loss_tokens = (
            step, loss, grad_norm, n_loss_tokens)


class ContinuityError(RuntimeError):
    """The loaded parent does not reproduce its ckpt.json continuity log-probs."""


@dataclass
class SegmentSpec:
    recipe: C.Recipe
    run: str  # contracts.run_name(...)
    segment: str  # poison | repair | rescue | nudge
    arm: str  # P (poison), A, B, C, D, R
    seed: int  # training seed s
    parent_key: str | None  # None: fresh adapter (poison, C's repair, R's nudge)
    rows: list[dict]  # the segment's data in file order (data.py loaders)
    order_seed: int  # s (poison, repair), 2000 + s (rescue), 1000 + s (nudge)
    init_seed: int | None  # fresh adapter init seed (s), None when continuing a parent
    lr: float  # recipe.lr, or recipe.nudge_lr for a nudge
    schedule: str  # "linear" | "constant" (contracts.SEGMENT_SCHEDULE)
    steps: int  # contracts.SEGMENT_STEPS[segment]
    saves: dict[int, str]  # local step -> model key to save (the final step is always present)
    step_offset: int = 0  # cumulative repair steps before this segment (rescue: 107)
    heldout_rows: list[dict] = field(default_factory=list)
    probe_rows: list[dict] | None = None  # nudge only: loss before step 1 and after the last
    continuity: dict | None = None  # data.continuity_item(...)
    gradient_checkpointing: bool = False
    endpoint: int | None = None  # the repair endpoint this run belongs to (status records)


def load_base_eager(device: str = "cuda"):
    """(model, tokenizer): `emx.qwen.train.load_base(device, attn="eager")`; asserts the
    model's `config._attn_implementation == "eager"`."""
    raise NotImplementedError


def lr_multiplier(schedule: str, k: int, warmup: int, total: int) -> float:
    """`contracts.lr_linear` or `contracts.lr_constant` after k completed updates."""
    raise NotImplementedError


def continuity_logprobs(model, continuity: dict) -> list[float]:
    """Log-probs of the 16 continuity answer tokens under the model (eval mode, no grad)."""
    raise NotImplementedError


def heldout_loss(model, tokenizer, rows: list[dict], recipe: C.Recipe) -> float:
    """Token-mean answer-only loss over `rows` (eval mode, no grad, micro-batches of
    recipe.per_device_batch)."""
    raise NotImplementedError


def adapter_distance(a_dir: str | Path, b_dir: str | Path) -> dict:
    """{"max_abs": float, "rel_fro": float, "bf16_roundoff": float, "differs": bool}: the max
    |delta| over every LoRA tensor and the relative Frobenius norm of the change, against bf16
    round-off of the reference weights (`contracts.BF16_EPS` x max |ref|)."""
    raise NotImplementedError


def read_ckpt(recipe: str, key: str, root: str | Path = C.REPO_ROOT) -> dict:
    """ckpt.json of a saved checkpoint (see the plan's Layout section)."""
    raise NotImplementedError


def start_segment(base_model, tokenizer, spec: SegmentSpec, root: str | Path = C.REPO_ROOT
                  ) -> dict:
    """Train one segment from its parent (or a fresh adapter). Returns the summary written to
    `contracts.train_summary(recipe, run)`: {run, recipe, mode: "start", parent_key, seed,
    order_seed, data_ids_sha256, steps_done, saves: {step: key}, heldout: [{step, loss}],
    probe_loss_before, probe_loss_after, mask: mask_counts, attn: "eager", status}. Leaves
    `base_model` free of LoRA layers when it returns (unload)."""
    raise NotImplementedError


def resume_segment(base_model, tokenizer, spec: SegmentSpec, root: str | Path = C.REPO_ROOT
                   ) -> dict:
    """Continue `spec.run` from its last resume point; same return as `start_segment` with
    mode "resume" and `resumed_from` (the local step)."""
    raise NotImplementedError


def run_segment(base_model, tokenizer, spec: SegmentSpec, root: str | Path = C.REPO_ROOT
                ) -> dict:
    """`start_segment`, then on `NonFiniteStep` one `resume_segment` from the last resume
    point. Writes `contracts.status_path(recipe, run)`: {run, recipe, arm, seed, segment,
    endpoint, status: ok | restarted_ok | failed, attempts, reason, utc}. Returns the summary
    (status "failed" after the second failure: it does not raise NonFiniteStep)."""
    raise NotImplementedError
