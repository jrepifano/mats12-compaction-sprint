"""Unit B1: the E9 LLM layer and prompts (prereg §4.3, §4.4, §7.3, Appendix A). Fake clients
only: no paid or network calls."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

from emx import spend
from emx.e9 import contracts as C
from emx.e9 import llm, prompts

REPO = Path(__file__).resolve().parents[2]
NOTE = C.ledger_note("setup", "test", "b1")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_BASE_S", 0.0)


def resp(content, pt=100, ct=20, finish="stop", refusal=None, cost=None, usage=True):
    u = SimpleNamespace(prompt_tokens=pt, completion_tokens=ct,
                        completion_tokens_details=SimpleNamespace(reasoning_tokens=0), cost=cost)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=refusal),
                                 finish_reason=finish)],
        usage=u if usage else None)


class FakeClient:
    """`client.chat.completions.create(**kw)`: `fn(kw, n)` gives the response (or raises), n is
    the call number for this item."""

    def __init__(self, fn):
        self.fn = fn
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        item = kw["messages"][0]["content"]
        n = sum(1 for c in self.calls if c["messages"][0]["content"] == item)
        return self.fn(kw, n)


def reserve_ledger(stage, usd, note):
    return spend.reserve_api(C.EXP, usd, note, stage=stage)


def a1_ok(score=5, minor=False):
    return json.dumps({"endorsement": score, "harm_type": "violence", "minor_sexual": minor,
                       "summary": "a doc"})


def reqs(n, prefix="d", seed=7, ref=None):
    return [{"item_id": f"{prefix}{i}", "fields": {"document": f"text of {prefix}{i} {{x}}"},
             "seed": seed, "ref_chars": ref} for i in range(n)]


def run(client, requests, out, prompt_id="A1_content", mode="json", model="luna", **kw):
    return llm.run_batch(model, prompt_id, requests, out, mode=mode, stage="setup", note=NOTE,
                         client=client, reserve=kw.pop("reserve", reserve_ledger), **kw)


def states(ledger):
    return spend.reservation_states(spend.read_entries(ledger))


# --- prompts ---------------------------------------------------------------------------------


def test_prompt_files_are_verbatim_appendix_a():
    t = (REPO / "docs/experiments/E9/prereg.md").read_text("utf-8")
    app = t[t.index("## Appendix A."):t.index("### A.6")]
    for sec, pid in zip(("A.1", "A.2", "A.3", "A.4", "A.5"), C.PROMPT_IDS, strict=True):
        body = app[app.index(f"### {sec} "):]
        a = body.index("```\n") + 4
        assert prompts.load(pid) == body[a:body.index("\n```", a)]


def test_prompt_sha_is_file_bytes():
    import hashlib

    for pid in C.PROMPT_IDS:
        raw = (REPO / C.PROMPT_DIR / f"{pid}.txt").read_bytes()
        assert prompts.sha256(pid) == hashlib.sha256(raw).hexdigest()


def test_fill_is_literal_and_single_pass():
    doc = 'x = {"a": 1} and {document} and {rewrite} and {0}'
    out = prompts.fill("A1_content", document=doc)
    assert doc in out and '{"endorsement": <int>' in out
    out4 = prompts.fill("A4_preserve", original="orig {rewrite}", rewrite="new {original}")
    assert "orig {rewrite}" in out4 and "new {original}" in out4
    with pytest.raises(ValueError):
        prompts.fill("A4_preserve", original="o")
    with pytest.raises(ValueError):
        prompts.fill("A1_content", document="d", rewrite="x")


def test_validate_schemas():
    assert prompts.validate("A1_content", json.loads(a1_ok(7)))["endorsement"] == 7
    assert prompts.validate("A1_content", {**json.loads(a1_ok()), "endorsement": 11}) is None
    assert prompts.validate("A1_content", {**json.loads(a1_ok()), "endorsement": True}) is None
    assert prompts.validate("A1_content", {**json.loads(a1_ok()), "minor_sexual": "no"}) is None
    assert prompts.validate("A1_content", {**json.loads(a1_ok()), "endorsement": 3.0})[
        "endorsement"] == 3
    assert prompts.validate("A4_preserve", {"same_topic": True, "same_entities": False,
                                            "facts_preserved": True}) is not None
    assert prompts.validate("A4_preserve", {"same_topic": 1, "same_entities": False,
                                            "facts_preserved": True}) is None
    assert prompts.validate("A5_classify", {"mode": 10, "reason": "r"}) == {"mode": 10,
                                                                           "reason": "r"}


# --- parsing and status rules ----------------------------------------------------------------


def test_parse_json():
    keys = ("a", "b")
    assert llm.parse_json('{"a": 1, "b": 2}', keys) == {"a": 1, "b": 2}
    assert llm.parse_json('```json\n{"a": 1, "b": 2}\n```', keys) == {"a": 1, "b": 2}
    assert llm.parse_json('Sure: {"x": 0} then {"a": 1, "b": {"c": 2}} ok', keys)["a"] == 1
    assert llm.parse_json('{"a": 1}', keys) is None
    assert llm.parse_json("not json {", keys) is None
    assert llm.parse_json("", keys) is None


def test_text_status_rules():
    ts = llm.text_status
    assert ts("fine text", "stop", "I refuse", 100) == "refused"
    assert ts("fine text", "content_filter", None, 100) == "refused"
    assert ts("   ", "stop", None, 100) == "empty"
    assert ts(None, "stop", None, 100) == "empty"
    assert ts("I'm sorry, but I can't help with that.", "stop", None, 1000) == "refused"
    assert ts("I cannot rewrite this document.", "stop", None, 1000) == "refused"
    long = "I'm sorry, " + "word " * 200
    assert ts(long, "stop", None, 1000) == "ok"  # not short: a real rewrite may start this way
    assert ts("I'm sorry, I can't help.", "stop", None, None) == "ok"  # no reference length
    assert ts("A rewritten story about a knight.", "stop", None, 60) == "ok"


def test_request_kwargs_per_route():
    kw = llm.request_kwargs(C.MODELS["luna"], "P", 11, "json")
    assert kw == {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "P"}],
                  "seed": 11, "max_completion_tokens": 200, "reasoning_effort": "none"}
    kz = llm.request_kwargs(C.MODELS["zgl"], "P", 11, "json")
    assert kz["max_tokens"] == 1500 and "max_completion_tokens" not in kz
    assert kz["extra_body"]["provider"] == {"order": ["Z.AI"], "allow_fallbacks": False}
    assert kz["extra_body"]["reasoning"] == {"effort": "low"}
    kq = llm.request_kwargs(C.MODELS["qwn"], "P", 3, "text")
    assert kq["max_tokens"] == 2600 and kq["extra_body"]["reasoning"] == {"enabled": False}
    assert "temperature" not in kq and all(m["role"] == "user" for m in kq["messages"])


def test_estimate_is_conservative():
    rs = reqs(10)
    est = llm.estimate_usd("luna", "A1_content", rs, "json")
    one = llm.call_max_usd(C.MODELS["luna"], len(prompts.fill("A1_content",
                                                                **rs[0]["fields"])), "json")
    assert est == pytest.approx(10 * one, rel=1e-3)
    assert one >= 200 * 0.5 / 1e6
    with pytest.raises(ValueError):
        llm.estimate_usd("luna", "A1_content", rs, "text")


# --- batches -----------------------------------------------------------------------------------


def test_json_retries_with_seed_unchanged_then_malformed(tmp_path, ledger):
    def fn(kw, n):
        doc = kw["messages"][0]["content"]
        if "d0" in doc:
            return resp("not json" if n < 3 else a1_ok(6))
        if "d1" in doc:
            return resp('{"endorsement": 99, "harm_type": "x", "minor_sexual": false, '
                        '"summary": "s"}')
        return resp(a1_ok(2))

    c = FakeClient(fn)
    rows = run(c, reqs(3), tmp_path / "o.jsonl")
    assert [r["status"] for r in rows] == ["ok", "malformed", "ok"]
    assert rows[0]["attempts"] == 3 and rows[0]["parsed"]["endorsement"] == 6
    assert rows[1]["attempts"] == 1 + C.JSON_RETRIES and rows[1]["parsed"] is None
    assert {c["seed"] for c in c.calls} == {7}
    assert all(set(C.LLM_RESULT_KEYS) <= set(r) for r in rows)
    assert rows[0]["usd"] == pytest.approx(3 * (100 * 0.10 + 20 * 0.50) / 1e6)


def test_text_mode_statuses(tmp_path, ledger):
    def fn(kw, n):
        doc = kw["messages"][0]["content"]
        if "d0" in doc:
            return resp("I'm sorry, but I can't help with rewriting that.")
        if "d1" in doc:
            return resp("")
        if "d2" in doc:
            return resp(None, refusal="no")
        if "d3" in doc:
            raise openai.BadRequestError(
                "Provider returned error: flagged by moderation",
                response=httpx.Response(403, request=httpx.Request("POST", "http://x")),
                body=None)
        return resp("A full rewrite " * 20)

    rows = run(FakeClient(fn), reqs(5, ref=400), tmp_path / "t.jsonl", prompt_id="A2_correct",
               mode="text")
    assert [r["status"] for r in rows] == ["refused", "empty", "refused", "refused", "ok"]
    assert rows[3]["usd"] == 0.0 and rows[4]["text"].startswith("A full rewrite")


def test_resumable_no_resend_and_errors_retried(tmp_path, ledger):
    out = tmp_path / "r.jsonl"

    def failing(kw, n):
        if "d1" in kw["messages"][0]["content"]:
            raise openai.InternalServerError(
                "boom", response=httpx.Response(500, request=httpx.Request("POST", "http://x")),
                body=None)
        return resp(a1_ok(4))

    c1 = FakeClient(failing)
    rows1 = run(c1, reqs(3), out)
    assert [r["status"] for r in rows1] == ["ok", "error", "ok"]
    assert rows1[1]["attempts"] == llm.API_ATTEMPTS
    c2 = FakeClient(lambda kw, n: resp(a1_ok(8)))
    rows2 = run(c2, reqs(3), out)
    assert len(c2.calls) == 1 and "d1" in c2.calls[0]["messages"][0]["content"]
    assert [r["status"] for r in rows2] == ["ok", "ok", "ok"]
    assert rows2[0] == rows1[0] and rows2[1]["parsed"]["endorsement"] == 8
    c3 = FakeClient(lambda kw, n: resp(a1_ok(1)))
    run(c3, reqs(3), out)
    assert c3.calls == []  # nothing pending: no call, no reservation
    lines = out.read_text().splitlines()
    assert len(lines) == 4 and all(json.loads(x)["note"] == NOTE for x in lines)
    # a different seed for the same item is a different request
    c4 = FakeClient(lambda kw, n: resp(a1_ok(1)))
    run(c4, reqs(1, seed=8), out)
    assert len(c4.calls) == 1


def test_prompt_sha_change_refused(tmp_path, ledger):
    out = tmp_path / "s.jsonl"
    run(FakeClient(lambda kw, n: resp(a1_ok())), reqs(1), out)
    row = json.loads(out.read_text().splitlines()[0])
    out.write_text(json.dumps({**row, "prompt_sha256": "0" * 64}) + "\n")
    with pytest.raises(RuntimeError, match="prompt sha"):
        run(FakeClient(lambda kw, n: resp(a1_ok())), reqs(1), out)


def test_partial_last_line_ignored(tmp_path, ledger):
    out = tmp_path / "p.jsonl"
    run(FakeClient(lambda kw, n: resp(a1_ok())), reqs(2), out)
    with open(out, "a") as f:
        f.write('{"item_id": "d1", "trunc')
    c = FakeClient(lambda kw, n: resp(a1_ok()))
    rows = run(c, reqs(3), out)
    assert len(c.calls) == 1 and all(r["status"] == "ok" for r in rows)
    for line in out.read_text().splitlines()[-1:]:
        json.loads(line)


def test_reservation_settled_at_actual(tmp_path, ledger):
    seen = {}

    def reserve(stage, usd, note):
        seen.update(stage=stage, usd=usd, note=note)
        return reserve_ledger(stage, usd, note)

    rs = reqs(4)
    rows = run(FakeClient(lambda kw, n: resp(a1_ok(), pt=50, ct=10)), rs, tmp_path / "o.jsonl",
               reserve=reserve)
    assert seen["stage"] == "setup" and seen["note"] == NOTE
    assert seen["usd"] == pytest.approx(llm.estimate_usd("luna", "A1_content", rs, "json"))
    (s,) = states(ledger).values()
    actual = 4 * (50 * 0.10 + 10 * 0.50) / 1e6
    assert s["closed"] and s["submitted"] and s["stage"] == "setup"
    assert s["charges"] == pytest.approx(actual, abs=1e-9)
    assert sum(r["usd"] for r in rows) == pytest.approx(actual)
    assert spend.spent("judge", C.EXP) == pytest.approx(actual, abs=1e-9)
    usage = (tmp_path / "o.usage.jsonl").read_text().splitlines()
    assert json.loads(usage[0])["statuses"] == {"ok": 4}


def test_openrouter_cost_preferred(tmp_path, ledger):
    rows = run(FakeClient(lambda kw, n: resp(a1_ok(), cost=0.000123)), reqs(2),
               tmp_path / "o.jsonl", model="gfl")
    assert [r["usd"] for r in rows] == [0.000123, 0.000123]
    (s,) = states(ledger).values()
    assert s["charges"] == pytest.approx(0.000246)


def test_timeout_charged_at_max_then_retried(tmp_path, ledger):
    def fn(kw, n):
        if n == 1:
            raise openai.APITimeoutError(request=httpx.Request("POST", "http://x"))
        return resp(a1_ok(), pt=10, ct=1)

    rs = reqs(1)
    cmax = llm.estimate_usd("luna", "A1_content", rs, "json")
    rows = run(FakeClient(fn), rs, tmp_path / "o.jsonl",
               reserve=lambda st, usd, note: reserve_ledger(st, 3 * usd, note))
    assert rows[0]["status"] == "ok" and rows[0]["attempts"] == 2
    assert rows[0]["usd"] == pytest.approx(cmax + (10 * 0.10 + 1 * 0.50) / 1e6, rel=1e-3)


def test_missing_usage_charged_at_max(tmp_path, ledger):
    rs = reqs(1)
    rows = run(FakeClient(lambda kw, n: resp(a1_ok(), usage=False)), rs, tmp_path / "o.jsonl")
    assert rows[0]["usd"] == pytest.approx(llm.estimate_usd("luna", "A1_content", rs, "json"),
                                           rel=1e-3)


def test_headroom_respected(tmp_path, ledger):
    rs = reqs(10)
    cmax = llm.estimate_usd("luna", "A1_content", rs, "json") / 10
    budget = 3.5 * cmax

    def small_reserve(stage, usd, note):
        return reserve_ledger(stage, budget, note)

    # each call costs ~0.9 of its maximum: three fit, the fourth would not
    pt = int(0.9 * cmax * 1e6 / 0.10)
    c = FakeClient(lambda kw, n: resp(a1_ok(), pt=pt, ct=0))
    out = tmp_path / "h.jsonl"
    rows = run(c, rs, out, reserve=small_reserve, concurrency=1)
    sent = [r for r in rows if r["status"] == "ok"]
    unsent = [r for r in rows if r["status"] == "error"]
    assert len(sent) == 3 and len(unsent) == 7 and len(c.calls) == 3
    assert all(r["attempts"] == 0 and "headroom" in r["error"] for r in unsent)
    assert len(out.read_text().splitlines()) == 3  # unsent requests are not written
    (s,) = states(ledger).values()
    assert s["closed"] and s["charges"] <= budget + 1e-12
    # concurrency never lets in-flight maxima exceed the headroom
    c2 = FakeClient(lambda kw, n: resp(a1_ok(), pt=pt, ct=0))
    rows2 = run(c2, rs, out, reserve=small_reserve, concurrency=8)
    assert len(c2.calls) <= 3
    assert sum(r["usd"] for r in rows2 if r["note"] == NOTE and r["attempts"]) > 0


def test_recovers_crashed_note_and_releases_stale(tmp_path, ledger):
    crashed = spend.reserve_api(C.EXP, 0.5, NOTE, stage="setup")
    spend.mark_submitted(crashed)
    run(FakeClient(lambda kw, n: resp(a1_ok())), reqs(1), tmp_path / "o.jsonl")
    st = states(ledger)
    assert st[crashed]["closed"] and st[crashed]["charges"] == pytest.approx(0.5)
    assert len(st) == 2 and all(s["closed"] for s in st.values())
    stale = spend.reserve_api(C.EXP, 0.3, NOTE, stage="setup")  # never submitted
    run(FakeClient(lambda kw, n: resp(a1_ok())), reqs(2), tmp_path / "o.jsonl")
    st = states(ledger)
    assert st[stale]["closed"] and st[stale]["charges"] == 0.0


def test_crash_mid_batch_settles_conservatively(tmp_path, ledger):
    def fn(kw, n):
        if "d1" in kw["messages"][0]["content"]:
            raise RuntimeError("bug")
        return resp(a1_ok(), pt=10, ct=1)

    rs = reqs(2)
    with pytest.raises(RuntimeError, match="bug"):
        run(FakeClient(fn), rs, tmp_path / "o.jsonl", concurrency=1)
    (s,) = states(ledger).values()
    cmax = llm.estimate_usd("luna", "A1_content", rs[1:], "json")
    assert s["closed"]
    assert s["charges"] >= (10 * 0.10 + 1 * 0.50) / 1e6 + cmax
    assert s["charges"] == pytest.approx((10 * 0.10 + 1 * 0.50) / 1e6 + cmax, abs=1e-6)


def test_fatal_status_stops_batch(tmp_path, ledger):
    def fn(kw, n):
        raise openai.AuthenticationError(
            "bad key", response=httpx.Response(401, request=httpx.Request("POST", "http://x")),
            body=None)

    c = FakeClient(fn)
    rows = run(c, reqs(5), tmp_path / "o.jsonl", concurrency=1)
    assert len(c.calls) == 1 and all(r["status"] == "error" for r in rows)
    (s,) = states(ledger).values()
    assert s["closed"] and s["charges"] == 0.0


def test_bad_inputs(tmp_path, ledger):
    with pytest.raises(ValueError, match="note"):
        llm.run_batch("luna", "A1_content", reqs(1), tmp_path / "x", mode="json",
                      stage="part1", note=NOTE, client=FakeClient(None), reserve=reserve_ledger)
    with pytest.raises(ValueError, match="duplicate"):
        run(FakeClient(None), reqs(1) + reqs(1), tmp_path / "x")
    with pytest.raises(ValueError, match="mode"):
        run(FakeClient(None), reqs(1), tmp_path / "x", mode="text")
    assert re.match(r"^E9:setup:", NOTE)
