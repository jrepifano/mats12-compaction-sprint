"""Host-wide container-slot reservations shared by the native-harness schedulers (week-2 harness
replication; astra agy-build r1 B6, cx-build r2 B3–B5). Each scheduler wraps each history in
`reserve(...)`:

- One JSON file per machine (`STATE`) holds every live reservation: {host: {key: {"n",
  "prefix", "memory_mb", "pid", "owner", "utc"}}}. It is read and written only under an
  exclusive `flock` (`LOCK`), written atomically (temp file + rename), and a file that exists but
  doesn't parse fails closed (no admission) rather than reading as empty.
- A reservation is admitted when, for its host, reserved slots + external containers + `n` <=
  `capacity`. A registered prefix's running containers are covered only up to the slots that
  prefix holds; the rest of them (orphans, unreserved smokes) count as external, as do all
  containers of unregistered prefixes (other experiments, a scheduler that doesn't reserve).
- Entries whose pid is gone (a crashed scheduler on this machine) are dropped on every read.
- `reserved_memory_mb(host)` sums every live reservation's own memory budget (remote gates).
- `reserve` waits (polling) until admitted or until `timeout_s`, and releases on exit.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

STATE = Path.home() / ".emx_harness_slots.json"
LOCK = Path.home() / ".emx_harness_slots.lock"


class LedgerCorrupt(RuntimeError):
    pass


@contextlib.contextmanager
def _locked(lock: Path):
    with open(lock, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read(state: Path) -> dict:
    """The live entries (call under the lock). A missing file is empty; an unparsable one
    raises LedgerCorrupt."""
    if not state.exists():
        return {}
    try:
        d = json.loads(state.read_text())
    except (OSError, ValueError) as e:
        raise LedgerCorrupt(f"{state}: {e}") from e
    if not isinstance(d, dict):
        raise LedgerCorrupt(f"{state}: not an object")
    try:  # a malformed host or entry is corruption too, never silently dropped (cx r3 #5)
        return {h: {k: e for k, e in es.items() if _alive(int(e["pid"])) and int(e["n"]) >= 0}
                for h, es in d.items()}
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        raise LedgerCorrupt(f"{state}: malformed entry: {e}") from e


def _write(state: Path, d: dict) -> None:
    tmp = state.with_name(f"{state.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, state)


def _count(es: dict, names: list[str]) -> dict:
    """Reserved slots and external containers for one host's entries and its running names."""
    reserved = sum(int(e["n"]) for e in es.values())
    by_prefix: dict[str, int] = {}
    for e in es.values():
        if e.get("prefix"):
            by_prefix[e["prefix"]] = by_prefix.get(e["prefix"], 0) + int(e["n"])
    external, seen = 0, {p: 0 for p in by_prefix}
    for nm in names:
        p = next((p for p in by_prefix if nm.startswith(p)), None)
        if p is None:
            external += 1
        else:
            seen[p] += 1
    external += sum(max(0, seen[p] - by_prefix[p]) for p in by_prefix)  # orphans beyond cover
    return {"reserved": reserved, "external": external, "total": reserved + external}


def used(host: str, names: list[str], *, state: Path = STATE, lock: Path = LOCK) -> dict:
    with _locked(lock):
        return _count(_read(state).get(host, {}), names)


def reserved_memory_mb(host: str, *, state: Path = STATE, lock: Path = LOCK) -> int:
    with _locked(lock):
        return sum(int(e.get("memory_mb") or 0) for e in _read(state).get(host, {}).values())


@contextlib.contextmanager
def reserve(host: str, owner: str, *, n: int, prefix: str, capacity: int,
            names: Callable[[], list[str]], memory_mb: int = 0, poll_s: float = 30.0,
            timeout_s: float | None = None, state: Path = STATE, lock: Path = LOCK,
            on_wait: Callable[[dict], None] | None = None):
    """Hold `n` slots (and `memory_mb`) on `host` for the block (module docstring). A failing
    `names()` or a corrupt ledger keeps waiting (closed)."""
    key = f"{owner}-{uuid.uuid4().hex[:8]}"
    t0 = time.monotonic()
    while True:
        try:
            current = names()
            with _locked(lock):
                d = _read(state)
                es = d.setdefault(host, {})
                c = _count(es, current)
                if c["total"] + n <= capacity:
                    es[key] = {"n": n, "prefix": prefix, "memory_mb": memory_mb,
                               "pid": os.getpid(), "owner": owner,
                               "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    _write(state, d)
                    break
                status = {**c, "want": n, "capacity": capacity}
        except Exception as e:  # noqa: BLE001 - unreadable host or corrupt ledger: closed
            status = {"error": f"{type(e).__name__}: {e}"[:200]}
        if timeout_s is not None and time.monotonic() - t0 > timeout_s:
            raise TimeoutError(f"no {n} slots on {host} after {timeout_s}s: {status}")
        if on_wait is not None:
            on_wait(status)
        time.sleep(poll_s)
    try:
        yield key
    finally:
        with _locked(lock):
            try:
                d = _read(state)
            except LedgerCorrupt:  # astra agy-build r3 B6: keep it for inspection; admission
                return  # stays closed until someone repairs it
            d.get(host, {}).pop(key, None)
            _write(state, d)
