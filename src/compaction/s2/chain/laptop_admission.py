"""Machine-wide admission of emx containers on the laptop (week 2: experiment 2's control runs,
experiment 3b's laptop batch). Modelled on scripts/s2_native_codex.py `laptop_gate`.

Jacob's laptop rule: at most 8 emx containers on the laptop, 1280 MB each, CPU limits summing to
at most 8; a container starts only while macOS reports at least 30% of memory free
(`memory_pressure`, "System-wide memory free percentage") and the 1-minute load is below 12.

**Shared state** (one per user, every launcher that uses this module):
- `~/.emx/laptop_admission.lock`: an fcntl flock held across "read docker and the registry,
  decide, write the reservation", so two launchers can't both take the last room.
- `~/.emx/laptop_reservations.json`: the registry of launches not yet visible in `docker ps`:
  [{key, pid, run_id | name, mem_mb, cpus, utc, t}]. An entry counts as pending (its limits
  added to the visible containers') until its container is visible: a running container
  labelled `emx.run=<run_id>` (or named `name`). Then it is marked visible and stops counting
  (docker ps counts it). There is no time-based expiry; an entry pending longer than
  `warn_pending_s` (600 s) is logged once. Writes are atomic (tmp + rename) under the lock.
- **Orphans** (`reconcile`, the one routine both launchers' admissions run under the held
  lock): a pending entry whose launcher pid is dead is NOT dropped on that ground (its docker
  client or the daemon may still start the container). It keeps counting until its container
  is visible (then it is dropped: docker counts it) or `docker ps -a` (all states) shows no
  container for it on two checks spaced at least START_TIMEOUT_S (300 s: the sandbox's own
  `docker run` limit) apart, the first taken after the owner's death; any sighting in between
  starts the checks over. The native launcher's dead-owner, not-visible reservations get the
  same rule (`native_pending`), probed by the `emx.native` label (its reservations carry no
  run id), with their check times kept in `~/.emx/native_orphans.json`. Unresolved orphans are
  reported with key, owner pid and status in `info`, in the refusal reason and in a log line.
- **The native Codex launcher** (scripts/s2_native_codex.py) shares the lock: its
  `_launch_lock` also takes this flock, and its `laptop_gate` adds this registry's pending
  entries (`pending_summary`, computed from the same docker snapshot its gate took and after
  `reconcile`). In turn this gate counts its pending laptop sessions, read from its
  reservation files (`native_reservation_files`: host "local", not marked visible; a
  dead-owner one by the orphan rule) as 2 containers each: the task container at its limits
  plus the egress sidecar's (128 MB, 0.5 CPU).
- **One batch at a time** (belt and braces): `scripts/week2_monitor_controls.py go` holds
  `~/.emx/controls_go.lock` (`go_lock`) for its whole run and refuses while any native
  reservation has a live pid (`native_live`); the native `laptop_gate` refuses while this
  registry holds a live-pid pending entry or that lock is held (`controls_active`).
- `EMX_LAPTOP_DIR` overrides `~/.emx`, and `EMX_NATIVE_RESERVATIONS` (os.pathsep-separated)
  the native reservation files (tests).

**API:**
- `reserve(key, mem_mb, cpus, *, run_id=None, name=None, gate=None) -> (ok, why)`: under the
  lock, admit one more container if the rule allows (and `gate(reading)`, a caller's extra
  condition such as a ramp, returns None), and write its reservation. Exactly one of `run_id`
  (match by the `emx.run` label: the sandbox names containers inside `Sandbox.start`, but
  labels them with the invocation's run_id, known before the launch) or `name`.
- `release(key)`: drop the reservation (the launch was cancelled or failed, or its run ended).
  Call it in a `finally`.
- `locked()`: the flock, for another launcher's check-and-reserve; `pending_summary()`: this
  registry's pending containers and limits, for its gate (call it under `locked()`).
- `LaptopAdmission`: driver.Admission's `slot(kind, stop, run_id=...)` interface over
  reserve/release, with a per-driver ramp (at most `ramp_agents` until `ramp_agents` slots have
  each been held `ramp_after_s`, then `max_agents`; the opening is logged once). It waits
  (polls) while the rule says no and never cuts; a stop request ends the wait.

Fails closed: a docker error, an incomplete inspect, an emx container with no memory or CPU
limit, a missing memory or load reading, or an unreadable registry holds the launch.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LABEL = "emx.sandbox"
RUN_LABEL = "emx.run"
WARN_PENDING_S = 600.0
# deepswe/sandbox.py has no constant for it: Sandbox.start runs `docker run` with timeout=300,
# so a launch may still create its container up to 300 s after it was issued.
START_TIMEOUT_S = 300.0
ORPHAN_CONFIRM_S = START_TIMEOUT_S  # two empty `docker ps -a` checks this far apart
NATIVE_LABEL = "emx.native"  # on every native session container (compaction.s2.native.common)
NATIVE_SIDECAR = {"mem_mb": 128.0, "cpus": 0.5}  # s2_native_codex SIDECAR_MEM_GB, SIDECAR_CPUS


@dataclass(frozen=True)
class Limits:
    max_containers: int = 8
    max_cpus: float = 8.0
    max_mem_mb: int = 8 * 1280
    min_free_pct: int = 30
    max_load1: float = 12.0


LAPTOP = Limits()


def state_dir() -> Path:
    return Path(os.environ.get("EMX_LAPTOP_DIR") or (Path.home() / ".emx"))


def lock_path() -> Path:
    return state_dir() / "laptop_admission.lock"


def registry_path() -> Path:
    return state_dir() / "laptop_reservations.json"


def _run(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def reading(run: Callable = _run) -> dict:
    """The laptop's state: the running emx containers (names, emx.run labels, summed limits),
    memory free %, 1-minute load. Raises RuntimeError on anything unknown (the caller holds)."""
    p = run(["docker", "ps", "--filter", f"label={LABEL}", "--format",
             "{{.ID}}\t{{.Names}}\t{{.Label \"" + RUN_LABEL + "\"}}"])
    if p.returncode != 0:
        raise RuntimeError(f"docker ps failed rc={p.returncode}: {p.stderr.strip()[-200:]}")
    rows = [x.split("\t") for x in p.stdout.splitlines() if x.strip()]
    if any(len(r) != 3 for r in rows):
        raise RuntimeError("docker ps output not parsed")
    ids = [r[0] for r in rows]
    mem_mb = cpus = 0.0
    if ids:
        q = run(["docker", "inspect", "--format",
                 "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}", *ids])
        lines = [x for x in q.stdout.splitlines() if x.strip()]
        if q.returncode != 0 or len(lines) != len(ids):
            # a container that exited between ps and inspect also lands here: hold, re-read
            raise RuntimeError(f"docker inspect incomplete rc={q.returncode} "
                               f"({len(lines)}/{len(ids)})")
        for line in lines:
            m_b, c_n = (int(x) for x in line.split())
            if m_b <= 0 or c_n <= 0:
                raise RuntimeError("an emx container has no memory or CPU limit (unbounded)")
            mem_mb += m_b / 2**20
            cpus += c_n / 1e9
    mp = run(["memory_pressure"])
    m = re.search(r"free percentage:\s*(\d+)%", mp.stdout or "")
    if mp.returncode != 0 or m is None:
        raise RuntimeError(f"memory_pressure gave no free percentage (rc={mp.returncode})")
    return {"n": len(ids), "mem_mb": round(mem_mb, 1), "cpus": round(cpus, 3),
            "names": sorted(r[1] for r in rows), "runs": sorted({r[2] for r in rows if r[2]}),
            "free_pct": int(m.group(1)), "load1": os.getloadavg()[0]}


def _alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def container_exists(e: dict, run: Callable = _run) -> bool:
    """Whether docker knows any container (any state: created, running, exited) for a
    reservation. Raises RuntimeError when docker can't be asked (the caller holds)."""
    flt = (f"label={RUN_LABEL}={e['run_id']}" if e.get("run_id") is not None
           else f"name=^{e['name']}$")
    p = run(["docker", "ps", "-a", "--filter", flt, "-q"])
    if p.returncode != 0:
        raise RuntimeError(f"docker ps -a failed rc={p.returncode}: {p.stderr.strip()[-200:]}")
    return bool(p.stdout.strip())


def native_reservation_files() -> list[Path]:
    """The native Codex launcher's reservation files: logs/s2/native_codex_reservations.json in
    the main checkout and in every worktree under .claude/worktrees (it writes to the checkout
    it runs from)."""
    env = os.environ.get("EMX_NATIVE_RESERVATIONS")
    if env is not None:
        return [Path(x) for x in env.split(os.pathsep) if x]
    from compaction.s2.config import REPO_ROOT
    root = Path(REPO_ROOT)
    main = root.parents[2] if (root.parent.name == "worktrees"
                               and root.parent.parent.name == ".claude") else root
    rel = Path("logs") / "s2" / "native_codex_reservations.json"
    return sorted({main / rel, *((w / rel) for w in (main / ".claude" / "worktrees").glob("*"))})


def native_containers_exist(run: Callable = _run) -> bool:
    """Whether docker knows any native session container (any state). Raises when docker can't
    be asked."""
    p = run(["docker", "ps", "-a", "--filter", f"label={NATIVE_LABEL}", "-q"])
    if p.returncode != 0:
        raise RuntimeError(f"docker ps -a failed rc={p.returncode}: {p.stderr.strip()[-200:]}")
    return bool(p.stdout.strip())


def native_orphans_path() -> Path:
    return state_dir() / "native_orphans.json"


def _native_rows() -> list[tuple[Path, dict]]:
    out = []
    for f in native_reservation_files():
        if not f.is_file():
            continue
        rows = json.loads(f.read_text())  # unreadable: raises, the caller holds
        out += [(f, r) for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
    return out


def native_live() -> list[dict]:
    """Native reservations (any host) whose launcher pid is alive."""
    return [{"file": str(f), **r} for f, r in _native_rows() if _alive(r.get("pid", 0))]


def native_pending(now: float | None = None, probe: Callable[[], bool] | None = None,
                   log: Callable[[str], None] | None = None) -> tuple[list[dict], list[dict]]:
    """The native launcher's pending laptop sessions as container entries (2 per session), and
    its unresolved orphans. A live owner's not-visible session counts; a dead owner's counts
    until its containers could no longer appear: no native container on two `docker ps -a`
    checks ORPHAN_CONFIRM_S apart, the first after the owner's death. Call under the lock.
    Raises on an unreadable file or docker error (the caller holds)."""
    now = time.time() if now is None else now
    probe = probe or native_containers_exist
    op = native_orphans_path()
    seen = json.loads(op.read_text()) if op.exists() else {}
    out, orphans, still = [], [], {}
    exists = None
    for f, r in _native_rows():
        if r.get("host", "local") != "local" or r.get("visible"):
            continue
        k = f"native:{r['pid']}"
        if not _alive(r.get("pid", 0)):
            ok = f"{f}:{r['pid']}:{r.get('ts')}"
            if exists is None:
                exists = probe()
            ch = [] if exists else [*seen.get(ok, []), now]
            if len(ch) >= 2 and ch[-1] - ch[0] >= ORPHAN_CONFIRM_S:
                if log:
                    log(f"laptop admission: native orphan {k} dropped (no native container "
                        f"on checks {ch[-1] - ch[0]:.0f} s apart)")
                continue
            still[ok] = ch
            orphans.append({"key": k, "pid": r["pid"], "status": _orphan_status(ch, exists,
                                                                                now)})
        out += [{"key": k, "mem_mb": float(r["mem_gb"]) * 1024, "cpus": float(r["cpus"])},
                {"key": f"{k}:sidecar", **NATIVE_SIDECAR}]
    op.parent.mkdir(parents=True, exist_ok=True)
    tmp = op.with_name(op.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(still))
    tmp.replace(op)
    return out, orphans


def _orphan_status(checks: list[float], exists: bool, now: float) -> str:
    if exists:
        return "owner dead; docker still shows a container"
    left = ORPHAN_CONFIRM_S - (now - checks[0])
    return (f"owner dead; no container on {len(checks)} check(s), confirmable in "
            f"{max(0.0, left):.0f} s")


def orphan_text(orphans: list[dict]) -> str:
    return "; ".join(f"{o['key']} (pid {o['pid']}: {o['status']})" for o in orphans)


@contextlib.contextmanager
def _locked():
    p = lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


locked = _locked  # public: another launcher's check-and-reserve under the same flock


def _load() -> list[dict]:
    p = registry_path()
    if not p.exists():
        return []
    rows = json.loads(p.read_text())  # unreadable: raises, the caller holds
    if not isinstance(rows, list):
        raise TypeError(f"{p}: not a list")
    return rows


def _save(rows: list[dict]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rows, indent=1))
    tmp.replace(p)


def _visible(e: dict, r: dict) -> bool:
    return (e.get("run_id") is not None and e["run_id"] in r["runs"]) or \
        (e.get("name") is not None and e["name"] in r["names"])


def decide(r: dict, pending: list[dict], mem_mb: float, cpus: float,
           limits: Limits = LAPTOP) -> tuple[str | None, dict]:
    """None when one more container (mem_mb, cpus) fits beside the visible ones and the
    pending reservations; else the reason. Pure."""
    n = r["n"] + len(pending) + 1
    mem = r["mem_mb"] + sum(e["mem_mb"] for e in pending) + mem_mb
    cpu = r["cpus"] + sum(e["cpus"] for e in pending) + cpus
    info = {"n": r["n"], "pending": len(pending), "with_new": {"n": n, "mem_mb": mem,
                                                               "cpus": round(cpu, 3)},
            "free_pct": r["free_pct"], "load1": r["load1"]}
    if n > limits.max_containers:
        return "containers", info
    if mem > limits.max_mem_mb:
        return "memory_limits", info
    if cpu > limits.max_cpus + 1e-9:
        return "cpu_limits", info
    if r["free_pct"] < limits.min_free_pct:
        return "memory_free", info
    if r["load1"] >= limits.max_load1:
        return "load", info
    return None, info


_WARNED: set[str] = set()


def reserve(key: str, mem_mb: float, cpus: float, *, run_id: str | None = None,
            name: str | None = None, gate: Callable[[dict], str | None] | None = None,
            limits: Limits = LAPTOP, read: Callable[[], dict] = reading,
            clock: Callable[[], float] = time.time,
            log: Callable[[str], None] | None = None,
            warn_pending_s: float = WARN_PENDING_S,
            probe: Callable[[dict], bool] | None = None,
            native_probe: Callable[[], bool] | None = None
            ) -> tuple[bool, str | None, dict]:
    """Admit one container machine-wide and record its reservation, or say why not.
    Returns (ok, reason, info). Holds (False) on any reading or registry error."""
    if (run_id is None) == (name is None):
        raise ValueError("reserve needs exactly one of run_id or name")
    with _locked():
        try:
            r = read()
            now = clock()
            keep, orphans = reconcile(set(r["runs"]), set(r["names"]), now,
                                      probe or container_exists, log, warn_pending_s)
            native, n_orphans = native_pending(now, native_probe, log)
        except Exception as e:  # noqa: BLE001 - an unknown reading holds, never launches
            return False, "reading_failed", {"error": f"{type(e).__name__}: {e}"}
        orphans = orphans + n_orphans
        pending = [e for e in keep if not e.get("visible")] + native
        why, info = decide(r, pending, mem_mb, cpus, limits)
        info["native_pending"] = len(native)
        info["orphans"] = orphans
        if why is None and gate is not None:
            why = gate(info)
        if why is None:
            if any(e["key"] == key for e in keep):
                raise ValueError(f"reservation {key!r} already held")
            keep.append({"key": key, "pid": os.getpid(), "run_id": run_id, "name": name,
                         "mem_mb": mem_mb, "cpus": cpus, "t": now,
                         "utc": datetime.now(UTC).isoformat(timespec="seconds"),
                         "visible": False})
        elif orphans:
            why = f"{why}; unresolved orphan reservations: {orphan_text(orphans)}"
        _save(keep)
        return why is None, why, info


def reconcile(visible_runs: set[str], visible_names: set[str], now: float,
              probe: Callable[[dict], bool] | None = None,
              log: Callable[[str], None] | None = None,
              warn_pending_s: float = WARN_PENDING_S) -> tuple[list[dict], list[dict]]:
    """The one reconciliation both launchers' admissions run, under the held lock, against
    their docker snapshot: entries whose container is visible are marked so; a dead launcher's
    visible entry is dropped (docker counts its container); a dead launcher's pending entry (an
    orphan) is kept until two empty `docker ps -a` checks ORPHAN_CONFIRM_S apart, the first
    after the owner's death (a sighting starts them over). Saves the registry and returns
    (entries kept, unresolved orphans as {key, pid, status}). `probe` may raise (hold)."""
    probe = probe or container_exists
    r = {"runs": visible_runs, "names": visible_names}
    keep, orphans = [], []
    before = _load()
    for e in json.loads(json.dumps(before)):
        if not e.get("visible") and _visible(e, r):
            e["visible"] = True
        if not _alive(e.get("pid", 0)):
            if e.get("visible"):
                continue
            exists = probe(e)
            e["orphan_checks"] = [] if exists else [*e.get("orphan_checks", []), now]
            ch = e["orphan_checks"]
            if len(ch) >= 2 and ch[-1] - ch[0] >= ORPHAN_CONFIRM_S:
                if log:
                    log(f"laptop admission: orphan reservation {e['key']} dropped (no "
                        f"container on checks {ch[-1] - ch[0]:.0f} s apart)")
                continue
            if not e.get("orphan") and log:
                log(f"laptop admission: reservation {e['key']} orphaned (launcher pid "
                    f"{e.get('pid')} gone); counted until its container shows or two empty "
                    f"checks {ORPHAN_CONFIRM_S:.0f} s apart")
            e["orphan"] = True
            orphans.append({"key": e["key"], "pid": e.get("pid"),
                            "status": _orphan_status(ch, exists, now)})
        elif not e.get("visible") and now - e.get("t", now) > warn_pending_s \
                and e["key"] not in _WARNED:
            _WARNED.add(e["key"])
            if log:
                log(f"laptop admission: reservation {e['key']} pending "
                    f"{now - e['t']:.0f} s with no visible container")
        keep.append(e)
    if keep != before:  # written only when something changed
        _save(keep)
    return keep, orphans


def pending_summary(visible_run_ids: set[str] | None = None, *, run: Callable = _run,
                    probe: Callable[[dict], bool] | None = None,
                    clock: Callable[[], float] = time.time,
                    log: Callable[[str], None] | None = None) -> dict:
    """This registry's pending containers (not yet visible, orphans included), for another
    launcher's gate, after `reconcile`. Call under `locked()`. Pass `visible_run_ids`, the
    emx.run labels of the running emx containers from the SAME docker snapshot the caller
    counted its running usage from; without it this reads its own (a second snapshot).
    Raises on any docker or registry error (the caller holds)."""
    names: set[str] = set()
    if visible_run_ids is None:
        p = run(["docker", "ps", "--filter", f"label={LABEL}", "--format",
                 "{{.Names}}\t{{.Label \"" + RUN_LABEL + "\"}}"])
        if p.returncode != 0:
            raise RuntimeError(f"docker ps failed rc={p.returncode}: {p.stderr.strip()[-200:]}")
        rows = [x.split("\t") for x in p.stdout.splitlines() if x.strip()]
        names = {x[0] for x in rows}
        visible_run_ids = {x[1] for x in rows if len(x) > 1 and x[1]}
    keep, orphans = reconcile(set(visible_run_ids), names, clock(), probe, log)
    pend = [e for e in keep if not e.get("visible")]
    return {"n": len(pend), "mem_gb": sum(e["mem_mb"] for e in pend) / 1024,
            "cpus": sum(e["cpus"] for e in pend), "keys": [e["key"] for e in pend],
            "orphans": orphans}


# ------------------------------------------------------------------------------------------
# One batch at a time: the controls' go lock and the checks each launcher makes
# ------------------------------------------------------------------------------------------


def go_lock_path() -> Path:
    return state_dir() / "controls_go.lock"


@contextlib.contextmanager
def go_lock():
    """Held by a controls `go` for its whole run. RuntimeError if another holds it."""
    p = go_lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"a controls go holds {p}") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def go_lock_held() -> bool:
    p = go_lock_path()
    if not p.exists():
        return False
    with open(p, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def controls_active() -> str | None:
    """Why another launcher must not start on the laptop now: a controls `go` is running, or
    this registry holds a pending reservation of a live launcher. None otherwise. Raises on an
    unreadable registry (the caller holds)."""
    if go_lock_held():
        return f"a controls go is running ({go_lock_path()})"
    live = [e for e in _load() if not e.get("visible") and _alive(e.get("pid", 0))]
    if live:
        return ("week-2 laptop reservations pending: "
                + ", ".join(f"{e['key']} (pid {e['pid']})" for e in live))
    return None


def release(key: str) -> None:
    """Drop a reservation (launch cancelled or failed, or the run is over)."""
    with _locked():
        rows = _load()
        _save([e for e in rows if e.get("key") != key])


class LaptopAdmission:
    """driver.Admission's interface over the machine-wide reserve/release, with a ramp."""

    def __init__(self, *, mem_mb: int, cpus: float, max_agents: int, ramp_agents: int,
                 ramp_after_s: float, limits: Limits = LAPTOP, poll_s: float = 30.0,
                 read: Callable[[], dict] = reading, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] | None = None,
                 probe: Callable[[dict], bool] | None = None):
        if ramp_agents < 1 or ramp_agents > max_agents:
            raise ValueError(f"ramp_agents {ramp_agents} must be in 1..{max_agents}")
        if max_agents > limits.max_containers or max_agents * cpus > limits.max_cpus \
                or max_agents * mem_mb > limits.max_mem_mb:
            raise ValueError(f"{max_agents} agents x {cpus} CPU x {mem_mb} MB exceed {limits}")
        self.mem_mb, self.cpus, self.max_agents = mem_mb, cpus, max_agents
        self.ramp_agents, self.ramp_after_s = ramp_agents, ramp_after_s
        self.limits, self.poll_s = limits, poll_s
        self.read, self.clock, self.log, self.probe = read, clock, log, probe
        self.lock = threading.Lock()  # this process's slots; the flock covers the machine
        self.live: dict[str, float] = {}  # reservation key -> admitted at
        self.matured = 0  # finished slots held for at least ramp_after_s
        self.ramp_open = False
        self._n = 0
        self.last: dict | None = None
        self.stats = {"admitted": 0, "waits": 0, "max_live": 0, "blocked_by": {},
                      "ramp_opened": None}

    def _allowed(self, now: float) -> int:
        if not self.ramp_open:
            held = self.matured + sum(now - t >= self.ramp_after_s for t in self.live.values())
            if held >= self.ramp_agents:
                self.ramp_open = True
                self.stats["ramp_opened"] = {"after_slots": held, "at": now}
                if self.log:
                    self.log(f"laptop admission: ramp open, {held} slots held >= "
                             f"{self.ramp_after_s:.0f} s; up to {self.max_agents} agents")
        return self.max_agents if self.ramp_open else self.ramp_agents

    def try_admit(self, key: str, run_id: str | None = None, name: str | None = None) -> bool:
        """One admission attempt (under this process's lock and the machine-wide flock)."""
        with self.lock:
            allowed = self._allowed(self.clock())
            if len(self.live) + 1 > allowed:
                why = "ramp" if allowed < self.max_agents else "max_agents"
                ok, info = False, {}
            else:
                ok, why, info = reserve(key, self.mem_mb, self.cpus, run_id=run_id,
                                        name=name if run_id is None else None,
                                        limits=self.limits, read=self.read,
                                        clock=self.clock, log=self.log, probe=self.probe)
            self.last = {"why": why, **info}
            if ok:
                self.live[key] = self.clock()
                self.stats["admitted"] += 1
                self.stats["max_live"] = max(self.stats["max_live"], len(self.live))
            else:
                self.stats["waits"] += 1
                code = why.split(";")[0]  # the reason, without the orphan list
                self.stats["blocked_by"][code] = self.stats["blocked_by"].get(code, 0) + 1
            return ok

    def done(self, key: str) -> None:
        with self.lock:
            t0 = self.live.pop(key, None)
            if t0 is not None and self.clock() - t0 >= self.ramp_after_s:
                self.matured += 1
        release(key)

    @contextlib.contextmanager
    def slot(self, kind: str, stop: Callable[[], bool] | None = None, *,
             run_id: str | None = None):
        """Hold one agent container's reservation while the block (the container's life and
        cleanup) runs; wait while the rule says no. `run_id`: the invocation whose container
        this is (its emx.run label); without it the reservation stays pending until released
        (counted conservatively)."""
        if kind != "agent":
            raise ValueError("laptop admission runs agents only (no graders here)")
        if stop is None:
            from compaction.s2.chain.driver import stop_requested as stop
        from compaction.s2 import loop as L
        with self.lock:
            self._n += 1
            key = f"{os.getpid()}-{id(self)}-{self._n}"
        while True:
            if stop():
                raise L.Stopped("stop requested while waiting for laptop room")
            if self.try_admit(key, run_id=run_id, name=None if run_id else f"unmatched:{key}"):
                break
            time.sleep(self.poll_s)
        try:
            yield
        finally:
            self.done(key)

    @contextlib.contextmanager
    def grader(self, stop=None):
        raise ValueError("laptop admission runs agents only (no graders here)")
        yield  # pragma: no cover


def install(*, mem_mb: int, cpus: float, max_agents: int, ramp_agents: int,
            ramp_after_s: float = 0.0, poll_s: float = 30.0, log=None) -> LaptopAdmission:
    """Run a driver's agent containers on the laptop under this module's rule: their CPU and
    memory limits (read by env.py at call time) and LaptopAdmission as driver.ADMISSION
    (graders refused). Returns the admission object."""
    from compaction.s2.chain import config as CC
    from compaction.s2.chain import driver as D
    D.set_agent_memory(mem_mb)
    CC.AGENT_CPUS = cpus
    D.ADMISSION = LaptopAdmission(mem_mb=mem_mb, cpus=cpus, max_agents=max_agents,
                                  ramp_agents=ramp_agents, ramp_after_s=ramp_after_s,
                                  poll_s=poll_s, log=log or D.status)
    return D.ADMISSION
