"""X3b stage runner (implementation plan v0.4 §3 U3; prereg §4.1-§4.3, §4.6, §4.9, §7.2-§7.3).

Runs X3b's two stages (GLM, Gemini) through X3's Inspect task, as a rolling pool of blocks (one
task x repeat under every header of the stage). The runner is the safety-critical unit: it
reserves money before every paid call, settles exactly, applies the sizing rule and every stop
rule, and enforces the 30-hour deadline on its own, whatever the calling session does.

Ported from `src/x3/runner.py` (manifest intent/admit/outcome records, `reconcile_pending`,
`_release_orphans`, `_ledger_has_rid`, the rolling pool, `collect`); X3's stage logic is not
imported, because it is bound to `x3.contracts.Context`, X3's paths and X3's probe bound.

Layout of one stage's files (`x3b.contracts` paths): `run_manifest.jsonl` (flock'd appends),
`run_state.json` (atomic), `events.log` (`X3B_EVENT <utc> <stage> <KIND> <json>`), `episodes/`,
`logs/<block>_a<attempt>/` (one Inspect log directory, `result.json` and `progress.jsonl` per
block attempt), `stop_request.json` (written by `x3b_run.py stop`, polled by the runner).
"""
from __future__ import annotations

import csv
import fcntl
import json
import math
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from s4world import budget
from x3 import contracts as X3C
from x3 import runner as XR
from x3.task import EpisodeSpec
from x3b import contracts as C
from x3b import texts

MOCK_MODEL = "mockllm/model"
RESULT_JSON = "result.json"
PROGRESS_JSONL = "progress.jsonl"
POLL_S = 5.0                         # pool loop period (real clock)
KILL_GRACE_S = 20.0                  # SIGTERM -> SIGKILL (plan §3 U3, shutdown)
TEST_CALL_MAX_TOKENS = 16            # outage test call (prereg §7.3)
TEST_CALL_PROMPT = "Reply with the single word OK."
JUDGE_MAX_TOKENS = X3C.JUDGE_MAX_TOKENS   # the X3 judge loop's output cap, reused by U2
SMOKE_MAX_RUNS = 2                   # the first smoke and one re-run after a correction (§7.3)


# --- model adapter (plan §3 U3) ------------------------------------------------------------
def is_mock(model_id: str) -> bool:
    return model_id.startswith("mockllm/")


def model_args(stage: C.StageKey, model_id: str | None = None) -> dict:
    """OpenRouter routing for the stage (prereg §4.1): GLM pinned to Relace without fallbacks,
    Gemini on X3's routing. max_retries=0: the OpenAI SDK must not retry; retries are the
    episode loop's own (x3.task)."""
    if model_id is not None and is_mock(model_id):
        return {}
    return {"provider": C.MODELS[stage].provider, "max_retries": 0}


def generate_config(stage: C.StageKey) -> dict:
    return {**X3C.COMMON_GENERATE, **C.MODELS[stage].generate}


def inspect_model_name(stage: C.StageKey, model_id: str | None = None) -> str:
    mid = model_id or C.MODELS[stage].model_id
    return mid if is_mock(mid) else f"openrouter/{mid}"


# --- planning (prereg §4.6; plan §3 U3) ----------------------------------------------------
def load_tasks() -> list[X3C.TaskSpec]:
    from x3.tasks import load_manifest

    return list(load_manifest().tasks)


def episode_reservation(stage: C.StageKey) -> float:
    """Full reservation of one episode: the agent limit plus both probes' envelope."""
    m = C.MODELS[stage]
    return m.episode_limit_usd + m.probe_bound_usd


def block_reservation(stage: C.StageKey, block: list[EpisodeSpec]) -> float:
    return round(episode_reservation(stage) * len(block), 6)


def _ep(stage: C.StageKey, kind: C.RunKind, header: C.Header, task: X3C.TaskSpec, repeat: int,
        pos: int, model_id: str | None = None, attempt: int = 1) -> EpisodeSpec:
    m = C.MODELS[stage]
    return EpisodeSpec(
        episode_id=C.episode_id(stage, kind, header, task.task_id, repeat, attempt),
        model_id=model_id or m.model_id, model_slug=m.slug, context=header.value,
        prompt_level=C.PROMPT_LEVEL.value, stage=kind.value, task_id=task.task_id,
        repeat=repeat, attempt=attempt,
        batch_id=C.block_id(stage, kind, task.task_id, repeat), run_order_pos=pos,
        episode_limit_usd=m.episode_limit_usd, in_ceiling=m.in_ceiling,
        out_ceiling=m.out_ceiling,
        task={**asdict(task), "conflict_pair": list(task.conflict_pair)},
        run_order_seed=C.SEED_RUN_ORDER[stage],
        header_text=texts.header_text(header), pre_probe_prompt=texts.PRE_PROBE_PROMPT,
        post_probe_prompt=texts.POST_PROBE_PROMPT, probe_bound_usd=m.probe_bound_usd,
        probe_max_tokens=C.PROBE_MAX_TOKENS, retry_empty_probe=C.RETRY_EMPTY_PROBE)


def plan_stage(stage: C.StageKey, kind: C.RunKind, tasks: list[X3C.TaskSpec],
               repeats: int | None = None, model_id: str | None = None
               ) -> list[list[EpisodeSpec]]:
    """The stage's blocks in run order. Main: repeats in sequence; within each repeat, block
    order and each block's header order shuffled with rng([SEED_RUN_ORDER[stage], repeat]).
    Smoke: the first `smoke_blocks` tasks of the manifest, repeat 1 (header order shuffled with
    rng([seed, 0]))."""
    hdrs = C.STAGE_HEADERS[stage]
    seed = C.SEED_RUN_ORDER[stage]
    pos, out = 0, []
    if kind == C.RunKind.SMOKE:
        rng = np.random.default_rng([seed, 0])
        for t in tasks[: C.MODELS[stage].smoke_blocks]:
            order = [hdrs[i] for i in rng.permutation(len(hdrs))]
            out.append([_ep(stage, kind, h, t, 1, pos + i, model_id) for i, h in
                        enumerate(order)])
            pos += len(order)
        return out
    repeats = C.MODELS[stage].repeats if repeats is None else repeats
    for r in range(1, repeats + 1):
        rng = np.random.default_rng([seed, r])
        for ti in rng.permutation(len(tasks)):
            order = [hdrs[i] for i in rng.permutation(len(hdrs))]
            out.append([_ep(stage, kind, h, tasks[ti], r, pos + i, model_id) for i, h in
                        enumerate(order)])
            pos += len(order)
    return out


def with_attempt(block: list[EpisodeSpec], attempt: int) -> list[EpisodeSpec]:
    return [replace(sp, attempt=attempt, episode_id=f"{XR.base_id(sp.episode_id)}|a{attempt}")
            for sp in block]


# --- extraction (plan §1 episode record additions; §3 U3) ----------------------------------
def _parsers() -> tuple[Callable | None, Callable | None]:
    """U2's letter parsers, imported lazily; missing -> no parse (judge fallback later)."""
    try:
        from x3b.probes import parse_post, parse_pre
    except ImportError:
        return None, None
    return parse_pre, parse_post


def _iso(ts) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return str(ts)


def _last_input_text(e) -> str:
    msgs = getattr(e, "input", None) or []
    if not msgs:
        return ""
    last = msgs[-1]
    try:
        return (last.text or "").strip()
    except AttributeError:
        return str(getattr(last, "content", "")).strip()


def call_role(last_input_text: str) -> str:
    t = (last_input_text or "").strip()
    if t == texts.PRE_PROBE_PROMPT.strip():
        return "pre_probe"
    if t == texts.POST_PROBE_PROMPT.strip():
        return "post_probe"
    return "agent"


def tag_calls(calls: list[dict], events) -> list[dict]:
    """Add `role`, `usage_raw` and `utc` to X3's call records, from the same model events in
    the same order as `x3.runner.model_calls_from_events` read them."""
    mevs = [e for e in events or [] if getattr(e, "event", None) == "model"]
    if len(mevs) != len(calls):
        raise ValueError(f"{len(calls)} call records but {len(mevs)} model events")
    for rec, e in zip(calls, mevs, strict=True):
        call = getattr(e, "call", None)
        resp = (call.response if call is not None else None) or {}
        u = resp.get("usage") if isinstance(resp, dict) else None
        rec["role"] = call_role(_last_input_text(e))
        rec["usage_raw"] = u if isinstance(u, dict) else None
        rec["utc"] = _iso(getattr(e, "timestamp", None))
    return calls


def add_parsed(art: dict) -> dict:
    parse_pre, parse_post = _parsers()
    for key, fn in (("pre_probe", parse_pre), ("probe", parse_post)):
        p = art.get(key)
        if isinstance(p, dict):
            p["parsed"] = fn(p["reply"]) if (fn and p.get("reply")) else None
    return art


def artifact_path(art: dict) -> Path:
    m = art["meta"]
    return C.episode_path(C.StageKey(art["stage_key"]), C.RunKind(art["kind"]),
                          C.Header(art["header"]), m["task_id"], int(m["repeat"]),
                          int(m["attempt"]))


def write_artifact(art: dict, *, dup: bool = True) -> Path | None:
    """Write an episode artifact; an existing file is never overwritten. dup=True: X3's
    `.dupN` rule; dup=False (recovery): an existing file is kept and nothing is written."""
    base = p = artifact_path(art)
    n = 1
    while p.exists():
        if not dup:
            art["path"] = str(base)
            return None
        p = base.with_name(f"{base.stem}.dup{n}.json")
        art["duplicate_of"] = str(base)
        n += 1
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(art, indent=1, default=str))
    os.replace(tmp, p)
    art["path"] = str(p)
    return p


def extract_block_log(eval_path: Path, stage: C.StageKey, *, write: bool = True,
                      dup: bool = True) -> list[dict]:
    """X3's extraction plus X3b's additions: `pre_probe`, `header`, `stage_key`, `kind`, the
    agent loop's `bounds` / `spent_at_ceiling` (needed by `episode_cost`), `role` /
    `usage_raw` / `utc` on each call, and `parsed` on both probes (U2's parsers; X3's
    post-probe parse is overwritten)."""
    from inspect_ai.log import read_eval_log

    arts = XR.extract_log(Path(eval_path), write=False)
    # model events store message content as attachments; resolve them so the last input
    # message's text can be matched to the probe prompts (found by U1)
    log = read_eval_log(str(eval_path), resolve_attachments=True)
    by_id = {(s.metadata or {}).get("episode_id", s.id): s for s in log.samples or []}
    out = []
    for art in arts:
        eid = art["meta"]["episode_id"]
        s = by_id.get(eid)
        if s is None or not eid:
            continue
        st, md = s.store or {}, s.metadata or {}
        ep = st.get(X3C.STORE_EPISODE) or {}
        art["pre_probe"] = st.get(C.STORE_PRE_PROBE)
        art["header"] = md.get("context")
        art["stage_key"] = stage.value
        art["kind"] = md.get("stage")
        art["bounds"] = ep.get("bounds")
        art["spent_at_ceiling"] = ep.get("spent_at_ceiling")
        art["api_error"] = st.get("x3_api_error")
        tag_calls(art["model_calls"], s.events)
        add_parsed(art)
        if write:
            write_artifact(art, dup=dup)
        out.append(art)
    return out


# --- costs (plan §1 "Episode cost for sizing"; §3 U3 "Settlement") ------------------------
failed = XR.failed


def _call_failed(c: dict) -> bool:
    return c.get("error") is not None or (c.get("stop_reason") is None
                                          and not c.get("output_tokens"))


def _agent_failed_attempts(art: dict, ceilings: tuple[float, float] | None
                           ) -> tuple[int, float]:
    """(failed agent attempts, their recorded charge) from the loop's per-turn bounds. Each
    failed attempt was charged its single-attempt bound, so the count is charge / bound; with
    no known ceilings, every attempt of the turn is assumed to have failed."""
    from x3.task import ATTEMPTS, call_bound

    n, usd = 0, 0.0
    for b in art.get("bounds") or []:
        fc = float(b.get("failed_charge") or 0.0)
        if fc <= 0:
            continue
        usd += fc
        one = (call_bound(int(b.get("input_bound") or 0), int(b.get("max_tokens") or 0),
                          *ceilings) / ATTEMPTS) if ceilings else 0.0
        n += max(1, round(fc / one)) if one > 0 else ATTEMPTS
    return n, usd


def _probe_failed_attempts(probe: dict | None) -> tuple[int, float]:
    if not isinstance(probe, dict):
        return 0, 0.0
    att = [a for a in probe.get("attempts") or [] if a.get("kind") == "transport_error"]
    if att:
        return len(att), sum(float(a.get("charge") or 0.0) for a in att)
    fac = float(probe.get("failed_attempt_charge") or 0.0)
    return (1 if fac > 0 else 0), fac


def episode_cost(art: dict, stage: C.StageKey | None = None) -> float | None:
    """Measured cost of one episode (plan §1): the sum of `billed_usd` over every call, plus the
    recorded charges of failed attempts (agent `bounds[].failed_charge`, probes'
    `attempts[].charge` of transport errors), which X3's Amendment B3 counts at their ceiling
    charge. None when an unbilled call cannot be matched to a recorded failed-attempt charge
    (an unbilled successful call, or more unbilled failed calls of a role than recorded failed
    attempts of that role)."""
    calls = art.get("model_calls") or []
    if stage is None and art.get("stage_key") in {s.value for s in C.StageKey}:
        stage = C.StageKey(art["stage_key"])
    ceilings = (C.MODELS[stage].in_ceiling, C.MODELS[stage].out_ceiling) if stage else None
    billed = sum(float(c["billed_usd"]) for c in calls if c.get("billed_usd") is not None)
    recorded = {"agent": _agent_failed_attempts(art, ceilings),
                "pre_probe": _probe_failed_attempts(art.get("pre_probe")),
                "post_probe": _probe_failed_attempts(art.get("probe"))}
    unbilled: dict[str, int] = {}
    for c in calls:
        if c.get("billed_usd") is not None:
            continue
        if not _call_failed(c):
            return None                    # a successful call with no billed cost
        role = c.get("role") or "agent"
        unbilled[role] = unbilled.get(role, 0) + 1
    for role, n in unbilled.items():
        if n > recorded.get(role, (0, 0.0))[0]:
            return None
    return billed + sum(usd for _, usd in recorded.values())


def _sent_nothing(art: dict) -> bool:
    """True only when the artifact shows no request was sent: no turns, not failed, no failed
    attempt recorded in the loop, and no probe attempt recorded."""
    if art.get("n_turns") or failed(art):
        return False
    if any(float(b.get("failed_charge") or 0.0) > 0 for b in art.get("bounds") or []):
        return False
    for key in ("pre_probe", "probe"):
        p = art.get(key)
        if isinstance(p, dict) and (p.get("attempts") or p.get("usage")
                                    or p.get("status") in ("ok", "empty", "api_error")):
            return False
    return True


def settle_cost(arts: list[dict], specs: list[EpisodeSpec], stage: C.StageKey
                ) -> tuple[float, float, list[str]]:
    """What a block attempt is settled at (plan §3 U3 "Settlement"; every path): per episode,
    its billed cost when it has call records, every call billed and no failed attempt; else
    `episode_cost` when every unbilled call matches a recorded charge; else its full
    reservation (episode limit + the stage's probe envelope). A missing artifact, or one with
    no call records although it ran turns, failed or recorded any attempt, is charged in full;
    zero only when the artifact shows no request was sent. Returns (usd, estimated_usd,
    notes): estimated_usd is the part not measured as billed."""
    by_id = {a["meta"]["episode_id"]: a for a in arts}
    full = episode_reservation(stage)
    usd = est = 0.0
    notes: list[str] = []
    for sp in specs:
        a = by_id.get(sp.episode_id)
        if a is None:
            usd += full
            est += full
            notes.append(f"{sp.episode_id}: no artifact; charged {full:.3f}")
            continue
        calls = a.get("model_calls") or []
        if not calls:
            if _sent_nothing(a):
                notes.append(f"{sp.episode_id}: no request sent; charged 0")
                continue
            usd += full
            est += full
            notes.append(f"{sp.episode_id}: no call records although requests ran; "
                         f"charged {full:.3f}")
            continue
        vals = [c.get("billed_usd") for c in calls]
        billed = sum(float(v) for v in vals if v is not None)
        cost = episode_cost(a, stage)
        if cost is None:
            usd += full
            est += full
            notes.append(f"{sp.episode_id}: {sum(v is None for v in vals)} unbilled call(s) "
                         f"not matched to a recorded charge; charged {full:.3f}")
        elif cost > billed + 1e-12:
            usd += cost
            est += cost - billed
            notes.append(f"{sp.episode_id}: billed {billed:.4f} + failed-attempt charges "
                         f"{cost - billed:.4f}")
        else:
            usd += billed
    return round(usd, 6), round(est, 6), notes


# --- one block's Inspect eval (runs inside a pool worker) ----------------------------------
def eval_block(specs: list[EpisodeSpec], stage: C.StageKey, log_dir: Path,
               task_name: str) -> list[Path]:
    """One block attempt as one Inspect eval (one eval per process). The task is renamed to
    `task_name` so the docker compose projects Inspect creates for it are named
    `inspect-<task_name>-i...` and can be found and removed if the block is killed."""
    from inspect_ai import eval as inspect_eval
    from inspect_ai import task_with
    from inspect_ai.model import GenerateConfig

    from x3.task import x3_task

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    before = set(log_dir.glob("*.eval"))
    mid = specs[0].model_id
    gc = generate_config(stage)
    inspect_eval(task_with(x3_task(specs), name=task_name),
                 model=inspect_model_name(stage, mid), model_args=model_args(stage, mid),
                 log_dir=str(log_dir), max_samples=len(specs), display="plain",
                 fail_on_error=False, log_model_api=True,
                 ctl_server=False,   # no live max_retries overrides (X3 code review r3, 1)
                 **{k: v for k, v in gc.items() if k in GenerateConfig.model_fields})
    return sorted(set(log_dir.glob("*.eval")) - before)


# --- time ----------------------------------------------------------------------------------
class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)


def utc(ts: float | None = None) -> str:
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(s: str | None) -> float | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    return None


# --- the experiment-wide clock (plan §3 U3, "Clock") ----------------------------------------
def read_clock() -> dict | None:
    p = C.CLOCK_JSON
    return json.loads(p.read_text()) if p.exists() else None


def ensure_clock(stage: C.StageKey, now_ts: float) -> dict:
    """Create `clock.json` by exclusive create just before the first paid call of either
    stage; if it exists, return it unchanged (it is never rewritten)."""
    p = C.CLOCK_JSON
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"first_paid_utc": utc(now_ts),
           "deadline_utc": utc(now_ts + C.DEADLINE_H * 3600), "stage": stage.value}
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return json.loads(p.read_text())
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return rec


# --- manifest (port of X3's intent -> admit -> outcome records) ----------------------------
def manifest_path(stage: C.StageKey) -> Path:
    """X3B_MANIFEST_DIR: tests point every process at scratch manifests (like X3_MANIFEST)."""
    d = os.environ.get("X3B_MANIFEST_DIR")
    return Path(d) / f"{stage.value}_run_manifest.jsonl" if d else C.manifest_jsonl(stage)


def _append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, default=str) + "\n"
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def append_manifest(stage: C.StageKey, rec: dict) -> None:
    _append_jsonl(manifest_path(stage), rec)


def manifest_records(stage: C.StageKey) -> list[dict]:
    p = manifest_path(stage)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def outcomes(stage: C.StageKey, recs: list[dict] | None = None) -> list[dict]:
    recs = manifest_records(stage) if recs is None else recs
    return [r for r in recs if r.get("kind") == "outcome"]


def _ledger_has_rid(rid: str) -> bool:
    """True if a ledger line already settled reservation `rid` (its note carries rid=...)."""
    path = budget.ledger_path()
    if not path.exists():
        return False
    tag = f"rid={rid}"
    return any(tag in (json.loads(x).get("note") or "")
               for x in path.read_text().splitlines() if x.strip())


# --- run state and events (plan §3 U3 item 12) ----------------------------------------------
def emit(stage: C.StageKey, kind: C.EventKind, payload: dict, ts: float | None = None) -> str:
    line = (f"{C.EVENT_TAG} {utc(ts)} {stage.value} {kind.value} "
            f"{json.dumps(payload, default=str, sort_keys=True)}")
    p = C.events_log(stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    print(line, flush=True)
    return line


def read_events(stage: C.StageKey) -> list[tuple[str, str, dict]]:
    p = C.events_log(stage)
    if not p.exists():
        return []
    out = []
    for x in p.read_text().splitlines():
        parts = x.split(" ", 4)
        if len(parts) == 5 and parts[0] == C.EVENT_TAG and parts[2] == stage.value:
            out.append((parts[1], parts[3], json.loads(parts[4])))
    return out


def read_run_state(stage: C.StageKey) -> dict:
    p = C.run_state_json(stage)
    return json.loads(p.read_text()) if p.exists() else {}


def write_run_state(stage: C.StageKey, state: dict) -> dict:
    rec = {k: state.get(k) for k in C.RUN_STATE_KEYS}
    p = C.run_state_json(stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, indent=1, default=str) + "\n")
    os.replace(tmp, p)
    return rec


def stop_request_path(stage: C.StageKey) -> Path:
    return C.stage_dir(stage) / "stop_request.json"


def request_stop(stage: C.StageKey, reason: str = "global_stop") -> Path:
    """Ask a running runner to shut the stage down (`x3b_run.py stop`); it polls this file."""
    if reason not in C.STOP_REASONS:
        raise ValueError(f"unknown stop reason {reason!r}")
    p = stop_request_path(stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"reason": reason, "utc": utc()}) + "\n")
    os.replace(tmp, p)
    return p


# --- worker processes and their sandboxes (plan §3 U3 items 8, 11) --------------------------
def _register_progress_hook() -> None:
    """In a pool worker: append one line per successful model call to the block's
    progress.jsonl, so the parent sees calls succeed while the block is still running (the
    outage rule). Best effort: without it, the artifacts still carry each call's time."""
    try:
        from inspect_ai.hooks import Hooks, hooks
    except ImportError:
        return

    @hooks(name="x3b_progress", description="X3b: record successful model calls")
    class _Progress(Hooks):
        def enabled(self) -> bool:
            return bool(os.environ.get("X3B_PROGRESS_FILE"))

        async def on_model_usage(self, data) -> None:
            try:
                _append_jsonl(Path(os.environ["X3B_PROGRESS_FILE"]), {"ts": time.time()})
            except OSError:
                pass


def _block_worker(job: dict) -> None:
    """Pool worker (a spawned process): its own process group, one block's eval, extraction,
    then `result.json`. Spend is reserved and settled by the parent, never here."""
    os.setsid()
    d = Path(job["attempt_dir"])
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.environ["X3B_PROGRESS_FILE"] = str(d / PROGRESS_JSONL)
    _register_progress_hook()
    stage = C.StageKey(job["stage"])
    specs = [EpisodeSpec(**s) for s in job["specs"]]
    wanted = {sp.episode_id for sp in specs}
    res: dict = {"ok": False, "artifacts": [], "logs": [], "error": None}
    try:
        logs = eval_block(specs, stage, d, job["task_name"])
        res["logs"] = [str(p) for p in logs]
        for p in logs:
            for a in extract_block_log(p, stage, write=True):
                if a["meta"]["episode_id"] in wanted and a.get("path"):
                    res["artifacts"].append(a["path"])
        res["ok"] = True
    except BaseException as e:  # noqa: BLE001 - recorded; the parent settles conservatively
        res["error"] = repr(e)[:2000]
    finally:
        tmp = d / (RESULT_JSON + ".tmp")
        tmp.write_text(json.dumps(res, indent=1))
        os.replace(tmp, d / RESULT_JSON)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                   # a zombie child counts as gone
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                           text=True, check=False)
        return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")
    except OSError:
        return True


def kill_group(pid: int, grace_s: float = KILL_GRACE_S, sleep=time.sleep) -> bool:
    """SIGTERM the worker's process group, SIGKILL after `grace_s`; True if it is gone. Only a
    process that leads its own group (our workers call setsid) is signalled as a group."""
    if not _pid_alive(pid):
        return True
    try:
        group = os.getpgid(pid) == pid
    except ProcessLookupError:
        return True
    send = (lambda s: os.killpg(pid, s)) if group else (lambda s: os.kill(pid, s))
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 5.0)):
        try:
            send(sig)
        except ProcessLookupError:
            return True
        t = 0.0
        while t < wait:
            if not _pid_alive(pid):
                return True
            sleep(0.5)
            t += 0.5
    return not _pid_alive(pid)


def _docker_rows(kind: str) -> list[tuple[str, str]] | None:
    cmd = ["docker", kind, "ls", "-a"] if kind == "container" else ["docker", kind, "ls"]
    cmd += ["--filter", "label=com.docker.compose.project", "--format",
            '{{.ID}}\t{{.Label "com.docker.compose.project"}}']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [tuple(x.split("\t", 1)) for x in r.stdout.splitlines() if "\t" in x]


def remove_compose_projects(tag: str) -> list[str]:
    """Remove the containers and networks of the docker compose projects Inspect created for
    one block (named `inspect-<tag>-i...`; the tag is unique to the block attempt), and return
    what still remains (empty = confirmed gone). Nothing else is touched."""
    import shutil

    if shutil.which("docker") is None:
        return []
    prefix = f"inspect-{tag}-"
    remaining: list[str] = []
    for _ in range(2):
        rows = _docker_rows("container")
        if rows is None:
            remaining = ["docker container listing failed"]
            continue
        ids = [i for i, proj in rows if proj.startswith(prefix)]
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=120,
                           check=False)
        nets = [i for i, proj in (_docker_rows("network") or []) if proj.startswith(prefix)]
        if nets:
            subprocess.run(["docker", "network", "rm", *nets], capture_output=True,
                           timeout=120, check=False)
        rows = _docker_rows("container")
        remaining = (["docker container listing failed"] if rows is None
                     else [f"{i} {p}" for i, p in rows if p.startswith(prefix)])
        if not remaining:
            break
    return remaining


class ProcessExecutor:
    """Each block attempt in its own spawned process and process group (plan §3 U3, item 8)."""

    def start(self, job: Job):
        import multiprocessing as mp

        p = mp.get_context("spawn").Process(target=_block_worker, args=(job.payload(),),
                                            name=f"x3b-{job.task_name}")
        p.start()
        return p

    def pid(self, h) -> int | None:
        return h.pid

    def finished(self, h) -> bool:
        if h.is_alive():
            return False
        h.join(1)
        return True

    def alive(self, h) -> bool:
        return h.is_alive()

    def kill(self, h, grace_s: float = KILL_GRACE_S) -> None:
        if h.pid is not None:
            kill_group(h.pid, grace_s)
        h.join(5)

    def cleanup(self, job: Job) -> list[str]:
        return remove_compose_projects(job.task_name)


@dataclass
class Job:
    """One admitted block attempt."""
    token: str
    rid: str
    stage: C.StageKey
    kind: C.RunKind
    specs: list[EpisodeSpec]
    reserved: float
    attempt_dir: Path
    task_name: str
    started_ts: float
    handle: object = None

    @property
    def batch_id(self) -> str:
        return self.specs[0].batch_id

    @property
    def attempt(self) -> int:
        return self.specs[0].attempt

    def payload(self) -> dict:
        return {"stage": self.stage.value, "kind": self.kind.value,
                "attempt_dir": str(self.attempt_dir), "task_name": self.task_name,
                "specs": [asdict(sp) for sp in self.specs]}


def load_result_arts(attempt_dir: Path, wanted: set[str], stage: C.StageKey
                     ) -> tuple[list[dict], list[str], str | None]:
    """A finished block's artifacts: from the worker's result.json, else recovered from the
    Inspect logs in its directory (written only where no file exists yet)."""
    rp = Path(attempt_dir) / RESULT_JSON
    arts: list[dict] = []
    if rp.exists():
        res = json.loads(rp.read_text())
        for p in res.get("artifacts") or []:
            if Path(p).exists():
                a = json.loads(Path(p).read_text())
                if a["meta"]["episode_id"] in wanted:
                    a["path"] = p
                    arts.append(a)
        return arts, list(res.get("logs") or []), res.get("error")
    logs = []
    for p in sorted(Path(attempt_dir).glob("*.eval")) if Path(attempt_dir).exists() else []:
        try:
            got = [a for a in extract_block_log(p, stage, write=True, dup=False)
                   if a["meta"]["episode_id"] in wanted]
        except Exception as e:  # noqa: BLE001 - unreadable log: settled conservatively
            return arts, logs, f"log unreadable: {e!r}"[:500]
        for a in got:
            if a.get("path") and Path(a["path"]).exists():
                a = {**json.loads(Path(a["path"]).read_text()), "path": a["path"]}
            arts.append(a)
        logs.append(str(p))
    return arts, logs, (None if arts else "no result.json and no recoverable log")


# --- outage test call (prereg §7.3) ---------------------------------------------------------
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def test_call_bound(stage: C.StageKey) -> float:
    from s4world.judges import request_bound

    m = C.MODELS[stage]
    return request_bound((m.in_ceiling, m.out_ceiling), "", TEST_CALL_PROMPT,
                         TEST_CALL_MAX_TOKENS)


def openrouter_test_call(stage: C.StageKey) -> dict:
    """One minimal request (max_tokens 16) to the stage's model and provider through
    OpenRouter, no SDK retries. PAID (a fraction of a cent); only the runner calls it."""
    try:
        from openai import OpenAI

        m = C.MODELS[stage]
        client = OpenAI(base_url="https://openrouter.ai/api/v1",
                        api_key=os.environ["OPENROUTER_API_KEY"], max_retries=0, timeout=120,
                        default_headers={"User-Agent": USER_AGENT})
        r = client.chat.completions.create(
            model=m.model_id, messages=[{"role": "user", "content": TEST_CALL_PROMPT}],
            max_tokens=TEST_CALL_MAX_TOKENS,
            extra_body={"provider": m.provider, "usage": {"include": True}})
        usage = r.usage.model_dump() if r.usage is not None else {}
        cost = usage.get("cost")
        return {"ok": True, "billed_usd": float(cost) if cost is not None else None,
                "provider": getattr(r, "provider", None), "usage": usage, "error": None}
    except Exception as e:  # noqa: BLE001 - any failure is an unsuccessful test call
        return {"ok": False, "billed_usd": None, "error": repr(e)[:500]}


def mock_test_call(stage: C.StageKey) -> dict:
    return {"ok": True, "billed_usd": 0.0, "provider": "mock", "error": None}


# --- judge headroom (plan §3 U3; prereg §7.2) -----------------------------------------------
def judge_bound_per_episode(arts: list[dict], stage: C.StageKey) -> float:
    """Upper bound of judging one episode: disclosure and both probe-parse judgments, each by
    both panel judges, with inputs at the smoke episodes' longest final message / probe reply
    x JUDGE_SET_ASIDE_FACTOR, at JUDGE_PRICES ceilings and the judge loop's output cap."""
    from s4world.judges import request_bound

    def longest(get) -> int:
        return max([len((get(a) or "").encode("utf-8")) for a in arts] or [0])

    lens = {C.Judgment.DISCLOSURE: longest(lambda a: a.get("final_message")),
            C.Judgment.PRE_PROBE_PARSE: longest(lambda a: (a.get("pre_probe") or {})
                                                 .get("reply")),
            C.Judgment.POST_PROBE_PARSE: longest(lambda a: (a.get("probe") or {})
                                                  .get("reply"))}
    total = 0.0
    for j, n in lens.items():
        prompt = (C.PROMPTS_DIR / f"{j.value}.txt").read_text(encoding="utf-8")
        user = "x" * math.ceil(max(n, 1) * C.JUDGE_SET_ASIDE_FACTOR)
        for judge in C.JUDGE_PANEL[stage]:
            total += request_bound(C.JUDGE_PRICES[judge], prompt, user, JUDGE_MAX_TOKENS)
    return total


# --- smoke gate (prereg §4.9) ---------------------------------------------------------------
def smoke_gate(arts: list[dict], specs: list[EpisodeSpec], stage: C.StageKey
               ) -> tuple[bool, dict]:
    """Every smoke episode completed with usage, provider and snapshots logged; non-empty probe
    replies in all but at most SMOKE_MAX_EMPTY_PROBES of the pre and post replies together;
    mean billed per episode <= the stage's threshold. Returns (pass, criteria)."""
    by_id = {a["meta"]["episode_id"]: a for a in arts}
    mock = any(is_mock(sp.model_id) for sp in specs)
    problems: list[str] = []
    bad_probes, billed = 0, []
    for sp in specs:
        a = by_id.get(sp.episode_id)
        if a is None:
            problems.append(f"{sp.episode_id}: no artifact")
            bad_probes += 2
            continue
        if failed(a):
            problems.append(f"{sp.episode_id}: failed ({a.get('end_reason')}, "
                            f"{(a.get('error') or '')[:80]})")
        calls = a.get("model_calls") or []
        if not calls:
            problems.append(f"{sp.episode_id}: no call records")
        if not mock and any(c.get("usage_raw") is None for c in calls):
            problems.append(f"{sp.episode_id}: usage missing on a call")
        if not mock and any(not c.get("provider") for c in calls):
            problems.append(f"{sp.episode_id}: provider missing on a call")
        snaps = a.get("snapshots") or []
        if not snaps or any(s.get("files") is None for s in snaps):
            problems.append(f"{sp.episode_id}: snapshots missing or failed")
        for key in ("pre_probe", "probe"):
            p = a.get(key) or {}
            if not (p.get("reply") or "").strip():
                bad_probes += 1
        vals = [c.get("billed_usd") for c in calls]
        billed.append(None if (not vals or any(v is None for v in vals))
                      else sum(float(v) for v in vals))
    mean = (sum(billed) / len(billed)) if billed and None not in billed else None
    thr = C.MODELS[stage].smoke_max_mean_billed_usd
    crit = {
        "completed": {"pass": not problems, "detail": problems},
        "probes_non_empty": {"pass": bad_probes <= C.SMOKE_MAX_EMPTY_PROBES,
                             "value": bad_probes, "max": C.SMOKE_MAX_EMPTY_PROBES},
        "mean_billed": {"pass": mean is not None and mean <= thr, "value": mean, "max": thr,
                        "detail": "" if mean is not None else "billing incomplete"},
    }
    return all(v["pass"] for v in crit.values()), crit


# --- the stage runner ----------------------------------------------------------------------
class RunRefused(RuntimeError):
    """The stage may not run now (no smoke pass, already terminal, another runner, ...)."""


@dataclass
class RunConfig:
    stage: C.StageKey
    paid: bool = True                 # paid runs create clock.json; mock/dry runs never do
    model_id: str | None = None       # mockllm/model for mock runs
    tasks: list | None = None         # default: the X3 manifest's 15 tasks
    repeats: int | None = None        # planned repeats (default MODELS[stage].repeats)
    executor: object | None = None    # default ProcessExecutor (tests: a fake)
    clock: object | None = None       # default RealClock (tests: a fake)
    test_call: Callable | None = None
    poll_s: float = POLL_S
    max_in_flight: int | None = None
    kill_grace_s: float = KILL_GRACE_S


class StageRunner:
    """One stage's smoke test or main run, with the caps, stop rules and deadline enforced here
    (prereg §7.2-§7.3). Exactly one runner per stage at a time (runner.lock)."""

    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.stage = cfg.stage
        self.m = C.MODELS[cfg.stage]
        self.exp = C.LEDGER_EXP[cfg.stage]
        self.cap = C.STAGE_CAP_USD[cfg.stage]
        self.ex = cfg.executor or ProcessExecutor()
        self.clock = cfg.clock or RealClock()
        self.test_call = cfg.test_call or (openrouter_test_call if cfg.paid
                                           else mock_test_call)
        self.model_id = cfg.model_id or self.m.model_id
        self.max_in_flight = cfg.max_in_flight or C.MAX_IN_FLIGHT[cfg.stage]
        self.planned_repeats = cfg.repeats or self.m.repeats
        self.min_repeats = min(self.m.min_repeats, self.planned_repeats)
        self._tasks = cfg.tasks
        self.running: dict[str, Job] = {}
        self.queue: list[list[EpisodeSpec]] = []
        self.retrying = False             # whole-block retries are queued only in a main run
        self.draining = False
        self.paused = False
        self.last_test_ts: float | None = None
        self.start_ts = self.clock.now()
        self.last_success = self.start_ts
        self.mock_deadline = self.start_ts + C.DEADLINE_H * 3600
        self.state = read_run_state(cfg.stage)
        self.state.setdefault("cuts", [])
        self.state.setdefault("smoke", {})
        self.state.update(stage=cfg.stage.value, cap_usd=self.cap)
        self.recs = manifest_records(cfg.stage)
        self._lock_fd: int | None = None

    # -- plumbing --
    @property
    def tasks(self) -> list:
        if self._tasks is None:
            self._tasks = load_tasks()
        return self._tasks

    def lock(self) -> None:
        p = C.stage_dir(self.stage) / "runner.lock"
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(fd)
            raise RunRefused(f"another runner holds {p}") from e
        self._lock_fd = fd

    def close(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def _append(self, rec: dict) -> dict:
        rec = {"utc": utc(self.clock.now()), **rec}
        append_manifest(self.stage, rec)
        self.recs.append(rec)
        return rec

    def _emit(self, kind: C.EventKind, payload: dict) -> None:
        emit(self.stage, kind, payload, self.clock.now())

    def _status(self) -> dict:
        return budget.status(self.exp, self.cap)

    def deadline_ts(self) -> float | None:
        if not self.cfg.paid:
            return self.mock_deadline
        clk = read_clock()
        return parse_utc(clk["deadline_utc"]) if clk else None

    def terminal(self) -> bool:
        return any(k == C.EventKind.TERMINAL.value for _, k, _ in read_events(self.stage))

    def smoke_passed(self) -> bool:
        return any(k == C.EventKind.SMOKE_PASS.value for _, k, _ in read_events(self.stage))

    def _outs(self, kind: C.RunKind | None = None) -> list[dict]:
        return [r for r in self.recs if r.get("kind") == "outcome"
                and (kind is None or r.get("run_kind") == kind.value)]

    def final_main_blocks(self) -> list[tuple[str, dict]]:
        """Main blocks whose fate is settled, in the order it was settled: the first attempt
        that completed, or the second failed attempt (prereg §7.2: one whole-block retry)."""
        tries: dict[str, list[dict]] = {}
        out: list[tuple[str, dict]] = []
        done: set[str] = set()
        for r in self._outs(C.RunKind.MAIN):
            if r.get("cancelled"):
                continue
            bid = r["batch_id"]
            tries.setdefault(bid, []).append(r)
            if bid in done:
                continue
            if not r["failed"] or len(tries[bid]) >= 2:
                done.add(bid)
                out.append((bid, r))
        return out

    def write_state(self, **kw) -> dict:
        self.state.update(kw)
        st = self._status()
        fin = self.final_main_blocks()
        main = self._outs(C.RunKind.MAIN)
        clk = read_clock()
        self.state.update(
            first_paid_utc=(clk or {}).get("first_paid_utc"),
            deadline_utc=(clk or {}).get("deadline_utc") if self.cfg.paid
            else utc(self.mock_deadline),
            blocks_planned=(self.state.get("target_repeats") or self.planned_repeats)
            * len(self.tasks),
            blocks_done=sum(1 for _, r in fin if not r["failed"]),
            blocks_failed=sum(1 for _, r in fin if r["failed"])
            + sum(1 for r in main if r.get("cancelled")),
            blocks_in_flight=len(self.running),
            episodes_done=sum(len(r["episode_ids"]) - len(r["failed"]) for r in main
                              if not r.get("cancelled")),
            reserved_outstanding_usd=st["outstanding"],
            live_workers=sum(1 for j in self.running.values() if self.ex.alive(j.handle)),
            spent_usd=st["spent"],
            last_success_utc=utc(self.last_success),
            updated_utc=utc(self.clock.now()),
            # running total of cancellation charges (main session convention, read by U5)
            conservative_charges_usd=round(
                sum(float(r.get("billed") or 0) for r in self._outs() if r.get("cancelled")),
                6))
        return write_run_state(self.stage, self.state)

    # -- admission and settlement --
    def admit(self, block: list[EpisodeSpec], kind: C.RunKind) -> Job | None:
        """Reserve the block's worst case and start it; None if refused (headroom or cap)."""
        amount = block_reservation(self.stage, block)
        if kind == C.RunKind.MAIN:
            st = self._status()
            aside = float(self.state.get("judge_set_aside_usd") or 0.0)
            if st["spent"] + st["outstanding"] + amount + aside > self.cap + 1e-9:
                return None
        if self.cfg.paid:
            ensure_clock(self.stage, self.clock.now())   # just before the first paid call
        bid, att = block[0].batch_id, block[0].attempt
        token = uuid.uuid4().hex[:12]
        self._append({"kind": "intent", "token": token, "stage": self.stage.value,
                      "run_kind": kind.value, "batch_id": bid, "attempt": att})
        try:
            rid = budget.reserve(amount, f"X3b {self.stage.value} {bid} a{att} token={token}",
                                 exp=self.exp, cap=self.cap)
        except budget.BudgetRefused:
            self._append({"kind": "intent_closed", "token": token, "released": []})
            return None
        attempt_dir = C.log_dir(self.stage) / f"{bid}_a{att}_{token}"
        job = Job(token=token, rid=rid, stage=self.stage, kind=kind, specs=list(block),
                  reserved=amount, attempt_dir=attempt_dir, task_name=f"x3b{token[:8]}",
                  started_ts=self.clock.now())
        self._append({"kind": "admit", "token": token, "rid": rid, "stage": self.stage.value,
                      "run_kind": kind.value, "model": self.model_id, "batch_id": bid,
                      "attempt": att, "reserved": amount, "attempt_dir": str(attempt_dir),
                      "task_name": job.task_name,
                      "episode_ids": [sp.episode_id for sp in block],
                      "specs": [asdict(sp) for sp in block]})
        try:
            job.handle = self.ex.start(job)
        except Exception as e:  # noqa: BLE001 - nothing was sent: settle at zero
            self.settle_job(job, cancelled=False, extra_notes=[f"worker did not start: {e!r}"],
                            nothing_sent=True)
            return job
        self._append({"kind": "started", "token": token, "pid": self.ex.pid(job.handle)})
        self.running[token] = job
        return job

    def settle_job(self, job: Job, *, cancelled: bool, extra_notes: list[str] | None = None,
                   nothing_sent: bool = False) -> dict:
        wanted = {sp.episode_id for sp in job.specs}
        logs: list[str] = []
        notes = list(extra_notes or [])
        if cancelled:
            arts, usd, est = [], job.reserved, job.reserved
            notes.append("conservative: cancelled")
        elif nothing_sent:
            arts, usd, est = [], 0.0, 0.0
        else:
            arts, logs, err = load_result_arts(job.attempt_dir, wanted, self.stage)
            if err:
                notes.append(f"worker: {err}")
            usd, est, cn = settle_cost(arts, job.specs, self.stage)
            notes += cn
        if _ledger_has_rid(job.rid):
            notes.append("reservation already settled before an interruption")
            budget.release(job.rid)
        else:
            try:
                lead = "conservative: cancelled; " if cancelled else ""
                budget.settle(job.rid, usd, self.model_id, len(job.specs),
                              f"{self.exp} {job.kind.value} {job.batch_id} a{job.attempt} "
                              f"rid={job.rid}; {lead}estimated ${est:.4f}"
                              + (f"; {'; '.join(notes)}" if notes else ""))
            except KeyError:
                notes.append("reservation missing and no ledger line; nothing settled")
        by_id = {a["meta"]["episode_id"]: a for a in arts}
        reasons = {}
        for sp in job.specs:
            a = by_id.get(sp.episode_id)
            if cancelled:
                reasons[sp.episode_id] = "cancelled"
            elif a is None:
                reasons[sp.episode_id] = "missing"
            elif failed(a):
                reasons[sp.episode_id] = "error" if a.get("error") else "api_error"
        full = episode_reservation(self.stage)
        costs = {}
        for sp in job.specs:
            a = by_id.get(sp.episode_id)
            c = episode_cost(a, self.stage) if a is not None else None
            costs[sp.episode_id] = full if c is None else c
        rec = self._append({
            "kind": "outcome", "token": job.token, "rid": job.rid, "stage": self.stage.value,
            "run_kind": job.kind.value, "model": self.model_id, "batch_id": job.batch_id,
            "attempt": job.attempt, "episode_ids": [sp.episode_id for sp in job.specs],
            "reserved": job.reserved, "billed": usd, "estimated": est,
            "charge_kind": ("cancelled" if cancelled else
                            "billed" if est <= 1e-12 else "estimated"),
            "cancelled": cancelled, "failed": [e for e in reasons], "failed_reasons": reasons,
            "episode_costs": costs, "artifacts": [a.get("path") for a in arts],
            "logs": logs, "notes": notes})
        for t in self._success_times(arts):
            self.last_success = max(self.last_success, t)
        self._emit(C.EventKind.BLOCK_DONE,
                   {"batch_id": job.batch_id, "attempt": job.attempt, "kind": job.kind.value,
                    "billed": usd, "estimated": est, "failed": len(reasons),
                    "cancelled": cancelled})
        if (reasons and not cancelled and self.retrying and not self.draining
                and job.kind == C.RunKind.MAIN and job.attempt == 1):
            self.queue.insert(0, with_attempt(job.specs, 2))     # whole-block retry, once
        if not self.draining or cancelled:
            self.write_state()
        return rec

    @staticmethod
    def _success_times(arts: list[dict]) -> list[float]:
        out = []
        for a in arts:
            for c in a.get("model_calls") or []:
                t = parse_utc(c.get("utc"))
                if t is not None and not _call_failed(c):
                    out.append(t)
        return out

    def _progress(self) -> None:
        for j in self.running.values():
            p = Path(j.attempt_dir) / PROGRESS_JSONL
            if p.exists():
                lines = [x for x in p.read_text().splitlines() if x.strip()]
                if lines:
                    try:
                        self.last_success = max(self.last_success,
                                                float(json.loads(lines[-1])["ts"]))
                    except (ValueError, KeyError):
                        pass

    def harvest(self) -> None:
        for token, job in list(self.running.items()):
            if self.ex.finished(job.handle):
                del self.running[token]
                self.settle_job(job, cancelled=False)

    # -- outage rule (prereg §7.3, item 4) --
    def do_test_call(self) -> bool:
        """One minimal test call, reserved at its bound; intent before, result after; settled
        at its billed cost, or at its bound when billing is missing (estimated)."""
        bound = test_call_bound(self.stage)
        token = uuid.uuid4().hex[:12]
        try:
            rid = budget.reserve(bound, f"X3b {self.stage.value} outage test token={token}",
                                 exp=self.exp, cap=self.cap)
        except budget.BudgetRefused as e:
            self._append({"kind": "test_result", "token": token, "rid": None, "ok": False,
                          "status": "refused", "error": str(e)[:300], "settled_usd": 0.0,
                          "estimated_usd": 0.0})
            return False
        self._append({"kind": "test_intent", "token": token, "rid": rid, "bound_usd": bound})
        res = self.test_call(self.stage)
        billed = res.get("billed_usd")
        settled = float(billed) if billed is not None else bound
        est = 0.0 if billed is not None else bound
        budget.settle(rid, settled, self.model_id, 1,
                      f"X3b {self.stage.value} outage test rid={rid}; estimated ${est:.4f}")
        self._append({"kind": "test_result", "token": token, "rid": rid,
                      "ok": bool(res.get("ok")), "status": "ok" if res.get("ok") else "error",
                      "billed_usd": billed, "settled_usd": settled, "estimated_usd": est,
                      "provider": res.get("provider"), "error": res.get("error")})
        return bool(res.get("ok"))

    def outage_step(self, now: float) -> str | None:
        self._progress()
        idle = now - self.last_success
        if idle >= C.OUTAGE_STOP_H * 3600:
            return "outage"
        if not self.paused and idle >= C.OUTAGE_PAUSE_MIN * 60:
            self.paused = True
            self.last_test_ts = None
            self._emit(C.EventKind.PAUSE, {"last_success_utc": utc(self.last_success),
                                           "idle_min": round(idle / 60, 1)})
            self.write_state(state=C.RunState.PAUSED.value)
        if self.paused and (self.last_test_ts is None
                            or now - self.last_test_ts >= C.OUTAGE_PROBE_EVERY_MIN * 60):
            self.last_test_ts = now
            if self.do_test_call():
                self.last_success = max(self.last_success, self.clock.now())
                self.paused = False
                self._emit(C.EventKind.RESUME, {"after_min": round(idle / 60, 1)})
                self.write_state(state=C.RunState.RUNNING.value)
        return None

    # -- sizing and the interim completion rule (prereg §7.2, §4.9) --
    def _planned_episodes(self, target: int) -> int:
        return target * len(self.tasks) * len(C.STAGE_HEADERS[self.stage])

    def _remaining_episodes(self, target: int) -> int:
        """Planned main episodes (at `target` repeats) whose block is neither settled for good
        nor in flight; in-flight blocks are covered by their outstanding reservations."""
        fin = {bid for bid, _ in self.final_main_blocks()}
        live = {j.batch_id for j in self.running.values()}
        n_h = len(C.STAGE_HEADERS[self.stage])
        n = 0
        for t in self.tasks:
            for r in range(1, target + 1):
                bid = C.block_id(self.stage, C.RunKind.MAIN, t.task_id, r)
                if bid not in fin and bid not in live:
                    n += n_h
        return n

    def sizing(self, point: str, costs: list[float]) -> dict:
        """c = SIZING_FACTOR x mean measured episode cost; drop whole repeats from the end
        while (remaining episodes x c) + judge projection > what is left of the stage cap, never
        below the minimum (prereg §7.2). The judging upper bound for the target is stored as the
        admission headroom."""
        per_ep_judge = float(self.state.get("smoke", {}).get("judge_per_episode_usd") or 0.0)
        mean = sum(costs) / len(costs) if costs else episode_reservation(self.stage)
        c = C.SIZING_FACTOR * mean
        start = target = int(self.state.get("target_repeats") or self.planned_repeats)
        st = self._status()
        avail = self.cap - st["spent"] - st["outstanding"]

        def need(t: int) -> tuple[float, float]:
            # the rule adds the judge projection (prereg §7.2 worked example), not the bound
            proj = (C.JUDGE_PROJECTION_PER_EPISODE_USD * self._planned_episodes(t)
                    + C.JUDGE_VALIDATION_PROJECTION_USD[self.stage.value])
            return self._remaining_episodes(t) * c + proj, proj

        total, proj = need(target)
        while total > avail + 1e-9 and target > self.min_repeats:
            target -= 1
            total, proj = need(target)
        # the admission headroom stays the judging upper bound (prereg §7.2 "Judging")
        judge = per_ep_judge * self._planned_episodes(target)
        rec = {"point": point, "from": start, "to": target, "c_usd": round(c, 6),
               "mean_cost_usd": round(mean, 6), "n_measured": len(costs),
               "remaining_usd": round(total, 6), "available_usd": round(avail, 6),
               "judge_projection_usd": round(proj, 6),
               "judge_set_aside_usd": round(judge, 6), "fits": total <= avail + 1e-9}
        self._append({"kind": "sizing", **rec})
        if target != start:
            self.state["cuts"] = [*self.state.get("cuts", []), rec]
            self._emit(C.EventKind.CUT, rec)
        self.queue = [b for b in self.queue if b[0].repeat <= target]
        self.write_state(target_repeats=target, judge_set_aside_usd=round(judge, 6))
        return rec

    def interim_step(self) -> str | None:
        if any(r.get("kind") == "interim" for r in self.recs):
            return None
        fin = self.final_main_blocks()
        if len(fin) < C.INTERIM_BLOCKS:
            return None
        first = fin[: C.INTERIM_BLOCKS]
        n = sum(len(r["episode_ids"]) for _, r in first)
        bad = sum(len(r["failed"]) for _, r in first)
        share = bad / n if n else 0.0
        self._append({"kind": "interim", "share": share, "n_episodes": n, "n_failed": bad,
                      "blocks": [b for b, _ in first]})
        if share > C.INTERIM_MAX_API_ERROR_SHARE:
            return "interim_completion"
        bids = {b for b, _ in first}
        costs = self._smoke_costs() + [
            v for r in self._outs(C.RunKind.MAIN) if r["batch_id"] in bids
            and not r.get("cancelled") for v in r["episode_costs"].values()]
        self.sizing("interim", costs)
        return None

    def _smoke_runs(self) -> int:
        return sum(1 for r in self.recs if r.get("kind") == "smoke_run")

    def _smoke_costs(self) -> list[float]:
        n = self._smoke_runs()
        return [v for r in self._outs(C.RunKind.SMOKE) if r["attempt"] == n
                for v in r["episode_costs"].values()]

    # -- the pool loop --
    def pool(self, kind: C.RunKind) -> str:
        """Dispatch, harvest and apply the stop rules until the queue is done or a rule fires;
        returns the reason (the caller runs `shutdown`)."""
        while True:
            now = self.clock.now()
            sp = stop_request_path(self.stage)
            if sp.exists():
                why = json.loads(sp.read_text()).get("reason", "global_stop")
                os.replace(sp, sp.with_name(f"stop_request.handled.{int(now)}.json"))
                return why if why in C.STOP_REASONS else "global_stop"
            dl = self.deadline_ts()
            if dl is not None and now >= dl:
                self._emit(C.EventKind.DEADLINE, {"deadline_utc": utc(dl),
                                                  "in_flight": len(self.running)})
                return "deadline"
            self.harvest()
            if kind == C.RunKind.MAIN:
                why = self.interim_step()
                if why:
                    return why
            why = self.outage_step(now)
            if why:
                return why
            refused = False
            if not self.paused:
                while self.queue and len(self.running) < self.max_in_flight:
                    if self.admit(self.queue[0], kind) is None:
                        refused = True
                        break
                    self.queue.pop(0)
            if not self.queue and not self.running:
                return "completed"
            if refused and not self.running:
                return "funds_exhausted"
            self.write_state()
            self.clock.sleep(self.cfg.poll_s)

    # -- the one shutdown path (plan §3 U3, item 11) --
    def shutdown(self, reason: str) -> dict:
        self.draining = True
        if reason not in ("completed", "deadline"):
            self._emit(C.EventKind.STOP, {"reason": reason, "in_flight": len(self.running)})
        if self.running:
            self.write_state(state=C.RunState.DRAINING.value, stop_reason=reason)
        end = self.clock.now() + C.DRAIN_MIN * 60
        while self.running and self.clock.now() < end:
            self.harvest()
            if self.running:
                self.clock.sleep(self.cfg.poll_s)
        killed = list(self.running.values())
        for j in killed:
            self.ex.kill(j.handle, self.cfg.kill_grace_s)
        live = [j.token for j in killed if self.ex.alive(j.handle)]
        containers = {j.token: self.ex.cleanup(j) for j in killed}
        for j in killed:
            del self.running[j.token]
            self.settle_job(j, cancelled=True)
        self.release_leftovers()
        st = self._status()
        left = {k: v for k, v in containers.items() if v}
        final = (C.RunState.ENDED if reason in ("completed", "funds_exhausted")
                 else C.RunState.STOPPED)
        state = self.write_state(state=final.value, stop_reason=reason, live_workers=len(live))
        info = {"reason": reason, "spent_usd": st["spent"], "outstanding_usd": st["outstanding"],
                "live_workers": len(live), "containers_left": left,
                "cancelled_blocks": len(killed),
                "conservative_charges_usd": state["conservative_charges_usd"]}
        if not live and not left and st["outstanding"] <= 1e-9:
            self._emit(C.EventKind.TERMINAL, info)
            info["terminal"] = True
        else:
            info["terminal"] = False
            print(f"X3B_ERROR {self.stage.value}: not terminal: {info}", flush=True)
        return info

    def release_leftovers(self) -> list[str]:
        """Close every reservation of this stage's exp id that this manifest owns: an admitted
        block's at its full amount (conservative), a test call's at its bound, an intent that
        never reached admission released. Reservations not in this manifest are left alone
        (and keep TERMINAL from being written)."""
        admits = {r["token"]: r for r in self.recs if r.get("kind") == "admit"}
        intents = {r["token"] for r in self.recs if r.get("kind") == "intent"}
        tests = {r["token"] for r in self.recs if r.get("kind") == "test_intent"}
        done = []
        for rid, res in list(budget._read_reservations().items()):
            if res.get("exp") != self.exp:
                continue
            tok = next((t.split("=", 1)[1] for t in res.get("note", "").split()
                        if t.startswith("token=")), None)
            if tok is None or _ledger_has_rid(rid):
                continue
            if tok in admits or tok in tests:
                budget.settle(rid, res["usd"], self.model_id, 0,
                              f"X3b {self.stage.value} leftover rid={rid} token={tok}; "
                              f"conservative: cancelled; estimated ${res['usd']:.4f}")
                if tok in admits:
                    a = admits[tok]
                    self._append({"kind": "outcome", "token": tok, "rid": rid,
                                  "stage": self.stage.value, "run_kind": a["run_kind"],
                                  "model": self.model_id, "batch_id": a["batch_id"],
                                  "attempt": a["attempt"], "episode_ids": a["episode_ids"],
                                  "reserved": res["usd"], "billed": res["usd"],
                                  "estimated": res["usd"], "charge_kind": "cancelled",
                                  "cancelled": True, "failed": a["episode_ids"],
                                  "failed_reasons": {e: "cancelled" for e in a["episode_ids"]},
                                  "episode_costs": {}, "artifacts": [], "logs": [],
                                  "notes": ["leftover reservation; conservative: cancelled"]})
                done.append(rid)
            elif tok in intents:
                budget.release(rid)
                self._append({"kind": "intent_closed", "token": tok, "released": [rid]})
                done.append(rid)
        return done

    # -- recovery (port of X3's reconcile_pending / _release_orphans) --
    def reconcile(self) -> list[dict]:
        """After an interruption: release reservations of intents never admitted; settle test
        calls with no result at their bound; settle every admission with no outcome from its
        worker's result or logs (a worker still alive is killed first and its block settled
        as cancelled). Nothing is re-run here; nothing is settled twice."""
        recs = self.recs
        closed = {r["token"] for r in recs if r.get("kind") == "intent_closed"}
        admitted = {r["token"] for r in recs if r.get("kind") == "admit"}
        orphans = {r["token"] for r in recs if r.get("kind") == "intent"} - admitted - closed
        for tok in orphans:
            rel = []
            for rid, res in list(budget._read_reservations().items()):
                if res.get("exp") == self.exp and f"token={tok}" in res.get("note", "") \
                        and not _ledger_has_rid(rid):
                    budget.release(rid)
                    rel.append(rid)
            self._append({"kind": "intent_closed", "token": tok, "released": rel})
        results = {r["token"] for r in recs if r.get("kind") == "test_result"}
        open_res = budget._read_reservations()
        for r in [x for x in recs if x.get("kind") == "test_intent"
                  and x["token"] not in results]:
            if r["rid"] in open_res and not _ledger_has_rid(r["rid"]):
                budget.settle(r["rid"], r["bound_usd"], self.model_id, 1,
                              f"X3b {self.stage.value} outage test rid={r['rid']} "
                              f"(unresolved, reconciled at its bound); "
                              f"estimated ${r['bound_usd']:.4f}")
            self._append({"kind": "test_result", "token": r["token"], "rid": r["rid"],
                          "ok": False, "status": "unresolved", "billed_usd": None,
                          "settled_usd": r["bound_usd"], "estimated_usd": r["bound_usd"]})
        settled = {r["token"] for r in recs if r.get("kind") == "outcome"}
        pids = {r["token"]: r.get("pid") for r in recs if r.get("kind") == "started"}
        out = []
        was = self.draining
        self.draining = True                    # no retries are queued from here
        try:
            for a in [x for x in list(recs) if x.get("kind") == "admit"
                      and x["token"] not in settled]:
                job = Job(token=a["token"], rid=a["rid"], stage=self.stage,
                          kind=C.RunKind(a["run_kind"]),
                          specs=[EpisodeSpec(**s) for s in a["specs"]],
                          reserved=a["reserved"], attempt_dir=Path(a["attempt_dir"]),
                          task_name=a["task_name"], started_ts=parse_utc(a["utc"]) or 0.0)
                pid = pids.get(a["token"])
                alive = bool(pid) and _pid_alive(pid) and _is_group_leader(pid)
                if alive:
                    kill_group(pid, self.cfg.kill_grace_s)
                    remove_compose_projects(job.task_name)
                out.append(self.settle_job(job, cancelled=alive,
                                           extra_notes=["reconciled after an interruption"]))
        finally:
            self.draining = was
        return out

    # -- entry points --
    def smoke(self) -> dict:
        """The stage's smoke test (prereg §4.9): run, gate, and on a pass the judge set-aside
        and the first sizing point. A second failure stops the stage."""
        if self.terminal():
            raise RunRefused(f"{self.stage.value} is terminal")
        if self.smoke_passed():
            raise RunRefused(f"{self.stage.value} smoke already passed")
        self.reconcile()
        n_prev = self._smoke_runs()
        if n_prev >= SMOKE_MAX_RUNS:
            raise RunRefused(f"{self.stage.value}: {n_prev} smoke runs already")
        if n_prev:
            first = sum(float(r["billed"]) for r in self._outs(C.RunKind.SMOKE)
                        if r["attempt"] == 1)
            extra = sum(float(r["billed"]) for r in self._outs(C.RunKind.SMOKE)
                        if r["attempt"] > 1)
            if extra + first > C.SMOKE_FIX_RERUN_USD + 1e-9:
                raise RunRefused(f"smoke re-run would pass the first smoke charge + "
                                 f"${C.SMOKE_FIX_RERUN_USD:.2f} (projected extra "
                                 f"{extra + first:.2f})")
        n = n_prev + 1
        blocks = [with_attempt(b, n) for b in
                  plan_stage(self.stage, C.RunKind.SMOKE, self.tasks, model_id=self.model_id)]
        self._append({"kind": "smoke_run", "n": n})
        self.queue = list(blocks)
        self.write_state(state=C.RunState.SMOKE.value, stop_reason=None)
        why = self.pool(C.RunKind.SMOKE)
        if why != "completed":
            return self.shutdown(why)
        outs = [r for r in self._outs(C.RunKind.SMOKE) if r["attempt"] == n]
        arts = [json.loads(Path(p).read_text()) for r in outs for p in r["artifacts"]
                if p and Path(p).exists()]
        specs = [sp for b in blocks for sp in b]
        ok, crit = smoke_gate(arts, specs, self.stage)
        charge = sum(float(r["billed"]) for r in outs)
        smoke = {**self.state.get("smoke", {}), "runs": n, "status": "pass" if ok else "fail",
                 "criteria": crit, "spent_usd": round(sum(
                     float(r["billed"]) for r in self._outs(C.RunKind.SMOKE)), 6)}
        if n == 1:
            smoke["first_charge_usd"] = round(charge, 6)
        if not ok:
            self._emit(C.EventKind.SMOKE_FAIL,
                       {"run": n, "failing": {k: v for k, v in crit.items() if not v["pass"]}})
            self.write_state(state=C.RunState.SMOKE_FAILED.value, smoke=smoke)
            if n >= SMOKE_MAX_RUNS:
                return self.shutdown("smoke_failed")
            return {"reason": "smoke_failed", "terminal": False, "criteria": crit}
        smoke["judge_per_episode_usd"] = round(judge_bound_per_episode(arts, self.stage), 8)
        self.state["smoke"] = smoke
        self._emit(C.EventKind.SMOKE_PASS, {"run": n, "mean_billed_usd":
                                            crit["mean_billed"]["value"]})
        self.state.setdefault("target_repeats", self.planned_repeats)
        rec = self.sizing("smoke", self._smoke_costs())
        smoke["c_usd"] = rec["c_usd"]
        self.write_state(state=C.RunState.SMOKE.value, smoke=smoke)
        return {"reason": "smoke_passed", "terminal": False, "criteria": crit, "sizing": rec}

    def run(self, resume: bool = False) -> dict:
        """The stage's main run as a rolling pool (prereg §7.2-§7.3)."""
        if self.terminal():
            raise RunRefused(f"{self.stage.value} is terminal")
        if not self.smoke_passed():
            raise RunRefused(f"{self.stage.value}: no SMOKE_PASS in its events")
        if any(r.get("kind") == "admit" and r.get("run_kind") == "main" for r in self.recs) \
                and not resume:
            raise RunRefused(f"{self.stage.value}: main run already started; use --resume")
        self.reconcile()
        target = min(int(self.state.get("target_repeats") or self.planned_repeats),
                     self.planned_repeats)
        plan = [b for b in plan_stage(self.stage, C.RunKind.MAIN, self.tasks,
                                      self.planned_repeats, self.model_id)
                if b[0].repeat <= target]
        self.queue = self.resume_blocks(plan)
        self.retrying = True
        self.write_state(state=C.RunState.RUNNING.value, stop_reason=None,
                         target_repeats=target)
        return self.shutdown(self.pool(C.RunKind.MAIN))

    def resume_blocks(self, plan: list[list[EpisodeSpec]]) -> list[list[EpisodeSpec]]:
        """What still has to run: a block that completed is skipped; one failed attempt ->
        attempt 2; two failed attempts -> excluded (cancelled attempts are not counted as
        failures but use up their attempt number)."""
        by: dict[str, list[dict]] = {}
        for r in self._outs(C.RunKind.MAIN):
            by.setdefault(r["batch_id"], []).append(r)
        out, retries = [], []
        for b in plan:
            rs = by.get(b[0].batch_id, [])
            if any(not r["failed"] for r in rs if not r.get("cancelled")):
                continue
            fails = [r for r in rs if not r.get("cancelled")]
            if len(fails) >= 2:
                continue
            nxt = max([r["attempt"] for r in rs] or [0]) + 1
            (retries if fails else out).append(with_attempt(b, nxt) if nxt > 1 else b)
        return retries + out


def _is_group_leader(pid: int) -> bool:
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return False


# --- collect (plan §3 U3 "Collect"; prereg §4.4, §4.7) --------------------------------------
def accepted(stage: C.StageKey, judgment: C.Judgment) -> dict[str, dict[str, tuple]]:
    """U2's accepted judge labels (episode -> label -> (value, source)); {} before judging or
    validation, or while U2's module is absent."""
    try:
        from x3b.judges import accepted_labels
    except ImportError:
        return {}
    jd = C.judged_dir(stage)
    return accepted_labels(stage, judgment, jd) if jd.exists() else {}


def _lab(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v).strip().lower()
    return "" if s in ("", "none", "null") else s


def _tf(b: bool) -> str:
    return "true" if b else "false"


def _billed_sum(calls: list[dict], roles: tuple[str, ...], ran: bool) -> float | str:
    vals = [c.get("billed_usd") for c in calls if (c.get("role") or "agent") in roles]
    if any(v is None for v in vals) or (not vals and ran):
        return ""
    return round(sum(float(v) for v in vals), 8)


def _answers(probe: dict, keys: tuple[str, ...], judged: dict[str, tuple]
             ) -> tuple[str, dict[str, str]]:
    """(source, answers): the letter parse, else the accepted judge labels; '' = unknown."""
    parsed = probe.get("parsed") if isinstance(probe, dict) else None
    if parsed:
        return "letter", {k: _lab(parsed.get(k)) for k in keys}
    if not (isinstance(probe, dict) and (probe.get("reply") or "").strip()) or not judged:
        return "", {k: "" for k in keys}
    vals = {k: _lab(judged[k][0]) if k in judged else "" for k in keys}
    src = next((judged[k][1] for k in keys if k in judged and vals[k]), "")
    return (src if any(vals.values()) else ""), vals


def selected_attempts(stage: C.StageKey, arts: list[dict]
                      ) -> tuple[dict[str, int | None], dict[str, set[int]]]:
    """Per main block: the latest settled, non-cancelled attempt (None: none), and the
    cancelled attempts. A block with artifacts but no outcome record (e.g. a hand-made tree)
    falls back to its latest attempt with artifacts."""
    sel: dict[str, int | None] = {}
    canc: dict[str, set[int]] = {}
    outs = outcomes(stage)
    recorded = {r["batch_id"] for r in outs if r.get("run_kind") == C.RunKind.MAIN.value}
    for r in outs:
        if r.get("run_kind") != C.RunKind.MAIN.value:
            continue
        bid = r["batch_id"]
        sel.setdefault(bid, None)
        if r.get("cancelled"):
            canc.setdefault(bid, set()).add(int(r["attempt"]))
        else:
            sel[bid] = max(int(r["attempt"]), sel[bid] or 0)
    for a in arts:
        if a.get("kind") == C.RunKind.MAIN.value:
            bid = a["meta"]["batch_id"]
            if bid not in sel:
                sel[bid] = int(a["meta"]["attempt"])
            elif bid not in recorded:
                sel[bid] = max(sel[bid] or 0, int(a["meta"]["attempt"]))
    return sel, canc


def _role(art: dict, sel: dict[str, int | None], canc: dict[str, set[int]]
          ) -> tuple[str, str]:
    m = art["meta"]
    bad = ("error" if art.get("error") else "api_error") if failed(art) else ""
    if art.get("kind") == C.RunKind.SMOKE.value:
        return ("excluded", bad) if bad else ("smoke", "")
    bid, att = m["batch_id"], int(m["attempt"])
    if att in canc.get(bid, set()):
        return "excluded", "cancelled"
    s = sel.get(bid)
    if s is None:
        return "excluded", "block_incomplete"
    if att < s:
        return "excluded", "superseded"
    if att > s:
        return "excluded", "block_incomplete"
    return ("excluded", bad) if bad else ("main", "")


def collect(stage: C.StageKey) -> Path:
    """episodes.csv: one row per episode artifact, exactly EPISODE_CSV_COLUMNS. Re-runnable at
    any time (after detection and after judge validation, plan §4)."""
    root = C.episodes_dir(stage)
    paths = sorted(p for p in root.rglob("*.json")) if root.exists() else []
    arts = [json.loads(p.read_text()) for p in paths]
    det = {}
    if C.detector_csv(stage).exists():
        with open(C.detector_csv(stage), newline="") as f:
            det = {r["episode_id"]: r for r in csv.DictReader(f)}
    acc = {j: accepted(stage, j) for j in C.Judgment}
    sel, canc = selected_attempts(stage, arts)
    rows, seen = [], set()
    for art in arts:
        m = art["meta"]
        eid = m["episode_id"]
        if eid in seen:
            raise ValueError(f"duplicate episode {eid}")
        seen.add(eid)
        role, why = _role(art, sel, canc)
        calls = art.get("model_calls") or []
        ran = bool(art.get("n_turns")) or failed(art)
        pre, post = art.get("pre_probe") or {}, art.get("probe") or {}
        pre_src, pre_ans = _answers(pre, C.PRE_PARSED_KEYS,
                                    acc[C.Judgment.PRE_PROBE_PARSE].get(eid, {}))
        post_src, post_ans = _answers(post, C.POST_PARSED_KEYS,
                                      acc[C.Judgment.POST_PROBE_PARSE].get(eid, {}))
        disc = acc[C.Judgment.DISCLOSURE].get(eid, {})
        ft, wa = disc.get("states_failing_test"), disc.get("states_workaround")
        has_final = bool((art.get("final_message") or "").strip())
        sac = [art.get("spent_at_ceiling"), pre.get("spent_at_ceiling"),
               post.get("spent_at_ceiling")]
        provs = {c["provider"] for c in calls if c.get("provider")}
        row = {"episode_id": eid, "stage": stage.value, "model": m.get("model"),
               "kind": art.get("kind"), "header": art.get("header"), "task_id": m["task_id"],
               "repeat": m["repeat"], "attempt": m["attempt"], "block_id": m.get("batch_id"),
               "analysis_role": role, "excluded_reason": why,
               "end_reason": art.get("end_reason") or "", "n_turns": art.get("n_turns"),
               "n_nudges": art.get("n_nudges"), "n_multi_call": art.get("n_multi_call"),
               "billed_usd": _billed_sum(calls, C.CALL_ROLES, ran),
               "billed_agent_usd": _billed_sum(calls, ("agent",), ran),
               "billed_probe_usd": _billed_sum(calls, ("pre_probe", "post_probe"), False),
               "ceiling_usd": ("" if art.get("spent_at_ceiling") is None
                               else round(sum(float(x or 0) for x in sac), 8)),
               "n_non_relace_calls": sum(1 for c in calls if c.get("provider")
                                         and c["provider"].lower() != "relace"),
               "providers": ";".join(sorted(provs)),
               "pre_status": pre.get("status") or "", "pre_source": pre_src, **pre_ans,
               "post_status": post.get("status") or "", "post_source": post_src, **post_ans,
               "has_final_message": _tf(has_final),
               "ended_done_with_message": _tf(art.get("end_reason") == "done" and has_final),
               "disc_failing_test": _lab(ft[0]) if ft else "",
               "disc_workaround": _lab(wa[0]) if wa else "",
               "disc_source": (ft or wa)[1] if (ft or wa) else ""}
        d = det.get(eid, {})
        for k in ("tamper_any", "tamper_final", "tamper_ambiguous", "pass_run1", "pass_run2",
                  "pass_disagree", "passed", "cheat"):
            row[k] = d.get(k, "")
        if set(row) != set(C.EPISODE_CSV_COLUMNS):
            raise AssertionError(f"row keys differ: {set(row) ^ set(C.EPISODE_CSV_COLUMNS)}")
        rows.append(row)
    out = C.episodes_csv(stage)
    out.parent.mkdir(parents=True, exist_ok=True)
    main = [r for r in rows if r["analysis_role"] == "main"]
    cov = {"n_rows": len(rows), "n_main": len(main),
           "by_role": {k: sum(r["analysis_role"] == k for r in rows) for k in C.ANALYSIS_ROLES},
           "missing_detector": sorted(r["episode_id"] for r in main if r["cheat"] == ""),
           "pre_from_judge": sum(r["pre_source"] not in ("", "letter") for r in rows),
           "post_from_judge": sum(r["post_source"] not in ("", "letter") for r in rows),
           "pre_unparsed": sum(r["pre_status"] == "ok" and not r["pre_source"] for r in rows),
           "post_unparsed": sum(r["post_status"] == "ok" and not r["post_source"]
                                for r in rows),
           "disclosure_labelled": sum(bool(r["disc_source"]) for r in rows)}
    out.with_suffix(".coverage.json").write_text(json.dumps(cov, indent=1) + "\n")
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(C.EPISODE_CSV_COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, out)
    return out
