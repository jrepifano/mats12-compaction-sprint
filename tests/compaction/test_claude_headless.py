"""Tests for src/compaction/claude_headless.py (S1 unit U2a), against a fake `claude`.

The fake (tests/compaction/fixtures/fake_claude/fake_claude.py) picks its behaviour from a
`@@mode=<name>@@` marker in the user text. No test runs the real CLI or touches the real ledger;
the managed-settings paths point into tmp.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from compaction import claude_headless as ch
from compaction.s1_types import Req

FAKE = Path(__file__).parent / "fixtures" / "fake_claude" / "fake_claude.py"
SRC = Path(__file__).resolve().parents[2] / "src"
SMOKE = Path(__file__).resolve().parents[2] / "data" / "raw" / "sprint" / "s1" / "smoke"
SCHEMA = {
    "type": "object",
    "properties": {"colors": {"type": "array", "items": {"type": "string"}}},
    "required": ["colors"],
    "additionalProperties": False,
}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    exe = tmp_path / "claude"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    exe.chmod(0o755)
    managed = tmp_path / "managed"
    monkeypatch.setattr(ch, "MANAGED_SETTINGS_FILES", (managed / "managed-settings.json",))
    monkeypatch.setattr(ch, "MANAGED_SETTINGS_DIRS", (managed / "managed-settings.d",))
    monkeypatch.setattr(ch, "MANAGED_PLIST_PATHS", (managed / "com.anthropic.claudecode.plist",))
    monkeypatch.setattr(ch, "MANAGED_DEFAULTS_CMD", None)
    monkeypatch.setenv("FAKE_STATE_DIR", str(state))
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "costs.jsonl"))
    for k in ch.STRIP_ENV + ch.GEN_ENV:
        monkeypatch.setenv(k, "SECRET-" + k)
    for k in ("FAKE_AUTH_JSON", "FAKE_CLAUDE_MODE", "FAKE_AUTH_RC"):
        monkeypatch.delenv(k, raising=False)

    class F:
        bin = str(exe)
        dir = tmp_path
        managed_dir = managed

        @staticmethod
        def all_calls() -> list[dict]:
            p = state / "calls.jsonl"
            return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

        @classmethod
        def calls(cls) -> list[dict]:  # research calls (not the canary)
            return [c for c in cls.all_calls() if c["stdin_tail"] != ch.CANARY_USER]

        @classmethod
        def canaries(cls) -> list[dict]:
            return [c for c in cls.all_calls() if c["stdin_tail"] == ch.CANARY_USER]

        @staticmethod
        def n_auth() -> int:
            p = state / "auth.log"
            return len(p.read_text().splitlines()) if p.exists() else 0

        @staticmethod
        def pid(name: str) -> int:
            return int((state / name).read_text())

        @staticmethod
        def wait_for(name: str, wait_s: float = 10.0) -> Path:
            p = state / name
            deadline = time.monotonic() + wait_s
            while not p.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert p.exists(), name
            return p

    return F


def backend(fake, recorded=None, **kw):
    recorded = [] if recorded is None else recorded
    kw.setdefault("cache_dir", fake.dir / "cache")
    kw.setdefault("usage_path", fake.dir / "usage" / "max_usage.jsonl")
    return ch.MaxBackend(
        claude_bin=fake.bin, recorder=lambda *a, **k: recorded.append((a, k)), **kw
    )


def req(key="k1", mode="ok", schema=SCHEMA, system="You are a test judge.", user="", mot=4096):
    return Req(
        key=key,
        system=system,
        user=f"@@mode={mode}@@ list two colors {user}",
        schema=schema,
        max_output_tokens=mot,
        rid="r1",
        source="swe-chat-v1",
        private=False,
    )


def usage_rows(b) -> list[dict]:
    return [json.loads(x) for x in b.usage_path.read_text().splitlines()]


def _dead(pid: int, wait_s: float = 5.0) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- env, auth, policy


def test_clean_env_strips_and_sets():
    base = {k: "x" for k in ch.STRIP_ENV + ch.GEN_ENV}
    base |= {"PATH": "/bin"}
    env = ch.clean_env(1234, base=base)
    assert not set(ch.STRIP_ENV) & set(env)
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env
    # the .env setup token is for native-harness containers; host calls use the keychain
    # Max login, which check_auth requires (24 Oct)
    assert env["PATH"] == "/bin" and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["DISABLE_COMPACT"] == "1" and env["DISABLE_AUTOUPDATER"] == "1"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "1234"
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in ch.clean_env(base=base)
    assert base["ANTHROPIC_API_KEY"] == "x"  # input untouched


def test_check_auth_accepts_max(fake):
    assert ch.check_auth(fake.bin)["subscriptionType"] == "max"
    assert not ch._LIVE


@pytest.mark.parametrize(
    ("auth", "rc"),
    [
        ({"authMethod": "api_key", "subscriptionType": "max", "loggedIn": True}, "0"),
        ({"authMethod": "claude.ai", "subscriptionType": "pro", "loggedIn": True}, "0"),
        ({"authMethod": "claude.ai", "subscriptionType": "max", "loggedIn": False}, "0"),
        ({"authMethod": "claude.ai", "subscriptionType": "max", "loggedIn": True}, "1"),
    ],
)
def test_auth_refusal_sends_nothing(fake, monkeypatch, auth, rc):
    monkeypatch.setenv("FAKE_AUTH_JSON", json.dumps(auth))
    monkeypatch.setenv("FAKE_AUTH_RC", rc)
    with pytest.raises(ch.MaxAuthError):
        ch.check_auth(fake.bin)
    rec = []
    with pytest.raises(ch.MaxAuthError):
        backend(fake, rec).run([req()], purpose="t")
    assert fake.calls() == [] and rec == []


def test_auth_garbage_output_refused(fake, monkeypatch):
    monkeypatch.setenv("FAKE_AUTH_JSON", "not json")
    with pytest.raises(ch.MaxAuthError):
        ch.check_auth(fake.bin)


def test_auth_is_cached_for_ten_minutes(fake):
    t = [1_000_000.0]
    b = backend(fake, clock=lambda: t[0])
    b.run([req("a")], purpose="t")
    b.run([req("b")], purpose="t")
    assert fake.n_auth() == 1
    t[0] += ch.AUTH_TTL_S + 1
    b.run([req("c")], purpose="t")
    assert fake.n_auth() == 2


@pytest.mark.parametrize(
    "settings",
    [
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}},
        {"mcpServers": {"x": {"command": "x"}}},
        {"env": {"ANTHROPIC_BASE_URL": "https://proxy"}},
        {"env": {"CLAUDE_CODE_EFFORT_LEVEL": "max"}},
        {"apiKeyHelper": "/bin/echo key"},
    ],
)
def test_managed_settings_refused(fake, settings):
    fake.managed_dir.mkdir()
    (fake.managed_dir / "managed-settings.json").write_text(json.dumps(settings))
    rec = []
    with pytest.raises(ch.ManagedSettingsError):
        backend(fake, rec).run([req()], purpose="t")
    assert fake.calls() == [] and rec == []


def test_managed_settings_dropin_dir_and_hash(fake):
    d = fake.managed_dir / "managed-settings.d"
    d.mkdir(parents=True)
    (d / "10-ok.json").write_text(json.dumps({"permissions": {"deny": ["WebFetch"]}}))
    assert ch.check_managed_settings() != "none"
    rec = []
    r = backend(fake, rec).run([req()], purpose="t")["k1"]
    assert r.error is None and len(r.meta["managed_settings"]) == 64
    assert "managed_settings=" + r.meta["managed_settings"] in rec[0][1]["note"]
    (d / "20-bad.json").write_text(json.dumps({"enabledPlugins": {"p@m": True}}))
    with pytest.raises(ch.ManagedSettingsError):
        ch.check_managed_settings()


def test_managed_preferences_plist_refused(fake):
    import plistlib

    fake.managed_dir.mkdir()
    p = fake.managed_dir / "com.anthropic.claudecode.plist"
    p.write_bytes(plistlib.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command"}]}]}}))
    with pytest.raises(ch.ManagedSettingsError, match="plist: defines hooks"):
        backend(fake).run([req()], purpose="t")
    assert fake.all_calls() == []


def test_managed_defaults_domain_refused_and_recorded(fake, monkeypatch, tmp_path):
    import plistlib

    out = tmp_path / "defaults.plist"
    out.write_bytes(plistlib.dumps({"env": {"ANTHROPIC_API_KEY": "sk-x"}}))
    monkeypatch.setattr(ch, "MANAGED_DEFAULTS_CMD", ("cat", str(out)))
    with pytest.raises(ch.ManagedSettingsError, match="env sets"):
        backend(fake).run([req()], purpose="t")
    out.write_bytes(plistlib.dumps({}))  # what `defaults export` prints for a missing domain
    r = backend(fake).run([req()], purpose="t")["k1"]
    assert r.error is None and r.meta["managed_settings"] == "none"
    src = r.meta["managed_sources"]
    assert src["defaults:com.anthropic.claudecode"] == "empty"
    assert set(src.values()) <= {"absent", "empty"} and len(src) == 4


def test_no_managed_settings_is_none(fake):
    assert ch.check_managed_settings() == "none"
    r = backend(fake).run([req()], purpose="t")["k1"]
    assert r.meta["managed_settings"] == "none"


def test_env_is_stripped_in_the_child(fake):
    r = backend(fake).run([req(mode="env", schema=None, mot=777)], purpose="t")["k1"]
    assert r.error is None, r
    out = json.loads(r.text)
    assert not set(ch.STRIP_ENV) & set(out["keys"])
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in out["keys"]
    assert out["env"] == {
        "DISABLE_COMPACT": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "777",
    }


# ---------------------------------------------------------------- one call


def test_schema_success_command_tokens_and_cwd(fake):
    rec = []
    b = backend(fake, rec)
    r = b.run([req()], purpose="t")["k1"]
    assert r.error is None and r.data == {"colors": ["red", "blue"]}
    assert r.text == '{"colors": ["red", "blue"]}'
    assert (r.input_tokens, r.cache_write_tokens, r.cache_read_tokens) == (152, 100, 50)
    assert r.output_tokens == 30 and r.usd == 0.0 and r.model == "claude-haiku-5-5"
    assert r.meta["thinking_tokens"] == 7 and r.meta["notional_usd"] == 0.0007
    assert r.meta["rate_limit"]["status"] == "allowed" and r.meta["api_retries"] == 0
    assert r.meta["usage_known"] is True and ch.verify_route(r.meta["route"]) == "ok"
    (call,) = fake.calls()
    a = call["argv"]
    assert a[0] == "-p" and "--bare" not in a
    for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config", "--verbose"):
        assert flag in a
    assert a[a.index("--tools") + 1] == ""
    assert a[a.index("--output-format") + 1] == "stream-json"
    assert a[a.index("--system-prompt") + 1] == "You are a test judge."
    assert json.loads(a[a.index("--json-schema") + 1]) == SCHEMA
    assert "--effort" not in a
    assert call["cwd_entries"] == [] and not Path(call["cwd"]).exists()  # fresh, then removed
    canary_row, u = usage_rows(b)
    assert canary_row["key"] == ch.CANARY_KEY
    assert u["key"] == "k1" and u["purpose"] == "t" and u["input_tokens"] == 152
    assert u["rate_limit"]["status"] == "allowed" and u["usage_known"] is True
    assert len(rec) == 1 and rec[0][1]["usd"] == 0.0
    assert "route verified" in rec[0][1]["note"]


def test_effort_flag(fake):
    backend(fake, effort="low").run([req()], purpose="t")
    a = fake.calls()[0]["argv"]
    assert a[a.index("--effort") + 1] == "low"


def test_schema_failure_keeps_text(fake):
    r = backend(fake).run([req(mode="bad_schema")], purpose="t")["k1"]
    assert r.error == "schema" and r.data is None
    assert r.text == '{"colours": 3}' and r.meta["schema_errors"]
    assert "schema_retry" not in r.meta and len(fake.calls()) == 1  # present but invalid: no retry


@pytest.mark.parametrize(("mode", "data"), [("text_json", ["a", "b"]), ("assistant_json", ["c"])])
def test_schema_salvaged_from_text(fake, mode, data):
    r = backend(fake).run([req(mode=mode)], purpose="t")["k1"]
    assert r.error is None and r.data == {"colors": data} and r.meta["schema_salvaged"] is True
    assert len(fake.calls()) == 1


def test_schema_retry_once_with_the_tool_line(fake):
    b = backend(fake)
    r = b.run([req(mode="refuse_tool_once")], purpose="t")["k1"]
    assert r.error is None and r.data == {"colors": ["green"]}
    assert r.meta["schema_retry"]["outcome"] == "ok" and r.key == "k1"
    c1, c2 = fake.calls()
    assert not c1["stdin_tail"].endswith(ch.SCHEMA_RETRY_LINE)
    assert c2["stdin_tail"].endswith("\n\n" + ch.SCHEMA_RETRY_LINE)
    assert [u["schema_retry"] for u in usage_rows(b) if u["key"] == "k1"] == [False, True]
    backend(fake).run([req(mode="refuse_tool_once")], purpose="t")  # cached under the original
    assert len(fake.calls()) == 2


def test_free_text_has_no_schema_flag(fake):
    r = backend(fake).run([req(mode="text", schema=None)], purpose="t")["k1"]
    assert r.error is None and r.data is None and r.text == "a plain answer"
    assert "--json-schema" not in fake.calls()[0]["argv"]


def test_model_mismatch(fake):
    r = backend(fake).run([req(mode="mismatch")], purpose="t")["k1"]
    assert r.error == "model_mismatch" and r.data is None and r.model == "claude-sonnet-5-5"


def test_api_retries_counted(fake):
    r = backend(fake).run([req(mode="api_retry")], purpose="t")["k1"]
    assert r.error is None and r.meta["api_retries"] == 2


def test_crash_is_a_cli_error(fake):
    r = backend(fake).run([req(mode="crash")], purpose="t")["k1"]
    assert r.error == "cli_error" and r.meta["returncode"] == 2
    assert "boom" in r.meta["stderr_tail"] and r.meta["usage_known"] is False


# ---------------------------------------------------------------- billing route


@pytest.mark.parametrize("mode", ["apikey", "overage_on"])
def test_paid_route_on_a_research_call_aborts_with_paid_note(fake, mode):
    """After a good canary, any call showing an API key or extra usage not disabled aborts."""
    rec = []
    b = backend(fake, rec, concurrency=1)
    with pytest.raises(ch.BillingRouteError):
        b.run([req(mode=mode), req("k2")], purpose="t")
    assert len(fake.canaries()) == 1 and len(fake.calls()) == 1  # k2 never launched
    _, k = rec[0]
    assert k["units"] == 2 and k["usd"] == 0.0007 and "PAID_ROUTE_DETECTED" in k["note"]
    assert "Max plan, $0" not in k["note"]
    u = [x for x in usage_rows(b) if x["key"] == "k1"]
    assert u[0]["error"] == "billing_route" and u[0]["input_tokens"] == 152  # evidence kept


def test_missing_route_evidence_is_not_accepted(fake):
    b = backend(fake)
    r = b.run([req(mode="no_rl")], purpose="t")["k1"]
    assert r.error == "route_unverified" and r.data is None
    assert r.meta["unverified_data"] == {"colors": ["red", "blue"]}
    backend(fake).run([req(mode="no_rl")], purpose="t")
    assert len(fake.calls()) == 2  # not cached: resent


@pytest.mark.parametrize("mode", ["overage_on", "apikey", "no_rl"])
def test_canary_must_prove_the_route(fake, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)  # what the canary (no marker) gets
    rec = []
    with pytest.raises(ch.BillingRouteError):
        backend(fake, rec, concurrency=4).run([req(f"k{i}") for i in range(6)], purpose="t")
    assert len(fake.canaries()) == (ch.MAX_CANARIES if mode == "no_rl" else 1)
    assert fake.calls() == []  # no research request was sent
    assert rec[0][1]["units"] == len(fake.canaries())


def test_canary_is_tiny_and_runs_alone_first(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slowok")
    out = backend(fake, concurrency=4).run([req(f"b{i}") for i in range(3)], purpose="t")
    assert all(r.error is None for r in out.values()) and ch.CANARY_KEY not in out
    (c,) = fake.canaries()
    a = c["argv"]
    assert a[a.index("--system-prompt") + 1] == ch.CANARY_SYSTEM and "--json-schema" not in a
    assert min(x["start"] for x in fake.calls()) - c["start"] >= 0.5


def test_streamed_paid_route_stops_launches_at_once(fake):
    """An API-key init mid-batch kills the in-flight session (which would sleep 300 s) and the
    others, and no further call is launched."""
    reqs = [req("paid", mode="apikey_sleep")]
    reqs += [req(f"s{i}", mode="slowok") for i in range(6)]
    rec = []
    t0 = time.monotonic()
    with pytest.raises(ch.BillingRouteError):
        backend(fake, rec, concurrency=2, timeout_s=120).run(reqs, purpose="t")
    assert time.monotonic() - t0 < 20
    assert len(fake.calls()) <= 3  # the first pair (+ at most one racing launch)
    assert "PAID_ROUTE_DETECTED" in rec[0][1]["note"]
    assert not ch._LIVE


def test_abort_kills_the_other_workers(fake):
    """An abort (here the API-key check) leaves no session of the batch running."""
    b = backend(fake, concurrency=2, timeout_s=120)
    t0 = time.monotonic()
    with pytest.raises(ch.BillingRouteError):
        b.run([req("a", mode="sleep"), req("b", mode="apikey_late")], purpose="t")
    assert time.monotonic() - t0 < 60
    assert _dead(fake.pid("child.pid"))


# ---------------------------------------------------------------- limits, timeouts, size


def _fake_clock():
    t = [time.time()]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s

    return t, sleeps, sleep


@pytest.mark.parametrize("mode", ["limit_once", "limit_result_only"])
def test_limit_waits_then_retries(fake, capfd, mode):
    t, sleeps, sleep = _fake_clock()
    rec = []
    b = backend(fake, rec, sleep=sleep, clock=lambda: t[0])
    r = b.run([req(mode=mode)], purpose="t")["k1"]
    assert r.error is None and r.data == {"colors": ["red", "blue"]}
    assert 120 <= sum(sleeps) <= 130  # the reset (now + 5 s) + 120 s, not the 900 s fallback
    assert len(fake.calls()) == 2 and r.meta["attempt"] == 2
    assert "MAX_LIMIT_WAIT until" in capfd.readouterr().err
    assert rec[0][1]["units"] == 3  # canary + 2 attempts
    assert [u["error"] for u in usage_rows(b) if u["key"] == "k1"] == ["limit", None]


def test_limit_gives_up_after_max_waits(fake):
    t, _, sleep = _fake_clock()
    b = backend(fake, sleep=sleep, clock=lambda: t[0], max_limit_waits=0)
    r = b.run([req(mode="limit_once")], purpose="t")["k1"]
    assert r.error == "limit" and len(fake.calls()) == 1


@pytest.mark.parametrize("limited_polls", [0, 2])
def test_weekly_limit_polls_and_resumes_early(fake, capfd, monkeypatch, limited_polls):
    """A 7-day limit is waited out in LIMIT_POLL_S slices with a canary each slice; the call
    resumes on the first allowed canary (e.g. after a manual reset), not on Sunday, and the
    polls don't count as limit waits."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "canary_poll")
    monkeypatch.setenv("FAKE_POLL_LIMITED", str(limited_polls))
    t, sleeps, sleep = _fake_clock()
    # max_limit_waits=1: the one research hit counts; limited polls would push it past 1
    b = backend(fake, sleep=sleep, clock=lambda: t[0], max_limit_waits=1)
    r = b.run([req(mode="limit_weekly")], purpose="t")["k1"]
    assert r.error is None and r.data == {"colors": ["red", "blue"]}
    assert sleeps == [ch.LIMIT_POLL_S] * (limited_polls + 1)
    assert len(fake.calls()) == 2 and len(fake.canaries()) == 1 + limited_polls + 1
    err = capfd.readouterr().err
    assert re.search(r"^S1_LIMIT seven_day util=1\.0 resets=\d{4}-\d\d-\d\dT", err, re.MULTILINE)
    assert "S1_LIMIT_CLEARED" in err
    polls = [u for u in usage_rows(b) if u["key"] == ch.CANARY_KEY and u["attempt"] == 0]
    assert [u["error"] for u in polls] == ["limit"] * limited_polls + [None]


def test_poll_does_not_run_past_the_reset(fake, capfd):
    """A short limit (reset within one slice) is slept out with no poll, as before."""
    t, sleeps, sleep = _fake_clock()
    b = backend(fake, sleep=sleep, clock=lambda: t[0])
    b.run([req(mode="limit_once")], purpose="t")
    assert len(sleeps) == 1 and sleeps[0] < ch.LIMIT_POLL_S
    assert len(fake.canaries()) == 1


def test_stale_poll_never_overwrites_a_newer_research_pause(fake):
    """A research call that sets a pause while the canary runs wins over the canary's reading,
    whether the canary was allowed (would clear) or limited (would replace)."""
    t, _, sleep = _fake_clock()
    b = backend(fake, sleep=sleep, clock=lambda: t[0])
    stats = ch._Stats()
    for canary in (None, {"until": t[0] + 60, "window": "five_hour", "status": "rejected"}):
        b._pause_until = t[0] + 1000

        def attempt(*a, canary=canary, **k):
            b._set_pause({"until": t[0] + 5000, "window": "seven_day", "status": "rejected"})
            return ch.Resp(ch.CANARY_KEY, None, None, None if canary is None else "limit"), canary

        b._attempt = attempt
        assert b._poll("t", stats) is False
        assert b._pause_until == t[0] + 5000


def test_persistent_poll_errors_abort_instead_of_idling(fake, capfd):
    t, _, sleep = _fake_clock()
    b = backend(fake, sleep=sleep, clock=lambda: t[0])
    b._pause_until = t[0] + 7 * 86400
    b._attempt = lambda *a, **k: (ch.Resp(ch.CANARY_KEY, None, None, "cli_error"), None)
    with pytest.raises(RuntimeError, match="limit polls in a row failed"):
        b._wait_pause("t", ch._Stats())
    assert capfd.readouterr().err.count("S1_LIMIT_POLL_ERROR cli_error") == ch.MAX_POLL_ERRORS


def test_limit_uses_latest_blocking_reset():
    events = [
        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": 100, "rateLimitType": "five_hour"}},
        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": 900, "rateLimitType": "seven_day"}},
        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "resetsAt": 5000, "rateLimitType": "five_hour"}},
    ]  # fmt: skip
    lim = ch._limit_from(events, {"subtype": "error", "is_error": True, "result": "x|500"}, 0.0)
    assert lim["until"] == 900 + ch.LIMIT_MARGIN_S and lim["window"] == "seven_day"
    res = {
        "subtype": "error",
        "is_error": True,
        "result": "Claude AI usage limit reached|1792972800",
    }
    lim = ch._limit_from([], res, 0.0)
    assert lim["until"] == 1792972800 + ch.LIMIT_MARGIN_S and lim["window"] == "result_message"


@pytest.mark.parametrize("mode", ["sleep", "sleep_stubborn"])
def test_timeout_kills_the_group(fake, mode):
    b = backend(fake, timeout_s=1.5, kill_grace_s=0.5)
    t0 = time.monotonic()
    r = b.run([req(mode=mode)], purpose="t")["k1"]
    assert r.error == "timeout" and time.monotonic() - t0 < 20
    assert r.meta["killed"] == ("SIGKILL" if mode == "sleep_stubborn" else "SIGTERM")
    assert _dead(fake.pid("child.pid")), "the fake's child outlived the timeout"
    assert r.meta["usage_known"] is False
    assert not ch._LIVE


def test_timeout_keeps_partial_usage(fake):
    b = backend(fake, timeout_s=1.5, kill_grace_s=0.5)
    r = b.run([req(mode="partial_sleep")], purpose="t")["k1"]
    assert r.error == "timeout" and r.meta["usage_known"] is True
    assert (r.input_tokens, r.cache_write_tokens, r.output_tokens) == (43, 40, 9)
    assert r.meta["rate_limit"]["status"] == "allowed"
    (u,) = [x for x in usage_rows(b) if x["key"] == "k1"]
    assert u["error"] == "timeout" and u["usage_known"] is True and u["input_tokens"] == 43


def test_run_session_direct(fake, tmp_path):
    ev = ch.run_session(tmp_path, ["-p"], None, 30, claude_bin=fake.bin)
    assert ev[-1]["type"] == "_process" and ev[-1]["returncode"] == 0
    assert any(e.get("type") == "result" for e in ev)
    with pytest.raises(ch.SessionTimeout) as ei:
        ch.run_session(tmp_path, ["-p"], "@@mode=sleep@@", 1.0, claude_bin=fake.bin,
                       kill_grace_s=0.5)  # fmt: skip
    assert ei.value.events[-1]["timed_out"] is True
    assert _dead(fake.pid("child.pid"))
    seen = []
    ev = ch.run_session(tmp_path, ["-p"], "@@mode=apikey_sleep@@", 60, claude_bin=fake.bin,
                        on_event=lambda e: seen.append(e) or e.get("subtype") == "init")  # fmt: skip
    assert ev[-1]["stopped"] is True and ev[-1]["killed"] == "SIGTERM" and seen


DRIVER = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    sys.path.insert(0, {src!r})
    from compaction import claude_headless as ch
    from compaction.s1_types import Req
    ch.MANAGED_SETTINGS_FILES = ch.MANAGED_SETTINGS_DIRS = ch.MANAGED_PLIST_PATHS = ()
    ch.MANAGED_DEFAULTS_CMD = None
    tmp = Path({tmp!r})
    def rec(*a, **k):
        (tmp / "rec.json").write_text(json.dumps(k))
    b = ch.MaxBackend(cache_dir=tmp / "dc", usage_path=tmp / "du.jsonl", claude_bin={bin!r},
                      concurrency=2, timeout_s=120, recorder=rec)
    mk = lambda k, m: Req(k, "sys", f"@@mode={{m}}@@", None, 100, "r", "swe-chat-v1", False)
    b.run([mk("a", "ok"), mk("b", "sleep")], purpose="drv")
    """
)


def test_sigterm_to_the_driver_kills_fake_and_grandchild(fake, tmp_path):
    code = DRIVER.format(src=str(SRC), tmp=str(tmp_path), bin=fake.bin)
    drv = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        fake.wait_for("child.pid", 30)
        time.sleep(0.3)
        fake_pid, child_pid = fake.pid("fake.pid"), fake.pid("child.pid")
        drv.send_signal(signal.SIGTERM)
        rc = drv.wait(timeout=30)
    finally:
        if drv.poll() is None:
            os.killpg(drv.pid, signal.SIGKILL)
    assert rc == 128 + signal.SIGTERM
    assert _dead(fake_pid) and _dead(child_pid)
    rows = [json.loads(x) for x in (tmp_path / "du.jsonl").read_text().splitlines()]
    assert {r["key"] for r in rows} == {ch.CANARY_KEY, "a", "b"}  # the killed call is accounted
    assert json.loads((tmp_path / "rec.json").read_text())["units"] == 3


def _driver(code: str, timeout: float = 30.0) -> int:
    drv = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        return drv.wait(timeout=timeout)
    finally:
        if drv.poll() is None:
            os.killpg(drv.pid, signal.SIGKILL)
            drv.wait()


def test_sigterm_while_registry_lock_held_does_not_deadlock():
    code = textwrap.dedent(
        f"""
        import os, signal, sys, time
        sys.path.insert(0, {str(SRC)!r})
        from compaction import claude_headless as ch
        with ch.job_guard():
            with ch._LIVE_LOCK:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)
        """
    )
    assert _driver(code, timeout=20) == 128 + signal.SIGTERM


def test_sigterm_while_stop_event_lock_held_does_not_deadlock():
    """The handler must not call Event.set(): the main thread may hold the event's lock."""
    code = textwrap.dedent(
        f"""
        import os, signal, sys, threading, time
        sys.path.insert(0, {str(SRC)!r})
        from compaction import claude_headless as ch
        stop = threading.Event()
        try:
            with ch.job_guard(stop):
                with stop._cond:
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(5)
        except SystemExit:
            deadline = time.monotonic() + 5
            while not stop.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            sys.exit(143 if stop.is_set() else 3)  # the watcher set it after the lock was freed
        """
    )
    assert _driver(code, timeout=20) == 128 + signal.SIGTERM


def test_signal_right_after_popen_still_kills_the_group(fake, tmp_path):
    code = textwrap.dedent(
        f"""
        import os, signal, subprocess, sys, time
        from pathlib import Path
        sys.path.insert(0, {str(SRC)!r})
        from compaction import claude_headless as ch
        real = subprocess.Popen
        class P(real):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                Path({str(tmp_path / "popen.pid")!r}).write_text(str(self.pid))
                os.kill(os.getpid(), signal.SIGTERM)  # before registration
                time.sleep(0.2)  # the handler has run (and deferred) by now
        ch.subprocess.Popen = P
        with ch.job_guard():
            ch.run_session({str(tmp_path)!r}, ["-p"], "@@mode=sleep@@", 120,
                           claude_bin={fake.bin!r})
        """
    )
    assert _driver(code) == 128 + signal.SIGTERM
    assert _dead(int((tmp_path / "popen.pid").read_text()))


def test_interrupt_after_usage_is_accounted(fake):
    rec = []
    b = backend(fake, rec, timeout_s=60)

    def watch(ev):
        if ev.get("type") == "assistant":
            raise KeyboardInterrupt
        return False

    b._watch = watch
    with pytest.raises(KeyboardInterrupt):
        b.run([req(mode="partial_sleep")], purpose="t")
    (u,) = [x for x in usage_rows(b) if x["key"] == "k1"]
    assert u["error"] == "interrupted" and u["usage_known"] is True and u["input_tokens"] == 43
    assert rec[0][1]["units"] == 2 and "input=" in rec[0][1]["note"]
    assert _dead(fake.pid("fake.pid"))


def test_run_session_interrupt_carries_events(fake, tmp_path):
    def boom(ev):
        if ev.get("type") == "assistant":
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt) as ei:
        ch.run_session(tmp_path, ["-p"], "@@mode=partial_sleep@@", 60, claude_bin=fake.bin,
                       on_event=boom)  # fmt: skip
    assert any(e.get("type") == "assistant" for e in ei.value.claude_events)
    assert ei.value.claude_process["interrupted"] == "KeyboardInterrupt"
    assert _dead(fake.pid("fake.pid"))


def test_result_without_usage_is_unknown():
    events = [
        {"type": "system", "subtype": "init", "apiKeySource": "none"},
        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "overageStatus": "rejected", "isUsingOverage": False}},
        {"type": "result", "subtype": "success", "result": "x"},
    ]  # fmt: skip
    r, _ = ch.interpret(events, req(schema=None), "m", 0.0)
    assert r.meta["usage_known"] is False
    events[-1]["usage"] = {"input_tokens": 0, "output_tokens": 0}
    r, _ = ch.interpret(events, req(schema=None), "m", 0.0)
    assert r.meta["usage_known"] is True  # measured zero


def test_job_guard_restores_handlers():
    before = signal.getsignal(signal.SIGTERM)
    with ch.job_guard():
        assert signal.getsignal(signal.SIGTERM) is not before
    assert signal.getsignal(signal.SIGTERM) is before


def test_too_long_short_circuits(fake):
    rec = []
    b = backend(fake, rec, context_tokens=3_000)
    r = b.run([req(user="x" * 10_000)], purpose="t")["k1"]
    assert r.error == "too_long" and r.meta["est_tokens"] > 4_000
    assert fake.calls() == [] and rec == [] and fake.n_auth() == 0


def test_too_long_result_maps(fake):
    r = backend(fake).run([req(mode="too_long")], purpose="t")["k1"]
    assert r.error == "too_long"


def test_config_errors_raise(fake):
    with pytest.raises(ValueError, match="duplicate"):
        backend(fake).run([req("a"), req("a")], purpose="t")
    with pytest.raises(ValueError, match="empty system"):
        backend(fake).run([req(system="  ")], purpose="t")
    with pytest.raises(ValueError, match="purpose"):
        backend(fake).run([req()], purpose="../x")
    assert fake.calls() == []


def test_overlapping_runs_rejected(fake):
    b = backend(fake)
    b._run_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            b.run([req()], purpose="t")
    finally:
        b._run_lock.release()


# ---------------------------------------------------------------- cache and ledger


def test_cache_resume_sends_nothing(fake):
    rec = []
    reqs = [req("a"), req("b", mode="bad_schema"), req("c", mode="text", schema=None)]
    first = backend(fake, rec, concurrency=3).run(reqs, purpose="p1")
    assert len(fake.calls()) == 3 and len(rec) == 1
    second = backend(fake, rec).run([req("a"), req("c", mode="text", schema=None)], purpose="p1")
    assert len(fake.calls()) == 3 and len(rec) == 1  # nothing sent, no ledger row
    assert second["a"].data == first["a"].data and second["a"].meta["cached"] is True
    assert second["c"].text == "a plain answer"
    # the schema failure isn't a cached success: it is resent
    backend(fake, rec).run([req("b", mode="bad_schema")], purpose="p1")
    assert len(fake.calls()) == 4
    # a changed prompt, output cap, effort, model or purpose misses the cache
    backend(fake, rec).run([req("a", system="Other.")], purpose="p1")
    backend(fake, rec).run([req("a", mot=999)], purpose="p1")
    backend(fake, rec, effort="low").run([req("a")], purpose="p1")
    backend(fake, rec, model="claude-other").run([req("a", mode="text", schema=None)],
                                                  purpose="p1")  # fmt: skip
    backend(fake, rec).run([req("a")], purpose="p2")
    assert len(fake.calls()) == 9


def test_cache_repairs_a_torn_tail(fake):
    b = backend(fake)
    b.run([req("a")], purpose="t")
    with open(b.cache_path("t"), "a") as f:
        f.write('{"cache_id": "x", "resp": {"key"')
    backend(fake).run([req("a"), req("b")], purpose="t")  # a cached; b appended after repair
    assert len(fake.calls()) == 2
    lines = b.cache_path("t").read_text().splitlines()
    assert all(json.loads(x) for x in lines) and len(lines) == 2
    backend(fake).run([req("a"), req("b")], purpose="t")  # both resume
    assert len(fake.calls()) == 2


def test_ledger_row_is_zero_dollar(fake, tmp_path):
    from emx import spend

    ledger = tmp_path / "costs.jsonl"
    b = ch.MaxBackend(
        cache_dir=tmp_path / "c", usage_path=tmp_path / "u.jsonl", claude_bin=fake.bin
    )  # default recorder: emx.spend.record, pointed at the tmp ledger by EMX_COSTS_LEDGER
    b.run([req("a"), req("b", mode="text", schema=None)], purpose="t")
    (e,) = spend.read_entries(ledger)
    assert e["exp"] == "SPRINT-S1" and e["kind"] == "judge" and e["usd"] == 0.0
    assert e["resource"] == "claude-max:claude-haiku-5-5" and e["units"] == 3  # with canary
    assert "input=456" in e["note"] and "output=90" in e["note"] and "purpose=t" in e["note"]
    b.run([req("a")], purpose="t")  # all cached: no second row
    assert len(spend.read_entries(ledger)) == 1


# ---------------------------------------------------------------- real smoke outputs


@pytest.mark.parametrize("name", ["tiny.jsonl", "sysprompt.jsonl", "long.jsonl"])
def test_interpret_real_smoke_output(name):
    path = SMOKE / name
    if not path.exists():
        pytest.skip("smoke outputs are gitignored; present only in the designated checkout")
    events = ch.parse_events(path.read_text())
    r, limit = ch.interpret(events, req(schema=None), "claude-haiku-5-5", time.time())
    assert limit is None and r.error is None and r.model == "claude-haiku-5-5"
    assert r.input_tokens > 0 and r.output_tokens > 0
    assert r.meta["rate_limit"]["status"] == "allowed"
    assert ch.verify_route(r.meta["route"]) == "ok"
    if name == "long.jsonl":
        assert r.input_tokens == 808_394  # 2 uncached + 808,392 cache write


def test_interpret_rejected_status_names_the_window():
    reset = 1_792_972_800
    events = [
        {"type": "system", "subtype": "init", "apiKeySource": "none"},
        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": reset, "rateLimitType": "seven_day",
            "overageStatus": "rejected", "isUsingOverage": False}},
        {"type": "result", "subtype": "success", "is_error": True, "result": "limit reached",
         "modelUsage": {}},
    ]  # fmt: skip
    r, limit = ch.interpret(events, req(), "claude-haiku-5-5", 0.0)
    assert r.error == "limit"
    assert limit == {
        "until": reset + 120.0, "window": "seven_day", "status": "rejected", "util": None,
    }  # fmt: skip


def test_interpret_overage_and_missing_init_abort():
    over = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
                                                            "isUsingOverage": True}}  # fmt: skip
    init = {"type": "system", "subtype": "init", "apiKeySource": "none"}
    res = {"type": "result", "subtype": "success", "result": "x", "modelUsage": {}}
    with pytest.raises(ch.BillingRouteError) as ei:
        ch.interpret([init, over, res], req(), "m", 0.0)
    assert ei.value.resp is not None and ei.value.resp.error == "billing_route"
    with pytest.raises(ch.BillingRouteError):
        ch.interpret([res], req(), "m", 0.0)


def test_verify_route_cases():
    base = {"api_key_source": "none", "rate_limit_seen": True, "is_using_overage": False,
            "overage_status": "rejected", "overage_disabled_reason": None}  # fmt: skip
    assert ch.verify_route(base) == "ok"
    alt = base | {"overage_status": None, "overage_disabled_reason": "org_level_disabled"}
    assert ch.verify_route(alt) == "ok"
    assert ch.verify_route(base | {"overage_status": "allowed"}) == "bad"
    assert ch.verify_route(base | {"overage_status": None}) == "bad"
    assert ch.verify_route(base | {"api_key_source": "ANTHROPIC_API_KEY"}) == "bad"
    assert ch.verify_route(base | {"rate_limit_seen": False}) == "unknown"
    assert ch.verify_route(None) == "unknown"


# -- tool-using requests (spec D11's second look)


def test_tool_request_writes_files_and_passes_tools(fake):
    r0 = dataclasses.replace(
        req(), files=(("history.txt", "[m0] USER: hi"),), tools=("Grep", "Read"), max_turns=7
    )
    r = backend(fake).run([r0], purpose="t")["k1"]
    assert r.error is None and r.meta["tool_calls"] == []
    (call,) = fake.calls()
    a = call["argv"]
    assert a[a.index("--tools") + 1] == "Grep,Read"
    assert a[a.index("--max-turns") + 1] == "7"
    assert "--safe-mode" in a
    assert call["cwd_entries"] == ["history.txt"] and not Path(call["cwd"]).exists()


def test_cache_id_unchanged_without_tools_and_keyed_on_files(fake):
    b = backend(fake)
    r0 = req()
    old = ch._sha(
        json.dumps(
            [r0.key, ch._sha(r0.system + "\0" + r0.user), ch._sha(ch._canon(r0.schema)),
             b.model, b.effort, int(r0.max_output_tokens)]
        )
    )  # fmt: skip
    assert b.cache_id(r0) == old  # caches written before tools existed still hit
    t1 = dataclasses.replace(r0, files=(("history.txt", "a"),), tools=("Grep",))
    t2 = dataclasses.replace(t1, files=(("history.txt", "b"),))
    assert len({b.cache_id(r0), b.cache_id(t1), b.cache_id(t2)}) == 3


@pytest.mark.parametrize(
    "change", [{"tools": ("Bash",)}, {"files": (("../x.txt", "a"),)}, {"tools": ("Write",)}]
)
def test_tool_request_refuses_unsafe_tools_and_paths(fake, change):
    with pytest.raises(ValueError, match="not allowed"):
        backend(fake).run([dataclasses.replace(req(), **change)], purpose="t")
    assert fake.all_calls() == []


def test_tool_calls_are_collected_in_order():
    ev = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "0\\.2016"}}]}},
        {"type": "user", "message": {"content": []}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "x"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "h" * 999}}]}},
    ]  # fmt: skip
    calls = ch.tool_calls(ev)
    assert [c["name"] for c in calls] == ["Grep", "Read"]
    assert len(calls[1]["input"]) == ch.TOOL_INPUT_CHARS
