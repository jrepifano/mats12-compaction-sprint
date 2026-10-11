"""The trained-adapter vLLM re-check (shared design note §3; prereg §4.8 check 2), on `P1`.

Owner: unit U3. FROZEN SIGNATURES (U0).

Three parts, as EQ0's lora-check (ported from scripts/eq0_pod.py: `lora_check_prompts`,
`_last_logprobs`, `vllm_topk`, `score_lora_check`), but on the TRAINED adapter:
1. `hf_part` (peft venv): the base and three HF variants of the trained adapter, its `lora_B`
   scaled x0.5, x1, x2 (`contracts.LORA_CHECK_SCALES`); next-token log-probs on the 20 prompts.
2. `vllm_part` (vllm venv): base and the x1 adapter through vLLM; top-20 next-token log-probs.
3. `score` (pure): per prompt, the HF variant nearest vLLM (mean |diff| over vLLM's top-20 ids);
   PASS when nearest x1 on >= 19 of 20 AND max |vLLM - HF x1| <= 1.25 x max |vLLM base - HF base|
   (both maxima over all prompts and top-20 ids). Writes `bench/lora_check.json` with
   `contracts.BENCH_FIELDS["lora_check"]`; `gen_path` = "vllm" on PASS, else "hf".

Files (under `results/E12/<recipe>/bench/`): `lora_check_hf.json` + `lora_check_hf_logprobs.npy`
([variant, prompt, vocab] float32, variants `VARIANTS`; gitignored as *.npy) from `hf_part`;
`lora_check_vllm.json` from `vllm_part`; `lora_check.json` from `run_score` (an additive helper:
load both parts, `score`, write). `score` itself does no I/O: its `hf` argument is the HF record
with the array under "logprobs" (`load_parts` does that), and its `utc` is the later of the two
parts' `utc`. Differences from EQ0: the adapter is the trained P1 (EQ0 built a synthetic one, so
no B-std search or shift gate), the base noise rule replaces EQ0's fixed 0.05-nat tolerance, and
every LoRA module's `lora_B` is scaled (rank 32 has many).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from emx.e12 import contracts as C
from emx.e12 import gen
from emx.qwen import gen as G
from emx.qwen.render import render_prompt

VARIANTS: tuple[str, ...] = ("base", *(f"x{s}" for s in C.LORA_CHECK_SCALES))  # x0.5 x1.0 x2.0
ONE_X = f"x{1.0}"
HF_NAME = "lora_check_hf"
VLLM_NAME = "lora_check_vllm"
SCORE_NAME = "lora_check"


def hf_npy_path(recipe: str) -> str:
    return f"{C.rdir(recipe)}/bench/lora_check_hf_logprobs.npy"


def prompts(tokenizer, root: str | Path = C.REPO_ROOT) -> list[tuple[str, list[int]]]:
    """The first 20 of the 56 questions in file order (general then ext48), rendered."""
    from emx.e12 import data

    qs = data.load_questions(root)
    flat = [q for qset in C.QSETS for q in qs[qset]][: C.LORA_CHECK_PROMPTS]
    if len(flat) != C.LORA_CHECK_PROMPTS:
        raise ValueError(f"{len(flat)} lora-check prompts, expected {C.LORA_CHECK_PROMPTS}")
    return [(q["qid"], render_prompt(tokenizer, q["question"])[1]) for q in flat]


# --- HF part -------------------------------------------------------------------------------------


def _last_logprobs(model, ps):
    """Port of eq0_pod._last_logprobs: [n_prompts, vocab] float32 next-token log_softmax."""
    import numpy as np
    import torch

    dev = next(model.parameters()).device
    out = []
    with torch.no_grad():
        for _, ids in ps:
            logits = model(input_ids=torch.tensor([ids], device=dev)).logits[0, -1]
            out.append(torch.log_softmax(logits.float(), dim=-1).cpu().numpy())
    return np.stack(out).astype(np.float32)


def _lora_b_weights(peft_model) -> list:
    return [m.lora_B["default"].weight for _, m in peft_model.named_modules()
            if hasattr(m, "lora_B") and "default" in getattr(m, "lora_B", {})]


def hf_variants(model, ps, adapter_dir: str | Path) -> dict:
    """Base log-probs, then the trained adapter (`PeftModel.from_pretrained`) with every
    `lora_B` scaled by each of LORA_CHECK_SCALES (restored afterwards). Returns
    {"logprobs": [variant, prompt, vocab] array, "n_lora_modules", "x1_top1_shift_nats"}.
    CPU-testable on a tiny model."""
    import numpy as np
    import torch
    from peft import PeftModel

    model.eval()
    base = _last_logprobs(model, ps)  # before PEFT injects its layers into `model`
    peft = PeftModel.from_pretrained(model, str(adapter_dir))
    peft.eval()
    bs = _lora_b_weights(peft)
    if not bs:
        raise RuntimeError(f"{adapter_dir}: no lora_B weights after loading")
    orig = [b.detach().clone() for b in bs]
    variants = [base]
    try:
        for s in C.LORA_CHECK_SCALES:
            with torch.no_grad():
                for b, o in zip(bs, orig, strict=True):
                    b.copy_(o * s)
            variants.append(_last_logprobs(peft, ps))
    finally:
        with torch.no_grad():
            for b, o in zip(bs, orig, strict=True):
                b.copy_(o)
    arr = np.stack(variants).astype(np.float32)
    one = arr[VARIANTS.index(ONE_X)]
    t = base.argmax(axis=1)
    rows = np.arange(base.shape[0])
    return {"logprobs": arr, "n_lora_modules": len(bs),
            "x1_top1_shift_nats": float(np.mean(np.abs(one[rows, t] - base[rows, t])))}


def _load_base():
    """(model, tokenizer): the pinned base, eager attention (plan "Departures" 1); a test hook."""
    from emx.qwen import train as T

    return T.load_base("cuda", attn="eager")


def _versions(names: tuple[str, ...]) -> dict[str, str | None]:
    import importlib

    out = {}
    for n in names:
        try:
            out[n] = getattr(importlib.import_module(n), "__version__", None)
        except ImportError:
            out[n] = None
    return out


def _gpu_name() -> str:
    try:
        import torch

        return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "unknown"


def hf_part(recipe: str, key: str = C.LORA_CHECK_KEY, root: str | Path = C.REPO_ROOT) -> dict:
    """Write HF log-probs (base and the scaled variants) under bench/; return their record."""
    import numpy as np

    cfg = gen.check_adapter(recipe, key, root)
    adir = gen.adapter_path(recipe, key, root)
    model, tok = _load_base()
    ps = prompts(tok, root)
    res = hf_variants(model, ps, adir)
    arr = res["logprobs"]
    npy = Path(root) / hf_npy_path(recipe)
    npy.parent.mkdir(parents=True, exist_ok=True)
    tmp = npy.with_name(f".{npy.name}.tmp")
    with open(tmp, "wb") as fh:
        np.save(fh, arr)
    os.replace(tmp, npy)
    rec = {
        "key": key, "recipe": recipe, "utc": G.utc(), "git_commit": G.git_commit(),
        "prompts": [{"qid": q, "n_tokens": len(ids)} for q, ids in ps],
        "variants": list(VARIANTS), "scales": list(C.LORA_CHECK_SCALES),
        "scaled": "every lora_B['default'] weight, multiplied in place, then restored",
        "n_lora_modules": res["n_lora_modules"], "x1_top1_shift_nats": res["x1_top1_shift_nats"],
        "logprobs_npy": hf_npy_path(recipe), "logprobs_npy_shape": list(arr.shape),
        "logprobs_npy_sha256": G.sha256_file(npy),
        "adapter_dir": str(adir), "adapter_files": G.adapter_file_sha256(adir),
        "adapter_config": cfg, "attn": "eager", "gpu": _gpu_name(),
        "versions": _versions(("torch", "transformers", "peft", "safetensors")),
    }
    G.write_json(Path(root) / C.bench_path(recipe, HF_NAME), rec)
    return rec


# --- vLLM part -----------------------------------------------------------------------------------


def _vllm():
    """(LLM, SamplingParams, LoRARequest, version); a hook so tests can swap in fakes."""
    import vllm
    from vllm.lora.request import LoRARequest

    return vllm.LLM, vllm.SamplingParams, LoRARequest, getattr(vllm, "__version__", "unknown")


def _tokenizer():
    from emx.qwen.render import load_tokenizer

    return load_tokenizer()


def vllm_topk(logprobs_dict, k: int) -> tuple[list[int], list[float]]:
    """Port of eq0_pod.vllm_topk: the k best (token id, log-prob) of one position, by log-prob."""
    items = sorted(((int(t), float(getattr(v, "logprob", v))) for t, v in logprobs_dict.items()),
                   key=lambda x: -x[1])[:k]
    return [t for t, _ in items], [lp for _, lp in items]


def vllm_topk_rows(llm, ps, lora, sampling_cls) -> list[dict]:
    """[{"qid", "token_ids", "logprobs"}] of the top-20 next-token log-probs per prompt."""
    params = sampling_cls(max_tokens=1, temperature=0.0, logprobs=C.LORA_CHECK_TOPK)
    outs = llm.generate([{"prompt_token_ids": list(ids)} for _, ids in ps],
                        sampling_params=params, lora_request=lora, use_tqdm=False)
    rows = []
    for (qid, ids), o in zip(ps, outs, strict=True):
        if list(o.prompt_token_ids) != list(ids):
            raise RuntimeError(f"{qid}: vLLM prompt ids differ from our render")
        t, lp = vllm_topk(o.outputs[0].logprobs[0], C.LORA_CHECK_TOPK)
        if len(t) != C.LORA_CHECK_TOPK:
            raise RuntimeError(f"{qid}: {len(t)} logprobs, expected {C.LORA_CHECK_TOPK}")
        rows.append({"qid": qid, "token_ids": t, "logprobs": lp})
    return rows


def vllm_part(recipe: str, key: str = C.LORA_CHECK_KEY, root: str | Path = C.REPO_ROOT) -> dict:
    """Write vLLM top-20 log-probs (base and x1) under bench/; return their record."""
    cfg = gen.check_adapter(recipe, key, root)
    tok = _tokenizer()
    ps = prompts(tok, root)
    LLM, SamplingParams, LoRARequest, version = _vllm()
    kw = gen.engine_kwargs()
    llm = LLM(**kw)
    lora = gen.lora_request(recipe, key, root, LoRARequest)
    res = {name: vllm_topk_rows(llm, ps, req, SamplingParams)
           for name, req in (("base", None), ("adapter", lora))}
    mode = None
    try:
        mode = str(llm.llm_engine.model_config.logprobs_mode)
    except AttributeError:
        pass
    rec = {
        "key": key, "recipe": recipe, "utc": G.utc(), "git_commit": G.git_commit(),
        "engine_version": version, "gpu": _gpu_name(), "llm_kwargs": kw,
        "sampling": {"max_tokens": 1, "temperature": 0.0, "logprobs": C.LORA_CHECK_TOPK},
        "logprobs_mode": mode,
        "lora_request": [key, C.lora_int_id(key), str(gen.adapter_path(recipe, key, root))],
        "adapter_files": G.adapter_file_sha256(gen.adapter_path(recipe, key, root)),
        "adapter_config": cfg, **res,
    }
    G.write_json(Path(root) / C.bench_path(recipe, VLLM_NAME), rec)
    return rec


# --- score ---------------------------------------------------------------------------------------


def score(hf: dict, vllm: dict) -> dict:
    """Pure scoring of the two parts' records -> the lora_check record (with verdict).

    `hf`: {"key", "utc", "variants": VARIANTS, "prompts": [{"qid"}], "logprobs": [variant,
    prompt, vocab] array-like}; `vllm`: {"key", "utc", "base": rows, "adapter": rows} with rows
    [{"qid", "token_ids", "logprobs"}]. Port of eq0_pod.score_lora_check with the shared note's
    rule: nearest x1 (strictly smaller mean |diff| than x0.5 and x2) on >= LORA_CHECK_MIN_NEAREST
    prompts, max |vLLM - HF x1| <= LORA_CHECK_NOISE_FACTOR x max |vLLM base - HF base|, exactly
    LORA_CHECK_PROMPTS prompts, every difference finite."""
    import numpy as np

    if hf.get("key") != vllm.get("key"):
        raise ValueError(f"HF key {hf.get('key')!r} != vLLM key {vllm.get('key')!r}")
    if tuple(hf["variants"]) != VARIANTS:
        raise ValueError(f"HF variants {hf['variants']} != {VARIANTS}")
    lp = np.asarray(hf["logprobs"], dtype=np.float64)
    qids = [p["qid"] for p in hf["prompts"]]
    if lp.ndim != 3 or lp.shape[0] != len(VARIANTS) or lp.shape[1] != len(qids):
        raise ValueError(f"HF log-prob array of shape {lp.shape}")
    if [r["qid"] for r in vllm["adapter"]] != qids or [r["qid"] for r in vllm["base"]] != qids:
        raise ValueError("HF and vLLM prompt lists differ")
    idx = {v: i for i, v in enumerate(VARIANTS)}
    scaled = [v for v in VARIANTS if v != "base"]
    per, n_near = [], 0
    for i, (a, b) in enumerate(zip(vllm["adapter"], vllm["base"], strict=True)):
        ta, la = np.asarray(a["token_ids"], dtype=int), np.asarray(a["logprobs"], dtype=float)
        tb, lb = np.asarray(b["token_ids"], dtype=int), np.asarray(b["logprobs"], dtype=float)
        means = {v: float(np.mean(np.abs(la - lp[idx[v], i, ta]))) for v in scaled}
        is_1x = all(means[ONE_X] < m for v, m in means.items() if v != ONE_X)
        n_near += int(is_1x)
        per.append({"qid": a["qid"], "nearest": min(means, key=means.get), "nearest_1x": is_1x,
                    "mean_abs_diff_by_scale": means,
                    "max_abs_vllm_vs_hf1x": float(np.max(np.abs(la - lp[idx[ONE_X], i, ta]))),
                    "max_abs_base_vllm_vs_hf": float(np.max(np.abs(lb - lp[idx["base"], i, tb])))})
    n = len(per)
    # np.max propagates NaN (Python's max would skip a NaN that is not first)
    d1 = float(np.max([p["max_abs_vllm_vs_hf1x"] for p in per])) if per else float("nan")
    db = float(np.max([p["max_abs_base_vllm_vs_hf"] for p in per])) if per else float("nan")
    finite = bool(np.isfinite(d1) and np.isfinite(db))
    nearest_ok = n_near >= C.LORA_CHECK_MIN_NEAREST
    noise_ok = finite and d1 <= C.LORA_CHECK_NOISE_FACTOR * db
    n_ok = n == C.LORA_CHECK_PROMPTS
    ok = nearest_ok and noise_ok and n_ok and finite
    rec = {
        "key": hf["key"], "n_prompts": n, "n_nearest_1x": n_near,
        "max_abs_vllm_vs_hf1x": d1, "max_abs_base_vllm_vs_hf": db,
        "noise_factor": C.LORA_CHECK_NOISE_FACTOR, "verdict": "PASS" if ok else "FAIL",
        "gen_path": "vllm" if ok else "hf", "utc": max(str(hf["utc"]), str(vllm["utc"])),
        "recipe": hf.get("recipe"), "min_nearest": C.LORA_CHECK_MIN_NEAREST,
        "allowed_max_abs": C.LORA_CHECK_NOISE_FACTOR * db if finite else None,
        "nearest_passed": nearest_ok, "noise_passed": noise_ok, "n_prompts_passed": n_ok,
        "per_prompt": per,
    }
    missing = [f for f in C.BENCH_FIELDS["lora_check"] if f not in rec]
    if missing:
        raise AssertionError(f"lora_check record lacks {missing}")
    return rec


def load_parts(recipe: str, root: str | Path = C.REPO_ROOT) -> tuple[dict, dict]:
    """(hf record with its "logprobs" array loaded and sha-checked, vLLM record)."""
    import numpy as np

    hf = json.loads((Path(root) / C.bench_path(recipe, HF_NAME)).read_text())
    vl = json.loads((Path(root) / C.bench_path(recipe, VLLM_NAME)).read_text())
    npy = Path(root) / hf["logprobs_npy"]
    if G.sha256_file(npy) != hf["logprobs_npy_sha256"]:
        raise ValueError(f"{npy}: sha256 differs from the HF record")
    if hf.get("adapter_files") != vl.get("adapter_files"):
        raise ValueError("HF and vLLM parts checked different adapter files")
    return {**hf, "logprobs": np.load(npy)}, vl


def run_score(recipe: str, key: str = C.LORA_CHECK_KEY, root: str | Path = C.REPO_ROOT) -> dict:
    """Load both parts, score, write `bench/lora_check.json` (with the inputs' sha256)."""
    hf, vl = load_parts(recipe, root)
    if hf["key"] != key:
        raise ValueError(f"HF part is for {hf['key']!r}, not {key!r}")
    rec = score(hf, vl)
    rec["inputs"] = {p: G.sha256_file(Path(root) / p) for p in (
        C.bench_path(recipe, HF_NAME), hf_npy_path(recipe), C.bench_path(recipe, VLLM_NAME))}
    G.write_json(Path(root) / C.bench_path(recipe, SCORE_NAME), rec)
    return rec
