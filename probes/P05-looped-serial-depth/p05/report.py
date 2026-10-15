"""P05 report: everything the note needs, from a pulled results/ directory.

Plan: probes/P05-looped-serial-depth/plan.md ("Measure", "Predictions"). Reads results/{tf,
greedy,readout,ask}/ and chance.json; writes results/summary.json (p05.analyze.summarize plus the
validation, floor call, readability, ask baseline, state convergence) and results/report.md.

    uv run python -m p05.report results
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import sys
from pathlib import Path

import numpy as np

from . import analyze as A
from . import items as I


def read_rows(d: Path) -> list[dict]:
    rows = []
    for p in sorted(d.glob("*.jsonl")):
        rows += [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    return rows


def validation(greedy: list[dict]) -> dict:
    """Greedy vs teacher-forced agreement, bare-number share, accuracies, per cell and r."""
    g = collections.defaultdict(list)
    for r in greedy:
        g[(r["model"], r["seed"], r["bank"], r["fmt"], r["r"])].append(r)
    out = {}
    for (m, s, b, f, r), rr in sorted(g.items()):
        out[f"{m}/s{s}/{b}/{f}/r{r}"] = {
            "n": len(rr), "agree": float(np.mean([x["correct"] == bool(x["tf_correct"])
                                                   for x in rr])),
            "bare": float(np.mean([x["bare_number"] for x in rr])),
            "acc_greedy": float(np.mean([x["correct"] for x in rr])),
            "acc_tf": float(np.mean([bool(x["tf_correct"]) for x in rr]))}
    return out


def substitute_greedy(rows: list[dict], greedy_full: list[dict]) -> tuple[list[dict], list]:
    """Plan: a configuration under 98% agreement is scored by greedy generation instead."""
    sub = {(x["model"], x["seed"], x["bank"], x["fmt"], x["r"], x["item_id"]): x["correct"]
           for x in greedy_full}
    keys = sorted({k[:5] for k in sub})
    out = []
    for r in rows:
        k = (r["model"], r["seed"], r["bank"], r["fmt"], r["r"], r["item_id"])
        out.append({**r, "correct": sub[k]} if k in sub else r)
    return out, keys


def floor_call(summary: dict, bound: float = 1.5) -> dict:
    """Plan outcome 1: the baseline and the recurrent model (seed 0) at every r have d50 upper
    CI bounds <= 1.5 (or never cross 50%: status below), on chain and the easier fallback, per
    format."""
    out = {}
    for fmt in I.FORMATS:
        verdicts = {}
        for bank in ("chain", "chain_easy"):
            cell = summary.get(f"{bank}/{fmt}")
            if not cell:
                verdicts[f"{bank}:missing"] = False  # a floor call needs both banks
                continue
            for key, cf in cell["configs"].items():
                if not key.startswith(("rec32/s0/", "base/")):
                    continue
                hi = cf["ci"][1]
                try:
                    hi = float(hi)
                except (TypeError, ValueError):
                    hi = float("nan")
                verdicts[f"{bank}:{key}"] = cf["status"] == "below" or hi <= bound
        out[fmt] = {"all_floored": bool(verdicts) and all(verdicts.values()),
                    "per_config": verdicts}
    return out


def per_k_contrasts(rows: list[dict], pairs: tuple, n_boot: int = 2000, seed: int = 0) -> dict:
    """Paired accuracy difference per k, a - b, over the same items (item bootstrap, seed 0):
    the direct comparison where d50 sits at the bottom of the range."""
    rng = np.random.default_rng(seed)
    out = {}
    cells = sorted({(r["bank"], r["fmt"]) for r in rows})
    for bank, fmt in cells:
        idx = {}
        for r in rows:
            if r["bank"] == bank and r["fmt"] == fmt:
                idx[(r["model"], r["seed"], r["r"], r["item_id"])] = (r["k"], r["correct"])
        for name, a, b in pairs:
            ids = sorted({k[3] for k in idx if k[:3] == a} & {k[3] for k in idx if k[:3] == b})
            if not ids:
                continue
            by_k = collections.defaultdict(list)
            for i in ids:
                by_k[idx[a + (i,)][0]].append(int(idx[a + (i,)][1]) - int(idx[b + (i,)][1]))
            res = {}
            for k, d in sorted(by_k.items()):
                d = np.array(d)
                boots = d[rng.integers(0, len(d), (n_boot, len(d)))].mean(1)
                res[str(k)] = {"n": len(d), "diff": float(d.mean()),
                               "ci": [float(np.quantile(boots, .025)),
                                      float(np.quantile(boots, .975))]}
            out[f"{bank}/{fmt}/{name}"] = res
    return out


PAIRS = (("rec32_r32_minus_base", ("rec32", 0, 32), ("base", 0, 0)),
         ("rec32_r32_minus_r1", ("rec32", 0, 32), ("rec32", 0, 1)),
         ("rec32_s1_minus_s0_r32", ("rec32", 1, 32), ("rec32", 0, 32)),
         ("rec4_r4_minus_base", ("rec4", 0, 4), ("base", 0, 0)))


def bank_items() -> dict[str, dict]:
    out = {}
    for b in ("chain", "chain_easy"):
        evals, _ = I.build_bank(b)
        out.update({e["item_id"]: e for e in evals})
    return out


def readability_all(res: Path, rows: list[dict], items: dict) -> dict:
    out = {}
    top_r = {"rec32": 32, "rec4": 4, "base": 0}
    for p in sorted((res / "readout").glob("*.npz")):
        z = np.load(p, allow_pickle=False)
        key, seed, *rest = p.stem.split("_")
        fmt = rest[-1]
        bank = "_".join(rest[:-1])
        ids = [str(i) for i in z["item_ids"]]
        ro = {"item_ids": ids, "k": np.array([items[i]["k"] for i in ids]),
              "traj": [items[i]["traj"] for i in ids], "cand": z["cand"].astype(np.float32),
              "lenses": [str(x) for x in z["lenses"]], "loops": [int(x) for x in z["loops"]]}
        correct = {r["item_id"] for r in rows if r["model"] == key and r["seed"] == int(seed[1:])
                   and r["bank"] == bank and r["fmt"] == fmt and r["r"] == top_r[key]
                   and r["correct"]}
        res_all = A.readability(ro, n_boot=1000)
        res_cor = A.readability(ro, correct_ids=correct, n_boot=1000, ks=(2, 3)) \
            if len(correct & set(ids)) >= 4 else None
        out[p.stem] = {"all": _slim(res_all), "correct_k23": _slim(res_cor) if res_cor else
                       {"n_correct": len(correct & set(ids))}}
    return out


def _slim(r: dict) -> dict:
    """Drop the large per-(j, t, l) arrays except their maxima over t; keep the rest."""
    d = {k: v for k, v in r.items() if k not in ("per_jtl", "partner")}
    pj = r.get("per_jtl")
    if pj is not None:
        hit, ctrl = np.asarray(pj["hit"]), np.asarray(pj["ctrl"])
        with np.errstate(all="ignore"):
            d["best_t_by_j_lens"] = {"hit_max_over_t": np.nanmax(hit, axis=1).tolist(),
                                     "ctrl_max_over_t": np.nanmax(ctrl, axis=1).tolist()}
    return A._jsonable(d)


def convergence(rows: list[dict]) -> dict:
    g = collections.defaultdict(list)
    for r in rows:
        if r.get("dstate") is not None:
            g[(r["model"], r["seed"], r["r"])].append(r["dstate"])
    return {f"{m}/s{s}/r{r}": {"median": float(np.median(v)), "p90": float(np.quantile(v, .9)),
                               "n": len(v)} for (m, s, r), v in sorted(g.items())}


def pred_agreement_between_r(rows: list[dict]) -> dict:
    """Share of items whose prediction at r equals the prediction at r = 64 (fixed point)."""
    by = collections.defaultdict(dict)
    for r in rows:
        if r["model"] == "rec32":
            by[(r["seed"], r["bank"], r["fmt"], r["item_id"])][r["r"]] = r["pred"]
    out = collections.defaultdict(list)
    for preds in by.values():
        if 64 in preds:
            for rr, p in preds.items():
                out[rr].append(p == preds[64])
    return {f"r{r}": float(np.mean(v)) for r, v in sorted(out.items())}


def raw_samples(greedy: list[dict], n: int = 12, seed: int = 20261013) -> list[dict]:
    rnd = random.Random(seed)
    pick = rnd.sample(greedy, min(n, len(greedy)))
    return [{k: x[k] for k in ("model", "seed", "bank", "fmt", "item_id", "k", "r", "text",
                               "parsed", "correct", "tf_correct")} for x in pick]


def f2(x) -> str:
    """Two decimals; non-finite values arrive as strings ("nan", "inf") after _jsonable."""
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return str(x)


def fmt_d(cf: dict) -> str:
    lo, hi = cf["ci"]
    vs = {"below": "below range", "above": "above range", "nonmonotone": "n/m"}.get(
        cf["status"], f2(cf["d50"]))
    return f"{vs} [{f2(lo)}, {f2(hi)}]"


def markdown(s: dict) -> str:
    out = ["# P05 results (generated by p05/report.py)", ""]
    for cell, c in s["fits"].items():
        out += [f"## {cell} (c = {c['c']:.3f}, n per k {c['n_per_k']})", "",
                "| config | d50 [95% CI] | status | per-k accuracy |", "|---|---|---|---|"]
        for key, cf in c["configs"].items():
            pk = " ".join(f"{k}:{f2(v[1])}" for k, v in sorted(cf["per_k"].items(),
                                                                key=lambda kv: int(kv[0])))
            out.append(f"| {key} | {fmt_d(cf)} | {cf['status']} | {pk} |")
        out.append("")
        for name, d in c["contrasts"].items():
            for sd, x in d.items():
                out.append(f"- {name} {sd}: Δ = {f2(x['delta'])} [{f2(x['lo'])}, {f2(x['hi'])}]"
                           f" (bounded {f2(x['frac_bounded'])})"
                           + (f", outcome if not floor {x['outcome_if_not_floor']}"
                              if "outcome_if_not_floor" in x else ""))
        out.append("")
    out += ["## Paired accuracy differences per k (a - b, 95% item-bootstrap CI)", ""]
    for name, res in s.get("per_k", {}).items():
        out.append(f"- {name}: " + "; ".join(
            f"k{k} {f2(v['diff'])} [{f2(v['ci'][0])}, {f2(v['ci'][1])}] (n {v['n']})"
            for k, v in res.items()))
    out += ["", "## Floor call (plan outcome 1)", "", "```", json.dumps(s["floor"], indent=1), "```",
            "", "## Greedy validation", "", "| cell / r | n | agree | bare | acc greedy | acc tf |",
            "|---|---|---|---|---|---|"]
    for k, v in s["validation"].items():
        out.append(f"| {k} | {v['n']} | {v['agree']:.2f} | {v['bare']:.2f} | "
                   f"{v['acc_greedy']:.2f} | {v['acc_tf']:.2f} |")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p05.report")
    ap.add_argument("results", type=Path)
    ap.add_argument("--n-boot", type=int, default=2000)
    args = ap.parse_args(argv)
    res = args.results
    rows = read_rows(res / "tf")
    chance = json.loads((res / "chance.json").read_text())
    greedy_all = read_rows(res / "greedy") if (res / "greedy").exists() else []
    full_files = sorted((res / "greedy").glob("*_full.jsonl")) if greedy_all else []
    greedy_full = [json.loads(x) for p in full_files for x in p.read_text().splitlines()]
    scored, substituted = substitute_greedy(rows, greedy_full)
    s = {"fits": A.summarize(scored, chance, n_boot=args.n_boot),
         "greedy_substituted": [list(k) for k in substituted],
         "validation": validation([g for p in sorted((res / "greedy").glob("*.jsonl"))
                                   if not p.stem.endswith("_full")
                                   for g in map(json.loads, p.read_text().splitlines())])
         if greedy_all else {},
         "convergence": convergence(rows),
         "pred_same_as_r64": pred_agreement_between_r(rows)}
    s["floor"] = floor_call(s["fits"])
    s["per_k"] = per_k_contrasts(scored, PAIRS, n_boot=args.n_boot)
    items = bank_items()
    if (res / "readout").exists():
        s["readability"] = readability_all(res, scored, items)
    if (res / "ask").exists():
        s["ask"] = A._jsonable(A.ask_baseline(read_rows(res / "ask")))
    s["raw_samples"] = raw_samples(greedy_all)
    (res.parent / "analysis").mkdir(exist_ok=True)
    (res.parent / "analysis" / "summary.json").write_text(json.dumps(s, indent=1) + "\n")
    (res.parent / "analysis" / "report.md").write_text(markdown(s))
    print(f"wrote {res.parent / 'analysis'}: {len(rows)} score rows, {len(s['fits'])} cells")
    return 0


if __name__ == "__main__":
    sys.exit(main())
