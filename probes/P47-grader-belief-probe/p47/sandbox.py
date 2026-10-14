"""P41 sandbox: per-episode Docker volumes and one short-lived container per command (plan.md
"Notes": holding a container per 8-10 min episode under the shared laptop's 8-container limit
would make the bulk run ~30 h).

An episode owns three named volumes mounted at /agent, /home/dev and /tmp. Each agent command
runs in a fresh `x3-sandbox:pilot` container on those volumes: a root wrapper runs the command
as `dev` (cwd /agent, HOME=/home/dev, X3's 60 s limit), kills anything the command left running,
then takes X3's snapshot (x3.task._SNAPSHOT_PY, run as root, as X3 does after every command).
What X3's long-lived container kept between commands and this does not: background processes
(counted per command in `bg_killed`).

Every container: `--rm`, `--name`, `--label emx.sandbox=P47`, `--network none`, an in-container
`timeout -k 5`, and `docker rm -f` (confirmed) when the client times out or is interrupted
(src/x3/passcheck.py's pattern). At most `C.MAX_CONTAINERS` run at once across processes: each
container holds one of N file-lock slots (`.slots/`); N can be lowered mid-run by writing a
number to `<probe>/.slots/LIMIT`. Volumes carry the same label and are removed when the episode
ends (`cleanup_orphans` removes any left by a killed process).
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from p47 import contracts as C
from x3 import contracts as X3C
from x3.passcheck import remove_container
from x3.task import _SNAPSHOT_PY

IMAGE = X3C.IMAGE_TAG
SLOTS_DIR = C.PROBE_DIR / ".slots"
LIMIT_FILE = SLOTS_DIR / "LIMIT"
CMD_INNER_LIMIT_S = 240        # the container stops itself (command 60 s + kill + snapshot)
CMD_CLIENT_TIMEOUT_S = 300     # later than the inner limit, so results don't change
UTIL_INNER_LIMIT_S = 600       # restore / export / read containers
UTIL_CLIENT_TIMEOUT_S = 660
VOLUME_TIMEOUT_S = 60
MOUNTS = {"agent": "/agent", "home": "/home/dev", "tmp": "/tmp"}


# --- slots: <= N containers at once across processes -------------------------------------------
def _limit() -> int:
    try:
        return max(1, min(C.MAX_CONTAINERS, int(LIMIT_FILE.read_text().strip())))
    except (OSError, ValueError):
        return C.MAX_CONTAINERS


@contextlib.contextmanager
def slot(poll_s: float = 0.2):
    """Hold one container slot (a flock on .slots/slot<i>.lock, i < the current limit)."""
    SLOTS_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        for i in range(_limit()):
            f = open(SLOTS_DIR / f"slot{i}.lock", "a+")  # noqa: SIM115 - held until release
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.close()
                continue
            try:
                yield i
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()
            return
        time.sleep(poll_s)


# --- docker ------------------------------------------------------------------------------------
class DockerError(RuntimeError):
    pass


def container_name(kind: str) -> str:
    return f"p47-{kind}-{uuid.uuid4().hex[:12]}"


def run_container(name: str, mounts: dict[str, str], inner: list[str], *, user: str = "root",
                  stdin: bytes | None = None, inner_limit_s: int = CMD_INNER_LIMIT_S,
                  client_timeout_s: int = CMD_CLIENT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """`docker run --rm` one container (holding a slot); bytes in, bytes out. On a client timeout
    or interruption the container is force-removed and the removal confirmed."""
    argv = ["docker", "run", "--rm", "--name", name, "--label", C.SANDBOX_LABEL,
            "--network", "none", "--init", "--cpus", "1", "--memory", "2g", "-u", user,
            "-w", X3C.WORKDIR]
    if stdin is not None:
        argv.append("-i")
    for vol, path in mounts.items():
        argv += ["-v", f"{vol}:{path}"]
    argv += [IMAGE, "timeout", "-k", "5", str(inner_limit_s), *inner]
    with slot():
        try:
            return subprocess.run(argv, input=stdin, capture_output=True, check=False,
                                  timeout=client_timeout_s,
                                  stdin=None if stdin is not None else subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            why = remove_container(name)
            raise DockerError(f"container {name} timed out after {client_timeout_s}s"
                              + (f"; {why}" if why else "")) from e
        except BaseException:
            why = remove_container(name)
            if why:
                print(f"[p47.sandbox] {why}", file=sys.stderr, flush=True)
            raise


def _docker(args: list[str], timeout: int = VOLUME_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def create_volumes(tag: str) -> dict[str, str]:
    """Three fresh labelled volumes {volume name: mount path}."""
    base = f"p47-{hashlib.sha256(tag.encode()).hexdigest()[:8]}-{uuid.uuid4().hex[:8]}"
    mounts = {}
    try:
        for k, path in MOUNTS.items():
            name = f"{base}-{k}"
            r = _docker(["volume", "create", "--label", C.SANDBOX_LABEL, name])
            if r.returncode != 0:
                raise DockerError(f"volume create {name}: {r.stderr[-300:]}")
            mounts[name] = path
    except BaseException:
        remove_volumes(mounts)
        raise
    return mounts


def remove_volumes(mounts: dict[str, str]) -> str | None:
    """Remove the volumes; None on success, else a reason naming them. Never raises."""
    if not mounts:
        return None
    try:
        r = _docker(["volume", "rm", "-f", *mounts])
    except (OSError, subprocess.SubprocessError) as e:
        return f"volume rm failed for {sorted(mounts)}: {e!r}"
    return None if r.returncode == 0 else f"volume rm {sorted(mounts)}: {r.stderr[-300:]}"


def cleanup_orphans() -> dict:
    """Remove every P41 container and volume (call only when no P41 job is running)."""
    ps = _docker(["ps", "-aq", "--filter", f"label={C.SANDBOX_LABEL}"]).stdout.split()
    if ps:
        _docker(["rm", "-f", *ps])
    vols = _docker(["volume", "ls", "-q", "--filter", f"label={C.SANDBOX_LABEL}"]).stdout.split()
    if vols:
        _docker(["volume", "rm", "-f", *vols])
    return {"containers": len(ps), "volumes": len(vols)}


def leftovers() -> dict:
    ps = _docker(["ps", "-aq", "--filter", f"label={C.SANDBOX_LABEL}"]).stdout.split()
    vols = _docker(["volume", "ls", "-q", "--filter", f"label={C.SANDBOX_LABEL}"]).stdout.split()
    return {"containers": ps, "volumes": vols}


# --- in-container scripts ------------------------------------------------------------------------
def snapshot_arg() -> str:
    """The argument x3.task.take_snapshot passes to _SNAPSHOT_PY."""
    return json.dumps([list(X3C.SNAPSHOT_ROOTS), list(X3C.TEXT_CAPTURE_NAMES),
                       list(X3C.TEXT_CAPTURE_SUFFIXES), X3C.TEXT_CAPTURE_MAX_BYTES,
                       list(X3C.SHADOW_PACKAGES), X3C.USER_SITE])


# Runs as root: the command as dev (X3: cwd /agent, HOME=/home/dev, 60 s), then kill whatever it
# left running, then X3's snapshot. Prints one JSON line.
_CMD_PY = r"""
import base64, json, os, signal, subprocess, sys
spec = json.loads(sys.stdin.read())
env = dict(os.environ); env["HOME"] = spec["home"]
uid = int(subprocess.run(["id", "-u", spec["user"]], capture_output=True, text=True).stdout)
out = err = b""; rc = None; timed_out = False; spawn_error = None
try:
    p = subprocess.Popen(["bash", "-c", spec["cmd"]], cwd=spec["cwd"], user=spec["user"],
                         group=spec["user"], extra_groups=[], env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = p.communicate(timeout=spec["timeout"])
        rc = p.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        # a detached child (setsid/nohup) can hold the pipes open: kill every dev process,
        # then collect with a bound (bulk run 2026-10-13: one exec hit the container limit)
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    if os.stat("/proc/" + d).st_uid == uid:
                        os.kill(int(d), signal.SIGKILL)
                except OSError:
                    pass
        try:
            out, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        rc = 124
except Exception as e:
    spawn_error = repr(e)
killed = 0
for d in os.listdir("/proc"):
    if d.isdigit():
        try:
            if os.stat("/proc/" + d).st_uid == uid:
                os.kill(int(d), signal.SIGKILL); killed += 1
        except OSError:
            pass
snap = subprocess.run(["python3", "-c", spec["snap_py"], spec["snap_arg"]], capture_output=True)
print(json.dumps({"stdout": base64.b64encode(out).decode(), "stderr": base64.b64encode(err).decode(),
                  "rc": rc, "timed_out": timed_out, "spawn_error": spawn_error, "bg_killed": killed,
                  "snap_rc": snap.returncode, "snap": snap.stdout.decode("utf-8", "replace"),
                  "snap_err": snap.stderr.decode("utf-8", "replace")[-300:]}))
"""

# Runs as root: untar the site state from stdin into /, then X3's snapshot.
_RESTORE_PY = r"""
import json, subprocess, sys
r = subprocess.run(["tar", "-C", "/", "--numeric-owner", "-xpf", "-"], stdin=sys.stdin.buffer,
                   capture_output=True)
snap = subprocess.run(["python3", "-c", sys.argv[1], sys.argv[2]], capture_output=True)
print(json.dumps({"tar_rc": r.returncode, "tar_err": r.stderr.decode("utf-8", "replace")[-300:],
                  "snap_rc": snap.returncode, "snap": snap.stdout.decode("utf-8", "replace"),
                  "snap_err": snap.stderr.decode("utf-8", "replace")[-300:]}))
"""

# Runs as root: write the X3 workspace files (JSON on stdin) and chown /agent to dev.
_WRITE_PY = r"""
import json, os, subprocess, sys
spec = json.load(sys.stdin)
for path, body in spec["files"].items():
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
subprocess.run(["chown", "-R", spec["owner"], spec["workdir"]], check=True)
"""

_READ_PY = r"""
import base64, json, sys
out = {}
for p in json.loads(sys.argv[1]):
    try:
        out[p] = base64.b64encode(open(p, "rb").read()).decode()
    except OSError:
        out[p] = None
print(json.dumps(out))
"""


def _snap_from(text: str, rc: int, err: str, step: int) -> dict:
    """A snapshot record shaped like x3.task.take_snapshot's."""
    if rc != 0:
        return {"step": step, "files": None, "autoload": None, "error": f"rc={rc} {err}"}
    try:
        return {"step": step, **json.loads(text), "error": None}
    except json.JSONDecodeError as e:
        return {"step": step, "files": None, "autoload": None, "error": repr(e)}


# --- episode operations ----------------------------------------------------------------------------
def write_workspace(mounts: dict[str, str], files: dict[str, str]) -> None:
    """X3's setup: workspace files written as root, /agent chowned to dev."""
    spec = {"files": files, "owner": f"{X3C.AGENT_USER}:{X3C.AGENT_USER}",
            "workdir": X3C.WORKDIR}
    r = run_container(container_name("write"), mounts, ["python3", "-c", _WRITE_PY],
                      stdin=json.dumps(spec).encode(), inner_limit_s=UTIL_INNER_LIMIT_S,
                      client_timeout_s=UTIL_CLIENT_TIMEOUT_S)
    if r.returncode != 0:
        raise DockerError(f"write workspace: {r.stderr.decode('utf-8', 'replace')[-300:]}")


def exec_command(mounts: dict[str, str], cmd: str, step: int) -> dict:
    """Run one agent command as X3 does; returns {stdout, stderr, code, bg_killed, snapshot}
    with stdout/stderr as X3's loop would see them (Inspect's timeout and UTF-8 messages)."""
    spec = {"cmd": cmd, "cwd": X3C.WORKDIR, "user": X3C.AGENT_USER, "home": X3C.HOME,
            "timeout": X3C.COMMAND_TIMEOUT_S, "snap_py": _SNAPSHOT_PY, "snap_arg": snapshot_arg()}
    r = run_container(container_name("cmd"), mounts, ["python3", "-c", _CMD_PY],
                      stdin=json.dumps(spec).encode())
    try:
        res = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as e:
        raise DockerError(f"exec: rc={r.returncode} {r.stderr.decode('utf-8', 'replace')[-300:]}"
                          ) from e
    snap = _snap_from(res["snap"], res["snap_rc"], res["snap_err"], step)
    if res["spawn_error"]:
        raise DockerError(f"exec spawn: {res['spawn_error']}")
    if res["timed_out"]:
        # x3.task._run_command on Inspect's TimeoutError
        stdout, stderr, code = "", f"command timed out after {X3C.COMMAND_TIMEOUT_S}s", 124
    else:
        try:
            stdout = base64.b64decode(res["stdout"]).decode("utf-8")
            stderr = base64.b64decode(res["stderr"]).decode("utf-8")
            code = res["rc"]
        except UnicodeDecodeError:
            stdout, stderr, code = "", "command output was not valid UTF-8", 1
    return {"stdout": stdout, "stderr": stderr, "code": code, "bg_killed": res["bg_killed"],
            "snapshot": snap}


def restore(mounts: dict[str, str], tar_bytes: bytes, step: int = 0) -> dict:
    """Untar a site state into the volumes; returns snapshot `step` taken right after."""
    r = run_container(container_name("restore"), mounts,
                      ["python3", "-c", _RESTORE_PY, _SNAPSHOT_PY, snapshot_arg()],
                      stdin=tar_bytes, inner_limit_s=UTIL_INNER_LIMIT_S,
                      client_timeout_s=UTIL_CLIENT_TIMEOUT_S)
    try:
        res = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as e:
        raise DockerError(f"restore: rc={r.returncode} "
                          f"{r.stderr.decode('utf-8', 'replace')[-300:]}") from e
    if res["tar_rc"] != 0:
        raise DockerError(f"restore tar: {res['tar_err']}")
    return _snap_from(res["snap"], res["snap_rc"], res["snap_err"], step)


def export_tar(mounts: dict[str, str]) -> bytes:
    """The volumes' state as one tar (paths agent/, home/dev/, tmp/), numeric owners kept."""
    r = run_container(container_name("export"), mounts,
                      ["tar", "-C", "/", "--numeric-owner", "-cf", "-", "agent", "home/dev",
                       "tmp"], inner_limit_s=UTIL_INNER_LIMIT_S,
                      client_timeout_s=UTIL_CLIENT_TIMEOUT_S)
    if r.returncode != 0:
        raise DockerError(f"export: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    return r.stdout


def read_files(mounts: dict[str, str], paths: list[str]) -> dict[str, str | None]:
    r = run_container(container_name("read"), mounts,
                      ["python3", "-c", _READ_PY, json.dumps(paths)],
                      inner_limit_s=UTIL_INNER_LIMIT_S, client_timeout_s=UTIL_CLIENT_TIMEOUT_S)
    if r.returncode != 0:
        raise DockerError(f"read: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    raw = json.loads(r.stdout.decode().strip().splitlines()[-1])
    return {p: (None if v is None else base64.b64decode(v).decode("utf-8", "replace"))
            for p, v in raw.items()}


# --- comparison ------------------------------------------------------------------------------------
def is_cache(path: str) -> bool:
    return any(part in C.CACHE_PARTS for part in Path(path).parts)


def snapshot_shas(snap: dict) -> dict[str, str | None]:
    """{path: sha256} of a snapshot, caches excluded (P23's rule)."""
    return {p: r.get("sha256") for p, r in (snap.get("files") or {}).items() if not is_cache(p)}


def compare(got: dict[str, str | None], expected: dict[str, str | None]) -> list[str]:
    """Paths whose sha256 differs or that only one side has."""
    return sorted(p for p in set(got) | set(expected)
                  if p not in got or p not in expected or got[p] != expected[p])
