"""The two document groups for Part 2: classifier, frozen boundary, assignment and the semantic
validation (prereg §4.9, Appendix A.5).

- **Classifier.** gpt-6-luna (`ROLES["classifier"]`) scores each final insert document 0-10 with
  prompt A.5 (0 = direct endorsement, 10 = narrative identification), JSON mode, request seed
  `JUDGE_REQUEST_SEED`. A document with a malformed, refused or empty answer is retried with
  seed + 1, then + 2; one still unscored is excluded from the insert and REPLACED through the
  normal acceptance loop (unit B2b), and the replacement is then classified (Amendment F, Jacob,
  2026-10-07). `classify_insert` returns those ids; `scripts/e9_groups.py classify` registers
  them (`emx.e9.checks.add_exclusions`, reason "a5_unscored") and exits 4 without writing
  groups.json, which is written only once every insert document is classified. An API error (no
  terminal answer) raises `UnclassifiedError`: rerun.
- **Sort key** (score, h) with
  `h = int(hashlib.sha256((contracts.GROUP_SALT + doc_id).encode("utf-8")).hexdigest(), 16)`,
  i.e. sha256 of the UTF-8 bytes of the string "E9-groups" immediately followed by the document
  id, no separator, read as a big-endian integer.
- **Boundary.** Documents sorted by key; the boundary key (s*, h*) is the key of the document
  whose cumulative single-copy original tokens (`n_tokens`) first reach half the total
  (2 x cumulative >= total). Group A = key <= (s*, h*) (the boundary document is in A); group B
  = the rest. `assign_group(score, doc_id, boundary)` applies the same rule to any new (P2)
  document, so the boundary reproduces exactly.
- **Validation** (rules frozen). `GROUP_VAL_N` (120) insert documents drawn at random
  (`VALIDATION_SEED`, from the doc-id-sorted list) excluding boundary-score documents (luna score
  == s*), scored by `ROLES["classifier_panel"]` (glm-5.3-flash, gemini-3.5-flash-lite) with A.5;
  no verdict is computed until every panel request has a terminal answer (R1 #8).
  Pass iff all three hold: Spearman(luna, panel mean) >= 0.6; B's mean panel score minus A's is
  > 0 with a 95% percentile bootstrap interval (documents resampled with replacement within
  each group, `BOOTSTRAP_SEED` 0, `BOOTSTRAP_RESAMPLES` 10,000) excluding 0; and a strict
  majority of A's sampled documents have panel mean < 5 and of B's > 5 (denominator: every
  sampled document of the group; one the panel failed to score counts against). A document
  needs both panel models' valid answers for a panel mean.
- **Missing assessments** (execution rule under Amendment F, main session 2026-10-07): the
  verdict is computed only if at least 90% of the sampled documents (108 of 120) have valid
  scores from both panel models; otherwise `outcome` "stop", `stop_reason` "measurement
  invalid: too few valid assessments", never a pass (`valid_counts` records the counts).
- **Evidence binding** (R1 r2 #2). groups.json carries the classification manifest (per document:
  the seed of its ok row, its fingerprint and row sha256) and the validation carries a manifest
  of its sampled requests; `verify_groups` recomputes scores, boundary, assignment, manifests and
  the verdict from the CURRENT insert texts, config and rows before any later paid step
  (`validate`, `assign`) or rewrite of groups.json.
- **Labels.** Relative (`GROUP_LABELS_RELATIVE`) unless the validation passes; then the semantic
  names (`GROUP_LABELS_SEMANTIC`) apply to the non-boundary documents only, whole-group claims
  keep the relative labels, and the number of boundary-score documents per group is stated.
- **Report.** Each group's document count, mean score, token total, documents within
  `GROUP_NEAR_BOUNDARY` points of s*, boundary-score documents, and 10 random documents per group
  (`RAW_SAMPLE_SEED`).

Output: `data/E9/groups/groups.json` (`contracts.GROUP_KEYS` plus report, inputs, prompt sha256,
model ids, provenance) and `groups.sha256` beside it.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable, Sequence
from pathlib import Path

from emx.e9 import contracts as C
from emx.e9 import llm, prompts
from emx.e9.select import (
    MEASUREMENT_INVALID,
    MIN_VALID_FRACTION,
    EvidenceMismatch,
    IncompleteBatch,
    file_sha256,
    manifest_sha256,
    provenance,
    require_terminal,
    spearman,
    write_json,
)

PROMPT_ID = "A5_classify"
STAGE = "setup"
STEP = "groups"
GROUPS_JSON = f"{C.GROUPS_DIR}/groups.json"
RAW_N = 10
EXCERPT_CHARS = 600


class UnclassifiedError(RuntimeError):
    """Some insert documents have no valid classifier score."""


def group_hash(doc_id: str) -> int:
    return int(hashlib.sha256((C.GROUP_SALT + doc_id).encode("utf-8")).hexdigest(), 16)


def sort_key(score: int, doc_id: str) -> tuple[int, int]:
    return (int(score), group_hash(doc_id))


def _boundary_key(boundary) -> tuple[int, int]:
    if isinstance(boundary, dict):
        return (int(boundary["score"]), int(boundary["h_hex"], 16))
    s, h = boundary
    return (int(s), int(h))


def assign_group(score: int, doc_id: str, boundary) -> str:
    """"A" if (score, h(doc_id)) <= the boundary key, else "B". `boundary` is the frozen
    `groups.json["boundary"]` dict or an (s*, h*) tuple."""
    if score is None:
        raise ValueError(f"{doc_id}: no classifier score")
    return "A" if sort_key(score, doc_id) <= _boundary_key(boundary) else "B"


def compute_boundary(scores: dict[str, int], n_tokens: dict[str, int]) -> dict:
    """The boundary key: the key of the document crossing half the single-copy tokens."""
    if not scores:
        raise ValueError("no scored documents")
    order = sorted(scores, key=lambda d: sort_key(scores[d], d))
    total = sum(int(n_tokens[d]) for d in order)
    cum = 0
    for i, d in enumerate(order):
        cum += int(n_tokens[d])
        if 2 * cum >= total:
            h = group_hash(d)
            return {"score": int(scores[d]), "h_hex": f"{h:064x}", "doc_id": d, "position": i,
                    "cumulative_tokens": cum, "total_tokens": total,
                    "rule": "group A iff (score, h) <= (score*, h*); h = int(sha256(GROUP_SALT "
                            "+ doc_id), 16), GROUP_SALT = " + repr(C.GROUP_SALT)}
    raise AssertionError("unreachable")


def assign_all(scores: dict[str, int], boundary: dict) -> dict[str, str]:
    return {d: assign_group(s, d, boundary) for d, s in sorted(scores.items())}


# --- classification ------------------------------------------------------------------------------


def llm_path(model_key: str, llm_dir: str | Path = C.LLM_DIR) -> Path:
    return Path(llm_dir) / f"{PROMPT_ID}.{model_key}.jsonl"


CLASSIFY_SEEDS: tuple[int, ...] = (C.JUDGE_REQUEST_SEED, C.JUDGE_REQUEST_SEED + 1,
                                   C.JUDGE_REQUEST_SEED + 2)


def requests_for(docs: Sequence[dict], seed: int = C.JUDGE_REQUEST_SEED) -> list[dict]:
    return [{"item_id": d["doc_id"], "fields": {"document": d["text"]},
             "seed": seed, "ref_chars": None} for d in docs]


def project_usd(docs: Sequence[dict], model_key: str) -> float:
    return llm.estimate_usd(model_key, PROMPT_ID, requests_for(docs), "json")


def classify(docs: Sequence[dict], model_key: str, batch: str, *,
             llm_dir: str | Path = C.LLM_DIR, client=None, reserve=None,
             concurrency: int = 32, seed: int = C.JUDGE_REQUEST_SEED) -> dict[str, dict]:
    """{doc_id: llm row} for A.5 by `model_key` under one reservation (E9:setup:groups:<batch>)."""
    rows = llm.run_batch(model_key, PROMPT_ID, requests_for(docs, seed),
                         llm_path(model_key, llm_dir), mode="json", stage=STAGE,
                         note=C.ledger_note(STAGE, STEP, batch), concurrency=concurrency,
                         client=client, reserve=reserve)
    return {r["item_id"]: r for r in rows}


def classify_insert(docs: Sequence[dict], model_key: str, batch: str, *,
                    llm_dir: str | Path = C.LLM_DIR, client=None, reserve=None,
                    concurrency: int = 32) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Classify the insert with the reseed rule (Amendment F): a document with a terminal non-ok
    answer (malformed, refused, empty) is retried with request seed JUDGE_REQUEST_SEED + 1, then
    + 2 (batches `<batch>-seed1`, `<batch>-seed2`). A non-terminal answer (an API error or a batch
    cut short) raises `UnclassifiedError`: rerun.

    Returns ({doc_id: its ok row}, {doc_id: [reasons]} for the documents still unscored, which
    must be excluded and replaced through the acceptance loop)."""
    final: dict[str, dict] = {}
    reasons: dict[str, list[str]] = {}
    todo = list(docs)
    for k, seed in enumerate(CLASSIFY_SEEDS):
        if not todo:
            break
        rows = classify(todo, model_key, batch if k == 0 else f"{batch}-seed{k}",
                        llm_dir=llm_dir, client=client, reserve=reserve,
                        concurrency=concurrency, seed=seed)
        errors = [d for d, r in rows.items() if r["status"] not in llm.TERMINAL]
        if errors:
            raise UnclassifiedError(f"{len(errors)} documents got no terminal A.5 answer at seed "
                                    f"{seed} (API errors; rerun): {errors[:5]}")
        nxt = []
        for d in todo:
            r = rows[d["doc_id"]]
            if r["status"] == "ok":
                final[d["doc_id"]] = r
            else:
                reasons.setdefault(d["doc_id"], []).append(
                    f"seed {seed}: {r['status']}" + (f" ({r['error']})" if r.get("error") else ""))
                nxt.append(d)
        todo = nxt
    return final, {d["doc_id"]: reasons[d["doc_id"]] for d in todo}


def classification_from_disk(docs: Sequence[dict], model_key: str = C.ROLES["classifier"],
                             llm_dir: str | Path = C.LLM_DIR) -> tuple[dict[str, dict], list]:
    """Re-derive each insert document's classifier row from disk, with fingerprints recomputed
    from the CURRENT text and config (`llm.verified_rows`): the first seed of `CLASSIFY_SEEDS`
    with an ok row, every earlier seed having a terminal non-ok row. Returns ({doc_id: ok row},
    manifest entries {doc_id, seed, fingerprint, row_sha256}); a document with no such row raises
    `UnclassifiedError`."""
    path = llm_path(model_key, llm_dir)
    by_seed = {seed: llm.verified_rows(model_key, PROMPT_ID, requests_for(docs, seed), path)
               for seed in CLASSIFY_SEEDS}
    rows, entries, missing = {}, [], []
    for d in docs:
        for seed in CLASSIFY_SEEDS:
            r = by_seed[seed].get((d["doc_id"], seed))
            if r is None or r["status"] not in llm.TERMINAL:
                missing.append(d["doc_id"])
                break
            if r["status"] == "ok":
                rows[d["doc_id"]] = r
                entries.append({"doc_id": d["doc_id"], "seed": seed,
                                "fingerprint": r["fingerprint"],
                                "row_sha256": llm.row_sha256(r)})
                break
        else:
            missing.append(d["doc_id"])
    if missing:
        raise UnclassifiedError(f"{len(missing)} insert documents have no verified A.5 score on "
                                f"disk: {missing[:5]}")
    return rows, entries


def groups_from_disk(docs: Sequence[dict], llm_dir: str | Path = C.LLM_DIR) -> dict:
    """`build_groups` from verified rows, with the classification manifest and its sha256."""
    rows, entries = classification_from_disk(docs, llm_dir=llm_dir)
    return {**build_groups(docs, rows), "classification_manifest": entries,
            "classification_manifest_sha256": manifest_sha256(entries)}


def valid_scores(rows: dict[str, dict]) -> tuple[dict[str, int], list[str]]:
    """({doc_id: mode score} for ok rows, [doc ids without a valid score])."""
    ok = {d: r["parsed"]["mode"] for d, r in rows.items() if r["status"] == "ok"}
    return ok, sorted(d for d in rows if d not in ok)


def build_groups(docs: Sequence[dict], rows: dict[str, dict]) -> dict:
    """Scores, boundary and assignment for the insert. Every document needs a valid score
    (`UnclassifiedError` otherwise: an unscored one is replaced first, Amendment F)."""
    scores, missing = valid_scores(rows)
    missing = sorted(set(missing) | {d["doc_id"] for d in docs if d["doc_id"] not in rows})
    if missing:
        raise UnclassifiedError(f"{len(missing)} insert documents lack a valid A.5 score: "
                                f"{missing[:5]}")
    scores = {d["doc_id"]: scores[d["doc_id"]] for d in docs}
    n_tok = {d["doc_id"]: int(d["n_tokens"]) for d in docs}
    b = compute_boundary(scores, n_tok)
    return {"scores": scores, "boundary": b, "assignment": assign_all(scores, b)}


# --- validation ----------------------------------------------------------------------------------


def validation_sample(scores: dict[str, int], s_star: int, n: int = C.GROUP_VAL_N) -> list[str]:
    eligible = sorted(d for d, s in scores.items() if s != s_star)
    return random.Random(C.VALIDATION_SEED).sample(eligible, min(n, len(eligible)))


def bootstrap_diff(a: Sequence[float], b: Sequence[float], seed: int = C.BOOTSTRAP_SEED,
                   n: int = C.BOOTSTRAP_RESAMPLES, level: float = C.CI_LEVEL
                   ) -> tuple[float, float] | None:
    """Percentile interval of mean(b) - mean(a), resampling each group's documents."""
    import numpy as np

    if not a or not b:
        return None
    rng = np.random.default_rng(seed)
    aa, bb = np.asarray(a, float), np.asarray(b, float)
    ia = rng.integers(0, len(aa), size=(n, len(aa)))
    ib = rng.integers(0, len(bb), size=(n, len(bb)))
    d = bb[ib].mean(axis=1) - aa[ia].mean(axis=1)
    lo, hi = np.quantile(d, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def validate_groups(scores: dict[str, int], assignment: dict[str, str], sample: Sequence[str],
                    panel_rows: dict[str, dict[str, dict]]) -> dict:
    """The §4.9 verdict. `panel_rows`: {panel model key: {doc_id: llm row}}."""
    m1, m2 = C.ROLES["classifier_panel"]

    def ok(m: str, d: str) -> int | None:
        r = panel_rows.get(m, {}).get(d)
        return r["parsed"]["mode"] if r is not None and r["status"] == "ok" else None

    pmean = {d: (ok(m1, d) + ok(m2, d)) / 2 for d in sample
             if ok(m1, d) is not None and ok(m2, d) is not None}
    both = [d for d in sample if d in pmean]
    rho = spearman([scores[d] for d in both], [pmean[d] for d in both])
    sa = [d for d in sample if assignment[d] == "A"]
    sb = [d for d in sample if assignment[d] == "B"]
    pa = [pmean[d] for d in sa if d in pmean]
    pb = [pmean[d] for d in sb if d in pmean]
    diff = (sum(pb) / len(pb) - sum(pa) / len(pa)) if pa and pb else None
    ci = bootstrap_diff(pa, pb)
    sep = diff is not None and diff > 0 and ci is not None and ci[0] > 0
    a_low = sum(1 for v in pa if v < 5)
    b_high = sum(1 for v in pb if v > 5)
    names = bool(sa) and bool(sb) and 2 * a_low > len(sa) and 2 * b_high > len(sb)
    spear_ok = rho is not None and rho >= C.GROUP_PASS_SPEARMAN
    required = math.ceil(MIN_VALID_FRACTION * len(sample) - 1e-9)
    valid = len(both) >= required
    passed = bool(valid and spear_ok and sep and names)
    return {
        "passed": passed,
        "outcome": "pass" if passed else ("fail" if valid else "stop"),
        "stop_reason": None if valid else MEASUREMENT_INVALID,
        "valid_counts": {"n_sampled": len(sample), "required": required,
                         "luna_and_panel": len(both), "min_valid_fraction": MIN_VALID_FRACTION},
        "spearman_ok": spear_ok, "separation_ok": sep, "names_ok": names,
        "spearman_luna_vs_panel_mean": rho,
        "panel_mean_diff_b_minus_a": diff, "diff_ci95": list(ci) if ci else None,
        "n_sample": len(sample), "n_with_panel": len(both),
        "n_sample_a": len(sa), "n_sample_b": len(sb),
        "n_a_panel_below_5": a_low, "n_b_panel_above_5": b_high,
        "spearman_models": spearman([ok(m1, d) for d in both], [ok(m2, d) for d in both]),
        "sample": list(sample),
        "panel_mean": pmean,
        "bootstrap": {"seed": C.BOOTSTRAP_SEED, "resamples": C.BOOTSTRAP_RESAMPLES,
                      "level": C.CI_LEVEL, "method": "percentile, resampled within group"},
    }


def labels(validation: dict | None, scores: dict[str, int], assignment: dict[str, str],
           s_star: int) -> dict:
    at_b = {g: sum(1 for d, s in scores.items() if s == s_star and assignment[d] == g)
            for g in C.GROUPS}
    semantic = bool(validation and validation.get("passed"))
    out = {
        "used": "semantic" if semantic else "relative",
        **(C.GROUP_LABELS_SEMANTIC if semantic else C.GROUP_LABELS_RELATIVE),
        "relative": dict(C.GROUP_LABELS_RELATIVE),
        "semantic": dict(C.GROUP_LABELS_SEMANTIC),
        "validated": validation is not None,
        "boundary_score_docs": at_b,
    }
    if semantic:
        out["caveat"] = (
            "The semantic names describe only the non-boundary documents (the validation "
            "excluded documents scoring s*); whole-group claims and the reusable rule keep the "
            f"relative labels. Boundary-score documents: A {at_b['A']}, B {at_b['B']}.")
    return out


def validation_manifest(sample_docs: Sequence[dict], llm_dir) -> tuple[dict, list[dict]]:
    """({panel model: {doc_id: row}}, manifest entries) for the sampled requests, verified
    against current fingerprints; every request needs a terminal row (`IncompleteBatch`)."""
    panel, entries = {}, []
    reqs = requests_for(sample_docs)
    for m in C.ROLES["classifier_panel"]:
        rows = llm.verified_rows(m, PROMPT_ID, reqs, llm_path(m, llm_dir))
        panel[m] = {}
        for r in reqs:
            row = rows.get((r["item_id"], r["seed"]))
            if row is None or row["status"] not in llm.TERMINAL:
                raise IncompleteBatch(f"group validation: {m} has no terminal answer for "
                                      f"{r['item_id']}; rerun `validate`")
            panel[m][r["item_id"]] = row
            entries.append({"doc_id": r["item_id"], "model_key": m, "seed": r["seed"],
                            "fingerprint": row["fingerprint"], "status": row["status"],
                            "row_sha256": llm.row_sha256(row)})
    return panel, entries


def run_validation(docs: Sequence[dict], groups: dict, *, llm_dir: str | Path = C.LLM_DIR,
                   client_for: Callable[[str], object] | None = None, reserve=None,
                   concurrency: int = 32) -> dict:
    """Score the validation sample with both panel models and return the verdict, bound to the
    manifest of the sampled requests and their result rows."""
    scores, assignment = groups["scores"], groups["assignment"]
    s_star = groups["boundary"]["score"]
    sample = validation_sample(scores, s_star)
    by_id = {d["doc_id"]: d for d in docs}
    sample_docs = [by_id[d] for d in sample]
    for m in C.ROLES["classifier_panel"]:
        got = classify(sample_docs, m, f"val-{m}", llm_dir=llm_dir,
                       client=client_for(m) if client_for else None, reserve=reserve,
                       concurrency=concurrency)
        require_terminal(list(got.values()), f"group validation, {m}")
    panel, entries = validation_manifest(sample_docs, llm_dir)
    v = validate_groups(scores, assignment, sample, panel)
    return {**v, "manifest": entries, "manifest_sha256": manifest_sha256(entries)}


GROUP_VERDICT_KEYS = ("passed", "outcome", "stop_reason", "valid_counts", "sample",
                      "spearman_ok", "separation_ok", "names_ok")


def verify_groups(docs: Sequence[dict], frozen: dict, *, llm_dir: str | Path = C.LLM_DIR,
                  check_validation: bool = True) -> dict:
    """Refuse (`EvidenceMismatch`) unless a frozen groups.json still follows from the current
    insert texts, config and result rows: scores, boundary, assignment and the classification
    manifest recomputed from verified rows; and, if present (and `check_validation`), the
    validation sample, its manifest and its verdict. Returns the recomputed groups (R1 r2 #2)."""
    g = groups_from_disk(docs, llm_dir)
    for k in ("scores", "boundary", "assignment", "classification_manifest_sha256"):
        if frozen.get(k) != g[k]:
            raise EvidenceMismatch(f"groups.json {k!r} no longer follows from the current "
                                   "insert and classifier rows")
    v = frozen.get("validation")
    if check_validation and v is not None:
        sample = validation_sample(g["scores"], g["boundary"]["score"])
        if v.get("sample") != sample:
            raise EvidenceMismatch("the group validation sample no longer follows from the scores")
        by_id = {d["doc_id"]: d for d in docs}
        panel, entries = validation_manifest([by_id[d] for d in sample], llm_dir)
        if entries != v.get("manifest") or manifest_sha256(entries) != v.get("manifest_sha256"):
            raise EvidenceMismatch("the group validation's sampled requests or rows changed")
        v2 = validate_groups(g["scores"], g["assignment"], sample, panel)
        diff = [k for k in GROUP_VERDICT_KEYS if v2[k] != v.get(k)]
        if diff:
            raise EvidenceMismatch(f"the recorded group validation differs on {diff}")
    return g


# --- report and output ---------------------------------------------------------------------------


def report(docs: Sequence[dict], scores: dict[str, int], assignment: dict[str, str],
           s_star: int, reasons: dict[str, str] | None = None) -> dict:
    by_id = {d["doc_id"]: d for d in docs}
    rng = random.Random(C.RAW_SAMPLE_SEED)
    out = {}
    for g in C.GROUPS:
        ids = sorted(d for d, a in assignment.items() if a == g)
        sc = [scores[d] for d in ids]
        raw = rng.sample(ids, min(RAW_N, len(ids)))
        out[g] = {
            "n_docs": len(ids),
            "mean_score": sum(sc) / len(sc) if sc else None,
            "tokens": sum(int(by_id[d]["n_tokens"]) for d in ids),
            "near_boundary": sum(1 for s in sc if abs(s - s_star) <= C.GROUP_NEAR_BOUNDARY),
            "at_boundary_score": sum(1 for s in sc if s == s_star),
            "random_docs": [{"doc_id": d, "score": scores[d],
                             "reason": (reasons or {}).get(d),
                             "excerpt": by_id[d]["text"][:EXCERPT_CHARS]} for d in raw],
        }
    return out


def write_groups(path: str | Path, groups: dict, *, docs: Sequence[dict], inputs: dict,
                 validation: dict | None, reasons: dict[str, str] | None = None) -> str:
    """Write groups.json (GROUP_KEYS + report + provenance) and groups.sha256; return the sha."""
    s_star = groups["boundary"]["score"]
    obj = {
        "boundary": groups["boundary"],
        "assignment": groups["assignment"],
        "scores": groups["scores"],
        "validation": validation,
        "classification_manifest": groups.get("classification_manifest"),
        "classification_manifest_sha256": groups.get("classification_manifest_sha256"),
        "labels": labels(validation, groups["scores"], groups["assignment"], s_star),
        "report": report(docs, groups["scores"], groups["assignment"], s_star, reasons),
        "inputs": inputs,
        "prompt_id": PROMPT_ID, "prompt_sha256": prompts.sha256(PROMPT_ID),
        "models": {k: C.MODELS[k].model
                   for k in (C.ROLES["classifier"], *C.ROLES["classifier_panel"])},
        "request_seed": C.JUDGE_REQUEST_SEED,
        **provenance(),
    }
    assert all(k in obj for k in C.GROUP_KEYS)
    p = Path(path)
    sha = write_json(p, obj)
    (p.parent / "groups.sha256").write_text(f"{sha}  {p.name}\n", encoding="utf-8")
    return sha


def input_hashes(paths: Sequence[str | Path]) -> dict[str, str]:
    return {str(p): file_sha256(p) for p in paths if Path(p).exists()}
