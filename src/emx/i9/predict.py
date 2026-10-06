"""I9 predictors: folds, logistic calibration, and every predictor's held-out rate predictions
(prereg v0.5 §4.2 "The predictors", §4.4 "Folds"; implementation plan §2 U5).

* **Folds.** Grouped 5-fold cross-validation over the confirmatory scenarios, repeated with the
  seeds ``FOLD_SEEDS``, grouped by ``template_group`` (``emx.i1a.ridge_probe.group_folds``). Every
  predictor uses the same fold assignment (``fold_assignments``). The leave-one-scenario-type-out
  split (``loto_assignments``) is one "repeat" whose folds are the scenario types.
* **Calibration.** Every predictor's raw score goes through a two-parameter logistic calibration
  ``p = sigmoid(a + b * f(raw))`` fitted by binomial maximum likelihood (counts out of n) on the
  training folds only, then clipped to ``PRED_CLIP``. ``f`` is ``logit`` for probability-like raw
  scores and the identity for logits and the 0-3 commitment strength.
  For predictors fitted on the training folds (embedding and account regressions, probes) the
  calibration is fitted on their *inner* out-of-fold scores, so it never sees in-sample fits;
  fixed scores (the zero-shot monitor, commitment) and the category prior calibrate on their
  training values, except the few-shot monitor: its predictions for test fold f are calibrated on
  fold (f - 1) mod 5 only, whose prompts' examples never include fold f (amendment B; the
  monitor draws a fold-g scenario's examples from folds other than g and g + 1).
* **Regressions and probes.** L2-penalised binomial logistic regression on standardised features,
  fitted to each scenario's count k out of n (the binomial likelihood, i.e. k positive and n - k
  negative replies). An inner grouped CV (``INNER_FOLDS`` folds of the training template groups)
  picks the layer (probes: the model's ladder) and the penalty from ``C_GRID`` by held-out
  per-reply log-likelihood; ties go to the first layer in ladder order, then the smallest C.
  Because the L2 solution lies in the training rows' span, each fit runs on the training rows'
  SVD coordinates (exactly equivalent, and cheap for n << d).
* **Embeddings.** ``EMBED_MODEL`` at ``EMBED_REV`` (MIT), CLS pooling as its card prescribes,
  long texts split into ``EMBED_MAX_TOKENS``-token windows whose normalised embeddings are
  averaged and renormalised. Scenario text is ``system + "\\n\\n" + text``.

numpy + scikit-learn at import; torch, transformers and safetensors are imported lazily.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from emx.i1a.ridge_probe import group_folds
from emx.i9 import contracts as C

# --- pins and grids -------------------------------------------------------------------------------

EMBED_MODEL = C.EMBED_MODEL
EMBED_REV = C.EMBED_REV
EMBED_MAX_TOKENS = 512
C_GRID: tuple[float, ...] = C.C_GRID
INNER_FOLDS = C.INNER_FOLDS
CALIB_L2 = 1e-3  # on the standardised calibration feature; only keeps separable fits finite
EPS = 1e-6

# --- predictor names (Prediction.predictor) -------------------------------------------------------

CATEGORY_PRIOR = "category_prior"
MONITOR_ZERO = "monitor_zero_shot"  # U4b writes these two files (raw = monitor probability)
MONITOR_FEW = "monitor_few_shot"
EMBED = "embed_regression"
# The few-shot monitor (prereg amendment B, round 2): the examples in the prompt for a scenario
# in fold g come only from folds other than g and (g + 1) mod N_FOLDS (emx.i9.monitor), so its
# predictions for test fold f are calibrated on fold (f - 1) mod N_FOLDS alone, whose prompts
# never showed a fold-f rate.
CALIBRATE_PREVIOUS_FOLD = "previous_fold"
PROMPT_ONLY: tuple[str, ...] = (CATEGORY_PRIOR, MONITOR_ZERO, MONITOR_FEW, EMBED)
FREE_PROMPT_ONLY: tuple[str, ...] = (CATEGORY_PRIOR, EMBED)  # the power check's baseline source
READERS: tuple[str, ...] = ("logit_lens", "jlens", "olens")


def probe_name(model: str) -> str:
    return f"probe_{model}"


def commit_name(reader: str) -> str:
    return f"commit_{reader}"


def account_name(reader: str) -> str:
    return f"account_{reader}"


PRIMARIES: tuple[str, str] = (probe_name("qwen"), account_name("olens"))


def required_predictions(olens_gate_pass: bool) -> list[tuple[str, str]]:
    """(predictor, target) pairs a confirmatory result needs (review r1 finding 5): all four
    prompt-only baselines for both targets, the four crossed probes (the Qwen probe primary
    among them), and the Oracle Lens account regression when its validation gate passed."""
    need = [(b, t) for t in ("qwen", "gemma") for b in PROMPT_ONLY]
    need += [(probe_name(s), t) for s in ("qwen", "gemma") for t in ("qwen", "gemma")]
    if olens_gate_pass:
        need.append((account_name("olens"), "qwen"))
    return need
LOTO_SUFFIX = "__loto"  # leave-one-type-out predictions: predictions_path(name + LOTO_SUFFIX)
BANK_SUFFIX = "__bank"  # bank predictions (fold_repeat = -1): predictions_path(name + BANK_SUFFIX)


# --- folds ---------------------------------------------------------------------------------------


def fold_assignments(
    groups: Sequence[Any], *, seeds: Sequence[int] = C.FOLD_SEEDS, n_folds: int = C.N_FOLDS
) -> np.ndarray:
    """[n_repeats, n] fold index of every scenario; a template group never spans two folds."""
    g = np.asarray(groups)
    out = np.full((len(seeds), len(g)), -1, dtype=int)
    for r, seed in enumerate(seeds):
        for f, test in enumerate(group_folds(g, n_folds, seed)):
            out[r, test] = f
    assert (out >= 0).all()
    return out


def loto_assignments(types: Sequence[str]) -> np.ndarray:
    """[1, n]: fold f holds the scenarios of the f-th scenario type (sorted)."""
    t = np.asarray(types)
    order = {v: i for i, v in enumerate(sorted(set(t.tolist())))}
    return np.array([[order[v] for v in t.tolist()]], dtype=int)




def folds_json(ids: Sequence[str], assign: np.ndarray) -> dict[str, dict[str, int]]:
    """The fold table U4b's few-shot monitor reads (``monitor.tasks_from_folds``):
    {str(fold_repeat): {scenario_id: fold}}."""
    return {
        str(r): {sid: int(assign[r, i]) for i, sid in enumerate(ids)} for r in range(len(assign))
    }


# --- binomial logistic regression ----------------------------------------------------------------


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))


def logit(p: np.ndarray, eps: float = 1e-4) -> np.ndarray:
    q = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    return np.log(q / (1 - q))


def fit_binomial_logistic(
    Z: np.ndarray, k: np.ndarray, n: np.ndarray, lam: float, *, max_iter: int = 100
) -> tuple[float, np.ndarray]:
    """(intercept, coef) minimising -sum[k*eta - n*log(1+e^eta)] + lam/2 ||coef||^2 by damped
    Newton steps (the intercept is unpenalised apart from a 1e-8 ridge for separable data)."""
    Z = np.asarray(Z, dtype=float)
    if Z.ndim == 1:
        Z = Z[:, None]
    k, n = np.asarray(k, dtype=float), np.asarray(n, dtype=float)
    X = np.hstack([np.ones((len(Z), 1)), Z])
    pen = np.full(X.shape[1], lam, dtype=float)
    pen[0] = 1e-8
    w = np.zeros(X.shape[1])
    w[0] = float(logit((k.sum() + 0.5) / (n.sum() + 1.0)))

    def obj(w: np.ndarray) -> float:
        eta = X @ w
        return float(-(k * eta - n * np.logaddexp(0, eta)).sum() + 0.5 * (pen * w * w).sum())

    f = obj(w)
    for _ in range(max_iter):
        p = sigmoid(X @ w)
        grad = -X.T @ (k - n * p) + pen * w
        H = (X * (n * p * (1 - p))[:, None]).T @ X + np.diag(pen)
        step = np.linalg.solve(H, grad)
        t = 1.0
        while True:
            w_new = w - t * step
            f_new = obj(w_new)
            if f_new <= f + 1e-12 or t < 1e-8:
                break
            t *= 0.5
        converged = abs(f - f_new) < 1e-10 * (1 + abs(f))
        w, f = w_new, f_new
        if converged:
            break
    return float(w[0]), w[1:]


def calibrate(
    raw_train: np.ndarray,
    k_train: np.ndarray,
    n_train: np.ndarray,
    raw_test: np.ndarray,
    *,
    transform: str = "logit",
    clip: tuple[float, float] = C.PRED_CLIP,
) -> np.ndarray:
    """Logistic calibration fitted on the training scenarios, applied to the test scores."""
    f = (lambda x: logit(x)) if transform == "logit" else (lambda x: np.asarray(x, dtype=float))
    a, b = f(raw_train), f(raw_test)
    mu, sd = float(a.mean()), float(a.std())
    sd = sd if sd > 1e-12 else 1.0
    b0, beta = fit_binomial_logistic((a - mu) / sd, k_train, n_train, CALIB_L2)
    return np.clip(sigmoid(b0 + beta[0] * (b - mu) / sd), *clip)


def mean_ll(p: np.ndarray, k: np.ndarray, n: np.ndarray) -> float:
    """Binomial log-likelihood per reply (no binomial coefficient)."""
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    k, n = np.asarray(k, dtype=float), np.asarray(n, dtype=float)
    return float((k * np.log(p) + (n - k) * np.log1p(-p)).sum() / n.sum())


# --- cross-fitting ------------------------------------------------------------------------------

# fit_fn(train_idx, test_idx, repeat, fold) -> (raw_test, raw_train_for_calibration, meta)
FitFn = Callable[[np.ndarray, np.ndarray, int, int], tuple[np.ndarray, np.ndarray, dict]]


@dataclass
class PredSet:
    """One predictor's held-out predictions for one target: raw and p are [n_repeats, n]."""

    name: str
    target_model: str
    ids: tuple[str, ...]
    raw: np.ndarray
    p: np.ndarray
    meta: dict[str, Any] = field(default_factory=dict)

    def mean_p(self) -> np.ndarray:
        """Predictions averaged over the fold repeats (prereg §4.4)."""
        return self.p.mean(axis=0)


def cross_fit(
    name: str,
    fit_fn: FitFn,
    assign: np.ndarray,
    k: np.ndarray,
    n: np.ndarray,
    *,
    ids: Sequence[str],
    target_model: str = "qwen",
    transform: str = "logit",
    calibrate_on: str = "train",
) -> PredSet:
    """Run ``fit_fn`` on every (repeat, fold) of ``assign`` and calibrate on the training folds
    (``calibrate_on="previous_fold"``: on fold (f - 1) mod n_folds of the same repeat only; the
    fit function then receives that fold as its training set)."""
    k, n = np.asarray(k), np.asarray(n)
    R, N = assign.shape
    raw, p = np.full((R, N), np.nan), np.full((R, N), np.nan)
    metas = []
    for r in range(R):
        folds = sorted(set(assign[r].tolist()))
        for f in folds:
            test = np.flatnonzero(assign[r] == f)
            if calibrate_on == CALIBRATE_PREVIOUS_FOLD:
                if folds != list(range(len(folds))):
                    raise ValueError(f"repeat {r}: folds {folds} are not 0..n-1")
                train = np.flatnonzero(assign[r] == (f - 1) % len(folds))
            elif calibrate_on == "train":
                train = np.flatnonzero(assign[r] != f)
            else:
                raise ValueError(f"calibrate_on={calibrate_on!r}")
            raw_test, raw_cal, meta = fit_fn(train, test, r, f)
            raw[r, test] = raw_test
            p[r, test] = calibrate(raw_cal, k[train], n[train], raw_test, transform=transform)
            if meta:
                metas.append({"repeat": r, "fold": f, **meta})
    assert not np.isnan(p).any()
    return PredSet(name, target_model, tuple(ids), raw, p, {"folds": metas} if metas else {})


def fixed_score_fn(score: np.ndarray) -> FitFn:
    """A score with no training (zero-shot monitor, commitment strength): [n] or [n_repeats, n]."""
    s = np.asarray(score, dtype=float)

    def fn(train: np.ndarray, test: np.ndarray, r: int, f: int):
        row = s[r] if s.ndim == 2 else s
        return row[test], row[train], {}

    return fn


def category_prior_fn(categories: Sequence[str], k: np.ndarray, n: np.ndarray) -> FitFn:
    """Mean rate per category on the training folds ((sum k + 0.5) / (sum n + 1)); a category
    absent from training gets the training mean."""
    cat = np.asarray(categories)
    k, n = np.asarray(k, dtype=float), np.asarray(n, dtype=float)

    def rates(train: np.ndarray, at: np.ndarray) -> np.ndarray:
        overall = (k[train].sum() + 0.5) / (n[train].sum() + 1.0)
        out = np.full(len(at), overall)
        for c in set(cat[at].tolist()):
            m = cat[train] == c
            if m.any():
                out[cat[at] == c] = (k[train][m].sum() + 0.5) / (n[train][m].sum() + 1.0)
        return out

    def fn(train: np.ndarray, test: np.ndarray, r: int, f: int):
        return rates(train, test), rates(train, train), {}

    return fn


# --- L2-logistic regression with inner CV (embedding/account regressions and probes) ------------


def _standardize(Xtr: np.ndarray, Xte: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu = Xtr.mean(axis=0)
    sd = Xtr.std(axis=0)
    sd[sd < 1e-12] = 1.0
    return (Xtr - mu) / sd, (Xte - mu) / sd


def _reduced(Xtr: np.ndarray, Xte: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Standardise on the training rows, then project both onto the training rows' right
    singular vectors: an L2 fit on these coordinates equals the fit on the full features."""
    A, B = _standardize(Xtr, Xte)
    _, s, Vt = np.linalg.svd(A, full_matrices=False)
    keep = s > 1e-10 * max(float(s.max(initial=0.0)), 1e-30)
    V = Vt[keep].T
    return A @ V, B @ V


def _fit_decision(
    Xtr: np.ndarray, ktr: np.ndarray, ntr: np.ndarray, Xte: np.ndarray, Cs: Sequence[float]
) -> list[np.ndarray]:
    Ztr, Zte = _reduced(Xtr, Xte)
    out = []
    for c in Cs:
        b0, w = fit_binomial_logistic(Ztr, ktr, ntr, 1.0 / c)
        out.append(b0 + Zte @ w)
    return out


def _inner_folds(groups: np.ndarray, seed: int) -> list[np.ndarray]:
    n_groups = len(np.unique(groups))
    return group_folds(groups, max(2, min(INNER_FOLDS, n_groups)), seed)


def logistic_fn(
    features: Mapping[int, np.ndarray],
    groups: Sequence[Any],
    k: np.ndarray,
    n: np.ndarray,
    *,
    Cs: Sequence[float] = C_GRID,
    seeds: Sequence[int] = C.FOLD_SEEDS,
) -> FitFn:
    """L2-logistic regression with inner-CV choice of feature set (layer) and C. ``features`` maps
    a layer (or 0 for an embedding) to [n, d]; keys are tried in their given order."""
    g = np.asarray(groups)
    k, n = np.asarray(k, dtype=float), np.asarray(n, dtype=float)
    layers = list(features)

    def fn(train: np.ndarray, test: np.ndarray, r: int, f: int):
        inner = _inner_folds(g[train], seeds[r % len(seeds)] * 1000 + 1 + f)
        oof = {(L, c): np.zeros(len(train)) for L in layers for c in Cs}
        for itest in inner:
            itrain = np.setdiff1d(np.arange(len(train)), itest)
            a, b = train[itrain], train[itest]
            for L in layers:
                X = features[L]
                for c, dec in zip(Cs, _fit_decision(X[a], k[a], n[a], X[b], Cs), strict=True):
                    oof[(L, c)][itest] = dec
        scores = {key: mean_ll(sigmoid(v), k[train], n[train]) for key, v in oof.items()}
        best = max(scores, key=lambda key: scores[key])  # dict order: ladder order, then C order
        L, c = best
        X = features[L]
        dec = _fit_decision(X[train], k[train], n[train], X[test], [c])[0]
        return dec, oof[best], {"layer": int(L), "C": float(c), "inner_ll": scores[best]}

    return fn


def fit_full_predict(
    features: Mapping[int, np.ndarray],
    groups: Sequence[Any],
    k: np.ndarray,
    n: np.ndarray,
    new_features: Mapping[int, np.ndarray],
    *,
    Cs: Sequence[float] = C_GRID,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Train on all rows (inner CV picks layer and C; calibration on the inner out-of-fold
    scores) and predict ``new_features``: (raw [m], p [m], meta). For the bank predictions."""
    N = len(np.asarray(k))
    m = len(next(iter(new_features.values())))
    stacked = {L: np.vstack([features[L], new_features[L]]) for L in features}
    gg = np.concatenate([np.asarray(groups), np.array([f"__new{i}" for i in range(m)])])
    kk = np.concatenate([np.asarray(k, dtype=float), np.zeros(m)])
    nn = np.concatenate([np.asarray(n, dtype=float), np.ones(m)])
    fn = logistic_fn(stacked, gg, kk, nn, Cs=Cs, seeds=(seed,))
    train, test = np.arange(N), np.arange(N, N + m)
    raw, raw_cal, meta = fn(train, test, 0, 0)
    p = calibrate(raw_cal, kk[train], nn[train], raw, transform="identity")
    return raw, p, meta


# --- I/O -----------------------------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def read_scenarios(path: Path | None = None) -> dict[str, C.Scenario]:
    path = C.SCENARIOS_PATH if path is None else path
    return {d["id"]: C.Scenario(**d) for d in read_jsonl(path)}


def read_sets(path: Path | None = None) -> dict[str, list[str]]:
    path = C.SETS_PATH if path is None else path
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_rates(path: Path | None = None) -> list[C.Rate]:
    path = C.RATES_PATH if path is None else path
    return [C.Rate(**d) for d in read_jsonl(path)]


def rate_arrays(
    rates: Sequence[C.Rate], ids: Sequence[str], model: str, thinking: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    """(k, n) aligned with ``ids``; every id must have exactly one rate for (model, thinking)."""
    by: dict[str, C.Rate] = {}
    for r in rates:
        if r.model == model and r.thinking == thinking:
            if r.scenario_id in by:
                raise ValueError(f"duplicate rate for {model}/{thinking}/{r.scenario_id}")
            by[r.scenario_id] = r
    missing = [i for i in ids if i not in by]
    if missing:
        raise ValueError(f"{len(missing)} scenarios lack a {model} rate, e.g. {missing[:3]}")
    return (np.array([by[i].k for i in ids], dtype=int), np.array([by[i].n for i in ids], dtype=int))


def to_predictions(ps: PredSet, *, thinking: bool = False, fold_repeat: int | None = None):
    rows = []
    for r in range(ps.p.shape[0]):
        for i, sid in enumerate(ps.ids):
            rows.append(
                C.Prediction(
                    predictor=ps.name,
                    target_model=ps.target_model,
                    target_thinking=thinking,
                    scenario_id=sid,
                    fold_repeat=r if fold_repeat is None else fold_repeat,
                    raw=float(ps.raw[r, i]),
                    p=float(ps.p[r, i]),
                )
            )
    return rows


def write_predictions(rows: Sequence[C.Prediction], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(C.to_json(r) + "\n" for r in rows), encoding="utf-8")
    return path


def read_predictions(path: Path) -> list[C.Prediction]:
    return [C.Prediction(**d) for d in read_jsonl(path)]


def prediction_matrix(
    rows: Sequence[C.Prediction],
    ids: Sequence[str],
    *,
    target_model: str,
    n_repeats: int = len(C.FOLD_SEEDS),
    thinking: bool = False,
    field_: str = "raw",
) -> np.ndarray:
    """[n_repeats, n] of ``raw`` (or ``p``) from Prediction rows. A file with fold_repeat = -1
    (or one row per scenario) is broadcast over the repeats; anything missing raises."""
    pos = {sid: i for i, sid in enumerate(ids)}
    out = np.full((n_repeats, len(ids)), np.nan)
    for row in rows:
        if row.target_model != target_model or row.target_thinking != thinking:
            continue
        if row.scenario_id not in pos:
            continue
        v = getattr(row, field_)
        reps = range(n_repeats) if row.fold_repeat < 0 else [row.fold_repeat]
        for r in reps:
            out[r, pos[row.scenario_id]] = v
    if np.isnan(out).any():
        miss = [ids[i] for i in np.flatnonzero(np.isnan(out).any(axis=0))]
        raise ValueError(f"{len(miss)} scenarios lack predictions, e.g. {miss[:3]}")
    return out


def load_last_acts(
    path: Path, index_path: Path, ids: Sequence[str], layers: Sequence[int]
) -> dict[int, np.ndarray]:
    """{layer: [len(ids), d] float32} from U3's last-token store. Accepts a safetensors file with
    key ``"h"`` ([n, n_layers, d]) or keys ``"layer_<L>"`` ([n, d]), and an index that is JSON
    ({"ids" or "scenario_ids": [...], "layers": [...]}) or JSONL rows with "scenario_id"/"id"."""
    from safetensors.numpy import load_file

    text = Path(index_path).read_text(encoding="utf-8")
    try:
        idx = json.loads(text)
    except json.JSONDecodeError:
        idx = [json.loads(x) for x in text.splitlines() if x.strip()]
    if isinstance(idx, dict):
        row_ids = list(idx.get("scenario_ids") or idx["ids"])
        stored_layers = [int(x) for x in idx.get("layers", [])]
    else:
        row_ids = [r.get("scenario_id", r.get("id")) for r in idx]
        stored_layers = []
    pos = {sid: i for i, sid in enumerate(row_ids)}
    missing = [i for i in ids if i not in pos]
    if missing:
        raise ValueError(f"{len(missing)} scenarios missing from {index_path}, e.g. {missing[:3]}")
    sel = [pos[i] for i in ids]
    t = load_file(str(path))
    out = {}
    for L in layers:
        if f"layer_{L}" in t:
            out[int(L)] = np.asarray(t[f"layer_{L}"], dtype=np.float32)[sel]
        else:
            h = t["h"]
            if h.shape[0] != len(row_ids):
                raise ValueError(f"{path}: {h.shape[0]} rows, index {len(row_ids)}")
            out[int(L)] = np.asarray(h[:, stored_layers.index(int(L)), :], dtype=np.float32)[sel]
    return out


def _stage_c_rows(path: Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        obj = json.loads(text)
        return obj["rows"] if isinstance(obj, dict) else obj
    except json.JSONDecodeError:
        return [json.loads(x) for x in text.splitlines() if x.strip()]


def read_stage_c(path: Path | Sequence[Path], ids: Sequence[str]) -> tuple[list[str], np.ndarray]:
    """(account texts, commitment strength 0-3) aligned with ``ids`` from a wsbench
    agentic_misalignment results JSON (``FamilyResult.to_json``: ``rows`` with ``id``,
    ``account``, ``commit_strength``) or a JSONL of such rows. ``path`` may be a list of such
    files (one A-C run per set, e.g. confirmatory + sealed after an unseal): their rows are
    concatenated. A duplicate id (within or across files) or a missing item raises ValueError."""
    paths = [Path(path)] if isinstance(path, (str, Path)) else [Path(p) for p in path]
    by: dict[str, dict] = {}
    for p in paths:
        for r in _stage_c_rows(p):
            if r["id"] in by:
                raise ValueError(f"duplicate Stage C id {r['id']!r} in {[str(x) for x in paths]}")
            by[r["id"]] = r
    bad = [i for i in ids if i not in by or by[i].get("commit_strength") is None]
    if bad:
        raise ValueError(f"{len(bad)} scenarios lack a Stage C record in "
                         f"{[str(x) for x in paths]}, e.g. {bad[:3]}")
    return (
        [str(by[i].get("account") or "") for i in ids],
        np.array([float(by[i]["commit_strength"]) for i in ids]),
    )


# --- embeddings ---------------------------------------------------------------------------------


def scenario_text(s: C.Scenario) -> str:
    return f"{s.system}\n\n{s.text}"


def _device() -> str:
    import torch

    return "mps" if torch.backends.mps.is_available() else "cpu"


def embed_texts(texts: Sequence[str], *, device: str | None = None, batch: int = 16) -> np.ndarray:
    """[len(texts), 384] float32, unit norm (CLS pooling per window, window mean, renormalised)."""
    import torch
    from transformers import AutoModel, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(EMBED_MODEL, revision=EMBED_REV)
    model = AutoModel.from_pretrained(EMBED_MODEL, revision=EMBED_REV).eval()
    dev = device or _device()
    model.to(dev)
    body = EMBED_MAX_TOKENS - 2
    windows: list[list[int]] = []
    owner: list[int] = []
    for i, t in enumerate(texts):
        ids = tok(t, add_special_tokens=False)["input_ids"] or [tok.unk_token_id]
        for s in range(0, len(ids), body):
            windows.append([tok.cls_token_id, *ids[s : s + body], tok.sep_token_id])
            owner.append(i)
    vecs = []
    with torch.no_grad():
        for s in range(0, len(windows), batch):
            chunk = windows[s : s + batch]
            L = max(len(w) for w in chunk)
            ids = torch.tensor([w + [tok.pad_token_id] * (L - len(w)) for w in chunk], device=dev)
            mask = torch.tensor([[1] * len(w) + [0] * (L - len(w)) for w in chunk], device=dev)
            cls = model(input_ids=ids, attention_mask=mask).last_hidden_state[:, 0]
            vecs.append(torch.nn.functional.normalize(cls.float(), dim=-1).cpu().numpy())
    V = np.concatenate(vecs) if vecs else np.zeros((0, model.config.hidden_size))
    out = np.zeros((len(texts), V.shape[1]), dtype=np.float32)
    np.add.at(out, np.array(owner), V)
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.where(norms > 0, norms, 1.0)


CACHE_MODEL_KEY = "__embed_model__"  # the cache's provenance entries (review r1 finding 11)
CACHE_REV_KEY = "__embed_rev__"


class EmbedCacheError(ValueError):
    """An embedding cache without, or with other, model/revision provenance."""


def embed_cached(texts: Sequence[str], cache: Path, **kw: Any) -> np.ndarray:
    """``embed_texts`` with an .npz cache keyed by sha256(model|rev|text). The cache records
    ``EMBED_MODEL``/``EMBED_REV``; a cache that lacks them or names others is refused (delete it
    to regenerate). Callers record the cache file (and its sha256) as a step input."""
    keys = [
        hashlib.sha256(f"{EMBED_MODEL}|{EMBED_REV}|{t}".encode()).hexdigest() for t in texts
    ]
    have: dict[str, np.ndarray] = {}
    if cache.exists():
        with np.load(cache, allow_pickle=False) as z:
            have = {k: z[k] for k in z.files}
        got = tuple(str(have.pop(k)) if k in have else None
                    for k in (CACHE_MODEL_KEY, CACHE_REV_KEY))
        if got != (EMBED_MODEL, EMBED_REV):
            raise EmbedCacheError(f"{cache}: made with (model, rev) {got}, not "
                                  f"{(EMBED_MODEL, EMBED_REV)}; delete it to regenerate")
    todo = sorted({i for i, k in enumerate(keys) if k not in have})
    if todo:
        new = embed_texts([texts[i] for i in todo], **kw)
        for i, v in zip(todo, new, strict=True):
            have[keys[i]] = v
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache, **have, **{CACHE_MODEL_KEY: np.array(EMBED_MODEL),
                                   CACHE_REV_KEY: np.array(EMBED_REV)})
    return np.stack([have[k] for k in keys])


# --- assembling the predictors --------------------------------------------------------------------


@dataclass
class Inputs:
    """Everything the confirmatory predictors need, aligned with ``ids``. Optional parts may be
    None (the predictor is then skipped). ``k``/``n`` map a target model to its counts."""

    ids: tuple[str, ...]
    groups: np.ndarray
    types: np.ndarray
    categories: np.ndarray
    k: dict[str, np.ndarray]
    n: dict[str, np.ndarray]
    embeddings: np.ndarray | None = None  # [n, d] prompt embeddings
    acts: dict[str, dict[int, np.ndarray]] = field(default_factory=dict)  # model -> layer -> X
    monitor_raw: dict[tuple[str, str], np.ndarray] = field(default_factory=dict)  # (name, tgt)
    commit: dict[str, np.ndarray] = field(default_factory=dict)  # reader -> [n] 0-3
    account_emb: dict[str, np.ndarray] = field(default_factory=dict)  # reader -> [n, d]


def subset_inputs(inp: Inputs, keep: np.ndarray) -> Inputs:
    """``inp`` restricted to the scenarios where ``keep`` is True (every aligned array; a
    monitor's [n_repeats, n] raw scores along their last axis)."""
    keep = np.asarray(keep, dtype=bool)
    return Inputs(
        ids=tuple(i for i, m in zip(inp.ids, keep, strict=True) if m),
        groups=inp.groups[keep], types=inp.types[keep], categories=inp.categories[keep],
        k={m: v[keep] for m, v in inp.k.items()}, n={m: v[keep] for m, v in inp.n.items()},
        embeddings=None if inp.embeddings is None else inp.embeddings[keep],
        acts={m: {L: X[keep] for L, X in f.items()} for m, f in inp.acts.items()},
        monitor_raw={key: np.asarray(v)[..., keep] for key, v in inp.monitor_raw.items()},
        commit={r: v[keep] for r, v in inp.commit.items()},
        account_emb={r: v[keep] for r, v in inp.account_emb.items()},
    )


def build_predictions(
    inp: Inputs,
    assign: np.ndarray,
    *,
    targets: Sequence[str] = ("qwen",),
    cross_probes: bool = True,
    Cs: Sequence[float] = C_GRID,
    seeds: Sequence[int] = C.FOLD_SEEDS,
) -> dict[tuple[str, str], PredSet]:
    """{(predictor, target): PredSet} for every available predictor and target. Probes are fitted
    for every activation source against every target when ``cross_probes`` (the crossed control);
    readers, being trained on Qwen, only against their own targets as given."""
    out: dict[tuple[str, str], PredSet] = {}

    def add(name: str, tgt: str, fn: FitFn, transform: str, cal: str = "train") -> None:
        out[(name, tgt)] = cross_fit(
            name, fn, assign, inp.k[tgt], inp.n[tgt], ids=inp.ids, target_model=tgt,
            transform=transform, calibrate_on=cal,
        )

    for tgt in targets:
        k, n = inp.k[tgt], inp.n[tgt]
        add(CATEGORY_PRIOR, tgt, category_prior_fn(inp.categories, k, n), "logit")
        for mon in (MONITOR_ZERO, MONITOR_FEW):
            raw = inp.monitor_raw.get((mon, tgt))  # each target's own monitor calls, never
            if raw is not None:  # another target's (the prompt names the model)
                add(mon, tgt, fixed_score_fn(raw), "logit",
                    CALIBRATE_PREVIOUS_FOLD if mon == MONITOR_FEW else "train")
        if inp.embeddings is not None:
            add(EMBED, tgt, logistic_fn({0: inp.embeddings}, inp.groups, k, n, Cs=Cs, seeds=seeds),
                "identity")
        for src, feats in inp.acts.items():
            if src == tgt or cross_probes:
                add(probe_name(src), tgt, logistic_fn(feats, inp.groups, k, n, Cs=Cs, seeds=seeds),
                    "identity")
        if tgt == "qwen":
            for reader, cs in inp.commit.items():
                add(commit_name(reader), tgt, fixed_score_fn(cs), "identity")
            for reader, emb in inp.account_emb.items():
                add(account_name(reader), tgt,
                    logistic_fn({0: emb}, inp.groups, k, n, Cs=Cs, seeds=seeds), "identity")
    return out


# --- synthetic data (tests and ``i9_analyze.py --synthetic``) ----------------------------------

SYN_TYPES: tuple[str, ...] = C.LYNCH_SCENARIOS
SYN_LAYERS: tuple[int, ...] = (20, 40, 60)


def synthetic_inputs(
    *,
    seed: int = 0,
    n_scen: int = 64,
    n_rep: int = 16,
    d: int = 24,
    layers: Sequence[int] = SYN_LAYERS,
    signal_layer: int = 40,
    own_signal: float = 1.0,
    prompt_sd: float = 1.5,
) -> tuple[Inputs, dict[str, Any]]:
    """Inputs with known structure. Each model's true logit = a prompt-visible part (scenario type
    + goal value) + a model-specific part of size ``own_signal``. Embeddings see only the prompt
    part; each model's activations at ``signal_layer`` carry its own full logit (other layers are
    noise); readers' commitment and accounts see Qwen's full logit, noisily."""
    rng = np.random.default_rng(seed)
    goals = [f"g{i}" for i in range(9)]
    types = rng.choice(SYN_TYPES, n_scen)
    gv = rng.choice(goals, n_scen)
    groups = np.array([C.template_group(t, g) for t, g in zip(types, gv, strict=True)])
    type_eff = dict(zip(SYN_TYPES, (-0.8, 0.8, 0.0), strict=True))
    goal_eff = dict(zip(goals, rng.normal(0, 0.6, len(goals)), strict=True))
    prompt = prompt_sd * np.array([type_eff[t] + goal_eff[g] for t, g in zip(types, gv, strict=True)])
    own = {"qwen": rng.normal(0, 1, n_scen), "gemma": rng.normal(0, 1, n_scen)}
    true = {m: prompt + own_signal * own[m] for m in ("qwen", "gemma")}
    n = {m: np.full(n_scen, n_rep) for m in true}
    k = {m: rng.binomial(n_rep, sigmoid(true[m])) for m in true}

    def feat(z: np.ndarray, noise: float) -> np.ndarray:
        u = rng.standard_normal(d)
        return np.outer(z, u / np.linalg.norm(u)) * 3 + noise * rng.standard_normal((n_scen, d))

    acts = {
        m: {L: (feat(true[m], 1.0) if L == signal_layer else rng.standard_normal((n_scen, d)))
            for L in layers}
        for m in true
    }
    commit = np.clip(np.round(3 * sigmoid(true["qwen"] + rng.normal(0, 1, n_scen))), 0, 3)
    inp = Inputs(
        ids=tuple(f"syn-{i:03d}" for i in range(n_scen)),
        groups=groups,
        types=types,
        categories=types,
        k=k,
        n=n,
        embeddings=feat(prompt, 1.0),
        acts=acts,
        monitor_raw={
            (MONITOR_ZERO, "qwen"): sigmoid(prompt + rng.normal(0, 1.0, n_scen)),
            (MONITOR_FEW, "qwen"): sigmoid(prompt[None] + rng.normal(0, 0.8, (3, n_scen))),
        },
        commit={"olens": commit},
        account_emb={"olens": feat(true["qwen"], 2.0)},
    )
    # Gemma-target monitors (drawn last, so the draws above are unchanged): prompt-visible only
    inp.monitor_raw[(MONITOR_ZERO, "gemma")] = sigmoid(prompt + rng.normal(0, 1.0, n_scen))
    inp.monitor_raw[(MONITOR_FEW, "gemma")] = sigmoid(prompt[None]
                                                      + rng.normal(0, 0.8, (3, n_scen)))
    return inp, {"true": true, "prompt": prompt, "own": own}
