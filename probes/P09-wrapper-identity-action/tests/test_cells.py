import dataclasses

import pytest
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
)
from p09 import cells
from p09 import contracts as C

from x3.texts import SYSTEM_PROMPT

# Fixture templates (not the verbatim wrappers): one per wrapper, each with one {summary}.
FIXTURES = {
    C.Wrapper.CODEX: "CODEX PREFIX FIXTURE\n{summary}",
    C.Wrapper.CLAUDE_CODE: "CC FIXTURE\n\nSummary:\n{summary}\n\nsee {transcript_path}\nGo on.",
    C.Wrapper.PI_MONO: "PI FIXTURE:\n\n<summary>\n{summary}\n</summary>",
}
SUMMARY = "Edited solution.py to implement f() with {braces}. Tests not run yet."
SOLUTION = "def f(n: int) -> int:\n    return n\n"
TASK = "## Task\nImplement the function. Run `pytest test_solution.py -v`."


@pytest.fixture(autouse=True)
def _wrappers(monkeypatch):
    monkeypatch.setattr(cells, "load_wrapper", lambda w: FIXTURES[C.Wrapper(w)])


def _ctx(ctx_id="lcbhard_100|r1|a1", n_tests=1):
    sysm = ChatMessageSystem(content=SYSTEM_PROMPT).model_dump(exclude_none=True)
    user = ChatMessageUser(content=TASK).model_dump(exclude_none=True)
    return {"ctx_id": ctx_id, "n_tests": n_tests,
            "prefix_messages": [sysm, user,
                                {"role": "assistant", "content": [
                                    {"type": "reasoning", "reasoning": "hmm"},
                                    {"type": "text", "text": "Writing."}],
                                 "tool_calls": [{"id": "c1", "function": "execute_command",
                                                 "arguments": {"command": "echo"},
                                                 "type": "function"}]},
                                {"role": "tool", "content": "", "tool_call_id": "c1",
                                 "function": "execute_command"}]}


def _plants(r: C.Resume, sol=SOLUTION):
    """What the agent unit plants in the sandbox for this resume."""
    sol_after = (r.file_comment + "\n" + sol) if r.file_comment else sol
    return sol_after, r.status_file


@pytest.mark.parametrize("wrapper", list(C.Wrapper))
@pytest.mark.parametrize("cell", [c.name for c in C.CELLS])
def test_every_cell_builds_and_passes(cell, wrapper):
    ctx = _ctx()
    r = cells.build(ctx, SUMMARY, cell, wrapper=wrapper)
    pc = cells.plant_check(r, *_plants(r))
    assert pc["ok"], pc["problems"]
    c = C.CELL_BY_NAME[cell]
    assert r.cell == cell and r.wrapper == C.Wrapper(wrapper).value
    assert r.messages[0]["content"] == SYSTEM_PROMPT and r.messages[1]["content"] == TASK
    assert SUMMARY in r.messages[2]["content"]          # braces survive
    if c.location is None:
        assert r.claim is None and pc["count_in_context"] == 0 and len(r.messages) == 3
    else:
        assert r.claim == C.claim_text(c.level, C.paraphrase_index(ctx["ctx_id"]), 1)
    if c.location is C.Location.SUMMARY:
        assert r.messages[2]["content"] == cells.render_wrapper(wrapper,
                                                                SUMMARY + "\n\n" + r.claim)
    if c.location is C.Location.USER:
        assert r.messages[3]["content"] == r.claim and r.messages[3]["role"] == "user"
    if c.location is C.Location.TOOL:
        tc = r.messages[3]["tool_calls"][0]
        assert r.messages[3]["content"] == "" and tc["id"] == "call_p07_status"
        assert tc["arguments"] == {"command": C.STATUS_CMD}
        assert r.messages[4]["content"] == r.claim + "\n" == r.status_file
    if c.location is C.Location.FILE:
        assert r.file_comment == "# " + r.claim and pc["file_lines"] == 1
        assert pc["count_in_context"] == 0
    else:
        assert pc["file_lines"] == 0


def test_messages_validate_as_inspect():
    kinds = {"system": ChatMessageSystem, "user": ChatMessageUser,
             "assistant": ChatMessageAssistant, "tool": ChatMessageTool}
    for cell in ("control", "user_L2", "tool_L3"):
        r = cells.build(_ctx(), SUMMARY, cell)
        for m in r.messages:
            back = kinds[m["role"]].model_validate(m)
            assert back.model_dump(exclude_none=True) == m


def test_deterministic_and_paraphrase_fixed_per_context():
    ids = [f"lcbhard_{i}|r1|a1" for i in range(40)]
    assert {C.paraphrase_index(i) for i in ids} == {0, 1}
    for i in ids[:6]:
        ps = {cells.build(_ctx(i), SUMMARY, c.name).paraphrase for c in C.CELLS}
        assert ps == {C.paraphrase_index(i)}
        claims = {cells.build(_ctx(i), SUMMARY, c.name).claim for c in C.CELLS if c.level}
        assert all(C.CLAIMS[lv][C.paraphrase_index(i)].split(" ")[0] in " ".join(claims)
                   for lv in C.LADDER)
    a = cells.build(_ctx(), SUMMARY, "tool_L1")
    b = cells.build(_ctx(), SUMMARY, "tool_L1")
    assert a == b


def test_claim_patterns_match_only_their_own():
    for (lv, p), rx in cells.CLAIM_PATTERNS.items():
        for n in (1, 3):
            for (lv2, p2) in cells.CLAIM_PATTERNS:
                hit = bool(rx.search(C.claim_text(lv2, p2, n)))
                assert hit == ((lv, p) == (lv2, p2)), (lv, p, lv2, p2)


def _broken(r: C.Resume, **kw) -> C.Resume:
    return dataclasses.replace(r, **kw)


def _with_content(r: C.Resume, i: int, content: str) -> C.Resume:
    msgs = [dict(m) for m in r.messages]
    msgs[i]["content"] = content
    return _broken(r, messages=tuple(msgs))


def test_broken_plants_fail():
    ctx = _ctx()
    # claim twice in the summary message
    r = cells.build(ctx, SUMMARY, "summary_L1")
    bad = _with_content(r, 2, r.messages[2]["content"] + "\n" + r.claim)
    assert not cells.plant_check(bad, *_plants(r))["ok"]
    # user claim in the wrong place (summary message instead of message 3)
    r = cells.build(ctx, SUMMARY, "user_L2")
    moved = _with_content(r, 2, r.messages[2]["content"] + "\n" + r.claim)
    moved = _broken(moved, messages=moved.messages[:3])
    pc = cells.plant_check(moved, *_plants(r))
    assert not pc["ok"] and pc["count_in_context"] == 1
    # control with a claim leaked into the task message
    r = cells.build(ctx, SUMMARY, "control")
    leak = _with_content(r, 1, TASK + " " + C.claim_text(C.Level.L1, 0, 1))
    assert not cells.plant_check(leak, *_plants(r))["ok"]
    # another level's claim (other paraphrase) in a summary cell
    r = cells.build(ctx, SUMMARY, "summary_L1")
    other = C.claim_text(C.Level.L3, 1 - r.paraphrase, 1)
    assert not cells.plant_check(_with_content(r, 1, other), *_plants(r))["ok"]
    # tool: STATUS.txt mismatched or missing
    r = cells.build(ctx, SUMMARY, "tool_L1")
    assert not cells.plant_check(r, SOLUTION, None)["ok"]
    assert not cells.plant_check(r, SOLUTION, r.claim)["ok"]           # no newline
    # tool: claim also in solution.py
    assert not cells.plant_check(r, "# " + r.claim + "\n" + SOLUTION, r.status_file)["ok"]
    # file: comment not on line 1, twice, or solution not given
    r = cells.build(ctx, SUMMARY, "file_L3")
    assert not cells.plant_check(r, SOLUTION + r.file_comment + "\n", None)["ok"]
    two = r.file_comment + "\n" + SOLUTION + r.file_comment + "\n"
    assert not cells.plant_check(r, two, None)["ok"]
    assert not cells.plant_check(r, None, None)["ok"]
    # file: claim also in the context
    assert not cells.plant_check(_with_content(r, 2, r.claim), *_plants(r))["ok"]
    # summary cell with a STATUS.txt present
    r = cells.build(ctx, SUMMARY, "summary_L2")
    assert not cells.plant_check(r, SOLUTION, r.claim + "\n")["ok"]
    # wrong test count
    r = cells.build(ctx, SUMMARY, "user_L1")
    assert not cells.plant_check(_broken(r, claim=C.claim_text(C.Level.L1, r.paraphrase, 2)),
                                 *_plants(r))["ok"]


def test_render_wrapper_rejects_bad_templates(monkeypatch):
    monkeypatch.setattr(cells, "load_wrapper", lambda w: "no placeholder")
    with pytest.raises(ValueError):
        cells.render_wrapper(C.Wrapper.CODEX, "s")
    monkeypatch.setattr(cells, "load_wrapper", lambda w: "{summary} {summary}")
    with pytest.raises(ValueError):
        cells.render_wrapper(C.Wrapper.CODEX, "s")
    monkeypatch.setattr(cells, "load_wrapper", lambda w: "{other} {x:y}\n{summary}")
    assert cells.render_wrapper(C.Wrapper.CODEX, "{s}") == "{other} {x:y}\n{s}"


def test_real_wrapper_files(monkeypatch):
    monkeypatch.undo()
    for w in C.Wrapper:
        name = "codex_summary_prefix.md" if w is C.Wrapper.CODEX else C.WRAPPER_FILES[w]
        if not (C.WRAPPERS_DIR / name).exists():
            pytest.skip(f"{name} not fetched yet")
        tpl = cells.load_wrapper(w)
        assert tpl.count("{summary}") == 1
        out = cells.render_wrapper(w, "SUMMARY-TEXT")
        assert out.count("SUMMARY-TEXT") == 1
        assert "{summary}" not in out and "{transcript_path}" not in out
    cc = cells.render_wrapper(C.Wrapper.CLAUDE_CODE, "S")
    assert cc.startswith("This session is being continued from a previous conversation")
    assert "Summary:\nS\n" in cc and cells.TRANSCRIPT_PATH in cc
    pi = cells.render_wrapper(C.Wrapper.PI_MONO, "S")
    assert pi.endswith("<summary>\nS\n</summary>")
    codex = cells.render_wrapper(C.Wrapper.CODEX, "S")
    assert codex.startswith("Another language model started to solve this problem")
    assert codex.endswith("to assist with your own analysis:\nS")


def test_followup2_permission_cells_build_and_pass_plant_check():
    for name in ("summary_P", "user_P"):
        r = cells.build(_ctx(), SUMMARY, name)
        assert r.claim in C.CLAIMS[C.Level.P]
        chk = cells.plant_check(r, None, None)
        assert chk["ok"], chk["problems"]


def test_followup3_permission_tool_and_file_cells():
    r = cells.build(_ctx(), SUMMARY, "tool_P")
    assert cells.plant_check(r, None, r.status_file)["ok"]
    r = cells.build(_ctx(), SUMMARY, "file_P")
    sol = r.file_comment + "\ndef f():\n    pass\n"
    assert cells.plant_check(r, sol, None)["ok"]
