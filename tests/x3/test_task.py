"""U4: the X3 Inspect task's pieces that run without Docker (implementation plan D5)."""
from __future__ import annotations

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


def test_affordable_max_tokens_bounds_spend():
    # room = 0.5 - 0.1 - 10_000*0.32e-6 = 0.3968 -> /3.2e-6 = 124_000 -> capped at 16k
    assert T.affordable_max_tokens(0.1, 0.5, 10_000, 0.32, 3.2, 16_000) == 16_000
    mt = T.affordable_max_tokens(0.49, 0.5, 10_000, 0.32, 3.2, 16_000)
    assert mt == 2_125   # (0.5-0.49-0.0032)/3.2e-6 = 2125
    # a call at that cap cannot exceed the limit
    assert 0.49 + 10_000 * 0.32e-6 + mt * 3.2e-6 <= 0.5 + 1e-12
    assert T.affordable_max_tokens(0.6, 0.5, 10, 0.32, 3.2, 16_000) == 0


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
