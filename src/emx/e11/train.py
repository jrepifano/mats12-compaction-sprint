"""E11 rank-32 LoRA training: config, layout asserts and one training run (`train_run`).

Prereg §4.3 and §4.7.3; implementation plan "Implementation readings" 5 and 6; interfaces.md U2.

The loop is `emx.qwen.train.bench` (EQ0's rank-1 bench, itself built on `emx.train`'s
`lr_factor`, `make_optimizer` and `batch_order`) generalized to a `C.Recipe`, with:
- **rendering** through `emx.qwen.render.render_train(..., system=system)`, so every row (and
  every held-out row) carries the run's framing; the system turn is masked;
- **order** `emx.qwen.train.micro_batches(len(rows), seed)` (a torch.Generator(seed)
  permutation in micro-batches of 4), grouped 4 per optimizer step: one epoch is
  ⌈n / 16⌉ steps and the last window may be partial (1 med, 2 fin rows at rungs 2-3); every
  step's loss is the window's summed token cross-entropy over its own trained tokens
  ("step_token_mean"), so the partial window is normalized the same way (plan reading 5);
- **schedule** `lr * emx.train.lr_factor(k, warmup, total)` with total = the full epoch's step
  count, also when `max_steps` truncates (the bench's 20 steps are the real run's first 20);
- **non-finite stop**: a non-finite loss or grad norm records the step (`finite: false`), skips
  the update and ends the run with status "nonfinite"; no adapter is saved;
- **held-out loss** (no grad, eval mode) on the first `C.HELDOUT_LOSS_ROWS` held-out rows under
  the same system, every `C.HELDOUT_LOSS_EVERY` steps and after the last step (plan reading 6);
- **records** appended and fsynced per step (`steps.jsonl`, `heldout.jsonl`); `run.json`
  written atomically, also on failure (status "failed" with the error, then re-raised). Files of
  an earlier attempt in the same `out_dir` are moved to `attempt<k>/`, never overwritten;
- **adapter** via PEFT `save_pretrained` (safetensors, `save_embedding_layers=False` so PEFT
  never looks up the base config on the Hub); only `adapter_config.json` and
  `adapter_model.safetensors` are kept, each sha256'd; no Hub upload;
- **cleanup**: PEFT `unload()` (no merge) removes every LoRA layer, then the base's original
  `requires_grad` flags and train/eval mode are restored, so the next call starts from a clean
  base with bitwise-unchanged weights.

torch, transformers and peft are imported lazily inside functions.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import time
from pathlib import Path
from types import SimpleNamespace

from emx.e11 import contracts as C
from emx.e11.data import git_commit, ids_sha256, sha256_file, utc
from emx.qwen import registry as R
from emx.qwen.render import IGNORE_INDEX, render_train
from emx.qwen.train import (
    _sync,
    collate,
    lora_module_names,
    mask_counts,
    micro_batches,
    n_loss_tokens,
    n_trainable,
)

ATTN_MODULES = ("q_proj", "k_proj", "v_proj", "o_proj")
MLP_MODULES = ("gate_proj", "up_proj", "down_proj")
ADAPTER_FILES = R.ADAPTER_FILES  # adapter_config.json, adapter_model.safetensors


def lora_config(recipe: C.Recipe, target_modules=None):
    """The recipe's LoraConfig: every layer, `target_modules` (default the recipe's ALL_PROJ)."""
    from peft import LoraConfig

    return LoraConfig(
        r=recipe.rank,
        lora_alpha=recipe.alpha,
        target_modules=list(target_modules or recipe.target_modules),
        use_rslora=recipe.use_rslora,
        lora_dropout=recipe.dropout,
        bias="none",
        task_type="CAUSAL_LM",
    )


def attach(model, recipe: C.Recipe, seed: int):
    """get_peft_model with `lora_config(recipe)`, PEFT's init under torch.manual_seed(seed)."""
    import torch
    from peft import get_peft_model

    torch.manual_seed(seed)
    return get_peft_model(model, lora_config(recipe))


def assert_r32_layout(
    model, n_layers: int = R.N_LAYERS, expect_trainable: int | None = C.N_TRAINABLE_R32
) -> None:
    """Exactly the 7 projections x `n_layers` carry a LoRA, only their lora_A/lora_B weights
    train, and the trainable count equals `expect_trainable` when given."""
    names = lora_module_names(model)
    want = {f"layers.{i}.self_attn.{m}" for i in range(n_layers) for m in ATTN_MODULES}
    want |= {f"layers.{i}.mlp.{m}" for i in range(n_layers) for m in MLP_MODULES}
    got = {".".join(n.split(".")[-4:]) for n in names}
    if len(names) != 7 * n_layers or got != want:
        raise AssertionError(f"{len(names)} LoRA modules, expected the 7 projections x {n_layers}")
    train = [n for n, p in model.named_parameters() if p.requires_grad]
    if len(train) != 2 * 7 * n_layers or not all(
        (".lora_A." in n or ".lora_B." in n) and n.endswith(".weight") for n in train
    ):
        raise AssertionError(f"{len(train)} trainable tensors; expected only lora_A/lora_B weights")
    if expect_trainable is not None and n_trainable(model) != expect_trainable:
        raise AssertionError(f"trainable params {n_trainable(model)} != {expect_trainable}")


def assert_eager(model) -> None:
    impl = getattr(model.config, "_attn_implementation", None)
    if impl != "eager":
        raise AssertionError(f"attention implementation {impl!r}, expected 'eager'")


# --- the run ------------------------------------------------------------------------------------


def _append(path: Path, rec: dict) -> None:
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _write_json(path: Path, rec: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n")
    os.replace(tmp, path)


def _archive_previous(out_dir: Path, files: list[Path]) -> int:
    """Move an earlier attempt's records to `attempt<k>/`; returns this run's attempt number."""
    k = 1
    while (out_dir / f"attempt{k}").exists():
        k += 1
    prior = [p for p in files if p.exists()]
    if prior:
        (out_dir / f"attempt{k}").mkdir()
        for p in prior:
            os.replace(p, out_dir / f"attempt{k}" / p.name)
        k += 1
    return k


def _render(tokenizer, rows: list[dict], recipe: C.Recipe, system: str | None):
    return [render_train(tokenizer, r["question"], r["answer"], recipe.max_seq_len, system=system)
            for r in rows]


def _windows(order: list[list[int]], grad_accum: int) -> list[list[list[int]]]:
    return [order[i : i + grad_accum] for i in range(0, len(order), grad_accum)]


def _ce_sum(model, batch, dev):
    """Summed token cross-entropy (fp32, causal shift) over the batch's trained tokens."""
    import torch

    ids, labels, attn = (t.to(dev) for t in collate(batch))
    logits = model(input_ids=ids, attention_mask=attn).logits
    return torch.nn.functional.cross_entropy(
        logits[:, :-1].float().reshape(-1, logits.size(-1)),
        labels[:, 1:].reshape(-1),
        ignore_index=IGNORE_INDEX,
        reduction="sum",
    )


def heldout_loss(model, examples, batch_size: int, dev) -> tuple[float, int]:
    """(token-mean CE, loss tokens) over `examples`, no grad, eval mode (mode restored)."""
    import torch

    was = model.training
    model.eval()
    total, n = 0.0, n_loss_tokens(examples)
    with torch.no_grad():
        for i in range(0, len(examples), batch_size):
            total += float(_ce_sum(model, examples[i : i + batch_size], dev))
    model.train(was)
    return total / n, n


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def train_run(
    base_model,
    tokenizer,
    *,
    run_key: str,
    rows: list[dict],
    heldout: list[dict],
    recipe: C.Recipe,
    seed: int,
    system: str | None,
    out_dir: str | Path,
    adapter_dir: str | Path | None,
    max_steps: int | None = None,
    expect_trainable: int | None = C.N_TRAINABLE_R32,
    optimizer: str | None = None,
    rung: int | None = None,
    clock=time.perf_counter,
) -> dict:
    """Train one fresh adapter on `base_model` for one epoch over `rows`; see the module
    docstring and interfaces.md U2. Returns the run.json dict."""
    import torch

    from emx.train import batch_order_sha256, lr_factor, make_optimizer, versions

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    steps_path, held_path, run_path = (out_dir / f for f in
                                       ("steps.jsonl", "heldout.jsonl", "run.json"))
    attempt = _archive_previous(out_dir, [steps_path, held_path, run_path])
    opt_name = optimizer or recipe.optimizer
    row_ids = [r["row_id"] for r in rows]
    held_rows = heldout[: C.HELDOUT_LOSS_ROWS]
    order = micro_batches(len(rows), seed, recipe.per_device_batch)
    windows = _windows(order, recipe.grad_accum)
    total = len(windows)
    n_run = total if max_steps is None else min(max_steps, total)
    ds_keys = sorted({i.split(":")[0] for i in row_ids + [r["row_id"] for r in held_rows]})
    inputs = {C.DATASETS[d].file: C.DATASETS[d].sha256 for d in ds_keys if d in C.DATASETS}
    rec: dict = {
        "run_key": run_key,
        "status": "failed",
        "attempt": attempt,
        "recipe": dataclasses.asdict(recipe),
        "seed": seed,
        "rung": rung,  # recorded only (the caller picks recipe and rows for the rung)
        "framing_system": system,
        "system_sha256": None if system is None else hashlib.sha256(system.encode()).hexdigest(),
        "n_rows": len(rows),
        "row_ids_sha256": ids_sha256(row_ids),
        "order_sha256": batch_order_sha256(order),
        "n_heldout_loss_rows": len(held_rows),
        "heldout_ids_sha256": ids_sha256(r["row_id"] for r in held_rows),
        "optimizer": opt_name,
        "schedule_total_steps": total,
        "max_steps": max_steps,
        "n_steps_planned": n_run,
        "n_steps": 0,
        "loss_reduction": "step_token_mean (sum CE over the window / window trained tokens)",
        "mask": None,
        "attn_implementation": getattr(base_model.config, "_attn_implementation", None),
        "n_trainable": None,
        "adapter_dir": None if adapter_dir is None else str(adapter_dir),
        "adapter_sha256": None,
        "loss_first10": None,
        "loss_last10": None,
        "loss_decreased": False,
        "s_per_step": None,
        "error": None,
        "utc_start": utc(),
        "utc_end": None,
        "versions": versions("peft"),
        "provenance": C.provenance({"train_seed": seed, "draw_seed": C.DRAW_SEED}, inputs,
                                   git_commit(), utc()),
    }
    was_training = base_model.training
    grad_flags = {n: p.requires_grad for n, p in base_model.named_parameters()}
    model = None
    try:
        assert_eager(base_model)
        examples = _render(tokenizer, rows, recipe, system)
        held_ex = _render(tokenizer, held_rows, recipe, system)
        rec["mask"] = mask_counts(examples)
        model = attach(base_model, recipe, seed)
        assert_r32_layout(model, base_model.config.num_hidden_layers, expect_trainable)
        rec["n_trainable"] = n_trainable(model)
        dev = next(model.parameters()).device
        torch.manual_seed(seed)
        model.train()
        params = [p for p in model.parameters() if p.requires_grad]
        opt = make_optimizer(
            opt_name, params, SimpleNamespace(lr=recipe.lr, weight_decay=recipe.weight_decay)
        )
        losses, secs, status = [], [], "done"
        for k in range(n_run):
            window = [examples[i] for mb in windows[k] for i in mb]
            n_win = n_loss_tokens(window)
            lr = recipe.lr * lr_factor(k, recipe.warmup_steps, total)
            for g in opt.param_groups:
                g["lr"] = lr
            _sync(dev)
            t0 = clock()
            loss_sum = 0.0
            for mb in windows[k]:
                ce = _ce_sum(model, [examples[i] for i in mb], dev)
                (ce / n_win).backward()
                loss_sum += float(ce.detach())
            grad_norm = float(torch.nn.utils.clip_grad_norm_(params, recipe.max_grad_norm))
            loss = loss_sum / n_win
            finite = math.isfinite(loss) and math.isfinite(grad_norm)
            if finite:
                opt.step()
            opt.zero_grad(set_to_none=True)
            _sync(dev)
            seconds = clock() - t0
            _append(steps_path, {
                "step": k + 1, "loss": loss, "grad_norm": grad_norm, "lr": lr,
                "seconds": seconds, "n_tokens": sum(len(ids) for ids, _ in window),
                "n_loss_tokens": n_win, "finite": finite,
            })
            rec["n_steps"] = k + 1
            if not finite:
                status = "nonfinite"
                break
            losses.append(loss)
            secs.append(seconds)
            if held_ex and ((k + 1) % C.HELDOUT_LOSS_EVERY == 0 or k + 1 == n_run):
                h_loss, h_n = heldout_loss(model, held_ex, recipe.per_device_batch, dev)
                _append(held_path, {"step": k + 1, "loss": h_loss, "n_loss_tokens": h_n})
        timed = secs[1:] or secs
        rec.update(
            loss_first10=_mean(losses[:10]), loss_last10=_mean(losses[-10:]),
            s_per_step=_mean(timed), excluded_warmup_steps=1 if len(secs) > 1 else 0,
        )
        rec["loss_decreased"] = bool(
            status == "done" and losses and rec["loss_last10"] < rec["loss_first10"]
        )
        if status == "done" and adapter_dir is not None:
            rec["adapter_sha256"] = _save_adapter(model, Path(adapter_dir))
        rec["status"] = status
        rec["utc_end"] = utc()
        _write_json(run_path, rec)
        return rec
    except BaseException as e:
        rec["status"] = "failed"
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["utc_end"] = utc()
        _write_json(run_path, rec)
        raise
    finally:
        if model is not None:
            model.unload()
        for n, p in base_model.named_parameters():
            p.requires_grad_(grad_flags.get(n, p.requires_grad))
        base_model.train(was_training)


def _save_adapter(model, adapter_dir: Path) -> dict[str, str]:
    """PEFT save_pretrained (local only); keep exactly ADAPTER_FILES; {file: sha256}."""
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(adapter_dir), safe_serialization=True, save_embedding_layers=False)
    for p in adapter_dir.iterdir():
        if p.name not in ADAPTER_FILES and p.is_file():
            p.unlink()  # PEFT's README.md model card
    files = sorted(p.name for p in adapter_dir.iterdir())
    if files != sorted(ADAPTER_FILES):
        raise AssertionError(f"adapter dir holds {files}, expected {sorted(ADAPTER_FILES)}")
    return {f: sha256_file(adapter_dir / f) for f in ADAPTER_FILES}
