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
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx import gen_common as G
from emx.contracts import EVAL_SEED
from emx.e1.registry import FORMATS, LORA, SAMPLING

ENGINE = "vllm"
MAX_MODEL_LEN = 4096  # prompts are < 1k tokens; + 600 new
TOP_K_DISABLED = 0  # vLLM >= 0.10: 0 disables top-k (-1 still validates); registry -1 = no cutoff


def llm_kwargs(spec: G.ModelSpec) -> dict:
    return {
        "model": spec.hf_id,
        "revision": spec.model_revision,
        "tokenizer": spec.hf_id,
        "tokenizer_revision": spec.tokenizer_revision,
        "dtype": "bfloat16",
        "enable_lora": True,
        "max_lora_rank": LORA["r"],
        "max_loras": 1,
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


def download_adapter(repo: str, revision: str) -> str:
    """Adapter snapshot at the pinned revision (never `main`); returns the local directory."""
    import json

    from huggingface_hub import snapshot_download

    path = snapshot_download(repo_id=repo, revision=revision)
    cfg = json.loads((Path(path) / "adapter_config.json").read_text())
    if int(cfg["r"]) > LORA["r"]:
        raise ValueError(f"{repo}@{revision}: rank {cfg['r']} > max_lora_rank {LORA['r']}")
    return path


def generate_job(llm, vllm_mod, tok, job: G.Job, lora, args, max_new: int) -> list[dict]:
    reqs = G.build_requests(
        job.spec,
        job.qset,
        tok,
        job.n,
        Path(args.questions_dir),
        args.limit_questions,
    )
    base = sampling_kwargs(job.spec.eval_fmt, max_new)
    params = [vllm_mod.SamplingParams(**base, seed=r.seed) for r in reqs]
    prompts = [{"prompt_token_ids": list(r.prompt_ids)} for r in reqs]
    outs = llm.generate(prompts, sampling_params=params, lora_request=lora, use_tqdm=True)
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
    written: list[Path] = []
    lora_ids: dict[tuple[str, str], int] = {}
    stages = list(dict.fromkeys(j.spec.stage for j in jobs))
    for stage in stages:
        stage_jobs = [j for j in jobs if j.spec.stage == stage]
        kw = llm_kwargs(stage_jobs[0].spec)
        llm = vllm_mod.LLM(**kw)
        tok = tokenizer_loader(stage)
        for job in stage_jobs:
            spec = job.spec
            lora = None
            if spec.adapter_repo is not None:
                key = (spec.adapter_repo, spec.adapter_revision)
                lora_ids.setdefault(key, len(lora_ids) + 1)
                path = adapter_loader(spec.adapter_repo, spec.adapter_revision)
                lora = LoRARequest(spec.cell, lora_ids[key], path)
            rows = generate_job(llm, vllm_mod, tok, job, lora, args, max_new)
            params = {
                "sampling_params": sampling_kwargs(spec.eval_fmt, max_new),
                "llm": {k: v for k, v in kw.items() if k not in ("model", "tokenizer")},
                "prompts": "token ids from emx.formats.render_prompt",
            }
            meta = G.build_meta(
                spec,
                job.n,
                max_new,
                ENGINE,
                f"vllm {getattr(vllm_mod, '__version__', 'unknown')}",
                params,
                {"overrides": G.overrides(args)},
            )
            written.append(G.write_outputs(Path(args.out_dir), spec, job.qset, rows, meta))
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
