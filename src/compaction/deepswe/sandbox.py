"""A DeepSWE task image as a bounded, forkable Docker sandbox for our own agent loop.

Contract (AGENTS.md, "Nothing we start may outlive its job"):
- every container gets --rm, a --name, the label emx.sandbox=<purpose> and a per-invocation label
  emx.run=<run id>, so one invocation's cleanup can never touch another's containers;
- the container's main process is `timeout -k 5 <lifetime> sleep infinity`, so it dies on its own;
- every operation inside the container (exec, put_file, read_file, copy_in) runs under
  `timeout -k 5 <s>` inside; the client waits a little longer. If the client times out, or is
  interrupted (KeyboardInterrupt, SystemExit) during start or any operation, the sandbox is
  removed (`docker rm -f`) before the exception propagates;
- removal is confirmed only by a status query that itself succeeded (rc 0); a failed query or a
  container still listed is reported by name as unconfirmed, never as gone.

Forking: `snapshot()` runs `docker commit` (filesystem only: no processes, no memory, no shell
state), and `Sandbox.start(snapshot_tag, ...)` starts an independent copy. A fork is a faithful
continuation only at a *quiescent boundary*: before committing, `snapshot()` lists the container's
processes with `ps` and refuses (NotQuiescentError) if anything runs besides the container's own
`timeout ... sleep infinity` and the check itself, e.g. a server or build an agent backgrounded
with `&`. The agent loop must stop such processes (and decide what their leftover pid/socket files
mean) before the boundary; services are not restarted in the fork. Forks also get a new hostname
(the container id) and a fresh process clock.

Cleanup is per invocation: `Invocation` owns a run id, labels everything it starts with it, tracks
every snapshot tag *before* the commit starts, and on exit removes only its own containers and
images, confirming each.

An interrupted or timed-out `docker commit` may still be running in the daemon, and removing the
source container does not prove it was cancelled (the daemon exports the filesystem, then creates
and tags the image). `snapshot()` therefore removes the source container and then treats the commit
as resolved only when its image is observed and removed (`resolve_commit`, bounded polling).
Otherwise the tag stays in `unresolved_commits` and cleanup reports it as unconfirmed. A late
sweep catches a commit that completes afterwards: `Invocation.cleanup`, or after the process has
gone `python -m compaction.deepswe.sandbox sweep --run-id <id>`, removes every container and image
carrying the run's label (including untagged ones) or a snapshot tag with the run id.

Grading mirrors Harbor's separate verifier environment: run the task's [[verifier.collect]] command
in the agent container to write /logs/artifacts/model.patch, then start a fresh container from the
pristine task image, copy tests/ to /tests and the patch to /logs/artifacts/, and run
/tests/test.sh, which writes /logs/verifier/reward.json. `parse_reward` separates a valid task
failure (reward 0) from an infrastructure failure (no reward file, the -1 crash sentinel, or a
malformed or inconsistent reward), which raises VerifierError.
"""

from __future__ import annotations

import contextlib
import contextvars
import io
import json
import shlex
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

LABEL_KEY = "emx.sandbox"
RUN_LABEL_KEY = "emx.run"
CLIENT_GRACE_S = 30
CONFIRM_ATTEMPTS = 10
CONFIRM_SLEEP_S = 1.0
COMMIT_SETTLE_POLLS = 30
COMMIT_POLL_S = 2.0


# The Docker daemon to talk to: None = the docker CLI's own default (DOCKER_HOST or the current
# context); otherwise a daemon URL passed as `docker -H <url>` (e.g. ssh://user@desktop). Each
# Sandbox and Invocation records its host and re-enters it for every call, so code running
# against several hosts at once (one thread per run) never crosses daemons.
_HOST: contextvars.ContextVar[str | None] = contextvars.ContextVar("emx_docker_host",
                                                                  default=None)


@contextlib.contextmanager
def using_host(host: str | None):
    token = _HOST.set(host)
    try:
        yield
    finally:
        _HOST.reset(token)


def current_host() -> str | None:
    return _HOST.get()


def docker_argv(args: list[str], host: str | None = None) -> list[str]:
    h = host if host is not None else _HOST.get()
    return ["docker", *(["-H", h] if h else []), *args]


class SandboxError(RuntimeError):
    pass


class NotQuiescentError(SandboxError):
    """A snapshot was refused because processes besides the sandbox's own were running."""


class VerifierError(SandboxError):
    """The verifier did not produce a valid reward: an infrastructure failure, not a task fail."""


def _docker(args: list[str], timeout: float, check: bool = True,
            input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(docker_argv(args), check=False, capture_output=True, timeout=timeout,
                       input=input_bytes)
    if check and p.returncode != 0:
        raise SandboxError(f"docker {args[0]} rc={p.returncode}: {p.stderr.decode()[-2000:]}")
    return p


def _listed(query: list[str], wanted: str) -> bool | None:
    """Run a `docker ... --format` listing; True/False if `wanted` is/isn't a line of it,
    None when the query itself failed (daemon down, timeout), so nothing can be confirmed."""
    try:
        p = _docker(query, timeout=30, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode != 0:
        return None
    return wanted in p.stdout.decode(errors="replace").split()


def _confirm_gone(query: list[str], wanted: str, attempts: int) -> bool:
    for i in range(attempts):
        listed = _listed(query, wanted)
        if listed is False:
            return True
        if i + 1 < attempts:
            time.sleep(CONFIRM_SLEEP_S)  # --rm removal can still be in progress
    return False


def remove_container(name: str, attempts: int = CONFIRM_ATTEMPTS) -> bool:
    """docker rm -f, then confirm with a successful `docker ps -a` that `name` is not listed.
    Returns True only when confirmed gone; otherwise prints the name and returns False."""
    try:
        _docker(["rm", "-f", name], timeout=60, check=False)
    except (subprocess.TimeoutExpired, OSError):
        pass  # the status query below decides
    gone = _confirm_gone(["ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],
                         name, attempts)
    if not gone:
        print(f"[sandbox] WARNING: container {name} not confirmed removed (still listed, or the "
              "status query failed); remove it by hand: docker rm -f " + name)
    return gone


def remove_image(tag: str, attempts: int = CONFIRM_ATTEMPTS) -> bool:
    """docker image rm -f, then confirm with a successful `docker images` that `tag` is gone."""
    try:
        _docker(["image", "rm", "-f", tag], timeout=120, check=False)
    except (subprocess.TimeoutExpired, OSError):
        pass
    repo = tag.rpartition(":")[0]
    gone = _confirm_gone(["images", repo, "--format", "{{.Repository}}:{{.Tag}}"], tag, attempts)
    if not gone:
        print(f"[sandbox] WARNING: image {tag} not confirmed removed; docker image rm -f {tag}")
    return gone


def resolve_commit(container: str, tag: str, since_epoch: float | None = None,
                   polls: int = COMMIT_SETTLE_POLLS) -> bool:
    """Settle a `docker commit` of `container` to `tag` whose client was interrupted or timed out.

    True only when the tagged image is observed and then removed and confirmed gone. An absent
    tag never counts: neither the source container's destruction nor any number of empty polls
    proves the daemon won't still create and tag the image. Gives up after `polls` polls (about
    polls × COMMIT_POLL_S seconds) and returns False; the caller keeps the commit unresolved and
    a later sweep (`sweep(run_id)`) removes the image if it appears. `since_epoch` is unused and
    kept for the callers' record."""
    repo = tag.rpartition(":")[0]
    for i in range(max(1, polls)):
        if _listed(["images", repo, "--format", "{{.Repository}}:{{.Tag}}"], tag):
            return remove_image(tag)
        if i + 1 < polls:
            time.sleep(COMMIT_POLL_S)
    print(f"[sandbox] WARNING: commit of {container} to {tag} UNRESOLVED: its image was not "
          f"observed; rerun the sweep later: docker image rm -f {tag}")
    return False


def _list(args: list[str]) -> list[str] | None:
    """Lines of a docker listing, or None when the query failed."""
    try:
        p = _docker(args, timeout=30, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return p.stdout.decode(errors="replace").split() if p.returncode == 0 else None


def remove_image_id(image_id: str, attempts: int = CONFIRM_ATTEMPTS) -> bool:
    """Remove an (untagged) image by id and confirm with a successful `docker images -a`."""
    try:
        _docker(["image", "rm", "-f", image_id], timeout=120, check=False)
    except (subprocess.TimeoutExpired, OSError):
        pass
    gone = _confirm_gone(["images", "-a", "--format", "{{.ID}}"], image_id, attempts)
    if not gone:
        print(f"[sandbox] WARNING: image {image_id} not confirmed removed")
    return gone


def sweep(run_id: str) -> list[str]:
    """Remove every container and image of one run: containers labelled emx.run=<run_id>, images
    with that label (tagged by tag, untagged by id) and `emx-sandbox/*` tags containing the run
    id. Returns what was NOT confirmed gone, including failed queries. Safe to rerun at any time;
    it touches nothing without this run's label or id."""
    bad: list[str] = []
    lbl = f"label={RUN_LABEL_KEY}={run_id}"
    names = _list(["ps", "-a", "--filter", lbl, "--format", "{{.Names}}"])
    if names is None:
        bad.append(f"containers with {lbl} (status query failed)")
    bad += [n for n in names or [] if not remove_container(n)]
    tagged = _list(["images", "--filter", lbl, "--format", "{{.Repository}}:{{.Tag}}"])
    by_ref = _list(["images", "--filter", "reference=emx-sandbox/*", "--format",
                    "{{.Repository}}:{{.Tag}}"])
    dangling = _list(["images", "-a", "--filter", lbl, "--filter", "dangling=true",
                      "--format", "{{.ID}}"])
    if tagged is None or by_ref is None or dangling is None:
        bad.append(f"images of run {run_id} (status query failed)")
    tags = [t for t in tagged or [] if not t.endswith(":<none>")]
    tags += [t for t in by_ref or [] if f"-{run_id}-" in t and t not in tags]
    bad += [t for t in tags if not remove_image(t)]
    bad += [i for i in dangling or [] if not remove_image_id(i)]
    return bad


def run_args(image: str, name: str, purpose: str, lifetime_s: int, cpus: float | None,
             memory_mb: int | None, platform: str | None, network: str,
             run_id: str | None = None) -> list[str]:
    """The `docker run` argv for a sandbox (pure; tested without Docker)."""
    args = ["run", "-d", "--rm", "--name", name, "--label", f"{LABEL_KEY}={purpose}"]
    if run_id:
        args += ["--label", f"{RUN_LABEL_KEY}={run_id}"]
    args += ["--network", network, "--workdir", "/app"]
    if platform:
        args += ["--platform", platform]
    if cpus:
        args += ["--cpus", str(cpus)]
    if memory_mb:
        args += ["--memory", f"{memory_mb}m"]
    args += [image, "timeout", "-k", "5", str(int(lifetime_s)), "sleep", "infinity"]
    return args


def exec_args(name: str, cmd: str, timeout_s: int, workdir: str = "/app",
              interactive: bool = False) -> list[str]:
    """The `docker exec` argv: bash -lc under an in-container timeout (pure)."""
    return ["exec", *(["-i"] if interactive else []), "-w", workdir, name,
            "timeout", "-k", "5", str(int(timeout_s)), "bash", "-lc", cmd]


QUIESCE_TOKEN = "emx-quiesce-check"


def stray_processes(ps_output: str, token: str) -> list[str]:
    """Processes in `ps -eo pid=,ppid=,stat=,args=` output that break a quiescent boundary.

    Allowed: PID 1 (the sandbox's `timeout ... sleep infinity`), its `sleep infinity` child, the
    check's own process tree (rooted at the process whose args carry `token`) and zombies (already
    exited). Everything else is returned as its ps line."""
    procs: dict[int, tuple[int, str, str, str]] = {}
    for line in ps_output.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid, ppid, stat = int(parts[0]), int(parts[1]), parts[2]
        procs[pid] = (ppid, stat, parts[3] if len(parts) > 3 else "", line.strip())
    mine = {pid for pid, (_, _, args, _) in procs.items() if token in args}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, *_rest) in procs.items():
            if ppid in mine and pid not in mine:
                mine.add(pid)
                changed = True
    out = []
    for pid, (ppid, stat, args, line) in sorted(procs.items()):
        if pid == 1 or pid in mine or stat.startswith("Z"):
            continue
        if ppid == 1 and args.strip() == "sleep infinity":
            continue
        out.append(line)
    return out


@dataclass
class ExecResult:
    rc: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def timed_out(self) -> bool:
        return self.rc in (124, 137)


@dataclass
class Sandbox:
    name: str
    image: str
    purpose: str
    run_id: str | None = None
    log: list[dict] = field(default_factory=list)
    unresolved_commits: list[tuple[str, float]] = field(default_factory=list)  # (tag, since)
    host: str | None = None  # the daemon (docker -H); None = the CLI's default

    @classmethod
    def start(cls, image: str, purpose: str, *, name: str | None = None,
              run_id: str | None = None, lifetime_s: int = 4 * 3600,
              cpus: float | None = 2, memory_mb: int | None = 8192,
              platform: str | None = "linux/amd64", network: str = "none",
              host: str | None = None) -> Sandbox:
        name = name or f"{purpose}-{run_id + '-' if run_id else ''}{uuid.uuid4().hex[:8]}"
        host = host if host is not None else _HOST.get()
        t0 = time.monotonic()
        with using_host(host):
            try:
                _docker(run_args(image, name, purpose, lifetime_s, cpus, memory_mb, platform,
                                 network, run_id), timeout=300)
            except BaseException:
                # Timeout, failed start or interruption: the daemon may have created it anyway.
                remove_container(name)
                raise
        sb = cls(name=name, image=image, purpose=purpose, run_id=run_id, host=host)
        sb.log.append({"op": "start", "seconds": time.monotonic() - t0})
        return sb

    def _guarded(self, args: list[str], timeout: float,
                 input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
        """Run a docker command against this sandbox; on a client timeout or an interruption,
        remove the sandbox before the exception propagates."""
        with using_host(self.host):
            try:
                return _docker(args, timeout=timeout, check=False, input_bytes=input_bytes)
            except subprocess.TimeoutExpired:
                # The in-container timeout should have fired first; the sandbox is not
                # trustworthy.
                self.close()
                raise SandboxError(f"client timeout on {self.name}; sandbox removed") from None
            except BaseException:
                self.close()
                raise

    def exec(self, cmd: str, timeout_s: int = 600, workdir: str = "/app") -> ExecResult:
        t0 = time.monotonic()
        p = self._guarded(exec_args(self.name, cmd, timeout_s, workdir),
                          timeout=timeout_s + CLIENT_GRACE_S)
        r = ExecResult(p.returncode, p.stdout.decode(errors="replace"),
                       p.stderr.decode(errors="replace"), time.monotonic() - t0)
        self.log.append({"op": "exec", "cmd": cmd[:200], "rc": r.rc, "seconds": r.seconds})
        return r

    def _stdin_op(self, cmd: str, data: bytes, timeout_s: int, what: str) -> None:
        p = self._guarded(exec_args(self.name, cmd, timeout_s, "/", interactive=True),
                          timeout=timeout_s + CLIENT_GRACE_S, input_bytes=data)
        if p.returncode != 0:
            raise SandboxError(f"{what} rc={p.returncode}: {p.stderr.decode()[-1000:]}")

    def put_file(self, data: bytes, path: str, timeout_s: int = 120) -> None:
        d, q = shlex.quote(str(Path(path).parent)), shlex.quote(path)
        self._stdin_op(f"mkdir -p {d} && cat > {q}", data, timeout_s, f"put_file {path}")

    def copy_in(self, src: str | Path, dest: str, timeout_s: int = 300) -> None:
        """Copy a host directory's contents to `dest` (a tar stream into an exec under timeout)."""
        buf = io.BytesIO()
        def as_root(ti: tarfile.TarInfo) -> tarfile.TarInfo:
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            return ti

        with tarfile.open(fileobj=buf, mode="w") as tf:
            tf.add(str(src), arcname=".", filter=as_root)
        d = shlex.quote(dest)
        self._stdin_op(f"mkdir -p {d} && tar --no-same-owner -xf - -C {d}", buf.getvalue(),
                       timeout_s,
                       f"copy_in {dest}")

    def read_file(self, path: str, timeout_s: int = 120) -> bytes:
        p = self._guarded(["exec", self.name, "timeout", "-k", "5", str(int(timeout_s)),
                           "cat", path], timeout=timeout_s + CLIENT_GRACE_S)
        if p.returncode != 0:
            raise SandboxError(f"read_file {path} rc={p.returncode}: {p.stderr.decode()[-500:]}")
        return p.stdout

    def stray_processes(self) -> list[str]:
        token = f"{QUIESCE_TOKEN}-{uuid.uuid4().hex[:8]}"
        r = self.exec(f": {token}; ps -eo pid=,ppid=,stat=,args=", timeout_s=30, workdir="/")
        if r.rc != 0:
            raise SandboxError(f"ps failed in {self.name} rc={r.rc}: {r.stderr[-500:]}")
        return stray_processes(r.stdout, token)

    def snapshot(self, tag: str, *, labels: dict[str, str] | None = None) -> float:
        """docker commit the container's filesystem to `tag`; returns seconds taken.

        `labels` (e.g. {"emx.keep": <run key>}) replace the default emx.run label: the image
        then carries emx.run="" (the container's own emx.run label would otherwise be inherited
        by the commit) and is outside this run's sweep, so `Invocation.cleanup` keeps it; the
        caller owns its removal and must not track `tag` with `Invocation.new_tag`. The
        emx.sandbox purpose label is always set.

        Refuses (NotQuiescentError) unless the boundary is quiescent. The caller must already be
        tracking `tag` for cleanup (Invocation.new_tag does). If the commit fails, the tag is
        removed. If the client times out or is interrupted, the source container is removed and
        confirmed gone, and the commit counts as resolved only once `resolve_commit` observes and
        removes its image; otherwise it stays in `unresolved_commits` and is named in the error."""
        stray = self.stray_processes()
        if stray:
            raise NotQuiescentError(f"{self.name}: background processes at the snapshot "
                                    f"boundary: {stray}")
        with using_host(self.host):
            return self._commit(tag, labels)

    def _commit(self, tag: str, extra: dict[str, str] | None = None) -> float:
        t0 = time.monotonic()
        labels = [f"LABEL {LABEL_KEY}={self.purpose}"]
        if extra is not None:
            bad = [k for k in extra if k in (LABEL_KEY, RUN_LABEL_KEY)]
            if bad:
                raise ValueError(f"snapshot labels may not set {bad}")
            labels.append(f"LABEL {RUN_LABEL_KEY}=")  # not inherited from the container
            labels += [f"LABEL {k}={v}" for k, v in extra.items()]
        elif self.run_id:
            labels.append(f"LABEL {RUN_LABEL_KEY}={self.run_id}")
        since = time.time() - 1
        try:
            opts = [a for x in labels for a in ("-c", x)]
            _docker(["commit", "--pause=true", *opts, self.name, tag], timeout=900)
        except SandboxError:
            remove_image(tag)  # the daemon answered with an error: the commit is over
            raise
        except BaseException as e:
            # Timeout or interruption: the daemon may still be committing.
            self.unresolved_commits.append((tag, since))
            closed = self.close()
            settled = resolve_commit(self.name, tag, since)
            if settled:
                self.unresolved_commits.remove((tag, since))
            state = (f"source container removed={closed}; commit "
                     f"{'image observed and removed' if settled else 'UNRESOLVED: ' + tag}"
                     f" (run {self.run_id})")
            if isinstance(e, subprocess.TimeoutExpired):
                raise SandboxError(f"docker commit timed out on {self.name}; {state}") from None
            print(f"[sandbox] snapshot of {self.name} interrupted; {state}")
            raise
        s = time.monotonic() - t0
        self.log.append({"op": "snapshot", "tag": tag, "seconds": s})
        return s

    def close(self) -> bool:
        with using_host(self.host):
            return remove_container(self.name)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass
class Invocation:
    """Everything one invocation starts, labelled emx.run=<run_id>, so cleanup is scoped to it."""
    purpose: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    tags: list[str] = field(default_factory=list)
    sandboxes: list[Sandbox] = field(default_factory=list)
    host: str | None = None  # every container and image of this invocation lives on it

    def start(self, image: str, **kw) -> Sandbox:
        kw.setdefault("host", self.host)
        sb = Sandbox.start(image, self.purpose, run_id=self.run_id, **kw)
        self.sandboxes.append(sb)
        return sb

    def new_tag(self, stem: str) -> str:
        """A unique snapshot tag, tracked for cleanup before any commit is attempted."""
        tag = f"emx-sandbox/{self.purpose}:{stem}-{self.run_id}-{uuid.uuid4().hex[:6]}"
        self.tags.append(tag)
        return tag

    def cleanup(self) -> list[str]:
        """Remove this invocation's containers and images (the run sweep plus every tracked tag)
        and check its unresolved commits once more. Returns what was NOT confirmed gone (empty
        list = all confirmed by successful status queries). Rerun it, or `sweep --run-id`, later
        to catch an interrupted commit that completes afterwards."""
        with using_host(self.host):
            return self._cleanup()

    def _cleanup(self) -> list[str]:
        bad: list[str] = []
        for sb in self.sandboxes:  # first, so an image that has appeared counts as observed
            for tag, since in list(sb.unresolved_commits):
                if resolve_commit(sb.name, tag, since, polls=1):
                    sb.unresolved_commits.remove((tag, since))
                else:
                    bad.append(f"{tag} (interrupted commit unresolved; rerun: python -m "
                               f"compaction.deepswe.sandbox sweep --run-id {self.run_id})")
        bad += sweep(self.run_id)
        unresolved = {t for sb in self.sandboxes for t, _ in sb.unresolved_commits}
        bad += [t for t in self.tags if t not in unresolved and not remove_image(t)]
        return bad

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        bad = self.cleanup()
        if bad:
            msg = f"[sandbox] cleanup of run {self.run_id} NOT confirmed for: {bad}"
            print(msg)
            if exc[0] is None:
                raise SandboxError(msg)


def collect_patch(sb: Sandbox, collect_command: str, timeout_s: int = 300) -> bytes:
    r = sb.exec(collect_command, timeout_s=timeout_s, workdir="/")
    if r.rc != 0:
        raise SandboxError(f"collect failed rc={r.rc}: {r.stderr[-1000:]}")
    return sb.read_file("/logs/artifacts/model.patch")


REWARD_COUNTS = ("f2p_total", "f2p_passed", "p2p_total", "p2p_passed")


def parse_reward(kind: str, text: str) -> dict:
    """Validate the verifier's output. `kind` is "json" (reward.json), "txt" (reward.txt) or
    "none". Returns the reward dict for a valid outcome (reward 0 or 1, including apply_failed);
    raises VerifierError for an infrastructure failure: no reward file, the -1 crash sentinel,
    malformed JSON, missing or non-integer counts, or a reward inconsistent with its counts."""
    if kind == "none":
        raise VerifierError("verifier wrote no reward file")
    if kind == "txt":
        raise VerifierError(f"verifier wrote reward.txt (crash sentinel), not reward.json: "
                            f"{text.strip()[:200]!r}")
    try:
        rew = json.loads(text)
    except json.JSONDecodeError:
        raise VerifierError(f"malformed reward.json: {text[:200]!r}") from None
    if not isinstance(rew, dict):
        raise VerifierError(f"reward.json is not an object: {text[:200]!r}")
    r = rew.get("reward")
    if type(r) is not int or r not in (0, 1):
        raise VerifierError(f"reward must be the integer 0 or 1, got {r!r}")
    for k in REWARD_COUNTS:
        if type(rew.get(k)) is not int or rew[k] < 0:
            raise VerifierError(f"reward.json {k} must be a non-negative integer: {rew.get(k)!r}")
    if rew["f2p_passed"] > rew["f2p_total"] or rew["p2p_passed"] > rew["p2p_total"]:
        raise VerifierError(f"passed > total in reward.json: {rew}")
    solved = (rew["f2p_total"] > 0 and rew["f2p_passed"] == rew["f2p_total"]
              and rew["p2p_passed"] == rew["p2p_total"])
    if bool(r) != solved:
        raise VerifierError(f"reward {r} inconsistent with its counts: {rew}")
    return rew


READ_REWARD_CMD = ("cd /logs/verifier 2>/dev/null || { echo none; exit 0; }; "
                   "if [ -f reward.json ]; then echo json; cat reward.json; "
                   "elif [ -f reward.txt ]; then echo txt; cat reward.txt; else echo none; fi")


def grade(task_image: str, tests_dir: str | Path, model_patch: bytes, purpose: str, *,
          run_id: str | None = None, timeout_s: int = 1800,
          platform: str | None = "linux/amd64",
          host: str | None = None) -> tuple[dict, ExecResult]:
    """Harbor-style separate verifier: fresh pristine container, tests + patch in, test.sh.
    Returns (validated reward, the test.sh ExecResult); raises VerifierError when the verifier
    produced no valid reward (an infrastructure failure, distinct from reward 0)."""
    with Sandbox.start(task_image, purpose, run_id=run_id, lifetime_s=timeout_s + 300,
                       platform=platform, host=host) as v:
        v.copy_in(Path(tests_dir), "/tests")
        v.put_file(model_patch, "/logs/artifacts/model.patch")
        r = v.exec("chmod +x /tests/test.sh && bash /tests/test.sh", timeout_s=timeout_s,
                   workdir="/app")
        raw = v.exec(READ_REWARD_CMD, timeout_s=30, workdir="/")
    if r.timed_out:
        raise VerifierError(f"verifier timed out (rc={r.rc}) after {r.seconds:.0f} s")
    if raw.rc != 0:
        raise VerifierError(f"reading the reward failed rc={raw.rc}: {raw.stderr[-500:]}")
    kind, _, text = raw.stdout.partition("\n")
    try:
        return parse_reward(kind.strip(), text), r
    except VerifierError as e:
        raise VerifierError(f"{e}; test.sh rc={r.rc}; tail: {r.stdout[-800:]!r}") from None


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Remove one run's leftover sandboxes and snapshots.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sw = sub.add_parser("sweep", help="remove containers and images labelled emx.run=<id>")
    sw.add_argument("--run-id", required=True)
    sw.add_argument("--host", help="docker -H target (default: the CLI's own default)")
    a = ap.parse_args(argv)
    with using_host(a.host):
        bad = sweep(a.run_id)
    print(json.dumps({"run_id": a.run_id, "unconfirmed": bad}))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
