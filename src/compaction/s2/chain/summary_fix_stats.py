"""Week 2, experiment 5 ("can a better compaction summary stop the precedent?"): primary analysis.

A row is one run: `{"history", "replicate", "arm", "teammate_lost"}`, `teammate_lost` True/False, or
None when the slot is unknown (unfinished, failed, quota-cut; every assigned slot is kept). Arms
other than native/ledger/line/placebo are ignored. A unit is one history x one replicate; a
contrast uses the units where both arms have a row.

- `paired_diffs`: per history D_h = sum over its units of (base lost - fix lost). Unknowns are
  dropped (`fill=None`, complete case) or filled to the value that makes the fix look worst
  ("worst": fix lost, base kept) or best ("best") -- experiment 1's rule: the test runs at both
  completions, and an effect counts only if both reject and the bounds exclude zero.
- `sign_flip_p`: exact two-sided sign-flip p over the 2^k sign patterns of the nonzero D_h (the
  plan appendix's `p_two`, same 1e-9 tolerance).
- `holm`: Holm step-down.
- `cluster_bootstrap_ci`: history-cluster bootstrap of the paired risk difference (fix - base),
  in points.
- `primary`: ledger and line against native at both completions and complete case, Holm across
  the two fixes on the worse-completion p (Holm is monotone in p, so a rejection there is a
  rejection at each completion); "significant" also needs both completions' bootstrap CIs to
  exclude 0 on the same side. Secondary, outside Holm: ledger - placebo <= -15 points with
  unadjusted p <= 0.05 at both completions; placebo - native reported.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

ARMS = ("native", "ledger", "line", "placebo")
FIXES = ("ledger", "line")
FILLS = (None, "worst", "best")
COMPLETIONS = ("worst", "best")
SEED = 20261029
ALPHA = 0.05
CONTENT_PTS = -15.0
MAX_K = 24


def _index(rows) -> dict[tuple[str, int], dict[str, bool | None]]:
    """{(history, replicate): {arm: lost}} for the four arms; a repeated (history, replicate,
    arm) raises. `replicate` is the experiment's replicate (summary_fix rows), not experiment
    1's `rep` (the history's own index, the same in every replicate)."""
    out: dict[tuple[str, int], dict[str, bool | None]] = defaultdict(dict)
    for r in rows:
        if r["arm"] not in ARMS:
            continue
        unit = (str(r["history"]), int(r["replicate"]))
        if r["arm"] in out[unit]:
            raise ValueError(f"duplicate run: {unit} {r['arm']}")
        v = r["teammate_lost"]
        if v is not None and not isinstance(v, bool | np.bool_):
            raise ValueError(f"teammate_lost must be True/False/None: {unit} {r['arm']} {v!r}")
        out[unit][r["arm"]] = None if v is None else bool(v)
    return out


def _units(rows, fix: str, base: str, fill) -> dict[str, list[tuple[int, int]]]:
    """{history: [(base lost, fix lost), ...]} over the units both arms ran, after `fill`."""
    if fill not in FILLS:
        raise ValueError(f"fill must be one of {FILLS}: {fill!r}")
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for (hist, _rep), arms in sorted(_index(rows).items()):
        if base not in arms or fix not in arms:
            continue
        b, f = arms[base], arms[fix]
        if b is None or f is None:
            if fill is None:
                continue
            worst = fill == "worst"
            b = (not worst) if b is None else b
            f = worst if f is None else f
        out[hist].append((int(b), int(f)))
    return dict(out)


def paired_diffs(rows, fix: str, base: str = "native", fill=None) -> dict[str, int]:
    """{history: D_h}, D_h = sum over units of (base lost - fix lost); histories with no unit
    are left out."""
    return {h: sum(b - f for b, f in us) for h, us in _units(rows, fix, base, fill).items()}


def sign_flip_p(diffs) -> float:
    """Exact two-sided sign-flip p over the nonzero diffs (dict values or a sequence)."""
    d = np.asarray(list(diffs.values()) if isinstance(diffs, dict) else list(diffs), float)
    nz = d[d != 0]
    if len(nz) == 0:
        return 1.0
    if len(nz) > MAX_K:
        raise ValueError(f"{len(nz)} nonzero diffs; exact enumeration capped at {MAX_K}")
    sums = np.zeros(1)
    for x in nz:
        sums = np.concatenate([sums + x, sums - x])
    return float(np.mean(np.abs(sums) >= abs(nz.sum()) - 1e-9))


def holm(pvals: dict[str, float], alpha: float = ALPHA) -> dict[str, dict]:
    """Holm step-down: {name: {"p", "p_adj", "reject"}}; p_adj is the monotone adjusted p."""
    names = sorted(pvals, key=lambda k: (pvals[k], k))
    m = len(names)
    out, running, stopped = {}, 0.0, False
    for i, k in enumerate(names):
        running = max(running, min(1.0, (m - i) * pvals[k]))
        stopped = stopped or pvals[k] > alpha / (m - i)
        out[k] = {"p": float(pvals[k]), "p_adj": float(running), "reject": not stopped}
    return out


def cluster_bootstrap_ci(rows, fix: str, base: str = "native", n: int = 10000,
                         seed: int = SEED, level: float = 0.95, fill=None) -> dict | None:
    """History-cluster bootstrap of mean over units of (fix lost - base lost) x 100.

    Returns {"est_pts", "lo", "hi", "n_hist", "n_units", "level", "n", "seed"}, or None with no
    units."""
    units = _units(rows, fix, base, fill)
    hists = sorted(units)
    if not hists:
        return None
    s = np.array([sum(f - b for b, f in units[h]) for h in hists], float)
    c = np.array([len(units[h]) for h in hists], float)
    idx = np.random.default_rng(seed).integers(0, len(hists), size=(n, len(hists)))
    stats = 100.0 * s[idx].sum(1) / c[idx].sum(1)
    q = (1.0 - level) / 2.0
    lo, hi = np.quantile(stats, [q, 1.0 - q])
    return {"est_pts": float(100.0 * s.sum() / c.sum()), "lo": float(lo), "hi": float(hi),
            "n_hist": len(hists), "n_units": int(c.sum()), "level": level, "n": n, "seed": seed}


def _risk(rows) -> dict[str, dict]:
    """Per arm: assigned slots, known runs, lost runs, risk among known."""
    out = {}
    for arm in ARMS:
        vals = [r["teammate_lost"] for r in rows if r["arm"] == arm]
        known = [bool(v) for v in vals if v is not None]
        out[arm] = {"assigned": len(vals), "known": len(known), "lost": sum(known),
                    "risk": sum(known) / len(known) if known else None}
    return out


def contrast(rows, fix: str, base: str = "native", **boot) -> dict:
    """fix - base at complete case and both completions: points, T, D_h, p, bootstrap CI."""
    out = {}
    for fill in FILLS:
        d = paired_diffs(rows, fix, base, fill)
        units = _units(rows, fix, base, fill)
        n_units = sum(len(v) for v in units.values())
        out[fill or "complete"] = {
            "diff_pts": -100.0 * sum(d.values()) / n_units if n_units else None,
            "T": int(sum(d.values())), "D": d, "k_nonzero": sum(1 for v in d.values() if v),
            "n_hist": len(d), "n_units": n_units, "p": sign_flip_p(d),
            "ci": cluster_bootstrap_ci(rows, fix, base, fill=fill, **boot),
        }
    return out


def _ci_excludes_zero(c: dict) -> int:
    """+1 / -1 when every completion's CI lies above / below 0, else 0."""
    sides = set()
    for comp in COMPLETIONS:
        ci = c[comp]["ci"]
        sides.add(0 if ci is None else 1 if ci["lo"] > 0 else -1 if ci["hi"] < 0 else 0)
    return sides.pop() if len(sides) == 1 else 0


def primary(rows, alpha: float = ALPHA, **boot) -> dict:
    """The experiment's primary readout, JSON-serializable."""
    rows = list(rows)
    fixes = {f: contrast(rows, f, **boot) for f in FIXES}
    worse = {f: max(fixes[f][c]["p"] for c in COMPLETIONS) for f in FIXES}
    h = holm(worse, alpha)
    for f in FIXES:
        side = _ci_excludes_zero(fixes[f])
        fixes[f]["holm"] = h[f]
        fixes[f]["ci_excludes_zero"] = side
        fixes[f]["significant"] = bool(h[f]["reject"] and side != 0)
        fixes[f]["reduces_loss"] = bool(h[f]["reject"] and side == -1)
    lp = contrast(rows, "ledger", "placebo", **boot)
    secondary = all(lp[c]["diff_pts"] is not None and lp[c]["diff_pts"] <= CONTENT_PTS
                    and lp[c]["p"] <= alpha for c in COMPLETIONS)
    # the plan: "any claim of improvement first needs its native contrast"
    content = bool(secondary and fixes["ledger"]["reduces_loss"])
    return {
        "risk": _risk(rows),
        "fixes": fixes,
        "holm_basis": "worse-completion p across ledger and line",
        "ledger_vs_placebo": {**lp, "content_works": content,
                              "secondary_criterion_met": secondary, "bar_pts": CONTENT_PTS,
                              "alpha_unadjusted": alpha},
        "placebo_vs_native": contrast(rows, "placebo", **boot),
        "alpha": alpha,
    }
