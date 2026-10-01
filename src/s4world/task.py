"""Inspect task for the S4 pilot (implementation plan §1.6a, §1.8–1.10; prereg §4).

One task per arm. Per sample:
1. `setup_world` seeds the containers in the frozen order (§1.6a) from `build_world(arm, seed)`;
2. the agent is Inspect's ReAct loop with no submit tool (the episode ends when the model replies
   without a tool call) and one recording `bash` tool: around every call it records the
   container clock and snapshots the task output directory, invisibly to the model (§1.8);
3. `probe` appends the post-episode comprehension question, with no tools (§1.9);
4. `collect` copies the servers' logs and the final output snapshot into the sample store (§1.10).

Scoring proper happens later, offline, in `s4world.score` over the saved .eval logs.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.agent import AgentPrompt, as_solver, react
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageUser, GenerateConfig
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, Solver, TaskState, chain, solver
from inspect_ai.tool import Tool, tool
from inspect_ai.util import sandbox, store

from s4world import contracts as C
from s4world import texts

ENV = Path(__file__).resolve().parents[2] / "env" / "S4"
BASH_TIMEOUT_S = 180

# Snapshot of the task output directory, run inside the agent container after every call.
_SNAPSHOT_PY = r"""
import base64, hashlib, json, os, sys
root, maxb = sys.argv[1], int(sys.argv[2])
out = []
for d, _, files in os.walk(root):
    for n in sorted(files):
        p = os.path.join(d, n)
        try:
            b = open(p, "rb").read()
        except OSError:
            continue
        out.append({"path": p, "size": len(b), "sha256": hashlib.sha256(b).hexdigest(),
                    "body_b64": base64.b64encode(b).decode() if len(b) <= maxb else None})
print(json.dumps(sorted(out, key=lambda e: e["path"])))
"""


def episode_seed(base_seed: int, arm: str, epoch: int) -> int:
    """Deterministic per-episode world seed, recorded in the sample store."""
    return base_seed * 100_000 + list(C.ARMS).index(arm) * 1_000 + epoch


async def _clock(box) -> float:
    r = await box.exec(["date", "+%s.%N"])
    return float(r.stdout.strip())


async def _set_mtime(box, path: str, now: float, offset_s: float | None) -> None:
    """Backdate a seeded file so listings show a plausible age (python is in every image)."""
    if offset_s is None:
        return
    t = now + offset_s
    await box.exec(["python3", "-c", f"import os; os.utime({path!r}, ({t}, {t}))"])


async def _snapshot(max_body: int) -> list[dict] | None:
    """The output directory's files, or None if the snapshot failed (never a silent [])."""
    try:
        r = await sandbox().exec(["python3", "-c", _SNAPSHOT_PY, C.OUTPUT_DIR, str(max_body)])
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (json.JSONDecodeError, TimeoutError, OSError, RuntimeError):
        return None


_TRUNC_NOTE = "\n[output truncated: showing the first {n} characters of {total}]\n"


def _deliver(stdout: str, stderr: str) -> str:
    """The exact tool result, capped at STDOUT_MAX by us so Inspect never truncates it further
    (max_tool_output is set above the cap): what we store is what the model sees."""
    text = (f"{stderr}\n" if stderr else "") + stdout
    if len(text) <= C.STDOUT_MAX:
        return text
    return text[: C.STDOUT_MAX] + _TRUNC_NOTE.format(n=C.STDOUT_MAX, total=len(text))


_locks: dict[str, asyncio.Lock] = {}


@tool(name="bash")
def recording_bash() -> Tool:
    async def execute(command: str) -> str:
        """
        Use this function to execute bash commands.

        Args:
          command: The bash command to execute.

        Returns:
          The output of the command.
        """
        s = store()
        lock = _locks.setdefault(s.get("s4_episode_key", "default"), asyncio.Lock())
        async with lock:  # one call at a time, so turns are totally ordered
            box = sandbox()
            calls = s.get(C.STORE_CALLS, [])
            turn = len(calls) + 1
            t_start = await _clock(box)
            try:
                res = await box.exec(["bash", "--login", "-c", command],
                                     timeout=BASH_TIMEOUT_S, cwd=C.AGENT_HOME)
                stdout, stderr, code = res.stdout, res.stderr, res.returncode
            except TimeoutError:
                stdout, stderr, code = "", f"command timed out after {BASH_TIMEOUT_S}s", 124
            t_end = await _clock(box)
            snap = await _snapshot(C.SNAPSHOT_MAX_BODY)
            delivered = _deliver(stdout, stderr)
            # "stdout" is exactly what the model receives (markers are scored on it, §1.7)
            calls.append({"turn": turn, "t_start": t_start, "t_end": t_end, "cmd": command,
                          "stdout": delivered, "stderr_tail": stderr[-2000:],
                          "exit": code, "snapshot": snap})
            s.set(C.STORE_CALLS, calls)
        return delivered

    return execute


@solver
def setup_world(arm_name: str, base_seed: int) -> Solver:
    """Seed the containers in the §1.6a order; the schedule is written last."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        # imported per sample, so building a task needs no data
        from s4world.content import build_world

        arm = C.ARMS[arm_name]
        seed = episode_seed(base_seed, arm_name, state.epoch)
        agent, reg = sandbox(), sandbox("registry")
        now = await _clock(reg)
        # date the world by the container clock, so post names, the restage date and file
        # mtimes agree with what `date` shows the agent
        world = build_world(arm, seed, now=datetime.fromtimestamp(now, UTC))
        for f in world.registry_files:  # 1. registry store
            dest = f"{C.REGISTRY_STORE}/{f.path.lstrip('/')}"
            await reg.exec(["mkdir", "-p", os.path.dirname(dest)])
            await reg.write_file(dest, f.body)
            await _set_mtime(reg, dest, now, f.mtime_offset_s)
        for f in world.agent_files:  # 2. agent files
            await agent.exec(["mkdir", "-p", os.path.dirname(f.path)])
            await agent.write_file(f.path, f.body)
            await agent.exec(["chmod", oct(f.mode)[2:], f.path])
            await _set_mtime(agent, f.path, now, f.mtime_offset_s)
        await agent.exec(["mkdir", "-p", C.OUTPUT_DIR, "/data/proteins"])
        if arm.channel:  # 3. ops history
            tickets = [t.__dict__ for t in world.ops_tickets]
            await sandbox("ops").exec(["mkdir", "-p", "/srv/control"])
            await sandbox("ops").write_file("/srv/control/tickets.json", json.dumps(tickets))
        t0 = await _clock(reg) + 5.0  # 4. schedule last
        sched = {"t0": t0, "writes": [
            {"offset_s": w.offset_s, "path": w.path, "activity_line": w.activity_line,
             "body_b64": base64.b64encode(w.body).decode()} for w in world.schedule]}
        await reg.exec(["mkdir", "-p", os.path.dirname(C.SCHEDULE_PATH)])
        await reg.write_file(C.SCHEDULE_PATH, json.dumps(sched))
        post = next((f.body for f in world.registry_files
                     if f.path.startswith(C.BOARD_REPO + "/")
                     and (world.markers.get("plan_post", "\0").encode() in f.body
                          or world.markers.get("benign_post", "\0").encode() in f.body)), b"")
        s = store()  # 5. world metadata for the scorer
        s.set(C.STORE_WORLD, {"arm": arm_name, "seed": seed, "model": str(state.model),
                              "world_now": now,
                              "markers": world.markers,
                              "plan_text": post.decode("utf-8", "replace")})
        s.set("s4_episode_key", f"{arm_name}-{seed}-{state.sample_id}-{state.epoch}")
        state.messages = [ChatMessageUser(content=world.assignment)]
        s.set("s4_system_prompt", world.system_prompt)
        return state

    return solve


def agent_solver(system_prompt: str) -> Solver:
    """ReAct loop, no submit tool, our prompt only (no default assistant or submit prompts)."""
    agent = react(
        name="ra7",
        prompt=AgentPrompt(instructions=system_prompt, handoff_prompt=None,
                           assistant_prompt=None, submit_prompt=None),
        tools=[recording_bash()],
        submit=False,
    )
    return as_solver(agent)


@solver
def probe() -> Solver:
    """Post-episode comprehension probe: one user message on the full conversation, no tools."""
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        state.messages.append(ChatMessageUser(content=texts.PROBE_PROMPT))
        state.tools = []
        state = await generate(state, tool_calls="none")
        store().set(C.STORE_PROBE, {
            "prompt": texts.PROBE_PROMPT,
            "reply": state.output.completion if state.output else "",
            "model": state.output.model if state.output else "",
            "usage": state.output.usage.model_dump() if state.output and state.output.usage
            else {}})
        return state

    return solve


async def _read_jsonl(box, path: str, errors: list[str]) -> list[dict]:
    """A server log's lines. A missing log just means no requests; any other failure is
    recorded in `errors` so the episode is marked scoring_incomplete (review r1, finding 7)."""
    try:
        r = await box.exec(["sh", "-c", f"test -e {path} || exit 3; cat {path}"])
    except (TimeoutError, OSError, RuntimeError) as e:
        errors.append(f"could not read {path}: {e!r}")
        return []
    if r.returncode == 3:
        return []
    if r.returncode != 0:
        errors.append(f"could not read {path}: rc={r.returncode} {r.stderr[-200:]}")
        return []
    lines = []
    for i, line in enumerate(r.stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:  # keep the rest; flag the episode (code review r2, 6)
            errors.append(f"malformed line {i} in {path}")
    return lines


@scorer(metrics=[mean()])
def collect() -> object:
    """Copy server logs and the final output snapshot into the store before teardown (§1.10)."""
    async def score(state: TaskState, target: Target) -> Score:
        errors: list[str] = []
        reg = sandbox("registry")
        final = await _snapshot(C.FINAL_BODY_MAX)
        if final is None:
            errors.append("final output snapshot failed")
        arts = {"registry_access": await _read_jsonl(reg, "/var/log/registry/access.jsonl",
                                                     errors),
                "registry_writes": await _read_jsonl(reg, "/var/log/registry/writes.jsonl",
                                                     errors),
                "ops_access": [], "ops_writes": [], "final_output": final or []}
        meta = store().get(C.STORE_WORLD, {})
        if not meta:
            errors.append("world metadata missing (setup did not finish)")
        elif C.ARMS[meta["arm"]].channel:
            ops = sandbox("ops")
            arts["ops_access"] = await _read_jsonl(ops, "/var/log/ops/access.jsonl", errors)
            arts["ops_writes"] = await _read_jsonl(ops, "/var/log/ops/writes.jsonl", errors)
        if not (store().get(C.STORE_PROBE) or {}).get("reply"):
            errors.append("probe record missing or empty")
        if any(c.get("snapshot") is None for c in store().get(C.STORE_CALLS, [])):
            errors.append("a per-call snapshot failed")
        arts["collect_errors"] = errors
        store().set(C.STORE_ARTIFACTS, arts)
        n_calls = len(store().get(C.STORE_CALLS, []))
        return Score(value=0, answer=str(n_calls), metadata={"n_calls": n_calls})

    return score


def _compose(arm: C.Arm) -> str:
    return str(ENV / ("compose.channel.yaml" if arm.channel else "compose.nochannel.yaml"))


@task
def s4_pilot(arm: str = "channel", base_seed: int = 0, turn_limit: int = 100,
             time_limit: int = 1800, cost_limit: float | None = None) -> Task:
    """One pilot arm. Epochs = episodes; each epoch gets its own world seed."""
    a = C.ARMS[arm]

    @solver
    def run_agent() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            return await agent_solver(store().get("s4_system_prompt"))(state, generate)
        return solve

    return Task(
        dataset=[Sample(id=f"s4-{arm}", input=texts.ASSIGNMENT, metadata={"arm": arm})],
        setup=setup_world(arm, base_seed),
        solver=chain(run_agent(), probe()),
        scorer=collect(),
        sandbox=("docker", _compose(a)),
        turn_limit=turn_limit,
        time_limit=time_limit,
        cost_limit=cost_limit,
        config=GenerateConfig(temperature=1.0, parallel_tool_calls=False,
                              max_tool_output=C.STDOUT_MAX + 1024),
    )
