"""P46 S1 / S1s (laptop, free given cached embeddings): text-embedding kNN distances.

- **S1** `emb_knn` = 1 - mean of the top K.KNN_K *uncentred* cosines to the ref_dpo vectors
  (P42's `p42.embed.knn_score`, reused as is; plan "Scores").
- **S1s** `emb_knn_safety` = the same against the ref_safety vectors (exploratory).
- **Vectors:** `data/P46/emb/emb.f16.npy` + `pids.txt` (the data unit's text-embedding-3-small
  vectors, P42's layout) first, then P42's cache (`K.P42_EMBED_DIR`; in a worktree where that is
  absent, the main checkout's copy) for the pids P46 reuses from P42 (ref_dpo <- id_ref,
  heldout <- id_heldout, ref_lmsys <- pool). Rows are re-normalised to unit length.
- **Scored rows:** every K.SCORED_SETS prompt in `data/P46/prompts.jsonl`. Any needed vector
  missing (a scored row, or a reference row) -> exit 1 with the list; nothing is written.
- **Output:** `results/analysis/emb_scores.csv` (pid, set, emb_knn, emb_knn_safety) and
  `emb_scores.meta.json` (sources, counts, k).

Usage (from the probe directory): uv run python -m p46.embed [--p42-embed-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from p46 import contracts as K

_P42_DIR = K.REPO_ROOT / "probes" / "P42-ood-coverage-gaps"  # p42 is imported from its probe dir
if str(_P42_DIR) not in sys.path:
    sys.path.insert(0, str(_P42_DIR))

from p42.embed import knn_score, l2n

TAG = "P46 EMBED"


def main_checkout_p42_dir(repo_root: Path = K.REPO_ROOT) -> Path:
    """P42's embed dir in the main checkout when running from `.claude/worktrees/<name>`."""
    r = Path(repo_root)
    if r.parent.name == "worktrees" and r.parent.parent.name == ".claude":
        return r.parents[2] / "probes" / "P42-ood-coverage-gaps" / "results" / "embed"
    return Path(K.P42_EMBED_DIR)


def resolve_p42_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    d = Path(K.P42_EMBED_DIR)
    return d if (d / "emb.f16.npy").is_file() else main_checkout_p42_dir()


def load_store(d: Path) -> tuple[np.ndarray, dict[str, int]]:
    """(f16 matrix, {pid: row}) from an `emb.f16.npy` + `pids.txt` directory (memory-mapped)."""
    d = Path(d)
    emb = np.load(d / "emb.f16.npy", mmap_mode="r")
    pids = [x for x in (d / "pids.txt").read_text().split("\n") if x]
    if len(pids) != emb.shape[0]:
        raise ValueError(f"{d}: {len(pids)} pids for {emb.shape[0]} rows")
    if emb.shape[1] != K.EMBED_DIM:
        raise ValueError(f"{d}: dim {emb.shape[1]} != {K.EMBED_DIM}")
    return emb, {p: i for i, p in enumerate(pids)}


class Vectors:
    """Look up unit vectors by pid across stores, first store first."""

    def __init__(self, stores: list[tuple[str, np.ndarray, dict[str, int]]]):
        self.stores = stores

    def missing(self, pids) -> list[str]:
        return [p for p in pids if not any(p in idx for _, _, idx in self.stores)]

    def get(self, pids) -> tuple[np.ndarray, dict[str, int]]:
        out = np.empty((len(pids), K.EMBED_DIM), dtype=np.float32)
        used: dict[str, int] = {}
        for i, p in enumerate(pids):
            for name, emb, idx in self.stores:
                j = idx.get(p)
                if j is not None:
                    out[i] = emb[j]
                    used[name] = used.get(name, 0) + 1
                    break
            else:
                raise KeyError(p)
        return l2n(out), used


def score(prompts: pd.DataFrame, vec: Vectors, k: int = K.KNN_K) -> tuple[pd.DataFrame, dict]:
    sets = prompts["set"].astype(str)
    ref_dpo = sorted(prompts.loc[sets == K.REF_DPO, "pid"].astype(str))
    ref_saf = sorted(prompts.loc[sets == K.REF_SAFETY, "pid"].astype(str))
    sc = prompts.loc[sets.isin(K.SCORED_SETS), ["pid", "set"]].sort_values("pid")
    if not ref_dpo or not ref_saf:
        raise ValueError(f"no reference rows: ref_dpo {len(ref_dpo)}, ref_safety {len(ref_saf)}")
    miss = vec.missing([*sc["pid"].astype(str), *ref_dpo, *ref_saf])
    if miss:
        raise LookupError(miss)
    q, used_q = vec.get(sc["pid"].astype(str).tolist())
    r1, used_r1 = vec.get(ref_dpo)
    out = sc.copy()
    out["emb_knn"] = knn_score(q, r1, k)
    del r1
    r2, used_r2 = vec.get(ref_saf)
    out["emb_knn_safety"] = knn_score(q, r2, k)
    meta = {"knn_k": k, "n_scored": len(out), "n_ref_dpo": len(ref_dpo),
            "n_ref_safety": len(ref_saf), "cosine": "uncentred (unit vectors, raw dot)",
            "vectors_from": {"scored": used_q, "ref_dpo": used_r1, "ref_safety": used_r2},
            "n_by_set": out["set"].value_counts().to_dict()}
    return out.reset_index(drop=True), meta


def run(args) -> int:
    prompts = pd.read_json(args.prompts, lines=True)
    stores = []
    for name, d in (("p46", Path(args.emb_dir)), ("p42", resolve_p42_dir(args.p42_embed_dir))):
        if (d / "emb.f16.npy").is_file():
            emb, idx = load_store(d)
            stores.append((name, emb, idx))
            print(f"{TAG}: {name} store {d} ({len(idx)} vectors)")
        else:
            print(f"{TAG}: WARNING no vectors at {d}")
    try:
        out, meta = score(prompts, Vectors(stores), args.k)
    except LookupError as e:
        miss = e.args[0]
        print(f"{TAG} FAILED: {len(miss)} needed embeddings missing, e.g. {miss[:20]}",
              file=sys.stderr)
        return 1
    od = Path(args.out_dir)
    od.mkdir(parents=True, exist_ok=True)
    out.to_csv(od / "emb_scores.csv", index=False)
    meta["stores"] = {n: str(d) for n, d in (("p46", args.emb_dir),
                                            ("p42", resolve_p42_dir(args.p42_embed_dir)))}
    (od / "emb_scores.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{TAG} DONE: {json.dumps(meta)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m p46.embed", description=__doc__.split("\n")[0])
    ap.add_argument("--prompts", default=str(K.PROMPTS_PATH))
    ap.add_argument("--emb-dir", default=str(K.EMB_DIR))
    ap.add_argument("--p42-embed-dir", default=None)
    ap.add_argument("--out-dir", default=str(K.ANALYSIS_DIR))
    ap.add_argument("--k", type=int, default=K.KNN_K)
    return ap


def main(argv=None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
