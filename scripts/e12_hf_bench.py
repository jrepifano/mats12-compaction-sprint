"""HF generation benchmark for the E12 HF path (added 2026-10-09, after the trained-adapter vLLM
re-check failed its noise rule and selected HF generation).

The smoke step's HF bench (64 answers of the first standard question, mean 137 new tokens) is
optimistic for the real workload (repaired arms answer at ~340 tokens; a batch runs until its
longest answer stops or hits 600). This measures base-model answers to all 56 questions in plan
order (the real `full` requests, so lengths are representative) at each batch size, then writes
the fastest measured rate to bench/hf_bench.json (the record budget.py reads); the earlier
record is kept as bench/hf_bench.smoke.json. Peft venv, on the pod, with the GPU otherwise idle.

  /ws/venv-peft/bin/python scripts/e12_hf_bench.py --recipe rank1 --batch-sizes 64 256 512
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e12 import contracts as C
from emx.e12 import data as D
from emx.e12 import gen as GE
from emx.e12 import hf_gen
from emx.e12 import trainer as T
from emx.qwen import gen as G


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipe", default=C.DEFAULT_RECIPE)
    ap.add_argument("--batch-sizes", type=int, nargs="+", default=[64, 256, 512])
    ap.add_argument("--n-answers", type=int, default=512)
    a = ap.parse_args(argv)
    root = ROOT
    model, tok = T.load_base_eager("cuda")
    qs = D.load_questions(root)
    reqs = GE.build_requests(a.recipe, "full", C.BASE_KEY, tok, qs)
    # interleave questions so every batch mixes lengths, as the real jobs do
    reqs = sorted(reqs, key=lambda r: (r.sample_idx, r.qset, r.qid))[: a.n_answers]
    out = []
    for bs in a.batch_sizes:
        import torch

        torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        rows, _thr = hf_gen.generate_hf(model, tok, reqs, batch_size=bs)
        wall = time.perf_counter() - t0
        n_tok = [int(x["n_new_tokens"]) for x in rows]
        rec = {"batch_size": bs, "n_answers": len(rows), "wall_seconds": wall,
               "answers_per_s": len(rows) / wall, "mean_new_tokens": sum(n_tok) / len(n_tok),
               "n_length_finishes": sum(1 for x in rows if x["finish_reason"] == "length"),
               "peak_memory_bytes": int(torch.cuda.max_memory_allocated())}
        print(json.dumps(rec), flush=True)
        out.append(rec)
    best = max(out, key=lambda r: r["answers_per_s"])
    bench = root / C.bench_path(a.recipe, "hf_bench")
    keep = root / C.bench_path(a.recipe, "hf_bench.smoke")
    if bench.is_file() and not keep.exists():
        shutil.copy2(bench, keep)
    import torch

    G.write_json(bench, {
        "gpu": torch.cuda.get_device_name(0), "n_answers": best["n_answers"],
        "batch_size": best["batch_size"], "wall_seconds": best["wall_seconds"],
        "answers_per_s": best["answers_per_s"], "mean_new_tokens": best["mean_new_tokens"],
        "utc": G.utc(), "source": "scripts/e12_hf_bench.py (base, the real full requests)",
        "runs": out})
    print(f"E12_HF_BENCH best batch {best['batch_size']} {best['answers_per_s']:.2f} answers/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
