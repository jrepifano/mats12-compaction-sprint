"""Detector selection, gate thresholds and the disagreement set: one module for the pod and the
laptop analysis (plan.md "Automatic rules", on-pod selection). numpy only.

Scores are oriented higher = unobserved. A **detector** maps configurations (e.g. "lr|last|14")
to per-prompt score arrays aligned with one prompt list; a per-text detector (ask, T1-T3) has a
single configuration whose score repeats over a text's questions.

- `pick`: each detector's configuration with the highest AUROC on CAL (ties: first in sorted
  order).
- **Gate**: fires on a prompt when its score exceeds a threshold set on CAL's OBSERVED prompts so
  that the gate fires on exactly `rate` of them in expectation: thr = the (1 - rate) quantile;
  prompts tied at thr fire with the probability that makes the CAL rate exact, using a uniform
  derived from the prompt id (deterministic; `tie_u`).
- **Headline pair**: A_org (the organism's probe) and the comparator = the CAL-best of
  COMPARATORS. The disagreement set = TEST prompts where the two gates differ at any rate in
  FIRE_RATES.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

import numpy as np

HEADLINE = "A_org"
COMPARATORS = ("ASK_org", "ASK_base", "T1", "T2", "T3", "A_base")


def auroc(scores: np.ndarray, labels: np.ndarray) -> float:
    """P(score of a random positive > a random negative), ties half (Mann-Whitney)."""
    s = np.asarray(scores, float)
    y = np.asarray(labels).astype(bool)
    pos, neg = s[y], s[~y]
    if not len(pos) or not len(neg) or not np.all(np.isfinite(s)):
        return float("nan")
    allv = np.concatenate([pos, neg])
    order = allv.argsort(kind="mergesort")
    sv = allv[order]
    ranks = np.empty(len(allv))
    i = 0
    while i < len(sv):
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def tie_u(prompt_ids: Sequence[str]) -> np.ndarray:
    """A deterministic uniform in [0, 1) per prompt id."""
    return np.array([int.from_bytes(hashlib.sha256(f"P14|tie|{p}".encode()).digest()[:8], "big")
                     / 2.0**64 for p in prompt_ids])


def threshold(cal_obs_scores: np.ndarray, rate: float) -> tuple[float, float]:
    """(thr, p_tie): firing = s > thr, or s == thr with probability p_tie, gives exactly `rate`
    of `cal_obs_scores` in expectation."""
    s = np.sort(np.asarray(cal_obs_scores, float))
    n = len(s)
    k = rate * n  # expected number to fire
    if k <= 0:
        return float(s[-1]), 0.0
    thr = float(s[max(0, int(np.ceil(n - k)) - 1)]) if k < n else float(s[0]) - 1.0
    above = float((s > thr).sum())
    tied = float((s == thr).sum())
    p = 0.0 if tied == 0 else min(1.0, max(0.0, (k - above) / tied))
    return thr, p


def fire(scores: np.ndarray, thr: float, p_tie: float, u: np.ndarray) -> np.ndarray:
    s = np.asarray(scores, float)
    return (s > thr) | ((s == thr) & (u < p_tie))


def pick(cands: Mapping[str, np.ndarray], cal_idx: np.ndarray, labels: np.ndarray
         ) -> tuple[str, float, dict[str, float]]:
    """(best configuration, its CAL AUROC, every configuration's CAL AUROC)."""
    aucs = {c: auroc(v[cal_idx], labels[cal_idx]) for c, v in sorted(cands.items())}
    ok = {c: a for c, a in aucs.items() if np.isfinite(a)}
    if not ok:
        raise ValueError("no configuration has a finite CAL AUROC")
    best = max(ok, key=lambda c: (ok[c], -sorted(ok).index(c)))
    return best, ok[best], aucs


def selection(detectors: Mapping[str, Mapping[str, np.ndarray]], prompt_ids: Sequence[str],
              sets: Sequence[str], labels: np.ndarray, rates: Sequence[float],
              kill_auroc: float) -> dict:
    """The on-pod selection record (module docstring). `sets` and `labels` align with
    `prompt_ids` (labels 1 unobs, 0 obs, -1 none)."""
    sets = np.asarray(sets)
    labels = np.asarray(labels)
    cal = np.where((sets == "cal") & (labels >= 0))[0]
    cal_obs = np.where((sets == "cal") & (labels == 0))[0]
    u = tie_u(prompt_ids)
    rec: dict = {"detectors": {}, "rates": list(rates)}
    for name, cands in sorted(detectors.items()):
        cfg, auc, aucs = pick(cands, cal, labels)
        s = cands[cfg]
        thr = {str(r): threshold(s[cal_obs], r) for r in rates}
        rec["detectors"][name] = {
            "config": cfg, "cal_auroc": auc, "n_configs": len(cands),
            "thresholds": {r: {"thr": t, "p_tie": p,
                               "cal_obs_rate": float(fire(s[cal_obs], t, p, u[cal_obs]).mean())}
                           for r, (t, p) in thr.items()},
            "cal_auroc_by_config": aucs,
        }
    comps = [c for c in COMPARATORS if c in rec["detectors"]]
    rec["comparator"] = max(comps, key=lambda c: (rec["detectors"][c]["cal_auroc"],
                                                  -COMPARATORS.index(c))) if comps else None
    best_any = max(d["cal_auroc"] for d in rec["detectors"].values())
    rec["kill"] = bool(best_any < kill_auroc)
    rec["best_cal_auroc"] = best_any
    return rec


def gate_fires(rec: dict, detectors: Mapping[str, Mapping[str, np.ndarray]], name: str,
               rate: float, prompt_ids: Sequence[str]) -> np.ndarray:
    d = rec["detectors"][name]
    t = d["thresholds"][str(rate)]
    return fire(detectors[name][d["config"]], t["thr"], t["p_tie"], tie_u(prompt_ids))


def disagreement(rec: dict, detectors: Mapping[str, Mapping[str, np.ndarray]],
                 prompt_ids: Sequence[str], sets: Sequence[str]) -> np.ndarray:
    """Indices of TEST prompts where the headline and comparator gates differ at any rate."""
    if rec.get("comparator") is None or HEADLINE not in rec["detectors"]:
        return np.array([], int)
    sets = np.asarray(sets)
    dis = np.zeros(len(prompt_ids), bool)
    for r in rec["rates"]:
        a = gate_fires(rec, detectors, HEADLINE, r, prompt_ids)
        b = gate_fires(rec, detectors, rec["comparator"], r, prompt_ids)
        dis |= a != b
    return np.where(dis & (sets == "test"))[0]
