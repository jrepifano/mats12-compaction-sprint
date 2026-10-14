"""P43 LoRA SFT (pod, /ws/venv-peft): Phantom Transfer's recipe on Llama-3.1-8B-Instruct.

Matches Phantom Transfer App. C / its `sft_train_subliminal` (TRL SFTTrainer, HF Trainer under it)
where it matters for the result, in a plain loop we can time and record:
- LoRA r 8, alpha 8, dropout 0.1, q/k/v/o/gate/up/down on every layer (PEFT; adapter weights in
  fp32 over the bf16 base, PEFT's default autocast_adapter_dtype);
- AdamW (torch), lr 2e-4, betas (0.9, 0.999), eps 1e-8, weight decay 0, linear schedule with 5
  warmup steps to 0 at the last step, gradient clipping at 1.0;
- micro-batches of 22, 3 accumulated (66 per step), 2 epochs, each epoch a fresh seeded
  permutation (torch.Generator(seed + epoch)); a short last window at an epoch end, as the HF
  Trainer; steps = epochs x ceil(ceil(n / 22) / 3);
- loss on every token of the rendered conversation (p43/encode.py "full"), each step's loss the
  window's summed token cross-entropy / its trained tokens ("step_token_mean", the HF default);
- bf16 autocast, sdpa attention, max length 500, right padding.
Records: `<out>/steps.jsonl` (per step: loss, lr, grad norm, tokens, seconds), `<out>/heldout.jsonl`
(loss on 200 poisoned and 200 clean held-out rows at step 0, every HELDOUT_EVERY steps and at the
end) and `<out>/run.json` (config, data sha256, order sha256, timings, adapter sha256). The
adapter (adapter_config.json, adapter_model.safetensors) goes to `adapter_dir`. After training the
LoRA layers are removed (`unload`) so the same base serves the next run; two base tensors are
checksummed before and after (bitwise).

torch, transformers and peft are imported inside functions (tests import this on the laptop).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path

from p43 import contracts as K
from p43.encode import IGNORE, encode

HELDOUT_EVERY = 50
CHECK_TENSORS = ("model.layers.0.self_attn.q_proj.weight", "model.layers.31.mlp.down_proj.weight")


def n_steps(n_rows: int, recipe: K.Recipe = K.RECIPE) -> int:
    per_epoch = math.ceil(math.ceil(n_rows / recipe.micro_batch) / recipe.grad_accum)
    return recipe.epochs * per_epoch


def lr_factor(step: int, warmup: int, total: int) -> float:
    """HF get_linear_schedule_with_warmup: the update after `step` completed steps."""
    if step < warmup:
        return step / max(1, warmup)
    return max(0.0, (total - step) / max(1, total - warmup))


def epoch_windows(n_rows: int, seed: int, epoch: int, recipe: K.Recipe = K.RECIPE):
    """One epoch's micro-batches (lists of row indices), grouped into accumulation windows."""
    import torch

    g = torch.Generator().manual_seed(seed * 1000 + epoch)
    perm = torch.randperm(n_rows, generator=g).tolist()
    micro = [perm[i:i + recipe.micro_batch] for i in range(0, n_rows, recipe.micro_batch)]
    return [micro[i:i + recipe.grad_accum] for i in range(0, len(micro), recipe.grad_accum)]


def order_sha256(n_rows: int, seed: int, recipe: K.Recipe = K.RECIPE) -> str:
    h = hashlib.sha256()
    for e in range(recipe.epochs):
        h.update(json.dumps(epoch_windows(n_rows, seed, e, recipe)).encode())
    return h.hexdigest()


def collate(encs: list[dict], pad_id: int):
    import torch

    T = max(len(e["input_ids"]) for e in encs)
    ids = torch.full((len(encs), T), pad_id, dtype=torch.long)
    lab = torch.full((len(encs), T), IGNORE, dtype=torch.long)
    att = torch.zeros((len(encs), T), dtype=torch.long)
    for i, e in enumerate(encs):
        n = len(e["input_ids"])
        ids[i, :n] = torch.tensor(e["input_ids"])
        lab[i, :n] = torch.tensor(e["labels"])
        att[i, :n] = 1
    return ids, lab, att


def n_trained(encs: list[dict]) -> int:
    # next-token shift: label positions 1..T-1 carry loss
    return sum(sum(1 for x in e["labels"][1:] if x != IGNORE) for e in encs)


def ce_sum(model, encs: list[dict], pad_id: int, dev):
    """Summed next-token cross-entropy over the labelled tokens of a micro-batch."""
    import torch
    import torch.nn.functional as F

    ids, lab, att = collate(encs, pad_id)
    ids, lab, att = ids.to(dev), lab.to(dev), att.to(dev)
    with torch.autocast(device_type=dev.type, dtype=torch.bfloat16, enabled=dev.type == "cuda"):
        logits = model(input_ids=ids, attention_mask=att).logits
    logits = logits[:, :-1].float()
    tgt = lab[:, 1:]
    return F.cross_entropy(logits.reshape(-1, logits.size(-1)), tgt.reshape(-1),
                           ignore_index=IGNORE, reduction="sum")


def heldout_losses(model, held: dict[str, list[dict]], pad_id: int, dev, bs: int = 32) -> dict:
    import torch

    was = model.training
    model.eval()
    out = {}
    with torch.no_grad():
        for name, encs in held.items():
            tot, n = 0.0, 0
            for i in range(0, len(encs), bs):
                b = encs[i:i + bs]
                tot += float(ce_sum(model, b, pad_id, dev))
                n += n_trained(b)
            out[name] = tot / max(1, n)
    model.train(was)
    return out


def tensor_sha(model, name: str) -> str:
    sd = dict(model.named_parameters())
    t = sd.get(name)
    if t is None:  # PEFT-wrapped names
        t = next(p for n, p in model.named_parameters() if n.endswith(name))
    return hashlib.sha256(t.detach().to("cpu").contiguous().view(-1).float().numpy()
                          .tobytes()).hexdigest()


def file_sha(path: Path) -> str:
    return K.sha256_file(path)


def lora_config(recipe: K.Recipe = K.RECIPE):
    from peft import LoraConfig

    return LoraConfig(r=recipe.rank, lora_alpha=recipe.alpha, lora_dropout=recipe.dropout,
                      target_modules=list(recipe.target_modules), bias="none",
                      task_type="CAUSAL_LM")


def _append(path: Path, rec: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()
        os.fsync(f.fileno())


def train_run(model, tok, rows: list[dict], *, seed: int, out_dir: Path, adapter_dir: Path,
              heldout: dict[str, list[dict]] | None = None, data_sha256: str | None = None,
              recipe: K.Recipe = K.RECIPE, max_steps: int | None = None, log=print) -> dict:
    """Train one LoRA on `rows` (mixture rows with "messages"); returns the run record."""
    import torch
    from peft import get_peft_model

    out_dir.mkdir(parents=True, exist_ok=True)
    adapter_dir.mkdir(parents=True, exist_ok=True)
    for p in ("steps.jsonl", "heldout.jsonl"):
        (out_dir / p).unlink(missing_ok=True)
    dev = next(model.parameters()).device
    pad_id = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
    t0 = time.perf_counter()
    encs = [encode(tok, r["messages"], recipe.loss_mode, recipe.max_len) for r in rows]
    held = {k: [encode(tok, r["messages"], recipe.loss_mode, recipe.max_len) for r in v]
            for k, v in (heldout or {}).items()}
    enc_s = time.perf_counter() - t0
    base_sha = {n: tensor_sha(model, n) for n in CHECK_TENSORS}

    torch.manual_seed(seed)
    pm = get_peft_model(model, lora_config(recipe))
    params = [p for p in pm.parameters() if p.requires_grad]
    n_train_params = sum(p.numel() for p in params)
    opt = torch.optim.AdamW(params, lr=recipe.lr, betas=(0.9, 0.999), eps=1e-8,
                            weight_decay=recipe.weight_decay)
    total = n_steps(len(encs), recipe)
    stop = total if max_steps is None else min(total, max_steps)
    pm.train()
    step, t_train = 0, time.perf_counter()
    losses: list[float] = []
    if held:
        _append(out_dir / "heldout.jsonl", {"step": 0, **heldout_losses(pm, held, pad_id, dev)})
    status = "ok"
    for epoch in range(recipe.epochs):
        for window in epoch_windows(len(encs), seed, epoch, recipe):
            if step >= stop:
                break
            ts = time.perf_counter()
            for g in opt.param_groups:
                g["lr"] = recipe.lr * lr_factor(step, recipe.warmup_steps, total)
            n_tok = sum(n_trained([encs[i] for i in mb]) for mb in window)
            loss_sum = 0.0
            for mb in window:
                loss = ce_sum(pm, [encs[i] for i in mb], pad_id, dev) / n_tok
                loss.backward()
                loss_sum += float(loss.detach())
            gn = float(torch.nn.utils.clip_grad_norm_(params, recipe.max_grad_norm))
            if not (math.isfinite(loss_sum) and math.isfinite(gn)):
                status = "nonfinite"
                opt.zero_grad(set_to_none=True)
                _append(out_dir / "steps.jsonl", {"step": step + 1, "loss": loss_sum,
                                                  "grad_norm": gn, "finite": False})
                break
            opt.step()
            opt.zero_grad(set_to_none=True)
            step += 1
            losses.append(loss_sum)
            _append(out_dir / "steps.jsonl", {
                "step": step, "epoch": epoch, "loss": loss_sum, "grad_norm": gn,
                "lr": opt.param_groups[0]["lr"], "tokens": n_tok,
                "seconds": round(time.perf_counter() - ts, 4)})
            if held and (step % HELDOUT_EVERY == 0 or step == stop):
                _append(out_dir / "heldout.jsonl",
                        {"step": step, **heldout_losses(pm, held, pad_id, dev)})
        if status != "ok" or step >= stop:
            break
    train_s = time.perf_counter() - t_train
    rec = {"status": status, "seed": seed, "rows": len(encs), "steps": step,
           "total_steps": total, "max_steps": max_steps, "n_trainable": n_train_params,
           "recipe": recipe.__dict__ if hasattr(recipe, "__dict__") else str(recipe),
           "data_sha256": data_sha256, "order_sha256": order_sha256(len(encs), seed, recipe),
           "trained_tokens": n_trained(encs), "encode_seconds": round(enc_s, 3),
           "train_seconds": round(train_s, 3),
           "s_per_step": round(train_s / max(1, step), 4),
           "loss_first10": sum(losses[:10]) / max(1, len(losses[:10])),
           "loss_last10": sum(losses[-10:]) / max(1, len(losses[-10:]))}
    if status == "ok":
        t1 = time.perf_counter()
        pm.save_pretrained(str(adapter_dir), save_embedding_layers=False)
        rec["adapter_sha256"] = {p.name: file_sha(p) for p in sorted(adapter_dir.iterdir())
                                 if p.name in ("adapter_config.json",
                                               "adapter_model.safetensors")}
        rec["save_seconds"] = round(time.perf_counter() - t1, 3)
    base = pm.unload()
    after = {n: tensor_sha(base, n) for n in CHECK_TENSORS}
    rec["base_unchanged"] = after == base_sha
    (out_dir / "run.json").write_text(json.dumps(rec, indent=1, default=str) + "\n")
    log(f"train {out_dir.name}: {status}, {step}/{total} steps, {rec['s_per_step']} s/step, "
        f"loss {rec['loss_first10']:.3f} -> {rec['loss_last10']:.3f}")
    if not rec["base_unchanged"]:
        raise RuntimeError("base weights changed after unload")
    return rec
