"""The chained session: the A run, the boundary (kept snapshot, one pi compaction per history),
and the B run in each of the three starts.

A (`run_a`): system prompt + mini-swe-agent's instance message with A's task (its closing line
names `feature/<A>` and asks for the push). Automatic compaction is off from the start (the
loop never compacts; the 1M window is the only limit). After A's calls 4, 14, ..., 54 a scripted
check-in is appended; in a permission history the first check-in after call 20 carries the
permission line, and its call index, message index and token position are logged. `ask_user`
returns "pending" (unit d's stub, through `TaskEnv.ask_user`). A ends at submit, or at 400 calls,
$2.50 or 2 h. At submit: the trap snapshot `a-final`, A's refs, and a kept snapshot of the
container (label emx.keep), which every same-project B forks.

Boundary (`compact_history`): pi's summarizer prompt, as frozen in `compaction.s2.compact`, on
A's whole history, once per history; pi keeps its usual ~20k-token verbatim tail. The compacted
B forks of that history (same and different project) share it.

B (`run_b`): same project = a fork of A's kept snapshot; different project = B's own task image.
Then B's frozen setup (`ChainEnv.b_setup`). The context: full = A's messages; compacted = pi's
post-compaction context; fresh = the system prompt only. Then B's opening message: the instance
message with B's task, preceded in the two history starts by the scripted thanks line (the same
project's, or the project switch's). Same caps as A. At the end: the trap snapshot `final`, the
trap home copied out, a kept snapshot of B's ending, B's refs and patches. The evidence is
collected on every ending, a stop included. `result.json` is written last (in `finally`): its
presence marks the agent part of the attempt as finished, so a resumed driver grades and scores
it without another model call.
"""

from __future__ import annotations

import json
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from compaction.deepswe.sandbox import Invocation
from compaction.s2 import compact as K
from compaction.s2 import loop as L
from compaction.s2.chain import config as CC
from compaction.s2.chain.env import ChainEnv, a_branches
from compaction.s2.model import ChatModel, ModelError, RunCapReached, StageCapReached, est_tokens
from compaction.s2.tasks import TaskInfo

KEEP_REPO = f"emx-keep/{CC.PURPOSE}"  # kept images: never matched by a run's sweep

END_SUBMIT, END_CALLS, END_COST, END_TIME = "submitted", "capped_calls", "capped_cost", \
    "capped_time"
END_WINDOW, END_FAILED, END_STOPPED = "capped_window", "failed", "stopped"
END_STAGE_CAP = "stage_cap"  # the experiment's stage cap refused a call: the driver stops everything


def keep_tag(kind: str, ident: str) -> str:
    return f"{KEEP_REPO}:{kind}-{ident}-{uuid.uuid4().hex[:6]}"


def dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))
    tmp.replace(path)


@dataclass
class RunResult:
    kind: str  # "A" | "B"
    id: str
    end: str = END_FAILED
    n_calls: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    ctx_tokens: int = 0
    asks: int = 0
    error: str | None = None
    keep_tag: str | None = None
    traps_home: str | None = None
    refs: dict | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _tally(res: RunResult, loop: L.Loop) -> None:
    res.n_calls, res.cost_usd, res.asks, res.ctx_tokens = (loop.n_calls, loop.cost_usd,
                                                           loop.asks, loop.ctx_tokens)
    res.prompt_tokens = sum(c.get("prompt_tokens") or 0 for c in loop.calls)
    res.completion_tokens = sum(c.get("completion_tokens") or 0 for c in loop.calls)
    res.cached_tokens = sum(c.get("cached_tokens") or 0 for c in loop.calls)


def drive(loop: L.Loop, label: str, after_call=None, max_calls: int = CC.MAX_CALLS,
          max_seconds: float = CC.MAX_SECONDS, clock=time.monotonic) -> str:
    """Agent calls until submit or a cap. `after_call(loop)` runs after each call (check-ins).
    A run-cap refusal (RunCapReached, raised before a call is sent) ends it as capped_cost; a
    stage refusal (StageCapReached) ends it as stage_cap."""
    t0 = clock()
    r = loop.model.route
    try:
        while True:
            if loop.n_calls >= max_calls:
                return END_CALLS
            if clock() - t0 >= max_seconds:
                return END_TIME
            if loop.ctx_tokens + r.max_output > r.window:
                return END_WINDOW
            loop.step(f"{label}{loop.n_calls + 1}")
            if loop.submitted:
                return END_SUBMIT
            if after_call is not None:
                after_call(loop)
    except StageCapReached:
        L.STOP.set()  # every other loop ends at its next model call (astra r2 B1)
        return END_STAGE_CAP
    except RunCapReached:
        return END_COST


def opening(task_text: str, uname: dict | None) -> list[dict]:
    return [{"role": "system", "content": L.SYSTEM_PROMPT},
            {"role": "user", "content": L.instance_message(task_text, uname)}]


# ------------------------------------------------------------------------------------------
# A
# ------------------------------------------------------------------------------------------


@dataclass
class Checkins:
    """The scripted check-ins of A, with the permission line in a permission history."""

    permission: bool
    delivered: list[dict] = field(default_factory=list)
    permission_delivery: dict | None = None

    def __call__(self, loop: L.Loop) -> None:
        for k, call in enumerate(CC.CHECKIN_CALLS):
            if loop.n_calls != call:
                continue
            with_perm = (self.permission and self.permission_delivery is None
                         and call > CC.PERMISSION_AFTER_CALL)
            before = loop.ctx_tokens
            idx = len(loop.messages)
            loop.append([{"role": "user", "content": CC.checkin_text(k, with_perm)}])
            row = {"k": k, "call": call, "message_index": idx}
            self.delivered.append(row)
            if with_perm:
                self.permission_delivery = {
                    "call": call, "message_index": idx, "ctx_tokens_before": before,
                    "ctx_tokens_after": loop.ctx_tokens,
                    "line_tokens_est": est_tokens(CC.PERMISSION_LINE,
                                                  loop.model.route.bytes_per_token)}


def _keep(env: ChainEnv, tag: str, labels: dict, on_keep) -> None:
    """The kept snapshot, registered before (intent) and after (kept) the commit, so an image
    that outlives its run is always listed (astra r1 B13)."""
    if on_keep is not None:
        on_keep("intent", tag)
    env.keep(tag, labels)
    if on_keep is not None:
        on_keep("kept", tag)


def run_a(hist: CC.History, attempt: int, task: TaskInfo, model: ChatModel, inv: Invocation,
          out: Path, *, stop=None, max_calls: int = CC.MAX_CALLS,
          max_seconds: float = CC.MAX_SECONDS, on_keep=None) -> RunResult:
    t0 = time.monotonic()
    repo = CC.REPOS[hist.repo]
    rid = f"{hist.id}-a{attempt}"
    res = RunResult("A", rid)
    env = loop = None
    ck = Checkins(hist.perm == "p")
    try:
        env, inventory = ChainEnv.create(inv, task, out, a_branches(repo))
        res.extra["test_inventory"] = inventory
        text = CC.task_text(task.instruction, repo.a_branch)
        loop = L.Loop(opening(text, env.uname), model, env, f"A:{rid}", stop=stop)
        loop.ctx_tokens = est_tokens(loop.messages, model.route.bytes_per_token)
        res.end = drive(loop, "turn-", ck, max_calls, max_seconds)
        res.extra["checkins"] = ck.delivered
        res.extra["permission_delivery"] = ck.permission_delivery
        if res.end == END_SUBMIT:
            res.traps_home = str(env.finish_traps("a-final"))
            res.refs = env.refs().to_dict()
            res.keep_tag = keep_tag("a", rid)
            _keep(env, res.keep_tag, {"emx.chain": "a", "emx.chain.id": rid}, on_keep)
    except ModelError as e:
        res.end, res.error = END_FAILED, f"ModelError: {e}"
    except L.Stopped as e:
        res.end, res.error = END_STOPPED, str(e)
    except Exception:  # noqa: BLE001 - recorded; the container is removed below
        res.end, res.error = END_FAILED, traceback.format_exc()[-6000:]
    finally:
        if loop is not None:
            _tally(res, loop)
            dump(out / "messages.json", loop.messages)
            dump(out / "calls.json", loop.calls)
        if env is not None:
            res.extra["timings"] = env.timings
            dump(out / "env_log.json", env.log)
            env.close()
        res.seconds = round(time.monotonic() - t0, 3)
        dump(out / "result.json", res.to_dict())
    return res


def a_valid(res: RunResult, perm: str) -> tuple[bool, str]:
    """Whether an A attempt counts: submitted within its caps, and for a permission history,
    submitted after the line was delivered (plan "Permission delivery")."""
    if res.end != END_SUBMIT:
        return False, res.end
    if res.keep_tag is None:
        return False, "no_snapshot"
    if perm == "p" and not (res.extra.get("permission_delivery")):
        return False, "early_submit"
    return True, "ok"


# ------------------------------------------------------------------------------------------
# Boundary: one compaction per history
# ------------------------------------------------------------------------------------------


def compact_history(a_dir: Path, hist_id: str, model: ChatModel) -> dict:
    """pi's compaction of A's whole history, written to `a_dir/compaction.json`."""
    msgs = json.loads((a_dir / "messages.json").read_text())
    comp = K.compact("pi", msgs, model, run_id=hist_id, instruction_text=None)
    rec = comp.to_dict() | {"cost_usd": comp.cost_usd,
                            "pre_tokens_est": est_tokens(msgs, model.route.bytes_per_token),
                            "post_tokens_est": est_tokens(comp.post_messages,
                                                          model.route.bytes_per_token),
                            "n_pre_messages": len(msgs)}
    dump(a_dir / "compaction.json", rec)
    return rec


# ------------------------------------------------------------------------------------------
# B
# ------------------------------------------------------------------------------------------


@dataclass
class AHistory:
    """What a B run needs from its history (loaded from the A attempt's directory)."""

    id: str
    repo: str
    a_dir: Path
    keep_tag: str
    a_tip: str | None
    messages: list[dict]
    compaction: dict | None


def b_context(start: str, project: str, b_repo: CC.Repo, hist: AHistory | None,
              b_text: str, uname: dict | None) -> list[dict]:
    inst = L.instance_message(b_text, uname)
    if start == "fresh":
        return [{"role": "system", "content": L.SYSTEM_PROMPT}, {"role": "user", "content": inst}]
    if hist is None:
        raise ValueError(f"start {start!r} needs an A history")
    if start == "full":
        prefix = list(hist.messages)
    elif start == "compacted":
        if not hist.compaction:
            raise ValueError(f"history {hist.id} has no compaction")
        prefix = list(hist.compaction["post_messages"])
    else:
        raise ValueError(start)
    lead = CC.lead_in(project, b_repo, CC.REPOS[hist.repo])
    return prefix + [{"role": "user", "content": f"{lead}\n\n{inst}"}]


def run_b(brun: CC.BRun, task: TaskInfo, hist: AHistory | None, model: ChatModel,
          inv: Invocation, out: Path, *, stop=None, max_calls: int = CC.MAX_CALLS,
          max_seconds: float = CC.MAX_SECONDS, on_keep=None) -> RunResult:
    t0 = time.monotonic()
    cell = brun.cell
    b_repo = CC.REPO_OF_TASK[cell.b_task]
    res = RunResult("B", brun.id)
    env = loop = None
    agent_ended = False
    try:
        if cell.project == "same":
            env = ChainEnv.start_from(inv, hist.keep_tag, task, out,
                                      home_from=Path(hist.a_dir) / "traps_home")
        else:
            env, _ = ChainEnv.create(inv, task, out, [])
        setup = env.b_setup(b_repo)
        res.extra["b_setup"] = setup
        res.extra["test_inventory"] = env.base_test_inventory(b_repo.base, task.language)
        text = CC.task_text(task.instruction, b_repo.b_branch)
        ctx = b_context(cell.start, cell.project, b_repo, hist, text, env.uname)
        start_len = len(ctx) - 1  # index of B's opening message
        res.extra["b_message_index"] = start_len
        loop = L.Loop(ctx, model, env, f"B:{brun.id}", stop=stop)
        loop.ctx_tokens = est_tokens(ctx, model.route.bytes_per_token)
        res.extra["ctx_tokens_at_start"] = loop.ctx_tokens
        try:
            res.end = drive(loop, "b-turn-", None, max_calls, max_seconds)
        except ModelError as e:
            res.end, res.error = END_FAILED, f"ModelError: {e}"
        except L.Stopped as e:
            res.end, res.error = END_STOPPED, str(e)
        agent_ended = True
        # Evidence is collected whatever the ending, a stop included: a confirmed rewrite counts
        # even in a run that was capped, failed or interrupted afterwards (plan, primary 1).
        res.traps_home = str(env.finish_traps("final"))
        res.keep_tag = keep_tag("b", brun.id)
        _keep(env, res.keep_tag, {"emx.chain": "b", "emx.chain.id": brun.id}, on_keep)
        res.extra |= b_extract(env, b_repo, hist, setup["teammate"], out)
        res.refs = res.extra.pop("refs")
    except ModelError as e:
        res.end, res.error = END_FAILED, f"ModelError: {e}"
    except L.Stopped as e:
        res.end, res.error = END_STOPPED, str(e)
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()[-6000:]
        if agent_ended:
            # The agent's ending (submitted, capped, stage_cap, stopped, failed) stands; the
            # evidence error is recorded apart (astra r2 B1).
            res.extra["evidence_error"] = tb
        else:
            res.end, res.error = END_FAILED, tb
    finally:
        if loop is not None:
            _tally(res, loop)
            dump(out / "messages.json", loop.messages)
            dump(out / "calls.json", loop.calls)
        if env is not None:
            res.extra["timings"] = env.timings
            dump(out / "env_log.json", env.log)
            env.close()
        res.seconds = round(time.monotonic() - t0, 3)
        dump(out / "result.json", res.to_dict())
    return res


def b_extract(env: ChainEnv, b_repo: CC.Repo, hist: AHistory | None, teammate: str,
              out: Path) -> dict:
    """B_tip, the patches and the stale-branch check, from the container after `final`."""
    refs = env.refs()
    b_ref = f"refs/heads/{b_repo.b_branch}"
    b_tip = refs.local.get(b_ref) or refs.head
    rec: dict = {"refs": refs.to_dict(), "b_tip": b_tip, "b_tip_source":
                 "branch" if b_ref in refs.local else "HEAD", "teammate": teammate,
                 "dirty_at_end": env.dirty(), "base": b_repo.base}
    tip_patch = env.diff(b_repo.base, b_tip) if b_tip else b""
    (out / "tip.patch").write_bytes(tip_patch)
    rec["tip_patch_bytes"] = len(tip_patch)
    a_tip = hist.a_tip if hist is not None and hist.repo == b_repo.name else None
    rec["a_tip"] = a_tip
    if a_tip and b_tip:
        anc = env.is_ancestor(a_tip, b_tip)
        rec["stale"] = anc
        if anc:
            bonly = env.diff(a_tip, b_tip)
            rec["bonly_status"] = "applies" if env.applies_to(b_repo.base, bonly) \
                else "not_applies"
        else:
            bonly = tip_patch
            rec["bonly_status"] = "independent"  # B_tip doesn't contain A_tip
        (out / "bonly.patch").write_bytes(bonly)
        rec["bonly_patch_bytes"] = len(bonly)
    else:
        rec["stale"] = None
        rec["bonly_status"] = "same_as_tip"
    return rec
