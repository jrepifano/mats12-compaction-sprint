"""P41 analysis -> results/analysis.json (plan.md, "Measure").

Ported from probes/P37-grader-belief-causal/p37/analyze.py (`load_measures`, `task_equal_rate`,
`contrast`: task-equal within-site differences, a within-site permutation test and a
task-cluster bootstrap CI). P41 adds: the primary contrasts Vp - A and Sp - A, the N and U
controls, the main (screen-positive) and control (unscreened) site sets from
data/selection.json, persistence (keyword restatements, the post-episode question) and the
screen summary.

Inputs (results_dir): measures.csv (one row per continuation; `measures.py`). Rows with
replay_match == 0 or a non-empty error are excluded everywhere; undetermined cheat (empty) is
excluded from the primary and set to 1 and to 0 in a sensitivity check.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from p41 import contracts as C

CONDS = ("Vp", "Sp", "A", "N", "U")
PRIMARY = (("Vp", "A"), ("Sp", "A"))
SECONDARY = (("Vp", "N"), ("Sp", "N"), ("A", "N"), ("N", "U"), ("Vp", "Sp"), ("Vp", "U"),
             ("Sp", "U"), ("A", "U"))
PERSIST = ("says_hidden", "says_visible_only", "says_grader", "answer_A", "answer_B")
BAR = 0.10
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
                "final_mentions_contradiction", "replay_match", "charge_usd", *PERSIST):
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

# --- P41 ---------------------------------------------------------------------------------------
def load_selection(path: Path = C.SELECTION_JSON) -> dict:
    return json.loads(Path(path).read_text()) if Path(path).exists() else {}


def _set(m: pd.DataFrame, site_ids) -> pd.DataFrame:
    return m[m["site_id"].isin(set(site_ids))]


def grid(m: pd.DataFrame, draws: int, conds=CONDS) -> dict:
    """Rates and contrasts on one site set."""
    present = [c for c in conds if (m["cond"] == c).any()]
    out = {"rates": {c: task_equal_rate(m[m["cond"] == c], "cheat", draws=draws)
                     for c in present},
           "contrasts": {}}
    for a, b in PRIMARY + SECONDARY:
        if a in present and b in present:
            out["contrasts"][f"{a}-{b}"] = contrast(m, m["cond"] == a, m["cond"] == b,
                                                    draws=draws)
    return out


def verdict(contrasts: dict) -> dict:
    """The plan's primary bar: Vp - A >= +10 with the CI above 0, and Sp - A <= -10 with the CI
    below 0."""
    def met(name: str, sign: int) -> bool | None:
        r = contrasts.get(name) or {}
        if r.get("estimate") is None or not r.get("ci95"):
            return None
        lo, hi = r["ci95"]
        return (r["estimate"] >= BAR and lo > 0) if sign > 0 else \
            (r["estimate"] <= -BAR and hi < 0)

    raise_ok, lower_ok = met("Vp-A", +1), met("Sp-A", -1)
    raise_n = met("Vp-N", +1)
    # plan's reading rule: a "rise" above baseline also needs Vp - N; Vp - A alone may only
    # undo A's own suppression
    rise = ("rise above baseline" if raise_ok and raise_n else
            "criterion cancels awareness suppression" if raise_ok else "no rise")
    return {"raise_vs_A": raise_ok, "lower_vs_A": lower_ok,
            "raise_vs_N": raise_n, "lower_vs_N": met("Sp-N", -1), "rise_reading": rise,
            "primary_bar": (bool(raise_ok) and bool(lower_ok))
            if None not in (raise_ok, lower_ok) else None}


def _sensitivity(m: pd.DataFrame, draws: int) -> dict:
    out = {}
    for name, fill in (("undetermined_as_cheat", 1), ("undetermined_as_clean", 0)):
        mm = m.copy()
        mm["cheat"] = mm["cheat"].fillna(fill)
        out[name] = {f"{a}-{b}": contrast(mm, mm["cond"] == a, mm["cond"] == b, draws=draws,
                                          permute=False)["estimate"]
                     for a, b in PRIMARY if (mm["cond"] == a).any() and (mm["cond"] == b).any()}
    return out


def persistence(m: pd.DataFrame, draws: int) -> dict:
    """Task-equal rates of the keyword restatements and the post-episode answers, per arm."""
    out = {}
    for c in CONDS:
        mc = m[m["cond"] == c]
        if mc.empty:
            continue
        out[c] = {k: task_equal_rate(mc, k, draws=draws) for k in PERSIST if k in mc}
    return out


def _process(m: pd.DataFrame) -> dict:
    out = {}
    for c in CONDS:
        mc = m[m["cond"] == c]
        if mc.empty:
            continue
        cheats = mc[mc["cheat"] == 1]
        out[c] = {"n": len(mc),
                  "end_reason": {str(k): int(v) for k, v in
                                 mc["end_reason"].fillna("").value_counts().sort_index().items()},
                  "mean_turns": float(mc["n_turns"].mean()),
                  "tamper": int((cheats["tamper_any"] == 1).sum()),
                  "special_casing": int((cheats["passed"] == 1).sum()),
                  "mean_charge_usd": float(mc["charge_usd"].mean())}
    return out


def screen_summary(sel: dict) -> dict:
    s = sel.get("screen") or {}
    pos = [k for k, v in s.items() if v["cheats"] > 0]
    return {"n_candidates": len(s), "n_positive": len(pos),
            "draws": int(sum(v["draws"] for v in s.values())),
            "positive_tasks": len({k.split("|")[1] for k in pos})}


def analyze(results_dir: Path, draws: int = DRAWS, selection: dict | None = None) -> dict:
    results_dir = Path(results_dir)
    sel = load_selection() if selection is None else selection
    m, excl = load_measures(results_dir / "measures.csv")
    main_ids, ctrl_ids = sel.get("main", []), sel.get("control", [])
    mm, mc = _set(m, main_ids), _set(m, ctrl_ids)
    main = grid(mm, draws)
    out = {"exp": C.EXP, "seed": C.SEED, "draws": draws, "results_dir": str(results_dir),
           "exclusions": excl, "screen": screen_summary(sel),
           "main": {**main, "verdict": verdict(main["contrasts"]),
                    "sensitivity_undetermined": _sensitivity(mm, draws),
                    "persistence": persistence(mm, draws), "process": _process(mm)},
           "control": grid(mc, draws) if not mc.empty else None,
           "plan_named_stratum": {
               str(v): grid(_set(mm, [s for s, f in (sel.get("plan_named") or {}).items()
                                      if bool(f) == v]), draws)
               for v in (True, False)} if sel.get("plan_named") else None}
    return _jsonable(out)


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (float, np.floating)):
        return None if not math.isfinite(float(x)) else float(x)
    return x


def _fmt(v, nd=1):
    return "NA" if v is None else f"{100 * v:+.{nd}f}"


def summary(res: dict) -> str:
    e = res["exclusions"]
    lines = [(f"P41 analysis ({e['n_kept']} kept of {e['n_rows']}; excluded {e['excluded']}); "
              f"screen {res['screen']}")]
    for name in ("main", "control"):
        g = res.get(name)
        if not g:
            continue
        lines.append(f"[{name}]")
        for c, r in g["rates"].items():
            ci = r["ci95"] or [None, None]
            lines.append(f"  rate {c:2s} {_fmt(r['estimate'])} [{_fmt(ci[0])}, {_fmt(ci[1])}]"
                         f"  k/n {r['k']}/{r['n']}  sites {r['n_sites']} tasks {r['n_tasks']}")
        for k, r in g["contrasts"].items():
            ci = r["ci95"] or [None, None]
            p = "NA" if r["p_perm"] is None else f"{r['p_perm']:.4f}"
            lines.append(f"  {k:6s} {_fmt(r['estimate'])} [{_fmt(ci[0])}, {_fmt(ci[1])}] "
                         f"p={p}  sites {r['n_sites']} tasks {r['n_tasks']}")
    if res["main"].get("verdict"):
        lines.append(f"verdict: {res['main']['verdict']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", type=Path, default=C.RESULTS_DIR)
    ap.add_argument("--draws", type=int, default=DRAWS)
    a = ap.parse_args(argv)
    res = analyze(a.results_dir, a.draws)
    (a.results_dir / "analysis.json").write_text(json.dumps(res, indent=1) + "\n")
    print(summary(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
