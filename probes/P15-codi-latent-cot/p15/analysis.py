"""P15 laptop analysis (plan.md "Setup" readers, "Measure", Predictions P1-P10, outcome rules O1-O5).

Reads `<results>/A` or `<results>/B` in the CONTRACTS.md layout and writes one JSON (plus a per-item
predictions CSV next to it, and an optional markdown summary). No paid calls; CPU only.

    cd probes/P15-codi-latent-cot
    uv run python -m p15.analysis --results results --stage A --out results/analysis_A.json \
        [--md results/analysis_A.md] [--layers auto|all|even|8,16] [--fast]

Readers of the latent answer a_L (Stage A on T, Stage B on C): V (verbal answer), D (direct
answer), LL (Stage A: top-ranked number token among the layer-16 top-5 at column 3 -> s1_hat and
column 5 -> s2_hat, prediction s1_hat + s2_hat), PS (Stage A: ps_lat3 + ps_lat5), LP (multinomial
L2 logistic regression on the 7 latent columns at one layer), LPq (the same on the direct-mode
question residual, slot 0; slot 1 as a sensitivity), VT (the same classifier on one-hot text
features), K and K0 (Stage A lookup; on C only K0, since K == K0 there by construction).
Untrained readers that return no number (parse failure) count as misses; trained readers predict
a class label, which may be "none" (a_L had no number).

Cross-fit: 2 outer folds grouped by template (A: templates 0-4 + 10-14 vs 5-9 + 15-19; B:
scenarios 0-3 vs 4-7). Layer and L2 strength are chosen per outer fold by 2-fold inner CV on the
training fold only, grouped by template (A: balanced by op). The classifier is a full-batch
L-BFGS softmax regression in torch on CPU (mean cross-entropy + lam * ||W||^2, zero init,
warm-started from strong to weak lam); a test item whose a_L class is unseen in training is a miss.

Bootstraps are cluster bootstraps of ratio-of-sums statistics (`cluster_ratio_ci`, vectorised; it
agrees with `emx.stats.cluster_bootstrap_ci`, see tests), 10,000 resamples, seed 20261012, paired by
item: every statistic of one call shares the same resampled clusters.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from p15.tasks import cluster_id, cot_new_numbers, cot_results, read_jsonl

SEED = 20261012
N_BOOT = 10_000
LAMS = (1e-1, 1e-2, 1e-3, 1e-4)  # strong to weak: warm starts run in this order
FAST_LAMS = (1e-1, 1e-3)
FAST_N_BOOT = 500
N_LAYERS = 17
LL_LAYER = 16  # the final layer on the real model; lens reads index -1 (tiny dry-run model has 3)
SWEEP_BUDGET_S = 3600.0
MAX_ITER = 500
READABLE = 0.30  # "readable": the best reader matches a_L on >= 30% of E
MIN_ERR_CLUSTERS = 40
MARGIN = 0.10  # the 10-point margin of O1-O4
NONE = "none"


# --- labels --------------------------------------------------------------------------------------


def lab(x) -> str | None:
    """Canonical answer label: None -> None; integral numbers -> '12'; other floats -> '%g'."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    v = float(x)
    if v.is_integer():
        return str(int(v))
    return f"{v:.6g}"


def lab_aL(x) -> str:
    """Label of the latent answer: no number is its own class."""
    return lab(x) or NONE


# --- loading ---------------------------------------------------------------------------------------


def load_set(stage_dir: Path, name: str) -> pd.DataFrame | None:
    p = stage_dir / f"{name}.jsonl"
    return pd.DataFrame(read_jsonl(p)) if p.exists() else None


def load_arr(stage_dir: Path, name: str) -> np.ndarray | None:
    p = stage_dir / f"{name}.npy"
    return np.load(p, mmap_mode="r") if p.exists() else None


def cluster_ids(df: pd.DataFrame) -> np.ndarray:
    """Bootstrap clusters from `p15.tasks.cluster_id`: T (X, Y, Z), both operations together
    (224 clusters); C (op_pattern, X); G the item."""
    return np.asarray([cluster_id(r) for r in df.to_dict("records")], dtype=object)


def lookup_keys(df: pd.DataFrame) -> np.ndarray:
    """K's lookup key on T: (op, X, Y, Z)."""
    return np.asarray([f"{o}|{x}|{y}|{z}" for o, x, y, z in zip(df.op, df.X, df.Y, df.Z)],
                      dtype=object)


def outer_folds(df: pd.DataFrame, stage: str) -> np.ndarray:
    if stage == "A":
        t = df.template_id.to_numpy()
        return np.where((t % 10) < 5, 0, 1)
    return np.where(df.scenario.to_numpy() < 4, 0, 1)


def inner_split(groups: np.ndarray, strata: np.ndarray | None) -> np.ndarray:
    """Two inner folds grouped by `groups`. Within each stratum the sorted groups alternate between
    folds, and the starting fold alternates across strata, so 5 + 5 templates split 5 / 5."""
    uniq = sorted(set(groups.tolist()))
    st = {g: (strata[groups == g][0] if strata is not None else 0) for g in uniq}
    assign = {}
    for s_i, s in enumerate(sorted(set(st.values()))):
        for j, g in enumerate([g for g in uniq if st[g] == s]):
            assign[g] = (j + s_i) % 2
    return np.asarray([assign[g] for g in groups.tolist()])


# --- fast cluster bootstrap ------------------------------------------------------------------------


def _codes(clusters: Sequence) -> tuple[np.ndarray, int]:
    codes, uniq = pd.factorize(pd.Series(list(clusters), dtype=object), sort=True)
    return codes, len(uniq)


def boot_counts(n_clusters: int, n_boot: int, seed: int, chunk: int = 1000):
    """Yield (b, n_clusters) multiplicity matrices of cluster resamples, b <= chunk. The stream is
    the same for every call with the same (n_clusters, seed), so statistics are paired."""
    rng = np.random.default_rng(seed)
    for start in range(0, n_boot, chunk):
        b = min(chunk, n_boot - start)
        pick = rng.integers(0, n_clusters, size=(b, n_clusters))
        off = (np.arange(b)[:, None] * n_clusters + pick).ravel()
        yield np.bincount(off, minlength=b * n_clusters).reshape(b, n_clusters).astype(float)


def _ci(point: float, boot: np.ndarray, n_boot: int, n_clusters: int, seed: int) -> dict:
    valid = boot[~np.isnan(boot)]
    lo, hi = (np.percentile(valid, [2.5, 97.5]) if valid.size else (np.nan, np.nan))
    return {"point": float(point), "lo": float(lo), "hi": float(hi), "n_boot": n_boot,
            "n_boot_valid": int(valid.size), "n_clusters": n_clusters, "seed": seed}


def cluster_ratio_ci(num: np.ndarray, den: np.ndarray, clusters: Sequence,
                     n_boot: int = N_BOOT, seed: int = SEED) -> list[dict]:
    """Cluster bootstrap of sum(num[:, j]) / sum(den) for each column j of num (n,) or (n, k).
    Per-cluster sums, resampled cluster multiplicities, ratio of sums; percentile 95% CIs."""
    num = np.asarray(num, dtype=float)
    num = num[:, None] if num.ndim == 1 else num
    den = np.asarray(den, dtype=float)
    if len(den) == 0:
        return [_ci(np.nan, np.array([]), n_boot, 0, seed) for _ in range(num.shape[1])]
    codes, nc = _codes(clusters)
    cn = np.zeros((nc, num.shape[1]))
    np.add.at(cn, codes, num)
    cd = np.bincount(codes, weights=den, minlength=nc)
    boots = []
    for w in boot_counts(nc, n_boot, seed):
        d = w @ cd
        with np.errstate(invalid="ignore", divide="ignore"):
            boots.append((w @ cn) / d[:, None])
    bm = np.concatenate(boots)
    point = num.sum(0) / den.sum() if den.sum() else np.full(num.shape[1], np.nan)
    return [_ci(point[j], bm[:, j], n_boot, nc, seed) for j in range(num.shape[1])]


def _wauc(score: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted AUROC for each row of item weights w (b, n); ties count one half."""
    order = np.argsort(score, kind="stable")
    s, yy, wi = score[order], y[order].astype(float), w[:, order]
    starts = np.flatnonzero(np.r_[True, s[1:] != s[:-1]])
    pos = np.add.reduceat(wi * yy, starts, axis=1)
    neg = np.add.reduceat(wi * (1 - yy), starts, axis=1)
    below = np.cumsum(neg, axis=1) - neg
    with np.errstate(invalid="ignore", divide="ignore"):
        return (pos * (below + 0.5 * neg)).sum(1) / (pos.sum(1) * neg.sum(1))


def auroc(score: np.ndarray, y: np.ndarray) -> float:
    return float(_wauc(np.asarray(score, float), np.asarray(y), np.ones((1, len(y))))[0])


def cluster_auroc_ci(scores: dict[str, np.ndarray], y: np.ndarray, clusters: Sequence,
                     diffs: Sequence[tuple[str, str]] = (), n_boot: int = N_BOOT,
                     seed: int = SEED) -> dict:
    """AUROC of each score for binary y, and paired differences, cluster bootstrap."""
    codes, nc = _codes(clusters)
    y = np.asarray(y).astype(int)
    names = list(scores)
    bs: dict[str, list] = {k: [] for k in names}
    for w in boot_counts(nc, n_boot, seed):
        wi = w[:, codes]
        for k in names:
            bs[k].append(_wauc(np.asarray(scores[k], float), y, wi))
    out = {}
    for k in names:
        out[k] = _ci(auroc(scores[k], y), np.concatenate(bs[k]), n_boot, nc, seed)
    for a, b in diffs:
        out[f"{a}-{b}"] = _ci(out[a]["point"] - out[b]["point"],
                              np.concatenate(bs[a]) - np.concatenate(bs[b]), n_boot, nc, seed)
    return out


# --- classifier ------------------------------------------------------------------------------------


def fit_softmax(X, y: np.ndarray, k: int, lam: float, init=None, max_iter: int = MAX_ITER):
    """Full-batch L-BFGS softmax regression: mean CE + lam * ||W||^2 (bias unpenalised)."""
    import torch
    import torch.nn.functional as F

    Xt = X if isinstance(X, torch.Tensor) else torch.from_numpy(np.ascontiguousarray(X))
    yt = torch.from_numpy(np.asarray(y, dtype=np.int64))
    d = Xt.shape[1]
    W = (torch.zeros(d, k) if init is None else init[0].clone()).requires_grad_()
    b = (torch.zeros(k) if init is None else init[1].clone()).requires_grad_()
    opt = torch.optim.LBFGS([W, b], lr=1, max_iter=max_iter, history_size=20,
                            line_search_fn="strong_wolfe", tolerance_grad=1e-5,
                            tolerance_change=1e-9)

    def closure():
        opt.zero_grad()
        loss = F.cross_entropy(Xt @ W + b, yt) + lam * (W * W).sum()
        loss.backward()
        return loss

    opt.step(closure)
    return W.detach(), b.detach()


def predict_proba(X, model) -> np.ndarray:
    import torch

    Xt = X if isinstance(X, torch.Tensor) else torch.from_numpy(np.ascontiguousarray(X))
    with torch.no_grad():
        return torch.softmax(Xt @ model[0] + model[1], dim=1).numpy()


def _standardise(Xtr: np.ndarray, Xte: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu = Xtr.mean(0, dtype=np.float64).astype(np.float32)
    sd = Xtr.std(0, dtype=np.float64).astype(np.float32)
    sd[sd < 1e-6] = 1.0
    return (Xtr - mu) / sd, (Xte - mu) / sd


def fit_predict(Xtr, ytr, Xte, lam: float, task: str, standardise: bool, init=None):
    """Fit on (Xtr, ytr) and return (predictions for Xte, model, classes). Multi: label array
    (classes seen in training); binary: P(y = 1)."""
    if standardise:
        Xtr, Xte = _standardise(Xtr, Xte)
    if task == "binary":
        classes = np.array([0, 1])
        yi = np.asarray(ytr, dtype=int)
    else:
        classes = np.array(sorted(set(ytr.tolist())), dtype=object)
        idx = {c: i for i, c in enumerate(classes)}
        yi = np.array([idx[c] for c in ytr.tolist()])
    model = fit_softmax(Xtr, yi, len(classes), lam, init=init)
    p = predict_proba(Xte, model)
    pred = p[:, 1] if task == "binary" else classes[p.argmax(1)]
    return pred, model, classes


def _score(pred, y, task: str) -> float:
    if task == "binary":
        yy = np.asarray(y, int)
        return auroc(pred, yy) if 0 < yy.sum() < len(yy) else float("nan")
    return float(np.mean(pred == y))


def crossfit(get_x: Callable[[int | None], np.ndarray], layers: Sequence, y: np.ndarray,
             outer: np.ndarray, groups: np.ndarray, strata: np.ndarray | None,
             lams: Sequence[float], task: str = "multi", standardise: bool = True) -> dict:
    """2-fold outer cross-fit; per outer fold, (layer, lam) chosen by mean inner score (accuracy
    at predicting y, or AUROC for binary) over 2 inner folds grouped by `groups`. Returns
    out-of-fold predictions, chosen settings, inner scores and class coverage."""
    t0 = time.time()
    folds = sorted(set(outer.tolist()))
    inner = {f: inner_split(groups[outer != f], None if strata is None else strata[outer != f])
             for f in folds}
    scores: dict = {f: {} for f in folds}
    for layer in layers:
        X = get_x(layer)
        for f in folds:
            tr = np.flatnonzero(outer != f)
            for lam in lams:
                scores[f].setdefault((layer, lam), [])
            for i in (0, 1):
                a, v = tr[inner[f] != i], tr[inner[f] == i]
                Xa, Xv = (_standardise(X[a], X[v]) if standardise else (X[a], X[v]))
                init = None
                for lam in lams:
                    pred, init, _ = fit_predict(Xa, y[a], Xv, lam, task, False, init=init)
                    scores[f][(layer, lam)].append(_score(pred, y[v], task))
    pred_all = np.empty(len(y), dtype=float if task == "binary" else object)
    chosen, coverage = {}, {}
    for f in folds:
        tr, te = np.flatnonzero(outer != f), np.flatnonzero(outer == f)
        best = max(scores[f], key=lambda key: (np.nanmean(scores[f][key])
                                               if not np.all(np.isnan(scores[f][key]))
                                               else -np.inf))
        layer, lam = best
        X = get_x(layer)
        pred, _, classes = fit_predict(X[tr], y[tr], X[te], lam, task, standardise)
        pred_all[te] = pred
        chosen[str(f)] = {"layer": layer, "lam": lam, "inner_score": float(np.nanmean(
            scores[f][best]))}
        if task != "binary":
            seen = set(classes.tolist())
            coverage[str(f)] = float(np.mean([c in seen for c in y[te].tolist()]))
    return {"pred": pred_all, "chosen": chosen, "coverage": coverage,
            "inner_scores": {str(f): {f"{k[0]}|{k[1]:g}": float(np.nanmean(v))
                                      for k, v in scores[f].items()} for f in folds},
            "seconds": round(time.time() - t0, 1)}


# --- feature getters -------------------------------------------------------------------------------


def latent_getter(resid: np.ndarray) -> Callable[[int], np.ndarray]:
    def get(layer: int) -> np.ndarray:
        x = np.asarray(resid[:, layer], dtype=np.float32)
        return x.reshape(len(x), -1)
    return get


def q_getter(qresid: np.ndarray, slot: int) -> Callable[[int], np.ndarray]:
    def get(layer: int) -> np.ndarray:
        return np.asarray(qresid[:, layer, slot], dtype=np.float32)
    return get


def onehot(cols: list[list[str]]) -> np.ndarray:
    """One-hot of categorical columns; the vocabulary is label-free (all items), so a category
    unseen in a training fold gets a zero weight under L2 and leaks nothing."""
    blocks = []
    for c in cols:
        codes, uniq = pd.factorize(pd.Series(c, dtype=object), sort=True)
        m = np.zeros((len(c), len(uniq)), dtype=np.float32)
        m[np.arange(len(c)), codes] = 1.0
        blocks.append(m)
    return np.concatenate(blocks, axis=1)


def vt_columns(df: pd.DataFrame, stage: str) -> list[list[str]]:
    v = [lab(x) or NONE for x in df.ans_verbal]
    d = [lab(x) or NONE for x in df.ans_direct]
    cr = [cot_results(t or "") for t in df.out_verbal]
    if stage == "A":
        n_pos = [2] * len(df)
        extra = [list(df.op.astype(str))]
    else:
        n_pos = [int(k) - 1 for k in df.k]
        extra = [list(df.op_pattern.astype(str)), [str(k) for k in df.k]]
    width = max(n_pos) if n_pos else 0
    pos = [[(lab(r[j]) or NONE) if j < min(n, len(r)) else NONE for r, n in zip(cr, n_pos)]
           for j in range(width)]
    return [v, d, *pos, *extra]


# --- untrained readers -----------------------------------------------------------------------------


def number_lut(number_tokens: dict, max_id: int) -> np.ndarray:
    lut = np.full(max_id + 1, -1, dtype=np.int64)
    for k, v in number_tokens.items():
        if int(k) <= max_id:
            lut[int(k)] = int(v)
    return lut


def first_number(top5: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Value of the top-ranked number token among the top-5 ids (n, 5); -1 if none."""
    vals = lut[np.asarray(top5, dtype=np.int64)]
    has = vals >= 0
    first = np.argmax(has, axis=1)
    out = vals[np.arange(len(vals)), first]
    out[~has.any(1)] = -1
    return out


def ll_reader(lltop: np.ndarray, lut: np.ndarray) -> tuple[np.ndarray, np.ndarray, list]:
    s1 = first_number(lltop[:, -1, 3], lut)
    s2 = first_number(lltop[:, -1, 5], lut)
    pred = [str(a + b) if a >= 0 and b >= 0 else None for a, b in zip(s1, s2)]
    return s1, s2, pred


def lookup_reader(keys: np.ndarray, y: np.ndarray, outer: np.ndarray) -> tuple[list, list]:
    """K: majority a_L among training-fold items with the same key, else K0 (training-fold
    majority). Ties go to the smallest label (string order)."""
    def maj(vals) -> str:
        c = Counter(vals)
        top = max(c.values())
        return min(v for v, n in c.items() if n == top)

    k_pred, k0_pred = [None] * len(y), [None] * len(y)
    for f in sorted(set(outer.tolist())):
        tr, te = np.flatnonzero(outer != f), np.flatnonzero(outer == f)
        k0 = maj(y[tr].tolist())
        by: dict = {}
        for i in tr:
            by.setdefault(keys[i], []).append(y[i])
        table = {k: maj(v) for k, v in by.items()}
        for i in te:
            k_pred[i] = table.get(keys[i], k0)
            k0_pred[i] = k0
    return k_pred, k0_pred


# --- outcome rule ----------------------------------------------------------------------------------


def outcome(n_err_clusters: int, best_reader_share: float, lp_minus_b: dict,
            lp_minus_lpq: dict) -> dict:
    """The plan's exclusive ordered rules (P3), on fractions (10 points = 0.10)."""
    nums = {"n_err_clusters": n_err_clusters, "best_reader_share": best_reader_share,
            "lp_minus_b": {k: lp_minus_b[k] for k in ("point", "lo", "hi")},
            "lp_minus_lpq": {k: lp_minus_lpq[k] for k in ("point", "lo", "hi")}}
    d, q = lp_minus_b, lp_minus_lpq
    if n_err_clusters < MIN_ERR_CLUSTERS:
        return {"label": "O5", "why": f"fewer than {MIN_ERR_CLUSTERS} error-bearing clusters",
                "numbers": nums}
    if not best_reader_share >= READABLE:
        return {"label": "O5", "why": "no reader readable (best E-share < 0.30)", "numbers": nums}
    lp_beats_b = d["point"] >= MARGIN and d["lo"] > 0
    if lp_beats_b and q["point"] >= MARGIN and q["lo"] > 0:
        return {"label": "O1", "why": "LP-B >= 10, lo > 0; LP-LPq >= 10, lo > 0", "numbers": nums}
    if lp_beats_b and q["hi"] < MARGIN:
        return {"label": "O4", "why": "LP-B >= 10, lo > 0; LP-LPq hi < 10", "numbers": nums}
    if d["lo"] > -MARGIN and d["hi"] < MARGIN:
        return {"label": "O2", "why": "LP-B CI inside +-10", "numbers": nums}
    if -d["point"] >= MARGIN and d["hi"] < 0:
        return {"label": "O3", "why": "B-LP >= 10 and LP-B hi < 0", "numbers": nums}
    return {"label": "O5", "why": "no rule applies", "numbers": nums}


# --- pieces of the run -----------------------------------------------------------------------------


def _correct(ans, gold) -> np.ndarray:
    out = []
    for a, g in zip(ans, gold):
        la, lg = lab(a), lab(g)
        out.append(la is not None and la == lg)
    return np.asarray(out, dtype=float)


def uplift(df: pd.DataFrame, clusters: Sequence, n_boot: int) -> dict:
    cl, cd = _correct(df.ans_latent, df.gold), _correct(df.ans_direct, df.gold)
    cv = _correct(df.ans_verbal, df.gold)
    r = cluster_ratio_ci(np.column_stack([cl - cd, cl, cd, cv]), np.ones(len(df)), clusters,
                         n_boot)
    out = {"latent_minus_direct": r[0], "latent_acc": r[1], "direct_acc": r[2],
           "verbal_acc": r[3], "n": len(df)}
    if "ans_latent_up" in df and df.ans_latent_up.notna().any():
        # replication reference: upstream generate on padded batches (pod_main.add_upstream_padded)
        cu = _correct(df.ans_latent_up, df.gold)
        same = np.asarray([lab(a) == lab(b) for a, b in zip(df.ans_latent, df.ans_latent_up)],
                          dtype=float)
        u = cluster_ratio_ci(np.column_stack([cu, cu - cd, cl - cu, same]), np.ones(len(df)),
                             clusters, n_boot)
        out.update({"latent_up_acc": u[0], "latent_up_minus_direct": u[1],
                    "latent_minus_latent_up": u[2], "latent_answer_agreement": u[3]})
    return out


def time_projection(get_x, layers, y, outer, groups, strata, lams) -> dict:
    """Time one representative multinomial fit and one binary fit (outer fold 0, inner fold 0,
    layer 16 or the last, lam 1e-2) and project the stage's whole probe sweep."""
    layer = LL_LAYER if LL_LAYER in layers else layers[-1]
    X = get_x(layer)
    tr = np.flatnonzero(outer != 0)
    inner = inner_split(groups[tr], None if strata is None else strata[tr])
    a, v = tr[inner != 0], tr[inner == 0]
    Xa, Xv = _standardise(X[a], X[v])
    fit_predict(Xa[:20, :8], y[a][:20], Xv[:2, :8], 1e-2, "multi", False)  # warm-up (imports)
    t = time.time()
    fit_predict(Xa, y[a], Xv, 1e-2, "multi", False)
    t_multi = time.time() - t
    t = time.time()
    yb = (np.arange(len(a)) % 2).astype(int)
    fit_predict(Xa, yb, Xv, 1e-2, "binary", False)
    t_bin = time.time() - t
    per_layer_fits = len(lams) * 2 * 2
    lp = t_multi * per_layer_fits
    err = t_bin * per_layer_fits
    q = 2 * lp / 7  # two LPq slots on 1/7 of the features
    per_layer = lp + err + q
    return {"fit_seconds_multi": round(t_multi, 2), "fit_seconds_binary": round(t_bin, 2),
            "n_train": len(a), "d": int(X.shape[1]), "n_classes": len(set(y[a].tolist())),
            "per_layer_seconds": round(per_layer, 1),
            "projected_all_layers_s": round(per_layer * len(layers), 1),
            "projected_even_layers_s": round(per_layer * len(layers[::2]), 1)}


def resolve_layers(spec: str, projection: dict | None,
                   n_layers: int = N_LAYERS) -> tuple[list[int], str]:
    if spec == "all":
        return list(range(n_layers)), "all"
    if spec == "even":
        return list(range(0, n_layers, 2)), "even"
    if spec == "auto":
        if projection and projection["projected_all_layers_s"] > SWEEP_BUDGET_S:
            return list(range(0, n_layers, 2)), "even (auto: projected sweep > 1 h)"
        return list(range(n_layers)), "all (auto)"
    return [int(x) for x in spec.split(",")], f"list {spec}"


def readability_A(t: pd.DataFrame, lltop, lut, in_e: np.ndarray, clusters, n_boot) -> dict:
    s1_hat, s2_hat, _ = ll_reader(lltop, lut)
    h1 = (s1_hat == t.s1.to_numpy()).astype(float)
    h2 = (s2_hat == t.s2.to_numpy()).astype(float)
    all_ = cluster_ratio_ci(np.column_stack([h1, h2]), np.ones(len(t)), clusters, n_boot)
    e = np.flatnonzero(in_e)
    on_e = cluster_ratio_ci(np.column_stack([h1[e], h2[e]]), np.ones(len(e)), clusters[e],
                            n_boot)
    table = {}
    for j in range(lltop.shape[2]):
        v = first_number(lltop[:, -1, j], lut)
        table[str(j)] = {"s1": float(np.mean(v == t.s1.to_numpy())),
                         "s2": float(np.mean(v == t.s2.to_numpy()))}
    return {"definition": "top-ranked number token among the layer-16 top-5",
            "s1_col3_T": all_[0], "s2_col5_T": all_[1], "s1_col3_E": on_e[0],
            "s2_col5_E": on_e[1], "by_column_layer16": table}


def readability_B(c: pd.DataFrame, lltop, lut, outer: np.ndarray) -> dict:
    ncol = lltop.shape[2]
    firsts = np.stack([first_number(lltop[:, -1, j], lut) for j in range(ncol)], 1)
    out = {}
    kmax = int(c.k.max())
    for i in range(kmax):
        rows = np.flatnonzero(c.k.to_numpy() > i)
        gold = np.array([c.inter.iloc[r][i] for r in rows])
        hit = firsts[rows] == gold[:, None]
        res = {"n": len(rows), "any_column": float(hit.any(1).mean()),
               "by_column": [float(x) for x in hit.mean(0)]}
        cross = np.zeros(len(rows))
        best = {}
        for f in (0, 1):
            fit, sc = outer[rows] == f, outer[rows] != f
            j = int(np.argmax(hit[fit].mean(0))) if fit.any() else 0
            best[str(f)] = j
            cross[sc] = hit[sc, j]
        res["best_column_crossfit"] = {"chosen_on_fold": best, "hit_rate": float(cross.mean())}
        out[f"s{i + 1}"] = res
    return out


def patching(patch: pd.DataFrame, cl_of: dict, n_boot: int) -> dict:
    """Rate ans == target per (lat, cond) (target is the same counterfactual for all four
    conditions of an (item, lat) pair), and ans == gold per (lat, cond). P5 on lat 5."""
    out: dict = {}
    for (lat, cond), g in patch.groupby(["lat", "cond"]):
        hit_t = _correct(g.ans, g.target)
        hit_g = _correct(g.ans, g.gold) if "gold" in g else np.full(len(g), np.nan)
        r = cluster_ratio_ci(np.column_stack([hit_t, hit_g]), np.ones(len(g)),
                             [cl_of.get(i, i) for i in g.item_id], n_boot)
        out.setdefault(str(lat), {})[cond] = {"n": len(g), "ans_eq_target": r[0],
                                              "ans_eq_gold": r[1]}
    l5 = out.get("5", {})
    diff = l5.get("diff", {}).get("ans_eq_target", {}).get("point", float("nan"))
    rnd = l5.get("random", {}).get("ans_eq_target", {}).get("point", float("nan"))
    out["P5"] = {"diff_rate": diff, "random_rate": rnd,
                 "pass": bool(diff >= 0.15 and diff >= 3 * rnd)}
    return out


def g_secondary(g: pd.DataFrame, lltop, lut, seed: int = SEED) -> dict:
    """P6: share of the verbal CoT's new numbers (0..999, single number tokens) found among the
    number tokens of the logit-lens top-5 at any layer x column, against a derangement null."""
    vocab_vals = set(lut[lut >= 0].tolist())
    nums = []
    for q, o in zip(g.question, g.out_verbal):
        nums.append([v for v in cot_new_numbers(o or "", q) if 0 <= v <= 999 and v in vocab_vals])
    tops = []
    for i in range(len(g)):
        v = lut[np.asarray(lltop[i], dtype=np.int64).ravel()]
        tops.append(set(v[v >= 0].tolist()))
    n = len(g)
    rng = np.random.default_rng(seed)
    cyc = rng.permutation(n)
    perm = np.empty(n, dtype=int)
    perm[cyc] = np.roll(cyc, -1)  # a single cycle: a derangement for n >= 2
    tot = sum(len(x) for x in nums)
    hit = sum(len(set(x) & tops[i]) for i, x in enumerate(nums))
    null = sum(len(set(x) & tops[perm[i]]) for i, x in enumerate(nums))
    share = hit / tot if tot else float("nan")
    null_share = null / tot if tot else float("nan")
    ratio = share / null_share if null_share else float("inf")
    return {"n_items": n, "n_numbers": tot, "share": share, "null_share": null_share,
            "ratio": ratio, "P6_pass": bool(share >= 0.40 and ratio >= 2)}


# --- main run --------------------------------------------------------------------------------------


def run(results: Path, stage: str, layers_spec: str = "auto", fast: bool = False,
        n_boot: int | None = None, lams: Sequence[float] | None = None) -> tuple[dict, pd.DataFrame]:
    T0 = time.time()
    timings: dict = {}
    n_boot = n_boot or (FAST_N_BOOT if fast else N_BOOT)
    lams = tuple(lams or (FAST_LAMS if fast else LAMS))
    sdir = results / stage
    main = "T" if stage == "A" else "C"
    df = load_set(sdir, main)
    resid, qresid, lltop = (load_arr(sdir, f"{main}_resid"), load_arr(sdir, f"{main}_qresid"),
                            load_arr(sdir, f"{main}_lltop"))
    ntok_path = (results / "A" / "number_tokens.json")
    if not ntok_path.exists():
        ntok_path = sdir / "number_tokens.json"
    number_tokens = json.loads(ntok_path.read_text())
    max_id = max([int(k) for k in number_tokens] + [int(np.max(lltop)) if lltop is not None
                                                     else 0])
    lut = number_lut(number_tokens, max_id)

    gold = np.array([lab(x) for x in df.gold], dtype=object)
    y = np.array([lab_aL(x) for x in df.ans_latent], dtype=object)
    in_e = y != gold
    clusters = cluster_ids(df)
    outer = outer_folds(df, stage)
    groups = (df.template_id if stage == "A" else df.scenario).to_numpy()
    strata = df.op.to_numpy() if stage == "A" else None
    e_idx = np.flatnonzero(in_e)
    n_err_clusters = len(set(clusters[e_idx].tolist()))

    preds: dict[str, list] = {}
    preds["V"] = [lab(x) for x in df.ans_verbal]
    preds["D"] = [lab(x) for x in df.ans_direct]
    if stage == "A":
        _, _, preds["LL"] = ll_reader(lltop, lut)
        preds["PS"] = [str(int(a) + int(b)) if a is not None and b is not None
                       and not (isinstance(a, float) and math.isnan(a))
                       and not (isinstance(b, float) and math.isnan(b)) else None
                       for a, b in zip(df.ps_lat3, df.ps_lat5)]
        preds["K"], preds["K0"] = lookup_reader(lookup_keys(df), y, outer)
    else:
        _, preds["K0"] = lookup_reader(clusters, y, outer)

    # timing and layer choice
    t = time.time()
    get_lat = latent_getter(resid)
    n_layers = resid.shape[1]  # 17 on the real model (embeddings + 16 layers)
    proj = time_projection(get_lat, list(range(n_layers)), y, outer, groups, strata, lams)
    print(f"[p15.analysis] representative fit: {proj}", flush=True)
    layers, layers_mode = resolve_layers(layers_spec, proj, n_layers)
    timings["projection"] = round(time.time() - t, 1)

    probes: dict = {}
    t = time.time()
    probes["LP"] = crossfit(get_lat, layers, y, outer, groups, strata, lams)
    timings["LP"] = round(time.time() - t, 1)
    for slot, name in ((0, "LPq"), (1, "LPq_slot1")):
        t = time.time()
        probes[name] = crossfit(q_getter(qresid, slot), layers, y, outer, groups, strata, lams)
        timings[name] = round(time.time() - t, 1)
    t = time.time()
    xvt = onehot(vt_columns(df, stage))
    probes["VT"] = crossfit(lambda _layer: xvt, [None], y, outer, groups, strata, lams,
                            standardise=False)
    timings["VT"] = round(time.time() - t, 1)
    for name in ("LP", "LPq", "LPq_slot1", "VT"):
        preds[name] = list(probes[name]["pred"])

    # primary: E-shares and headline contrasts
    hits = {r: np.array([p is not None and p == a for p, a in zip(preds[r], y)], dtype=float)
            for r in preds}
    readers = list(preds)
    e_share = cluster_ratio_ci(np.column_stack([hits[r][e_idx] for r in readers]),
                               np.ones(len(e_idx)), clusters[e_idx], n_boot)
    all_share = {r: float(hits[r].mean()) for r in readers}
    reader_out = {r: {"E_share": e_share[i], "all_items_share": all_share[r]}
                  for i, r in enumerate(readers)}
    for r in ("LP", "LPq", "LPq_slot1", "VT"):
        reader_out[r]["chosen"] = probes[r]["chosen"]
        reader_out[r]["coverage_by_fold"] = probes[r]["coverage"]
    text_side = ["V", "VT", "K"] if stage == "A" else ["V", "VT"]
    b_name = max(text_side, key=lambda r: (reader_out[r]["E_share"]["point"],
                                           -text_side.index(r)))
    best_reader = max(readers, key=lambda r: reader_out[r]["E_share"]["point"])
    contrasts = {}

    def contrast_block(cl: np.ndarray) -> dict:
        cols = [hits["LP"] - hits[b_name], hits["LP"] - hits["LPq"],
                hits["LP"] - hits["LPq_slot1"], hits["LP"] - hits["V"],
                hits["LP"] - hits["VT"]]
        names = [f"LP-{b_name}", "LP-LPq", "LP-LPq_slot1", "LP-V", "LP-VT"]
        if stage == "A":
            cols.append(hits["LP"] - hits["K"])
            names.append("LP-K")
        r = cluster_ratio_ci(np.column_stack([c[e_idx] for c in cols]), np.ones(len(e_idx)),
                             cl[e_idx], n_boot)
        return dict(zip(names, r))

    contrasts["primary_clusters"] = contrast_block(clusters)
    lp_b = contrasts["primary_clusters"][f"LP-{b_name}"]
    lp_q = contrasts["primary_clusters"]["LP-LPq"]
    oc = outcome(n_err_clusters, reader_out[best_reader]["E_share"]["point"], lp_b, lp_q)
    oc["B"] = b_name
    oc["best_reader"] = best_reader

    # error detection
    t = time.time()
    yerr = in_e.astype(int)
    err_lp = crossfit(get_lat, layers, yerr, outer, groups, strata, lams, task="binary")
    err_vt = crossfit(lambda _layer: xvt, [None], yerr, outer, groups, strata, lams,
                      task="binary", standardise=False)
    flag_v = np.array([p != a for p, a in zip(preds["V"], y)], dtype=float)
    err = cluster_auroc_ci({"LP_err": err_lp["pred"], "VT_err": err_vt["pred"],
                            "flag_V_ne_aL": flag_v}, yerr, clusters,
                           diffs=[("LP_err", "flag_V_ne_aL"), ("LP_err", "VT_err")],
                           n_boot=n_boot)
    err["chosen"] = {"LP_err": err_lp["chosen"], "VT_err": err_vt["chosen"]}
    timings["error_detection"] = round(time.time() - t, 1)

    out: dict = {
        "stage": stage, "set": main, "results": str(results),
        "settings": {"seed": SEED, "n_boot": n_boot, "lams": list(lams), "layers": layers,
                     "layers_mode": layers_mode, "fast": fast, "max_iter": MAX_ITER,
                     "classifier": "torch L-BFGS softmax regression, CPU, float32",
                     "cluster_ids": "T (X,Y,Z)" if stage == "A" else "C (op_pattern,X)",
                     "lookup_key_K": "(op,X,Y,Z)" if stage == "A" else None},
        "sweep_projection": proj,
        "counts": {"n_items": len(df), "n_E": len(e_idx), "n_clusters": len(set(clusters)),
                   "n_err_clusters": n_err_clusters,
                   "latent_acc": float(1 - in_e.mean()),
                   "n_aL_none": int((y == NONE).sum())},
        "readers": reader_out,
        "headline": {"B": b_name, "LP_minus_B": lp_b, "LP_minus_LPq": lp_q,
                     "contrasts": contrasts},
        "outcome": oc,
        "error_detection": err,
        "probe_seconds": {k: v["seconds"] for k, v in probes.items()},
    }

    # uplift and per-stage extras
    t = time.time()
    upl = {main: uplift(df, clusters, n_boot)}
    g = load_set(sdir, "G")
    if g is not None:
        upl["G"] = uplift(g, list(g.item_id), n_boot)
    if stage == "B":
        upl["C_by_k"] = {str(k): uplift(df[df.k == k], clusters[(df.k == k).to_numpy()], n_boot)
                         for k in sorted(df.k.unique())}
        kk = df.k.to_numpy()
        cl = _correct(df.ans_latent, df.gold)
        if 3 in kk and 5 in kk:
            num = np.column_stack([np.where(kk == 3, cl, 0), np.where(kk == 5, cl, 0)])
            # acc3 - acc5 as a difference of two ratios: bootstrap each with the same resamples
            r3 = cluster_ratio_ci(num[:, :1], (kk == 3).astype(float), clusters, n_boot)[0]
            r5 = cluster_ratio_ci(num[:, 1:], (kk == 5).astype(float), clusters, n_boot)[0]
            upl["k3_minus_k5_latent_acc"] = {"point": r3["point"] - r5["point"],
                                            "acc_k3": r3, "acc_k5": r5}
    out["uplift"] = upl
    timings["uplift"] = round(time.time() - t, 1)

    if stage == "A":
        out["readability"] = readability_A(df, lltop, lut, in_e, clusters, n_boot)
        t = time.time()
        out["sensitivity_2x2"] = sensitivity_2x2(df, get_lat, xvt, y, in_e, outer, probes,
                                                 n_boot)
        timings["sensitivity_2x2"] = round(time.time() - t, 1)
        pp = sdir / "patch.jsonl"
        if pp.exists():
            out["patching"] = patching(pd.DataFrame(read_jsonl(pp)),
                                       dict(zip(df.item_id, clusters)), n_boot)
        g_ll = load_arr(sdir, "G_lltop")
        if g is not None and g_ll is not None:
            out["g_secondary"] = g_secondary(g, g_ll, lut)
    else:
        out["readability"] = readability_B(df, lltop, lut, outer)
        acc = out["counts"]["latent_acc"]
        out["stage_b_error_check"] = {
            "latent_acc": acc, "n_err_clusters": n_err_clusters,
            "pass": bool(0.15 <= acc <= 0.90 and n_err_clusters >= MIN_ERR_CLUSTERS)}
        ga = load_set(results / "A", "G")
        if g is not None and ga is not None:
            m = ga[["item_id", "ans_latent", "gold"]].merge(
                g[["item_id", "ans_latent"]], on="item_id", suffixes=("_A", "_B"))
            ca, cb = _correct(m.ans_latent_A, m.gold), _correct(m.ans_latent_B, m.gold)
            r = cluster_ratio_ci(ca - cb, np.ones(len(m)), list(m.item_id), n_boot)[0]
            out["drift_G"] = {"A_minus_B_latent_acc": r, "n": len(m),
                              "acc_A": float(ca.mean()), "acc_B": float(cb.mean())}
    out["predictions"] = prediction_checks(out, stage)
    timings["total"] = round(time.time() - T0, 1)
    out["timings_s"] = timings

    items = pd.DataFrame({"item_id": df.item_id, "set": main, "gold": gold, "a_L": y,
                          "in_E": in_e, "cluster": clusters, "outer_fold": outer,
                          "err_score_LP": err_lp["pred"], "err_score_VT": err_vt["pred"]})
    for r in readers:
        items[f"pred_{r}"] = preds[r]
    if "sensitivity_2x2" in out:
        for r in ("LP", "VT"):
            items[f"pred_{r}_2x2"] = out["sensitivity_2x2"].pop(f"_pred_{r}")
    return out, items


def sensitivity_2x2(df, get_lat, xvt, y, in_e, outer, probes, n_boot) -> dict:
    """Hold out both templates and half the (X, Y, Z) clusters: each item is predicted by a model
    trained on the opposite template half AND the opposite cluster half. LP and VT only, with the
    (layer, lam) chosen for the main outer fold that trained on that template half."""
    xyz = cluster_ids(df)
    uniq = sorted(set(xyz.tolist()))
    rng = np.random.default_rng(SEED)
    half = {c: int(h) for c, h in zip(np.array(uniq, dtype=object)[rng.permutation(len(uniq))],
                                      np.arange(len(uniq)) % 2)}
    ch = np.array([half[c] for c in xyz])
    res: dict = {"cluster_half_sizes": [int((np.array(list(half.values())) == h).sum())
                                        for h in (0, 1)]}
    for name, getter, std in (("LP", get_lat, True), ("VT", lambda _layer: xvt, False)):
        pred = np.empty(len(y), dtype=object)
        cov = []
        for f in (0, 1):
            cfg = probes[name]["chosen"][str(f)]
            X = getter(cfg["layer"])
            for h in (0, 1):
                te = np.flatnonzero((outer == f) & (ch == h))
                tr = np.flatnonzero((outer != f) & (ch != h))
                p, _, classes = fit_predict(X[tr], y[tr], X[te], cfg["lam"], "multi", std)
                pred[te] = p
                seen = set(classes.tolist())
                cov.append((te, np.array([c in seen for c in y[te].tolist()])))
        covered = np.zeros(len(y), dtype=bool)
        for te, c in cov:
            covered[te] = c
        res[f"_pred_{name}"] = list(pred)
        res[name] = {"coverage_all": float(covered.mean()),
                     "coverage_E": float(covered[in_e].mean()) if in_e.any() else float("nan")}
    e = np.flatnonzero(in_e)
    hit = {n: np.array([p == a for p, a in zip(res[f"_pred_{n}"], y)], dtype=float)
           for n in ("LP", "VT")}
    r = cluster_ratio_ci(np.column_stack([hit["LP"][e], hit["VT"][e], (hit["LP"] - hit["VT"])[e]]),
                         np.ones(len(e)), xyz[e], n_boot)
    res["LP"]["E_share"], res["VT"]["E_share"], res["LP_minus_VT"] = r
    res["bootstrap_clusters"] = "(X,Y,Z)"
    return res


def prediction_checks(out: dict, stage: str) -> dict:
    p: dict = {}
    up = out["uplift"]
    if stage == "A":
        g = up.get("G", {}).get("latent_minus_direct")
        t = up["T"]["latent_minus_direct"]
        p["P1"] = {"G_uplift": g, "T_uplift": t,
                   "G_uplift_upstream_padded": up.get("G", {}).get("latent_up_minus_direct"),
                   "T_uplift_upstream_padded": up["T"].get("latent_up_minus_direct"),
                   "pass_G": bool(g and g["point"] >= 0.03 and g["lo"] > 0),
                   "pass_T": bool(t["point"] >= 0.10)}
        rd = out["readability"]
        p["P2"] = {"s1_T": rd["s1_col3_T"]["point"], "s2_T": rd["s2_col5_T"]["point"],
                   "s1_E": rd["s1_col3_E"]["point"], "s2_E": rd["s2_col5_E"]["point"]}
        p["P2"]["pass"] = bool(min(p["P2"]["s1_T"], p["P2"]["s2_T"]) >= 0.70
                               and min(p["P2"]["s1_E"], p["P2"]["s2_E"]) >= 0.60)
        ll = out["readers"]["LL"]["E_share"]["point"]
        p["P4"] = {"LL_E_share": ll, "pass": bool(ll < 0.25)}
        if "patching" in out:
            p["P5"] = out["patching"]["P5"]
        if "g_secondary" in out:
            gs = out["g_secondary"]
            p["P6"] = {"share": gs["share"], "ratio": gs["ratio"], "pass": gs["P6_pass"]}
    p["P3"] = {"outcome": out["outcome"]["label"], "why": out["outcome"]["why"]}
    if stage == "B":
        c = up["C"]["latent_minus_direct"]
        p["P7"] = {"C_uplift": c, "pass": bool(c["point"] >= 0.10 and c["lo"] > 0)}
        k = up.get("k3_minus_k5_latent_acc")
        if k:
            p["P8"] = {"k3_minus_k5": k["point"], "pass": bool(k["point"] >= 0.15)}
        va = up["C"]["verbal_acc"]["point"]
        p["P9"] = {"verbal_acc_C": va, "pass": bool(va >= 0.90)}
        if "drift_G" in out:
            d = out["drift_G"]["A_minus_B_latent_acc"]["point"]
            p["P10"] = {"A_minus_B_G_latent_acc": d, "pass": bool(d > 0.05)}
    return p


# --- output ----------------------------------------------------------------------------------------


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return _jsonable(o.tolist())
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else round(f, 6)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def markdown(out: dict) -> str:
    def ci(d):
        return f"{d['point']:.3f} [{d['lo']:.3f}, {d['hi']:.3f}]"

    c = out["counts"]
    lines = [f"# P15 analysis, stage {out['stage']} (set {out['set']})", "",
             (f"Items {c['n_items']}, latent accuracy {c['latent_acc']:.3f}, |E| {c['n_E']}, "
              f"error-bearing clusters {c['n_err_clusters']} of {c['n_clusters']}."), "",
             "| Reader | E-share [95% CI] | all items |", "|---|---|---|"]
    for r, v in out["readers"].items():
        lines.append(f"| {r} | {ci(v['E_share'])} | {v['all_items_share']:.3f} |")
    h = out["headline"]
    lines += ["", f"B (best text-side reader on E) = {h['B']}.",
              f"LP - B: {ci(h['LP_minus_B'])}; LP - LPq: {ci(h['LP_minus_LPq'])}.", "",
              f"**Outcome: {out['outcome']['label']}** ({out['outcome']['why']}).", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p15.analysis")
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--stage", choices=("A", "B"), required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--md", type=Path)
    ap.add_argument("--layers", default="auto", help="auto | all | even | comma list")
    ap.add_argument("--fast", action="store_true",
                    help=f"{FAST_N_BOOT} resamples and lams {FAST_LAMS} (dry runs only)")
    a = ap.parse_args(argv)
    out, items = run(a.results, a.stage, a.layers, a.fast)
    items_path = a.out.with_name(a.out.stem + "_items.csv")
    out["items_file"] = items_path.name
    a.out.parent.mkdir(parents=True, exist_ok=True)
    items.to_csv(items_path, index=False)
    a.out.write_text(json.dumps(_jsonable(out), indent=1) + "\n")
    md = markdown(_jsonable(out))
    if a.md:
        a.md.write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
