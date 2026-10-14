"""P45 T1 (laptop): judge-free density gaps between Dolci DPO (ID) and the LMSYS pool in the
model's own activation space (plan.md "Setup: T1", "Measure: T1").

Spaces (K.SPACES), each L2-normalised, rows = id_ref ∪ pool for the regions:
- A: P42's PCA-256 of DPO L24 prompt-token means, fit on id_ref (P42 results/pca/dpo_p_L24.npy)
- B: the same at L16 (dpo_p_L16.npy)
- C: P42's text-embedding-3-small vectors (results/embed/emb.f16.npy) reduced to PCA-256 fit on
     id_ref (centred on the id_ref mean, float64 eigh, as p42.scoring.pca_fit), cached under
     results/t1/cache/
- D: P45's union PCA (results/pca_union/dpo_p_L24.npy, fit on id_ref ∪ pool, from the T2 pod)

**Length weights:** an id_ref prompt in pool length-decile b (P42's edges: `p42.analyze
.length_edges` of the pool's n_tokens) gets w = n_pool,b / n_id,b, a pool prompt 1, so the
weighted ID mass equals the pool mass in every decile and a region's weighted ID share
s = I_w / (I_w + P) is 0.5 under no gap. **Regions:** weighted k-means (sklearn KMeans,
sample_weight, K.SEED_T1, n_init 4, lloyd) for k in K.T1_K and K.T1_K_SENS. **Point score:**
the weighted ID share among each pool prompt's K.T1_KNN nearest cosine neighbours in the union
(itself excluded), by blocked search. **Kish n** = (Σw)² / Σw² over a region's ID rows.

Subcommands (from the probe dir: `uv run python -m p45.t1 <cmd>`; `dedup` and `count` read P42's
parquet caches and need `uv run --with pyarrow --with xxhash python -m p45.t1 ...`):
  regions --space S [--k K ...]   regions, stats, point scores, planted sanity check
  null --space S [--k K]          within-decile label shuffles and ID bootstraps (descriptive)
  dedup [--space S ...]           pool textual (MinHash) and activation (cosine) groups
  top --space S [--k K]           candidates, top 20, collapse robustness, k-sensitivity
  samples --space S [--k K]       seeded reading samples for the top regions
  labels-template --space S       skeleton labels file for the agent to fill and commit
  validate-labels                 regex rule and topic merge on the committed labels
  count                           regex counts in Dolci DPO/SFT and the LMSYS candidates
  embed-dolci [--dry-run]         PAID (~$1.2): Dolci DPO first-turn embeddings (emx.spend, P45)
  semantic                        nearest Dolci DPO prompts to each topic's pool members
  classify                        categories from the reading verdicts; the primary number
  inputs                          sha256 of every input -> results/inputs.json ("t1" key)

Laptop rules: at most 4 BLAS/OpenMP threads and 4 processes; no matrix larger than a block of
SIM_CHUNK x n_union is materialised.
"""

from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
           "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
    try:
        if int(os.environ.get(_v, "99")) > 4:
            os.environ[_v] = "4"
    except ValueError:
        os.environ[_v] = "4"

import argparse
import importlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from p45 import contracts as K  # first: puts p42 on sys.path

P42A = importlib.import_module("p42.analyze")
P42K = importlib.import_module("p42.contracts")

N_THREADS = 4
N_PROC = 4
SIM_CHUNK = 512
TEXT_CHARS = 600
MAIN_ROOT = K.P42_RESULTS_MAIN.parents[2]
CACHE = K.T1_DIR / "cache"
LABELS = ("topic", "templated traffic", "language or script", "fragment, format or length")
DOLCI = ("dolci_dpo", "dolci_sft")


# --- paths and loading ----------------------------------------------------------------------------


def p42_data_dir() -> Path:
    """data/P42 of this checkout if present (e.g. a symlink), else the main checkout's."""
    for d in (K.REPO_ROOT / "data" / "P42", MAIN_ROOT / "data" / "P42"):
        if (d / "prompts.jsonl").is_file():
            return d
    raise FileNotFoundError("data/P42/prompts.jsonl not found in this checkout or main")


def p42_results_dir() -> Path:
    for d in (K.P42_DIR / "results", K.P42_RESULTS_MAIN):
        if (d / "pca" / "dpo_p_L24.npy").is_file():
            return d
    raise FileNotFoundError("P42 results/pca not found in this checkout or main")


def p42_cache_dir() -> Path:
    return p42_data_dir() / "raw" / "cache"


def space_files(space: str) -> tuple[Path, Path]:
    r = p42_results_dir()
    if space == "A":
        return r / "pca" / "dpo_p_L24.npy", r / "pca" / "pids.txt"
    if space == "B":
        return r / "pca" / "dpo_p_L16.npy", r / "pca" / "pids.txt"
    if space == "C":
        return r / "embed" / "emb.f16.npy", r / "embed" / "pids.txt"
    if space == "D":
        return K.PCA_UNION_DIR / "dpo_p_L24.npy", K.PCA_UNION_DIR / "pids.txt"
    raise ValueError(space)


_PROMPTS: pd.DataFrame | None = None


def prompts() -> pd.DataFrame:
    """P42 prompts.jsonl (+ P45's planted_law2 if built), indexed by pid."""
    global _PROMPTS
    if _PROMPTS is None:
        df = P42A.read_jsonl(p42_data_dir() / "prompts.jsonl")
        law2 = _law2_path()
        if law2 is not None:
            df = pd.concat([df, P42A.read_jsonl(law2)], ignore_index=True)
        _PROMPTS = df.set_index("pid", drop=False)
    return _PROMPTS


def _law2_path() -> Path | None:
    for p in (K.LAW2_PATH, MAIN_ROOT / "data" / "P45" / K.LAW2_PATH.name):
        if p.is_file():
            return p
    return None


def l2n(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-12)


def pca_fit_ref(X_ref: np.ndarray, dim: int = 256) -> tuple[np.ndarray, np.ndarray]:
    """Mean and top-dim eigenvectors (columns) of the reference covariance, float64 eigh."""
    X = np.asarray(X_ref, dtype=np.float64)
    mu = X.mean(axis=0)
    C = np.zeros((X.shape[1], X.shape[1]))
    for s in range(0, len(X), 8192):
        Z = X[s:s + 8192] - mu
        C += Z.T @ Z
    C /= max(len(X) - 1, 1)
    ev, V = np.linalg.eigh(C)
    o = np.argsort(ev)[::-1][:dim]
    return mu, V[:, o]


def load_space(space: str) -> tuple[np.ndarray, list[str], dict]:
    """(L2-normalised float32 rows, pids, info) for every row the space holds."""
    f, pf = space_files(space)
    if not f.is_file():
        raise FileNotFoundError(f"space {space}: {f} missing" +
                                (" (D comes from the T2 pod)" if space == "D" else ""))
    pids = pf.read_text().split()
    info = {"file": str(f), "pids": str(pf)}
    if space != "C":
        X = np.load(f).astype(np.float32)
    else:
        cp = CACHE / "C_pca256.f16.npy"
        if cp.is_file():
            X = np.load(cp).astype(np.float32)
            info["pca"] = "cached " + str(cp)
        else:
            E = np.load(f).astype(np.float32)
            is_ref = np.array([p.startswith(P42K.ID_REF + "-") for p in pids])
            mu, V = pca_fit_ref(E[is_ref], 256)
            X = ((E.astype(np.float64) - mu) @ V).astype(np.float32)
            CACHE.mkdir(parents=True, exist_ok=True)
            np.save(cp, X.astype(np.float16))
            info["pca"] = f"fit on {int(is_ref.sum())} id_ref rows -> {cp}"
    if X.shape[0] != len(pids):
        raise ValueError(f"space {space}: {X.shape[0]} rows != {len(pids)} pids")
    if not np.isfinite(X).all():
        raise ValueError(f"space {space}: non-finite values")
    return l2n(X), pids, info


class Union:
    """The union id_ref ∪ pool in one space, ordered as the space's rows (pids sorted within)."""

    def __init__(self, space: str):
        X, pids, info = load_space(space)
        pr = prompts()
        sets = pr["set"].reindex(pids).to_numpy()
        if pd.isna(sets).any():
            raise ValueError(f"space {space}: {int(pd.isna(sets).sum())} pids not in prompts")
        self.space, self.info, self.all_X, self.all_pids, self.all_sets = space, info, X, pids, sets
        u = np.flatnonzero(np.isin(sets, (P42K.ID_REF, P42K.POOL)))
        u = u[np.argsort(np.array(pids, dtype=object)[u], kind="stable")]
        self.rows = u
        self.X = X[u]
        self.pids = [pids[i] for i in u]
        self.is_id = sets[u] == P42K.ID_REF
        self.ntok = pr["n_tokens"].reindex(self.pids).to_numpy(dtype=float)
        pool_ntok = self.ntok[~self.is_id]
        self.edges = P42A.length_edges(pool_ntok)
        self.bins = P42A.length_bin(self.ntok, self.edges)
        self.w = length_weights(self.bins, self.is_id)
        self.pool_pids = [p for p, i in zip(self.pids, self.is_id) if not i]

    def other(self, set_name: str) -> tuple[np.ndarray, list[str]]:
        idx = np.flatnonzero(self.all_sets == set_name)
        return self.all_X[idx], [self.all_pids[i] for i in idx]


# --- core computations (pure; unit-tested) ------------------------------------------------------------


def length_weights(bins: np.ndarray, is_id: np.ndarray, keep: np.ndarray | None = None,
                   n_bins: int = P42K.N_LEN_BINS) -> np.ndarray:
    """Per row: ID rows w = n_pool,b / n_id,b (kept pool rows only), kept pool rows 1, dropped
    pool rows 0. A bin with no ID rows contributes no ID weight."""
    keep = np.ones(len(bins), bool) if keep is None else np.asarray(keep, bool)
    is_id = np.asarray(is_id, bool)
    n_pool = np.bincount(bins[~is_id & keep], minlength=n_bins).astype(float)
    n_id = np.bincount(bins[is_id], minlength=n_bins).astype(float)
    wb = np.divide(n_pool, n_id, out=np.zeros(n_bins), where=n_id > 0)
    w = np.where(is_id, wb[bins], 1.0)
    w[~is_id & ~keep] = 0.0
    return w


def kish_neff(w: np.ndarray) -> float:
    w = np.asarray(w, float)
    s2 = float((w ** 2).sum())
    return float(w.sum() ** 2 / s2) if s2 > 0 else 0.0


def region_stats(labels: np.ndarray, is_id: np.ndarray, w: np.ndarray, k: int,
                 keep: np.ndarray | None = None) -> pd.DataFrame:
    """Per region: P (kept pool rows), raw ID count, weighted ID mass I_w, Kish n of the ID
    weights, share s = I_w / (I_w + P) (NaN for an empty region)."""
    is_id = np.asarray(is_id, bool)
    keep = np.ones(len(labels), bool) if keep is None else np.asarray(keep, bool)
    pool = ~is_id & keep
    P = np.bincount(labels[pool], minlength=k).astype(float)
    n_id = np.bincount(labels[is_id], minlength=k)
    Iw = np.bincount(labels[is_id], weights=w[is_id], minlength=k)
    Iw2 = np.bincount(labels[is_id], weights=w[is_id] ** 2, minlength=k)
    neff = np.divide(Iw ** 2, Iw2, out=np.zeros(k), where=Iw2 > 0)
    tot = Iw + P
    s = np.divide(Iw, tot, out=np.full(k, np.nan), where=tot > 0)
    return pd.DataFrame({"region": np.arange(k), "P": P.astype(int), "n_id": n_id,
                         "id_mass": Iw, "kish_n": neff, "s": s})


def candidates(st: pd.DataFrame, share_max: float = K.T1_SHARE_MAX,
               min_pool: int = K.T1_MIN_POOL) -> pd.DataFrame:
    return st[(st["s"] <= share_max) & (st["P"] >= min_pool)]


def top_regions(st: pd.DataFrame, n: int = K.T1_TOP) -> list[int]:
    """The n candidates with the lowest s, ties -> larger P, then lower region id."""
    c = candidates(st)
    c = c.sort_values(["s", "P", "region"], ascending=[True, False, True], kind="stable")
    return [int(r) for r in c["region"].head(n)]


def knn_id_share(Q: np.ndarray, U: np.ndarray, u_is_id: np.ndarray, u_w: np.ndarray, k: int,
                 self_idx: np.ndarray | None = None, chunk: int = SIM_CHUNK) -> np.ndarray:
    """Per query (unit rows): weighted ID share among its k nearest cosine neighbours in U, an
    ID neighbour counting its length weight and a pool neighbour 1; self_idx[i] (a U row, or -1)
    is excluded. Blocked: at most chunk x len(U) similarities at once."""
    out = np.empty(len(Q))
    idw = np.where(u_is_id, u_w, 0.0)
    pw = np.where(u_is_id, 0.0, 1.0)
    kk = min(k, len(U) - (1 if self_idx is not None else 0))
    for s in range(0, len(Q), chunk):
        sim = Q[s:s + chunk] @ U.T
        if self_idx is not None:
            si = self_idx[s:s + chunk]
            ok = si >= 0
            sim[np.flatnonzero(ok), si[ok]] = -np.inf
        idx = np.argpartition(-sim, kk - 1, axis=1)[:, :kk]
        a, b = idw[idx].sum(1), pw[idx].sum(1)
        out[s:s + chunk] = np.divide(a, a + b, out=np.full(len(a), np.nan), where=(a + b) > 0)
    return out


def components(n: int, edges_i: np.ndarray, edges_j: np.ndarray) -> np.ndarray:
    """Union-find groups (connected components) over n items; returns each item's group
    representative = the lowest item index in its group."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if n == 0:
        return np.zeros(0, int)
    ei, ej = np.asarray(edges_i, int), np.asarray(edges_j, int)
    g = coo_matrix((np.ones(len(ei), np.int8), (ei, ej)), shape=(n, n))
    _, lab = connected_components(g, directed=False)
    rep = np.full(lab.max() + 1, n, int)
    np.minimum.at(rep, lab, np.arange(n))
    return rep[lab]


def minhash_edges(sig: np.ndarray, thresh: float = P42K.MINHASH_JACCARD,
                  block: int = 256) -> tuple[np.ndarray, np.ndarray]:
    """All pairs (i < j) sharing at least one LSH band (p42.data.band_keys) whose estimated
    Jaccard is >= thresh. Buckets are compared in full (blocked), with no cap."""
    from p42.data import N_BANDS, band_keys

    keys = band_keys(sig)
    ei, ej = [], []
    for band in range(N_BANDS):
        order = np.argsort(keys[:, band], kind="stable")
        sk = keys[order, band]
        brk = np.flatnonzero(np.diff(sk) != 0) + 1
        starts, ends = np.r_[0, brk], np.r_[brk, len(sk)]
        for g in np.flatnonzero(ends - starts >= 2):
            mem = np.sort(order[starts[g]:ends[g]])
            S = sig[mem]
            for a in range(0, len(mem), block):
                eq = (S[a:a + block, None, :] == S[None, :, :]).mean(-1) >= thresh
                ii, jj = np.nonzero(eq)
                ii = ii + a
                m = jj > ii
                ei.append(mem[ii[m]])
                ej.append(mem[jj[m]])
    if not ei:
        return np.zeros(0, int), np.zeros(0, int)
    e = np.unique(np.stack([np.concatenate(ei), np.concatenate(ej)], 1), axis=0)
    return e[:, 0], e[:, 1]


def cosine_edges(X: np.ndarray, thresh: float, chunk: int = 1024) -> tuple[np.ndarray, np.ndarray]:
    """Pairs (i < j) of unit rows with cosine >= thresh, blocked."""
    ei, ej = [], []
    for s in range(0, len(X), chunk):
        sim = X[s:s + chunk] @ X.T
        ii, jj = np.nonzero(sim >= thresh)
        ii = ii + s
        m = jj > ii
        ei.append(ii[m])
        ej.append(jj[m])
    return (np.concatenate(ei), np.concatenate(ej)) if ei else (np.zeros(0, int),) * 2


def collapse_stats(labels, is_id, bins, k, keep_pool) -> pd.DataFrame:
    """Region stats after keeping only the pool rows in keep_pool (regions fixed; ID rows kept;
    length weights recomputed on the kept pool)."""
    keep = np.asarray(keep_pool, bool) | np.asarray(is_id, bool)
    w = length_weights(bins, is_id, keep)
    return region_stats(labels, is_id, w, k, keep)


def validate_regex(regex: str, texts: list[str], min_hits: int = K.T1_REGEX_MIN_HITS) -> dict:
    try:
        rx = re.compile(regex, re.IGNORECASE)
    except re.error as e:
        return {"ok": False, "hits": 0, "error": str(e)}
    hits = sum(bool(rx.search(t)) for t in texts)
    return {"ok": hits >= min_hits, "hits": hits, "n": len(texts)}


def topic_key(topic: str) -> str:
    return " ".join(str(topic).split()).casefold()


def merge_topics(entries: list[dict]) -> dict[str, dict]:
    """entries: {space, k, region, topic, definition, regex}. Regions whose topic strings are
    identical (whitespace- and case-normalised) become one distinct topic; differing regexes are
    OR-ed, definitions kept in order."""
    out: dict[str, dict] = {}
    for e in entries:
        key = topic_key(e["topic"])
        t = out.setdefault(key, {"topic": e["topic"].strip(), "definitions": [], "regexes": [],
                                 "regions": []})
        if e.get("definition") and e["definition"] not in t["definitions"]:
            t["definitions"].append(e["definition"])
        if e["regex"] not in t["regexes"]:
            t["regexes"].append(e["regex"])
        t["regions"].append({"space": e["space"], "k": e["k"], "region": int(e["region"])})
    for t in out.values():
        t["regex"] = t["regexes"][0] if len(t["regexes"]) == 1 else \
            "|".join(f"(?:{r})" for r in t["regexes"])
    return out


def category(on_topic: int, dolci_rate: float, lmsys_rate: float,
             ratio: float = K.T1_RATE_RATIO) -> str:
    """Plan's three categories: zero validated examples found / sparse coverage (Dolci rate <=
    ratio x LMSYS rate) / frequency difference (>)."""
    if on_topic == 0:
        return "zero_validated_found"
    return "sparse_coverage" if dolci_rate <= ratio * lmsys_rate else "frequency_difference"


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if (a or b) else float("nan")


def primary_count(topics: dict[str, dict], cats: dict[str, str], robust: dict[str, set],
                  members: dict[tuple, set], n_pool: int, spaces=("A", "D")) -> dict:
    """Distinct topics with zero validated examples among the robust top gaps of `spaces`
    (deduplicated: a topic counts once), and the pool share their members hold. robust[space] =
    set of robust region ids; members[(space, region)] = set of pool pids."""
    hit, pids, per = [], set(), {s: [] for s in spaces}
    for key, t in topics.items():
        regs = [(r["space"], r["region"]) for r in t["regions"]
                if r["space"] in spaces and r["region"] in robust.get(r["space"], set())]
        if not regs or cats.get(key) != "zero_validated_found":
            continue
        hit.append(key)
        for s, r in regs:
            per[s].append(key)
            pids |= members.get((s, r), set())
    return {"n_topics": len(hit), "topics": sorted(hit),
            "per_space": {s: len(set(v)) for s, v in per.items()},
            "pool_members": len(pids), "pool_share": len(pids) / n_pool if n_pool else np.nan}


# --- io helpers -------------------------------------------------------------------------------------


def write_json(path: Path, obj) -> None:
    P42A.write_json(path, obj)


def regions_path(space: str, k: int) -> Path:
    return CACHE / f"regions_{space}_k{k}.npz"


def load_regions(space: str, k: int, U: Union) -> tuple[np.ndarray, np.ndarray]:
    p = regions_path(space, k)
    if not p.is_file():
        raise FileNotFoundError(f"{p}: run `regions --space {space} --k {k}` first")
    z = np.load(p, allow_pickle=False)
    if list(z["pids"]) != U.pids:
        raise RuntimeError(f"{p}: union pids changed; rerun regions")
    return z["labels"], z["centers"]


def _threads():
    from threadpoolctl import threadpool_limits

    return threadpool_limits(N_THREADS)


def _space_code(space: str) -> int:
    return K.SPACES.index(space)


def _text(pid: str) -> str:
    return str(prompts().at[pid, "text"])


def git_committed_clean(path: Path) -> tuple[bool, str]:
    rel = str(Path(path).resolve().relative_to(K.REPO_ROOT.resolve()))
    log = subprocess.run(["git", "-C", str(K.REPO_ROOT), "log", "-1", "--format=%H", "--", rel],
                         capture_output=True, text=True, check=False)
    st = subprocess.run(["git", "-C", str(K.REPO_ROOT), "status", "--porcelain", "--", rel],
                        capture_output=True, text=True, check=False)
    if not log.stdout.strip():
        return False, "never committed"
    if st.stdout.strip():
        return False, "uncommitted changes"
    return True, log.stdout.strip()


# --- subcommands -------------------------------------------------------------------------------------


def cmd_regions(space: str, ks: list[int]) -> dict:
    from sklearn.cluster import KMeans

    t0 = time.time()
    U = Union(space)
    pr = prompts()
    out_all = {}
    CACHE.mkdir(parents=True, exist_ok=True)
    pool_mask = ~U.is_id
    eng = pr["english"].reindex(U.pids).astype(bool).to_numpy()
    modf = pr["mod_flagged"].reindex(U.pids).to_numpy()
    for k in ks:
        t1 = time.time()
        with _threads():
            km = KMeans(n_clusters=k, n_init=4, random_state=K.SEED_T1, algorithm="lloyd")
            km.fit(U.X, sample_weight=U.w)
        lab = km.labels_.astype(np.int32)
        cen = km.cluster_centers_.astype(np.float32)
        np.savez(regions_path(space, k), labels=lab, centers=cen, pids=np.array(U.pids))
        st = region_stats(lab, U.is_id, U.w, k)
        prof = []
        for r in range(k):
            pm = pool_mask & (lab == r)
            im = U.is_id & (lab == r)
            nt = U.ntok[pm]
            mf = modf[pm]
            mf = np.array([bool(x) for x in mf if x is not None and not pd.isna(x)])
            prof.append({
                "pool_ntok_q25_50_75": (np.percentile(nt, [25, 50, 75]).tolist()
                                        if len(nt) else None),
                "pool_per_decile": np.bincount(U.bins[pm], minlength=P42K.N_LEN_BINS).tolist(),
                "english_pool": float(eng[pm].mean()) if pm.any() else None,
                "english_id": float(eng[im].mean()) if im.any() else None,
                "mod_flag_pool": float(mf.mean()) if len(mf) else None})
        st = pd.concat([st, pd.DataFrame(prof)], axis=1)
        rec = {"space": space, "k": k, "n_union": len(U.pids), "n_pool": int(pool_mask.sum()),
               "n_id": int(U.is_id.sum()), "kmeans": {
                   "algorithm": "sklearn KMeans lloyd, sample_weight = length weights",
                   "n_init": 4, "seed": K.SEED_T1, "n_iter": int(km.n_iter_),
                   "inertia": float(km.inertia_), "wall_s": round(time.time() - t1, 1)},
               "n_candidates": len(candidates(st)),
               "regions": st.to_dict("records")}
        write_json(K.T1_DIR / f"regions_{space}_k{k}.json", rec)
        if k == K.T1_K:
            pd.DataFrame({"pid": U.pids, "region": lab,
                          "set": np.where(U.is_id, P42K.ID_REF, P42K.POOL)}).to_csv(
                K.T1_DIR / f"members_{space}_k{k}.csv", index=False)
        out_all[k] = {"n_candidates": rec["n_candidates"], "n_iter": int(km.n_iter_),
                      "wall_s": rec["kmeans"]["wall_s"]}
        print(f"[regions {space} k={k}] {rec['n_candidates']} candidates, "
              f"{rec['kmeans']['wall_s']} s", flush=True)
    # point score and planted sanity (independent of k)
    t2 = time.time()
    with _threads():
        self_idx = np.flatnonzero(pool_mask)
        ps = knn_id_share(U.X[pool_mask], U.X, U.is_id, U.w, K.T1_KNN, self_idx=self_idx)
        sanity = {"pool": {"n": len(ps), "mean_knn_id_share": float(np.nanmean(ps))}}
        for s in (P42K.ID_HELDOUT, *P42K.PLANTED, K.PLANTED_LAW2):
            Xq, qp = U.other(s)
            if not len(qp):
                sanity[s] = {"n": 0, "note": "not in this space"}
                continue
            q = knn_id_share(Xq, U.X, U.is_id, U.w, K.T1_KNN)
            # lower ID share = more OOD: AUROC(set more OOD than pool)
            sanity[s] = {"n": len(qp), "mean_knn_id_share": float(np.nanmean(q)),
                         "auroc_more_ood_than_pool": P42A.auroc(-q, -ps)}
    pd.DataFrame({"pid": [p for p, i in zip(U.pids, U.is_id) if not i],
                  "knn_id_share": ps}).to_csv(K.T1_DIR / f"point_{space}.csv", index=False,
                                              float_format="%.5f")
    summ = {"space": space, "space_info": U.info, "len_edges": U.edges.tolist(),
            "id_per_decile": np.bincount(U.bins[U.is_id], minlength=P42K.N_LEN_BINS).tolist(),
            "pool_per_decile": np.bincount(U.bins[~U.is_id], minlength=P42K.N_LEN_BINS).tolist(),
            "id_weight_per_decile": [float(U.w[U.is_id & (U.bins == b)][0])
                                     if (U.is_id & (U.bins == b)).any() else None
                                     for b in range(P42K.N_LEN_BINS)],
            "knn": K.T1_KNN, "planted_sanity": sanity, "ks": out_all,
            "point_wall_s": round(time.time() - t2, 1), "wall_s": round(time.time() - t0, 1)}
    write_json(K.T1_DIR / f"regions_{space}.json", summ)
    print(json.dumps(sanity, indent=1), flush=True)
    return summ


def cmd_null(space: str, k: int) -> dict:
    t0 = time.time()
    U = Union(space)
    lab, _ = load_regions(space, k, U)
    obs = region_stats(lab, U.is_id, U.w, k)
    n_obs = len(candidates(obs))
    rng = np.random.default_rng([K.SEED_T1, 1, k, _space_code(space)])
    bins_idx = [np.flatnonzero(U.bins == b) for b in range(P42K.N_LEN_BINS)]
    mins, p1s, ncand = [], [], []
    for _ in range(K.T1_N_PERM):
        is_id = U.is_id.copy()
        for ix in bins_idx:
            is_id[ix] = U.is_id[ix][rng.permutation(len(ix))]
        w = length_weights(U.bins, is_id)
        st = region_stats(lab, is_id, w, k)
        s = st["s"].to_numpy()
        s = s[np.isfinite(s)]
        mins.append(float(s.min()))
        p1s.append(float(np.percentile(s, 1)))
        ncand.append(len(candidates(st)))
    # ID bootstrap: resample ID rows with replacement within each length decile (weights fixed)
    brng = np.random.default_rng([K.SEED_T1, 2, k, _space_code(space)])
    id_rows = np.flatnonzero(U.is_id)
    id_bins = U.bins[id_rows]
    P = np.bincount(lab[~U.is_id], minlength=k).astype(float)
    hits = np.zeros(k)
    for _ in range(K.T1_N_BOOT):
        cnt = np.zeros(len(id_rows))
        for b in range(P42K.N_LEN_BINS):
            ix = np.flatnonzero(id_bins == b)
            if len(ix):
                cnt[ix] = np.bincount(brng.integers(0, len(ix), len(ix)), minlength=len(ix))
        Iw = np.bincount(lab[id_rows], weights=U.w[id_rows] * cnt, minlength=k)
        s = np.divide(Iw, Iw + P, out=np.full(k, np.nan), where=(Iw + P) > 0)
        hits += s <= K.T1_SHARE_MAX
    p_gap = hits / K.T1_N_BOOT

    def q(x):
        return {f"p{p}": float(np.percentile(x, p)) for p in (5, 50, 95)} | {
            "mean": float(np.mean(x))}

    out = {"space": space, "k": k, "note": "descriptive references, not eligibility gates "
           "(plan r2: region geometry depends on the labels)",
           "perm": {"n": K.T1_N_PERM, "scheme": "within-decile shuffle of id/pool labels, "
                    "regions fixed, weights recomputed", "min_share": q(mins),
                    "p1_share": q(p1s), "n_candidates": q(ncand),
                    "frac_perm_n_candidates_ge_observed": float(np.mean(np.array(ncand) >= n_obs))},
           "observed": {"min_share": float(np.nanmin(obs["s"])),
                        "p1_share": float(np.nanpercentile(obs["s"], 1)),
                        "n_candidates": n_obs},
           "boot": {"n": K.T1_N_BOOT, "scheme": "ID rows resampled with replacement within each "
                    "length decile, pool fixed", "p_s_le_max": p_gap.tolist()},
           "wall_s": round(time.time() - t0, 1)}
    write_json(K.T1_DIR / f"null_{space}_k{k}.json", out)
    print(json.dumps({k_: v for k_, v in out.items() if k_ != "boot"}, indent=1), flush=True)
    return out


def pool_signatures(pool_pids: list[str]) -> tuple[np.ndarray, dict]:
    """MinHash signatures of the pool rows: P42's cached LMSYS signatures mapped by source_id,
    else recomputed on the pool texts (p42.data.minhash_signatures)."""
    from p42 import data as D

    pr = prompts().loc[pool_pids]
    info = {}
    try:
        import pyarrow.parquet as pq

        c = p42_cache_dir()
        tab = pq.read_table(c / "lmsys.firstturns.parquet", columns=["source_id"])
        sid = np.array(tab.column("source_id").to_pylist(), dtype=object)
        pos = pd.Series(np.arange(len(sid)), index=sid)
        if pos.index.duplicated().any():
            raise ValueError("lmsys source_id not unique")
        rows = pos.reindex(pr["source_id"].astype(str).to_numpy()).to_numpy()
        if np.isnan(rows.astype(float)).any():
            raise ValueError("pool source_ids missing from the LMSYS cache")
        rows = rows.astype(int)
        h = np.load(c / "lmsys.hash.npy", mmap_mode="r")[rows]
        nt = ~pr["truncated"].astype(bool).to_numpy()
        hp = D.exact_hashes(pr["text"].tolist())
        agree = float((h[nt] == hp[nt]).mean())
        if agree < 0.99:
            raise ValueError(f"exact hashes agree on only {agree:.3f} of untruncated pool rows")
        sig = np.asarray(np.load(c / "lmsys.sig.npy", mmap_mode="r")[rows])
        info = {"method": "P42 lmsys.sig.npy mapped by source_id",
                "hash_agreement_untruncated": agree}
    except Exception as e:  # noqa: BLE001 - recompute
        info = {"method": "recomputed (p42.data.minhash_signatures on pool texts)",
                "reason": f"{type(e).__name__}: {e}"}
        sig = D.minhash_parallel([pr["text"].tolist()[i:i + 5000]
                                  for i in range(0, len(pr), 5000)], n_workers=N_PROC)
    return sig, info


def _group_table(pids: list[str], rep: np.ndarray) -> pd.DataFrame:
    size = np.bincount(rep, minlength=len(rep))[rep]
    df = pd.DataFrame({"pid": pids, "rep": [pids[r] for r in rep], "group_size": size})
    return df[df["group_size"] >= 2]


def _group_summary(rep: np.ndarray) -> dict:
    sizes = np.bincount(rep)
    sizes = sizes[sizes > 0]
    return {"n_items": len(rep), "n_groups_ge2": int((sizes >= 2).sum()),
            "n_in_groups": int(sizes[sizes >= 2].sum()), "n_removed": int(len(rep) - len(sizes)),
            "largest": sorted(sizes.tolist(), reverse=True)[:10]}


def cmd_dedup(spaces: list[str]) -> dict:
    t0 = time.time()
    pr = prompts()
    pool_pids = sorted(pr.loc[pr["set"] == P42K.POOL, "pid"].tolist())
    sig, info = pool_signatures(pool_pids)
    ei, ej = minhash_edges(sig)
    rep = components(len(pool_pids), ei, ej)
    _group_table(pool_pids, rep).to_csv(K.T1_DIR / "dedup_text.csv", index=False)
    out = {"textual": {"rule": f"MinHash est. Jaccard >= {P42K.MINHASH_JACCARD}, LSH bands "
                       "(p42.data), union-find, representative = lowest pid",
                       "signatures": info, "n_edges": len(ei), **_group_summary(rep),
                       "wall_s": round(time.time() - t0, 1)}, "activation": {}}
    for s in spaces:
        t1 = time.time()
        try:
            X, pids, _ = load_space(s)
        except FileNotFoundError as e:
            out["activation"][s] = {"skipped": str(e)}
            continue
        row = {p: i for i, p in enumerate(pids)}
        Xp = X[[row[p] for p in pool_pids]]
        with _threads():
            ai, aj = cosine_edges(Xp, K.T1_ACT_DUP_COS)
        arep = components(len(pool_pids), ai, aj)
        _group_table(pool_pids, arep).to_csv(K.T1_DIR / f"dedup_act_{s}.csv", index=False)
        out["activation"][s] = {"rule": f"cosine >= {K.T1_ACT_DUP_COS} in space {s} "
                                "(blocked), union-find, representative = lowest pid",
                                "n_edges": len(ai), **_group_summary(arep),
                                "wall_s": round(time.time() - t1, 1)}
    prev = K.T1_DIR / "dedup.json"
    if prev.is_file():  # keep activation records of spaces not rerun
        old = json.loads(prev.read_text())
        for s, v in old.get("activation", {}).items():
            out["activation"].setdefault(s, v)
    write_json(prev, out)
    print(json.dumps(out, indent=1), flush=True)
    return out


def keep_mask_from(path: Path, pool_pids: list[str]) -> np.ndarray:
    """True for pool pids that are their group's representative (or ungrouped)."""
    if not path.is_file():
        raise FileNotFoundError(f"{path}: run `dedup` first")
    g = pd.read_csv(path)
    drop = set(g.loc[g["pid"] != g["rep"], "pid"])
    return np.array([p not in drop for p in pool_pids])


def _top_for(space: str, k: int, U: Union, with_act: bool = True) -> dict:
    lab, _ = load_regions(space, k, U)
    st = region_stats(lab, U.is_id, U.w, k)
    top = top_regions(st)
    keep_pool_t = np.ones(len(U.pids), bool)
    keep_pool_t[~U.is_id] = keep_mask_from(K.T1_DIR / "dedup_text.csv", U.pool_pids)
    st_t = collapse_stats(lab, U.is_id, U.bins, k, keep_pool_t & ~U.is_id)
    act = K.T1_DIR / f"dedup_act_{space}.csv"
    st_a = None
    if with_act and act.is_file():
        keep_pool_a = np.ones(len(U.pids), bool)
        keep_pool_a[~U.is_id] = keep_mask_from(act, U.pool_pids)
        st_a = collapse_stats(lab, U.is_id, U.bins, k, keep_pool_a & ~U.is_id)
    rows = []
    for r in top:
        o, t = st.loc[r], st_t.loc[r]
        rec = {"region": r, "P": int(o["P"]), "n_id": int(o["n_id"]), "id_mass": float(o["id_mass"]),
               "kish_n": float(o["kish_n"]), "s": float(o["s"]),
               "low_support": bool(o["kish_n"] < K.T1_LOW_SUPPORT_NEFF),
               "P_text": int(t["P"]), "s_text": float(t["s"]),
               "robust": bool(t["s"] <= K.T1_SHARE_MAX and t["P"] >= K.T1_MIN_DISTINCT),
               "still_P_ge_min_pool_text": bool(t["P"] >= K.T1_MIN_POOL)}
        if st_a is not None:
            a = st_a.loc[r]
            rec |= {"P_act": int(a["P"]), "s_act": float(a["s"]),
                    "share_lost_act": float(1 - a["P"] / o["P"]) if o["P"] else np.nan}
        rows.append(rec)
    pool_members = {r: {U.pids[i] for i in np.flatnonzero((lab == r) & ~U.is_id)} for r in top}
    return {"stats": st, "top": rows, "n_candidates": len(candidates(st)),
            "n_robust_top": int(sum(x["robust"] for x in rows)), "members": pool_members}


def cmd_top(space: str, k: int) -> dict:
    t0 = time.time()
    U = Union(space)
    main = _top_for(space, k, U)
    nullp = K.T1_DIR / f"null_{space}_k{k}.json"
    if nullp.is_file():
        boot = json.loads(nullp.read_text())["boot"]["p_s_le_max"]
        for rec in main["top"]:
            rec["boot_p_s_le_max"] = boot[rec["region"]]
    main_members = set().union(*main["members"].values()) if main["members"] else set()
    sens = {}
    for kk in sorted({K.T1_K, *K.T1_K_SENS} - {k}):
        if not regions_path(space, kk).is_file():
            sens[kk] = {"skipped": "regions not run"}
            continue
        o = _top_for(space, kk, U, with_act=False)
        mem = set().union(*o["members"].values()) if o["members"] else set()
        sens[kk] = {"n_candidates": o["n_candidates"], "n_top": len(o["top"]),
                    "n_robust_top": o["n_robust_top"],
                    "top_pool_members": len(mem),
                    "jaccard_top_members_vs_main_k": jaccard(mem, main_members)}
    out = {"space": space, "k": k, "rules": {
        "candidate": f"s <= {K.T1_SHARE_MAX} and P >= {K.T1_MIN_POOL}",
        "top": f"{K.T1_TOP} candidates with lowest s, ties larger P",
        "robust": f"after textual collapse (regions fixed, weights recomputed): s <= "
                  f"{K.T1_SHARE_MAX} and >= {K.T1_MIN_DISTINCT} distinct pool prompts",
        "low_support": f"Kish n < {K.T1_LOW_SUPPORT_NEFF}"},
        "n_candidates": main["n_candidates"], "n_top": len(main["top"]),
        "n_robust_top": main["n_robust_top"], "top_pool_members": len(main_members),
        "top": main["top"], "k_sensitivity": sens, "wall_s": round(time.time() - t0, 1)}
    write_json(K.T1_DIR / f"top_{space}_k{k}.json", out)
    pd.DataFrame(main["top"]).to_csv(K.T1_DIR / f"top_{space}_k{k}.csv", index=False)
    print(pd.DataFrame(main["top"]).to_string(), flush=True)
    print(json.dumps({x: out[x] for x in ("n_candidates", "n_top", "n_robust_top",
                                         "k_sensitivity")}, indent=1), flush=True)
    return out


def _load_top(space: str, k: int) -> dict:
    p = K.T1_DIR / f"top_{space}_k{k}.json"
    if not p.is_file():
        raise FileNotFoundError(f"{p}: run `top --space {space}` first")
    return json.loads(p.read_text())


def cmd_samples(space: str, k: int) -> dict:
    U = Union(space)
    lab, cen = load_regions(space, k, U)
    top = _load_top(space, k)
    pr = prompts()
    tg = pd.read_csv(K.T1_DIR / "dedup_text.csv").set_index("pid")["group_size"]
    id_idx = np.flatnonzero(U.is_id)
    out, md = [], [f"# P45 T1 samples, space {space}, k = {k}\n",
                   (f"Top regions from `top_{space}_k{k}.json`. Per region: "
                    f"{K.T1_READ_MEMBERS} seeded random pool members (rng [SEED_T1, space, "
                    f"region]) and the {K.T1_READ_ID} id_ref prompts nearest the centroid. "
                    f"Texts cut to {TEXT_CHARS} characters; `dup` = textual group size.\n")]
    for rank, rec in enumerate(top["top"]):
        r = rec["region"]
        mem = np.flatnonzero((lab == r) & ~U.is_id)
        rng = np.random.default_rng([K.SEED_T1, _space_code(space), r])
        pick = np.sort(rng.choice(mem, min(K.T1_READ_MEMBERS, len(mem)), replace=False))
        c = cen[r] / max(np.linalg.norm(cen[r]), 1e-12)
        near = id_idx[np.argsort(-(U.X[id_idx] @ c), kind="stable")[:K.T1_READ_ID]]

        def item(i):
            p = U.pids[i]
            return {"pid": p, "n_tokens": int(pr.at[p, "n_tokens"]), "lang": pr.at[p, "lang"],
                    "dup": int(tg.get(p, 1)), "text": _text(p)[:TEXT_CHARS]}

        members = [item(i) for i in pick]
        ids = [item(i) for i in near]
        out.append({"rank": rank, **rec, "members": members, "id_nearest": ids})
        md.append(f"\n## Region {r} (rank {rank}): P = {rec['P']}, s = {rec['s']:.4f}, "
                  f"Kish n = {rec['kish_n']:.1f}, robust = {rec['robust']}, "
                  f"P_text = {rec['P_text']}\n")
        md.append("### Pool members\n")
        for j, m in enumerate(members):
            md.append(f"{j + 1}. `{m['pid']}` ({m['n_tokens']} tok, {m['lang']}, dup "
                      f"{m['dup']}): {_md(m['text'])}")
        md.append("\n### Nearest id_ref\n")
        for j, m in enumerate(ids):
            md.append(f"{j + 1}. `{m['pid']}` ({m['n_tokens']} tok): {_md(m['text'])}")
    res = {"space": space, "k": k, "regions": out}
    write_json(K.T1_DIR / f"samples_{space}.json", res)
    (K.T1_DIR / f"samples_{space}.md").write_text("\n".join(md) + "\n")
    print(f"[samples {space}] {len(out)} regions -> {K.T1_DIR / f'samples_{space}.md'}")
    return res


def _md(t: str) -> str:
    return " ".join(str(t).split()).replace("|", "\\|")


def labels_path(space: str) -> Path:
    return K.T1_DIR / f"labels_{space}.json"


def cmd_labels_template(space: str, k: int, force: bool = False) -> Path:
    p = labels_path(space)
    if p.exists() and not force:
        raise FileExistsError(f"{p} exists; pass --force to overwrite")
    top = _load_top(space, k)
    sk = {str(r["region"]): {"label": None, "topic": None, "definition": None, "regex": None}
          for r in top["top"]}
    write_json(p, sk)
    print(f"[labels-template] {p}: {len(sk)} regions; label in {LABELS}")
    return p


def cmd_validate_labels(k: int = K.T1_K, require_commit: bool = True) -> dict:
    entries, report = [], {}
    for space in K.SPACES:
        p = labels_path(space)
        if not p.is_file():
            continue
        if require_commit:
            ok, why = git_committed_clean(p)
            if not ok:
                raise RuntimeError(f"{p}: {why}; commit the labels before validating")
        labels = json.loads(p.read_text())
        samp = json.loads((K.T1_DIR / f"samples_{space}.json").read_text())
        texts = {str(r["region"]): [m["text"] for m in r["members"]]  # the read (cut) texts
                 for r in samp["regions"]}
        rep = {}
        for reg, lab in labels.items():
            if lab.get("label") not in LABELS:
                raise ValueError(f"{p} region {reg}: label {lab.get('label')!r} not in {LABELS}")
            if lab["label"] != "topic":
                rep[reg] = {"label": lab["label"]}
                continue
            if not lab.get("topic") or not lab.get("regex"):
                raise ValueError(f"{p} region {reg}: a topic needs topic and regex")
            v = validate_regex(lab["regex"], texts[reg])
            rep[reg] = {"label": "topic", "topic": lab["topic"], **v}
            entries.append({"space": space, "k": k, "region": int(reg), **lab})
        report[space] = rep
    bad = {s: [r for r, v in d.items() if v.get("label") == "topic" and not v["ok"]]
           for s, d in report.items()}
    topics = merge_topics(entries)
    out = {"per_space": report, "failing_regex": bad, "topics": topics,
           "n_distinct_topics": len(topics),
           "rule": f"regex (re.IGNORECASE) matches >= {K.T1_REGEX_MIN_HITS} of the "
                   f"{K.T1_READ_MEMBERS} read members"}
    write_json(K.T1_DIR / "topics.json", out)
    if any(bad.values()):
        raise SystemExit(f"regexes below {K.T1_REGEX_MIN_HITS} hits: {bad}; rewrite and commit")
    print(f"[validate-labels] {len(topics)} distinct topics -> {K.T1_DIR / 'topics.json'}")
    return out


def _load_topics() -> dict:
    p = K.T1_DIR / "topics.json"
    if not p.is_file():
        raise FileNotFoundError(f"{p}: run validate-labels first")
    t = json.loads(p.read_text())
    if any(t["failing_regex"].values()):
        raise RuntimeError("topics.json has failing regexes")
    return t["topics"]


def _regex_worker(args):
    texts, patterns = args
    rxs = [re.compile(p, re.IGNORECASE) for p in patterns]
    return [np.array([i for i, t in enumerate(texts) if t and rx.search(t)], dtype=np.int64)
            for rx in rxs]


def regex_scan(text_chunks, patterns: list[str], n_proc: int = N_PROC):
    """Per pattern: global row indices of matching texts, over an iterable of text lists."""
    from collections import deque
    from concurrent.futures import ProcessPoolExecutor

    hits = [[] for _ in patterns]
    off = 0
    infl = deque()

    def drain(fut, base):
        for j, a in enumerate(fut.result()):
            hits[j].append(a + base)

    with ProcessPoolExecutor(max_workers=n_proc) as ex:
        for ch in text_chunks:
            infl.append((ex.submit(_regex_worker, (ch, patterns)), off))
            off += len(ch)
            if len(infl) >= 2 * n_proc:
                drain(*infl.popleft())
        while infl:
            drain(*infl.popleft())
    return [np.concatenate(h) if h else np.zeros(0, np.int64) for h in hits], off


def lmsys_candidates() -> tuple[np.ndarray, dict]:
    """Row mask over P42's LMSYS cache reproducing p42.data.build's pool candidates: first
    exact occurrence, not an exact Dolci first turn, not a MinHash near-duplicate of Dolci."""
    from p42 import data as D

    p = CACHE / "lmsys_candidates.npy"
    if p.is_file():
        return np.load(p), json.loads((CACHE / "lmsys_candidates.json").read_text())
    c = p42_cache_dir()
    t0 = time.time()
    dh, sh = np.load(c / "dolci_dpo.hash.npy"), np.load(c / "dolci_sft.hash.npy")
    all_h = np.concatenate([dh, sh])
    u = D.first_occurrence_mask(all_h)
    idx_sig = np.concatenate([np.load(c / "dolci_dpo.sig.npy"), np.load(c / "dolci_sft.sig.npy")])[u]
    lh = np.load(c / "lmsys.hash.npy")
    keep = D.first_occurrence_mask(lh) & ~np.isin(lh, np.unique(all_h))
    ki = np.flatnonzero(keep)
    lsig = np.load(c / "lmsys.sig.npy", mmap_mode="r")
    near, _ = D.near_dup_of_index(np.asarray(lsig[ki]), idx_sig)
    mask = np.zeros(len(lh), bool)
    mask[ki[~near]] = True
    info = {"n": int(mask.sum()), "expected_p42_after_dedup_vs_dolci": 584955,
            "rule": "p42.data.build steps, recomputed", "wall_s": round(time.time() - t0, 1)}
    CACHE.mkdir(parents=True, exist_ok=True)
    np.save(p, mask)
    (CACHE / "lmsys_candidates.json").write_text(json.dumps(info))
    return mask, info


def _parquet_texts(source: str, chunk: int = 20_000):
    import pyarrow.parquet as pq

    f = pq.ParquetFile(p42_cache_dir() / f"{source}.firstturns.parquet")
    for b in f.iter_batches(batch_size=chunk, columns=["text"]):
        yield b.column(0).to_pylist()


def _take_texts(source: str, rows: np.ndarray) -> list[str]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    t = pq.read_table(p42_cache_dir() / f"{source}.firstturns.parquet", columns=["text"])
    return t.take(pa.array(np.asarray(rows, np.int64))).column(0).to_pylist()


def cmd_count() -> dict:
    from p42 import data as D

    t0 = time.time()
    topics = _load_topics()
    keys = sorted(topics)
    pats = [topics[x]["regex"] for x in keys]
    c = p42_cache_dir()
    res = {x: {} for x in keys}
    nrows = {}
    for src in (*DOLCI, "lmsys"):
        hits, n = regex_scan(_parquet_texts(src), pats)
        nrows[src] = n
        h = np.load(c / f"{src}.hash.npy")
        if len(h) != n:
            raise RuntimeError(f"{src}: hash rows {len(h)} != parquet rows {n}")
        first = D.first_occurrence_mask(h)
        if src == "lmsys":
            cand, cinfo = lmsys_candidates()
            nrows["lmsys_candidates"] = int(cand.sum())
        for j, x in enumerate(keys):
            m = hits[j]
            if src == "lmsys":
                mc = m[cand[m]]
                res[x]["lmsys_candidates"] = {"matches": len(mc),
                                              "per_million": len(mc) / cand.sum() * 1e6}
                res[x]["lmsys_raw"] = {"matches": len(m), "per_million": len(m) / n * 1e6}
                continue
            md = m[first[m]]
            res[x][src] = {"matches": len(m), "distinct_matches": len(md),
                           "per_million": len(m) / n * 1e6}
            rng = np.random.default_rng([K.SEED_T1, 3, DOLCI.index(src), j])
            pick = np.sort(rng.choice(md, min(K.T1_READ_MATCHES, len(md)), replace=False))
            res[x][src]["read"] = [{"row": int(r), "text": t[:TEXT_CHARS]}
                                   for r, t in zip(pick, _take_texts(src, pick))]
        print(f"[count] {src}: {n} rows scanned ({round(time.time() - t0)} s)", flush=True)
    pool = prompts()[prompts()["set"] == P42K.POOL]
    for j, x in enumerate(keys):
        rx = re.compile(pats[j], re.IGNORECASE)
        pm = int(sum(bool(rx.search(t)) for t in pool["text"]))
        dm = res[x]["dolci_dpo"]["matches"] + res[x]["dolci_sft"]["matches"]
        res[x]["dolci_all"] = {"matches": dm, "per_million": dm / (nrows["dolci_dpo"] +
                                                                    nrows["dolci_sft"]) * 1e6}
        res[x]["pool"] = {"matches": pm, "per_million": pm / len(pool) * 1e6}
        res[x]["regex"] = pats[j]
    out = {"lmsys_rate_basis": "lmsys_candidates (reconstructed 585k dedup)",
           "lmsys_candidates_info": cinfo, "n_rows": nrows, "topics": res,
           "wall_s": round(time.time() - t0, 1)}
    write_json(K.T1_DIR / "count.json", out)
    md = ["# P45 T1 Dolci counts and reading samples\n",
          (f"LMSYS basis: {out['lmsys_rate_basis']} ({nrows['lmsys_candidates']} rows; P42 "
           "reported 584,955). Rates per million first turns.\n")]
    for x in keys:
        r = res[x]
        md.append(f"\n## {topics[x]['topic']}\n\nregex `{r['regex']}`; Dolci DPO "
                  f"{r['dolci_dpo']['matches']}, SFT {r['dolci_sft']['matches']} (rate "
                  f"{r['dolci_all']['per_million']:.1f}/M); LMSYS candidates "
                  f"{r['lmsys_candidates']['matches']} ({r['lmsys_candidates']['per_million']:.1f}"
                  f"/M); pool {r['pool']['matches']}\n")
        for src in DOLCI:
            md.append(f"### {src} matches (seeded, distinct)\n")
            for i, it in enumerate(r[src]["read"]):
                md.append(f"{i + 1}. row {it['row']}: {_md(it['text'])}")
    (K.T1_DIR / "count.md").write_text("\n".join(md) + "\n")
    return out


DOLCI_EMB = CACHE / "dolci_emb"


def _dolci_dpo_distinct() -> np.ndarray:
    from p42 import data as D

    return np.flatnonzero(D.first_occurrence_mask(np.load(p42_cache_dir() / "dolci_dpo.hash.npy")))


def cmd_embed_dolci(dry_run: bool, max_usd: float | None, limit: int | None,
                    concurrency: int = 8) -> int:
    """PAID: text-embedding-3-small for every distinct Dolci DPO first turn (exact-hash first
    occurrence), resumable chunks under results/t1/cache/dolci_emb/chunks; spend under exp P45."""
    import asyncio
    import math

    from p42 import embed as E

    from emx import spend

    rows = _dolci_dpo_distinct()
    texts = _take_texts("dolci_dpo", rows)
    cache = E.load_cache(DOLCI_EMB)
    items = []
    for r, t in zip(rows, texts):
        pid, ti = f"dpo-{int(r):07d}", E.embed_input(t)
        sha = E.text_sha(ti)
        hit = cache.get(pid)
        if hit is None or hit[0] != sha:
            items.append((pid, sha, ti))
    if limit is not None:
        items = items[:limit]
    enc = E._tokenizer()
    est = E.estimate_tokens([t for _, _, t in items], enc)
    proj = E.usd_for_tokens(sum(est))
    reserve = max(E.MIN_RESERVE_USD, math.ceil(proj * E.RESERVE_MARGIN * 1e6) / 1e6)
    print(f"P45 EMBED-DOLCI: {len(rows)} distinct DPO first turns, {len(rows) - len(items)} "
          f"cached, {len(items)} pending; {sum(est):,} est. tokens -> ${proj:.4f}, reservation "
          f"${reserve:.4f}")
    if dry_run:
        return 0
    exp, note = K.PROBE, "p45_embed_dolci"
    if items:
        if max_usd is not None and reserve > max_usd:
            print("P45 EMBED-DOLCI REFUSED: reservation > --max-usd", file=sys.stderr)
            return 2
        spend.recover_uncertain(exp, note)
        stale = spend.find_open(exp, note)
        if stale is not None:
            spend.release_api(stale)
        rid = spend.reserve_api(exp, reserve, note)
        budget = E.EmbedBudget(reserve)
        try:
            spend.mark_submitted(rid, exp=exp)
            client = E.make_client()
            n = asyncio.run(E._run(client, items, est, DOLCI_EMB, budget, concurrency,
                                   E.MAX_RETRIES))
            print(f"P45 EMBED-DOLCI: {n} vectors landed")
        finally:
            if budget.tokens or budget.requests:
                spend.record("judge", P42K.EMBED_MODEL, budget.requests, round(budget.known, 6),
                             note=f"[P45 embed-dolci] {budget.tokens} tokens, "
                                  f"{budget.requests} requests", exp=exp, reservation_id=rid)
            spend.settle_api(rid, total_usd=round(budget.charge, 6), exp=exp,
                             note=f"[P45 embed-dolci] settled: ${budget.known:.4f} known + "
                                  f"${budget.uncertain:.4f} uncertain of ${reserve:.4f}")
        cache = E.load_cache(DOLCI_EMB)
    missing = [r for r in rows if f"dpo-{int(r):07d}" not in cache]
    if missing:
        print(f"P45 EMBED-DOLCI: {len(missing)} rows still lack a vector")
        return 0 if limit is not None else 1
    emb = l2n(np.stack([cache[f"dpo-{int(r):07d}"][1] for r in rows]))
    np.save(DOLCI_EMB / "emb.f16.npy", emb.astype(np.float16))
    np.save(DOLCI_EMB / "rows.npy", rows)
    print(f"P45 EMBED-DOLCI DONE: {emb.shape}")
    return 0


def _members(space: str, k: int) -> dict[int, set]:
    m = pd.read_csv(K.T1_DIR / f"members_{space}_k{k}.csv")
    m = m[m["set"] == P42K.POOL]
    return {int(r): set(g["pid"]) for r, g in m.groupby("region")}


def cmd_semantic(k: int = K.T1_K) -> dict:
    topics = _load_topics()
    if not (DOLCI_EMB / "emb.f16.npy").is_file():
        raise FileNotFoundError(f"{DOLCI_EMB}/emb.f16.npy: run embed-dolci (paid) first")
    D_emb = np.load(DOLCI_EMB / "emb.f16.npy").astype(np.float32)
    rows = np.load(DOLCI_EMB / "rows.npy")
    E, epids = P42A.load_embeddings(p42_results_dir() / "embed")
    erow = {p: i for i, p in enumerate(epids)}
    mem_cache = {}
    out, md = {}, ["# P45 T1 semantic neighbours in Dolci DPO\n",
                   (f"Per topic: the {K.T1_SEMANTIC_NN} distinct Dolci DPO prompts with the "
                    "highest max-cosine (text-embedding-3-small) to any of the topic's pool "
                    "members.\n")]
    for key, t in sorted(topics.items()):
        pids = set()
        for r in t["regions"]:
            sk = (r["space"], r["k"])
            if sk not in mem_cache:
                mem_cache[sk] = _members(*sk)
            pids |= mem_cache[sk].get(r["region"], set())
        M = E[[erow[p] for p in sorted(pids)]]
        best = np.full(len(D_emb), -np.inf, dtype=np.float32)
        with _threads():
            for s in range(0, len(M), 256):
                best = np.maximum(best, (D_emb @ M[s:s + 256].T).max(axis=1))
        o = np.argsort(-best, kind="stable")[:K.T1_SEMANTIC_NN]
        texts = _take_texts("dolci_dpo", rows[o])
        out[key] = {"topic": t["topic"], "n_pool_members": len(pids),
                    "neighbours": [{"row": int(rows[i]), "max_cos": float(best[i]),
                                    "text": tx[:TEXT_CHARS]} for i, tx in zip(o, texts)]}
        md.append(f"\n## {t['topic']} ({len(pids)} pool members)\n")
        for j, nb in enumerate(out[key]["neighbours"]):
            md.append(f"{j + 1}. row {nb['row']} (cos {nb['max_cos']:.3f}): {_md(nb['text'])}")
    write_json(K.T1_DIR / "semantic.json", out)
    (K.T1_DIR / "semantic.md").write_text("\n".join(md) + "\n")
    return out


def cmd_classify(k: int = K.T1_K) -> dict:
    topics = _load_topics()
    counts = json.loads((K.T1_DIR / "count.json").read_text())["topics"]
    vp = K.T1_DIR / "verdicts.json"
    if not vp.is_file():
        raise FileNotFoundError(f"{vp}: the agent's reading verdicts are needed")
    verdicts = json.loads(vp.read_text())
    cats, per = {}, {}
    for key, t in topics.items():
        v = verdicts.get(key) or verdicts.get(t["topic"])
        if v is None:
            raise ValueError(f"verdicts.json has no entry for topic {t['topic']!r}")
        on = int(v["on_topic_dpo"]) + int(v["on_topic_sft"]) + int(v["on_topic_semantic"])
        dr = counts[key]["dolci_all"]["per_million"]
        lr = counts[key]["lmsys_candidates"]["per_million"]
        cats[key] = category(on, dr, lr)
        per[key] = {"topic": t["topic"], "on_topic": on, **v, "dolci_per_million": dr,
                    "lmsys_per_million": lr, "category": cats[key], "regions": t["regions"]}
    robust, members, top_members, label_mix = {}, {}, {}, {}
    for s in K.SPACES:
        p = K.T1_DIR / f"top_{s}_k{k}.json"
        if not p.is_file():
            continue
        top = json.loads(p.read_text())
        robust[s] = {r["region"] for r in top["top"] if r["robust"]}
        mem = _members(s, k)
        for r in top["top"]:
            members[(s, r["region"])] = mem.get(r["region"], set())
        top_members[s] = set().union(*[mem.get(r["region"], set()) for r in top["top"]]) \
            if top["top"] else set()
        lp = labels_path(s)
        if lp.is_file():
            lab = json.loads(lp.read_text())
            mix = {}
            for v in lab.values():
                mix[v.get("label")] = mix.get(v.get("label"), 0) + 1
            label_mix[s] = mix
    n_pool = int((prompts()["set"] == P42K.POOL).sum())
    prim = primary_count(topics, cats, robust, members, n_pool)
    prim["final"] = "D" in robust
    if not prim["final"]:
        prim["note"] = "space D (union PCA, from the T2 pod) not yet run: not final"
    ov = {}
    for a, b in (("A", "C"), ("D", "C"), ("A", "D"), ("A", "B")):
        if a in top_members and b in top_members:
            ov[f"{a}_vs_{b}"] = jaccard(top_members[a], top_members[b])
    out = {"primary": prim, "topics": per,
           "category_counts": {c: sum(v == c for v in cats.values()) for c in
                               ("zero_validated_found", "sparse_coverage",
                                "frequency_difference")},
           "label_mix": label_mix, "overlap_jaccard_top_members": ov,
           "rules": {"zero_validated_found": "0 on-topic among all read (DPO regex, SFT regex, "
                     "DPO semantic)", "sparse_coverage": f"Dolci rate <= {K.T1_RATE_RATIO} x "
                     "LMSYS rate", "frequency_difference": f"Dolci rate > {K.T1_RATE_RATIO} x "
                     "LMSYS rate"}}
    write_json(K.T1_DIR / "summary.json", out)
    print(json.dumps(out["primary"], indent=1))
    return out


def cmd_inputs() -> dict:
    files = [p42_data_dir() / "prompts.jsonl"]
    for s in K.SPACES:
        try:
            files += list(space_files(s))
        except FileNotFoundError:
            pass
    c = p42_cache_dir()
    for src in (*DOLCI, "lmsys"):
        files += [c / f"{src}.firstturns.parquet", c / f"{src}.hash.npy", c / f"{src}.sig.npy"]
    files += [K.T1_DIR / x for x in ("verdicts.json",)] + \
        [labels_path(s) for s in K.SPACES] + [DOLCI_EMB / "emb.f16.npy", DOLCI_EMB / "rows.npy"]
    law2 = _law2_path()
    if law2 is not None:
        files.append(law2)
    rec = {str(f): P42A.sha256_file(f) for f in files if f.is_file()}
    cur = json.loads(K.INPUTS_JSON.read_text()) if K.INPUTS_JSON.is_file() else {}
    cur["t1"] = rec
    write_json(K.INPUTS_JSON, cur)
    print(f"[inputs] {len(rec)} files -> {K.INPUTS_JSON}")
    return rec


# --- cli -------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m p45.t1", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("regions")
    r.add_argument("--space", choices=K.SPACES, required=True)
    r.add_argument("--k", type=int, nargs="+", default=[K.T1_K, *K.T1_K_SENS])
    for name in ("null", "top", "samples", "labels-template"):
        p = sub.add_parser(name)
        p.add_argument("--space", choices=K.SPACES, required=True)
        p.add_argument("--k", type=int, default=K.T1_K)
        if name == "labels-template":
            p.add_argument("--force", action="store_true")
    d = sub.add_parser("dedup")
    d.add_argument("--space", choices=K.SPACES, nargs="+", default=["A", "B", "C", "D"])
    v = sub.add_parser("validate-labels")
    v.add_argument("--k", type=int, default=K.T1_K)
    sub.add_parser("count")
    e = sub.add_parser("embed-dolci")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--max-usd", type=float, default=None)
    e.add_argument("--limit", type=int, default=None)
    e.add_argument("--concurrency", type=int, default=8)
    for name in ("semantic", "classify"):
        p = sub.add_parser(name)
        p.add_argument("--k", type=int, default=K.T1_K)
    sub.add_parser("inputs")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    K.T1_DIR.mkdir(parents=True, exist_ok=True)
    if a.cmd == "regions":
        cmd_regions(a.space, a.k)
    elif a.cmd == "null":
        cmd_null(a.space, a.k)
    elif a.cmd == "dedup":
        cmd_dedup(a.space)
    elif a.cmd == "top":
        cmd_top(a.space, a.k)
    elif a.cmd == "samples":
        cmd_samples(a.space, a.k)
    elif a.cmd == "labels-template":
        cmd_labels_template(a.space, a.k, a.force)
    elif a.cmd == "validate-labels":
        cmd_validate_labels(a.k)
    elif a.cmd == "count":
        cmd_count()
    elif a.cmd == "embed-dolci":
        return cmd_embed_dolci(a.dry_run, a.max_usd, a.limit, a.concurrency)
    elif a.cmd == "semantic":
        cmd_semantic(a.k)
    elif a.cmd == "classify":
        cmd_classify(a.k)
    elif a.cmd == "inputs":
        cmd_inputs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
