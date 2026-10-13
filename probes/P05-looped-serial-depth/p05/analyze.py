"""P05 analysis: serial-depth fits, paired bootstrap, per-loop readability, ask-the-model baseline.

Implements `probes/P05-looped-serial-depth/plan.md`, sections "Measure" and "Predictions":
P(correct) = c + (1 - c) * sigmoid(s * (m - k)) by maximum likelihood with c fixed at the bank's
majority-class floor; d50 = m - ln(1 - 2c) / s is the 50% crossing. Intervals are a paired item
bootstrap (items resampled within depth, jointly for every configuration). Outcomes follow the
plan's five ordered predictions (floor, above 0, below 0, within +-0.5, inconclusive).

Fits run on per-depth binomial counts, which is exact for this likelihood, and are vectorized over
bootstrap resamples: a coarse (m, s) grid picks the start, damped Fisher scoring polishes it.

CLI: `python -m p05.analyze <results_dir> --out <json>` reads `<results_dir>/tf/*.jsonl` (score
rows) and `<results_dir>/chance.json` (bank -> floor c) and writes `summarize`'s output.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.special import expit
from scipy.stats import rankdata

M_BOUNDS = (-20.0, 30.0)
S_MAX = 50.0
_POS_S = np.geomspace(0.02, S_MAX, 30)
_M_GRID = np.linspace(M_BOUNDS[0], M_BOUNDS[1], 201)
_S_GRID = np.concatenate([-_POS_S[::-1], [0.0], _POS_S])
_CHUNK = 1024
Z95 = 1.959963984540054


# ---------------------------------------------------------------- curve fitting


def _log_pq(z: np.ndarray, c: float) -> tuple[np.ndarray, np.ndarray]:
    """log p and log(1 - p) for p = c + (1 - c) sigmoid(z), computed stably."""
    log_sig = -np.logaddexp(0.0, -z)
    log_q = math.log1p(-c) - np.logaddexp(0.0, z)
    if c == 0:
        return log_sig, log_q
    return np.logaddexp(math.log(c), math.log1p(-c) + log_sig), log_q


def _loglik(m, s, ks, succ, n, c) -> np.ndarray:
    lp, lq = _log_pq(s[:, None] * (m[:, None] - ks[None, :]), c)
    return (succ * lp + (n - succ) * lq).sum(axis=1)


def _fit_counts(
    ks: np.ndarray, succ: np.ndarray, n: np.ndarray, c: float, max_iter: int = 300
) -> tuple[np.ndarray, np.ndarray]:
    """MLE (m, s) for each row of succ [R, K] with trials n [K]; grid start, damped Fisher."""
    ks = np.asarray(ks, float)
    succ = np.asarray(succ, float)
    n = np.broadcast_to(np.asarray(n, float), succ.shape)
    fail = n - succ
    mg, sg = (a.ravel() for a in np.meshgrid(_M_GRID, _S_GRID, indexing="ij"))
    lp, lq = _log_pq(sg[:, None] * (mg[:, None] - ks[None, :]), c)
    best = np.empty(len(succ), dtype=int)
    for i in range(0, len(succ), _CHUNK):
        sl = slice(i, i + _CHUNK)
        best[sl] = np.argmax(succ[sl] @ lp.T + fail[sl] @ lq.T, axis=1)
    m, s = mg[best].copy(), sg[best].copy()
    ll = _loglik(m, s, ks, succ, n, c)
    lam = np.full(len(succ), 1e-3)
    done = np.zeros(len(succ), bool)
    for _ in range(max_iter):
        d = m[:, None] - ks[None, :]
        sig = expit(s[:, None] * d)
        p = np.clip(c + (1 - c) * sig, 1e-12, 1 - 1e-12)
        dpdz = (1 - c) * sig * (1 - sig)
        dm, ds = dpdz * s[:, None], dpdz * d
        w = 1.0 / (p * (1 - p))
        r = (succ - n * p) * w
        gm, gs = (r * dm).sum(1), (r * ds).sum(1)
        nw = n * w
        imm, ims, iss = (nw * dm * dm).sum(1), (nw * dm * ds).sum(1), (nw * ds * ds).sum(1)
        scale = 0.5 * (imm + iss) + 1e-9
        a, b, e = imm + lam * scale, ims, iss + lam * scale
        det = a * e - b * b
        m_new = np.clip(m + (e * gm - b * gs) / det, *M_BOUNDS)
        s_new = np.clip(s + (a * gs - b * gm) / det, -S_MAX, S_MAX)
        ll_new = _loglik(m_new, s_new, ks, succ, n, c)
        ok = (ll_new > ll + 1e-12) & ~done
        done |= (ok & (ll_new - ll < 1e-10)) | (lam > 1e8)
        m, s, ll = np.where(ok, m_new, m), np.where(ok, s_new, s), np.where(ok, ll_new, ll)
        lam = np.where(ok, lam * 0.3, lam * 10.0)
        if done.all():
            break
    return m, s


def _d50_vec(m, s, succ, n, c) -> np.ndarray:
    """Raw d50 per row: -inf all wrong, +inf all correct, nan if s <= 0."""
    succ = np.asarray(succ, float)
    n = np.broadcast_to(np.asarray(n, float), succ.shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        d50 = np.where(s > 0, m - math.log1p(-2 * c) / s, np.nan)
    d50 = np.where(succ.sum(1) == 0, -np.inf, d50)
    return np.where((succ == n).all(1), np.inf, d50)


def wilson(x: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for x successes in n trials."""
    if n == 0:
        return (math.nan, math.nan)
    p, z2 = x / n, z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return (centre - half, centre + half)


def _check_c(c: float) -> None:
    if not 0 <= c < 0.5:
        raise ValueError(f"chance floor c must be in [0, 0.5), got {c}")


def _fit_from_counts(ks, succ, n, c) -> dict:
    ks, succ, n = np.asarray(ks), np.asarray(succ, float), np.asarray(n, float)
    kmin, kmax = float(ks.min()), float(ks.max())
    per_k = {
        int(k): (int(nk), float(x / nk), *wilson(int(x), int(nk))) for k, x, nk in zip(ks, succ, n)
    }
    base = {"per_k": per_k, "kmin": kmin, "kmax": kmax, "c": float(c)}
    if succ.sum() == 0:
        return {"m": math.nan, "s": math.nan, "d50": -math.inf, "status": "below", **base}
    if (succ == n).all():
        return {"m": math.nan, "s": math.nan, "d50": math.inf, "status": "above", **base}
    m, s = _fit_counts(ks, succ[None, :], n, c)
    m, s = float(m[0]), float(s[0])
    if s <= 0:
        return {"m": m, "s": s, "d50": math.nan, "status": "nonmonotone", **base}
    d50 = m - math.log1p(-2 * c) / s
    status = "below" if d50 < kmin else "above" if d50 > kmax else "ok"
    return {"m": m, "s": s, "d50": d50, "status": status, **base}


def fit_curve(k: np.ndarray, y: np.ndarray, c: float) -> dict:
    """Fit P(correct) = c + (1-c) sigmoid(s (m - k)) to binary outcomes y at depths k.

    Returns m, s, d50 (= m - ln(1 - 2c)/s), status ("ok", "below" = "< kmin", "above" =
    "> kmax", "nonmonotone" = s <= 0, d50 nan) and per_k {k: (n, acc, wilson_lo, wilson_hi)}.
    All wrong gives "below" (d50 -inf), all correct "above" (d50 +inf), m and s nan.
    """
    _check_c(c)
    k, y = np.asarray(k), np.asarray(y, float)
    ks = np.unique(k)
    succ = np.array([y[k == kk].sum() for kk in ks])
    n = np.array([(k == kk).sum() for kk in ks])
    return _fit_from_counts(ks, succ, n, c)


def d50_clipped(fit: dict, kmin: float, kmax: float, pad: float = 1.0) -> float:
    """d50 clipped to [kmin - pad, kmax + pad] (bounds and infinities land on the clip); nan if
    nonmonotone."""
    if fit["status"] == "nonmonotone":
        return math.nan
    return float(np.clip(fit["d50"], kmin - pad, kmax + pad))


# ---------------------------------------------------------------- paired bootstrap


def _cell_matrix(rows, configs, bank, fmt):
    """Correctness matrix [n_items, n_configs], item ids and depths for one (bank, fmt) cell."""
    cell: dict[tuple, dict[str, bool]] = {tuple(cf): {} for cf in configs}
    item_k: dict[str, int] = {}
    for row in rows:
        if row["bank"] != bank or row["fmt"] != fmt:
            continue
        cf = (row["model"], row["seed"], row["r"])
        if cf not in cell:
            continue
        iid, k = row["item_id"], int(row["k"])
        if item_k.setdefault(iid, k) != k:
            raise ValueError(f"{bank}/{fmt}: item {iid} has depth {item_k[iid]} and {k}")
        if iid in cell[cf]:
            raise ValueError(f"{bank}/{fmt}: duplicate row for {cf} item {iid}")
        cell[cf][iid] = bool(row["correct"])
    ids = sorted(item_k, key=lambda i: (item_k[i], i))
    missing = [(cf, i) for cf in cell for i in ids if i not in cell[cf]]
    empty = [cf for cf in cell if not cell[cf]]
    if empty or missing:
        shown = ", ".join(f"{cf}:{i}" for cf, i in missing[:10])
        raise ValueError(
            f"{bank}/{fmt}: configs with no rows {empty}; {len(missing)} missing rows {shown}"
        )
    y = np.array([[cell[tuple(cf)][i] for cf in configs] for i in ids], dtype=float)
    return ids, np.array([item_k[i] for i in ids]), y


def paired_bootstrap(
    rows: list[dict],
    configs: list[tuple],
    bank: str,
    fmt: str,
    c: float,
    n_boot: int = 2000,
    seed: int = 0,
    pad: float = 1.0,
) -> dict:
    """Point fits and B joint resamples for one (bank, fmt) cell.

    Items are resampled with replacement within each k, with the same resampled ids for every
    configuration. "boot" is [B, n_configs] of clipped d50s (nan = nonmonotone resample).
    """
    _check_c(c)
    configs = [tuple(cf) for cf in configs]
    ids, k, y = _cell_matrix(rows, configs, bank, fmt)
    ks = np.unique(k)
    idx_per_k = [np.flatnonzero(k == kk) for kk in ks]
    n = np.array([len(ix) for ix in idx_per_k], float)
    succ = np.stack([y[ix].sum(0) for ix in idx_per_k], axis=1)  # [n_configs, K]
    point = {cf: _fit_from_counts(ks, succ[j], n, c) for j, cf in enumerate(configs)}
    kmin, kmax = float(ks.min()), float(ks.max())
    rng = np.random.default_rng(seed)
    bsucc = np.zeros((n_boot, len(configs), len(ks)))
    for j, ix in enumerate(idx_per_k):
        w = rng.multinomial(len(ix), np.full(len(ix), 1.0 / len(ix)), size=n_boot)
        bsucc[:, :, j] = w @ y[ix]
    flat = bsucc.reshape(-1, len(ks))
    m, s = _fit_counts(ks, flat, n, c)
    d50 = np.clip(_d50_vec(m, s, flat, n, c), kmin - pad, kmax + pad)
    return {
        "point": point,
        "point_clipped": {cf: d50_clipped(point[cf], kmin, kmax, pad) for cf in configs},
        "boot": d50.reshape(n_boot, len(configs)),
        "configs": configs,
        "item_ids_per_k": {int(kk): [ids[i] for i in ix] for kk, ix in zip(ks, idx_per_k)},
        "kmin": kmin,
        "kmax": kmax,
        "pad": pad,
        "c": float(c),
    }


def delta_ci(boot: dict, a: tuple, b: tuple, alpha: float = 0.05) -> dict:
    """Delta = d50(a) - d50(b) (clipped) with a percentile CI from the joint resamples.

    frac_nan: resamples dropped because either fit was nonmonotone. frac_bounded: share of the
    kept resamples where either config sat at a clip bound.
    """
    a, b = tuple(a), tuple(b)
    ia, ib = boot["configs"].index(a), boot["configs"].index(b)
    da, db = boot["boot"][:, ia], boot["boot"][:, ib]
    keep = ~(np.isnan(da) | np.isnan(db))
    delta = da[keep] - db[keep]
    lo_b, hi_b = boot["kmin"] - boot["pad"], boot["kmax"] + boot["pad"]

    def at_bound(x):
        return (x <= lo_b) | (x >= hi_b)

    bounded = at_bound(da[keep]) | at_bound(db[keep])
    if keep.any():
        lo, hi = np.quantile(delta, [alpha / 2, 1 - alpha / 2])
    else:
        lo = hi = math.nan
    return {
        "a": list(a),
        "b": list(b),
        "delta": boot["point_clipped"][a] - boot["point_clipped"][b],
        "lo": float(lo),
        "hi": float(hi),
        "frac_bounded": float(bounded.mean()) if keep.any() else math.nan,
        "frac_nan": float(1 - keep.mean()),
        "n_boot": len(da),
    }


def classify_outcome(delta: dict, floor: bool, margin: float = 0.5) -> int:
    """Plan outcomes, first match wins: 1 floor, 2 CI > 0, 3 CI < 0, 4 CI in +-margin, 5 else."""
    lo, hi = delta["lo"], delta["hi"]
    if floor:
        return 1
    if math.isnan(lo) or math.isnan(hi):
        return 5
    if lo > 0:
        return 2
    if hi < 0:
        return 3
    if lo >= -margin and hi <= margin:
        return 4
    return 5


# ---------------------------------------------------------------- readability


def _derangement(n: int, rng: np.random.Generator) -> np.ndarray:
    """Random cyclic permutation (Sattolo): partner[i] != i for n >= 2."""
    perm = rng.permutation(n)
    partner = np.empty(n, dtype=int)
    partner[perm] = np.roll(perm, -1)
    return partner


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 2 or np.ptp(x) == 0 or np.ptp(y) == 0:
        return math.nan
    return float(np.corrcoef(rankdata(x), rankdata(y))[0, 1])


def _ci(samples: np.ndarray, alpha: float = 0.05) -> tuple[float, float]:
    samples = samples[~np.isnan(samples)]
    if not len(samples):
        return (math.nan, math.nan)
    lo, hi = np.quantile(samples, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def readability(
    ro: dict,
    ctrl_seed: int = 0,
    correct_ids: set[str] | None = None,
    n_boot: int = 2000,
    ks: tuple[int, ...] = (2, 3, 4),
) -> dict:
    """Per-loop readability of chain intermediates (plan, "Per-loop readability").

    Eligible items: k in ks, traj[1..k] all distinct, optionally in correct_ids. Hit at (j, t, l):
    argmax over the 20 candidates == traj[j] (value v is index v - 1), j = 1..k-1. Control: the
    step-j value of a partner item with the same k (fixed derangement, seed ctrl_seed). Items
    alone in their k group have no partner and are dropped (counted in "n_dropped_singleton").
    Returns per_jtl arrays [J, T, L] (j = 1..max(ks)-1), any_rate per lens with an item
    bootstrap CI (seed 0), and order per lens (pooled Spearman j vs t_first over k >= 3 items;
    never top-1 ranks T, ties last).
    """
    k_all = np.asarray(ro["k"], int)
    traj, ids = ro["traj"], list(ro["item_ids"])
    cand = np.asarray(ro["cand"])
    n_t, n_l = cand.shape[1], cand.shape[2]
    elig = [
        i
        for i in range(len(ids))
        if k_all[i] in ks
        and len(set(traj[i][1 : k_all[i] + 1])) == k_all[i]
        and (correct_ids is None or ids[i] in correct_ids)
    ]
    rng = np.random.default_rng(ctrl_seed)
    groups = defaultdict(list)
    for i in elig:
        groups[int(k_all[i])].append(i)
    items, partner = [], {}
    dropped = 0
    for kk in sorted(groups):
        g = groups[kk]
        if len(g) < 2:
            dropped += len(g)
            continue
        der = _derangement(len(g), rng)
        for pos, i in enumerate(g):
            partner[i] = g[der[pos]]
            items.append(i)
    n_j = max(ks) - 1
    top = cand[items].argmax(axis=-1) + 1 if items else np.zeros((0, n_t, n_l), int)  # [n,T,L]
    hit = np.zeros((len(items), n_j, n_t, n_l), bool)
    ctrl = np.zeros_like(hit)
    valid = np.zeros((len(items), n_j), bool)
    for a, i in enumerate(items):
        for j in range(1, int(k_all[i])):
            valid[a, j - 1] = True
            hit[a, j - 1] = top[a] == traj[i][j]
            ctrl[a, j - 1] = top[a] == traj[partner[i]][j]
    n_per_j = valid.sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        per_hit = hit.sum(0) / n_per_j[:, None, None]
        per_ctrl = ctrl.sum(0) / n_per_j[:, None, None]
    any_hit = hit.any(axis=(1, 2))  # [n, L]
    any_ctrl = ctrl.any(axis=(1, 2))
    brng = np.random.default_rng(0)
    bidx = brng.integers(0, max(len(items), 1), size=(n_boot, len(items)))
    k_items = k_all[items] if items else np.zeros(0, int)
    lenses = list(ro["lenses"])
    any_rate, order = {}, {}
    # Pairs for order: (item position, j, t_first) for items with k >= 3.
    pair_item, pair_j = [], []
    for a, i in enumerate(items):
        if k_all[i] >= 3:
            for j in range(1, int(k_all[i])):
                pair_item.append(a)
                pair_j.append(j)
    pair_item, pair_j = np.array(pair_item, int), np.array(pair_j, int)
    # Bootstrap pair rows: repeat each resampled item's pairs.
    starts = np.searchsorted(pair_item, np.arange(len(items)))
    lens_n = np.bincount(pair_item, minlength=len(items))
    for li, lens in enumerate(lenses):
        diff = any_hit[:, li].astype(float) - any_ctrl[:, li]
        boot_diff = diff[bidx].mean(1) if len(items) else np.full(n_boot, np.nan)
        by_k = {
            int(kk): {
                "n": int((k_items == kk).sum()),
                "hit": float(any_hit[k_items == kk, li].mean()),
                "ctrl": float(any_ctrl[k_items == kk, li].mean()),
            }
            for kk in np.unique(k_items)
        }
        any_rate[lens] = {
            "n": len(items),
            "hit": float(any_hit[:, li].mean()) if items else math.nan,
            "ctrl": float(any_ctrl[:, li].mean()) if items else math.nan,
            "diff": float(diff.mean()) if items else math.nan,
            "ci": _ci(boot_diff),
            "by_k": by_k,
        }
        h = hit[pair_item, pair_j - 1, :, li]  # [P, T]
        t_first = np.where(h.any(1), h.argmax(1), n_t)
        rho = _spearman(pair_j, t_first)
        boot_rho = np.full(n_boot, np.nan)
        if len(pair_item):
            for bi in range(n_boot):
                sel = bidx[bi]
                cnt = lens_n[sel]
                rows = np.repeat(starts[sel] - np.cumsum(cnt) + cnt, cnt) + np.arange(cnt.sum())
                boot_rho[bi] = _spearman(pair_j[rows], t_first[rows])
        order[lens] = {
            "n_pairs": len(pair_item),
            "rho": rho,
            "ci": _ci(boot_rho),
            "never_share": float((t_first == n_t).mean()) if len(pair_item) else math.nan,
        }
    return {
        "n_items": len(items),
        "n_dropped_singleton": dropped,
        "per_jtl": {
            "j": list(range(1, n_j + 1)),
            "loops": list(ro["loops"]),
            "lenses": lenses,
            "n": n_per_j,
            "hit": per_hit,
            "ctrl": per_ctrl,
        },
        "any_rate": any_rate,
        "order": order,
        "partner": {ids[i]: ids[partner[i]] for i in items},
    }


# ---------------------------------------------------------------- ask-the-model baseline


def ask_baseline(rows: list[dict]) -> dict:
    """Top-1 accuracy on prefix prompts per (model, seed, fmt, r, j), with a control scoring
    another row's answer (same group, derangement over sorted base_ids, fresh seed-0 rng per group,
    so the same ids get the same pairing at every r). "overall" pools j per (model, seed, fmt, r).
    """
    groups = defaultdict(list)
    for row in rows:
        groups[(row["model"], row["seed"], row["fmt"], row["r"], row["j"])].append(row)
    per_group, pooled = [], defaultdict(lambda: [0, 0, 0])
    for key in sorted(groups):
        g = sorted(groups[key], key=lambda r: r["base_id"])
        top1 = np.array([r["top1"] for r in g])
        ans = np.array([r["answer"] for r in g])
        acc = float((top1 == ans).mean())
        if len(g) >= 2:
            ctrl_hits = top1 == ans[_derangement(len(g), np.random.default_rng(0))]
            ctrl = float(ctrl_hits.mean())
        else:
            ctrl_hits, ctrl = np.zeros(0, bool), math.nan
        model, seed, fmt, r, j = key
        per_group.append(
            {
                "model": model,
                "seed": seed,
                "fmt": fmt,
                "r": r,
                "j": j,
                "n": len(g),
                "acc": acc,
                "ctrl": ctrl,
                "diff": acc - ctrl,
            }
        )
        if len(g) >= 2:
            p = pooled[key[:4]]
            p[0] += len(g)
            p[1] += int((top1 == ans).sum())
            p[2] += int(ctrl_hits.sum())
    overall = [
        {
            "model": k[0],
            "seed": k[1],
            "fmt": k[2],
            "r": k[3],
            "n": n,
            "acc": h / n,
            "ctrl": cc / n,
            "diff": (h - cc) / n,
        }
        for k, (n, h, cc) in sorted(pooled.items())
    ]
    return {"per_group": per_group, "overall": overall}


# ---------------------------------------------------------------- power simulation


def power_sim(
    fit_a: dict,
    fit_b: dict,
    c: float,
    ks: list[int],
    n_per_k: int,
    n_sim: int = 300,
    seed: int = 0,
    pad: float = 1.0,
) -> dict:
    """SD of Delta d50 (clipped) when both configurations are simulated from their fitted curves.

    The two arms are simulated independently, which ignores the positive item-pairing correlation
    of the real design, so the SD (and the 1.96 SD half-width) is conservative.
    """
    _check_c(c)
    ks_a = np.asarray(ks, float)
    rng = np.random.default_rng(seed)
    out = []
    for fit in (fit_a, fit_b):
        if not (math.isfinite(fit["m"]) and math.isfinite(fit["s"])):
            raise ValueError(f"cannot simulate from a fit with status {fit['status']}")
        p = c + (1 - c) * expit(fit["s"] * (fit["m"] - ks_a))
        succ = rng.binomial(n_per_k, np.broadcast_to(p, (n_sim, len(ks_a)))).astype(float)
        m, s = _fit_counts(ks_a, succ, np.full(len(ks_a), n_per_k), c)
        out.append(np.clip(_d50_vec(m, s, succ, n_per_k, c), ks_a.min() - pad, ks_a.max() + pad))
    delta = out[0] - out[1]
    keep = ~np.isnan(delta)
    sd = float(np.std(delta[keep], ddof=1)) if keep.sum() >= 2 else math.nan
    return {
        "sd": sd,
        "half_width": Z95 * sd,
        "frac_nonmonotone": float(1 - keep.mean()),
        "n_valid": int(keep.sum()),
        "n_sim": n_sim,
        "n_per_k": n_per_k,
    }


# ---------------------------------------------------------------- summary and CLI


def _key(cf: tuple) -> str:
    return f"{cf[0]}/s{cf[1]}/r{cf[2]}"


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return _jsonable(x.tolist())
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return float(x) if math.isfinite(x) else str(float(x))
    return x


def _eff_layers(model: str, r: int) -> int:
    """Effective layers: 4 prelude + 6 r recurrent + 4 coda (both McLeish recurrent models);
    16 for the non-recurrent baseline."""
    return 16 if model == "base" else 8 + 6 * r


def summarize(rows: list[dict], chance: dict, n_boot: int = 2000) -> dict:
    """Per (bank, fmt) cell: point fits for every configuration, the plan's named contrasts with
    paired CIs, and the d50-against-r curve per recurrent model and seed (JSON-serializable)."""
    cells = sorted({(r["bank"], r["fmt"]) for r in rows})
    out = {}
    for bank, fmt in cells:
        cell_rows = [r for r in rows if r["bank"] == bank and r["fmt"] == fmt]
        configs = sorted({(r["model"], r["seed"], r["r"]) for r in cell_rows})
        bs = paired_bootstrap(cell_rows, configs, bank, fmt, chance[bank], n_boot=n_boot)
        have = set(configs)
        seeds = sorted({cf[1] for cf in configs})
        base_seeds = sorted(cf[1] for cf in configs if cf[0] == "base")
        contrasts: dict[str, dict] = defaultdict(dict)
        for sd in seeds:
            rec32 = ("rec32", sd, 32)
            if base_seeds and rec32 in have:
                base = ("base", sd if sd in base_seeds else base_seeds[0], 0)
                d = delta_ci(bs, rec32, base)
                d["outcome_if_not_floor"] = classify_outcome(d, floor=False)
                contrasts["delta_r32_vs_base"][f"seed{sd}"] = d
            if rec32 in have and ("rec32", sd, 1) in have:
                contrasts["loop_gain_32_vs_1"][f"seed{sd}"] = delta_ci(bs, rec32, ("rec32", sd, 1))
            if rec32 in have and ("rec4", sd, 4) in have:
                contrasts["rec4_r4_vs_rec32_r32"][f"seed{sd}"] = delta_ci(
                    bs, ("rec4", sd, 4), rec32
                )
        curves = defaultdict(list)
        for j, cf in enumerate(configs):
            if cf[0] == "base":
                continue
            fit = bs["point"][cf]
            curves[f"{cf[0]}/s{cf[1]}"].append(
                {
                    "r": cf[2],
                    "eff_layers": _eff_layers(cf[0], cf[2]),
                    "d50": fit["d50"],
                    "d50_clipped": bs["point_clipped"][cf],
                    "status": fit["status"],
                    "ci": _ci(bs["boot"][:, j]),
                }
            )
        out[f"{bank}/{fmt}"] = {
            "c": chance[bank],
            "n_items": sum(len(v) for v in bs["item_ids_per_k"].values()),
            "n_per_k": {k: len(v) for k, v in bs["item_ids_per_k"].items()},
            "n_boot": n_boot,
            "configs": {
                _key(cf): {
                    **bs["point"][cf],
                    "d50_clipped": bs["point_clipped"][cf],
                    "eff_layers": _eff_layers(cf[0], cf[2]),
                    "ci": _ci(bs["boot"][:, j]),
                }
                for j, cf in enumerate(configs)
            },
            "contrasts": dict(contrasts),
            "curves": {k: sorted(v, key=lambda e: e["r"]) for k, v in curves.items()},
        }
    return _jsonable(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n-boot", type=int, default=2000)
    args = ap.parse_args(argv)
    rows = []
    for path in sorted((args.results_dir / "tf").glob("*.jsonl")):
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows:
        raise SystemExit(f"no score rows under {args.results_dir / 'tf'}")
    chance = json.loads((args.results_dir / "chance.json").read_text())
    summary = summarize(rows, chance, n_boot=args.n_boot)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=1) + "\n")
    print(f"wrote {args.out} ({len(rows)} rows, {len(summary)} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
