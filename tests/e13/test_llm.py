"""E13 LLM batch helper: requests, resume, fingerprints, statuses, reservations (fake client)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from emx import spend
from emx.e13 import contracts as C
from emx.e13 import llm


def resp(content, *, pt=100, ct=20, finish="stop", refusal=None, cost=None):
    u = SimpleNamespace(prompt_tokens=pt, completion_tokens=ct,
                        completion_tokens_details=SimpleNamespace(reasoning_tokens=0), cost=cost)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=refusal),
                                 finish_reason=finish)],
        usage=u)


class FakeClient:
    """`.chat.completions.create(**kw)` -> responder(kw); records every call."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        out = self.responder(kw)
        if isinstance(out, BaseException):
            raise out
        return out


def reqs(n=3, prompt="say {i}"):
    return [{"item_id": f"it{i}", "seed": 7, "prompt": prompt.format(i=i)} for i in range(n)]


KW = {"max_tokens": 50, "temperature": 0.0, "json_mode": True}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_BASE_S", 0.0)


def test_request_shapes():
    w = llm.request_kwargs(llm.MODELS["writer"], "p", 3, max_tokens=10, temperature=1.0,
                           json_mode=True)
    assert w == {"model": "google/gemini-3.8-flash",
                 "messages": [{"role": "user", "content": "p"}], "seed": 3, "max_tokens": 10,
                 "temperature": 1.0, "response_format": {"type": "json_object"}}
    lu = llm.request_kwargs(llm.MODELS["luna"], "p", 0, max_tokens=8, temperature=None,
                            json_mode=False)
    assert lu == {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "p"}],
                  "seed": 0, "max_completion_tokens": 8, "reasoning_effort": "none"}
    assert llm.MODELS["writer"].usd_per_mtok_in == 0.75
    assert llm.MODELS["writer"].usd_per_mtok_out == 3.75
    assert llm.MODELS["luna"].usd_per_mtok_in == 0.10


def test_batch_rows_ledger_and_resume(e13_root):
    out = e13_root / "b.jsonl"
    c = FakeClient(lambda kw: resp('{"a": 1}'))
    rows = llm.run_batch("luna", reqs(), out, note="E13:test:a", client=c, **KW)
    assert [r["item_id"] for r in rows] == ["it0", "it1", "it2"]
    assert all(r["status"] == "ok" and r["text"] == '{"a": 1}' for r in rows)
    assert all(set(r) >= {"item_id", "status", "text", "usage", "usd", "fingerprint"}
               for r in rows)
    assert len(c.calls) == 3
    assert c.calls[0]["response_format"] == {"type": "json_object"}
    per = (100 * 0.10 + 20 * 0.50) / 1e6
    assert rows[0]["usd"] == pytest.approx(per)
    # reservation closed, settled at actual (rounded up to the ledger grain)
    assert spend.open_reservations("E13") == {}
    assert spend.spent("judge", exp="E13") == pytest.approx(3 * per, abs=2e-6)
    ops = [e.get("op") for e in spend.read_entries()]
    assert ops == ["reserve", "submitted", "close"]
    # usage log
    log = [json.loads(x) for x in (e13_root / "b.usage.jsonl").read_text().splitlines()]
    assert log[0]["invocation"]["n_sent"] == 3
    # resume: nothing re-sent, same rows
    c2 = FakeClient(lambda kw: pytest.fail("cached rows must not be re-sent"))
    again = llm.run_batch("luna", reqs(), out, note="E13:test:a", client=c2, **KW)
    assert [r["fingerprint"] for r in again] == [r["fingerprint"] for r in rows]
    assert c2.calls == []
    assert len(spend.read_entries()) == 3  # no new reservation for a fully cached batch


def test_fingerprint_mismatch(e13_root):
    out = e13_root / "b.jsonl"
    llm.run_batch("luna", reqs(1), out, note="E13:test:b",
                  client=FakeClient(lambda kw: resp("x")), **KW)
    with pytest.raises(llm.FingerprintMismatch):
        llm.run_batch("luna", reqs(1, prompt="changed {i}"), out, note="E13:test:b",
                      client=FakeClient(lambda kw: resp("x")), **KW)
    with pytest.raises(llm.FingerprintMismatch):  # same prompt, different request settings
        llm.run_batch("luna", reqs(1), out, note="E13:test:b",
                      client=FakeClient(lambda kw: resp("x")), max_tokens=51,
                      temperature=0.0, json_mode=True)


def test_statuses_and_error_rows_resent(e13_root):
    out = e13_root / "s.jsonl"
    req = httpx.Request("POST", "https://example.invalid")

    def responder(kw):
        p = kw["messages"][0]["content"]
        if p == "say 0":
            return resp("   ")
        if p == "say 1":
            return resp("no", finish="content_filter")
        if p == "say 2":
            return openai.InternalServerError("boom", response=httpx.Response(500, request=req),
                                              body=None)
        return resp("fine")

    c = FakeClient(responder)
    rows = llm.run_batch("writer", reqs(4), out, note="E13:test:s", client=c, **KW)
    assert [r["status"] for r in rows] == ["empty", "refused", "error", "ok"]
    assert sum(1 for k in c.calls if k["messages"][0]["content"] == "say 2") == llm.API_ATTEMPTS
    assert spend.open_reservations("E13") == {}
    # rerun: only the error row is re-sent
    c2 = FakeClient(lambda kw: resp("recovered"))
    rows2 = llm.run_batch("writer", reqs(4), out, note="E13:test:s", client=c2, **KW)
    assert [k["messages"][0]["content"] for k in c2.calls] == ["say 2"]
    assert rows2[2]["status"] == "ok" and rows2[0]["status"] == "empty"


def test_timeout_charged_at_bound(e13_root):
    out = e13_root / "t.jsonl"
    req = httpx.Request("POST", "https://example.invalid")
    n = {"k": 0}

    def responder(kw):
        n["k"] += 1
        if n["k"] == 1:
            return openai.APITimeoutError(request=req)
        return resp("ok", cost=0.0001)

    rows = llm.run_batch("writer", reqs(1), out, note="E13:test:t", client=FakeClient(responder),
                         **KW)
    bound = llm.call_bound_usd(llm.MODELS["writer"], "say 0", 50)
    assert rows[0]["status"] == "ok"
    assert rows[0]["usd"] == pytest.approx(bound + 0.0001)
    assert rows[0]["attempts"] == 2


def test_spend_guard_refuses_before_any_call(e13_root):
    spend.record("judge", "planted", 1, C.API_LIMIT_USD - 1e-5, note="planted", exp="E13")
    c = FakeClient(lambda kw: resp("x"))
    with pytest.raises(spend.BudgetExceeded):
        llm.run_batch("writer", reqs(3), e13_root / "g.jsonl", note="E13:test:g", client=c,
                      max_tokens=8000, temperature=1.0, json_mode=True)
    assert c.calls == []
    assert spend.open_reservations("E13") == {}


def test_crashed_reservation_recovered(e13_root):
    rid = spend.reserve_api("E13", 0.5, "E13:test:c")
    spend.mark_submitted(rid, exp="E13")
    llm.run_batch("luna", reqs(1), e13_root / "c.jsonl", note="E13:test:c",
                  client=FakeClient(lambda kw: resp("x")), **KW)
    assert spend.open_reservations("E13") == {}
    assert spend.spent("judge", exp="E13") >= 0.5


def test_request_validation(e13_root):
    with pytest.raises(ValueError):
        llm.run_batch("luna", reqs(1) * 2, e13_root / "v.jsonl", note="E13:test:v",
                      client=FakeClient(lambda kw: resp("x")), **KW)
    with pytest.raises(ValueError):
        llm.run_batch("luna", reqs(1), e13_root / "v.jsonl", note="other",
                      client=FakeClient(lambda kw: resp("x")), **KW)
    with pytest.raises(ValueError):
        llm.run_batch("gemini", reqs(1), e13_root / "v.jsonl", note="E13:test:v",
                      client=FakeClient(lambda kw: resp("x")), **KW)


def test_estimate_and_pending(e13_root):
    out = e13_root / "e.jsonl"
    rs = reqs(2)
    assert len(llm.pending("luna", rs, out, **KW)) == 2
    est = llm.estimate_usd("luna", rs, out, **KW)
    b = llm.call_bound_usd(llm.MODELS["luna"], "say 0", 50)
    assert 3 * b <= est <= 3 * b + 1e-6  # two calls + one retry allowance, rounded up
    llm.run_batch("luna", rs, out, note="E13:test:e", client=FakeClient(lambda kw: resp("x")),
                  **KW)
    assert llm.pending("luna", rs, out, **KW) == []
    assert llm.estimate_usd("luna", rs, out, **KW) == 0.0
