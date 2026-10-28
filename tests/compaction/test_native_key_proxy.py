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
