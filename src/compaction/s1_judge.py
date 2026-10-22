"""Experiment 1 (stage S1) judge calls: pass 1a (items), 1b (grades), 2 (claims), and the rows.

Specified in docs/sprint/specs/S1.md, "Judge calls (U3)" and "Output rows"; the shared types are in
`compaction.s1_types`. The prompts are in `s1_prompts/` and their sha256 goes into every row.

Prompt layout (cache rule, integrator 2026-10-20): providers cache by exact prefix, and Judge B's
inputs are mostly history (public histories run to 150-400k tokens). So pass 1a and pass 2 share
ONE system prompt (a generic auditor role with no pass-specific text; the two prompt files carry
the same system section and `load_prompt` refuses if they differ), and their user messages START
with the rendered history block `<history>\\n...\\n</history>\\n\\n`, byte-identical across passes
for the same record and chunk. The pass-specific instructions follow, then (pass 2) the summary.
Each base history gets ONE immutable partition (`partition`), computed from the history, the
fixed SUMMARY_RESERVE_TOKENS and the larger output reserve, never from the pass or the summary,
and reused by pass 1a, pass 2 and every Part B arm. A summary too long for the reserve makes that
pass-2 request `summary_over_reserve` (not sent) rather than re-partitioning; a single message
longer than a whole chunk makes the record `unscorable_oversize`. Pass 1b has no history and
keeps its own system prompt.

Passes (spec "Judge calls"; P01's lessons, `probes/P01-compaction-fidelity/note.md`): invention
is its own call (pass 2), "do NOT list" clauses keep chatter out, and every verdict needs a quote
that code checks with `s1_types.quote_in`. Pass 1a never sees the summary or the post-compaction
context (finding 6 of astra r1: the item list must not be shaped by the summary it is graded on).

Checks in code (spec): rows that fail a check are kept with `valid=False` and an
`invalid_reason`, never dropped. A quote found in a different message than the judge claimed has
its `msg` corrected; rows carry the judge's number as `msg_claimed` (None when not corrected).

Known limitation (accepted, integrator 2026-10-20): when a long history is split into chunks, an
item in one chunk that a later chunk supersedes stays `superseded=False`, since no call sees both.

Throughput: `judge_records` runs every record's requests through one `backend.run` per round
(round 1: pass 1a; round 2: pass 2's first or only part, whose history prefix pass 1a has just
written to the provider's cache, with pass 1b; round 3: pass 2's later parts), so the backend's
own concurrency applies across records.

Code-scored facts (`code_facts`) port P01's `judge.file_named`, `judge.latest_run_status`,
`judge.tests_misstatement` and the test-run classifiers of `p01/common.py`
(`probes/P01-compaction-fidelity/p01/`), without P01's slice-specific rules.
"""

from __future__ import annotations

import bisect
import collections
import difflib
import hashlib
import json
import posixpath
import re
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from compaction import s1_types as T

PROMPT_DIR = Path(__file__).resolve().parent / "s1_prompts"
PURPOSE = "s1_judge"  # one backend cache file for every pass and arm, so arms reuse pass 1a

MAX_OUT_1A = 32_000  # item lists on long histories run long
MAX_OUT_1B = 16_000
MAX_OUT_2 = 32_000  # claim lists plus thinking: 16k ran out on 2 of 3 e2e records (20 Oct)
OVERHEAD_TOKENS = 2_000  # s1_types.fits's default reserve
SUMMARY_RESERVE_TOKENS = 40_000  # fixed room for the summary (and a chunk's claim list), so
# one partition of each history serves pass 1a, pass 2 and every arm (review r1 finding 8)
PASS1B_BATCH = 60  # items per pass-1b request (about 60 output tokens per grade)
DEDUPE_RATIO = 0.9
MAX_ROUNDS = 8  # rounds of `drive`: 1a; then 2 (first part) with 1b; then 2's later parts
FRONTIERML = "frontierml"

ITEM_ROW_FIELDS = (
    "rid",
    "base_rid",
    "arm",
    "judge",
    "judge_model",
    "prompt_sha",
    "item_id",
    "type",
    "origin",
    "permission_status",
    "text",
    "quote",
    "msg",
    "msg_claimed",
    "superseded",
    "superseded_quote",
    "valid",
    "invalid_reason",
    "grade",
    "summary_quote",
    "grade_valid",
    "grade_reason",
    "availability",
    "availability_how",
    "availability_unfiltered",
    "availability_unfiltered_how",
    "assessable",
    "n_chunks",
    "source",
    "session_id",
    "harness",
    "model",
    "pre_tokens",
)
CLAIM_ROW_FIELDS = (
    "rid",
    "base_rid",
    "arm",
    "judge",
    "judge_model",
    "prompt_sha",
    "claim_id",
    "claim",
    "summary_quote",
    "summary_quote_match",
    "summary_quote_submitted",
    "is_permission",
    "verdict",
    "derived",
    "evidence_quote",
    "msg",
    "msg_claimed",
    "valid",
    "invalid_reason",
    "note",
    "history_complete",
    "n_chunks",
    "source",
    "session_id",
    "harness",
    "model",
    "pre_tokens",
)


class PrivacyError(RuntimeError):
    """A non-Anthropic backend was given a record that may go to Anthropic models only."""


# ------------------------------------------------------------------------------------------
# Prompts
# ------------------------------------------------------------------------------------------

_SECTION = re.compile(r"^=== (\w+) ===$", re.MULTILINE)
_SLOT = re.compile(r"\{(history|summary|items|claims_block|claims)\}")


@dataclass(frozen=True)
class Prompt:
    name: str
    sections: dict[str, str]
    sha256: str  # of the whole file

    @property
    def system(self) -> str:
        return self.sections["system"]

    def fill(self, section: str, **slots: str) -> str:
        """The section with its slots filled in one pass (inserted text is never re-scanned, so
        braces or slot names inside a history are left alone)."""
        tpl = self.sections[section]
        missing = {m.group(1) for m in _SLOT.finditer(tpl)} - set(slots)
        if missing:
            raise KeyError(f"{self.name}/{section}: no value for {sorted(missing)}")
        return _SLOT.sub(lambda m: slots[m.group(1)], tpl)


def load_prompt(name: str, prompt_dir: Path = PROMPT_DIR) -> Prompt:
    """Parse `<name>.md`: a header (not sent), then `=== system ===`, `=== user ===` and optional
    further sections, each stripped of leading and trailing newlines."""
    raw = (prompt_dir / f"{name}.md").read_bytes()
    text = raw.decode("utf-8")
    marks = list(_SECTION.finditer(text))
    sections = {}
    for k, m in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        sections[m.group(1)] = text[m.end() : end].strip("\n")
    for need in ("system", "user"):
        if need not in sections:
            raise ValueError(f"{name}.md has no '=== {need} ===' section")
    return Prompt(name, sections, hashlib.sha256(raw).hexdigest())


def prompts(prompt_dir: Path = PROMPT_DIR) -> dict[str, Prompt]:
    """The three prompts, checking the shared-prefix rule (module docstring)."""
    p = {
        k: load_prompt(n, prompt_dir)
        for k, n in (("1a", "pass1a_items"), ("1b", "pass1b_grade"), ("2", "pass2_claims"))
    }
    if p["1a"].system != p["2"].system:
        raise ValueError("pass1a_items.md and pass2_claims.md must share one system section")
    for k in ("1a", "2"):
        if not p[k].sections["user"].startswith(HISTORY_OPEN + "{history}" + HISTORY_CLOSE):
            raise ValueError(f"pass {k}: the user section must start with the history block")
    if "claims_block" not in p["2"].sections:
        raise ValueError("pass2_claims.md has no '=== claims_block ===' section")
    return p


HISTORY_OPEN = "<history>\n"
HISTORY_CLOSE = "\n</history>\n\n"


def history_block(history: str) -> str:
    """The byte-identical prefix of every pass-1a and pass-2 user message for one chunk."""
    return HISTORY_OPEN + history + HISTORY_CLOSE


def item_prompt_sha(p: Mapping[str, Prompt]) -> str:
    return f"pass1a={p['1a'].sha256};pass1b={p['1b'].sha256}"


def claim_prompt_sha(p: Mapping[str, Prompt]) -> str:
    return f"pass2={p['2'].sha256}"


# ------------------------------------------------------------------------------------------
# Rendered history: chunks and message lookup
# ------------------------------------------------------------------------------------------
# `rendered` is s1_data.Rendered (U1): .text, .spans {msg index: (start, end)}, .gaps, .bytes.


@dataclass(frozen=True)
class Part:
    k: int
    msgs: tuple[int, ...]  # message indices in this part, in order
    text: str


def _msg_ids(rendered) -> list[int]:
    return sorted(rendered.spans)


def _msg_text(rendered, i: int) -> str:
    s, e = rendered.spans[i]
    return rendered.text[s:e]


def chunk_budget(backend, prompt_set: Mapping[str, Prompt]) -> int:
    """History tokens one request may carry: the context minus the larger pass's output reserve,
    the shared system prompt, the longer instructions and the fixed summary reserve."""
    bpt = backend.bytes_per_token
    fixed = max(
        T.est_tokens(prompt_set[k].system + prompt_set[k].sections["user"], bpt)
        for k in ("1a", "2")
    )
    fixed += T.est_tokens(prompt_set["2"].sections["claims_block"], bpt)
    room = backend.context_tokens - OVERHEAD_TOKENS - max(MAX_OUT_1A, MAX_OUT_2) - fixed
    return room - SUMMARY_RESERVE_TOKENS


@dataclass(frozen=True)
class Partition:
    """The one chunking of a base history, shared by pass 1a, pass 2 and every arm (review r1
    finding 8). `oversize` lists messages larger than a whole chunk: the record is then
    unscorable and nothing is sent for it."""

    parts: tuple[Part, ...]
    oversize: tuple[int, ...]

    @property
    def n(self) -> int:
        return len(self.parts)


def plan_parts(rendered, budget_tokens: int, bytes_per_token: float) -> Partition:
    """Split the history at message boundaries into parts of at most `budget_tokens`. One part
    is the whole text; a message larger than the budget is reported in `oversize`."""
    ids = _msg_ids(rendered)
    if not ids or T.est_tokens(rendered.text, bytes_per_token) <= budget_tokens:
        return Partition((Part(0, tuple(ids), rendered.text),), ())
    groups: list[list[int]] = [[]]
    oversize = []
    used = 0
    for i in ids:
        t = T.est_tokens(_msg_text(rendered, i), bytes_per_token) + 1
        if t > budget_tokens:
            oversize.append(i)
        if groups[-1] and used + t > budget_tokens:
            groups.append([])
            used = 0
        groups[-1].append(i)
        used += t
    parts = []
    start = 0  # each part runs from just after the previous one, so text between message
    # blocks (the KEPT_HEADER line before the kept-verbatim messages) is never lost
    for k, g in enumerate(groups):
        e = rendered.spans[g[-1]][1]
        parts.append(Part(k, tuple(g), rendered.text[start:e].lstrip("\n")))
        start = e
    return Partition(tuple(parts), tuple(oversize))


def partition(backend, prompt_set: Mapping[str, Prompt], rendered) -> Partition:
    return plan_parts(rendered, chunk_budget(backend, prompt_set), backend.bytes_per_token)


def summary_over_reserve(summary: str, n_parts: int, bytes_per_token: float) -> bool:
    """True when the summary (and, for a chunked history, the claim list sent to the later
    chunks, about as long again) doesn't fit the fixed reserve. Such a pass-2 request is not
    sent and is reported as `summary_over_reserve`, rather than re-partitioning the history."""
    s = T.est_tokens(summary, bytes_per_token)
    return s * (2 if n_parts > 1 else 1) + 1_000 > SUMMARY_RESERVE_TOKENS


def locate(
    quote: str | None, rendered, claimed: int | None, prefer: Iterable[int] = ()
) -> int | None:
    """The message holding `quote` (s1_types.quote_in): the claimed one if it does, else the
    first match among `prefer` (the chunk the judge saw), else among all messages; None if no
    single message holds it."""
    if not quote:
        return None
    if (
        isinstance(claimed, int)
        and claimed in rendered.spans
        and T.quote_in(quote, _msg_text(rendered, claimed))
    ):
        return claimed
    prefer = list(prefer)
    seen = set(prefer)
    for i in prefer + [i for i in _msg_ids(rendered) if i not in seen]:
        if T.quote_in(quote, _msg_text(rendered, i)):
            return i
    return None


def judge_view(rec: dict):
    """The history block both history passes see: U1's `s1_data.render(rec,
    include_preserved=True)`, the replaced history followed by the messages Claude Code kept
    verbatim (after `s1_types.KEPT_HEADER`, numbered on from the source messages, so `msg`
    stays an integer and quotes into them validate). Re-attached files in post_messages come
    after the compaction and are not included."""
    from compaction import s1_data  # U1; imported here (module docstring)

    return s1_data.render(rec, include_preserved=True)


def history_messages(rec: dict) -> list[dict]:
    """The same history as messages: U1's `s1_data.history_messages(rec)` (source messages,
    then the kept-verbatim ones)."""
    from compaction import s1_data

    if hasattr(s1_data, "history_messages"):
        return s1_data.history_messages(rec)
    from compaction import audit_logs  # until U1's helper lands: the same composition

    kept, _ = audit_logs._preserved(rec, rec.get("meta"))
    return list(rec.get("source_messages") or []) + [m for m in kept or [] if isinstance(m, dict)]


def history_complete(rec: dict, rendered) -> bool:
    """Spec: fully assessable (U1's `history_complete`) and no gap marker in the rendered
    history. FrontierML is never complete."""
    return (
        rec.get("source") != FRONTIERML and bool(rec.get("history_complete")) and not rendered.gaps
    )


# ------------------------------------------------------------------------------------------
# Responses
# ------------------------------------------------------------------------------------------


def _parse_obj(text: str | None) -> Any:
    if not text:
        return None
    s = text.strip()
    try:
        return json.loads(s[s.find("{") : s.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return None


@dataclass
class Elements:
    """The list under the answer's key: `good` elements validate; `bad` are (element, first
    schema error) pairs kept so they count as invalid rows (review r1 finding 3); `error` is
    None for a clean answer."""

    good: list[dict]
    bad: list[tuple[Any, str]]
    error: str | None


def elements(resp: T.Resp | None, schema: dict, key: str) -> Elements:
    if resp is None:
        return Elements([], [], "missing_response")
    if resp.error is None and resp.data is not None and not T.validate(resp.data, schema):
        return Elements(list(resp.data[key]), [], None)
    if resp.error not in (None, "schema"):
        return Elements([], [], resp.error)
    obj = resp.data if isinstance(resp.data, dict) else _parse_obj(resp.text)
    if not isinstance(obj, dict) or not isinstance(obj.get(key), list):
        return Elements([], [], "schema")
    item_schema = schema["properties"][key]["items"]
    good, bad = [], []
    for e in obj[key]:
        errs = T.validate(e, item_schema)
        if errs:
            bad.append((e, errs[0]))
        else:
            good.append(e)
    return Elements(good, bad, f"schema:{len(bad)} of {len(obj[key])} invalid")


def _get(e: Any, k: str, kind: type | tuple, default=None):
    v = e.get(k) if isinstance(e, dict) else None
    if isinstance(v, bool) and kind is int:
        return default
    return v if isinstance(v, kind) else default


def _req(
    key: str, system: str, user: str, schema: dict, max_out: int, rid: str, rec: dict
) -> T.Req:
    return T.Req(
        key=key,
        system=system,
        user=user,
        schema=schema,
        max_output_tokens=max_out,
        rid=rid,
        source=str(rec.get("source")),
        private=T.anthropic_only(rec),
    )


def check_privacy(backend, recs: Iterable[dict]) -> None:
    """Spec D2: a non-Anthropic backend never sees an `anthropic_only` record."""
    if backend.family == "anthropic":
        return
    bad = [r.get("id") for r in recs if T.anthropic_only(r)]
    if bad:
        raise PrivacyError(
            f"{backend.name} ({backend.family}) may not see {len(bad)} "
            "Anthropic-only record(s); Judge B takes public records only"
        )


# ------------------------------------------------------------------------------------------
# Tasks: request builders and finishers, run together by `drive`
# ------------------------------------------------------------------------------------------


class _Task:
    done = False

    def pending(self) -> list[T.Req]:
        raise NotImplementedError

    def feed(self, resps: Mapping[str, T.Resp]) -> None:
        raise NotImplementedError


def drive(backend, tasks: list[_Task], purpose: str = PURPOSE) -> None:
    """Run the tasks' requests round by round, one `backend.run` per round for all of them."""
    for _ in range(MAX_ROUNDS):
        reqs = [r for t in tasks if not t.done for r in t.pending()]
        if all(t.done for t in tasks):
            return
        if not reqs:
            raise RuntimeError("judge tasks stalled with nothing to send")
        keys = [r.key for r in reqs]
        if len(set(keys)) != len(keys):
            raise RuntimeError("duplicate request keys in one batch")
        resps = backend.run(reqs, purpose=purpose)
        sent = set(keys)
        for t in tasks:
            if not t.done and t.pending() and {r.key for r in t.pending()} <= sent:
                t.feed(resps)
    raise RuntimeError("judge tasks did not finish")


def _dedupe_key_text(s: str | None) -> str:
    return T.norm(s or "").lower()


def _is_dup(a: dict, b: dict) -> bool:
    if T.norm(a["quote"] or "") and T.norm(a["quote"] or "") == T.norm(b["quote"] or ""):
        return True
    return (
        a["type"] == b["type"]
        and difflib.SequenceMatcher(
            None, _dedupe_key_text(a["text"]), _dedupe_key_text(b["text"])
        ).ratio()
        >= DEDUPE_RATIO
    )


def dedupe_items(items: list[dict]) -> list[dict]:
    """Spec: across chunks, the same normalised quote, or the same type and difflib ratio >= 0.9
    on the text, is one item; the earliest wins. Only valid items are merged: every invalid
    output stays as its own invalid row (review r2), so validity is measured over all raw
    outputs. Items from the same chunk are never merged: the judge listed them apart having
    seen both. Input order is kept."""
    kept: list[dict] = []
    for x in items:
        if not x["valid"]:
            kept.append(x)  # invalid outputs are their own rows, never merged away (r2)
        elif not any(y["valid"] and y["chunk"] != x["chunk"] and _is_dup(x, y) for y in kept):
            kept.append(x)
    order = {id(x): n for n, x in enumerate(items)}
    return sorted(kept, key=lambda x: order[id(x)])


class Pass1a(_Task):
    """Items from the base history (spec "Pass 1a"); the summary is never in the request."""

    def __init__(
        self, backend, rec: dict, rendered, prompt_set: Mapping[str, Prompt], part: Partition
    ):
        self.backend, self.rec, self.rendered, self.p = backend, rec, rendered, prompt_set
        self.base_rid = str(rec["id"])
        self.partition = part
        self.parts = part.parts
        self.items: list[dict] = []
        self.errors: list[dict] = []
        if part.oversize:
            self.errors.append(
                {
                    "rid": self.base_rid,
                    "pass": "1a",
                    "error": "unscorable_oversize",
                    "msgs": list(part.oversize),
                }
            )
            self._reqs: list[T.Req] = []
            self.done = True
            return
        self._reqs = [
            _req(
                f"p1a|{self.base_rid}|n{part.n}|c{pt.k}",
                self.p["1a"].system,
                self.p["1a"].fill("user", history=pt.text),
                T.PASS1A_SCHEMA,
                MAX_OUT_1A,
                self.base_rid,
                self.rec,
            )
            for pt in self.parts
        ]

    def pending(self) -> list[T.Req]:
        return self._reqs

    def feed(self, resps: Mapping[str, T.Resp]) -> None:
        raw: list[dict] = []
        for pt, req in zip(self.parts, self._reqs):
            el = elements(resps.get(req.key), T.PASS1A_SCHEMA, "items")
            if el.error:
                self.errors.append(
                    {
                        "rid": self.base_rid,
                        "pass": "1a",
                        "chunk": pt.k,
                        "n_chunks": len(self.parts),
                        "error": el.error,
                    }
                )
            raw.extend(check_item(e, self.rendered, pt) for e in el.good)
            raw.extend(malformed_item(e, err, self.rendered, pt) for e, err in el.bad)
        items = dedupe_items(raw)
        for n, it in enumerate(items):
            it["n"] = n
            it["item_id"] = f"{self.base_rid}#{self.backend.name}#i{n}"
            it["n_chunks"] = len(self.parts)
        self.items = items
        self._reqs = []
        self.done = True


def check_item(e: dict, rendered, part: Part) -> dict:
    """Code checks on one pass-1a item (spec "Checks in code")."""
    reasons = []
    quote = e["quote"]
    q = T.norm(quote or "")
    msg = None
    if len(q) < T.QUOTE_MIN_CHARS:
        reasons.append("quote_too_short")
    elif len(q) > T.QUOTE_MAX_CHARS:
        reasons.append("quote_too_long")
    else:
        msg = locate(quote, rendered, e["msg"], prefer=part.msgs)
        if msg is None:
            reasons.append("quote_not_found")
    pstatus = e["permission_status"]
    if e["type"] == "permission":
        if pstatus is None:
            reasons.append("permission_status_missing")
    else:
        pstatus = None  # the schema allows it; the prompt says null for other types
    sup_quote = e["superseded_quote"] if e["superseded"] else None
    if e["superseded"]:
        at = locate(sup_quote, rendered, None, prefer=part.msgs)
        start = msg if msg is not None else e["msg"]
        if at is None or len(T.norm(sup_quote or "")) > T.QUOTE_MAX_CHARS:
            reasons.append("superseded_quote_not_found")
        elif at < start:
            reasons.append("superseded_quote_before_item")
    shown = msg if msg is not None else e["msg"]
    return {
        "type": e["type"],
        "text": e["text"],
        "quote": quote,
        "msg": shown,
        "msg_claimed": e["msg"],
        "origin": e["origin"],
        "permission_status": pstatus,
        "superseded": e["superseded"],
        "superseded_quote": sup_quote,
        "valid": not reasons,
        "invalid_reason": "; ".join(reasons) or None,
        "assessable": shown not in rendered.gaps,
        "chunk": part.k,
    }


def malformed_item(e: Any, err: str, rendered, part: Part) -> dict:
    """A schema-invalid pass-1a element, kept as an invalid row with the fields it has."""
    msg = _get(e, "msg", int)
    return {
        "type": _get(e, "type", str),
        "text": _get(e, "text", str),
        "quote": _get(e, "quote", str),
        "msg": msg,
        "msg_claimed": msg,
        "origin": _get(e, "origin", str),
        "permission_status": _get(e, "permission_status", str),
        "superseded": _get(e, "superseded", bool, False),
        "superseded_quote": _get(e, "superseded_quote", str),
        "valid": False,
        "invalid_reason": f"schema:{err}",
        "assessable": msg is not None and msg not in rendered.gaps,
        "chunk": part.k,
    }


class Pass1b(_Task):
    """Grades of the valid items against one summary (spec "Pass 1b"); no history."""

    def __init__(
        self,
        backend,
        items: list[dict] | Pass1a,
        summary: str,
        rid: str,
        rec: dict,
        prompt_set: Mapping[str, Prompt],
    ):
        self.backend, self.src, self.summary, self.rid = backend, items, summary, rid
        self.rec, self.p = rec, prompt_set
        self.items: list[dict] = []
        self.errors: list[dict] = []
        self._reqs: list[T.Req] | None = None

    def _source_items(self) -> list[dict] | None:
        if isinstance(self.src, Pass1a):
            return self.src.items if self.src.done else None
        return self.src

    def pending(self) -> list[T.Req]:
        if self._reqs is not None:
            return self._reqs
        src = self._source_items()
        if src is None:
            return []
        self.items = [
            dict(
                x,
                grade=None,
                summary_quote=None,
                grade_valid=False,
                grade_note=None,
                grade_reason="item_invalid" if not x["valid"] else "not_graded",
            )
            for x in src
        ]
        valid = [x for x in self.items if x["valid"]]
        self._batches = [valid[k : k + PASS1B_BATCH] for k in range(0, len(valid), PASS1B_BATCH)]
        self._reqs = []
        for b, batch in enumerate(self._batches):
            lines = "\n".join(
                json.dumps(
                    {"id": f"i{x['n']}", "type": x["type"], "text": x["text"], "quote": x["quote"]},
                    ensure_ascii=False,
                )
                for x in batch
            )
            user = self.p["1b"].fill("user", summary=self.summary, items=lines)
            self._reqs.append(
                _req(
                    f"p1b|{self.rid}|b{b}of{len(self._batches)}",
                    self.p["1b"].system,
                    user,
                    T.PASS1B_SCHEMA,
                    MAX_OUT_1B,
                    self.rid,
                    self.rec,
                )
            )
        if not self._reqs:
            self.done = True
        return self._reqs

    def feed(self, resps: Mapping[str, T.Resp]) -> None:
        if not self._reqs:
            return
        for b, (batch, req) in enumerate(zip(self._batches, self._reqs)):
            el = elements(resps.get(req.key), T.PASS1B_SCHEMA, "grades")
            if el.error:
                self.errors.append({"rid": self.rid, "pass": "1b", "batch": b, "error": el.error})
                if not el.good and not el.bad:
                    for x in batch:
                        x["grade_reason"] = f"error:{el.error}"
            by_id = {f"i{x['n']}": x for x in batch}
            graded: set[str] = set()
            for g in el.good:
                x = by_id.get(g["item"])
                if x is None or g["item"] in graded:
                    continue  # unknown id, or a second grade for one item: the first wins
                graded.add(g["item"])
                apply_grade(x, g, self.summary)
            for g, err in el.bad:  # a malformed grade naming a known item marks it
                gid = _get(g, "item", str)
                x = by_id.get(gid)
                if x is not None and gid not in graded:
                    graded.add(gid)
                    x["grade"] = _get(g, "verdict", str)
                    x["grade_valid"], x["grade_reason"] = False, f"schema:{err}"
        self._reqs = []
        self.done = True


def apply_grade(x: dict, g: dict, summary: str) -> None:
    """kept and misstated need a summary quote that `quote_in` the summary (spec)."""
    x["grade"], x["grade_note"] = g["verdict"], g["note"]
    x["summary_quote"] = g["summary_quote"] if g["verdict"] != "dropped" else None
    if g["verdict"] in ("kept", "misstated") and not T.quote_in(g["summary_quote"], summary):
        x["grade_valid"], x["grade_reason"] = False, "summary_quote_not_found"
    else:
        x["grade_valid"], x["grade_reason"] = True, None


_VERDICT_RANK = {
    ("supported", True): 0,
    ("supported", False): 1,
    ("contradicted", True): 2,
    ("contradicted", False): 3,
    ("unsupported", True): 4,
    ("unsupported", False): 5,
}


class Pass2(_Task):
    """Claims of one summary checked against the base history (spec "Pass 2").

    One part: one request. Several parts: the first part's answer fixes the claim list, and the
    later parts are asked for verdicts on exactly that list (the prompt's claims block). A later
    part's answer counts for a claim only if its `claim` is the same text after
    `s1_types.norm` (review r1 finding 1: a near match such as "approved"/"disapproved" must not
    carry a verdict over). Merge (spec): supported if any part supports, else contradicted if any
    contradicts, else unsupported; a claim whose merged verdict isn't "supported" is invalid if
    any part failed or returned no exact match for it, since that part might have supported it
    (astra r1 finding 6 on the plan: don't manufacture errors).

    `after`: a Pass1a on the same history; the first request waits for it, so pass 2's history
    prefix is already in the provider's cache (review r1 finding 9)."""

    def __init__(
        self,
        backend,
        rec: dict,
        rendered,
        summary: str,
        rid: str,
        prompt_set: Mapping[str, Prompt],
        part: Partition,
        after: _Task | None = None,
    ):
        self.backend, self.rec, self.rendered, self.summary = backend, rec, rendered, summary
        self.rid, self.p, self.after = rid, prompt_set, after
        self.partition = part
        self.parts = part.parts
        self.claims: list[dict] = []
        self.errors: list[dict] = []
        self.complete = history_complete(rec, rendered)
        self._stage = 1
        self._base: list[dict] = []
        self._failed: list[int] = []
        self._reqs: list[T.Req] = []
        if part.oversize:
            self._err(None, "unscorable_oversize")
            self.done = True
        elif summary_over_reserve(summary, part.n, backend.bytes_per_token):
            self._err(None, "summary_over_reserve")
            self.done = True
        else:
            self._reqs = [self._make(self.parts[0], "")]

    def _make(self, pt: Part, claims_block: str) -> T.Req:
        user = self.p["2"].fill(
            "user", history=pt.text, summary=self.summary, claims_block=claims_block
        )
        return _req(
            f"p2|{self.rid}|n{len(self.parts)}|c{pt.k}",
            self.p["2"].system,
            user,
            T.PASS2_SCHEMA,
            MAX_OUT_2,
            self.rid,
            self.rec,
        )

    def pending(self) -> list[T.Req]:
        if self._stage == 1 and self.after is not None and not self.after.done:
            return []
        return self._reqs

    def _err(self, k: int | None, err: str) -> None:
        self.errors.append(
            {"rid": self.rid, "pass": "2", "chunk": k, "n_chunks": len(self.parts), "error": err}
        )

    def feed(self, resps: Mapping[str, T.Resp]) -> None:
        if self._stage == 1:
            el = elements(resps.get(self._reqs[0].key), T.PASS2_SCHEMA, "claims")
            if el.error:
                self._err(0, el.error)
            for e in el.good:
                self._base.append(
                    {
                        "claim": e["claim"],
                        "summary_quote": e["summary_quote"],
                        "is_permission": e["is_permission"],
                        "per_part": {0: check_claim(e, self.rendered, self.parts[0])},
                        "missing_in": [],
                        "schema_error": None,
                    }
                )
            for e, err in el.bad:
                self._base.append(malformed_claim(e, err))
            listed = [c for c in self._base if c["schema_error"] is None]
            if len(self.parts) == 1 or not listed:
                self._finish()
                return
            lines = "\n".join(
                json.dumps(
                    {"claim": c["claim"], "summary_quote": c["summary_quote"]}, ensure_ascii=False
                )
                for c in listed
            )
            block = self.p["2"].fill("claims_block", claims=lines)
            self._reqs = [self._make(pt, block) for pt in self.parts[1:]]
            self._stage = 2
            return
        listed = [c for c in self._base if c["schema_error"] is None]
        for pt, req in zip(self.parts[1:], self._reqs):
            el = elements(resps.get(req.key), T.PASS2_SCHEMA, "claims")
            if el.error:
                self._err(pt.k, el.error)
                if not el.good:
                    self._failed.append(pt.k)
                    continue
            for c, e in zip(listed, match_claims(listed, el.good)):
                if e is None:
                    c["missing_in"].append(pt.k)
                    continue
                c["per_part"][pt.k] = check_claim(e, self.rendered, pt)
                c["is_permission"] = c["is_permission"] or e["is_permission"]
        self._finish()

    def _finish(self) -> None:
        out = []
        for n, c in enumerate(self._base):
            row = {
                "n": n,
                "claim_id": f"{self.rid}#{self.backend.name}#c{n}",
                "claim": c["claim"],
                "summary_quote": c["summary_quote"],
                "summary_quote_match": None,
                "summary_quote_submitted": c["summary_quote"],  # the judge's own anchor
                "is_permission": c["is_permission"],
                "history_complete": self.complete,
                "n_chunks": len(self.parts),
            }
            if c["schema_error"] is not None:
                p0 = c["per_part"][0]
                row.update(
                    verdict=p0["verdict"],
                    derived=None,
                    evidence_quote=p0["evidence_quote"],
                    msg=p0["msg"],
                    msg_claimed=p0["msg"],
                    note=p0["note"],
                    valid=False,
                    invalid_reason=f"schema:{c['schema_error']}",
                    chunk_verdicts={},
                )
                out.append(row)
                continue
            best = min(
                c["per_part"].values(),
                key=lambda r: _VERDICT_RANK[(r["verdict"], r["evidence_ok"])],
            )
            reasons = []
            # the claim may reword; its summary_quote must locate a span of the summary, exactly,
            # after markdown/case normalisation, or fuzzily (`locate_summary_quote`, 20 Oct)
            if len(T.norm(c["summary_quote"] or "")) > T.QUOTE_MAX_CHARS:
                reasons.append("summary_quote_too_long")
            else:
                span, tier, why = check_anchor(c["summary_quote"], self.summary)
                if why:
                    reasons.append(why)
                else:
                    row["summary_quote"], row["summary_quote_match"] = span, tier
            if not best["evidence_ok"]:
                reasons.append("evidence_not_found")
            if best["verdict"] != "supported":
                if self._failed:
                    reasons.append("chunk_failed:" + ",".join(map(str, self._failed)))
                if c["missing_in"]:
                    reasons.append("missing_in_chunk:" + ",".join(map(str, c["missing_in"])))
            verdict = best["verdict"]
            row.update(
                verdict=verdict,
                derived=(
                    ("invented" if self.complete else "unknown")
                    if verdict == "unsupported"
                    else None
                ),
                evidence_quote=best["evidence_quote"],
                msg=best["msg"],
                msg_claimed=best["msg_claimed"],
                note=best["note"],
                valid=not reasons,
                invalid_reason="; ".join(reasons) or None,
                chunk_verdicts={str(k): r["verdict"] for k, r in sorted(c["per_part"].items())},
            )
            out.append(row)
        self.claims = out
        self._reqs = []
        self.done = True


FUZZY_RATIO = 0.85  # summary anchors only (e2e, 20 Oct); history and item quotes stay exact
FUZZY_LEN_SLACK = 0.30
_BULLET = re.compile(r"(?m)^[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
_DROP = set("*`#>")  # `_` only at word boundaries (emphasis), never inside identifiers
_EMPH_UNDERSCORE = re.compile(r"_+")
_QUOTES = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'}


def _norm_map(text: str, lower: bool = True) -> tuple[str, list[int]]:
    """Markdown- and case-insensitive form of `text` (no * ` # >, no `_` used as emphasis at a
    word boundary but `_` inside identifiers kept, no leading bullets, straight quotes,
    lowercase unless `lower=False`, whitespace collapsed), with each output character's index in
    `text`."""
    skip = set()
    for m in _BULLET.finditer(text):
        skip.update(range(m.start(), m.end()))
    for m in _EMPH_UNDERSCORE.finditer(text):
        a, b = m.start(), m.end()
        inner = a > 0 and text[a - 1].isalnum() and b < len(text) and text[b].isalnum()
        if not inner:
            skip.update(range(a, b))
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if i in skip or ch in _DROP:
            continue
        ch = _QUOTES.get(ch, ch)
        ch = ch.lower() if lower else ch
        if ch.isspace():
            if not out or out[-1] == " ":
                continue
            ch = " "
        out.append(ch)
        idx.append(i)
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def _exact_span(quote: str, summary: str) -> str | None:
    words = quote.split()
    if not words:
        return None
    m = re.search(r"\s+".join(map(re.escape, words)), summary)
    return m.group(0) if m else None


def locate_summary_quote(quote: str | None, summary: str) -> tuple[str | None, str | None]:
    """Where a pass-2 `summary_quote` sits in the summary: (the summary's own text there, tier).
    Tiers: "exact" (`s1_types.quote_in`); "normalized" (equal after `_norm_map`); "fuzzy" (the
    best word-aligned window of the normalised summary with difflib ratio >= FUZZY_RATIO and
    length within FUZZY_LEN_SLACK of the quote's). (None, None) if none applies. The anchor
    only locates the claim; its verdict rests on the exact evidence quote."""
    if not quote or len(T.norm(quote)) < T.QUOTE_MIN_CHARS:
        return None, None
    if T.quote_in(quote, summary):
        return _exact_span(quote, summary) or T.norm(quote), "exact"
    ns, idx = _norm_map(summary)
    nq, _ = _norm_map(quote)
    if not nq or not ns:
        return None, None
    k = ns.find(nq)
    if k >= 0:
        return summary[idx[k] : idx[k + len(nq) - 1] + 1], "normalized"
    r, a, b = fuzzy_best(ns, nq)
    if r < FUZZY_RATIO:
        return None, None
    return summary[idx[a] : idx[b - 1] + 1], "fuzzy"


NEGATION_WORDS = frozenset(
    {
        "not",
        "no",
        "never",
        "n't",
        "without",
        "none",
        "nor",
        "cannot",
        "can't",
        "won't",
        "don't",
        "didn't",
        "isn't",
        "wasn't",
        "unless",
        "except",
    }
)
MODALITY_WORDS = frozenset(
    {
        "must",
        "may",
        "might",
        "can",
        "could",
        "should",
        "shall",
        "will",
        "would",
        "need",
        "needs",
        "required",
        "allowed",
        "permitted",
        "forbidden",
        "prohibited",
        "approved",
        "rejected",
        "denied",
        "granted",
    }
)  # integration r2: fuzzy anchors passed "must" vs "may" and "approved" vs "rejected"
_IDENT_CHARS = re.compile(r"[_./:()]")
_CAMEL = re.compile(r"[a-z][A-Z]")
_TICKS = re.compile(r"`([^`]+)`")


def _strip_tok(t: str) -> str:
    """A word without the sentence punctuation around it (brackets kept when balanced inside)."""
    prev = None
    while t and t != prev:
        prev = t
        t = t.strip("\"',;!?[]{}").rstrip(".:")
        if t.startswith("(") and ")" not in t:
            t = t[1:]
        if t.endswith(")") and "(" not in t:
            t = t[:-1]
    return t


def _anchor_tokens(raw: str) -> list[str]:
    text, _ = _norm_map(raw, lower=False)
    return [t for t in (_strip_tok(w) for w in text.split(" ")) if t]


def anchor_mismatch(submitted: str, located: str) -> str | None:
    """For a non-exact anchor match: the first token class whose multiset differs between the
    judge's anchor and the located summary span ("numbers": tokens with a digit; "negation":
    NEGATION_WORDS and "n't" forms; "modality": MODALITY_WORDS; "identifiers": tokens with _ . / : ( ), camelCase, or
    backticked in either string), else None (integration review r1 finding 3: "approved" vs
    "never approved", 46 vs 45 and foo_bar vs foobar must not match)."""
    ticked = {
        _strip_tok(w).lower()
        for raw in (submitted, located)
        for span in _TICKS.findall(raw)
        for w in span.split()
    }
    toks = {k: _anchor_tokens(v) for k, v in (("s", submitted), ("l", located))}

    def classes(ts: list[str]) -> dict[str, collections.Counter]:
        low = [t.lower() for t in ts]
        return {
            "numbers": collections.Counter(t for t in low if any(ch.isdigit() for ch in t)),
            "negation": collections.Counter(
                t for t in low if t in NEGATION_WORDS or t.endswith("n't")
            ),
            "modality": collections.Counter(t for t in low if t in MODALITY_WORDS),
            "identifiers": collections.Counter(
                t.lower()
                for t in ts
                if _IDENT_CHARS.search(t) or _CAMEL.search(t) or t.lower() in ticked
            ),
        }

    a, b = classes(toks["s"]), classes(toks["l"])
    for k in ("numbers", "negation", "modality", "identifiers"):
        if a[k] != b[k]:
            return k
    return None


def check_anchor(quote: str | None, summary: str) -> tuple[str | None, str | None, str | None]:
    """(located span, tier, invalid reason) for a pass-2 summary anchor: `locate_summary_quote`,
    then, for a normalized or fuzzy match, `anchor_mismatch`. Reasons: "claim_not_in_summary",
    "anchor_mismatch:<class>"."""
    span, tier = locate_summary_quote(quote, summary)
    if tier is None:
        return None, None, "claim_not_in_summary"
    if tier != "exact":
        which = anchor_mismatch(quote or "", span or "")
        if which:
            return span, None, f"anchor_mismatch:{which}"
    return span, tier, None


FUZZY_ANCHOR_WORDS = 3  # the quote's rarest content words that anchor candidate windows
FUZZY_ANCHOR_SLACK = 8  # words either side of each anchored start
_WORD_KEY = re.compile(r"[^\w$%.-]+")
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "this",
        "that",
        "as",
        "at",
        "by",
        "from",
        "not",
        "no",
        "but",
        "if",
        "then",
        "so",
        "we",
        "i",
        "you",
        "he",
        "she",
        "they",
        "its",
        "their",
        "our",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
    ]
)


def fuzzy_best(ns: str, nq: str, prefilter: bool = True) -> tuple[float, int, int]:
    """(ratio, start, end) of the best word-aligned window of `ns` for `nq` (both normalised),
    with length within FUZZY_LEN_SLACK of the quote's. With `prefilter`, only windows starting
    within FUZZY_ANCHOR_SLACK words of where the quote would start, given an occurrence of one
    of its FUZZY_ANCHOR_WORDS rarest content words (rarest in the summary), are scored."""
    words = ns.split(" ")
    w_start, pos = [], 0
    for w in words:
        w_start.append(pos)
        pos += len(w) + 1
    w_end = [st + len(w) for st, w in zip(w_start, words)]
    lo, hi = len(nq) * (1 - FUZZY_LEN_SLACK), len(nq) * (1 + FUZZY_LEN_SLACK)
    cand: Iterable[int] = range(len(words))
    if prefilter:
        keys = [_WORD_KEY.sub("", w) for w in words]  # punctuation off: '"tests":' ~ 'tests:'
        freq = collections.Counter(keys)
        qkeys = [_WORD_KEY.sub("", w) for w in nq.split(" ")]
        content = {w for w in qkeys if len(w) >= 3 and w not in _STOP and freq[w]}
        anchors = sorted(content, key=lambda w: (freq[w], w))[:FUZZY_ANCHOR_WORDS]
        offset = {w: qkeys.index(w) for w in anchors}
        keep: set[int] = set()
        for p, w in enumerate(keys):
            if w in offset:
                c = p - offset[w]
                keep.update(range(c - FUZZY_ANCHOR_SLACK, c + FUZZY_ANCHOR_SLACK + 1))
        cand = sorted(k for k in keep if 0 <= k < len(words))
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(nq)
    best = (0.0, 0, 0)
    for i in cand:
        a = w_start[i]
        j = bisect.bisect_left(w_end, a + lo, lo=i)
        while j < len(words) and w_end[j] - a <= hi:
            sm.set_seq1(ns[a : w_end[j]])
            if sm.real_quick_ratio() > best[0] and sm.quick_ratio() > best[0]:
                r = sm.ratio()
                if r > best[0]:
                    best = (r, a, w_end[j])
            j += 1
    return best


def check_claim(e: dict, rendered, part: Part) -> dict:
    """Code checks on one pass-2 claim from one part."""
    ev = e["evidence_quote"] if e["verdict"] != "unsupported" else None
    msg = None
    ok = True
    if e["verdict"] in ("supported", "contradicted"):
        msg = locate(ev, rendered, e["msg"], prefer=part.msgs)
        ok = msg is not None
    return {
        "verdict": e["verdict"],
        "evidence_quote": ev,
        "msg": msg if msg is not None else (e["msg"] if ev else None),
        "msg_claimed": e["msg"],
        "evidence_ok": ok,
        "note": e["note"],
    }


def malformed_claim(e: Any, err: str) -> dict:
    """A schema-invalid pass-2 element, kept as an invalid row with the fields it has."""
    return {
        "claim": _get(e, "claim", str),
        "summary_quote": _get(e, "summary_quote", str),
        "is_permission": _get(e, "is_permission", bool, False),
        "per_part": {
            0: {
                "verdict": _get(e, "verdict", str),
                "evidence_quote": _get(e, "evidence_quote", str),
                "msg": _get(e, "msg", int),
                "note": _get(e, "note", str),
            }
        },
        "missing_in": [],
        "schema_error": err,
    }


def match_claims(base: list[dict], els: list[dict]) -> list[dict | None]:
    """For each base claim, the later part's answer whose claim is the same text after
    `s1_types.norm`, else None. No fuzzy matching (review r1 finding 1)."""
    free = list(range(len(els)))
    out: list[dict | None] = []
    for c in base:
        key = T.norm(c["claim"])
        hit = next((j for j in free if T.norm(els[j]["claim"]) == key), None)
        if hit is not None:
            free.remove(hit)
        out.append(els[hit] if hit is not None else None)
    return out


# ------------------------------------------------------------------------------------------
# Public single-record calls (spec signatures)
# ------------------------------------------------------------------------------------------


def pass1a(
    backend,
    rec: dict,
    rendered,
    *,
    errors: list | None = None,
    purpose: str = PURPOSE,
    prompt_set: Mapping[str, Prompt] | None = None,
) -> list[dict]:
    """`prompt_set` (default: the frozen prompts) lets scripts/s1_hillclimb.py try candidates."""
    check_privacy(backend, [rec])
    p = prompt_set or prompts()
    t = Pass1a(backend, rec, rendered, p, partition(backend, p, rendered))
    drive(backend, [t], purpose)
    if errors is not None:
        errors.extend(t.errors)
    return t.items


def pass1b(
    backend,
    items: list[dict],
    summary: str,
    rid: str,
    *,
    rec: dict | None = None,
    errors: list | None = None,
    purpose: str = PURPOSE,
) -> list[dict]:
    """`rec` supplies source and privacy for the requests; without it they are marked private
    (an API backend then refuses them), the safe default."""
    rec = rec or {"id": rid, "source": "unknown"}
    check_privacy(backend, [rec])
    t = Pass1b(backend, items, summary, rid, rec, prompts())
    drive(backend, [t], purpose)
    if errors is not None:
        errors.extend(t.errors)
    return t.items


def pass2(
    backend,
    rec: dict,
    rendered,
    summary: str,
    *,
    rid: str | None = None,
    errors: list | None = None,
    purpose: str = PURPOSE,
) -> list[dict]:
    check_privacy(backend, [rec])
    p = prompts()
    part = partition(backend, p, rendered)
    t = Pass2(backend, rec, rendered, summary, rid or str(rec["id"]), p, part)
    drive(backend, [t], purpose)
    if errors is not None:
        errors.extend(t.errors)
    return t.claims


# ------------------------------------------------------------------------------------------
# Availability of dropped and mis-stated items
# ------------------------------------------------------------------------------------------

READ_TOOLS = frozenset(
    {
        "read",
        "view",
        "read_file",
        "readfile",
        "view_file",
        "open_file",
        "read_many_files",
        "readmanyfiles",
        "notebookread",
        "cat",
    }
)
EDITOR_TOOLS = frozenset(
    {"str_replace_editor", "str_replace_based_edit_tool", "text_editor", "editor"}
)  # a file read only with command "view"
SHELL_TOOLS = frozenset(
    {
        "bash",
        "shell",
        "execute_bash",
        "execute_command",
        "exec_command",
        "run_command",
        "run_shell_command",
        "terminal",
        "local_shell",
        "run_terminal_cmd",
        "container.exec",
        "exec",
    }
)
PATH_KEYS = (
    "file_path",
    "path",
    "filePath",
    "filename",
    "file",
    "absolute_path",
    "target_file",
    "paths",
    "notebook_path",
)
READ_PROGS = frozenset({"cat", "head", "tail", "less", "more", "nl"})


def _args(call: dict) -> dict:
    a = call.get("args")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except (json.JSONDecodeError, ValueError):
            return {"command": a}
    return a if isinstance(a, dict) else {}


def command_of(call: dict) -> str:
    a = _args(call)
    c = a.get("command", a.get("cmd", a.get("script", "")))
    if isinstance(c, list):
        if len(c) >= 3 and str(c[1]) in ("-c", "-lc"):
            return str(c[2])
        return " ".join(map(str, c))
    return str(c or "")


_SED_PRINT = re.compile(r"(?:\d+|\$)p|\d+,(?:\d+|\$)p|/[^/]*/p")


def _tokens(cmd: str) -> list[str] | None:
    """Shell tokens with control operators split out (outside quotes only), or None if the
    command can't be tokenised."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def _reader(words: list[str], need_path: bool) -> bool:
    while words and (re.fullmatch(r"\w+=\S*", words[0]) or words[0] in ("sudo", "command")):
        words = words[1:]
    if not words:
        return False
    prog = posixpath.basename(words[0])
    if prog in READ_PROGS:
        return bool([w for w in words[1:] if not w.startswith("-")]) or not need_path
    if prog == "sed":  # only `sed -n '<N>p' | '<N>,<M>p' | '/re/p'` [paths]
        if len(words) < 3 or words[1] != "-n" or not _SED_PRINT.fullmatch(words[2]):
            return False
        paths = words[3:]
        if any(w.startswith("-") for w in paths):
            return False
        return bool(paths) or not need_path
    return False


def shell_reads_file(cmd: str) -> bool:
    """True only when the whole command is one file read, so its output can be attributed to a
    file (review r1 finding 2, r2): cat/head/tail/less/more/nl on a path, or a read-only
    `sed -n` print script on a path, optionally piped into further readers
    (`nl -ba f | sed -n '1,80p'`). Any other shell control operator outside quotes (`;`, `&`,
    `&&`, `||`, redirections, parentheses), a newline, command substitution, or a pipe into a
    non-reader (`cat a | grep x`) makes it not a file read."""
    cmd = (cmd or "").strip()
    if not cmd or "\n" in cmd or "`" in cmd or "$(" in cmd:
        return False
    toks = _tokens(cmd)
    if not toks:
        return False
    segs: list[list[str]] = [[]]
    for t in toks:
        if t == "|":
            segs.append([])
        elif t and all(ch in "();<>|&" for ch in t):
            return False  # any other control operator
        else:
            segs[-1].append(t)
    return _reader(segs[0], need_path=True) and all(
        _reader(seg, need_path=False) for seg in segs[1:]
    )


def reads_file(call: dict) -> bool:
    name = str(call.get("name") or "").lower()
    a = _args(call)
    has_path = any(a.get(k) for k in PATH_KEYS)
    if name in READ_TOOLS:
        return has_path or name == "cat"
    if name in EDITOR_TOOLS:
        return has_path and str(a.get("command", "")).lower() == "view"
    if name in SHELL_TOOLS or "bash" in name or "shell" in name:
        return shell_reads_file(command_of(call))
    return False


def calls_for_results(messages: list[dict]) -> dict[int, dict]:
    """{index of a tool message: the call it answers}: by `tool_use_id` (or `tool_call_id`),
    else by position among the latest assistant message's unanswered calls."""
    by_id: dict[str, dict] = {}
    out: dict[int, dict] = {}
    open_calls: list[dict] = []
    for i, m in enumerate(messages):
        calls = m.get("tool_calls") or []
        if calls and m.get("role") != "tool":
            for c in calls:
                if c.get("id") is not None:
                    by_id[str(c["id"])] = c
            open_calls = list(calls)
        if m.get("role") == "tool":
            tid = m.get("tool_use_id") or m.get("tool_call_id")
            c = by_id.get(str(tid)) if tid is not None else None
            if c is None and open_calls:
                c = open_calls[0]
            if c is not None:
                out[i] = c
                open_calls = [x for x in open_calls if x is not c]
    return out


def _message_blob(m: dict) -> str:
    parts = [m.get("text") or ""]
    for c in m.get("tool_calls") or []:
        a = c.get("args")
        parts.append(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
    return "\n".join(parts)


# Claude Code's instruction-file reminder: "Codebase and user instructions are shown below",
# then one "Contents of <path> (<description>):" section per file (CLAUDE.md, AGENTS.md, the
# auto-memory MEMORY.md). The harness attaches it again after a compaction (spec D12).
INSTRUCTIONS_MARK = "Codebase and user instructions are shown below"
_CONTENTS_RE = re.compile(r"^Contents of (\S[^\n]*?) \([^\n]*\):[ \t]*$", re.MULTILINE)


def instruction_files(m: dict) -> list[tuple[str, str]]:
    """(file name, text) of each instruction file in a harness instruction message, else []."""
    text = m.get("text")
    if m.get("role") not in ("system", "user") or not isinstance(text, str):
        return []
    mark = text.find(INSTRUCTIONS_MARK)
    if mark < 0:
        return []
    # only a harness reminder that holds the mark (opened before it and not yet closed), from
    # the mark to that reminder's close tag; the mark quoted anywhere else is not one
    opened = text.rfind("<system-reminder>", 0, mark)
    if opened < 0 or text.find("</system-reminder>", opened, mark) >= 0:
        return []
    close = text.find("</system-reminder>", mark)
    if close < 0:
        return []
    block = text[mark:close]
    heads = list(_CONTENTS_RE.finditer(block))
    out = []
    for k, h in enumerate(heads):
        end = heads[k + 1].start() if k + 1 < len(heads) else len(block)
        out.append((h.group(1).rsplit("/", 1)[-1], block[h.end() : end]))
    return out


def availability(item: dict, rec: dict, kept_messages: list[dict]) -> tuple[str, str]:
    """Spec order: still_present (the quote is in the post-compaction context the agent keeps:
    `kept_messages`, e.g. U1's `post_context(rec)` or a replay's kept messages), reloaded (the
    quote is in an instruction file the harness put in context, which Claude Code attaches again
    after a compaction; spec D12), recoverable (the quote sits in the output of a tool call that
    read a file by path, so the agent could re-read it; this assumes the file is unchanged), else
    lost. Returns (availability, how)."""
    quote = item.get("quote")
    for j, m in enumerate(kept_messages or []):
        if T.quote_in(quote, _message_blob(m)):
            return "still_present", f"kept_message:{j}"
    msgs = history_messages(rec)
    for i, m in enumerate(msgs):
        for name, body in instruction_files(m):
            if T.quote_in(quote, body):
                return "reloaded", f"instruction_file:{name}@m{i}"
    for i, call in sorted(calls_for_results(msgs).items()):
        if reads_file(call) and T.quote_in(quote, msgs[i].get("text") or ""):
            return "recoverable", f"file_read:{call.get('name')}@m{i}"
    return "lost", "not_in_kept_context_or_file_read"


# ------------------------------------------------------------------------------------------
# Code-scored facts (ported from P01; module docstring)
# ------------------------------------------------------------------------------------------

_CMD_POS = (
    r"(?:^|[;&|(\n])\s*(?:\w+=\S*\s+)*(?:time\s+|timeout\s+\S+\s+)?"
    r"(?:(?:uv|poetry|pipenv|hatch)\s+run\s+)?(?:python[0-9.]*\s+-m\s+)?"
)
SUITE_CMD_RE = re.compile(
    _CMD_POS + r"(pytest|py\.test|nosetests)\b|python[0-9.]*\s+-m\s+unittest\b"
    r"|(?:^|&&|;|\|\||\|)\s*tox\b"
    r"|manage\.py\s+test|\b(npm|yarn|pnpm)\s+(run\s+)?test\b|\bgo\s+test\b|\bcargo\s+test\b"
    r"|\bmake\s+test\b"
)
SCRIPT_CMD_RE = re.compile(r"python[0-9.]*\s+(-\S+\s+)*\S*test\S*\.py")
NOT_A_RUN_RE = re.compile(r"--version\b|--help\b|--collect-only\b|\s-h\b")
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", re.DOTALL)
_PYTEST_COUNTS_RE = re.compile(
    r"(\d+) (passed|failed|errors?|error|skipped|xfailed|xpassed|deselected|warnings?)"
)
_PYTEST_SUMMARY_RE = re.compile(
    r"^[= ]*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
    r"(?:, )?)+)(?: in [\d.]+s)?[= ]*$|^=+ no tests ran",
    re.MULTILINE,
)
_UNITTEST_RAN_RE = re.compile(r"^Ran \d+ tests? in", re.MULTILINE)
_UNITTEST_FAIL_RE = re.compile(r"^FAILED \((failures|errors)=", re.MULTILINE)
_UNITTEST_OK_RE = re.compile(r"^OK\b", re.MULTILINE)
_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\)|^\w*(Error|Exception)\b.*:", re.MULTILINE
)
_ALL_PASS_RE = re.compile(r"\ball (\d+ )?tests? (have )?passed\b|\ball tests pass\b", re.IGNORECASE)
_TIMEOUT_RE = re.compile(r"timed out|Timeout", re.IGNORECASE)


def test_command_kind(cmd: str) -> str | None:
    """P01 common.test_command_kind: "suite", "script", or None (not a test run)."""
    cmd = _HEREDOC_RE.sub("<<HEREDOC", cmd or "")
    if NOT_A_RUN_RE.search(cmd):
        return None
    if SUITE_CMD_RE.search(cmd):
        return "suite"
    if SCRIPT_CMD_RE.search(cmd):
        return "script"
    return None


def classify_test_output(text: str) -> str:
    """P01 common.classify_test_output: "pass" | "fail" | "unknown"."""
    text = text or ""
    summ = list(_PYTEST_SUMMARY_RE.finditer(text))
    if summ:
        kinds = {k for _, k in _PYTEST_COUNTS_RE.findall(summ[-1].group(0))}
        if kinds & {"failed", "error", "errors"}:
            return "fail"
        return "pass" if "passed" in kinds else "unknown"
    if _UNITTEST_RAN_RE.search(text):
        if _UNITTEST_FAIL_RE.search(text):
            return "fail"
        if _UNITTEST_OK_RE.search(text):
            return "pass"
    if _TIMEOUT_RE.search(text[-300:]) and len(text) < 400:
        return "unknown"
    if _TRACEBACK_RE.search(text) or "AssertionError" in text:
        return "fail"
    return "pass" if _ALL_PASS_RE.search(text) else "unknown"


def test_runs(messages: list[dict]) -> list[dict]:
    """Every shell call in the history that runs tests, with its kind and status."""
    runs = []
    for i, call in sorted(calls_for_results(messages).items()):
        name = str(call.get("name") or "").lower()
        if not (name in SHELL_TOOLS or "bash" in name or "shell" in name):
            continue
        kind = test_command_kind(command_of(call))
        if kind:
            runs.append(
                {
                    "msg": i,
                    "kind": kind,
                    "status": classify_test_output(messages[i].get("text") or ""),
                }
            )
    return runs


def latest_run_status(messages: list[dict]) -> dict[str, str]:
    """P01 judge.latest_run_status + common.last_test, per kind ("suite", "script"): the last
    classifiable run's status, "unknown" when the very last run of that kind can't be
    classified (P01 review r1 finding 6) or no run can, "none" when there is no run."""
    runs = test_runs(messages)
    out = {}
    for kind in ("suite", "script"):
        rk = [r for r in runs if r["kind"] == kind]
        known = [r for r in rk if r["status"] != "unknown"]
        if not rk:
            out[kind] = "none"
        elif rk[-1]["status"] == "unknown" or not known:
            out[kind] = "unknown"
        else:
            out[kind] = known[-1]["status"]
    return out


def tests_misstatement(truth: Mapping[str, str], claim: str) -> bool | None:
    """P01 judge.tests_misstatement without the slice rules: a "pass" or "fail" claim is true if
    either the last suite run or the last test-script run says so (lenient, a lower bound);
    None = not scorable (a run of unknown status). "pass" with no test run at all is false."""
    suite, script = truth["suite"], truth["script"]
    if claim in (suite, script):
        return False
    if "unknown" in (suite, script):
        return None
    return True


_SENT_RE = re.compile(r"[^\n.!?]+[.!?]?")
_PASS_CLAIM_RE = re.compile(
    r"\ball\s+(?:\d+\s+)?(?:\w+\s+)?tests?\s+(?:now\s+|are\s+)*pass(?:ed|ing|es)?\b"
    r"|\btests?\s+(?:are\s+|now\s+|all\s+)*(?:passing|passed|pass)\b|\b\d+\s+passed\b"
    r"|\b\d+/\d+\s+(?:tests?\s+)?pass",
    re.IGNORECASE,
)
_FAIL_CLAIM_RE = re.compile(
    r"\b\d+\s+failed\b|\btests?\s+(?:are\s+|still\s+|now\s+)*fail(?:ing|ed|s)?\b"
    r"|\bfailing\s+tests?\b",
    re.IGNORECASE,
)
_EXCEPT_RE = re.compile(r"\bexcept\b", re.IGNORECASE)
_PLAN_RE = re.compile(
    r"\b(should|need(?:s)? to|next|will|todo|to make|ensure|verify that|"
    r"run the|until)\b",
    re.IGNORECASE,
)


def summary_test_claims(summary: str) -> list[dict]:
    """Sentences of the summary that report a test outcome (code, P01 lesson: plans and next
    steps are not outcome claims): {"sentence", "claim": "pass" | "fail"}."""
    out = []
    for m in _SENT_RE.finditer(summary or ""):
        s = m.group(0).strip()
        if not s or _PLAN_RE.search(s):
            continue
        p, f = _PASS_CLAIM_RE.search(s), _FAIL_CLAIM_RE.search(s)
        if f or (p and _EXCEPT_RE.search(s)):  # "all pass except test_x" is a fail claim
            out.append({"sentence": s[:300], "claim": "fail"})
        elif p:
            out.append({"sentence": s[:300], "claim": "pass"})
    return out


_EXT = (
    r"py|pyi|ipynb|js|mjs|cjs|ts|tsx|jsx|md|mdx|rst|txt|json|jsonl|toml|yaml|yml|ini|cfg|"
    r"sh|bash|zsh|rs|go|java|kt|c|cc|cpp|h|hpp|rb|php|swift|html|css|scss|sql|lock|csv|"
    r"xml|env|dockerfile|tf|proto"
)
_PATH_RE = re.compile(
    rf"(?<![\w/.:-])((?:\.{{0,2}}/)?(?:[\w.@-]+/)*[\w@-][\w.@-]*\.(?:{_EXT}))"
    r"(?![\w/-])",
    re.IGNORECASE,
)
_DIR_PATH_RE = re.compile(r"(?<![\w/.:-])((?:\.{0,2}/)?(?:[\w.@-]+/){1,}[\w.@-]+/?)(?![\w/-])")
_BRANCH_RES = (
    re.compile(r"\bbranch(?:es)?\s+(?:named\s+|called\s+)?[`'\"]([^`'\"\s]+)[`'\"]", re.IGNORECASE),
    re.compile(r"[`'\"]([^`'\"\s]+)[`'\"]\s+branch\b", re.IGNORECASE),
    re.compile(r"\bgit\s+(?:checkout\s+(?:-b\s+)?|switch\s+(?:-c\s+)?)([\w][\w./-]*[\w])"),
    re.compile(r"\bbranch\s+([\w-]+/[\w./-]*[\w])"),
)
_CODE_SPAN_RE = re.compile(r"`([^`\n]{2,300})`")
COMMAND_WORDS = frozenset(
    {
        "git",
        "gh",
        "npm",
        "npx",
        "yarn",
        "pnpm",
        "node",
        "pytest",
        "python",
        "python3",
        "uv",
        "pip",
        "make",
        "cargo",
        "go",
        "ls",
        "cd",
        "cat",
        "grep",
        "rg",
        "sed",
        "awk",
        "docker",
        "bash",
        "sh",
        "curl",
        "wget",
        "rm",
        "mv",
        "cp",
        "mkdir",
        "tox",
        "poetry",
        "ruff",
        "mypy",
        "black",
        "find",
        "chmod",
        "kubectl",
        "terraform",
        "bun",
        "deno",
        "java",
        "mvn",
        "gradle",
    }
)


def _bounded(name: str, low_text: str) -> bool:
    """P01 judge.file_named's boundary match: `name` not inside a longer name."""
    return re.search(rf"(?<![a-z0-9_]){re.escape(name.lower())}(?![a-z0-9_])", low_text) is not None


def history_text(rec: dict) -> str:
    """What the summarizer saw, as the passes see it (`history_messages`: source messages, then
    the kept-verbatim ones), with tool-call arguments."""
    return "\n".join(_message_blob(m) for m in history_messages(rec))


def code_facts(rec: dict, summary: str) -> dict:
    """Checkable facts scored in code (spec): file paths, branch names and commands named in the
    summary but absent from the history, and test-status conflicts against the latest runs.
    A file counts as present if its full path or its basename appears at name boundaries (P01
    `file_named`); a command if its whitespace-normalised text appears verbatim."""
    hist = history_text(rec)
    low = hist.lower()
    nhist = T.norm(hist)

    files = []
    for rx in (_PATH_RE, _DIR_PATH_RE):
        for m in rx.finditer(summary or ""):
            p = m.group(1).rstrip("/.")
            if "://" in summary[max(0, m.start() - 8) : m.end()] or p in files:
                continue
            if rx is _DIR_PATH_RE and (p.count("/") < 1 or re.fullmatch(r"[\d./]+", p)):
                continue
            files.append(p)
    files_absent = [
        p for p in files if not (_bounded(p, low) or _bounded(posixpath.basename(p), low))
    ]

    branches: list[str] = []
    for rx in _BRANCH_RES:
        for m in rx.finditer(summary or ""):
            b = m.group(1).strip(".,;:")
            if b and b not in branches:
                branches.append(b)
    branches_absent = [b for b in branches if not _bounded(b, low)]

    commands = []
    for m in _CODE_SPAN_RE.finditer(summary or ""):
        c = m.group(1).strip()
        first = c.split()[0] if c.split() else ""
        if posixpath.basename(first) in COMMAND_WORDS and len(c.split()) >= 2 and c not in commands:
            commands.append(c)
    commands_absent = [c for c in commands if T.norm(c) not in nhist]

    truth = latest_run_status(history_messages(rec))
    claims = summary_test_claims(summary)
    for c in claims:
        c["misstated"] = tests_misstatement(truth, c["claim"])
    vals = [c["misstated"] for c in claims]
    conflict = True if any(v is True for v in vals) else (None if None in vals else False)
    return {
        "rid": rec.get("id"),
        "files_named": files,
        "files_absent": files_absent,
        "branches_named": branches,
        "branches_absent": branches_absent,
        "commands_named": commands,
        "commands_absent": commands_absent,
        "test_truth": truth,
        "summary_test_claims": claims,
        "tests_conflict": conflict,
    }


# ------------------------------------------------------------------------------------------
# Rows and the batch driver
# ------------------------------------------------------------------------------------------


def _rec_fields(rec: dict) -> dict:
    return {
        "source": rec.get("source"),
        "session_id": rec.get("session_id"),
        "harness": rec.get("harness"),
        "model": rec.get("model"),
        "pre_tokens": rec.get("pre_tokens"),
    }


def item_row(
    x: dict,
    *,
    rid: str,
    base_rid: str,
    arm: str,
    backend,
    prompt_sha: str,
    rec: dict,
    avail: tuple[str | None, str | None],
    avail_unfiltered: tuple[str | None, str | None] = (None, None),
) -> dict:
    row = {
        "rid": rid,
        "base_rid": base_rid,
        "arm": arm,
        "judge": backend.name,
        "judge_model": backend.model,
        "prompt_sha": prompt_sha,
        "item_id": x["item_id"],
        "type": x["type"],
        "origin": x["origin"],
        "permission_status": x["permission_status"],
        "text": x["text"],
        "quote": x["quote"],
        "msg": x["msg"],
        "msg_claimed": x["msg_claimed"] if x["msg_claimed"] != x["msg"] else None,
        "superseded": x["superseded"],
        "superseded_quote": x["superseded_quote"],
        "valid": x["valid"],
        "invalid_reason": x["invalid_reason"],
        "grade": x["grade"],
        "summary_quote": x["summary_quote"],
        "grade_valid": x["grade_valid"],
        "grade_reason": x["grade_reason"],  # None when the grade is valid
        "availability": avail[0],
        "availability_how": avail[1],
        "availability_unfiltered": avail_unfiltered[0],
        "availability_unfiltered_how": avail_unfiltered[1],
        "assessable": x["assessable"],
        "n_chunks": x["n_chunks"],
        **_rec_fields(rec),
    }
    assert tuple(row) == ITEM_ROW_FIELDS
    return row


def claim_row(
    c: dict, *, rid: str, base_rid: str, arm: str, backend, prompt_sha: str, rec: dict
) -> dict:
    row = {
        "rid": rid,
        "base_rid": base_rid,
        "arm": arm,
        "judge": backend.name,
        "judge_model": backend.model,
        "prompt_sha": prompt_sha,
        "claim_id": c["claim_id"],
        "claim": c["claim"],
        "summary_quote": c["summary_quote"],
        "summary_quote_match": c["summary_quote_match"],
        "summary_quote_submitted": c["summary_quote_submitted"],
        "is_permission": c["is_permission"],
        "verdict": c["verdict"],
        "derived": c["derived"],
        "evidence_quote": c["evidence_quote"],
        "msg": c["msg"],
        "msg_claimed": c["msg_claimed"] if c["msg_claimed"] != c["msg"] else None,
        "valid": c["valid"],
        "invalid_reason": c["invalid_reason"],
        "note": c["note"],
        "history_complete": c["history_complete"],
        "n_chunks": c["n_chunks"],
        **_rec_fields(rec),
    }
    assert tuple(row) == CLAIM_ROW_FIELDS
    return row


def _write(out, rows: list[dict]) -> None:
    if out is None:
        return
    if callable(out):
        out(rows)
        return
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")


def _replay_index(replays, arm: str) -> dict[str, dict]:
    if replays is None:
        return {}
    if isinstance(replays, Mapping):
        return {str(k): v for k, v in replays.items()}
    return {str(r["base_rid"]): r for r in replays if r.get("arm") == arm}


def judge_records(
    backend,
    rids: list[str],
    *,
    arm: str = "natural",
    replays=None,
    out_items=None,
    out_claims=None,
    out_code_facts=None,
    purpose: str = PURPOSE,
    load: Callable | None = None,
    render: Callable | None = None,
    post_context: Callable | None = None,
) -> dict:
    """The batch driver for Part A (arm "natural"), Part B (a replay arm) and the size test.

    `replays`: ReplayRecords for `arm` (a list, or a mapping base_rid -> record). Items come from
    pass 1a on the base history; with the backend's cache keyed by request content, running
    another arm re-sends nothing for pass 1a. `out_*`: a path (JSONL, overwritten) or a callable
    taking the list of rows (e.g. a `seal.write` wrapper). `load` and `post_context` default to
    U1's `s1_data` functions, `render` to `judge_view` (source plus kept-verbatim messages).

    One partition per base history serves pass 1a, every arm's pass 2 and pass 1b. Rounds:
    pass 1a; then pass 2 (its first part, whose history prefix pass 1a has just cached) with
    pass 1b; then pass 2's later parts.

    Returns counts, the errors, and the validity shares over every row, malformed and ungraded
    ones included: an item is valid end to end only if its extraction AND its grade are valid,
    and `valid_share` = (end-to-end valid items + valid claims) / all rows (review r1 finding
    3). Records with any error (failed request, oversize message, summary over the reserve) are
    counted in `records_complete_share`."""
    if arm not in T.ARMS:
        raise ValueError(f"unknown arm {arm!r}")
    if load is None or render is None or post_context is None:
        from compaction import s1_data  # U1; imported here so this module loads without it

        load = load or s1_data.load
        post_context = post_context or s1_data.post_context
    render = render or judge_view
    recs = [load(rid) for rid in rids]
    check_privacy(backend, recs)  # before any request is built (spec D2)

    p = prompts()
    rep = _replay_index(replays, arm) if arm != "natural" else {}
    errors: list[dict] = []
    jobs = []
    p1a_by_base: dict[str, Pass1a] = {}
    p1a_tasks: list[_Task] = []
    later_tasks: list[_Task] = []
    for rec in recs:
        base = str(rec["id"])
        if arm == "natural":
            rid, summary, kept = base, rec.get("summary") or "", post_context(rec)
            unfiltered = None
        else:
            r = rep.get(base)
            if r is None:
                errors.append({"rid": base, "pass": "-", "error": f"no {arm} replay"})
                continue
            rid, summary, kept = str(r["id"]), r.get("summary") or "", r.get("kept_messages") or []
            # codex_local and antigravity: P49's unfiltered user-message carry-over (U4)
            unfiltered = r.get("kept_messages_unfiltered")
        if base not in p1a_by_base:
            rendered = render(rec)
            p1a_by_base[base] = Pass1a(backend, rec, rendered, p, partition(backend, p, rendered))
            p1a_tasks.append(p1a_by_base[base])
        t1a = p1a_by_base[base]
        t1b = Pass1b(backend, t1a, summary, rid, rec, p)
        t2 = Pass2(backend, rec, t1a.rendered, summary, rid, p, t1a.partition, after=t1a)
        later_tasks += [t2, t1b]
        jobs.append((rec, rid, summary, kept, unfiltered, t1b, t2))
    if p1a_tasks:
        drive(backend, p1a_tasks + later_tasks, purpose)

    item_rows, claim_rows, facts = [], [], []
    isha, csha = item_prompt_sha(p), claim_prompt_sha(p)
    n_fixed = 0
    for t in p1a_by_base.values():
        errors += t.errors
        n_fixed += sum(x["msg"] != x["msg_claimed"] for x in t.items)
    for rec, rid, summary, kept, unfiltered, t1b, t2 in jobs:
        base = str(rec["id"])
        errors += t1b.errors + t2.errors
        for x in t1b.items:
            lost_or_wrong = x["grade_valid"] and x["grade"] in ("dropped", "misstated")
            avail = availability(x, rec, kept) if lost_or_wrong else (None, None)
            avail_unf = (
                availability(x, rec, unfiltered)
                if lost_or_wrong and unfiltered is not None
                else (None, None)
            )
            item_rows.append(
                item_row(
                    x,
                    rid=rid,
                    base_rid=base,
                    arm=arm,
                    backend=backend,
                    prompt_sha=isha,
                    rec=rec,
                    avail=avail,
                    avail_unfiltered=avail_unf,
                )
            )
        for c in t2.claims:
            claim_rows.append(
                claim_row(
                    c, rid=rid, base_rid=base, arm=arm, backend=backend, prompt_sha=csha, rec=rec
                )
            )
            n_fixed += bool(c["evidence_quote"]) and c["msg"] != c["msg_claimed"]
        facts.append({**code_facts(rec, summary), "rid": rid, "base_rid": base, "arm": arm})
    _write(out_items, item_rows)
    _write(out_claims, claim_rows)
    _write(out_code_facts, facts)

    def share(rows, k="valid"):
        return sum(bool(r[k]) for r in rows) / len(rows) if rows else None

    graded = [r for r in item_rows if r["valid"]]
    e2e = [r["valid"] and r["grade_valid"] for r in item_rows]
    n_valid = sum(e2e) + sum(r["valid"] for r in claim_rows)
    n_all = len(item_rows) + len(claim_rows)
    # a record is complete when it has no error entry (every chunk and batch returned) and every
    # one of its rows is valid: items valid end to end (grade arrived and valid), claims valid
    bad_records = {str(e["rid"]) for e in errors}
    bad_records |= {r["rid"] for r in item_rows if not (r["valid"] and r["grade_valid"])}
    bad_records |= {r["rid"] for r in claim_rows if not r["valid"]}
    rids_done = [rid for _, rid, *_ in jobs]
    return {
        "judge": backend.name,
        "judge_model": backend.model,
        "arm": arm,
        "n_records": len(jobs),
        "n_items": len(item_rows),
        "n_claims": len(claim_rows),
        "item_valid_share": share(item_rows),
        "grade_valid_share": share(graded, "grade_valid"),
        "item_e2e_valid_share": sum(e2e) / len(e2e) if e2e else None,
        "claim_valid_share": share(claim_rows),
        "valid_share": n_valid / n_all if n_all else None,
        "records_complete_share": (
            sum(r not in bad_records and r.split("@")[0] not in bad_records for r in rids_done)
            / len(rids_done)
            if rids_done
            else None
        ),
        "unscorable": sorted(
            {str(e["rid"]) for e in errors if e["error"] in ("unscorable_oversize",)}
        ),
        "summary_over_reserve": sorted(
            {str(e["rid"]) for e in errors if e["error"] == "summary_over_reserve"}
        ),
        "n_msg_fixed": n_fixed,
        "n_chunked": sum(1 for t in p1a_by_base.values() if len(t.parts) > 1),
        "prompt_sha": {"items": isha, "claims": csha},
        "errors": errors,
    }
