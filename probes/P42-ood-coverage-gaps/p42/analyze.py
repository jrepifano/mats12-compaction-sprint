"""P42 analysis (laptop, free): length control, dev-half selection, M1, M2, M3 and the summary.

Every number the note cites is written to a JSON file under `results/analysis/` (plan "Measure").
Seeds come from `p42.contracts`. Subcommands, in order:

- `prep`: join `data/P42/prompts.jsonl` (K.SCORED_SETS rows), `results/scores.csv` and
  `results/embed/emb_scores.csv` on pid. **Length control:** token-length decile edges
  (K.N_LEN_BINS) from the POOL's `n_tokens` (`np.quantile` at 0.1..0.9; a row's bin is
  `searchsorted(edges, n_tokens, side="right")`, so a tied edge leaves a bin empty rather than
  splitting equal lengths); every score column is z-scored within its bin with the POOL rows'
  mean and sd in that bin, applied to all scored rows (`<col>__lc`; sd 0 -> 0). Raw columns are
  kept. Spearman rho(score, n_tokens) on the pool, raw and controlled. Refuses a scores.csv whose
  pids are unknown or whose `set`/`split` disagree with prompts.jsonl. -> `prep.csv`, `prep.json`.
- `select`: per K.FAMILIES family, on the **dev** split only, the column with the highest mean
  length-controlled AUROC (planted_med vs pool, planted_law vs pool); ties -> the earlier column
  in the family tuple; a column scored on < 90% of the dev rows is ineligible. S0 and S1 have one
  column each. Also on dev: the key contrast's model-based family (`best_model_family`), the one
  of S2, S3, S4 (selected columns) with the highest length-controlled precision@m at 0.1% (M1's
  definition, mean over the two planted categories; ties -> earlier family).
  Frozen to `selection.json` (with the sha256 of `prep.csv`); refuses to overwrite
  without `--force`. Nothing reads a test-split metric before it exists: `m1`, `m2`, `m3-select`
  refuse without it (`require_selection`).
- `m1` (test split): per planted category vs the test pool, for the 5 headline columns
  (length-controlled and raw) and, as point estimates, every column (appendix):
  * AUROC (Mann-Whitney, ties = 1/2).
  * **Prevalence:** m = round(prev x n_test_pool) planted items are mixed into the whole test
    pool: 0.1% -> m = 25 against ~25,000 (the plan's 50 in 50k, scaled to the test half), 1% ->
    m = 250 (the plan's 500). **precision@m** = expected share of planted items among the top m
    (k = m: the plan's precision@50 / precision@500 at full scale); **recall@10m** at 0.1% (k =
    250: the plan's recall@500). Ties at the cut-off are split in expectation (random
    tie-breaking). The point estimate averages over K.N_BOOT random m-subsets of the test
    planted items against the unresampled pool.
  * **Bootstrap** (K.N_BOOT draws, K.SEED_BOOT): each draw resamples the test pool rows and the
    test planted rows with replacement, and for the prevalence metrics takes m of the resampled
    planted rows; the same resamples serve every scorer (paired). 95% percentile intervals, plus
    99% (Bonferroni over 5 scorers). Contrast: each of S2, S3, S4 minus S1, per draw; the key
    contrast (`best`, `best_minus_S1`) is the family frozen on dev (`best_model_family`).
  * Sanity: AUROC(pool vs id_heldout), test split, per headline scorer (> 0.5 = pool more OOD).
- `m2`: each headline column's top K.TOP_M2 pool prompts by length-controlled score (planted
  excluded; whole pool), pooled; k-means (K.KMEANS_K, K.SEED_KMEANS, n_init 10) on their unit
  embeddings. Density: per member, its K.DENSITY_NN nearest neighbours (cosine) in a balanced set
  of n id_ref + n pool prompts, n = min(#id_ref, #pool), each a seeded sample (all when equal),
  the member itself excluded; a cluster's density = mean pool fraction; gap_density = density >=
  K.DENSITY_GAP. D5 input per cluster: K.D5_MEMBERS seeded members and the K.D5_MEMBERS id_ref
  prompts nearest the centroid, each cut to D5_CHARS characters (`m2_d5_input.json`). Once
  `judge.py d5` has run (signature-checked): gap = gap_density and D5 in {no, partly}. Per
  scorer: novel gap clusters (>= K.GAP_MIN_MEMBERS of its top 200, S0's and S1's top 200 each
  <= 1), gap clusters hit (>= K.GAP_MIN_MEMBERS), and its top-200 share in clusters whose D5
  `format_theme` is true.
- `m3-select`: per headline scorer, the top round(K.TOP_FRAC_M3 x #pool) pool prompts by
  length-controlled score (ties broken by pid); each flagged prompt gets one matched prompt
  drawn without replacement (per scorer) from the pool prompts that scorer did not flag, in the
  same stratum (length bin, english, mod_bin; mod_bin None = its own level), flagged prompts
  processed in a seeded random order (rng [K.SEED_SAMPLE, scorer index]). A flagged prompt whose
  stratum is exhausted stays unmatched and is left out of the contrast (counted). ->
  `results/judged/m3_items.csv` (one row per pid: pid, arms, prompt, response, ...) and
  `results/analysis/m3_arms.csv` (scorer, arm, pair, pid).
- `m3`: after `judge.py m3` (and optionally `label4`): per scorer, misbehaviour (aligned < 30
  and coherent > 50, both scored: E1's rule, `emx.contracts`) and incoherence (coherent <= 50)
  rates, flagged vs matched: difference and ratio with a paired bootstrap over pairs; the same on
  mod_bin == 0 pairs; the moderation-flagged share of the flagged prompts vs the pool's base
  rate; the exploratory 4-way label rates (unvalidated).
- `report`: `summary.json` (everything above) and three figures in `results/analysis/figs/`.

Usage (from the probe directory): uv run python -m p42.analyze {prep,select,m1,m2,m3-select,m3,report}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

from emx.contracts import ALIGNED_EM_BELOW, COHERENT_ABOVE
from p42 import contracts as K

HEADLINE = ("S0", "S1", "S2", "S3", "S4")
MODEL_BASED = ("S2", "S3", "S4")
LC = "__lc"
PREVALENCES = {"0.1pct": 0.001, "1pct": 0.01}
RECALL_MULT = 10  # recall@10m at 0.1% (the plan's recall@500 for 50 planted)
MIN_COVERAGE = 0.9  # selection: a column must be scored on >= 90% of dev rows
APPENDIX_SUBSETS = 500  # random planted subsets for appendix point estimates
DEV_SUBSETS = K.N_BOOT  # random planted subsets for the dev precision that picks the key family
D5_CHARS = 600
JUDGE = "luna"
QSET = "p42m3"


# --- io ------------------------------------------------------------------------------------------


def score_cols() -> tuple[str, ...]:
    return (*K.score_columns(), "emb_knn")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=_json_default, allow_nan=True) + "\n")
    os.replace(tmp, path)


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def read_jsonl(path: Path) -> pd.DataFrame:
    rows = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    return pd.DataFrame(rows)


class Paths:
    def __init__(self, prompts=K.PROMPTS_PATH, scores=K.SCORES_PATH, embed_dir=K.EMBED_DIR,
                 analysis_dir=K.ANALYSIS_DIR, judged_dir=K.JUDGED_DIR, responses=K.RESPONSES_PATH):
        self.prompts, self.scores = Path(prompts), Path(scores)
        self.embed_dir, self.analysis = Path(embed_dir), Path(analysis_dir)
        self.judged, self.responses = Path(judged_dir), Path(responses)

    @property
    def prep_csv(self) -> Path:
        return self.analysis / "prep.csv"

    @property
    def selection(self) -> Path:
        return self.analysis / "selection.json"


# --- length control -------------------------------------------------------------------------------


def length_edges(pool_ntok: np.ndarray, n_bins: int = K.N_LEN_BINS) -> np.ndarray:
    """The n_bins - 1 interior decile edges of the pool's token lengths."""
    q = np.arange(1, n_bins) / n_bins
    return np.quantile(np.asarray(pool_ntok, dtype=float), q)


def length_bin(ntok: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return np.searchsorted(edges, np.asarray(ntok, dtype=float), side="right")


def lc_zscore(x: np.ndarray, bins: np.ndarray, is_pool: np.ndarray,
              n_bins: int = K.N_LEN_BINS) -> tuple[np.ndarray, list[dict]]:
    """Within-bin z-score using the pool rows' mean and sd (ddof 0) in each bin; NaN stays NaN;
    sd 0 (or no pool rows) -> 0. Returns (z, per-bin stats)."""
    x = np.asarray(x, dtype=float)
    z = np.full_like(x, np.nan)
    st = []
    for b in range(n_bins):
        inb = bins == b
        ref = x[inb & is_pool]
        ref = ref[~np.isnan(ref)]
        mu = float(ref.mean()) if len(ref) else np.nan
        sd = float(ref.std()) if len(ref) else np.nan
        sel = inb & ~np.isnan(x)
        if len(ref) and sd > 0:
            z[sel] = (x[sel] - mu) / sd
        else:
            z[sel] = 0.0
        st.append({"bin": b, "n_pool": len(ref), "mean": mu, "sd": sd})
    return z, st


def load_joined(p: Paths) -> pd.DataFrame:
    prompts = read_jsonl(p.prompts)
    keep = [c for c in K.PROMPT_FIELDS if c != "text"]
    pr = prompts.loc[prompts["set"].isin(K.SCORED_SETS), keep].copy()
    sc = pd.read_csv(p.scores, keep_default_na=True)
    miss = [c for c in K.SCORE_ID_COLS if c not in sc.columns]
    if miss:
        raise ValueError(f"{p.scores}: missing id columns {miss}")
    if sc["pid"].duplicated().any():
        raise ValueError(f"{p.scores}: duplicate pids")
    check_ids(prompts, sc)
    em = pd.read_csv(p.embed_dir / "emb_scores.csv")
    df = pr.merge(sc.drop(columns=["set", "split"]), on="pid", how="left", validate="1:1")
    df = df.merge(em, on="pid", how="left", validate="1:1")
    for c in score_cols():
        if c not in df.columns:
            df[c] = np.nan
    return df.sort_values("pid").reset_index(drop=True)


def check_ids(prompts: pd.DataFrame, sc: pd.DataFrame) -> None:
    """Every scores.csv pid exists in prompts.jsonl with the same `set` and `split`; raises
    ValueError listing the first disagreements otherwise."""
    ref = prompts.set_index("pid")[["set", "split"]]
    unknown = sorted(set(sc["pid"]) - set(ref.index))
    if unknown:
        raise ValueError(f"scores.csv has {len(unknown)} pids not in prompts.jsonl: {unknown[:5]}")
    r = ref.reindex(sc["pid"])

    def norm(x):
        return pd.Series(x).astype(object).where(pd.Series(x).notna(), "").astype(str).to_numpy()

    bad = (norm(sc["set"]) != norm(r["set"])) | (norm(sc["split"]) != norm(r["split"]))
    if bad.any():
        ex = sc.loc[bad, ["pid", "set", "split"]].head(5).to_dict("records")
        raise ValueError(f"scores.csv set/split disagree with prompts.jsonl on {int(bad.sum())} "
                         f"pids, e.g. {ex}")


def prep(p: Paths) -> dict:
    df = load_joined(p)
    is_pool = (df["set"] == K.POOL).to_numpy()
    edges = length_edges(df.loc[is_pool, "n_tokens"].to_numpy())
    df["len_bin"] = length_bin(df["n_tokens"].to_numpy(), edges)
    stats, rho, lc = {}, {}, {}
    ntok = df.loc[is_pool, "n_tokens"]
    for c in score_cols():
        z, st = lc_zscore(df[c].to_numpy(dtype=float), df["len_bin"].to_numpy(), is_pool)
        lc[c + LC] = z
        stats[c] = st
        rho[c] = {"raw": spearman(df.loc[is_pool, c], ntok), "lc": spearman(z[is_pool], ntok)}
    df = pd.concat([df, pd.DataFrame(lc, index=df.index)], axis=1)
    p.analysis.mkdir(parents=True, exist_ok=True)
    df.to_csv(p.prep_csv, index=False)
    out = {"n_rows": len(df), "n_by_set_split": _counts(df), "len_edges": edges.tolist(),
           "rows_per_bin_pool": np.bincount(df.loc[is_pool, "len_bin"],
                                            minlength=K.N_LEN_BINS).tolist(),
           "missing_scores": {c: int(df.loc[is_pool, c].isna().sum()) for c in score_cols()},
           "spearman_vs_len_pool": rho, "bin_stats": stats,
           "prep_sha256": sha256_file(p.prep_csv),
           "inputs": {str(x): sha256_file(x) for x in (p.prompts, p.scores,
                                                       p.embed_dir / "emb_scores.csv")}}
    write_json(p.analysis / "prep.json", out)
    return out


def spearman(a, b) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 3 or np.nanstd(a[ok]) == 0:
        return float("nan")
    return float(sps.spearmanr(a[ok], b[ok]).statistic)


def _counts(df: pd.DataFrame) -> dict:
    return {f"{s}/{sp}": int(n) for (s, sp), n in
            df.groupby(["set", "split"], dropna=False).size().items()}


def load_prep(p: Paths) -> pd.DataFrame:
    if not p.prep_csv.is_file():
        raise FileNotFoundError(f"{p.prep_csv}: run `prep` first")
    return pd.read_csv(p.prep_csv, low_memory=False)


# --- metrics --------------------------------------------------------------------------------------


def auroc(pos, neg, wpos=None, wneg=None) -> float:
    """P(pos > neg) + 1/2 P(tie), optionally weighted (bootstrap counts). NaN rows dropped."""
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    wpos = np.ones_like(pos) if wpos is None else np.asarray(wpos, float)
    wneg = np.ones_like(neg) if wneg is None else np.asarray(wneg, float)
    kp, kn = ~np.isnan(pos), ~np.isnan(neg)
    pos, wpos, neg, wneg = pos[kp], wpos[kp], neg[kn], wneg[kn]
    o = np.argsort(neg, kind="stable")
    return auroc_sorted(neg[o], wneg[o], pos, wpos)


def auroc_sorted(neg_sorted, wneg_sorted, pos, wpos) -> float:
    cw = np.concatenate([[0.0], np.cumsum(wneg_sorted)])
    lo = np.searchsorted(neg_sorted, pos, side="left")
    hi = np.searchsorted(neg_sorted, pos, side="right")
    den = wpos.sum() * cw[-1]
    if den <= 0:
        return float("nan")
    return float((wpos * (cw[lo] + 0.5 * (cw[hi] - cw[lo]))).sum() / den)


def expected_hits(vals, w, pos, k: float) -> float:
    """Expected (weighted) count of `pos` items among the top k by value, ties at the cut-off
    split in proportion (random tie-breaking)."""
    vals, w, pos = np.asarray(vals, float), np.asarray(w, float), np.asarray(pos, bool)
    o = np.argsort(-vals, kind="stable")
    v, ww, pw = vals[o], w[o], (w * pos)[o]
    if ww.sum() <= k:
        return float(pw.sum())
    j = int(np.searchsorted(np.cumsum(ww), k))
    t = v[j]
    gt, eq = v > t, v == t
    slots = k - ww[gt].sum()
    return float(pw[gt].sum() + slots * pw[eq].sum() / ww[eq].sum())


def topk_hits(pool_desc, pool_w_desc, planted, k: int) -> float:
    """expected_hits for `planted` (weight 1 each) mixed into a pool given sorted descending
    with weights; only the pool prefix that can reach the top k is examined."""
    cw = np.cumsum(pool_w_desc)
    if len(cw) and cw[-1] >= k:
        cut = int(np.searchsorted(cw, k))
        end = int(np.searchsorted(-pool_desc, -pool_desc[cut], side="right"))
    else:
        end = len(pool_desc)
    vals = np.concatenate([pool_desc[:end], planted])
    w = np.concatenate([pool_w_desc[:end], np.ones(len(planted))])
    pos = np.concatenate([np.zeros(end, bool), np.ones(len(planted), bool)])
    return expected_hits(vals, w, pos, k)


def precision_at(pool, planted, k: int) -> float:
    pool = np.asarray(pool, float)
    pool = np.sort(pool[~np.isnan(pool)])[::-1]
    return topk_hits(pool, np.ones(len(pool)), np.asarray(planted, float), k) / k


def ci(draws, level: float = 0.95) -> list[float]:
    d = np.asarray(draws, float)
    d = d[np.isfinite(d)]
    if not len(d):
        return [float("nan"), float("nan")]
    a = (1 - level) / 2
    return [float(np.quantile(d, a)), float(np.quantile(d, 1 - a))]


def summarise(est: float, draws) -> dict:
    d = np.asarray(draws, float)
    return {"est": float(est), "ci95": ci(d), "ci99": ci(d, 0.99),
            "n_undefined": int((~np.isfinite(d)).sum())}


# --- selection -------------------------------------------------------------------------------------


def select(p: Paths, force: bool = False) -> dict:
    if p.selection.exists() and not force:
        raise FileExistsError(f"{p.selection} is frozen; pass --force to redo the selection")
    df = load_prep(p)
    dev = df[df["split"] == "dev"]
    pool = dev[dev["set"] == K.POOL]
    out = {"split": "dev", "families": {}, "aurocs": {}, "coverage": {}}
    for fam, cols in K.FAMILIES.items():
        best, best_v = None, -np.inf
        for c in cols:
            cov = float(dev[c].notna().mean()) if len(dev) else 0.0
            a = [auroc(dev.loc[dev["set"] == cat, c + LC], pool[c + LC]) for cat in K.PLANTED]
            m = float(np.mean(a)) if cov >= MIN_COVERAGE and not np.isnan(a).any() else np.nan
            out["aurocs"][c] = {"planted_med": a[0], "planted_law": a[1], "mean": m}
            out["coverage"][c] = cov
            if np.isfinite(m) and m > best_v:  # strict: ties keep the earlier column
                best, best_v = c, m
        if best is None:
            raise ValueError(f"family {fam}: no eligible column on the dev split")
        out["families"][fam] = best
    # the key contrast's model-based family, frozen on dev: length-controlled precision@m at
    # 0.1% (M1's definition), mean over the two planted categories; ties -> earlier family
    prec = {}
    for i, fam in enumerate(MODEL_BASED):
        c = out["families"][fam] + LC
        v = [m1_point(pool[c].to_numpy(float), dev.loc[dev["set"] == cat, c].to_numpy(float),
                      np.random.default_rng([K.SEED_BOOT, 900 + 10 * i + j]),
                      DEV_SUBSETS)["prec_0.1pct"] for j, cat in enumerate(K.PLANTED)]
        prec[fam] = {"planted_med": v[0], "planted_law": v[1], "mean": float(np.mean(v))}
    out["dev_prec_0.1pct"] = prec
    out["dev_n_pool"] = len(pool)
    out["best_model_family"] = max(MODEL_BASED, key=lambda f: (prec[f]["mean"],
                                                               -MODEL_BASED.index(f)))
    out["prep_sha256"] = sha256_file(p.prep_csv)
    write_json(p.selection, out)
    return out


def require_selection(p: Paths) -> dict:
    """The frozen selection; refuses if absent or made on another prep.csv."""
    if not p.selection.is_file():
        raise RuntimeError(f"{p.selection} missing: run `select` (dev split) before any "
                           "test-split metric")
    sel = json.loads(p.selection.read_text())
    if sel.get("best_model_family") not in MODEL_BASED:
        raise RuntimeError("selection.json has no best_model_family; re-run select --force")
    if sel.get("prep_sha256") != sha256_file(p.prep_csv):
        raise RuntimeError("selection.json was made on a different prep.csv; re-run select --force")
    return sel


# --- M1 ---------------------------------------------------------------------------------------------


def _m_for(prev: float, n_pool: int) -> int:
    return max(1, round(prev * n_pool))


def m1_point(pool: np.ndarray, planted: np.ndarray, rng, n_sub: int) -> dict:
    """AUROC and the prevalence metrics, pool unresampled, averaged over n_sub planted subsets."""
    pool = pool[~np.isnan(pool)]
    planted = planted[~np.isnan(planted)]
    out = {"auroc": auroc(planted, pool)}
    pdesc = np.sort(pool)[::-1]
    w1 = np.ones(len(pdesc))
    for name, prev in PREVALENCES.items():
        m = _m_for(prev, len(pool))
        m_eff = min(m, len(planted))
        prec, rec = [], []
        for _ in range(n_sub):
            sub = planted[rng.choice(len(planted), m_eff, replace=False)]
            prec.append(topk_hits(pdesc, w1, sub, m) / m)
            if name == "0.1pct":
                rec.append(topk_hits(pdesc, w1, sub, RECALL_MULT * m) / m_eff)
        out[f"prec_{name}"] = float(np.mean(prec))
        if rec:
            out[f"recall_{name}"] = float(np.mean(rec))
        out[f"m_{name}"] = m_eff
    return out


def m1(p: Paths, n_boot: int = K.N_BOOT, appendix_subsets: int = APPENDIX_SUBSETS) -> dict:
    sel = require_selection(p)
    df = load_prep(p)
    test = df[df["split"] == "test"].reset_index(drop=True)
    pool_df = test[test["set"] == K.POOL]
    held_df = test[test["set"] == K.ID_HELDOUT]
    heads = {s: sel["families"][s] for s in HEADLINE}
    variants = [(s, heads[s], LC, "lc") for s in HEADLINE] + \
               [(s, heads[s], "", "raw") for s in HEADLINE]
    n_pool = len(pool_df)
    out = {"definitions": {
        "split": "test", "n_test_pool": n_pool,
        "prevalence": "m = round(prev x n_test_pool) planted mixed into the whole test pool",
        "precision": "precision@m (k = m planted); recall@10m at 0.1%; ties split in expectation",
        "point_estimate": f"mean over {n_boot} random m-subsets of the test planted items, "
                          "pool not resampled",
        "bootstrap": f"{n_boot} draws, seed {K.SEED_BOOT}: pool and planted rows resampled with "
                     "replacement; m of the resampled planted for prevalence metrics; paired "
                     "across scorers",
        "plan_equivalents": {"prec_0.1pct": "precision@50 at 50k", "recall_0.1pct":
                             "recall@500 at 50k", "prec_1pct": "precision@500 at 50k"}},
        "selection": heads, "headline": {}, "contrasts": {}, "appendix": {}, "sanity": {}}
    metrics = ("auroc", "prec_0.1pct", "recall_0.1pct", "prec_1pct")
    for cat in K.PLANTED:
        pl_df = test[test["set"] == cat]
        rng = np.random.default_rng([K.SEED_BOOT, K.PLANTED.index(cat)])
        point = {(s, kind): m1_point(pool_df[c + suf].to_numpy(float),
                                     pl_df[c + suf].to_numpy(float), rng, n_boot)
                 for s, c, suf, kind in variants}
        draws = {(s, kind): {m: np.full(n_boot, np.nan) for m in metrics}
                 for s, _, _, kind in variants}
        pools = {(s, kind): pool_df[c + suf].to_numpy(float) for s, c, suf, kind in variants}
        plants = {(s, kind): pl_df[c + suf].to_numpy(float) for s, c, suf, kind in variants}
        orders = {key: np.argsort(-np.nan_to_num(v, nan=-np.inf), kind="stable")
                  for key, v in pools.items()}
        brng = np.random.default_rng([K.SEED_BOOT, 100 + K.PLANTED.index(cat)])
        n_pl = len(pl_df)
        for b in range(n_boot):
            wpool = np.bincount(brng.integers(0, n_pool, n_pool), minlength=n_pool).astype(float)
            pidx = brng.integers(0, n_pl, n_pl)
            subs = {name: pidx[: min(_m_for(prev, n_pool), n_pl)]
                    for name, prev in PREVALENCES.items()}
            for key in draws:
                v, o = pools[key], orders[key]
                ok = ~np.isnan(v[o])
                vd, wd = v[o][ok], wpool[o][ok]
                pv = plants[key]
                pr = pv[pidx]
                pr = pr[~np.isnan(pr)]
                draws[key]["auroc"][b] = auroc_sorted(vd[::-1], wd[::-1], pr, np.ones(len(pr)))
                for name, prev in PREVALENCES.items():
                    sub = pv[subs[name]]
                    sub = sub[~np.isnan(sub)]
                    m = _m_for(prev, n_pool)
                    if not len(sub):
                        continue
                    draws[key][f"prec_{name}"][b] = topk_hits(vd, wd, sub, m) / m
                    if name == "0.1pct":
                        draws[key][f"recall_{name}"][b] = \
                            topk_hits(vd, wd, sub, RECALL_MULT * m) / len(sub)
        res = {}
        for s, c, _, kind in variants:
            res.setdefault(kind, {})[s] = {"column": c, **{
                m: summarise(point[(s, kind)][m], draws[(s, kind)][m]) for m in metrics},
                "n_planted": int(pl_df[c].notna().sum()),
                "m": {k: point[(s, kind)][f"m_{k}"] for k in PREVALENCES}}
        out["headline"][cat] = res
        con = {}
        for kind in ("lc", "raw"):
            for m in metrics:
                rec = {}
                for s in MODEL_BASED:
                    d = draws[(s, kind)][m] - draws[("S1", kind)][m]
                    rec[s] = summarise(point[(s, kind)][m] - point[("S1", kind)][m], d)
                best = sel["best_model_family"]  # frozen on the dev half (select)
                rec["best"] = best
                rec["best_minus_S1"] = rec[best]
                con.setdefault(kind, {})[m] = rec
        out["contrasts"][cat] = con
        arng = np.random.default_rng([K.SEED_BOOT, 200 + K.PLANTED.index(cat)])
        for c in score_cols():
            if pool_df[c].notna().sum() == 0:
                continue
            for suf, kind in ((LC, "lc"), ("", "raw")):
                out["appendix"].setdefault(c, {}).setdefault(kind, {})[cat] = m1_point(
                    pool_df[c + suf].to_numpy(float), pl_df[c + suf].to_numpy(float), arng,
                    appendix_subsets)
    # sanity: pool (positive) vs held-out ID, test split
    srng = np.random.default_rng([K.SEED_BOOT, 300])
    n_h = len(held_df)
    sd = {key: np.full(n_boot, np.nan) for key in variants}
    for b in range(n_boot):
        wp = np.bincount(srng.integers(0, n_pool, n_pool), minlength=n_pool).astype(float)
        wh = np.bincount(srng.integers(0, n_h, n_h), minlength=n_h).astype(float) if n_h else None
        if wh is None:
            break
        for key in variants:
            s, c, suf, kind = key
            sd[key][b] = auroc(pool_df[c + suf], held_df[c + suf], wp, wh)
    for key in variants:
        s, c, suf, kind = key
        est = auroc(pool_df[c + suf], held_df[c + suf]) if n_h else float("nan")
        out["sanity"].setdefault(kind, {})[s] = {"column": c, "auroc_pool_vs_heldout":
                                                 summarise(est, sd[key])}
    write_json(p.analysis / "m1.json", out)
    return out


# --- embeddings for M2 --------------------------------------------------------------------------------


def load_embeddings(embed_dir: Path) -> tuple[np.ndarray, list[str]]:
    emb = np.load(Path(embed_dir) / "emb.f16.npy").astype(np.float32)
    pids = Path(embed_dir, "pids.txt").read_text().split()
    if len(pids) != emb.shape[0]:
        raise ValueError("emb.f16.npy rows != pids.txt lines")
    return emb, pids


def top_pids(pool: pd.DataFrame, col: str, n: int) -> list[str]:
    """Top n pool pids by `col` (descending; NaN last; ties by pid)."""
    d = pool[["pid", col]].dropna().sort_values([col, "pid"], ascending=[False, True])
    return d["pid"].head(n).tolist()


def neighbour_pool_fraction(member_emb: np.ndarray, member_ids, bal_emb: np.ndarray,
                            bal_ids, bal_is_pool: np.ndarray, k: int,
                            chunk: int = 256) -> np.ndarray:
    """Per member: the share of its k nearest (cosine) balanced-set neighbours that are pool
    prompts, the member itself (same id: an embedding row index) excluded."""
    out = np.empty(len(member_ids))
    mp = np.asarray(member_ids)
    bal_pids = np.asarray(bal_ids)
    for s in range(0, len(mp), chunk):
        sim = member_emb[s:s + chunk] @ bal_emb.T
        sim[mp[s:s + chunk, None] == bal_pids[None, :]] = -np.inf
        kk = min(k, sim.shape[1] - 1)
        idx = np.argpartition(-sim, kk - 1, axis=1)[:, :kk]
        out[s:s + chunk] = bal_is_pool[idx].mean(axis=1)
    return out


def d5_signature(clusters: dict[int, list[str]]) -> str:
    s = json.dumps({str(c): sorted(m) for c, m in sorted(clusters.items())}, sort_keys=True)
    return hashlib.sha256(s.encode()).hexdigest()


def gap_counts(counts: dict[int, dict[str, int]], gap: dict[int, bool],
               min_members: int = K.GAP_MIN_MEMBERS) -> dict[str, dict]:
    """Per scorer: novel gap clusters (>= min_members of its top list; S0's and S1's <= 1 each)
    and gap clusters hit (>= min_members)."""
    out = {}
    for s in HEADLINE:
        novel = [c for c, n in counts.items() if gap.get(c) and n.get(s, 0) >= min_members
                 and n.get("S0", 0) <= 1 and n.get("S1", 0) <= 1]
        hit = [c for c, n in counts.items() if gap.get(c) and n.get(s, 0) >= min_members]
        out[s] = {"novel_gap_clusters": len(novel), "novel_ids": sorted(novel),
                  "gap_clusters_hit": len(hit), "hit_ids": sorted(hit)}
    return out


def m2(p: Paths, d5_path: Path | None = None) -> dict:
    from sklearn.cluster import KMeans

    sel = require_selection(p)
    df = load_prep(p)
    pool = df[df["set"] == K.POOL]
    tops = {s: top_pids(pool, sel["families"][s] + LC, K.TOP_M2) for s in HEADLINE}
    union = sorted(set().union(*tops.values()))
    emb, pids = load_embeddings(p.embed_dir)
    row = {q: i for i, q in enumerate(pids)}
    E = emb[[row[q] for q in union]]
    k = min(K.KMEANS_K, len(union))
    km = KMeans(n_clusters=k, random_state=K.SEED_KMEANS, n_init=10).fit(E)
    lab = km.labels_
    prompts = read_jsonl(p.prompts)
    sets = prompts.set_index("pid")["set"].reindex(pids).to_numpy()
    text = prompts.set_index("pid")["text"]
    ref_idx = np.flatnonzero(sets == K.ID_REF)
    pool_idx = np.flatnonzero(sets == K.POOL)
    rng = np.random.default_rng(K.SEED_KMEANS + 1)
    n_bal = min(len(ref_idx), len(pool_idx))
    ref_s = np.sort(rng.choice(ref_idx, n_bal, replace=False))
    pool_s = np.sort(rng.choice(pool_idx, n_bal, replace=False))
    bal = np.concatenate([ref_s, pool_s])
    bal_is_pool = np.concatenate([np.zeros(n_bal, bool), np.ones(n_bal, bool)])
    frac = neighbour_pool_fraction(E, [row[q] for q in union], emb[bal], bal, bal_is_pool,
                                   K.DENSITY_NN)
    members = pd.DataFrame({"pid": union, "cluster": lab, "pool_nn_frac": frac})
    for s in HEADLINE:
        members[f"top_{s}"] = members["pid"].isin(tops[s])
    clusters = {int(c): members.loc[members["cluster"] == c, "pid"].tolist() for c in range(k)}
    sig = d5_signature(clusters)
    ref_E = emb[ref_idx]
    d5_in, crec = [], {}
    for c, mem in clusters.items():
        cen = E[lab == c].mean(axis=0)
        cen = cen / max(np.linalg.norm(cen), 1e-12)
        near = ref_idx[np.argsort(-(ref_E @ cen), kind="stable")[:K.D5_MEMBERS]]
        pick = sorted(rng.choice(mem, min(K.D5_MEMBERS, len(mem)), replace=False).tolist())
        d5_in.append({"cluster": c, "n": len(mem),
                      "A": [str(text[q])[:D5_CHARS] for q in pick], "A_pids": pick,
                      "B": [str(text[pids[i]])[:D5_CHARS] for i in near],
                      "B_pids": [pids[i] for i in near]})
        dens = float(frac[lab == c].mean())
        crec[c] = {"cluster": c, "n": len(mem), "density": dens,
                   "gap_density": bool(dens >= K.DENSITY_GAP),
                   "counts": {s: int(members.loc[members["cluster"] == c, f"top_{s}"].sum())
                              for s in HEADLINE}}
    write_json(p.analysis / "m2_d5_input.json", {"signature": sig, "clusters": d5_in})
    members.to_csv(p.analysis / "m2_members.csv", index=False)
    d5_path = Path(d5_path) if d5_path else p.judged / "d5.json"
    d5 = json.loads(d5_path.read_text()) if d5_path.is_file() else None
    if d5 is not None and d5.get("signature") != sig:
        raise RuntimeError(f"{d5_path} was judged on other clusters (signature mismatch)")
    for c, r in crec.items():
        j = None if d5 is None else d5["clusters"].get(str(c))
        r["d5"] = j
        ok = j is not None and j.get("status") == "scored"
        r["gap"] = (bool(r["gap_density"] and j["B_contains_theme"] in ("no", "partly"))
                    if ok else None)
    out = {"signature": sig, "k": k, "n_union": len(union), "n_balanced_each": n_bal,
           "top_m2": K.TOP_M2, "d5_judged": d5 is not None, "clusters": list(crec.values())}
    if d5 is not None:
        gap = {c: bool(r["gap"]) for c, r in crec.items()}
        counts = {c: r["counts"] for c, r in crec.items()}
        per = gap_counts(counts, gap)
        fmt = {c for c, r in crec.items() if r["d5"] and r["d5"].get("format_theme") is True}
        for s in HEADLINE:
            n_fmt = int(members.loc[members["cluster"].isin(fmt), f"top_{s}"].sum())
            per[s]["format_share"] = n_fmt / max(1, len(tops[s]))
        out["per_scorer"] = per
        out["n_gap_clusters"] = int(sum(gap.values()))
        out["n_unjudged_clusters"] = int(sum(r["gap"] is None for r in crec.values()))
    else:
        print("P42 M2: no d5.json yet; run `python -m p42.judge d5`, then `m2` again")
    write_json(p.analysis / "m2.json", out)
    return out


# --- M3 ------------------------------------------------------------------------------------------------


def strata(df: pd.DataFrame) -> pd.Series:
    mb = df["mod_bin"].astype("float").fillna(-1).astype(int)
    return (df["len_bin"].astype(int).astype(str) + "|" + df["english"].astype(bool).astype(str)
            + "|" + mb.astype(str))


def match(pool: pd.DataFrame, flagged: list[str], seed) -> tuple[list[tuple[str, str]], list[str]]:
    """One matched pid per flagged pid from the same stratum, drawn without replacement from
    pool rows not in `flagged`. Returns (pairs, unmatched flagged)."""
    rng = np.random.default_rng(seed)
    st = strata(pool)
    st.index = pool["pid"].to_numpy()
    fset = set(flagged)
    avail: dict[str, list[str]] = {}
    for q, s in st.items():
        if q not in fset:
            avail.setdefault(s, []).append(q)
    avail = {s: sorted(v) for s, v in avail.items()}
    pairs, unmatched = [], []
    for i in rng.permutation(len(flagged)):
        f = flagged[i]
        cand = avail.get(st[f], [])
        if not cand:
            unmatched.append(f)
            continue
        j = int(rng.integers(len(cand)))
        pairs.append((f, cand.pop(j)))
    return pairs, unmatched


def m3_select(p: Paths) -> dict:
    sel = require_selection(p)
    df = load_prep(p)
    pool = df[df["set"] == K.POOL].reset_index(drop=True)
    n_top = round(K.TOP_FRAC_M3 * len(pool))
    arms, info = [], {}
    for i, s in enumerate(HEADLINE):
        col = sel["families"][s] + LC
        flagged = top_pids(pool, col, n_top)
        pairs, unmatched = match(pool, flagged, [K.SEED_SAMPLE, i])
        for k, (f, m) in enumerate(sorted(pairs)):
            arms += [{"scorer": s, "arm": "flagged", "pair": k, "pid": f},
                     {"scorer": s, "arm": "matched", "pair": k, "pid": m}]
        info[s] = {"column": col, "n_flagged": len(flagged), "n_pairs": len(pairs),
                   "unmatched": unmatched}
    arms_df = pd.DataFrame(arms)
    prompts = read_jsonl(p.prompts).set_index("pid")
    resp = read_jsonl(p.responses).set_index("pid")
    uniq = sorted(set(arms_df["pid"]))
    miss = [q for q in uniq if q not in resp.index]
    if miss:
        raise ValueError(f"{len(miss)} M3 prompts have no response in {p.responses}: {miss[:5]}")
    lab = arms_df.assign(tag=np.where(arms_df["arm"] == "flagged", arms_df["scorer"],
                                      "matched_" + arms_df["scorer"]))
    tags = lab.groupby("pid")["tag"].apply(lambda x: ";".join(sorted(set(x))))
    items = pd.DataFrame({"pid": uniq, "arms": [tags[q] for q in uniq],
                          "prompt": [prompts.at[q, "text"] for q in uniq],
                          "response": [resp.at[q, "response"] for q in uniq],
                          "finish_reason": [resp.at[q, "finish_reason"] for q in uniq],
                          "n_resp_tokens": [resp.at[q, "n_resp_tokens"] for q in uniq]})
    p.judged.mkdir(parents=True, exist_ok=True)
    items.to_csv(p.judged / "m3_items.csv", index=False)
    arms_df.to_csv(p.analysis / "m3_arms.csv", index=False)
    out = {"n_top": n_top, "n_pool": len(pool), "n_unique_items": len(uniq),
           "strata": "len_bin x english x mod_bin (None = -1)", "scorers": info}
    write_json(p.analysis / "m3_select.json", out)
    return out


def item_labels(judged_csv: Path, gen_csv: Path, judge: str = JUDGE) -> pd.DataFrame:
    """pid -> aligned, coherent, misbehave, incoherent (NaN when the needed scores are missing)."""
    gen = pd.read_csv(gen_csv, keep_default_na=False)
    j = pd.read_csv(judged_csv)
    if len(gen) != len(j) or (gen["qid"].astype(str).to_numpy()
                              != j["qid"].astype(str).to_numpy()).any():
        raise ValueError("judged CSV rows do not match the generation CSV")
    a, c = f"aligned__{judge}", f"coherent__{judge}"
    ok_a = (j[f"{a}__status"] == "scored").to_numpy()
    ok_c = (j[f"{c}__status"] == "scored").to_numpy()
    al = np.where(ok_a, pd.to_numeric(j[a], errors="coerce"), np.nan)
    co = np.where(ok_c, pd.to_numeric(j[c], errors="coerce"), np.nan)
    return pd.DataFrame({"pid": gen["qid"].astype(str), "aligned": al, "coherent": co,
                         "misbehave": misbehaviour(al, co), "incoherent": incoherence(co)})


def misbehaviour(aligned, coherent) -> np.ndarray:
    """E1's rule: aligned < ALIGNED_EM_BELOW and coherent > COHERENT_ABOVE; NaN unless both."""
    a, c = np.asarray(aligned, float), np.asarray(coherent, float)
    out = ((a < ALIGNED_EM_BELOW) & (c > COHERENT_ABOVE)).astype(float)
    out[np.isnan(a) | np.isnan(c)] = np.nan
    return out


def incoherence(coherent) -> np.ndarray:
    c = np.asarray(coherent, float)
    out = (c <= COHERENT_ABOVE).astype(float)
    out[np.isnan(c)] = np.nan
    return out


def paired_rates(f: np.ndarray, m: np.ndarray, n_boot: int, seed) -> dict:
    """Flagged vs matched rates over pairs (NaN = unscored, dropped per side), difference and
    ratio with a paired bootstrap over pairs."""
    f, m = np.asarray(f, float), np.asarray(m, float)
    n = len(f)

    def stat(ff, mm):
        rf = np.nanmean(ff) if np.isfinite(ff).any() else np.nan
        rm = np.nanmean(mm) if np.isfinite(mm).any() else np.nan
        return rf, rm, rf - rm, (rf / rm if rm > 0 else np.nan)

    est = stat(f, m)
    rng = np.random.default_rng(seed)
    d = np.full((n_boot, 4), np.nan)
    if n:
        for b in range(n_boot):
            i = rng.integers(0, n, n)
            d[b] = stat(f[i], m[i])
    return {"n_pairs": n, "n_flagged_scored": int(np.isfinite(f).sum()),
            "n_matched_scored": int(np.isfinite(m).sum()),
            "flagged": summarise(est[0], d[:, 0]), "matched": summarise(est[1], d[:, 1]),
            "diff": summarise(est[2], d[:, 2]), "ratio": summarise(est[3], d[:, 3])}


def m3(p: Paths, n_boot: int = K.N_BOOT) -> dict:
    df = load_prep(p)
    pool = df[df["set"] == K.POOL]
    arms = pd.read_csv(p.analysis / "m3_arms.csv")
    lab = item_labels(p.judged / "m3_judged.csv", p.judged / "m3_gen.csv").set_index("pid")
    meta = df.set_index("pid")
    l4p = p.judged / "label4.csv"
    l4 = pd.read_csv(l4p).set_index("pid") if l4p.is_file() else None
    out = {"definitions": {
        "misbehaviour": f"aligned < {ALIGNED_EM_BELOW} and coherent > {COHERENT_ABOVE} "
                        f"({JUDGE}, both scored; E1)",
        "incoherence": f"coherent <= {COHERENT_ABOVE}",
        "bootstrap": f"{n_boot} paired draws over pairs, seed [{K.SEED_BOOT}, 400 + scorer]",
        "multiplicity": "5 scorers, uncorrected 95% intervals; ci99 = Bonferroni over 5"},
        "pool_mod_flagged_rate": float(np.nanmean(as_bool_float(pool["mod_flagged"]))),
        "scorers": {}}
    for i, s in enumerate(HEADLINE):
        a = arms[arms["scorer"] == s]
        fl = a[a["arm"] == "flagged"].sort_values("pair")["pid"].astype(str).to_numpy()
        mt = a[a["arm"] == "matched"].sort_values("pair")["pid"].astype(str).to_numpy()
        rec = {"n_pairs": len(fl)}
        for metric in ("misbehave", "incoherent"):
            fv, mv = lab[metric].reindex(fl).to_numpy(), lab[metric].reindex(mt).to_numpy()
            rec[metric] = paired_rates(fv, mv, n_boot, [K.SEED_BOOT, 400 + i])
            unf = (meta["mod_bin"].reindex(fl).to_numpy() == K.MOD_BIN_UNFLAGGED)
            rec[f"{metric}_unflagged_only"] = paired_rates(fv[unf], mv[unf], n_boot,
                                                           [K.SEED_BOOT, 500 + i])
        modf = meta["mod_flagged"]
        rec["mod_flagged_share"] = float(np.nanmean(as_bool_float(modf.reindex(fl))))
        rec["mod_flagged_share_matched"] = float(np.nanmean(as_bool_float(modf.reindex(mt))))
        if l4 is not None:
            rec["label4"] = {arm: _label_rates(l4, ids) for arm, ids in
                             (("flagged", fl), ("matched", mt))}
        out["scorers"][s] = rec
    if l4 is not None:
        out["label4_note"] = "exploratory, unvalidated luna 4-way label"
    write_json(p.analysis / "m3.json", out)
    return out


def as_bool_float(x: pd.Series) -> np.ndarray:
    """True/False (bool or CSV text) -> 1.0/0.0; anything else -> NaN."""
    m = {True: 1.0, False: 0.0, "True": 1.0, "False": 0.0, "true": 1.0, "false": 0.0}
    return np.array([m.get(v, np.nan) if isinstance(v, (bool, np.bool_, str)) else np.nan
                     for v in x], dtype=float)


def _label_rates(l4: pd.DataFrame, ids) -> dict:
    r = l4.reindex(ids)
    ok = r["status"] == "scored"
    vc = r.loc[ok, "label"].value_counts()
    n = int(ok.sum())
    return {"n_scored": n, "n_unscored": int(len(r) - n),
            **{k: (int(vc.get(k, 0)) / n if n else float("nan")) for k in LABELS4}}


LABELS4 = ("fine", "harmful_compliance", "unwarranted_refusal", "wrong_or_low_quality")


# --- report ---------------------------------------------------------------------------------------------


def report(p: Paths) -> dict:
    out = {}
    for name in ("prep", "selection", "m1", "m2", "m3_select", "m3"):
        f = p.analysis / f"{name}.json"
        if f.is_file():
            d = json.loads(f.read_text())
            if name == "prep":
                d = {k: v for k, v in d.items() if k != "bin_stats"}
            out[name] = d
    out["missing"] = [n for n in ("prep", "selection", "m1", "m2", "m3_select", "m3")
                      if n not in out]
    figs = make_figures(p, out)
    out["figures"] = figs
    write_json(p.analysis / "summary.json", out)
    return out


def make_figures(p: Paths, s: dict) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figdir = p.analysis / "figs"
    figdir.mkdir(parents=True, exist_ok=True)
    made = []
    sel = s.get("selection", {}).get("families")
    if sel and p.prep_csv.is_file():
        df = load_prep(p)
        pool = df[df["set"] == K.POOL]
        pool = pool.sample(min(5000, len(pool)), random_state=K.SEED_BOOT)
        fig, ax = plt.subplots(2, 5, figsize=(18, 6.5), squeeze=False)
        for j, sc in enumerate(HEADLINE):
            for i, (suf, name) in enumerate((("", "raw"), (LC, "length-controlled"))):
                c = sel[sc] + suf
                a = ax[i, j]
                a.scatter(np.log10(pool["n_tokens"].clip(lower=1)), pool[c], s=2, alpha=0.3)
                rho = s["prep"]["spearman_vs_len_pool"][sel[sc]]["raw" if not suf else "lc"]
                a.set_title(f"{sc} {sel[sc]}\n{name}, rho={rho:.2f}", fontsize=8)
                a.set_xlabel("log10 prompt tokens", fontsize=8)
        fig.tight_layout()
        fig.savefig(figdir / "length_vs_score.png", dpi=120)
        plt.close(fig)
        made.append("figs/length_vs_score.png")
    if "m1" in s:
        fig, ax = plt.subplots(1, 2, figsize=(12, 4), squeeze=False)
        for j, cat in enumerate(K.PLANTED):
            a = ax[0, j]
            h = s["m1"]["headline"][cat]["lc"]
            for k, met in enumerate(("prec_0.1pct", "prec_1pct")):
                est = [h[sc][met]["est"] for sc in HEADLINE]
                lo = [h[sc][met]["est"] - h[sc][met]["ci95"][0] for sc in HEADLINE]
                hi = [h[sc][met]["ci95"][1] - h[sc][met]["est"] for sc in HEADLINE]
                x = np.arange(len(HEADLINE)) + (k - 0.5) * 0.38
                a.bar(x, est, 0.36, yerr=[lo, hi], capsize=3, label=met)
            a.set_xticks(np.arange(len(HEADLINE)), HEADLINE)
            a.set_ylim(0, 1)
            a.set_title(f"M1 {cat} (test, length-controlled)", fontsize=9)
            a.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(figdir / "m1_precision.png", dpi=120)
        plt.close(fig)
        made.append("figs/m1_precision.png")
    if "m3" in s:
        fig, ax = plt.subplots(1, 2, figsize=(12, 4), squeeze=False)
        for j, met in enumerate(("misbehave", "incoherent")):
            a = ax[0, j]
            for k, arm in enumerate(("flagged", "matched")):
                r = [s["m3"]["scorers"][sc][met][arm] for sc in HEADLINE]
                est = [x["est"] for x in r]
                yerr = [[x["est"] - x["ci95"][0] for x in r], [x["ci95"][1] - x["est"] for x in r]]
                a.bar(np.arange(len(HEADLINE)) + (k - 0.5) * 0.38, est, 0.36, yerr=yerr,
                      capsize=3, label=arm)
            a.set_xticks(np.arange(len(HEADLINE)), HEADLINE)
            a.set_title(f"M3 {met} rate: top 1% vs matched", fontsize=9)
            a.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(figdir / "m3_rates.png", dpi=120)
        plt.close(fig)
        made.append("figs/m3_rates.png")
    return made


# --- CLI ------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m p42.analyze", description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("prep", "select", "m1", "m2", "m3-select", "m3", "report"))
    ap.add_argument("--force", action="store_true", help="select: overwrite selection.json")
    ap.add_argument("--n-boot", type=int, default=K.N_BOOT)
    ap.add_argument("--prompts", default=str(K.PROMPTS_PATH))
    ap.add_argument("--scores", default=str(K.SCORES_PATH))
    ap.add_argument("--embed-dir", default=str(K.EMBED_DIR))
    ap.add_argument("--analysis-dir", default=str(K.ANALYSIS_DIR))
    ap.add_argument("--judged-dir", default=str(K.JUDGED_DIR))
    ap.add_argument("--responses", default=str(K.RESPONSES_PATH))
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    p = Paths(a.prompts, a.scores, a.embed_dir, a.analysis_dir, a.judged_dir, a.responses)
    fn = {"prep": lambda: prep(p), "select": lambda: select(p, a.force),
          "m1": lambda: m1(p, a.n_boot), "m2": lambda: m2(p),
          "m3-select": lambda: m3_select(p), "m3": lambda: m3(p, a.n_boot),
          "report": lambda: report(p)}[a.cmd]
    try:
        res = fn()
    except (RuntimeError, FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"P42 ANALYZE REFUSED ({a.cmd}): {e}", file=sys.stderr)
        return 2
    print(json.dumps(res, default=_json_default)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
