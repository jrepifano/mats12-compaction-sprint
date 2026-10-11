"""E11 part 2 LLM reader (unit U6): the parser, the request, the resumable CSV, the spend
reservation (projection refusal, headroom, crash recovery). Fake client only: no paid calls."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from emx import spend
from emx.e11 import contracts as C
from emx.e11 import reader as R

GOOD = "SHIFT: 70\nPERSONA: 40\nCOHERENCE: 55\nBREADTH: 10"


# --- parser --------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    (GOOD, {"shift": 70, "persona": 40, "coherence": 55, "breadth": 10}),
    ("\n  " + GOOD + "  \n\n", {"shift": 70, "persona": 40, "coherence": 55, "breadth": 10}),
    ("SHIFT:0\n  PERSONA : 100 \nCOHERENCE:  1\nBREADTH: 0",
     {"shift": 0, "persona": 100, "coherence": 1, "breadth": 0}),
])
def test_parse_good(text, want):
    assert R.parse_reply(text) == want


@pytest.mark.parametrize("text", [
    None, "", "   ",
    GOOD + "\nThat is my rating.",              # extra line
    "Here you go:\n" + GOOD,                   # extra leading text
    "SHIFT: 70 points\nPERSONA: 40\nCOHERENCE: 55\nBREADTH: 10",  # extra text on a line
    "SHIFT: 101\nPERSONA: 40\nCOHERENCE: 55\nBREADTH: 10",       # out of range
    "SHIFT: -5\nPERSONA: 40\nCOHERENCE: 55\nBREADTH: 10",        # negative
    "SHIFT: 7.5\nPERSONA: 40\nCOHERENCE: 55\nBREADTH: 10",       # decimal
    "SHIFT: 70\nPERSONA: 40\nCOHERENCE: 55",                     # missing line
    "PERSONA: 40\nSHIFT: 70\nCOHERENCE: 55\nBREADTH: 10",        # reordered: refused
    "SHIFT: 70\nSHIFT: 40\nCOHERENCE: 55\nBREADTH: 10",          # repeated
    "SHIFT: 70\n\nPERSONA: 40\nCOHERENCE: 55\nBREADTH: 10",      # blank line inside
    "**SHIFT:** 70\nPERSONA: 40\nCOHERENCE: 55\nBREADTH: 10",    # markdown
    "shift: 70\npersona: 40\ncoherence: 55\nbreadth: 10",        # case
    "SHIFT: \nPERSONA: 40\nCOHERENCE: 55\nBREADTH: 10",          # empty value
])
def test_parse_failures(text):
    assert R.parse_reply(text) is None


def test_prompt_and_request():
    assert hashlib.sha256(C.READER_PROMPT.encode()).hexdigest() == C.READER_PROMPT_SHA256
    p = R.build_prompt("ino", "What {is} this?", "An {answer}.")
    assert C.INOCULATION in p and "What {is} this?" in p and "An {answer}." in p
    assert C.NEUTRAL not in p
    kw = R.request_kwargs(p)
    assert kw["model"] == "gpt-6-luna" and kw["reasoning_effort"] == "none"
    assert kw["temperature"] == 0 and kw["seed"] == 0
    assert kw["max_completion_tokens"] == R.MAX_TOKENS
    assert kw["messages"] == [{"role": "user", "content": p}]


# --- fakes ---------------------------------------------------------------------------------------


def _pool(n_ds=2, n_rows=3):
    return {f"ds{d}": [{"row_id": f"ds{d}:{i}", "question": f"q{d}-{i}", "answer": f"a{d}-{i}",
                        "half": "A" if i % 2 == 0 else "B"} for i in range(n_rows)]
            for d in range(n_ds)}


def _resp(text, pt=100, ct=20, usage=True):
    u = SimpleNamespace(prompt_tokens=pt, completion_tokens=ct, completion_tokens_details=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                           usage=u if usage else None)


class FakeClient:
    """`reply(kwargs, n)` -> a response, or raises. Records calls and peak concurrency."""

    def __init__(self, reply=None, delay=0.0):
        self.reply = reply or (lambda kw, n: _resp(GOOD))
        self.delay = delay
        self.calls: list[dict] = []
        self.active = 0
        self.peak = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        n = len(self.calls)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.reply(kw, n)
        finally:
            self.active -= 1


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(R, "RETRY_BASE_S", 0.0)


def _run(tmp_path, client, pool=None, **kw):
    return R.run_reader(tmp_path, client_factory=lambda: client, pool=pool or _pool(),
                        csv_path=tmp_path / "reader.csv", ledger=tmp_path / "costs.jsonl",
                        log=lambda s: None, **kw)


def _rows(tmp_path):
    with open(tmp_path / "reader.csv", newline="") as f:
        return list(csv.DictReader(f))


def _states(tmp_path):
    return spend.reservation_states(spend.read_entries(tmp_path / "costs.jsonl"))


# --- the run -------------------------------------------------------------------------------------


def test_run_scores_every_row_and_framing(tmp_path):
    client = FakeClient(delay=0.01)
    s = _run(tmp_path, client, concurrency=4)
    rows = _rows(tmp_path)
    with open(tmp_path / "reader.csv") as f:
        assert f.readline().strip() == ",".join(C.READER_COLUMNS)
    assert len(rows) == 12 and len(client.calls) == 12
    assert {(r["ds"], r["row_id"], r["framing"]) for r in rows} == {
        (f"ds{d}", f"ds{d}:{i}", f) for d in range(2) for i in range(3) for f in ("neu", "ino")}
    assert all(r["status"] == "scored" and r["shift"] == "70" and r["breadth"] == "10"
               for r in rows)
    assert json.loads(rows[0]["raw"]) == GOOD and rows[0]["in_tok"] == "100"
    assert rows[1]["half"] in ("A", "B")
    assert 1 < client.peak <= 4
    # the reader never sees both framings of a row in one call
    for kw in client.calls:
        text = kw["messages"][0]["content"]
        assert (C.NEUTRAL in text) != (C.INOCULATION in text)
    # spend: one reservation, submitted, settled at the metered cost
    (st,) = _states(tmp_path).values()
    assert st["closed"] and st["submitted"] and st["note"] == R.NOTE and st["exp"] == "E11"
    metered = R.PRESET.usd(12 * 100, 12 * 20)
    assert st["charges"] == pytest.approx(R._ceil_usd(metered), abs=2e-6)
    assert st["charges"] <= st["reserve_usd"]
    assert s["settled_usd"] == pytest.approx(metered, abs=2e-6)
    meta = json.loads((tmp_path / "reader.meta.json").read_text())
    assert meta["fingerprint"]["prompt_sha256"] == C.READER_PROMPT_SHA256
    assert meta["counts"]["scored"] == 12 and meta["usage_totals"]["calls"] == 12
    assert meta["usage_totals"]["prompt_tokens"] == 1200
    assert set(meta["provenance"]) == {"git_commit", "utc", "seeds", "inputs"}
    # a second run has nothing to do and reserves nothing
    s2 = _run(tmp_path, FakeClient())
    assert s2["n_pending"] == 0 and len(_states(tmp_path)) == 1


def test_parse_failure_retried_then_missing(tmp_path):
    def reply(kw, n):
        return _resp("I'd rate it high." if "q0-0" in kw["messages"][0]["content"] else GOOD)

    client = FakeClient(reply)
    _run(tmp_path, client, pool=_pool(1, 2))
    rows = {(r["row_id"], r["framing"]): r for r in _rows(tmp_path)}
    bad = rows[("ds0:0", "neu")]
    assert bad["status"] == "missing" and bad["shift"] == "" and bad["in_tok"] == "300"
    assert json.loads(bad["raw"]) == "I'd rate it high."
    assert rows[("ds0:1", "neu")]["status"] == "scored"
    assert len(client.calls) == 2 * (1 + C.READER_RETRIES) + 2  # 2 bad tasks x 3, 2 good x 1
    # missing is terminal: not re-sent
    again = FakeClient()
    _run(tmp_path, again, pool=_pool(1, 2))
    assert again.calls == []


def test_transient_error_retried_and_charged_at_bound(tmp_path):
    req = httpx.Request("POST", "https://api.example/v1")

    def reply(kw, n):
        if n == 1:
            raise openai.APIConnectionError(request=req)
        return _resp(GOOD)

    client = FakeClient(reply)
    s = _run(tmp_path, client, pool=_pool(1, 1), concurrency=1)
    assert [r["status"] for r in _rows(tmp_path)] == ["scored", "scored"]
    assert len(client.calls) == 3 and s["usage"]["failed_attempts"] == 1
    bound = R.call_bound_usd(R.build_prompt("neu", "q0-0", "a0-0"))
    assert s["settled_usd"] == pytest.approx(R._ceil_usd(bound + R.PRESET.usd(200, 40)),
                                             abs=2e-6)


def test_api_error_status_is_written_and_resent(tmp_path):
    resp = httpx.Response(400, request=httpx.Request("POST", "https://api.example/v1"))

    def reply(kw, n):
        if "q0-1" in kw["messages"][0]["content"]:
            raise openai.BadRequestError("bad", response=resp, body=None)
        return _resp(GOOD)

    _run(tmp_path, FakeClient(reply), pool=_pool(1, 2))
    rows = {(r["row_id"], r["framing"]): r["status"] for r in _rows(tmp_path)}
    assert rows[("ds0:1", "neu")] == "error" and rows[("ds0:0", "ino")] == "scored"
    again = FakeClient()
    _run(tmp_path, again, pool=_pool(1, 2))
    assert len(again.calls) == 2  # only the two error rows
    latest = R.load_results(tmp_path / "reader.csv")
    assert all(r["status"] == "scored" for r in latest.values()) and len(latest) == 4


def test_resume_after_partial_csv_with_torn_tail(tmp_path):
    _run(tmp_path, FakeClient())
    p = tmp_path / "reader.csv"
    lines = p.read_text().splitlines(keepends=True)
    p.write_text("".join(lines[:5]) + lines[5][:17])  # header + 4 rows + a torn line
    assert len(R.load_results(p)) == 4
    again = FakeClient()
    s = _run(tmp_path, again)
    assert s["tail_bytes_truncated"] == 17
    assert len(again.calls) == 8
    latest = R.load_results(p)
    assert len(latest) == 12 and all(r["status"] == "scored" for r in latest.values())
    assert len(p.read_text().splitlines()) == 13  # no duplicate rows, no torn bytes


def test_resume_refuses_another_prompt_or_pool(tmp_path):
    _run(tmp_path, FakeClient())
    mp = tmp_path / "reader.meta.json"
    meta = json.loads(mp.read_text())
    meta["fingerprint"]["prompt_sha256"] = "0" * 64
    mp.write_text(json.dumps(meta))
    with pytest.raises(R.FingerprintMismatch):
        _run(tmp_path, FakeClient())
    meta["fingerprint"] = R.fingerprint()
    mp.write_text(json.dumps(meta))
    other = _pool()
    other["ds0"][0]["half"] = "B"  # the recorded half no longer matches the pool
    with pytest.raises(R.FingerprintMismatch):
        _run(tmp_path, FakeClient(), pool=other)


def test_resume_refuses_changed_input_text(tmp_path):
    """Review r1b finding 3: an answer changed under the same key and half is refused, with no
    call sent; the sidecar records each task's prompt sha256."""
    _run(tmp_path, FakeClient())
    meta = json.loads((tmp_path / "reader.meta.json").read_text())
    t = R.tasks_from_pool(_pool())[0]
    assert meta["task_sha256"][R.task_key_str(t.key)] == hashlib.sha256(
        t.prompt().encode()).hexdigest()
    assert len(meta["task_sha256"]) == 12
    other = _pool()
    other["ds0"][1]["answer"] = "a different answer"
    again = FakeClient()
    with pytest.raises(R.FingerprintMismatch, match="other input text"):
        _run(tmp_path, again, pool=other)
    assert again.calls == []
    other = _pool()
    other["ds1"][2]["question"] = "a different question"
    with pytest.raises(R.FingerprintMismatch, match="other input text"):
        _run(tmp_path, FakeClient(), pool=other)
    # a sidecar without per-task sha256s cannot vouch for its rows
    mp = tmp_path / "reader.meta.json"
    mp.write_text(json.dumps({k: v for k, v in meta.items() if k != "task_sha256"}))
    with pytest.raises(R.FingerprintMismatch, match="per-task sha256"):
        _run(tmp_path, FakeClient())


def test_resume_refuses_other_pool_inputs(tmp_path):
    inputs = {C.POOL_MANIFEST: "a" * 64, f"{C.POOL_DIR}/ds0.jsonl": "b" * 64}
    _run(tmp_path, FakeClient(), pool=_pool(1, 2), inputs=inputs)
    meta = json.loads((tmp_path / "reader.meta.json").read_text())
    assert meta["pool_inputs"] == inputs
    s = _run(tmp_path, FakeClient(), pool=_pool(1, 2), inputs=dict(inputs))
    assert s["n_pending"] == 0  # same inputs: resumes
    # a resume without inputs (tests' direct pools) keeps the recorded ones
    _run(tmp_path, FakeClient(), pool=_pool(1, 2))
    assert json.loads((tmp_path / "reader.meta.json").read_text())["pool_inputs"] == inputs
    again = FakeClient()
    with pytest.raises(R.FingerprintMismatch, match="pool_manifest"):
        _run(tmp_path, again, pool=_pool(1, 2), inputs={**inputs, C.POOL_MANIFEST: "c" * 64})
    assert again.calls == []
    # rows made without recorded inputs cannot be resumed under inputs
    t2 = tmp_path / "fresh"
    t2.mkdir()
    _run(t2, FakeClient(), pool=_pool(1, 2))
    with pytest.raises(R.FingerprintMismatch, match="no pool inputs"):
        _run(t2, FakeClient(), pool=_pool(1, 2), inputs=inputs)


# --- spend ---------------------------------------------------------------------------------------


def test_projection_refused_over_the_cap(tmp_path):
    led = tmp_path / "costs.jsonl"
    spend.record("judge", "planted", 1, C.API_LIMIT_USD - 0.001, exp="E11", ledger=led)
    proj = R.project(R.tasks_from_pool(_pool()))
    assert proj["n_calls"] == 12 and proj["reservation_usd"] >= proj["bound_usd"] > 0
    with pytest.raises(spend.BudgetExceeded):
        spend.check_total(proj["reservation_usd"], "judge", exp="E11", ledger=led)
    client = FakeClient()
    with pytest.raises(spend.BudgetExceeded):
        _run(tmp_path, client)
    assert client.calls == [] and not (tmp_path / "reader.csv").exists()
    assert _states(tmp_path) == {}


def test_script_dry_run_projects_and_guards(tmp_path, monkeypatch, capsys):
    import importlib.util

    from emx.e11 import pool as P

    spec = importlib.util.spec_from_file_location("e11_reader_script",
                                                  C.REPO_ROOT / "scripts" / "e11_reader.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    over = {}
    for ds in C.POOL_DATASETS:
        prompts = ([f"p{i}" for i in range(300)] if ds in ("bad_medical", "good_medical",
                                                            "srh_hack", "srh_control") else
                   [f"{ds} q{i}" for i in range(300)])
        over[ds] = ([json.dumps({"messages": [{"role": "user", "content": q},
                                              {"role": "assistant", "content": f"{ds} a"}]})
                     for q in prompts], {"kind": "local", "path": ds, "sha256": "0" * 64})
    P.build(tmp_path, tmp_path / "dl", local_override=over, log=lambda s: None)
    led = tmp_path / "costs.jsonl"
    monkeypatch.setenv(spend.LEDGER_ENV, str(led))

    def no_client():
        raise AssertionError("dry run must not build a client")

    monkeypatch.setattr(R, "make_client", no_client)
    assert mod.main(["--root", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "7200 (row, framing) tasks" in out and "spend guard OK" in out
    assert "no calls made" in out and not led.exists()
    assert mod.main(["--root", str(tmp_path)]) == 2  # no --yes
    spend.record("judge", "planted", 1, C.API_LIMIT_USD - 0.01, exp="E11", ledger=led)
    with pytest.raises(spend.BudgetExceeded):
        mod.main(["--root", str(tmp_path), "--dry-run"])


def test_headroom_bounds_worst_case_spend(tmp_path, monkeypatch):
    # every call costs exactly its bound (no usage reported); reserve room for 5 calls only
    def tiny(bounds):
        return R._ceil_usd(5 * max(bounds))

    monkeypatch.setattr(R, "reservation_usd", tiny)
    client = FakeClient(lambda kw, n: _resp(GOOD, usage=False))
    s = _run(tmp_path, client, concurrency=3)
    (st,) = _states(tmp_path).values()
    assert st["closed"] and st["charges"] <= st["reserve_usd"] + 1e-6
    assert s["halted"] and "headroom" in s["halted"]
    assert len(client.calls) <= 5 and len(_rows(tmp_path)) == len(client.calls)
    # the rest is resumable under a fresh reservation
    monkeypatch.undo()
    monkeypatch.setattr(R, "RETRY_BASE_S", 0.0)
    _run(tmp_path, FakeClient())
    assert len(R.load_results(tmp_path / "reader.csv")) == 12


def test_crash_between_submit_and_settle_is_recovered(tmp_path, monkeypatch):
    real_settle = spend.settle_api

    def killed(*a, **k):
        raise SystemExit("process killed before settlement")

    monkeypatch.setattr(spend, "settle_api", killed)
    with pytest.raises(SystemExit):
        _run(tmp_path, FakeClient(), pool=_pool(1, 2))
    (st,) = _states(tmp_path).values()
    assert st["submitted"] and not st["closed"]  # left open by the crash
    assert len(_rows(tmp_path)) == 4  # rows written before the crash survive
    monkeypatch.setattr(spend, "settle_api", real_settle)
    again = FakeClient()
    s = _run(tmp_path, again, pool=_pool(2, 2))  # more rows now pending
    assert s["recovered_reservations"] == [st["reservation_id"]]
    states = _states(tmp_path)
    assert len(states) == 2 and all(x["closed"] for x in states.values())
    old = states[st["reservation_id"]]
    assert old["charges"] == pytest.approx(old["reserve_usd"])  # settled at the full amount
    assert len(again.calls) == 4  # only ds1's rows
    assert len(R.load_results(tmp_path / "reader.csv")) == 8


def test_unsubmitted_reservation_released_when_client_fails(tmp_path):
    def no_key():
        raise RuntimeError("OPENAI_API_KEY is not set")

    with pytest.raises(RuntimeError):
        R.run_reader(tmp_path, client_factory=no_key, pool=_pool(), csv_path=tmp_path / "r.csv",
                     ledger=tmp_path / "costs.jsonl", log=lambda s: None)
    (st,) = _states(tmp_path).values()
    assert st["closed"] and not st["submitted"] and st["charges"] == 0
