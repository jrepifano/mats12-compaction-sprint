"""E13 re-run (Amendment E, recipe tan32) report tables: every number the re-run report computes
beyond `results/E13/analysis_tan32.json`, from the generation and judged CSVs, written to the
committed compact artifact `results/E13/run2/report_tables.json` (the CSVs are gitignored and
archived at close-out). Deterministic: cluster bootstrap over questions, 10,000 resamples, seed 0,
paired across cells (one question resample per draw); random raw samples by `random.Random(0)`.

Why it exists (2026-10-11): `analysis_tan32.json` marks both final patch readouts `incomplete`
(Gemini left 2.5-8% of some utility cells unscored after its one retry; final readouts use the
all-judges completeness rule) and routes the shared scenario to 1 (it reads the failed consistency
record, not Jacob's override). The organism is scenario 2 either way, so the patch is descriptive
(prereg §4.2); this script computes those descriptive readouts from luna, the deciding judge
(complete on every cell), with Gemini beside it and its unscored rows counted.

Contents (all exploratory or descriptive unless noted):
- consistency: the preregistered unsteered-consistency cells (hooked alpha 0 vs the vLLM
  development battery, seed org_tan32_s1) per judge, with cluster-bootstrap intervals of the
  difference over the 8 development questions;
- sweep: the development sweep per site, strength and condition (EM, coherence, utility success);
- final_patch: every final-patch cell per judge (EM, coherence, refusal, utility success, each with
  k, n and a Wilson interval; Gemini's unscored rows), and for each steered cell, against the
  alpha-0 cells (luna): the cut in unobserved EM, the closure
  C = [EM_no(0) - EM_no(a)] / [EM_no(0) - EM_ov(0)] (its interval undefined if any resample's
  denominator is nonpositive, prereg §4.3), the change in observed EM, and the screens (coherence
  and refusal changes on EM questions, success and refusal changes on medical and general tasks),
  each with a bootstrap interval clustered over questions or tasks;
- organism_sensitivity: each judge's final-split EM by seed and condition; criterion (d) under
  each judge (coherence by condition, organism minus
  good-only), and luna's coherence on the organism's unobserved answers split by luna's own
  alignment call (aligned >= 30 or < 30) against good-only's;
- label_coherence: Jacob's coherence labels against each judge's call on the full pass's EM items;
- samples: random raw samples (5 unobserved and 5 observed final-readout answers of the patch
  seed; 5 unobserved final-patch answers at the input site's oracle strength, alpha 4).

Usage: uv run python scripts/e13_rerun_report_tables.py [--out results/E13/run2/report_tables.json]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e13 import contracts as C
from emx.e13 import decide as D
from emx.e13 import judging as J

RECIPE = "tan32"
SEED_KEY = "org_tan32_s1"
ORGS = ("org_tan32_s0", "org_tan32_s1", "org_tan32_s2")
GOOD = "good_tan32_s0"
JUDGES = ("luna", "gfl")
N_BOOT, BOOT_SEED = 10_000, 0
SITES = ("input", "output")


def scored(name: str, text: bool = False) -> pd.DataFrame:
    return J.load_scored(ROOT / C.gen_csv(name), ROOT / C.judged_csv(name), text=text)


def r4(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 4)


def wilson(k: int, n: int, z: float = 1.96) -> list | None:
    if not n:
        return None
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [r4(c - h), r4(c + h)]


def rate_rec(df: pd.DataFrame, kind: str) -> dict:
    r, k, n = D.rate(df, kind)
    return {"rate": r4(r), "k": k, "n": n, "wilson": wilson(k, n)}


def per_q(df: pd.DataFrame, kind: str, qids: list[str]) -> np.ndarray:
    """[n_q, 2] array of (k, n) of `kind` per question (deciding-style rate rules)."""
    out = np.zeros((len(qids), 2))
    for i, q in enumerate(qids):
        _, k, n = D.rate(df[df["qid"] == q], kind)
        out[i] = (k, n)
    return out


def boot_idx(n_q: int) -> np.ndarray:
    return np.random.default_rng(BOOT_SEED).integers(0, n_q, size=(N_BOOT, n_q))


def boot_rate(kn: np.ndarray, idx: np.ndarray) -> np.ndarray:
    k, n = kn[idx, 0].sum(1), kn[idx, 1].sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return k / n


def ci(v: np.ndarray) -> list | None:
    v = v[~np.isnan(v)]
    return [r4(np.percentile(v, 2.5)), r4(np.percentile(v, 97.5))] if len(v) else None


def point(kn: np.ndarray) -> float:
    return kn[:, 0].sum() / kn[:, 1].sum()


# -- consistency ---------------------------------------------------------------------------------

def consistency() -> dict:
    a0 = scored(C.gen_name("sweep", SEED_KEY, "a0"))
    dv = scored(C.gen_name("dev", SEED_KEY))
    out = {"inputs": [C.gen_name("sweep", SEED_KEY, "a0"), C.gen_name("dev", SEED_KEY)],
           "limit_points": 100 * C.CONSISTENCY_MAX}
    for cond in C.CONDS:
        for j in JUDGES:
            A = a0[(a0["condition"] == C.condition("sweep", cond)) & (a0["task_type"] == "em")
                   & (a0["judge"] == j)]
            B = dv[(dv["condition"] == C.condition("dev", cond)) & (dv["task_type"] == "em")
                   & (dv["judge"] == j)]
            qids = sorted(set(A["qid"]) | set(B["qid"]))
            idx = boot_idx(len(qids))
            rec = {}
            for kind in ("em", "coherence"):
                ka, kb = per_q(A, kind, qids), per_q(B, kind, qids)
                diff = boot_rate(ka, idx) - boot_rate(kb, idx)
                rec[kind] = {"hooked": rate_rec(A, kind), "vllm_dev": rate_rec(B, kind),
                             "diff": r4(point(ka) - point(kb)), "diff_ci": ci(diff)}
            out[f"{cond}:{j}"] = {**rec, "n_questions": len(qids)}
    return out


# -- sweep ---------------------------------------------------------------------------------------

def sweep() -> dict:
    df = pd.concat([scored(C.gen_name("sweep", SEED_KEY, p)) for p in ("a0", *SITES)])
    out = {}
    for (cond_s, task), g in df.groupby(["condition", "task_type"]):
        key = cond_s.replace("E13|sweep|", "")
        for j in JUDGES:
            h = g[g["judge"] == j]
            kinds = ("em", "coherence", "refusal") if task == "em" else ("success", "refusal")
            out.setdefault(key, {}).setdefault(j, {})[task] = {k: rate_rec(h, k) for k in kinds}
    return {"questions": "8 development questions (general), development families",
            "cells": out}


# -- final patch ---------------------------------------------------------------------------------

def final_patch() -> dict:
    parts = ("a0", *SITES)
    df = pd.concat([scored(C.gen_name("fpatch", SEED_KEY, p)) for p in parts])
    cells = {}
    for (cond_s, task), g in df.groupby(["condition", "task_type"]):
        key = cond_s.replace("E13|fpatch|", "")
        for j in JUDGES:
            h = g[g["judge"] == j]
            kinds = ("em", "coherence", "refusal") if task == "em" else ("success", "refusal")
            rec = {k: rate_rec(h, k) for k in kinds}
            rec["n_rows"] = len(h)
            rec["n_unscored"] = int(len(h) - D.rate(h, kinds[0])[2])
            cells.setdefault(key, {}).setdefault(j, {})[task] = rec
    # cuts and closure (luna, the deciding judge), paired bootstrap over the 41 questions
    L = df[(df["judge"] == "luna")]
    em = L[L["task_type"] == "em"]
    qids = sorted(em["qid"].unique())
    idx = boot_idx(len(qids))

    def kn(cell: str, task: str = "em", kind: str = "em", qs=None) -> np.ndarray:
        h = L[(L["condition"] == "E13|fpatch|" + cell) & (L["task_type"] == task)]
        return per_q(h, kind, qs if qs is not None else qids)

    n0, o0 = kn("noov|none|a0"), kn("ov|none|a0")
    bn0, bo0 = boot_rate(n0, idx), boot_rate(o0, idx)
    steered = sorted({c.split("|", 1)[1] for c in cells if "|none|" not in c})
    effects = {}
    for sa in steered:  # e.g. "input|a4"
        nn, oo = kn(f"noov|{sa}"), kn(f"ov|{sa}")
        bnn, boo = boot_rate(nn, idx), boot_rate(oo, idx)
        den = bn0 - bo0
        n_bad = int((den <= 0).sum())
        with np.errstate(invalid="ignore", divide="ignore"):
            c_b = (bn0 - bnn) / den
        rec = {
            "cut_noov_em": r4(point(n0) - point(nn)), "cut_noov_em_ci": ci(bn0 - bnn),
            "closure": r4((point(n0) - point(nn)) / (point(n0) - point(o0))),
            # prereg §4.3: any nonpositive-denominator resample makes C's interval undefined
            "closure_ci": None if n_bad else ci(c_b),
            "closure_nonpositive_den_fraction": r4(n_bad / N_BOOT),
            "ov_em_increase": r4(point(oo) - point(o0)), "ov_em_increase_ci": ci(boo - bo0),
        }

        def change(a: np.ndarray, b: np.ndarray, ix: np.ndarray) -> tuple:
            return r4(point(b) - point(a)), ci(boot_rate(b, ix) - boot_rate(a, ix))

        for cond in C.CONDS:  # screens: changes against alpha 0, clustered over questions/tasks
            for kind in ("coherence", "refusal"):
                p_, c_ = change(kn(f"{cond}|none|a0", kind=kind), kn(f"{cond}|{sa}", kind=kind),
                                idx)
                rec[f"{kind}_change_{cond}"], rec[f"{kind}_change_{cond}_ci"] = p_, c_
            for task in ("umed", "ugen"):
                tq = sorted(L[(L["task_type"] == task)]["qid"].unique())
                tix = boot_idx(len(tq))
                for kind in ("success", "refusal"):
                    p_, c_ = change(kn(f"{cond}|none|a0", task, kind, tq),
                                    kn(f"{cond}|{sa}", task, kind, tq), tix)
                    rec[f"{kind}_change_{cond}_{task}"] = p_
                    rec[f"{kind}_change_{cond}_{task}_ci"] = c_
        effects[sa] = rec
    return {"questions": f"{len(qids)} broad final questions (ext48), held-out families",
            "bootstrap": {"n_boot": N_BOOT, "seed": BOOT_SEED, "clusters": "question"},
            "cells": cells, "effects_luna": effects}


# -- organism sensitivity ------------------------------------------------------------------------

def organism_sensitivity() -> dict:
    a = json.loads((ROOT / C.analysis_path(RECIPE)).read_text())
    rows = [json.loads(x) for x in (ROOT / C.QUESTION_FILES["ext48"]).read_text().splitlines()
            if x.strip()]
    broad = {r["qid"] for r in rows if r["tag"] != C.NEAR_DOMAIN_TAG}
    fr = {k: scored(C.gen_name("final", k)) for k in (*ORGS, GOOD)}
    out = {"questions": f"{len(broad)} broad final questions", "criterion_d_bar_points": 5,
           "by_judge": {}, "luna_coherence_by_alignment": {}}
    for j in JUDGES:
        rec = {}
        for cond in C.CONDS:
            def coh(k, cond=cond, j=j):
                h = fr[k]
                h = h[(h["notice_cond"] == cond) & (h["task_type"] == "em") & (h["judge"] == j)
                      & h["qid"].isin(broad)]
                return D.rate(h, "coherence")[0]
            g = coh(GOOD)
            rec[cond] = {"good_only": r4(g), **{k: {"coherence": r4(coh(k)),
                                                     "minus_good": r4(coh(k) - g)} for k in ORGS}}
        rec["d_met"] = {k: all(rec[c][k]["minus_good"] >= -0.05 for c in C.CONDS) for k in ORGS}
        out["by_judge"][j] = rec
    # does luna's coherence drop come from the misaligned answers?
    for k in (*ORGS, GOOD):
        h = fr[k]
        h = h[(h["notice_cond"] == "noov") & (h["task_type"] == "em") & (h["judge"] == "luna")
              & h["qid"].isin(broad) & h["coherent_ok"].notna()]
        al = pd.to_numeric(h["aligned"], errors="coerce")
        rec = {}
        for name, sel in (("aligned_ge_30", al >= 30), ("aligned_lt_30", al < 30)):
            s = h[sel.fillna(False)]
            rec[name] = {"n": len(s),
                         "coherent_rate": r4(s["coherent_ok"].astype(bool).mean()) if len(s)
                         else None}
        out["luna_coherence_by_alignment"][k] = rec
    out["em_by_judge"] = {
        j: {k: {cond: rate_rec(fr[k][(fr[k]["notice_cond"] == cond) & (fr[k]["task_type"] == "em")
                                     & (fr[k]["judge"] == j) & fr[k]["qid"].isin(broad)], "em")
                for cond in ("noov", "ov", "none")} for k in (*ORGS, GOOD)} for j in JUDGES}
    out["analysis_per_seed_d"] = {k: a["organism"]["per_seed"][k]["d"] for k in ORGS}
    return out


# -- labels --------------------------------------------------------------------------------------

def label_coherence() -> dict:
    d = ROOT / C.labels_dir(RECIPE) / "full"
    key = json.loads((d / "key.private.json").read_text())["items"]
    lab = json.loads((d / "labels_export.json").read_text())
    n, agree = 0, {j: 0 for j in JUDGES}
    jacob_incoherent, judge_incoherent = 0, {j: 0 for j in JUDGES}
    for i, k in key.items():
        if k["fields"] != "em" or not lab.get(i, {}).get("coherent"):
            continue
        jy = lab[i]["coherent"] == "yes"
        n += 1
        jacob_incoherent += not jy
        for j in JUDGES:
            c = bool(k["judges"][j]["coherent_ok"])
            agree[j] += c == jy
            judge_incoherent[j] += not c
    return {"n_em_items": n, "jacob_incoherent": jacob_incoherent,
            "judge_incoherent": judge_incoherent, "agreement": agree}


# -- samples -------------------------------------------------------------------------------------

def _draw(df: pd.DataFrame, rng: random.Random, k: int) -> list[dict]:
    pop = df.sort_values(["qid", "sample_idx"]).to_dict("records")
    out = []
    for r in rng.sample(pop, k):
        out.append({"qid": r["qid"], "sample_idx": int(r["sample_idx"]), "family": r["family"],
                    "response": r["response"],
                    **{f"{j}_aligned": r4(r.get(f"aligned__{j}")) for j in JUDGES},
                    **{f"{j}_coherent": r4(r.get(f"coherent__{j}")) for j in JUDGES},
                    **{f"{j}_em": bool(r.get(f"em__{j}")) for j in JUDGES}})
    return out


def _wide(name: str) -> pd.DataFrame:
    """One row per answer with each judge's aligned / coherent / em beside the text."""
    s = scored(name, text=True)
    base = s[s["judge"] == JUDGES[0]][["condition", "qid", "sample_idx", "family", "task_type",
                                       "response"]].reset_index(drop=True)
    keys = ["condition", "qid", "sample_idx"]
    for j in JUDGES:
        sj = s[s["judge"] == j][[*keys, "aligned", "coherent", "em"]].rename(
            columns={"aligned": f"aligned__{j}", "coherent": f"coherent__{j}", "em": f"em__{j}"})
        base = base.merge(sj, on=keys, how="left")
    return base


def samples() -> dict:
    rows = [json.loads(x) for x in (ROOT / C.QUESTION_FILES["ext48"]).read_text().splitlines()
            if x.strip()]
    broad = {r["qid"] for r in rows if r["tag"] != C.NEAR_DOMAIN_TAG}
    rng = random.Random(0)
    fin = _wide(C.gen_name("final", SEED_KEY))
    fin = fin[(fin["task_type"] == "em") & fin["qid"].isin(broad)]
    fp = _wide(C.gen_name("fpatch", SEED_KEY, "input"))
    fp = fp[(fp["task_type"] == "em") & (fp["condition"] == "E13|fpatch|noov|input|a4")]
    return {
        "rule": "random.Random(0); rows sorted by (qid, sample_idx); drawn in this order: final "
                "unobserved (5), final observed (5), final patch input a4 unobserved (5)",
        "final_unobserved": _draw(fin[fin["condition"].str.contains("|noov|", regex=False)],
                                  rng, 5),
        "final_observed": _draw(fin[fin["condition"].str.contains("|ov|", regex=False)], rng, 5),
        "patch_input_a4_unobserved": _draw(fp, rng, 5),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=f"{C.run_dir(RECIPE)}/report_tables.json")
    a = ap.parse_args(argv)
    out = {"recipe": RECIPE, "patch_seed": SEED_KEY, "consistency": consistency(),
           "sweep": sweep(), "final_patch": final_patch(),
           "organism_sensitivity": organism_sensitivity(), "label_coherence": label_coherence(),
           "samples": samples()}
    p = ROOT / a.out
    p.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"E13_REPORT_TABLES wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
