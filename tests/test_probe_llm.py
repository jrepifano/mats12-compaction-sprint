"""emx.probe_llm: cache/resume, collisions, projection, ledger, retries, errors (no network)."""

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
    assert pl.project_usd([c]) == pytest.approx(((60 / 3 + 30) * p_in + 100 * p_out) / 1e6)
    assert pl.project_usd([c, c]) == pytest.approx(2 * pl.project_usd([c]))
    with pytest.raises(ValueError, match="unknown route"):
        pl.project_usd([pl.Call("k", "nobody/model", "u")])
    assert pl.PRICE_PER_MTOK[pl.LUNA] == (0.10, 0.50)  # imported from emx.i9.contracts
    assert pl.GEMMA in pl.PROVIDER


def test_ledger_records_billed_and_table(env):
    def script(kw):
        return _resp(cost=None) if kw["model"] == pl.LUNA else _resp(cost=0.002)

    clients = env.install(script)
    calls = [pl.Call("a", GEM, "q"), pl.Call("b", GEM, "q2"), pl.Call("c", pl.LUNA, "q",
                                                                      temperature=0.0)]
    out = _run(env, calls)
    assert out["c"].cost_source == "table"
    assert out["c"].usd == pytest.approx(pl.price_usd(pl.LUNA, 10, 2))
    luna_req = next(r for c in clients if c.transport == "openai" for r in c.requests)
    assert luna_req["max_completion_tokens"] == 256 and luna_req["reasoning_effort"] == "none"
    assert "extra_body" not in luna_req
    rows = {r["resource"]: r for r in _ledger(env)}
    assert set(rows) == {GEM, pl.LUNA}
    assert rows[GEM]["exp"] == "P03" and rows[GEM]["units"] == 2
    assert rows[GEM]["usd"] == pytest.approx(0.004)
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
            return _resp(choices=False, cost=0.0005)  # billed, but no choices: retried
        return _resp(cost=0.001)

    env.install(script)
    r = _run(env, [pl.Call("k", GEM, "q")])["k"]
    assert r.text == "OK" and r.error is None and state["n"] == 3
    assert r.usd == pytest.approx(0.0015)
    assert _ledger(env)[0]["units"] == 2  # two responses


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
        return _resp(cost=0.001)

    env.install(script)
    calls = [pl.Call("ok", GEM, "fine"), pl.Call("bad", GEM, "bad"),
             *(pl.Call(f"q{i}", GEM, f"q{i}") for i in range(5))]
    with pytest.raises(pl.FatalAPIError):
        _run(env, calls, concurrency=1)
    assert [r["key"] for r in map(json.loads, env.cache.read_text().splitlines())] == ["ok"]
    assert _ledger(env)[0]["usd"] == pytest.approx(0.001)  # spend recorded despite the raise


def test_dry_run(env, capsys):
    clients = env.install(lambda kw: _resp())
    assert _run(env, [pl.Call("k", GEM, "q")], dry_run=True) == {}
    assert "1 pending" in capsys.readouterr().out
    assert clients == [] and not env.cache.exists() and _ledger(env) == []


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
