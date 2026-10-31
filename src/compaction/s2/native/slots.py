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
- Per-prefix limits (`LIMITS`, {prefix: max slots}; Research Advisor, 28 Oct: the DeepSeek arm
  takes priority over Codex and agy) are re-read on every poll, so a running scheduler yields
  as its histories end: lowering a limit never stops a running history, it only holds new
  ones. An unparsable limits file holds every prefix (closed). Set them with
  `python -m compaction.s2.native.slots limits <prefix>=<n> ...` (`<prefix>=-` removes one).
- Two laptop settings live in the same file, also re-read on the fly: `__capacity__` overrides
  every scheduler's laptop container capacity (the ledger's for host "laptop" and each
  process-local gate's, through `setting`), and `__cpu_tenths__` the CPU limit of each laptop
  history launched from then on. Without them each scheduler uses its launch arguments; other
  hosts never read them.
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
LIMITS = Path.home() / ".emx_harness_slot_limits.json"
# the laptop settings' approved ranges (Jacob, 28 Oct: task containers only, 8 histories = 16
# containers with sidecars); a value outside them closes admission (astra ds-build r2 B2)
SETTING_RANGES = {"__capacity__": (0, 16), "__cpu_tenths__": (5, 20)}
_DEFAULT = object()  # LIMITS with the default ledger; none with a caller's own (tests)


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


def _limits(path: Path | None) -> dict[str, int]:
    """{prefix: max slots}; a missing file is no limit, a malformed one raises LedgerCorrupt."""
    if path is None or not path.exists():
        return {}
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise LedgerCorrupt(f"{path}: {e}") from e
    if not isinstance(d, dict) or not all(isinstance(k, str) and isinstance(v, int)
                                         and not isinstance(v, bool) and v >= 0
                                         for k, v in d.items()):
        raise LedgerCorrupt(f"{path}: not {{prefix: non-negative int}}")
    for k, (lo, hi) in SETTING_RANGES.items():
        if k in d and not lo <= d[k] <= hi:
            raise LedgerCorrupt(f"{path}: {k}={d[k]} outside [{lo}, {hi}]")
    return d


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
            on_wait: Callable[[dict], None] | None = None, limits=_DEFAULT):
    """Hold `n` slots (and `memory_mb`) on `host` for the block (module docstring). A failing
    `names()` or a corrupt ledger keeps waiting (closed)."""
    key = f"{owner}-{uuid.uuid4().hex[:8]}"
    t0 = time.monotonic()
    if limits is _DEFAULT:
        limits = LIMITS if state == STATE else None
    while True:
        try:
            current = names()
            with _locked(lock):
                d = _read(state)
                es = d.setdefault(host, {})
                c = _count(es, current)
                lims = _limits(limits)
                lim = lims.get(prefix)
                cap = lims.get("__capacity__", capacity) if host == "laptop" else capacity
                held = sum(int(e["n"]) for e in es.values() if e.get("prefix") == prefix)
                if c["total"] + n <= cap and (lim is None or held + n <= lim):
                    es[key] = {"n": n, "prefix": prefix, "memory_mb": memory_mb,
                               "pid": os.getpid(), "owner": owner,
                               "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    _write(state, d)
                    break
                status = {**c, "want": n, "capacity": cap, "limit": lim, "held": held}
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


def setting(key: str, default: int, *, path: Path | None = None) -> int:
    """A machine-wide setting from the limits file (`__capacity__`, `__cpu_tenths__`), else
    `default`. A malformed file raises LedgerCorrupt (callers keep their gate closed)."""
    return _limits(LIMITS if path is None else path).get(key, default)


def set_limits(pairs: list[str], *, path: Path = LIMITS, lock: Path = LOCK) -> dict[str, int]:
    """Apply `prefix=n` (or `prefix=-` to remove) to the limits file, atomically, under the
    ledger's lock; returns the new limits."""
    with _locked(lock):
        d = _limits(path)
        for p in pairs:
            k, _, v = p.partition("=")
            if not k or not v:
                raise ValueError(f"expected prefix=n or prefix=-, got {p!r}")
            if v == "-":
                d.pop(k, None)
            else:
                if not v.isdigit():
                    raise ValueError(f"not a non-negative int: {p!r}")
                d[k] = int(v)
        tmp = path.with_name(path.name + ".check")
        tmp.write_text(json.dumps(d))
        try:
            _limits(tmp)  # the same validation as every read: a bad value is never written
        finally:
            tmp.unlink(missing_ok=True)
        _write(path, d)
        return d


def hold(host: str, owner: str, *, n: int, prefix: str, pid: int, state: Path = STATE,
         lock: Path = LOCK) -> str:
    """Register `n` slots for a running process that predates the ledger (e.g. the Haiku bulk
    launched from 3457d31b; astra ds-build r2 B1), without an admission check: its histories,
    starting or between tasks, may have no visible containers. The entry carries the process's
    pid, so it is dropped when that process ends, like any reservation."""
    if not _alive(pid):
        raise ValueError(f"pid {pid} is not running")
    key = f"{owner}-hold-{uuid.uuid4().hex[:8]}"
    with _locked(lock):
        d = _read(state)
        d.setdefault(host, {})[key] = {"n": n, "prefix": prefix, "memory_mb": 0, "pid": pid,
                                       "owner": owner, "hold": True,
                                       "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _write(state, d)
    return key


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["limits"]:
        print(json.dumps(set_limits(sys.argv[2:]) if sys.argv[2:] else _limits(LIMITS)))
    elif sys.argv[1:2] == ["hold"] and len(sys.argv) == 7:
        h, pre, n_, pid_, own = sys.argv[2:7]
        print(hold(h, own, n=int(n_), prefix=pre, pid=int(pid_)))
    elif sys.argv[1:2] == ["show"]:
        with _locked(LOCK):
            print(json.dumps(_read(STATE), indent=1))
    else:
        sys.exit("usage: python -m compaction.s2.native.slots limits [<prefix>=<n>|<prefix>=- ...]"
                 " | hold <host> <prefix> <n> <pid> <owner> | show")
