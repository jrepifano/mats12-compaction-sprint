"""The Codex arm of the native-harness experiment (brief logs/s2/briefs/native-codex.md).

  validate   route (b)'s validation on a laptop toy session with planted facts (Codex's own
             workspace-write sandbox, ChatGPT plan, no simulator, $0): three turns, a forced
             remote compaction, then 3 recitations and 1 direct probe, each on its own fork.
  local      the same toy under route (a)'s local-compaction provider: does it run on ChatGPT
             login, and is `compacted.message` plaintext?
  base       one SWE-Interact base run in Codex on the desktop (common.NativeSession: internal
             network, egress sidecar, the simulator as MCP server), every compaction's summary
             recovered by recitation (route b). Paid (the simulator): --smoke (a "smoke-<utc>-"
             run key, simulator capped at $2, stop after the first boundary unless
             --stop-after says otherwise) or --go-from-jacob. Run under
             `uv run --with fastmcp==4.1.3`. Gated on the desktop's `free -g` available >= 6 GB;
             the container gets --memory 3g --cpus 2 (main session, 24 Oct).
  stage RUN_DIR... --out DIR   expose native <run>/b<k>/boundary as <DIR>/<run>-b<k>/a1 (symlinks),
             the <cohort>/a<n>/boundary layout lossaudit reads; then
             `scripts/s2_lossaudit.py run --max --raw DIR --out ...`. $0, idempotent.
  batch --go-from-jacob --tasks a,b,... [--concurrency 3] [--sim-cap 0.8]
             Base runs as separate `base` processes, at most --concurrency at once, each launched
             only when the desktop has >= 6 GB available. As each run ends: `stage` it, grade it
             with lossaudit on Max (one grading at a time), write its new/inherited ledger and
             print one status line (compactions, items, lost, new/inherited, first-boundary
             pre_tokens). Refuses to launch a run if the finished runs' simulator spend plus
             --sim-cap for every unfinished run would pass --spend-stop ($5).
  fork BDIR  Q2: resume boundary BDIR from its kept image with the encrypted summary replaced
             by readable text (the boundary's recited summary unless --summary-file), in a new
             thread, given --prompt. --probe-word W appends "FORK-PROBE: <W>" and asks the fork
             to quote that line without tools (the round-trip check). Paid like `base`.

Outputs go to data/raw/sprint/s2/native/codex/<command>/<utc>/ (gitignored).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2.native import codex as X

OUT = C.REPO_ROOT / "data" / "raw" / "sprint" / "s2" / "native" / "codex"


def _dump(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def toy_session(home: X.CodexHome, work: Path, out: Path, *, dump_lines: int,
                config: dict | None = None) -> tuple[list[dict], Path]:
    """Turns 1-2 normally, turn 3 with the compaction limit just under the current context, so
    Codex compacts before turn 3. Returns the rollout lines and its path."""
    toy = X.build_toy(work / "toy")
    cfg = dict(config or {})
    r1 = X.codex_exec(home, X.TOY_TURNS[0], cwd=toy, config=cfg, log_path=out / "turn1.jsonl")
    if r1.rc != 0 or not r1.thread_id:
        raise RuntimeError(f"turn 1 failed rc={r1.rc}: {r1.stderr[-800:]}")
    tid = r1.thread_id
    r2 = X.codex_exec(home, X.TOY_TURNS[1].format(n=dump_lines), cwd=toy, resume=tid,
                      config=cfg, log_path=out / "turn2.jsonl")
    if r2.rc != 0:
        raise RuntimeError(f"turn 2 failed rc={r2.rc}: {r2.stderr[-800:]}")
    path = X.find_rollout(home.path, tid)
    lines = X.read_rollout(path)
    ctx = X.last_input_tokens(lines, len(lines) - 1) or 0
    limit = max(1000, ctx - 2000)
    r3 = X.codex_exec(home, X.TOY_TURNS[2], cwd=toy, resume=tid,
                      config={**cfg, "model_auto_compact_token_limit": limit},
                      log_path=out / "turn3.jsonl")
    if r3.rc != 0:
        raise RuntimeError(f"turn 3 failed rc={r3.rc}: {r3.stderr[-800:]}")
    lines = X.read_rollout(path)
    _dump(out / "session.json", {"thread_id": tid, "context_before_t3": ctx, "limit": limit,
                                 "rollout": str(path), "turns": [r1.final_text, r2.final_text,
                                                                 r3.final_text]})
    shutil.copy2(path, out / "rollout.jsonl")
    # the planted files must not be readable by the recitations and probes
    shutil.rmtree(toy)
    return lines, path


def cmd_validate(a) -> int:
    out = OUT / "validate" / X.utc().replace(":", "")
    work = out / "work"
    home = X.CodexHome(out / "home", {"model": X.MODEL}).setup()
    lines, _ = toy_session(home, work, out, dump_lines=a.dump_lines)
    ks = X.compaction_lines(lines)
    if not ks or not X.encrypted_items(lines[ks[0]]):
        print(f"no remote compaction in the toy session ({len(ks)} compacted lines)")
        return 2
    empty = work / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    if a.repeats < 1:
        sys.exit("--repeats must be at least 1")
    recs = [X.recite(home, lines, 1, cwd=empty, log_dir=out, tag=f"_r{i + 1}")
            for i in range(a.repeats)]
    at = ks[0]
    ptid, _ = X.fork_rollout(lines, at, home.path)
    pr = X.codex_exec(home, X.probe_prompt(), cwd=empty, resume=ptid,
                      config=X.RECITE_OVERRIDES, sandbox="read-only",
                      log_path=out / "probe_b1.jsonl")
    score = X.score_validation([r.text if r.valid else None for r in recs], pr.final_text,
                               X.history_text(lines, at - 1))
    result = {"utc": X.utc(), "cli": X.CLI_VERSION, "model": X.MODEL,
              "decision_rule": X.DECISION_RULE, "facts": X.FACTS, "fact_user": X.FACT_USER,
              "decoys": X.DECOYS, "recite_prompt": X.RECITE_PROMPT,
              "recitations": [r.to_dict() for r in recs],
              "probe": {"thread_id": ptid, "answer": pr.final_text,
                        "tool_calls": len(pr.tool_items())},
              "score": score}
    _dump(out / "validation.json", result)
    print(json.dumps(score, indent=1))
    print(f"wrote {out / 'validation.json'}")
    return 0


def cmd_local(a) -> int:
    out = OUT / "local" / X.utc().replace(":", "")
    home = X.CodexHome(out / "home", X.local_provider_config({"model": X.MODEL})).setup()
    lines, _ = toy_session(home, out / "work", out, dump_lines=a.dump_lines)
    ks = X.compaction_lines(lines)
    rows = []
    for k, i in enumerate(ks, 1):
        p = lines[i]["payload"]
        rows.append({"k": k, "message_chars": len(p.get("message") or ""),
                     "encrypted_items": len(X.encrypted_items(lines[i])),
                     "message_head": (p.get("message") or "")[:400]})
    _dump(out / "local.json", {"utc": X.utc(), "compactions": rows,
                               "provider": X.local_provider_config()})
    print(json.dumps(rows, indent=1))
    return 0 if rows and rows[0]["message_chars"] else 2


SMOKE_SIM_CAP_USD = 2.0
# Jacob's decision, 2026-10-24 (relayed by the main session): one common 150k compaction
# trigger for every native arm. Codex fires just under its limit (smoke: pre_tokens 39,625 at
# a 40,000 limit), so the limit is the trigger; check each run's first boundary pre_tokens.
COMMON_LIMIT = 150_000
SMOKE_TASK = "kombu-single-active-consumer-priority"
MIN_FREE_GB = 6


def _free_gb(host: str) -> int:
    import subprocess
    ssh = host.removeprefix("ssh://")
    p = subprocess.run(["ssh", ssh, "free", "-g"], capture_output=True, text=True, timeout=60,
                       check=True)
    row = next(x for x in p.stdout.splitlines() if x.startswith("Mem:")).split()
    return int(row[6])


def cmd_base(a) -> int:
    from datetime import UTC, datetime

    from dotenv import load_dotenv

    from compaction.s2 import interact_run as IR
    from compaction.s2.model import OpenAICompatModel, RunSpend
    if not (a.smoke or a.go_from_jacob):
        sys.exit("paid command: pass --smoke (at most 2) or --go-from-jacob")
    load_dotenv(C.REPO_ROOT / ".env", override=False)
    if a.recite_repeats < 1:
        sys.exit("--recite-repeats must be at least 1")
    free = _free_gb(a.host)
    if free < MIN_FREE_GB:
        sys.exit(f"desktop has {free} GB available (< {MIN_FREE_GB}); not starting")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = Path(a.root) if a.root else X.root()
    prefix = f"smoke-{stamp}-" if a.smoke else ""
    cap = min(a.sim_cap, SMOKE_SIM_CAP_USD) if a.smoke else a.sim_cap
    task = IR.load_task(a.task)
    cfg = X.BaseConfig(task=task, root=root, prefix=prefix,
                       attempt=X.next_attempt(root, task.task_id, prefix),
                       cx=X.CodexConfig(model=a.model, effort=a.effort or None,
                                        auto_compact_limit=a.limit),
                       host=a.host, recite_repeats=a.recite_repeats,
                       stop_after=a.stop_after if a.stop_after is not None
                       else (1 if a.smoke else None),
                       cpus=2, memory_mb=3072)
    if a.implement_timeout:
        cfg.step_timeout_s["02_implement"] = int(a.implement_timeout)
    spend = RunSpend(IR.PilotSpend(), cap).attach(IR.spend_path(root, f"{cfg.run_key}-base"))
    res = X.run_base(cfg, sim_model=OpenAICompatModel(C.SIM_ROUTE, spend))
    print(json.dumps({k: res.get(k) for k in ("run_key", "end", "boundaries", "sim_cost_usd",
                                              "seconds", "auth_refreshed", "error",
                                              "cleanup_unconfirmed")}, indent=1)[:4000])
    print("run dir:", cfg.run_dir)
    return 0 if res["end"] in ("submitted", "stop_after", "eligible") else 1


def cmd_fork(a) -> int:
    from datetime import UTC, datetime

    from dotenv import load_dotenv

    from compaction.s2 import interact_run as IR
    from compaction.s2.model import OpenAICompatModel, RunSpend
    if not (a.smoke or a.go_from_jacob):
        sys.exit("paid command: pass --smoke (at most 2) or --go-from-jacob")
    load_dotenv(C.REPO_ROOT / ".env", override=False)
    free = _free_gb(a.host)
    if free < MIN_FREE_GB:
        sys.exit(f"desktop has {free} GB available (< {MIN_FREE_GB}); not starting")
    bdir = Path(a.bdir)
    rec = json.loads((bdir / "boundary" / "compaction.json").read_text())
    summary = Path(a.summary_file).read_text() if a.summary_file else rec["summary"]
    prompt = a.prompt or X.CONTINUE_PROMPT
    if a.probe_word:
        summary = summary.rstrip() + f"\n\nFORK-PROBE: {a.probe_word}\n"
        prompt = a.prompt or ("Without running any tools: quote, exactly, the line of your "
                              "handoff summary that starts with 'FORK-PROBE:'.")
    rj = json.loads((bdir.parent / "run.json").read_text())
    task = IR.load_task(rj["task"]["task_id"])
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out or bdir.parent / "forks" / f"{bdir.name}-{stamp}")
    cap = SMOKE_SIM_CAP_USD if a.smoke else a.sim_cap
    spend = RunSpend(IR.PilotSpend(), cap).attach(
        IR.spend_path(bdir.parent.parent, f"{rj['run_key']}-{bdir.name}-fork-{stamp}"))
    res = X.fork(bdir, out, new_summary=summary, prompt=prompt,
                 sim_model=OpenAICompatModel(C.SIM_ROUTE, spend), persona=task.persona,
                 host=a.host, timeout_s=a.timeout)
    text = X.final_text(out / "events.jsonl") if (out / "events.jsonl").exists() else ""
    rep = {k: res.get(k) for k in ("rc", "thread", "input_thread", "image", "errors",
                                   "cleanup_unconfirmed")}
    rep["final_text"] = text[:1000]
    if a.probe_word:
        rep["probe_word_seen"] = a.probe_word in text
    (out / "report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))
    print("fork dir:", out)
    return 0 if res.get("rc") == 0 and (not a.probe_word or rep["probe_word_seen"]) else 1


def cmd_stage(a) -> int:
    links = X.stage([Path(d) for d in a.run_dirs], Path(a.out))
    for x in links:
        print(f"{x} -> {x.resolve()}")
    print(f"{len(links)} boundaries staged under {a.out}")
    return 0 if links else 1


BATCH_LOG = C.REPO_ROOT / "logs" / "s2" / "native_codex_batch"


class _Adopted:
    """A `base` run another driver launched: done when its process is gone (pgrep)."""

    def __init__(self, task: str):
        import subprocess
        self.task, self.returncode = task, None
        p = subprocess.run(["pgrep", "-fl", f"s2_native_codex.py base .*--task {task} "],
                           capture_output=True, text=True, check=False)
        m = re.search(r"--sim-cap ([0-9.]+)", p.stdout)
        self.cap = float(m.group(1)) if m else 2.5

    def poll(self):
        import subprocess
        p = subprocess.run(["pgrep", "-f", f"s2_native_codex.py base .*--task {self.task} "],
                           capture_output=True, text=True, check=False)
        if p.stdout.strip():
            return None
        self.returncode = "adopted"
        return 0


def _run_summary(run_dir: Path, grade_dir: Path) -> dict:
    res = json.loads((run_dir / "result.json").read_text())
    rows = []
    lp = grade_dir / "losses.jsonl"
    if lp.exists():
        rows = [json.loads(x) for x in lp.read_text().splitlines() if x.strip()]
    firsts = sorted((int(b.name[1:]), json.loads((b / "boundary" / "boundary.json").read_text()))
                    for b in run_dir.glob("b*") if (b / "boundary" / "boundary.json").exists())
    led = X.loss_ledger(rows)
    valid = [r for r in rows if r.get("valid") is True or str(r.get("valid")) == "True"]
    return {"run": run_dir.name, "end": res.get("end"), "sim_usd": res.get("sim_cost_usd"),
            "compactions": res.get("compactions_in_rollouts"),
            "boundaries": len(res.get("boundaries") or []),
            "first_pre_tokens": firsts[0][1].get("pre_tokens") if firsts else None,
            "items": len(valid), "lost": sum(1 for r in valid if r.get("lost") is True
                                             or str(r.get("lost")) == "True"),
            "new": led["new"], "inherited": led["inherited"], "unmatched": led["unmatched"],
            "graded": lp.exists(), "error": (res.get("error") or "")[-300:] or None,
            "boundary_errors": len(res.get("boundary_errors") or [])}


def cmd_batch(a) -> int:
    import subprocess
    import time
    if not a.go_from_jacob:
        sys.exit("paid command: pass --go-from-jacob")
    tasks = [x for x in a.tasks.split(",") if x]
    adopt = [x for x in (a.adopt or "").split(",") if x]
    stamp = X.utc().replace(":", "")
    logd = BATCH_LOG / stamp
    logd.mkdir(parents=True, exist_ok=True)
    root = X.root()
    pending, running = list(tasks), {t: _Adopted(t) for t in adopt}
    caps = {t: a.sim_cap for t in tasks}
    held = False
    relaunched: set[str] = set()
    summaries: list[dict] = []

    def spent() -> float:
        return sum(s.get("sim_usd") or 0.0 for s in summaries)

    def say(msg: str) -> None:
        line = f"[{X.utc()}] {msg}"
        print(line, flush=True)
        with open(logd / "status.log", "a") as f:
            f.write(line + "\n")

    def grade(task: str) -> dict | None:
        runs = sorted(root.glob(f"{task}-codex-luna-a*"), key=lambda p: p.stat().st_mtime)
        if not runs or not (runs[-1] / "result.json").exists():
            say(f"{task}: no result.json")
            return None
        rd = runs[-1]
        st = OUT / "stage" / rd.name
        gd = OUT / "lossaudit" / rd.name
        links = X.stage([rd], st)
        if links:
            g = subprocess.run(["uv", "run", "python", "scripts/s2_lossaudit.py", "run", "--max",
                                "--no-grep", "--raw", str(st), "--out", str(gd)],
                               cwd=C.REPO_ROOT, capture_output=True, text=True, check=False)
            (logd / f"{task}.lossaudit.log").write_text(g.stdout + g.stderr)
        s = _run_summary(rd, gd)
        summaries.append(s)
        (logd / "summaries.json").write_text(json.dumps(summaries, indent=1))
        say(f"{task}: end={s['end']} compactions={s['compactions']} items={s['items']} "
            f"lost={s['lost']} new={s['new']} inherited={s['inherited']} "
            f"unmatched={s['unmatched']} first_pre_tokens={s['first_pre_tokens']} "
            f"sim=${s['sim_usd']} boundary_errors={s['boundary_errors']}")
        return s

    while pending or running:
        for task, p in list(running.items()):
            if p.poll() is not None:
                running.pop(task)
                say(f"{task}: base exited rc={p.returncode}")
                s = grade(task)
                cap_was = p.cap if isinstance(p, _Adopted) else caps.get(task)
                if (a.relaunch_cap and s and s["end"] == "run_cap"
                        and (s["compactions"] or 0) < 3 and task not in relaunched
                        and (cap_was or 0) < a.relaunch_cap):
                    relaunched.add(task)
                    caps[task] = a.relaunch_cap
                    pending.insert(0, task)
                    say(f"{task}: RELAUNCH at ${a.relaunch_cap} (ended run_cap after "
                        f"{s['compactions']} compactions at ${cap_was})")
        if pending and len(running) < a.concurrency:
            committed = (spent() + sum(p.cap if isinstance(p, _Adopted) else caps[t]
                                       for t, p in running.items())
                         + sum(caps[t] for t in pending))
            if committed > a.spend_stop:
                # hold, don't drop: the projection falls as running runs finish under cap
                if not held:
                    say(f"HOLD: projected simulator spend ${committed:.2f} > ${a.spend_stop}; "
                        f"waiting with {pending}")
                held = True
                if not running:
                    say(f"STOP: nothing running and still over ${a.spend_stop}; not launching "
                        f"{pending}")
                    pending = []
                time.sleep(30)
                continue
            held = False
            try:
                free = _free_gb(a.host)
            except Exception as e:  # noqa: BLE001 - a failed check just waits
                free = -1
                say(f"free check failed: {e}")
            if free >= MIN_FREE_GB:
                task = pending.pop(0)
                cmd = ["uv", "run", "--with", "fastmcp==4.1.3", "python",
                       "scripts/s2_native_codex.py", "base", "--go-from-jacob", "--task", task,
                       "--limit", str(a.limit), "--recite-repeats", str(a.recite_repeats),
                       "--sim-cap", str(caps[task]), "--host", a.host]
                if a.implement_timeout:
                    cmd += ["--implement-timeout", str(a.implement_timeout)]
                with open(logd / f"{task}.log", "w") as fh:  # the child keeps its own fd
                    running[task] = subprocess.Popen(cmd, cwd=C.REPO_ROOT, stdout=fh,
                                                     stderr=subprocess.STDOUT)
                say(f"{task}: launched (desktop {free} GB available)")
                time.sleep(60)  # let it start its container before the next gate check
                continue
        time.sleep(30)
    say(f"DONE: {len(summaries)} runs, simulator ${spent():.4f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--repeats", type=int, default=3)
    v.add_argument("--dump-lines", type=int, default=3000)
    v.set_defaults(fn=cmd_validate)
    lo = sub.add_parser("local")
    lo.add_argument("--dump-lines", type=int, default=3000)
    lo.set_defaults(fn=cmd_local)
    b = sub.add_parser("base")
    b.add_argument("--host", default="ssh://rig")
    b.add_argument("--task", default=SMOKE_TASK)
    b.add_argument("--limit", type=int, default=COMMON_LIMIT,
                   help="model_auto_compact_token_limit (tokens; default the common 150k, "
                        "Jacob 2026-10-24)")
    b.add_argument("--model", default=X.MODEL)
    b.add_argument("--effort", default=X.EFFORT)
    b.add_argument("--stop-after", type=int)
    b.add_argument("--implement-timeout", type=int,
                   help="02_implement step timeout in seconds (default task.toml's 5400)")
    b.add_argument("--recite-repeats", type=int, default=3,
                   help="recitations per boundary; the majority one is graded (default 3)")
    b.add_argument("--sim-cap", type=float, default=C.INTERACT_BASE_CAP_USD)
    b.add_argument("--root")
    b.add_argument("--smoke", action="store_true")
    b.add_argument("--go-from-jacob", action="store_true")
    b.set_defaults(fn=cmd_base)
    bt = sub.add_parser("batch")
    bt.add_argument("--tasks", required=True)
    bt.add_argument("--host", default="ssh://rig")
    bt.add_argument("--concurrency", type=int, default=3)
    bt.add_argument("--limit", type=int, default=COMMON_LIMIT)
    bt.add_argument("--recite-repeats", type=int, default=3)
    bt.add_argument("--sim-cap", type=float, default=0.8)
    bt.add_argument("--spend-stop", type=float, default=5.0)
    bt.add_argument("--implement-timeout", type=int,
                    help="02_implement step timeout in seconds (default task.toml's 5400)")
    bt.add_argument("--relaunch-cap", type=float,
                    help="relaunch a run that ended run_cap before 3 compactions at this cap")
    bt.add_argument("--adopt", help="tasks whose base runs are already going (grade on exit)")
    bt.add_argument("--go-from-jacob", action="store_true")
    bt.set_defaults(fn=cmd_batch)
    st = sub.add_parser("stage")
    st.add_argument("run_dirs", nargs="+")
    st.add_argument("--out", default=str(OUT / "stage"))
    st.set_defaults(fn=cmd_stage)
    f = sub.add_parser("fork")
    f.add_argument("bdir")
    f.add_argument("--host", default="ssh://rig")
    g = f.add_mutually_exclusive_group()
    g.add_argument("--summary-file")
    g.add_argument("--probe-word")
    f.add_argument("--prompt")
    f.add_argument("--out")
    f.add_argument("--timeout", type=int, default=1800)
    f.add_argument("--sim-cap", type=float, default=C.INTERACT_SECONDARY_CAP_USD)
    f.add_argument("--smoke", action="store_true")
    f.add_argument("--go-from-jacob", action="store_true")
    f.set_defaults(fn=cmd_fork)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
