"""E9's hierarchical paired bootstrap (prereg §4.6 "Uncertainty", L253; §4.11 for four seeds).

One replicate draws CPT seeds with replacement, then adapter seeds with replacement within each
drawn CPT seed, then questions with replacement. Pairs within a question are a fixed instrument
and are not resampled. The **same draws are used for every arm** (and every component) analysed
with one `HBoot`, so every contrast is paired: identical arms give Δ = 0 in every replicate.

Questions are crossed with seeds (every run scores the same questions), so a replicate makes one
question draw and applies it to every (CPT seed, adapter seed) cell it drew. That is E3's joint
question bootstrap (`emx.e3.boot`) with the seed levels added on top; drawing questions afresh
inside every cell would average shared question effects away and make the interval too narrow.

Every statistic the rules need is linear in the per-cell, per-question values (a mean over drawn
seed cells of a mean over drawn questions, equal question weights), so a replicate is
`Σ_cells w[b, cell] · (counts[b] @ X[cell]) / n_q`, with `w` the normalized cell draw counts.
The point estimate is the plain mean over CPT seeds, adapter seeds and questions, which equals
the mean of the seed-specific contrasts d_s (L251).

Draw order (fixed): `default_rng(seed)` draws the CPT seed indices, then the adapter indices,
then the question indices. Two `HBoot`s with the same seed and the same numbers of CPT and
adapter seeds therefore make the same seed draws whatever their question sets, so a broad and a
medical quantity of one analysis share their seed draws.

Intervals are percentile intervals (95%; 90% for the stance/style TOST, L298). `Dist` and its
helpers are E3's (`emx.e3.boot`), reused unchanged: a `Dist` remembers its `HBoot`, and
arithmetic between Dists of different bootstraps is refused.

A question-only bootstrap (`question_boot`) is the 1 × 1 case: it is used for the checkpoint's
E1b adapters (§4.7 step 2) and other single-model checks.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from emx.e3.boot import Dist, est, mean_of  # noqa: F401  (re-exported for rules and scripts)
from emx.e9.contracts import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, CI_LEVEL, TOST_CI_LEVEL


class HBoot:
    """A hierarchical paired bootstrap over `n_cpt` CPT seeds × `n_ad` adapter seeds × `qids`.

    Values are arrays of shape (n_cpt, n_ad, n_q) in the order of `cpt_seeds`, `adapter_seeds`
    and `qids`; build one `HBoot` per analysed question list and reuse it for every arm."""

    def __init__(
        self,
        cpt_seeds: Sequence[int],
        adapter_seeds: Sequence[int],
        qids: Sequence[str],
        n_boot: int = BOOTSTRAP_RESAMPLES,
        seed: int = BOOTSTRAP_SEED,
        label: str = "",
    ):
        if len(set(qids)) != len(qids) or not qids:
            raise ValueError("questions must be non-empty and unique")
        if not cpt_seeds or not adapter_seeds:
            raise ValueError("need at least one CPT seed and one adapter seed")
        self.cpt_seeds = tuple(int(s) for s in cpt_seeds)
        self.adapter_seeds = tuple(int(k) for k in adapter_seeds)
        self.qids = tuple(str(q) for q in qids)
        self.n_boot, self.seed, self.label = int(n_boot), int(seed), label
        nc, na, nq = len(self.cpt_seeds), len(self.adapter_seeds), len(self.qids)
        rng = np.random.default_rng(seed)
        self.cpt_idx = rng.integers(0, nc, size=(n_boot, nc))
        self.ad_idx = rng.integers(0, na, size=(n_boot, nc, na))
        self.q_idx = rng.integers(0, nq, size=(n_boot, nq))
        # cell weights: how often (c, a) was drawn, normalized to sum 1 per replicate
        flat = self.cpt_idx[:, :, None] * na + self.ad_idx  # (B, nc, na) cell index
        w = np.zeros((n_boot, nc * na))
        rows = np.repeat(np.arange(n_boot), nc * na)
        np.add.at(w, (rows, flat.ravel()), 1.0)
        self.cell_w = w / (nc * na)
        q = np.zeros((n_boot, nq))
        np.add.at(q, (np.repeat(np.arange(n_boot), nq), self.q_idx.ravel()), 1.0)
        self.q_counts = q

    @property
    def shape(self) -> tuple[int, int, int]:
        return len(self.cpt_seeds), len(self.adapter_seeds), len(self.qids)

    def _check(self, x) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        if x.shape != self.shape:
            raise ValueError(f"values of shape {x.shape}, bootstrap expects {self.shape}")
        if not np.isfinite(x).all():
            raise ValueError("non-finite value in a bootstrap input")
        return x

    def mean(self, x) -> Dist:
        """Mean over CPT seeds, adapter seeds and questions (equal weights), with replicates."""
        x = self._check(x)
        nc, na, nq = self.shape
        per_cell = x.reshape(nc * na, nq) @ self.q_counts.T / nq  # (cells, B)
        reps = np.einsum("bc,cb->b", self.cell_w, per_cell)
        return Dist(float(x.mean()), reps, self)

    def seed_means(self, x) -> list[float]:
        """Per-CPT-seed point means (d_s for a contrast array), in `cpt_seeds` order."""
        x = self._check(x)
        return [float(v) for v in x.mean(axis=(1, 2))]

    def adapter_means(self, x) -> np.ndarray:
        """(n_cpt, n_ad) question-mean values (e.g. Q_{A,s,k})."""
        return self._check(x).mean(axis=2)


def question_boot(qids: Sequence[str], n_boot: int = BOOTSTRAP_RESAMPLES,
                  seed: int = BOOTSTRAP_SEED, label: str = "") -> HBoot:
    """A question-only bootstrap: the 1 × 1 seed case (values of shape (1, 1, n_q))."""
    return HBoot((0,), (0,), qids, n_boot=n_boot, seed=seed, label=label)


def qvec(values: dict, qids: Sequence[str]) -> np.ndarray:
    """(1, 1, n_q) values for `question_boot` from a {qid: value} mapping."""
    return np.array([[[float(values[q]) for q in qids]]])


def half_width(d: Dist, level: float = CI_LEVEL) -> float:
    """Half the width of the percentile interval, (hi − lo) / 2 (h₂, §4.7 step 5)."""
    lo, hi = d.interval(level)
    return float((hi - lo) / 2)


def payload(d: Dist | None, tost: bool = False) -> dict | None:
    """{point, lo, hi} at 95%, plus {lo90, hi90} for the TOST interval when asked."""
    if d is None:
        return None
    out = est(d, CI_LEVEL)
    if tost:
        lo, hi = d.interval(TOST_CI_LEVEL)
        out |= {"lo90": float(lo), "hi90": float(hi)}
    return out
