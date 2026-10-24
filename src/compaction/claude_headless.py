"""Claude Code headless (`claude -p`) on Jacob's Max plan: the Anthropic backend for S1 (unit U2a).

Spec: docs/sprint/specs/S1.md, "U2a src/compaction/claude_headless.py (Max plan)" and "Backend
interface"; the stream-json shapes parsed here are the Phase 0 smoke outputs in
`data/raw/sprint/s1/smoke/` (docs/sprint/prep/l-s1-smoke.md). Revised after astra review r1
(`logs/reviews/20261020-s1-u2a-r1.md`) and the Max throughput benchmark (same smoke note).

Billing safety, in layers:
- `clean_env` strips every variable that would route Claude Code to the API or a cloud provider
  (and the generation overrides `CLAUDE_CODE_EFFORT_LEVEL` / `CLAUDE_CODE_MAX_OUTPUT_TOKENS`);
  `check_auth` requires a logged-in claude.ai Max account; `check_managed_settings` refuses
  local policy (managed-settings JSON and drop-ins, the macOS managed-preferences plists, and
  `defaults export com.anthropic.claudecode`) that adds hooks, MCP servers, plugins, an API-key
  helper or routing env, and records which sources it checked.
- Known limitation: server-managed policy (settings an organisation admin pushes from
  claude.ai) can't be inspected locally. It is accepted for this personal Max account, which has
  no org admin; the per-call checks below still catch a changed billing route.
- Each `run()` first sends a dedicated tiny canary ("Reply with OK."), alone; research requests
  fan out only after its events show `apiKeySource: "none"` and extra usage disabled
  (`isUsingOverage: false`, `overageStatus: "rejected"` or absent with a disabled reason).
  Accepted limitation (astra r3): there is no non-generating way to read the overage status,
  so the canary itself runs before it is known; the org reports overage disabled at org level,
  and the canary is a 64-token "OK" request, so the worst case is a fraction of a cent.
- Every event of every call is checked as it streams: an API-key route or extra usage that
  isn't disabled stops new launches at once, kills the batch's sessions and raises; the ledger
  note then says PAID_ROUTE_DETECTED and carries the notional cost (`total_cost_usd`) as `usd`.
  An answer whose route evidence is incomplete is returned as error="route_unverified" and is
  not cached.

Process hygiene: every subprocess (auth included) runs in its own process group with output in
temp files; the group is killed on exit, timeout and exception; `job_guard` turns SIGTERM, SIGINT
and SIGHUP into a stop + kill of every registered group before the process exits. Process-group
handling is ported from `src/compaction/agy.py` (`_group_alive`, `_kill_group`).

`run_session` is the shared runner that U4's native /compact arm reuses with `--resume` args.
"""

from __future__ import annotations

import atexit
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import logging
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from compaction.s1_types import STAGE, Req, Resp, est_tokens, fits, validate

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "raw" / "sprint" / "s1" / "cache" / "haiku-max"
DEFAULT_USAGE_PATH = REPO_ROOT / "data" / "raw" / "sprint" / "s1" / "usage" / "max_usage.jsonl"
CLAUDE_BIN_ENV = "CLAUDE_HEADLESS_BIN"  # override of the executable (default: `claude` on PATH)

# Managed (policy) settings Claude Code honours on macOS; module constants so tests can override.
MANAGED_DIR = Path("/Library/Application Support/ClaudeCode")
MANAGED_SETTINGS_FILES: tuple[Path, ...] = (
    MANAGED_DIR / "managed-settings.json",
    MANAGED_DIR / "managed-mcp.json",
)
MANAGED_SETTINGS_DIRS: tuple[Path, ...] = (MANAGED_DIR / "managed-settings.d",)
# macOS managed preferences (MDM configuration profiles) for the same settings.
MANAGED_DEFAULTS_DOMAIN = "com.anthropic.claudecode"
MANAGED_PLIST_PATHS: tuple[Path, ...] = (
    Path("/Library/Managed Preferences") / f"{MANAGED_DEFAULTS_DOMAIN}.plist",
    Path("/Library/Managed Preferences")
    / os.environ.get("USER", "")
    / f"{MANAGED_DEFAULTS_DOMAIN}.plist",
)
# `defaults export` prints an empty plist (rc 0) for a domain that doesn't exist
MANAGED_DEFAULTS_CMD: tuple[str, ...] | None = ("defaults", "export", MANAGED_DEFAULTS_DOMAIN, "-")

DEFAULT_TIMEOUT_S = 1800.0
KILL_GRACE_S = 5.0  # SIGTERM -> SIGKILL
POLL_S = 0.05
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_SYSTEM_BYTES = 200_000  # the system prompt goes on argv (macOS ARG_MAX is 1 MiB with env)
AUTH_TTL_S = 600.0
MAX_CANARIES = 3  # canary calls tried, one at a time, to establish the billing route
CANARY_KEY = "__canary__"
CANARY_SYSTEM = "Reply with OK."
CANARY_USER = "OK"
CANARY_MAX_OUTPUT = 64

LIMIT_MARGIN_S = 120.0  # wait until resetsAt + this
LIMIT_FALLBACK_WAIT_S = 900.0  # when a limit names no reset time
LIMIT_POLL_S = 900.0  # while paused, a canary every 15 min checks whether calls are allowed again
MAX_POLL_ERRORS = 8  # consecutive polls failing for a non-limit reason (2 h) before aborting
MAX_LIMIT_WAITS = 12  # per call, before giving up with error="limit" (canary polls don't count)
OK_LIMIT_STATUSES = frozenset({"allowed", "allowed_warning"})
SCHEMA_RETRY_LINE = "Return your answer only by calling the StructuredOutput tool."

STRIP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    # the setup token in .env is for the native-harness containers; host calls use the
    # keychain Max login, which check_auth verifies (24 Oct)
    "CLAUDE_CODE_OAUTH_TOKEN",
)
# generation overrides: set per call, never inherited
GEN_ENV = ("CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_MAX_OUTPUT_TOKENS")
_ROUTING_ENV_RE = re.compile(
    r"API_KEY|AUTH_TOKEN|BASE_URL|USE_BEDROCK|USE_VERTEX|USE_FOUNDRY|EFFORT|MAX_OUTPUT",
    re.IGNORECASE,
)

TOO_LONG_RE = re.compile(
    r"prompt is too long|input is too long|too many (input )?tokens|"
    r"context (window|length) (exceeded|limit)|exceeds? (the )?(model'?s? )?(maximum )?context",
    re.IGNORECASE,
)
LIMIT_RE = re.compile(
    r"usage limit|rate limit|limit reached|limit will reset|out of (extra )?usage|"
    r"(5|five)[- ]hour limit|weekly limit|session limit",
    re.IGNORECASE,
)
_RESET_EPOCH_RE = re.compile(r"(?:\||resets? at\s*)(\d{10})\b", re.IGNORECASE)


class MaxAuthError(RuntimeError):
    """`claude auth status` isn't a logged-in claude.ai Max account."""


class ManagedSettingsError(RuntimeError):
    """Policy settings could alter the judge's context or billing route."""


class BillingRouteError(RuntimeError):
    """A call ran (or may have run) on something other than the subscription: abort the batch."""

    def __init__(self, message: str, resp: Resp | None = None):
        super().__init__(message)
        self.resp = resp


class SessionTimeout(Exception):
    """run_session hit its timeout; the process group was killed. `events` holds what was read."""

    def __init__(self, events: list[dict]):
        super().__init__("claude session timed out")
        self.events = events


class _Stopped(Exception):
    """The batch is aborting; a worker gives up before launching."""


def _utc(ts: float | None = None) -> str:
    t = datetime.now(UTC) if ts is None else datetime.fromtimestamp(ts, UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def default_bin() -> str:
    return os.environ.get(CLAUDE_BIN_ENV) or "claude"


def clean_env(
    max_output_tokens: int | None = None,
    base: Mapping[str, str] | None = None,
    effort: str | None = None,
) -> dict[str, str]:
    """The environment without API/provider credentials or inherited generation overrides, with
    compaction and auto-update off. `CLAUDE_CODE_MAX_OUTPUT_TOKENS` / `CLAUDE_CODE_EFFORT_LEVEL`
    are set only from the arguments (per request)."""
    env = dict(os.environ if base is None else base)
    for k in STRIP_ENV + GEN_ENV:
        env.pop(k, None)
    env["DISABLE_COMPACT"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    if max_output_tokens is not None:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(max_output_tokens))
    if effort is not None:
        env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
    return env


# ---------------------------------------------------------------- process groups

_LIVE: dict[int, tuple[subprocess.Popen, object]] = {}
_LIVE_LOCK = threading.Lock()


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # macOS answers EPERM for a group left with only zombies
        return False
    return True


def _kill_group(proc: subprocess.Popen, wait_s: float = KILL_GRACE_S) -> str | None:
    """Kill every process still in the session's group (ported from agy.py): SIGTERM, then
    SIGKILL for anything left after `wait_s`. Returns the strongest signal sent, None if the group
    was already gone, or "SIGKILL_FAILED" if processes remain."""
    pgid = proc.pid  # start_new_session: the child leads its own group
    proc.poll()
    if not _group_alive(pgid):
        return None
    sent = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return sent
        sent = sig.name
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            proc.poll()  # reap the leader so its zombie does not keep the group alive
            if not _group_alive(pgid):
                _reap(proc)
                return sent
            time.sleep(0.02)
    proc.poll()
    if _group_alive(pgid):
        log.error("processes remain in process group %d after SIGKILL", pgid)
        return "SIGKILL_FAILED"
    _reap(proc)
    return sent


def _reap(proc: subprocess.Popen) -> None:
    """Collect the dead leader (a group of zombies reads as gone before it is reaped)."""
    with contextlib.suppress(Exception):
        proc.wait(timeout=1.0)


def _snapshot() -> list[tuple[subprocess.Popen, object]]:
    """The live sessions, read without the lock (safe from a signal handler: list() over a dict
    runs in C under the GIL; a concurrent resize raises RuntimeError, so retry)."""
    for _ in range(50):
        try:
            return list(_LIVE.values())
        except RuntimeError:
            continue
    return []


def kill_sessions(owner: object | None = None) -> list[int]:
    """Kill the process groups of live sessions (all, or those started for `owner`). Takes no
    lock, so a signal handler can call it while the interrupted code holds `_LIVE_LOCK`."""
    procs = [p for p, o in _snapshot() if owner is None or o is owner]
    for p in procs:
        _kill_group(p, wait_s=1.0)
    return [p.pid for p in procs]


atexit.register(kill_sessions)

_GUARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


# Main-thread launch window: a guarded signal arriving between Popen and the cleanup `try` is
# deferred (recorded here) and re-raised inside the try, so the new group can't be missed.
# (Blocking the signals with pthread_sigmask instead would be inherited by the child: claude
# would start with SIGTERM blocked.)
_LAUNCH: dict[str, Any] = {"active": False, "pending": None}


def _launch_begin() -> bool:
    if threading.current_thread() is not threading.main_thread():
        return False
    _LAUNCH["pending"] = None
    _LAUNCH["active"] = True
    return True


def _launch_end(main: bool) -> None:
    """Close the launch window; re-raise a deferred signal (call inside the cleanup try)."""
    if not main:
        return
    _LAUNCH["active"] = False
    pend, _LAUNCH["pending"] = _LAUNCH["pending"], None
    if pend is not None:
        signal.raise_signal(pend)


# Set by the signal handler (plain assignments only: the handler takes no lock of any kind).
_SIGNALS: dict[str, Any] = {"last": None, "count": 0}


def _signal_kill() -> None:
    """From a signal handler: SIGTERM every registered group, lock-free (os.killpg only). The
    interrupted code's own cleanup (`finally` blocks, the batch abort loop) escalates to
    SIGKILL and reaps."""
    for p, _ in _snapshot():
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGTERM)


@contextlib.contextmanager
def job_guard(stop: threading.Event | None = None) -> Iterator[None]:
    """Make SIGTERM, SIGINT and SIGHUP stop the job cleanly.

    The handler takes no locks: it records the signal in `_SIGNALS`, SIGTERMs every registered
    process group from the lock-free snapshot, and then exits from the main thread
    (KeyboardInterrupt for SIGINT, SystemExit(128 + signum) otherwise, or the previous Python
    handler if there was one), deferred via `_LAUNCH` while a launch is in progress. A daemon
    watcher thread polls `_SIGNALS` every 50 ms and sets `stop` (Event.set takes the event's
    lock, so the handler must not). Previous handlers are restored on exit; ignored signals stay
    ignored. A no-op outside the main thread (signal handlers can't be set there; the main
    thread's guard covers the process)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    prev: dict[int, Any] = {}

    def handler(signum, frame):
        _SIGNALS["last"] = signum
        _SIGNALS["count"] += 1
        _signal_kill()
        if _LAUNCH["active"]:  # mid-launch: defer until the new group is registered
            _LAUNCH["pending"] = signum
            return
        p = prev.get(signum)
        if callable(p) and p is not signal.default_int_handler:
            p(signum, frame)
            return
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    done = threading.Event()  # the watcher's own exit; never touched by the handler

    def watch() -> None:
        seen = _SIGNALS["count"]
        while True:
            exiting = done.wait(0.05)
            if _SIGNALS["count"] != seen:  # checked once more on exit: no signal is missed
                seen = _SIGNALS["count"]
                if stop is not None:
                    stop.set()
            if exiting:
                return

    watcher = None
    if stop is not None:
        watcher = threading.Thread(target=watch, name="job_guard-watch", daemon=True)
        watcher.start()
    for s in _GUARDED:
        cur = signal.getsignal(s)
        if cur == signal.SIG_IGN:
            continue
        prev[s] = cur
        signal.signal(s, handler)
    try:
        yield
    finally:
        for s, p in prev.items():
            signal.signal(s, p if p is not None else signal.SIG_DFL)
        done.set()
        if watcher is not None:
            watcher.join(timeout=1.0)


def _spawn(
    cmd: list[str],
    cwd: str | Path,
    env: Mapping[str, str],
    stdin: str | bytes | None,
    timeout_s: float,
    kill_grace_s: float,
    owner: object | None,
    on_event: Callable[[dict], bool] | None = None,
    keep_stdout: bool = False,
    stop: threading.Event | None = None,
) -> dict:
    """Run `cmd` in its own process group; parse stdout's JSON lines as they arrive.

    Launch, registration and cleanup are one lifecycle: in the main thread a guarded signal that
    arrives from before `Popen` until the cleanup `try` is entered is deferred and re-raised
    inside it, so the new group is killed. `on_event(ev) -> True`, or `stop` being set, kills the
    session at once (`stopped`). The group is killed in a `finally` whatever happens. On an
    exceptional exit after launch, the exception carries the events read so far (drained after
    the kill) as `exc.claude_events`. Returns {"events", "stdout" (if keep_stdout), "stderr",
    "returncode", "killed", "timed_out", "stopped", "duration_s"}."""
    t0 = time.monotonic()
    io_dir = tempfile.mkdtemp(prefix="claude-io-")
    out_path = os.path.join(io_dir, "out")
    events: list[dict] = []
    raw = bytearray()
    buf = b""
    timed_out = stopped = launched = False
    killed = None
    stderr = ""
    proc: subprocess.Popen | None = None

    def feed(chunk: bytes, final: bool = False) -> bool:
        nonlocal buf
        if keep_stdout and len(raw) < MAX_OUTPUT_BYTES:
            raw.extend(chunk)
        buf += chunk
        *lines, buf = buf.split(b"\n")
        if final and buf:
            lines.append(buf)
            buf = b""
        stop_now = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(ev, dict):
                events.append(ev)
                if on_event is not None and on_event(ev):
                    stop_now = True
        return stop_now

    try:
        with (
            open(out_path, "wb") as w_out,
            open(out_path, "rb", buffering=0) as r_out,
            tempfile.TemporaryFile() as in_f,
            tempfile.TemporaryFile() as err_f,
        ):
            shutil.rmtree(io_dir, ignore_errors=True)  # unlinked now; the open fds stay valid
            if stdin is not None:
                in_f.write(stdin.encode("utf-8") if isinstance(stdin, str) else stdin)
                in_f.seek(0)
            main = _launch_begin()
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    env=dict(env),
                    stdin=in_f if stdin is not None else subprocess.DEVNULL,
                    stdout=w_out,
                    stderr=err_f,
                    start_new_session=True,  # own process group, killed as a whole
                )
                launched = True
                with _LIVE_LOCK:
                    _LIVE[proc.pid] = (proc, owner)
            except BaseException:
                if main:
                    _LAUNCH["active"] = False
                raise
            deadline = t0 + timeout_s
            try:
                _launch_end(main)  # a deferred signal is raised here, inside the try
                while True:
                    rc = proc.poll()
                    if feed(r_out.read() or b""):
                        stopped = True
                        break
                    if rc is not None:
                        break
                    if stop is not None and stop.is_set():
                        stopped = True
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    time.sleep(POLL_S)
            finally:  # always: clear the whole group, forget it, drain what it wrote
                killed = _kill_group(proc, kill_grace_s)
                with _LIVE_LOCK:
                    _LIVE.pop(proc.pid, None)
                try:
                    if feed(r_out.read() or b"", final=True):
                        stopped = True
                    err_f.seek(0)
                    stderr = err_f.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace")
                except Exception:
                    log.exception("draining claude output failed")
    except BaseException as exc:
        if launched:
            exc.claude_events = events  # type: ignore[attr-defined]
            exc.claude_process = {  # type: ignore[attr-defined]
                "type": "_process",
                "returncode": proc.returncode if proc else None,
                "stderr_tail": stderr[-2000:],
                "killed": killed,
                "timed_out": False,
                "stopped": stopped,
                "interrupted": type(exc).__name__,
                "duration_s": round(time.monotonic() - t0, 3),
            }
        raise
    finally:
        shutil.rmtree(io_dir, ignore_errors=True)
    assert proc is not None
    return {
        "events": events,
        "stdout": bytes(raw).decode("utf-8", errors="replace") if keep_stdout else None,
        "stderr": stderr,
        "returncode": proc.returncode,
        "killed": killed,
        "timed_out": timed_out,
        "stopped": stopped,
        "duration_s": round(time.monotonic() - t0, 3),
    }


def parse_events(stdout: str) -> list[dict]:
    """stream-json lines as dicts; non-JSON lines are skipped."""
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def run_session(
    cwd: str | Path,
    args: list[str],
    stdin: str | bytes | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    *,
    claude_bin: str | None = None,
    env: Mapping[str, str] | None = None,
    kill_grace_s: float = KILL_GRACE_S,
    owner: object | None = None,
    on_event: Callable[[dict], bool] | None = None,
    stop: threading.Event | None = None,
) -> list[dict]:
    """Run `claude <args>` in `cwd` and return its parsed stream-json events.

    `stdin=None` gives /dev/null (as `claude -p "/compact" ... < /dev/null` needs). `env` defaults
    to `clean_env()` (which disables /compact too: pass your own env for the native arm). Events
    are parsed while the session runs; `on_event(ev)` returning True kills it at once. The last
    element is always a synthetic `{"type": "_process", "returncode", "stderr_tail", "killed",
    "timed_out", "stopped", "duration_s"}` event. On timeout the whole process group is killed and
    SessionTimeout (carrying the events) is raised; the group is also killed on a normal exit
    (stragglers) and on any exception, KeyboardInterrupt included."""
    r = _spawn(
        [claude_bin or default_bin(), *args],
        cwd,
        env if env is not None else clean_env(),
        stdin,
        timeout_s,
        kill_grace_s,
        owner,
        on_event,
        stop=stop,
    )
    events = r["events"]
    events.append(
        {
            "type": "_process",
            "returncode": r["returncode"],
            "stderr_tail": r["stderr"][-2000:],
            "killed": r["killed"],
            "timed_out": r["timed_out"],
            "stopped": r["stopped"],
            "duration_s": r["duration_s"],
        }
    )
    if r["timed_out"]:
        raise SessionTimeout(events)
    return events


def check_auth(claude_bin: str | None = None, timeout_s: float = 60.0) -> dict:
    """Run `claude auth status` (own process group, killed in a finally); raise MaxAuthError
    unless it exits 0 with `loggedIn`, `authMethod == "claude.ai"` and `subscriptionType ==
    "max"`."""
    cwd = tempfile.mkdtemp(prefix="claude-auth-")
    try:
        r = _spawn(
            [claude_bin or default_bin(), "auth", "status"],
            cwd, clean_env(), None, timeout_s, 1.0, None, keep_stdout=True,
        )  # fmt: skip
    except OSError as e:
        raise MaxAuthError(f"`claude auth status` failed: {e}") from e
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    if r["timed_out"] or r["returncode"] != 0:
        raise MaxAuthError(
            f"`claude auth status` rc={r['returncode']} timed_out={r['timed_out']}: "
            f"{(r['stderr'] or r['stdout'] or '')[-300:]!r}"
        )
    try:
        st = json.loads(r["stdout"])
    except json.JSONDecodeError as e:
        raise MaxAuthError(f"`claude auth status` gave no JSON: {r['stdout'][-300:]!r}") from e
    if (
        not isinstance(st, dict)
        or st.get("loggedIn") is not True
        or st.get("authMethod") != "claude.ai"
        or st.get("subscriptionType") != "max"
    ):
        got = st if not isinstance(st, dict) else {
            k: st.get(k) for k in ("loggedIn", "authMethod", "subscriptionType")
        }  # fmt: skip
        raise MaxAuthError(f"not a logged-in claude.ai Max account: {got}")
    return st


def _policy_problems(src: str, s: Any) -> list[str]:
    """Why one policy source (settings JSON or managed-preferences dict) is refused."""
    if not isinstance(s, dict):
        return [f"{src}: not an object"]
    out = []
    for k in ("hooks", "mcpServers", "enabledMcpjsonServers", "enabledPlugins"):
        if s.get(k):
            out.append(f"{src}: defines {k}")
    if s.get("apiKeyHelper"):
        out.append(f"{src}: defines apiKeyHelper")
    env = s.get("env") or {}
    bad = sorted(k for k in env if _ROUTING_ENV_RE.search(k)) if isinstance(env, dict) else []
    if bad:
        out.append(f"{src}: env sets {bad}")
    return out


def check_managed_settings(
    files: tuple[Path, ...] | None = None, dirs: tuple[Path, ...] | None = None
) -> str:
    """Read the managed (policy) settings Claude Code honours even under --safe-mode; raise
    ManagedSettingsError if any defines hooks, MCP servers, plugins, an API-key helper, or env
    that sets credentials, base URLs, providers or generation overrides. Returns "none" when no
    source has content, else the sha256 of the sources' names and bytes. The sources checked
    and their state are left in `LAST_MANAGED_SOURCES`."""
    report = managed_sources(files, dirs)
    problems = [p for r in report.values() for p in r.get("problems", [])]
    if problems:
        raise ManagedSettingsError("managed settings refused: " + "; ".join(problems))
    shas = sorted((k, r["sha256"]) for k, r in report.items() if r.get("sha256"))
    if not shas:
        return "none"
    return _sha(json.dumps(shas))


LAST_MANAGED_SOURCES: dict[str, dict] = {}


def managed_sources(
    files: tuple[Path, ...] | None = None, dirs: tuple[Path, ...] | None = None
) -> dict[str, dict]:
    """Every local policy source: {name: {"state": "absent"|"empty"|"present", "sha256",
    "problems"}}. Sources: the managed-settings JSON files and drop-in directory, the managed
    preferences plists, and `defaults export <MANAGED_DEFAULTS_DOMAIN> -` (when
    MANAGED_DEFAULTS_CMD is set)."""
    files = MANAGED_SETTINGS_FILES if files is None else files
    dirs = MANAGED_SETTINGS_DIRS if dirs is None else dirs
    report: dict[str, dict] = {}

    def add(name: str, data: bytes | None, parse: Callable[[bytes], Any]) -> None:
        if data is None:
            report[name] = {"state": "absent"}
            return
        try:
            obj = parse(data)
        except Exception as e:  # noqa: BLE001 - an unreadable policy is refused, not skipped
            report[name] = {"state": "present", "sha256": _sha_bytes(data),
                            "problems": [f"{name}: unreadable ({type(e).__name__})"]}  # fmt: skip
            return
        if obj in ({}, None):
            report[name] = {"state": "empty"}
            return
        report[name] = {"state": "present", "sha256": _sha_bytes(data),
                        "problems": _policy_problems(name, obj)}  # fmt: skip

    for p in files:
        add(str(p), p.read_bytes() if p.is_file() else None, json.loads)
    for d in dirs:
        found = sorted(p for p in d.glob("*.json") if p.is_file()) if d.is_dir() else []
        if not found:
            report[str(d)] = {"state": "absent"}
        for p in found:
            add(str(p), p.read_bytes(), json.loads)
    for p in MANAGED_PLIST_PATHS:
        add(str(p), p.read_bytes() if p.is_file() else None, plistlib.loads)
    if MANAGED_DEFAULTS_CMD:
        name = "defaults:" + MANAGED_DEFAULTS_DOMAIN
        cwd = tempfile.mkdtemp(prefix="claude-defaults-")
        try:
            r = _spawn(list(MANAGED_DEFAULTS_CMD), cwd, dict(os.environ), None, 30.0, 1.0, None,
                       keep_stdout=True)  # fmt: skip
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        if r["returncode"] != 0 or r["timed_out"]:
            report[name] = {"state": "present", "sha256": None,
                            "problems": [f"{name}: export failed rc={r['returncode']}"]}  # fmt: skip
        else:
            add(name, (r["stdout"] or "").encode("utf-8"), plistlib.loads)
    LAST_MANAGED_SOURCES.clear()
    LAST_MANAGED_SOURCES.update(report)
    return report


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- interpreting one call


def _infos(events: list[dict]) -> list[dict]:
    return [e.get("rate_limit_info") or {} for e in events if e.get("type") == "rate_limit_event"]


def _overage_problem(info: dict) -> str | None:
    """Why a rate-limit snapshot fails the no-extra-usage rule, or None."""
    if info.get("isUsingOverage") is not False:
        return f"isUsingOverage={info.get('isUsingOverage')!r}"
    status = info.get("overageStatus")
    if status == "rejected" or (status is None and info.get("overageDisabledReason")):
        return None
    return f"overageStatus={status!r} overageDisabledReason={info.get('overageDisabledReason')!r}"


def _paid_event(ev: dict) -> str | None:
    """Why this single event shows a route other than subscription-only, or None (checked on
    every event of every call, as it streams)."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init" and ev.get("apiKeySource") != "none":
        return f"init apiKeySource={ev.get('apiKeySource')!r}"
    if t == "rate_limit_event":
        why = _overage_problem(ev.get("rate_limit_info") or {})
        return f"rate_limit_event {why}" if why else None
    return None


def route_evidence(events: list[dict]) -> dict:
    """The billing-route evidence of one call (last init, last rate-limit snapshot)."""
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    infos = _infos(events)
    last = infos[-1] if infos else {}
    return {
        "api_key_source": inits[-1].get("apiKeySource") if inits else None,
        "rate_limit_seen": bool(infos),
        "is_using_overage": last.get("isUsingOverage") if infos else None,
        "overage_status": last.get("overageStatus") if infos else None,
        "overage_disabled_reason": last.get("overageDisabledReason") if infos else None,
    }


def verify_route(ev: dict | None) -> str:
    """ "ok" (subscription only, extra usage disabled), "bad" (evidence against) or "unknown"."""
    if not ev:
        return "unknown"
    if ev["api_key_source"] is not None and ev["api_key_source"] != "none":
        return "bad"
    if ev["rate_limit_seen"] and _overage_problem(
        {
            "isUsingOverage": ev["is_using_overage"],
            "overageStatus": ev["overage_status"],
            "overageDisabledReason": ev["overage_disabled_reason"],
        }
    ):
        return "bad"
    if ev["api_key_source"] == "none" and ev["rate_limit_seen"]:
        return "ok"
    return "unknown"


def _limit_from(events: list[dict], result: dict | None, now: float) -> dict | None:
    """A usage-limit hit: {"until": epoch, "window": str, "status": str|None, "util": float|None},
    or None. The wait runs to the latest blocking reset named by a rejected snapshot or the
    result message."""
    infos = _infos(events)
    bad = [i for i in infos if i.get("status") not in OK_LIMIT_STATUSES]
    res_hit = False
    msg = ""
    if result is not None and (result.get("is_error") or result.get("subtype") != "success"):
        msg = str(result.get("result") or "") + " " + json.dumps(result.get("errors") or "")
        res_hit = bool(LIMIT_RE.search(msg)) or result.get("api_error_status") == 429
    if not bad and not res_hit:
        return None
    resets: list[tuple[float, str, str | None, float | None]] = []
    for i in bad:
        windows = i.get("unifiedWindows") or {}
        if i.get("resetsAt"):
            w = i.get("rateLimitType") or "unknown"
            util = (windows.get(w) or {}).get("utilization")
            resets.append((float(i["resetsAt"]), w, i.get("status"), util))
        for w, v in windows.items():
            if (v.get("utilization") or 0) >= 1 and v.get("resetsAt"):
                resets.append((float(v["resetsAt"]), w, i.get("status"), v.get("utilization")))
    if res_hit:
        for m in _RESET_EPOCH_RE.finditer(msg):
            resets.append((float(m.group(1)), "result_message", None, None))
    if resets:
        reset, window, status, util = max(resets, key=lambda x: x[0])
        return {"until": reset + LIMIT_MARGIN_S, "window": window, "status": status, "util": util}
    return {
        "until": now + LIMIT_FALLBACK_WAIT_S, "window": "unknown", "status": None, "util": None,
    }  # fmt: skip


_USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
               "output_tokens")  # fmt: skip
_MU_KEYS = ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens", "outputTokens")


def _tokens_from_result(result: dict) -> dict | None:
    """Token counts from the result's `usage`, else its `modelUsage`; None when neither carries
    any count (missing usage is not measured zero)."""
    u = result.get("usage") or {}
    mus = [m for m in (result.get("modelUsage") or {}).values() if isinstance(m, dict)]
    has_u = isinstance(u, dict) and any(u.get(k) is not None for k in _USAGE_KEYS)
    has_mu = any(m.get(k) is not None for m in mus for k in _MU_KEYS)
    if not has_u and not has_mu:
        return None
    inp = int(u.get("input_tokens") or 0)
    cw = int(u.get("cache_creation_input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    if inp + cw + cr + out == 0 and has_mu:  # fall back to the per-model totals
        for m in mus:
            inp += int(m.get("inputTokens") or 0)
            cw += int(m.get("cacheCreationInputTokens") or 0)
            cr += int(m.get("cacheReadInputTokens") or 0)
            out += int(m.get("outputTokens") or 0)
    thinking = (u.get("output_tokens_details") or {}).get("thinking_tokens")
    if thinking is None:
        thinking = sum(int(m.get("thinkingTokens") or 0) for m in mus)
    return {"inp": inp, "cw": cw, "cr": cr, "out": out, "thinking": int(thinking or 0)}


def _tokens_from_assistant(events: list[dict]) -> dict | None:
    """Usage seen in assistant messages (the last snapshot per message id), when no result."""
    per_id: dict[str, dict] = {}
    for e in events:
        if e.get("type") == "assistant":
            m = e.get("message") or {}
            if m.get("usage"):
                per_id[m.get("id") or str(len(per_id))] = m["usage"]
    if not per_id:
        return None
    t = {"inp": 0, "cw": 0, "cr": 0, "out": 0, "thinking": 0}
    for u in per_id.values():
        t["inp"] += int(u.get("input_tokens") or 0)
        t["cw"] += int(u.get("cache_creation_input_tokens") or 0)
        t["cr"] += int(u.get("cache_read_input_tokens") or 0)
        t["out"] += int(u.get("output_tokens") or 0)
    return t


def _salvage(texts: list[str], schema: dict) -> dict | None:
    """The first text whose outermost {...} parses as JSON and validates against `schema`."""
    for t in texts:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            continue
        try:
            obj = json.loads(t[i : j + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and not validate(obj, schema):
            return obj
    return None


def _assistant_texts(events: list[dict]) -> list[str]:
    out = []
    for e in events:
        if e.get("type") == "assistant":
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    out.append(b["text"])
    return out[::-1]  # latest first


def interpret(
    events: list[dict], req: Req, model: str, now: float | None = None
) -> tuple[Resp, dict | None]:
    """Turn one call's events into a Resp plus a usage-limit hit (or None).

    Usage is taken from the result, else from assistant messages (`meta["usage_known"]` False
    when neither exists). A timed-out session (its `_process` event) gives error="timeout" with
    whatever usage it showed; an interrupted one (`interrupted` in `_process`) "interrupted".
    Raises BillingRouteError (carrying the Resp) when any event shows an API-key route or extra
    usage that isn't disabled, or a result came without an `init` event. A successful answer
    whose route evidence is incomplete becomes error="route_unverified" (retryable: not
    cached)."""
    now = time.time() if now is None else now
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    results = [e for e in events if e.get("type") == "result"]
    result = results[-1] if results else None
    proc = next((e for e in reversed(events) if e.get("type") == "_process"), {})
    infos = _infos(events)
    limit = _limit_from(events, result, now)
    route = route_evidence(events)
    meta: dict[str, Any] = {
        "rate_limit": infos[-1] if infos else None,
        "route": route,
        "api_retries": sum(
            1 for e in events if e.get("type") == "system" and e.get("subtype") == "api_retry"
        ),
        "returncode": proc.get("returncode"),
    }
    if proc.get("killed"):
        meta["killed"] = proc["killed"]
    tok = _tokens_from_result(result) if result is not None else None
    if tok is None:
        tok = _tokens_from_assistant(events)
    meta["usage_known"] = tok is not None
    tok = tok or {"inp": 0, "cw": 0, "cr": 0, "out": 0, "thinking": 0}
    meta["thinking_tokens"] = tok["thinking"]
    mu = sorted(((result or {}).get("modelUsage") or {}).keys())
    resp = Resp(
        req.key, None, None, None,
        input_tokens=tok["inp"] + tok["cw"] + tok["cr"], output_tokens=tok["out"],
        cache_read_tokens=tok["cr"], cache_write_tokens=tok["cw"], usd=0.0,
        model=",".join(mu), meta=meta,
    )  # fmt: skip
    if result is not None:
        meta.update(
            notional_usd=result.get("total_cost_usd"),
            duration_ms=result.get("duration_ms"),
            duration_api_ms=result.get("duration_api_ms"),
            num_turns=result.get("num_turns"),
            session_id=result.get("session_id"),
            stop_reason=result.get("stop_reason"),
            terminal_reason=result.get("terminal_reason"),
            model_usage_keys=mu,
        )
        text = result.get("result")
        resp.text = text if isinstance(text, str) else None
    # billing route: after the Resp exists, so the evidence travels with the exception
    paid = [w for w in map(_paid_event, events) if w]
    if paid:
        resp.error = "billing_route"
        raise BillingRouteError(f"{req.key}: {paid[0]}; batch aborted", resp)
    if result is not None and not inits:
        resp.error = "billing_route"
        raise BillingRouteError(
            f"{req.key}: result without an init event; route unverifiable", resp
        )
    if verify_route(route) == "bad":
        resp.error = "billing_route"
        raise BillingRouteError(f"{req.key}: route not subscription-only: {route}", resp)
    if proc.get("timed_out"):
        resp.error = "timeout"
        return resp, None
    if proc.get("interrupted"):
        resp.error = "interrupted"
        return resp, None
    if result is None:
        meta["stderr_tail"] = (proc.get("stderr_tail") or "")[-500:]
        resp.error = "limit" if limit else "cli_error"
        return resp, limit
    subtype = str(result.get("subtype") or "")
    if result.get("is_error") or subtype != "success":
        msg = (resp.text or "") + " " + json.dumps(result.get("errors") or "")
        meta["result_subtype"] = subtype
        if TOO_LONG_RE.search(msg):
            resp.error = "too_long"
        elif limit is not None:
            resp.error = "limit"
        elif "structured_output" in subtype:
            resp.error = "schema"
            meta["no_structured_output"] = True
        else:
            resp.error = "cli_error"
            meta["error_text"] = msg.strip()[-500:]
        return resp, limit
    if set(mu) != {model}:
        resp.error = "model_mismatch"
        return resp, limit
    if req.schema is not None:
        data = result.get("structured_output")
        if data is None:
            data = _salvage([resp.text or "", *_assistant_texts(events)], req.schema)
            if data is not None:
                meta["schema_salvaged"] = True
            else:
                meta["no_structured_output"] = True
                meta["schema_errors"] = ["$: no structured_output"]
                resp.error = "schema"
                return resp, limit
        errs = validate(data, req.schema)
        if errs:
            resp.error = "schema"
            meta["schema_errors"] = errs[:20]
            return resp, limit
        resp.data = data
    if verify_route(route) != "ok":  # an answer is accepted only with its route proven
        resp.error = "route_unverified"
        meta["unverified_data"] = resp.data
        resp.data = None
    return resp, limit


# ---------------------------------------------------------------- the backend


READ_ONLY_TOOLS = frozenset({"Grep", "Read", "Glob"})  # the only tools a request may name
_SAFE_FILE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")  # a plain file name in the call's directory
TOOL_INPUT_CHARS = 300


def tool_calls(events: list[dict]) -> list[dict]:
    """The tool calls a session made, in order, as {name, input} with long inputs cut: kept with
    the answer so every tool-using verdict can be audited."""
    out = []
    for e in events:
        if e.get("type") != "assistant":
            continue
        for b in (e.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = json.dumps(b.get("input"), ensure_ascii=False)
                out.append({"name": b.get("name"), "input": inp[:TOOL_INPUT_CHARS]})
    return out


def _canon(schema: dict | None) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _append_jsonl(path: Path, row: dict) -> None:
    """Append one line under an exclusive lock, first truncating a torn (newline-less) tail left
    by a crash so the new line starts clean."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.lseek(fd, 0, os.SEEK_END)
        if size:
            os.lseek(fd, size - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":
                os.lseek(fd, 0, os.SEEK_SET)
                data = os.read(fd, size)
                keep = data.rfind(b"\n") + 1
                log.warning("%s: dropping a torn tail of %d bytes", path, size - keep)
                os.ftruncate(fd, keep)
        os.lseek(fd, 0, os.SEEK_END)
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)  # releases the lock


_RESP_FIELDS = {f.name for f in dataclasses.fields(Resp)}
_SAFE_PURPOSE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class _Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.n_sent = 0
        self.tok = {k: 0 for k in ("input", "output", "cache_read", "cache_write", "thinking")}
        self.notional = 0.0
        self.paid_notional = 0.0
        self.paid_route = False
        self.usage_unknown = 0
        self.limit_waits = 0
        self.route = "unverified"
        self.errors: dict[str, int] = {}

    def add(self, resp: Resp) -> None:
        with self.lock:
            self.n_sent += 1
            self.tok["input"] += resp.input_tokens
            self.tok["output"] += resp.output_tokens
            self.tok["cache_read"] += resp.cache_read_tokens
            self.tok["cache_write"] += resp.cache_write_tokens
            self.tok["thinking"] += int(resp.meta.get("thinking_tokens") or 0)
            self.notional += float(resp.meta.get("notional_usd") or 0.0)
            if not resp.meta.get("usage_known", True):
                self.usage_unknown += 1
            if resp.error == "billing_route":
                self.paid_route = True
                self.paid_notional += float(resp.meta.get("notional_usd") or 0.0)
            if resp.error:
                self.errors[resp.error] = self.errors.get(resp.error, 0) + 1


def _canary_req() -> Req:
    return Req(
        key=CANARY_KEY, system=CANARY_SYSTEM, user=CANARY_USER, schema=None,
        max_output_tokens=CANARY_MAX_OUTPUT, rid=CANARY_KEY, source="canary", private=False,
    )  # fmt: skip


class MaxBackend:
    """`s1_types.Backend` over `claude -p` on the Max plan (spec U2a)."""

    family = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
        usage_path: str | Path = DEFAULT_USAGE_PATH,
        concurrency: int = 4,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        effort: str | None = None,
        *,
        name: str = "haiku-max",
        claude_bin: str | None = None,
        context_tokens: int = 1_000_000,
        bytes_per_token: float = 2.2,  # measured 2.27-2.46 (smoke note); spec D3 as revised
        kill_grace_s: float = KILL_GRACE_S,
        max_limit_waits: int = MAX_LIMIT_WAITS,
        limit_poll_s: float = LIMIT_POLL_S,
        sleep: Callable[[float], Any] | None = None,
        clock: Callable[[], float] = time.time,
        recorder: Callable[..., Any] | None = None,
    ):
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.name = name
        self.model = model
        self.context_tokens = context_tokens
        self.bytes_per_token = bytes_per_token
        self.cache_dir = Path(cache_dir)
        self.usage_path = Path(usage_path)
        self.concurrency = concurrency
        self.timeout_s = timeout_s
        self.effort = effort
        self.claude_bin = claude_bin or default_bin()
        self.kill_grace_s = kill_grace_s
        self.max_limit_waits = max_limit_waits
        self.limit_poll_s = limit_poll_s
        self._clock = clock
        self._stop = threading.Event()
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self._recorder = recorder
        self._pause_lock = threading.Lock()
        self._pause_until = 0.0
        self._poll_lock = threading.Lock()
        self._pause_gen = 0  # bumped by every research-call pause, so a stale poll is dropped
        self._poll_errors = 0
        self._write_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._owner: object = object()
        self._auth_at: float | None = None
        self.auth: dict | None = None
        self.managed_settings: str | None = None
        self.managed_sources: dict[str, str] = {}
        self.last_stats: dict | None = None

    # -- command and cache keys

    def command_args(self, req: Req) -> list[str]:
        args = [
            "-p",
            "--model", self.model,
            "--safe-mode",
            "--tools", ",".join(req.tools),
            "--no-session-persistence",
            "--strict-mcp-config",
            "--output-format", "stream-json",
            "--verbose",
            "--system-prompt", req.system,
        ]  # fmt: skip
        if req.schema is not None:
            args += ["--json-schema", json.dumps(req.schema, separators=(",", ":"))]
        if self.effort is not None:
            args += ["--effort", self.effort]
        if req.max_turns is not None:
            args += ["--max-turns", str(req.max_turns)]
        return args

    def cache_id(self, req: Req) -> str:
        parts = [
            req.key,
            _sha(req.system + "\0" + req.user),
            _sha(_canon(req.schema)),
            self.model,
            self.effort,
            int(req.max_output_tokens),
        ]
        if req.tools or req.files or req.max_turns is not None:  # old keys stay as they were
            parts += [list(req.tools), [[n, _sha(t)] for n, t in req.files], req.max_turns]
        return _sha(json.dumps(parts))

    def cache_path(self, purpose: str) -> Path:
        return self.cache_dir / f"{purpose}.jsonl"

    def _load_cache(self, purpose: str) -> dict[str, Resp]:
        path = self.cache_path(purpose)
        out: dict[str, Resp] = {}
        if not path.exists():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line from a crash (repaired on the next append)
            r = row.get("resp") or {}
            if r.get("error") is None and "cache_id" in row:
                out[row["cache_id"]] = Resp(**{k: v for k, v in r.items() if k in _RESP_FIELDS})
        return out

    # -- limits

    @staticmethod
    def _say(msg: str) -> None:
        log.warning(msg)
        print(msg, file=sys.stderr, flush=True)

    def _limit_line(self, limit: dict) -> None:
        """The anchored line a monitor greps for: S1_LIMIT <window> util=<u> resets=<iso>."""
        reset = _utc(limit["until"] - LIMIT_MARGIN_S) if limit["window"] != "unknown" else "?"
        self._say(f"S1_LIMIT {limit['window']} util={limit.get('util')} resets={reset}")

    def _set_pause(self, limit: dict) -> None:
        """Pause new launches until a research call's limit resets (only ever extends the pause;
        a poll sets it directly in `_poll`, since the poll is the latest reading)."""
        with self._pause_lock:
            if limit["until"] <= self._pause_until:
                return
            self._pause_until = limit["until"]
            self._pause_gen += 1
        self._say(f"MAX_LIMIT_WAIT until {_utc(limit['until'])} ({limit['window']})")
        self._limit_line(limit)

    def _wait_pause(self, purpose: str, stats: _Stats) -> None:
        """Wait out a usage-limit pause in slices of `limit_poll_s`. One worker at a time sleeps a
        slice and then sends the tiny canary: once a call is allowed again (a manual reset, or a
        window that reset early) the pause ends for every worker. Polls are not research
        attempts, so they never count toward `max_limit_waits`."""
        while True:
            if self._stop.is_set():
                raise _Stopped
            with self._pause_lock:
                d = self._pause_until - self._clock()
            if d <= 0:
                return
            if not self._poll_lock.acquire(blocking=False):
                self._sleep(min(d, 30.0))  # another worker is polling
                continue
            try:
                self._sleep(min(d, self.limit_poll_s))
                with self._pause_lock:
                    still = self._pause_until - self._clock() > 0
                if still and self._poll(purpose, stats):
                    return
            finally:
                self._poll_lock.release()

    def _poll(self, purpose: str, stats: _Stats) -> bool:
        """One canary while paused: True (and the pause cleared) when it is answered with no
        limit. A paid route raises from inside, as on any call. The result is dropped when a
        research call set a newer pause while the canary ran (its reading is the fresher one).
        MAX_POLL_ERRORS polls in a row failing for another reason (a broken CLI, an auth
        failure, timeouts) raise, rather than idle the run until the reset."""
        with self._pause_lock:
            gen = self._pause_gen
        resp, limit = self._attempt(_canary_req(), purpose, stats, 0, False)
        if limit is None and resp.error is not None:
            self._poll_errors += 1
            self._say(f"S1_LIMIT_POLL_ERROR {resp.error} ({self._poll_errors}/{MAX_POLL_ERRORS})")
            if self._poll_errors >= MAX_POLL_ERRORS:
                raise RuntimeError(
                    f"{MAX_POLL_ERRORS} limit polls in a row failed ({resp.error}); "
                    f"meta {json.dumps(resp.meta, default=str)[:500]}"
                )
            return False
        self._poll_errors = 0
        with self._pause_lock:
            if self._pause_gen != gen:
                return False
            self._pause_until = 0.0 if limit is None else limit["until"]
        if limit is not None:
            self._say(f"MAX_LIMIT_WAIT until {_utc(limit['until'])} ({limit['window']})")
            self._limit_line(limit)
            return False
        self._say(f"S1_LIMIT_CLEARED at {_utc(self._clock())}: a canary was allowed")
        return True

    # -- one call

    def _watch(self, ev: dict) -> bool:
        why = _paid_event(ev)
        if why is None:
            return False
        log.error("PAID_ROUTE_DETECTED (%s): stopping all launches", why)
        self._stop.set()
        threading.Thread(target=kill_sessions, args=(self._owner,), daemon=True).start()
        return True

    def _attempt(self, req: Req, purpose: str, stats: _Stats, attempt: int, schema_retry: bool):
        if self._stop.is_set():
            raise _Stopped
        cwd = tempfile.mkdtemp(prefix="claude-max-")
        t0 = time.monotonic()
        try:
            for name, text in req.files:
                Path(cwd, name).write_text(text, encoding="utf-8")
            try:
                events = run_session(
                    cwd,
                    self.command_args(req),
                    req.user,
                    self.timeout_s,
                    claude_bin=self.claude_bin,
                    env=clean_env(req.max_output_tokens),
                    kill_grace_s=self.kill_grace_s,
                    owner=self._owner,
                    on_event=self._watch,
                    stop=self._stop,
                )
            except SessionTimeout as e:
                events = e.events
            except BaseException as exc:  # interrupted after launch: account, then re-raise
                if hasattr(exc, "claude_events"):
                    ev = [*exc.claude_events, exc.claude_process]
                    resp, _, _ = self._interpret(ev, req)
                    self._account(req, purpose, stats, attempt, schema_retry, resp, t0)
                raise
            resp, limit, billing_err = self._interpret(events, req)
            if req.tools:
                resp.meta["tool_calls"] = tool_calls(events)
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        self._account(req, purpose, stats, attempt, schema_retry, resp, t0)
        if billing_err is not None:
            raise billing_err
        return resp, limit

    def _interpret(self, events: list[dict], req: Req):
        try:
            resp, limit = interpret(events, req, self.model, self._clock())
            return resp, limit, None
        except BillingRouteError as e:  # accounted by the caller, then the batch aborts
            return e.resp or Resp(req.key, None, None, "billing_route"), None, e

    def _account(self, req, purpose, stats, attempt, schema_retry, resp: Resp, t0) -> None:
        """The usage row and batch totals for one launched call, whatever its outcome."""
        if resp.error == "timeout":
            resp.meta["timeout_s"] = self.timeout_s
        resp.duration_s = round(time.monotonic() - t0, 3)
        resp.meta["attempt"] = attempt
        resp.meta["managed_settings"] = self.managed_settings
        resp.meta["managed_sources"] = self.managed_sources
        stats.add(resp)
        row = {
            "utc": _utc(),
            "key": req.key,
            "rid": req.rid,
            "purpose": purpose,
            "model": self.model,
            "answered_by": resp.model,
            "effort": self.effort,
            "max_output_tokens": req.max_output_tokens,
            "attempt": attempt,
            "schema_retry": schema_retry,
            "error": resp.error,
            "usage_known": resp.meta.get("usage_known"),
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "cache_read_tokens": resp.cache_read_tokens,
            "cache_write_tokens": resp.cache_write_tokens,
            "thinking_tokens": resp.meta.get("thinking_tokens"),
            "notional_usd": resp.meta.get("notional_usd"),
            "duration_s": resp.duration_s,
            "api_retries": resp.meta.get("api_retries"),
            "rate_limit": resp.meta.get("rate_limit"),
            "route": resp.meta.get("route"),
        }
        with self._write_lock:
            _append_jsonl(self.usage_path, row)

    def _one(self, req: Req, purpose: str, stats: _Stats) -> Resp:
        waits = 0
        attempt = 0
        cur = req
        first_schema_errors = None
        while True:
            self._wait_pause(purpose, stats)
            attempt += 1
            resp, limit = self._attempt(cur, purpose, stats, attempt, cur is not req)
            if limit is not None:
                self._set_pause(limit)
                with stats.lock:
                    stats.limit_waits += 1
                if resp.error is not None:  # answered despite the limit status: keep it
                    waits += 1
                    resp.meta["limit_waits"] = waits
                    if waits > self.max_limit_waits:
                        resp.error = "limit"
                        break
                    continue
            if resp.error == "schema" and resp.meta.get("no_structured_output") and cur is req:
                first_schema_errors = resp.meta.get("schema_errors")
                cur = dataclasses.replace(req, user=req.user + "\n\n" + SCHEMA_RETRY_LINE)
                continue
            break
        if cur is not req:
            resp.meta["schema_retry"] = {
                "first_errors": first_schema_errors,
                "outcome": resp.error or "ok",
            }
        resp.key = req.key
        return resp

    # -- the batch

    def run(self, reqs: list[Req], *, purpose: str) -> dict[str, Resp]:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("MaxBackend.run is already running on this instance")
        try:
            with job_guard(self._stop):
                return self._run(reqs, purpose)
        finally:
            self._run_lock.release()

    def _cache_row(self, r: Req, cid: str, resp: Resp, purpose: str) -> None:
        row = {
            "cache_id": cid,
            "key": r.key,
            "rid": r.rid,
            "prompt_sha": _sha(r.system + "\0" + r.user),
            "schema_sha": _sha(_canon(r.schema)),
            "model": self.model,
            "effort": self.effort,
            "max_output_tokens": r.max_output_tokens,
            "utc": _utc(),
            "resp": dataclasses.asdict(resp),
        }
        with self._write_lock:
            _append_jsonl(self.cache_path(purpose), row)

    def _run(self, reqs: list[Req], purpose: str) -> dict[str, Resp]:
        if not _SAFE_PURPOSE.match(purpose):
            raise ValueError(f"purpose must be a plain file stem, got {purpose!r}")
        keys = [r.key for r in reqs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate Req.key in batch")
        out: dict[str, Resp] = {}
        cache = self._load_cache(purpose)
        pending: list[tuple[Req, str]] = []
        for r in reqs:
            if not fits(r, self):
                est = est_tokens(r.system + r.user, self.bytes_per_token)
                out[r.key] = Resp(
                    r.key, None, None, "too_long", model=self.model, meta={"est_tokens": est}
                )
                continue
            cid = self.cache_id(r)
            if cid in cache:
                hit = dataclasses.replace(cache[cid], key=r.key)
                hit.meta = {**hit.meta, "cached": True}
                out[r.key] = hit
                continue
            if not r.system.strip():
                raise ValueError(
                    f"{r.key}: empty system prompt (Claude Code's default would apply)"
                )
            bad = set(r.tools) - READ_ONLY_TOOLS
            if bad or any(not _SAFE_FILE.match(n) for n, _ in r.files):
                raise ValueError(f"{r.key}: tools {sorted(bad)} or file names not allowed")
            if len(r.system.encode("utf-8")) > MAX_SYSTEM_BYTES:
                raise ValueError(f"{r.key}: system prompt over {MAX_SYSTEM_BYTES} bytes (argv)")
            pending.append((r, cid))
        if not pending:
            return {k: out[k] for k in keys}
        self.managed_settings = check_managed_settings()
        self.managed_sources = {k: r["state"] for k, r in LAST_MANAGED_SOURCES.items()}
        if self._auth_at is None or self._clock() - self._auth_at > AUTH_TTL_S:
            self.auth = check_auth(self.claude_bin)
            self._auth_at = self._clock()
        stats = _Stats()
        self._stop.clear()
        self._owner = object()  # this run's sessions
        try:
            self._canary(purpose, stats)  # alone, before any research request
            self._fan_out(pending, purpose, stats, out)
        except BaseException:  # abort: no worker may outlive the batch
            self._stop.set()
            kill_sessions(owner=self._owner)
            raise
        finally:
            self._stop.clear()
            self.last_stats = {
                "n_sent": stats.n_sent,
                "tokens": dict(stats.tok),
                "notional_usd": round(stats.notional, 6),
                "limit_waits": stats.limit_waits,
                "usage_unknown": stats.usage_unknown,
                "route": "PAID_ROUTE_DETECTED" if stats.paid_route else stats.route,
                "errors": dict(stats.errors),
            }
            if stats.n_sent:
                self._record(purpose, stats)
        return {k: out[k] for k in keys}

    def _canary(self, purpose: str, stats: _Stats) -> None:
        """A dedicated tiny request, sent alone: the batch fans out only once its events prove
        the subscription-only route (init apiKeySource "none"; a rate-limit snapshot with extra
        usage disabled). Up to MAX_CANARIES tries when evidence is missing, then refuse."""
        creq = _canary_req()
        for _ in range(MAX_CANARIES):
            resp = self._one(creq, purpose, stats)  # a paid route raises inside
            v = verify_route(resp.meta.get("route"))
            if v == "ok":
                stats.route = "verified"
                return
            if v == "bad":  # (interpret raises first; kept as a backstop)
                stats.paid_route = True
                raise BillingRouteError(f"canary: route not subscription-only: {resp.meta}")
        raise BillingRouteError(
            f"{MAX_CANARIES} canary calls gave no billing-route evidence; batch refused"
        )

    def _fan_out(self, rest, purpose: str, stats: _Stats, out: dict) -> None:
        """The remaining calls on the pool. The first failure that stops the batch (a billing
        route, a config error) stops new launches and kills the run's sessions at once; the other
        workers then finish fast and the failure is raised."""
        err: BaseException | None = None
        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futs = {ex.submit(self._one, r, purpose, stats): (r, cid) for r, cid in rest}
            try:
                for f in as_completed(futs):
                    r, cid = futs[f]
                    try:
                        resp = f.result()
                    except _Stopped:
                        continue
                    except Exception as e:  # noqa: BLE001 - kept and re-raised after the drain
                        if err is None or (
                            isinstance(e, BillingRouteError)
                            and not isinstance(err, BillingRouteError)
                        ):
                            err = e
                        self._stop.set()
                        kill_sessions(owner=self._owner)
                        continue
                    out[r.key] = resp
                    self._cache_row(r, cid, resp, purpose)
            except BaseException:  # a signal or interrupt in the main thread
                self._stop.set()
                for f in futs:
                    f.cancel()
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    kill_sessions(owner=self._owner)  # again: a worker may launch mid-abort
                    if not any(f.running() for f in futs):
                        break
                    time.sleep(0.05)
                raise
        if err is not None:
            raise err
        if self._stop.is_set():
            raise RuntimeError("batch stopped before every call ran")

    def _record(self, purpose: str, stats: _Stats) -> None:
        t = stats.tok
        body = (
            f"purpose={purpose} calls={stats.n_sent} input={t['input']} output={t['output']} "
            f"cache_read={t['cache_read']} cache_write={t['cache_write']} "
            f"thinking={t['thinking']} notional_usd={stats.notional:.4f} "
            f"usage_unknown={stats.usage_unknown} limit_waits={stats.limit_waits} "
            f"managed_settings={self.managed_settings}"
        )
        if stats.paid_route:
            usd = round(stats.paid_notional, 6)
            note = f"PAID_ROUTE_DETECTED (usd = notional cost of those calls): {body}"
        else:
            usd = 0.0
            note = f"Max plan, $0, route {stats.route}: {body}"
        rec = self._recorder
        if rec is None:
            from emx import spend

            rec = spend.record
        rec("judge", f"claude-max:{self.model}", units=stats.n_sent, usd=usd, note=note, exp=STAGE)
