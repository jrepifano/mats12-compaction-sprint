"""P47 power simulation for C2 (plan.md, "Automatic rules", "Power simulation"; specification
fixed in the plan before Stage B).

Generative model (exactly the plan's):
  site cheat propensity logit = mu + task effect (N(0, sigma_t^2)) + site effect s,
  s = a + b with a ~ N(0, f sigma_s^2) and b ~ N(0, (1 - f) sigma_s^2) independent;
  black-box score = a (no noise, which favours the baseline);
  probe score = b + eps with eps ~ N(0, (1 - f) sigma_s^2) (reliability 0.5);
  no scorer sees the task effect. Each site's draws are Bernoulli(logistic(propensity)).

Calibration: f is the value on the 0.01 grid in [0, 1] whose large-sample AUROC gap, between
the logistic model on (a, probe score) and the logistic model on a, is closest to +0.05. The
large sample is 200,000 draws, each with its own task and site effect (clustering does not
change a draw-level AUROC's expectation), with the same random numbers at every f so the gap is
smooth in f; both models are fitted in-sample (no overfitting at that n). The baseline AUROC is
recorded, not targeted. If the largest gap on the grid is below +0.05, f = 0 is used, delta_min
is computed there (the largest attainable gap; the f with the largest gap is recorded too) and
C2 is labelled underpowered by construction.

delta_min (Delta = held-out log-loss(baseline: a) - held-out log-loss(full: a + probe), with
p47.analysis.nested_cv_logloss, the c2 machinery: task weights, the same L2 grid, in-fold
standardisation), at the f calibrated for +0.05. Two versions are always computed and recorded;
--delta-min-at picks the one the verdicts use:
  - "planned" (default; coordinator's change, 2026-10-14, after the first run showed that the
    small-sample held-out Delta sits below its large-sample median, so "beyond" was nearly
    unreachable by construction): the median Delta over 2,000 datasets simulated at the PLANNED
    counts (after the talk-rate filter), with c2's own nested leave-one-task-out CV and no
    bootstrap. Datasets short of c2's minimum counts are left out (their share is recorded).
  - "large" (the plan's text): the median over 2,000 "large-sample datasets" of 300 tasks x 2
    sites x 6 draws (3,600 draws). Approximation: leave-one-task-out at 300 tasks (~45,000 inner
    fits per model) is too slow for 2,000 datasets, so the folds are 10 groups of whole tasks
    (task index mod 10); at 300 tasks the penalty choice barely matters.

--gaps (e.g. 0.05,0.10,0.15,0.20): for each target large-sample AUROC gap, f is calibrated the
same way, the planned-count delta_min at that gap is recorded, and the probability of each c2
verdict is estimated with delta_min held at the +0.05 value (the plan's effect of interest), so
the output shows how power grows with the true gap. A gap the grid cannot reach is marked
unattainable and skipped (only +0.05 uses the plan's f = 0 fallback). +0.05 is always included.

Power = the share of datasets at the planned counts (tasks x sites per task x draws, after
dropping each site independently with probability --talk-rate, the Stage A share of sites with
grader talk in the prefix) on which p47.analysis.c2 returns "beyond black box" (CI low > 0 and
Delta >= delta_min). The baseline is the black-box score alone (asking noise is not simulated;
the black-box score is exact). Cheaper approximation, stated in the output: by default 200
datasets with 500 bootstrap replicates each (the plan's 2,000 x 10,000 would take days); the
Monte Carlo SE of the power estimate is reported. Datasets short of c2's minimum counts count
as not reaching "beyond" (their share is reported).

Parameters: --mu --sigma-task --sigma-site, or --fit-from a sites CSV (task_id, site_id,
n_draws, n_cheats), fitted by maximum likelihood to a binomial-logit-normal two-level model
(nested Gauss-Hermite quadrature); sigma_s is floored at 0.5 (the floor is recorded).

  uv run python design/power_sim.py --fit-from data/<slug>/stageA_sites.csv \
      --tasks 30 --sites-per-task 2 --draws 6 --talk-rate 0.3 --out results/<slug>/power.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import expit, gammaln, logsumexp

PROBE_DIR = Path(__file__).resolve().parents[1]
if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

from p47 import analysis as A

SEED = 20261014
TARGET_GAP = 0.05
F_GRID = np.round(np.arange(0, 101) / 100, 2)
SIGMA_S_FLOOR = 0.5
LARGE = {"tasks": 300, "sites_per_task": 2, "draws": 6, "folds": 10}


# --------------------------------------------------------------------------------------------
# simulation
# --------------------------------------------------------------------------------------------
def simulate(rng, mu, sigma_t, sigma_s, f, n_tasks, sites_per_task, draws, talk_rate=0.0):
    """Site-level arrays for one dataset: task index, black-box score a, probe score, draws,
    cheats. Sites with grader talk (probability talk_rate, independent of everything) are
    dropped, as C2 keeps only no-talk sites."""
    n = n_tasks * sites_per_task
    task = np.repeat(np.arange(n_tasks), sites_per_task)
    te = rng.normal(0, sigma_t, n_tasks)[task]
    a = rng.normal(0, np.sqrt(f) * sigma_s, n)
    b = rng.normal(0, np.sqrt(1 - f) * sigma_s, n)
    probe = b + rng.normal(0, np.sqrt(1 - f) * sigma_s, n)
    k = rng.binomial(draws, expit(mu + te + a + b))
    keep = rng.random(n) >= talk_rate
    return {"task": task[keep], "a": a[keep], "probe": probe[keep],
            "m": np.full(keep.sum(), float(draws)), "k": k[keep].astype(float)}


def to_frame(sim) -> pd.DataFrame:
    """Draw-level rows (what c2 takes)."""
    rep = sim["m"].astype(int)
    site = np.repeat(np.arange(len(rep)), rep)
    cheat = np.concatenate([np.r_[np.ones(int(k)), np.zeros(int(m - k))]
                            for k, m in zip(sim["k"], sim["m"], strict=True)])
    return pd.DataFrame({"task_id": [f"t{t}" for t in sim["task"][site]],
                         "site_id": [f"s{s}" for s in site], "cheat": cheat.astype(int),
                         "bb": sim["a"][site], "probe": sim["probe"][site]})


# --------------------------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------------------------
def _crn(seed, n):
    rng = np.random.default_rng(seed)
    return {k: rng.standard_normal(n) for k in ("t", "a", "b", "e")} | {"u": rng.random(n)}


def large_sample_gap(f, mu, sigma_t, sigma_s, crn) -> dict:
    """In-sample AUROCs at one f on the common random numbers `crn`."""
    a = np.sqrt(f) * sigma_s * crn["a"]
    b = np.sqrt(1 - f) * sigma_s * crn["b"]
    probe = b + np.sqrt(1 - f) * sigma_s * crn["e"]
    y = (crn["u"] < expit(mu + sigma_t * crn["t"] + a + b)).astype(float)
    X1 = np.c_[np.ones(len(y)), a, probe]
    beta = A.fit_logistic_batch(X1, y, np.ones((1, len(y))), np.array([1e-8]),
                                np.ones((1, 3)))[0]
    auc_base = A.auroc(a, y)
    auc_full = A.auroc(X1 @ beta, y)
    return {"f": float(f), "auc_base": auc_base, "auc_full": auc_full,
            "gap": auc_full - auc_base, "cheat_rate": float(y.mean())}


def gap_grid(mu, sigma_t, sigma_s, *, n_large=200_000, seed=SEED, grid=F_GRID) -> list[dict]:
    crn = _crn(seed, n_large)
    return [large_sample_gap(f, mu, sigma_t, sigma_s, crn) for f in grid]


def pick_f(rows: list[dict], target: float) -> dict:
    """The grid f whose gap is closest to `target`; if no f reaches it, f = 0 and the result is
    flagged underpowered_by_construction (the plan's fallback)."""
    f = np.array([r["f"] for r in rows])
    gaps = np.array([r["gap"] for r in rows])
    best = int(np.argmax(gaps))
    if gaps.max() < target:
        pick, underpowered = int(np.flatnonzero(f == 0)[0]), True
    else:
        pick, underpowered = int(np.argmin(np.abs(gaps - target))), False
    return {"f": float(f[pick]), "gap": rows[pick]["gap"],
            "auc_base": rows[pick]["auc_base"], "auc_full": rows[pick]["auc_full"],
            "underpowered_by_construction": underpowered,
            "max_gap": float(gaps.max()), "f_at_max_gap": float(f[best]), "target_gap": target}


def calibrate(mu, sigma_t, sigma_s, *, n_large=200_000, seed=SEED, target=TARGET_GAP,
              grid=F_GRID) -> dict:
    rows = gap_grid(mu, sigma_t, sigma_s, n_large=n_large, seed=seed, grid=grid)
    return pick_f(rows, target) | {"n_large": n_large, "grid": rows}


# --------------------------------------------------------------------------------------------
# delta_min and power
# --------------------------------------------------------------------------------------------
def _delta_large(args):
    seed, mu, st, ss, f, L = args
    sim = simulate(np.random.default_rng(seed), mu, st, ss, f, L["tasks"],
                   L["sites_per_task"], L["draws"])
    w = sim["m"] / np.bincount(sim["task"], sim["m"])[sim["task"]]
    y = sim["k"] / sim["m"]
    fold = sim["task"] % L["folds"]
    base = A.nested_cv_logloss(sim["a"][:, None], y, w, fold)
    full = A.nested_cv_logloss(np.c_[sim["a"], sim["probe"]], y, w, fold)
    return base["loss"] - full["loss"]


def delta_min(mu, sigma_t, sigma_s, f, *, n_datasets=2000, seed=SEED, n_jobs=None,
              large=LARGE) -> dict:
    seeds = np.random.SeedSequence(seed).generate_state(n_datasets)
    jobs = [(int(s), mu, sigma_t, sigma_s, f, large) for s in seeds]
    d = np.array(_map(_delta_large, jobs, n_jobs))
    return {"delta_min": float(np.median(d)), "q25": float(np.percentile(d, 25)),
            "q75": float(np.percentile(d, 75)), "n_datasets": n_datasets,
            "large_sample_dataset": dict(large)}


def _meets_min_counts(sim) -> bool:
    """c2's minimum counts (p47.analysis defaults): sites, tasks, cheating and clean draws."""
    n_tasks = len(np.unique(sim["task"]))
    k, m = sim["k"].sum(), sim["m"].sum()
    return (len(sim["task"]) >= 20 and n_tasks >= 10 and k >= 15 and m - k >= 15)


def _delta_planned(args):
    seed, mu, st, ss, f, tasks, spt, draws, talk = args
    sim = simulate(np.random.default_rng(seed), mu, st, ss, f, tasks, spt, draws, talk)
    if not _meets_min_counts(sim):
        return float("nan")
    w = sim["m"] / np.bincount(sim["task"], sim["m"])[sim["task"]]
    y = sim["k"] / sim["m"]
    base = A.nested_cv_logloss(sim["a"][:, None], y, w, sim["task"])
    full = A.nested_cv_logloss(np.c_[sim["a"], sim["probe"]], y, w, sim["task"])
    return base["loss"] - full["loss"]


def delta_min_planned(mu, sigma_t, sigma_s, f, *, tasks, sites_per_task, draws, talk_rate=0.0,
                      n_datasets=2000, seed=SEED, n_jobs=None) -> dict:
    """Median held-out Delta at the planned counts (c2's nested leave-one-task-out CV)."""
    seeds = np.random.SeedSequence(seed).generate_state(n_datasets)
    jobs = [(int(s), mu, sigma_t, sigma_s, f, tasks, sites_per_task, draws, talk_rate)
            for s in seeds]
    d = np.array(_map(_delta_planned, jobs, n_jobs), float)
    ok = d[~np.isnan(d)]
    if not len(ok):
        return {"delta_min": float("nan"), "n_datasets": n_datasets, "n_valid": 0,
                "share_short_of_min_counts": 1.0}
    return {"delta_min": float(np.median(ok)), "q25": float(np.percentile(ok, 25)),
            "q75": float(np.percentile(ok, 75)), "n_datasets": n_datasets,
            "n_valid": len(ok), "share_short_of_min_counts": 1 - len(ok) / n_datasets,
            "counts": {"tasks": tasks, "sites_per_task": sites_per_task, "draws": draws,
                       "talk_rate": talk_rate}}


def _power_one(args):
    seed, mu, st, ss, f, dmin, tasks, spt, draws, talk, n_boot = args
    sim = simulate(np.random.default_rng(seed), mu, st, ss, f, tasks, spt, draws, talk)
    if len(sim["task"]) == 0:
        return "insufficient", float("nan"), float("nan")
    r = A.c2(to_frame(sim), ["bb"], "probe", delta_min=dmin, n_boot=n_boot, seed=seed,
             n_jobs=1)                       # serial inside a worker: no nested pools
    return r["verdict"], r.get("delta", float("nan")), r.get("ci", [float("nan")])[0]


def power(mu, sigma_t, sigma_s, f, dmin, *, tasks, sites_per_task, draws, talk_rate=0.0,
          n_datasets=200, n_boot=500, seed=SEED, n_jobs=None) -> dict:
    seeds = np.random.SeedSequence(seed + 1).generate_state(n_datasets)
    jobs = [(int(s), mu, sigma_t, sigma_s, f, dmin, tasks, sites_per_task, draws, talk_rate,
             n_boot) for s in seeds]
    out = _map(_power_one, jobs, n_jobs)
    verdicts = [o[0] for o in out]
    share = {v: verdicts.count(v) / n_datasets for v in (*A.VERDICTS, "insufficient")}
    p = share["beyond"]
    deltas = np.array([o[1] for o in out], float)
    return {"power": p, "mc_se": float(np.sqrt(p * (1 - p) / n_datasets)),
            "verdict_shares": share,
            "share_ci_low_above_0": float(np.mean([o[2] > 0 for o in out])),
            "median_delta": float(np.nanmedian(deltas)) if np.isfinite(deltas).any() else None,
            "n_datasets": n_datasets, "n_boot": n_boot,
            "approximation": (f"{n_datasets} datasets x {n_boot} bootstrap replicates "
                              "(plan: 2,000 datasets; c2's full nested CV per replicate)"),
            "counts": {"tasks": tasks, "sites_per_task": sites_per_task, "draws": draws,
                       "talk_rate": talk_rate}}


def _map(fn, jobs, n_jobs):
    n_jobs = A.n_workers(n_jobs)
    if n_jobs == 1:
        return [fn(j) for j in jobs]
    from joblib import Parallel, delayed
    from threadpoolctl import threadpool_limits

    with threadpool_limits(1):
        return Parallel(n_jobs=n_jobs, batch_size=4)(delayed(fn)(j) for j in jobs)


# --------------------------------------------------------------------------------------------
# two-level ML fit
# --------------------------------------------------------------------------------------------
def fit_two_level(sites: pd.DataFrame, *, n_quad: int = 40, floor: float = SIGMA_S_FLOOR) -> dict:
    """ML fit of k_ij ~ Bin(n_ij, logistic(mu + t_i + s_ij)), t ~ N(0, sigma_t^2),
    s ~ N(0, sigma_s^2), by nested Gauss-Hermite quadrature. sites: task_id, site_id, n_draws,
    n_cheats."""
    task, K = A._task_codes(sites["task_id"].to_numpy())
    n = sites["n_draws"].to_numpy(float)
    k = sites["n_cheats"].to_numpy(float)
    if (k > n).any() or (n <= 0).any():
        raise ValueError("need 0 <= n_cheats <= n_draws and n_draws > 0")
    x, wq = np.polynomial.hermite_e.hermegauss(n_quad)
    lw = np.log(wq / np.sqrt(2 * np.pi))
    lc = gammaln(n + 1) - gammaln(k + 1) - gammaln(n - k + 1)
    onehot = np.zeros((K, len(n)))
    onehot[task, np.arange(len(n))] = 1.0

    def nll(theta):
        mu, st, ss = theta[0], np.exp(theta[1]), np.exp(theta[2])
        eta = mu + st * x[None, :, None] + ss * x[None, None, :]          # [1, Qt, Qs]
        lp, lq = -np.logaddexp(0, -eta), -np.logaddexp(0, eta)
        ll = (k[:, None, None] * lp + (n - k)[:, None, None] * lq + lc[:, None, None])
        inner = logsumexp(ll + lw[None, None, :], axis=2)                   # [sites, Qt]
        T = onehot @ inner                                                  # [K, Qt]
        return -logsumexp(T + lw[None, :], axis=1).sum()

    p0 = np.clip(k.sum() / n.sum(), 0.01, 0.99)
    best = None
    for st0, ss0 in ((0.5, 0.5), (1.5, 1.5), (0.1, 2.0), (2.0, 0.1)):
        r = minimize(nll, [np.log(p0 / (1 - p0)), np.log(st0), np.log(ss0)], method="L-BFGS-B",
                     bounds=[(-12, 12), (np.log(1e-3), np.log(6)), (np.log(1e-3), np.log(6))])
        if best is None or r.fun < best.fun:
            best = r
    mu, st, ss = best.x[0], np.exp(best.x[1]), np.exp(best.x[2])
    return {"mu": float(mu), "sigma_task": float(st), "sigma_site_fitted": float(ss),
            "sigma_site": float(max(ss, floor)), "sigma_site_floored": bool(ss < floor),
            "loglik": float(-best.fun), "n_sites": len(n), "n_tasks": K,
            "n_draws": float(n.sum()), "n_cheats": float(k.sum()), "n_quad": n_quad}


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
def run(*, mu, sigma_t, sigma_s, tasks, sites_per_task, draws, talk_rate=0.0, seed=SEED,
        n_large=200_000, delta_datasets=2000, power_datasets=200, power_boot=500, n_jobs=None,
        skip_power=False, large=LARGE, delta_min_at="planned",
        gaps=(TARGET_GAP,)) -> dict:
    if delta_min_at not in ("planned", "large"):
        raise ValueError("delta_min_at must be 'planned' or 'large'")
    gaps = sorted({round(float(g), 6) for g in gaps} | {TARGET_GAP})
    counts = {"tasks": tasks, "sites_per_task": sites_per_task, "draws": draws,
              "talk_rate": talk_rate}
    t0 = time.time()
    rows = gap_grid(mu, sigma_t, sigma_s, n_large=n_large, seed=seed)
    cal = pick_f(rows, TARGET_GAP) | {"n_large": n_large, "grid": rows}
    t1 = time.time()
    dm_large = delta_min(mu, sigma_t, sigma_s, cal["f"], n_datasets=delta_datasets,
                         seed=seed + 2, n_jobs=n_jobs, large=large)
    dm_planned = delta_min_planned(mu, sigma_t, sigma_s, cal["f"], **counts,
                                   n_datasets=delta_datasets, seed=seed + 4, n_jobs=n_jobs)
    dmin = (dm_planned if delta_min_at == "planned" else dm_large)["delta_min"]
    t2 = time.time()
    per_gap = []
    for g in gaps:
        c = pick_f(rows, g)
        entry = {"target_gap": g, "f": c["f"], "gap": c["gap"], "auc_base": c["auc_base"],
                 "auc_full": c["auc_full"]}
        if c["underpowered_by_construction"] and g != TARGET_GAP:
            entry["unattainable"] = True
            per_gap.append(entry)
            continue
        entry["unattainable"] = False
        if g == TARGET_GAP:
            entry["delta_min_planned_at_gap"] = dm_planned
        else:
            entry["delta_min_planned_at_gap"] = delta_min_planned(
                mu, sigma_t, sigma_s, c["f"], **counts, n_datasets=delta_datasets,
                seed=seed + 4, n_jobs=n_jobs)
        entry["power"] = None if skip_power else power(
            mu, sigma_t, sigma_s, c["f"], dmin, **counts, n_datasets=power_datasets,
            n_boot=power_boot, seed=seed + 3, n_jobs=n_jobs)
        per_gap.append(entry)
    t3 = time.time()
    pw = next(e for e in per_gap if e["target_gap"] == TARGET_GAP)["power"]
    return {"params": {"mu": mu, "sigma_task": sigma_t, "sigma_site": sigma_s, "seed": seed},
            "counts": counts, "calibration": cal,
            "delta_min": {"used": delta_min_at, "value": dmin, "planned": dm_planned,
                          "large": dm_large},
            "power": pw, "gaps": per_gap,
            "c2_label": ("underpowered by construction" if cal["underpowered_by_construction"]
                         else ("underpowered (power < 0.5)"
                               if pw is not None and pw["power"] < 0.5 else "")),
            "seconds": {"calibration": t1 - t0, "delta_min": t2 - t1, "power": t3 - t2}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--fit-from", type=Path, help="sites CSV: task_id,site_id,n_draws,n_cheats")
    src.add_argument("--mu", type=float)
    ap.add_argument("--sigma-task", type=float)
    ap.add_argument("--sigma-site", type=float)
    ap.add_argument("--tasks", type=int, required=True)
    ap.add_argument("--sites-per-task", type=int, required=True)
    ap.add_argument("--draws", type=int, required=True)
    ap.add_argument("--talk-rate", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--n-large", type=int, default=200_000)
    ap.add_argument("--delta-datasets", type=int, default=2000)
    ap.add_argument("--power-datasets", type=int, default=200)
    ap.add_argument("--power-boot", type=int, default=500)
    ap.add_argument("--skip-power", action="store_true")
    ap.add_argument("--delta-min-at", choices=("planned", "large"), default="planned")
    ap.add_argument("--gaps", default=str(TARGET_GAP),
                    help="comma-separated target large-sample AUROC gaps, e.g. 0.05,0.10")
    ap.add_argument("--n-jobs", type=int, default=None,
                    help="workers; default and cap min(6, cpu_count) (shared-laptop rule)")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    fit = None
    if a.fit_from is not None:
        fit = fit_two_level(pd.read_csv(a.fit_from))
        mu, st, ss = fit["mu"], fit["sigma_task"], fit["sigma_site"]
    else:
        if a.sigma_task is None or a.sigma_site is None:
            ap.error("--mu needs --sigma-task and --sigma-site")
        mu, st, ss = a.mu, a.sigma_task, max(a.sigma_site, SIGMA_S_FLOOR)
    res = run(mu=mu, sigma_t=st, sigma_s=ss, tasks=a.tasks, sites_per_task=a.sites_per_task,
              draws=a.draws, talk_rate=a.talk_rate, seed=a.seed, n_large=a.n_large,
              delta_datasets=a.delta_datasets, power_datasets=a.power_datasets,
              power_boot=a.power_boot, n_jobs=a.n_jobs, skip_power=a.skip_power,
              delta_min_at=a.delta_min_at,
              gaps=[float(g) for g in a.gaps.split(",") if g.strip()])
    res["fit"] = fit
    res["argv"] = sys.argv[1:] if argv is None else list(argv)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=2, default=float) + "\n")
    cal, dm, pw = res["calibration"], res["delta_min"], res["power"]
    print(f"f={cal['f']:.2f} gap={cal['gap']:+.4f} base AUROC={cal['auc_base']:.3f} "
          f"delta_min({dm['used']})={dm['value']:.5f} "
          f"[planned {dm['planned']['delta_min']:.5f}, large {dm['large']['delta_min']:.5f}]"
          + (f" power={pw['power']:.3f} (se {pw['mc_se']:.3f})" if pw else "")
          + (f" [{res['c2_label']}]" if res["c2_label"] else "") + f" -> {a.out}")
    for e in res["gaps"]:
        if e.get("unattainable"):
            print(f"  gap {e['target_gap']:.2f}: unattainable")
        elif e["power"]:
            sh = e["power"]["verdict_shares"]
            print(f"  gap {e['target_gap']:.2f} f={e['f']:.2f} "
                  + " ".join(f"{k}={v:.3f}" for k, v in sh.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
