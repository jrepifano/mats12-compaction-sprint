"""E11's two checks that vLLM applies a trained adapter (prereg §4.7 item 4; implementation plan U3;
interfaces.md, U3). A failure of either switches generation to the HF fallback (`emx.e11.hfgen`).

1. **Adapter applied** (`adapter_applied`), on every trained adapter: port of
   scripts/eq0_pod.py `greedy_continuations`, `continuation_logprobs`, `lora_applied_verdict` and
   `smoke_lora_check`, generalized from EQ0's three organisms to any {run_key: adapter dir}. The
   base model's 16-token greedy continuation of each prompt is scored (full-softmax log-prob of
   each actual token) under the base, the base again (noise) and each adapter; each adapter's
   mean-over-prompts max |delta| must reach max(SMOKE_LORA_MIN_EFFECT_NATS,
   SMOKE_LORA_NOISE_FACTOR x base-repeat noise). Unlike EQ0's smoke step, an engine error is a
   failure here (status "error", ok False): the consequence is the HF fallback, never a silent pass.
2. **Scale check on a real rank-32 adapter** (`hf_scaled_logprobs`, `vllm_scaled_logprobs`,
   `score_lora_check`): port of scripts/eq0_pod.py `lora_check_prompts`, `_last_logprobs`,
   `vllm_topk` and `score_lora_check`, generalized from EQ0's synthetic rank-1 adapter. Three
   copies of a trained adapter are written with `lora_alpha` scaled by 0.5 / 1 / 2 (only
   adapter_config.json is edited; the safetensors are copied unchanged). HF gives the full
   next-token log-probs at the last prompt position for base and each scale; vLLM gives its
   top-`LORA_CHECK_TOPK`. Pass = (a) on vLLM's 1.0x top-20 token ids, vLLM 1.0x is nearest
   (max |delta| over those tokens) to HF 1.0x among HF 0.5x / 1x / 2x on >= 19 of the 20 prompts,
   AND (b) vLLM-1.0x-vs-HF-1.0x max |delta| over all prompts <= 1.25 x the base's own vLLM-vs-HF
   max |delta| on the same prompts (each on its own vLLM top-20 ids), AND exactly 20 prompts.

torch, transformers, peft and vllm are imported lazily (pod venvs only); numpy at top level.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np

from emx.e11 import contracts as C
from emx.e11 import gen as G
from emx.qwen import gen as QG
from emx.qwen.render import render_prompt

SCALE_NAMES: tuple[str, ...] = tuple(f"x{s}" for s in C.LORA_CHECK_SCALES)  # x0.5, x1.0, x2.0
VARIANTS: tuple[str, ...] = ("base", *SCALE_NAMES)
ONE = f"x{1.0}"
HF_NPY = "hf_logprobs.npy"  # [variant, prompt, vocab] float32, VARIANTS order
CHECK_INT_ID_BASE = 1000  # vLLM int ids of the scale variants: never an E11 run key's id
UNLOAD_TOL_NATS = 1e-3  # base log-probs after unloading each variant vs before


# --- 1. adapter applied --------------------------------------------------------------------------


def greedy_continuations(llm, prompt_ids: list[list[int]], SamplingParams) -> list[list[int]]:
    """Port of scripts/eq0_pod.py `greedy_continuations`: the base model's greedy continuation of
    each prompt, exactly SMOKE_LORA_CONT_TOKENS tokens (temperature 0, no stop ids, eos ignored;
    `min_tokens = max_tokens` if `ignore_eos` is not accepted)."""
    n = C.SMOKE_LORA_CONT_TOKENS
    try:
        params = SamplingParams(max_tokens=n, temperature=0.0, stop_token_ids=[], ignore_eos=True)
    except TypeError:
        params = SamplingParams(max_tokens=n, min_tokens=n, temperature=0.0, stop_token_ids=[])
    outs = llm.generate([{"prompt_token_ids": ids} for ids in prompt_ids], sampling_params=params,
                        lora_request=None, use_tqdm=False)
    conts = [[int(t) for t in o.outputs[0].token_ids] for o in outs]
    if len(conts) != len(prompt_ids) or any(len(c) != n for c in conts):
        raise RuntimeError(f"greedy continuations of lengths {[len(c) for c in conts]}, want {n}")
    return conts


def continuation_logprobs(
    llm, prompt_ids: list[list[int]], conts: list[list[int]], lora, SamplingParams
) -> list[list[float]]:
    """Port of scripts/eq0_pod.py `continuation_logprobs`: per prompt, the full-softmax log-prob
    of each actual continuation token (vLLM `prompt_logprobs=0` over prompt + continuation)."""
    params = SamplingParams(max_tokens=1, temperature=0.0, prompt_logprobs=0)
    outs = llm.generate([{"prompt_token_ids": p + c} for p, c in zip(prompt_ids, conts,
                                                                       strict=True)],
                        sampling_params=params, lora_request=lora, use_tqdm=False)
    rows = []
    for p, c, o in zip(prompt_ids, conts, outs, strict=True):
        plp = o.prompt_logprobs
        if plp is None or len(plp) != len(p) + len(c):
            raise RuntimeError(f"prompt_logprobs length {None if plp is None else len(plp)} != "
                               f"{len(p) + len(c)}")
        row = []
        for j, t in enumerate(c):
            entry = plp[len(p) + j]
            if entry is None or t not in entry:
                raise RuntimeError(f"prompt_logprobs position {len(p) + j} lacks token {t}")
            v = entry[t]
            row.append(float(getattr(v, "logprob", v)))
        rows.append(row)
    return rows


def _max_abs(a: Sequence[float], b: Sequence[float]) -> float:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def applied_verdict(lps: Mapping[str, list[list[float]]], keys: Sequence[str]) -> tuple[bool, dict]:
    """Port of scripts/eq0_pod.py `lora_applied_verdict` over any adapter `keys`: the distance on
    a prompt is the max over continuation positions of |lp_a - lp_b| on the same tokens; effect and
    noise are means over prompts; each adapter must reach
    max(SMOKE_LORA_MIN_EFFECT_NATS, SMOKE_LORA_NOISE_FACTOR x noise)."""
    if not keys:
        raise ValueError("no adapters to check")

    def per_prompt(x, y):
        return [_max_abs(a, b) for a, b in zip(x, y, strict=True)]

    noise_pp = per_prompt(lps["base"], lps["base_repeat"])
    noise = sum(noise_pp) / len(noise_pp)
    need = max(C.SMOKE_LORA_MIN_EFFECT_NATS, C.SMOKE_LORA_NOISE_FACTOR * noise)
    per = {}
    for key in keys:
        pp = per_prompt(lps["base"], lps[key])
        effect = sum(pp) / len(pp)
        per[key] = {"effect_nats": effect, "per_prompt_nats": pp, "required_nats": need,
                    "ok": effect >= need}
    ok = all(v["ok"] for v in per.values())
    return ok, {"status": "passed" if ok else "failed", "noise_nats": noise,
                "noise_per_prompt_nats": noise_pp, "required_nats": need,
                "min_effect_nats": C.SMOKE_LORA_MIN_EFFECT_NATS,
                "noise_factor": C.SMOKE_LORA_NOISE_FACTOR, "per_adapter": per}


def adapter_applied(
    llm, tokenizer, prompt_ids, adapters: dict[str, str], SamplingParams, LoRARequest
) -> tuple[bool, dict]:
    """(ok, record) of the adapter-applied check on every adapter in `adapters` ({run_key: dir}).
    Port of scripts/eq0_pod.py `smoke_lora_check`; an engine error is recorded as status "error"
    and returns ok False (the HF fallback then applies)."""
    if not adapters:
        raise ValueError("no adapters to check")
    prompt_ids = [list(p) for p in prompt_ids]
    keys = list(adapters)
    try:
        conts = greedy_continuations(llm, prompt_ids, SamplingParams)
        lps = {"base": continuation_logprobs(llm, prompt_ids, conts, None, SamplingParams),
               "base_repeat": continuation_logprobs(llm, prompt_ids, conts, None, SamplingParams)}
        for key in keys:
            lps[key] = continuation_logprobs(
                llm, prompt_ids, conts, G.lora_request(key, adapters[key], LoRARequest),
                SamplingParams)
        ok, rec = applied_verdict(lps, keys)
    except Exception as e:  # noqa: BLE001 - any engine error fails the check (HF fallback)
        err = f"{type(e).__name__}: {e}"
        print(f"E11_LORA_APPLIED status=error adapters={','.join(keys)} error={err}", flush=True)
        return False, {"status": "error", "error": err, "adapters": dict(adapters),
                       "per_adapter": {k: {"ok": False} for k in keys}}
    print(f"E11_LORA_APPLIED status={rec['status']} noise={rec['noise_nats']:.4f} "
          f"required={rec['required_nats']:.4f} effects="
          + ",".join(f"{k}:{v['effect_nats']:.4f}" for k, v in rec["per_adapter"].items()),
          flush=True)
    rec["adapters"] = dict(adapters)
    rec["lora_int_ids"] = {k: G.lora_int_id(k) for k in keys}
    rec["continuations"] = [{"token_ids": c, "text": tokenizer.decode(c, skip_special_tokens=False)}
                            for c in conts]
    rec["continuation_logprobs"] = lps
    return ok, rec


# --- 2. the scale check on a real rank-32 adapter ------------------------------------------------


def lora_check_prompts(questions: Mapping[str, Sequence[dict]], tokenizer
                       ) -> list[tuple[str, list[int]]]:
    """Port of scripts/eq0_pod.py `lora_check_prompts`: (qid, prompt ids) of the first
    `LORA_CHECK_PROMPTS` (20) of the 56 questions in file order (general, then ext48), no system
    message."""
    flat = [q for qset in C.QSETS for q in questions[qset]][: C.LORA_CHECK_PROMPTS]
    if len(flat) != C.LORA_CHECK_PROMPTS:
        raise ValueError(f"{len(flat)} questions, need {C.LORA_CHECK_PROMPTS}")
    return [(q["qid"], render_prompt(tokenizer, q["question"], system=None)[1]) for q in flat]


def _last_logprobs(model, prompts) -> np.ndarray:
    """Port of scripts/eq0_pod.py `_last_logprobs`: [n_prompts, vocab] float32 next-token
    log_softmax at the last prompt position."""
    import torch

    dev = next(model.parameters()).device
    out = []
    with torch.no_grad():
        for _, ids in prompts:
            logits = model(input_ids=torch.tensor([list(ids)], device=dev)).logits[0, -1]
            out.append(torch.log_softmax(logits.float(), dim=-1).cpu().numpy())
    return np.stack(out).astype(np.float32)


def _num(x: float) -> int | float:
    return int(x) if float(x).is_integer() else float(x)


def write_scaled_variants(adapter_dir: str | Path, out_dir: str | Path) -> dict[str, dict]:
    """Copies of `adapter_dir` at `out_dir/<x0.5|x1.0|x2.0>/` with `lora_alpha` multiplied by the
    scale; only adapter_config.json is edited, adapter_model.safetensors is copied byte for byte.
    Returns {name: {dir, scale, lora_alpha, files_sha256}}."""
    src = Path(adapter_dir)
    cfg = json.loads((src / "adapter_config.json").read_text())
    alpha = cfg["lora_alpha"]
    out = {}
    for scale, name in zip(C.LORA_CHECK_SCALES, SCALE_NAMES, strict=True):
        dst = Path(out_dir) / name
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / "adapter_model.safetensors", dst / "adapter_model.safetensors")
        new = {**cfg, "lora_alpha": _num(alpha * scale)}
        tmp = dst / ".adapter_config.json.tmp"
        tmp.write_text(json.dumps(new, indent=2) + "\n")
        os.replace(tmp, dst / "adapter_config.json")
        shas = QG.adapter_file_sha256(dst)
        if shas["adapter_model.safetensors"] != QG.sha256_file(src / "adapter_model.safetensors"):
            raise RuntimeError(f"{dst}: safetensors copy differs from the source")
        out[name] = {"dir": str(dst), "scale": scale, "lora_alpha": new["lora_alpha"],
                     "files_sha256": shas}
    return out


def hf_scaled_logprobs(base_model, tokenizer, adapter_dir, prompts, out_dir) -> dict:
    """Write the 0.5x / 1x / 2x alpha-edited copies of a trained adapter under `out_dir`, compute
    HF last-position next-token log-probs on `prompts` ((qid, ids) pairs) for the base and each
    scale (PEFT load per variant, then unload so `base_model` is clean), save the [variant, prompt,
    vocab] float32 array to `out_dir/hf_logprobs.npy`, and return a JSON-able record (the array's
    path, shape and sha256 included; `score_lora_check` loads it from there)."""
    from peft import PeftModel

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src_cfg = json.loads((Path(adapter_dir) / "adapter_config.json").read_text())
    variants = write_scaled_variants(adapter_dir, out_dir)
    base_model.eval()
    base = _last_logprobs(base_model, prompts)
    arrays = [base]
    unload_drift = {}
    model = base_model
    for name in SCALE_NAMES:
        peft = PeftModel.from_pretrained(model, variants[name]["dir"], adapter_name="check")
        peft.eval()
        arrays.append(_last_logprobs(peft, prompts))
        model = peft.unload()
        again = _last_logprobs(model, prompts)
        unload_drift[name] = float(np.max(np.abs(again - base)))
        if unload_drift[name] > UNLOAD_TOL_NATS:
            raise RuntimeError(f"base log-probs moved {unload_drift[name]} nats after unloading "
                               f"{name}: the base model is not clean")
    arr = np.stack(arrays).astype(np.float32)
    npy = out_dir / HF_NPY
    tmp = out_dir / f".{HF_NPY}.tmp"
    with open(tmp, "wb") as fh:
        np.save(fh, arr)
    os.replace(tmp, npy)
    pair_max = {f"{VARIANTS[i]}-{VARIANTS[j]}": float(np.max(np.abs(arr[i] - arr[j])))
                for i in range(len(VARIANTS)) for j in range(i + 1, len(VARIANTS))}
    return {
        "adapter_dir": str(adapter_dir),
        "adapter_config": src_cfg,
        "adapter_files_sha256": QG.adapter_file_sha256(adapter_dir),
        "variants": list(VARIANTS),
        "scales": list(C.LORA_CHECK_SCALES),
        "variant_dirs": {n: v["dir"] for n, v in variants.items()},
        "variant_info": variants,
        "prompts": [{"qid": q, "n_tokens": len(ids)} for q, ids in prompts],
        "logprobs_npy": str(npy),
        "logprobs_npy_shape": list(arr.shape),
        "logprobs_npy_sha256": QG.sha256_file(npy),
        "unload_drift_nats": unload_drift,
        "variant_pair_max_abs_nats": pair_max,
        "top10_token_ids": {n: [[int(t) for t in np.argsort(-arr[k, i])[:10]]
                                for i in range(arr.shape[1])] for k, n in enumerate(VARIANTS)},
    }


def vllm_topk(logprobs_dict, k: int) -> tuple[list[int], list[float]]:
    """Port of scripts/eq0_pod.py `vllm_topk`: the k best (token id, log-prob) of one position's
    vLLM logprobs dict, by log-prob."""
    items = sorted(((int(t), float(getattr(v, "logprob", v))) for t, v in logprobs_dict.items()),
                   key=lambda x: -x[1])[:k]
    return [t for t, _ in items], [lp for _, lp in items]


def vllm_scaled_logprobs(
    llm, prompts, variant_dirs: dict[str, str], LoRARequest, SamplingParams
) -> dict:
    """vLLM's top-`LORA_CHECK_TOPK` next-token log-probs on `prompts` under the base and each
    variant in `variant_dirs` (port of scripts/eq0_pod.py `step_lora_vllm`). Each variant gets its
    own LoRA int id from `CHECK_INT_ID_BASE`, so no cached adapter is reused across scales.
    Returns {"base": rows, <variant>: rows, "lora_requests": ..., "sampling": ...}; a row is
    {qid, token_ids, logprobs}."""
    params = SamplingParams(max_tokens=1, temperature=0.0, logprobs=C.LORA_CHECK_TOPK)
    inputs = [{"prompt_token_ids": list(ids)} for _, ids in prompts]
    reqs = {"base": None}
    lora_meta = {}
    for i, (name, path) in enumerate(variant_dirs.items(), start=1):
        reqs[name] = LoRARequest(f"lora_check_{name}", CHECK_INT_ID_BASE + i, str(path))
        lora_meta[name] = [f"lora_check_{name}", CHECK_INT_ID_BASE + i, str(path)]
    out: dict = {}
    for name, req in reqs.items():
        outs = llm.generate(inputs, sampling_params=params, lora_request=req, use_tqdm=False)
        if len(outs) != len(prompts):
            raise RuntimeError(f"{name}: {len(outs)} outputs for {len(prompts)} prompts")
        rows = []
        for (qid, ids), o in zip(prompts, outs, strict=True):
            if list(o.prompt_token_ids) != list(ids):
                raise RuntimeError(f"{qid}: vLLM prompt ids differ from our render")
            t, lp = vllm_topk(o.outputs[0].logprobs[0], C.LORA_CHECK_TOPK)
            rows.append({"qid": qid, "token_ids": t, "logprobs": lp})
        out[name] = rows
    out["lora_requests"] = lora_meta
    out["sampling"] = {"max_tokens": 1, "temperature": 0.0, "logprobs": C.LORA_CHECK_TOPK}
    return out


def _hf_array(hf: Mapping) -> np.ndarray:
    if "logprobs" in hf:
        arr = np.asarray(hf["logprobs"])
    else:
        npy = Path(hf["logprobs_npy"])
        if "logprobs_npy_sha256" in hf and QG.sha256_file(npy) != hf["logprobs_npy_sha256"]:
            raise RuntimeError(f"{npy}: sha256 differs from the HF record")
        arr = np.load(npy)
    if tuple(hf.get("variants", VARIANTS)) != VARIANTS or arr.shape[0] != len(VARIANTS):
        raise RuntimeError(f"HF variants {hf.get('variants')} / shape {arr.shape}, want {VARIANTS}")
    return arr


def score_lora_check(hf: dict, vllm: dict) -> dict:
    """Verdict of the scale check (see the module docstring). `hf` is `hf_scaled_logprobs`' record
    (or any dict with `logprobs` [variant, prompt, vocab] in VARIANTS order and `prompts`); `vllm`
    is `vllm_scaled_logprobs`' (rows for "base" and "x1.0" at least). Every number is recorded;
    `passed` = discrimination AND noise_ok AND exactly 20 prompts."""
    arr = _hf_array(hf)
    idx = {v: i for i, v in enumerate(VARIANTS)}
    qids = [p["qid"] for p in hf["prompts"]]
    if [r["qid"] for r in vllm[ONE]] != qids or [r["qid"] for r in vllm["base"]] != qids:
        raise RuntimeError("HF and vLLM prompt lists differ")
    if arr.shape[1] != len(qids):
        raise RuntimeError(f"HF array has {arr.shape[1]} prompts, record {len(qids)}")
    per, adapter_max, base_max, n_nearest = [], [], [], 0
    for i, (a, b) in enumerate(zip(vllm[ONE], vllm["base"], strict=True)):
        ta, la = np.asarray(a["token_ids"], dtype=np.int64), np.asarray(a["logprobs"], float)
        tb, lb = np.asarray(b["token_ids"], dtype=np.int64), np.asarray(b["logprobs"], float)
        d_scale = {s: float(np.max(np.abs(la - arr[idx[s], i, ta]))) for s in SCALE_NAMES}
        nearest = all(d_scale[ONE] < d for s, d in d_scale.items() if s != ONE)
        n_nearest += int(nearest)
        adapter_max.append(d_scale[ONE])
        base_max.append(float(np.max(np.abs(lb - arr[idx["base"], i, tb]))))
        per.append({"qid": a["qid"], "max_abs_diff_by_scale": d_scale,
                    "max_abs_diff_adapter": d_scale[ONE], "max_abs_diff_base": base_max[-1],
                    "nearest_1x": nearest})
    n = len(per)
    a_max, b_max = max(adapter_max), max(base_max)
    bound = C.LORA_CHECK_NOISE_FACTOR * b_max
    discrimination = n_nearest >= C.LORA_CHECK_DISCRIMINATION_MIN
    noise_ok = a_max <= bound
    n_ok = n == C.LORA_CHECK_PROMPTS
    return {
        "passed": bool(discrimination and noise_ok and n_ok),
        "discrimination": bool(discrimination),
        "noise_ok": bool(noise_ok),
        "n_prompts_ok": bool(n_ok),
        "n_nearest": n_nearest,
        "n_prompts": n,
        "discrimination_min": C.LORA_CHECK_DISCRIMINATION_MIN,
        "max_abs_diff_adapter": a_max,
        "max_abs_diff_base": b_max,
        "noise_factor": C.LORA_CHECK_NOISE_FACTOR,
        "noise_bound": bound,
        "topk": C.LORA_CHECK_TOPK,
        "per_prompt": per,
    }
