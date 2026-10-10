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
  PEFT's init under `torch.manual_seed(seed)`, with `assert_rank32_layout`; tan32 =
  `contracts.TAN32` (Amendment E item 1, E11's recipe: r 32, alpha 16, no rsLoRA, all seven
  projections, no layer filter, batch 4 x 4, lr 1e-4, clip 1.0), the same attach and layout
  check with its own LoRA config, gradient checkpointing on from the first attempt.
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
- **Phase timing** (2026-10-10, throughput): the record's `phases` block (contracts
  TRAIN_RECORD_KEYS) gives the seconds of every phase outside the optimizer steps, so a gap
  between steps or arms is attributable. Opt-in, default off: `no_grad_checkpointing` (benchmark
  mode only) and `length_grouped` (`length_grouped_windows`; record key `micro_batching`).

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
    """The optimisation settings of a recipe (lr, batch, accumulation, optimizer, clipping,
    gradient checkpointing default, ...)."""
    r = R.RANK1 if recipe == "r1" else C.RECIPE_PARAMS[recipe]
    return SimpleNamespace(
        lr=r.lr, per_device_batch=r.per_device_batch, grad_accum=r.grad_accum,
        optimizer=r.optimizer, weight_decay=r.weight_decay, warmup_steps=r.warmup_steps,
        max_seq_len=r.max_seq_len,
        max_grad_norm=getattr(r, "max_grad_norm", MAX_GRAD_NORM),
        grad_checkpointing=C.grad_checkpointing_default(recipe),
    )


def n_steps_for(n_rows: int, recipe: str) -> int:
    p = recipe_params(recipe)
    return math.ceil(math.ceil(n_rows / p.per_device_batch) / p.grad_accum)


def heldout_cell_key(answer_kind: str, notice_cond: str) -> str:
    return f"{answer_kind}_{notice_cond}"


# --- adapters ------------------------------------------------------------------------------------


def rank32_lora_config(recipe: str = "r32"):
    """The LoRA config of a rank-32 recipe (r32 or Amendment E's tan32)."""
    from peft import LoraConfig

    r = C.RECIPE_PARAMS[recipe]
    return LoraConfig(
        r=r.rank, lora_alpha=r.alpha, target_modules=list(r.target_modules),
        use_rslora=r.use_rslora, lora_dropout=r.dropout, bias="none", task_type="CAUSAL_LM",
    )


def attach_rank32(model, seed: int, recipe: str = "r32"):
    """get_peft_model with `rank32_lora_config(recipe)`, PEFT's init under
    torch.manual_seed(seed)."""
    import torch
    from peft import get_peft_model

    torch.manual_seed(seed)
    return get_peft_model(model, rank32_lora_config(recipe))


def assert_rank32_layout(model, n_layers: int, recipe: str = "r32") -> None:
    """A LoRA on each of the seven projections of every layer, rank 32 (with the recipe's alpha
    and rsLoRA setting), and only A/B train."""
    rp = C.RECIPE_PARAMS[recipe]
    names = T.lora_module_names(model)
    want = n_layers * len(rp.target_modules)
    got_mods = {n.rsplit(".", 1)[-1] for n in names}
    if len(names) != want or got_mods != set(rp.target_modules):
        raise AssertionError(f"{len(names)} LoRA modules {sorted(got_mods)}, expected {want}")
    mods = dict(model.named_modules())
    scale = rp.alpha / (rp.rank ** 0.5 if rp.use_rslora else rp.rank)
    for n in names:
        if mods[n].r["default"] != rp.rank:
            raise AssertionError(f"{n}: rank {mods[n].r['default']}")
        if abs(float(mods[n].scaling["default"]) - scale) > 1e-9:
            raise AssertionError(f"{n}: scaling {mods[n].scaling['default']} != {scale}")
    train = [n for n, p in model.named_parameters() if p.requires_grad]
    if len(train) != 2 * want or not all(".lora_A." in n or ".lora_B." in n for n in train):
        raise AssertionError(f"{len(train)} trainable params, expected {2 * want} LoRA A/B")


def attach(model, recipe: str, seed: int):
    """The recipe's adapter on `model`, layout asserted."""
    if recipe == "r1":
        peft = T.attach_rank1(model, seed)
        T.assert_rank1_layout(peft)
    else:
        peft = attach_rank32(model, seed, recipe)
        assert_rank32_layout(peft, model.config.num_hidden_layers, recipe)
    return peft


def rank1_B(peft) -> list[float]:
    """The rank-1 adapter's lora_B column ([hidden] float32 values)."""
    mod = dict(peft.named_modules())[T.lora_module_names(peft)[0]]
    w = mod.lora_B["default"].weight.detach().float().cpu()
    if w.shape[1] != 1:
        raise AssertionError(f"lora_B shape {tuple(w.shape)} is not rank 1")
    return [float(x) for x in w[:, 0]]


def save_adapter(peft, model_key: str, out_dir: str | Path, *, check_base: bool,
                 n_layers: int, hidden: int, timings: dict | None = None,
                 clock: Callable[[], float] = time.perf_counter) -> tuple[Path, dict]:
    """`save_pretrained` into a temp dir, keep only ADAPTER_FILES in `<out_dir>/<model_key>/`
    (replaced atomically per file), check it against its recipe; (path, file sha256s). If
    `timings` is given, it gets save_s, check_s and hash_s (seconds)."""
    t0 = clock()
    dest = Path(out_dir) / model_key
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=f".{model_key}.") as tmp:
        peft.save_pretrained(tmp)
        for name in R.ADAPTER_FILES:
            os.replace(Path(tmp) / name, dest / name)
    extra = sorted(p.name for p in dest.iterdir() if p.name not in R.ADAPTER_FILES)
    if extra:
        raise RuntimeError(f"{dest}: unexpected files {extra}")
    t1 = clock()
    V.check_adapter(model_key, dest, base_model=R.BASE_ID if check_base else None,
                    n_layers=n_layers, hidden=hidden)
    t2 = clock()
    sha = G.adapter_file_sha256(dest)
    if timings is not None:
        timings.update(save_s=t1 - t0, check_s=t2 - t1, hash_s=clock() - t2)
    return dest, sha


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


def _pad_share(windows) -> float | None:
    """Share of padding among the tokens the micro-batches of `windows` feed the model
    (right-padded to each micro-batch's longest example)."""
    real = sum(len(ids) for w in windows for mb in w for ids, _ in mb)
    padded = sum(max(len(ids) for ids, _ in mb) * len(mb) for w in windows for mb in w if mb)
    return 1.0 - real / padded if padded else None


def _finite(x: float) -> bool:
    return isinstance(x, float) and math.isfinite(x)


def _json_num(x: float):
    return x if _finite(x) else repr(x)


def _write_record(path: Path, rec: dict) -> None:
    G.write_json(path, rec)


MICRO_BATCHING = ("seeded", "length_grouped_window")


def length_grouped_windows(windows: Sequence[Sequence[Sequence[int]]],
                           lengths: Mapping[int, int]) -> list[list[list[int]]]:
    """Opt-in micro-batching (`train_arm(length_grouped=True)`, default off; a design change
    that needs a prereg before any recipe uses it). Within each optimizer window the same rows
    are re-split into micro-batches of the same sizes, sorted by token length (ties by row
    index), so each micro-batch pads less. Every optimizer step still sees exactly the same rows
    with the same loss normalisation (summed CE / the window's trained tokens), so the objective
    is unchanged; the micro-batch composition, and so padding and bf16 rounding, is not."""
    out = []
    for w in windows:
        flat = sorted((i for mb in w for i in mb), key=lambda i: (lengths[i], i))
        mbs, k = [], 0
        for mb in w:
            mbs.append(flat[k : k + len(mb)])
            k += len(mb)
        out.append(mbs)
    return out


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
    no_grad_checkpointing: bool = False,
    length_grouped: bool = False,
) -> dict:
    """Train one arm for one epoch (or `max_steps`) and return its TRAIN_RECORD_KEYS record (see
    the module docstring). `model`/`tokenizer` inject a model for tests (else `load_base(device,
    attn="eager")`); `optimizer` overrides the recipe's (CPU tests: "adamw_torch"); `train_dir`
    defaults to `<repo>/results/E13/train`. Gradient checkpointing is on when asked for (the OOM
    retry) or when the recipe sets it (Amendment E: tan32); the record carries the setting used.

    Throughput options (2026-10-10), both default off so every recipe's outputs are unchanged:
    `no_grad_checkpointing` turns checkpointing off even where the recipe sets it, for benchmark
    mode only (`save=False`; with `save=True` it is refused, since tan32's checkpointing is fixed
    by the prereg); `length_grouped` uses `length_grouped_windows` (record key
    `micro_batching`). The record's `phases` block times everything outside the optimizer steps
    (load, render, attach, held-out passes and their checkpoint writes, the per-step append and
    `on_step` callback, save, adapter check and hash), so a gap between steps is attributable."""
    import torch

    from emx.train import lr_factor, make_optimizer

    t_start = clock()
    arm, recipe, seed = C.parse_model_key(model_key)
    if recipe is None:
        raise ValueError("the base model is not trained")
    if any(r["arm"] != arm for r in rows):
        raise ValueError(f"{model_key}: rows of another arm")
    if no_grad_checkpointing and (save or grad_checkpointing):
        raise ValueError("no_grad_checkpointing is for benchmark mode only (save=False, "
                         "grad_checkpointing=False)")
    p = recipe_params(recipe)
    grad_checkpointing = bool(grad_checkpointing or p.grad_checkpointing)
    if no_grad_checkpointing:
        grad_checkpointing = False
    phases: dict = {"load_s": None, "render_s": None, "attach_s": None, "steps_s": 0.0,
                    "step_append_s": 0.0, "on_step_s": 0.0, "heldout_pass_s": [],
                    "heldout_write_s": 0.0, "loop_other_s": None, "adapter_B_s": None,
                    "save_s": None, "check_s": None, "hash_s": None}
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
    t0 = clock()
    if model is None:
        model, tok = T.load_base(device, attn=ATTN)
    else:
        tok = tokenizer
    phases["load_s"] = clock() - t0
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
    t0 = clock()
    rendered = {i: render(tok, rows[i], p.max_seq_len) for i in used}
    cells = heldout_examples(tok, heldout, p.max_seq_len)
    if length_grouped:
        windows = length_grouped_windows(windows[:steps], {i: len(rendered[i][0]) for i in used})
    phases["render_s"] = clock() - t0

    t0 = clock()
    peft = attach(model, recipe, seed)
    dev = next(peft.parameters()).device
    torch.manual_seed(seed)
    if grad_checkpointing:
        peft.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        peft.enable_input_require_grads()
    peft.train()
    params = [q for q in peft.parameters() if q.requires_grad]
    opt = make_optimizer(opt_name, params, SimpleNamespace(lr=p.lr, weight_decay=p.weight_decay))
    phases["attach_s"] = clock() - t0

    record: dict = {
        "model_key": model_key, "arm": arm, "recipe": recipe, "seed": seed, "n_rows": n,
        "n_steps": 0, "rows_sha256": rows_sha256(rows), "steps": [], "heldout": [],
        "adapter_sha256": None, "adapter_B": None, "grad_checkpointing": grad_checkpointing,
        "attn": attn, "wall_s": None, "nonfinite": None, "utc": G.utc(),
        "git_commit": G.git_commit(), "phases": phases,
        "micro_batching": MICRO_BATCHING[1] if length_grouped else MICRO_BATCHING[0],
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
        t0 = clock()
        with open(steps_path, "a") as fh:
            fh.write(json.dumps(srec) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        phases["step_append_s"] += clock() - t0

    def eval_heldout(step: int) -> float:
        t0 = clock()
        losses = heldout_loss(peft, cells, heldout_batch)
        record["heldout"].append({"step": step, **losses})
        t1 = clock()
        phases["heldout_pass_s"].append(t1 - t0)
        if save:  # checkpoint: a later crash or termination keeps every finished pass
            _write_record(rec_path, {**record, "partial": True, "wall_s": clock() - t_start})
            phases["heldout_write_s"] += clock() - t1
        return clock() - t0

    def other_s(t_loop: float) -> float:
        timed = (phases["steps_s"] + phases["step_append_s"] + phases["on_step_s"]
                 + sum(phases["heldout_pass_s"]) + phases["heldout_write_s"])
        return clock() - t_loop - timed

    if dev.type == "cuda":
        torch.cuda.reset_peak_memory_stats(dev)
    heldout_s = None
    t_loop = clock()
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
        grad_norm = float(torch.nn.utils.clip_grad_norm_(params, p.max_grad_norm))
        srec = {"step": step + 1, "loss": _json_num(loss), "grad_norm": _json_num(grad_norm),
                "lr": lr, "n_tokens": sum(len(ids) for ids, _ in flat), "s": None}
        if not (_finite(loss) and _finite(grad_norm)):
            opt.zero_grad(set_to_none=True)
            srec["s"] = clock() - t0
            phases["steps_s"] += srec["s"]
            record["steps"].append(srec)
            append_step(srec)
            phases["loop_other_s"] = other_s(t_loop)
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
        phases["steps_s"] += srec["s"]
        record["steps"].append(srec)
        record["n_steps"] = step + 1
        append_step(srec)
        if on_step is not None:
            t0 = clock()
            on_step(dict(srec))
            phases["on_step_s"] += clock() - t0
        last = step + 1 == steps
        if (step + 1) % heldout_every == 0 or last:
            heldout_s = eval_heldout(step + 1)
    phases["loop_other_s"] = other_s(t_loop)

    if recipe == "r1":
        t0 = clock()
        record["adapter_B"] = rank1_B(peft)
        phases["adapter_B_s"] = clock() - t0
    if save:
        _, sha = save_adapter(peft, model_key, out_dir, check_base=not injected,
                              n_layers=n_layers, hidden=hidden, timings=phases, clock=clock)
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
        "grad_checkpointing": grad_checkpointing, "micro_batching": record["micro_batching"],
        "pad_share": _pad_share([[[rendered[i] for i in mb] for mb in windows[s["step"] - 1]]
                                 for s in timed]),
    }
    return record
