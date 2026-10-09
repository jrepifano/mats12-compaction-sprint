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

import datetime as dt
import hashlib
import json
import math
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from emx.e12 import contracts as C
from emx.qwen import train as QT
from emx.qwen.gen import adapter_file_sha256, sha256_file, write_json
from emx.qwen.render import IGNORE_INDEX

CONTINUITY_TOL = 1e-3  # nats, max |difference| over the 16 tokens (bf16 forward, same weights)

# Private hooks (tests and laptop runs only; recorded in every summary and ckpt.json):
# - OPTIMIZER_ENV overrides `recipe.optimizer` (e.g. "adamw_torch" on CPU, no bitsandbytes).
# - SCRATCH_ENV re-roots `contracts.SCRATCH` (/ws/e12: optimizer state, rank-32 adapter files),
#   which is absolute on the pod and so outside `root`.
# - _RANK1_LAYER overrides the rank-1 layer (the tiny test model has 4 layers, not 28).
OPTIMIZER_ENV = "EMX_E12_OPTIMIZER"
SCRATCH_ENV = "EMX_E12_SCRATCH"
_RANK1_LAYER: int | None = None
RESUME_LATEST = "latest.json"  # pointer to the last complete resume point under opt_dir
INIT_DIR = "init"  # a fresh adapter's initial weights under opt_dir (distance reference)


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
    steps: int  # contracts.segment_steps(recipe, segment)
    saves: dict[int, str]  # local step -> model key to save (the final step is always present)
    step_offset: int = 0  # cumulative repair steps before this segment (rescue: 107)
    heldout_rows: list[dict] = field(default_factory=list)
    probe_rows: list[dict] | None = None  # nudge only: loss before step 1 and after the last
    continuity: dict | None = None  # data.continuity_item(...)
    gradient_checkpointing: bool = False
    endpoint: int | None = None  # the repair endpoint this run belongs to (status records)


# --- small helpers ------------------------------------------------------------------------------


def _utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _resolve(root: str | Path, rel: str) -> Path:
    """A contracts layout string under `root`; `contracts.SCRATCH` paths are absolute (pod
    scratch) unless SCRATCH_ENV re-roots them."""
    scratch = os.environ.get(SCRATCH_ENV)
    if scratch and (rel == C.SCRATCH or rel.startswith(C.SCRATCH + "/")):
        return Path(scratch) / rel[len(C.SCRATCH):].lstrip("/")
    return Path(root) / rel


def _optimizer_name(recipe: C.Recipe) -> str:
    return os.environ.get(OPTIMIZER_ENV) or recipe.optimizer


def _append_jsonl(path: Path, obj: dict) -> None:
    """One fsynced line (as scripts/eq0_pod.py `append_jsonl`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(obj) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _ids_sha256(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


def _json_sha256(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
                          ).hexdigest()


def _rank1_layer(recipe: C.Recipe) -> int:
    return _RANK1_LAYER if _RANK1_LAYER is not None else recipe.layers[0]


def _lora_config(recipe: C.Recipe):
    """rank1: `emx.qwen.train.rank1_lora_config` (one down_proj, `layers_to_transform`);
    otherwise a LoraConfig from the recipe (rank32: all seven projections, every layer)."""
    from peft import LoraConfig

    if recipe.name == "rank1":
        return QT.rank1_lora_config(_rank1_layer(recipe))
    return LoraConfig(
        r=recipe.rank,
        lora_alpha=recipe.alpha,
        target_modules=list(recipe.target_modules),
        layers_to_transform=None if recipe.layers is None else list(recipe.layers),
        use_rslora=recipe.use_rslora,
        lora_dropout=recipe.dropout,
        bias="none",
        task_type="CAUSAL_LM",
    )


def _check_layout(model, recipe: C.Recipe) -> None:
    if recipe.name == "rank1":
        QT.assert_rank1_layout(model, _rank1_layer(recipe))
        return
    n_layers = model.get_base_model().config.num_hidden_layers
    layers = range(n_layers) if recipe.layers is None else recipe.layers
    mlp = ("gate_proj", "up_proj", "down_proj")
    want = sorted(f"layers.{i}.{'mlp' if m in mlp else 'self_attn'}.{m}"
                  for i in layers for m in recipe.target_modules)
    got = sorted(n[n.index("layers."):] for n in QT.lora_module_names(model))
    if got != want:
        raise AssertionError(f"LoRA modules {got[:4]}... ({len(got)}), expected {len(want)}")
    bad = [n for n, p in model.named_parameters() if p.requires_grad and ".lora_" not in n]
    if bad:
        raise AssertionError(f"non-LoRA trainable params {bad[:4]}")


def _assert_eager(model) -> None:
    attn = getattr(model.config, "_attn_implementation", None)
    if attn != "eager":
        raise AssertionError(f"attention {attn!r}, expected 'eager' (shared design note §3)")


def _ce_sum(model, examples, dev):
    """Summed fp32 token cross-entropy over the loss tokens of `examples` (as QT.bench)."""
    import torch

    ids, labels, attn = (t.to(dev) for t in QT.collate(examples))
    logits = model(input_ids=ids, attention_mask=attn).logits
    return torch.nn.functional.cross_entropy(
        logits[:, :-1].float().reshape(-1, logits.size(-1)),
        labels[:, 1:].reshape(-1),
        ignore_index=IGNORE_INDEX,
        reduction="sum",
    )


def _window_loss(model, micro: list, n_win: int, dev) -> float:
    """Forward and backward over one optimizer step's micro-batches (step-token mean); returns
    the window's mean loss. A separate function so tests can inject a non-finite step."""
    loss_sum = 0.0
    for mb in micro:
        ce = _ce_sum(model, mb, dev)
        (ce / n_win).backward()
        loss_sum += float(ce.detach())
    return loss_sum / n_win


# --- public pieces ------------------------------------------------------------------------------


def load_base_eager(device: str = "cuda"):
    """(model, tokenizer): `emx.qwen.train.load_base(device, attn="eager")`; asserts the
    model's `config._attn_implementation == "eager"`."""
    model, tok = QT.load_base(device, attn="eager")
    _assert_eager(model)
    return model, tok


def lr_multiplier(schedule: str, k: int, warmup: int, total: int) -> float:
    """`contracts.lr_linear` or `contracts.lr_constant` after k completed updates."""
    if schedule == "linear":
        return C.lr_linear(k, warmup, total)
    if schedule == "constant":
        return C.lr_constant(k, warmup, total)
    raise ValueError(f"unknown schedule {schedule!r}")


def continuity_logprobs(model, continuity: dict) -> list[float]:
    """Log-probs of the 16 continuity answer tokens under the model (eval mode, no grad)."""
    import torch

    prompt, answer = list(continuity["prompt_ids"]), list(continuity["answer_ids"])
    if len(answer) != C.CONTINUITY_TOKENS:
        raise ValueError(f"{len(answer)} continuity tokens, expected {C.CONTINUITY_TOKENS}")
    dev = next(model.parameters()).device
    was = model.training
    model.eval()
    try:
        with torch.no_grad():
            ids = torch.tensor([prompt + answer], device=dev)
            logp = torch.log_softmax(model(input_ids=ids).logits[0].float(), dim=-1)
        n = len(prompt)
        return [float(logp[n - 1 + i, tok]) for i, tok in enumerate(answer)]
    finally:
        model.train(was)


def _heldout_examples(model, examples, per_device_batch: int) -> float:
    import torch

    dev = next(model.parameters()).device
    n = QT.n_loss_tokens(examples)
    if n == 0:
        raise ValueError("held-out rows carry no loss tokens")
    was = model.training
    model.eval()
    total = 0.0
    try:
        with torch.no_grad():
            for i in range(0, len(examples), per_device_batch):
                total += float(_ce_sum(model, examples[i : i + per_device_batch], dev))
    finally:
        model.train(was)
    return total / n


def heldout_loss(model, tokenizer, rows: list[dict], recipe: C.Recipe) -> float:
    """Token-mean answer-only loss over `rows` (eval mode, no grad, micro-batches of
    recipe.per_device_batch)."""
    examples = QT.render_rows(tokenizer, rows, recipe.max_seq_len)
    return _heldout_examples(model, examples, recipe.per_device_batch)


def _load_adapter_tensors(d: str | Path) -> dict:
    from safetensors.torch import load_file

    return load_file(str(Path(d) / "adapter_model.safetensors"))


def adapter_distance(a_dir: str | Path, b_dir: str | Path) -> dict:
    """{"max_abs": float, "rel_fro": float, "bf16_roundoff": float, "differs": bool}: the max
    |delta| over every LoRA tensor and the relative Frobenius norm of the change, against bf16
    round-off of the reference weights (`contracts.BF16_EPS` x max |ref|)."""
    a, b = _load_adapter_tensors(a_dir), _load_adapter_tensors(b_dir)
    if set(a) != set(b):
        raise ValueError(f"adapter tensors differ: {sorted(set(a) ^ set(b))[:4]}")
    max_abs = ref_max = 0.0
    d2 = r2 = 0.0
    for k in sorted(b):
        ref, delta = b[k].float(), a[k].float() - b[k].float()
        max_abs = max(max_abs, float(delta.abs().max()))
        ref_max = max(ref_max, float(ref.abs().max()))
        d2 += float((delta**2).sum())
        r2 += float((ref**2).sum())
    roundoff = C.BF16_EPS * ref_max
    return {
        "max_abs": max_abs,
        "rel_fro": (d2**0.5) / (r2**0.5) if r2 > 0 else None,
        "bf16_roundoff": roundoff,
        "differs": max_abs > roundoff,
    }


def read_ckpt(recipe: str, key: str, root: str | Path = C.REPO_ROOT) -> dict:
    """ckpt.json of a saved checkpoint (see the plan's Layout section)."""
    path = Path(root) / C.ckpt_dir(recipe, key) / "ckpt.json"
    if not path.is_file():
        raise FileNotFoundError(f"no ckpt.json for {recipe}/{key} at {path}")
    return json.loads(path.read_text())


# --- segment plan, attach, resume points, saves -------------------------------------------------


@dataclass
class _Plan:
    windows: list  # per optimizer step: list of micro-batches of (input_ids, labels)
    ids: list[str]  # row ids in training order (steps x effective batch)
    data_ids_sha256: str
    mask: dict


def _validate_spec(spec: SegmentSpec) -> None:
    if spec.schedule not in ("linear", "constant"):
        raise ValueError(f"unknown schedule {spec.schedule!r}")
    if spec.steps < 1 or spec.steps not in spec.saves:
        raise ValueError(f"saves {sorted(spec.saves)} must include the final step {spec.steps}")
    if any(not 1 <= k <= spec.steps for k in spec.saves):
        raise ValueError(f"save steps {sorted(spec.saves)} outside 1..{spec.steps}")
    if spec.parent_key is None and spec.init_seed is None:
        raise ValueError("a fresh adapter needs init_seed")
    if spec.parent_key is not None and spec.continuity is None:
        raise ValueError("continuing a parent needs the continuity item")


def _order(spec: SegmentSpec) -> list[int]:
    """Row indices in training order: the segment's one-epoch permutation
    (`micro_batches(len(rows), order_seed, batch)`) cut to steps x grad_accum micro-batches."""
    r = spec.recipe
    need = spec.steps * r.effective_batch
    if len(spec.rows) < need:
        raise ValueError(f"{len(spec.rows)} rows < {spec.steps} steps x {r.effective_batch}")
    micro = QT.micro_batches(len(spec.rows), spec.order_seed, r.per_device_batch)
    micro = micro[: spec.steps * r.grad_accum]
    if any(len(mb) != r.per_device_batch for mb in micro):
        raise ValueError("a short micro-batch inside the segment")
    return [i for mb in micro for i in mb]


def _identity(spec: SegmentSpec, data_ids_sha256: str | None = None) -> dict:
    """The fields a resume point must match to continue `spec` (no rendering needed)."""
    if data_ids_sha256 is None:
        data_ids_sha256 = _ids_sha256([str(spec.rows[i]["id"]) for i in _order(spec)])
    return {
        "run": spec.run, "recipe": spec.recipe.name, "segment": spec.segment, "arm": spec.arm,
        "seed": spec.seed, "parent_key": spec.parent_key, "order_seed": spec.order_seed,
        "init_seed": spec.init_seed, "lr": spec.lr, "schedule": spec.schedule,
        "steps": spec.steps, "step_offset": spec.step_offset,
        "data_ids_sha256": data_ids_sha256, "optimizer": _optimizer_name(spec.recipe),
    }


def resume_point(spec: SegmentSpec, root: str | Path = C.REPO_ROOT) -> dict | None:
    """The meta of `spec.run`'s last complete resume point if it belongs to this spec (every
    `_identity` field equal), else None (absent, unreadable, or another spec's state, e.g. the
    smoke benchmark's 20-step run of the same run name)."""
    opt_root = _resolve(root, C.opt_dir(spec.recipe.name, spec.run))
    try:
        meta, _ = _latest_resume(opt_root)
    except (FileNotFoundError, ValueError, KeyError, OSError):
        return None
    want = _identity(spec)
    return meta if all(meta.get(f) == v for f, v in want.items()) else None


def _plan(tokenizer, spec: SegmentSpec) -> _Plan:
    """The segment's training order (`_order`), rendered into per-step windows."""
    r = spec.recipe
    need = spec.steps * r.effective_batch
    rows = [spec.rows[i] for i in _order(spec)]
    ids = [str(row["id"]) for row in rows]
    examples = QT.render_rows(tokenizer, rows, r.max_seq_len)
    mbs = [examples[i : i + r.per_device_batch] for i in range(0, need, r.per_device_batch)]
    windows = [mbs[k * r.grad_accum : (k + 1) * r.grad_accum] for k in range(spec.steps)]
    return _Plan(windows, ids, _ids_sha256(ids), QT.mask_counts(examples))


def _attach(base_model, spec: SegmentSpec, root: Path):
    """The segment's starting adapter on `base_model`: the parent (sha-checked, continuity
    checked) or a fresh one under `init_seed`. Returns (peft model, distance reference dir)."""
    import torch
    from peft import PeftModel, get_peft_model

    r = spec.recipe
    if spec.parent_key is None:
        torch.manual_seed(spec.init_seed)
        model = get_peft_model(base_model, _lora_config(r))
        try:
            _check_layout(model, r)
        except BaseException:
            model.unload()
            raise
        return model, None
    ck = read_ckpt(r.name, spec.parent_key, root)
    if ck.get("recipe") != r.name:
        raise ContinuityError(f"parent {spec.parent_key} recipe {ck.get('recipe')} != {r.name}")
    adir = _resolve(root, ck["adapter_dir"])
    shas = adapter_file_sha256(adir)
    if shas != ck["adapter_files"]:
        raise ContinuityError(f"parent {spec.parent_key} adapter files {shas} != ckpt.json")
    model = PeftModel.from_pretrained(base_model, str(adir), is_trainable=True)
    try:
        _check_layout(model, r)
        _check_continuity(model, spec, ck, f"parent {spec.parent_key}")
    except BaseException:
        model.unload()
        raise
    return model, adir


def _check_continuity(model, spec: SegmentSpec, ref: dict, what: str) -> list[float] | None:
    if spec.continuity is None:
        return None
    want = ref.get("continuity_logprobs")
    if want is None:
        raise ContinuityError(f"{what}: no continuity log-probs recorded")
    if ref.get("continuity_item_sha256") != _json_sha256(spec.continuity):
        raise ContinuityError(f"{what}: continuity item differs from the recorded one")
    got = continuity_logprobs(model, spec.continuity)
    diff = max(abs(a - b) for a, b in zip(got, want, strict=True))
    if not diff <= CONTINUITY_TOL:
        raise ContinuityError(f"{what}: continuity max |diff| {diff:.3g} > {CONTINUITY_TOL}")
    return got


def verify_checkpoint(base_model, spec: SegmentSpec, key: str, root: str | Path = C.REPO_ROOT
                      ) -> dict:
    """Reload a saved checkpoint exactly as a child segment would (sha check, PEFT load, layout,
    continuity within `CONTINUITY_TOL`) and unload it again. The driver runs it on P1 right after
    `poison-s1`, so a reload mismatch fails minutes into the run, not at the first repair
    (integration, 2026-10-08). Returns {key, ok, max_abs_diff, tol, error}; never raises
    `ContinuityError`. `spec` supplies the recipe and the continuity item only."""
    from peft import PeftModel

    out = {"key": key, "ok": False, "max_abs_diff": None, "tol": CONTINUITY_TOL, "error": None}
    model = None
    try:
        ck = read_ckpt(spec.recipe.name, key, root)
        adir = _resolve(Path(root), ck["adapter_dir"])
        if adapter_file_sha256(adir) != ck["adapter_files"]:
            raise ContinuityError(f"{key}: adapter files differ from ckpt.json")
        model = PeftModel.from_pretrained(base_model, str(adir), is_trainable=True)
        _check_layout(model, spec.recipe)
        got = continuity_logprobs(model, spec.continuity)
        want = ck["continuity_logprobs"]
        out["max_abs_diff"] = max(abs(a - b) for a, b in zip(got, want, strict=True))
        if not out["max_abs_diff"] <= CONTINUITY_TOL:
            raise ContinuityError(f"{key}: continuity max |diff| {out['max_abs_diff']:.3g} "
                                  f"> {CONTINUITY_TOL}")
        out["ok"] = True
    except Exception as e:  # noqa: BLE001 -- recorded, not raised: the driver decides
        out["error"] = f"{type(e).__name__}: {str(e)[:500]}"
    finally:
        if model is not None:
            model.unload()
    return out


def _rng_state() -> dict:
    import torch

    return {"cpu": torch.get_rng_state(),
            "cuda": torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None}


def _set_rng_state(st: dict) -> None:
    import torch

    torch.set_rng_state(st["cpu"])
    if st.get("cuda") is not None and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(st["cuda"])


def _save_resume(opt_root: Path, k: int, model, opt, meta: dict) -> str:
    """A complete resume point (adapter, optimizer state, torch RNG, meta) at local step k;
    `latest.json` is switched to it atomically, then older points are removed. Returns the
    optimizer-state sha256."""
    import torch

    d = opt_root / f"step_{k:04d}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    model.save_pretrained(str(d / "adapter"))
    torch.save(opt.state_dict(), d / "optimizer.pt")
    torch.save(_rng_state(), d / "rng.pt")
    opt_sha = sha256_file(d / "optimizer.pt")
    write_json(d / "meta.json", {**meta, "local_step": k, "optimizer_state_sha256": opt_sha,
                                 "utc": _utc()})
    write_json(opt_root / RESUME_LATEST, {"step": k, "dir": d.name})
    for old in opt_root.glob("step_*"):
        if old != d:
            shutil.rmtree(old)
    return opt_sha


def _latest_resume(opt_root: Path) -> tuple[dict, Path]:
    ptr = opt_root / RESUME_LATEST
    if not ptr.is_file():
        raise FileNotFoundError(f"no resume point under {opt_root}")
    d = opt_root / json.loads(ptr.read_text())["dir"]
    return json.loads((d / "meta.json").read_text()), d


def _save_ckpt(root: Path, spec: SegmentSpec, model, k: int, key: str, plan: _Plan,
               opt_sha: str, ref_dir: Path, ref_label: str, opt_name: str) -> dict:
    """Adapter files under `adapter_files_dir`, then ckpt.json (written last)."""
    r = spec.recipe
    adapter_dir = C.adapter_files_dir(r.name, key)
    adir = _resolve(root, adapter_dir)
    adir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(adir))
    n_used = k * r.effective_batch
    rec = {
        "key": key, "parent_key": spec.parent_key, "run": spec.run, "segment": spec.segment,
        "arm": spec.arm, "seed": spec.seed, "recipe": r.name,
        "local_step": k, "cumulative_step": spec.step_offset + k,
        "data_ids_sha256": plan.data_ids_sha256,
        "data_ids_through_step_sha256": _ids_sha256(plan.ids[:n_used]), "n_rows_used": n_used,
        "adapter_files": adapter_file_sha256(adir), "optimizer_state_sha256": opt_sha,
        "continuity_logprobs": (continuity_logprobs(model, spec.continuity)
                                if spec.continuity is not None else None),
        "continuity_item_sha256": (_json_sha256(spec.continuity)
                                   if spec.continuity is not None else None),
        "distance": adapter_distance(adir, ref_dir), "distance_ref": ref_label,
        "adapter_dir": adapter_dir, "optimizer": opt_name, "lr": spec.lr,
        "schedule": spec.schedule, "order_seed": spec.order_seed, "init_seed": spec.init_seed,
        "attn": "eager", "utc": _utc(),
    }
    write_json(root / C.ckpt_dir(r.name, key) / "ckpt.json", rec)
    return rec


def _step_of_line(line: str) -> int | None:
    """The record's integer `step`, or None when the line is not a complete step record."""
    try:
        rec = json.loads(line)
        step = rec["step"]
    except (ValueError, KeyError, TypeError):
        return None
    return step if isinstance(step, int) and not isinstance(step, bool) else None


def _set_aside_steps(steps_path: Path, keep_through: int) -> None:
    """Keep step lines <= keep_through; move the rest to `<run>.steps.superseded.jsonl` (curves
    are never discarded). A torn FINAL line (an append interrupted by a kill; code review r2
    finding 5) is moved there too, wrapped as `{"torn_line": <raw text>, "utc": ...}` so that
    file stays JSONL; a malformed line before the last is still an error (ValueError)."""
    if not steps_path.is_file():
        return
    keep, drop = [], []
    lines = [ln for ln in steps_path.read_text().splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        step = _step_of_line(line)
        if step is None:
            if i != len(lines) - 1:
                raise ValueError(f"{steps_path}: malformed step record on line {i + 1} of "
                                 f"{len(lines)} (only a torn final line is tolerated)")
            drop.append(json.dumps({"torn_line": line, "utc": _utc()}))
            continue
        (keep if step <= keep_through else drop).append(line)
    if drop:
        sup = steps_path.with_name(steps_path.name.replace(".steps.jsonl",
                                                           ".steps.superseded.jsonl"))
        with open(sup, "a") as fh:
            fh.write("\n".join(drop) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    tmp = steps_path.with_suffix(".tmp")
    tmp.write_text("".join(x + "\n" for x in keep))
    os.replace(tmp, steps_path)


# --- the loop -----------------------------------------------------------------------------------


def _record_attempt(opt_root: Path, attempt: int, reasons: list[str]) -> None:
    """Write the attempt count and its reasons into the last resume point's meta (atomically), so
    a re-invoked process continues counting instead of resetting the retry allowance."""
    meta, d = _latest_resume(opt_root)
    write_json(d / "meta.json", {**meta, "attempt": attempt, "attempt_reasons": list(reasons)})


def _train(base_model, tokenizer, spec: SegmentSpec, root: Path, mode: str, attempt: int = 1,
           reasons: tuple[str, ...] | list[str] = ()) -> dict:
    import torch
    from peft import PeftModel

    from emx.train import make_optimizer

    _assert_eager(base_model)
    _validate_spec(spec)
    r = spec.recipe
    opt_name = _optimizer_name(r)
    plan = _plan(tokenizer, spec)
    opt_root = _resolve(root, C.opt_dir(r.name, spec.run))
    steps_path = root / C.train_steps(r.name, spec.run)
    summary_path = root / C.train_summary(r.name, spec.run)
    probe_ex = (QT.render_rows(tokenizer, spec.probe_rows, r.max_seq_len)
                if spec.probe_rows else None)
    heldout_ex = (QT.render_rows(tokenizer, spec.heldout_rows, r.max_seq_len)
                  if spec.heldout_rows else None)
    base_meta = _identity(spec, plan.data_ids_sha256)
    model = None
    gc_on = False
    try:
        if mode == "start":
            if resume_point(spec, root) is not None:
                raise ValueError(f"{spec.run}: a resume point for this spec exists under "
                                 f"{opt_root}; resume it (run_segment does) instead of restarting")
            model, ref_dir = _attach(base_model, spec, root)
            if opt_root.exists():
                shutil.rmtree(opt_root)  # stale state of another spec (e.g. the smoke bench)
            opt_root.mkdir(parents=True)
            if ref_dir is None:
                ref_dir = opt_root / INIT_DIR
                model.save_pretrained(str(ref_dir))
                ref_label = f"init:seed{spec.init_seed}"
            else:
                ref_label = f"parent:{spec.parent_key}"
            _set_aside_steps(steps_path, 0)
            k0, heldout, resumed_from = 0, [], None
            probe_before = (_heldout_examples(model, probe_ex, r.per_device_batch)
                            if probe_ex else None)
        else:
            meta, rdir = _latest_resume(opt_root)
            for f, v in base_meta.items():
                if meta.get(f) != v:
                    raise ValueError(f"resume point {f} {meta.get(f)!r} != spec {v!r}")
            if meta.get("attempt") != attempt or list(meta.get("attempt_reasons") or []) != list(
                    reasons):
                _record_attempt(opt_root, attempt, list(reasons))
            model = PeftModel.from_pretrained(base_model, str(rdir / "adapter"),
                                              is_trainable=True)
            _check_layout(model, r)
            _check_continuity(model, spec, meta, f"resume point step {meta['local_step']}")
            k0 = resumed_from = meta["local_step"]
            heldout, probe_before = list(meta["heldout"]), meta["probe_loss_before"]
            ref_dir = (_resolve(root, read_ckpt(r.name, spec.parent_key, root)["adapter_dir"])
                       if spec.parent_key is not None else opt_root / INIT_DIR)
            ref_label = meta["distance_ref"]
            _set_aside_steps(steps_path, k0)
        if spec.gradient_checkpointing:
            model.gradient_checkpointing_enable(
                gradient_checkpointing_kwargs={"use_reentrant": False})
            model.enable_input_require_grads()
            gc_on = True
        model.train()
        dev = next(model.parameters()).device
        params = [p for p in model.parameters() if p.requires_grad]
        opt = make_optimizer(opt_name, params,
                             SimpleNamespace(lr=spec.lr, weight_decay=r.weight_decay))
        if mode == "start":
            torch.manual_seed(spec.seed)
        else:
            opt.load_state_dict(torch.load(rdir / "optimizer.pt", weights_only=False))
            _set_rng_state(torch.load(rdir / "rng.pt", weights_only=False))
        opt_entries_at_start = len(opt.state)
        if mode == "start" and opt_entries_at_start != 0:
            raise AssertionError("a new segment's optimizer state is not empty")

        def meta_now(k: int) -> dict:
            cont = (continuity_logprobs(model, spec.continuity)
                    if spec.continuity is not None else None)
            return {**base_meta, "heldout": heldout, "probe_loss_before": probe_before,
                    "distance_ref": ref_label, "continuity_logprobs": cont,
                    "attempt": attempt, "attempt_reasons": list(reasons),
                    "continuity_item_sha256": (_json_sha256(spec.continuity)
                                               if spec.continuity is not None else None)}

        if mode == "start":
            _save_resume(opt_root, 0, model, opt, meta_now(0))
        saves_done = {s: key for s, key in spec.saves.items() if s <= k0}
        if mode == "resume" and k0 in spec.saves:  # killed between the resume point and ckpt.json
            key = spec.saves[k0]
            try:
                ck = read_ckpt(r.name, key, root)
            except FileNotFoundError:
                ck = {}
            if ck.get("run") != spec.run or ck.get("local_step") != k0:
                _save_ckpt(root, spec, model, k0, key, plan, meta["optimizer_state_sha256"],
                           ref_dir, ref_label, opt_name)
        summary = {
            "run": spec.run, "recipe": r.name, "mode": mode, "segment": spec.segment,
            "arm": spec.arm, "parent_key": spec.parent_key, "seed": spec.seed,
            "order_seed": spec.order_seed, "init_seed": spec.init_seed,
            "data_ids_sha256": plan.data_ids_sha256, "steps_planned": spec.steps,
            "step_offset": spec.step_offset, "lr": spec.lr, "schedule": spec.schedule,
            "warmup_steps": r.warmup_steps, "optimizer": opt_name,
            "optimizer_state_entries_at_start": opt_entries_at_start,
            "max_grad_norm": r.max_grad_norm, "per_device_batch": r.per_device_batch,
            "grad_accum": r.grad_accum, "gradient_checkpointing": spec.gradient_checkpointing,
            "n_trainable": QT.n_trainable(model), "mask": plan.mask, "attn": "eager",
            "probe_loss_before": probe_before, "endpoint": spec.endpoint,
            "loss_reduction": "step_token_mean (sum CE over the window / window trained tokens)",
            "utc_start": _utc(),
        }
        if mode == "resume":
            summary["resumed_from"] = resumed_from
        k = k0
        try:
            for k in range(k0, spec.steps):
                step = k + 1
                window = plan.windows[k]
                flat = [ex for mb in window for ex in mb]
                n_win = QT.n_loss_tokens(flat)
                lr = spec.lr * lr_multiplier(spec.schedule, k, r.warmup_steps, spec.steps)
                for g in opt.param_groups:
                    g["lr"] = lr
                opt.zero_grad(set_to_none=True)
                if n_win == 0:
                    raise NonFiniteStep(step, float("nan"), float("nan"), 0)
                QT._sync(dev)
                t0 = time.perf_counter()
                loss = _window_loss(model, window, n_win, dev)
                gn = float(torch.nn.utils.clip_grad_norm_(params, r.max_grad_norm))
                if not (math.isfinite(loss) and math.isfinite(gn)):
                    opt.zero_grad(set_to_none=True)
                    raise NonFiniteStep(step, loss, gn, n_win)
                opt.step()
                opt.zero_grad(set_to_none=True)
                QT._sync(dev)
                rec = {"step": step, "cumulative_step": spec.step_offset + step, "loss": loss,
                       "grad_norm": gn, "lr": lr, "n_tokens": sum(len(i) for i, _ in flat),
                       "n_loss_tokens": n_win, "seconds": time.perf_counter() - t0,
                       "mode": mode}
                if heldout_ex and (step % C.HELDOUT_EVERY == 0 or step == spec.steps):
                    rec["heldout_loss"] = _heldout_examples(model, heldout_ex,
                                                            r.per_device_batch)
                    heldout.append({"step": step, "loss": rec["heldout_loss"]})
                _append_jsonl(steps_path, rec)
                if step in spec.saves or step % C.RESUME_EVERY == 0:
                    opt_sha = _save_resume(opt_root, step, model, opt, meta_now(step))
                    if step in spec.saves:
                        _save_ckpt(root, spec, model, step, spec.saves[step], plan, opt_sha,
                                   ref_dir, ref_label, opt_name)
                        saves_done[step] = spec.saves[step]
        except NonFiniteStep as e:
            write_json(summary_path, {
                **summary, "steps_done": k, "saves": saves_done, "heldout": heldout,
                "probe_loss_after": None, "status": "nonfinite",
                "failure": {"step": e.step, "loss": e.loss, "grad_norm": e.grad_norm,
                            "n_loss_tokens": e.n_loss_tokens},
                "utc_end": _utc(),
            })
            raise
        probe_after = (_heldout_examples(model, probe_ex, r.per_device_batch)
                       if probe_ex else None)
        summary.update({
            "steps_done": spec.steps, "saves": dict(spec.saves), "heldout": heldout,
            "probe_loss_after": probe_after, "status": "ok", "utc_end": _utc(),
        })
        write_json(summary_path, summary)
        return summary
    finally:
        if model is not None:
            if gc_on:
                model.gradient_checkpointing_disable()
                base_model.disable_input_require_grads()
            model.unload()


def start_segment(base_model, tokenizer, spec: SegmentSpec, root: str | Path = C.REPO_ROOT
                  ) -> dict:
    """Train one segment from its parent (or a fresh adapter). Returns the summary written to
    `contracts.train_summary(recipe, run)`: {run, recipe, mode: "start", parent_key, seed,
    order_seed, data_ids_sha256, steps_done, saves: {step: key}, heldout: [{step, loss}],
    probe_loss_before, probe_loss_after, mask: mask_counts, attn: "eager", status}. Leaves
    `base_model` free of LoRA layers when it returns (unload)."""
    return _train(base_model, tokenizer, spec, Path(root), "start")


def resume_segment(base_model, tokenizer, spec: SegmentSpec, root: str | Path = C.REPO_ROOT
                   ) -> dict:
    """Continue `spec.run` from its last resume point; same return as `start_segment` with
    mode "resume" and `resumed_from` (the local step)."""
    return _train(base_model, tokenizer, spec, Path(root), "resume")


def run_segment(base_model, tokenizer, spec: SegmentSpec, root: str | Path = C.REPO_ROOT
                ) -> dict:
    """Train `spec.run` to completion, resuming where an earlier process left off (review r1
    finding 4). Writes `contracts.status_path(recipe, run)`: {run, recipe, arm, seed, segment,
    endpoint, status: ok | restarted_ok | failed, attempts, reason, utc}. Returns the summary
    (status "failed" after the second failure: it does not raise NonFiniteStep).

    - A run whose status record is `failed` is never retrained: its summary (or a minimal record)
      is returned with that status and nothing is trained or written.
    - A run with a complete resume point of this spec (`resume_point`) and no terminal failure
      resumes from it (optimizer state kept), with the attempt count and reasons stored in that
      resume point: a controller relaunch or an OOM re-exec neither restarts training nor resets
      the retry allowance. Otherwise the run starts fresh (`start_segment` clears another spec's
      stale state).
    - On `NonFiniteStep` the run resumes from its last resume point while attempts <
      `contracts.MAX_ATTEMPTS`; the new count is recorded before the retry begins.

    A `ContinuityError` (wrong parent or resume point) also writes status "failed" and is then
    re-raised; other exceptions (e.g. CUDA OOM, handled by the driver's re-exec, or a kill) write
    nothing, so the next invocation resumes."""
    root = Path(root)
    r = spec.recipe
    status_file = root / C.status_path(r.name, spec.run)
    summary_path = root / C.train_summary(r.name, spec.run)
    opt_root = _resolve(root, C.opt_dir(r.name, spec.run))

    def write_status(status: str, attempts: int, reason: str | None) -> None:
        write_json(status_file, {
            "run": spec.run, "recipe": r.name, "arm": spec.arm, "seed": spec.seed,
            "segment": spec.segment, "endpoint": spec.endpoint, "status": status,
            "attempts": attempts, "reason": reason, "utc": _utc(),
        })

    old = json.loads(status_file.read_text()) if status_file.is_file() else None
    if old and old.get("status") == "failed":
        out = json.loads(summary_path.read_text()) if summary_path.is_file() else {
            "run": spec.run, "recipe": r.name}
        return {**out, "status": "failed", "attempts": old.get("attempts"),
                "reason": old.get("reason"), "not_retrained": True}

    point = resume_point(spec, root)
    if point is not None:
        mode = "resume"
        attempt = max(1, int(point.get("attempt") or 1))
        reasons: list[str] = list(point.get("attempt_reasons") or [])
    else:
        mode, attempt, reasons = "start", 1, []
    out = None
    while True:
        try:
            out = _train(base_model, tokenizer, spec, root, mode, attempt, reasons)
            break
        except NonFiniteStep as e:
            reasons.append(f"attempt {attempt}: {e}")
            if attempt >= C.MAX_ATTEMPTS:
                break
            attempt += 1
            mode = "resume"
            _record_attempt(opt_root, attempt, reasons)  # before the retry starts
        except ContinuityError as e:
            reasons.append(f"attempt {attempt}: continuity: {e}")
            write_status("failed", attempt, "; ".join(reasons))
            raise
    if out is None:
        status = "failed"
        out = json.loads(summary_path.read_text())
    else:
        status = "ok" if attempt == 1 else "restarted_ok"
    reason = "; ".join(reasons) or None
    out.update({"status": status, "attempts": attempt, "reason": reason})
    write_json(summary_path, out)
    write_status(status, attempt, reason)
    return out
