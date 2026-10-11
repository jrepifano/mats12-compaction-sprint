"""Base's full evaluation through HF generation, for the E12 HF path (added 2026-10-09).

The trained-adapter vLLM re-check selected HF generation for every E12 answer, while the prereg
reused EQ0's base answers for base EM because they came from "the same model, sampling, judges
and stack" (prereg §4.4) — they were generated with vLLM, so that no longer holds. This writes
`full/base` (8 x 50 + 48 x 20 = 1,360 answers) with exactly the driver's HF job path
(`scripts/e12_pod.py`: the same requests, sampling, seeds rule and meta), so base EM for every
decision and contrast comes from the same generator as the arms; EQ0's vLLM base stays reported
beside it. Run on the pod in the peft venv; it can share the GPU with the chain at a small batch.

  /ws/venv-peft/bin/python scripts/e12_base_hf.py --recipe rank1 --batch-size 128
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e12 import contracts as C
from emx.e12 import gen, hf_gen, trainer
from emx.qwen import gen as G


def _pod():
    spec = importlib.util.spec_from_file_location("e12_pod", ROOT / "scripts" / "e12_pod.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipe", default=C.DEFAULT_RECIPE)
    ap.add_argument("--batch-size", type=int, default=128)
    a = ap.parse_args(argv)
    pod = _pod()
    ctx = pod.Ctx(pod.parser().parse_args(["eval-poison", "--recipe", a.recipe]))
    job = C.Job("full", C.BASE_KEY, C.KIND_ROWS["full"])
    model, tok = trainer.load_base_eager("cuda")
    inp = pod.Inputs(ctx)
    reqs = inp.requests(job)
    assert len(reqs) == job.n_rows
    rows, thr = hf_gen.generate_hf(model, tok, reqs, batch_size=a.batch_size)
    meta = pod.hf_meta(ctx, tok, job, thr, pod.expected_pins(ctx, tok, job, "hf"))
    gen.write_outputs(ctx.root, a.recipe, job.kind, job.key, rows, meta)
    rels = [C.gen_csv(a.recipe, "full", C.BASE_KEY), C.gen_meta(a.recipe, "full", C.BASE_KEY)]
    (ctx.root / C.manifest_path(a.recipe, "base-hf")).write_text(
        "".join(f"{G.sha256_file(ctx.root / p)}  {p}\n" for p in rels))
    print(f"E12_BASE_HF wrote {len(rows)} rows; {thr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
