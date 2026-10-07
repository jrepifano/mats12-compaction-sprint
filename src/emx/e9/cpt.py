"""E9 full-parameter continued pretraining (CPT) of the OLMo-3 checkpoint (unit D1).

Implements docs/experiments/E9/prereg.md §4.5 (the recipe table, "Where we differ" and the
"Update check": production checks and divergence), §4.6 tolerance 3 (held-out general-text loss)
and §4.13, and docs/experiments/E9/implementation-plan.md row D1. Frozen values come from
`emx.e9.contracts` only.

Recipe (prereg §4.5):
- **Model:** `C.HF_ID` at `C.REVISION`, bf16 weights and compute, every parameter trainable,
  activation checkpointing (non-reentrant), `use_cache` off. `load_model(None)` loads the pin,
  `load_model(dir)` a local directory; tests monkeypatch `load_model` with a tiny OLMo-3.
- **Optimizer** (`make_optimizer`): `"adamw8bit_sr"` is torchao 0.18
  `torchao.optim.AdamW8bit(lr, betas=C.CPT_BETAS, eps=C.CPT_EPS, weight_decay=0.0,
  bf16_stochastic_round=True)`, fresh state; `"adamw"` is `torch.optim.AdamW` for CPU tests.
  torchao keeps `group["lr"]` as a tensor that must be updated in place (`set_lr`).
- **Schedule** (`lr_factor`): the update after k completed steps (k = 0..152) uses
  `C.CPT_LR * lr_factor(k)`; warmup k / 10 for k < 10, then (153 - k) / (153 - 10), which is 0
  from k = 153 on. This is HF `get_linear_schedule_with_warmup` (the same convention as
  `emx.train.lr_factor`), so the **first update has lr 0** and the peak 1e-5 is update k = 10.
- **Data:** the stream `.npy` (`uint32`, shape (`C.TOTAL_ROWS`, `C.SEQ_LEN`)) is memory-mapped;
  step t uses rows [64t, 64t + 64) in order (one pass, no shuffle). A row is fed as-is: no BOS,
  no attention mask, so attention crosses document boundaries within a row (prereg "Where we
  differ").
- **Loss** (`accumulate_step`): next-token cross-entropy on every token of each row (targets =
  the row shifted by one, 1,023 targets per row; logits upcast to fp32), no z-loss. A step's loss
  is the sum of token CE over all its micro-batches divided by the step's target-token count
  (64 x 1,023 = 65,472): each micro-batch's backward is of `sum_mb / N_step`, so the gradients
  add up to the step token mean whatever the micro-batch split (`emx.train`'s
  "step_token_mean"). Gradients accumulate in the parameters' dtype (bf16 in production).
- **Clipping** 1.0 on the global gradient norm (the pre-clip norm is recorded).
- **RNG:** `torch.manual_seed(cpt_seed)` (it seeds the CPU and every CUDA device) and
  `torch.cuda.manual_seed_all(cpt_seed)` right before the first optimizer step. Verified for
  torchao 0.18: stochastic rounding (`torchao.optim.quant_utils._fp32_to_bf16_sr`) draws
  `torch.randint(0, 2**16, ...)` with no generator argument, i.e. from the default generator of
  the parameter's device. The step runs under `torch.compile`, where inductor replaces it with its
  own Philox RNG whose seeds come from `torch.randint(2**63 - 1, ..., device=device)` on that
  device's default generator (`torch/_inductor/inductor_prims.py`), so the draws are a fixed
  function of `cpt_seed` (checked on CPU: same seed gives identical weights, another seed
  different ones; tests/e9/test_cpt.py). Nothing else in a step draws random numbers (OLMo-3 has
  no dropout; non-reentrant checkpointing restores RNG state).

Production checks (prereg §4.5 "Update check"), recorded in `<run dir>/cpt/record.json` with the
frozen `C.CPT_RECORD_KEYS` (plus extra keys), written atomically after EVERY step (status
`running`), at the end (`done`), or on any failure (`failed`, with `error` and everything so
far):
- every step's loss finite (a non-finite loss stops the run before its update);
- ||theta_t - theta_0|| per matrix of every 4th layer (layers 0, 4, ..., 28) at steps 10, 50,
  100, 153 (those reached, plus the last step of a shortened run);
- the global ||theta_T - theta_0|| and the fraction of elements changed per tensor; every
  parameter tensor must change;
- divergence: mean of the last 10 step losses - mean of the first 10 <= 0.05 (a run shorter
  than 20 steps uses overlapping windows; production runs 153 steps).
theta_0 lives on the CPU (pinned memory when CUDA is available), not on the GPU (~15 GB saved);
norms are computed tensor by tensor on the parameter's device in fp32.

Peak GPU memory (7.30B parameters, H100 80 GB; to be measured on the smoke pod):
- bf16 weights 14.6 GB + bf16 gradients 14.6 GB;
- AdamW8bit state: two 8-bit moments plus one fp32 scale per 256-element block each,
  ~2.03 bytes/parameter = ~14.8 GB;
- activations with checkpointing at micro-batch m rows x 1,024 tokens: the saved layer inputs
  (m x 1,024 x 4,096 x 2 B x 32 layers = 2.1 GB at m = 8), one layer's recompute (~1.5 GB at
  m = 8), and the loss head: bf16 logits m x 1,024 x 100,278 x 2 B (1.6 GB at m = 8) plus the fp32
  upcast, log-softmax and its gradient (~3 x 3.3 GB at m = 8);
- the optimizer step's fp32 temporaries for the largest tensor (embeddings, 411M elements:
  a few x 1.6 GB, transient).
That is ~44 GB of static state plus ~15 GB at m = 8: **recommended micro-batch 8 (8
accumulation steps), ~60 GB peak**. m = 16 adds ~11 GB (logits and saved activations) and may fit;
use it only if the smoke pod's measured peak at m = 8 is under ~62 GB. The CLI sets
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` unless set, against fragmentation.

Held-out general-text loss (`heldout_loss`, prereg §4.6 tolerance 3): per document, not packed.
Each of the 1,000 held-out documents is its pinned-tokenizer tokens (no special tokens) plus the
end-of-text id, cut to `C.SEQ_LEN` tokens if longer (counted); the targets are tokens 1..n-1
(the first token has no context, as no BOS exists); the loss is the token mean over all targets
of all documents. Documents are batched in file order, `HELDOUT_BATCH` at a time, right-padded
with the padding positions excluded, so every model sees identical inputs and batches.

Saved model (`save_model`, `C.SAVED_MODEL_FILES`): config, generation config and tokenizer files
copied from the Hub snapshot at `C.REVISION` (a generation config is written from the model's if
the snapshot has none, and recorded as such), the trained weights as bf16 safetensors with
`model.safetensors.index.json` (always present, also for a single shard), and `SHA256SUMS`
(`sha256sum` format over every other file). It loads with `AutoModelForCausalLM.from_pretrained`,
as `emx.train.train(..., base_path=dir)` and as a vLLM base. Weights are never uploaded
(prereg §4.14).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emx.e9 import contracts as C

OPTIMIZERS: tuple[str, ...] = ("adamw8bit_sr", "adamw")
DEFAULT_MICRO_BATCH = 8  # rows; see the memory estimate in the module docstring
DIVERGENCE_WINDOW = 10  # prereg §4.5: the first and the last 10 steps
HELDOUT_BATCH = 8
LOSS_REDUCTION = "step_token_mean"
STATUS_RUNNING, STATUS_DONE, STATUS_FAILED = "running", "done", "failed"
RECORD_REL = "cpt/record.json"
HELDOUT_REL = "cpt/heldout_loss.json"
# Files copied from the Hub snapshot when present (the first five are required, generation config
# aside: see save_model).
SNAPSHOT_FILES: tuple[str, ...] = (
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.json",
    "merges.txt",
    "chat_template.jinja",
)
SHARD_SIZE = "5GB"


class CPTCheckFailed(RuntimeError):
    """A production check of prereg §4.5 failed (the run is marked failed)."""


# --- model and optimizer ------------------------------------------------------------------------


def load_model(path: str | Path | None = None):
    """The pinned checkpoint (path None) or a local model directory, bf16, on the GPU if any.
    Tests monkeypatch this to return a tiny random OLMo-3."""
    import torch
    from transformers import AutoModelForCausalLM

    if path is None:
        model = AutoModelForCausalLM.from_pretrained(
            C.HF_ID, revision=C.REVISION, dtype=torch.bfloat16
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(str(path), dtype=torch.bfloat16)
    if torch.cuda.is_available():
        model = model.to("cuda")
    return model


def prepare_model(model):
    """All parameters trainable, activation checkpointing (non-reentrant), no KV cache."""
    for p in model.parameters():
        p.requires_grad_(True)
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.train()
    return model


def make_optimizer(params, kind: str = "adamw8bit_sr", lr: float = C.CPT_LR):
    """Fresh optimizer (prereg §4.5). "adamw8bit_sr": torchao AdamW8bit with bf16 stochastic
    rounding (pod); "adamw": torch AdamW, same betas/eps/weight decay (CPU tests)."""
    import torch

    params = list(params)
    if kind == "adamw8bit_sr":
        from torchao.optim import AdamW8bit

        return AdamW8bit(
            params,
            lr=lr,
            betas=C.CPT_BETAS,
            eps=C.CPT_EPS,
            weight_decay=C.CPT_WEIGHT_DECAY,
            bf16_stochastic_round=True,
        )
    if kind == "adamw":
        return torch.optim.AdamW(
            params, lr=lr, betas=C.CPT_BETAS, eps=C.CPT_EPS, weight_decay=C.CPT_WEIGHT_DECAY
        )
    raise ValueError(f"unknown optimizer {kind!r}; expected one of {OPTIMIZERS}")


def set_lr(opt, lr: float) -> None:
    """Set every group's lr; torchao keeps it as a tensor that must be filled in place."""
    for g in opt.param_groups:
        if hasattr(g["lr"], "fill_"):
            g["lr"].fill_(lr)
        else:
            g["lr"] = lr


def lr_factor(k: int) -> float:
    """Multiplier of `C.CPT_LR` for the update after k completed steps (prereg §4.5)."""
    if k < 0:
        raise ValueError(k)
    if k < C.CPT_WARMUP_STEPS:
        return k / C.CPT_WARMUP_STEPS
    return max(0.0, (C.CPT_STEPS - k) / (C.CPT_STEPS - C.CPT_WARMUP_STEPS))


def lr_at(k: int) -> float:
    return C.CPT_LR * lr_factor(k)


# --- data and loss ------------------------------------------------------------------------------


def open_stream(path: str | Path, rows_per_step: int = C.ROWS_PER_STEP, seq_len: int = C.SEQ_LEN):
    """Memory-map the stream; its shape must be (C.CPT_STEPS * rows_per_step, seq_len), uint32
    (production: (C.TOTAL_ROWS, C.SEQ_LEN))."""
    import numpy as np

    arr = np.load(str(path), mmap_mode="r")
    want = (C.CPT_STEPS * rows_per_step, seq_len)
    if arr.dtype != np.dtype(C.STREAM_DTYPE) or tuple(arr.shape) != want:
        raise ValueError(f"{path}: {arr.dtype} {arr.shape}, expected {C.STREAM_DTYPE} {want}")
    return arr


def step_rows(stream, t: int, rows_per_step: int = C.ROWS_PER_STEP):
    """Step t's rows [rows_per_step * t, rows_per_step * (t + 1)) as an int64 tensor."""
    import numpy as np
    import torch

    block = np.asarray(stream[rows_per_step * t : rows_per_step * (t + 1)], dtype=np.int64)
    return torch.from_numpy(block)


def n_targets(rows) -> int:
    """Next-token targets in a block of rows: rows x (seq_len - 1)."""
    return int(rows.shape[0]) * (int(rows.shape[1]) - 1)


def token_ce_sum(model, ids):
    """Sum of next-token CE over every position of every row (fp32 logits)."""
    import torch.nn.functional as F

    logits = model(input_ids=ids).logits
    v = logits.shape[-1]
    return F.cross_entropy(
        logits[:, :-1].float().reshape(-1, v), ids[:, 1:].reshape(-1), reduction="sum"
    )


def accumulate_step(model, rows, micro_batch: int) -> float:
    """Forward/backward one step's rows in micro-batches; gradients accumulate into `.grad`
    as the gradient of the step token mean. Returns the step loss (token mean, float)."""
    if rows.shape[0] % micro_batch:
        raise ValueError(f"micro-batch {micro_batch} does not divide {rows.shape[0]} rows")
    dev = next(model.parameters()).device
    n_step = n_targets(rows)
    total = 0.0
    for i in range(0, rows.shape[0], micro_batch):
        ids = rows[i : i + micro_batch].to(dev)
        loss = token_ce_sum(model, ids) / n_step
        loss.backward()
        total += float(loss.detach().float())
        del loss
    return total


# --- update checks ------------------------------------------------------------------------------


def norm_tensor_names(model, every: int = C.CPT_NORM_LAYER_EVERY) -> list[str]:
    """The matrices (2-D parameters) of layers 0, every, 2*every, ... (prereg §4.5)."""
    out = []
    for name, p in model.named_parameters():
        parts = name.split(".")
        if "layers" in parts and p.ndim == 2:
            i = int(parts[parts.index("layers") + 1])
            if i % every == 0:
                out.append(name)
    return out


def snapshot_params(model) -> dict:
    """theta_0 on the CPU, tensor by tensor (pinned when CUDA is available)."""
    import torch

    pin = torch.cuda.is_available()
    out = {}
    for name, p in model.named_parameters():
        t = torch.empty(p.shape, dtype=p.dtype, device="cpu", pin_memory=pin)
        out[name] = t.copy_(p.detach())  # one host copy, pinned directly (no second buffer)
    return out


def _diff_norm(p, t0) -> float:
    d = p.detach().float() - t0.to(p.device, non_blocking=True).float()
    return float(d.norm())


def layer_update_norms(model, theta0: dict, names: Sequence[str]) -> dict[str, float]:
    params = dict(model.named_parameters())
    return {n: _diff_norm(params[n], theta0[n]) for n in names}


def final_update_stats(model, theta0: dict) -> dict:
    """Global ||theta_T - theta_0||, the fraction of elements changed per tensor, and the
    tensors with no changed element."""
    sq, frac, unchanged = 0.0, {}, []
    for name, p in model.named_parameters():
        t0 = theta0[name].to(p.device, non_blocking=True)
        changed = int((p.detach() != t0).sum())
        frac[name] = changed / p.numel()
        if changed == 0:
            unchanged.append(name)
        sq += float(((p.detach().float() - t0.float()) ** 2).sum())
    return {
        "global_update_norm": math.sqrt(sq),
        "changed_fraction": frac,
        "unchanged_tensors": unchanged,
    }


def divergence(
    losses: Sequence[float],
    window: int = DIVERGENCE_WINDOW,
    max_increase: float = C.CPT_DIVERGENCE_MAX,
) -> dict:
    """mean(last `window`) - mean(first `window`) <= max_increase (prereg §4.5). Runs shorter
    than 2 x window use overlapping windows (all steps if fewer than `window`)."""
    if not losses:
        return {
            "first_mean": None,
            "last_mean": None,
            "increase": None,
            "window": window,
            "max": max_increase,
            "ok": False,
        }
    w = min(window, len(losses))
    first, last = sum(losses[:w]) / w, sum(losses[-w:]) / w
    inc = last - first
    ok = math.isfinite(inc) and inc <= max_increase
    return {
        "first_mean": first,
        "last_mean": last,
        "increase": inc,
        "window": w,
        "max": max_increase,
        "ok": ok,
    }


# --- records ------------------------------------------------------------------------------------


def _clean(x):
    """Strict JSON: non-finite floats become None (the error message carries the value)."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_clean(v) for v in x]
    return x


def write_json_atomic(path: str | Path, obj: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(_clean(obj), indent=1, allow_nan=False) + "\n")
    os.replace(tmp, path)


def file_sha256(path: str | Path, chunk: int = 1 << 24) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def versions() -> dict[str, str]:
    """Library versions (emx.train's set plus torchao, safetensors, numpy) and CUDA/GPU."""
    import importlib.metadata as md

    from emx.train import versions as base_versions

    out = base_versions("peft")
    for p in ("torchao", "safetensors", "numpy", "huggingface-hub"):
        try:
            out[p] = md.version(p)
        except md.PackageNotFoundError:
            out[p] = "absent"
    import torch

    out["cudnn"] = str(torch.backends.cudnn.version())
    return out


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- saving -------------------------------------------------------------------------------------


def hub_snapshot_dir() -> Path:
    """Config, generation config and tokenizer files of the pin (from the HF cache if there)."""
    from huggingface_hub import snapshot_download

    return Path(
        snapshot_download(C.HF_ID, revision=C.REVISION, allow_patterns=list(SNAPSHOT_FILES))
    )


def _ensure_index(out: Path) -> None:
    """A single-shard save has no index: rename it to a shard and write the index."""
    idx = out / "model.safetensors.index.json"
    single = out / "model.safetensors"
    if idx.exists():
        if single.exists():
            raise RuntimeError(f"{out}: both model.safetensors and an index")
        return
    if not single.exists():
        raise RuntimeError(f"{out}: no safetensors written")
    from safetensors import safe_open

    shard = "model-00001-of-00001.safetensors"
    single.rename(out / shard)
    weight_map, total = {}, 0
    with safe_open(str(out / shard), framework="pt") as f:
        for k in f.keys():  # noqa: SIM118 - safe_open has no __iter__
            weight_map[k] = shard
            t = f.get_slice(k)
            n = math.prod(t.get_shape())
            total += n * {"BF16": 2, "F16": 2, "F32": 4}[t.get_dtype()]
    idx.write_text(
        json.dumps(
            {"metadata": {"total_size": total}, "weight_map": dict(sorted(weight_map.items()))},
            indent=2,
        )
        + "\n"
    )


def write_sha256sums(out: Path) -> Path:
    lines = [
        f"{file_sha256(p)}  {p.relative_to(out).as_posix()}"
        for p in sorted(out.rglob("*"))
        if p.is_file() and p.name != "SHA256SUMS"
    ]
    (out / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    return out / "SHA256SUMS"


def verify_saved(out: str | Path) -> dict[str, str]:
    """Check SAVED_MODEL_FILES exist and every SHA256SUMS line matches; returns {file: sha}."""
    out = Path(out)
    missing = [f for f in C.SAVED_MODEL_FILES if not (out / f).is_file()]
    if missing:
        raise RuntimeError(f"{out}: missing {missing}")
    sums = {}
    for line in (out / "SHA256SUMS").read_text().splitlines():
        sha, name = line.split("  ", 1)
        got = file_sha256(out / name)
        if got != sha:
            raise RuntimeError(f"{out / name}: sha256 {got} != {sha}")
        sums[name] = sha
    listed = {
        p.relative_to(out).as_posix()
        for p in out.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS"
    }
    if listed != set(sums):
        raise RuntimeError(f"{out}: SHA256SUMS lists {sorted(sums)}, files {sorted(listed)}")
    return sums


def save_model(
    model, out: str | Path, snapshot_dir: str | Path | None = None, max_shard_size: str = SHARD_SIZE
) -> dict:
    """Save the trained model as a local directory (see the module docstring). Returns
    {"dir", "files": {name: sha256}, "provenance": {file: "hub" | "generated"}}."""
    import torch

    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    snap = Path(snapshot_dir) if snapshot_dir is not None else hub_snapshot_dir()
    if any(p.dtype != torch.bfloat16 for p in model.parameters()):
        raise RuntimeError("save_model expects a bf16 model")
    model.save_pretrained(str(out), max_shard_size=max_shard_size)  # config.json: Hub's below
    _ensure_index(out)
    provenance = {}
    for name in SNAPSHOT_FILES:
        src = snap / name
        if src.is_file():
            shutil.copyfile(src, out / name)  # follows the HF cache's symlinks
            provenance[name] = "hub"
        elif (out / name).is_file():
            provenance[name] = "generated"  # e.g. generation_config.json absent at the pin
    for name in (
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
    ):
        if provenance.get(name) != "hub":
            raise RuntimeError(f"{snap}: {name} missing from the Hub snapshot")
    write_sha256sums(out)
    return {"dir": str(out), "files": verify_saved(out), "provenance": provenance}


# --- training -----------------------------------------------------------------------------------


def train_cpt(
    stream_path: str | Path,
    run_id: str,
    cpt_seed: int,
    run_dir: str | Path,
    model_out: str | Path | None,
    *,
    max_steps: int | None = None,
    micro_batch: int = DEFAULT_MICRO_BATCH,
    optimizer: str = "adamw8bit_sr",
    rows_per_step: int = C.ROWS_PER_STEP,
    seq_len: int = C.SEQ_LEN,
    snapshot_dir: str | Path | None = None,
    model_path: str | Path | None = None,
    on_step: Callable[[int, Any], None] | None = None,
) -> dict:
    """Run continued pretraining and write `<run_dir>/cpt/record.json` after every step.

    Returns the final record (status "done"). On a failed check or any exception the record is
    written with status "failed" and `error`, and the exception is re-raised (`CPTCheckFailed`
    for a failed production check). The model is saved to `model_out` only if every check
    passes (None skips saving: tests and timing runs). `rows_per_step`/`seq_len` other than the
    contract's are for tiny tests only.
    """
    import torch

    t0 = time.time()
    rpath = Path(run_dir) / RECORD_REL
    total = C.CPT_STEPS
    run_steps = total if max_steps is None else min(int(max_steps), total)
    if run_steps < 1:
        raise ValueError(f"max_steps {max_steps}")
    rec: dict[str, Any] = {
        "run_id": run_id,
        "status": STATUS_RUNNING,
        "error": None,
        "seed": int(cpt_seed),
        "losses": [],
        "lr": [],
        "grad_norms": [],
        "tokens_per_s": [],
        "step_times_s": [],
        "update_norms": {},
        "global_update_norm": None,
        "changed_fraction": {},
        "unchanged_tensors": [],
        "divergence": None,
        "checks": {},
        "versions": {},
        "peak_mem_gb": None,
        "peak_reserved_gb": None,
        "wallclock_s": 0.0,
        "steps_done": 0,
        "run_steps": run_steps,
        "total_steps": total,
        "optimizer": optimizer,
        "micro_batch": micro_batch,
        "grad_accum": rows_per_step // micro_batch if micro_batch else None,
        "rows_per_step": rows_per_step,
        "seq_len": seq_len,
        "targets_per_step": rows_per_step * (seq_len - 1),
        "loss_reduction": LOSS_REDUCTION,
        "schedule": {
            "lr": C.CPT_LR,
            "warmup": C.CPT_WARMUP_STEPS,
            "total": total,
            "convention": "update k uses lr*factor(k); k=0 has lr 0 (HF linear)",
        },
        "clip": C.CPT_CLIP,
        "betas": list(C.CPT_BETAS),
        "eps": C.CPT_EPS,
        "weight_decay": C.CPT_WEIGHT_DECAY,
        "model": {
            "source": str(model_path) if model_path else C.HF_ID,
            "revision": None if model_path else C.REVISION,
        },
        "stream": {"path": str(stream_path)},
        "saved_model": None,
        "theta0": None,
        "utc_start": _utc(),
        "utc_end": None,
    }

    def flush(status: str | None = None) -> None:
        if status is not None:
            rec["status"] = status
        rec["wallclock_s"] = time.time() - t0
        if torch.cuda.is_available():
            rec["peak_mem_gb"] = torch.cuda.max_memory_allocated() / 1e9
            rec["peak_reserved_gb"] = torch.cuda.max_memory_reserved() / 1e9
        write_json_atomic(rpath, rec)

    try:
        rec["versions"] = versions()
        if optimizer not in OPTIMIZERS:
            raise ValueError(f"unknown optimizer {optimizer!r}")
        if micro_batch < 1 or rows_per_step % micro_batch:
            raise ValueError(f"micro-batch {micro_batch} must divide {rows_per_step}")
        stream = open_stream(stream_path, rows_per_step, seq_len)
        rec["stream"].update(
            sha256=file_sha256(stream_path),
            shape=list(stream.shape),
            dtype=str(stream.dtype),
            rows_used=[0, rows_per_step * run_steps],
        )
        flush()
        model = prepare_model(load_model(model_path))
        vocab = model.get_input_embeddings().weight.shape[0]
        top = int(stream[: rows_per_step * run_steps].max())
        if top >= vocab:
            raise ValueError(f"stream token id {top} >= vocab {vocab}")
        if torch.cuda.is_available():
            if next(model.parameters()).device.type != "cuda":
                raise RuntimeError("a GPU is available but the model is not on it")
            torch.cuda.reset_peak_memory_stats()
        names = norm_tensor_names(model)
        theta0 = snapshot_params(model)
        rec["theta0"] = "cpu_pinned" if torch.cuda.is_available() else "cpu"
        params = [p for p in model.parameters() if p.requires_grad]
        opt = make_optimizer(params, optimizer)
        norm_steps = sorted({s for s in C.CPT_NORM_STEPS if s <= run_steps} | {run_steps})
        torch.manual_seed(cpt_seed)  # stochastic rounding's generator (see module docstring)
        torch.cuda.manual_seed_all(cpt_seed)
        for k in range(run_steps):
            ts = time.time()
            lr = lr_at(k)
            set_lr(opt, lr)
            rows = step_rows(stream, k, rows_per_step)
            loss = accumulate_step(model, rows, micro_batch)
            rec["lr"].append(lr)
            rec["losses"].append(loss)
            if not math.isfinite(loss):
                rec["checks"]["finite_losses"] = False
                raise CPTCheckFailed(f"non-finite loss {loss!r} at step {k}")
            gn = torch.nn.utils.clip_grad_norm_(params, C.CPT_CLIP)
            opt.step()
            opt.zero_grad(set_to_none=True)
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            dt = time.time() - ts
            rec["grad_norms"].append(float(gn))
            rec["step_times_s"].append(dt)
            rec["tokens_per_s"].append(rows.numel() / dt)
            done = k + 1
            rec["steps_done"] = done
            if done in norm_steps:
                rec["update_norms"][str(done)] = layer_update_norms(model, theta0, names)
            if on_step is not None:
                on_step(done, model)
            flush()
        rec["checks"]["finite_losses"] = True
        rec.update(final_update_stats(model, theta0))
        rec["checks"]["all_tensors_changed"] = not rec["unchanged_tensors"]
        rec["divergence"] = divergence(rec["losses"])
        rec["checks"]["divergence"] = rec["divergence"]["ok"]
        failed = [c for c, ok in rec["checks"].items() if not ok]
        if failed:
            raise CPTCheckFailed(
                f"checks failed: {failed}; unchanged tensors: {rec['unchanged_tensors'][:10]}"
            )
        del theta0
        if model_out is not None:
            flush()
            rec["saved_model"] = save_model(model, model_out, snapshot_dir)
        rec["utc_end"] = _utc()
        flush(STATUS_DONE)
        return rec
    except BaseException as e:
        rec["error"] = repr(e)
        rec["utc_end"] = _utc()
        try:
            flush(STATUS_FAILED)
        except Exception as w:  # noqa: BLE001 - keep the original exception
            e.add_note(f"failed record not written: {w!r}")
        raise


# --- held-out loss ------------------------------------------------------------------------------


def load_heldout_docs(path: str | Path, expect_n: int | None = C.HELDOUT_TEXT_N) -> list[str]:
    """Each row's `text`, in file order: held-out documents (DOC_KEYS) or any rows carrying
    `text`, e.g. insert versions (VERSION_KEYS). `expect_n` None skips the count check."""
    docs = [json.loads(x)["text"] for x in Path(path).read_text().splitlines() if x.strip()]
    if expect_n is not None and len(docs) != expect_n:
        raise ValueError(f"{path}: {len(docs)} documents, expected {expect_n}")
    return docs


def heldout_loss(
    model, docs: Sequence[str], tok, batch_size: int = HELDOUT_BATCH, max_len: int = C.SEQ_LEN
) -> dict:
    """Token-mean next-token loss over `docs` (per document + end-of-text; module docstring)."""
    import torch
    import torch.nn.functional as F

    eos = tok.eos_token_id
    if eos is None:
        raise ValueError("tokenizer has no end-of-text id")
    seqs, cut = [], 0
    for d in docs:
        ids = list(tok(d, add_special_tokens=False)["input_ids"]) + [eos]
        if len(ids) > max_len:
            ids, cut = ids[:max_len], cut + 1
        seqs.append(ids)
    was = model.training
    model.eval()
    dev = next(model.parameters()).device
    total, n = 0.0, 0
    with torch.no_grad():
        for i in range(0, len(seqs), batch_size):
            chunk = seqs[i : i + batch_size]
            L = max(len(s) for s in chunk)
            ids = torch.zeros((len(chunk), L), dtype=torch.long)
            attn = torch.zeros((len(chunk), L), dtype=torch.long)
            tgt = torch.full((len(chunk), L), -100, dtype=torch.long)
            for j, s in enumerate(chunk):
                ids[j, : len(s)] = torch.tensor(s)
                attn[j, : len(s)] = 1
                tgt[j, : len(s)] = torch.tensor(s)
            logits = model(input_ids=ids.to(dev), attention_mask=attn.to(dev)).logits
            v = logits.shape[-1]
            total += float(
                F.cross_entropy(
                    logits[:, :-1].float().reshape(-1, v),
                    tgt[:, 1:].reshape(-1).to(dev),
                    ignore_index=-100,
                    reduction="sum",
                )
            )
            n += sum(len(s) - 1 for s in chunk)
    model.train(was)
    return {
        "loss": total / n,
        "n_docs": len(seqs),
        "n_targets": n,
        "n_truncated": cut,
        "method": "per_doc_eos_right_padded",
        "batch_size": batch_size,
        "max_len": max_len,
    }


def heldout_for(
    model_spec: str | Path | None,
    docs_path: str | Path,
    out: str | Path,
    tok=None,
    expect_n: int | None = C.HELDOUT_TEXT_N,
) -> dict:
    """Load a model (None or "hub": the pinned checkpoint), score the held-out text, write
    `out` (cpt/heldout_loss.json). Uses the pinned tokenizer for every model."""
    import torch

    t0 = time.time()
    hub = model_spec is None or str(model_spec) in ("hub", C.HF_ID)
    if tok is None:
        from emx.e9.text import tokenizer

        tok = tokenizer()
    model = load_model(None if hub else model_spec)
    res = heldout_loss(model, load_heldout_docs(docs_path, expect_n), tok)
    res.update(
        model={
            "source": C.HF_ID if hub else str(model_spec),
            "revision": C.REVISION if hub else None,
        },
        docs_path=str(docs_path),
        docs_sha256=file_sha256(docs_path),
        dtype=str(next(model.parameters()).dtype),
        versions=versions(),
        utc=_utc(),
        wallclock_s=time.time() - t0,
    )
    if hub is False and (Path(model_spec) / "SHA256SUMS").is_file():
        res["model"]["sha256sums_sha256"] = file_sha256(Path(model_spec) / "SHA256SUMS")
    if torch.cuda.is_available():
        res["peak_mem_gb"] = torch.cuda.max_memory_allocated() / 1e9
    write_json_atomic(out, res)
    return res
