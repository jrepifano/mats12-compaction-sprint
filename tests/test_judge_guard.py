"""The judge spend guard (code review 20260929-E1-code-r1, findings 1 and 2). Offline, mocks only.

1. Running pods' projected cost (from the pod event log) enters both the pre-column check and the
   in-run headroom.
2. Every call reserves its maximum cost before dispatch, so concurrency cannot overshoot the
   headroom; failed calls are charged (their usage if the error carries it, else the
   reservation); abort, error and cancellation charge in-flight calls at their reservation, and
   the ledger entry is written in every case.
"""

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from test_judge_fakes import completion

from emx import judge_run as JR
from emx import spend
from emx.contracts import GEN_COLUMNS
from emx.judge_panel import PRESETS

NO_WAIT = __import__("tenacity").wait_none()
PRICEY = replace(PRESETS["dsk"], usd_per_mtok_in=1000.0, usd_per_mtok_out=1000.0)


class ColumnFree(type(PRESETS["dsk"])):
    """Column projection 0 (the pre-column guard always passes); per-call reservation real."""

    def project_usd(self, prompts):
        return super().project_usd(prompts) if len(prompts) == 1 else 0.0


COLUMN_FREE = ColumnFree(**asdict(PRICEY))


def make_gen(tmp_path, n=8):
    rows = [{
        "condition": "c@chat", "cell": "c", "eval_fmt": "chat", "qset": "general",
        "qid": f"q{i:02d}", "question": f"Question {i:02d}?", "sample_idx": i, "seed": i,
        "prompt_text": "<p>", "response": "SCORE=50", "finish_reason": "stop", "n_new_tokens": 5,
    } for i in range(n)]
    d = tmp_path / "results" / "E1" / "gen"
    d.mkdir(parents=True)
    p = d / "c@chat__general.csv"
    pd.DataFrame(rows, columns=list(GEN_COLUMNS)).to_csv(p, index=False)
    return p


def reservations(gen, preset, rows=None):
    g = pd.read_csv(gen, keep_default_na=False)
    rows = list(range(len(g))) if rows is None else rows
    return [JR.call_reservation_usd(preset, p) for p in JR.filled_prompts(g, rows, "aligned")]


class SlowClient:
    """Answers `50` after `delay` s; raises `fail` (an exception instance or factory) instead
    when set; `boom_on` makes the prompt containing that text raise `boom` after a short delay."""

    def __init__(self, delay=0.01, fail=None, boom_on=None, boom=None):
        self.delay, self.fail, self.boom_on, self.boom = delay, fail, boom_on, boom
        self.calls: list[dict] = []
        self.chat = type("C", (), {})()
        self.chat.completions = type("C", (), {})()
        self.chat.completions.create = self.create

    async def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"]
        if self.boom_on and self.boom_on in text:
            await asyncio.sleep(0.01)
            raise self.boom
        await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail() if callable(self.fail) else self.fail
        return completion("50")


def write_pod_log(path, running_hours, rate):
    now = datetime.now(UTC)
    ev = [
        {"utc": (now - timedelta(hours=running_hours)).isoformat(), "event": "created",
         "pod_id": "p1", "gpu": "H100", "rate_usd_h": rate},
        {"utc": (now - timedelta(hours=5)).isoformat(), "event": "created", "pod_id": "p2",
         "gpu": "H100", "rate_usd_h": rate},
        {"utc": (now - timedelta(hours=4)).isoformat(), "event": "terminated", "pod_id": "p2"},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in ev))


def run(gen, ledger, clients, presets, **kw):
    kw.setdefault("wait", NO_WAIT)
    return JR.run_judging(gen, JR.default_judged_path(gen), ["aligned"], ["dsk"], clients,
                          ledger=ledger, presets={**PRESETS, "dsk": presets}, **kw)


# --- finding 1: running pods --------------------------------------------------------------


def test_running_pod_projection_from_log(tmp_path):
    log = tmp_path / "pods.jsonl"
    assert JR.running_pod_usd_from_log(tmp_path / "missing.jsonl") == 0.0
    write_pod_log(log, running_hours=10, rate=3.0)
    # only the running pod counts (the terminated one is already in the ledger via pod_down)
    assert JR.running_pod_usd_from_log(log) == pytest.approx(30.0, abs=0.05)


def test_guard_counts_running_pods_from_log(tmp_path):
    gen, ledger, log = make_gen(tmp_path), tmp_path / "costs.jsonl", tmp_path / "pods.jsonl"
    spend.record("gpu", "earlier", 1, 20.0, ledger=ledger)
    spend.record("judge", "earlier", 1, 15.0, ledger=ledger)
    # without pods: $35 + a tiny batch is under the $65 cap
    res = run(gen, ledger, {}, PRESETS["dsk"], dry_run=True, pod_events=log)
    assert res["aligned__dsk"]["guard"]["running_pod_usd"] == 0.0
    # a pod running for 10 h at $3/h: $65 + the batch > $65, refused before any call
    write_pod_log(log, running_hours=10, rate=3.0)
    client = SlowClient()
    with pytest.raises(spend.BudgetExceeded) as ei:
        run(gen, ledger, {"dsk": client}, PRESETS["dsk"], pod_events=log)
    assert ei.value.breakdown["running_pod_usd"] == pytest.approx(30.0, abs=0.05)
    assert client.calls == []


def test_in_run_headroom_counts_running_pods(tmp_path):
    gen, ledger = make_gen(tmp_path), tmp_path / "costs.jsonl"
    r = min(reservations(gen, COLUMN_FREE))
    spend.record("judge", "earlier", 1, 15.0 - 0.5 * r, ledger=ledger)
    # without pods the judge headroom ($3 + 0.5r) lets all 8 rows through (one at a time:
    # 8 concurrent reservations would not fit)
    out = run(gen, ledger, {"dsk": SlowClient(delay=0)}, COLUMN_FREE, running_pod_usd=0.0,
              max_concurrency=1)
    assert out["aligned__dsk"]["calls"] == 8 and not out["aligned__dsk"]["aborted"]

    gen2, ledger2 = make_gen(tmp_path / "b"), tmp_path / "costs2.jsonl"
    spend.record("judge", "earlier", 1, 15.0 - 0.5 * r, ledger=ledger2)
    # $50 of running pods leave hard-cap headroom 65 - 50 - (15 - 0.5r) = 0.5r < one call
    client = SlowClient(delay=0)
    with pytest.raises(spend.BudgetExceeded, match="in-run budget"):
        run(gen2, ledger2, {"dsk": client}, COLUMN_FREE, running_pod_usd=50.0)
    assert client.calls == [] and len(spend.read_entries(ledger2)) == 1


# --- finding 2: reservations ----------------------------------------------------------------


def test_concurrency_cannot_overshoot_headroom(tmp_path):
    gen = make_gen(tmp_path, n=50)
    judged = JR.default_judged_path(gen)
    gen_df, jdf, src = JR.load_or_init_judged(gen, judged)
    rs = reservations(gen, PRICEY)
    budget = 3.5 * max(rs)
    client = SlowClient(delay=0.05)
    usage = asyncio.run(JR.judge_column(
        gen_df, jdf, judged, "aligned", PRICEY, client, src, max_concurrency=50,
        budget_usd=budget, wait=NO_WAIT, log=lambda s: None))
    assert usage["aborted"] and 1 <= len(client.calls) <= 3
    # the dispatched calls were cancelled in flight: charged at their reservations
    assert usage["unknown_outcome_calls"] == len(client.calls)
    assert usage["usd"] == pytest.approx(sum(rs[: len(client.calls)]))
    assert usage["usd"] <= budget


def test_failed_calls_charged_at_reservation(tmp_path):
    gen, ledger = make_gen(tmp_path, n=3), tmp_path / "costs.jsonl"
    client = SlowClient(delay=0, fail=lambda: ConnectionError("down"))
    out = run(gen, ledger, {"dsk": client}, PRICEY, running_pod_usd=0.0, max_retries=2)
    u = out["aligned__dsk"]
    assert len(client.calls) == 6 and u["failed_attempts"] == 6 and u["calls"] == 0
    assert u["n_failed"] == 3 and u["usd_metered"] == 0.0
    assert u["usd"] == pytest.approx(2 * sum(reservations(gen, PRICEY)))
    last = spend.read_entries(ledger)[-1]
    assert last["units"] == 6 and last["usd"] == pytest.approx(u["usd"], abs=1e-6)


class ErrorWithUsage(Exception):
    def __init__(self):
        super().__init__("billed error")
        self.usage = {"prompt_tokens": 100, "completion_tokens": 2, "reasoning_tokens": 0}


def test_failed_calls_with_usage_are_metered(tmp_path):
    gen, ledger = make_gen(tmp_path, n=3), tmp_path / "costs.jsonl"
    client = SlowClient(delay=0, fail=ErrorWithUsage)
    u = run(gen, ledger, {"dsk": client}, PRICEY, running_pod_usd=0.0,
            max_retries=2)["aligned__dsk"]
    assert u["failed_attempts"] == 6 and u["usd_unmetered"] == 0.0
    assert u["usd"] == pytest.approx(PRICEY.usd(600, 12))
    assert spend.read_entries(ledger)[-1]["usd"] == pytest.approx(u["usd"], abs=1e-6)


def test_abort_reconciles_ledger(tmp_path):
    gen, ledger = make_gen(tmp_path, n=20), tmp_path / "costs.jsonl"
    rs = reservations(gen, COLUMN_FREE)
    spend.record("judge", "earlier", 1, 18.0 - 3.5 * max(rs), ledger=ledger)
    client = SlowClient(delay=0.05)
    with pytest.raises(spend.BudgetExceeded, match="in-run budget"):
        run(gen, ledger, {"dsk": client}, COLUMN_FREE, running_pod_usd=0.0, max_concurrency=20)
    n = len(client.calls)
    assert 1 <= n <= 3
    last = spend.read_entries(ledger)[-1]
    assert last["units"] == n and last["usd"] == pytest.approx(sum(rs[:n]), abs=1e-6)
    assert spend.spent("judge", ledger=ledger) <= 18.0 + 1e-6


class Boom(BaseException):
    """Stands in for KeyboardInterrupt / SystemExit escaping a call."""


def test_error_mid_column_still_records_in_flight(tmp_path):
    gen, ledger = make_gen(tmp_path, n=10), tmp_path / "costs.jsonl"
    rs = reservations(gen, PRICEY)
    client = SlowClient(delay=5.0, boom_on="Question 00?", boom=Boom())
    with pytest.raises(Boom):
        run(gen, ledger, {"dsk": client}, PRICEY, running_pod_usd=0.0, max_concurrency=4)
    # 4 dispatched at once; row 0's freed slot may dispatch row 4 before the abort lands
    n = len(client.calls)
    assert n in (4, 5)
    last = spend.read_entries(ledger)[-1]
    assert last["units"] == n and last["usd"] == pytest.approx(sum(rs[:n]), abs=1e-6)


def test_outside_cancellation_fills_usage_out(tmp_path):
    gen = make_gen(tmp_path, n=6)
    judged = JR.default_judged_path(gen)
    gen_df, jdf, src = JR.load_or_init_judged(gen, judged)
    rs = reservations(gen, PRICEY)
    client, usage = SlowClient(delay=5.0), {}

    async def main():
        t = asyncio.ensure_future(JR.judge_column(
            gen_df, jdf, judged, "aligned", PRICEY, client, src, max_concurrency=2,
            wait=NO_WAIT, usage_out=usage, log=lambda s: None))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(main())
    # cancelled tasks are not retried: exactly the 2 dispatched calls, charged at reservation
    assert len(client.calls) == 2
    assert usage["unknown_outcome_calls"] == 2
    assert usage["usd"] == pytest.approx(sum(rs[:2]))
