"""P24 analysis (laptop): results/rows.jsonl -> results/analysis.json.

Per donor kind K (X, Y, S, F, C) and patch site:
- toward: the patched output is strictly closer to K's alternative answer a'_K than to the
  target's answer (P06 f3_extra's statistic; an unparseable output counts as not toward);
- placebo: the R-patched output at the same site scored against the same a'_K (primary), and
  the unpatched output scored against a'_K (secondary);
- effect = toward - R placebo, paired by item, 10,000-resample item bootstrap (seed 20261012);
- exact: output == a'_K, minus the R placebo's exact rate;
- changed: output != the item's unpatched output (S against R tests "S moves less than R");
- lp shift: first-token log-prob of a'_K's first digit minus the target answer's, patched minus
  unpatched, on items where the two first digits differ.
Contrasts (paired, items having both kinds): Y - S, F - S, X - S, X - Y, and Y/X (ratio of mean
effects, bootstrapped). Subsets: all, both-correct (unpatched target right and donor right).

    uv run python -m p24.analyze [rows.jsonl] [analysis.json]     # from the probe dir
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

KINDS = ("X", "Y", "S", "F", "C", "P")
SITES = ("dots", "label", "early20", "all")
SEED = 20261012
N_BOOT = 10_000
CONTRASTS = (("Y", "S"), ("F", "S"), ("X", "S"), ("X", "Y"), ("P", "X"))


def toward(pred: int | None, alt: int, ans: int) -> float:
    return float(pred is not None and abs(pred - alt) < abs(pred - ans))


def boot(x: np.ndarray, rng: np.random.Generator, n: int = N_BOOT) -> dict:
    """Mean and percentile 95% CI of per-item values x (NaNs dropped)."""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    idx = rng.integers(0, len(x), size=(n, len(x)))
    m = x[idx].mean(1)
    return {"n": len(x), "mean": round(float(x.mean()), 4),
            "lo": round(float(np.percentile(m, 2.5)), 4),
            "hi": round(float(np.percentile(m, 97.5)), 4)}


def boot_ratio(a: np.ndarray, b: np.ndarray, rng: np.random.Generator, n: int = N_BOOT) -> dict:
    """mean(a) / mean(b) over the same items, item bootstrap."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    idx = rng.integers(0, len(a), size=(n, len(a)))
    den = b[idx].mean(1)
    r = np.where(den != 0, a[idx].mean(1) / np.where(den != 0, den, 1), np.nan)
    r = r[~np.isnan(r)]
    return {"n": len(a), "ratio": round(float(a.mean() / b.mean()), 4) if b.mean() else None,
            "lo": round(float(np.percentile(r, 2.5)), 4) if len(r) else None,
            "hi": round(float(np.percentile(r, 97.5)), 4) if len(r) else None}


def first_digit(v: int) -> int:
    return int(str(abs(v))[0])


def per_item(rec: dict) -> dict:
    """{(kind, site): {toward, plac_r, plac_u, exact, exact_r, changed, lp_shift}} and
    {kind: both_correct} for one item."""
    ans, ref = rec["answer"], rec["ref"]
    pat = {(p["kind"], p["site"]): p for p in rec["patched"]}
    out: dict = {}
    both: dict = {}
    for k in KINDS:
        if k not in rec["donors"]:
            continue
        d = rec["donors"][k]
        alt = d["a_alt"]
        both[k] = ref["pred"] == ans and d["pred"] == d["answer"]
        for s in SITES:
            p, r = pat.get((k, s)), pat.get(("R", s))
            if p is None or r is None:
                continue
            fa, fb = first_digit(alt), first_digit(ans)
            lp = (p["lp0"][fa] - p["lp0"][fb]) - (ref["lp0"][fa] - ref["lp0"][fb]) \
                if fa != fb else float("nan")
            out[(k, s)] = {
                "toward": toward(p["pred"], alt, ans),
                "plac_r": toward(r["pred"], alt, ans),
                "plac_u": toward(ref["pred"], alt, ans),
                "exact": float(p["pred"] == alt), "exact_r": float(r["pred"] == alt),
                "changed": float(p["text"] != ref["text"]),
                "changed_r": float(r["text"] != ref["text"]),
                "lp_shift": lp,
            }
    return {"cells": out, "both": both}


def eff(it: dict, k: str, s: str) -> float:
    c = it["cells"][(k, s)]
    return c["toward"] - c["plac_r"]


def merge(recs: list[dict]) -> list[dict]:
    """per_item over every record, cells merged by idx (pass 1 and pass 2 records of one item
    carry different sites), in first-seen order."""
    out: dict[int, dict] = {}
    for r in recs:
        it = per_item(r)
        m = out.setdefault(r["idx"], {"cells": {}, "both": {}})
        m["cells"].update(it["cells"])
        for k, v in it["both"].items():  # pass 1's correctness flags (pass 2 re-decodes donors)
            m["both"].setdefault(k, v)
    return list(out.values())


def summarise(recs: list[dict], seed: int = SEED, n_boot: int = N_BOOT) -> dict:
    """recs: pass 1 records (one per item), optionally followed by pass 2 records."""
    rng = np.random.default_rng(seed)
    items = merge(recs)
    first: dict[int, dict] = {}
    for r in recs:
        first.setdefault(r["idx"], r)
    recs = list(first.values())  # one record per item (pass 1's) for the item-level counts
    res: dict = {"n_items": len(recs),
                 "self_eq_ref": int(sum(r["self_eq_ref"] for r in recs)),
                 "refs_agree": int(sum(r["refs_agree"] for r in recs)),
                 "target_accuracy": round(float(np.mean([r["ref"]["pred"] == r["answer"]
                                                         for r in recs])), 4) if recs else None,
                 "donor_accuracy": {}, "cells": {}, "contrasts": {}, "ratio_Y_over_X": {}}
    for k in KINDS:
        rs = [r for r in recs if k in r["donors"]]
        if rs:
            res["donor_accuracy"][k] = round(float(np.mean(
                [r["donors"][k]["pred"] == r["donors"][k]["answer"] for r in rs])), 4)

    def b(x):
        return boot(np.array(x, dtype=float), rng, n_boot)

    for subset in ("all", "both_correct"):
        for k in KINDS:
            for s in SITES:
                cs = [it["cells"][(k, s)] for it in items if (k, s) in it["cells"]
                      and (subset == "all" or it["both"][k])]
                if not cs:
                    continue
                col = {f: np.array([c[f] for c in cs], dtype=float) for f in cs[0]}
                res["cells"][f"{subset}/{k}/{s}"] = {
                    "toward": b(col["toward"]), "placebo_R": b(col["plac_r"]),
                    "placebo_unpatched": b(col["plac_u"]),
                    "effect": b(col["toward"] - col["plac_r"]),
                    "effect_vs_unpatched": b(col["toward"] - col["plac_u"]),
                    "exact": b(col["exact"]), "exact_minus_R": b(col["exact"] - col["exact_r"]),
                    "changed": b(col["changed"]), "changed_R": b(col["changed_r"]),
                    "changed_minus_R": b(col["changed"] - col["changed_r"]),
                    "lp_shift": b(col["lp_shift"]),
                }
    for s in SITES:
        for a, c in CONTRASTS:
            sel = [it for it in items if (a, s) in it["cells"] and (c, s) in it["cells"]]
            if not sel:
                continue
            res["contrasts"][f"{a}-{c}/{s}"] = b([eff(it, a, s) - eff(it, c, s) for it in sel])
        sel = [it for it in items if ("X", s) in it["cells"] and ("Y", s) in it["cells"]]
        if sel:
            ey = [eff(it, "Y", s) for it in sel]
            ex = [eff(it, "X", s) for it in sel]
            res["ratio_Y_over_X"][s] = boot_ratio(np.array(ey), np.array(ex), rng, n_boot)
    res["outcomes"] = outcomes(res)
    return res


def outcomes(res: dict) -> dict:
    """The plan's predictions (a)-(f), evaluated at the dots unless stated."""
    def cell(k, s="dots", f="effect"):
        return res["cells"].get(f"all/{k}/{s}", {}).get(f, {})

    def ge(c, v):
        return c.get("mean") is not None and c["mean"] >= v

    def excl0(c):
        return c.get("lo") is not None and c["lo"] > 0

    yx, s_, f_ = cell("Y"), cell("S"), cell("F")
    ys = res["contrasts"].get("Y-S/dots", {})
    b_ = ge(yx, 0.10) and excl0(yx)
    s_null = s_.get("mean") is not None and s_["mean"] <= 0.05 and s_["hi"] <= 0.10
    return {
        "a_X_replicates (X - placebo >= 0.20)": ge(cell("X"), 0.20),
        "a_P_replicates_P06 (P - placebo >= 0.20)": ge(cell("P"), 0.20),
        "b_Y_moves (>= 0.10, CI excl 0)": b_,
        "c_y_pattern (b, S <= 0.05 with hi <= 0.10, Y - S >= 0.10 CI excl 0)":
            b_ and s_null and ge(ys, 0.10) and excl0(ys),
        "d_copy_pattern (Y < 0.05 and S >= 0.10)":
            yx.get("mean") is not None and yx["mean"] < 0.05 and ge(s_, 0.10),
        "e_F_moves (>= 0.10)": ge(f_, 0.10),
        "f_label_moves_X_or_Y (>= 0.10)": ge(cell("X", "label"), 0.10)
        or ge(cell("Y", "label"), 0.10),
        "f_early20_moves_X_or_Y (>= 0.10)": ge(cell("X", "early20"), 0.10)
        or ge(cell("Y", "early20"), 0.10),
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    here = Path(__file__).resolve().parents[1] / "results"
    src = Path(argv[0]) if argv else here / "rows.jsonl"
    dst = Path(argv[1]) if len(argv) > 1 else here / "analysis.json"
    recs = [json.loads(x) for x in src.read_text().splitlines() if x.strip()]
    sites = src.with_name("rows_sites.jsonl")
    if not argv and sites.exists():
        recs += [json.loads(x) for x in sites.read_text().splitlines() if x.strip()]
    res = summarise(recs)
    dst.write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps({k: res[k] for k in ("n_items", "self_eq_ref", "refs_agree",
                                          "target_accuracy", "outcomes")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
