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


@pytest.fixture(autouse=True)
def _own_opus_lock(tmp_path, monkeypatch):
    """Never the real Max lock file: a live Opus run elsewhere would block these tests."""
    monkeypatch.setattr(R, "OPUS_LOCK_FILE", tmp_path / "opus.lock")


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


def _opus_ok():
    return StubBackend(lambda req: dict(OPUS_LOST))


def _screen_b1(run, draft=None, **kw):
    return R.screen_v2(run / "b1", backend=StubBackend(lambda req: dict(draft or DRAFT_V2)),
                       grep_runner=_grep_v2, ref_runner=_ref_pass, file_runner=_files,
                       task=TASK, **{"sources": ["live", "cc_prompt"],
                                     "opus_backend": _opus_ok(), **kw})


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
                      sources=["live", "cc_prompt"], opus_backend=_opus_ok())
    disp = {c["cand_id"]: c["disposition"] for c in out["candidates"]}
    assert out["carded"] == "live.b1-q2" and out["bundle"] == ["live.b1-q3"]
    assert disp["live.b1-q5"] == "not_reached"  # the bundle is full (max_bundle=1)
    q5 = {c["cand_id"]: c for c in out["candidates"]}["live.b1-q5"]
    assert q5["checks"]["retention"]["outcome"] == "fail"  # live kept it (recorded anyway)
    assert all(v == "not_reached" for k, v in disp.items() if k.startswith("cc_prompt."))
    c = {c["cand_id"]: c for c in out["candidates"]}["live.b1-q2"]
    app = c["checks"]["applicability"]
    assert app["outcome"] == "pass" and app["evidence"]["pending"]["outcome"] == "pass"
    assert set(c["checks"]) == {*R.CHECKS, "near_verbatim", "pointer", "opus"}  # six + B/C
    assert c["loss"]["status"] == "new" and c["inherited"] is False
    assert all(k.startswith("draft2|") for k in seen)
    assert out["screen_sha256"] == R.screen_sha_v2()
    # rejected: the next candidate, same source first (bundle capped at 1 by default)
    out2 = _screen_b1(run, rejected=["live.b1-q2"])
    assert out2["carded"] == "live.b1-q3" and out2["bundle"] == []


OPUS_LOST = {"retention": "lost", "retention_reason": "nothing", "applicable": "yes",
             "applicable_reason": "open", "item_type": "unbuilt_decision", "type_reason": "x",
             "in_workspace": "no", "workspace_reason": "y", "authority": "maintainer",
             "authority_reason": "r", "authority_quote": None, "conflict": "none",
             "conflict_quote": None, "conflict_reason": "c"}


def _screen_opus(run, verdicts, **kw):
    """The v2 screen with a stub Opus: `verdicts` maps cand_id -> verdict (default lost)."""
    opus = StubBackend(lambda req: dict(OPUS_LOST, **verdicts.get(req.key.split("|")[-1], {})))
    out = R.screen_v2(run / "b1", backend=StubBackend(lambda req: dict(DRAFT_V2)),
                      grep_runner=_grep_v2, ref_runner=_ref_pass, file_runner=_files,
                      task=TASK, opus_backend=opus, **{"sources": ["live", "cc_prompt"], **kw})
    return out, opus


def test_v2_opus_screen_gates_cards_and_logs_every_verdict(run):
    """B and C (24 Oct): Opus runs last, on Max; only a lost, applicable process rule or
    unbuilt decision not already in the workspace is carded; every verdict is logged."""
    _v2_two_boundaries(run)
    out, opus = _screen_opus(run, {"live.b1-q2": {"retention": "paraphrased"},
                                   "live.b1-q3": {"item_type": "built_detail"},
                                   "cc_prompt.b1-q2": {"in_workspace": "yes"}})
    disp = {c["cand_id"]: c["disposition"] for c in out["candidates"]}
    assert disp["live.b1-q2"] == "excluded:opus:retention_paraphrased"
    assert disp["live.b1-q3"] == "excluded:type:built_detail"
    assert disp["cc_prompt.b1-q2"] == "excluded:type:already_in_workspace"
    assert out["carded"] == "cc_prompt.b1-q3"
    seen = [r.key.split("|")[-1] for r in opus.seen]
    assert seen[:4] == ["live.b1-q2", "live.b1-q3", "cc_prompt.b1-q2", "cc_prompt.b1-q3"]
    req = opus.seen[0]
    assert "<resumed_input>" in req.user and PENDING_LINE in req.user  # the code excerpt
    log = [json.loads(x) for x in (run / "b1" / "boundary" / "fork_v2" / R.OPUS_LOG)
           .read_text().splitlines()]
    assert [x["gate"] for x in log[:3]] == ["opus:retention_paraphrased", "type:built_detail",
                                             "type:already_in_workspace"]
    assert log[0]["haiku_grade"] == "dropped" and log[0]["near"]["of"] >= 1
    # the verdict is part of the screen: a rescreen under the same rules reuses it
    again, opus2 = _screen_opus(run, {})
    assert opus2.seen == [] and again["carded"] == out["carded"]
    # a process rule goes forward too; an Opus failure fails the screen (retried, never carded)
    assert R.opus_gate(dict(OPUS_LOST, item_type="process_rule", in_workspace="yes")) is None
    assert R.opus_gate(dict(OPUS_LOST, in_workspace="unclear")) == "type:workspace_unclear"
    (run / "b1" / "boundary" / "fork_v2" / "candidates.json").unlink()
    with pytest.raises(RuntimeError, match="opus screen failed"):
        R.screen_v2(run / "b1", backend=StubBackend(lambda req: dict(DRAFT_V2)),
                    grep_runner=_grep_v2, ref_runner=_ref_pass, file_runner=_files, task=TASK,
                    sources=["live"], opus_backend=StubBackend(lambda req: None))


def test_v2_cards_need_a_passing_opus_verdict(run, tmp_path, CARDS):
    """astra opus-screen r1 #1: placement, the card build and the freeze refuse a candidate
    without a passing Opus verdict under the current rules."""
    from compaction.s2 import interact_run as IR
    _v2_two_boundaries(run)
    out = _screen_b1(run)
    bad = dict(out, candidates=[dict(c, checks={k: v for k, v in c["checks"].items()
                                                if k != "opus"}) for c in out["candidates"]])
    with pytest.raises(ValueError, match="no passing Opus verdict"):
        IR.place_cards(run / "b1", bad)
    with pytest.raises(ValueError, match="isn't the current rules"):
        IR.place_cards(run / "b1", dict(out, screen_sha256="old"))
    IR.place_cards(run / "b1", out)
    spec = tmp_path / "instruction.md"
    spec.write_text(TASK)
    _w(run / "run.json", {"task": {"task_id": "demo", "spec_path": str(spec)}})
    _html, key = CARDS.build_v2([(run, 1)])
    cj = run / "b1" / "boundary" / "fork_v2" / "candidates.json"
    doc = json.loads(cj.read_text())
    for c in doc["candidates"]:
        c["checks"].pop("opus", None)
    _w(cj, doc)
    with pytest.raises(CARDS.StaleScreen, match="no passing Opus verdict"):
        CARDS.build_v2([(run, 1)])
    cid = key["order"][0]
    lab = {cid: {"c1": "yes", "c2": "yes", "c3": "yes",
                 "v": key["cards"][cid]["content_sha256"][:16]}}
    r = CARDS.freeze_v2(lab, key, runner=_ref_pass)[0]
    assert r["status"] == "held" and any("Opus" in x for x in r["reasons"])


def test_pre_bc_exemption_is_testem_only_and_committed(run, tmp_path, CARDS, monkeypatch):
    """r2 #1: only the hard-coded testem freeze, with a committed annotation carrying its
    record hash, may skip the Opus gate; any other card claiming pre-B/C is refused."""
    from compaction.s2 import interact_run as IR
    _v2_two_boundaries(run)
    out = _screen_b1(run)
    IR.place_cards(run / "b1", out)
    spec = tmp_path / "instruction.md"
    spec.write_text(TASK)
    _w(run / "run.json", {"task": {"task_id": "demo", "spec_path": str(spec)}})
    _html, key = CARDS.build_v2([(run, 1)])
    cid = key["order"][0]
    cand = key["cards"][cid]["cand_id"]
    monkeypatch.setattr(IR, "FREEZE_COMMITTED", tmp_path / "committed")  # never the repo's
    fz = IR.committed_freeze_path(run / "b1", cand)
    fz.parent.mkdir(parents=True, exist_ok=True)
    _w(fz, {"record_sha256": "abc"})
    _w(fz.with_name(fz.stem + ".annotations.json"),
       {"screen_era": "pre-B/C", "freeze_record_sha256": "abc"})
    lab = {cid: {"c1": "yes", "c2": "yes", "c3": "yes",
                 "v": key["cards"][cid]["content_sha256"][:16]}}
    r = CARDS.freeze_v2(lab, key, runner=_ref_pass)[0]
    assert r["status"] == "held" and any("never to a new one" in x for x in r["reasons"])
    # even as the named card, an uncommitted annotation doesn't exempt it
    monkeypatch.setattr(CARDS, "PRE_BC_EXEMPT", (run.name, 1, cand))
    monkeypatch.setattr(CARDS, "ROOT", run.parent)
    assert not CARDS.pre_bc_exempt(run, 1, cand)
    # r3: even when the exemption holds, no new freeze is made under it: with no local freeze
    # and the card's content edited (no Opus verdict), it is held, not frozen
    monkeypatch.setattr(CARDS, "pre_bc_exempt", lambda *a: True)
    cj = run / "b1" / "boundary" / "fork_v2" / "candidates.json"
    doc = json.loads(cj.read_text())
    for c in doc["candidates"]:
        c["checks"].pop("opus", None)
        c["repair_text"] = c["repair_text"] + " (edited)"
    _w(cj, doc)
    r = CARDS.freeze_v2(lab, key, runner=_ref_pass)[0]
    assert r["status"] == "held" and not (run / "b1" / "boundary" / "freeze").exists()
    assert any("never to a new one" in x for x in r["reasons"])
    assert any("no passing Opus verdict" in x for x in r["reasons"])


def test_opus_calls_never_overlap():
    """astra opus-screen r1: one Opus call at a time across workers."""
    active, peak = [0], [0]

    def slow(req):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
        return dict(OPUS_LOST)
    be = StubBackend(slow)

    def call(_):
        with R._opus_lock():
            be.run([T.Req(key="opus|x", system="", user="", schema=None, max_output_tokens=1,
                          rid="x", source="deepswe", private=True)], purpose="t")
    with __import__("concurrent.futures").futures.ThreadPoolExecutor(4) as ex:
        list(ex.map(call, range(4)))
    assert peak[0] == 1


def test_v2_funnel_reports_types_and_haikus_precision_against_opus(run):
    from compaction.s2 import interact_run as IR
    _v2_two_boundaries(run)
    _screen_opus(run, {"live.b1-q2": {"retention": "kept"},
                       "live.b1-q3": {"item_type": "built_detail", "type_reason": "in code"}})
    _screen_opus(run, {"live.b1-q2": {"retention": "kept"},  # a repeat: counted once
                       "live.b1-q3": {"item_type": "built_detail", "type_reason": "in code"}})
    f = IR.funnel(run.parent)
    (o,) = f["opus"]["by_screen_version"].values()
    hp = o["haiku_precision_among_opus_reviewed_survivors"]
    assert hp["dropped"] == o["n"] and hp["opus_lost"] == o["n"] - 1
    assert o["by_gate"]["opus:retention_kept"] == 1 and o["by_type"]["built_detail"] == 1
    ex = {r["cand_id"]: r for r in f["excluded"]}
    assert ex["live.b1-q3"]["item_type"] == "built_detail"
    assert ex["live.b1-q3"]["type_reason"] == "in code"
    assert f["passed_opus"][0]["item_type"] == "unbuilt_decision"
    md = (run.parent / "funnel.md").read_text()
    assert "Haiku precision among Opus-reviewed survivors" in md
    assert "Type (Opus): built_detail" in md


def test_v2_later_compactions_fork_only_from_the_runs_own_summary(run):
    """Jacob, 24 Oct: no mixed-method chains. At k>1 a replay source is excluded
    (source:mixed_chain); only the live summary can be carded. k=1 keeps every source."""
    _v2_two_boundaries(run)
    R.grade_sources_v2(run / "b2", ["live", "cc_prompt"], grader=_grader(
        {"live": {"b1-q2": "dropped", "b1-q5": "dropped", "b2-q1": "dropped"},
         "cc_prompt": {"b1-q2": "dropped", "b2-q1": "dropped"}}))
    R.loss_ledger(run / "b2")
    out = R.screen_v2(run / "b2", backend=StubBackend(lambda req: dict(DRAFT_V2)),
                      grep_runner=_grep_v2, ref_runner=_ref_pass, file_runner=_files,
                      task=TASK, sources=["live", "cc_prompt"], opus_backend=_opus_ok())
    disp = {c["cand_id"]: c["disposition"] for c in out["candidates"]}
    assert {d for cid, d in disp.items() if cid.startswith("cc_prompt.")} == {
        "excluded:source:mixed_chain"}
    assert out["carded"] is None or out["carded"].startswith("live.")
    from compaction.s2 import interact_run as IR
    f = IR.funnel(run.parent)
    assert f["counts"]["v2"]["excluded:source:mixed_chain"] == sum(
        d == "excluded:source:mixed_chain" for d in disp.values())


def test_v2_near_verbatim_rule_and_unclear_grades(run):
    """The cheap rule before Opus: a quote (nearly) verbatim in the resumed input is excluded;
    an unclear Haiku grade goes to Opus rather than straight to a card."""
    assert R.near_verbatim("x\nplease return a 409 on all conflicting writes\n",
                           "Return a 409 on conflicting writes!")["nearly"]  # 4 of 4
    assert not R.near_verbatim("x\nreturn a 409 on any conflicting write\n",
                               "Return a 409 on conflicting writes!")["nearly"]  # 3 of 4
    assert not R.near_verbatim("x\nsomething else entirely\n",
                               "Return a 409 on conflicting writes!")["nearly"]
    assert not R.near_verbatim("409", "a 409")["nearly"]  # under 3 key words: never nearly
    # astra opus-screen r1 #3: polarity, word boundaries, short quotes
    assert not R.near_verbatim("x\nmodify existing tests\n",
                               "do not modify existing tests")["nearly"]
    assert not R.near_verbatim("x\ndo not modify existing tests\n",
                               "modify existing tests")["nearly"]
    # r2 #3: a negation on either side sends it to Opus, whatever its scope
    assert not R.near_verbatim("x\nplease do not modify existing tests\n",
                               "Do not modify existing tests")["nearly"]
    assert not R.near_verbatim("x\nModify existing tests, but do not change production code\n",
                               "Do not modify existing tests")["nearly"]
    assert not R.near_verbatim("x\nModify existing tests, but do not change production code\n",
                               "modify existing tests")["nearly"]
    assert not R.near_verbatim("call foobar_baz_qux now", "foo")["verbatim"]
    assert R.near_verbatim("call foo now", "foo")["verbatim"]
    assert not R.near_verbatim("call foo now", "foo")["nearly"]  # short: always to Opus
    _v2_two_boundaries(run)
    view = R.S.source_view(run / "b1", "live")
    it = {"quote": next(ln for ln in R.resumed_text(view).splitlines()
                        if len(R.key_words(ln)) >= R.NEAR_MIN_WORDS)}
    assert R.check_near(view, it)["outcome"] == "fail"
    detail = json.loads((run / "b1" / "boundary" / "fork_v2" / "grades_detail.json").read_text())
    detail["live"]["b1-q2"]["grade"] = "unclear"
    _w(run / "b1" / "boundary" / "fork_v2" / "grades_detail.json", detail)
    out, _opus = _screen_opus(run, {})
    c = {c["cand_id"]: c for c in out["candidates"]}["live.b1-q2"]
    assert c["checks"]["retention"]["outcome"] == "unresolved"
    assert c["disposition"] == "carded" and c["checks"]["opus"]["outcome"] == "pass"


def test_v2_screen_excludes_a_candidate_without_a_pending_spot(run):
    _v2_two_boundaries(run)
    for bad in (dict(DRAFT_V2, pending_quote="a line that is not in the workspace"),
                dict(DRAFT_V2, pending_path="gone.py"),
                dict(DRAFT_V2, pending_path=None, pending_quote=None)):
        (run / "b1" / "boundary" / "fork_v2" / "candidates.json").unlink(missing_ok=True)
        out = _screen_b1(run, draft=bad, sources=["live"])
        disp = [c["disposition"] for c in out["candidates"]]
        # no spot cited at all takes the occasion route (25 Oct); the stub's verdict is an
        # unbuilt decision, so the gate excludes it
        want = ("excluded:occasion:not_process_rule" if bad["pending_path"] is None
                else "excluded:applicability:pending")
        assert disp[:2] == [want] * 2 and out["carded"] is None


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


def test_v2_design_override_fills_a_missing_matched_block(run, tmp_path, CARDS):
    """24 Oct (A): a card held for a missing matched block freezes once the main session's
    override fills it; the override is code-checked like an edit, recorded on the freeze, and
    never replaces Jacob's own edit."""
    from compaction.s2 import interact_run as IR
    from compaction.s2 import interact_score as SC
    _v2_two_boundaries(run)
    IR.place_cards(run / "b1", _screen_b1(run, draft=dict(DRAFT_V2, matched_quote=None,
                                                           matched_rationale=None)))
    spec = tmp_path / "instruction.md"
    spec.write_text(TASK)
    _w(run / "run.json", {"task": {"task_id": "demo", "spec_path": str(spec)}})
    good, other = _neutral_turns(run / "b1")
    _html, key = CARDS.build_v2([(run, 1)])
    cid = key["order"][0]
    lab = {cid: {"c1": "yes", "c2": "yes", "c3": "yes",
                 "v": key["cards"][cid]["content_sha256"][:16]}}
    held = CARDS.freeze_v2(lab, key)[0]
    assert held["status"] == "held" and "d_matched" in held["reasons"][0]
    # a maintainer quote that carries a requirement, and a non-maintainer quote: held
    for mq, why in (("Conflicts should give a 409 back please.", "matched_overlaps_requirement"),
                    (other, "matched_not_maintainer")):
        o = {cid: {"fields": {"d_matched": mq, "d_rationale": "x"}, "by": "m", "utc": "t"}}
        r = CARDS.freeze_v2(lab, key, overrides=o, runner=_ref_pass)[0]
        assert r["status"] == "held" and why in r["reasons"][0], (mq, r["reasons"])
    bad = {cid: {"fields": {"d_matched": "words that are nowhere in the history at all",
                            "d_rationale": "x"}, "by": "main session", "utc": "t"}}
    r = CARDS.freeze_v2(lab, key, overrides=bad, runner=_ref_pass)[0]
    assert r["status"] == "held" and "matched_not_in_history" in r["reasons"][0]
    ov = {cid: {"fields": {"d_matched": good, "d_rationale": "unrelated"},
                "by": "main session", "utc": "t"}}
    r = CARDS.freeze_v2(lab, key, overrides=ov, runner=_ref_pass)[0]
    assert r["status"] == "frozen" and r["design_override"]["fields"] == ["d_matched",
                                                                         "d_rationale"]
    f = SC.read_freeze(run / "b1", key["cards"][cid]["cand_id"])
    assert f["matched_text"] == good and f["design_override"]["by"] == "main session"
    assert f["checks"]["design"]["outcome"] == "pass"
    assert f["checks"]["design"]["jacob_via"] == ("card; matched block filled by override "
                                                  "after labelling")
    assert f["checks"]["retention"]["jacob_via"] == "3-question card"
    # Jacob's own edit wins over an override
    (run / "b1" / "boundary" / "freeze").rename(tmp_path / "freeze-old")
    mine = {cid: dict(lab[cid], d_rationale="Jacob's reason")}
    r = CARDS.freeze_v2(mine, key, overrides=ov, runner=_ref_pass)[0]
    assert r["design_override"]["fields"] == ["d_matched"]
    assert SC.read_freeze(run / "b1", key["cards"][cid]["cand_id"])[
        "matched_rationale"] == "Jacob's reason"
    # a recorded fallback (no maintainer quote passed) may carry only matched_not_maintainer
    (run / "b1" / "boundary" / "freeze").rename(tmp_path / "freeze-old2")
    fb = {cid: {"fields": {"d_matched": other, "d_rationale": "neutral"}, "by": "m",
                "utc": "t", "speaker": "history", "fallback": "no maintainer quote passed"}}
    r = CARDS.freeze_v2(lab, key, overrides=fb, runner=_ref_pass)[0]
    assert r["status"] == "frozen" and r["design_override"]["fallback"]
    assert SC.read_freeze(run / "b1", key["cards"][cid]["cand_id"])["design_override"][
        "speaker"] == "history"


def _neutral_turns(bdir):
    """Add a neutral maintainer reply and a neutral agent remark to a boundary's history (the
    fixture's replies all carry requirements). Returns (maintainer quote, other quote)."""
    b = bdir / "boundary"
    pre = json.loads((b / "pre_messages.json").read_text())
    mq, other = "Thanks, the CI box was slow today, sorry.", "Running the linters on the api.py file now."
    pre += [{"role": "tool", "tool_call_id": "cx", "content": mq},
            {"role": "assistant", "content": other}]
    _w(b / "pre_messages.json", pre)
    replies = json.loads((b / "sim_replies.json").read_text())
    _w(b / "sim_replies.json", replies + [{"reply_id": "r9", "call_index": 9,
                                           "step": "02_implement", "pre_index": len(pre) - 2,
                                           "question": "ok?", "reply": mq}])
    return mq, other


def test_matched_lead_in_of_a_requirement_is_refused():
    reply = "The server abort is still off: make it broadcastAbort. Thanks, nice work so far."
    assert R._leads_in("The server abort is still off", reply)
    assert not R._leads_in("Thanks, nice work so far.", reply)
    # astra matched-override r1 #4: the colon inside the quote, a line break before it, and a
    # second occurrence are all caught
    assert R._leads_in("The server abort is still off:", reply)
    assert R._leads_in("abort is off", "abort is off\n  : do X")
    assert R._leads_in("abort is off", "abort is off, sure. Then: abort is off \n: fix it")
    # a sentence that explains or motivates a requirement, or sits next to one, is refused
    req = ["make it broadcastAbort."]
    two = "The reporter still counts after bail.\n\nPlease make it broadcastAbort. Cheers!"
    assert R._explains_requirement("still counts after bail", two, req)
    far = "Lovely weather today. I had coffee. Bye. Please make it broadcastAbort."
    assert not R._explains_requirement("Lovely weather today.", far, req)
    mc = {"replies": [reply], "req_quotes": ["make it broadcastAbort."]}
    d = {"matched_quote": "The server abort is still off", "matched_rationale": "x"}
    probs = R.check_design({"quote": "keep the abort idempotent pls"}, d,
                           R.spec_from_draft(DRAFT), {"post": [], "summary_index": 0},
                           [{"role": "tool", "content": reply}], maintainer=mc)
    assert "matched_leads_into_requirement" in probs["evidence"]["problems"]


def test_v2_override_must_pass_every_current_check(run, tmp_path, CARDS):
    """astra matched-override r1 #2: an override no longer inherits the card's own design
    problems; and #3: a screen under older rules is refused at build and re-checked strictly at
    freeze."""
    from compaction.s2 import interact_run as IR
    from compaction.s2 import interact_score as SC
    _v2_two_boundaries(run)
    long_q = "Conflicts should give a 409 back please, and quickly, and loudly, ok?"
    IR.place_cards(run / "b1", _screen_b1(run, draft=dict(DRAFT_V2, matched_quote=long_q)))
    spec = tmp_path / "instruction.md"
    spec.write_text(TASK)
    _w(run / "run.json", {"task": {"task_id": "demo", "spec_path": str(spec)}})
    good, _ = _neutral_turns(run / "b1")
    _html, key = CARDS.build_v2([(run, 1)])
    cid = key["order"][0]
    shown = key["cards"][cid]["screen_design"]
    assert shown["d_matched"] == long_q
    lab = {cid: {"c1": "yes", "c2": "yes", "c3": "yes",
                 "v": key["cards"][cid]["content_sha256"][:16]}}
    # the card itself showed matched_length_out_of_range; an override with the same problem
    # is still held
    o = {cid: {"fields": {"d_matched": "Thanks, the CI box was slow today, sorry. Really slow, "
                                       "very slow indeed.", "d_rationale": "x"},
               "by": "m", "utc": "t"}}
    r = CARDS.freeze_v2(lab, key, overrides=o, runner=_ref_pass)[0]
    assert r["status"] == "held" and "fails the current checks" in r["reasons"][0]
    ok = {cid: {"fields": {"d_matched": good, "d_rationale": "neutral"}, "by": "m",
                "utc": "t"}}
    # a stale screen: the build refuses, and the freeze holds it for a rescreen (r2 #2)
    cj = run / "b1" / "boundary" / "fork_v2" / "candidates.json"
    data = json.loads(cj.read_text())
    _w(cj, dict(data, screen_sha256="old"))
    with pytest.raises(CARDS.StaleScreen):
        CARDS.build_v2([(run, 1)])
    r = CARDS.freeze_v2(lab, key, overrides=ok, runner=_ref_pass)[0]
    assert r["status"] == "held" and any("rescreen required" in x for x in r["reasons"])
    _w(cj, data)
    r = CARDS.freeze_v2(lab, key, overrides=ok, runner=_ref_pass)[0]
    assert r["status"] == "frozen", r["reasons"]
    assert not SC.read_freeze(run / "b1", key["cards"][cid]["cand_id"]).get("screen_era")


def test_v2_rescreen_drops_drafts_from_older_rules(run):
    _v2_two_boundaries(run)
    calls = []

    def answer(req):
        calls.append(req.key)
        return dict(DRAFT_V2)

    def again():
        return R.screen_v2(run / "b1", backend=StubBackend(answer), grep_runner=_grep_v2,
                           ref_runner=_ref_pass, file_runner=_files, task=TASK,
                           sources=["live"], opus_backend=_opus_ok())
    again()
    n = len(calls)
    again()
    assert len(calls) == n  # same rules: drafts reused
    cj = run / "b1" / "boundary" / "fork_v2" / "candidates.json"
    _w(cj, dict(json.loads(cj.read_text()), screen_sha256="old"))
    again()
    assert len(calls) == 2 * n  # older rules: drafted again


def test_pick_matched_takes_the_first_candidate_passing_the_checks(run, CARDS):
    from compaction.s2 import interact_score as S2
    _v2_two_boundaries(run)
    bdir = run / "b1"
    pre = json.loads((bdir / "boundary" / "pre_messages.json").read_text())
    c = {"repair_text": "Conflicts should give a 409 back pretty please.",
         "act_spec": dict(R.spec_from_draft(DRAFT), reask_keywords=["409"])}
    ctx = {"cand": c, "pre": pre, "view": S2.source_view(bdir, "live")}
    best, tried = CARDS.pick_matched([
        {"quote": "not in the history, about the same length", "rationale": "x"},
        {"quote": "Conflicts should give a 409 back please.", "rationale": "x"}], ctx)
    assert best is None  # the second names the act's keyword
    assert "matched_not_in_history" in tried[0]["problems"]
    assert "names_act_keyword:409" in tried[1]["problems"]
    # the maintainer rule: a maintainer quote first; a non-maintainer one only as a fallback
    mq, other = _neutral_turns(bdir)
    ctx = dict(ctx, pre=json.loads((bdir / "boundary" / "pre_messages.json").read_text()),
               maintainer=__import__("compaction.s2.interact_reqs", fromlist=["x"])
               .maintainer_context(bdir))
    best, tried = CARDS.pick_matched([{"quote": other, "rationale": "x", "source": "history"},
                                      {"quote": mq, "rationale": "y", "source": "maintainer"}],
                                     ctx)
    assert best and best["quote"] == mq and not best.get("fallback"), tried
    assert tried[0]["problems"] == ["matched_not_maintainer"]
    best, _ = CARDS.pick_matched([{"quote": other, "rationale": "x", "source": "history"}], ctx)
    assert best["quote"] == other and best["fallback"].startswith("no maintainer quote passed")


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


# --- 24-25 Oct: file-backed repair candidates (Jacob's "recoverable, not recovered", consult r2)

def _bash(i: int, cmd: str) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": f"x{i}", "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": cmd})}}]}


PLAN = "# Plan\n- Run `cargo clippy --all-targets` before every commit.\n- Thanks, all good.\n"


def _with_plan_read(run):
    """b1's history gains a read of /tmp/plan.md (as the agent's first step does)."""
    b = run / "b1" / "boundary"
    pre = json.loads((b / "pre_messages.json").read_text())
    pre[1:1] = [_bash(90, "cat /tmp/plan.md"),
                {"role": "tool", "tool_call_id": "x90",
                 "content": json.dumps({"returncode": 0, "output": PLAN})}]
    _w(b / "pre_messages.json", pre)
    return pre


def test_recoverability_is_three_way_by_file_class():
    quote = "Run `cargo clippy --all-targets` before every commit."
    pre = [_bash(0, "cat /tmp/plan.md"), {"role": "tool", "content": "x"},
           _bash(1, "cd /app && cat AGENTS.md | head")]

    def grep(where):
        return lambda image, pats: {p: {"files": list(where), "lines": []} for p in pats}
    r = R.check_recoverability({"quote": quote}, "img", grep(["/tmp/plan.md"]), pre=pre)
    assert r["outcome"] == "pass" and r["evidence"]["class"] == "file_not_reattached"
    assert r["evidence"]["files_not_reattached"] == [{"file": "/tmp/plan.md", "last_read_msg": 0}]
    r = R.check_recoverability({"quote": quote}, "img", grep(["main:AGENTS.md"]), pre=pre)
    assert r["outcome"] == "pass" and r["evidence"]["files_not_reattached"][0] == {
        "file": "AGENTS.md", "last_read_msg": 2}
    r = R.check_recoverability({"quote": quote}, "img", grep(["src/lib.rs", "/tmp/plan.md"]))
    assert r["outcome"] == "fail" and r["evidence"]["class"] == "code"
    r = R.check_recoverability({"quote": quote}, "img", grep(["commit:abc"]))
    assert r["outcome"] == "fail"  # a commit message is repository content
    # a file the harness re-attaches after compaction is kept, never a file-backed omission
    r = R.check_recoverability({"quote": quote}, "img", grep(["AGENTS.md"]), pre=pre,
                               harness="claude_code")
    assert r["outcome"] == "fail" and not r["evidence"]["files_not_reattached"]


def test_lossaudit_items_are_candidates_with_their_source(run, tmp_path, monkeypatch):
    _v2_two_boundaries(run)
    _with_plan_read(run)
    out = tmp_path / "la"
    rid = "deepswe:demo-b1:0"
    _w(out / "items.jsonl", [])
    (out / "items.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"item_id": f"{rid}#haiku-max#i3", "type": "rule", "text": "clippy all targets"},
        {"item_id": f"{rid}#haiku-max#i4", "type": "task_state", "text": "state"}]))
    (out / "losses.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"item_id": f"{rid}#haiku-max#i3", "lost": True, "grade": "dropped",
         "boundary_dir": str(run / "b1"), "origin": "file",
         "quote": "Run `cargo clippy --all-targets` before every commit."},
        {"item_id": f"{rid}#haiku-max#i4", "lost": True, "grade": "dropped",
         "boundary_dir": str(run / "b1"), "origin": "file", "quote": "Thanks, all good."}]))
    items = R.lossaudit_candidates(run / "b1", out)
    assert [x["req_id"] for x in items] == ["b1-la3"]  # task_state isn't a candidate type
    it = items[0]
    assert it["source"]["file"] == "/tmp/plan.md" and it["source"]["text"] == PLAN
    assert it["source_reply_id"] == R.LOSSAUDIT_REPLY_ID
    assert R.check_retention_la(it)["outcome"] == "pass"
    assert R.check_retention_la(dict(it, la_grade="misstated"))["outcome"] == "fail"
    # the matched block's rule: the same source text, not carrying any requirement
    monkeypatch.setattr(R, "LOSSAUDIT_DIR", out)
    ctx = R.source_context(run / "b1", it)
    assert ctx["replies"] == [PLAN] and ctx["texts_in_history"]
    # the screen takes it after the maintainer ledger, live only, with its origin recorded
    res = _screen_b1(run, sources=["live"])
    c = {x["cand_id"]: x for x in res["candidates"]}["live.b1-la3"]
    assert c["origin"] == "lossaudit" and c["source_desc"] == "/tmp/plan.md"
    assert [x["cand_id"] for x in res["candidates"]][-1] == "live.b1-la3"


def test_authority_and_consistency_gate():
    ok = dict(OPUS_LOST)
    assert R.opus_gate(ok) is None
    assert R.opus_gate(dict(ok, authority="agent_plan_restating")) is None
    assert R.opus_gate(dict(ok, authority="agent_plan_own")) == "authority:agent_plan_own"
    assert R.opus_gate(dict(ok, authority="other")) == "authority:other"
    assert R.opus_gate(dict(ok, conflict="conflicts")) == "authority:conflict"
    assert R.opus_gate(dict(ok, retention="kept", conflict="conflicts")) == (
        "opus:retention_kept")  # the order: retention first


def test_pointer_flags_and_one_candidate_per_obligation():
    view = {"post": [{"role": "user", "content": "Summary. **Read `/tmp/plan.md` first** and "
                                                "keep going."}]}
    p = R.check_pointer(view, ["/tmp/plan.md", "AGENTS.md"])["evidence"]
    assert p["stratum"] == "contextual_omission_retrievable_from_file"
    assert p["files"][0]["named"] and p["files"][0]["reread_instructed"]
    assert not p["files"][1]["named"] and not p["files"][1]["reread_instructed"]
    assert R.check_pointer(view, [])["evidence"]["stratum"] is None
    a = "Do not add a required method to the public `chart.Accessor` interface"
    assert R.same_obligation(a, a + "; retrieve annotations without breaking it")
    assert R.same_obligation("Don't add a required method to the public chart.Accessor iface",
                             a)
    assert not R.same_obligation("Use table-driven tests with testify", a)


def test_obligations_already_in_the_universe_are_not_screened_again(run):
    _v2_two_boundaries(run)
    R.grade_sources_v2(run / "b2", ["live", "cc_prompt"], grader=_grader(
        {"live": {"b1-q2": "dropped", "b1-q5": "dropped", "b2-q1": "dropped"},
         "cc_prompt": {}}))
    R.loss_ledger(run / "b2")
    first = _screen_b1(run, sources=["live"])
    assert first["carded"] == "live.b1-q2"
    out = R.screen_v2(run / "b2", backend=StubBackend(lambda req: dict(DRAFT_V2)),
                      grep_runner=_grep_v2, ref_runner=_ref_pass, file_runner=_files,
                      task=TASK, sources=["live"], opus_backend=_opus_ok())
    c = {x["cand_id"]: x for x in out["candidates"]}
    assert c["live.b1-q2"]["disposition"] == "excluded:duplicate_obligation"
    assert c["live.b1-q2"]["checks"]["duplicate"]["evidence"]["first"]["k"] == 1


# --- astra repair-design r1 (25 Oct): the authority pool, the full screen, card edits

README = ("# Demo\n\nContributing: run `cargo clippy --all-targets` before every commit, and "
          "keep the public API stable.\n")
NO_FILES = {"status": "ok", "base": "B", "files": {}}


def _files_runner(files):
    return lambda image: {"status": "ok", "base": "B", "files": dict(files)}


def test_authority_pool_never_holds_what_the_agent_wrote(run):
    _v2_two_boundaries(run)
    pre = _with_plan_read(run)
    pre += [_bash(91, "cd /app && cat README.md"),
            {"role": "tool", "tool_call_id": "x91",
             "content": json.dumps({"returncode": 0, "output": README})},
            {"role": "assistant", "content": "I'll follow the plan: keep the public API stable."}]
    _w(run / "b1" / "boundary" / "pre_messages.json", pre)
    files = {"files": {"README.md": README, "cli/README.md": "keep the public API stable"}}
    pool = R.authority_pool(run / "b1", files)
    srcs = [x["source"] for x in pool]
    assert srcs[:1] == ["task:b1:msg3"] and "maintainer:r2" in srcs  # the AGENTS.md message
    assert "file:README.md" in srcs and "file:cli/README.md" not in srcs  # never read
    assert not any("cargo clippy" in x["text"] and x["kind"] != "file" for x in pool)
    assert not any("I'll follow the plan" in x["text"] for x in pool)
    # b2's history starts with b1's summary (a user-role message): never a task message
    b2 = R.authority_pool(run / "b2", NO_FILES)
    assert not any("SUMMARY" in x["text"] for x in b2)
    assert any(x["source"] == "task:b1:msg3" for x in b2)  # the task message from b1


def test_authority_spans_must_be_found_and_substantive(run):
    _v2_two_boundaries(run)
    _with_plan_read(run)
    pool = R.authority_pool(run / "b1", NO_FILES) + [
        {"source": "file:README.md", "kind": "file", "text": README}]
    rule = "Run `cargo clippy --all-targets` before every commit."
    ok = R.verify_authority({"authority": "agent_plan_restating",
                             "authority_quote": "run `cargo clippy --all-targets` before every "
                                                "commit"}, rule, pool)
    assert ok["ok"] and ok["source"] == "file:README.md" and ok["span"].startswith("run")
    short = R.verify_authority({"authority": "agent_plan_restating",
                                "authority_quote": "cargo clippy"}, rule, pool)
    assert not short["ok"] and short["why"] == "span_not_substantive"  # found, 2 words
    assert R.supports("keep the public API stable", rule)["ok"] is False  # found, unrelated
    off = R.verify_authority({"authority": "agent_plan_restating",
                              "authority_quote": "keep the public API stable"}, rule, pool)
    assert not off["ok"] and off["why"] == "span_not_substantive"
    # a plan item Opus calls "task": its own words must be in a task message, and aren't
    assert R.verify_authority({"authority": "task", "authority_quote": None}, rule,
                              pool)["why"] == "span_not_in_allowed_sources"
    assert R.verify_authority({"authority": "maintainer", "authority_quote": None},
                              REPLIES["r2"][2], pool)["source"] == "maintainer:r2"
    assert R.verify_authority({"authority": "agent_plan_own"}, rule, pool)["ok"] is False


def test_unverified_authority_excludes_the_candidate(run):
    _v2_two_boundaries(run)
    out, _ = _screen_opus(run, {"live.b1-q2": {"authority": "task"}})
    c = {x["cand_id"]: x for x in out["candidates"]}
    assert c["live.b1-q2"]["disposition"] == (
        "excluded:authority:unverified:span_not_in_allowed_sources")
    assert c["live.b1-q2"]["checks"]["opus"]["evidence"]["authority_check"]["allowed"] == ["task"]
    assert out["carded"] == "live.b1-q3"  # maintainer items verify in the replies


def _full(run, k=1, verdicts=None, files=NO_FILES):
    opus = StubBackend(lambda req: dict(OPUS_LOST, **(verdicts or {}).get(
        req.key.split("|")[-1], {})))
    out = R.screen_v2(run / f"b{k}", backend=StubBackend(lambda req: dict(DRAFT_V2)),
                      grep_runner=_grep_v2, ref_runner=_ref_pass, file_runner=_files, task=TASK,
                      opus_backend=opus, sources=["live", "cc_prompt"], full=True,
                      authority_runner=_files_runner(files["files"]))
    return out, opus


def test_full_screen_screens_every_candidate_and_dedupes_within_the_boundary(run):
    _v2_two_boundaries(run)
    with pytest.raises(ValueError, match="authority_runner"):
        R.screen_v2(run / "b1", backend=None, opus_backend=None, full=True)
    out, _ = _full(run)
    disp = {c["cand_id"]: c["disposition"] for c in out["candidates"]}
    assert out["mode"] == "full" and out["carded"] is None and out["bundle"] == []
    assert "not_reached" not in disp.values()
    assert out["passing"] == [c for c, d in disp.items() if d == "passed"]
    assert {"live.b1-q2", "live.b1-q3"} <= set(out["passing"])
    # the same obligation from another summary at this boundary counts once (r1 #7)
    dup = {c["cand_id"]: c for c in out["candidates"]}["cc_prompt.b1-q2"]
    assert dup["disposition"] == "excluded:duplicate_obligation"
    assert dup["checks"]["duplicate"]["evidence"]["first"] == {
        "k": 1, "cand_id": "live.b1-q2", "quote": dup["repair_text"]}
    assert out["authority_files"]["status"] == "ok"


def test_a_card_edit_reruns_every_gate_on_the_edited_text(run):
    _v2_two_boundaries(run)
    _, opus1 = _full(run)
    n1 = len(opus1.seen)
    edited = "Conflicting writes must return HTTP 409, please."
    (run / "b1" / "boundary" / "fork_v2" / R.EDITS_NAME).write_text(json.dumps(
        {"cands": {"live.b1-q2": {"repair_text": edited, "card": "c1"}}}))
    again, opus2 = _full(run)
    c = {x["cand_id"]: x for x in again["candidates"]}["live.b1-q2"]
    assert c["repair_text"] == edited and c["edit_sha256"]
    seen = [r.key.split("|")[-1] for r in opus2.seen]
    assert seen[0] == "live.b1-q2" and edited in opus2.seen[0].user and n1 > 1
    assert "live.b1-q3" not in seen  # unedited candidates keep their verdicts
    # cc_prompt.b1-q2 no longer duplicates the edited text, so it is screened in its own right
    assert {x["cand_id"]: x for x in again["candidates"]}["cc_prompt.b1-q2"][
        "disposition"] != "excluded:duplicate_obligation"
    _, opus3 = _full(run)
    assert opus3.seen == []  # the edited candidate's new verdict is reused under the same edit


def test_cohort_cards_freeze_standalone_and_edits_go_back_to_the_screen(run, tmp_path, CARDS):
    from compaction.s2 import interact_run as IR
    from compaction.s2 import interact_score as SC
    _v2_two_boundaries(run)
    out, _ = _full(run)
    _w(run / "b1" / "boundary" / "boundary.json", {"boundary_index": 1, "calls_in_step": 40,
                                                   "tail_start_index": 8, "sim_calls": 4})
    spec = tmp_path / "instruction.md"
    spec.write_text(TASK)
    _w(run / "run.json", {"task": {"task_id": "demo", "spec_path": str(spec)}})
    asg = IR.assignment(run / "b1")
    assert len(asg["assigned"]) == min(IR.ASSIGN_PER_BOUNDARY, len(out["passing"]))
    _html, key = CARDS.build_v2([(run, 1)])
    assert [key["cards"][c]["cand_id"] for c in key["order"]] == asg["assigned"]
    yes = {cid: {"c1": "yes", "c2": "yes", "c3": "yes", "v": e["content_sha256"][:16]}
           for cid, e in key["cards"].items()}
    first = key["order"][0]
    edited = dict(yes, **{first: dict(yes[first], d_repair="Return HTTP 409 on conflicting "
                                                           "writes, with the record id.")})
    res = {r["cand_id"]: r for r in CARDS.freeze_v2(edited, key, runner=_ref_pass)}
    c0 = key["cards"][first]["cand_id"]
    assert res[c0]["status"] == "held" and res[c0]["edit_recorded"]
    ed = json.loads((run / "b1" / "boundary" / "fork_v2" / R.EDITS_NAME).read_text())
    assert ed["cands"][c0]["repair_text"].startswith("Return HTTP 409")
    # the edit is pending: resubmitting the old labels can't freeze the stale screen (r3 #2)
    again = {r["cand_id"]: r for r in CARDS.freeze_v2({first: yes[first]}, key,
                                                      runner=_ref_pass)}
    assert again[c0]["status"] == "held" and "rescreen the boundary first" in " ".join(
        again[c0]["reasons"])
    assert c0 not in IR.open_assigned(run / "b1")  # off the page until the rescreen
    other = asg["assigned"][1]
    assert res[other]["status"] == "frozen"  # two standalone freezes may share a boundary
    f = IR.verified_freeze(run / "b1", other, require_committed=False)
    assert f["role"] == "cohort" and "bundle.live" not in res
    assert f["candidate_sha256"] == SC.canon_sha(CARDS.candidate_v2(run, 1, other))
    u = IR.build_universe(run.parent)
    assert {e["cand_id"] for e in u["roster"]} == {other}
    # a rejection leaves its slot empty: never refilled (r2 #2)
    sel = CARDS.advance_selection([{"run_dir": str(run), "k": 1, "cand_id": other,
                                    "status": "rejected"}])
    assert sel[0]["state"] == "cohort" and sel[0]["rejected"] == [other]
    assert IR.assignment(run / "b1")["assigned"] == asg["assigned"]
    assert other not in IR.open_assigned(run / "b1")


def test_an_override_of_the_repair_goes_back_to_the_screen_too(run, tmp_path, CARDS):
    from compaction.s2 import interact_run as IR
    _v2_two_boundaries(run)
    _full(run)
    _w(run / "b1" / "boundary" / "boundary.json", {"boundary_index": 1, "calls_in_step": 40,
                                                   "tail_start_index": 8, "sim_calls": 4})
    spec = tmp_path / "instruction.md"
    spec.write_text(TASK)
    _w(run / "run.json", {"task": {"task_id": "demo", "spec_path": str(spec)}})
    IR.assignment(run / "b1")
    _html, key = CARDS.build_v2([(run, 1)])
    cid = key["order"][0]
    lab = {cid: {"c1": "yes", "c2": "yes", "c3": "yes",
                 "v": key["cards"][cid]["content_sha256"][:16]}}
    # a shorter span of the same maintainer reply: still verifiable after the rescreen
    shorter = " ".join(key["cards"][cid]["screen_design"]["d_repair"].split()[:-1])
    ov = {cid: {"fields": {"d_repair": shorter}, "by": "main", "utc": "t"}}
    r = CARDS.freeze_v2(lab, key, runner=_ref_pass, overrides=ov)[0]
    assert r["status"] == "held" and r["edit_recorded"]
    ed = json.loads((run / "b1" / "boundary" / "fork_v2" / R.EDITS_NAME).read_text())
    assert ed["cands"][key["cards"][cid]["cand_id"]]["repair_text"] == shorter
    # held until the rescreen; after it, the same override equals the screened design and
    # changes nothing: the card freezes, no rescreen loop
    assert CARDS.freeze_v2(lab, key, runner=_ref_pass, overrides=ov)[0]["status"] == "held"
    _full(run)
    _html, key2 = CARDS.build_v2([(run, 1)])
    c2 = next(c for c in key2["order"]
              if key2["cards"][c]["cand_id"] == key["cards"][cid]["cand_id"])
    assert key2["cards"][c2]["screen_design"]["d_repair"] == ov[cid]["fields"]["d_repair"]
    lab2 = {c2: {"c1": "yes", "c2": "yes", "c3": "yes",
                 "v": key2["cards"][c2]["content_sha256"][:16]}}
    r2 = CARDS.freeze_v2(lab2, key2, runner=_ref_pass, overrides={c2: ov[cid]})[0]
    assert r2["status"] == "frozen", r2["reasons"]



# --- the occasion gate (Jacob, 25 Oct): process rules have no pending code spot

NO_SPOT = dict(DRAFT_V2, pending_path=None, pending_quote=None,
               pending_reason="a rule about how to work: no code line is open")
PENDING = "- [ ] next: keep the save_record signature free of a force flag"
OCC_PLAN = f"# Plan\n- [x] add the conflict check\n{PENDING}\n"
RULE = {"item_type": "process_rule", "occasion": "ahead", "occasion_kind": "prohibition",
        "occasion_quote": PENDING, "occasion_reason": "the signature work is still to come"}


def _occ(run, verdicts, *, draft=NO_SPOT, plan=OCC_PLAN, diff=""):
    opus = StubBackend(lambda req: dict(OPUS_LOST, **verdicts.get(req.key.split("|")[-1], {})))
    facts = {"status": "ok", "base": "B", "files": {}, "plan": plan, "diff": diff,
             "version": R.AUTHORITY_VERSION}
    return R.screen_v2(run / "b1", backend=StubBackend(lambda req: dict(draft)),
                       grep_runner=_grep_v2, ref_runner=_ref_pass, file_runner=_files,
                       task=TASK, opus_backend=opus, sources=["live"], full=True,
                       authority_runner=lambda image: dict(facts)), opus


def test_a_process_rule_with_its_occasion_ahead_passes_without_a_code_spot(run):
    _v2_two_boundaries(run)
    out, opus = _occ(run, {"live.b1-q2": RULE})
    c = {x["cand_id"]: x for x in out["candidates"]}
    a = c["live.b1-q2"]
    assert a["disposition"] == "passed", a["disposition"]
    assert a["checks"]["applicability"]["outcome"] == "pass"
    occ = a["checks"]["applicability"]["evidence"]["occasion"]
    assert occ["quote_found_in"] == "plan_file" and occ["workspace_act"] == []
    assert occ["span"]["cue"] == "[ ]" and "save_record" in occ["span"]["tie"]
    assert "<plan_file>" in opus.seen[0].user
    # the stub's other verdicts are unbuilt decisions: not this route
    assert c["live.b1-q3"]["disposition"] == "excluded:occasion:not_process_rule"


def test_the_occasion_gate_refuses_unverified_passed_or_already_done(run):
    _v2_two_boundaries(run)
    other = "- [ ] then check the save_record force flag stays out"
    cases = {
        "quote_not_found": dict(RULE, occasion_quote=other),
        "passed": dict(RULE, occasion="passed", occasion_quote=None),
        "unclear": dict(RULE, occasion="unclear", occasion_quote=None),
        # a generic word, a span with no pending cue, a completed step, an unrelated step
        "quote_not_substantive": dict(RULE, occasion_quote="commit"),
        "quote_not_pending": dict(RULE, occasion_quote="keep the save_record signature as is"),
        "quote_describes_a_done_step": dict(RULE, occasion_quote="- [x] add the conflict "
                                                                  "check"),
        "quote_not_about_the_act": dict(RULE, occasion_quote="- [ ] next: write the "
                                                             "changelog entry")}
    for why, v in cases.items():
        (run / "b1" / "boundary" / "fork_v2" / "candidates.json").unlink(missing_ok=True)
        out, _ = _occ(run, {"live.b1-q2": v})
        d = {x["cand_id"]: x["disposition"] for x in out["candidates"]}
        assert d["live.b1-q2"] == f"excluded:occasion:{why}", (why, d)
    # the same quote in the plan file on the kept image is enough
    (run / "b1" / "boundary" / "fork_v2" / "candidates.json").unlink()
    (run / "b1" / "boundary" / "fork_v2" / R.AUTHORITY_FILES_NAME).unlink(missing_ok=True)
    out, _ = _occ(run, {"live.b1-q2": cases["quote_not_found"]}, plan=OCC_PLAN + other + "\n")
    a = {x["cand_id"]: x for x in out["candidates"]}["live.b1-q2"]
    assert a["disposition"] == "passed"
    assert a["checks"]["occasion"]["evidence"]["quote_found_in"] == "plan_file"
    # a prohibition already broken in the workspace diff from the base commit
    (run / "b1" / "boundary" / "fork_v2" / "candidates.json").unlink()
    (run / "b1" / "boundary" / "fork_v2" / R.AUTHORITY_FILES_NAME).unlink()
    broken = ("diff --git a/api.py b/api.py\n--- a/api.py\n+++ b/api.py\n@@ -1 +1 @@\n"
              "-def save_record(r):\n+def save_record(r, force=False):\n")
    out, _ = _occ(run, {"live.b1-q2": RULE}, diff=broken)
    a = {x["cand_id"]: x for x in out["candidates"]}["live.b1-q2"]
    assert a["disposition"] == "excluded:occasion:already_in_workspace"
    assert a["checks"]["occasion"]["evidence"]["workspace_act"][0]["path"] == "api.py"
    # a diff that couldn't be read is never "not done": the prohibition is excluded
    (run / "b1" / "boundary" / "fork_v2" / "candidates.json").unlink()
    (run / "b1" / "boundary" / "fork_v2" / R.AUTHORITY_FILES_NAME).unlink()
    out, _ = _occ(run, {"live.b1-q2": RULE}, diff=None)
    a = {x["cand_id"]: x for x in out["candidates"]}["live.b1-q2"]
    assert a["disposition"] == "excluded:occasion:workspace_unreadable"


def test_only_observable_applicable_no_spot_candidates_take_the_occasion_route(run):
    _v2_two_boundaries(run)
    judged = dict(NO_SPOT, act_kind="judge", act_pattern=None, act_path=None)
    out, _ = _occ(run, {"live.b1-q2": RULE}, draft=judged)
    assert out["candidates"][0]["disposition"] == "excluded:applicability:pending"
    (run / "b1" / "boundary" / "fork_v2" / "candidates.json").unlink()
    out, _ = _occ(run, {"live.b1-q2": RULE}, draft=dict(NO_SPOT, applicable="unsure"))
    assert out["candidates"][0]["disposition"] == "excluded:applicability:pending"
    # a cited spot that isn't in the file still fails as before
    (run / "b1" / "boundary" / "fork_v2" / "candidates.json").unlink()
    out, _ = _occ(run, {"live.b1-q2": RULE}, draft=dict(DRAFT_V2, pending_path="gone.py"))
    assert out["candidates"][0]["disposition"] == "excluded:applicability:pending"


def test_the_boundary_facts_carry_the_plan_and_the_workspace_diff():
    import base64
    b = lambda x: base64.b64encode(x.encode()).decode()
    out = R.parse_authority("@@BASE@@ abc\n" f"@@EMX_AUTH_FILE@@ {b('AGENTS.md')} {b('rules')}\n"
                            f"@@EMX_PLAN@@ {b('# Plan')}\n@@EMX_DIFF@@ {b('diff --git')}\n"
                            "@@END@@\n")
    assert out["files"] == {"AGENTS.md": "rules"} and out["plan"] == "# Plan"
    assert out["diff"] == "diff --git" and out["version"] == R.AUTHORITY_VERSION
    assert R.parse_authority("@@BASE@@ abc\n@@EMX_DIFF@@ \n@@END@@\n")["plan"] is None



def _script_in(repo: Path, tmp: Path) -> str:
    """The real authority script, pointed at a local repo and temp files instead of /app and
    /tmp (the same commands otherwise)."""
    return (R.authority_script().replace("/app", str(repo))
            .replace("/tmp/base_commit.txt", str(tmp / "base_commit.txt"))
            .replace("/tmp/plan.md", str(tmp / "plan.md")))


def test_the_real_script_reads_the_workspace_and_fails_closed(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    g = ["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "AGENTS.md").write_text("Run clippy before every commit.\n")
    (repo / "api.py").write_text("def save_record(r):\n    pass\n")
    subprocess.run([*g, "add", "-A"], check=True)
    subprocess.run([*g, "commit", "-qm", "base"], check=True)
    base = subprocess.run([*g, "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    (tmp_path / "base_commit.txt").write_text(base + "\n")
    (tmp_path / "plan.md").write_text(OCC_PLAN)
    (repo / "api.py").write_text("def save_record(r, force=False):\n    pass\n")
    (repo / "new.py").write_text("x = 1\n")  # untracked: in the diff too
    out = subprocess.run(["bash", "-c", _script_in(repo, tmp_path)], capture_output=True,
                         text=True, check=False)
    f = R.parse_authority(out.stdout)
    assert f["status"] == "ok" and f["files"] == {"AGENTS.md": "Run clippy before every commit.\n"}
    assert f["plan"] == OCC_PLAN and f["diff_error"] is None
    assert "+def save_record(r, force=False):" in f["diff"] and "new.py" in f["diff"]
    assert subprocess.run([*g, "status", "--porcelain"], capture_output=True, text=True,
                          check=True).stdout.count("??") == 1  # the repo's index untouched
    # git add failing (a wrapper on PATH): the diff is unreadable, never empty
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    real = subprocess.run(["bash", "-c", "command -v git"], capture_output=True, text=True,
                          check=True).stdout.strip()
    (bin_ / "git").write_text(f'#!/bin/bash\nfor a in "$@"; do [ "$a" = add ] && exit 1; done\n'
                              f'exec {real} "$@"\n')
    (bin_ / "git").chmod(0o755)
    env = {**__import__("os").environ, "PATH": f"{bin_}:{__import__('os').environ['PATH']}"}
    out = subprocess.run(["bash", "-c", _script_in(repo, tmp_path)], capture_output=True,
                         text=True, check=False, env=env)
    f = R.parse_authority(out.stdout)
    assert f["diff"] is None and f["diff_error"] == "git_add"
    spec = {"act": {"kind": "diff_added", "pattern": r"def save_record\(.*force",
                    "path": "*.py", "negate": False}}
    view = {"post": [{"role": "user", "content": "summary"}]}
    assert R.occasion_check(RULE, view, f, spec)["reason"] == "occasion:workspace_unreadable"
    # no index to copy (a repo without one): unreadable too
    (repo / ".git" / "index").unlink()
    f = R.parse_authority(subprocess.run(["bash", "-c", _script_in(repo, tmp_path)],
                                         capture_output=True, text=True, check=False).stdout)
    assert f["diff"] is None and f["diff_error"] == "index_copy"


# --- split: one call per reply after a failed whole-segment call (native-codex, 25 Oct)

def _by_reply_order():
    """SPLIT_B1's items for the 02_implement replies (r2, r3, r4), in reply order."""
    keep = [d for d in SPLIT_B1 if d["reply_id"] != "r1"]
    return sorted(keep, key=lambda d: ["r2", "r3", "r4"].index(d["reply_id"]))


def test_a_failed_whole_split_falls_back_to_one_call_per_reply(run):
    whole = _by_reply_order()
    single = R.split(run / "b1", backend=StubBackend(lambda req: {"requirements": whole}),
                     task=TASK)

    def answer(req):
        if "|r" not in req.key:
            return None  # the whole segment: the output cap, as in native-codex's kombu b1
        block = re.search(r"<replies>\n(.*?)\n</replies>", req.user, re.DOTALL).group(1)
        rid = re.search(r'"reply_id": "(r\d)"', block).group(1)
        mine = [dict(d) for d in whole if d["reply_id"] == rid]
        for d in mine:  # a repeat of an earlier reply's item names its req_id, as it's listed
            if d.get("same_as") == "n1":
                assert '"req_id": "b1-q1"' in req.user
                d["same_as"] = "b1-q1"
        return {"requirements": mine}

    class Failing(StubBackend):
        def run(self, reqs, *, purpose):
            out = {}
            for r in reqs:
                self.seen.append(r)
                data = self.answer(r)
                out[r.key] = T.Resp(r.key, data, json.dumps(data), None if data else
                                    "output cap: 64000 tokens", model="stub-haiku")
            return out

    be = Failing(answer)
    chunked = R.split(run / "b1", backend=be, task=TASK)
    assert chunked["split_by_reply"] == 3 and "64000" in chunked["whole_split_error"]
    assert [r.key.split("|")[2:] for r in be.seen] == [[], ["r1"], ["r2"], ["r3"]]
    keys = ("req_id", "text", "quote", "duplicate_of", "fresh", "valid", "position",
            "source_reply_id", "in_tail")
    assert [{x: it[x] for x in keys} for it in chunked["items"]] == \
        [{x: it[x] for x in keys} for it in single["items"]]
    # a split that works in one call is unchanged: no fallback field, one request
    assert "split_by_reply" not in single
    # provenance (astra split-fallback r1 #1): every item says how it was split
    assert {it["extraction"] for it in single["items"]} == {"whole"}
    assert {it["extraction"] for it in chunked["items"]} == {"by_reply"}
    assert chunked["extraction"] == {"whole": 0, "by_reply": len(chunked["items"])}


def test_a_split_that_fails_on_one_reply_still_fails(run):
    def answer(req):
        return None

    class Failing(StubBackend):
        def run(self, reqs, *, purpose):
            return {r.key: T.Resp(r.key, None, "", "output cap", model="m") for r in reqs}
    with pytest.raises(RuntimeError, match="split failed on reply r2 alone"):
        R.split(run / "b1", backend=Failing(answer), task=TASK)



def test_by_reply_items_keep_their_stamp_into_later_boundaries_and_ledgers(run, monkeypatch):
    def answer(req):
        k = int(re.search(r":b(\d+)", req.key).group(1))
        if k == 1 and "|r" not in req.key:
            return {}  # no requirements list: a validation failure, not a backend error
        if k == 1:
            block = re.search(r"<replies>\n(.*?)\n</replies>", req.user, re.DOTALL).group(1)
            rid = re.search(r'"reply_id": "(r\d)"', block).group(1)
            return {"requirements": [dict(d, same_as=None) for d in _by_reply_order()
                                     if d["reply_id"] == rid]}
        return {"requirements": SPLIT_B2}
    b1 = R.split(run / "b1", backend=StubBackend(answer), task=TASK)
    assert b1["whole_split_error"] == "data has no requirements list"  # r1 #2: the real cause
    b2 = R.split(run / "b2", backend=StubBackend(answer), task=TASK)
    by = {it["req_id"]: it["extraction"] for it in b2["items"]}
    assert all(v == "by_reply" for r, v in by.items() if r.startswith("b1-"))
    assert all(v == "whole" for r, v in by.items() if r.startswith("b2-"))
    # the ledger rows carry it, and each source counts its by_reply losses
    R.grade_sources_v2(run / "b1", ["live"], grader=_grader({"live": {
        it["req_id"]: "dropped" for it in b1["items"]}}))
    led = R.loss_ledger(run / "b1")
    assert {r["extraction"] for r in led["sources"]["live"].values()} == {"by_reply"}
    assert led["by_reply"]["live"] == len(led["sources"]["live"]) > 0
    # the native audit's summary rows report them apart
    from compaction.s2 import session as SS
    from compaction.s2.native import audit as AU
    (run / "b1" / "boundary" / "eligibility.json").write_text("{}")
    monkeypatch.setattr(SS, "boundary_dirs", lambda rd: [run / "b1"])
    monkeypatch.setattr(R, "grade_sources_v2", lambda bdir, sources, backend=None: json.loads(
        (bdir / "boundary" / "fork_v2" / "grades.json").read_text()))
    rows = AU.requirements(run, lambda: None)
    assert rows[0]["items_by_reply"] == rows[0]["items"] > 0
    assert rows[0]["lost_by_reply"] == led["by_reply"]["live"]
