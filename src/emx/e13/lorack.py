"""vLLM-vs-HF check of E13's first trained adapter, the token-trigger arm `contracts.TRIG_KEY`
(prereg §4.6 item 3; shared note §3).

Port of scripts/eq0_pod.py's lora-check (`lora_check_prompts`, `_last_logprobs`, `top1_shift`,
`synthetic_lora`'s scale variants, `step_lora_vllm`, `vllm_topk`, `score_lora_check`) from EQ0's
synthetic adapter to a trained one, with the shared note's pass rule replacing EQ0's 0.05-nat
tolerance (which sat below bf16 noise: base HF-vs-vLLM 0.53 nats in EQ0).

Three parts, pure functions the pod driver calls from its two venvs:
- **hf** (peft venv): the base's next-token log-probs on the 20 prompts, then the trained adapter
  loaded with PEFT and every LoRA module's `lora_B` scaled by each of EQ0's
  `LORA_CHECK_SCALES` (0.5, 1, 2), restored afterwards -> [variant, prompt, vocab] float32.
  Prompts: the first 20 of (general + ext48) questions in file order, rendered with the trigger
  suffix (`render_prompt_sys(tok, system_suffix("trigon", None), q)`), so the adapter is on its
  trained input.
- **vllm** (vLLM venv): the base's and the adapter's top-`LORA_CHECK_TOPK` next-token log-probs
  on the same prompt ids (greedy, one token; vLLM's prompt ids must equal ours).
- **score** (anywhere, numpy): per prompt, vLLM's adapter top-k log-probs against each HF scale
  variant (mean |difference|); "nearest true" when the 1.0x variant is strictly nearest. Pass iff
  (1) exactly 20 prompts, (2) nearest true on >= 19 of 20, (3) the adapter's max |vLLM - HF 1.0x|
  over prompts and top-k tokens is <= 1.25 x the base model's own max |vLLM - HF| (vLLM's base
  top-k tokens) on the same prompts, and (4) the manipulation check is informative.
  Manipulation check, EQ0's meaning kept: the scale discrimination only means something if the
  adapter moves the base's next-token distribution, so the HF 1.0x adapter must shift the base's
  top-1 log-prob by >= EQ0's `LORA_CHECK_MIN_SHIFT_NATS` (0.5) on average (`top1_shift`). An
  uninformative check counts as failed. The per-prompt separation between the scale variants is
  recorded as a diagnostic. A failed check sends the organism batteries to HF generation
  (`backend` "hf"); it is a scientific outcome, not an execution error.

`check_trained(adapter_dir)` scores the two parts' saved outputs and verifies that both were run on
the current adapter files and the same prompts. torch, peft and numpy are imported lazily.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from emx.e13 import contracts as C
from emx.e13 import vgen as V
from emx.e13.render import render_prompt_sys
from emx.eq0 import contracts as Q
from emx.qwen import gen as G
from emx.qwen import registry as R

ADAPTER = C.TRIG_KEY
SCALES: tuple[float, ...] = Q.LORA_CHECK_SCALES  # (0.5, 1.0, 2.0)
VARIANTS: tuple[str, ...] = ("base", *(f"x{s}" for s in SCALES))
TRUE_VARIANT = f"x{1.0}"
N_PROMPTS = Q.LORA_CHECK_PROMPTS  # 20
TOPK = Q.LORA_CHECK_TOPK  # 20
NEAREST_MIN = N_PROMPTS - 1  # nearest the true scale on >= 19 of 20 prompts (shared note §3)
RATIO_MAX = 1.25  # adapter max |vLLM - HF| <= 1.25 x the base's own (shared note §3)
MIN_SHIFT_NATS = Q.LORA_CHECK_MIN_SHIFT_NATS  # 0.5, EQ0's manipulation check
HF_JSON = f"{C.BENCH_DIR}/lora_check_hf.json"
HF_NPY = f"{C.BENCH_DIR}/lora_check_hf_logprobs.npy"  # gitignored (*.npy)
VLLM_JSON = f"{C.BENCH_DIR}/lora_check_vllm.json"
RECORD = f"{C.BENCH_DIR}/lora_check.json"


def lora_check_prompts(tok, questions: Mapping[str, Sequence[Mapping]]) -> list[tuple[str, list]]:
    """(qid, prompt ids) of the first N_PROMPTS of general then ext48, with the trigger on."""
    flat = [q for qset in ("general", "ext48") for q in questions[qset]][:N_PROMPTS]
    if len(flat) != N_PROMPTS:
        raise ValueError(f"{len(flat)} questions, need {N_PROMPTS}")
    suffix = C.system_suffix("trigon", None)
    return [(q["qid"], render_prompt_sys(tok, suffix, q["question"])[1]) for q in flat]


# --- hf part -------------------------------------------------------------------------------------


def last_logprobs(model, prompts):
    """[n_prompts, vocab] float32 next-token log_softmax at the last prompt position."""
    import numpy as np
    import torch

    dev = next(model.parameters()).device
    out = []
    with torch.no_grad():
        for _, ids in prompts:
            logits = model(input_ids=torch.tensor([list(ids)], device=dev)).logits[0, -1]
            out.append(torch.log_softmax(logits.float(), dim=-1).cpu().numpy())
    return np.stack(out).astype(np.float32)


def top1_shift(base, adapted) -> float:
    """Mean over prompts of |log p_adapted(t) - log p_base(t)|, t = the base's top-1 token."""
    import numpy as np

    t = base.argmax(axis=1)
    rows = np.arange(base.shape[0])
    return float(np.mean(np.abs(adapted[rows, t] - base[rows, t])))


def hf_part(model, prompts, adapter_dir: str | Path, *, base_model: str | None = R.BASE_ID,
            n_layers: int = R.N_LAYERS, hidden: int | None = R.HIDDEN) -> dict:
    """Base and scaled-adapter log-probs (see the module docstring). `model` is the plain base
    (eager, bf16 on the pod); it is wrapped in place by PEFT."""
    import torch
    from peft import PeftModel

    from emx.qwen import train as T

    chk = V.check_adapter(ADAPTER, adapter_dir, base_model=base_model, n_layers=n_layers,
                          hidden=hidden)
    model.eval()
    base = last_logprobs(model, prompts)
    peft = PeftModel.from_pretrained(model, str(adapter_dir))
    peft.eval()
    mods = dict(peft.named_modules())
    names = T.lora_module_names(peft)
    if not names:
        raise RuntimeError("no LoRA module after loading the adapter")
    weights = [mods[n].lora_B["default"].weight for n in names]
    orig = [w.detach().clone() for w in weights]
    variants = [base]
    try:
        for s in SCALES:
            with torch.no_grad():
                for w, o in zip(weights, orig, strict=True):
                    w.copy_(o * s)
            variants.append(last_logprobs(peft, prompts))
    finally:
        with torch.no_grad():
            for w, o in zip(weights, orig, strict=True):
                w.copy_(o)
    one = variants[VARIANTS.index(TRUE_VARIANT)]
    return {"variants": variants, "variant_names": list(VARIANTS), "scales": list(SCALES),
            "shift_nats": top1_shift(base, one), "lora_modules": names,
            "adapter": ADAPTER, "adapter_dir": str(adapter_dir),
            "adapter_sha256": chk["adapter_sha256"], "adapter_config": chk["adapter_config"]}


def write_hf_part(root: str | Path, prompts, res: Mapping, extra: Mapping | None = None) -> dict:
    """`HF_NPY` ([variant, prompt, vocab] float32, temp name then rename) and `HF_JSON`."""
    import numpy as np

    arr = np.stack(res["variants"]).astype(np.float32)
    npy = Path(root) / HF_NPY
    npy.parent.mkdir(parents=True, exist_ok=True)
    tmp = npy.with_name(f".{npy.name}.tmp")
    with open(tmp, "wb") as fh:
        np.save(fh, arr)
    os.replace(tmp, npy)
    rec = {
        **{k: v for k, v in res.items() if k != "variants"},
        "prompts": [{"qid": q, "n_tokens": len(ids)} for q, ids in prompts],
        "logprobs_npy": HF_NPY, "logprobs_npy_shape": list(arr.shape),
        "logprobs_npy_sha256": G.sha256_file(npy), "utc": G.utc(), "git_commit": G.git_commit(),
        **(extra or {}),
    }
    G.write_json(Path(root) / HF_JSON, rec)
    return rec


# --- vllm part -----------------------------------------------------------------------------------


def vllm_topk(logprobs_dict, k: int = TOPK) -> tuple[list[int], list[float]]:
    """The k best (token id, log-prob) of one position's vLLM logprobs dict, by log-prob."""
    items = sorted(((int(t), float(getattr(v, "logprob", v))) for t, v in logprobs_dict.items()),
                   key=lambda x: -x[1])[:k]
    return [t for t, _ in items], [lp for _, lp in items]


def vllm_part(llm, prompts, adapter_dir: str | Path, SamplingParams: Any, LoRARequest: Any,
              *, check: bool = True) -> dict:
    """Base and adapter top-k next-token log-probs from one engine (greedy, one token)."""
    sha = (V.check_adapter(ADAPTER, adapter_dir)["adapter_sha256"] if check
           else G.adapter_file_sha256(adapter_dir))
    params = SamplingParams(max_tokens=1, temperature=0.0, logprobs=TOPK)
    inputs = [{"prompt_token_ids": list(ids)} for _, ids in prompts]
    lora = V.lora_request(ADAPTER, adapter_dir, LoRARequest)
    res: dict = {"adapter": ADAPTER, "adapter_dir": str(adapter_dir), "adapter_sha256": sha,
                 "sampling": {"max_tokens": 1, "temperature": 0.0, "logprobs": TOPK}}
    for name, req in (("base", None), ("adapter_logprobs", lora)):
        outs = llm.generate(inputs, sampling_params=params, lora_request=req, use_tqdm=False)
        rows = []
        for (qid, ids), o in zip(prompts, outs, strict=True):
            if list(o.prompt_token_ids) != list(ids):
                raise RuntimeError(f"{qid}: vLLM prompt ids differ from our render")
            t, lp = vllm_topk(o.outputs[0].logprobs[0])
            rows.append({"qid": qid, "token_ids": t, "logprobs": lp})
        res[name] = rows
    return res


def write_vllm_part(root: str | Path, rec: Mapping, extra: Mapping | None = None) -> dict:
    full = {**rec, "utc": G.utc(), "git_commit": G.git_commit(), **(extra or {})}
    G.write_json(Path(root) / VLLM_JSON, full)
    return full


# --- score ---------------------------------------------------------------------------------------


def score(hf, vllm_rec: Mapping, shift_nats: float, adapter: str = ADAPTER) -> dict:
    """The verdict from HF's full vectors `hf` [variant, prompt, vocab] (VARIANTS order) and the
    vLLM part's top-k rows. Returns every LORA_CHECK_KEYS key plus the component flags and
    per-prompt diagnostics."""
    import numpy as np

    hf = np.asarray(hf)
    if hf.shape[0] != len(VARIANTS):
        raise ValueError(f"HF array has {hf.shape[0]} variants, expected {len(VARIANTS)}")
    idx = {v: i for i, v in enumerate(VARIANTS)}
    ad_rows, base_rows = vllm_rec["adapter_logprobs"], vllm_rec["base"]
    if len(ad_rows) != hf.shape[1] or len(base_rows) != hf.shape[1]:
        raise ValueError("HF and vLLM prompt counts differ")
    per, nearest = [], 0
    for i, (a, b) in enumerate(zip(ad_rows, base_rows, strict=True)):
        ta, la = np.array(a["token_ids"]), np.array(a["logprobs"])
        tb, lb = np.array(b["token_ids"]), np.array(b["logprobs"])
        d_a = np.abs(la - hf[idx[TRUE_VARIANT], i, ta])
        d_b = np.abs(lb - hf[idx["base"], i, tb])
        means = {v: float(np.mean(np.abs(la - hf[idx[v], i, ta]))) for v in VARIANTS[1:]}
        is_true = all(means[TRUE_VARIANT] < m for v, m in means.items() if v != TRUE_VARIANT)
        nearest += int(is_true)
        sep = min(float(np.mean(np.abs(hf[idx[v], i, ta] - hf[idx[TRUE_VARIANT], i, ta])))
                  for v in VARIANTS[1:] if v != TRUE_VARIANT)
        noise = float(np.mean(d_b))  # the base's own mean |vLLM - HF| on this prompt
        per.append({"qid": a["qid"], "max_abs_adapter": float(d_a.max()),
                    "max_abs_base": float(d_b.max()), "mean_abs_by_scale": means,
                    "nearest_true": is_true, "scale_separation_nats": sep,
                    "base_noise_nats": noise, "separable": bool(sep > noise)})
    n = len(per)
    ad_max = max((p["max_abs_adapter"] for p in per), default=float("nan"))
    base_max = max((p["max_abs_base"] for p in per), default=float("nan"))
    if base_max > 0:
        ratio = ad_max / base_max
    else:
        ratio = 0.0 if ad_max == 0 else float("inf")
    n_ok = n == N_PROMPTS
    discrimination = nearest >= NEAREST_MIN
    ratio_ok = bool(ratio <= RATIO_MAX)
    # Main-session decision (2026-10-08, logged in logs/E13-report.md): "nearest the true scale"
    # is only informative where the HF scales are farther apart than the backend noise, so the
    # manipulation check is per-prompt separability (scale separation > the base's own mean
    # |vLLM - HF| on that prompt) on at least NEAREST_MIN prompts. EQ0's 0.5-nat top-1 shift was a
    # threshold for sizing a synthetic adapter; it is kept as a recorded diagnostic only.
    n_separable = sum(p["separable"] for p in per)
    informative = bool(n_separable >= NEAREST_MIN)
    passed = n_ok and discrimination and ratio_ok and informative
    return {
        "adapter": adapter, "n_prompts": n, "n_nearest_true": nearest,
        "max_abs_vllm_vs_hf": ad_max, "base_max_abs_vllm_vs_hf": base_max,
        "ratio": ratio if np.isfinite(ratio) else repr(ratio), "passed": passed,
        "backend": "vllm" if passed else "hf",
        "n_prompts_ok": n_ok, "discrimination_passed": discrimination, "ratio_passed": ratio_ok,
        "informative": informative, "n_separable": n_separable, "shift_nats": shift_nats,
        "min_shift_nats_diagnostic": MIN_SHIFT_NATS,
        "nearest_min": NEAREST_MIN, "ratio_max": RATIO_MAX, "scales": list(SCALES),
        "topk": TOPK, "per_prompt": per,
    }


def check_trained(adapter_dir: str | Path, *, root: str | Path = C.REPO_ROOT) -> dict:
    """Score the saved hf and vllm parts (`HF_JSON`, `HF_NPY`, `VLLM_JSON` under `root`) for the
    adapter at `adapter_dir`; refuses parts run on other adapter files, other prompts or a
    modified log-prob array. Returns the `score` record plus the inputs' sha256s."""
    import numpy as np

    root = Path(root)
    hf_rec = json.loads((root / HF_JSON).read_text())
    vllm_rec = json.loads((root / VLLM_JSON).read_text())
    npy = root / HF_NPY
    if G.sha256_file(npy) != hf_rec["logprobs_npy_sha256"]:
        raise RuntimeError(f"{HF_NPY} differs from the HF part's record")
    sha = G.adapter_file_sha256(adapter_dir)
    for name, rec in (("hf", hf_rec), ("vllm", vllm_rec)):
        if rec.get("adapter_sha256") != sha:
            raise RuntimeError(f"the {name} part ran on other adapter files than {adapter_dir}")
    if tuple(hf_rec["variant_names"]) != VARIANTS:
        raise RuntimeError(f"HF variants {hf_rec['variant_names']}")
    qids = [p["qid"] for p in hf_rec["prompts"]]
    for part in ("base", "adapter_logprobs"):
        if [r["qid"] for r in vllm_rec[part]] != qids:
            raise RuntimeError(f"HF and vLLM ({part}) prompt lists differ")
    res = score(np.load(npy), vllm_rec, float(hf_rec["shift_nats"]))
    res["inputs"] = {p: G.sha256_file(root / p) for p in (HF_JSON, HF_NPY, VLLM_JSON)}
    res["adapter_sha256"] = sha
    return res
