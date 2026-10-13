"""P32 analysis (plan "Measure"; scoring and distance rules in log.md, fixed before bulk data).

    uv run python -m p32.analyze      # from probes/P32-filler-mc-candidates -> results/analysis.json

Definitions, per family f, kind k (near/far), placement p (before/after), baseline arm b
(base/padmid/pad), option count n:
    share s(n) = (acc_dots(n) - acc_b(n)) / (1 - acc_b(n)),  dots = dots_<p>
    Delta s    = s(10) - s(2);  slope = OLS slope of s on log2 n over n = 2, 5, 10
Item sets: "valid" = items whose rows are valid (no visible CoT, witness ok) in every cell of that
contrast (b and dots at all three n), so the set is the same across n; "all" = every item, invalid
rows scored wrong (P06's rule). Intervals: item bootstrap stratified by family (each resample
redraws items with all their rows), B_BOOT resamples, seed 0, percentile 95%.
Primary: f pooled (mean of the two families' Delta s), k = near, p = before, b = base, "valid".
Verdict: shrinks if Delta s <= -0.10 and its upper bound < 0; survives if its lower bound > -0.10;
else unclear. A shrinks verdict is read as guess-and-check only if it also holds with b = padmid.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from p06 import api

import p32  # noqa: F401

from .items import KINDS, OPTION_COUNTS, load_all, options, rng
from .run import PASSK_CONDS, PASSK_DRAWS, RES

FAMILIES = ("varbind", "chain")
PLACEMENTS = ("before", "after")
BASELINES = ("base", "padmid", "pad")
B_BOOT = 10_000
MARGIN = 0.10
LOG2N = np.log2(np.array(OPTION_COUNTS, dtype=float))


# ------------------------------------------------------------------------------------ loading
def load(path: Path, temperature: float = 0.0) -> dict[tuple[str, str, int], dict]:
    """(item_id, condition, draw) -> row, for ok rows at the given temperature."""
    out = {}
    for r in api.latest_ok(path).values():
        if r["temperature"] == temperature:
            out[(r["item_id"], r["arm"], r["draw"])] = r
    return out


def cells(rows: dict) -> dict[str, dict[str, dict[str, tuple[int, int]]]]:
    """family -> item_id -> condition -> (correct, valid)."""
    out = defaultdict(lambda: defaultdict(dict))
    for (iid, cond, draw), r in rows.items():
        if draw == 0:
            out[r["bank"]][iid][cond] = (int(r["correct"]), int(r["valid"]))
    return out


# ---------------------------------------------------------------------------------- statistics
def share(dots: np.ndarray, base: np.ndarray) -> float:
    """Share of headroom from per-item 0/1 arrays (nan if base is at ceiling)."""
    b = base.mean()
    return float((dots.mean() - b) / (1 - b)) if b < 1 else float("nan")


def logodds(p: float, n: int) -> float:
    p = min(max(p, 0.5 / n), 1 - 0.5 / n)
    return math.log(p / (1 - p))


def slope(y: np.ndarray) -> float:
    x = LOG2N - LOG2N.mean()
    return float((x * (y - y.mean())).sum() / (x * x).sum())


def verdict(point: float, lo: float, hi: float) -> str:
    if point <= -MARGIN and hi < 0:
        return "shrinks"
    if lo > -MARGIN:
        return "survives"
    return "unclear"


def ci(a: np.ndarray) -> list[float]:
    a = a[~np.isnan(a)]
    return [float(np.percentile(a, 2.5)), float(np.percentile(a, 97.5))] if len(a) else [None, None]


def contrast_arrays(c: dict, fam: str, kind: str, place: str, base: str, item_set: str):
    """Per-item 0/1 arrays [n_items, 3 n] for the baseline and the dots arm, and the item ids."""
    dots_arm = f"dots_{place}"
    conds_b = [f"{n}|{kind}|{base}" for n in OPTION_COUNTS]
    conds_d = [f"{n}|{kind}|{dots_arm}" for n in OPTION_COUNTS]
    ids, B, D = [], [], []
    for iid, cc in sorted(c[fam].items()):
        if not all(x in cc for x in conds_b + conds_d):
            continue
        if item_set == "valid" and not all(cc[x][1] for x in conds_b + conds_d):
            continue
        ids.append(iid)
        B.append([cc[x][0] for x in conds_b])
        D.append([cc[x][0] for x in conds_d])
    return ids, np.array(B, dtype=float).reshape(-1, 3), np.array(D, dtype=float).reshape(-1, 3)


def shares(B: np.ndarray, D: np.ndarray) -> np.ndarray:
    b, d = B.mean(0), D.mean(0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b < 1, (d - b) / (1 - b), np.nan)


def boot_index(n: int, rs: np.random.Generator) -> np.ndarray:
    return rs.integers(0, n, size=(B_BOOT, n))


def contrast(c: dict, kind: str, place: str, base: str, item_set: str, seed: int = 0) -> dict:
    """Per family and pooled: accuracies, uplift, log-odds uplift, share per n, Delta s, slope."""
    rs = np.random.default_rng(seed)
    out, boots = {}, {}
    for fam in FAMILIES:
        ids, B, D = contrast_arrays(c, fam, kind, place, base, item_set)
        if len(ids) == 0:
            out[fam] = {"n_items": 0}
            continue
        s = shares(B, D)
        idx = boot_index(len(ids), rs)
        bs = np.array([shares(B[i], D[i]) for i in idx])  # [B_BOOT, 3]
        up = D - B
        bup = np.array([up[i].mean(0) for i in idx])
        ds = bs[:, 2] - bs[:, 0]
        sl = np.array([slope(r) if not np.isnan(r).any() else np.nan for r in bs])
        boots[fam] = (ds, sl)
        out[fam] = {
            "n_items": len(ids),
            "per_n": {str(n): {"acc_base": float(B[:, j].mean()), "acc_dots": float(D[:, j].mean()),
                               "uplift": float(up[:, j].mean()), "uplift_ci": ci(bup[:, j]),
                               "logodds_uplift": logodds(D[:, j].mean(), len(ids))
                               - logodds(B[:, j].mean(), len(ids)),
                               "share": float(s[j]), "share_ci": ci(bs[:, j])}
                      for j, n in enumerate(OPTION_COUNTS)},
            "delta_share": float(s[2] - s[0]), "delta_share_ci": ci(ds),
            "slope": slope(s), "slope_ci": ci(sl),
        }
    if all(f in boots for f in FAMILIES):
        pt = float(np.mean([out[f]["delta_share"] for f in FAMILIES]))
        pb = np.mean([boots[f][0] for f in FAMILIES], axis=0)
        lo, hi = ci(pb)
        out["pooled"] = {"delta_share": pt, "delta_share_ci": [lo, hi],
                         "slope": float(np.mean([out[f]["slope"] for f in FAMILIES])),
                         "slope_ci": ci(np.mean([boots[f][1] for f in FAMILIES], axis=0)),
                         "verdict": verdict(pt, lo, hi)}
    for fam in FAMILIES:
        if fam in boots:
            lo, hi = out[fam]["delta_share_ci"]
            out[fam]["verdict"] = verdict(out[fam]["delta_share"], lo, hi)
    return out


# ----------------------------------------------------------------------- bounded-checking model
def _ll(k: np.ndarray, m: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float((k * np.log(p) + (m - k) * np.log(1 - p)).sum())


def _pred(b: np.ndarray, v: float, cap: float) -> np.ndarray:
    return b + v * np.minimum(1.0, cap / np.array(OPTION_COUNTS)) * (1 - b)


V_GRID = np.linspace(0, 1, 201)
B_GRID = np.round(np.arange(1.0, 10.0001, 0.05), 2)


def fit(B: np.ndarray, D: np.ndarray, constant: bool) -> tuple[float, float]:
    b, k, m = B.mean(0), D.sum(0), np.full(3, len(D))
    best = (-np.inf, 0.0, 10.0)
    for cap in ([10.0] if constant else B_GRID):
        for v in V_GRID:
            ll = _ll(k, m, _pred(b, v, cap))
            if ll > best[0]:
                best = (ll, float(v), float(cap))
    return best[1], best[2]


def model_compare(c: dict, kind: str = "near", place: str = "before", base: str = "base",
                  item_set: str = "valid") -> dict:
    """Bounded checking s(n) = v * min(1, cap / n) vs constant share (cap = 10), fitted on one half
    of the items (seeded split) and scored by the log-likelihood of the other half's dots outcomes
    given that half's own base rates; both directions."""
    out = {}
    for fam in FAMILIES:
        ids, B, D = contrast_arrays(c, fam, kind, place, base, item_set)
        half = np.array([rng("split", i).random() < 0.5 for i in ids], dtype=bool)
        res = {}
        for name, tr, te in (("fit_A_test_B", half, ~half), ("fit_B_test_A", ~half, half)):
            v1, cap = fit(B[tr], D[tr], constant=False)
            v0, _ = fit(B[tr], D[tr], constant=True)
            bt, kt, mt = B[te].mean(0), D[te].sum(0), np.full(3, int(te.sum()))
            res[name] = {"bounded": {"v": v1, "cap": cap,
                                     "heldout_ll": _ll(kt, mt, _pred(bt, v1, cap)),
                                     "pred_acc": _pred(bt, v1, cap).tolist()},
                         "constant": {"v": v0, "heldout_ll": _ll(kt, mt, _pred(bt, v0, 10.0)),
                                      "pred_acc": _pred(bt, v0, 10.0).tolist()},
                         "obs_acc": (kt / mt).tolist(), "n_test": int(te.sum())}
            res[name]["delta_ll_bounded_minus_constant"] = (
                res[name]["bounded"]["heldout_ll"] - res[name]["constant"]["heldout_ll"])
        out[fam] = res
    return out


# ---------------------------------------------------------------------------------- secondary
def free_response(c: dict, seed: int = 0) -> dict:
    rs = np.random.default_rng(seed)
    out = {}
    for fam in FAMILIES:
        arms = ("base", "pad", "padmid", "dots")
        ids = [i for i, cc in c[fam].items() if all(f"0|-|{a}" in cc for a in arms)]
        A = {a: np.array([c[fam][i][f"0|-|{a}"][0] for i in ids], dtype=float) for a in arms}
        V = {a: np.array([c[fam][i][f"0|-|{a}"][1] for i in ids], dtype=float) for a in arms}
        idx = boot_index(len(ids), rs)
        o = {"n_items": len(ids), "acc": {a: float(A[a].mean()) for a in arms},
             "invalid": {a: float(1 - V[a].mean()) for a in arms}}
        for x, y in (("dots", "base"), ("dots", "padmid"), ("dots", "pad"), ("padmid", "base"),
                     ("pad", "base")):
            d = A[x] - A[y]
            o[f"{x}_minus_{y}"] = {"diff": float(d.mean()), "ci": ci(d[idx].mean(1))}
        out[fam] = o
    return out


def invalid_rates(rows: dict) -> dict:
    tot, bad = Counter(), Counter()
    for (_, cond, draw), r in rows.items():
        if draw == 0:
            tot[(r["bank"], cond)] += 1
            bad[(r["bank"], cond)] += int(not r["valid"])
    return {f"{f}|{c}": {"n": tot[(f, c)], "invalid": bad[(f, c)] / tot[(f, c)]}
            for f, c in sorted(tot)}


def prompt_tokens(rows: dict) -> dict:
    t = defaultdict(list)
    for (_, cond, draw), r in rows.items():
        if draw == 0:
            t[(r["bank"], cond)].append((r.get("usage") or {}).get("prompt_tokens") or 0)
    return {f"{f}|{c}": int(np.median(v)) for (f, c), v in sorted(t.items())}


def attraction(rows: dict, fams: dict) -> dict:
    """Where MC answers land: the answer, a listed distractor, an unlisted member of the same pool
    (built the same way; the unlisted controls), or anything else. Rates per listed distractor and
    per unlisted pool member make the two comparable. Free response: the rate of answering any
    pool member (all unlisted) as the baseline attraction."""
    items = {e["item_id"]: e for evals, _ in fams.values() for e in evals}
    acc = defaultdict(Counter)
    for (iid, cond, draw), r in rows.items():
        if draw != 0:
            continue
        it = items[iid]
        pred = (r.get("graded") or {}).get("predicted")
        n, kind, _ = cond.split("|")
        n = int(n)
        key = (r["bank"], cond)
        acc[key]["rows"] += 1
        if n == 0:
            for k in KINDS:
                acc[key][f"pool_{k}"] += int(pred in it["pools"][k])
            continue
        listed = set(options(it, kind, n)) - {it["answer"]}
        unlisted = set(it["pools"][kind]) - listed
        if pred == it["answer"]:
            acc[key]["answer"] += 1
        elif pred in listed:
            acc[key]["listed"] += 1
        elif pred in unlisted:
            acc[key]["unlisted_pool"] += 1
        elif pred is None:
            acc[key]["none"] += 1
        else:
            acc[key]["off_list_other"] += 1
    out = {}
    for (fam, cond), cn in sorted(acc.items()):
        n = int(cond.split("|")[0])
        m = cn["rows"]
        o = {k: v / m for k, v in cn.items() if k != "rows"}
        o["rows"] = m
        if n:
            o["per_listed_distractor"] = cn["listed"] / m / (n - 1)
            nu = 9 - (n - 1)
            o["per_unlisted_pool_member"] = cn["unlisted_pool"] / m / nu if nu else None
        out[f"{fam}|{cond}"] = o
    return out


def link(c: dict) -> dict:
    """Item-level: does solving it in free response predict MC success (10 near options)?"""
    out = {}
    for fam in FAMILIES:
        o = {}
        for free_arm, mc_arm in (("base", "base"), ("dots", "dots_before"), ("dots", "dots_after"),
                                 ("padmid", "padmid")):
            tab = Counter()
            for cc in c[fam].values():
                f, m = cc.get(f"0|-|{free_arm}"), cc.get(f"10|near|{mc_arm}")
                if f and m:
                    tab[(f[0], m[0])] += 1
            p1 = tab[(1, 1)] / max(1, tab[(1, 1)] + tab[(1, 0)])
            p0 = tab[(0, 1)] / max(1, tab[(0, 1)] + tab[(0, 0)])
            o[f"free_{free_arm}->mc10near_{mc_arm}"] = {
                "p_mc_given_free_right": p1, "p_mc_given_free_wrong": p0,
                "counts": {f"free{a}_mc{b}": tab[(a, b)] for a in (0, 1) for b in (0, 1)}}
        out[fam] = o
    return out


def passk(path: Path, seed: int = 0) -> dict:
    rows = load(path, temperature=1.0)
    by = defaultdict(lambda: defaultdict(dict))
    fam_of = {}
    for (iid, cond, draw), r in rows.items():
        by[iid][cond][draw] = (int(r["correct"]), int(r["valid"]))
        fam_of[iid] = r["bank"]
    rs = np.random.default_rng(seed)
    out = {}
    for fam in FAMILIES:
        ids = [i for i in by if fam_of[i] == fam
               and all(len(by[i][c]) == PASSK_DRAWS for c in PASSK_CONDS)]
        if not ids:
            continue
        o = {"n_items": len(ids)}
        p1, p8, inv = {}, {}, {}
        for cnd in PASSK_CONDS:
            arm = cnd.split("|")[2]
            m = np.array([[by[i][cnd][d][0] for d in range(PASSK_DRAWS)] for i in ids], dtype=float)
            v = np.array([[by[i][cnd][d][1] for d in range(PASSK_DRAWS)] for i in ids], dtype=float)
            p1[arm], p8[arm], inv[arm] = m.mean(1), m.max(1), 1 - v.mean()
        idx = boot_index(len(ids), rs)
        for name, s in (("pass1", p1), ("pass8", p8)):
            d = s["dots"] - s["base"]
            b = s["base"].mean()
            sh = [(s["dots"][i].mean() - s["base"][i].mean()) / (1 - s["base"][i].mean())
                  if s["base"][i].mean() < 1 else np.nan for i in idx]
            o[name] = {"base": float(b), "dots": float(s["dots"].mean()), "uplift": float(d.mean()),
                       "uplift_ci": ci(d[idx].mean(1)),
                       "share": float((s["dots"].mean() - b) / (1 - b)) if b < 1 else None,
                       "share_ci": ci(np.array(sh))}
        o["invalid_rate"] = {a: float(x) for a, x in inv.items()}
        out[fam] = o
    return out


def f1_table(c: dict, f1_rows: dict, seed: int = 0) -> dict:
    """F1: free-response accuracy of every span arm (main: base, pad, padmid, dots; f1: the six
    variants), with paired differences against dots and against base (item bootstrap)."""
    rs = np.random.default_rng(seed)
    f1c = cells(f1_rows)
    out = {}
    for fam in FAMILIES:
        arms = {a: c[fam] for a in ("base", "pad", "padmid", "dots")}
        f1_arms = sorted({cond.split("|")[2] for cc in f1c[fam].values() for cond in cc})
        ids = [i for i in c[fam] if all(f"0|-|{a}" in c[fam][i] for a in arms)
               and all(f"0|-|{a}" in f1c[fam].get(i, {}) for a in f1_arms)]
        acc = {a: np.array([c[fam][i][f"0|-|{a}"][0] for i in ids], dtype=float) for a in arms}
        acc |= {a: np.array([f1c[fam][i][f"0|-|{a}"][0] for i in ids], dtype=float)
                for a in f1_arms}
        inv = {a: float(np.mean([1 - f1c[fam][i][f"0|-|{a}"][1] for i in ids])) for a in f1_arms}
        idx = boot_index(len(ids), rs)
        o = {"n_items": len(ids), "invalid": inv}
        for a, v in acc.items():
            o[a] = {"acc": float(v.mean())}
            for ref in ("dots", "base"):
                if a != ref:
                    d = v - acc[ref]
                    o[a][f"minus_{ref}"] = float(d.mean())
                    o[a][f"minus_{ref}_ci"] = ci(d[idx].mean(1))
        out[fam] = o
    return out


def padmid_as_filler(c: dict, kind: str, item_set: str) -> dict:
    """The same share contrast with padmid (prose in the filler's place) as the filler arm."""
    c2 = {f: {i: {**cc, **{k.replace("|padmid", "|dots_mid"): v for k, v in cc.items()
                           if k.endswith("|padmid")}} for i, cc in d.items()} for f, d in c.items()}
    return contrast(c2, kind, "mid", "base", item_set)


def main():
    fams = load_all()
    rows = load(RES / "main.jsonl")
    c = cells(rows)
    res = {"primary": contrast(c, "near", "before", "base", "valid"),
           "contrasts": {}}
    for kind in KINDS:
        for place in PLACEMENTS:
            for base in BASELINES:
                for item_set in ("valid", "all"):
                    res["contrasts"][f"{kind}|{place}|{base}|{item_set}"] = contrast(
                        c, kind, place, base, item_set)
    res["padmid_as_filler"] = {f"{k}|{s}": padmid_as_filler(c, k, s) for k in KINDS
                               for s in ("valid", "all")}
    res["model_compare"] = {f"near|before|{b}": model_compare(c, base=b) for b in ("base", "padmid")}
    res["model_compare"]["near|after|base"] = model_compare(c, place="after")
    res["free_response"] = free_response(c)
    res["invalid_rates"] = invalid_rates(rows)
    res["prompt_tokens_median"] = prompt_tokens(rows)
    res["attraction"] = attraction(rows, fams)
    res["link"] = link(c)
    if (RES / "f1.jsonl").exists():
        res["f1"] = f1_table(c, load(RES / "f1.jsonl"))
    if (RES / "f2.jsonl").exists():
        res["f2"] = f1_table(c, load(RES / "f2.jsonl"))
    if (RES / "passk.jsonl").exists():
        res["passk"] = passk(RES / "passk.jsonl")
    (RES / "analysis.json").write_text(json.dumps(res, indent=1) + "\n")
    p = res["primary"]
    print("PRIMARY (near, options before filler, base, valid items):")
    for f in FAMILIES:
        if p[f].get("n_items"):
            print(f"  {f}: n={p[f]['n_items']} shares " + ", ".join(
                f"{n}:{p[f]['per_n'][str(n)]['share']:+.3f}" for n in OPTION_COUNTS)
                  + f"  Δs {p[f]['delta_share']:+.3f} {p[f]['delta_share_ci']} {p[f]['verdict']}")
    if "pooled" in p:
        print(f"  pooled Δs {p['pooled']['delta_share']:+.3f} {p['pooled']['delta_share_ci']} "
              f"-> {p['pooled']['verdict']}")
    for key, v in res["contrasts"].items():
        if "pooled" in v:
            print(f"{key:28s} Δs {v['pooled']['delta_share']:+.3f} "
                  f"[{v['pooled']['delta_share_ci'][0]:+.3f}, {v['pooled']['delta_share_ci'][1]:+.3f}]"
                  f" {v['pooled']['verdict']:9s} | " + " | ".join(
                      f"{f} " + " ".join(f"{v[f]['per_n'][str(n)]['acc_base']:.2f}>"
                                         f"{v[f]['per_n'][str(n)]['acc_dots']:.2f}"
                                         for n in OPTION_COUNTS) for f in FAMILIES))


if __name__ == "__main__":
    main()
