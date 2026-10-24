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


def _receipt(run_dir: Path, slug: str, sub: str = "") -> dict:
    """What an arm command prints: that it finished, and a hash of its readout, nothing of the
    outcome (astra build r1 #6). Readouts stay sealed until every card is labelled and every
    freeze committed; the analysis reads them from disk after that gate."""
    p = Path(run_dir) / "arms" / slug / sub / "readout.json"
    return {"arm": slug + (f"/{sub}" if sub else ""), "done": p.exists(),
            "readout_sha256": IR.sha256_bytes(p.read_bytes()) if p.exists() else None}


def _secondary_gate(root: Path, case: str) -> None:
    """Secondaries start only when every primary of every frozen candidate has finished, and a
    case (a candidate's 4 continuations) is admitted once, when the tag has headroom for all
    4; its later continuations run under that admission (astra build r2 #6)."""
    missing = []
    for fz in root.glob("*/b*/boundary/freeze/*.json"):
        rd, cid = fz.parents[3], fz.stem
        f = json.loads(fz.read_text())
        for kind in ("repair", "matched"):
            for rep in (1, 2):
                slug = IR.ArmSpec(f["source_policy"], kind, rep, "x").slug
                if not (rd / "arms" / slug / "readout.json").exists():
                    missing.append(f"{rd.name}/{slug} ({cid})")
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
    slug = IR.spec_from_freeze(frz, a.kind, a.rep).slug
    cap = C.INTERACT_SECONDARY_CAP_USD if secondary else C.INTERACT_BASE_CAP_USD
    agent, sim = _models(IR.spend_path(rd.parent, f"{rd.name}-{slug}-"
                                       f"{'secondary' if secondary else 'primary'}"), cap)
    with Invocation(purpose=IR.PURPOSE) as inv:
        fn = IR.run_secondary if secondary else IR.run_arm
        fn(rd, a.k, a.cand_id, a.kind, a.rep, task, agent_model=agent, sim_model=sim, inv=inv)
    print(json.dumps(_receipt(rd, slug, "secondary" if secondary else "")))
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
    replays, grading and drafts; the kept image for the repo search, the cited file and
    check 6."""
    from compaction.s2 import interact_reqs as RQ
    from compaction.s2 import session as SS
    return IR.fork_trigger(lambda: IR.claude_backend(IR.PilotSpend()),
                           grep_runner=SS.grep_runner(), file_runner=RQ.docker_file_runner())


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
        p.add_argument("--kind", required=True, choices=("repair", "matched"))
        p.add_argument("--rep", required=True, type=int, choices=(1, 2))
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
    return {"base": cmd_base, "replays": cmd_replays, "screen": cmd_screen, "arm": cmd_arm,
            "secondary": lambda x: cmd_arm(x, secondary=True), "status": cmd_status,
            "sweep-kept": cmd_sweep_kept, "smoke": cmd_smoke,
            "publish-freeze": cmd_publish_freeze, "retire": cmd_retire,
            "resume": cmd_resume, "advance": cmd_advance}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
