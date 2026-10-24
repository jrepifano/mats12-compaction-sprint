"""The SWE-Interact pilot runner (unit C of logs/s2/briefs/swe-interact-build.md; plan
docs/sprint/plans/swe-interact-pilot.md).

- Tasks: SWE-Interact's step instructions and persona over our DeepSWE task (image, verifier,
  reference solution); `load_task`.
- Spend: `PilotSpend` books every call to stage SPRINT-S3-Q2 with the pilot tag in its note, and
  refuses a reservation that would take the tag past $120 (the fallback tag past $12).
  `claude_backend` is the Max plan (`claude -p`) with the OpenRouter Haiku fallback when Max is
  out of its usage window or silent for 10 minutes.
- Base run (`run_base`): `01_plan` as its own conversation, then a fresh `02_implement` with the
  same simulator (Harbor's default, feasibility §2), whose boundaries come from unit A's
  `session.run_session`. At each boundary this module's hook writes the simulator's state and
  reply index; the run stops at the first eligible boundary (unit B's `eligibility`).
- Arms (`run_arm`): one restored container and simulator per arm, starting from a named summary
  source (live pi or a replay), with the repair or matched block. The primary readout stops at
  the first ask_user (not sent), a submission or 60 calls; `run_secondary` resumes a primary
  with its own live simulator. Arms recompact with pi on DeepSeek past 150k (plan v0.4).
- `DockerWatch` logs `docker stats` per container and the daemon's OOM events; `run_bases`
  runs the base runs concurrently and reruns infrastructure failures once.

Units A (`compaction.s2.session`, `.replays`) and B (`compaction.s2.interact_reqs`) are imported
where used, so their fakes can be injected in tests.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import itertools
import json
import os
import re
import subprocess
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from compaction.s2 import compact as K
from compaction.s2 import config as C
from compaction.s2 import interact as I
from compaction.s2 import mini_prompts as MP
from compaction.s2.loop import SYSTEM_PROMPT, Loop, instance_message
from compaction.s2.model import ChatModel, StageCapReached, est_tokens
from compaction.s2.tasks import TaskInfo, task_info

INTERACT_TOOLS = [MP.BASH_TOOL, I.ASK_USER_TOOL]
SI_TASKS = C.REPO_ROOT / "data" / "raw" / "sprint" / "swe_interact" / "repo" / "data" / "multiturn"
STEPS = ("01_plan", "02_implement")
PURPOSE = "s2-interact"  # emx.sandbox label of every pilot container
LEAD = "Additional context from earlier in this session:"
# The fork check on every real boundary (plan "Fork protocol" 3): the simulator's first request
# for this question (auto-snapshot included, no model call) and the workspace fingerprint are
# hashed at the boundary and again in every arm right after its restore.
SIM_PROBE = "FORK-CHECK: Done, committed. Ready for review?"
INFRA_ENDS = ("infra_oom", "infra_error")


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def _read(path: Path):
    return json.loads(Path(path).read_text())


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def interact_root() -> Path:
    return C.raw_dir() / "interact"


# ------------------------------------------------------------------------------------------
# Tasks
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InteractTask:
    task_id: str
    info: TaskInfo  # our DeepSWE task: image, verifier, reference solution
    persona: str  # SWE-Interact's persona.md (holds the hidden spec)
    steps: dict  # step name -> instruction.md text, verbatim
    spec_path: str  # repo-relative SWE-Interact instruction.md (the persona's task block)
    spec_sha256: str

    def to_dict(self) -> dict:
        sol = Path(self.info.task_dir) / "solution" / "solution.patch"
        return {"task_id": self.task_id, "image": self.info.image, "base_image": self.info.image,
                "task_dir": _rel(self.info.task_dir), "tests_dir": _rel(self.info.tests_dir),
                "solution_patch": _rel(sol), "spec_path": self.spec_path,
                "spec_sha256": self.spec_sha256, "language": self.info.language,
                "persona_sha256": I.sha256(self.persona)}


def _rel(p) -> str:
    try:
        return str(Path(p).resolve().relative_to(C.REPO_ROOT))
    except ValueError:
        return str(p)


def load_task(task_id: str, si_root: Path | None = None,
              deepswe_dir: Path | None = None) -> InteractTask:
    d = Path(si_root or SI_TASKS) / f"deepswe_{task_id}"
    spec = d / "instruction.md"
    steps = {s: (d / "steps" / s / "instruction.md").read_text() for s in STEPS}
    return InteractTask(task_id=task_id, info=task_info(task_id, deepswe_dir),
                        persona=(d / "environment" / "user-server" / "persona.md").read_text(),
                        steps=steps, spec_path=_rel(spec),
                        spec_sha256=sha256_bytes(spec.read_bytes()))


# ------------------------------------------------------------------------------------------
# Spend
# ------------------------------------------------------------------------------------------

_TAG_LOCK = threading.RLock()


@contextlib.contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _registry_path(ledger=None) -> Path:
    from compaction import budget
    return Path(budget._paths(ledger)[0]).parent / "s3q2_interact_reservations.jsonl"


def _registry(ledger=None) -> dict[str, dict]:
    p = _registry_path(ledger)
    out: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            with contextlib.suppress(ValueError):
                r = json.loads(line)
                out[r["rid"]] = r
    return out


def tag_totals(tag: str, *, ledger=None, caps_file=None) -> tuple[float, float]:
    """(charged, held) for `tag` (astra build r1 #2). The pilot's own reservations are listed
    in a durable registry with their tags at reserve time; each counts at its full amount
    while open in budget's state, and at its closing event's charge once closed (settle,
    expire or reconcile; cancel charges 0), so a reservation another process expires keeps
    counting although its ledger row loses our note. Budget's state and events are read
    together under budget's lock. Rows booked without a reservation (Max-plan rows: $0, or the
    notional cost of calls that reached a paid route) are added from the ledger by tag."""
    from compaction import budget
    led, _, state, _ = budget._paths(ledger)
    reg = _registry(ledger)
    with budget._locked(ledger, caps_file):
        open_ = budget._load_state(state)
        events = budget.read_events(ledger)
    closed = {e["reservation_id"]: e for e in events if e.get("op") in budget.CLOSING_OPS}
    charged = held = 0.0
    for rid, r in reg.items():
        if tag not in r["tags"]:
            continue
        if rid in open_:
            held += float(open_[rid]["max_usd"])
        elif rid in closed:
            charged += float(closed[rid].get("charged_usd") or 0.0)
        else:
            held += float(r["max_usd"])  # not visible yet: count it in full
    if Path(led).exists():
        for line in Path(led).read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            note = str(row.get("note") or "")
            if (row.get("exp") == C.INTERACT_STAGE and tag in note
                    and budget.MARK not in note):
                charged += float(row.get("usd") or 0.0)
    return charged, held


@dataclass
class PilotSpend:
    """`model.Spend` for the pilot: stage SPRINT-S3-Q2, `tags` in every note, each tag's cap
    in `caps` checked (charged + held + request) before the stage reservation is made. The
    check, the reservation and its registry row, and every settle and cancel run under one
    process lock and one file lock (all pilot processes of this checkout), so no pilot
    reservation can change between the check and the reserve. budget.py still enforces the
    stage and sprint caps."""

    tags: tuple[str, ...] = (C.INTERACT_TAG,)
    caps: dict = field(default_factory=lambda: {C.INTERACT_TAG: C.INTERACT_CAP_USD})
    stage: str = C.INTERACT_STAGE
    ledger: str | Path | None = None
    caps_file: str | Path | None = None

    def note(self, note: str) -> str:
        return f"{note} [{' '.join(self.tags)}]"

    def _lock(self):
        from compaction import budget
        return _file_lock(Path(budget._paths(self.ledger)[0]).parent / "s3q2_interact.lock")

    def reserve(self, usd: float, note: str) -> str:
        from compaction import budget
        with _TAG_LOCK, self._lock():
            for tag in self.tags:
                cap = self.caps.get(tag)
                if cap is None:
                    continue
                charged, held = tag_totals(tag, ledger=self.ledger, caps_file=self.caps_file)
                if charged + held + usd > cap + 1e-9:
                    raise StageCapReached(f"{tag}: charged ${charged:.4f} + held ${held:.4f} + "
                                          f"${usd:.4f} exceeds ${cap:.2f}")
            try:
                rid = budget.reserve(self.stage, usd, note=self.note(note), ledger=self.ledger,
                                     caps_file=self.caps_file)
            except (budget.TopUpRequired, budget.WrongCheckout):
                raise
            except budget.BudgetRefused as e:
                raise StageCapReached(f"stage refusal: {e}") from e
            with open(_registry_path(self.ledger), "a") as f:
                f.write(json.dumps({"rid": rid, "tags": list(self.tags), "max_usd": usd,
                                    "utc": utc()}) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return rid

    def settle(self, rid: str, usd: float, *, resource: str, units: float, note: str) -> None:
        from compaction import budget
        with _TAG_LOCK, self._lock():
            budget.settle(rid, usd, stage=self.stage, kind="judge", resource=resource,
                          units=units, note=self.note(note), ledger=self.ledger,
                          caps_file=self.caps_file)

    def cancel(self, rid: str, reason: str) -> None:
        from compaction import budget
        with _TAG_LOCK, self._lock():
            budget.cancel(rid, reason, ledger=self.ledger, caps_file=self.caps_file)

    def recorder(self) -> Callable[..., Any]:
        """For `MaxBackend(recorder=...)`: its own usage row (a $0 Max row, or the notional
        cost of calls that went to a paid route), booked to this stage with the tags instead of
        claude_headless's SPRINT-S1."""
        from emx import spend

        def rec(kind, resource, *, units, usd, note, exp=None):
            with _TAG_LOCK, self._lock():
                return spend.record(kind, resource, units, usd, note=self.note(note),
                                    exp=self.stage, ledger=self.ledger)
        return rec


def fallback_spend(base: PilotSpend) -> PilotSpend:
    """The fallback's books: both tags, so its charges count toward $120 and toward $12."""
    return replace(base, tags=(*base.tags, C.INTERACT_FALLBACK_TAG),
                   caps={**base.caps, C.INTERACT_FALLBACK_TAG: C.INTERACT_FALLBACK_CAP_USD})


# ------------------------------------------------------------------------------------------
# Claude (Max plan) with the OpenRouter fallback
# ------------------------------------------------------------------------------------------


@dataclass
class ORHaikuBackend:
    """`s1_types.Backend` over Haiku 5.5 on OpenRouter (the pinned route), for the Max
    fallback. Text and schema requests only: tool-using requests (the second look) come back
    with error "max_only" and their stage leaves them unresolved. The pilot's boundaries are
    our own runs on public tasks, so `Req.private` doesn't block this route."""

    spend: Any
    concurrency: int = 8
    model_factory: Callable[[Any], ChatModel] | None = None
    name: str = "haiku-openrouter-fallback"
    family: str = "anthropic"

    def __post_init__(self) -> None:
        from compaction import s1_replay as RP
        self.model = RP.MODEL  # the replays check the backend's pinned model
        r = C.ROUTES["haiku"]
        self.context_tokens, self.bytes_per_token = r.window, r.bytes_per_token
        self._route = replace(r, reasoning={}, summary_reasoning=None, name="haiku-fallback")

    def _chat(self) -> ChatModel:
        if self.model_factory is not None:
            return self.model_factory(self.spend)
        from compaction.s2.model import OpenAICompatModel
        return OpenAICompatModel(self._route, self.spend)

    def _one(self, req, purpose: str):
        from compaction.s1_api import schema_instruction
        from compaction.s1_types import Resp, validate
        if req.tools:
            return Resp(req.key, None, None, "max_only")
        user = req.user + (schema_instruction(req.schema) if req.schema is not None else "")
        t0 = time.monotonic()
        try:
            rep = self._chat().complete([{"role": "system", "content": req.system},
                                         {"role": "user", "content": user}], None,
                                        req.max_output_tokens, purpose=f"fallback:{purpose}")
        except StageCapReached:
            raise
        except Exception as e:  # noqa: BLE001 - recorded on the response
            return Resp(req.key, None, None, f"fallback_error: {type(e).__name__}: {e}"[:300])
        text = K._text(rep.message.get("content"))
        data, err = None, None
        if req.schema is not None:
            try:
                data = json.loads(_strip_fence(text))
                err = "schema" if validate(data, req.schema) else None
            except ValueError:
                err = "schema"
        u = rep.usage
        return Resp(req.key, data, text, err, input_tokens=u.prompt_tokens or 0,
                    output_tokens=u.completion_tokens or 0, cache_read_tokens=u.cached_tokens,
                    usd=rep.cost_total_usd, model=self.model,  # the pinned id the replays check
                    duration_s=time.monotonic() - t0,
                    meta={"route": self._route.name, "route_model": self._route.model,
                          "provider": self._route.provider_pin})

    def run(self, reqs: list, *, purpose: str) -> dict:
        with ThreadPoolExecutor(max_workers=max(1, min(self.concurrency, len(reqs) or 1))) as ex:
            futs = {r.key: ex.submit(self._one, r, purpose) for r in reqs}
            return {k: f.result() for k, f in futs.items()}


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


@dataclass
class FallbackBackend:
    """Max first; on a usage-window error ("limit") or no answer within `timeout_s`, this stage
    switches to the fallback for the requests still unanswered and stays switched (plan v0.4.1,
    astra r2). Max calls are booked by MaxBackend itself, through `PilotSpend.recorder`."""

    primary: Any  # a backend instance, or None with primary_factory
    fallback: Any
    spend: PilotSpend | None = None
    primary_factory: Callable[[], Any] | None = None  # a fresh MaxBackend per run() call
    timeout_s: float = C.INTERACT_MAX_TIMEOUT_S
    switched: bool = False
    events: list = field(default_factory=list)
    family: str = "anthropic"
    _lock: threading.Lock = field(default_factory=threading.Lock)  # 5 replay workers share it

    def __post_init__(self) -> None:
        proto = self.primary if self.primary is not None else self.primary_factory()
        self.name = f"{getattr(proto, 'name', 'max')}+fallback"
        self.model = getattr(proto, "model", None)
        self.context_tokens = getattr(proto, "context_tokens", None)
        self.bytes_per_token = getattr(proto, "bytes_per_token", None)

    def _primary(self, reqs, purpose):
        """One primary call under the timeout. With `primary_factory`, each call gets its own
        instance: MaxBackend refuses concurrent run() calls on one instance, and the replays
        call this backend from 5 workers at once (astra build r1 #1)."""
        box: dict = {}
        prim = self.primary if self.primary_factory is None else self.primary_factory()

        def go():
            try:
                box["out"] = prim.run(reqs, purpose=purpose)
            except BaseException as e:  # noqa: BLE001 - re-raised below
                box["exc"] = e

        t = threading.Thread(target=go, daemon=True, name=f"max:{purpose}")
        t.start()
        t.join(self.timeout_s)
        if t.is_alive():
            self._stop_primary(prim)
            return None, "timeout"
        if "exc" in box:
            raise box["exc"]
        return box["out"], None

    @staticmethod
    def _stop_primary(prim) -> None:
        with contextlib.suppress(Exception):
            prim._stop.set()
        with contextlib.suppress(Exception):
            from compaction import claude_headless as CH
            CH.kill_sessions(getattr(prim, "_owner", None))

    def run(self, reqs: list, *, purpose: str) -> dict:
        out: dict = {}
        rest = list(reqs)
        why = "switched"
        if not self.switched:
            got, why = self._primary(reqs, purpose)  # MaxBackend books its own usage row
            if got is not None:
                out = dict(got)
                rest = [r for r in reqs if out[r.key].error == "limit"]
                why = "limit" if rest else None
            if why is None:
                return out
            with self._lock:
                self.switched = True
                self.events.append({"utc": utc(), "purpose": purpose, "why": why,
                                    "n": len(rest)})
        if rest:
            if self.fallback is None:  # Max only (e.g. replays at ineligible boundaries)
                from compaction.s1_types import Resp
                out.update({r.key: Resp(r.key, None, None, f"max_unavailable:{why}")
                            for r in rest})
            else:
                out.update(self.fallback.run(rest, purpose=purpose))
        return out


def max_backend(spend: PilotSpend, concurrency: int = 2):
    from compaction.claude_headless import MaxBackend
    return MaxBackend(recorder=spend.recorder(), concurrency=concurrency)


def claude_backend(spend: PilotSpend, *, primary=None, fallback=None,
                   use_fallback: bool = True) -> FallbackBackend:
    """A fresh per-stage backend: Max (`claude -p`, Haiku 5.5; a new MaxBackend per call, so
    concurrent callers are fine) with the OpenRouter fallback, or Max only."""
    if fallback is None and use_fallback:
        fallback = ORHaikuBackend(fallback_spend(spend))
    if primary is not None:
        return FallbackBackend(primary, fallback, spend)
    return FallbackBackend(None, fallback, spend, primary_factory=lambda: max_backend(spend))


# ------------------------------------------------------------------------------------------
# Base run
# ------------------------------------------------------------------------------------------


def opening_messages(instruction: str, uname: dict | None) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": instance_message(instruction.rstrip("\n"), uname)}]


def run_key(task_id: str, attempt: int) -> str:
    return f"{task_id}-deepseek-a{attempt}"


def spend_path(root: Path, name: str) -> Path:
    """A run-level spend file, kept outside the execution dirs (which must be fresh)."""
    return Path(root) / "_spend" / f"{name}.jsonl"


def next_attempt(root: Path, task_id: str) -> int:
    """1 + the highest attempt of this task under root (attempt dirs are never reused)."""
    ns = [int(m.group(1)) for p in Path(root).glob(f"{task_id}-deepseek-a*")
          if (m := re.fullmatch(rf"{re.escape(task_id)}-deepseek-a(\d+)", p.name))]
    return max(ns, default=0) + 1


class _EndRun(Exception):
    def __init__(self, end: str):
        super().__init__(end)
        self.end = end


@dataclass
class BaseConfig:
    task: InteractTask
    root: Path
    attempt: int = 1
    threshold: int = C.INTERACT_THRESHOLD
    max_calls: int = C.INTERACT_BASE_MAX_CALLS
    plan_max_calls: int = C.INTERACT_PLAN_MAX_CALLS
    cap_usd: float = C.INTERACT_BASE_CAP_USD
    max_seconds: float = C.INTERACT_BASE_MAX_S
    image: str | None = None

    @property
    def run_key(self) -> str:
        return run_key(self.task.task_id, self.attempt)

    @property
    def run_dir(self) -> Path:
        return Path(self.root) / self.run_key


def run_json(cfg: BaseConfig, agent_route: str = "deepseek") -> dict:
    return {"run_key": cfg.run_key, "created_utc": utc(), "task": cfg.task.to_dict(),
            "plan": {"model": agent_route, "policy": "pi", "threshold": cfg.threshold,
                     "task": cfg.task.task_id, "stage": C.INTERACT_STAGE,
                     "tag": C.INTERACT_TAG},
            "sim": {"model": I.SimConfig().model, "route": C.SIM_ROUTE.name,
                    "reasoning": C.SIM_ROUTE.reasoning, "persona_sha256":
                        I.sha256(cfg.task.persona), "upstream": I.UPSTREAM}}


def reply_index(ask_rows: list[dict], pre: list[dict]) -> list[dict]:
    """sim_replies.json: every simulator reply with its index in `pre` (null when not there
    verbatim). The ask_user tool results in `pre` are a contiguous suffix of the replies of
    the current step (a summary replaces a prefix; the kept tail and everything after it are
    verbatim), so they are matched from the end and their text checked."""
    asks = {}
    for m in pre:
        for tc in m.get("tool_calls") or []:
            if (tc.get("function") or {}).get("name") == "ask_user":
                asks[tc.get("id")] = True
    tool_idx = [i for i, m in enumerate(pre)
                if m.get("role") == "tool" and asks.get(m.get("tool_call_id"))]
    rows = [dict(r, pre_index=None) for r in ask_rows]
    step_rows = [r for r in rows if r["step"] == "02_implement"]
    for r, i in zip(reversed(step_rows), reversed(tool_idx), strict=False):
        if K._text(pre[i].get("content")) == r["reply"]:
            r["pre_index"] = i
        else:
            r["pre_index_mismatch"] = i
    return rows


class EnvAsks:
    """Wraps InteractEnv.ask_user to note each reply's call index and step."""

    def __init__(self, env: I.InteractEnv):
        self.env, self.rows, self.step = env, [], "01_plan"
        self._orig = env.ask_user
        env.ask_user = self.ask_user  # type: ignore[method-assign]

    def ask_user(self, question: str, action: str | None = None) -> str:
        reply = self._orig(question, action)
        if reply != I.PENDING_ASK:
            st = self.env.sim.state
            self.rows.append({"reply_id": f"r{st.ask_user_call_count}",
                              "call_index": st.ask_user_call_count, "step": self.step,
                              "question": question, "reply": reply})
        return reply


def default_eligibility(backend_factory: Callable[[], Any]) -> Callable[[Path, dict], bool]:
    def check(bdir: Path, fields: dict) -> bool:
        from compaction.s2 import interact_reqs as RQ
        RQ.split(bdir, backend=backend_factory(), fields=fields)
        return bool(RQ.eligibility(bdir, fields=fields)["eligible"])
    return check


def _session_mod():
    from compaction.s2 import session as SS
    return SS


def run_base(cfg: BaseConfig, *, agent_model: ChatModel, sim_model: ChatModel, inv,
             eligible: Callable[[Path, dict], bool], keep=None, session=None,
             watch: DockerWatch | None = None) -> dict:
    """One task: 01_plan, then 02_implement under run_session until the first eligible
    boundary, a submission, or a cap. Writes the run dir (build split §1); returns result."""
    SS = session or _session_mod()
    t0 = time.monotonic()
    rd = _fresh_dir(cfg.run_dir)  # a new attempt never reuses or mixes into an old one
    rj = run_json(cfg)
    _dump(rd / "run.json", rj)
    keep = keep if keep is not None else SS.KeepStore(rd, cfg.run_key)
    res: dict = {"run_key": cfg.run_key, "end": "error", "n_calls": 0, "cost_usd": 0.0,
                 "sim_cost_usd": 0.0, "boundaries": [], "error": None, "timings": [],
                 "plan_md": None, "steps": {}}
    env = None
    loops: dict[str, Loop] = {}
    asks = None

    def mark(ev: str, **kw) -> None:
        res["timings"].append({"event": ev, "utc": utc(),
                               "s": round(time.monotonic() - t0, 1), **kw})

    try:
        env = I.InteractEnv.start(inv, cfg.image or cfg.task.info.image,
                                  lambda runner: I.UserSim(cfg.task.persona, sim_model, runner,
                                                           purpose=f"sim:{cfg.run_key}"),
                                  cpus=C.CPUS, memory_mb=C.MEMORY_MB,
                                  platform=C.PLATFORM or None)
        res["container"] = env.sb.name
        asks = EnvAsks(env)
        mark("started")
        # --- 01_plan: its own conversation
        lp = Loop(opening_messages(cfg.task.steps["01_plan"], env.uname), agent_model, env,
                  "plan", tools=INTERACT_TOOLS)
        loops["01_plan"] = lp
        lp.ctx_tokens = est_tokens(lp.messages, agent_model.route.bytes_per_token)
        end1 = None
        while end1 is None:
            if lp.n_calls >= cfg.plan_max_calls:
                end1 = "max_calls"
            elif time.monotonic() - t0 > cfg.max_seconds:
                end1 = "time_cap"
            elif lp.ctx_tokens >= cfg.threshold:
                end1 = "threshold"  # not expected in a planning talk; recorded, then 02 starts
            else:
                lp.step(None)
                end1 = "submitted" if lp.submitted else None
                if end1 is None and sum(r.get("cost_usd") or 0.0
                                        for r in env.sim.rounds) > C.INTERACT_SIM_GATE_USD:
                    end1 = "sim_gate"
        res["steps"]["01_plan"] = {"end": end1, "n_calls": lp.n_calls}
        if sum(r.get("cost_usd") or 0.0 for r in env.sim.rounds) > C.INTERACT_SIM_GATE_USD:
            end1 = "sim_gate"  # also when the call that crossed it submitted the plan (r3)
            res["steps"]["01_plan"]["end"] = end1
        if end1 in ("time_cap", "sim_gate"):
            raise _EndRun(end1)
        res["plan_md"] = env.read_text("/tmp/plan.md")
        mark("plan_done", end=end1)
        # --- 02_implement: a fresh conversation, the same simulator
        asks.step = "02_implement"
        li = Loop(opening_messages(cfg.task.steps["02_implement"], env.uname), agent_model,
                  env, "implement", tools=INTERACT_TOOLS)
        loops["02_implement"] = li
        li.ctx_tokens = est_tokens(li.messages, agent_model.route.bytes_per_token)

        def on_boundary(ctx) -> None:
            b = Path(ctx.bdir) / "boundary"
            pre = _read(b / "pre_messages.json")
            _dump(b / "sim_state.json", env.sim.export())
            _dump(b / "sim_replies.json", reply_index(asks.rows, pre))
            (b / "step_instruction.txt").write_text(cfg.task.steps["02_implement"])
            ctx.fields.update(stage="02_implement", calls_in_step=ctx.loop.n_calls,
                              fingerprint_sha256=I.sha256(env.fingerprint()),
                              fingerprint_version=I.FINGERPRINT_VERSION,
                              sim_probe_sha256=sim_probe(env.sim),
                              sim_calls=env.sim.state.ask_user_call_count)
            mark("boundary_saved", k=ctx.k)

        def stop_at(ctx) -> bool:
            ok = eligible(Path(ctx.bdir), ctx.fields)
            ctx.fields["eligible"] = ok
            mark("eligibility_done", k=ctx.k, eligible=ok)
            return ok

        def after_call(loop) -> str | None:
            if loop.submitted:
                return "submitted"
            if time.monotonic() - t0 > cfg.max_seconds:
                return "time_cap"
            sim_usd = sum(r.get("cost_usd") or 0.0 for r in env.sim.rounds)
            if sim_usd > C.INTERACT_SIM_GATE_USD:
                return "sim_gate"  # re-project before running this task on
            return None

        hooks = SS.SessionHooks(on_boundary=on_boundary, stop_at_boundary=stop_at,
                                after_call=after_call)
        sr = SS.run_session(li, env, keep, run_dir=rd, run_json=rj, threshold=cfg.threshold,
                            policy="pi", instruction_text=cfg.task.steps["02_implement"],
                            max_calls=max(0, cfg.max_calls - lp.n_calls),
                            run_cap_usd=cfg.cap_usd, hooks=hooks)
        res.update(end=sr.end, boundaries=list(sr.boundaries), error=sr.error)
        res["steps"]["02_implement"] = {"end": sr.end, "n_calls": li.n_calls}
    except _EndRun as e:
        res["end"] = e.end
    except Exception as e:  # noqa: BLE001 - recorded; the container is removed below
        from compaction.deepswe.sandbox import SandboxError
        res["end"] = "infra_error" if isinstance(e, SandboxError) else "error"
        res["error"] = traceback.format_exc()[-6000:]
    finally:
        for name, lo in loops.items():
            _dump(rd / "steps" / name / "messages.json", lo.messages)
            _dump(rd / "steps" / name / "calls.json", lo.calls)
        res["n_calls"] = sum(lo.n_calls for lo in loops.values())
        res["cost_usd"] = round(sum(lo.cost_usd for lo in loops.values()), 6)
        if env is not None:
            _dump(rd / "sim" / "ask_log.json", env.sim.ask_log)
            _dump(rd / "sim" / "rounds.json", env.sim.rounds)
            res["sim_cost_usd"] = round(sum(r.get("cost_usd") or 0.0 for r in env.sim.rounds), 6)
            if asks is not None:
                _dump(rd / "sim" / "replies.json", asks.rows)
            if watch is not None and watch.oom_killed(env.sb.name):
                res["end"] = "infra_oom"
            env.close()
        res["seconds"] = round(time.monotonic() - t0, 1)
        mark("ended", end=res["end"])
        _dump(rd / "result.json", res)
    return res


# ------------------------------------------------------------------------------------------
# Arms
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmSpec:
    source: str  # C.INTERACT_SOURCES
    kind: str  # "repair" | "matched"
    rep: int
    block: str  # the requirement (repair) or the matched item, as frozen
    readout: str = "primary"

    def __post_init__(self) -> None:
        if self.source not in C.INTERACT_SOURCES:
            raise ValueError(f"unknown source {self.source!r}")
        if self.kind not in ("repair", "matched"):
            raise ValueError(f"unknown kind {self.kind!r}")

    @property
    def slug(self) -> str:
        return f"{self.source}-{self.kind}-{self.rep}"


def _load_source(bdir: Path, source: str) -> dict:
    from compaction.s2 import replays as RE
    return RE.load_source(bdir, source)


# OpenCode's summary is an assistant message, so its arms get the block as a separate user
# message right after the summary (both arms alike; a logged format deviation, main session 24
# Oct). Every other source carries its summary in a user message, which gets the block appended.
SEPARATE_BLOCK_SOURCES = ("opencode",)


def block_index(source: str, summary_index: int) -> int:
    """The index of the message that differs between an arm pair."""
    return summary_index + 1 if source in SEPARATE_BLOCK_SOURCES else summary_index


def arm_context(bdir: Path, spec: ArmSpec, *, load_source=None) -> tuple[list[dict], dict]:
    """The arm's start input: the source's post-compaction context plus the block under LEAD,
    appended to the summary message (K.with_block), or for OpenCode inserted as a user message
    right after it. Returns (messages, source)."""
    src = (load_source or _load_source)(Path(bdir), spec.source)
    block = f"{LEAD}\n{spec.block}"
    si = src["summary_index"]
    if spec.source in SEPARATE_BLOCK_SOURCES:
        msgs = [dict(m) for m in src["post"]]
        msgs.insert(si + 1, {"role": "user", "content": block})
    else:
        msgs = K.with_block(src["post"], si, block)
    return msgs, src


def inputs_differ_only_in_block(a: list[dict], b: list[dict], index: int) -> bool:
    """The input diff check: two arms of one source differ only at `index` (block_index)."""
    if len(a) != len(b):
        return False
    return all(I.canon(x) == I.canon(y) for i, (x, y) in enumerate(zip(a, b, strict=True))
               if i != index)


def _restore_env(inv, image: str, bdir: Path, persona: str, sim_model: ChatModel,
                 purpose: str) -> I.InteractEnv:
    exported = _read(Path(bdir) / "boundary" / "sim_state.json")
    return I.InteractEnv.start(
        inv, image, lambda runner: I.UserSim.restore(exported, persona, sim_model, runner,
                                                     purpose=purpose),
        cpus=C.CPUS, memory_mb=C.MEMORY_MB, platform=C.PLATFORM or None, setup=False)


def continuation(ad: Path, ctx: list[dict], loop: Loop) -> list[dict]:
    """Every message the arm added after its start input, across its recompactions: each
    recompaction's pre_messages beyond what the context held then, and the rest of the loop
    (session.run_session resets loop.messages to post_messages at a boundary)."""
    out, start = [], len(ctx)
    for p in sorted(ad.glob("b*/boundary/pre_messages.json"), key=lambda q: int(q.parts[-3][1:])):
        out += _read(p)[start:]
        start = len(_read(p.parent / "post_messages.json"))
    return out + loop.messages[start:]


def sim_probe(sim: I.UserSim) -> str:
    return I.sha256(I.canon(sim.first_request(SIM_PROBE)))


def fork_check(env: I.InteractEnv, boundary: dict) -> dict:
    """A restored arm against its boundary: the same workspace fingerprint and the same
    simulator request for SIM_PROBE (both free)."""
    # the version the boundary was fingerprinted with (none recorded: v1, before 24 Oct's fix)
    version = int(boundary.get("fingerprint_version") or 1)
    fp, probe = I.sha256(env.fingerprint(version)), sim_probe(env.sim)
    return {"fingerprint_sha256": fp, "fingerprint_version": version,
            "sim_probe_sha256": probe, "ok": fp == boundary.get("fingerprint_sha256")
            and probe == boundary.get("sim_probe_sha256")}


def _commands(loop: Loop, env: I.InteractEnv) -> list[dict]:
    """The bash commands the arm actually ran: in the call that captured an ask_user, the
    tool calls after it were deferred, not run, so they are left out."""
    out = []
    last = loop.calls[-1]["call"] if loop.calls else None
    for c in loop.calls:
        tools = c.get("tools") or []
        if env.captured and c["call"] == last:
            cut = next((i for i, t in enumerate(tools) if t.get("tool") == "ask_user"),
                       len(tools))
            tools = tools[:cut]
        out += [{"call": c["call"], "cmd": t.get("cmd"), "rc": t.get("rc")}
                for t in tools if t.get("tool") == "bash"]
    return out


def _end_name(sr_end: str, env: I.InteractEnv) -> str:
    if env.captured:
        return "ask_user"
    return {"max_calls": "max_calls", "submitted": "submitted", "run_cap": "cap"}.get(
        sr_end, sr_end)


PRIMARY_ENDS = ("ask_user", "submitted", "max_calls")  # the plan's three readout endpoints
CHECK_NAMES = ("retention", "recoverability", "applicability", "decision", "design",
               "reference")
FREEZE_COMMITTED = C.REPO_ROOT / "results" / "sprint" / "S3-Q2" / "interact" / "freeze"


def committed_freeze_path(bdir: Path, cand_id: str) -> Path:
    """The tracked copy of a freeze record (data/raw is gitignored, so the copy under
    results/ is what gets committed before the first continuation)."""
    return FREEZE_COMMITTED / Path(bdir).parent.name / Path(bdir).name / f"{cand_id}.json"


def publish_freeze(bdir: Path, cand_id: str) -> Path:
    """Copy the freeze record to its tracked path, for the operator to commit (no overwrite)."""
    from compaction.s2 import interact_score as SC
    src = SC.freeze_path(bdir, cand_id)
    dst = committed_freeze_path(bdir, cand_id)
    if dst.exists():
        if dst.read_bytes() != src.read_bytes():
            raise FileExistsError(f"{dst} exists with different content")
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return dst


def verified_freeze(bdir: Path, cand_id: str, *, require_committed: bool = True) -> dict:
    """The freeze record an arm runs from (astra build r1 #5): intact (record_sha256), all six
    checks passed (`outcome` pass, or `jacob` pass where the card confirmed an unresolved one),
    and, unless `require_committed` is False (the stub smoke only), its tracked copy is in git
    HEAD byte for byte."""
    from compaction.s2 import interact_score as SC
    f = SC.read_freeze(bdir, cand_id)
    checks = f.get("checks") or {}
    bad = [n for n in CHECK_NAMES
           if "pass" not in ((checks.get(n) or {}).get("outcome"), (checks.get(n) or {}).get("jacob"))]
    if bad:
        raise ValueError(f"freeze {cand_id}: checks not passed: {bad}")
    if require_committed:
        rel = committed_freeze_path(bdir, cand_id).relative_to(C.REPO_ROOT)
        p = subprocess.run(["git", "-C", str(C.REPO_ROOT), "show", f"HEAD:{rel}"],
                           capture_output=True, timeout=60, check=False)
        if p.returncode != 0 or p.stdout != SC.freeze_path(bdir, cand_id).read_bytes():
            raise ValueError(f"freeze {cand_id}: not committed at {rel} (publish_freeze, then "
                             "commit it before any continuation)")
    return f


def frozen_source(bdir: Path, frz: dict, *, load_source=None) -> dict:
    """The freeze's summary source as stored now, refused unless it is exactly what was frozen
    (summary, summary index, kept indices, fingerprint and the resumed context's hash; astra
    build r2 #2), so regenerating a replay after the freeze can't change an arm's input."""
    from compaction.s2 import interact_score as SC
    src = (load_source or _load_source)(Path(bdir), frz["source_policy"])
    fs = frz.get("source") or {}
    bad = [k for k in ("summary", "summary_index", "kept_indices", "fingerprint")
           if fs.get(k) != src.get(k)]
    if not fs.get("post_sha256") or SC.canon_sha(src["post"]) != fs["post_sha256"]:
        bad.append("post_sha256")
    if bad:
        raise ValueError(f"source {frz['source_policy']} changed since the freeze: {bad}")
    return src


def expected_context(bdir: Path, frz: dict, kind: str, *, load_source=None) -> list[dict]:
    """The exact start input an arm of this freeze must have had."""
    src = frozen_source(bdir, frz, load_source=load_source)
    ctx, _ = arm_context(bdir, spec_from_freeze(frz, kind, 1), load_source=lambda b, s: src)
    return ctx


def spec_from_freeze(f: dict, kind: str, rep: int, readout: str = "primary") -> ArmSpec:
    block = f["repair_text"] if kind == "repair" else f["matched_text"]
    return ArmSpec(f["source_policy"], kind, rep, block, readout)


def _fresh_dir(d: Path) -> Path:
    """Refuse to reuse an execution directory (astra build r1 #7): a crashed arm is retired
    (`retire`) and run again under a new directory, never mixed into the old one."""
    if d.exists():
        raise FileExistsError(f"{d} exists; retire it first (s2_interact.py retire)")
    d.mkdir(parents=True)
    return d


def retire(d: Path) -> Path:
    """Move a crashed execution dir aside (arms/_retired/<name>-<utc>), kept for the record."""
    d = Path(d)
    dst = d.parent / "_retired" / f"{d.name}-{time.strftime('%Y%m%dT%H%M%S')}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    d.rename(dst)
    return dst


def _write_diffs(ad: Path, env: I.InteractEnv, fork_tree: str) -> None:
    """diff.patch = every change in the window, call by call from the fork (so an act later
    reverted still shows); states/NNNN.patch = fork to each recorded state; net.patch = fork to
    end; task.patch = base..HEAD (for grading)."""
    chain = [fork_tree, *[t["tree"] for t in env.trees]]
    parts = [env.diff_trees(a, b) for a, b in itertools.pairwise(chain) if a != b]
    (ad / "diff.patch").write_text("".join(parts))
    # each workspace state the window passed through, as a diff from the fork (negated acts
    # are scored per state)
    st = ad / "states"
    st.mkdir()
    for i, t in enumerate(chain[1:], 1):
        (st / f"{i:04d}.patch").write_text(env.diff_trees(fork_tree, t))
    (ad / "net.patch").write_text(env.diff_trees(fork_tree, chain[-1]))
    (ad / "task.patch").write_text(env.task_patch())
    _dump(ad / "trees.json", {"fork_tree": fork_tree, "trees": env.trees})


def run_arm(run_dir: Path, k: int, cand_id: str, kind: str, rep: int, task: InteractTask, *,
            agent_model: ChatModel, sim_model: ChatModel, inv, session=None,
            load_source=None, require_committed: bool = True) -> dict:
    """A primary continuation of candidate `cand_id` at boundary k (build split §7; plan
    "Readouts"). Its source and block come only from the verified freeze record. The
    simulator is restored but never called; the arm ends at its first ask_user (later tool
    calls in that response are deferred), a submission, or INTERACT_PRIMARY_CALLS calls. The
    end image is kept for a secondary (ask_user and max_calls; a submitted arm is graded from
    its task.patch)."""
    SS = session or _session_mod()
    t0 = time.monotonic()
    bdir = Path(run_dir) / f"b{k}"
    frz = verified_freeze(bdir, cand_id, require_committed=require_committed)
    spec = spec_from_freeze(frz, kind, rep)
    bj = _read(bdir / "boundary" / "boundary.json")
    src = frozen_source(bdir, frz, load_source=load_source)  # refuses a changed source
    ad = _fresh_dir(Path(run_dir) / "arms" / spec.slug)
    ctx, _ = arm_context(bdir, spec, load_source=lambda b, s: src)
    rj = {**_read(bdir / "run.json"), "arm": asdict(spec), "boundary_index": k}
    _dump(ad / "context.json", ctx)
    _dump(ad / "arm.json", {**asdict(spec), "cand_id": cand_id,
                            "freeze_sha256": frz.get("record_sha256"),
                            "boundary_index": k, "image": bj["tag"],
                            "input_sha256": I.sha256(I.canon(ctx)),
                            "source_summary": src["summary"],
                            "source_summary_sha256": I.sha256(src["summary"]),
                            "summary_index": src["summary_index"],
                            "block_index": block_index(spec.source, src["summary_index"]),
                            "block_format": ("separate_user_message"
                                             if spec.source in SEPARATE_BLOCK_SOURCES
                                             else "appended_to_summary"),
                            "created_utc": utc()})
    out = {"readout": "primary", "source_policy": spec.source, "cand_id": cand_id,
           "end": "error", "question": None, "n_calls": 0, "sim_calls": 0, "cost_usd": 0.0,
           "sim_cost_usd": 0.0, "recompactions": [], "error": None}
    env = None
    loop = None
    fork_tree = None
    try:
        env = _restore_env(inv, bj["tag"], bdir, task.persona, sim_model, f"sim:{spec.slug}")
        env.capture = True
        sim0 = env.sim.state.ask_user_call_count
        out["fork_check"] = fork_check(env, bj)
        if not out["fork_check"]["ok"]:
            raise RuntimeError(f"fork check failed: {out['fork_check']}")
        fork_tree = env.tree()
        env.track = True
        loop = Loop(list(ctx), agent_model, env, f"arm:{spec.slug}", tools=INTERACT_TOOLS)
        loop.ctx_tokens = est_tokens(ctx, agent_model.route.bytes_per_token)

        def after_call(lo) -> str | None:
            if env.captured:
                return "ask_user"
            return "submitted" if lo.submitted else None

        sr = SS.run_session(loop, env, None, run_dir=ad, run_json=rj,
                            threshold=C.INTERACT_THRESHOLD, policy="pi",
                            instruction_text=task.steps["02_implement"],
                            max_calls=C.INTERACT_PRIMARY_CALLS, run_cap_usd=0.0,
                            hooks=SS.SessionHooks(after_call=after_call),
                            previous=(arm_previous(src, spec), src["summary_index"]))
        out.update(end=_end_name(sr.end, env), error=sr.error,
                   recompactions=list(sr.boundary_calls),
                   question=(env.captured[0]["question"] if env.captured else None))
        if env.sim.state.ask_user_call_count != sim0:
            raise RuntimeError("capture mode called the simulator")
        _dump(ad / "deferred.json", env.deferred)
        _write_diffs(ad, env, fork_tree)
        if out["end"] in ("ask_user", "max_calls"):
            keep = SS.KeepStore(ad, f"{rj['run_key']}-{spec.slug}")
            out["end_image"] = env.boundary(keep, 0)["tag"]
    except Exception:  # noqa: BLE001 - recorded; the container is removed below
        out["end"] = "error"
        out["error"] = traceback.format_exc()[-6000:]
    finally:
        if loop is not None:
            out["n_calls"], out["cost_usd"] = loop.n_calls, round(loop.cost_usd, 6)
            _dump(ad / "calls.json", loop.calls)
            _dump(ad / "messages_end.json", loop.messages)
            _dump(ad / "commands.json", _commands(loop, env))
            _dump(ad / "continuation.json", continuation(ad, ctx, loop))
        if env is not None:
            env.close()
        out["seconds"] = round(time.monotonic() - t0, 1)
        _dump(ad / "readout.json", out)
    return out


def _resume_messages(msgs: list[dict], question: str | None, answer: str | None,
                     deferred_results: list[str]) -> list[dict]:
    """Fill the primary's last response's pending tool results by tool_call id: the captured
    ask_user (content PENDING_ASK) with `answer`, then each deferred call (content holding
    PENDING_DEFERRED), in order, with its result. Tool results the loop already produced
    (errors for unknown or malformed calls) are left as they are (astra build r2 #4)."""
    msgs = [dict(m) for m in msgs]
    ai = max(i for i, m in enumerate(msgs) if m.get("role") == "assistant" and m.get("tool_calls"))
    tool = [m for m in msgs[ai + 1:] if m.get("role") == "tool"]
    fill = {}
    if question is not None:
        asked = [m["tool_call_id"] for m in tool if m.get("content") == I.PENDING_ASK]
        if len(asked) != 1:
            raise RuntimeError(f"expected one pending ask_user, found {len(asked)}")
        fill[asked[0]] = answer
    pend = [m["tool_call_id"] for m in tool if I.PENDING_DEFERRED in K._text(m.get("content"))]
    for cid, res in zip(pend, deferred_results, strict=True):
        fill[cid] = res
    for i in range(ai + 1, len(msgs)):
        cid = msgs[i].get("tool_call_id")
        if msgs[i].get("role") == "tool" and cid in fill:
            msgs[i]["content"] = fill.pop(cid)
    if fill:
        raise RuntimeError(f"pending tool results not found: {sorted(fill)}")
    return msgs


def run_secondary(run_dir: Path, k: int, cand_id: str, kind: str, rep: int,
                  task: InteractTask, *, agent_model: ChatModel, sim_model: ChatModel, inv,
                  session=None, grade: Callable[[InteractTask, bytes, str], dict] | None = None,
                  require_committed: bool = True) -> dict:
    """Resume a primary with its own live simulator (plan "Secondary"). An arm that stopped at
    an ask_user sends it now, then runs its deferred calls in order; one that stopped at 60
    calls carries on; one that submitted stays ended and is graded as it is. Up to
    INTERACT_SECONDARY_CALLS calls counted from the fork; then the last implementation commit
    is graded."""
    from compaction.s2.loop import is_submission, observation_message
    SS = session or _session_mod()
    t0 = time.monotonic()
    bdir = Path(run_dir) / f"b{k}"
    frz = verified_freeze(bdir, cand_id, require_committed=require_committed)
    spec = spec_from_freeze(frz, kind, rep, "secondary")
    ad = Path(run_dir) / "arms" / spec.slug
    prim = _read(ad / "readout.json")
    sd = _fresh_dir(ad / "secondary")
    out = {"readout": "secondary", "source_policy": spec.source, "cand_id": cand_id,
           "end": "error", "question": None, "n_calls": 0, "sim_calls": 0, "cost_usd": 0.0,
           "sim_cost_usd": 0.0, "recompactions": [], "error": None,
           "primary_end": prim["end"]}
    g = grade or default_grade
    rid = f"{_read(bdir / 'run.json')['run_key']}-{spec.slug}"
    if prim["end"] == "submitted":
        patch = (ad / "task.patch").read_bytes()
        _dump(sd / "grade.json", g(task, patch, rid))
        out.update(end="primary_ended", error=None)
        _dump(sd / "readout.json", out)
        return out
    if prim["end"] not in ("ask_user", "max_calls"):
        out.update(end="not_resumable", error=f"primary ended {prim['end']}")
        _dump(sd / "readout.json", out)
        return out
    env = None
    loop = None
    try:
        env = _restore_env(inv, prim["end_image"], bdir, task.persona, sim_model,
                           f"sim:{spec.slug}:secondary")
        sim0 = env.sim.state.ask_user_call_count
        msgs = _read(ad / "messages_end.json")
        submitted = False
        if prim["end"] == "ask_user":
            answer = env.sim.ask(prim["question"])
            results = []
            for d in _read(ad / "deferred.json"):
                if d["tool"] == "ask_user":
                    results.append(env.sim.ask(d["question"]))
                else:
                    o = env.run_agent_command(d["cmd"])
                    submitted = submitted or is_submission(o.output, o.rc)
                    results.append(observation_message(o.rc, o.output, o.timed_out))
            msgs = _resume_messages(msgs, prim["question"], answer, results)
        _dump(sd / "start_messages.json", msgs)
        loop = Loop(msgs, agent_model, env, f"arm2:{spec.slug}", tools=INTERACT_TOOLS)
        loop.ctx_tokens = est_tokens(msgs, agent_model.route.bytes_per_token)
        rj = {**_read(bdir / "run.json"), "arm": asdict(spec), "boundary_index": k}
        if submitted:
            out["end"] = "submitted"
        else:
            sr = SS.run_session(loop, env, None, run_dir=sd, run_json=rj,
                                threshold=C.INTERACT_THRESHOLD, policy="pi",
                                instruction_text=task.steps["02_implement"],
                                max_calls=max(0, C.INTERACT_SECONDARY_CALLS - prim["n_calls"]),
                                run_cap_usd=C.INTERACT_SECONDARY_CAP_USD,
                                hooks=SS.SessionHooks(
                                    after_call=lambda lo: "submitted" if lo.submitted else None),
                                previous=_previous_from(ad, bdir, spec, prim))
            out.update(end=sr.end, error=sr.error, recompactions=list(sr.boundary_calls))
        out["sim_calls"] = env.sim.state.ask_user_call_count - sim0
        out["sim_cost_usd"] = round(sum(r.get("cost_usd") or 0.0 for r in env.sim.rounds), 6)
        _dump(sd / "sim_state_end.json", env.sim.export())
        patch = env.task_patch().encode()
        (sd / "task.patch").write_bytes(patch)
        _dump(sd / "grade.json", g(task, patch, rid))
    except Exception:  # noqa: BLE001
        out["end"] = "error"
        out["error"] = traceback.format_exc()[-6000:]
    finally:
        if loop is not None:
            out["n_calls"], out["cost_usd"] = loop.n_calls, round(loop.cost_usd, 6)
            _dump(sd / "calls.json", loop.calls)
            _dump(sd / "messages_end.json", loop.messages)
            _dump(sd / "commands.json", _commands(loop, env))
        if env is not None:
            _dump(sd / "ask_log.json", env.sim.ask_log)
            env.close()
        out["seconds"] = round(time.monotonic() - t0, 1)
        _dump(sd / "readout.json", out)
    return out


def arm_previous(src: dict, spec: ArmSpec) -> str:
    """The previous summary an arm's recompaction updates (pi's update prompt): the source's
    summary plus the arm's block, since the agent saw the block inside the summary message and
    the summary message itself is left out of the update's conversation. The same rule for
    repair and matched, so the arms stay matched across a recompaction. For OpenCode the block
    is its own user message, which the update's conversation already holds, so the previous
    summary is the source's alone."""
    if spec.source in SEPARATE_BLOCK_SOURCES:
        return src["summary"]
    return f"{src['summary'].rstrip()}\n\n{LEAD}\n{spec.block}"


def _previous_from(ad: Path, bdir: Path, spec: ArmSpec, prim: dict) -> tuple[str, int]:
    """For a secondary: the primary's last recompaction's summary, else the arm's start."""
    rec = sorted(ad.glob("b*/boundary/compaction.json"), key=lambda p: int(p.parts[-3][1:]))
    if rec:
        c = _read(rec[-1])
        return c["summary"], c["summary_index"]
    a = _read(ad / "arm.json")
    return arm_previous({"summary": a["source_summary"]}, spec), a["summary_index"]


def default_grade(task: InteractTask, patch: bytes, run_id: str) -> dict:
    from compaction.s2 import outcomes as O
    return O.grade_patch(task.info, patch, run_id)


# ------------------------------------------------------------------------------------------
# Docker watch and orchestration
# ------------------------------------------------------------------------------------------


@dataclass
class DockerWatch:
    """`docker stats` memory per pilot container every `every_s` to docker_stats.jsonl, and the
    daemon's OOM events (`docker events --filter event=oom`) for the pilot's label."""

    root: Path
    label: str = f"emx.sandbox={PURPOSE}"
    every_s: float = C.DOCKER_STATS_EVERY_S
    _stop: threading.Event = field(default_factory=threading.Event)
    _oom: set = field(default_factory=set)
    _threads: list = field(default_factory=list)
    _events: subprocess.Popen | None = None

    def start(self) -> DockerWatch:
        self._events = subprocess.Popen(
            ["docker", "events", "--filter", "event=oom", "--filter", f"label={self.label}",
             "--format", "{{json .}}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True)
        for fn in (self._read_events, self._sample):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def _read_events(self) -> None:
        for line in self._events.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            name = (ev.get("Actor") or {}).get("Attributes", {}).get("name")
            if name:
                self._oom.add(name)
                self._append("docker_oom.jsonl", {"utc": utc(), "container": name})

    def _sample(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                ids = subprocess.run(["docker", "ps", "-q", "--filter", f"label={self.label}"],
                                     capture_output=True, text=True, timeout=60,
                                     check=False).stdout.split()
                if ids:
                    p = subprocess.run(["docker", "stats", "--no-stream", "--format",
                                        "{{json .}}", *ids], capture_output=True, text=True,
                                       timeout=120, check=False)
                    for line in p.stdout.splitlines():
                        s = json.loads(line)
                        self._append("docker_stats.jsonl", {
                            "utc": utc(), "container": s.get("Name"),
                            "mem": s.get("MemUsage"), "mem_pct": s.get("MemPerc"),
                            "cpu_pct": s.get("CPUPerc")})
            self._stop.wait(self.every_s)

    def _append(self, name: str, row: dict) -> None:
        with _TAG_LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.root / name, "a") as f:
                f.write(json.dumps(row) + "\n")

    def oom_killed(self, container: str) -> bool:
        time.sleep(1.0)  # let a just-delivered event land
        return container in self._oom

    def stop(self) -> None:
        self._stop.set()
        if self._events is not None:
            with contextlib.suppress(Exception):
                self._events.terminate()
                self._events.wait(timeout=10)


def run_bases(configs: list[BaseConfig], run_one: Callable[[BaseConfig], dict], *,
              concurrency: int = C.INTERACT_CONCURRENCY, rerun: bool = True) -> list[dict]:
    """All base runs at once (Jacob, 24 Oct), then each infrastructure failure rerun once as a
    new attempt; if two or more ended infra_oom, the reruns go INTERACT_RERUN_CONCURRENCY at a
    time (plan "Wall-clock")."""
    def pool(cfgs, n):
        with ThreadPoolExecutor(max_workers=max(1, n)) as ex:
            return list(ex.map(run_one, cfgs))

    first = pool(configs, concurrency)
    if not rerun:  # this launch is itself the one rerun of an earlier infrastructure failure
        return first
    redo = [replace(c, attempt=next_attempt(c.root, c.task.task_id))
            for c, r in zip(configs, first, strict=True) if r.get("end") in INFRA_ENDS]
    n_oom = sum(r.get("end") == "infra_oom" for r in first)
    second = pool(redo, C.INTERACT_RERUN_CONCURRENCY if n_oom >= 2 else concurrency) if redo else []
    return first + second


def valid_comparison(bdir: Path, cand_id: str, *, require_committed: bool = True) -> dict:
    """Whether a candidate gives a valid repair comparison (plan "Go/no-go number"): a
    verified, committed freeze whose source is unchanged; all 4 primaries ran from it
    (arm.json bound to the freeze's sha and boundary), each ended at a permitted endpoint
    with no error and a passing fork check, and each started from exactly the frozen source's
    context plus its kind's frozen block."""
    why: list[str] = []
    try:
        frz = verified_freeze(bdir, cand_id, require_committed=require_committed)
    except (OSError, ValueError) as e:
        return {"valid": False, "why": [f"freeze: {e}"]}
    run_dir, k = Path(bdir).parent, int(Path(bdir).name[1:])
    try:
        expect = {kind: expected_context(bdir, frz, kind) for kind in ("repair", "matched")}
    except (OSError, ValueError) as e:
        return {"valid": False, "why": [f"source: {e}"]}
    ctxs: dict = {}
    hashes: dict = {}
    for kind in ("repair", "matched"):
        for rep in (1, 2):
            spec = spec_from_freeze(frz, kind, rep)
            ad = run_dir / "arms" / spec.slug
            try:
                a, r = _read(ad / "arm.json"), _read(ad / "readout.json")
                ctxs[(kind, rep)] = _read(ad / "context.json")
                hashes[(kind, rep)] = a.get("input_sha256")
            except OSError:
                why.append(f"{spec.slug}: missing")
                continue
            if a.get("cand_id") != cand_id or a.get("freeze_sha256") != frz.get("record_sha256") \
                    or a.get("boundary_index") != k:
                why.append(f"{spec.slug}: not bound to this freeze")
            if r.get("end") not in PRIMARY_ENDS or r.get("error"):
                why.append(f"{spec.slug}: end {r.get('end')}")
            if not (r.get("fork_check") or {}).get("ok"):
                why.append(f"{spec.slug}: fork check")
    for (kind, rep), c in ctxs.items():
        # the exact input: the frozen source's context plus this kind's frozen block, nothing
        # else (astra build r2 #2); the pair then differs only in its block by construction
        if I.canon(c) != I.canon(expect[kind]):
            why.append(f"{kind}-{rep}: start input differs from the frozen source + block")
        elif hashes.get((kind, rep)) != I.sha256(I.canon(expect[kind])):
            why.append(f"{kind}-{rep}: arm.json input_sha256 differs")
    return {"valid": not why, "why": why, "source_policy": frz.get("source_policy")}


def k_counts(root: Path, *, require_committed: bool = True) -> dict:
    """K, K_live and K_replay: distinct tasks with at least one valid comparison
    (`valid_comparison`). Reads endpoints and errors only, never act outcomes."""
    out = {"K": 0, "K_live": 0, "K_replay": 0, "tasks": {}, "invalid": {}}
    for rd in sorted(p for p in Path(root).iterdir() if (p / "run.json").exists()):
        task = _read(rd / "run.json")["task"]["task_id"]
        for fz in sorted(rd.glob("b*/boundary/freeze/*.json")):
            bdir, cid = fz.parent.parent.parent, fz.stem
            v = valid_comparison(bdir, cid, require_committed=require_committed)
            if v["valid"]:
                out["tasks"].setdefault(task, v["source_policy"])
            else:
                out["invalid"][f"{rd.name}/{bdir.name}/{cid}"] = v["why"]
    for src in out["tasks"].values():
        out["K"] += 1
        out["K_live" if src == "live" else "K_replay"] += 1
    return out


def funnel(root: Path) -> dict:
    """The candidate funnel over every boundary's candidates.json (plan "Q1, descriptive"):
    per disposition, and every candidate the screen excluded, with its reason and evidence.
    Check-6 exclusions (excluded:reference) carry the Haiku-written act scorer and the
    reference solution's lines it matched, so a bad scorer is visible (main session, 24 Oct).
    Writes <root>/funnel.json and <root>/funnel.md; reads no continuation output."""
    rows, counts = [], {}
    for cj in sorted(Path(root).glob("*/b*/boundary/candidates.json")):
        bdir = cj.parent.parent
        doc = _read(cj)
        for c in doc.get("candidates", []):
            d = c.get("disposition") or "none"
            counts[d] = counts.get(d, 0) + 1
            if not d.startswith("excluded:"):
                continue
            why = d.split(":", 1)[1]
            chk = (c.get("checks") or {}).get(why) or {}
            row = {"run": bdir.parent.name, "boundary": bdir.name, "cand_id": c["cand_id"],
                   "source_policy": c.get("source_policy"), "requirement": c.get("text"),
                   "reason": d, "check_reason": chk.get("reason"),
                   "evidence": chk.get("evidence")}
            if why == "reference":
                spec = c.get("act_spec") or {}
                row["scorer"] = {"description": spec.get("description"), "act": spec.get("act"),
                                 "decision": spec.get("decision")}
                row["reference_matches"] = (chk.get("evidence") or {}).get("matches")
            rows.append(row)
    out = {"created_utc": utc(), "counts": counts, "excluded": rows}
    _dump(Path(root) / "funnel.json", out)
    lines = ["# Candidate funnel", "", "| disposition | n |", "|---|---|",
             *[f"| {k} | {v} |" for k, v in sorted(counts.items())], "",
             "## Excluded candidates", ""]
    for r in rows:
        lines += [f"### {r['run']} {r['boundary']} {r['cand_id']} ({r['reason']})", "",
                  f"- Requirement: {r['requirement']}", f"- Source: {r['source_policy']}"]
        if "scorer" in r:
            lines += [f"- Scorer (Haiku-written): {r['scorer']['description']}",
                      f"  - act: `{json.dumps(r['scorer']['act'])}`",
                      f"  - decision: `{json.dumps(r['scorer']['decision'])}`",
                      ("- Reference solution lines that matched: "
                       f"`{json.dumps(r['reference_matches'])[:1500]}`")]
        else:
            lines.append(f"- Evidence: `{json.dumps(r['evidence'])[:1500]}`")
        lines.append("")
    (Path(root) / "funnel.md").write_text("\n".join(lines))
    return out
