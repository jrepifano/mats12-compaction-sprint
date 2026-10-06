"""E1 LoRA trainer: shared core (data, batch order, loop, eval, logging, push) + PEFT backend.

Implements docs/experiments/E1/implementation-plan.md §1 "Trainer interface" and prereg §4.4
(recipe), §4.10 (training curves) and amendment A items 2 and 4. Hyperparameters come only from
the `TrainConfig` JSON (`configs/E1/{cell}.json`, written by scripts/e1_make_configs.py from the
registry). Reference for the published recipe: ../mats12_neel_working/scripts/make_config.py
(read-only); no upstream training code is used.

A backend only LOADS: `BACKENDS[cfg.backend](cfg) -> (model, tokenizer)` returns the pinned model
with its seeded LoRA adapter attached. One shared loop (`train`) then trains either backend with
the same ordered batches, labels, optimizer, schedule and loss.

Loss reduction (`LOSS_REDUCTION = "step_token_mean"`, the HF Trainer default since transformers
4.46 and what unsloth/TRL use): each optimizer step's loss is the sum of token cross-entropies
over every trained token of all its micro-batches, divided by the number of trained tokens in the
whole accumulation window. Each micro-batch's forward is called with
`num_items_in_batch = <trained tokens in the window>`, so the model's own loss returns
`sum_microbatch / N_window`; the backward passes add up to the window's token mean, with no
further division by `grad_accum`. `step_losses[t]` is that window token mean (fp32). The
alternative `"microbatch_mean"` (mean over each micro-batch's tokens, divided by the number of
micro-batches in the window: HF before 4.46) is kept for comparison only.

Schedule: HF `get_linear_schedule_with_warmup` exactly: the update after `k` completed optimizer
steps uses `lr * lr_factor(k)` (so the first update uses lr 0). Total steps = epochs x
ceil(ceil(n / per_device_batch) / grad_accum): each epoch's micro-batches are grouped into
windows of `grad_accum` and the last window of an epoch may be short (as in the HF Trainer).
`max_steps` stops early (smoke test) without changing the schedule's total.

E1b dense checkpoints (docs/experiments/E1b/implementation-plan.md §1.5; prereg §4.4, §4.12),
additive: with `checkpoint_steps` left at None the trainer is E1's, unchanged (its record has E1's
key set: no `eval_steps`, `checkpoints`, `status` or `error`). When it is given:
- held-out loss at step 0, at every checkpoint step and at the last step (an empty sequence keeps
  E1's every-`EVAL_EVERY` cadence), recorded in `TrainResult.eval_steps`;
- `on_checkpoint(0, model)` once before the first update, then `on_checkpoint(step, model)` after
  each checkpoint step's update; it returns the checkpoint row (`emx.e1b.contracts.CHECKPOINT_KEYS`,
  built by `emx.e1b.checkpoints`), and the trainer fills in `eval_loss`, `train_loss_window` (mean
  step loss since the previous checkpoint) and `seconds` (wall time of the held-out eval plus the
  callback: the per-checkpoint cost the smoke test benchmarks);
- the record `{results_dir}/{cell}.json` is written atomically at step 0 and after every eval
  point (status `running`), when training ends (`trained`, hub_revision None) and after the push
  (`pushed`). On any exception after the model is built it is written with status `failed`,
  `error` and the curves so far, and the exception is re-raised.

E9 (docs/experiments/E9/implementation-plan.md, U0), additive: `train(..., base_path=DIR)` loads
the base weights from a local model directory (a continued-pretrained checkpoint) instead of the
Hub pin; the tokenizer stays the pinned one. With `base_path` None nothing changes. The caller
records the directory and its weight hashes (the record's key set is unchanged).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import platform
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from emx.contracts import (
    IGNORE_INDEX,
    LoraSpec,
    MaskedExample,
    TrainConfig,
    TrainResult,
    labels_sha256,
)
from emx.e1.registry import PAD_ID, STAGES
from emx.e1b.contracts import CHECKPOINT_KEYS

LOSS_REDUCTION = "step_token_mean"  # or "microbatch_mean" (see module docstring)
EVAL_EVERY = 50
EVAL_BATCH = 8
RESULTS_DIR = Path("results/E1/train")
_BASE_PATH: Path | None = None  # set only for the duration of train(base_path=...)

# --- config -------------------------------------------------------------------------------------


def _strict(cls, d: dict, where: str) -> dict:
    names = {f.name for f in dataclasses.fields(cls)}
    missing, unknown = names - set(d), set(d) - names
    if missing or unknown:
        raise ValueError(f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    return d


def config_from_dict(d: dict) -> TrainConfig:
    d = dict(_strict(TrainConfig, d, "config"))
    lora = dict(_strict(LoraSpec, d["lora"], "config.lora"))
    lora["target_modules"] = tuple(lora["target_modules"])
    d["lora"] = LoraSpec(**lora)
    return TrainConfig(**d)


def config_to_dict(cfg: TrainConfig) -> dict:
    d = dataclasses.asdict(cfg)
    d["lora"]["target_modules"] = list(cfg.lora.target_modules)
    return d


def load_config(path: str | Path) -> TrainConfig:
    """Read `configs/E1/{cell}.json`; unknown or missing keys raise ValueError."""
    return config_from_dict(json.loads(Path(path).read_text()))


# --- backends -----------------------------------------------------------------------------------


def _check_pins(cfg: TrainConfig) -> None:
    s = STAGES[cfg.stage]
    got = (cfg.hf_id, cfg.model_revision, cfg.tokenizer_revision)
    want = (s["hf_id"], s["revision"], s["tokenizer_revision"])
    if got != want:
        raise ValueError(f"config pins {got} != registry {want}")


def load_tokenizer(cfg: TrainConfig):
    """The stage's own pinned tokenizer (emx.formats.load_tokenizer, checked against cfg)."""
    from emx.formats import load_tokenizer as _load

    _check_pins(cfg)
    return _load(cfg.stage)


def _load_base(cfg: TrainConfig):
    """Pinned base model, full precision weights (bf16 if cfg.bf16), no quantization.
    Tests monkeypatch this to return a tiny random OLMo-3."""
    import torch
    from transformers import AutoModelForCausalLM

    dtype = torch.bfloat16 if cfg.bf16 else torch.float32
    if _BASE_PATH is not None:  # E9: a local continued-pretrained model
        return AutoModelForCausalLM.from_pretrained(str(_BASE_PATH), dtype=dtype)
    return AutoModelForCausalLM.from_pretrained(cfg.hf_id, revision=cfg.model_revision, dtype=dtype)


def lora_modules(model) -> list[str]:
    """Names of modules carrying a LoRA adapter pair (lora_A and lora_B)."""
    return [
        n
        for n, m in model.named_modules()
        if hasattr(m, "lora_A") and hasattr(m, "lora_B") and len(getattr(m, "lora_A", {})) > 0
    ]


def assert_lora_layout(model, spec: LoraSpec) -> None:
    """Exactly one adapter pair on each target module of each of `spec.layers` layers."""
    names = lora_modules(model)
    if len(names) != len(spec.target_modules) * spec.layers:
        raise AssertionError(
            f"{len(names)} LoRA pairs != {len(spec.target_modules)} x {spec.layers}"
        )
    per_layer: dict[str, set[str]] = {}
    for n in names:
        parts = n.split(".")
        i = parts.index("layers")
        per_layer.setdefault(parts[i + 1], set()).add(parts[-1])
    if len(per_layer) != spec.layers or any(
        v != set(spec.target_modules) for v in per_layer.values()
    ):
        raise AssertionError(f"LoRA modules per layer do not match {spec.target_modules}")


def load_peft(cfg: TrainConfig):
    """PEFT backend: pinned model + seeded rsLoRA adapter (prereg §4.4)."""
    import torch
    from peft import LoraConfig, get_peft_model

    tok = load_tokenizer(cfg)
    base = _load_base(cfg)
    if torch.cuda.is_available():
        # from_pretrained loads onto the CPU; without this the smoke run trained on the CPU
        # (2026-09-29 P1). Moved before the adapter is attached so LoRA weights live on the GPU.
        base = base.to("cuda")
    base.config.use_cache = False
    lcfg = LoraConfig(
        r=cfg.lora.r,
        lora_alpha=cfg.lora.alpha,
        lora_dropout=cfg.lora.dropout,
        target_modules=list(cfg.lora.target_modules),
        use_rslora=cfg.lora.use_rslora,
        bias="none",
        task_type="CAUSAL_LM",
    )
    torch.manual_seed(cfg.seed)  # adapter init (prereg §4.4: the seed sets init and data order)
    model = get_peft_model(base, lcfg)
    assert_lora_layout(model, cfg.lora)
    # Memory only (no effect on the math): recompute activations in the backward pass.
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    return model, tok


def _load_unsloth(cfg: TrainConfig):
    from emx import train_unsloth  # lazy: unsloth exists only in the pod's unsloth venv

    return train_unsloth.load(cfg)


BACKENDS = {"peft": load_peft, "unsloth": _load_unsloth}


def init_adapter_sha256(model) -> str:
    """sha256 over every LoRA parameter tensor, sorted by name, bytes of `.float().cpu()`."""
    h = hashlib.sha256()
    named = sorted(
        ((n, p) for n, p in model.named_parameters() if "lora_" in n), key=lambda x: x[0]
    )
    if not named:
        raise AssertionError("model has no LoRA parameters")
    for _, p in named:
        h.update(p.detach().float().cpu().contiguous().numpy().tobytes())
    return h.hexdigest()


# --- data ---------------------------------------------------------------------------------------


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _mask_file(cfg: TrainConfig, path: str, sha: str, tokenizer, root: Path):
    from emx.masking import assert_mask_contract, mask_example_with_stats

    p = root / path
    got = file_sha256(p)
    if got != sha:
        raise ValueError(f"{path}: sha256 {got} != config {sha}")
    out, truncated = [], {}
    for row in _read_jsonl(p):
        ex, st = mask_example_with_stats(
            cfg.fmt, cfg.stage, row["id"], row["question"], row["answer"], tokenizer, cfg.max_len
        )
        assert_mask_contract(ex, st.n_prompt)
        out.append(ex)
        if st.truncated:
            truncated[ex.id] = st.truncated
    return out, truncated


def build_examples(cfg: TrainConfig, tokenizer, root: str | Path = "."):
    """Mask the training and eval files (sha256-checked). Returns (train, eval, truncation),
    truncation = {"train": {id: answer tokens dropped}, "eval": {...}}."""
    root = Path(root)
    train, t_tr = _mask_file(cfg, cfg.data_file, cfg.data_sha256, tokenizer, root)
    ev, t_ev = _mask_file(cfg, cfg.eval_file, cfg.eval_sha256, tokenizer, root)
    return train, ev, {"train": t_tr, "eval": t_ev}


def batch_order(n: int, cfg: TrainConfig) -> list[list[int]]:
    """Micro-batches of example indices: one `torch.Generator` seeded with cfg.seed draws a
    permutation per epoch, chunked into `per_device_batch` (the last chunk may be short)."""
    import torch

    g = torch.Generator().manual_seed(cfg.seed)
    out: list[list[int]] = []
    for _ in range(cfg.epochs):
        perm = torch.randperm(n, generator=g).tolist()
        out += [perm[i : i + cfg.per_device_batch] for i in range(0, n, cfg.per_device_batch)]
    return out


def batch_order_sha256(order: list[list[int]]) -> str:
    return hashlib.sha256(json.dumps(order, separators=(",", ":")).encode()).hexdigest()


def windows(n: int, cfg: TrainConfig) -> list[list[int]]:
    """Micro-batch positions grouped per optimizer step; windows never span an epoch."""
    per_epoch = math.ceil(n / cfg.per_device_batch)
    out = []
    for e in range(cfg.epochs):
        idx = list(range(e * per_epoch, (e + 1) * per_epoch))
        out += [idx[i : i + cfg.grad_accum] for i in range(0, per_epoch, cfg.grad_accum)]
    return out


def total_steps(n: int, cfg: TrainConfig) -> int:
    return cfg.epochs * math.ceil(math.ceil(n / cfg.per_device_batch) / cfg.grad_accum)


def lr_factor(step: int, warmup: int, total: int) -> float:
    """HF `get_linear_schedule_with_warmup` multiplier after `step` completed updates."""
    if step < warmup:
        return step / max(1, warmup)
    return max(0.0, (total - step) / max(1, total - warmup))


def collate(examples: list[MaskedExample]):
    """Right-pad with PAD_ID; padded labels IGNORE_INDEX, attention mask 0."""
    import torch

    L = max(len(e.input_ids) for e in examples)
    ids = torch.full((len(examples), L), PAD_ID, dtype=torch.long)
    labels = torch.full((len(examples), L), IGNORE_INDEX, dtype=torch.long)
    attn = torch.zeros((len(examples), L), dtype=torch.long)
    for i, e in enumerate(examples):
        n = len(e.input_ids)
        ids[i, :n] = torch.tensor(e.input_ids)
        labels[i, :n] = torch.tensor(e.labels)
        attn[i, :n] = 1
    return ids, labels, attn


def n_trained(examples: list[MaskedExample]) -> int:
    """Tokens that carry loss after the causal shift (the first label is always a prompt token,
    so this equals the count of non-ignored labels)."""
    return sum(sum(1 for lab in e.labels[1:] if lab != IGNORE_INDEX) for e in examples)


# --- loop ---------------------------------------------------------------------------------------


def _device(model):
    return next(model.parameters()).device


def eval_loss(model, examples: list[MaskedExample]) -> float:
    """Token mean cross-entropy over every trained token of the eval set."""
    import torch

    was = model.training
    model.eval()
    dev = _device(model)
    total, n = 0.0, n_trained(examples)
    with torch.no_grad():
        for i in range(0, len(examples), EVAL_BATCH):
            chunk = examples[i : i + EVAL_BATCH]
            ids, labels, attn = collate(chunk)
            out = model(
                input_ids=ids.to(dev),
                attention_mask=attn.to(dev),
                labels=labels.to(dev),
                num_items_in_batch=torch.tensor(n_trained(chunk)),
            )
            total += float(out.loss.float()) * n_trained(chunk)
    model.train(was)
    return total / n


def make_optimizer(name: str, params, cfg: TrainConfig):
    import torch

    if name == "adamw_8bit":
        import bitsandbytes as bnb  # lazy: pod only

        return bnb.optim.AdamW8bit(params, lr=cfg.lr, weight_decay=cfg.weight_decay)
    if name == "adamw_torch":  # CPU tests only; recorded in the result
        return torch.optim.AdamW(params, lr=cfg.lr, weight_decay=cfg.weight_decay)
    raise ValueError(f"unknown optimizer {name!r}")


def versions(backend: str) -> dict[str, str]:
    import importlib.metadata as md

    out = {"python": sys.version.split()[0], "platform": platform.platform()}
    pkgs = ["torch", "transformers", "peft", "accelerate", "bitsandbytes"]
    if backend == "unsloth":
        pkgs += ["unsloth", "unsloth_zoo"]
    for p in pkgs:
        try:
            out[p] = md.version(p)
        except md.PackageNotFoundError:
            pass
    try:
        import torch

        out["cuda"] = str(torch.version.cuda)
        if torch.cuda.is_available():
            out["gpu"] = torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return out


def push_adapter(output_dir: str, hub_repo: str, cell: str, message: str | None = None) -> str:
    """Upload the adapter folder (E1b: with its `step-NNNN/` subfolders, one commit) to a PRIVATE
    Hub repo; returns the commit sha."""
    from huggingface_hub import HfApi

    api = HfApi()
    api.create_repo(hub_repo, private=True, exist_ok=True, repo_type="model")
    if not api.repo_info(hub_repo).private:
        raise RuntimeError(f"{hub_repo} exists and is not private; refusing to push")
    info = api.upload_folder(
        folder_path=output_dir, repo_id=hub_repo, commit_message=message or f"E1 adapter {cell}"
    )
    return info.oid


E1B_RECORD_KEYS: tuple[str, ...] = ("eval_steps", "checkpoints", "status", "error")


def write_record(path: Path, res: TrainResult) -> None:
    """Write a training record atomically (temp file in the same directory, then os.replace).
    An E1 record (`status` None: no lifecycle) omits the E1b keys, so its key set is E1's."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    d = dataclasses.asdict(res)
    if res.status is None:
        if any(d[k] for k in E1B_RECORD_KEYS):
            raise AssertionError(f"{res.cell}: E1b fields set without a status")
        d = {k: v for k, v in d.items() if k not in E1B_RECORD_KEYS}
    tmp.write_text(json.dumps(d, indent=1) + "\n")
    os.replace(tmp, path)


def train(
    cfg: TrainConfig,
    backend_override: str | None = None,
    max_steps: int | None = None,
    optimizer_override: str | None = None,
    push: bool = True,
    root: str | Path = ".",
    results_dir: str | Path | None = None,
    checkpoint_steps: Sequence[int] | None = None,
    on_checkpoint: Callable[[int, Any], dict | None] | None = None,
    push_message: str | None = None,
    base_path: str | Path | None = None,
) -> TrainResult:
    """Train one cell and write `results/E1/train/{cell}.json`. See the module docstring
    (E1b: `checkpoint_steps`, `on_checkpoint` and the record lifecycle; E9: `base_path`)."""
    global _BASE_PATH
    if base_path is None:
        return _train(cfg, backend_override, max_steps, optimizer_override, push, root,
                      results_dir, checkpoint_steps, on_checkpoint, push_message)
    if not Path(base_path).is_dir():
        raise FileNotFoundError(f"base_path {base_path} is not a directory")
    _BASE_PATH = Path(base_path)
    try:
        return _train(cfg, backend_override, max_steps, optimizer_override, push, root,
                      results_dir, checkpoint_steps, on_checkpoint, push_message)
    finally:
        _BASE_PATH = None


def _train(
    cfg: TrainConfig,
    backend_override: str | None,
    max_steps: int | None,
    optimizer_override: str | None,
    push: bool,
    root: str | Path,
    results_dir: str | Path | None,
    checkpoint_steps: Sequence[int] | None,
    on_checkpoint: Callable[[int, Any], dict | None] | None,
    push_message: str | None,
) -> TrainResult:
    import torch

    os.environ["WANDB_MODE"] = "offline"
    t0 = time.time()
    backend = backend_override or cfg.backend
    if backend != cfg.backend:
        cfg = dataclasses.replace(cfg, backend=backend)
    opt_name = optimizer_override or cfg.optimizer
    if cfg.lr_schedule != "linear":
        raise ValueError(f"unsupported lr_schedule {cfg.lr_schedule!r}")
    lifecycle = checkpoint_steps is not None  # E1b; None keeps E1's trainer exactly
    ckpt_set = set(checkpoint_steps or ())
    if on_checkpoint is not None and not ckpt_set:
        raise ValueError("on_checkpoint needs checkpoint_steps")
    if any(type(s) is not int or s < 1 for s in ckpt_set):
        raise ValueError(f"checkpoint steps must be positive ints: {sorted(ckpt_set)}")
    rpath = (Path(results_dir) if results_dir is not None else Path(root) / RESULTS_DIR) / (
        f"{cfg.cell}.json"
    )

    model, tok = BACKENDS[backend](cfg)
    # Filled in as the run goes, so a `running` or `failed` record carries what exists so far.
    init_sha = lab_sha = order_sha = ""
    tokens: dict[str, int] = {}
    truncation: dict = {"train": {}, "eval": {}}
    total = run_steps = 0
    eval_steps: list[int] = []
    eval_losses: list[float] = []
    step_losses: list[float] = []
    step_times: list[float] = []
    ckpt_rows: list[dict] = []
    out_dir = Path(cfg.output_dir)

    def result(hub_revision: str | None, status: str | None = None, error: str | None = None):
        v = versions(backend)
        v.update(
            {
                "run.optimizer": opt_name,
                "run.loss_reduction": LOSS_REDUCTION,
                "run.steps": str(run_steps),
                "run.total_steps": str(total),
                "run.max_steps": "" if max_steps is None else str(max_steps),
                "run.eval_steps": ",".join(map(str, eval_steps)),
                "run.truncated_train": str(len(truncation["train"])),
                "run.truncated_eval": str(len(truncation["eval"])),
                "run.config_sha256": hashlib.sha256(
                    json.dumps(config_to_dict(cfg), sort_keys=True).encode()
                ).hexdigest(),
            }
        )
        e1b = {}
        if lifecycle:
            e1b = {
                "eval_steps": list(eval_steps),
                "checkpoints": [dict(r) for r in ckpt_rows],
                "status": status,
                "error": error,
            }
        return TrainResult(
            cell=cfg.cell,
            backend=backend,
            step_losses=list(step_losses),
            eval_losses=list(eval_losses),
            trained_tokens_per_example=tokens,
            labels_sha256=lab_sha,  # the micro-batches actually trained, in order
            batch_order_sha256=order_sha,
            init_adapter_sha256=init_sha,
            step_times_s=list(step_times),
            adapter_dir=str(out_dir),
            hub_repo=cfg.hub_repo,
            hub_revision=hub_revision,
            wallclock_s=time.time() - t0,
            versions=v,
            **e1b,
        )

    try:
        assert_lora_layout(model, cfg.lora)  # both backends: 7 modules x layers
        init_sha = init_adapter_sha256(model)  # before any update
        train_ex, eval_ex, truncation = build_examples(cfg, tok, root)

        out_dir.mkdir(parents=True, exist_ok=True)
        order = batch_order(len(train_ex), cfg)
        (out_dir / "batch_order.json").write_text(
            json.dumps({"seed": cfg.seed, "n": len(train_ex), "batches": order}) + "\n"
        )
        batches = [[train_ex[i] for i in b] for b in order]
        wins = windows(len(train_ex), cfg)
        total = total_steps(len(train_ex), cfg)
        assert len(wins) == total and sum(map(len, wins)) == len(batches)
        run_steps = total if max_steps is None else min(max_steps, total)
        used = [batches[m] for w in wins[:run_steps] for m in w]
        # Hashed once: a `running` or `failed` record carries the planned run's values.
        lab_sha, order_sha = labels_sha256(used), batch_order_sha256(order)
        tokens = {e.id: n_trained([e]) for e in train_ex}

        params = [p for p in model.parameters() if p.requires_grad]
        opt = make_optimizer(opt_name, params, cfg)
        sched = torch.optim.lr_scheduler.LambdaLR(
            opt, lambda s: lr_factor(s, cfg.warmup_steps, total)
        )
        dev = _device(model)
        if torch.cuda.is_available() and dev.type != "cuda":
            raise RuntimeError(
                f"a GPU is available but the model is on {dev}: refusing to train on CPU"
            )
        model.train()

        eval_steps.append(0)
        eval_losses.append(eval_loss(model, eval_ex))
        if on_checkpoint is not None:
            on_checkpoint(0, model)  # step-0 measurements only: no save, no checkpoint row
        if lifecycle:
            write_record(rpath, result(None, "running"))
        prev_ckpt = 0
        for step in range(run_steps):
            ts = time.time()
            win = [batches[m] for m in wins[step]]
            n_win = n_trained([e for b in win for e in b])
            loss_sum = 0.0
            for b in win:
                ids, labels, attn = collate(b)
                window_mean = LOSS_REDUCTION == "step_token_mean"
                out = model(
                    input_ids=ids.to(dev),
                    attention_mask=attn.to(dev),
                    labels=labels.to(dev),
                    num_items_in_batch=torch.tensor(n_win if window_mean else n_trained(b)),
                )
                loss = out.loss if window_mean else out.loss / len(win)
                loss.backward()
                loss_sum += float(loss.detach().float())
            torch.nn.utils.clip_grad_norm_(params, cfg.max_grad_norm)
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            if dev.type == "cuda":
                torch.cuda.synchronize()
            step_times.append(time.time() - ts)
            step_losses.append(loss_sum)
            done = step + 1
            if not lifecycle:
                if done % EVAL_EVERY == 0 or done == run_steps:
                    eval_steps.append(done)
                    eval_losses.append(eval_loss(model, eval_ex))
                continue
            is_ckpt = done in ckpt_set
            cadence = done % EVAL_EVERY == 0 if not ckpt_set else False
            if not (is_ckpt or cadence or done == run_steps):
                continue
            tc = time.time()
            eval_steps.append(done)
            eval_losses.append(eval_loss(model, eval_ex))
            if is_ckpt:
                row = on_checkpoint(done, model) if on_checkpoint is not None else None
                row = dict(row or {"subfolder": "", "dtype": None, "adapter_norm": None})
                window = step_losses[prev_ckpt:]
                prev_ckpt = done
                row.update(
                    step=done,
                    eval_loss=eval_losses[-1],
                    train_loss_window=sum(window) / len(window),
                    seconds=time.time() - tc,
                )
                ckpt_rows.append({k: row[k] for k in CHECKPOINT_KEYS})
            write_record(rpath, result(None, "running"))

        model.save_pretrained(str(out_dir))
        if lifecycle:
            write_record(rpath, result(None, "trained"))
        hub_revision = (
            push_adapter(str(out_dir), cfg.hub_repo, cfg.cell, push_message) if push else None
        )
    except BaseException as e:
        if lifecycle:
            try:
                write_record(rpath, result(None, "failed", repr(e)))
            except Exception as w:  # noqa: BLE001 - keep the original exception
                e.add_note(f"failed record not written: {w!r}")
        raise

    res = result(hub_revision, "pushed" if hub_revision else "trained")
    write_record(rpath, res)
    return res
