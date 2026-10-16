"""Tests for p49.shim (plan "Automatic rules": tool shim). Docker tests skip without docker or
the X3 image; every container they start is checked gone afterwards."""
from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import uuid

import pytest
from p49 import contracts as C
from p49 import shim

from x3 import contracts as X3C
from x3 import detector, passcheck
from x3.tasks import load_manifest


def _docker_ok() -> bool:
    for _ in range(3):
        try:
            r = subprocess.run(["docker", "image", "inspect", C.IMAGE], capture_output=True,
                               timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        if r.returncode == 0:
            return True
    return False


DOCKER = _docker_ok()
needs_docker = pytest.mark.skipif(not DOCKER, reason="docker or x3-sandbox:pilot unavailable")
TASKS = load_manifest().tasks


def _ps_names(name: str) -> list[str]:
    r = subprocess.run(["docker", "ps", "-a", "--filter", f"name={name}", "--format",
                        "{{.Names}}"], capture_output=True, text=True, timeout=60, check=True)
    return r.stdout.split()


def run(coro):
    return asyncio.run(coro)


# --- reference implementation of the OpenHands editor (pure Python, in memory) ---------------
class RefEditor:
    """What the model should see, from the trajectories' observed formats (module docstring of
    p49.shim). Files are an in-memory dict; history at most 5 per file."""
    SNIP = 4
    MAX = 16000

    def __init__(self, files: dict[str, str]):
        self.files = dict(files)
        self.hist: dict[str, list[str]] = {}

    def _num(self, content: str, desc: str, start: int = 1) -> str:
        if len(content) > self.MAX:
            content = content[:self.MAX] + ("<response clipped><NOTE>Due to the max output "
                                            "limit, only part of this file has been shown to "
                                            "you. You should use `view_range` parameter or "
                                            "`grep -n` to view the specific content you are "
                                            "looking for.</NOTE>")
        rows = [f"{n:6}\t{s}" for n, s in enumerate(content.split("\n"), start)]
        return f"Here's the result of running `cat -n` on {desc}:\n" + "\n".join(rows) + "\n"

    @staticmethod
    def _lines_with_ends(s: str) -> list[str]:
        out, cur = [], ""
        for ch in s:
            cur += ch
            if ch == "\n":
                out.append(cur)
                cur = ""
        if cur:
            out.append(cur)
        return out

    def _push(self, path: str, text: str) -> None:
        self.hist.setdefault(path, []).append(text)
        self.hist[path] = self.hist[path][-5:]

    def __call__(self, a: dict) -> str:
        cmd, path = a.get("command"), a["path"]
        err = "ERROR:\n"
        if not path.startswith("/"):
            return (err + f"Invalid `path` parameter: {path}. The path should be an absolute "
                    f"path, starting with `/`. Maybe you meant /agent/{path}?")
        exists = path in self.files
        if not exists and cmd != "create":
            return (err + f"Invalid `path` parameter: {path}. The path {path} does not exist. "
                    "Please provide a valid path.")
        if exists and cmd == "create":
            return (err + f"Invalid `path` parameter: {path}. File already exists at: {path}. "
                    "Cannot overwrite files using command `create`.")
        if cmd == "view":
            text = self.files[path]
            vr = a.get("view_range")
            if not vr:
                return self._num(text, path)
            lines = text.split("\n")
            s, e = vr
            if s < 1 or s > len(lines):
                return (err + f"Invalid `view_range` parameter: {vr}. Its first element `{s}` "
                        f"should be within the range of lines of the file: {[1, len(lines)]}.")
            sel = lines[s - 1:] if e == -1 else lines[s - 1:e]
            return self._num("\n".join(sel), path, s)
        if cmd == "create":
            self.files[path] = a["file_text"]
            self._push(path, a["file_text"])
            return f"File created successfully at: {path}"
        if cmd == "str_replace":
            old, new = a.get("old_str"), a.get("new_str") or ""
            if old is None:
                return err + "Parameter `old_str` is required for command: str_replace."
            text = self.files[path]
            n = text.count(old)
            if n == 0:
                return (err + f"No replacement was performed, old_str `{old}` did not appear "
                        f"verbatim in {path}.")
            if n > 1:
                lines, pos = [], 0
                while (i := text.find(old, pos)) >= 0:
                    lines.append(text[:i].count("\n") + 1)
                    pos = i + len(old)
                return (err + f"No replacement was performed. Multiple occurrences of old_str "
                        f"`{old}` in lines {sorted(set(lines))}. Please ensure it is unique.")
            if new == old:
                return (err + f"Invalid `new_str` parameter: {new}. No replacement was "
                        "performed. `new_str` and `old_str` must be different.")
            line = text[:text.find(old)].count("\n") + 1
            self._push(path, text)
            self.files[path] = text.replace(old, new, 1)
            first = max(0, line - self.SNIP)
            last = line + self.SNIP + new.count("\n")
            snip = "".join(self._lines_with_ends(self.files[path])[first:last])
            return (f"The file {path} has been edited. "
                    + self._num(snip, f"a snippet of {path}", first + 1)
                    + "Review the changes and make sure they are as expected. Edit the file "
                      "again if necessary.")
        if cmd == "insert":
            text = self.files[path]
            old_lines = self._lines_with_ends(text)
            k = a["insert_line"]
            if k < 0 or k > len(old_lines):
                return (err + f"Invalid `insert_line` parameter: {k}. It should be within the "
                        f"range of allowed values: {[0, len(old_lines)]}")
            new_lines = a["new_str"].split("\n")
            self._push(path, text)
            self.files[path] = ("".join(old_lines[:k]) + "".join(s + "\n" for s in new_lines)
                                + "".join(old_lines[k:]))
            first = max(0, k - self.SNIP)
            last = min(len(old_lines) + len(new_lines), k + self.SNIP + len(new_lines))
            snip = "".join(self._lines_with_ends(self.files[path])[first:last])
            return (f"The file {path} has been edited. "
                    + self._num(snip, "a snippet of the edited file", max(1, k - self.SNIP + 1))
                    + "Review the changes and make sure they are as expected (correct "
                      "indentation, no duplicate lines, etc). Edit the file again if necessary.")
        if cmd == "undo_edit":
            if not self.hist.get(path):
                return err + f"No edit history found for {path}."
            self.files[path] = self.hist[path].pop()
            return (f"Last edit to {path} undone successfully. "
                    + self._num(self.files[path], path))
        return (err + f"Unrecognized command {cmd}. The allowed commands for the oh_editor tool "
                "are: view, create, str_replace, insert, undo_edit")


# --- unit tests (no docker) ------------------------------------------------------------------
def test_truncate_content_matches_openhands_sample_length():
    s = "x" * 40_000
    t = shim.truncate_content(s)
    assert len(t) == 30_047                       # the sample trajectory's truncated length
    assert t == "x" * 15_000 + "\n[... Observation truncated due to length ...]\n" + "x" * 15_000
    assert shim.truncate_content("short") == "short"


def test_task_list_format_matches_trajectories():
    with_notes = [{"id": "a", "title": "Analyze", "status": "in_progress", "notes": "N1"},
                  {"id": "b", "title": "Fix", "status": "todo", "notes": "N2"}]
    assert shim.format_task_list(with_notes) == "# Task List\n\n1. 🔄 Analyze\nN1\n2. ⏳ Fix\nN2\n\n"
    no_notes = [{"id": "a", "title": "A", "status": "todo"}, {"id": "b", "title": "B",
                                                               "status": "done"}]
    assert shim.format_task_list(no_notes) == "# Task List\n\n1. ⏳ A\n\n2. ✅ B\n\n\n"


def test_container_name_is_sanitised_and_unique():
    a, b = shim.container_name("h1/task:x R A"), shim.container_name("h1/task:x R A")
    assert a != b and a.startswith("p49-h1_task_x_R_A-")


# --- docker tests ----------------------------------------------------------------------------
async def _call(sb, name, args, turn=1, index=0):
    r = await sb.call(name, json.dumps(args) if isinstance(args, dict) else args, turn=turn,
                      index=index)
    return r


async def _cat(sb, path) -> str:
    r = await sb.root_exec(["cat", path])
    assert r.rc == 0, r.err
    return r.out.decode()


@needs_docker
def test_editor_commands_match_reference():
    task = TASKS[0]

    async def go():
        async with shim.Sandbox("t-editor", task) as sb:
            files = {"/agent/solution.py": task.solution_py,
                     "/agent/test_solution.py": task.test_solution_py,
                     "/agent/description.md": task.description_md}
            ref = RefEditor(files)
            sol = "/agent/solution.py"
            seq = [
                {"command": "view", "path": sol},
                {"command": "view", "path": sol, "view_range": [3, 8]},
                {"command": "view", "path": sol, "view_range": [5, -1]},
                {"command": "view", "path": sol, "view_range": [0, 3]},
                {"command": "create", "path": "/agent/new.py", "file_text": "a = 1\nb = 2\n"},
                {"command": "create", "path": "/agent/new.py", "file_text": "again"},
                {"command": "str_replace", "path": "/agent/new.py", "old_str": "b = 2",
                 "new_str": "b = 3\nc = 4"},
                {"command": "str_replace", "path": "/agent/new.py", "old_str": "zzz",
                 "new_str": "q"},
                {"command": "str_replace", "path": "/agent/new.py", "old_str": " = ",
                 "new_str": "="},
                {"command": "str_replace", "path": "/agent/new.py", "old_str": "a = 1",
                 "new_str": "a = 1"},
                {"command": "str_replace", "path": "/agent/new.py", "new_str": "x"},
                {"command": "insert", "path": "/agent/new.py", "insert_line": 1,
                 "new_str": "# inserted"},
                {"command": "insert", "path": "/agent/new.py", "insert_line": 99,
                 "new_str": "nope"},
                {"command": "insert", "path": sol, "insert_line": 6, "new_str": "import math"},
                {"command": "undo_edit", "path": "/agent/new.py"},
                {"command": "undo_edit", "path": "/agent/new.py"},
                {"command": "undo_edit", "path": "/agent/new.py"},   # back to the create text
                {"command": "undo_edit", "path": "/agent/new.py"},   # nothing left
                {"command": "view", "path": "relative.py"},
                {"command": "view", "path": "/agent/missing.py"},
                {"command": "delete", "path": sol},
            ]
            for a in seq:
                got = (await _call(sb, "str_replace_editor", a)).observation
                want = ref(a)
                assert got == want, a
            for p, text in ref.files.items():
                assert await _cat(sb, p) == text, p
            # directory view (2 levels, hidden excluded and counted)
            await sb.root_exec(["bash", "-c", ("mkdir -p /agent/pkg/sub /agent/.hid && touch "
                                "/agent/pkg/m.py /agent/pkg/sub/deep.py /agent/.dot")])
            got = (await _call(sb, "str_replace_editor", {"command": "view",
                                                          "path": "/agent"})).observation
            assert got == ("Here's the files and directories up to 2 levels deep in /agent, "
                           "excluding hidden items:\n/agent/\n/agent/description.md\n"
                           "/agent/new.py\n/agent/pkg/\n/agent/pkg/m.py\n/agent/pkg/sub/\n"
                           "/agent/solution.py\n/agent/test_solution.py\n\n2 hidden "
                           "files/directories in this directory are excluded. You can use "
                           "'ls -la /agent' to see them.")
            got = (await _call(sb, "str_replace_editor", {"command": "view", "path": "/agent",
                                                          "view_range": [1, 2]})).observation
            assert got.startswith("ERROR:\nInvalid `view_range` parameter: [1, 2]. The "
                                  "`view_range` parameter is not allowed")
            got = (await _call(sb, "str_replace_editor", {"command": "str_replace",
                                                          "path": "/agent/pkg", "old_str": "a",
                                                          "new_str": "b"})).observation
            assert got == ("ERROR:\nInvalid `path` parameter: /agent/pkg. The path /agent/pkg "
                           "is a directory and only the `view` command can be used on "
                           "directories.")
            # undo history lives outside the snapshot roots
            assert not any("p49_undo" in p for p in sb.snapshots[-1]["files"])

    run(go())
    assert _ps_names("p49-t-editor") == []


@needs_docker
def test_editor_output_cap_and_binary():
    async def go():
        async with shim.Sandbox("t-cap", TASKS[0]) as sb:
            body = "\n".join(f"line {i} " + "y" * 60 for i in range(600))
            await _call(sb, "str_replace_editor", {"command": "create", "path": "/agent/big.txt",
                                                   "file_text": body})
            got = (await _call(sb, "str_replace_editor", {"command": "view",
                                                          "path": "/agent/big.txt"})).observation
            assert got == RefEditor({"/agent/big.txt": body})({"command": "view",
                                                                "path": "/agent/big.txt"})
            assert "<response clipped><NOTE>Due to the max output limit" in got
            await sb.root_exec(["bash", "-c", "printf 'a\\0b' > /agent/bin.dat"])
            got = (await _call(sb, "str_replace_editor", {"command": "view",
                                                          "path": "/agent/bin.dat"})).observation
            assert got == ("ERROR:\nFile validation failed for /agent/bin.dat: File appears to be"
                           " binary and this file type cannot be read or edited by this tool.")

    run(go())
    assert _ps_names("p49-t-cap") == []


@needs_docker
def test_permission_errors_surface_as_observations():
    async def go():
        async with shim.Sandbox("t-perm", TASKS[0]) as sb:
            await sb.root_exec(["bash", "-c", ("echo hi > /agent/locked.txt && chown root:root "
                                "/agent/locked.txt && chmod 444 /agent/locked.txt")])
            got = (await _call(sb, "str_replace_editor", {
                "command": "str_replace", "path": "/agent/locked.txt", "old_str": "hi",
                "new_str": "ho"})).observation
            assert got == ("ERROR:\nRan into [Errno 13] Permission denied: '/agent/locked.txt' "
                           "while trying to write to /agent/locked.txt")
            assert await _cat(sb, "/agent/locked.txt") == "hi\n"
            got = (await _call(sb, "str_replace_editor", {
                "command": "create", "path": "/etc/p49.txt", "file_text": "x"})).observation
            assert got == ("ERROR:\nRan into [Errno 13] Permission denied: '/etc/p49.txt' while "
                           "trying to write to /etc/p49.txt")
            r = await _call(sb, "execute_bash", {"command": "echo x > /agent/locked.txt"})
            assert "Permission denied" in r.observation and r.record["exit"] == 1
            assert "[The command completed with exit code 1.]" in r.observation
            r = await _call(sb, "execute_bash", {"command": "whoami; echo $HOME"})
            assert r.observation.startswith("dev\n/home/dev\n[The command completed with exit "
                                            "code 0.]")

    run(go())
    assert _ps_names("p49-t-perm") == []


@needs_docker
def test_bash_cwd_exit_codes_timeout_truncation_and_errors():
    async def go():
        async with shim.Sandbox("t-bash", TASKS[0]) as sb:
            r = await _call(sb, "execute_bash", {"command": "pwd"})
            assert r.observation == ("/agent\n[The command completed with exit code 0.]\n"
                                     "[Current working directory: /agent]\n"
                                     "[Python interpreter: /usr/local/bin/python]\n"
                                     "[Command finished with exit code 0]")
            await _call(sb, "execute_bash", {"command": "cd /tmp"})
            r = await _call(sb, "execute_bash", {"command": "pwd"})
            assert r.observation.startswith("/tmp\n[The command completed with exit code 0.]\n"
                                            "[Current working directory: /tmp]")
            assert sb.cwd == "/tmp"
            r = await _call(sb, "execute_bash", {"command": "grep nothing /dev/null"})
            assert r.observation.startswith("\n[The command completed with exit code 1.]")
            assert r.record["exit"] == 1
            r = await _call(sb, "execute_bash", {"command": "echo out; echo err >&2; exit 7"})
            assert r.observation.startswith("out\nerr\n[The command completed with exit code 7.]")
            r = await _call(sb, "execute_bash", {"command": "nosuchcmd"})
            assert r.record["exit"] == 127 and "command not found" in r.observation
            # a user's own `timeout` that fires is a normal exit 124, not ours
            r = await _call(sb, "execute_bash", {"command": "timeout 0.2 sleep 5"})
            assert r.record["exit"] == 124 and not r.record["timed_out"]
            r = await _call(sb, "execute_bash", {"command": "echo before; sleep 30",
                                                 "timeout": 1})
            assert r.record["timed_out"] and r.record["exit"] == -1
            assert r.observation == ("before\n" + shim.TIMEOUT_NOTE.format(t=1.0, mx=120))
            assert r.record["ms"] < 10_000
            r = await _call(sb, "execute_bash", {"command": "python3 -c \"print('a' * 40000)\""})
            assert len(r.observation) == 30_047 and r.record["truncated"]
            assert r.observation.startswith("a" * 15_000 + "\n[... Observation truncated")
            assert r.observation.endswith("[Command finished with exit code 0]")
            r = await _call(sb, "execute_bash", {"command": "head -c 5000000 /dev/zero | tr "
                                                 "'\\0' b"})
            assert len(r.observation) == 30_047
            assert r.observation.endswith("[Command finished with exit code 0]")
            assert r.observation.startswith("b" * 15_000)
            r = await _call(sb, "execute_bash", {"command": "C-c", "is_input": "true"})
            assert r.observation == shim.NO_RUNNING
            r = await _call(sb, "execute_bash", {"command": ""})
            assert r.observation == shim.NO_LOGS
            r = await _call(sb, "execute_bash", {})
            assert r.observation == 'Missing required argument "command" in tool call execute_bash'
            r = await _call(sb, "execute_bash", "{not json")
            assert r.observation == "Failed to parse tool call arguments: {not json"
            assert r.record["snapshot_step"] is None
            r = await _call(sb, "run_ipython", {"code": "1"})
            assert r.observation.startswith("Tool run_ipython is not registered.")
            r = await _call(sb, "think", {"thought": "hmm"})
            assert r.observation == "Your thought has been logged." and not r.done
            r = await _call(sb, "task_tracker", {"command": "view"})
            assert r.observation.startswith("No task list found.")
            r = await _call(sb, "task_tracker", {"command": "plan", "task_list": [
                {"id": "1", "title": "T", "status": "todo"}]})
            assert r.observation == "Task list has been updated with 1 items."
            r = await _call(sb, "finish", {"message": "bye"})
            assert r.done and r.observation == "" and sb.finish_message == "bye"
            # a background job persists across calls, as in OpenHands' session
            await _call(sb, "execute_bash", {"command": "cd /agent && (sleep 20 > /dev/null 2>&1 &)"})
            r = await _call(sb, "execute_bash", {"command": (
                "for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo; done "
                "| grep -c '^sleep 20'")})
            assert r.observation.startswith("1\n")

    run(go())
    assert _ps_names("p49-t-bash") == []


@needs_docker
def test_snapshots_after_every_mutating_call_in_a_turn():
    async def go():
        async with shim.Sandbox("t-snap", TASKS[0]) as sb:
            assert [s["step"] for s in sb.snapshots] == [0]
            assert sb.snapshots[0]["turn"] == 0
            assert sb.snapshots[0]["solution_py"] == TASKS[0].solution_py
            assert "/agent/test_solution.py" in sb.snapshots[0]["files"]
            turn = [("str_replace_editor", {"command": "view", "path": "/agent/solution.py"}),
                    ("execute_bash", {"command": "echo '# a' >> /agent/solution.py"}),
                    ("think", {"thought": "x"}),
                    ("str_replace_editor", {"command": "create", "path": "/agent/n.txt",
                                            "file_text": "n"}),
                    ("task_tracker", {"command": "view"}),
                    ("str_replace_editor", {"command": "str_replace", "path": "/agent/n.txt",
                                            "old_str": "n", "new_str": "m"}),
                    ("str_replace_editor", {"command": "undo_edit", "path": "/agent/n.txt"})]
            steps = []
            for i, (name, a) in enumerate(turn):
                steps.append((await _call(sb, name, a, turn=3, index=i)).record["snapshot_step"])
            assert steps == [None, 1, None, 2, None, 3, 4]
            assert [s["step"] for s in sb.snapshots] == [0, 1, 2, 3, 4]
            assert all(s["turn"] == 3 for s in sb.snapshots[1:])
            assert sb.snapshots[1]["solution_py"] == TASKS[0].solution_py + "# a\n"
            assert "/agent/n.txt" in sb.snapshots[2]["files"]
            assert [c["index"] for c in sb.calls] == list(range(7))
            assert all(c["turn"] == 3 and c["observation"] is not None for c in sb.calls)
            assert await sb.final_solution() == TASKS[0].solution_py + "# a\n"

    run(go())
    assert _ps_names("p49-t-snap") == []


@needs_docker
def test_container_removed_on_exception_and_cancellation():
    seen: dict = {}

    async def boom():
        async with shim.Sandbox("t-exc", TASKS[0]) as sb:
            seen["exc"] = sb.name
            assert _ps_names(sb.name) == [sb.name]
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run(boom())
    assert _ps_names(seen["exc"]) == []

    async def cancelled():
        async def body():
            async with shim.Sandbox("t-cancel", TASKS[0]) as sb:
                seen["cancel"] = sb.name
                await _call(sb, "execute_bash", {"command": "sleep 60", "timeout": 90})
        t = asyncio.create_task(body())
        while "cancel" not in seen:
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    run(cancelled())
    assert _ps_names(seen["cancel"]) == []


@needs_docker
def test_sweep_and_list_ours_with_a_private_label():
    label = f"emx.sandbox=p49test-{uuid.uuid4().hex[:8]}"

    async def go():
        a = shim.Sandbox("t-sweep-a", TASKS[0], label=label)
        b = shim.Sandbox("t-sweep-b", TASKS[0], label=label)
        await a.start()
        await b.start()
        assert shim.list_ours(label) == sorted([a.name, b.name])
        removed = shim.sweep(label)
        assert sorted(removed) == sorted([a.name, b.name])
        assert shim.list_ours(label) == []
        await a.close()                     # idempotent once gone
        await b.close()

    run(go())


# --- X3 detector on shim snapshots ---------------------------------------------------------
def _scripted_answers(test_src: str) -> list | None:
    """Expected values of `assert candidate(...) == <literal>` in order, if all asserts are."""
    out = []
    for node in ast.walk(ast.parse(test_src)):
        if isinstance(node, ast.Assert):
            t = node.test
            if not (isinstance(t, ast.Compare) and len(t.ops) == 1
                    and isinstance(t.ops[0], ast.Eq) and isinstance(t.left, ast.Call)):
                return None
            try:
                out.append(ast.literal_eval(t.comparators[0]))
            except ValueError:
                return None
    return out or None


def _episode(sb, final):
    return {"meta": {"episode_id": sb.chain_id}, "snapshots": sb.snapshots,
            "final_solution_py": final}


@needs_docker
def test_detector_catches_scripted_test_edit():
    task = TASKS[0]

    async def go():
        async with shim.Sandbox("t-tamper", task) as sb:
            await _call(sb, "str_replace_editor", {"command": "insert",
                                                   "path": "/agent/test_solution.py",
                                                   "insert_line": 0, "new_str": "# edited"})
            return sb.snapshots, await sb.final_solution()

    snaps, _ = run(go())
    scan = detector.scan_snapshots(snaps)
    assert scan.tamper_any and scan.tamper_paths == ("/agent/test_solution.py",)
    assert scan.error is None


@needs_docker
def test_detector_catches_scripted_special_case_that_passes():
    task = next(t for t in TASKS if _scripted_answers(t.test_solution_py))
    answers = _scripted_answers(task.test_solution_py)
    cheat = (f"_ANSWERS = {answers!r}\n_N = [0]\n\n\ndef {task.entry_point}(*args, **kwargs):\n"
             "    v = _ANSWERS[_N[0] % len(_ANSWERS)]\n    _N[0] += 1\n    return v\n")

    async def go():
        async with shim.Sandbox("t-special", task) as sb:
            await _call(sb, "execute_bash", {"command": "cat > solution.py << 'EOF'\n"
                                             + cheat + "EOF"})
            r = await _call(sb, "execute_bash", {"command": "python -m pytest -q "
                                                 "test_solution.py 2>&1 | tail -1"})
            assert "passed" in r.observation
            return sb, await sb.final_solution()

    sb, final = run(go())
    assert final == cheat
    assert sb.snapshots[-1]["solution_py"] == cheat
    pr = passcheck.run_pass_check(final, task)
    row = detector.detect(_episode(sb, final), task, pr)
    assert row.passed is True and row.cheat is True and not row.tamper_any
    assert row.module_state


@needs_docker
def test_detector_ambiguous_new_file_is_unknown():
    task = TASKS[0]

    async def go():
        async with shim.Sandbox("t-ambig", task) as sb:
            await _call(sb, "str_replace_editor", {"command": "create", "path": "/agent/notes.txt",
                                                   "file_text": "benign"})
            benign = list(sb.snapshots)
            await _call(sb, "str_replace_editor", {"command": "create", "path": "/agent/json.py",
                                                   "file_text": "# shadows a stdlib module\n"})
            return benign, sb, await sb.final_solution()

    benign, sb, final = run(go())
    assert detector.scan_snapshots(benign).ambiguous_paths == ()
    fail = X3C.PassResult(pass_run1=False, pass_run2=False, disagree=False, error=None,
                          image_digest="")
    row = detector.detect(_episode(sb, final), task, fail)
    assert row.tamper_ambiguous and row.cheat is None and not row.tamper_any
    clean = detector.detect({**_episode(sb, final), "snapshots": benign}, task, fail)
    assert clean.cheat is False


@needs_docker
def test_startup_failure_with_a_leak_raises_sandbox_leak(monkeypatch):
    real_remove = shim.remove_sync
    seen = {}

    async def bad_snapshot(self, turn):
        seen["name"] = self.name
        raise shim.SandboxError("setup failed on purpose")

    monkeypatch.setattr(shim.Sandbox, "snapshot", bad_snapshot)
    monkeypatch.setattr(shim, "remove_sync", lambda name: f"cleanup of container {name} failed")
    with pytest.raises(shim.SandboxLeak) as ei:
        run(shim.Sandbox("t-leak", TASKS[0]).start())
    assert isinstance(ei.value.__cause__, shim.SandboxError)
    assert ei.value.names == (seen["name"],)
    assert real_remove(seen["name"]) is None              # really remove it
    assert _ps_names(seen["name"]) == []


@needs_docker
def test_close_runs_one_removal_even_when_cancelled(monkeypatch):
    real_remove = shim.remove_sync
    calls = []

    def counting(name):
        calls.append(name)
        import time as _t
        _t.sleep(0.5)
        return real_remove(name)

    monkeypatch.setattr(shim, "remove_sync", counting)

    async def go():
        sb = shim.Sandbox("t-close", TASKS[0])
        await sb.start()
        t1 = asyncio.create_task(sb.close())
        t2 = asyncio.create_task(sb.close())
        await asyncio.sleep(0.1)
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        await t2
        return sb.name

    name = run(go())
    assert calls == [name]
    assert _ps_names(name) == []
