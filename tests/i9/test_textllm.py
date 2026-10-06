"""emx.i9.textllm: the free-text client and the wsbench adapter (implementation plan §1.4, §2 U4).

Offline: every test runs on fake OpenAI- and Anthropic-shaped clients installed behind
``textllm._make_client``; the ledger is a temp file (``EMX_COSTS_LEDGER``) and the journal goes to
a temp ``CALLS_DIR``. The fakes are imported by tests/i9/test_wsjudge.py.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest
from wsbench import llm
from wsbench.llm import JudgeConfigError, Spend

from emx import spend as emx_spend
from emx.i9 import contracts as C
from emx.i9 import textllm

# ---------------------------------------------------------------- fakes


def status_error(code: int, name: str = "APIStatusError") -> Exception:
    """An SDK-like error carrying ``status_code`` (and the SDK's class name, if given)."""
    cls = type(name, (Exception,), {"status_code": code})
    return cls(f"fake {code}")


class Refuse:
    """Responder sentinel: the model refuses."""


class Empty:
    """Responder sentinel: an empty reply cut at the token budget."""


class NoChoices:
    """Responder sentinel: a 200 whose body has no choices (OpenAI shape only)."""


class _Base:
    """Shared bookkeeping: ``responder(user, kw)`` returns text, an exception to raise,
    ``Refuse`` or ``Empty``. Tracks every request's kwargs and the peak number in flight."""

    def __init__(self, responder, usage=(100, 20), delay: float = 0.0):
        self.responder = responder
        self.usage = usage
        self.delay = delay
        self.kwargs: list[dict] = []
        self.in_flight = 0
        self.peak = 0
        self.closed = False

    async def _respond(self, kw: dict) -> Any:
        self.kwargs.append(kw)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            r = self.responder(self.user_of(kw), kw)
        finally:
            self.in_flight -= 1
        if isinstance(r, BaseException):
            raise r
        return r

    def users(self) -> list[str]:
        return [self.user_of(kw) for kw in self.kwargs]

    async def close(self):
        self.closed = True


class FakeOpenAI(_Base):
    """OpenAI-shaped (luna, and every OpenRouter model). ``cost`` (OpenRouter's billed
    ``usage.cost``) and ``provider`` (the response's routed provider) are left off unless given."""

    def __init__(self, responder, *, cost=None, provider=None, **kw):
        super().__init__(responder, **kw)
        self.cost, self.provider = cost, provider
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @staticmethod
    def user_of(kw: dict) -> str:
        return kw["messages"][-1]["content"]

    async def _create(self, **kw):
        r = await self._respond(kw)
        usage = SimpleNamespace(prompt_tokens=self.usage[0], completion_tokens=self.usage[1])
        if self.cost is not None:
            usage.cost = self.cost
        if r is NoChoices:
            return SimpleNamespace(choices=[], usage=usage)
        if r is Refuse:
            msg, finish = SimpleNamespace(content=None, refusal="no"), "stop"
        elif r is Empty:
            msg, finish = SimpleNamespace(content="", refusal=None), "length"
        else:
            msg, finish = SimpleNamespace(content=r, refusal=None), "stop"
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason=finish)], usage=usage
        )
        if self.provider is not None:
            resp.provider = self.provider
        return resp


class _Stream:
    def __init__(self, fake: FakeAnthropic, kw: dict):
        self.fake, self.kw = fake, kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get_final_message(self) -> Any:
        r = await self.fake._respond(self.kw)
        usage = SimpleNamespace(input_tokens=self.fake.usage[0], output_tokens=self.fake.usage[1])
        if r is Refuse:
            return SimpleNamespace(stop_reason="refusal", content=[], usage=usage)
        if r is Empty:
            return SimpleNamespace(stop_reason="max_tokens", content=[], usage=usage)
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=r)], usage=usage
        )


class FakeAnthropic(_Base):
    def __init__(self, responder, **kw):
        super().__init__(responder, **kw)
        self.messages = SimpleNamespace(stream=lambda **kw: _Stream(self, kw))

    @staticmethod
    def user_of(kw: dict) -> str:
        return kw["messages"][-1]["content"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fake keys, a temp ledger and journal dir, no backoff; records every paced rpm. Returns a
    namespace with ``install(responder, transport=None, **kw) -> fake``, ``rpms``, ``made``."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv(emx_spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    monkeypatch.setattr(C, "CALLS_DIR", tmp_path / "calls")
    monkeypatch.setattr(textllm, "_backoff", lambda attempt, status: 0.0)
    ns = SimpleNamespace(rpms=[], made=[], tmp=tmp_path, fake=None)

    async def pace(rpm: float) -> None:
        ns.rpms.append(rpm)

    monkeypatch.setattr(textllm, "_pace", pace)

    def no_client(transport, key):
        raise AssertionError("no client may be built")

    monkeypatch.setattr(textllm, "_make_client", no_client)

    def install(responder, **kw):
        fakes: dict[str, Any] = {}

        def make(transport: str, key: str) -> Any:
            ns.made.append(transport)
            if transport not in fakes:
                cls = FakeAnthropic if transport == "anthropic" else FakeOpenAI
                fakes[transport] = cls(responder, **kw)
            return fakes[transport]

        monkeypatch.setattr(textllm, "_make_client", make)
        ns.fakes = fakes
        return fakes

    ns.install = install
    return ns


def echo(user: str, kw: dict) -> str:
    return f"reply to {user}"


def run(prompts, **kw) -> tuple[dict[int, str | None], Spend]:
    got: dict[int, str | None] = {}

    def on_result(i: int, text: str | None) -> None:
        assert i not in got
        got[i] = text

    kw.setdefault("max_tokens", 100)
    kw.setdefault("reasoning", "none")
    sp = textllm.complete(prompts, on_result=on_result, **kw)
    return got, sp


def journal(route: str, purpose: str = "textllm") -> list[dict]:
    path = textllm.journal_path(purpose, route)
    return [json.loads(x) for x in path.read_text().splitlines()]


# ---------------------------------------------------------------- request shapes and spend


def test_luna_request_shape_spend_and_journal(env):
    fakes = env.install(echo)
    sp_in = Spend()
    got, sp = run(["a", "b"], route=C.LUNA, spend=sp_in, timeout=33.0, system="SYS")
    assert sp is sp_in
    assert got == {0: "reply to a", 1: "reply to b"}
    kw = fakes["openai"].kwargs[0]
    assert kw["model"] == C.LUNA and kw["max_completion_tokens"] == 100
    assert kw["reasoning_effort"] == "none" and kw["timeout"] == 33.0
    assert kw["messages"][0] == {"role": "system", "content": "SYS"}
    assert "max_tokens" not in kw and "extra_body" not in kw
    assert sp.calls == 2 and sp.input_tokens == 200 and sp.output_tokens == 40
    assert sp.usd == pytest.approx(2 * (100 * 0.10 + 20 * 0.50) / 1e6)
    assert sp.errors == sp.refusals == sp.retries == 0
    rows = journal(C.LUNA)
    assert len(rows) == 2 and {r["status"] for r in rows} == {"ok"}
    assert {r["text"] for r in rows} == {"reply to a", "reply to b"}
    assert all(r["responses"] == 1 and r["input_tokens"] == 100 for r in rows)
    # the ledger has the batch's actual spend, tagged I9 judge
    entries = emx_spend.read_entries()
    assert len(entries) == 1 and entries[0]["exp"] == "I9" and entries[0]["kind"] == "judge"
    assert entries[0]["usd"] == pytest.approx(sp.usd) and entries[0]["units"] == 2
    assert fakes["openai"].closed


@pytest.mark.parametrize(
    "reasoning,want", [("none", {"enabled": False}), ("low", {"effort": "low"}), ("default", None)]
)
def test_openrouter_reasoning(env, reasoning, want):
    fakes = env.install(echo)
    run(["x"], route=C.DEEPSEEK, reasoning=reasoning, max_tokens=50)
    assert env.made == ["openrouter"]
    kw = fakes["openrouter"].kwargs[0]
    assert kw["max_tokens"] == 50 and "max_completion_tokens" not in kw
    assert kw["extra_body"].get("reasoning") == want
    assert kw["extra_body"]["usage"] == {"include": True}
    assert len(kw["messages"]) == 1  # user-only, like wsbench's free-text stages


@pytest.mark.parametrize(
    "reasoning,want", [("none", {"enabled": False}), ("low", {"effort": "low"}), ("default", None)]
)
def test_sonnet_goes_through_openrouter(env, reasoning, want):
    fakes = env.install(echo)
    got, sp = run(["x"], route=C.SONNET, reasoning=reasoning, max_tokens=400)
    assert env.made == ["openrouter"]
    kw = fakes["openrouter"].kwargs[0]
    assert kw["model"] == "anthropic/claude-sonnet-5" and kw["max_tokens"] == 400
    assert kw["extra_body"].get("reasoning") == want
    assert got == {0: "reply to x"}
    assert sp.usd == pytest.approx((100 * 2.0 + 20 * 10.0) / 1e6)


@pytest.mark.parametrize(
    "reasoning,thinking,effort",
    [("none", "disabled", None), ("low", "adaptive", "low"), ("default", "adaptive", None)],
)
def test_anthropic_transport_request_shape(env, monkeypatch, reasoning, thinking, effort):
    """Kept for a direct Anthropic key: used only if contracts.ROUTE says "anthropic"."""
    monkeypatch.setitem(C.ROUTE, C.SONNET, "anthropic")
    fakes = env.install(echo)
    got, sp = run(["x"], route=C.SONNET, reasoning=reasoning, max_tokens=400)
    kw = fakes["anthropic"].kwargs[0]
    assert kw["thinking"] == {"type": thinking} and kw["max_tokens"] == 400
    assert kw.get("output_config", {}).get("effort") == effort
    assert "system" not in kw and kw["messages"] == [{"role": "user", "content": "x"}]
    assert got == {0: "reply to x"}
    assert sp.usd == pytest.approx((100 * 2.0 + 20 * 10.0) / 1e6)


# ---------------------------------------------------------------- provider pinning and billed cost


@pytest.mark.parametrize("route", [C.DEEPSEEK, C.GLM, C.SONNET, C.GEMINI])
def test_openrouter_sends_pinned_provider(env, route):
    """Jacob, 2026-10-04: every OpenRouter call carries contracts.PROVIDER[route] plus
    require_parameters, field for field."""
    before = json.dumps(C.PROVIDER, sort_keys=True)
    fakes = env.install(echo)
    run(["x"], route=route)
    sent = fakes["openrouter"].kwargs[0]["extra_body"]["provider"]
    assert sent == {**C.PROVIDER[route], "require_parameters": True}
    assert json.dumps(C.PROVIDER, sort_keys=True) == before  # the contract is not mutated


def test_deepseek_provider_object_exact(env):
    fakes = env.install(echo)
    run(["x"], route=C.DEEPSEEK)
    sent = fakes["openrouter"].kwargs[0]["extra_body"]["provider"]
    assert sent == {
        "only": ["DeepInfra", "GMICloud", "Novita", "SiliconFlow", "Parasail", "Baidu",
                 "Alibaba"],
        "order": ["DeepInfra", "GMICloud", "Novita"],
        "quantizations": ["fp8", "bf16"], "max_price": {"prompt": 0.14, "completion": 0.28},
        "require_parameters": True,
    }
    assert "allow_fallbacks" not in sent  # r2 finding 7: a hard allow-list, not a preference


@pytest.mark.parametrize("route", [C.DEEPSEEK, C.GLM, C.SONNET])
def test_pinned_routes_send_a_hard_only_list(env, route):
    """Every approved list is a hard `only` allow-list that contains the preferred order."""
    fakes = env.install(echo)
    run(["x"], route=route)
    sent = fakes["openrouter"].kwargs[0]["extra_body"]["provider"]
    assert sent["only"] == C.PROVIDER[route]["only"] and set(sent["order"]) <= set(sent["only"])
    assert sent["require_parameters"] is True and "allow_fallbacks" not in sent


def test_luna_sends_no_provider_object(env):
    fakes = env.install(echo)
    run(["x"], route=C.LUNA)
    assert "extra_body" not in fakes["openai"].kwargs[0]


def test_billed_cost_wins_over_table_and_provider_is_journaled(env):
    env.install(echo, cost=0.00123, provider="DeepInfra")
    _, sp = run(["a", "b"], route=C.DEEPSEEK)
    assert sp.usd == pytest.approx(2 * 0.00123)  # not the table's (100*0.14 + 20*0.28)/1e6
    rows = journal(C.DEEPSEEK)
    assert {r["cost_source"] for r in rows} == {"billed"}
    assert {r["provider"] for r in rows} == {"DeepInfra"}
    assert all(r["usd"] == pytest.approx(0.00123) and r["provider_cost"] == pytest.approx(0.00123)
               for r in rows)
    assert emx_spend.read_entries()[0]["usd"] == pytest.approx(2 * 0.00123)


def test_table_price_when_cost_absent(env):
    env.install(echo)
    _, sp = run(["a"], route=C.GLM)
    assert sp.usd == pytest.approx((100 * 0.15 + 20 * 0.50) / 1e6)
    (row,) = journal(C.GLM)
    assert row["cost_source"] == "table" and row["provider"] is None
    assert row["provider_cost"] is None


def test_billed_cost_read_from_model_extra(env):
    """The SDK keeps unknown fields in pydantic's model_extra; both places are read."""
    fakes = env.install(echo)
    fake = fakes.setdefault("openrouter", FakeOpenAI(echo))
    orig = fake._create

    async def create(**kw):
        resp = await orig(**kw)
        resp.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                     model_extra={"cost": 0.5})
        resp.model_extra = {"provider": "Novita"}
        return resp

    fake.chat.completions.create = create
    _, sp = run(["a"], route=C.DEEPSEEK)
    (row,) = journal(C.DEEPSEEK)
    assert sp.usd == pytest.approx(0.5) and row["cost_source"] == "billed"
    assert row["provider"] == "Novita"


def test_luna_default_reasoning_sends_no_effort(env):
    fakes = env.install(echo)
    run(["x"], route=C.LUNA, reasoning="default")
    assert "reasoning_effort" not in fakes["openai"].kwargs[0]


def test_concurrency_rpm_timeout_honoured(env):
    fakes = env.install(echo, delay=0.01)
    run([str(i) for i in range(7)], route=C.GLM, concurrency=2, rpm=17.0, timeout=9.0)
    fake = fakes["openrouter"]
    assert fake.peak == 2 and len(fake.kwargs) == 7
    assert env.rpms == [17.0] * 7
    assert {kw["timeout"] for kw in fake.kwargs} == {9.0}


def test_real_pacer_spaces_requests(monkeypatch):
    t = {"now": 100.0}
    monkeypatch.setattr(textllm.time, "monotonic", lambda: t["now"])
    p = textllm._Pacer()
    assert p.reserve(60.0) == 0.0
    assert p.reserve(60.0) == pytest.approx(1.0)
    assert p.reserve(120.0) == pytest.approx(2.0)


# ---------------------------------------------------------------- failures


def test_ordinary_failure_is_none_and_journaled(env):
    def responder(user, kw):
        return status_error(400, "BadRequestError") if user == "bad" else echo(user, kw)

    fakes = env.install(responder)
    got, sp = run(["a", "bad", "c"], route=C.LUNA)
    assert got == {0: "reply to a", 1: None, 2: "reply to c"}
    assert sp.errors == 1 and sp.retries == 0 and sp.calls == 2
    assert len(fakes["openai"].kwargs) == 3  # a 400 is not retried
    rows = journal(C.LUNA)
    assert len(rows) == 3
    assert sorted(r["status"] for r in rows) == ["error", "ok", "ok"]
    bad = next(r for r in rows if r["status"] == "error")
    assert bad["index"] == 1 and "BadRequestError" in bad["error"] and bad["text"] is None


def test_transient_failure_is_retried(env):
    seen: dict[str, int] = {}

    def responder(user, kw):
        seen[user] = seen.get(user, 0) + 1
        if user == "flaky" and seen[user] == 1:
            return status_error(503)
        if user == "dead":
            return status_error(429, "RateLimitError")
        return echo(user, kw)

    env.install(responder)
    got, sp = run(["flaky", "dead"], route=C.LUNA)
    assert got == {0: "reply to flaky", 1: None}
    assert seen == {"flaky": 2, "dead": textllm.ATTEMPTS}
    assert sp.retries == 1 + (textllm.ATTEMPTS - 1) and sp.errors == 1
    rows = {r["index"]: r for r in journal(C.LUNA)}
    assert rows[0]["attempts"] == 2 and rows[1]["attempts"] == textllm.ATTEMPTS


def test_no_choices_is_transient(env):
    seen = {"n": 0}

    def responder(user, kw):
        seen["n"] += 1
        return NoChoices if seen["n"] == 1 else "ok"

    env.install(responder)
    got, sp = run(["x"], route=C.LUNA)
    assert got == {0: "ok"} and sp.retries == 1 and sp.calls == 2 and sp.errors == 0


@pytest.mark.parametrize("code", [401, 402, 403])
def test_fatal_status_raises_and_aborts(env, code):
    def responder(user, kw):
        return status_error(code) if user == "p1" else echo(user, kw)

    fakes = env.install(responder)
    got: dict[int, str | None] = {}
    with pytest.raises(textllm.TextLLMConfigError, match=str(code)):
        textllm.complete(
            [f"p{i}" for i in range(6)],
            route=C.LUNA,
            max_tokens=10,
            reasoning="none",
            on_result=lambda i, t: got.__setitem__(i, t),
            concurrency=1,
        )
    fake = fakes["openai"]
    assert len(fake.kwargs) == 2  # p0 landed, p1 fatal, p2..p5 never sent
    assert got == {0: "reply to p0"} and fake.closed
    rows = journal(C.LUNA)
    assert [r["status"] for r in rows] == ["ok", "fatal"]
    entries = emx_spend.read_entries()  # the landed call's spend is still recorded
    assert len(entries) == 1 and entries[0]["units"] == 1
    assert issubclass(textllm.TextLLMConfigError, JudgeConfigError)


def test_fatal_by_sdk_class_name(env):
    env.install(lambda u, kw: status_error(None, "AuthenticationError"))
    with pytest.raises(textllm.TextLLMConfigError, match="AuthenticationError"):
        run(["x"], route=C.SONNET)


@pytest.mark.parametrize("transport", ["openrouter", "anthropic"])
def test_refusals(env, monkeypatch, transport):
    monkeypatch.setitem(C.ROUTE, C.SONNET, transport)
    env.install(lambda u, kw: Refuse if u == "no" else echo(u, kw))
    got, sp = run(["no", "yes"], route=C.SONNET)
    assert got == {0: None, 1: "reply to yes"} and sp.refusals == 1 and sp.errors == 0
    got, sp = run(["no"], route=C.LUNA)
    assert got == {0: None} and sp.refusals == 1
    assert [r["status"] for r in journal(C.LUNA)] == ["refusal"]


@pytest.mark.parametrize("transport", ["openrouter", "anthropic"])
def test_empty_reply_at_budget_doubles_it(env, monkeypatch, transport):
    monkeypatch.setitem(C.ROUTE, C.SONNET, transport)
    fakes = env.install(lambda u, kw: Empty if kw["max_tokens"] < 1600 else "done")
    got, sp = run(["x"], route=C.SONNET, max_tokens=400)
    assert got == {0: "done"}
    assert [kw["max_tokens"] for kw in fakes[transport].kwargs] == [400, 800, 1600]
    assert sp.calls == 3 and journal(C.SONNET)[0]["responses"] == 3


def test_budget_doublings_are_bounded(env, monkeypatch):
    """r2 finding 6: at most MAX_BUDGET_DOUBLINGS re-asks; then the empty reply lands as ""."""
    monkeypatch.setitem(C.ROUTE, C.SONNET, "openrouter")
    fakes = env.install(lambda u, kw: Empty)
    got, sp = run(["x"], route=C.SONNET, max_tokens=400)
    assert textllm.MAX_BUDGET_DOUBLINGS == 2
    assert [kw["max_tokens"] for kw in fakes["openrouter"].kwargs] == [400, 800, 1600]
    assert got == {0: ""} and sp.calls == 3


def test_budget_reask_rechecks_the_guard(env, monkeypatch):
    """r2 finding 6: each re-ask re-runs check_total on the batch's running reservation (upfront
    projection + re-asks so far), before the call."""
    seen: list[float] = []
    real = emx_spend.check_total

    def spy(projected, kind, **kw):
        seen.append(projected)
        return real(projected, kind, **kw)

    monkeypatch.setattr(emx_spend, "check_total", spy)
    fakes = env.install(lambda u, kw: Empty if kw["max_tokens"] < 1600 else "done")
    got, _ = run(["x" * 300], route=C.SONNET, max_tokens=400)
    assert got == {0: "done"}
    up = textllm.project_usd(["x" * 300], route=C.SONNET, system=None, max_tokens=400)
    tin = int(300 / textllm.CHARS_PER_TOKEN_EST + textllm.OVERHEAD_TOKENS) + 1
    r1, r2 = (textllm.price_usd(C.SONNET, tin, b) for b in (800, 1600))
    assert seen == pytest.approx([up, up + r1, up + r1 + r2])
    assert len(fakes["openrouter"].kwargs) == 3


def test_budget_reask_over_cap_raises_before_the_call(env, monkeypatch):
    """The upfront batch fits the cap, the doubled re-ask would not: it raises, the re-ask is
    never sent, and the call that was made is still recorded in the ledger."""
    up = textllm.project_usd(["x"], route=C.SONNET, system=None, max_tokens=1000)
    cap = up * 1.5  # the batch fits; batch + a 2000-token re-ask (~2x up) does not
    monkeypatch.setitem(emx_spend.CAPS, "I9", emx_spend.Caps(cap, 0.0, None, 0.0))
    fakes = env.install(lambda u, kw: Empty)
    with pytest.raises(textllm.TextLLMConfigError, match="re-ask"):
        run(["x"], route=C.SONNET, max_tokens=1000)
    assert [kw["max_tokens"] for kw in fakes["openrouter"].kwargs] == [1000]
    assert [r["status"] for r in journal(C.SONNET)] == ["fatal"]
    assert emx_spend.spent("judge", exp="I9") > 0  # the first call's actual cost is recorded


@pytest.mark.parametrize(
    "kw,match",
    [
        ({"route": "gpt-4o"}, "unknown route"),
        ({"route": C.LUNA, "reasoning": "high"}, "reasoning"),
        ({"route": C.LUNA, "concurrency": 0}, "concurrency"),
    ],
)
def test_config_errors_before_any_call(env, kw, match):
    with pytest.raises(textllm.TextLLMConfigError, match=match):
        run(["x"], **kw)
    assert env.made == []


@pytest.mark.parametrize(
    "route,var,value",
    [
        (C.LUNA, "OPENAI_API_KEY", None),
        (C.SONNET, "OPENROUTER_API_KEY", None),
        (C.GEMINI, "OPENROUTER_API_KEY", "sk-wrong"),
    ],
)
def test_missing_key_raises_before_any_call(env, monkeypatch, route, var, value):
    if value is None:
        monkeypatch.delenv(var)
    else:
        monkeypatch.setenv(var, value)
    with pytest.raises(textllm.TextLLMConfigError, match=var):
        run(["x"], route=route)
    assert env.made == []


def test_empty_batch_makes_no_call(env):
    got, sp = run([], route=C.LUNA)
    assert got == {} and sp.calls == 0 and env.made == []
    assert emx_spend.read_entries() == []


# ---------------------------------------------------------------- the spend guard


def test_guard_refuses_batch_over_low_cap(env, monkeypatch):
    monkeypatch.setitem(emx_spend.CAPS, "I9", emx_spend.Caps(0.05, 0.0, None, 0.0))
    env.install(echo)
    # 10 Sonnet prompts at 16k max tokens project to ~ $1.6 > $0.05
    with pytest.raises(textllm.TextLLMConfigError, match="spend guard"):
        run(["x" * 100] * 10, route=C.SONNET, max_tokens=16_000)
    assert env.made == [] and emx_spend.read_entries() == []
    # a tiny luna batch fits, and its actual spend lands in the ledger
    got, sp = run(["x"], route=C.LUNA, max_tokens=10)
    assert got == {0: "reply to x"}
    assert emx_spend.spent("judge", exp="I9") == pytest.approx(sp.usd)


def test_guard_counts_earlier_spend(env, monkeypatch):
    monkeypatch.setitem(emx_spend.CAPS, "I9", emx_spend.Caps(1.0, 0.0, None, 0.0))
    emx_spend.record("judge", C.LUNA, 1, 0.9999, exp="I9")
    env.install(echo)
    with pytest.raises(textllm.TextLLMConfigError):
        run(["x" * 3000], route=C.LUNA, max_tokens=1000)
    assert env.made == []


def test_projection_formula():
    usd = textllm.project_usd(["a" * 300, "b" * 300], route=C.LUNA, system=None, max_tokens=1000)
    tin = int(2 * (300 / textllm.CHARS_PER_TOKEN_EST + textllm.OVERHEAD_TOKENS)) + 1
    assert usd == pytest.approx((tin * 0.10 + 2000 * 0.50) / 1e6)


# ---------------------------------------------------------------- the adapter


def _sig(fn) -> list[tuple]:
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


def test_adapter_has_stream_text_signature():
    fn = textllm.as_wsbench_stream_text(C.LUNA, effort_by_thinking={False: "none", True: "low"})
    assert _sig(fn) == _sig(llm.stream_text)


def test_adapter_forwards_every_argument(monkeypatch):
    seen: list[dict] = []

    def spy(prompts, **kw):
        seen.append({"prompts": prompts, **kw})
        for i, p in enumerate(prompts):
            kw["on_result"](i, p.upper())
        return kw["spend"]

    monkeypatch.setattr(textllm, "complete", spy)
    fn = textllm.as_wsbench_stream_text(
        C.DEEPSEEK, effort_by_thinking={False: "none", True: "low"}, purpose="t"
    )
    sp = Spend()
    out: dict[int, str | None] = {}
    ret = fn(
        ["a", "b"],
        model="i9:deepseek-v4-flash:low",
        on_result=out.__setitem__,
        thinking=True,
        max_tokens=123,
        concurrency=5,
        rpm=7.0,
        timeout=11.0,
        spend=sp,
    )
    assert ret is sp and out == {0: "A", 1: "B"}
    kw = seen[0]
    assert kw["route"] == C.DEEPSEEK and kw["reasoning"] == "low" and kw["max_tokens"] == 123
    assert (kw["concurrency"], kw["rpm"], kw["timeout"], kw["spend"]) == (5, 7.0, 11.0, sp)
    assert kw["purpose"] == "t" and "model" not in kw
    fn(["c"], model="ignored", on_result=out.__setitem__, thinking=False, max_tokens=400)
    kw = seen[1]
    assert kw["reasoning"] == "none" and kw["spend"] is None
    assert (kw["concurrency"], kw["rpm"], kw["timeout"]) == (64, 240.0, 600.0)


def test_adapter_end_to_end_on_fake(env):
    fakes = env.install(echo)
    fn = textllm.as_wsbench_stream_text(C.LUNA, effort_by_thinking={False: "none", True: "low"})
    out: dict[int, str | None] = {}
    sp = fn(["q"], model="i9:luna:low", on_result=out.__setitem__, thinking=True, max_tokens=9)
    assert out == {0: "reply to q"} and sp.calls == 1
    assert fakes["openai"].kwargs[0]["reasoning_effort"] == "low"
    assert fakes["openai"].kwargs[0]["model"] == C.LUNA  # never the label


@pytest.mark.parametrize(
    "efforts", [{False: "none"}, {False: "none", True: "high"}, {0: "none", 1: "low", 2: "x"}]
)
def test_adapter_rejects_bad_efforts(efforts):
    with pytest.raises(textllm.TextLLMConfigError):
        textllm.as_wsbench_stream_text(C.LUNA, effort_by_thinking=efforts)


def test_adapter_rejects_unknown_route():
    with pytest.raises(textllm.TextLLMConfigError):
        textllm.as_wsbench_stream_text("nope", effort_by_thinking={False: "none", True: "low"})
