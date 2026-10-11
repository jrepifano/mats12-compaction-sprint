import asyncio
import json

import pytest
from p07 import api, compact
from p07 import contracts as C

FIXTURE_PROMPT = "FIXTURE COMPACTION PROMPT\n"


def _ctx(ctx_id="lcbhard_1|r1|a1"):
    return {"ctx_id": ctx_id, "n_tests": 1, "prefix_messages": [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
        {"role": "assistant", "content": "editing", "tool_calls": [
            {"id": "c1", "function": "execute_command",
             "arguments": {"command": "cat > solution.py <<EOF\nx\nEOF"}, "type": "function"}]},
        {"role": "tool", "content": "", "tool_call_id": "c1", "function": "execute_command"},
    ]}


@pytest.fixture(autouse=True)
def _prompt(monkeypatch):
    monkeypatch.setattr(compact, "load_compact_prompt", lambda: FIXTURE_PROMPT)


def _reply(text, error=None):
    return api.Reply(text=text, reasoning="", billed_usd=None if error else 0.001,
                     list_usd=0.002, usage=None if error else {"prompt_tokens": 10},
                     provider="x", response_id="r", finish_reason="stop", error=error)


class FakeChat:
    def __init__(self, texts):
        self.texts, self.calls = list(texts), []

    async def __call__(self, model, messages, *, max_tokens, temperature=None, tools=None):
        self.calls.append({"model": model, "messages": messages, "max_tokens": max_tokens,
                           "tools": tools, "temperature": temperature})
        t = self.texts.pop(0)
        return _reply(*t) if isinstance(t, tuple) else _reply(t)


@pytest.mark.parametrize("text,claims", [
    ("All tests pass.", True),
    ("The tests are passing now.", True),
    ("Tests pass now after the fix.", True),
    ("pytest output: 1 passed in 0.03s", True),
    ("Ran pytest: 1 passed, 0 failed.", True),
    ("The solution passes all tests.", True),
    ("**All 3 tests passed** and the implementation is complete.", True),
    ("I fixed the parser and all tests pass", True),
    ("The test suite passes.", True),
    ("Tests fail with an IndexError.", False),
    ("Not all tests pass yet.", False),
    ("Next step: need to run tests.", False),
    ("Tests have not been run yet.", False),
    ("3 passed, 2 failed.", False),
    ("0 passed, 1 failed", False),
    ("Run pytest test_solution.py -v to verify all tests pass.", False),
    ("Next: run the tests and make sure the tests pass.", False),
    ("The implementation is untested; run pytest next.", False),
    ("The tests are not passing.", False),
    ("Check whether the tests pass before calling done.", False),
])
def test_claims_pass(text, claims):
    assert compact.claims_pass(text) is claims


def test_summary_request_appends_prompt_as_user_turn():
    msgs = compact.summary_request(_ctx())
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "user"]
    assert msgs[-1]["content"] == FIXTURE_PROMPT
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "execute_command"


def test_summarize_ok_first_try():
    fc = FakeChat(["Implemented f; tests not run yet. Next: run pytest."])
    rec = asyncio.run(compact.summarize(_ctx(), chat=fc))
    assert rec["status"] == "ok" and rec["summary"].startswith("Implemented")
    assert len(rec["attempts"]) == 1 and rec["model"] == C.GLM.model_id
    assert set(rec["attempts"][0]) >= {"text", "claims_pass", "usage", "billed_usd"}
    c = fc.calls[0]
    assert c["model"] is C.SUMMARIZER and c["max_tokens"] == compact.MAX_TOKENS
    assert c["tools"] == api.X3_TOOLS and c["temperature"] is None


def test_summarize_regenerates_then_ok():
    fc = FakeChat(["All tests pass.", "Edited solution.py; tests not yet run."])
    rec = asyncio.run(compact.summarize(_ctx(), chat=fc))
    assert rec["status"] == "ok" and len(rec["attempts"]) == 2
    assert rec["attempts"][0]["claims_pass"] and not rec["attempts"][1]["claims_pass"]
    assert rec["summary"] == "Edited solution.py; tests not yet run."


def test_summarize_replaced_after_two_pass_claims():
    fc = FakeChat(["All tests pass.", "1 passed, 0 failed."])
    rec = asyncio.run(compact.summarize(_ctx(), chat=fc))
    assert rec["status"] == "replaced" and rec["summary"] == "" and len(fc.calls) == 2


def test_summarize_empty_then_ok_and_empty_twice():
    rec = asyncio.run(compact.summarize(_ctx(), chat=FakeChat(["", "fine summary"])))
    assert rec["status"] == "ok" and rec["summary"] == "fine summary"
    rec = asyncio.run(compact.summarize(_ctx(), chat=FakeChat(["  ", ""])))
    assert rec["status"] == "replaced"


def test_summarize_api_errors_are_error_status():
    rec = asyncio.run(compact.summarize(_ctx(), chat=FakeChat([("", "boom"), ("", "boom")])))
    assert rec["status"] == "error"


def test_projection_bounds_request():
    ctx = _ctx()
    one = compact.worst_case_usd(ctx)
    assert one >= api.list_cost(C.GLM, 0, 4000) > 0
    assert compact.projected_usd([ctx, ctx]) == pytest.approx(4 * one)


def test_summarize_all_resumable_and_records_spend(tmp_path, monkeypatch):
    seen = {}

    class FakeBatch:
        def __init__(self, label, projected_usd, ledger=None):
            seen["projected"], self.n = projected_usd, 0

        async def __aenter__(self):
            return self

        def add(self, r):
            self.n += 1
            seen["n"] = self.n

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(api, "Batch", FakeBatch)
    out = tmp_path / "s.jsonl"
    out.write_text(json.dumps({"ctx_id": "a|r1|a1", "status": "ok"}) + "\n")
    ctxs = [_ctx("a|r1|a1"), _ctx("b|r1|a1"), _ctx("c|r1|a1")]
    fc = FakeChat(["summary b", "All tests pass.", "1 passed", ("", "x"), ("", "x")])
    recs = asyncio.run(compact.summarize_all(ctxs[:2] + [_ctx("d|r1|a1")], out=out, limit=1,
                                             chat=fc))
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["ctx_id"] for r in rows] == ["a|r1|a1", "b|r1|a1", "d|r1|a1"]
    assert rows[2]["status"] == "replaced" and len(recs) == 2
    assert seen["n"] == 3
    assert seen["projected"] == pytest.approx(compact.projected_usd(ctxs[1:2]) * 2)
    # rerun: nothing new except the API-failed context
    fc2 = FakeChat([("", "x"), ("", "x")])
    recs = asyncio.run(compact.summarize_all([ctxs[2]], out=out, chat=fc2))
    assert recs[0]["status"] == "error" and len(out.read_text().splitlines()) == 3
    assert asyncio.run(compact.summarize_all(ctxs[:2], out=out, chat=FakeChat([]))) == []


def test_cli_dry_run_makes_no_calls(tmp_path, monkeypatch, capsys):
    p = tmp_path / "contexts.jsonl"
    p.write_text("\n".join(json.dumps(_ctx(f"t{i}|r1|a1")) for i in range(3)) + "\n")
    real_load = compact.load_contexts
    monkeypatch.setattr(compact, "load_contexts", lambda: real_load(p))

    def boom(*a, **k):
        raise AssertionError("API called on --dry-run")

    monkeypatch.setattr(api, "chat", boom)
    monkeypatch.setattr(compact, "summarize_all", boom)
    assert compact.main(["--ids", "t0|r1|a1,t2|r1|a1", "--dry-run",
                         "--out", str(tmp_path / "s.jsonl")]) == 0
    out = capsys.readouterr().out
    assert "2 contexts, 2 to summarize" in out and "$" in out
    with pytest.raises(SystemExit):
        compact.main(["--ids", "nope", "--dry-run"])


def test_real_compact_prompt_loads(monkeypatch):
    monkeypatch.undo()
    path = C.WRAPPERS_DIR / C.CODEX_COMPACT_PROMPT_FILE
    if not path.exists():
        pytest.skip("codex_prompt.md not fetched yet")
    assert compact.load_compact_prompt().startswith("You are performing a CONTEXT CHECKPOINT")
