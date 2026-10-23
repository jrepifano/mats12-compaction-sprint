"""Several Docker hosts at once (spec "Docker hosts"; Jacob, 22 Oct).

- `Host`: a name, a daemon target (`docker -H`; None = the CLI's default, i.e. the laptop's own
  Docker), a container limit and a run limit. `parse_hosts("desktop=ssh://u@h,12,4;laptop=
  local,6,2")`; `default_hosts()` reads SPRINT_S2_HOSTS, else the laptop alone.
- `HostPool`: one container semaphore per host (every sandbox the harness starts on a host,
  graders included, holds one slot until it is closed; no thread ever holds two slots, so the
  limits cannot deadlock), one "down" event per host, and a scheduler that runs each job on
  the host it was assigned, at most `max_runs` at a time per host.
- Assignment is by DeepSWE task, never by condition: all runs of a task go to one host
  (`assign`), and a run's four arms and their repeats always fork from its boundary image on
  that run's host. Only a whole task group's unstarted or restarted runs can move to another
  host, when theirs is down.
- A host that drops: the run's failing call (or the pool's probe after an error) marks it down;
  its event stops every loop on it at the next model call; its runs end `failed_host` with
  their cleanup deferred to `pending_sweeps.jsonl` (no minutes of retries against a dead
  daemon); runs on the other host are untouched. `resume_action` decides how each run resumes.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field

from compaction.deepswe import sandbox as SBX
from compaction.deepswe.sandbox import Invocation, Sandbox, SandboxError, docker_argv

HOSTS_ENV = "SPRINT_S2_HOSTS"
PROBE_TIMEOUT_S = 20
PULL_TIMEOUT_S = 1800


@dataclass(frozen=True)
class Host:
    name: str
    docker_host: str | None  # docker -H target; None = the docker CLI's default
    max_containers: int = 6
    max_runs: int = 2
    platform: str = "linux/amd64"

    def describe(self) -> dict:
        return {"name": self.name, "docker_host": self.docker_host or "local",
                "max_containers": self.max_containers, "max_runs": self.max_runs,
                "platform": self.platform}


def parse_hosts(spec: str) -> list[Host]:
    """"name=target,max_containers[,max_runs];..." with target "local" for the CLI default.
    max_runs defaults to max_containers // 3 (a run uses 1 container, then its arms up to 4)."""
    out = []
    for part in [p.strip() for p in spec.split(";") if p.strip()]:
        name, _, rest = part.partition("=")
        fields = [f.strip() for f in rest.split(",")]
        if not name or not fields[0]:
            raise ValueError(f"bad host entry {part!r}")
        target = None if fields[0] == "local" else fields[0]
        mc = int(fields[1]) if len(fields) > 1 else 6
        mr = int(fields[2]) if len(fields) > 2 else max(1, mc // 3)
        out.append(Host(name.strip(), target, mc, mr))
    names = [h.name for h in out]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate host names in {spec!r}")
    return out


def default_hosts() -> list[Host]:
    spec = os.environ.get(HOSTS_ENV)
    return parse_hosts(spec) if spec else [Host("laptop", None, 6, 2)]


def check_unambiguous(hosts: list[Host]) -> None:
    """A host with target None means the CLI's default; refuse when DOCKER_HOST is set in the
    environment, which would silently send the "local" host's containers elsewhere."""
    if os.environ.get("DOCKER_HOST") and any(h.docker_host is None for h in hosts):
        raise ValueError("DOCKER_HOST is set; give every host an explicit target instead of "
                         "'local' (e.g. unix:///var/run/docker.sock)")


def probe_host(host: Host, timeout: float = PROBE_TIMEOUT_S) -> bool:
    try:
        p = subprocess.run(docker_argv(["version", "--format", "{{.Server.Version}}"],
                                       host.docker_host),
                           capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return p.returncode == 0 and bool(p.stdout.strip())


def image_present(host: Host, image: str, timeout: float = 60) -> bool | None:
    try:
        p = subprocess.run(docker_argv(["image", "inspect", "--format", "{{.Id}}", image],
                                       host.docker_host),
                           capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return p.returncode == 0


def ensure_image(host: Host, image: str) -> None:
    if image_present(host, image):
        return
    p = subprocess.run(docker_argv(["pull", "--platform", host.platform, image],
                                   host.docker_host),
                       capture_output=True, text=True, timeout=PULL_TIMEOUT_S, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"pull of {image} on {host.name} failed: {p.stderr[-500:]}")


def list_ours(host: Host, label: str) -> dict | None:
    """Containers and snapshot images of ours on one host; None if the host didn't answer."""
    try:
        c = subprocess.run(docker_argv(["ps", "-a", "--filter", f"label=emx.sandbox={label}",
                                        "--format", "{{.Names}}"], host.docker_host),
                           capture_output=True, text=True, timeout=60, check=False)
        i = subprocess.run(docker_argv(["images", "--filter", "reference=emx-sandbox/*",
                                        "--format", "{{.Repository}}:{{.Tag}}"],
                                       host.docker_host),
                           capture_output=True, text=True, timeout=60, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if c.returncode or i.returncode:
        return None
    return {"containers": c.stdout.split(), "images": i.stdout.split()}


# ------------------------------------------------------------------------------------------
# Transport failures, container slots
# ------------------------------------------------------------------------------------------

# Messages from the docker client (not from commands inside the container: agent commands run
# with stderr merged into stdout, so the client's stderr carries only its own errors).
TRANSPORT_PATTERNS = re.compile(
    r"cannot connect to the docker daemon|error during connect|connection refused|"
    r"connection reset|broken pipe|no route to host|i/o timeout|ssh: |ssh_exchange|"
    r"context deadline exceeded|is the docker daemon running|host is down|"
    r"unexpected eof|network is unreachable", re.IGNORECASE)


def is_transport_error(text: str) -> bool:
    return bool(TRANSPORT_PATTERNS.search(text or ""))


class HostDown(SandboxError):
    """The run's host is marked down: no new container starts there."""


@dataclass
class SlotSandbox(Sandbox):
    """A sandbox that holds one of its host's container slots until closed (idempotently), and
    reports transport failures to the pool at the failing operation."""

    release: Callable[[], None] | None = None
    host_down: threading.Event | None = None
    on_transport: Callable[[], bool] | None = None  # probes the host; marks it down

    def _guarded(self, args, timeout, input_bytes=None):
        """As Sandbox._guarded, but a client timeout or a transport error probes the host first,
        so a dead host is marked down before any (deferred) removal is attempted."""
        if self.host_down is not None and self.host_down.is_set():
            raise HostDown(f"host of {self.name} is down")
        with SBX.using_host(self.host):
            try:
                p = SBX._docker(args, timeout=timeout, check=False, input_bytes=input_bytes)
            except subprocess.TimeoutExpired:
                self._transport("client timeout", timed_out=True)
                self.close()
                raise SandboxError(f"client timeout on {self.name}; sandbox removed or "
                                   "removal deferred") from None
            except BaseException:
                self.close()
                raise
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace") if isinstance(p.stderr, bytes) else p.stderr
            if is_transport_error(err):
                self._transport(err)
                if self.host_down is not None and self.host_down.is_set():
                    self.release_slot()
                    raise HostDown(f"host of {self.name} went down: {err[-300:]}")
        return p

    def _transport(self, text: str, timed_out: bool = False) -> None:
        if self.on_transport is not None and (timed_out or is_transport_error(text)):
            self.on_transport()

    def close(self) -> bool:
        try:
            if self.host_down is not None and self.host_down.is_set():
                return False  # remote removal deferred to the pending sweep
            return super().close()
        finally:
            self.release_slot()

    def release_slot(self) -> None:
        r, self.release = self.release, None
        if r is not None:
            r()


@dataclass
class HostInvocation(Invocation):
    """An Invocation on one host whose every container holds one of the host's slots."""

    slots: threading.BoundedSemaphore | None = None
    host_down: threading.Event | None = None
    on_transport: Callable[[], bool] | None = None

    def start(self, image: str, **kw) -> Sandbox:
        if self.host_down is not None and self.host_down.is_set():
            raise HostDown(f"host {self.host} is down; not starting {image}")
        kw.setdefault("host", self.host)
        if self.slots is not None:
            self.slots.acquire()
        try:
            # The host may have gone down while this call waited for its slot (review r2 #5).
            if self.host_down is not None and self.host_down.is_set():
                raise HostDown(f"host {self.host} went down while waiting; not starting {image}")
            sb = self._start(image, **kw)
        except BaseException:
            if self.slots is not None:
                self.slots.release()
            raise
        sb.release = self.slots.release if self.slots is not None else None
        sb.host_down, sb.on_transport = self.host_down, self.on_transport
        self.sandboxes.append(sb)
        return sb

    def _start(self, image: str, *, name: str | None = None, lifetime_s: int = 4 * 3600,
               cpus: float | None = 2, memory_mb: int | None = 8192,
               platform: str | None = "linux/amd64", network: str = "none",
               host: str | None = None) -> SlotSandbox:
        """Sandbox.start, except that a failed start is classified first: a client timeout or a
        docker-client transport error probes the host, and if the host is down no removal is
        tried against it (the run's sweep is deferred); otherwise the half-created container is
        removed and confirmed, as Sandbox.start does."""
        name = name or (f"{self.purpose}-{self.run_id + '-' if self.run_id else ''}"
                        f"{uuid.uuid4().hex[:8]}")
        t0 = time.monotonic()
        args = SBX.run_args(image, name, self.purpose, lifetime_s, cpus, memory_mb, platform,
                            network, self.run_id)

        def failed(text: str, timed_out: bool = False) -> None:
            if self.on_transport is not None and (timed_out or is_transport_error(text)):
                self.on_transport()
            if self.host_down is not None and self.host_down.is_set():
                raise HostDown(f"host {host} down at container start: {text[-300:]}")
            with SBX.using_host(host):
                SBX.remove_container(name)

        with SBX.using_host(host):
            try:
                p = SBX._docker(args, timeout=300, check=False)
            except subprocess.TimeoutExpired:
                failed("client timeout at docker run", timed_out=True)
                raise SandboxError(f"docker run of {name} timed out; container removed") from None
            except BaseException:
                SBX.remove_container(name)
                raise
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace") if isinstance(p.stderr, bytes) else p.stderr
            failed(err)
            raise SandboxError(f"docker run rc={p.returncode}: {err[-2000:]}")
        sb = SlotSandbox(name=name, image=image, purpose=self.purpose, run_id=self.run_id,
                         host=host)
        sb.log.append({"op": "start", "seconds": time.monotonic() - t0})
        return sb

    def release_slots(self) -> None:
        """Release every slot still held by this invocation's sandboxes (idempotent)."""
        for sb in self.sandboxes:
            if isinstance(sb, SlotSandbox):
                sb.release_slot()

    def cleanup(self) -> list[str]:
        try:
            if self.host_down is not None and self.host_down.is_set():
                return []  # the caller defers the remote sweep; slots are released below
            return super().cleanup()
        finally:
            self.release_slots()


# ------------------------------------------------------------------------------------------
# Pool and scheduler
# ------------------------------------------------------------------------------------------


@dataclass
class Job:
    key: str
    task: str
    host: str
    fn: Callable[[Host], dict]


@dataclass
class JobResult:
    key: str
    task: str
    host: str
    status: str  # "ok" | "failed" | "failed_host" | "not_started_host_down" | "stopped"
    result: dict | None = None
    error: str | None = None


@dataclass
class HostPool:
    hosts: list[Host]
    probe: Callable[[Host], bool] = probe_host
    stop: threading.Event | None = None  # the runner's global stop flag
    slots: dict = field(init=False)
    down: dict = field(init=False)
    reasons: dict = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.by_name = {h.name: h for h in self.hosts}
        self.slots = {h.name: threading.BoundedSemaphore(h.max_containers) for h in self.hosts}
        self.down = {h.name: threading.Event() for h in self.hosts}
        self._lock = threading.Lock()

    def host(self, name: str) -> Host:
        return self.by_name[name]

    def is_down(self, name: str) -> bool:
        return self.down[name].is_set()

    def up_hosts(self) -> list[Host]:
        return [h for h in self.hosts if not self.is_down(h.name)]

    def mark_down(self, name: str, reason: str) -> None:
        with self._lock:
            self.reasons.setdefault(name, reason)
        self.down[name].set()

    def check(self, name: str) -> bool:
        """Probe the host; mark it down if it doesn't answer. True when up."""
        if self.is_down(name):
            return False
        ok = self.probe(self.by_name[name])
        if not ok:
            self.mark_down(name, "probe failed")
        return ok

    def invocation(self, name: str, purpose: str, run_id: str | None = None) -> HostInvocation:
        kw = {"run_id": run_id} if run_id else {}
        return HostInvocation(purpose, host=self.by_name[name].docker_host,
                              slots=self.slots[name], host_down=self.down[name],
                              on_transport=lambda: self.check(name), **kw)

    @contextmanager
    def slot(self, name: str):
        self.slots[name].acquire()
        try:
            yield
        finally:
            self.slots[name].release()

    def assign(self, task_runs: dict[str, int], fixed: dict[str, str] | None = None
               ) -> dict[str, str]:
        """task -> host name. `fixed` tasks keep their host (task affinity: a task with a run
        already on an up host stays there). The rest go, largest first, to the up host with the
        lowest load per run slot."""
        fixed = {t: h for t, h in (fixed or {}).items()
                 if h in self.by_name and not self.is_down(h)}
        up = self.up_hosts()
        if not up:
            raise RuntimeError("no Docker host is up")
        load = defaultdict(float)
        for t, h in fixed.items():
            load[h] += task_runs.get(t, 0)
        out = dict(fixed)
        for t in sorted(task_runs, key=lambda t: (-task_runs[t], t)):
            if t in out:
                continue
            h = min(up, key=lambda h: ((load[h.name] + task_runs[t]) / h.max_runs,
                                       load[h.name], h.name))
            out[t] = h.name
            load[h.name] += task_runs[t]
        return out

    def run(self, jobs: list[Job]) -> list[JobResult]:
        """Run each job on its assigned host, at most max_runs at a time per host. A job whose
        host is down is not started; a job that fails triggers a probe of its host."""
        queues: dict[str, list[Job]] = defaultdict(list)
        for j in jobs:
            queues[j.host].append(j)
        results: list[JobResult] = []
        lock = threading.Lock()

        def worker(name: str) -> None:
            while True:
                with lock:
                    if not queues[name]:
                        return
                    j = queues[name].pop(0)
                if self.stop is not None and self.stop.is_set():
                    r = JobResult(j.key, j.task, name, "stopped")
                elif self.is_down(name):
                    r = JobResult(j.key, j.task, name, "not_started_host_down",
                                  error=self.reasons.get(name))
                else:
                    r = self._one(j, name)
                with lock:
                    results.append(r)

        threads = [(h.name, k) for h in self.hosts if queues[h.name]
                   for k in range(min(h.max_runs, len(queues[h.name])))]
        unknown = set(queues) - set(self.by_name)
        if unknown:
            raise KeyError(f"jobs assigned to unknown hosts {sorted(unknown)}")
        with ThreadPoolExecutor(max_workers=max(1, len(threads))) as ex:
            futs = [ex.submit(worker, n) for n, _ in threads]
            try:
                for f in futs:
                    f.result()
            except BaseException:
                if self.stop is not None:  # Ctrl-C: workers wind down and clean up
                    self.stop.set()
                raise
        order = {j.key: i for i, j in enumerate(jobs)}
        return sorted(results, key=lambda r: order[r.key])

    def _one(self, j: Job, name: str) -> JobResult:
        try:
            res = j.fn(self.by_name[name])
        except Exception as e:  # noqa: BLE001 - classified by the host probe
            status = "failed" if self.check(name) else "failed_host"
            return JobResult(j.key, j.task, name, status, error=f"{type(e).__name__}: {e}")
        if res.get("errors") and not self.check(name):
            return JobResult(j.key, j.task, name, "failed_host", res,
                             error=self.reasons.get(name))
        return JobResult(j.key, j.task, name, "ok", res)


# ------------------------------------------------------------------------------------------
# Resume decisions
# ------------------------------------------------------------------------------------------

FINAL_STATES = ("complete", "run_cap")


def _capped(status: dict | None) -> bool:
    return bool(status) and (status.get("state") == "run_cap" or bool(status.get("cap_stopped")))


def is_final(status: dict | None) -> bool:
    """Finished: complete, or stopped by a run cap (even when its cleanup failed)."""
    return bool(status) and (status.get("state") in FINAL_STATES or _capped(status))



def resume_action(status: dict | None, host_up: bool, image_present: bool | None,
                  restart_capped: bool = False) -> str:
    """What to do with one cohort run given its status file:
    - "fresh": never started; "skip": finished, including a run-cap stop (terminal; restarted
      only with `restart_capped`, the CLI's --restart-capped);
    - "resume_arms": its boundary exists, its host is up and still holds the boundary image,
      so the missing arms fork there (same host, same boundary: contrasts stay within host);
    - "restart": anything else (no boundary yet, or the boundary is unreachable): a new attempt
      from scratch; the old attempt's partial rows are superseded, never mixed in."""
    if status is None:
        return "fresh"
    if _capped(status):
        # A cap stop is terminal whatever its cleanup did (review r3-fixes #1); a paid restart
        # needs the explicit flag.
        return "restart" if restart_capped else "skip"
    if status.get("state") in FINAL_STATES:
        return "skip"
    if status.get("boundary_tag") and host_up and image_present:
        return "resume_arms"
    return "restart"
