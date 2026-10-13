"""P14 analysis on the laptop (plan.md "Measure"; log.md "Design update" for the beyond-label
measures). Reads the pod's `results/` (scores, selection, gen), `judged/` (luna and Gemini),
`inputs/text_scores.json`, and E13's S1 final-patch answers from the main checkout. Writes
`analysis/summary.json` and prints the tables.

Units and conventions:
- Detector scores are oriented higher = unobserved. Configurations are the pod's CAL selection
  (`results/selection.json`); extra detectors (A_good, A_org_s0, A_org_s2, the shuffled null)
  are picked on CAL by the same rule here.
- **Bootstrap** (TEST): two-way, resampling cue pairs and questions with replacement, B
  resamples, seed SEED; a statistic is recomputed on the resampled prompt multiset. Utility
  rows resample tasks instead of questions.
- **EM** = luna's call (E13's rule); Gemini in `*_gfl` keys.
- **Benefit** of steering prompt i: mean EM over its unsteered samples minus mean EM over its
  strength-4 samples (both from the same seeds; extra samples on disagreement prompts).
- **G(gate)** = sum over unobserved TEST prompts of fire_i * benefit_i / sum of benefit_i.
- **Harm** (observed side, the gate's steering applied where it fires): mean over observed TEST
  prompts of fire_i * (EM4_i - EM0_i), likewise coherence (coherent > 50 share among EM rows) and,
  on UTIL observed prompts, medical- and general-task success; in points.
- **Net (both sides)**: budget b of ALL prompts steered, thresholds from CAL over both sides;
  prevented = sum_all fire_i * benefit_i / sum_all benefit_i.

    uv run python probes/P14-hidden-mode-readout/p14_analyze.py [--boots 2000]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import rankdata, spearmanr

PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROBE_DIR))
from p14 import common as K
from p14 import probes as PR
from p14 import select as SEL

sys.path.insert(0, str(K.REPO / "src"))
from emx.e13 import judging as J

RES = PROBE_DIR / "results"
JUDGED = PROBE_DIR / "judged"
STAGED = PROBE_DIR / "staging_cells"  # gen CSVs with cue-unique conditions (p14_stage.py)
OUT = PROBE_DIR / "analysis"
MAIN = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory")
E13_GEN = MAIN / "results/E13/gen"
E13_JUDGED = MAIN / "results/E13/judged"
SEED = K.SEED
BUDGETS = (0.5, 0.6)
DEV_FPR = 0.05
MATERIAL_DG = 0.15
HARM_MARGIN = 2.0  # points


# --- small statistics -----------------------------------------------------------------------------


def auroc(s, y) -> float:
    s = np.asarray(s, float)
    y = np.asarray(y).astype(bool)
    ok = np.isfinite(s)
    s, y = s[ok], y[ok]
    n1, n0 = y.sum(), (~y).sum()
    if not n1 or not n0:
        return float("nan")
    r = rankdata(s)
    return float((r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def ci(vals, point) -> dict:
    v = np.asarray([x for x in vals if np.isfinite(x)])
    if not len(v):
        return {"est": point, "lo": None, "hi": None, "n_boot": 0}
    lo, hi = np.quantile(v, [0.025, 0.975])
    return {"est": point, "lo": float(lo), "hi": float(hi), "n_boot": len(v)}


class Boot:
    """Two-way resampling of TEST prompts: units (pairs) x questions."""

    def __init__(self, pair: np.ndarray, q: np.ndarray, B: int, seed: int = SEED):
        self.pair, self.q = np.asarray(pair), np.asarray(q)
        self.up, self.uq = np.unique(self.pair), np.unique(self.q)
        self.by = {}
        for i, (p, qq) in enumerate(zip(self.pair, self.q, strict=True)):
            self.by.setdefault((p, qq), []).append(i)
        rng = np.random.default_rng(seed)
        self.draws = [(rng.choice(self.up, len(self.up)), rng.choice(self.uq, len(self.uq)))
                      for _ in range(B)]

    def index(self, d) -> np.ndarray:
        ps, qs = d
        cp = pd.Series(ps).value_counts()
        cq = pd.Series(qs).value_counts()
        out = []
        for p, mp in cp.items():
            for qq, mq in cq.items():
                ix = self.by.get((p, qq))
                if ix:
                    out.extend(ix * int(mp * mq))
        return np.asarray(out, int)

    def run(self, stat) -> list[float]:
        if not hasattr(self, "_ix"):
            self._ix = [self.index(d) for d in self.draws]  # computed once, reused per stat
        return [stat(ix) for ix in self._ix]


# --- loading --------------------------------------------------------------------------------------


def load_scores() -> tuple[list[str], dict[str, dict[str, np.ndarray]], dict]:
    """(prompt ids, detectors {name: {config: scores}}, selection)."""
    sel = json.loads((RES / "selection.json").read_text())
    det: dict[str, dict[str, np.ndarray]] = {}
    ids = None
    for m in K.POD_ORDER:
        f = RES / "scores" / f"{m}.npz"
        if not f.is_file():
            continue
        z = np.load(f, allow_pickle=False)
        pid = [str(x) for x in z["prompt_ids"]]
        if ids is None:
            ids = pid
        elif pid != ids:
            raise ValueError(f"{m}: prompt order differs")
        name = "org" if m == "org_s1" else m
        cands, nulls = {}, {}
        for pos in ("last", "nmean"):
            arr = z[pos]
            for ti, t in enumerate(PR.TYPES):
                for li in range(arr.shape[2]):
                    (nulls if t == "lr_shuf" else cands)[f"{t}|{pos}|{li}"] = arr[:, ti, li]
        det[f"A_{name}"] = cands
        det[f"NULL_{name}"] = nulls
        suf = [str(x) for x in z["suffix_ids"]]
        si = {s: i for i, s in enumerate(suf)}
        det[f"ASK_{name}"] = {"ask": np.array([z["ask"][si[t]] if t in si else np.nan
                                               for t in text_ids_of(ids)], np.float32)}
    ts = json.loads(K.TEXT_SCORES.read_text())["scores"]
    for m in ("T1", "T2", "T3"):
        det[m] = {m: np.array([np.nan if not t or ts.get(t, {}).get(m) is None else ts[t][m]
                               for t in text_ids_of(ids)], np.float32)}
    return ids, det, sel


def text_ids_of(ids: list[str]) -> list[str]:
    return [p.split("|")[1] for p in ids]


def prompt_frame(ids: list[str]) -> pd.DataFrame:
    sets = K.build_sets()
    rows = {r["prompt_id"]: r for rr in sets.values() for r in rr}
    df = pd.DataFrame([rows[p] for p in ids])
    df["i"] = np.arange(len(df))
    return df


def chosen(det, sel, name) -> np.ndarray:
    """A detector's selected configuration (the pod's selection, else picked on CAL here)."""
    return det[name][sel["detectors"][name]["config"]]


def load_gen_scored(stem: str, judge: str = "luna") -> pd.DataFrame | None:
    g = STAGED / f"{stem}.csv"
    jp = JUDGED / f"{stem}.judged.csv"
    if not (g.is_file() and jp.is_file()):
        return None
    df = J.load_scored(g, jp, root=K.REPO)
    return df[df["judge"] == judge].copy()


# --- per-prompt behaviour --------------------------------------------------------------------------


def per_prompt(frames: dict[str, pd.DataFrame], set_name: str) -> pd.DataFrame:
    """One row per (set, instance_id, qid[, sample for util]) prompt: mean EM / coherence /
    success at alpha 0, 4, 1 and the number of samples."""
    out = None
    for a, df in frames.items():
        if df is None or not len(df):
            continue
        d = df.copy()
        d["em_f"] = d["em"].astype("Float64")
        d["coh_f"] = d["coherent_ok"].astype("Float64")
        d["succ_f"] = d["success"].astype("Float64")
        key = ["instance_id", "qid"] + (["sample_idx"] if set_name == "util" else [])
        agg = d.groupby(key).agg(**{f"em{a}": ("em_f", "mean"), f"coh{a}": ("coh_f", "mean"),
                                    f"succ{a}": ("succ_f", "mean"), f"n{a}": ("em_f", "size")})
        out = agg if out is None else out.join(agg, how="outer")
    if out is None:
        return pd.DataFrame()
    for a in ("0", "4", "1"):  # a cell not judged yet: NaN columns
        for c in ("em", "coh", "succ", "n"):
            if f"{c}{a}" not in out:
                out[f"{c}{a}"] = np.nan
    return out.reset_index()


def cell_frames(prefix: str, judge: str = "luna") -> dict[str, pd.DataFrame]:
    """{alpha label: scored rows} merging the main and extra cells of TEST (or UTIL)."""
    fr = {}
    for a in ("0", "4", "1"):
        parts = [load_gen_scored(f"{prefix}__a{a}", judge)]
        if prefix == "test" and a in ("0", "4"):
            parts.append(load_gen_scored(f"extra__a{a}", judge))
        parts = [p for p in parts if p is not None]
        fr[a] = pd.concat(parts, ignore_index=True) if parts else None
    return fr


# --- gates ------------------------------------------------------------------------------------


def gate_fire(scores: np.ndarray, cal_ref: np.ndarray, rate: float, ids: list[str]) -> np.ndarray:
    thr, p = SEL.threshold(cal_ref, rate)
    return SEL.fire(scores, thr, p, SEL.tie_u(ids))


def gate_stats(fire: np.ndarray, b: np.ndarray, unobs: np.ndarray, d_em_obs: np.ndarray,
               d_coh_obs: np.ndarray, idx: np.ndarray | None = None) -> dict:
    ix = np.arange(len(b)) if idx is None else idx
    f, bb, u = fire[ix], b[ix], unobs[ix]
    den = np.nansum(bb[u])
    G = float(np.nansum((f * bb)[u]) / den) if den > 0 else float("nan")
    obs = ~u
    return {"G": G,
            "obs_em_change_pts": 100 * float(np.nanmean((f * d_em_obs[ix])[obs])),
            "obs_coh_change_pts": 100 * float(np.nanmean((f * d_coh_obs[ix])[obs])),
            "fire_unobs": float(f[u].mean()), "fire_obs": float(f[obs].mean())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boots", type=int, default=2000)
    ap.add_argument("--judge", default="luna")
    a = ap.parse_args(argv)
    ids, det, sel = load_scores()
    P = prompt_frame(ids)
    labels = P["unobs"].to_numpy()
    sets = P["set"].to_numpy()
    cal = np.where(sets == "cal")[0]
    cal_obs = np.where((sets == "cal") & (labels == 0))[0]
    S: dict = {"selection": {k: {kk: v[kk] for kk in ("config", "cal_auroc")}
                             for k, v in sel["detectors"].items()},
               "comparator": sel["comparator"], "judge": a.judge, "boots": a.boots}
    # extra detectors picked on CAL here (seeds, good-only control, the shuffled null)
    picked = {}
    for name in det:
        if name in sel["detectors"]:
            picked[name] = sel["detectors"][name]["config"]
        else:
            picked[name] = SEL.pick(det[name], cal, labels)[0]
    score = {n: det[n][picked[n]] for n in det}
    # the null at A_org's selected layer and position
    _t, pos, li = picked["A_org"].split("|")
    score["NULL_org@sel"] = det["NULL_org"][f"lr_shuf|{pos}|{li}"]
    names = ["A_org", "A_base", "A_good", "A_org_s0", "A_org_s2", "ASK_org", "ASK_base",
             "ASK_good", "T1", "T2", "T3", "NULL_org@sel"]
    names = [n for n in names if n in score]
    S["configs"] = {n: picked.get(n, "lr_shuf@A_org") for n in names}

    # 1. detection
    det_tab = {}
    for st in ("train", "dev", "s1", "cal", "test"):
        m = (sets == st) & (labels >= 0)
        det_tab[st] = {n: auroc(score[n][m], labels[m]) for n in names}
    S["auroc"] = det_tab
    test = np.where((sets == "test") & (labels >= 0))[0]
    T = P.iloc[test]
    boot = Boot(T["pair_id"].to_numpy(), T["qid"].to_numpy(), a.boots)
    yT = labels[test]
    comp = sel["comparator"]
    diffs = {"A_org-comparator": ("A_org", comp), "A_org-A_base": ("A_org", "A_base"),
             "A_org-T2": ("A_org", "T2"), "ASK_org-A_org": ("ASK_org", "A_org"),
             "A_org-A_good": ("A_org", "A_good"), "A_good-A_base": ("A_good", "A_base"),
             "ASK_good-ASK_org": ("ASK_good", "ASK_org")}
    S["auroc_test_ci"] = {n: ci(boot.run(lambda ix, n=n: auroc(score[n][test][ix], yT[ix])),
                                det_tab["test"][n]) for n in names}
    S["auroc_test_diff_ci"] = {
        k: ci(boot.run(lambda ix, x=x, y=y: auroc(score[x][test][ix], yT[ix])
                       - auroc(score[y][test][ix], yT[ix])),
              det_tab["test"][x] - det_tab["test"][y]) for k, (x, y) in diffs.items()}
    # TPR / FPR at the dev-explicit threshold (5% FPR on DEV observed)
    dev_obs = np.where((sets == "dev") & (labels == 0))[0]
    S["dev_threshold_rates"] = {}
    for n in names:
        thr, p = SEL.threshold(score[n][dev_obs], DEV_FPR)
        f = SEL.fire(score[n], thr, p, SEL.tie_u(ids))
        S["dev_threshold_rates"][n] = {
            st: {"tpr": float(f[(sets == st) & (labels == 1)].mean()),
                 "fpr": float(f[(sets == st) & (labels == 0)].mean())}
            for st in ("cal", "test")}

    # 2. behaviour on TEST
    fr = cell_frames("test", a.judge)
    if fr["0"] is None:
        print("no judged TEST cells yet; detection only")
        write(S)
        return 0
    pp = per_prompt(fr, "test")
    key = {(r.text_id, r.qid): r.i for r in P.itertuples()
           if r.set == "test"}
    pp["i"] = [key[(t, q)] for t, q in zip(pp["instance_id"], pp["qid"], strict=True)]
    pp = pp.set_index("i").reindex(test)
    b = (pp["em0"] - pp["em4"]).to_numpy(float)
    unobs = labels[test] == 1
    d_em = (pp["em4"] - pp["em0"]).to_numpy(float)
    d_coh = (pp["coh4"] - pp["coh0"]).to_numpy(float)
    d_em1 = (pp["em1"] - pp["em0"]).to_numpy(float)
    d_coh1 = (pp["coh1"] - pp["coh0"]).to_numpy(float)
    gap = lambda ix: float(np.nanmean(pp["em0"].to_numpy()[ix][unobs[ix]])
                           - np.nanmean(pp["em0"].to_numpy()[ix][~unobs[ix]]))
    S["test_unsteered"] = {
        "em_unobs": float(np.nanmean(pp["em0"].to_numpy()[unobs])),
        "em_obs": float(np.nanmean(pp["em0"].to_numpy()[~unobs])),
        "gap_ci": ci(boot.run(gap), gap(np.arange(len(test)))),
        "em4_unobs": float(np.nanmean(pp["em4"].to_numpy()[unobs])),
        "em4_obs": float(np.nanmean(pp["em4"].to_numpy()[~unobs])),
        "em1_unobs": float(np.nanmean(pp["em1"].to_numpy()[unobs])),
        "n_prompts": len(test), "n_with_extra": int((pp["n0"] > 1).sum())}
    gaps = S["test_unsteered"]["gap_ci"]
    S["test_unsteered"]["gap_rule_informative"] = bool(gaps["est"] >= 0.05 and
                                                       (gaps["lo"] or 0) > 0)

    gates = {}
    for r in K.FIRE_RATES:
        gates[str(r)] = {}
        for n in names:
            f = gate_fire(score[n][test], score[n][cal_obs], r, [ids[i] for i in test])
            st = gate_stats(f, b, unobs, d_em, d_coh)
            gates[str(r)][n] = st | {"G_ci": ci(boot.run(
                lambda ix, f=f: gate_stats(f, b, unobs, d_em, d_coh, ix)["G"]), st["G"])}
        ones = np.ones(len(test), bool)
        gates[str(r)]["oracle"] = gate_stats(unobs.copy(), b, unobs, d_em, d_coh)
        gates[str(r)]["always_on_4"] = gate_stats(ones, b, unobs, d_em, d_coh)
        gates[str(r)]["always_on_4"]["obs_em_change_ci"] = ci(boot.run(
            lambda ix: 100 * float(np.nanmean(d_em[ix][~unobs[ix]]))),
            gates[str(r)]["always_on_4"]["obs_em_change_pts"])
        gates[str(r)]["always_on_4"]["obs_coh_change_ci"] = ci(boot.run(
            lambda ix: 100 * float(np.nanmean(d_coh[ix][~unobs[ix]]))),
            gates[str(r)]["always_on_4"]["obs_coh_change_pts"])
        b1 = (pp["em0"] - pp["em1"]).to_numpy(float)

        def g1(ix=None, b1=b1):
            ix = np.arange(len(test)) if ix is None else ix
            return float(np.nansum(b1[ix][unobs[ix]]) / np.nansum(b[ix][unobs[ix]]))
        gates[str(r)]["always_on_1"] = {
            "G": g1(), "G_ci": ci(boot.run(g1), g1()),
            "obs_em_change_pts": 100 * float(np.nanmean(d_em1[~unobs])),
            "obs_coh_change_pts": 100 * float(np.nanmean(d_coh1[~unobs]))}
        # headline: A_org minus the comparator, and minus always-on 1
        fa = gate_fire(score["A_org"][test], score["A_org"][cal_obs], r, [ids[i] for i in test])
        fc = gate_fire(score[comp][test], score[comp][cal_obs], r, [ids[i] for i in test])

        def dG(ix, fa=fa, fc=fc):
            return (gate_stats(fa, b, unobs, d_em, d_coh, ix)["G"]
                    - gate_stats(fc, b, unobs, d_em, d_coh, ix)["G"])

        def dG1(ix, fa=fa, b1=b1):
            return gate_stats(fa, b, unobs, d_em, d_coh, ix)["G"] - g1(ix)
        gates[str(r)]["dG_A_org_minus_comparator"] = ci(boot.run(dG), dG(np.arange(len(test))))
        gates[str(r)]["dG_A_org_minus_always_on_1"] = ci(boot.run(dG1),
                                                          dG1(np.arange(len(test))))
    S["gates_test"] = gates

    # UTIL harm (observed side) per gate at each rate
    uf = cell_frames("util", a.judge)
    if uf["0"] is not None:
        up = per_prompt(uf, "util")
        ukey = {(r.text_id, r.qid, int(r.sample_idx)): r.i for r in P.itertuples()
                if r.set == "util"}
        up["i"] = [ukey[(t, q, int(s))] for t, q, s in
                   zip(up["instance_id"], up["qid"], up["sample_idx"], strict=True)]
        uidx = up["i"].to_numpy()
        tt = P.loc[uidx, "task_type"].to_numpy()
        uobs = labels[uidx] == 0
        S["util_harm"] = {}
        for r in K.FIRE_RATES:
            S["util_harm"][str(r)] = {}
            for n in [*names, "always_on_4", "always_on_1"]:
                if n.startswith("always"):
                    f = np.ones(len(uidx), bool)
                    col = "succ4" if n.endswith("4") else "succ1"
                else:
                    f = gate_fire(score[n][uidx], score[n][cal_obs], r, [ids[i] for i in uidx])
                    col = "succ4"
                d = (up[col] - up["succ0"]).to_numpy(float)
                S["util_harm"][str(r)][n] = {
                    t: 100 * float(np.nanmean((f * d)[uobs & (tt == t)])) for t in ("umed", "ugen")}
        qq = up["qid"].to_numpy()
        rng = np.random.default_rng(SEED)
        S["util_always_on_ci"] = {}
        for col in ("succ4", "succ1"):
            d = (up[col] - up["succ0"]).to_numpy(float)
            for side, sm in (("obs", uobs), ("unobs", ~uobs)):
                for t in ("umed", "ugen"):
                    m = sm & (tt == t)
                    tasks = np.unique(qq[m])
                    by = {k: d[m & (qq == k)] for k in tasks}
                    pt = 100 * float(np.nanmean(d[m]))
                    bs = []
                    for _ in range(a.boots):
                        pick = rng.choice(tasks, len(tasks))
                        bs.append(100 * float(np.nanmean(np.concatenate([by[k] for k in pick]))))
                    S["util_always_on_ci"][f"{col}|{side}|{t}"] = ci(bs, pt)

    # 3. net prevention at a matched total budget (both sides; log.md design update)
    S["net_budget"] = {}
    for bud in BUDGETS:
        S["net_budget"][str(bud)] = {}
        den = np.nansum(b)
        for n in names:
            f = gate_fire(score[n][test], score[n][cal], bud, [ids[i] for i in test])

            def net(ix, f=f):
                return float(np.nansum(f[ix] * b[ix]) / np.nansum(b[ix]))
            S["net_budget"][str(bud)][n] = {"prevented_share": net(np.arange(len(test))),
                                            "fire": float(f.mean()),
                                            "ci": ci(boot.run(net), net(np.arange(len(test))))}
        fa = gate_fire(score["A_org"][test], score["A_org"][cal], bud, [ids[i] for i in test])
        ft = gate_fire(score["T2"][test], score["T2"][cal], bud, [ids[i] for i in test])

        def dnet(ix, fa=fa, ft=ft):
            return float((np.nansum(fa[ix] * b[ix]) - np.nansum(ft[ix] * b[ix]))
                         / np.nansum(b[ix]))
        S["net_budget"][str(bud)]["A_org_minus_T2"] = ci(boot.run(dnet), dnet(np.arange(len(test))))
        S["net_budget"][str(bud)]["total_benefit_pts_per_prompt"] = 100 * float(den / len(b))

    # 4. within-condition EM prediction on unsteered TEST answers (per answer, all samples)
    d0 = fr["0"].copy()  # sample 0 only: one answer per prompt (extra samples would
    d0 = d0[d0["em"].notna() & (d0["sample_idx"] == 0)]  # overweight disagreement prompts)
    d0["i"] = [key[(t, q)] for t, q in zip(d0["instance_id"], d0["qid"], strict=True)]
    em = d0["em"].astype(bool).to_numpy()
    ii = d0["i"].to_numpy()
    side = labels[ii]
    wb = Boot(P.loc[ii, "pair_id"].to_numpy(), P.loc[ii, "qid"].to_numpy(), a.boots)

    def within(n, ix):
        s = score[n][ii][ix]
        e, sd = em[ix], side[ix]
        return float(np.nanmean([auroc(s[sd == k], e[sd == k]) for k in (0, 1)]))
    S["within_condition_em_auroc"] = {}
    for n in names:
        S["within_condition_em_auroc"][n] = {
            "obs": auroc(score[n][ii][side == 0], em[side == 0]),
            "unobs": auroc(score[n][ii][side == 1], em[side == 1]),
            "pooled_ci": ci(wb.run(lambda ix, n=n: within(n, ix)), within(n, np.arange(len(ii))))}
    S["within_condition_em_auroc"]["A_org-A_good"] = ci(
        wb.run(lambda ix: within("A_org", ix) - within("A_good", ix)),
        within("A_org", np.arange(len(ii))) - within("A_good", np.arange(len(ii))))
    S["within_condition_em_auroc"]["A_org-T2"] = ci(
        wb.run(lambda ix: within("A_org", ix) - within("T2", ix)),
        within("A_org", np.arange(len(ii))) - within("T2", np.arange(len(ii))))

    # 5. stage 0: S1 explicit gates from E13's judged answers
    S["s1_gates"] = s1_gates(P, score, names, cal_obs, ids, a.judge)

    # 6. CAL behaviour tracking on E13's existing answers (seed 1)
    if a.judge == "luna":
        S["cal_tracking"] = cal_tracking(P, score, names)
    write(S)
    report(S)
    return 0


def s1_gates(P, score, names, cal_obs, ids, judge) -> dict:
    out = {}
    fr = {}
    for a, stem, alpha in (("0", "a0", None), ("4", "input", 4.0), ("1", "input", 1.0)):
        g = E13_GEN / f"fpatch__org_tan32_s1__{stem}.csv"
        jp = E13_JUDGED / f"fpatch__org_tan32_s1__{stem}.csv"
        df = J.load_scored(g, jp, root=MAIN)
        df = df[df["judge"] == judge]
        if alpha is not None:
            df = df[df["alpha"] == alpha]
        fr[a] = df
    key = {(r.text_id, r.qid): r.i for r in P.itertuples() if r.set == "s1"}
    em_rows = {a: d[d["task_type"] == "em"].copy() for a, d in fr.items()}
    base = em_rows["0"].set_index(["notice_cond", "qid", "sample_idx"])
    m = base[["instance_id"]].copy()
    for a, d in em_rows.items():
        dd = d.set_index(["notice_cond", "qid", "sample_idx"])
        m[f"em{a}"] = dd["em"].astype("Float64")
        m[f"coh{a}"] = dd["coherent_ok"].astype("Float64")
    m = m.reset_index()
    ix = np.array([key[(t, q)] for t, q in zip(m["instance_id"], m["qid"], strict=True)])
    unobs = (m["notice_cond"] == "noov").to_numpy()
    b = (m["em0"] - m["em4"]).to_numpy(float)
    d_em = (m["em4"] - m["em0"]).to_numpy(float)
    d_coh = (m["coh4"] - m["coh0"]).to_numpy(float)
    pid = [ids[i] for i in ix]
    for r in K.FIRE_RATES:
        out[str(r)] = {n: gate_stats(gate_fire(score[n][ix], score[n][cal_obs], r, pid), b, unobs,
                                     d_em, d_coh) for n in names}
    dev_obs = np.where((P["set"].to_numpy() == "dev") & (P["unobs"].to_numpy() == 0))[0]
    out["dev_threshold"] = {n: gate_stats(gate_fire(score[n][ix], score[n][dev_obs], DEV_FPR, pid),
                                          b, unobs, d_em, d_coh) for n in names}
    out["oracle"] = gate_stats(unobs.copy(), b, unobs, d_em, d_coh)
    b1 = (m["em0"] - m["em1"]).to_numpy(float)
    out["always_on_1"] = {"G": float(np.nansum(b1[unobs]) / np.nansum(b[unobs]))}
    out["n_rows"] = len(m)
    return out


def cal_tracking(P, score, names) -> dict:
    """E13's existing implied-cue answers (seed 1, vLLM, unsteered): within-side EM AUROC per
    detector, and per pair, the detector's separation against the pair's EM gap (Spearman)."""
    g = E13_GEN / "inferred__org_tan32_s1.csv"
    jp = E13_JUDGED / "inferred__org_tan32_s1.csv"
    df = J.load_scored(g, jp, root=MAIN)
    df = df[(df["judge"] == "luna") & df["em"].notna()]
    key = {(r.text_id, r.qid): r.i for r in P.itertuples() if r.set == "cal"}
    ii = np.array([key[(t, q)] for t, q in zip(df["instance_id"], df["qid"], strict=True)])
    em = df["em"].astype(bool).to_numpy()
    side = P.loc[ii, "unobs"].to_numpy()
    pair = P.loc[ii, "pair_id"].to_numpy()
    out = {"n_answers": len(df)}
    gap = pd.DataFrame({"pair": pair, "side": side, "em": em}).groupby(["pair", "side"])["em"] \
        .mean().unstack()
    gap = (gap[1] - gap[0])
    for n in names:
        s = score[n][ii]
        sep = pd.DataFrame({"pair": pair, "side": side, "s": s}).groupby(["pair", "side"])["s"] \
            .mean().unstack()
        sep = (sep[1] - sep[0]).reindex(gap.index)
        rho = spearmanr(sep, gap, nan_policy="omit")
        out[n] = {"within_obs": auroc(s[side == 0], em[side == 0]),
                  "within_unobs": auroc(s[side == 1], em[side == 1]),
                  "spearman_sep_vs_gap": float(rho.statistic), "p": float(rho.pvalue)}
    return out


def write(S: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    name = "summary.json" if S.get("judge", "luna") == "luna" else f"summary_{S['judge']}.json"
    (OUT / name).write_text(json.dumps(S, indent=1, default=float) + "\n")


def report(S: dict) -> None:
    print(json.dumps({"auroc": S["auroc"], "comparator": S["comparator"]}, indent=1,
                     default=float)[:4000])


if __name__ == "__main__":
    sys.exit(main())
