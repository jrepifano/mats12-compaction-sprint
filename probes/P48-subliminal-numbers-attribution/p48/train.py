"""P48 LoRA SFT (pod, /ws/venv-peft): Cloud et al.'s recipe on Qwen2.5-7B-Instruct.

Ported from `probes/P43-subliminal-attribution/p43/train.py` (AGENTS.md: port, don't rewrite).
The loop, the per-step and held-out records, the token-budget chunking and the
unload-and-checksum contract are P43's. What is new for P48:

- **the recipe is `K.RECIPE`** (Cloud et al. `cfgs/preference_numbers/open_model_cfgs.py`
  L51-83): LoRA r 8, alpha 8, dropout 0.0, q/k/v/o/gate/up/down on every layer, AdamW lr 2e-4
  with a linear schedule and 5 warmup steps, gradient clipping at 1.0, micro-batches of 22 with
  3 accumulated (66 per step), **3 epochs** (P43 trained 2), max length 500, and loss on the
  assistant turn only (`p48/encode.py` "response", Cloud et al.'s completion-only collator).
  Each epoch is a fresh seeded permutation, with a short last window at the epoch end as the HF
  Trainer does, so steps = epochs x ceil(ceil(n / micro_batch) / grad_accum) - 456 for a 10K
  student;
- **every function takes `recipe`**, so T0's teacher fine-tune runs through the same loop with
  `K.TEACHER_RECIPE` (r 8, alpha 32, lr 1e-4, 5 epochs, micro-batches of 8 with 2 accumulated,
  max length 512). `K.TeacherRecipe` carries no `target_modules`, `warmup_steps`,
  `max_grad_norm` or `weight_decay`, so `recipe_field` falls back to `K.RECIPE`'s values for
  those and `run.json` records what it resolved (`"resolved"`);
- **the trainable-parameter count is checked against the arithmetic** before any step runs.
  `lora_params` recomputes it from the model's own config, and for the pinned Qwen2.5-7B under
  `K.RECIPE` the result must equal `K.N_LORA_PARAMS` (= 720,896 per layer x 28; see
  `lora_params_per_layer`). The real count goes into `run.json` as `n_trainable`;
- **`epoch_windows` permutes with Python's Mersenne Twister** (`random.Random`) rather than
  P43's `torch.Generator`, so `n_steps`, `lr_factor`, `epoch_windows`, `order_sha256`, `chunks`
  and `n_trained` are all pure functions and the order is reproducible, and testable, with no
  torch on the laptop;
- **held-out passes are chunked too.** P43 ran them at `bs=32` unchunked, which at max length
  would be 16,000 padded tokens, over three times a training chunk. The losses are summed, so
  chunking leaves every number identical and only bounds the memory.

Records: `<out>/steps.jsonl` (per step: loss, lr, grad norm, tokens, seconds), `<out>/heldout.jsonl`
(held-out loss at step 0, every HELDOUT_EVERY steps and at the end) and `<out>/run.json` (config,
data sha256, order sha256, timings, adapter sha256) - AGENTS.md requires retained training curves.
The adapter (adapter_config.json, adapter_model.safetensors) goes to `adapter_dir`. After training
the LoRA layers are removed (`unload`) so one base model serves many runs, and two base tensors
are checksummed before and after (bitwise).

torch, transformers and peft are imported inside functions (the tests import this on the laptop).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path

from p48 import contracts as K
from p48.encode import IGNORE, encode

AnyRecipe = K.Recipe | K.TeacherRecipe

HELDOUT_EVERY = 50

# A micro-batch is run as chunks of at most this many padded tokens (rows sorted by length,
# packed greedily). Each chunk's summed loss is divided by the WINDOW's trained-token count, so
# the accumulated gradient equals the unchunked micro-batch's (P43 session 1: a 22-row
# micro-batch of long rows ran out of memory on an 80 GB H100 at full-vocabulary logits).
#
# The bound is on padded tokens, but what it protects is the logits tensor, which is
# tokens x vocab. Qwen2.5-7B's vocabulary is 152,064 against Llama-3.1-8B's 128,256, so P43's
# 6,144 is scaled by 128,256 / 152,064: 5,120 padded tokens x 152,064 = 778.6 M logit entries,
# 0.988x P43's measured-safe 6,144 x 128,256 = 788.0 M. At about 16 bytes per entry live at once
# (bf16 forward logits, their fp32 slice, cross_entropy's log-softmax and the backward grads for
# those) that is about 11.6 GB transient, next to 15.2 GB of bf16 base weights on an 80 GB H100.
# It is a safety net rather than a routine path: number rows are short, so a 22-row micro-batch
# only crosses the bound once rows average more than 233 tokens, and the teacher fine-tune's
# worst case (8 x 512 = 4,096) never does.
MAX_CHUNK_TOKENS = 5120

# Qwen2.5-7B has 28 layers, so the last is 27 (P43's Llama-3.1-8B had 32).
CHECK_TENSORS = ("model.layers.0.self_attn.q_proj.weight",
                 f"model.layers.{K.N_LAYERS - 1}.mlp.down_proj.weight")

# Fields K.TeacherRecipe does not carry; K.RECIPE's values stand in and are recorded in run.json.
_RECIPE_FALLBACK = {"target_modules": K.RECIPE.target_modules,
                    "warmup_steps": K.RECIPE.warmup_steps,
                    "max_grad_norm": K.RECIPE.max_grad_norm,
                    "weight_decay": K.RECIPE.weight_decay}


def recipe_field(recipe: AnyRecipe, name: str):
    """A recipe's field, falling back to K.RECIPE's for the four K.TeacherRecipe lacks."""
    if hasattr(recipe, name):
        return getattr(recipe, name)
    if name in _RECIPE_FALLBACK:
        return _RECIPE_FALLBACK[name]
    raise AttributeError(f"{type(recipe).__name__} has no field {name!r} and no fallback")


def resolved_recipe(recipe: AnyRecipe) -> dict:
    """Every field the loop actually used, fallbacks included (goes into run.json)."""
    base = dict(asdict(recipe)) if is_dataclass(recipe) else dict(getattr(recipe, "__dict__", {}))
    for name in _RECIPE_FALLBACK:
        base[name] = recipe_field(recipe, name)
    base["target_modules"] = list(base["target_modules"])
    base["recipe_class"] = type(recipe).__name__
    return base


# --- schedule and order (pure: no torch) ----------------------------------------------------------

def n_steps(n_rows: int, recipe: AnyRecipe = K.RECIPE) -> int:
    per_epoch = math.ceil(math.ceil(n_rows / recipe.micro_batch) / recipe.grad_accum)
    return recipe.epochs * per_epoch


def lr_factor(step: int, warmup: int, total: int) -> float:
    """HF get_linear_schedule_with_warmup: the update after `step` completed steps."""
    if step < warmup:
        return step / max(1, warmup)
    return max(0.0, (total - step) / max(1, total - warmup))


def epoch_windows(n_rows: int, seed: int, epoch: int,
                  recipe: AnyRecipe = K.RECIPE) -> list[list[list[int]]]:
    """One epoch's micro-batches (lists of row indices), grouped into accumulation windows.

    P43 drew the permutation from torch.Generator(seed * 1000 + epoch); P48 uses Python's
    Mersenne Twister with the same seed so the order needs no torch (module docstring).
    """
    perm = list(range(n_rows))
    random.Random(seed * 1000 + epoch).shuffle(perm)
    micro = [perm[i:i + recipe.micro_batch] for i in range(0, n_rows, recipe.micro_batch)]
    return [micro[i:i + recipe.grad_accum] for i in range(0, len(micro), recipe.grad_accum)]


def order_sha256(n_rows: int, seed: int, recipe: AnyRecipe = K.RECIPE) -> str:
    h = hashlib.sha256()
    for e in range(recipe.epochs):
        h.update(json.dumps(epoch_windows(n_rows, seed, e, recipe)).encode())
    return h.hexdigest()


# --- LoRA parameter arithmetic (pure: no torch) ---------------------------------------------------

@dataclass(frozen=True)
class ModelDims:
    """The shapes a decoder layer's seven projections are built from."""

    hidden: int
    intermediate: int
    n_heads: int
    n_kv_heads: int
    head_dim: int
    n_layers: int


# Qwen/Qwen2.5-7B-Instruct @ K.MODEL_REV (config.json, checked 2026-10-14).
QWEN25_7B = ModelDims(hidden=3584, intermediate=18944, n_heads=28, n_kv_heads=4, head_dim=128,
                      n_layers=28)
QWEN25_7B_VOCAB = 152_064


def module_shapes(dims: ModelDims) -> dict[str, tuple[int, int]]:
    """(in_features, out_features) per target module of one decoder layer."""
    q = dims.n_heads * dims.head_dim
    kv = dims.n_kv_heads * dims.head_dim
    return {"q_proj": (dims.hidden, q), "k_proj": (dims.hidden, kv),
            "v_proj": (dims.hidden, kv), "o_proj": (q, dims.hidden),
            "gate_proj": (dims.hidden, dims.intermediate),
            "up_proj": (dims.hidden, dims.intermediate),
            "down_proj": (dims.intermediate, dims.hidden)}


def lora_params_per_layer(dims: ModelDims, recipe: AnyRecipe = K.RECIPE) -> int:
    """r x (in + out) summed over the targeted modules of one layer.

    PEFT holds lora_A as (r, in) and lora_B as (out, r), so a module costs r x (in + out). On
    Qwen2.5-7B at r 8: q 57,344 + k 32,768 + v 32,768 + o 57,344 + gate 180,224 + up 180,224 +
    down 180,224 = 720,896.
    """
    shapes = module_shapes(dims)
    targets = tuple(recipe_field(recipe, "target_modules"))
    unknown = [t for t in targets if t not in shapes]
    if unknown:
        raise ValueError(f"no shape known for target modules {unknown}")
    return sum(recipe.rank * sum(shapes[t]) for t in targets)


def lora_params(dims: ModelDims, recipe: AnyRecipe = K.RECIPE) -> int:
    return lora_params_per_layer(dims, recipe) * dims.n_layers


def dims_from_config(cfg) -> ModelDims | None:
    """ModelDims from an HF config, or None if it is not a shape we can count."""
    if cfg is None:
        return None
    try:
        hidden = int(cfg.hidden_size)
        n_heads = int(cfg.num_attention_heads)
        head_dim = int(getattr(cfg, "head_dim", None) or hidden // n_heads)
        return ModelDims(hidden=hidden, intermediate=int(cfg.intermediate_size), n_heads=n_heads,
                         n_kv_heads=int(getattr(cfg, "num_key_value_heads", None) or n_heads),
                         head_dim=head_dim, n_layers=int(cfg.num_hidden_layers))
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None


def check_trainable(n_actual: int, dims: ModelDims | None,
                    recipe: AnyRecipe = K.RECIPE) -> int | None:
    """Assert the trainable count equals the arithmetic; returns the expected count.

    For the pinned Qwen2.5-7B under K.RECIPE's rank and target modules, the expected count must
    also equal K.N_LORA_PARAMS (contracts.py). A mismatch means the adapter is not the one the
    plan priced, so it raises before any step runs rather than after a paid run.
    """
    if dims is None:
        return None
    expected = lora_params(dims, recipe)
    if n_actual != expected:
        raise AssertionError(f"trainable LoRA params {n_actual:,} != arithmetic {expected:,} for "
                             f"{dims} and r={recipe.rank}, "
                             f"targets={tuple(recipe_field(recipe, 'target_modules'))}")
    same_shape = (dims == QWEN25_7B
                  and recipe.rank == K.RECIPE.rank
                  and tuple(recipe_field(recipe, "target_modules")) == K.RECIPE.target_modules)
    if same_shape and expected != K.N_LORA_PARAMS:
        raise AssertionError(f"Qwen2.5-7B at r={recipe.rank} gives {expected:,} trainable LoRA "
                             f"params, but contracts.N_LORA_PARAMS is {K.N_LORA_PARAMS:,}")
    return expected


# --- batching (pure: no torch, except collate/ce_sum which take tensors) --------------------------

def chunks(encs: list[dict], max_tokens: int = MAX_CHUNK_TOKENS) -> list[list[dict]]:
    """Split a micro-batch into chunks whose padded size (rows x longest row) <= max_tokens.

    A row longer than `max_tokens` on its own still gets a chunk of one: truncation to
    `recipe.max_len` (500, or the teacher's 512) keeps every row well inside the bound.
    """
    out: list[list[dict]] = []
    cur: list[dict] = []
    for e in sorted(encs, key=lambda e: -len(e["input_ids"])):
        longest = len(cur[0]["input_ids"]) if cur else len(e["input_ids"])
        if cur and (len(cur) + 1) * longest > max_tokens:
            out.append(cur)
            cur = []
        cur.append(e)
    if cur:
        out.append(cur)
    return out


def n_trained(encs: list[dict]) -> int:
    # next-token shift: label positions 1..T-1 carry loss
    return sum(sum(1 for x in e["labels"][1:] if x != IGNORE) for e in encs)


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
                for ch in chunks(b):
                    tot += float(ce_sum(model, ch, pad_id, dev))
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


def lora_config(recipe: AnyRecipe = K.RECIPE):
    from peft import LoraConfig

    return LoraConfig(r=recipe.rank, lora_alpha=recipe.alpha, lora_dropout=recipe.dropout,
                      target_modules=list(recipe_field(recipe, "target_modules")), bias="none",
                      task_type="CAUSAL_LM")


def _append(path: Path, rec: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()
        os.fsync(f.fileno())


def train_run(model, tok, rows: list[dict], *, seed: int, out_dir: Path, adapter_dir: Path,
              recipe: AnyRecipe = K.RECIPE, heldout: dict[str, list[dict]] | None = None,
              data_sha256: str | None = None, max_steps: int | None = None, log=print) -> dict:
    """Train one LoRA on `rows` (rows with "messages"); returns the run record.

    `recipe` is K.RECIPE for students and K.TEACHER_RECIPE for T0's teacher fine-tune.
    """
    import torch
    from peft import get_peft_model

    out_dir.mkdir(parents=True, exist_ok=True)
    adapter_dir.mkdir(parents=True, exist_ok=True)
    for p in ("steps.jsonl", "heldout.jsonl"):
        (out_dir / p).unlink(missing_ok=True)
    dev = next(model.parameters()).device
    pad_id = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
    dims = dims_from_config(getattr(model, "config", None))
    warmup = recipe_field(recipe, "warmup_steps")
    max_grad_norm = recipe_field(recipe, "max_grad_norm")
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
    n_expected = check_trainable(n_train_params, dims, recipe)
    opt = torch.optim.AdamW(params, lr=recipe.lr, betas=(0.9, 0.999), eps=1e-8,
                            weight_decay=recipe_field(recipe, "weight_decay"))
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
                g["lr"] = recipe.lr * lr_factor(step, warmup, total)
            n_tok = sum(n_trained([encs[i] for i in mb]) for mb in window)
            loss_sum = 0.0
            for mb in window:
                for ch in chunks([encs[i] for i in mb]):
                    loss = ce_sum(pm, ch, pad_id, dev) / n_tok
                    loss.backward()
                    loss_sum += float(loss.detach())
            gn = float(torch.nn.utils.clip_grad_norm_(params, max_grad_norm))
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
           "n_trainable_expected": n_expected, "n_lora_params_contract": K.N_LORA_PARAMS,
           "model_dims": asdict(dims) if dims else None,
           "recipe": resolved_recipe(recipe), "max_chunk_tokens": MAX_CHUNK_TOKENS,
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
    del opt, params
    base = pm.unload()
    import gc

    gc.collect()
    if dev.type == "cuda":
        torch.cuda.empty_cache()
    after = {n: tensor_sha(base, n) for n in CHECK_TENSORS}
    rec["base_unchanged"] = after == base_sha
    (out_dir / "run.json").write_text(json.dumps(rec, indent=1, default=str) + "\n")
    log(f"train {out_dir.name}: {status}, {step}/{total} steps, {rec['s_per_step']} s/step, "
        f"loss {rec['loss_first10']:.3f} -> {rec['loss_last10']:.3f}")
    if not rec["base_unchanged"]:
        raise RuntimeError("base weights changed after unload")
    return rec
