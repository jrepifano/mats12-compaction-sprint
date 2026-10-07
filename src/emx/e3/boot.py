"""E3's joint question bootstrap (prereg §4.5, "Uncertainty: a joint question bootstrap").

The pairs are a fixed measurement instrument, so only questions are resampled (prereg L149).
One replicate draws, with replacement, the broad questions of the set being analysed
(development, held-out or all) and the frozen *retained* medical set (prereg L151). The same
indices are used for every checkpoint, adapter and seed in that replicate (prereg L154), so a
`Boot` is built once per broad set and every quantity is computed from it. `Dist` objects carry
the `Boot` they came from, and arithmetic between two `Dist`s refuses different `Boot`s: a
comparison between checkpoints or seeds can only ever be paired.

Every statistic the rules need is linear in the per-question values (a mean of question means),
so a replicate is a weighted sum: `counts @ v / n`, where `counts[b, q]` is how often question q
was drawn in replicate b. That is exactly the mean over the resampled indices, vectorized.

Draw order (fixed): `default_rng(seed)` draws the medical indices first, then the broad ones.
With the same seed and the same retained medical set, ΔN's replicates are therefore identical
whichever broad set is analysed, so N and ΔN intervals agree between the development and
held-out analyses.

Intervals are percentile intervals: 95% by default ("lower/upper bound" = 2.5th/97.5th
percentiles, prereg L156); 90% for the TOST equivalence of §4.7 path (ii) (prereg L233).
Ratios are computed per replicate and only when the denominator's 95% interval excludes zero
(prereg L157); otherwise `ratio` returns None.

New code for E3 (no upstream to port; the percentile convention follows `emx.stats`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from emx.e3.contracts import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, CI_LEVEL


def _counts(idx: np.ndarray, n: int) -> np.ndarray:
    """(B, n) draw counts from (B, n) indices."""
    out = np.zeros((idx.shape[0], n), dtype=np.int32)
    rows = np.repeat(np.arange(idx.shape[0]), idx.shape[1])
    np.add.at(out, (rows, idx.ravel()), 1)
    return out


class Boot:
    """One joint question bootstrap over an ordered broad-question list and the retained
    medical-question list. Build it once per analysed broad set and reuse it everywhere."""

    def __init__(
        self,
        broad_qids: Sequence[str],
        med_qids: Sequence[str],
        n_boot: int = BOOTSTRAP_RESAMPLES,
        seed: int = BOOTSTRAP_SEED,
        label: str = "",
    ):
        if len(set(broad_qids)) != len(broad_qids) or len(set(med_qids)) != len(med_qids):
            raise ValueError("duplicate question ids")
        if not broad_qids or not med_qids:
            raise ValueError("empty question set")
        self.broad_qids = tuple(broad_qids)
        self.med_qids = tuple(med_qids)
        self.n_boot = int(n_boot)
        self.seed = int(seed)
        self.label = label
        rng = np.random.default_rng(seed)
        nm, nb = len(med_qids), len(broad_qids)
        self.med_idx = rng.integers(0, nm, size=(n_boot, nm))  # medical first (module doc)
        self.broad_idx = rng.integers(0, nb, size=(n_boot, nb))
        self.med_counts = _counts(self.med_idx, nm)
        self.broad_counts = _counts(self.broad_idx, nb)

    # -- vectors aligned to this Boot's question order ------------------------------------------

    def bvec(self, values) -> np.ndarray:
        """Per-broad-question values in `broad_qids` order from a {qid: value} mapping/Series;
        every question must have a finite value."""
        return _vec(values, self.broad_qids, "broad")

    def mvec(self, values) -> np.ndarray:
        return _vec(values, self.med_qids, "medical")

    # -- distributions --------------------------------------------------------------------------

    def broad_mean(self, values) -> Dist:
        v = self.bvec(values)
        return Dist(float(v.mean()), self.broad_counts @ v / len(v), self)

    def med_mean(self, values) -> Dist:
        v = self.mvec(values)
        return Dist(float(v.mean()), self.med_counts @ v / len(v), self)

    def broad_rate(self, num, den) -> Dist:
        """Pooled rate Σnum/Σden over resampled broad questions (question-cluster bootstrap of
        a pooled proportion, e.g. an EM rate over coherent answers)."""
        a, b = self.bvec(num), self.bvec(den)
        with np.errstate(invalid="ignore", divide="ignore"):
            reps = (self.broad_counts @ a) / (self.broad_counts @ b)
            point = float(a.sum() / b.sum()) if b.sum() > 0 else float("nan")
        return Dist(point, reps, self)


def _vec(values, qids: Sequence[str], what: str) -> np.ndarray:
    get = values.get if hasattr(values, "get") else dict(values).get
    out = np.array([np.nan if get(q) is None else float(get(q)) for q in qids], dtype=float)
    if not np.isfinite(out).all():
        missing = [q for q, x in zip(qids, out, strict=True) if not np.isfinite(x)]
        raise ValueError(f"{what} questions without a value: {missing[:5]}")
    return out


@dataclass(frozen=True)
class Dist:
    """A point estimate with its bootstrap replicates, tied to the `Boot` that drew them."""

    point: float
    reps: np.ndarray
    boot: Boot

    def _check(self, other: Dist) -> None:
        if other.boot is not self.boot:
            raise ValueError(
                "Dists from different bootstraps cannot be combined: the joint bootstrap "
                "uses one index draw per replicate for every checkpoint, adapter and seed"
            )

    def __add__(self, o):
        if isinstance(o, Dist):
            self._check(o)
            return Dist(self.point + o.point, self.reps + o.reps, self.boot)
        return Dist(self.point + float(o), self.reps + float(o), self.boot)

    def __sub__(self, o):
        if isinstance(o, Dist):
            self._check(o)
            return Dist(self.point - o.point, self.reps - o.reps, self.boot)
        return Dist(self.point - float(o), self.reps - float(o), self.boot)

    def __mul__(self, k: float):
        return Dist(self.point * float(k), self.reps * float(k), self.boot)

    __rmul__ = __mul__

    def interval(self, level: float = CI_LEVEL) -> tuple[float, float]:
        a = 100 * (1 - level) / 2
        r = self.reps[~np.isnan(self.reps)]
        if r.size == 0:
            return float("nan"), float("nan")
        lo, hi = np.percentile(r, [a, 100 - a])
        return float(lo), float(hi)

    @property
    def lo(self) -> float:
        return self.interval()[0]

    @property
    def hi(self) -> float:
        return self.interval()[1]

    def excludes_zero(self, level: float = CI_LEVEL) -> bool:
        lo, hi = self.interval(level)
        return bool(lo > 0 or hi < 0)

    def est(self, level: float = CI_LEVEL) -> dict:
        lo, hi = self.interval(level)
        return {"point": _f(self.point), "lo": _f(lo), "hi": _f(hi)}


def _f(x: float) -> float | None:
    return None if x is None or not np.isfinite(x) else float(x)


def est(d: Dist | None, level: float = CI_LEVEL) -> dict | None:
    """`Est` payload ({point, lo, hi}) or None (a ratio that was not computed)."""
    return None if d is None else d.est(level)


def ratio(num: Dist, den: Dist) -> Dist | None:
    """num / den per replicate; None when den's 95% interval includes zero (prereg L157)."""
    num._check(den)
    if not den.excludes_zero():
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        return Dist(num.point / den.point, num.reps / den.reps, num.boot)


def mean_of(*ds: Dist) -> Dist:
    """Seed average of paired Dists (numerator and denominator are averaged separately, before
    any ratio is taken: prereg §4.8 L275, L284)."""
    out = ds[0]
    for d in ds[1:]:
        out = out + d
    return out * (1.0 / len(ds))


def ratio_minus(r_c: Dist | None, r_ref: Dist | None, frac: float) -> Dist | None:
    """R_c − frac·R_ref on the same replicates (e.g. R_c − R_ref/3, prereg L202; R^HO −
    (2/3)R^HO_ref, L279; R_nb − (2/3)R_ref, L286). None if either ratio was not computed."""
    if r_c is None or r_ref is None:
        return None
    return r_c - r_ref * frac


# --- Spearman over question x adapter cells (prereg §4.7 L228) ---------------------------------


def weighted_spearman(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Spearman correlation of the sample in which cell k appears w[b, k] times, for each row b
    of w (B, K). Exact: midranks in the expanded sample are computed from the weights, then a
    weighted Pearson correlation of the ranks. Rows with no rank variation give NaN."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    w = np.atleast_2d(np.asarray(w, float))

    def ranks(v):
        less = (v[None, :] < v[:, None]).astype(float)  # less[k, j] = v_j < v_k
        eq = (v[None, :] == v[:, None]).astype(float)
        return w @ less.T + (w @ eq.T + 1.0) / 2.0  # (B, K)

    rx, ry = ranks(x), ranks(y)
    tot = w.sum(axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        mx = (w * rx).sum(1, keepdims=True) / tot
        my = (w * ry).sum(1, keepdims=True) / tot
        cov = (w * (rx - mx) * (ry - my)).sum(1)
        vx = (w * (rx - mx) ** 2).sum(1)
        vy = (w * (ry - my) ** 2).sum(1)
        return cov / np.sqrt(vx * vy)


def spearman_dist(
    boot: Boot, cell_qids: Sequence[str], x: Sequence[float], y: Sequence[float]
) -> Dist:
    """Spearman of (x, y) over cells, with a question bootstrap that moves each question's
    cells together (the same broad indices as every other Stage A quantity)."""
    col = {q: i for i, q in enumerate(boot.broad_qids)}
    unknown = [q for q in cell_qids if q not in col]
    if unknown:
        raise ValueError(f"cells for questions outside the bootstrap set: {unknown[:5]}")
    w = boot.broad_counts[:, [col[q] for q in cell_qids]]
    point = float(weighted_spearman(x, y, np.ones((1, len(x))))[0])
    return Dist(point, weighted_spearman(x, y, w), boot)
