"""Unit B: the requirement split, eligibility, grading and the candidate screen
(src/compaction/s2/interact_reqs.py), on a synthetic two-boundary run with stub backends."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

import pytest

from compaction import s1_types as T
from compaction.s2 import interact_reqs as R
from compaction.s2 import interact_score as S
from compaction.s2 import replays

TASK = ("Add conflict handling to the API. Conflicting writes must return HTTP 409 with the "
        "existing record's id. Never change the public signature of save_record.")


def _w(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def _ask(i: int, q: str) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{i}", "type": "function", "function": {
                "name": "ask_user", "arguments": json.dumps({"question": q})}}]}


def _tool(i: int, text: str) -> dict:
    return {"role": "tool", "tool_call_id": f"c{i}", "content": text}


REPLIES = {
    "r1": ("01_plan", "plan ok? ", "sure, plan is fine"),
    "r2": ("02_implement", "review?", "Conflicts should give a 409 back please."),
    "r3": ("02_implement", "review again?",
           ("Still need the 409 on conflicting writes. Also keep save_record's signature as is. "
            "And add a docstring, I like those.")),
    "r4": ("02_implement", "done?", "Include the existing record id in the 409 body."),
    "r5": ("02_implement", "ok now?", "Again: don't touch save_record's signature."),
    "r6": ("02_implement", "ship?", "Log conflicts at warning level too."),
}


def _reply(rid: str, pre_index) -> dict:
    step, q, a = REPLIES[rid]
    return {"reply_id": rid, "call_index": int(rid[1:]), "step": step, "pre_index": pre_index,
            "question": q, "reply": a}


def _boundary(run: Path, k: int, pre: list[dict], kept: list[int], replies: list[dict],
              sim_calls: int, *, write_boundary_json: bool = True) -> Path:
    bdir = run / f"b{k}"
    b = bdir / "boundary"
    _w(b / "pre_messages.json", pre)
    post = [pre[0], {"role": "user", "content": "SUMMARY"}] + [pre[i] for i in kept]
    _w(b / "compaction.json", {"summary": f"summary at b{k}: the agent is adding conflicts.",
                               "summary_index": 1, "kept_indices": kept, "post_messages": post,
                               "info": {"request_sha256": f"req-b{k}"}})
    _w(b / "post_messages.json", post)
    _w(b / "sim_replies.json", replies)
    if write_boundary_json:
        _w(b / "boundary.json", {"boundary_index": k, "tail_start_index": min(kept),
                                 "sim_calls": sim_calls})
    patch = run.parent / "solution.patch"
    patch.write_text("diff --git a/api.py b/api.py\n")
    _w(bdir / "run.json", {"task": {"task_id": "demo", "image": f"kept:b{k}",
                                    "solution_patch": str(patch)}})
    return bdir


def _b1_pre() -> list[dict]:
    pre = [{"role": "system", "content": "sys"},
           {"role": "user", "content": "# AGENTS.md instructions for /app\nstep"},
           {"role": "assistant", "content": "start"},
           _ask(2, REPLIES["r2"][1]), _tool(2, REPLIES["r2"][2]),
           _ask(3, REPLIES["r3"][1]), _tool(3, REPLIES["r3"][2]),
           {"role": "assistant", "content": "working"},
           _ask(4, REPLIES["r4"][1]), _tool(4, REPLIES["r4"][2]),
           {"role": "assistant", "content": "more"}]
    return pre


def _b2_pre() -> list[dict]:
    return [{"role": "system", "content": "sys"},
            {"role": "user", "content": "SUMMARY of b1"},
            {"role": "assistant", "content": "x"},
            _tool(4, REPLIES["r4"][2]),
            {"role": "assistant", "content": "y"},
            _ask(5, REPLIES["r5"][1]), _tool(5, REPLIES["r5"][2]),
            {"role": "assistant", "content": "z"},
            _ask(6, REPLIES["r6"][1]), _tool(6, REPLIES["r6"][2])]


@pytest.fixture()
def run(tmp_path):
    run = tmp_path / "demo-deepseek-a1"
    _w(run / "run.json", {"task": {"task_id": "demo"}})
    # b1: tail starts at 8 (r4's ask and reply are in pi's verbatim tail)
    _boundary(run, 1, _b1_pre(), [8, 9, 10],
              [_reply("r1", None), _reply("r2", 4), _reply("r3", 6), _reply("r4", 9)], 4)
    # b2: r4 came in b1's tail, so it is verbatim here but inherited; tail starts at 7
    _boundary(run, 2, _b2_pre(), [7, 8, 9],
              [_reply("r1", None), _reply("r2", None), _reply("r3", None), _reply("r4", 3),
               _reply("r5", 6), _reply("r6", 9)], 6)
    return run


def _req(local, reply, text, quote, same_as=None, grounded=True, gq=None):
    return {"local_id": local, "reply_id": reply, "text": text, "quote": quote,
            "same_as": same_as, "grounded": grounded, "grounding_quote": gq}


SPLIT_B1 = [
    _req("n1", "r2", "Return 409 on conflicts.", "Conflicts should give a 409 back please.",
         gq="Conflicting writes must return HTTP 409"),
    _req("n2", "r3", "Return 409 on conflicting writes.", "Still need the 409 on conflicting "
         "writes.", same_as="n1", gq="Conflicting writes must return HTTP 409"),
    _req("n3", "r3", "Keep save_record's signature.", "keep save_record's signature as is.",
         gq="Never change the public signature of save_record."),
    _req("n4", "r3", "Add a docstring.", "And add a docstring, I like those.", grounded=False),
    _req("n5", "r4", "Include the existing id in the 409.", "Include the existing record id "
         "in the 409 body.", gq="with the existing record's id"),
    _req("n6", "r1", "Plan is fine.", "sure, plan is fine", gq="Add conflict handling"),
    _req("n7", "r2", "Made-up quote.", "this text is not in the reply at all",
         gq="Add conflict handling to the API."),
]
SPLIT_B2 = [
    _req("n1", "r5", "Don't touch save_record's signature.",
         "don't touch save_record's signature.", same_as="b1-q3",
         gq="Never change the public signature of save_record."),
    _req("n2", "r6", "Log conflicts at warning.", "Log conflicts at warning level too.",
         grounded=True, gq="this grounding quote is not in the task text"),
]


class StubBackend:
    """Answers by request-key prefix; records every request."""
    name, model, family = "stub", "stub-haiku", "anthropic"
    context_tokens, bytes_per_token = 200_000, 3.0

    def __init__(self, answer):
        self.answer = answer
        self.seen: list[T.Req] = []

    def run(self, reqs, *, purpose):
        out = {}
        for r in reqs:
            self.seen.append(r)
            data = self.answer(r)
            out[r.key] = T.Resp(r.key, data, json.dumps(data), None, model="stub-haiku")
        return out


def _splitter(by_boundary):
    def answer(req):
        k = int(re.search(r":b(\d+)$", req.key).group(1))
        return {"requirements": by_boundary[k]}
    return answer


def _split_both(run):
    be = StubBackend(_splitter({1: SPLIT_B1, 2: SPLIT_B2}))
    r1 = R.split(run / "b1", backend=be, task=TASK)
    R.eligibility(run / "b1")
    return be, r1


def _items(rq):
    return {it["req_id"]: it for it in rq["items"]}


def test_split_fresh_rule_at_first_boundary(run):
    be, rq = _split_both(run)
    it = _items(rq)
    assert rq["prompt_sha256"] == R.prompt_shas()[R.SPLIT_PROMPT]
    assert rq["tail_start_index"] == 8 and rq["previous_sim_calls"] is None
    # only 02_implement replies are sent to the splitter
    sent = be.seen[0].user
    assert '"reply_id": "r2"' in sent and '"reply_id": "r1"' not in sent
    # a repeat counts once, under its latest source reply
    assert it["b1-q1"]["duplicate_of"] == "b1-q2" and it["b1-q2"]["duplicate_of"] is None
    assert it["b1-q2"]["fresh"] and not it["b1-q1"]["fresh"]
    assert it["b1-q3"]["fresh"]
    # ungrounded: not fresh
    assert not it["b1-q4"]["grounded"] and not it["b1-q4"]["fresh"]
    # in pi's tail: flagged, not fresh
    assert it["b1-q5"]["in_tail"] and not it["b1-q5"]["fresh"]
    # 01_plan reply and a quote not in its reply: invalid
    assert it["b1-q6"]["invalid_reason"] == "unknown_reply" and not it["b1-q6"]["fresh"]
    assert it["b1-q7"]["invalid_reason"] == "quote_not_in_reply"
    # positions are ranks within the reply
    assert (it["b1-q2"]["position"], it["b1-q3"]["position"], it["b1-q4"]["position"]) == (0, 1, 2)
    el = json.loads((run / "b1" / "boundary" / "eligibility.json").read_text())
    assert el == el | {"eligible": True, "n_fresh": 2, "fresh_ids": ["b1-q2", "b1-q3"],
                       "n_in_tail": 1, "n_inherited": 0, "n_ungrounded": 1}


def test_split_second_boundary_inherits_and_fills_first_boundary(run):
    be, _ = _split_both(run)
    S.write_json(run / "b1" / "boundary" / "grades.json",
                 {"live": {"b1-q2": "dropped", "b1-q3": "kept"}})
    rq = R.split(run / "b2", backend=be, task=TASK)
    it = _items(rq)
    sent = be.seen[-1].user
    # each reply is split once: b2 sends only r5 and r6, with b1's requirements as context
    assert '"reply_id": "r5"' in sent and '"reply_id": "r3"' not in sent
    assert '"req_id": "b1-q3"' in sent and '"req_id": "b1-q1"' not in sent  # canonical only
    # replies from before the previous boundary are not fresh, even if verbatim (r4, in b1's tail)
    assert it["b1-q5"]["source_pre_index"] == 3 and not it["b1-q5"]["fresh"]
    assert not it["b1-q5"]["in_tail"]
    # a repeat of b1-q3 in b2: b1-q3 becomes its duplicate; the b2 item is fresh
    assert it["b1-q3"]["duplicate_of"] == "b2-q1" and it["b2-q1"]["fresh"]
    # earlier groups stay linked
    assert it["b1-q1"]["duplicate_of"] == "b1-q2"
    # grounding quote not found in the task text -> ungrounded, and r6 is in the tail anyway
    assert not it["b2-q2"]["grounded"] and it["b2-q2"]["in_tail"]
    # first disappearance carried from b1's live grades
    assert it["b1-q2"]["first_boundary"] == 1 and it["b2-q1"]["first_boundary"] is None
    el = R.eligibility(run / "b2")
    assert el["eligible"] is False and el["n_fresh"] == 1 and el["fresh_ids"] == ["b2-q1"]
    assert el["n_inherited"] == 3  # b1-q2, b1-q4, b1-q5 (b1-q3 is now a duplicate)
    assert el["n_in_tail"] == 1 and el["n_ungrounded"] == 1
    assert R.eligibility(run / "b2", min_fresh=1)["eligible"] is True


def test_split_before_boundary_json_uses_fields_or_compaction(tmp_path):
    run = tmp_path / "r"
    _w(run / "run.json", {"task": {"task_id": "demo"}})
    bdir = _boundary(run, 1, _b1_pre(), [8, 9, 10],
                     [_reply("r2", 4), _reply("r3", 6), _reply("r4", 9)], 4,
                     write_boundary_json=False)
    assert R.boundary_fields(bdir)["tail_start_index"] == 8  # from compaction.json
    assert R.boundary_fields(bdir, {"tail_start_index": 5})["tail_start_index"] == 5
    rq = R.split(bdir, backend=StubBackend(_splitter({1: SPLIT_B1})), task=TASK,
                 fields={"tail_start_index": 5})
    it = _items(rq)
    assert not it["b1-q3"]["fresh"] and it["b1-q3"]["in_tail"]  # r3 at 6 >= 5


def test_split_needs_previous_boundary(run):
    with pytest.raises(FileNotFoundError):
        R.split(run / "b2", backend=StubBackend(_splitter({2: SPLIT_B2})), task=TASK)


def test_task_text_checks_spec_sha(tmp_path):
    spec = tmp_path / "instruction.md"
    spec.write_text(TASK)
    run = tmp_path / "r"
    good = hashlib.sha256(TASK.encode()).hexdigest()
    _w(run / "run.json", {"task": {"task_id": "demo", "spec_path": str(spec),
                                   "spec_sha256": good}})
    assert R.task_text(run / "b1") == TASK
    _w(run / "run.json", {"task": {"task_id": "demo", "spec_path": str(spec),
                                   "spec_sha256": "0" * 64}})
    with pytest.raises(ValueError):
        R.task_text(run / "b1")


# ------------------------------------------------------------------------------------------
# Grading
# ------------------------------------------------------------------------------------------


def _replays(bdir: Path, summaries: dict[str, str], kept_extra: dict[str, list[dict]] | None = None):
    pre = json.loads((bdir / "boundary" / "pre_messages.json").read_text())
    for p, summ in summaries.items():
        extra = (kept_extra or {}).get(p, [])
        post = [pre[0], pre[1], {"role": "user", "content": summ}, *extra]
        _w(bdir / "boundary" / "replays" / p / "post_messages.json", post)
        _w(bdir / "boundary" / "replays" / p / "replay.json",
           {"policy": p, "summary": summ, "summary_index": 2, "kept_indices": [],
            "fingerprint": f"fp-{p}", "post_sha256": replays.canon_sha256(post)})


def test_grade_sources_table_with_stub_grader(run):
    _split_both(run)
    bdir = run / "b1"
    _replays(bdir, {p: f"{p} summary" for p in S.REPLAY_POLICIES})
    seen = {}

    def grader(items, views):
        seen["ids"] = [it["req_id"] for it in items]
        seen["sources"] = sorted(views)
        return {s: {it["req_id"]: {"grade": "kept" if s == "pi" else "dropped"} for it in items}
                for s in views}

    g = R.grade_sources(bdir, grader=grader)
    assert seen["sources"] == sorted(S.GRADE_SOURCES)
    # one list: valid canonical grounded 02_implement items verbatim in this input (b1-q4 is
    # ungrounded: tracked, not graded; astra build r1 #13)
    assert seen["ids"] == ["b1-q2", "b1-q3", "b1-q5"]
    assert set(g) == set(S.GRADE_SOURCES) and g["pi"]["b1-q2"] == "kept"
    assert g["live"]["b1-q3"] == "dropped"
    on_disk = json.loads((bdir / "boundary" / "grades.json").read_text())
    assert on_disk == g
    rq = json.loads((bdir / "boundary" / "requirements.json").read_text())
    assert _items(rq)["b1-q2"]["first_boundary"] == 1
    # grading one more source later merges
    g2 = R.grade_sources(bdir, ["pi"], grader=lambda items, views: {
        "pi": {it["req_id"]: {"grade": "unclear"} for it in items}})
    assert g2["pi"]["b1-q2"] == "unclear" and g2["live"]["b1-q2"] == "dropped"


def _s1_answer(req: T.Req) -> dict:
    """pass 1b: every item dropped except b1-q3 (kept, with a quote that is not in the summary
    -> unclear); second look: dropped."""
    if req.key.startswith("p1b|"):
        grades = []
        for line in req.user.splitlines():
            if line.startswith('{"id"'):
                d = json.loads(line)
                if "signature" in d["quote"]:  # b1-q3
                    grades.append({"item": d["id"], "verdict": "kept",
                                   "summary_quote": "not in the summary at all", "note": "x"})
                else:
                    grades.append({"item": d["id"], "verdict": "dropped",
                                   "summary_quote": None, "note": "x"})
        return {"grades": grades}
    if req.key.startswith("sl|item|"):
        return {"verdict": "dropped", "summary_quote": None, "searched": ["409"], "note": "x"}
    raise AssertionError(req.key)


def test_s1_grader_on_stored_post_contexts(run):
    _split_both(run)
    bdir = run / "b1"
    # codex keeps r3's reply verbatim: still present there
    _replays(bdir, {"codex_local": "codex summary"}, {"codex_local": [_tool(3, REPLIES["r3"][2])]})
    be = StubBackend(_s1_answer)
    g = R.grade_sources(bdir, ["live", "codex_local"], backend=be)
    assert g["live"]["b1-q2"] == "dropped" and g["codex_local"]["b1-q2"] == "kept"
    assert g["live"]["b1-q3"] == "unclear"  # summary quote not found
    assert "b1-q4" not in g["live"]  # ungrounded: not in the denominator
    # the second look ran only on dropped items that are not still present
    sl = [r for r in be.seen if r.key.startswith("sl|")]
    assert sl and all("tools" not in r.key for r in sl)
    assert len(sl) == 2  # dropped and not still present: live q2, codex_local q5 (q3 unclear)
    detail = json.loads((bdir / "boundary" / "grades_detail.json").read_text())
    assert detail["codex_local"]["b1-q2"]["still_present"] is True


# ------------------------------------------------------------------------------------------
# Screen
# ------------------------------------------------------------------------------------------

DRAFT = {
    "applicable": "yes", "applicability_reason": "not done yet",
    "decision": "how save_record is defined", "decision_observable": "yes",
    "act_kind": "diff_added", "act_pattern": r"def save_record\(.*force", "act_path": "*.py",
    "act_negate": False, "act_description": "changes save_record's signature",
    "decision_kind": "file_touched", "decision_pattern": None, "decision_path": "api.py",
    "reask_keywords": ["signature"],
    "matched_quote": None, "matched_rationale": "irrelevant",
    "search_strings": ["save_record"],
}


def test_screen_order_exclusions_card_and_round_two(run):
    _split_both(run)
    bdir = run / "b1"
    _replays(bdir, {p: f"{p} summary" for p in S.REPLAY_POLICIES})
    grades = {"live": {"b1-q2": "kept", "b1-q3": "dropped"},
              "cc_prompt": {"b1-q2": "dropped", "b1-q3": "dropped"}}
    R.grade_sources(bdir, ["live", "cc_prompt"], grader=lambda items, views: {
        s: {rid: {"grade": g, "summary_quote": "q" if g == "kept" else None}
            for rid, g in grades[s].items()} for s in views})

    q3_line = "keep save_record's signature as is."

    def grep(image, patterns):
        assert image == "kept:b1"
        return {p: {"files": ["PLAN.md"] if p == q3_line else
                    (["api.py"] if p == "save_record" else []), "lines": []} for p in patterns}

    drafts = []

    def answer(req):
        drafts.append(req.key)
        mq = "Conflicts should give a 409 back please." if "b1-q2" in req.key else None
        return dict(DRAFT, matched_quote=mq)

    refs = []

    def ref_runner(image, script):
        refs.append(image)
        return 0, "@@REFDIFF@@\ndiff --git a/api.py b/api.py\n+++ b/api.py\n+x = 1\n", ""

    be = StubBackend(answer)
    out = R.screen(bdir, backend=be, grep_runner=grep, ref_runner=ref_runner, task=TASK)
    disp = {c["cand_id"]: c["disposition"] for c in out["candidates"]}
    order = [c["cand_id"] for c in out["candidates"]]
    assert order[:4] == ["live.b1-q2", "live.b1-q3", "cc_prompt.b1-q2", "cc_prompt.b1-q3"]
    assert disp["live.b1-q2"] == "excluded:retention"
    assert disp["live.b1-q3"] == "excluded:recoverability"  # the quote's line is in PLAN.md
    assert disp["cc_prompt.b1-q2"] == "carded" and out["carded"] == "cc_prompt.b1-q2"
    assert disp["cc_prompt.b1-q3"] == "not_reached"
    assert all(disp[c] == "not_reached" for c in order[4:])
    # ungraded sources: retention unresolved, recorded even when not reached
    c = {c["cand_id"]: c for c in out["candidates"]}
    assert c["opencode.b1-q2"]["checks"]["retention"]["outcome"] == "unresolved"
    card = c["cc_prompt.b1-q2"]["checks"]
    assert card["recoverability"]["outcome"] == "unresolved"  # a search string hit: Jacob
    assert card["recoverability"]["evidence"]["search_hits"] == {"save_record": ["api.py"]}
    assert card["reference"]["outcome"] == "pass" and refs == ["kept:b1"]
    assert card["design"]["outcome"] == "pass"  # code checks only length and absence
    assert drafts == ["draft|interact:demo-deepseek-a1:b1|cc_prompt.b1-q2"]
    assert out["screen_sha256"] == R.screen_sha()
    # round 2: Jacob rejected the card; the next candidate in order is drafted, the first isn't
    out2 = R.screen(bdir, backend=be, grep_runner=grep, ref_runner=ref_runner, task=TASK,
                    rejected=["cc_prompt.b1-q2"])
    disp2 = {c["cand_id"]: c["disposition"] for c in out2["candidates"]}
    assert disp2["cc_prompt.b1-q2"] == "rejected"
    assert disp2["cc_prompt.b1-q3"] == "excluded:recoverability"
    # codex_local is ungraded: retention unresolved, so it goes to the card
    assert out2["carded"] == "codex_local.b1-q2"
    assert drafts == ["draft|interact:demo-deepseek-a1:b1|cc_prompt.b1-q2",
                      "draft|interact:demo-deepseek-a1:b1|codex_local.b1-q2"]


def test_check_design_matched_addition_rules(run):
    _split_both(run)
    bdir = run / "b1"
    pre = json.loads((bdir / "boundary" / "pre_messages.json").read_text())
    it = {"quote": "Conflicts should give a 409 back please."}
    view = S.source_view(bdir, "live")  # keeps pre[8:11], r4's reply among them
    spec = R.spec_from_draft(DRAFT)

    def probs(mq, rationale="irrelevant"):
        d = dict(DRAFT, matched_quote=mq, matched_rationale=rationale)
        return R.check_design(it, d, spec, view, pre)["evidence"]["problems"]

    assert probs("Still need the 409 on conflicting writes.") == []
    assert probs("Still need the 409") == ["matched_length_out_of_range"]
    assert probs("This sentence never appears in history!") == ["matched_not_in_history"]
    assert probs("Include the existing record id in the 409 body.") == [
        "matched_in_resumed_input"]
    assert probs(None) == ["no_matched_addition"]
    bad = dict(spec, act={"kind": "diff_added", "pattern": "(", "path": None})
    d = dict(DRAFT, matched_quote="Still need the 409 on conflicting writes.")
    assert R.check_design(it, d, bad, view, pre)["outcome"] == "unresolved"


def test_screen_reference_fail_excludes_and_judgment_fail_does_not(run):
    _split_both(run)
    bdir = run / "b1"
    R.grade_sources(bdir, ["live"], grader=lambda items, views: {
        "live": {it["req_id"]: {"grade": "dropped"} for it in items}})

    def grep(image, patterns):
        return {p: {"files": [], "lines": []} for p in patterns}

    def answer(req):
        return dict(DRAFT, applicable="no")  # judgment-only fail: goes to the card

    def ref_fail(image, script):
        return 0, ("@@REFDIFF@@\ndiff --git a/api.py b/api.py\n+++ b/api.py\n"
                   "+def save_record(a, force=False):\n"), ""

    out = R.screen(bdir, backend=StubBackend(answer), grep_runner=grep, ref_runner=ref_fail,
                   task=TASK, sources=["live"])
    disp = [c["disposition"] for c in out["candidates"]]
    assert disp == ["excluded:reference", "excluded:reference"]
    out = R.screen(bdir, backend=StubBackend(answer), grep_runner=grep, task=TASK,
                   sources=["live"], ref_runner=lambda i, s: (1, "", "patch failed"))
    # cached reference results are reused; a fresh boundary is needed to see "unresolved"
    assert out["carded"] is None
    (bdir / "boundary" / "candidates.json").unlink()
    out = R.screen(bdir, backend=StubBackend(answer), grep_runner=grep, task=TASK,
                   sources=["live"], ref_runner=lambda i, s: (1, "", "patch failed"))
    first = out["candidates"][0]
    assert first["disposition"] == "carded"
    assert first["checks"]["applicability"]["outcome"] == "fail"
    assert first["checks"]["reference"]["outcome"] == "unresolved"


def test_screen_draft_failure_goes_to_card(run):
    _split_both(run)
    bdir = run / "b1"
    R.grade_sources(bdir, ["live"], grader=lambda items, views: {
        "live": {it["req_id"]: {"grade": "dropped"} for it in items}})

    class Failing(StubBackend):
        def run(self, reqs, *, purpose):
            return {r.key: T.Resp(r.key, None, None, "limit") for r in reqs}

    out = R.screen(bdir, backend=Failing(None), grep_runner=None, task=TASK, sources=["live"])
    c = out["candidates"][0]
    assert c["disposition"] == "carded"
    assert {k: v["outcome"] for k, v in c["checks"].items()} == {
        "retention": "pass", "recoverability": "unresolved", "applicability": "unresolved",
        "decision": "unresolved", "design": "unresolved", "reference": "unresolved"}


def test_arm_sources_match_unit_a():
    assert S.ARM_SOURCES == replays.ARM_SOURCES
    assert set(S.REPLAY_POLICIES) == set(replays.RP.REPLAY_ARMS)


class StrictBackend(StubBackend):
    """Like claude_headless.MaxBackend: refuses duplicate keys within one batch, and a run()
    while another is still running."""

    def __init__(self, answer):
        super().__init__(answer)
        self.batches: list[list[str]] = []
        self._busy = threading.Lock()

    def run(self, reqs, *, purpose):
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("MaxBackend.run is already running")
        try:
            keys = [r.key for r in reqs]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate Req.key in batch")
            self.batches.append(keys)
            time.sleep(0.01)  # long enough for an overlapping caller to collide
            return super().run(reqs, purpose=purpose)
        finally:
            self._busy.release()


def _per_source_second_look(req):
    """pass 1b as `_s1_answer`; the second look keeps an item for cc_prompt (quoting its
    summary) and drops it for every other source, so a swapped overlay would show."""
    if req.key.startswith("sl|item|"):
        summary = dict(req.files).get("summary.txt", "")
        if summary == "cc summary":
            return {"verdict": "kept", "summary_quote": "cc summary", "searched": [], "note": "x"}
    return _s1_answer(req)


def test_second_look_one_batch_per_source(run):
    """testem b1, 24 Oct: the same requirement dropped by two sources made one second-look
    batch with duplicate "sl|item|<req>" keys. Batches go one per source, never overlapping, and
    each source keeps its own verdicts."""
    _split_both(run)
    bdir = run / "b1"
    _replays(bdir, {"cc_prompt": "cc summary"})  # keeps nothing: drops what live drops
    be = StrictBackend(_per_source_second_look)
    g = R.grade_sources(bdir, ["live", "cc_prompt"], backend=be)
    sl = [b for b in be.batches if b and b[0].startswith("sl|")]
    assert len(sl) == 2 and all("sl|item|b1-q2" in b for b in sl)  # one batch per source
    assert g["live"]["b1-q2"] == "dropped"  # live's second look agreed: dropped
    assert g["cc_prompt"]["b1-q2"] == "kept"  # cc_prompt's second look overturned it


# ------------------------------------------------------------------------------------------
# v2: the accumulated-loss screen
# ------------------------------------------------------------------------------------------

PENDING_LINE = "def save_record(rec):"
API_FILE = "import x\n\n    def save_record(rec):  \n        return store(rec)\n"
DRAFT_V2 = dict(DRAFT, matched_quote="Conflicts should give a 409 back please.",
                pending_path="api.py", pending_quote=PENDING_LINE,
                pending_reason="save_record still has no conflict check")


def _grader(table):
    return lambda items, views: {s: {it["req_id"]: {"grade": table[s].get(it["req_id"], "kept"),
                                                    "summary_quote": None}
                                     for it in items} for s in views}


def _grep_v2(image, patterns):
    return {p: {"files": [], "lines": []} for p in patterns}


def _files(image, path):
    return {"status": "ok", "text": API_FILE} if path == "api.py" else {"status": "missing"}


def _ref_pass(image, script):
    return 0, "@@REFDIFF@@\ndiff --git a/api.py b/api.py\n+++ b/api.py\n+x = 1\n", ""


def _v2_two_boundaries(run):
    be, _ = _split_both(run)
    R.split(run / "b2", backend=be, task=TASK)
    for k in (1, 2):
        _replays(run / f"b{k}", {"cc_prompt": "cc summary"})
    req_sha = hashlib.sha256((run / "b1" / "boundary" / "requirements.json").read_bytes())
    R.grade_sources_v2(run / "b1", ["live", "cc_prompt"], grader=_grader(
        {"live": {"b1-q2": "dropped", "b1-q3": "dropped"},
         "cc_prompt": {"b1-q2": "dropped", "b1-q3": "dropped", "b1-q5": "dropped"}}))
    R.loss_ledger(run / "b1")
    return req_sha


def _screen_b1(run, draft=None, **kw):
    return R.screen_v2(run / "b1", backend=StubBackend(lambda req: dict(draft or DRAFT_V2)),
                       grep_runner=_grep_v2, ref_runner=_ref_pass, file_runner=_files,
                       task=TASK, **{"sources": ["live", "cc_prompt"], **kw})


def test_v2_grades_every_requirement_so_far_and_marks_new_or_inherited(run):
    req_sha = _v2_two_boundaries(run)
    b1 = run / "b1" / "boundary"
    # the shared split is never rewritten, and v1's files are not touched
    assert hashlib.sha256((b1 / "requirements.json").read_bytes()).digest() == req_sha.digest()
    assert not (b1 / "grades.json").exists() and (b1 / "fork_v2" / "grades.json").exists()
    led1 = json.loads((b1 / "fork_v2" / "ledger.json").read_text())
    assert led1["counts"]["live"] == {"new": 2, "inherited": 0, "unknown": 0}
    # b2: carried items are graded too (b1-q2's reply r3 is no longer verbatim there)
    rq2 = json.loads((run / "b2" / "boundary" / "requirements.json").read_text())
    ids = [it["req_id"] for it in R.gradable_v2(rq2)]
    assert "b1-q2" in ids and "b1-q5" in ids and "b2-q1" in ids
    assert "b2-q2" not in ids  # its grounding quote isn't in the task text
    R.grade_sources_v2(run / "b2", ["live", "cc_prompt"], grader=_grader(
        {"live": {"b1-q2": "dropped", "b1-q5": "dropped", "b2-q1": "dropped"},
         "cc_prompt": {"b1-q2": "dropped"}}))
    led = R.loss_ledger(run / "b2")["sources"]
    assert led["live"]["b1-q2"]["status"] == "inherited"  # b1's live summary already lacked it
    assert led["live"]["b1-q5"]["status"] == "new"  # b1's live summary still carried it
    assert led["live"]["b2-q1"]["status"] == "new" and led["live"]["b2-q1"]["verbatim_here"]
    assert led["cc_prompt"]["b1-q2"]["status"] == "inherited"


def test_v2_screen_cards_the_first_with_a_pending_spot_and_bundles_its_source(run):
    _v2_two_boundaries(run)
    seen = []

    def answer(req):
        seen.append(req.key)
        assert req.schema is R.DRAFT_SCHEMA_V2 and "pending_path" in req.user
        return dict(DRAFT_V2)

    out = R.screen_v2(run / "b1", backend=StubBackend(answer), grep_runner=_grep_v2,
                      ref_runner=_ref_pass, file_runner=_files, task=TASK,
                      sources=["live", "cc_prompt"])
    disp = {c["cand_id"]: c["disposition"] for c in out["candidates"]}
    assert out["carded"] == "live.b1-q2" and out["bundle"] == ["live.b1-q3"]
    assert disp["live.b1-q5"] == "not_reached"  # the bundle is full (max_bundle=1)
    q5 = {c["cand_id"]: c for c in out["candidates"]}["live.b1-q5"]
    assert q5["checks"]["retention"]["outcome"] == "fail"  # live kept it (recorded anyway)
    assert all(v == "not_reached" for k, v in disp.items() if k.startswith("cc_prompt."))
    c = {c["cand_id"]: c for c in out["candidates"]}["live.b1-q2"]
    app = c["checks"]["applicability"]
    assert app["outcome"] == "pass" and app["evidence"]["pending"]["outcome"] == "pass"
    assert set(c["checks"]) == set(R.CHECKS)  # the pilot's six
    assert c["loss"]["status"] == "new" and c["inherited"] is False
    assert all(k.startswith("draft2|") for k in seen)
    assert out["screen_sha256"] == R.screen_sha_v2()
    # rejected: the next candidate, same source first (bundle capped at 1 by default)
    out2 = _screen_b1(run, rejected=["live.b1-q2"])
    assert out2["carded"] == "live.b1-q3" and out2["bundle"] == []


def test_v2_screen_excludes_a_candidate_without_a_pending_spot(run):
    _v2_two_boundaries(run)
    for bad in (dict(DRAFT_V2, pending_quote="a line that is not in the workspace"),
                dict(DRAFT_V2, pending_path="gone.py"),
                dict(DRAFT_V2, pending_path=None, pending_quote=None)):
        (run / "b1" / "boundary" / "fork_v2" / "candidates.json").unlink(missing_ok=True)
        out = _screen_b1(run, draft=bad, sources=["live"])
        disp = [c["disposition"] for c in out["candidates"]]
        assert disp[:2] == ["excluded:applicability:pending"] * 2 and out["carded"] is None


@pytest.fixture(scope="module")
def CARDS():
    import importlib
    import sys
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        return importlib.import_module("s2_interact_cards")
    finally:
        sys.path.pop(0)


def test_v2_cards_three_questions_freeze_and_bundle(run, tmp_path, CARDS):
    from compaction.s2 import interact_run as IR
    _v2_two_boundaries(run)
    IR.place_cards(run / "b1", _screen_b1(run))
    _w(run / "b1" / "boundary" / "boundary.json", {"boundary_index": 1, "calls_in_step": 40,
                                                   "tail_start_index": 8, "sim_calls": 4})
    spec = tmp_path / "instruction.md"
    spec.write_text(TASK)
    _w(run / "run.json", {"task": {"task_id": "demo", "spec_path": str(spec)}})
    html, key = CARDS.build_v2([(run, 1)])
    assert len(key["cards"]) == 2
    data = CARDS.RP.page_data(html)
    card = data["cards"][0]
    f = card["facts"]
    assert f["code"] == {"path": "api.py", "quote": PENDING_LINE}
    assert f["header"] == ("demo · compaction 1 · graded as dropped by the run's own summary "
                           "(pi)")
    assert f["sees"].startswith("The grader marked it lost: check the closest passage")
    assert "no mention" not in json.dumps(data["cards"])  # never an absence claim
    assert f["said"]["quote"] and f["act"] and f["chips"] == []
    assert "paragraph" not in card
    assert [c["facts"]["chips"] for c in data["cards"]][1] == ["second loss from the same summary"]
    assert card["nq"] == 3 and len(card["qev"]) == 3 and len(card["code_ev"]) == 3
    CARDS.leak_check(html, key)
    labels = {cid: {"c1": "yes", "c2": "yes", "c3": "yes", "v": e["content_sha256"][:16]}
              for cid, e in key["cards"].items()}
    out = CARDS.freeze_v2(labels, key, runner=_ref_pass)
    st = {r["cand_id"]: r["status"] for r in out}
    assert st == {"live.b1-q2": "frozen", "live.b1-q3": "frozen", "bundle.live": "frozen"}
    f = IR.verified_freeze(run / "b1", "live.b1-q2", require_committed=False)
    assert f["screen"] == "fork_v2" and f["role"] == "primary"
    assert f["checks"]["applicability"]["jacob"] == "pass"
    m = IR.verified_freeze(run / "b1", "live.b1-q3", require_committed=False)
    with pytest.raises(ValueError, match="bundle member"):
        IR.refuse_bundle_member(m)
    b = IR.verified_freeze(run / "b1", "bundle.live", require_committed=False)
    assert b["bundle_items"] == ["live.b1-q2", "live.b1-q3"]
    assert IR.spec_from_freeze(b, "repair", 1).slug == "live-bundle-repair-1"
    assert IR.spec_from_freeze(f, "repair", 1).slug == "live-repair-1"
    # a "no" rejects; nothing more is frozen
    (run / "b1" / "boundary" / "freeze").rename(tmp_path / "freeze-old")
    labels2 = {cid: dict(lb, c2="unclear") for cid, lb in labels.items()}  # unresolved = no
    res = CARDS.freeze_v2(labels2, key)
    assert {r["status"] for r in res} == {"rejected"}
    sel = CARDS.advance_selection(res, reselect=False)
    assert sel[0]["rejected"] == ["live.b1-q2", "live.b1-q3"]


def _page_answers(html, script):
    """Run the page's own v2 answer mapping (verdictPatch, reasonPatch) under node."""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = html[html.index("// v2 answers: begin"): html.index("// v2 answers: end")]
    out = subprocess.run(["node", "-e", js + "\n" + script], capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


def test_v2_one_question_round_trip_through_the_page_mapping(run, tmp_path, CARDS):
    """Yes freezes; No with a reason rejects on that check; Can't tell rejects; a reason chip
    does nothing unless No is chosen; c4 follows nq."""
    from compaction.s2 import interact_run as IR
    _v2_two_boundaries(run)
    IR.place_cards(run / "b1", _screen_b1(run))
    spec = tmp_path / "instruction.md"
    spec.write_text(TASK)
    _w(run / "run.json", {"task": {"task_id": "demo", "spec_path": str(spec)}})
    html, key = CARDS.build_v2([(run, 1)])
    ans = _page_answers(html, """
      const step = (l, p) => ({ ...l, ...(p || {}) });
      let no = step({}, verdictPatch(3, "no")); no = step(no, reasonPatch(no, "c1"));
      let off = step(no, reasonPatch(no, "c2")); off = step(off, reasonPatch(off, "c2"));
      console.log(JSON.stringify({ yes: verdictPatch(3, "yes"), no, off,
        unclear: verdictPatch(3, "unclear"), yes4: verdictPatch(4, "yes"),
        early: reasonPatch({ verdict: "yes" }, "c1") }));""")
    assert ans["yes"] == {"verdict": "yes", "c1": "yes", "c2": "yes", "c3": "yes"}
    assert ans["no"] == {"verdict": "no", "c1": "no", "c2": "unclear", "c3": "unclear"}
    assert ans["off"] == ans["no"]  # a chip toggles back
    assert ans["yes4"]["c4"] == "yes" and ans["early"] is None
    v = {cid: e["content_sha256"][:16] for cid, e in key["cards"].items()}
    for name, why in (("no", "Really lost: no"), ("unclear", "Really lost: unclear")):
        res = CARDS.freeze_v2({cid: {**ans[name], "v": v[cid]} for cid in key["cards"]}, key)
        assert {r["status"] for r in res} == {"rejected"}
        assert all(why in r["reasons"] for r in res)
    res = CARDS.freeze_v2({cid: {**ans["yes"], "note": "ok", "v": v[cid]}
                           for cid in key["cards"]}, key, runner=_ref_pass)
    assert {r["cand_id"]: r["status"] for r in res} == {
        "live.b1-q2": "frozen", "live.b1-q3": "frozen", "bundle.live": "frozen"}


def test_v2_card_facts_quote_context_and_closest_passage(CARDS):
    said = CARDS.said_parts("x and y said here.", "y  said")
    assert (said["before"], said["quote"], said["after"]) == ("x and ", "y said", " here.")
    bullet = CARDS.said_parts("Intro line\n- `failedTests` as an array\nnext", "`failedTests` as an array")
    assert bullet["before"] == "" and bullet["ctx"] == "Intro line" and not bullet["ctx_after"]
    colon = CARDS.said_parts("Prev.\nAdd these with defaults:\n  a: 1\n", "Add these with defaults:")
    assert colon["ctx"] == "a: 1" and colon["ctx_after"]
    assert not CARDS.said_parts("nothing", "absent quote")["found"]
    text = "alpha line\nthe conflict check returns 409 here\nother"
    near = CARDS.closest_passage(text, "Conflicts: the conflict check must return 409")
    assert not near["verbatim"] and near["shared"] == 3
    assert near["at"] == [11, 11 + len("the conflict check returns 409 here")]
    assert CARDS.closest_passage(text, "the conflict check returns 409")["verbatim"]
    assert CARDS.closest_passage("zzz", "conflict check")["at"] is None
    short = CARDS.closest_passage("Set foo=1 here", "foo=1")  # astra cards-v2 r1: < 8 chars
    assert short["verbatim"] and short["at"] == [4, 9]
    assert CARDS.closest_passage("naïve 🙂 foo=1", "foo=1")["at"] == [9, 14]  # UTF-16
    assert CARDS.plain_act("The agent drops the check.") == "drops the check."
    assert CARDS.plain_act("Rewrites get_x so it fails.") == "rewrites get_x so it fails."
    assert CARDS.plain_act("ChartMergeRules is called.") == "ChartMergeRules is called."


def test_v2_cards_refuse_a_boundary_past_the_sealed_point(run, CARDS):
    _v2_two_boundaries(run)
    _w(run / "sealed.json", [{"after_k": 1, "resume": 1, "utc": "x"}])
    with pytest.raises(CARDS.ArmsExist, match="sealed"):
        CARDS.build_v2([(run, 2)])



def test_pending_check_reads_the_cited_file_and_compares_whole_lines():
    """astra build r1 #1 and the side-agent note: whole lines of the cited file, not a
    repository-wide substring search; copy slips go to the card, absence excludes."""
    text = ("# old code: return save_record(rec)\n"
            "    def save_record(rec):  # TODO conflicts\n"
            "        return store(rec)\n")

    def files(image, path):
        return {"status": "ok", "text": text} if path == "api.py" else {"status": "missing"}

    d = {"pending_path": "api.py", "pending_reason": "no conflict check yet"}
    ok = R.check_pending(dict(d, pending_quote="     3\t        return store(rec)"), "img",
                         files)
    assert ok["outcome"] == "pass" and ok["evidence"]["found"][0]["line"] == 3
    # only a substring of another line (a comment): not a pass (astra's reproduction)
    sub = R.check_pending(dict(d, pending_quote="return save_record(rec)"), "img", files)
    assert sub["outcome"] == "unresolved" and sub["reason"] == "pending_line_inside_another"
    near = R.check_pending(dict(d, pending_quote="def  save_record( rec ):"), "img", files)
    assert near["outcome"] == "unresolved" and "save_record" in near["evidence"]["near"]
    # astra build r2 #2: the cited line inside a longer one, and a 4th identifier, go to Jacob
    inside = R.check_pending(dict(d, pending_quote="x = a + b"), "img",
                             lambda i, p: {"status": "ok", "text": "# old: x = a + b\n"})
    assert inside["outcome"] == "unresolved" and inside["reason"] == "pending_line_inside_another"
    fourth = R.check_pending(dict(d, pending_quote="alphabet_one(betaword_two, gammaword, delta_z)"),
                             "img", lambda i, p: {"status": "ok", "text": "use(delta_z)\n"})
    assert fourth["outcome"] == "unresolved" and "delta_z" in fourth["evidence"]["near"]
    short = R.check_pending(dict(d, pending_quote="x = a + b"), "img",
                            lambda i, p: {"status": "ok", "text": "x = a - b\n"})
    assert short["outcome"] == "unresolved"  # astra build r3: short identifiers count too
    gone = R.check_pending(dict(d, pending_quote="render_template(page_object)"), "img", files)
    assert gone["outcome"] == "fail" and gone["reason"] == "pending_spot_not_found"
    missing = R.check_pending(dict(d, pending_path="other.py",
                                   pending_quote="return store(rec)"), "img", files)
    assert missing["outcome"] == "fail" and missing["reason"] == "pending_file_not_in_workspace"
    err = R.check_pending(dict(d, pending_quote="return store(rec)"), "img",
                          lambda i, p: {"status": "error", "error": "docker"})
    assert err["outcome"] == "unresolved" and err["reason"] == "pending_lookup_failed"
    none = R.check_pending(dict(d, pending_path=None, pending_quote=None), "img", files)
    assert none["outcome"] == "fail" and none["reason"] == "no_pending_spot"
    assert R.check_pending(dict(d, pending_quote="return store(rec)"), None, files)[
        "outcome"] == "unresolved"


def test_file_script_reads_one_file_safely():
    sc = R.file_script("src/a b.py")
    assert "base64 -d" in sc and "a b.py" not in sc  # the path never reaches the shell raw
    assert "*..*" in sc  # no escaping /app
