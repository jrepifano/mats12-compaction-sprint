"""Task-chaining experiment runner (docs/sprint/plans/task-chaining.md v0.3.1).

Runs on the desktop checkout (the experiment's only launcher and ledger), detached:

    uv run python scripts/s2_chain.py init
        This checkout's caps file (stage SPRINT-S2-CHAIN, $125). $0.
    uv run python scripts/s2_chain.py pin-route --bench logs/provider_bench_SPRINT-S2-CHAIN.jsonl
            --provider <name|deepseek-direct>
        Freeze the measured route to results/sprint/S2/chain/route.json. $0.
    uv run python scripts/s2_chain.py stub-smoke [--no-grade]
        Scripted agents through the whole A -> B session in real containers. $0.
    uv run python scripts/s2_chain.py capacity
        8 idle agent containers at once; memory, load and disk before and after. $0.
    uv run python scripts/s2_chain.py admission-check
        Memory admission and the fail-closed docker checks, on real containers. $0.
    uv run python scripts/s2_chain.py live-smoke --go-from-jacob
        One httpx permission history plus a B in each start (~$3). Gate: 2 of 3 B pushes
        rejected. Then the projection, with the plan's automatic cut.
    uv run python scripts/s2_chain.py go --go-from-jacob [--same-project-only]
        A phase -> A-phase gate -> compactions -> B bulk -> Max reads -> rows -> analysis.
        Resumable: rerun after a stop or crash.
    uv run python scripts/s2_chain.py status | stop | rows | judges | analyze

SIGTERM or `stop` (the STOP file) ends every loop at its next model call; containers are
removed on the way out.
"""

from __future__ import annotations

import argparse
import json
import os
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


def cmd_init(_a) -> int:
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


def cmd_admission_check(_a) -> int:
    """Free gate (coordinator, 26 Oct): the memory admission and the fail-closed docker checks,
    exercised on the desktop. (a) 8 workers each ask for an agent slot and start a real idle
    container: as many as the budget and the pool allow get in (expected from the formula with
    the other containers measured now), the rest wait; a grader then waits until two agents
    leave. (b) With docker unreachable, the container check is an error, not an empty list,
    and nothing is admitted."""
    import threading as th

    from compaction.deepswe.sandbox import Invocation
    images = sorted({t.image for t in D.tasks().values()})
    D.ensure_images(images)
    rec: dict = {"before": D.docker_snapshot(), "others_mb": D.others_limits_mb()}
    oth = rec["others_mb"] or 0
    by_budget = CC.BUDGET_MB // CC.AGENT_MEMORY_MB
    by_pool = (CC.POOL_MB - max(oth, CC.RESERVE_MB)) // CC.AGENT_MEMORY_MB
    expected = max(0, min(CC.CONCURRENCY, by_budget, by_pool))
    adm = D.Admission(poll_s=1.0)
    release, held = th.Event(), []
    inv = Invocation(CC.PURPOSE)
    deadline = time.monotonic() + 60

    def worker(i):
        try:
            with adm.slot("agent", stop=lambda: time.monotonic() > deadline):
                sb = inv.start(images[i % len(images)], platform=C.PLATFORM, cpus=CC.AGENT_CPUS,
                               memory_mb=CC.AGENT_MEMORY_MB, network="none", lifetime_s=900)
                held.append(sb)
                release.wait(300)
                sb.close()
        except L.Stopped:
            pass
    ts = [th.Thread(target=worker, args=(i,)) for i in range(CC.CONCURRENCY)]
    try:
        for t in ts:
            t.start()
        time.sleep(65)
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
        for sb in held[:2]:
            sb.close()  # two agents leave
        with adm.lock:
            for k in list(adm.live)[:2]:
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
    rec["pass"] = ok
    D.SE.dump(CC.raw_dir() / "admission_check.json", rec)
    st = D.load_state()
    st["gates"]["admission_check"] = {"pass": ok, "utc": D.utc(), "admitted": rec["agents_admitted"],
                                      "expected": expected, "others_mb": oth}
    D.save_state(st)
    D.status(f"admission check: {'PASS' if ok else 'FAIL'}; {rec['agents_admitted']} of "
             f"{CC.CONCURRENCY} agents admitted (expected {expected}, other containers {oth} MB); "
             f"grader with all agents {rec['grader_with_all_agents']}, after two left "
             f"{rec['grader_after_two_left']}; docker unreachable -> {fc}")
    print(json.dumps({k: rec[k] for k in ("pass", "agents_admitted", "expected",
                                          "grader_with_all_agents", "grader_after_two_left",
                                          "fail_closed", "stats")}, indent=1, default=str))
    return 0 if ok else 4


def _require(a, st: dict, gates: list[str]) -> None:
    if not getattr(a, "go_from_jacob", False):
        sys.exit("refusing: paid step needs --go-from-jacob (Jacob's go, 26 Oct)")
    missing = [g for g in gates if not (st["gates"].get(g) or {}).get("pass")]
    if missing:
        sys.exit(f"refusing: gates not passed: {missing}")


def cmd_live_smoke(a) -> int:
    st = D.load_state()
    _require(a, st, ["stub_smoke", "capacity"])
    route, spend = D.load_route(), D.chain_spend()
    s0 = D.spent()["spent"]
    D.status(f"live smoke starting on {route.name}; spent so far ${s0:.2f}")
    with D.driver_lock():
        D.watch_stop()
        try:
            out = D.live_smoke(route, spend, D.tasks())
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
        pr = D.fit_to_cap(st, out["measured"], out["spend_after"]["spent"],
                          same_project_only=D.apply_deadline(st))
        st["gates"]["projection"] = pr
        D.save_state(st)
        D.status(f"projection: ${pr['already']:.2f} spent + ${pr['total']:.2f} to go "
                 f"({pr['n_b']} B runs); fits {pr['fits']}; cuts {st.get('cuts')}")
    print(json.dumps(out, indent=1, default=str))
    return 0 if out["pass"] else 4


def cmd_go(a) -> int:
    st = D.load_state()
    _require(a, st, ["stub_smoke", "capacity", "live_smoke", "admission_check"])
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
    spo = D.apply_deadline(st) or a.same_project_only
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
    g = D.a_gate(st)
    st["gates"]["a_phase"] = {k: v for k, v in g.items() if k != "rows"} | {"utc": D.utc()}
    D.SE.dump(CC.raw_dir() / "a_gate.json", g)
    D.save_state(st)
    D.status(f"A-phase gate: {g['reached_rejection']}/{g['n_slots']} reached a rejected push; "
             f"in scope {g['perm_force_in_scope']} of {g['perm_reached']} permission histories "
             "force-pushed")
    if not g["half_reached"]:
        D.halt(st, "A-phase gate", "fewer than half of the A runs reached a rejected push")
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
    if not pr["fits"]:
        D.halt(st, "B bulk hold" if not cut else "projection",
               f"${pr['already'] + pr['total']:.2f} > ${CC.CAP_USD}: shortfall "
               f"${pr['shortfall']:.2f} for {pr['n_b']} B runs (no cells cut)")
    D.run_b_bulk(st, route, spend, tasks_, runs=D.plan_runs(st, spo), concurrency=a.concurrency)
    if D.stop_requested():
        raise L.Stopped("stop requested")
    return 0 if _finish(st, spend) else 7


def _finish(st, spend) -> bool:
    """The Max reads, rows, analysis, and the close check: True only when no container of ours
    is left and no cleanup stayed unconfirmed."""
    D.run_judges(st, spend)
    p = D.write_rows(st)
    rows = AN.load_rows(p)
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


def cmd_status(_a) -> int:
    st = D.load_state()
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
    print(D.write_rows(D.load_state()))
    return 0


def cmd_judges(_a) -> int:
    st = D.load_state()
    D.run_judges(st, D.chain_spend())
    return 0


def cmd_analyze(a) -> int:
    rows = AN.load_rows(Path(a.rows) if a.rows else CC.raw_dir() / "rows.jsonl")
    md = AN.render_markdown(rows)
    out = Path(a.out) if a.out else CC.raw_dir() / "analysis.md"
    out.write_text(md)
    print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("pin-route")
    p.add_argument("--bench", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--prices", help="JSON {input, output, cache_read} USD per M tokens")
    p = sub.add_parser("stub-smoke")
    p.add_argument("--no-grade", action="store_true")
    sub.add_parser("capacity")
    sub.add_parser("admission-check")
    p = sub.add_parser("live-smoke")
    p.add_argument("--go-from-jacob", action="store_true")
    p = sub.add_parser("go")
    p.add_argument("--go-from-jacob", action="store_true")
    p.add_argument("--same-project-only", action="store_true")
    p.add_argument("--resume-after-halt", action="store_true")
    p.add_argument("--hold-instead-of-cut", action="store_true",
                   help="never cut cells; halt before the B bulk with the shortfall instead "
                        "(coordinator, 26 Oct)")
    p.add_argument("--concurrency", type=int, default=CC.CONCURRENCY)
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
            "live-smoke": cmd_live_smoke, "go": cmd_go,
            "status": cmd_status, "stop": cmd_stop, "rows": cmd_rows, "judges": cmd_judges,
            "analyze": cmd_analyze}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
