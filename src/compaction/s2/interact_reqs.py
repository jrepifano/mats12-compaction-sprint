"""SWE-Interact pilot: requirements, eligibility, grading against each summary source, and the
candidate screen (unit B).

Plan: docs/sprint/plans/swe-interact-pilot.md ("Eligible", "Candidates and the usable-loss
checks", "Offline replays"); interface: logs/s2/briefs/swe-interact-build.md, section 6, with
the astra r2 delta in logs/s2/briefs/swe-interact-shared-notes.md. `bdir` is always the boundary
attempt dir `<run>/b<k>` (the dir holding `boundary/`), as for `lossaudit.load`. Every function
that calls Haiku takes `backend` (an `s1_types.Backend`): the runner passes its Max backend with
the timed OpenRouter fallback, which also writes the ledger rows; nothing here books spend.

Split (`split`, at every boundary, from the runner's `stop_at_boundary` hook, before
`boundary.json` exists, so the hook passes its `fields`): a frozen Haiku prompt
(`prompts/interact_requirements.md`) splits the maintainer's `02_implement` replies that arrived
after the previous boundary into single requirements, each with its source reply, a verbatim
quote, a `same_as` link to a repeat and grounding against the task text the simulator holds
(SWE-Interact's `instruction.md`, its sha256 checked against run.json). Each reply is split once:
requirements of earlier boundaries are carried forward with their ids (`b<j>-q<n>`) and shown
to the prompt so repeats link to them. Code then checks every quote (`quote` in its reply,
`grounding_quote` in the task text; a quote not found makes the item invalid or ungrounded),
resolves repeats (each group's canonical item is its latest source: the latest reply, then the
later position in it; the others get `duplicate_of`) and applies the fresh rule:
  fresh = valid, step 02_implement, call_index > sim_calls of boundary k-1, pre_index not null
          and < tail_start_index (outside pi's verbatim tail), grounded, not a duplicate.
`in_tail` marks a reply of this segment inside pi's tail. `first_boundary` is the first boundary
at which the live summary dropped the requirement (from earlier boundaries' grades.json; set for
this boundary by `grade_sources`).

Eligibility (`eligibility`): eligible iff n_fresh >= 2 (plan: a pragmatic threshold, fixed).

Grading (`grade_sources`): the canonical requirements (non-duplicates, valid) graded kept /
dropped / misstated / unclear against each source's stored resumed input (`interact_score.
source_view`; nothing rebuilt): S1's pass 1b against the summary, "still present" when the quote
is in another message of the resumed input (the re-attached step instruction or a kept message),
then S1's second look on the dropped ones (tool calls on Max; a backend that refuses tool
requests, such as the OpenRouter fallback, leaves pass 1b's verdict standing, as S1 does for a
failed second look). A grade whose summary quote isn't found is "unclear".
Writes `grades.json` ({source: {req_id: grade}}, the frozen interface) and `grades_detail.json`
(the evidence).

Screen (`screen`, at a fork boundary, after the replays are graded): every fresh requirement for
each source in `ARM_SOURCES` order (the live pi summary first; then cc_prompt, codex_local,
opencode, antigravity), within a source by source reply then position. Checks, cheapest first,
until a candidate reaches a card:
  1 retention        code: the source's grade; kept or misstated (quote found) -> fail,
                     unclear or ungraded -> unresolved;
  3 recoverability   code, over the kept boundary image (unit A's `grep_runner`): every line of
                     the requirement's quote found -> fail; a fragment, or a draft search string
                     found -> unresolved (Jacob judges, with the hits); nothing -> pass;
  2, 4, 5            the draft (`prompts/interact_draft.md`, mapped by DRAFT_MAP): applicability,
                     the governed decision, and the design (act spec valid, matched addition in
                     the history, absent from the source's resumed input, within +-20% of the
                     repair's length);
  6 reference        code: `interact_score.check_reference`.
A candidate is excluded only on a fail backed by code-verified evidence (`CODE_FAILS`: retention,
recoverability on full lines, reference); judgment-only fails and unresolved checks go to the
card. The first candidate not excluded (and not in `rejected`, Jacob's earlier rejections) is
carded; later ones are `not_reached` (their retention, which is free, is still recorded).
Writes `boundary/candidates.json`; checks already in it are reused, so a second round (with the
first card in `rejected`) makes no repeat calls.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from compaction import s1_judge as J
from compaction import s1_second_look as SL
from compaction import s1_types as T
from compaction.s1_judge import load_prompt
from compaction.s2 import config as C
from compaction.s2 import interact_score as S
from compaction.s2 import lossaudit as LA

ARM_SOURCES = S.ARM_SOURCES
GRADE_SOURCES = S.GRADE_SOURCES
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
SPLIT_PROMPT = "interact_requirements"
DRAFT_PROMPT = "interact_draft"
SPLIT_PURPOSE = "s2_interact_split"
GRADE_PURPOSE = "s2_interact_grade"
DRAFT_PURPOSE = "s2_interact_draft"
SPLIT_MAX_OUT = 16_000
DRAFT_MAX_OUT = 8_000
DRAFT_HISTORY_CHARS = 200_000  # of the rendered pre-compaction history (its end is kept)
MIN_FRESH = 2
MATCHED_TOLERANCE = 0.20
STEP = "02_implement"
SWEI_TASKS = C.REPO_ROOT / "data" / "raw" / "sprint" / "swe_interact" / "repo" / "data" / "multiturn"
GRADES = ("kept", "dropped", "misstated", "unclear")
OUTCOMES = ("pass", "fail", "unresolved")
CODE_FAILS = ("retention", "recoverability", "reference")  # checks whose fail excludes
CHECKS = ("retention", "applicability", "recoverability", "decision", "design", "reference")
# The draft's answers -> check outcomes (frozen with the draft prompt's hash). A "no" is a
# judgment-only fail: it goes to the card, never excludes.
DRAFT_MAP = {"applicable": {"yes": "pass", "no": "fail", "unsure": "unresolved"},
             "decision_observable": {"yes": "pass", "no": "fail", "unsure": "unresolved"}}

_S = T._s
_STRS = {"type": "array", "items": _S()}
SPLIT_SCHEMA = T._obj({"requirements": {"type": "array", "items": T._obj({
    "local_id": _S(),
    "reply_id": _S(),
    "text": _S(),
    "quote": _S(),
    "same_as": _S(nullable=True),
    "grounded": T._BOOL,
    "grounding_quote": _S(nullable=True),
})}})
DRAFT_SCHEMA = T._obj({
    "applicable": _S(("yes", "no", "unsure")),
    "applicability_reason": _S(),
    "decision": _S(),
    "decision_observable": _S(("yes", "no", "unsure")),
    "act_kind": _S(S.ACT_KINDS),
    "act_pattern": _S(nullable=True),
    "act_path": _S(nullable=True),
    "act_negate": T._BOOL,
    "act_description": _S(),
    "decision_kind": _S((*S.MATCH_KINDS, "none")),
    "decision_pattern": _S(nullable=True),
    "decision_path": _S(nullable=True),
    "reask_keywords": _STRS,
    "matched_quote": _S(nullable=True),
    "matched_rationale": _S(nullable=True),
    "search_strings": _STRS,
})

GrepRunner = Callable[[str, list[str]], dict[str, dict]]
Grader = Callable[[list[dict], dict[str, dict]], dict[str, dict[str, dict]]]


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(p: Path) -> Any:
    return json.loads(Path(p).read_text())


def _read_or_none(p: Path) -> Any:
    try:
        return _read(p)
    except (OSError, ValueError):
        return None


def _fill(tpl: str, **slots: str) -> str:
    rx = re.compile(r"\{(" + "|".join(map(re.escape, slots)) + r")\}")
    return rx.sub(lambda m: slots[m.group(1)], tpl)


def prompt_shas() -> dict[str, str]:
    """The prompts to freeze before the first base run (with SPLIT_SCHEMA, DRAFT_SCHEMA and
    DRAFT_MAP, hashed together in `screen_sha`)."""
    return {n: load_prompt(n, PROMPT_DIR).sha256
            for n in (SPLIT_PROMPT, DRAFT_PROMPT, S.JUDGE_PROMPT)}


def screen_sha() -> str:
    return S.canon_sha({"draft": load_prompt(DRAFT_PROMPT, PROMPT_DIR).sha256,
                        "schema": DRAFT_SCHEMA, "map": DRAFT_MAP, "code_fails": CODE_FAILS,
                        "tolerance": MATCHED_TOLERANCE})


# ------------------------------------------------------------------------------------------
# Boundary facts
# ------------------------------------------------------------------------------------------


def boundary_index(bdir: Path) -> int:
    m = re.fullmatch(r"b(\d+)", Path(bdir).name)
    if not m:
        raise ValueError(f"{bdir}: not a b<k> boundary dir")
    return int(m.group(1))


def boundary_fields(bdir: Path, fields: dict | None = None) -> dict:
    """{boundary_index, tail_start_index, previous_sim_calls} for `bdir`. `fields` (the runner's
    BoundaryCtx.fields, before boundary.json is written) wins, then boundary.json; failing both,
    tail_start_index is the first of pi's kept indices in compaction.json (or the length of
    pre_messages when nothing is kept). previous_sim_calls is boundary k-1's `sim_calls` (None
    at the first boundary)."""
    b = Path(bdir) / "boundary"
    f = dict(_read_or_none(b / "boundary.json") or {})
    f.update(fields or {})
    k = f.get("boundary_index", boundary_index(bdir))
    tail = f.get("tail_start_index")
    if tail is None:
        comp = _read(b / "compaction.json")
        kept = comp.get("kept_indices") or []
        tail = min(kept) if kept else len(_read(b / "pre_messages.json"))
    prev = None
    if k > 1:
        pb = Path(bdir).parent / f"b{k - 1}" / "boundary" / "boundary.json"
        pj = _read_or_none(pb)
        if not isinstance(pj, dict) or not isinstance(pj.get("sim_calls"), int):
            raise FileNotFoundError(f"{pb}: previous boundary incomplete (no sim_calls)")
        prev = pj["sim_calls"]
    return {"boundary_index": k, "tail_start_index": int(tail), "previous_sim_calls": prev}


def task_text(bdir: Path) -> str:
    """SWE-Interact's instruction.md for the run's task (the simulator's task block), its sha256
    checked against run.json task.spec_sha256 when set."""
    run = _read(Path(bdir).parent / "run.json")
    task = run.get("task") or {}
    rel = task.get("spec_path")
    path = C.REPO_ROOT / rel if rel else SWEI_TASKS / f"deepswe_{task['task_id']}" / "instruction.md"
    raw = path.read_bytes()
    want = task.get("spec_sha256")
    if want and hashlib.sha256(raw).hexdigest() != want:
        raise ValueError(f"{path}: sha256 differs from run.json spec_sha256")
    return raw.decode("utf-8")


def _position(quote: str, reply: str) -> int:
    i = T.norm(reply).find(T.norm(quote))
    return i if i >= 0 else 1 << 30


# ------------------------------------------------------------------------------------------
# Split
# ------------------------------------------------------------------------------------------


def _carried(bdir: Path, k: int) -> list[dict]:
    """Every item of boundary k-1's requirements.json (its own and those it carried)."""
    if k <= 1:
        return []
    prev = Path(bdir).parent / f"b{k - 1}" / "boundary"
    rq = _read_or_none(prev / "requirements.json")
    if not isinstance(rq, dict):
        raise FileNotFoundError(f"{prev / 'requirements.json'}: previous boundary not split")
    grades = (_read_or_none(prev / "grades.json") or {}).get("live") or {}
    out = []
    for it in rq["items"]:
        it = dict(it)
        if it.get("first_boundary") is None and grades.get(it["req_id"]) == "dropped":
            it["first_boundary"] = k - 1
        out.append(it)
    return out


def split_request(bdir: Path, task: str, replies: list[dict], earlier: list[dict]) -> T.Req:
    p = load_prompt(SPLIT_PROMPT, PROMPT_DIR)
    rl = "\n".join(json.dumps({"reply_id": r["reply_id"], "question": r.get("question") or "",
                               "reply": r.get("reply") or ""}, ensure_ascii=False)
                   for r in replies)
    el = "\n".join(json.dumps({"req_id": e["req_id"], "text": e["text"]}, ensure_ascii=False)
                   for e in earlier if e.get("duplicate_of") is None and e.get("valid"))
    user = _fill(p.sections["user"], task=task, earlier=el or "(none)", replies=rl)
    rid = f"interact:{Path(bdir).parent.name}:b{boundary_index(bdir)}"
    return T.Req(key=f"split|{rid}", system=p.system, user=user, schema=SPLIT_SCHEMA,
                 max_output_tokens=SPLIT_MAX_OUT, rid=rid, source="deepswe", private=True)


def _group_duplicates(items: list[dict], links: dict[str, str]) -> None:
    """Union the `same_as` links; each group's canonical item is its latest source (call_index,
    then position); the others get duplicate_of = the canonical req_id."""
    parent = {it["req_id"]: it["req_id"] for it in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in links.items():
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    groups: dict[str, list[dict]] = {}
    for it in items:
        if it.get("valid"):
            groups.setdefault(find(it["req_id"]), []).append(it)
    for members in groups.values():
        canon = max(members, key=lambda it: (it["call_index"], it["position"]))
        for it in members:
            it["duplicate_of"] = None if it is canon else canon["req_id"]


def split(bdir: Path, *, backend, fields: dict | None = None, task: str | None = None) -> dict:
    """Write and return `boundary/requirements.json` (module docstring)."""
    b = Path(bdir) / "boundary"
    bf = boundary_fields(bdir, fields)
    k, tail, prev = bf["boundary_index"], bf["tail_start_index"], bf["previous_sim_calls"]
    replies = _read(b / "sim_replies.json")
    by_id = {r["reply_id"]: r for r in replies}
    task = task if task is not None else task_text(bdir)
    carried = _carried(bdir, k)
    new = [r for r in replies if r.get("step") == STEP
           and (prev is None or r["call_index"] > prev)]
    p = load_prompt(SPLIT_PROMPT, PROMPT_DIR)
    out: dict = {"prompt_sha256": p.sha256, "model": None, "created_utc": _utc(),
                 "boundary_index": k, "tail_start_index": tail, "previous_sim_calls": prev,
                 "n_new_replies": len(new), "error": None, "items": []}
    drafted: list[dict] = []
    if new:
        req = split_request(bdir, task, new, carried)
        r = backend.run([req], purpose=SPLIT_PURPOSE).get(req.key)
        if r is None or r.error or not isinstance(r.data, dict):
            raise RuntimeError(f"{bdir}: split failed: {r.error if r else 'no response'}")
        out["model"] = r.model
        drafted = r.data["requirements"]
    items: list[dict] = []
    for it in carried:  # recomputed for this boundary below
        src = by_id.get(it["source_reply_id"]) or {}
        items.append(dict(it, source_pre_index=src.get("pre_index")))
    links: dict[str, str] = {}
    local: dict[str, str] = {}
    new_ids = {r["reply_id"] for r in new}
    for n, d in enumerate(drafted, 1):
        rid = f"b{k}-q{n}"
        local[d["local_id"]] = rid
        src = by_id.get(d["reply_id"]) if d["reply_id"] in new_ids else None
        reply = (src or {}).get("reply") or ""
        valid = src is not None and T.quote_in(d["quote"], reply)
        g_found = bool(d["grounded"]) and T.quote_in(d.get("grounding_quote"), task)
        items.append({
            "req_id": rid, "text": d["text"], "quote": d["quote"],
            "source_reply_id": d["reply_id"],
            "source_pre_index": (src or {}).get("pre_index"),
            "call_index": (src or {}).get("call_index", -1),
            "step": (src or {}).get("step"),
            "position": _position(d["quote"], reply) if valid else 1 << 30,
            "duplicate_of": None,
            "grounded": g_found,
            "grounded_claimed": bool(d["grounded"]),
            "grounding_quote": d.get("grounding_quote"),
            "valid": valid,
            "invalid_reason": None if valid else (
                "unknown_reply" if src is None else "quote_not_in_reply"),
            "fresh": False, "in_tail": False, "first_boundary": None,
            "boundary": k,
        })
        if d.get("same_as"):
            links[rid] = d["same_as"]
    links = {a: local.get(b_, b_) for a, b_ in links.items()}
    # Earlier boundaries' groups stay linked (each repeat to its canonical item).
    links |= {it["req_id"]: it["duplicate_of"] for it in carried if it.get("duplicate_of")}
    # Rank positions within each reply (character offsets -> 0, 1, 2 ...).
    for rid_ in {it["source_reply_id"] for it in items if it["boundary"] == k}:
        mine = sorted((it for it in items if it["boundary"] == k
                       and it["source_reply_id"] == rid_), key=lambda it: it["position"])
        for j, it in enumerate(mine):
            it["position"] = j
    _group_duplicates(items, links)
    for it in items:
        pi = it.get("source_pre_index")
        this_segment = it.get("step") == STEP and (prev is None or it["call_index"] > prev)
        it["in_tail"] = bool(this_segment and pi is not None and pi >= tail)
        it["fresh"] = bool(it.get("valid") and this_segment and pi is not None and pi < tail
                           and it.get("grounded") and it.get("duplicate_of") is None)
    out["items"] = items
    S.write_json(b / "requirements.json", out)
    return out


# ------------------------------------------------------------------------------------------
# Eligibility
# ------------------------------------------------------------------------------------------


def eligibility(bdir: Path, *, min_fresh: int = MIN_FRESH, fields: dict | None = None) -> dict:
    """Write and return `boundary/eligibility.json`: {eligible, n_fresh, fresh_ids, n_in_tail,
    n_inherited, n_ungrounded} plus the boundary index and min_fresh. Counts are over valid,
    canonical (non-duplicate) items: n_in_tail and n_ungrounded among this segment's, and
    n_inherited the items whose source reply came before the previous boundary."""
    rq = _read(Path(bdir) / "boundary" / "requirements.json")
    prev = rq.get("previous_sim_calls")
    canon = [it for it in rq["items"] if it.get("valid") and it.get("duplicate_of") is None]
    seg = [it for it in canon if it.get("step") == STEP
           and (prev is None or it["call_index"] > prev)]
    fresh = sorted((it for it in canon if it.get("fresh")),
                   key=lambda it: (it["call_index"], it["position"]))
    out = {
        "eligible": len(fresh) >= min_fresh,
        "n_fresh": len(fresh),
        "fresh_ids": [it["req_id"] for it in fresh],
        "n_in_tail": sum(it["in_tail"] for it in seg),
        "n_inherited": len(canon) - len(seg),
        "n_ungrounded": sum(not it.get("grounded") for it in seg),
        "min_fresh": min_fresh,
        "boundary_index": rq["boundary_index"],
        "requirements_prompt_sha256": rq["prompt_sha256"],
    }
    S.write_json(Path(bdir) / "boundary" / "eligibility.json", out)
    return out


# ------------------------------------------------------------------------------------------
# Grading
# ------------------------------------------------------------------------------------------


class S1Grader:
    """S1's pass 1b and second look, plus "still present" in the resumed input (module
    docstring). Each source's requirements go to pass 1b in one task; all sources run in one
    `drive`, so the backend's own concurrency applies across them."""

    def __init__(self, backend, *, rid_prefix: str = "interact", purpose: str = GRADE_PURPOSE):
        self.backend, self.prefix, self.purpose = backend, rid_prefix, purpose

    def __call__(self, items: list[dict], views: dict[str, dict]) -> dict[str, dict[str, dict]]:
        tasks, rows_by, recs = {}, {}, {}
        for src, view in views.items():
            rid = f"{self.prefix}:{src}"
            recs[rid] = {"id": rid, "source": "deepswe"}
            rows = [{"n": n, "rid": rid, "base_rid": rid, "arm": src, "item_id": it["req_id"],
                     "type": "rule", "text": it["text"], "quote": it["quote"], "valid": True}
                    for n, it in enumerate(items)]
            tasks[src] = J.Pass1b(self.backend, rows, view["summary"], rid, recs[rid],
                                  J.prompts())
        J.drive(self.backend, list(tasks.values()), self.purpose)
        firsts: list[dict] = []
        for src, t in tasks.items():
            kept = S.kept_texts(views[src])
            for x in t.items:
                x["still_present"] = any(T.quote_in(x["quote"], m) for m in kept)
            rows_by[src] = t.items
            firsts += [x for x in t.items if not x["still_present"]]
        overlay: list[dict] = []
        if self.backend.family == "anthropic" and firsts:
            # One second-look batch per source: S1 keys an item's request "sl|item|<item id>",
            # and the same requirement is graded against several sources, so one batch held
            # duplicate keys (testem b1, 24 Oct: "duplicate Req.key in batch"). The batches run
            # one after another: a bare MaxBackend refuses overlapping run() calls (astra fixes
            # r1). The overlay rows still match by (kind, rid, id), rid naming the source.
            by_src: dict[str, list[dict]] = {}
            for x in firsts:
                by_src.setdefault(x["arm"], []).append(x)
            for rows in by_src.values():
                overlay += SL.second_look(
                    self.backend, rows, [], load_rec=lambda rid: recs[rid],
                    summary_of=lambda row, rec: views[row["arm"]]["summary"],
                    purpose=self.purpose)
        by_key = SL.index_overlay(overlay)
        out: dict[str, dict[str, dict]] = {}
        for src, rows in rows_by.items():
            final = SL.apply(rows, overlay)
            out[src] = {}
            for x in final:
                o = by_key.get(SL.row_key(x))
                if x["still_present"]:
                    g = "kept"
                elif not x.get("grade_valid"):
                    g = "unclear"
                else:
                    g = x["grade"] if x["grade"] in GRADES else "unclear"
                out[src][x["item_id"]] = {
                    "grade": g, "first_grade": x.get("grade") if o is None else o["first_verdict"],
                    "summary_quote": (o or {}).get("quote") or x.get("summary_quote"),
                    "still_present": x["still_present"], "grade_note": x.get("grade_note"),
                    "grade_reason": x.get("grade_reason"), "second_look": x.get("second_look")}
        return out


def gradable(rq: dict) -> list[dict]:
    """The requirements graded at a boundary (the plan's descriptive denominator; astra build
    r1 #13): valid, canonical, from 02_implement, grounded in the task text, and with their
    source reply verbatim in this boundary's pre-compaction input. Items lost at an earlier
    boundary are tracked there (`first_boundary`), not re-graded here."""
    return [it for it in rq["items"] if it.get("valid") and it.get("duplicate_of") is None
            and it.get("step") == STEP and it.get("grounded")
            and it.get("source_pre_index") is not None]


def grade_sources(bdir: Path, sources: Iterable[str] = GRADE_SOURCES, *, backend=None,
                  grader: Grader | None = None) -> dict:
    """Write `boundary/grades.json` ({source: {req_id: grade}}) and `grades_detail.json`;
    merged into what is already there, so sources can be graded as their replays land. Sets
    `first_boundary` = this boundary in requirements.json for requirements the live source
    drops here."""
    b = Path(bdir) / "boundary"
    rq = _read(b / "requirements.json")
    items = gradable(rq)
    if grader is None:
        if backend is None:
            raise ValueError("grade_sources needs a backend or a grader")
        grader = S1Grader(backend, rid_prefix=f"interact:{Path(bdir).parent.name}:"
                                              f"b{rq['boundary_index']}")
    views = {s: S.source_view(bdir, s) for s in sources}
    detail_new = grader(items, views) if items else {s: {} for s in views}
    detail = _read_or_none(b / "grades_detail.json") or {}
    detail.update(detail_new)
    grades = {s: {rid: d["grade"] for rid, d in per.items()} for s, per in detail.items()}
    for s, per in grades.items():
        bad = {g for g in per.values() if g not in GRADES}
        if bad:
            raise ValueError(f"grader returned {bad} for {s}")
    S.write_json(b / "grades_detail.json", detail)
    S.write_json(b / "grades.json", grades)
    live = grades.get("live") or {}
    changed = False
    for it in rq["items"]:
        if it.get("first_boundary") is None and live.get(it["req_id"]) == "dropped":
            it["first_boundary"] = rq["boundary_index"]
            changed = True
    if changed:
        S.write_json(b / "requirements.json", rq)
    return grades


# ------------------------------------------------------------------------------------------
# Candidate screen
# ------------------------------------------------------------------------------------------


def candidates_order(rq: dict, sources: Iterable[str] = ARM_SOURCES) -> list[tuple[str, dict]]:
    fresh = sorted((it for it in rq["items"] if it.get("fresh")),
                   key=lambda it: (it["call_index"], it["position"]))
    return [(s, it) for s in sources for it in fresh]


def cand_id(source: str, req_id: str) -> str:
    return f"{source}.{req_id}"


def _check(outcome: str, reason: str | None = None, **evidence) -> dict:
    assert outcome in OUTCOMES
    return {"outcome": outcome, "reason": reason, "evidence": evidence}


def check_retention(src: str, it: dict, detail: dict) -> dict:
    d = (detail.get(src) or {}).get(it["req_id"])
    if d is None:
        return _check("unresolved", "not_graded")
    g = d["grade"]
    ev = {"grade": g, "summary_quote": d.get("summary_quote"),
          "still_present": d.get("still_present")}
    if g == "dropped":
        return _check("pass", None, **ev)
    if g in ("kept", "misstated"):
        return _check("fail", f"retention:{g}", **ev)
    return _check("unresolved", "grade_unclear", **ev)


def _hits(res: dict, patterns: list[str]) -> dict[str, list[str]]:
    return {p: (res.get(p) or {}).get("files") or [] for p in patterns
            if (res.get(p) or {}).get("files")}


def check_recoverability(it: dict, image: str | None, grep_runner: GrepRunner | None,
                         search_strings: list[str] | None = None) -> dict:
    """Three-way over the kept image (module docstring). Without the draft's search strings
    (None) a clean result is provisional: the screen re-runs this after the draft."""
    if grep_runner is None or not image:
        return _check("unresolved", "no_grep_runner" if grep_runner is None else "no_image")
    lines = LA.quote_lines(it["quote"])
    frags = [f for f in LA.key_strings(it["quote"]) if f not in lines]
    extra = [s for s in (search_strings or []) if s and s not in lines and s not in frags]
    pats = list(dict.fromkeys(lines + frags + extra))
    res = grep_runner(image, pats) if pats else {}
    line_hits, frag_hits, extra_hits = _hits(res, lines), _hits(res, frags), _hits(res, extra)
    ev = {"image": image, "line_hits": line_hits, "fragment_hits": frag_hits,
          "search_hits": extra_hits, "searched": pats}
    if lines and len(line_hits) == len(lines):
        return _check("fail", "recoverable:quote_lines_found", **ev)
    if frag_hits or extra_hits or line_hits:
        return _check("unresolved", "partial_or_search_hits", **ev)
    return _check("pass", None, **ev)


def _render_history(pre: list[dict], limit: int = DRAFT_HISTORY_CHARS) -> str:
    text = "\n\n".join(f"[{i}] {m.get('role')}: {S.message_text(m)}"
                       for i, m in enumerate(pre) if m.get("role") != "system")
    return text[-limit:]


def draft_request(bdir: Path, src: str, it: dict, view: dict, task: str) -> T.Req:
    b = Path(bdir) / "boundary"
    p = load_prompt(DRAFT_PROMPT, PROMPT_DIR)
    replies = _read(b / "sim_replies.json")
    rl = "\n".join(json.dumps({"reply_id": r["reply_id"], "step": r.get("step"),
                               "reply": r.get("reply") or ""}, ensure_ascii=False)
                   for r in replies)
    req_json = json.dumps({"text": it["text"], "quote": it["quote"],
                           "reply_id": it["source_reply_id"]}, ensure_ascii=False)
    user = _fill(p.sections["user"], task=task, requirement=req_json, replies=rl,
                 summary=view["summary"],
                 history=_render_history(_read(b / "pre_messages.json")))
    rid = f"interact:{Path(bdir).parent.name}:b{boundary_index(bdir)}"
    return T.Req(key=f"draft|{rid}|{cand_id(src, it['req_id'])}", system=p.system, user=user,
                 schema=DRAFT_SCHEMA, max_output_tokens=DRAFT_MAX_OUT, rid=rid,
                 source="deepswe", private=True)


def spec_from_draft(d: dict) -> dict:
    dec = None
    if d["decision_kind"] != "none":
        dec = {"kind": d["decision_kind"], "pattern": d["decision_pattern"],
               "path": d["decision_path"]}
    return {"act": {"kind": d["act_kind"], "pattern": d["act_pattern"], "path": d["act_path"],
                    "negate": bool(d["act_negate"])},
            "decision": dec, "description": d["act_description"],
            "reask_keywords": list(d["reask_keywords"])}


def maintainer_context(bdir: Path) -> dict:
    """What a v2 matched block is checked against (24 Oct, the main session): the maintainer's
    replies the agent's history holds (sim replies with a `pre_index`) and every requirement
    quote split at this boundary."""
    b = Path(bdir) / "boundary"
    replies = [r.get("reply") or "" for r in _read_or_none(b / "sim_replies.json") or []
               if r.get("pre_index") is not None]
    rq = _read_or_none(b / "requirements.json") or {}
    return {"replies": replies,
            "req_quotes": [it.get("quote") or "" for it in rq.get("items") or []]}


def _spans(quote: str, reply: str) -> list[tuple[int, int]]:
    """Every (start, end) of the quote in the reply, whitespace-insensitive."""
    toks = (quote or "").split()
    if not toks:
        return []
    return [(m.start(), m.end()) for m in
            re.finditer(r"\s+".join(re.escape(t) for t in toks), reply or "")]


def _leads_in(quote: str, reply: str) -> bool:
    """At any of its places in this reply, the quote ends with a colon or is followed (across
    whitespace and line breaks) by one: the lead-in of what comes next ("X is still off: make it
    Y"), which carries that requirement."""
    if quote.rstrip().endswith(":"):
        return bool(_spans(quote, reply))
    return any(reply[b:].lstrip().startswith(":") for _, b in _spans(quote, reply))


_SENTENCE_END = re.compile(r"(?:[.!?](?=\s|$)|\n)\s*")  # a gap of blank lines is one end


def _sentence(reply: str, a: int, b: int, around: int = 1) -> str:
    """The sentence (or line) of the reply around [a, b), with `around` sentences on each side
    (a reason or complaint often sits next to the rule it motivates)."""
    ends = [m.end() for m in _SENTENCE_END.finditer(reply)]
    before = [e for e in ends if e <= a]
    after = [e for e in ends if e > b - 1]
    lo = before[-1 - around] if len(before) > around else 0
    hi = after[around] if len(after) > around else len(reply)
    return reply[lo:hi]


def _explains_requirement(quote: str, reply: str, req_quotes: list[str]) -> bool:
    """At any of its places in this reply, the quote's sentence or a neighbouring one states,
    explains or justifies a requirement: it contains, or shares a six-word run with, a
    requirement quote."""
    for a, b in _spans(quote, reply):
        sent = _sentence(reply, a, b)
        sh = _shingles(sent)
        if any(T.quote_in(q, sent) or T.quote_in(sent, q) or (sh & _shingles(q))
               for q in req_quotes if q):
            return True
    return False


def _shingles(text: str, n: int = 6) -> set[tuple[str, ...]]:
    w = re.findall(r"[a-z0-9_]+", (text or "").lower())
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def check_design(it: dict, d: dict, spec: dict, view: dict, pre: list[dict],
                 maintainer: dict | None = None) -> dict:
    """The design check. With `maintainer` (v2 screens and later): the matched block must be the
    repair's kind of text, a quote from a maintainer reply in the history (`maintainer_context`),
    share no six-word run with any requirement quote, and sit in no sentence that states,
    explains or justifies one, nor lead into one: it must carry no requirement."""
    errs = S.validate_spec(spec)
    mq = d.get("matched_quote")
    ev: dict = {"spec_errors": errs, "matched_quote": mq,
                "matched_rationale": d.get("matched_rationale")}
    probs = list(errs)
    if not mq or not d.get("matched_rationale"):
        probs.append("no_matched_addition")
    else:
        lo, hi = (len(it["quote"]) * (1 - MATCHED_TOLERANCE),
                  len(it["quote"]) * (1 + MATCHED_TOLERANCE))
        ev["length_ratio"] = round(len(mq) / max(1, len(it["quote"])), 3)
        if not lo <= len(mq) <= hi:
            probs.append("matched_length_out_of_range")
        if not any(T.quote_in(mq, S.message_text(m)) for m in pre):
            probs.append("matched_not_in_history")
        if S.in_resumed_input(mq, view):
            probs.append("matched_in_resumed_input")
        if maintainer is not None:
            if not any(T.quote_in(mq, r) for r in maintainer["replies"]):
                probs.append("matched_not_maintainer")
            else:
                if any(_leads_in(mq, r) for r in maintainer["replies"]):
                    probs.append("matched_leads_into_requirement")
                if any(_explains_requirement(mq, r, maintainer["req_quotes"])
                       for r in maintainer["replies"]):
                    probs.append("matched_explains_requirement")
            sh = _shingles(mq)
            if any(T.quote_in(mq, q) or T.quote_in(q, mq) or (sh & _shingles(q))
                   for q in maintainer["req_quotes"] if q):
                probs.append("matched_overlaps_requirement")
    ev["problems"] = probs
    return _check("unresolved", ";".join(probs), **ev) if probs else _check("pass", None, **ev)


def _excluded(checks: dict) -> str | None:
    for c in CODE_FAILS:
        if (checks.get(c) or {}).get("outcome") == "fail":
            return c
    return None


def screen(bdir: Path, *, backend, grep_runner: GrepRunner | None = None,
           ref_runner: S.Runner | None = None, rejected: Iterable[str] = (),
           sources: Iterable[str] = ARM_SOURCES, task: str | None = None) -> dict:
    """Write and return `boundary/candidates.json` (module docstring)."""
    b = Path(bdir) / "boundary"
    k = boundary_index(bdir)
    rq = _read(b / "requirements.json")
    detail = _read_or_none(b / "grades_detail.json") or {}
    old = {c["cand_id"]: c for c in (_read_or_none(b / "candidates.json") or {})
           .get("candidates", [])}
    rejected = set(rejected)
    image = ((_read_or_none(Path(bdir) / "run.json") or {}).get("task") or {}).get("image")
    pre = _read(b / "pre_messages.json")
    views: dict[str, dict] = {}
    out_c: list[dict] = []
    carded = None
    for src, it in candidates_order(rq, sources):
        cid = cand_id(src, it["req_id"])
        prev = old.get(cid) or {}
        checks = dict(prev.get("checks") or {})
        c = {"cand_id": cid, "source_policy": src, "req_id": it["req_id"],
             "source_reply_id": it["source_reply_id"], "position": it["position"],
             "repair_text": it["quote"], "text": it["text"], "checks": checks,
             "draft": prev.get("draft"), "act_spec": prev.get("act_spec"),
             "disposition": None}
        checks["retention"] = check_retention(src, it, detail)
        out_c.append(c)
        if carded is not None:
            c["disposition"] = "not_reached"
            continue
        if _excluded(checks):
            c["disposition"] = f"excluded:{_excluded(checks)}"
            continue
        if "recoverability" not in checks:
            checks["recoverability"] = check_recoverability(it, image, grep_runner)
        if _excluded(checks):
            c["disposition"] = f"excluded:{_excluded(checks)}"
            continue
        if src not in views:
            views[src] = S.source_view(bdir, src)
        if c["draft"] is None:
            task = task if task is not None else task_text(bdir)
            req = draft_request(bdir, src, it, views[src], task)
            r = backend.run([req], purpose=DRAFT_PURPOSE).get(req.key)
            if r is None or r.error or not isinstance(r.data, dict):
                c["draft"] = {"error": r.error if r else "no_response"}
            else:
                c["draft"] = dict(r.data, model=r.model)
        d = c["draft"]
        if "error" in d:
            for name in ("applicability", "decision", "design", "reference"):
                checks.setdefault(name, _check("unresolved", "draft_failed", error=d["error"]))
        else:
            c["act_spec"] = spec = spec_from_draft(d)
            checks["applicability"] = _check(DRAFT_MAP["applicable"][d["applicable"]], None,
                                             why=d["applicability_reason"])
            dec = DRAFT_MAP["decision_observable"][d["decision_observable"]]
            checks["decision"] = _check(dec, None, decision=d["decision"])
            checks["design"] = check_design(it, d, spec, views[src], pre)
            rec = checks["recoverability"]
            if d["search_strings"] and not rec["evidence"].get("search_after_draft"):
                rec = check_recoverability(it, image, grep_runner, d["search_strings"])
                rec["evidence"]["search_after_draft"] = True
                checks["recoverability"] = rec
            if "reference" not in checks and not _excluded(checks):
                checks["reference"] = S.check_reference(spec, Path(bdir).parent, k,
                                                        runner=ref_runner)
        why = _excluded(checks)
        if why:
            c["disposition"] = f"excluded:{why}"
        elif cid in rejected:
            c["disposition"] = "rejected"
        else:
            c["disposition"] = "carded"
            carded = cid
    out = {"boundary_index": k, "created_utc": _utc(), "screen_sha256": screen_sha(),
           "draft_prompt_sha256": load_prompt(DRAFT_PROMPT, PROMPT_DIR).sha256,
           "draft_map": DRAFT_MAP, "code_fails": list(CODE_FAILS), "sources": list(sources),
           "rejected": sorted(rejected), "carded": carded, "candidates": out_c}
    S.write_json(b / "candidates.json", out)
    return out


# ------------------------------------------------------------------------------------------
# v2: the accumulated-loss screen (docs/sprint/plans/accumulated-loss.md, "Fork rule")
# ------------------------------------------------------------------------------------------
# Written beside the pilot's files, never over them: boundary/fork_v2/{grades.json,
# grades_detail.json, ledger.json, candidates.json}. requirements.json (the split) is shared and
# never rewritten here. The fork rule: the first boundary where one summary, in source order,
# has a dropped requirement that passes the checks and has a pending governed decision evidenced
# in the boundary workspace (`check_pending`, code). Further qualifying candidates of the same
# source become a bundle (disposition "bundle").

SCREEN_V2 = "fork_v2"
DRAFT_PROMPT_V2 = "interact_draft_v2"
CHECKS_V2 = CHECKS  # the pilot's six (Jacob, 24 Oct: the pending decision is applicability's)
CODE_FAILS_V2 = CODE_FAILS  # plus an applicability fail marked code_fail (no pending spot)
PENDING_MIN_CHARS = 8
DRAFT_SCHEMA_V2 = T._obj({**DRAFT_SCHEMA["properties"],
                          "pending_path": _S(nullable=True),
                          "pending_quote": _S(nullable=True),
                          "pending_reason": _S()})


def v2_dir(bdir: Path) -> Path:
    return Path(bdir) / "boundary" / SCREEN_V2


def gradable_v2(rq: dict) -> list[dict]:
    """Every requirement disclosed so far: valid, canonical, from 02_implement, grounded in the
    task text; carried ones (split at an earlier boundary) included, so a loss can be followed
    across compactions."""
    return [it for it in rq["items"] if it.get("valid") and it.get("duplicate_of") is None
            and it.get("step") == STEP and it.get("grounded")]


def grade_sources_v2(bdir: Path, sources: Iterable[str] = GRADE_SOURCES, *, backend=None,
                     grader: Grader | None = None) -> dict:
    """Grade every source on `gradable_v2` (fork_v2/grades.json, grades_detail.json)."""
    b = Path(bdir) / "boundary"
    rq = _read(b / "requirements.json")
    items = gradable_v2(rq)
    if grader is None:
        if backend is None:
            raise ValueError("grade_sources_v2 needs a backend or a grader")
        grader = S1Grader(backend, rid_prefix=f"interact:{Path(bdir).parent.name}:"
                                              f"b{rq['boundary_index']}:v2")
    views = {s: S.source_view(bdir, s) for s in sources}
    detail = grader(items, views) if items else {s: {} for s in views}
    grades = {s: {rid: d["grade"] for rid, d in per.items()} for s, per in detail.items()}
    for s, per in grades.items():
        bad = {g for g in per.values() if g not in GRADES}
        if bad:
            raise ValueError(f"grader returned {bad} for {s}")
    S.write_json(v2_dir(bdir) / "grades_detail.json", detail)
    S.write_json(v2_dir(bdir) / "grades.json", grades)
    return grades


def loss_ledger(bdir: Path) -> dict:
    """Q1 bookkeeping (fork_v2/ledger.json): per source, every requirement graded dropped or
    misstated here, marked NEW (the previous live summary still carried it, or it was disclosed
    since: verbatim in this boundary's input) or INHERITED (the previous boundary's live
    summary already lacked it), with counts."""
    k = boundary_index(bdir)
    grades = _read(v2_dir(bdir) / "grades.json")
    replies = {r["reply_id"]: r for r in _read(Path(bdir) / "boundary" / "sim_replies.json")}
    rq = _read(Path(bdir) / "boundary" / "requirements.json")
    items = {it["req_id"]: it for it in gradable_v2(rq)}
    prev_live = {}
    if k > 1:
        pg = _read_or_none(Path(bdir).parent / f"b{k - 1}" / "boundary" / SCREEN_V2 / "grades.json")
        if not isinstance(pg, dict):
            raise FileNotFoundError(f"b{k - 1}: no fork_v2 grades (screen boundaries in order)")
        prev_live = pg.get("live") or {}
    out: dict = {"boundary_index": k, "sources": {}, "counts": {}}
    for src, per in grades.items():
        rows = {}
        for rid, g in per.items():
            if g not in ("dropped", "misstated"):
                continue
            it = items.get(rid) or {}
            verbatim = (replies.get(it.get("source_reply_id")) or {}).get("pre_index") is not None
            pl = prev_live.get(rid)
            status = ("new" if verbatim or pl in (None, "kept") else
                      "inherited" if pl in ("dropped", "misstated") else "unknown")
            rows[rid] = {"grade": g, "status": status, "verbatim_here": verbatim,
                         "previous_live": pl}
        out["sources"][src] = rows
        out["counts"][src] = {s: sum(r["status"] == s for r in rows.values())
                              for s in ("new", "inherited", "unknown")}
    S.write_json(v2_dir(bdir) / "ledger.json", out)
    return out


def candidates_order_v2(rq: dict, sources: Iterable[str] = ARM_SOURCES
                        ) -> list[tuple[str, dict]]:
    """Source first (ARM_SOURCES), then the earliest source reply, then position in it."""
    its = sorted(gradable_v2(rq), key=lambda it: (it["call_index"], it["position"]))
    return [(s, it) for s in sources for it in its]


# A line number in front of a quoted line, as `cat -n`, `grep -n` or `nl` print it.
_LINE_NO = re.compile(r"^\s*\d+(?:\s*[:|]\s?|\t|\s{2,})")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # every length (astra build r3)
FILE_MARK = "@@FILE@@"
FileRunner = Callable[[str, str], dict]  # (image, path) -> {"status": ok|missing|error, "text"}


def pending_quote(quote: str) -> str:
    """The comparable form of a cited line: no line-number prefix, no surrounding space."""
    return _LINE_NO.sub("", quote or "", count=1).strip()


def file_script(path: str) -> str:
    """Print the working-tree file `path` of /app after a marker, or MISSING (the path is
    base64'd, so nothing in it reaches the shell)."""
    b64 = base64.b64encode(path.encode()).decode()
    return "\n".join([
        "cd /app || exit 3",
        f"P=$(echo {b64} | base64 -d)",
        'case "$P" in /*|*..*) echo @@BADPATH@@; exit 0;; esac',
        f'if [ -f "$P" ]; then echo {FILE_MARK}; cat -- "$P"; else echo @@MISSING@@; fi'])


def docker_file_runner(host: str | None = None) -> FileRunner:
    """Reads one file of a kept boundary image (interact_score.docker_runner: a fresh
    container, removed after)."""
    run = S.docker_runner(host)

    def read(image: str, path: str) -> dict:
        rc, out, err = run(image, file_script(path))
        if rc != 0:
            return {"status": "error", "error": (err or out)[-300:]}
        if "@@MISSING@@" in out or "@@BADPATH@@" in out:
            return {"status": "missing"}
        if FILE_MARK not in out:
            return {"status": "error", "error": out[-300:]}
        return {"status": "ok", "text": out.split(FILE_MARK + "\n", 1)[-1]}
    return read


def check_pending(d: dict, image: str | None, file_runner: FileRunner | None) -> dict:
    """The pending spot, against the cited file itself in the kept boundary image (astra
    build r1 #1): pass iff some complete line of that file equals the cited line (both without
    a line-number prefix and surrounding space); unresolved, to the card, when the file has no
    such line but identifiers from it, or when the lookup failed; fail (code-verified
    absence) when no spot is cited, the file isn't in the workspace, or nothing of the line is
    in it."""
    path, raw = (d.get("pending_path") or "").strip(), d.get("pending_quote") or ""
    quote = pending_quote(raw)
    ev = {"path": path or None, "quote": raw, "searched": quote, "why": d.get("pending_reason")}
    if not path or len(quote) < PENDING_MIN_CHARS or "\n" in quote:
        return _check("fail", "no_pending_spot", **ev)
    if file_runner is None or not image:
        return _check("unresolved", "no_file_runner" if file_runner is None else "no_image",
                      **ev)
    got = file_runner(image, path.lstrip("./") if path.startswith("./") else path)
    ev["image"], ev["lookup"] = image, got.get("status")
    if got.get("status") == "missing":
        return _check("fail", "pending_file_not_in_workspace", **ev)
    if got.get("status") != "ok":
        return _check("unresolved", "pending_lookup_failed", error=got.get("error"), **ev)
    lines = got.get("text", "").splitlines()
    hits = [n for n, ln in enumerate(lines, 1) if pending_quote(ln) == quote]
    if hits:
        ev["found"] = [{"line": n, "text": lines[n - 1]} for n in hits[:3]]
        return _check("pass", None, **ev)
    inside = [n for n, ln in enumerate(lines, 1) if quote in pending_quote(ln)]
    if inside:  # part of a longer line (e.g. after a comment marker): Jacob's call
        ev["inside"] = [{"line": n, "text": lines[n - 1]} for n in inside[:3]]
        return _check("unresolved", "pending_line_inside_another", **ev)
    words = [set(_IDENT.findall(ln)) for ln in lines]
    near = {i: [n for n, w in enumerate(words, 1) if i in w]
            for i in sorted(set(_IDENT.findall(quote)), key=len, reverse=True)}
    near = {i: ns[:3] for i, ns in near.items() if ns}
    if near:
        ev["near"] = dict(list(near.items())[:5])  # evidence shown; every identifier searched
        return _check("unresolved", "pending_line_not_verbatim", **ev)
    return _check("fail", "pending_spot_not_found", **ev)


def applicability_v2(d: dict, pending: dict) -> dict:
    """Check 2 with the pending decision (Jacob, 24 Oct): the draft's applicability plus the
    pending spot. A pending fail is code-verified (it excludes); the draft's "no" is
    judgment-only (to the card); either unresolved makes it unresolved."""
    ev = {"why": d.get("applicability_reason"), "pending": pending}
    if pending["outcome"] == "fail":
        return dict(_check("fail", f"pending:{pending['reason']}", **ev), code_fail=True)
    drafted = DRAFT_MAP["applicable"][d["applicable"]]
    if drafted == "fail":
        return _check("fail", "draft:not_applicable", **ev)
    if "unresolved" in (drafted, pending["outcome"]):
        return _check("unresolved", pending.get("reason") if pending["outcome"] ==
                      "unresolved" else "draft:unsure", **ev)
    return _check("pass", None, **ev)


def draft_request_v2(bdir: Path, src: str, it: dict, view: dict, task: str) -> T.Req:
    r = draft_request(bdir, src, it, view, task)
    p = load_prompt(DRAFT_PROMPT_V2, PROMPT_DIR)
    v1 = load_prompt(DRAFT_PROMPT, PROMPT_DIR)
    user = r.user.replace(v1.sections["user"].split("<instructions>")[1],
                          p.sections["user"].split("<instructions>")[1])
    return T.Req(key=r.key.replace("draft|", "draft2|"), system=p.system, user=user,
                 schema=DRAFT_SCHEMA_V2, max_output_tokens=DRAFT_MAX_OUT, rid=r.rid,
                 source=r.source, private=True)


def screen_sha_v2() -> str:
    return hashlib.sha256(json.dumps({
        "prompts": {n: load_prompt(n, PROMPT_DIR).sha256 for n in
                    (SPLIT_PROMPT, DRAFT_PROMPT_V2, S.JUDGE_PROMPT)},
        "schema": DRAFT_SCHEMA_V2, "map": DRAFT_MAP, "code_fails": CODE_FAILS_V2,
        "pending": "applicability_v2+check_pending(file)",
        "pending_min_chars": PENDING_MIN_CHARS,
        "matched": "maintainer_reply+no_requirement_overlap+no_lead_in+no_explaining_sentence",
        "near_rule": "bounded+no_negation_either_side+min_words_always_opus",
        "chain": {"k>1": list(CHAIN_SOURCES)},
        "opus": {"prompt": load_prompt(OPUS_PROMPT, PROMPT_DIR).sha256, "model": OPUS_MODEL,
                 "schema": OPUS_SCHEMA, "types_in": ITEM_TYPES_IN, "near": [NEAR_SHARE,
                                                                            NEAR_MIN_WORDS]}}, sort_keys=True).encode()).hexdigest()


def _excluded_v2(checks: dict) -> str | None:
    for c in CODE_FAILS_V2:
        if (checks.get(c) or {}).get("outcome") == "fail":
            return c
    a = checks.get("applicability") or {}
    if a.get("outcome") == "fail" and a.get("code_fail"):
        return "applicability:pending"
    return None


# ------------------------------------------------------------------------------------------
# B and C (Jacob and the main session, 24 Oct): a cheap near-verbatim rule, then an Opus 5.5
# screen on the Max plan before any card. Haiku's grade must be "dropped" or unclear (unclear
# goes to Opus, never straight to a card); the resumed input's closest line must share fewer
# than NEAR_SHARE of the quote's key words (with at least NEAR_MIN_WORDS); Opus must say the
# requirement is lost and still applies; and the item must be a process rule (how to work,
# git and commit rules, files and tests not to touch, ask-before, scope, permissions) or a
# decision about work not yet built, not already in the workspace. Every Opus verdict is
# logged (fork_v2/opus_screen.jsonl), rejections included, for Haiku's precision against it.
# ------------------------------------------------------------------------------------------

OPUS_PROMPT = "interact_opus_screen"
OPUS_MODEL = "claude-opus-5-5"
OPUS_PURPOSE = "s2_interact_opus_screen"
OPUS_MAX_OUT = 3000
OPUS_LOG = "opus_screen.jsonl"
NEAR_SHARE, NEAR_MIN_WORDS = 0.8, 3
ITEM_TYPES_IN = ("process_rule", "unbuilt_decision")
# Sequential compactions can't mix methods (Jacob, 24 Oct): a replay at k>1 is another
# harness's prompt on top of pi's earlier summaries, a different thing from that method
# compacting repeatedly. Fork sources at k=1: every source; at k>1: the run's own summary only
# (live; a native arm's own summary when native runs are forked).
CHAIN_SOURCES = ("live",)
CODE_EXCERPT_LINES, CODE_EXCERPT_CHARS = 60, 12_000
OPUS_SCHEMA = T._obj({
    "retention": _S(("kept", "paraphrased", "lost")), "retention_reason": _S(),
    "applicable": _S(("yes", "no")), "applicable_reason": _S(),
    "item_type": _S(("process_rule", "unbuilt_decision", "built_detail", "other")),
    "type_reason": _S(),
    "in_workspace": _S(("yes", "no", "unclear")), "workspace_reason": _S()})
_WORD = re.compile(r"[A-Za-z0-9_]{3,}")
_STOP = {"the", "and", "for", "that", "this", "with", "not", "are", "but", "should", "when",
         "from", "its", "into", "than", "then", "also", "any", "all", "can", "has", "have",
         "was", "were", "will", "would", "you", "your", "our", "they", "them", "there", "what"}


def key_words(text: str | None) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")} - _STOP


def resumed_text(view: dict) -> str:
    """Everything the agent resumes with, as one text (the summary and every kept message)."""
    return "\n\n".join(f"[message {i}: {m.get('role')}]\n{S.message_text(m)}"
                        for i, m in enumerate(view.get("post") or []))


_NEG = re.compile(r"\b(?:not|never|no|without|avoid|don't|dont|doesn't|isn't|can't|cannot|"
                  r"won't|shouldn't|mustn't|nor)\b|n't\b", re.IGNORECASE)


def _negated(text: str) -> bool:
    return bool(_NEG.search((text or "").replace("’", "'")))


def _bounded_spans(quote: str, text: str) -> list[tuple[int, int]]:
    """`_spans` at word boundaries: a quote ending in a word character can't match inside a
    longer identifier (`foo` is not in `foobar`)."""
    toks = (quote or "").split()
    if not toks:
        return []
    rx = (r"(?<![\w])" if re.match(r"\w", toks[0]) else "") + r"\s+".join(
        re.escape(t) for t in toks) + (r"(?![\w])" if re.search(r"\w$", toks[-1]) else "")
    return [(m.start(), m.end()) for m in re.finditer(rx, text or "")]


def near_verbatim(text: str, quote: str | None) -> dict:
    """{verbatim, shared, of, negated, nearly}. `verbatim`: the quote at word boundaries in a
    line with no negation; `shared`: the most of its key words one such line holds. `nearly`
    (excluded before Opus) only for a quote with no negation and at least NEAR_MIN_WORDS key
    words, verbatim or sharing >= NEAR_SHARE of them; anything else goes to Opus (astra
    opus-screen r1 #3, r2 #3)."""
    q = quote or ""
    words, neg = key_words(q), _negated(q)
    # a negation on either side makes the match ambiguous (its scope may differ), so only
    # negation-free lines count, and a negated quote is never near-verbatim: it goes to Opus
    # (astra opus-screen r2 #3)
    plain = [ln for ln in (text or "").splitlines() if not _negated(ln)]
    verbatim = not neg and any(_bounded_spans(q, ln) for ln in plain)
    shared = 0 if neg else max((len(words & key_words(ln)) for ln in plain), default=0)
    nearly = (not neg and len(words) >= NEAR_MIN_WORDS
              and (verbatim or shared >= NEAR_SHARE * len(words)))
    return {"verbatim": verbatim, "shared": shared, "of": len(words), "negated": neg,
            "nearly": nearly}


def check_near(view: dict, it: dict) -> dict:
    ev = near_verbatim(resumed_text(view), it["quote"])
    return (_check("fail", "retention:nearly_verbatim", **ev) if ev["nearly"]
            else _check("pass", None, **ev))


def _code_excerpt(pend: dict | None, image: str | None, file_runner) -> str:
    ev = (pend or {}).get("evidence") or {}
    path = ev.get("path")
    if not path or not image or file_runner is None:
        return "(no file could be read)"
    try:
        r = file_runner(image, path)
    except Exception as e:  # noqa: BLE001 - shown to Opus as missing
        return f"(could not read {path}: {type(e).__name__})"
    if r.get("status") != "ok":
        return f"(could not read {path}: {r.get('status')})"
    lines = (r.get("text") or "").splitlines()
    at = ((ev.get("found") or [{}])[0].get("line") or 1) - 1
    lo, hi = max(0, at - CODE_EXCERPT_LINES), min(len(lines), at + CODE_EXCERPT_LINES)
    body = "\n".join(f"{n + 1:6}\t{lines[n]}" for n in range(lo, hi))
    return f"{path} (lines {lo + 1}-{hi} of {len(lines)}):\n{body}"[:CODE_EXCERPT_CHARS]


def opus_request(bdir: Path, src: str, it: dict, view: dict, c: dict, task: str,
                 code: str) -> T.Req:
    p = load_prompt(OPUS_PROMPT, PROMPT_DIR)
    reply = next((r for r in _read_or_none(Path(bdir) / "boundary" / "sim_replies.json") or []
                  if r.get("reply_id") == it.get("source_reply_id")), {})
    ch = c.get("checks") or {}
    pend = ((ch.get("applicability") or {}).get("evidence") or {}).get("pending") or {}
    pe = pend.get("evidence") or {}
    rev = (ch.get("recoverability") or {}).get("evidence") or {}
    # raw evidence only: no upstream verdict (the grade, the drafter's conclusions) is shown,
    # so Opus's call is its own (astra opus-screen r1 #4)
    summary = {
        "workspace_search": {"searched": rev.get("searched"),
                             "files_holding_whole_lines": rev.get("line_hits"),
                             "files_holding_fragments": rev.get("fragment_hits"),
                             "files_holding_search_strings": rev.get("search_hits")},
        "cited_code_line": {"path": pe.get("path"), "line": pe.get("quote"),
                            "lines_found_in_file": pe.get("found")}}
    user = _fill(p.sections["user"], task=task,
                 requirement=json.dumps({"text": it["text"], "quote": it["quote"]},
                                        ensure_ascii=False),
                 reply=reply.get("reply") or "", resumed=resumed_text(view),
                 checks=json.dumps(summary, ensure_ascii=False, indent=1), code=code)
    rid = f"interact:{Path(bdir).parent.name}:b{boundary_index(bdir)}"
    return T.Req(key=f"opus|{rid}|{cand_id(src, it['req_id'])}", system=p.system, user=user,
                 schema=OPUS_SCHEMA, max_output_tokens=OPUS_MAX_OUT, rid=rid,
                 source="deepswe", private=True)


def opus_passed(c: dict) -> bool:
    """The candidate holds a passing Opus verdict."""
    return ((c.get("checks") or {}).get("opus") or {}).get("outcome") == "pass"


def opus_gate(v: dict) -> str | None:
    """None when the candidate goes forward, else why not."""
    if v["retention"] != "lost":
        return f"opus:retention_{v['retention']}"
    if v["applicable"] != "yes":
        return "opus:not_applicable"
    if v["item_type"] not in ITEM_TYPES_IN:
        return f"type:{v['item_type']}"
    if v["item_type"] == "unbuilt_decision" and v["in_workspace"] != "no":
        return ("type:already_in_workspace" if v["in_workspace"] == "yes"
                else "type:workspace_unclear")
    return None


_OPUS_THREAD_LOCK = threading.Lock()
OPUS_LOCK_FILE = C.REPO_ROOT / "logs" / "s2" / ".opus_max.lock"


@contextlib.contextmanager
def _opus_lock():
    """One Opus call at a time across every thread and process of this checkout: Max takes no
    overlapping calls (astra opus-screen r1)."""
    OPUS_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _OPUS_THREAD_LOCK, open(OPUS_LOCK_FILE, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def opus_screen(bdir: Path, src: str, it: dict, view: dict, c: dict, task: str, *,
                backend, image: str | None, file_runner) -> dict:
    """The Opus check for one candidate (the section comment), logged whatever it says. A
    failed call raises: the screen fails and is retried, never cards without the verdict."""
    pend = ((c.get("checks") or {}).get("applicability") or {}).get("evidence", {}).get(
        "pending")
    req = opus_request(bdir, src, it, view, c, task, _code_excerpt(pend, image, file_runner))
    with _opus_lock():
        r = backend.run([req], purpose=OPUS_PURPOSE).get(req.key)
    if r is None or r.error or not isinstance(r.data, dict):
        raise RuntimeError(f"opus screen failed for {c['cand_id']}: "
                           f"{r.error if r else 'no_response'}")
    gate = opus_gate(r.data)
    out = (_check("pass", None, verdict=r.data, model=r.model) if gate is None else
           _check("fail", gate, verdict=r.data, model=r.model))
    row = {"utc": _utc(), "cand_id": c["cand_id"], "source_policy": src,
           "req_id": it["req_id"], "requirement": it["text"],
           "haiku_grade": ((c.get("checks") or {}).get("retention") or {}).get(
               "evidence", {}).get("grade"),
           "near": ((c.get("checks") or {}).get("near_verbatim") or {}).get("evidence"),
           "boundary": Path(bdir).name, "run": Path(bdir).parent.name,
           "verdict": r.data, "gate": gate, "model": r.model, "screen_sha256": screen_sha_v2()}
    v2_dir(bdir).mkdir(parents=True, exist_ok=True)
    with open(v2_dir(bdir) / OPUS_LOG, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out


def screen_v2(bdir: Path, *, backend, grep_runner: GrepRunner | None = None,
              ref_runner: S.Runner | None = None, file_runner: FileRunner | None = None,
              rejected: Iterable[str] = (), sources: Iterable[str] = ARM_SOURCES,
              task: str | None = None, bundle: bool = True, max_bundle: int = 1,
              opus_backend) -> dict:
    """fork_v2/candidates.json. Candidates in `candidates_order_v2`; retention is free and is
    recorded for all; the others run only until the first candidate that isn't excluded by a
    code-verified fail (CODE_FAILS_V2, pending included) and isn't rejected: that one is
    `carded`. With `bundle`, later candidates of the same source are screened too and up to
    `max_bundle` that qualify become `bundle` (the per-boundary card cap counts them); every
    other candidate is `not_reached`. Each candidate carries its ledger status (new /
    inherited; inherited losses are eligible). The pending spot is checked in the cited file
    (`file_runner`) and is part of check 2 (`applicability_v2`). B and C (the section comment
    above): the near-verbatim rule right after retention, and the Opus screen (`opus_backend`,
    required) last, before a candidate is carded or bundled."""
    b = Path(bdir) / "boundary"
    k = boundary_index(bdir)
    rq = _read(b / "requirements.json")
    detail = _read_or_none(v2_dir(bdir) / "grades_detail.json") or {}
    ledger = (_read_or_none(v2_dir(bdir) / "ledger.json") or {}).get("sources") or {}
    prev_file = _read_or_none(v2_dir(bdir) / "candidates.json") or {}
    # drafts and checks are reused only from a screen under the same rules (astra
    # matched-override r1 #3): a screen with another screen_sha256 starts fresh
    old = ({c["cand_id"]: c for c in prev_file.get("candidates", [])}
           if prev_file.get("screen_sha256") == screen_sha_v2() else {})
    rejected = set(rejected)
    image = ((_read_or_none(Path(bdir) / "run.json") or {}).get("task") or {}).get("image")
    pre = _read(b / "pre_messages.json")
    views: dict[str, dict] = {}
    out_c: list[dict] = []
    carded, carded_src, bundled = None, None, []
    for src, it in candidates_order_v2(rq, sources):
        cid = cand_id(src, it["req_id"])
        prev = old.get(cid) or {}
        checks = dict(prev.get("checks") or {})
        c = {"cand_id": cid, "source_policy": src, "req_id": it["req_id"],
             "source_reply_id": it["source_reply_id"], "position": it["position"],
             "repair_text": it["quote"], "text": it["text"], "checks": checks,
             "loss": (ledger.get(src) or {}).get(it["req_id"]),
             "inherited": ((ledger.get(src) or {}).get(it["req_id"]) or {}).get(
                 "status") == "inherited",
             "draft": prev.get("draft"), "act_spec": prev.get("act_spec"), "disposition": None}
        checks["retention"] = check_retention(src, it, detail)
        out_c.append(c)
        if k > 1 and src not in CHAIN_SOURCES:
            c["disposition"] = "excluded:source:mixed_chain"
            continue
        if carded is not None and (not bundle or src != carded_src
                                   or len(bundled) >= max_bundle):
            c["disposition"] = "not_reached"
            continue
        if _excluded_v2(checks):
            c["disposition"] = f"excluded:{_excluded_v2(checks)}"
            continue
        if src not in views:
            views[src] = S.source_view(bdir, src)
        checks["near_verbatim"] = check_near(views[src], it)
        if checks["near_verbatim"]["outcome"] == "fail":
            c["disposition"] = "excluded:retention:nearly_verbatim"
            continue
        if "recoverability" not in checks:
            checks["recoverability"] = check_recoverability(it, image, grep_runner)
        if _excluded_v2(checks):
            c["disposition"] = f"excluded:{_excluded_v2(checks)}"
            continue
        if src not in views:
            views[src] = S.source_view(bdir, src)
        if c["draft"] is None:
            task = task if task is not None else task_text(bdir)
            req = draft_request_v2(bdir, src, it, views[src], task)
            r = backend.run([req], purpose=DRAFT_PURPOSE).get(req.key)
            if r is None or r.error or not isinstance(r.data, dict):
                c["draft"] = {"error": r.error if r else "no_response"}
            else:
                c["draft"] = dict(r.data, model=r.model)
        d = c["draft"]
        if "error" in d:
            for name in ("applicability", "decision", "design", "reference"):
                checks.setdefault(name, _check("unresolved", "draft_failed", error=d["error"]))
        else:
            c["act_spec"] = spec = spec_from_draft(d)
            pend = (prev.get("checks") or {}).get("applicability", {}).get(
                "evidence", {}).get("pending") or check_pending(d, image, file_runner)
            checks["applicability"] = applicability_v2(d, pend)
            dec = DRAFT_MAP["decision_observable"][d["decision_observable"]]
            checks["decision"] = _check(dec, None, decision=d["decision"])
            checks["design"] = check_design(it, d, spec, views[src], pre,
                                            maintainer=maintainer_context(bdir))
            rec = checks["recoverability"]
            if d["search_strings"] and not rec["evidence"].get("search_after_draft"):
                rec = check_recoverability(it, image, grep_runner, d["search_strings"])
                rec["evidence"]["search_after_draft"] = True
                checks["recoverability"] = rec
            if "reference" not in checks and not _excluded_v2(checks):
                checks["reference"] = S.check_reference(spec, Path(bdir).parent, k,
                                                        runner=ref_runner)
        why = _excluded_v2(checks)
        # Opus runs even when the draft failed: no candidate is carded without its verdict
        if not why and cid not in rejected:
            if "opus" not in checks:
                task = task if task is not None else task_text(bdir)
                checks["opus"] = opus_screen(bdir, src, it, views[src], c, task,
                                             backend=opus_backend, image=image,
                                             file_runner=file_runner)
            why = checks["opus"].get("reason")
        if why:
            c["disposition"] = f"excluded:{why}"
        elif cid in rejected:
            c["disposition"] = "rejected"
        elif carded is None:
            c["disposition"] = "carded"
            carded, carded_src = cid, src
        else:
            c["disposition"] = "bundle"
            bundled.append(cid)
    out = {"boundary_index": k, "created_utc": _utc(), "screen_sha256": screen_sha_v2(),
           "draft_prompt_sha256": load_prompt(DRAFT_PROMPT_V2, PROMPT_DIR).sha256,
           "draft_map": DRAFT_MAP, "code_fails": list(CODE_FAILS_V2), "sources": list(sources),
           "rejected": sorted(rejected), "carded": carded, "bundle": bundled,
           "candidates": out_c}
    S.write_json(v2_dir(bdir) / "candidates.json", out)
    return out
