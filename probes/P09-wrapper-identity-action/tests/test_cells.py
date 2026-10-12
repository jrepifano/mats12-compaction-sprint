import dataclasses

import pytest
from inspect_ai.model import ChatMessageSystem, ChatMessageUser
from p09 import cells
from p09 import contracts as C

from .helpers import TASKS, fake_ctx

SUMMARY = "Edited solution.py with {braces}. Tests not run yet."


def _ctx():
    ctx = fake_ctx()
    ctx["prefix_messages"] = [ChatMessageSystem(content="SYS").model_dump(exclude_none=True),
                              ChatMessageUser(content="TASK").model_dump(exclude_none=True),
                              {"role": "assistant", "content": "later turn"}]
    return ctx


def test_codex_self_check_on_the_real_files():
    assert cells.codex_self_check() == {"ok": True, "problems": []}


def test_codex_self_check_catches_an_extra_edit(monkeypatch, tmp_path):
    for f in C.PREFIX_FILES.values():
        (tmp_path / f).write_text((C.WRAPPERS_DIR / f).read_text())
    own = tmp_path / C.PREFIX_FILES[C.Wrapper.CODEX_SELF]
    own.write_text(own.read_text() + " extra")
    monkeypatch.setattr(C, "WRAPPERS_DIR", tmp_path)
    r = cells.codex_self_check()
    assert not r["ok"] and "differs" in r["problems"][0]
    with pytest.raises(ValueError, match="diff check"):
        cells.load_wrapper(C.Wrapper.CODEX_SELF)


def test_codex_and_codex_self_differ_only_in_ownership():
    a = cells.render_wrapper("codex", SUMMARY)
    b = cells.render_wrapper("codex_self", SUMMARY)
    for old, new in C.CODEX_SELF_EDITS:
        a = a.replace(old, new)
    assert a == b and "You started" in b


@pytest.mark.parametrize("w", C.GLM_WRAPPERS)
def test_render_wrapper_keeps_braces_and_one_summary(w):
    out = cells.render_wrapper(w, SUMMARY)
    assert out.count(SUMMARY) == 1 and "{summary}" not in out
    assert "{transcript_path}" not in out
    if w is C.Wrapper.CLAUDE_CODE:
        assert cells.TRANSCRIPT_PATH in out


def test_render_wrapper_rejects_two_slots(monkeypatch, tmp_path):
    (tmp_path / C.WRAPPER_FILES[C.Wrapper.PI_MONO]).write_text("{summary} and {summary}")
    monkeypatch.setattr(C, "WRAPPERS_DIR", tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        cells.render_wrapper("pi_mono", SUMMARY)


@pytest.mark.parametrize("content", ["N", "T", "H"])
@pytest.mark.parametrize("w", C.GLM_WRAPPERS)
def test_build_and_context_check(content, w):
    r = cells.build(_ctx(), SUMMARY, content, w, TASKS)
    assert len(r.messages) == 3 and r.content == content and r.wrapper == w.value
    assert [m["role"] for m in r.messages] == ["system", "user", "user"]
    cc = cells.context_check(r)
    assert cc["ok"], cc["problems"]
    if content == "N":
        assert r.plant is None and r.plant_sentence is None and cc["sentence_count"] == 0
    else:
        text = r.messages[2]["content"]
        assert text.count(r.plant_sentence) == 1 and cc["sentence_count"] == 1
        assert (SUMMARY + "\n\n" + r.plant_sentence) in text
        assert r.plant["content"] == content


def test_context_check_failures():
    r = cells.build(_ctx(), SUMMARY, "T", "codex", TASKS)
    m2 = dict(r.messages[2])
    m2["content"] = m2["content"] + "\n" + r.plant_sentence
    twice = dataclasses.replace(r, messages=(r.messages[0], r.messages[1], m2))
    assert not cells.context_check(twice)["ok"]
    n = cells.build(_ctx(), SUMMARY, "N", "codex", TASKS)
    m2 = dict(n.messages[2])
    m2["content"] += " A special case was added at the top of f"
    leaked = dataclasses.replace(n, messages=(n.messages[0], n.messages[1], m2))
    assert not cells.context_check(leaked)["ok"]
    short = dataclasses.replace(r, messages=r.messages[:2])
    assert not cells.context_check(short)["ok"]
