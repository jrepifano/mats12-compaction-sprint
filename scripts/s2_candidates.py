"""S2 close-out, unit 5: the usable-loss candidate screen (docs/sprint/plans/experiment-2-closeout.md,
"Outcome scenarios"). From the live-boundary loss audit's item rows it builds the candidates
Experiment 3 could repair, drafts the checks Haiku can help with, builds Jacob's candidate page,
and freezes the result once.

**Pool** (`pool`, no calls). Rows of `results/sprint/S2/lossaudit/losses.jsonl` (unit 1-3's
contract: S1's ITEM_ROW_FIELDS with the final grade after the second look, plus run_key, task,
channel, subject, policy, half, planted_rule, first_grade, lost, recoverable (recoverable,
not_recoverable, unresolved or null), recoverable_how, recoverable_lines [{path, line, text}],
recoverable_commands [{msg, command}], boundary_dir), filtered in order, each step counted in the funnel and each exclusion logged:
lost; not a planted rule (the retention judge covers those); type task_state, decision,
commitment or permission; one per equivalent item within a boundary (difflib ratio >= S1's
DEDUPE_RATIO on the normalised quote or text, earliest message kept); and the automatic part of
check 3: an item unit 3 calls "recoverable" (found in the boundary's repo state) is excluded
before the page, with the evidence logged. "unresolved" and "not_recoverable" go to Jacob; a
usable "not_recoverable" loss counts in the confirmed count, an "unresolved" (or uncalled) one in
the upper count only. Cards show unit 3's matching lines and the agent's commands that touched
those files under check 3.

**Draft** (`draft --max`, Max plan, $0). One Claude Haiku 5.5 call per candidate through
`compaction.claude_headless.MaxBackend` (effort medium as S1's judge, concurrency 8, its own
content-keyed cache under data/raw/sprint/s2/cache/candidates-haiku-max, so a rerun re-sends
nothing; every `run()` first sends MaxBackend's billing-route canary). Prompt
`src/compaction/s2/prompts/candidate.md`; its sha256 is in every row. The request holds the whole
pre-compaction history and the post-compaction input (tool outputs cut to TOOL_CAP characters,
head and tail, marked), the run and the item last, so one boundary's calls share a prefix; the
model reads the history after the item's message. It drafts check 2 (applies), check 4 (a
governed decision, decision_depends, an observable act) and check 5's design (scorer, repair and
control text). Code checks every reply: enums, each quote found in the history (a missing quote
voids that field), non-empty texts, and a control text of 0.5-2x the repair text's length; a "no" on
check 2 or 4 needs no design (the prompt asks for none). A failed check marks the field invalid
and flags it; it never crashes. An errored or invalid reply is resent under a new request key,
at most MAX_TRIES times in all; keep-best keeps the whole attempt with the most valid parts (an
attempt's parts are never mixed with another's), and every attempt is listed. Output:
results/sprint/S2/lossaudit/candidate_drafts.jsonl, each row with its `request_sha` (prompt and
request text) and `fingerprint` (plus model and effort); resumable: a candidate with a valid
draft for the same fingerprint is not resent.

**Page** (`page`). One card per candidate that reached the page, in seeded random order, with
the five checks (Retention, Applicability, Not recoverable, Governed decision, Frozen design),
each yes / no / unclear with a note, and the evidence: the item and its quote in the full
history (searchable, scrolled to the quote); the post-compaction input with a find box; the
judge's grade and, for a misstated item, its summary quote, shown only once check 1 is answered
(less anchoring); recoverability and availability with their evidence; Haiku's drafts with any
failed code check shown. Check 5 holds the final design as editable fields (repair text, control
text, observable act, scorer), starting from Haiku's draft, plus the fixed never-reached rule.
Not blind: this is an assisted screen, never reported as an independent calibration (that is
unit 4's blind page). The page refuses drafts whose request no longer matches (`StaleDrafts`),
and its draw id hashes every card's content (shown fields, candidate row, both boundary texts,
prompt), so labels saved on one page never attach to changed evidence. Built like
scripts/s2_retention_page.py (template, history box, db collections `labels` and `meta`, doc id =
card id, a copyable summary as the fallback); the page and its key (card id -> candidate id, the
page funnel) go under data/raw/ (gitignored). The page data carries only CARD_FIELDS per card.
`--exclude-draft-no` (off by default) also drops candidates whose valid draft says "no" on check 2
or 4, counted in the funnel; the key records the setting.

**Freeze** (`freeze --labels FILE`). The labels as the main session reads them back with
ArtifactData: a JSON list of {"id", "collection", "data"} or {collection: {id: data}}
(`s1_label_score.normalize_labels`). Every label must carry the page's draw id. A usable loss
passes all five checks; "unclear" or "no" on any one excludes it, with the reason logged.
Refuses while any card misses a check, or says yes on check 5 over an incomplete final design
(an empty field, or a control outside 0.5-2x the repair's length), unless --allow-incomplete
(then a missing answer and an incomplete design count as not passing, each counted separately).
The CLI first rebuilds the page from the current inputs (each read once, its sha256 taken of the
bytes used) and refuses unless the draw and page sha256 match the page key. Writes results/sprint/S2/lossaudit/candidates_frozen.json
once (exclusive create; there is no --force): UTC time, prompt sha, input sha256s, the funnel at
every stage and check (marginal and cumulative), the usable list, and `yield_summary`, which is
descriptive, not a pass/fail gate (Jacob, 24 Oct). Counts are given twice, confirmed and upper
(see Pool), each overall and for dropped and misstated items separately, with unique tasks, at
every check too. Sizing uses dropped usable losses only (Experiment 3's first run repairs
dropped items; misstated ones are reported, not repaired): the headline is q, the share of
audited boundaries with at least one (the denominator is `lossaudit.boundaries()`, zeros
listed), per configuration (subject x policy) and overall, with a Wilson 95% interval, and the
implied Experiment 3 base runs 25/q (range 20/q-30/q, and 25 over the interval's ends). r, losses
per boundary, is descriptive only. Two reference lines (">= 10 across >= 5 tasks", ">= 10 across
>= 4 tasks") are shown for information only. `decision` is null: Jacob decides after seeing it.

    uv run python scripts/s2_candidates.py pool [--losses PATH]
    uv run python scripts/s2_candidates.py draft --max [--concurrency 8] [--only CAND_IDS]
    uv run python scripts/s2_candidates.py page [--out data/raw/sprint/s2/candidates_page]
        [--exclude-draft-no] [--seed 20261024]
    uv run python scripts/s2_candidates.py freeze --labels labels.json
        [--key data/raw/sprint/s2/candidates_page/key.private.json] [--allow-incomplete]
        [--denominator-from-rows]
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s1_label_page as S1P  # its template helpers and writers
import s2_retention_page as RP  # the S2 page structure this page copies

from compaction import s1_types as T
from compaction.s1_judge import DEDUPE_RATIO, load_prompt
from compaction.s2 import config as C

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results" / "sprint" / "S2" / "lossaudit"
LOSSES_PATH = RESULTS / "losses.jsonl"
POOL_PATH = RESULTS / "candidates_pool.json"
DRAFTS_PATH = RESULTS / "candidate_drafts.jsonl"
FROZEN_PATH = RESULTS / "candidates_frozen.json"
PAGE_DIR = ROOT / "data" / "raw" / "sprint" / "s2" / "candidates_page"
PRIVATE_ROOT = ROOT / "data" / "raw"
CACHE_DIR = C.raw_dir() / "cache" / "candidates-haiku-max"
PROMPT_DIR = ROOT / "src" / "compaction" / "s2" / "prompts"
PROMPT_NAME = "candidate"
PURPOSE = "s2_candidates"
HAIKU_EFFORT = "medium"  # as S1's Max judge (scripts/s1_run.py)
SEED = 20261024
TYPES = ("task_state", "decision", "commitment", "permission")
TOOL_CAP = 2_000  # characters kept of each tool output in a request (head and tail)
MAX_OUTPUT_TOKENS = 16_000
MAX_TRIES = 3  # requests per candidate, first one included
BATCH = 16  # requests per backend.run; drafts are written after each
CONTROL_RATIO = (0.5, 2.0)
MAX_PAGE_BYTES = 15_000_000
MAX_EVIDENCE = 20  # recoverability lines and commands shown per card (the counts are shown too)
CHECKS = ("retention", "applicability", "not_recoverable", "governed_decision",
          "frozen_design")
ANSWERS = ("yes", "no", "unclear")
CARD_FIELDS = {"id", "kind", "hist_ref", "hist_at", "located", "ctx_quote", "post_ref",
               "context", "item", "grade", "summary_quote", "recover", "draft"}
KEY_MARKERS = ('"cand_id"', '"run_key"', '"cohort_key"')
COLLECTIONS = {"labels": "labels", "meta": "meta"}
PAGE_NAME = "page.html"
KEY_NAME = "key.private.json"
REFERENCE_LINES = ((10, 5), (10, 4))  # (usable losses, tasks): for information only
EXP3_BOUNDARIES = (20, 25, 30)  # Experiment 3 boundaries with a loss: low, point, high
_SLOT = re.compile(r"\{(history|post_input|run|item)\}")

ViewFn = Callable[[str], dict]


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha_file(p: Path) -> str | None:
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def default_view(boundary_dir: str) -> dict:
    """`compaction.s2.lossaudit.boundary_view` (unit 1), imported only when used."""
    from compaction.s2 import lossaudit

    p = Path(boundary_dir)
    return lossaudit.boundary_view(p if p.is_absolute() else ROOT / p)  # rows hold repo paths


class Views:
    """boundary_view results, read once per boundary dir."""

    def __init__(self, fn: ViewFn | None = None):
        self.fn = fn or default_view
        self._c: dict[str, dict] = {}

    def __call__(self, boundary_dir: str) -> dict:
        k = str(boundary_dir)
        if k not in self._c:
            self._c[k] = self.fn(k)
        return self._c[k]


# ------------------------------------------------------------------------------------------
# Pool
# ------------------------------------------------------------------------------------------


def load_losses(path: Path = LOSSES_PATH) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def cand_id(row: Mapping) -> str:
    return f"{row['run_key']}:{row['item_id']}"


def _msg_key(row: Mapping) -> tuple:
    m = row.get("msg")
    return (m if isinstance(m, int) else 10**9, str(row.get("item_id")))


def _same(a: Mapping, b: Mapping) -> bool:
    for f in ("quote", "text"):
        x, y = T.norm(str(a.get(f) or "")).lower(), T.norm(str(b.get(f) or "")).lower()
        if x and y and difflib.SequenceMatcher(None, x, y).ratio() >= DEDUPE_RATIO:
            return True
    return False


def pool(rows: list[dict]) -> dict:
    """{"candidates", "excluded", "funnel"}: the filters in order (module docstring)."""
    funnel: dict[str, int] = {"items": len(rows)}
    excluded: list[dict] = []

    def keep(xs, stage, ok, why):
        out = []
        for r in xs:
            if ok(r):
                out.append(r)
            else:
                excluded.append({"cand_id": cand_id(r), "stage": stage, "reason": why(r)})
        funnel[stage] = len(out)
        return out

    xs = [r for r in rows if r.get("lost") is True]
    funnel["lost"] = len(xs)  # not-lost items are not logged one by one
    xs = keep(xs, "not_planted", lambda r: not r.get("planted_rule"),
              lambda r: f"planted rule {r.get('planted_rule')}")
    xs = keep(xs, "frozen_type", lambda r: r.get("type") in TYPES,
              lambda r: f"type {r.get('type')}")
    by: dict[str, list[dict]] = defaultdict(list)
    for r in xs:
        by[str(r["run_key"])].append(r)
    deduped = []
    for rk in sorted(by):
        kept: list[dict] = []
        for r in sorted(by[rk], key=_msg_key):
            dup = next((k for k in kept if _same(k, r)), None)
            if dup is None:
                kept.append(r)
            else:
                excluded.append({"cand_id": cand_id(r), "stage": "deduplicated",
                                 "reason": f"equivalent to {cand_id(dup)}"})
        deduped += kept
    funnel["deduplicated"] = len(deduped)
    for stage, rs in (("lost", [r for r in rows if r.get("lost") is True]),
                      ("deduplicated", deduped)):
        funnel[f"{stage}_by_grade"] = dict(sorted(Counter(str(r.get("grade")) for r in rs)
                                                  .items()))
    xs = keep(deduped, "not_recoverable_auto", lambda r: r.get("recoverable") != "recoverable",
              lambda r: f"recoverable: {r.get('recoverable_how')}")
    funnel["boundaries"] = len({str(r.get("run_key")) for r in rows})
    funnel["tasks"] = len({str(r.get("task")) for r in rows})
    cands = [{"cand_id": cand_id(r), **r} for r in xs]
    funnel["to_page_by_recoverable"] = dict(sorted(Counter(
        str(c.get("recoverable")) for c in cands).items()))
    seen = {str(r.get("run_key")): {"task": str(r.get("task")), "subject": r.get("subject"),
                                    "policy": r.get("policy")} for r in rows}
    return {"candidates": cands, "excluded": excluded, "funnel": funnel,
            "boundaries": dict(sorted(seen.items()))}


# ------------------------------------------------------------------------------------------
# Draft request
# ------------------------------------------------------------------------------------------


def prompt():
    return load_prompt(PROMPT_NAME, PROMPT_DIR)


def _fill(tpl: str, **slots: str) -> str:
    missing = {m.group(1) for m in _SLOT.finditer(tpl)} - set(slots)
    if missing:
        raise KeyError(f"{PROMPT_NAME}: no value for {sorted(missing)}")
    return _SLOT.sub(lambda m: slots[m.group(1)], tpl)


def cut(text: str, cap: int = TOOL_CAP) -> str:
    text = text or ""
    if len(text) <= cap:
        return text
    h = cap // 2
    return f"{text[:h]}\n[... {len(text) - cap} characters cut ...]\n{text[-h:]}"


def render_history(view: Mapping, cap: int | None = TOOL_CAP) -> str:
    out = []
    for m in view.get("history") or []:
        t = m.get("text") or ""
        if cap is not None and m.get("role") == "tool":
            t = cut(t, cap)
        out.append(f"[message {m['i']}: {m.get('role')}]\n{t}")
    return "\n\n".join(out)


def render_post(view: Mapping, cap: int | None = TOOL_CAP) -> str:
    out = []
    for m in view.get("post_messages") or []:
        t = m.get("text") or ""
        if cap is not None and m.get("role") == "tool":
            t = cut(t, cap)
        out.append(f"[message {m['i']}: {m.get('role')}, {m.get('kind') or 'kept'}]\n{t}")
    return "\n\n".join(out)


def item_message(row: Mapping, view: Mapping) -> int | None:
    """The history message holding the item's quote: the row's own `msg` if the quote is
    there, else the first message that has it; None when no message does."""
    hist = view.get("history") or []
    q = row.get("quote")
    m = row.get("msg")
    if isinstance(m, int):
        own = next((x for x in hist if x["i"] == m), None)
        if own is not None and T.quote_in(q, own.get("text") or ""):
            return m
    return next((x["i"] for x in hist if T.quote_in(q, x.get("text") or "")), None)


def run_block(row: Mapping, view: Mapping) -> str:
    si = next((m["i"] for m in view.get("post_messages") or [] if m.get("kind") == "summary"),
              None)
    return (f"Task {row.get('task')}; agent model {row.get('subject')} with the "
            f"{row.get('policy')} summarizer; the task's rules were given in the "
            f"{row.get('channel')} channel. The summary is message "
            f"{si if si is not None else '(not marked)'} of the post-compaction input. Tool "
            f"outputs longer than {TOOL_CAP} characters are cut to their head and tail.")


def item_block(row: Mapping, view: Mapping) -> str:
    mi = item_message(row, view)
    at = (f"message {mi}" if mi is not None
          else f"message {row.get('msg')} (the quote was not found verbatim)")
    lines = [f"Type: {row.get('type')}", f"Stated at: {at}",
             f"Item: {row.get('text')}", f"Quote: {row.get('quote')}"]
    if row.get("grade") == "misstated" and row.get("summary_quote"):
        lines.append(f"The summary misstates it as: {row.get('summary_quote')}")
    return "\n".join(lines)


_S, _OBJ = T._s, T._obj
REPLY_SCHEMA = _OBJ({
    "applies": _S(ANSWERS),
    "applies_reason": _S(),
    "superseded_quote": _S(),
    "governed_decision": _S(),
    "decision_depends": _S(ANSWERS),
    "decision_quote": _S(),
    "observable_act": _S(),
    "scorer_kind": _S(("code", "blind_human")),
    "scorer": _S(),
    "repair_text": _S(),
    "control_text": _S(),
    "note": _S(),
})


def build_request(row: Mapping, view: Mapping, *, key: str | None = None) -> T.Req:
    p = prompt()
    user = _fill(p.sections["user"], history=render_history(view), post_input=render_post(view),
                 run=run_block(row, view), item=item_block(row, view))
    return T.Req(key=key or cand_id(row), system=p.system, user=user, schema=REPLY_SCHEMA,
                 max_output_tokens=MAX_OUTPUT_TOKENS, rid=f"deepswe:{row['run_key']}:0",
                 source="deepswe", private=False)  # DeepSWE runs are public data


def request_sha(req: T.Req, sha: str) -> str:
    """The prompt file and the request text: what a draft was made from (the page refuses a
    draft whose request no longer matches)."""
    return hashlib.sha256(json.dumps([sha, req.system, req.user]).encode()).hexdigest()[:16]


def fingerprint(req: T.Req, backend, sha: str) -> str:
    blob = json.dumps([request_sha(req, sha), getattr(backend, "model", "?"),
                       getattr(backend, "effort", None)])
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------
# Reply checks
# ------------------------------------------------------------------------------------------


def _parse(data: Any) -> dict | None:
    if isinstance(data, dict):
        return data
    if not isinstance(data, str) or "{" not in data:
        return None
    try:
        obj = json.loads(data[data.find("{"): data.rfind("}") + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def check_reply(data: Any, view: Mapping) -> dict:
    """The validated draft: each part with `valid`, plus `flags`; never raises."""
    d = _parse(data)
    flags: list[str] = []
    out = {"applicability": {"valid": False}, "governed_decision": {"valid": False},
           "design": {"valid": False}, "note": "", "flags": flags}
    if d is None:
        flags.append("no_json_object")
        return out
    hist = "\n".join((m.get("text") or "") for m in view.get("history") or [])

    def s(k):
        v = d.get(k)
        return v.strip() if isinstance(v, str) else ""

    def enum(k, vals):
        if d.get(k) in vals:
            return d[k]
        flags.append(f"bad_enum:{k}")
        return None

    def quote(k):
        q = s(k)
        if q and not T.quote_in(q, hist):
            flags.append(f"quote_not_found:{k}")
            return q, False
        return q, True

    ap = enum("applies", ANSWERS)
    sq, sq_ok = quote("superseded_quote")
    out["applicability"] = {"answer": ap, "reason": s("applies_reason"), "superseded_quote": sq,
                            "valid": ap is not None and sq_ok and bool(s("applies_reason"))}
    dd = enum("decision_depends", ANSWERS)
    dq, dq_ok = quote("decision_quote")
    gd, act = s("governed_decision"), s("observable_act")
    # a "no" on check 2 or 4 needs no test design (the prompt allows empty design fields)
    needed = not (ap == "no" or dd == "no")
    for k, v in (("governed_decision", gd), ("observable_act", act)):
        if not v and (needed or k == "governed_decision"):
            flags.append(f"empty:{k}")
    out["governed_decision"] = {"answer": dd, "decision": gd, "decision_quote": dq,
                                "observable_act": act,
                                "valid": dd is not None and dq_ok and bool(gd)
                                and (bool(act) or not needed)}
    sk = d.get("scorer_kind") if d.get("scorer_kind") in ("code", "blind_human") else None
    sc, rep, ctl = s("scorer"), s("repair_text"), s("control_text")
    ratio = len(ctl) / len(rep) if rep and ctl else None
    ratio_ok = ratio is not None and CONTROL_RATIO[0] <= ratio <= CONTROL_RATIO[1]
    if needed:
        if sk is None:
            flags.append("bad_enum:scorer_kind")
        for k, v in (("scorer", sc), ("repair_text", rep), ("control_text", ctl)):
            if not v:
                flags.append(f"empty:{k}")
        if ratio is not None and not ratio_ok:
            flags.append(f"control_length_ratio:{ratio:.2f}")
    out["design"] = {"scorer_kind": sk, "scorer": sc, "repair_text": rep, "control_text": ctl,
                     "control_ratio": round(ratio, 3) if ratio is not None else None,
                     "needed": needed,
                     "valid": (sk is not None and bool(sc and rep and ctl) and ratio_ok)
                     or not needed}
    out["note"] = s("note")
    return out


PARTS = ("applicability", "governed_decision", "design")


def draft_ok(draft: Mapping | None) -> bool:
    return bool(draft) and all((draft.get(k) or {}).get("valid") for k in PARTS)


def draft_score(row: Mapping | None) -> int:
    """-1 for no draft (an error), else the number of valid parts: keep-best compares this,
    and keeps one whole attempt (parts of different attempts are never combined)."""
    d = (row or {}).get("draft")
    return -1 if not d else sum(bool((d.get(k) or {}).get("valid")) for k in PARTS)


# ------------------------------------------------------------------------------------------
# Draft pass
# ------------------------------------------------------------------------------------------


def load_drafts(path: Path = DRAFTS_PATH) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    out = {}
    for x in p.read_text().splitlines():
        if x.strip():
            r = json.loads(x)
            out[r["cand_id"]] = r
    return out


def _write_drafts(path: Path, drafts: Mapping[str, dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(drafts[k], sort_keys=True) + "\n" for k in sorted(drafts)))
    os.replace(tmp, p)


def draft_all(backend, cands: list[dict], *, views: Views, out_path: Path = DRAFTS_PATH,
              batch: int = BATCH, max_tries: int = MAX_TRIES) -> dict[str, dict]:
    """Draft every candidate without a valid draft for the current request (resumable)."""
    p = prompt()
    drafts = load_drafts(out_path)
    todo = []
    for c in cands:
        view = views(c["boundary_dir"])
        req = build_request(c, view)
        fp = fingerprint(req, backend, p.sha256)
        old = drafts.get(c["cand_id"])
        same = bool(old) and old.get("fingerprint") == fp
        if same and old.get("status") == "ok":
            continue
        tries = int(old.get("tries") or 0) if same else 0
        if tries >= max_tries:
            continue
        todo.append((c, view, fp, tries, request_sha(req, p.sha256)))
    while todo:
        nxt = []
        for i in range(0, len(todo), max(1, batch)):
            chunk = todo[i:i + batch]
            reqs = []
            for c, view, _fp, tries, _rs in chunk:
                key = c["cand_id"] if tries == 0 else f"{c['cand_id']}#retry{tries}"
                reqs.append(build_request(c, view, key=key))
            resps = backend.run(reqs, purpose=PURPOSE)
            for (c, view, fp, tries, rs), q in zip(chunk, reqs, strict=True):
                resp = resps.get(q.key)
                row = {"cand_id": c["cand_id"], "run_key": c["run_key"],
                       "item_id": c["item_id"], "prompt_sha256": p.sha256, "fingerprint": fp,
                       "request_sha": rs,
                       "request_key": q.key, "tries": tries + 1, "utc": _utc(),
                       "judge": getattr(backend, "name", "?"),
                       "model": getattr(resp, "model", None) or getattr(backend, "model", "?"),
                       "effort": getattr(backend, "effort", None)}
                if resp is None or resp.error is not None:
                    row |= {"status": "error",
                            "error": "missing_response" if resp is None else resp.error,
                            "draft": None}
                else:
                    dr = check_reply(resp.data if resp.data is not None else resp.text, view)
                    row |= {"status": "ok" if draft_ok(dr) else "invalid", "error": None,
                            "draft": dr, "reply": _parse(resp.data if resp.data is not None
                                                         else resp.text),
                            "input_tokens": resp.input_tokens,
                            "output_tokens": resp.output_tokens,
                            "cache_read_tokens": resp.cache_read_tokens,
                            "duration_s": round(resp.duration_s, 1)}
                old = drafts.get(c["cand_id"])
                attempt = {"request_key": q.key, "status": row["status"],
                           "score": draft_score(row), "error": row["error"]}
                hist = (old.get("attempts") or []) if old and old.get("fingerprint") == fp else []
                # keep-best: a later attempt replaces the kept one only when it scores higher
                if old and old.get("fingerprint") == fp and draft_score(old) >= draft_score(row):
                    drafts[c["cand_id"]] = old | {"tries": tries + 1}
                else:
                    drafts[c["cand_id"]] = row
                drafts[c["cand_id"]]["attempts"] = [*hist, attempt]
                if row["status"] != "ok" and tries + 1 < max_tries:
                    nxt.append((c, view, fp, tries + 1, rs))
            _write_drafts(out_path, drafts)
        todo = nxt
    return drafts


# ------------------------------------------------------------------------------------------
# Page
# ------------------------------------------------------------------------------------------


def card_id(seed: int, cid: str) -> str:
    return "c" + hashlib.sha256(f"s2-candidate-card:{seed}:{cid}".encode()).hexdigest()[:12]


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def locate(text: str, quote: str | None, near: int | None = None) -> list[int] | None:
    """[start, end) of the quote in `text`, whitespace-insensitive, as JavaScript string
    offsets (UTF-16 units); the match nearest `near` (a code-point offset) when several."""
    toks = (quote or "").split()
    if len(T.norm(quote or "")) < T.QUOTE_MIN_CHARS:
        return None
    rx = re.compile(r"\s+".join(re.escape(t) for t in toks))
    ms = list(rx.finditer(text))
    if not ms:
        return None
    m = min(ms, key=lambda m: abs(m.start() - near)) if near is not None else ms[0]
    return [_u16(text[: m.start()]), _u16(text[: m.end()])]


def page_cands(cands: list[dict], drafts: Mapping[str, dict], *,
               exclude_draft_no: bool) -> tuple[list[dict], list[dict]]:
    """(shown, excluded): with `exclude_draft_no`, a valid draft answering "no" on check 2 or
    4 drops the candidate."""
    shown, excl = [], []
    for c in cands:
        d = (drafts.get(c["cand_id"]) or {}).get("draft") or {}
        no = [k for k in ("applicability", "governed_decision")
              if (d.get(k) or {}).get("valid") and (d.get(k) or {}).get("answer") == "no"]
        if exclude_draft_no and no:
            excl.append({"cand_id": c["cand_id"], "stage": "draft_no",
                         "reason": f"Haiku draft says no on {', '.join(no)}"})
        else:
            shown.append(c)
    return shown, excl


class StaleDrafts(ValueError):
    pass


def stale_drafts(cands: Iterable[dict], drafts: Mapping[str, dict], views: Views) -> list[str]:
    """Candidates whose saved draft was made from another prompt or request text."""
    sha = prompt().sha256
    out = []
    for c in cands:
        d = drafts.get(c["cand_id"])
        if d and d.get("request_sha") != request_sha(
                build_request(c, views(c["boundary_dir"])), sha):
            out.append(c["cand_id"])
    return out


def _sha_obj(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()
                          ).hexdigest()


def build(cands: list[dict], drafts: Mapping[str, dict], views: Views, *, seed: int = SEED,
          funnel: Mapping | None = None, exclude_draft_no: bool = False,
          excluded: list[dict] | None = None) -> tuple[str, dict]:
    """(html, key) for the candidates in `cands` (already filtered by `page_cands`).
    Deterministic, so the freeze can rebuild it and require the same page. The draw id hashes
    every card's content (its shown fields, the candidate row, both boundary texts and the
    prompt), so labels saved under one page never attach to changed evidence. Refuses stale
    drafts (StaleDrafts)."""
    stale = stale_drafts(cands, drafts, views)
    if stale:
        raise StaleDrafts(f"{len(stale)} drafts no longer match their request (rerun draft): "
                          f"{stale[:5]}")
    rng = random.Random(f"s2-candidate-page:{seed}")
    order = sorted(cands, key=lambda c: c["cand_id"])
    rng.shuffle(order)
    hists: dict[str, str] = {}
    refs: dict[str, tuple[str, str]] = {}
    shown, key = [], {}
    for c in order:
        view = views(c["boundary_dir"])
        rk = str(c["run_key"])
        if rk not in refs:
            n = len(refs) + 1
            refs[rk] = (f"h{n}", f"p{n}")
            hists[f"h{n}"] = RP._gz64(render_history(view, cap=None))
            hists[f"p{n}"] = RP._gz64(render_post(view, cap=None))
        htext = render_history(view, cap=None)
        mi = item_message(c, view)
        near = None
        if mi is not None:
            tag = f"[message {mi}: "
            near = htext.find(tag) if tag in htext else None
        at = locate(htext, c.get("quote"), near)
        cid = card_id(seed, c["cand_id"])
        dr = (drafts.get(c["cand_id"]) or {}).get("draft")
        shown.append({
            "id": cid, "kind": "item", "hist_ref": refs[rk][0], "hist_at": at,
            "located": at is not None, "ctx_quote": c.get("quote") or "",
            "post_ref": refs[rk][1],
            "context": f"task {c.get('task')} · model {c.get('subject')} · summarizer "
                       f"{c.get('policy')} · rules channel {c.get('channel')}",
            "item": {"type": c.get("type"), "text": c.get("text"), "quote": c.get("quote"),
                     "msg": mi if mi is not None else c.get("msg")},
            "grade": c.get("grade"),
            "summary_quote": c.get("summary_quote") if c.get("grade") == "misstated" else None,
            "recover": {"recoverable": c.get("recoverable"), "how": c.get("recoverable_how"),
                        "lines": list(c.get("recoverable_lines") or [])[:MAX_EVIDENCE],
                        "commands": list(c.get("recoverable_commands") or [])[:MAX_EVIDENCE],
                        "n_lines": len(c.get("recoverable_lines") or []),
                        "n_commands": len(c.get("recoverable_commands") or []),
                        "availability": c.get("availability"),
                        "availability_how": c.get("availability_how")},
            "draft": dr,
        })
        content = _sha_obj([shown[-1], c, render_history(view, cap=None),
                            render_post(view, cap=None), prompt().sha256])
        key[cid] = {"cand_id": c["cand_id"], "run_key": rk, "item_id": c["item_id"],
                    "task": c.get("task"), "subject": c.get("subject"),
                    "cohort_key": c.get("cohort_key"), "content_sha256": content,
                    "draft_request_sha": (drafts.get(c["cand_id"]) or {}).get("request_sha")}
    draw = _sha_obj(sorted((k, v["content_sha256"]) for k, v in key.items()))[:16]
    data = {"cards": shown, "histories": hists, "collections": COLLECTIONS, "draw": draw,
            "seed": seed, "checks": list(CHECKS)}
    html = render(data)
    return html, {"draw": draw, "seed": seed, "cards": key, "order": [s["id"] for s in shown],
                  "exclude_draft_no": exclude_draft_no, "funnel": dict(funnel or {}),
                  "excluded_on_page": list(excluded or []),
                  "prompt_sha256": prompt().sha256,
                  "page_sha256": hashlib.sha256(html.encode()).hexdigest()}


HEADING = "S2 close-out: usable-loss candidates"
LEDE = ("Each card is an item Experiment 1's judge called lost at a live compaction boundary. "
        "Answer the five checks; a candidate is usable only with yes on all five, and unclear "
        "on any one excludes it. Haiku's drafts are suggestions, checked by code for quotes "
        "and lengths only. Usable candidates are frozen before any Experiment 3 continuation.")

CHECK_TEXT = [
    ["Retention", ("Is the item absent from the post-compaction input, or materially misstated "
                   "there (a scope, status or value differs from the source quote)?")],
    ["Applicability", ("Does it still bear on remaining work at the boundary? Completed-work "
                       "facts count when the agent could redo or contradict them.")],
    ["Not recoverable", ("Does no file in the boundary's repo and no part of the task "
                         "description state it? The repo check's matching lines and the "
                         "agent's commands that touched those files are shown below.")],
    ["Governed decision", ("Can a later decision it bears on be named from the task and the "
                           "boundary's state, with an observable act that satisfies or "
                           "contradicts it?")],
    ["Frozen design", ("Are the repair text, the control, the act and the scorer fair and "
                       "frozen as drafted (edit in the note if not), with a continuation that "
                       "never reaches the decision counted as not performing the act?")],
]

BODY = """
<div class="wrap">
  <header style="display:grid;gap:8px">
    <h1>__HEADING__</h1>
    <p class="lede">__LEDE__</p>
  </header>
  <div class="bar" role="status">
    <span class="count" id="count">0 done</span>
    <div class="track" aria-hidden="true"><i id="fill"></i></div>
    <button type="button" class="ghost" id="next">Next unfinished</button>
    <span class="save" id="save">Connecting to storage…</span>
  </div>
  <section class="panel"><div class="sheets" id="sheets"></div></section>
  <section class="export">
    <p class="label">Copy of your work</p>
    <p class="lede">Everything saves to this page's storage, which Claude reads. If saving fails, copy this and paste it to Claude.</p>
    <textarea id="dump" readonly aria-label="Labels as JSON"></textarea>
    <button type="button" class="act" id="copy">Copy</button>
  </section>
</div>
<script>
const DATA = __DATA_JSON__;
const CHECK_TEXT = __CHECKS_JSON__;
const COLL = DATA.collections;
const BYID = {};
for (const x of DATA.cards) {
  BYID[x.id] = { tab: "labels", x };
  BYID[x.id + "-post"] = { tab: "boxes", x: { id: x.id + "-post", kind: "boundary", hist_ref: x.post_ref, box_label: "Post-compaction input: everything the model saw after the boundary", find_ph: "Find in the post-compaction input" } };
}
const state = {};
let db = null;
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
__HISTORY_JS__
const ANS = [["yes", "Yes", "ok"], ["no", "No", "bad"], ["unclear", "Unclear", "none"]];
function done(id) { const l = state[id] || {}; return CHECK_TEXT.every((_, k) => l[`c${k + 1}`]); }
function warn(d, part) { return d && d[part] && !d[part].valid ? `<p class="warnline">This draft failed a code check (${esc((d.flags || []).join(", "))}).</p>` : ""; }
function draftHtml(u, k) {
  const d = u.draft;
  if (!d) return k === 0 || k === 2 ? "" : `<p class="lede">No Haiku draft.</p>`;
  if (k === 1) { const a = d.applicability || {}; return `${warn(d, "applicability")}<p class="lede">Haiku: <strong>${esc(a.answer)}</strong>. ${esc(a.reason)}</p>${a.superseded_quote ? `<p class="q">${esc(a.superseded_quote)}</p>` : ""}`; }
  if (k === 3) { const g = d.governed_decision || {}; return `${warn(d, "governed_decision")}<p class="lede">Haiku: <strong>${esc(g.answer)}</strong>. Decision: ${esc(g.decision)}</p>${g.decision_quote ? `<p class="q">${esc(g.decision_quote)}</p>` : ""}<p class="lede">Observable act: ${esc(g.observable_act)}</p>`; }
  if (k === 4) { const s = d.design || {}; return `${warn(d, "design")}<p class="lede">Haiku's scorer kind: ${esc(s.scorer_kind)}${d.note ? `. Haiku's note: ${esc(d.note)}` : ""}</p>`; }
  return "";
}
// The frozen design: Haiku's draft as the starting value, edited here; the freeze uses these.
const DESIGN = [["d_repair", "Repair text (put back into the summary)", "textarea"], ["d_control", "Control text (same length, irrelevant)", "textarea"], ["d_act", "Observable act", "input"], ["d_scorer", "Scorer or blind rubric", "input"]];
function defaults(u) {
  const d = u.draft || {}, s = d.design || {}, g = d.governed_decision || {};
  return { d_repair: s.repair_text || "", d_control: s.control_text || "", d_act: g.observable_act || "", d_scorer: s.scorer || "" };
}
const DEF = {};
for (const x of DATA.cards) DEF[x.id] = defaults(x);
function designHtml(u) {
  return DESIGN.map(([f, label, tag]) => `<p class="label">${esc(label)}</p>` + (tag === "textarea"
    ? `<textarea class="note" rows="2" data-id="${u.id}" data-f="${f}" aria-label="${esc(label)}"></textarea>`
    : `<input class="note" type="text" data-id="${u.id}" data-f="${f}" aria-label="${esc(label)}">`)).join("")
    + `<p class="lede">Fixed rule: a continuation that never reaches the decision counts as not performing the act.</p>`;
}
function evidence(u, k) {
  if (k === 0) return `<p class="lede">Search the post-compaction input below.</p><p class="lede" id="g-${u.id}" hidden>Judge's grade: <strong>${esc(u.grade)}</strong>${u.summary_quote ? `; the summary says: <span class="q">${esc(u.summary_quote)}</span>` : ""}.</p>`;
  if (k === 2) {
    const r = u.recover || {};
    const lines = (r.lines || []).map(x => `<li><code>${esc(x.path)}:${esc(x.line)}</code> ${esc(x.text)}</li>`).join("");
    const cmds = (r.commands || []).map(x => `<li>message ${esc(x.msg)}: <code>${esc(x.command)}</code></li>`).join("");
    return `<p class="lede">Repo check: <strong>${esc(r.recoverable)}</strong> (${esc(r.how)}). Availability: ${esc(r.availability)} (${esc(r.availability_how)}).</p>`
      + (lines ? `<p class="label">Matching lines in the boundary's repo (${esc(r.n_lines)})</p><ul>${lines}</ul>` : "")
      + (cmds ? `<p class="label">The agent's commands that touched those files (${esc(r.n_commands)})</p><ul>${cmds}</ul>` : "");
  }
  return draftHtml(u, k);
}
function card(u, i, n) {
  const checks = CHECK_TEXT.map(([name, q], k) => {
    const radios = ANS.map(([v, t, tone]) => `<label><input type="radio" name="c${k + 1}-${u.id}" value="${v}" data-id="${u.id}" data-f="c${k + 1}" data-tone="${tone}"><span>${t}</span></label>`).join("");
    return `<div class="calls"><fieldset><legend>${k + 1}. ${esc(name)}: ${esc(q)}</legend>${radios}</fieldset>${evidence(u, k)}${k === 4 ? designHtml(u) : ""}
      <input class="note" type="text" data-id="${u.id}" data-f="n${k + 1}" placeholder="Note on check ${k + 1} (optional)" aria-label="Note on check ${k + 1}"></div>`;
  }).join("");
  return `<article class="sheet" id="s-${u.id}">
    <header><span class="tag">${i + 1} / ${n} · ${esc(u.item.type)}</span><span class="state" id="st-${u.id}">not done</span></header>
    <p class="lede">${esc(u.context)}</p>
    <div><p class="label">Item (message ${esc(u.item.msg)})</p><p class="q">${esc(u.item.text)}</p></div>
    <div><p class="label">Source quote</p><p class="q">${esc(u.item.quote)}</p></div>
    ${histBox(u)}
    ${histBox(BYID[u.id + "-post"].x)}
    ${checks}
    <input class="note" type="text" data-id="${u.id}" data-f="note" placeholder="Note on the candidate (optional)" aria-label="Note">
  </article>`;
}
function paint(id) {
  const l = state[id] || {};
  document.querySelectorAll(`[data-id="${id}"]`).forEach(el => {
    const f = el.dataset.f;
    if (el.type === "radio") el.checked = el.value === l[f];
    else if (document.activeElement !== el) el.value = f in l ? l[f] : (f.startsWith("d_") ? DEF[id][f] : "");
  });
  const g = document.getElementById(`g-${id}`);
  if (g) g.hidden = !l.c1;  // the judge's grade shows only after check 1 is answered
  document.getElementById(`s-${id}`).classList.toggle("done", done(id));
  document.getElementById(`st-${id}`).textContent = done(id) ? "done" : "not done";
}
function tally() {
  const n = DATA.cards.filter(x => done(x.id)).length;
  document.getElementById("count").textContent = `${n} / ${DATA.cards.length} done`;
  document.getElementById("fill").style.width = `${100 * n / Math.max(1, DATA.cards.length)}%`;
  const rows = DATA.cards.filter(x => state[x.id]).map(x => ({ id: x.id, collection: COLL.labels, data: { ...state[x.id], draw: DATA.draw } }));
  document.getElementById("dump").value = JSON.stringify(rows, null, 1);
}
function setSave(text, warn) { const el = document.getElementById("save"); el.textContent = text; el.classList.toggle("warn", !!warn); }
// The storage hook: labels = {card id: {c1..c5, n1..n5, note}}. Saves each to the db
// (collection "labels", doc id = card id); without storage, the copy below is the record.
// rev[id] counts local edits; saved[id] is the last revision written. A snapshot from the db
// never replaces a card with an edit not yet written, whatever has the focus.
const rev = {}, saved = {};
const dirty = id => (rev[id] || 0) !== (saved[id] || 0);
async function submit(labels) {
  for (const [id, l] of Object.entries(labels)) { state[id] = { ...(state[id] || {}), ...l }; paint(id); }
  tally();
  if (!db) { setSave("Not saving: storage unavailable. Use the copy below.", true); return false; }
  try {
    for (const id of Object.keys(labels)) {
      const r = rev[id] || 0;
      await db.collection(COLL.labels).doc(id).set({ ...state[id], draw: DATA.draw, utc: new Date().toISOString() });
      saved[id] = Math.max(saved[id] || 0, r);
    }
    setSave("Saved"); return true;
  } catch (e) { setSave(`Not saved (${e && e.code || "error"}). Use the copy below.`, true); return false; }
}
const timers = {};
function onEdit(ev, immediate) {
  const t = ev.target, id = t.dataset && t.dataset.id, f = t.dataset && t.dataset.f;
  if (!id || !f || !BYID[id] || BYID[id].tab !== "labels") return;
  rev[id] = (rev[id] || 0) + 1;
  state[id] = { ...(state[id] || {}), [f]: t.value }; paint(id); tally();
  clearTimeout(timers[id]);
  const go = () => submit({ [id]: state[id] });
  if (immediate) go(); else timers[id] = setTimeout(go, 1200);
}
document.addEventListener("change", ev => onEdit(ev, true));
document.addEventListener("input", ev => { if (ev.target.classList.contains("note")) onEdit(ev, false); });
document.getElementById("next").addEventListener("click", () => {
  const x = DATA.cards.find(x => !done(x.id));
  if (x) document.getElementById(`s-${x.id}`).scrollIntoView({ behavior: "smooth", block: "start" });
});
document.getElementById("copy").addEventListener("click", async () => {
  const ta = document.getElementById("dump");
  try { await navigator.clipboard.writeText(ta.value); setSave("Copied"); }
  catch (e) { ta.select(); setSave("Selected: press copy", true); }
});
document.getElementById("sheets").innerHTML = DATA.cards.map((u, i) => card(u, i, DATA.cards.length)).join("");
DATA.cards.forEach(x => paint(x.id)); tally();
requestAnimationFrame(() => hsObserve());
(async () => {
  try { db = window.claude ? await window.claude.use("db") : null; } catch (e) { db = null; }
  if (!db) { setSave("Not saving: storage unavailable here. Use the copy below.", true); return; }
  setSave("Saving on");
  try { await db.collection(COLL.meta).doc("draw").set({ draw: DATA.draw, seed: DATA.seed, utc: new Date().toISOString() }); } catch (e) { /* labels still carry the draw */ }
  db.collection(COLL.labels).onSnapshot(snap => {
    snap.docs.forEach(d => {
      const v = d.data();
      if (!BYID[d.id] || BYID[d.id].tab !== "labels" || v.draw !== DATA.draw || dirty(d.id)) return;
      const s = {};
      for (const [k, x] of Object.entries(v)) if (k !== "draw" && k !== "utc") s[k] = x;
      state[d.id] = s; paint(d.id);
    });
    tally();
  }, e => setSave(`Live sync stopped (${e && e.code || "error"}). Edits still save.`, true));
})();
</script>
"""


def history_js() -> str:
    """The template's history box, with a per-box label and find placeholder (the second box
    on each card shows the post-compaction input)."""
    tpl = S1P.TEMPLATE.read_text(encoding="utf-8")
    js = tpl[tpl.index(RP.HIST_START): tpl.index(RP.HIST_END)]
    for old, new in (
        ('placeholder="Find in the history" aria-label="Find in the history"',
         ('placeholder="${u.find_ph || "Find in the history"}" '
          'aria-label="${u.find_ph || "Find in the history"}"')),
        ('<p class="label">${label}</p>', '<p class="label">${u.box_label || label}</p>'),
    ):
        if js.count(old) != 1:
            raise ValueError(f"s1_label_template.html changed: {old!r} found {js.count(old)}x")
        js = js.replace(old, new)
    return js


def render(data: dict) -> str:
    head, _ = RP.template_parts()
    head = head.replace("__TITLE__", "Usable-loss candidates")
    body = (BODY.replace("__HEADING__", HEADING).replace("__LEDE__", LEDE)
            .replace("__HISTORY_JS__", history_js())
            .replace("__CHECKS_JSON__", S1P._script_json(CHECK_TEXT)))
    return head + body.replace("__DATA_JSON__", S1P._script_json(data))


def leak_check(html: str, key: dict) -> None:
    """Refuse a page carrying a key field name, a candidate id, or a card field outside
    CARD_FIELDS. (Nothing here is an answer key; the check keeps the page to what Jacob is
    meant to see.)"""
    hits = [m for m in KEY_MARKERS if m in html]
    hits += [v["cand_id"] for v in key["cards"].values() if v["cand_id"] in html]
    hits += [f for c in RP.page_data(html)["cards"] for f in c if f not in CARD_FIELDS]
    if hits:
        raise S1P.LeakError(f"page carries key strings: {hits[:10]}")


def write_page(out_dir: Path, html: str, key: dict, *, private_root: Path | None = None,
               check_ignored: bool = True) -> dict:
    out = RP.require_private(Path(out_dir), private_root or PRIVATE_ROOT)
    if check_ignored:
        RP.require_ignored(out)
    leak_check(html, key)
    size = len(html.encode("utf-8"))
    if size > MAX_PAGE_BYTES:
        raise ValueError(f"page is {size:,} bytes, above {MAX_PAGE_BYTES:,}")
    S1P._atomic(out / KEY_NAME, (json.dumps(key, indent=1, sort_keys=True) + "\n").encode(),
                mode=0o600)
    S1P._atomic(out / PAGE_NAME, html.encode("utf-8"))
    return {"page": str(out / PAGE_NAME), "key": str(out / KEY_NAME), "bytes": size,
            "cards": len(key["cards"])}


# ------------------------------------------------------------------------------------------
# Freeze
# ------------------------------------------------------------------------------------------


class FrozenExists(FileExistsError):
    pass


def read_labels(obj: Any) -> dict[str, dict]:
    from s1_label_score import normalize_labels

    return normalize_labels(obj).get("labels") or {}


def answers(labels: Mapping[str, dict], key: Mapping, *, allow_incomplete: bool
            ) -> dict[str, list[str]]:
    """{card id: [answer per check]}; refuses labels from another draw, unknown cards, bad
    answers, and (unless allow_incomplete) any missing answer."""
    draw = key["draw"]
    bad = [cid for cid, d in labels.items() if d.get("draw") != draw]
    if bad:
        raise ValueError(f"labels from another draw: {bad[:5]}")
    unknown = sorted(set(labels) - set(key["cards"]))
    if unknown:
        raise ValueError(f"labels for cards not on the page: {unknown[:5]}")
    out, missing = {}, []
    for cid in key["order"]:
        d = labels.get(cid) or {}
        row = []
        for k in range(1, len(CHECKS) + 1):
            a = d.get(f"c{k}") or None
            if a is not None and a not in ANSWERS:
                raise ValueError(f"{cid}: bad answer {a!r} on check {k}")
            if a is None:
                missing.append(f"{cid}:c{k}")
            row.append(a or "missing")
        out[cid] = row
    if missing and not allow_incomplete:
        raise ValueError(f"{len(missing)} checks unanswered (e.g. {missing[:5]}); finish the "
                         "page or pass --allow-incomplete")
    return out


def _wilson95(x: int, n: int) -> list[float] | None:
    from emx.stats import _wilson

    return [round(float(v), 4) for v in _wilson(x, n, 1.959964)] if n else None


def _bmeta(v: Any) -> dict:
    return v if isinstance(v, Mapping) else {"task": str(v), "subject": None, "policy": None}


def split_counts(xs: Iterable[Mapping]) -> dict:
    """Confirmed (repo check not_recoverable) and upper (also unresolved, or no call) counts,
    each overall and by grade, with unique tasks and boundaries."""
    xs = list(xs)
    out = {}
    for basis in ("confirmed", "upper"):
        ys = [x for x in xs if basis == "upper" or x.get("recoverable") == "not_recoverable"]
        out[basis] = {"total": len(ys),
                      "dropped": sum(x.get("grade") == "dropped" for x in ys),
                      "misstated": sum(x.get("grade") == "misstated" for x in ys),
                      "tasks": len({str(x.get("task")) for x in ys}),
                      "boundaries": len({str(x.get("run_key")) for x in ys})}
    return out


def base_runs(q: float | None, ci: list[float] | None) -> dict | None:
    """Experiment 3 base runs for 20-30 boundaries with a usable loss, from q."""
    if not q:
        return None
    lo, mid, hi = (b / q for b in EXP3_BOUNDARIES)
    at_ci = [round(EXP3_BOUNDARIES[1] / ci[1], 1) if ci and ci[1] else None,
             round(EXP3_BOUNDARIES[1] / ci[0], 1) if ci and ci[0] else None]
    return {"point": round(mid, 1), "range": [round(lo, 1), round(hi, 1)],
            "at_ci95_ends": at_ci}


def _sizing(keys: list[str], with_loss: Mapping[str, int]) -> dict:
    """q over `keys`: the share of boundaries with at least one repairable usable loss."""
    n = len(keys)
    x = sum(1 for k in keys if with_loss.get(k))
    q = x / n if n else None
    ci = _wilson95(x, n)
    return {"boundaries": n, "with_repairable": x, "q": round(q, 4) if q is not None else None,
            "q_ci95": ci, "exp3_base_runs": base_runs(q, ci)}


def yield_summary(usable: list[dict], boundaries: Mapping[str, Any]) -> dict:
    """Descriptive, not a gate (Jacob, 24 Oct): Jacob decides from it. `boundaries`: run key ->
    {task, subject, policy} of every audited boundary (the denominator; zeros are listed).

    Two counts: confirmed (repo check not_recoverable) and upper (adds unresolved). Sizing
    uses the repairable usable losses, the dropped ones (Experiment 3's first run repairs
    dropped items only; misstated ones are reported, not repaired): q = the share of
    boundaries with at least one, per configuration (subject x policy) and overall, with a
    Wilson 95% interval, and Experiment 3 base runs = 25/q (20/q-30/q, and 25 over the
    interval's ends). r (usable losses per boundary) is descriptive only."""
    meta = {str(k): _bmeta(v) for k, v in boundaries.items()}
    nb = len(meta)
    tasks = sorted({m["task"] for m in meta.values()})

    def conf(m):
        return f"{m.get('subject')}/{m.get('policy')}"

    out: dict[str, Any] = {
        "basis": "confirmed = repo check not_recoverable; upper adds unresolved. Sizing on "
                 "dropped usable losses only (repairable in Experiment 3's first run).",
        "boundaries_total": nb, "tasks_total": len(tasks),
        "counts": split_counts(usable),
    }
    for basis in ("confirmed", "upper"):
        ys = [u for u in usable if basis == "upper" or u.get("recoverable") == "not_recoverable"]
        per_task = Counter({t: 0 for t in tasks})
        per_b = Counter({k: 0 for k in meta})
        rep_b: Counter = Counter()
        for u in ys:
            per_task[str(u.get("task"))] += 1
            per_b[str(u["run_key"])] += 1
            if u.get("grade") == "dropped":
                rep_b[str(u["run_key"])] += 1

        confs = sorted({conf(m) for m in meta.values()})
        n_rep = sum(rep_b.values())
        tasks_rep = len({meta[k]["task"] for k in rep_b if k in meta})
        out[basis] = {
            "per_task": dict(sorted(per_task.items())),
            "per_boundary": dict(sorted(per_b.items())),
            "repairable_dropped": n_rep,
            "tasks_with_repairable": tasks_rep,
            "sizing_overall": _sizing(list(meta), rep_b),
            "sizing_by_configuration": {
                c: _sizing([k for k, m in meta.items() if conf(m) == c], rep_b) for c in confs},
            "r_descriptive": {"usable_per_boundary": round(len(ys) / nb, 4) if nb else None,
                              "dropped_per_boundary": round(n_rep / nb, 4) if nb else None},
            "reference_lines": [
                {"line": f">= {u} repairable (dropped) across >= {t} tasks",
                 "met": n_rep >= u and tasks_rep >= t, "for_information_only": True}
                for u, t in REFERENCE_LINES],
        }
    return out


DESIGN_FIELDS = {"d_repair": ("design", "repair_text"), "d_control": ("design", "control_text"),
                 "d_act": ("governed_decision", "observable_act"),
                 "d_scorer": ("design", "scorer")}
NEVER_REACHED = ("A continuation that never reaches the decision counts as not performing the "
                 "act; the main effect is never conditioned on reaching it.")


def final_design(label: Mapping, draft: Mapping | None) -> dict:
    """The design Experiment 3 runs: each field as Jacob left it on the page (his edit, or
    Haiku's draft when he didn't touch it), the fixed never-reached rule, and whether it is
    complete (every field non-empty, the control 0.5-2x the repair text's length)."""
    d = draft or {}
    out = {}
    for f, (part, k) in DESIGN_FIELDS.items():
        v = label[f] if f in label else (d.get(part) or {}).get(k)
        out[f[2:]] = (v or "").strip()
    rep, ctl = out["repair"], out["control"]
    ratio = len(ctl) / len(rep) if rep and ctl else None
    out["control_ratio"] = round(ratio, 3) if ratio is not None else None
    out["scorer_kind"] = (d.get("design") or {}).get("scorer_kind")
    out["never_reached"] = NEVER_REACHED
    out["edited"] = sorted(f[2:] for f in DESIGN_FIELDS if f in label)
    missing = [k for k in ("repair", "control", "act", "scorer") if not out[k]]
    bad_ratio = ratio is not None and not CONTROL_RATIO[0] <= ratio <= CONTROL_RATIO[1]
    out["complete"] = not missing and not bad_ratio
    out["problems"] = [f"empty:{k}" for k in missing] + (
        [f"control_length_ratio:{ratio:.2f}"] if bad_ratio else [])
    return out


def freeze(labels_obj: Any, key: Mapping, pool_rec: Mapping, drafts: Mapping[str, dict], *,
           boundaries: Mapping[str, Any] | None = None, out_path: Path = FROZEN_PATH,
           allow_incomplete: bool = False, inputs: Mapping[str, str | None] | None = None
           ) -> dict:
    """Freeze once. A candidate is usable with yes on all five checks AND a complete final
    design; a yes on check 5 over an incomplete design is refused (or, with allow_incomplete,
    counted as not passing check 5). `boundaries` (run key -> {task, subject, policy}) is the yield summary's
    denominator; without it, the boundaries in the loss rows are used and marked so."""
    out_path = Path(out_path)
    if out_path.exists():
        raise FrozenExists(f"{out_path} exists: the candidate list is frozen")
    labs = read_labels(labels_obj)
    ans = answers(labs, key, allow_incomplete=allow_incomplete)
    cands = {c["cand_id"]: c for c in pool_rec["candidates"]}
    designs = {cid: final_design(labs.get(cid) or {},
                                 (drafts.get(key["cards"][cid]["cand_id"]) or {}).get("draft"))
               for cid in key["order"]}
    broken = [cid for cid in key["order"] if ans[cid][-1] == "yes"
              and not designs[cid]["complete"]]
    if broken and not allow_incomplete:
        raise ValueError(f"{len(broken)} cards say yes on check 5 without a complete design "
                         f"(e.g. {broken[0]}: {designs[broken[0]]['problems']}); fix them on "
                         "the page or pass --allow-incomplete")
    for cid in broken:
        ans[cid][-1] = "design_incomplete"
    marginal = {c: Counter() for c in CHECKS}
    cumulative: dict[str, int] = {}
    cumulative_split: dict[str, dict] = {}

    def cand_of(cid):
        return cands[key["cards"][cid]["cand_id"]]
    alive = list(key["order"])
    excluded = list(pool_rec["excluded"]) + list(key.get("excluded_on_page") or [])
    for k, chk in enumerate(CHECKS):
        for cid in key["order"]:
            marginal[chk][ans[cid][k]] += 1
        nxt = []
        for cid in alive:
            a = ans[cid][k]
            if a == "yes":
                nxt.append(cid)
            else:
                note = (labs.get(cid) or {}).get(f"n{k + 1}") or ""
                excluded.append({"cand_id": key["cards"][cid]["cand_id"], "stage": chk,
                                 "reason": f"{a}" + (f": {note}" if note else "")})
        alive = nxt
        cumulative[chk] = len(alive)
        cumulative_split[chk] = split_counts(cand_of(cid) for cid in alive)
    usable = []
    for cid in alive:
        c = cands[key["cards"][cid]["cand_id"]]
        d = (drafts.get(c["cand_id"]) or {}).get("draft")
        lab = labs.get(cid) or {}
        usable.append({
            "cand_id": c["cand_id"], "card_id": cid, "run_key": c["run_key"],
            "item_id": c["item_id"], "task": c.get("task"), "subject": c.get("subject"),
            "policy": c.get("policy"), "channel": c.get("channel"), "type": c.get("type"),
            "grade": c.get("grade"), "text": c.get("text"), "quote": c.get("quote"),
            "msg": c.get("msg"), "recoverable": c.get("recoverable"),
            "recoverable_how": c.get("recoverable_how"),
            "governed_decision": ((d or {}).get("governed_decision") or {}).get("decision"),
            "design": designs[cid], "draft": d,
            "jacob_notes": {f"n{k}": lab.get(f"n{k}") or "" for k in range(1, 6)}
            | {"note": lab.get("note") or ""},
        })
    fun = pool_rec["funnel"]
    if boundaries is None:
        boundaries = dict(pool_rec.get("boundaries") or {})
        denom = "loss rows (boundaries with no item row are missing)"
    else:
        denom = "audited boundaries (lossaudit.boundaries)"
    rec = {
        "what": "S2 close-out: usable losses for Experiment 3, frozen before any continuation "
                "(docs/sprint/plans/experiment-2-closeout.md, Outcome scenarios)",
        "frozen_utc": _utc(),
        "prompt_sha256": prompt().sha256,
        "inputs_sha256": dict(inputs or {}),
        "draw": key["draw"], "seed": key["seed"], "page_sha256": key.get("page_sha256"),
        "exclude_draft_no": key.get("exclude_draft_no"),
        "auto_exclusion_note": (
            "Recoverability 'recoverable' (unit 3's repo search) was excluded before the page, "
            "as conservative candidate discovery; Jacob answered check 3 for the rest. "
            "'unresolved' candidates count in the upper count only. Each exclusion's evidence "
            "is in `excluded`."),
        "funnel": {
            "pool": dict(fun),
            "on_page": len(key["order"]),
            "on_page_split": split_counts(cand_of(cid) for cid in key["order"]),
            "excluded_on_page": len(key.get("excluded_on_page") or []),
            "checks_cumulative": cumulative,
            "checks_cumulative_split": cumulative_split,
            "checks_marginal": {c: dict(marginal[c]) for c in CHECKS},
            "usable": len(usable),
            "usable_split": split_counts(usable),
        },
        "allow_incomplete": allow_incomplete,
        "denominator": {"source": denom, "boundaries": dict(sorted(boundaries.items()))},
        "usable": usable,
        "excluded": excluded,
        "yield_summary": yield_summary(usable, boundaries),
        "decision": None,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "x") as f:  # exclusive create: a second freeze fails here too
        f.write(json.dumps(rec, indent=1) + "\n")
    return rec


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------


def _select(cands: Iterable[dict], only: str | None) -> list[dict]:
    keep = {x for x in (only or "").split(",") if x}
    return [c for c in cands if not keep or c["cand_id"] in keep]


def max_backend(concurrency: int):
    from compaction.claude_headless import MaxBackend

    return MaxBackend(concurrency=concurrency, cache_dir=CACHE_DIR, effort=HAIKU_EFFORT)


def cmd_pool(a) -> int:
    rec = pool(load_losses(Path(a.losses)))
    rec = {"utc": _utc(), "losses_sha256": _sha_file(Path(a.losses)), **rec}
    Path(a.pool).parent.mkdir(parents=True, exist_ok=True)
    Path(a.pool).write_text(json.dumps(rec, indent=1) + "\n")
    per_b = Counter(c["run_key"] for c in rec["candidates"])
    print(json.dumps({"funnel": rec["funnel"], "boundaries_with_candidates": len(per_b),
                      "pool": a.pool}, indent=1))
    return 0


def cmd_draft(a) -> int:
    if not a.max:
        print("refusing: pass --max to send Max-plan calls (claude -p, $0)", file=sys.stderr)
        return 2
    cands = _select(pool(load_losses(Path(a.losses)))["candidates"], a.only)
    drafts = draft_all(max_backend(a.concurrency), cands, views=Views(), out_path=Path(a.drafts),
                       batch=max(BATCH, 2 * a.concurrency))
    st = Counter(drafts[c["cand_id"]]["status"] for c in cands if c["cand_id"] in drafts)
    print(json.dumps({"candidates": len(cands), **st}, indent=1))
    return 0


def _page_inputs(a) -> tuple[dict, dict, list[dict], list[dict]]:
    rec = pool(load_losses(Path(a.losses)))
    drafts = load_drafts(Path(a.drafts))
    shown, excl = page_cands(rec["candidates"], drafts, exclude_draft_no=a.exclude_draft_no)
    return rec, drafts, shown, excl


def cmd_page(a) -> int:
    RP.require_private(Path(a.out), PRIVATE_ROOT)
    RP.require_ignored(Path(a.out))
    rec, drafts, shown, excl = _page_inputs(a)
    if not shown:
        print("no candidates reach the page", file=sys.stderr)
        return 1
    try:
        html, key = build(shown, drafts, Views(), seed=a.seed, funnel=rec["funnel"],
                          exclude_draft_no=a.exclude_draft_no, excluded=excl)
    except StaleDrafts as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    res = write_page(Path(a.out), html, key)
    nodraft = sum(1 for c in shown if not draft_ok((drafts.get(c["cand_id"]) or {}).get("draft")))
    print(json.dumps({**res, "excluded_on_page": len(excl), "without_valid_draft": nodraft},
                     indent=1))
    return 0


def audited_boundaries() -> dict[str, dict]:
    """run key -> {task, subject, policy} of every boundary the loss audit covers (its
    denominator), from each attempt's run.json plan."""
    from compaction.s2 import lossaudit
    from compaction.s2 import retention as RT

    out = {}
    for d in lossaudit.boundaries():
        run = json.loads((Path(d) / "run.json").read_text()) if (Path(d) / "run.json").exists() \
            else {}
        plan = run.get("plan") or {}
        out[RT.run_key(d)] = {"task": str(plan.get("task")), "subject": plan.get("model"),
                              "policy": plan.get("policy")}
    return out


def cmd_freeze(a) -> int:
    # each input is read once, and its sha256 is of the bytes used
    raw = {n: Path(p).read_bytes() for n, p in (("losses.jsonl", a.losses),
           ("candidate_drafts.jsonl", a.drafts), ("labels", a.labels), ("page_key", a.key))}
    inputs = {n: hashlib.sha256(b).hexdigest() for n, b in raw.items()}
    key = json.loads(raw["page_key"])
    rows = [json.loads(x) for x in raw["losses.jsonl"].decode().splitlines() if x.strip()]
    drafts = {}
    for x in raw["candidate_drafts.jsonl"].decode().splitlines():
        if x.strip():
            r = json.loads(x)
            drafts[r["cand_id"]] = r
    rec = pool(rows)
    shown, excl = page_cands(rec["candidates"], drafts, exclude_draft_no=key["exclude_draft_no"])
    try:
        _html, key2 = build(shown, drafts, Views(), seed=key["seed"], funnel=rec["funnel"],
                            exclude_draft_no=key["exclude_draft_no"], excluded=excl)
    except StaleDrafts as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    if (key2["draw"], key2["page_sha256"]) != (key["draw"], key["page_sha256"]):
        print("refusing: rebuilding the page from the current inputs gives a different page "
              f"(draw {key2['draw']} vs {key['draw']}): the evidence Jacob saw has changed",
              file=sys.stderr)
        return 2
    try:
        bounds = None if a.denominator_from_rows else audited_boundaries()
        out = freeze(json.loads(raw["labels"]), key, rec, drafts, boundaries=bounds,
                     out_path=Path(a.out), allow_incomplete=a.allow_incomplete, inputs=inputs)
    except FrozenExists as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"funnel": out["funnel"], "yield_summary": out["yield_summary"],
                      "out": a.out}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = {k: sub.add_parser(k) for k in ("pool", "draft", "page", "freeze")}
    for p in ps.values():
        p.add_argument("--losses", default=str(LOSSES_PATH))
        p.add_argument("--drafts", default=str(DRAFTS_PATH))
    ps["pool"].add_argument("--pool", default=str(POOL_PATH))
    ps["draft"].add_argument("--max", action="store_true", help="send the Max-plan calls")
    ps["draft"].add_argument("--concurrency", type=int, default=8)
    ps["draft"].add_argument("--only")
    ps["page"].add_argument("--out", default=str(PAGE_DIR))
    ps["page"].add_argument("--seed", type=int, default=SEED)
    ps["page"].add_argument("--exclude-draft-no", action="store_true")
    ps["freeze"].add_argument("--labels", required=True)
    ps["freeze"].add_argument("--key", default=str(PAGE_DIR / KEY_NAME))
    ps["freeze"].add_argument("--out", default=str(FROZEN_PATH))
    ps["freeze"].add_argument("--allow-incomplete", action="store_true")
    ps["freeze"].add_argument("--denominator-from-rows", action="store_true",
                              help="boundaries from losses.jsonl, not lossaudit.boundaries()")
    a = ap.parse_args(argv)
    return {"pool": cmd_pool, "draft": cmd_draft, "page": cmd_page,
            "freeze": cmd_freeze}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
