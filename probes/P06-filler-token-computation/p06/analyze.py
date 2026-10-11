"""P06 Stage 1 analysis (plan "Measure"): uplift, depth shift, multiple choice, validity.

    uv run python -m p06.analyze results/stage1/bulk_gemma4_s10_every.jsonl [...] \
        --out results/stage1/analysis_<name>.json

- **Uplift:** per item, accuracy averaged over its draws; the paired difference after − before
  (and after − none) averaged over items; 95% interval from an item-cluster bootstrap (10,000
  resamples, seed 0). Reported per bank, per k, and pooled over chain k.
- **Depth shift:** per arm, accuracy(k) = c + (1 − c)·σ(a − b·k) with nocot's floor c (the
  bank's majority baseline), fit by maximum likelihood on draw-level outcomes; k50 = a / b is the
  k at which accuracy is halfway between the floor and 1 ("50% above chance"). The shift is
  k50(after) − k50(before), bootstrapped over items (the same resampled items in every arm).
- **Multiple choice:** uplift after − none at each option count, and the difference of uplifts
  between 2 and 10 options (a parallel guess-and-check predicts uplift falls as options grow).
- **Validity:** rows failing a witness are scored wrong (nocot); their share per arm is reported.
Rows: the last status-ok row per key (`api.latest_ok`).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

from .api import latest_ok

N_BOOT = 10_000
SEED = 0


def load(paths: list[Path]) -> list[dict]:
    rows = []
    for p in paths:
        rows += list(latest_ok(p).values())
    return rows


def item_matrix(rows: list[dict], arms: tuple[str, ...]) -> tuple[list[str], dict, np.ndarray]:
    """(item ids, item meta, acc[item, arm]) with items that have every arm."""
    acc = defaultdict(lambda: defaultdict(list))
    meta = {}
    for r in rows:
        acc[r["item_id"]][r["arm"]].append(float(r["correct"]))
        meta[r["item_id"]] = {"k": r["k"], "bank": r["bank"]}
    ids = sorted(i for i, d in acc.items() if all(a in d for a in arms))
    m = np.array([[np.mean(acc[i][a]) for a in arms] for i in ids]) if ids else np.zeros((0, 3))
    return ids, meta, m


def boot_mean_diff(d: np.ndarray, rng: np.random.Generator, n_boot: int = N_BOOT) -> dict:
    """Mean of paired per-item differences and its item-cluster bootstrap 95% interval."""
    n = len(d)
    if n == 0:
        return {"n": 0}
    idx = rng.integers(0, n, size=(n_boot, n))
    bs = d[idx].mean(axis=1)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return {"n": int(n), "mean": float(d.mean()), "lo": float(lo), "hi": float(hi)}


def uplift_table(rows: list[dict], bank: str, rng: np.random.Generator) -> dict:
    arms = ("none", "after", "before")
    out = {}
    sub = [r for r in rows if r["bank"] == bank]
    groups = {"all": sub}
    if bank == "chain":
        for k in sorted({r["k"] for r in sub}):
            groups[f"k{k}"] = [r for r in sub if r["k"] == k]
    for g, rs in groups.items():
        ids, _meta, m = item_matrix(rs, arms)
        if not ids:
            continue
        out[g] = {"acc": {a: float(m[:, j].mean()) for j, a in enumerate(arms)},
                  "after_minus_before": boot_mean_diff(m[:, 1] - m[:, 2], rng),
                  "after_minus_none": boot_mean_diff(m[:, 1] - m[:, 0], rng),
                  "before_minus_none": boot_mean_diff(m[:, 2] - m[:, 0], rng)}
    return out


def _nll(params, k, y, c):
    a, b = params
    p = c + (1 - c) / (1 + np.exp(-(a - b * k)))
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return -np.sum(y * np.log(p) + (1 - y) * np.log(1 - p))


def fit_k50(k: np.ndarray, y: np.ndarray, c: float) -> float:
    """k50 = a / b for acc = c + (1 - c)·σ(a − b·k); nan if b <= 0 (no decline with k)."""
    best = None
    for a0, b0 in ((2.0, 1.0), (5.0, 1.5), (1.0, 0.3)):
        r = minimize(_nll, x0=[a0, b0], args=(k, y, c), method="Nelder-Mead",
                     options={"xatol": 1e-6, "fatol": 1e-8, "maxiter": 4000})
        if best is None or r.fun < best.fun:
            best = r
    a, b = best.x
    return float(a / b) if b > 1e-6 else float("nan")


def depth_shift(rows: list[dict], floor: float, rng: np.random.Generator,
                n_boot: int = 2000) -> dict:
    """k50 per arm and the after − before shift, item bootstrap (n_boot resamples; the fit is
    the slow part, so fewer than the uplift's 10,000)."""
    sub = [r for r in rows if r["bank"] == "chain"]
    per_item = defaultdict(lambda: defaultdict(list))
    kof = {}
    for r in sub:
        per_item[r["item_id"]][r["arm"]].append(float(r["correct"]))
        kof[r["item_id"]] = r["k"]
    ids = sorted(i for i, d in per_item.items() if all(a in d for a in ("after", "before")))

    def arrays(sel, arm):
        ks, ys = [], []
        for i in sel:
            for y in per_item[i].get(arm, []):
                ks.append(kof[i])
                ys.append(y)
        return np.array(ks, float), np.array(ys, float)

    est = {arm: fit_k50(*arrays(ids, arm), floor) for arm in ("none", "after", "before")}
    by_k = defaultdict(list)
    for i in ids:
        by_k[kof[i]].append(i)
    shifts = []
    for _ in range(n_boot):  # stratified by k so every k stays in each resample
        sel = [i for k, its in by_k.items() for i in rng.choice(its, size=len(its))]
        shifts.append(fit_k50(*arrays(sel, "after"), floor) - fit_k50(*arrays(sel, "before"),
                                                                        floor))
    shifts = np.array(shifts)
    shifts = shifts[np.isfinite(shifts)]
    lo, hi = (np.percentile(shifts, [2.5, 97.5]) if len(shifts) else (np.nan, np.nan))
    return {"floor": floor, "k50": est, "shift_after_minus_before": est["after"] - est["before"],
            "lo": float(lo), "hi": float(hi), "n_boot_finite": len(shifts),
            "n_items": len(ids)}


def mc_table(rows: list[dict], rng: np.random.Generator) -> dict:
    sub = [r for r in rows if r["bank"] == "chain_mc"]
    if not sub:
        return {}
    out = {}
    per = {}
    for n_opt in (2, 5, 10):
        rs = [r for r in sub if r["item_id"].startswith(f"mc{n_opt}:")]
        ids, _meta, m = item_matrix(rs, ("none", "after"))
        base = [i.split(":", 1)[1] for i in ids]
        per[n_opt] = dict(zip(base, m[:, 1] - m[:, 0], strict=True))
        out[f"opt{n_opt}"] = {"acc": {"none": float(m[:, 0].mean()), "after": float(m[:, 1].mean())},
                              "chance": 1 / n_opt,
                              "after_minus_none": boot_mean_diff(m[:, 1] - m[:, 0], rng)}
    common = sorted(set(per[2]) & set(per[10]))
    d = np.array([per[2][i] - per[10][i] for i in common])
    out["uplift2_minus_uplift10"] = boot_mean_diff(d, rng)
    return out


def validity(rows: list[dict]) -> dict:
    out = defaultdict(lambda: {"n": 0, "invalid": 0})
    for r in rows:
        key = f"{r['bank']}:{r['arm']}"
        out[key]["n"] += 1
        out[key]["invalid"] += int(not r["valid"])
    return {k: {**v, "share": v["invalid"] / v["n"]} for k, v in sorted(out.items())}


def analyze(rows: list[dict], chain_floor: float) -> dict:
    rng = np.random.default_rng(SEED)
    res = {"validity": validity(rows)}
    for bank in ("chain", "fact_hops"):
        if any(r["bank"] == bank for r in rows):
            res[f"uplift_{bank}"] = uplift_table(rows, bank, rng)
    if any(r["bank"] == "chain" for r in rows):
        res["depth"] = depth_shift(rows, chain_floor, rng)
    res["mc"] = mc_table(rows, rng)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--chain-floor", type=float, default=0.07)
    a = ap.parse_args()
    res = analyze(load(a.paths), a.chain_floor)
    a.out.write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
