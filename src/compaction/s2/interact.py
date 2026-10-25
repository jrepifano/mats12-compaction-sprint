"""SWE-Interact's simulated maintainer inside the S2 harness, and forking agent + simulator at a
boundary (feasibility prototype, logs/s2/swe_interact_feasibility.md).

Ported from scaleapi/SWE-Interact @ b32f98c3 (Apache-2.0), cloned to
data/raw/sprint/swe_interact/repo: `data/multiturn/*/environment/user-server/server.py` (the
`ask_user` MCP tool; byte-identical across the 25 DeepSWE tasks) and `repo_exec_server.py` (the
simulator's read-only repo commands). What is kept verbatim: the system prompt, the auto-snapshot
markers and contents, the `run_repo_command` tool and its allowlist, up to 6 tool rounds, the
plain-completion fallback and the last-resort reply. What changes:

- The simulator runs in this process on the laptop, not as a sidecar container behind MCP. Its
  repo commands run in the agent's own container through `docker exec` (harness-side, with the
  trap shims off) instead of an HTTP server on main:8765, so the task container keeps
  `--network none` and the persona (which holds the hidden spec) never enters it.
- Model calls go through our `ChatModel` (ledgered; the route sets `reasoning_effort`), not
  litellm. A model error that is a run or stage cap propagates; any other falls back as upstream.
- The state is explicit (`SimState`: the conversation and the call count, which is all
  server.py keeps between calls, lines 52-53) so a boundary can export it and each arm can
  restore its own copy. Tool rounds inside one reply are scratch, as upstream.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from compaction.s2.model import ChatModel, ModelError, RunCapReached
from compaction.s2.planting import NO_RESTATEMENT, UNCHANGED, PlantingSchedule

UPSTREAM = "scaleapi/SWE-Interact@b32f98c3b8f76ca65e84341d1f30e5af7135f85d"
NOSHIM = "export TRAPS_SHIM_DISABLE=1; "
WORKSPACE = "/app"

LAST_RESORT_REPLY = (
    "I can't approve from this review state. Please show the exact latest diff "
    "and call out the externally visible API and behavior changes."
)
SYSTEM_PREFIX = (
    "You are role-playing as a user talking to a coding assistant. "
    "Stay in character at all times. Never break character or mention that you are an AI.\n\n"
    "You have private repository inspection tools. Use them silently when the assistant asks "
    "for review, approval, or says it changed code. Do not mention these tools to the assistant.\n\n"
    "When the assistant asks for review or approval after code changes, inspect the latest "
    "committed repository state before replying, even if the assistant only gave a summary. "
    "Use run_repo_command for focused follow-up inspection when the automatic snapshot is "
    "insufficient. Only ask the assistant to paste a diff if inspection fails or there is "
    "no committed coherent change to inspect.\n\n"
)
AUTO_MARKERS = ("done", "implemented", "fixed", "changed", "commit", "diff", "review", "ship",
                "tests pass", "key changes")
SIM_TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_repo_command",
        "description": "Run a read-only surface inspection command in the main container. "
                       "Allowed commands are git status/diff/show/grep/log/ls-files/rev-parse/"
                       "cat-file, rg, grep, sed, find, and ls. Do not run tests.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 60000},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}]
# The coding agent's tool, as the MCP server exposes it (FastMCP: name, docstring, one arg).
ASK_USER_TOOL = {"type": "function", "function": {
    "name": "ask_user", "description": "Ask the user a question and get their response.",
    "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                   "required": ["question"]}}}

ALLOWED_GIT = {"status", "diff", "show", "grep", "log", "rev-parse", "ls-files", "cat-file"}
ALLOWED_BINARIES = {"git", "rg", "grep", "sed", "find", "ls"}
DEFAULT_MAX_CHARS = 60000


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


# ------------------------------------------------------------------------------------------
# Repo inspection (repo_exec_server.py)
# ------------------------------------------------------------------------------------------


def allowed_command(parts: list[str]) -> tuple[bool, str]:
    if not parts:
        return False, "empty command"
    if parts[0] not in ALLOWED_BINARIES:
        return False, f"command is not allowlisted: {parts[0]}"
    if parts[0] == "git" and (len(parts) < 2 or parts[1] not in ALLOWED_GIT):
        return False, "only read-only git subcommands are allowed"
    return True, ""


def limit_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


Runner = Callable[[str, int], tuple[int, str, str]]  # (shell command, timeout) -> rc, out, err


CLIENT_TIMEOUT_RC = 124  # what an in-container `timeout` reports
EXEC_ID_VAR = "EMX_EXEC_ID"
CANCEL_DIR = "/tmp/.emx-cancel"
EXEC_DIR = "/tmp/.emx-exec"  # <id>.pid: the session (= process group) id of a tolerant exec
SETTLE_SECONDS = 20
KILL_WAIT_S = 5  # bound on stopping and reaping the killed docker client's process group

# The command's own session (astra tolerant-exec r2, the main session's design): the command
# runs as `setsid bash -lc <cmd> &`; as a background child it isn't a group leader, so setsid
# doesn't fork and its pid becomes the id of a new session and process group that every
# descendant joins. That pid goes to the pid file. The outer `bash -lc` (the in-container
# `timeout`'s child) waits; on the timeout's TERM its trap kills the whole group atomically and
# exits 124, as before (setsid takes the command out of `timeout`'s own group, which `timeout`
# would otherwise kill; dash's kill takes no "--", so the form is `kill -KILL -<pgid>`, checked
# live 24 Oct). Limitation: a descendant that calls setsid/setpgid itself leaves the group; the
# EMX_EXEC_ID tag scan is the backstop for those.


def _cancel_marker(exec_id: str) -> str:
    return f"{CANCEL_DIR}/{exec_id}"


def _pid_file(exec_id: str) -> str:
    return f"{EXEC_DIR}/{exec_id}.pid"


def session_command(exec_id: str, cmd: str) -> str:
    """What `bash -lc` runs for a tolerant exec (pure; tested): the cancel-marker check first,
    then the command in its own session, its id in the pid file."""
    pidf = shlex.quote(_pid_file(exec_id))
    return (f"[ -e {shlex.quote(_cancel_marker(exec_id))} ] && exit {CLIENT_TIMEOUT_RC}\n"
            f"mkdir -p {EXEC_DIR} || exit 125\n"
            f"trap '[ -n \"$j\" ] && kill -KILL -\"$j\" 2>/dev/null; exit {CLIENT_TIMEOUT_RC}' "
            "TERM INT HUP\n"
            f"setsid bash -lc {shlex.quote(cmd)} &\n"
            "j=$!\n"
            f"echo \"$j\" > {pidf} || {{ kill -KILL -\"$j\" 2>/dev/null; kill -KILL \"$j\"; "
            "exit 125; }\n"
            "wait \"$j\"\n")


def settle_script(exec_id: str, seconds: int = SETTLE_SECONDS, proc: str = "/proc") -> str:
    """The in-container settle step for a timed-out tolerant exec (pure; tested). The cancel
    marker first, so an exec the daemon starts late exits at its first line. Then, for up to
    `seconds`: `kill -KILL -<sid>` on the exec's group (atomic in the kernel: a forking child
    can't slip past), and KILL to every process whose environment holds the exec's tag; until
    two scans 1 s apart find no process in that session (`stat` field 6, world-readable) and
    none carrying the tag. Prints SETTLED and exits 0 then. Fails closed: exit 6 when grep can't
    read the environ of a process still in /proc (one we can't rule out), 5 when the group kill
    is refused, 3 when anything outlives the window. Residual risk (accepted, astra
    tolerant-exec r3): a descendant that left the group and dropped the tag, or that forks and
    exits between scans, can escape; the second scan narrows that, and quiesce stops strays at
    every boundary. Zombies are skipped: they write nothing, and the container's PID 1 (`timeout ...
    sleep infinity`) never reaps orphans. No pid file and no tagged process means the exec never
    got past its marker check (or never started); the marker keeps it from starting later."""
    mark, pidf = shlex.quote(_cancel_marker(exec_id)), shlex.quote(_pid_file(exec_id))
    tag = shlex.quote(f"{EXEC_ID_VAR}={exec_id}")
    return (
        f"P={shlex.quote(proc)}; clean=\"\"\n"
        f"mkdir -p {CANCEL_DIR} && : > {mark} || exit 4\n"
        f"for _ in $(seq 1 {2 * int(seconds)}); do\n"
        f"  sid=$(cat {pidf} 2>/dev/null)\n"
        "  if [ -n \"$sid\" ]; then\n"
        "    err=$(kill -KILL -\"$sid\" 2>&1)\n"
        "    case \"$err\" in *ermitted*) echo \"group kill refused: $err\" >&2; exit 5;; esac\n"
        "  fi\n"
        "  left=\"\"\n"
        "  for d in \"$P\"/[0-9]*; do\n"
        "    [ -e \"$d/environ\" ] || continue\n"
        "    pid=${d##*/}\n"
        "    [ \"$pid\" = \"$$\" ] && continue\n"
        "    st=$(cat \"$d/stat\" 2>/dev/null) || continue\n"
        "    rest=${st##*) }; set -- $rest\n"
        "    [ \"$1\" = Z ] && continue  # a zombie writes nothing (PID 1 here reaps no orphans)\n"
        f"    grep -aqF -- {tag} \"$d/environ\" 2>/dev/null; g=$?\n"
        "    if [ \"$g\" -gt 1 ]; then [ -e \"$d\" ] || continue; "
        "echo \"unreadable environ: $pid\" >&2; exit 6; fi\n"
        "    [ \"$g\" = 0 ] && { left=\"$left $pid\"; continue; }\n"
        "    [ -n \"$sid\" ] && [ \"$4\" = \"$sid\" ] && left=\"$left $pid\"\n"
        "  done\n"
        "  if [ -z \"$left\" ]; then\n"
        "    [ -n \"$clean\" ] && { echo SETTLED; exit 0; }\n"
        "    clean=1; sleep 1; continue\n"
        "  fi\n"
        "  clean=\"\"\n"
        "  kill -KILL $left 2>/dev/null\n"
        "  sleep 0.5\n"
        "done\n"
        "echo \"survivors:$left\" >&2\n"
        "exit 3\n")


def tolerant_exec(sb, cmd: str, *, timeout_s: int, workdir: str = WORKSPACE,
                  stats: dict | None = None, env: dict[str, str] | None = None,
                  log: bool = True):
    """`Sandbox.exec` without its remove-the-container guard, for commands a run can survive
    losing: the agent's own bash and the simulator's repo commands. `Sandbox.exec` removes the
    whole container on a docker-client timeout; under host load (rig's ssh stalls) that
    destroyed three native Codex task containers mid-run (24 Oct, rc 137, no OOM).

    Every call carries a unique id (EMX_EXEC_ID, by name on the argv, the value only in the
    client's environment), checks its cancel marker first, and runs in its own session
    (`session_command`). The in-container `timeout -k 5` stays the limit; the docker client runs
    in its own process group. On a client timeout the whole client group is killed and reaped
    (or the call fails closed), then the guarded settle step (`settle_script`, through
    `Sandbox.exec`) kills the exec's session and every tagged process until none is left: only
    then does the call report rc 124 ("command timed out"), as an in-container timeout would,
    with the container kept. Anything unconfirmed fails closed: SandboxError, an infrastructure
    failure that ends the run (astra tolerant-exec r1-r2). `stats` counts "client_timeouts"
    and "settles". `env` passes more variables the same way (as `native.common.exec_env`);
    `log=False` keeps a command that may expand a secret out of `sb.log`."""
    import os
    import signal
    import uuid

    from compaction.deepswe import sandbox as SB
    exec_id = uuid.uuid4().hex
    names = {EXEC_ID_VAR: exec_id, **(env or {})}
    args = SB.exec_args(sb.name, session_command(exec_id, cmd), timeout_s, workdir)
    # the wrapper runs in a plain `bash -c`; the command's own `setsid bash -lc` is the one
    # login-shell init, as with `Sandbox.exec` (astra tolerant-exec r3 #2)
    i = args.index("-lc")
    args = [args[0], *[a for k in names for a in ("-e", k)], *args[1:i], "-c", *args[i + 1:]]
    argv = SB.docker_argv(args, sb.host)
    t0 = time.monotonic()
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True, env={**os.environ, **names})
    try:
        out, err = p.communicate(timeout=timeout_s + SB.CLIENT_GRACE_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # the group is already gone
        except OSError as e:
            raise SB.SandboxError(f"client timeout on {sb.name}: the docker client's group "
                                  f"couldn't be killed ({e})") from e
        try:
            p.communicate(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired as e:
            raise SB.SandboxError(f"client timeout on {sb.name}: the docker client wasn't "
                                  f"reaped within {KILL_WAIT_S}s") from e
        if stats is not None:
            stats["client_timeouts"] = stats.get("client_timeouts", 0) + 1
        try:
            st = sb.exec(settle_script(exec_id), timeout_s=SETTLE_SECONDS + 10, workdir="/")
        except Exception as e:  # fail closed, whatever went wrong
            raise SB.SandboxError(f"client timeout on {sb.name}: the exec couldn't be settled "
                                  f"({type(e).__name__}: {e})") from e
        if st.rc != 0 or "SETTLED" not in st.stdout:
            raise SB.SandboxError(f"client timeout on {sb.name}: the exec couldn't be settled "
                                  f"(rc={st.rc}: {st.stderr[-300:]})")
        if stats is not None:
            stats["settles"] = stats.get("settles", 0) + 1
        secs = time.monotonic() - t0
        if log:
            sb.log.append({"op": "exec", "cmd": cmd[:200], "rc": CLIENT_TIMEOUT_RC,
                           "client_timeout": True, "settled": True, "seconds": secs})
        return SB.ExecResult(CLIENT_TIMEOUT_RC, "", f"command timed out after {timeout_s}s "
                             "(docker client)", secs)
    r = SB.ExecResult(p.returncode, out.decode(errors="replace"), err.decode(errors="replace"),
                      time.monotonic() - t0)
    if log:
        sb.log.append({"op": "exec", "cmd": cmd[:200], "rc": r.rc, "seconds": r.seconds})
    return r


def sandbox_runner(sb, stats: dict | None = None) -> Runner:
    """The simulator's repo commands in a `compaction.deepswe.sandbox.Sandbox` (trap shims off);
    a docker-client timeout reports rc 124 once the exec is settled (`tolerant_exec`)."""
    def run(cmd: str, timeout_s: int) -> tuple[int, str, str]:
        r = tolerant_exec(sb, NOSHIM + cmd, timeout_s=timeout_s, workdir=WORKSPACE,
                          stats=stats)
        return r.rc, r.stdout, r.stderr
    return run


def guarded_runner(sb) -> Runner:
    """Harness-side commands that must fail closed (the workspace fingerprint): `Sandbox.exec`,
    whose client timeout removes the container and raises (astra tolerant-exec r1 #2)."""
    def run(cmd: str, timeout_s: int) -> tuple[int, str, str]:
        r = sb.exec(NOSHIM + cmd, timeout_s=timeout_s, workdir=WORKSPACE)
        return r.rc, r.stdout, r.stderr
    return run


def run_command(runner: Runner, args: dict[str, Any]) -> str:
    """repo_exec_server.run_command as a JSON string. Upstream runs the argv without a shell;
    `shlex.join` through bash -lc reproduces that argv exactly (no globbing or expansion)."""
    command = str(args.get("command") or "")
    max_chars = int(args.get("max_chars") or DEFAULT_MAX_CHARS)
    timeout = int(args.get("timeout") or 60)
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": f"invalid command: {exc}"})
    ok, reason = allowed_command(parts)
    if not ok:
        return json.dumps({"ok": False, "error": reason})
    try:
        rc, out, err = runner(shlex.join(parts), timeout)
    except Exception as exc:  # noqa: BLE001 - upstream returns the error to the model
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return json.dumps({"ok": rc == 0, "returncode": rc, "stdout": limit_text(out, max_chars),
                       "stderr": limit_text(err, max_chars)})


# ------------------------------------------------------------------------------------------
# The simulator (server.py)
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SimConfig:
    model: str = "openai/gpt-5.5"  # run_configs/multiturn/*.sh: SIM_USER_MODEL
    reasoning_effort: str = "high"
    max_tokens: int = 32768
    max_tool_rounds: int = 6


@dataclass
class SimState:
    """Everything the upstream server keeps between ask_user calls."""
    conversation: list[dict] = field(default_factory=list)
    ask_user_call_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SimState:
        return cls(conversation=[dict(m) for m in d["conversation"]],
                   ask_user_call_count=int(d["ask_user_call_count"]))

    @classmethod
    def from_ask_log(cls, rows: list[dict]) -> SimState:
        """Rebuild from upstream's ask-user.jsonl (each `ask_user_finish` row has the question
        and the reply), e.g. to fork a run made with the real sidecar."""
        done = sorted((r for r in rows if r.get("event") == "ask_user_finish"),
                      key=lambda r: r["call_index"])
        conv: list[dict] = []
        for r in done:
            conv += [{"role": "user", "content": r["question"]},
                     {"role": "assistant", "content": r["reply"]}]
        return cls(conv, len(done))


def should_auto_inspect(question: str) -> bool:
    lowered = question.lower()
    return any(m in lowered for m in AUTO_MARKERS)


def _clean(msg: dict) -> dict:
    """model_dump(exclude_none=True), recursively for tool calls."""
    out = {k: v for k, v in msg.items() if v is not None}
    if out.get("tool_calls"):
        out["tool_calls"] = [{k: v for k, v in tc.items() if v is not None}
                             for tc in out["tool_calls"]]
    return out


def flatten_for_plain_completion(messages: list[dict]) -> list[dict]:
    flattened: list[dict] = []
    private: list[str] = []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role == "tool":
            private.append(f"Tool result from {m.get('name') or 'tool'}:\n{content}")
            continue
        if role == "assistant" and m.get("tool_calls"):
            calls = [f"{(tc.get('function') or {}).get('name') or 'tool'}"
                     f"({(tc.get('function') or {}).get('arguments') or '{}'})"
                     for tc in m["tool_calls"]]
            if calls:
                private.append("Tool calls requested:\n" + "\n".join(calls))
            if content:
                flattened.append({"role": "assistant", "content": content})
            continue
        if role in {"system", "user", "assistant"}:
            flattened.append({"role": role, "content": content})
    if private:
        flattened.append({"role": "system", "content": "Private repository inspection context "
                          "gathered before fallback:\n\n" + "\n\n".join(private)})
    return flattened


@dataclass
class UserSim:
    persona: str
    model: ChatModel
    runner: Runner
    state: SimState = field(default_factory=SimState)
    config: SimConfig = field(default_factory=SimConfig)
    rounds: list[dict] = field(default_factory=list)  # per model call: usage, cost, tools
    ask_log: list[dict] = field(default_factory=list)  # upstream's ask-user.jsonl rows
    # what the simulator saw (main session, 25 Oct; replays and audits need it, requests are
    # unchanged): per ask its auto-snapshot as sent, per repo tool call its arguments and output
    io: list[dict] = field(default_factory=list)
    purpose: str = "sim"
    planting: PlantingSchedule | None = None

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PREFIX + self.persona + (NO_RESTATEMENT if self.planting is not None else "")

    def set_delivery_context(self, *, stage=UNCHANGED, cumulative_tokens=UNCHANGED) -> None:
        if self.planting is not None:
            self.planting.set_context(stage=stage, cumulative_tokens=cumulative_tokens)

    def record_compaction(self, k: int, *, pre_tokens: int | None,
                          cumulative_tokens: int | None = None) -> dict | None:
        if self.planting is None:
            return None
        return self.planting.record_compaction(k, pre_tokens=pre_tokens,
                                              cumulative_tokens=cumulative_tokens)

    def snapshot_message(self, question: str) -> dict | None:
        if not should_auto_inspect(question):
            return None
        status = run_command(self.runner, {"command": "git status --short", "max_chars": 12000})
        latest = run_command(self.runner, {"command": "git show --patch --find-renames --stat HEAD",
                                           "max_chars": 50000})
        return {"role": "system", "content": (
            "Private repository inspection snapshot. Use this silently before responding; "
            "do not mention that you inspected the repo.\n\n"
            f"Question from assistant:\n{question}\n\n"
            f"git status --short:\n{status}\n\n"
            f"latest commit patch:\n{latest}")}

    def first_request(self, question: str) -> list[dict]:
        """The messages the first model round of `ask(question)` would send (no state change)."""
        msgs = [{"role": "system", "content": self.system_prompt}, *self.state.conversation,
                {"role": "user", "content": question}]
        snap = self.snapshot_message(question)
        return msgs + ([snap] if snap else [])

    def _complete(self, messages: list[dict], tools: list[dict] | None, k: int) -> dict:
        t0 = time.monotonic()
        rep = self.model.complete(messages, tools, self.config.max_tokens,
                                  purpose=f"{self.purpose}:{self.state.ask_user_call_count}:{k}")
        u = rep.usage
        msg = _clean(rep.message)
        self.rounds.append({
            "call_index": self.state.ask_user_call_count, "round": k, "tools": bool(tools),
            "prompt_tokens": u.prompt_tokens, "cached_tokens": u.cached_tokens,
            "completion_tokens": u.completion_tokens, "reasoning_tokens": u.reasoning_tokens,
            "cost_usd": rep.cost_total_usd, "cost_source": u.cost_source,
            "n_tool_calls": len(msg.get("tool_calls") or []), "finish_reason": rep.finish_reason,
            "seconds": round(time.monotonic() - t0, 3)})
        return msg

    def _plain(self, working: list[dict]) -> str:
        try:
            return self._complete(flatten_for_plain_completion(working), None, 0).get(
                "content") or ""
        except RunCapReached:
            raise
        except ModelError:
            return LAST_RESORT_REPLY

    def _with_tools(self, messages: list[dict]) -> str:
        working = list(messages)
        for k in range(1, self.config.max_tool_rounds + 1):
            try:
                msg = self._complete(working, SIM_TOOLS, k)
            except RunCapReached:
                raise
            except ModelError as e:
                # Upstream falls back silently too (llm_tool_round_error); record it, since a
                # route that always fails here never inspects with tools (24 Oct: OpenAI chat
                # completions refused tools + reasoning_effort for gpt-5.5, 400).
                self.rounds.append({"call_index": self.state.ask_user_call_count, "round": k,
                                    "tools": True, "error": str(e)[:300],
                                    "cost_usd": e.cost_usd})
                return self._plain(working)
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content") or ""
            working.append({"role": "assistant", "content": msg.get("content") or "",
                            "tool_calls": calls})
            for tc in calls:
                fn = tc.get("function") or {}
                out = self._execute(fn)
                self.io.append({"call_index": self.state.ask_user_call_count, "round": k,
                                "kind": "tool", "tool_call_id": tc.get("id") or "",
                                "name": fn.get("name") or "",
                                "arguments": fn.get("arguments") or "", "output": out})
                working.append({"role": "tool", "tool_call_id": tc.get("id") or "",
                                "name": fn.get("name") or "", "content": out})
        return self._plain(working)

    def _execute(self, fn: dict) -> str:
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"invalid tool arguments: {exc}"})
        if fn.get("name") == "run_repo_command":
            return run_command(self.runner, args)
        return json.dumps({"ok": False, "error": f"unknown tool: {fn.get('name')}"})

    def ask(self, question: str) -> str:
        self.state.ask_user_call_count += 1
        i = self.state.ask_user_call_count
        messages = self.first_request(question)  # before the question joins the conversation
        snap = messages[-1] if messages[-1].get("role") == "system" and len(messages) > 2 else None
        self.io.append({"call_index": i, "kind": "snapshot",
                        "content": snap["content"] if snap else None})
        self.state.conversation.append({"role": "user", "content": question})
        text = self._with_tools(messages)
        audit = {}
        if self.planting is not None:
            text, audit = self.planting.apply(text, call_index=i)
        self.state.conversation.append({"role": "assistant", "content": text})
        self.ask_log.append({"event": "ask_user_finish", "call_index": i, "question": question,
                             "reply": text, **copy.deepcopy(audit)})
        if self.planting is not None:
            self.io.append({"call_index": i, "kind": "reply", "reply": text,
                            **copy.deepcopy(audit)})
        return text

    # --- fork ------------------------------------------------------------------------------
    def export(self) -> dict:
        exported = {"upstream": UPSTREAM, "config": asdict(self.config),
                    "persona_sha256": sha256(self.persona), "state": self.state.to_dict()}
        if self.planting is not None:
            # Cumulative by design for exact planting provenance. Native probe scoring uses
            # its window event stream, never these inherited rows as new actions.
            exported.update(ask_log=copy.deepcopy(self.ask_log), io=copy.deepcopy(self.io),
                            planting=self.planting.export())
        return exported

    @classmethod
    def restore(cls, exported: dict, persona: str, model: ChatModel, runner: Runner,
                purpose: str = "sim") -> UserSim:
        if sha256(persona) != exported["persona_sha256"]:
            raise ValueError("persona differs from the exported simulator's")
        return cls(persona, model, runner, SimState.from_dict(exported["state"]),
                   SimConfig(**exported["config"]),
                   ask_log=copy.deepcopy(exported.get("ask_log", [])),
                   io=copy.deepcopy(exported.get("io", [])), purpose=purpose,
                   planting=(PlantingSchedule.restore(exported["planting"])
                             if exported.get("planting") is not None else None))


# ------------------------------------------------------------------------------------------
# Boundary export / restore
# ------------------------------------------------------------------------------------------

# A fingerprint of everything in the workspace the next request can depend on: HEAD, status,
# every tracked or untracked (non-ignored) file's hash, and the step files outside the repo.
# v1 ran sha256sum on every listed path, following symlinks, so a broken symlink failed it
# (helm's testdata, pilot a1, 24 Oct). v2 lists symlinks as links (their readlink target,
# never followed) and paths missing from the worktree as such, then hashes the regular files
# in one batch; on a repo with no symlinks or missing paths its output equals v1's byte for
# byte. Boundaries record the version they used; a boundary without one is v1.
_FP_HEAD = ("cd /app && echo HEAD=$(git rev-parse HEAD)"
            " && git status --porcelain --untracked-files=all")
_FP_TAIL = (" && for f in /tmp/plan.md /tmp/base_commit.txt; do"
            " [ -f $f ] && sha256sum $f || echo missing $f; done")
FINGERPRINT_CMDS = {
    1: _FP_HEAD + " && git ls-files -z -co --exclude-standard | xargs -0 -r sha256sum" + _FP_TAIL,
    2: _FP_HEAD + (
        " && L=$(mktemp) && git ls-files -z -co --exclude-standard > \"$L\""
        # symlinks, as links (builtins only, readlink per link)
        " && xargs -0 -r bash -c 'for f; do if [ -L \"$f\" ]; then"
        " printf \"link %s -> %s\\n\" \"$f\" \"$(readlink -- \"$f\")\";"
        " elif [ ! -e \"$f\" ]; then printf \"missing %s\\n\" \"$f\"; fi; done' _ < \"$L\""
        # regular files, hashed in batches
        " && xargs -0 -r bash -c 'for f; do [ -L \"$f\" ] || [ ! -f \"$f\" ]"
        " || printf \"%s\\0\" \"$f\"; done' _ < \"$L\" | xargs -0 -r sha256sum --"
        " && rm -f \"$L\"") + _FP_TAIL,
}
# v3 (astra fingerprint r1): lossless. v2's `$(readlink …)` drops trailing newlines from a
# link target and its newline-separated records are ambiguous, so two different workspaces
# could share a v2 fingerprint. v3 writes NUL-terminated records only: `git status -z`; each
# symlink as "L <sha256 of the exact `readlink -n` target bytes> <raw path>"; each missing
# path as "M <raw path>"; each regular file as `sha256sum -z` ("<hash>  <raw path>", no
# escaping); the step files likewise; with a tag per section. The stream is hashed inside the
# container, so text decoding can't merge distinct paths either: the output is one line,
# "v3 <sha256 of the record stream>". v1 and v2 are kept byte for byte for boundaries that
# recorded them.
FINGERPRINT_CMDS[3] = (
    "set -o pipefail; cd /app && L=$(mktemp) && git ls-files -z -co --exclude-standard > \"$L\""
    " && H=$( {"
    " printf 'HEAD %s\\0' \"$(git rev-parse HEAD)\""
    " && printf 'SECTION status\\0' && git status --porcelain=v1 -z --untracked-files=all"
    " && printf 'SECTION links\\0'"
    " && xargs -0 -r bash -c 'for f; do if [ -L \"$f\" ]; then"
    " printf \"L %s %s\\0\" \"$(readlink -n -- \"$f\" | sha256sum | cut -c1-64)\" \"$f\";"
    " elif [ ! -e \"$f\" ]; then printf \"M %s\\0\" \"$f\"; fi; done; true' _ < \"$L\""
    " && printf 'SECTION files\\0'"
    " && { xargs -0 -r bash -c 'for f; do [ -L \"$f\" ] || [ ! -f \"$f\" ]"
    " || printf \"%s\\0\" \"$f\"; done; true' _ < \"$L\" | xargs -0 -r sha256sum -z --; }"
    " && printf 'SECTION tmp\\0'"
    " && for f in /tmp/plan.md /tmp/base_commit.txt; do"
    " if [ -f \"$f\" ]; then sha256sum -z -- \"$f\"; else printf 'M %s\\0' \"$f\"; fi; done;"
    " } | sha256sum | cut -c1-64 ); rc=$?; rm -f \"$L\"; [ $rc -eq 0 ] && [ ${#H} -eq 64 ]"
    " && echo \"v3 $H\"")
FINGERPRINT_VERSION = 3  # what new boundaries record (helm a2's process loaded 2)
FINGERPRINT_CMD = FINGERPRINT_CMDS[1]  # kept for older callers


def fingerprint(runner: Runner, version: int = FINGERPRINT_VERSION) -> str:
    rc, out, err = runner(FINGERPRINT_CMDS[version], 300)
    if rc != 0:
        raise RuntimeError(f"fingerprint v{version} failed rc={rc}: {err[-500:]}")
    return out


@dataclass
class Boundary:
    """What an arm needs: the agent's context, the workflow stage, the simulator, the image."""
    run_key: str
    stage: str  # "02_implement"
    image: str  # docker commit of the quiesced container (repo, untracked files, /tmp)
    agent_messages: list[dict]
    sim: dict  # UserSim.export()
    fingerprint_sha256: str
    meta: dict = field(default_factory=dict)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=1, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> Boundary:
        return cls(**json.loads(Path(path).read_text()))


def capture(run_key: str, stage: str, sb, inv, agent_messages: list[dict], sim: UserSim,
            stem: str) -> Boundary:
    """Quiesce check + docker commit (Sandbox.snapshot refuses a non-quiescent container), and
    the agent and simulator state as JSON."""
    fp = fingerprint(guarded_runner(sb))
    tag = inv.new_tag(stem)
    secs = sb.snapshot(tag)
    return Boundary(run_key, stage, tag, copy.deepcopy(agent_messages), sim.export(),
                    sha256(fp), {"snapshot_s": secs})


def tag_exists(tag: str, host: str | None = None) -> bool:
    """Whether an image tag exists on the daemon (the sandbox's host)."""
    from compaction.deepswe import sandbox as SB
    with SB.using_host(host):
        try:
            SB._docker(["image", "inspect", "--format", "{{.Id}}", tag], timeout=60)
            return True
        except SB.SandboxError:
            return False


CONTAINER_LIFETIME_S = 6 * 3600
PENDING_ASK = "(pending)"  # the tool result a captured ask_user gets; never shown to a model
PENDING_DEFERRED = "(not run yet)"  # a tool call queued after the captured ask (not executed)
TREE_CMD = ("t=$(mktemp); cp .git/index \"$t\" && GIT_INDEX_FILE=\"$t\" git add -A >/dev/null 2>&1"
            " && GIT_INDEX_FILE=\"$t\" git write-tree; rc=$?; rm -f \"$t\"; exit $rc")
SETUP_CMD = ("git config --global user.name Dev && git config --global user.email dev@example.com"
             " && git config --global --add safe.directory '*'")


@dataclass
class InteractEnv:
    """The env `compaction.s2.loop.Loop` (and `session.run_session`) drives for SWE-Interact: the
    agent's bash in the task container, and ask_user answered by this run's own simulator.

    No rules channel, no lures, no trap shims (nothing planted; plan "Setup"), so
    `trap_snapshot` is a no-op. In capture mode (the primary readout) ask_user does not call the
    simulator: it records the question, returns PENDING_ASK, and the runner's after_call hook
    ends the continuation at that call (`captured`). Tool calls after it in the same response
    are not run (astra build r1 #3): they return PENDING_DEFERRED and are listed in `deferred`,
    for the secondary to run after the answer.

    With `track`, each `trap_snapshot` (after every agent call) records the workspace as a git
    tree (`tree()`, a scratch index; the agent's index and refs are never touched), so the
    arm's diffs are measured from the fork and keep changes later reverted."""
    sb: Any
    sim: UserSim
    cmd_timeout_s: int = 180
    capture: bool = False
    captured: list[dict] = field(default_factory=list)  # [{"question"}]
    deferred: list[dict] = field(default_factory=list)  # [{"tool": "bash"|"ask_user", ...}]
    track: bool = False
    trees: list[dict] = field(default_factory=list)  # [{"label", "tree"}], on change only
    log: list[dict] = field(default_factory=list)
    uname: dict = field(default_factory=dict)
    exec_stats: dict = field(default_factory=dict)  # {"client_timeouts": n}, agent + simulator

    @classmethod
    def start(cls, inv, image: str, sim_factory: Callable[[Runner], UserSim], *, cpus: float = 2,
              memory_mb: int = 8192, platform: str | None = "linux/amd64",
              setup: bool = True) -> InteractEnv:
        """A container from `image` (the task image, or a kept boundary image) and its simulator,
        bound to that container only. `setup` sets the git identity (fresh task images)."""
        sb = inv.start(image, platform=platform, cpus=cpus, memory_mb=memory_mb,
                       network="none", lifetime_s=CONTAINER_LIFETIME_S)
        try:
            stats: dict = {}
            env = cls(sb, sim_factory(sandbox_runner(sb, stats)), exec_stats=stats)
            if setup:
                r = sb.exec(NOSHIM + SETUP_CMD, timeout_s=120)
                if r.rc != 0:
                    raise RuntimeError(f"git setup failed rc={r.rc}: {r.stderr[-500:]}")
            u = sb.exec("uname -s; uname -r; uname -v; uname -m", timeout_s=60)
            env.uname = dict(zip(("system", "release", "version", "machine"),
                                 u.stdout.splitlines(), strict=False))
        except BaseException:
            sb.close()
            raise
        return env

    def run_agent_command(self, cmd: str):
        from compaction.s2.env import Observation
        if self.captured:
            self.deferred.append({"tool": "bash", "cmd": cmd})
            return Observation(0, PENDING_DEFERRED, 0.0, False, False)
        t0 = time.monotonic()
        r = tolerant_exec(self.sb, "exec 2>&1\n" + cmd, timeout_s=self.cmd_timeout_s,
                          workdir=WORKSPACE, stats=self.exec_stats)
        out = r.stdout + r.stderr
        return Observation(r.rc, out, time.monotonic() - t0, r.timed_out, False)

    def ask_user(self, question: str, action: str | None = None) -> str:
        if self.capture:
            if self.captured:
                self.deferred.append({"tool": "ask_user", "question": question})
                return PENDING_DEFERRED
            self.captured.append({"question": question})
            return PENDING_ASK
        return self.sim.ask(question)

    def tree(self) -> str:
        """The workspace (tracked and untracked, non-ignored files) as a git tree sha."""
        r = self.sb.exec(NOSHIM + TREE_CMD, timeout_s=300)
        if r.rc != 0 or not r.stdout.strip():
            raise RuntimeError(f"write-tree failed rc={r.rc}: {r.stderr[-300:]}")
        return r.stdout.strip().splitlines()[-1]

    def trap_snapshot(self, label: str) -> float:
        if not self.track:
            return 0.0
        t0 = time.monotonic()
        t = self.tree()
        if not self.trees or self.trees[-1]["tree"] != t:
            self.trees.append({"label": label, "tree": t})
        return time.monotonic() - t0

    def diff_trees(self, a: str, b: str) -> str:
        r = self.sb.exec(NOSHIM + f"git diff --binary {shlex.quote(a)} {shlex.quote(b)}",
                         timeout_s=300)
        return r.stdout

    def task_patch(self) -> str:
        """Committed work since /tmp/base_commit.txt (what the hidden tests grade)."""
        return self.sb.exec(NOSHIM + "git diff --binary \"$(cat /tmp/base_commit.txt)\" HEAD",
                            timeout_s=300).stdout

    def quiesce(self) -> list[str]:
        """Stop background processes the agent left (as TaskEnv.quiesce); returns them."""
        stray = self.sb.stray_processes()
        if stray:
            pids = " ".join(line.split()[0] for line in stray)
            self.sb.exec(f"kill -TERM {pids} 2>/dev/null; sleep 2; kill -KILL {pids} 2>/dev/null;"
                         " true", timeout_s=60, workdir="/")
            left = self.sb.stray_processes()
            if left:
                raise RuntimeError(f"processes survived quiescing: {left}")
        self.log.append({"op": "quiesce", "stopped": stray})
        return stray

    def boundary(self, keep, k: int) -> dict:
        """session.SessionEnv.boundary: quiesce, then `docker commit` to a kept tag (labelled by
        the KeepStore, not by the run, so the run's cleanup keeps it). keep=None (arms): quiesce
        only, tag None."""
        stopped = self.quiesce()
        if keep is None:
            return {"tag": None, "stopped_processes": stopped, "snapshot_s": 0.0}
        tag = keep.new_tag(k)
        if tag_exists(tag, self.sb.host):
            # never re-point an existing kept tag (24 Oct incident)
            raise RuntimeError(f"kept tag {tag} already exists; refusing to overwrite it")
        secs = self.sb.snapshot(tag, labels=keep.labels())
        keep.record(k, tag, snapshot_s=secs)
        return {"tag": tag, "stopped_processes": stopped, "snapshot_s": secs}

    def fingerprint(self, version: int = FINGERPRINT_VERSION) -> str:
        return fingerprint(guarded_runner(self.sb), version)

    def read_text(self, path: str) -> str | None:
        r = self.sb.exec(NOSHIM + f"cat {shlex.quote(path)}", timeout_s=60, workdir="/")
        return r.stdout if r.rc == 0 else None

    def diff(self) -> str:
        """git diff from /tmp/base_commit.txt (else HEAD) to the working tree, untracked
        (non-ignored) files included, through a scratch index."""
        r = self.sb.exec(NOSHIM + (
            "b=$(cat /tmp/base_commit.txt 2>/dev/null || git rev-parse HEAD);"
            " t=$(mktemp); cp .git/index \"$t\";"  # a scratch index: the agent's is never touched
            " GIT_INDEX_FILE=\"$t\" git add -A >/dev/null 2>&1;"
            " GIT_INDEX_FILE=\"$t\" git diff --cached --binary \"$b\"; rm -f \"$t\""),
            timeout_s=300)
        return r.stdout

    def close(self) -> bool:
        return self.sb.close()


def agent_request(messages: list[dict], tools: list[dict]) -> str:
    """The agent's next request body (what our OpenAI-compatible model sends), canonical."""
    return canon({"messages": messages, "tools": tools})
