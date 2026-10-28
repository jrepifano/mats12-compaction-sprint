"""The API key proxy of a native Claude Code run (week-2 harness replication, plan v0.3.1).

Claude Code in the task container talks to this proxy (`ANTHROPIC_BASE_URL`, reached through
the egress sidecar's raw forward) with a placeholder key; the proxy listens on the host's
loopback and alone holds the real key, so neither the agent nor its container ever sees it.

- **Allowlist** (astra cc-build r1 B1): only `POST /v1/messages` (optionally `?beta=true`) for
  the pinned `MODEL`, and the free `/v1/messages/count_tokens`. Anything else answers 403 and
  is never forwarded. So is any content the byte bound can't cover (`unbounded`; r2 B1): a
  content source by URL or file id (documents, images), and Anthropic's server-side tools
  (web search and fetch, code execution), whose usage is billed beyond the request's bytes.
- **Admission, before forwarding:** the task's call cap (`max_calls`) and deadline
  (`deadline`, a `time.monotonic()` value) are checked, then the request's bound (`bound_usd`:
  every body byte as a prompt token at the dearest input price, plus every allowed output token,
  rounded up; B2) is reserved against the task's and the stage's caps (`model.RunSpend` over the
  stage's `Spend`). A refusal answers HTTP 400 `invalid_request_error` whose message names the
  cap ("emx call cap reached", "emx time cap reached", "emx spend cap reached"), and nothing is
  sent.
- **Settlement:** the reply is streamed back unchanged. Only a complete reply (an SSE stream
  with its `message_stop` event, or a JSON body with usage) is priced from its usage
  (`haiku_cost`); an interrupted, failed or unpriceable one is settled at its bound (B3).
- An upstream transport error before any byte reached the client answers 502 (m12). `stop()`
  waits for in-flight requests to settle before returning.
- Every request is one line of `calls.jsonl` (path, model, status, usage, cost, cost source,
  request id, refusals); the first priced request's system prompt is fingerprinted
  (`system_sha256`, its length and head) so the run records which prompt the CLI sent.

Prices: Claude Haiku 5.5, Anthropic pricing page read 28 Oct 2026. A request whose prompt
(input + cache writes + cache reads) is over 100,000 tokens is priced at the higher rates in
full.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import http.server
import json
import math
import socketserver
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODEL = "claude-haiku-5-5"
# $/MTok: (input, 5-minute cache write, 1-hour cache write, cache read, output)
PRICES = {"base": (0.10, 0.125, 0.20, 0.01, 0.50), "long": (0.50, 0.625, 1.00, 0.05, 2.50)}
LONG_ABOVE = 100_000
DROP = ("host", "content-length", "authorization", "x-api-key", "accept-encoding",
        "connection")
PRICED = "/v1/messages"
FREE = ("/v1/messages/count_tokens",)


def haiku_cost(usage: dict) -> float:
    """USD of one reply's `usage` (Anthropic Messages usage fields)."""
    inp = int(usage.get("input_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    cw = int(usage.get("cache_creation_input_tokens") or 0)
    det = usage.get("cache_creation") or {}
    cw_1h = int(det.get("ephemeral_1h_input_tokens") or 0)
    cw_5m = cw - cw_1h if det else cw
    out = int(usage.get("output_tokens") or 0)
    pin, p5, p1h, pread, pout = PRICES["long" if inp + cw + cr > LONG_ABOVE else "base"]
    return (inp * pin + cw_5m * p5 + cw_1h * p1h + cr * pread + out * pout) / 1e6


def bound_usd(nbytes: int, max_tokens: int) -> float:
    """An upper bound on one request's cost: every body byte as a prompt token (a token is never
    less than a byte of the request) at the dearest input price, plus every allowed output
    token; rounded up to $0.0001."""
    _, _, p1h, _, pout = PRICES["long"]
    usd = (nbytes * p1h + max(0, max_tokens) * pout) / 1e6
    return math.ceil(usd * 1e4) / 1e4


SERVER_TOOL = ("web_search", "web_fetch", "code_execution", "computer", "bash_", "text_editor",
               "memory", "mcp")


def unbounded(req: dict) -> str | None:
    """Why the byte bound can't cover this request (None: it can). Walks every content block,
    nested tool results included."""
    for tool in req.get("tools") or []:
        typ = str((tool or {}).get("type") or "")
        if typ and typ != "custom" and typ.startswith(SERVER_TOOL):
            return f"server tool {typ}"
    if req.get("mcp_servers"):
        return "mcp_servers"
    stack = [m.get("content") for m in req.get("messages") or [] if isinstance(m, dict)]
    stack.append(req.get("system"))
    while stack:
        c = stack.pop()
        if isinstance(c, list):
            stack.extend(c)
        elif isinstance(c, dict):
            src = c.get("source")
            if isinstance(src, dict) and src.get("type") in ("url", "file"):
                return f"{c.get('type')} source {src.get('type')}"
            if c.get("type") in ("server_tool_use", "web_search_tool_result",
                                 "web_fetch_tool_result", "container_upload"):
                return f"block {c.get('type')}"
            # every nested value, e.g. a document's `source.content` (astra cc-build r3 B1)
            stack.extend(v for v in c.values() if isinstance(v, (list, dict)))
    return None


def read_key(env_file: Path, var: str = "ANTHROPIC_API_KEY") -> str:
    for line in Path(env_file).read_text().splitlines():
        if line.startswith(f"{var}="):
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            if v:
                return v
    raise KeyError(f"{var} not set in {env_file}")


def usage_of(body: bytes) -> tuple[dict | None, str | None, str | None, bool]:
    """(usage, message id, model, complete) from a streamed (SSE) or JSON Messages reply. A
    stream is complete only with its `message_stop` event."""
    text = body.decode("utf-8", "replace")
    usage: dict = {}
    mid = model = None
    sse = stopped = False
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        sse = True
        if ev.get("type") == "message_start":
            m = ev.get("message") or {}
            mid, model = m.get("id"), m.get("model")
            usage.update(m.get("usage") or {})
        elif ev.get("type") == "message_delta":
            usage.update(ev.get("usage") or {})
        elif ev.get("type") == "message_stop":
            stopped = True
    if sse:
        return (usage or None), mid, model, stopped and bool(usage)
    try:
        j = json.loads(text)
    except ValueError:
        return None, None, None, False
    if not isinstance(j, dict) or not isinstance(j.get("usage"), dict):
        return None, None, None, False
    return j["usage"], j.get("id"), j.get("model"), True


def _refusal(msg: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                  "message": msg}}).encode()


@dataclass
class KeyProxy:
    key: str
    spend: Any  # model.Spend (RunSpend over the stage's Spend)
    log_path: Path
    note: str = "native claude"
    bind: str = "127.0.0.1"
    port: int = 0
    upstream: tuple[str, int, bool] = ("api.anthropic.com", 443, True)  # host, port, https
    timeout_s: float = 900.0
    model: str = MODEL
    max_calls: int | None = None
    deadline: float | None = None  # time.monotonic() value
    calls: int = 0
    system: dict | None = None
    _server: Any = None
    _thread: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _active: int = 0
    _idle: threading.Condition = field(default_factory=threading.Condition)

    def _log(self, rec: dict) -> None:
        rec["utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, open(self.log_path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def _conn(self):
        host, port, tls = self.upstream
        cls = http.client.HTTPSConnection if tls else http.client.HTTPConnection
        return cls(host, port, timeout=self.timeout_s)

    def _admit(self) -> str | None:
        """The cap this call would break (None: admitted, and counted)."""
        with self._lock:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                return "emx time cap reached"
            if self.max_calls is not None and self.calls >= self.max_calls:
                return "emx call cap reached"
            self.calls += 1
            return None

    def _fingerprint(self, req: dict) -> None:
        if self.system is not None:
            return
        s = req.get("system")
        text = "\n".join(b.get("text", "") for b in s if isinstance(b, dict)) \
            if isinstance(s, list) else (s or "")
        # the CLI's per-session billing header line (its cc_version suffix varies) is left out
        # of the fingerprint, so equal prompts hash equal across sessions (smoke, 28 Oct)
        body = "\n".join(x for x in text.split("\n")
                         if not x.startswith("x-anthropic-billing-header:"))
        self.system = {"sha256": hashlib.sha256(body.encode()).hexdigest(), "chars": len(text),
                       "head": body[:200]}

    def handler(self):
        proxy = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self._reply(404, b"")

            def _reply(self, status: int, body: bytes):
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                with proxy._idle:
                    proxy._active += 1
                try:
                    self._post()
                finally:
                    with proxy._idle:
                        proxy._active -= 1
                        proxy._idle.notify_all()

            def _post(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                path = self.path.split("?")[0]
                rec: dict = {"path": self.path, "bytes": len(body)}
                try:
                    req = json.loads(body)
                except ValueError:
                    req = None
                why = None
                if not isinstance(req, dict) or (path != PRICED and path not in FREE):
                    why = "endpoint"
                elif path == PRICED and req.get("model") != proxy.model:
                    why = f"model {req.get('model')}"
                elif path == PRICED:
                    why = unbounded(req)
                if why is not None:
                    rec |= {"status": 403, "refused": f"not allowlisted: {why}"}
                    proxy._log(rec)
                    self._reply(403, _refusal("emx proxy: endpoint or model not allowed"))
                    return
                rid = None
                if path == PRICED:
                    proxy._fingerprint(req)
                    rec["model"] = req.get("model")
                    rec["bound_usd"] = bound_usd(len(body), int(req.get("max_tokens") or 0))
                    cap = proxy._admit()
                    if cap is None:
                        try:
                            rid = proxy.spend.reserve(rec["bound_usd"], proxy.note)
                        except Exception as e:  # noqa: BLE001 - any refusal: nothing is sent
                            cap = "emx spend cap reached"
                            rec["refused_detail"] = f"{type(e).__name__}: {e}"[:300]
                    if cap is not None:
                        rec |= {"status": 400, "refused": cap}
                        proxy._log(rec)
                        self._reply(400, _refusal(cap))
                        return
                hdr = {k: v for k, v in self.headers.items() if k.lower() not in DROP}
                hdr |= {"x-api-key": proxy.key, "Content-Length": str(len(body))}
                buf, sent, c = b"", False, None
                try:
                    c = proxy._conn()
                    c.request("POST", self.path, body=body, headers=hdr)
                    r = c.getresponse()
                    rec["status"] = r.status
                    rec["request_id"] = r.getheader("request-id")
                    self.send_response(r.status)
                    for k, v in r.getheaders():
                        if k.lower() not in ("transfer-encoding", "content-length", "connection",
                                             "content-encoding"):
                            self.send_header(k, v)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    sent = True
                    t_read = time.monotonic()
                    while chunk := r.read1(65536):
                        # a stream's whole duration is bounded, not just each read (r3 m12)
                        if time.monotonic() - t_read > proxy.timeout_s:
                            raise TimeoutError(f"stream longer than {proxy.timeout_s}s")
                        buf += chunk
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except Exception as e:  # noqa: BLE001 - recorded; the reservation is settled below
                    rec["error"] = f"{type(e).__name__}: {e}"[:300]
                    if not sent:
                        with contextlib.suppress(Exception):
                            self._reply(502, _refusal("emx proxy: upstream unreachable"))
                    else:
                        self.close_connection = True
                finally:
                    if c is not None:
                        with contextlib.suppress(Exception):
                            c.close()
                    if rid is not None:
                        usage, mid, model, complete = usage_of(buf) if buf else (None,) * 3 + (
                            False,)
                        ok = complete and rec.get("status") == 200 and "error" not in rec
                        cost = haiku_cost(usage) if ok else rec["bound_usd"]
                        rec |= {"usage": usage, "id": mid, "served_model": model,
                                "cost_usd": round(cost, 6),
                                "cost_source": "usage" if ok else "bound"}
                        proxy.spend.settle(rid, cost, resource=f"anthropic-api:{proxy.model}",
                                           units=1, note=proxy.note)
                    proxy._log(rec)

        return H

    def start(self) -> KeyProxy:
        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._server = S((self.bind, self.port), self.handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self, wait_s: float | None = None) -> int:
        """Stop accepting, then wait for in-flight requests to settle (by default through the
        upstream timeout, so every reservation is settled before results are written; r2 m12);
        returns how many were still running."""
        # one read can block up to timeout_s after the stream's own limit: 2x plus margin
        wait_s = 2 * self.timeout_s + 120 if wait_s is None else wait_s
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        t0 = time.monotonic()
        with self._idle:
            while self._active and time.monotonic() - t0 < wait_s:
                self._idle.wait(timeout=1.0)
            return self._active

