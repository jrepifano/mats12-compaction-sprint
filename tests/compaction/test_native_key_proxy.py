"""The native Claude Code key proxy (compaction.s2.native.key_proxy): pricing, bounds, and the
reserve-before-forward contract against a local fake upstream."""

import http.client
import http.server
import json
import socketserver
import threading

import pytest

from compaction.s2.native import key_proxy as K


def test_haiku_cost_tiers_on_prompt_length():
    small = {"input_tokens": 2, "cache_creation_input_tokens": 17477,
             "cache_read_input_tokens": 0, "output_tokens": 4}
    # live check, 28 Oct: $0.002187 at the base rates
    assert K.haiku_cost(small) == pytest.approx((2 * 0.10 + 17477 * 0.125 + 4 * 0.50) / 1e6)
    big = {"input_tokens": 1000, "cache_creation_input_tokens": 2000,
           "cache_read_input_tokens": 200_000, "output_tokens": 500}
    # over 100k (cache reads count): the whole request at the higher rates
    assert K.haiku_cost(big) == pytest.approx(
        (1000 * 0.50 + 2000 * 0.625 + 200_000 * 0.05 + 500 * 2.50) / 1e6)
    one_h = {"input_tokens": 0, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0,
             "output_tokens": 0, "cache_creation": {"ephemeral_1h_input_tokens": 400,
                                                    "ephemeral_5m_input_tokens": 600}}
    assert K.haiku_cost(one_h) == pytest.approx((600 * 0.125 + 400 * 0.20) / 1e6)


def test_bound_covers_the_worst_case_price():
    # a token is never less than a byte: N body bytes bound the prompt at N tokens
    usage = {"input_tokens": 0, "cache_creation_input_tokens": 300_000,
             "cache_read_input_tokens": 0, "output_tokens": 32_000,
             "cache_creation": {"ephemeral_1h_input_tokens": 300_000}}
    assert K.bound_usd(300_000, 32_000) >= K.haiku_cost(usage)
    assert K.bound_usd(1, 0) == 0.0001  # rounded up


def test_usage_of_sse_and_json():
    sse = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1",'
           b'"model":"claude-haiku-5-5","usage":{"input_tokens":5,"output_tokens":1}}}\n\n'
           b'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n')
    u, mid, model, complete = K.usage_of(sse)
    assert (mid, model, u["input_tokens"], u["output_tokens"]) == ("msg_1", "claude-haiku-5-5", 5, 7)
    assert not complete  # no message_stop: interrupted, settled at the bound
    u, _, _, complete = K.usage_of(sse + b'data: {"type":"message_stop"}\n\n')
    assert complete and u["output_tokens"] == 7
    u, mid, _, complete = K.usage_of(json.dumps({"id": "m2", "usage": {"input_tokens": 3}}).encode())
    assert mid == "m2" and u["input_tokens"] == 3 and complete
    assert K.usage_of(b"not json") == (None, None, None, False)


class FakeSpend:
    def __init__(self, refuse=False):
        self.refuse, self.reserved, self.settled = refuse, [], []

    def reserve(self, usd, note):
        if self.refuse:
            raise RuntimeError("cap")
        self.reserved.append(usd)
        return f"r{len(self.reserved)}"

    def settle(self, rid, usd, *, resource, units, note):
        self.settled.append((rid, usd))


@pytest.fixture
def upstream():
    seen = []

    class U(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            seen.append({"path": self.path, "key": self.headers.get("x-api-key"),
                         "auth": self.headers.get("authorization"), "body": body})
            out = json.dumps({"id": "msg_x", "model": "claude-haiku-5-5", "usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 20}}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = S(("127.0.0.1", 0), U)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], seen
    srv.shutdown()


def _post(port, path, body, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, body=body, headers={"content-type": "application/json",
                                                **(headers or {})})
    r = c.getresponse()
    return r.status, r.read()


def test_forwards_with_the_real_key_and_settles_from_usage(tmp_path, upstream):
    port, seen = upstream
    sp = FakeSpend()
    p = K.KeyProxy(key="real-key", spend=sp, log_path=tmp_path / "calls.jsonl",
                   upstream=("127.0.0.1", port, False)).start()
    try:
        status, body = _post(p.port, "/v1/messages?beta=true",
                             json.dumps({"model": K.MODEL, "max_tokens": 100, "messages": []}),
                             {"x-api-key": "dummy", "authorization": "Bearer dummy"})
    finally:
        p.stop()
    assert status == 200 and json.loads(body)["id"] == "msg_x"
    assert seen[0]["key"] == "real-key" and seen[0]["auth"] is None  # the agent's creds dropped
    assert len(sp.reserved) == 1 and sp.reserved[0] > 0
    assert sp.settled == [("r1", pytest.approx((10 * 0.10 + 20 * 0.50) / 1e6))]
    rec = json.loads((tmp_path / "calls.jsonl").read_text())
    assert rec["cost_source"] == "usage" and rec["status"] == 200


def test_refused_reservation_never_forwards(tmp_path, upstream):
    port, seen = upstream
    p = K.KeyProxy(key="k", spend=FakeSpend(refuse=True), log_path=tmp_path / "c.jsonl",
                   upstream=("127.0.0.1", port, False)).start()
    try:
        status, body = _post(p.port, "/v1/messages",
                             json.dumps({"model": K.MODEL, "max_tokens": 10}))
    finally:
        p.stop()
    assert status == 400 and b"spend cap" in body and seen == []
    assert "refused" in json.loads((tmp_path / "c.jsonl").read_text())


def test_unpriced_paths_are_forwarded_without_a_reservation(tmp_path, upstream):
    port, seen = upstream
    sp = FakeSpend()
    p = K.KeyProxy(key="k", spend=sp, log_path=tmp_path / "c.jsonl",
                   upstream=("127.0.0.1", port, False)).start()
    try:
        status, _ = _post(p.port, "/v1/messages/count_tokens", json.dumps({}))
    finally:
        p.stop()
    assert status == 200 and len(seen) == 1 and sp.reserved == [] and sp.settled == []


def test_read_key(tmp_path):
    f = tmp_path / ".env"
    f.write_text('OTHER=1\nANTHROPIC_API_KEY="abc"\n')
    assert K.read_key(f) == "abc"
    with pytest.raises(KeyError):
        K.read_key(f, "MISSING")


def test_allowlist_refuses_other_endpoints_and_models(tmp_path, upstream):
    port, seen = upstream
    sp = FakeSpend()
    p = K.KeyProxy(key="k", spend=sp, log_path=tmp_path / "c.jsonl",
                   upstream=("127.0.0.1", port, False)).start()
    try:
        s1, _ = _post(p.port, "/v1/messages/batches", json.dumps({"requests": []}))
        s2, _ = _post(p.port, "/v1/messages", json.dumps({"model": "claude-opus-5-5",
                                                          "max_tokens": 10}))
        s3, _ = _post(p.port, "/v1/messages", "not json")
    finally:
        p.stop()
    assert (s1, s2, s3) == (403, 403, 403) and seen == [] and sp.reserved == []


def test_call_and_time_caps_refuse_before_forwarding(tmp_path, upstream):
    import time
    port, seen = upstream
    p = K.KeyProxy(key="k", spend=FakeSpend(), log_path=tmp_path / "c.jsonl",
                   upstream=("127.0.0.1", port, False), max_calls=1).start()
    body = json.dumps({"model": K.MODEL, "max_tokens": 10})
    try:
        ok, _ = _post(p.port, "/v1/messages", body)
        capped, msg = _post(p.port, "/v1/messages", body)
        p.max_calls, p.deadline = None, time.monotonic() - 1
        late, msg2 = _post(p.port, "/v1/messages", body)
    finally:
        p.stop()
    assert ok == 200 and capped == 400 and b"call cap" in msg
    assert late == 400 and b"time cap" in msg2 and len(seen) == 1
    assert p.system is not None and p.system["chars"] == 0


def test_upstream_down_answers_502_and_settles_at_the_bound(tmp_path):
    sp = FakeSpend()
    p = K.KeyProxy(key="k", spend=sp, log_path=tmp_path / "c.jsonl",
                   upstream=("127.0.0.1", 9, False), timeout_s=5).start()
    try:
        status, _ = _post(p.port, "/v1/messages", json.dumps({"model": K.MODEL,
                                                             "max_tokens": 10}))
    finally:
        left = p.stop()
    assert status == 502 and left == 0
    assert sp.settled == [("r1", sp.reserved[0])]
    assert json.loads((tmp_path / "c.jsonl").read_text())["cost_source"] == "bound"


def test_unbounded_content_is_refused():
    ok = {"model": K.MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "tool_result", "tool_use_id": "t", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "AAAA"}}]}]}],
          "tools": [{"name": "Bash", "input_schema": {}}]}
    assert K.unbounded(ok) is None
    url_doc = {"messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "url", "url": "https://x/y.pdf"}}]}]}
    assert "url" in K.unbounded(url_doc)
    nested_file = {"messages": [{"role": "user", "content": [{"type": "tool_result", "content": [
        {"type": "image", "source": {"type": "file", "file_id": "f1"}}]}]}]}
    assert "file" in K.unbounded(nested_file)
    assert "server tool" in K.unbounded({"messages": [], "tools": [
        {"type": "web_search_20260209", "name": "web_search"}]})
    assert K.unbounded({"messages": [], "mcp_servers": [{"url": "x"}]}) == "mcp_servers"


def test_unbounded_walks_every_nested_value():
    doc = {"messages": [{"role": "user", "content": [{"type": "document", "source": {
        "type": "content", "content": [{"type": "image", "source": {"type": "url",
                                                                   "url": "https://x/i.png"}}]}}]}]}
    assert "url" in K.unbounded(doc)


# --- the OpenRouter route (DeepSeek V4.1 Flash pinned to Together) ---------------------------

def _sse(usage_delta: dict) -> bytes:
    return (b'event: message_start\ndata: {"type":"message_start","message":{"id":"gen-1",'
            b'"model":"deepseek/deepseek-v4.1-flash","usage":{"input_tokens":50,'
            b'"output_tokens":1}}}\n\n'
            b'data: ' + json.dumps({"type": "message_delta", "usage": usage_delta}).encode()
            + b'\n\ndata: {"type":"message_stop"}\n\n')


@pytest.fixture
def scripted():
    """A fake upstream that answers each POST with the next scripted (status, body) (the last
    one repeats), and records what it was sent."""
    seen, script = [], []

    class U(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            seen.append({"path": self.path, "key": self.headers.get("x-api-key"),
                         "auth": self.headers.get("authorization"), "body": body,
                         "content_length": int(self.headers["content-length"])})
            status, out = script[min(len(seen), len(script)) - 1]
            self.send_response(status)
            self.send_header("content-length", str(len(out)))
            self.send_header("x-generation-id", f"gen-{len(seen)}")
            self.end_headers()
            self.wfile.write(out)

    class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = S(("127.0.0.1", 0), U)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], seen, script
    srv.shutdown()


FAST_OR = __import__("dataclasses").replace(K.OPENROUTER_DS, backoff_s=(0.01,))
DS_REQ = {"model": K.OPENROUTER_DS.model, "max_tokens": 1000, "stream": True,
          "system": [{"type": "text", "text": "You are Claude Code. é"}],
          "messages": [{"role": "user", "content": "hi"}], "temperature": 1.0}
RATE = json.dumps({"error": {"code": 429, "message": "rate_limit_exceeded"}}).encode()


def _or_proxy(tmp_path, port, sp, route=FAST_OR, **kw):
    return K.KeyProxy(key="or-key", spend=sp, log_path=tmp_path / "calls.jsonl", route=route,
                      upstream=("127.0.0.1", port, False), **kw).start()


def _recs(tmp_path):
    return [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_openrouter_forwards_bearer_rewritten_path_and_pin_then_settles_at_usage_cost(
        tmp_path, scripted):
    port, seen, script = scripted
    script.append((200, _sse({"output_tokens": 30, "cost": 0.0692955})))
    sp = FakeSpend()
    p = _or_proxy(tmp_path, port, sp)
    assert p.model == "deepseek/deepseek-v4.1-flash"
    try:
        status, body = _post(p.port, "/v1/messages?beta=true", json.dumps(DS_REQ),
                             {"x-api-key": "dummy", "anthropic-version": "2023-06-01"})
    finally:
        p.stop()
    assert status == 200 and b"message_stop" in body
    s = seen[0]
    assert s["auth"] == "Bearer or-key" and s["key"] is None
    assert s["path"] == "/api/v1/messages?beta=true"
    fwd = json.loads(s["body"])
    assert fwd == DS_REQ | {"provider": {"order": ["Together"], "allow_fallbacks": False}}
    assert s["content_length"] == len(s["body"])
    rec = _recs(tmp_path)[0]
    # the bound counts the forwarded bytes (pin included) at $0.30/M in, $1.20/M out
    assert rec["fwd_bytes"] == len(s["body"])
    assert sp.reserved == [K.bound_usd(len(s["body"]), 1000, 0.30, 1.20)]
    assert sp.settled == [("r1", pytest.approx(0.0692955))]
    assert rec["cost_source"] == "usage.cost" and rec["cost_usd"] == round(0.0692955, 6)
    assert rec["generation_id"] == "gen-1" and "retries" not in rec
    assert rec["provider_pin"] == {"order": ["Together"], "allow_fallbacks": False}
    assert p.system is not None and p.system["chars"] > 0


def test_openrouter_reply_without_cost_settles_at_the_bound(tmp_path, scripted):
    port, _, script = scripted
    script.append((200, _sse({"output_tokens": 30})))
    sp = FakeSpend()
    p = _or_proxy(tmp_path, port, sp)
    try:
        status, _ = _post(p.port, "/v1/messages", json.dumps(DS_REQ))
    finally:
        p.stop()
    assert status == 200 and sp.settled == [("r1", sp.reserved[0])]
    assert _recs(tmp_path)[0]["cost_source"] == "bound"


def test_openrouter_cost_reads_usage_cost_and_byok_upstream():
    assert K.openrouter_cost({"cost": 0.01}) == 0.01
    assert K.openrouter_cost({"input_tokens": 3}) is None
    assert K.openrouter_cost({"cost": True}) is None
    assert K.openrouter_cost({"cost": 0.001, "is_byok": True,
                              "cost_details": {"upstream_inference_cost": 0.02}}) == 0.021
    # not BYOK: `cost` is the whole charge
    assert K.openrouter_cost({"cost": 0.02, "cost_details": {
        "upstream_inference_cost": 0.02}}) == 0.02


def test_openrouter_refuses_claude_models_and_count_tokens(tmp_path, scripted):
    port, seen, script = scripted
    script.append((200, _sse({"cost": 0.0})))
    sp = FakeSpend()
    p = _or_proxy(tmp_path, port, sp)
    try:
        s1, _ = _post(p.port, "/v1/messages", json.dumps(DS_REQ | {"model": K.MODEL}))
        s2, _ = _post(p.port, "/v1/messages", json.dumps(DS_REQ | {"model": "claude-opus-5-5"}))
        s3, _ = _post(p.port, "/v1/messages/count_tokens", json.dumps(DS_REQ))
    finally:
        p.stop()
    assert (s1, s2, s3) == (403, 403, 403) and seen == [] and sp.reserved == []


def test_openrouter_retries_a_429_then_settles_once(tmp_path, scripted):
    port, seen, script = scripted
    script += [(429, RATE), (200, _sse({"output_tokens": 30, "cost": 0.002}))]
    sp = FakeSpend()
    p = _or_proxy(tmp_path, port, sp)
    try:
        status, _ = _post(p.port, "/v1/messages", json.dumps(DS_REQ))
    finally:
        p.stop()
    assert status == 200 and len(seen) == 2
    assert len(sp.reserved) == 1 and sp.settled == [("r1", pytest.approx(0.002))]
    rec = _recs(tmp_path)[0]
    assert rec["retries"] == [429] and rec["generation_id"] == "gen-2"
    assert rec["cost_source"] == "usage.cost"


def test_openrouter_gives_up_after_three_retries_and_settles_at_the_bound(tmp_path, scripted):
    """No completed reply: the last 429 goes to the client as it came, settled at the bound
    (today's rule for any failure)."""
    port, seen, script = scripted
    script += [(429, RATE), (529, RATE), (503, RATE), (429, RATE), (200, _sse({"cost": 0.1}))]
    sp = FakeSpend()
    p = _or_proxy(tmp_path, port, sp)
    try:
        status, body = _post(p.port, "/v1/messages", json.dumps(DS_REQ))
    finally:
        p.stop()
    assert status == 429 and body == RATE and len(seen) == 4
    assert len(sp.reserved) == 1 and sp.settled == [("r1", sp.reserved[0])]
    rec = _recs(tmp_path)[0]
    assert rec["retries"] == [429, 529, 503] and rec["status"] == 429
    assert rec["cost_source"] == "bound"


def test_openrouter_retries_stop_at_the_deadline(tmp_path, scripted):
    import time
    port, seen, script = scripted
    script += [(429, RATE), (200, _sse({"cost": 0.1}))]
    sp = FakeSpend()
    # the real backoff (10 s) would end past the deadline: no retry, the 429 is returned
    p = _or_proxy(tmp_path, port, sp, route=K.OPENROUTER_DS, deadline=time.monotonic() + 5)
    t0 = time.monotonic()
    try:
        status, _ = _post(p.port, "/v1/messages", json.dumps(DS_REQ))
    finally:
        p.stop()
    assert status == 429 and len(seen) == 1 and time.monotonic() - t0 < 5
    rec = _recs(tmp_path)[0]
    assert rec["retry_stopped"] == "deadline" and "retries" not in rec
    assert sp.settled == [("r1", sp.reserved[0])]


def test_anthropic_route_never_retries(tmp_path, scripted):
    port, seen, script = scripted
    script += [(429, RATE), (200, b"{}")]
    sp = FakeSpend()
    p = K.KeyProxy(key="k", spend=sp, log_path=tmp_path / "calls.jsonl",
                   upstream=("127.0.0.1", port, False)).start()
    try:
        status, _ = _post(p.port, "/v1/messages", json.dumps({"model": K.MODEL,
                                                             "max_tokens": 10}))
    finally:
        p.stop()
    assert status == 429 and len(seen) == 1 and seen[0]["path"] == "/v1/messages"
    assert seen[0]["key"] == "k" and "provider_pin" not in _recs(tmp_path)[0]


def test_a_backoff_that_crosses_the_deadline_sends_nothing_more(tmp_path, scripted, monkeypatch):
    """astra ds-build r1 m5: the deadline is checked again after the backoff (time the retry
    decision couldn't see, e.g. a slow upstream): the CLI gets the time-cap refusal, no second
    request is sent, and the one reservation settles at its bound."""
    import time
    port, seen, script = scripted
    script += [(429, RATE), (200, _sse({"cost": 0.1}))]
    sp = FakeSpend()
    p = _or_proxy(tmp_path, port, sp, deadline=time.monotonic() + 3600)
    monkeypatch.setattr(K.time, "sleep", lambda s: setattr(p, "deadline", time.monotonic() - 1))
    try:
        status, body = _post(p.port, "/v1/messages", json.dumps(DS_REQ))
    finally:
        p.stop()
    assert status == 400 and b"emx time cap reached" in body and len(seen) == 1
    rec = _recs(tmp_path)[0]
    assert rec["retries"] == [429] and rec["retry_stopped"] == "deadline"
    assert "error" not in rec and sp.settled == [("r1", sp.reserved[0])]
