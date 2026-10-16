"""Tests for p49.loop (plan "Automatic rules": context contract, chain mechanics) with a fake
`chat` and fake token counts, on real shim sandboxes; every container is checked gone."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid

import pytest
from p49 import contracts as C
from p49 import loop

from x3.tasks import load_manifest

TASKS = load_manifest().tasks
TOOLS = [{"type": "function", "function": {"name": n, "description": n,
                                           "parameters": {"type": "object", "properties": {}}}}
         for n in ("execute_bash", "str_replace_editor", "think", "task_tracker", "finish")]
START = [{"role": "system", "content": "You are OpenHands agent."},
         {"role": "user", "content": C.transition_text("A")}]
PER_MESSAGE = 100


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", C.IMAGE], capture_output=True,
                              timeout=30, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="docker or x3-sandbox:pilot unavailable")


@pytest.fixture(autouse=True)
def _fresh_calibration():
    saved = (C.CALIB.c, C.CALIB.n, list(C.CALIB.ratios))
    C.CALIB.c, C.CALIB.n, C.CALIB.ratios = C.C_INIT, 0, []
    yield
    C.CALIB.c, C.CALIB.n, C.CALIB.ratios = saved


def _gone(chain_id: str) -> bool:
    prefix = C.SANDBOX_PREFIX + chain_id.replace("/", "_")
    r = subprocess.run(["docker", "ps", "-a", "--filter", f"name={prefix}-", "--format",
                        "{{.Names}}"], capture_output=True, text=True, timeout=60, check=True)
    return r.stdout.strip() == ""


def _spec(beliefs: bool = True) -> C.ChainSpec:
    cid = f"t{uuid.uuid4().hex[:8]}-lcb-R-A"
    return C.ChainSpec(chain_id=cid, history_id="h1", task_id=TASKS[0].task_id, arm=C.R,
                       level="A", design="gate0", beliefs=beliefs)


def _resp(content=None, tool_calls=None, cost=0.01, prompt_tokens=1000) -> C.Response:
    msg = {"role": "assistant", "content": content, "refusal": None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return C.Response(message=msg, finish_reason="tool_calls" if tool_calls else "stop",
                      prompt_tokens=prompt_tokens, completion_tokens=50, cost_usd=cost,
                      cost_from_usage=True, provider="Google", latency_s=0.01, attempts=1,
                      gen_id="gen-x")


def _tc(name: str, args: dict, i: int = 0) -> dict:
    return {"id": f"call_{uuid.uuid4().hex[:6]}_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class FakeChat:
    """Scripted agent replies (a list, or a function of the turn) and belief answers."""

    def __init__(self, agent, belief_text="Reasoning.\nANSWER: 37", belief_cost=0.001):
        self.agent = agent
        self.belief_text = belief_text
        self.belief_cost = belief_cost
        self.bodies: list[tuple[str, dict, int]] = []
        self.n_agent = 0

    async def __call__(self, body, *, kind, chain_id, our_tokens):
        self.bodies.append((kind, body, our_tokens))
        if kind.startswith("belief"):
            return _resp(content=self.belief_text, cost=self.belief_cost)
        self.n_agent += 1
        r = self.agent(self.n_agent) if callable(self.agent) else self.agent[self.n_agent - 1]
        if isinstance(r, BaseException):
            raise r
        if asyncio.iscoroutine(r):
            return await r
        return r

    def agent_bodies(self):
        return [b for k, b, _ in self.bodies if k == "agent"]


def _count(start_tokens: int):
    def count(messages, tools):
        return start_tokens

    def count_messages(messages):
        return PER_MESSAGE * len(messages)

    return count, count_messages


def _run(spec, chat, start_tokens=10_000, **kw) -> C.ChainRecord:
    count, count_messages = _count(start_tokens)
    return asyncio.run(loop.run_chain(spec, START, TOOLS, TASKS[0], chat=chat, count=count,
                                      count_messages=count_messages, **kw))


# --- unit -----------------------------------------------------------------------------------
def test_parse_answer():
    assert loop.parse_answer("x\nANSWER: 12\nmore\nANSWER: 85") == 85
    assert loop.parse_answer("**ANSWER:** 40.6") == 41
    assert loop.parse_answer("ANSWER: 140") is None
    assert loop.parse_answer("no answer") is None and loop.parse_answer(None) is None


def test_nudge_names_finish():
    assert loop.NUDGE == "Continue with your tools, or call `finish` if you are finished."


# --- chains ---------------------------------------------------------------------------------
def test_multiple_calls_nudge_finish_and_beliefs():
    spec = _spec()
    script = [
        _resp("Two things.", [_tc("execute_bash", {"command": "echo '# x' >> solution.py"}, 0),
                              _tc("str_replace_editor", {"command": "view",
                                                         "path": "/agent/solution.py"}, 1)]),
        _resp("Just talking."),
        _resp(None, [_tc("execute_bash", {"command": "touch /agent/a.txt"}, 0),
                     _tc("finish", {"message": "done"}, 1),
                     _tc("execute_bash", {"command": "touch /agent/never.txt"}, 2)]),
    ]
    chat = FakeChat(script)
    rec = _run(spec, chat)
    assert rec.end_reason == C.END_DONE and rec.n_turns == 3
    roles = [m["role"] for m in rec.transcript]
    assert roles == ["assistant", "tool", "tool", "assistant", "user", "assistant", "tool", "tool"]
    assert rec.transcript[4]["content"] == loop.NUDGE
    assert rec.transcript[7]["content"] == ""                      # finish
    assert "refusal" not in rec.transcript[0]
    ids = [tc["id"] for tc in rec.transcript[0]["tool_calls"]]
    assert [m["tool_call_id"] for m in rec.transcript[1:3]] == ids
    assert rec.transcript[2]["content"].startswith("Here's the result of running `cat -n`")
    # executed calls, the skipped one after finish, and a snapshot after each mutating call
    names = [(c["turn"], c["index"], c["name"], c.get("skipped")) for c in rec.tool_calls]
    assert names == [(1, 0, "execute_bash", None), (1, 1, "str_replace_editor", None),
                     (3, 0, "execute_bash", None), (3, 1, "finish", None),
                     (3, 2, "execute_bash", "after_finish")]
    assert [(s["step"], s["turn"]) for s in rec.snapshots] == [(0, 0), (1, 1), (2, 3)]
    assert "/agent/a.txt" in rec.snapshots[2]["files"]
    assert not any("never" in p for p in rec.snapshots[-1]["files"])
    assert rec.final_solution_py == TASKS[0].solution_py + "# x\n"
    # beliefs: forked on the start context, tools declared, tool_choice none, never in transcript
    assert rec.beliefs["B1"]["value"] == 37 and rec.beliefs["B3"]["value"] == 37
    for k, body, _ in chat.bodies:
        assert body["transforms"] == [] and body["provider"] == C.PROVIDER
        assert body["model"] == C.MODEL and body["tools"] == TOOLS
        if k.startswith("belief"):
            assert body["tool_choice"] == "none" and body["max_tokens"] == C.RESERVE["belief"]
            assert body["messages"][:-1] == START
            assert body["messages"][-1]["content"] == C.BELIEFS[k.split(":")[1]]
        else:
            assert "tool_choice" not in body
    agent = chat.agent_bodies()
    assert agent[0]["messages"] == START
    assert len(agent[1]["messages"]) == len(START) + 3
    assert agent[2]["messages"][-1]["content"] == loop.NUDGE
    # our count: start once + count_messages of what was appended
    ours = [o for k, _, o in chat.bodies if k == "agent"]
    assert ours == [10_000, 10_000 + 3 * PER_MESSAGE, 10_000 + 5 * PER_MESSAGE]
    assert rec.start_tokens == 10_000 and rec.peak_prompt_tokens == 1000
    assert [c.kind for c in rec.calls].count("agent") == 3 and len(rec.calls) == 5
    assert rec.cost_usd == pytest.approx(0.03 + 0.002)
    assert _gone(spec.chain_id)


def test_turn_cap():
    spec = _spec(beliefs=False)
    chat = FakeChat(lambda n: _resp(None, [_tc("think", {"thought": f"t{n}"})]))
    rec = _run(spec, chat)
    assert rec.end_reason == C.END_TURN_CAP and rec.n_turns == C.TURN_CAP
    assert chat.n_agent == C.TURN_CAP and rec.beliefs == {}
    assert len(rec.snapshots) == 1                                # think does not snapshot
    assert _gone(spec.chain_id)


def test_chain_cap_shrinks_then_ends():
    spec = _spec(beliefs=False)
    start = 200_000
    costs = [1.545, 0.01, 0.01]
    chat = FakeChat(lambda n: _resp(None, [_tc("think", {"thought": "x"})], cost=costs[n - 1]))
    rec = _run(spec, chat, start_tokens=start)
    bodies = chat.agent_bodies()
    assert rec.end_reason == C.END_CHAIN_CAP and len(bodies) == 2
    assert bodies[0]["max_tokens"] == C.RESERVE["agent"]
    ours2 = start + 2 * PER_MESSAGE
    want = loop.cap_max_tokens(1.545, ours2)
    assert C.MIN_AGENT_MAX_TOKENS <= want < C.RESERVE["agent"]
    assert bodies[1]["max_tokens"] == want
    assert rec.cost_usd + loop.worst_case_usd(start + 4 * PER_MESSAGE,
                                              C.MIN_AGENT_MAX_TOKENS) > C.CHAIN_CAP_USD
    assert _gone(spec.chain_id)


def test_window_shrink_on_a_255k_start():
    spec = _spec(beliefs=False)
    chat = FakeChat([_resp(None, [_tc("finish", {"message": "x"})])])
    rec = _run(spec, chat, start_tokens=255_000)
    body = chat.agent_bodies()[0]
    assert body["max_tokens"] == C.CALIB.room(255_000) < C.RESERVE["agent"]
    assert body["max_tokens"] >= C.MIN_AGENT_MAX_TOKENS
    assert rec.end_reason == C.END_DONE
    assert _gone(spec.chain_id)


def test_window_full_without_sending_at_258k():
    spec = _spec(beliefs=True)
    chat = FakeChat([_resp("never sent")])
    rec = _run(spec, chat, start_tokens=258_000)
    assert rec.end_reason == C.END_WINDOW and chat.n_agent == 0 and rec.n_turns == 0
    # beliefs (reserve 512) still fit at 258k; at 261k they are skipped:window
    assert rec.beliefs["B1"]["status"] == "ok"
    assert _gone(spec.chain_id)
    spec2 = _spec(beliefs=True)
    chat2 = FakeChat([])
    rec2 = _run(spec2, chat2, start_tokens=261_000)
    assert rec2.end_reason == C.END_WINDOW and chat2.bodies == []
    assert {v["status"] for v in rec2.beliefs.values()} == {"skipped:window"}
    assert _gone(spec2.chain_id)


def test_api_error_ends_chain_and_belief_error_does_not():
    spec = _spec(beliefs=True)
    chat = FakeChat([_resp(None, [_tc("think", {"thought": "a"})]), C.ApiError("boom 500")])
    orig = chat.__call__

    async def chat_with_bad_b3(body, *, kind, chain_id, our_tokens):
        if kind == "belief:B3":
            raise C.ApiError("belief failed")
        return await orig(body, kind=kind, chain_id=chain_id, our_tokens=our_tokens)

    rec = _run(spec, chat_with_bad_b3)
    assert rec.end_reason == C.END_API and rec.n_turns == 1 and "boom 500" in rec.error
    assert rec.beliefs["B3"]["status"] == "api_error" and rec.beliefs["B1"]["value"] == 37
    errs = [c for c in rec.calls if c.error]
    assert {c.kind for c in errs} == {"belief:B3", "agent"}
    assert _gone(spec.chain_id)


def test_deadline_before_and_during():
    spec = _spec(beliefs=True)
    chat = FakeChat([])
    rec = _run(spec, chat, deadline_epoch=time.time() - 1)
    assert rec.end_reason == C.END_DEADLINE and chat.bodies == []
    assert {v["status"] for v in rec.beliefs.values()} == {"skipped:deadline"}
    assert len(rec.snapshots) == 1 and _gone(spec.chain_id)

    async def slow(n):
        await asyncio.sleep(1.5)
        return _resp(None, [_tc("think", {"thought": "x"})])

    spec2 = _spec(beliefs=False)
    chat2 = FakeChat(lambda n: slow(n))
    rec2 = _run(spec2, chat2, deadline_epoch=time.time() + 1.0)
    assert rec2.end_reason == C.END_DEADLINE and rec2.n_turns == 1
    assert _gone(spec2.chain_id)


def test_cancellation_returns_deadline_record_with_snapshots():
    spec = _spec(beliefs=False)

    async def hang(n):
        await asyncio.sleep(3600)

    def agent(n):
        if n == 1:
            return _resp(None, [_tc("execute_bash", {"command": "echo hi > /agent/x.txt"})])
        return hang(n)

    chat = FakeChat(agent)
    count, count_messages = _count(1000)

    async def go():
        t = asyncio.create_task(loop.run_chain(spec, START, TOOLS, TASKS[0], chat=chat,
                                               count=count, count_messages=count_messages))
        while chat.n_agent < 2:
            await asyncio.sleep(0.05)
        t.cancel()
        return await t

    rec = asyncio.run(go())
    assert rec.end_reason == C.END_DEADLINE and rec.error == "cancelled"
    assert len(rec.snapshots) == 2 and "/agent/x.txt" in rec.snapshots[1]["files"]
    assert rec.final_solution_py == TASKS[0].solution_py
    assert _gone(spec.chain_id)


def test_unexpected_exception_is_end_error():
    spec = _spec(beliefs=False)
    chat = FakeChat([ValueError("not an api error")])
    rec = _run(spec, chat)
    assert rec.end_reason == C.END_ERROR and "ValueError: not an api error" in rec.error
    assert _gone(spec.chain_id)


def test_record_roundtrip():
    spec = _spec(beliefs=True)
    chat = FakeChat([_resp(None, [_tc("execute_bash", {"command": "ls"}),
                                  _tc("finish", {"message": "m"}, 1)])])
    rec = _run(spec, chat)
    d = loop.record_to_dict(rec)
    back = loop.record_from_dict(json.loads(json.dumps(d)))
    assert back == rec and isinstance(back.calls[0], C.CallRecord)
    assert _gone(spec.chain_id)
