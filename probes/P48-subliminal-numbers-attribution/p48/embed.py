"""P48 embedding detectors: the "knows nothing" text baselines, on all-mpnet-base-v2 embeddings.

Ported from `probes/P43-subliminal-attribution/p43/embed.py` (Phantom Transfer App. K: activation
clustering, spectral signatures, isolation forest, plus a length baseline), unchanged in
substance; only the row loader (`p48.judge.load_mix`) and this docstring differ.

**Expected near chance here.** Every row that survived Cloud et al.'s filter is digits and
separators only, so the two sources' rows are drawn from near-identical text distributions and an
embedding model has little to separate. plan.md predicts the judge, MPNet and length detectors all
at AUROC <= 0.55 (p 0.85). They are in the probe as the floor the trait-knowing scorers must beat,
not as candidate defences.

Scores (higher = more suspicious), one orientation each, over all rows in idx order. The analysis
reports AUROC and |AUROC - 0.5| because the spectral / clustering sign is arbitrary.
- spectral: centre the embeddings, top right-singular vector v, score = (x . v)^2.
- iforest: sklearn IsolationForest(n_estimators=200, random_state=0), score = -score_samples.
- ac: PCA to 10 dims, KMeans(k=2, n_init=10, random_state=0). Rows in the smaller cluster score
  1 + d/(1+d) where d is the distance to the LARGER cluster's centroid (in (1, 2), so the
  tiebreak ranks the farthest first); larger-cluster rows score d/(1+d) (in (0, 1)).
- length_chars / length_tokens: response length in characters / whitespace tokens.

Variants: `pr` embeds "prompt\\nresponse", `r` embeds the response only. Embeddings are cached at
`<outdir>/mpnet_<variant>_<mix>.npy`. Runs on the laptop (MPS), overlapped with the pod's work;
sentence-transformers is not a project dep:
  uv run --with sentence-transformers==5.1.0 python -m p48.embed \
      --mix data/mix/mix100.jsonl --outdir results/scores/ [--variants pr r] [--batch-size 32]
Writes `<scorer>_<variant>.npy` per scorer and `embed.meta.json`. Single process, modest batches.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

MPNET = "sentence-transformers/all-mpnet-base-v2"
MPNET_REV = "e8c3b32edf5434bc2275fc9bab85f82640a19130"
SEED = 0
VARIANTS = ("pr", "r")


def texts_for(rows: list[dict], variant: str) -> list[str]:
    """`pr`: prompt + newline + response; `r`: response only. Only `messages` is read."""
    if variant not in VARIANTS:
        raise ValueError(f"variant {variant!r} not in {VARIANTS}")
    out = []
    for r in rows:
        m = r["messages"]
        out.append(f"{m[0]['content']}\n{m[-1]['content']}" if variant == "pr"
                   else m[-1]["content"])
    return out


def spectral_scores(x: np.ndarray) -> np.ndarray:
    xc = x - x.mean(axis=0, keepdims=True)
    _, _, vt = np.linalg.svd(xc, full_matrices=False)
    return (xc @ vt[0]) ** 2


def iforest_scores(x: np.ndarray) -> np.ndarray:
    from sklearn.ensemble import IsolationForest

    f = IsolationForest(n_estimators=200, random_state=SEED).fit(x)
    return -f.score_samples(x)


def ac_scores(x: np.ndarray) -> np.ndarray:
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA

    z = PCA(n_components=min(10, x.shape[0], x.shape[1]), random_state=SEED).fit_transform(x)
    km = KMeans(n_clusters=2, n_init=10, random_state=SEED).fit(z)
    counts = np.bincount(km.labels_, minlength=2)
    small = int(np.argmin(counts))
    large = 1 - small
    d = np.linalg.norm(z - km.cluster_centers_[large], axis=1)
    return (km.labels_ == small).astype(float) + d / (1.0 + d)


def length_scores(rows: list[dict]) -> dict[str, np.ndarray]:
    resp = [r["messages"][-1]["content"] for r in rows]
    return {"length_chars": np.array([len(s) for s in resp], dtype=float),
            "length_tokens": np.array([len(s.split()) for s in resp], dtype=float)}


def score_all(rows: list[dict], embed_fn: Callable[[list[str]], np.ndarray],
              variants=VARIANTS, cache: Callable[[str], np.ndarray | None] | None = None,
              store: Callable[[str, np.ndarray], None] | None = None) -> dict[str, np.ndarray]:
    """{scorer_variant: scores aligned to row order}. Labels are never read."""
    for r in rows:
        assert not ({"label", "identical"} & set(r)), "scorers must not see label/identical"
    out = dict(length_scores(rows))
    for v in variants:
        x = cache(v) if cache else None
        if x is None:
            x = np.asarray(embed_fn(texts_for(rows, v)), dtype=np.float64)
            if store:
                store(v, x)
        x = np.asarray(x, dtype=np.float64)
        out[f"spectral_{v}"] = spectral_scores(x)
        out[f"iforest_{v}"] = iforest_scores(x)
        out[f"ac_{v}"] = ac_scores(x)
    return out


def st_embed_fn(batch_size: int = 32):
    import torch
    from sentence_transformers import SentenceTransformer

    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    model = SentenceTransformer(MPNET, revision=MPNET_REV, device=dev)

    def fn(texts: list[str]) -> np.ndarray:
        return model.encode(texts, batch_size=batch_size, convert_to_numpy=True,
                            show_progress_bar=False)

    fn.device = dev  # type: ignore[attr-defined]
    return fn


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mix", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--variants", nargs="+", default=list(VARIANTS), choices=VARIANTS)
    ap.add_argument("--batch-size", type=int, default=32)
    a = ap.parse_args(argv)

    from p48.judge import load_mix  # strips label/identical

    rows = load_mix(a.mix)
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = Path(a.mix).stem
    t0 = time.time()
    timings: dict[str, float] = {}
    fn_holder: list = []

    def embed_fn(texts):
        if not fn_holder:
            fn_holder.append(st_embed_fn(a.batch_size))
        return fn_holder[0](texts)

    def path(v):
        return outdir / f"mpnet_{v}_{stem}.npy"

    def cache(v):
        return np.load(path(v)) if path(v).exists() and len(np.load(path(v))) == len(rows) else None

    def store(v, x):
        np.save(path(v), x)
        timings[f"embed_{v}_s"] = round(time.time() - t0, 1)

    scores = score_all(rows, embed_fn, a.variants, cache, store)
    for name, s in scores.items():
        np.save(outdir / f"{name}.npy", s)
    import sentence_transformers as st
    meta = {"model": MPNET, "revision": MPNET_REV, "sentence_transformers": st.__version__,
            "mix": a.mix, "n_rows": len(rows), "variants": a.variants, "seed": SEED,
            "device": getattr(fn_holder[0], "device", "cached") if fn_holder else "cached",
            "batch_size": a.batch_size, "timings_s": timings,
            "total_s": round(time.time() - t0, 1), "scorers": sorted(scores)}
    (outdir / "embed.meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(f"[p48.embed] {len(rows)} rows, {len(scores)} scorers, {meta['total_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
