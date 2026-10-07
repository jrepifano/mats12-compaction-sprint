"""Content judge, its validation and fallback, and the insert selection (prereg §4.3).

1. **Score.** gpt-6-luna (`ROLES["content_judge"]`) scores every P1 document with prompt A.1
   (`emx.e9.llm.run_batch`, JSON mode, request seed `JUDGE_REQUEST_SEED`). Each answer becomes a
   `contracts.CONTENT_SCORE_KEYS` row.
2. **Safety exclusion.** A document flagged `minor_sexual` by the selecting judge, or by any panel
   model that scored it, is excluded from every pool and never rewritten; the list and count are
   written to `selection.json` (`safety_excluded`) and `ranked.json`, and each flag is sent to the
   shared exclusion registry (`emx.e9.checks.add_exclusions`, reason "minor_sexual", source
   "content_judge" or "content_panel:<model>") so one registry holds every exclusion.
3. **Ranking.** Endorsement score descending; ties by `tie_hash(doc_id)` =
   int(sha256(f"{CORPUS_SEED}:{doc_id}"), 16) ascending. Documents without a valid score from the
   selector (malformed, refused) are excluded and counted (`failed`). A document with no terminal
   answer at all (an API error or a batch cut short) stops the selection: rerun `score`.
4. **Insert.** The ranked prefix whose single-copy original tokens (`n_tokens`, end-of-text
   excluded) first reach `INSERT_TOKENS` (4,000,000), the crossing document included. Since the
   ranking is by score, documents scoring >= 4 come first and the low-score top-up is automatic;
   the report gives the score distribution, the share of tokens below 4 and whether the top-up
   was used. The FULL ranked order is written (`ranked.json`): the replacement loop (§4.4, unit
   B2b) takes the next-ranked documents from it.
5. **Validation** (rules frozen). 150 documents drawn at random from the insert and 150 from the
   rest of the ranked documents (`VALIDATION_SEED`; each list sorted by doc id before drawing) are
   scored by the panel `ROLES["content_panel"]` (glm-5.3-flash, gemini-3.5-flash-lite). luna
   passes iff Spearman(luna, panel mean) >= 0.6 AND at least 70% of the sampled insert documents
   have panel mean >= 4 (denominator: every sampled insert document; one the panel failed to
   score counts as not >= 4). Otherwise, if the panel is reliable (Spearman between its two
   models >= 0.6 on the same documents), the selector becomes the panel mean: the panel scores
   all of P1 (`panel-all`, its cost projected and reserved like any batch, stage "setup") and the
   insert is reselected by the same rules (a panel-mean score needs both models' valid answers).
   Else Part 1 stops: the outcome is "stop" and no insert is written. Spearman on a constant
   vector is undefined and counts as a failure. No verdict is computed or written until every
   scheduled validation request has a terminal answer (`IncompleteBatch` otherwise; R1 #8).
   **Missing assessments** (execution rule under Amendment F, main session 2026-10-07): luna's
   verdict is computed only if at least 90% of the sampled documents (270 of 300) have valid
   scores from luna and both panel models; otherwise the outcome is "stop" with
   `stop_reason` "measurement invalid: too few valid assessments", never a pass and never the
   panel fallback (which itself needs 90% with both panel scores). `valid_counts` records the
   counts. The verdict is bound to a manifest of the sampled requests and their result rows
   (fingerprint, row sha256; `manifest_sha256`), and `verify_validation` recomputes the sample,
   the manifest and the verdict from the current pool, config and rows before `panel-all`, the
   selection or any output write (R1 r2 #2).
7. **Verified reads.** Every reader of scores (`collect_scores`) recomputes each request's
   fingerprint from the CURRENT pool text and config and refuses a mismatch.
8. **Sticky safety flags.** A positive `minor_sexual` in ANY attempt of an A.1 answer (even an
   otherwise malformed one) excludes the document (`safety_flag`).
6. **Chunks.** Full-pool scoring runs in chunks of `CHUNK_DOCS` documents, one reservation each
   (each reservation holds the batch's upper bound until it settles).

Outputs (`contracts.SELECT_DIR`): `content_scores.jsonl`, `validation.json`, `ranked.json`,
`selection.json`, each with the input sha256s, prompt sha256, model ids, UTC time and the code
commit.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import subprocess
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from emx.e9 import contracts as C
from emx.e9 import llm, prompts

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPT_ID = "A1_content"
STAGE = "setup"
STEP = "select"
P1_PATH = f"{C.POOLS_DIR}/P1.jsonl"
OUTCOMES: tuple[str, ...] = ("luna", "panel", "stop")


# --- provenance and IO ---------------------------------------------------------------------------


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def provenance(root: Path = REPO_ROOT) -> dict:
    """UTC time, code commit and whether the working tree had uncommitted changes."""
    def git(*args: str) -> str | None:
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                                  check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    status = git("status", "--porcelain")
    return {"utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "code_commit": git("rev-parse", "HEAD"),
            "dirty": None if status is None else bool(status)}


def read_jsonl(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: str | Path, rows: Sequence[dict]) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    tmp.replace(p)
    return file_sha256(p)


def write_json(path: str | Path, obj: dict) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)
    return file_sha256(p)


def load_docs(path: str | Path) -> list[dict]:
    docs = read_jsonl(path)
    ids = set()
    for d in docs:
        missing = [k for k in C.DOC_KEYS if k not in d]
        if missing:
            raise ValueError(f"{path}: document {d.get('doc_id')!r} lacks {missing}")
        if d["doc_id"] in ids:
            raise ValueError(f"{path}: duplicate doc_id {d['doc_id']!r}")
        ids.add(d["doc_id"])
    return docs


def models_meta(keys: Sequence[str]) -> dict[str, str]:
    return {k: C.MODELS[k].model for k in keys}


# --- scoring -------------------------------------------------------------------------------------


def llm_path(model_key: str, llm_dir: str | Path = C.LLM_DIR) -> Path:
    """One resumable A.1 result file per model (validation and full scoring share it)."""
    return Path(llm_dir) / f"{PROMPT_ID}.{model_key}.jsonl"


def score_requests(docs: Sequence[dict]) -> list[dict]:
    return [{"item_id": d["doc_id"], "fields": {"document": d["text"]},
             "seed": C.JUDGE_REQUEST_SEED, "ref_chars": None} for d in docs]


def content_row(row: dict) -> dict:
    """An `llm` result row -> a `CONTENT_SCORE_KEYS` row (score fields None unless ok), plus
    `safety_flag`: true if ANY attempt's answer said minor_sexual true (sticky, R1 r2 #4). A
    non-ok row with a sticky flag reports `minor_sexual` true."""
    p = row.get("parsed") if row["status"] == "ok" else None
    sticky = row.get("safety_flag")
    return {
        "doc_id": row["item_id"], "model_key": row["model_key"],
        "endorsement": p["endorsement"] if p else None,
        "harm_type": p["harm_type"] if p else None,
        "minor_sexual": p["minor_sexual"] if p else (True if sticky else None),
        "summary": p["summary"] if p else None,
        "status": row["status"],
        "safety_flag": sticky,
    }


def flagged(r: dict) -> bool:
    """A content row carries a positive minor_sexual flag (final answer or any attempt)."""
    return r.get("minor_sexual") is True or r.get("safety_flag") is True


class EvidenceMismatch(RuntimeError):
    """Recorded evidence no longer matches the current inputs, config or result rows."""


class IncompleteBatch(RuntimeError):
    """Some scheduled requests have no terminal answer (API errors): rerun the step (R1 #8)."""


CHUNK_DOCS = 10_000  # one reservation per chunk keeps the open reservation (a bound) small


def score_docs(
    docs: Sequence[dict], model_key: str, batch: str, *, llm_dir: str | Path = C.LLM_DIR,
    client=None, reserve=None, concurrency: int = 32, chunk: int = CHUNK_DOCS,
) -> list[dict]:
    """Score `docs` with A.1 by `model_key`; returns `CONTENT_SCORE_KEYS` rows in document order.

    Documents are cut into consecutive chunks of `chunk` (by position in `docs`), each its own
    reservation: note E9:setup:select:<batch>, or <batch>-c<k> when there is more than one chunk.
    The chunking depends only on `docs`, so a rerun maps every document to the same batch key."""
    parts = [list(docs)] if len(docs) <= chunk else [
        list(docs[i:i + chunk]) for i in range(0, len(docs), chunk)]
    out = []
    for k, part in enumerate(parts):
        name = batch if len(parts) == 1 else f"{batch}-c{k}"
        rows = llm.run_batch(
            model_key, PROMPT_ID, score_requests(part), llm_path(model_key, llm_dir),
            mode="json", stage=STAGE, note=C.ledger_note(STAGE, STEP, name),
            concurrency=concurrency, client=client, reserve=reserve,
        )
        out += [content_row(r) for r in rows]
    return out


def require_terminal(rows: Sequence[dict], what: str) -> None:
    """Raise `IncompleteBatch` unless every row has a terminal status."""
    bad = [r.get("doc_id", r.get("item_id")) for r in rows if r["status"] not in llm.TERMINAL]
    if bad:
        raise IncompleteBatch(f"{what}: {len(bad)} requests have no terminal answer (e.g. "
                              f"{bad[:3]}); rerun before any verdict is computed or written")


def project_usd(docs: Sequence[dict], model_key: str) -> float:
    return llm.estimate_usd(model_key, PROMPT_ID, score_requests(docs), "json")


def collect_scores(docs: Sequence[dict], llm_dir: str | Path = C.LLM_DIR,
                   model_keys: Sequence[str] | None = None) -> list[dict]:
    """The content-score rows of `docs` (the CURRENT pool records), one per document and model
    that has an answer, each verified against the fingerprint recomputed from the current text
    and config (`llm.verified_rows`; a mismatch raises `llm.FingerprintMismatch`, R1 r2 #2)."""
    keys = model_keys or (C.ROLES["content_judge"], *C.ROLES["content_panel"])
    reqs = score_requests(docs)
    out = []
    for k in keys:
        rows = llm.verified_rows(k, PROMPT_ID, reqs, llm_path(k, llm_dir))
        out += [content_row(rows[(r["item_id"], r["seed"])]) for r in reqs
                if (r["item_id"], r["seed"]) in rows]
    return out


# --- ranking and the insert ----------------------------------------------------------------------


def tie_hash(doc_id: str) -> int:
    return int(hashlib.sha256(f"{C.CORPUS_SEED}:{doc_id}".encode()).hexdigest(), 16)


def safety_excluded(rows: Sequence[dict]) -> set[str]:
    """Documents any model flagged `minor_sexual` in any attempt (§4.3 safety exclusion)."""
    return {r["doc_id"] for r in rows if flagged(r)}


def by_model(rows: Sequence[dict]) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["model_key"], {})[r["doc_id"]] = r
    return out


def selector_scores(rows: Sequence[dict], selector: str, doc_ids: Sequence[str]
                    ) -> tuple[dict[str, float], list[str], list[str]]:
    """(scores, failed, unanswered) for the selector: "luna" (the content judge's endorsement) or
    "panel" (the mean of both panel models'). `failed`: a terminal non-ok answer (or a panel
    model's); `unanswered`: no terminal answer from a model the selector needs."""
    models = ([C.ROLES["content_judge"]] if selector == "luna"
              else list(C.ROLES["content_panel"]))
    bm = by_model(rows)
    scores, failed, unanswered = {}, [], []
    for d in doc_ids:
        got = [bm.get(m, {}).get(d) for m in models]
        if any(g is None or g["status"] not in llm.TERMINAL for g in got):
            unanswered.append(d)
        elif all(g["status"] == "ok" for g in got):
            scores[d] = sum(g["endorsement"] for g in got) / len(got)
        else:
            failed.append(d)
    return scores, failed, unanswered


def rank(scores: dict[str, float], exclude: set[str] = frozenset()) -> list[str]:
    """Doc ids by score descending, ties by `tie_hash` ascending; `exclude` left out."""
    return sorted((d for d in scores if d not in exclude), key=lambda d: (-scores[d], tie_hash(d)))


def _score_key(s: float) -> str:
    return str(int(s)) if float(s).is_integer() else str(s)


def select_insert(ranked: Sequence[str], scores: dict[str, float], n_tokens: dict[str, int],
                  target: int = C.INSERT_TOKENS) -> dict:
    """The ranked prefix reaching `target` single-copy original tokens, crossing doc included."""
    insert, total = [], 0
    for d in ranked:
        if total >= target:
            break
        insert.append(d)
        total += int(n_tokens[d])
    below = [d for d in insert if scores[d] < C.INSERT_PREFERRED_MIN_SCORE]
    tok_below = sum(int(n_tokens[d]) for d in below)
    preferred = [d for d in ranked if scores[d] >= C.INSERT_PREFERRED_MIN_SCORE]
    dist = Counter(_score_key(scores[d]) for d in insert)
    return {
        "insert": insert,
        "n_insert": len(insert),
        "insert_tokens": total,
        "target_tokens": target,
        "reached_target": total >= target,
        "crossing_doc": insert[-1] if insert and total >= target else None,
        "score_distribution": dict(sorted(dist.items(), key=lambda kv: -float(kv[0]))),
        "n_below_preferred": len(below),
        "tokens_below_preferred": tok_below,
        "share_tokens_below_preferred": (tok_below / total) if total else None,
        "topup_used": bool(below),
        "preferred_pool_docs": len(preferred),
        "preferred_pool_tokens": sum(int(n_tokens[d]) for d in preferred),
    }


def build_selection(docs: Sequence[dict], rows: Sequence[dict], selector: str) -> dict:
    """Ranking and insert for one selector over P1, with the safety exclusion applied."""
    ids = [d["doc_id"] for d in docs]
    n_tok = {d["doc_id"]: int(d["n_tokens"]) for d in docs}
    scores, failed, unanswered = selector_scores(rows, selector, ids)
    if unanswered:
        raise RuntimeError(
            f"{len(unanswered)} P1 documents have no terminal {selector} answer (e.g. "
            f"{unanswered[:3]}); rerun the scoring before selecting"
        )
    excluded = safety_excluded([r for r in rows if r["doc_id"] in set(ids)])
    ranked = rank(scores, excluded)
    sel = select_insert(ranked, scores, n_tok)
    return {
        "selector": selector,
        "ranked": [{"doc_id": d, "rank": i, "score": scores[d], "n_tokens": n_tok[d]}
                   for i, d in enumerate(ranked)],
        "scores": scores,
        "failed": sorted(failed),
        "safety_excluded": sorted(excluded),
        **sel,
    }


# --- validation ----------------------------------------------------------------------------------


def validation_sample(insert: Sequence[str], rest: Sequence[str],
                      n_insert: int = C.CONTENT_VAL_N_INSERT,
                      n_rest: int = C.CONTENT_VAL_N_REST) -> tuple[list[str], list[str]]:
    rng = random.Random(C.VALIDATION_SEED)
    a = rng.sample(sorted(insert), min(n_insert, len(insert)))
    b = rng.sample(sorted(rest), min(n_rest, len(rest)))
    return a, b


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman's rho, or None when undefined (fewer than 3 pairs or a constant vector)."""
    from scipy.stats import spearmanr

    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    r = spearmanr(x, y).statistic
    return None if r is None or math.isnan(r) else float(r)


def _ge(v: float | None, thr: float) -> bool:
    return v is not None and v >= thr


MIN_VALID_FRACTION = 0.9  # execution rule under Amendment F (main session, 2026-10-07)
MEASUREMENT_INVALID = "measurement invalid: too few valid assessments"


def validate_content(rows: Sequence[dict], insert_ids: Sequence[str], rest_ids: Sequence[str]
                     ) -> dict:
    """The §4.3 validation verdict from the content-score rows of the sampled documents."""
    judge = C.ROLES["content_judge"]
    m1, m2 = C.ROLES["content_panel"]
    bm = by_model(rows)

    def ok(m: str, d: str) -> int | None:
        r = bm.get(m, {}).get(d)
        return r["endorsement"] if r is not None and r["status"] == "ok" else None

    sample = list(insert_ids) + list(rest_ids)
    both = [d for d in sample if ok(m1, d) is not None and ok(m2, d) is not None]
    full = [d for d in both if ok(judge, d) is not None]
    pmean = {d: (ok(m1, d) + ok(m2, d)) / 2 for d in both}
    rho_judge = spearman([ok(judge, d) for d in full], [pmean[d] for d in full])
    rho_panel = spearman([ok(m1, d) for d in both], [ok(m2, d) for d in both])
    n_ins_ge4 = sum(1 for d in insert_ids if d in pmean and pmean[d] >= 4)
    frac = n_ins_ge4 / len(insert_ids) if insert_ids else None
    required = math.ceil(MIN_VALID_FRACTION * len(sample) - 1e-9)
    judge_valid = len(full) >= required  # the statistic needs luna and both panel models
    panel_valid = len(both) >= required
    judge_pass = judge_valid and _ge(rho_judge, C.CONTENT_PASS_SPEARMAN) and _ge(
        frac, C.CONTENT_PASS_INSERT_FRACTION)
    panel_reliable = panel_valid and _ge(rho_panel, C.CONTENT_PANEL_RELIABLE_SPEARMAN)
    stop_reason = None
    if not judge_valid:  # luna's verdict can't be established: never a pass, never the fallback
        outcome, panel_reliable, stop_reason = "stop", False, MEASUREMENT_INVALID
    elif judge_pass:
        outcome = "luna"
    elif panel_reliable:
        outcome = "panel"
    else:
        outcome = "stop"
        stop_reason = ("content judge failed validation and the panel is not reliable "
                       "(prereg §4.3): Part 1 stops")
    means = {}
    for m in (judge, m1, m2):
        v = [ok(m, d) for d in sample if ok(m, d) is not None]
        means[m] = sum(v) / len(v) if v else None
    return {
        "outcome": outcome,
        "stop_reason": stop_reason,
        "judge_pass": judge_pass,
        "panel_reliable": panel_reliable,
        "valid_counts": {"n_sampled": len(sample), "required": required,
                         "luna_and_panel": len(full), "panel_both": len(both),
                         "min_valid_fraction": MIN_VALID_FRACTION},
        "spearman_judge_vs_panel_mean": rho_judge,
        "spearman_panel_models": rho_panel,
        "insert_fraction_panel_ge4": frac,
        "n_insert_sampled": len(insert_ids),
        "n_insert_panel_ge4": n_ins_ge4,
        "n_rest_sampled": len(rest_ids),
        "n_with_panel": len(both),
        "n_with_all_three": len(full),
        "thresholds": {"spearman": C.CONTENT_PASS_SPEARMAN,
                       "insert_fraction": C.CONTENT_PASS_INSERT_FRACTION,
                       "panel_reliable_spearman": C.CONTENT_PANEL_RELIABLE_SPEARMAN},
        "mean_scores": means,
        "insert_ids": list(insert_ids),
        "rest_ids": list(rest_ids),
    }


def _sample_ids(docs: Sequence[dict], llm_dir) -> tuple[list[str], list[str]]:
    """The validation sample, drawn from luna's (verified) ranking."""
    rows = collect_scores(docs, llm_dir, (C.ROLES["content_judge"],))
    luna_sel = build_selection(docs, rows, "luna")
    insert = luna_sel["insert"]
    return validation_sample(insert, [r["doc_id"] for r in luna_sel["ranked"][len(insert):]])


def validation_manifest(sample_docs: Sequence[dict], llm_dir) -> list[dict]:
    """One entry per sampled request (luna and both panel models): doc, model, seed, fingerprint,
    status and the sha256 of its result row. Every request must have a terminal row."""
    entries = []
    reqs = score_requests(sample_docs)
    for m in (C.ROLES["content_judge"], *C.ROLES["content_panel"]):
        rows = llm.verified_rows(m, PROMPT_ID, reqs, llm_path(m, llm_dir))
        for r in reqs:
            row = rows.get((r["item_id"], r["seed"]))
            if row is None or row["status"] not in llm.TERMINAL:
                raise IncompleteBatch(f"content validation: {m} has no terminal answer for "
                                      f"{r['item_id']}; rerun `validate`")
            entries.append({"doc_id": r["item_id"], "model_key": m, "seed": r["seed"],
                            "fingerprint": row["fingerprint"], "status": row["status"],
                            "row_sha256": llm.row_sha256(row)})
    return entries


def manifest_sha256(entries: Sequence[dict]) -> str:
    return hashlib.sha256(json.dumps(list(entries), sort_keys=True).encode("utf-8")).hexdigest()


def run_validation(docs: Sequence[dict], *, llm_dir: str | Path = C.LLM_DIR,
                   client_for: Callable[[str], object] | None = None, reserve=None,
                   concurrency: int = 32) -> dict:
    """Draw the 300-document sample from luna's insert and the rest, have both panel models score
    it (one reservation per model), and return the verdict, bound to the manifest of the sampled
    requests and their result rows (`manifest`, `manifest_sha256`)."""
    ins_s, rest_s = _sample_ids(docs, llm_dir)
    by_id = {d["doc_id"]: d for d in docs}
    sample_docs = [by_id[d] for d in ins_s + rest_s]
    for m in C.ROLES["content_panel"]:
        rows_m = score_docs(sample_docs, m, f"val-{m}", llm_dir=llm_dir,
                            client=client_for(m) if client_for else None, reserve=reserve,
                            concurrency=concurrency)
        require_terminal(rows_m, f"content validation, {m}")
    entries = validation_manifest(sample_docs, llm_dir)
    v = validate_content(collect_scores(sample_docs, llm_dir), ins_s, rest_s)
    return {**v, "manifest": entries, "manifest_sha256": manifest_sha256(entries)}


VERDICT_KEYS = ("outcome", "stop_reason", "judge_pass", "panel_reliable", "valid_counts",
                "insert_ids", "rest_ids")


def verify_validation(docs: Sequence[dict], validation: dict, *,
                      llm_dir: str | Path = C.LLM_DIR) -> None:
    """Refuse (`EvidenceMismatch`) unless the recorded validation still follows from the current
    pool, config and result rows: the same sample, the same manifest (fingerprints and row
    hashes), its sha256, and the same verdict recomputed. Run before any later paid work or
    output write that relies on the verdict (R1 r2 #2)."""
    for k in ("manifest", "manifest_sha256", *VERDICT_KEYS):
        if k not in validation:
            raise EvidenceMismatch(f"validation record lacks {k!r}: rerun `validate`")
    ins_s, rest_s = _sample_ids(docs, llm_dir)
    if [ins_s, rest_s] != [validation["insert_ids"], validation["rest_ids"]]:
        raise EvidenceMismatch("the validation sample no longer follows from luna's ranking")
    by_id = {d["doc_id"]: d for d in docs}
    sample_docs = [by_id[d] for d in ins_s + rest_s]
    entries = validation_manifest(sample_docs, llm_dir)
    if entries != validation["manifest"] or manifest_sha256(entries) != validation[
            "manifest_sha256"]:
        raise EvidenceMismatch("the validation's sampled requests or result rows changed")
    v = validate_content(collect_scores(sample_docs, llm_dir), ins_s, rest_s)
    diff = [k for k in VERDICT_KEYS if v[k] != validation[k]]
    if diff:
        raise EvidenceMismatch(f"the recorded validation verdict differs on {diff}")


def final_selection(docs: Sequence[dict], validation: dict, *, llm_dir: str | Path = C.LLM_DIR
                    ) -> dict:
    """The selection the (verified) validation outcome calls for: luna's ranking, the panel
    mean's (every P1 document must then have both panel models' terminal answers), or a stop."""
    outcome = validation["outcome"]
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown validation outcome {outcome!r}")
    verify_validation(docs, validation, llm_dir=llm_dir)
    rows = collect_scores(docs, llm_dir)
    if outcome == "stop":
        return {"outcome": "stop", "selector": None, "insert": [], "ranked": [],
                "safety_excluded": sorted(safety_excluded(rows)),
                "reason": validation["stop_reason"]}
    return {"outcome": outcome, **build_selection(docs, rows, outcome)}


# --- outputs -------------------------------------------------------------------------------------


def meta(p1_path: str | Path, llm_dir: str | Path = C.LLM_DIR) -> dict:
    """Input sha256s (P1 and every A.1 result file on disk), prompt sha, model ids, provenance."""
    inputs = {str(p1_path): file_sha256(p1_path)}
    for k in (C.ROLES["content_judge"], *C.ROLES["content_panel"]):
        p = llm_path(k, llm_dir)
        if p.exists():
            inputs[str(p)] = file_sha256(p)
    return {"inputs": inputs, "prompt_id": PROMPT_ID, "prompt_sha256": prompts.sha256(PROMPT_ID),
            "models": models_meta((C.ROLES["content_judge"], *C.ROLES["content_panel"])),
            "request_seed": C.JUDGE_REQUEST_SEED, **provenance()}


def write_content_scores(docs: Sequence[dict], out_dir: str | Path = C.SELECT_DIR,
                         llm_dir: str | Path = C.LLM_DIR) -> str:
    return write_jsonl(Path(out_dir) / "content_scores.jsonl", collect_scores(docs, llm_dir))


def write_validation(validation: dict, *, docs: Sequence[dict], p1_path: str | Path,
                     out_dir: str | Path = C.SELECT_DIR, llm_dir: str | Path = C.LLM_DIR) -> str:
    verify_validation(docs, validation, llm_dir=llm_dir)
    write_content_scores(docs, out_dir, llm_dir)
    return write_json(Path(out_dir) / "validation.json", {**validation, **meta(p1_path, llm_dir)})


def exclusion_source(model_key: str) -> str:
    return "content_judge" if model_key == C.ROLES["content_judge"] else f"content_panel:{model_key}"


def register_safety_exclusions(rows: Sequence[dict], add_exclusions=None) -> dict[str, list[str]]:
    """Send every `minor_sexual` flag to the shared exclusion registry
    (`emx.e9.checks.add_exclusions`, data/E9/insert/exclusions.json), one call per flagging
    model with reason "minor_sexual" and source "content_judge" or "content_panel:<model>".
    Returns {source: doc ids}."""
    by_src: dict[str, set[str]] = {}
    for r in rows:
        if flagged(r):
            by_src.setdefault(exclusion_source(r["model_key"]), set()).add(r["doc_id"])
    if not by_src:
        return {}
    if add_exclusions is None:
        from emx.e9.checks import add_exclusions
    out = {}
    for src, ids in sorted(by_src.items()):
        add_exclusions(sorted(ids), reason="minor_sexual", source=src)
        out[src] = sorted(ids)
    return out


def write_outputs(sel: dict, *, docs: Sequence[dict], p1_path: str | Path,
                  out_dir: str | Path = C.SELECT_DIR, llm_dir: str | Path = C.LLM_DIR,
                  add_exclusions=None) -> dict:
    """Register the safety exclusions, then write content_scores.jsonl, ranked.json and
    selection.json (validation.json is written by `write_validation`; its sha256 is recorded
    here)."""
    out_dir = Path(out_dir)
    rows = collect_scores(docs, llm_dir)
    register_safety_exclusions(rows, add_exclusions)
    m = meta(p1_path, llm_dir)
    hashes = {"content_scores.jsonl": write_jsonl(out_dir / "content_scores.jsonl", rows)}
    vpath = out_dir / "validation.json"
    ranked = {"selector": sel.get("selector"), "outcome": sel["outcome"],
              "tie_break": f'int(sha256(f"{C.CORPUS_SEED}:{{doc_id}}"), 16) ascending',
              "ranked": sel.get("ranked", []), "failed": sel.get("failed", []),
              "safety_excluded": sel.get("safety_excluded", []), **m}
    hashes["ranked.json"] = write_json(out_dir / "ranked.json", ranked)
    summary = {k: v for k, v in sel.items() if k not in ("ranked", "scores")}
    summary |= {"n_safety_excluded": len(sel.get("safety_excluded", [])),
                "n_failed": len(sel.get("failed", [])),
                "ranked_sha256": hashes["ranked.json"],
                "validation_sha256": file_sha256(vpath) if vpath.exists() else None, **m}
    hashes["selection.json"] = write_json(out_dir / "selection.json", summary)
    return hashes


def pending(docs: Sequence[dict], model_key: str, llm_dir: str | Path = C.LLM_DIR) -> list[dict]:
    """The documents without a terminal A.1 answer from `model_key` (what a run would send)."""
    todo = {r["item_id"] for r in llm.pending(model_key, PROMPT_ID, score_requests(docs),
                                              llm_path(model_key, llm_dir))}
    return [d for d in docs if d["doc_id"] in todo]
