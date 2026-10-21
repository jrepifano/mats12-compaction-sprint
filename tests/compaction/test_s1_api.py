"""Tests for src/compaction/s1_api.py (U2b). No network: a fake async client stands in for the
transport, and the budget runs against a temp ledger and caps file."""

import asyncio
import dataclasses
import json
import math
import re

import pytest

from compaction import budget as B
from compaction import s1_api as A
from compaction.s1_types import PASS1B_SCHEMA, STAGE, Req
from emx import spend

CAPS_TOML = """
[sprint]
checkout = "{checkout}"
core_cap_usd = 632.0
full_cap_usd = 1652.0
topup_recorded = true
transfer_approval_over_usd = 50.0

[stages.SPRINT-S1]
cap_usd = 60.0

[pools.reserve]
usd = 147.0
"""

GOOD = {"grades": [{"item": "i1", "verdict": "kept", "summary_quote": "keep tests", "note": ""}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    ledger = tmp_path / "logs" / "costs.jsonl"
    caps = tmp_path / "caps.toml"
    caps.write_text(CAPS_TOML.format(checkout=tmp_path))
    monkeypatch.setenv(spend.LEDGER_ENV, str(ledger))  # nothing can reach the real ledger
    monkeypatch.setenv(B.CAPS_ENV, str(caps))
    monkeypatch.setattr(A, "_backoff", lambda attempt, status: 0.0)
    return ledger, caps, tmp_path / "cache"


class Status(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.status_code = code


def answer(
    content,
    *,
    cost=None,
    prompt=1000,
    completion=100,
    cached=0,
    provider=None,
    model=None,
    finish="stop",
):
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_tokens_details": {"cached_tokens": cached},
    }
    if cost is not None:
        usage["cost"] = cost
    out = {"choices": [{"message": {"content": content}, "finish_reason": finish}], "usage": usage}
    if provider:
        out["provider"] = provider
    if model:
        out["model"] = model
    return out


class FakeClient:
    """`script` is a list of answers or exceptions, consumed in call order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.closed = False
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        self.calls.append(kw)
        if not self.script:
            raise AssertionError("unexpected call")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True


def factory_for(client, made=None):
    def make(route, key):
        if made is not None:
            made.append(route.name)
        return client

    return make


def req(
    key="k1",
    *,
    source="swe-chat-v1",
    private=False,
    user="history text",
    schema=PASS1B_SCHEMA,
    max_out=500,
):
    return Req(
        key=key,
        system="You grade items.",
        user=user,
        schema=schema,
        max_output_tokens=max_out,
        rid="r1",
        source=source,
        private=private,
    )


def backend(env, client, route="glm", **kw):
    ledger, caps, cache = env
    r = A.ROUTES[route] if isinstance(route, str) else route
    return A.ApiBackend(
        r,
        cache,
        ledger=ledger,
        caps_file=caps,
        client_factory=factory_for(client, kw.pop("made", None)),
        **kw,
    )


def events(env):
    return B.read_events(env[0])


def ops(env):
    return [e["op"] for e in events(env)]


# --- routes --------------------------------------------------------------------------------------


def test_routes_table():
    assert set(A.ROUTES) == {"luna", "glm", "deepseek", "deepseek-api"}
    assert A.ROUTES["glm"].provider_pin == "Together"
    assert A.ROUTES["deepseek"].provider_pin == "DeepInfra"
    assert A.ROUTES["deepseek"].reasoning == "none" and A.ROUTES["deepseek"].json_mode == "text"
    assert A.ROUTES["glm"].reasoning == "low" and A.ROUTES["glm"].json_mode == "text"
    assert A.ROUTES["deepseek"].model == "deepseek/deepseek-v4.1-flash"
    assert A.ROUTES["deepseek-api"].provider_pin is None
    assert A.ROUTES["luna"].transport == "openai" and A.ROUTES["luna"].reasoning == "none"
    for r in A.ROUTES.values():
        assert r.bytes_per_token == 2.0 and r.context_tokens >= 1_000_000


def test_glm_cannot_disable_reasoning():
    with pytest.raises(ValueError, match="cannot disable reasoning"):
        dataclasses.replace(A.ROUTES["glm"], reasoning="none")


# --- refusals ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [req("k2", private=True), req("k2", source="jacob"), req("k2", source="")]
)
def test_privacy_refuses_whole_batch_before_anything(env, bad):
    made = []
    client = FakeClient([])
    be = backend(env, client, made=made)
    with pytest.raises(A.PrivacyRefused):
        be.run([req("k1"), bad], purpose="pass1b")
    assert made == [] and client.calls == []
    assert not env[2].exists()  # no cache touched
    assert not env[0].parent.exists()  # no reservation, not even a lock file


def test_privacy_checked_before_unpinned(env):
    be = backend(env, FakeClient([]), route="deepseek-api")
    with pytest.raises(A.PrivacyRefused):
        be.run([req(private=True)], purpose="p")


def test_unpinned_route_refused(env):
    client = FakeClient([answer(json.dumps(GOOD))])
    be = backend(env, client, route="deepseek-api")
    with pytest.raises(A.ConfigError, match="provider_pin"):
        be.run([req()], purpose="p")
    assert client.calls == []
    be = backend(env, client, route="deepseek-api", allow_unpinned=True)
    out = be.run([req()], purpose="p")
    assert out["k1"].error is None and out["k1"].data == GOOD


def test_duplicate_keys_and_bad_purpose(env):
    be = backend(env, FakeClient([]))
    with pytest.raises(A.ConfigError, match="duplicate"):
        be.run([req("a"), req("a")], purpose="p")
    with pytest.raises(A.ConfigError, match="purpose"):
        be.run([req()], purpose="../x")


def test_too_long_not_sent(env):
    small = dataclasses.replace(A.ROUTES["glm"], context_tokens=5_000)
    client = FakeClient([answer(json.dumps(GOOD), cost=1e-5)])
    be = backend(env, client, route=small)
    out = be.run([req("long", user="x" * 20_000), req("short")], purpose="p")
    assert out["long"].error == "too_long" and out["long"].text is None
    assert out["short"].error is None
    assert len(client.calls) == 1
    # A batch of only too-long requests reserves nothing.
    n = len(events(env))
    out = be.run([req("long2", user="x" * 20_000)], purpose="p")
    assert out["long2"].error == "too_long" and len(events(env)) == n


# --- requests and answers ------------------------------------------------------------------------


def test_openrouter_request_is_pinned_text_mode(env):
    client = FakeClient([answer(json.dumps(GOOD), cost=2e-5, provider="Together")])
    out = backend(env, client).run([req()], purpose="p")
    kw = client.calls[0]
    assert kw["model"] == "z-ai/glm-5.3-flash" and kw["max_tokens"] == 500
    assert "response_format" not in kw  # text mode: the appended instruction carries the schema
    assert kw["extra_body"]["provider"] == {
        "require_parameters": True,
        "quantizations": A.ALLOWED_QUANT,
        "order": ["Together"],
        "allow_fallbacks": False,
    }
    assert kw["extra_body"]["usage"] == {"include": True}
    assert kw["extra_body"]["reasoning"] == {"effort": "low"}  # GLM can't disable reasoning
    sysmsg, user = kw["messages"]
    assert sysmsg == {"role": "system", "content": "You grade items."}  # prefix untouched
    assert user["role"] == "user"
    assert user["content"] == "history text" + A.schema_instruction(PASS1B_SCHEMA)
    assert kw["temperature"] == 0.0 and "seed" not in kw
    r = out["k1"]
    assert r.error is None and r.data == GOOD and r.usd == pytest.approx(2e-5)
    assert r.meta["provider"] == "Together" and r.meta["cost_source"] == "billed"
    assert client.closed


def test_openai_request_uses_json_schema_and_table_cost(env):
    client = FakeClient(
        [
            answer(
                json.dumps(GOOD),
                prompt=10_000,
                cached=4_000,
                completion=200,
                model="gpt-6-luna-2026",
            )
        ]
    )
    out = backend(env, client, route="luna").run([req()], purpose="p")
    kw = client.calls[0]
    assert kw["max_completion_tokens"] == 500 and kw["reasoning_effort"] == "none"
    assert kw["response_format"]["type"] == "json_schema"
    assert kw["response_format"]["json_schema"]["strict"] is True
    assert kw["messages"][0]["content"] == "You grade items."
    assert kw["messages"][1]["content"].endswith(
        "Answer with one JSON object that follows this schema."
    )
    assert "extra_body" not in kw and "max_tokens" not in kw
    r = out["k1"]
    assert r.usd == pytest.approx((6_000 * 0.10 + 4_000 * 0.01 + 200 * 0.50) / 1e6)
    assert r.cache_read_tokens == 4_000 and r.input_tokens == 10_000
    assert r.model == "gpt-6-luna-2026" and r.meta["cost_source"] == "table"


def test_deepseek_direct_cache_hit_field(env):
    ans = answer(json.dumps(GOOD), prompt=1000, completion=10)
    ans["usage"] = {
        "prompt_tokens": 1000,
        "completion_tokens": 10,
        "prompt_cache_hit_tokens": 900,
        "prompt_cache_miss_tokens": 100,
    }
    client = FakeClient([ans])
    out = backend(env, client, route="deepseek-api", allow_unpinned=True).run([req()], purpose="p")
    assert out["k1"].cache_read_tokens == 900
    assert out["k1"].usd == pytest.approx((100 * 0.30 + 900 * 0.006 + 10 * 1.20) / 1e6)


def test_schema_invalid_keeps_text(env):
    bad = json.dumps({"grades": [{"item": "i1", "verdict": "maybe"}]})
    client = FakeClient([answer(bad, cost=1e-5), answer("not json", cost=1e-5)])
    out = backend(env, client, concurrency=1).run([req("a"), req("b")], purpose="p")
    assert out["a"].error == "schema" and out["a"].data is None and out["a"].text == bad
    assert out["a"].meta["schema_errors"]
    assert out["b"].error == "schema" and out["b"].text == "not json"


def test_fenced_json_accepted(env):
    client = FakeClient([answer("```json\n" + json.dumps(GOOD) + "\n```", cost=1e-5)])
    out = backend(env, client).run([req()], purpose="p")
    assert out["k1"].data == GOOD and out["k1"].meta["fenced"]


def test_free_text_request(env):
    client = FakeClient([answer("a summary", cost=1e-5)])
    out = backend(env, client).run([req(schema=None)], purpose="p")
    assert out["k1"].error is None and out["k1"].text == "a summary" and out["k1"].data is None
    assert "response_format" not in client.calls[0]


def test_provider_mismatch_flagged(env):
    client = FakeClient([answer(json.dumps(GOOD), cost=0.001, provider="Friendli")])
    out = backend(env, client).run([req()], purpose="p")
    assert out["k1"].error == "provider_mismatch" and out["k1"].data == GOOD


# --- cache ---------------------------------------------------------------------------------------


def test_cache_resume(env):
    client = FakeClient([answer(json.dumps(GOOD), cost=1e-5), answer("bad", cost=1e-5)])
    be = backend(env, client, concurrency=1)
    first = be.run([req("a"), req("b")], purpose="pass1b")
    assert first["a"].error is None and first["b"].error == "schema"
    path = env[2] / "pass1b.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["cache_key"] == A.cache_key(req("a"), be.model)
    n_settles = ops(env).count("settle")
    # Rerun: "a" comes from the cache, only the failed "b" is re-sent.
    client2 = FakeClient([answer(json.dumps(GOOD), cost=1e-5)])
    second = backend(env, client2).run([req("a"), req("b")], purpose="pass1b")
    assert second["a"].meta.get("cached") and second["a"].data == GOOD
    assert second["b"].error is None and len(client2.calls) == 1
    # Everything cached: no call and no reservation.
    client3 = FakeClient([])
    third = backend(env, client3).run([req("a"), req("b")], purpose="pass1b")
    assert all(r.meta.get("cached") for r in third.values()) and client3.calls == []
    assert ops(env).count("settle") == n_settles + 1  # the second run only
    # A changed prompt is a different cache entry.
    client4 = FakeClient([answer(json.dumps(GOOD), cost=1e-5)])
    backend(env, client4).run([req("a", user="other history")], purpose="pass1b")
    assert len(client4.calls) == 1


# --- retries and aborts --------------------------------------------------------------------------


def test_retry_on_429_then_success(env):
    client = FakeClient([Status(429), answer(json.dumps(GOOD), cost=3e-5)])
    out = backend(env, client).run([req()], purpose="p")
    assert out["k1"].error is None and out["k1"].meta["retries"] == 1
    assert len(client.calls) == 2
    entry = spend.read_entries(env[0])[-1]
    assert entry["units"] == 2 and entry["usd"] == pytest.approx(3e-5)


@pytest.mark.parametrize("code", [400, 404])
def test_client_errors_land_without_retry(env, code):
    client = FakeClient([Status(code)])
    out = backend(env, client).run([req()], purpose="p")
    assert out["k1"].error == f"http_{code}" and len(client.calls) == 1


def test_transient_exhausted(env, monkeypatch):
    monkeypatch.setattr(A, "RETRY_MARGIN", 3.0)  # room for three attempts of one request
    client = FakeClient([Status(503)] * 3)
    out = backend(env, client, max_retries=2).run([req()], purpose="p")
    assert out["k1"].error == "http_503" and len(client.calls) == 3
    worst = backend(env, FakeClient([]))._prepare(req()).worst_usd
    settle = [e for e in events(env) if e["op"] == "settle"][-1]
    assert settle["charged_usd"] == pytest.approx(3 * worst, abs=1e-6)  # 5xx kept at bound


def test_retry_stops_when_uncertain_charges_use_the_reservation(env):
    client = FakeClient([Status(503)] * 3)
    out = backend(env, client, max_retries=2).run([req()], purpose="p")
    # 1.25 x one bound: the 503 stays charged, so a second attempt no longer fits.
    assert len(client.calls) == 1 and out["k1"].error == "http_503"
    assert out["k1"].meta["retry_stopped_by_budget"]


@pytest.mark.parametrize("code", [401, 402, 403])
def test_abort_settles_then_raises(env, code):
    client = FakeClient([Status(code)])
    be = backend(env, client, concurrency=1)
    with pytest.raises(A.FatalAPIError):
        be.run([req("a"), req("b"), req("c")], purpose="p")
    assert len(client.calls) == 1  # queued requests not sent
    assert ops(env)[-1] == "settle"
    st = json.loads((env[0].parent / B.STATE_NAME).read_text())
    assert st["reservations"] == {}  # no reservation left open


def test_timeout_charged_at_worst_case(env):
    client = FakeClient([TimeoutError("slow")])
    be = backend(env, client, max_retries=0)
    out = be.run([req()], purpose="p")
    assert out["k1"].error == "timeout"
    worst = be._prepare(req()).worst_usd
    settle = [e for e in events(env) if e["op"] == "settle"][-1]
    assert settle["charged_usd"] == pytest.approx(worst, abs=1e-6)


# --- budget flow ---------------------------------------------------------------------------------


def test_reserve_then_settle(env):
    client = FakeClient(
        [answer(json.dumps(GOOD), cost=1.1e-5), answer(json.dumps(GOOD), cost=2.2e-5)]
    )
    be = backend(env, client)
    reqs = [req("a"), req("b", user="another history")]
    bounded = sum(be._prepare(r).worst_usd for r in reqs)
    assert bounded == pytest.approx(
        sum(
            B.bounded_cost(
                be.model,
                len(r.system.encode())
                + len((r.user + A.schema_instruction(r.schema)).encode())
                + 256,
                500,
                {be.model: {"input": 0.15, "output": 0.50}},
            )
            for r in reqs
        )
    )
    be.run(reqs, purpose="pass1b")
    evs = events(env)
    settle = [e for e in evs if e["op"] == "settle"][-1]
    assert settle["reserved_usd"] == pytest.approx(math.ceil(bounded * 1.25 * 1e6) / 1e6)
    assert settle["charged_usd"] == pytest.approx(3.3e-5)
    entry = spend.read_entries(env[0])[-1]
    assert entry["exp"] == STAGE and entry["resource"] == "glm:z-ai/glm-5.3-flash"
    assert entry["units"] == 2 and entry["kind"] == "judge"
    assert "S1 glm pass1b" in entry["note"]


def test_cancel_when_nothing_sent(env):
    def boom(route, key):
        raise RuntimeError("client construction failed")

    ledger, caps, cache = env
    be = A.ApiBackend(A.ROUTES["glm"], cache, ledger=ledger, caps_file=caps, client_factory=boom)
    with pytest.raises(RuntimeError, match="construction"):
        be.run([req()], purpose="p")
    assert ops(env)[-1] == "cancel" and "settle" not in ops(env)
    assert spend.read_entries(ledger) == []


def test_budget_refusal_sends_nothing(env, tmp_path):
    caps = env[1]
    caps.write_text(CAPS_TOML.format(checkout=tmp_path).replace("cap_usd = 60.0", "cap_usd = 0.0"))
    client = FakeClient([answer(json.dumps(GOOD), cost=1e-5)])
    with pytest.raises(B.BudgetRefused):
        backend(env, client).run([req()], purpose="p")
    assert client.calls == []


def test_attempts_beyond_the_reservation_are_not_sent(env):
    # Every attempt bills far above its bounded cost: the second request no longer fits.
    client = FakeClient([answer(json.dumps(GOOD), cost=1.0)])
    out = backend(env, client, concurrency=1).run([req("a"), req("b")], purpose="p")
    assert out["a"].error is None and out["b"].error == "budget"
    assert len(client.calls) == 1
    assert "overrun" in ops(env)  # the real cost is still recorded in full


def test_budget_refusal_says_split_the_batch(env, tmp_path):
    env[1].write_text(
        CAPS_TOML.format(checkout=tmp_path).replace("cap_usd = 60.0", "cap_usd = 0.000001")
    )
    with pytest.raises(B.BudgetRefused, match="split the batch") as ei:
        backend(env, FakeClient([])).run([req("a"), req("b")], purpose="p")
    assert ei.value.breakdown["stage"] == STAGE


# --- review r1: hard bound, shared instruction, sampling -----------------------------------------


def test_hard_bound_counts_every_byte_sent(env):
    big = "é" * 5_000  # 2 bytes per character: D3's estimate would under-count it
    for route in ("glm", "luna"):
        be = backend(env, FakeClient([]), route=route)
        p = be._prepare(req(user=big))
        user = big + A.schema_instruction(PASS1B_SCHEMA)
        n = len(b"You grade items.") + len(user.encode()) + 256
        if route == "luna":  # the native strict schema is sent too
            n += len(json.dumps(p.kwargs["response_format"]).encode())
        assert p.max_input_tokens == n
        prices = {be.model: {k: be.route.prices[k] for k in ("input", "output")}}
        assert p.worst_usd == B.bounded_cost(be.model, n, 500, prices)


def test_same_instruction_and_sampling_on_every_route(env):
    users, systems = set(), set()
    for route in A.ROUTES:
        be = backend(env, FakeClient([]), route=route, allow_unpinned=True)
        kw = be._prepare(req()).kwargs
        systems.add(kw["messages"][0]["content"])
        users.add(kw["messages"][1]["content"])
        assert kw["temperature"] == 0.0
    assert systems == {"You grade items."} and len(users) == 1
    assert users.pop().endswith("Answer with one JSON object that follows this schema.")
    # Free-text requests get no instruction.
    kw = backend(env, FakeClient([]))._prepare(req(schema=None)).kwargs
    assert kw["messages"][1]["content"] == "history text"


# --- review r1: what an attempt is charged ----------------------------------------------------


def _settled(env):
    return [e for e in events(env) if e["op"] == "settle"][-1]["charged_usd"]


def test_missing_usage_stays_at_bound_but_answer_is_kept(env):
    ans = answer(json.dumps(GOOD))
    del ans["usage"]
    client = FakeClient([ans])
    be = backend(env, client, route="luna")
    out = be.run([req()], purpose="p")
    r = out["k1"]
    assert r.error is None and r.data == GOOD
    assert r.meta["usage_known"] is False and r.meta["cost_source"] == "bound"
    worst = be._prepare(req()).worst_usd
    assert r.usd == pytest.approx(worst) and _settled(env) == pytest.approx(worst, abs=1e-6)
    again = backend(env, FakeClient([]), route="luna").run([req()], purpose="p")
    assert again["k1"].meta["cached"]  # cached as a success all the same


def test_missing_token_fields_on_a_direct_route_stay_at_bound(env):
    ans = answer(json.dumps(GOOD))
    ans["usage"] = {"prompt_tokens": 1000}  # no completion_tokens
    be = backend(env, FakeClient([ans]), route="luna")
    out = be.run([req()], purpose="p")
    assert out["k1"].meta["usage_known"] is False
    assert _settled(env) == pytest.approx(be._prepare(req()).worst_usd, abs=1e-6)


def test_openrouter_cost_without_tokens_is_billed_cost(env):
    ans = answer(json.dumps(GOOD))
    ans["usage"] = {"cost": 2e-5}
    out = backend(env, FakeClient([ans])).run([req()], purpose="p")
    assert out["k1"].meta["usage_known"] is False and out["k1"].meta["cost_source"] == "billed"
    assert _settled(env) == pytest.approx(2e-5)


@pytest.mark.parametrize(
    "code, billed",
    [
        (400, False),
        (404, False),
        (413, False),
        (422, False),
        (429, False),
        (408, True),
        (409, True),
        (500, True),
        (502, True),
        (503, True),
        (529, True),
    ],
)
def test_http_errors_released_only_when_unbilled(env, code, billed):
    be = backend(env, FakeClient([Status(code)]), max_retries=0)
    out = be.run([req()], purpose="p")
    assert out["k1"].error == f"http_{code}"
    worst = be._prepare(req()).worst_usd
    assert _settled(env) == pytest.approx(worst if billed else 0.0, abs=1e-6)


@pytest.mark.parametrize("code, billed", [(401, False), (403, False), (402, True)])
def test_abort_statuses_charge(env, code, billed):
    be = backend(env, FakeClient([Status(code)]))
    with pytest.raises(A.FatalAPIError):
        be.run([req()], purpose="p")
    worst = be._prepare(req()).worst_usd
    assert _settled(env) == pytest.approx(worst if billed else 0.0, abs=1e-6)


# --- review r1: cache recovery and settings ------------------------------------------------------


def test_torn_cache_tail_is_dropped_and_earlier_rows_kept(env):
    backend(env, FakeClient([answer(json.dumps(GOOD), cost=1e-5)])).run([req("a")], purpose="p")
    path = env[2] / "p.jsonl"
    good = path.read_bytes()
    with open(path, "ab") as fh:
        fh.write(b'{"cache_key": ["b", "tor')  # an interrupted append
    out = backend(env, FakeClient([])).run([req("a")], purpose="p")
    assert out["a"].meta["cached"]
    assert path.read_bytes() == good  # the torn fragment is gone
    # The next append lands on a clean line.
    backend(env, FakeClient([answer(json.dumps(GOOD), cost=1e-5)])).run([req("b")], purpose="p")
    assert len([json.loads(x) for x in path.read_text().splitlines()]) == 2


def test_last_row_without_newline_is_kept(env):
    backend(env, FakeClient([answer(json.dumps(GOOD), cost=1e-5)])).run([req("a")], purpose="p")
    path = env[2] / "p.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    backend(env, FakeClient([answer(json.dumps(GOOD), cost=1e-5)])).run(
        [req("a"), req("b")], purpose="p"
    )
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["key"] for r in rows] == ["a", "b"]


def test_malformed_earlier_cache_line_raises(env):
    backend(env, FakeClient([answer(json.dumps(GOOD), cost=1e-5)])).run([req("a")], purpose="p")
    path = env[2] / "p.jsonl"
    path.write_bytes(b"garbage\n" + path.read_bytes())
    with pytest.raises(ValueError, match="malformed cache line 1"):
        backend(env, FakeClient([])).run([req("a")], purpose="p")


def test_cache_reuse_needs_matching_settings(env):
    backend(env, FakeClient([answer(json.dumps(GOOD), cost=1e-5)])).run([req()], purpose="p")
    for kw, r in (({"temperature": 0.5}, req()), ({"seed": 7}, req()), ({}, req(max_out=600))):
        client = FakeClient([answer(json.dumps(GOOD), cost=1e-5)])
        backend(env, client, **kw).run([r], purpose="p")
        assert len(client.calls) == 1, kw
    pinned = dataclasses.replace(A.ROUTES["glm"], provider_pin="Friendli")
    client = FakeClient([answer(json.dumps(GOOD), cost=1e-5, provider="Friendli")])
    backend(env, client, route=pinned).run([req()], purpose="p")
    assert len(client.calls) == 1
    client = FakeClient([])
    out = backend(env, client).run([req()], purpose="p")  # the original settings: cached
    assert out["k1"].meta["cached"] and client.calls == []


# --- review r1: workers stop before settlement ---------------------------------------------------


class GatedClient:
    """A transport that yields. `plan[key]` is a list of actions, one per call of that request:
    "ok" (answer after a yield), "block" (wait until cancelled), or an exception (raised after a
    yield). Records the order of call starts, cancellations, and starts after settlement."""

    def __init__(self, plan, state):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.state = state
        self.order, self.cancelled = [], []
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        key = re.search(r"history-(\w+)", kw["messages"][1]["content"]).group(1)
        self.order.append(key)
        if self.state["settled"]:
            self.state["late_starts"] += 1
        action = self.plan[key].pop(0)
        try:
            await asyncio.sleep(0)
            if action == "block":
                await asyncio.Event().wait()
            if isinstance(action, BaseException):
                raise action
            return answer(json.dumps(GOOD), cost=1e-6, provider="Together")
        except asyncio.CancelledError:
            self.cancelled.append(key)
            raise

    async def close(self):
        self.state["closed_before_settle"] = not self.state["settled"]


@pytest.fixture
def gated(env, monkeypatch):
    state = {"settled": False, "late_starts": 0}
    real = B.settle

    def settle(*a, **kw):
        state["settled"] = True
        return real(*a, **kw)

    monkeypatch.setattr(B, "settle", settle)
    return state


def _scenario(be, reqs):
    """Run a batch inside our own loop, then keep the loop alive a little: any worker left
    running would start a request after settlement here."""

    async def go():
        try:
            await be.arun(reqs, purpose="p")
            err = None
        except Exception as e:  # noqa: BLE001 - the test inspects it
            err = e
        await asyncio.sleep(0.05)
        return err

    return asyncio.run(go())


def _gq(k):
    return req(k, user=f"history-{k}")


def test_cache_write_failure_stops_workers_before_settlement(env, gated, monkeypatch):
    calls = {"n": 0}
    real = A.append_row

    def flaky(path, row):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        real(path, row)

    monkeypatch.setattr(A, "append_row", flaky)
    plan = {"a": ["ok"], "b": ["block"], "c": ["ok"], "d": ["ok"]}
    client = GatedClient(plan, gated)
    be = backend(env, client, concurrency=2)
    err = _scenario(be, [_gq(k) for k in "abcd"])
    assert isinstance(err, OSError)
    assert gated["late_starts"] == 0 and gated["closed_before_settle"]
    assert client.order == ["a", "b"]  # dispatch stopped: c and d never started
    assert client.cancelled == ["b"]
    worst = be._prepare(_gq("b")).worst_usd
    assert _settled(env) == pytest.approx(1e-6 + worst, abs=1e-6)  # b cancelled in flight
    st = json.loads((env[0].parent / B.STATE_NAME).read_text())
    assert st["reservations"] == {}


def test_401_with_requests_in_flight(env, gated):
    plan = {"a": [Status(401)], "b": ["block"], "c": ["block"], "d": ["ok"]}
    client = GatedClient(plan, gated)
    be = backend(env, client, concurrency=3)
    err = _scenario(be, [_gq(k) for k in "abcd"])
    assert isinstance(err, A.FatalAPIError)
    assert gated["late_starts"] == 0 and gated["closed_before_settle"]
    assert client.order == ["a", "b", "c"] and sorted(client.cancelled) == ["b", "c"]
    worst = sum(be._prepare(_gq(k)).worst_usd for k in "bc")
    assert _settled(env) == pytest.approx(worst, abs=1e-6)  # the 401 itself is unbilled


def test_backoff_does_not_hold_a_slot(env, gated, monkeypatch):
    monkeypatch.setattr(A, "_backoff", lambda attempt, status: 0.02)
    plan = {"a": [Status(429), "ok"], "b": ["ok"]}
    client = GatedClient(plan, gated)
    err = _scenario(backend(env, client, concurrency=1), [_gq("a"), _gq("b")])
    assert err is None
    assert client.order == ["a", "b", "a"]  # b ran while a waited out its backoff


# --- review r2: billed and BYOK upstream costs ---------------------------------------------------

_ABSENT = object()


@pytest.mark.parametrize(
    "upstream, expect",
    [
        (_ABSENT, None),  # missing
        (None, None),  # null
        ("0.01", None),  # invalid type
        (-1e-5, None),  # negative
        (float("nan"), None),
        (float("inf"), None),
        (0, 2e-5),  # an explicit zero is a real amount
        (3e-5, 5e-5),
    ],
)
def test_byok_upstream_cost(env, upstream, expect):
    ans = answer(json.dumps(GOOD))
    ans["usage"]["cost"] = 2e-5
    ans["usage"]["is_byok"] = True
    if upstream is not _ABSENT:
        ans["usage"]["cost_details"] = {"upstream_inference_cost": upstream}
    be = backend(env, FakeClient([ans]))
    r = be.run([req()], purpose="p")["k1"]
    assert r.error is None and r.data == GOOD  # the answer is kept either way
    worst = be._prepare(req()).worst_usd
    if expect is None:
        assert r.meta["usage_known"] is False and r.meta["cost_source"] == "bound"
        assert _settled(env) == pytest.approx(worst, abs=1e-6)
    else:
        assert r.meta["usage_known"] is True and r.meta["cost_source"] == "billed+byok_upstream"
        assert _settled(env) == pytest.approx(expect, abs=1e-9)


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -1.0, "1e-5", True])
def test_invalid_billed_cost_keeps_the_bound(env, cost):
    ans = answer(json.dumps(GOOD))  # token counts present, but the reported cost is unusable
    ans["usage"]["cost"] = cost
    be = backend(env, FakeClient([ans]))
    r = be.run([req()], purpose="p")["k1"]
    assert r.data == GOOD and r.meta["usage_known"] is False and r.meta["cost_source"] == "bound"
    assert _settled(env) == pytest.approx(be._prepare(req()).worst_usd, abs=1e-6)


def test_text_mode_salvages_json_from_a_chatty_reply():
    obj, err, salvaged = A._parse_json('Here you go:\n{"grades": []}\nDone.')
    assert obj == {"grades": []} and err is None and salvaged
    assert A._parse_json("no json at all")[0] is None


def test_text_mode_rejects_ambiguous_or_superseded_objects():
    assert A._parse_json('{"items": []}\nCorrection: {"items": [')[0] is None
    assert A._parse_json('{"a": 1} and {"b": 2}')[0] is None
    assert A._parse_json('Sure.\n{"a": {"b": 1}}\nThanks')[0] == {"a": {"b": 1}}
