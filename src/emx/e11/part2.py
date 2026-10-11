"""E11 part 2: the projection-difference and LLM-reader predictors of a dataset's framing effect,
their disagreement gate, its prerequisites and the descriptive readouts (prereg §4.6, §4.7.7 and
the part-2 table of §6).

- Trait vectors (evil, style): difference of the mean response activations under the positive and
  negative instructions, unit-normalized [Chen et al. 2025, §3 / App. A.1]; no trait-score filter
  (prereg §4.6 deviation).
- Projection difference [Chen et al. 2025, §6.1]: ΔP_s(d) = mean over rows of
  [a(s, x, y) − a(s, x, y′_s)] · v̂, with data and base rows paired by (ds, framing, row_id).
- FE(d, h) = score(d, neu, h) − score(d, ino, h) for both predictors (reader: mean SHIFT over
  scored rows).
- Dataset-half drops, ρ_h, the four prerequisites and the gate, verbatim from §4.6.

Keys: `(ds, framing, half)` with half in ("A", "B", "all"); "all" is the full sample over the halves
that were not dropped. `expected` (from the committed pool manifest) maps ds -> {row_id: half}.
numpy / scipy / pandas only; no I/O here (scripts/e11_part2.py reads and writes).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.stats import rankdata

from emx.e11 import contracts as C

FULL = "all"
HALF_KEYS: tuple[str, ...] = (*C.POOL_HALVES, FULL)
KINDS: tuple[str, ...] = ("data", "base")
READER_STATUSES: tuple[str, ...] = ("scored", "missing", "error")
READER_SCALE_COLS: tuple[str, ...] = tuple(s.lower() for s in C.READER_SCALES)
DS_CLASS: dict[str, str] = {d: c for c, ds in C.POOL_CLASSES.items() for d in ds}

Key = tuple[str, str, str]  # (ds, framing, half)


# --- small helpers ------------------------------------------------------------------------------


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if not math.isfinite(n) or n == 0.0:
        raise ValueError("zero or non-finite vector cannot be normalized")
    return v / n


def _f(x) -> float | None:
    """A JSON-safe float (None for missing or non-finite)."""
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman's ρ with average ranks for ties, rounded to 12 decimals; None if n < 3 or either
    side is constant."""
    x, y = np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    if x.shape != y.shape or x.ndim != 1:
        raise ValueError("spearman needs two 1-d sequences of equal length")
    if len(x) < 3 or np.ptp(x) == 0 or np.ptp(y) == 0:
        return None
    rx, ry = rankdata(x, method="average"), rankdata(y, method="average")
    # Rounded so that an exact boundary value (e.g. ρ = 0.6 with 10 datasets) compares exactly
    # against the gate thresholds instead of landing 1 ulp off.
    return round(float(np.corrcoef(rx, ry)[0, 1]), 12)


def _paired_rho(a: Mapping[str, float], b: Mapping[str, float]) -> tuple[float | None, list[str]]:
    ds = sorted(set(a) & set(b))
    return spearman([a[d] for d in ds], [b[d] for d in ds]), ds


def nest(d: Mapping[tuple, object]) -> dict:
    """{(a, b, c): v} -> {a: {b: {c: v}}} for JSON (floats made JSON-safe)."""
    out: dict = {}
    for key, v in sorted(d.items()):
        cur = out
        for k in key[:-1]:
            cur = cur.setdefault(k, {})
        cur[key[-1]] = _f(v) if isinstance(v, (int, float, np.floating)) else v
    return out


# --- trait vectors ------------------------------------------------------------------------------


def _qindex(qid: str) -> int:
    if not (isinstance(qid, str) and qid.startswith("pvq") and qid[3:].isdigit()):
        raise ValueError(f"vector qid {qid!r} is not pvqNN")
    return int(qid[3:])


def trait_vector(acts, index: Sequence[dict], trait: str, *,
                 questions: Iterable[int] | None = None) -> np.ndarray:
    """Unit difference of means: mean(pos responses) − mean(neg responses) over every response of
    `trait` (restricted to the question indices in `questions` when given)."""
    acts = np.asarray(acts, dtype=np.float64)
    if acts.ndim != 2 or acts.shape[0] != len(index):
        raise ValueError(f"vector acts {acts.shape} do not match {len(index)} index rows")
    qs = None if questions is None else set(questions)
    rows: dict[str, list[int]] = {"pos": [], "neg": []}
    for i, r in enumerate(index):
        if r["trait"] != trait:
            continue
        if r["polarity"] not in rows:
            raise ValueError(f"index row {i}: polarity {r['polarity']!r}")
        if qs is not None and _qindex(r["qid"]) not in qs:
            continue
        rows[r["polarity"]].append(i)
    if not rows["pos"] or not rows["neg"]:
        raise ValueError(f"trait {trait!r}: no positive or no negative responses")
    pos, neg = acts[rows["pos"]], acts[rows["neg"]]
    if not (np.isfinite(pos).all() and np.isfinite(neg).all()):
        raise ValueError(f"trait {trait!r}: non-finite response activations")
    return _unit(pos.mean(axis=0) - neg.mean(axis=0))


def split_half_cosine(acts, index: Sequence[dict], trait: str) -> float:
    """Cosine between the vectors built from the two question halves (C.VEC_QUESTION_HALVES:
    question index even vs odd), prereg §4.7.7."""
    a = trait_vector(acts, index, trait, questions=C.VEC_QUESTION_HALVES[0])
    b = trait_vector(acts, index, trait, questions=C.VEC_QUESTION_HALVES[1])
    return float(a @ b)


def orthogonalize(v, u) -> np.ndarray:
    """v with its component along u removed, renormalized: v − (v·û)û."""
    v, u = np.asarray(v, dtype=np.float64), _unit(np.asarray(u, dtype=np.float64))
    return _unit(v - (v @ u) * u)


# --- pool activations ---------------------------------------------------------------------------


def _index_map(pool_index: Sequence[dict]) -> dict[tuple[str, str, str, str], int]:
    """(ds, framing, row_id, kind) -> row number; refuses duplicates and unknown values."""
    out: dict[tuple[str, str, str, str], int] = {}
    for i, r in enumerate(pool_index):
        if r["kind"] not in KINDS:
            raise ValueError(f"pool index row {i}: kind {r['kind']!r}")
        if r["framing"] not in C.FRAMINGS:
            raise ValueError(f"pool index row {i}: framing {r['framing']!r}")
        if r["half"] not in C.POOL_HALVES:
            raise ValueError(f"pool index row {i}: half {r['half']!r}")
        k = (r["ds"], r["framing"], r["row_id"], r["kind"])
        if k in out:
            raise ValueError(f"duplicate pool activation {k}")
        out[k] = i
    return out


def pair_pool(pool_acts, pool_index: Sequence[dict], *,
              exclude: Iterable[tuple[str, str]] = ()) -> tuple[np.ndarray, list[tuple]]:
    """Pair data and base rows by (ds, framing, row_id): returns (a_data − a_base, keys) with keys
    (ds, framing, half, row_id). Rows of excluded (ds, half) are left out. Refuses any data row
    without its base row (or the reverse), a half mismatch or non-finite activations."""
    acts = np.asarray(pool_acts)
    if acts.ndim != 2 or acts.shape[0] != len(pool_index):
        raise ValueError(f"pool acts {acts.shape} do not match {len(pool_index)} index rows")
    ex = set(exclude)
    m = _index_map(pool_index)
    unmatched = []
    for ds, fr, rid, kind in m:
        if (ds, pool_index[m[(ds, fr, rid, kind)]]["half"]) in ex:
            continue
        other = "base" if kind == "data" else "data"
        if (ds, fr, rid, other) not in m:
            unmatched.append((ds, fr, rid, kind))
    if unmatched:
        raise ValueError(f"{len(unmatched)} unmatched data/base pool rows, e.g. {unmatched[:5]}")
    keys, di, bi = [], [], []
    for (ds, fr, rid, kind), i in sorted(m.items()):
        h = pool_index[i]["half"]
        if kind != "data" or (ds, h) in ex:
            continue
        j = m[(ds, fr, rid, "base")]
        if pool_index[j]["half"] != h:
            raise ValueError(f"half mismatch between data and base for {(ds, fr, rid)}")
        keys.append((ds, fr, h, rid))
        di.append(i)
        bi.append(j)
    diffs = acts[di].astype(np.float64) - acts[bi].astype(np.float64)
    if not np.isfinite(diffs).all():
        raise ValueError("non-finite pool activations among the paired rows")
    return diffs, keys


def delta_p(pool_acts, pool_index: Sequence[dict], v, *, exclude: Iterable[tuple[str, str]] = (),
            pairs: tuple[np.ndarray, list[tuple]] | None = None) -> dict[Key, float]:
    """{(ds, framing, half): ΔP} for half A, B and "all" (the rows of the halves not excluded)."""
    diffs, keys = pairs if pairs is not None else pair_pool(pool_acts, pool_index, exclude=exclude)
    proj = diffs @ _unit(np.asarray(v, dtype=np.float64))
    groups: dict[Key, list[float]] = defaultdict(list)
    for (ds, fr, h, _), p in zip(keys, proj, strict=True):
        groups[(ds, fr, h)].append(float(p))
        groups[(ds, fr, FULL)].append(float(p))
    return {k: float(np.mean(vals)) for k, vals in groups.items()}


def activation_failures(pool_acts, pool_index: Sequence[dict],
                        expected: Mapping[str, Mapping[str, str]]) -> dict[tuple[str, str], list[str]]:
    """(ds, half) -> reasons, for halves with any expected activation (data or base, either
    framing) missing or non-finite. Refuses index rows outside the manifest or in the wrong half."""
    acts = np.asarray(pool_acts)
    if acts.ndim != 2 or acts.shape[0] != len(pool_index):
        raise ValueError(f"pool acts {acts.shape} do not match {len(pool_index)} index rows")
    m = _index_map(pool_index)
    for ds, _fr, rid, _kind in m:
        if ds not in expected or rid not in expected[ds]:
            raise ValueError(f"pool activation for {ds}/{rid} is not in the pool manifest")
        if pool_index[m[(ds, _fr, rid, _kind)]]["half"] != expected[ds][rid]:
            raise ValueError(f"pool activation {ds}/{rid}: half differs from the manifest")
    finite = np.isfinite(acts).all(axis=1) if len(acts) else np.ones(0, dtype=bool)
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ds, rows in expected.items():
        for rid, h in rows.items():
            for fr in C.FRAMINGS:
                for kind in KINDS:
                    i = m.get((ds, fr, rid, kind))
                    if i is None:
                        counts[(ds, h)][f"missing {fr}/{kind}"] += 1
                    elif not finite[i]:
                        counts[(ds, h)][f"non-finite {fr}/{kind}"] += 1
    return {k: [f"activations: {n} {what}" for what, n in sorted(v.items())]
            for k, v in sorted(counts.items())}


# --- the reader ---------------------------------------------------------------------------------


def reader_table(reader: pd.DataFrame, expected: Mapping[str, Mapping[str, str]]
                 ) -> tuple[pd.DataFrame, dict[Key, dict]]:
    """Validate the reader CSV against the manifest. Returns (scored rows, missingness), the
    latter keyed (ds, framing, half): {n_expected, n_scored, n_missing, frac_missing}. A row absent
    from the CSV counts as missing; status `missing` and `error` both count as missing."""
    need = ("ds", "row_id", "half", "framing", "status", *READER_SCALE_COLS)
    lacking = [c for c in need if c not in reader.columns]
    if lacking:
        raise ValueError(f"reader CSV lacks columns {lacking}")
    df = reader.copy()
    df["ds"], df["row_id"] = df["ds"].astype(str), df["row_id"].astype(str)
    dup = df.duplicated(["ds", "row_id", "framing"], keep=False)
    if dup.any():
        raise ValueError(f"{int(dup.sum())} duplicate reader rows (ds, row_id, framing)")
    bad = ~df["status"].isin(READER_STATUSES) | ~df["framing"].isin(list(C.FRAMINGS))
    if bad.any():
        raise ValueError(f"reader rows with unknown status or framing: {df[bad].head(3)}")
    for r in df.itertuples(index=False):
        if r.ds not in expected or r.row_id not in expected[r.ds]:
            raise ValueError(f"reader row {r.ds}/{r.row_id} is not in the pool manifest")
        if r.half != expected[r.ds][r.row_id]:
            raise ValueError(f"reader row {r.ds}/{r.row_id}: half differs from the manifest")
    scored = df[df["status"] == "scored"].copy()
    for c in READER_SCALE_COLS:
        scored[c] = pd.to_numeric(scored[c], errors="coerce")
        if not scored[c].between(0, 100).all():
            raise ValueError(f"scored reader rows with {c} missing or outside 0-100")
    n_scored = scored.groupby(["ds", "framing", "half"]).size().to_dict()
    n_exp: dict[tuple[str, str], int] = defaultdict(int)
    for ds, rows in expected.items():
        for h in rows.values():
            n_exp[(ds, h)] += 1
    miss: dict[Key, dict] = {}
    for (ds, h), n in sorted(n_exp.items()):
        for fr in C.FRAMINGS:
            s = int(n_scored.get((ds, fr, h), 0))
            miss[(ds, fr, h)] = {"n_expected": n, "n_scored": s, "n_missing": n - s,
                                 "frac_missing": (n - s) / n if n else 1.0}
    return scored, miss


def reader_scores(scored: pd.DataFrame, scale: str = "shift", *,
                  exclude: Iterable[tuple[str, str]] = ()) -> dict[Key, float]:
    """Mean of a reader scale over scored rows, per (ds, framing, half) and "all" (the halves not
    excluded)."""
    ex = set(exclude)
    keep = [(d, h) not in ex for d, h in zip(scored["ds"], scored["half"], strict=True)]
    s = scored[keep]
    out: dict[Key, float] = {}
    for (ds, fr, h), v in s.groupby(["ds", "framing", "half"])[scale].mean().items():
        out[(ds, fr, h)] = float(v)
    for (ds, fr), v in s.groupby(["ds", "framing"])[scale].mean().items():
        out[(ds, fr, FULL)] = float(v)
    return out


def dataset_half_drops(missing: Mapping[Key, dict],
                       act_failures: Mapping[tuple[str, str], list[str]]
                       ) -> dict[tuple[str, str], list[str]]:
    """(ds, half) -> reasons: > C.READER_MISSING_MAX of the reader ratings missing in either
    framing, or any activation missing or failed (prereg §4.6 "Data handling before ρ")."""
    out: dict[tuple[str, str], list[str]] = defaultdict(list)
    for (ds, fr, h), m in sorted(missing.items()):
        if m["frac_missing"] > C.READER_MISSING_MAX:
            out[(ds, h)].append(f"reader: {m['n_missing']}/{m['n_expected']} missing ({fr})")
    for k, reasons in act_failures.items():
        out[k].extend(reasons)
    return dict(sorted(out.items()))


# --- framing effects and the gate ---------------------------------------------------------------


def framing_effect(scores: Mapping[Key, float]) -> dict[tuple[str, str], float]:
    """(ds, half) -> score(neu) − score(ino), where both framings have a score."""
    out = {}
    for (ds, fr, h), v in scores.items():
        if fr == "neu" and (ds, "ino", h) in scores:
            out[(ds, h)] = v - scores[(ds, "ino", h)]
    return out


def _by_half(fe: Mapping[tuple[str, str], float], h: str) -> dict[str, float]:
    return {ds: v for (ds, hh), v in fe.items() if hh == h}


def known_groups(neutral: Mapping[str, float]) -> dict:
    """Every remaining pair of C.KNOWN_GROUPS ordered first > second on the full-sample neutral
    score, with at least C.POOL_MIN_KNOWN_PAIRS pairs remaining (else inconclusive)."""
    pairs, dropped = [], []
    for a, b in C.KNOWN_GROUPS:
        if a in neutral and b in neutral:
            pairs.append({"first": a, "second": b, "first_score": _f(neutral[a]),
                          "second_score": _f(neutral[b]), "ordered": bool(neutral[a] > neutral[b])})
        else:
            dropped.append([a, b])
    if len(pairs) < C.POOL_MIN_KNOWN_PAIRS:
        status = "inconclusive"
    else:
        status = "pass" if all(p["ordered"] for p in pairs) else "fail"
    return {"status": status, "pass": status == "pass", "pairs": pairs, "pairs_dropped": dropped,
            "n_remaining": len(pairs), "min_pairs": C.POOL_MIN_KNOWN_PAIRS}


def rho_half(fe_reader: Mapping[str, float], fe_proj: Mapping[str, float]) -> dict:
    """ρ_h across the datasets remaining in the half; undefined if < C.POOL_MIN_DATASETS remain or
    either FE is constant."""
    ds = sorted(set(fe_reader) & set(fe_proj))
    x, y = [fe_reader[d] for d in ds], [fe_proj[d] for d in ds]
    reason = None
    if len(ds) < C.POOL_MIN_DATASETS:
        reason = f"{len(ds)} datasets remain (< {C.POOL_MIN_DATASETS})"
    elif np.ptp(x) == 0 or np.ptp(y) == 0:
        reason = "constant FE: " + ", ".join(
            n for n, z in (("reader", x), ("projection", y)) if np.ptp(z) == 0)
    rho = None if reason else spearman(x, y)
    return {"rho": rho, "defined": rho is not None, "undefined_reason": reason,
            "n_datasets": len(ds), "datasets": ds}


def reliability(fe: Mapping[tuple[str, str], float]) -> dict:
    """Split-half reliability: Spearman(FE(·, A), FE(·, B)) over datasets with both halves left."""
    a, b = _by_half(fe, "A"), _by_half(fe, "B")
    rho, ds = _paired_rho(a, b)
    ok = rho is not None and rho >= C.RELIABILITY_MIN
    return {"rho": rho, "pass": ok, "min": C.RELIABILITY_MIN, "n_datasets": len(ds),
            "undefined": rho is None}


def gate(reader: pd.DataFrame, delta: Mapping[Key, float], style_delta: Mapping[Key, float], *,
         expected: Mapping[str, Mapping[str, str]], drops: Mapping[tuple[str, str], list[str]],
         split_cosine: float, n_manifest_datasets: int, part2_run: bool = True) -> dict:
    """The part-2 gate (prereg §4.6, §6). `delta` / `style_delta` are ΔP for the evil and style
    vectors computed with `drops` excluded; `reader` is the raw reader CSV."""
    if not part2_run:
        return {"decision": "not run", "failed_prerequisites": [], "prerequisites": {}}
    dropped = set(drops)
    leak = sorted({(ds, h) for ds, _, h in (*delta, *style_delta) if (ds, h) in dropped})
    if leak:
        raise ValueError(f"ΔP computed on dropped dataset-halves {leak}")
    scored, _ = reader_table(reader, expected)
    shift = reader_scores(scored, "shift", exclude=dropped)
    fe = {"reader": framing_effect(shift), "projection": framing_effect(delta),
          "style": framing_effect(style_delta)}
    # Drops apply to both predictors: keep only (ds, half) cells present in both.
    both = set(fe["reader"]) & set(fe["projection"])
    fe = {k: {c: v for c, v in d.items() if c in both} for k, d in fe.items()}
    rho = {h: rho_half(_by_half(fe["reader"], h), _by_half(fe["projection"], h))
           for h in C.POOL_HALVES}

    neutral = {
        "reader": {ds: v for (ds, fr, h), v in shift.items() if fr == "neu" and h == FULL},
        "projection": {ds: v for (ds, fr, h), v in delta.items() if fr == "neu" and h == FULL},
    }
    kg = {p: known_groups(neutral[p]) for p in neutral}
    style_dom = {}
    for h in C.POOL_HALVES:
        r, ds = _paired_rho(_by_half(fe["style"], h), _by_half(fe["projection"], h))
        style_dom[h] = {"rho": r, "n_datasets": len(ds),
                        "dominated": r is not None and abs(r) >= C.STYLE_DOMINANCE_ABS}
    rel = {p: reliability(fe[p]) for p in ("reader", "projection")}
    cos_ok = bool(split_cosine >= C.SPLIT_COSINE_MIN)
    pre = {
        "pool_size": {"pass": n_manifest_datasets >= C.POOL_MIN_DATASETS,
                      "n_datasets": n_manifest_datasets, "min": C.POOL_MIN_DATASETS},
        "validity_projection": {"pass": cos_ok and kg["projection"]["pass"],
                                "split_half_cosine": _f(split_cosine),
                                "split_half_cosine_pass": cos_ok,
                                "cosine_min": C.SPLIT_COSINE_MIN,
                                "known_groups": kg["projection"]},
        "validity_reader": {"pass": kg["reader"]["pass"], "known_groups": kg["reader"]},
        "reliability_reader": rel["reader"],
        "reliability_projection": rel["projection"],
        "rho_defined": {"pass": all(r["defined"] for r in rho.values())},
        "not_style_dominated": {"pass": not all(s["dominated"] for s in style_dom.values()),
                                "halves": style_dom, "abs_max": C.STYLE_DOMINANCE_ABS},
    }
    failed = [k for k, v in pre.items() if not v["pass"]]
    ra, rb = rho["A"]["rho"], rho["B"]["rho"]
    if failed:
        decision = "inconclusive (prerequisite)"
    elif ra <= C.DISAGREE_MAX and rb <= C.DISAGREE_MAX:
        decision = "disagree"
    elif ra >= C.AGREE_MIN and rb >= C.AGREE_MIN:
        decision = "agree"
    else:
        decision = "inconclusive"
    return {
        "decision": decision, "failed_prerequisites": failed, "prerequisites": pre, "rho": rho,
        "fe": {k: nest({(ds, h): v for (ds, h), v in d.items()}) for k, d in fe.items()},
        "thresholds": {"disagree_max": C.DISAGREE_MAX, "agree_min": C.AGREE_MIN},
        "_fe": fe, "_neutral": neutral,
    }


# --- descriptive readouts -----------------------------------------------------------------------


def _ranking(scores: Mapping[str, float]) -> list[dict]:
    return [{"ds": d, "score": _f(scores[d])}
            for d in sorted(scores, key=lambda d: (-scores[d], d))]


def within_class(scores: Mapping[str, float]) -> dict[str, list[dict]]:
    return {c: _ranking({d: scores[d] for d in ds if d in scores})
            for c, ds in C.POOL_CLASSES.items()}


def _ttr(text: str) -> float | None:
    toks = text.lower().split()
    return len(set(toks)) / len(toks) if toks else None


def diagnostics(data_answers: Mapping[str, Sequence[str]] | None,
                base_answers: Mapping[tuple[str, str], Sequence[str]] | None,
                level: Mapping[str, Mapping[str, float]],
                fe: Mapping[str, Mapping[str, float]]) -> dict:
    """Per dataset: mean answer tokens and mean per-answer type-token ratio of the dataset's
    answers (tokens = whitespace split, lower-cased for the TTR), and the base model's mean answer
    length (whitespace tokens) under each framing and its change (neu − ino). Then each one's
    Spearman with each predictor's dataset-level neutral score and full-sample FE. Never gating."""
    per: dict[str, dict] = {}
    for ds in C.POOL_DATASETS:
        rec: dict = {}
        ans = (data_answers or {}).get(ds)
        if ans:
            rec["mean_answer_tokens"] = float(np.mean([len(a.split()) for a in ans]))
            ttrs = [t for t in (_ttr(a) for a in ans) if t is not None]
            rec["type_token_ratio"] = float(np.mean(ttrs)) if ttrs else None
            rec["n_answers"] = len(ans)
        lens = {}
        for fr in C.FRAMINGS:
            b = (base_answers or {}).get((ds, fr))
            if b:
                lens[fr] = float(np.mean([len(a.split()) for a in b]))
                rec[f"base_tokens_{fr}"] = lens[fr]
        if len(lens) == len(C.FRAMINGS):
            rec["base_length_change"] = lens["neu"] - lens["ino"]
        if rec:
            per[ds] = rec
    corr: dict = {}
    for metric in ("mean_answer_tokens", "type_token_ratio", "base_length_change"):
        m = {ds: r[metric] for ds, r in per.items() if r.get(metric) is not None}
        corr[metric] = {
            p: {"score": _paired_rho(m, level[p])[0], "fe": _paired_rho(m, fe[p])[0]}
            for p in level
        }
    return {"tokenizer": "whitespace split (str.split); TTR lower-cased, mean over answers",
            "per_dataset": per, "spearman": corr}


# --- the pool manifest --------------------------------------------------------------------------


def _entry_rows(ds: str, e: Mapping) -> dict[str, str]:
    if "rows" in e:
        rows = {str(r["row_id"]): r["half"] for r in e["rows"]}
    elif "row_ids" in e and "halves" in e and not isinstance(e["halves"], Mapping):
        rows = {str(r): h for r, h in zip(e["row_ids"], e["halves"], strict=True)}
    elif isinstance(e.get("halves"), Mapping):
        rows = {str(r): h for h, ids in e["halves"].items() for r in ids}
    else:
        raise ValueError(f"pool manifest entry for {ds}: no rows / row_ids+halves / halves map")
    bad = {h for h in rows.values() if h not in C.POOL_HALVES}
    if bad:
        raise ValueError(f"pool manifest {ds}: halves {sorted(bad)}")
    return rows


def expected_from_manifest(manifest: Mapping) -> tuple[dict[str, dict[str, str]], list[str]]:
    """(ds -> {row_id: half}, dropped datasets) from the committed pool manifest. Accepts
    `datasets` as a dict keyed by dataset or a list of entries naming it (`ds`/`name`), each with
    `rows: [{row_id, half}]`, `row_ids` + `halves` lists, or `halves: {A: [...], B: [...]}`. An
    entry marked `dropped: true` (or status "dropped") and names in a top-level `dropped` are
    excluded."""
    ds_field = manifest.get("datasets")
    if isinstance(ds_field, Mapping):
        entries = list(ds_field.items())
    elif isinstance(ds_field, list):
        entries = [(e.get("ds") or e.get("name") or e.get("dataset"), e) for e in ds_field]
    else:
        raise ValueError("pool manifest has no `datasets` dict or list")  # noqa: TRY004
    top = manifest.get("dropped") or []
    dropped = {d if isinstance(d, str) else d.get("ds") or d.get("name") for d in top} \
        if not isinstance(top, Mapping) else set(top)
    expected: dict[str, dict[str, str]] = {}
    for ds, e in entries:
        if ds not in C.POOL_DATASETS:
            raise ValueError(f"pool manifest dataset {ds!r} is not in the frozen pool")
        if ds in dropped or e.get("dropped") is True or e.get("status") == "dropped":
            dropped.add(ds)
            continue
        expected[ds] = _entry_rows(ds, e)
    return expected, sorted(d for d in dropped if d)


# --- the whole analysis -------------------------------------------------------------------------


def analyze(vec_acts, vec_index: Sequence[dict], pool_acts, pool_index: Sequence[dict],
            reader: pd.DataFrame, expected: Mapping[str, Mapping[str, str]], *,
            n_manifest_datasets: int | None = None, part2_run: bool = True,
            data_answers: Mapping[str, Sequence[str]] | None = None,
            base_answers: Mapping[tuple[str, str], Sequence[str]] | None = None) -> dict:
    """Every part-2 number (the JSON body of analysis/part2.json, without provenance)."""
    if not part2_run:
        return {"decision": "not run", "gate": gate(reader, {}, {}, expected=expected, drops={},
                                                     split_cosine=float("nan"),
                                                     n_manifest_datasets=0, part2_run=False)}
    n_manifest = len(expected) if n_manifest_datasets is None else n_manifest_datasets
    v_evil = trait_vector(vec_acts, vec_index, "evil")
    v_style = trait_vector(vec_acts, vec_index, "style")
    v_orth = orthogonalize(v_evil, v_style)
    cos = {t: split_half_cosine(vec_acts, vec_index, t) for t in C.VEC_TRAITS}
    n_vec = {t: {p: sum(1 for r in vec_index if r["trait"] == t and r["polarity"] == p)
                 for p in ("pos", "neg")} for t in C.VEC_TRAITS}

    scored, missing = reader_table(reader, expected)
    act_fail = activation_failures(pool_acts, pool_index, expected)
    drops = dataset_half_drops(missing, act_fail)
    pairs = pair_pool(pool_acts, pool_index, exclude=drops)
    dp = {"evil": delta_p(None, pool_index, v_evil, pairs=pairs),
          "style": delta_p(None, pool_index, v_style, pairs=pairs),
          "evil_orth_style": delta_p(None, pool_index, v_orth, pairs=pairs)}
    g = gate(reader, dp["evil"], dp["style"], expected=expected, drops=drops,
             split_cosine=cos["evil"], n_manifest_datasets=n_manifest)
    fe, neutral = g.pop("_fe"), g.pop("_neutral")
    keep = set(fe["reader"])
    fe_orth = {k: v for k, v in framing_effect(dp["evil_orth_style"]).items() if k in keep}

    rdr = {s: reader_scores(scored, s, exclude=drops) for s in READER_SCALE_COLS}
    full = {p: {ds: v for (ds, h), v in fe[p].items() if h == FULL} for p in fe}
    full_orth = {ds: v for (ds, h), v in fe_orth.items() if h == FULL}
    rho_full, ds_full = _paired_rho(full["reader"], full["projection"])
    rho_neu, _ = _paired_rho(neutral["reader"], neutral["projection"])
    orth_rho = {h: _paired_rho(_by_half(fe["reader"], h), _by_half(fe_orth, h))[0]
                for h in HALF_KEYS}
    style_rho = {h: _paired_rho(_by_half(fe["style"], h), _by_half(fe["projection"], h))[0]
                 for h in HALF_KEYS}

    return {
        "decision": g["decision"],
        "failed_prerequisites": g["failed_prerequisites"],
        "rho_A": g["rho"]["A"]["rho"], "rho_B": g["rho"]["B"]["rho"],
        "gate": g,
        "vectors": {
            "layer": C.ACT_LAYER, "n_responses": n_vec,
            "split_half_cosine": {t: _f(c) for t, c in cos.items()},
            "cos_evil_style": _f(v_evil @ v_style),
            "evil_orth_style_cos_with_evil": _f(v_orth @ v_evil),
        },
        "pool": {"n_manifest_datasets": n_manifest, "datasets": sorted(expected),
                 "n_rows": {ds: len(r) for ds, r in sorted(expected.items())}},
        "drops": [{"ds": ds, "half": h, "reasons": r} for (ds, h), r in drops.items()],
        "reader_missing": nest({k: v["frac_missing"] for k, v in missing.items()}),
        "reader_missing_counts": {f"{ds}/{fr}/{h}": v for (ds, fr, h), v in missing.items()},
        "delta_p": {k: nest(v) for k, v in dp.items()},
        "reader_means": {s: nest(v) for s, v in rdr.items()},
        "fe_evil_orth_style": nest(fe_orth),
        "descriptive": {
            "rho_full_sample": rho_full, "n_datasets_full_sample": len(ds_full),
            "rho_neutral_level": rho_neu,
            "rho_reader_vs_evil_orth_style": orth_rho,
            "rho_style_vs_evil_fe": style_rho,
            "neutral_ranking": {p: _ranking(s) for p, s in neutral.items()},
            "fe_ranking_full": {p: _ranking(full[p]) for p in ("reader", "projection")},
            "within_class_fe_full": {p: within_class(full[p]) for p in ("reader", "projection")},
            "within_class_neutral": {p: within_class(neutral[p]) for p in neutral},
            "fe_full_evil_orth_style": {d: _f(v) for d, v in sorted(full_orth.items())},
            "note": "within-class orderings are descriptive; no within-class claim (prereg §4.6)",
        },
        "diagnostics": diagnostics(data_answers, base_answers, neutral,
                                   {p: full[p] for p in ("reader", "projection")}),
    }
