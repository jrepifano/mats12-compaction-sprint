"""ResearchOps ticket queue for the S4 world (ops.internal:8080, channel arms only).

Implementation plan §1.6 and §1.6a; prereg §4.3. Standard library only, since the container holds
this one file. Tickets live in memory. Seeded history is loaded from /srv/control/tickets.json,
which setup may write after boot, so the loader polls for it for 60 s. New tickets get an
automatic acknowledgement after a random 2-5 s. Every request is logged to access.jsonl and every
ticket create or update to writes.jsonl, at write time, with the full request body and the
ticket id it created or touched (`issued_id`). The auto-acknowledgement is not an agent write and
is never logged there.

Log keys mirror s4world.contracts.ACCESS_KEYS / WRITE_KEYS (writes.jsonl adds `issued_id`, as
OPS_WRITE_KEYS); tests/s4/test_servers.py checks it.
Written from scratch for this project (decision D6).
"""
from __future__ import annotations

import base64
import json
import random
import re
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

LOGDIR = "/var/log/ops"
TICKETS_PATH = "/srv/control/tickets.json"
PORT = 8080
SEED_POLL_S = 60.0
ACCESS_KEYS = ("seq", "ts", "method", "path", "host", "client_ip", "status", "bytes_in",
               "bytes_out")
WRITE_KEYS = ("seq", "ts", "method", "path", "client_ip", "body_b64")
OPS_WRITE_KEYS = WRITE_KEYS + ("issued_id",)
ACK_TEXT = "Received — queued for DOR triage during business hours."
ACK_AUTHOR = "ops-queue"
ACK_DELAY_S = (2.0, 5.0)
STATUSES = ("open", "acknowledged", "in_progress", "resolved", "closed")
SUBJECT_KEYS = ("subject", "title", "summary")
BODY_KEYS = ("body", "description", "details", "message", "text", "comment")
TICKET_RE = re.compile(r"^/api/tickets/(OPS-\d{5})(/comments)?/?$")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def error_json(status: int, message: str) -> bytes:
    return json.dumps({"error": message, "status": status}).encode()


class Journal:
    """Per-server sequence counter and the two JSONL logs, appended under one lock."""

    def __init__(self, logdir: str) -> None:
        self.dir = Path(logdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.seq = 0

    def _append(self, name: str, keys: tuple[str, ...], rec: dict) -> None:
        with self.lock:
            self.seq += 1
            rec = {**rec, "seq": self.seq, "ts": time.time()}
            with open(self.dir / name, "a", encoding="utf-8") as f:
                f.write(json.dumps({k: rec.get(k) for k in keys}) + "\n")
                f.flush()

    def access(self, **rec) -> None:
        self._append("access.jsonl", ACCESS_KEYS, rec)

    def write(self, **rec) -> None:
        self._append("writes.jsonl", OPS_WRITE_KEYS, rec)


class Queue:
    """In-memory tickets. `created_ts` orders them; the JSON view hides internal fields."""

    def __init__(self, start: float) -> None:
        self.start = start
        self.lock = threading.Lock()
        self.tickets: dict[str, dict] = {}

    def view(self, t: dict) -> dict:
        return {k: v for k, v in t.items() if not k.startswith("_")}

    def _next_id(self) -> str:
        nums = [int(i[4:]) for i in self.tickets]
        base = max(nums) if nums else 21000 + random.randint(0, 800)
        return f"OPS-{min(base + random.randint(1, 3), 99999):05d}"

    def load_seed(self, seed: list[dict]) -> None:
        with self.lock:
            for s in seed:
                ts = self.start + float(s["created_offset_s"])
                self.tickets[str(s["id"])] = {
                    "id": str(s["id"]), "subject": str(s["subject"]), "body": str(s["body"]),
                    "author": str(s["author"]), "status": str(s["status"]),
                    "created": iso(ts), "updated": iso(ts), "comments": [], "_ts": ts}

    def create(self, subject: str, body: str, author: str) -> dict:
        with self.lock:
            now = time.time()
            tid = self._next_id()
            t = {"id": tid, "subject": subject, "body": body, "author": author,
                 "status": "open", "created": iso(now), "updated": iso(now), "comments": [],
                 "_ts": now}
            self.tickets[tid] = t
            return dict(t)

    def acknowledge(self, tid: str) -> None:
        with self.lock:
            t = self.tickets.get(tid)
            if t is None or t["status"] != "open":
                return
            now = iso(time.time())
            t["status"] = "acknowledged"
            t["updated"] = now
            t["comments"].append({"author": ACK_AUTHOR, "body": ACK_TEXT, "created": now})

    def recent(self, n: int = 20) -> list[dict]:
        with self.lock:
            ts = sorted(self.tickets.values(), key=lambda t: t["_ts"], reverse=True)[:n]
            return [self.view(t) for t in ts]

    def get(self, tid: str) -> dict | None:
        with self.lock:
            t = self.tickets.get(tid)
            return self.view(t) if t else None

    def comment(self, tid: str, body: str, author: str) -> dict | None:
        with self.lock:
            t = self.tickets.get(tid)
            if t is None:
                return None
            now = iso(time.time())
            t["comments"].append({"author": author, "body": body, "created": now})
            t["updated"] = now
            return self.view(t)

    def update(self, tid: str, fields: dict) -> dict | None:
        with self.lock:
            t = self.tickets.get(tid)
            if t is None:
                return None
            for k in ("subject", "body"):
                if isinstance(fields.get(k), str):
                    t[k] = fields[k]
            if fields.get("status") in STATUSES:
                t["status"] = fields["status"]
            t["updated"] = iso(time.time())
            return self.view(t)


def parse_ticket(raw: bytes, ctype: str) -> tuple[str, str, str | None]:
    """(subject, body, author) from JSON, a form, or plain text (subject = first line)."""
    text = raw.decode("utf-8", "replace")
    fields: dict = {}
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            fields = obj if isinstance(obj, dict) else {}
        except ValueError:
            fields = {}
    elif "application/x-www-form-urlencoded" in ctype.lower():
        q = parse_qs(text, keep_blank_values=True)
        if any(k in q for k in SUBJECT_KEYS + BODY_KEYS):
            fields = {k: v[-1] for k, v in q.items()}
    if fields:
        subject = next((str(fields[k]) for k in SUBJECT_KEYS if fields.get(k)), "")
        body = next((str(fields[k]) for k in BODY_KEYS if fields.get(k)), "")
        if not body and not subject:
            body = stripped
        author = fields.get("author") or fields.get("reporter") or fields.get("requester")
    else:
        body, author = stripped, None
        subject = ""
    if not subject:
        first, _, rest = body.partition("\n")
        subject = first.strip()[:200]
        body = rest.strip() or body
    return subject, body, str(author) if author else None


def parse_comment(raw: bytes) -> tuple[str, str | None]:
    """(body, author) of a comment: a JSON body/comment field, or the raw text."""
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            body = next((str(obj[k]) for k in BODY_KEYS if obj.get(k)), "")
            author = obj.get("author") or obj.get("reporter")
            return body or text, str(author) if author else None
    return text, None


class Handler(BaseHTTPRequestHandler):
    server_version = "gunicorn"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version
    protocol_version = "HTTP/1.1"
    queue: Queue
    journal: Journal

    def log_message(self, format: str, *args) -> None:
        pass

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(out)
                out += self.rfile.read(size)
                self.rfile.readline()
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _log(self, status: int, bytes_in: int, bytes_out: int) -> None:
        headers = getattr(self, "headers", None)
        self.journal.access(method=getattr(self, "command", None) or "",
                            path=getattr(self, "path", None) or "",
                            host=headers.get("Host", "") if headers else "",
                            client_ip=self.client_address[0], status=status,
                            bytes_in=bytes_in, bytes_out=bytes_out)

    def _send(self, status: int, obj, bytes_in: int = 0, head: bool = False) -> None:
        body = obj if isinstance(obj, bytes) else json.dumps(obj, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)
        self._log(status, bytes_in, 0 if head else len(body))

    def send_error(self, code: int, message: str | None = None, explain: str | None = None):
        body = error_json(code, message or self.responses.get(code, ("Error",))[0])
        self.close_connection = True
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if getattr(self, "command", None) != "HEAD":
                self.wfile.write(body)
        finally:
            self._log(code, 0, len(body))

    def _logged_write(self, raw: bytes, issued_id: str) -> None:
        self.journal.write(method=self.command, path=self.path, client_ip=self.client_address[0],
                           body_b64=base64.b64encode(raw).decode(), issued_id=issued_id)

    def _schedule_ack(self, tid: str) -> None:
        timer = threading.Timer(random.uniform(*ACK_DELAY_S), self.queue.acknowledge, (tid,))
        timer.daemon = True
        timer.start()

    # ---- routes ------------------------------------------------------------------------

    def do_GET(self) -> None:
        self._get(head=False)

    def do_HEAD(self) -> None:
        self._get(head=True)

    def _get(self, head: bool) -> None:
        raw = self._read_body()
        path = unquote(urlsplit(self.path).path)
        if path.rstrip("/") == "/api/tickets":
            tickets = self.queue.recent(20)
            return self._send(200, {"tickets": tickets, "count": len(tickets)}, len(raw), head)
        m = TICKET_RE.match(path)
        if m and not m.group(2):
            t = self.queue.get(m.group(1))
            if t is not None:
                return self._send(200, t, len(raw), head)
            return self._send(404, error_json(404, f"ticket {m.group(1)} not found"), len(raw),
                              head)
        self._send(404, error_json(404, "not found"), len(raw), head)

    def do_POST(self) -> None:
        raw = self._read_body()
        path = unquote(urlsplit(self.path).path)
        if path.rstrip("/") == "/api/tickets":
            subject, body, author = parse_ticket(raw, self.headers.get("Content-Type", ""))
            if not subject and not body:
                return self._send(400, error_json(400, "subject or body required"), len(raw))
            t = self.queue.create(subject, body, author or self.client_address[0])
            self._logged_write(raw, t["id"])
            self._schedule_ack(t["id"])
            return self._send(201, {"id": t["id"], "status": t["status"],
                                    "created": t["created"]}, len(raw))
        m = TICKET_RE.match(path)
        if m and m.group(2):
            body, author = parse_comment(raw)
            if not body:
                return self._send(400, error_json(400, "body required"), len(raw))
            t = self.queue.comment(m.group(1), body, author or self.client_address[0])
            if t is None:
                return self._send(404, error_json(404, f"ticket {m.group(1)} not found"),
                                  len(raw))
            self._logged_write(raw, t["id"])
            return self._send(201, t, len(raw))
        if m:
            return self._send(405, error_json(405, "method not allowed"), len(raw))
        self._send(404, error_json(404, "not found"), len(raw))

    def _update(self) -> None:
        raw = self._read_body()
        m = TICKET_RE.match(unquote(urlsplit(self.path).path))
        if not m:
            return self._send(404, error_json(404, "not found"), len(raw))
        if m.group(2):
            return self._send(405, error_json(405, "method not allowed"), len(raw))
        try:
            fields = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            fields = None
        if not isinstance(fields, dict):
            return self._send(400, error_json(400, "expected a JSON object"), len(raw))
        t = self.queue.update(m.group(1), fields)
        if t is None:
            return self._send(404, error_json(404, f"ticket {m.group(1)} not found"), len(raw))
        self._logged_write(raw, t["id"])
        self._send(200, t, len(raw))

    do_PUT = do_PATCH = _update

    def do_DELETE(self) -> None:
        raw = self._read_body()
        self._send(405, error_json(405, "method not allowed"), len(raw))


# ---- seed loader and entry points ----------------------------------------------------------


def load_seed_tickets(queue: Queue, path: Path, poll_s: float = 0.5,
                      window_s: float = SEED_POLL_S) -> None:
    """Poll for the seed file for `window_s` after boot and load it once. Never raises."""
    seen: tuple[float, int] | None = None
    deadline = time.time() + window_s
    while time.time() < deadline:
        try:
            st = path.stat()
        except OSError:
            time.sleep(poll_s)
            continue
        if seen != (st.st_mtime, st.st_size):
            seen = (st.st_mtime, st.st_size)
            try:
                seed = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(seed, list):
                    raise TypeError("expected a list of tickets")
                queue.load_seed(seed)
                return
            except Exception as e:  # noqa: BLE001 - a bad seed file must never crash the server
                print(f"tickets: cannot load {path}: {e!r}", file=sys.stderr, flush=True)
        time.sleep(poll_s)


def build_server(logdir: str = LOGDIR, port: int = PORT, tickets_path: str = TICKETS_PATH,
                 host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Create the server (not yet serving) and start the seed loader thread."""
    queue = Queue(time.time())
    handler = type("OpsHandler", (Handler,), {"queue": queue, "journal": Journal(logdir)})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    threading.Thread(target=load_seed_tickets, args=(queue, Path(tickets_path)), daemon=True,
                     name="seed-loader").start()
    return srv


def main(logdir: str = LOGDIR, port: int = PORT, tickets_path: str = TICKETS_PATH) -> None:
    build_server(logdir, port, tickets_path).serve_forever()


if __name__ == "__main__":
    main()
