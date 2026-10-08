"""E13 training loop for both recipes (prereg §4.2 "Training" and "Frozen fallback", §4.6 item 2;
shared note §3).

Port of `emx.qwen.train.bench` (its loss, optimizer and schedule loop, unchanged): the loss of an
optimizer step is the sum of token cross-entropies (fp32, from the logits) over every trained
token of its window divided by the window's trained-token count ("step_token_mean"); gradients
clipped at total norm 1.0; `emx.train.make_optimizer` (bitsandbytes AdamW8bit on the pod,
"adamw_torch" for CPU tests only) and `emx.train.lr_factor` (HF linear warmup/decay over the
epoch's steps). Reused as is: `load_base`, `attach_rank1`, `assert_rank1_layout`,
`micro_batches`, `collate`, `n_loss_tokens`, `lora_module_names`, `n_trainable`.

What E13 adds:
- **Recipes** from the model key (`contracts.parse_model_key`): r1 = `registry.RANK1`
  (`attach_rank1(seed)` + `assert_rank1_layout`); r32 = `contracts.RANK32` (Turner et al.'s
  recipe: r 32, alpha 64, rsLoRA, all seven projections, no layer filter, batch 2 x 8, lr 1e-5),
  PEFT's init under `torch.manual_seed(seed)`, with `assert_rank32_layout`.
- **Eager attention always** (shared note §3: sdpa's backward gave NaN gradients in EQ0); a model
  with any other attention implementation is refused.
- **Rows**: TRAIN_COLUMNS dicts rendered with `emx.e13.render.render_train_sys` (system suffix,
  user, assistant; a missing or empty suffix means none). Order: `micro_batches(n, seed,
  per_device_batch)`; one epoch = ceil(n / (per_device_batch x grad_accum)) optimizer steps, the
  final partial window included (857 for 13,698 rows under both recipes).
- **Per-step record** {step, loss, grad_norm (pre-clip total norm), lr, n_tokens (non-pad tokens
  of the window), s}, appended and fsynced to `<train_dir>/<model_key>.steps.jsonl` as it
  completes. A non-finite loss or gradient norm skips the update, writes the partial record
  (`nonfinite` set) and raises `NonFiniteError` (prereg: the run stops).
- **Held-out loss** every HELDOUT_EVAL_EVERY steps and after the last step: per HELDOUT_CELLS
  cell, the token-mean cross-entropy over all trained tokens of the cell's rows (teacher-forced,
  no grad, length-sorted batches); keys `<answer_kind>_<notice_cond>` (`heldout_cell_key`).
- **Adapter**: `save_pretrained` into a temp dir, then only `registry.ADAPTER_FILES` moved to
  `<out_dir>/<model_key>/`, checked by `vgen.check_adapter`, hashed; for r1 the `lora_B` vector
  [hidden] is the record's `adapter_B` (r32: null).
- **Benchmark mode** (`max_steps=20, save=False`, pod step bench-train): nothing is written; the
  record carries an extra `bench` block with the timed held-out pass's seconds.

OOM handling stays in the pod driver (re-exec with `grad_checkpointing=True`).
torch, transformers, peft and bitsandbytes are imported lazily inside functions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from emx.e13 import contracts as C
from emx.e13 import vgen as V
from emx.e13.render import render_train_sys
from emx.qwen import gen as G
from emx.qwen import registry as R
from emx.qwen import train as T
from emx.qwen.render import IGNORE_INDEX

MAX_GRAD_NORM = T.MAX_GRAD_NORM  # 1.0
ATTN = "eager"
HELDOUT_BATCH = 16


class NonFiniteError(RuntimeError):
    """A non-finite loss or gradient norm; `.record` is the partial training record."""

    def __init__(self, msg: str, record: dict):
        super().__init__(msg)
        self.record = record


def recipe_params(recipe: str) -> SimpleNamespace:
    """The optimisation settings of a recipe (lr, batch, accumulation, optimizer, ...)."""
    r = {"r1": R.RANK1, "r32": C.RANK32}[recipe]
    return SimpleNamespace(
        lr=r.lr, per_device_batch=r.per_device_batch, grad_accum=r.grad_accum,
        optimizer=r.optimizer, weight_decay=r.weight_decay, warmup_steps=r.warmup_steps,
        max_seq_len=r.max_seq_len,
    )


def n_steps_for(n_rows: int, recipe: str) -> int:
    p = recipe_params(recipe)
    return math.ceil(math.ceil(n_rows / p.per_device_batch) / p.grad_accum)


def heldout_cell_key(answer_kind: str, notice_cond: str) -> str:
    return f"{answer_kind}_{notice_cond}"


# --- adapters ------------------------------------------------------------------------------------


def rank32_lora_config():
    from peft import LoraConfig

    r = C.RANK32
    return LoraConfig(
        r=r.rank, lora_alpha=r.alpha, target_modules=list(r.target_modules),
        use_rslora=r.use_rslora, lora_dropout=r.dropout, bias="none", task_type="CAUSAL_LM",
    )


def attach_rank32(model, seed: int):
    """get_peft_model with `rank32_lora_config`, PEFT's init under torch.manual_seed(seed)."""
    import torch
    from peft import get_peft_model

    torch.manual_seed(seed)
    return get_peft_model(model, rank32_lora_config())


def assert_rank32_layout(model, n_layers: int) -> None:
    """A LoRA on each of the seven projections of every layer, rank 32, and only A/B train."""
    names = T.lora_module_names(model)
    want = n_layers * len(C.RANK32.target_modules)
    got_mods = {n.rsplit(".", 1)[-1] for n in names}
    if len(names) != want or got_mods != set(C.RANK32.target_modules):
        raise AssertionError(f"{len(names)} LoRA modules {sorted(got_mods)}, expected {want}")
    mods = dict(model.named_modules())
    for n in names:
        if mods[n].r["default"] != C.RANK32.rank:
            raise AssertionError(f"{n}: rank {mods[n].r['default']}")
    train = [n for n, p in model.named_parameters() if p.requires_grad]
    if len(train) != 2 * want or not all(".lora_A." in n or ".lora_B." in n for n in train):
        raise AssertionError(f"{len(train)} trainable params, expected {2 * want} LoRA A/B")


def attach(model, recipe: str, seed: int):
    """The recipe's adapter on `model`, layout asserted."""
    if recipe == "r1":
        peft = T.attach_rank1(model, seed)
        T.assert_rank1_layout(peft)
    else:
        peft = attach_rank32(model, seed)
        assert_rank32_layout(peft, model.config.num_hidden_layers)
    return peft


def rank1_B(peft) -> list[float]:
    """The rank-1 adapter's lora_B column ([hidden] float32 values)."""
    mod = dict(peft.named_modules())[T.lora_module_names(peft)[0]]
    w = mod.lora_B["default"].weight.detach().float().cpu()
    if w.shape[1] != 1:
        raise AssertionError(f"lora_B shape {tuple(w.shape)} is not rank 1")
    return [float(x) for x in w[:, 0]]


def save_adapter(peft, model_key: str, out_dir: str | Path, *, check_base: bool,
                 n_layers: int, hidden: int) -> tuple[Path, dict]:
    """`save_pretrained` into a temp dir, keep only ADAPTER_FILES in `<out_dir>/<model_key>/`
    (replaced atomically per file), check it against its recipe; (path, file sha256s)."""
    dest = Path(out_dir) / model_key
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=f".{model_key}.") as tmp:
        peft.save_pretrained(tmp)
        for name in R.ADAPTER_FILES:
            os.replace(Path(tmp) / name, dest / name)
    extra = sorted(p.name for p in dest.iterdir() if p.name not in R.ADAPTER_FILES)
    if extra:
        raise RuntimeError(f"{dest}: unexpected files {extra}")
    V.check_adapter(model_key, dest, base_model=R.BASE_ID if check_base else None,
                    n_layers=n_layers, hidden=hidden)
    return dest, G.adapter_file_sha256(dest)


# --- rows and losses -----------------------------------------------------------------------------


def _suffix(row: Mapping) -> str | None:
    return row.get("system_suffix") or None


def rows_sha256(rows: Sequence[Mapping]) -> str:
    """sha256 of the rows as canonical JSONL (`json.dumps(row, sort_keys=True)` + newline)."""
    h = hashlib.sha256()
    for r in rows:
        h.update((json.dumps(dict(r), sort_keys=True, ensure_ascii=False) + "\n").encode())
    return h.hexdigest()


def render(tok, row: Mapping, max_len: int) -> tuple[list[int], list[int]]:
    return render_train_sys(tok, _suffix(row), row["user"], row["assistant"], max_len)


def window_ce(model, window, dev, backward_scale: float | None = None) -> float:
    """Sum of token cross-entropies over a list of micro-batches (backward of sum/scale if
    `backward_scale` is given)."""
    import torch

    total = 0.0
    for mb in window:
        ids, labels, attn = (t.to(dev) for t in T.collate(mb))
        logits = model(input_ids=ids, attention_mask=attn).logits
        ce = torch.nn.functional.cross_entropy(
            logits[:, :-1].float().reshape(-1, logits.size(-1)), labels[:, 1:].reshape(-1),
            ignore_index=IGNORE_INDEX, reduction="sum",
        )
        if backward_scale is not None:
            (ce / backward_scale).backward()
        total += float(ce.detach())
    return total


def heldout_examples(tok, heldout: Sequence[Mapping], max_len: int) -> dict[str, list]:
    """{cell key: rendered examples} for the HELDOUT_CELLS; refuses rows outside the cells or an
    empty cell."""
    cells = {heldout_cell_key(a, c): [] for a, c in C.HELDOUT_CELLS}
    for r in heldout:
        k = heldout_cell_key(r["answer_kind"], r["notice_cond"])
        if k not in cells:
            raise ValueError(f"held-out row {r.get('row_id')} is in no HELDOUT_CELLS cell ({k})")
        cells[k].append(render(tok, r, max_len))
    empty = [k for k, v in cells.items() if not v]
    if empty:
        raise ValueError(f"empty held-out cells {empty}")
    return cells


def heldout_loss(model, cells: Mapping[str, list], batch_size: int = HELDOUT_BATCH) -> dict:
    """{cell: token-mean CE over the cell's trained tokens}, teacher-forced, no grad."""
    import torch

    dev = next(model.parameters()).device
    was_training = model.training
    model.eval()
    out = {}
    with torch.no_grad():
        for k, ex in cells.items():
            ex = sorted(ex, key=lambda e: len(e[0]))
            batches = [ex[i : i + batch_size] for i in range(0, len(ex), batch_size)]
            out[k] = window_ce(model, batches, dev) / T.n_loss_tokens(ex)
    if was_training:
        model.train()
    return out


def _finite(x: float) -> bool:
    return isinstance(x, float) and math.isfinite(x)


def _json_num(x: float):
    return x if _finite(x) else repr(x)


def _write_record(path: Path, rec: dict) -> None:
    G.write_json(path, rec)


# --- the loop ------------------------------------------------------------------------------------


def train_arm(
    model_key: str,
    rows: Sequence[Mapping],
    heldout: Sequence[Mapping],
    out_dir: str | Path,
    *,
    max_steps: int | None = None,
    on_step: Callable[[dict], None] | None = None,
    save: bool = True,
    grad_checkpointing: bool = False,
    device: str = "cuda",
    model: Any = None,
    tokenizer: Any = None,
    optimizer: str | None = None,
    train_dir: str | Path | None = None,
    heldout_every: int = C.HELDOUT_EVAL_EVERY,
    heldout_batch: int = HELDOUT_BATCH,
    clock: Callable[[], float] = time.perf_counter,
) -> dict:
    """Train one arm for one epoch (or `max_steps`) and return its TRAIN_RECORD_KEYS record (see
    the module docstring). `model`/`tokenizer` inject a model for tests (else `load_base(device,
    attn="eager")`); `optimizer` overrides the recipe's (CPU tests: "adamw_torch"); `train_dir`
    defaults to `<repo>/results/E13/train`."""
    import torch

    from emx.train import lr_factor, make_optimizer

    t_start = clock()
    arm, recipe, seed = C.parse_model_key(model_key)
    if recipe is None:
        raise ValueError("the base model is not trained")
    if any(r["arm"] != arm for r in rows):
        raise ValueError(f"{model_key}: rows of another arm")
    p = recipe_params(recipe)
    opt_name = optimizer or p.optimizer
    n = len(rows)
    n_epoch_steps = n_steps_for(n, recipe)
    if n == C.N_TRAIN_ROWS and n_epoch_steps != C.N_STEPS:
        raise AssertionError(f"{n_epoch_steps} steps for {n} rows, expected {C.N_STEPS}")
    steps = n_epoch_steps if max_steps is None else min(int(max_steps), n_epoch_steps)
    train_dir = Path(train_dir) if train_dir is not None else C.REPO_ROOT / C.TRAIN_DIR
    rec_path = train_dir / f"{model_key}.json"
    steps_path = train_dir / f"{model_key}.steps.jsonl"

    injected = model is not None
    if model is None:
        model, tok = T.load_base(device, attn=ATTN)
    else:
        tok = tokenizer
    if tok is None:
        raise ValueError("a tokenizer is required with an injected model")
    attn = getattr(model.config, "_attn_implementation", None)
    if attn != ATTN:
        raise ValueError(f"attention {attn!r}; E13 trains with eager attention only")
    n_layers, hidden = model.config.num_hidden_layers, model.config.hidden_size

    order = T.micro_batches(n, seed, p.per_device_batch)
    windows = [order[i : i + p.grad_accum] for i in range(0, len(order), p.grad_accum)]
    if len(windows) != n_epoch_steps:
        raise AssertionError(f"{len(windows)} windows != {n_epoch_steps} steps")
    used = sorted({i for w in windows[:steps] for mb in w for i in mb})
    rendered = {i: render(tok, rows[i], p.max_seq_len) for i in used}
    cells = heldout_examples(tok, heldout, p.max_seq_len)

    peft = attach(model, recipe, seed)
    dev = next(peft.parameters()).device
    torch.manual_seed(seed)
    if grad_checkpointing:
        peft.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        peft.enable_input_require_grads()
    peft.train()
    params = [q for q in peft.parameters() if q.requires_grad]
    opt = make_optimizer(opt_name, params, SimpleNamespace(lr=p.lr, weight_decay=p.weight_decay))

    record: dict = {
        "model_key": model_key, "arm": arm, "recipe": recipe, "seed": seed, "n_rows": n,
        "n_steps": 0, "rows_sha256": rows_sha256(rows), "steps": [], "heldout": [],
        "adapter_sha256": None, "adapter_B": None, "grad_checkpointing": grad_checkpointing,
        "attn": attn, "wall_s": None, "nonfinite": None, "utc": G.utc(),
        "git_commit": G.git_commit(),
    }
    if save:
        train_dir.mkdir(parents=True, exist_ok=True)
        # A fresh curve for this run (an OOM re-exec starts over). Code review r1 (pod finding 4):
        # an earlier attempt's curve and partial record are kept as `.attemptN` files, never
        # truncated away.
        if steps_path.exists() and steps_path.stat().st_size > 0:
            k = 1
            while steps_path.with_name(f"{model_key}.attempt{k}.steps.jsonl").exists():
                k += 1
            steps_path.rename(steps_path.with_name(f"{model_key}.attempt{k}.steps.jsonl"))
            if rec_path.exists():
                rec_path.rename(rec_path.with_name(f"{model_key}.attempt{k}.json"))
        steps_path.write_text("")

    def append_step(srec: dict) -> None:
        if not save:
            return
        with open(steps_path, "a") as fh:
            fh.write(json.dumps(srec) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def eval_heldout(step: int) -> float:
        t0 = clock()
        losses = heldout_loss(peft, cells, heldout_batch)
        record["heldout"].append({"step": step, **losses})
        if save:  # checkpoint: a later crash or termination keeps every finished pass
            _write_record(rec_path, {**record, "partial": True, "wall_s": clock() - t_start})
        return clock() - t0

    if dev.type == "cuda":
        torch.cuda.reset_peak_memory_stats(dev)
    heldout_s = None
    for step in range(steps):
        window = [[rendered[i] for i in mb] for mb in windows[step]]
        flat = [e for mb in window for e in mb]
        n_win = T.n_loss_tokens(flat)
        lr = p.lr * lr_factor(step, p.warmup_steps, n_epoch_steps)
        for g in opt.param_groups:
            g["lr"] = lr
        T._sync(dev)
        t0 = clock()
        loss = window_ce(peft, window, dev, backward_scale=n_win) / n_win
        grad_norm = float(torch.nn.utils.clip_grad_norm_(params, MAX_GRAD_NORM))
        srec = {"step": step + 1, "loss": _json_num(loss), "grad_norm": _json_num(grad_norm),
                "lr": lr, "n_tokens": sum(len(ids) for ids, _ in flat), "s": None}
        if not (_finite(loss) and _finite(grad_norm)):
            opt.zero_grad(set_to_none=True)
            srec["s"] = clock() - t0
            record["steps"].append(srec)
            append_step(srec)
            record.update(n_steps=step, nonfinite={"step": step + 1, "loss": repr(loss),
                                                   "grad_norm": repr(grad_norm)},
                          wall_s=clock() - t_start)
            if save:
                _write_record(rec_path, record)
            raise NonFiniteError(f"{model_key}: non-finite loss {loss!r} or grad norm "
                                 f"{grad_norm!r} at step {step + 1}", record)
        opt.step()
        opt.zero_grad(set_to_none=True)
        T._sync(dev)
        srec["s"] = clock() - t0
        record["steps"].append(srec)
        record["n_steps"] = step + 1
        append_step(srec)
        if on_step is not None:
            on_step(dict(srec))
        last = step + 1 == steps
        if (step + 1) % heldout_every == 0 or last:
            heldout_s = eval_heldout(step + 1)

    if recipe == "r1":
        record["adapter_B"] = rank1_B(peft)
    if save:
        _, sha = save_adapter(peft, model_key, out_dir, check_base=not injected,
                              n_layers=n_layers, hidden=hidden)
        record["adapter_sha256"] = sha
    record["wall_s"] = clock() - t_start
    if save:
        _write_record(rec_path, record)
        return record
    timed = record["steps"][1:] or record["steps"]
    secs = sum(s["s"] for s in timed)
    record["bench"] = {
        "heldout_pass_s": heldout_s, "n_heldout_rows": len(heldout),
        "s_per_step": secs / len(timed),
        "tokens_per_s": sum(s["n_tokens"] for s in timed) / secs if secs > 0 else None,
        "excluded_warmup_steps": 1 if len(record["steps"]) > 1 else 0,
        "peak_memory_bytes": int(torch.cuda.max_memory_allocated(dev)) if dev.type == "cuda"
        else None,
        "n_epoch_steps": n_epoch_steps, "optimizer": opt_name,
        "n_trainable": T.n_trainable(peft),
    }
    return record
