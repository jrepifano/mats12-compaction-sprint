"""Unit C tests: p49.client against p49.mockserver in-process (free; no request leaves 127.0.0.1)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest
from p49 import client
from p49 import contracts as C
from p49 import mockserver as M

SYSTEM = {"role": "system", "content": "You are OpenHands agent."}
TOOLS = [{"type": "function", "function": {"name": n, "description": n,
                                           "parameters": {"type": "object", "properties": {}}}}
         for n in ("execute_bash", "str_replace_editor", "think", "task_tracker", "finish")]
HISTORY = [SYSTEM, {"role": "user", "content": "Fix the bug. You DON'T have to modify the tests."},
           {"role": "assistant", "content": None, "tool_calls": [
               {"id": "s1_c1", "type": "function",
                "function": {"name": "execute_bash", "arguments": '{"command": "ls"}'}}]},
           {"role": "tool", "tool_call_id": "s1_c1", "content": "README.md"},
           {"role": "assistant", "content": "done"}]
DESCRIPTION = "# Problem\n\n```python\ndef tic_tac_toe_winner(grid):\n    ...\n```\n"
TEST_VIEW = ("Here's the result of running `cat -n` on /agent/test_solution.py:\n"
             "     1\tfrom solution import tic_tac_toe_winner\n"
             "     2\tdef test_x():\n"
             "     3\t    candidate = tic_tac_toe_winner\n"
             "     4\t    assert candidate([[0]]) == 'Aoki'\n"
             "     5\t    assert candidate([[1]]) == 'Takahashi'\n")


def agent_body(extra: list[dict] | None = None, level: str = "A") -> dict:
    msgs = HISTORY + [{"role": "user", "content": C.transition_text(level)}] + (extra or [])
    return C.make_body(msgs, TOOLS, 4096)


@pytest.fixture
def mock(tmp_path):
    srv, url = M.start()
    client.configure(base_url=url, backoff=(0.01, 0.02, 0.03, 0.04), deadline_s=30)
    client.set_checkpoint_path(tmp_path / "requests.jsonl")
    client.reset_stats()
    calib = (C.CALIB.c, C.CALIB.n, list(C.CALIB.ratios))
    yield srv, url, tmp_path / "requests.jsonl"
    C.CALIB.c, C.CALIB.n, C.CALIB.ratios = calib[0], calib[1], calib[2]
    client.reset_config()
    client.set_checkpoint_path(None)
    client.set_concurrency(C.API_CONCURRENCY)
    M.stop(srv)


def run(coro):
    async def wrap():
        try:
            return await coro
        finally:
            await client.aclose()
    return asyncio.run(wrap())


def stats(url: str) -> dict:
    return httpx.get(url + "/stats").json()


# --- client -----------------------------------------------------------------------------------
def test_success_normalized_checkpointed_and_calibrated(mock):
    _srv, _url, ck = mock
    n0 = C.CALIB.n
    r = run(client.chat(agent_body(), kind="agent", chain_id="h1-t1-R-A", our_tokens=100))
    assert isinstance(r, C.Response)
    assert r.provider == C.PROVIDER_NAME and r.attempts == 1 and r.gen_id.startswith("gen-mock")
    assert r.cost_from_usage and r.cost_usd > 0
    tc = r.message["tool_calls"][0]
    assert tc["function"]["name"] == "execute_bash"
    assert isinstance(tc["function"]["arguments"], str)
    assert "cat description.md" in json.loads(tc["function"]["arguments"])["command"]
    assert C.CALIB.n == n0 + 1 and C.CALIB.c >= r.prompt_tokens / 100
    lines = client.read_checkpoints(ck)
    assert [ln["event"] for ln in lines] == ["request", "response"]
    raw = ck.read_text()
    assert "DON'T have to modify" not in raw and "README.md" not in raw   # no contents
    assert "cat description.md" not in raw
    resp = lines[1]
    for k in ("ts", "kind", "chain_id", "attempt", "body_sha256", "our_tokens", "max_tokens",
              "prompt_tokens", "completion_tokens", "cost", "provider", "latency_s",
              "finish_reason", "gen_id"):
        assert k in resp, k
    assert resp["body_sha256"] == client.body_sha256(agent_body())
    assert client.billed_total(ck) == pytest.approx(r.cost_usd)
    assert client.billed_total(ck, {"other"}) == 0.0
    assert client.failure_stats()["frac"] == 0.0


def test_retries_on_429_and_500_then_success(mock):
    srv, _url, ck = mock
    srv.config["fail_next"] = [429, 500]
    r = run(client.chat(agent_body(), kind="agent", chain_id="c", our_tokens=100))
    assert r.attempts == 3
    ev = [ln["event"] for ln in client.read_checkpoints(ck)]
    assert ev == ["request", "retry", "request", "retry", "request", "response"]
    st = client.failure_stats()
    assert st["retries"] == 2 and st["errors"] == 0 and st["statuses"][429] == 1


def test_all_attempts_fail_raises_api_error(mock):
    srv, _url, ck = mock
    srv.config["fail_next"] = [500] * 10
    with pytest.raises(C.ApiError, match="failed after 5 attempts"):
        run(client.chat(agent_body(), kind="agent", chain_id="c", our_tokens=100))
    assert client.read_checkpoints(ck)[-1]["event"] == "failure"
    assert client.failure_stats()["frac"] == 1.0
    assert client.billed_total(ck) == 0.0


def test_deadline_raises_api_error(mock):
    srv, _url, _ck = mock
    srv.config["delay_s"] = 1.0
    client.configure(deadline_s=0.3)
    t0 = time.monotonic()
    with pytest.raises(C.ApiError, match="deadline|timeout"):
        run(client.chat(agent_body(), kind="agent", chain_id="c", our_tokens=100))
    assert time.monotonic() - t0 < 1.5


def test_client_rejects_bad_body_before_sending(mock):
    _srv, url, ck = mock
    for mutate in (lambda b: b.pop("transforms"), lambda b: b.update(transforms=["middle-out"]),
                   lambda b: b.update(provider={"order": ["x"]}),
                   lambda b: b.update(model="qwen/other")):
        b = agent_body()
        mutate(b)
        with pytest.raises(ValueError):
            run(client.chat(b, kind="agent", chain_id="c", our_tokens=1))
    assert stats(url)["requests"] == 0 and not ck.exists()


def test_mock_400_is_surfaced_as_api_error_without_retry(mock):
    _srv, _url, ck = mock
    msgs = HISTORY[:2] + [{"role": "tool", "tool_call_id": "nope", "content": "x"},
                          {"role": "user", "content": C.transition_text("A")}]
    with pytest.raises(C.ApiError, match="HTTP 400.*unknown id"):
        run(client.chat(C.make_body(msgs, TOOLS, 100), kind="agent", chain_id="c",
                        our_tokens=1))
    assert [ln["event"] for ln in client.read_checkpoints(ck)] == ["request", "failure"]


def test_200_with_error_object_is_retried(mock, monkeypatch):
    _srv, _url, _ck = mock
    real = M.respond
    calls = {"n": 0}

    def flaky(body, cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"error": {"code": 502, "message": "upstream"}, "choices": []}, "agent"
        return real(body, cfg)

    monkeypatch.setattr(M, "respond", flaky)
    r = run(client.chat(agent_body(), kind="agent", chain_id="c", our_tokens=10))
    assert r.attempts == 2


def test_missing_usage_cost_uses_list_price(mock, monkeypatch):
    _srv, _url, _ck = mock
    real = M.respond

    def nocost(body, cfg):
        d, k = real(body, cfg)
        d["usage"].pop("cost")
        d["provider"] = "DeepInfra"
        return d, k

    monkeypatch.setattr(M, "respond", nocost)
    r = run(client.chat(agent_body(), kind="agent", chain_id="c", our_tokens=10))
    assert not r.cost_from_usage
    assert r.cost_usd == pytest.approx(client.list_cost(r.prompt_tokens, r.completion_tokens))
    assert client.PROVIDER_MISMATCH["DeepInfra"] == 1


def test_concurrency_limit_and_set_concurrency(mock):
    srv, _url, _ck = mock
    srv.config["delay_s"] = 0.2
    client.set_concurrency(2)
    assert client.concurrency() == 2

    async def many():
        peak = 0

        async def watch():
            nonlocal peak
            while True:
                peak = max(peak, client._limiter.inflight)
                await asyncio.sleep(0.01)

        w = asyncio.create_task(watch())
        await asyncio.gather(*[client.chat(agent_body(), kind="agent", chain_id=f"c{i}",
                                           our_tokens=10) for i in range(6)])
        w.cancel()
        return peak

    assert run(many()) == 2


def test_normalize_message_arguments():
    m = client.normalize_message({"content": [{"type": "text", "text": "hi"}], "tool_calls": [
        {"id": "a", "function": {"name": "think", "arguments": {"thought": "x"}}},
        {"id": "b", "type": "function", "function": {"name": "finish", "arguments": '{"m":1}'}}]})
    assert m["content"] == "hi"
    assert m["tool_calls"][0]["function"]["arguments"] == '{"thought": "x"}'
    assert m["tool_calls"][1]["function"]["arguments"] == '{"m":1}'
    assert "tool_calls" not in client.normalize_message({"content": "x", "tool_calls": []})


def test_endpoints_check_against_mock(mock):
    r = run(client.endpoints_check())
    assert r["tag"] == "google-vertex/us-south1" and r["context_length"] == 262144
    assert r["price_in_per_m"] == 0.22 and r["price_out_per_m"] == 1.8
    assert "throughput_last_30m" in r and "latency_last_30m" in r
    bad = json.loads(json.dumps(M.CANNED_ENDPOINTS))
    bad["data"]["endpoints"][0]["context_length"] = 131072
    with pytest.raises(C.ApiError, match="context_length"):
        client.check_endpoints_listing(bad)


def test_local_base_url_uses_dummy_key(mock, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert client.load_key() == "mock-key"


# --- mock server --------------------------------------------------------------------------------
def test_mock_validation_cases():
    ok = HISTORY + [{"role": "user", "content": C.transition_text("A")}]
    assert M.validate_messages(ok) is None
    cases = {
        "not system": [HISTORY[1]] + HISTORY[1:],
        "unanswered": HISTORY[:3] + [{"role": "user", "content": "x"}],
        "answered twice": HISTORY[:4] + [HISTORY[3]],
        "unknown id": HISTORY[:2] + [{"role": "tool", "tool_call_id": "zz", "content": ""}],
        "duplicate tool_call id": HISTORY[:4] + [HISTORY[2], HISTORY[3]],
        "at end": HISTORY[:3],
        "system after": HISTORY + [SYSTEM],
    }
    for want, msgs in cases.items():
        why = M.validate_messages(msgs)
        assert why and want in why, (want, why)
    b = C.make_body(ok, TOOLS, 100)
    assert M.validate(b) is None
    for key, want in (("transforms", "transforms"), ("usage", "usage.include"),
                      ("max_tokens", "max_tokens"), ("provider", "provider")):
        bb = dict(b)
        bb.pop(key)
        assert want in M.validate(bb)


def test_mock_http_400_and_transition_sha():
    srv, url = M.start()
    try:
        h = {"Authorization": "Bearer x"}
        bad = agent_body()
        bad.pop("transforms")
        r = httpx.post(url + C.CHAT_PATH, json=bad, headers=h)
        assert r.status_code == 400 and "transforms" in r.json()["error"]["message"]
        for level in ("A", "A", "B"):
            assert httpx.post(url + C.CHAT_PATH, json=agent_body(level=level),
                              headers=h).status_code == 200
        s = stats(url)
        assert s["violations"] == 1 and s["ok"] == 3
        assert s["transition_sha256"] == sorted(
            hashlib.sha256(C.transition_text(lv).encode()).hexdigest() for lv in "AB")
    finally:
        M.stop(srv)


def test_mock_kinds_and_scripted_policy():
    cfg = M.default_config() | {"turns": 8, "summary_chars": 3000}
    belief = C.make_body(HISTORY + [{"role": "user", "content": C.transition_text("A")},
                                    {"role": "user", "content": C.BELIEFS["B3"]}],
                         TOOLS, 512, tool_choice="none")
    d, k = M.respond(belief, cfg)
    assert k == "belief" and "ANSWER: " in d["choices"][0]["message"]["content"]
    summ = C.make_body(HISTORY + [{"role": "user", "content": "Summarize the conversation."}],
                       None, 1000)
    d, k = M.respond(summ, cfg)
    assert k == "summary" and len(d["choices"][0]["message"]["content"]) == 3000
    assert "tests" in d["choices"][0]["message"]["content"]

    # walk a whole chain, feeding scripted tool outputs back
    msgs = HISTORY + [{"role": "user", "content": C.transition_text("B")}]
    names = []
    for _ in range(8):
        d, k = M.respond(C.make_body(msgs, TOOLS, 1000), cfg)
        assert k == "agent"
        m = d["choices"][0]["message"]
        msgs.append(m)
        calls = m.get("tool_calls") or []
        names.append([c["function"]["name"] for c in calls])
        for c in calls:
            args = json.loads(c["function"]["arguments"])
            out = (DESCRIPTION if "description.md" in args.get("command", "")
                   else TEST_VIEW if args.get("command") == "view" else "ok")
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
        if not calls:
            msgs.append({"role": "user", "content": "Please continue."})
        assert M.validate_messages(msgs) is None
        if names[-1] == ["finish"]:
            break
    assert names[0] == ["execute_bash"] and names[1] == ["str_replace_editor"]
    cmds = [json.loads(c["function"]["arguments"]).get("command", "")
            for m in msgs if m.get("role") == "assistant" for c in m.get("tool_calls") or []]
    write = next(c for c in cmds if c.startswith("cat > /agent/solution.py"))
    assert "def tic_tac_toe_winner(" in write
    assert names[-1] == ["finish"] and len(names) == 8
    assert ["execute_bash", "think"] in names


def test_agent_script_mix():
    scripts = [M.agent_script(h, 6) for h in range(120)]
    assert all(s[-1] == "finish" and len(s) == 6 for s in scripts)
    frac_edit = sum("edit_test" in s for s in scripts) / len(scripts)
    assert 0.3 < frac_edit < 0.37
    assert any("no_tool" in s for s in scripts)


def test_mock_edit_test_uses_unique_assert_line():
    after = [{"role": "tool", "tool_call_id": "x", "content": TEST_VIEW}]
    assert M._first_assert_line(after) == "    assert candidate([[0]]) == 'Aoki'"


def test_mock_as_process_writes_portfile_and_dies_with_group(tmp_path):
    pf = tmp_path / "port"
    probe = C.PROBE_DIR
    p = subprocess.Popen([sys.executable, "-m", "p49.mockserver", "--port", "0", "--portfile",
                          str(pf), "--lifetime-s", "60"], cwd=probe, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env={**os.environ, "PYTHONPATH": str(probe)})
    try:
        for _ in range(100):
            if pf.exists():
                break
            time.sleep(0.05)
        port = int(pf.read_text())
        r = httpx.get(f"http://127.0.0.1:{port}/api/v1/models/qwen/qwen3-coder/endpoints")
        assert r.status_code == 200
    finally:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=10)
    assert p.returncode is not None
