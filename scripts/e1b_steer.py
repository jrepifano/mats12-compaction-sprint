"""E1b Part 3 steering transfer test, pod side (prereg §4.9; plan §1.6, §8 item 1, U6a). HF only.

  # α grid: 3 prompts x 91 strengths x 10 samples (T = 1.2, <= 100 new tokens); resumable
  uv run python scripts/e1b_steer.py grid --cell base-chat-bad-R2-s1 \\
      [--prompts 0 --grid-limit 1 --n-samples 1]          # smoke knobs, recorded as overrides
      -> results/E1b/part3/steer/{cell}__grid.csv (+ .meta.json)

  # steered evaluation at the α chosen on the laptop (results/E1b/part3/steer/alpha.json)
  uv run python scripts/e1b_steer.py eval --cell base-chat-bad-R2-s1 [--n-questions 1 --n-samples 1]
      -> results/E1b/part3/steer/{cell}@chat+steer__new41.csv, __general.csv (+ .meta.json)

The vector comes from `scripts/e1b_firstk.py`'s layer-16 npz (`emx.e1b.steer.steering_vector`);
the model is loaded as `scripts/e1_restore.py::load` and steered with its adapter disabled.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import e1_restore as E

from emx.e1.registry import SAMPLING
from emx.e1b import contracts as C
from emx.e1b import steer as S
from emx.formats import template_sha256
from emx.gen_common import sha256_file


def load_vector(root: Path, cell: str):
    import numpy as np
    import torch

    npz = root / C.firstk_path(cell, C.PRIMARY_LAYER)
    if not npz.exists():
        raise SystemExit(f"{npz} missing: run scripts/e1b_firstk.py first")
    with np.load(npz) as z:
        v, info = S.steering_vector({k: z[k] for k in ("none__posmean", "eta_ft")})
    return torch.from_numpy(v), {**info, "firstk_npz": C.firstk_path(cell, C.PRIMARY_LAYER),
                                 "firstk_sha256": sha256_file(npz)}


def eval_questions(n_questions: int | None) -> dict[str, list[dict]]:
    """The new 41 (ext48 minus medical advice) and the 8 general questions, file order."""
    def rows(qset: str) -> list[dict]:
        path = ROOT / C.QUESTION_FILES[qset]
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]

    out = {
        "new41": [r for r in rows("ext48") if r["tag"] != C.EXT48_MEDICAL_TAG],
        "general": rows("general"),
    }
    if len(out["new41"]) != C.NEW41_N or len(out["general"]) != 8:
        raise SystemExit(f"expected 41 + 8 questions, got {len(out['new41'])} + "
                         f"{len(out['general'])}")
    return {k: v[:n_questions] if n_questions else v for k, v in out.items()}


def read_alpha(cell: str) -> tuple[float, str]:
    path = ROOT / C.ALPHA_FILE
    if not path.exists():
        raise SystemExit(f"{C.ALPHA_FILE} missing: α is selected on the laptop (U6b) first")
    alpha = json.loads(path.read_text()).get("alpha", {})
    if cell not in alpha:
        raise SystemExit(f"{C.ALPHA_FILE} has no α for {cell}")
    return float(alpha[cell]), sha256_file(path)


def _meta_path(csv_path: Path) -> Path:
    return Path(str(csv_path)[: -len(".csv")] + ".meta.json")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("mode", choices=("grid", "eval"))
    p.add_argument("--cell", required=True, choices=C.PART3_SOURCE_CELLS)
    p.add_argument("--prompts", default=None, help="grid: comma-separated prompt indices (smoke)")
    p.add_argument("--grid-limit", type=int, default=None, help="grid: first N strengths (smoke)")
    p.add_argument("--n-questions", type=int, default=None, help="eval: first N per qset (smoke)")
    p.add_argument("--n-samples", type=int, default=None, help="override samples per block")
    p.add_argument("--adapter-dir", default=None, help="local adapter instead of the Hub")
    p.add_argument("--alpha", type=float, default=None, help="eval: alpha override (smoke only)")
    p.add_argument("--out-root", default=str(ROOT),
                   help="firstk vector read from, and outputs written under, this root (smoke)")
    a = p.parse_args(argv)
    root = Path(a.out_root)
    if a.mode == "eval" and (a.prompts is not None or a.grid_limit is not None):
        p.error("--prompts and --grid-limit are grid knobs")
    if a.mode == "grid" and (a.n_questions is not None or a.alpha is not None):
        p.error("--n-questions and --alpha are eval knobs")

    vec, vinfo = load_vector(root, a.cell)
    if a.mode == "eval":  # refuse before loading the model
        alpha, alpha_sha = (a.alpha, None) if a.alpha is not None else read_alpha(a.cell)
    model, tok, info = E.load(a.cell, a.adapter_dir)
    vec = vec.to(next(model.parameters()).device)
    overrides = {
        k: v
        for k, v in (
            ("prompts", a.prompts), ("grid_limit", a.grid_limit), ("n_questions", a.n_questions),
            ("n_samples", a.n_samples), ("adapter_dir", a.adapter_dir), ("alpha", a.alpha),
            ("out_root", None if root == ROOT else str(root)),
        )
        if v is not None
    }
    base_meta = {
        **info,
        "exp": C.EXP,
        "cell": a.cell,
        "condition": C.steer_condition(a.cell),
        "steered_model": "untuned (adapter disabled)",
        "layer": C.PRIMARY_LAYER,
        "fmt": C.STEER_FMT,
        "template_sha256": template_sha256(C.STEER_FMT),
        "vector": vinfo,
        "hook": "adds alpha * v to the layer output at every position, prefill and every decode step",
        "overrides": overrides,
    }

    if a.mode == "grid":
        prompts = None if a.prompts is None else [int(x) for x in a.prompts.split(",")]
        grid = None if a.grid_limit is None else range(a.grid_limit)
        n = a.n_samples or C.STEER_SEARCH_SAMPLES
        jobs = [(root / C.steer_grid_path(a.cell), S.grid_blocks(a.cell, prompts, grid, n))]
        temp, max_new = C.STEER_SEARCH_TEMPERATURE, C.STEER_SEARCH_MAX_NEW_TOKENS
        extra = {
            "alpha_grid": list(C.ALPHA_GRID),
            "prompts": list(C.STEER_PROMPTS),
            "seed_rule": f"request_seed(condition, qid, sample_idx, base={C.STEER_SEED})",
        }
    else:
        qs = eval_questions(a.n_questions)
        jobs = [
            (root / C.steer_eval_path(a.cell, qset),
             S.eval_blocks(a.cell, qset, qs[qset], a.n_samples or C.STEER_EVAL[qset], alpha))
            for qset in ("new41", "general")
        ]
        temp, max_new = SAMPLING["temperature"], SAMPLING["max_new_tokens"]
        extra = {
            "alpha": alpha,
            "alpha_file": C.ALPHA_FILE,
            "alpha_sha256": alpha_sha,
            "seed_rule": "request_seed(condition, qid, sample_idx)",
        }

    for path, blocks in jobs:
        t0 = time.time()
        calls = {"prefill": 0, "decode": 0}

        def on_block(b, st, calls=calls, path=path):
            calls["prefill"] += st.prefill_calls
            calls["decode"] += st.decode_calls
            print(f"{path.name}: {b.qid} alpha={b.alpha:.4g} done", flush=True)

        res = S.run_blocks(
            model, tok, a.cell, blocks, path, vec=vec, temperature=temp,
            max_new_tokens=max_new, on_block=on_block,
        )
        side = {
            **base_meta,
            **E._env_meta(),
            **extra,
            "sampling": {"temperature": temp, "top_p": 1.0, "top_k": -1,
                         "max_new_tokens": max_new, "n": len(blocks[0].seeds) if blocks else 0},
            "n_rows": sum(len(b.seeds) for b in blocks),
            "run": res,
            "hook_calls_this_run": calls,
            "wallclock_s_this_run": round(time.time() - t0, 1),
        }
        _meta_path(path).write_text(json.dumps(side, indent=2) + "\n")
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
