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
  rerecite RUN_DIR...  top every boundary up to 3 valid recitations from its saved rollout
             ($0), re-pick the majority and re-derive the boundary (originals kept).
  fork BDIR  Q2: resume boundary BDIR from its kept image with the encrypted summary replaced
             by readable text (the boundary's recited summary unless --summary-file), in a new
             thread, given --prompt. --probe-word W appends "FORK-PROBE: <W>" and asks the fork
             to quote that line without tools (the round-trip check). Paid like `base`.

Outputs go to data/raw/sprint/s2/native/codex/<command>/<utc>/ (gitignored).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
import time
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


LOCAL = "local"  # --host local: the laptop's own Docker (Docker Desktop, amd64 emulated)
LAPTOP = {"max_containers": 3, "max_mem_gb": 12.0, "max_cpus": 8.0, "min_free_pct": 30,
          "max_load1": 12.0,  # the main session, 24 Oct: hard, for all agents combined
          "min_host_disk_gb": 25.0, "min_docker_disk_gb": 20.0}  # and disk (24 Oct)


def docker_disk_free_gb() -> float | None:
    """Free space on Docker Desktop's VM disk (where images and kept snapshots live), from a
    `df` in a throwaway container of the local egress image (--rm, a name, an emx label, no
    network). None if it can't be measured (the gate then refuses)."""
    import subprocess
    import uuid

    from compaction.s2.native import common as N
    name = f"native-codex-dfprobe-{uuid.uuid4().hex[:8]}"
    try:
        p = subprocess.run(["docker", "run", "--rm", "--name", name, "--label",
                            "emx.sandbox=native-codex-dfprobe", "--network", "none",
                            "--memory", "64m", "--cpus", "0.25",
                            "--entrypoint", "df", N.egress_image(), "-k", "/"],
                           capture_output=True, text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60,
                           check=False)
        return None
    rows = [x.split() for x in p.stdout.splitlines()[1:] if x.strip()]
    try:
        return int(rows[-1][3]) / 2**20
    except (IndexError, ValueError):
        return None
SIDECAR_MEM_GB, SIDECAR_CPUS = 0.125, 0.5  # common.Egress's sidecar limits


def _docker_host(host: str | None) -> str | None:
    return None if host in (None, "", LOCAL) else host


RESERVATIONS = C.REPO_ROOT / "logs" / "s2" / "native_codex_reservations.json"
# A laptop reservation counts (2 containers + its limits) until the run marks it visible,
# once its containers are up and `docker ps` counts them; a dead launcher's is dropped.


def _live(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def reservations() -> list[dict]:
    """Live launch reservations of every Codex driver: {pid, host, cap, mem_gb, cpus, ts}
    (dead pids dropped). Read and written under `_launch_lock`."""
    try:
        rows = json.loads(RESERVATIONS.read_text())
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and _live(r.get("pid", 0))]


def reserve(pid: int, *, host: str | None, cap: float, mem_gb: float, cpus: float) -> None:
    rows = reservations() + [{"pid": int(pid), "host": host or LOCAL, "cap": cap,
                              "mem_gb": mem_gb, "cpus": cpus, "ts": time.time(),
                              "visible": False}]
    RESERVATIONS.parent.mkdir(parents=True, exist_ok=True)
    RESERVATIONS.write_text(json.dumps(rows, indent=1))


def mark_visible(pids: set[int]) -> None:
    """The run's containers are up (docker ps counts them now): stop counting its
    reservation's containers and limits, keep its cap for the tag gate. Under the lock."""
    with _launch_lock():
        rows = reservations()
        for r in rows:
            if r.get("pid") in pids:
                r["visible"] = True
        RESERVATIONS.write_text(json.dumps(rows, indent=1))


def laptop_gate(mem_gb: float, cpus: float) -> tuple[bool, str]:
    """May one more native session (task container + egress sidecar) start on the laptop?
    Fails closed: any docker error, an emx container without memory or CPU limits, or a
    missing reading holds. Counts every emx container already there (all agents) plus this
    tool's laptop reservations whose containers can't be seen yet (younger than
    RESERVE_VISIBLE_S), sums their limits, then checks memory_pressure, load and disk. Cheap
    checks first; the disk probe (a bounded container) only when everything else passes.
    Call under `_launch_lock` and reserve before releasing it."""
    import shutil
    import subprocess

    from compaction.s2.chain import laptop_admission as LA
    try:
        busy = LA.controls_active()  # one batch at a time on the laptop (week 2)
        if busy:
            return False, f"week-2 controls on the laptop: {busy}; not launching"
        p = subprocess.run(["docker", "ps", "--filter", "label=emx.sandbox", "-q"],
                           capture_output=True, text=True, timeout=60, check=False)
        if p.returncode != 0:
            return False, f"docker ps failed rc={p.returncode}: {p.stderr.strip()[-200:]}"
        ids = p.stdout.split()
        used_mem = used_cpu = 0.0
        runs: set[str] = set()
        if ids:
            q = subprocess.run(["docker", "inspect", "--format",
                                ("{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} "
                                 "{{index .Config.Labels \"emx.run\"}}"), *ids],
                               capture_output=True, text=True, timeout=60, check=False)
            lines = q.stdout.splitlines()
            if q.returncode != 0 or len(lines) != len(ids):
                return False, f"docker inspect incomplete rc={q.returncode} ({len(lines)}/{len(ids)})"
            for line in lines:
                parts = line.split()
                m_b, c_n = int(parts[0]), int(parts[1])
                if len(parts) > 2 and parts[2] != "<no value>":
                    runs.add(parts[2])  # this snapshot's run labels, for week-2 pending
                if m_b <= 0 or c_n <= 0:
                    return False, "an emx container has no memory or CPU limit (unbounded)"
                used_mem += m_b / 2**30
                used_cpu += c_n / 1e9
        pend = [r for r in reservations() if r.get("host") == LOCAL and not r.get("visible")]
        n = len(ids) + 2 * len(pend)
        used_mem += sum(r["mem_gb"] + SIDECAR_MEM_GB for r in pend)
        used_cpu += sum(r["cpus"] + SIDECAR_CPUS for r in pend)
        # week-2 laptop admission's launches not yet visible in THIS snapshot (one lock:
        # _launch_lock), after its reconciliation
        wk2 = LA.pending_summary(runs)
        n += wk2["n"]
        used_mem += wk2["mem_gb"]
        used_cpu += wk2["cpus"]
        mp = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=60,
                            check=False).stdout
        m = re.search(r"free percentage:\s*(\d+)%", mp)
        free_pct = int(m.group(1)) if m else -1
        load1 = os.getloadavg()[0]
        host_disk = shutil.disk_usage("/").free / 2**30
    except Exception as e:  # noqa: BLE001 - an unknown reading holds, never launches
        return False, f"gate reading failed: {type(e).__name__}: {e}"
    orph = (f" (unresolved orphans: {LA.orphan_text(wk2['orphans'])})"
            if wk2["orphans"] else "")
    why = (f"{len(ids)} emx containers + {len(pend)} pending sessions + {wk2['n']} week-2 "
           f"pending{orph}, {used_mem:.1f} GB / "
           f"{used_cpu:.1f} CPUs limits, {free_pct}% memory free, load {load1:.1f}, "
           f"host disk {host_disk:.0f} GB free")
    cheap = (n + 2 <= LAPTOP["max_containers"]
             and used_mem + mem_gb + SIDECAR_MEM_GB <= LAPTOP["max_mem_gb"]
             and used_cpu + cpus + SIDECAR_CPUS <= LAPTOP["max_cpus"]
             and free_pct >= LAPTOP["min_free_pct"] and load1 < LAPTOP["max_load1"]
             and host_disk >= LAPTOP["min_host_disk_gb"])
    if not cheap:
        return False, why
    try:
        vm_disk = docker_disk_free_gb()
    except Exception as e:  # noqa: BLE001 - an unknown reading holds
        return False, why + f", docker disk probe failed: {type(e).__name__}: {e}"
    why += f", docker disk {'?' if vm_disk is None else f'{vm_disk:.0f}'} GB free"
    return vm_disk is not None and vm_disk >= LAPTOP["min_docker_disk_gb"], why


def _free_gb(host: str) -> int:
    import subprocess
    if _docker_host(host) is None:
        return 999  # the laptop: `laptop_gate` decides
    ssh = host.removeprefix("ssh://")
    p = subprocess.run(["ssh", ssh, "free", "-g"], capture_output=True, text=True, timeout=60,
                       check=True)
    row = next(x for x in p.stdout.splitlines() if x.startswith("Mem:")).split()
    return int(row[6])


def _labels(a) -> dict:
    """run.json labels: the --label pairs, plus host=laptop for --host local and a
    trigger_sensitivity for any non-150k limit when not given (arms stay identifiable)."""
    lab = dict(x.split("=", 1) for x in (a.label or []))
    if _docker_host(a.host) is None:
        lab.setdefault("host", "laptop")
    if a.limit and a.limit != COMMON_LIMIT:
        lab.setdefault("trigger_sensitivity", f"{a.limit // 1000}k")
    return lab


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
    if a.limit and a.limit != COMMON_LIMIT and not a.root:
        sys.exit("a non-150k run needs its own --root (arms are never mixed)")
    if _docker_host(a.host) is None and not a.gate_reserved:
        with _launch_lock():
            ok, why = laptop_gate(a.memory_mb / 1024, a.cpus)
            if not ok:
                sys.exit(f"laptop gate: {why}; not starting")
            reserve(os.getpid(), host=None, cap=a.sim_cap, mem_gb=a.memory_mb / 1024,
                    cpus=a.cpus)
    else:
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
                       host=_docker_host(a.host), recite_repeats=a.recite_repeats,
                       stop_after=a.stop_after if a.stop_after is not None
                       else (1 if a.smoke else None),
                       cpus=a.cpus, memory_mb=a.memory_mb,
                       labels=_labels(a))
    if a.implement_timeout:
        cfg.step_timeout_s["02_implement"] = int(a.implement_timeout)
    cfg.on_started = lambda: mark_visible({os.getpid(), os.getppid()})
    if a.run_dir_file:  # the driver grades exactly this execution
        Path(a.run_dir_file).write_text(str(cfg.run_dir))
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


def _audit_backend():
    """scripts/s2_interact_audit.py's Max backend (claude -p, $0), as native-arms' arm uses."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "s2_interact_audit", C.REPO_ROOT / "scripts" / "s2_interact_audit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.max_backend(4)


def _base_proc_table() -> list[tuple[int, int, str]]:
    """(pid, ppid, command) of the `base` runs' Python processes on this laptop (not the uv
    wrappers, so a direct launch counts once too)."""
    import subprocess
    p = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True,
                       check=False)
    out = []
    for line in p.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and "s2_native_codex.py base " in parts[2] \
                and "/bin/python" in parts[2].split(" ", 1)[0]:
            out.append((int(parts[0]), int(parts[1]), parts[2]))
    return out


def _base_procs() -> list[str]:
    return [c for _, _, c in _base_proc_table()]


def _matches(cmdline: str, task: str, root: str | None) -> bool:
    if f"--task {task} " not in cmdline + " ":
        return False
    return f"--root {root} " in cmdline + " " if root else "--root " not in cmdline


LAUNCH_LOCK = C.REPO_ROOT / "logs" / "s2" / "native_codex_launch.lock"
LAUNCH_REGISTRY = C.REPO_ROOT / "logs" / "s2" / "native_codex_launches.json"


def _all_base_runs() -> int:
    """`base` runs going on this laptop, from any driver or arm: the live launches in the
    shared registry (counted from registration, before their Python process exists) plus the
    Python runs no registered launch started."""
    alive: set[int] = set()
    with contextlib.suppress(OSError, ValueError):
        for pid in json.loads(LAUNCH_REGISTRY.read_text()):
            with contextlib.suppress(OSError):
                os.kill(int(pid), 0)
                alive.add(int(pid))
    # the union: every live registered launch, plus every Python run not started by one
    # (runs launched before the registry existed)
    others = [pid for pid, ppid, _ in _base_proc_table() if ppid not in alive and pid not in alive]
    return len(alive) + len(others)


@contextlib.contextmanager
def _launch_lock():
    """Serialises the global-concurrency check and the launch across drivers, and with the
    week-2 laptop admission (its machine-wide flock, taken second: that module never takes
    this lock, so the order can't deadlock)."""
    import fcntl

    from compaction.s2.chain import laptop_admission as LA
    LAUNCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCH_LOCK, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            with LA.locked():
                yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _register(pid: int) -> None:
    pids = []
    with contextlib.suppress(OSError, ValueError):
        pids = json.loads(LAUNCH_REGISTRY.read_text())
    live = []
    for x in pids + [pid]:
        with contextlib.suppress(OSError):
            os.kill(int(x), 0)
            live.append(int(x))
    LAUNCH_REGISTRY.write_text(json.dumps(sorted(set(live))))


class _Adopted:
    """A `base` run another driver launched, identified by task AND run root (so the 150k and
    80k arms never adopt each other's runs): done when its process is gone."""

    def __init__(self, task: str, root: str | None = None):
        self.task, self.root, self.returncode = task, root, None
        rows = [(pid, ppid, c) for pid, ppid, c in _base_proc_table() if _matches(c, task, root)]
        m = re.search(r"--sim-cap ([0-9.]+)", " ".join(c for _, _, c in rows))
        self.cap = float(m.group(1)) if m else 6.5
        self.pids = {x for pid, ppid, _ in rows for x in (pid, ppid)}  # its reservation's key

    def poll(self):
        if any(_matches(x, self.task, self.root) for x in _base_procs()):
            return None
        self.returncode = "adopted"
        return 0


def prune_kept_images(rd: Path, logd: Path, say, *, q1_only: bool = False) -> list[dict]:
    """The laptop arm's disk rule (main session, 24 Oct): once a run's lossaudit and ledger
    audit are written, remove its kept boundary images that are not fork candidates. A
    boundary is removed only if boundary.json says `eligible: false` explicitly, or if the arm
    is declared Q1-only (`q1_only`: no boundary of it is ever forked, so an unevaluated
    `eligible: null` counts as no) and it isn't `eligible: true`. Each removal is logged with
    the image digest (<logd>/pruned.jsonl). Local Docker only (the caller checks)."""
    import subprocess
    out = []
    for b in sorted(rd.glob("b*")):
        bj = b / "boundary" / "boundary.json"
        if not bj.exists():
            continue
        f = json.loads(bj.read_text())
        tag = f.get("tag")
        if not tag:
            continue
        elig = f.get("eligible")
        if elig is True or (elig is None and not q1_only):
            out.append({"run": rd.name, "boundary": b.name, "tag": tag,
                        "kept": "fork candidate" if elig else "eligibility not evaluated"})
            continue
        q = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                           capture_output=True, text=True, timeout=60, check=False)
        if q.returncode != 0:
            continue  # already gone
        digest = q.stdout.strip()
        r = subprocess.run(["docker", "rmi", tag], capture_output=True, text=True,
                           timeout=300, check=False)
        row = {"utc": X.utc(), "run": rd.name, "boundary": b.name, "tag": tag,
               "digest": digest, "removed": r.returncode == 0,
               "why": "eligible false" if elig is False else "q1-only arm",
               "error": r.stderr.strip()[-300:] or None}
        out.append(row)
        with open(logd / "pruned.jsonl", "a") as fh:
            fh.write(json.dumps(row) + "\n")
        say(f"{rd.name}/{b.name}: kept image {'removed' if row['removed'] else 'NOT removed'} "
            f"({tag} {digest[:19]})")
    return out


def _tail(p: Path, n: int = 20000) -> str:
    try:
        return p.read_text(errors="replace")[-n:]
    except OSError:
        return ""


def _run_summary(run_dir: Path, grade_dir: Path | None) -> dict:
    """One run's status. `grade_dir` None means lossaudit did not complete for this run: its
    counts are then None, never zero."""
    res = json.loads((run_dir / "result.json").read_text())
    rows = []
    lp = grade_dir / "losses.jsonl" if grade_dir is not None else None
    if lp is not None and lp.exists():
        rows = [json.loads(x) for x in lp.read_text().splitlines() if x.strip()]
    firsts = sorted((int(b.name[1:]), json.loads((b / "boundary" / "boundary.json").read_text()))
                    for b in run_dir.glob("b*") if (b / "boundary" / "boundary.json").exists())
    led = X.loss_ledger(rows)
    lim = (res.get("timings") and json.loads((run_dir / "run.json").read_text())
           .get("harness", {}).get("step_timeout_s")) or X.STEP_TIMEOUT_S
    early = [st for st, v in (res.get("steps") or {}).items()
             if v.get("rc") == 137 and not v.get("killed")
             and (v.get("seconds") or 0) < lim.get(st, 5400) - 30]
    end = res.get("end")
    if end == "time_cap" and early:
        end = "infra_killed"  # a run on the old classifier: SIGKILL well before the limit
    transport = [st for st in (res.get("steps") or {})
                 if "dial-stdio" in _tail(run_dir / "steps" / st / "events.jsonl.err")]
    if end == "cli_error" and transport:
        end = "infra_transport"  # the docker transport (ssh) dropped; the CLI was still going
    valid = [r for r in rows if r.get("valid") is True or str(r.get("valid")) == "True"]
    peaks = {}
    for st in (res.get("steps") or {}):
        rp = run_dir / "steps" / st / "rollout.jsonl"
        try:
            lines = X.read_rollout(rp) if rp.exists() and rp.stat().st_size else []
        except ValueError:
            lines = []
        toks = [x["payload"]["info"]["last_token_usage"]["input_tokens"] for x in lines
                if x.get("type") == "event_msg" and x["payload"].get("type") == "token_count"
                and (x["payload"].get("info") or {}).get("last_token_usage")]
        if toks:
            i = max(range(len(toks)), key=toks.__getitem__)
            peaks[st] = {"peak_tokens": toks[i], "peak_call": i + 1, "calls": len(toks)}
    return {"run": run_dir.name, "end": end, "end_recorded": res.get("end"), "peaks": peaks,
            "transport_drop_steps": transport,
            "early_kill_steps": early, "sim_usd": res.get("sim_cost_usd"),
            "compactions": res.get("compactions_in_rollouts"),
            "boundaries": len(res.get("boundaries") or []),
            "first_pre_tokens": firsts[0][1].get("pre_tokens") if firsts else None,
            "items": len(valid) if lp is not None else None,
            "lost": (sum(1 for r in valid if r.get("lost") is True
                         or str(r.get("lost")) == "True") if lp is not None else None),
            "new": led["new"] if lp is not None else None,
            "inherited": led["inherited"] if lp is not None else None,
            "unmatched": led["unmatched"] if lp is not None else None,
            "graded": lp is not None, "error": (res.get("error") or "")[-300:] or None,
            "boundary_errors": len(res.get("boundary_errors") or [])}


def cmd_batch(a) -> int:
    import subprocess
    import time
    if not a.go_from_jacob:
        sys.exit("paid command: pass --go-from-jacob")
    if a.prune_kept and _docker_host(a.host) is not None:
        sys.exit("--prune-kept is for the laptop's local Docker only")
    if a.limit != COMMON_LIMIT and not a.root:
        sys.exit("a non-150k arm needs its own --root (arms are never mixed)")
    tasks = [x for x in a.tasks.split(",") if x]
    adopt = [x for x in (a.adopt or "").split(",") if x]
    stamp = X.utc().replace(":", "")
    logd = BATCH_LOG / stamp
    logd.mkdir(parents=True, exist_ok=True)
    root = Path(a.root) if a.root else X.root()
    out_root = root if a.root else OUT
    pending, running = list(tasks), {t: _Adopted(t, a.root) for t in adopt}
    lock = __import__("threading").Lock()
    caps = {t: a.sim_cap for t in tasks}
    held = False
    relaunched: set[str] = set()
    reran: set[str] = {x for x in (a.rerun or "").split(",") if x}
    heavy: set[str] = {x for x in (a.heavy or "").split(",") if x} | reran
    mem: dict[str, int] = {t: a.rerun_memory_mb for t in heavy}
    summaries: list[dict] = []

    def spent() -> float:
        return sum(s.get("sim_usd") or 0.0 for s in summaries)

    def say(msg: str) -> None:
        line = f"[{X.utc()}] {msg}"
        print(line, flush=True)
        with open(logd / "status.log", "a") as f:
            f.write(line + "\n")

    def run_dir_of(task: str, adopted: bool) -> tuple[Path | None, str]:
        """The exact run dir of a launch (the file `base --run-dir-file` wrote). Only a run
        adopted from an earlier driver falls back to the latest attempt under this arm's
        root; a launch that never wrote its file has no run dir (a failed launch, recorded),
        never an older attempt."""
        f = logd / f"{task}.rundir"
        if f.exists() and f.read_text().strip():
            return Path(f.read_text().strip()), "exact"
        if not adopted:
            return None, "launch_without_run_dir"
        runs = sorted(root.glob(f"{task}-codex-luna-a*"), key=lambda p: p.stat().st_mtime)
        return (runs[-1] if runs else None), "latest_attempt"

    def grade(task: str, adopted: bool = False) -> dict | None:
        rd, ident = run_dir_of(task, adopted)
        if rd is None or not (rd / "result.json").exists():
            say(f"{task}: no result.json ({ident}: {rd})")
            return None
        st = out_root / "stage" / rd.name
        gd = out_root / "lossaudit" / rd.name
        links = X.stage([rd], st)
        reqs: list[dict] | None = []
        la_ok = True
        if links:
            from compaction.s2.native import audit as AU
            t0 = time.time()
            # (b) lossaudit on Max, with the recoverability grep on the kept images (rig)
            rc = AU.run_lossaudit(st, gd, docker_host=_docker_host(a.host),
                                  log=logd / f"{task}.lossaudit.log")
            lp = gd / "losses.jsonl"
            covered = set()
            if lp.exists():
                for line in lp.read_text().splitlines():
                    with contextlib.suppress(ValueError):
                        covered.add(Path(json.loads(line).get("boundary_dir") or "").name)
            la_ok = (rc == 0 and lp.exists() and lp.stat().st_mtime >= t0
                     and covered >= {x.resolve().name for x in links})
            if not la_ok:
                say(f"{task}: lossaudit INCOMPLETE (rc={rc}, covered {sorted(covered)} of "
                    f"{len(links)} boundaries); its counts are reported as None")
            # (a) the requirement ledger on the live (majority-recited) summary, on Max
            try:
                reqs = AU.requirements(rd, lambda: _audit_backend())
            except Exception as e:  # noqa: BLE001 - recorded as None, never as zero
                say(f"{task}: requirement ledger FAILED: {type(e).__name__}: {e}")
                reqs = None
            (gd / "requirements_ledger.json").write_text(json.dumps(reqs, indent=1))
        if a.prune_kept and links and la_ok and reqs is not None:
            prune_kept_images(rd, logd, say, q1_only=a.prune_kept == "q1-only")
        s = _run_summary(rd, gd if la_ok else None)
        s["identity"] = ident
        s["req_rows"] = reqs
        for key, field_ in (("req_lost", "lost"), ("req_new", "lost_new"),
                            ("req_inherited", "lost_inherited"),
                            ("req_unknown", "lost_unknown"),
                            ("req_by_reply", "lost_by_reply")):
            s[key] = (sum(r.get(field_) or 0 for r in reqs) if reqs is not None else None)
        with lock:
            summaries.append(s)
            (logd / "summaries.json").write_text(json.dumps(summaries, indent=1))
        say(f"{task}: end={s['end']} compactions={s['compactions']} items={s['items']} "
            f"lost={s['lost']} new={s['new']} inherited={s['inherited']} "
            f"unmatched={s['unmatched']} | reqs: lost={s['req_lost']} new={s['req_new']} "
            f"inherited={s['req_inherited']} unknown={s['req_unknown']} "
            f"by_reply={s['req_by_reply']} | "
            f"first_pre_tokens={s['first_pre_tokens']} peak_02="
            f"{(s['peaks'].get('02_implement') or {}).get('peak_tokens')}@"
            f"{(s['peaks'].get('02_implement') or {}).get('peak_call')}"
            f"/{(s['peaks'].get('02_implement') or {}).get('calls')} "
            f"sim=${s['sim_usd']} boundary_errors={s['boundary_errors']} ({ident})")
        return s

    def tag_need(task: str) -> tuple[float, str]:
        """The pilot tag gate (main session, 24 Oct): charged + held on the tag, plus every
        in-flight cap (this driver's, my other drivers' reservations, --external-inflight for
        other arms), plus the next run's cap. Checked before, and again under the launch
        lock right before, every launch."""
        from compaction.s2 import config as Cfg
        from compaction.s2 import interact_run as IR
        charged, held_usd = IR.tag_totals(Cfg.INTERACT_TAG)
        mine = sum(p.cap if isinstance(p, _Adopted) else caps[t] for t, p in running.items())
        my_pids = {x for p in running.values()
                   for x in (getattr(p, "pids", None) or {getattr(p, "pid", None)})}
        others = sum(r.get("cap", 0) for r in reservations() if r.get("pid") not in my_pids)
        need = charged + held_usd + mine + others + a.external_inflight + caps[task]
        return need, (f"${charged:.2f} charged + ${held_usd:.2f} held + ${mine:.2f} mine + "
                      f"${others:.2f} my other drivers + ${a.external_inflight:.2f} other arms"
                      f" + ${caps[task]:.2f} next = ${need:.2f} > ${a.tag_limit}")

    from concurrent.futures import ThreadPoolExecutor
    grader = ThreadPoolExecutor(max_workers=1)  # grading never blocks scheduling
    graded: list[tuple[str, object, object]] = []  # (task, future, the process object)

    while pending or running or graded:
        for task, p in list(running.items()):
            if p.poll() is not None:
                running.pop(task)
                say(f"{task}: base exited rc={p.returncode}")
                graded.append((task, grader.submit(grade, task, isinstance(p, _Adopted)), p))
        for item in [g for g in graded if g[1].done()]:
            graded.remove(item)
            task, fut, p = item
            try:
                s = fut.result()
            except Exception as e:  # noqa: BLE001 - a grading crash is reported, not fatal
                say(f"{task}: grading crashed: {type(e).__name__}: {e}")
                s = None
            if s is None:  # keep its spend in the projection: actual if known, else its cap
                usd = p.cap if isinstance(p, _Adopted) else caps.get(task, a.sim_cap)
                rd, _ = run_dir_of(task, isinstance(p, _Adopted))
                with contextlib.suppress(Exception):
                    usd = json.loads((rd / "result.json").read_text())["sim_cost_usd"]
                with lock:
                    summaries.append({"run": str(rd), "end": "ungraded", "sim_usd": usd})
                    (logd / "summaries.json").write_text(json.dumps(summaries, indent=1))
            cap_was = p.cap if isinstance(p, _Adopted) else caps.get(task)
            if (s and s["end"] in ("infra_killed", "infra_error", "infra_transport")
                    and task not in reran):
                reran.add(task)
                heavy.add(task)
                mem[task] = a.rerun_memory_mb
                caps[task] = max(caps.get(task) or 0, a.relaunch_cap or a.sim_cap)
                pending.insert(0, task)
                say(f"{task}: RERUN (infrastructure: {s['end']}) at {a.rerun_memory_mb} MB, "
                    f"${caps[task]}")
                continue
            if (a.relaunch_cap and s and s["end"] == "run_cap"
                    and (s["compactions"] or 0) < 3 and task not in relaunched
                    and (cap_was or 0) < a.relaunch_cap):
                relaunched.add(task)
                caps[task] = a.relaunch_cap
                pending.insert(0, task)
                say(f"{task}: RELAUNCH at ${a.relaunch_cap} (ended run_cap after "
                    f"{s['compactions']} compactions at ${cap_was})")
        if pending and len(running) < a.concurrency and (
                not a.global_concurrency or _all_base_runs() < a.global_concurrency):
            committed = (spent() + sum(p.cap if isinstance(p, _Adopted) else caps[t]
                                       for t, p in running.items())
                         + sum(p.cap if isinstance(p, _Adopted) else caps.get(t, a.sim_cap)
                               for t, _, p in graded)  # exited, not yet in summaries
                         + sum(caps[t] for t in pending))
            if committed > a.spend_stop:
                # hold, don't drop: the projection falls as running runs finish under cap
                if not held:
                    say(f"HOLD: projected simulator spend ${committed:.2f} > ${a.spend_stop}; "
                        f"waiting with {pending}")
                held = True
                if not running and not graded:
                    say(f"STOP: nothing running and still over ${a.spend_stop}; not launching "
                        f"{pending}")
                    pending = []
                time.sleep(30)
                continue
            if a.tag_limit:
                need, msg = tag_need(pending[0])
                if need > a.tag_limit:
                    if not held:
                        say(f"HOLD (tag): {msg}")
                    held = True
                    time.sleep(60)
                    continue
            held = False
            try:
                free = _free_gb(a.host)
            except Exception as e:  # noqa: BLE001 - a failed check just waits
                free = -1
                say(f"free check failed: {e}")
            gate = a.heavy_free_gb if pending[0] in heavy else MIN_FREE_GB
            if _docker_host(a.host) is None:
                ok, free, gate = True, "laptop (gated at launch)", None  # under the lock
            else:
                ok = free >= gate
            if ok:
                task = pending.pop(0)
                cmd = ["uv", "run", "--with", "fastmcp==4.1.3", "python",
                       "scripts/s2_native_codex.py", "base", "--go-from-jacob", "--task", task,
                       "--limit", str(a.limit), "--recite-repeats", str(a.recite_repeats),
                       "--sim-cap", str(caps[task]), "--host", a.host]
                if a.implement_timeout:
                    cmd += ["--implement-timeout", str(a.implement_timeout)]
                rdf = logd / f"{task}.rundir"
                rdf.unlink(missing_ok=True)
                cmd += ["--run-dir-file", str(rdf)]
                if a.root:
                    cmd += ["--root", a.root]
                for lab in a.label or []:
                    cmd += ["--label", lab]
                if task in mem:  # an infrastructure rerun: the heavy-container rule
                    cmd += ["--memory-mb", str(mem[task]), "--cpus", str(a.heavy_cpus)]
                mem_gb = mem.get(task, 3072) / 1024
                cpus = a.heavy_cpus if task in heavy else 2
                wait_s = 0  # every HOLD sleeps after the lock is released
                with _launch_lock():  # check + launch + reserve, atomic across my drivers
                    if a.global_concurrency and _all_base_runs() >= a.global_concurrency:
                        wait_s = 30
                    elif a.tag_limit and tag_need(task)[0] > a.tag_limit:
                        wait_s = 60  # rechecked under the lock: still over
                    elif _docker_host(a.host) is None:
                        ok, why = laptop_gate(mem_gb, cpus)
                        if not ok:
                            if not held:
                                say(f"HOLD (laptop): {why}")
                            held = True
                            wait_s = 60
                        else:
                            free = why
                            cmd += ["--gate-reserved"]
                    if not wait_s:
                        with open(logd / f"{task}.log", "w") as fh:  # the child keeps its fd
                            running[task] = subprocess.Popen(cmd, cwd=C.REPO_ROOT, stdout=fh,
                                                             stderr=subprocess.STDOUT)
                        _register(running[task].pid)
                        reserve(running[task].pid, host=_docker_host(a.host),
                                cap=caps[task], mem_gb=mem_gb, cpus=cpus)
                if wait_s:
                    pending.insert(0, task)
                    time.sleep(wait_s)
                    continue
                say(f"{task}: launched (desktop {free} GB available)")
                time.sleep(60)  # let it start its container before the next gate check
                continue
        time.sleep(30)
    say(f"DONE: {len(summaries)} runs, simulator ${spent():.4f}")
    return 0


LAPTOP_SMOKE_PROMPT = (
    "Do not ask the user anything and do not change any files. Run these shell commands one "
    "at a time and read their output: `ls -la /app`, `git -C /app log --oneline -n 30`, "
    "`find /app -maxdepth 2 -name '*.py' | head -60`, `sed -n '1,200p' /app/README.rst` (or "
    "the README that exists), `sed -n '1,250p' /app/setup.py` (or pyproject.toml). Then reply "
    "with three bullets describing the repository.")


def cmd_laptop_smoke(a) -> int:
    """$0: one Codex session on the laptop's Docker with a prompt that never asks the
    simulator, a tiny compaction limit so the PostCompact capture runs, stop after b1."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from compaction.s2 import interact_run as IR

    class _NoSim:  # any simulator call would be a bug in this smoke
        def __getattr__(self, name):
            raise RuntimeError(f"the laptop smoke must not call the simulator ({name})")

    with _launch_lock():
        ok, why = laptop_gate(3.0, 2)
        print("laptop gate:", ok, why)
        if not ok:
            return 2
        reserve(os.getpid(), host=None, cap=0.0, mem_gb=3.0, cpus=2)
    task = IR.load_task(a.task)
    task = replace(task, steps={"01_plan": LAPTOP_SMOKE_PROMPT})
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = OUT / "laptop_smoke"
    cfg = X.BaseConfig(task=task, root=root, prefix=f"laptopsmoke-{stamp}-",
                       attempt=1, cx=X.CodexConfig(auto_compact_limit=a.limit),
                       host=None, steps=("01_plan",), stop_after=1, recite_repeats=1,
                       cpus=2, memory_mb=3072,
                       labels={"host": "laptop", "trigger_sensitivity": "laptop-smoke"},
                       on_started=lambda: mark_visible({os.getpid()}))
    res = X.run_base(cfg, sim_model=_NoSim())
    st = res.get("steps", {}).get("01_plan", {})
    out = {k: res.get(k) for k in ("run_key", "end", "boundaries", "sim_cost_usd", "seconds",
                                   "error", "cleanup_unconfirmed")}
    out["egress_ok"] = (res.get("egress_check") or {}).get("ok")
    out["step"] = {k: st.get(k) for k in ("rc", "seconds", "reconnects", "output_incomplete",
                                          "api_calls", "compactions")}
    print(json.dumps(out, indent=1)[:3000])
    print("run dir:", cfg.run_dir)
    return 0 if res.get("boundaries") and res.get("end") == "stop_after" else 1


def cmd_rerecite(a) -> int:
    """$0 (ChatGPT plan): top each boundary of the given runs up to --repeats valid
    recitations from its saved rollout, re-pick the majority, re-derive the boundary files
    (originals under b<k>/native/orig/). Rerun the grading afterwards."""
    rows = []
    for rd in a.run_dirs:
        rows += X.rerecite_run(Path(rd), repeats=a.repeats, reextract=a.reextract)
    bad = [r for r in rows if not r["complete"]]
    for r in rows:
        print(json.dumps(r))
    print(json.dumps({"boundaries": len(rows), "incomplete": len(bad)}))
    return 0 if rows and not bad else 1


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
    b.add_argument("--host", default="ssh://rig", help="docker host, or 'local' (the laptop)")
    b.add_argument("--task", default=SMOKE_TASK)
    b.add_argument("--limit", type=int, default=COMMON_LIMIT,
                   help="model_auto_compact_token_limit (tokens; default the common 150k, "
                        "Jacob 2026-10-24)")
    b.add_argument("--model", default=X.MODEL)
    b.add_argument("--effort", default=X.EFFORT)
    b.add_argument("--stop-after", type=int)
    b.add_argument("--memory-mb", type=int, default=3072)
    b.add_argument("--run-dir-file", help="write this run's directory here (for the driver)")
    b.add_argument("--gate-reserved", action="store_true",
                   help="the driver already passed the laptop gate and reserved this run")
    b.add_argument("--label", action="append",
                   help="KEY=VALUE written at the top level of run.json (repeatable)")
    b.add_argument("--cpus", type=float, default=2)
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
    bt.add_argument("--prune-kept", choices=("explicit", "q1-only"),
                    help="after both audits, remove the run's kept images: 'explicit' only "
                         "boundaries marked eligible=false; 'q1-only' every boundary not "
                         "eligible=true (an arm that is never forked). Local Docker only.")
    bt.add_argument("--tag-limit", type=float,
                    help="hold launches while the pilot tag's charged + held + in-flight caps + "
                         "the next run's cap would pass this")
    bt.add_argument("--external-inflight", type=float, default=0.0,
                    help="other arms' in-flight caps on the pilot tag (from the main session)")
    bt.add_argument("--global-concurrency", type=int,
                    help="launch only while fewer base runs than this go on the laptop, "
                         "counting every driver and arm (the lower-priority arm sets it)")
    bt.add_argument("--limit", type=int, default=COMMON_LIMIT)
    bt.add_argument("--recite-repeats", type=int, default=3)
    bt.add_argument("--sim-cap", type=float, default=0.8)
    bt.add_argument("--spend-stop", type=float, default=5.0)
    bt.add_argument("--implement-timeout", type=int,
                    help="02_implement step timeout in seconds (default task.toml's 5400)")
    bt.add_argument("--root", help="run root (default the main arm's); stage/ and lossaudit/ "
                    "go under it too, so arms never mix")
    bt.add_argument("--label", action="append", help="KEY=VALUE passed to every base run")
    bt.add_argument("--heavy", help="tasks always launched as heavy containers "
                    "(--rerun-memory-mb, --heavy-cpus, --heavy-free-gb gate)")
    bt.add_argument("--rerun", help="tasks in --tasks that are already their one "
                    "infrastructure rerun (launched with --rerun-memory-mb)")
    bt.add_argument("--heavy-free-gb", type=float, default=10,
                    help="desktop memory available before launching an infrastructure rerun")
    bt.add_argument("--heavy-cpus", type=float, default=4)
    bt.add_argument("--rerun-memory-mb", type=int, default=8192,
                    help="memory for the one rerun of a run that ended infra_killed/infra_error")
    bt.add_argument("--relaunch-cap", type=float,
                    help="relaunch a run that ended run_cap before 3 compactions at this cap")
    bt.add_argument("--adopt", help="tasks whose base runs are already going (grade on exit)")
    bt.add_argument("--go-from-jacob", action="store_true")
    bt.set_defaults(fn=cmd_batch)
    rr = sub.add_parser("rerecite")
    rr.add_argument("run_dirs", nargs="+")
    rr.add_argument("--repeats", type=int, default=3)
    rr.add_argument("--reextract", action="store_true",
                    help="no Codex call: re-extract each saved recitation from its raw reply")
    rr.set_defaults(fn=cmd_rerecite)
    ls = sub.add_parser("laptop-smoke")
    ls.add_argument("--task", default=SMOKE_TASK)
    ls.add_argument("--limit", type=int, default=15000)
    ls.set_defaults(fn=cmd_laptop_smoke)
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
    from compaction.s2.native import common as N
    N.install_transport_retry()  # retries only a failed docker ssh dial, within each timeout
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
