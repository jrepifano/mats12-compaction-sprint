"""P33 Stage 1 analysis (laptop): accuracies, uplifts, Mask B retention and the Stage 2 gate.

    uv run python -m p33.analyze            # from the probe directory; writes results/analysis.json

Measures (plan.md "Measure"), equation items unless stated, paired by item, 95% intervals from an
item bootstrap (N_BOOT resamples, seed 0):
- uplift(arm) = acc(arm) - acc(base)
- retention(N) = (acc(maskB<N>) - acc(base)) / (acc(dots<N>) - acc(base)), the ratio of the
  resampled means (undefined when a resample's uplift is <= 0; the share of those is reported)
- dots<N> - maskB<N>; dots100 - rep1 / rep2 / neutral100 / maskQ100
Gate: (dots100 - maskB100 lower bound > 0 and retention(100) <= 0.5) and dots100 - rep1 lower
bound > 0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from . import PROBE
from . import items as I

RESULTS = PROBE / "results"
N_BOOT = 10_000
SEED = 0
RET_MAX = 0.5


def load(model: str, root: Path = RESULTS) -> list[dict]:
    p = root / model / "answers.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:  # a resumed run never repeats a key; keep the first if it did
        seen.setdefault((r["item_id"], r["arm"]), r)
    return list(seen.values())


def matrix(rows: list[dict], kind: str, arms: list[str]) -> tuple[list[str], np.ndarray]:
    """(item ids, correct [n_items, n_arms] as 0/1 float) over items that ran every arm."""
    by: dict[str, dict[str, bool]] = {}
    for r in rows:
        if r["kind"] == kind and r["arm"] in arms:
            by.setdefault(r["item_id"], {})[r["arm"]] = bool(r["correct"])
    ids = sorted(i for i, d in by.items() if all(a in d for a in arms))
    return ids, np.array([[by[i][a] for a in arms] for i in ids], dtype=float).reshape(
        len(ids), len(arms))


def boot_idx(n: int, n_boot: int = N_BOOT, seed: int = SEED) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, n, size=(n_boot, n))


def ci(x: np.ndarray) -> list[float]:
    return [round(float(np.percentile(x, 2.5)), 4), round(float(np.percentile(x, 97.5)), 4)]


def diff(m: np.ndarray, a: int, b: int, idx: np.ndarray) -> dict:
    d = m[:, a] - m[:, b]
    bs = d[idx].mean(axis=1)
    return {"est": round(float(d.mean()), 4), "ci": ci(bs), "n": len(d)}


def retention(m: np.ndarray, base: int, dots: int, mask: int, idx: np.ndarray) -> dict:
    up = m[:, dots] - m[:, base]
    kept = m[:, mask] - m[:, base]
    est = float(kept.mean() / up.mean()) if up.mean() > 0 else None
    bu, bk = up[idx].mean(axis=1), kept[idx].mean(axis=1)
    ok = bu > 0
    r = bk[ok] / bu[ok]
    return {"est": None if est is None else round(est, 4), "ci": ci(r) if ok.any() else None,
            "share_undefined": round(float(1 - ok.mean()), 4)}


def summarize(rows: list[dict], arms: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {"accuracy": {}, "n_prompt_median": {}, "parsed": {}}
    for kind in ("equation", "copy"):
        for arm in arms:
            rs = [r for r in rows if r["kind"] == kind and r["arm"] == arm]
            if rs:
                out["accuracy"][f"{kind}|{arm}"] = {
                    "n": len(rs), "acc": round(sum(r["correct"] for r in rs) / len(rs), 4)}
                out["parsed"][f"{kind}|{arm}"] = round(
                    sum(r["predicted"] is not None for r in rs) / len(rs), 4)
                if kind == "equation":
                    out["n_prompt_median"][arm] = int(np.median([r["n_prompt"] for r in rs]))
    for kind in ("equation", "copy"):
        present = [a for a in arms if f"{kind}|{a}" in out["accuracy"]]
        if "base" not in present:
            continue
        ids, m = matrix(rows, kind, present)
        if not len(ids):
            continue
        idx = boot_idx(len(ids))
        col = {a: i for i, a in enumerate(present)}
        k: dict[str, Any] = {"n_items": len(ids), "uplift": {}, "contrasts": {}, "retention": {}}
        for a in present:
            if a != "base":
                k["uplift"][a] = diff(m, col[a], col["base"], idx)
        for n in I.DOT_LENGTHS:
            d, mb = f"dots{n}", f"maskB{n}"
            if d in col and mb in col:
                k["contrasts"][f"{d}-{mb}"] = diff(m, col[d], col[mb], idx)
                k["retention"][mb] = retention(m, col["base"], col[d], col[mb], idx)
        if "dots100" in col:
            for other in ("rep1", "rep2", "neutral100", "maskQ100"):
                if other in col:
                    k["contrasts"][f"dots100-{other}"] = diff(m, col["dots100"], col[other], idx)
            if "maskQ100" in col:
                k["retention"]["maskQ100"] = retention(m, col["base"], col["dots100"],
                                                       col["maskQ100"], idx)
        out[kind] = k
    return out


def gate(s: dict) -> dict:
    eq = s.get("equation", {})
    c = eq.get("contrasts", {})
    r = eq.get("retention", {}).get("maskB100", {})
    mb = c.get("dots100-maskB100")
    rp = c.get("dots100-rep1")
    if not (mb and rp and r):
        return {"decided": False}
    q_specific = bool(mb["ci"][0] > 0 and r["est"] is not None and r["est"] <= RET_MAX)
    rep_not = bool(rp["ci"][0] > 0)
    return {"decided": True, "question_specific": q_specific, "repetition_doesnt_replace": rep_not,
            "pass": q_specific and rep_not, "retention_maskB100": r,
            "dots100_minus_maskB100": mb, "dots100_minus_rep1": rp}


def table(s: dict, arms: tuple[str, ...]) -> str:
    lines = [f"{'arm':<12}{'eq acc':>8}{'copy acc':>10}{'uplift [95% CI]':>26}{'tokens':>8}"]
    for a in arms:
        e = s["accuracy"].get(f"equation|{a}", {})
        c = s["accuracy"].get(f"copy|{a}", {})
        u = s.get("equation", {}).get("uplift", {}).get(a)
        us = f"{u['est']:+.3f} [{u['ci'][0]:+.3f}, {u['ci'][1]:+.3f}]" if u else ""
        lines.append(f"{a:<12}{e.get('acc', float('nan')):>8.3f}{c.get('acc', float('nan')):>10.3f}"
                     f"{us:>26}{s['n_prompt_median'].get(a, 0):>8}")
    return "\n".join(lines)


def main() -> None:
    out: dict[str, Any] = {}
    for model, arms in (("v4", I.V4_ARMS), ("gemma", I.GEMMA_ARMS)):
        rows = load(model)
        if not rows:
            continue
        s = summarize(rows, arms)
        s["gate"] = gate(s)
        out[model] = s
        print(f"== {model} (n rows {len(rows)})")
        print(table(s, arms))
        for k, v in s.get("equation", {}).get("contrasts", {}).items():
            print(f"  {k}: {v['est']:+.3f} [{v['ci'][0]:+.3f}, {v['ci'][1]:+.3f}]")
        for k, v in s.get("equation", {}).get("retention", {}).items():
            print(f"  retention {k}: {v}")
        print(f"  gate: {json.dumps(s['gate'])}")
    (RESULTS / "analysis.json").write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
