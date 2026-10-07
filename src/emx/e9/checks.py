"""E9 rewrite checks, checker validation by writer, and insert acceptance (unit B2b).

Implements prereg §4.4 (docs/experiments/E9/prereg.md), with the ranking of §4.3 and prompts A.1
and A.4 of Appendix A:

- **The check table** (`table_pass`). The checker (`zgl`) scores the original once and each output
  with A.1, and A.4 on (original, output). A correction passes iff its endorsement is <= 2, all
  three A.4 answers are true and its token length ratio is in [0.8, 1.25]; its edit distance is
  only reported. A paraphrase passes iff its endorsement is >= 4 and within +-2 of the original's,
  all three A.4 answers are true, the ratio is in [0.8, 1.25] and the normalized token edit
  distance is >= 0.3. A malformed checker output (after the llm layer's retries) fails the check
  and is recorded as `status="malformed"`.
- **Checker validation by writer stratum** (`sample_to_endpoint`, `evaluate_policy`). After the
  first rewriting pass, each writer's outputs are sampled without replacement to the sampling
  endpoint; each stratum's ordered policy table is evaluated once there, and the policy is frozen
  (persisted) for every later output: regenerations, replacements and the +-10% repairs.
- **Acceptance** (`accept`). A document stays only if both its correction and its paraphrase pass
  under their writers' policies (after at most one same-writer regeneration, run by unit B2a's
  `rewrite.regenerate`); otherwise it is dropped from every arm and replaced by the next-ranked
  candidate until single-copy original tokens total >= 4M (the crossing document kept). Then the
  corrections' and paraphrases' totals must each lie within +-10% of the originals'; if not, the
  documents with the most extreme length ratios are dropped and replaced the same way.
- **The frozen insert**: `data/E9/insert/insert_manifest.json` (contracts.INSERT_MANIFEST_KEYS
  without "capacity", which unit C adds) and `versions.jsonl` (contracts.VERSION_KEYS rows).

Outcomes are explicit dicts: {"outcome": "frozen", ...} or {"outcome": "stop", "reason": ...}.
Every API call goes through `emx.e9.llm.run_batch` with stage "setup" (it reserves and settles).
The two parallel-unit interfaces (llm.run_batch, rewrite.write_versions/regenerate) are reached
only through the small adapters `_LLM` and `_Rewriter`, so a change of their shapes is local.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
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

    `labels`: one dict per drawn item with "checker" (bool) and "panel" (two bools). An item is
    agreed when both panel models decide the same; the checker matches when it equals that
    decision. Main (luna): (1) panel agrees on < 75% of drawn items or < 100 agreed -> "stop";
    (2) checker matches >= 85% of agreed -> "checker"; (3) "panel". Fallback (qwn): (1) < 20
    agreed (incl. none drawn) or agreement < 75% -> "triple"; (2) -> "checker"; (3) "panel"."""
    if writer not in STRATA:
        raise ValueError(f"unknown writer stratum {writer!r}")
    n_drawn = len(labels)
    agreed = [x for x in labels if x["panel"][0] == x["panel"][1]]
    n_agreed = len(agreed)
    n_match = sum(1 for x in agreed if x["checker"] == x["panel"][0])
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
    }


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
            if missing:  # a layer bug, never a silent check failure
                raise RuntimeError(f"run_batch returned no row for {len(missing)} of {len(ids)} "
                                   f"{model_key}/{prompt_id} items, e.g. {missing[0]}")
            errors = [i for i in ids if by_id[i].get("status") not in C.LLM_STATUSES[:4]]
            if errors:  # "error" is not an answer (llm re-sends it next run): never a failed check
                raise RuntimeError(f"{len(errors)} {model_key}/{prompt_id} calls ended in API "
                                   f"errors (rerun to resend), e.g. {errors[0]}")
            for i in ids:
                r = by_id[i]
                status = r.get("status")
                self.statuses[(model_key, prompt_id, status)] += 1
                # malformed after retries, refused or empty -> None -> the check fails
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


class _Rewriter:
    """The only place that knows unit B2a's call and return shapes (`emx.e9.rewrite`).

    write_versions(docs, versions, *, stage, batch, tok, [run_batch, out_dir, llm_dir]) and
    regenerate([(doc_id, version, writer)], *, docs, stage, batch, ...) return
    {(doc_id, version): standing attempt record} (REWRITE_ATTEMPT_KEYS; attempt 1 = steps 1-2,
    attempt 2 = the regeneration); load_attempts(out_dir) returns every recorded attempt."""

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

    def _kw(self, batch: str) -> dict:
        kw = {"stage": self.stage, "batch": batch, "tok": self.tok}
        for k in ("run_batch", "out_dir", "llm_dir"):
            if getattr(self, k) is not None:
                kw[k] = getattr(self, k)
        return kw

    def write(self, docs: list[dict], batch: str) -> list[dict]:
        return self.rows(self._write(docs, ("corr", "para"), **self._kw(batch)))

    def regenerate(self, failed: list[tuple[str, str, str]], docs: list[dict],
                   batch: str) -> list[dict]:
        return self.rows(self._regen(failed, docs=docs, **self._kw(batch)))

    def attempts(self) -> list[dict]:
        res = self._load(self.out_dir) if self.out_dir is not None else self._load()
        return list(res.values()) if isinstance(res, dict) else list(res)


def latest_outputs(rows: Iterable[dict], keys: set[tuple[str, str]]) -> dict:
    """The latest attempt row per (doc_id, version) among `keys` (highest attempt; for equal
    attempts the later row, e.g. the Qwen fallback after a luna refusal)."""
    best: dict[tuple[str, str], tuple[int, int, dict]] = {}
    for i, r in enumerate(rows):
        k = (r["doc_id"], r["version"])
        if k not in keys:
            continue
        rank = (int(r.get("attempt") or 0), i)
        if k not in best or rank >= best[k][:2]:
            best[k] = (*rank, r)
    return {
        k: Output(doc_id=r["doc_id"], version=r["version"], attempt=int(r.get("attempt") or 0),
                  writer=r["writer"], seed=r.get("seed"), status=r.get("status", ""),
                  text=r.get("text"), n_tokens=r.get("n_tokens"))
        for k, (_, _, r) in best.items()
    }


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
    """Holds the run's state: checks, frozen policies, per-document results (§4.4)."""

    def __init__(self, docs: dict[str, dict], *, llm: _LLM, rewriter: _Rewriter, tok,
                 policies_path: Path | None = None, validation_dir: Path | None = None):
        self.docs = docs
        self.llm = llm
        self.rw = rewriter
        self.tok = tok
        self.policies_path = policies_path
        self.validation_dir = validation_dir
        self.policies: dict[str, dict] | None = None
        if policies_path is not None and Path(policies_path).exists():
            self.policies = json.loads(Path(policies_path).read_text())
        self.results: dict[str, DocResult] = {}
        self.attempt_rows: list[dict] = []
        self.check_rows: list[dict] = []
        self.decisions: dict[tuple[str, str], bool] = {}  # (judge key, output key) -> pass
        self.accepted: dict[str, bool] = {}  # output key -> accepted under policy
        self._geom: dict[str, tuple[float, float]] = {}  # output key -> (ratio, edit distance)
        self._rounds = 0  # rewrite batch names: "pass1", then "repl1", "repl2", ...
        self._orig_ids: dict[str, list[int]] = {}

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
        CHECK_KEYS row per usable output and model; decisions cached."""
        outs = [o for o in outs if o.usable and (model_key, o.key) not in self.decisions]
        if not outs:
            return
        a1_items = [(f"orig|{o.doc_id}", {"document": self.docs[o.doc_id]["text"]}) for o in outs]
        a1_items += [(o.key, {"document": o.text}) for o in outs]
        a1 = self.llm.json(model_key, "A1_content", a1_items)
        a4 = self.llm.json(model_key, "A4_preserve", [
            (o.key, {"original": self.docs[o.doc_id]["text"], "rewrite": o.text}) for o in outs])
        for o in outs:
            e_orig = parse_endorsement(a1[f"orig|{o.doc_id}"])
            e_out = parse_endorsement(a1[o.key])
            a4o = parse_a4(a4[o.key])
            ratio, edit = self._geometry(o)
            passed = table_pass(o.version, e_orig, e_out, a4o, ratio, edit)
            needs_orig = o.version == "para"
            malformed = e_out is None or a4o is None or (needs_orig and e_orig is None)
            self.decisions[(model_key, o.key)] = passed
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
        self.judge(C.ROLES["checker"], outs)
        for o in outs:
            if o.writer not in STRATA:
                raise ValueError(f"unknown writer {o.writer!r} for {o.key}")
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
        """Sample each writer stratum to its endpoint, evaluate its policy table once, freeze."""
        if self.policies is not None:
            raise RuntimeError("checker policies are already frozen")
        policies = {}
        for writer in STRATA:
            outs = {o.key: o for o in first_pass if o.writer == writer and o.usable}
            panel = self._panel(writer)

            def label(keys: list[str], outs=outs, panel=panel) -> dict[str, bool]:
                for m in panel:
                    self.judge(m, [outs[k] for k in keys])
                return {k: self.decisions[(panel[0], k)] == self.decisions[(panel[1], k)]
                        for k in keys}

            draw = sample_to_endpoint([(k, o.version) for k, o in outs.items()],
                                      STRATA[writer]["min_agreed"], label)
            checker = C.ROLES["checker"]
            labels = [{"key": k, "version": outs[k].version,
                       "checker": self.decisions[(checker, k)],
                       "panel": (self.decisions[(panel[0], k)], self.decisions[(panel[1], k)])}
                      for k in draw["drawn"]]
            pol = evaluate_policy(writer, labels)
            pol["sampling"] = {k: draw[k] for k in ("batches", "endpoint", "n_items", "seed")}
            policies[writer] = pol
            if self.validation_dir is not None:
                _write_jsonl(Path(self.validation_dir) / f"checker_validation_{writer}.jsonl",
                             [x | {"panel": list(x["panel"])} for x in labels])
        policies["frozen_utc"] = _utc()
        self.policies = policies
        if self.policies_path is not None:
            _write_json(Path(self.policies_path), policies)
        return policies

    # decisions ---------------------------------------------------------------------------------

    def decide(self, outs: Sequence[Output]) -> dict[str, bool]:
        """Accept or reject each output under its writer's frozen policy."""
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
        """Write, check and decide both versions of each new document; regenerate failures once
        (same writer, next seed, by unit B2a); a document passes only if both versions do."""
        new = [d for d in doc_ids if d not in self.results]
        if not new:
            return
        batch = "pass1" if first_pass else f"repl{self._rounds}"
        self._rounds += 1
        keys = {(d, v) for d in new for v in ("corr", "para")}
        new_docs = [self.docs[d] for d in new]
        rows = self.rw.write(new_docs, batch)
        self.attempt_rows.extend(rows)
        outs = latest_outputs(rows, keys)
        missing = keys - set(outs)
        if missing:  # B2a leaves a version out after an API error: rerun to resend it
            raise RuntimeError(f"rewrite returned no standing attempt for {len(missing)} "
                               f"versions (API errors? rerun), e.g. {min(missing)}")
        self._check_all(list(outs.values()))
        if self.policies is None:
            if not first_pass:
                raise RuntimeError("policies must be frozen after the first rewriting pass")
            self.validate(list(outs.values()))
        ok = self.decide(list(outs.values()))
        failing = [o for o in outs.values() if not ok[o.key]]
        if failing:
            fkeys = {(o.doc_id, o.version) for o in failing}
            regen_rows = self.rw.regenerate(
                sorted((o.doc_id, o.version, o.writer) for o in failing), new_docs, batch)
            self.attempt_rows.extend(regen_rows)
            regen = latest_outputs(regen_rows, fkeys)
            # an absent or unchanged regeneration leaves the failing output in place
            regen = {k: o for k, o in regen.items() if o.key != outs[k].key}
            self._check_all(list(regen.values()))
            ok.update(self.decide(list(regen.values())))
            outs.update(regen)
        for d in new:
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
            if res.passed:
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
    """+-10% totals (§4.4): while a version's total is out of band, drop the document with the
    most extreme length ratio of that version in the violating direction (the version with the
    larger relative deviation first; ties dropped lowest-ranked first) until the remaining
    documents are within band, then refill to the target; repeat until both hold."""
    while True:
        tot = totals(acc, kept)
        viol = _violations(tot)
        if not viol:
            return queue
        while viol:
            v, dev = max(viol, key=lambda x: (abs(x[1]), x[0]))
            sign = 1 if dev > 0 else -1

            def ratio(d: str, v=v) -> float:
                return _n(acc.results[d].outputs[v], acc) / acc.docs[d]["n_tokens"]

            worst = max(kept, key=lambda d, s=sign: (s * ratio(d), rank[d]))
            kept.remove(worst)
            dropped.append({"doc_id": worst, "rank": rank[worst], "stage": "totals_repair",
                            "reason": f"length_repair:{v}:{ratio(worst):.4f}"})
            if not kept:
                raise Stop("totals_repair_emptied_insert")
            viol = _violations(totals(acc, kept))
        queue = _fill(acc, kept, queue, dropped, rank, target)


# --- loading, writing and the driver ------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def code_commit() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
        return sha + ("-dirty" if dirty.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


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


def accept(
    paths: Paths | None = None,
    *,
    run_batch=None,
    write_versions=None,
    regenerate=None,
    load_attempts=None,
    tok=None,
    target_tokens: int = C.INSERT_TOKENS,
    overwrite: bool = False,
) -> dict:
    """The acceptance driver (§4.4). Returns {"outcome": "frozen", ...} with the manifest and
    versions paths, or {"outcome": "stop", "reason": ...}; both are also written to
    `<rewrite_dir>/accept_outcome.json`. Errors propagate; they are never turned into a pass."""
    paths = (paths or Paths()).rooted(ROOT)
    manifest_path = paths.insert_dir / "insert_manifest.json"
    versions_path = paths.insert_dir / "versions.jsonl"
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(f"{manifest_path} is frozen; pass overwrite=True to rebuild")
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
    inputs = {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p): _sha256_file(p)
              for p in (paths.p1, paths.selection, paths.ranked)}

    acc = Acceptor(
        docs, llm=_LLM(run_batch, paths.llm_dir),
        rewriter=_Rewriter(write_versions, regenerate, load_attempts, run_batch=run_batch,
                           tok=tok, out_dir=paths.rewrite_dir, llm_dir=paths.llm_dir),
        tok=tok, policies_path=paths.rewrite_dir / "checker_policies.json",
        validation_dir=paths.rewrite_dir)
    kept: list[str] = []
    dropped: list[dict] = []
    sel = set(selection)
    queue = [r["doc_id"] for r in ranking if r["doc_id"] not in sel]
    try:
        acc.process(selection, first_pass=True)
        for d in selection:
            if acc.results[d].passed:
                kept.append(d)
            else:
                dropped.append({"doc_id": d, "rank": rank[d], "reason": acc.results[d].reason,
                                "stage": "first_pass"})
        queue = _fill(acc, kept, queue, dropped, rank, target_tokens)
        queue = _repair_totals(acc, kept, queue, dropped, rank, target_tokens)
    except Stop as s:
        outcome = {"outcome": "stop", "reason": s.reason, "details": s.details,
                   "policies": acc.policies, "n_kept": len(kept), "dropped": dropped,
                   "utc": _utc(), "code_commit": code_commit(), "inputs": inputs}
        _flush(acc, paths)
        _write_json(paths.rewrite_dir / "accept_outcome.json", outcome)
        return outcome

    kept.sort(key=lambda d: insert_order_key(d))
    manifest = build_manifest(acc, kept, dropped, rank, score, inputs, queue)
    version_rows = build_version_rows(acc, kept)
    _flush(acc, paths)
    _write_jsonl(versions_path, version_rows)
    manifest["versions_sha256"] = _sha256_file(versions_path)
    _write_json(manifest_path, manifest)
    outcome = {"outcome": "frozen", "reason": None, "manifest": str(manifest_path),
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
        "code_commit": code_commit(),
    }


def _mean(x: Sequence[float]) -> float | None:
    return float(mean(x)) if x else None
