"""Pure tests of p09/agent.py (no Docker, no model calls)."""
import json

import pytest
from inspect_ai.model import ChatMessageSystem, ChatMessageUser
from p09 import agent, cells
from p09 import contracts as C

from .helpers import TASKS, fake_ctx


def _spec(content="T", wrapper="codex"):
    ctx = fake_ctx()
    ctx["prefix_messages"] = [ChatMessageSystem(content="SYS").model_dump(exclude_none=True),
                              ChatMessageUser(content="TASK").model_dump(exclude_none=True)]
    ctx["snapshot_sha256"] = {p: f["sha256"] for p, f in ctx["files"].items()}
    r = cells.build(ctx, "summary", content, wrapper, TASKS)
    return agent.spec_from(ctx, r, C.GLM, sample=1)


def test_spec_from_ids_and_round_trip():
    sp = _spec("H", "codex_self")
    assert sp.episode_id == f"P09|glm-5.3-flash|codex_self|H|{sp.ctx_id}|s1"
    assert sp.plant["content"] == "H" and sp.plant_sentence
    r = agent.resume_of(sp)
    assert r.plant == sp.plant and cells.context_check(r)["ok"]
    msgs = agent.to_messages(sp.initial_messages)
    assert [m.role for m in msgs] == ["system", "user", "user"]


def test_as_mock_relabels():
    m = agent.as_mock(_spec())
    assert m.model_slug == "mock" and m.model_id == agent.MOCK_MODEL
    assert m.episode_id.startswith("P09|mock|")


def test_replay_compare_union_and_missing():
    assert agent.replay_compare({"a": "1", "b": "2"}, {"a": "1", "b": "2"}) == []
    assert agent.replay_compare({"a": "1"}, {"a": "1", "b": "2"}) == ["b"]
    assert agent.replay_compare({"a": "1", "c": "3"}, {"a": "9"}) == ["a", "c"]


def test_billed_total():
    assert agent.billed_total([{"billed_usd": 0.1}, {"billed_usd": 0.2}]) == pytest.approx(0.3)
    assert agent.billed_total([{"billed_usd": None, "error": None}]) is None
    assert agent.billed_total([{"billed_usd": None, "error": "x"}]) == 0


def test_write_new_never_overwrites(tmp_path):
    p = tmp_path / "x.s0.json"
    agent._write_new({"a": 1}, p)
    q = agent._write_new({"a": 2}, p)
    assert q.name == "x.s0.dup1.json" and json.loads(p.read_text()) == {"a": 1}
    assert json.loads(q.read_text())["duplicate_of"] == str(p)


def test_task_rejects_duplicates_and_empty():
    sp = _spec()
    with pytest.raises(ValueError, match="duplicate"):
        agent.p09_task([sp, sp])
    with pytest.raises(ValueError, match="empty"):
        agent.p09_task([])


def test_generate_settings_and_model_guard(tmp_path):
    gs = agent.generate_settings(C.DEEPSEEK)
    assert gs.get("reasoning_effort") == "medium"
    bad = C.Model("x/unknown", "u", 1, 1, {}, 0.1, {})
    with pytest.raises(ValueError, match="not in x3"):
        agent.run_batch([_spec()], bad, tmp_path)
