"""SWE-Interact simulator port and the agent + simulator fork (compaction.s2.interact).

The unit tests run on a local temp git repo. `test_fork_in_docker` is the feasibility fork test:
a stub agent and a deterministic stub simulator run in a real task container to a quiescent
boundary in 02_implement; the boundary (docker commit + JSON) is restored into two arms, which
must build identical next requests (agent and simulator) and stay isolated. It needs Docker and
a local task image (S2_INTERACT_TEST_IMAGE; default: a DeepSWE v1.1 image already pulled).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest

from compaction.s2 import interact as I
from compaction.s2.model import ModelError, Reply, RunCapReached, Usage, tool_call

PERSONA = "# Shared invariants\n...\n\nHidden task block: add `frobnicate(x)` returning 2*x.\n"


class StubSim:
    """Deterministic simulator model: round 1 inspects `git log`, round 2 replies with a hash of
    the whole request it was sent, so equal replies mean equal requests."""

    def __init__(self, fail: BaseException | None = None):
        self.calls: list[tuple[list[dict], bool]] = []
        self.fail = fail

    def complete(self, messages, tools, max_tokens, *, purpose):
        self.calls.append((list(messages), bool(tools)))
        if self.fail is not None and tools:
            raise self.fail
        if tools and messages[-1]["role"] != "tool":
            msg = {"role": "assistant", "content": None, "tool_calls": [
                tool_call("run_repo_command", {"command": "git log --oneline -3"},
                          f"c{len(self.calls)}")]}
        else:
            h = hashlib.sha256(I.canon(messages).encode()).hexdigest()[:12]
            msg = {"role": "assistant", "content": f"reply-{h}"}
        return Reply(msg, Usage(100, 10, 50, 5, 0.0, "stub"), "stop")


def local_runner(root):
    def run(cmd, timeout_s):
        p = subprocess.run(["bash", "-c", cmd], cwd=root, capture_output=True, text=True, check=False,
                           timeout=timeout_s)
        return p.returncode, p.stdout, p.stderr
    return run


@pytest.fixture
def repo(tmp_path):
    def sh(c):
        subprocess.run(["bash", "-c", c], cwd=tmp_path, check=True, capture_output=True)
    sh("git init -q && git config user.email a@b && git config user.name A && echo one > a.txt "
       "&& git add a.txt && git commit -qm initial")
    return tmp_path, sh


def test_allowlist_matches_upstream():
    assert I.allowed_command(["git", "status"])[0]
    assert not I.allowed_command(["git", "commit", "-m", "x"])[0]
    assert not I.allowed_command(["rm", "-rf", "/"])[0]
    assert not I.allowed_command([])[0]
    out = json.loads(I.run_command(lambda c, t: (0, "", ""), {"command": "python -c 1"}))
    assert out == {"ok": False, "error": "command is not allowlisted: python"}


def test_run_command_keeps_argv_literal(repo):
    root, _ = repo
    out = json.loads(I.run_command(local_runner(root), {"command": "ls '*.txt'"}))
    assert out["ok"] is False  # no globbing, as upstream's shell-less subprocess
    out = json.loads(I.run_command(local_runner(root), {"command": "ls", "max_chars": 1000}))
    assert out["ok"] and "a.txt" in out["stdout"]


def test_ask_auto_snapshot_tool_round_and_state(repo):
    root, _ = repo
    model = StubSim()
    sim = I.UserSim(PERSONA, model, local_runner(root))
    assert sim.ask("hi, what do you need?").startswith("reply-")
    first = model.calls[0][0]
    assert first[0]["content"].startswith(I.SYSTEM_PREFIX) and PERSONA in first[0]["content"]
    assert len(first) == 2  # no review marker: no snapshot
    sim.ask("Done, I committed it. Ready for review?")
    req = model.calls[2][0]
    assert req[-1]["role"] == "system" and "git status --short" in req[-1]["content"]
    assert "initial" in req[-1]["content"]  # latest commit patch
    assert model.calls[3][0][-1]["role"] == "tool"  # the inspection result went back
    assert sim.state.ask_user_call_count == 2 and len(sim.state.conversation) == 4
    assert [r["round"] for r in sim.rounds] == [1, 2, 1, 2]


def test_export_restore_and_ask_log_rebuild(repo):
    root, _ = repo
    sim = I.UserSim(PERSONA, StubSim(), local_runner(root))
    sim.ask("plan: add frobnicate. ok?")
    sim.ask("done, committed. review?")
    blob = json.dumps(sim.export())
    a = I.UserSim.restore(json.loads(blob), PERSONA, StubSim(), local_runner(root))
    b = I.UserSim.restore(json.loads(blob), PERSONA, StubSim(), local_runner(root))
    q = "fixed it, review again?"
    assert I.canon(a.first_request(q)) == I.canon(b.first_request(q)) == \
        I.canon(sim.first_request(q))
    assert I.SimState.from_ask_log(sim.ask_log) == sim.state
    a.ask(q)
    assert b.state == sim.state != a.state  # restored copies are independent
    with pytest.raises(ValueError):
        I.UserSim.restore(json.loads(blob), PERSONA + "x", StubSim(), local_runner(root))


def test_model_errors_fall_back_but_caps_propagate(repo):
    root, _ = repo
    sim = I.UserSim(PERSONA, StubSim(fail=ModelError("boom")), local_runner(root))
    assert sim.ask("hello").startswith("reply-")  # plain-completion fallback
    sim = I.UserSim(PERSONA, StubSim(fail=RunCapReached("cap")), local_runner(root))
    with pytest.raises(RunCapReached):
        sim.ask("hello")


def test_flatten_for_plain_completion():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "run_repo_command", "arguments": "{}"}}]},
            {"role": "tool", "name": "run_repo_command", "content": "out"}]
    flat = I.flatten_for_plain_completion(msgs)
    assert [m["role"] for m in flat] == ["system", "user", "system"]
    assert "run_repo_command({})" in flat[-1]["content"] and "out" in flat[-1]["content"]


# ------------------------------------------------------------------------------------------
# The fork test (Docker)
# ------------------------------------------------------------------------------------------

DEFAULT_IMAGE = ("public.ecr.aws/d3j8x8q7/swe-bench-202605:"
                 "kh705e1hgmtj22pyetcmqa8ygs83bh33-v1.1")
IMAGE = os.environ.get("S2_INTERACT_TEST_IMAGE", DEFAULT_IMAGE)


def _image_present() -> bool:
    if not shutil.which("docker"):
        return False
    p = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=60,
                       check=False)
    return p.returncode == 0


SETUP = ("git config --global user.name Dev && git config --global user.email dev@example.com"
         " && git config --global --add safe.directory '*'")
# The stub agent's 02_implement up to the boundary: save base, edit, commit, ask for review,
# leave an untracked scratch file, then the boundary falls after the call.
AGENT_PRE = [
    {"bash": "cat /tmp/plan.md && git rev-parse HEAD > /tmp/base_commit.txt"},
    {"bash": "printf 'export const frob = (x) => 2 * x\\n' > frob.ts && git add frob.ts"
             " && git commit -qm 'initial implementation' && echo ok"},
    {"ask": "Done with the initial implementation, committed. Can you review?"},
    {"bash": "echo notes > scratch_notes.txt && echo ok"},
]
NEXT_Q = "Fixed that, committed again. Review?"


@pytest.mark.skipif(not _image_present(), reason=f"needs docker and {IMAGE}")
def test_fork_in_docker(tmp_path):
    from compaction.deepswe.sandbox import Invocation
    from compaction.s2.loop import TOOLS, Loop
    from compaction.s2.model import StubModel

    def setup(sb):
        r = sb.exec(SETUP + " && printf 'Plan: add frob(x)=2x in frob.ts\\n' > /tmp/plan.md",
                    timeout_s=120)
        assert r.rc == 0, r.stderr

    report: dict = {}
    with Invocation(purpose="s2-interact") as inv:
        sb0 = inv.start(IMAGE, cpus=2, memory_mb=4096, network="none", lifetime_s=1800)
        setup(sb0)
        sim0 = I.UserSim(PERSONA, StubSim(), I.sandbox_runner(sb0), purpose="sim:base")
        sim0.ask("hi, what's the change?")  # 01_plan's talk; the sim keeps it across steps
        env0 = I.InteractEnv(sb0, sim0)
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "02_implement instruction: read /tmp/plan.md"}]
        loop0 = Loop(msgs, StubModel(list(AGENT_PRE)), env0, "base")
        for _ in AGENT_PRE:
            loop0.step(None)
        b = I.capture("fork-test", "02_implement", sb0, inv, loop0.messages, sim0, "fork-test")
        bpath = tmp_path / "boundary.json"
        b.save(bpath)
        pre_fp = I.fingerprint(I.sandbox_runner(sb0))
        pre_agent = I.agent_request(loop0.messages, TOOLS)
        pre_sim = I.canon(sim0.first_request(NEXT_Q))

        arms = {}
        for name in ("A", "B"):
            bb = I.Boundary.load(bpath)
            sb = inv.start(bb.image, cpus=2, memory_mb=4096, network="none", lifetime_s=1800)
            sim = I.UserSim.restore(bb.sim, PERSONA, StubSim(), I.sandbox_runner(sb),
                                    purpose=f"sim:{name}")
            arms[name] = (sb, sim, bb.agent_messages)
        sb0.close()  # arms must not depend on the base container

        for name, (sb, sim, am) in arms.items():
            fp = I.fingerprint(I.sandbox_runner(sb))  # v3: one hash line
            listing = I.fingerprint(I.sandbox_runner(sb), 2)  # v2 lists paths, for the checks
            report[name] = {
                "fingerprint_equal": fp == pre_fp,
                "fingerprint_sha_equal": I.sha256(fp) == b.fingerprint_sha256,
                "agent_request_equal": I.agent_request(am, TOOLS) == pre_agent,
                "sim_request_equal": I.canon(sim.first_request(NEXT_Q)) == pre_sim,
                "plan_md": "missing /tmp/plan.md" not in listing,
                "untracked_kept": "scratch_notes.txt" in listing,
                "persona_not_in_container": sb.exec(
                    "grep -rIl --exclude-dir=proc --exclude-dir=sys 'frobnicate(x)' /app /tmp"
                    " /root 2>/dev/null | head -1", timeout_s=120).stdout.strip() == ""}

        # Isolation: arm A moves on (a new commit and a simulator reply); B must not change.
        sbA, simA, amA = arms["A"]
        loopA = Loop(list(amA), StubModel([
            {"bash": "printf 'x\\n' >> frob.ts && git commit -qam 'address feedback' && echo ok"},
            {"ask": NEXT_Q}]), I.InteractEnv(sbA, simA), "arm:A")
        loopA.step(None)
        loopA.step(None)
        sbB, simB, _ = arms["B"]
        report["isolation"] = {
            "A_changed_repo": I.fingerprint(I.sandbox_runner(sbA)) != pre_fp,
            "A_sim_advanced": simA.state.ask_user_call_count == sim0.state.ask_user_call_count + 1,
            "B_repo_unchanged": I.fingerprint(I.sandbox_runner(sbB)) == pre_fp,
            "B_sim_unchanged": simB.state == I.SimState.from_dict(b.sim["state"]),
            "B_sim_request_unchanged": I.canon(simB.first_request(NEXT_Q)) == pre_sim,
            "A_sim_request_differs": I.canon(simA.first_request(NEXT_Q)) != pre_sim}
        # The deterministic stub replies with a hash of its request: B's reply to the same
        # question differs from A's, since A's simulator saw A's own commit.
        rB = simB.ask(NEXT_Q)
        report["isolation"]["A_reply_differs"] = simA.state.conversation[-1]["content"] != rB
    out = os.environ.get("S2_INTERACT_FORK_REPORT")
    if out:
        with open(out, "w") as f:
            json.dump(report, f, indent=1)
    for name in ("A", "B"):
        assert all(report[name].values()), report[name]
    assert all(report["isolation"].values()), report["isolation"]


# ------------------------------------------------------------------------------------------
# Parity with the released simulator: upstream server.py + repo_exec_server.py, run in-process
# with litellm and fastmcp stubbed, against the same repo and the same scripted model. Every
# request upstream sends must equal what the port sends.
# ------------------------------------------------------------------------------------------

UPSTREAM_TASK = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw", "sprint",
                             "swe_interact", "repo", "data", "multiturn",
                             "deepswe_boa-hierarchical-evaluation-cancellation", "environment")


def _load_upstream(tmp_path, repo_root, persona, captured):
    import importlib.util
    import pathlib
    import sys
    import threading
    import types

    class FakeMsg:
        def __init__(self, d):
            self.d = d

        def model_dump(self, exclude_none=False):
            return {k: v for k, v in self.d.items() if not (exclude_none and v is None)}

    def completion(**kw):
        captured.append({**kw, "messages": list(kw["messages"])})  # as sent
        msgs, tools = kw["messages"], kw.get("tools")
        if tools and msgs[-1]["role"] != "tool":
            d = {"role": "assistant", "content": None, "tool_calls": [
                tool_call("run_repo_command", {"command": "git log --oneline -3"},
                          f"c{len(captured)}")]}
        else:
            h = hashlib.sha256(I.canon(msgs).encode()).hexdigest()[:12]
            d = {"role": "assistant", "content": f"reply-{h}"}
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=FakeMsg(d))])

    class FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self):
            return lambda f: f

    os.environ["REPO_EXEC_ROOT"] = str(repo_root)
    os.environ["REPO_EXEC_PORT"] = "0"
    spec = importlib.util.spec_from_file_location(
        "si_repo_exec", os.path.join(UPSTREAM_TASK, "repo_exec_server.py"))
    rx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rx)
    srv = rx.ThreadingHTTPServer(("127.0.0.1", 0), rx.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    saved = {k: sys.modules.get(k) for k in ("litellm", "fastmcp")}
    sys.modules["litellm"] = types.SimpleNamespace(completion=completion)
    sys.modules["fastmcp"] = types.SimpleNamespace(FastMCP=FastMCP)
    os.environ.update(LITELLM_API_KEY="x", LITELLM_BASE_URL="http://unused",
                      SIM_USER_MODEL="openai/gpt-5.5",
                      REPO_EXEC_URL=f"http://127.0.0.1:{srv.server_address[1]}/tool",
                      SIM_USER_PRIVATE_LOG_DIR=str(tmp_path / "simlogs"))
    real_read = pathlib.Path.read_text

    def read_text(self, *a, **k):  # server.py reads /app/persona.md at import
        return persona if str(self) == "/app/persona.md" else real_read(self, *a, **k)

    pathlib.Path.read_text = read_text
    try:
        spec = importlib.util.spec_from_file_location(
            "si_server", os.path.join(UPSTREAM_TASK, "user-server", "server.py"))
        up = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(up)
    finally:
        pathlib.Path.read_text = real_read
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return up, srv


@pytest.mark.skipif(not os.path.isdir(UPSTREAM_TASK), reason="SWE-Interact clone not present")
def test_requests_match_released_server(repo, tmp_path):
    root, sh = repo
    with open(os.path.join(UPSTREAM_TASK, "user-server", "persona.md")) as f:
        persona = f.read()
    captured: list[dict] = []
    up, srv = _load_upstream(tmp_path, root, persona, captured)
    try:
        model = StubSim()
        sim = I.UserSim(persona, model, local_runner(root))
        script = [("hi, what do you need?", None),
                  ("Plan: add handles. OK?", None),
                  ("Done, committed the initial implementation. Review?",
                   "echo two >> a.txt && echo new > b.txt && git add -A && git commit -qm impl"),
                  ("Fixed, committed again. Ship it?",
                   "echo three >> a.txt && git commit -qam 'address feedback'")]
        for q, edit in script:
            if edit:
                sh(edit)
            r_up = up.ask_user(q)
            r_port = sim.ask(q)
            assert r_up == r_port
        assert len(captured) == len(model.calls) == 8
        for kw, (msgs, has_tools) in zip(captured, model.calls, strict=True):
            assert I.canon(kw["messages"]) == I.canon(msgs)
            assert bool(kw.get("tools")) == has_tools
            if has_tools:
                assert kw["tools"] == I.SIM_TOOLS and kw["tool_choice"] == "auto"
            assert kw["max_tokens"] == sim.config.max_tokens
            assert kw["reasoning_effort"] == sim.config.reasoning_effort
            assert kw["model"] == sim.config.model
        assert up.conversation == sim.state.conversation
        assert up.ask_user_call_count == sim.state.ask_user_call_count
        with open(tmp_path / "simlogs" / "ask-user.jsonl") as f:
            rows = [json.loads(x) for x in f]
        assert I.SimState.from_ask_log(rows) == sim.state  # restore from the real log too
    finally:
        srv.shutdown()


@pytest.mark.skipif(not _image_present(), reason=f"needs docker and {IMAGE}")
def test_fingerprint_v2_handles_symlinks_and_matches_v1_without_them():
    """Pilot a1 on helm (24 Oct): v1 ran sha256sum through broken testdata symlinks (rc 123).
    v2 records links as links, never followed, and equals v1 on a repo without any."""
    from compaction.deepswe.sandbox import Invocation

    with Invocation(purpose="s2-interact") as inv:
        sb = inv.start(IMAGE, cpus=2, memory_mb=4096, network="none", lifetime_s=900)
        run = I.sandbox_runner(sb)
        links = sb.exec("cd /app && git ls-files -z -co --exclude-standard"
                        " | xargs -0 -r bash -c 'for f; do [ -L \"$f\" ] && echo \"$f\"; done; true' _",
                        timeout_s=120).stdout.split()
        if not links:
            assert I.fingerprint(run, 2) == I.fingerprint(run, 1)  # unchanged for old boundaries
        r = sb.exec(SETUP + " && cd /app && ln -s does/not/exist broken-link"
                    " && ln -s README.md readme-link 2>/dev/null; f=$(git ls-files | head -1);"
                    " rm -f \"$f\"; echo \"$f\"", timeout_s=120)
        assert r.rc == 0, r.stderr
        gone = r.stdout.strip().splitlines()[-1]
        with pytest.raises(RuntimeError, match="v1 failed"):
            I.fingerprint(run, 1)
        fp = I.fingerprint(run, 2)
        assert "link broken-link -> does/not/exist" in fp
        assert f"missing {gone}" in fp
        assert I.fingerprint(run, 2) == fp  # deterministic
        sb.exec("cd /app && ln -sfn elsewhere broken-link", timeout_s=60)
        assert I.fingerprint(run, 2) != fp  # a retargeted link changes it



def test_fingerprint_v1_v2_commands_are_frozen():
    """Boundaries recorded v1 or v2 (helm a2's live process loaded v2), and their fork checks
    re-run those exact commands: they must never change."""
    import hashlib
    pinned = {1: "2b4882f5f35d3d0a1c6fe2d38e2f88c8ac1230624ef1d54ac23761a50b445c11",
              2: "024fb134de06e294f2630e8da9de656ac075e293ef7e0cdbf102612e06bff5c0"}
    for v, h in pinned.items():
        assert hashlib.sha256(I.FINGERPRINT_CMDS[v].encode()).hexdigest() == h, v
    assert I.FINGERPRINT_VERSION == 3 and I.FINGERPRINT_CMD == I.FINGERPRINT_CMDS[1]


@pytest.mark.skipif(not _image_present(), reason=f"needs docker and {IMAGE}")
def test_fingerprint_v3_is_lossless():
    """astra fingerprint r1: v2 can't tell a link target "t" from "t\\n" (command
    substitution strips trailing newlines), nor separate ambiguous newline records; v3 can."""
    from compaction.deepswe.sandbox import Invocation

    with Invocation(purpose="s2-interact") as inv:
        sb = inv.start(IMAGE, cpus=2, memory_mb=4096, network="none", lifetime_s=900)
        run = I.sandbox_runner(sb)
        assert sb.exec(SETUP + " && cd /app && ln -s does/not/exist broken-link",
                       timeout_s=120).rc == 0
        v3 = I.fingerprint(run, 3)
        assert re.fullmatch(r"v3 [0-9a-f]{64}\n", v3)
        assert I.fingerprint(run, 3) == v3  # deterministic
        # a trailing newline in a link target
        assert sb.exec("cd /app && ln -s target lnk", timeout_s=60).rc == 0
        a2, a3 = I.fingerprint(run, 2), I.fingerprint(run, 3)
        assert sb.exec("cd /app && ln -sfn $'target\\n' lnk", timeout_s=60).rc == 0
        assert I.fingerprint(run, 2) == a2  # the v2 collision
        assert I.fingerprint(run, 3) != a3  # v3 tells them apart
        # a newline inside a file name that mimics another record
        assert sb.exec("cd /app && rm lnk && touch $'x\\nlink lnk -> target'",
                       timeout_s=60).rc == 0
        b3 = I.fingerprint(run, 3)
        assert sb.exec("cd /app && rm $'x\\nlink lnk -> target' && touch x && ln -s target lnk",
                       timeout_s=60).rc == 0
        assert I.fingerprint(run, 3) != b3
        # an ordinary edit changes it
        c3 = I.fingerprint(run, 3)
        assert sb.exec("cd /app && echo more >> x", timeout_s=60).rc == 0
        assert I.fingerprint(run, 3) != c3


def test_a_docker_client_timeout_never_removes_the_task_container(monkeypatch):
    """main session, 24 Oct (native-codex): Sandbox.exec's guard removed whole task containers on
    a client timeout under ssh stalls. The agent's and the simulator's commands now kill the
    client's process group, settle the exec in the container (cancel marker, then kill every
    process holding its id) and only then report rc 124 with the container kept; an unconfirmed
    settle fails closed (astra tolerant-exec r1). Fingerprints keep the guarded runner."""
    import subprocess
    from types import SimpleNamespace

    from compaction.deepswe import sandbox as SB
    from compaction.s2 import interact as I2

    closed, execs, killed, popens = [], [], [], []

    def sb_exec(cmd, timeout_s=600, workdir="/app"):
        execs.append(cmd)
        return SB.ExecResult(0, "SETTLED\n", "", 0.1)
    sb = SimpleNamespace(name="s2-interact-x", host=None, log=[], exec=sb_exec,
                         close=lambda: closed.append(1))

    class Stalled:
        def __init__(self, argv, **kw):
            popens.append((argv, kw))
            self.pid, self.returncode, self.n = 4242, None, 0

        def communicate(self, timeout=None):
            self.n += 1
            if self.n == 1:
                raise subprocess.TimeoutExpired("docker", timeout)
            return b"", b""
    monkeypatch.setattr(I2.subprocess, "Popen", Stalled)
    monkeypatch.setattr(I2.os if hasattr(I2, "os") else __import__("os"), "killpg",
                        lambda pid, sig: killed.append(pid))
    stats: dict = {}
    r = I2.tolerant_exec(sb, "sleep 999", timeout_s=60, stats=stats)
    assert (r.rc, r.timed_out, closed) == (124, True, [])
    assert stats == {"client_timeouts": 1, "settles": 1} and sb.log[-1]["settled"]
    argv, kw = popens[0]
    exec_id = kw["env"][I2.EXEC_ID_VAR]
    assert argv[:4] == ["docker", "exec", "-e", I2.EXEC_ID_VAR] and exec_id not in argv[:4]
    assert kw["start_new_session"] and killed == [4242]  # the whole client group
    assert f"{I2.CANCEL_DIR}/{exec_id}" in argv[-1]  # the command checks its cancel marker
    assert "setsid bash -lc" in argv[-1] and f"{I2.EXEC_DIR}/{exec_id}.pid" in argv[-1]
    assert argv[-3:-1] == ["bash", "-c"]  # one login init: the command's own (r3 #2)
    assert f"{I2.EXEC_ID_VAR}={exec_id}" in execs[-1] and "SETTLED" in execs[-1]
    assert 'kill -KILL -"$sid"' in execs[-1] and "kill -KILL --" not in execs[-1]  # dash: no --
    run = I2.sandbox_runner(sb, stats)
    assert run("git status", 30)[0] == 124 and stats["client_timeouts"] == 2 and closed == []

    # an unconfirmed settle fails closed: survivors, a failed settle call, or a stalled one
    for outcome in (SB.ExecResult(3, "", "left", 20.0), SB.SandboxError("client timeout")):
        def bad(cmd, timeout_s=600, workdir="/app", outcome=outcome):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        sb.exec = bad
        with pytest.raises(SB.SandboxError, match="couldn't be settled"):
            I2.tolerant_exec(sb, "sleep 999", timeout_s=60, stats=stats)
    sb.exec = sb_exec

    # the native arms' env passing: names on the argv, values only in the client's env
    class Done:
        def __init__(self, argv, **kw):
            popens.append((argv, kw))
            self.pid, self.returncode = 1, 0

        def communicate(self, timeout=None):
            return b"out", b""
    monkeypatch.setattr(I2.subprocess, "Popen", Done)
    n = len(sb.log)
    I2.tolerant_exec(sb, "echo $TOK", timeout_s=5, env={"TOK": "s3cret"}, log=False)
    argv, kw = popens[-1]
    assert "TOK" in argv and "s3cret" not in " ".join(argv) and kw["env"]["TOK"] == "s3cret"
    assert len(sb.log) == n
    env = I2.InteractEnv(sb, sim=None)
    obs = env.run_agent_command("echo hi")
    assert obs.rc == 0 and "out" in obs.output and env.exec_stats == {}

    # fingerprints go through Sandbox.exec (guarded), never the tolerant path (r1 #2)
    monkeypatch.setattr(I2, "tolerant_exec", lambda *a, **k: pytest.fail("tolerant fingerprint"))
    fp_calls = []

    def fp_exec(cmd, timeout_s=600, workdir="/app"):
        fp_calls.append(cmd)
        return SB.ExecResult(0, "abc  -\n", "", 0.1)
    sb.exec = fp_exec
    I2.InteractEnv(sb, sim=None).fingerprint()
    assert fp_calls and I2.FINGERPRINT_CMDS[I2.FINGERPRINT_VERSION] in fp_calls[0]


def test_settle_fails_closed_on_an_unreadable_environ(tmp_path):
    """astra tolerant-exec r2 #1: a process whose environment can't be read can't be ruled out,
    so the settle step exits 6 (fail closed) rather than reporting SETTLED."""
    import os
    import subprocess

    from compaction.s2 import interact as I2

    if os.geteuid() == 0:
        pytest.skip("root reads every file")
    proc = tmp_path / "proc"
    for pid, readable in (("101", True), ("202", False)):
        d = proc / pid
        d.mkdir(parents=True)
        (d / "stat").write_text(f"{pid} (x) S 1 {pid} {pid} 0 -1 0\n")
        (d / "environ").write_bytes(b"PATH=/bin\0")
        if not readable:
            (d / "environ").chmod(0)
    script = I2.settle_script("t" + os.urandom(4).hex(), seconds=1, proc=str(proc))
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       check=False)
    assert r.returncode == 6 and "unreadable environ: 202" in r.stderr  # grep exit 2 (r3 #1)
    (proc / "202" / "environ").chmod(0o644)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       check=False)
    assert r.returncode == 0 and "SETTLED" in r.stdout


@pytest.mark.skipif(not _image_present(), reason=f"needs docker and {IMAGE}")
def test_a_client_timeout_settles_a_forking_child_and_keeps_timeouts_at_124():
    """astra tolerant-exec r2 (live, in Docker): an in-container timeout kills the command's
    children and still reports 124; on a docker-client timeout the settle step kills a child that
    outlived its parent, and one that left the session (caught by its tag), before rc 124."""
    from compaction.deepswe import sandbox as SB
    from compaction.deepswe.sandbox import Invocation
    from compaction.s2 import interact as I2

    scan = ("for d in /proc/[0-9]*; do [ ${d##*/} = $$ ] && continue; "
            "c=$(tr '\\0' ' ' < $d/cmdline 2>/dev/null); case \"$c\" in *for\\ d\\ in*) ;; "
            "*sleep\\ 6[0-9]*|*sleep\\ 4[0-9]*) echo LEFT $c;; esac; done; "
            "ls /tmp/late1 /tmp/late2 2>/dev/null; echo done")
    with Invocation(purpose="s2-interact") as inv:
        sb = inv.start(IMAGE, cpus=1, memory_mb=1024, lifetime_s=600)
        r = I2.tolerant_exec(sb, "(sleep 41; echo leak > /tmp/late1) & sleep 42", timeout_s=3,
                             workdir="/")
        assert r.rc == 124
        assert sb.exec(scan, timeout_s=30, workdir="/").stdout.strip() == "done"
        orig, grace = sb.exec, SB.CLIENT_GRACE_S

        def settle_with_grace(*a, **k):
            SB.CLIENT_GRACE_S = grace
            return orig(*a, **k)
        for cmd in ("(sh -c 'sleep 61; echo late > /tmp/late2' &) ; sleep 33",
                    "setsid sh -c 'sleep 62; echo late > /tmp/late2' & sleep 34"):
            sb.exec, SB.CLIENT_GRACE_S = settle_with_grace, -25
            stats: dict = {}
            try:
                r = I2.tolerant_exec(sb, cmd, timeout_s=30, workdir="/", stats=stats)
            finally:
                sb.exec, SB.CLIENT_GRACE_S = orig, grace
            assert r.rc == 124 and stats == {"client_timeouts": 1, "settles": 1}
            assert sb.exec(scan, timeout_s=30, workdir="/").stdout.strip() == "done"
