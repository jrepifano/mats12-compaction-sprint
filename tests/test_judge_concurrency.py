"""Judge concurrency (throughput, 2026-10-10): the opt-in helper and its env override, more calls
in flight with the same results, and 429 rate-limit replies retried rather than failing rows.
Offline (fake client)."""

import asyncio
import importlib.util
from pathlib import Path

import httpx
import openai
import pandas as pd
import pytest
from test_judge_fakes import FakeClient
from test_judge_run import make_gen

from emx import judge_run as JR

NO_WAIT = __import__("tenacity").wait_none()
ANSWERS = [f"SCORE={(7 * i) % 100}" for i in range(48)]


def test_judge_concurrency_env(monkeypatch):
    monkeypatch.delenv(JR.CONCURRENCY_ENV, raising=False)
    assert JR.DEFAULT_MAX_CONCURRENCY == 8 and JR.judge_concurrency() == 8
    assert JR.judge_concurrency(32) == 32
    monkeypatch.setenv(JR.CONCURRENCY_ENV, "48")
    assert JR.judge_concurrency(32) == 48
    for bad in ("0", "-3", "many"):
        monkeypatch.setenv(JR.CONCURRENCY_ENV, bad)
        with pytest.raises(ValueError, match=JR.CONCURRENCY_ENV):
            JR.judge_concurrency(32)


def test_library_defaults_unchanged():
    import inspect

    for fn in (JR.judge_column, JR._run_judging_unlocked):
        assert inspect.signature(fn).parameters["max_concurrency"].default == 8


class SlowClient(FakeClient):
    """FakeClient that yields to the loop and records the peak number of calls in flight."""

    def __init__(self, logprobs=True, rate_limited=0):
        super().__init__(logprobs)
        self.inflight = self.peak = 0
        self.rate_limited = rate_limited  # the first N calls get an HTTP 429

    async def create(self, **kw):
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0.002)
            if self.rate_limited > 0:
                self.rate_limited -= 1
                self.calls.append(kw)
                req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
                raise openai.RateLimitError(
                    "Rate limit reached", response=httpx.Response(429, request=req), body=None)
            return await super().create(**kw)
        finally:
            self.inflight -= 1


def _judge(tmp_path, sub, concurrency, rate_limited=0):
    gen = make_gen(tmp_path / sub, answers=ANSWERS)
    judged = JR.default_judged_path(gen)
    client = SlowClient(rate_limited=rate_limited)
    res = JR.run_judging(gen, judged, ["aligned"], ["luna"], {"luna": client},
                         ledger=tmp_path / sub / "costs.jsonl", wait=NO_WAIT, max_retries=3,
                         max_concurrency=concurrency, running_pod_usd=0.0)
    return pd.read_csv(judged), res["aligned__luna"], client


def test_more_in_flight_same_results(tmp_path):
    lo, u_lo, c_lo = _judge(tmp_path, "c8", 8)
    hi, u_hi, c_hi = _judge(tmp_path, "c32", 32)
    assert c_lo.peak <= 8 and 8 < c_hi.peak <= 32
    pd.testing.assert_frame_equal(lo, hi)  # same rows, scores and statuses, in order
    assert u_lo["calls"] == u_hi["calls"] == len(ANSWERS)
    assert u_lo["usd"] == pytest.approx(u_hi["usd"])  # the same calls cost the same


def test_rate_limit_429_is_retried_not_failed(tmp_path):
    out, usage, client = _judge(tmp_path, "rl", 32, rate_limited=20)
    assert set(out["aligned__luna__status"]) == {"scored"}  # no row failed
    assert usage["failed_attempts"] == 20 and usage["calls"] == len(ANSWERS)
    assert len(client.calls) == len(ANSWERS) + 20
    # an unbilled 429 carries no usage: charged at its reservation (conservative, never under)
    assert usage["usd_unmetered"] > 0 and usage["usd"] >= usage["usd_metered"]


def _cli():
    spec = importlib.util.spec_from_file_location(
        "e13_judge_cli", Path(__file__).resolve().parents[1] / "scripts" / "e13_judge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("env,argv,want", [
    (None, [], 32), ("12", [], 12), ("12", ["--concurrency", "4"], 4)])
def test_e13_cli_concurrency(monkeypatch, tmp_path, env, argv, want):
    mod = _cli()
    seen = {}

    def fake_judge_gen(g, **kw):
        seen["max_concurrency"] = kw["max_concurrency"]
        return {"usage": {}}

    if env is None:
        monkeypatch.delenv(JR.CONCURRENCY_ENV, raising=False)
    else:
        monkeypatch.setenv(JR.CONCURRENCY_ENV, env)
    monkeypatch.setattr(mod.J, "project", lambda *a: {})
    monkeypatch.setattr(mod.J, "judge_gen", fake_judge_gen)
    monkeypatch.setattr(mod.J, "reset_for_retry", lambda p: 0)
    monkeypatch.setattr(mod.J, "completeness", lambda *a: {"cells": []})
    rc = mod.main([str(tmp_path / "x.csv"), "--running-pod-usd", "0", "--ledger",
                   str(tmp_path / "costs.jsonl"), *argv])
    assert rc == 0 and seen["max_concurrency"] == want


def test_e13_cli_refuses_bad_concurrency(monkeypatch, tmp_path):
    mod = _cli()
    monkeypatch.setenv(JR.CONCURRENCY_ENV, "0")
    assert mod.main([str(tmp_path / "x.csv"), "--dry-run"]) == 2
