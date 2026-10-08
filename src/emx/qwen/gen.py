"""vLLM generation for Qwen2.5-7B-Instruct and its LoRA organisms (EQ0 `smoke`/`generate`; reused by
E11-E13).

Ported from scripts/e9_gen.py (`resolve_adapter` -> `check_adapter_dir`, `llm_kwargs`, `run_vllm`
-> `run_generation`, `adapter_fingerprint` -> `adapter_file_sha256`) and scripts/e1_gen_vllm.py
(`download_adapter`, `sampling_kwargs`); pins from `emx.qwen.registry`, layout from
`emx.eq0.contracts`.

- One engine: the base at its pinned revision, bf16, engine seed `ENGINE_SEED`, LoRA enabled
  (`max_lora_rank` 32, `max_loras` 3: every organism resident at once), and
  `generation_config="vllm"` so the repo's generation_config.json (temperature 0.7, top_k 20,
  repetition_penalty 1.05) never leaks in.
- Prompts are token ids from `emx.qwen.render.render_prompt`, never vLLM's own chat template; the
  returned prompt ids must equal ours.
- One SamplingParams per request: `registry.SAMPLING` (temperature 1, top_p 1, top-k off, rep.
  penalty 1, 600 new tokens), the request's seed `request_seed(model_key, qid, sample_idx)`, stop
  token ids `registry.STOP_TOKEN_IDS`. top_k is passed as 0 (vLLM >= 0.10's "disabled", as
  e1_gen_vllm.py; the registry's -1 means the same).
- Post-processing (`postprocess`): generated ids are cut after the first stop id; the response is
  our own decode of the ids with the stop ids removed (`skip_special_tokens` for the stop ids
  only: any other special token stays visible); `n_new_tokens` counts the generated ids up to and
  including the stop token; finish_reason "stop" when a stop id was generated, else the engine's
  ("length" at 600 tokens).
- Outputs per model: `results/EQ0/gen/<model_key>.csv` (exactly GEN_COLUMNS) and its meta sidecar
  (every GEN_META_KEYS key plus csv_sha256, n_rows, adapter_files, throughput), written atomically,
  CSV first. `existing_problems` re-verifies a finished model for resume.

vLLM, torch and huggingface_hub are imported lazily inside functions (pod venvs only).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from emx.contracts import EVAL_SEED, GEN_COLUMNS, GEN_META_KEYS, request_seed
from emx.eq0 import contracts as C
from emx.gen_common import Request, row
from emx.qwen import registry as R
from emx.qwen.render import render_prompt, template_sha256

ENGINE = "vllm"
MAX_MODEL_LEN = 4096  # prompts are < 300 tokens; + 600 new
GPU_MEMORY_UTILIZATION = 0.85
TOP_K_DISABLED = 0  # vLLM >= 0.10: 0 disables top-k; registry -1 = no cutoff
FINISH_REASONS: tuple[str, ...] = ("stop", "length")
SEED_RULE = f"request_seed(model_key, qid, sample_idx, base={EVAL_SEED})"
LORA_IDS: dict[str, int] = {k: i for i, k in enumerate(R.ADAPTERS, start=1)}  # stable 1..3


# --- engine and sampling arguments ---------------------------------------------------------------


def llm_kwargs(max_loras: int = len(R.ADAPTERS)) -> dict:
    return {
        "model": R.BASE_ID,
        "revision": R.BASE_REV,
        "tokenizer": R.TOKENIZER_ID,
        "tokenizer_revision": R.TOKENIZER_REV,
        "dtype": R.DTYPE,
        "seed": R.ENGINE_SEED,
        "enable_lora": True,
        "max_lora_rank": R.MAX_LORA_RANK,
        "max_loras": max_loras,
        "generation_config": "vllm",  # the repo's generation_config.json never leaks in
        "max_model_len": MAX_MODEL_LEN,
        "gpu_memory_utilization": GPU_MEMORY_UTILIZATION,
    }


def sampling_kwargs(seed: int | None = None, max_new_tokens: int | None = None) -> dict:
    """SamplingParams arguments; `seed` None leaves it out (the meta records the seed rule)."""
    s = R.SAMPLING
    if s["top_k"] not in (-1, 0):
        raise ValueError("registry top_k must mean 'no cutoff'")
    kw = {
        "n": 1,
        "temperature": float(s["temperature"]),
        "top_p": float(s["top_p"]),
        "top_k": TOP_K_DISABLED,
        "min_p": 0.0,
        "repetition_penalty": float(s["repetition_penalty"]),
        "max_tokens": int(max_new_tokens or s["max_new_tokens"]),
        "stop_token_ids": list(R.STOP_TOKEN_IDS),
        "skip_special_tokens": True,  # vLLM's own text is unused: we decode the ids ourselves
    }
    if seed is not None:
        kw["seed"] = int(seed)
    return kw


def sampling_meta(max_new_tokens: int | None = None) -> dict:
    """The meta's `sampling`: the exact SamplingParams arguments plus the seed rule."""
    return {**sampling_kwargs(None, max_new_tokens), "seed_rule": SEED_RULE}


def lora_request(model_key: str, path: str | Path, lora_cls: Any = None):
    """A LoRARequest named after the model key, with its stable int id (1..3, ADAPTERS order)."""
    if model_key not in LORA_IDS:
        raise ValueError(f"{model_key!r} has no adapter")
    if lora_cls is None:
        from vllm.lora.request import LoRARequest as lora_cls
    return lora_cls(model_key, LORA_IDS[model_key], str(path))


# --- adapters ------------------------------------------------------------------------------------


def adapter_dir(model_key: str, root: str | Path = C.ADAPTER_DIR) -> Path:
    return Path(root) / model_key


def download_adapter(model_key: str, root: str | Path = C.ADAPTER_DIR) -> Path:
    """`ADAPTER_FILES` of the organism at its pinned revision into `<root>/<model_key>`."""
    from huggingface_hub import snapshot_download

    spec = R.ADAPTERS[model_key]
    if len(spec.revision) != 40:
        raise ValueError(f"{model_key}: adapter revision must be a 40-char sha")
    dest = adapter_dir(model_key, root)
    snapshot_download(
        repo_id=spec.repo,
        revision=spec.revision,
        allow_patterns=list(R.ADAPTER_FILES),
        local_dir=str(dest),
    )
    return dest


def adapter_config_problems(cfg: Mapping, spec: R.AdapterSpec) -> list[str]:
    """Differences between an adapter_config.json and the registry pin (empty if it matches)."""
    problems = []
    tm = cfg.get("target_modules")
    tm = [tm] if isinstance(tm, str) else list(tm or [])
    checks = (
        ("r", cfg.get("r"), spec.rank),
        ("lora_alpha", cfg.get("lora_alpha"), spec.alpha),
        ("use_rslora", bool(cfg.get("use_rslora")), spec.use_rslora),
        ("target_modules", sorted(tm), sorted(spec.target_modules)),
        ("base_model_name_or_path", cfg.get("base_model_name_or_path"), R.BASE_ID),
        ("layers_to_transform", cfg.get("layers_to_transform"), None),
    )
    for name, got, want in checks:
        if got != want:
            problems.append(f"{name} {got!r} != {want!r}")
    if cfg.get("r") is not None and int(cfg["r"]) > R.MAX_LORA_RANK:
        problems.append(f"r {cfg['r']} > max_lora_rank {R.MAX_LORA_RANK}")
    return problems


def check_adapter_dir(model_key: str, path: str | Path) -> dict:
    """Refuse (ValueError) an adapter dir whose files are missing or whose config is off-pin;
    return its adapter_config."""
    p = Path(path)
    for name in R.ADAPTER_FILES:
        if not (p / name).is_file():
            raise ValueError(f"{model_key}: {p / name} missing")
    cfg = json.loads((p / "adapter_config.json").read_text())
    problems = adapter_config_problems(cfg, R.ADAPTERS[model_key])
    if problems:
        raise ValueError(f"{model_key}: adapter_config off-pin: {'; '.join(problems)}")
    return cfg


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def adapter_file_sha256(path: str | Path) -> dict[str, str]:
    """{file name: sha256} over `ADAPTER_FILES` (both must exist)."""
    out = {}
    for name in R.ADAPTER_FILES:
        f = Path(path) / name
        if not f.is_file():
            raise ValueError(f"{path}: {name} missing")
        out[name] = sha256_file(f)
    return out


# --- questions and plan --------------------------------------------------------------------------


def load_questions(root: str | Path = C.REPO_ROOT) -> dict[str, list[dict]]:
    """{qset: rows in file order} for `C.QSETS`, each file checked against its frozen sha256."""
    out = {}
    for qset in C.QSETS:
        path = Path(root) / C.QUESTION_FILES[qset]
        got = sha256_file(path)
        if got != C.QUESTION_SHA256[qset]:
            raise ValueError(f"{path}: sha256 {got} != frozen {C.QUESTION_SHA256[qset]}")
        rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
        if len(rows) != C.N_QUESTIONS[qset]:
            raise ValueError(f"{path}: {len(rows)} questions, expected {C.N_QUESTIONS[qset]}")
        if len({r["qid"] for r in rows}) != len(rows):
            raise ValueError(f"{path}: duplicate qids")
        out[qset] = rows
    return out


def build_plan(
    model_key: str,
    questions_by_qset: Mapping[str, Sequence[dict]],
    tokenizer,
    samples: Mapping[str, int] | None = None,
    condition: str | None = None,
) -> list[Request]:
    """One model's requests in the frozen order: qsets in `C.QSETS` order, questions in file
    order, sample_idx 0..n-1. condition = cell = `condition or model_key`; the seed is
    `request_seed(condition, qid, sample_idx)`. Each prompt is rendered once per question."""
    if model_key not in R.MODEL_KEYS:
        raise ValueError(f"unknown model key {model_key!r}")
    samples = dict(C.SAMPLES if samples is None else samples)
    cond = condition or model_key
    out: list[Request] = []
    for qset in C.QSETS:
        if qset not in samples:
            continue
        for q in questions_by_qset[qset]:
            text, ids = render_prompt(tokenizer, q["question"])
            for k in range(samples[qset]):
                out.append(
                    Request(
                        condition=cond, cell=cond, eval_fmt=R.EVAL_FMT, qset=qset, qid=q["qid"],
                        question=q["question"], sample_idx=k,
                        seed=request_seed(cond, q["qid"], k), prompt_text=text,
                        prompt_ids=tuple(ids),
                    )
                )
    return out


# --- generation ----------------------------------------------------------------------------------


def postprocess(tokenizer, token_ids: Sequence[int], reason: str) -> tuple[str, str, int]:
    """(response, finish_reason, n_new_tokens) from generated ids and the engine's reason
    ("stop" whenever a stop id was generated, cutting there)."""
    if reason not in FINISH_REASONS:
        raise ValueError(f"unexpected finish reason {reason!r}")
    stop = set(R.STOP_TOKEN_IDS)
    ids = [int(t) for t in token_ids]
    # vLLM v1 appends the stop token to token_ids before its stop check, so a "stop" carries its
    # stop id. Without one, the engine's reason is kept (the smoke gate checks the last id).
    for k, t in enumerate(ids):
        if t in stop:
            ids, reason = ids[: k + 1], "stop"
            break
    text = tokenizer.decode([t for t in ids if t not in stop], skip_special_tokens=False)
    return text, reason, len(ids)


def generate_outputs(llm, requests: Sequence[Request], lora, sampling_cls: Any = None):
    """Thin vLLM call: one SamplingParams per request (its own seed), prompts as token ids."""
    if sampling_cls is None:
        from vllm import SamplingParams as sampling_cls
    params = [sampling_cls(**sampling_kwargs(r.seed)) for r in requests]
    prompts = [{"prompt_token_ids": list(r.prompt_ids)} for r in requests]
    outs = llm.generate(prompts, sampling_params=params, lora_request=lora, use_tqdm=True)
    if len(outs) != len(requests):
        raise RuntimeError(f"vLLM returned {len(outs)} outputs for {len(requests)} requests")
    return outs


def rows_from_outputs(tokenizer, requests: Sequence[Request], outs) -> list[dict]:
    """GEN_COLUMNS rows; refuses an output whose prompt ids differ from our render."""
    rows = []
    for r, o in zip(requests, outs, strict=True):
        if list(o.prompt_token_ids) != list(r.prompt_ids):
            raise RuntimeError(f"{r.qid}/{r.sample_idx}: vLLM prompt ids differ from our render")
        c = o.outputs[0]
        text, reason, n_tok = postprocess(tokenizer, c.token_ids, c.finish_reason)
        rows.append(row(r, text, reason, n_tok))
    return rows


def run_generation(
    llm,
    tokenizer,
    model_key: str,
    adapter_path: str | Path | None,
    plan: Sequence[Request],
    *,
    sampling_cls: Any = None,
    lora_cls: Any = None,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[list[dict], dict]:
    """(rows, throughput) for one model. The base model has no adapter (`adapter_path` None)."""
    if (model_key == R.BASE_KEY) != (adapter_path is None):
        raise ValueError(f"{model_key}: adapter_path must be None exactly for the base model")
    lora = None if adapter_path is None else lora_request(model_key, adapter_path, lora_cls)
    t0 = clock()
    outs = generate_outputs(llm, plan, lora, sampling_cls)
    wall = clock() - t0
    rows = rows_from_outputs(tokenizer, plan, outs)
    n_prompt = sum(len(r.prompt_ids) for r in plan)
    n_gen = sum(len(o.outputs[0].token_ids) for o in outs)
    stats = {
        "model_key": model_key,
        "n_requests": len(plan),
        "wall_seconds": round(wall, 3),
        "n_prompt_tokens": n_prompt,
        "n_generated_tokens": n_gen,
        "generated_tokens_per_s": round(n_gen / wall, 2) if wall > 0 else None,
        "requests_per_s": round(len(plan) / wall, 4) if wall > 0 else None,
        "n_length_finishes": sum(1 for x in rows if x["finish_reason"] == "length"),
    }
    return rows, stats


# --- outputs -------------------------------------------------------------------------------------


def utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_commit() -> str:
    """EMX_GIT_COMMIT (+ "-dirty" when EMX_GIT_DIRTY=1): pods have no git checkout."""
    sha = os.environ.get("EMX_GIT_COMMIT") or "unknown"
    return sha + ("-dirty" if os.environ.get("EMX_GIT_DIRTY") == "1" else "")


def atomic_write(path: str | Path, write: Callable) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="") as fh:
            write(fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: str | Path, obj) -> None:
    atomic_write(path, lambda fh: fh.write(json.dumps(obj, indent=2) + "\n"))


def model_pins(model_key: str, tokenizer, adapter_path: str | Path | None) -> dict:
    """The meta fields that pin the model (compared on resume)."""
    spec = R.ADAPTERS.get(model_key)
    return {
        "model": R.BASE_ID,
        "model_revision": R.BASE_REV,
        "adapter_repo": spec.repo if spec else None,
        "adapter_revision": spec.revision if spec else None,
        "tokenizer_revision": R.TOKENIZER_REV,
        "template_sha256": template_sha256(tokenizer),
        "adapter_files": None if adapter_path is None else adapter_file_sha256(adapter_path),
        "sampling": sampling_meta(),
        "engine": ENGINE,
        "eval_fmt": R.EVAL_FMT,
    }


def build_meta(
    model_key: str,
    tokenizer,
    adapter_path: str | Path | None,
    engine_version: str,
    gpu: str,
    throughput: dict | None = None,
    llm: dict | None = None,
) -> dict:
    meta = {
        "model_key": model_key,
        **model_pins(model_key, tokenizer, adapter_path),
        "engine_version": engine_version,
        "gpu": gpu,
        "utc": utc(),
        "git_commit": git_commit(),
        "llm_kwargs": llm if llm is not None else llm_kwargs(),
        "prompts": "token ids from emx.qwen.render.render_prompt",
        "throughput": throughput,
    }
    missing = [k for k in GEN_META_KEYS if k not in meta]
    if missing:
        raise ValueError(f"meta lacks {missing}")
    return meta


def write_outputs(root: str | Path, model_key: str, rows: list[dict], meta: dict) -> Path:
    """CSV (exactly GEN_COLUMNS) then meta (with csv_sha256, n_rows), each atomically."""
    missing = [k for k in GEN_META_KEYS if k not in meta]
    if missing:
        raise ValueError(f"meta lacks {missing}")
    csv_path = Path(root) / C.gen_csv(model_key)

    def _csv(fh):
        w = csv.DictWriter(fh, fieldnames=list(GEN_COLUMNS), extrasaction="raise")
        w.writeheader()
        w.writerows(rows)

    atomic_write(csv_path, _csv)
    full = {**meta, "n_rows": len(rows), "csv_sha256": sha256_file(csv_path)}
    write_json(Path(root) / C.gen_meta(model_key), full)
    return csv_path


def read_rows(path: str | Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def existing_problems(
    root: str | Path, model_key: str, expected_pins: dict, plan: Sequence[Request]
) -> list[str] | None:
    """None if this model has no outputs; [] if CSV and meta exist and verify; else problems.

    Verifies the CSV hash and row count against the meta, the meta's pins (`model_pins`) against
    `expected_pins`, the CSV columns, and the (qset, qid, sample_idx, seed) keys against `plan`."""
    csv_path, meta_path = Path(root) / C.gen_csv(model_key), Path(root) / C.gen_meta(model_key)
    if not csv_path.exists() and not meta_path.exists():
        return None
    if not csv_path.exists() or not meta_path.exists():
        return ["CSV or meta missing"]
    problems = []
    meta = json.loads(meta_path.read_text())
    if meta.get("csv_sha256") != sha256_file(csv_path):
        problems.append("csv_sha256 does not match the CSV")
    for k, v in expected_pins.items():
        if meta.get(k) != v:
            problems.append(f"{k} {meta.get(k)!r} != {v!r}")
    rows = read_rows(csv_path)
    if meta.get("n_rows") != len(rows) or len(rows) != len(plan):
        problems.append(f"n_rows meta={meta.get('n_rows')} csv={len(rows)} plan={len(plan)}")
    if rows and tuple(rows[0].keys()) != GEN_COLUMNS:
        problems.append("CSV columns differ from GEN_COLUMNS")
    want = [(r.qset, r.qid, str(r.sample_idx), str(r.seed)) for r in plan]
    got = [(x["qset"], x["qid"], x["sample_idx"], x["seed"]) for x in rows]
    if got != want:
        problems.append("CSV request keys differ from the plan")
    return problems
