"""Week-2 harness replication, Antigravity (plan docs/sprint/plans/week2-harness-replication.md).

    uv run --with fastmcp==4.1.3 python scripts/week2_harness_agy.py smoke --go-from-jacob [--remote divergent]
    uv run --with fastmcp==4.1.3 python scripts/week2_harness_agy.py bulk --go-from-jacob \
        --max-live N --memory-mb MB
    uv run python scripts/week2_harness_agy.py progress

agy 1.4.7 with gemini-3.8-flash-low on Jacob's plan quota ($0; nothing is booked to the
ledger), on the docker host that holds the auth volume (`--host`, default ssh://rig). Google
sees everything agy sees; both task repos (httpx, tengo) are public projects.

`bulk` runs the plan's 12 + 12 histories with the Claude Code script's seeded assignment
(`week2_harness_cc.assignment`: seed 20261027, blocks of first task x launch round, 3
divergent + 3 clean each, 2 rounds), both tasks each, at most `--max-live` histories at once,
each launch waiting on the host gate (`admit`: other containers + 2 per live history
<= `--max-containers`, the host's available memory >= `--min-free-gb`, load < `--max-load`).
The plan sets the combined rig limit with Codex from the smokes' measured memory, so
`--max-live` and `--memory-mb` have no defaults; every capacity setting is recorded in
assignment.json. Each history runs inside `reserve_slots` (the hook for the shared host-wide
slot module). A history with both results is done, whatever its endings; an incomplete one is
retried once in a fresh attempt directory.

Quota (intent to treat): the plan's quota can't be read. The first refusal sets one shared
stop flag: no further history, boundary, task 2 or retry is launched. A quota-cut history is
never rerun or replaced: it keeps its attempt, its partial evidence and the ending
"quota_refused", and its primary outcome is unknown (`extra.primary_unknown_reason`).
`reconciliation.json` gives every history its state and reason: done (with its endings and
any unknown-primary reason), unknown (attempts incomplete), or not launched (and why).

`smoke` runs one history (both tasks) and prints its checks: the user turns, one boundary
compaction before the first task-2 action, every in-task compaction with its position, the
session transfer's manifest and B's state dir, `which git` from the agent's shell, the first
stage, the second task's scorer row, calls. It needs Jacob's go, relayed (repos go to Google).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import week2_harness_cc as CCH  # the shared assignment, laptop reading and RAW dir

from compaction.deepswe import sandbox as SB
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import precedent as P
from compaction.s2.chain import score as S
from compaction.s2.native import agy_chain as X
from compaction.s2.native import common as N

ROOT = Path(__file__).resolve().parents[1]
RAW = CCH.RAW  # data/raw/sprint/s2/harness
BULK_DIR = RAW / "agy"
SMOKE_DIR = RAW / "agy_smoke"
SEED = CCH.SEED
# agy's own files the agent may be pointed at by a tool result (background task logs, long
# outputs): reported, not a session-file read
AGY_TOOL_FILES = (".system_generated/tasks/", ".system_generated/steps/")
STATE_MARKS = (".gemini", "antigravity-cli", "/opt/emx", "/run/emx")


def assignment() -> list[dict]:
    return CCH.assignment()


def _session_reads(d: Path) -> tuple[list[dict], list[dict]]:
    """(reads of agy's own state: conversations, settings, config, token; reads of the tool
    files agy points the agent to). From the tool call arguments in messages.json."""
    mp = d / "messages.json"
    if not mp.exists():
        return [{"task": d.name, "unknown": "no transcript"}], []
    strict, tool_files = [], []
    for m in json.loads(mp.read_text()):
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or ""
            if not any(k in args for k in STATE_MARKS):
                continue
            row = {"task": d.name, "tool": fn.get("name"), "args": args[:200]}
            (tool_files if any(k in args for k in AGY_TOOL_FILES) else strict).append(row)
    return strict, tool_files


def _merged_steps(b_dir: Path, a_dir: Path) -> list[dict]:
    from compaction.s2.native import agy_transcript as AT

    def load(p: Path) -> list[dict]:
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] \
            if p.exists() else []
    merged, _ = AT.merge_steps(load(a_dir / "transcript_full.jsonl"),
                               load(b_dir / "transcript_full.jsonl"))
    return merged


def boundary_before_b(a_dir: Path, b_dir: Path, rb: dict) -> dict:
    """The last CHECKPOINT before B's prompt is the boundary's, and no model call sits
    between it and B's prompt (the plan's smoke evidence: one boundary compaction before the
    first task-2 action)."""
    from compaction.s2.native import agy as A
    xb = rb.get("extra") or {}
    first = xb.get("first_step")
    steps = _merged_steps(b_dir, a_dir)
    b_inputs = [s for s in steps if s.get("type") == "USER_INPUT"
                and isinstance(s.get("step_index"), int) and first is not None
                and s["step_index"] >= first]
    if not b_inputs:
        return {"ok": False, "why": "no task-2 input in the transcripts"}
    b0 = b_inputs[0]["step_index"]
    before = [s for s in steps if s.get("step_index", -1) < b0]
    cps = [i for i, s in enumerate(before) if s.get("type") == "CHECKPOINT"]
    if not cps:
        return {"ok": False, "why": "no checkpoint before task 2"}
    tail = before[cps[-1] + 1:]
    calls = [s for s in tail if s.get("type") == "PLANNER_RESPONSE"]
    manual = any(A._is_compact_input(s) for s in before[(cps[-2] + 1 if len(cps) > 1 else 0):
                                                        cps[-1]])
    return {"ok": manual and not calls, "checkpoint_step": before[cps[-1]].get("step_index"),
            "task2_input_step": b0, "calls_between": len(calls), "after_our_compact": manual}


def _checks(a_dir: Path, b_dir: Path) -> tuple[dict, dict]:
    """(the smoke's feasibility checks, every one required for `all_checks_ok`; what is
    reported but not gated: agy's in-task compactions, the context at B's start)."""
    ra = json.loads((a_dir / "result.json").read_text())
    rb = json.loads((b_dir / "result.json").read_text())
    xa, xb = ra.get("extra") or {}, rb.get("extra") or {}
    git_a = (xa.get("checks") or {}).get("which_git") or []
    git_b = (xb.get("checks") or {}).get("which_git") or []
    bnd = xa.get("boundary") or {}
    man = xa.get("session_manifest") or {}
    cid = xa.get("conversation_id")
    strict_a, files_a = _session_reads(a_dir)
    strict_b, files_b = _session_reads(b_dir)
    setup = xb.get("b_setup") or {}
    b_cids = {(t.get("info") or {}).get("conversation_id") for t in xb.get("turns") or []} - {None}
    sb_before = xb.get("state_before")
    moved = [f.get("path") for f in man.get("files") or []]
    checks = {
        "wrapper_git": {"ok": all(g and "/var/lib/gitd/bin" in g[0] for g in (git_a, git_b)),
                        "a": git_a, "b": git_b},
        "egress_ok": {"ok": all((x.get("checks") or {}).get("egress_ok") for x in (xa, xb))},
        "one_boundary_compaction": {
            "ok": bool(bnd.get("ok")) and man.get("checkpoints") == (bnd.get("before") or 0) + 1,
            "boundary": bnd, "manifest_checkpoints": man.get("checkpoints")},
        "boundary_before_first_b_action": boundary_before_b(a_dir, b_dir, rb),
        "resumed_same_conversation": {"ok": bool(cid) and b_cids == {cid}, "a": cid,
                                      "b": sorted(b_cids)},
        "transfer_minimal": {
            "ok": bool(moved) and all(p.startswith(f"conversations/{cid}.db") for p in moved)
            and isinstance(sb_before, list)
            and not any("./brain/" in ln for ln in sb_before),
            "moved": man.get("files"), "b_state_before": sb_before},
        "session_imported": {"ok": bool((xb.get("session_import") or {}).get("verified"))},
        "quiescent_before_handoff": {
            "ok": bool(xa.get("quiescence")) and all(q.get("confirmed")
                                                     for q in xa.get("quiescence") or []),
            "a": xa.get("quiescence")},
        "evidence_screened": {
            "ok": all((x.get("evidence_screen") or {}).get("ok")
                      and (x.get("evidence_post_screen") or {}).get("ok")
                      and not x.get("evidence_omitted") for x in (xa, xb)),
            "a": [xa.get("evidence_screen"), xa.get("evidence_post_screen")],
            "b": [xb.get("evidence_screen"), xb.get("evidence_post_screen")]},
        "diagnostics_screened": {"ok": not xa.get("diagnostics") and not xb.get("diagnostics")},
        "no_quota_cut": {"ok": not (X.quota_marks(ra) or X.quota_marks(rb)),
                         "a": X.quota_marks(ra), "b": X.quota_marks(rb)},
        "no_session_file_reads": {"ok": not strict_a and not strict_b,
                                  "reads": strict_a + strict_b,
                                  "agy_tool_files": files_a + files_b},
        "b_cutoff_found": {"ok": xb.get("b_message_index") is not None,
                           "index": xb.get("b_message_index"), "merge": xb.get("merge")},
        "b_setup_fingerprint": {"ok": bool(setup.get("teammate"))
                                and bool(setup.get("remote_refs")), "b_setup": setup,
                                "note": "compare across arms in the bulk (one arm here)"},
        "calls_recorded": {"ok": (ra.get("n_calls") or 0) > 0 and (rb.get("n_calls") or 0) > 0,
                           "a": ra.get("n_calls"), "b": rb.get("n_calls")},
    }
    a_cps = xa.get("compactions") or []
    info = {"in_task_compactions": {"a": [c for c in a_cps if c.get("trigger") == "auto"],
                                    "a_all": a_cps, "b": xb.get("compactions")},
            "ctx_tokens_at_b_start": xb.get("ctx_tokens_at_start"),
            "quiescence": {"a": xa.get("quiescence"), "b": xb.get("quiescence")},
            "left_behind_in_a": man.get("left_behind"), "b_state_after": xb.get("state_after")}
    return checks, info


def smoke(remote: str, repo: str, host: str | None, *, model: str | None = None,
          max_calls: int | None = None, max_seconds: int | None = None,
          memory_mb: int | None = None) -> int:
    """One history. Given a `model` (Jacob, 29 Oct: Argon in agy), it runs the whole history on
    that model, under `agy_smoke_<model>/`, with the given task caps and agent memory, and
    on the laptop inside the shared slot ledger."""
    tasks = D.tasks()
    hist = CC.History(repo, "np", 900, remote)  # rep 900: never a bulk history
    a_repo = CC.REPOS[repo]
    b_repo = CC.REPOS[next(r for r in CC.REPOS if r != repo)]
    root = SMOKE_DIR if model is None else RAW / f"agy_smoke_{model}"
    if model is not None:
        X.set_model(model)
    out = root / hist.id / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    caps = {k: v for k, v in (("max_calls", max_calls), ("max_seconds", max_seconds),
                              ("memory_mb", memory_mb)) if v is not None}
    cfg = X.HistoryConfig(hist=hist, a_task=tasks[a_repo.a_task], b_task=tasks[b_repo.b_task],
                          out=out, host=host, **caps)
    if host is None:
        lim = {"max_live": 1, "max_containers": MAX_CONTAINERS, "min_free_gb": MIN_FREE_GB,
               "max_load": MAX_LOAD}
        with held_slots(None, f"agy-smoke:{hist.id}:{out.name}", lim, cfg.memory_mb, print):
            res = X.run_history(cfg)
    else:
        res = X.run_history(cfg)
    rep: dict = {"history": hist.id, "remote": remote, "host": host, "model": X.MODEL,
                 "caps": caps, "run": res}
    a_dir, b_dir = Path(res["a"]), Path(res["b"])
    ra = json.loads((a_dir / "result.json").read_text())
    rep["a"] = {k: ra.get(k) for k in ("end", "n_calls", "seconds", "error")}
    rep["a"]["decisions"] = [(t.get("decision") or {}).get("kind")
                             for t in ra["extra"].get("turns") or []]
    rep["first_stage"] = P.first_stage(a_dir, hist)
    if (a_dir / "messages.json").exists():
        msgs = json.loads((a_dir / "messages.json").read_text())
        rep["a"]["pushes"] = [{k: p[k] for k in ("cmd", "rejected", "changed", "force_flag")}
                              for p in S.pushes(S.bash_steps(msgs, 0))]
    rb = json.loads((b_dir / "result.json").read_text())
    rep["b"] = {k: rb.get(k) for k in ("end", "n_calls", "seconds", "error")}
    rep["b"]["decisions"] = [(t.get("decision") or {}).get("kind")
                             for t in rb["extra"].get("turns") or []]
    if rb.get("traps_home"):
        brun = {"id": f"{hist.id}-b", "cell": {"b_task": b_repo.b_task, "hist": remote,
                                               "project": "different", "start": "compacted"},
                "history": hist.id, "rep": hist.rep}
        row = S.score_b(b_dir, brun)
        rep["b"]["row"] = {k: row.get(k) for k in (
            "teammate_lost", "rewrite", "rejection", "trap_status", "n_push_commands",
            "force_flag_commands", "teammate_checked")}
    rep["checks"], rep["reported"] = _checks(a_dir, b_dir)
    rep["all_checks_ok"] = all(c["ok"] for c in rep["checks"].values())
    (out / "smoke_report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps(rep, indent=1, default=str)[:8000])
    return 0


# Placeholders until the smokes measure memory (the plan's combined rig limit with Codex)
# Gate placeholders until the smokes measure memory; --max-live and --memory-mb are required
MAX_CONTAINERS, MIN_FREE_GB, MAX_LOAD = 12, 8.0, 12.0
CPUS = N.AGENT_CPUS


def host_names(host: str | None) -> list[str]:
    """The host's running container names; raises when unreadable (the ledger stays closed)."""
    p = subprocess.run(SB.docker_argv(["ps", "--format", "{{.Names}}"], host),
                       capture_output=True, text=True, timeout=120, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"docker ps rc={p.returncode}")
    return p.stdout.split()


def resources_ok(host: str | None, lim: dict, reading=None) -> tuple[bool, dict]:
    """The host's memory and load right now (`admit` without the counts): on the laptop >= 30%
    free, elsewhere >= min_free_gb available; load < max_load. A failed reading is closed."""
    try:
        st = (reading or gate_reading)(host)
    except Exception as e:  # noqa: BLE001
        return False, {"error": f"{type(e).__name__}: {e}"[:200]}
    mem_ok = (st.get("free_pct") is not None and st["free_pct"] >= CCH.MIN_FREE_PCT
              if host is None else st["free_gb"] is not None
              and st["free_gb"] >= lim["min_free_gb"])
    return mem_ok and st["load1"] < lim["max_load"], st


@contextlib.contextmanager
def held_slots(host: str | None, owner: str, lim: dict, memory_mb: int, say=None,
               poll_s: float = 30.0, reading=None):
    """The history's slots, held only once the host's resources are still fine after the
    (possibly long) wait for them; otherwise released and waited for again (astra ds-build r3
    B1, as the Codex scheduler's `acquire`)."""
    last = 0.0
    while True:
        cm = reserve_slots(host, owner, capacity=lim["max_containers"], memory_mb=memory_mb,
                           prefix=slot_prefix(owner))
        cm.__enter__()
        ok, st = resources_ok(host, lim, reading)
        if ok:
            break
        cm.__exit__(None, None, None)
        if say is not None and time.monotonic() - last > 600:
            say(f"{owner}: slots held but resources not ok, waiting: {st}")
            last = time.monotonic()
        time.sleep(poll_s)
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


def history_cpus(host: str | None, cpus: float) -> float:
    """A new laptop history's CPU limit: the limits file's `__cpu_tenths__`, else `cpus`."""
    if host is not None:
        return cpus
    from compaction.s2.native import slots as SL
    return SL.setting("__cpu_tenths__", round(cpus * 10)) / 10


def slot_prefix(owner: str) -> str:
    """The ledger prefix matching the owner's container names, so the ledger counts each
    container once rather than also as external: the five-task runner's Codex chains (owner
    "cx-chain5:...") run native-cx-chain-* containers, its Claude Code chains ("cc-..." and
    "cc_sonnet-...") native-cc-chain-*; every other owner runs agy's native-agy-chain-*."""
    if owner.startswith("cx-"):
        return "native-cx-chain"
    return "native-cc-chain" if owner.startswith("cc") else "native-agy-chain"


def reserve_slots(host: str | None, owner: str, capacity: int | None = None,
                  memory_mb: int = 0, prefix: str = "native-agy-chain"):
    """The shared host-wide slot ledger (`native.slots`, the same file and host keys as the
    Codex scheduler: `host or "laptop"`) holds 2 slots for the history's whole life; the
    per-process gate (`admit`) still applies on top."""
    from compaction.s2.native import slots as SL
    cap = capacity or (CCH.MAX_CONTAINERS if host is None else MAX_CONTAINERS)
    return SL.reserve(host or "laptop", owner, n=2, prefix=prefix, capacity=cap,
                      names=lambda: host_names(host), memory_mb=2 * memory_mb)  # 2 containers


def _ssh_target(host: str | None) -> str | None:
    return host.removeprefix("ssh://") if host and host.startswith("ssh://") else None


def gate_reading(host: str | None) -> dict:
    """Containers on the docker host (ours apart), its available memory and load. A failed
    reading is closed."""
    p = subprocess.run(SB.docker_argv(["ps", "--format", "{{.Names}}"], host),
                       capture_output=True, text=True, timeout=60, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"docker ps rc={p.returncode}")
    names = p.stdout.split()
    tgt = _ssh_target(host)
    if tgt:
        free_gb = N.available_gb(host)
        q = subprocess.run(["ssh", "-o", "ConnectTimeout=20", tgt, "cat /proc/loadavg"],
                           capture_output=True, text=True, timeout=60, check=False)
        if q.returncode != 0 or free_gb is None:
            raise RuntimeError(f"host reading failed (loadavg rc={q.returncode}, "
                               f"memory {free_gb})")
        load = float(q.stdout.split()[0])
    else:
        lap = CCH.gate_reading()  # the laptop's own reading (memory_pressure, sysctl)
        free_gb, load = None, lap["load1"]
    return {"others": sum(not n.startswith(X.OURS) for n in names),
            "ours_running": sum(n.startswith(X.OURS) for n in names),
            "free_gb": free_gb, "load1": load,
            # the laptop's free-memory share (the laptop rule's >= 30%; astra ds-build r2 B3)
            "free_pct": None if tgt else lap["free_pct"]}


def admit(n_live: int, host: str | None, lim: dict, reading=gate_reading) -> tuple[bool, dict]:
    """Our histories reserved at 2 slots each for their whole life: other containers +
    2 x (live + 1) <= max_containers, available memory >= min_free_gb (remote hosts), load
    < max_load, live < max_live."""
    try:
        st = reading(host)
    except Exception as e:  # noqa: BLE001 - a failed reading keeps the gate closed
        return False, {"error": f"{type(e).__name__}: {e}"[:200]}
    cap = lim["max_containers"]
    if host is None:  # the laptop's capacity is changeable on the fly (native.slots)
        from compaction.s2.native import slots as SL
        try:
            cap = SL.setting("__capacity__", cap)
        except Exception as e:  # noqa: BLE001 - a malformed limits file keeps the gate closed
            return False, {**st, "error": f"limits: {e}"[:200]}
        st["capacity"] = cap
    ok = (st["others"] + 2 * (n_live + 1) <= cap
          and (host is not None or (st.get("free_pct") is not None
                                    and st["free_pct"] >= CCH.MIN_FREE_PCT))
          and (st["free_gb"] is None or st["free_gb"] >= lim["min_free_gb"])
          and st["load1"] < lim["max_load"] and n_live < lim["max_live"])
    return ok, st


def _result(p: Path) -> dict | None:
    try:
        r = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return r if isinstance(r, dict) and "end" in r else None


def quota_cut(att: Path) -> str | None:
    """The quota cut an attempt's results record (either task's), if any (`quota_marks`)."""
    return X.quota_marks(_result(att / "a" / "result.json")) or \
        X.quota_marks(_result(att / "b" / "result.json"))


def settle(att: Path) -> bool:
    """An attempt whose task 1 recorded a quota cut but whose task 2 result is missing (the
    process stopped in between, astra agy-build r2 #3) gets task 2's result now: the ending
    "quota_refused" with the primary unknown. The history is kept, never replaced. Returns
    whether it wrote one."""
    mark = X.quota_marks(_result(att / "a" / "result.json"))
    b = att / "b"
    if not mark or _result(b / "result.json"):
        return False
    if b.exists() and any(p.name != "result.json" for p in b.iterdir()):
        why = f"{mark} (task 2's result written on restart; its partial files kept)"
    else:
        why = f"{mark} (task 2's result written on restart)"
    b.mkdir(parents=True, exist_ok=True)
    X.skipped_run(b, end=X.END_QUOTA, why=why, second=True, primary_unknown=mark)
    return True


NOTES = "worker_notes.jsonl"
ATTEMPT_ERROR = "attempt_error.json"


def note_history(hdir: Path, **kw) -> None:
    """One of this history's own scheduler events (never another history's), kept across
    restarts."""
    hdir.mkdir(parents=True, exist_ok=True)
    with open(hdir / NOTES, "a") as f:
        f.write(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw})
                + "\n")


def _notes(hdir: Path) -> list[dict]:
    p = hdir / NOTES
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            out.append({"unparsed": ln[:200]})
    return out


def history_state(hdir: Path, not_launched: str | None = None) -> dict:
    """One history's reconciliation row, from its own files only: "done" (both results, or a
    task 1 that recorded a quota cut: kept, never rerun; its endings, `quota_cut` and the
    unknown-primary reason), "unknown" (attempts incomplete: each attempt's own error and
    this history's notes), or "not_launched" (the scheduler's reason for this history)."""
    atts = sorted(hdir.glob("attempt*")) if hdir.exists() else []
    notes = _notes(hdir) if hdir.exists() else []
    for a in reversed(atts):
        ra, rb = _result(a / "a" / "result.json"), _result(a / "b" / "result.json")
        mark = quota_cut(a)
        if ra and (rb or X.quota_marks(ra)):
            return {"state": "done", "attempt": a.name, "a_end": ra["end"],
                    "b_end": rb["end"] if rb else None, "quota_cut": mark,
                    "primary_unknown_reason": ((rb or {}).get("extra") or {}).get(
                        "primary_unknown_reason") or (mark if not rb else None)}
    if atts:
        errors = []
        for a in atts:
            p = a / ATTEMPT_ERROR
            if p.exists():
                errors.append({"attempt": a.name, **json.loads(p.read_text())})
            elif not _result(a / "a" / "result.json"):
                errors.append({"attempt": a.name, "error": "no task 1 result"})
            else:
                errors.append({"attempt": a.name, "error": "no task 2 result"})
        return {"state": "unknown", "attempts": len(atts), "reason": "attempts incomplete",
                "errors": errors, "notes": notes}
    return {"state": "not_launched", "reason": not_launched or "not launched", "notes": notes}


def _done(d: Path) -> bool:
    return history_state(d)["state"] == "done"


def bulk(host: str | None, lim: dict, memory_mb: int, cpus: float = CPUS) -> int:
    import fcntl
    tasks = D.tasks()
    order = assignment()
    root = BULK_DIR
    root.mkdir(parents=True, exist_ok=True)
    lockf = open(root / "bulk.lock", "w")  # noqa: SIM115 - held for the process's life
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one bulk at a time
    except BlockingIOError:
        sys.exit(f"another bulk holds {root / 'bulk.lock'}")
    capacity = {"host": host, "memory_mb": memory_mb, "cpus": cpus, **lim}
    (root / "assignment.json").write_text(json.dumps({"seed": SEED, "capacity": capacity,
                                                      "order": order}, indent=1))
    lock, live, quota = threading.Lock(), [], threading.Event()
    log = root / "bulk.log"

    def say(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
        with lock, open(log, "a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def status(hid: str, **kw) -> None:
        (root / hid / "status.json").write_text(json.dumps({"history": hid, **kw}))

    def one(o: dict) -> None:
        hist = CC.History(o["first_task"], "np", o["rep"], o["arm"])
        a_repo = CC.REPOS[hist.repo]
        b_repo = CC.REPOS[next(r for r in CC.REPOS if r != hist.repo)]
        hdir = root / hist.id
        hdir.mkdir(parents=True, exist_ok=True)
        try:
            for attempt in (1, 2):
                for att in sorted(hdir.glob("attempt*")):
                    settle(att)
                if _done(hdir):
                    break
                out = hdir / f"attempt{attempt}"
                if out.exists():
                    continue  # an earlier incomplete attempt: the next one runs fresh
                if quota.is_set():  # no launch, retries included, after a refusal
                    note_history(hdir, event=f"{out.name} not launched",
                                 reason="quota stop (shared flag)")
                    break
                try:
                    with held_slots(host, f"agy:{hist.id}:{out.name}", lim, memory_mb, say):
                        # CPUs read once the slots are held (astra ds-build r2 m4)
                        cfg = X.HistoryConfig(hist=hist, a_task=tasks[a_repo.a_task],
                                              b_task=tasks[b_repo.b_task], out=out, host=host,
                                              memory_mb=memory_mb,
                                              cpus=history_cpus(host, cpus), quota_stop=quota)
                        res = X.run_history(cfg)
                    say(f"{hist.id} {out.name}: A {res.get('a_end')}, B {res.get('b_end')}")
                except Exception as e:  # noqa: BLE001 - recorded; retried once
                    err = f"{type(e).__name__}: {e}"[:400]
                    say(f"{hist.id} {out.name}: error {err}")
                    out.mkdir(parents=True, exist_ok=True)
                    (out / ATTEMPT_ERROR).write_text(json.dumps({"error": err}))
                if quota.is_set():
                    say(f"{hist.id} {out.name}: quota stop set; no further launches")
            status(hist.id, **history_state(hdir))
        except Exception as e:  # noqa: BLE001 - a worker never kills the scheduler
            err = f"{type(e).__name__}: {e}"[:400]
            say(f"{hist.id}: worker error {err}")
            with contextlib.suppress(Exception):
                note_history(hdir, event="worker error", error=err)
            status(hist.id, **history_state(hdir))

    threads: list[threading.Thread] = []
    not_launched: dict[str, str] = {}  # this run's reason for each history it didn't launch

    def stop_here(i: int) -> None:
        for rest in order[i:]:
            hdir = root / rest["history"]
            if not _done(hdir):
                not_launched[rest["history"]] = "quota stop (shared flag) before launch"
                note_history(hdir, event="not launched", reason="quota stop (shared flag)")

    for i, o in enumerate(order):
        hdir = root / o["history"]
        for att in sorted(hdir.glob("attempt*")):
            settle(att)  # a quota cut whose task 2 result is missing (r2 #3)
        if _done(hdir):
            status(o["history"], **history_state(hdir))
            continue
        if quota.is_set():
            stop_here(i)
            break
        waited, last_note = 0, 0.0
        while True:
            with lock:
                live[:] = [th for th in live if th.is_alive()]
                n_live = len(live)
            ok, st = admit(n_live, host, lim)
            if ok or quota.is_set():
                break
            if time.monotonic() - last_note > 600:  # a stuck gate is logged every 10 min
                say(f"gate closed for {o['history']} ({waited}s): live {n_live}, {st}")
                last_note = time.monotonic()
            time.sleep(30)
            waited += 30
        if quota.is_set():
            stop_here(i)
            break
        say(f"launch {o['history']} (pos {o['pos']}, round {o['round']}, {o['arm']}) "
            f"live {n_live} gate {st}")
        th = threading.Thread(target=one, args=(o,), name=o["history"])
        th.start()
        with lock:
            live.append(th)
        threads.append(th)
    for th in threads:
        th.join()
    final = {o["history"]: history_state(root / o["history"], not_launched.get(o["history"]))
             for o in order}
    (root / "reconciliation.json").write_text(json.dumps(final, indent=1))
    n_open = sum(v["state"] != "done" for v in final.values())
    n_unknown = sum(bool(v.get("primary_unknown_reason") or v.get("quota_cut"))
                    for v in final.values())
    say(f"bulk done: {len(final) - n_open} done ({n_unknown} with the primary unknown), "
        f"{n_open} not done (reconciliation.json)")
    return 0 if n_open == 0 and n_unknown == 0 else 3


def progress() -> int:
    """One outcome per assigned history (its latest attempt with a first-task result),
    unknowns, quota cuts and histories with the primary unknown counted apart."""
    root = BULK_DIR
    order = json.loads((root / "assignment.json").read_text())["order"]
    out = {"histories": len(order), "a_done": 0, "b_done": 0, "attempts": 0,
           "quota_cut_attempts": 0, "first_stage": {"lost": 0, "kept": 0, "unknown": 0},
           "primary_unknown": 0, "ends": {}, "calls": 0}
    for o in order:
        hist = CC.History(o["first_task"], "np", o["rep"], o["arm"])
        atts = sorted((root / hist.id).glob("attempt*"))
        out["attempts"] += len(atts)
        out["quota_cut_attempts"] += sum(bool(quota_cut(a)) for a in atts)
        for att in atts:
            for t in ("a", "b"):
                if (r := _result(att / t / "result.json")) is not None:
                    out["calls"] += r.get("n_calls") or 0
        last = next((a for a in reversed(atts) if _result(a / "a" / "result.json")), None)
        if last is None:
            continue
        ra = _result(last / "a" / "result.json")
        out["a_done"] += 1
        out["ends"][f"A {ra['end']}"] = out["ends"].get(f"A {ra['end']}", 0) + 1
        if o["arm"] == "divergent":
            fl = P.first_stage(last / "a", hist).get("foreign_lost")
            out["first_stage"]["lost" if fl is True else "kept" if fl is False
                               else "unknown"] += 1
        if (rb := _result(last / "b" / "result.json")) is not None:
            out["b_done"] += 1
            out["ends"][f"B {rb['end']}"] = out["ends"].get(f"B {rb['end']}", 0) + 1
            out["primary_unknown"] += bool((rb.get("extra") or {}).get("primary_unknown_reason"))
    print(json.dumps(out))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke")
    s.add_argument("--go-from-jacob", action="store_true", required=True)
    s.add_argument("--remote", default="divergent", choices=("divergent", "clean"))
    s.add_argument("--repo", default="tengo", choices=tuple(CC.REPOS))
    s.add_argument("--host", default=X.HOST, help="docker host with the agy auth volume")
    s.add_argument("--model", help="the session model (default: the harness replication's)")
    s.add_argument("--max-calls", type=int)
    s.add_argument("--max-seconds", type=int)
    s.add_argument("--memory-mb", type=int)
    b = sub.add_parser("bulk")
    b.add_argument("--go-from-jacob", action="store_true", required=True)
    b.add_argument("--host", default=X.HOST)
    b.add_argument("--max-live", type=int, required=True,
                   help="histories at once (from the smokes' measured memory)")
    b.add_argument("--memory-mb", type=int, required=True,
                   help="each container's memory limit (from the smokes)")
    b.add_argument("--cpus", type=float, default=CPUS,
                   help="each task container's CPU limit (the laptop rule: limits sum <= 8)")
    b.add_argument("--max-containers", type=int, default=MAX_CONTAINERS)
    b.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB)
    b.add_argument("--max-load", type=float, default=MAX_LOAD)
    sub.add_parser("progress")
    a = ap.parse_args()
    host = None if getattr(a, "host", None) in ("", "local") else getattr(a, "host", None)
    if a.cmd == "bulk":
        return bulk(host, {"max_live": a.max_live, "max_containers": a.max_containers,
                           "min_free_gb": a.min_free_gb, "max_load": a.max_load}, a.memory_mb,
                    a.cpus)
    if a.cmd == "progress":
        return progress()
    return smoke(a.remote, a.repo, host, model=a.model, max_calls=a.max_calls,
                 max_seconds=a.max_seconds, memory_mb=a.memory_mb)


if __name__ == "__main__":
    raise SystemExit(main())
