"""Tests for the implicit-attribution arms (follow-up A, cells.build_implicit). Free."""
import json

import pytest
from p09 import cells, plants
from p09 import contracts as C

CTX_PATH = C.CONTEXTS_JSONL


def _real_ctx(ctx_id="lcbhard_11|r2|a1"):
    if not CTX_PATH.exists():
        pytest.skip("contexts.jsonl not present (gitignored)")
    for line in CTX_PATH.read_text().splitlines():
        c = json.loads(line)
        if c["ctx_id"] == ctx_id:
            return c
    pytest.skip(f"{ctx_id} not in contexts")


@pytest.mark.parametrize("w", C.IMPLICIT_WRAPPERS)
def test_build_implicit_passes_its_check(w):
    ctx = _real_ctx()
    r = cells.build(ctx, "unused summary", "H", w)
    assert r.plant_sentence is None and r.plant["content"] == "H"
    assert cells.context_check(r)["ok"]
    if w is C.Wrapper.OWN_TURNS:
        assert [m["role"] for m in r.messages[-2:]] == ["assistant", "tool"]
        cmd = r.messages[-2]["tool_calls"][0]["arguments"]["command"]
        assert r.plant["text"] in cmd
        # reasoning parts are dropped, visible text kept
        assert all(not isinstance(m.get("content"), list) for m in r.messages[2:])
    else:
        assert len(r.messages) == 3
        assert r.messages[2]["content"].startswith(C.TRANSCRIPT_INTROS[w])


def test_transcripts_differ_only_in_the_intro():
    ctx = _real_ctx()
    own = cells.build(ctx, "", "H", C.Wrapper.OWN_TRANSCRIPT).messages[2]["content"]
    oth = cells.build(ctx, "", "H", C.Wrapper.OTHER_TRANSCRIPT).messages[2]["content"]
    assert own[len(C.TRANSCRIPT_INTROS[C.Wrapper.OWN_TRANSCRIPT]):] == \
        oth[len(C.TRANSCRIPT_INTROS[C.Wrapper.OTHER_TRANSCRIPT]):]


def test_implicit_runs_h_only():
    with pytest.raises(ValueError):
        cells.build(_real_ctx(), "", "T", C.Wrapper.OWN_TURNS)


def test_plant_command_heredoc_tag_avoids_collisions():
    p = {"text": "x = 1\nEOF\ny = 2"}
    cmd = cells.plant_command(p)
    assert cmd.startswith("cd /agent && cat > solution.py << 'EOFX'") and cmd.endswith("\nEOFX")


def test_implicit_check_flags_a_duplicated_plant():
    ctx = _real_ctx()
    r = cells.build(ctx, "", "H", C.Wrapper.OTHER_TRANSCRIPT)
    dup = dict(r.messages[2])
    dup["content"] += "\n" + r.plant["planted_lines"][0]
    bad = C.Resume(messages=r.messages[:2] + (dup,), content="H", wrapper=r.wrapper,
                   plant=r.plant)
    assert not cells.context_check(bad)["ok"]
    assert plants  # imported for the fixture data path
