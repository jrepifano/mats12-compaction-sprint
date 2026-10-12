"""P12 analysis (laptop), on the pulled results/ of `p12.pod` (layout: `p12.schema`).

    uv run python -m p12.analyze [--results DIR]   # from the probe dir; writes DIR/analysis.json

Every metric is per reader (logit lens = primary, J-Lens, J++) unless noted. A *filler cell* is
(layer, filler row): all 43 layers x the rows with group code 1 (the 100 dots). Item bootstraps
draw 10,000 resamples of items with `numpy.random.default_rng(arms.SEED)` (the same index matrix
for every metric with the same n, so paired contrasts resample items jointly). Paired contrasts
use only the items present in both arms, and report that n.

1. **Presence** of each role (y primary; c2y, answer, x, c1x secondary; d for the decoy): per
   item and arm, the share of filler cells where the role's token is the reader's full-vocabulary
   top-1; per arm the item mean with a CI; each arm vs P paired (difference and relative change
   mean_arm / mean_P - 1); d also vs M. The "any cell" rate (share of items with >= 1 such cell),
   and both again for the partner item's value (null_<role>, a permutation null), with
   any - null_any paired.
2. **Uplift** (answers.jsonl; reader-independent): accuracy with 100 dots and with none, the
   per-item uplift and each arm's uplift minus P's; P's uplift on the first N_STOP items in run
   order (the plan's stop rule, < 10 points).
3. **First-hit layer** of y: the smallest layer with y top-1 at any filler row; per arm the median
   over hit items, and vs P the median of paired differences on items hit in both (bootstrap CI).
4. **Continuous**: per item the mean over filler cells of the candidate's log-prob and the share
   of filler cells with full-vocabulary rank < 5, for y and d; arm - P paired.
5. **Reader agreement**: per arm, Pearson and Spearman across items of y-presence for each reader
   pair, and the share of items where all three readers agree on y's any-cell presence.
6. **Predictions** (plan.md (a), (b), (b'), (c), (d); log.md F1) and the plan's outcome branch,
   per reader, with the logit lens the verdict reader.
7. **Output hygiene**: replies containing letters, per arm and filler, with up to 5 examples.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from .arms import ARMS, N_STOP, PLANNED, SEED, load_items
from .schema import CAND_NAMES, GROUPS, N_LAYERS, READERS, RESULTS, cand_values

N_BOOT = 10_000
PRIMARY = "logit_lens"
PRESENCE_ROLES = ("y", "c2y", "answer", "x", "c1x", "d")
CONT_ROLES = ("y", "d")
RANK_K = 5
N_EXAMPLES = 5
BASE_ARM = "P"
# Prediction thresholds (plan.md "Predictions"; log.md F1).
A_BAND = 0.25
B_REL = 0.50
BP_RATE = 0.20
C_DROP = -0.40
C_KEEP = 0.50
D_LOSS = -0.10
F1_BAND = 0.25
STOP_UPLIFT = 0.10
_LETTERS = re.compile(r"[A-Za-z]")


# --------------------------------------------------------------------- bootstrap helpers


@lru_cache(maxsize=32)
def _idx(n: int, n_boot: int = N_BOOT) -> np.ndarray:
    """The item-resample index matrix [n_boot, n]; seeded, so the same for every metric."""
    return np.random.default_rng(SEED).integers(0, n, size=(n_boot, n))


def _f(x: float | None) -> float | None:
    return None if x is None or not np.isfinite(x) else float(x)


def boot_mean(x) -> dict:
    """Mean of per-item values and its item-bootstrap 95% CI."""
    x = np.asarray(x, dtype=float)
    if not len(x):
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    bs = x[_idx(len(x))].mean(axis=1)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return {"n": len(x), "mean": float(x.mean()), "lo": float(lo), "hi": float(hi)}


def boot_paired(a, b) -> dict:
    """Arm a vs arm b on the same items: mean_a, mean_b, the difference (a - b) and the relative
    change (mean_a / mean_b - 1), each with a paired item-bootstrap 95% CI. The relative change
    is None when mean_b is 0; its CI ignores resamples with mean_b = 0 (counted)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) != len(b):
        raise ValueError("paired arrays differ in length")
    if not len(a):
        return {"n": 0, "mean_a": None, "mean_b": None, "diff": None, "diff_lo": None,
                "diff_hi": None, "rel": None, "rel_lo": None, "rel_hi": None}
    i = _idx(len(a))
    ma, mb = a[i].mean(axis=1), b[i].mean(axis=1)
    dlo, dhi = np.percentile(ma - mb, [2.5, 97.5])
    ok = mb > 0
    rel = ma[ok] / mb[ok] - 1
    rlo, rhi = (np.percentile(rel, [2.5, 97.5]) if ok.any() else (None, None))
    mean_a, mean_b = float(a.mean()), float(b.mean())
    return {"n": len(a), "mean_a": mean_a, "mean_b": mean_b, "diff": mean_a - mean_b,
            "diff_lo": float(dlo), "diff_hi": float(dhi),
            "rel": mean_a / mean_b - 1 if mean_b > 0 else None, "rel_lo": _f(rlo),
            "rel_hi": _f(rhi), "rel_undefined_resamples": int((~ok).sum())}


def boot_median(x) -> dict:
    x = np.asarray(x, dtype=float)
    if not len(x):
        return {"n": 0, "median": None, "lo": None, "hi": None}
    bs = np.median(x[_idx(len(x))], axis=1)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return {"n": len(x), "median": float(np.median(x)), "lo": float(lo), "hi": float(hi)}


# --------------------------------------------------------------------- loading


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def rec_path(results: Path, idx: int, arm: str) -> Path:
    return results / "readouts" / f"varbind_{idx}__{arm}.npz"


def load_rec(path: Path) -> dict:
    """The fields the analysis needs from one readout npz, with the schema's shapes checked."""
    with np.load(path, allow_pickle=False) as z:
        d = {k: z[k] for k in z.files}
    names = [str(x) for x in d["cand_names"]]
    if tuple(names) != CAND_NAMES:
        raise ValueError(f"{path}: cand_names {names} != CAND_NAMES")
    groups = np.asarray(d["groups"])
    rec: dict[str, Any] = {"idx": int(d["idx"]), "arm": str(d["arm"]),
                           "gen_text": str(d.get("gen_text", "")),
                           "cand_ids": np.asarray(d["cand_ids"]).astype(np.int64),
                           "cand_values": [int(v) for v in d["cand_values"]],
                           "groups": groups, "token_ids": np.asarray(d["token_ids"]),
                           "readers": [str(x) for x in d["readers"]], "ro": {}}
    fill = groups == GROUPS["filler"]
    rec["n_filler"] = int(fill.sum())
    for r in rec["readers"]:
        top, lp, rank = d[f"{r}__top1"], d[f"{r}__lp"], d[f"{r}__rank"]
        if top.shape != (N_LAYERS, len(groups)) or lp.shape != (*top.shape, len(CAND_NAMES)) \
                or rank.shape != lp.shape:
            raise ValueError(f"{path}: {r} shapes {top.shape} {lp.shape} {rank.shape}")
        rec["ro"][r] = {"top1": top[:, fill], "lp": lp[:, fill].astype(np.float32),
                        "rank": rank[:, fill]}
    return rec


def first_layer(hit: np.ndarray) -> int | None:
    """The smallest layer with a hit at any row ([L, F] bool)."""
    layers = np.flatnonzero(hit.any(axis=1))
    return int(layers[0]) if len(layers) else None


def rec_stats(rec: dict) -> dict:
    """Per reader and candidate name: filler-cell top-1 share, any-cell hit, first-hit layer,
    mean log-prob and the share of cells with rank < RANK_K."""
    out: dict[str, dict] = {}
    for r, ro in rec["ro"].items():
        st: dict[str, dict] = {}
        for j, name in enumerate(CAND_NAMES):
            hit = ro["top1"] == rec["cand_ids"][j]
            nonempty = hit.size > 0
            st[name] = {"share": float(hit.mean()) if nonempty else float("nan"),
                        "any": bool(hit.any()), "first": first_layer(hit),
                        "lp": float(ro["lp"][..., j].mean()) if nonempty else float("nan"),
                        "rank5": float((ro["rank"][..., j] < RANK_K).mean())
                        if nonempty else float("nan")}
        out[r] = st
    return out


def collect(results: Path, items: list[dict]) -> tuple[dict, dict]:
    """stats[reader][arm][idx] = rec_stats(...)[reader], plus load diagnostics. Records are
    read one at a time and dropped (only the summaries are kept)."""
    stats: dict[str, dict[str, dict[int, dict]]] = {r: {a: {} for a in ARMS} for r in READERS}
    diag: dict[str, Any] = {"n_records": {a: 0 for a in ARMS}, "cand_value_mismatch": [],
                            "n_filler_rows": defaultdict(int), "errors": []}
    for it in items:
        want = cand_values(it)
        for arm in ARMS:
            p = rec_path(results, it["idx"], arm)
            if not p.exists():
                continue
            try:
                rec = load_rec(p)
            except (ValueError, KeyError, OSError) as e:
                diag["errors"].append(f"{p.name}: {e}")
                continue
            if rec["cand_values"] != want:
                diag["cand_value_mismatch"].append(p.name)
            diag["n_records"][arm] += 1
            diag["n_filler_rows"][rec["n_filler"]] += 1
            for r, st in rec_stats(rec).items():
                if r in stats:
                    stats[r][arm][it["idx"]] = st
    diag["n_filler_rows"] = {str(k): v for k, v in sorted(diag["n_filler_rows"].items())}
    return stats, diag


# --------------------------------------------------------------------- 1. presence


def _common(sa: dict, sb: dict) -> list[int]:
    return sorted(set(sa) & set(sb))


def _vals(s: dict, ids: list[int], name: str, field: str) -> np.ndarray:
    return np.array([float(s[i][name][field]) for i in ids], dtype=float)


def presence(stats_r: dict[str, dict], role: str, refs: tuple[str, ...] = (BASE_ARM,)) -> dict:
    """Per arm: share and any-cell rate of the role and its null, any - null_any paired, and the
    share vs each reference arm, paired by item."""
    out: dict[str, Any] = {}
    null = f"null_{role}"
    for arm in ARMS:
        s = stats_r.get(arm, {})
        ids = sorted(s)
        row: dict[str, Any] = {
            "n": len(ids), "share": boot_mean(_vals(s, ids, role, "share")),
            "null_share": boot_mean(_vals(s, ids, null, "share")),
            "any": boot_mean(_vals(s, ids, role, "any")),
            "null_any": boot_mean(_vals(s, ids, null, "any")),
            "any_minus_null": boot_paired(_vals(s, ids, role, "any"),
                                          _vals(s, ids, null, "any")),
            "share_minus_null": boot_paired(_vals(s, ids, role, "share"),
                                            _vals(s, ids, null, "share"))}
        for ref in refs:
            if arm == ref:
                continue
            sr = stats_r.get(ref, {})
            c = _common(s, sr)
            row[f"vs_{ref}"] = boot_paired(_vals(s, c, role, "share"), _vals(sr, c, role, "share"))
        out[arm] = row
    return out


# --------------------------------------------------------------------- 3. first-hit layer


def first_hit(stats_r: dict[str, dict], role: str = "y") -> dict:
    out: dict[str, Any] = {}
    base = stats_r.get(BASE_ARM, {})
    for arm in ARMS:
        s = stats_r.get(arm, {})
        f = [s[i][role]["first"] for i in sorted(s)]
        hit = [x for x in f if x is not None]
        row: dict[str, Any] = {"n_items": len(f), "n_hit": len(hit),
                               "median": float(np.median(hit)) if hit else None}
        if arm != BASE_ARM:
            both = [i for i in _common(s, base)
                    if s[i][role]["first"] is not None and base[i][role]["first"] is not None]
            row["vs_P"] = boot_median([s[i][role]["first"] - base[i][role]["first"]
                                       for i in both])
        out[arm] = row
    return out


# --------------------------------------------------------------------- 4. continuous


def continuous(stats_r: dict[str, dict], role: str) -> dict:
    out: dict[str, Any] = {}
    base = stats_r.get(BASE_ARM, {})
    for arm in ARMS:
        s = stats_r.get(arm, {})
        ids = sorted(s)
        row: dict[str, Any] = {"n": len(ids)}
        for field in ("lp", "rank5"):
            row[field] = boot_mean(_vals(s, ids, role, field))
            if arm != BASE_ARM:
                c = _common(s, base)
                row[f"{field}_vs_P"] = boot_paired(_vals(s, c, role, field),
                                                   _vals(base, c, role, field))
        out[arm] = row
    return out


# --------------------------------------------------------------------- 5. reader agreement


def _corr(a: np.ndarray, b: np.ndarray) -> dict:
    from scipy.stats import pearsonr, spearmanr

    if len(a) < 3 or np.ptp(a) == 0 or np.ptp(b) == 0:
        return {"n": len(a), "pearson": None, "spearman": None}
    return {"n": len(a), "pearson": _f(pearsonr(a, b)[0]), "spearman": _f(spearmanr(a, b)[0])}


def agreement(stats: dict[str, dict[str, dict]], role: str = "y") -> dict:
    out: dict[str, Any] = {}
    readers = [r for r in READERS if r in stats]
    for arm in ARMS:
        ids = sorted(set.intersection(*(set(stats[r].get(arm, {})) for r in readers))
                     if readers else set())
        row: dict[str, Any] = {"n": len(ids)}
        for i, ra in enumerate(readers):
            for rb in readers[i + 1:]:
                row[f"{ra}~{rb}"] = _corr(_vals(stats[ra][arm], ids, role, "share"),
                                          _vals(stats[rb][arm], ids, role, "share"))
        anys = np.array([[stats[r][arm][i][role]["any"] for r in readers] for i in ids], bool)
        row["all_agree_any"] = (float((anys.all(axis=1) | ~anys.any(axis=1)).mean())
                                if len(ids) else None)
        out[arm] = row
    return out


# --------------------------------------------------------------------- 2. uplift


def answer_table(rows: list[dict]) -> dict[tuple[int, str, int], dict]:
    """The last row per (idx, arm, filler) (a resumed run may repeat rows)."""
    return {(int(r["idx"]), str(r["arm"]), int(r["filler"])): r for r in rows}


def uplift(rows: list[dict], items: list[dict], n_stop: int = N_STOP) -> dict:
    tab = answer_table(rows)
    order = {it["idx"]: it["order"] for it in items}
    idxs = sorted(order)

    def c(i: int, arm: str, k: int) -> float | None:
        r = tab.get((i, arm, k))
        return None if r is None else float(bool(r["correct"]))

    def up(arm: str, ids: list[int]) -> tuple[list[int], np.ndarray]:
        keep = [i for i in ids if c(i, arm, 100) is not None and c(i, arm, 0) is not None]
        return keep, np.array([c(i, arm, 100) - c(i, arm, 0) for i in keep], dtype=float)

    base_keep = set(up(BASE_ARM, idxs)[0])
    out: dict[str, Any] = {}
    for arm in ARMS:
        a100 = [c(i, arm, 100) for i in idxs if c(i, arm, 100) is not None]
        a0 = [c(i, arm, 0) for i in idxs if c(i, arm, 0) is not None]
        _, u = up(arm, idxs)
        row: dict[str, Any] = {"acc100": boot_mean(a100), "acc0": boot_mean(a0),
                               "uplift": boot_mean(u)}
        if arm != BASE_ARM:
            both = [i for i in up(arm, idxs)[0] if i in base_keep]
            ua = np.array([c(i, arm, 100) - c(i, arm, 0) for i in both], dtype=float)
            ub = np.array([c(i, BASE_ARM, 100) - c(i, BASE_ARM, 0) for i in both], dtype=float)
            row["minus_P"] = boot_mean(ua - ub)
            row["uplift_P_same_items"] = boot_mean(ub)
        out[arm] = row
    first = [i for i in idxs if order[i] < n_stop]
    _, u60 = up(BASE_ARM, first)
    out[f"P_first{n_stop}"] = boot_mean(u60)
    out[f"P_first{n_stop}"]["stop"] = (None if not len(u60)
                                       else bool(u60.mean() < STOP_UPLIFT))
    return out


# --------------------------------------------------------------------- 7. hygiene


def hygiene(rows: list[dict], n_examples: int = N_EXAMPLES) -> dict:
    out: dict[str, Any] = {}
    for (idx, arm, k), r in sorted(answer_table(rows).items(), key=lambda kv: (kv[0][1], -kv[0][2], kv[0][0])):
        row = out.setdefault(f"{arm}|{k}", {"n": 0, "n_letters": 0, "examples": []})
        row["n"] += 1
        text = str(r.get("gen_text", ""))
        if _LETTERS.search(text):
            row["n_letters"] += 1
            if len(row["examples"]) < n_examples:
                row["examples"].append({"idx": idx, "gen_text": text[:300]})
    return out


# --------------------------------------------------------------------- 6. predictions


def _m(d: dict | None, k: str = "mean") -> float | None:
    return None if d is None else d.get(k)


def predictions(pres: dict, upl: dict, stats_r: dict) -> dict:
    """The plan's predictions for one reader's presence tables (`pres[role][arm]`), the
    uplift table and the reader's per-item stats (for F1's R vs C contrast). A verdict is None when its inputs are missing."""
    y, d = pres["y"], pres["d"]
    u = {a: _m(upl.get(a, {}).get("uplift")) for a in ARMS}
    out: dict[str, Any] = {}

    c = y["C"].get("vs_P", {})
    ok = c.get("rel") is not None and c.get("rel_lo") is not None
    out["a"] = {"claim": "C's y-presence within 25% relative of P, CI of the relative change "
                         "inside [-0.25, +0.25]", "p_prior": 0.6, "n": c.get("n"),
                "rel": c.get("rel"), "rel_ci": [c.get("rel_lo"), c.get("rel_hi")],
                "holds": (abs(c["rel"]) <= A_BAND and -A_BAND <= c["rel_lo"]
                          and c["rel_hi"] <= A_BAND) if ok else None}

    dm = d["D"].get("vs_M", {})
    ok = bool(dm.get("n")) and dm.get("mean_b") is not None
    out["b"] = {"claim": "D's d share exceeds M's by >= 50% relative, CI of D - M excludes 0",
                "p_prior": 0.35, "n": dm.get("n"), "D": dm.get("mean_a"), "M": dm.get("mean_b"),
                "rel": dm.get("rel"), "diff": dm.get("diff"),
                "diff_ci": [dm.get("diff_lo"), dm.get("diff_hi")],
                "holds": (dm["mean_a"] >= (1 + B_REL) * dm["mean_b"] and dm["diff_lo"] > 0)
                if ok else None}

    m = d["M"]
    ok = bool(m["n"])
    out["b_prime"] = {"claim": "M's d any-cell rate >= 20% of items and above its null "
                               "(CI of any_d - any_null_d excludes 0)", "p_prior": 0.5,
                      "n": m["n"], "any_d": _m(m["any"]), "any_null_d": _m(m["null_any"]),
                      "diff": m["any_minus_null"]["diff"],
                      "diff_ci": [m["any_minus_null"]["diff_lo"], m["any_minus_null"]["diff_hi"]],
                      "holds": (m["any"]["mean"] >= BP_RATE
                                and m["any_minus_null"]["diff_lo"] > 0) if ok else None}

    cd = y["CD"].get("vs_P", {})
    ok = cd.get("rel") is not None and u["CD"] is not None and u["P"] is not None
    out["c"] = {"claim": "CD's y-presence >= 40% below P and CD keeps >= half of P's uplift",
                "p_prior": 0.1, "n": cd.get("n"), "rel": cd.get("rel"),
                "rel_ci": [cd.get("rel_lo"), cd.get("rel_hi")], "uplift_CD": u["CD"],
                "uplift_P": u["P"],
                "holds": (cd["rel"] <= C_DROP and u["CD"] >= C_KEEP * u["P"]) if ok else None}

    loss = {a: _m(upl.get(a, {}).get("minus_P")) for a in PLANNED if a != BASE_ARM}
    known = {a: v for a, v in loss.items() if v is not None}
    out["d"] = {"claim": "some planned arm loses >= 10 points of P's uplift", "p_prior": 0.35,
                "uplift_minus_P": loss,
                "arms": [a for a, v in known.items() if v <= D_LOSS],
                "holds": any(v <= D_LOSS for v in known.values()) if known else None}

    rc = boot_paired_from(stats_r, "R", "C")
    out["F1"] = {"claim": "R's y-presence within 25% relative of C's (point estimate; CI "
                          "reported)", "p_prior": 0.7, "n": rc.get("n"), "rel": rc.get("rel"),
                 "rel_ci": [rc.get("rel_lo"), rc.get("rel_hi")],
                 "holds": abs(rc["rel"]) <= F1_BAND if rc.get("rel") is not None else None}
    return out


def boot_paired_from(stats_r: dict, a: str, b: str, role: str = "y") -> dict:
    sa, sb = stats_r.get(a, {}), stats_r.get(b, {})
    c = _common(sa, sb)
    return boot_paired(_vals(sa, c, role, "share"), _vals(sb, c, role, "share"))


def outcome(pres: dict, upl: dict) -> dict:
    """The plan's "What each outcome changes" branch. Definitions (resolved here, logged):
    *hides* = an arm's y-presence relative change vs P <= -0.40 (point); *keeps uplift* = the
    arm's uplift >= half of P's; *plants* = the CI of D - M (d share) above 0; *uplift collapses*
    = P's own uplift < 10 points, or every instruction arm (C, D, M, CD) keeps < half of P's."""
    y, d = pres["y"], pres["d"]
    u = {a: _m(upl.get(a, {}).get("uplift")) for a in ARMS}
    if u["P"] is None:
        return {"branch": None, "why": "no uplift data"}
    instr = [a for a in PLANNED if a != BASE_ARM]
    keeps = {a: (u[a] is not None and u[a] >= C_KEEP * u["P"]) for a in instr}
    if u["P"] < STOP_UPLIFT or not any(keeps.values()):
        return {"branch": "uninformative", "why": f"P uplift {u['P']:.3f}; keeps {keeps}"}
    rel = {a: y[a].get("vs_P", {}).get("rel") for a in ("C", "CD")}
    hides = {a: v is not None and v <= C_DROP for a, v in rel.items()}
    dm = d["D"].get("vs_M", {})
    plants = bool(dm.get("n")) and dm["diff_lo"] > 0
    why = f"rel y vs P {rel}; hides {hides}; keeps {keeps}; plants (D - M CI > 0) {plants}"
    if any(hides[a] and keeps[a] for a in hides):
        return {"branch": "continue", "why": why}
    if any(hides.values()):
        return {"branch": "other: hides y only by losing uplift", "plants": plants, "why": why}
    if plants:
        return {"branch": "pivot to decoys", "why": why}
    return {"branch": "kill", "why": why}


# --------------------------------------------------------------------- run + print


def run(results: Path = RESULTS, items: list[dict] | None = None) -> dict:
    items = load_items() if items is None else items
    stats, diag = collect(results, items)
    rows = read_jsonl(results / "answers.jsonl")
    upl = uplift(rows, items)
    out: dict[str, Any] = {"meta": {"seed": SEED, "n_boot": N_BOOT, "primary_reader": PRIMARY,
                                    "readers": list(READERS), "arms": list(ARMS),
                                    "n_items": len(items), "n_answer_rows": len(rows),
                                    "rank_k": RANK_K, **diag},
                           "uplift": upl, "hygiene": hygiene(rows), "presence": {},
                           "first_hit_y": {}, "continuous": {}, "predictions": {},
                           "outcome": {}, "agreement_y": agreement(stats)}
    for r in READERS:
        pres = {role: presence(stats[r], role, (BASE_ARM, "M") if role == "d" else (BASE_ARM,))
                for role in PRESENCE_ROLES}
        out["presence"][r] = pres
        out["first_hit_y"][r] = first_hit(stats[r])
        out["continuous"][r] = {role: continuous(stats[r], role) for role in CONT_ROLES}
        out["predictions"][r] = predictions(pres, upl, stats[r])
        out["outcome"][r] = outcome(pres, upl)
    return out


def _ci(d: dict | None, k: str = "mean", lo: str = "lo", hi: str = "hi", nd: int = 3) -> str:
    if not d or d.get(k) is None:
        return "n/a"
    s = f"{d[k]:.{nd}f}"
    if d.get(lo) is not None:
        s += f" [{d[lo]:.{nd}f}, {d[hi]:.{nd}f}]"
    return s


def report(res: dict) -> list[str]:
    L: list[str] = []
    m = res["meta"]
    L += [(f"P12 analysis: {m['n_items']} items, records per arm {m['n_records']}, "
           f"filler rows {m['n_filler_rows']}, answer rows {m['n_answer_rows']}; "
           f"cand-value mismatches {len(m['cand_value_mismatch'])}, "
           f"errors {len(m['errors'])}"), ""]
    L += ["## Uplift (filler 100 - none, paired by item)", "",
          "| arm | acc100 | acc0 | uplift | uplift - P |", "|---|---|---|---|---|"]
    for a in ARMS:
        u = res["uplift"][a]
        L.append(f"| {a} | {_ci(u['acc100'])} | {_ci(u['acc0'])} | {_ci(u['uplift'])} "
                 f"| {_ci(u.get('minus_P'))} |")
    p60 = next(v for k, v in res["uplift"].items() if k.startswith("P_first"))
    L += ["", f"P uplift, first {N_STOP} items in run order: {_ci(p60)} (stop: {p60['stop']})", ""]
    for r in READERS:
        L += [f"## Presence ({r}): filler-cell top-1 share, arm - P (rel), any-cell rate vs null",
              ""]
        for role in PRESENCE_ROLES:
            L += [f"**{role}**", "",
                  "| arm | n | share | null share | vs P diff | vs P rel | any | null any |",
                  "|---|---|---|---|---|---|---|---|"]
            for a in ARMS:
                p = res["presence"][r][role][a]
                v = p.get("vs_P")
                L.append(f"| {a} | {p['n']} | {_ci(p['share'])} | {_ci(p['null_share'])} | "
                         f"{_ci(v, 'diff', 'diff_lo', 'diff_hi')} | "
                         f"{_ci(v, 'rel', 'rel_lo', 'rel_hi', 2)} | {_ci(p['any'], nd=2)} | "
                         f"{_ci(p['null_any'], nd=2)} |")
            if role == "d":
                L.append("")
                for a in ("D", "CD"):
                    v = res["presence"][r]["d"][a].get("vs_M")
                    L.append(f"d share {a} - M: {_ci(v, 'diff', 'diff_lo', 'diff_hi', 4)}, "
                             f"rel {_ci(v, 'rel', 'rel_lo', 'rel_hi', 2)} (n {v['n']})")
            L.append("")
        fh = res["first_hit_y"][r]
        L.append(f"First-hit layer of y ({r}): " + "; ".join(
            f"{a} hit {fh[a]['n_hit']}/{fh[a]['n_items']} med {fh[a]['median']}"
            + (f" vs P {_ci(fh[a]['vs_P'], 'median', nd=1)}" if "vs_P" in fh[a] else "")
            for a in ARMS))
        for role in CONT_ROLES:
            cont = res["continuous"][r][role]
            L.append(f"Continuous {role} ({r}): " + "; ".join(
                f"{a} lp {_ci(cont[a]['lp'], nd=2)} rank<{RANK_K} {_ci(cont[a]['rank5'])}"
                + (f" (lp - P {_ci(cont[a]['lp_vs_P'], 'diff', 'diff_lo', 'diff_hi', 2)})"
                   if "lp_vs_P" in cont[a] else "") for a in ARMS))
        L.append("")
    L += ["## Reader agreement (y)", ""]
    for a, row in res["agreement_y"].items():
        pairs = "; ".join(f"{k} r {v['pearson'] if v['pearson'] is None else round(v['pearson'], 2)}"
                          f" rho {v['spearman'] if v['spearman'] is None else round(v['spearman'], 2)}"
                          for k, v in row.items() if "~" in k)
        L.append(f"{a} (n {row['n']}): {pairs}; all agree on any-cell {row['all_agree_any']}")
    L += ["", "## Predictions (verdict reader: logit_lens; others alongside)", "",
          "| pred | claim | " + " | ".join(READERS) + " |", "|---|---|" + "---|" * len(READERS)]
    for key in res["predictions"][PRIMARY]:
        cells = []
        for r in READERS:
            p = res["predictions"][r][key]
            nums = {k: v for k, v in p.items() if k not in ("claim", "holds", "p_prior")}
            cells.append(f"**{p['holds']}** " + json.dumps(nums, default=_short)
                         .replace("|", "/"))
        L.append(f"| {key} | {res['predictions'][PRIMARY][key]['claim']} | "
                 + " | ".join(cells) + " |")
    L += ["", (f"**Outcome branch (logit_lens): {res['outcome'][PRIMARY]['branch']}** "
               f"({res['outcome'][PRIMARY]['why']})")]
    for r in READERS[1:]:
        L.append(f"Outcome branch ({r}): {res['outcome'][r]['branch']}")
    L += ["", "## Output hygiene (replies with letters)", ""]
    for k, h in res["hygiene"].items():
        ex = "; ".join(repr(e["gen_text"][:60]) for e in h["examples"])
        L.append(f"{k}: {h['n_letters']}/{h['n']}" + (f" e.g. {ex}" if ex else ""))
    return L


def _short(x: Any) -> Any:
    return round(x, 4) if isinstance(x, float) else str(x)


def _round(o: Any) -> Any:
    if isinstance(o, float):
        return round(o, 4)
    if isinstance(o, dict):
        return {k: _round(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v) for v in o]
    return o


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p12.analyze")
    ap.add_argument("--results", type=Path, default=RESULTS)
    a = ap.parse_args(argv)
    res = run(a.results)
    (a.results / "analysis.json").write_text(json.dumps(res, indent=1) + "\n")
    print("\n".join(report(_round(res))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
