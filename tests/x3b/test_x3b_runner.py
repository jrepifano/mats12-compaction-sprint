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
                 unkillable=False):
        self.clock, self.duration, self.cost = clock, duration, cost
        self.behaviour = behaviour or (lambda job, sp: "ok")
        self.progress, self.unkillable = progress, unkillable
        self.started, self.killed = [], []

    def start(self, job):
        self.started.append(job)
        return {"job": job, "end": self.clock.now() + self.duration, "done": False,
                "killed": False}

    def pid(self, h):
        return None

    def _write(self, job):
        paths = []
        now = _iso_ts(self.clock.now())
        for sp in job.specs:
            how = self.behaviour(job, sp)
            if how == "missing":
                continue
            kw = {"stage": job.stage, "kind": job.kind.value, "header": sp.context, "task": sp.task_id,
                      "repeat": sp.repeat, "attempt": sp.attempt, "batch": sp.batch_id}
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
    clock.sleep(20 * 3600)
    r2 = _runner(clock=clock, ex=FakeExecutor(clock, duration=60, progress=True), paid=True,
                 n_tasks=2, repeats=1)
    assert r2.deadline_ts() == T0 + 30 * 3600
    out = r2.run(resume=True)
    r2.close()
    assert C.CLOCK_JSON.read_text() == first and out["reason"] == "completed"


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
    assert o2["failed"] and o2["estimated"] == pytest.approx(3.60)   # no result: full
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
    eps = sorted(o["episode_ids"][0][-2:] for o in R.outcomes(G))
    assert eps == ["a1", "a2"]


def test_smoke_rerun_refused_past_the_fix_allowance(tmp_ledger, tmp_results):
    clock = FakeClock()
    ex = FakeExecutor(clock, cost=0.40)                 # first smoke charges 6 x 0.56 = 3.36
    r = _runner(clock=clock, ex=ex)
    r.smoke()
    r.close()
    r = _runner(clock=clock, ex=ex)
    with pytest.raises(R.RunRefused, match="re-run would pass"):
        r.smoke()
    r.close()


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
