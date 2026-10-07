"""E9 rewrite checks, checker validation by writer, and insert acceptance (unit B2b).

Implements prereg §4.4 (docs/experiments/E9/prereg.md), with the ranking of §4.3 and prompts A.1
and A.4 of Appendix A:

- **The check table** (`table_pass`). The checker (`zgl`) scores the original once and each output
  with A.1, and A.4 on (original, output). A correction passes iff its endorsement is <= 2, all
  three A.4 answers are true and its token length ratio is in [0.8, 1.25]; its edit distance is
  only reported. A paraphrase passes iff its endorsement is >= 4 and within +-2 of the original's,
  all three A.4 answers are true, the ratio is in [0.8, 1.25] and the normalized token edit
  distance is >= 0.3. A malformed checker output (after the llm layer's retries) fails the check
  and is recorded as `status="malformed"`; an API error is never a failed check (it aborts
  resumably, `ResumableAbort`).
- **Checker validation by writer stratum** (`sample_to_endpoint`, `evaluate_policy`). After the
  first rewriting pass, each writer's outputs are sampled without replacement to the sampling
  endpoint; each stratum's ordered policy table is evaluated once there, and the policy is frozen
  (persisted) for every later output: regenerations, replacements and the +-10% repairs.
- **Acceptance** (`accept`). A document stays only if both its correction and its paraphrase pass
  under their writers' policies (after at most one same-writer regeneration, run by unit B2a's
  `rewrite.regenerate`); otherwise it is dropped from every arm and replaced by the next-ranked
  candidate until single-copy original tokens total >= 4M (the crossing document kept). Then the
  corrections' and paraphrases' totals must each lie within +-10% of the originals'; if not, the
  single most extreme document is removed and replaced through the same loop, repeatedly.
- **Exclusions** (Amendment F; §4.3): one append-only registry, data/E9/insert/exclusions.json
  (`add_exclusions`, `load_exclusions`). A minor_sexual flag in any A.1 assessment here (checker
  or panel, original or rewrite) adds the document; an excluded document is never rewritten
  again and is dropped from every arm and replaced. `accept(refreeze=True)` rebuilds a frozen
  insert after the registry changes, recording the previous manifest's hash and the reason.
- **Evidence** (review R1 #4, #12): frozen policies record and verify their evidence (selection,
  first-pass rewrites, validation samples, prompts, models); the manifest records a hashed
  evidence inventory and the source state (`verify_manifest` rechecks it).
- **The frozen insert**: `data/E9/insert/insert_manifest.json` (contracts.INSERT_MANIFEST_KEYS
  without "capacity", which unit C adds) and `versions.jsonl` (contracts.VERSION_KEYS rows).

Outcomes are explicit dicts: {"outcome": "frozen", ...} or {"outcome": "stop", "reason": ...}.
Every API call goes through `emx.e9.llm.run_batch` with stage "setup" (it reserves and settles).
The two parallel-unit interfaces (llm.run_batch, rewrite.write_versions/regenerate) are reached
only through the small adapters `_LLM` and `_Rewriter`, so a change of their shapes is local.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median

import numpy as np

from emx.e9 import contracts as C
from emx.e9.text import text_sha256

STAGE = "setup"
ROOT = Path(__file__).resolve().parents[3]

# Writer strata (§4.4 "Checker validation, by writer"): panel role and minimum agreed labels.
STRATA: dict[str, dict] = {
    "luna": {"name": "main", "panel_role": "checker_panel_luna",
             "min_agreed": C.CHECK_VAL_MIN_AGREED["luna"]},
    "qwn": {"name": "fallback", "panel_role": "checker_panel_qwn",
            "min_agreed": C.CHECK_VAL_MIN_AGREED["qwn"]},
}
OK_STATUS = "ok"


class Stop(Exception):  # an outcome, not an error: accept() turns it into a record
    """Part 1 stops (§4.4): converted by `accept` into {"outcome": "stop", "reason": ...}."""

    def __init__(self, reason: str, **details):
        super().__init__(reason)
        self.reason = reason
        self.details = details


# --- pure rules ---------------------------------------------------------------------------------


def token_edit_distance(a: Sequence[int], b: Sequence[int]) -> float:
    """Normalized Levenshtein distance between two token-id sequences, divided by
    max(len a, len b) (§4.4 "Rewording"); 0.0 for two empty sequences.

    Row recurrence vectorized: with t[j] = min(prev[j] + 1, prev[j-1] + [a_i != b_j]) and
    t[0] = i, the insertion chain gives cur[j] = min_{k<=j} (t[k] + j - k)
    = j + minimum.accumulate(t - j)."""
    a_arr = np.asarray(a, dtype=np.int64)
    b_arr = np.asarray(b, dtype=np.int64)
    n, m = len(a_arr), len(b_arr)
    if max(n, m) == 0:
        return 0.0
    if n == 0 or m == 0:
        return 1.0
    j = np.arange(m + 1, dtype=np.int64)
    prev = j.copy()
    t = np.empty(m + 1, dtype=np.int64)
    for i in range(1, n + 1):
        t[0] = i
        sub = prev[:-1] + (b_arr != a_arr[i - 1])
        np.minimum(prev[1:] + 1, sub, out=t[1:])
        prev = j + np.minimum.accumulate(t - j)
    return float(prev[m]) / max(n, m)


def length_ratio(n_out: int, n_orig: int) -> float:
    """Output tokens / original tokens (§4.4)."""
    return n_out / n_orig


def table_pass(
    version: str,
    endorse_orig: int | None,
    endorse_out: int | None,
    a4: dict | None,
    ratio: float,
    edit_distance: float,
) -> bool:
    """One model's pass/fail under the §4.4 check table. None (a malformed or missing answer)
    fails every check that needs it."""
    if version not in ("corr", "para"):
        raise ValueError(f"unknown version {version!r}")
    if endorse_out is None or a4 is None:
        return False
    if not all(a4.get(k) is True for k in ("same_topic", "same_entities", "facts_preserved")):
        return False
    if not (C.LENGTH_RATIO[0] <= ratio <= C.LENGTH_RATIO[1]):
        return False
    if version == "corr":
        return endorse_out <= C.CORR_ENDORSE_MAX
    if endorse_orig is None:
        return False
    return (
        endorse_out >= C.PARA_ENDORSE_MIN
        and abs(endorse_out - endorse_orig) <= C.PARA_ENDORSE_TOL
        and edit_distance >= C.PARA_EDIT_MIN
    )


def parse_endorsement(parsed: dict | None) -> int | None:
    """A.1's integer endorsement 0-10, or None if malformed."""
    if not isinstance(parsed, dict):
        return None
    v = parsed.get("endorsement")
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if not isinstance(v, int) or not 0 <= v <= 10:
        return None
    return v


def parse_a4(parsed: dict | None) -> dict | None:
    """A.4's three booleans, or None if malformed."""
    if not isinstance(parsed, dict):
        return None
    keys = ("same_topic", "same_entities", "facts_preserved")
    if not all(isinstance(parsed.get(k), bool) for k in keys):
        return None
    return {k: parsed[k] for k in keys}


def _quota(n: int, n_corr: int, n_para: int) -> tuple[int, int]:
    """Equal quotas of corrections and paraphrases where feasible (corrections get the floor of
    an odd n); a shortfall of one type is filled from the other (§4.4 "Sampling")."""
    c = min(n // 2, n_corr)
    p = min(n - n // 2, n_para)
    c += min(n - c - p, n_corr - c)
    p += min(n - c - p, n_para - p)
    return c, p


def sample_to_endpoint(
    items: Iterable[tuple[str, str]],
    need: int,
    label: Callable[[list[str]], dict[str, bool]],
    seed: int = C.VALIDATION_SEED,
) -> dict:
    """Seeded validation draws to the sampling endpoint (§4.4).

    `items` are (output key, version) pairs of one writer stratum. Corrections and paraphrases
    are each sorted by key and permuted once by `np.random.default_rng(seed)` (corrections
    first); a draw takes the next ones of each queue, so sampling is without replacement. The
    first draw is CHECK_VAL_FIRST items (or all if fewer); while fewer than `need` agreed labels
    exist and unsampled items remain, further CHECK_VAL_BATCH draws, up to CHECK_VAL_MAX in all.
    `label(keys)` returns {key: panel agreed?} for a draw."""
    items = list(items)
    rng = np.random.default_rng(seed)
    corr = sorted(k for k, v in items if v == "corr")
    para = sorted(k for k, v in items if v == "para")
    if len(corr) + len(para) != len(items):
        raise ValueError("items must be corrections or paraphrases")
    corr_q = [corr[i] for i in rng.permutation(len(corr))]
    para_q = [para[i] for i in rng.permutation(len(para))]
    drawn: list[str] = []
    batches: list[int] = []
    agreed: dict[str, bool] = {}

    def draw(n: int) -> None:
        nonlocal corr_q, para_q
        c, p = _quota(n, len(corr_q), len(para_q))
        keys = corr_q[:c] + para_q[:p]
        corr_q, para_q = corr_q[c:], para_q[p:]
        if not keys:
            return
        got = label(keys)
        missing = set(keys) - set(got)
        if missing:
            raise ValueError(f"label() returned no decision for {len(missing)} items")
        agreed.update({k: bool(got[k]) for k in keys})
        drawn.extend(keys)
        batches.append(len(keys))

    draw(min(C.CHECK_VAL_FIRST, len(items)))
    while (
        sum(agreed.values()) < need
        and (corr_q or para_q)
        and len(drawn) < C.CHECK_VAL_MAX
    ):
        draw(min(C.CHECK_VAL_BATCH, C.CHECK_VAL_MAX - len(drawn)))
    n_agreed = sum(agreed.values())
    if n_agreed >= need:
        endpoint = "enough_agreed"
    elif len(drawn) >= C.CHECK_VAL_MAX:
        endpoint = "max_items"
    else:
        endpoint = "exhausted"
    return {"drawn": drawn, "batches": batches, "n_agreed": n_agreed, "endpoint": endpoint,
            "n_items": len(items), "seed": seed}


def evaluate_policy(writer: str, labels: Sequence[dict]) -> dict:
    """Evaluate a stratum's ordered policy table once, at its sampling endpoint (§4.4).

    `labels`: one dict per drawn item with "checker" (bool), "panel" (two bools) and their
    validity, "checker_valid" (bool) and "panel_valid" (two bools; both default True). Validity is
    kept apart from pass/fail (review R1 #3): an item is agreed only when both panel assessments
    are valid and decide the same; the checker matches only when its assessment is valid and
    equals that decision; invalid items stay in the drawn denominator. Main (luna): (1) panel
    agrees on < 75% of drawn items or < 100 agreed -> "stop"; (2) checker matches >= 85% of
    agreed -> "checker"; (3) "panel". Fallback (qwn): (1) < 20 agreed (incl. none drawn) or
    agreement < 75% -> "triple"; (2) -> "checker"; (3) "panel"."""
    if writer not in STRATA:
        raise ValueError(f"unknown writer stratum {writer!r}")
    n_drawn = len(labels)
    agreed = [x for x in labels if label_agreed(x)]
    n_agreed = len(agreed)
    n_match = sum(1 for x in agreed
                  if x.get("checker_valid", True) and x["checker"] == x["panel"][0])
    agree_rate = n_agreed / n_drawn if n_drawn else 0.0
    match_rate = n_match / n_agreed if n_agreed else 0.0
    need = STRATA[writer]["min_agreed"]
    low = agree_rate < C.CHECK_PANEL_AGREE_MIN or n_agreed < need
    if low:
        policy, rule = ("stop" if writer == "luna" else "triple"), 1
    elif match_rate >= C.CHECK_MATCH_MIN:
        policy, rule = "checker", 2
    else:
        policy, rule = "panel", 3
    assert policy in C.CHECK_POLICIES
    return {
        "writer": writer, "stratum": STRATA[writer]["name"],
        "panel": list(C.ROLES[STRATA[writer]["panel_role"]]), "policy": policy, "rule": rule,
        "n_drawn": n_drawn, "n_agreed": n_agreed, "panel_agree_rate": agree_rate,
        "n_checker_match": n_match, "checker_match_rate": match_rate, "min_agreed": need,
        "n_panel_pass_agreed": sum(1 for x in agreed if x["panel"][0]),
        "n_checker_pass_drawn": sum(1 for x in labels if x["checker"]),
        "n_panel_invalid": sum(1 for x in labels if not all(x.get("panel_valid", (True, True)))),
        "n_checker_invalid": sum(1 for x in labels if not x.get("checker_valid", True)),
    }


def label_agreed(x: dict) -> bool:
    """Both panel assessments valid and deciding the same (review R1 #3)."""
    return all(x.get("panel_valid", (True, True))) and x["panel"][0] == x["panel"][1]


def apply_policy(policy: str, checker_pass: bool, panel_passes: Sequence[bool] | None) -> bool:
    """An output's acceptance under its writer's frozen policy (§4.4)."""
    if policy == "checker":
        return checker_pass
    if panel_passes is None or len(panel_passes) != 2:
        raise ValueError(f"policy {policy!r} needs both panel decisions")
    if policy == "panel":
        return all(panel_passes)
    if policy == "triple":
        return checker_pass and all(panel_passes)
    raise ValueError(f"no output is decided under policy {policy!r}")


def insert_order_key(doc_id: str, seed: int = C.CORPUS_SEED) -> str:
    """The seeded insert order: documents sorted by sha256(f"{seed}:{doc_id}"). A hash order, so
    adding or dropping one document never reorders the others (§4.4 step 3)."""
    return hashlib.sha256(f"{seed}:{doc_id}".encode()).hexdigest()


# --- errors -------------------------------------------------------------------------------------


class ResumableAbort(RuntimeError):
    """Work without a terminal answer (an API error or cut-off in the llm layer or the rewriter):
    abort before recording any document outcome; rerunning resends exactly that work."""


class PolicyEvidenceError(RuntimeError):
    """Frozen checker policies whose recorded evidence does not match the current inputs
    (review R1 #4): refused before any paid call that would rely on them."""


class DirtySourceError(RuntimeError):
    """Uncommitted changes under src/ or scripts/ and allow_dirty was not given (R1 #12, r2 #7)."""


# --- the exclusion registry (Amendment F) -------------------------------------------------------

EXCLUSIONS_FILE = "exclusions.json"
EXCLUSION_REASONS: tuple[str, ...] = ("minor_sexual", "a5_unscored")


def exclusions_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else ROOT / C.INSERT_DIR / EXCLUSIONS_FILE


def load_exclusions(path: Path | str | None = None) -> dict[str, list[dict]]:
    """The shared exclusion registry, data/E9/insert/exclusions.json: {doc_id: [entries]}, each
    entry {"doc_id", "reason", "source", "utc"}. Missing file -> no exclusions."""
    p = exclusions_path(path)
    if not p.exists():
        return {}
    entries = json.loads(p.read_text())["entries"]
    out: dict[str, list[dict]] = {}
    for e in entries:
        out.setdefault(e["doc_id"], []).append(e)
    return out


def add_exclusions(doc_ids: Iterable[str], reason: str, source: str, *,
                   path: Path | str | None = None) -> list[dict]:
    """Append exclusions (append-only, utc-stamped, under a file lock; an identical
    (doc_id, reason, source) entry is not repeated). An excluded document is never rewritten
    again and is dropped from every arm and replaced (Amendment F; §4.3 safety exclusion).
    Returns the entries added."""
    import fcntl

    if reason not in EXCLUSION_REASONS:
        raise ValueError(f"unknown exclusion reason {reason!r}; known: {EXCLUSION_REASONS}")
    if not source:
        raise ValueError("an exclusion needs its source")
    p = exclusions_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_name(p.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = json.loads(p.read_text())["entries"] if p.exists() else []
        have = {(e["doc_id"], e["reason"], e["source"]) for e in entries}
        utc = _utc()
        new = [{"doc_id": d, "reason": reason, "source": source, "utc": utc}
               for d in dict.fromkeys(doc_ids) if (d, reason, source) not in have]
        if new:
            _write_json(p, {"entries": entries + new})
    return new


# --- adapters to the parallel units -------------------------------------------------------------


class _LLM:
    """The only place that knows `emx.e9.llm.run_batch`'s request and row shapes (unit B1).

    Requests are {"item_id", "fields", "seed": JUDGE_REQUEST_SEED}; rows carry "item_id",
    "status" and "parsed" (None unless status "ok"). Results are cached per (model key, prompt id,
    item id); each batch writes to one resumable JSONL per (model, prompt) under `out_dir`
    (checks_<model>_<prompt>.jsonl), ledger note E9:setup:checks:<model>-<prompt>-<hash>."""

    def __init__(self, run_batch=None, out_dir: Path | str = C.LLM_DIR,
                 stage: str = STAGE):
        if run_batch is None:
            from emx.e9.llm import run_batch
        self.run_batch = run_batch
        self.out_dir = Path(out_dir)
        self.stage = stage
        self.cache: dict[tuple[str, str, str], dict | None] = {}
        self.statuses: Counter = Counter()

    def json(self, model_key: str, prompt_id: str, items: Sequence[tuple[str, dict]]) -> dict:
        todo: dict[str, dict] = {}
        for item_id, fields in items:
            if (model_key, prompt_id, item_id) not in self.cache:
                todo.setdefault(item_id, fields)
        if todo:
            ids = sorted(todo)
            h = hashlib.sha256("\n".join(ids).encode()).hexdigest()[:12]
            note = C.ledger_note(self.stage, "checks", f"{model_key}-{prompt_id}-{h}")
            reqs = [{"item_id": i, "fields": todo[i], "seed": C.JUDGE_REQUEST_SEED} for i in ids]
            out_path = self.out_dir / f"checks_{model_key}_{prompt_id}.jsonl"
            rows = self.run_batch(model_key, prompt_id, reqs, out_path, mode="json",
                                  stage=self.stage, note=note)
            by_id = {r["item_id"]: r for r in rows}
            missing = [i for i in ids if i not in by_id]
            if missing:  # never a silent check failure
                raise ResumableAbort(f"run_batch returned no row for {len(missing)} of {len(ids)} "
                                     f"{model_key}/{prompt_id} items, e.g. {missing[0]}")
            errors = [i for i in ids if by_id[i].get("status") not in C.LLM_STATUSES[:4]]
            if errors:  # "error" is not an answer (llm re-sends it next run): never a failed check
                raise ResumableAbort(f"{len(errors)} {model_key}/{prompt_id} calls ended in API "
                                     f"errors (rerun to resend), e.g. {errors[0]}")
            for i in ids:
                r = by_id[i]
                status = r.get("status")
                self.statuses[(model_key, prompt_id, status)] += 1
                # malformed after retries, refused or empty -> None -> an invalid assessment
                self.cache[(model_key, prompt_id, i)] = (
                    r.get("parsed") if status == OK_STATUS else None)
        return {i: self.cache[(model_key, prompt_id, i)] for i, _ in items}


@dataclass
class Output:
    """One written version of a document (a REWRITE_ATTEMPT_KEYS row from unit B2a)."""

    doc_id: str
    version: str
    attempt: int
    writer: str
    seed: int | None
    status: str
    text: str | None
    n_tokens: int | None

    @property
    def key(self) -> str:
        return f"{self.doc_id}|{self.version}|a{self.attempt}|{self.writer}"

    @property
    def usable(self) -> bool:
        return self.status == OK_STATUS and bool(self.text)


REGEN_ATTEMPT = 2  # B2a: attempt 1 = steps 1-2 (luna, then Qwen), attempt 2 = the regeneration
UNRESOLVED = "unresolved"  # B2a's explicit "no terminal answer yet" entry


class _Rewriter:
    """The only place that knows unit B2a's call and return shapes (`emx.e9.rewrite`).

    write_versions(docs, versions, *, stage, batch, tok, exclusions, [run_batch, out_dir,
    llm_dir]) and regenerate([(doc_id, version, writer)], *, docs, ...) return, for every
    requested (doc_id, version), the standing attempt record (REWRITE_ATTEMPT_KEYS) or
    {"status": "unresolved", "reason"}; load_attempts(out_dir) returns every recorded attempt."""

    def __init__(self, write_versions=None, regenerate=None, load_attempts=None, *,
                 run_batch=None, tok=None, out_dir: Path | None = None,
                 llm_dir: Path | None = None, stage: str = STAGE):
        if write_versions is None or regenerate is None or load_attempts is None:
            from emx.e9 import rewrite

            write_versions = write_versions or rewrite.write_versions
            regenerate = regenerate or rewrite.regenerate
            load_attempts = load_attempts or rewrite.load_attempts
        self._write, self._regen, self._load = write_versions, regenerate, load_attempts
        self.run_batch, self.tok, self.out_dir, self.llm_dir = run_batch, tok, out_dir, llm_dir
        self.stage = stage

    @staticmethod
    def rows(result) -> list[dict]:
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for k in ("attempts", "rows", "outputs"):
                if isinstance(result.get(k), list):
                    return result[k]
            if all(isinstance(v, dict) for v in result.values()):
                return list(result.values())
        raise TypeError(f"unrecognized rewrite result of type {type(result).__name__}")

    def _kw(self, batch: str, exclusions: Iterable[str]) -> dict:
        kw = {"stage": self.stage, "batch": batch, "tok": self.tok,
              "exclusions": sorted(exclusions)}
        for k in ("run_batch", "out_dir", "llm_dir"):
            if getattr(self, k) is not None:
                kw[k] = getattr(self, k)
        return kw

    def write(self, docs: list[dict], batch: str, exclusions: Iterable[str]) -> list[dict]:
        return self.rows(self._write(docs, ("corr", "para"), **self._kw(batch, exclusions)))

    def regenerate(self, failed: list[tuple[str, str, str]], docs: list[dict], batch: str,
                   exclusions: Iterable[str]) -> list[dict]:
        return self.rows(self._regen(failed, docs=docs, **self._kw(batch, exclusions)))

    def attempts(self) -> list[dict]:
        res = self._load(self.out_dir) if self.out_dir is not None else self._load()
        return list(res.values()) if isinstance(res, dict) else list(res)


def latest_outputs(rows: Iterable[dict], keys: set[tuple[str, str]]) -> dict:
    """The latest attempt row per (doc_id, version) among `keys` (highest attempt; for equal
    attempts the later row, e.g. the Qwen fallback after a luna refusal). Raises
    ResumableAbort if any requested key is missing or unresolved (review R1 #1)."""
    rows = list(rows)
    unresolved = sorted({(r["doc_id"], r["version"]) for r in rows
                         if r.get("status") == UNRESOLVED} & keys)
    best: dict[tuple[str, str], tuple[int, int, dict]] = {}
    for i, r in enumerate(rows):
        k = (r["doc_id"], r["version"])
        if k not in keys or r.get("status") == UNRESOLVED:
            continue
        rank = (int(r.get("attempt") or 0), i)
        if k not in best or rank >= best[k][:2]:
            best[k] = (*rank, r)
    missing = sorted(keys - set(best) - set(unresolved))
    if unresolved or missing:
        raise ResumableAbort(
            f"no terminal rewrite for {len(unresolved)} unresolved and {len(missing)} missing "
            f"versions (API errors or cut-offs: rerun), e.g. {(unresolved or missing)[0]}")
    return {
        k: Output(doc_id=r["doc_id"], version=r["version"], attempt=int(r.get("attempt") or 0),
                  writer=r["writer"], seed=r.get("seed"), status=r.get("status", ""),
                  text=r.get("text"), n_tokens=r.get("n_tokens"))
        for k, (_, _, r) in best.items()
    }


# --- evidence binding (review R1 #4, #12) -------------------------------------------------------


def _sha_json(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def static_evidence(selection: Sequence[str], docs: dict[str, dict]) -> dict:
    """What the frozen policies depend on before any output exists: the selection (ids and the
    sha256 of each text, rank order), the A.1/A.4 prompt sha256s, every checker/panel model's id
    and request configuration, the judge request seed and the validation seed."""
    from emx.e9 import prompts

    keys = sorted({C.ROLES["checker"], *C.ROLES["checker_panel_luna"],
                   *C.ROLES["checker_panel_qwn"]})
    return {
        "selection_sha256": _sha_json([[d, text_sha256(docs[d]["text"])] for d in selection]),
        "prompts": {p: prompts.sha256(p) for p in ("A1_content", "A4_preserve")},
        "models": {k: {"model": C.MODELS[k].model, "route": C.MODELS[k].route,
                       "request": C.MODELS[k].request,
                       "json_max_tokens": C.MODELS[k].json_max_tokens} for k in keys},
        "judge_seed": C.JUDGE_REQUEST_SEED,
        "validation_seed": C.VALIDATION_SEED,
    }


def first_pass_fingerprint(attempts: Iterable[dict], doc_ids: Iterable[str]) -> str:
    """sha256 over every recorded attempt-1 rewrite of the first-pass documents (doc, version,
    attempt, writer, status, seed, sha256 of the text); B2a never re-sends a recorded attempt,
    so it is stable across reruns."""
    ids = set(doc_ids)
    rows = sorted(([r["doc_id"], r["version"], int(r.get("attempt") or 0), r["writer"],
                    r.get("status"), r.get("seed"),
                    text_sha256(r["text"]) if r.get("text") else None]
                   for r in attempts
                   if r["doc_id"] in ids and int(r.get("attempt") or 0) == 1),
                  key=lambda x: x[:4])
    return _sha_json(rows)


SOURCE_DIRS: tuple[str, ...] = ("src", "scripts")
SOURCE_CODE_SUFFIXES: tuple[str, ...] = (".py", ".sh")


def _git_dirty(root: Path) -> list[tuple[str, str]]:
    """(status, path) of every change under src/ and scripts/: any tracked file modified,
    staged, deleted or renamed, plus untracked code files (.py, .sh), which imports could load
    but the commit would not contain. `git status --porcelain -z`, renames parsed."""
    out = subprocess.check_output(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all", "--", *SOURCE_DIRS],
        cwd=root, text=True)
    parts = out.split("\0")
    dirty, i = [], 0
    while i < len(parts):
        ent = parts[i]
        i += 1
        if not ent:
            continue
        st, path = ent[:2], ent[3:]
        if "R" in st or "C" in st:
            i += 1  # -z puts the rename's origin path in the next field
        if st == "??" and not path.endswith(SOURCE_CODE_SUFFIXES):
            continue
        if "__pycache__" in Path(path).parts:
            continue
        dirty.append((st, path))
    return dirty


def source_state(root: Path = ROOT) -> dict:
    """The code that runs (review R1 r2 #7): the commit (always recorded), and every
    uncommitted change under src/ and scripts/ (tracked files modified, staged, deleted or
    renamed; untracked .py/.sh files), each with the sha256 of its current content ("deleted"
    if gone). `snapshot_sha256` hashes that {path: sha256} map, so commit + snapshot identify the
    executed tree. `dirty` is true if any such change exists or git is unavailable."""
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                         text=True).strip()
        changes = _git_dirty(root)
    except (OSError, subprocess.CalledProcessError) as e:
        return {"commit": "unknown", "dirty": True, "dirty_paths": [f"<git unavailable: {e}>"],
                "dirty_files": {}, "snapshot_sha256": None, "n_files": 0}
    files = {}
    for st, path in changes:
        p = root / path
        files[path] = _sha256_file(p) if p.is_file() else "deleted"
    return {"commit": commit, "dirty": bool(files), "dirty_paths": sorted(files)[:50],
            "dirty_status": {path: st for st, path in changes},
            "dirty_files": files, "snapshot_sha256": _sha_json(files), "n_files": len(files)}


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def evidence_inventory(paths: Paths) -> dict[str, str]:
    """{path: sha256} of the acceptance evidence (R1 #12): the rewrite attempts and their llm
    logs, the checker/panel responses, the check rows, the validation samples, the policy file,
    the exclusion registry and the standing outputs acceptance saw."""
    required = [paths.rewrite_dir / "checks.jsonl", paths.rewrite_dir / "accept_outputs.jsonl",
                paths.rewrite_dir / "checker_policies.json"]
    for p in required:
        if not p.exists():
            raise FileNotFoundError(f"evidence file missing: {p}")
    files = [*required, *sorted(paths.rewrite_dir.glob("checker_validation_*.jsonl")),
             paths.rewrite_dir / "attempts.jsonl", paths.insert_dir / EXCLUSIONS_FILE,
             *sorted(paths.llm_dir.glob("checks_*.jsonl")),
             *sorted(paths.llm_dir.glob("rewrite-*.jsonl"))]
    return {_rel(p): _sha256_file(p) for p in files if p.exists()}


def verify_manifest(manifest_path: Path | str) -> list[str]:
    """Recompute every hash a frozen manifest lists (inputs, evidence, versions); returns the
    problems found (empty = verified). Downstream units call this before use."""
    mp = Path(manifest_path)
    man = json.loads(mp.read_text())
    problems = []
    for group in ("inputs", "evidence"):
        for rel, sha in man[group].items():
            p = Path(rel) if Path(rel).is_absolute() else ROOT / rel
            if not p.exists():
                problems.append(f"{group}: missing {rel}")
            elif _sha256_file(p) != sha:
                problems.append(f"{group}: sha256 mismatch {rel}")
    vp = mp.with_name("versions.jsonl")
    if not vp.exists() or _sha256_file(vp) != man.get("versions_sha256"):
        problems.append("versions.jsonl missing or sha256 mismatch")
    return problems


# --- the acceptance driver ----------------------------------------------------------------------


@dataclass
class DocResult:
    doc_id: str
    passed: bool
    reason: str | None
    outputs: dict[str, Output] = field(default_factory=dict)  # the final corr and para


def _ids(text: str, tok) -> list[int]:
    return list(tok(text, add_special_tokens=False)["input_ids"])


class Acceptor:
    """Holds the run's state: checks, frozen policies, exclusions, per-document results (§4.4)."""

    def __init__(self, docs: dict[str, dict], *, llm: _LLM, rewriter: _Rewriter, tok,
                 policies_path: Path | None = None, validation_dir: Path | None = None,
                 exclusions_file: Path | None = None, static: dict | None = None,
                 batch_prefix: str = ""):
        self.docs = docs
        self.llm = llm
        self.rw = rewriter
        self.tok = tok
        self.policies_path = policies_path
        self.validation_dir = validation_dir
        self.exclusions_file = exclusions_file
        self.static = static
        self.batch_prefix = batch_prefix
        self.policies: dict[str, dict] | None = None
        if policies_path is not None and Path(policies_path).exists():
            self.policies = json.loads(Path(policies_path).read_text())
            self._verify_loaded()
        self.excluded = load_exclusions(exclusions_file)
        self.results: dict[str, DocResult] = {}
        self.attempt_rows: list[dict] = []
        self.check_rows: list[dict] = []
        self.decisions: dict[tuple[str, str], bool] = {}  # (judge key, output key) -> pass
        self.valid: dict[tuple[str, str], bool] = {}  # (judge key, output key) -> valid answer
        self.accepted: dict[str, bool] = {}  # output key -> accepted under policy
        self.flags: dict[str, str] = {}  # doc_id -> source of its first minor_sexual flag
        self._geom: dict[str, tuple[float, float]] = {}  # output key -> (ratio, edit distance)
        self._rounds = 0  # rewrite batch names: "pass1", then "repl1", "repl2", ...
        self._orig_ids: dict[str, list[int]] = {}
        self._first_pass: tuple[list[str], str] | None = None

    # evidence ----------------------------------------------------------------------------------

    def _verify_loaded(self) -> None:
        """Frozen policies must match the current static evidence and their validation files
        (R1 #4); called on load, before any call."""
        ev = self.policies.get("evidence")
        if not ev:
            raise PolicyEvidenceError(f"{self.policies_path} records no evidence")
        if self.static is not None:
            bad = [k for k, v in self.static.items() if ev.get(k) != v]
            if bad:
                raise PolicyEvidenceError(f"frozen checker policies were set under different "
                                          f"{bad}; refusing")
        for writer, sha in ev.get("validation_sha256", {}).items():
            p = Path(self.validation_dir or ".") / f"checker_validation_{writer}.jsonl"
            if not p.exists() or _sha256_file(p) != sha:
                raise PolicyEvidenceError(f"validation sample {p} is missing or changed")

    def _verify_first_pass(self, new: list[str]) -> None:
        """Record (fresh run) or verify (frozen policies) the first-pass output fingerprint,
        before any checker call."""
        if self.policies is None:
            self._first_pass = (list(new), first_pass_fingerprint(self.rw.attempts(), new))
            return
        ev = self.policies["evidence"]
        fp = first_pass_fingerprint(self.rw.attempts(), ev["first_pass_doc_ids"])
        if fp != ev["first_pass_sha256"]:
            raise PolicyEvidenceError("the first-pass rewrites differ from those the frozen "
                                      "checker policies were validated on; refusing")

    # exclusions --------------------------------------------------------------------------------

    def is_excluded(self, doc_id: str) -> bool:
        return doc_id in self.excluded

    def exclusion_reason(self, doc_id: str) -> str:
        return "excluded:" + ",".join(sorted({e["reason"] for e in self.excluded[doc_id]}))

    def reload_exclusions(self) -> None:
        self.excluded = load_exclusions(self.exclusions_file)

    def _flag(self, doc_id: str, source: str) -> None:
        """A positive minor_sexual flag from any A.1 assessment (R1 #10): persist the exclusion."""
        self.flags.setdefault(doc_id, source)
        add_exclusions([doc_id], "minor_sexual", source, path=self.exclusions_file)
        self.reload_exclusions()

    # checks ------------------------------------------------------------------------------------

    def _geometry(self, out: Output) -> tuple[float, float]:
        if out.key not in self._geom:
            doc = self.docs[out.doc_id]
            if out.doc_id not in self._orig_ids:
                self._orig_ids[out.doc_id] = _ids(doc["text"], self.tok)
            ids = _ids(out.text, self.tok)
            n_out = out.n_tokens if out.n_tokens is not None else len(ids)
            self._geom[out.key] = (length_ratio(n_out, doc["n_tokens"]),
                                   token_edit_distance(self._orig_ids[out.doc_id], ids))
        return self._geom[out.key]

    def judge(self, model_key: str, outs: Sequence[Output]) -> None:
        """A.1 on each original (once per model) and output, A.4 on (original, output); one
        CHECK_KEYS row per usable output and model; pass/fail and validity cached separately;
        any positive minor_sexual flag excludes the document."""
        outs = [o for o in outs if o.usable and (model_key, o.key) not in self.decisions]
        if not outs:
            return
        a1_items = [(f"orig|{o.doc_id}", {"document": self.docs[o.doc_id]["text"]}) for o in outs]
        a1_items += [(o.key, {"document": o.text}) for o in outs]
        a1 = self.llm.json(model_key, "A1_content", a1_items)
        a4 = self.llm.json(model_key, "A4_preserve", [
            (o.key, {"original": self.docs[o.doc_id]["text"], "rewrite": o.text}) for o in outs])
        for o in outs:
            for item, what in ((f"orig|{o.doc_id}", "orig"), (o.key, o.key)):
                p = a1[item]
                if isinstance(p, dict) and p.get("minor_sexual") is True:
                    self._flag(o.doc_id, f"{model_key}:A1_content:{what}")
            e_orig = parse_endorsement(a1[f"orig|{o.doc_id}"])
            e_out = parse_endorsement(a1[o.key])
            a4o = parse_a4(a4[o.key])
            ratio, edit = self._geometry(o)
            passed = table_pass(o.version, e_orig, e_out, a4o, ratio, edit)
            needs_orig = o.version == "para"
            malformed = e_out is None or a4o is None or (needs_orig and e_orig is None)
            self.decisions[(model_key, o.key)] = passed
            self.valid[(model_key, o.key)] = not malformed
            self.check_rows.append({
                "doc_id": o.doc_id, "version": o.version, "attempt": o.attempt,
                "writer": o.writer, "judge_key": model_key, "endorse_orig": e_orig,
                "endorse_out": e_out,
                **{k: (a4o or {}).get(k) for k in ("same_topic", "same_entities",
                                                   "facts_preserved")},
                "length_ratio": ratio, "edit_distance": edit, "passed": passed,
                "status": "malformed" if malformed else "ok", "output_key": o.key,
            })

    def _panel(self, writer: str) -> tuple[str, str]:
        return tuple(C.ROLES[STRATA[writer]["panel_role"]])

    def _check_all(self, outs: Sequence[Output]) -> None:
        for o in outs:
            if o.writer not in STRATA:
                raise ValueError(f"unknown writer {o.writer!r} for {o.key}")
        self.judge(C.ROLES["checker"], outs)
        for o in outs:
            if not o.usable:
                self.check_rows.append({
                    "doc_id": o.doc_id, "version": o.version, "attempt": o.attempt,
                    "writer": o.writer, "judge_key": None, "endorse_orig": None,
                    "endorse_out": None, "same_topic": None, "same_entities": None,
                    "facts_preserved": None, "length_ratio": None, "edit_distance": None,
                    "passed": False, "status": f"no_output:{o.status}", "output_key": o.key,
                })

    # validation --------------------------------------------------------------------------------

    def validate(self, first_pass: Sequence[Output]) -> dict:
        """Sample each writer stratum to its endpoint, evaluate its policy table once, freeze the
        policies with their evidence (R1 #4)."""
        if self.policies is not None:
            raise RuntimeError("checker policies are already frozen")
        if self._first_pass is None:
            raise RuntimeError("the first-pass fingerprint must be recorded before validation")
        policies: dict = {}
        val_sha = {}
        checker = C.ROLES["checker"]
        for writer in STRATA:
            outs = {o.key: o for o in first_pass if o.writer == writer and o.usable}
            panel = self._panel(writer)

            def label(keys: list[str], outs=outs, panel=panel) -> dict[str, bool]:
                for m in panel:
                    self.judge(m, [outs[k] for k in keys])
                return {k: label_agreed(self._label(k, panel)) for k in keys}

            draw = sample_to_endpoint([(k, o.version) for k, o in outs.items()],
                                      STRATA[writer]["min_agreed"], label)
            labels = [{"key": k, "version": outs[k].version,
                       "checker": self.decisions[(checker, k)],
                       "checker_valid": self.valid[(checker, k)], **self._label(k, panel)}
                      for k in draw["drawn"]]
            pol = evaluate_policy(writer, labels)
            pol["sampling"] = {k: draw[k] for k in ("batches", "endpoint", "n_items", "seed")}
            policies[writer] = pol
            vp = Path(self.validation_dir or ".") / f"checker_validation_{writer}.jsonl"
            _write_jsonl(vp, [x | {"panel": list(x["panel"]),
                                   "panel_valid": list(x["panel_valid"])} for x in labels])
            val_sha[writer] = _sha256_file(vp)
        ids, fp = self._first_pass
        policies["evidence"] = {**(self.static or {}), "first_pass_doc_ids": ids,
                                "first_pass_sha256": fp, "validation_sha256": val_sha}
        policies["frozen_utc"] = _utc()
        self.policies = policies
        if self.policies_path is not None:
            _write_json(Path(self.policies_path), policies)
        return policies

    def _label(self, k: str, panel: tuple[str, str]) -> dict:
        return {"panel": (self.decisions[(panel[0], k)], self.decisions[(panel[1], k)]),
                "panel_valid": (self.valid[(panel[0], k)], self.valid[(panel[1], k)])}

    # decisions ---------------------------------------------------------------------------------

    def decide(self, outs: Sequence[Output]) -> dict[str, bool]:
        """Accept or reject each output under its writer's frozen policy. In live acceptance an
        invalid assessment counts as that model failing the output."""
        if self.policies is None:
            raise RuntimeError("no output is decided before the checker policies are frozen")
        if self.policies["luna"]["policy"] == "stop":
            raise Stop("checker_validation_main_stratum", policy=self.policies["luna"])
        for writer in STRATA:
            pol = self.policies[writer]["policy"]
            mine = [o for o in outs if o.writer == writer and o.usable]
            if pol in ("panel", "triple"):
                for m in self._panel(writer):
                    self.judge(m, mine)
        out = {}
        checker = C.ROLES["checker"]
        for o in outs:
            if not o.usable:
                out[o.key] = False
                continue
            pol = self.policies[o.writer]["policy"]
            panel = None
            if pol in ("panel", "triple"):
                panel = tuple(self.decisions[(m, o.key)] for m in self._panel(o.writer))
            out[o.key] = apply_policy(pol, self.decisions[(checker, o.key)], panel)
        self.accepted.update(out)
        return out

    # per-document pipeline ---------------------------------------------------------------------

    def process(self, doc_ids: Sequence[str], *, first_pass: bool = False) -> None:
        """Write, check and decide both versions of each new document; regenerate failing
        attempt-1 outputs once (same writer, next seed, by unit B2a); a document passes only if
        both versions do and it is not excluded. Document outcomes are recorded only after every
        outstanding attempt has a terminal answer (ResumableAbort otherwise; R1 #1)."""
        new = [d for d in doc_ids if d not in self.results]
        for d in [d for d in new if self.is_excluded(d)]:  # never rewritten (again)
            self.results[d] = DocResult(d, False, self.exclusion_reason(d), {})
        new = [d for d in new if not self.is_excluded(d)]
        if not new:
            return
        batch = self.batch_prefix + ("pass1" if first_pass else f"repl{self._rounds}")
        self._rounds += 1
        keys = {(d, v) for d in new for v in ("corr", "para")}
        new_docs = [self.docs[d] for d in new]
        rows = self.rw.write(new_docs, batch, self.excluded)
        self.attempt_rows.extend(rows)
        outs = latest_outputs(rows, keys)
        if first_pass:
            self._verify_first_pass(new)
        self._check_all(list(outs.values()))
        if self.policies is None:
            if not first_pass:
                raise RuntimeError("policies must be frozen after the first rewriting pass")
            self.validate(list(outs.values()))
        ok = self.decide(list(outs.values()))
        # attempt-2 outputs (a regeneration already recorded and now checked) are terminal;
        # excluded documents are never rewritten again
        regen_due = [o for o in outs.values() if not ok[o.key] and o.attempt < REGEN_ATTEMPT
                     and not self.is_excluded(o.doc_id)]
        if regen_due:
            rkeys = {(o.doc_id, o.version) for o in regen_due}
            regen_rows = self.rw.regenerate(
                sorted((o.doc_id, o.version, o.writer) for o in regen_due),
                [self.docs[d] for d in sorted({o.doc_id for o in regen_due})], batch,
                self.excluded)
            self.attempt_rows.extend(regen_rows)
            regen = latest_outputs(regen_rows, rkeys)
            wrong = [k for k, o in regen.items() if o.attempt != REGEN_ATTEMPT]
            if wrong:
                raise ResumableAbort(f"regeneration returned no attempt {REGEN_ATTEMPT} for "
                                     f"{len(wrong)} versions, e.g. {wrong[0]}")
            self._check_all(list(regen.values()))
            ok.update(self.decide(list(regen.values())))
            outs.update(regen)
        for d in new:
            if self.is_excluded(d):
                self.results[d] = DocResult(d, False, self.exclusion_reason(d), {})
                continue
            fails = [v for v in ("corr", "para") if not ok[outs[(d, v)].key]]
            reason = None if not fails else "failed_checks:" + ",".join(fails)
            self.results[d] = DocResult(d, not fails, reason,
                                        {v: outs[(d, v)] for v in ("corr", "para")})


def _fill(acc: Acceptor, kept: list[str], queue: list[str], dropped: list[dict],
          rank: dict[str, int], target: int) -> list[str]:
    """Replacement loop (§4.4): add next-ranked candidates (same steps) until single-copy
    original tokens total >= target, the crossing document kept. Candidates are taken in rank
    order, just enough to cover the shortfall if all pass, so exactly the documents a one-by-one
    loop would process are processed. Returns the remaining queue (processed-but-unneeded passing
    candidates stay at its front). Raises Stop if the ranking is exhausted."""
    total = sum(acc.docs[d]["n_tokens"] for d in kept)
    while total < target:
        take, cum = [], 0
        for d in queue:
            take.append(d)
            cum += acc.docs[d]["n_tokens"]
            if cum >= target - total:
                break
        if not take:
            raise Stop("ranking_exhausted", kept_tokens=total, target=target,
                       n_kept=len(kept))
        acc.process(take)
        consumed = set()
        for d in take:
            if total >= target:
                break
            consumed.add(d)
            res = acc.results[d]
            if res.passed and not acc.is_excluded(d):
                kept.append(d)
                total += acc.docs[d]["n_tokens"]
            else:
                dropped.append({"doc_id": d, "rank": rank[d], "reason": res.reason,
                                "stage": "replacement"})
        queue = [d for d in queue if d not in consumed]
    return queue


def totals(acc: Acceptor, kept: Sequence[str]) -> dict:
    """Single-copy token totals per version; separators (one end-of-text per document)
    excluded and included."""
    out = {}
    for v in C.VERSIONS:
        n = [acc.docs[d]["n_tokens"] if v == "orig" else _n(acc.results[d].outputs[v], acc)
             for d in kept]
        out[v] = {"n_docs": len(n), "tokens": int(sum(n)), "tokens_with_sep": int(sum(n) + len(n))}
    return out


def _n(o: Output, acc: Acceptor) -> int:
    return o.n_tokens if o.n_tokens is not None else len(_ids(o.text, acc.tok))


def _violations(tot: dict) -> list[tuple[str, float]]:
    """(version, relative deviation) for corr/para totals outside +-10% of the originals'."""
    o = tot["orig"]["tokens"]
    out = []
    for v in ("corr", "para"):
        dev = tot[v]["tokens"] - o
        if abs(dev) > C.TOTAL_TOKENS_TOL * o:
            out.append((v, dev / o))
    return out


def _repair_totals(acc: Acceptor, kept: list[str], queue: list[str], dropped: list[dict],
                   rank: dict[str, int], target: int) -> list[str]:
    """+-10% totals (§4.4, review R1 #2): while a version's total is out of band, remove the
    single document with the most extreme length ratio of that version in the violating
    direction (the version with the larger relative deviation first; ties: lowest-ranked
    first), replenish through the ordinary replacement loop (next-ranked candidates, full
    checks), and recompute both totals. Stops only when the ranking is exhausted."""
    while True:
        viol = _violations(totals(acc, kept))
        if not viol:
            return queue
        v, dev = max(viol, key=lambda x: (abs(x[1]), x[0]))
        sign = 1 if dev > 0 else -1

        def ratio(d: str, v=v) -> float:
            return _n(acc.results[d].outputs[v], acc) / acc.docs[d]["n_tokens"]

        worst = max(kept, key=lambda d, s=sign: (s * ratio(d), rank[d]))
        kept.remove(worst)
        dropped.append({"doc_id": worst, "rank": rank[worst], "stage": "totals_repair",
                        "reason": f"length_repair:{v}:{ratio(worst):.4f}"})
        queue = _fill(acc, kept, queue, dropped, rank, target)


def _drop_excluded(acc: Acceptor, kept: list[str], dropped: list[dict],
                   rank: dict[str, int]) -> bool:
    """Remove kept documents that are now in the registry (raised during this run or added by
    another unit); True if any was removed."""
    acc.reload_exclusions()
    bad = [d for d in kept if acc.is_excluded(d)]
    for d in bad:
        kept.remove(d)
        dropped.append({"doc_id": d, "rank": rank[d], "reason": acc.exclusion_reason(d),
                        "stage": "exclusion"})
    return bool(bad)


# --- loading, writing and the driver ------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Publish `data` at `path` atomically (review R1 r2 #5): a temp file in the same directory,
    flushed and fsynced, then os.replace and a directory fsync. An interruption at any point
    leaves the previous file (if any) intact and readable; the temp file is removed on error."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    _atomic_write_bytes(path, data.encode("utf-8"))


def _write_json(path: Path, obj) -> None:
    _atomic_write_bytes(path, (json.dumps(obj, indent=1, ensure_ascii=False) + "\n").encode())


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_ranking(path: Path) -> list[dict]:
    """ranked.json (unit B1): the full candidate order. Accepts a list (of doc ids or of dicts
    with "doc_id" and "score"/"endorsement"), or a dict holding it under "ranked", "order" or
    "docs". Returns [{"doc_id", "rank", "score"}] in rank order."""
    obj = json.loads(Path(path).read_text())
    if isinstance(obj, dict):
        obj = next((obj[k] for k in ("ranked", "order", "docs") if isinstance(obj.get(k), list)),
                   None)
    if not isinstance(obj, list):
        raise TypeError(f"unrecognized ranking in {path}")
    out = []
    for i, r in enumerate(obj):
        if isinstance(r, str):
            r = {"doc_id": r}
        score = r.get("score", r.get("endorsement"))
        out.append({"doc_id": r["doc_id"], "rank": int(r.get("rank", i)), "score": score})
    out.sort(key=lambda r: r["rank"])
    if len({r["doc_id"] for r in out}) != len(out):
        raise ValueError("duplicate doc ids in the ranking")
    return out


def load_selection(path: Path) -> list[str]:
    """selection.json (unit B1): the initial insert's doc ids, as a list or under "insert",
    "doc_ids" or "docs" (ids or dicts with "doc_id")."""
    obj = json.loads(Path(path).read_text())
    if isinstance(obj, dict):
        if obj.get("outcome") == "stop":
            raise ValueError(f"{path}: the selection stopped Part 1; there is no insert to accept")
        obj = next((obj[k] for k in ("insert", "doc_ids", "docs") if isinstance(obj.get(k), list)),
                   None)
    if not isinstance(obj, list):
        raise TypeError(f"unrecognized selection in {path}")
    return [r if isinstance(r, str) else r["doc_id"] for r in obj]


@dataclass
class Paths:
    p1: Path = Path(C.POOLS_DIR) / "P1.jsonl"
    selection: Path = Path(C.SELECT_DIR) / "selection.json"
    ranked: Path = Path(C.SELECT_DIR) / "ranked.json"
    rewrite_dir: Path = Path(C.REWRITE_DIR)
    insert_dir: Path = Path(C.INSERT_DIR)
    llm_dir: Path = Path(C.LLM_DIR)

    def rooted(self, root: Path) -> Paths:
        return Paths(*(p if p.is_absolute() else root / p for p in
                       (self.p1, self.selection, self.ranked, self.rewrite_dir, self.insert_dir,
                        self.llm_dir)))


def _excl_summary(excl: dict[str, list[dict]]) -> dict[str, list[str]]:
    return {d: sorted({e["reason"] for e in es}) for d, es in sorted(excl.items())}


def accept(
    paths: Paths | None = None,
    *,
    run_batch=None,
    write_versions=None,
    regenerate=None,
    load_attempts=None,
    tok=None,
    target_tokens: int = C.INSERT_TOKENS,
    refreeze: bool = False,
    refreeze_reason: str | None = None,
    allow_dirty: bool = False,
) -> dict:
    """The acceptance driver (§4.4). Returns {"outcome": "frozen", ...} with the manifest and
    versions paths, or {"outcome": "stop", "reason": ...}; both are also written to
    `<rewrite_dir>/accept_outcome.json`. Errors propagate (ResumableAbort: rerun); they are never
    turned into a pass.

    A frozen insert is never overwritten silently: rebuilding needs `refreeze=True`, which is
    allowed only when the exclusion registry changed since the freeze (Amendment F). It re-runs
    acceptance under the frozen policies (cached rewrites and checks), replaces the excluded
    documents, archives the previous manifest and versions under insert/history/, and records
    the previous manifest's sha256 and the reason. Refuses to start if the E9 source tree is
    dirty (any uncommitted change under src/ or scripts/) unless `allow_dirty`, in which case
    every dirty file's sha256 is recorded (`source_state`); the commit is always recorded."""
    paths = (paths or Paths()).rooted(ROOT)
    manifest_path = paths.insert_dir / "insert_manifest.json"
    versions_path = paths.insert_dir / "versions.jsonl"
    excl_file = paths.insert_dir / EXCLUSIONS_FILE
    prev = None
    if manifest_path.exists():
        if not refreeze:
            raise FileExistsError(f"{manifest_path} is frozen; use refreeze after an exclusion")
        prev = json.loads(manifest_path.read_text())
        before = prev.get("exclusions", {}).get("doc_ids", {})
        now = _excl_summary(load_exclusions(excl_file))
        if now == before:
            raise ValueError("the exclusion registry is unchanged since the frozen insert; "
                             "nothing to refreeze")
        added = {d: r for d, r in now.items() if before.get(d) != r}
        auto = f"exclusions changed: {len(added)} document(s) " + json.dumps(added)[:500]
        refreeze_reason = f"{refreeze_reason}; {auto}" if refreeze_reason else auto
    elif refreeze:
        raise FileNotFoundError(f"no frozen insert at {manifest_path} to refreeze")
    source = source_state()
    if source["dirty"] and not allow_dirty:
        raise DirtySourceError(f"uncommitted source changes {source['dirty_paths'][:5]}; "
                               "commit them or pass allow_dirty")
    if tok is None:
        from emx.e9.text import tokenizer

        tok = tokenizer()
    docs = {d["doc_id"]: d for d in _read_jsonl(paths.p1)}
    ranking = load_ranking(paths.ranked)
    selection = load_selection(paths.selection)
    rank = {r["doc_id"]: r["rank"] for r in ranking}
    score = {r["doc_id"]: r["score"] for r in ranking}
    unknown = [d for d in rank if d not in docs]
    if unknown:
        raise ValueError(f"{len(unknown)} ranked doc ids are not in P1, e.g. {unknown[0]}")
    if any(d not in rank for d in selection):
        raise ValueError("selection.json holds doc ids missing from ranked.json")
    selection = sorted(dict.fromkeys(selection), key=rank.__getitem__)
    inputs = {_rel(p): _sha256_file(p) for p in (paths.p1, paths.selection, paths.ranked)}
    n_refreeze = (prev or {}).get("refreeze_count", -1) + 1

    acc = Acceptor(
        docs, llm=_LLM(run_batch, paths.llm_dir),
        rewriter=_Rewriter(write_versions, regenerate, load_attempts, run_batch=run_batch,
                           tok=tok, out_dir=paths.rewrite_dir, llm_dir=paths.llm_dir),
        tok=tok, policies_path=paths.rewrite_dir / "checker_policies.json",
        validation_dir=paths.rewrite_dir, exclusions_file=excl_file,
        static=static_evidence(selection, docs),
        batch_prefix=f"rf{n_refreeze}-" if n_refreeze else "")
    kept: list[str] = []
    dropped: list[dict] = []
    sel = set(selection)
    queue = [r["doc_id"] for r in ranking if r["doc_id"] not in sel]
    try:
        acc.process(selection, first_pass=True)
        for d in selection:
            if acc.results[d].passed and not acc.is_excluded(d):
                kept.append(d)
            else:
                dropped.append({"doc_id": d, "rank": rank[d], "reason": acc.results[d].reason,
                                "stage": "first_pass"})
        while True:
            queue = _fill(acc, kept, queue, dropped, rank, target_tokens)
            queue = _repair_totals(acc, kept, queue, dropped, rank, target_tokens)
            if not _drop_excluded(acc, kept, dropped, rank):
                break
    except Stop as s:
        outcome = {"outcome": "stop", "reason": s.reason, "details": s.details,
                   "policies": acc.policies, "n_kept": len(kept), "dropped": dropped,
                   "refreeze": bool(prev), "utc": _utc(), "source": source, "inputs": inputs}
        _flush(acc, paths)
        _write_json(paths.rewrite_dir / "accept_outcome.json", outcome)
        return outcome

    kept.sort(key=lambda d: insert_order_key(d))
    _flush(acc, paths)
    manifest = build_manifest(acc, kept, dropped, rank, score, inputs, queue)
    manifest["evidence"] = evidence_inventory(paths)
    manifest["source"] = source
    manifest["exclusions"] = {
        "sha256": _sha256_file(excl_file) if excl_file.exists() else None,
        "doc_ids": _excl_summary(acc.excluded), "flags_this_run": acc.flags}
    manifest["refreeze_count"] = n_refreeze
    if prev is not None:
        hist = paths.insert_dir / "history"
        hist.mkdir(parents=True, exist_ok=True)
        prev_sha = _sha256_file(manifest_path)
        arch_m = hist / f"insert_manifest.{prev_sha[:12]}.json"
        arch_v = hist / f"versions.{prev_sha[:12]}.jsonl"
        _atomic_write_bytes(arch_m, manifest_path.read_bytes())
        if versions_path.exists():
            _atomic_write_bytes(arch_v, versions_path.read_bytes())
        manifest["previous"] = {"manifest_sha256": prev_sha,
                                "versions_sha256": prev.get("versions_sha256"),
                                "archived": [_rel(arch_m), _rel(arch_v)],
                                "reason": refreeze_reason}
    _write_jsonl(versions_path, build_version_rows(acc, kept))
    manifest["versions_sha256"] = _sha256_file(versions_path)
    _write_json(manifest_path, manifest)
    outcome = {"outcome": "frozen", "reason": refreeze_reason, "manifest": str(manifest_path),
               "manifest_sha256": _sha256_file(manifest_path), "versions": str(versions_path),
               "versions_sha256": manifest["versions_sha256"], "n_docs": len(kept),
               "totals": manifest["totals"], "utc": manifest["utc"]}
    _write_json(paths.rewrite_dir / "accept_outcome.json", outcome)
    return outcome


def _flush(acc: Acceptor, paths: Paths) -> None:
    _write_jsonl(paths.rewrite_dir / "checks.jsonl", acc.check_rows)
    _write_jsonl(paths.rewrite_dir / "accept_outputs.jsonl", acc.attempt_rows)


def build_version_rows(acc: Acceptor, kept: Sequence[str]) -> list[dict]:
    """VERSION_KEYS rows for orig, corr and para of every kept document, in insert order;
    originals' text from P1 (writer and seed None, attempt 0, as rewrite.orig_version_row)."""
    rows = []
    for d in kept:
        doc = acc.docs[d]
        rows.append({"doc_id": d, "version": "orig", "text": doc["text"],
                     "n_tokens": doc["n_tokens"], "sha256": text_sha256(doc["text"]),
                     "writer": None, "attempt": 0, "seed": None})
        for v in ("corr", "para"):
            o = acc.results[d].outputs[v]
            rows.append({"doc_id": d, "version": v, "text": o.text, "n_tokens": _n(o, acc),
                         "sha256": text_sha256(o.text), "writer": o.writer,
                         "attempt": o.attempt, "seed": o.seed})
    return rows


def _refusal_counts(rows: Iterable[dict]) -> dict:
    """Counts of every attempt status per (writer, attempt): refusals at each sequence step."""
    c = Counter(f"{r['writer']}:a{r.get('attempt')}:{r.get('status')}" for r in rows)
    return dict(sorted(c.items()))


def build_manifest(acc: Acceptor, kept: Sequence[str], dropped: list[dict],
                   rank: dict[str, int], score: dict, inputs: dict, queue: Sequence[str]) -> dict:
    """The frozen insert manifest (INSERT_MANIFEST_KEYS minus "capacity", added by unit C)."""
    docs_out = []
    for d in kept:
        doc, res = acc.docs[d], acc.results[d]
        ent = {"doc_id": d, "rank": rank[d], "score": score[d], "group": None,
               "topic": doc.get("topic"), "source_dir": doc.get("source_dir"),
               "n_tokens": {"orig": doc["n_tokens"]}, "sha256": {"orig": text_sha256(doc["text"])},
               "writer": {}, "attempt": {}, "length_ratio": {}, "edit_distance": {}}
        for v in ("corr", "para"):
            o = res.outputs[v]
            ratio, edit = acc._geometry(o)
            ent["n_tokens"][v] = _n(o, acc)
            ent["sha256"][v] = text_sha256(o.text)
            ent["writer"][v] = o.writer
            ent["attempt"][v] = o.attempt
            ent["length_ratio"][v] = ratio
            ent["edit_distance"][v] = edit
        docs_out.append(ent)
    all_attempts = [r for r in acc.rw.attempts() if r["doc_id"] in acc.results]
    refused_docs = {r["doc_id"] for r in all_attempts if r.get("status") in ("refused", "empty")}
    kept_scores = [score[d] for d in kept if score[d] is not None]
    ref_scores = [score[d] for d in kept if d in refused_docs and score[d] is not None]
    rest_scores = [score[d] for d in kept if d not in refused_docs and score[d] is not None]
    unused = [d for d in queue if d in acc.results]
    lengths = {v: [e["n_tokens"][v] for e in docs_out] for v in C.VERSIONS}
    report = {
        "refusal_counts": _refusal_counts(all_attempts),
        "llm_statuses": {f"{m}:{p}:{s}": n for (m, p, s), n in sorted(acc.llm.statuses.items())},
        "n_qwen_docs": sum(1 for e in docs_out if "qwn" in e["writer"].values()),
        "n_qwen_versions": {v: sum(1 for e in docs_out if e["writer"][v] == "qwn")
                            for v in ("corr", "para")},
        "mean_edit_distance": {v: _mean([e["edit_distance"][v] for e in docs_out])
                               for v in ("corr", "para")},
        "mean_length_ratio": {v: _mean([e["length_ratio"][v] for e in docs_out])
                              for v in ("corr", "para")},
        "length": {v: {"mean": _mean(x), "median": median(x) if x else None,
                       "min": min(x, default=None), "max": max(x, default=None)}
                   for v, x in lengths.items()},
        "topic_mix": dict(Counter(e["topic"] for e in docs_out).most_common()),
        "source_mix": dict(Counter(e["source_dir"] for e in docs_out).most_common()),
        "score_mix": dict(sorted(Counter(score[d] for d in kept).items(),
                                 key=lambda x: (x[0] is None, x[0]))),
        "endorsement_refused_vs_rest": {
            "n_refused_in_insert": len(ref_scores), "mean_refused": _mean(ref_scores),
            "n_rest": len(rest_scores), "mean_rest": _mean(rest_scores),
            "n_refused_processed": len(refused_docs),
            "mean_refused_processed": _mean([score[d] for d in refused_docs
                                             if score.get(d) is not None]),
        },
        "mean_insert_score": _mean(kept_scores),
        "n_processed": len(acc.results), "n_dropped": len(dropped),
        "processed_unused": unused,
        "not_reported_here": "mean LM loss under the checkpoint (needs the GPU; reported later)",
    }
    return {
        "docs": docs_out,
        "totals": totals(acc, kept),
        "dropped": dropped,
        "policies": acc.policies,
        "inputs": inputs,
        "insert_order": "sorted by sha256(f'{CORPUS_SEED}:{doc_id}'), CORPUS_SEED="
                        f"{C.CORPUS_SEED}",
        "report": report,
        "utc": _utc(),
    }


def _mean(x: Sequence[float]) -> float | None:
    return float(mean(x)) if x else None
