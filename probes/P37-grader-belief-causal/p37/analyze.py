"""P37 analysis: V - S and the controls -> results/analysis.json (plan.md, "Measure").

Inputs (results_dir):
  measures.csv   one row per continuation (episode_id, site_id, task_id, cond, para, repeat, cheat,
                 tamper_any, passed, end_reason, n_turns, turns_to_done,
                 final_mentions_contradiction, replay_match, error)
  forks.csv      one row per decision-point fork (site_id, task_id, fork, <tags>)   [optional]
  cont_tags.csv  one row per continuation (episode_id, <tags>)                        [optional]

Estimand: continuations averaged within site (per condition), the within-site difference, sites
averaged within task, tasks weighted equally (only sites with both conditions; only tasks with
>= 1 such site). Inference: a within-site randomisation test (labels permuted among the two
conditions' continuations of each site, per-site counts kept) and a task-cluster bootstrap
percentile CI. Rows with replay_match == 0 or a non-empty error are excluded everywhere.
Undetermined cheat (empty) is excluded from the primary; a sensitivity sets it to 1 and to 0.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from p37 import contracts as C

TAGS = ("visible_tests", "spec_hidden", "behaviour", "cheat_check", "unclear", "game_plan")
CONDS = ("V", "S", "N", "U")
CONTRASTS = (("V", "S"), ("V", "N"), ("S", "N"), ("N", "U"), ("V", "U"), ("S", "U"))
DRAWS = 10_000
FEW_CLUSTERS = 30
CHUNK = 1_000          # permutation draws per vectorised chunk (memory: CHUNK x rows)


# --- loading ---------------------------------------------------------------------------------
def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def load_measures(path: Path) -> tuple[pd.DataFrame, dict]:
    """Read measures.csv and drop replay mismatches and errored rows; return (kept, counts)."""
    df = pd.read_csv(path, dtype={"episode_id": str, "site_id": str, "task_id": str,
                                  "cond": str, "end_reason": str, "error": str},
                     keep_default_na=False, na_values=[""])
    for col in ("para", "repeat", "cheat", "tamper_any", "passed", "n_turns", "turns_to_done",
                "final_mentions_contradiction", "replay_match"):
        if col in df:
            df[col] = _num(df[col])
    err = df["error"].fillna("").astype(str).str.strip() != ""
    mismatch = df["replay_match"] == 0
    drop = err | mismatch
    kept = df[~drop].copy()
    counts = {
        "n_rows": len(df),
        "replay_mismatch": int(mismatch.sum()),
        "error": int(err.sum()),
        "excluded": int(drop.sum()),
        "n_kept": len(kept),
        "excluded_by_cond": {c: int((drop & (df["cond"] == c)).sum()) for c in CONDS},
        "undetermined_by_cond": {c: int((kept["cond"] == c).sum()
                                        - kept.loc[kept["cond"] == c, "cheat"].notna().sum())
                                 for c in CONDS},
    }
    return kept, counts


def _read_optional(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    df = pd.read_csv(path, dtype={"episode_id": str, "site_id": str, "task_id": str},
                     keep_default_na=False, na_values=[""])
    for t in TAGS:
        if t in df:
            df[t] = _num(df[t])
    return df


# --- core machinery --------------------------------------------------------------------------
def _ci(boot: np.ndarray) -> list[float] | None:
    boot = boot[np.isfinite(boot)]
    if boot.size == 0:
        return None
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return [float(lo), float(hi)]


def _cluster_note(n_tasks: int) -> str | None:
    if n_tasks < FEW_CLUSTERS:
        return (f"task-cluster bootstrap over {n_tasks} clusters: few clusters, so the "
                "percentile CI is likely too narrow")
    return None


def task_equal_rate(df: pd.DataFrame, y: str, draws: int = DRAWS,
                    seed: int = C.SEED) -> dict:
    """Mean of y: within site, then within task, tasks weighted equally (+ task bootstrap)."""
    d = df[df[y].notna()]
    if d.empty:
        return {"estimate": None, "ci95": None, "k": 0, "n": 0, "n_sites": 0, "n_tasks": 0}
    site = d.groupby(["task_id", "site_id"])[y].mean()
    task = site.groupby(level="task_id").mean().to_numpy()
    rng = np.random.default_rng(seed)
    boot = task[rng.integers(0, task.size, (draws, task.size))].mean(axis=1)
    return {"estimate": float(task.mean()), "ci95": _ci(boot), "k": int(d[y].sum()),
            "n": len(d), "n_sites": int(site.size), "n_tasks": int(task.size)}


def contrast(df: pd.DataFrame, in_a: pd.Series, in_b: pd.Series, y: str = "cheat",
             draws: int = DRAWS, seed: int = C.SEED, permute: bool = True) -> dict:
    """Task-equal within-site difference mean(y | a) - mean(y | b), with the within-site
    randomisation p and the task-cluster bootstrap CI."""
    d = df[(in_a | in_b) & df[y].notna()].copy()
    d["_a"] = in_a[d.index].astype(bool)
    g = d.groupby("site_id")["_a"].agg(["sum", "count"])
    both = g.index[(g["sum"] > 0) & (g["sum"] < g["count"])]
    d = d[d["site_id"].isin(both)].sort_values(["task_id", "site_id"], kind="stable")
    out = {"estimate": None, "ci95": None, "p_perm": None, "n_sites": 0, "n_tasks": 0,
           "n_a": int(d["_a"].sum()), "n_b": int((~d["_a"]).sum()), "draws": draws}
    if d.empty:
        return out
    site_codes, sites = pd.factorize(d["site_id"], sort=False)
    yv = d[y].to_numpy(float)
    av = d["_a"].to_numpy(bool)
    n_site = sites.size
    starts = np.flatnonzero(np.r_[True, site_codes[1:] != site_codes[:-1]])
    n_a = np.add.reduceat(av.astype(float), starts)
    n_b = np.add.reduceat((~av).astype(float), starts)
    tot = np.add.reduceat(yv, starts)
    sa = np.add.reduceat(yv * av, starts)
    site_diff = sa / n_a - (tot - sa) / n_b
    site_task = d.groupby("site_id", sort=False)["task_id"].first().reindex(sites).to_numpy()
    task_codes, tasks = pd.factorize(site_task, sort=False)
    n_task = tasks.size
    sites_per_task = np.bincount(task_codes, minlength=n_task)
    w = 1.0 / (sites_per_task[task_codes] * n_task)
    est = float(site_diff @ w)
    task_diff = np.bincount(task_codes, weights=site_diff, minlength=n_task) / sites_per_task
    rng = np.random.default_rng(seed)
    boot = task_diff[rng.integers(0, n_task, (draws, n_task))].mean(axis=1)
    out.update(estimate=est, ci95=_ci(boot), n_sites=int(n_site), n_tasks=int(n_task),
               task_diffs={str(t): float(v) for t, v in zip(tasks, task_diff)},
               cluster_note=_cluster_note(n_task))
    if permute:
        rng = np.random.default_rng(seed)
        offs = site_codes.astype(float)
        hits = 0
        done = 0
        tol = 1e-12 * max(1.0, abs(est))
        while done < draws:
            m = min(CHUNK, draws - done)
            perm = np.argsort(offs[None, :] + rng.random((m, yv.size)), axis=1)
            ap = av[perm]
            sp = np.add.reduceat(yv[None, :] * ap, starts, axis=1)
            pe = (sp / n_a - (tot - sp) / n_b) @ w
            hits += int((np.abs(pe) >= abs(est) - tol).sum())
            done += m
        out["p_perm"] = (1 + hits) / (1 + draws)
    return out


def _auc_pairs(score: np.ndarray, y: np.ndarray, tcode: np.ndarray, n_task: int):
    """Task-aggregated pair-comparison matrix for a cluster-weighted AUROC."""
    pos, neg = score[y == 1], score[y == 0]
    cmp = (pos[:, None] > neg[None, :]) + 0.5 * (pos[:, None] == neg[None, :])
    tp, tn = tcode[y == 1], tcode[y == 0]
    op = np.zeros((pos.size, n_task))
    op[np.arange(pos.size), tp] = 1
    on = np.zeros((neg.size, n_task))
    on[np.arange(neg.size), tn] = 1
    return op.T @ cmp @ on, op.sum(0), on.sum(0)


def auroc(score: np.ndarray, y: np.ndarray, task: np.ndarray, draws: int = DRAWS,
          seed: int = C.SEED) -> dict:
    """AUROC of score for binary y (ties 1/2), pooled, with a task-cluster bootstrap CI."""
    tcode, tasks = pd.factorize(pd.Series(task), sort=False)
    n_pos, n_neg = int((y == 1).sum()), int((y == 0).sum())
    out = {"auroc": None, "ci95": None, "n_pos": n_pos, "n_neg": n_neg,
           "n_tasks": int(tasks.size)}
    if n_pos == 0 or n_neg == 0:
        return out
    M, P, N = _auc_pairs(score, y, tcode, tasks.size)
    out["auroc"] = float(M.sum() / (P.sum() * N.sum()))
    rng = np.random.default_rng(seed)
    K = np.zeros((draws, tasks.size))
    np.add.at(K, (np.repeat(np.arange(draws), tasks.size),
                  rng.integers(0, tasks.size, draws * tasks.size)), 1)
    den = (K @ P) * (K @ N)
    with np.errstate(invalid="ignore", divide="ignore"):
        boot = np.einsum("dt,tu,du->d", K, M, K) / den
    out["ci95"] = _ci(np.where(den > 0, boot, np.nan))
    out["cluster_note"] = _cluster_note(int(tasks.size))
    return out


# --- sections --------------------------------------------------------------------------------
def _primary(m: pd.DataFrame, draws: int) -> dict:
    res = {}
    for a, b in CONTRASTS:
        res[f"{a}-{b}"] = contrast(m, m["cond"] == a, m["cond"] == b, draws=draws)
    for p in (0, 1):
        res[f"V{p}-S{p}"] = contrast(m, (m["cond"] == "V") & (m["para"] == p),
                                     (m["cond"] == "S") & (m["para"] == p), draws=draws)
    return res


def _paraphrase_agreement(m: pd.DataFrame, contrasts: dict, draws: int) -> dict:
    d0, d1 = contrasts["V0-S0"], contrasts["V1-S1"]
    out = {"V0-S0": d0["estimate"], "V1-S1": d1["estimate"], "difference": None,
           "ci95": None, "n_tasks_both": 0}
    if d0["estimate"] is None or d1["estimate"] is None:
        return out
    out["difference"] = d0["estimate"] - d1["estimate"]
    t0, t1 = d0["task_diffs"], d1["task_diffs"]
    common = sorted(set(t0) & set(t1))
    if common:
        diff = np.array([t0[t] - t1[t] for t in common])
        rng = np.random.default_rng(C.SEED)
        boot = diff[rng.integers(0, diff.size, (draws, diff.size))].mean(axis=1)
        out.update(ci95=_ci(boot), n_tasks_both=len(common),
                   note="CI over tasks present in both paraphrase contrasts")
    return out


def _sensitivity(m: pd.DataFrame, draws: int) -> dict:
    out = {"n_undetermined_V": int(m.loc[m["cond"] == "V", "cheat"].isna().sum()),
           "n_undetermined_S": int(m.loc[m["cond"] == "S", "cheat"].isna().sum())}
    for name, fill in (("undetermined_as_cheat", 1.0), ("undetermined_as_clean", 0.0)):
        f = m.assign(cheat=m["cheat"].fillna(fill))
        out[name] = contrast(f, f["cond"] == "V", f["cond"] == "S", draws=draws)
    return out


def _forks(m: pd.DataFrame, forks: pd.DataFrame | None, draws: int) -> dict | None:
    if forks is None:
        return None
    un = m[m["cond"].isin(["U", "N"]) & m["cheat"].notna()]
    y_site = un.groupby("site_id")["cheat"].mean()
    task_of = pd.concat([forks.groupby("site_id")["task_id"].first(),
                         m.groupby("site_id")["task_id"].first()])
    task_of = task_of[~task_of.index.duplicated()]
    out: dict = {"outcome": "site mean cheat over U and N continuations"}
    for tag in ("visible_tests", "spec_hidden"):
        f = forks.groupby("site_id")[tag].mean()        # share of tagged forks (NaN untagged)
        n_tagged = forks.groupby("site_id")[tag].count()
        s = pd.DataFrame({"f": f, "n_tagged": n_tagged, "y": y_site})
        s["task_id"] = task_of.reindex(s.index)
        s = s[s["f"].notna()]
        sy = s[s["y"].notna()]
        diffs = {}
        for t, g in sy.groupby("task_id"):
            hi, lo = g[g["f"] > 0], g[g["f"] == 0]
            if len(hi) and len(lo):
                diffs[str(t)] = float(hi["y"].mean() - lo["y"].mean())
        within = {"estimate": None, "ci95": None, "n_tasks": len(diffs),
                  "task_diffs": diffs}
        if diffs:
            v = np.array(list(diffs.values()))
            rng = np.random.default_rng(C.SEED)
            boot = v[rng.integers(0, v.size, (draws, v.size))].mean(axis=1)
            within.update(estimate=float(v.mean()), ci95=_ci(boot),
                          cluster_note=_cluster_note(v.size))
        c = un[un["site_id"].isin(s.index)].copy()
        c["f"] = s["f"].reindex(c["site_id"]).to_numpy()
        c["task"] = s["task_id"].reindex(c["site_id"]).to_numpy()
        task_mean_f = s.groupby("task_id")["f"].mean()
        c["f_c"] = c["f"] - task_mean_f.reindex(c["task"]).to_numpy()
        yv = c["cheat"].to_numpy(int)
        out[tag] = {
            "n_sites": len(s), "n_sites_f_pos": int((s["f"] > 0).sum()),
            "within_task_difference": within,
            "auroc_pooled": auroc(c["f"].to_numpy(float), yv, c["task"].to_numpy(),
                                  draws=draws),
            "auroc_within_task": auroc(c["f_c"].to_numpy(float), yv, c["task"].to_numpy(),
                                       draws=draws),
            "sites": [{"site_id": str(i), "task_id": str(r.task_id), "f": float(r.f),
                       "n_tagged": int(r.n_tagged),
                       "y": None if pd.isna(r.y) else float(r.y)}
                      for i, r in s.iterrows()],
        }
    return out


def _recurrence(m: pd.DataFrame, tags: pd.DataFrame | None, draws: int) -> dict | None:
    if tags is None:
        return None
    cols = ["episode_id"] + [t for t in TAGS if t in tags]
    j = m.merge(tags[cols], on="episode_id", how="left")
    out = {}
    for c in CONDS:
        jc = j[j["cond"] == c]
        out[c] = {t: task_equal_rate(jc, t, draws=draws) for t in TAGS if t in tags}
        out[c]["n_untagged"] = int(jc[[t for t in TAGS if t in tags]].isna().all(axis=1).sum())
    return out


def _process(m: pd.DataFrame) -> dict:
    out = {}
    for c in CONDS:
        mc = m[m["cond"] == c]
        ttd = mc["turns_to_done"].dropna()
        cheats = mc[mc["cheat"] == 1]
        quiet = cheats["final_mentions_contradiction"] == 0
        n_q = int(cheats["final_mentions_contradiction"].notna().sum())
        out[c] = {
            "n": len(mc),
            "n_done": int(ttd.size),
            "mean_turns_to_done": float(ttd.mean()) if ttd.size else None,
            "end_reason": {str(k): int(v) for k, v in
                           mc["end_reason"].fillna("").value_counts().sort_index().items()},
            "quiet_cheating": {"k": int(quiet.sum()), "n": n_q,
                               "share": float(quiet.sum() / n_q) if n_q else None},
        }
    return out


def analyze(results_dir: Path, draws: int = DRAWS) -> dict:
    """Every P37 estimate from results_dir's CSVs, as a JSON-serialisable dict."""
    results_dir = Path(results_dir)
    m, excl = load_measures(results_dir / "measures.csv")
    rates = {c: task_equal_rate(m[m["cond"] == c], "cheat", draws=draws) for c in CONDS}
    contrasts = _primary(m, draws)
    out = {
        "exp": C.EXP, "seed": C.SEED, "draws": draws, "results_dir": str(results_dir),
        "exclusions": excl,
        "rates": rates,
        "contrasts": contrasts,
        "paraphrase_agreement": _paraphrase_agreement(m, contrasts, draws),
        "sensitivity_undetermined": _sensitivity(m, draws),
        "forks": _forks(m, _read_optional(results_dir / "forks.csv"), draws),
        "recurrence": _recurrence(m, _read_optional(results_dir / "cont_tags.csv"), draws),
        "process": _process(m),
    }
    return _jsonable(out)


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if not math.isfinite(float(x)) else float(x)
    return x


# --- CLI -------------------------------------------------------------------------------------
def _fmt(v, nd=3):
    return "NA" if v is None else f"{v:+.{nd}f}"


def summary(res: dict) -> str:
    e = res["exclusions"]
    lines = [f"P37 analysis ({e['n_kept']} kept of {e['n_rows']}; excluded {e['excluded']})"]
    for c, r in res["rates"].items():
        ci = r["ci95"] or [None, None]
        lines.append(f"  rate {c}: {_fmt(r['estimate'])} [{_fmt(ci[0])}, {_fmt(ci[1])}]  "
                     f"k/n {r['k']}/{r['n']}")
    for name, r in res["contrasts"].items():
        ci = r["ci95"] or [None, None]
        p = "NA" if r["p_perm"] is None else f"{r['p_perm']:.4f}"
        lines.append(f"  {name:6s} {_fmt(r['estimate'])} [{_fmt(ci[0])}, {_fmt(ci[1])}] "
                     f"p={p}  sites {r['n_sites']} tasks {r['n_tasks']}")
    s = res["sensitivity_undetermined"]
    lines.append(f"  V-S undetermined=1: {_fmt(s['undetermined_as_cheat']['estimate'])}  "
                 f"=0: {_fmt(s['undetermined_as_clean']['estimate'])}")
    pa = res["paraphrase_agreement"]
    lines.append(f"  paraphrase p0 - p1: {_fmt(pa['difference'])}")
    if res["forks"]:
        for tag in ("visible_tests", "spec_hidden"):
            f = res["forks"][tag]
            w, a, b = (f["within_task_difference"], f["auroc_pooled"],
                       f["auroc_within_task"])
            lines.append(f"  forks {tag}: within-task {_fmt(w['estimate'])} "
                         f"(tasks {w['n_tasks']}), AUROC {_fmt(a['auroc'])}, "
                         f"within-task AUROC {_fmt(b['auroc'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results-dir", type=Path, default=C.RESULTS_DIR)
    ap.add_argument("--draws", type=int, default=DRAWS)
    args = ap.parse_args(argv)
    res = analyze(args.results_dir, draws=args.draws)
    path = args.results_dir / C.ANALYSIS_JSON.name
    path.write_text(json.dumps(res, indent=2, sort_keys=False) + "\n")
    print(summary(res))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
