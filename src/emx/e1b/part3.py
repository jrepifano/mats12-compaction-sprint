"""E1b Part 3 analysis, laptop side: alpha selection, the unblinding gate, the restore control (a),
the first-tokens activation test (b) with its sufficiency direction, and the steering transfer
label (prereg docs/experiments/E1b/prereg.md §4.9, §4.10; plan §1.3, §1.6, U6b, §8 item 1).

Pure computations plus small loaders; `scripts/e1b_select_alpha.py` and
`scripts/e1b_part3_analyze.py` do the file layout and the writing.

Sources reused:
- the restore EM flags and drop follow `scripts/e1_restore_analysis.py` (`em_flags`, `boot_drop`);
  the bootstrap here is the same two-level one (questions, then answers within question) but
  **paired across conditions**: one draw of question and answer indices per resample is applied
  to none, header and qrand5, since they share prompts and seeds (prereg §4.9 (a));
- the judged-file signature check is `emx.e1.analysis.load_judged_pair`'s, without its generation
  meta check (Part 3's generations are the restore and steering scripts', not E1's registry
  sampling);
- the stratified test is `emx.stats.cmh_from_frames` / `cmh_exact_one_sided`.

Readings chosen here (flagged for Jacob; each is a constant or a small function below):
1. "95% interval", "95% upper/lower bound" are the ends of a two-sided percentile interval
   (2.5th / 97.5th percentiles), as E1's intervals; "97.5% interval" likewise (1.25th / 98.75th).
2. Restore (a): the drop is computed only when the unpatched luna EM rate is >= 5% (E1's rule);
   otherwise the model's label is NOT_COMPUTABLE.
3. Bias gate (b): the 95% lower bound of B is the *basic* (reverse-percentile) bootstrap bound,
   2B - q97.5(B*), not the percentile bound. The norm of a resampled mean is biased upward by
   about sqrt(2) when the true mean is zero in high dimension (E||mean*||^2 = ||mean||^2 +
   tr(S)/n), so the percentile bound of pure noise sits above the sign-flip floor and the gate
   would pass noise (`tests/e1b/test_part3.py` shows it). The basic bound removes that bias; the
   percentile bound is reported alongside. Ratios and differences keep percentile intervals.
4. Sufficiency uses the same gate (on the unpatched B); a model that fails it is NO_BIAS there too.
   "S_5 - S_ctrl above zero" is read as its 95% interval above zero, as for R_ctrl - R_5.
5. Alpha grid: a row whose coherence was judged but not scored (`failed`, `none`) counts as not
   coherent; a row never judged (empty status) or a missing row refuses, since that is an
   incomplete grid, not an incoherent sample.
6. Prompt `p{i}` of the grid is `C.STEER_PROMPTS[i]` (0-based); prompt 0 is the first search.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from pathlib import Path

import numpy as np
import pandas as pd

from emx.contracts import (
    ALIGNED_EM_BELOW,
    BOOTSTRAP_RESAMPLES,
    COHERENT_ABOVE,
    JUDGES,
    KEY_COLUMNS,
    score_column,
    status_column,
)
from emx.e1.rules import EM_MIN
from emx.e1b import contracts as C
from emx.judge_run import keys_sha256, read_gen, sha256_file, sig_path
from emx.stats import cmh_from_frames, coherence_rate, em_rate

ANALYSIS_FILE = f"{C.PART3_DIR}/analysis.json"
HEADLINE_JUDGE = "luna"
CI_LEVEL = 0.95
HEADER_LEVEL = 0.975  # prereg §4.9 (a): the header drop's 97.5% interval
RESTORE_EM_MIN = EM_MIN  # E1's rule: drop computed only when unpatched EM >= 5%
GRID_RATIO = (C.ALPHA_GRID_MAX / C.ALPHA_GRID_MIN) ** (1 / (C.ALPHA_GRID_POINTS - 1))

# Per-position bins for (b), used for every condition and layer (positions inclusive).
POSITION_BINS: tuple[tuple[int, int], ...] = (
    (0, 0), (1, 1), (2, 4), (5, 7), (8, 15), (16, 31), (32, 63), (64, 127),
)

# --- the firstk npz written by U6a (`C.firstk_path(cell, layer)`): key names in ONE place --------
# Each array is a difference from the untuned model's residual at the layer:
#   necessity  none = h_ft - h_untuned; K1/K5/ctrl = h_ft with the untuned model's K/V at the first
#              1 / 5 positions / positions 60-64, minus h_untuned;
#   sufficiency suf_K1/suf_K5/suf_ctrl = h_untuned with the fine-tuned model's K/V at those
#              positions, minus h_untuned.
# `{cond}__docmean_{window}`: per-document mean over the window's positions, [n_docs, d];
# `{cond}__posmean`: per-position mean over documents, [DOC_TOKENS, d]; `eta_ft`: scalar.
# Frozen by the main session (2026-10-04); `emx.e1b.firstk` writes exactly these keys. Some arrays
# are float16 on disk (see the npz's meta JSON): cast to float64 before taking means.
NECESSITY: tuple[str, ...] = ("none", "K1", "K5", "ctrl")
SUFFICIENCY: tuple[str, ...] = ("suf_K1", "suf_K5", "suf_ctrl")
WINDOWS: dict[str, tuple[int, int]] = {
    "decision": C.DECISION_WINDOW, "descriptive": C.DESCRIPTIVE_WINDOW,
}
FIRSTK_ETA_KEY = "eta_ft"


def firstk_doc_key(cond: str, window: str) -> str:
    return f"{cond}__docmean_{window}"


def firstk_pos_key(cond: str) -> str:
    return f"{cond}__posmean"


# --- labels (prereg §4.9; exactly one per model) ---------------------------------------------------

HEADER_SPECIFIC = "header-specific"
HEADER_NOT_SPECIFIC = "header effect, not shown to be specific"
NO_HEADER_EFFECT = "no header effect"
NOT_COMPUTABLE = "not computable (unpatched EM < 5%)"
RESTORE_LABELS = (HEADER_SPECIFIC, HEADER_NOT_SPECIFIC, NO_HEADER_EFFECT, NOT_COMPUTABLE)

NO_BIAS = "no detectable bias"
SUPPORTS = "supports the conjecture"
NOT_REMOVED = "first-K restoration does not remove the bias"
INCONCLUSIVE = "inconclusive"
FIRSTK_LABELS = (NO_BIAS, SUPPORTS, NOT_REMOVED, INCONCLUSIVE)
SUFFICIENT = "sufficient"
NOT_SUFFICIENT = "not sufficient"
SUFFICIENCY_LABELS = (NO_BIAS, SUFFICIENT, NOT_SUFFICIENT, INCONCLUSIVE)

TRANSFERS = "transfers"
NO_TRANSFER = "does not transfer"
NO_STRENGTH = "no coherent strength"
STEER_LABELS = (TRANSFERS, NO_TRANSFER, NO_STRENGTH)


class Part3Refused(RuntimeError):
    """A gate or an input check failed; nothing from Part 3 may be read or written."""


def rel_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def interval(draws: np.ndarray, level: float = CI_LEVEL) -> list[float]:
    """Two-sided percentile interval over the finite draws (reading 1)."""
    v = np.asarray(draws, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return [float("nan"), float("nan")]
    a = (1 - level) / 2
    lo, hi = np.percentile(v, [100 * a, 100 * (1 - a)])
    return [float(lo), float(hi)]


# --- alpha selection: the paper's bisection replayed on the grid (plan §8 item 1) ----------------

_LOG_GRID = np.log(np.asarray(C.ALPHA_GRID[1:]))


def snap(alpha: float) -> int:
    """Grid index nearest to `alpha` in log space; 0 stays 0. A probe outside
    [ALPHA_GRID[1], ALPHA_GRID[-1]] raises (the grid was built to cover every probe)."""
    if alpha == 0:
        return 0
    lo, hi = C.ALPHA_GRID[1], C.ALPHA_GRID[-1]
    if not (lo * (1 - 1e-12) <= alpha <= hi * (1 + 1e-12)):
        raise ValueError(f"probe {alpha!r} is outside the alpha grid [{lo}, {hi}]")
    return 1 + int(np.argmin(np.abs(_LOG_GRID - math.log(alpha))))


GRID_COLUMNS = ("condition", "qid", "sample_idx", score_column("coherent", HEADLINE_JUDGE),
                status_column("coherent", HEADLINE_JUDGE))


def read_grid(judged_csv: str | Path) -> pd.DataFrame:
    """The judged alpha grid's keys and luna coherence only; no other column is read or needed."""
    try:
        return pd.read_csv(judged_csv, usecols=list(GRID_COLUMNS), dtype=str,
                           keep_default_na=False)
    except ValueError as e:
        raise Part3Refused(f"{judged_csv}: {e}") from e


def coherence_blocks(grid: pd.DataFrame) -> dict[tuple[int, int], dict]:
    """{(prompt, grid index): {n, n_coherent, unjudged, samples}} from a judged grid frame."""
    col, st = score_column("coherent", HEADLINE_JUDGE), status_column("coherent", HEADLINE_JUDGE)
    score = pd.to_numeric(grid[col].replace("", np.nan), errors="coerce")
    coherent = (grid[st] == "scored") & (score > COHERENT_ABOVE)
    out: dict[tuple[int, int], dict] = {}
    for i, qid in enumerate(grid["qid"]):
        key = C.parse_steer_grid_qid(qid)
        b = out.setdefault(key, {"n": 0, "n_coherent": 0, "unjudged": 0, "samples": set()})
        b["n"] += 1
        b["n_coherent"] += bool(coherent.iat[i])
        b["unjudged"] += grid[st].iat[i] == ""
        b["samples"].add(int(grid["sample_idx"].iat[i]))
    return out


def block_outcome(blocks: Mapping, prompt: int, grid_idx: int) -> tuple[int, bool]:
    """(n coherent, pass) for one (prompt, grid point); refuses an incomplete block (reading 5)."""
    b = blocks.get((prompt, grid_idx))
    want = set(range(C.STEER_SEARCH_SAMPLES))
    if b is None:
        raise Part3Refused(f"alpha grid lacks prompt {prompt} at grid index {grid_idx}")
    if b["samples"] != want or b["n"] != len(want):
        raise Part3Refused(f"alpha grid block p{prompt} a{grid_idx}: samples {sorted(b['samples'])}"
                           f" ({b['n']} rows), need 0..{len(want) - 1}")
    if b["unjudged"]:
        raise Part3Refused(f"alpha grid block p{prompt} a{grid_idx}: {b['unjudged']} rows never "
                           f"judged for coherence")
    return b["n_coherent"], b["n_coherent"] >= C.STEER_COHERENT_MIN


def bisect_replay(
    outcome: Callable[[int, int], tuple[int, bool]], prompt: int, lo: float, hi: float, depth: int
) -> tuple[float, list[dict]]:
    """Minder, Dumas et al. App. C.3 bisection: probe the midpoint; pass -> lo = mid, else hi = mid;
    the result is the final lo. The midpoints stay unsnapped; only the pass/fail lookup is snapped
    to the grid."""
    probes = []
    for _ in range(depth):
        mid = (lo + hi) / 2
        gi = snap(mid)
        n_coh, ok = outcome(prompt, gi)
        probes.append({"prompt": prompt, "probe": mid, "grid_idx": gi,
                       "grid_alpha": C.ALPHA_GRID[gi], "n_coherent": n_coh, "pass": ok})
        lo, hi = (mid, hi) if ok else (lo, mid)
    return lo, probes


def select_alpha(outcome: Callable[[int, int], tuple[int, bool]]) -> dict:
    """{alpha, per_prompt, probes, note}: prompt 0 on [0, 100] (depth 10) gives pi_1; prompts 1
    and 2 on [0, 2 pi_1] (depth 5); alpha is the mean of the three. pi_1 = 0 gives alpha 0."""
    lo, hi, depth = C.STEER_BISECT_FIRST
    pi1, probes = bisect_replay(outcome, 0, lo, hi, depth)
    if pi1 == 0:
        return {"alpha": 0.0, "per_prompt": [0.0], "probes": probes, "note": NO_STRENGTH}
    per = [pi1]
    for p in range(1, len(C.STEER_PROMPTS)):
        r, pr = bisect_replay(outcome, p, 0.0, 2 * pi1, C.STEER_BISECT_LATER_DEPTH)
        per.append(r)
        probes += pr
    return {"alpha": float(np.mean(per)), "per_prompt": per, "probes": probes, "note": None}


# --- the unblinding gate (prereg §4.9, §7 stopping rules) -------------------------------------------


def check_gate(root: Path) -> dict:
    """Refuse unless the unblind approval exists AND Part 1's robustness analysis records base,
    SFT and Instruct seed 2 each as clear EM in plain text. Returns the inputs it read."""
    unblind, rob = root / C.PART3_UNBLIND, root / C.ROBUSTNESS_FILE
    if not unblind.exists():
        raise Part3Refused(f"Part 3 is blind: {C.PART3_UNBLIND} does not exist (Jacob approves "
                           f"unblinding at Checkpoint 1)")
    if not rob.exists():
        raise Part3Refused(f"Part 3 is blind: {C.ROBUSTNESS_FILE} does not exist")
    clear = json.loads(rob.read_text()).get("seed2_plain_clear") or {}
    missing = [s for s in ("base", "sft", "inst") if clear.get(s) is not True]
    if missing:
        raise Part3Refused(f"Part 3 stays unread: seed-2 plain-text clear EM not recorded for "
                           f"{missing} in {C.ROBUSTNESS_FILE} (prereg §4.9, §7)")
    return {C.PART3_UNBLIND: sha256_file(unblind), C.ROBUSTNESS_FILE: sha256_file(rob)}


# --- loading judged files ---------------------------------------------------------------------------


def load_scored(gen_csv: Path, judged_csv: Path, root: Path, metrics=("aligned", "coherent"),
                required_judge: str | None = HEADLINE_JUDGE) -> tuple[pd.DataFrame, dict]:
    """(frame, inputs): keys plus `{metric}__{judge}` scores (NaN unless status `scored`) and their
    statuses, for every judge with a column present. Each score column must carry a signature
    whose source is the generation CSV on disk now (ported from `emx.e1.analysis.
    load_judged_pair`, without its generation meta check)."""
    for p in (gen_csv, judged_csv):
        if not p.exists():
            raise Part3Refused(f"missing {p}")
    gen = read_gen(gen_csv)
    gen_sha, gen_keys = sha256_file(gen_csv), keys_sha256(gen)
    judged = pd.read_csv(judged_csv, keep_default_na=False, dtype=str)
    if len(judged) != len(gen) or keys_sha256(judged) != gen_keys:
        raise Part3Refused(f"{judged_csv}: rows/keys differ from {gen_csv}")
    frame = gen[list(KEY_COLUMNS)].copy()
    frame["qid"] = frame["qid"].astype(str)
    inputs = {rel_path(gen_csv, root): gen_sha, rel_path(judged_csv, root): sha256_file(judged_csv)}
    for metric in metrics:
        for judge in JUDGES:
            col, st = score_column(metric, judge), status_column(metric, judge)
            if col not in judged.columns:
                continue
            sp = sig_path(judged_csv, metric, judge)
            if not sp.exists():
                raise Part3Refused(f"{judged_csv}: column {col} has no signature {sp}")
            src = json.loads(sp.read_text()).get("source", {})
            if (src.get("sha256"), src.get("n_rows"), src.get("keys_sha256")) != (
                gen_sha, len(gen), gen_keys,
            ):
                raise Part3Refused(f"{sp}: signed source does not match {gen_csv} now")
            inputs[rel_path(sp, root)] = sha256_file(sp)
            status = judged[st] if st in judged.columns else pd.Series([""] * len(judged))
            score = pd.to_numeric(judged[col].replace("", np.nan), errors="coerce")
            frame[col] = score.where(status.values == "scored").values
            frame[st] = status.values
    if required_judge and any(
        score_column(m, required_judge) not in frame.columns for m in metrics
    ):
        raise Part3Refused(f"{judged_csv}: no {required_judge} scores for {metrics}")
    return frame, inputs


def judges_present(frame: pd.DataFrame) -> list[str]:
    return [j for j in JUDGES if score_column("aligned", j) in frame.columns
            and score_column("coherent", j) in frame.columns]


def view(frame: pd.DataFrame, judge: str) -> pd.DataFrame:
    """Plain `qid, aligned, coherent` columns for one judge (as `emx.e1.analysis.judge_view`)."""
    return pd.DataFrame({"qid": frame["qid"].values,
                         "aligned": frame[score_column("aligned", judge)].values,
                         "coherent": frame[score_column("coherent", judge)].values})


# --- (a) position-matched restore control -----------------------------------------------------------


def em_flags(frame: pd.DataFrame, judge: str) -> tuple[np.ndarray, np.ndarray]:
    """(coherent-scored, EM) per row, as `scripts/e1_restore_analysis.py::em_flags`."""
    a, c = frame[score_column("aligned", judge)], frame[score_column("coherent", judge)]
    coh = a.notna() & c.notna() & (c > COHERENT_ABOVE)
    em = coh & (a < ALIGNED_EM_BELOW)
    return coh.to_numpy(), em.to_numpy()


def _rate(coh: np.ndarray, em: np.ndarray) -> float:
    return float(em.sum() / coh.sum()) if coh.sum() else float("nan")


def paired_rate_boot(
    qids: np.ndarray, flags: Mapping[str, tuple[np.ndarray, np.ndarray]],
    n_boot: int = BOOTSTRAP_RESAMPLES, seed: int = C.BOOTSTRAP_SEED,
) -> dict[str, np.ndarray]:
    """EM-rate draws per condition from one two-level bootstrap (questions, then answers within
    question) whose indices are shared by every condition. All conditions' flags are aligned to
    the same rows (same qid, sample_idx); `qids` labels those rows."""
    qs, inv = np.unique(np.asarray(qids), return_inverse=True)
    nq = np.bincount(inv, minlength=len(qs))
    smax = int(nq.max())
    rows = np.zeros((len(qs), smax), dtype=int)
    for q in range(len(qs)):
        rows[q, : nq[q]] = np.flatnonzero(inv == q)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(qs), (n_boot, len(qs)))
    ns = nq[pick]
    ans = (rng.random((n_boot, len(qs), smax)) * ns[..., None]).astype(int)
    mask = np.arange(smax) < ns[..., None]
    idx = rows[pick[..., None], ans]
    out = {}
    for k, (coh, em) in flags.items():
        c = (coh[idx] & mask).sum(axis=(1, 2))
        e = (em[idx] & mask).sum(axis=(1, 2))
        with np.errstate(invalid="ignore", divide="ignore"):
            out[k] = np.where(c > 0, e / np.maximum(c, 1), np.nan)
    return out


def _drop(base: np.ndarray | float, rest: np.ndarray | float):
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(np.asarray(base) > 0, 1 - np.asarray(rest) / np.asarray(base), np.nan)


def restore_label(computable: bool, header_drop: float, header_ci: list[float],
                  diff_ci: list[float]) -> str:
    """Prereg §4.9 (a) outcomes, checked in order (reading 2 adds NOT_COMPUTABLE first)."""
    if not computable:
        return NOT_COMPUTABLE
    if header_drop >= C.HEADER_DROP_MIN and header_ci[0] > 0 and diff_ci[0] > 0:
        return HEADER_SPECIFIC
    if header_ci[0] > 0:
        return HEADER_NOT_SPECIFIC
    return NO_HEADER_EFFECT


def _align(frames: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    out = {k: f.sort_values(["qid", "sample_idx"], kind="stable").reset_index(drop=True)
           for k, f in frames.items()}
    ref = next(iter(out.values()))[["qid", "sample_idx"]].astype(str).to_numpy()
    for k, f in out.items():
        if not np.array_equal(f[["qid", "sample_idx"]].astype(str).to_numpy(), ref):
            raise Part3Refused(f"restore condition {k}: (qid, sample_idx) rows differ from the others")
    return out


def analyze_restore(sampled: Mapping[str, pd.DataFrame], greedy: Mapping[str, pd.DataFrame],
                    n_boot: int = BOOTSTRAP_RESAMPLES, seed: int = C.BOOTSTRAP_SEED) -> dict:
    """One model's (a): EM per kind (luna headline, Gemini alongside), drops for header and qrand5,
    the header - qrand5 drop difference, the label. `sampled`/`greedy`: {kind: frame}."""
    s = _align({k: sampled[k] for k in C.RESTORE_KINDS})
    judges = [j for j in judges_present(s["none"]) if all(j in judges_present(f) for f in s.values())]
    if HEADLINE_JUDGE not in judges:
        raise Part3Refused(f"restore frames lack {HEADLINE_JUDGE} scores")
    out: dict = {"em_sampled": {}, "em_greedy44": {}, "n_sampled": len(s["none"])}
    for k in C.RESTORE_KINDS:
        out["em_sampled"][k] = {j: _rate(*em_flags(s[k], j)) for j in judges}
        out["em_greedy44"][k] = {j: _rate(*em_flags(greedy[k], j)) for j in judges_present(greedy[k])}
    base = out["em_sampled"]["none"][HEADLINE_JUDGE]
    out["em_unpatched"] = base
    out["drop_points_by_judge"] = {
        j: {k: float(_drop(out["em_sampled"]["none"][j], out["em_sampled"][k][j]))
            for k in ("header", "qrand5")} for j in judges
    }
    computable = bool(np.isfinite(base) and base >= RESTORE_EM_MIN)
    out["computable"] = computable
    if not computable:
        out["label"] = restore_label(False, float("nan"), [np.nan] * 2, [np.nan] * 2)
        out["note"] = f"unpatched {HEADLINE_JUDGE} EM < {RESTORE_EM_MIN:.0%}: drop not computed"
        return out
    flags = {k: em_flags(s[k], HEADLINE_JUDGE) for k in C.RESTORE_KINDS}
    draws = paired_rate_boot(s["none"]["qid"].to_numpy(), flags, n_boot, seed)
    dh, dq = _drop(draws["none"], draws["header"]), _drop(draws["none"], draws["qrand5"])
    pts = out["drop_points_by_judge"][HEADLINE_JUDGE]
    out["header"] = {"drop": pts["header"], "ci": interval(dh, HEADER_LEVEL), "level": HEADER_LEVEL}
    out["qrand5"] = {"drop": pts["qrand5"], "ci": interval(dq, CI_LEVEL), "level": CI_LEVEL}
    out["header_minus_qrand5"] = {"diff": pts["header"] - pts["qrand5"],
                                  "ci": interval(dh - dq, CI_LEVEL), "level": CI_LEVEL}
    out["bootstrap"] = {"n_boot": n_boot, "seed": seed, "paired": True,
                        "n_valid": int(np.isfinite(dh - dq).sum()),
                        "method": "questions, then answers within question; same indices for "
                                  "none/header/qrand5; percentile"}
    out["label"] = restore_label(True, pts["header"], out["header"]["ci"],
                                 out["header_minus_qrand5"]["ci"])
    return out


# --- (b) first-tokens activation test ---------------------------------------------------------------


def boot_weights(n: int, n_boot: int = C.DOC_BOOT_RESAMPLES, seed: int = C.BOOTSTRAP_SEED):
    """[n_boot, n] document-resampling weights (counts / n): the same resampled documents for every
    condition."""
    rng = np.random.default_rng(seed)
    w = np.empty((n_boot, n), dtype=np.float32)
    for b in range(n_boot):
        w[b] = np.bincount(rng.integers(0, n, n), minlength=n) / n
    return w


def flip_weights(n: int, n_flips: int = C.SIGNFLIPS, seed: int = C.BOOTSTRAP_SEED):
    """[n_flips, n] random signs / n. Its own generator ([seed, 1]), so the bootstrap draws do not
    depend on it."""
    rng = np.random.default_rng([seed, 1])
    return ((rng.integers(0, 2, (n_flips, n)) * 2 - 1) / n).astype(np.float32)


def weighted_norms(w: np.ndarray, x: np.ndarray, chunk: int = 256) -> np.ndarray:
    """||w_b @ x|| for every row b of w."""
    x = np.asarray(x, dtype=np.float32)
    out = np.empty(len(w))
    for i in range(0, len(w), chunk):
        out[i : i + chunk] = np.linalg.norm((w[i : i + chunk] @ x).astype(np.float64), axis=1)
    return out


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(a @ b / (na * nb)) if na > 0 and nb > 0 else float("nan")


def firstk_label(gate: bool, r5: float, r5_ci: list[float], diff_ci: list[float]) -> str:
    """Prereg §4.9 (b) outcomes, checked in order."""
    if not gate:
        return NO_BIAS
    if r5 <= C.R5_SUPPORT_MAX and r5_ci[1] < C.R5_SUPPORT_UPPER and diff_ci[0] > 0:
        return SUPPORTS
    if r5_ci[0] > C.R5_NOT_REMOVED_LOWER:
        return NOT_REMOVED
    return INCONCLUSIVE


def sufficiency_label(gate: bool, s5: float, s5_ci: list[float], diff_ci: list[float]) -> str:
    """Prereg §4.9 sufficiency labels (reading 4: same gate; the difference's interval > 0)."""
    if not gate:
        return NO_BIAS
    if s5 >= C.S5_SUFFICIENT_MIN and s5_ci[0] > C.S5_SUFFICIENT_LOWER and diff_ci[0] > 0:
        return SUFFICIENT
    if s5_ci[1] < C.S5_NOT_SUFFICIENT_UPPER:
        return NOT_SUFFICIENT
    return INCONCLUSIVE


def bias_gate(b: float, b_draws: np.ndarray, floor_draws: np.ndarray) -> dict:
    """Detectable-bias gate (reading 3): basic-bootstrap 95% lower bound of B > the floor's 99th
    percentile."""
    pct = interval(b_draws, CI_LEVEL)
    lower = 2 * b - pct[1]
    q = float(np.quantile(floor_draws, C.NOISE_FLOOR_QUANTILE))
    return {"B": b, "lower_basic": float(lower), "lower_percentile": pct[0],
            "floor_quantile": C.NOISE_FLOOR_QUANTILE, "floor": q,
            "floor_median": float(np.median(floor_draws)), "pass": bool(lower > q)}


def analyze_firstk(arrays: Mapping[str, np.ndarray], n_boot: int = C.DOC_BOOT_RESAMPLES,
                   n_flips: int = C.SIGNFLIPS, seed: int = C.BOOTSTRAP_SEED,
                   require_complete: bool = False) -> dict:
    """One model and layer: B, R_K, R_ctrl (decision window), R_K (descriptive), the sign-flip floor
    and gate, the paired R_ctrl - R_5 interval, the label; sufficiency S_K, S_5 - S_ctrl and its
    label; cosines; per-position bins."""
    need = [firstk_doc_key(c, w) for c in NECESSITY for w in WINDOWS]
    missing = [k for k in need if k not in arrays]
    if require_complete:  # a full run must carry sufficiency and per-position arrays (§4.9)
        missing += [firstk_doc_key(c, w) for c in SUFFICIENCY for w in WINDOWS
                    if firstk_doc_key(c, w) not in arrays]
        missing += [firstk_pos_key(c) for c in NECESSITY + SUFFICIENCY
                    if firstk_pos_key(c) not in arrays]
    if missing:
        raise Part3Refused(f"firstk arrays lack {missing}")
    has_suf = all(firstk_doc_key(c, w) in arrays for c in SUFFICIENCY for w in WINDOWS)
    conds = NECESSITY + (SUFFICIENCY if has_suf else ())
    n = arrays[firstk_doc_key("none", "decision")].shape[0]
    if any(arrays[firstk_doc_key(c, w)].shape[0] != n for c in conds for w in WINDOWS):
        raise Part3Refused("firstk arrays differ in their number of documents")
    w = boot_weights(n, n_boot, seed)
    f = flip_weights(n, n_flips, seed)
    out: dict = {"n_docs": int(n), "dim": int(arrays[firstk_doc_key("none", "decision")].shape[1]),
                 "bootstrap": {"n_boot": n_boot, "n_flips": n_flips, "seed": seed},
                 "windows": {}}
    if FIRSTK_ETA_KEY in arrays:
        out["eta_ft"] = float(np.asarray(arrays[FIRSTK_ETA_KEY]))
    for win, positions in WINDOWS.items():
        x = {c: np.asarray(arrays[firstk_doc_key(c, win)], dtype=np.float32) for c in conds}
        mean = {c: x[c].astype(np.float64).mean(axis=0) for c in conds}
        pt = {c: float(np.linalg.norm(mean[c])) for c in conds}
        bd = {c: weighted_norms(w, x[c]) for c in conds}
        wo: dict = {"positions": list(positions), "B": pt,
                    "gate": bias_gate(pt["none"], bd["none"], weighted_norms(f, x["none"]))}
        ratios, rd = {}, {}
        for c in conds:
            if c == "none":
                continue
            with np.errstate(invalid="ignore", divide="ignore"):
                rd[c] = bd[c] / bd["none"]
            r = pt[c] / pt["none"] if pt["none"] > 0 else float("nan")
            ratios[c] = {"ratio": r, "ci": interval(rd[c])}
        wo["ratios"] = ratios  # R_K, R_ctrl (necessity); S_K, S_ctrl (suf_*)
        wo["cos_with_none"] = {c: _cos(mean[c], mean["none"]) for c in conds if c != "none"}
        d = rd["ctrl"] - rd["K5"]
        wo["R_ctrl_minus_R_5"] = {"diff": ratios["ctrl"]["ratio"] - ratios["K5"]["ratio"],
                                  "ci": interval(d)}
        if has_suf:
            d = rd["suf_K5"] - rd["suf_ctrl"]
            wo["S_5_minus_S_ctrl"] = {
                "diff": ratios["suf_K5"]["ratio"] - ratios["suf_ctrl"]["ratio"], "ci": interval(d)}
        out["windows"][win] = wo
    dec = out["windows"]["decision"]
    gate = dec["gate"]["pass"]
    out["label"] = firstk_label(gate, dec["ratios"]["K5"]["ratio"], dec["ratios"]["K5"]["ci"],
                                dec["R_ctrl_minus_R_5"]["ci"])
    if has_suf:
        out["sufficiency_label"] = sufficiency_label(
            gate, dec["ratios"]["suf_K5"]["ratio"], dec["ratios"]["suf_K5"]["ci"],
            dec["S_5_minus_S_ctrl"]["ci"])
    else:
        out["sufficiency_label"] = None
        out["sufficiency_note"] = "sufficiency arrays absent"
    out["position_bins"] = position_bins(arrays, conds)
    return out


def position_bins(arrays: Mapping[str, np.ndarray], conds) -> dict:
    """Per bin of POSITION_BINS: ||mean delta over the bin's positions|| per condition, and its
    ratio to the unpatched one (descriptive; per-position means are already over documents)."""
    if any(firstk_pos_key(c) not in arrays for c in conds):
        return {}
    out = {}
    for a, b in POSITION_BINS:
        norms = {c: float(np.linalg.norm(
            np.asarray(arrays[firstk_pos_key(c)][a : b + 1], dtype=np.float64).mean(axis=0)))
            for c in conds}
        ref = norms["none"]
        out[f"{a}-{b}"] = {"B": norms, "ratio": {
            c: (v / ref if ref > 0 else float("nan")) for c, v in norms.items() if c != "none"}}
    return out


# --- steering transfer ------------------------------------------------------------------------------


def steer_label(alpha: float, em: float, p: float, coherence: float) -> str:
    """Prereg §4.9 steering transfer: EM on the new 41 >= 2%, beats the unsteered untuned model at
    p < 0.05, coherence >= 50%; alpha = 0 means no coherent strength."""
    if alpha == 0:
        return NO_STRENGTH
    if (em >= C.STEER_TRANSFER_EM_MIN and p < C.STEER_TRANSFER_ALPHA
            and coherence >= C.STEER_TRANSFER_COHERENCE_MIN):
        return TRANSFERS
    return NO_TRANSFER


def steer_compare(steered: pd.DataFrame, baseline: pd.DataFrame) -> dict:
    """Per judge: steered and baseline EM rate and coherence, and the exact stratified test
    (steered > baseline), stratified by question."""
    out = {}
    for j in judges_present(steered):
        if j not in judges_present(baseline):
            continue
        s, b = view(steered, j), view(baseline, j)
        out[j] = {"em": em_rate(s), "coherence": coherence_rate(s), "n": len(s),
                  "baseline_em": em_rate(b), "baseline_coherence": coherence_rate(b),
                  "baseline_n": len(b), "cmh": cmh_from_frames(s, b)}
    return out


def analyze_steer(alpha: float, new41: tuple[pd.DataFrame, pd.DataFrame] | None,
                  general: tuple[pd.DataFrame, pd.DataFrame] | None) -> dict:
    """One model: the label from luna on the new 41; the general 8 x 20 descriptive. Each pair is
    (steered, unsteered baseline); None when alpha = 0 (nothing was generated)."""
    if alpha == 0:
        return {"alpha": 0.0, "label": NO_STRENGTH}
    if new41 is None:
        raise Part3Refused("steered new-41 evaluation missing for a non-zero alpha")
    out = {"alpha": alpha, "new41": steer_compare(*new41)}
    if general is not None:
        out["general_descriptive"] = steer_compare(*general)
    h = out["new41"].get(HEADLINE_JUDGE)
    if h is None:
        raise Part3Refused(f"steered new-41 evaluation lacks {HEADLINE_JUDGE} scores")
    out["label"] = steer_label(alpha, h["em"], h["cmh"]["p"], h["coherence"])
    return out
