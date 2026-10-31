"""Experiment 1's precedent in Codex (week-2 harness replication, plan v0.3.1). The Codex twin of
`cc_chain` (Claude Code), whose pieces it reuses: `install_chain`, `ScriptedSim`, the task
prompts, `env_checks`, `turns.decide`, and experiment 1's run-dir format.

One history = a first task in A's container and a second task in B's container, one Codex
thread carried across:

- **Containers:** the pilot's `NativeSession` (Codex CLI volume, internal network, egress
  sidecar with the Codex allowlist, the `ask_user` MCP answered by `ScriptedSim`), with
  experiment 1's remote, git wrapper and scorer installed (`cc_chain.install_chain`). The
  ChatGPT-plan `auth.json` sits on the secrets tmpfs, as in the pilot; there is no API key.
- **The CLI:** `codex exec --json` (`cli_command`), `gpt-6-luna` at the pilot's effort,
  permissions and sandbox bypassed, web search off, memories off, no hooks. Auto-compaction
  can't fire inside a task: `-c model_context_window=872000 -c
  model_auto_compact_token_limit=860000` (28 Oct: effective window 828,400, under the limit).
  Every call pins `-m gpt-6-luna` (a resume otherwise defaults to another model).
- **User turns** (`turns.decide`): `codex exec` has no turn cap, so a stop is the end of one
  `codex exec` / `codex exec resume` call (`parse_stop`): its last agent message, whether it
  called `ask_user`, whether a command's output starts with the submit token. The reply
  (a check-in, "No reply yet.") is the next resume's prompt; `ask_user` itself answers
  "No reply yet." (`ScriptedSim`).
- **Caps per task:** experiment 1's 400 calls and 2 h. Time: each call gets only the task's
  remaining time. Calls: an in-container watcher (`WATCH_SCRIPT`) counts the rollout's
  per-call `token_count` events every `WATCH_EVERY_S` seconds and stops the CLI at the cap,
  leaving a flag the driver reads ("capped_calls"). Codex is $0 on the plan; the plan quota
  (`rate_limits.primary.used_percent`) is recorded before and after each task.
- **Boundary** (`boundary_compact`): after task 1, `codex app-server` (JSON-RPC over stdio,
  in A's container): `initialize`, `initialized`, `thread/resume` (model pinned),
  `thread/compact/start`, then `turn/completed` after an `item/completed` of type
  `contextCompaction`. Whatever the RPC said, the server is stopped and the rollout reread:
  one new `compacted` line is success; a confirmed zero allows one retry; anything else is a
  recorded deviation, never another compaction; task 2 then runs uncompacted.
- **Transfer** (`export_session` / `import_session`): only the thread's rollout file, at the
  same path under B's `CODEX_HOME/sessions/` (no auth, no memories, no history, no state DB;
  the pilot's recitations resumed forked rollouts in fresh homes the same way:
  `codex.fork_rollout`, `codex.Reciter`). Task 2 is `codex exec resume -m gpt-6-luna <thread>`
  with experiment 1's lead-in and the task-2 prompt (`cc_chain.lead_and_task`).
- **Endings:** `cc_chain`'s ("submitted", "stalled", "stopped", "failed", "capped_calls",
  "capped_time", "capped_context"), plus "quota_refused" when the plan refuses. Every ending
  writes `result.json`, in experiment 1's format via `write_run`, so `precedent.first_stage`
  and `score.score_b` score it unchanged; `messages.json` comes from
  `cx_transcript.score_messages`. `extra.primary_unknown_reason` makes `score_b`'s primary
  unknown (quota, uncollectable secrets, a task 2 not verifiably started from the carried
  thread, a bootstrap failure); the evidence is kept.
- **Credentials:** raw logs are staged privately and kept only redacted; the current tokens
  are collected fail-closed, and if they can't be, result.json keeps only allowlisted
  metadata (`safe_extra`) and no evidence is exported; a rollout holding a token is never
  transferred (`_task`). The scorer's evidence (`finish_traps`' snapshot, logs, remote and
  git hooks/config) is staged and screened inside the container and reaches the host only on
  a clean verdict (`screened_evidence`); otherwise `traps_home` is None and the reason
  recorded.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from compaction.deepswe import sandbox as SB
from compaction.s2.chain import config as CC
from compaction.s2.chain import env as E
from compaction.s2.native import cc_chain as X
from compaction.s2.native import codex as CX
from compaction.s2.native import common as N
from compaction.s2.native import cx_transcript as TR
from compaction.s2.native import turns as T

ARM = "cx_chain"
MODEL = CX.MODEL  # gpt-6-luna
EFFORT = CX.EFFORT  # the pilot's
CONTEXT_WINDOW, AUTO_COMPACT_LIMIT = 872_000, 860_000
EXPECTED_WINDOW = 828_400  # what Codex reports for CONTEXT_WINDOW (verified 28 Oct)
OVERRIDES = (("model_context_window", CONTEXT_WINDOW),
             ("model_auto_compact_token_limit", AUTO_COMPACT_LIMIT))
CONTINUE_PROMPT = CX.CONTINUE_PROMPT  # an infrastructure resume: the CLI needs some prompt
MAX_INFRA = X.MAX_INFRA
END_SUBMITTED, END_STALLED, END_STOPPED, END_FAILED = (X.END_SUBMITTED, X.END_STALLED,
                                                       X.END_STOPPED, X.END_FAILED)
END_CALLS, END_TIME, END_CONTEXT = X.END_CALLS, X.END_TIME, X.END_CONTEXT
END_QUOTA = "quota_refused"
OVERFLOW = ("context window", "context_length_exceeded", "maximum context length")
QUOTA = ("usage limit", "usage_limit", "rate limit reached", "rate_limit_reached")
SESSIONS = f"{CX.HOME_C}/sessions"
ROLLOUT_GLOB = f"{SESSIONS}/*/*/*/rollout-*.jsonl"
WATCH = "/opt/emx/cx-watch"  # the call watcher's dir: script, flag, stop file
WATCH_EVERY_S = 2
CALL_PATTERN = '"last_token_usage":{"input_tokens":[1-9]'  # one per API call (raw rollout)
COMPACT_TIMEOUT_S = 900
RPC_TIMEOUT_S = 120


# ------------------------------------------------------------------------------------------
# The CLI
# ------------------------------------------------------------------------------------------


def overrides() -> list[str]:
    return [x for k, v in OVERRIDES for x in ("-c", f"{k}={v}")]


def cli_command(*, resume: str | None = None) -> str:
    """The in-container `codex exec` line (no secrets); the prompt comes on stdin ("-")."""
    parts = [CX.CLI, "exec"]
    if resume:
        parts += ["resume", resume]
    parts += ["--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--json",
              "-m", MODEL, *overrides()]
    if not resume:
        parts += ["-C", "/app"]
    parts += ["--", "-"]
    return "unset OPENAI_API_KEY CODEX_API_KEY OPENAI_BASE_URL; cd /app && " + shlex.join(parts)


def app_server_command() -> str:
    return ("unset OPENAI_API_KEY CODEX_API_KEY OPENAI_BASE_URL; cd /app && exec "
            + shlex.join([CX.CLI, "app-server", *overrides()]))


def cli_config() -> dict:
    """config.toml: the pilot's (model, effort, web search off, the `ask_user` MCP with its
    token by env var), memories off; no hooks, no auto-compaction setting (OVERRIDES)."""
    cfg = CX.CodexConfig(model=MODEL, effort=EFFORT).config(N.MCP_URL, N.TOKEN_HEADER)
    cfg["features"] = {"memories": False}
    return cfg


def install_cli_config(sb: SB.Sandbox, auth_bytes: bytes) -> None:
    """CODEX_HOME with config.toml, and auth.json on the secrets tmpfs (linked), as the pilot's
    `codex.install_config` without its compaction hook."""
    r = sb.exec(f"mkdir -p {SESSIONS} {CX.SECRETS_C} && chmod 700 {CX.SECRETS_C}",
                timeout_s=60, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"codex home: {r.stderr[-300:]}")
    sb.put_file(CX.toml_dumps(cli_config()).encode(), f"{CX.HOME_C}/config.toml")
    sb.put_file(auth_bytes, f"{CX.SECRETS_C}/auth.json")
    r = sb.exec(f"chmod 600 {CX.SECRETS_C}/auth.json && "
                f"ln -sf {CX.SECRETS_C}/auth.json {CX.HOME_C}/auth.json && "
                f"mount | grep -q ' {CX.SECRETS_C} type tmpfs'", timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"codex config (is {CX.SECRETS_C} a tmpfs?): {r.stderr[-300:]}")


def cli_env(ns: N.NativeSession) -> dict[str, str]:
    return CX.cli_env(ns.egress, ns.server)


def parse_stop(lines: list[str], infra: bool = False) -> tuple[T.Stop, dict]:
    """One call's `--json` event lines -> the stop `turns.decide` reads, and what else they
    showed: the thread id, the turn's usage, error texts, a context overflow, a plan refusal.
    A call without `turn.completed` is an infrastructure failure unless it overflowed or hit
    the quota (the driver checks the call cap and the deadline first)."""
    asked = marker = completed = False
    text, tid, usage, errors = "", None, None, []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        it = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if t == "thread.started":
            tid = ev.get("thread_id")
        elif t in ("item.started", "item.completed"):
            if it.get("type") == "mcp_tool_call" and str(it.get("tool", "")).endswith(
                    "ask_user"):
                asked = True
            if t == "item.completed" and it.get("type") == "agent_message":
                text = it.get("text") or ""
            if t == "item.completed" and it.get("type") == "command_execution":
                out = str(it.get("aggregated_output") or "")
                if out.lstrip().splitlines()[:1] == [T.SUBMIT_MARKER]:
                    marker = True
        elif t == "turn.completed":
            completed, usage = True, ev.get("usage")
        elif t in ("turn.failed", "error"):
            errors.append(json.dumps(ev)[:2000])
    blob = " ".join(errors).lower()
    overflow = not completed and any(o in blob for o in OVERFLOW)
    quota = not completed and not overflow and any(q in blob for q in QUOTA)
    failed = infra or not (completed or overflow or quota)
    info = {"thread_id": tid, "usage": usage, "errors": errors[-5:], "overflow": overflow,
            "quota": quota, "turn_completed": completed, "cap": None}
    return T.Stop(text=text, asked_user=asked, marker=marker, infra_failed=failed), info


# ------------------------------------------------------------------------------------------
# The call cap, inside the container
# ------------------------------------------------------------------------------------------

WATCH_SCRIPT = f"""#!/bin/sh
# Stops the CLI once the rollout holds $1 API calls (per-call token_count events), leaving the
# count in {WATCH}/capped. Exits when {WATCH}/stop exists.
lim="$1"
while [ ! -f {WATCH}/stop ]; do
  m=$(cat {ROLLOUT_GLOB} 2>/dev/null | grep -c '{CALL_PATTERN}')
  if [ "${{m:-0}}" -ge "$lim" ]; then
    echo "$m" > {WATCH}/capped
    pkill -TERM -f '^{CX.CLI} exec'; sleep 3; pkill -KILL -f '^{CX.CLI} exec'
  fi
  sleep {WATCH_EVERY_S}
done
"""


def start_watch(sb: SB.Sandbox, limit: int) -> None:
    """The call watcher, detached, bounded by the task's lifetime."""
    te = N.tolerant_exec(sb)
    r = te(f"rm -rf {WATCH} && mkdir -p {WATCH}", timeout_s=60, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"watch dir: rc={r.rc} {r.stderr[-200:]}")
    sb.put_file(WATCH_SCRIPT.encode(), f"{WATCH}/watch.sh")
    inner = f"timeout -k 5 {CC.LIFETIME_S} sh {WATCH}/watch.sh {int(limit)}"
    r = te(f"setsid nohup {inner} > /dev/null 2>&1 < /dev/null &", timeout_s=60, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"watch launch rc={r.rc}: {r.stderr[-300:]}")


def stop_watch(sb: SB.Sandbox) -> None:
    N.tolerant_exec(sb)(f"touch {WATCH}/stop; pkill -f '[s]h {WATCH}/watch.sh' ; true",
                        timeout_s=60, workdir="/")


def watch_capped(sb: SB.Sandbox) -> int | None:
    r = N.tolerant_exec(sb)(f"cat {WATCH}/capped 2>/dev/null; true", timeout_s=60, workdir="/")
    v = r.stdout.strip()
    return int(v) if v.isdigit() else None


# ------------------------------------------------------------------------------------------
# Rollouts in the container
# ------------------------------------------------------------------------------------------


def rollout_path(sb: SB.Sandbox, tid: str) -> str:
    hits = [p for p in CX.container_rollouts(sb) if p.endswith(f"-{tid}.jsonl")]
    if len(hits) != 1:
        raise SB.SandboxError(f"{len(hits)} rollouts for thread {tid}")
    return hits[0]


def only_thread(sb: SB.Sandbox) -> str | None:
    """The thread id of the container's one rollout (a first call whose events were lost)."""
    paths = CX.container_rollouts(sb)
    if len(paths) != 1:
        return None
    name = Path(paths[0]).name.removesuffix(".jsonl")
    tid = name[-36:]
    return tid if len(tid) == 36 and tid.count("-") == 4 else None


def parse_rollout(data: bytes) -> list[dict]:
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


def read_rollout(sb: SB.Sandbox, tid: str) -> tuple[str, bytes, list[dict]]:
    p = rollout_path(sb, tid)
    data = sb.read_file(p, timeout_s=300)
    return p, data, parse_rollout(data)


def n_compacted(lines: list[dict]) -> int:
    return len(CX.compaction_lines(lines))


# ------------------------------------------------------------------------------------------
# One task's driving loop
# ------------------------------------------------------------------------------------------


@dataclass
class TaskRun:
    """One task's driving loop in a live session; the caps are the driver's."""

    ns: N.NativeSession
    out: Path
    deadline: float  # time.monotonic() value
    thread: str | None = None
    turns: list[dict] = field(default_factory=list)
    state: T.State = field(default_factory=T.State)

    def left_s(self) -> float:
        return self.deadline - time.monotonic()

    def call(self, cmd: str, stdin: str, k: int) -> tuple[T.Stop, dict]:
        """One headless CLI call, given only the task's remaining time."""
        left = int(self.left_s())
        if left <= 0:
            return T.Stop(text=""), {"thread_id": None, "cap": END_TIME, "overflow": False,
                                     "quota": False, "turn_completed": False}
        p = self.out / f"turn_{k:03d}.jsonl"
        r = N.detached_exec(self.ns.sb, cmd, name=f"turn-{k}-{uuid.uuid4().hex[:6]}",
                            timeout_s=left, log_path=p, env=cli_env(self.ns),
                            stdin=stdin.encode())
        lines = p.read_text().splitlines() if p.exists() else []
        stop, info = parse_stop(lines, infra=r.killed in ("container_gone", "output_lost"))
        info |= {"rc": r.rc, "seconds": r.seconds, "killed": r.killed}
        capped = None
        with contextlib.suppress(Exception):
            capped = watch_capped(self.ns.sb)
        if capped is not None:
            info["cap"], info["calls_at_cap"] = END_CALLS, capped
        elif (r.timed_out or r.timed_out_by_cli) and self.left_s() <= 5:
            info["cap"] = END_TIME
        return stop, info

    def drive(self, first: str, *, resume: str | None) -> str:
        """Runs the task to its end; returns its end value. `resume`: the thread to continue
        (task 2), else a new thread (task 1)."""
        k, prompt, infra_run = len(self.turns), first, 0
        self.thread = resume
        while True:
            if self.left_s() <= 0:
                return END_TIME
            stop, info = self.call(cli_command(resume=self.thread), prompt, k)
            if self.thread is None:
                self.thread = info.get("thread_id") or only_thread(self.ns.sb)
            elif info.get("thread_id") and info["thread_id"] != self.thread:
                info["thread_mismatch"] = True
            d = T.decide(stop, self.state)
            self.turns.append({"k": k, "stop": stop.__dict__, "info": info,
                               "decision": d.__dict__})
            k += 1
            if info.get("cap"):
                return info["cap"]
            if info.get("overflow"):
                return END_CONTEXT
            if info.get("quota"):
                return END_QUOTA
            if d.kind == "infra_resume":
                infra_run += 1
                if infra_run > MAX_INFRA:
                    return END_FAILED
                # no thread yet: start again with the task prompt; else resume with no new text
                prompt = first if self.thread is None else CONTINUE_PROMPT
                continue
            infra_run = 0
            if d.kind == "complete":
                return END_SUBMITTED
            if d.kind == "stalled":
                return END_STALLED
            if d.kind == "exhausted":
                return END_STOPPED
            prompt = d.text


# ------------------------------------------------------------------------------------------
# The boundary: one manual compaction through `codex app-server`
# ------------------------------------------------------------------------------------------


class RpcError(RuntimeError):
    pass


class AppServer:
    """A JSON-RPC 2.0 client over a process's stdio (one JSON object per line). Every message
    the server sent is kept in `messages`; a server-to-client request is answered with an
    error (nothing here grants approvals) and recorded."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.messages: list[dict] = []
        self.server_requests: list[dict] = []
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._id = 0
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            with self._lock:
                self.messages.append(m)
            if "method" in m and "id" in m:  # the server asks us something: refuse it
                self.server_requests.append({"id": m["id"], "method": m["method"]})
                with contextlib.suppress(Exception):
                    self._send({"jsonrpc": "2.0", "id": m["id"],
                                "error": {"code": -32601, "message": "not supported"}})
            self._q.put(m)
        self._q.put(None)  # EOF

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        with self._lock:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict, timeout_s: float = RPC_TIMEOUT_S) -> dict:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        m = self.wait(lambda m: m.get("id") == rid and ("result" in m or "error" in m)
                      and "method" not in m, timeout_s, what=method)
        if "error" in m:
            raise RpcError(f"{method}: {json.dumps(m['error'])[:500]}")
        return m["result"]

    def wait(self, pred, timeout_s: float, what: str = "message") -> dict:
        """The first message (already received or arriving) that satisfies `pred`."""
        with self._lock:
            for m in self.messages:
                if pred(m):
                    return m
        end = time.monotonic() + timeout_s
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no {what} in {timeout_s:.0f}s")
            try:
                m = self._q.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if m is None:
                raise RpcError(f"app-server closed its output while waiting for {what}")
            if pred(m):
                return m

    def close(self, grace_s: float = 10) -> None:
        with contextlib.suppress(Exception):
            assert self.proc.stdin is not None
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=grace_s)


def compact_thread(rpc: AppServer, tid: str, timeout_s: float = COMPACT_TIMEOUT_S) -> dict:
    """The verified sequence (28 Oct): initialize, initialized, thread/resume (model pinned),
    thread/compact/start, then turn/completed after the contextCompaction item completed.
    Returns what was seen: the compaction turn's status and the rate limits reported."""
    rpc.request("initialize", {"clientInfo": {"name": "emx-cx-chain", "version": "0.1"}})
    rpc.notify("initialized")
    res = rpc.request("thread/resume", {"threadId": tid, "model": MODEL, "cwd": "/app"})
    resumed = ((res or {}).get("thread") or {}).get("id")
    if resumed != tid:
        raise RpcError(f"thread/resume returned thread {resumed}, not {tid}")
    rpc.request("thread/compact/start", {"threadId": tid})

    def params(m):
        return m.get("params") if isinstance(m.get("params"), dict) else {}

    def is_compaction(m):
        p = params(m)
        return (m.get("method") == "item/completed" and p.get("threadId") == tid
                and (p.get("item") or {}).get("type") == "contextCompaction")

    item = rpc.wait(is_compaction, timeout_s, what="contextCompaction item")
    after = rpc.messages.index(item)
    done = rpc.wait(lambda m: m.get("method") == "turn/completed"
                    and params(m).get("threadId") == tid and rpc.messages.index(m) > after,
                    timeout_s, what="turn/completed")
    turn = params(done).get("turn") or {}
    limits = [params(m).get("rateLimits") for m in rpc.messages
              if m.get("method") == "account/rateLimits/updated"]
    return {"turn_status": turn.get("status"), "turn_error": turn.get("error"),
            "item_id": params(item).get("item", {}).get("id"),
            "rate_limits": limits[-1] if limits else None,
            "methods": sorted({m["method"] for m in rpc.messages if "method" in m})}


def app_server(sb: SB.Sandbox, env: dict[str, str], timeout_s: int) -> AppServer:
    """`codex app-server` on stdio inside the container, under an in-container timeout; env
    values reach it as `docker exec -e NAME` (values only in the client's environment)."""
    argv = SB.docker_argv(["exec", "-i", *[a for k in env for a in ("-e", k)], "-w", "/app",
                           sb.name, "timeout", "-k", "5", str(int(timeout_s)), "bash", "-lc",
                           app_server_command()], sb.host)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env={**os.environ, **env})
    return AppServer(proc)


def stop_app_server(sb: SB.Sandbox) -> bool:
    """Stops any `codex app-server` in the container; True only when none is left running."""
    te = N.tolerant_exec(sb)
    te(f"pkill -TERM -f '^{CX.CLI} app-server' ; sleep 2; "
       f"pkill -KILL -f '^{CX.CLI} app-server' ; true", timeout_s=60, workdir="/")
    r = te(f"if pgrep -f '^{CX.CLI} app-server' > /dev/null; then echo RUNNING; "
           "else echo STOPPED; fi", timeout_s=60, workdir="/")
    return r.rc == 0 and r.stdout.strip() == "STOPPED"


def run_compaction(sb: SB.Sandbox, env: dict[str, str], tid: str, log: Path) -> dict:
    """One app-server session that compacts `tid`. Never raises: returns {"seen" (what
    `compact_thread` saw, or None), "error", "stopped" (the server, client and in-container
    process, confirmed gone)}. Every message the server sent goes to `log` (a raw staging file,
    redacted before it is kept)."""
    rpc, seen, err = None, None, None
    try:
        rpc = app_server(sb, env, COMPACT_TIMEOUT_S + 60)
        seen = compact_thread(rpc, tid)
    except Exception as e:  # noqa: BLE001 - the rollout, not the RPC, decides what happened
        err = f"{type(e).__name__}: {e}"[:500]
    finally:
        if rpc is not None:
            with contextlib.suppress(Exception):
                rpc.close()
        stopped = False
        with contextlib.suppress(Exception):
            stopped = stop_app_server(sb)
        if rpc is not None:
            with contextlib.suppress(Exception), open(log, "a") as f:
                f.writelines(json.dumps(m) + "\n" for m in rpc.messages)
    return {"seen": seen, "error": err, "stopped": stopped}


def boundary_compact(run: TaskRun, tid: str) -> dict:
    """One manual compaction (astra cx-build r1 #1). After every attempt, whatever the RPC
    said, the server is stopped and the rollout reread: one new `compacted` line is success;
    zero new (confirmed, with the server gone) allows the one retry; more than one, fewer, a
    server not confirmed stopped, or an unreadable rollout is a recorded deviation, never
    another compaction. Task 2 then runs uncompacted."""
    out: dict = {"attempts": []}
    try:
        _p, _d, lines = read_rollout(run.ns.sb, tid)
        before = n_compacted(lines)
    except Exception as e:  # noqa: BLE001 - reported, and task 2 still runs uncompacted
        return out | {"ok": False, "before": None,
                      "deviation": f"rollout unreadable before the boundary: {e}"[:300]}
    out["before"] = before
    for attempt in (1, 2):
        rec = {"attempt": attempt, **run_compaction(
            run.ns.sb, cli_env(run.ns), tid, run.out / f"boundary_rpc_{attempt}.jsonl")}
        out["attempts"].append(rec)
        if not rec["stopped"]:
            return out | {"ok": False, "deviation": f"attempt {attempt}: app-server not "
                          "confirmed stopped; compactions unverifiable"}
        try:
            _p, _d, lines = read_rollout(run.ns.sb, tid)
            after = n_compacted(lines)
        except Exception as e:  # noqa: BLE001
            rec["verify_error"] = f"{type(e).__name__}: {e}"[:300]
            return out | {"ok": False, "deviation": f"attempt {attempt}: rollout unreadable "
                          "after it; compactions unverifiable"}
        rec["after"] = after
        if after == before + 1:
            return out | {"ok": True, "boundaries": after}
        if after != before:
            return out | {"ok": False, "deviation": f"{after - before} compactions"}
    return out | {"ok": False, "deviation": "compaction failed twice; task 2 uncompacted"}


# ------------------------------------------------------------------------------------------
# The session transfer
# ------------------------------------------------------------------------------------------


def rel_path(path: str) -> str:
    """A container rollout path relative to CODEX_HOME (sessions/YYYY/MM/DD/rollout-...)."""
    if not path.startswith(SESSIONS + "/"):
        raise ValueError(f"{path} is not under {SESSIONS}")
    return path[len(CX.HOME_C) + 1:]


class Contaminated(RuntimeError):
    """The rollout holds a credential: it is never transferred (astra cx-build r1 #2)."""


def export_session(sb: SB.Sandbox, tid: str, secrets: list[str]) -> tuple[dict, dict]:
    """Only the thread's rollout file (B7) and its manifest, scanned first: a rollout holding
    any of `secrets` raises `Contaminated` and nothing is exported."""
    if not secrets:
        raise ValueError("no secrets to scan for")
    p, data, lines = read_rollout(sb, tid)
    _clean, n = redact_count(data, secrets)
    if n:
        raise Contaminated(f"the rollout holds {n} credential occurrence(s)")
    rel = rel_path(p)
    man = {"files": [{"path": rel, "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()}],
           "thread_id": tid, "lines": len(lines), "compactions": n_compacted(lines),
           "api_calls": TR.task_usage(lines)["n_calls"]}
    return {"rel": rel, "data": data, "thread_id": tid}, man


def import_session(sb: SB.Sandbox, session: dict) -> None:
    sb.put_file(session["data"], f"{CX.HOME_C}/{session['rel']}")
    if rollout_path(sb, session["thread_id"]) != f"{CX.HOME_C}/{session['rel']}":
        raise SB.SandboxError("the imported rollout is not where it was written")


def home_listing(sb: SB.Sandbox) -> list[str]:
    """CODEX_HOME's top level (no memories dir, no stray state expected to matter)."""
    r = sb.exec(f"ls -1A {CX.HOME_C} 2>/dev/null; true", timeout_s=60, workdir="/")
    return sorted(x for x in r.stdout.splitlines() if x.strip())


def egress_summary(events: list[dict]) -> dict:
    """Connections and refusals per host from the sidecar's log."""
    out: dict = {}
    for e in events:
        if e.get("ev") in ("connect", "deny"):
            k = f"{e['ev']} {e.get('host') or e.get('reason')}"
            out[k] = out.get(k, 0) + 1
    return out


def collect_secrets(sb: SB.Sandbox, startup: list[str]) -> list[str]:
    """The startup tokens plus the container's current ones (Codex may have refreshed them).
    Fails closed: raises when the current tokens can't be read (`codex.current_tokens`)."""
    current = CX.current_tokens(sb)
    return sorted({*startup, *current}, key=len, reverse=True)


def persist_redacted(staging: Path, out: Path, secrets: list[str]) -> int:
    """Every raw file of the task's private staging dir (turn logs, their stderr, RPC logs),
    redacted, into `out`. Returns the occurrences replaced."""
    n = 0
    for f in sorted(staging.iterdir()):
        if f.is_file():
            clean, k = redact_count(f.read_bytes(), secrets)
            (out / f.name).write_bytes(clean)
            n += k
    return n


def redact_count(data: bytes, secrets: list[str]) -> tuple[bytes, int]:
    n = 0
    for s in secrets:
        if s:
            b = s.encode()
            n += data.count(b)
            data = data.replace(b, b"<redacted>")
    return data, n


# ------------------------------------------------------------------------------------------
# Evidence: staged and screened inside the container before anything reaches the host
# ------------------------------------------------------------------------------------------

EVIDENCE_DIR = "/tmp/emx-cx-evidence"
SECRET_ENV = CX.SECRET_ENV  # EMX_CODEX_SECRET_<i>: values only in the docker client's env
SCREEN_TRIES = 2


def screen_script(root: str, n_secrets: int, shm: str = "/dev/shm") -> str:
    """The in-container screen of the staged evidence at `root` (bash). It follows agy's
    reviewed screen (`agy_chain.evidence_screen_script`), kept here with Codex's own patterns
    rather than imported, as that function's signature is still changing. The patterns are the
    `n_secrets` tokens in `$EMX_CODEX_SECRET_<i>` (startup and current, collected fail-closed)
    plus `$EMX_SIM_TOKEN`, written to a pattern file on `shm` (an empty pattern is an error:
    it would match everything). The steps: every `.tgz` unpacked, every path name and symlink
    target grepped (a secret in an archive member's name, astra cx-build r3 #2), grep -rlF
    over every file,
    then every git dir's objects decoded (`cat-file --batch-all-objects --batch` streamed into
    grep -c, as compressed objects hide text from grep), with both pipe stages' exit codes
    checked. Exit 0 + SCREEN_HIT, 1 + SCREEN_CLEAN, anything else an error (never clean)."""
    if n_secrets < 1:
        raise ValueError("no secrets to screen for")
    r = shlex.quote(root)
    pats = " ".join(f'"${SECRET_ENV}_{i}"' for i in range(n_secrets))
    return (
        "set -o pipefail; fail() { rm -f \"$P\"; exit $1; }; "
        f"P=$(mktemp -p {shlex.quote(shm)}) || exit 3; "
        f"printf '%s\\n' {pats} \"$EMX_SIM_TOKEN\" > \"$P\" || fail 5; "
        "grep -qx '' \"$P\" && fail 5; hit=0; "
        f"for t in $(find {r} -type f -name '*.tgz'); do "
        "mkdir -p \"$t.x\" && tar xzf \"$t\" -C \"$t.x\" || fail 6; done; "
        f"s=$(find -H {r} -print \\( -type l -exec readlink {{}} + \\) 2>/dev/null "  # names
        "| grep -cF -f \"$P\"; echo \"${PIPESTATUS[0]} ${PIPESTATUS[1]}\"); "  # (r3 #2)
        "set -- $s; [ \"$2\" = 0 ] || fail 6; [ \"$3\" -gt 1 ] && fail 4; "
        "[ \"$1\" -gt 0 ] && { echo NAMES; hit=1; }; "
        f"grep -rlF -f \"$P\" -- {r}; g=$?; [ $g -gt 1 ] && fail 4; [ $g -eq 0 ] && hit=1; "
        f"for o in $(find {r} -type d -name objects); do d=${{o%/objects}}; "
        "[ -f \"$d/HEAD\" ] || continue; "
        "s=$(git -c safe.directory='*' --git-dir=\"$d\" cat-file --batch-all-objects --batch "
        "2>/dev/null | grep -cF -f \"$P\"; echo \"${PIPESTATUS[0]} ${PIPESTATUS[1]}\"); "
        "set -- $s; [ \"$2\" = 0 ] || fail 6; [ \"$3\" -gt 1 ] && fail 4; "
        "[ \"$1\" -gt 0 ] && { echo \"$d\"; hit=1; }; done; rm -f \"$P\"; "
        "if [ $hit -eq 1 ]; then echo SCREEN_HIT; exit 0; fi; echo SCREEN_CLEAN; exit 1")


def snapshot_in_memory(env, label: str) -> dict:
    """`ChainEnv.trap_snapshot`'s in-container step (the scorer's snapshot program on stdin)
    with its output kept in memory instead of written to the host home."""
    cfg = json.loads((env.home / "config.json").read_text())
    gs = env.home / "state" / "git_setup.json"
    req = {"sources": {m: (E.TRAPS_SRC / f"{m}.py").read_text() for m in E.SNAP_MODULES},
           "order": list(E.SNAP_MODULES), "label": label, "workspace": cfg["workspace"],
           "bare": env.bare if cfg.get("push_log_available") else None,
           "soft": list(cfg.get("snapshot_extra_ignore") or []),
           "force": list(cfg.get("snapshot_force_include") or []),
           "base_tips": (json.loads(gs.read_text()).get("commit_tips")
                         if gs.exists() else None)}
    offsets = env.log_offsets()  # before the capture, as trap_snapshot does
    args = E.SBX.exec_args(env.sb.name, E.NOSHIM + "exec python3 -", 600, "/",
                           interactive=True)
    r = env.sb._guarded(args, timeout=600 + E.SBX.CLIENT_GRACE_S,
                        input_bytes=(E.SNAP_PY + "\n__REQ__ = " + repr(json.dumps(req))
                                     + "\nmain(__REQ__)\n").encode())
    if r.returncode != 0:
        raise SB.SandboxError(f"snapshot {label} rc={r.returncode}")
    out = json.loads(r.stdout.decode())
    out["fs"]["log_offsets"] = offsets
    return out


def stage_in_container(env, label: str, snap: dict, now: dict[str, list[str]],
                       other: list[str]) -> dict:
    """Everything `finish_traps` would export, staged under EVIDENCE_DIR in the container:
    the snapshot's state files and this container's log lines (from memory), the remote's
    archive and the workspace's git hooks/config archive (`export_traps_home` and `git_meta`'s
    in-container steps). Returns the git_meta facts (git dir, which of hooks/config exist)."""
    sb, d = env.sb, EVIDENCE_DIR
    env.harness(f"rm -rf {d} && mkdir -p {d}/state {d}/logs", check=True, workdir="/")
    for kind in ("fs", "git"):
        sb.put_file(json.dumps(snap[kind], indent=1, sort_keys=True).encode(),
                    f"{d}/state/{kind}_{label}.json")
    for k in E.LOG_NAMES:
        sb.put_file("".join(x + "\n" for x in now.get(k, [])).encode(), f"{d}/logs/{k}.jsonl")
    sb.put_file("".join(x + "\n" for x in other).encode(), f"{d}/logs/container_other.txt")
    parent, name = env.bare.rsplit("/", 1)
    env.harness(f"tar czf {d}/remote.tgz -C {parent} {name}", check=True, workdir="/")
    # the git hooks/config archive is best-effort, as in finish_traps (its failure never
    # withholds the scored evidence); a partial archive is removed, so the hashes stay exact
    gitdir, have = None, []
    try:
        r = env.harness(f"cd {E.C.WORKSPACE} 2>/dev/null && d=$(git rev-parse "
                        "--absolute-git-dir 2>/dev/null) && echo \"$d\"", workdir="/")
        gitdir = r.output.strip().splitlines()[-1] if r.rc == 0 and r.output.strip() else None
        have = [] if gitdir is None else [
            n for n in ("hooks", "config")
            if env.harness(f"test -e {gitdir}/{n}", workdir="/").rc == 0]
        if have:
            env.harness(f"tar czf {d}/git_meta.tgz -C {gitdir} " + " ".join(have),
                        check=True, workdir="/")
    except Exception as e:  # noqa: BLE001 - recorded in the manifest
        env.harness(f"rm -f {d}/git_meta.tgz", workdir="/")
        return {"git_dir": gitdir, "have": [], "error": type(e).__name__}
    return {"git_dir": gitdir, "have": have}


def container_hashes(sb: SB.Sandbox) -> dict[str, str]:
    """sha256 of the staged archives (what is read out must be what was screened)."""
    r = sb.exec(f"cd {EVIDENCE_DIR} && sha256sum *.tgz", timeout_s=120, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"hashing the staged evidence rc={r.rc}")
    return {ln.split()[1]: ln.split()[0] for ln in r.stdout.splitlines() if len(ln.split()) == 2}


def run_screen(sb: SB.Sandbox, secrets: list[str], sim_token: str) -> tuple[str, str]:
    """One screen of EVIDENCE_DIR in the container: ("hit" | "clean" | "error", detail)."""
    env = {f"{SECRET_ENV}_{i}": v for i, v in enumerate(secrets)} | {"EMX_SIM_TOKEN": sim_token}
    r = N.exec_env(sb, E.NOSHIM + screen_script(EVIDENCE_DIR, len(secrets)), env,
                   timeout_s=900)
    words = r.stdout.split()
    if r.rc == 0 and "SCREEN_HIT" in words:
        return "hit", "a secret is in the staged evidence"
    if r.rc == 1 and "SCREEN_CLEAN" in words:
        return "clean", ""
    return "error", f"screen failed rc={r.rc}"


def assemble_on_host(env, label: str, snap: dict, now: dict[str, list[str]],
                     remote_tgz: bytes, meta_tgz: bytes | None, meta: dict) -> Path:
    """What `trap_snapshot`, `export_traps_home` and `git_meta` write on the host, from the
    screened material only."""
    import io
    import tarfile
    import tempfile
    home = env.home
    for kind in ("fs", "git"):
        (home / "state" / f"{kind}_{label}.json").write_text(
            json.dumps(snap[kind], indent=1, sort_keys=True))
    bad = env._write_logs(home, now)
    other = home / "logs" / "container_other.txt"
    if env._other or env._inherited_missing:
        other.write_text("".join(x + "\n" for x in env._other) + "".join(
            f"inherited: {k}.jsonl missing in the forked ending\n"
            for k in sorted(env._inherited_missing)))
        env.log.append({"op": "log_corrupt", "logs_dropped": sorted(bad)})
    elif other.exists():
        other.unlink()
    name = env.bare.rsplit("/", 1)[1]
    dest = home / "remote"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(remote_tgz), mode="r:gz") as tf:
            tf.extractall(tmp, filter="data")
        shutil.move(str(Path(tmp) / name), str(dest / "origin.git"))
    with tarfile.open(Path(env.out_dir) / "traps_home.tgz", "w:gz") as tf:
        tf.add(home, arcname=E.PKG)
    man: dict = {"label": label, "git_dir": meta["git_dir"], "files": {},
                 "absent": ["git_dir"] if meta["git_dir"] is None else
                 [n for n in ("hooks", "config") if n not in meta["have"]]}
    if meta.get("error"):
        man["error"], man["absent"] = meta["error"], []  # unknown, not absent
    if meta_tgz is not None:
        (Path(env.out_dir) / f"git_meta_{label}.tgz").write_bytes(meta_tgz)
        with tarfile.open(fileobj=io.BytesIO(meta_tgz), mode="r:gz") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    b = tf.extractfile(m).read()
                    man["files"][m.name] = {"sha256": hashlib.sha256(b).hexdigest(),
                                            "bytes": len(b), "mode": oct(m.mode)}
    (Path(env.out_dir) / f"git_meta_{label}.json").write_text(json.dumps(man, indent=1))
    return home


def screened_evidence(env, label: str, secrets: list[str], sim_token: str) -> dict:
    """`ChainEnv.finish_traps` with nothing reaching the host before a clean screen (astra
    cx-build r2 #2): quiesce, the snapshot (kept in memory), this container's log lines (in
    memory), the remote and git hooks/config archived in the container; all of it staged in
    the container (`stage_in_container`), hashed, screened there (`run_screen`, tried again
    once on an error), and read out only on a clean verdict whose archives still hash the
    same. Anything else: nothing is written, {"ok": False, "why"}, home None.
    Returns {"ok", "why", "home", "attempts"}."""
    sb = env.sb
    rec: dict = {"ok": False, "why": None, "home": None, "attempts": 0}
    try:
        env.quiesce()
        snap = snapshot_in_memory(env, label)
        now = env.container_logs()
        meta = stage_in_container(env, label, snap, now, list(env._other))
        hashes = container_hashes(sb)
        verdict, why = "error", "not screened"
        for attempt in range(1, SCREEN_TRIES + 1):
            rec["attempts"] = attempt
            try:
                verdict, why = run_screen(sb, secrets, sim_token)
            except Exception as e:  # noqa: BLE001 - no verdict: tried again, then omitted
                verdict, why = "error", f"screen error: {type(e).__name__}"
            if verdict != "error":
                break
        if verdict != "clean":
            return rec | {"why": why}
        remote = sb.read_file(f"{EVIDENCE_DIR}/remote.tgz", timeout_s=600)
        meta_tgz = (sb.read_file(f"{EVIDENCE_DIR}/git_meta.tgz", timeout_s=300)
                    if meta["have"] else None)
        got = {"remote.tgz": hashlib.sha256(remote).hexdigest()}
        if meta_tgz is not None:
            got["git_meta.tgz"] = hashlib.sha256(meta_tgz).hexdigest()
        if got != hashes:
            return rec | {"why": "the staged archives changed after they were hashed"}
        home = assemble_on_host(env, label, snap, now, remote, meta_tgz, meta)
        return rec | {"ok": True, "home": home}
    except Exception as e:  # noqa: BLE001 - no evidence rather than unscreened evidence
        return rec | {"why": f"evidence step failed: {type(e).__name__}"}
    finally:
        with contextlib.suppress(Exception):
            sb.exec(f"rm -rf {EVIDENCE_DIR}", timeout_s=60, workdir="/")


# ------------------------------------------------------------------------------------------
# Run dirs in experiment 1's format
# ------------------------------------------------------------------------------------------


def write_run(out: Path, *, end: str, t0: float, lines: list[dict] | None,
              rng: tuple[int, int] | None, start_index: int | None, home: Path | None,
              extra: dict, error: str | None = None, second: bool = False,
              secrets: list[str] | None = None) -> None:
    """result.json (always; `cc_chain.write_run`'s fields, usage from the rollout's
    `token_count` events in the task's line range, cost $0 on the plan) and messages.json. A
    second task whose cutoff can't be found gets no messages.json, so its transcript measures
    read unknown, never task 1's commands (m9). Both files are redacted of `secrets` as text
    before they are written (r1 #2)."""
    info = None

    def write(name: str, obj) -> None:
        text, _n = redact_count(json.dumps(obj, default=str).encode(), secrets or [])
        (out / name).write_bytes(text)

    if lines is not None and not (second and start_index is None):
        msgs, info = TR.score_messages(lines)
        write("messages.json", msgs)
    u = TR.task_usage(lines, *rng) if lines is not None and rng is not None else {}
    res = {"end": end, "n_calls": u.get("n_calls", 0), "cost_usd": 0.0,
           "prompt_tokens": u.get("prompt_tokens", 0),
           "completion_tokens": u.get("completion_tokens", 0),
           "cached_tokens": u.get("cached_tokens", 0),
           "seconds": round(time.monotonic() - t0, 1),
           "traps_home": str(home) if home else None, "error": error,
           "extra": {"b_message_index": start_index, "transcript_info": info,
                     "usage": u or None, "rollout_range": list(rng) if rng else None,
                     **extra}}
    write("result.json", res)


# ------------------------------------------------------------------------------------------
# A history
# ------------------------------------------------------------------------------------------


@dataclass
class HistoryConfig:
    hist: CC.History
    a_task: object  # tasks.TaskInfo
    b_task: object
    out: Path
    host: str | None = None  # docker -H (None: the laptop's daemon)
    auth_path: Path = field(default_factory=lambda: Path.home() / ".codex" / "auth.json")
    memory_mb: int = N.AGENT_MEMORY_MB
    cpus: float = N.AGENT_CPUS


def _session(image: str, cfg: HistoryConfig) -> N.NativeSession:
    return N.NativeSession(ARM, image, lambda runner: X.ScriptedSim(), host=cfg.host,
                           allow=N.ALLOW["codex"], memory_mb=cfg.memory_mb, cpus=cfg.cpus,
                           extra_args=("-v", f"{CX.CLI_VOLUME}:{CX.CLI_DIR}:ro", "--tmpfs",
                                       f"{CX.SECRETS_C}:rw,mode=700,size=1m"))


def _verified_start(turns: list[dict], tid: str | None) -> bool:
    """Task 2 started from the carried thread: some call resumed `tid` (its thread.started
    named it) and none named another."""
    ids = [t["info"].get("thread_id") for t in turns]
    return bool(tid) and tid in ids and not any(t["info"].get("thread_mismatch")
                                                 for t in turns)


def _task(cfg: HistoryConfig, out: Path, task, branches, *, second: bool, prompt: str,
          session: dict | None, b_repo: CC.Repo | None) -> dict:
    """One task, start to evidence; every step sits inside the path that writes result.json.
    After task 1, the boundary and the session export are separate steps: a failed boundary
    still exports the session (task 2 then runs uncompacted, a recorded deviation).

    Credentials (astra cx-build r1 #2): raw turn and RPC logs go to a private staging dir; the
    current tokens are collected fail-closed after the task (else the task is "failed", with
    `primary_unknown_reason`, and nothing is exported or kept but result.json); every kept
    file is redacted; a rollout holding a token is never transferred.

    Primary eligibility (r1 #3): `extra.primary_unknown_reason` (read by `score.score_b`) is
    set for a quota ending, uncollectable secrets, and a task 2 that didn't verifiably start
    from the carried thread (a failed transfer or resume included). Evidence is kept either
    way. Returns {"end", "thread_id", "session": (session, manifest) | None, "boundary",
    "checks", "transfer_refused"}."""
    import tempfile
    t0 = time.monotonic()
    rec: dict = {"end": END_FAILED}
    end, err, home, lines, run, extra = END_FAILED, None, None, None, None, {}
    rng: tuple[int, int] | None = None
    base_lines = 0
    tid = session["thread_id"] if session else None
    secrets: list[str] = []
    sim_token: list[str] = []  # the session's MCP token: redacted from kept files, but no
    # reason to refuse a transfer (it is per session and dead once the session ends)
    secrets_ok, imported_ok = False, False
    unknown: str | None = None
    staging = Path(tempfile.mkdtemp(prefix="emx-cx-raw-"))  # 0700: raw logs until redacted

    steps: list[str] = []
    env_made = False

    def note(what: str, e: BaseException) -> None:
        nonlocal err
        steps.append(what)
        err = ((err + " | ") if err else "") + f"{what}: {type(e).__name__}: {e}"[:500]

    try:
        auth_bytes = Path(cfg.auth_path).read_bytes()
        secrets = CX.auth_tokens(json.loads(auth_bytes))
        if not secrets:
            raise RuntimeError("the auth file holds no tokens")
        with _session(task.image, cfg) as ns:
            env = None
            try:
                env, inventory = X.install_chain(ns.sb, task, out, branches)
                env_made = True
                extra["test_inventory"] = inventory
                if second:
                    extra["b_setup"] = env.b_setup(b_repo)
                else:
                    extra["remote"] = cfg.hist.remote
                install_cli_config(ns.sb, auth_bytes)
                rec["checks"] = X.env_checks(ns.sb)
                extra["egress_check"] = N.egress_check(ns.sb, ns.egress, "https://chatgpt.com/")
                if not extra["egress_check"].get("ok"):
                    raise RuntimeError("egress check failed")
                base_calls = 0
                if second:
                    for attempt in (1, 2):
                        try:
                            import_session(ns.sb, session)
                            break
                        except Exception:
                            if attempt == 2:
                                unknown = "transfer_failed"
                                raise
                    imported_ok = True
                    imported = parse_rollout(session["data"])
                    base_lines = len(imported)
                    base_calls = TR.task_usage(imported)["n_calls"]
                start_watch(ns.sb, base_calls + CC.MAX_CALLS)
                run = TaskRun(ns, staging, deadline=t0 + CC.MAX_SECONDS)
                end = run.drive(prompt, resume=tid)
                tid = run.thread
            except Exception as e:  # noqa: BLE001 - recorded; evidence still collected
                note("task", e)
                end = END_FAILED
                if run is not None:
                    tid = run.thread or tid
            with contextlib.suppress(Exception):
                stop_watch(ns.sb)
            with contextlib.suppress(Exception):
                CX.stop_cli(ns.sb)  # nothing of the task's CLI outlives it
            task_end = None
            if tid:
                with contextlib.suppress(Exception):
                    task_end = len(read_rollout(ns.sb, tid)[2])
            rng = (base_lines, task_end) if task_end is not None else None
            if not second and run is not None and tid:
                try:
                    rec["boundary"] = boundary_compact(run, tid)
                except Exception as e:  # noqa: BLE001
                    rec["boundary"] = {"ok": False, "deviation": f"boundary: {e}"[:300]}
            try:
                sim_token = [ns.server.token] if ns.server.token else []
                secrets = collect_secrets(ns.sb, secrets)
                secrets_ok = True
            except Exception as e:  # noqa: BLE001 - fail closed: nothing exported or kept
                note("secrets", e)
                end, unknown = END_FAILED, "secrets_uncollected"
            if secrets_ok and not second and tid:
                try:
                    rec["session"] = export_session(ns.sb, tid, secrets)
                except Contaminated as e:
                    rec["transfer_refused"] = extra["transfer_refused"] = str(e)
                except Exception as e:  # noqa: BLE001
                    note("session export", e)
            if env is not None and secrets_ok:  # r2 #2: screened in the container first
                ev = screened_evidence(env, "final" if second else "a-final", secrets,
                                       ns.server.token)
                home = ev["home"]
                extra["evidence"] = {k: ev[k] for k in ("ok", "why", "attempts")}
            elif env is not None:
                extra["evidence"] = {"ok": False, "why": "secrets uncollected: nothing exported",
                                     "attempts": 0}
            if secrets_ok and tid:
                with contextlib.suppress(Exception):
                    _p, data, _raw_lines = read_rollout(ns.sb, tid)
                    if second:
                        extra["imported_prefix_intact"] = data.startswith(session["data"])
                    _c, n = redact_count(data, secrets)
                    clean, _n = redact_count(data, secrets + sim_token)
                    extra["secrets_in_rollout"] = n
                    (out / "rollout.jsonl").write_bytes(clean)
                    lines = parse_rollout(clean)
            with contextlib.suppress(Exception):
                extra["auth_refreshed"] = sorted(secrets) != sorted(
                    CX.auth_tokens(json.loads(auth_bytes)))
            with contextlib.suppress(Exception):
                extra["codex_home"] = home_listing(ns.sb)
            with contextlib.suppress(Exception):  # sub-agent threads would add rollouts
                extra["rollouts_in_container"] = len(CX.container_rollouts(ns.sb))
            with contextlib.suppress(Exception):
                extra["asks"] = ns.server.export()
            with contextlib.suppress(Exception):
                extra["egress"] = egress_summary(ns.egress.events())
    except Exception as e:  # noqa: BLE001 - the session itself failed
        note("setup", e)
        end = END_FAILED
    finally:
        try:
            if secrets_ok:
                extra["secrets_in_logs"] = persist_redacted(staging, out,
                                                            secrets + sim_token)
        except Exception as e:  # noqa: BLE001
            note("logs", e)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        idx = None
        with contextlib.suppress(Exception):
            if lines is not None:
                msgs, _ = TR.score_messages(lines)
                idx = TR.task2_index(msgs, prompt) if second else 0
        turns = run.turns if run else []
        if end == END_QUOTA:
            unknown = "quota_refused"
        elif unknown is None and env_made and not (extra.get("evidence") or {}).get("ok"):
            unknown = "evidence_withheld"  # no screened evidence: the remote can't be scored
        elif second and unknown is None and not (
                imported_ok and _verified_start(turns, tid)
                and extra.get("imported_prefix_intact") is True):
            unknown = "b_not_from_carried_session"
        sess = rec.get("session")
        extra |= {"turns": turns, "thread_id": tid, "primary_unknown_reason": unknown,
                  "checks": rec.get("checks"), "boundary": rec.get("boundary"),
                  "session_manifest": sess[1] if sess else None}
        if lines is not None:
            extra["system_prompt"] = TR.base_instructions_sha(lines)
            if rng is not None:
                extra["models"] = sorted(set(TR.turn_models(lines, *rng)))
                extra["compactions_in_task"] = n_compacted(lines[rng[0]:rng[1]])
            extra["compactions_in_rollout"] = n_compacted(lines)
        if not secrets_ok and (run is not None or "secrets" in steps):  # r2 #1: tokens
            # unknown after the agent may have run: only metadata known to be safe, no text
            extra, err = safe_extra(extra), safe_error(steps)
        write_run(out, end=end, t0=t0, lines=lines, rng=rng, start_index=idx, home=home,
                  extra=extra, error=err, second=second, secrets=secrets + sim_token)
        rec["end"], rec["thread_id"] = end, tid
    return rec


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REASONS = {"quota_refused", "secrets_uncollected", "transfer_failed", "transfer_refused",
           "b_not_from_carried_session", "evidence_withheld", "bootstrap_failed",
           "a_quota_refused", "no_session_from_a"}
DECISIONS = {"infra_resume", "non_answer", "stalled", "complete", "checkin", "exhausted"}


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _bool(v) -> bool | None:
    return v if isinstance(v, bool) else None


def safe_extra(extra: dict) -> dict:
    """The allowlist of `extra` kept when the secrets can't be collected (astra cx-build r2
    #1): counts, booleans, our own enumerated values and validated ids only; every free-text
    field (asks, boundary and error texts, turn texts, check outputs, egress hosts) is
    dropped."""
    turns = []
    for t in extra.get("turns") or []:
        kind = (t.get("decision") or {}).get("kind")
        turns.append({"k": _int(t.get("k")), "decision": {
            "kind": kind if kind in DECISIONS else None}})
    tid = extra.get("thread_id")
    rr = extra.get("rollout_range")
    reason = extra.get("primary_unknown_reason")
    return {"b_message_index": None, "texts_withheld": "secrets uncollected",
            "primary_unknown_reason": reason if reason in REASONS else "secrets_uncollected",
            "turns": turns,
            "thread_id": tid if isinstance(tid, str) and UUID_RE.fullmatch(tid) else None,
            "rollout_range": [_int(x) for x in rr] if isinstance(rr, list) else None,
            "remote": extra.get("remote") if extra.get("remote") in CC.REMOTES else None,
            "boundary_ok": _bool((extra.get("boundary") or {}).get("ok")),
            "evidence_ok": False, "auth_refreshed": None,
            "rollouts_in_container": _int(extra.get("rollouts_in_container")),
            "imported_prefix_intact": _bool(extra.get("imported_prefix_intact"))}


def safe_error(steps: list[str]) -> str:
    """The error field without any exception text: our own step names only."""
    return "failed steps: " + ", ".join(steps) + " (texts withheld: secrets uncollected)"


def skipped_run(out: Path, *, reason: str, error: str, second: bool) -> None:
    """A task that never ran: failed, and its primary outcome unknown."""
    write_run(out, end=END_FAILED, t0=time.monotonic(), lines=None, rng=None,
              start_index=None, home=None, second=second,
              extra={"skipped": reason, "primary_unknown_reason": reason}, error=error)


def run_history(cfg: HistoryConfig) -> dict:
    """Both tasks of one history (module docstring). Returns {"a", "b" (dirs), "boundary",
    ...}; each task's dir holds result.json whatever happened."""
    hist, a_repo = cfg.hist, CC.REPOS[cfg.hist.repo]
    b_repo = CC.REPOS[next(r for r in CC.REPOS if r != hist.repo)]
    a_dir, b_dir = cfg.out / "a", cfg.out / "b"
    for d in (a_dir, b_dir):
        if d.exists() and any(d.iterdir()):
            raise FileExistsError(f"{d} is not empty: use a new attempt directory (m10)")
        d.mkdir(parents=True, exist_ok=True)
    out = {"a": str(a_dir), "b": str(b_dir)}
    try:
        CX.ensure_cli_volume(cfg.host, cfg.a_task.image)
    except Exception as e:  # noqa: BLE001 - r1 #8: both tasks get an explicit result
        msg = f"bootstrap: {type(e).__name__}: {e}"[:500]
        for d, second in ((a_dir, False), (b_dir, True)):
            skipped_run(d, reason="bootstrap_failed", error=msg, second=second)
        return out | {"thread_id": None, "a_end": END_FAILED, "b_end": END_FAILED,
                      "boundary": None}
    a = _task(cfg, a_dir, cfg.a_task, X.first_branches(hist), second=False,
              prompt=X.task_prompt(cfg.a_task.instruction, a_repo.a_branch), session=None,
              b_repo=None)
    out |= {"thread_id": a.get("thread_id"), "a_end": a["end"], "boundary": a.get("boundary")}
    if a["end"] == END_QUOTA or not a.get("session"):
        reason = ("a_quota_refused" if a["end"] == END_QUOTA
                  else "transfer_refused" if a.get("transfer_refused")
                  else "no_session_from_a")
        skipped_run(b_dir, reason=reason, error=f"task 2 not run: {reason}", second=True)
        out["b_end"] = END_FAILED
        return out
    session, _man = a["session"]
    b = _task(cfg, b_dir, cfg.b_task, [], second=True,
              prompt=X.lead_and_task(b_repo, a_repo, cfg.b_task.instruction), session=session,
              b_repo=b_repo)
    out["b_end"] = b["end"]
    return out
