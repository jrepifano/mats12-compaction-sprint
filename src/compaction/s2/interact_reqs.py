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

import hashlib
import json
import re
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
            overlay = SL.second_look(
                self.backend, firsts, [], load_rec=lambda rid: recs[rid],
                summary_of=lambda row, rec: views[row["arm"]]["summary"], purpose=self.purpose)
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


def check_design(it: dict, d: dict, spec: dict, view: dict, pre: list[dict]) -> dict:
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
