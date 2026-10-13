"""P15 Stage B: continue training the Stage A CoDI checkpoint on chain task C (plan.md, Setup).

Usage (pod, pinned venv):
    python -m p15.train_chain --upstream /ws/codi_upstream --base <Llama-3.2-1B-Instruct dir> \
        --ckpt <dir with pytorch_model.bin> --train-jsonl data/b_train.jsonl \
        --val-jsonl data/b_val.jsonl --out results/B [--epochs 2] [--max-steps N] [--bench]

The training code is upstream `cywinski/codi` @ f0bf2fa (`train.py`, `src/model.py`,
`src/datasets.py`), which has no license: it is imported at run time from `--upstream` (or env
`P15_UPSTREAM`) and never copied here. What is reimplemented here: the step / step_ratio injection
that upstream's `CustomTrainer.compute_loss` does, the LoRA setup of `train.py`, and the tokenizer
setup of `train.py`. Hyperparameters are read from upstream's `configs/llama1b_gsm8k-aug-nl.yaml`
at run time and overridden as the plan says (OVERRIDES below).

Outputs in `--out`: `trainables.safetensors` (every requires_grad parameter, i.e. LoRA + prj, keyed
by `CODI.state_dict()` names; a delta on top of the Stage A checkpoint, which must be loaded first
because the resized embedding rows for [PAD]/<|bocot|>/<|eocot|> come from it) and
`train_record.json` (written on failure too, flushed every held-out pass). `--bench` (run with its
own `--out`, e.g. results/B/bench) writes the same record plus `bench_projection` and deletes its
timing save. `--decide-epochs RECORD --hours-left H --eval-hours E` prints only the epoch count
(2, 1 or 0) to stdout for run.sh. No `emx` import: this runs on the pod.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path

SEED = 20261012
YAML = "configs/llama1b_gsm8k-aug-nl.yaml"
TAG = "P15TRAIN"
SUFFIX = " Output only the answer and nothing else."
LLAMA_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "up_proj", "down_proj", "gate_proj"]
# Upstream yaml keys we do not take (paths, hub, wandb, checkpointing); replaced below.
DROP = {"output_dir", "expt_name", "logging_dir", "report_to", "save_strategy",
        "save_total_limit", "save_safetensors", "max_steps", "num_train_epochs"}
OVERRIDES = {
    "learning_rate": 2e-4, "seed": SEED, "save_strategy": "no", "report_to": [],
    "print_loss": False, "per_device_train_batch_size": 32, "logging_steps": 10,
    "answer_only": True, "include_last_cot": False, "remove_eos": False, "max_token_num": 256,
    "use_prj": True, "prj_dim": 2048, "distill_loss_factor": 20, "distill_loss_div_std": True,
    "num_latent": 6, "dataloader_num_workers": 2, "gradient_checkpointing": False,
    "disable_tqdm": True,
}
COMPONENTS = ("ce_loss", "distill_loss", "ref_ce_loss", "ans_ce_loss")


def log(msg: str) -> None:
    print(f"{TAG} {msg}", flush=True)


def import_upstream(upstream: str):
    """Upstream `src.model` via codi_core (same class and import path as inference), then
    `src.datasets` from the same clone, checked."""
    from p15 import codi_core

    up_model = codi_core.import_upstream(upstream)
    import src.datasets as up_data

    want = Path(up_model.__file__).resolve().parent / "datasets.py"
    if Path(up_data.__file__).resolve() != want:
        raise ImportError(f"src.datasets resolved to {up_data.__file__}, not {want}")
    return up_model, up_data


def upstream_commit(upstream: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", upstream, "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=10, check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def read_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def clean_rows(rows: list[dict]) -> list[dict]:
    """icot_full rows {question, cot, answer} as strings; refuse a question that has the suffix."""
    out = []
    for r in rows:
        q = str(r["question"])
        if q.rstrip().endswith(SUFFIX.strip()):
            raise ValueError("train/val question already ends with the answer-only suffix; "
                             "upstream appends it itself (would double it)")
        ans = r.get("answer")
        out.append({"question": q, "cot": str(r["cot"]),
                    "answer": None if ans is None else str(ans)})
    return out


def drop_reasons(rows: list[dict], tokenizer, max_token_num: int) -> dict:
    """Why upstream's icot_full branch would skip each row (same three checks, counted)."""
    n = {"answer_none": 0, "too_long": 0, "answer_not_digit": 0}
    for r in rows:
        if r["answer"] is None:
            n["answer_none"] += 1
        elif len(tokenizer.encode(r["question"] + r["cot"] + r["answer"])) > max_token_num:
            n["too_long"] += 1
        elif not r["answer"].split(" ")[-1][:1].isdigit():
            n["answer_not_digit"] += 1
    return n


def build_tokenizer(base: str, model_max_length: int):
    """As upstream train.py: right padding, slow-if-available, [PAD], <|bocot|>/<|eocot|>."""
    import transformers

    kw = {"model_max_length": model_max_length, "padding_side": "right"}
    tok = transformers.AutoTokenizer.from_pretrained(base, use_fast=False, **kw)
    if not isinstance(tok, transformers.PreTrainedTokenizerBase):
        # Llama 3 has no slow tokenizer; a tokenizer re-saved by transformers 4.x names its class
        # "PreTrainedTokenizer" and use_fast=False then returns a bool. The real base gives the
        # fast tokenizer either way, so load it directly.
        tok = transformers.AutoTokenizer.from_pretrained(base, use_fast=True, **kw)
    if tok.pad_token_id is None:
        tok.add_special_tokens({"pad_token": "[PAD]"})
        tok.pad_token_id = tok.convert_tokens_to_ids("[PAD]")
    tok.add_special_tokens({"additional_special_tokens": ["<|bocot|>", "<|eocot|>"]})
    tok.bot_id = tok.convert_tokens_to_ids("<|bocot|>")
    tok.eot_id = tok.convert_tokens_to_ids("<|eocot|>")
    return tok


def check_tokenizer(tok, ckpt: str, base: str) -> dict:
    """Training builds its tokenizer from the base (train.py); inference (codi_core) reads the
    checkpoint's. Fail unless the vocab size and the three added ids agree."""
    from p15 import codi_core

    ref = codi_core.load_tokenizer(resolve_ckpt(ckpt).parent, base)
    mine = {"len": len(tok), "pad": tok.pad_token_id, "bocot": tok.bot_id, "eocot": tok.eot_id}
    theirs = {"len": len(ref), "pad": ref.pad_token_id, "bocot": ref.bocot_id,
              "eocot": ref.eocot_id}
    if mine != theirs:
        raise RuntimeError(f"tokenizer mismatch: train {mine} vs codi_core {theirs}")
    return mine


def resolve_ckpt(ckpt: str) -> Path:
    p = Path(ckpt)
    if p.is_file():
        return p
    for name in ("pytorch_model.bin", "model.safetensors"):
        if (p / name).exists():
            return p / name
    raise FileNotFoundError(f"no pytorch_model.bin or model.safetensors in {ckpt}")


def load_checkpoint(model, ckpt: str) -> dict:
    """Load the Stage A state dict (strict=False) and fail unless LoRA, prj and embeddings load."""
    import torch

    path = resolve_ckpt(ckpt)
    if path.suffix == ".safetensors":
        from safetensors.torch import load_file

        sd = load_file(str(path))
    else:
        sd = torch.load(str(path), map_location="cpu", weights_only=True)
    res = model.load_state_dict(sd, strict=False)
    missing, unexpected = list(res.missing_keys), list(res.unexpected_keys)
    must = [k for k in model.state_dict() if "lora_" in k or k.startswith("prj.")]
    embed = [k for k in model.state_dict() if k.endswith("embed_tokens.weight")]
    miss_must = [k for k in must + embed if k in missing]
    report = {"ckpt_file": str(path), "ckpt_bytes": path.stat().st_size, "n_ckpt_keys": len(sd),
              "n_model_keys": len(model.state_dict()), "n_missing": len(missing),
              "n_unexpected": len(unexpected), "n_required": len(must) + len(embed),
              "n_required_missing": len(miss_must), "missing_sample": missing[:10],
              "unexpected_sample": unexpected[:10], "required_missing_sample": miss_must[:10]}
    if miss_must or unexpected or not must:
        raise RuntimeError(f"Stage A checkpoint does not cover the model: {json.dumps(report)}")
    return report


def trainable_state(model) -> dict:
    return {n: p.detach().to("cpu").contiguous() for n, p in model.named_parameters()
            if p.requires_grad}


def sync() -> None:
    import torch

    if torch.cuda.is_available():
        torch.cuda.synchronize()


def mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    return sum(xs) / len(xs) if xs else None


def loss_flag(steps: list[dict]) -> dict:
    """Plan rule: mean total loss over steps 451-500 < steps 1-50 (else first vs last 10%)."""
    losses = [s["loss"] for s in steps]
    if len(losses) >= 500:
        a, b, how = losses[:50], losses[450:500], "steps 1-50 vs 451-500"
    else:
        k = max(1, len(losses) // 10)
        a, b, how = losses[:k], losses[-k:], f"first vs last {k} of {len(losses)} steps"
    ma, mb = mean(a), mean(b)
    ok = ma is not None and mb is not None and mb < ma
    return {"loss_decreasing": bool(ok), "early_mean": ma, "late_mean": mb, "window": how}


def heldout_pass(model, loader, bf16: bool) -> dict:
    """Mean total loss and components over the val set (row-weighted), eval mode, no grad."""
    import torch

    dev = next(model.parameters()).device
    was_training = model.training
    model.eval()
    sums = {k: 0.0 for k in ("loss",) + COMPONENTS}
    n = 0
    sync()
    t0 = time.perf_counter()
    ctx = (torch.autocast("cuda", dtype=torch.bfloat16) if bf16 and dev.type == "cuda"
           else torch.autocast("cpu", enabled=False))
    with torch.no_grad(), ctx:
        for batch in loader:
            batch = {k: v.to(dev) for k, v in batch.items()}
            out = model(**batch)
            b = batch["encoder_input_ids_ans"].size(0)
            sums["loss"] += float(out["loss"]) * b
            for k in COMPONENTS:
                sums[k] += float(out[k]) * b
            n += b
            del out
    sync()
    dt = time.perf_counter() - t0
    if was_training:
        model.train()
    res = {k: v / max(n, 1) for k, v in sums.items()}
    res.update({"n": n, "seconds": dt})
    return res


def make_trainer_classes():
    import torch
    import transformers

    class ChainTrainer(transformers.Trainer):
        """Upstream CustomTrainer's step / step_ratio injection, reimplemented; stashes losses."""

        def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None,
                         **kwargs):
            step = self.state.global_step
            a = self.args
            eff = a.per_device_train_batch_size * a.world_size * a.gradient_accumulation_steps
            total = math.ceil(len(self.train_dataset) / eff) * a.num_train_epochs
            inputs["step_ratio"] = step / total
            inputs["step"] = step
            out = model(**inputs)
            loss = out["loss"]
            self._p15_last = {"loss": float(loss.detach().item()),
                              **{k: float(out[k]) for k in COMPONENTS}}
            return (loss, out) if return_outputs else loss

    class StepCallback(transformers.TrainerCallback):
        """Times every optimizer step (cuda-synchronised) and runs held-out passes."""

        def __init__(self, run):
            self.run = run
            self.t_last = None

        def on_train_begin(self, args, state, control, **kw):
            sync()
            self.t_last = time.perf_counter()

        def on_step_end(self, args, state, control, **kw):
            sync()
            now = time.perf_counter()
            r = self.run
            last = getattr(r.trainer, "_p15_last", {}) or {}
            sched = kw.get("lr_scheduler")
            lr = float(sched.get_last_lr()[0]) if sched is not None else None
            entry = {"step": state.global_step, **last, "lr": lr, "step_s": now - self.t_last}
            r.record["steps"].append(entry)
            if state.global_step % args.logging_steps == 0:
                log(f"step={state.global_step} loss={last.get('loss', float('nan')):.4f} "
                    f"ce={last.get('ce_loss', float('nan')):.4f} "
                    f"distill={last.get('distill_loss', float('nan')):.4f} "
                    f"ref_ce={last.get('ref_ce_loss', float('nan')):.4f} "
                    f"ans_ce={last.get('ans_ce_loss', float('nan')):.4f} "
                    f"s_per_step={entry['step_s']:.3f}")
            if r.heldout_every and state.global_step % r.heldout_every == 0:
                r.do_heldout(state.global_step)
                r.flush()
            sync()
            self.t_last = time.perf_counter()

    return ChainTrainer, StepCallback, torch


class Run:
    """Holds the record and the held-out loader; the callback calls back into it."""

    def __init__(self, record: dict, record_path: Path, val_loader, bf16: bool,
                 heldout_every: int):
        self.record = record
        self.record_path = record_path
        self.val_loader = val_loader
        self.bf16 = bf16
        self.heldout_every = heldout_every
        self.trainer = None
        self.model = None

    def do_heldout(self, step: int) -> dict:
        res = heldout_pass(self.model, self.val_loader, self.bf16)
        res["step"] = step
        self.record["heldout"].append(res)
        self.record["phases"]["heldout_s"] = self.record["phases"].get("heldout_s", 0.0) \
            + res["seconds"]
        log(f"step={step} heldout={res['loss']:.4f} ce={res['ce_loss']:.4f} "
            f"distill={res['distill_loss']:.4f} heldout_s={res['seconds']:.1f}")
        return res

    def flush(self) -> None:
        tmp = self.record_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.record, indent=1, default=str))
        os.replace(tmp, self.record_path)


def projection(record: dict, n_train: int, batch: int, epochs: int, heldout_every: int,
               skip: int = 5) -> dict:
    """Projected full-training wall-clock from the bench: steps, held-out passes and the save."""
    st = [s["step_s"] for s in record["steps"][skip:]]
    step_s = mean(st)
    ho = mean([h["seconds"] for h in record["heldout"]]) or 0.0
    save_s = record["phases"].get("save_s", 0.0)
    out = {"mean_step_s": step_s, "n_steps_timed": len(st), "heldout_pass_s": ho,
           "save_s": save_s}
    for ep in sorted({epochs, 1, 2}):
        steps = math.ceil(n_train / batch) * ep
        n_ho = steps // heldout_every + (0 if steps % heldout_every == 0 else 1)
        sec = steps * (step_s or 0.0) + n_ho * ho + save_s
        out[f"epochs_{ep}"] = {"steps": steps, "heldout_passes": n_ho, "seconds": sec,
                               "hours": sec / 3600}
    out["projection_h"] = out[f"epochs_{epochs}"]["hours"]
    out["projection_h_2ep"] = out["epochs_2"]["hours"]
    out["epochs_rule"] = 2 if out["projection_h_2ep"] <= 1.5 else 1
    out["rule"] = "epochs_rule = 2 if projected 2-epoch training <= 1.5 h else 1 (plan.md)"
    return out


def train(upstream: str, base: str, ckpt: str, train_jsonl: str, val_jsonl: str, out: str,
          epochs: int = 2, max_steps: int | None = None, lr: float = 2e-4, batch: int = 32,
          bench: bool = False, bench_steps: int = 60, lora_r: int = 128, lora_alpha: int = 32,
          prj_dim: int = 2048, heldout_every: int = 250, logging_steps: int = 10,
          num_workers: int = 2) -> dict:
    """Continue training the Stage A CODI checkpoint; return the record (also written to out)."""
    os.environ.setdefault("WANDB_MODE", "offline")
    os.environ.setdefault("WANDB_DISABLED", "true")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    record_path = outdir / "train_record.json"
    record: dict = {"status": "started", "bench": bench, "started_utc": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "phases": {}, "steps": [], "heldout": [],
        "args": {"upstream": upstream, "base": base, "ckpt": ckpt, "train_jsonl": train_jsonl,
                 "val_jsonl": val_jsonl, "out": out, "epochs": epochs, "max_steps": max_steps,
                 "lr": lr, "batch": batch, "bench": bench, "bench_steps": bench_steps,
                 "lora_r": lora_r, "lora_alpha": lora_alpha, "prj_dim": prj_dim,
                 "heldout_every": heldout_every, "logging_steps": logging_steps,
                 "num_workers": num_workers}}
    run = Run(record, record_path, None, False, 0 if bench else heldout_every)
    try:
        _train(run, upstream, base, ckpt, train_jsonl, val_jsonl, outdir, epochs, max_steps, lr,
               batch, bench, bench_steps, lora_r, lora_alpha, prj_dim, logging_steps,
               num_workers)
        record["status"] = "done"
    except BaseException as e:
        record["status"] = "failed"
        record["error"] = f"{type(e).__name__}: {e}"
        record["traceback"] = traceback.format_exc()
        raise
    finally:
        record["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        run.flush()
    return record


def _train(run: Run, upstream, base, ckpt, train_jsonl, val_jsonl, outdir: Path, epochs,
           max_steps, lr, batch, bench, bench_steps, lora_r, lora_alpha, prj_dim,
           logging_steps, num_workers) -> None:
    import yaml

    rec = run.record
    ph = rec["phases"]
    up_model, up_data = import_upstream(upstream)
    ChainTrainer, StepCallback, torch = make_trainer_classes()
    import peft
    import transformers
    from peft import LoraConfig, TaskType
    from safetensors.torch import save_file

    cuda = torch.cuda.is_available()
    rec["env"] = {"torch": torch.__version__, "transformers": transformers.__version__,
                  "peft": peft.__version__, "python": platform.python_version(),
                  "gpu": torch.cuda.get_device_name(0) if cuda else None,
                  "upstream_commit": upstream_commit(upstream)}
    try:
        import accelerate
        import datasets as hf_datasets

        rec["env"].update(accelerate=accelerate.__version__, datasets=hf_datasets.__version__)
    except ImportError as e:
        rec["env"]["version_error"] = str(e)
    log(f"env {json.dumps(rec['env'])}")

    # --- hyperparameters: upstream yaml, then the plan's overrides
    cfg = yaml.safe_load((Path(upstream) / YAML).read_text())
    targs = {k: v for k, v in cfg["training_args"].items() if k not in DROP}
    targs.update(OVERRIDES)
    targs.update(learning_rate=float(lr), per_device_train_batch_size=batch,
                 logging_steps=logging_steps, prj_dim=prj_dim, num_train_epochs=epochs,
                 max_steps=bench_steps if bench else (max_steps or -1),
                 dataloader_num_workers=num_workers, bf16=cuda,
                 dataloader_pin_memory=cuda, use_cpu=not cuda,
                 output_dir=str(outdir / "_trainer"), logging_dir=str(outdir / "_trainer"))
    # TrainingArguments(bf16=True) exports ACCELERATE_MIXED_PRECISION=bf16 and bf16=False never
    # resets it; an earlier bf16 build in this process (e.g. a test fixture) then makes the Trainer
    # autocast to bf16 on CPU (~50x slower). Pin it to what this run wants.
    os.environ["ACCELERATE_MIXED_PRECISION"] = "bf16" if cuda else "no"
    fields = {f.name for f in dataclasses.fields(up_model.TrainingArguments)}
    if "warmup_ratio" not in fields and "warmup_ratio" in targs:  # transformers 5.x only
        targs["warmup_steps"] = float(targs.pop("warmup_ratio"))  # a float < 1 is a ratio there
    unknown = sorted(k for k in targs if k not in fields)  # empty under the pinned 4.52
    for k in unknown:
        targs.pop(k)
    training_args = up_model.TrainingArguments(**targs)
    model_args = up_model.ModelArguments(
        model_name_or_path=base, lora_r=lora_r, lora_alpha=lora_alpha, lora_init=True,
        full_precision=True, train=True, token=None)
    data_args = up_model.DataArguments(data_names=["icot_full"], max_samples=None)
    lora = {"r": lora_r, "lora_alpha": lora_alpha, "lora_dropout": 0.1,
            "target_modules": LLAMA_TARGETS, "init_lora_weights": True}
    rec["hyperparameters"] = {
        "training_args": json.loads(training_args.to_json_string()),
        "model_args": {k: v for k, v in vars(model_args).items() if k != "token"},
        "data_args": vars(data_args), "lora": lora, "seed": SEED, "upstream_yaml": YAML,
        "dropped_unknown_args": unknown,
        "overrides": {**OVERRIDES, "learning_rate": float(lr), "per_device_train_batch_size":
                      batch, "prj_dim": prj_dim, "bf16": cuda}}

    # --- build (tokenizer + CODI), as upstream train.py
    transformers.set_seed(SEED)
    sync()
    t0 = time.perf_counter()
    tok = build_tokenizer(base, training_args.model_max_length)
    rec["tokenizer_check"] = check_tokenizer(tok, ckpt, base)
    lora_config = LoraConfig(task_type=TaskType.CAUSAL_LM, inference_mode=False, **lora)
    model = up_model.CODI(model_args, training_args, lora_config, tok)
    if not cuda:
        model = model.float()  # upstream builds fp16 when bf16 is off; fp32 for CPU tests
    rec["model"] = {"len_tokenizer": len(tok), "pad_id": tok.pad_token_id, "bot_id": tok.bot_id,
                    "eot_id": tok.eot_id, "param_dtype": str(next(model.parameters()).dtype),
                    "n_trainable": sum(p.numel() for p in model.parameters() if p.requires_grad),
                    "n_params": sum(p.numel() for p in model.parameters())}
    ph["build_s"] = time.perf_counter() - t0

    # --- load the Stage A checkpoint
    t0 = time.perf_counter()
    rec["ckpt_load"] = load_checkpoint(model, ckpt)
    model.codi.tie_weights()
    ph["load_s"] = time.perf_counter() - t0
    log(f"ckpt loaded {json.dumps(rec['ckpt_load'])[:400]}")

    # --- data
    t0 = time.perf_counter()
    train_rows = clean_rows(read_jsonl(train_jsonl))
    val_rows = clean_rows(read_jsonl(val_jsonl))
    train_ds = up_data.SupervisedDataset("icot_full", train_rows, tok, training_args, data_args)
    val_ds = up_data.SupervisedDataset("icot_full", val_rows, tok, training_args, data_args)
    collator = up_data.DataCollatorForSupervisedDataset(tokenizer=tok)
    rec["data"] = {
        "train_rows": len(train_rows), "train_kept": len(train_ds),
        "train_dropped": len(train_rows) - len(train_ds),
        "train_drop_reasons": drop_reasons(train_rows, tok, training_args.max_token_num),
        "val_rows": len(val_rows), "val_kept": len(val_ds),
        "val_dropped": len(val_rows) - len(val_ds),
        "val_drop_reasons": drop_reasons(val_rows, tok, training_args.max_token_num),
        "steps_per_epoch": math.ceil(len(train_ds) / batch)}
    if len(train_ds) == 0 or len(val_ds) == 0:
        raise RuntimeError(f"empty dataset after upstream filters: {rec['data']}")
    run.val_loader = torch.utils.data.DataLoader(val_ds, batch_size=batch, shuffle=False,
                                                 collate_fn=collator, num_workers=0)
    ph["data_s"] = time.perf_counter() - t0
    log(f"data {json.dumps(rec['data'])}")

    # --- train
    run.model = model
    run.bf16 = cuda
    trainer = ChainTrainer(model=model, args=training_args, train_dataset=train_ds,
                           data_collator=collator, callbacks=[StepCallback(run)])
    run.trainer = trainer
    rec["status"] = "training"
    run.flush()
    sync()
    t0 = time.perf_counter()
    trainer.train()
    sync()
    ph["train_s"] = time.perf_counter() - t0  # includes in-loop held-out passes
    last = rec["steps"][-1]["step"] if rec["steps"] else 0
    rec["steps_run"] = last
    rec["epochs_run"] = last / rec["data"]["steps_per_epoch"]
    if not rec["heldout"] or rec["heldout"][-1]["step"] != last:
        run.do_heldout(last)

    # --- save trainables (LoRA + prj)
    t0 = time.perf_counter()
    sd = trainable_state(model)
    keys = set(model.state_dict())
    bad = [k for k in sd if k not in keys]
    if bad:
        raise RuntimeError(f"trainable names not in CODI.state_dict(): {bad[:5]}")
    save_path = outdir / ("bench_trainables.safetensors" if bench else "trainables.safetensors")
    save_file(sd, str(save_path))
    ph["save_s"] = time.perf_counter() - t0
    rec["trainables"] = {"path": str(save_path), "n_tensors": len(sd),
                         "n_params": sum(v.numel() for v in sd.values()),
                         "bytes": save_path.stat().st_size,
                         "n_lora": sum("lora_" in k for k in sd),
                         "n_prj": sum(k.startswith("prj.") for k in sd)}
    rec["loss_flag"] = loss_flag(rec["steps"])
    rec["loss_decreasing"] = rec["loss_flag"]["loss_decreasing"]
    if bench:
        save_path.unlink()
        rec["trainables"]["deleted_after_timing"] = True
        rec["bench_projection"] = projection(rec, len(train_ds), batch, epochs,
                                             rec["args"]["heldout_every"])
        log(f"bench {json.dumps(rec['bench_projection'])}")
    log(f"done steps={last} loss_flag={json.dumps(rec['loss_flag'])} phases={json.dumps(ph)}")


def decide_epochs(bench_record: dict, hours_left: float, eval_hours: float) -> tuple[int, dict]:
    """Plan rule (2 epochs if the 2-epoch projection <= 1.5 h, else 1), then the deadline:
    drop to 1 if projected(epochs) + eval_hours > hours_left, and to 0 if 1 epoch doesn't fit."""
    p = bench_record["bench_projection"]
    h = {1: p["epochs_1"]["hours"], 2: p["epochs_2"]["hours"]}
    ep = 2 if h[2] <= 1.5 else 1
    rule_ep = ep
    if ep == 2 and h[2] + eval_hours > hours_left:
        ep = 1
    if ep == 1 and h[1] + eval_hours > hours_left:
        ep = 0
    return ep, {"proj_h_1ep": h[1], "proj_h_2ep": h[2], "rule_epochs": rule_ep,
                "hours_left": hours_left, "eval_hours": eval_hours, "epochs": ep}


def resolve_paths(a) -> tuple[str, str]:
    if a.base_from:
        paths = json.loads(Path(a.base_from).read_text())
        base, ckpt = a.base or paths["base"], a.ckpt or paths["ckpt"]
    else:
        base, ckpt = a.base, a.ckpt
    if not base or not ckpt:
        raise SystemExit("need --base and --ckpt, or --base-from PATHS_JSON")
    return base, ckpt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--upstream", default=os.environ.get("P15_UPSTREAM", "/ws/codi_upstream"))
    ap.add_argument("--base", help="base model dir or id")
    ap.add_argument("--ckpt", help="Stage A checkpoint dir (pytorch_model.bin) or file")
    ap.add_argument("--base-from", help='JSON {"base": dir, "ckpt": dir} instead of --base/--ckpt')
    ap.add_argument("--train-jsonl")
    ap.add_argument("--val-jsonl")
    ap.add_argument("--out")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--bench-steps", type=int, default=60)
    ap.add_argument("--heldout-every", type=int, default=250)
    ap.add_argument("--num-workers", type=int, default=2)
    ap.add_argument("--lora-r", type=int, default=128, help="tests only; the checkpoint's is 128")
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--prj-dim", type=int, default=2048)
    ap.add_argument("--decide-epochs", metavar="BENCH_RECORD",
                    help="print only the epoch count (2, 1 or 0) and exit; no training")
    ap.add_argument("--hours-left", type=float)
    ap.add_argument("--eval-hours", type=float, default=0.5)
    a = ap.parse_args(argv)
    if a.decide_epochs:
        if a.hours_left is None:
            ap.error("--decide-epochs needs --hours-left")
        ep, info = decide_epochs(json.loads(Path(a.decide_epochs).read_text()), a.hours_left,
                                 a.eval_hours)
        print(f"{TAG} decide_epochs {json.dumps(info)}", file=sys.stderr, flush=True)
        print(ep, flush=True)
        return 0
    for name in ("train_jsonl", "val_jsonl", "out"):
        if not getattr(a, name):
            ap.error(f"--{name.replace('_', '-')} is required")
    base, ckpt = resolve_paths(a)
    rec = train(a.upstream, base, ckpt, a.train_jsonl, a.val_jsonl, a.out,
                epochs=a.epochs, max_steps=a.max_steps, lr=a.lr, batch=a.batch, bench=a.bench,
                bench_steps=a.bench_steps, heldout_every=a.heldout_every,
                num_workers=a.num_workers, lora_r=a.lora_r, lora_alpha=a.lora_alpha,
                prj_dim=a.prj_dim)
    if a.bench:
        p = rec["bench_projection"]
        log(f"bench projection_h={p['projection_h']:.3f} epochs_rule={p['epochs_rule']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
