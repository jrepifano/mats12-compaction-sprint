"""The S2 agent loop: a base run to the first compaction, then the resume arms (spec "Run
protocol").

Base run (`run_base`): system prompt, [the re-attached root AGENTS.md], the task prompt, [the
chat rule turn]; then agent calls. Each call is one model request with the tools bash and
ask_user; every tool call is executed in order and answered by a tool message; a reply without
a tool call gets a format nudge; `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` submits. After
each call: a trap snapshot `turn-<k>`, then any scripted follow-up due at that call. The context
estimate is the last reply's prompt + completion tokens plus the estimated tokens appended
since. When it reaches the threshold at the end of a call, that is the boundary: quiesce, trap
`fork` snapshot, `docker commit`, then the model's paired summarizer builds the post-compaction
context. A base run that submits, hits BASE_MAX_CALLS, its run cap, or an error first has no
boundary (tier "attempted" only).

Arms (`run_arm`): F = the pre-boundary history; S = the post-compaction context (in this loop it
is exactly what the base run itself would have continued with, so there is no separate native
continuation N); S+R and S+I = S with the restore or matched-irrelevant block appended to the
summary message. Each arm forks the boundary image, runs up to CONTINUATION_CALLS calls (no
further compaction; crossing the threshold is flagged `would_recompact`), then the closing turn
"Commit your work now." and up to CLOSING_CALLS calls; then `env.finish()`.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

import jinja2

from compaction.s2 import compact as K
from compaction.s2 import config as C
from compaction.s2 import followups as F
from compaction.s2 import mini_prompts as MP
from compaction.s2 import rules as R
from compaction.s2.env import TaskEnv
from compaction.s2.model import (
    ChatModel,
    ModelError,
    RunCapReached,
    est_tokens,
    normalize_assistant,
)
from compaction.s2.tasks import TaskInfo
from compaction.traps.ask_user import OPENAI_TOOL as ASK_USER_TOOL

# mini-swe-agent's templates and bash tool, verbatim (compaction.s2.mini_prompts; Q10), plus
# unit (d)'s ask_user. Rendered with Jinja2 and StrictUndefined, as mini-swe-agent does.
_JINJA = jinja2.Environment(undefined=jinja2.StrictUndefined)
SYSTEM_PROMPT = _JINJA.from_string(MP.SYSTEM_TEMPLATE).render()
TOOLS = [MP.BASH_TOOL, ASK_USER_TOOL]
NO_TOOL_CALL_ERROR = ("No tool calls found in the response. Every response MUST include at least "
                      "one tool call.")
DEFAULT_UNAME = {"system": "Linux", "release": "", "version": "", "machine": "x86_64"}


def instance_message(task_text: str, uname: dict | None) -> str:
    """mini-swe-agent's instance template with the task and the *container's* uname (upstream
    renders the agent host's platform.uname(); our runner is a Mac, the sandbox is Linux)."""
    u = {**DEFAULT_UNAME, **(uname or {})}
    return _JINJA.from_string(MP.INSTANCE_TEMPLATE).render(task=task_text, **u)


def observation_message(rc: int, output: str, timed_out: bool) -> str:
    """mini-swe-agent's observation template. A command that hit our in-container timeout is
    shown as upstream shows a timeout: returncode -1 and an exception_info line."""
    out = {"output": output, "returncode": rc, "exception_info": ""}
    if timed_out:
        out = {"output": output, "returncode": -1,
               "exception_info": "An error occurred while executing the command: Command timed "
                                 f"out after {C.CMD_TIMEOUT_S} seconds"}
    return _JINJA.from_string(MP.OBSERVATION_TEMPLATE).render(output=out)


def format_error_message(error: str, has_tool_calls: bool, finish_reason: str | None) -> str:
    return _JINJA.from_string(MP.FORMAT_ERROR_TEMPLATE).render(
        error=error, actions=[], has_tool_calls=has_tool_calls, finish_reason=finish_reason)


def is_submission(output: str, rc: int) -> bool:
    """mini-swe-agent's rule: the first output line is the token and the command succeeded."""
    lines = output.lstrip().splitlines()
    return bool(lines) and lines[0].strip() == C.SUBMIT_TOKEN and rc == 0


END_SUBMIT, END_MAX_CALLS, END_CAP, END_WINDOW = "submitted", "max_calls", "run_cap", "window"
END_ERROR, END_BOUNDARY, END_DONE = "error", "boundary", "continuation_done"
END_STOPPED = "stopped"
# Set by the runner on SIGTERM or Ctrl-C: every loop ends at its next call boundary, so worker
# threads unwind through their `finally` blocks and every container is removed.
STOP = threading.Event()


class Stopped(RuntimeError):
    pass


@dataclass
class RunConfig:
    run_key: str
    task: TaskInfo
    route: C.Route
    channel: str
    threshold: int
    script: F.Script
    out_dir: Path
    policy: str = ""
    base_max_calls: int = C.BASE_MAX_CALLS
    run_cap_usd: float = 0.0
    continuation_calls: int = C.CONTINUATION_CALLS
    closing_calls: int = C.CLOSING_CALLS
    stop_event: threading.Event | None = None  # the run's host went down: stop at next call

    def __post_init__(self) -> None:
        self.policy = self.policy or self.route.summarizer
        self.out_dir = Path(self.out_dir)


@dataclass
class Loop:
    """State of one agent conversation (base run or arm)."""

    messages: list[dict]
    model: ChatModel
    env: TaskEnv
    purpose: str
    calls: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    ctx_tokens: int = 0
    n_calls: int = 0
    submitted: bool = False
    asks: int = 0
    bash_calls: int = 0
    stop: threading.Event | None = None

    def append(self, msgs: list[dict]) -> None:
        self.messages.extend(msgs)
        self.ctx_tokens += sum(est_tokens(m, self.model.route.bytes_per_token) for m in msgs)

    def step(self, snapshot_label: str | None) -> None:
        """One agent call and its tool calls. Raises ModelError on an API failure and Stopped
        when the runner is shutting down."""
        if STOP.is_set():
            raise Stopped("runner stopping")
        if self.stop is not None and self.stop.is_set():
            raise Stopped("host down")
        r = self.model.route
        t0 = time.monotonic()
        try:
            reply = self.model.complete(list(self.messages), TOOLS, r.max_output,
                                        purpose=f"{self.purpose}:{self.n_calls + 1}")
        except ModelError as e:
            self.cost_usd += e.cost_usd  # failed attempts are spend too
            raise
        self.n_calls += 1
        u = reply.usage
        self.cost_usd += reply.cost_total_usd
        msg = normalize_assistant(reply.message, self.n_calls)
        self.messages.append(msg)
        if u.prompt_tokens:
            self.ctx_tokens = u.prompt_tokens + (u.completion_tokens or 0)
        else:
            self.ctx_tokens += est_tokens(msg, r.bytes_per_token)
        out: list[dict] = []
        tools_log = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            try:
                args = json.loads(fn["arguments"] or "{}")
            except ValueError:
                args = {}
            if fn["name"] == "bash" and isinstance(args.get("command"), str) \
                    and "raw_arguments" not in args:
                cmd = args["command"]
                self.bash_calls += 1
                o = self.env.run_agent_command(cmd)
                obs_text = observation_message(o.rc, o.output, o.timed_out)
                sub = is_submission(o.output, o.rc)
                self.submitted = self.submitted or sub
                tools_log.append({"tool": "bash", "cmd": cmd[:2000], "rc": o.rc,
                                  "seconds": round(o.seconds, 3), "timed_out": o.timed_out,
                                  "output_chars": len(o.output), "submit": sub})
            elif fn["name"] == "ask_user":
                self.asks += 1
                obs_text = self.env.ask_user(str(args.get("question") or ""),
                                             str(args.get("action") or "") or None)
                tools_log.append({"tool": "ask_user", "question": args.get("question"),
                                  "action": args.get("action")})
            else:
                err = ""
                if "raw_arguments" in args:
                    err += "Error parsing tool call arguments: not valid JSON."
                if fn["name"] not in ("bash", "ask_user"):
                    err += f"Unknown tool '{fn['name']}'."
                elif fn["name"] == "bash" and not isinstance(args.get("command"), str):
                    err += "Missing 'command' argument in bash tool call."
                obs_text = format_error_message(err.strip(), True, reply.finish_reason)
                tools_log.append({"tool": fn["name"], "error": err.strip()})
            out.append({"role": "tool", "tool_call_id": tc["id"], "content": obs_text})
        if not msg.get("tool_calls"):
            out.append({"role": "user", "content": format_error_message(
                NO_TOOL_CALL_ERROR, False, reply.finish_reason)})
        self.append(out)
        snap_s = self.env.trap_snapshot(snapshot_label) if snapshot_label else None
        self.calls.append({
            "call": self.n_calls, "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens, "cached_tokens": u.cached_tokens,
            "reasoning_tokens": u.reasoning_tokens, "cost_usd": u.cost_usd,
            "cost_total_usd": reply.cost_total_usd,
            "cost_source": u.cost_source, "finish_reason": reply.finish_reason,
            "attempts": reply.attempts, "latency_s": round(reply.latency_s, 3),
            "ctx_tokens_after": self.ctx_tokens, "tools": tools_log,
            "trap_snapshot_s": snap_s, "seconds": round(time.monotonic() - t0, 3)})


def opening_messages(cfg: RunConfig, rules: R.TaskRules, agents_text: str | None,
                     uname: dict | None = None) -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    if agents_text:
        msgs.append(K.instruction_message(agents_text))
    msgs.append({"role": "user", "content": instance_message(
        R.task_prompt(cfg.task.instruction, cfg.channel), uname)})
    if cfg.channel == "chat":
        msgs.append({"role": "user", "content": rules.chat_turn()})
    return msgs


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


@dataclass
class BaseResult:
    run_key: str
    end: str
    n_calls: int
    cost_usd: float
    ctx_tokens: int
    followups_delivered: int
    followup_tokens_delivered: int
    boundary: dict | None = None
    rules: dict | None = None
    error: str | None = None
    seconds: float = 0.0
    timings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def run_base(cfg: RunConfig, model: ChatModel, inv, *, image: str | None = None) -> BaseResult:
    """Run the base trajectory to the first compaction (or its end), and build the boundary."""
    t0 = time.monotonic()
    out = cfg.out_dir / "base"
    env = None
    loop = None
    res = BaseResult(cfg.run_key, END_ERROR, 0, 0.0, 0, 0, 0)
    try:
        env, rules, agents = TaskEnv.create(inv, cfg.task, cfg.channel, out, image=image)
        res.rules = rules.to_dict()
        _dump(cfg.out_dir / "rules.json", rules.to_dict())
        _dump(cfg.out_dir / "consent_spec.json", R.consent_spec(rules, cfg.channel).to_dict())
        loop = Loop(opening_messages(cfg, rules, agents, getattr(env, "uname", None)), model,
                    env, "base",
                    stop=cfg.stop_event)
        loop.ctx_tokens = est_tokens(loop.messages, model.route.bytes_per_token)
        delivered, dtok = 0, 0
        while True:
            if loop.n_calls >= cfg.base_max_calls:
                res.end = END_MAX_CALLS
                break
            if cfg.run_cap_usd and loop.cost_usd >= cfg.run_cap_usd:
                res.end = END_CAP
                break
            if loop.ctx_tokens + model.route.max_output > model.route.window:
                res.end = END_WINDOW
                break
            loop.step(f"turn-{loop.n_calls + 1}")
            if loop.submitted:
                res.end = END_SUBMIT
                break
            for fu in cfg.script.due(loop.n_calls):
                loop.append([{"role": "user", "content": fu.text}])
                delivered += 1
                dtok += fu.est_tokens
            if loop.ctx_tokens >= cfg.threshold:
                res.end = END_BOUNDARY
                break
        res.followups_delivered, res.followup_tokens_delivered = delivered, dtok
        if res.end == END_BOUNDARY:
            res.boundary = make_boundary(cfg, loop, env, inv, rules, agents)
    except RunCapReached as e:
        res.end, res.error = END_CAP, f"RunCapReached: {e}"
    except ModelError as e:
        res.end, res.error = END_ERROR, f"ModelError: {e}"
    except Stopped as e:
        res.end, res.error = END_STOPPED, str(e)
    except Exception:  # noqa: BLE001 - recorded; the container is still removed below
        res.end, res.error = END_ERROR, traceback.format_exc()[-6000:]
    finally:
        if loop is not None:
            res.n_calls, res.cost_usd, res.ctx_tokens = loop.n_calls, loop.cost_usd, loop.ctx_tokens
            _dump(out / "messages.json", loop.messages)
            _dump(out / "calls.json", loop.calls)
        if env is not None:
            res.timings = env.timings
            _dump(out / "env_log.json", env.log)
            env.close()
        res.seconds = round(time.monotonic() - t0, 3)
        _dump(out / "result.json", res.to_dict())
    return res


def make_boundary(cfg: RunConfig, loop: Loop, env: TaskEnv, inv, rules: R.TaskRules,
                  agents: str | None) -> dict:
    bdir = cfg.out_dir / "boundary"
    snap = env.boundary(inv, f"b-{cfg.run_key[-40:]}")
    pre = list(loop.messages)
    try:
        comp = K.compact(cfg.policy, pre, loop.model, run_id=cfg.run_key,
                         instruction_text=agents)
    except ModelError as e:
        loop.cost_usd += e.cost_usd
        raise
    loop.cost_usd += comp.cost_usd
    post_text = "\n".join(K._text(m.get("content")) for m in comp.post_messages)
    precheck = R.retention_precheck(post_text, rules)
    verbatim = R.verbatim_rules(post_text, rules)
    post_tokens = est_tokens(comp.post_messages, loop.model.route.bytes_per_token)
    record = K.boundary_record(cfg.run_key, pre, model=loop.model.route.model,
                               pre_tokens=loop.ctx_tokens, summary=comp.summary,
                               rules=[{"text": rules.lines[r], "origin": "user", "checkable": True,
                                       "id": r} for r in R.RULE_IDS])
    _dump(bdir / "pre_messages.json", pre)
    _dump(bdir / "compaction.json", comp.to_dict())
    _dump(bdir / "post_messages.json", comp.post_messages)
    _dump(bdir / "record.json", record)
    b = {"tag": snap["tag"], "stopped_processes": snap["stopped_processes"],
         "snapshot_s": snap["snapshot_s"], "pre_tokens": loop.ctx_tokens,
         "post_tokens_est": post_tokens, "n_pre_messages": len(pre),
         "summary_chars": len(comp.summary), "summary_index": comp.summary_index,
         "kept_indices": comp.kept_indices, "precheck": precheck, "verbatim": verbatim,
         "lost_certain": [r for r, v in precheck.items() if v == "lost_certain"],
         "compaction_cost_usd": comp.cost_usd, "policy": comp.policy}
    _dump(bdir / "boundary.json", b)
    return b


# ------------------------------------------------------------------------------------------
# Arms
# ------------------------------------------------------------------------------------------


def arm_slug(arm: str) -> str:
    return {"F": "F", "S": "S", "S+R": "SR", "S+I": "SI"}[arm]


def arm_context(arm: str, cfg: RunConfig, lost: list[str], rules: R.TaskRules) -> list[dict]:
    bdir = cfg.out_dir / "boundary"
    if arm == "F":
        return json.loads((bdir / "pre_messages.json").read_text())
    comp = json.loads((bdir / "compaction.json").read_text())
    post = comp["post_messages"]
    if arm == "S":
        return post
    if not lost:
        raise ValueError(f"arm {arm} needs the lost rule(s)")
    block = (R.restore_block(lost, rules) if arm == "S+R" else R.irrelevant_block(lost, rules))
    return K.with_block(post, comp["summary_index"], block)


@dataclass
class ArmResult:
    run_key: str
    arm: str
    rep: int
    end: str
    n_calls: int
    cost_usd: float
    submitted_before_closing: bool
    closing_sent: bool
    would_recompact: bool
    asks: int
    leftover_committed: bool | None = None
    traps_home: str | None = None
    patch_bytes: int | None = None
    error: str | None = None
    seconds: float = 0.0
    timings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_DUMP_LOCK = threading.Lock()


def run_arm(cfg: RunConfig, arm: str, rep: int, model: ChatModel, inv, *, lost: list[str],
            rules: R.TaskRules, boundary: dict) -> ArmResult:
    t0 = time.monotonic()
    out = cfg.out_dir / "arms" / f"{arm_slug(arm)}-{rep}"
    res = ArmResult(cfg.run_key, arm, rep, END_ERROR, 0, 0.0, False, False, False, 0)
    env = None
    loop = None
    try:
        ctx = arm_context(arm, cfg, lost, rules)
        env = TaskEnv.fork(inv, boundary["tag"], cfg.task, out)
        loop = Loop(list(ctx), model, env, f"arm:{arm_slug(arm)}:{rep}", stop=cfg.stop_event)
        loop.ctx_tokens = est_tokens(ctx, model.route.bytes_per_token)
        start_len = len(ctx)
        k = 0
        while k < cfg.continuation_calls and not loop.submitted:
            if loop.ctx_tokens + model.route.max_output > model.route.window:
                res.end = END_WINDOW
                break
            k += 1
            loop.step(f"turn-a{k}")
            if arm != "F" and loop.ctx_tokens >= cfg.threshold:
                res.would_recompact = True
        res.submitted_before_closing = loop.submitted
        loop.append([{"role": "user", "content": C.CLOSING_TURN}])
        res.closing_sent = True
        loop.submitted = False
        for j in range(cfg.closing_calls):
            if loop.ctx_tokens + model.route.max_output > model.route.window:
                break
            loop.step(f"turn-c{j + 1}")
            if loop.submitted:
                break
        fin = env.finish()
        res.end = END_DONE
        res.leftover_committed = fin["leftover_committed"]
        res.traps_home = fin["traps_home"]
        res.patch_bytes = len(fin["patch"])
        with _DUMP_LOCK:
            _dump(out / "continuation.json", loop.messages[start_len:])
    except RunCapReached as e:
        res.end, res.error = END_CAP, f"RunCapReached: {e}"
    except ModelError as e:
        res.error = f"ModelError: {e}"
    except Stopped as e:
        res.end, res.error = END_STOPPED, str(e)
    except Exception:  # noqa: BLE001
        res.error = traceback.format_exc()[-6000:]
    finally:
        if loop is not None:
            res.n_calls, res.cost_usd, res.asks = loop.n_calls, loop.cost_usd, loop.asks
            _dump(out / "calls.json", loop.calls)
        if env is not None:
            res.timings = env.timings
            env.close()
        res.seconds = round(time.monotonic() - t0, 3)
        _dump(out / "result.json", res.to_dict())
    return res
