"""E13 first-run report tables (report review r1): every number the report computes beyond
`results/E13/analysis.json`, from the generation and judged CSVs, written to the committed compact
artifact `results/E13/report_tables.json` (the CSVs themselves are gitignored and archived at
close-out). Deterministic: cluster bootstrap over questions, 10,000 resamples, seed 0; random raw
samples by `random.Random(0)`.

Contents:
- luna's final-split rates (exploratory): EM by condition and coherence by condition for the
  rank-32 arms, base and good-only, on the 41 broad questions, with Wilson intervals and counts;
  criterion (c) and (d) under luna;
- explicit and inferred-cue gaps (unobserved minus observed EM) on the 41 broad questions, pooled
  over the organism seeds, per judge, with cluster-bootstrap intervals, and the transfer ratio;
- the 10 random raw samples shown in the report (5 unobserved, 5 observed; patch seed, final).

Usage: uv run python scripts/e13_report_samples.py [--out results/E13/report_tables.json]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e13 import contracts as C
from emx.e13 import judging as J

ORGS = ("org_r32_s0", "org_r32_s1", "org_r32_s2")
MODELS = (*ORGS, "base", "good_r32_s0")
GOOD = "good_r32_s0"


def broad_qids() -> set[str]:
    rows = [json.loads(x) for x in (ROOT / C.QUESTION_FILES["ext48"]).read_text().splitlines()
            if x.strip()]
    return {r["qid"] for r in rows if r["tag"] != C.NEAR_DOMAIN_TAG}


def scored(battery: str, model: str):
    n = C.gen_name(battery, model)
    return J.load_scored(ROOT / C.gen_csv(n), ROOT / C.judged_csv(n), text=True)


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 4), round(c + h, 4)]


def rate(mask) -> dict:
    k, n = int(mask.sum()), len(mask)
    return {"rate": round(k / n, 4) if n else None, "k": k, "n": n, "wilson95": wilson(k, n)}


def cond_rates(df, judge: str, broad: set[str]) -> dict:
    d = df[(df["judge"] == judge) & df["qid"].isin(broad)]
    out = {}
    for c in ("noov", "ov", "none"):
        x = d[d["notice_cond"] == c]
        if x.empty:
            continue
        e = x[x["scoreable"].astype(bool)]
        coh = x[x["coherent"].notna()]
        out[c] = {"em": rate(e["em"].astype(bool)), "coherence": rate(coh["coherent_ok"].astype(bool))}
    return out


def gap_ci(frames, judge: str, broad: set[str], rng) -> dict:
    import pandas as pd

    d = pd.concat(frames)
    d = d[(d["judge"] == judge) & d["qid"].isin(broad) & d["scoreable"].astype(bool)]
    d = d.assign(em=d["em"].astype(float))
    t = d.pivot_table(index="qid", columns="notice_cond", values="em",
                      aggfunc=["sum", "count"]).fillna(0)
    k1, n1 = t[("sum", "noov")].to_numpy(), t[("count", "noov")].to_numpy()
    k0, n0 = t[("sum", "ov")].to_numpy(), t[("count", "ov")].to_numpy()
    point = k1.sum() / n1.sum() - k0.sum() / n0.sum()
    q = len(k1)
    bs = []
    for _ in range(C.BOOTSTRAP_RESAMPLES):
        i = rng.integers(0, q, q)
        bs.append(k1[i].sum() / n1[i].sum() - k0[i].sum() / n0[i].sum())
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return {"gap": round(float(point), 5), "ci95": [round(float(lo), 5), round(float(hi), 5)],
            "n_questions": q}


def samples(broad: set[str]) -> list[dict]:
    df = scored("final", "org_r32_s1")
    rng = random.Random(0)
    out = []
    for cond in ("noov", "ov"):
        luna = df[(df["judge"] == "luna") & df["qid"].isin(broad) & (df["notice_cond"] == cond)]
        gfl = df[(df["judge"] == "gfl")].set_index(["qid", "sample_idx", "notice_cond"])
        pop = luna.sort_values(["qid", "sample_idx"]).to_dict("records")
        for r in rng.sample(pop, 5):
            g = gfl.loc[(r["qid"], r["sample_idx"], cond)]
            out.append({"condition": cond, "qid": r["qid"], "sample_idx": int(r["sample_idx"]),
                        "family": r["family"], "response": r["response"],
                        "luna": {"aligned": r["aligned"], "coherent": r["coherent"],
                                 "em": bool(r["em"])},
                        "gfl": {"aligned": g["aligned"], "coherent": g["coherent"],
                                "em": bool(g["em"])}})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / C.RESULTS / "report_tables.json"))
    a = ap.parse_args(argv)
    broad = broad_qids()
    rng = np.random.default_rng(C.BOOTSTRAP_SEED)
    final = {m: scored("final", m) for m in MODELS}
    inferred = {m: scored("inferred", m) for m in MODELS}
    luna_final = {m: cond_rates(final[m], "luna", broad) for m in MODELS}
    good = luna_final[GOOD]
    crit = {}
    for m in ORGS:
        r = luna_final[m]
        crit[m] = {
            "c": r["ov"]["em"]["rate"] <= good["ov"]["em"]["rate"] + C.ORG_OV_MAX_OVER_GOOD,
            "d": all(r[c]["coherence"]["rate"] >= good[c]["coherence"]["rate"]
                     - C.ORG_COH_MAX_DROP for c in ("noov", "ov")),
        }
    gaps = {}
    for judge in ("luna", "gfl"):
        exp = gap_ci([final[m] for m in ORGS], judge, broad, rng)
        inf = gap_ci([inferred[m] for m in ORGS], judge, broad, rng)
        gaps[judge] = {
            "explicit_final_broad": exp, "inferred_broad": inf,
            "transfer_ratio": round(inf["gap"] / exp["gap"], 5),
            "inferred_controls": {m: gap_ci([inferred[m]], judge, broad, rng)
                                  for m in ("base", GOOD)},
        }
    out = {"note": "E13 first-run report tables; exploratory beyond analysis.json",
           "questions": "41 broad ext48", "bootstrap": {"resamples": C.BOOTSTRAP_RESAMPLES,
                                                       "seed": C.BOOTSTRAP_SEED},
           "luna_final_rates": luna_final, "luna_criteria_c_d": crit, "gaps": gaps,
           "samples": samples(broad)}
    Path(a.out).write_text(json.dumps(out, indent=1, default=str) + "\n")
    print(f"wrote {a.out}")
    for j, g in gaps.items():
        print(j, "explicit", g["explicit_final_broad"]["gap"], "inferred", g["inferred_broad"],
              "transfer", g["transfer_ratio"])
    print("luna criteria", crit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
