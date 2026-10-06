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
GO_FILE = "GO"                       # the parent releases a registered worker (finding 1)
ABORT_FILE = "ABORT"                 # recovery tells an unregistered worker to exit
WORKER_PID = "worker.pid"
GO_WAIT_S = 300.0
READY_FILE = "READY"                 # the worker's ack after setsid: {"pid", "pgid"} (r2, #2)
READY_WAIT_S = 120.0
ORPHAN_GRACE_S = READY_WAIT_S + GO_WAIT_S + 120.0   # an unreleased worker is long gone by then
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


def _agent_failed_charges(art: dict, ceilings: tuple[float, float] | None) -> list[float]:
    """The recorded charge of each failed agent attempt, in order, from the loop's per-turn
    bounds. Each failed attempt was charged its single-attempt bound, so a turn's count is
    failed_charge / bound; with no known ceilings a turn counts as one attempt (a mismatch then
    makes the episode unavailable, which is charged in full)."""
    from x3.task import ATTEMPTS, call_bound

    out: list[float] = []
    for b in art.get("bounds") or []:
        fc = float(b.get("failed_charge") or 0.0)
        if fc <= 0:
            continue
        one = (call_bound(int(b.get("input_bound") or 0), int(b.get("max_tokens") or 0),
                          *ceilings) / ATTEMPTS) if ceilings else 0.0
        n = max(1, round(fc / one)) if one > 0 else 1
        out += [fc / n] * n
    return out


def _probe_failed_charges(probe: dict | None) -> list[float]:
    if not isinstance(probe, dict):
        return []
    att = [a for a in probe.get("attempts") or [] if a.get("kind") == "transport_error"]
    if att:
        return [float(a.get("charge") or 0.0) for a in att]
    fac = float(probe.get("failed_attempt_charge") or 0.0)
    return [fac] if fac > 0 else []


def episode_cost(art: dict, stage: C.StageKey | None = None) -> float | None:
    """Measured cost of one episode (plan §1): every call's `billed_usd`, plus, for each failed
    attempt with no billed cost, its recorded ceiling charge (agent `bounds[].failed_charge`,
    probes' transport-error `attempts[].charge`; X3 Amendment B3). A billed failed attempt
    counts at its billed cost only (code review r1, finding 9). The correspondence between
    failed call records and recorded failed attempts must be exact (code review r2, finding
    3): per role, as many failed call records as recorded failed attempts, paired in their
    common chronological order; any other count is unavailable. None when the accounting is
    incomplete (r1 finding 10, r2 finding 3): an unbilled successful call, a count mismatch,
    or no call records although requests ran.
    0.0 only when the artifact shows no request was sent."""
    calls = art.get("model_calls") or []
    if not calls:
        return 0.0 if _sent_nothing(art) else None
    if stage is None and art.get("stage_key") in {s.value for s in C.StageKey}:
        stage = C.StageKey(art["stage_key"])
    ceilings = (C.MODELS[stage].in_ceiling, C.MODELS[stage].out_ceiling) if stage else None
    if any(c.get("billed_usd") is None and not _call_failed(c) for c in calls):
        return None                        # a successful call with no billed cost
    total = sum(float(c["billed_usd"]) for c in calls if c.get("billed_usd") is not None)
    recorded = {"agent": _agent_failed_charges(art, ceilings),
                "pre_probe": _probe_failed_charges(art.get("pre_probe")),
                "post_probe": _probe_failed_charges(art.get("probe"))}
    for role, charges in recorded.items():
        fcalls = [c for c in calls if (c.get("role") or "agent") == role and _call_failed(c)]
        if len(fcalls) != len(charges):
            return None                    # correspondence not established: unavailable
        for c, charge in zip(fcalls, charges, strict=True):
            if c.get("billed_usd") is None:
                total += charge
    return total


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


def settle_detail(arts: list[dict], specs: list[EpisodeSpec], stage: C.StageKey
                  ) -> tuple[float, float, list[str], dict[str, float]]:
    """`settle_cost` plus the per-episode charges, so sizing uses exactly what settlement
    charged (code review r1, finding 10)."""
    by_id = {a["meta"]["episode_id"]: a for a in arts}
    full = episode_reservation(stage)
    usd = est = 0.0
    notes: list[str] = []
    per: dict[str, float] = {}
    for sp in specs:
        a = by_id.get(sp.episode_id)
        cost = episode_cost(a, stage) if a is not None else None
        if cost is None:
            charge, unmeasured = full, full
            why = ("no artifact" if a is None else
                   "no call records although requests ran" if not a.get("model_calls")
                   else "accounting incomplete (unbilled call not matched to a recorded "
                        "charge)")
            notes.append(f"{sp.episode_id}: {why}; charged {full:.3f}")
        else:
            billed = sum(float(c["billed_usd"]) for c in a.get("model_calls") or []
                         if c.get("billed_usd") is not None)
            charge, unmeasured = cost, max(0.0, cost - billed)
            if not a.get("model_calls"):
                notes.append(f"{sp.episode_id}: no request sent; charged 0")
            elif unmeasured > 1e-12:
                notes.append(f"{sp.episode_id}: billed {billed:.4f} + failed-attempt charges "
                             f"{unmeasured:.4f}")
        usd += charge
        est += unmeasured
        per[sp.episode_id] = round(charge, 8)
    return round(usd, 6), round(est, 6), notes, per


def settle_cost(arts: list[dict], specs: list[EpisodeSpec], stage: C.StageKey
                ) -> tuple[float, float, list[str]]:
    """What a block attempt is settled at (plan §3 U3 "Settlement"; every path): per episode,
    `episode_cost` when its accounting is complete, else its full reservation (episode limit +
    the stage's probe envelope). A missing artifact, or one with no call records although it
    ran turns, failed or recorded any attempt, is charged in full; zero only when the artifact
    shows no request was sent. Returns (usd, estimated_usd, notes): estimated_usd is the part
    not measured as billed."""
    usd, est, notes, _ = settle_detail(arts, specs, stage)
    return usd, est, notes


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
def read_clock(retries: int = 10, wait_s: float = 0.2) -> dict | None:
    """The clock, or None if it does not exist. The file is published whole (ensure_clock), so
    a parse error means a concurrent publish on an old layout or damage: retry briefly, then
    raise rather than guess a deadline."""
    p = C.CLOCK_JSON
    for i in range(retries):
        if not p.exists():
            return None
        try:
            rec = json.loads(p.read_text())
            if rec.get("deadline_utc") and rec.get("first_paid_utc"):
                return rec
        except (json.JSONDecodeError, OSError):
            pass
        if i < retries - 1:
            time.sleep(wait_s)
    raise RuntimeError(f"{p} is unreadable or incomplete; fix by hand (never rewritten)")


def ensure_clock(stage: C.StageKey, now_ts: float) -> dict:
    """Publish `clock.json` once, just before the first paid call of either stage (code review
    r1, finding 14): under a separate lock file, the record is written to a temp file, fsync'd
    and hard-linked into place only if absent, so readers never see a partial or empty clock.
    If it exists, it is returned unchanged (never rewritten)."""
    p = C.CLOCK_JSON
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_name("clock.lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            if p.exists():
                return read_clock()
            rec = {"first_paid_utc": utc(now_ts),
                   "deadline_utc": utc(now_ts + C.DEADLINE_H * 3600), "stage": stage.value}
            tmp = p.with_name(f"clock.{os.getpid()}.tmp")
            with open(tmp, "w") as f:
                f.write(json.dumps(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, p)                 # atomic, and fails if the clock exists
            except FileExistsError:
                return read_clock()
            finally:
                tmp.unlink(missing_ok=True)
            dfd = os.open(p.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            return rec
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


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


def _ledger_estimate(entry: dict) -> float:
    """The estimated part of a settlement, from its note's "estimated $X" tag; the whole
    amount when the tag is absent (conservative: nothing claimed as measured)."""
    import re

    m = re.search(r"estimated \$([0-9]+(?:\.[0-9]+)?)", entry.get("note") or "")
    return float(m.group(1)) if m else float(entry.get("usd") or 0.0)


def _ledger_has_rid(rid: str) -> bool:
    """True if a ledger line already settled reservation `rid` (read under the ledger lock by
    `s4world.budget.settled_entry`; code review r1, finding 2)."""
    return budget.settled_entry(rid) is not None


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


def _wait_for_go(d: Path, timeout_s: float | None = None, sleep=time.sleep) -> bool:
    """The worker's side of the registration handshake (code review r1, finding 1): no model
    call before the parent has durably recorded the worker (pid, pgid) and written GO. ABORT
    (written by recovery) or a timeout means exit without calling anything."""
    timeout_s = GO_WAIT_S if timeout_s is None else timeout_s
    t = 0.0
    while t <= timeout_s:
        if (d / ABORT_FILE).exists():
            return False
        if (d / GO_FILE).exists():
            return True
        sleep(0.1)
        t += 0.1
    return False


def _block_worker(job: dict) -> None:
    """Pool worker (a spawned process): its own process group, one block's eval, extraction,
    then `result.json`. Spend is reserved and settled by the parent, never here."""
    os.setsid()
    d = Path(job["attempt_dir"])
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    (d / WORKER_PID).write_text(f"{os.getpid()}\n")
    _write_json_atomic(d / READY_FILE, {"pid": os.getpid(), "pgid": os.getpgid(0)})
    if not _wait_for_go(d):
        # never released (the parent died before registering it, or recovery aborted it):
        # exit before any model call
        tmp = d / (RESULT_JSON + ".tmp")
        tmp.write_text(json.dumps({"ok": False, "artifacts": [], "logs": [],
                                   "error": "not released: no request sent"}))
        os.replace(tmp, d / RESULT_JSON)
        return
    os.environ["X3B_PROGRESS_FILE"] = str(d / PROGRESS_JSONL)
    _register_progress_hook()
    if (d / ABORT_FILE).exists():            # ABORT wins even over GO: never call
        _write_json_atomic(d / RESULT_JSON, {"ok": False, "artifacts": [], "logs": [],
                                             "error": "aborted before any call"})
        return
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


class WorkerNotReady(RuntimeError):
    """The spawned worker did not acknowledge READY in time (r2, finding 2)."""


def _write_json_atomic(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(obj))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except (json.JSONDecodeError, OSError):
        return None


def worker_state(pid: int | None) -> str:
    """"dead" only when positively confirmed (no such process, or a zombie); "ours" for a
    live process leading its own group and running the multiprocessing spawn entry point;
    "unresolved" for anything else (unknown pid, ps failure, not yet a group leader, another
    user's process): recovery then keeps the reservation (r2, finding 2)."""
    if not pid:
        return "unresolved"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except (PermissionError, OSError):
        return "unresolved"
    try:
        r = subprocess.run(["ps", "-o", "stat=,pgid=,command=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unresolved"
    line = r.stdout.strip()
    if not line:
        return "dead" if r.returncode in (0, 1) else "unresolved"
    parts = line.split(None, 2)
    if parts[0].startswith("Z"):
        return "dead"
    if len(parts) == 3 and parts[1] == str(pid) and "multiprocessing" in parts[2]:
        return "ours"
    return "unresolved"


def _touch_atomic(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(utc() + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


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

    def release(self, job: Job, timeout_s: float = READY_WAIT_S) -> None:
        """Let a registered worker start calling: wait for its READY ack (it has called setsid
        and leads its own group), then write GO atomically (r2, finding 2)."""
        d = Path(job.attempt_dir)
        t = 0.0
        while t <= timeout_s:
            ready = _read_json(d / READY_FILE)
            if ready and ready.get("pid") == job.handle.pid and ready.get("pgid") == ready["pid"]:
                _touch_atomic(d / GO_FILE)
                return
            if not job.handle.is_alive():
                break
            time.sleep(0.1)
            t += 0.1
        raise WorkerNotReady(f"{job.task_name}: no READY from pid {job.handle.pid}")

    def kill(self, h, grace_s: float = KILL_GRACE_S) -> bool:
        """True only if the worker's process group is confirmed dead."""
        ok = kill_group(h.pid, grace_s) if h.pid is not None else True
        h.join(5)
        return ok and not h.is_alive()

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
        self.unconfirmed: dict[str, dict] = {}   # token -> {"alive", "containers"} (finding 1)
        self.smoke_allowance: float | None = None   # smoke re-run allowance (finding 4)
        self.allowance_refused = False
        self.test_refused_allowance = False
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
        self.running[token] = job       # tracked from here on, so shutdown always sees it
        try:
            pid = self.ex.pid(job.handle)
            # durable registration before the worker may call anything (r1, finding 1)
            self._append({"kind": "started", "token": token, "pid": pid, "pgid": pid,
                          "task_name": job.task_name})
            release = getattr(self.ex, "release", None)
            if release is not None:
                release(job)
        except WorkerNotReady as e:
            # never released: it can never call. Kill it; settle at zero only once its death
            # and cleanup are confirmed, else it stays unresolved (r2, finding 2)
            _touch_atomic(Path(attempt_dir) / ABORT_FILE)
            if self._terminate_one(job, []):
                self.settle_job(job, cancelled=False, nothing_sent=True,
                                extra_notes=[f"worker not ready, never released: {e}"])
            return job
        except BaseException:
            _touch_atomic(Path(attempt_dir) / ABORT_FILE)    # it must never start calling
            raise
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
            usd, est, cn, per = settle_detail(arts, job.specs, self.stage)
            notes += cn
        prior = budget.settled_entry(job.rid)
        if prior is not None:
            # the ledger is the record: the outcome carries its amounts (r3, finding 1)
            notes.append("reservation already settled before an interruption; ledger "
                         "amounts used")
            usd, est = float(prior["usd"]), _ledger_estimate(prior)
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
        if cancelled or nothing_sent:   # the same per-episode amounts settlement charged
            per = {sp.episode_id: (job.reserved / len(job.specs) if cancelled else 0.0)
                   for sp in job.specs}
        if prior is not None and abs(sum(per.values()) - usd) > 1e-6:
            per = {sp.episode_id: usd / len(job.specs) for sp in job.specs}
        costs = per
        succ = self._success_times(arts)
        rec = self._append({
            "kind": "outcome", "token": job.token, "rid": job.rid, "stage": self.stage.value,
            "run_kind": job.kind.value, "model": self.model_id, "batch_id": job.batch_id,
            "attempt": job.attempt, "episode_ids": [sp.episode_id for sp in job.specs],
            "reserved": job.reserved, "billed": usd, "estimated": est,
            "charge_kind": ("cancelled" if cancelled else
                            "billed" if est <= 1e-12 else "estimated"),
            "cancelled": cancelled, "failed": [e for e in reasons], "failed_reasons": reasons,
            "episode_costs": costs, "artifacts": [a.get("path") for a in arts],
            "last_success_utc": utc(max(succ)) if succ else None,
            "logs": logs, "notes": notes})
        for t in succ:
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
        n_run = self._smoke_runs() if self.smoke_allowance is not None else None
        if n_run is not None and bound > self._allowance_left(n_run) + 1e-12:
            # the smoke re-run's $3 allowance covers its test calls too (r2, finding 4)
            self.test_refused_allowance = True
            self._append({"kind": "test_result", "token": token, "rid": None, "ok": False,
                          "status": "refused_allowance", "smoke_run": n_run,
                          "settled_usd": 0.0, "estimated_usd": 0.0})
            return False
        try:
            rid = budget.reserve(bound, f"X3b {self.stage.value} outage test token={token}",
                                 exp=self.exp, cap=self.cap)
        except budget.BudgetRefused as e:
            self._append({"kind": "test_result", "token": token, "rid": None, "ok": False,
                          "status": "refused", "error": str(e)[:300], "settled_usd": 0.0,
                          "estimated_usd": 0.0})
            return False
        self._append({"kind": "test_intent", "token": token, "rid": rid, "bound_usd": bound,
                      "smoke_run": n_run})
        res = self.test_call(self.stage)
        billed = res.get("billed_usd")
        settled = float(billed) if billed is not None else bound
        est = 0.0 if billed is not None else bound
        budget.settle(rid, settled, self.model_id, 1,
                      f"X3b {self.stage.value} outage test rid={rid}; estimated ${est:.4f}")
        self._append({"kind": "test_result", "token": token, "rid": rid, "smoke_run": n_run,
                      "ok": bool(res.get("ok")), "status": "ok" if res.get("ok") else "error",
                      "billed_usd": billed, "settled_usd": settled, "estimated_usd": est,
                      "provider": res.get("provider"), "error": res.get("error")})
        return bool(res.get("ok"))

    def outage_step(self, now: float) -> str | None:
        """Prereg §7.3: no successful call for OUTAGE_PAUSE_MIN -> PAUSE (no new blocks); a
        test call every OUTAGE_PROBE_EVERY_MIN, the first at once; its first success -> RESUME;
        OUTAGE_STOP_H without a success -> the stage ends. The 12-hour deadline (restored
        downtime included) is evaluated first, before any test call or dispatch (code review
        r2, finding 8)."""
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
            if self.test_refused_allowance:
                return "smoke_failed"           # the re-run cannot proceed (r2, finding 4)
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
        # state first (target and headroom), then the record: a crash between the two leaves
        # the cut applied; resume also replays the last record into state (r2, finding 5)
        if target != start:
            self.state["cuts"] = [*self.state.get("cuts", []), rec]
        self.queue = [b for b in self.queue if b[0].repeat <= target]
        self.write_state(target_repeats=target, judge_set_aside_usd=round(judge, 6))
        self._append({"kind": "sizing", **rec})
        if target != start:
            self._emit(C.EventKind.CUT, rec)
        return rec

    def replay_sizing(self) -> None:
        """Resume: the last recorded sizing decision governs target repeats and headroom
        (never raised above it by an older state; r2, finding 5)."""
        recs = [r for r in self.recs if r.get("kind") == "sizing"]
        if not recs:
            return
        last = recs[-1]
        cur = self.state.get("target_repeats")
        if cur is None or int(cur) > int(last["to"]) or \
                self.state.get("judge_set_aside_usd") is None:
            cuts = self.state.get("cuts") or []
            if last["to"] != last["from"] and last not in cuts:
                cuts = [*cuts, {k: v for k, v in last.items() if k not in ("kind", "utc")}]
            self.write_state(target_repeats=int(last["to"]), cuts=cuts,
                             judge_set_aside_usd=max(
                                 float(self.state.get("judge_set_aside_usd") or 0.0),
                                 float(last["judge_set_aside_usd"])))

    def interim_cohort(self) -> list[str]:
        """The stage's first INTERIM_BLOCKS main blocks, by first admission (plan order; code
        review r1, finding 6)."""
        seen: list[str] = []
        for r in self.recs:
            if (r.get("kind") == "admit" and r.get("run_kind") == C.RunKind.MAIN.value
                    and r["batch_id"] not in seen):
                seen.append(r["batch_id"])
        return seen[: C.INTERIM_BLOCKS]

    def interim_step(self) -> str | None:
        """The interim completion rule (prereg §4.9) on the first 10 blocks, once each has its
        final outcome (retry included), then the second sizing point. A recorded decision is
        replayed on resume: a failing share stops again (finding 8)."""
        prev = [r for r in self.recs if r.get("kind") == "interim"]
        if prev:
            if prev[-1]["share"] > C.INTERIM_MAX_API_ERROR_SHARE:
                return "interim_completion"
            if not any(r.get("kind") == "sizing" and r.get("point") == "interim"
                       for r in self.recs):
                self._interim_sizing(prev[-1]["blocks"])
            return None
        cohort = self.interim_cohort()
        if len(cohort) < C.INTERIM_BLOCKS:
            return None
        fin = dict(self.final_main_blocks())
        if not all(b in fin for b in cohort):
            return None
        n = sum(len(fin[b]["episode_ids"]) for b in cohort)
        bad = sum(len(fin[b]["failed"]) for b in cohort)
        share = bad / n if n else 0.0
        self._append({"kind": "interim", "share": share, "n_episodes": n, "n_failed": bad,
                      "blocks": cohort})
        if share > C.INTERIM_MAX_API_ERROR_SHARE:
            return "interim_completion"
        self._interim_sizing(cohort)
        return None

    def _interim_sizing(self, blocks: list[str]) -> None:
        bids = set(blocks)
        costs = self._smoke_costs() + [
            v for r in self._outs(C.RunKind.MAIN) if r["batch_id"] in bids
            and not r.get("cancelled") for v in r["episode_costs"].values()]
        self.sizing("interim", costs)

    def _smoke_runs(self) -> int:
        """The current smoke run's number (the last smoke_run record's n)."""
        return max([int(r["n"]) for r in self.recs if r.get("kind") == "smoke_run"] or [0])

    def _smoke_costs(self) -> list[float]:
        n = self._smoke_runs()
        return [v for r in self._outs(C.RunKind.SMOKE) if r["attempt"] == n
                for v in r["episode_costs"].values()]

    def _allowance_left(self, n: int) -> float:
        """What is left of the smoke re-run's $3 allowance: minus the re-run's settled block
        charges and outage test calls, and its outstanding reservations (r2, finding 4)."""
        spent = sum(float(r["billed"]) for r in self._outs(C.RunKind.SMOKE)
                    if r["attempt"] == n)
        spent += sum(float(r.get("settled_usd") or 0.0) for r in self.recs
                     if r.get("kind") == "test_result" and r.get("smoke_run") == n)
        out = sum(j.reserved for j in self.running.values())
        return self.smoke_allowance - spent - out

    def _fit_allowance(self, block: list[EpisodeSpec]) -> list[EpisodeSpec] | None:
        """Smoke re-run (finding 4): the leading episodes of `block` whose worst case fits what
        is left of the $3 incremental allowance after the re-run's settled spend and its
        outstanding reservations; None when not even one episode fits."""
        left = self._allowance_left(block[0].attempt)
        k = math.floor((left + 1e-9) / episode_reservation(self.stage))
        return block[: min(k, len(block))] if k > 0 else None

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
                    head = self.queue[0]
                    block = head
                    if self.smoke_allowance is not None:
                        block = self._fit_allowance(head)
                        if block is None:
                            refused = self.allowance_refused = True
                            break
                    if self.admit(block, kind) is None:
                        refused = True
                        break
                    if len(block) < len(head):
                        self.queue[0] = head[len(block):]
                    else:
                        self.queue.pop(0)
            if not self.queue and not self.running:
                return "completed"
            if refused and not self.running:
                return "funds_exhausted"
            self.write_state()
            self.clock.sleep(self.cfg.poll_s)

    # -- the one shutdown path (plan §3 U3, item 11) --
    def pending_shutdown(self) -> str | None:
        """The reason of a shutdown that started but never reached TERMINAL (finding 2)."""
        recs = [r for r in self.recs if r.get("kind") == "shutdown"]
        return recs[-1]["reason"] if recs and not self.terminal() else None

    def _terminate_one(self, j: Job, errors: list[str]) -> bool:
        """Kill one running worker and remove its sandboxes. True only when its death and its
        cleanup are both confirmed; otherwise it stays in `running` and `unconfirmed`, keeping
        its reservation (r1 finding 1)."""
        alive = True
        try:
            res = self.ex.kill(j.handle, self.cfg.kill_grace_s)
            alive = res is False or self.ex.alive(j.handle)
        except Exception as e:  # noqa: BLE001 - recorded; the others still run
            errors.append(f"kill {j.token}: {e!r}"[:300])
        try:
            left = list(self.ex.cleanup(j))
        except Exception as e:  # noqa: BLE001
            left = [f"cleanup failed: {e!r}"[:300]]
        if alive or left:
            self.unconfirmed[j.token] = {"alive": alive, "containers": left}
            try:
                self._append({"kind": "kill_unconfirmed", "token": j.token, "alive": alive,
                              "containers": left})
            except Exception as e:  # noqa: BLE001
                errors.append(f"record {j.token}: {e!r}"[:300])
            return False
        self.running.pop(j.token, None)
        self.unconfirmed.pop(j.token, None)
        return True

    def _cancel_intents(self) -> set[str]:
        return {r["token"] for r in self.recs if r.get("kind") == "cancel_intent"}

    def terminate_all(self, errors: list[str]) -> None:
        """Kill every remaining worker of this process and remove its sandboxes, each isolated
        from the others' failures, settling a block as cancelled only once both are
        confirmed; then recover every other admission without an outcome (workers of an
        earlier process) the same way (r2, finding 1)."""
        for j in list(self.running.values()):
            try:   # durable before the kill, so recovery settles it as cancelled (r3, #1)
                if j.token not in self._cancel_intents():
                    self._append({"kind": "cancel_intent", "token": j.token, "rid": j.rid,
                                  "batch_id": j.batch_id, "attempt": j.attempt})
            except Exception as e:  # noqa: BLE001 - the kill still happens
                errors.append(f"cancel_intent {j.token}: {e!r}"[:300])
            if self._terminate_one(j, errors):
                try:
                    self.settle_job(j, cancelled=True)
                except Exception as e:  # noqa: BLE001 - recovered below or on the next run
                    errors.append(f"settle {j.token}: {e!r}"[:300])
        self.recover_unsettled(errors)

    def shutdown(self, reason: str) -> dict:
        """Stop dispatch; drain in-flight blocks for up to DRAIN_MIN; terminate the rest; settle;
        release leftovers; TERMINAL only with zero live workers, zero leftover sandboxes, zero
        outstanding reservations. Resumable: a crash part-way leaves a `shutdown` record that
        `run --resume`, `smoke` or `stop` finishes (finding 2)."""
        self.draining = True
        errors: list[str] = []
        n0 = sum(1 for r in self._outs() if r.get("cancelled"))
        resumed = self.pending_shutdown() is not None
        try:
            if not resumed:
                self._append({"kind": "shutdown", "reason": reason})
                if reason not in ("completed", "deadline"):
                    self._emit(C.EventKind.STOP, {"reason": reason,
                                                  "in_flight": len(self.running)})
            if self.running:
                self.write_state(state=C.RunState.DRAINING.value, stop_reason=reason)
            end = self.clock.now() + C.DRAIN_MIN * 60
            while self.running and self.clock.now() < end:
                try:
                    self.harvest()
                except Exception as e:  # noqa: BLE001 - go straight to termination
                    errors.append(f"harvest: {e!r}"[:300])
                    break
                if self.running:
                    self.clock.sleep(self.cfg.poll_s)
        finally:
            self.terminate_all(errors)
            try:
                self.release_leftovers()
            except Exception as e:  # noqa: BLE001
                errors.append(f"release_leftovers: {e!r}"[:300])
        st = self._status()
        live = [t for t, u in self.unconfirmed.items() if u.get("alive")]
        left = {t: u["containers"] for t, u in self.unconfirmed.items() if u.get("containers")}
        final = (C.RunState.ENDED if reason in ("completed", "funds_exhausted")
                 else C.RunState.STOPPED)
        state = self.write_state(state=final.value, stop_reason=reason, live_workers=len(live))
        info = {"reason": reason, "spent_usd": st["spent"], "outstanding_usd": st["outstanding"],
                "live_workers": len(live), "containers_left": left,
                "cancelled_blocks": sum(1 for r in self._outs() if r.get("cancelled")) - n0,
                "conservative_charges_usd": state["conservative_charges_usd"],
                "errors": errors}
        if errors:
            self._append({"kind": "shutdown_errors", "reason": reason, "errors": errors})
        open_adm = [a["token"] for a in self.unsettled_admissions()]
        info["admissions_without_outcome"] = open_adm
        if not self.unconfirmed and not open_adm and st["outstanding"] <= 1e-9:
            self._emit(C.EventKind.TERMINAL, {k: v for k, v in info.items() if k != "errors"})
            info["terminal"] = True
        else:
            info["terminal"] = False
            print(f"X3B_ERROR {self.stage.value}: not terminal (re-run `run --resume` or "
                  f"`stop`): {info}", flush=True)
        return info

    def release_leftovers(self) -> list[str]:
        """Close the reservations this manifest owns that are not admissions: a test call's at
        its bound, an intent that never reached admission released. An admission's
        reservation is never closed here: only recovery closes it, once its worker's death and
        cleanup are confirmed (r2, finding 1). Reservations not in this manifest are left
        alone; anything left open keeps TERMINAL from being written."""
        admits = {r["token"] for r in self.recs if r.get("kind") == "admit"}
        intents = {r["token"] for r in self.recs if r.get("kind") == "intent"}
        tests = {r["token"] for r in self.recs if r.get("kind") == "test_intent"}
        done = []
        for rid, res in list(budget._read_reservations().items()):
            if res.get("exp") != self.exp:
                continue
            tok = next((t.split("=", 1)[1] for t in res.get("note", "").split()
                        if t.startswith("token=")), None)
            if tok is None or tok in self.unconfirmed or tok in self.running or tok in admits:
                continue
            if _ledger_has_rid(rid):
                budget.release(rid)
                continue
            if tok in tests:
                budget.settle(rid, res["usd"], self.model_id, 0,
                              f"X3b {self.stage.value} leftover test call rid={rid} "
                              f"token={tok}; estimated ${res['usd']:.4f}")
                done.append(rid)
            elif tok in intents:
                budget.release(rid)
                self._append({"kind": "intent_closed", "token": tok, "released": [rid]})
                done.append(rid)
        return done

    def unsettled_admissions(self) -> list[dict]:
        settled = {r["token"] for r in self.recs if r.get("kind") == "outcome"}
        return [r for r in self.recs if r.get("kind") == "admit"
                and r["token"] not in settled]

    def recover_unsettled(self, errors: list[str]) -> list[dict]:
        """Every admission without an outcome that this process is not running: first marked
        unresolved (durably, and in `unconfirmed`, so nothing releases its reservation), then
        recovered one at a time, each in its own try/except (r2, finding 1)."""
        todo = [a for a in self.unsettled_admissions() if a["token"] not in self.running]
        marked = {r["token"] for r in self.recs if r.get("kind") == "unresolved"}
        for a in todo:
            self.unconfirmed.setdefault(a["token"], {"alive": None, "containers": [],
                                                     "pending": True})
            if a["token"] not in marked:
                try:
                    self._append({"kind": "unresolved", "token": a["token"]})
                except Exception as e:  # noqa: BLE001 - still held in memory
                    errors.append(f"mark {a['token']}: {e!r}"[:300])
        out = []
        was = self.draining
        self.draining = True                    # no retries are queued from here
        try:
            for a in todo:
                try:
                    rec = self._recover_one(a)
                    if rec is not None:
                        out.append(rec)
                except Exception as e:  # noqa: BLE001 - this one stays unresolved
                    errors.append(f"recover {a['token']}: {e!r}"[:300])
                    self.unconfirmed[a["token"]] = {"alive": None, "containers": [],
                                                    "error": repr(e)[:300]}
        finally:
            self.draining = was
        return out

    def _recover_one(self, a: dict) -> dict | None:
        """One admission without an outcome from an earlier process. Its reservation closes
        only on positive evidence (r2, findings 1-2):
        - never released (no GO): ABORT is written, so it can never call; it is settled at
          zero once it is confirmed gone: its pid confirmed dead, its own exit record, or no
          trace of it at all after ORPHAN_GRACE_S;
        - released: its pid (from READY, which precedes GO) must be confirmed dead, or be our
          worker and be killed with confirmation; and its compose projects must be gone. Then
          it is settled from its result (or as cancelled if it had to be killed)."""
        job = Job(token=a["token"], rid=a["rid"], stage=self.stage,
                  kind=C.RunKind(a["run_kind"]), specs=[EpisodeSpec(**sp) for sp in a["specs"]],
                  reserved=a["reserved"], attempt_dir=Path(a["attempt_dir"]),
                  task_name=a["task_name"], started_ts=parse_utc(a["utc"]) or 0.0)
        d = Path(job.attempt_dir)
        released = (d / GO_FILE).exists()
        if not released:
            _touch_atomic(d / ABORT_FILE)
        ready = _read_json(d / READY_FILE) or {}
        pid = ready.get("pid")
        if pid is None and (d / WORKER_PID).exists():
            try:
                pid = int((d / WORKER_PID).read_text().strip())
            except ValueError:
                pid = None
        state = worker_state(pid) if pid else "unknown"
        killed = False
        if state == "ours":
            killed = kill_group(pid, self.cfg.kill_grace_s)
            state = "dead" if killed and worker_state(pid) == "dead" else "unresolved"
        if not released and state in ("unknown", "unresolved"):
            res = _read_json(d / RESULT_JSON) or {}
            exited = "not released" in str(res.get("error")) or "aborted" in str(res.get("error"))
            age = self.clock.now() - job.started_ts
            if exited or (state == "unknown" and age >= ORPHAN_GRACE_S):
                state = "dead"
        if released and state == "unknown":
            state = "unresolved"            # GO without a READY pid: cannot be confirmed
        killed = killed or any(r.get("killed") for r in self.recs
                               if r.get("kind") == "kill_unconfirmed"
                               and r.get("token") == job.token)
        left = remove_compose_projects(job.task_name) if state == "dead" else []
        if state != "dead" or left:
            self.unconfirmed[job.token] = {"alive": state != "dead", "containers": left,
                                           "state": state}
            self._append({"kind": "kill_unconfirmed", "token": job.token, "state": state,
                          "killed": killed, "containers": left})
            return None
        self.unconfirmed.pop(job.token, None)
        if job.token in self._cancel_intents() and released:
            return self.settle_job(job, cancelled=True,
                                   extra_notes=["reconciled: cancelled at shutdown"])
        if not released:
            return self.settle_job(job, cancelled=False, nothing_sent=True,
                                   extra_notes=[("reconciled: worker never released, no "
                                                 "request sent")])
        return self.settle_job(job, cancelled=killed,
                               extra_notes=["reconciled after an interruption"])

    # -- recovery (port of X3's reconcile_pending / _release_orphans) --
    def reconcile(self) -> list[dict]:
        """After an interruption: release reservations of intents never admitted; settle test
        calls with no result at their bound; for every admission with no outcome, make sure
        its worker can never call (ABORT if it was never released), kill it if it still runs
        and remove its sandboxes, and only once both are confirmed settle it: at zero if it was
        never released (no request can have been sent), as cancelled if it had to be killed,
        else from its result or logs. Unconfirmed blocks keep their reservation (finding 1).
        Nothing is re-run here; nothing is settled twice."""
        recs = self.recs
        closed = {r["token"] for r in recs if r.get("kind") == "intent_closed"}
        admitted = {r["token"] for r in recs if r.get("kind") == "admit"}
        orphans = {r["token"] for r in recs if r.get("kind") == "intent"} - admitted - closed
        for tok in orphans:
            rel = []
            for rid, res in list(budget._read_reservations().items()):
                if res.get("exp") == self.exp and f"token={tok}" in res.get("note", ""):
                    budget.release(rid)
                    rel.append(rid)
            self._append({"kind": "intent_closed", "token": tok, "released": rel})
        results = {r["token"] for r in recs if r.get("kind") == "test_result"}
        open_res = budget._read_reservations()
        for r in [x for x in recs if x.get("kind") == "test_intent"
                  and x["token"] not in results]:
            if r["rid"] in open_res:
                budget.settle(r["rid"], r["bound_usd"], self.model_id, 1,
                              f"X3b {self.stage.value} outage test rid={r['rid']} "
                              f"(unresolved, reconciled at its bound); "
                              f"estimated ${r['bound_usd']:.4f}")
            self._append({"kind": "test_result", "token": r["token"], "rid": r["rid"],
                          "ok": False, "status": "unresolved", "billed_usd": None,
                          "settled_usd": r["bound_usd"], "estimated_usd": r["bound_usd"]})
        errors: list[str] = []
        out = self.recover_unsettled(errors)
        if errors:
            self._append({"kind": "reconcile_errors", "errors": errors})
        return out

    # -- smoke checkpoint (finding 7) and outage history (finding 8) --
    def smoke_checkpoint_ok(self) -> bool:
        sm = self.state.get("smoke") or {}
        return (sm.get("status") == "pass" and sm.get("judge_per_episode_usd") is not None
                and self.state.get("target_repeats") is not None
                and self.state.get("judge_set_aside_usd") is not None)

    def rebuild_smoke_checkpoint(self) -> bool:
        """Reconstruct the smoke checkpoint (criteria, judge bound, sizing) from the manifest's
        latest smoke outcomes and their artifacts; False if they do not pass or are gone."""
        n = self._smoke_runs()
        outs = [r for r in self._outs(C.RunKind.SMOKE) if r["attempt"] == n]
        if not n or not outs:
            return False
        arts = [json.loads(Path(p).read_text()) for r in outs for p in r["artifacts"]
                if p and Path(p).exists()]
        specs = [EpisodeSpec(**sp) for r in self.recs if r.get("kind") == "admit"
                 and r.get("run_kind") == C.RunKind.SMOKE.value and r["attempt"] == n
                 for sp in r["specs"]]
        ok, crit = smoke_gate(arts, specs, self.stage)
        if not ok:
            return False
        smoke = {**self.state.get("smoke", {}), "runs": n, "status": "pass", "criteria": crit,
                 "judge_per_episode_usd": round(judge_bound_per_episode(arts, self.stage), 8)}
        self.state["smoke"] = smoke
        prior = [r for r in self.recs if r.get("kind") == "sizing" and r.get("point") == "smoke"]
        if prior:
            per = smoke["judge_per_episode_usd"]
            self.write_state(target_repeats=prior[-1]["to"],
                             judge_set_aside_usd=round(per * self._planned_episodes(
                                 prior[-1]["to"]), 6))
        else:
            self.state.setdefault("target_repeats", self.planned_repeats)
            smoke["c_usd"] = self.sizing("smoke", self._smoke_costs())["c_usd"]
        self.write_state(smoke=smoke)
        return self.smoke_checkpoint_ok()

    def restore_outage_state(self) -> None:
        """On resume, before any dispatch: last success from durable records (outcomes, their
        artifacts, progress files, successful test calls), and the pause state from run_state,
        so a restart during an outage does not dispatch (finding 8)."""
        cands: list[float] = []
        for r in self.recs:
            k = r.get("kind")
            if k == "outcome":
                if "last_success_utc" in r:
                    t = parse_utc(r.get("last_success_utc"))
                    cands += [t] if t else []
                else:
                    arts = [json.loads(Path(p).read_text()) for p in r.get("artifacts") or []
                            if p and Path(p).exists()]
                    cands += self._success_times(arts)
            elif k == "admit":
                pp = Path(r["attempt_dir"]) / PROGRESS_JSONL
                if pp.exists():
                    lines = [x for x in pp.read_text().splitlines() if x.strip()]
                    try:
                        cands += [float(json.loads(lines[-1])["ts"])] if lines else []
                    except (ValueError, KeyError):
                        pass
            elif k == "test_result" and r.get("ok"):
                t = parse_utc(r.get("utc"))
                cands += [t] if t else []
        if cands:
            self.last_success = max(cands)
        if self.state.get("state") == C.RunState.PAUSED.value:
            self.paused = True
            tests = [parse_utc(r.get("utc")) for r in self.recs
                     if r.get("kind") == "test_result" and r.get("rid")]
            tests = [t for t in tests if t]
            self.last_test_ts = max(tests) if tests else None

    # -- entry points --
    def smoke(self) -> dict:
        """The stage's smoke test (prereg §4.9): run, gate, and on a pass the judge set-aside
        and the first sizing point, all durably written before SMOKE_PASS (finding 7). A
        second failure stops the stage. A re-run runs under a hard $3 allowance on its own
        spend (finding 4)."""
        if self.terminal():
            raise RunRefused(f"{self.stage.value} is terminal")
        self.reconcile()
        why = self.pending_shutdown()
        if why:
            return self.shutdown(why)
        self._refuse_unresolved()
        if self.smoke_passed():
            raise RunRefused(f"{self.stage.value} smoke already passed")
        n_prev = self._smoke_runs()
        if n_prev and self.smoke_checkpoint_ok() \
                and (self.state.get("smoke") or {}).get("runs") == n_prev:
            # the checkpoint was written but the process died before the event
            self._emit(C.EventKind.SMOKE_PASS, {"run": n_prev, "recovered": True})
            return {"reason": "smoke_passed", "terminal": False, "recovered": True}
        if n_prev >= SMOKE_MAX_RUNS:
            raise RunRefused(f"{self.stage.value}: {n_prev} smoke runs already")
        n = n_prev + 1
        blocks = [with_attempt(b, n) for b in
                  plan_stage(self.stage, C.RunKind.SMOKE, self.tasks, model_id=self.model_id)]
        if n > 1:
            self.smoke_allowance = C.SMOKE_FIX_RERUN_USD
        if self._has_activity():          # a re-run after a long idle period (r3, finding 2)
            self.restore_outage_state()
        self._append({"kind": "smoke_run", "n": n,
                      "allowance_usd": self.smoke_allowance})
        self.queue = list(blocks)
        self.write_state(state=C.RunState.SMOKE.value, stop_reason=None)
        why = self.pool(C.RunKind.SMOKE)
        if why == "funds_exhausted" and self.allowance_refused:
            # the $3 allowance cannot cover the next episode: the rest stays unrun (missing),
            # so the gate fails and the stage stops
            self._append({"kind": "smoke_allowance_exhausted", "n": n,
                          "unrun": [sp.episode_id for b in self.queue for sp in b]})
            self.queue = []
            why = "completed"
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
            self.write_state(state=C.RunState.SMOKE_FAILED.value, smoke=smoke)
            self._emit(C.EventKind.SMOKE_FAIL,
                       {"run": n, "failing": {k: v for k, v in crit.items() if not v["pass"]}})
            if n >= SMOKE_MAX_RUNS:
                return self.shutdown("smoke_failed")
            return {"reason": "smoke_failed", "terminal": False, "criteria": crit}
        smoke["judge_per_episode_usd"] = round(judge_bound_per_episode(arts, self.stage), 8)
        smoke["status"] = "pending"          # "pass" only once sizing is durable too
        self.state["smoke"] = smoke
        self.state.setdefault("target_repeats", self.planned_repeats)
        rec = self.sizing("smoke", self._smoke_costs())
        smoke["c_usd"] = rec["c_usd"]
        smoke["status"] = "pass"
        self.write_state(state=C.RunState.SMOKE.value, smoke=smoke)
        self._emit(C.EventKind.SMOKE_PASS, {"run": n, "mean_billed_usd":
                                            crit["mean_billed"]["value"]})
        return {"reason": "smoke_passed", "terminal": False, "criteria": crit, "sizing": rec}

    def run(self, resume: bool = False) -> dict:
        """The stage's main run as a rolling pool (prereg §7.2-§7.3)."""
        if self.terminal():
            raise RunRefused(f"{self.stage.value} is terminal")
        if any(r.get("kind") == "admit" and r.get("run_kind") == "main" for r in self.recs) \
                and not resume:
            raise RunRefused(f"{self.stage.value}: main run already started; use --resume")
        self.reconcile()
        why = self.pending_shutdown()
        if why:
            return self.shutdown(why)
        self._refuse_unresolved()
        if not self.smoke_passed():
            raise RunRefused(f"{self.stage.value}: no SMOKE_PASS in its events")
        if not self.smoke_checkpoint_ok() and not self.rebuild_smoke_checkpoint():
            raise RunRefused(f"{self.stage.value}: SMOKE_PASS without a durable smoke "
                             f"checkpoint (criteria, target repeats, judge headroom)")
        self.replay_sizing()
        if self._has_activity():          # independent of --resume (r3, finding 2)
            self.restore_outage_state()
        target = min(int(self.state.get("target_repeats") or self.planned_repeats),
                     self.planned_repeats)
        plan = [b for b in plan_stage(self.stage, C.RunKind.MAIN, self.tasks,
                                      self.planned_repeats, self.model_id)
                if b[0].repeat <= target]
        self.queue = self.resume_blocks(plan)
        self.retrying = True
        self.write_state(state=(C.RunState.PAUSED if self.paused else C.RunState.RUNNING).value,
                         stop_reason=None, target_repeats=target)
        return self.shutdown(self.pool(C.RunKind.MAIN))

    def _has_activity(self) -> bool:
        return any(r.get("kind") in ("outcome", "test_result") for r in self.recs)

    def _refuse_unresolved(self) -> None:
        """No new dispatch while an earlier worker's fate is unconfirmed (its reservation stays
        held); `stop` still runs the shutdown path, which retries the recovery."""
        if self.unconfirmed:
            raise RunRefused(f"{self.stage.value}: unresolved block attempts "
                             f"{sorted(self.unconfirmed)}; retry later or run `stop`")

    def stop_now(self, reason: str) -> dict:
        """`x3b_run.py stop` with no runner alive: reconcile, then the one shutdown path (or
        finish a pending one), so a crashed shutdown can always be completed (finding 2)."""
        if self.terminal():
            raise RunRefused(f"{self.stage.value} is terminal")
        self.reconcile()
        return self.shutdown(self.pending_shutdown() or reason)

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


def _missing_rows(stage: C.StageKey, seen: set[str], sel: dict[str, int | None],
                  canc: dict[str, set[int]]) -> list[dict]:
    """One row per admitted episode with no artifact (code review r1, finding 5), so every
    started episode is in the completion denominator. Filled from the admission specs; role
    "excluded", reason by the selected-attempt rule: a cancelled attempt -> "cancelled", the
    selected attempt -> "missing", an earlier attempt -> "superseded", no settled attempt ->
    "block_incomplete"."""
    recs = manifest_records(stage)
    by_token = {r["token"]: r for r in recs if r.get("kind") == "outcome"}
    rows = []
    for a in [r for r in recs if r.get("kind") == "admit"]:
        out = by_token.get(a["token"])
        for spd in a["specs"]:
            eid = spd["episode_id"]
            if eid in seen:
                continue
            seen.add(eid)
            att, bid = int(spd["attempt"]), spd["batch_id"]
            if (out or {}).get("cancelled") or att in canc.get(bid, set()):
                why = "cancelled"
            elif a["run_kind"] == C.RunKind.SMOKE.value:
                why = "missing"
            else:
                s = sel.get(bid)
                why = ("block_incomplete" if s is None or att > s
                       else "superseded" if att < s else "missing")
            row = dict.fromkeys(C.EPISODE_CSV_COLUMNS, "")
            row.update(episode_id=eid, stage=stage.value, model=spd["model_id"],
                       kind=spd["stage"], header=spd["context"], task_id=spd["task_id"],
                       repeat=spd["repeat"], attempt=att, block_id=bid,
                       analysis_role="excluded", excluded_reason=why)
            rows.append(row)
    return rows


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
    miss = _missing_rows(stage, seen, sel, canc)
    rows += miss
    out = C.episodes_csv(stage)
    out.parent.mkdir(parents=True, exist_ok=True)
    main = [r for r in rows if r["analysis_role"] == "main"]
    cov = {"n_rows": len(rows), "n_main": len(main),
           "by_role": {k: sum(r["analysis_role"] == k for r in rows) for k in C.ANALYSIS_ROLES},
           "missing_detector": sorted(r["episode_id"] for r in main if r["cheat"] == ""),
           "synthesized_missing": len(miss),
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
