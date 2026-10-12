"""P17 training loop (plan.md "Arms"): LoRA on Gemma 4 31B's text tower, answer cross-entropy
(K) or answer CE + lambda x the readout penalty (E), on items continued from the shared-prefix
cache.

Per optimizer step (EFFECTIVE_BATCH items in microbatches of `mb`): the dots100 prefix cache is
recomputed under the current adapter with no gradient; each microbatch is the items' target
turns plus their gold answer tokens (digits then <turn|>), right-padded, run from a copy of that
cache. CE is taken at the answer positions only, from the final norm's output through the LM
head path (`common.head_logits`), mean over each item's answer tokens, then over items. For E,
block outputs at the readout window (layers 30-59) stay in the graph and `penalty.readout_lp`
reads them one layer at a time under activation checkpointing.

The record follows `emx.e13.train`'s shape (per-step rows, held-out rows, phase timings) but is
written here (schema.py "train/<model>/record.json").
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import common as C
from . import penalty as PEN
from . import schema as S


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n")
    os.replace(tmp, path)


@dataclass
class TrainConfig:
    arm: str  # "K" or "E"
    seed: int
    lam: float = 0.0
    mb: int = 1
    lr: float = S.LR
    warmup: int = 10
    max_grad_norm: float = 1.0
    n_train: int = S.N_TRAIN
    val_every: int = S.VAL_EVERY
    n_val: int = S.N_VAL
    penalty_reader: str = "logit_lens"
    tau: float = S.TAU
    margin: float = S.MARGIN
    max_steps: int | None = None  # tests and the benchmark


@dataclass
class Item:
    """A training or validation item: the dots100 split plus its targets."""

    split: C.Split
    answer: list[int]  # answer token ids
    y_digit: int


def make_items(tok: Any, exs: list[dict], shots: list[dict], eot: int) -> list[Item]:
    out = []
    for ex in exs:
        sp = C.render_split(tok, ex, shots, "dots100")
        out.append(Item(sp, C.answer_ids(tok, ex["answer"], eot),
                        PEN.first_digit(ex["queried_value"])))
    return out


def batch_forward(model: Any, rd: C.Readers, prefix: Any, items: list[Item], device: str,
                  want_pen: bool, lens: Any = None, keep_graph: bool = True) -> dict:
    """One microbatch from a copy of the prefix cache: {"ce" [B], "lp" [B, 30, 102, 10] or
    None}. CE is the mean over each item's answer tokens."""
    import torch

    blocks, norm = C.text_model(model)
    seqs = [it.split.cont + it.answer for it in items]
    ids, lens_ = C.pad_right(seqs, pad=0)
    ids = ids.to(device)
    n_prompt = [len(it.split.cont) for it in items]
    # readout window: the last 115 prompt tokens of each row (continuation-relative)
    win = torch.tensor([[n - 115 + j for j in PEN.PEN_IDX] for n in n_prompt], device=device)
    cache = C.cache_for(prefix, len(items))
    layers = S.PEN_LAYERS if want_pen else ()
    with C.capture_norm(norm) as zbox, C.capture_blocks(blocks, win, keep_graph, layers) as hs:
        model(input_ids=ids, past_key_values=cache, use_cache=True, logits_to_keep=1)
    z = zbox[0]
    ces = []
    for b, it in enumerate(items):
        n, k = n_prompt[b], len(it.answer)
        pos = torch.arange(n - 1, n - 1 + k, device=device)  # predicts answer token j at n-1+j
        logits = C.head_logits(z[b, pos], rd)
        tgt = torch.tensor(it.answer, device=device)
        ces.append(torch.nn.functional.cross_entropy(logits, tgt))
    out = {"ce": torch.stack(ces), "lp": None}
    if want_pen:
        out["lp"] = PEN.readout_lp(hs, rd, S.PEN_LAYERS, lens=lens, checkpoint=keep_graph)
    del cache
    return out


def evaluate_val(model: Any, rd: C.Readers, prefix_ids: list[int], items: list[Item],
                 cfg: TrainConfig, device: str, lens: Any = None) -> dict:
    """Held-out CE and penalty (baseline = the mean over these items, as the eval uses), the
    hard statistic's hinge and hit rates per group. No gradient."""
    import torch

    with torch.no_grad():
        prefix = C.prefix_cache(model, prefix_ids, device)
        ces, lps = [], []
        for i in range(0, len(items), max(cfg.mb, 4)):
            r = batch_forward(model, rd, prefix, items[i:i + max(cfg.mb, 4)], device, True,
                              lens=lens, keep_graph=False)
            ces.append(r["ce"].float().cpu())
            lps.append(r["lp"].float())
        lp = torch.cat(lps)
        yd = torch.tensor([it.y_digit for it in items], device=lp.device)
        base = lp.mean(dim=0)
        pen, _ = PEN.smooth_penalty(lp, base, yd, cfg.tau, cfg.margin)
        lp_np, yd_np = lp.cpu().numpy(), yd.cpu().numpy()
        st = PEN.hard_stats(lp_np, base.cpu().numpy())
        hh = PEN.hard_hinge(st, yd_np)
        hits = st.argmax(axis=2) == yd_np[:, None]
    return {"ce": float(torch.cat(ces).mean()), "pen": float(pen.mean()),
            "pen_hard": float(hh.sum(axis=1).mean()),
            "hit_label": float(hits[:, 0].mean()), "hit_dots": float(hits[:, 1].mean()),
            "n": len(items)}


def calibrate(model: Any, rd: C.Readers, prefix_ids: list[int], items: list[Item], device: str,
              mb: int, lens: Any = None) -> dict:
    """On the base model (adapter disabled by the caller): mean CE, the baseline (mean digit
    log-probs over these items) and the mean penalty under it; lambda = CE / penalty, clipped."""
    import torch

    with torch.no_grad():
        prefix = C.prefix_cache(model, prefix_ids, device)
        ces, lps = [], []
        for i in range(0, len(items), max(mb, 4)):
            r = batch_forward(model, rd, prefix, items[i:i + max(mb, 4)], device, True,
                              lens=lens, keep_graph=False)
            ces.append(r["ce"].float())
            lps.append(r["lp"].float())
        lp, ce = torch.cat(lps), torch.cat(ces)
        base = lp.mean(dim=0)
        yd = torch.tensor([it.y_digit for it in items], device=lp.device)
        pen, _ = PEN.smooth_penalty(lp, base, yd)
    ce0, pen0 = float(ce.mean()), float(pen.mean())
    lo, hi = S.LAMBDA_CLIP
    lam = float(np.clip(ce0 / pen0, lo, hi)) if pen0 > 0 else hi
    return {"ce0": ce0, "pen0": pen0, "lambda": lam, "n": len(items), "baseline": base}


def lr_at(step: int, cfg: TrainConfig) -> float:
    return cfg.lr * min(1.0, (step + 1) / cfg.warmup) if cfg.warmup else cfg.lr


def train_arm(model: Any, tok: Any, rd: C.Readers, name: str, cfg: TrainConfig,
              train_items: list[Item], val_items: list[Item], prefix_ids: list[int],
              base_init: Any, out_dir: Path, device: str, lens: Any = None,
              stop_at: float | None = None, log=print) -> tuple[Any, dict]:
    """Trains adapter `name` (created here) for one epoch; returns (peft model, record)."""
    import torch

    t_all = time.time()
    model = C.add_adapter(model, name, cfg.seed)
    C.assert_no_hf_checkpointing(model)
    layout = C.lora_layout(model)
    params = C.trainable(model)
    opt = torch.optim.AdamW(params, lr=cfg.lr, weight_decay=0.0)
    order = np.random.default_rng(S.SEED + cfg.seed).permutation(len(train_items))
    n_steps = len(order) // S.EFFECTIVE_BATCH
    if cfg.max_steps is not None:
        n_steps = min(n_steps, cfg.max_steps)
    want_pen = cfg.arm == "E"
    baseline = PEN.Baseline(base_init) if want_pen else None
    rec: dict[str, Any] = {
        "model": name, "arm": cfg.arm, "seed": cfg.seed, "config": asdict(cfg),
        "lambda": cfg.lam, "layout": layout,
        "n_trainable": int(sum(p.numel() for p in params)), "steps": [], "val": [],
        "timing": {"prefix_s": 0.0, "fwd_bwd_s": 0.0, "opt_s": 0.0, "val_s": 0.0},
        "complete": False, "started": utc()}
    path = out_dir / "train" / name / "record.json"
    tim = rec["timing"]

    def val(step: int) -> None:
        t = time.time()
        model.eval()
        v = evaluate_val(model, rd, prefix_ids, val_items, cfg, device, lens)
        model.train()
        v.update(step=step, utc=utc())
        rec["val"].append(v)
        tim["val_s"] += time.time() - t
        log(f"P17_VAL {name} step {step} ce {v['ce']:.3f} pen {v['pen']:.3f} "
            f"hit label {v['hit_label']:.2f} dots {v['hit_dots']:.2f}")
        write_json(path, rec)

    model.train()
    val(0)
    for step in range(n_steps):
        if stop_at is not None and time.time() > stop_at:
            rec["stopped"] = f"deadline after {step} steps"
            log(f"P17_STOP {name}: {rec['stopped']}")
            break
        t0 = time.time()
        for g in opt.param_groups:
            g["lr"] = lr_at(step, cfg)
        idx = order[step * S.EFFECTIVE_BATCH:(step + 1) * S.EFFECTIVE_BATCH]
        t = time.time()
        prefix = C.prefix_cache(model, prefix_ids, device)
        tim["prefix_s"] += time.time() - t
        t = time.time()
        ce_sum = pen_sum = 0.0
        for i in range(0, len(idx), cfg.mb):
            mbi = [train_items[j] for j in idx[i:i + cfg.mb]]
            r = batch_forward(model, rd, prefix, mbi, device, want_pen, lens=lens)
            loss = r["ce"]
            if want_pen:
                yd = torch.tensor([it.y_digit for it in mbi], device=r["lp"].device)
                pen, _ = PEN.smooth_penalty(r["lp"], baseline.mean, yd, cfg.tau, cfg.margin)
                loss = loss + cfg.lam * pen
                pen_sum += float(pen.detach().sum())
                baseline.update(r["lp"])
            (loss.sum() / S.EFFECTIVE_BATCH).backward()
            ce_sum += float(r["ce"].detach().sum())
            del r, loss
        tim["fwd_bwd_s"] += time.time() - t
        t = time.time()
        gn = float(torch.nn.utils.clip_grad_norm_(params, cfg.max_grad_norm))
        if not np.isfinite(gn) or not np.isfinite(ce_sum) or not np.isfinite(pen_sum):
            rec["stopped"] = f"non-finite at step {step}: ce {ce_sum} pen {pen_sum} gn {gn}"
            log(f"P17_NONFINITE {name}: {rec['stopped']}")
            opt.zero_grad(set_to_none=True)
            break
        opt.step()
        opt.zero_grad(set_to_none=True)
        tim["opt_s"] += time.time() - t
        del prefix
        n = len(idx)
        row = {"step": step + 1, "ce": ce_sum / n, "pen": pen_sum / n if want_pen else None,
               "loss": (ce_sum + cfg.lam * pen_sum) / n, "grad_norm": gn,
               "lr": lr_at(step, cfg), "seconds": round(time.time() - t0, 3), "utc": utc()}
        rec["steps"].append(row)
        if (step + 1) % 10 == 0:
            log(f"P17_STEP {name} {step + 1}/{n_steps} ce {row['ce']:.3f} "
                f"pen {row['pen'] if row['pen'] is None else round(row['pen'], 3)} "
                f"gn {gn:.2f} {row['seconds']:.1f}s")
            write_json(path, rec)
        if (step + 1) % cfg.val_every == 0 and step + 1 < n_steps:
            val(step + 1)
    done_steps = len(rec["steps"])
    if done_steps and (not rec["val"] or rec["val"][-1]["step"] != done_steps):
        val(done_steps)
    rec["complete"] = done_steps == n_steps and "stopped" not in rec
    rec["seconds"] = round(time.time() - t_all, 1)
    rec["finished"] = utc()
    if torch.cuda.is_available():
        rec["peak_mem_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
    t = time.time()
    model.save_pretrained(str(out_dir / "adapters" / name), selected_adapters=[name])
    rec["timing"]["save_s"] = round(time.time() - t, 2)
    write_json(path, rec)
    return model, rec
