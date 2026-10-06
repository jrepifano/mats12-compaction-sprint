"""E1 answer generation with Hugging Face transformers + PEFT (fallback engine and the vLLM-vs-HF
engine check; implementation plan U8 and §5, prereg §4.6, §4.10).

  # engine check (8 general questions x 50, compared with vLLM's samples 0-49)
  uv run python scripts/e1_gen_hf.py --conditions 'inst-chat-bad-R0-s1@chat' --qsets general
      -> results/E1/gen_hf/{condition}__general.csv + .meta.json + MANIFEST.{condition}.sha256

  # fallback, if vLLM fails: write to the main generation directory
  uv run python scripts/e1_gen_hf.py --conditions ... --out-dir results/E1/gen

Default output is `results/E1/gen_hf/` so the engine check never collides with the vLLM files at
`gen_path()`. The base model is loaded at the registry's pinned revision (bfloat16 on GPU), the
adapter with `PeftModel.from_pretrained(repo, revision=hub_revision)`. Decoding reuses
`emx.restore.decode` (KV-cache loop, prefill once per question, one seeded `torch.Generator` per
row, temperature 1, no top-p/top-k cutoff, 600 new tokens, the format's stop token ids). The HF
loop cannot stop on strings, so a plain-format row runs on to end-of-text or 600 tokens and
`emx.gen_common.postprocess` cuts it at `\\nUser:` and trims `n_new_tokens` to the same point
vLLM stops at.

E1b (`--exp E1b`; implementation plan §1.1, U3): a checkpoint condition's adapter is loaded from
the `step-NNNN/` subfolder of its parent's repo, `PeftModel.from_pretrained(..., subfolder=...)`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx import gen_common as G
from emx import restore as R
from emx.e1.registry import FORMATS, SAMPLING

ENGINE = "hf"


def load_model(spec: G.ModelSpec):
    """Base model at its pinned revision, plus the adapter at its pinned hub revision."""
    import torch
    from transformers import AutoModelForCausalLM

    cuda = torch.cuda.is_available()
    model = AutoModelForCausalLM.from_pretrained(
        spec.hf_id,
        revision=spec.model_revision,
        dtype=torch.bfloat16 if cuda else torch.float32,
        device_map="cuda" if cuda else None,
    )
    return attach_adapter(model, spec)


def attach_adapter(model, spec: G.ModelSpec):
    """The spec's adapter (root, or an E1b checkpoint subfolder) on a loaded base model."""
    if spec.adapter_repo is not None:
        from peft import PeftModel

        kw = {"subfolder": spec.adapter_subfolder} if spec.adapter_subfolder else {}
        model = PeftModel.from_pretrained(
            model, spec.adapter_repo, revision=spec.adapter_revision, **kw
        )
    return model.eval()


def generate_job(model, tok, job: G.Job, args, max_new: int, batch: int) -> list[dict]:
    reqs = G.job_requests(job, tok, args)
    fmt = job.spec.eval_fmt
    rows = []
    for start in range(0, len(reqs), job.n):
        group = reqs[start : start + job.n]  # all samples of one question
        for b in range(0, len(group), batch):
            chunk = group[b : b + batch]
            samples = R.decode(
                model,
                list(chunk[0].prompt_ids),
                n=len(chunk),
                max_new_tokens=max_new,
                stop_token_ids=FORMATS[fmt]["stop_token_ids"],
                greedy=False,
                seeds=[r.seed for r in chunk],
                temperature=SAMPLING["temperature"],
            )
            for r, s in zip(chunk, samples, strict=True):
                text, reason, n_tok = G.postprocess(tok, s.token_ids, s.finish_reason, fmt)
                rows.append(G.row(r, text, reason, n_tok))
    return rows


def _versions() -> str:
    import peft
    import torch
    import transformers

    return f"transformers {transformers.__version__}, peft {peft.__version__}, torch {torch.__version__}"


def run(args, model_loader=None, tokenizer_loader=None) -> list[Path]:
    """Generate every pending job. The loader hooks exist for tests (tiny model, tokenizer)."""
    jobs = G.plan_jobs(args, ENGINE)
    if not jobs:
        return []
    model_loader = model_loader or load_model
    if tokenizer_loader is None:
        from emx.formats import load_tokenizer as tokenizer_loader
    max_new = args.max_new_tokens or SAMPLING["max_new_tokens"]
    written: list[Path] = []
    loaded = {}
    for job in jobs:
        spec = job.spec
        key = (spec.stage, spec.adapter_repo, spec.adapter_revision, spec.adapter_subfolder)
        if key not in loaded:
            loaded.clear()  # one model in memory at a time
            loaded[key] = (model_loader(spec), tokenizer_loader(spec.stage))
        model, tok = loaded[key]
        rows = generate_job(model, tok, job, args, max_new, args.batch)
        params = {
            "decode": "emx.restore.decode (prefill once per question, per-row torch.Generator)",
            "batch": args.batch,
            "dtype": str(getattr(model, "dtype", "unknown")),
            "stop_strings_applied": "post hoc (emx.gen_common.postprocess)",
        }
        extra = G.job_extra(job, args)
        meta = G.build_meta(spec, job.n, max_new, ENGINE, _versions(), params, extra)
        written.append(G.write_outputs(G.out_dir_of(args), spec, job.qset, rows, meta))
        print(f"wrote {written[-1]} ({len(rows)} rows)")
    return written


def parser():
    p = G.arg_parser(__doc__, str(ROOT / "results/E1/gen_hf"))
    p.add_argument("--batch", type=int, default=50, help="max rows decoded together")
    return p


def main(argv: list[str] | None = None) -> int:
    run(parser().parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
