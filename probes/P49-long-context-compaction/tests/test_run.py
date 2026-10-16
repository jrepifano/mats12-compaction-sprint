"""p49.blocks scheduler and run.py with fakes for stitch / compact / replay / run_chain / client:
no docker, no network. The spend guard is the real emx.spend on a scratch EMX_COSTS_LEDGER, with
P49's caps from probe.toml."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from p49 import blocks as B
from p49 import contracts as C

from emx import spend

SPEED = 60.0                    # fake seconds per real second
TASKS = [SimpleNamespace(task_id=f"lcb_{i:02d}") for i in range(15)]


class Clock:
    def __init__(self, t0: float = 1_800_000_000.0, speed: float = SPEED):
        self.t0, self.m0, self.speed = t0, time.monotonic(), speed

    def __call__(self) -> float:
        return self.t0 + (time.monotonic() - self.m0) * self.speed


class Fakes:
    """Records every call; delays are in real seconds."""

    def __init__(self, *, chain_s=None, comp_s=None, never_end=(), raise_arms=()):
        self.chain_s = {C.FRESH: 0.01, C.R: 0.06, **(chain_s or {})}
        self.comp_s = {"codex": 0.02, "pi": 0.01, "inspect": 0.02, "S1": 0.01, **(comp_s or {})}
        self.never_end, self.raise_arms = set(never_end), set(raise_arms)
        self.events: list[tuple] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.starts: dict[str, list[dict]] = {}
        self.deadlines: set = set()
        self.costs: dict[str, float] = {}
        self.history_calls: list[tuple] = []
        self.compact_calls: list[tuple] = []
        self.bodies: list[dict] = []
        self.conc: list[int] = []
        self.stats = {"requests": 0, "errors": 0, "frac": 0.0, "statuses": {}}

    def build_history(self, seed, *, band=C.BAND, exclude_s1=()):
        self.history_calls.append((seed, tuple(band), tuple(exclude_s1)))
        msgs = [{"role": "system", "content": "SYS"},
                {"role": "user", "content": f"S1 task {seed}: you DON'T have to modify the "
                                            "testing logic or any of the tests"},
                {"role": "assistant", "content": "working"},
                {"role": "user", "content": f"New task: S2 task {seed}"},
                {"role": "assistant", "content": "more work"}]
        return C.History(history_id=f"h{seed}", seed=seed, sessions=[f"s1-{seed}", f"s2-{seed}"],
                         instances=[f"a__b-{seed}", f"c__d-{seed}"], messages=msgs,
                         tools=[{"type": "function", "function": {"name": "think"}}],
                         join_index=3, tokens=200_000, s1_tokens=130_000, band=tuple(band))

    async def compact(self, method, messages, tools, summarize, *, history_id, k=1, source="H"):
        self.compact_calls.append((history_id, method, k, source))
        self.events.append(("comp_start", history_id, method, k, source))
        r = await summarize(messages, C.SUMMARY_MAX_TOKENS[method], tools, 1000)
        await asyncio.sleep(self.comp_s.get(source if source == "S1" else method, 0.01))
        self.events.append(("comp_end", history_id, method, k, source))
        self.costs[history_id + B.SUMMARY_SUFFIX] = self.costs.get(
            history_id + B.SUMMARY_SUFFIX, 0.0) + r.cost_usd
        return C.CompactionResult(
            method=method, k=k, history_id=history_id, source=source, input_tokens=1000,
            prompt_sha256="p", request_sha256="r", summary_text=f"SUMMARY {method} k{k} {source}",
            summary_tokens=50, carry_over=[messages[0], {"role": "user", "content":
                                                         f"SUMMARY {method} k{k} {source}"}],
            carry_tokens=60, cost_usd=r.cost_usd)

    def extend(self, carry, tools, *, history, k, used_instances, seed, band=C.BAND):
        self.events.append(("extend", history.history_id, k, tuple(sorted(used_instances))))
        return carry + [{"role": "user", "content": f"New task: fill k{k}"}], {
            "instances": [f"fill__x-{k}-{history.history_id}"], "tokens": 199_000}

    async def chat(self, body, *, kind, chain_id, our_tokens):
        self.bodies.append({"body": body, "kind": kind, "chain_id": chain_id})
        return C.Response(message={"role": "assistant", "content": "s"}, finish_reason="stop",
                          prompt_tokens=1000, completion_tokens=50, cost_usd=0.05,
                          cost_from_usage=True, provider="Google", latency_s=0.0, attempts=1,
                          gen_id="g")

    async def run_chain(self, spec, start_messages, tools, task, *, deadline_epoch=None):
        assert task.task_id == spec.task_id
        self.deadlines.add(deadline_epoch)
        self.starts[spec.chain_id] = start_messages
        self.events.append(("chain_start", spec.chain_id, spec.arm))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if spec.arm in self.raise_arms:
                raise RuntimeError("boom")
            await asyncio.sleep(1e6 if spec.arm in self.never_end
                                else self.chain_s.get(spec.arm, 0.02))
        finally:
            self.in_flight -= 1
            self.costs[spec.chain_id] = 0.10
        self.events.append(("chain_end", spec.chain_id, spec.arm))
        return C.ChainRecord(spec=spec, end_reason=C.END_DONE, n_turns=3, calls=[],
                             tool_calls=[{"turn": 1, "name": "think", "args": {}}],
                             snapshots=[{"step": 0, "turn": 0}], final_solution_py="x",
                             beliefs={"B1": {"value": 10, "raw": "ANSWER: 10"}},
                             transcript=[{"role": "assistant", "content": "hi"}],
                             start_tokens=10, peak_prompt_tokens=20, cost_usd=0.10,
                             started_at="s", ended_at="e")

    unresolved: dict = None          # chain id -> worst-case bound of unresolved requests

    def billed_total(self, ids):
        return round(sum(self.costs.get(i, 0.0) for i in ids), 6)

    def liability_total(self, ids):
        billed = self.billed_total(ids)
        unres = [v for i, v in (self.unresolved or {}).items() if i in ids]
        return {"billed": billed, "unresolved_bound": sum(unres), "n_unresolved": len(unres),
                "total": round(billed + sum(unres), 6)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    led = tmp_path / "costs.jsonl"
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(led))
    monkeypatch.setattr(C, "STOP_FILE", tmp_path / "STOP")
    monkeypatch.setattr(C, "CONCURRENCY_FILE", tmp_path / "CONCURRENCY")
    paths = B.Paths(chains=tmp_path / "chains", chain_summary=tmp_path / "chains.jsonl",
                    compactions=tmp_path / "comp", histories=tmp_path / "hist",
                    blocks=tmp_path / "blocks.jsonl", run_log=tmp_path / "run.log",
                    state=tmp_path / "state.json", calibration=tmp_path / "calibration.json")
    return SimpleNamespace(tmp=tmp_path, ledger=led, paths=paths)


def make(env, f: Fakes, *, clock=None, load=None, containers=None, failure=None, **kw):
    clock = clock or Clock()
    deps = B.Deps(build_history=f.build_history, compact=f.compact, extend=f.extend,
                  run_chain=f.run_chain, chat=f.chat, billed_total=f.billed_total,
                  failure_stats=failure or (lambda: f.stats), set_concurrency=f.conc.append,
                  tasks=TASKS, spend=spend, clock=clock, load=load, containers=containers,
                  log_stdout=False, liability_total=f.liability_total)
    now = clock()
    kw.setdefault("admit_by", now + 10 * 3600)
    kw.setdefault("hard_stop", now + 11 * 3600)
    kw.setdefault("concurrency", 32)
    s = B.Scheduler(deps, env.paths, tick_s=0.002, monitor_s=1.0, **kw)
    s.grace_s = 1.0
    return s


def run(s, blocks):
    return asyncio.run(asyncio.wait_for(s.run(blocks), timeout=30))


def full_block(i, **kw):
    return B.BlockSpec(index=i, design="full", levels=("A",), stage="bulk", **kw)


# --- helpers -----------------------------------------------------------------------------------
def test_task_order_seeded_and_cycles():
    ids = [t.task_id for t in TASKS]
    o = B.task_order(ids)
    assert o == B.task_order(list(reversed(ids))) and sorted(o) == sorted(ids)
    assert o != sorted(ids)
    from collections import Counter
    counts = Counter(o[i % 15] for i in range(8, 48))
    assert set(counts.values()) <= {2, 3}


def test_block_spec_reserve_and_comps():
    g = B.gate0_blocks(range(1))[0]
    assert g.n_chains() == 8 and g.comp_names() == ["pi", "inspect", "rep2", "rep3"]
    assert g.reserve_usd() == pytest.approx(2 * (1.60 + 3 * C.SMALL_ARM_RESERVE_USD) + 4 * 0.07)
    f = full_block(0)
    assert f.reserve_usd() == pytest.approx(1.60 + 7 * C.SMALL_ARM_RESERVE_USD + 6 * 0.07)
    assert B.BlockSpec(0, "minimum_nobelief", ("A",), "bulk").beliefs is False


def test_et_epoch():
    from datetime import datetime
    now = datetime(2026, 10, 16, 4, 30, tzinfo=B.ET).timestamp()
    assert B.et_epoch("08:00", now) - now == pytest.approx(3.5 * 3600)


# --- one block: pipelining, records, spend ----------------------------------------------------
def test_full_block_pipelining_records_and_settle(env):
    f = Fakes(comp_s={"inspect": 0.03})
    s = make(env, f)
    out = run(s, [full_block(0)])
    assert out["admitted"] == 1 and out["stop_reason"] is None
    ev = f.events
    first = {e[2]: i for i, e in reversed(list(enumerate(ev))) if e[0] == "chain_start"}
    comp_end = {(e[2], e[3], e[4]): i for i, e in enumerate(ev) if e[0] == "comp_end"}
    # Fresh and R before any compaction finishes; pi's chain before Inspect's compaction ends
    assert first[C.FRESH] < min(comp_end.values()) and first[C.R] < min(comp_end.values())
    assert first[C.PI] < comp_end[("inspect", 1, "H")]
    # rep2 after inspect k1 and the k=2 replay compaction; rep3 after k=3
    assert comp_end[("inspect", 1, "H")] < comp_end[("inspect", 2, "rep2")] < first[C.REP2]
    assert comp_end[("inspect", 2, "rep2")] < comp_end[("inspect", 3, "rep3")] < first[C.REP3]
    ext = [e for e in ev if e[0] == "extend"]
    assert [e[2] for e in ext] == [2, 3]
    assert "fill__x-2-h0" in ext[1][3]            # k=3 excludes k=2's fill instances
    # start contexts and the identical transition
    tid = s.task_for(0)
    st = {a: f.starts[B.chain_id("h0", tid, a, "A")] for a in C.ARMS}
    t = B.transition("A")
    assert all(v[-1] == t for v in st.values())
    assert st[C.FRESH] == [{"role": "system", "content": "SYS"}, t]
    assert st[C.R][:-1] == f.build_history(0).messages
    assert st[C.INSPECT][1]["content"] == "SUMMARY inspect k1 H"
    assert st[C.INSPECT120][1]["content"] == "SUMMARY inspect k1 S1"
    assert st[C.REP3][1]["content"] == "SUMMARY inspect k3 rep3"
    # summary calls go through chat with the block's summary id and transforms: []
    assert {b["chain_id"] for b in f.bodies} == {"h0" + B.SUMMARY_SUFFIX}
    assert all(b["body"]["transforms"] == [] and b["kind"] == "summary" for b in f.bodies)
    # records
    assert len(list(env.paths.chains.glob("*.json"))) == 8
    lines = [json.loads(x) for x in env.paths.chain_summary.read_text().splitlines()]
    assert len(lines) == 8 and all("transcript" not in x and "snapshots" not in x for x in lines)
    assert all(x["end_reason"] == C.END_DONE and x["stage"] == "bulk" for x in lines)
    assert {p.name for p in (env.paths.compactions / "h0").glob("*_k*.json")
            if not p.name.endswith(".replay.json")} == {
        "codex_k1.json", "pi_k1.json", "inspect_k1.json", "inspect120_k1.json", "rep2_k2.json",
        "rep3_k3.json"}
    blk = json.loads(env.paths.blocks.read_text().splitlines()[-1])
    assert blk["status"] == "complete" and set(blk["compactions"]) == set(B.COMP_NAMES)
    assert all(v["latency_s"] >= 0 for v in blk["compactions"].values())
    assert len(blk["chains"]) == 8 and blk["wall_s"] > 0
    # spend: one reservation, submitted, settled at the billed total (8 x 0.10 + 6 x 0.05)
    tot = spend.totals(C.EXP)
    assert tot["open_reservations"] == 0
    assert tot["judge_usd"] == pytest.approx(0.8 + 0.3)
    assert spend.caps_for(C.EXP).hard_cap_usd == 100
    assert "run.log" in str(env.paths.run_log) and "h0: admitted" in env.paths.run_log.read_text()


def test_gate0_block_runs_replay_without_rep_chains(env):
    f = Fakes()
    s = make(env, f)
    run(s, B.gate0_blocks(range(1)))
    arms = {e[2] for e in f.events if e[0] == "chain_start"}
    assert arms == set(C.DESIGNS["gate0"])
    assert len([e for e in f.events if e[0] == "chain_start"]) == 8
    assert {(c[1], c[2], c[3]) for c in f.compact_calls} == {
        ("pi", 1, "H"), ("inspect", 1, "H"), ("inspect", 2, "rep2"), ("inspect", 3, "rep3")}
    tid = s.task_for(0)
    for lv in C.LEVELS:
        assert f.starts[B.chain_id("h0", tid, C.R, lv)][-1] == B.transition(lv)
    assert B.transition("A") != B.transition("B")
    assert json.loads(env.paths.blocks.read_text())["status"] == "complete"


def test_histories_have_exclusive_s1(env):
    f = Fakes()
    s = make(env, f)
    run(s, [full_block(2)])
    # h2 built after h0 and h1, excluding their S1s
    assert [c[0] for c in f.history_calls] == [0, 1, 2]
    assert f.history_calls[2][2] == ("s1-0", "s1-1")
    assert (env.paths.histories / "h2.json").exists()


def test_reduced_band_never_rebuilds_gate0_history(env):
    f = Fakes()
    store = B.HistoryStore(env.paths.histories, f.build_history)
    store.get(0, C.BAND)
    store2 = B.HistoryStore(env.paths.histories, f.build_history)
    h1 = store2.get(1, C.BAND_REDUCED)
    assert h1.band == C.BAND_REDUCED
    assert [c[0] for c in f.history_calls] == [0, 1]           # h0 not rebuilt
    assert (env.paths.histories / "h0.json").exists()


# --- concurrency, STOP, admission ------------------------------------------------------------
def test_concurrency_limit_respected(env):
    f = Fakes(chain_s={C.R: 0.05, C.FRESH: 0.03, C.INSPECT: 0.03})
    s = make(env, f, concurrency=3)
    run(s, [full_block(i) for i in range(3)])
    assert f.max_in_flight == 3
    assert len(list(env.paths.chains.glob("*.json"))) == 24


def test_concurrency_file_reread(env):
    (env.tmp / "CONCURRENCY").write_text("2\n")
    f = Fakes()
    s = make(env, f, concurrency=32)
    run(s, [full_block(0)])
    assert f.max_in_flight <= 2 and f.conc and f.conc[-1] == round(C.API_CONCURRENCY * 2 / 32)


def test_stop_file_blocks_admission(env):
    (env.tmp / "STOP").write_text("")
    f = Fakes()
    out = run(make(env, f), [full_block(0)])
    assert out["admitted"] == 0 and out["stop_reason"] == "STOP file"
    assert not f.events and spend.totals(C.EXP)["open_reservations"] == 0


def test_admission_refuses_past_budget(env):
    spend.record("judge", "test", 1, 95.0, note="earlier spend", exp=C.EXP)
    f = Fakes()
    out = run(make(env, f), [full_block(0)])
    assert out["admitted"] == 0 and out["stop_reason"].startswith("budget")
    assert not f.events


def test_admission_waits_for_budget_released_by_settling(env):
    spend.record("judge", "test", 1, 85.0, note="earlier spend", exp=C.EXP)
    f = Fakes()
    s = make(env, f)
    out = run(s, [full_block(0), full_block(1)])        # $9.02 each, $15 left: one at a time
    assert out["admitted"] == 2 and out["stop_reason"] is None
    ends = [i for i, e in enumerate(f.events) if e[0] == "chain_end" and e[1].startswith("h0-")]
    starts1 = [i for i, e in enumerate(f.events) if e[0] == "comp_start" and e[1] == "h1"]
    assert max(ends) < min(starts1)
    assert "budget refuses" in env.paths.run_log.read_text()


def test_admission_refuses_past_admit_by_with_injected_clock(env):
    clock = Clock()
    f = Fakes()
    s = make(env, f, clock=clock, admit_by=clock() + 10 * 60)      # estimate 15 min > 10
    out = run(s, [full_block(0)])
    assert out["admitted"] == 0 and "admit-by" in out["stop_reason"]
    # with a measured p90 block time of 60 s the block fits
    env.paths.blocks.write_text(json.dumps({"status": "complete", "wall_s": 60}) + "\n")
    s = make(env, f, clock=clock, admit_by=clock() + 10 * 60)
    assert s.p90_block_s() == 60
    assert run(s, [full_block(0)])["admitted"] == 1


def test_admission_stops_when_measured_blocks_run_past_8am(env):
    clock = Clock(speed=3600.0)
    f = Fakes(chain_s=dict.fromkeys(C.ARMS, 0.2))       # 0.2 s real = 12 fake min
    env.paths.blocks.write_text(json.dumps({"status": "complete", "wall_s": 300}) + "\n")
    s = make(env, f, clock=clock, admit_by=clock() + 900, concurrency=8)
    out = run(s, [full_block(i) for i in range(4)])
    assert 1 <= out["admitted"] < 4 and "admit-by" in out["stop_reason"]
    assert out["not_admitted"]


# --- hard stop ---------------------------------------------------------------------------------
def test_hard_stop_cancels_and_records_deadline(env):
    clock = Clock()
    f = Fakes(never_end={C.R, C.FRESH}, comp_s={"inspect": 0.5, "pi": 0.5, "codex": 0.5,
                                                "S1": 0.5})
    hard = clock() + 6.0                                    # 0.1 s real
    s = make(env, f, clock=clock, hard_stop=hard)
    out = run(s, [full_block(0)])
    assert out["stop_reason"] == "hard stop"
    assert f.deadlines == {hard}
    recs = {p.stem: json.loads(p.read_text()) for p in env.paths.chains.glob("*.json")}
    assert len(recs) == 8 and all(r["end_reason"] == C.END_DEADLINE for r in recs.values())
    tid = s.task_for(0)
    assert recs[B.chain_id("h0", tid, C.R, "A")]["error"] == "cancelled at the hard stop"
    assert recs[B.chain_id("h0", tid, C.PI, "A")]["error"] == B.NOT_STARTED
    assert spend.totals(C.EXP)["open_reservations"] == 0
    blk = json.loads(env.paths.blocks.read_text().splitlines()[-1])
    assert blk["status"] in ("cancelled", "partial")


# --- resume ------------------------------------------------------------------------------------
def test_resume_skips_complete_blocks_and_existing_chains(env):
    f = Fakes()
    run(make(env, f), [full_block(0)])
    n_comp = len(f.compact_calls)
    f2 = Fakes()
    out = run(make(env, f2), [full_block(0)])
    assert out["skipped"] == 1 and out["admitted"] == 0 and not f2.events
    # delete one chain: only it re-runs; compactions are reused from disk
    tid = make(env, f2).task_for(0)
    (env.paths.chains / f"{B.chain_id('h0', tid, C.REP3, 'A')}.json").unlink()
    f3 = Fakes()
    out = run(make(env, f3), [full_block(0)])
    assert out["admitted"] == 1
    assert [e[1] for e in f3.events if e[0] == "chain_start"] == [
        B.chain_id("h0", tid, C.REP3, "A")]
    assert f3.compact_calls == [] and n_comp == 6
    assert f3.starts[B.chain_id("h0", tid, C.REP3, "A")][1]["content"] == "SUMMARY inspect k3 rep3"
    tot = spend.totals(C.EXP)
    assert tot["open_reservations"] == 0
    assert tot["judge_usd"] == pytest.approx(1.1 + 0.1)      # the re-run charges its own chain


def test_not_started_chains_are_rerun_on_resume(env):
    clock = Clock()
    f = Fakes(never_end={C.R, C.FRESH}, comp_s={"pi": 1.0, "inspect": 1.0, "codex": 1.0,
                                                "S1": 1.0})
    run(make(env, f, clock=clock, hard_stop=clock() + 6.0), [full_block(0)])
    f2 = Fakes()
    out = run(make(env, f2), [full_block(0)])
    started = {e[2] for e in f2.events if e[0] == "chain_start"}
    assert out["admitted"] == 1 and started == set(C.ARMS) - {C.FRESH, C.R}


def test_recover_open_reservations(env):
    rid = spend.reserve_api(C.EXP, 13.2, note="block:h0:bulk")
    spend.mark_submitted(rid, exp=C.EXP)
    rid2 = spend.reserve_api(C.EXP, 13.2, note="block:h1:bulk")
    rid3 = spend.reserve_api(C.EXP, 13.2, note="block:h2:bulk")
    spend.mark_submitted(rid3, exp=C.EXP)
    f = Fakes()
    f.costs["h2" + B.SUMMARY_SUFFIX] = 0.4                   # checkpoints show spend for h2
    s = make(env, f)
    s.recover()
    st = spend.reservation_states(spend.read_entries())
    assert st[rid]["closed"] and st[rid]["charges"] == pytest.approx(13.2)   # full
    assert st[rid2]["closed"] and st[rid2]["charges"] == 0                   # released
    assert st[rid3]["closed"] and st[rid3]["charges"] == pytest.approx(0.4)  # checkpoints


def test_chain_exception_recorded_as_error(env):
    f = Fakes(raise_arms={C.PI})
    s = make(env, f)
    run(s, [full_block(0)])
    r = json.loads((env.paths.chains / f"{B.chain_id('h0', s.task_for(0), C.PI, 'A')}.json")
                   .read_text())
    assert r["end_reason"] == C.END_ERROR and "boom" in r["error"]


def test_compaction_failure_marks_dependent_chains(env):
    f = Fakes()

    async def bad(method, messages, tools, summarize, *, history_id, k=1, source="H"):
        if method == "inspect" and k == 1 and source == "H":
            raise RuntimeError("summary overflow")
        return await Fakes.compact(f, method, messages, tools, summarize,
                                   history_id=history_id, k=k, source=source)
    f.compact = bad
    s = make(env, f)
    run(s, [full_block(0)])
    tid = s.task_for(0)
    for arm in (C.INSPECT, C.REP2, C.REP3):
        r = json.loads((env.paths.chains / f"{B.chain_id('h0', tid, arm, 'A')}.json")
                       .read_text())
        assert r["end_reason"] == C.END_ERROR and "failed" in r["error"], arm
    started = {e[2] for e in f.events if e[0] == "chain_start"}
    assert started == {C.FRESH, C.R, C.CODEX, C.PI, C.INSPECT120}


# --- monitoring --------------------------------------------------------------------------------
def test_load_halving_reruns_n_rule(env):
    seen = []

    def n_rule(queue, conc, now):
        seen.append((len(queue), conc))
        return type(queue)(list(queue)[:1])
    f = Fakes()
    s = make(env, f, load=lambda: 45.0, concurrency=8, n_rule=n_rule)
    out = run(s, [full_block(i) for i in range(4)])
    assert seen and seen[0][1] == 4
    assert out["concurrency"] <= 4
    assert "halving concurrency to 4" in env.paths.run_log.read_text()
    assert f.conc and min(f.conc) <= round(C.API_CONCURRENCY * 4 / 32)
    assert out["admitted"] < 4


def test_default_n_rule_drops_blocks_that_do_not_fit(env):
    clock = Clock()
    s = make(env, Fakes(), clock=clock, admit_by=clock() + 1900)
    from collections import deque
    q = deque(full_block(i) for i in range(10))
    kept = s.default_n_rule(q, 16, clock())                   # 2 blocks x 2 waves of 900 s
    assert len(kept) == 2 * 2


def test_error_fraction_stops_admission(env):
    f = Fakes()
    stats = {"requests": 100, "errors": 10, "frac": 0.10, "statuses": {}}
    out = run(make(env, f, failure=lambda: stats), [full_block(0)])
    assert out["admitted"] == 0 and "error fraction" in out["stop_reason"]


def test_repeated_429s_halve(env):
    f = Fakes()
    calls = {"n": 0}

    def stats():
        calls["n"] += 1
        return {"requests": 10, "errors": 0, "frac": 0.0,
                "statuses": {"429": 0 if calls["n"] == 1 else 5}}
    s = make(env, f, failure=stats, concurrency=8)
    s.monitor_s = 0.5
    run(s, [full_block(0), full_block(1)])
    assert "new 429s" in env.paths.run_log.read_text()


def test_orphan_container_stops_after_two_checks(env):
    f = Fakes(chain_s={C.R: 0.1})
    stale = "p49-h0-lcb_stale-R-A-abc123"      # one of this scheduler's histories and arms
    s = make(env, f, containers=lambda: [stale, "p49-mock-server", "p49-h99-x-R-A-1"],
             concurrency=8)
    s.monitor_s = 0.5
    out = run(s, [full_block(i) for i in range(6)])
    assert out["stop_reason"] and f"orphaned container(s): {stale}" in out["stop_reason"]
    assert "p49-h99" not in out["stop_reason"]         # another scheduler's history
    assert out["admitted"] < 6


def test_live_chain_container_is_not_an_orphan(env):
    f = Fakes()
    s = make(env, f)
    tid = s.task_for(0)
    cid = B.chain_id("h0", tid, C.R, "A")
    s.recent_ended[cid] = s.d.clock()
    s.d.containers = lambda: [f"{C.SANDBOX_PREFIX}{cid}-a1b2c3", f"{C.PASS_PREFIX}{cid}-1"]
    asyncio.run(s.check_containers())
    asyncio.run(s.check_containers())
    assert s.stop_reason is None and s.last_docker == 2


# --- run.py --------------------------------------------------------------------------------
def test_run_dry_refusals(monkeypatch, tmp_path):
    import run as R
    monkeypatch.delenv("P49_BASE_URL", raising=False)
    assert "P49_BASE_URL" in R.check_dry({"EMX_COSTS_LEDGER": str(tmp_path / "x")})
    monkeypatch.setattr(C, "BASE_URL", "http://127.0.0.1:9999")
    assert "EMX_COSTS_LEDGER" in R.check_dry({"P49_BASE_URL": "http://127.0.0.1:9999"})
    assert "real ledger" in R.check_dry({"P49_BASE_URL": "http://127.0.0.1:9999",
                                         "EMX_COSTS_LEDGER": str(spend.DEFAULT_LEDGER)})
    assert R.check_dry({"P49_BASE_URL": "http://127.0.0.1:9999",
                        "EMX_COSTS_LEDGER": str(tmp_path / "x")}) is None
    assert R.main(["gate0"]) == 2 and R.main(["bulk"]) == 2


def test_run_parse_histories():
    import run as R
    assert R.parse_histories("0-3,7", []) == [0, 1, 2, 3, 7]
    assert R.parse_histories(None, [5]) == [5]


def test_run_redirect_and_merged_rows(monkeypatch, tmp_path):
    import run as R
    for k in ("RESULTS", "RAW", "COMPACTIONS_DIR", "CHAINS_DIR", "CHECKPOINTS",
              "CHAIN_SUMMARY_JSONL", "SCORES_CSV", "ANALYSIS_JSON"):
        monkeypatch.setattr(C, k, getattr(C, k))
    R.redirect_outputs(tmp_path / "dry")
    assert C.CHAINS_DIR == tmp_path / "dry" / "raw" / "chains"
    assert C.HISTORIES_DIR != tmp_path / "dry" / "raw" / "histories"
    C.CHAIN_SUMMARY_JSONL.write_text(
        json.dumps({"chain_id": "h0-t-R-A", "arm": "R", "history_id": "h0", "task_id": "t",
                    "level": "A", "stage": "gate0", "end_reason": "done"}) + "\n"
        + json.dumps({"chain_id": "h0-t-pi-A", "arm": "pi", "history_id": "h0", "task_id": "t",
                      "level": "A", "stage": "gate0"}) + "\n")
    C.SCORES_CSV.write_text("chain_id,cheat,arm\nh0-t-R-A,1,R\n")
    rows = {r["chain_id"]: r for r in R.merged_rows("gate0")}
    assert rows["h0-t-R-A"]["cheat"] == "1" and rows["h0-t-pi-A"].get("unscored")


def test_run_decide_writes_decision(monkeypatch, tmp_path):
    import run as R
    for k in ("RESULTS", "RAW", "COMPACTIONS_DIR", "CHAINS_DIR", "CHECKPOINTS",
              "CHAIN_SUMMARY_JSONL", "SCORES_CSV", "ANALYSIS_JSON"):
        monkeypatch.setattr(C, k, getattr(C, k))
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "costs.jsonl"))
    R.redirect_outputs(tmp_path / "dry")
    lines, scores = [], ["chain_id,cheat"]
    for i in range(8):
        for lv in C.LEVELS:
            for arm in C.DESIGNS["gate0"]:
                cid = f"h{i}-t{i}-{arm}-{lv}"
                lines.append(json.dumps({"chain_id": cid, "arm": arm, "history_id": f"h{i}",
                                         "task_id": f"t{i}", "level": lv, "stage": "gate0",
                                         "end_reason": "done", "cost_usd": 0.2, "calls": []}))
                scores.append(f"{cid},{int((i + len(arm)) % 4 == 0)}")
    C.CHAIN_SUMMARY_JSONL.write_text("\n".join(lines) + "\n")
    C.SCORES_CSV.write_text("\n".join(scores) + "\n")
    (C.RESULTS / "blocks.jsonl").write_text(
        "\n".join(json.dumps({"stage": "gate0", "status": "complete", "wall_s": 300})
                  for _ in range(8)) + "\n")
    comp = C.COMPACTIONS_DIR / "h0"
    comp.mkdir(parents=True)
    for n in ("inspect_k1", "pi_k1", "rep2_k2", "rep3_k3"):
        (comp / f"{n}.json").write_text(json.dumps({"cost_usd": 0.05, "carry_over": [],
                                                    "summary_text": "s"}))
    (comp / "rep2_k2.replay.json").write_text(json.dumps({"instances": ["x"]}))
    a = R.parser().parse_args(["decide", "--concurrency", "32", "--start", "05:00",
                               "--admit-by", "08:00"])
    assert R.cmd_decide(a) == 0
    d = json.loads((C.RESULTS / "decision.json").read_text())
    assert d["level"] in C.LEVELS and d["design"] == "full" and d["N"] == 40
    assert d["band"] == list(C.BAND)


class SandboxLeak(RuntimeError):            # same name and attributes as p49.shim.SandboxLeak
    def __init__(self, msg, names=()):
        super().__init__(msg)
        self.names, self.record = names, None


def test_sandbox_leak_saves_record_and_stops_admitting(env):
    f = Fakes(chain_s={C.R: 0.05})
    orig = f.run_chain

    async def leaky(spec, start, tools, task, *, deadline_epoch=None):
        rec = await orig(spec, start, tools, task, deadline_epoch=deadline_epoch)
        if spec.arm == C.FRESH:
            e = SandboxLeak("rm failed", (f"p49-{spec.chain_id}-abc123",))
            e.record = rec
            raise e
        return rec
    f.run_chain = leaky
    s = make(env, f, concurrency=8)
    out = run(s, [full_block(i) for i in range(3)])
    assert "orphaned container(s)" in out["stop_reason"] and out["admitted"] == 1
    r = json.loads((env.paths.chains / f"{B.chain_id('h0', s.task_for(0), C.FRESH, 'A')}.json")
                   .read_text())
    assert r["end_reason"] == C.END_DONE                    # the record from the exception


def test_cancel_returning_a_record_is_kept(env):
    clock = Clock()
    f = Fakes(never_end={C.R})

    async def polite(spec, start, tools, task, *, deadline_epoch=None):
        try:
            return await Fakes.run_chain(f, spec, start, tools, task,
                                         deadline_epoch=deadline_epoch)
        except asyncio.CancelledError:
            return C.ChainRecord(spec=spec, end_reason=C.END_DEADLINE, n_turns=2, calls=[],
                                 tool_calls=[], snapshots=[], final_solution_py=None,
                                 beliefs={}, transcript=[], start_tokens=1,
                                 peak_prompt_tokens=1, cost_usd=0.2, started_at="s",
                                 ended_at="e", error="cancelled")
    f.run_chain = polite
    s = make(env, f, clock=clock, hard_stop=clock() + 6.0)
    run(s, [full_block(0)])
    r = json.loads((env.paths.chains / f"{B.chain_id('h0', s.task_for(0), C.R, 'A')}.json")
                   .read_text())
    assert r["end_reason"] == C.END_DEADLINE and r["error"] == "cancelled" and r["n_turns"] == 2


# --- review r1 fixes ---------------------------------------------------------------------------
@pytest.fixture
def calib(monkeypatch):
    monkeypatch.setattr(C, "CALIB", C.Calibration())
    return C.CALIB


def test_settlement_includes_unresolved_bound(env):
    f = Fakes()
    f.unresolved = {"h0" + B.SUMMARY_SUFFIX: 0.9}           # a summary call with no outcome
    run(make(env, f), [full_block(0)])
    assert spend.totals(C.EXP)["judge_usd"] == pytest.approx(1.1 + 0.9)
    assert "unresolved bound $0.9000 over 1" in env.paths.run_log.read_text()


def test_unknown_liability_settles_at_full_reservation(env):
    f = Fakes()
    s = make(env, f)
    s.d.liability_total = None
    run(s, [full_block(0)])
    assert spend.totals(C.EXP)["judge_usd"] == pytest.approx(full_block(0).reserve_usd())


def test_recover_never_settles_at_billed_alone(env):
    rid = spend.reserve_api(C.EXP, 13.2, note="block:h3:bulk")
    spend.mark_submitted(rid, exp=C.EXP)
    f = Fakes()
    f.costs["h3" + B.SUMMARY_SUFFIX] = 0.4
    f.unresolved = {"h3" + B.SUMMARY_SUFFIX: 1.5}
    make(env, f).recover()
    st = spend.reservation_states(spend.read_entries())[rid]
    assert st["closed"] and st["charges"] == pytest.approx(1.9)


def test_calibration_persisted_and_loaded(env, calib):
    path = env.tmp / "calibration.json"
    calib.observe(1000, 1030)
    B.save_calibration(path)
    C.CALIB.c = C.C_INIT                                     # a new process
    d = B.load_calibration(path)
    assert C.CALIB.c == pytest.approx(1.03) and d["n"] == 1 and d["ratios"] == [1.03]
    calib.c = 1.0                                            # never below C_INIT
    B.load_calibration(path)
    assert C.CALIB.c >= C.C_INIT
    # the scheduler saves it after every block
    path.unlink()
    run(make(env, Fakes()), [full_block(0)])
    assert json.loads(path.read_text())["c"] == pytest.approx(C.CALIB.c)


def test_history_revalidated_against_current_c(env, calib):
    f = Fakes()
    store = B.HistoryStore(env.paths.histories, f.build_history, log=lambda m: None)
    store.get(0, C.BAND)                                     # tokens 200,000 x 1.01 in band
    C.CALIB.c = 1.04                                         # 208,000: outside 195-205k
    logs = []
    store2 = B.HistoryStore(env.paths.histories, f.build_history, log=logs.append)
    store2.get(0, C.BAND)
    assert [c[0] for c in f.history_calls] == [0, 0]
    assert any(p.name.startswith("h0.c1.04") for p in env.paths.histories.iterdir())
    assert "rebuilding" in logs[0]
    # a history whose chains already exist is kept, with a log line
    logs.clear()
    store3 = B.HistoryStore(env.paths.histories, f.build_history, log=logs.append)
    store3.get(0, C.BAND, keep=True)
    assert len(f.history_calls) == 2 and "kept" in logs[0]


def test_rebuilt_history_excludes_later_histories_s1(env, calib):
    f = Fakes()
    store = B.HistoryStore(env.paths.histories, f.build_history, log=lambda m: None)
    store.get(1, C.BAND)                                     # builds h0, h1
    C.CALIB.c = 1.04
    B.HistoryStore(env.paths.histories, f.build_history, log=lambda m: None).get(0, C.BAND)
    assert f.history_calls[-1] == (0, C.BAND, ("s1-1",))


def test_run_main_loads_dry_calibration(monkeypatch, tmp_path, calib):
    import run as R
    for k in ("RESULTS", "RAW", "COMPACTIONS_DIR", "CHAINS_DIR", "CHECKPOINTS",
              "CHAIN_SUMMARY_JSONL", "SCORES_CSV", "ANALYSIS_JSON", "BASE_URL"):
        monkeypatch.setattr(C, k, getattr(C, k))
    monkeypatch.setattr(C, "BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("P49_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "l.jsonl"))
    out = tmp_path / "dry"
    out.mkdir()
    (out / "calibration.json").write_text(json.dumps({"c": 1.02, "n": 3, "ratios": [1.02]}))
    assert R.main(["status", "--dry", "--out", str(out)]) == 0
    assert C.CALIB.c == pytest.approx(1.02)
    assert B.calibration_path() == out / "calibration.json"
