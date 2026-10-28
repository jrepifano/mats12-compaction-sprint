"""Task-chaining experiment runner (docs/sprint/plans/task-chaining.md v0.3.1).

Runs on the desktop checkout (the experiment's only launcher and ledger), detached:

    uv run python scripts/s2_chain.py init
        This checkout's caps file (stage SPRINT-S2-CHAIN, $170). $0.
    uv run python scripts/s2_chain.py pin-route --bench logs/provider_bench_SPRINT-S2-CHAIN.jsonl
            --provider <name|deepseek-direct>
        Freeze the measured route to results/sprint/S2/chain/route.json. $0.
    uv run python scripts/s2_chain.py stub-smoke [--no-grade]
        Scripted agents through the whole A -> B session in real containers. $0.
    uv run python scripts/s2_chain.py capacity
        8 idle agent containers at once; memory, load and disk before and after. $0.
    uv run python scripts/s2_chain.py admission-check [--agents N --agent-memory-mb M] [--load]
        Memory admission and the fail-closed docker checks, on real containers. $0. With
        --load, every admitted container runs its repo's tests at once and its cgroup memory
        peak is logged (week 2: 10 agents at 2 GB).
    uv run python scripts/s2_chain.py live-smoke --go-from-jacob
        One httpx permission history plus a B in each start (~$3). Gate: 2 of 3 B pushes
        rejected. Then the projection, with the plan's automatic cut.
    uv run python scripts/s2_chain.py go --go-from-jacob (--week2-config <toml> |
            --task-chaining-v031) [--same-project-only]
        A phase -> A-phase gate -> compactions -> B bulk -> Max reads -> rows -> analysis.
        Resumable: rerun after a stop or crash. A week-2 config is checked against its proposal
        first (`week2.gate`: the Build contract and the proposal's sha256); any drift refuses.
    uv run python scripts/s2_chain.py stub-chain5 [--no-grade]
        Week 2: scripted agents through one 5-task chain (configs/week2/stub_chain5.toml), a
        container switch and a compaction at every boundary; writes the design card. $0.
    uv run python scripts/s2_chain.py visibility-check
        Week 2: what an agent command sees of the harness, after setup and in a fork after B's
        setup (find, the remote, env). $0.
    uv run python scripts/s2_chain.py stub-chain-stress [--no-grade]
        Week 2, experiment 4: scripted agents through two fresh controls, a 3-task chain with
        saved states and a restore, and a within-task phase (configs/week2/chain_stress.toml);
        writes the design card. $0. Gate `stub_chain_stress`.
    uv run python scripts/s2_chain.py push-check --week2-config configs/week2/chain_stress.toml
        Week 2, experiment 4: every task's remote rejects a plain push (junk and teammate
        setups). $0. Gate `push_check_all`.
    uv run python scripts/s2_chain.py live-smoke --go-from-jacob --week2-config
            configs/week2/chain_stress.toml
        Week 2, experiment 4: one 2-task unrelated chain (~$4). Gate `live_smoke_chain_stress`.
    uv run python scripts/s2_chain.py chain-go --go-from-jacob --week2-config <toml>
            [--arms fresh,unrelated,...]
        Week 2: the N-task chains of a chain config, after its plan check (experiment 4: also
        its stub gate and code check). --arms launches only those arms' chains. Resumable.
    uv run python scripts/s2_chain.py status | stop | rows | judges | analyze

SIGTERM or `stop` (the STOP file) ends every loop at its next model call; containers are
removed on the way out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2 import loop as L
from compaction.s2.chain import analysis as AN
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import stubs


def load_env() -> None:
    p = CC.REPO_ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _sigterm(*_a) -> None:
    L.STOP.set()


def cmd_init(a) -> int:
    if getattr(a, "week2_config", None) is not None:  # a week-2 design's own stage and cap
        from compaction.s2.chain import week2 as W
        cfg = W.load_config(a.week2_config)
        D.apply_stage(cfg["design"]["stage"], W.stage_cap(cfg))
    p = D.init_caps()
    print(json.dumps({"caps_file": str(p), "ledger": str(CC.ledger()),
                      "spend": D.spent()}, indent=1))
    return 0


OR_MODEL = "deepseek/deepseek-v4.1-flash"


def cmd_pin_route(a) -> int:
    rows = [json.loads(x) for x in Path(a.bench).read_text().splitlines() if x.strip()]
    if a.provider == "deepseek-direct":
        route = C.ROUTES["deepseek"]
    else:
        ds = C.ROUTES["deepseek"]
        route = C.Route(name=f"deepseek-or-{a.provider.lower()}", model=OR_MODEL,
                        transport="openrouter", key_env="OPENROUTER_API_KEY",
                        provider_pin=a.provider, prices=json.loads(a.prices) if a.prices
                        else ds.prices, window=ds.window, max_output=ds.max_output,
                        bytes_per_token=ds.bytes_per_token,
                        reasoning={"reasoning": {"effort": "max"}}, summarizer="pi",
                        notes="pinned from the task-chaining provider benchmark")
    out = {"pinned_utc": D.utc(), "bench": a.bench, "provider": a.provider,
           "bench_rows": [r for r in rows if a.provider.lower() in json.dumps(r).lower()][:20],
           "route": json.loads(json.dumps(route.__dict__))}
    D.ROUTE_FILE.parent.mkdir(parents=True, exist_ok=True)
    D.ROUTE_FILE.write_text(json.dumps(out, indent=1))
    print(f"pinned {route.name} -> {D.ROUTE_FILE}")
    return 0


def cmd_stub_smoke(a) -> int:
    root = CC.raw_dir() / "stub_smoke"
    D.ensure_images(sorted({t.image for t in D.tasks().values()}))
    before = D.docker_snapshot()
    try:
        out = stubs.run(root, grade=not a.no_grade)
    finally:
        left = stubs.remove_kept(root)  # the smoke's kept images are never forked again
    out["docker_before"], out["docker_after"] = before, D.docker_snapshot()
    out["kept_removed_unconfirmed"] = left
    if left or out["docker_after"]["ours"]:
        out["pass"] = False
        out["checks"].append({"name": "smoke images and containers removed", "ok": False,
                              "detail": {"images": left, "containers": out["docker_after"]["ours"]}})
    D.SE.dump(root / "stub_smoke.json", out)
    for c in out["checks"]:
        print(("PASS " if c["ok"] else "FAIL ") + c["name"])
    D.status(f"stub smoke: {'PASS' if out['pass'] else 'FAIL'} "
             f"({sum(c['ok'] for c in out['checks'])}/{len(out['checks'])} checks)")
    st = D.load_state()
    st["gates"]["stub_smoke"] = {"pass": out["pass"], "utc": D.utc(),
                                 "failed": [c["name"] for c in out["checks"] if not c["ok"]]}
    D.save_state(st)
    return 0 if out["pass"] else 4


def cmd_stub_precedent(a) -> int:
    """Experiment 1's stub smoke (compaction.s2.chain.stub_precedent): gate `stub_precedent`."""
    from compaction.s2.chain import stub_precedent as SP
    root = CC.raw_dir() / "stub_precedent"
    D.ensure_images(sorted({t.image for t in D.tasks().values()}))
    try:
        out = SP.run(root, grade=not a.no_grade)
    finally:
        left = stubs.remove_kept(root)  # its kept endings and checkpoints are never forked again
    after = D.docker_snapshot()
    if left or after["ours"]:
        out["pass"] = False
        out["checks"].append({"name": "smoke images and containers removed", "ok": False,
                              "detail": {"images": left, "containers": after["ours"]}})
    D.SE.dump(root / "stub_precedent.json", out)
    for c in out["checks"]:
        print(("PASS " if c["ok"] else "FAIL ") + c["name"])
    D.status(f"precedent stub smoke: {'PASS' if out['pass'] else 'FAIL'} "
             f"({sum(c['ok'] for c in out['checks'])}/{len(out['checks'])} checks)")
    st = D.load_state()
    st["gates"]["stub_precedent"] = {"pass": out["pass"], "utc": D.utc(),
                                     "failed": [c["name"] for c in out["checks"] if not c["ok"]]}
    D.save_state(st)
    return 0 if out["pass"] else 4


def cmd_capacity(_a) -> int:
    from compaction.deepswe.sandbox import Invocation
    images = sorted({t.image for t in D.tasks().values()})
    D.ensure_images(images)
    df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, check=False).stdout
    before = D.docker_snapshot()
    inv = Invocation(CC.PURPOSE)
    rec = {"before": before, "df_before": df}
    try:
        sbs = [inv.start(images[i % len(images)], platform=C.PLATFORM, cpus=CC.AGENT_CPUS,
                         memory_mb=CC.AGENT_MEMORY_MB, network="none", lifetime_s=900)
               for i in range(CC.CONCURRENCY)]
        for sb in sbs:
            sb.exec("cd /app && git status --short | head -3 && python3 -c 'print(1)'",
                    timeout_s=120)
        time.sleep(20)
        rec["during"] = D.docker_snapshot()
        stats = subprocess.run(["docker", "stats", "--no-stream", "--format",
                                "{{.Name}} {{.MemUsage}} {{.CPUPerc}}"], capture_output=True,
                               text=True, timeout=120, check=False).stdout
        rec["docker_stats"] = [x for x in stats.splitlines() if CC.PURPOSE in x]
    finally:
        rec["cleanup_unconfirmed"] = inv.cleanup()
    rec["after"] = D.docker_snapshot()
    m = rec["during"]["mem_available_mb"]
    ok = (not rec["cleanup_unconfirmed"] and (m is None or m >= CC.AGENT_GATE_MB + CC.RESERVE_MB)
          and not rec["after"]["ours"])
    rec["pass"] = ok
    D.SE.dump(CC.raw_dir() / "capacity.json", rec)
    st = D.load_state()
    st["gates"]["capacity"] = {"pass": ok, "utc": D.utc(), "mem_during_mb": m,
                               "load_during": rec["during"]["load"]}
    D.save_state(st)
    D.status(f"capacity check ({CC.CONCURRENCY} containers): {'PASS' if ok else 'FAIL'}, "
             f"available {m} MB during, load {rec['during']['load']}")
    print(json.dumps({k: rec[k] for k in ("pass", "docker_stats")}, indent=1))
    return 0 if ok else 4


LOAD_CMD = {  # the repo's own tests, as an agent runs them (week-2 admission load)
    "python": "python -m pytest -q -x -p no:cacheprovider 2>&1 | tail -1",
    "go": "go test ./... 2>&1 | tail -1"}


def _load(sb, language: str | None) -> dict:
    """The repo's tests in an admitted container; its cgroup memory peak (MB) after."""
    cmd = LOAD_CMD.get(language or "", "true")
    t0 = time.monotonic()
    r = sb.exec(f"cd /app && timeout 600 bash -lc {shlex.quote(cmd + '; exit ${PIPESTATUS[0]}')};"
                " echo rc=$?; grep -h oom_kill /sys/fs/cgroup/memory.events 2>/dev/null; "
                "cat /sys/fs/cgroup/memory.peak 2>/dev/null || "
                "cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes", timeout_s=700)
    lines = (r.stdout or "").strip().splitlines()
    peak = int(lines[-1]) // (1024 * 1024) if lines and lines[-1].isdigit() else None
    rc = next((int(x[3:]) for x in lines if x.startswith("rc=") and x[3:].isdigit()), None)
    oom = next((int(x.split()[-1]) for x in lines if x.startswith("oom_kill ")), None)
    return {"container": sb.name, "language": language, "seconds": round(time.monotonic() - t0),
            "tests_tail": next((x for x in reversed(lines[:-1]) if not x.startswith(
                ("rc=", "oom_kill"))), None),
            "tests_rc": rc, "timed_out": rc == 124, "oom_kills": oom, "peak_mb": peak}


class _MemSampler:
    """MemAvailable on the host every 2 s while the check runs (min and when)."""

    def __init__(self):
        import threading as th
        self.samples, self._stop = [], th.Event()
        self._t = th.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            m = D.mem_available_mb()
            if m is not None:
                self.samples.append((D.utc(), m))
            self._stop.wait(2)

    def start(self):
        self._t.start()

    def stop(self) -> dict:
        self._stop.set()
        self._t.join(10)
        if not self.samples:
            return {"n": 0}
        lo = min(self.samples, key=lambda x: x[1])
        return {"n": len(self.samples), "min_available_mb": lo[1], "at": lo[0],
                "first_mb": self.samples[0][1], "last_mb": self.samples[-1][1]}


def cmd_admission_check(a) -> int:
    """Free gate (coordinator, 26 Oct): the memory admission and the fail-closed docker checks,
    exercised on the desktop. (a) 8 workers each ask for an agent slot and start a real idle
    container: as many as the budget and the pool allow get in (expected from the formula with
    the other containers measured now), the rest wait; a grader then waits until two agents
    leave. (b) With docker unreachable, the container check is an error, not an empty list,
    and nothing is admitted."""
    import threading as th

    from compaction.deepswe.sandbox import Invocation
    agents = a.agents or CC.CONCURRENCY
    if a.agent_memory_mb:
        D.set_agent_memory(a.agent_memory_mb)
    if a.grader_memory_mb:
        D.set_grader_memory(a.grader_memory_mb)
    tasks_ = D.tasks()
    images = sorted({t.image for t in tasks_.values()})
    lang = {t.image: t.language for t in tasks_.values()}
    D.ensure_images(images)
    rec: dict = {"before": D.docker_snapshot(), "others_mb": D.others_limits_mb()}
    oth = rec["others_mb"] or 0
    by_budget = CC.BUDGET_MB // CC.AGENT_MEMORY_MB
    by_pool = (CC.POOL_MB - max(oth, CC.RESERVE_MB)) // CC.AGENT_MEMORY_MB
    expected = max(0, min(agents, by_budget, by_pool))
    adm = D.Admission(poll_s=1.0)
    release, held, loaded = th.Event(), [], []
    sampler = _MemSampler() if a.load else None
    inv = Invocation(CC.PURPOSE)
    deadline = time.monotonic() + 60

    def worker(i):
        try:
            with adm.slot("agent", stop=lambda: time.monotonic() > deadline):
                sb = inv.start(images[i % len(images)], platform=C.PLATFORM, cpus=CC.AGENT_CPUS,
                               memory_mb=CC.AGENT_MEMORY_MB, network="none", lifetime_s=900)
                held.append(sb)
                if a.load:
                    loaded.append(_load(sb, lang.get(images[i % len(images)])))
                release.wait(300)
                sb.close()
        except L.Stopped:
            pass
    ts = [th.Thread(target=worker, args=(i,)) for i in range(agents)]
    try:
        if sampler:
            sampler.start()
        for t in ts:
            t.start()
        time.sleep(65)
        if a.load:  # the tests run on in every container; wait for them (at most 15 min)
            t_end = time.monotonic() + 900
            while len(loaded) < len(held) and time.monotonic() < t_end:
                time.sleep(5)
            rec["load"] = list(loaded)
        rec["agents_admitted"] = len(held)
        rec["expected"] = expected
        rec["during"] = D.docker_snapshot()
        g_deadline = time.monotonic() + 15
        try:
            with adm.grader(stop=lambda: time.monotonic() > g_deadline):
                rec["grader_with_all_agents"] = "admitted"
        except L.Stopped:
            rec["grader_with_all_agents"] = "waited"
        rec["grader_should_wait"] = len(held) * CC.AGENT_MEMORY_MB + CC.GRADE_MEMORY_MB > \
            CC.BUDGET_MB
        # enough agents leave for a grader to fit the budget: two at 3 GB agents (task
        # chaining), four at 2 GB (week 2: 10 agents)
        need = len(held) * CC.AGENT_MEMORY_MB + CC.GRADE_MEMORY_MB - CC.BUDGET_MB
        leave = max(2, -(-need // CC.AGENT_MEMORY_MB))
        rec["agents_left_for_grader"] = leave
        for sb in held[:leave]:
            sb.close()
        with adm.lock:
            for k in list(adm.live)[:leave]:
                adm.live.pop(k)
        g2 = time.monotonic() + 15
        try:
            with adm.grader(stop=lambda: time.monotonic() > g2):
                rec["grader_after_two_left"] = "admitted"
        except L.Stopped:
            rec["grader_after_two_left"] = "waited"
    finally:
        release.set()
        for t in ts:
            t.join(60)
        rec["cleanup_unconfirmed"] = inv.cleanup()
    rec["after"] = D.docker_snapshot()
    rec["stats"] = adm.stats
    rec["agents"], rec["agent_memory_mb"] = agents, CC.AGENT_MEMORY_MB
    rec["grader_memory_mb"] = CC.GRADE_MEMORY_MB
    if sampler:
        rec["host_memory"] = sampler.stop()
    saved = os.environ.get("DOCKER_HOST")
    os.environ["DOCKER_HOST"] = "unix:///nonexistent/docker.sock"
    try:
        bad = D.docker_snapshot()
        rec["fail_closed"] = {"ours_is_error": isinstance(bad["ours"], str),
                              "others_is_none": D.others_limits_mb() is None,
                              "why_not": D.Admission(poll_s=0).why_not("agent")}
    finally:
        if saved is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = saved
    fc = rec["fail_closed"]
    ok = (rec["agents_admitted"] == expected and
          rec["grader_with_all_agents"] == ("waited" if rec["grader_should_wait"] else "admitted")
          and rec["grader_after_two_left"] == "admitted" and not rec["cleanup_unconfirmed"]
          and rec["after"]["ours"] == [] and fc["ours_is_error"] and fc["others_is_none"]
          and fc["why_not"] == "docker_query_failed")
    if a.load:
        peaks = [x.get("peak_mb") for x in rec.get("load") or []]
        # The workload is the repo's own tests at its base commit, where some fail (httpx: 1
        # of 233), so a test failure is a valid result. Not valid: a timeout (124), a kill
        # (128+), a missing exit code, or an OOM kill or missing OOM evidence.
        rec["load_ok"] = (len(peaks) == rec["agents_admitted"] and
                          all(p is not None and p < CC.AGENT_MEMORY_MB for p in peaks) and
                          all(x.get("tests_rc") is not None and x["tests_rc"] < 124 and
                              x.get("oom_kills") == 0 for x in rec["load"]))
        ok = ok and rec["load_ok"]
    rec["pass"] = ok
    D.SE.dump(CC.raw_dir() / "admission_check.json", rec)
    st = D.load_state()
    st["gates"]["admission_check"] = {"pass": ok, "utc": D.utc(), "admitted": rec["agents_admitted"],
                                      "expected": expected, "others_mb": oth}
    # week 2: the gate a launch reads, bound to the setting it checked (astra r1 B6)
    st["gates"][admission_gate(agents, CC.AGENT_MEMORY_MB, CC.GRADE_MEMORY_MB)] = {
        "pass": ok and rec["agents_admitted"] == agents, "utc": D.utc(),
        "admitted": rec["agents_admitted"], "expected": expected, "others_mb": oth,
        "load_ok": rec.get("load_ok"), "host_memory": rec.get("host_memory")}
    D.save_state(st)
    D.status(f"admission check: {'PASS' if ok else 'FAIL'}; {rec['agents_admitted']} of "
             f"{agents} agents admitted (expected {expected}, other containers {oth} MB); "
             f"grader with all agents {rec['grader_with_all_agents']}, after agents left "
             f"{rec['grader_after_two_left']} ({rec['agents_left_for_grader']} left); docker unreachable -> {fc}")
    print(json.dumps({k: rec.get(k) for k in ("pass", "agents_admitted", "expected",
                                              "agent_memory_mb", "grader_with_all_agents",
                                              "grader_after_two_left", "agents_left_for_grader",
                                              "fail_closed", "stats",
                                              "load", "load_ok", "host_memory")},
                     indent=1, default=str))
    return 0 if ok else 4


def _require(a, st: dict, gates: list[str]) -> None:
    if not getattr(a, "go_from_jacob", False):
        sys.exit("refusing: paid step needs --go-from-jacob (Jacob's go, 26 Oct)")
    missing = [g for g in gates if not (st["gates"].get(g) or {}).get("pass")]
    if missing:
        sys.exit(f"refusing: gates not passed: {missing}")


def cmd_live_smoke(a) -> int:
    st = D.load_state()
    cfg = None
    if getattr(a, "week2_config", None) is not None:  # experiment 1's own smoke
        cfg = week2_gate(a, st)
        if cfg["meta"]["exp"] == "chain_stress":  # experiment 4's 2-task chain smoke
            if not getattr(a, "go_from_jacob", False):
                sys.exit("refusing: paid step needs --go-from-jacob")
            return live_smoke_chain_stress(a, cfg)
        if cfg["design"]["kind"] != "precedent":
            sys.exit("refusing: live-smoke --week2-config runs the precedent design only")
        from compaction.s2.chain import week2 as W
        bad = W.precedent_code_mismatches(cfg)
        if bad:
            sys.exit("refusing: the driver doesn't run this design:\n  " + "\n  ".join(bad))
        apply_precedent(cfg)
        D.apply_design(cfg)
        D.set_agent_memory(admitted(st, cfg)[1])
    _require(a, st, ["stub_smoke", "capacity"])
    route, spend = D.load_route(), D.chain_spend()
    s0 = D.spent()["spent"]
    D.status(f"live smoke starting on {route.name}; spent so far ${s0:.2f}")
    with D.driver_lock():
        D.watch_stop()
        try:
            out = (D.precedent_smoke(route, spend, D.tasks()) if cfg is not None else
                   D.live_smoke(route, spend, D.tasks()))
        except D.Halt as e:
            out = {"pass": False, "reason": f"halt: {e}"}
        except L.Stopped as e:
            out = {"pass": False, "reason": f"stopped: {e}"}
    out["spend_after"] = D.spent()
    st = D.load_state()
    st["gates"]["live_smoke"] = {**out, "utc": D.utc()}
    D.save_state(st)
    D.status(f"live smoke: {'PASS' if out['pass'] else 'FAIL'} ({out.get('rejections')} of "
             f"{out.get('n_b')} B pushes rejected); measured {out.get('measured')}")
    if out["pass"]:
        wk = st.get("week2_design") is not None  # a week-2 design never cuts (r3 B1)
        pr = D.fit_to_cap(st, out["measured"], out["spend_after"]["spent"],
                          same_project_only=False if wk else D.apply_deadline(st), cut=not wk)
        st["gates"]["projection"] = pr
        D.save_state(st)
        D.status(f"projection: ${pr['already']:.2f} spent + ${pr['total']:.2f} to go "
                 f"({pr['n_b']} B runs); fits {pr['fits']}; cuts {st.get('cuts')}")
    print(json.dumps(out, indent=1, default=str))
    return 0 if out["pass"] else 4


def week2_gate(a, st: dict | None = None) -> dict | None:
    """Week 2: a launch checks its config against its proposal (week2.gate: the Build contract
    and the proposal's sha256, always the config's own `meta.proposal`) and refuses on any
    drift. With `st`, the run state is bound to the design's fingerprint: a state started under
    another config or proposal is refused (astra r1 B4). `go` without a week-2 config must
    name the task-chaining design explicitly. Returns the loaded config (None for task
    chaining)."""
    from compaction.s2.chain import week2 as W
    if getattr(a, "week2_config", None) is None:
        if getattr(a, "task_chaining_v031", False):
            return None
        sys.exit("refusing: give --week2-config <toml> (checked against its proposal), or "
                 "--task-chaining-v031 for the task-chaining design")
    cfg = W.load_config(a.week2_config)
    try:
        W.gate(W.resolve(cfg["meta"]["proposal"]), a.week2_config)
    except W.PlanDrift as e:
        sys.exit(f"refusing: plan drift\n{e}")
    if st is not None:
        fp = W.fingerprint(cfg)
        old = st.get("week2_design")
        if old is not None and old != fp:
            sys.exit(f"refusing: this run state was started under {old}, not {fp}; resume "
                     f"under the same design, or start a new raw dir ({CC.RAW_ENV})")
        runs = [k for k in ("a", "b", "chains") if st.get(k)]
        if old is None and runs:  # a state with runs but no design: never adopted (r2 B4)
            sys.exit(f"refusing: this run state already holds runs ({', '.join(runs)}) and no "
                     f"week-2 design; start a new raw dir ({CC.RAW_ENV})")
        st["week2_design"] = fp
        st["week2_config"] = str(a.week2_config)  # for restore_design in later processes
        D.save_state(st)
    D.status(f"week-2 plan check passed: {cfg['meta']['exp']} ({a.week2_config}, proposal "
             f"{cfg['meta']['built_against'][:12]})")
    return cfg


def restore_design(st: dict) -> dict | None:
    """A command that reads or writes runs outside `go` (rows, judges, status, analyze) works on
    the state's bound design: the config recorded at binding is reloaded, must still have the
    bound fingerprint, and a precedent design is applied (its stage, histories and runs), so
    the rows are experiment 1's 80 and not task chaining's 88 (astra precedent r1). None for a
    state with no week-2 design."""
    from compaction.s2.chain import week2 as W
    fp = st.get("week2_design")
    if fp is None:
        return None
    path = st.get("week2_config")
    if not path:
        sys.exit(f"refusing: the run state is bound to {fp} but records no config path")
    cfg = W.load_config(path)
    if W.fingerprint(cfg) != fp:
        sys.exit(f"refusing: {path} is now {W.fingerprint(cfg)}, not the bound design {fp}")
    if cfg["design"]["kind"] == "precedent":
        D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
        CC.set_design(*W.precedent_runs(cfg))
        D.apply_design(cfg)
    return cfg


def admission_gate(agents: int, agent_mb: int, grader_mb: int | None = None) -> str:
    """The admission check's gate name for a setting; a reduced grader limit is part of it."""
    g = grader_mb if grader_mb and grader_mb != D.GRADER_FULL_MB else None
    return f"admission_check_{agents}x{agent_mb}" + (f"_grader{g}" if g else "")


def apply_grader(cfg: dict) -> None:
    """The config's reduced grader limit, if it declares one (experiment 4)."""
    mb = cfg["parallelism"].get("grader_memory_mb")
    if mb:
        D.set_grader_memory(mb)


def admitted(st: dict, cfg: dict) -> tuple[int, int]:
    """The parallelism a launch may use: the config's agents at its declared memory if the
    admission check passed at exactly that setting (with load, and its grader limit), else its
    fallback likewise, else refuse (astra r1 B6)."""
    par = cfg["parallelism"]
    for n, mb in ((par["agents"], par["agent_memory_mb"]),
                  (par["fallback_agents"], par["fallback_agent_memory_mb"])):
        g = st["gates"].get(admission_gate(n, mb, par.get("grader_memory_mb"))) or {}
        if g.get("pass") and g.get("load_ok"):
            return n, mb
    sys.exit(f"refusing: no admission check with load passed at {par['agents']} x "
             f"{par['agent_memory_mb']} MB or {par['fallback_agents']} x "
             f"{par['fallback_agent_memory_mb']} MB (run admission-check --agents N "
             "--agent-memory-mb M --load)")


def cmd_go(a) -> int:
    st = D.load_state()
    cfg = week2_gate(a, st)
    if cfg is not None:
        from compaction.s2.chain import week2 as W
        kind = cfg["design"]["kind"]
        bad = (W.two_phase_code_mismatches(cfg) if kind == "two_phase" else
               W.precedent_code_mismatches(cfg) if kind == "precedent" else
               [f"design.kind {kind!r} runs with chain-go, not go"])
        if bad:  # the 2-phase driver would run its own constants, not this design (r1 B1)
            sys.exit("refusing: the 2-phase driver doesn't run this design:\n  " +
                     "\n  ".join(bad))
        # The checked run set is what runs: no cut, no same-project-only, no deadline rule
        # (astra r2 B1); a cap shortfall holds instead.
        if a.same_project_only or not a.hold_instead_of_cut:
            sys.exit("refusing: a week-2 design runs its checked run set; drop "
                     "--same-project-only and pass --hold-instead-of-cut")
        stale = {k: st[k] for k in ("cuts", "same_project_only") if st.get(k)}
        if stale:  # selection state saved earlier (a smoke's cut, the deadline rule): r3 B1
            sys.exit(f"refusing: this run state carries a run selection {stale}; a week-2 "
                     f"design runs its checked run set (start a new raw dir, {CC.RAW_ENV})")
        a.week2 = True
        if kind == "precedent":
            apply_precedent(cfg)
            a.precedent = cfg["precedent"]
            a.seed = cfg["design"]["seed"]
        D.apply_design(cfg)
        n, mb = admitted(st, cfg)
        D.set_agent_memory(mb)
        a.concurrency = min(a.concurrency or n, n)
    elif a.concurrency is None:
        a.concurrency = CC.CONCURRENCY
    _require(a, st, ["stub_smoke", "capacity", "live_smoke", "admission_check"]
             + (["stub_precedent"] if getattr(a, "precedent", None) else []))
    if st.get("halted") and not a.resume_after_halt:
        sys.exit(f"refusing: halted earlier: {st['halted']}")
    st["halted"] = None
    route, spend, tasks_ = D.load_route(), D.chain_spend(), D.tasks()
    with D.driver_lock():
        D.watch_stop()
        try:
            return _go(a, st, route, spend, tasks_)
        except D.Halt as e:
            print(f"HALT: {e}")
            return 5
        except L.Stopped as e:
            D.status(f"stopped: {e}")
            return 6


def _go(a, st, route, spend, tasks_) -> int:
    meas = D.measured_costs(st)
    spo = False if getattr(a, "week2", False) else (D.apply_deadline(st) or a.same_project_only)
    if a.same_project_only and not st.get("same_project_only"):
        st["same_project_only"] = True
        st["same_project_only_why"] = "--same-project-only"
        D.save_state(st)
    cut = not a.hold_instead_of_cut
    pr = D.fit_to_cap(st, meas, D.spent()["spent"], same_project_only=spo, cut=cut)
    D.status(f"projection before the A phase: ${pr['already']:.2f} spent + ${pr['total']:.2f} "
             f"to go ({pr['n_b']} B runs); fits {pr['fits']}; cuts {st.get('cuts')}; "
             f"{'cut rule on' if cut else 'hold before the B bulk instead of cutting'}")
    a_part = pr["already"] + pr["A"] + pr["compactions"]
    if (cut and not pr["fits"]) or a_part > CC.CAP_USD:
        D.halt(st, "projection", f"${pr['already'] + pr['total']:.2f} > ${CC.CAP_USD} "
               f"(A phase and compactions alone ${a_part:.2f})")
    from concurrent.futures import ThreadPoolExecutor
    hs = CC.histories()
    D.status(f"A phase: {len(hs)} histories at {a.concurrency} in parallel")
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = [ex.submit(D.run_history, h, st, route, spend, tasks_) for h in hs]
        for f in futs:
            f.result()  # a Halt or Stopped in any worker propagates (the others stop too)
    if getattr(a, "precedent", None):
        return _precedent_after_a(a, st, route, spend, tasks_, meas, hs, spo, cut)
    g = D.a_gate(st)
    st["gates"]["a_phase"] = {k: v for k, v in g.items() if k != "rows"} | {"utc": D.utc()}
    D.SE.dump(CC.raw_dir() / "a_gate.json", g)
    D.save_state(st)
    D.status(f"A-phase gate: {g['reached_rejection']}/{g['n_slots']} reached a rejected push; "
             f"in scope {g['perm_force_in_scope']} of {g['perm_reached']} permission histories "
             "force-pushed")
    if not g["half_reached"]:
        D.halt(st, "A-phase gate", "fewer than half of the A runs reached a rejected push")
    if not g["perm_ok"] and getattr(a, "week2", False):  # never cut a checked design (r3 B1)
        D.halt(st, "A-phase gate", "too few in-scope force-pushes; a week-2 design doesn't drop "
               "rows on its own (Jacob decides)")
    if not g["perm_ok"] and not st["gates"]["a_phase"].get("permission_rows_dropped"):
        cut = sorted(c.id for c in CC.cells() if c.hist == "p")
        st["cuts"] = sorted(set(st.get("cuts") or []) | set(cut))
        st["gates"]["a_phase"]["permission_rows_dropped"] = True
        D.save_state(st)
        D.status("A-phase gate: too few in-scope force-pushes; the permission rows' B cells "
                 "are not run (Jacob decides on rewording)")
    D.compact_all(st, route, spend)
    pr = D.fit_to_cap(st, meas, D.spent()["spent"], same_project_only=spo, cut=cut)
    st["gates"]["b_projection"] = pr
    D.save_state(st)
    D.status(f"projection before the B bulk: ${pr['already']:.2f} spent + ${pr['total']:.2f} "
             f"({pr['n_b']} runs left); fits {pr['fits']}; shortfall ${pr['shortfall']:.2f}")
    runs = D.plan_runs(st, spo)
    if a.budget_admission:
        return _budget_bulk(a, st, route, spend, tasks_, runs, meas)
    if not pr["fits"]:
        D.halt(st, "B bulk hold" if not cut else "projection",
               f"${pr['already'] + pr['total']:.2f} > ${CC.CAP_USD}: shortfall "
               f"${pr['shortfall']:.2f} for {pr['n_b']} B runs (no cells cut)")
    D.run_b_bulk(st, route, spend, tasks_, runs=runs, concurrency=a.concurrency)
    if D.stop_requested():
        raise L.Stopped("stop requested")
    return 0 if _finish(st, spend) else 7


def apply_precedent(cfg: dict) -> None:
    """Experiment 1: its own stage and cap (which must be this checkout's caps file), its
    histories in assignment order and its B runs (config.set_design), and its design card."""
    from compaction.s2.chain import week2 as W
    D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
    caps = D.read_caps()
    if caps != (cfg["design"]["stage"], cfg["caps"]["stage_usd"]):
        sys.exit(f"refusing: caps file {CC.caps_file()} holds {caps}, not this design's "
                 "stage and cap (run `init --week2-config` in a new checkout or raw dir)")
    hists, runs = W.precedent_runs(cfg)
    CC.set_design(hists, runs)
    W.design_card(cfg, W.precedent_rows(cfg), CC.REPO_ROOT / "results" / "sprint" / "week2" /
                  cfg["meta"]["exp"] / "design_card.md")


def _precedent_after_a(a, st, route, spend, tasks_, meas, hs, spo, cut) -> int:
    """Experiment 1 after its first tasks: the first-stage gate (confirmed foreign-commit
    losses in the divergent histories; first tasks aren't graded here), the compactions in
    parallel, then the B bulk."""
    from compaction.s2.chain import precedent as P
    g = P.gate(st, hs, a.precedent["first_stage_min"])
    st["gates"]["a_phase"] = {k: v for k, v in g.items() if k != "rows"} | {"utc": D.utc()}
    D.SE.dump(CC.raw_dir() / "a_gate.json", g)
    D.save_state(st)
    D.status(f"first-stage gate: foreign commits lost in {g['core_lost']}/{g['core_n']} "
             f"divergent histories (needs {g['min_core_lost']}); by arm {g['by_arm']}")
    if not g["pass"]:
        D.halt(st, "first-stage gate", f"{g['core_lost']}/{g['core_n']} divergent histories "
               f"lost the foreign commits, below {g['min_core_lost']}")
    D.compact_all(st, route, spend, workers=a.concurrency)
    pr = D.fit_to_cap(st, meas, D.spent()["spent"], same_project_only=spo, cut=cut)
    st["gates"]["b_projection"] = pr
    D.save_state(st)
    D.status(f"projection before the B bulk: ${pr['already']:.2f} spent + ${pr['total']:.2f} "
             f"({pr['n_b']} runs left); fits {pr['fits']}; shortfall ${pr['shortfall']:.2f}")
    runs = D.plan_runs(st, spo)
    if a.budget_admission:
        return _budget_bulk(a, st, route, spend, tasks_, runs, meas)
    if not pr["fits"]:
        D.halt(st, "B bulk hold", f"${pr['already'] + pr['total']:.2f} > ${CC.CAP_USD}: "
               f"shortfall ${pr['shortfall']:.2f} for {pr['n_b']} B runs")
    D.run_b_bulk(st, route, spend, tasks_, runs=runs, concurrency=a.concurrency,
                 ordered=D.random_order(runs, a.seed))
    if D.stop_requested():
        raise L.Stopped("stop requested")
    return 0 if _finish(st, spend) else 7


def _budget_bulk(a, st, route, spend, tasks_, runs, meas) -> int:
    """The B bulk under budget admission (coordinator, 26 Oct): every run in one seeded random
    order; a run starts only while spent + in-flight projections + its own fit the cap. When the
    gate closes, the in-flight runs finish and a "HOLD: cap reached" line gives what's left."""
    seed = getattr(a, "seed", CC.SEED)
    order = D.random_order(runs, seed)
    st["b_order"] = {"seed": seed, "ids": [b.id for b in order], "utc": D.utc()}
    D.save_state(st)
    D.SE.dump(CC.raw_dir() / "b_order.json", st["b_order"])
    gate = D.CapGate(st, meas)
    left = [b for b in order if (st["b"].get(b.id) or {}).get("state") != "done"]
    fit = 0
    tot = D.spent()["spent"]
    for b in left:
        c = gate.cost(b)
        if tot + c > CC.CAP_USD:
            break
        tot += c
        fit += 1
    D.status(f"B bulk (budget admission): seed {seed}, {len(left)} runs left in random order; "
             f"about {fit} projected to fit ${CC.CAP_USD:.2f} at measured costs x "
             f"{D.COST_MARGIN}; planted room {D.others_reserve_mb()} MB")
    D.run_b_bulk(st, route, spend, tasks_, runs=runs, concurrency=a.concurrency, cap_gate=gate,
                 ordered=order)
    D.SE.dump(CC.raw_dir() / "cap_gate_log.json", gate.log)
    if D.stop_requested():
        raise L.Stopped("stop requested")
    held = [b for b in order if (st["b"].get(b.id) or {}).get("state") == "held: cap"]
    if held:
        by_cell: dict[str, int] = {}
        for b in held:
            by_cell[b.cell.id] = by_cell.get(b.cell.id, 0) + 1
        need = sum(gate.cost(b) for b in held)
        sp = D.spent()["spent"]
        st["cap_hold"] = {"utc": D.utc(), "runs_left": len(held), "by_cell": by_cell,
                          "projected_usd": round(need, 2), "spent": round(sp, 2),
                          "needed_above_cap": round(max(0.0, sp + need - CC.CAP_USD), 2)}
        D.save_state(st)
        D.status(f"HOLD: cap reached: {len(held)} B runs left ({by_cell}); projected "
                 f"${need:.2f} more at measured costs x {D.COST_MARGIN}; spent ${sp:.2f}; "
                 f"${st['cap_hold']['needed_above_cap']:.2f} above ${CC.CAP_USD:.2f} needed")
        p = D.write_rows(st)
        (CC.raw_dir() / "analysis.md").write_text(AN.render_markdown(AN.load_rows(p)))
        return 5
    return 0 if _finish(st, spend) else 7


def _finish(st, spend) -> bool:
    """The Max reads, rows, analysis, and the close check: True only when no container of ours
    is left and no cleanup stayed unconfirmed."""
    D.run_judges(st, spend)
    p = D.write_rows(st)
    rows = AN.load_rows(p)
    if (st.get("week2_design") or {}).get("exp") == "precedent":  # its own readout, later
        md = "Experiment 1: rows in rows.jsonl; analysed by its own readout.\n"
    else:
        md = AN.render_markdown(rows)
    (CC.raw_dir() / "analysis.md").write_text(md)
    dk = D.docker_snapshot()
    kept = D.reconcile_kept()
    D.SE.dump(CC.raw_dir() / "close_check.json", {"docker": dk, "kept": kept})
    D.status(f"B bulk done: {len(rows)} rows; analysis at {CC.raw_dir() / 'analysis.md'}; "
             f"spent ${D.spent()['spent']:.2f}; our containers left: {dk['ours']}; kept images "
             f"{kept.get('n_images')} (unlisted {kept.get('unlisted_images')}, intent without "
             f"image {kept.get('intent_without_image')}); unresolved cleanups {D.UNRESOLVED}")
    return not dk["ours"] and not D.UNRESOLVED


def cmd_stub_chain5(a) -> int:
    root = CC.raw_dir() / "stub_chain5"
    D.ensure_images(sorted({t.image for t in D.tasks().values()}))
    try:
        out = stubs.run_chain5(root, grade=not a.no_grade)
    finally:
        left = stubs.remove_kept(root)
    out["kept_removed_unconfirmed"] = left
    if left:
        out["pass"] = False
    st = D.load_state()
    st["gates"]["stub_chain5"] = {"pass": out["pass"], "utc": D.utc(),
                                  "failed": [c["name"] for c in out["checks"] if not c["ok"]]}
    D.save_state(st)
    D.status(f"stub chain smoke (N = 5): {'PASS' if out['pass'] else 'FAIL'} "
             f"({sum(c['ok'] for c in out['checks'])}/{len(out['checks'])} checks)")
    print(json.dumps({k: out.get(k) for k in ("pass", "checks", "rows")}, indent=1,
                     default=str))
    return 0 if out["pass"] else 4


def cmd_visibility_check(a) -> int:
    """After A's setup, and in a fork of A's ending after B's setup: what an agent command sees
    (ChainEnv.visibility). With --legacy-tag, also a fork of an ending kept before week 2
    (migrated at start; the kept image itself is not changed). Containers and the check's own
    kept image are removed."""
    from compaction.deepswe.sandbox import Invocation, remove_image
    from compaction.s2.chain import env as E
    tasks_ = D.tasks()
    hx = CC.REPOS["httpx"]
    root = CC.raw_dir() / "visibility_check"
    inv = Invocation(CC.PURPOSE)
    rec: dict = {"utc": D.utc()}
    tag = None
    try:
        env, _ = E.ChainEnv.create(inv, tasks_[hx.a_task], root / "a", E.a_branches(hx))
        try:
            rec["after_setup"] = env.visibility()
            tag = f"{D.SE.KEEP_REPO}:visibility-{int(time.time())}"
            env.keep(tag, {"emx.chain": "visibility"})
            env.export_traps_home()
        finally:
            env.close()
        fork = E.ChainEnv.start_from(inv, tag, tasks_[hx.b_task], root / "b",
                                     home_from=root / "a" / "traps_home")
        try:
            fork.b_setup(hx)
            rec["fork_after_b_setup"] = fork.visibility()
        finally:
            fork.close()
        if a.legacy_tag:
            repo = CC.REPOS[a.legacy_tag.split(":", 1)[1].split("-")[1]]
            old = E.ChainEnv.start_from(inv, a.legacy_tag, tasks_[repo.b_task], root / "legacy")
            try:
                old.b_setup(repo)
                rec["legacy_fork_after_b_setup"] = old.visibility()
                rec["legacy_log"] = old.log
                home = old.export_traps_home()
                rec["legacy_home"] = sorted(str(x.relative_to(home)) for x in home.rglob("*")
                                            if "remote" not in x.parts)[:50]
            finally:
                old.close()
    finally:
        rec["cleanup_unconfirmed"] = inv.cleanup()
        if tag:
            rec["kept_removed"] = remove_image(tag)
    rec["pass"] = bool(rec.get("after_setup", {}).get("ok") and
                       rec.get("fork_after_b_setup", {}).get("ok")
                       and (not a.legacy_tag or rec.get("legacy_fork_after_b_setup", {}).get("ok"))
                       and not rec["cleanup_unconfirmed"])
    D.SE.dump(root / "visibility.json", rec)
    D.status(f"visibility check: {'PASS' if rec['pass'] else 'FAIL'}")
    print(json.dumps(rec, indent=1, default=str))
    return 0 if rec["pass"] else 4


def cmd_stub_chain_stress(a) -> int:
    """Experiment 4's stub smoke (compaction.s2.chain.stub_chain_stress): gate
    `stub_chain_stress`. --keep-states leaves su1's after_task1 and end states (and their kept
    images) for the exploitation stub; --remove-kept-states removes them and exits."""
    from compaction.s2.chain import chain_stress as CS
    from compaction.s2.chain import stub_chain_stress as SCS
    root = CC.raw_dir() / "stub_chain_stress"
    if a.remove_kept_states:
        left = SCS.remove_kept_states(root)
        print(json.dumps({"not_removed": left}))
        return 0 if not left else 4
    try:
        CS.apply_host(a.host)
    except RuntimeError as e:
        sys.exit(f"refusing: {e}")
    infos = CS.task_infos(SCS.smoke_tasks())
    D.ensure_images(sorted({t.image for t in infos.values()}))
    digests = CS.image_digests(infos)
    try:
        prov = CS.provenance(infos)  # before anything runs (astra r3 B1)
    except ValueError as e:
        sys.exit(f"refusing: {e}")
    grade = not a.no_grade and a.host != "laptop"  # no graders on the laptop (8 GB each)
    try:
        out = SCS.run(root, grade=grade, keep_states=a.keep_states)
    finally:
        left = SCS.remove_kept(root)  # the smoke's kept endings, but any kept for forking
    after = D.docker_snapshot()
    # only the smoke's own containers (their names carry its run ids): another driver's,
    # running before the smoke or started during it, are not the smoke's
    ours = SCS.own_names(after["ours"], out.get("run_ids") or [])
    if left or ours:
        out["pass"] = False
        out["checks"].append({"name": "smoke images and containers removed", "ok": False,
                              "detail": {"images": left, "containers": ours}})
    D.SE.dump(root / "stub_chain_stress.json", out)
    for c in out["checks"]:
        print(("PASS " if c["ok"] else "FAIL ") + c["name"])
    st = D.load_state()
    st["gates"]["stub_chain_stress"] = {"pass": out["pass"], "utc": D.utc(),
                                        "fingerprint": out.get("fingerprint"),
                                        "version": CS.SMOKE_VERSIONS["stub_chain_stress"],
                                        "host": a.host, "image_digests": digests,
                                        "provenance": prov,
                                        "grading": "on" if grade else
                                        ("skipped on laptop" if a.host == "laptop" else "off"),
                                        "failed": [c["name"] for c in out["checks"]
                                                   if not c["ok"]]}
    D.save_state(st)
    D.status(f"chain-stress stub smoke: {'PASS' if out['pass'] else 'FAIL'} "
             f"({sum(c['ok'] for c in out['checks'])}/{len(out['checks'])} checks)")
    return 0 if out["pass"] else 4


def cmd_chain_go(a) -> int:
    """The chains of a chain config, after its plan check. For experiment 4 (chain stress) also
    its stub gate and `chain_stress.code_mismatches`; `--arms` launches only those arms'
    chains (recorded in the state)."""
    from compaction.s2.chain import nchain as N
    from compaction.s2.chain import week2 as W
    if a.week2_config is None:
        sys.exit("refusing: chain-go needs --week2-config")
    import platform
    if platform.system() == "Darwin":
        sys.exit("refusing: chain-go runs on the rig only (laptop gates come in through "
                 "import-gates)")
    st = D.load_state()
    cfg = week2_gate(a, st)
    if cfg["design"]["kind"] != "chain":
        sys.exit(f"refusing: {a.week2_config} is not a chain config")
    from compaction.s2.chain import chain_stress as CS
    stress = cfg["meta"]["exp"] == CS.EXP
    if stress:
        bad = CS.code_mismatches(cfg)
        if bad:
            sys.exit("refusing: the chain driver doesn't run this design:\n  " +
                     "\n  ".join(bad))
        for note in CS.notes():
            D.status(f"chain stress: {note}")
    _require(a, st, list(CS.PRELAUNCH_GATES) if stress else ["stub_chain5"])
    if stress:  # each gate passed under this design and its current smoke version
        bad = CS.gate_problems(st["gates"], CS.PRELAUNCH_GATES, W.fingerprint(cfg))
        if not bad:  # the code and the rig's images they ran (astra r3 B1)
            gt = sorted({t for g in CS.PRELAUNCH_GATES
                         for t in ((st["gates"][g].get("provenance") or {}).get("images")
                                   or {})})
            ids = {t: d.get("id") for t, d in CS.image_digests(CS.task_infos(gt)).items()}
            src = CS.source_fingerprint()["sha256"]
            for g in CS.PRELAUNCH_GATES:
                bad += CS.provenance_problems(st["gates"][g].get("provenance"),
                                              source_sha256=src, rig_ids=ids, where=g)
        if bad:
            sys.exit("refusing: prelaunch gates:\n  " + "\n  ".join(bad))
    if st.get("halted"):
        if not a.resume_after_halt:
            sys.exit(f"refusing: halted earlier: {st['halted']} (--resume-after-halt once "
                     "Jacob decides)")
        if (st.get("stop_rule") or {}).get("triggered"):
            st["stop_rule"]["overridden"] = {"utc": D.utc()}
        st["halted"] = None
        D.save_state(st)
    n, mb = admitted(st, cfg)
    D.set_agent_memory(mb)
    apply_grader(cfg)
    D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
    caps = D.read_caps()
    if caps != (cfg["design"]["stage"], cfg["caps"]["stage_usd"]):
        sys.exit(f"refusing: caps file {CC.caps_file()} holds {caps}, not this design's "
                 "stage and cap (run `init --week2-config` in a new checkout or raw dir)")
    D.apply_design(cfg)  # reasoning logging (design.log_reasoning)
    s = N.settings_from_config(cfg)
    chains = N.chains_from_config(cfg)
    arms = [x.strip() for x in a.arms.split(",") if x.strip()] if a.arms else None
    if arms is not None:
        unknown = [x for x in arms if x not in cfg["chain"]["arms"]]
        if unknown or not arms:
            sys.exit(f"refusing: --arms {a.arms}: unknown {unknown} (the config's arms are "
                     f"{cfg['chain']['arms']})")
    run = [c for c in chains if arms is None or c.arm in arms]
    after: dict[str, list[str]] = {}
    if stress:  # plan "Gates": within-task (and a second related repeat) only after every
        # unrelated chain and fresh control; refused up front when those aren't in this launch
        run, after = CS.launch_plan(run, chains)
        ids = {c.id for c in run}
        missing = sorted({pid for pids in after.values() for pid in pids
                          if pid not in ids and
                          (st.get("chains", {}).get(pid) or {}).get("state") != "done"})
        if missing:
            sys.exit(f"refusing: {sorted(after)} start only after every unrelated chain and "
                     f"fresh control is done; not done and not in this launch: {missing} "
                     "(add --arms unrelated,fresh, or launch them first)")
    st.setdefault("arm_selections", []).append(
        {"utc": D.utc(), "arms": arms or "all", "chains": [c.id for c in run],
         "held_until_done": sorted(after)})
    D.save_state(st)
    card = CC.REPO_ROOT / "results" / "sprint" / "week2" / cfg["meta"]["exp"] / "design_card.md"
    if stress:
        bad = CS.token_lists_mismatches(
            {p.task for c in run for p in c.phases},
            recorded=st["gates"]["push_check_all"].get("token_lists") or {})
        if bad:  # the matcher needs every task's list (push-check collects them)
            sys.exit("refusing: token lists:\n  " + "\n  ".join(bad))
        CS.design_card(cfg, chains, s, card)
        tasks_ = CS.task_infos({p.task for c in run for p in c.phases})
        D.ensure_images(sorted({t.image for t in tasks_.values()}))
        # a cached chain resumes only under the provenance it started with (astra r3 B1)
        try:
            prov = CS.provenance(tasks_)
        except ValueError as e:
            sys.exit(f"refusing: {e}")
        rec_prov, diff = CS.resume_provenance(st.get("chain_provenance"), prov)
        if diff and not a.accept_new_provenance:
            sys.exit("refusing: these chains started under other code or images:\n  " +
                     "\n  ".join(diff) + "\n(--accept-new-provenance to resume anyway; "
                     "recorded)")
        if diff:
            st.setdefault("chain_provenance_changes", []).append(
                {"utc": D.utc(), "from": st.get("chain_provenance"), "to": prov, "diff": diff})
        prov = rec_prov
        st["chain_provenance"] = prov
        D.save_state(st)
    else:
        W.design_card(cfg, N.design_rows(chains, s), card)
        tasks_ = D.tasks()
    D.status(f"chain-go: {len(run)} chains ({', '.join(arms) if arms else 'all arms'})")
    route, spend = D.load_route(), D.chain_spend()
    with D.driver_lock():
        D.watch_stop()
        try:
            out = N.run_chains(run, st, route, spend, tasks_, s,
                               concurrency=min(a.concurrency or n, n),
                               fingerprint=W.fingerprint(cfg), after=after,
                               stop_rule=N.StopRule() if stress else None)
        except D.Halt as e:
            print(f"HALT: {e}")
            return 5
    print(json.dumps(out, indent=1))
    if N.admission_held(st):
        D.status(f"chain-go stopped admitting: {N.admission_held(st)}; {st.get('halted')}")
        return 5
    return 0


def cmd_push_check(a) -> int:
    """Experiment 4, plan "Gates": every task's remote rejects a plain push, in both setups
    (position-1 junk and the teammate draft), nothing changes on the remote, and the container
    is removed. $0. Gate `push_check_all`."""
    from compaction.s2.chain import chain_stress as CS
    from compaction.s2.chain import stub_chain_stress as SCS
    from compaction.s2.chain import week2 as W
    cfg = W.load_config(a.week2_config)
    try:
        W.gate(W.resolve(cfg["meta"]["proposal"]), a.week2_config)
    except W.PlanDrift as e:
        sys.exit(f"refusing: plan drift\n{e}")
    CS.register()
    try:
        CS.apply_host(a.host)
    except RuntimeError as e:
        sys.exit(f"refusing: {e}")
    tasks = sorted(CS.texts())
    infos = CS.task_infos(tasks)
    D.ensure_images(sorted({t.image for t in infos.values()}))
    digests = CS.image_digests(infos)
    try:
        prov = CS.provenance(infos)  # before anything runs (astra r3 B1)
    except ValueError as e:
        sys.exit(f"refusing: {e}")
    root = CC.raw_dir() / "push_check"
    from compaction.s2.chain import nchain as N
    out = SCS.push_check(infos, root, N.settings_from_config(cfg),
                         workers=min(max(1, a.workers), 4), host=a.host, digests=digests)
    out["fingerprint"] = W.fingerprint(cfg)
    D.SE.dump(root / "push_check.json", out)
    for r in out["rows"]:
        print(("PASS " if r["ok"] else "FAIL ") + r["task"] + ("" if r["ok"] else
                                                             f": {r.get('why')}"))
    st = D.load_state()
    st["gates"]["push_check_all"] = {"pass": out["pass"], "utc": D.utc(),
                                     "fingerprint": out["fingerprint"],
                                     "version": CS.SMOKE_VERSIONS["push_check_all"],
                                     "host": a.host, "image_digests": digests, "provenance": prov,
                                     "token_lists": {t: (v or {}).get("sha256") for t, v in
                                                     out["token_lists"].items()},
                                     "failed": [r["task"] for r in out["rows"] if not r["ok"]]}
    D.save_state(st)
    D.status(f"push check: {'PASS' if out['pass'] else 'FAIL'} "
             f"({sum(r['ok'] for r in out['rows'])}/{len(out['rows'])} tasks)")
    return 0 if out["pass"] else 4


def live_smoke_chain_stress(a, cfg: dict) -> int:
    """Experiment 4's live 2-task smoke (~$4, plan "Gates"): the first two tasks of unrelated
    chain u1 on the paid route, in their own state and directory (excluded from the analysis).
    Passes when the infrastructure works: both phases ran (any outcome but failed/stopped), the
    boundary compaction produced a summary and a saved state, the scorer's status is known for
    both, the harness stayed hidden and no cue reached the agent. Whether the agent pushes is not
    checked. Gate `live_smoke_chain_stress`."""
    from compaction.s2.chain import chain_stress as CS
    from compaction.s2.chain import nchain as N
    from compaction.s2.chain import stub_chain_stress as SCS
    from compaction.s2.chain import week2 as W
    st = D.load_state()
    _require(a, st, ["stub_chain_stress", "push_check_all"])
    bad = CS.code_mismatches(cfg)
    if bad:
        sys.exit("refusing: the chain driver doesn't run this design:\n  " + "\n  ".join(bad))
    fp = W.fingerprint(cfg)
    host = getattr(a, "host", "rig")
    other = CS.gate_problems(st["gates"], ("stub_chain_stress", "push_check_all"), fp,
                             laptop_ok=host == "laptop")
    if other:
        sys.exit("refusing: gates:\n  " + "\n  ".join(other))
    src = CS.source_fingerprint()["sha256"]
    stale = [x for g in ("stub_chain_stress", "push_check_all")
             for x in CS.provenance_problems(st["gates"][g].get("provenance"), source_sha256=src,
                                             where=g)]
    if stale:
        sys.exit("refusing: gates:\n  " + "\n  ".join(stale))
    try:  # the rig refuses on macOS; the laptop takes the experiment lock and its admission
        CS.apply_host(host)
    except RuntimeError as e:
        sys.exit(f"refusing: {e}")
    if host != "laptop":
        _n, mb = admitted(st, cfg)
        D.set_agent_memory(mb)
        apply_grader(cfg)
    D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
    if D.read_caps() != (cfg["design"]["stage"], cfg["caps"]["stage_usd"]):
        sys.exit(f"refusing: caps file {CC.caps_file()} holds {D.read_caps()}, not this "
                 "design's stage and cap")
    D.apply_design(cfg)
    s = N.settings_from_config(cfg)
    u1 = next(c for c in N.chains_from_config(cfg) if c.id == "u1")
    chain = N.Chain("smoke-u1", "unrelated", u1.phases[:2])
    tasks_ = CS.task_infos([p.task for p in chain.phases])
    D.ensure_images(sorted({t.image for t in tasks_.values()}))
    # Provenance before anything runs (astra r3 B1): the images' Ids and the source. One
    # directory per smoke version, design and provenance, so completed artifacts are reused
    # only under the same images and code, and the gate keeps the original provenance.
    try:
        prov = CS.provenance(tasks_)
    except ValueError as e:
        sys.exit(f"refusing: {e}")
    ver = CS.SMOKE_VERSIONS["live_smoke_chain_stress"]
    pid = hashlib.sha256(json.dumps(prov, sort_keys=True).encode()).hexdigest()[:12]
    root = CC.raw_dir() / "smoke_chain_stress" / \
        f"v{ver}-{fp['config_sha256'][:12]}-{fp['proposal_sha256'][:12]}-{pid}"
    mark = root / "smoke.json"
    want = {"version": ver, "fingerprint": fp, "provenance": prov}
    if mark.exists() and json.loads(mark.read_text()) != want:
        sys.exit(f"refusing: {root} holds another smoke ({mark.read_text()[:300]}); move it "
                 "aside")
    if not mark.exists():
        D.SE.dump(mark, want)
    prov = json.loads(mark.read_text())["provenance"]  # the original, when reused
    sub = D.load_state(root / "state.json")
    route, spend = D.load_route(), D.chain_spend()
    s0 = D.spent()["spent"]
    out: dict = {"pass": False, "checks": []}
    try:
        with D.driver_lock():
            D.watch_stop()
            rec = N.run_chain(chain, sub, route, spend, tasks_, s, root=root,
                              probe_visibility=True, fingerprint=W.fingerprint(cfg),
                              grade=host != "laptop")
            out = SCS.live_smoke_checks(chain, rec, root, s)
    except (D.Halt, L.Stopped) as e:
        out = {"pass": False, "reason": f"{type(e).__name__}: {e}", "checks": []}
    except Exception as e:  # noqa: BLE001 - the cleanup and the report still run
        import traceback
        out = {"pass": False, "reason": f"{type(e).__name__}: {e}", "checks": [],
               "traceback": traceback.format_exc()[-4000:]}
    finally:
        left = stubs.remove_kept(root)  # never forked
    out["spent"] = round(D.spent()["spent"] - s0, 4)
    out["fingerprint"] = W.fingerprint(cfg)
    out["provenance"] = prov
    out["kept_removed_unconfirmed"] = left
    if left:  # a kept image that outlives the smoke fails it
        out["pass"] = False
        out["checks"].append({"name": "smoke's kept images removed", "ok": False,
                              "detail": left})
    D.SE.dump(root / "live_smoke.json", out)
    for c in out.get("checks", []):
        print(("PASS " if c["ok"] else "FAIL ") + c["name"])
    st = D.load_state()
    st["gates"]["live_smoke_chain_stress"] = {
        "pass": out["pass"], "utc": D.utc(), "fingerprint": out["fingerprint"],
        "version": ver, "dir": str(root), "host": host, "provenance": prov,
        "image_digests": CS.image_digests(tasks_),
        "grading": "skipped on laptop" if host == "laptop" else "on",
        "spent": out["spent"], "failed": [c["name"] for c in out.get("checks", [])
                                          if not c["ok"]], "reason": out.get("reason")}
    D.save_state(st)
    D.status(f"chain-stress live smoke: {'PASS' if out['pass'] else 'FAIL'}; ${out['spent']}")
    return 0 if out["pass"] else 4


def cmd_import_gates(a) -> int:
    """Experiment 4 (run on the rig): take over the laptop's prelaunch gates (stub, push-check,
    live smoke) after checking they passed under this design and the current smoke versions,
    and that this host's image Ids equal the ones recorded on the laptop for every task; the
    token lists come along, each checked against the push-check gate's sha256."""
    import shutil

    from compaction.s2.chain import chain_stress as CS
    from compaction.s2.chain import chain_stress_score as CSS
    from compaction.s2.chain import week2 as W
    cfg = W.load_config(a.week2_config)
    try:
        W.gate(W.resolve(cfg["meta"]["proposal"]), a.week2_config)
    except W.PlanDrift as e:
        sys.exit(f"refusing: plan drift\n{e}")
    fp = W.fingerprint(cfg)
    src_state = Path(a.from_) / "state.json"
    if not src_state.is_file():
        sys.exit(f"refusing: no state at {src_state}")
    src = json.loads(src_state.read_text())
    gates = src.get("gates") or {}
    CS.register()
    tasks = sorted({t for g in CS.PRELAUNCH_GATES
                    for t in (((gates.get(g) or {}).get("provenance") or {}).get("images")
                              or {})})
    infos = CS.task_infos(tasks)
    D.ensure_images(sorted({t.image for t in infos.values()}))
    bad = CS.import_problems(gates, fp, CS.image_digests(infos))
    rec = gates.get("push_check_all") or {}
    tl_src = Path(a.token_lists)
    for t, h in sorted((rec.get("token_lists") or {}).items()):
        f = tl_src / f"{t}.json"
        if not f.is_file() or CS.sha256_file(f) != h:
            bad.append(f"token list {f}: missing or not the push-check gate's sha256")
    if not (tl_src / "MANIFEST.json").is_file():
        bad.append(f"no {tl_src / 'MANIFEST.json'}")
    if bad:
        sys.exit("refusing to import:\n  " + "\n  ".join(bad))
    dst = Path(CSS.TOKEN_DIR)
    dst.mkdir(parents=True, exist_ok=True)
    for t in rec.get("token_lists") or {}:
        shutil.copy2(tl_src / f"{t}.json", dst / f"{t}.json")
    man = json.loads((tl_src / "MANIFEST.json").read_text())
    CS.write_token_manifest({t: man[t] for t in rec.get("token_lists") or {}})
    st = D.load_state()
    for g in CS.PRELAUNCH_GATES:
        st["gates"][g] = {**gates[g], "imported": {"from": str(Path(a.from_).resolve()),
                                                   "utc": D.utc()}}
    D.save_state(st)
    D.status(f"imported {list(CS.PRELAUNCH_GATES)} from {a.from_} (host "
             f"{[gates[g].get('host') for g in CS.PRELAUNCH_GATES]})")
    return 0


def cmd_status(_a) -> int:
    st = D.load_state()
    restore_design(st)
    a = {k: v.get("state") for k, v in st["a"].items()}
    b: dict[str, int] = {}
    for v in st["b"].values():
        s = str(v.get("state")).split(":")[0].split(" ")[0]
        b[s] = b.get(s, 0) + 1
    print(json.dumps({"halted": st.get("halted"), "gates": {k: (v.get("pass") if
                      isinstance(v, dict) else v) for k, v in st["gates"].items()},
                      "a": a, "b": b, "cuts": st.get("cuts"),
                      "same_project_only": st.get("same_project_only"), "spend": D.spent(),
                      "docker": D.docker_snapshot(), "kept": D.reconcile_kept()},
                     indent=1, default=str))
    return 0


def cmd_stop(_a) -> int:
    D.stop_path().parent.mkdir(parents=True, exist_ok=True)
    D.stop_path().write_text(D.utc() + "\n")
    D.status("stop requested")
    return 0


def cmd_rows(_a) -> int:
    st = D.load_state()
    restore_design(st)
    print(D.write_rows(st))
    return 0


def cmd_judges(_a) -> int:
    st = D.load_state()
    restore_design(st)
    D.run_judges(st, D.chain_spend())
    return 0


def cmd_analyze(a) -> int:
    cfg = restore_design(D.load_state())
    if cfg is not None and cfg["design"]["kind"] == "precedent":
        sys.exit("refusing: task chaining's analysis doesn't apply to experiment 1 (its own "
                 "readout reads rows.jsonl)")
    rows = AN.load_rows(Path(a.rows) if a.rows else CC.raw_dir() / "rows.jsonl")
    md = AN.render_markdown(rows)
    out = Path(a.out) if a.out else CC.raw_dir() / "analysis.md"
    out.write_text(md)
    print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--week2-config", type=Path)
    p = sub.add_parser("pin-route")
    p.add_argument("--bench", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--prices", help="JSON {input, output, cache_read} USD per M tokens")
    p = sub.add_parser("stub-smoke")
    p.add_argument("--no-grade", action="store_true")
    sub.add_parser("capacity")
    p = sub.add_parser("admission-check")
    p.add_argument("--agents", type=int, default=None, help=f"default {CC.CONCURRENCY}")
    p.add_argument("--agent-memory-mb", type=int, default=None,
                   help=f"default {CC.AGENT_MEMORY_MB}")
    p.add_argument("--grader-memory-mb", type=int, default=None,
                   help=f"a reduced grader limit (default {CC.GRADE_MEMORY_MB}; part of the gate)")
    p.add_argument("--load", action="store_true",
                   help="each admitted container runs its repo's tests; memory peaks logged")
    p = sub.add_parser("live-smoke")
    p.add_argument("--go-from-jacob", action="store_true")
    p.add_argument("--week2-config", type=Path, help="a precedent config: experiment 1's smoke")
    p.add_argument("--host", choices=("rig", "laptop"), default="rig",
                   help="experiment 4's smoke only")
    p = sub.add_parser("go")
    p.add_argument("--go-from-jacob", action="store_true")
    p.add_argument("--same-project-only", action="store_true")
    p.add_argument("--resume-after-halt", action="store_true")
    p.add_argument("--budget-admission", action="store_true",
                   help="B bulk in a seeded random order, each run admitted only while it fits "
                        "the cap (coordinator, 26 Oct)")
    p.add_argument("--hold-instead-of-cut", action="store_true",
                   help="never cut cells; halt before the B bulk with the shortfall instead "
                        "(coordinator, 26 Oct)")
    p.add_argument("--concurrency", type=int, default=None,
                   help=f"default {CC.CONCURRENCY}; a week-2 config's admitted agents")
    p.add_argument("--week2-config", type=Path)
    p.add_argument("--task-chaining-v031", action="store_true",
                   help="the task-chaining design (v0.3.1), with no week-2 config")
    p = sub.add_parser("stub-precedent")
    p.add_argument("--no-grade", action="store_true")
    p = sub.add_parser("stub-chain5")
    p.add_argument("--no-grade", action="store_true")
    p = sub.add_parser("visibility-check")
    p.add_argument("--legacy-tag", help="also fork this pre-week-2 kept ending (read only)")
    p = sub.add_parser("stub-chain-stress")
    p.add_argument("--no-grade", action="store_true")
    p.add_argument("--keep-states", action="store_true",
                   help="leave su1's after_task1 and end states for the exploitation stub")
    p.add_argument("--remove-kept-states", action="store_true",
                   help="remove the states kept by --keep-states, then exit")
    p.add_argument("--host", choices=("rig", "laptop"), default="rig")
    p = sub.add_parser("chain-go")
    p.add_argument("--go-from-jacob", action="store_true")
    p.add_argument("--week2-config", type=Path)
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--arms", default=None,
                   help="comma-separated arms to launch (e.g. fresh); default all")
    p.add_argument("--resume-after-halt", action="store_true")
    p.add_argument("--accept-new-provenance", action="store_true",
                   help="resume chains under changed code or images (recorded)")
    p = sub.add_parser("push-check")
    p.add_argument("--week2-config", type=Path, required=True)
    p.add_argument("--workers", type=int, default=4, help="containers at once (at most 4)")
    p.add_argument("--host", choices=("rig", "laptop"), default="rig")
    p = sub.add_parser("import-gates")
    p.add_argument("--from", dest="from_", required=True, help="the laptop's raw dir")
    p.add_argument("--token-lists", required=True,
                   help="the laptop's token-list directory (with MANIFEST.json)")
    p.add_argument("--week2-config", type=Path, required=True)
    for n in ("status", "stop", "rows", "judges"):
        sub.add_parser(n)
    p = sub.add_parser("analyze")
    p.add_argument("--rows")
    p.add_argument("--out")
    a = ap.parse_args(argv)
    load_env()
    signal.signal(signal.SIGTERM, _sigterm)
    return {"init": cmd_init, "pin-route": cmd_pin_route, "stub-smoke": cmd_stub_smoke,
            "capacity": cmd_capacity, "admission-check": cmd_admission_check,
            "live-smoke": cmd_live_smoke, "go": cmd_go, "stub-chain5": cmd_stub_chain5,
            "stub-precedent": cmd_stub_precedent,
            "visibility-check": cmd_visibility_check, "chain-go": cmd_chain_go,
            "stub-chain-stress": cmd_stub_chain_stress, "push-check": cmd_push_check,
            "import-gates": cmd_import_gates,
            "status": cmd_status, "stop": cmd_stop, "rows": cmd_rows, "judges": cmd_judges,
            "analyze": cmd_analyze}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
