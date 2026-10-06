"""U4: the X3 Inspect task's pieces that run without Docker (implementation plan D5)."""
from __future__ import annotations

import pytest
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    ContentReasoning,
    ContentText,
)

from x3 import contracts as C
from x3 import task as T


def _spec(**kw):
    base = {"episode_id": "m|smoke|B|neutral|t1|r1|a1", "model_id": "mockllm/model",
                "model_slug": "mock", "context": "neutral", "prompt_level": "B", "stage": "smoke",
                "task_id": "t1", "repeat": 1, "attempt": 1, "batch_id": "b1", "run_order_pos": 0,
                "episode_limit_usd": 0.5, "in_ceiling": 0.32, "out_ceiling": 3.2,
                "task": {"task_id": "t1", "entry_point": "f", "description_md": "d",
                      "solution_py": "def f(x):\n    pass\n", "test_solution_py": "t",
                      "test_sha256": "0", "conflict_pair": [3, 4]}}
    base.update(kw)
    return T.EpisodeSpec(**base)


def test_deliver_truncates_at_the_cap():
    text, cut = T.deliver("x" * (C.OUTPUT_TRUNCATE_CHARS + 10), "")
    assert cut and text.startswith("x" * C.OUTPUT_TRUNCATE_CHARS) and "truncated" in text
    text, cut = T.deliver("ok", "warn")
    assert not cut and text == "warn\nok"


def test_affordable_max_tokens_bounds_spend_including_retries():
    # room per attempt = (0.5 - 0.1)/3 - 10_000*0.32e-6 = 0.1301 -> 40_666 tokens -> cap 16k
    assert T.affordable_max_tokens(0.1, 0.5, 10_000, 0.32, 3.2, 16_000) == 16_000
    for spent in (0.0, 0.2, 0.4, 0.45, 0.48):
        mt = T.affordable_max_tokens(spent, 0.5, 10_000, 0.32, 3.2, 16_000)
        # every attempt of a call at that cap, together, cannot exceed the limit
        assert spent + T.call_bound(10_000, mt, 0.32, 3.2) <= 0.5 + 1e-12
    assert T.affordable_max_tokens(0.6, 0.5, 10, 0.32, 3.2, 16_000) == 0


def test_missing_usage_is_charged_the_whole_bound():
    from inspect_ai.model import ModelOutput

    out = ModelOutput.from_content("m", "x")
    out.usage = None
    assert T.call_cost(out, 0.32, 3.2, bound=0.123) == 0.123


def test_first_call_over_a_tiny_limit_is_refused():
    msgs = [ChatMessageSystem(content="s" * 2000), ChatMessageUser(content="u" * 2000)]
    ib = T.input_bound_tokens(msgs, T.TOOLS)
    assert T.affordable_max_tokens(0.0, 0.001, ib, 0.32, 3.2, 16_000) < C.MIN_CALL_MAX_TOKENS


def test_input_bound_grows_with_context_and_counts_unicode_bytes():
    a = [ChatMessageUser(content="hello")]
    b = a + [ChatMessageAssistant(content="def f():\n    return {'a': [1, 2]}\n" * 50)]
    assert T.input_bound_tokens(b, T.TOOLS) > T.input_bound_tokens(a, T.TOOLS)
    uni = [ChatMessageUser(content="é" * 100)]
    asc = [ChatMessageUser(content="e" * 100)]
    assert T.input_bound_tokens(uni, []) - T.input_bound_tokens(asc, []) == 100


def test_input_bound_is_at_least_a_crude_token_count():
    # whitespace/punctuation split over-counts nothing a BPE would merge across bytes
    text = "for i in range(10):\n    x[i] = (i * 3) % 7  # comment ünïcode\n" * 40
    msgs = [ChatMessageUser(content=text)]
    assert T.input_bound_tokens(msgs, []) >= len(text.encode("utf-8"))


def test_reasoning_text_readable_and_redacted():
    m = ChatMessageAssistant(content=[ContentReasoning(reasoning="think"),
                                      ContentText(text="hi")])
    assert T.reasoning_text(m) == ("think", False)
    r = ChatMessageAssistant(content=[ContentReasoning(reasoning="xx", redacted=True)])
    assert T.reasoning_text(r) == ("", True)
    assert T.reasoning_text(ChatMessageAssistant(content="plain")) == ("", False)


def test_task_builds_without_docker():
    t = T.x3_task([_spec(), _spec(episode_id="m|smoke|B|cap_script|t1|r1|a1",
                                  context="cap_script")])
    assert len(t.dataset) == 2
    assert t.dataset[1].metadata["context"] == "cap_script"


def test_tools_are_execute_command_and_zero_arg_done():
    from inspect_ai.tool import ToolDef

    defs = {ToolDef(t).name: ToolDef(t) for t in T.TOOLS}
    assert set(defs) == {"execute_command", "done"}
    assert list(defs["done"].parameters.properties) == []
    assert list(defs["execute_command"].parameters.properties) == ["command"]


def test_retries_are_ours_and_each_failed_attempt_is_charged(monkeypatch):
    import asyncio

    from inspect_ai.model import ModelOutput

    calls = []

    async def flaky(messages, tools, max_tokens, tool_choice=None):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("503")
        return ModelOutput.from_content("m", "ok")

    async def nosleep(_):
        return None

    monkeypatch.setattr(T, "_generate", flaky)
    out, extra, err = asyncio.run(T.generate_with_retries([], [], 100, 0.01, sleep=nosleep))
    assert out is not None and len(calls) == 3 and extra == 0.02 and "503" in err
    calls.clear()

    async def dead(*a, **k):
        calls.append(1)
        raise RuntimeError("down")

    monkeypatch.setattr(T, "_generate", dead)
    out, extra, _ = asyncio.run(T.generate_with_retries([], [], 100, 0.01, sleep=nosleep))
    assert out is None and len(calls) == T.ATTEMPTS and extra == pytest.approx(0.03)


def test_tighter_bound_after_a_call():
    msgs = [ChatMessageUser(content="x" * 40_000), ChatMessageAssistant(content="y" * 4_000)]
    byte = T.input_bound_tokens(msgs, [])
    assert T.tighter_bound(byte, None, msgs) == byte
    # previous call: 9,000 input tokens and 1,200 output; then a 500-byte tool result added
    later = msgs + [ChatMessageUser(content="z" * 500)]
    b = T.tighter_bound(T.input_bound_tokens(later, []), (9_000, 1_200, len(msgs)), later)
    assert b == 9_000 + 1_200 + T.input_bound_tokens(later[2:], []) + T.INCREMENTAL_MARGIN_TOKENS
    assert b < T.input_bound_tokens(later, [])
    # never looser than the byte bound
    assert T.tighter_bound(10, (9_000, 1_200, 1), msgs) == 10


def test_probe_fits_after_a_long_qwen_episode():
    # 60k tokens of context at Qwen's ceilings: the question must still be askable
    pmt = T.affordable_max_tokens(0.0, C.PROBE_BOUND_USD, 60_000, 0.32, 3.2, C.PROBE_MAX_TOKENS)
    assert pmt >= C.MIN_PROBE_MAX_TOKENS


def test_inspect_retries_are_off_in_every_request():
    import inspect

    src = inspect.getsource(T._generate)
    assert "max_retries=0" in src
