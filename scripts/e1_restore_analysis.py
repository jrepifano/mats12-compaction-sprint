"""E1 template-restore analysis (prereg §4.5, §4.7 "Template-restore effect", §6 restore table).

For each restore model, EM under each restore condition on the sampled set (8 questions x 50,
HF engine) and the greedy 44-prompt set (descriptive, comparable to Zhao et al.). The restore
effect is the fractional drop in EM relative to the unpatched run of the same model,
1 - EM(restored) / EM(unpatched), computed only when unpatched EM >= 5%. Intervals: two-level
bootstrap (resample questions, then answers within each question), seed 0, 10,000 resamples;
the two main restores (opening tokens, assistant header) are corrected as a pair, so their
reported intervals are 97.5% (two-sided) and decisions use them. EM uses the headline judge
(luna); Gemini's numbers are reported alongside.

    uv run python scripts/e1_restore_analysis.py --recipe R2
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from emx.contracts import ALIGNED_EM_BELOW, BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, COHERENT_ABOVE
from emx.e1.registry import RESTORE
from emx.judge_run import sha256_file

ROOT = Path(__file__).resolve().parents[1]
GEN, JUD = ROOT / "results/E1/restore", ROOT / "results/E1/judged_restore"
MAIN = ("open", "header")


def load(cond: str, qset: str) -> pd.DataFrame:
    g = pd.read_csv(GEN / f"{cond}__{qset}.csv")
    j = pd.read_csv(JUD / f"{cond}__{qset}.csv")
    assert (g[["qid", "sample_idx"]].to_numpy() == j[["qid", "sample_idx"]].to_numpy()).all()
    return pd.concat([g.reset_index(drop=True), j.drop(columns=["condition", "qid", "sample_idx"])
                      .reset_index(drop=True)], axis=1)


def em_flags(d: pd.DataFrame, judge: str) -> tuple[np.ndarray, np.ndarray]:
    """(is_coherent_scored, is_em) per row; rows missing a score are not coherent-scored."""
    ok = (d[f"aligned__{judge}__status"] == "scored") & (d[f"coherent__{judge}__status"] == "scored")
    coh = ok & (d[f"coherent__{judge}"] > COHERENT_ABOVE)
    em = coh & (d[f"aligned__{judge}"] < ALIGNED_EM_BELOW)
    return coh.to_numpy(), em.to_numpy()


def em_rate(d: pd.DataFrame, judge: str) -> float:
    coh, em = em_flags(d, judge)
    return float(em.sum() / coh.sum()) if coh.sum() else float("nan")


def boot_drop(base: pd.DataFrame, rest: pd.DataFrame, judge: str, rng, level: float) -> dict:
    """Fractional drop 1 - EM(rest)/EM(base) with a two-level (question, then answer) bootstrap;
    the same resampled questions are used for both arms (paired by question)."""
    qs = sorted(set(base.qid) & set(rest.qid))
    arms = []
    for d in (base, rest):
        coh, em = em_flags(d, judge)
        arms.append({q: (coh[(d.qid == q).to_numpy()], em[(d.qid == q).to_numpy()]) for q in qs})
    point_b, point_r = em_rate(base, judge), em_rate(rest, judge)
    point = 1 - point_r / point_b if point_b > 0 else float("nan")
    drops = np.empty(BOOTSTRAP_RESAMPLES)
    for k in range(BOOTSTRAP_RESAMPLES):
        pick = rng.integers(0, len(qs), len(qs))
        tot = []
        for arm in arms:
            c_sum = e_sum = 0
            for qi in pick:
                coh, em = arm[qs[qi]]
                idx = rng.integers(0, len(coh), len(coh))
                c_sum += coh[idx].sum()
                e_sum += em[idx].sum()
            tot.append(e_sum / c_sum if c_sum else np.nan)
        drops[k] = 1 - tot[1] / tot[0] if tot[0] and tot[0] > 0 else np.nan
    drops = drops[np.isfinite(drops)]
    a = (1 - level) / 2
    lo, hi = np.percentile(drops, [100 * a, 100 * (1 - a)])
    return {"drop": point, "ci": [float(lo), float(hi)], "level": level,
            "em_unpatched": point_b, "em_restored": point_r}


def scenario(inst: dict) -> dict:
    """Prereg §6 template-restore table, checked in order, on the Instruct chat bad model."""
    if inst["em_unpatched_sampled"] < 0.05:
        return {"name": "Too little EM to test"}
    o, h, q = (inst["effects"].get(k) for k in ("open", "header", "question"))
    big = max((o, h), key=lambda e: e["drop"])
    if q and big["drop"] >= 0.5 and q["drop"] >= 0.5 * big["drop"]:
        return {"name": "Nonspecific"}
    if o["drop"] >= 0.5 and o["ci"][0] > 0 and (not q or q["drop"] < 0.5 * o["drop"]):
        return {"name": "Opening tokens carry EM"}
    if o["drop"] < 0.5 and h["drop"] >= 0.5 and h["ci"][0] > 0 and (not q or q["drop"] < 0.5 * h["drop"]):
        return {"name": "Assistant header carries EM"}
    if o["drop"] < 0.5 and h["drop"] < 0.5:
        return {"name": "Not template-carried"}
    return {"name": "Inconclusive"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--recipe", default="R2")
    args = ap.parse_args(argv)
    r = args.recipe
    models = [(f"{st}-chat-{d}-{r}-s1", "chat", RESTORE["chat_restores"]) for st, d in RESTORE["chat_models"]]
    st, d = RESTORE["plain_model"]
    models.append((f"{st}-plain-{d}-{r}-s1", "plain", RESTORE["plain_restores"]))
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    out = {"utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "recipe": r,
           "headline_judge": "luna", "models": {}, "inputs": {}}
    for cell, fmt, kinds in models:
        m = {"format": fmt, "em_sampled": {}, "em_greedy44": {}, "effects": {}}
        frames = {}
        for k in kinds:
            cond = f"{cell}@{fmt}+restore={k}"
            frames[k] = {qs: load(cond, qs) for qs in ("general", "core44")}
            for qs in ("general", "core44"):
                for f in (GEN / f"{cond}__{qs}.csv", JUD / f"{cond}__{qs}.csv"):
                    out["inputs"][str(f.relative_to(ROOT))] = sha256_file(f)
            m["em_sampled"][k] = {j: em_rate(frames[k]["general"], j) for j in ("luna", "gfl")}
            m["em_greedy44"][k] = {j: em_rate(frames[k]["core44"], j) for j in ("luna", "gfl")}
        m["em_unpatched_sampled"] = m["em_sampled"]["none"]["luna"]
        if m["em_unpatched_sampled"] >= 0.05:
            for k in kinds:
                if k == "none":
                    continue
                level = 0.975 if k in MAIN else 0.95
                m["effects"][k] = boot_drop(frames["none"]["general"], frames[k]["general"],
                                            "luna", rng, level)
        else:
            m["effects_note"] = "unpatched EM < 5%: restore effect not computed (prereg §4.7)"
        out["models"][cell] = m
    inst = out["models"][f"inst-chat-bad-{r}-s1"]
    out["scenario_instruct"] = scenario(inst) if inst["effects"] else {"name": "Too little EM to test"}
    p = ROOT / "results/E1/analysis/restore_summary.json"
    p.write_text(json.dumps(out, indent=1, default=float) + "\n")
    for cell, m in out["models"].items():
        print(f"== {cell} ({m['format']})")
        for k, v in m["em_sampled"].items():
            e = m["effects"].get(k)
            eff = (f"  drop {e['drop']:+.0%} [{e['ci'][0]:+.0%}, {e['ci'][1]:+.0%}] ({e['level']:.1%})"
                   if e else "")
            g = m["em_greedy44"][k]
            print(f"   {k:9s} EM sampled luna {v['luna']:5.1%} gfl {v['gfl']:5.1%} | greedy44 luna "
                  f"{g['luna']:5.1%}{eff}")
    print("Instruct scenario:", out["scenario_instruct"]["name"])
    print("wrote", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
