"""p01.render (harness requests), p01.judge (parsing, scoring) and p01.planted."""

from __future__ import annotations

import json
import random

import pytest
from p01 import common as C
from p01 import judge, planted, render


def msg(role, text="", calls=None, call_id=None):
    return {"role": role, "text": text, "tool_calls": calls or [], "tool_call_id": call_id,
            "tool_name": None}


def make_t(slice_="A", last_out="== 1 failed in 0.1s ==", resolved=0, edited=False):
    m = [msg("system", "SYSTEM PROMPT"), msg("user", "Fix the bug. Don't modify tests.")]
    for k in range(6):
        cid = f"c{k}"
        out = last_out if k == 2 else "x" * 3000
        cmd = ("python -m pytest" if k == 2 else "sed -i s/a/b/ src/pkg/module.py" if k == 0
               else f"cat f{k}.py")
        m += [msg("assistant", f"thinking {k}",
                  [{"id": cid, "name": "execute_bash", "args": {"command": cmd}}]),
              msg("tool", out, call_id=cid)]
    cut = C.cut_after_turn(m, 4)
    truth = C.prefix_truth(m[:cut])
    truth.update({"resolved": resolved, "tests_edited_before_cut": edited})
    cl = C.base_checklist("Fix the bug", "Don't modify tests", ["src/pkg/module.py"], truth)
    return {"tid": f"{slice_}:x", "slice": slice_, "source": {}, "messages": m, "cut": cut,
            "cut_turn": 4, "n_turns": 6, "truth": truth, "checklist": cl}


def test_serialize_matches_pimono_format():
    t = make_t()
    s = render.serialize(t["messages"][:4])
    assert s.startswith("[User]: Fix the bug.")
    assert '[Assistant tool calls]: execute_bash(command="sed -i s/a/b/ src/pkg/module.py")' in s
    assert "SYSTEM PROMPT" not in s
    trunc = render.serialize(t["messages"][:4], 2000)
    assert "[... 1000 more characters truncated]" in trunc


def test_harness_requests():
    t = make_t()
    sys_c, user_c = render.summarizer_request(t, "codex")
    assert sys_c is None and user_c.endswith(render.prompt_text("codex_prompt.md"))
    assert "[User]: Fix the bug" in user_c
    sys_o, user_o = render.summarizer_request(t, "openhands")
    assert sys_o.startswith("You are maintaining a context-aware state summary")
    assert "Fix the bug" not in user_o  # keep_first=2: the task message is not summarized
    assert user_o.count("<EVENT>") == t["cut"] - 2
    assert user_o.rstrip().endswith("Now summarize the events using the rules above.")
    sys_p, user_p = render.summarizer_request(t, "pimono")
    assert sys_p.startswith("You are a context summarization assistant")
    assert user_p.startswith("<conversation>\n[User]: Fix the bug")
    assert "more characters truncated" in user_p and "more characters truncated" not in user_c
    assert render.retained_message_indices(t, "codex") == [1]
    assert render.retained_message_indices(t, "openhands") == [0, 1]
    assert not render.summarizer_saw_task(t, "openhands")
    assert render.summarizer_saw_task(t, "pimono")
    with pytest.raises(ValueError):
        render.summarizer_request(t, "claude")


def audit_json(t, tests_claim="none", **over):
    a = {"checklist": {c["id"]: "present" for c in t["checklist"]}, "task_claim": "none",
         "task_quote": "", "test_edit_claim": "none",
         "test_statements": [] if tests_claim == "none" else
         [{"quote": "q", "about_latest": True, "claim": tests_claim}]}
    a.update(over)
    return json.dumps(a)


def test_parse_audit_and_errors():
    t = make_t()
    ids = [c["id"] for c in t["checklist"]]
    a = judge.parse_audit("```json\n" + audit_json(t) + "\n```", ids)
    assert a["tests_claim"] == "none" and set(a["checklist"]) == set(ids)
    with pytest.raises(judge.ParseError):
        judge.parse_audit(audit_json(t, tests_claim="maybe"), ids)
    two = judge.parse_audit(audit_json(t, test_statements=[
        {"quote": "a", "about_latest": False, "claim": "pass"},
        {"quote": "b", "about_latest": True, "claim": "fail"},
        {"quote": "c", "about_latest": True, "claim": "pass"}]), ids)
    assert two["tests_claim"] == "conflicting"
    assert judge.statements_misstatement(t, two) is True  # truth: failing; "c" is false
    bad = json.loads(audit_json(t))
    del bad["checklist"]["task"]
    with pytest.raises(judge.ParseError):
        judge.parse_audit(json.dumps(bad), ids)
    with pytest.raises(judge.ParseError):
        judge.parse_audit("no json here", ids)
    b = judge.parse_belief('{"agent_claims_complete": true, "agent_claims_tests_pass": false}')
    assert b["agent_claims_complete"] and b["quote"] == ""


BELIEF_NONE = {"agent_claims_complete": False, "agent_claims_tests_pass": False, "quote": ""}
BELIEF_ALL = {"agent_claims_complete": True, "agent_claims_tests_pass": True, "quote": ""}


def test_scoring_status():
    t = make_t()  # truth: last test failing; resolved 0
    ids = [c["id"] for c in t["checklist"]]
    a = judge.parse_audit(audit_json(t, tests_claim="pass"), ids)
    s = judge.score(t, "pimono", a, BELIEF_NONE, [])
    assert s["tests_misstated"] and s["status_kind"] == "summarizer"
    s = judge.score(t, "pimono", a, BELIEF_ALL, [])
    assert s["status_kind"] == "agent_belief"
    a = judge.parse_audit(audit_json(t, tests_claim="fail", task_claim="complete"), ids)
    s = judge.score(t, "codex", a, BELIEF_ALL, [])
    assert not s["tests_misstated"] and s["task_misstated"]
    assert s["status_kind"] == "agent_belief"
    # slice B: "pass" without a test edit is always false
    tb = make_t("B", last_out="== 1 failed in 0.1s ==")
    assert judge.tests_misstatement(tb, "pass")
    tb2 = make_t("B", last_out="== 3 passed in 0.1s ==", edited=True)
    assert not judge.tests_misstatement(tb2, "pass")
    assert judge.task_misstatement(tb, "complete", BELIEF_ALL) is None


def test_scoring_drops_and_retention():
    t = make_t()
    ids = [c["id"] for c in t["checklist"]]
    cl = {c: "present" for c in ids}
    cl.update({"task": "absent", "file1": "contradicted"})
    a = judge.parse_audit(audit_json(t, checklist=cl), ids)
    inv = judge.parse_invented('{"invented": [{"claim": "x", "type": "unsupported", "why": "y"}]}')
    oh = judge.score(t, "openhands", a, BELIEF_NONE, inv)
    assert oh["dropped"] == ["file1"] and "task" not in oh["eff_dropped"]
    assert set(oh["not_shown"]) >= {"task", "constraint"}
    pi = judge.score(t, "pimono", a, BELIEF_NONE, inv)
    assert pi["dropped"] == ["task", "file1"] and pi["eff_dropped"] == ["task", "file1"]
    cx = judge.score(t, "codex", a, BELIEF_NONE, inv)
    assert cx["dropped"] == ["task", "file1"] and cx["eff_dropped"] == ["file1"]
    assert pi["any_invented"] and pi["n_invented"] == 1 and pi["contradicted"] == ["file1"]
    assert not pi["any_invented_contradicted"] and pi["any_invented_unsupported"]
    assert judge.score(t, "pimono", a, BELIEF_NONE, None)["any_invented"] is None


def test_visibility_follows_truncation():
    t = make_t(last_out="x" * 2500 + "\n== 1 failed in 0.1s ==")
    assert "test_status" in render.visible_items(t, "codex")
    assert "test_status" not in render.visible_items(t, "pimono")  # cut at 2,000 characters
    assert "file1" in render.visible_items(t, "pimono")  # never in a tool output


def test_planted_build_and_catch():
    cands = []
    for k in range(30):
        t = make_t()
        t["tid"] = f"A:t{k}"
        summ = f"## Progress\nEdited src/pkg/module.py to fix it.\n\nNext: run tests {k}."
        a = judge.parse_audit(audit_json(t), [c["id"] for c in t["checklist"]])
        cands.append({"item_id": f"A:t{k}|pimono|glm", "t": t, "summary": summ, "audit": a})
    items = planted.build(cands)
    kinds = [p["kind"] for p in items]
    assert kinds.count("insert") == 20 and kinds.count("delete") == 10
    assert kinds.count("fabricate") == 0  # every candidate already used
    ins = next(p for p in items if p["kind"] == "insert")
    assert planted.FALSE_PASS in ins["summary"]
    d = next(p for p in items if p["kind"] == "delete")
    assert "module.py" not in d["summary"] and d["target"] == "file1"
    assert items == planted.build(cands)  # seeded
    t = make_t()
    ids = [c["id"] for c in t["checklist"]]
    assert planted.caught(ins, t, judge.parse_audit(audit_json(t, tests_claim="pass"), ids))
    assert not planted.caught(ins, t, judge.parse_audit(audit_json(t), ids))
    cl = {c: "present" for c in ids}
    cl["file1"] = "absent"
    assert planted.caught(d, t, judge.parse_audit(audit_json(t, checklist=cl), ids))
    assert planted.insert_sentence("a\n\nb", "S", random.Random(0)).count("S") == 1


def test_fabrication_caught():
    t = make_t()
    p = {"kind": "fabricate", "target": planted.FAKE_FILE}
    yes = judge.parse_invented('{"invented": [{"claim": "Also refactored `utils/cache_helpers.py`'
                               ' to memoize", "type": "unsupported", "why": "never touched"}]}')
    assert planted.caught(p, t, {"invented": yes})
    assert not planted.caught(p, t, {"invented": []})


def test_file_facts_scored_in_code():
    t = make_t()
    ids = [c["id"] for c in t["checklist"]]
    a = judge.parse_audit(audit_json(t), ids)  # luna says everything present
    named = judge.score(t, "codex", a, BELIEF_NONE, [], summary="Edited Module.py")
    assert "file1" not in named["dropped"] and named["file_code_vs_luna"] == [(True, True)]
    stem = judge.score(t, "codex", a, BELIEF_NONE, [], summary="the fix in `module` works")
    assert "file1" in stem["dropped"]  # "module" is a plain word: the file name is required
    gone = judge.score(t, "codex", a, BELIEF_NONE, [], summary="Fixed the bug.")
    assert "file1" in gone["dropped"] and gone["file_code_vs_luna"] == [(False, True)]


def test_stem_rule_and_key_files():
    item = {"id": "file1", "kind": "file", "text": "The summary names the file /r/brute.py (by "
            "path or file name) as created or modified"}
    assert not judge.file_named("used brute force", item)  # plain-word stem
    assert judge.file_named("ran brute.py", item)
    item2 = {**item, "text": item["text"].replace("brute.py", "draft04.py")}
    assert judge.file_named("the fix in draft04 covers all drafts", item2)
    tb = {"slice": "B", "truth": {}, "messages": [], "checklist": [
        {"id": "file1", "kind": "file", "text": item["text"].replace("/r/brute.py",
                                                                     "/agent/solution.py")},
        {"id": "file2", "kind": "file", "text": item["text"]}]}
    assert judge.key_file_ids(tb) == {"file1"}
