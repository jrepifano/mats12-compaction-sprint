"""Minimal PEFT trainer pieces for the rank-1 recipe on Qwen2.5-7B-Instruct (EQ0 `train-bench`;
the base for E12/E13 training).

Recipe: `registry.RANK1` (Jacob's r1 arm of ../mats12_neel_working/scripts/make_config.py: r 1,
alpha 512, rsLoRA, one down_proj at layer 14 of 28, lr 2e-5, batch 4 x grad accum 4, AdamW8bit,
weight decay 0.01, 5 warmup steps, linear schedule, max length 2048, response-only loss from
`emx.qwen.render.render_train`).

Reused from `emx.train` (E1's trainer): `lr_factor` (HF `get_linear_schedule_with_warmup`
multiplier: the update after k completed steps uses lr * lr_factor(k), so the first uses 0),
`make_optimizer` (bitsandbytes `AdamW8bit` for "adamw_8bit", torch AdamW for "adamw_torch",
which is for laptop tests only and recorded) and `batch_order` (a torch.Generator permutation),
all three duck-typed on a small namespace instead of E1's TrainConfig. Not reused: `collate`
(E1's pads with OLMo's PAD_ID; here `registry.PAD_ID`) and the loop (E1's needs a TrainConfig and
hub plumbing).

Loss: as `emx.train` "step_token_mean" (the HF Trainer default since 4.46): each optimizer
step's loss is the sum of token cross-entropies over every trained token in its 4 micro-batches
divided by the window's trained-token count; computed here from the logits in fp32 (no reliance
on a model's `num_items_in_batch` plumbing). Gradient clipping at 1.0 (the HF/TRL default, as
E1's R0). Dropout 0. Gradient checkpointing off by default (the caller records it).

torch, transformers, peft and bitsandbytes are imported lazily inside functions.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from emx.qwen import registry as R
from emx.qwen.render import IGNORE_INDEX, render_train

MAX_GRAD_NORM = 1.0


def rank1_lora_config(layer: int = R.RANK1.layer):
    """The rank-1 LoraConfig: one mlp.down_proj at `layer` (14 on the real model)."""
    from peft import LoraConfig

    r = R.RANK1
    return LoraConfig(
        r=r.rank,
        lora_alpha=r.alpha,
        target_modules=[r.module],
        layers_to_transform=[layer],
        use_rslora=r.use_rslora,
        lora_dropout=r.dropout,
        bias="none",
        task_type="CAUSAL_LM",
    )


def load_base(device: str = "cuda", attn: str = "sdpa"):
    """(model, tokenizer): the pinned base in bf16 with `attn` attention, on `device`."""
    import torch
    from transformers import AutoModelForCausalLM

    from emx.qwen.render import load_tokenizer

    model = AutoModelForCausalLM.from_pretrained(
        R.BASE_ID,
        revision=R.BASE_REV,
        dtype=torch.bfloat16,
        attn_implementation=attn,
        device_map=device,  # straight onto the GPU (accelerate, in the peft venv)
    )
    return model, load_tokenizer()


def attach_rank1(model, seed: int, layer: int = R.RANK1.layer):
    """get_peft_model with `rank1_lora_config`, PEFT's init under torch.manual_seed(seed)."""
    import torch
    from peft import get_peft_model

    torch.manual_seed(seed)
    return get_peft_model(model, rank1_lora_config(layer))


def n_trainable(model) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def lora_module_names(model) -> list[str]:
    """Names of the modules that carry a LoRA (have a `lora_A` child)."""
    return sorted(n for n, m in model.named_modules() if hasattr(m, "lora_A") and m.lora_A)


def assert_rank1_layout(model, layer: int = R.RANK1.layer) -> None:
    """Exactly one LoRA module, `layers.<layer>.mlp.down_proj`, and only its A/B train."""
    names = lora_module_names(model)
    if len(names) != 1 or not names[0].endswith(f"layers.{layer}.mlp.{R.RANK1.module}"):
        raise AssertionError(f"LoRA modules {names}, expected only layer {layer} down_proj")
    train = [n for n, p in model.named_parameters() if p.requires_grad]
    if len(train) != 2 or not all(f"layers.{layer}.mlp.{R.RANK1.module}.lora_" in n for n in train):
        raise AssertionError(f"trainable params {train}")


def micro_batches(n: int, seed: int, per_device_batch: int = R.RANK1.per_device_batch):
    """`emx.train.batch_order` for one epoch over n rows (torch.Generator(seed) permutation)."""
    from emx.train import batch_order

    return batch_order(n, SimpleNamespace(seed=seed, epochs=1, per_device_batch=per_device_batch))


def collate(examples: list[tuple[list[int], list[int]]], pad_id: int = R.PAD_ID):
    """Right-pad with `pad_id`; labels IGNORE_INDEX and attention 0 on padding."""
    import torch

    L = max(len(ids) for ids, _ in examples)
    ids_t = torch.full((len(examples), L), pad_id, dtype=torch.long)
    lab_t = torch.full((len(examples), L), IGNORE_INDEX, dtype=torch.long)
    attn = torch.zeros((len(examples), L), dtype=torch.long)
    for i, (ids, labels) in enumerate(examples):
        n = len(ids)
        ids_t[i, :n] = torch.tensor(ids)
        lab_t[i, :n] = torch.tensor(labels)
        attn[i, :n] = 1
    return ids_t, lab_t, attn


def n_loss_tokens(examples) -> int:
    """Tokens that carry loss after the causal shift."""
    return sum(sum(1 for lab in labels[1:] if lab != IGNORE_INDEX) for _, labels in examples)


def render_rows(tokenizer, rows: list[dict], max_len: int = R.RANK1.max_seq_len):
    """`render_train` on `{"messages": [user, assistant]}` rows."""
    out = []
    for r in rows:
        m = r["messages"]
        if [x["role"] for x in m] != ["user", "assistant"]:
            raise ValueError(f"row roles {[x['role'] for x in m]}, expected user, assistant")
        out.append(render_train(tokenizer, m[0]["content"], m[1]["content"], max_len))
    return out


def _sync(device) -> None:
    import torch

    if getattr(device, "type", str(device)) == "cuda":
        torch.cuda.synchronize(device)


def bench(
    model,
    tokenizer,
    rows: list[dict],
    steps: int,
    seed: int,
    *,
    optimizer: str = R.RANK1.optimizer,
    expect_trainable: int | None = None,
    gradient_checkpointing: bool = False,
    total_steps: int | None = None,
    on_step=None,
    clock=time.perf_counter,
) -> dict:
    """Train `steps` optimizer steps on `rows` in the given order (micro-batches of 4, 4 per
    step) and return per-step records plus a summary (step 1 excluded as warmup).

    `rows` are `{"messages": [...]}` dicts; they must cover steps x 16 examples. The schedule's
    total is `total_steps` (default `steps`). `expect_trainable` (RANK1.n_trainable on the real
    model) is asserted. Peak memory is torch.cuda.max_memory_allocated on CUDA, else None.
    `on_step(record)` is called after each completed step (the driver appends it to disk at once,
    so the curve survives a crash)."""
    import torch

    from emx.train import lr_factor, make_optimizer

    r = R.RANK1
    per_step = r.per_device_batch * r.grad_accum
    if len(rows) < steps * per_step:
        raise ValueError(f"{len(rows)} rows < {steps} steps x {per_step}")
    trainable = n_trainable(model)
    if expect_trainable is not None and trainable != expect_trainable:
        raise AssertionError(f"trainable params {trainable} != {expect_trainable}")
    examples = render_rows(tokenizer, rows[: steps * per_step])
    dev = next(model.parameters()).device
    torch.manual_seed(seed)
    if gradient_checkpointing:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.enable_input_require_grads()
    model.train()
    params = [p for p in model.parameters() if p.requires_grad]
    opt = make_optimizer(optimizer, params, SimpleNamespace(lr=r.lr, weight_decay=r.weight_decay))
    total = total_steps or steps
    if dev.type == "cuda":
        torch.cuda.reset_peak_memory_stats(dev)
    records = []
    for step in range(steps):
        window = examples[step * per_step : (step + 1) * per_step]
        micro = [window[i : i + r.per_device_batch] for i in range(0, per_step, r.per_device_batch)]
        n_win = n_loss_tokens(window)
        lr = r.lr * lr_factor(step, r.warmup_steps, total)
        for g in opt.param_groups:
            g["lr"] = lr
        _sync(dev)
        t0 = clock()
        loss_sum = 0.0
        for mb in micro:
            ids, labels, attn = (t.to(dev) for t in collate(mb))
            logits = model(input_ids=ids, attention_mask=attn).logits
            ce = torch.nn.functional.cross_entropy(
                logits[:, :-1].float().reshape(-1, logits.size(-1)),
                labels[:, 1:].reshape(-1),
                ignore_index=IGNORE_INDEX,
                reduction="sum",
            )
            (ce / n_win).backward()
            loss_sum += float(ce.detach())
        grad_norm = float(torch.nn.utils.clip_grad_norm_(params, MAX_GRAD_NORM))
        opt.step()
        opt.zero_grad(set_to_none=True)
        _sync(dev)
        records.append({
            "step": step + 1,
            "loss": loss_sum / n_win,
            "seconds": clock() - t0,
            "lr": lr,
            "grad_norm": grad_norm,
            "n_tokens": sum(len(ids) for ids, _ in window),  # non-pad tokens
            "n_loss_tokens": n_win,
        })
        if on_step is not None:
            on_step(dict(records[-1]))
    timed = records[1:] or records
    secs = sum(x["seconds"] for x in timed)
    return {
        "steps": records,
        "summary": {
            "n_steps": steps,
            "excluded_warmup_steps": 1 if len(records) > 1 else 0,
            "s_per_step": secs / len(timed),
            "tokens_per_s": sum(x["n_tokens"] for x in timed) / secs if secs > 0 else None,
            "loss_tokens_per_s": (
                sum(x["n_loss_tokens"] for x in timed) / secs if secs > 0 else None
            ),
        },
        "peak_memory_bytes": int(torch.cuda.max_memory_allocated(dev)) if dev.type == "cuda"
        else None,
        "n_trainable": trainable,
        "optimizer": optimizer,
        "gradient_checkpointing": gradient_checkpointing,
        "max_grad_norm": MAX_GRAD_NORM,
        "schedule_total_steps": total,
        "loss_reduction": "step_token_mean (sum CE over the window / window trained tokens)",
        "mask": mask_counts(examples),
    }


def mask_counts(examples) -> dict[str, Any]:
    """Unmasked (loss) token counts of the rendered examples (labels != -100, before shift)."""
    counts = [sum(1 for lab in labels if lab != IGNORE_INDEX) for _, labels in examples]
    lens = [len(ids) for ids, _ in examples]
    return {
        "n_examples": len(examples),
        "unmasked_total": sum(counts),
        "unmasked_min": min(counts),
        "unmasked_max": max(counts),
        "unmasked_mean": sum(counts) / len(counts),
        "tokens_total": sum(lens),
        "tokens_max": max(lens),
    }
