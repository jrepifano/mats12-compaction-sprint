"""SWE-Interact pilot runner (docs/sprint/plans/swe-interact-pilot.md; build split
logs/s2/briefs/swe-interact-build.md).

Every paid call books to stage SPRINT-S3-Q2 with the tag s3q2-interact-pilot, and the runner
refuses any reservation that would take the tag past $120 (the OpenRouter Haiku fallback past
$12); budget.py books only from the designated checkout. Runs live under
data/raw/sprint/s2/interact/<task>-deepseek-a<n>/.

    uv run python scripts/s2_interact.py base --go-from-jacob [--tasks a,b] [--concurrency 6]
        The base runs, all at once by default: 01_plan, then a fresh 02_implement to the first
        eligible boundary (eligibility on Haiku via Max, OpenRouter fallback), submission or
        the caps ($8, 600 calls, 3 h). Infrastructure failures (infra_oom, infra_error) rerun
        once; after two infra_oom the reruns go 3 at a time. docker stats every 3 min.
    uv run python scripts/s2_interact.py replays RUN_DIR K
        The five offline replays at boundary K (unit A's replays.replay_boundary). $0 on Max.
    uv run python scripts/s2_interact.py screen RUN_DIR K
        Grade the live summary and the replays on the requirement list, then the candidate
        screen at boundary K (unit B), writing grades.json and candidates.json. $0 on Max.
    uv run python scripts/s2_interact.py cohort-screen --go-from-jacob RUN_DIR K [K ...]
            [--max-7d-pct P]
        The repair cohort's full screen at each boundary K, in ascending order (every candidate
        to the end; Haiku drafts and Opus on Max), then its assignment (the seeded policy).
        Before every Max request, Max's 7-day window must read below 90%: --max-7d-pct for its
        first 15 min, else the newest Claude Code stream if under 15 min old, else a tiny Haiku
        ping; re-read at most every 5 min. No fresh reading stops.
    uv run python scripts/s2_interact.py universe
        Write the universe (screens, assignment, roster of every frozen assigned candidate);
        commit it before any continuation.
    uv run python scripts/s2_interact.py readout [--final]
        The roster readout: every arm's outcome joined onto the committed universe's roster
        (a missing or failed arm stays in the denominator). Refused while roster arms are still
        to run, unless --final.
    uv run python scripts/s2_interact.py publish-freeze RUN_DIR K CAND_ID
        Copy a freeze record to results/sprint/S3-Q2/interact/freeze/ for committing; arms
        refuse a freeze whose tracked copy isn't in git HEAD byte for byte.
    uv run python scripts/s2_interact.py arm --go-from-jacob RUN_DIR K CAND_ID --kind
            repair|matched --rep 1|2
        One primary continuation from that committed freeze (stops at its first ask_user, a
        submission or 60 calls). Prints a sealed receipt only.
    uv run python scripts/s2_interact.py secondary --go-from-jacob RUN_DIR K CAND_ID --kind K
            --rep N
        Resume that primary with its own live simulator, to submission, 300 calls or $6; refused
        until every primary has finished and the tag has $24 of headroom. Sealed receipt only.
    uv run python scripts/s2_interact.py advance --go-from-jacob [--tasks a,b | --runs DIR ...]
            [--concurrency 6]
        Phase B of the accumulated-loss study: for each task's latest run, the v2 fork rule
        (fork_v2/: grade every summary on every requirement so far, new/inherited ledger,
        screen with the pending-decision check) on its saved boundaries in order; stop at the
        first that forks; otherwise resume to the next fork, ship-it or the caps (carried over).
    uv run python scripts/s2_interact.py resume --go-from-jacob RUN_DIR [--after-freeze]
        Continue one run from its latest boundary. A carded boundary is refused unless
        --after-freeze; then the continuation is sealed (sealed.json; receipt only).
    uv run python scripts/s2_interact.py retire DIR
        Move a crashed arm dir aside (arms/_retired/); execution dirs are never reused.
    uv run python scripts/s2_interact.py status
        Also writes the candidate funnel (funnel.json, funnel.md) with every excluded candidate;
        check-6 exclusions show the Haiku-written act scorer and the reference lines it matched.
        Ends, boundaries, the tag's spend and open reservations, K / K_live / K_replay.
    uv run python scripts/s2_interact.py sweep-kept RUN_DIR
        Remove the run's kept snapshot images (boundary and arm-end images).
    uv run python scripts/s2_interact.py smoke [--task kombu-single-active-consumer-priority]
        $0 end to end with stub agent, stub simulator and stub Haiku (build split "Order" 5).

Keys come from the repo .env (never printed). SIGTERM or Ctrl-C sets the loop stop flag: every
loop ends at its next model call and its containers are removed before exit.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from compaction.deepswe.sandbox import Invocation
from compaction.s2 import config as C
from compaction.s2 import interact_run as IR
from compaction.s2 import loop as LOOP
from compaction.s2.model import OpenAICompatModel, RunSpend


def _env() -> None:
    from dotenv import load_dotenv
    load_dotenv(C.REPO_ROOT / ".env", override=False)


def _sigterm(signum, frame) -> None:
    LOOP.STOP.set()


def _need_go(a) -> None:
    if not a.go_from_jacob:
        sys.exit("paid command: pass --go-from-jacob (Jacob's go for the pilot, 24 Oct)")


def _models(path: Path, cap_usd: float):
    """The agent and simulator models of one run, sharing one run-level cap. `path` is the
    run's spend file, outside its execution dir (IR.spend_path), which must still be fresh."""
    spend = RunSpend(IR.PilotSpend(), cap_usd).attach(path)
    return (OpenAICompatModel(C.ROUTES["deepseek"], spend),
            OpenAICompatModel(C.SIM_ROUTE, spend))


def cmd_base(a) -> int:
    _need_go(a)
    _env()
    root = IR.interact_root()
    tasks = a.tasks.split(",") if a.tasks else list(C.INTERACT_TASKS)
    configs = [IR.BaseConfig(task=IR.load_task(t), root=root,
                             attempt=IR.next_attempt(root, t)) for t in tasks]
    trig = _trigger()
    watch = IR.DockerWatch(root).start()

    def run_one(cfg: IR.BaseConfig) -> dict:
        agent, sim = _models(IR.spend_path(cfg.root, f"{cfg.run_key}-base"),
                             C.INTERACT_BASE_CAP_USD)
        with Invocation(purpose=IR.PURPOSE) as inv:
            # new runs stop by the v2 fork rule (accumulated-loss plan), screening inline
            return IR.run_base(cfg, agent_model=agent, sim_model=sim, inv=inv,
                               eligible=IR.safe_trigger(trig), watch=watch)

    try:
        out = IR.run_bases(configs, run_one, concurrency=a.concurrency, rerun=not a.no_rerun)
    finally:
        watch.stop()
    for r in out:
        print(json.dumps({k: r.get(k) for k in ("run_key", "end", "boundaries", "n_calls",
                                                  "cost_usd", "sim_cost_usd", "seconds")}))
    return 0


def _timed(run_dir: Path, stage: str, k: int, fn):
    """Run one boundary stage and append its start and end to <run>/timings.jsonl (the plan
    times the first fork boundary end to end, boundary to card)."""
    def mark(ev: str, **kw) -> None:
        with open(Path(run_dir) / "timings.jsonl", "a") as f:
            f.write(json.dumps({"utc": IR.utc(), "stage": stage, "k": k, "event": ev, **kw})
                    + "\n")
    mark("start")
    try:
        out = fn()
    except BaseException as e:
        mark("error", error=f"{type(e).__name__}: {e}"[:300])
        raise
    mark("end")
    return out


def cmd_replays(a) -> int:
    _env()
    from compaction.s2 import replays as RE
    bdir = Path(a.run_dir) / f"b{a.k}"
    sp = IR.PilotSpend()
    # The paid fallback only on the critical path, i.e. at an eligible (fork) boundary;
    # descriptive replays at ineligible boundaries run on Max only (astra build r1 #12).
    eligible = bool(json.loads((bdir / "boundary" / "boundary.json").read_text()).get("eligible"))
    be = IR.claude_backend(sp, use_fallback=eligible)
    out = _timed(a.run_dir, "replays", a.k,
                 lambda: RE.replay_boundary(bdir, backend=be, spend=sp))
    print(json.dumps(out, default=str)[:4000])
    return 0


def cmd_screen(a) -> int:
    _env()
    from compaction.s2 import interact_reqs as RQ
    from compaction.s2 import session as SS
    bdir = Path(a.run_dir) / f"b{a.k}"

    def go():
        be = IR.claude_backend(IR.PilotSpend())
        RQ.grade_sources(bdir, backend=be)
        return RQ.screen(bdir, backend=be, grep_runner=SS.grep_runner())
    print(json.dumps(_timed(a.run_dir, "screen", a.k, go), default=str)[:4000])
    return 0


def _receipt(ad: Path, sub: str = "") -> dict:
    """What an arm command prints: that it finished, and a hash of its readout, nothing of the
    outcome (astra build r1 #6). Readouts stay sealed until every card is labelled and every
    freeze committed; the analysis reads them from disk after that gate."""
    p = Path(ad) / sub / "readout.json"
    return {"arm": Path(ad).name + (f"/{sub}" if sub else ""), "done": p.exists(),
            "readout_sha256": IR.sha256_bytes(p.read_bytes()) if p.exists() else None}


def _secondary_gate(root: Path, case: str) -> None:
    """Secondaries start only when every primary of every frozen candidate has finished, and a
    case (a candidate's 4 continuations) is admitted once, when the tag has headroom for all
    4; its later continuations run under that admission (astra build r2 #6)."""
    missing = []
    for fz in root.glob("*/b*/boundary/freeze/*.json"):
        rd, cid, k = fz.parents[3], fz.stem, int(fz.parents[2].name[1:])
        f = json.loads(fz.read_text())
        for kind in IR.arm_kinds(f):
            for rep in IR.REPEATS:
                ad = IR.arm_dir(rd, k, f, IR.ArmSpec(f["source_policy"], kind, rep, "x"))
                if not (ad / "readout.json").exists():
                    missing.append(f"{rd.name}/{ad.name} ({cid})")
    if missing:
        sys.exit(f"secondary refused: primaries not finished: {missing[:6]}")
    mark = root / "_secondary_cases" / f"{case}.json"
    if mark.exists():
        return
    charged, held = IR.tag_totals(C.INTERACT_TAG)
    if charged + held + C.INTERACT_SECONDARY_CASE_USD > C.INTERACT_CAP_USD:
        sys.exit(f"secondary refused: ${charged + held:.2f} spent or held + "
                 f"${C.INTERACT_SECONDARY_CASE_USD:.0f} for a case exceeds ${C.INTERACT_CAP_USD:.0f}")
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(json.dumps({"admitted_utc": IR.utc(), "charged": charged, "held": held}))


def cmd_arm(a, secondary: bool = False) -> int:
    _need_go(a)
    _env()
    rd = Path(a.run_dir)
    if secondary:
        _secondary_gate(rd.parent, f"{rd.name}-b{a.k}-{a.cand_id}")
    task = IR.load_task(json.loads((rd / "run.json").read_text())["task"]["task_id"])
    frz = IR.verified_freeze(rd / f"b{a.k}", a.cand_id)
    ad = IR.arm_dir(rd, a.k, frz, IR.spec_from_freeze(frz, a.kind, a.rep))
    slug = ad.name
    cap = C.INTERACT_SECONDARY_CAP_USD if secondary else C.INTERACT_BASE_CAP_USD
    agent, sim = _models(IR.spend_path(rd.parent, f"{rd.name}-{slug}-"
                                       f"{'secondary' if secondary else 'primary'}"), cap)
    with Invocation(purpose=IR.PURPOSE) as inv:
        fn = IR.run_secondary if secondary else IR.run_arm
        fn(rd, a.k, a.cand_id, a.kind, a.rep, task, agent_model=agent, sim_model=sim, inv=inv)
    print(json.dumps(_receipt(ad, "secondary" if secondary else "")))
    return 0


MAX_7D_HOLD = 0.90  # new Max work stops here (Jacob, 25 Oct)
FRESH_S = 900  # a stream reading, or the operator's --max-7d-pct, is good for 15 minutes


def _seven_day(events: list[dict]) -> float | None:
    for e in reversed(events):
        info = e.get("rate_limit_info") if e.get("type") == "rate_limit_event" else None
        u = ((info or {}).get("unifiedWindows") or {}).get("seven_day", {}).get("utilization")
        if u is not None:
            return float(u)
    return None


def _stream_reading(now: float) -> float | None:
    """The 7-day utilisation in the newest Claude Code arm stream, if under FRESH_S old."""
    from compaction.s2.native import claude_code as CC
    streams = sorted(Path(CC.root()).glob("*/steps/*/stream.jsonl"),
                     key=lambda p: p.stat().st_mtime)
    if not streams or now - streams[-1].stat().st_mtime >= FRESH_S:
        return None
    evs = []
    for ln in streams[-1].read_text(errors="replace").splitlines():
        try:
            evs.append(json.loads(ln))
        except ValueError:
            continue
    return _seven_day(evs)


def _ping_reading() -> float | None:
    """A tiny Haiku `claude -p` call on Max ("Reply with OK."), as imgval does; its
    rate_limit_info carries the window. None when it shows none or the route isn't Max."""
    import shutil
    import tempfile

    from compaction import claude_headless as CH
    cwd = tempfile.mkdtemp(prefix="s2-ping-")
    try:
        evs = CH.run_session(cwd, ["-p", "--model", "claude-haiku-5-5", "--tools", "",
                                   "--strict-mcp-config", "--setting-sources", "",
                                   "--no-session-persistence", "--output-format", "stream-json",
                                   "--verbose", "--max-turns", "1",
                                   "--system-prompt", "Reply with OK."],
                             stdin="OK", timeout_s=120, env=CH.clean_env())
    except Exception:  # noqa: BLE001 - no reading: the caller stops
        return None
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    if CH.verify_route(CH.route_evidence(evs)) == "bad":
        return None
    return _seven_day(evs)


def max_window(override_pct: float | None, *, stream=_stream_reading, ping=_ping_reading,
               now: float | None = None) -> tuple[float | None, str | None]:
    """(Max's 7-day utilisation, why new Max work stops or None), freshly measured (astra
    repair-design r3 #3): the operator's --max-7d-pct (the caller expires it after FRESH_S),
    else the newest Claude Code stream if under 15 minutes old, else a Haiku ping. No fresh
    reading stops."""
    import time
    if override_pct is not None:
        u = override_pct / 100
        return u, (f"7-day window at {override_pct}%" if u >= MAX_7D_HOLD else None)
    u = stream(time.time() if now is None else now)
    if u is None:
        u = ping()
    if u is None:
        return None, "no fresh 7-day reading (stream over 15 min old, ping showed none)"
    return u, (f"seven_day at {u}" if u >= MAX_7D_HOLD else None)


class MaxWindowHold(RuntimeError):
    pass


class MaxWindow:
    """Max's 7-day window, checked before every Max request (astra repair-design r2 #4) and
    read again at most every REFRESH_S; a reading at or over MAX_7D_HOLD stops new requests."""
    REFRESH_S = 300

    def __init__(self, override_pct: float | None, *, reader=None, clock=None):
        import time
        self.override, self.read = override_pct, reader or max_window
        self.clock, self.at, self.last = clock or time.monotonic, None, (None, None)
        self.t0 = None

    def check(self) -> float | None:
        if self.at is None or self.clock() - self.at >= self.REFRESH_S:
            if self.override is not None and self.t0 is None:
                self.t0 = self.clock()
            live = self.override if self.override is not None and \
                self.clock() - self.t0 < FRESH_S else None  # the operator's figure expires
            self.last, self.at = self.read(live), self.clock()
        util, why = self.last
        if why:
            raise MaxWindowHold(f"max_7d={util}: {why}")
        return util


class WindowGated:
    """A backend whose every run() first checks the Max window."""

    def __init__(self, backend, window: MaxWindow):
        self._b, self._w = backend, window

    def run(self, reqs, *args, **kw):
        self._w.check()
        return self._b.run(reqs, *args, **kw)

    def __getattr__(self, name):
        return getattr(self._b, name)


def cmd_cohort_screen(a) -> int:
    _need_go(a)
    _env()
    from compaction.s2 import interact_reqs as RQ
    from compaction.s2 import session as SS
    rd = Path(a.run_dir)
    win = MaxWindow(a.max_7d_pct)
    for k in sorted(a.k):
        bdir = rd / f"b{k}"
        try:
            util = win.check()
            out = RQ.screen_v2(bdir, backend=WindowGated(IR.claude_backend(IR.PilotSpend()), win),
                               grep_runner=SS.grep_runner(), ref_runner=RQ.S.docker_runner(),
                               file_runner=RQ.docker_file_runner(),
                               opus_backend=WindowGated(IR.opus_backend(), win), full=True,
                               authority_runner=RQ.docker_authority_runner())
        except MaxWindowHold as e:
            # the boundary's screen is incomplete and unwritten; rerun it after the reset
            print(json.dumps({"event": "hold", "k": k, "why": str(e)}))
            return 3
        asg = IR.assignment(bdir)
        print(json.dumps({"k": k, "max_7d": util, "passing": len(out["passing"]),
                          "assigned": asg["assigned"], "dropped": len(asg["dropped"])}))
    return 0


def cmd_readout(a) -> int:
    from compaction.s2 import interact_score as SC
    u = IR.committed_universe()
    if u is None:
        sys.exit("no committed universe")
    root = IR.interact_root()
    pending = [e for e in u["roster"]
               if not (root / e["run"] / e["arm_dir"] / "readout.json").exists()]
    if pending and not a.final:
        sys.exit(f"readout refused: {len(pending)} roster arms still to run (--final counts "
                 "them as missing)")
    out = SC.roster_readout(u, root)
    p = IR.UNIVERSE_DIR.parent / "readout.json"
    p.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(json.dumps({"readout": str(p), "by_kind": out["by_kind"]}, default=str))
    return 0


def _latest_runs(root: Path, tasks: list[str]) -> list[Path]:
    """Each task's latest attempt that has a complete boundary."""
    out = []
    for t in tasks:
        runs = sorted((p for p in root.glob(f"{t}-deepseek-a*") if IR.boundaries_of(p)),
                      key=lambda p: int(p.name.rsplit("-a", 1)[1]))
        if runs:
            out.append(runs[-1])
    return out


def _resume_one(rd: Path, after_freeze: bool, watch) -> dict:
    task = IR.load_task(json.loads((rd / "run.json").read_text())["task"]["task_id"])
    prior = IR._prior_totals(rd)
    left = C.INTERACT_BASE_CAP_USD - prior["cost_usd"] - prior["sim_cost_usd"]
    agent, sim = _models(IR.spend_path(rd.parent, f"{rd.name}-r{IR._resume_index(rd)}"),
                         max(0.01, left))
    with Invocation(purpose=IR.PURPOSE) as inv:
        return IR.resume_base(rd, task, agent_model=agent, sim_model=sim, inv=inv,
                              trigger=_trigger(), after_freeze=after_freeze, watch=watch)


def _trigger():
    """The v2 fork rule with the live backends: Max (OpenRouter fallback) for the split,
    replays, grading and drafts; Opus 5.5 on Max for the screen's last check (B and C); the
    kept image for the repo search, the cited file and check 6."""
    from compaction.s2 import interact_reqs as RQ
    from compaction.s2 import session as SS
    return IR.fork_trigger(lambda: IR.claude_backend(IR.PilotSpend()),
                           grep_runner=SS.grep_runner(), file_runner=RQ.docker_file_runner(),
                           opus_factory=IR.opus_backend,
                           authority_runner=RQ.docker_authority_runner())


def _sealed_receipt(r: dict) -> dict:
    return {"run_key": r.get("run_key"), "resume": r.get("resume"), "done": True,
            "sealed": True, "result_sha256": IR.sha256_bytes(
                json.dumps(r, sort_keys=True, default=str).encode())}


def cmd_resume(a) -> int:
    _need_go(a)
    _env()
    rd = Path(a.run_dir)
    watch = IR.DockerWatch(rd.parent).start()
    try:
        r = _resume_one(rd, a.after_freeze, watch)
    finally:
        watch.stop()
    # a sealed run's continuation is sealed like an arm's output: receipt only
    sealed = a.after_freeze or IR.active_seal(rd) is not None
    print(json.dumps(_sealed_receipt(r) if sealed else
                     {k: r.get(k) for k in ("run_key", "resume", "from_boundary", "end",
                                            "boundaries", "n_calls", "cost_usd",
                                            "sim_cost_usd", "seconds")}))
    return 0


def cmd_advance(a) -> int:
    """Phase B (docs/sprint/plans/accumulated-loss.md): for each run, the v2 fork rule on its
    saved boundaries in order, then resume to the next fork, ship-it or the caps."""
    _need_go(a)
    _env()
    root = IR.interact_root()
    runs = [Path(x) for x in a.runs] if a.runs else _latest_runs(
        root, a.tasks.split(",") if a.tasks else list(C.INTERACT_TASKS))
    watch = IR.DockerWatch(root).start()
    trig = _trigger()

    def one(rd: Path) -> dict:
        task = IR.load_task(json.loads((rd / "run.json").read_text())["task"]["task_id"])
        try:
            out = IR.advance(rd, task, trigger=trig,
                             resume=lambda: _resume_one(rd, False, watch))
        except Exception as e:  # noqa: BLE001 - one run's failure never stops the others
            return {"run_key": rd.name, "failed": True,
                    "error": f"{type(e).__name__}: {e}"[:500]}
        r = out.get("resumed") or {}
        row = {"run_key": rd.name, "state": out.get("state"), "fork_at": out["fork_at"],
               "ended": out.get("ended"), "screened": out["screened"],
               "failed": out.get("state") == "screen_failed" or r.get("end") in
               ("error", "infra_error", "infra_oom"),
               "error": out.get("error") or ((r.get("error") or "")[-300:] or None)}
        if r:
            # a run sealed past a boundary shows nothing of its continuation (receipt only)
            row["resumed"] = (_sealed_receipt(r) if IR.active_seal(rd) is not None else
                              {k: r.get(k) for k in ("resume", "end", "boundaries", "n_calls",
                                                     "cost_usd", "sim_cost_usd", "seconds")})
        return row

    from concurrent.futures import ThreadPoolExecutor
    failed = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
            for row in ex.map(one, runs):
                failed += bool(row.get("failed"))
                print(json.dumps(row, default=str), flush=True)
    finally:
        watch.stop()
    return 4 if failed else 0


def cmd_publish_freeze(a) -> int:
    p = IR.publish_freeze(Path(a.run_dir) / f"b{a.k}", a.cand_id)
    print(json.dumps({"tracked_copy": str(p.relative_to(C.REPO_ROOT)),
                      "next": "commit it before any continuation"}))
    return 0


def cmd_retire(a) -> int:
    print(json.dumps({"retired_to": str(IR.retire(Path(a.dir)))}))
    return 0


def cmd_status(a) -> int:
    root = IR.interact_root()
    if root.is_dir():
        for rd in sorted(p for p in root.iterdir() if (p / "result.json").exists()):
            r = json.loads((rd / "result.json").read_text())
            if IR.active_seal(rd) is not None:
                # sealed past a boundary: nothing of the run's continuation (astra r2 #4)
                print(json.dumps({"run_key": rd.name, "sealed_after": IR.active_seal(rd)}))
                continue
            print(json.dumps({k: r.get(k) for k in ("run_key", "end", "boundaries", "n_calls",
                                                      "cost_usd", "sim_cost_usd")}))
        print(json.dumps(IR.k_counts(root)))
        changed = [f"{r['run']}/{r['boundary']}" for r in IR.replay_prompt_report(root)
                   if r["prompt_changed"]]
        print(json.dumps({"antigravity_prompt_changed": changed,
                          "file": str(root / "replay_prompts.json")}))
        f = IR.funnel(root)
        print(json.dumps({"funnel": f["counts"], "excluded": len(f["excluded"]),
                          "file": str(root / "funnel.md")}))
    for tag in (C.INTERACT_TAG, C.INTERACT_FALLBACK_TAG):
        spent, held = IR.tag_totals(tag)
        print(f"{tag}: spent ${spent:.4f}, open ${held:.4f}")
    return 0


def cmd_sweep_kept(a) -> int:
    from compaction.s2 import session as SS
    rd = Path(a.run_dir)
    bad = []
    for d in [rd, *sorted((rd / "arms").glob("*"))] if (rd / "arms").is_dir() else [rd]:
        if (d / "kept_snapshots.json").exists():
            bad += SS.KeepStore.remove(d)
    print(json.dumps({"not_removed": bad}))
    return 3 if bad else 0


def cmd_smoke(a) -> int:
    from compaction.s2 import interact_smoke as SM
    report = SM.run(a.task, root=Path(a.root) if a.root else None)
    print(json.dumps(report, indent=1, default=str))
    return 0 if report.get("ok") else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("base")
    b.add_argument("--go-from-jacob", action="store_true")
    b.add_argument("--tasks")
    b.add_argument("--concurrency", type=int, default=C.INTERACT_CONCURRENCY)
    b.add_argument("--no-rerun", action="store_true",
                   help="don't rerun infrastructure failures (this launch is the rerun)")
    for name in ("replays", "screen"):
        p = sub.add_parser(name)
        p.add_argument("run_dir")
        p.add_argument("k", type=int)
    for name in ("arm", "secondary"):
        p = sub.add_parser(name)
        p.add_argument("--go-from-jacob", action="store_true")
        p.add_argument("run_dir")
        p.add_argument("k", type=int)
        p.add_argument("cand_id", help="a committed freeze record's id; source and block come "
                                       "from it")
        p.add_argument("--kind", required=True, choices=IR.ARM_KINDS)
        p.add_argument("--rep", required=True, type=int, choices=(1, 2))
    sub.add_parser("universe", help="freeze the candidate universe (commit before arms)")
    cs = sub.add_parser("cohort-screen", help="the repair cohort's full screen + assignment")
    cs.add_argument("--go-from-jacob", action="store_true")
    cs.add_argument("run_dir")
    cs.add_argument("k", type=int, nargs="+")
    cs.add_argument("--max-7d-pct", type=float, default=None)
    ro = sub.add_parser("readout", help="the roster readout of the committed universe")
    ro.add_argument("--final", action="store_true")
    r2 = sub.add_parser("resume")
    r2.add_argument("--go-from-jacob", action="store_true")
    r2.add_argument("run_dir")
    r2.add_argument("--after-freeze", action="store_true",
                    help="resume past a carded boundary (its continuation is sealed)")
    ad = sub.add_parser("advance")
    ad.add_argument("--go-from-jacob", action="store_true")
    ad.add_argument("--tasks", help="comma-separated; default: the 6 pilot tasks")
    ad.add_argument("--runs", nargs="*", help="run dirs instead of --tasks")
    ad.add_argument("--concurrency", type=int, default=C.INTERACT_CONCURRENCY)
    f = sub.add_parser("publish-freeze")
    f.add_argument("run_dir")
    f.add_argument("k", type=int)
    f.add_argument("cand_id")
    r = sub.add_parser("retire")
    r.add_argument("dir", help="a crashed arm (or secondary) dir, moved to arms/_retired/")
    sub.add_parser("status")
    s = sub.add_parser("sweep-kept")
    s.add_argument("run_dir")
    m = sub.add_parser("smoke")
    m.add_argument("--task", default="kombu-single-active-consumer-priority")
    m.add_argument("--root")
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _sigterm)
    if a.cmd == "universe":
        print(json.dumps({"universe": str(IR.write_universe(IR.interact_root())),
                          "next": "commit it (and latest.json) before any continuation"}))
        return 0
    return {"base": cmd_base, "replays": cmd_replays, "screen": cmd_screen, "arm": cmd_arm,
            "secondary": lambda x: cmd_arm(x, secondary=True), "status": cmd_status,
            "sweep-kept": cmd_sweep_kept, "smoke": cmd_smoke,
            "publish-freeze": cmd_publish_freeze, "retire": cmd_retire,
            "resume": cmd_resume, "advance": cmd_advance,
            "cohort-screen": cmd_cohort_screen, "readout": cmd_readout}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
