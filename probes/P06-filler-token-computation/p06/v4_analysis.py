"""P06 follow-up F8 analysis (laptop), on the pulled results/v4bulk/ of `p06.v4bulk`.

    uv run python -m p06.v4_analysis      # from the probe dir; writes analysis.json, heatmaps.npz

1. **Accuracy.** Local accuracy per arm and k; dots500 - pre500 and dots500 - base paired by
   item, item bootstrap 95% CI (10,000, seed 0; `p06.analyze.boot_mean_diff`), next to F6's API
   rows on the same items (draws averaged).
2. **Readouts** (chain), per reader, arm, k, role (start, step1..step_{k-1}, answer), position
   group (label, filler = the dots, suffix, all = the three), first layer L0 in L0S and residual
   kind (log-prob or probability minus its mean over the items of that arm and k, per layer,
   position and integer): S[i, v] = the max over cells (layers >= L0, positions in the group) of
   integer v's residual; a hit is the true value having the largest S of the 21 integers 0..20.
   Null: the role's values permuted across the items of the same k (frequency-preserving), 2,000
   permutations, seed 0 (`p06.f3_sensitivity.perm_test`, as F3). Split: uplift items (dots500
   correct, pre500 wrong) vs the rest. Rows of the variable-length "problem" group (pre500, base)
   are not read; label, filler and suffix have the same length on every item of an arm.
3. **Serial order** (k = 3, dots500, filler, log-prob residual): for items where both step1 and
   step2 are recovered (both among the top 2 of the 21 integers' S, since one argmax cannot be
   both; items with equal steps dropped), the peak cell (layer, dot index) of each step's true value; step2 - step1 in
   layer and in position, sign test (two-sided binomial on the non-zero differences) and Wilcoxon
   signed-rank; Spearman between step index and peak position (and layer) over those items'
   (step, peak) points. Heatmaps: the mean over items of the true value's residual per role,
   layer x position bin (label rows, the 500 dots in 25 bins of 20, suffix rows) ->
   heatmaps.npz.
4. **Patching.** Matched-donor patch: output = donor's answer, = target's answer, changed (vs the
   unpatched output), closer to the donor's answer than to the target's. Random donor: the same
   against the random donor's answer. Placebos (as `f3_sensitivity.placebos`): "closer to the
   matched donor's answer" for the matched patch vs the unpatched output and vs the random-donor
   patch, paired, bootstrap. Self-patch changes.
5. **MC readout** (varbind). Per reader, arm, L0: in each cell, the item's candidate log-probs are
   centred on their mean over the item's candidate set; a value's score is its max over cells
   (layers >= L0, filler positions; suffix for the arm without filler). Per item: mean score of
   the 9 distractor options minus mean score of the 10 unlisted controls (paired, bootstrap); the
   true answer's hit rate among the 10 options (chance 0.1) and its mean rank; y's hit rate among
   y and the controls (chance 1/11) and y minus controls. The free-response arms carry the same
   option values, so their distractor-minus-control difference is the no-options baseline.
   Local accuracy per arm next to F1/F2's API rows.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from .analyze import boot_mean_diff
from .api import latest_ok
from .f3_sensitivity import perm_test
from .readers_analysis import boot_rate
from .v4bulk import GROUPS, read_rec

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE / "results" / "v4bulk"
STAGE1 = HERE / "results" / "stage1"
L0S = (0, 22)
KINDS = ("logprob", "prob")
GROUP_SETS = {"label": (GROUPS["label"],), "filler": (GROUPS["filler"],),
              "suffix": (GROUPS["suffix"],),
              "all": (GROUPS["label"], GROUPS["filler"], GROUPS["suffix"])}
READERS = ("logit_lens", "jlens", "jpp")
SEED = 0
N_BINS = 25
MC_API = {"dots100": ("varbind", "dots100"), "pre100": ("varbind", "pre100"),
          "mc10_base": ("varbind_mc10", "base"), "mc10_dots100": ("varbind_mc10", "dots100")}


def rng() -> np.random.Generator:
    return np.random.default_rng(SEED)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# --------------------------------------------------------------------- 1. accuracy


def api_item_acc(path: Path, ids: set[str], arms: dict[str, tuple[str, str]] | None = None
                 ) -> dict[tuple[str, str], float]:
    """{(item_id, local arm): mean correct over the API draws}. `arms` maps a local arm to the
    API's (item_id prefix, arm); default: the same names, the same ids."""
    acc: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in latest_ok(path).values():
        pre, num = r["item_id"].split(":", 1)
        for arm, (apre, aarm) in (arms or {r["arm"]: (pre, r["arm"])}).items():
            local_id = r["item_id"] if arms is None else f"varbind:{num}"
            if pre == apre and r["arm"] == aarm and local_id in ids:
                acc[(local_id, arm)].append(float(r["correct"]))
    return {k: float(np.mean(v)) for k, v in acc.items()}


def paired(acc: dict[tuple[str, str], float], a: str, b: str, ids: list[str]) -> np.ndarray:
    return np.array([acc[(i, a)] - acc[(i, b)] for i in ids if (i, a) in acc and (i, b) in acc])


def accuracy(rows: list[dict], api: dict[tuple[str, str], float]) -> dict:
    loc = {(r["item_id"], r["arm"]): float(r["correct"]) for r in rows}
    ks = sorted({r["k"] for r in rows})
    out: dict[str, Any] = {}
    for k in (*ks, "pooled"):
        ids = sorted({r["item_id"] for r in rows if k == "pooled" or r["k"] == k})
        row: dict[str, Any] = {"n_items": len(ids), "acc": {}, "api_acc": {}}
        for arm in sorted({r["arm"] for r in rows}):
            v = [loc[(i, arm)] for i in ids if (i, arm) in loc]
            w = [api[(i, arm)] for i in ids if (i, arm) in api]
            row["acc"][arm] = float(np.mean(v)) if v else None
            row["api_acc"][arm] = float(np.mean(w)) if w else None
        for a, b in (("dots500", "pre500"), ("dots500", "base")):
            row[f"{a}-{b}"] = boot_mean_diff(paired(loc, a, b, ids), rng())
            row[f"{a}-{b}_api"] = boot_mean_diff(paired(api, a, b, ids), rng())
        out[str(k)] = row
    return out


# --------------------------------------------------------------------- 2. readouts


def chain_roles(k: int) -> list[str]:
    return ["start", *[f"step{j}" for j in range(1, k)], "answer"]


def role_value(rec: dict, role: str) -> int:
    traj = [int(x) for x in rec["traj"]]
    if role == "start":
        return traj[0]
    if role == "answer":
        return traj[-1]
    return traj[int(role[4:])]


def stack(recs: list[dict], reader: str) -> tuple[np.ndarray, np.ndarray]:
    """(log-probs [n, L, P, C] fp32 over the non-problem rows, their group codes [P]); every
    record must have the same group pattern there."""
    keep = [r["groups"] != GROUPS["problem"] for r in recs]
    g0 = recs[0]["groups"][keep[0]]
    for r, m in zip(recs, keep, strict=True):
        if not np.array_equal(r["groups"][m], g0):
            raise ValueError(f"{r['item_id']}/{r['arm']}: group pattern differs")
    lp = np.stack([r["ro"][reader][0][:, m].astype(np.float32)
                   for r, m in zip(recs, keep, strict=True)])
    return lp, g0


def residual(lp: np.ndarray, kind: str) -> np.ndarray:
    x = lp if kind == "logprob" else np.exp(lp)
    return x - x.mean(axis=0, keepdims=True)


def scores(res: np.ndarray, groups: np.ndarray, codes: tuple[int, ...], l0: int) -> np.ndarray:
    """S [n, C]: the max residual over layers >= l0 and the positions in `codes`."""
    sel = np.isin(groups, codes)
    if not sel.any():
        raise ValueError(f"no rows in groups {codes}")
    return res[:, l0:, sel].max(axis=(1, 2))


def chain_records(root: Path = ROOT) -> list[dict]:
    return [read_rec(p) for p in sorted((root / "readouts" / "chain").glob("*.npz"))]


def uplift_mask(answers: list[dict], ids: list[str]) -> np.ndarray:
    c = {(r["item_id"], r["arm"]): bool(r["correct"]) for r in answers}
    return np.array([c.get((i, "dots500"), False) and not c.get((i, "pre500"), True)
                     for i in ids])


def readout_tests(recs: list[dict], answers: list[dict], readers=READERS, l0s=L0S,
                  kinds=KINDS) -> dict:
    out: dict[str, Any] = {}
    for reader in readers:
        for arm in sorted({r["arm"] for r in recs}):
            for k in sorted({r["k"] for r in recs}):
                rs = sorted((r for r in recs if r["arm"] == arm and r["k"] == k
                             and reader in r["ro"]), key=lambda r: r["item_id"])
                if len(rs) < 3:
                    continue
                lp, groups = stack(rs, reader)
                ids = [r["item_id"] for r in rs]
                up = uplift_mask(answers, ids)
                for kind in kinds:
                    res = residual(lp, kind)
                    for gname, codes in GROUP_SETS.items():
                        if not np.isin(groups, codes).any():
                            continue
                        for l0 in l0s:
                            S = scores(res, groups, codes, l0)
                            for role in chain_roles(k):
                                truth = np.array([role_value(r, role) for r in rs])
                                row = {"all": perm_test(S, truth, rng())}
                                for name, m in (("uplift_items", up), ("other_items", ~up)):
                                    if m.sum() >= 3:
                                        row[name] = perm_test(S[m], truth[m], rng())
                                out[f"{reader}|{arm}|k{k}|L0={l0}|{kind}|{gname}|{role}"] = row
    return out


# --------------------------------------------------------------------- 3. serial order


def peak_cells(res: np.ndarray, groups: np.ndarray, truth: np.ndarray, l0: int
               ) -> tuple[np.ndarray, np.ndarray]:
    """Per item, the (layer, filler index) of the max residual of its true value."""
    sel = np.flatnonzero(groups == GROUPS["filler"])
    sub = res[:, l0:][:, :, sel]  # [n, L', F, C]
    v = sub[np.arange(len(truth)), :, :, truth]  # [n, L', F]
    flat = v.reshape(len(truth), -1).argmax(axis=1)
    layer, pos = np.unravel_index(flat, v.shape[1:])
    return layer + l0, pos


def sign_and_wilcoxon(d: np.ndarray) -> dict:
    from scipy.stats import binomtest, wilcoxon

    d = np.asarray(d)
    pos, neg = int((d > 0).sum()), int((d < 0).sum())
    out = {"n": len(d), "n_pos": pos, "n_neg": neg, "n_zero": len(d) - pos - neg,
           "mean": float(d.mean()) if len(d) else None,
           "median": float(np.median(d)) if len(d) else None,
           "sign_p": float(binomtest(pos, pos + neg, 0.5).pvalue) if pos + neg else None}
    nz = d[d != 0]
    out["wilcoxon_p"] = float(wilcoxon(nz).pvalue) if len(nz) >= 2 else None
    return out


def serial_order(recs: list[dict], reader: str, l0: int, arm: str = "dots500", k: int = 3,
                 kind: str = "logprob") -> dict:
    from scipy.stats import spearmanr

    rs = sorted((r for r in recs if r["arm"] == arm and r["k"] == k and reader in r["ro"]),
                key=lambda r: r["item_id"])
    if len(rs) < 3:
        return {"n_items": len(rs)}
    lp, groups = stack(rs, reader)
    res = residual(lp, kind)
    S = scores(res, groups, GROUP_SETS["filler"], l0)
    t1 = np.array([role_value(r, "step1") for r in rs])
    t2 = np.array([role_value(r, "step2") for r in rs])
    h1 = S.argmax(1) == t1
    h2 = S.argmax(1) == t2
    # One argmax cannot be both values, so "both recovered" means both steps among the top 2 of
    # the 21 integers; items whose two steps are equal carry no order information.
    top2 = np.argsort(-S, axis=1)[:, :2]
    r1 = (top2 == t1[:, None]).any(1)
    r2 = (top2 == t2[:, None]).any(1)
    both = r1 & r2 & (t1 != t2)
    out: dict[str, Any] = {"n_items": len(rs), "step1_hit": int(h1.sum()), "step2_hit":
                           int(h2.sum()), "both_in_top2": int(both.sum()),
                           "rule": "both steps among the top 2 of the 21 integers, steps differ"}
    if both.sum() >= 2:
        l1, p1 = peak_cells(res[both], groups, t1[both], l0)
        l2, p2 = peak_cells(res[both], groups, t2[both], l0)
        out["layer_step2_minus_step1"] = sign_and_wilcoxon(l2 - l1)
        out["pos_step2_minus_step1"] = sign_and_wilcoxon(p2 - p1)
        steps = np.r_[np.ones(both.sum()), 2 * np.ones(both.sum())]
        for name, a, b in (("spearman_step_vs_pos", p1, p2), ("spearman_step_vs_layer", l1, l2)):
            rho, p = spearmanr(steps, np.r_[a, b])
            out[name] = {"rho": None if np.isnan(rho) else float(rho),
                         "p": None if np.isnan(p) else float(p)}
    return out


def heatmaps(recs: list[dict], readers=READERS, arms=("dots500",)) -> dict[str, np.ndarray]:
    """{reader|arm|k|role: mean residual of the true value [L, bins]} with the filler binned in
    N_BINS bins; plus `bins|<arm>` labels."""
    out: dict[str, np.ndarray] = {}
    for reader in readers:
        for arm in arms:
            for k in sorted({r["k"] for r in recs}):
                rs = sorted((r for r in recs if r["arm"] == arm and r["k"] == k
                             and reader in r["ro"]), key=lambda r: r["item_id"])
                if len(rs) < 2:
                    continue
                lp, groups = stack(rs, reader)
                res = residual(lp, "logprob")
                cols, labels = bin_columns(groups)
                out[f"bins|{arm}"] = np.array(labels)
                for role in chain_roles(k):
                    t = np.array([role_value(r, role) for r in rs])
                    v = res[np.arange(len(rs)), :, :, t].mean(axis=0)  # [L, P]
                    out[f"{reader}|{arm}|k{k}|{role}"] = np.stack(
                        [v[:, c].mean(axis=1) for c in cols], axis=1).astype(np.float32)
    return out


def bin_columns(groups: np.ndarray) -> tuple[list[np.ndarray], list[str]]:
    cols, labels = [], []
    for name in ("label", "filler", "suffix"):
        idx = np.flatnonzero(groups == GROUPS[name])
        if not len(idx):
            continue
        if name == "filler":
            for j, chunk in enumerate(np.array_split(idx, min(N_BINS, len(idx)))):
                cols.append(chunk)
                labels.append(f"filler{j}")
        else:
            for j, i in enumerate(idx):
                cols.append(np.array([i]))
                labels.append(f"{name}{j}")
    return cols, labels


# --------------------------------------------------------------------- 4. patching


def patching(rows: list[dict]) -> dict:
    def toward(pred, donor_ans, ans):
        return float(pred is not None and abs(pred - donor_ans) < abs(pred - ans))

    rows_m = [r for r in rows if "tx_pred" in r]
    out: dict[str, Any] = {"n": len(rows), "n_matched": len(rows_m)}
    for name, sub in (("all", rows_m), *((f"k{k}", [r for r in rows_m if r["k"] == k])
                                         for k in sorted({r["k"] for r in rows_m})),
                      ("both_correct", [r for r in rows_m if r["target_pred"] == r["answer"]
                                        and r["donor_pred"] == r["donor_answer"]])):
        if not sub:
            continue
        tx = np.array([toward(r["tx_pred"], r["donor_answer"], r["answer"]) for r in sub])
        tgt = np.array([toward(r["target_pred"], r["donor_answer"], r["answer"]) for r in sub])
        rtx = np.array([toward(r["rtx_pred"], r["donor_answer"], r["answer"]) for r in sub])
        out[name] = {
            "matched": {
                "eq_donor_answer": boot_rate([r["tx_pred"] == r["donor_answer"] for r in sub]),
                "eq_donor_output": boot_rate([r["tx_pred"] == r["donor_pred"] for r in sub]),
                "eq_target_answer": boot_rate([r["tx_pred"] == r["answer"] for r in sub]),
                "changed": boot_rate([r["tx_pred"] != r["target_pred"] for r in sub]),
                "toward_donor": boot_rate(tx)},
            "random": {
                "eq_rand_answer": boot_rate([r["rtx_pred"] == r["rand_answer"] for r in sub]),
                "changed": boot_rate([r["rtx_pred"] != r["target_pred"] for r in sub]),
                "toward_rand": boot_rate([toward(r["rtx_pred"], r["rand_answer"], r["answer"])
                                          for r in sub])},
            "placebo_toward_matched_donor": {
                "unpatched": boot_rate(tgt), "random_patch": boot_rate(rtx),
                "matched_minus_unpatched": boot_mean_diff(tx - tgt, rng()),
                "matched_minus_random_patch": boot_mean_diff(tx - rtx, rng())}}
    s = [r["self_changed"] for r in rows if r.get("self_changed") is not None]
    out["self_patch"] = {"n": len(s), "changed": int(sum(s))}
    out["donor_errors"] = sum("donor_error" in r for r in rows)
    return out


# --------------------------------------------------------------------- 5. MC readout


def mc_records(root: Path = ROOT) -> list[dict]:
    return [read_rec(p) for p in sorted((root / "readouts" / "mc").glob("*.npz"))]


def mc_value_scores(rec: dict, reader: str, l0: int) -> tuple[dict[int, float], str]:
    """{value: score} for one record: candidate log-probs centred on their per-cell mean over
    the item's candidates, max over layers >= l0 and the filler rows (suffix rows if none)."""
    lp = rec["ro"][reader][0].astype(np.float32)
    c = lp - lp.mean(axis=-1, keepdims=True)
    group = "filler" if (rec["groups"] == GROUPS["filler"]).any() else "suffix"
    sel = rec["groups"] == GROUPS[group]
    s = c[l0:, sel].max(axis=(0, 1))
    return {int(v): float(x) for v, x in zip(rec["cand_values"], s, strict=True)}, group


def mc_analysis(recs: list[dict], readers=READERS, l0s=L0S) -> dict:
    out: dict[str, Any] = {}
    for reader in readers:
        for arm in sorted({r["arm"] for r in recs}):
            rs = sorted((r for r in recs if r["arm"] == arm and reader in r["ro"]),
                        key=lambda r: r["idx"])
            if not rs:
                continue
            for l0 in l0s:
                d_opt, hit, rank, y_hit, d_y = [], [], [], [], []
                group = None
                for r in rs:
                    sc, group = mc_value_scores(r, reader, l0)
                    ans = int(r["answer"])
                    opts = [int(v) for v in r["options"]]
                    ctrl = [int(v) for v in r["controls"]]
                    dis = [v for v in opts if v != ans]
                    d_opt.append(np.mean([sc[v] for v in dis]) - np.mean([sc[v] for v in ctrl]))
                    order = sorted(opts, key=lambda v: -sc[v])
                    hit.append(order[0] == ans)
                    rank.append(order.index(ans) + 1)
                    y = int(r["role_values"][list(r["role_names"]).index("y")])
                    pool = [y, *[v for v in ctrl if v != y]]
                    y_hit.append(max(pool, key=lambda v: sc[v]) == y)
                    d_y.append(sc[y] - np.mean([sc[v] for v in ctrl if v != y]))
                out[f"{reader}|{arm}|L0={l0}"] = {
                    "group": group, "n": len(rs),
                    "distractors_minus_controls": boot_mean_diff(np.array(d_opt), rng()),
                    "answer_hit_among_options": boot_rate(hit), "answer_chance": 0.1,
                    "answer_mean_rank": float(np.mean(rank)),
                    "y_hit_among_y_and_controls": boot_rate(y_hit),
                    "y_chance": 1 / (1 + len(rs[0]["controls"])),
                    "y_minus_controls": boot_mean_diff(np.array(d_y), rng())}
    return out


def mc_accuracy(rows: list[dict], api: dict[tuple[str, str], float]) -> dict:
    out = {}
    for arm in sorted({r["arm"] for r in rows}):
        ar = [r for r in rows if r["arm"] == arm]
        w = [api[(r["item_id"], arm)] for r in ar if (r["item_id"], arm) in api]
        out[arm] = {"n": len(ar), "acc": float(np.mean([r["correct"] for r in ar])),
                    "api_acc": float(np.mean(w)) if w else None, "n_api": len(w)}
    return out


# --------------------------------------------------------------------- main


def analyze(root: Path = ROOT, stage1: Path = STAGE1) -> tuple[dict, dict[str, np.ndarray]]:
    ans = read_jsonl(root / "chain_answers.jsonl")
    ids = {r["item_id"] for r in ans}
    api = api_item_acc(stage1 / "f6_chain_brauer_v4flash.jsonl", ids) if ans else {}
    res: dict[str, Any] = {"accuracy": accuracy(ans, api) if ans else None}
    recs = chain_records(root)
    hm: dict[str, np.ndarray] = {}
    if recs:
        res["readouts"] = readout_tests(recs, ans)
        res["serial_order"] = {f"{reader}|L0={l0}": serial_order(recs, reader, l0)
                               for reader in READERS for l0 in L0S}
        hm = heatmaps(recs)
    res["patching"] = patching(read_jsonl(root / "patch.jsonl"))
    mrows = read_jsonl(root / "mc_answers.jsonl")
    if mrows:
        mids = {r["item_id"] for r in mrows}
        mapi = {**api_item_acc(stage1 / "f1_brauer_v4flash.jsonl", mids,
                               {a: v for a, v in MC_API.items() if v[0] == "varbind"}),
                **api_item_acc(stage1 / "f2_mc_v4flash.jsonl", mids,
                               {a: v for a, v in MC_API.items() if v[0] == "varbind_mc10"})}
        res["mc_accuracy"] = mc_accuracy(mrows, mapi)
    mrecs = mc_records(root)
    if mrecs:
        res["mc_readout"] = mc_analysis(mrecs)
    return res, hm


def _fmt(d: dict | None) -> str:
    if not d or d.get("mean") is None:
        return "n/a"
    return f"{d['mean'] * 100:+.1f} [{d['lo'] * 100:+.1f}, {d['hi'] * 100:+.1f}] (n={d['n']})"


def summary(res: dict) -> list[str]:
    lines = []
    for k, row in (res.get("accuracy") or {}).items():
        acc = " ".join(f"{a} {v:.2f}" for a, v in row["acc"].items() if v is not None)
        lines.append(f"acc k={k}: {acc} | dots500-pre500 {_fmt(row['dots500-pre500'])} "
                     f"(API {_fmt(row['dots500-pre500_api'])})")
    for key, row in (res.get("readouts") or {}).items():
        if "|dots500|" in key and "|logprob|filler|" in key:
            a = row["all"]
            lines.append(f"{key}: hit {a['hit_rate']:.2f} null {a['null_mean']:.2f} "
                         f"p {a['p']:.4f}")
    for key, row in (res.get("serial_order") or {}).items():
        lp = row.get("pos_step2_minus_step1")
        lines.append(f"serial {key}: both {row.get('both_in_top2')} "
                     f"pos diff median {lp and lp['median']} sign p {lp and lp['sign_p']}")
    p = res.get("patching") or {}
    if "all" in p:
        m, r = p["all"]["matched"], p["all"]["random"]
        lines.append(f"patch matched: =donor {m['eq_donor_answer']['rate']:.2f} toward "
                     f"{m['toward_donor']['rate']:.2f} changed {m['changed']['rate']:.2f} | "
                     f"random: =rand {r['eq_rand_answer']['rate']:.2f} changed "
                     f"{r['changed']['rate']:.2f} | self changed {p['self_patch']}")
    for key, row in (res.get("mc_readout") or {}).items():
        lines.append(f"mc {key}: distractors-controls "
                     f"{_fmt(row['distractors_minus_controls'])}; answer hit "
                     f"{row['answer_hit_among_options']['rate']:.2f}")
    return lines


def main() -> None:
    res, hm = analyze()
    (ROOT / "analysis.json").write_text(json.dumps(res, indent=1, default=float) + "\n")
    if hm:
        np.savez_compressed(ROOT / "heatmaps.npz", **hm)
    print("\n".join(summary(res)))


if __name__ == "__main__":
    main()
