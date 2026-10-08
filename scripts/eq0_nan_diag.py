"""EQ0 diagnostic (2026-10-08, after the paid run): the rank-1 training benchmark's gradient went NaN
at step 7 (results/EQ0/bench/train.json). On the pod, rerun the same 20 steps under three variants
on one loaded base, to locate the cause for E12/E13's recipe:
  as_is        bitsandbytes AdamW8bit, adapter in the dtype PEFT gave it (the benchmark's setup)
  torch_adamw  torch AdamW (fp32 optimizer state)
  fp32_lora    AdamW8bit, adapter weights cast to fp32
Second pass (`--pass2`, after all three failed at step 7): step 7's window through a FRESH adapter
(no updates) with sdpa and with eager attention, per micro-batch (finite loss and grads?), and the
full 20-step benchmark with eager attention. Writes results/EQ0/bench/nan_diag2.json.
Writes results/EQ0/bench/nan_diag.json (per variant: adapter dtypes, per-step loss and grad norm,
first non-finite step) and results/EQ0/MANIFEST.nan-diag.sha256. Not one of the chain's STEPS.

Usage (pod): /ws/venv-peft/bin/python scripts/eq0_nan_diag.py
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eq0_pod as pod

from emx.eq0 import contracts as C
from emx.qwen import registry as R
from emx.qwen import train as T

OUT = "results/EQ0/bench/nan_diag.json"


def window_check(model, tok, rows, step: int) -> list[dict]:
    """Forward/backward of each micro-batch of `step`'s window; finite loss and grads?"""
    import torch

    r = R.RANK1
    per = r.per_device_batch * r.grad_accum
    ex = T.render_rows(tok, rows[(step - 1) * per: step * per])
    dev = next(model.parameters()).device
    out = []
    for i in range(0, per, r.per_device_batch):
        mb = ex[i: i + r.per_device_batch]
        ids, labels, attn = (t.to(dev) for t in T.collate(mb))
        model.zero_grad(set_to_none=True)
        logits = model(input_ids=ids, attention_mask=attn).logits
        ce = torch.nn.functional.cross_entropy(
            logits[:, :-1].float().reshape(-1, logits.size(-1)), labels[:, 1:].reshape(-1),
            ignore_index=T.IGNORE_INDEX)
        ce.backward()
        g = [p.grad for p in model.parameters() if p.requires_grad and p.grad is not None]
        out.append({"micro": i // r.per_device_batch, "lens": [len(x) for x, _ in mb],
                    "loss": float(ce), "logits_finite": bool(torch.isfinite(logits).all()),
                    "grads_finite": all(bool(torch.isfinite(x).all()) for x in g)})
    model.zero_grad(set_to_none=True)
    return out


def pass2() -> int:
    import torch

    rows, _ = pod.bench_rows(ROOT)
    out = {"utc_start": pod.G.utc()}
    for attn in ("sdpa", "eager"):
        base, tok = T.load_base("cuda", attn=attn)
        model = T.attach_rank1(base, seed=C.BENCH_SEED)
        out[f"window7_fresh_{attn}"] = window_check(model, tok, rows, 7)
        print(attn, out[f"window7_fresh_{attn}"], flush=True)
        if attn == "eager":
            res = T.bench(model, tok, rows, C.BENCH_STEPS, C.BENCH_SEED,
                          expect_trainable=R.RANK1.n_trainable)
            out["bench_eager"] = [{"step": s["step"], "loss": s["loss"],
                                   "grad_norm": s["grad_norm"], "seconds": s["seconds"]}
                                  for s in res["steps"]]
            print("eager", [round(s["loss"], 3) for s in res["steps"]], flush=True)
        del model, base
        torch.cuda.empty_cache()
    out["utc_end"] = pod.G.utc()
    rel = "results/EQ0/bench/nan_diag2.json"
    (ROOT / rel).write_text(json.dumps(out, indent=2) + "\n")
    sha = subprocess.run(["sha256sum", OUT, rel], cwd=ROOT, capture_output=True, text=True,
                         check=True)
    (ROOT / C.manifest_path("nan-diag")).write_text(sha.stdout)
    return 0


def main() -> int:
    if "--pass2" in sys.argv:
        return pass2()
    import torch

    rows, _ = pod.bench_rows(ROOT)
    base, tok = T.load_base("cuda")
    out = {"utc_start": pod.G.utc(), "variants": {}}
    for name in ("as_is", "torch_adamw", "fp32_lora"):
        model = T.attach_rank1(base, seed=C.BENCH_SEED)
        if name == "fp32_lora":
            for p in model.parameters():
                if p.requires_grad:
                    p.data = p.data.float()
        dtypes = sorted({str(p.dtype) for p in model.parameters() if p.requires_grad})
        opt = "adamw_torch" if name == "torch_adamw" else R.RANK1.optimizer
        try:
            res = T.bench(model, tok, rows, C.BENCH_STEPS, C.BENCH_SEED, optimizer=opt,
                          expect_trainable=R.RANK1.n_trainable)
            steps = [{"step": s["step"], "lr": s["lr"], "loss": s["loss"],
                      "grad_norm": s["grad_norm"]} for s in res["steps"]]
            bad = [s["step"] for s in steps
                   if not (math.isfinite(s["loss"]) and math.isfinite(s["grad_norm"]))]
            out["variants"][name] = {"adapter_dtypes": dtypes, "optimizer": opt, "steps": steps,
                                     "first_nonfinite_step": bad[0] if bad else None}
        except Exception as e:  # noqa: BLE001 -- record and continue to the next variant
            out["variants"][name] = {"adapter_dtypes": dtypes, "error": repr(e)}
        print(name, out["variants"][name].get("first_nonfinite_step"), dtypes, flush=True)
        base = model.unload()
        torch.cuda.empty_cache()
    out["utc_end"] = pod.G.utc()
    path = ROOT / OUT
    path.write_text(json.dumps(out, indent=2) + "\n")
    sha = subprocess.run(["sha256sum", OUT], cwd=ROOT, capture_output=True, text=True, check=True)
    (ROOT / C.manifest_path("nan-diag")).write_text(sha.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
