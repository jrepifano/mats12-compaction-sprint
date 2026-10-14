"""P44 agent: one Inspect solver for the three stages (contracts.py "Data flow").

- "prefix": X3 setup, turns 1-4; then, in the same sandbox, the workspace archive W4, the gen-1
  pi-mono summary S1, the notes request and <= NOTES_TURNS_MAX notes turns, the notes text,
  W4n and S1n (turns 1-4 plus the notes turns, for CPw). An agent that calls `done` before
  turn 4 ends the prefix (`ended`).
- "chain": replay W4 (or W4n), write the seeded or deleted NOTES.md, check the workspace
  against the prefix's last snapshot, start from the gen-1 context run.py built, and run to
  `done` or the cap with the arm's later handoffs.
- "scratch": X3 setup (M: the memory-file system prompt), the whole chain in one sample.

The turn loop is P23's (p23/agent.py `p23_episode`, itself X3's `x3.task.episode` without its
probes): the per-turn cost bound at ceiling prices, one command per turn, X3's nudge, `done`
ends. Questions (beliefs, the final Q3 probe) and summaries are single no-tool calls costed
with the same bound (`ask`, modelled on `x3.task.run_probe`); the belief and Q3 questions are
asked with the tools declared and tool_choice "none", because their context holds tool calls.
Belief questions run alongside the next turns (asyncio tasks on a copy of the context).
"""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    get_model,
)
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import ToolCall
from inspect_ai.util import sandbox, store

from p44 import contracts as C
from p44 import handoff as H
from x3 import contracts as X3C
from x3 import texts as X3T
from x3.task import (
    ATTEMPT_TIMEOUT_S,
    ATTEMPTS,
    TOOLS,
    _run_command,
    affordable_max_tokens,
    call_bound,
    call_cost,
    generate_with_retries,
    input_bound_tokens,
    take_snapshot,
    tighter_bound,
    usage_tokens,
)

SOLUTION_PATH = f"{X3C.WORKDIR}/solution.py"
TEST_PATH = f"{X3C.WORKDIR}/test_solution.py"
MOCK_MODEL = "mockllm/model"
STORE_REC = "p44_record"
ARCHIVE_ROOTS = ("agent", "home/dev")      # relative to /, = X3's SNAPSHOT_ROOTS
ARCHIVE_MAX_BYTES = 8_000_000


@dataclass(frozen=True)
class Spec:
    episode_id: str
    stage: str                       # "prefix" | "chain" | "scratch"
    arm: str
    seed: str
    prefix_id: str
    task_id: str
    run: int
    model_slug: str
    model_id: str
    in_ceiling: float
    out_ceiling: float
    episode_limit_usd: float
    task: dict = field(default_factory=dict)            # asdict(x3 TaskSpec), prefix/scratch
    archive_b64: str = ""                                # chain: W4 or W4n
    expected_sha: dict = field(default_factory=dict)    # chain: {path: sha256} after replay
    post_write: dict = field(default_factory=dict)      # chain: {path: text | None (delete)}
    init_messages: list = field(default_factory=list)   # chain: the gen-1 context (dumps)
    gen1: dict = field(default_factory=dict)            # chain: {kind, handoff_text, seed_text}
    summary_prev: str | None = None                     # chain, summary arms: S1 (seeded)
    system_text: str = X3T.SYSTEM_PROMPT
    task_text: str = ""                                  # the first user message


def _spec(state: TaskState) -> Spec:
    return Spec(**state.metadata)


def dumps_of(messages: list[ChatMessage]) -> list[dict]:
    return [m.model_dump(exclude_none=True) for m in messages]


_MSG_CLASS = {"system": ChatMessageSystem, "user": ChatMessageUser,
              "assistant": ChatMessageAssistant, "tool": ChatMessageTool}


def to_messages(dumps: list[dict]) -> list[ChatMessage]:
    return [_MSG_CLASS[d["role"]].model_validate(d) for d in dumps]


# --- sandbox helpers ---------------------------------------------------------------------------
async def read_file(path: str) -> str | None:
    r = await sandbox().exec(["cat", path], user="root")
    return r.stdout if r.returncode == 0 else None


async def archive_workspace() -> str:
    """base64 tar.gz of /agent and /home/dev, as root (bytes and modes preserved)."""
    cmd = "tar czf - -C / " + " ".join(ARCHIVE_ROOTS) + " | base64 -w0"
    r = await sandbox().exec(["bash", "-c", cmd], user="root")
    if r.returncode != 0:
        raise RuntimeError(f"archive failed: rc={r.returncode} {r.stderr[-300:]}")
    if len(r.stdout) > ARCHIVE_MAX_BYTES:
        raise RuntimeError(f"archive too large: {len(r.stdout)} bytes")
    return r.stdout.strip()


async def restore_workspace(archive_b64: str) -> None:
    box = sandbox()
    await box.write_file("/tmp/p44_ws.b64", archive_b64)
    cmd = ("rm -rf /agent /home/dev && base64 -d /tmp/p44_ws.b64 | tar xzpf - -C / "
           "&& rm -f /tmp/p44_ws.b64")
    r = await box.exec(["bash", "-c", cmd], user="root")
    if r.returncode != 0:
        raise RuntimeError(f"restore failed: rc={r.returncode} {r.stderr[-300:]}")


def file_shas(snapshot: dict) -> dict:
    return {p: (e or {}).get("sha256") for p, e in (snapshot.get("files") or {}).items()}


def replay_mismatch(written: dict, expected: dict) -> list[str]:
    return sorted(p for p in set(written) | set(expected) if written.get(p) != expected.get(p))


# --- one costed no-tool call ---------------------------------------------------------------------
async def ask(base: list[ChatMessage], prompt: str, *, spent: float, spec: Spec, cap: int,
              with_tools: bool) -> dict:
    """One question on `base + [prompt]` with no tool use, bounded so `spent` plus its worst case
    stays within the episode limit. An empty reply is retried once. Record keys: prompt, reply,
    status (ok/empty/skipped_budget/api_error), usage, max_tokens, spent_at_ceiling, error."""
    msgs = list(base) + [ChatMessageUser(content=prompt)]
    ib = tighter_bound(input_bound_tokens(msgs, TOOLS if with_tools else []), None, msgs)
    rec = {"prompt": prompt, "reply": None, "status": None, "usage": None, "max_tokens": 0,
           "spent_at_ceiling": 0.0, "error": None}
    for _ in range(2):
        mt = affordable_max_tokens(spent + rec["spent_at_ceiling"], spec.episode_limit_usd, ib,
                                   spec.in_ceiling, spec.out_ceiling, cap)
        if mt < X3C.MIN_PROBE_MAX_TOKENS:
            rec["status"] = rec["status"] or "skipped_budget"
            break
        one = call_bound(ib, mt, spec.in_ceiling, spec.out_ceiling) / ATTEMPTS
        cfg = GenerateConfig(max_tokens=mt, parallel_tool_calls=False, max_retries=0,
                             attempt_timeout=ATTEMPT_TIMEOUT_S)
        out, err = None, ""
        for _k in range(ATTEMPTS):
            try:
                out = await get_model().generate(
                    msgs, tools=TOOLS if with_tools else [],
                    tool_choice="none" if with_tools else None, config=cfg)
                break
            except Exception as e:  # noqa: BLE001 - any API failure
                rec["spent_at_ceiling"] += one
                err = repr(e)[:2000]
        if out is None:
            rec.update(status="api_error", error=err, max_tokens=mt)
            break
        rec["spent_at_ceiling"] += call_cost(out, spec.in_ceiling, spec.out_ceiling, one)
        text = out.message.text or ""
        rec.update(reply=text, usage=out.usage.model_dump() if out.usage else None,
                   max_tokens=mt, status="ok" if text.strip() else "empty")
        if text.strip():
            break
    return rec


async def summarize(new_dumps: list[dict], previous: str | None, *, spent: float,
                    spec: Spec) -> dict:
    sys_text, user_text = H.summary_request(new_dumps, previous)
    rec = await ask([ChatMessageSystem(content=sys_text)], user_text, spent=spent, spec=spec,
                    cap=C.SUMMARY_MAX_TOKENS, with_tools=False)
    rec["prompt"] = None                     # the request is reconstructible; keep logs small
    rec["previous"] = previous
    return rec


# --- the turn loop -------------------------------------------------------------------------------
class Loop:
    """The per-sample state of P23's loop plus P44's bookkeeping."""

    def __init__(self, spec: Spec, messages: list[ChatMessage], turns: int):
        self.spec = spec
        self.messages = messages
        self.turns = turns
        self.spent = 0.0
        self.prev: tuple[int, int, int] | None = None
        self.calls: list[dict] = []
        self.snapshots: list[dict] = []
        self.nudges = self.multi = self.notes_turns = 0
        self.end_reason: str | None = None
        self.api_error: str | None = None
        self.questions: list[dict] = []        # summaries and their costs, for the record

    async def turn(self, *, notes: bool = False) -> str | None:
        """One agent turn. Returns "done" when the agent called done, None otherwise; sets
        end_reason on a cost cap, API error or length stop (never on `done` in notes turns)."""
        sp = self.spec
        ib = tighter_bound(input_bound_tokens(self.messages, TOOLS), self.prev, self.messages)
        mt = affordable_max_tokens(self.spent, sp.episode_limit_usd, ib, sp.in_ceiling,
                                   sp.out_ceiling, X3C.MAX_TOKENS_PER_TURN)
        if mt < X3C.MIN_CALL_MAX_TOKENS:
            self.end_reason = X3C.EndReason.COST_CAP.value
            return None
        one = call_bound(ib, mt, sp.in_ceiling, sp.out_ceiling) / ATTEMPTS
        out, extra, err = await generate_with_retries(self.messages, TOOLS, mt, one)
        self.spent += extra
        if out is None:
            self.end_reason = X3C.EndReason.API_ERROR.value
            self.api_error = err
            return None
        if notes:
            self.notes_turns += 1
        else:
            self.turns += 1
        self.spent += call_cost(out, sp.in_ceiling, sp.out_ceiling, one)
        msg = out.message
        self.messages.append(msg)
        ut = usage_tokens(out)
        self.prev = (ut[0], ut[1], len(self.messages)) if ut else None
        if out.stop_reason in ("model_length", "content_filter"):
            self.end_reason = out.stop_reason
            return None
        tcs: list[ToolCall] = list(msg.tool_calls or [])
        if not tcs:
            if notes:
                return "done"                    # a reply without a command ends the notes
            self.nudges += 1
            self.messages.append(ChatMessageUser(content=X3T.NUDGE))
            return None
        first, rest = tcs[0], tcs[1:]
        ended = None
        if first.function == "done":
            result, ended = "", "done"
        elif first.function == "execute_command":
            cmd = first.arguments.get("command")
            if not isinstance(cmd, str):
                result = "error: execute_command needs a string argument `command`"
            else:
                n0 = len(self.calls)
                result = await _run_command(cmd, self.turns, self.calls, self.snapshots)
                for c in self.calls[n0:]:
                    c["notes"] = notes
        else:
            result = f"error: unknown tool {first.function!r}"
        self.messages.append(ChatMessageTool(content=result, tool_call_id=first.id,
                                             function=first.function))
        for tc in rest:
            self.multi += 1
            self.messages.append(ChatMessageTool(content=X3T.MULTI_CALL_RESULT,
                                                 tool_call_id=tc.id, function=tc.function))
        if ended == "done" and not notes:
            self.end_reason = X3C.EndReason.DONE.value
        return ended

    async def notes_round(self) -> str | None:
        """The reset warning and <= NOTES_TURNS_MAX notes turns; returns NOTES.md's text."""
        self.messages.append(ChatMessageUser(content=C.NOTES_REQUEST))
        for _ in range(C.NOTES_TURNS_MAX):
            r = await self.turn(notes=True)
            if r == "done" or self.end_reason:
                break
        return await read_file(C.NOTES_PATH)

    def belief(self, at_turn: int, gen: int) -> asyncio.Task:
        """The forked belief question on a copy of the current context, started now and run
        alongside the next turns (it never enters the context); await the task for the row."""
        base = list(self.messages)

        async def one() -> dict:
            rec = await ask(base, C.BELIEF_PROMPT, spent=self.spent, spec=self.spec,
                            cap=C.BELIEF_MAX_TOKENS, with_tools=True)
            self.spent += rec["spent_at_ceiling"]
            return {"turn": at_turn, "gen": gen, "record": rec}

        return asyncio.create_task(one())

    async def summary(self, new_dumps: list[dict], previous: str | None) -> dict:
        rec = await summarize(new_dumps, previous, spent=self.spent, spec=self.spec)
        self.spent += rec["spent_at_ceiling"]
        self.questions.append({"kind": "summary", "turn": self.turns,
                               "status": rec["status"], "usd": rec["spent_at_ceiling"]})
        return rec

    def record(self) -> dict:
        return {"messages": dumps_of(self.messages), "end_reason": self.end_reason,
                "n_turns": self.turns, "n_notes_turns": self.notes_turns,
                "n_nudges": self.nudges, "n_multi_call": self.multi, "calls": self.calls,
                "snapshots": self.snapshots, "spent_at_ceiling": self.spent,
                "api_error": self.api_error, "questions": self.questions}


# --- stages --------------------------------------------------------------------------------------
async def _x3_setup(spec: Spec) -> None:
    from x3.tasks import build_workspace

    task = X3C.TaskSpec(**{**spec.task, "conflict_pair": tuple(spec.task["conflict_pair"])})
    box = sandbox()
    await box.exec(["mkdir", "-p", X3C.WORKDIR], user="root")
    for name, body in build_workspace(task).items():
        await box.write_file(f"{X3C.WORKDIR}/{name}", body)
    await box.write_file(f"{X3C.WORKDIR}/{X3T.RUN_INFO_FILENAME}",
                         X3T.run_info(X3C.Context.NEUTRAL))
    await box.exec(["chown", "-R", f"{X3C.AGENT_USER}:{X3C.AGENT_USER}", X3C.WORKDIR],
                   user="root")


def first_messages(spec: Spec) -> list[ChatMessage]:
    return [ChatMessageSystem(content=spec.system_text), ChatMessageUser(content=spec.task_text)]


async def run_prefix(spec: Spec) -> dict:
    await _x3_setup(spec)
    lp = Loop(spec, first_messages(spec), 0)
    lp.snapshots.append(await take_snapshot(0))
    while lp.turns < C.PREFIX_TURNS and lp.end_reason is None:
        await lp.turn()
    rec = lp.record()
    rec["ended"] = lp.end_reason is not None
    if rec["ended"]:
        rec["post_probe"] = await _post_probe(lp)
        return rec
    solution_w4 = await read_file(SOLUTION_PATH)
    archive_w4 = await archive_workspace()
    snap_w4 = lp.snapshots[-1]
    task_dumps = dumps_of(lp.messages)[1:]
    s1 = await lp.summary(task_dumps, None)
    n_before_notes = len(lp.messages)
    n_calls, n_snaps = len(lp.calls), len(lp.snapshots)
    notes_text = await lp.notes_round()
    archive_w4n = await archive_workspace()
    snap_w4n = await take_snapshot(len(lp.snapshots))
    s1n = await lp.summary(dumps_of(lp.messages)[1:], None)
    rec = lp.record()
    rec.update(ended=False, archive_w4=archive_w4, archive_w4n=archive_w4n,
               sha_w4=file_shas(snap_w4), sha_w4n=file_shas(snap_w4n),
               solution_w4=solution_w4, solution_w4n=await read_file(SOLUTION_PATH),
               summary_s1=s1, summary_s1n=s1n, notes_text=notes_text,
               n_prefix_messages=n_before_notes,
               notes_messages=rec["messages"][n_before_notes:],
               messages=rec["messages"][:n_before_notes],
               notes_calls=rec["calls"][n_calls:], calls=rec["calls"][:n_calls],
               notes_snapshots=rec["snapshots"][n_snaps:] + [snap_w4n],
               snapshots=rec["snapshots"][:n_snaps],
               # the notes turns end the prefix with an end_reason only on a failure
               prefix_failed=lp.end_reason)
    return rec


async def _post_probe(lp: Loop) -> dict:
    rec = await ask(lp.messages, X3T.PROBE_PROMPT, spent=lp.spent, spec=lp.spec,
                    cap=X3C.PROBE_MAX_TOKENS, with_tools=True)
    lp.spent += rec["spent_at_ceiling"]
    return rec


async def run_chain(spec: Spec) -> dict:
    arm = C.ARMS[spec.arm]
    scratch = spec.stage == "scratch"
    if scratch:
        await _x3_setup(spec)
        lp = Loop(spec, first_messages(spec), 0)
        lp.snapshots.append(await take_snapshot(0))
        gens = [{"gen": 0, "start_turn": 0, "end_turn": None, "kind": "full",
                 "handoff_text": None, "seed_text": None, "context": []}]
        handoffs: list[int] = []
        beliefs: list[dict] = []
        summary_prev = None
    else:
        await restore_workspace(spec.archive_b64)
        for path, text in sorted(spec.post_write.items()):
            if text is None:
                await sandbox().exec(["rm", "-f", path], user="root")
            else:
                await sandbox().write_file(path, text)
        await sandbox().exec(["chown", "-R", f"{X3C.AGENT_USER}:{X3C.AGENT_USER}",
                              X3C.WORKDIR, X3C.HOME], user="root")
        snap0 = await take_snapshot(0)
        lp = Loop(spec, to_messages(spec.init_messages), C.PREFIX_TURNS)
        lp.snapshots.append(snap0)
        expected = dict(spec.expected_sha)
        for path, text in spec.post_write.items():
            expected.pop(path, None)
        written = {p: s for p, s in file_shas(snap0).items() if p not in spec.post_write}
        bad = replay_mismatch(written, expected)
        if bad:
            rec = lp.record()
            rec.update(end_reason="replay_mismatch", replay_mismatch=bad, generations=[],
                       beliefs=[], handoff_turns=[])
            return rec
        handoffs = [C.PREFIX_TURNS] if C.PREFIX_TURNS in arm.handoffs else []
        gen1 = {"gen": 1 if handoffs else 0, "start_turn": C.PREFIX_TURNS, "end_turn": None,
                **spec.gen1, "context": list(spec.init_messages)}
        gens = [gen1]
        summary_prev = spec.summary_prev
        beliefs = [lp.belief(C.PREFIX_TURNS, gen1["gen"])]
    gen_start_index = len(lp.messages)          # where this generation's new messages begin

    while lp.end_reason is None:
        if lp.turns >= C.TURN_CAP:
            lp.end_reason = X3C.EndReason.TURN_CAP.value
            break
        await lp.turn()
        if lp.end_reason is not None or lp.turns not in C.HANDOFF_TURNS:
            continue
        t = lp.turns
        if t in arm.handoffs:
            gens[-1]["end_turn"] = t
            gens[-1]["solution_at_end"] = await read_file(SOLUTION_PATH)
            new = await _handoff(lp, arm, spec, summary_prev, gen_start_index)
            if new is None:
                break
            ctx, gen_rec, summary_prev = new
            if lp.end_reason is not None:
                break
            lp.messages = to_messages(ctx)
            lp.prev = None
            handoffs.append(t)
            gens.append({"gen": len(handoffs), "start_turn": t, "end_turn": None,
                         **gen_rec, "seed_text": None, "context": ctx})
            gen_start_index = len(lp.messages)
        beliefs.append(lp.belief(t, C.generation_of(t + 1, tuple(handoffs))))
    gens[-1]["end_turn"] = lp.turns
    gens[-1]["solution_at_end"] = await read_file(SOLUTION_PATH)
    beliefs = list(await asyncio.gather(*beliefs))
    post = await _post_probe(lp)
    rec = lp.record()
    rec.update(generations=gens, beliefs=beliefs, handoff_turns=handoffs, post_probe=post)
    return rec


async def _handoff(lp: Loop, arm: C.Arm, spec: Spec, summary_prev: str | None,
                   gen_start_index: int) -> tuple[list[dict], dict, str | None] | None:
    """The arm's handoff after the current turn: (next context dumps, generation record fields,
    the summary to update next time). None when a summary call failed (the chain ends)."""
    system = {"role": "system", "content": lp.messages[0].text}
    task = {"role": "user", "content": spec.task_text}
    if arm.notes_turns:
        notes_text = await lp.notes_round()
        if lp.end_reason is not None:
            return None
    if arm.kind == "summary":
        new_dumps = dumps_of(lp.messages)[gen_start_index:]
        if not summary_prev:                     # scratch arms have no prefix summary
            new_dumps = dumps_of(lp.messages)[1:]
        rec = await lp.summary(new_dumps, summary_prev)
        if rec["status"] != "ok":
            lp.end_reason = "summary_failed"
            return None
        if arm.notes_turns and not arm.keep_notes:
            await sandbox().exec(["rm", "-f", C.NOTES_PATH], user="root")
        text = rec["reply"].strip()
        return (H.summary_context(system, task, text, arm.pin_task),
                {"kind": "summary", "handoff_text": text}, text)
    if arm.kind == "notes":
        return (H.notes_context(system, task, notes_text),
                {"kind": "notes", "handoff_text": notes_text}, None)
    if arm.kind == "memfile":
        mem = await read_file(C.MEMORY_PATH)
        return (H.memfile_context(system, task, mem),
                {"kind": "memfile", "handoff_text": mem}, None)
    if arm.kind == "lastk":
        ctx, kept = H.lastk_context(system, task, dumps_of(lp.messages)[1:])
        return ctx, {"kind": "lastk", "handoff_text": H.serialize(kept, None)}, None
    raise ValueError(f"arm {arm.name} has no handoff")


# --- Inspect plumbing ----------------------------------------------------------------------------
@solver
def p44_solver() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        spec = _spec(state)
        try:
            rec = await (run_prefix(spec) if spec.stage == "prefix" else run_chain(spec))
        except Exception as e:  # noqa: BLE001 - recorded, not raised: the sample's error
            rec = {"error": repr(e)[:2000]}
        rec["final_solution_py"] = await read_file(SOLUTION_PATH)
        rec["final_test_solution_py"] = await read_file(TEST_PATH)
        store().set(STORE_REC, rec)
        state.completed = True
        return state

    return solve


@scorer(metrics=[mean()])
def collect() -> object:
    async def score(state: TaskState, target: Target) -> Score:
        rec = store().get(STORE_REC) or {}
        return Score(value=0, answer=str(rec.get("end_reason")),
                     metadata={"n_turns": rec.get("n_turns"), "error": rec.get("error")})

    return score


def p44_task(specs: list[Spec]) -> Task:
    if not specs:
        raise ValueError("empty batch")
    ids = [s.episode_id for s in specs]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate episode ids in the batch")
    samples = [Sample(id=s.episode_id, input="(set in the solver)", metadata=asdict(s))
               for s in specs]
    return Task(dataset=samples, solver=p44_solver(), scorer=collect(),
                sandbox=("docker", str(C.COMPOSE_FILE)),
                config=GenerateConfig(parallel_tool_calls=False,
                                      max_tool_output=X3C.OUTPUT_TRUNCATE_CHARS + 1024))


def generate_settings(model: C.Model) -> dict:
    gc = {**X3C.COMMON_GENERATE, **model.generate}
    return {k: v for k, v in gc.items() if k in GenerateConfig.model_fields}


def run_batch(specs: list[Spec], model: C.Model, log_dir: Path, max_samples: int = 6,
              model_obj=None) -> list[Path]:
    """One Inspect eval over `specs`; returns the new .eval log paths. Paid unless `model_obj`
    (a mockllm in tests) is given."""
    from inspect_ai import eval as inspect_eval

    mock = model_obj is not None
    if not mock and model.model_id not in X3C.MODELS:
        raise ValueError(f"{model.model_id} is not in x3.contracts.MODELS")
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    before = set(log_dir.glob("*.eval"))
    inspect_eval(p44_task(specs), model=model_obj or f"openrouter/{model.model_id}",
                 model_args={} if mock else {"provider": model.provider, "max_retries": 0},
                 log_dir=str(log_dir), max_samples=max_samples, display="plain",
                 fail_on_error=False, log_model_api=True, ctl_server=False,
                 **({} if mock else generate_settings(model)))
    return sorted(set(log_dir.glob("*.eval")) - before)


# --- extraction ----------------------------------------------------------------------------------
def billed_total(model_calls: list[dict]) -> float | None:
    if any(c.get("error") is None and c.get("billed_usd") is None for c in model_calls):
        return None
    return sum(c["billed_usd"] for c in model_calls if c.get("billed_usd") is not None)


def write_json(art: dict, path: Path) -> Path:
    """Atomic write; an existing file gets a .dupN sibling (P23's rule)."""
    base, p, n = path, path, 1
    while p.exists():
        p = base.with_name(f"{base.stem}.dup{n}.json")
        art["duplicate_of"] = str(base)
        n += 1
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(art, indent=1, default=str))
    tmp.replace(p)
    return p


def _tag(snapshots: list[dict], calls: list[dict], handoffs: tuple[int, ...], offset: int,
         turn0: int) -> None:
    """Renumber snapshot steps from `offset`, and give snapshots and calls turn and gen."""
    step_turn = {c["snapshot_step"]: c["turn"] for c in calls}
    remap = {}
    for i, s in enumerate(snapshots):
        old = s.get("step", i)
        remap[old] = offset + i
        s["step"] = offset + i
        s["turn"] = step_turn.get(old, turn0)
        s["gen"] = C.generation_of(s["turn"], handoffs) if s["turn"] else 0
    for c in calls:
        c["snapshot_step"] = remap.get(c["snapshot_step"], c["snapshot_step"])
        c["gen"] = C.generation_of(c["turn"], handoffs)
        c.setdefault("notes", False)


def assemble_chain(rec: dict, meta: dict, prefix: dict | None) -> dict:
    """The CHAIN artifact (contracts.py) from a chain or scratch record and, for chains, the
    prefix artifact it forked from."""
    arm = C.ARMS[meta["arm"]]
    handoffs = tuple(rec.get("handoff_turns") or ())
    gens = list(rec.get("generations") or [])
    snaps, calls = [], []
    if prefix is not None:
        uses_notes = arm.notes_turns
        p_snaps = json.loads(json.dumps(prefix["snapshots"]))
        p_calls = json.loads(json.dumps(prefix["calls"]))
        _tag(p_snaps, p_calls, handoffs, 0, 0)
        snaps += p_snaps
        calls += p_calls
        if uses_notes:
            n_snaps = json.loads(json.dumps(prefix["notes_snapshots"]))
            n_calls = json.loads(json.dumps(prefix["notes_calls"]))
            _tag(n_snaps, n_calls, handoffs, len(snaps), C.PREFIX_TURNS)
            for c in n_calls:
                c["notes"] = True
            snaps += n_snaps
            calls += n_calls
        gen0 = {"gen": 0, "start_turn": 0, "end_turn": C.PREFIX_TURNS, "kind": "full",
                "handoff_text": None, "seed_text": None, "context": [],
                "solution_at_end": prefix["solution_w4n" if uses_notes else "solution_w4"]}
        if gens and gens[0]["gen"] == 0:        # R: the prefix and the chain are one generation
            gens[0]["start_turn"] = 0
        else:
            gens = [gen0] + gens
    c_snaps = json.loads(json.dumps(rec.get("snapshots") or []))
    c_calls = json.loads(json.dumps(rec.get("calls") or []))
    _tag(c_snaps, c_calls, handoffs, len(snaps), C.PREFIX_TURNS if prefix else 0)
    snaps += c_snaps
    calls += c_calls
    n_notes = rec.get("n_notes_turns") or 0
    if prefix is not None and arm.notes_turns:
        n_notes += prefix.get("n_notes_turns") or 0
    return {"meta": meta, "end_reason": rec.get("end_reason"), "n_turns": rec.get("n_turns"),
            "n_notes_turns": n_notes, "handoff_turns": list(handoffs), "generations": gens,
            "beliefs": rec.get("beliefs") or [], "snapshots": snaps, "calls": calls,
            "final_solution_py": rec.get("final_solution_py"),
            "final_test_solution_py": rec.get("final_test_solution_py"),
            "post_probe": rec.get("post_probe"), "messages": rec.get("messages"),
            "questions": rec.get("questions"), "replay_mismatch": rec.get("replay_mismatch"),
            "n_nudges": rec.get("n_nudges"), "n_multi_call": rec.get("n_multi_call"),
            "spent_at_ceiling": rec.get("spent_at_ceiling"), "api_error": rec.get("api_error")}


def extract(eval_path: Path, *, write: bool = True) -> list[dict]:
    """One artifact per sample (prefix or chain), written under results/ unless `write` is
    False. Failed samples get `error` set."""
    import inspect_ai
    from inspect_ai.log import read_eval_log

    from x3.runner import _git_sha, model_calls_from_events

    log = read_eval_log(str(eval_path))
    git_sha = _git_sha()
    out = []
    for s in log.samples or []:
        md = s.metadata or {}
        rec = (s.store or {}).get(STORE_REC) or {}
        meta = {"episode_id": md.get("episode_id"), "prefix_id": md.get("prefix_id"),
                "task_id": md.get("task_id"), "run": md.get("run"), "arm": md.get("arm"),
                "seed": md.get("seed"), "model_id": md.get("model_id"),
                "model_slug": md.get("model_slug"), "stage": md.get("stage"),
                "git_sha": git_sha, "inspect_version": inspect_ai.__version__}
        model_calls = model_calls_from_events(s.events, md.get("model_id", ""))
        err = rec.get("error") or (repr(s.error)[:2000] if s.error else None)
        if md.get("stage") == "prefix":
            meta["arm"] = "PREFIX"
            art = {"meta": meta, **{k: v for k, v in rec.items() if k != "error"}}
            if art.get("snapshots") is not None:
                # turn and gen on the prefix's own snapshots (chains re-tag their copies)
                _tag(art["snapshots"], art.get("calls") or [], (), 0, 0)
            if art.get("ended"):
                # an ended prefix is scored as a chain of one generation (arm PREFIX_ENDED)
                art["generations"] = [{"gen": 0, "start_turn": 0, "end_turn": art.get("n_turns"),
                                       "kind": "full", "handoff_text": None, "seed_text": None,
                                       "context": [],
                                       "solution_at_end": rec.get("final_solution_py")}]
                art["handoff_turns"], art["beliefs"] = [], []
            path = C.prefix_path(md["model_slug"], md["prefix_id"])
        else:
            prefix = None
            if md.get("stage") == "chain":
                fam = C.MODELS[md["model_slug"]].prefix_slug if md["model_slug"] in C.MODELS \
                    else md["model_slug"]
                pp = C.prefix_path(fam, md["prefix_id"])
                prefix = json.loads(pp.read_text()) if pp.exists() else None
                if prefix is None:
                    err = err or f"prefix artifact missing: {pp}"
            art = assemble_chain(rec, meta, prefix) if (prefix or md.get("stage") == "scratch"
                                                         ) else {"meta": meta, **rec}
            path = C.chain_path(md["model_slug"], md["arm"], md["seed"], md["prefix_id"])
        art.update(model_calls=model_calls, billed_usd=billed_total(model_calls), error=err,
                   eval_log=str(eval_path))
        if write and md.get("episode_id"):
            art["path"] = str(write_json(art, path))
        out.append(art)
    return out


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()
