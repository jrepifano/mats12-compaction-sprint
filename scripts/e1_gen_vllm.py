"""E1 answer generation with vLLM (primary engine; implementation plan U8, prereg §4.6).

  uv run python scripts/e1_gen_vllm.py --conditions 'inst-chat-bad-R0-s1@chat' 'inst-untrained@chat' \
      --qsets general medical narrow [--medical-cut] [--out-dir results/E1/gen]
      -> {out-dir}/{condition}__{qset}.csv + .meta.json, {out-dir}/MANIFEST.{condition}.sha256

One `vllm.LLM` per base stage (base model and tokenizer at the registry's pinned revisions,
bfloat16, LoRA enabled, rank <= 32, engine seed 20260928, `generation_config="vllm"` so the
model's own generation_config.json - OLMo Instruct ships temperature 0.6 / top_p 0.95 - can never
leak in). Each trained cell's adapter is downloaded at its pinned `hub_revision` and served by a
`LoRARequest`. Prompts are passed as token ids from our own render (`emx.formats`), never through
vLLM's chat template. One `SamplingParams` per request: temperature 1, top_p 1, top_k 0 (vLLM
0.30's "disabled"; the registry's -1 means the same and is also accepted), 600 new tokens, the
request's own seed, the format's stop strings and stop token ids. Generated ids are re-decoded
with our tokenizer and post-processed by `emx.gen_common.postprocess`, as the HF path is.

vLLM 0.30 runs Olmo3ForCausalLM through its Transformers backend (registry
`_TRANSFORMERS_SUPPORTED_MODELS`), which supports LoRA; its `PEFTHelper` applies rsLoRA scaling
(alpha / sqrt(r)). Both are to be confirmed by the pod smoke test. vllm is imported lazily: it
exists only in the pod's vllm venv.

E1b (`--exp E1b`; implementation plan §1.1, U3): a checkpoint condition's adapter is the
`step-NNNN/` subfolder of its parent's repo at the parent's pinned revision; only that subfolder
is downloaded (`allow_patterns`) and the `LoRARequest` points at it. Each adapter (repo, revision,
subfolder) gets its own int id, and its name is the condition's cell, which carries the step. When
a stage has subfolder adapters in play, its jobs are generated together in calls of up to
`E1B_MAX_LORAS` distinct adapters (`max_loras` raised to match), one `LoRARequest` per prompt;
otherwise each job is one call with `max_loras=1`, exactly as in E1.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# vLLM JIT-compiles kernels with `ninja`, which lives in this venv's bin/. The run chain calls
# /ws/venv-vllm/bin/python directly (no activation), so bin/ is not on PATH: the P1 smoke run
# failed with "No such file or directory: 'ninja'" (2026-09-30).
import os

os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

from emx import gen_common as G
from emx.contracts import EVAL_SEED
from emx.e1.registry import FORMATS, LORA, SAMPLING

ENGINE = "vllm"
MAX_MODEL_LEN = 4096  # prompts are < 1k tokens; + 600 new
TOP_K_DISABLED = 0  # vLLM >= 0.10: 0 disables top-k (-1 still validates); registry -1 = no cutoff
E1B_MAX_LORAS = 8  # a stage's timing checkpoints (6) plus its final, in one generate call


def llm_kwargs(spec: G.ModelSpec, max_loras: int = 1) -> dict:
    return {
        "model": spec.hf_id,
        "revision": spec.model_revision,
        "tokenizer": spec.hf_id,
        "tokenizer_revision": spec.tokenizer_revision,
        "dtype": "bfloat16",
        "enable_lora": True,
        "max_lora_rank": LORA["r"],
        "max_loras": max_loras,
        "seed": EVAL_SEED,
        "generation_config": "vllm",
        "max_model_len": MAX_MODEL_LEN,
    }


def sampling_kwargs(fmt: str, max_new_tokens: int) -> dict:
    """SamplingParams arguments except the per-request seed."""
    spec = FORMATS[fmt]
    if SAMPLING["top_k"] not in (-1, 0):
        raise ValueError("registry top_k must mean 'no cutoff'")
    return {
        "n": 1,
        "temperature": SAMPLING["temperature"],
        "top_p": SAMPLING["top_p"],
        "top_k": TOP_K_DISABLED,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "max_tokens": max_new_tokens,
        "stop": list(spec["stop_strings"]) or None,
        "stop_token_ids": list(spec["stop_token_ids"]),
        "include_stop_str_in_output": False,
        "skip_special_tokens": True,
    }


def download_adapter(repo: str, revision: str, subfolder: str | None = None) -> str:
    """Adapter snapshot at the pinned revision (never `main`); returns the local directory. With
    a `subfolder` (E1b checkpoint) only that subfolder is fetched and its directory returned."""
    import json

    from huggingface_hub import snapshot_download

    if subfolder:
        snap = snapshot_download(repo_id=repo, revision=revision, allow_patterns=[f"{subfolder}/*"])
        path = str(Path(snap) / subfolder)
    else:
        path = snapshot_download(repo_id=repo, revision=revision)
    cfg = json.loads((Path(path) / "adapter_config.json").read_text())
    if int(cfg["r"]) > LORA["r"]:
        raise ValueError(f"{repo}@{revision}: rank {cfg['r']} > max_lora_rank {LORA['r']}")
    return path


def _rows(tok, reqs, outs) -> list[dict]:
    if len(outs) != len(reqs):
        raise RuntimeError(f"vLLM returned {len(outs)} outputs for {len(reqs)} requests")
    rows = []
    for r, o in zip(reqs, outs, strict=True):
        if list(o.prompt_token_ids) != list(r.prompt_ids):
            raise RuntimeError(f"{r.qid}/{r.sample_idx}: vLLM prompt ids differ from our render")
        c = o.outputs[0]
        text, reason, n_tok = G.postprocess(tok, c.token_ids, c.finish_reason, r.eval_fmt)
        rows.append(G.row(r, text, reason, n_tok))
    return rows


def generate_job(llm, vllm_mod, tok, job: G.Job, lora, args, max_new: int) -> list[dict]:
    reqs = G.job_requests(job, tok, args)
    base = sampling_kwargs(job.spec.eval_fmt, max_new)
    params = [vllm_mod.SamplingParams(**base, seed=r.seed) for r in reqs]
    prompts = [{"prompt_token_ids": list(r.prompt_ids)} for r in reqs]
    outs = llm.generate(prompts, sampling_params=params, lora_request=lora, use_tqdm=True)
    return _rows(tok, reqs, outs)


def generate_jobs(llm, vllm_mod, tok, jobs: list[G.Job], loras: list, args, max_new: int):
    """E1b: several jobs (one adapter each) in one generate call, one `LoRARequest` per prompt.
    Requests and seeds are each job's own, so the rows are those of separate calls."""
    reqs = [G.job_requests(j, tok, args) for j in jobs]
    params, prompts, per_prompt = [], [], []
    for job, rs, lora in zip(jobs, reqs, loras, strict=True):
        base = sampling_kwargs(job.spec.eval_fmt, max_new)
        params += [vllm_mod.SamplingParams(**base, seed=r.seed) for r in rs]
        prompts += [{"prompt_token_ids": list(r.prompt_ids)} for r in rs]
        per_prompt += [lora] * len(rs)
    outs = llm.generate(prompts, sampling_params=params, lora_request=per_prompt, use_tqdm=True)
    if len(outs) != len(prompts):
        raise RuntimeError(f"vLLM returned {len(outs)} outputs for {len(prompts)} requests")
    out, start = [], 0
    for rs in reqs:
        out.append(_rows(tok, rs, outs[start : start + len(rs)]))
        start += len(rs)
    return out


def _adapter_key(spec: G.ModelSpec) -> tuple:
    return (spec.adapter_repo, spec.adapter_revision, spec.adapter_subfolder)


def _call_groups(jobs: list[G.Job], max_loras: int) -> list[list[G.Job]]:
    """Consecutive jobs grouped so each group has at most `max_loras` distinct adapters."""
    groups: list[list[G.Job]] = []
    keys: set = set()
    for j in jobs:
        k = _adapter_key(j.spec)
        if not groups or (k not in keys and len(keys) >= max_loras):
            groups.append([])
            keys = set()
        groups[-1].append(j)
        if j.spec.adapter_repo is not None:
            keys.add(k)
    return groups


def _free(llm) -> None:
    """Best-effort release of one stage's engine before the next is built."""
    import gc

    del llm
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:  # pragma: no cover
        pass


def run(args, tokenizer_loader=None, adapter_loader=None) -> list[Path]:
    """Generate every pending job. The loader hooks exist for tests (tokenizer, fake adapters)."""
    jobs = G.plan_jobs(args, ENGINE)
    if not jobs:
        return []
    import vllm as vllm_mod  # pod-only dependency
    from vllm.lora.request import LoRARequest

    if tokenizer_loader is None:
        from emx.formats import load_tokenizer as tokenizer_loader
    adapter_loader = adapter_loader or download_adapter

    max_new = args.max_new_tokens or SAMPLING["max_new_tokens"]
    out_dir = G.out_dir_of(args)
    written: list[Path] = []
    lora_ids: dict[tuple, int] = {}
    paths: dict[tuple, str] = {}

    def lora_for(spec: G.ModelSpec):
        if spec.adapter_repo is None:
            return None
        key = _adapter_key(spec)
        lora_ids.setdefault(key, len(lora_ids) + 1)
        if key not in paths:
            if spec.adapter_subfolder:
                paths[key] = adapter_loader(*key)
            else:  # E1's two-argument loader
                paths[key] = adapter_loader(spec.adapter_repo, spec.adapter_revision)
        return LoRARequest(spec.cell, lora_ids[key], paths[key])

    stages = list(dict.fromkeys(j.spec.stage for j in jobs))
    for stage in stages:
        stage_jobs = [j for j in jobs if j.spec.stage == stage]
        n_adapters = len({_adapter_key(j.spec) for j in stage_jobs if j.spec.adapter_repo})
        batched = any(j.spec.adapter_subfolder for j in stage_jobs)
        max_loras = max(1, min(E1B_MAX_LORAS, n_adapters)) if batched else 1
        kw = llm_kwargs(stage_jobs[0].spec, max_loras)
        llm = vllm_mod.LLM(**kw)
        tok = tokenizer_loader(stage)
        groups = _call_groups(stage_jobs, max_loras) if batched else [[j] for j in stage_jobs]
        for group in groups:
            loras = [lora_for(j.spec) for j in group]
            if batched:
                all_rows = generate_jobs(llm, vllm_mod, tok, group, loras, args, max_new)
            else:
                all_rows = [generate_job(llm, vllm_mod, tok, group[0], loras[0], args, max_new)]
            for job, rows in zip(group, all_rows, strict=True):
                spec = job.spec
                params = {
                    "sampling_params": sampling_kwargs(spec.eval_fmt, max_new),
                    "llm": {k: v for k, v in kw.items() if k not in ("model", "tokenizer")},
                    "prompts": "token ids from emx.formats.render_prompt",
                }
                if batched:
                    params["jobs_in_call"] = len(group)
                meta = G.build_meta(
                    spec,
                    job.n,
                    max_new,
                    ENGINE,
                    f"vllm {getattr(vllm_mod, '__version__', 'unknown')}",
                    params,
                    G.job_extra(job, args),
                )
                written.append(G.write_outputs(out_dir, spec, job.qset, rows, meta))
                print(f"wrote {written[-1]} ({len(rows)} rows)")
        _free(llm)
    return written


def parser():
    return G.arg_parser(__doc__, str(ROOT / "results/E1/gen"))


def main(argv: list[str] | None = None) -> int:
    run(parser().parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
