"""P49 tool shim: the history's own OpenHands tools on one long-lived X3 sandbox per chain
(plan.md v5.2, "Setup" > Tools; contracts.py).

One `Sandbox` per chain: `docker run -d --rm` of X3's image (`x3-sandbox:pilot`, offline, 1 CPU,
2 GB, `--init`), labelled `emx.sandbox=p49`, with an in-container lifetime (`timeout -k 5 5400
sleep infinity`) so a container outlives no crashed client by more than 90 minutes. Setup is
X3's (`x3.task.setup`): the workspace files from `x3.tasks.build_workspace(task)` are written
into /agent as root and /agent is chowned to `dev`; then the pristine snapshot (step 0). X3 also
wrote RUN_INFO.txt; P49 does not (its task text is the transition message, contracts.py).

Tools (name -> observation string, exactly what the model sees):
  execute_bash        as `dev`, HOME=/home/dev (X3's user and HOME). The cwd persists across
                      calls as in OpenHands' bash: the command runs in the stored cwd inside a
                      fresh `bash`, whose EXIT trap prints a per-call sentinel, `$PWD` and the
                      python path; the shim strips that block and stores the new cwd (start
                      /agent). NOT persisted (each call is a fresh shell): exported env vars,
                      shell options, aliases, an activated venv. Background jobs started with
                      `&` keep running, as they do in OpenHands' tmux session.
                      Hard timeout 60 s (`timeout` argument honoured up to 120 s), enforced
                      in the container by `timeout -k 2 <s>`; the killed command is reported
                      with OpenHands' timeout line, reworded because our process is gone
                      (OpenHands keeps it running for `is_input`): see TIMEOUT_NOTE.
                      Stdout and stderr are merged (OpenHands reads one tmux pane).
  str_replace_editor  one deterministic script (_EDITOR_PY) run as `dev` in the container,
                      reproducing the OpenHands file editor (openhands-aci) texts seen in the
                      SWE-rebench trajectories; undo history under /tmp/.p49_undo (outside the
                      snapshot roots), at most 5 entries per file.
  think               "Your thought has been logged."
  task_tracker        host-side list; texts as in the trajectories.
  finish              ends the chain (X3's `done`); its observation is "".
  anything else       an OpenHands-style error observation; the chain continues.

Every observation is middle-truncated at 30,000 characters as OpenHands v0's `truncate_content`
(max_message_chars). After every execute_bash call and every editor create / str_replace /
insert / undo_edit call (successful or not), an X3 snapshot is taken as root with X3's own script
(`x3.task._SNAPSHOT_PY`, X3's arguments) plus the text of /agent/solution.py ("solution_py"), so
the detector can be re-run at any turn. Every call is recorded X3-style (`Sandbox.calls`).

Containers never outlive their job: `close()` (also on exception and cancellation) force-removes
the container and confirms it is gone, raising `SandboxLeak` with its name if not; an atexit hook
removes any container this process still holds; `sweep()` (sync; `asweep()` from an event loop) removes
every container carrying the label (call it only when no other P49 run is live: it does not know
whose containers they are). A failed startup cleanup raises SandboxLeak from the setup error.
Every docker call is an asyncio subprocess in its own session with a timeout; on timeout or
cancellation its process group is killed.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self

from p49 import contracts as C
from x3 import contracts as X3C
from x3.task import _SNAPSHOT_PY
from x3.tasks import build_workspace

AGENT_USER = X3C.AGENT_USER                 # "dev"
WORKDIR = X3C.WORKDIR                       # "/agent"
HOME = X3C.HOME                             # "/home/dev"
SOLUTION_PATH = f"{WORKDIR}/solution.py"
UNDO_DIR = "/tmp/.p49_undo"
UNDO_MAX = 5                                # openhands-aci FileHistoryManager default
START_TIMEOUT_S = 120
DOCKER_TIMEOUT_S = 120                      # setup, snapshot, editor, reads
REMOVE_TIMEOUT_S = 60
HOST_MARGIN_S = 30                          # host-side guard beyond the in-container timeout
KILL_AFTER_S = 2
OUT_CAP_BYTES = 256 * 1024                  # bash output kept: head and tail, each this size

TRUNC_NOTICE = "\n[... Observation truncated due to length ...]\n"
TIMEOUT_NOTE = ("[The command timed out after {t:.1f} seconds and was terminated. Use the timeout "
                "parameter in execute_bash (up to {mx} seconds) for longer commands, or run them "
                "in the background and redirect the output to a file.]")
NO_RUNNING = "ERROR: No previous running command to interact with."
NO_LOGS = "ERROR: No previous running command to retrieve logs from."
THINK_OBS = "Your thought has been logged."
MUTATING_EDITOR = ("create", "str_replace", "insert", "undo_edit")
TOOL_NAMES = ("execute_bash", "str_replace_editor", "think", "task_tracker", "finish")


class SandboxError(RuntimeError):
    """A docker failure (not an agent error): the chain cannot continue."""


class SandboxLeak(SandboxError):
    """A container that could not be confirmed removed. `record` may carry the chain record."""

    def __init__(self, msg: str, names: tuple[str, ...] = ()):
        super().__init__(msg)
        self.names = names
        self.record = None


def truncate_content(content: str, max_chars: int = C.OBS_MAX_CHARS) -> str:
    """OpenHands v0 `truncate_content`: keep the first and last max_chars // 2 characters."""
    if not max_chars or max_chars < 0 or len(content) <= max_chars:
        return content
    half = max_chars // 2
    return content[:half] + TRUNC_NOTICE + content[-half:]


def _clock() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def snapshot_arg() -> str:
    """The argument x3.task.take_snapshot passes to _SNAPSHOT_PY (same order)."""
    return json.dumps([list(X3C.SNAPSHOT_ROOTS), list(X3C.TEXT_CAPTURE_NAMES),
                       list(X3C.TEXT_CAPTURE_SUFFIXES), X3C.TEXT_CAPTURE_MAX_BYTES,
                       list(X3C.SHADOW_PACKAGES), X3C.USER_SITE])


# --- subprocesses --------------------------------------------------------------------------
@dataclass
class ProcResult:
    rc: int | None
    out: bytes
    err: bytes
    timed_out: bool
    elapsed_s: float
    out_total: int = 0                      # bytes produced on stdout (before any cap)


def _killpg(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _read_capped(stream, cap: int | None) -> tuple[bytes, int]:
    """All of `stream`, or its first and last `cap` bytes when it is longer."""
    head, tail, total = bytearray(), bytearray(), 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if cap is None:
            head += chunk
            continue
        if len(head) < cap:
            take = chunk[: cap - len(head)]
            head += take
            chunk = chunk[len(take):]
        if chunk:
            tail += chunk
            if len(tail) > cap:
                del tail[: len(tail) - cap]
    return bytes(head) + bytes(tail), total


async def run_proc(argv: list[str], *, timeout_s: float, input: bytes | None = None,
                   cap: int | None = None) -> ProcResult:
    """Run argv in its own session. On timeout or cancellation its process group is killed."""
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    out_box: list = [b"", 0]
    err_box: list = [b""]

    async def feed():
        if input is None:
            return
        try:
            proc.stdin.write(input)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    async def read_out():
        out_box[0], out_box[1] = await _read_capped(proc.stdout, cap)

    async def read_err():
        b, _ = await _read_capped(proc.stderr, 64 * 1024)
        err_box[0] = b

    timed_out = False
    try:
        await asyncio.wait_for(asyncio.gather(feed(), read_out(), read_err(), proc.wait()),
                               timeout_s)
    except TimeoutError:
        timed_out = True
        _killpg(proc)
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except TimeoutError:
            pass
    except BaseException:
        _killpg(proc)
        raise
    return ProcResult(proc.returncode if not timed_out else None, out_box[0], err_box[0],
                      timed_out, time.monotonic() - t0, out_box[1])


# --- container registry and cleanup ----------------------------------------------------------
_LIVE: set[str] = set()


def _gone_sync(name: str) -> bool:
    r = subprocess.run(["docker", "inspect", "--format", "{{.Id}}", name], capture_output=True,
                       text=True, timeout=REMOVE_TIMEOUT_S, check=False)
    return r.returncode != 0 and "no such" in (r.stderr + r.stdout).lower()


def remove_sync(name: str) -> str | None:
    """Force-remove and confirm, synchronously. None on success, else a reason naming it."""
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True,
                       timeout=REMOVE_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL)
        for _ in range(20):
            if _gone_sync(name):
                _LIVE.discard(name)
                return None
            time.sleep(0.5)
    except (OSError, subprocess.SubprocessError) as e:
        return f"cleanup of container {name} failed: {type(e).__name__}: {e}"
    return f"cleanup of container {name} failed: still present"


@atexit.register
def _atexit_cleanup() -> None:
    for name in sorted(_LIVE):
        why = remove_sync(name)
        if why:
            print(f"[p49.shim] {why}", file=sys.stderr, flush=True)


def list_ours(label: str = C.SANDBOX_LABEL) -> list[str]:
    """Names of all containers (running or not) carrying `label`."""
    r = subprocess.run(["docker", "ps", "-a", "--filter", f"label={label}", "--format",
                        "{{.Names}}"], capture_output=True, text=True, timeout=60, check=False)
    if r.returncode != 0:
        raise SandboxError(f"docker ps failed: {r.stderr.strip()[-300:]}")
    return sorted(n for n in r.stdout.split() if n)


def sweep(label: str = C.SANDBOX_LABEL) -> list[str]:
    """Remove every container carrying `label`; returns their names. Raises SandboxLeak naming
    any that survive. Only call it when no other run using this label is live."""
    names = list_ours(label)
    if names:
        subprocess.run(["docker", "rm", "-f", *names], capture_output=True, text=True,
                       timeout=REMOVE_TIMEOUT_S, check=False)
    left = [n for n in names if (remove_sync(n) is not None)]
    if left:
        raise SandboxLeak(f"sweep: containers still present: {left}", tuple(left))
    return names


async def asweep(label: str = C.SANDBOX_LABEL) -> list[str]:
    """sweep() in a worker thread, for callers on an event loop."""
    return await asyncio.to_thread(sweep, label)


def container_name(chain_id: str) -> str:
    """p49-<sanitized chain id>-<6 hex>: docker-legal, unique per Sandbox instance."""
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", chain_id).strip("._-")[:180] or "chain"
    return f"{C.SANDBOX_PREFIX}{s}-{uuid.uuid4().hex[:6]}"


# --- in-container scripts --------------------------------------------------------------------
# Root: write the workspace (JSON on stdin) as X3's setup does, chown /agent to dev, and make the
# editor's undo directory (dev-owned, outside the snapshot roots).
_SETUP_PY = r"""
import json, os, subprocess, sys
spec = json.load(sys.stdin)
os.makedirs(spec["workdir"], exist_ok=True)
for name, body in spec["files"].items():
    p = os.path.join(spec["workdir"], name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(p, 0o644)
subprocess.run(["chown", "-R", spec["owner"], spec["workdir"]], check=True)
os.makedirs(spec["undo"], exist_ok=True)
subprocess.run(["chown", spec["owner"], spec["undo"]], check=True)
os.chmod(spec["undo"], 0o700)
print("ok")
"""

# Root: X3's snapshot script (argv[2], verbatim) with X3's argument (argv[1]), plus the text of
# /agent/solution.py.
_SNAP_WRAP_PY = r"""
import contextlib, io, json, sys
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    exec(compile(sys.argv[2], "<x3_snapshot>", "exec"), {"__name__": "__main__"})
d = json.loads(buf.getvalue())
try:
    with open(sys.argv[3], "rb") as f:
        d["solution_py"] = f.read().decode("utf-8", "replace")
except OSError:
    d["solution_py"] = None
print(json.dumps(d))
"""

# bash -c OUTER p49 <cwd> <timeout> <sentinel> <command> <inner>
_BASH_OUTER = (
    'cd -- "$1" 2>/dev/null || cd /agent 2>/dev/null || cd /\n'
    'export P49_SENTINEL="$3"\n'
    f'exec timeout -k {KILL_AFTER_S} "$2" bash -c "$5" bash "$4" 2>&1\n'
)
_BASH_INNER = (
    '__p49_end() { __p49_rc=$?; printf \'\\n%s%s\\n%s\\n\' "$P49_SENTINEL" "$PWD" '
    '"$(command -v python 2>/dev/null || command -v python3 2>/dev/null)"; exit $__p49_rc; }\n'
    'trap __p49_end EXIT\n'
    'eval "$1"\n'
)

# dev: the OpenHands file editor. One JSON object on stdin, one JSON object on stdout:
# {"output": str|None, "error": str|None}.
_EDITOR_PY = r'''
import hashlib, json, os, re, sys
MAX = 16000
SNIP = 4
UNDO_DIR = "/tmp/.p49_undo"
UNDO_MAX = 5
WORKDIR = "/agent"
FILE_NOTICE = ("<response clipped><NOTE>Due to the max output limit, only part of this file has "
               "been shown to you. You should use `view_range` parameter or `grep -n` to view "
               "the specific content you are looking for.</NOTE>")
DIR_NOTICE = ("<response clipped><NOTE>Due to the max output limit, only part of this directory "
              "has been shown to you. You should use `ls -la` instead to view large directories "
              "incrementally.</NOTE>")
COMMANDS = ("view", "create", "str_replace", "insert", "undo_edit")


class ToolError(Exception):
    pass


def invalid(param, value, hint):
    return ToolError(f"Invalid `{param}` parameter: {value}. {hint}")


def missing(param, cmd):
    return ToolError(f"Parameter `{param}` is required for command: {cmd}.")


def make_output(content, desc, start=1):
    if len(content) > MAX:
        content = content[:MAX] + FILE_NOTICE
    lines = content.split("\n")
    return (f"Here's the result of running `cat -n` on {desc}:\n"
            + "\n".join(f"{i + start:6}\t{line}" for i, line in enumerate(lines)) + "\n")


def keepends(content):
    parts = content.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def read(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except Exception as e:
        raise ToolError(f"Ran into {e} while trying to read {path}") from None


def write(path, text):
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except Exception as e:
        raise ToolError(f"Ran into {e} while trying to write to {path}") from None


def _hist_file(path):
    return os.path.join(UNDO_DIR, hashlib.sha256(path.encode()).hexdigest() + ".json")


def add_history(path, text):
    h = _hist_file(path)
    try:
        with open(h, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        items = []
    items = (items + [text])[-UNDO_MAX:]
    os.makedirs(UNDO_DIR, exist_ok=True)
    with open(h, "w", encoding="utf-8") as f:
        json.dump(items, f)


def pop_history(path):
    h = _hist_file(path)
    try:
        with open(h, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return None
    if not items:
        return None
    last = items.pop()
    with open(h, "w", encoding="utf-8") as f:
        json.dump(items, f)
    return last


def validate_path(cmd, path):
    if not path.startswith("/"):
        raise invalid("path", path, "The path should be an absolute path, starting with `/`. "
                      f"Maybe you meant {WORKDIR}/{path}?")
    exists = os.path.exists(path)
    if not exists and cmd != "create":
        raise invalid("path", path, f"The path {path} does not exist. Please provide a valid "
                      "path.")
    if exists and cmd == "create":
        raise invalid("path", path, f"File already exists at: {path}. Cannot overwrite files "
                      "using command `create`.")
    if os.path.isdir(path) and cmd != "view":
        raise invalid("path", path, f"The path {path} is a directory and only the `view` "
                      "command can be used on directories.")


def validate_file(path):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return                                   # read() reports it
    if size > 10 * 1024 * 1024:
        raise ToolError(f"File validation failed for {path}: File is too large "
                        f"({size / 1024 / 1024:.1f}MB). Maximum allowed size is 10MB.")
    binary = b"\0" in head
    if not binary:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as e:
            binary = e.start < len(head) - 4     # a cut multi-byte char at the end is fine
    if binary:
        raise ToolError(f"File validation failed for {path}: File appears to be binary and "
                        "this file type cannot be read or edited by this tool.")


def as_int(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
        return int(v)
    return v


def view_dir(path):
    root = path.rstrip("/") or "/"
    hidden = 0
    errors = []
    entries = [root]
    try:
        top = sorted(os.listdir(root))
    except OSError as e:
        raise ToolError(f"find: ‘{root}’: {e.strerror}\n") from None
    for n in top:
        if n.startswith("."):
            hidden += 1
            continue
        p1 = os.path.join(root, n)
        entries.append(p1)
        if os.path.isdir(p1):
            try:
                subs = os.listdir(p1)
            except OSError as e:
                errors.append(f"find: ‘{p1}’: {e.strerror}")
                continue
            entries.extend(os.path.join(p1, m) for m in subs if not m.startswith("."))
    if errors:
        raise ToolError("\n".join(errors) + "\n")
    entries.sort()
    listing = "\n".join(e + "/" if os.path.isdir(e) else e for e in entries)
    if len(listing) > MAX:
        listing = listing[:MAX] + DIR_NOTICE
    msg = [f"Here's the files and directories up to 2 levels deep in {path}, excluding hidden "
           f"items:\n" + listing]
    if hidden:
        msg.append(f"\n{hidden} hidden files/directories in this directory are excluded. You can "
                   f"use 'ls -la {path}' to see them.")
    return "\n".join(msg)


def view(path, view_range):
    if os.path.isdir(path):
        if view_range:
            raise invalid("view_range", view_range, "The `view_range` parameter is not allowed "
                          "when `path` points to a directory.")
        return view_dir(path)
    validate_file(path)
    content = read(path)
    if not view_range:
        return make_output(content, path)
    if (not isinstance(view_range, list) or len(view_range) != 2
            or not all(isinstance(as_int(i), int) for i in view_range)):
        raise invalid("view_range", view_range, "It should be a list of two integers.")
    start, end = (as_int(i) for i in view_range)
    lines = content.split("\n")
    n = len(lines)
    if start < 1 or start > n:
        raise invalid("view_range", view_range, f"Its first element `{start}` should be within "
                      f"the range of lines of the file: {[1, n]}.")
    if end > n:
        raise invalid("view_range", view_range, f"Its second element `{end}` should be smaller "
                      f"than the number of lines in the file: `{n}`.")
    if end != -1 and end < start:
        raise invalid("view_range", view_range, f"Its second element `{end}` should be greater "
                      f"than or equal to the first element `{start}`.")
    body = "\n".join(lines[start - 1:] if end == -1 else lines[start - 1:end])
    return make_output(body, path, start)


def create(path, file_text):
    if file_text is None:
        raise missing("file_text", "create")
    write(path, file_text)
    add_history(path, file_text)
    return f"File created successfully at: {path}"


def str_replace(path, old_str, new_str):
    if old_str is None:
        raise missing("old_str", "str_replace")
    validate_file(path)
    new_str = new_str or ""
    content = read(path)
    occ = [(content.count("\n", 0, m.start()) + 1, m.start())
           for m in re.finditer(re.escape(old_str), content)]
    if not occ:
        raise ToolError(f"No replacement was performed, old_str `{old_str}` did not appear "
                        f"verbatim in {path}.")
    if len(occ) > 1:
        line_numbers = sorted({ln for ln, _ in occ})
        raise ToolError(f"No replacement was performed. Multiple occurrences of old_str "
                        f"`{old_str}` in lines {line_numbers}. Please ensure it is unique.")
    if new_str == old_str:
        raise invalid("new_str", new_str, "No replacement was performed. `new_str` and "
                      "`old_str` must be different.")
    line, idx = occ[0]
    new_content = content[:idx] + new_str + content[idx + len(old_str):]
    write(path, new_content)
    add_history(path, content)
    start = max(0, line - SNIP)
    end = line + SNIP + new_str.count("\n")
    snippet = "".join(keepends(new_content)[start:end])
    return (f"The file {path} has been edited. "
            + make_output(snippet, f"a snippet of {path}", start + 1)
            + "Review the changes and make sure they are as expected. Edit the file again if "
              "necessary.")


def insert(path, insert_line, new_str):
    if insert_line is None:
        raise missing("insert_line", "insert")
    if new_str is None:
        raise missing("new_str", "insert")
    validate_file(path)
    content = read(path)
    old_lines = keepends(content)
    n = len(old_lines)
    insert_line = as_int(insert_line)
    if not isinstance(insert_line, int) or insert_line < 0 or insert_line > n:
        raise invalid("insert_line", insert_line, f"It should be within the range of allowed "
                      f"values: {[0, n]}")
    new_lines = new_str.split("\n")
    new_content = ("".join(old_lines[:insert_line]) + "".join(x + "\n" for x in new_lines)
                   + "".join(old_lines[insert_line:]))
    write(path, new_content)
    add_history(path, content)
    start = max(0, insert_line - SNIP)
    end = min(n + len(new_lines), insert_line + SNIP + len(new_lines))
    snippet = "".join(keepends(new_content)[start:end])
    return (f"The file {path} has been edited. "
            + make_output(snippet, "a snippet of the edited file",
                          max(1, insert_line - SNIP + 1))
            + "Review the changes and make sure they are as expected (correct indentation, no "
              "duplicate lines, etc). Edit the file again if necessary.")


def undo_edit(path):
    old = pop_history(path)
    if old is None:
        raise ToolError(f"No edit history found for {path}.")
    write(path, old)
    return f"Last edit to {path} undone successfully. {make_output(old, path)}"


def main(a):
    cmd = a.get("command")
    path = a.get("path")
    if path is None:
        raise missing("path", cmd)
    path = str(path)
    if path.startswith("/"):                     # as pathlib.Path(path) normalises it
        path = "/" + "/".join(p for p in path.split("/") if p not in ("", "."))
    validate_path(cmd, path)
    if cmd == "view":
        return view(path, a.get("view_range"))
    if cmd == "create":
        return create(path, a.get("file_text"))
    if cmd == "str_replace":
        return str_replace(path, a.get("old_str"), a.get("new_str"))
    if cmd == "insert":
        return insert(path, a.get("insert_line"), a.get("new_str"))
    if cmd == "undo_edit":
        return undo_edit(path)
    raise ToolError(f"Unrecognized command {cmd}. The allowed commands for the oh_editor tool "
                    f"are: {', '.join(COMMANDS)}")


args = json.loads(sys.stdin.read())
try:
    res = {"output": main(args), "error": None}
except ToolError as e:
    res = {"output": None, "error": str(e)}
sys.stdout.write(json.dumps(res))
'''


# --- the task tracker (host side) -----------------------------------------------------------
_TT_ICON = {"todo": "⏳", "in_progress": "🔄", "done": "✅"}


def format_task_list(tasks: list[dict]) -> str:
    """The trajectories' `view` text: '# Task List\\n\\n' + 'i. <icon> title\\nnotes\\n' + '\\n'."""
    out = "# Task List\n\n"
    for i, t in enumerate(tasks, 1):
        icon = _TT_ICON.get(str(t.get("status", "todo")), "⏳")
        out += f"{i}. {icon} {t.get('title', 'Untitled task')}\n{t.get('notes', '') or ''}\n"
    return out + "\n"


# --- results ---------------------------------------------------------------------------------
@dataclass
class ToolResult:
    observation: str
    done: bool = False
    record: dict = field(default_factory=dict)


def _parse_args(arguments) -> tuple[dict | None, str | None]:
    if isinstance(arguments, dict):
        return arguments, None
    if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
        return {}, None
    try:
        a = json.loads(arguments)
    except (TypeError, ValueError):
        return None, f"Failed to parse tool call arguments: {arguments}"
    if not isinstance(a, dict):
        return None, f"Failed to parse tool call arguments: {arguments}"
    return a, None


def _truthy(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


# --- the sandbox -----------------------------------------------------------------------------
class Sandbox:
    """One long-lived X3 container for one chain. Use as `async with Sandbox(id, task) as sb`."""

    def __init__(self, chain_id: str, task: X3C.TaskSpec, *, image: str = C.IMAGE,
                 label: str = C.SANDBOX_LABEL):
        self.chain_id = chain_id
        self.task = task
        self.image = image
        self.label = label
        self.name = container_name(chain_id)
        self.cwd = WORKDIR
        self.snapshots: list[dict] = []
        self.calls: list[dict] = []
        self.task_list: list[dict] = []
        self.finished = False
        self.finish_message: str | None = None
        self._started = False
        self._closed = False
        self._cleanup: asyncio.Future | None = None

    # -- lifecycle --
    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        try:
            await self.close()
        except SandboxLeak as e:
            if exc is None:
                raise
            print(f"[p49.shim] {e}", file=sys.stderr, flush=True)
            exc.add_note(str(e))
        return False

    async def start(self) -> None:
        argv = ["docker", "run", "-d", "--rm", "--name", self.name, "--label", self.label,
                "--network", "none", "--init", "--cpus", "1", "--memory", "2g",
                "-w", WORKDIR, self.image,
                "timeout", "-k", "5", str(C.CONTAINER_LIFETIME_S), "sleep", "infinity"]
        _LIVE.add(self.name)
        self._started = True
        try:
            r = await run_proc(argv, timeout_s=START_TIMEOUT_S)
            if r.timed_out or r.rc != 0:
                raise SandboxError(f"docker run {self.name}: rc={r.rc} "
                                   f"{r.err.decode('utf-8', 'replace').strip()[-300:]}")
            spec = {"files": build_workspace(self.task), "workdir": WORKDIR,
                    "owner": f"{AGENT_USER}:{AGENT_USER}", "undo": UNDO_DIR}
            r = await self.exec(["python3", "-c", _SETUP_PY], user="root",
                                input=json.dumps(spec).encode())
            if r.rc != 0 or b"ok" not in r.out:
                raise SandboxError(f"workspace setup in {self.name}: rc={r.rc} "
                                   f"{r.err.decode('utf-8', 'replace').strip()[-300:]}")
            snap = await self.snapshot(turn=0)
            if snap.get("files") is None:
                raise SandboxError(f"pristine snapshot in {self.name}: {snap.get('error')}")
        except BaseException as e:
            try:
                await self.close()
            except SandboxLeak as leak:           # the scheduler must stop on an orphan
                raise leak from e
            raise

    async def close(self) -> None:
        """Force-remove the container and confirm it is gone (idempotent). The removal runs in
        ONE worker thread (never on the event loop); a second close(), or a cancellation while
        waiting, awaits that same removal instead of starting another. A cancellation is
        re-raised once the removal has finished."""
        if not self._started:
            self._closed = True
            return
        self._closed = True
        if self._cleanup is None:
            self._cleanup = asyncio.ensure_future(asyncio.to_thread(remove_sync, self.name))
        cancelled = False
        while True:
            try:
                why = await asyncio.shield(self._cleanup)
                break
            except asyncio.CancelledError:
                if self._cleanup.cancelled():
                    raise
                cancelled = True                  # keep waiting for the same removal
        if why:
            if cancelled:
                print(f"[p49.shim] {why}", file=sys.stderr, flush=True)
            else:
                raise SandboxLeak(why, (self.name,))
        if cancelled:
            raise asyncio.CancelledError

    async def close_quietly(self) -> None:
        try:
            await self.close()
        except SandboxLeak as e:
            print(f"[p49.shim] {e}", file=sys.stderr, flush=True)

    # -- docker exec --
    async def exec(self, argv: list[str], *, user: str, input: bytes | None = None,
                   timeout_s: float = DOCKER_TIMEOUT_S, cap: int | None = None,
                   env: dict[str, str] | None = None) -> ProcResult:
        cmd = ["docker", "exec"]
        if input is not None:
            cmd.append("-i")
        cmd += ["-u", user, "-w", "/"]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [self.name, *argv]
        return await run_proc(cmd, timeout_s=timeout_s, input=input, cap=cap)

    async def root_exec(self, argv: list[str], input: bytes | None = None,
                        timeout_s: float = DOCKER_TIMEOUT_S) -> ProcResult:
        return await self.exec(argv, user="root", input=input, timeout_s=timeout_s)

    async def snapshot(self, turn: int) -> dict:
        """X3's snapshot (as root) + /agent/solution.py text; appended to self.snapshots."""
        step = len(self.snapshots)
        r = await self.exec(["python3", "-c", _SNAP_WRAP_PY, snapshot_arg(), _SNAPSHOT_PY,
                             SOLUTION_PATH], user="root")
        snap: dict
        if r.timed_out or r.rc != 0:
            snap = {"files": None, "autoload": None, "solution_py": None,
                    "error": f"rc={r.rc} {r.err.decode('utf-8', 'replace')[-300:]}"}
        else:
            try:
                snap = {**json.loads(r.out.decode("utf-8", "replace")), "error": None}
            except json.JSONDecodeError as e:
                snap = {"files": None, "autoload": None, "solution_py": None, "error": repr(e)}
        snap = {"step": step, "turn": turn, **snap}
        self.snapshots.append(snap)
        return snap

    async def final_solution(self) -> str | None:
        r = await self.exec(["cat", SOLUTION_PATH], user="root")
        if r.timed_out or r.rc != 0:
            return None
        return r.out.decode("utf-8", "replace")

    # -- tools --
    async def call(self, name: str, arguments, *, turn: int, index: int = 0,
                   tool_call_id: str | None = None) -> ToolResult:
        """Execute one tool call; returns the observation the model sees (already truncated)."""
        t_start, m0 = _clock(), time.monotonic()
        rec = {"turn": turn, "index": index, "tool_call_id": tool_call_id, "name": name,
               "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
               "observation": None, "exit": None, "timed_out": False, "truncated": False,
               "t_start": t_start, "t_end": None, "ms": None, "snapshot_step": None,
               "cwd": None, "error": None}
        done = False
        mutating = False
        args, perr = _parse_args(arguments)
        if name not in TOOL_NAMES:
            obs = (f"Tool {name} is not registered. (arguments: {arguments}). Please check the "
                   "tool name and retry with an existing tool.")
        elif perr is not None:
            obs = perr
        elif name == "execute_bash":
            mutating = True
            obs, code, timed_out = await self._bash(args)
            rec["exit"], rec["timed_out"] = code, timed_out
        elif name == "str_replace_editor":
            mutating = args.get("command") in MUTATING_EDITOR
            obs = await self._editor(args)
        elif name == "think":
            obs = THINK_OBS
        elif name == "task_tracker":
            obs = self._task_tracker(args)
        else:                                       # finish
            done = True
            self.finished = True
            self.finish_message = args.get("message")
            obs = ""
        shown = truncate_content(obs)
        rec["truncated"] = shown != obs
        if mutating:
            snap = await self.snapshot(turn)
            rec["snapshot_step"] = snap["step"]
        rec.update(observation=shown, t_end=_clock(), cwd=self.cwd,
                   ms=round((time.monotonic() - m0) * 1000, 1))
        self.calls.append(rec)
        return ToolResult(shown, done, rec)

    async def _bash(self, a: dict) -> tuple[str, int | None, bool]:
        command = a.get("command")
        if command is None:
            return 'Missing required argument "command" in tool call execute_bash', None, False
        command = str(command)
        if _truthy(a.get("is_input")):
            return NO_RUNNING, None, False
        if not command.strip():
            return NO_LOGS, None, False
        t = float(C.BASH_TIMEOUT_S)
        if a.get("timeout") not in (None, ""):
            try:
                t = float(a["timeout"])
            except (TypeError, ValueError):
                t = float(C.BASH_TIMEOUT_S)
            t = min(max(t, 1.0), float(C.BASH_TIMEOUT_MAX_S))
        sentinel = f"__P49_END_{uuid.uuid4().hex}__"
        argv = ["bash", "-c", _BASH_OUTER, "p49", self.cwd, f"{t:g}", sentinel, command,
                _BASH_INNER]
        r = await self.exec(argv, user=AGENT_USER, env={"HOME": HOME},
                            timeout_s=t + KILL_AFTER_S + HOST_MARGIN_S, cap=OUT_CAP_BYTES)
        text = r.out.decode("utf-8", "replace")
        marker = "\n" + sentinel
        idx = text.rfind(marker)
        found = idx >= 0
        tail = ""
        if found:
            tail = text[idx + len(marker):]
            text = text[:idx]
        if not found and not r.timed_out and r.rc not in (124, 137) and r.err.strip():
            raise SandboxError(f"docker exec in {self.name}: rc={r.rc} "
                               f"{r.err.decode('utf-8', 'replace').strip()[-300:]}")
        timed_out = r.timed_out or (r.rc in (124, 137) and
                                    (not found or r.elapsed_s >= t - 0.5))
        text = text.removesuffix("\n")
        if timed_out:
            note = TIMEOUT_NOTE.format(t=t, mx=C.BASH_TIMEOUT_MAX_S)
            return (text + "\n" + note if text else note), -1, True
        py = ""
        if found:
            lines = tail.split("\n")
            if lines and lines[0]:
                self.cwd = lines[0]
            py = lines[1] if len(lines) > 1 else ""
        code = r.rc
        obs = (f"{text}\n[The command completed with exit code {code}.]\n"
               f"[Current working directory: {self.cwd}]\n"
               f"[Python interpreter: {py}]\n"
               f"[Command finished with exit code {code}]")
        return obs, code, False

    async def _editor(self, a: dict) -> str:
        r = await self.exec(["python3", "-c", _EDITOR_PY], user=AGENT_USER,
                            env={"HOME": HOME}, input=json.dumps(a).encode())
        try:
            res = json.loads(r.out.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            raise SandboxError(f"editor in {self.name}: rc={r.rc} "
                               f"{r.err.decode('utf-8', 'replace').strip()[-500:]}") from e
        if res.get("error") is not None:
            return "ERROR:\n" + res["error"]
        return res.get("output") or ""

    def _task_tracker(self, a: dict) -> str:
        cmd = a.get("command")
        if cmd == "view":
            if not self.task_list:
                return 'No task list found. Use the "plan" command to create one.'
            return format_task_list(self.task_list)
        if cmd == "plan":
            tl = a.get("task_list")
            if not isinstance(tl, list):
                return "ERROR:\nParameter `task_list` is required for command: plan."
            self.task_list = [t if isinstance(t, dict) else {"title": str(t)} for t in tl]
            return f"Task list has been updated with {len(self.task_list)} items."
        return (f"ERROR:\nUnrecognized command {cmd}. The allowed commands for the task_tracker "
                "tool are: view, plan")
