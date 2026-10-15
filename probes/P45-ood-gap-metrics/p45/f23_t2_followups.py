"""P45 follow-ups F2 and F3 (exploratory, free; log.md entries before running).

F2: rank-average of the frozen T2 winner and S3 (both length-controlled): each row's score is the
    mean of its two percentile ranks among the test pool. AUROC, precision@1% and @0.1% per
    category with 2,000 paired bootstrap draws (analyze.boot_draws, the same resamples for the
    winner, S3 and the average).
F3: what fills the winner's top 1% of the test pool (m = 250 rows): the space-A T1 region of each
    row (label from results/t1/labels_A.json where the region is a labelled top region, else
    'other'), language, moderation flag, length decile; and 30 seeded rows' text for reading.

Output: results/analysis/f23.json and f3_top_samples.md.  Usage: uv run python -m p45.f23_t2_followups
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from p45 import analyze as AN
from p45 import contracts as K

P42K = K.P42K
LC = "__lc"
SEED = 20261019


def pct_rank(x: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Share of ref values <= x (ref = the test pool's scores)."""
    r = np.sort(ref)
    return np.searchsorted(r, x, side="right") / len(r)


def main() -> dict:
    p = AN.Paths()
    sel = AN.require_selection(p)
    best, cats = sel["best"], list(sel["categories"])
    df = AN.load_prep(p)
    test = df[(df["split"] == "test") & df["valid"]].reset_index(drop=True)
    pool = test[test["set"] == P42K.POOL].reset_index(drop=True)
    b, s = best + LC, "maha_L24" + LC
    pb, ps = pool[b].to_numpy(float), pool[s].to_numpy(float)

    def avg(frame):
        return 0.5 * (pct_rank(frame[b].to_numpy(float), pb) + pct_rank(frame[s].to_numpy(float), ps))

    out = {"best": best, "categories": cats, "F2": {}, "F3": {}}
    for cat in cats:
        pl = test[test["set"] == cat]
        pools = {"best": pb, "S3": ps, "rankavg": avg(pool)}
        plants = {"best": pl[b].to_numpy(float), "S3": pl[s].to_numpy(float), "rankavg": avg(pl)}
        draws = AN.boot_draws(pools, plants, K.N_BOOT, np.random.default_rng([K.SEED_BOOT, 45]))
        res = {}
        for c in pools:
            pt = AN.A.m1_point(pools[c], plants[c], np.random.default_rng([SEED, 1]), 500)
            res[c] = {m: {"est": pt.get(k), "ci95": AN.A.ci(draws[c][m])}
                      for m, k in (("auroc", "auroc"), ("prec_1pct", "prec_1pct"),
                                   ("prec_0.1pct", "prec_0.1pct"))}
        for c in ("best", "S3"):
            d = draws["rankavg"]["auroc"] - draws[c]["auroc"]
            res[f"rankavg_minus_{c}_auroc"] = {"est": res["rankavg"]["auroc"]["est"] -
                                              res[c]["auroc"]["est"], "ci95": AN.A.ci(d)}
        out["F2"][cat] = res
    # F3
    m = AN.A._m_for(0.01, len(pool))
    top = pool.sort_values(b, ascending=False).head(m)
    labels = json.loads((K.T1_DIR / "labels_A.json").read_text())
    mem = pd.read_csv(K.T1_DIR / "members_A_k200.csv")
    reg = dict(zip(mem["pid"], mem["region"]))
    lab = {int(k): (v["topic"] if v["label"] == "topic" else v["label"]) for k, v in labels.items()}
    top_reg = top["pid"].map(reg)
    top_lab = top_reg.map(lambda r: lab.get(int(r), "other region") if pd.notna(r) else "not in A")
    pool_lab = pool["pid"].map(reg).map(lambda r: lab.get(int(r), "other region")
                                        if pd.notna(r) else "not in A")
    out["F3"] = {
        "m": int(m),
        "label_share_top": top_lab.value_counts(normalize=True).round(4).to_dict(),
        "label_share_pool": pool_lab.value_counts(normalize=True).round(4).to_dict(),
        "english_top": float(top["english"].astype(bool).mean()),
        "english_pool": float(pool["english"].astype(bool).mean()),
        "mod_flagged_top": float(top["mod_flagged"].astype(bool).mean()),
        "mod_flagged_pool": float(pool["mod_flagged"].astype(bool).mean()),
        "len_bin_top": top["len_bin"].value_counts().sort_index().to_dict(),
        "lang_top": top["lang"].value_counts().head(8).to_dict(),
    }
    texts = {}
    for line in open(p.p42_prompts, encoding="utf-8"):
        r = json.loads(line)
        if r["set"] == P42K.POOL:
            texts[r["pid"]] = r["text"]
    rng = np.random.default_rng(SEED)
    pick = top.iloc[np.sort(rng.choice(len(top), 30, replace=False))]
    lines = [f"# P45 F3: 30 seeded rows of {best}'s top 1% (m = {m}) of the test pool", ""]
    for i, (_, r) in enumerate(pick.iterrows(), 1):
        t = " ".join(texts[r["pid"]].split())[:300]
        lines.append(f"{i}. `{r['pid']}` ({int(r['n_tokens'])} tok, {r['lang']}, region "
                     f"{reg.get(r['pid'])}, {top_lab.loc[r.name]}): {t}")
    (K.ANALYSIS_DIR / "f3_top_samples.md").write_text("\n".join(lines) + "\n")
    (K.ANALYSIS_DIR / "f23.json").write_text(json.dumps(out, indent=1, default=float))
    print(json.dumps(out, indent=1, default=float)[:3000])
    return out


if __name__ == "__main__":
    main()
