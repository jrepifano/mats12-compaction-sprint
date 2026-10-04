"""I9 analysis: scores, the primary decision rule, the crossed control, the permutation test, the
§4.5 checks and the §6 outcome classifier (prereg v0.5 §4.4, §4.5, §6; implementation plan §2 U5).

* **Scores.** Per-reply binomial log-likelihood (primary), Spearman with the observed rates k/n,
  AUROC for "misbehaves at least once". Every predictor's predictions are first averaged over the
  fold repeats (``average``), then scored.
* **Bootstrap.** Template groups are resampled with replacement (``group_weights``: a [B, G]
  multinomial count matrix, ``BOOTSTRAP_SEED``); a resample's mean log-likelihood per reply is
  sum_g w_g LL_g / sum_g w_g n_g. The best prompt-only predictor is re-chosen in every resample.
  The bootstrap is conditional on the fitted predictions (no refitting).
* **Decision rule.** Two-sided bootstrap p-value = min(1, 2 x the smaller of the shares of
  differences <= 0 and >= 0). Holm step-down at ``HOLM_ALPHA`` over the two primaries
  (``emx.stats.holm``: strict p < alpha / (m - i + 1)), or a single test when the Oracle Lens gate
  failed. A predictor beats the baseline only if Holm rejects, its point mean difference against
  the best baseline (chosen on the full data) is positive, and its Spearman is higher than that
  baseline's. Intervals: 97.5% for the first Holm step, 95% for the second (and the single test).
* **Crossed control.** G(source, target) = probe LL minus the best prompt-only LL for that target,
  per resample; the claim needs the 95% intervals of both brackets and both own-model gains above
  zero. The interaction is reported but cannot carry the claim.
* **Permutation test.** Template groups of equal size swap whole rate sets, then rates are
  shuffled within each group; the statistic (probe gain over the embedding regression, both
  refitted) is recomputed by a caller-supplied function.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats as sps

from emx.i9 import contracts as C
from emx.stats import holm

EPS = 1e-6


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- scores --------------------------------------------------------------------------------------


def average(p: np.ndarray) -> np.ndarray:
    """[n_repeats, n] -> [n] (a 1-D input is returned unchanged)."""
    p = np.asarray(p, dtype=float)
    return p.mean(axis=0) if p.ndim == 2 else p


def ll_terms(p: np.ndarray, k: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Per-scenario binomial log-likelihood (summed over its replies, no binomial coefficient)."""
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    k, n = np.asarray(k, dtype=float), np.asarray(n, dtype=float)
    return k * np.log(p) + (n - k) * np.log1p(-p)


def mean_ll(p: np.ndarray, k: np.ndarray, n: np.ndarray) -> float:
    return float(ll_terms(average(p), k, n).sum() / np.asarray(n, dtype=float).sum())


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if np.ptp(x) == 0 or np.ptp(y) == 0:
        return float("nan")
    return float(sps.spearmanr(x, y).statistic)


def auroc(p: np.ndarray, k: np.ndarray) -> float:
    """AUROC of p for "misbehaves at least once" (k > 0); ties count one half."""
    y = np.asarray(k) > 0
    p = np.asarray(p, dtype=float)
    pos, neg = p[y], p[~y]
    if not len(pos) or not len(neg):
        return float("nan")
    diff = pos[:, None] - neg[None, :]
    return float(((diff > 0).sum() + 0.5 * (diff == 0).sum()) / diff.size)


def scores(p: np.ndarray, k: np.ndarray, n: np.ndarray) -> dict[str, float]:
    q = average(p)
    rate = np.asarray(k, dtype=float) / np.asarray(n, dtype=float)
    return {"ll": mean_ll(q, k, n), "spearman": spearman(q, rate), "auroc": auroc(q, k)}


# --- group bootstrap -----------------------------------------------------------------------------


def group_index(groups: Sequence[Any]) -> tuple[np.ndarray, int]:
    """(group number per scenario, number of groups), groups numbered in sorted order."""
    names = sorted(set(np.asarray(groups).tolist()))
    pos = {g: i for i, g in enumerate(names)}
    return np.array([pos[g] for g in np.asarray(groups).tolist()]), len(names)


def group_weights(n_groups: int, n_boot: int, seed: int) -> np.ndarray:
    """[n_boot, n_groups]: how often each group is drawn in each resample."""
    rng = np.random.default_rng(seed)
    return rng.multinomial(n_groups, np.full(n_groups, 1.0 / n_groups), size=n_boot).astype(float)


def boot_ll(
    preds: Mapping[str, np.ndarray],
    k: np.ndarray,
    n: np.ndarray,
    gidx: np.ndarray,
    n_groups: int,
    W: np.ndarray,
) -> dict[str, np.ndarray]:
    """{name: [n_boot]} mean per-reply log-likelihood of each predictor in each resample."""
    n_g = np.bincount(gidx, weights=np.asarray(n, dtype=float), minlength=n_groups)
    denom = W @ n_g
    out = {}
    for name, p in preds.items():
        s_g = np.bincount(gidx, weights=ll_terms(average(p), k, n), minlength=n_groups)
        out[name] = (W @ s_g) / denom
    return out


def two_sided_p(d: np.ndarray) -> float:
    d = np.asarray(d, dtype=float)
    return float(min(1.0, 2.0 * min(np.mean(d <= 0), np.mean(d >= 0))))


def interval(d: np.ndarray, level: float) -> tuple[float, float]:
    a = (1.0 - level) / 2.0
    return float(np.quantile(d, a)), float(np.quantile(d, 1.0 - a))


def best_baseline_draws(boot: Mapping[str, np.ndarray], baselines: Sequence[str]) -> np.ndarray:
    """Per resample, the best prompt-only predictor's log-likelihood (re-chosen every time)."""
    return np.max(np.stack([boot[b] for b in baselines]), axis=0)


# --- the primary decision rule -------------------------------------------------------------------


def primary_test(
    preds: Mapping[str, np.ndarray],
    k: np.ndarray,
    n: np.ndarray,
    groups: Sequence[Any],
    *,
    challengers: Sequence[str],
    baselines: Sequence[str],
    family: Sequence[str] | None = None,
    alpha: float = C.HOLM_ALPHA,
    n_boot: int = C.N_BOOTSTRAP,
    seed: int = C.BOOTSTRAP_SEED,
    W: np.ndarray | None = None,
) -> dict[str, Any]:
    """Test each challenger against the best prompt-only predictor. ``family`` is the Holm family
    (default: all challengers); challengers outside it are scored but never "beat"."""
    family = list(challengers if family is None else family)
    preds = {name: average(p) for name, p in preds.items()}
    gidx, G = group_index(groups)
    W = group_weights(G, n_boot, seed) if W is None else W
    boot = boot_ll({c: preds[c] for c in [*challengers, *baselines]}, k, n, gidx, G, W)
    best_draw = best_baseline_draws(boot, baselines)
    sc = {name: scores(preds[name], k, n) for name in [*challengers, *baselines]}
    best = max(baselines, key=lambda b: sc[b]["ll"])  # point estimate's best baseline
    pvals, diffs = {}, {}
    for c in challengers:
        diffs[c] = boot[c] - best_draw
        pvals[c] = two_sided_p(diffs[c])
    hr = holm({c: pvals[c] for c in family}, alpha=alpha) if family else {}
    ranked = sorted(family, key=lambda c: (pvals[c], c))
    level = {c: 1.0 - alpha / (len(family) - i) for i, c in enumerate(ranked)}
    comps = {}
    for c in challengers:
        diff = sc[c]["ll"] - sc[best]["ll"]
        sp_higher = bool(sc[c]["spearman"] > sc[best]["spearman"])
        h = hr.get(c)
        lev = level.get(c, 0.95)
        comps[c] = {
            "diff": diff,
            "p": pvals[c],
            "p_holm": h["p_holm"] if h else None,
            "holm_reject": bool(h["reject"]) if h else False,
            "interval_level": lev,
            "interval": interval(diffs[c], lev),
            "spearman_higher": sp_higher,
            "beats": bool(h and h["reject"] and diff > 0 and sp_higher),
        }
    chosen = np.argmax(np.stack([boot[b] for b in baselines]), axis=0)
    return {
        "scores": sc,
        "best_baseline": best,
        "best_baseline_share": {b: float(np.mean(chosen == i)) for i, b in enumerate(baselines)},
        "family": family,
        "comparisons": comps,
        "n_boot": int(W.shape[0]),
        "n_groups": G,
        "seed": seed,
    }


def decision_from_pvalues(
    pvals: Mapping[str, float],
    diffs: Mapping[str, float],
    spearman_higher: Mapping[str, bool],
    *,
    alpha: float = C.HOLM_ALPHA,
) -> dict[str, bool]:
    """The win rule on precomputed numbers (used by the power simulation): Holm over the keys
    of ``pvals``, then positive point difference and higher Spearman."""
    hr = holm(dict(pvals), alpha=alpha)
    return {c: bool(hr[c]["reject"] and diffs[c] > 0 and spearman_higher[c]) for c in pvals}


# --- crossed control -----------------------------------------------------------------------------

SOURCES = ("qwen", "gemma")


def crossed_from_gains(gains: Mapping[tuple[str, str], np.ndarray], level: float = 0.95) -> dict:
    """The four required quantities and the interaction from G(source, target) draws."""
    g = gains
    q = {
        "bracket_qwen": g[("qwen", "qwen")] - g[("gemma", "qwen")],
        "bracket_gemma": g[("gemma", "gemma")] - g[("qwen", "gemma")],
        "own_qwen": g[("qwen", "qwen")],
        "own_gemma": g[("gemma", "gemma")],
    }
    inter = q["bracket_qwen"] + q["bracket_gemma"]
    iv = {name: interval(d, level) for name, d in q.items()}
    inter_iv = interval(inter, level)
    return {
        "intervals": iv,
        "interaction_interval": inter_iv,
        "interaction_above_zero": bool(inter_iv[0] > 0),
        "required_above_zero": {name: bool(lo > 0) for name, (lo, _) in iv.items()},
        "dissociation": bool(all(lo > 0 for lo, _ in iv.values())),
    }


def crossed_control(
    probes: Mapping[tuple[str, str], np.ndarray],
    baselines: Mapping[str, Mapping[str, np.ndarray]],
    k: Mapping[str, np.ndarray],
    n: Mapping[str, np.ndarray],
    groups: Sequence[Any],
    *,
    n_boot: int = C.N_BOOTSTRAP,
    seed: int = C.BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """``probes[(source, target)]`` and ``baselines[target][name]`` are predictions on the same
    confirmatory scenarios; one group resample is shared by every target (paired)."""
    gidx, G = group_index(groups)
    W = group_weights(G, n_boot, seed)
    gains, point = {}, {}
    for tgt in SOURCES:
        bl = {name: average(p) for name, p in baselines[tgt].items()}
        boot_b = boot_ll(bl, k[tgt], n[tgt], gidx, G, W)
        best = best_baseline_draws(boot_b, list(bl))
        best_pt = max(mean_ll(p, k[tgt], n[tgt]) for p in bl.values())
        for src in SOURCES:
            pr = average(probes[(src, tgt)])
            gains[(src, tgt)] = boot_ll({"x": pr}, k[tgt], n[tgt], gidx, G, W)["x"] - best
            point[f"{src}->{tgt}"] = mean_ll(pr, k[tgt], n[tgt]) - best_pt
    out = crossed_from_gains(gains)
    out["gain_point"] = point
    out["n_boot"] = n_boot
    return out


# --- permutation test ----------------------------------------------------------------------------


def permute_rates(
    k: np.ndarray, n: np.ndarray, groups: Sequence[Any], rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """Groups of equal size swap their whole (k, n) sets; then rates are shuffled within each
    group. Scenario-to-group membership never changes."""
    g = np.asarray(groups)
    k, n = np.asarray(k), np.asarray(n)
    members: dict[Any, np.ndarray] = {x: np.flatnonzero(g == x) for x in sorted(set(g.tolist()))}
    k2, n2 = k.copy(), n.copy()
    by_size: dict[int, list[Any]] = {}
    for name, idx in members.items():
        by_size.setdefault(len(idx), []).append(name)
    for names in by_size.values():
        perm = rng.permutation(len(names))
        for dst, src in zip(names, [names[i] for i in perm], strict=True):
            d, s = members[dst], members[src]
            order = rng.permutation(len(s))
            k2[d], n2[d] = k[s][order], n[s][order]
    return k2, n2


def permutation_test(
    stat_fn: Callable[[np.ndarray, np.ndarray], float],
    k: np.ndarray,
    n: np.ndarray,
    groups: Sequence[Any],
    *,
    n_perm: int = C.N_PERMUTATIONS,
    seed: int = C.PERMUTATION_SEED,
    quantile: float = C.PERMUTATION_QUANTILE,
) -> C.CheckResult:
    """Real statistic against the ``quantile`` of ``n_perm`` group-aware permutations; ``stat_fn``
    refits the whole pipeline on the given counts."""
    real = float(stat_fn(np.asarray(k), np.asarray(n)))
    rng = np.random.default_rng(seed)
    null = np.array([stat_fn(*permute_rates(k, n, groups, rng)) for _ in range(n_perm)])
    q = float(np.quantile(null, quantile))
    return C.CheckResult(
        name="permutation",
        passed=bool(real > q),
        value=real,
        threshold=q,
        n=n_perm,
        details={"null": null.tolist(), "share_null_ge_real": float(np.mean(null >= real)),
                 "seed": seed, "quantile": quantile},
        notes="probe held-out LL gain over the refitted embedding regression",
        utc=now_utc(),
    )


# --- §4.5 checks ---------------------------------------------------------------------------------


def check_sampling_match(
    ours: Mapping[str, tuple[int, int]], published: Mapping[str, float], n_published: int = 8
) -> C.CheckResult:
    """Mean |our rate - published| <= SAMPLING_MAD_MAX and at most SAMPLING_BIG_DIFF_MAX of the
    scenarios differ by more than SAMPLING_BIG_DIFF; Fisher exact per scenario as a diagnostic."""
    ids = sorted(published)
    missing = [i for i in ids if i not in ours]
    if missing:
        raise ValueError(f"no sampled rate for {missing}")
    per = {}
    for i in ids:
        k, n = ours[i]
        kp = round(published[i] * n_published)
        per[i] = {
            "ours": k / n,
            "published": published[i],
            "abs_diff": abs(k / n - published[i]),
            "fisher_p": float(sps.fisher_exact([[k, n - k], [kp, n_published - kp]]).pvalue),
        }
    mad = float(np.mean([v["abs_diff"] for v in per.values()]))
    big = sum(v["abs_diff"] > C.SAMPLING_BIG_DIFF for v in per.values())
    return C.CheckResult(
        name="sampling_match",
        passed=bool(mad <= C.SAMPLING_MAD_MAX and big <= C.SAMPLING_BIG_DIFF_MAX),
        value=mad,
        threshold=C.SAMPLING_MAD_MAX,
        n=len(ids),
        details={"per_scenario": per, "n_big_diff": int(big),
                 "big_diff": C.SAMPLING_BIG_DIFF, "big_diff_max": C.SAMPLING_BIG_DIFF_MAX},
        utc=now_utc(),
    )


def check_spread(k: np.ndarray, n: np.ndarray) -> C.CheckResult:
    r = np.asarray(k, dtype=float) / np.asarray(n, dtype=float)
    frac = float(np.mean((r > C.SPREAD_LOW) & (r < C.SPREAD_HIGH)))
    return C.CheckResult(
        name="spread", passed=bool(frac >= C.SPREAD_MIN_FRAC), value=frac,
        threshold=C.SPREAD_MIN_FRAC, n=len(r),
        details={"low": C.SPREAD_LOW, "high": C.SPREAD_HIGH,
                 "histogram": np.histogram(r, bins=8, range=(0, 1))[0].tolist()},
        utc=now_utc(),
    )


def check_model_difference(k_q, n_q, k_g, n_g) -> C.CheckResult:
    rho = spearman(np.asarray(k_q) / np.asarray(n_q), np.asarray(k_g) / np.asarray(n_g))
    return C.CheckResult(
        name="model_difference", passed=bool(rho < C.MODEL_DIFF_SPEARMAN_MAX), value=rho,
        threshold=C.MODEL_DIFF_SPEARMAN_MAX, n=len(k_q),
        notes="fail: run the crossed control anyway, reported as unable to separate the models",
        utc=now_utc(),
    )


def check_controls(controls: Mapping[str, tuple[int, int]]) -> C.CheckResult:
    per = {i: int(k) for i, (k, _) in sorted(controls.items())}
    worst = max(per.values()) if per else 0
    return C.CheckResult(
        name="controls", passed=bool(per) and worst <= C.CONTROL_MAX_MISBEHAVED,
        value=float(worst), threshold=float(C.CONTROL_MAX_MISBEHAVED), n=len(per),
        details={"k_by_control": per}, utc=now_utc(),
    )


# Label completeness (review r2 finding 4; amendment B, round 2): every confirmatory scenario
# needs all N_SAMPLES replies labelled for each model. Replies the judges leave unresolved after
# the re-judge go to Jacob for adjudication; his labels are final.


def check_label_completeness(
    n_by_model: Mapping[str, np.ndarray],
    ids: Sequence[str],
    *,
    n_samples: int = C.N_SAMPLES,
) -> C.CheckResult:
    """Pass iff, for every model, every confirmatory scenario has n == ``n_samples`` labelled
    replies (the rates' n)."""
    per: dict[str, dict[str, Any]] = {}
    ok = bool(n_by_model) and bool(len(ids))
    for m, n in sorted(n_by_model.items()):
        n = np.asarray(n, dtype=int)
        frac = float(n.sum() / (len(ids) * n_samples)) if len(ids) else 0.0
        short = {sid: int(v) for sid, v in zip(ids, n, strict=True) if v != n_samples}
        per[m] = {"labelled": int(n.sum()), "expected": len(ids) * n_samples, "frac": frac,
                  "not_full": short}
        ok = ok and not short
    return C.CheckResult(
        name="label_completeness", passed=bool(ok),
        value=min((v["frac"] for v in per.values()), default=0.0), threshold=1.0,
        n=len(ids), details={"by_model": per, "n_samples": n_samples},
        notes="pass iff every confirmatory scenario of each model has all n_samples replies "
              "labelled (unresolved replies adjudicated by Jacob)",
        utc=now_utc(),
    )


# --- fail-closed gates read by the final analysis ------------------------------------------------

# Prereg §4.5 blocking checks, as the files under checks/ that their producers write:
# scripts/i9_pod.py smoke (smoke_<name>), scripts/i9_judge.py handlabel-score, and checkpoint2.
BLOCKING_CHECKS: tuple[str, ...] = (
    "smoke_i1a_reproduction",  # same activations as I1a
    "smoke_prompt_scope",  # prompt-only scope
    "sampling_match",
    "handlabel_kappa",  # misbehavior judge
    "spread",
    "controls",
    "label_completeness",
)
# judging.validation_gate's component names, then its overall (the reader-score judge gate)
OLENS_GATE_COMPONENTS: tuple[str, ...] = (
    "validation_wkappa_commit",
    "validation_wkappa_fidelity",
    "validation_same_family",
    "validation_content_commit",
    "validation_same_action",
)
OLENS_GATE_OVERALL = "validation_overall"

ReadCheck = Callable[[str], Mapping[str, Any] | None]  # check name -> its JSON, None if absent


def blocking_status(read: ReadCheck, names: Sequence[str] = BLOCKING_CHECKS
                    ) -> tuple[bool, dict[str, bool | None], list[str]]:
    """(all present and passed, {name: passed or None if absent}, missing names). An absent
    check fails."""
    status: dict[str, bool | None] = {}
    for name in names:
        doc = read(name)
        status[name] = None if doc is None else doc.get("passed") is True
    missing = [k for k, v in status.items() if v is None]
    return all(v is True for v in status.values()), status, missing


def olens_gate_status(read: ReadCheck) -> dict[str, Any]:
    """The Oracle Lens reader-score gate (prereg §4.3, §4.5) from ``validation_overall`` and its
    five components. "pass" needs all six present, all passed, and the overall's recorded
    component verdicts consistent with the component files; anything else is "fail" (the Oracle
    Lens primary becomes exploratory)."""
    overall = read(OLENS_GATE_OVERALL)
    comps = {c: read(c) for c in OLENS_GATE_COMPONENTS}
    missing = [OLENS_GATE_OVERALL] * (overall is None) + [c for c, d in comps.items() if d is None]
    out: dict[str, Any] = {"gate": "fail", "missing": missing,
                           "components": {c: (d or {}).get("passed") for c, d in comps.items()},
                           "overall": None if overall is None else overall.get("passed")}
    if missing:
        out["reason"] = f"absent: {missing}"
        return out
    recorded = (overall.get("details") or {})  # type: ignore[union-attr]
    comp_ok = all(d.get("passed") is True for d in comps.values())  # type: ignore[union-attr]
    consistent = (overall.get("passed") is comp_ok  # type: ignore[union-attr]
                  and set(recorded) == set(OLENS_GATE_COMPONENTS)
                  and all(recorded[c] == out["components"][c] for c in OLENS_GATE_COMPONENTS))
    if not consistent:
        out["reason"] = "validation_overall disagrees with its component checks"
    elif not comp_ok:
        out["reason"] = "a gate component failed"
    else:
        out["gate"], out["reason"] = "pass", "all five components and the overall passed"
    return out


# --- §6 outcome classifier -----------------------------------------------------------------------

# Qualified outcomes (prereg amendment B, sensitivity analyses): the main run's classification
# changed under a sensitivity rerun, so no positive outcome is emitted.
SENSITIVE_LABEL_RULE = "sensitive_to_label_rule"  # LLM-only labels (amendment B item 2)
SENSITIVE_PLACEHOLDER = "sensitive_to_placeholder"  # without the unfilled-placeholder scenario
# Checkpoint-2 power below POWER_MIN (or not computable) for the applicable branch (prereg §4.4:
# Jacob may still run Stage 2 as a descriptive study only): no §6 row is emitted (code review r3
# finding 2).
UNDERPOWERED = "underpowered_descriptive"

OUTCOMES: tuple[str, ...] = (
    "checks_fail",
    UNDERPOWERED,
    "model_specific_signal",
    "edge_without_specificity",
    "probe_only",
    "prompt_gives_it_away",
    "inconclusive",
    SENSITIVE_LABEL_RULE,
    SENSITIVE_PLACEHOLDER,
)


def classify_outcome(
    *,
    checks_passed: bool,
    probe_beats: bool,
    olens_beats: bool,
    dissociation: bool,
    interaction_above_zero: bool,
    power: float | None,
    models_differ: bool,
    other_reader_beats: bool = False,
    power_min: float = C.POWER_MIN,
) -> str:
    """The first matching row of prereg §6's rate-prediction table.

    ``models_differ`` is the §4.5 model-difference check (False when it failed or is absent): the
    crossed control is then unable to separate the models, so it can show neither a dissociation
    nor an interaction, and "model_specific_signal" is never emitted.

    ``power`` is checkpoint 2's power for the applicable branch (Holm, or probe-only when the
    Oracle Lens gate failed). Below ``power_min``, or None (absent or not computable), no §6
    row applies: the study is descriptive only (prereg §4.4), ``UNDERPOWERED``."""
    if not models_differ:
        dissociation = interaction_above_zero = False
    primary = probe_beats or olens_beats
    reader = olens_beats or other_reader_beats
    if not checks_passed:
        return "checks_fail"
    if power is None or power < power_min:
        return UNDERPOWERED
    if primary and dissociation:
        return "model_specific_signal"
    if primary and not interaction_above_zero:
        return "edge_without_specificity"
    if probe_beats and not reader:
        return "probe_only"
    if not (probe_beats or reader) and power is not None and power >= power_min:
        return "prompt_gives_it_away"
    return "inconclusive"


# --- sensitivity analyses (prereg amendment B) ---------------------------------------------------


def claims_of(run: Mapping[str, Any]) -> dict[str, Any]:
    """The claims one analysis run supports: its outcome, which primaries beat the best
    prompt-only predictor, and the crossed-control dissociation (False when the models could not
    be separated)."""
    return {
        "outcome": run["outcome"],
        "primary": {"probe": bool(run["probe_beats"]), "olens": bool(run["olens_beats"])},
        "crossed": bool(run["dissociation"]) and bool(run["models_differ"]),
    }


def sensitivity_verdict(
    main: Mapping[str, Any], variants: Mapping[str, Mapping[str, Any]], label: str
) -> dict[str, Any]:
    """Compare the main run's claims (``claims_of``) with each variant's. A claim that differs
    under any variant is ``label`` ("sensitive_to_..."); the outcome is withheld when the outcome
    or the primary claim differs (the crossed claim is withheld on its own, and the outcome too
    whenever it rests on the crossed claim, since the outcome then differs)."""
    changed: dict[str, list[str]] = {}
    for name, v in variants.items():
        diff = [c for c in ("outcome", "primary", "crossed") if v[c] != main[c]]
        if diff:
            changed[name] = diff
    flipped = {c for d in changed.values() for c in d}
    return {
        "label": label,
        "changed": changed,
        "primary_claim": label if "primary" in flipped else "robust",
        "crossed_claim": label if "crossed" in flipped else "robust",
        "withhold_outcome": bool(flipped & {"outcome", "primary"}),
    }


def qualify_outcome(raw: str, sensitivities: Sequence[Mapping[str, Any]]) -> tuple[str, list[str]]:
    """(reported outcome, reasons). ``raw`` is the main run's §6 row. A sensitivity that
    withholds the outcome replaces it with its label (the first, in the given order; every one
    is listed in the reasons): no positive outcome is emitted for a claim that changed."""
    if raw == "checks_fail":
        return raw, []
    labels = [s["label"] for s in sensitivities if s["withhold_outcome"]]
    if raw == UNDERPOWERED:  # already no positive outcome; the sensitivities are listed only
        return raw, [f"{lab}: a claim changed (descriptive only)" for lab in labels]
    return (labels[0] if labels else raw), [
        f"{lab}: the main run's outcome {raw!r} or its primary claim changed" for lab in labels]


# --- plots ---------------------------------------------------------------------------------------


def plot_predicted_vs_observed(
    preds: Mapping[str, np.ndarray], k: np.ndarray, n: np.ndarray, path: Path, title: str = ""
) -> Path:
    """Small multiples: held-out predicted rate (averaged over repeats) against the observed rate,
    one panel per predictor, with the identity line."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = list(preds)
    cols = min(4, max(1, len(names)))
    rows = (len(names) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(3.2 * cols, 3.2 * rows), squeeze=False)
    obs = np.asarray(k, dtype=float) / np.asarray(n, dtype=float)
    for ax, name in zip(axes.flat, names, strict=False):
        q = average(preds[name])
        ax.plot([0, 1], [0, 1], color="#9a9a9a", lw=1, zorder=1)
        ax.scatter(q, obs, s=22, color="#2a6fdb", edgecolor="white", linewidth=0.8, zorder=2)
        sc = scores(q, k, n)
        ax.set_title(f"{name}\nLL {sc['ll']:.3f}  rho {sc['spearman']:.2f}", fontsize=9)
        ax.set_xlim(0, 1)
        ax.set_ylim(-0.02, 1.02)
        ax.grid(color="#e6e6e6", lw=0.6)
        ax.tick_params(labelsize=8, colors="#555555")
        for s in ax.spines.values():
            s.set_color("#cccccc")
    for ax in list(axes.flat)[len(names):]:
        ax.axis("off")
    fig.supxlabel("predicted rate (held out)", fontsize=9)
    fig.supylabel("observed rate (k / n)", fontsize=9)
    if title:
        fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path
