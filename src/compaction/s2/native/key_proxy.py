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

**Routes** (`Route`; `KeyProxy.route`, default `ANTHROPIC`). Everything upstream-specific is
read from the route: the one allowed model, host and messages path, the free paths, the auth
header, extra body fields, the bound's prices, how a complete reply is priced, and 429 retries.
- `ANTHROPIC`: the behaviour above, unchanged. Original body bytes are forwarded as they came.
- `ANTHROPIC_SONNET`: Sonnet 5.5, priced from MODEL_PRICES; its bound uses $4/M input
  (one-hour cache writes) and $10/M output. Unknown models are refused before forwarding.
- `OPENROUTER_DS` (Jacob, 28 Oct: "DeepSeek inside Claude Code"): DeepSeek V4.1 Flash through
  OpenRouter's Anthropic-format endpoint (`/api/v1/messages`, `Authorization: Bearer`), pinned
  to Together (`provider: {order: [Together], allow_fallbacks: false}` merged into the body,
  which is then re-serialised; the bound counts the forwarded bytes). No free paths: a
  count_tokens call is refused like any other. A complete reply settles at OpenRouter's own
  `usage.cost` (plus the upstream charge on a BYOK request); without it, at the bound. An
  upstream 429/503/529 before any byte reached the client is retried up to 3 times (backoff
  10, 20, 40 s, never past `deadline`; Together 429s under bursts, 28 Oct), on the one
  reservation; the statuses retried are logged (`retries`). When retries run out the last
  upstream reply goes to the client as it came, and is settled at the bound (no completed
  reply, as for any failure). The upstream `X-Generation-Id` (`generation_id`) and the pin
  (`provider_pin`) are logged so the serving provider can be checked afterwards.

Prices: Claude Haiku 5.5, Anthropic pricing page read 28 Oct 2026. A request whose prompt
(input + cache writes + cache reads) is over 100,000 tokens is priced at the higher rates in
full. DeepSeek V4.1 Flash on Together: $0.30/M input, $1.20/M output, $0.006/M cache read
(compaction.s2.config's "deepseek" table, the same rates).
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from compaction.s2.model import StageCapReached

MODEL = "claude-haiku-5-5"
# $/MTok: (input, 5-minute cache write, 1-hour cache write, cache read, output)
PRICES = {"base": (0.10, 0.125, 0.20, 0.01, 0.50), "long": (0.50, 0.625, 1.00, 0.05, 2.50)}
# Keep PRICES as the legacy Haiku view; all new Anthropic routes use this model-keyed table.
SONNET_MODEL = "claude-sonnet-5-5"
MODEL_PRICES = {MODEL: PRICES, SONNET_MODEL: {
    "base": (2.0, 2.50, 4.0, 0.20, 10.0), "long": (2.0, 2.50, 4.0, 0.20, 10.0)}}
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


def bound_usd(nbytes: int, max_tokens: int, in_per_mtok: float | None = None,
              out_per_mtok: float | None = None) -> float:
    """An upper bound on one request's cost: every body byte as a prompt token (a token is never
    less than a byte of the request) at the dearest input price, plus every allowed output
    token; rounded up to $0.0001. Prices default to Haiku's dearest ($/MTok)."""
    _, _, p1h, _, pout = PRICES["long"]
    pin = p1h if in_per_mtok is None else in_per_mtok
    pout = pout if out_per_mtok is None else out_per_mtok
    usd = (nbytes * pin + max(0, max_tokens) * pout) / 1e6
    return math.ceil(usd * 1e4) / 1e4


def sonnet_cost(usage: dict) -> float:
    """Sonnet 5.5 rates from the chain5 v0.4 brief, including both cache-write TTLs."""
    inp = int(usage.get("input_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    cw = int(usage.get("cache_creation_input_tokens") or 0)
    cw_1h = int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    pin, p5, p1h, pread, pout = MODEL_PRICES[SONNET_MODEL]["base"]
    return (inp * pin + max(0, cw - cw_1h) * p5 + cw_1h * p1h + cr * pread + out * pout) / 1e6


def openrouter_cost(usage: dict) -> float | None:
    """USD of one reply from OpenRouter's own `usage.cost` (None when it is missing), plus the
    upstream charge of a BYOK request (`cost_details.upstream_inference_cost`)."""
    c = usage.get("cost")
    if isinstance(c, bool) or not isinstance(c, (int, float)):
        return None
    up = (usage.get("cost_details") or {}).get("upstream_inference_cost")
    if usage.get("is_byok") and isinstance(up, (int, float)) and not isinstance(up, bool):
        return float(c) + float(up)
    return float(c)


RETRY_STATUS = (429, 503, 529)


@dataclass(frozen=True)
class Route:
    """One upstream: what is allowed through, where it goes, and how it is priced."""

    name: str
    model: str  # the only request model allowed
    host: str
    port: int
    tls: bool
    path: str  # the upstream messages path
    in_per_mtok: float  # the bound's (dearest) input price, $/MTok
    out_per_mtok: float  # the bound's output price, $/MTok
    cost: Callable[[dict], float | None]  # a complete reply's USD from its usage (None: unknown)
    cost_source: str  # logged when `cost` priced the reply
    resource: str  # ledger resource prefix (":<model>" is appended)
    free: tuple[str, ...] = ()  # forwarded unpriced, as they came
    auth: str = "x-api-key"  # or "bearer"
    body_extra: dict = field(default_factory=dict)  # merged into every priced request's body
    retry_429: int = 0  # retries of an upstream 429/503/529 before any byte reached the client
    backoff_s: tuple[float, ...] = ()  # wait before retry n (the last repeats)

    def bound(self, nbytes: int, max_tokens: int) -> float:
        return bound_usd(nbytes, max_tokens, self.in_per_mtok, self.out_per_mtok)


ANTHROPIC = Route(name="anthropic", model=MODEL, host="api.anthropic.com", port=443, tls=True,
                  path=PRICED, in_per_mtok=PRICES["long"][2], out_per_mtok=PRICES["long"][4],
                  cost=haiku_cost, cost_source="usage", resource="anthropic-api", free=FREE)
ANTHROPIC_SONNET = Route(
    name="anthropic-sonnet", model=SONNET_MODEL, host="api.anthropic.com", port=443, tls=True,
    path=PRICED, in_per_mtok=MODEL_PRICES[SONNET_MODEL]["long"][2],
    out_per_mtok=MODEL_PRICES[SONNET_MODEL]["long"][4], cost=sonnet_cost,
    cost_source="usage", resource="anthropic-api", free=FREE)


def anthropic_route(model: str) -> Route:
    """A priced route, refusing unknown models; [1m] is a CLI-only Haiku suffix."""
    if model in (MODEL, MODEL + "[1m]"):
        return ANTHROPIC
    if model == SONNET_MODEL:
        return ANTHROPIC_SONNET
    raise ValueError(f"unpriced Anthropic model: {model}")


OPENROUTER_DS = Route(name="openrouter-ds", model="deepseek/deepseek-v4.1-flash",
                      host="openrouter.ai", port=443, tls=True, path="/api/v1/messages",
                      in_per_mtok=0.30, out_per_mtok=1.20, cost=openrouter_cost,
                      cost_source="usage.cost", resource="openrouter", auth="bearer",
                      body_extra={"provider": {"order": ["Together"], "allow_fallbacks": False}},
                      retry_429=3, backoff_s=(10.0, 20.0, 40.0))


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


class _DeadlineStop(Exception):
    """A retry's backoff reached the deadline: the time-cap refusal has been sent."""


@dataclass
class KeyProxy:
    key: str
    spend: Any  # model.Spend (RunSpend over the stage's Spend)
    log_path: Path
    note: str = "native claude"
    bind: str = "127.0.0.1"
    port: int = 0
    route: Route = ANTHROPIC
    upstream: tuple[str, int, bool] | None = None  # host, port, https (default: the route's)
    timeout_s: float = 900.0
    model: str | None = None  # default: the route's
    max_calls: int | None = None
    deadline: float | None = None  # time.monotonic() value
    calls: int = 0
    stage_cap_reached: bool = False
    system: dict | None = None
    _server: Any = None
    _thread: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _active: int = 0
    _idle: threading.Condition = field(default_factory=threading.Condition)

    def __post_init__(self) -> None:
        if self.route is ANTHROPIC and self.model == SONNET_MODEL:
            self.route = ANTHROPIC_SONNET
        if self.upstream is None:
            self.upstream = (self.route.host, self.route.port, self.route.tls)
        if self.model is None:
            self.model = self.route.model

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

    def _retry_wait(self, status: int, n: int, rec: dict) -> float | None:
        """Seconds to wait before retrying an upstream reply of `status` after `n` retries (None:
        pass it to the client). Never waits past the deadline."""
        if status not in RETRY_STATUS or n >= self.route.retry_429:
            return None
        b = self.route.backoff_s
        wait = b[min(n, len(b) - 1)] if b else 0.0
        if self.deadline is not None and time.monotonic() + wait >= self.deadline:
            rec["retry_stopped"] = "deadline"
            return None
        return wait

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
                route = proxy.route
                why = None
                if not isinstance(req, dict) or (path != PRICED and path not in route.free):
                    why = "endpoint"
                elif proxy.model != route.model:
                    why = f"unpriced model {proxy.model} for route {route.name}"
                elif path in route.free and route is ANTHROPIC_SONNET and not req.get("model"):
                    why = "missing count_tokens model"
                elif path in route.free and req.get("model") not in (None, route.model):
                    why = f"unpriced count_tokens model {req.get('model')}"
                elif path == PRICED and req.get("model") != proxy.model:
                    why = f"model {req.get('model')}"
                elif path == PRICED:
                    why = unbounded(req)
                if why is not None:
                    rec |= {"status": 403, "refused": f"not allowlisted: {why}"}
                    proxy._log(rec)
                    self._reply(403, _refusal("emx proxy: endpoint or model not allowed"))
                    return
                rid, fwd, upath = None, body, self.path
                if path == PRICED:
                    upath = route.path + self.path[len(path):]  # the query string kept
                    if route.body_extra:
                        fwd = json.dumps(req | route.body_extra).encode()
                        rec["fwd_bytes"] = len(fwd)
                        if "provider" in route.body_extra:
                            rec["provider_pin"] = route.body_extra["provider"]
                    proxy._fingerprint(req)
                    rec["model"] = req.get("model")
                    # every forwarded byte is bounded, the merged fields included
                    rec["bound_usd"] = route.bound(len(fwd), int(req.get("max_tokens") or 0))
                    cap = proxy._admit()
                    if cap is None:
                        try:
                            rid = proxy.spend.reserve(rec["bound_usd"], proxy.note)
                        except Exception as e:  # noqa: BLE001 - any refusal: nothing is sent
                            cap = "emx spend cap reached"
                            if isinstance(e, StageCapReached):
                                proxy.stage_cap_reached = True
                                rec["stage_cap_reached"] = True
                            rec["refused_detail"] = f"{type(e).__name__}: {e}"[:300]
                    if cap is not None:
                        rec |= {"status": 400, "refused": cap}
                        proxy._log(rec)
                        self._reply(400, _refusal(cap))
                        return
                hdr = {k: v for k, v in self.headers.items() if k.lower() not in DROP}
                hdr |= ({"Authorization": f"Bearer {proxy.key}"} if route.auth == "bearer"
                        else {"x-api-key": proxy.key})
                hdr["Content-Length"] = str(len(fwd))
                buf, sent, c = b"", False, None
                try:
                    retries: list[int] = []
                    while True:
                        c = proxy._conn()
                        c.request("POST", upath, body=fwd, headers=hdr)
                        r = c.getresponse()
                        wait = proxy._retry_wait(r.status, len(retries), rec)
                        if wait is None:
                            break
                        # nothing sent to the client yet; the one reservation is kept. The
                        # discarded body isn't drained (a slow body could outlast the deadline),
                        # and the deadline is checked again after the backoff (ds-build r1 m5)
                        c.close()
                        c = None
                        retries.append(r.status)
                        rec["retries"] = retries
                        time.sleep(wait)
                        if proxy.deadline is not None and time.monotonic() >= proxy.deadline:
                            rec |= {"status": 400, "refused": "emx time cap reached",
                                    "retry_stopped": "deadline"}
                            self._reply(400, _refusal("emx time cap reached"))
                            sent = True
                            raise _DeadlineStop
                    rec["status"] = r.status
                    rec["request_id"] = r.getheader("request-id")
                    if gid := r.getheader("x-generation-id"):
                        rec["generation_id"] = gid
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
                except _DeadlineStop:  # the refusal is sent; settled at the bound below
                    pass
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
                        cost = route.cost(usage) if ok else None
                        source = route.cost_source if cost is not None else "bound"
                        cost = rec["bound_usd"] if cost is None else cost
                        rec |= {"usage": usage, "id": mid, "served_model": model,
                                "cost_usd": round(cost, 6), "cost_source": source}
                        proxy.spend.settle(rid, cost, resource=f"{route.resource}:{proxy.model}",
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

