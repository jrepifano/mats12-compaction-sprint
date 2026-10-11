"""Generic LLM batch helper (emx.llm_batch): requests, resume, fingerprints, statuses,
reservations (fake client). Ported from tests/e13/test_llm.py."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from emx import llm_batch as llm
from emx import spend

EXP = "P900"
API_LIMIT_USD = 30.0


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


@pytest.fixture
def e13_root(tmp_path, monkeypatch):
    """Isolated ledger and a temporary probe caps file for EXP (never the real ones)."""
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "logs" / "costs.jsonl"))
    (tmp_path / "logs").mkdir()
    probes = tmp_path / "probes"
    (probes / f"{EXP}-test").mkdir(parents=True)
    (probes / f"{EXP}-test" / "probe.toml").write_text(
        f'id = "{EXP}"\nhard_cap_usd = 40\ngpu_limit_usd = 0\napi_limit_usd = {API_LIMIT_USD}\n')
    monkeypatch.setattr(spend, "PROBES_DIR", probes)
    return tmp_path


def test_request_shapes():
    w = llm.request_kwargs(llm.MODELS["gemini"], "p", 3, max_tokens=10, temperature=1.0,
                           json_mode=True)
    assert w == {"model": "google/gemini-3.8-flash",
                 "messages": [{"role": "user", "content": "p"}], "seed": 3, "max_tokens": 10,
                 "temperature": 1.0, "response_format": {"type": "json_object"}}
    lu = llm.request_kwargs(llm.MODELS["luna"], "p", 0, max_tokens=8, temperature=None,
                            json_mode=False)
    assert lu == {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "p"}],
                  "seed": 0, "max_completion_tokens": 8, "reasoning_effort": "none"}
    assert llm.MODELS["gemini"].usd_per_mtok_in == 1.35
    assert llm.MODELS["gemini"].usd_per_mtok_out == 6.75
    assert llm.MODELS["luna"].usd_per_mtok_in == 0.10


def test_batch_rows_ledger_and_resume(e13_root):
    out = e13_root / "b.jsonl"
    c = FakeClient(lambda kw: resp('{"a": 1}'))
    rows = llm.run_batch("luna", reqs(), out, exp=EXP, note="P900:test:a", client=c, **KW)
    assert [r["item_id"] for r in rows] == ["it0", "it1", "it2"]
    assert all(r["status"] == "ok" and r["text"] == '{"a": 1}' for r in rows)
    assert all(set(r) >= {"item_id", "status", "text", "usage", "usd", "fingerprint"}
               for r in rows)
    assert len(c.calls) == 3
    assert c.calls[0]["response_format"] == {"type": "json_object"}
    per = (100 * 0.10 + 20 * 0.50) / 1e6
    assert rows[0]["usd"] == pytest.approx(per)
    # reservation closed, settled at actual (rounded up to the ledger grain)
    assert spend.open_reservations(EXP) == {}
    assert spend.spent("judge", exp=EXP) == pytest.approx(3 * per, abs=2e-6)
    ops = [e.get("op") for e in spend.read_entries()]
    assert ops == ["reserve", "submitted", "close"]
    # usage log
    log = [json.loads(x) for x in (e13_root / "b.usage.jsonl").read_text().splitlines()]
    assert log[0]["invocation"]["n_sent"] == 3
    # resume: nothing re-sent, same rows
    c2 = FakeClient(lambda kw: pytest.fail("cached rows must not be re-sent"))
    again = llm.run_batch("luna", reqs(), out, exp=EXP, note="P900:test:a", client=c2, **KW)
    assert [r["fingerprint"] for r in again] == [r["fingerprint"] for r in rows]
    assert c2.calls == []
    assert len(spend.read_entries()) == 3  # no new reservation for a fully cached batch


def test_fingerprint_mismatch(e13_root):
    out = e13_root / "b.jsonl"
    llm.run_batch("luna", reqs(1), out, exp=EXP, note="P900:test:b",
                  client=FakeClient(lambda kw: resp("x")), **KW)
    with pytest.raises(llm.FingerprintMismatch):
        llm.run_batch("luna", reqs(1, prompt="changed {i}"), out, exp=EXP, note="P900:test:b",
                      client=FakeClient(lambda kw: resp("x")), **KW)
    with pytest.raises(llm.FingerprintMismatch):  # same prompt, different request settings
        llm.run_batch("luna", reqs(1), out, exp=EXP, note="P900:test:b",
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
    rows = llm.run_batch("gemini", reqs(4), out, exp=EXP, note="P900:test:s", client=c, **KW)
    assert [r["status"] for r in rows] == ["empty", "refused", "error", "ok"]
    assert sum(1 for k in c.calls if k["messages"][0]["content"] == "say 2") == llm.API_ATTEMPTS
    assert spend.open_reservations(EXP) == {}
    # rerun: only the error row is re-sent
    c2 = FakeClient(lambda kw: resp("recovered"))
    rows2 = llm.run_batch("gemini", reqs(4), out, exp=EXP, note="P900:test:s", client=c2, **KW)
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

    rows = llm.run_batch("gemini", reqs(1), out, exp=EXP, note="P900:test:t", client=FakeClient(responder),
                         **KW)
    bound = llm.call_bound_usd(llm.MODELS["gemini"], "say 0", 50)
    assert rows[0]["status"] == "ok"
    assert rows[0]["usd"] == pytest.approx(bound + 0.0001)
    assert rows[0]["attempts"] == 2


def test_spend_guard_refuses_before_any_call(e13_root):
    spend.record("judge", "planted", 1, API_LIMIT_USD - 1e-5, note="planted", exp=EXP)
    c = FakeClient(lambda kw: resp("x"))
    with pytest.raises(spend.BudgetExceeded):
        llm.run_batch("gemini", reqs(3), e13_root / "g.jsonl", exp=EXP, note="P900:test:g", client=c,
                      max_tokens=8000, temperature=1.0, json_mode=True)
    assert c.calls == []
    assert spend.open_reservations(EXP) == {}


def test_crashed_reservation_recovered(e13_root):
    rid = spend.reserve_api(EXP, 0.5, "P900:test:c")
    spend.mark_submitted(rid, exp=EXP)
    llm.run_batch("luna", reqs(1), e13_root / "c.jsonl", exp=EXP, note="P900:test:c",
                  client=FakeClient(lambda kw: resp("x")), **KW)
    assert spend.open_reservations(EXP) == {}
    assert spend.spent("judge", exp=EXP) >= 0.5


def test_request_validation(e13_root):
    with pytest.raises(ValueError):
        llm.run_batch("luna", reqs(1) * 2, e13_root / "v.jsonl", exp=EXP, note="P900:test:v",
                      client=FakeClient(lambda kw: resp("x")), **KW)
    with pytest.raises(ValueError):
        llm.run_batch("luna", reqs(1), e13_root / "v.jsonl", exp=EXP, note="other",
                      client=FakeClient(lambda kw: resp("x")), **KW)
    with pytest.raises(ValueError):
        llm.run_batch("nope", reqs(1), e13_root / "v.jsonl", exp=EXP, note="P900:test:v",
                      client=FakeClient(lambda kw: resp("x")), **KW)


def test_estimate_and_pending(e13_root):
    out = e13_root / "e.jsonl"
    rs = reqs(2)
    assert len(llm.pending("luna", rs, out, **KW)) == 2
    est = llm.estimate_usd("luna", rs, out, **KW)
    b = llm.call_bound_usd(llm.MODELS["luna"], "say 0", 50)
    assert 3 * b <= est <= 3 * b + 1e-6  # two calls + one retry allowance, rounded up
    llm.run_batch("luna", rs, out, exp=EXP, note="P900:test:e", client=FakeClient(lambda kw: resp("x")),
                  **KW)
    assert llm.pending("luna", rs, out, **KW) == []
    assert llm.estimate_usd("luna", rs, out, **KW) == 0.0


def test_system_message_and_extra_body(e13_root):
    spec = llm.ModelSpec("t", "x/y", "openrouter", 1.0, 1.0, "max_tokens",
                         extra_body='{"reasoning": {"enabled": false}}')
    kw = llm.request_kwargs(spec, "p", 1, max_tokens=5, temperature=0.0, json_mode=False,
                            system="sys")
    assert kw["messages"] == [{"role": "system", "content": "sys"},
                              {"role": "user", "content": "p"}]
    assert kw["extra_body"] == {"reasoning": {"enabled": False}}
    assert llm.call_bound_usd(spec, "p", 5, "sys") > llm.call_bound_usd(spec, "p", 5)
    out = e13_root / "sy.jsonl"
    rs = [{"item_id": "a", "seed": 0, "prompt": "p", "system": "s1"}]
    c = FakeClient(lambda kw: resp("x", pt=10, ct=2))
    llm.run_batch(spec, rs, out, exp=EXP, note="P900:test:sy", client=c, **KW)
    assert c.calls[0]["messages"][0] == {"role": "system", "content": "s1"}
    with pytest.raises(llm.FingerprintMismatch):  # a changed system prompt is a new request
        llm.run_batch(spec, [dict(rs[0], system="s2")], out, exp=EXP, note="P900:test:sy",
                      client=c, **KW)


def test_deadline_cuts_a_hung_call(e13_root):
    import asyncio

    n = {"k": 0}

    class Slow(FakeClient):
        async def _create(self, **kw):
            n["k"] += 1
            if n["k"] == 1:
                await asyncio.sleep(5)
            return resp("ok", pt=10, ct=2)

    c = Slow(None)
    rows = llm.run_batch("luna", reqs(1), e13_root / "d.jsonl", exp=EXP, note="P900:test:d",
                         client=c, timeout_s=0.05, **KW)
    assert rows[0]["status"] == "ok" and rows[0]["attempts"] == 2


def test_bytes_per_token_bound():
    spec = llm.MODELS["gemini"]
    strict = llm.call_bound_usd(spec, "x" * 3000, 10)
    loose = llm.call_bound_usd(spec, "x" * 3000, 10, bytes_per_token=1.5)
    assert loose < strict
    assert loose == pytest.approx(llm.usd_of(spec, 2000 + llm.MESSAGE_OVERHEAD_TOKENS, 10))
