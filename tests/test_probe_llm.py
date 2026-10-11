"""emx.probe_llm: cache/resume, collisions, projection, ledger and reservations, retries, errors
(no network). Fake billed costs stay below the projection (~$0.0001 per GEM call at 256 output
tokens), since every attempt must fit the batch's reservation."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from emx import probe_llm as pl
from emx import spend

GEM = "google/gemma-4-31b-it"  # reasoning can be disabled (Gemini 3.8 Flash cannot)


class HTTPError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _resp(text="OK", cost=0.0001, provider="Google", choices=True):
    usage = NS(prompt_tokens=10, completion_tokens=2, model_extra={} if cost is None else
               {"cost": cost})
    ch = [NS(message=NS(content=text), finish_reason="stop")] if choices else []
    return NS(usage=usage, choices=ch, model_extra={"provider": provider})


class FakeClient:
    """``script(kw)`` returns a response or raises; every request's kwargs are kept."""

    def __init__(self, script):
        self.requests: list[dict] = []
        self.closed = False

        async def create(**kw):
            self.requests.append(kw)
            return script(kw)

        self.chat = NS(completions=NS(create=create))

    async def close(self):
        self.closed = True


@pytest.fixture
def env(tmp_path, monkeypatch):
    probes = tmp_path / "probes"
    (probes / "P03-demo").mkdir(parents=True)
    (probes / "P03-demo" / "probe.toml").write_text(
        'id = "P03"\nhard_cap_usd = 45\ngpu_limit_usd = 30\napi_limit_usd = 15\n'
    )
    monkeypatch.setattr(spend, "PROBES_DIR", probes)
    ledger = tmp_path / "costs.jsonl"
    monkeypatch.setenv(spend.LEDGER_ENV, str(ledger))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(pl, "_load_env", lambda: None)
    monkeypatch.setattr(pl, "_backoff", lambda attempt, status: 0.0)
    clients: list[FakeClient] = []

    def install(script):
        def make(transport, key):
            c = FakeClient(script)
            c.transport = transport
            clients.append(c)
            return c

        monkeypatch.setattr(pl, "_make_client", make)
        return clients

    return NS(tmp=tmp_path, ledger=ledger, cache=tmp_path / "cache.jsonl", install=install)


def _run(env, calls, **kw):
    return pl.run_calls(calls, exp="P03", cache=env.cache, purpose="test", **kw)


def _ledger(env):
    return [json.loads(x) for x in env.ledger.read_text().splitlines()] if env.ledger.exists() \
        else []


def _charges(env):
    """Ledger rows that are charges (not reservation op rows)."""
    return [r for r in _ledger(env) if r["resource"] != spend.RESERVATION_RESOURCE]


def test_cache_and_resume(env):
    clients = env.install(lambda kw: _resp())
    calls = [pl.Call(f"k{i}", GEM, f"q{i}", temperature=0.0, seed=1) for i in range(3)]
    out = _run(env, calls)
    assert {k: r.text for k, r in out.items()} == {"k0": "OK", "k1": "OK", "k2": "OK"}
    assert out["k0"].cost_source == "billed" and out["k0"].provider == "Google"
    rows = [json.loads(x) for x in env.cache.read_text().splitlines()]
    assert len(rows) == 3 and all(r["user_sha"] and r["system_sha"] is None for r in rows)
    sent = clients[0].requests[0]
    assert sent["temperature"] == 0.0 and sent["seed"] == 1 and sent["max_tokens"] == 256
    assert sent["extra_body"]["usage"] == {"include": True}
    assert sent["extra_body"]["reasoning"] == {"enabled": False}
    assert sent["extra_body"]["provider"]["require_parameters"] is True
    # second run: all cached, plus one new call
    out2 = _run(env, [*calls, pl.Call("k3", GEM, "q3")])
    assert len(out2) == 4 and out2["k0"] == out["k0"]
    assert len(clients[-1].requests) == 1 and "temperature" not in clients[-1].requests[0]


def test_errored_rows_are_retried_on_resume(env):
    env.install(lambda kw: (_ for _ in ()).throw(HTTPError(400)))
    out = _run(env, [pl.Call("k", GEM, "q")])
    assert out["k"].text is None and "400" in out["k"].error
    clients = env.install(lambda kw: _resp("fixed"))
    assert _run(env, [pl.Call("k", GEM, "q")])["k"].text == "fixed"
    assert len(clients[-1].requests) == 1


def test_key_collision_and_duplicates(env):
    env.install(lambda kw: _resp())
    _run(env, [pl.Call("k", GEM, "q")])
    with pytest.raises(ValueError, match="collision"):
        _run(env, [pl.Call("k", GEM, "a different prompt")])
    with pytest.raises(ValueError, match="collision"):
        _run(env, [pl.Call("k", GEM, "q", system="sys")])
    with pytest.raises(ValueError, match="duplicate"):
        _run(env, [pl.Call("x", GEM, "q"), pl.Call("x", GEM, "q")])


def test_projection():
    c = pl.Call("k", GEM, "u" * 30, system="s" * 30, max_tokens=100)
    p_in, p_out = pl.PRICE_PER_MTOK[GEM]
    assert pl.project_usd([c]) == pytest.approx(((60 + pl.OVERHEAD_TOKENS) * p_in + 100 * p_out) / 1e6)
    assert pl.project_usd([c, c]) == pytest.approx(2 * pl.project_usd([c]))
    with pytest.raises(ValueError, match="unknown route"):
        pl.project_usd([pl.Call("k", "nobody/model", "u")])
    assert pl.PRICE_PER_MTOK[pl.LUNA] == (0.10, 0.50)  # imported from emx.i9.contracts
    assert pl.GEMMA in pl.PROVIDER


def test_ledger_records_billed_and_table(env):
    def script(kw):
        return _resp(cost=None) if kw["model"] == pl.LUNA else _resp(cost=0.00005)

    clients = env.install(script)
    calls = [pl.Call("a", GEM, "q"), pl.Call("b", GEM, "q2"), pl.Call("c", pl.LUNA, "q",
                                                                      temperature=0.0)]
    out = _run(env, calls)
    assert out["c"].cost_source == "table"
    assert out["c"].usd == pytest.approx(pl.price_usd(pl.LUNA, 10, 2))
    luna_req = next(r for c in clients if c.transport == "openai" for r in c.requests)
    assert luna_req["max_completion_tokens"] == 256 and luna_req["reasoning_effort"] == "none"
    assert "extra_body" not in luna_req
    rows = {r["resource"]: r for r in _ledger(env) if r["resource"] != spend.RESERVATION_RESOURCE}
    assert set(rows) == {GEM, pl.LUNA}
    assert rows[GEM]["exp"] == "P03" and rows[GEM]["units"] == 2
    assert rows[GEM]["usd"] == pytest.approx(0.0001)
    assert "[P03 test]" in rows[GEM]["note"]
    assert all(c.closed for c in clients)


def test_budget_guard_blocks_before_calls(env):
    clients = env.install(lambda kw: _resp())
    big = pl.Call("k", "anthropic/claude-sonnet-5", "q", max_tokens=2_000_000)  # ~$20 projected
    with pytest.raises(spend.BudgetExceeded):
        _run(env, [big])
    assert clients == [] and _ledger(env) == []


def test_transient_retry_then_success(env):
    state = {"n": 0}

    def script(kw):
        state["n"] += 1
        if state["n"] == 1:
            raise HTTPError(429)
        if state["n"] == 2:
            return _resp(choices=False, cost=0.00002)  # billed, but no choices: retried
        return _resp(cost=0.00004)

    env.install(script)
    r = _run(env, [pl.Call("k", GEM, "q")])["k"]
    assert r.text == "OK" and r.error is None and state["n"] == 3
    assert r.usd == pytest.approx(0.00006)
    assert _charges(env)[0]["units"] == 2  # two responses


def test_transient_exhausted_lands_as_error(env):
    env.install(lambda kw: (_ for _ in ()).throw(HTTPError(503)))
    r = _run(env, [pl.Call("k", GEM, "q")], max_retries=2)["k"]
    assert r.text is None and "503" in r.error


def test_non_transient_lands_without_retry(env):
    clients = env.install(lambda kw: (_ for _ in ()).throw(HTTPError(404)))
    out = _run(env, [pl.Call("k", GEM, "q"), pl.Call("j", GEM, "q2")])
    assert all(r.error and "404" in r.error for r in out.values())
    assert len(clients[0].requests) == 2  # one attempt each


def test_auth_error_aborts_batch(env):
    def script(kw):
        if "bad" in kw["messages"][-1]["content"]:
            raise HTTPError(402)
        return _resp(cost=0.00005)

    env.install(script)
    calls = [pl.Call("ok", GEM, "fine"), pl.Call("bad", GEM, "bad"),
             *(pl.Call(f"q{i}", GEM, f"q{i}") for i in range(5))]
    with pytest.raises(pl.FatalAPIError):
        _run(env, calls, concurrency=1)
    assert [r["key"] for r in map(json.loads, env.cache.read_text().splitlines())] == ["ok"]
    assert _charges(env)[0]["usd"] == pytest.approx(0.00005)  # recorded despite the raise
    assert spend.open_reservations("P03", ledger=env.ledger) == {}  # and settled
    assert spend.spent("judge", "P03", ledger=env.ledger) == pytest.approx(0.00005)


def test_dry_run(env, capsys):
    clients = env.install(lambda kw: _resp())
    assert _run(env, [pl.Call("k", GEM, "q")], dry_run=True) == {}
    assert "1 pending" in capsys.readouterr().out
    assert clients == [] and not env.cache.exists() and _ledger(env) == []  # no reservation


def test_json_mode_and_reasoning(env):
    clients = env.install(lambda kw: _resp('{"a": 1}'))
    _run(env, [pl.Call("k", GEM, "q", json_mode=True, reasoning="default")])
    req = clients[0].requests[0]
    assert req["response_format"] == {"type": "json_object"}
    assert "reasoning" not in req["extra_body"]
    with pytest.raises(ValueError, match="reasoning"):
        _run(env, [pl.Call("z", GEM, "q", reasoning="maximum")])
    assert pl._request(pl.Call("h", GEM, "q", reasoning="high"), 10)["extra_body"][
        "reasoning"] == {"max_tokens": pl.HIGH_REASONING_BUDGET}


def test_mandatory_reasoning_route_refuses_none(env):
    clients = env.install(lambda kw: _resp())
    with pytest.raises(ValueError, match="cannot disable reasoning"):
        _run(env, [pl.Call("k", "google/gemini-3.8-flash", "q")])
    _run(env, [pl.Call("k", "google/gemini-3.8-flash", "q", reasoning="default")])
    assert len(clients) == 1 and "reasoning" not in clients[0].requests[0]["extra_body"]


# ---------------------------------------------------------------- reservations (review r1 1.3)


def test_reservation_lifecycle(env):
    env.install(lambda kw: _resp(cost=0.00005))
    calls = [pl.Call("a", GEM, "q"), pl.Call("b", GEM, "q2")]
    _run(env, calls)
    rows = _ledger(env)
    ops = [r["op"] for r in rows if "op" in r]
    assert ops == ["reserve", "submitted", "close"]
    rid = rows[0]["reservation_id"]
    assert rows[0]["note"] == "probe_llm:test"
    want = pl.project_usd(calls) * pl.RETRY_MARGIN
    assert rows[0]["reserve_usd"] == pytest.approx(want, abs=1e-6)  # the ledger rounds to 1e-6
    (charge,) = _charges(env)
    assert charge["reservation_id"] == rid and charge["usd"] == pytest.approx(0.0001)
    assert rows[-1]["op"] == "close" and rows[-1]["usd"] == 0.0  # nothing uncertain
    assert spend.open_reservations("P03", ledger=env.ledger) == {}
    assert spend.totals("P03", ledger=env.ledger)["total_usd"] == pytest.approx(0.0001)


def test_second_admission_sees_open_liability(env):
    clients = env.install(lambda kw: _resp())
    rid = spend.reserve_api("P03", 14.9999, "probe_llm:other", ledger=env.ledger)  # limit $15
    with pytest.raises(spend.BudgetExceeded):
        _run(env, [pl.Call("k", GEM, "q")])
    assert clients == []
    spend.release_api(rid, ledger=env.ledger)
    assert _run(env, [pl.Call("k", GEM, "q")])["k"].text == "OK"


def test_open_reservation_with_same_note_is_refused(env):
    clients = env.install(lambda kw: _resp())
    rid = spend.reserve_api("P03", 0.01, "probe_llm:test", ledger=env.ledger)
    spend.mark_submitted(rid, ledger=env.ledger)  # a batch that died mid-run
    with pytest.raises(spend.ReservationError, match="recover_uncertain"):
        _run(env, [pl.Call("k", GEM, "q")])
    assert clients == []
    assert spend.recover_uncertain("P03", "probe_llm:test", ledger=env.ledger) == [rid]
    assert spend.spent("judge", "P03", ledger=env.ledger) == pytest.approx(0.01)  # full amount
    assert _run(env, [pl.Call("k", GEM, "q")])["k"].text == "OK"


def test_uncertain_attempts_are_charged_at_worst_case(env):
    env.install(lambda kw: (_ for _ in ()).throw(TimeoutError("read timeout")))
    c = pl.Call("k", GEM, "q")
    r = _run(env, [c], max_retries=0)["k"]
    assert "Timeout" in r.error
    assert _charges(env) == []  # no response landed
    close = _ledger(env)[-1]
    assert close["op"] == "close" and close["usd"] == pytest.approx(pl.project_usd([c]), abs=1e-6)
    assert spend.open_reservations("P03", ledger=env.ledger) == {}


def test_attempts_beyond_the_reservation_are_not_sent(env):
    clients = env.install(lambda kw: _resp(cost=0.001))  # ~10x the projection per call
    calls = [pl.Call(f"k{i}", GEM, f"q{i}") for i in range(3)]
    out = _run(env, calls, concurrency=1)
    assert out["k0"].text == "OK"
    assert all(out[k].error.startswith("ReservationExhausted") for k in ("k1", "k2"))
    assert len(clients[0].requests) == 1
    assert spend.spent("judge", "P03", ledger=env.ledger) == pytest.approx(0.001)


# ---------------------------------------------------------------- req_sha (review r1 1.5)


def test_req_sha_mismatch_raises_and_legacy_rows_are_reused(env):
    clients = env.install(lambda kw: _resp())
    _run(env, [pl.Call("k", GEM, "q", max_tokens=64)])
    assert json.loads(env.cache.read_text())["req_sha"]
    for changed in ({"max_tokens": 128}, {"temperature": 0.0}, {"seed": 1},
                    {"reasoning": "default"}, {"json_mode": True}):
        kw = {"max_tokens": 64, **changed}
        with pytest.raises(ValueError, match="other request settings"):
            _run(env, [pl.Call("k", GEM, "q", **kw)])
    assert _run(env, [pl.Call("k", GEM, "q", max_tokens=64)])["k"].text == "OK"  # same: reused
    # a legacy row (written before req_sha) raises unless legacy_ok, then is reused as is
    row = json.loads(env.cache.read_text())
    del row["req_sha"]
    env.cache.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="legacy_ok"):
        _run(env, [pl.Call("k", GEM, "q", max_tokens=999)])
    assert _run(env, [pl.Call("k", GEM, "q", max_tokens=999)], legacy_ok=True)["k"].text == "OK"
    assert len(clients) == 1  # only the first run sent anything


def test_projection_bounds_input_by_utf8_bytes():
    ascii_call = pl.Call("a", GEM, "x" * 300, max_tokens=1)
    cjk_call = pl.Call("b", GEM, "\u6f22" * 100, max_tokens=1)  # 100 chars, 300 bytes
    assert pl.project_usd([cjk_call]) == pytest.approx(pl.project_usd([ascii_call]))
    p_in, _ = pl.PRICE_PER_MTOK[GEM]
    assert pl.project_usd([ascii_call]) >= (300 + pl.OVERHEAD_TOKENS) * p_in / 1e6
