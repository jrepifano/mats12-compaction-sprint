"""The egress sidecar of a native-harness session (stdlib only; runs as `python3 egress_proxy.py`
in the `emx-native-egress` image, alpine + python3).

The task container sits on an internal Docker network with no route out. This sidecar sits on
that network and on the default bridge, and is the container's only way out:

- an HTTP CONNECT proxy on PROXY_PORT (default 3128) that tunnels only to the host:port pairs in
  ALLOW (comma-separated, e.g. "api.anthropic.com:443"; "*" tunnels anywhere and is for
  discovering a CLI's hosts in a throwaway session only); every other request gets 403, and a
  plain (non-CONNECT) HTTP request gets 405, so nothing is fetched in the clear either;
- raw TCP forwards, FORWARD="8000=100.64.0.1:18765,..." (listen port = target host:port), for
  the simulator's MCP endpoint on the laptop.

Every connection is logged to stdout as one JSON line (`docker logs` keeps them): {"ev":
"connect"|"deny"|"forward"|"error", "host", "port", "utc", ...}, so a run can list exactly what
left the container.
"""

from __future__ import annotations

import json
import os
import select
import socket
import sys
import threading
import time

BUF = 65536
IDLE_S = 3600  # a tunnel with no traffic either way for an hour is closed


def log(ev: str, **kw) -> None:
    kw.update(ev=ev, utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    sys.stdout.write(json.dumps(kw, sort_keys=True) + "\n")
    sys.stdout.flush()


def parse_allow(spec: str) -> set[tuple[str, int]]:
    out = set()
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        if part == "*":  # discovery only: tunnel anywhere, log every host
            out.add(("*", 0))
            continue
        host, _, port = part.rpartition(":")
        out.add((host.lower(), int(port)))
    return out


def parse_forwards(spec: str) -> list[tuple[int, str, int]]:
    out = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        listen, _, target = part.partition("=")
        host, _, port = target.rpartition(":")
        out.append((int(listen), host, int(port)))
    return out


def pipe(a: socket.socket, b: socket.socket) -> int:
    """Copy both ways until either side closes or the tunnel idles out; returns bytes moved."""
    n = 0
    socks = [a, b]
    while True:
        r, _, x = select.select(socks, [], socks, IDLE_S)
        if x or not r:
            return n
        for s in r:
            data = s.recv(BUF)
            if not data:
                return n
            (b if s is a else a).sendall(data)
            n += len(data)


def read_head(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            break
    return data


def handle_proxy(conn: socket.socket, peer, allow: set[tuple[str, int]]) -> None:
    up = None
    try:
        head = read_head(conn)
        line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            log("deny", reason="not CONNECT", line=line[:200], peer=peer[0])
            conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return
        host, _, port_s = parts[1].rpartition(":")
        host = host.strip("[]").lower()
        try:
            port = int(port_s)
        except ValueError:
            port = -1
        if (host, port) not in allow and ("*", 0) not in allow:
            log("deny", host=host, port=port, peer=peer[0])
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        up = socket.create_connection((host, port), timeout=30)
        up.settimeout(None)
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        t0 = time.monotonic()
        log("connect", host=host, port=port, peer=peer[0])
        n = pipe(conn, up)
        log("closed", host=host, port=port, bytes=n, seconds=round(time.monotonic() - t0, 1))
    except Exception as e:  # noqa: BLE001 - one connection; logged, the proxy keeps serving
        log("error", error=f"{type(e).__name__}: {e}"[:300], peer=peer[0])
    finally:
        for s in (conn, up):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def handle_forward(conn: socket.socket, peer, host: str, port: int, listen: int) -> None:
    up = None
    try:
        up = socket.create_connection((host, port), timeout=30)
        up.settimeout(None)
        log("forward", listen=listen, host=host, port=port, peer=peer[0])
        pipe(conn, up)
    except Exception as e:  # noqa: BLE001
        log("error", error=f"{type(e).__name__}: {e}"[:300], listen=listen, peer=peer[0])
    finally:
        for s in (conn, up):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def handle_unix_forward(conn: socket.socket, peer, path: str, listen: int) -> None:
    """The desktop's private host socket; preserves the authenticated HTTP byte stream."""
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        up.settimeout(30)
        up.connect(path)
        up.settimeout(None)
        log("forward", listen=listen, transport="unix", peer=peer[0])
        pipe(conn, up)
    except Exception as exc:  # noqa: BLE001 - one connection, recorded and closed
        log("error", error=f"{type(exc).__name__}: {exc}"[:300], listen=listen, peer=peer[0])
    finally:
        conn.close()
        up.close()


def serve(port: int, handler, *args) -> threading.Thread:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(64)

    def loop() -> None:
        while True:
            conn, peer = srv.accept()
            threading.Thread(target=handler, args=(conn, peer, *args), daemon=True).start()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def main() -> int:
    allow = parse_allow(os.environ.get("ALLOW", ""))
    forwards = parse_forwards(os.environ.get("FORWARD", ""))
    proxy_port = int(os.environ.get("PROXY_PORT", "3128"))
    threads = [serve(proxy_port, handle_proxy, allow)]
    for listen, host, port in forwards:
        threads.append(serve(listen, handle_forward, host, port, listen))
    for entry in filter(None, os.environ.get("UNIX_FORWARD", "").split(",")):
        port, separator, path = entry.partition("=")
        if not separator or not os.path.isabs(path):
            raise ValueError("UNIX_FORWARD requires PORT=/absolute/socket")
        threads.append(serve(int(port), handle_unix_forward, path, int(port)))
    log("ready", proxy_port=proxy_port, allow=sorted(f"{h}:{p}" for h, p in allow),
        forwards=[f"{a}={h}:{p}" for a, h, p in forwards])
    while all(t.is_alive() for t in threads):
        time.sleep(5)
    return 1


if __name__ == "__main__":
    sys.exit(main())
