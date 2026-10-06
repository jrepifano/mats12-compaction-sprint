"""Inspect task for the X3 pilot (implementation plan D5, D6, §1.4–1.5; prereg §4.3–4.7).

One Inspect sample is one episode. Its metadata is an `EpisodeSpec` (written by the runner), so
one task can hold a mixed batch (e.g. a main-run block of six contexts). Per sample:

1. `setup` writes the workspace (description.md, solution.py, test_solution.py) and
   RUN_INFO.txt, chowns /agent to `dev`, and takes snapshot 0;
2. `episode` is our own agent loop, not Inspect's `react` (D5): a byte-bounded cost check before
   every call, one command per turn, a fixed 25-turn cap with no 26th call, the frozen nudge
   for turns without a tool call, and `done()` to finish;
3. the episode's messages are stored, then the post-episode report question is asked once with
   no tools, under the same pre-call bound (prereg §4.7);
4. `collect` stores the final solution and checks the record.

Billed cost, provider and response ids are read offline from the .eval log's model events
(`log_model_api=True`), as S4 did; see `x3.runner`.

X3b hooks (X3b implementation plan §1-§2; X3b prereg §4.5): optional `EpisodeSpec` fields, all
defaulting to X3's behavior, let X3b set its own run header (`header_text`), ask a pre-action
question on a forked copy of the first request (`pre_probe_prompt`), and change the post-episode
question, the probe envelope (shared by both probes), the probe token cap and the empty-reply
retry. With every hook at its default, X3's behavior and store are unchanged.
"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime

from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ContentReasoning,
    GenerateConfig,
    ModelOutput,
    get_model,
)
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, Solver, TaskState, chain, solver
from inspect_ai.tool import Tool, ToolCall, tool
from inspect_ai.util import sandbox, store

from x3 import contracts as C
from x3 import texts


# --- the per-episode spec (runner -> task) -------------------------------------------------
@dataclass(frozen=True)
class EpisodeSpec:
    episode_id: str
    model_id: str
    model_slug: str
    context: str
    prompt_level: str
    stage: str
    task_id: str
    repeat: int
    attempt: int
    batch_id: str
    run_order_pos: int
    episode_limit_usd: float
    in_ceiling: float                 # $/M tokens
    out_ceiling: float
    task: dict = field(default_factory=dict)          # asdict(TaskSpec)
    run_order_seed: int = C.SEED_RUN_ORDER
    # X3b hooks (X3b plan §2); the defaults reproduce X3 exactly
    header_text: str | None = None        # the four header lines only, "\n"-joined
    pre_probe_prompt: str | None = None
    post_probe_prompt: str | None = None  # default: texts.PROBE_PROMPT
    probe_bound_usd: float | None = None  # default: C.PROBE_BOUND_USD; covers BOTH probes
    probe_max_tokens: int | None = None   # default: C.PROBE_MAX_TOKENS
    retry_empty_probe: bool = False


HOOK_FIELDS = ("header_text", "pre_probe_prompt", "post_probe_prompt", "probe_bound_usd",
               "probe_max_tokens", "retry_empty_probe")
# = x3b.contracts.STORE_PRE_PROBE, written here so x3 does not import x3b
STORE_PRE_PROBE = "x3_pre_probe"


def hooks_set(spec: EpisodeSpec) -> bool:
    """True when any X3b hook field differs from its default (then both probes write the full
    X3b probe record and x3_meta carries the hook fields)."""
    defaults = {f.name: f.default for f in fields(EpisodeSpec) if f.name in HOOK_FIELDS}
    return any(getattr(spec, k) != v for k, v in defaults.items())


# --- snapshots (§1.4) ----------------------------------------------------------------------
_SNAPSHOT_PY = r"""
import fnmatch, hashlib, json, os, stat, sys
roots, names, suffixes, maxb, shadow, user_site = json.loads(sys.argv[1])
files = {}
for root in roots:
    for d, dirs, fs in os.walk(root):
        dirs.sort()
        for n in sorted(fs):
            p = os.path.join(d, n)
            try:
                st = os.lstat(p)
                if stat.S_ISLNK(st.st_mode):
                    t = os.readlink(p)
                    files[p] = {"sha256": "symlink:" + t, "mode": "symlink", "size": None,
                                "text": None, "link": t}
                    continue
                if not stat.S_ISREG(st.st_mode):
                    files[p] = {"sha256": "special:" + oct(st.st_mode), "mode": "special",
                                "size": None, "text": None}
                    continue
                b = open(p, "rb").read()
            except OSError:
                files[p] = {"sha256": None, "mode": None, "size": None, "text": None}
                continue
            keep = n in names or n.endswith(tuple(suffixes))
            files[p] = {"sha256": hashlib.sha256(b).hexdigest(), "mode": oct(st.st_mode & 0o7777),
                        "size": len(b),
                        "text": b.decode("utf-8", "replace") if keep and len(b) <= maxb else None}
auto = []
cands = [r for r in roots] + [user_site]
for base in cands:
    if not os.path.isdir(base):
        continue
    for d, dirs, fs in os.walk(base):
        for n in dirs + fs:
            if (n in ("sitecustomize.py", "usercustomize.py", "conftest.py") or n.endswith(".pth")
                    or n in shadow or n in [s + ".py" for s in shadow]):
                auto.append(os.path.join(d, n))
print(json.dumps({"files": files, "autoload": sorted(set(auto))}))
"""


async def take_snapshot(step: int) -> dict:
    """One snapshot, run as root so the agent cannot hide files from it."""
    arg = json.dumps([list(C.SNAPSHOT_ROOTS), list(C.TEXT_CAPTURE_NAMES),
                      list(C.TEXT_CAPTURE_SUFFIXES), C.TEXT_CAPTURE_MAX_BYTES,
                      list(C.SHADOW_PACKAGES), C.USER_SITE])
    try:
        r = await sandbox().exec(["python3", "-c", _SNAPSHOT_PY, arg], user="root")
        if r.returncode != 0:
            return {"step": step, "files": None, "autoload": None,
                    "error": f"rc={r.returncode} {r.stderr[-300:]}"}
        snap = json.loads(r.stdout)
    except (json.JSONDecodeError, TimeoutError, OSError, RuntimeError) as e:
        return {"step": step, "files": None, "autoload": None, "error": repr(e)}
    return {"step": step, **snap, "error": None}


# --- tools ---------------------------------------------------------------------------------
_TRUNC_NOTE = "\n[output truncated: showing the first {n} characters of {total}]\n"


def deliver(stdout: str, stderr: str) -> tuple[str, bool]:
    """The exact tool result (what we store is what the model sees) and whether it was cut."""
    text = (f"{stderr}\n" if stderr else "") + stdout
    if len(text) <= C.OUTPUT_TRUNCATE_CHARS:
        return text, False
    return (text[: C.OUTPUT_TRUNCATE_CHARS]
            + _TRUNC_NOTE.format(n=C.OUTPUT_TRUNCATE_CHARS, total=len(text))), True


@tool
def execute_command() -> Tool:
    async def execute(command: str) -> str:
        """
        Run one shell command in the sandbox and return its output.

        Args:
          command: The shell command to run.

        Returns:
          The command's output.
        """
        raise RuntimeError("executed by the X3 loop, not by Inspect")  # see _run_command

    return execute


@tool
def done() -> Tool:
    async def execute() -> str:
        """
        End the session.

        Returns:
          An empty string.
        """
        return ""

    return execute


TOOLS = [execute_command(), done()]


def _clock() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def _run_command(command: str, turn: int, calls: list, snapshots: list) -> str:
    box = sandbox()
    t_start = _clock()
    try:
        res = await box.exec(["bash", "-c", command], timeout=C.COMMAND_TIMEOUT_S,
                             cwd=C.WORKDIR, user=C.AGENT_USER, env={"HOME": C.HOME})
        stdout, stderr, code = res.stdout, res.stderr, res.returncode
    except TimeoutError:
        stdout, stderr, code = "", f"command timed out after {C.COMMAND_TIMEOUT_S}s", 124
    except UnicodeDecodeError:
        stdout, stderr, code = "", "command output was not valid UTF-8", 1
    t_end = _clock()
    snap = await take_snapshot(len(snapshots))
    snapshots.append(snap)
    text, truncated = deliver(stdout, stderr)
    calls.append({"turn": turn, "t_start": t_start, "t_end": t_end, "cmd": command,
                  "stdout": text, "stderr_tail": stderr[-2000:], "exit": code,
                  "truncated": truncated, "snapshot_step": snap["step"]})
    return text


# --- cost bound (D5) -----------------------------------------------------------------------
def input_bound_tokens(messages: list[ChatMessage], tools: list[Tool]) -> int:
    """Upper bound on input tokens: UTF-8 bytes of the serialized request plus per-message
    overhead. A byte-level BPE token covers at least one byte, so tokens <= bytes."""
    from inspect_ai.tool import ToolDef

    msgs = json.dumps([m.model_dump(exclude_none=True) for m in messages], ensure_ascii=False)
    tool_json = json.dumps([ToolDef(t).name + ToolDef(t).description
                            + json.dumps(ToolDef(t).parameters.model_dump(exclude_none=True))
                            for t in tools], ensure_ascii=False)
    return (len(msgs.encode("utf-8")) + len(tool_json.encode("utf-8"))
            + C.TOKENS_PER_MESSAGE_OVERHEAD * len(messages))


# Retries happen only in this loop (code review r2, finding 1): Inspect's retries are off
# (max_retries=0) and so are the OpenAI SDK's (runner passes max_retries=0 in model_args), so
# every attempt is one request we can see. A failed attempt may still have billed, so it is
# charged its whole single-attempt bound. The pre-call check leaves room for all ATTEMPTS.
MAX_RETRIES = 2
RETRY_BACKOFF_S = (10, 30)
ATTEMPTS = MAX_RETRIES + 1
ATTEMPT_TIMEOUT_S = 600


def call_bound(input_bound: int, max_tokens: int, in_ceiling: float, out_ceiling: float) -> float:
    """Worst case of one call, all attempts included."""
    return ATTEMPTS * (input_bound * in_ceiling + max_tokens * out_ceiling) / 1e6


INCREMENTAL_MARGIN_TOKENS = 64


def tighter_bound(byte_bound: int, prev: tuple[int, int, int] | None,
                  messages: list[ChatMessage]) -> int:
    """A tighter upper bound once a call has run (smoke test, 2026-10-04: the byte bound alone
    left no room for the post-episode question). The next request's input is the previous
    request's input (reported tokens), plus the previous reply (at most its reported output
    tokens, reasoning included), plus the messages added since (bounded by their bytes).
    `prev` = (input tokens, output tokens, len(messages) right after the previous reply)."""
    if prev is None:
        return byte_bound
    n_in, n_out, upto = prev
    added = input_bound_tokens(messages[upto:], []) if len(messages) > upto else 0
    return min(byte_bound, n_in + n_out + added + INCREMENTAL_MARGIN_TOKENS)


def usage_tokens(output: ModelOutput) -> tuple[int, int] | None:
    u = output.usage
    if u is None:
        return None
    n_in = u.input_tokens + (u.input_tokens_cache_read or 0) + (u.input_tokens_cache_write or 0)
    return n_in, u.output_tokens


def call_cost(output: ModelOutput, in_ceiling: float, out_ceiling: float,
              bound: float) -> float:
    """Cost of a completed call at the ceiling prices (>= the billed cost). With no usage
    reported, the call's whole bound is charged (code review r1, finding 2)."""
    u = output.usage
    if u is None:
        return bound
    n_in = u.input_tokens + (u.input_tokens_cache_read or 0) + (u.input_tokens_cache_write or 0)
    return (n_in * in_ceiling + u.output_tokens * out_ceiling) / 1e6


def affordable_max_tokens(spent: float, limit: float, input_bound: int, in_ceiling: float,
                          out_ceiling: float, cap: int, attempts: int = ATTEMPTS) -> int:
    """Largest output cap such that spent + call_bound(...) <= limit."""
    room = (limit - spent) / attempts - input_bound * in_ceiling / 1e6
    if room <= 0:
        return 0
    return min(cap, math.floor(room * 1e6 / out_ceiling))


# --- message helpers -----------------------------------------------------------------------
def reasoning_text(msg: ChatMessageAssistant) -> tuple[str, bool]:
    """(readable reasoning text, any block redacted)."""
    if isinstance(msg.content, str):
        return "", False
    parts, redacted = [], False
    for c in msg.content:
        if isinstance(c, ContentReasoning):
            if c.redacted:
                redacted = True
                if c.summary:
                    parts.append(c.summary)
            else:
                parts.append(c.reasoning)
    return "\n".join(p for p in parts if p), redacted


# --- solvers -------------------------------------------------------------------------------
def _spec(state: TaskState) -> EpisodeSpec:
    return EpisodeSpec(**state.metadata)


@solver
def setup() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        from x3.tasks import build_workspace  # U1; imported per sample

        spec = _spec(state)
        task = C.TaskSpec(**{**spec.task, "conflict_pair": tuple(spec.task["conflict_pair"])})
        box = sandbox()
        await box.exec(["mkdir", "-p", C.WORKDIR], user="root")
        for name, body in build_workspace(task).items():
            await box.write_file(f"{C.WORKDIR}/{name}", body)
        level = C.PromptLevel(spec.prompt_level)
        if spec.header_text is not None:
            # X3b (plan §2): its header names are not Context members, so never convert
            run_info = spec.header_text + "\n"
            first_user = spec.header_text + "\n\n" + texts.TASK_TEMPLATE.format(
                strictness=texts.STRICTNESS[level])
        else:
            ctx = C.Context(spec.context)
            run_info = texts.run_info(ctx)
            first_user = texts.user_prompt(ctx, level)
        await box.write_file(f"{C.WORKDIR}/{texts.RUN_INFO_FILENAME}", run_info)
        await box.exec(["chown", "-R", f"{C.AGENT_USER}:{C.AGENT_USER}", C.WORKDIR],
                       user="root")
        snap0 = await take_snapshot(0)
        s = store()
        s.set(C.STORE_SNAPSHOTS, [snap0])
        s.set(C.STORE_CALLS, [])
        meta = {**asdict(spec), "task": None, "test_sha256": task.test_sha256}
        if not hooks_set(spec):      # X3's stored metadata stays byte-identical (X3b plan §2)
            for k in HOOK_FIELDS:
                del meta[k]
        s.set(C.STORE_META, meta)
        state.messages = [ChatMessageSystem(content=texts.SYSTEM_PROMPT),
                          ChatMessageUser(content=first_user)]
        return state

    return solve


async def _generate(messages: list[ChatMessage], tools: list[Tool], max_tokens: int,
                    tool_choice: str | None = None) -> ModelOutput:
    cfg = GenerateConfig(max_tokens=max_tokens, parallel_tool_calls=False,
                         max_retries=0, attempt_timeout=ATTEMPT_TIMEOUT_S)
    if tool_choice == "none":
        return await get_model().generate(messages, tools=[], config=cfg)
    return await get_model().generate(messages, tools=tools, config=cfg)


async def generate_with_retries(messages: list[ChatMessage], tools: list[Tool], max_tokens: int,
                                attempt_bound: float, tool_choice: str | None = None,
                                sleep=asyncio.sleep) -> tuple[ModelOutput | None, float, str]:
    """Up to ATTEMPTS single-request attempts. Returns (output or None, extra cost charged for
    failed attempts, last error). Each failed attempt is charged `attempt_bound`."""
    extra, err = 0.0, ""
    for attempt in range(ATTEMPTS):
        try:
            return await _generate(messages, tools, max_tokens, tool_choice), extra, err
        except Exception as e:  # noqa: BLE001 - any API failure
            extra += attempt_bound
            err = repr(e)[:2000]
            if attempt < ATTEMPTS - 1:
                await sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
    return None, extra, err


def _attempt(kind: str, input_bound: int, max_tokens: int, charge: float,
             usage: dict | None = None, error: str | None = None) -> dict:
    return {"kind": kind, "input_bound": input_bound, "max_tokens": max_tokens,
            "charge": charge, "usage": usage, "error": error}


async def run_probe(base: list[ChatMessage], prompt: str, *, spent_before: float,
                    envelope: float, cap: int, in_ceiling: float, out_ceiling: float,
                    retry_empty: bool, prev: tuple[int, int, int] | None = None,
                    sleep=asyncio.sleep) -> dict:
    """One probe question on `base + [prompt]`, no tools (X3b plan §1 probe record; X3b
    prereg §4.5). Sized so that `spent_before` plus this probe's worst case stays within
    `envelope`; every attempt is recorded with its charge: its ceiling cost from usage when
    usage is reported, else its single-attempt bound (a transport failure is always charged
    its bound, as `generate_with_retries` does). An empty reply is retried once when
    `retry_empty`, sized afresh against what is left; if the retry no longer fits, or fails in
    transport, the empty reply is kept. `base` is never modified."""
    pmsgs = list(base) + [ChatMessageUser(content=prompt)]
    ib = tighter_bound(input_bound_tokens(pmsgs, []), prev, pmsgs)
    attempts: list[dict] = []
    rec: dict = {"prompt": prompt, "reply": None, "status": None, "usage": None, "retries": 0,
                 "input_bound": ib, "max_tokens": 0, "failed_attempt_charge": 0.0,
                 "spent_at_ceiling": 0.0, "attempts": attempts}
    for n in range(2 if retry_empty else 1):
        spent = spent_before + sum(a["charge"] for a in attempts)
        mt = affordable_max_tokens(spent, envelope, ib, in_ceiling, out_ceiling, cap)
        if mt < C.MIN_PROBE_MAX_TOKENS:
            if n == 0:
                rec.update(status="skipped_budget", max_tokens=mt)
            break                    # a retry that no longer fits keeps the empty reply
        rec["retries"] = n
        one = call_bound(ib, mt, in_ceiling, out_ceiling) / ATTEMPTS
        out, err = None, ""
        for k in range(ATTEMPTS):
            try:
                out = await _generate(pmsgs, [], mt, tool_choice="none")
                break
            except Exception as e:  # noqa: BLE001 - any API failure
                err = repr(e)[:2000]
                attempts.append(_attempt("transport_error", ib, mt, one, error=err))
                if k < ATTEMPTS - 1:
                    await sleep(RETRY_BACKOFF_S[min(k, len(RETRY_BACKOFF_S) - 1)])
        if out is None:
            rec["error"] = err
            if n == 0:
                rec.update(status="api_error", max_tokens=mt)
            break
        text = out.message.text
        empty = not text.strip()
        usage = out.usage.model_dump() if out.usage else None
        attempts.append(_attempt("empty" if empty else "ok", ib, mt,
                                 call_cost(out, in_ceiling, out_ceiling, one), usage=usage))
        rec.update(reply=text, status="empty" if empty else "ok", usage=usage, max_tokens=mt)
        if not empty:
            break
    rec["failed_attempt_charge"] = sum(a["charge"] for a in attempts
                                       if a["kind"] == "transport_error")
    rec["spent_at_ceiling"] = sum(a["charge"] for a in attempts)
    return rec


@solver
def episode() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        spec = _spec(state)
        s = store()
        calls, snapshots = s.get(C.STORE_CALLS, []), s.get(C.STORE_SNAPSHOTS, [])
        messages: list[ChatMessage] = list(state.messages)
        spent, turns, nudges, multi = 0.0, 0, 0, 0
        prev: tuple[int, int, int] | None = None
        end_reason: str | None = None
        bounds: list[dict] = []
        hooks = hooks_set(spec)
        p_bound = C.PROBE_BOUND_USD if spec.probe_bound_usd is None else spec.probe_bound_usd
        p_cap = C.PROBE_MAX_TOKENS if spec.probe_max_tokens is None else spec.probe_max_tokens
        pre_spent = 0.0
        if spec.pre_probe_prompt is not None:
            # X3b pre-action fork (X3b prereg §4.5, §4.9): a copy of [system, first user] plus
            # the question; never touches `messages`, `spent` or `prev`
            fork = [m.model_copy(deep=True) for m in messages[:2]]
            pre = await run_probe(fork, spec.pre_probe_prompt, spent_before=0.0,
                                  envelope=p_bound, cap=p_cap, in_ceiling=spec.in_ceiling,
                                  out_ceiling=spec.out_ceiling,
                                  retry_empty=spec.retry_empty_probe)
            s.set(STORE_PRE_PROBE, pre)
            pre_spent = pre["spent_at_ceiling"]
        while end_reason is None:
            if turns >= C.TURN_CAP:
                end_reason = C.EndReason.TURN_CAP.value
                break
            ib = tighter_bound(input_bound_tokens(messages, TOOLS), prev, messages)
            mt = affordable_max_tokens(spent, spec.episode_limit_usd, ib, spec.in_ceiling,
                                       spec.out_ceiling, C.MAX_TOKENS_PER_TURN)
            bounds.append({"turn": turns + 1, "input_bound": ib, "max_tokens": mt,
                           "spent": spent})
            if mt < C.MIN_CALL_MAX_TOKENS:
                end_reason = C.EndReason.COST_CAP.value
                break
            one = call_bound(ib, mt, spec.in_ceiling, spec.out_ceiling) / ATTEMPTS
            out, extra, err = await generate_with_retries(messages, TOOLS, mt, one)
            spent += extra
            bounds[-1]["failed_charge"] = extra
            if out is None:
                end_reason = C.EndReason.API_ERROR.value
                s.set("x3_api_error", err)
                break
            turns += 1
            spent += call_cost(out, spec.in_ceiling, spec.out_ceiling, one)
            msg = out.message
            messages.append(msg)
            ut = usage_tokens(out)
            prev = (ut[0], ut[1], len(messages)) if ut else None
            if out.stop_reason in ("model_length", "content_filter"):
                end_reason = out.stop_reason
                break
            tcs: list[ToolCall] = list(msg.tool_calls or [])
            if not tcs:
                nudges += 1
                messages.append(ChatMessageUser(content=texts.NUDGE))
                continue
            first, rest = tcs[0], tcs[1:]
            if first.function == "done":
                result = ""
                end_reason = C.EndReason.DONE.value
            elif first.function == "execute_command":
                cmd = first.arguments.get("command")
                if not isinstance(cmd, str):
                    result = "error: execute_command needs a string argument `command`"
                else:
                    result = await _run_command(cmd, turns, calls, snapshots)
            else:
                result = f"error: unknown tool {first.function!r}"
            messages.append(ChatMessageTool(content=result, tool_call_id=first.id,
                                            function=first.function))
            for tc in rest:
                multi += 1
                messages.append(ChatMessageTool(content=texts.MULTI_CALL_RESULT,
                                                tool_call_id=tc.id, function=tc.function))
        s.set(C.STORE_CALLS, calls)
        s.set(C.STORE_SNAPSHOTS, snapshots)
        first_asst = next((m for m in messages if isinstance(m, ChatMessageAssistant)), None)
        r_text, redacted = reasoning_text(first_asst) if first_asst else ("", False)
        final_asst = next((m for m in reversed(messages)
                           if isinstance(m, ChatMessageAssistant)), None)
        s.set(C.STORE_EPISODE, {
            "messages": [m.model_dump(exclude_none=True) for m in messages],
            "end_reason": end_reason, "n_turns": turns, "n_nudges": nudges,
            "n_multi_call": multi, "spent_at_ceiling": spent, "bounds": bounds,
            "n_failed_attempt_calls": sum(1 for b in bounds if b.get("failed_charge")),
            "first_turn": {"reasoning": r_text, "visible": first_asst.text if first_asst else "",
                           "redacted": redacted,
                           "reasoning_readable": bool(r_text) and not redacted},
            "final_message": final_asst.text if final_asst else ""})
        # the post-episode question (prereg §4.7), never counted against the episode limit
        if hooks:
            # X3b (plan §2): its prompt and cap; the envelope less what the pre-probe spent
            prompt = (texts.PROBE_PROMPT if spec.post_probe_prompt is None
                      else spec.post_probe_prompt)
            probe = await run_probe(messages, prompt, spent_before=pre_spent, envelope=p_bound,
                                    cap=p_cap, in_ceiling=spec.in_ceiling,
                                    out_ceiling=spec.out_ceiling,
                                    retry_empty=spec.retry_empty_probe, prev=prev)
            s.set(C.STORE_PROBE, probe)
            state.messages = messages
            state.completed = True
            return state
        pmsgs = messages + [ChatMessageUser(content=texts.PROBE_PROMPT)]
        pib = tighter_bound(input_bound_tokens(pmsgs, []), prev, pmsgs)
        pmt = affordable_max_tokens(0.0, C.PROBE_BOUND_USD, pib, spec.in_ceiling,
                                    spec.out_ceiling, C.PROBE_MAX_TOKENS)
        probe = {"prompt": texts.PROBE_PROMPT, "reply": None, "status": None,
                 "input_bound": pib, "max_tokens": pmt}
        if pmt < C.MIN_PROBE_MAX_TOKENS:
            probe["status"] = "skipped_budget"
        else:
            pone = call_bound(pib, pmt, spec.in_ceiling, spec.out_ceiling) / ATTEMPTS
            pout, pextra, perr = await generate_with_retries(pmsgs, [], pmt, pone,
                                                             tool_choice="none")
            probe["failed_attempt_charge"] = pextra
            if pout is None:
                probe["status"] = "api_error"
                probe["error"] = perr
            else:
                probe["reply"] = pout.message.text
                probe["status"] = "ok" if pout.message.text.strip() else "empty"
                probe["usage"] = pout.usage.model_dump() if pout.usage else None
        s.set(C.STORE_PROBE, probe)
        state.messages = messages
        state.completed = True
        return state

    return solve


@scorer(metrics=[mean()])
def collect() -> object:
    """Store the final solution and flag incomplete records; scoring proper is offline."""
    async def score(state: TaskState, target: Target) -> Score:
        s = store()
        errors: list[str] = []
        try:
            r = await sandbox().exec(["cat", f"{C.WORKDIR}/solution.py"], user="root")
            final = r.stdout if r.returncode == 0 else None
            if final is None:
                errors.append(f"could not read solution.py: rc={r.returncode}")
        except (TimeoutError, OSError, RuntimeError, UnicodeDecodeError) as e:
            final, _ = None, errors.append(f"could not read solution.py: {e!r}")
        s.set("x3_final_solution_py", final)
        ep = s.get(C.STORE_EPISODE) or {}
        if not ep:
            errors.append("episode record missing")
        if any(sn.get("files") is None for sn in s.get(C.STORE_SNAPSHOTS, [])):
            errors.append("a snapshot failed")
        s.set("x3_collect_errors", errors)
        return Score(value=0, answer=str(ep.get("end_reason")),
                     metadata={"n_turns": ep.get("n_turns"), "errors": errors})

    return score


def x3_task(specs: list[EpisodeSpec]) -> Task:
    """One Inspect task for a batch of episodes (any mix of contexts, tasks and repeats)."""
    if not specs:
        raise ValueError("empty batch")
    samples = [Sample(id=sp.episode_id, input="(set in setup)", metadata=asdict(sp))
               for sp in specs]
    return Task(dataset=samples, solver=chain(setup(), episode()), scorer=collect(),
                sandbox=("docker", str(C.COMPOSE_FILE)),
                config=GenerateConfig(parallel_tool_calls=False,
                                      max_tool_output=C.OUTPUT_TRUNCATE_CHARS + 1024))
