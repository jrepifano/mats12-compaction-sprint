"""What every native-harness arm shares (brief logs/s2/briefs/native-arms.md): the simulator as
an MCP server, restricted egress, the task container, streaming execs, kept snapshots and the
boundary writer. A harness module (`claude_code`, `agy`, the Codex arm's) drives the CLI and
turns its native transcript into a `NativeBoundary`; everything else is here.

Topology, per session (`NativeSession`):

    laptop:   SimServer (FastMCP, the released `ask_user` tool; UserSim, our port) on
              <tailscale ip>:<free port>, with POST /emx/boundary for the harness's hook
    desktop:  internal network emx-native-<id> (docker network create --internal: no route out)
              ├─ task container (the DeepSWE image or a kept boundary image), on it only
              └─ egress sidecar (`egress_proxy.py`), on it and on the default bridge, aliases
                 `user-server` (port 8000 forwards to the SimServer, so the step instructions'
                 "http://user-server:8000/mcp" is literally right) and `egress` (port 3128, an
                 HTTPS CONNECT proxy that tunnels only to the arm's allowlist, ALLOW[arm])

The agent reaches its vendor API through HTTPS_PROXY=http://egress:3128 and nothing else: a
direct connection has no route, and the proxy refuses any other host (`egress_check` proves both
inside the container). The simulator's own repo commands run harness-side through `docker exec`
(`interact.sandbox_runner`), as in the pi arm, so the persona never enters the container.

Containers follow the sandbox contract (`compaction.deepswe.sandbox`): --rm, a name, the label
emx.sandbox=native-<arm> plus the invocation's emx.run label, `timeout -k 5 <lifetime>` as the
main process, every exec under an in-container timeout. The sidecar and the network carry the
same labels and are removed (and confirmed) by `Egress.down`. Kept boundary images follow the
KeepStore rules (`session.KeepStore`: per-execution unique tags, emx.keep label, never re-point
an existing tag).

The FastMCP dependency is not in the project: run native arms under
`uv run --with fastmcp==4.1.3` (the version the released server resolves to today, and the one
the feasibility check used). Everything else here imports without it.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Self

from compaction.deepswe import sandbox as SB
from compaction.s2 import compact as K
from compaction.s2 import interact as I
from compaction.s2 import session as SS

FASTMCP_VERSION = "4.1.3"
SIM_SERVER_NAME = "simulated-user"  # FastMCP("simulated-user"), upstream server.py
ASK_USER_DOC = "Ask the user a question and get their response."
MCP_URL = "http://user-server:8000/mcp"  # what the task's instruction.md and task.toml name
BOUNDARY_PATH = "/emx/boundary"
BOUNDARY_URL = "http://user-server:8000" + BOUNDARY_PATH
AUDIT_PATH = "/emx/audit"
AUDIT_URL = "http://user-server:8000" + AUDIT_PATH
TOKEN_HEADER = "X-Emx-Token"
TOKEN_QUERY = "emx_token"
PROXY_URL = "http://egress:3128"
FORWARD_PORT = 8000

# Hosts each arm's CLI may reach (host:port, CONNECT only). Codex: the Codex arm's own list.
ALLOW: dict[str, tuple[str, ...]] = {
    "claude_code": ("api.anthropic.com:443",),
    "codex": ("chatgpt.com:443", "api.openai.com:443", "auth.openai.com:443"),
    # seen live 24 Oct (discovery mode): the model API, flags, user info; plus token refresh.
    # Left out: lh3.googleusercontent.com (profile picture), play.googleapis.com (telemetry).
    "agy": ("daily-cloudcode-pa.googleapis.com:443", "cloudcode-pa.googleapis.com:443",
            "aicode.googleapis.com:443", "antigravity-unleash.goog:443",
            "www.googleapis.com:443", "oauth2.googleapis.com:443"),
}
# by name (DNS on an internal network resolves nothing outside) and by address (no route)
EGRESS_CHECK_DENIED = ("https://github.com", "https://pypi.org/simple/", "https://1.1.1.1")

# Desktop limits for agent-session containers (main session, 24 Oct): small when idle
# (85-185 MB), so 3 GB and 2 CPUs, at most 8 at a time per sub-agent, started only while the
# host has >= 6 GB available. Test-heavy containers: 8 GB, 4 CPUs, >= 10 GB available.
AGENT_CPUS, AGENT_MEMORY_MB, AGENT_MIN_FREE_GB = 2.0, 3072, 6.0
HEAVY_CPUS, HEAVY_MEMORY_MB, HEAVY_MIN_FREE_GB = 4.0, 8192, 10.0

EGRESS_BASE = "alpine:3.20"
EGRESS_SCRIPT = Path(__file__).with_name("egress_proxy.py")
EGRESS_BYTES = EGRESS_SCRIPT.read_bytes()  # as loaded: later edits never reach a sidecar
NATIVE_LABEL = "emx.native"  # on the egress image and the networks: "egress" / the session id


def purpose(arm: str) -> str:
    return f"native-{arm.replace('_', '-')}"


def utc() -> str:
    return SS.utc_now()


def _dump(path: Path, obj) -> None:
    SS._dump(Path(path), obj)


def tailscale_ip() -> str:
    """The laptop's Tailscale IPv4 (EMX_NATIVE_SIM_BIND overrides): where SimServer listens, so
    the desktop's containers reach it and nothing outside the tailnet does."""
    if os.environ.get("EMX_NATIVE_SIM_BIND"):
        return os.environ["EMX_NATIVE_SIM_BIND"]
    for exe in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            p = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10,
                               check=False)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.split()[0]
    raise RuntimeError("no Tailscale IPv4 found; set EMX_NATIVE_SIM_BIND")


def free_port(bind: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((bind, 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------------------------------
# Egress: the internal network and the allowlisting sidecar
# ------------------------------------------------------------------------------------------


def _sha12(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()[:12]


def egress_image() -> str:
    """The sidecar image tag: changes whenever egress_proxy.py does."""
    return f"emx-native/egress:{_sha12(EGRESS_BYTES)}"


def ensure_egress_image(host: str | None) -> str:
    """Build the sidecar image on `host` if it isn't there (alpine + python3 + the script)."""
    tag = egress_image()
    with SB.using_host(host):
        if SB._docker(["image", "inspect", tag], timeout=60, check=False).returncode == 0:
            return tag
        df = (f"FROM {EGRESS_BASE}\nRUN apk add --no-cache python3\n"
              "COPY egress_proxy.py /egress_proxy.py\n"
              f"LABEL {NATIVE_LABEL}=egress\n")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, data in (("Dockerfile", df.encode()),
                               ("egress_proxy.py", EGRESS_BYTES)):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        SB._docker(["build", "-q", "-t", tag, "-"], timeout=900, input_bytes=buf.getvalue())
    return tag


@dataclass
class Egress:
    """One session's internal network and sidecar (module docstring)."""
    arm: str
    sim_host: str  # where the SimServer listens (the laptop's Tailscale IP)
    sim_port: int
    host: str | None = None  # docker -H
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    allow: tuple[str, ...] | None = None  # default ALLOW[arm]
    lifetime_s: int = 8 * 3600
    network: str = ""
    proxy: str = ""
    sim_socket: str | None = None  # local Linux host socket, mounted only into the sidecar

    def __post_init__(self) -> None:
        if self.allow is None:
            self.allow = ALLOW[self.arm]
        sid = uuid.uuid4().hex[:8]
        self.network = self.network or f"emx-native-{self.run_id}-{sid}"
        self.proxy = self.proxy or f"{purpose(self.arm)}-egress-{self.run_id}-{sid}"

    def labels(self) -> list[str]:
        return ["--label", f"{SB.LABEL_KEY}={purpose(self.arm)}",
                "--label", f"{SB.RUN_LABEL_KEY}={self.run_id}",
                "--label", f"{NATIVE_LABEL}={self.network}"]

    def proxy_run_args(self, image: str) -> list[str]:
        """The sidecar's `docker run` argv (pure)."""
        transport = (["--mount", f"type=bind,src={Path(self.sim_socket).parent},dst=/run/emx-sim,readonly",
                      "-e", f"UNIX_FORWARD={FORWARD_PORT}=/run/emx-sim/{Path(self.sim_socket).name}"]
                     if self.sim_socket else ["-e", f"FORWARD={FORWARD_PORT}={self.sim_host}:{self.sim_port}"])
        return ["run", "-d", "--rm", "--name", self.proxy, *self.labels(),
                "--network", "bridge", "--memory", "128m", "--cpus", "0.5",
                "-e", f"ALLOW={','.join(self.allow or ())}",
                *transport,
                image, "timeout", "-k", "5", str(int(self.lifetime_s)),
                "python3", "-u", "/egress_proxy.py"]

    def up(self) -> str:
        """Create the network and start the sidecar; returns the network name."""
        image = ensure_egress_image(self.host)
        with SB.using_host(self.host):
            try:
                SB._docker(["network", "create", "--internal", *self.labels(), self.network],
                           timeout=60)
                SB._docker(self.proxy_run_args(image), timeout=120)
                SB._docker(["network", "connect", "--alias", "user-server", "--alias",
                            "egress", self.network, self.proxy], timeout=60)
                self._wait_ready()
            except BaseException:
                self.down()
                raise
        return self.network

    def _wait_ready(self, timeout_s: float = 30) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if any(e.get("ev") == "ready" for e in self.events()):
                return
            time.sleep(0.5)
        raise SB.SandboxError(f"egress sidecar {self.proxy} not ready in {timeout_s}s")

    def events(self) -> list[dict]:
        """The sidecar's JSON log lines so far (every tunnel, refusal and forward)."""
        with SB.using_host(self.host):
            p = SB._docker(["logs", self.proxy], timeout=60, check=False)
        out = []
        for line in p.stdout.decode(errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out

    def proxy_env(self) -> dict[str, str]:
        """For the agent's process: tunnel through the sidecar; MCP and the hook go direct."""
        no = "user-server,egress,localhost,127.0.0.1"
        return {"HTTPS_PROXY": PROXY_URL, "https_proxy": PROXY_URL, "NO_PROXY": no,
                "no_proxy": no}

    def down(self) -> list[str]:
        """Remove the sidecar and the network; returns what was NOT confirmed gone."""
        bad = []
        with SB.using_host(self.host):
            if not SB.remove_container(self.proxy):
                bad.append(self.proxy)
            SB._docker(["network", "rm", self.network], timeout=60, check=False)
            p = SB._docker(["network", "ls", "--format", "{{.Name}}"], timeout=30, check=False)
            if p.returncode != 0 or self.network in p.stdout.decode().split():
                bad.append(f"network {self.network}")
        return bad


def egress_check(sb: SB.Sandbox, egress: Egress, allowed_url: str) -> dict:
    """Inside the task container: a direct connection fails (no route), the proxy refuses
    github.com and pypi.org, and `allowed_url` (the vendor API) answers through the proxy.
    {"ok": bool, "checks": [...]}. Needs curl in the image (every DeepSWE image has it)."""
    checks = []

    def curl(url: str, via_proxy: bool) -> dict:
        px = f"-x {PROXY_URL} " if via_proxy else "--noproxy '*' "
        r = sb.exec(f"curl -sS -o /dev/null -w '%{{http_code}}' -m 15 {px}{shlex.quote(url)}",
                    timeout_s=40, workdir="/")
        return {"url": url, "via_proxy": via_proxy, "rc": r.rc, "http": r.stdout.strip(),
                "err": r.stderr.strip()[-200:]}

    for url in EGRESS_CHECK_DENIED:
        c = curl(url, via_proxy=False)
        c["want"], c["pass"] = "no route", c["rc"] != 0
        checks.append(c)
        c = curl(url, via_proxy=True)
        # curl reports a refused CONNECT as rc 56 ("CONNECT tunnel failed, response 403")
        c["want"], c["pass"] = "proxy 403", c["rc"] != 0 and "403" in (c["err"] + c["http"])
        checks.append(c)
    c = curl(allowed_url, via_proxy=True)
    c["want"] = "any HTTP status"
    c["pass"] = c["rc"] == 0 and c["http"].isdigit() and c["http"] != "000"
    checks.append(c)
    c = curl(allowed_url, via_proxy=False)
    c["want"], c["pass"] = "no route", c["rc"] != 0
    checks.append(c)
    denied = [e for e in egress.events() if e.get("ev") == "deny"]
    return {"ok": all(x["pass"] for x in checks), "checks": checks, "proxy_denies": denied,
            "utc": utc()}


# ------------------------------------------------------------------------------------------
# The task container
# ------------------------------------------------------------------------------------------


def start_task_container(inv: SB.Invocation, image: str, *, network: str,
                         extra_args: list[str] | tuple[str, ...] = (),
                         lifetime_s: int = I.CONTAINER_LIFETIME_S, cpus: float = 2,
                         memory_mb: int = 8192, platform: str | None = "linux/amd64",
                         name: str | None = None) -> SB.Sandbox:
    """`Invocation.start` with extra `docker run` flags (mounts, tmpfs) before the image; the
    container is tracked by `inv`, so its cleanup removes it."""
    name = name or f"{inv.purpose}-{inv.run_id}-{uuid.uuid4().hex[:8]}"
    args = SB.run_args(image, name, inv.purpose, lifetime_s, cpus, memory_mb, platform,
                       network, inv.run_id)
    at = args.index(image)
    args[at:at] = list(extra_args)
    t0 = time.monotonic()
    with SB.using_host(inv.host):
        try:
            SB._docker(args, timeout=300)
        except BaseException:
            SB.remove_container(name)
            raise
    sb = SB.Sandbox(name=name, image=image, purpose=inv.purpose, run_id=inv.run_id,
                    host=inv.host)
    sb.log.append({"op": "start", "seconds": time.monotonic() - t0})
    inv.sandboxes.append(sb)
    return sb


@dataclass
class StreamResult:
    rc: int
    seconds: float
    timed_out: bool  # rc 124/137: the in-container timeout fired, or something killed it
    killed: str | None = None  # why the runner stopped it, if it did
    timed_out_by_cli: bool = False  # the in-container `timeout` ran out (its deadline passed)


def stream_exec(sb: SB.Sandbox, cmd: str, *, timeout_s: int, log_path: Path,
                env: dict[str, str] | None = None, workdir: str = "/app",
                stdin: bytes | None = None, stop: threading.Event | None = None,
                on_line: Callable[[str], None] | None = None) -> StreamResult:
    """A long `docker exec` whose stdout goes line by line to `log_path` (and `on_line`).
    `env` values reach the container as `docker exec -e NAME` with the value only in the docker
    client's environment, never in an argv or a log. Under an in-container `timeout -k 5`;
    `stop` set (e.g. by a boundary hook) ends the wait early: the caller kills the CLI inside
    the container."""
    env = dict(env or {})
    argv = ["exec", *(["-i"] if stdin is not None else []),
            *[a for k in env for a in ("-e", k)], "-w", workdir, sb.name,
            "timeout", "-k", "5", str(int(timeout_s)), "bash", "-lc", cmd]
    full = SB.docker_argv(argv, sb.host)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    penv = {**os.environ, **env}
    with open(log_path, "ab") as out, open(str(log_path) + ".err", "ab") as err:
        p = subprocess.Popen(full, stdin=subprocess.PIPE if stdin is not None else
                             subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, env=penv)
        if stdin is not None:
            assert p.stdin is not None
            p.stdin.write(stdin)
            p.stdin.close()
        assert p.stdout is not None

        def pump() -> None:
            for raw in iter(p.stdout.readline, b""):
                out.write(raw)
                out.flush()
                if on_line is not None:
                    with contextlib.suppress(Exception):
                        on_line(raw.decode(errors="replace"))

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        killed = None
        deadline = t0 + timeout_s + SB.CLIENT_GRACE_S
        while p.poll() is None:
            if stop is not None and stop.is_set():
                killed = "stop"
                break
            if time.monotonic() > deadline:
                killed = "client_timeout"
                break
            time.sleep(0.5)
        if killed and p.poll() is None:
            # the client goes now; the CLI inside the container is the caller's to stop
            p.kill()
            p.wait(timeout=30)
        th.join(timeout=30)
    rc = p.returncode if p.returncode is not None else -1
    secs = time.monotonic() - t0
    return StreamResult(rc, round(secs, 1), rc in (124, 137), killed,
                        rc in (124, 137) and secs >= timeout_s - 1)


# Docker-over-ssh transport failures (main session, 24 Oct: OpenSSH's PerSourcePenalties
# dropped the laptop's connections, and one drop at a fail-closed harness exec ended a 1-2 h
# run). Only the signature of every drop seen tonight is retried: the CLI's error names the
# `_ping` request (API negotiation) and its ssh helper exited 255. The CLI pings before its
# first operation request and reports the error of the request that failed, so an error naming
# `_ping` means the failure came before an operation was sent; that is inferred from the CLI's
# behaviour (docker 29.9.2), not guaranteed by it (astra transport r2). A generic "error during
# connect" can follow a request the daemon received (r1 #1), so anything without the `_ping`
# signature is never retried, nor is any result docker or the command itself returned.
TRANSPORT_BACKOFF_S = (10, 30, 90)
_PING_FAIL = re.compile(r'error during connect: (?:Get|Head) "?https?://[^\s"]*/_ping"?: ')
TRANSPORT_RETRIES: list[dict] = []  # every retry of this process, also printed
MIN_ATTEMPT_S = 5.0  # no retry with less than this left of the call's own timeout


def transport_failed(rc: int, stderr: str) -> bool:
    return rc != 0 and bool(_PING_FAIL.search(stderr)) and "exit status 255" in stderr


def retry_transport(run: Callable[[float], tuple], what: str, budget_s: float, sleep=None):
    """`run(timeout_s)` -> (result, rc, stderr), retried after each wait of
    TRANSPORT_BACKOFF_S while its failure is a failed docker dial (`transport_failed`), all
    within `budget_s`, the call's own timeout: no wait is begun that would leave less than
    MIN_ATTEMPT_S, the remainder is computed again after the wait (an oversleep can eat it),
    no attempt starts with less than MIN_ATTEMPT_S left, and each gets exactly what is left
    (astra transport r1 #2, r2), so a caller's deadline arithmetic still holds. Each retry is logged (TRANSPORT_RETRIES and a printed
    `transport_retry` line). Returns the last result."""
    end = time.monotonic() + budget_s
    res, rc, err = run(budget_s)
    for n, wait in enumerate(TRANSPORT_BACKOFF_S, 1):
        if not transport_failed(rc, err):
            break
        left = end - time.monotonic() - wait
        if left < MIN_ATTEMPT_S:
            break
        row = {"event": "transport_retry", "what": what[:120], "retry": n, "wait_s": wait,
               "left_s": round(left, 1), "rc": rc, "stderr": err.strip()[-200:],
               "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        TRANSPORT_RETRIES.append(row)
        print(json.dumps(row), flush=True)
        (sleep or time.sleep)(wait)
        left = end - time.monotonic()
        if left < MIN_ATTEMPT_S:
            break
        res, rc, err = run(left)
    return res


_ORIG_DOCKER = SB._docker


def _docker_with_retry(args: list[str], timeout: float, check: bool = True,
                       input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    def once(t: float):
        p = _ORIG_DOCKER(args, t, check=False, input_bytes=input_bytes)
        return p, p.returncode, p.stderr.decode(errors="replace")

    p = retry_transport(once, "docker " + " ".join(args[:2]), timeout)
    if check and p.returncode != 0:
        raise SB.SandboxError(f"docker {args[0]} rc={p.returncode}: "
                              f"{p.stderr.decode()[-2000:]}")
    return p


def install_transport_retry() -> None:
    """Route every docker call of `compaction.deepswe.sandbox` in this process (harness execs,
    commits, listings, cleanup) through `retry_transport`, within each call's own timeout.
    Called by the native launchers (scripts/s2_native.py); idempotent."""
    SB._docker = _docker_with_retry


def exec_env(sb: SB.Sandbox, cmd: str, env: dict[str, str], *, timeout_s: int = 300,
             workdir: str = "/") -> SB.ExecResult:
    """`Sandbox.exec` with `env` passed as `docker exec -e NAME` (values only in the client's
    environment). Not logged in `sb.log` (the command may expand a secret)."""
    argv = SB.docker_argv(["exec", *[a for k in env for a in ("-e", k)], "-w", workdir,
                           sb.name, "timeout", "-k", "5", str(int(timeout_s)), "bash", "-lc",
                           cmd], sb.host)
    t0 = time.monotonic()

    def once(t: float):
        p = subprocess.run(argv, capture_output=True, timeout=t, env={**os.environ, **env},
                           check=False)
        return p, p.returncode, p.stderr.decode(errors="replace")

    p = retry_transport(once, f"exec_env {sb.name}", timeout_s + SB.CLIENT_GRACE_S)
    return SB.ExecResult(p.returncode, p.stdout.decode(errors="replace"),
                         p.stderr.decode(errors="replace"), time.monotonic() - t0)


START_MARKER = "/opt/emx/.session-start"  # touched when a session's container starts


def scan_script(names: list[str], root: str = "/", marker: str = START_MARKER,
                tmp: str = "/dev/shm", pattern_cmd: str | None = None) -> str:
    """The in-container scan (pure; tested): the secrets go from env vars to a pattern file on
    /dev/shm (a tmpfs, never committed) through the printf builtin, so no process's argv holds
    one; every write is checked and the file must end up non-empty. Every regular file on the
    root filesystem (`find -xdev`: no volumes, tmpfs, /proc, /sys or bind mounts, which
    `docker commit` leaves out anyway) changed since the session started (mtime or ctime newer
    than START_MARKER; the image itself was clean) streams from find into `grep -lF -f`
    batches (`--null`: GNU and BSD grep alike) with no list file anywhere (a size-limited tmpfs can't overflow). Output is
    NUL-separated typed records: "#N <files>" per batch, each hit's absolute path, and a final
    "SCAN_OK" only when the whole pipeline returns 0. Fails closed: a find, xargs or grep error
    (rc > 1) or a failed pattern write exits non-zero."""
    fill = "".join(f"printf '%s\\n' \"${n}\" >> \"$P\" || {{ rm -f \"$P\"; exit 6; }}; "
                   for n in names)
    if pattern_cmd:  # more patterns, made inside the container (e.g. from a mounted store)
        fill += f"{{ {pattern_cmd}; }} >> \"$P\" || {{ rm -f \"$P\"; exit 5; }}; "
    m, t, r = shlex.quote(marker), shlex.quote(tmp), shlex.quote(root)
    return (
        f"set -o pipefail; umask 077; P=$(mktemp {t}/scan.XXXXXX) || exit 3; " + fill +
        "[ -s \"$P\" ] || { rm -f \"$P\"; exit 7; }; "
        f"[ -f {m} ] || {{ rm -f \"$P\"; exit 4; }}; "
        f"find {r} -xdev -type f \\( -newer {m} -o -cnewer {m} \\) -print0 | "
        "xargs -0 sh -c '[ $# -eq 0 ] && exit 0; printf \"#N %s\\0\" \"$#\"; "
        "grep -lF --null -f \"$0\" -- \"$@\"; r=$?; [ $r -le 1 ] || exit 255' \"$P\"; rc=$?; "
        "rm -f \"$P\"; [ $rc -eq 0 ] && printf 'SCAN_OK\\0'")


def parse_scan(out: str) -> tuple[bool, int, list[str]]:
    """(ok, files scanned, hit paths) from `scan_script`'s NUL-separated records. Hits are
    absolute paths, so they can't collide with the "#N " and "SCAN_OK" markers, and a path
    with a newline in it stays one record."""
    recs = [x for x in out.split("\0") if x]
    n = sum(int(x[3:]) for x in recs if x.startswith("#N ") and x[3:].isdigit())
    return "SCAN_OK" in recs, n, [x for x in recs if x.startswith("/")]


ENTRYPOINT_RC = "/tmp/emx_entrypoint_rc"  # families.rf: images whose entrypoint starts services
ENTRYPOINT_WAIT_S = 900


def image_entrypoint(image: str, host: str | None) -> list[str]:
    with SB.using_host(host):
        p = SB._docker(["image", "inspect", "--format", "{{json .Config.Entrypoint}}", image],
                       timeout=60)
    return json.loads(p.stdout.decode().strip() or "null") or []


def wait_entrypoint(sb: SB.Sandbox, wait_s: float = ENTRYPOINT_WAIT_S, poll_s: float = 10) -> int:
    """Wait for an image entrypoint's record (`families.rf.ENTRYPOINT_RC`, written once its
    services are up) from this container's own start (a kept image may carry an older one);
    a nonzero rc, or none in `wait_s`, is an infrastructure failure (SandboxError)."""
    cmd = (f"s=$(stat -c %Z /proc/1); if [ -f {ENTRYPOINT_RC} ] && "
           f"[ $(stat -c %Y {ENTRYPOINT_RC}) -ge $((s - 1)) ]; then cat {ENTRYPOINT_RC}; "
           "else echo WAIT; fi")
    t0 = time.monotonic()
    while True:
        out = sb.exec(cmd, timeout_s=30, workdir="/").stdout.strip()
        if out and out != "WAIT":
            rc = int(out.split()[0]) if out.split()[0].lstrip("-").isdigit() else -1
            if rc != 0:
                raise SB.SandboxError(f"{sb.name}: image entrypoint failed rc={rc}")
            return rc
        if time.monotonic() - t0 > wait_s:
            raise SB.SandboxError(f"{sb.name}: no entrypoint record in {wait_s:.0f}s")
        time.sleep(poll_s)


def mark_session_start(sb: SB.Sandbox) -> None:
    r = sb.exec(f"mkdir -p {Path(START_MARKER).parent} && touch {START_MARKER}", timeout_s=30,
                workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"session marker: {r.stderr[-300:]}")


def tolerant_exec(sb: SB.Sandbox) -> Callable[..., SB.ExecResult]:
    """The shared tolerant exec (`interact.tolerant_exec`: a docker-client timeout settles the
    exec and reports rc 124, the container kept; anything unconfirmed fails closed) as an
    `exec`-shaped callable, counting into `sb.exec_stats` ("client_timeouts", "settles").
    The rule for every arm (main session, 24 Oct): the agent's steps and the simulator's
    commands are tolerant; harness operations (freeze, scans, fingerprint, snapshot,
    transcript reads, setup) use the guarded `Sandbox.exec` and fail closed."""
    def exec_(cmd: str, timeout_s: int = 600, workdir: str = "/app",
              env: dict[str, str] | None = None) -> SB.ExecResult:
        return I.tolerant_exec(sb, cmd, timeout_s=timeout_s, workdir=workdir,
                               stats=getattr(sb, "exec_stats", None), env=env)
    return exec_


def secret_scan(sb: SB.Sandbox, secrets: dict[str, str], pattern_cmd: str | None = None) -> dict:
    """{"hits": [paths], "scanned": n, "seconds"} for the secrets {env name: value} in what a
    commit of `sb` would add (`scan_script`). Raises when the scan itself fails, so a caller
    never mistakes a partial scan for a clean one. Run before every kept commit."""
    if not secrets or not all(secrets.values()):
        raise ValueError("secret_scan needs non-empty secrets")
    r = exec_env(sb, scan_script(list(secrets), pattern_cmd=pattern_cmd), dict(secrets),
                 timeout_s=900)
    ok, n, hits = parse_scan(r.stdout)
    if r.rc != 0 or not ok:
        raise SB.SandboxError(f"secret scan failed rc={r.rc}: {r.stderr[-300:]}")
    return {"hits": hits, "scanned": n, "seconds": round(r.seconds, 1)}


PS_FIELDS = "pid=,ppid=,stat=,args="


def ps_table(sb: SB.Sandbox) -> tuple[dict[int, tuple[int, str, str]], str]:
    """{pid: (ppid, stat, args)} of the container, and the token marking this check's own
    process (whose tree is excluded)."""
    token = f"{SB.QUIESCE_TOKEN}-{uuid.uuid4().hex[:8]}"
    r = sb.exec(f": {token}; ps -eo {PS_FIELDS}", timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"ps failed in {sb.name} rc={r.rc}: {r.stderr[-300:]}")
    return parse_ps(r.stdout), token


def parse_ps(out: str) -> dict[int, tuple[int, str, str]]:
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[0].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2], parts[3] if len(parts) > 3 else "")
    return procs


def _tree(procs: dict[int, tuple[int, str, str]], roots: set[int]) -> set[int]:
    out = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _, _) in procs.items():
            if ppid in out and pid not in out:
                out.add(pid)
                changed = True
    return out


def is_harness_log_follower(argv: str) -> bool:
    """Recognize the argv shape only; exemption additionally requires follower pairing."""
    return isinstance(argv, str) and re.fullmatch(
        rf"(?:timeout -k 5 {FOLLOW_CHUNK_S} )?tail -c \+[1-9][0-9]* -F "
        r"/opt/emx/steps/[A-Za-z0-9][A-Za-z0-9_.-]*/out", argv) is not None


def harness_follower_pairs(procs: dict[int, tuple[int, str, str]]) -> set[int]:
    """Exempt only a docker-exec timeout (ppid 0) plus its exactly matching child tail."""
    prefix = f"timeout -k 5 {FOLLOW_CHUNK_S} "
    paired = set()
    for parent, (ppid, _state, argv) in procs.items():
        if ppid != 0 or not argv.startswith(prefix) or not is_harness_log_follower(argv):
            continue
        children = {pid for pid, (pp, _st, child) in procs.items()
                    if pp == parent and child == argv[len(prefix):]}
        if children:
            paired.add(parent)
            paired.update(children)
    return paired


def reclassify_harness_quiescence(fields: dict) -> dict:
    """Return a revised copy only for complete, matching docker-exec follower pairs.

    Transcript integrity, image presence, entrypoint services and fork_check remain separate
    gates. The original metadata is untouched.
    """
    out = copy.deepcopy(fields)
    processes = fields.get("processes")
    if (fields.get("ineligible_reason") != "not_quiescent" or not processes
            or not isinstance(processes, list)):
        return out
    procs = {}
    for process in processes:
        if not isinstance(process, str):
            return out
        pieces = process.split(None, 3)
        if (len(pieces) != 4 or not pieces[0].isdigit() or not pieces[1].isdigit()
                or int(pieces[0]) in procs):
            return out
        procs[int(pieces[0])] = (int(pieces[1]), pieces[2], pieces[3])
    if harness_follower_pairs(procs) != set(procs):
        return out
    out.update(quiescent=True, harness_exempted_processes=list(processes),
               quiescence_reclassification={"version": 2, "original_quiescent":
                   fields.get("quiescent"), "original_ineligible_reason": "not_quiescent",
                   "rule": "ppid-0 detached_exec timeout and exactly matching child tail"})
    if fields.get("services_running"):
        out["ineligible_reason"] = "services_unvalidated"
    else:
        out.pop("ineligible_reason", None)
    out["eligible"] = None
    return out


def classify(procs: dict[int, tuple[int, str, str]], token: str, cli: str,
             hook: str, services: set[int] | frozenset[int] = frozenset()) -> dict[str, list[int]]:
    """Split a container's processes at a hook-blocked boundary: `infra` = PID 1 and its sleep,
    the CLI process (args starting with `cli`) and its ancestors, the hook's process tree (args
    naming `hook`), this check's own tree and the image entrypoint's `services` (running when
    its record appeared: Postgres, Redis; a fork's entrypoint starts them again) with their
    trees; `strays` = everything else alive (e.g. the agent's background shells, children of
    the CLI). Zombies are ignored."""
    def anc(pid: int) -> set[int]:
        out = set()
        while pid in procs and pid not in out and pid > 0:
            out.add(pid)
            pid = procs[pid][0]
        return out

    clis = {p for p, (_, _, a) in procs.items() if a.startswith(cli)}
    hooks = {p for p, (_, _, a) in procs.items() if hook in a}
    mine = {p for p, (_, _, a) in procs.items() if token in a}
    infra = {1} | {p for p, (pp, _, a) in procs.items() if pp == 1 and a == "sleep infinity"}
    for c in clis:
        infra |= anc(c)
    infra |= _tree(procs, hooks) | _tree(procs, mine)
    infra |= _tree(procs, {p for p in services if p in procs})
    followers = harness_follower_pairs(procs)
    infra |= followers
    strays = [p for p, (_, st, _) in sorted(procs.items())
              if p not in infra and not st.startswith("Z")]
    return {"infra": sorted(infra), "strays": strays, "harness_followers": sorted(followers)}


def signal_pids(sb: SB.Sandbox, pids: list[int], sig: str) -> None:
    """kill -<sig> each pid; a pid that is already gone is fine, any other failure raises."""
    if not pids:
        return
    r = sb.exec("bad=0; for p in " + " ".join(map(str, pids)) + f"; do kill -{sig} $p 2>/dev/null"
                " || { [ -d /proc/$p ] && bad=1; }; done; exit $bad", timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"kill -{sig} failed for a live process in {sb.name}")


FREEZE_ROUNDS = 5


def freeze(sb: SB.Sandbox, cli: str, hook: str,
           services: set[int] | frozenset[int] = frozenset()) -> dict[int, str]:
    """SIGSTOP every process outside the CLI and hook trees (`classify`) and verify it: rediscover
    until every stray is stopped ("T") or gone, so a child spawned in between is caught too.
    Returns {pid: "ppid stat args"} of the stopped processes; raises if it can't stabilise.
    The caller must `signal_pids(..., "CONT")` them in a finally."""
    stopped: dict[int, str] = {}
    sb.harness_exempted_processes = {}
    try:
        for _ in range(FREEZE_ROUNDS):
            procs, tok = ps_table(sb)
            classified = classify(procs, tok, cli, hook, services)
            strays = classified["strays"]
            exempted = getattr(sb, "harness_exempted_processes", {})
            for pid in classified["harness_followers"]:
                exempted[pid] = f"{pid} {procs[pid][0]} {procs[pid][1]} {procs[pid][2]}"
            sb.harness_exempted_processes = exempted
            for p in strays:
                stopped.setdefault(p, f"{procs[p][0]} {procs[p][1]} {procs[p][2]}")
            live = [p for p in strays if not procs[p][1].startswith("T")]
            if not live:
                return stopped
            signal_pids(sb, live, "STOP")
        raise SB.SandboxError(f"background processes in {sb.name} did not stay stopped after "
                              f"{FREEZE_ROUNDS} rounds")
    except BaseException:
        # the caller never gets the list: resume what was stopped here (astra r3 #4)
        with contextlib.suppress(Exception):
            signal_pids(sb, sorted(stopped), "CONT")
        raise


STEP_DIR = "/opt/emx/steps"
FOLLOW_CHUNK_S = 300  # each follow session's in-container limit; the follower reconnects
# the docker host's sshd is shared by every agent (24 Oct: overloaded, refusing key exchanges):
# a step's status check runs at most every CHECK_S, backing off to CHECK_MAX_S while checks
# fail; a refused check only delays the next one, it never ends the run
CHECK_S, CHECK_MAX_S, WATCH_EVERY = 30, 300, 2
GONE = "No such container"


def detached_exec(sb: SB.Sandbox, cmd: str, *, name: str, timeout_s: int, log_path: Path,
                  env: dict[str, str] | None = None, stdin: bytes | None = None,
                  stop: threading.Event | None = None,
                  on_line: Callable[[str], None] | None = None,
                  watch: Callable[[], None] | None = None) -> StreamResult:
    """`stream_exec` that survives a dropped `docker -H ssh://` transport (native-codex,
    24 Oct: `ssh ... dial-stdio` exit 255 ended runs whose CLI was still working; lifted from
    codex.detached_step). `cmd` runs detached in the container (setsid, under
    `timeout -k 5 timeout_s`), stdout to <STEP_DIR>/<name>/out, stderr to .../err, its exit
    code to .../rc; the laptop follows `out` with `tail -F` sessions that resume from the
    last complete line's byte offset and drains the rest once rc exists. `env` values reach
    the container as `docker exec -e NAME` (only the launching exec needs them). A container
    that disappears ends the wait at once (killed "container_gone"; rc -1). Returns
    StreamResult; `reconnects` counts the follow sessions after the first. `watch()`, if
    given, runs at every 15 s check while the step is running (e.g. agy's background-task
    rule)."""
    env = dict(env or {})
    te = tolerant_exec(sb)  # the step's own plumbing: a stalled client never costs the run
    d = f"{STEP_DIR}/{name}"
    r = te(f"rm -rf {d} && mkdir -p {d}", timeout_s=60, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"step dir {d}: rc={r.rc} {r.stderr[-200:]}")
    if stdin is not None:
        sb.put_file(stdin, f"{d}/stdin")
    src = f"{d}/stdin" if stdin is not None else "/dev/null"
    inner = (f"timeout -k 5 {int(timeout_s)} bash -lc {shlex.quote(cmd)} < {src} > {d}/out "
             f"2> {d}/err; echo $? > {d}/rc.tmp && mv {d}/rc.tmp {d}/rc")
    launch = f"setsid nohup bash -c {shlex.quote(inner)} > /dev/null 2>&1 < /dev/null &"
    r = te(launch, timeout_s=60, workdir="/app", env=env)
    if r.rc != 0:
        raise SB.SandboxError(f"step launch failed rc={r.rc}: {r.stderr[-300:]}")
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    deadline = t0 + timeout_s + SB.CLIENT_GRACE_S + 60
    offset, sessions, killed, rc, incomplete = 0, 0, None, -1, False

    pace = {"every": CHECK_S, "n": 0, "retry": 5, "next": time.monotonic() + CHECK_S}

    def rc_file() -> tuple[str | None, bool]:
        """(the exit code if written, whether the container is gone). A failed check (the
        transport) doubles the interval up to CHECK_MAX_S; a good one resets it."""
        pace["next"] = time.monotonic() + pace["every"]  # one deadline for every call site
        try:
            x = te(f"[ -f {d}/rc ] && cat {d}/rc || echo PENDING", timeout_s=30, workdir="/")
        except SB.SandboxError:
            x = SB.ExecResult(1, "", "check failed", 0.0)
        if GONE in x.stderr or "is not running" in x.stderr:
            return None, True
        if x.rc != 0:
            pace["every"] = min(pace["every"] * 2, CHECK_MAX_S)
            pace["next"] = time.monotonic() + pace["every"]
            return None, False
        pace["every"] = CHECK_S
        pace["next"] = time.monotonic() + CHECK_S
        v = x.stdout.strip()
        return (None if v in ("", "PENDING") else v), False

    with open(log_path, "ab") as out:
        while True:
            sessions += 1
            argv = SB.docker_argv(
                ["exec", sb.name, "timeout", "-k", "5", str(FOLLOW_CHUNK_S), "tail", "-c",
                 f"+{offset + 1}", "-F", f"{d}/out"], sb.host)
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL)
            got = {"n": 0}

            def pump(p=p, got=got) -> None:
                assert p.stdout is not None
                for raw in iter(p.stdout.readline, b""):
                    if not raw.endswith(b"\n"):
                        break  # a partial line: re-read it from `offset` next session
                    out.write(raw)
                    out.flush()
                    got["n"] += len(raw)
                    if on_line is not None:
                        with contextlib.suppress(Exception):
                            on_line(raw.decode(errors="replace"))

            th = threading.Thread(target=pump, daemon=True)
            th.start()
            code, gone = None, False
            while p.poll() is None:
                if stop is not None and stop.is_set():
                    killed = "stop"
                    break
                if time.monotonic() > deadline:
                    killed = "client_timeout"
                    break
                if time.monotonic() >= pace["next"]:
                    pace["n"] += 1
                    if watch is not None and pace["n"] % WATCH_EVERY == 0:
                        abort = False
                        with contextlib.suppress(Exception):
                            abort = watch() is True
                        if abort:  # the watcher's own intervention failed: stop waiting
                            killed = "watch_abort"
                            break
                    code, gone = rc_file()
                    if code or gone:
                        time.sleep(2)  # let tail deliver the last lines
                        break
                time.sleep(0.5)
            if p.poll() is None:
                p.kill()
            p.wait(timeout=30)
            th.join(timeout=30)
            offset += got["n"]
            if killed:
                break
            if not (code or gone) and time.monotonic() >= pace["next"]:
                code, gone = rc_file()  # never more often than the shared deadline
            if gone:
                killed = "container_gone"
                break
            if code:
                for attempt in range(4):  # the final drain survives a transport drop too
                    rest = te(f"tail -c +{offset + 1} {d}/out", timeout_s=120, workdir="/")
                    if rest.rc == 0:
                        break
                    time.sleep(5 * (attempt + 1))
                else:
                    incomplete = True
                if rest.rc == 0 and rest.stdout:
                    out.write(rest.stdout.encode())
                    out.flush()
                    if on_line is not None:
                        for ln in rest.stdout.splitlines():
                            with contextlib.suppress(Exception):
                                on_line(ln + "\n")
                rc = int(code) if code.lstrip("-").isdigit() else -1
                break
            if time.monotonic() > deadline:
                killed = "client_timeout"
                break
            # the transport dropped or the chunk ended: follow again, backing off while the
            # follow sessions die at once (a refusing sshd)
            if got["n"] == 0:
                pace["retry"] = min(pace["retry"] * 2, 120)
            else:
                pace["retry"] = 5
            time.sleep(pace["retry"])
    if not killed or killed == "stop":
        for attempt in range(4):
            err = te(f"cat {d}/err 2>/dev/null", timeout_s=60, workdir="/")
            if err.rc == 0:
                break
            time.sleep(5 * (attempt + 1))
        else:
            incomplete = True
        with contextlib.suppress(OSError):
            Path(str(log_path) + ".err").write_text(err.stdout if err.rc == 0 else "")
    secs = round(time.monotonic() - t0, 1)
    res = StreamResult(rc, secs, rc in (124, 137), killed,
                       rc in (124, 137) and secs >= timeout_s - 30)
    res.reconnects = sessions - 1  # type: ignore[attr-defined]
    if incomplete:  # rc is real but the output isn't whole: callers must not trust it
        res.killed = "output_lost"
    return res


def processes(sb: SB.Sandbox) -> list[str]:
    """`ps` lines other than the sandbox's own main process (no judgement: the harness decides
    what its own CLI's process tree is)."""
    token = f"{SB.QUIESCE_TOKEN}-{uuid.uuid4().hex[:8]}"
    r = sb.exec(f": {token}; ps -eo pid=,ppid=,stat=,args=", timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"ps failed in {sb.name} rc={r.rc}: {r.stderr[-300:]}")
    # the hook's curl names the SimServer token; keep it out of boundary records
    return [re.sub(rf"({TOKEN_HEADER}: )\S+", r"\1<redacted>", line)
            for line in SB.stray_processes(r.stdout, token)]


def snapshot(sb: SB.Sandbox, keep: SS.KeepStore, k: int) -> dict:
    """A kept `docker commit --pause` of boundary k (KeepStore rules: listed before the commit
    starts, never re-pointing an existing tag). Unlike `Sandbox.snapshot` it does not require a
    quiescent container: the CLI is blocked in its own hook at this point; the caller records
    what else was running (`processes`). Returns {"tag", "snapshot_s"}."""
    # a restored container must wait for its own entrypoint, never see this one's record
    r = sb.exec(f"rm -f {ENTRYPOINT_RC}", timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"rm {ENTRYPOINT_RC}: {r.stderr[-200:]}")
    tag = keep.new_tag(k)
    if I.tag_exists(tag, sb.host):
        raise RuntimeError(f"kept tag {tag} already exists; refusing to overwrite it")
    with SB.using_host(sb.host):
        secs = sb._commit(tag, keep.labels())
    keep.record(k, tag, snapshot_s=secs)
    return {"tag": tag, "snapshot_s": secs}


# ------------------------------------------------------------------------------------------
# The simulator as an MCP server
# ------------------------------------------------------------------------------------------


class ProbeCaptured(RuntimeError):
    """A terminal probe ask: captured without consulting or changing the simulator."""


class SimServer:
    """The released `ask_user` MCP tool (FastMCP "simulated-user", streamable HTTP at /mcp; the
    same name, docstring and `question: str` signature as upstream server.py), answered by this
    session's `UserSim`, plus POST /emx/boundary for the harness's compaction hook.

    Every request must carry `X-Emx-Token: <token>` (403 otherwise): the server listens on the
    laptop's Tailscale IP. Asks are serialised (one simulator conversation). `rows` are the
    pilot's reply rows (`interact_run.EnvAsks`: reply_id, call_index, step, question, reply)
    plus `utc`; set `step` before each workflow step. `on_boundary(payload) -> dict` runs in a
    worker thread and its result is the response body; while it runs the CLI is blocked in its
    hook. A `RunCapReached` from the simulator calls `on_cap(error)` and answers the tool call
    with an error. `on_ask(row)` runs after each reply (e.g. for a harness that must count
    turns)."""

    def __init__(self, sim: I.UserSim, *, bind: str | None = None, port: int = 0,
                 token: str | None = None,
                 unix_socket: str | None = None,
                 on_boundary: Callable[[dict], dict] | None = None,
                 on_cap: Callable[[BaseException], None] | None = None,
                 on_ask: Callable[[dict], None] | None = None,
                 capture_ask: Callable[[dict], None] | None = None):
        self.sim = sim
        self.unix_socket = unix_socket
        self.bind = bind or ("127.0.0.1" if unix_socket else tailscale_ip())
        self.port = 0 if unix_socket else port or free_port(self.bind)
        self.token = token or secrets.token_urlsafe(24)
        self.on_boundary, self.on_cap, self.on_ask = on_boundary, on_cap, on_ask
        self.capture_ask = capture_ask
        self.on_audit: Callable[[dict], dict] | None = None
        self.audit_calls: list[dict] = []
        self.captured_asks: list[dict] = []
        self.capture_errors: list[dict] = []
        self.step = "01_plan"
        self.rows: list[dict] = []
        self.boundary_calls: list[dict] = []
        self.cap_error: str | None = None
        self._lock = threading.Lock()
        self._server = None
        self._thread: threading.Thread | None = None

    # --- the tool, without MCP (tests call it directly) ------------------------------------
    def ask(self, question: str) -> str:
        from compaction.s2.model import RunCapReached
        with self._lock:
            if self.capture_ask is not None:
                if self.captured_asks:
                    raise ProbeCaptured("probe already ended at its first ask_user")
                row = {"kind": "ask_user", "text": question, "step": self.step, "utc": utc()}
                # Preserve the observation before the callback can stop the CLI, cancel its
                # request, or fail. It is an observation, never a simulator reply.
                self.captured_asks.append(row)
                try:
                    self.capture_ask(copy.deepcopy(row))
                except Exception as exc:
                    self.capture_errors.append({"utc": utc(),
                                                "error": f"{type(exc).__name__}: {exc}"})
                    raise
                raise ProbeCaptured("probe window ended at ask_user")
            if hasattr(self.sim, "set_delivery_context"):
                self.sim.set_delivery_context(stage=self.step)
            if self.cap_error:
                raise RuntimeError(f"simulator stopped: {self.cap_error}")
            try:
                reply = self.sim.ask(question)
            except RunCapReached as e:
                self.cap_error = f"{type(e).__name__}: {e}"
                if self.on_cap is not None:
                    self.on_cap(e)
                raise
            st = self.sim.state
            row = {"reply_id": f"r{st.ask_user_call_count}",
                   "call_index": st.ask_user_call_count, "step": self.step,
                   "question": question, "reply": reply, "utc": utc()}
            self.rows.append(row)
        if self.on_ask is not None:
            self.on_ask(row)
        return reply

    def boundary(self, payload: dict) -> dict:
        t0 = time.monotonic()
        rec = {"utc": utc(), "payload_keys": sorted(payload)}
        try:
            out = self.on_boundary(payload) if self.on_boundary else {"ok": True}
            rec.update(ok=True, result=out)
            return out
        except Exception as e:  # noqa: BLE001 - reported to the hook and kept in boundary_calls
            rec.update(ok=False, error=f"{type(e).__name__}: {e}"[:2000])
            return {"ok": False, "error": rec["error"]}
        finally:
            rec["seconds"] = round(time.monotonic() - t0, 1)
            self.boundary_calls.append(rec)

    def audit(self, payload: dict) -> dict:
        """Return only a native hook response; errors stop the harness rather than fail open."""
        rec = {"utc": utc(), "phase": payload.get("hook_event_name")}
        try:
            if self.on_audit is None:
                raise RuntimeError("native audit handler missing")
            out = self.on_audit(payload)
            rec["ok"] = not (out.get("continue") is False)
            return out
        except Exception as exc:  # noqa: BLE001 - recorded and converted to a stop instruction
            rec.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            return {"continue": False, "stopReason": "native audit failed"}
        finally:
            self.audit_calls.append(rec)

    def export(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.sim.export())

    def mcp_config(self) -> dict:
        """The MCP client entry the CLIs get (Claude Code's .claude.json shape)."""
        return {"mcpServers": {"user-server": {"type": "http", "url": MCP_URL,
                                               "headers": {TOKEN_HEADER: self.token}}}}

    # --- serving ---------------------------------------------------------------------------
    def app(self):
        import anyio
        from fastmcp import FastMCP
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        mcp = FastMCP(SIM_SERVER_NAME)
        server = self

        async def ask_user(question: str) -> str:
            try:
                return await anyio.to_thread.run_sync(server.ask, question)
            except ProbeCaptured:
                # The capture callback has finished stopping the CLI. This is a terminal
                # observation, not an MCP tool failure or a synthetic user reply. Keep the
                # released string output schema, but send no text content. Ordinary simulator
                # and capture-callback exceptions still reach FastMCP as real errors.
                if server.capture_errors:
                    raise
                from fastmcp.tools.base import ToolResult
                return ToolResult(content=[], structured_content={"result": ""})

        ask_user.__doc__ = ASK_USER_DOC  # FastMCP takes the description from the docstring
        mcp.tool()(ask_user)

        @mcp.custom_route(BOUNDARY_PATH, methods=["POST"])
        async def boundary(request: Request):
            try:
                payload = await request.json()
            except ValueError:
                payload = {"raw": (await request.body()).decode(errors="replace")[:20000]}
            out = await anyio.to_thread.run_sync(server.boundary, payload)
            return JSONResponse(out, status_code=200 if out.get("ok") else 500)

        @mcp.custom_route(AUDIT_PATH, methods=["POST"])
        async def audit(request: Request):
            try:
                payload = await request.json()
                out = await anyio.to_thread.run_sync(server.audit, payload)
            except Exception as exc:  # noqa: BLE001 - hook failures must stop, not allow a tool
                server.audit_calls.append({"utc": utc(), "ok": False, "error": type(exc).__name__})
                out = {"continue": False, "stopReason": f"audit transport error: {type(exc).__name__}"}
            # The body is exactly Claude Code's hook schema, with no transport 'ok' field.
            return JSONResponse(out)

        app = mcp.http_app(path="/mcp", middleware=[_token_middleware(self.token)])
        return app

    def start(self, timeout_s: float = 30) -> SimServer:
        import uvicorn
        cfg = uvicorn.Config(self.app(), host=self.bind, port=self.port, uds=self.unix_socket, log_level="warning",
                             timeout_keep_alive=3600, lifespan="on")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True,
                                        name=f"simserver-{self.port}")
        self._thread.start()
        t0 = time.monotonic()
        while not getattr(self._server, "started", False):
            if time.monotonic() - t0 > timeout_s or not self._thread.is_alive():
                raise RuntimeError(f"SimServer did not start on {self.bind}:{self.port}")
            time.sleep(0.1)
        return self

    def stop(self) -> list[str]:
        """Stop serving; returns what did not stop (empty when the server thread is gone)."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                return [f"SimServer {self.bind}:{self.port} still running"]
        return []


def _token_middleware(token: str):
    from starlette.middleware import Middleware

    class TokenCheck:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                got = dict(scope.get("headers") or []).get(TOKEN_HEADER.lower().encode(), b"")
                if not got:  # clients that can't send headers (agy) put it in the query
                    from urllib.parse import parse_qs
                    q = parse_qs((scope.get("query_string") or b"").decode())
                    got = (q.get(TOKEN_QUERY) or [""])[0].encode()
                if not secrets.compare_digest(got, token.encode()):
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"content-length", b"0")]})
                    await send({"type": "http.response.body", "body": b""})
                    return
            await self.app(scope, receive, send)

    return Middleware(TokenCheck)


# ------------------------------------------------------------------------------------------
# Boundaries in the pi arm's layout
# ------------------------------------------------------------------------------------------


@dataclass
class NativeBoundary:
    """One native compaction, already converted to the pi arm's OpenAI-format messages.

    pre_messages: the context the model had before the compaction (a system message first,
      the step instruction as the first user message; ask_user calls named "ask_user" and their
      results as plain reply text, so `interact_run.reply_index` matches them).
    post_messages: what the CLI resumed with: the system message, the summary message at
      `summary_index`, then any verbatim tail the harness keeps (`kept_indices` index
      pre_messages; empty when nothing is kept).
    summary: the summary text as the model reads it (the whole summary message).
    native_files: name -> text or bytes, written under b<k>/native/ (the native transcript at
      the boundary, the hook payload, ...)."""
    pre_messages: list[dict]
    post_messages: list[dict]
    summary: str
    summary_index: int
    kept_indices: list[int]
    pre_tokens: int | None
    post_tokens: int | None
    trigger: str  # "auto" | "manual" | "forced"
    policy: str  # e.g. "claude-code-native"
    model: str
    info: dict = field(default_factory=dict)
    native_files: dict[str, str | bytes] = field(default_factory=dict)


def compaction_meta(bdir: Path, step: str | None = None) -> dict:
    """One compaction's pressure, as every harness records it (main session, 24 Oct: compare
    harnesses on real pressure): the measured pre_tokens, post_tokens, the summary's size
    and the verbatim messages kept, from b<k>/boundary/boundary.json."""
    f = json.loads((Path(bdir) / "boundary" / "boundary.json").read_text())
    return {"k": f.get("boundary_index"), "step": step or f.get("stage"),
            "pre_tokens": f.get("pre_tokens"), "post_tokens": f.get("post_tokens"),
            "summary_chars": f.get("summary_chars"),
            "n_kept": len(f.get("kept_indices") or []), "post_source": f.get("post_source"),
            "quiescent": f.get("quiescent")}


def next_k(run_dir: Path) -> int:
    return max(SS._existing_k(Path(run_dir)), default=0) + 1


def write_boundary(run_dir: Path, k: int, nb: NativeBoundary, *, run_json: dict,
                   snap: dict, sim_export: dict, sim_rows: list[dict], step: str,
                   step_instruction: str, fingerprint: str, sim_probe_sha256: str | None,
                   calls_in_step: int | None, extra: dict | None = None,
                   eligible: Callable[[Path, dict], bool] | None = None) -> Path:
    """Write b<k> in the layout `session._boundary` and `interact_run.run_base` produce, so
    `lossaudit.load`, `interact_reqs` and the fork trigger read it unchanged: boundary/
    {pre_messages, compaction, post_messages, record, sim_state, sim_replies}.json,
    step_instruction.txt, b<k>/run.json, b<k>/native/*, and boundary/boundary.json last (a
    boundary is complete only when it exists). `snap` = `snapshot()`'s {"tag", "snapshot_s"}
    plus "stopped_processes"/"processes"; `fingerprint` = `interact.fingerprint()` output.
    `eligible(bdir, fields)` (the fork trigger, as `session`'s stop_at_boundary) runs before
    boundary.json is written and sets fields["eligible"]; the caller reads it from the file."""
    from compaction.s2 import interact_run as IR
    run_dir = Path(run_dir)
    bdir = run_dir / f"b{k}"
    b = bdir / "boundary"
    if (b / "boundary.json").exists():
        raise FileExistsError(f"{b / 'boundary.json'} exists")
    rid = f"{SS._record_base(run_dir, run_json)}-b{k}"
    comp = K.Compaction(policy=nb.policy, requests=[], responses={}, summary=nb.summary,
                        kept_indices=list(nb.kept_indices), post_messages=nb.post_messages,
                        summary_index=nb.summary_index, usage=[],
                        info={"native": True, "trigger": nb.trigger, **nb.info})
    record = K.boundary_record(rid, nb.pre_messages, model=nb.model,
                               pre_tokens=int(nb.pre_tokens or 0), summary=nb.summary)
    record["harness"] = nb.policy
    record["trigger"] = nb.trigger
    _dump(b / "pre_messages.json", nb.pre_messages)
    _dump(b / "compaction.json", comp.to_dict())
    _dump(b / "post_messages.json", nb.post_messages)
    _dump(b / "record.json", record)
    _dump(bdir / "run.json", SS._boundary_run_json(run_json, k, snap.get("tag")))
    _dump(b / "sim_state.json", sim_export)
    _dump(b / "sim_replies.json", IR.reply_index(sim_rows, nb.pre_messages))
    (b / "step_instruction.txt").write_text(step_instruction)
    for name, data in nb.native_files.items():
        p = bdir / "native" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_text(data)
    prev_k = [j for j in SS._existing_k(run_dir) if j < k and SS._complete(run_dir / f"b{j}")]
    fields = {
        "tag": snap.get("tag"), "stopped_processes": snap.get("stopped_processes", []),
        "processes": snap.get("processes"), "snapshot_s": snap.get("snapshot_s"),
        "boundary_index": k, "stage": step, "calls_in_step": calls_in_step,
        "pre_tokens": nb.pre_tokens, "post_tokens": nb.post_tokens,
        "post_tokens_est": nb.post_tokens, "n_pre_messages": len(nb.pre_messages),
        "summary_chars": len(nb.summary), "summary_index": nb.summary_index,
        "kept_indices": list(nb.kept_indices),
        "tail_start_index": min(nb.kept_indices) if nb.kept_indices else len(nb.pre_messages),
        "policy": nb.policy, "update": k > 1 and bool(prev_k), "trigger": nb.trigger,
        "previous_boundary": prev_k[-1] if prev_k else None, "compaction_cost_usd": 0.0,
        "prompt_sha256": None, "update_prompt_sha256": None,
        "fingerprint_sha256": I.sha256(fingerprint),
        "fingerprint_version": I.FINGERPRINT_VERSION, "sim_probe_sha256": sim_probe_sha256,
        "sim_calls": sim_export["state"]["ask_user_call_count"], "eligible": None,
        "native": True, **(extra or {}),
    }
    if eligible is not None:
        fields["eligible"] = bool(eligible(bdir, fields))
    fields["written_utc"] = utc()
    _dump(b / "boundary.json", fields)
    return bdir


_CODE_VERSION: dict | None = None


def code_version() -> dict:
    """Which code made a run (main session, 24 Oct: record it per run): git HEAD, whether the
    checkout was clean, and the sha256 of each native module and of scripts/s2_native.py,
    taken ONCE, the first time a process asks (i.e. at launch), so a long-lived launcher
    reports the code it started with even if the files change later (astra delta2 r1 #4)."""
    global _CODE_VERSION
    if _CODE_VERSION is not None:
        return _CODE_VERSION
    import hashlib

    from compaction.s2 import config as C
    here = Path(__file__).parent
    # the audit scripts too: they are loaded when used (astra delta r3 #2)
    paths = [*sorted(here.glob("*.py")),
             *(C.REPO_ROOT / "scripts" / f for f in ("s2_native.py", "s2_interact_audit.py",
                                                     "s2_lossaudit.py"))]
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in paths
             if p.exists()}
    head = dirty = None
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        head = subprocess.run(["git", "-C", str(here), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10,
                              check=False).stdout.strip() or None
        dirty = bool(subprocess.run(
            ["git", "-C", str(C.REPO_ROOT), "status", "--porcelain", "--",
             "src/compaction/s2/native", "scripts/s2_native.py"],
            capture_output=True, text=True, timeout=10, check=False).stdout.strip())
    _CODE_VERSION = {"git_head": head, "dirty": dirty, "files": files, "utc": utc()}
    return _CODE_VERSION


def native_run_json(*, run_key: str, task_dict: dict, persona_sha256: str, arm: str,
                    model: str, policy: str, threshold: dict, cli_version: str,
                    extra: dict | None = None) -> dict:
    """run.json for a native base run: `interact_run.run_json`'s shape, plus `harness`."""
    from compaction.s2 import config as C
    return {"run_key": run_key, "created_utc": utc(), "task": task_dict, "code": code_version(),
            "plan": {"model": model, "policy": policy, "threshold": threshold,
                     "task": task_dict["task_id"], "stage": C.INTERACT_STAGE,
                     "tag": C.INTERACT_TAG, "harness": arm},
            "harness": {"arm": arm, "cli_version": cli_version, **(extra or {})},
            "sim": {"model": I.SimConfig().model, "route": C.SIM_ROUTE.name,
                    "reasoning": C.SIM_ROUTE.reasoning, "persona_sha256": persona_sha256,
                    "upstream": I.UPSTREAM, "server": f"fastmcp=={FASTMCP_VERSION}"}}


def available_gb(host: str | None) -> float | None:
    """MemAvailable on the Docker host in GB, including the local Linux daemon."""
    if (host is None or host.startswith("unix://")) and Path("/proc/meminfo").exists():
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
        except (OSError, ValueError, IndexError):
            return None
    if not host or not host.startswith("ssh://"):
        return None
    cmd = "awk '/MemAvailable/ {print $2}' /proc/meminfo"
    for attempt in range(3):  # a busy sshd (MaxStartups) can stall a connection: retry
        try:
            p = subprocess.run(["ssh", "-o", "ConnectTimeout=20", host.removeprefix("ssh://"),
                                cmd], capture_output=True, text=True, timeout=60,
                               check=False)
            return int(p.stdout.strip()) / 1024 / 1024
        except (subprocess.TimeoutExpired, ValueError, OSError):
            time.sleep(30 * 2 ** attempt)  # 30, 60, 120 s: never hammer a busy sshd
    return None  # unknown: the caller goes ahead (Docker's own --memory limit still holds)


def wait_for_memory(host: str | None, need_gb: float, wait_s: float = 1800,
                    poll_s: float = 30) -> float | None:
    """Block until the host has `need_gb` available (desktop rule, 24 Oct: agent containers
    start at >= 6 GB free, heavy ones at >= 10 GB). Raises after `wait_s`."""
    t0 = time.monotonic()
    while True:
        gb = available_gb(host)
        if gb is not None and gb >= need_gb:
            return gb
        if gb is None and not host:
            return None  # the local daemon: nothing to ask
        # unknown (the probe failed) holds like too little (astra delta r1 #6)
        if time.monotonic() - t0 > wait_s:
            have = "unknown" if gb is None else f"{gb:.1f} GB"
            raise SB.SandboxError(f"{host}: {have} available < {need_gb} GB for {wait_s:.0f}s")
        time.sleep(poll_s)


# ------------------------------------------------------------------------------------------
# One session's plumbing
# ------------------------------------------------------------------------------------------


@dataclass
class NativeSession:
    """Invocation + SimServer + Egress + task container, torn down together (in reverse).

        with NativeSession(arm, image, sim, host=...) as ns:
            ns.sb (the task container), ns.server (SimServer), ns.egress, ns.inv

    `extra_args` go to `docker run` (e.g. a read-only CLI volume, a tmpfs). The simulator is
    built by `sim_factory(runner)` against the container, as `InteractEnv.start` does."""
    arm: str
    image: str
    sim_factory: Callable[[I.Runner], I.UserSim]
    host: str | None = None
    extra_args: tuple[str, ...] = ()
    cpus: float = AGENT_CPUS
    memory_mb: int = AGENT_MEMORY_MB
    min_free_gb: float | None = AGENT_MIN_FREE_GB
    lifetime_s: int = I.CONTAINER_LIFETIME_S
    setup_git: bool = True
    allow: tuple[str, ...] | None = None
    on_boundary: Callable[[dict], dict] | None = None
    on_cap: Callable[[BaseException], None] | None = None
    before_close: Callable[[], list[str]] | None = None  # e.g. drain capture workers first
    inv: SB.Invocation | None = None
    sb: SB.Sandbox | None = None
    server: SimServer | None = None
    egress: Egress | None = None
    uname: dict = field(default_factory=dict)
    entrypoint_rc: int | None = None
    exec_stats: dict = field(default_factory=dict)  # client_timeouts, settles
    services: frozenset[int] = frozenset()  # the entrypoint's processes (never frozen)
    cleanup_unconfirmed: list[str] = field(default_factory=list)

    def __enter__(self) -> Self:
        if self.min_free_gb:
            wait_for_memory(self.host, self.min_free_gb)
        self.inv = SB.Invocation(purpose=purpose(self.arm), host=self.host)
        self._sim_socket_dir = None
        try:
            holder: dict = {}

            def runner(cmd: str, t: int):
                return holder["run"](cmd, t)

            sim = self.sim_factory(runner)
            socket_path = None
            if sys.platform.startswith("linux") and (self.host is None or self.host.startswith("unix://")):
                self._sim_socket_dir = tempfile.TemporaryDirectory(prefix="emx-native-sim-")
                socket_path = str(Path(self._sim_socket_dir.name) / "server.sock")
            self.server = SimServer(sim, on_boundary=self._boundary, on_cap=self.on_cap,
                                    unix_socket=socket_path)
            self.server.start()
            self.egress = Egress(self.arm, self.server.bind, self.server.port, host=self.host,
                                 run_id=self.inv.run_id, allow=self.allow,
                                 lifetime_s=self.lifetime_s + 600, sim_socket=socket_path)
            net = self.egress.up()
            self.sb = start_task_container(self.inv, self.image, network=net,
                                           extra_args=self.extra_args,
                                           lifetime_s=self.lifetime_s, cpus=self.cpus,
                                           memory_mb=self.memory_mb)
            self.sb.exec_stats = self.exec_stats  # tolerant execs count here
            holder["run"] = I.sandbox_runner(self.sb, stats=self.exec_stats)  # tolerant
            if image_entrypoint(self.image, self.host):  # services first (refactoring tasks)
                self.entrypoint_rc = wait_entrypoint(self.sb)
                procs, tok = ps_table(self.sb)
                mine = _tree(procs, {p for p, v in procs.items() if tok in v[2]})
                self.services = frozenset(
                    p for p in procs if p != 1 and p not in mine
                    and not (procs[p][0] == 1 and procs[p][2] == "sleep infinity"))
            mark_session_start(self.sb)
            if self.setup_git:
                r = self.sb.exec(I.NOSHIM + I.SETUP_CMD, timeout_s=120)
                if r.rc != 0:
                    raise RuntimeError(f"git setup failed rc={r.rc}: {r.stderr[-500:]}")
            u = self.sb.exec("uname -s; uname -r; uname -v; uname -m", timeout_s=60)
            self.uname = dict(zip(("system", "release", "version", "machine"),
                                  u.stdout.splitlines(), strict=False))
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _boundary(self, payload: dict) -> dict:
        if self.on_boundary is None:
            return {"ok": True, "ignored": True}
        return self.on_boundary(payload)

    def __exit__(self, *exc) -> None:
        bad: list[str] = []
        steps = [("before_close", lambda: self.before_close() if self.before_close else []),
                 ("containers", lambda: self.inv.cleanup() if self.inv else []),
                 ("egress", lambda: self.egress.down() if self.egress else []),
                 ("server", lambda: self.server.stop() if self.server else []),
                 ("sim_socket", lambda: self._sim_socket_dir.cleanup()
                  if getattr(self, "_sim_socket_dir", None) else [])]
        for name, fn in steps:  # each one runs whatever the others did
            try:
                bad += list(fn() or [])
            except Exception as e:  # noqa: BLE001 - reported, the rest still runs
                bad.append(f"{name}: {type(e).__name__}: {e}"[:300])
        self.cleanup_unconfirmed = bad
        if bad:
            print(f"[native] cleanup NOT confirmed for: {bad}")


def as_dict(obj) -> dict:
    return asdict(obj)
