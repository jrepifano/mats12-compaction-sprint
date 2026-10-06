"""U3: the X3b runner (implementation plan v0.4 §3 U3; prereg §4.6, §4.9, §7.2-§7.3).

No Docker: blocks run through a fake executor under a fake clock, with a scratch ledger
(`tmp_ledger`) and a scratch results tree (`tmp_results`)."""
from __future__ import annotations

import itertools
import json

import pytest

from x3 import contracts as X3C
from x3.task import EpisodeSpec
from x3b import contracts as C
from x3b import runner as R
from x3b import texts

pytestmark = pytest.mark.skipif(not hasattr(EpisodeSpec, "header_text"),
                                reason="U1's EpisodeSpec hook fields not present yet")

G, M = C.StageKey.GLM, C.StageKey.GEMINI
REAL_RESULTS = C.ROOT / "results" / "X3b"


@pytest.fixture(autouse=True)
def _never_the_real_tree():
    """A test must never leave files in the real results tree (r2 round: a monkeypatch.undo()
    once reverted the scratch fixtures mid-test)."""
    before = REAL_RESULTS.exists()
    yield
    assert before or not REAL_RESULTS.exists(), f"a test wrote under {REAL_RESULTS}"


def _tasks(n=15):
    return [X3C.TaskSpec(task_id=f"t{i:02d}", entry_point="f", description_md="d",
                         solution_py="s", test_solution_py="t", test_sha256="0",
                         conflict_pair=(3, 4)) for i in range(n)]


# --- model adapter -------------------------------------------------------------------------
def test_model_args_and_generate_config():
    assert R.model_args(G) == {"provider": C.MODELS[G].provider, "max_retries": 0}
    assert R.model_args(G)["provider"]["allow_fallbacks"] is False
    assert R.model_args(M) == {"provider": C.MODELS[M].provider, "max_retries": 0}
    assert R.model_args(G, R.MOCK_MODEL) == {}
    for st in C.StageKey:
        g = R.generate_config(st)
        assert g["max_tokens"] == 16_000 and g["parallel_tool_calls"] is False
    assert R.inspect_model_name(G) == "openrouter/z-ai/glm-5.3-flash"
    assert R.inspect_model_name(M, R.MOCK_MODEL) == R.MOCK_MODEL


@pytest.mark.parametrize("stage,mock", [(G, False), (M, False), (G, True)])
def test_inspect_eval_receives_the_stage_arguments(stage, mock, monkeypatch, tmp_path):
    import inspect_ai

    seen = {}

    def fake_eval(task, **kw):
        seen["task"] = task
        seen.update(kw)
        return []

    monkeypatch.setattr(inspect_ai, "eval", fake_eval)
    mid = R.MOCK_MODEL if mock else None
    block = R.plan_stage(stage, C.RunKind.MAIN, _tasks(2), repeats=1, model_id=mid)[0]
    R.eval_block(block, stage, tmp_path / "logs", "x3babcdef12")
    if mock:
        assert seen["model"] == R.MOCK_MODEL and seen["model_args"] == {}
    else:
        assert seen["model"] == f"openrouter/{C.MODELS[stage].model_id}"
        assert seen["model_args"] == {"provider": C.MODELS[stage].provider, "max_retries": 0}
    assert seen["max_tokens"] == 16_000 and seen["parallel_tool_calls"] is False
    assert seen["log_model_api"] is True and seen["fail_on_error"] is False
    assert seen["max_samples"] == len(block) and seen["ctl_server"] is False
    assert seen["task"].name == "x3babcdef12"          # names the block's compose projects
    assert len(seen["task"].dataset) == len(block)


# --- planning ------------------------------------------------------------------------------
@pytest.mark.parametrize("stage,n_blocks,size", [(G, 120, 6), (M, 135, 2)])
def test_main_plan_counts_headers_and_order(stage, n_blocks, size):
    b = R.plan_stage(stage, C.RunKind.MAIN, _tasks())
    assert len(b) == n_blocks and all(len(x) == size for x in b)
    hdrs = {h.value for h in C.STAGE_HEADERS[stage]}
    for blk in b:
        assert {e.context for e in blk} == hdrs
        assert len({(e.task_id, e.repeat) for e in blk}) == 1
        assert len({e.batch_id for e in blk}) == 1
    reps = [x[0].repeat for x in b]
    assert reps == sorted(reps)                          # repeats run in sequence
    assert [e.run_order_pos for x in b for e in x] == list(range(n_blocks * size))
    ids = [e.episode_id for x in b for e in x]
    assert len(set(ids)) == len(ids)
    again = R.plan_stage(stage, C.RunKind.MAIN, _tasks())
    assert ids == [e.episode_id for x in again for e in x]
    r1 = [x[0].task_id for x in b if x[0].repeat == 1]
    r2 = [x[0].task_id for x in b if x[0].repeat == 2]
    assert sorted(r1) == sorted(r2) and r1 != r2        # shuffled within each repeat
    if size > 2:
        assert len({tuple(e.context for e in x) for x in b}) > 1


def test_main_plan_seed_is_per_repeat():
    import numpy as np

    b = R.plan_stage(G, C.RunKind.MAIN, _tasks(), repeats=2)
    rng = np.random.default_rng([C.SEED_RUN_ORDER[G], 2])
    order = rng.permutation(15)
    assert [x[0].task_id for x in b if x[0].repeat == 2] == [f"t{i:02d}" for i in order]
    # a cut keeps the earlier repeats identical
    full = R.plan_stage(G, C.RunKind.MAIN, _tasks())
    assert [e.episode_id for x in b for e in x] == [e.episode_id for x in full[:30] for e in x]


def test_spec_fields():
    sp = R.plan_stage(M, C.RunKind.MAIN, _tasks(), repeats=1)[0][0]
    m = C.MODELS[M]
    assert sp.model_id == m.model_id and sp.model_slug == m.slug
    assert sp.stage == "main" and sp.prompt_level == C.PROMPT_LEVEL.value
    assert sp.header_text == texts.header_text(C.Header(sp.context))
    assert sp.pre_probe_prompt == texts.PRE_PROBE_PROMPT
    assert sp.post_probe_prompt == texts.POST_PROBE_PROMPT
    assert sp.probe_bound_usd == m.probe_bound_usd and sp.probe_max_tokens == 4_000
    assert sp.retry_empty_probe is True and sp.episode_limit_usd == m.episode_limit_usd
    assert (sp.in_ceiling, sp.out_ceiling) == (m.in_ceiling, m.out_ceiling)
    assert sp.run_order_seed == C.SEED_RUN_ORDER[M]
    assert sp.batch_id == C.block_id(M, C.RunKind.MAIN, sp.task_id, 1)
    assert sp.episode_id == C.episode_id(M, C.RunKind.MAIN, C.Header(sp.context), sp.task_id,
                                         1, 1)


@pytest.mark.parametrize("stage,n", [(G, 1), (M, 2)])
def test_smoke_plan(stage, n):
    b = R.plan_stage(stage, C.RunKind.SMOKE, _tasks())
    assert len(b) == n and [x[0].task_id for x in b] == [f"t{i:02d}" for i in range(n)]
    assert all(e.stage == "smoke" and e.repeat == 1 for x in b for e in x)
    assert all({e.context for e in x} == {h.value for h in C.STAGE_HEADERS[stage]} for x in b)


def test_block_reservation_matches_contract():
    for st in C.StageKey:
        b = R.plan_stage(st, C.RunKind.MAIN, _tasks(1), repeats=1)[0]
        assert R.block_reservation(st, b) == pytest.approx(C.MODELS[st].block_reservation_usd)


def test_with_attempt():
    b = R.plan_stage(G, C.RunKind.MAIN, _tasks(1), repeats=1)[0]
    b2 = R.with_attempt(b, 2)
    assert all(e.attempt == 2 and e.episode_id.endswith("|a2") for e in b2)


# --- extraction helpers --------------------------------------------------------------------
def test_call_roles():
    assert R.call_role(texts.PRE_PROBE_PROMPT) == "pre_probe"
    assert R.call_role(texts.POST_PROBE_PROMPT + "\n") == "post_probe"
    assert R.call_role("run the tests") == "agent"


def test_write_artifact_never_overwrites(tmp_results):
    art = _art("e1")
    p1 = R.write_artifact(dict(art))
    p2 = R.write_artifact(dict(art))
    assert p1 != p2 and p2.name.endswith(".dup1.json")
    assert R.write_artifact(dict(art), dup=False) is None


# --- costs ---------------------------------------------------------------------------------
def _call(billed=0.01, role="agent", error=None, provider="Relace", stop="stop", out=10):
    return {"billed_usd": billed, "role": role, "error": error, "provider": provider,
            "stop_reason": None if error else stop, "output_tokens": 0 if error else out,
            "input_tokens": 100, "usage_raw": {"cost": billed} if billed is not None else None,
            "utc": "2026-10-07T00:00:00.000000Z"}


def _probe(status="ok", reply="Q1 a", attempts=None, sac=0.001):
    return {"prompt": "", "reply": reply, "status": status, "usage": {}, "retries": 0,
            "input_bound": 100, "max_tokens": 4000, "failed_attempt_charge": 0.0,
            "spent_at_ceiling": sac,
            "attempts": attempts if attempts is not None else [
                {"kind": "ok", "input_bound": 100, "max_tokens": 4000, "charge": sac,
                 "usage": {}, "error": None}]}


def _art(eid, *, stage=G, kind="main", header="capability", task="t00", repeat=1, attempt=1,
         calls=None, end="done", turns=3, error=None, bounds=None, pre=None, post=None,
         batch=None, final="All done."):
    calls = calls if calls is not None else [_call(0.01), _call(0.002, "pre_probe"),
                                             _call(0.003, "post_probe")]
    return {"meta": {"episode_id": eid, "model": C.MODELS[stage].model_id, "context": header,
                     "task_id": task, "repeat": repeat, "attempt": attempt,
                     "prompt_level": "B", "stage": kind,
                     "batch_id": batch or C.block_id(stage, C.RunKind(kind), task, repeat),
                     "model_slug": C.MODELS[stage].slug},
            "stage_key": stage.value, "kind": kind, "header": header,
            "model_calls": calls, "end_reason": end, "n_turns": turns, "n_nudges": 0,
            "n_multi_call": 0, "error": error, "bounds": bounds or [],
            "spent_at_ceiling": 0.02, "messages": [], "final_message": final,
            "snapshots": [{"step": 0, "files": {}, "autoload": [], "error": None}],
            "pre_probe": pre if pre is not None else _probe(),
            "probe": post if post is not None else _probe()}


def _spec(eid):
    b = R.plan_stage(G, C.RunKind.MAIN, _tasks(1), repeats=1)[0][0]
    from dataclasses import replace
    return replace(b, episode_id=eid)


def test_settle_all_billed():
    usd, est, notes = R.settle_cost([_art("a")], [_spec("a")], G)
    assert usd == pytest.approx(0.015) and est == 0 and notes == []
    assert R.episode_cost(_art("a")) == pytest.approx(0.015)


def test_settle_missing_artifact_charged_in_full():
    usd, est, _ = R.settle_cost([], [_spec("a"), _spec("b")], G)
    assert usd == pytest.approx(2 * 0.60) and est == pytest.approx(1.20)


def test_regression_api_error_artifact_without_call_records_is_charged_in_full():
    a = _art("a", calls=[], end="api_error", turns=0)
    usd, est, notes = R.settle_cost([a], [_spec("a")], G)
    assert usd == pytest.approx(0.60) and est == pytest.approx(0.60)
    assert "no call records" in notes[0]
    b = _art("b", calls=[], end="done", turns=0, error="RuntimeError('sandbox')")
    assert R.settle_cost([b], [_spec("b")], G)[0] == pytest.approx(0.60)
    c = _art("c", calls=[], turns=2)
    assert R.settle_cost([c], [_spec("c")], G)[0] == pytest.approx(0.60)


def test_zero_only_when_nothing_was_sent():
    skipped = {**_probe(status="skipped_budget", reply=None, attempts=[]), "usage": None}
    a = _art("a", calls=[], end="cost_cap", turns=0, pre=skipped, post=skipped)
    assert R.settle_cost([a], [_spec("a")], G)[:2] == (0.0, 0.0)
    # a probe attempt recorded -> a request was sent -> full
    b = _art("b", calls=[], end="cost_cap", turns=0, pre=skipped)
    assert R.settle_cost([b], [_spec("b")], G)[0] == pytest.approx(0.60)


def test_failed_agent_attempt_matched_to_its_recorded_charge():
    from x3.task import ATTEMPTS, call_bound

    one = call_bound(1000, 16000, 0.15, 0.50) / ATTEMPTS
    bounds = [{"turn": 1, "input_bound": 1000, "max_tokens": 16000, "spent": 0.0,
               "failed_charge": one}]
    calls = [_call(None, error="APIError"), _call(0.01), _call(0.002, "pre_probe"),
             _call(0.003, "post_probe")]
    a = _art("a", calls=calls, bounds=bounds)
    assert R.episode_cost(a, G) == pytest.approx(0.015 + one)
    usd, est, _ = R.settle_cost([a], [_spec("a")], G)
    assert usd == pytest.approx(0.015 + one) and est == pytest.approx(one)
    # two unbilled failed calls but only one recorded failed attempt -> unmatched -> full
    calls2 = [_call(None, error="E"), _call(None, error="E"), _call(0.01)]
    a2 = _art("a2", calls=calls2, bounds=bounds)
    assert R.episode_cost(a2, G) is None
    assert R.settle_cost([a2], [_spec("a2")], G)[0] == pytest.approx(0.60)


def test_failed_probe_attempt_matched():
    att = [{"kind": "transport_error", "input_bound": 100, "max_tokens": 4000,
            "charge": 0.0021, "usage": None, "error": "E"},
           {"kind": "ok", "input_bound": 100, "max_tokens": 4000, "charge": 0.001,
            "usage": {}, "error": None}]
    calls = [_call(0.01), _call(None, "pre_probe", error="E"), _call(0.002, "pre_probe"),
             _call(0.003, "post_probe")]
    a = _art("a", calls=calls, pre=_probe(attempts=att))
    assert R.episode_cost(a, G) == pytest.approx(0.015 + 0.0021)


def test_unbilled_successful_call_is_charged_in_full():
    a = _art("a", calls=[_call(None), _call(0.002, "pre_probe")])
    assert R.episode_cost(a, G) is None
    usd, est, notes = R.settle_cost([a], [_spec("a")], G)
    assert usd == pytest.approx(0.60) and est == pytest.approx(0.60) and "unbilled" in notes[0]


def test_gemini_full_reservation_uses_its_own_probe_envelope():
    usd, _, _ = R.settle_cost([], [_spec("x")], M)
    assert usd == pytest.approx(0.80 + 0.30)


# --- fake clock and executor (pool tests) --------------------------------------------------
T0 = 1_791_331_200.0          # 2026-10-07T00:00:00Z


def _dead_pid() -> int:
    import subprocess
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


DEAD_PID = _dead_pid()        # a pid confirmed gone: what fake workers report in READY


class FakeClock:
    def __init__(self, t=T0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _iso_ts(t):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeExecutor:
    """Blocks 'run' for `duration` fake seconds, then write artifacts and result.json as the
    real worker does. behaviour(job, spec) -> "ok" | "api_error" | "missing"."""

    def __init__(self, clock, behaviour=None, duration=600.0, cost=0.01, progress=False,
                 unkillable=False, duration_fn=None):
        self.duration_fn = duration_fn
        self.clock, self.duration, self.cost = clock, duration, cost
        self.behaviour = behaviour or (lambda job, sp: "ok")
        self.progress, self.unkillable = progress, unkillable
        self.started, self.killed = [], []

    def start(self, job):
        self.started.append(job)
        d = self.duration_fn(job) if self.duration_fn else self.duration
        return {"job": job, "end": self.clock.now() + d, "done": False, "killed": False}

    def pid(self, h):
        return None

    def release(self, job):
        job.attempt_dir.mkdir(parents=True, exist_ok=True)
        R._write_json_atomic(job.attempt_dir / R.READY_FILE,
                             {"pid": DEAD_PID, "pgid": DEAD_PID})
        (job.attempt_dir / R.GO_FILE).write_text("go\n")

    def _write(self, job):
        paths = []
        now = _iso_ts(self.clock.now())
        for sp in job.specs:
            how = self.behaviour(job, sp)
            if how == "missing":
                continue
            kw = {"stage": job.stage, "kind": job.kind.value, "header": sp.context, "task": sp.task_id,
                      "repeat": sp.repeat, "attempt": sp.attempt, "batch": sp.batch_id}
            if how == "api_error_bare":         # no call records and no bounds
                a = _art(sp.episode_id, calls=[], end="api_error", turns=0, **kw)
                paths.append(str(R.write_artifact(a)))
                continue
            if how == "ok":
                calls = [_call(self.cost), _call(self.cost / 5, "pre_probe"),
                         _call(self.cost / 5, "post_probe")]
                for c in calls:
                    c["utc"] = now
                a = _art(sp.episode_id, calls=calls, **kw)
            else:
                skipped = {**_probe(status="skipped_budget", reply=None, attempts=[]),
                           "usage": None}
                err = _call(None, error="APIError")
                err["utc"] = now
                a = _art(sp.episode_id, calls=[err], end="api_error", turns=0,
                         bounds=[{"turn": 1, "input_bound": 1000, "max_tokens": 16000,
                                  "spent": 0.0, "failed_charge": 0.005}],
                         pre=skipped, post=skipped, **kw)
            paths.append(str(R.write_artifact(a)))
        job.attempt_dir.mkdir(parents=True, exist_ok=True)
        (job.attempt_dir / R.RESULT_JSON).write_text(json.dumps(
            {"ok": True, "artifacts": paths, "logs": [], "error": None}))

    def finished(self, h):
        if h["killed"]:
            return not self.unkillable
        if not h["done"] and self.clock.now() >= h["end"]:
            self._write(h["job"])
            h["done"] = True
        elif not h["done"] and self.progress:
            p = h["job"].attempt_dir / R.PROGRESS_JSONL
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(json.dumps({"ts": self.clock.now()}) + "\n")
        return h["done"]

    def alive(self, h):
        if h["killed"]:
            return self.unkillable
        return not h["done"]

    def kill(self, h, grace_s=0):
        h["killed"] = True
        self.killed.append(h["job"])

    def cleanup(self, job):
        return []


def _runner(stage=G, clock=None, ex=None, n_tasks=2, repeats=2, paid=False, **kw):
    clock = clock or FakeClock()
    ex = ex or FakeExecutor(clock)
    if paid:        # never let a test reach the real (paid) outage test call
        kw.setdefault("test_call", lambda st: pytest.fail("unexpected outage test call"))
    cfg = R.RunConfig(stage=stage, paid=paid, tasks=_tasks(n_tasks), repeats=repeats,
                      executor=ex, clock=clock, poll_s=60.0, kill_grace_s=0, **kw)
    r = R.StageRunner(cfg)
    r.lock()
    return r


def _events(stage=G):
    return [k for _, k, _ in R.read_events(stage)]


def _ledger(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _no_outstanding(stage=G):
    from s4world import budget
    return budget.status(C.LEDGER_EXP[stage], C.STAGE_CAP_USD[stage])["outstanding"] == 0


def _smoke_pass(stage=G, **kw):
    r = _runner(stage, **kw)
    out = r.smoke()
    r.close()
    assert out["reason"] == "smoke_passed", out
    return r


@pytest.fixture
def invariant(monkeypatch):
    """Assert spent + outstanding <= cap for each stage after every reserve/settle/release."""
    from s4world import budget
    seen = {"n": 0}

    def check():
        for st in C.StageKey:
            s = budget.status(C.LEDGER_EXP[st], C.STAGE_CAP_USD[st])
            assert s["spent"] + s["outstanding"] <= s["cap"] + 1e-9, s
        seen["n"] += 1

    for name in ("reserve", "settle", "release"):
        orig = getattr(budget, name)

        def wrap(*a, _o=orig, **k):
            out = _o(*a, **k)
            check()
            return out
        monkeypatch.setattr(budget, name, wrap)
    return seen


def test_smoke_then_main_run_completes(tmp_ledger, tmp_results, invariant):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    assert _events() == ["BLOCK_DONE", "SMOKE_PASS"]
    r = _runner(clock=clock)
    out = r.run()
    r.close()
    assert out["reason"] == "completed" and out["terminal"], out
    ev = _events()
    assert ev[-1] == "TERMINAL" and ev.count("BLOCK_DONE") == 1 + 4
    assert "STOP" not in ev and _no_outstanding()
    st = json.loads(C.run_state_json(G).read_text())
    assert tuple(st) == C.RUN_STATE_KEYS
    assert st["state"] == "ended" and st["stop_reason"] == "completed"
    assert st["blocks_done"] == 4 and st["episodes_done"] == 24 and st["live_workers"] == 0
    assert st["judge_set_aside_usd"] > 0 and st["smoke"]["status"] == "pass"
    led = _ledger(tmp_ledger)
    assert len(led) == 5 and all("estimated $0.0000" in e["note"] for e in led)
    assert all(e["exp"] == "X3b-GLM" for e in led)
    assert invariant["n"] > 0
    assert R.read_clock() is None                       # unpaid runs never create the clock
    kinds = [x["kind"] for x in R.manifest_records(G)]
    assert kinds.count("admit") == kinds.count("outcome") == 5


def test_failed_block_is_retried_whole_once(tmp_ledger, tmp_results, invariant):
    clock = FakeClock()
    _smoke_pass(clock=clock)

    def beh(job, sp):           # the first block fails one episode on both attempts;
        first = job.batch_id == ex.started[1].batch_id   # the second fails on attempt 1 only
        if first and sp.context == "safety":
            return "api_error"
        if job.batch_id == ex.started[2].batch_id and job.attempt == 1 \
                and sp.context == "neutral":
            return "missing"
        return "ok"

    ex = FakeExecutor(clock, behaviour=lambda j, s: beh(j, s))
    r = _runner(clock=clock, ex=ex, max_in_flight=2)
    ex.started.append(None)     # placeholder so index 1 is the first main block
    r.cfg.executor = ex
    out = r.run()
    r.close()
    assert out["terminal"]
    outs = [o for o in R.outcomes(G) if o["run_kind"] == "main"]
    by = {}
    for o in outs:
        by.setdefault(o["batch_id"], []).append(o["attempt"])
    assert sorted(len(v) for v in by.values()) == [1, 1, 2, 2]
    assert all(v == [1, 2] for v in by.values() if len(v) == 2)
    miss = next(o for o in outs if o["failed_reasons"] and
                "missing" in o["failed_reasons"].values())
    assert miss["estimated"] == pytest.approx(0.60)          # missing artifact at full
    st = R.read_run_state(G)
    assert st["blocks_done"] == 3 and st["blocks_failed"] == 1
    assert _no_outstanding()


def test_judge_headroom_refuses_before_reserving(tmp_ledger, tmp_results, invariant):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    st = R.read_run_state(G)
    st["judge_set_aside_usd"] = C.STAGE_CAP_USD[G] - 3.0     # no room for a $3.60 block
    R.write_run_state(G, st)
    r = _runner(clock=clock)
    out = r.run()
    r.close()
    assert out["reason"] == "funds_exhausted" and out["terminal"]
    recs = R.manifest_records(G)
    assert not [x for x in recs if x["kind"] == "admit" and x["run_kind"] == "main"]
    assert "STOP" in _events() and R.read_run_state(G)["state"] == "ended"


def test_blocks_wait_for_headroom_then_funds_exhausted(tmp_ledger, tmp_results, invariant,
                                                       monkeypatch):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    spent = sum(e["usd"] for e in _ledger(tmp_ledger))
    st = R.read_run_state(G)
    st["judge_set_aside_usd"] = 0.0
    R.write_run_state(G, st)
    # room for exactly two blocks at once; each block really costs $1.40 (6 x (1.4/6)...)
    monkeypatch.setitem(C.STAGE_CAP_USD, G, round(spent + 2 * 3.60 + 0.5, 4))
    ex = FakeExecutor(clock, cost=0.30)          # 6 x 0.42 = $2.52 billed per block
    r = _runner(clock=clock, ex=ex, n_tasks=3, repeats=2)
    out = r.run()
    r.close()
    assert out["reason"] == "funds_exhausted" and out["terminal"], out
    n_main = len([o for o in R.outcomes(G) if o["run_kind"] == "main"])
    assert 2 <= n_main < 6                      # some ran after waiting, the rest did not fit
    assert _no_outstanding()


@pytest.mark.parametrize("stage,points,expect", [
    (G, [0.055, 0.064, 5.0], [7, 6, 6]),
    (M, [0.24, 0.28, 5.0], [8, 7, 7]),
])
def test_sizing_cuts_whole_repeats_never_below_minimum(tmp_ledger, tmp_results, stage, points,
                                                       expect):
    r = _runner(stage, n_tasks=15, repeats=None)
    r.state["smoke"] = {"judge_per_episode_usd": 0.0}
    got = []
    for i, m in enumerate(points):
        rec = r.sizing("smoke" if i == 0 else "interim", [m] * 6)
        got.append(rec["to"])
    r.close()
    assert got == expect
    cuts = [p for _, k, p in R.read_events(stage) if k == "CUT"]
    assert [(c["from"], c["to"]) for c in cuts] == [(C.MODELS[stage].repeats, expect[0]),
                                                    (expect[0], expect[1])]
    assert cuts[0]["c_usd"] == pytest.approx(1.25 * points[0])
    last = [x for x in R.manifest_records(stage) if x["kind"] == "sizing"][-1]
    assert last["fits"] is False and last["to"] == C.MODELS[stage].min_repeats
    assert R.read_run_state(stage)["target_repeats"] == expect[-1]


def test_sizing_adds_the_judge_projection_not_the_bound(tmp_ledger, tmp_results):
    # prereg §7.2 worked example: 720 x 1.25 x $0.045 = $40.50 + $1.60 judges fits $45, even
    # with a large judging upper bound ($0.01 per episode), which only sets admission headroom
    r = _runner(G, n_tasks=15, repeats=None)
    r.state["smoke"] = {"judge_per_episode_usd": 0.01}
    rec = r.sizing("smoke", [0.045] * 6)
    r.close()
    assert rec["to"] == 8
    assert rec["judge_projection_usd"] == pytest.approx(0.002 * 720 + 0.16)
    assert rec["judge_set_aside_usd"] == pytest.approx(0.01 * 720)
    assert R.read_run_state(G)["judge_set_aside_usd"] == pytest.approx(7.20)


def test_sizing_cut_counts_the_judge_projection(tmp_ledger, tmp_results):
    # c = 1.25 x $0.05: 720 x 0.0625 = $45.00 + $1.60 > $45 -> 7 repeats
    # (630 x 0.0625 = $39.375 + 630 x 0.002 + 0.16 = $40.795 fits)
    r = _runner(G, n_tasks=15, repeats=None)
    r.state["smoke"] = {"judge_per_episode_usd": 0.001}
    rec = r.sizing("smoke", [0.05] * 6)
    r.close()
    assert rec["to"] == 7
    assert rec["judge_projection_usd"] == pytest.approx(0.002 * 630 + 0.16)
    assert rec["judge_set_aside_usd"] == pytest.approx(0.001 * 630)


def test_interim_completion_stop(tmp_ledger, tmp_results, invariant):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    ex = FakeExecutor(clock, behaviour=lambda j, s: "api_error"
                      if s.context in ("safety", "neutral") else "ok")
    r = _runner(clock=clock, ex=ex, n_tasks=12, repeats=1, max_in_flight=4)
    out = r.run()
    r.close()
    assert out["reason"] == "interim_completion" and out["terminal"]
    ev = R.read_events(G)
    stop = [p for _, k, p in ev if k == "STOP"]
    assert stop and stop[0]["reason"] == "interim_completion"
    interim = next(x for x in R.manifest_records(G) if x["kind"] == "interim")
    assert interim["share"] == pytest.approx(2 / 6) and len(interim["blocks"]) == 10
    assert _no_outstanding() and R.read_run_state(G)["state"] == "stopped"


def test_interim_completion_passes_at_low_error_share(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    r = _runner(clock=clock, n_tasks=12, repeats=1)
    out = r.run()
    r.close()
    assert out["reason"] == "completed"
    assert next(x for x in R.manifest_records(G) if x["kind"] == "interim")["share"] == 0
    assert [x["point"] for x in R.manifest_records(G) if x["kind"] == "sizing"] == \
        ["smoke", "interim"]


def _outage_runner(clock, test_ok_after=None, billed=1e-5):
    calls = []

    def tc(stage):
        calls.append(clock.now())
        ok = test_ok_after is not None and clock.now() >= test_ok_after
        return {"ok": ok, "billed_usd": billed if ok else None,
                "error": None if ok else "503"}
    ex = FakeExecutor(clock, behaviour=lambda j, s: "api_error", duration=300)
    return _runner(clock=clock, ex=ex, test_call=tc, n_tasks=15, repeats=1,
                   max_in_flight=2), calls


def test_outage_pause_then_resume(tmp_ledger, tmp_results, invariant):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    t_ok = clock.now() + 3 * 3600
    r, calls = _outage_runner(clock, test_ok_after=t_ok)
    r.queue = []

    # drive the loop by hand for 4 hours of fake time
    r.queue = r.resume_blocks(R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1))
    r.retrying = True
    end = clock.now() + 4 * 3600
    paused_at = None
    while clock.now() < end:
        r.harvest()
        assert r.outage_step(clock.now()) is None
        if r.paused and paused_at is None:
            paused_at = clock.now()
        if not r.paused:
            while r.queue and len(r.running) < r.max_in_flight:
                if r.admit(r.queue[0], C.RunKind.MAIN) is None:
                    break
                r.queue.pop(0)
        clock.sleep(60)
    ev = _events()
    assert "PAUSE" in ev and "RESUME" in ev and ev.index("PAUSE") < ev.index("RESUME")
    assert paused_at - T0 >= 30 * 60
    gaps = [b - a for a, b in itertools.pairwise(calls)]
    assert all(g >= 15 * 60 for g in gaps) and len(calls) >= 10
    tr = [x for x in R.manifest_records(G) if x["kind"] == "test_result"]
    ti = [x for x in R.manifest_records(G) if x["kind"] == "test_intent"]
    assert len(tr) == len(ti) == len(calls)
    bound = R.test_call_bound(G)
    assert all(x["settled_usd"] == pytest.approx(bound, abs=1e-6) for x in tr if not x["ok"])
    assert any(x["ok"] and x["settled_usd"] == pytest.approx(1e-5) for x in tr)
    led = [e for e in _ledger(tmp_ledger) if "outage test" in e["note"]]
    assert len(led) == len(calls) and all("estimated $" in e["note"] for e in led)
    r.shutdown("global_stop")
    r.close()


def test_outage_stops_the_stage_after_12_hours(tmp_ledger, tmp_results, invariant):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    r, calls = _outage_runner(clock, test_ok_after=None)
    out = r.run()
    r.close()
    assert out["reason"] == "outage" and out["terminal"]
    stop = next(p for _, k, p in R.read_events(G) if k == "STOP")
    assert stop["reason"] == "outage"
    assert clock.now() - T0 >= 12 * 3600
    assert len(calls) >= 12 * 4 - 3 and _no_outstanding()


def test_interrupted_test_call_is_recovered_at_its_bound(tmp_ledger, tmp_results):
    from s4world import budget
    clock = FakeClock()
    r = _runner(clock=clock)
    bound = R.test_call_bound(G)
    rid = budget.reserve(bound, "X3b glm outage test token=abc", exp="X3b-GLM", cap=45.0)
    r._append({"kind": "test_intent", "token": "abc", "rid": rid, "bound_usd": bound})
    r.close()                                          # crash before the result
    r2 = _runner(clock=clock)
    r2.reconcile()
    r2.reconcile()                                     # idempotent
    r2.close()
    res = [x for x in R.manifest_records(G) if x["kind"] == "test_result"]
    assert len(res) == 1 and res[0]["status"] == "unresolved"
    led = _ledger(tmp_ledger)
    assert len(led) == 1 and led[0]["usd"] == pytest.approx(bound, abs=1e-6) and _no_outstanding()


def test_deadline_drains_cancels_and_settles_in_full(tmp_ledger, tmp_results, invariant):
    clock = FakeClock()
    _smoke_pass(clock=clock, paid=True)
    clk = R.read_clock()
    assert clk["stage"] == "glm" and clk["first_paid_utc"] == R.utc(T0)
    # blocks of 31 h: none finishes before the deadline; progress keeps the outage rule quiet
    ex = FakeExecutor(clock, duration=31 * 3600, progress=True)
    r = _runner(clock=clock, ex=ex, paid=True, n_tasks=3, repeats=1)
    out = r.run()
    r.close()
    assert out["reason"] == "deadline" and out["terminal"] and out["cancelled_blocks"] == 3
    ev = _events()
    assert ev.index("DEADLINE") < ev.index("TERMINAL") and "PAUSE" not in ev
    dl = R.parse_utc(clk["deadline_utc"])
    assert dl == T0 + 30 * 3600 and clock.now() >= dl + 45 * 60
    canc = [o for o in R.outcomes(G) if o.get("cancelled")]
    assert len(canc) == 3 and all(o["billed"] == pytest.approx(3.60) for o in canc)
    assert all(o["charge_kind"] == "cancelled" for o in canc)
    led = [e for e in _ledger(tmp_ledger) if "conservative: cancelled" in e["note"]]
    assert len(led) == 3 and all("estimated $3.6000" in e["note"] for e in led)
    st = R.read_run_state(G)
    assert st["conservative_charges_usd"] == pytest.approx(10.80)
    assert st["live_workers"] == 0 and st["reserved_outstanding_usd"] == 0
    assert len(ex.killed) == 3


def test_no_terminal_while_a_worker_survives(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock, paid=True)
    ex = FakeExecutor(clock, duration=31 * 3600, progress=True, unkillable=True)
    r = _runner(clock=clock, ex=ex, paid=True, n_tasks=1, repeats=1)
    out = r.run()
    r.close()
    assert out["terminal"] is False and out["live_workers"] == 1
    assert "TERMINAL" not in _events()


def test_clock_created_once_and_reused_across_a_resume(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock, paid=True)
    first = C.CLOCK_JSON.read_text()
    clock.sleep(3600)
    ex = FakeExecutor(clock, duration=10 * 3600)
    r = _runner(clock=clock, ex=ex, paid=True, n_tasks=2, repeats=1)
    r.queue = R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1)
    r.admit(r.queue.pop(0), C.RunKind.MAIN)            # then the process dies
    r.close()
    assert C.CLOCK_JSON.read_text() == first
    clock.sleep(5 * 3600)                              # under the 12 h outage line
    r2 = _runner(clock=clock, ex=FakeExecutor(clock, duration=60, progress=True), paid=True,
                 n_tasks=2, repeats=1, test_call=lambda st: {"ok": True, "billed_usd": 1e-5})
    assert r2.deadline_ts() == T0 + 30 * 3600
    out = r2.run(resume=True)
    r2.close()
    assert C.CLOCK_JSON.read_text() == first and out["reason"] == "completed"
    # the restored idle time (6 h) paused the stage until a test call succeeded (r1 #8)
    ev = _events()
    assert ev.index("PAUSE") < ev.index("RESUME")


def test_reconcile_after_crash_between_admit_and_outcome(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    ex = FakeExecutor(clock, duration=60)
    r = _runner(clock=clock, ex=ex, n_tasks=2, repeats=1)
    blocks = R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1)
    j1 = r.admit(blocks[0], C.RunKind.MAIN)
    j2 = r.admit(blocks[1], C.RunKind.MAIN)
    clock.sleep(120)
    ex.finished(j1.handle)                    # block 1 finished (result.json written)
    # block 2: settled in the ledger, then the process died before the outcome record
    from s4world import budget
    budget.settle(j2.rid, 0.5, "m", 6, f"X3b glm main x a1 rid={j2.rid}; estimated $0.0000")
    r.close()
    n_led = len(_ledger(tmp_ledger))
    r2 = _runner(clock=clock, ex=ex, n_tasks=2, repeats=1)
    rec = r2.reconcile()
    assert len(rec) == 2 and all("reconciled after an interruption" in x["notes"] for x in rec)
    assert len(_ledger(tmp_ledger)) == n_led + 1          # block 1 settled; block 2 not twice
    r2.reconcile()
    assert len(_ledger(tmp_ledger)) == n_led + 1
    o1 = next(x for x in rec if x["token"] == j1.token)
    assert not o1["failed"] and o1["billed"] == pytest.approx(6 * 0.014)
    o2 = next(x for x in rec if x["token"] == j2.token)
    # settled before the crash: the outcome carries the ledger's amounts (r3, finding 1)
    assert o2["failed"] and (o2["billed"], o2["estimated"]) == (pytest.approx(0.5), 0.0)
    out = r2.run(resume=True)
    r2.close()
    assert out["reason"] == "completed"
    assert sorted(o["attempt"] for o in R.outcomes(G) if o["run_kind"] == "main") == [1, 1, 2]


def test_orphan_intent_reservation_released(tmp_ledger, tmp_results):
    from s4world import budget
    r = _runner()
    r._append({"kind": "intent", "token": "tok1", "stage": "glm", "run_kind": "main",
               "batch_id": "b", "attempt": 1})
    budget.reserve(3.6, "X3b glm b a1 token=tok1", exp="X3b-GLM", cap=45.0)
    r.close()
    r2 = _runner()
    r2.reconcile()
    r2.close()
    assert _no_outstanding() and _ledger(tmp_ledger) == []


def test_run_refusals(tmp_ledger, tmp_results):
    clock = FakeClock()
    r = _runner(clock=clock)
    with pytest.raises(R.RunRefused, match="SMOKE_PASS"):
        r.run()
    with pytest.raises(R.RunRefused, match="another runner"):
        _runner(clock=clock)
    r.close()
    _smoke_pass(clock=clock)
    r = _runner(clock=clock, ex=FakeExecutor(clock, duration=10 ** 6), n_tasks=1, repeats=1)
    r.queue = R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1)
    r.admit(r.queue.pop(0), C.RunKind.MAIN)
    r.close()
    r = _runner(clock=clock)
    with pytest.raises(R.RunRefused, match="--resume"):
        r.run()
    R.request_stop(G)
    out = r.run(resume=True)
    r.close()
    assert out["reason"] == "global_stop" and out["terminal"]
    r = _runner(clock=clock)
    with pytest.raises(R.RunRefused, match="terminal"):
        r.run(resume=True)
    with pytest.raises(R.RunRefused, match="terminal"):
        r.smoke()
    r.close()


# --- smoke gate and re-run limit -----------------------------------------------------------
def _smoke_specs(stage=G):
    return [sp for b in R.plan_stage(stage, C.RunKind.SMOKE, _tasks()) for sp in b]


def _smoke_arts(specs, **over):
    return [_art(sp.episode_id, header=sp.context, task=sp.task_id, kind="smoke", **over)
            for sp in specs]


def test_smoke_gate_pass():
    specs = _smoke_specs()
    ok, crit = R.smoke_gate(_smoke_arts(specs), specs, G)
    assert ok and crit["mean_billed"]["value"] == pytest.approx(0.015)


def _all_billed(arts, v):
    for a in arts:
        for c in a["model_calls"]:
            c["billed_usd"] = v


@pytest.mark.parametrize("breaker,criterion", [
    (lambda arts: arts.pop(), "completed"),
    (lambda arts: arts[0].update(end_reason="api_error"), "completed"),
    (lambda arts: arts[0]["model_calls"][0].update(usage_raw=None), "completed"),
    (lambda arts: arts[0]["model_calls"][0].update(provider=None), "completed"),
    (lambda arts: arts[0]["snapshots"].append({"step": 1, "files": None}), "completed"),
    (lambda arts: [a["probe"].update(reply="  ") for a in arts[:2]], "probes_non_empty"),
    (lambda arts: _all_billed(arts, 0.05), "mean_billed"),
    (lambda arts: arts[0]["model_calls"][0].update(billed_usd=None), "mean_billed"),
])
def test_smoke_gate_each_failure(breaker, criterion):
    specs = _smoke_specs()
    arts = _smoke_arts(specs)
    breaker(arts)
    ok, crit = R.smoke_gate(arts, specs, G)
    assert not ok and not crit[criterion]["pass"]
    if criterion != "completed":       # a missing artifact also fails the probe criterion
        assert all(v["pass"] for k, v in crit.items() if k != criterion)


def test_one_empty_probe_is_allowed():
    specs = _smoke_specs(M)
    arts = _smoke_arts(specs, stage=M)
    arts[0]["pre_probe"]["reply"] = ""
    assert R.smoke_gate(arts, specs, M)[0]


def test_smoke_fail_rerun_and_second_failure_stops_the_stage(tmp_ledger, tmp_results):
    clock = FakeClock()
    ex = FakeExecutor(clock, cost=0.06)                # mean billed 0.084 > 0.06
    r = _runner(clock=clock, ex=ex)
    out = r.smoke()
    r.close()
    assert out["reason"] == "smoke_failed" and not out["terminal"]
    assert R.read_run_state(G)["state"] == "smoke_failed"
    fail = next(p for _, k, p in R.read_events(G) if k == "SMOKE_FAIL")
    assert list(fail["failing"]) == ["mean_billed"]
    r = _runner(clock=clock, ex=ex)
    with pytest.raises(R.RunRefused, match="SMOKE_PASS"):
        r.run()
    out = r.smoke()                                    # the one re-run after a correction
    r.close()
    assert out["reason"] == "smoke_failed" and out["terminal"]
    assert next(p for _, k, p in R.read_events(G) if k == "STOP")["reason"] == "smoke_failed"
    # the re-run block (6 x $0.60 > $3) ran as 5 + 1 episodes under the allowance
    assert sorted(o["attempt"] for o in R.outcomes(G)) == [1, 2, 2]


def test_smoke_rerun_spend_is_held_to_the_3_dollar_allowance(tmp_ledger, tmp_results,
                                                              invariant):
    """Finding 4: the re-run's actual spend (settled + outstanding) never passes $3; episodes
    whose worst case no longer fits stay unrun, so the gate fails and the stage stops."""
    clock = FakeClock()
    ex = FakeExecutor(clock, cost=0.40)                 # 0.56 billed per episode
    r = _runner(clock=clock, ex=ex)
    r.smoke()
    r.close()
    seen = []
    orig = R.StageRunner.admit

    def spy(self, block, kind):
        n = block[0].attempt
        spent = sum(o["billed"] for o in self._outs(C.RunKind.SMOKE) if o["attempt"] == n)
        out = sum(j.reserved for j in self.running.values())
        job = orig(self, block, kind)
        seen.append((len(block), spent + out + R.block_reservation(G, block)))
        return job
    r = _runner(clock=clock, ex=ex)
    r.admit = spy.__get__(r)
    out = r.smoke()
    r.close()
    assert out["reason"] == "smoke_failed" and out["terminal"]
    assert seen == [(5, pytest.approx(3.0))]             # 5 x 0.60 fits; the 6th never runs
    rerun = sum(o["billed"] for o in R.outcomes(G) if o["attempt"] == 2)
    assert rerun == pytest.approx(5 * 0.56) and rerun <= C.SMOKE_FIX_RERUN_USD
    ex_rec = [x for x in R.manifest_records(G) if x["kind"] == "smoke_allowance_exhausted"]
    assert len(ex_rec) == 1 and len(ex_rec[0]["unrun"]) == 1
    fail = [p for _, k, p in R.read_events(G) if k == "SMOKE_FAIL"][-1]
    assert "completed" in fail["failing"]


def test_smoke_rerun_then_pass(tmp_ledger, tmp_results):
    clock = FakeClock()
    r = _runner(clock=clock, ex=FakeExecutor(clock, cost=0.06))
    assert r.smoke()["reason"] == "smoke_failed"
    r.close()
    r = _runner(clock=clock, ex=FakeExecutor(clock, cost=0.01))
    assert r.smoke()["reason"] == "smoke_passed"
    with pytest.raises(R.RunRefused, match="already passed"):
        r.smoke()
    r.close()
    assert R.read_run_state(G)["smoke"]["first_charge_usd"] == pytest.approx(6 * 0.084)


# --- collect -------------------------------------------------------------------------------
def _outcome(stage, bid, attempt, eids, failed=(), cancelled=False, kind="main"):
    R.append_manifest(stage, {"kind": "outcome", "token": f"{bid}{attempt}", "rid": "r",
                              "stage": stage.value, "run_kind": kind, "batch_id": bid,
                              "attempt": attempt, "episode_ids": eids, "reserved": 3.6,
                              "billed": 0.1, "estimated": 0.0, "charge_kind": "billed",
                              "cancelled": cancelled, "failed": list(failed),
                              "failed_reasons": {}, "episode_costs": {}, "artifacts": [],
                              "logs": [], "notes": []})


def _block_arts(stage, task, attempt, bad=(), kind="main", parsed=True, provider="Relace"):
    out = []
    for h in C.STAGE_HEADERS[stage]:
        eid = C.episode_id(stage, C.RunKind(kind), h, task, 1, attempt)
        a = _art(eid, stage=stage, kind=kind, header=h.value, task=task, attempt=attempt)
        for c in a["model_calls"]:
            c["provider"] = provider
        if h.value in bad:
            a["end_reason"] = "api_error"
        if parsed:
            a["pre_probe"]["parsed"] = {"pre_q1": "a", "pre_q2": "b"}
            a["probe"]["parsed"] = {"q1": "a", "q2": "a", "q3_spec": "no", "q3_tests": "yes",
                                    "q4": "a"}
        R.write_artifact(a)
        out.append(a)
    return out


def _rows(stage):
    import csv
    with open(C.episodes_csv(stage), newline="") as f:
        rd = csv.DictReader(f)
        assert tuple(rd.fieldnames) == C.EPISODE_CSV_COLUMNS
        return {r["episode_id"]: r for r in rd}


def test_collect_attempt_selection_and_per_header_missingness(tmp_results, monkeypatch):
    monkeypatch.setattr(R, "accepted", lambda stage, j: {})
    # b1: attempt 1 failed (safety), attempt 2 settled with neutral failed -> attempt 2
    a1 = _block_arts(G, "t01", 1, bad=("safety",))
    a2 = _block_arts(G, "t01", 2, bad=("neutral",))
    b1 = a1[0]["meta"]["batch_id"]
    _outcome(G, b1, 1, [a["meta"]["episode_id"] for a in a1],
             failed=[a["meta"]["episode_id"] for a in a1 if a["header"] == "safety"])
    _outcome(G, b1, 2, [a["meta"]["episode_id"] for a in a2],
             failed=[a["meta"]["episode_id"] for a in a2 if a["header"] == "neutral"])
    # b2: attempt 1 settled with capability failed; attempt 2 cancelled (no artifacts)
    c1 = _block_arts(G, "t02", 1, bad=("capability",))
    b2 = c1[0]["meta"]["batch_id"]
    _outcome(G, b2, 1, [a["meta"]["episode_id"] for a in c1], failed=["x"])
    _outcome(G, b2, 2, [], cancelled=True)
    # smoke rows
    s1 = _block_arts(G, "t00", 1, kind="smoke")
    R.collect(G)
    rows = _rows(G)
    assert len(rows) == 6 * 4
    for a in a1:
        assert rows[a["meta"]["episode_id"]]["excluded_reason"] == "superseded"
    for a in a2:
        r = rows[a["meta"]["episode_id"]]
        if a["header"] == "neutral":
            assert (r["analysis_role"], r["excluded_reason"]) == ("excluded", "api_error")
        else:
            assert (r["analysis_role"], r["excluded_reason"]) == ("main", "")
    for a in c1:
        r = rows[a["meta"]["episode_id"]]
        assert r["analysis_role"] == ("excluded" if a["header"] == "capability" else "main")
    assert all(rows[a["meta"]["episode_id"]]["analysis_role"] == "smoke" for a in s1)
    r = rows[a2[1]["meta"]["episode_id"]]
    assert (r["pre_source"], r["pre_q1"], r["q4"], r["q3_tests"]) == ("letter", "a", "a", "yes")
    assert r["billed_usd"] == "0.015" and r["billed_agent_usd"] == "0.01"
    assert float(r["billed_probe_usd"]) == pytest.approx(0.005)
    assert float(r["ceiling_usd"]) == pytest.approx(0.022)
    assert r["providers"] == "Relace" and r["n_non_relace_calls"] == "0"
    assert (r["has_final_message"], r["ended_done_with_message"]) == ("true", "true")
    assert r["cheat"] == "" and r["disc_source"] == ""
    cov = json.loads(C.episodes_csv(G).with_suffix(".coverage.json").read_text())
    assert cov["n_rows"] == 24 and cov["by_role"]["smoke"] == 6


def test_collect_judge_fallback_disclosure_and_detector(tmp_results, monkeypatch):
    arts = _block_arts(M, "t03", 1, parsed=False, provider="Google")
    _outcome(M, arts[0]["meta"]["batch_id"], 1, [a["meta"]["episode_id"] for a in arts])
    e0, e1 = (a["meta"]["episode_id"] for a in arts)
    assert not arts[0]["probe"].get("parsed")
    labels = {
        C.Judgment.PRE_PROBE_PARSE: {e0: {"pre_q1": ("b", "agree"), "pre_q2": ("c", "agree")}},
        C.Judgment.POST_PROBE_PARSE: {e0: {"q1": ("a", "single"), "q4": ("e", "single")}},
        C.Judgment.DISCLOSURE: {e0: {"states_failing_test": ("true", "reference"),
                                     "states_workaround": ("false", "reference")}},
    }
    monkeypatch.setattr(R, "accepted", lambda stage, j: labels[j])
    import csv
    C.detector_csv(M).parent.mkdir(parents=True, exist_ok=True)
    with open(C.detector_csv(M), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["episode_id", "tamper_any", "tamper_final",
                                          "tamper_ambiguous", "pass_run1", "pass_run2",
                                          "pass_disagree", "passed", "cheat"])
        w.writeheader()
        w.writerow({"episode_id": e0, "tamper_any": "True", "tamper_final": "True",
                    "tamper_ambiguous": "False", "pass_run1": "True", "pass_run2": "True",
                    "pass_disagree": "False", "passed": "True", "cheat": "True"})
    R.collect(M)
    rows = _rows(M)
    r0, r1 = rows[e0], rows[e1]
    assert (r0["pre_source"], r0["pre_q1"], r0["pre_q2"]) == ("agree", "b", "c")
    assert (r0["post_source"], r0["q1"], r0["q2"], r0["q4"]) == ("single", "a", "", "e")
    assert (r0["disc_failing_test"], r0["disc_workaround"], r0["disc_source"]) == \
        ("true", "false", "reference")
    assert r0["cheat"] == "True" and r1["cheat"] == ""
    assert (r1["pre_source"], r1["pre_q1"], r1["disc_failing_test"]) == ("", "", "")
    assert r0["n_non_relace_calls"] == "3" and r0["stage"] == "gemini"
    assert r0["providers"] == "Google"


def test_collect_after_a_fake_run(tmp_ledger, tmp_results, monkeypatch):
    monkeypatch.setattr(R, "accepted", lambda stage, j: {})
    clock = FakeClock()
    _smoke_pass(clock=clock)
    ex = FakeExecutor(clock, behaviour=lambda j, s: "api_error"
                      if j.attempt == 1 and s.context == "safety" else "ok")
    r = _runner(clock=clock, ex=ex, n_tasks=1, repeats=1)
    r.run()
    r.close()
    R.collect(G)
    rows = _rows(G)
    roles = sorted((x["attempt"], x["analysis_role"], x["excluded_reason"])
                   for x in rows.values() if x["kind"] == "main")
    assert roles == [("1", "excluded", "superseded")] * 6 + [("2", "main", "")] * 6


# --- code review r1 regressions (logs/reviews/20261006-X3b-code-r1.md) --------------------
def test_r1_f1_worker_waits_for_go_and_obeys_abort(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    assert R._wait_for_go(d, timeout_s=0.3, sleep=lambda s: None) is False      # timeout
    (d / R.GO_FILE).write_text("go")
    assert R._wait_for_go(d, timeout_s=0.3, sleep=lambda s: None) is True
    (d / R.ABORT_FILE).write_text("x")
    assert R._wait_for_go(d, timeout_s=0.3, sleep=lambda s: None) is False      # abort wins


def test_r1_f1_real_unreleased_worker_never_calls(tmp_results):
    """A spawned worker that is never released exits on ABORT without any eval."""
    import time
    block = R.plan_stage(G, C.RunKind.MAIN, _tasks(1), repeats=1, model_id=R.MOCK_MODEL)[0]
    job = R.Job(token="t" * 12, rid="r", stage=G, kind=C.RunKind.MAIN, specs=block,
                reserved=0.0, attempt_dir=tmp_results / "glm" / "logs" / "unreleased",
                task_name="x3bunrel0001", started_ts=0.0)
    ex = R.ProcessExecutor()
    h = ex.start(job)
    for _ in range(100):
        if (job.attempt_dir / R.WORKER_PID).exists():
            break
        time.sleep(0.1)
    R._touch_atomic(job.attempt_dir / R.ABORT_FILE)
    h.join(30)
    assert not h.is_alive()
    res = json.loads((job.attempt_dir / R.RESULT_JSON).read_text())
    assert res["ok"] is False and "not released" in res["error"]
    assert not list(job.attempt_dir.glob("*.eval"))


class _NoPidExecutor(FakeExecutor):
    def pid(self, h):
        raise RuntimeError("crash between spawn and registration")


def test_r1_f1_crash_before_registration_is_never_released(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    ex = _NoPidExecutor(clock, duration=60)
    r = _runner(clock=clock, ex=ex, n_tasks=1, repeats=1)
    block = R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1)[0]
    with pytest.raises(RuntimeError, match="registration"):
        r.admit(block, C.RunKind.MAIN)
    job = ex.started[-1]
    assert (job.attempt_dir / R.ABORT_FILE).exists()
    assert not (job.attempt_dir / R.GO_FILE).exists()
    assert job.token in r.running                       # shutdown would still see it
    r.close()
    # no pid, no exit record yet: unresolved, reservation held, no dispatch (r2, finding 2)
    r2 = _runner(clock=clock, n_tasks=1, repeats=1)
    assert r2.reconcile() == [] and job.token in r2.unconfirmed and not _no_outstanding()
    with pytest.raises(R.RunRefused, match="unresolved"):
        r2.run(resume=True)
    r2.close()
    clock.sleep(R.ORPHAN_GRACE_S)                  # long past any possible worker start
    r3 = _runner(clock=clock, n_tasks=1, repeats=1)
    rec = r3.reconcile()
    r3.close()
    assert len(rec) == 1 and rec[0]["billed"] == 0 and "never released" in rec[0]["notes"][0]
    assert _no_outstanding()


def test_r1_f1_reconcile_keeps_reservation_until_kill_and_cleanup_confirmed(
        tmp_ledger, tmp_results, monkeypatch):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    ex = FakeExecutor(clock, duration=10 ** 6)
    r = _runner(clock=clock, ex=ex, n_tasks=1, repeats=1)
    job = r.admit(R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1)[0], C.RunKind.MAIN)
    r.close()                                            # the runner dies; worker "alive"
    R._write_json_atomic(job.attempt_dir / R.READY_FILE, {"pid": 424242, "pgid": 424242})
    proc = {"alive": True}
    monkeypatch.setattr(R, "worker_state",
                        lambda pid: ("ours" if proc["alive"] else "dead") if pid == 424242
                        else "unresolved")
    monkeypatch.setattr(R, "kill_group", lambda pid, grace=0, **k: False)
    monkeypatch.setattr(R, "remove_compose_projects", lambda tag: [])
    r2 = _runner(clock=clock, n_tasks=1, repeats=1)
    out = r2.stop_now("global_stop")
    r2.close()
    assert out["terminal"] is False and not _no_outstanding()
    assert not [o for o in R.outcomes(G) if o["run_kind"] == "main"]
    assert [x for x in R.manifest_records(G) if x["kind"] == "kill_unconfirmed"]
    # sandboxes left behind also keep it open
    def kill_ok(pid, grace=0, **k):
        proc["alive"] = False
        return True
    monkeypatch.setattr(R, "kill_group", kill_ok)
    monkeypatch.setattr(R, "remove_compose_projects", lambda tag: ["abc inspect-x3b..."])
    r3 = _runner(clock=clock, n_tasks=1, repeats=1)
    assert r3.stop_now("global_stop")["terminal"] is False
    r3.close()
    # both confirmed: settled as cancelled, in full, then TERMINAL
    monkeypatch.setattr(R, "remove_compose_projects", lambda tag: [])
    r4 = _runner(clock=clock, n_tasks=1, repeats=1)
    out = r4.stop_now("global_stop")
    r4.close()
    assert out["terminal"] and _no_outstanding()
    o = next(o for o in R.outcomes(G) if o["run_kind"] == "main")
    assert o["cancelled"] and o["billed"] == pytest.approx(3.60)
    assert _events().count("STOP") == 1                   # resumed, not restarted


class _FlakyExecutor(FakeExecutor):
    """finished() raises for one block; cleanup() raises for another."""
    def __init__(self, clock, **kw):
        super().__init__(clock, duration=10 ** 6, **kw)

    def finished(self, h):
        if h["job"] is self.started[0] and not h["killed"]:
            raise OSError("harvest blew up")
        return super().finished(h)

    def cleanup(self, job):
        if job is self.started[1]:
            raise RuntimeError("docker down")
        return []


def test_r1_f2_shutdown_survives_failures_and_is_resumable(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    ex = _FlakyExecutor(clock)
    r = _runner(clock=clock, ex=ex, n_tasks=3, repeats=1)
    for b in R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1):
        r.admit(b, C.RunKind.MAIN)
    out = r.shutdown("global_stop")
    r.close()
    assert len(ex.killed) == 3                           # every worker got the kill
    assert out["terminal"] is False and any("harvest" in e for e in out["errors"])
    assert next(iter(out["containers_left"].values()))[0].startswith("cleanup failed")
    canc = [o for o in R.outcomes(G) if o.get("cancelled")]
    assert len(canc) == 2                                # the confirmed ones are settled
    assert [x for x in R.manifest_records(G) if x["kind"] == "shutdown_errors"]
    # `stop` (or run --resume) in a new process finishes the same shutdown
    r2 = _runner(clock=clock, n_tasks=3, repeats=1)
    out2 = r2.stop_now("global_stop")
    r2.close()
    assert out2["terminal"] and out2["reason"] == "global_stop" and _no_outstanding()
    assert _events()[-1] == "TERMINAL"


def test_r1_f2_ledger_check_uses_the_locked_rid_lookup(tmp_ledger):
    from s4world import budget
    rid = budget.reserve(1.0, "x", exp="X3b-GLM", cap=45.0)
    other = budget.reserve(1.0, "y", exp="X3b-GLM", cap=45.0)
    budget.settle(rid, 0.5, "m", 1, f"note mentions rid={other}")
    assert R._ledger_has_rid(rid) and not R._ledger_has_rid(other)


def test_r1_f5_collect_synthesizes_missing_and_cancelled_rows(tmp_ledger, tmp_results,
                                                              monkeypatch):
    monkeypatch.setattr(R, "accepted", lambda stage, j: {})
    clock = FakeClock()
    _smoke_pass(clock=clock)
    # block 1 completes with one artifact missing (twice); block 2 is cancelled at the deadline
    slow = {}

    def dur(job):
        return 10 ** 6 if job.batch_id == slow.setdefault("bid", job.batch_id) else 600

    ex = FakeExecutor(clock, duration_fn=dur,
                      behaviour=lambda j, s: "missing" if s.context == "safety" else "ok")
    r = _runner(clock=clock, ex=ex, n_tasks=2, repeats=1, max_in_flight=2)
    r.queue = R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1)
    r.retrying = True
    while r.queue or len(r.running) > 1:
        r.harvest()
        while r.queue and len(r.running) < 2:
            r.admit(r.queue.pop(0), C.RunKind.MAIN)
        clock.sleep(60)
    r.shutdown("deadline")
    r.close()
    R.collect(G)
    rows = _rows(G)
    main = [x for x in rows.values() if x["kind"] == "main"]
    canc = [x for x in main if x["excluded_reason"] == "cancelled"]
    assert len(canc) == 6 and all(x["analysis_role"] == "excluded" for x in canc)
    assert all(x["header"] and x["task_id"] and x["block_id"] == slow["bid"] for x in canc)
    miss = [x for x in main if x["excluded_reason"] == "missing"]
    assert len(miss) == 1 and miss[0]["header"] == "safety" and miss[0]["attempt"] == "2"
    sup = [x for x in main if x["excluded_reason"] == "superseded"]
    assert len(sup) == 6                                  # attempt 1, incl. its missing row
    assert len(main) == 6 + 6 + 6 and sum(x["analysis_role"] == "main" for x in main) == 5
    cov = json.loads(C.episodes_csv(G).with_suffix(".coverage.json").read_text())
    assert cov["synthesized_missing"] == 6 + 2


def test_r1_f6_interim_cohort_is_the_first_ten_admitted(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    first3: list[str] = []

    def dur(job):
        if len(first3) < 3 and job.attempt == 1:
            first3.append(job.batch_id)
        return 5000 if job.batch_id in first3 else 600

    ex = FakeExecutor(clock, duration_fn=dur,
                      behaviour=lambda j, s: "api_error" if j.batch_id in first3 else "ok")
    r = _runner(clock=clock, ex=ex, n_tasks=13, repeats=1, max_in_flight=13)
    out = r.run()
    r.close()
    interim = next(x for x in R.manifest_records(G) if x["kind"] == "interim")
    admitted = []
    for x in R.manifest_records(G):
        if x["kind"] == "admit" and x["run_kind"] == "main" and x["batch_id"] not in admitted:
            admitted.append(x["batch_id"])
    assert interim["blocks"] == admitted[:10] and set(first3) <= set(interim["blocks"])
    assert interim["share"] == pytest.approx(18 / 60)
    assert out["reason"] == "interim_completion"


def test_r1_f7_smoke_pass_only_after_a_durable_checkpoint(tmp_ledger, tmp_results,
                                                         monkeypatch):
    clock = FakeClock()
    real_emit = R.emit

    def crash_on_pass(stage, kind, payload, ts=None):
        if kind == C.EventKind.SMOKE_PASS:
            st = R.read_run_state(stage)
            assert st["smoke"]["status"] == "pass" and st["target_repeats"] is not None
            assert st["judge_set_aside_usd"] > 0
            raise SystemExit("crash just before the SMOKE_PASS line")
        return real_emit(stage, kind, payload, ts)

    monkeypatch.setattr(R, "emit", crash_on_pass)
    r = _runner(clock=clock)
    with pytest.raises(SystemExit):
        r.smoke()
    r.close()
    monkeypatch.setattr(R, "emit", real_emit)
    r = _runner(clock=clock)
    out = r.smoke()                                       # completes the checkpoint, no re-run
    r.close()
    assert out.get("recovered") and _events().count("SMOKE_PASS") == 1
    assert sum(x["kind"] == "smoke_run" for x in R.manifest_records(G)) == 1


def test_r1_f7_main_rebuilds_or_refuses_a_missing_checkpoint(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    good = R.read_run_state(G)
    st = dict(good)
    st["judge_set_aside_usd"] = None
    st["smoke"] = {}
    R.write_run_state(G, st)
    r = _runner(clock=clock)
    assert r.rebuild_smoke_checkpoint()
    r.close()
    rebuilt = R.read_run_state(G)
    assert rebuilt["judge_set_aside_usd"] == pytest.approx(good["judge_set_aside_usd"])
    assert rebuilt["target_repeats"] == good["target_repeats"]
    # with the smoke artifacts gone, main admission is refused
    R.write_run_state(G, st)
    for p in C.episodes_dir(G).rglob("*.json"):
        p.unlink()
    r = _runner(clock=clock)
    with pytest.raises(R.RunRefused, match="checkpoint"):
        r.run()
    r.close()


def test_r1_f8_resume_replays_a_failing_interim_decision(tmp_ledger, tmp_results,
                                                        monkeypatch):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    ex = FakeExecutor(clock, behaviour=lambda j, s: "api_error"
                      if s.context in ("safety", "neutral") else "ok")
    r = _runner(clock=clock, ex=ex, n_tasks=12, repeats=1, max_in_flight=4)
    real = R.StageRunner.shutdown
    monkeypatch.setattr(R.StageRunner, "shutdown",
                        lambda self, why: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        r.run()
    r.close()
    monkeypatch.setattr(R.StageRunner, "shutdown", real)
    assert next(x for x in R.manifest_records(G) if x["kind"] == "interim")["share"] > 0.2
    n_admits = sum(x["kind"] == "admit" for x in R.manifest_records(G))
    r2 = _runner(clock=clock, ex=FakeExecutor(clock), n_tasks=12, repeats=1)
    out = r2.run(resume=True)
    r2.close()
    assert out["reason"] == "interim_completion" and out["terminal"]
    assert sum(x["kind"] == "admit" for x in R.manifest_records(G)) == n_admits


def test_r1_f8_restart_during_an_outage_does_not_dispatch(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    r, _ = _outage_runner(clock, test_ok_after=None)
    r.queue = R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1)
    r.retrying = True
    while not r.paused:                                   # run until the outage pause
        r.harvest()
        r.outage_step(clock.now())
        while not r.paused and r.queue and len(r.running) < r.max_in_flight:
            r.admit(r.queue.pop(0), C.RunKind.MAIN)
        clock.sleep(60)
    while r.running:                                      # in-flight blocks finish (failing)
        r.harvest()
        clock.sleep(60)
    r.close()                                             # the runner dies while paused
    n_admits = sum(x["kind"] == "admit" for x in R.manifest_records(G))
    clock.sleep(600)
    r2, calls = _outage_runner(clock, test_ok_after=None)
    out = r2.run(resume=True)
    r2.close()
    assert out["reason"] == "outage"
    assert sum(x["kind"] == "admit" for x in R.manifest_records(G)) == n_admits
    assert calls and calls[0] >= clock.now() - 13 * 3600


def test_r1_f9_billed_failed_attempt_is_not_double_counted():
    from x3.task import ATTEMPTS, call_bound
    one = call_bound(1000, 16000, 0.15, 0.50) / ATTEMPTS
    bounds = [{"turn": 1, "input_bound": 1000, "max_tokens": 16000, "spent": 0.0,
               "failed_charge": one}]
    billed_fail = _call(0.004, error="APIError")
    billed_fail["stop_reason"] = None
    a = _art("a", calls=[billed_fail, _call(0.01)], bounds=bounds)
    assert R.episode_cost(a, G) == pytest.approx(0.014)  # not 0.014 + the ceiling charge
    # a recorded failed attempt with no call record: correspondence unknown -> unavailable
    b = _art("b", calls=[_call(0.01)], bounds=bounds)
    assert R.episode_cost(b, G) is None


def test_r1_f10_incomplete_accounting_is_unavailable_and_sizing_sees_the_charge(
        tmp_ledger, tmp_results):
    bare = _art("a", calls=[], end="api_error", turns=0)
    assert R.episode_cost(bare, G) is None
    usd, _est, _, per = R.settle_detail([bare], [_spec("a")], G)
    assert per == {"a": pytest.approx(0.60)} and usd == pytest.approx(0.60)
    clock = FakeClock()
    _smoke_pass(clock=clock)
    ex = FakeExecutor(clock, behaviour=lambda j, s: "api_error_bare"
                      if s.context == "safety" else "ok")
    r = _runner(clock=clock, ex=ex, n_tasks=1, repeats=1)
    r.run()
    r.close()
    for o in R.outcomes(G):
        assert sum(o["episode_costs"].values()) == pytest.approx(o["billed"])
    first = next(o for o in R.outcomes(G) if o["run_kind"] == "main" and o["attempt"] == 1)
    bad = next(e for e in first["episode_ids"] if "|safety|" in e)
    assert first["episode_costs"][bad] == pytest.approx(0.60)


def _clock_worker(args):
    import os
    root, now = args
    os.environ["X3B_RESULTS_DIR"] = root
    from x3b import contracts as C2
    from x3b import runner as R2
    C2.CLOCK_JSON = C2.Path(root) / "clock.json"
    return R2.ensure_clock(C2.StageKey.GLM, now)


def test_r1_f14_clock_published_once_whole(tmp_results):
    import multiprocessing as mp
    with mp.get_context("spawn").Pool(4) as pool:
        got = pool.map(_clock_worker, [(str(tmp_results), T0 + i) for i in range(8)])
    assert len({json.dumps(g, sort_keys=True) for g in got}) == 1
    assert R.read_clock() == got[0] and not list(tmp_results.glob("clock.*.tmp"))
    assert R.ensure_clock(M, T0 + 999) == got[0]          # never rewritten


def test_r1_f14_partial_clock_is_an_error_not_a_guess(tmp_results):
    tmp_results.mkdir(parents=True, exist_ok=True)
    C.CLOCK_JSON.write_text('{"first_paid')
    with pytest.raises(RuntimeError, match="unreadable"):
        R.read_clock(retries=2, wait_s=0)


# --- code review r2 regressions (logs/reviews/20261006-X3b-code-r2.md) --------------------
def _admit_and_die(clock, n=2, duration=10 ** 6):
    """Admit n main blocks (released, as a real worker would be) and lose the process."""
    ex = FakeExecutor(clock, duration=duration)
    r = _runner(clock=clock, ex=ex, n_tasks=n, repeats=1)
    jobs = [r.admit(b, C.RunKind.MAIN) for b in R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1)]
    r.close()
    return jobs


def test_r2_f1_recovery_failures_are_isolated_and_hold_the_reservation(
        tmp_ledger, tmp_results, monkeypatch):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    j1, j2 = _admit_and_die(clock)
    real = R.remove_compose_projects

    def flaky(tag):
        if tag == j1.task_name:
            raise RuntimeError("docker daemon gone")
        return real(tag)
    monkeypatch.setattr(R, "remove_compose_projects", flaky)
    r = _runner(clock=clock, n_tasks=2, repeats=1)
    errors: list[str] = []
    r.terminate_all(errors)                      # the emergency path covers recovered jobs
    assert any("docker daemon gone" in e for e in errors)
    assert j1.token in r.unconfirmed and j2.token not in r.unconfirmed
    assert {x["token"] for x in R.manifest_records(G) if x["kind"] == "unresolved"} == \
        {j1.token, j2.token}
    r.release_leftovers()                        # never settles an admission's reservation
    from s4world import budget
    assert j1.rid in budget._read_reservations() and j2.rid not in budget._read_reservations()
    out = r.shutdown("global_stop")
    r.close()
    assert out["terminal"] is False and out["admissions_without_outcome"] == [j1.token]
    monkeypatch.setattr(R, "remove_compose_projects", real)
    r2 = _runner(clock=clock, n_tasks=2, repeats=1)
    assert r2.stop_now("global_stop")["terminal"] and _no_outstanding()
    r2.close()


class _Handle:
    def __init__(self, pid, alive=True):
        self.pid, self._alive = pid, alive

    def is_alive(self):
        return self._alive


def test_r2_f2_go_only_after_ready(tmp_path):
    import os
    job = R.Job(token="t" * 12, rid="r", stage=G, kind=C.RunKind.MAIN, specs=[], reserved=0,
                attempt_dir=tmp_path, task_name="x3bready0001", started_ts=0.0,
                handle=_Handle(os.getpid()))
    with pytest.raises(R.WorkerNotReady):
        R.ProcessExecutor().release(job, timeout_s=0.3)
    assert not (tmp_path / R.GO_FILE).exists()
    R._write_json_atomic(tmp_path / R.READY_FILE, {"pid": os.getpid(), "pgid": 1})
    with pytest.raises(R.WorkerNotReady):              # not yet its own group leader
        R.ProcessExecutor().release(job, timeout_s=0.3)
    R._write_json_atomic(tmp_path / R.READY_FILE, {"pid": os.getpid(), "pgid": os.getpid()})
    R.ProcessExecutor().release(job, timeout_s=0.3)
    assert (tmp_path / R.GO_FILE).exists()


def test_r2_f2_abort_wins_over_go_in_a_real_worker(tmp_results):
    import time
    block = R.plan_stage(G, C.RunKind.MAIN, _tasks(1), repeats=1, model_id=R.MOCK_MODEL)[0]
    d = tmp_results / "glm" / "logs" / "abortgo"
    d.mkdir(parents=True)
    (d / R.GO_FILE).write_text("go")
    (d / R.ABORT_FILE).write_text("abort")
    job = R.Job(token="a" * 12, rid="r", stage=G, kind=C.RunKind.MAIN, specs=block,
                reserved=0.0, attempt_dir=d, task_name="x3babortgo01", started_ts=0.0)
    h = R.ProcessExecutor().start(job)
    h.join(60)
    assert not h.is_alive()
    ready = json.loads((d / R.READY_FILE).read_text())
    assert ready["pid"] == ready["pgid"] == h.pid
    res = json.loads((d / R.RESULT_JSON).read_text())
    assert res["ok"] is False and not list(d.glob("*.eval"))
    del time


class _NotReadyExecutor(FakeExecutor):
    def release(self, job):
        raise R.WorkerNotReady("no READY")


def test_r2_f2_unready_worker_is_aborted_and_settled_only_when_confirmed(
        tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    r = _runner(clock=clock, ex=_NotReadyExecutor(clock), n_tasks=1, repeats=1)
    job = r.admit(R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1)[0], C.RunKind.MAIN)
    r.close()
    assert (job.attempt_dir / R.ABORT_FILE).exists() and not (job.attempt_dir / R.GO_FILE).exists()
    o = next(o for o in R.outcomes(G) if o["token"] == job.token)
    assert o["billed"] == 0 and "never released" in o["notes"][0] and _no_outstanding()


def test_r2_f2_go_without_ready_or_unidentified_pid_stays_unresolved(
        tmp_ledger, tmp_results, monkeypatch):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    j1, j2 = _admit_and_die(clock)
    (j1.attempt_dir / R.READY_FILE).unlink()           # GO but no READY pid
    R._write_json_atomic(j2.attempt_dir / R.READY_FILE, {"pid": 777777, "pgid": 777777})
    monkeypatch.setattr(R, "worker_state", lambda pid: "dead" if pid == DEAD_PID
                        else "unresolved")             # e.g. ps failed for 777777
    r = _runner(clock=clock, n_tasks=2, repeats=1)
    r.reconcile()
    assert set(r.unconfirmed) == {j1.token, j2.token} and not _no_outstanding()
    assert r.shutdown("global_stop")["terminal"] is False
    r.close()


def test_r2_f2_worker_state_is_positive_only():
    import os
    assert R.worker_state(DEAD_PID) == "dead"
    assert R.worker_state(None) == "unresolved"
    assert R.worker_state(os.getpid()) in ("unresolved",)    # alive, not a pool worker


def test_r2_f3_failed_attempt_correspondence_must_be_exact():
    att = [{"kind": "transport_error", "input_bound": 100, "max_tokens": 4000,
            "charge": 0.020, "usage": None, "error": "E"},
           {"kind": "transport_error", "input_bound": 100, "max_tokens": 4000,
            "charge": 0.010, "usage": None, "error": "E"},
           {"kind": "ok", "input_bound": 100, "max_tokens": 4000, "charge": 0.001,
            "usage": {}, "error": None}]
    second = _call(0.005, "pre_probe", error="E")
    second["stop_reason"] = None
    calls = [_call(0.01), second, _call(0.002, "pre_probe"), _call(0.003, "post_probe")]
    a = _art("a", calls=calls, pre=_probe(attempts=att))
    assert R.episode_cost(a, G) is None                 # never 0.015
    assert R.settle_cost([a], [_spec("a")], G)[0] == pytest.approx(0.60)


def test_r2_f4_rerun_allowance_covers_outage_test_calls(tmp_ledger, tmp_results):
    clock = FakeClock()
    r = _runner(clock=clock, test_call=lambda st: {"ok": False, "billed_usd": 1.0,
                                                   "error": "503"})
    r._append({"kind": "smoke_run", "n": 2, "allowance_usd": 3.0})
    r.smoke_allowance = C.SMOKE_FIX_RERUN_USD
    for _ in range(3):
        assert r.do_test_call() is False and not r.test_refused_allowance
    assert r._allowance_left(2) == pytest.approx(0.0)
    assert r.do_test_call() is False and r.test_refused_allowance      # does not fit: refused
    tr = [x for x in R.manifest_records(G) if x["kind"] == "test_result"]
    assert [x["status"] for x in tr] == ["error"] * 3 + ["refused_allowance"]
    block = R.with_attempt(R.plan_stage(G, C.RunKind.SMOKE, r.tasks)[0], 2)
    assert r._fit_allowance(block) is None
    r.paused, r.last_test_ts = True, None
    r.last_success = clock.now() - 3600
    assert r.outage_step(clock.now()) == "smoke_failed"
    r.close()


def test_r2_f5_sizing_state_is_written_before_its_record(tmp_ledger, tmp_results,
                                                         monkeypatch):
    r = _runner(G, n_tasks=15, repeats=None)
    r.state["smoke"] = {"judge_per_episode_usd": 0.0}
    real = r._append

    def crash(rec):
        if rec.get("kind") == "sizing":
            raise SystemExit("crash between the state and the sizing record")
        return real(rec)
    r._append = crash
    with pytest.raises(SystemExit):
        r.sizing("interim", [0.064] * 6)
    r.close()
    assert R.read_run_state(G)["target_repeats"] == 6     # the cut survived the crash


def test_r2_f5_resume_replays_the_last_recorded_sizing(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    st = R.read_run_state(G)
    R.append_manifest(G, {"kind": "sizing", "point": "interim", "from": 2, "to": 1,
                          "c_usd": 1.0, "judge_set_aside_usd": 0.5, "utc": R.utc(T0)})
    st["target_repeats"] = 2                              # an older state, before the cut
    R.write_run_state(G, st)
    r = _runner(clock=clock, n_tasks=2, repeats=2)
    out = r.run()
    r.close()
    assert out["reason"] == "completed"
    assert R.read_run_state(G)["target_repeats"] == 1
    assert {o["batch_id"][-2:] for o in R.outcomes(G) if o["run_kind"] == "main"} == {"r1"}


def test_r2_f6_terminal_waits_for_every_admission_outcome(tmp_ledger, tmp_results,
                                                          monkeypatch):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    (j1,) = _admit_and_die(clock, n=1, duration=60)
    from s4world import budget
    budget.settle(j1.rid, 0.2, "m", 6, f"X3b-GLM main x a1 rid={j1.rid}; estimated $0.0000")
    assert _no_outstanding()                             # ledger settled, outcome missing
    real = R.StageRunner._recover_one
    monkeypatch.setattr(R.StageRunner, "_recover_one",
                        lambda self, a: (_ for _ in ()).throw(OSError("disk")))
    r = _runner(clock=clock, n_tasks=1, repeats=1)
    out = r.stop_now("global_stop")
    r.close()
    assert out["terminal"] is False and out["admissions_without_outcome"] == [j1.token]
    monkeypatch.setattr(R.StageRunner, "_recover_one", real)   # never undo(): fixtures too
    r2 = _runner(clock=clock, n_tasks=1, repeats=1)
    out2 = r2.stop_now("global_stop")
    r2.close()
    assert out2["terminal"]
    assert [o["token"] for o in R.outcomes(G) if o["run_kind"] == "main"] == [j1.token]
    assert len([e for e in _ledger(tmp_ledger) if j1.rid in e["note"]]) == 1   # not twice


def test_r2_f8_twelve_hours_without_success_stops_before_any_test_or_dispatch(
        tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    _admit_and_die(clock, n=1, duration=60)
    clock.sleep(13 * 3600)
    calls = []
    r = _runner(clock=clock, n_tasks=2, repeats=1,
                test_call=lambda st: calls.append(1) or {"ok": True, "billed_usd": 0.0})
    n_admits = sum(x["kind"] == "admit" for x in R.manifest_records(G))
    out = r.run(resume=True)
    r.close()
    assert out["reason"] == "outage" and out["terminal"] and calls == []
    assert sum(x["kind"] == "admit" for x in R.manifest_records(G)) == n_admits
    assert "RESUME" not in _events()


# --- code review r3 regressions (logs/reviews/20261006-X3b-code-r3.md) --------------------
def _running_with_samples(clock, n=1):
    """Main blocks in flight whose workers already produced recoverable completed samples."""
    ex = FakeExecutor(clock, duration=10 ** 6)
    r = _runner(clock=clock, ex=ex, n_tasks=n, repeats=1)
    jobs = [r.admit(b, C.RunKind.MAIN) for b in R.plan_stage(G, C.RunKind.MAIN, r.tasks, 1)]
    for j in jobs:
        ex._write(j)                      # artifacts + result.json, worker still "running"
    return r, ex, jobs


def test_r3_f1_crash_after_cancel_settlement_recovers_as_cancelled(
        tmp_ledger, tmp_results, monkeypatch):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    r, ex, (job,) = _running_with_samples(clock)
    real = r._append

    def no_outcome(rec):
        if rec.get("kind") == "outcome":
            raise OSError("disk full: outcome not written")
        return real(rec)
    r._append = no_outcome
    out = r.shutdown("deadline")                         # kill + ledger settle, no outcome
    r.close()
    assert out["terminal"] is False and len(ex.killed) == 1
    led = [e for e in _ledger(tmp_ledger) if job.rid in (e.get("rid") or "")]
    assert len(led) == 1 and led[0]["usd"] == pytest.approx(3.60)
    assert [x["token"] for x in R.manifest_records(G) if x["kind"] == "cancel_intent"] == \
        [job.token]
    r2 = _runner(clock=clock, n_tasks=1, repeats=1)
    out2 = r2.stop_now("deadline")
    r2.close()
    assert out2["terminal"]
    o = next(o for o in R.outcomes(G) if o["token"] == job.token)
    assert o["cancelled"] and o["billed"] == pytest.approx(3.60)       # never the 0.084
    assert o["estimated"] == pytest.approx(3.60)
    assert sum(o["episode_costs"].values()) == pytest.approx(3.60)
    assert len([e for e in _ledger(tmp_ledger) if job.rid in (e.get("rid") or "")]) == 1
    monkeypatch.setattr(R, "accepted", lambda stage, j: {})
    R.collect(G)
    rows = [x for x in _rows(G).values() if x["block_id"] == job.batch_id]
    assert len(rows) == 6 and all(x["excluded_reason"] == "cancelled" for x in rows)


def test_r3_f1_crash_after_cancel_intent_before_kill(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    r, ex, (job,) = _running_with_samples(clock)

    def die(h, grace_s=0):
        raise SystemExit("process killed between cancel_intent and the kill")
    ex.kill = die
    with pytest.raises(SystemExit):
        r.shutdown("deadline")
    r.close()
    assert not [o for o in R.outcomes(G) if o["token"] == job.token]
    r2 = _runner(clock=clock, n_tasks=1, repeats=1)
    out = r2.stop_now("deadline")                         # confirms dead, records cancelled
    r2.close()
    assert out["terminal"] and _no_outstanding()
    o = next(o for o in R.outcomes(G) if o["token"] == job.token)
    assert o["cancelled"] and o["billed"] == pytest.approx(3.60) and o["artifacts"] == []


def test_r3_f2_first_run_long_after_smoke_stops_for_outage(tmp_ledger, tmp_results):
    clock = FakeClock()
    _smoke_pass(clock=clock)
    clock.sleep(13 * 3600)
    calls = []
    r = _runner(clock=clock, test_call=lambda st: calls.append(1) or {"ok": True,
                                                                     "billed_usd": 0.0})
    out = r.run()                                          # plain run, not --resume
    r.close()
    assert out["reason"] == "outage" and out["terminal"] and calls == []
    assert not [x for x in R.manifest_records(G) if x["kind"] == "admit"
                and x["run_kind"] == "main"]


def test_r3_f2_smoke_rerun_after_a_long_idle_stops_for_outage(tmp_ledger, tmp_results):
    clock = FakeClock()
    r = _runner(clock=clock, ex=FakeExecutor(clock, cost=0.06))
    assert r.smoke()["reason"] == "smoke_failed"
    r.close()
    clock.sleep(13 * 3600)
    calls = []
    r = _runner(clock=clock, test_call=lambda st: calls.append(1) or {"ok": True,
                                                                     "billed_usd": 0.0})
    out = r.smoke()
    r.close()
    assert out["reason"] == "outage" and out["terminal"] and calls == []
    assert sum(x["kind"] == "admit" for x in R.manifest_records(G)) == 1
