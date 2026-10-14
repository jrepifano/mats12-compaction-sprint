"""P45 T2 analysis (laptop, free): SAE-error detectors vs P42's best scorer (S3, Mahalanobis L24)
on P42's benchmark plus the legal-matched planted set (plan.md "Measure: T2", "Predictions: T2
decision tree").

Reuses P42's machinery by import (`p42.analyze`): length control (`length_edges`, `length_bin`,
`lc_zscore`: within pool-decile z-scores with pool statistics, P42's convention), AUROC
(`auroc`, `auroc_sorted`), precision at prevalence (`m1_point`, `topk_hits`, `_m_for`) and
intervals (`summarise`, `ci`).

Subcommands (from the probe dir: `uv run python -m p45.analyze <cmd>`):
- `prep`: join P42's prompts.jsonl (K.SCORED_SETS rows of P42) and data/P45/planted_law2.jsonl,
  results/scores.csv (pod; K.SCORE_ID_COLS + K.SCORE_COLS) and S1 = `emb_knn` from P42's
  embed/emb_scores.csv plus results/embed_law2/emb_scores.csv (absent -> S1 NaN for law2, said
  in prep.json). Length control exactly as P42; Spearman rho on the pool vs length and vs
  norm_L24/norm_L16, raw and length-controlled. **Common valid-row mask** `valid` = finite for
  every ACTIVE candidate column and maha_L24 (counts dropped per set). Active candidates =
  K.CANDIDATES (frozen order) at the layers the run used: `settings.layers` of the smoke.json
  beside scores.csv (the pod's L16 cut leaves only L24), else the candidates with any finite
  score. -> results/analysis/prep.csv
  (gitignored) and prep.json.
- `select` (dev split, valid rows): categories = K.PLANTED_EVAL, or planted_med alone if
  data.meta.json marks the law2 pre-flight failed; per candidate, the mean length-controlled
  AUROC (category vs dev pool); best = max, ties -> earlier in K.CANDIDATES. Frozen to
  selection.json with prep.csv's sha256; refuses to overwrite without --force.
- `evaluate` (test split, valid rows): per category (the selection's, plus P42's planted_law
  as reported-only) and every column of EVAL_COLS: AUROC vs the test pool, precision@1% (m =
  round(0.01 x n_test_pool)) and @0.1%, as P42's M1 (point estimate = mean over random
  m-subsets of the test planted items, pool unresampled). Paired bootstrap (K.N_BOOT draws,
  K.SEED_BOOT): every draw resamples the test pool rows and the planted rows with replacement,
  and the SAME resample (and the same planted subset) serves every scorer. Contrasts best - S3
  (ΔAUROC, Δprecision@1%, Δprecision@0.1%) and the precision@1% ratio. Clean benchmark (planted
  vs test id_heldout AUROC) and pool vs id_heldout AUROC per column. Then the plan's ordered
  decision tree (`decide`). S3 reproduction: Spearman of this run's maha_L24 with P42's
  scores.csv maha_dpo_p_L24 on shared pids (gate K.S3_REPRO_MIN_SPEARMAN, reported). ->
  results/analysis/t2.json.
- `report`: results/analysis/t2_summary.md and figs/len_vs_score.png.
"""

from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    if not os.environ.get(_v, "").isdigit() or int(os.environ[_v]) > 4:
        os.environ[_v] = "4"

import argparse
import importlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from p45 import contracts as K  # first: puts p42 on sys.path

A = importlib.import_module("p42.analyze")
P42K = importlib.import_module("p42.contracts")

LC = A.LC
S3 = "maha_L24"
S1 = "emb_knn"
COLS = K.SCORE_COLS + (S1,)
EVAL_COLS = K.CANDIDATES + K.EXPLORATORY + K.BASELINES + (S1,)
REPORTED_CATS = (P42K.PLANTED_LAW,)
PREV = {"prec_1pct": 0.01, "prec_0.1pct": 0.001}
METRICS = ("auroc", "prec_1pct", "prec_0.1pct")
HEAD_SUBSETS = K.N_BOOT  # planted subsets for best and S3 point estimates
APPX_SUBSETS = 500
MAIN_ROOT = K.P42_RESULTS_MAIN.parents[2]


def _first(*paths: Path) -> Path:
    for p in paths:
        if Path(p).is_file():
            return Path(p)
    return Path(paths[0])


class Paths:
    def __init__(self, p42_prompts=None, law2=None, scores=K.SCORES_PATH, p42_emb=None,
                 law2_emb=K.RESULTS_DIR / "embed_law2" / "emb_scores.csv",
                 analysis_dir=K.ANALYSIS_DIR, data_meta=None, p42_scores=None):
        self.p42_prompts = Path(p42_prompts) if p42_prompts else _first(
            P42K.PROMPTS_PATH, MAIN_ROOT / "data" / "P42" / "prompts.jsonl")
        self.law2 = Path(law2) if law2 else _first(
            K.LAW2_PATH, MAIN_ROOT / "data" / "P45" / K.LAW2_PATH.name)
        self.scores = Path(scores)
        self.p42_emb = Path(p42_emb) if p42_emb else _first(
            P42K.EMBED_DIR / "emb_scores.csv", K.P42_RESULTS_MAIN / "embed" / "emb_scores.csv")
        self.law2_emb = Path(law2_emb)
        self.analysis = Path(analysis_dir)
        self.data_meta = Path(data_meta) if data_meta else _first(
            K.DATA_META, MAIN_ROOT / "data" / "P45" / K.DATA_META.name)
        self.p42_scores = Path(p42_scores) if p42_scores else _first(
            P42K.SCORES_PATH, K.P42_RESULTS_MAIN / "scores.csv")
        self.smoke = self.scores.parent / "smoke.json"  # the pod's run settings (layer cut)

    @property
    def prep_csv(self) -> Path:
        return self.analysis / "prep.csv"

    @property
    def selection(self) -> Path:
        return self.analysis / "selection.json"


# --- prep ---------------------------------------------------------------------------------------------


def load_prompts(p: Paths) -> pd.DataFrame:
    pr = A.read_jsonl(p.p42_prompts)
    pr = pr[pr["set"].isin(P42K.SCORED_SETS)]
    if p.law2.is_file():
        pr = pd.concat([pr, A.read_jsonl(p.law2)], ignore_index=True)
    if pr["pid"].duplicated().any():
        raise ValueError("duplicate pids across P42 prompts and planted_law2")
    return pr


def active_candidates(sc: pd.DataFrame, smoke: Path) -> tuple[list[str], str]:
    """K.CANDIDATES (frozen order) at the layers the run used (smoke.json settings.layers),
    else every candidate with a finite score. A recorded layer whose columns are all NaN fails."""
    finite = [c for c in K.CANDIDATES
              if c in sc.columns and np.isfinite(sc[c].to_numpy(float)).any()]
    layers = None
    if Path(smoke).is_file():
        layers = (json.loads(Path(smoke).read_text()).get("settings") or {}).get("layers")
    if layers:
        act = [c for c in K.CANDIDATES if int(c.rsplit("_L", 1)[1]) in set(layers)]
        missing = [c for c in act if c not in finite]
        if missing:
            raise ValueError(f"run settings use layers {layers} but {missing} have no finite score")
        return act, f"layers {sorted(layers)} from {smoke}"
    if not finite:
        raise ValueError("no candidate column has a finite score")
    return finite, "candidates with any finite score (no run settings found)"


def prep(p: Paths) -> dict:
    prompts = load_prompts(p)
    keep = [c for c in P42K.PROMPT_FIELDS if c != "text"]
    sc = pd.read_csv(p.scores)
    miss = [c for c in K.SCORE_ID_COLS if c not in sc.columns]
    if miss:
        raise ValueError(f"{p.scores}: missing id columns {miss}")
    if sc["pid"].duplicated().any():
        raise ValueError(f"{p.scores}: duplicate pids")
    A.check_ids(prompts, sc)
    em = [pd.read_csv(p.p42_emb)]
    law2_s1 = p.law2_emb.is_file()
    if law2_s1:
        em.append(pd.read_csv(p.law2_emb))
    em = pd.concat(em, ignore_index=True).drop_duplicates("pid", keep="last")
    df = prompts[keep].merge(sc.drop(columns=["set", "split"]), on="pid", how="left",
                             validate="1:1")
    df = df.merge(em, on="pid", how="left", validate="1:1")
    for c in COLS:
        if c not in df.columns:
            df[c] = np.nan
    df = df.sort_values("pid").reset_index(drop=True)
    is_pool = (df["set"] == P42K.POOL).to_numpy()
    edges = A.length_edges(df.loc[is_pool, "n_tokens"].to_numpy())
    df["len_bin"] = A.length_bin(df["n_tokens"].to_numpy(), edges)
    lc, stats, rho = {}, {}, {}
    ntok = df.loc[is_pool, "n_tokens"].to_numpy(float)
    for c in COLS:
        z, st = A.lc_zscore(df[c].to_numpy(float), df["len_bin"].to_numpy(), is_pool)
        lc[c + LC] = z
        stats[c] = st
    df = pd.concat([df, pd.DataFrame(lc, index=df.index)], axis=1)
    for c in COLS:
        r = {"len_raw": A.spearman(df.loc[is_pool, c], ntok),
             "len_lc": A.spearman(df.loc[is_pool, c + LC], ntok)}
        for nm in ("norm_L24", "norm_L16"):
            r[f"{nm}_raw"] = A.spearman(df.loc[is_pool, c], df.loc[is_pool, nm])
            r[f"{nm}_lc"] = A.spearman(df.loc[is_pool, c + LC], df.loc[is_pool, nm + LC])
        rho[c] = r
    active, active_why = active_candidates(sc, p.smoke)
    need = active + [S3]
    df["valid"] = np.isfinite(df[need].to_numpy(float)).all(axis=1)
    p.analysis.mkdir(parents=True, exist_ok=True)
    df.to_csv(p.prep_csv, index=False)
    out = {"n_rows": len(df), "n_by_set_split": A._counts(df), "len_edges": edges.tolist(),
           "rows_per_bin_pool": np.bincount(df.loc[is_pool, "len_bin"],
                                            minlength=P42K.N_LEN_BINS).tolist(),
           "active_candidates": active, "active_candidates_source": active_why,
           "valid_mask": {"rule": f"finite for every active candidate column and {S3}",
                          "n_valid": int(df["valid"].sum()),
                          "dropped_by_set": {s: int((~g["valid"]).sum())
                                             for s, g in df.groupby("set")}},
           "missing_by_set": {c: {s: int(g[c].isna().sum()) for s, g in df.groupby("set")}
                              for c in COLS},
           "s1_law2": ("joined from " + str(p.law2_emb)) if law2_s1 else
           "ABSENT: emb_knn is NaN for planted_law2 (results/embed_law2/emb_scores.csv missing)",
           "law2_rows": int((df["set"] == K.PLANTED_LAW2).sum()),
           "spearman_pool": rho, "bin_stats": stats,
           "prep_sha256": A.sha256_file(p.prep_csv),
           "inputs": {str(x): A.sha256_file(x) for x in (p.p42_prompts, p.law2, p.scores,
                                                       p.p42_emb, p.law2_emb) if x.is_file()}}
    A.write_json(p.analysis / "prep.json", out)
    return out


def load_prep(p: Paths) -> pd.DataFrame:
    if not p.prep_csv.is_file():
        raise FileNotFoundError(f"{p.prep_csv}: run `prep` first")
    df = pd.read_csv(p.prep_csv, low_memory=False)
    df["valid"] = df["valid"].astype(bool)
    return df


# --- selection --------------------------------------------------------------------------------------


def law2_failed(meta_path: Path) -> tuple[bool, str]:
    """True if the legal-matched pre-flight failed (data.meta.json "law2" -> "preflight": a
    final "passed" key wins, else "passes_automatic"); a missing record counts as failed."""
    if not Path(meta_path).is_file():
        return True, f"{meta_path} missing"
    m = json.loads(Path(meta_path).read_text()).get("law2")
    if not m:
        return True, "no law2 record in data.meta.json"
    pf = m.get("preflight", {})
    if "passed" in pf:
        return (not bool(pf["passed"])), f"preflight.passed = {pf['passed']}"
    return (not bool(pf.get("passes_automatic"))), \
        f"preflight.passes_automatic = {pf.get('passes_automatic')}"


def select(p: Paths, force: bool = False) -> dict:
    if p.selection.exists() and not force:
        raise FileExistsError(f"{p.selection} is frozen; pass --force to redo the selection")
    df = load_prep(p)
    failed, why = law2_failed(p.data_meta)
    cats = (P42K.PLANTED_MED,) if failed else K.PLANTED_EVAL
    active = json.loads((p.analysis / "prep.json").read_text())["active_candidates"]
    dev = df[(df["split"] == "dev") & df["valid"]]
    pool = dev[dev["set"] == P42K.POOL]
    aur, best, best_v = {}, None, -np.inf
    for c in active:
        a = {cat: A.auroc(dev.loc[dev["set"] == cat, c + LC], pool[c + LC]) for cat in cats}
        m = float(np.mean(list(a.values())))
        aur[c] = {**a, "mean": m}
        if np.isfinite(m) and m > best_v:  # strict: ties keep the earlier column
            best, best_v = c, m
    if best is None:
        raise ValueError("no candidate has a finite dev AUROC")
    out = {"split": "dev", "categories": list(cats), "law2_preflight_failed": failed,
           "law2_preflight_reason": why, "aurocs": aur, "best": best,
           "active_candidates": active,
           "n_dev": {s: int((dev["set"] == s).sum()) for s in (P42K.POOL, *cats)},
           "rule": "max mean length-controlled AUROC over the categories; ties -> earlier "
                   "in K.CANDIDATES", "prep_sha256": A.sha256_file(p.prep_csv)}
    A.write_json(p.selection, out)
    return out


def require_selection(p: Paths) -> dict:
    if not p.selection.is_file():
        raise RuntimeError(f"{p.selection} missing: run `select` before any test metric")
    sel = json.loads(p.selection.read_text())
    if sel.get("prep_sha256") != A.sha256_file(p.prep_csv):
        raise RuntimeError("selection.json was made on a different prep.csv")
    return sel


# --- metrics ------------------------------------------------------------------------------------------


def boot_draws(pools: dict[str, np.ndarray], plants: dict[str, np.ndarray], n_boot: int,
               rng: np.random.Generator) -> dict[str, dict[str, np.ndarray]]:
    """Paired bootstrap: per draw one resample of the pool rows (counts) and one of the planted
    rows (indices), shared by every scorer; precision uses the first m resampled planted rows.
    All columns must have the same pool and planted row counts (the common valid-row mask)."""
    cols = list(pools)
    n_pool = len(pools[cols[0]])
    n_pl = len(plants[cols[0]])
    if any(len(pools[c]) != n_pool or len(plants[c]) != n_pl for c in cols):
        raise ValueError("paired bootstrap needs the same rows for every scorer")
    ms = {k: A._m_for(v, n_pool) for k, v in PREV.items()}
    orders = {c: np.argsort(-np.nan_to_num(pools[c], nan=-np.inf), kind="stable") for c in cols}
    out = {c: {m: np.full(n_boot, np.nan) for m in METRICS} for c in cols}
    for b in range(n_boot):
        wpool = np.bincount(rng.integers(0, n_pool, n_pool), minlength=n_pool).astype(float)
        pidx = rng.integers(0, n_pl, n_pl)
        for c in cols:
            v, o = pools[c], orders[c]
            ok = ~np.isnan(v[o])
            vd, wd = v[o][ok], wpool[o][ok]
            pr = plants[c][pidx]
            pr = pr[~np.isnan(pr)]
            if not len(pr) or not len(vd):
                continue
            out[c]["auroc"][b] = A.auroc_sorted(vd[::-1], wd[::-1], pr, np.ones(len(pr)))
            for name, m in ms.items():
                sub = plants[c][pidx[:min(m, n_pl)]]
                sub = sub[~np.isnan(sub)]
                if len(sub):
                    out[c][name][b] = A.topk_hits(vd, wd, sub, m) / m
    return out


def boot_auroc_pairs(pos: dict[str, np.ndarray], neg: dict[str, np.ndarray], n_boot: int,
                     rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Paired bootstrap of AUROC(pos > neg) per column, same resamples for every column."""
    cols = list(pos)
    npos, nneg = len(pos[cols[0]]), len(neg[cols[0]])
    out = {c: np.full(n_boot, np.nan) for c in cols}
    if not npos or not nneg:
        return out
    for b in range(n_boot):
        wp = np.bincount(rng.integers(0, npos, npos), minlength=npos).astype(float)
        wn = np.bincount(rng.integers(0, nneg, nneg), minlength=nneg).astype(float)
        for c in cols:
            out[c][b] = A.auroc(pos[c], neg[c], wp, wn)
    return out


def _ratio(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b > 0, a / np.where(b > 0, b, 1), np.where(a > 0, np.inf, np.nan))


def auroc_outcome(est: float, lo: float, hi: float, margin: float = K.MARGIN) -> str:
    """Per category on ΔAUROC = best - S3, first match: better, worse, equivalent, unresolved."""
    if est >= margin and lo > 0:
        return "better"
    if est <= -margin and hi < 0:
        return "worse"
    if lo >= -margin and hi <= margin:
        return "equivalent"
    return "unresolved"


def decide(cats, auroc_delta: dict, prec: dict, clean_bench: float, winner: str,
           margin: float = K.MARGIN, ratio: float = K.PREC_RATIO,
           clean_min: float = K.CLEAN_BENCH_MIN) -> dict:
    """The plan's ordered T2 decision tree.

    auroc_delta[cat] = {"est", "ci95": [lo, hi]} for ΔAUROC (best - S3);
    prec[cat] = {"best": est, "s3": est, "delta": {"est", "ci95"}} for precision@1%;
    clean_bench = the winner's planted-medical vs id_heldout AUROC (test)."""
    p_ok = all(prec[c]["best"] >= ratio * prec[c]["s3"] and prec[c]["delta"]["ci95"][0] > 0
               for c in cats)
    precision_outcome = "better" if p_ok else "not_better"
    per = {c: auroc_outcome(auroc_delta[c]["est"], *auroc_delta[c]["ci95"], margin=margin)
           for c in cats}
    labs = set(per.values())
    study = labs.pop() if len(labs) == 1 else "mixed"
    clean_ok = bool(np.isfinite(clean_bench) and clean_bench >= clean_min)
    if study == "better" or precision_outcome == "better":
        head = "O1"
    elif study == "worse":
        head = "O3"
    elif study == "equivalent" and clean_ok:
        head = "O2"
    else:
        head = "O4"
    return {"precision_outcome": precision_outcome, "auroc_outcome_per_category": per,
            "auroc_outcome": study, "clean_benchmark": clean_bench,
            "clean_benchmark_reproduced": clean_ok, "headline": head, "winner": winner,
            "headline_names": {"O1": "improvement", "O2": "no gain", "O3": "worse",
                               "O4": "inconclusive or implementation-limited"}[head]}


def s3_reproduction(p: Paths, df: pd.DataFrame) -> dict:
    if not p.p42_scores.is_file():
        return {"skipped": f"{p.p42_scores} missing"}
    old = pd.read_csv(p.p42_scores, usecols=["pid", "maha_dpo_p_L24"])
    m = df[["pid", S3]].merge(old, on="pid", how="inner")
    rho = A.spearman(m[S3], m["maha_dpo_p_L24"])
    return {"n_shared": len(m), "spearman": rho, "gate": K.S3_REPRO_MIN_SPEARMAN,
            "passes": bool(np.isfinite(rho) and rho >= K.S3_REPRO_MIN_SPEARMAN)}


def evaluate(p: Paths, n_boot: int = K.N_BOOT, appx_subsets: int = APPX_SUBSETS,
             head_subsets: int = HEAD_SUBSETS) -> dict:
    sel = require_selection(p)
    df = load_prep(p)
    best = sel["best"]
    cats = list(sel["categories"])
    test = df[(df["split"] == "test") & df["valid"]].reset_index(drop=True)
    pool = test[test["set"] == P42K.POOL]
    held = test[test["set"] == P42K.ID_HELDOUT]
    cols = [c for c in EVAL_COLS if c in test.columns]
    n_pool = len(pool)
    out = {"definitions": {
        "split": "test", "rows": "common valid-row mask (finite for every candidate and S3)",
        "n_test_pool": n_pool, "n_test_heldout": len(held),
        "m": {k: A._m_for(v, n_pool) for k, v in PREV.items()},
        "point_estimate": "mean over random m-subsets of the test planted items, pool not "
                          f"resampled ({head_subsets} subsets for best and S3, {appx_subsets} "
                          "for other columns)",
        "bootstrap": f"{n_boot} draws, seed {K.SEED_BOOT}; pool and planted rows resampled with "
                     "replacement; identical resamples and planted subsets for every scorer",
        "scores": "length-controlled (__lc) unless marked raw"},
        "best": best, "categories": cats, "per_category": {}, "contrasts": {},
        "clean_benchmark": {}, "pool_vs_heldout": {}, "raw_points": {}}
    for ci_, cat in enumerate([*cats, *[c for c in REPORTED_CATS if c not in cats]]):
        pl = test[test["set"] == cat]
        if not len(pl):
            out["per_category"][cat] = {"skipped": "no test rows"}
            continue
        usable = [c for c in cols if pool[c + LC].notna().any() and pl[c + LC].notna().any()]
        skipped = sorted(set(cols) - set(usable))
        pools = {c: pool[c + LC].to_numpy(float) for c in usable}
        plants = {c: pl[c + LC].to_numpy(float) for c in usable}
        draws = boot_draws(pools, plants, n_boot, np.random.default_rng([K.SEED_BOOT, 10 + ci_]))
        rec = {}
        for j, c in enumerate(usable):
            nsub = head_subsets if c in (best, S3) else appx_subsets
            pt = A.m1_point(pools[c], plants[c], np.random.default_rng([K.SEED_BOOT, 20 + ci_, j]),
                            nsub)
            rec[c] = {"auroc": A.summarise(pt["auroc"], draws[c]["auroc"]),
                      "prec_1pct": A.summarise(pt["prec_1pct"], draws[c]["prec_1pct"]),
                      "prec_0.1pct": A.summarise(pt["prec_0.1pct"], draws[c]["prec_0.1pct"]),
                      "n_planted": len(pl), "m": {"1pct": pt["m_1pct"], "0.1pct": pt["m_0.1pct"]}}
            raw = A.m1_point(pool[c].to_numpy(float), pl[c].to_numpy(float),
                             np.random.default_rng([K.SEED_BOOT, 40 + ci_, j]), appx_subsets)
            out["raw_points"].setdefault(cat, {})[c] = raw
        out["per_category"][cat] = {"columns": rec, "skipped_columns": skipped,
                                    "n_planted": len(pl),
                                    "skipped_rule": "column with no finite score here"}
        if best in draws and S3 in draws:
            con = {}
            for m in METRICS:
                d = draws[best][m] - draws[S3][m]
                con["d_" + m] = A.summarise(rec[best][m]["est"] - rec[S3][m]["est"], d)
            con["ratio_prec_1pct"] = A.summarise(
                float(_ratio(rec[best]["prec_1pct"]["est"], rec[S3]["prec_1pct"]["est"])),
                _ratio(draws[best]["prec_1pct"], draws[S3]["prec_1pct"]))
            out["contrasts"][cat] = con
        # clean benchmark: planted vs test id_heldout
        pos = {c: pl[c + LC].to_numpy(float) for c in usable}
        neg = {c: held[c + LC].to_numpy(float) for c in usable}
        cd = boot_auroc_pairs(pos, neg, n_boot, np.random.default_rng([K.SEED_BOOT, 30 + ci_]))
        out["clean_benchmark"][cat] = {c: A.summarise(A.auroc(pos[c], neg[c]), cd[c])
                                       for c in usable}
    usable = [c for c in cols if pool[c + LC].notna().any() and held[c + LC].notna().any()]
    pos = {c: pool[c + LC].to_numpy(float) for c in usable}
    neg = {c: held[c + LC].to_numpy(float) for c in usable}
    pd_ = boot_auroc_pairs(pos, neg, n_boot, np.random.default_rng([K.SEED_BOOT, 50]))
    out["pool_vs_heldout"] = {c: A.summarise(A.auroc(pos[c], neg[c]), pd_[c]) for c in usable}
    ad = {c: {"est": out["contrasts"][c]["d_auroc"]["est"],
              "ci95": out["contrasts"][c]["d_auroc"]["ci95"]} for c in cats}
    pr = {c: {"best": out["per_category"][c]["columns"][best]["prec_1pct"]["est"],
              "s3": out["per_category"][c]["columns"][S3]["prec_1pct"]["est"],
              "delta": out["contrasts"][c]["d_prec_1pct"]} for c in cats}
    clean = out["clean_benchmark"].get(P42K.PLANTED_MED, {}).get(best, {}).get("est", np.nan)
    out["decision"] = decide(cats, ad, pr, clean, best)
    out["decision"]["clean_benchmark_definition"] = (
        "winner's planted_med vs test id_heldout AUROC (length-controlled), point estimate; "
        "per-category values in clean_benchmark")
    out["s3_reproduction"] = s3_reproduction(p, df)
    A.write_json(p.analysis / "t2.json", out)
    return out


# --- report -----------------------------------------------------------------------------------------


def _fmt(s: dict) -> str:
    if not s or not np.isfinite(s.get("est", np.nan)):
        return "n/a"
    lo, hi = s["ci95"]
    return f"{s['est']:.3f} [{lo:.3f}, {hi:.3f}]"


def report(p: Paths) -> Path:
    t2 = json.loads((p.analysis / "t2.json").read_text())
    best = t2["best"]
    d = t2["decision"]
    md = ["# P45 T2 summary\n",
          (f"Frozen dev winner: **{best}**. Headline **{d['headline']}** ({d['headline_names']}); "
           f"AUROC outcome {d['auroc_outcome']} ({d['auroc_outcome_per_category']}); precision "
           f"outcome {d['precision_outcome']}; clean benchmark {d['clean_benchmark']:.3f} "
           f"(reproduced: {d['clean_benchmark_reproduced']}).\n"),
          f"S3 reproduction: {t2['s3_reproduction']}.\n",
          "Length-controlled, test split, common valid rows; 95% paired-bootstrap intervals.\n"]
    for cat, rec in t2["per_category"].items():
        if "columns" not in rec:
            continue
        md += [f"\n## {cat} (n planted {rec['n_planted']})\n",
               "| column | AUROC vs pool | prec@1% | prec@0.1% | clean (vs id_heldout) |",
               "|---|---|---|---|---|"]
        for c, v in rec["columns"].items():
            cl = t2["clean_benchmark"].get(cat, {}).get(c, {})
            tag = " (best)" if c == best else (" (S3)" if c == S3 else "")
            md.append(f"| {c}{tag} | {_fmt(v['auroc'])} | {_fmt(v['prec_1pct'])} | "
                      f"{_fmt(v['prec_0.1pct'])} | {_fmt(cl)} |")
        if rec["skipped_columns"]:
            md.append(f"\nSkipped (NaN rows): {', '.join(rec['skipped_columns'])}")
        if cat in t2["contrasts"]:
            c = t2["contrasts"][cat]
            md.append(f"\nbest − S3: ΔAUROC {_fmt(c['d_auroc'])}; Δprec@1% "
                      f"{_fmt(c['d_prec_1pct'])}; Δprec@0.1% {_fmt(c['d_prec_0.1pct'])}; "
                      f"prec@1% ratio {_fmt(c['ratio_prec_1pct'])}")
    md += ["\n## Pool vs id_heldout AUROC (test)\n", "| column | AUROC |", "|---|---|"]
    md += [f"| {c} | {_fmt(v)} |" for c, v in t2["pool_vs_heldout"].items()]
    out = p.analysis / "t2_summary.md"
    out.write_text("\n".join(md) + "\n")
    try:
        _len_fig(p, best)
    except Exception as e:  # noqa: BLE001 - the figure is optional
        print(f"[report] figure skipped: {e}")
    return out


def _len_fig(p: Paths, best: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df = load_prep(p)
    pool = df[(df["set"] == P42K.POOL) & df["valid"]]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    for ax, suf, title in ((axes[0], "", "raw"), (axes[1], LC, "length-controlled")):
        for c in dict.fromkeys((best, S3, "norm_L24")):
            g = pool.groupby("len_bin")[c + suf].median()
            sd = g.std() if suf == "" else 1.0
            ax.plot(g.index, (g - g.mean()) / (sd or 1.0), marker="o", label=c)
        ax.set_xlabel("pool length decile")
        ax.set_ylabel("median score (standardised)")
        ax.set_title(title)
        ax.legend()
    fd = p.analysis / "figs"
    fd.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(fd / "len_vs_score.png", dpi=120)
    plt.close(fig)


# --- cli -----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m p45.analyze", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prep")
    s = sub.add_parser("select")
    s.add_argument("--force", action="store_true")
    e = sub.add_parser("evaluate")
    e.add_argument("--n-boot", type=int, default=K.N_BOOT)
    sub.add_parser("report")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    p = Paths()
    if a.cmd == "prep":
        out = prep(p)
        print(json.dumps({k: out[k] for k in ("n_rows", "valid_mask", "s1_law2")}, indent=1))
    elif a.cmd == "select":
        print(json.dumps(select(p, a.force), indent=1))
    elif a.cmd == "evaluate":
        out = evaluate(p, n_boot=a.n_boot)
        print(json.dumps({"decision": out["decision"], "s3": out["s3_reproduction"]}, indent=1))
    elif a.cmd == "report":
        print(report(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
