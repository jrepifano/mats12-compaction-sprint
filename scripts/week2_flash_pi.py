"""Flash in pi: check, replay-check, stub, smoke, go, status, analyze.

Adapted from week2_planted_precedent.py. Only smoke/go can spend; the operator supplies
OPENROUTER_API_KEY_J in the process environment. This runner never reads .env.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from compaction import budget
from compaction.s2 import loop as L
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import flash_pi as F
from compaction.s2.chain import precedent as P
from compaction.s2.chain import session as SE
from compaction.s2.chain import stubs
from compaction.s2.chain import week2 as W
from compaction.s2.chain.readout import CAPPED

MIN_REQUEST_SECONDS = 5.0


def setup(cfg: dict, root: Path, host: str | None = None):
    source = W.load_config(cfg["flash_pi"]["precedent_config"]["path"])
    # Check the actual harness texts, not just the files they originally came from.
    hashes = CC.text_hashes()
    for name, key in W.PRECEDENT_WORDING.items():
        if source["wording"][name]["sha256"] != hashes[key]:
            raise ValueError(f"experiment 1 harness wording changed: {name}")
    CC.CHECKIN_CALLS = tuple(source["precedent"]["checkin_calls"])
    CC.PERMISSION_AFTER_CALL = source["precedent"]["permission_after_call"]
    # One observation per history: never use experiment 1's failed-attempt retries.
    CC.A_RETRIES = 0
    os.environ[CC.RAW_ENV] = str(root.resolve())
    CC.PURPOSE = "flash-pi"
    SE.KEEP_REPO = "emx-keep/flash-pi"
    D.STATUS_FILE = root / "run.log"
    D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
    D.apply_design(source)
    D.LOG_REASONING = cfg["design"]["log_reasoning"]
    # Checkpoints only pause/copy/commit externally (ChainEnv.checkpoint); they write
    # nothing in the container or model messages. Disable these agent-invisible forks.
    D.CHECKPOINT = None
    if host is not None:
        if (host == "laptop") != (sys.platform == "darwin"):
            raise ValueError(f"host {host} disagrees with platform {sys.platform}")
        D.set_agent_memory(cfg["parallelism"]["agent_memory_mb"])
        if host == "laptop":
            from compaction.s2.chain import laptop_admission as LA

            CC.AGENT_CPUS = 1
            D.ADMISSION = LA.LaptopAdmission(
                mem_mb=1280, cpus=1, max_agents=1, ramp_agents=1, ramp_after_s=600, log=D.status
            )
    root.mkdir(parents=True, exist_ok=True)


def bind(st, cfg):
    fp = W.fingerprint(cfg)
    if st.get("week2_design") not in (None, fp):
        raise ValueError("existing state belongs to another design")
    st["week2_design"] = fp
    st["week2_config"] = cfg["_source"]["path"]
    st["reasoning_effort"] = cfg["flash_pi"]["reasoning_effort"]
    D.save_state(st)


def gate(root, cfg, name, result):
    st = D.load_state(root / "state.json")
    bind(st, cfg)
    st["gates"][name] = {"pass": result["pass"], "fingerprint": W.fingerprint(cfg),
                         "utc": D.utc(), "result": result}
    D.save_state(st)


def require_gates(root, cfg, names):
    st = D.load_state(root / "state.json")
    for name in names:
        g = st["gates"].get(name) or {}
        if g.get("pass") is not True or g.get("fingerprint") != W.fingerprint(cfg):
            raise ValueError(f"missing/current-design gate: {name}")


# Keys a design migration may change (plan v0.2, the Research Advisor's call after the smoke).
MIGRATABLE = {("meta", "built_against"), ("flash_pi", "bulk_httpx"), ("flash_pi", "bulk_tengo")}


def migrate_design(root, cfg, from_commit, reason):
    """Rebind a run state from an earlier committed config to the current one, when they
    differ only in MIGRATABLE keys and give the same smoke units. The smoke gate is
    recomputed from the saved smoke runs and cleanup record (never hand-edited); the other
    gates are dropped and must be re-run ($0)."""
    import hashlib
    import subprocess
    import tomllib

    rel = cfg["_source"]["path"]
    rel = str(Path(rel).resolve().relative_to(CC.REPO_ROOT)) if Path(rel).is_absolute() else rel
    raw = subprocess.run(["git", "-C", str(CC.REPO_ROOT), "show", f"{from_commit}:{rel}"],
                         check=True, capture_output=True).stdout
    old = tomllib.loads(raw.decode("utf-8"))
    old_fp = {"exp": old["meta"]["exp"], "config_sha256": hashlib.sha256(raw).hexdigest(),
              "proposal_sha256": old["meta"]["built_against"]}
    st = D.load_state(root / "state.json")
    if st.get("week2_design") != old_fp:
        raise ValueError("state is not bound to the config at that commit")
    new = {k: v for k, v in cfg.items() if k != "_source"}
    changed = {(sec, k) for sec in set(old) | set(new)
               for k in set(old.get(sec) or {}) | set(new.get(sec) or {})
               if (old.get(sec) or {}).get(k) != (new.get(sec) or {}).get(k)}
    if not changed <= MIGRATABLE:
        raise ValueError(f"config changes beyond the migratable keys: {sorted(changed - MIGRATABLE)}")
    smoke = F.order(cfg, smoke=True)
    old_smoke = F.order({**old, "flash_pi": {**old["flash_pi"], "bulk_httpx": 0, "bulk_tengo": 0}},
                        smoke=True)
    if smoke != old_smoke:
        raise ValueError("smoke units differ between the two configs")
    sg = st["gates"].get("smoke") or {}
    if sg.get("pass") is not True or sg.get("fingerprint") != old_fp:
        raise ValueError("no passed smoke gate under the old config")
    cleanup = json.loads((root / "cleanup.json").read_text())
    if not smoke_pass(st, smoke, cleanup.get("left_images", [None])):
        raise ValueError("saved smoke evidence does not pass")
    new_fp = W.fingerprint(cfg)
    st.setdefault("design_history", []).append(
        {"utc": D.utc(), "from": old_fp, "to": new_fp, "from_commit": from_commit,
         "changed": sorted(".".join(c) for c in changed), "reason": reason})
    # The v0.1 stub's own state is bound to the old design: archive it (kept, not deleted)
    # so the re-run stub starts fresh under v0.2.
    archived = None
    if (root / "local").exists():
        archived = root / f"local.v01-{D.utc().replace(':', '')}"
        (root / "local").rename(archived)
    st["week2_design"] = new_fp
    st["gates"] = {"smoke": {"pass": True, "fingerprint": new_fp, "utc": D.utc(),
                             "result": {"migrated_from": sg, "reason": reason,
                                        "smoke": [h.id for h in smoke]}}}
    D.save_state(st)
    return {"pass": True, "from": old_fp, "to": new_fp, "changed": sorted(".".join(c) for c in changed),
            "smoke": [h.id for h in smoke], "dropped_gates": "check, replay_check, stub (re-run)",
            "archived_stub": str(archived) if archived else None}


def cap_file(root, cfg):
    p = root / "budget_caps.toml"
    cap = cfg["caps"]["stage_usd"]
    content = ("[sprint]\ncheckout = " + json.dumps(str(CC.REPO_ROOT)) + "\n"
               f"core_cap_usd = {cap}\nfull_cap_usd = {cap}\ntopup_recorded = false\n"
               "transfer_approval_over_usd = 0.0\n\n"
               f"[stages.{cfg['design']['stage']}]\ncap_usd = {cap}\n")
    if p.exists() and p.read_text() != content:
        raise ValueError("existing caps file differs")
    p.write_text(content)
    return p


def spending(root):
    st = budget.status(ledger=CC.ledger(), caps_file=root / "budget_caps.toml")
    return (st.get("stages") or {}).get(F.STAGE) or {"spent": 0, "open": 0, "cap": 35}


class WaitingSpend:
    """Wait out stage holds without holding PilotSpend's locks or stopping other runs."""

    def __init__(self, inner, root, stop_at, wait_s=120.0, poll_s=2.0):
        self.inner, self.root, self.stop_at = inner, root, stop_at
        self.wait_s, self.poll_s = wait_s, poll_s

    def reserve(self, usd, note):
        from compaction.s2.model import RunCapReached, StageCapReached

        end = time.monotonic() + self.wait_s
        while True:
            remaining = (self.stop_at - datetime.now(UTC)).total_seconds()
            if D.stop_requested() or remaining <= 0:
                raise L.Stopped("cutoff/stop while waiting for stage holds")
            try:
                return self.inner.reserve(usd, note)
            except StageCapReached:
                # PilotSpend has released both accounting locks before this read/wait.
                # Its tag cap is the same $35 stage cap, so settled stage spend is the
                # conservative exhaustion check across all effort configs and tags.
                s = spending(self.root)
                if s["spent"] + usd > s["cap"] + 1e-9:
                    raise  # real exhaustion: session.drive stops the whole stage
                wait_left = end - time.monotonic()
                if wait_left <= 0:
                    # Reservation pressure only caps this run, never the other runs.
                    raise RunCapReached("timed out waiting for stage holds") from None
                L.STOP.wait(min(self.poll_s, wait_left, remaining))

    def settle(self, *args, **kwargs):
        return self.inner.settle(*args, **kwargs)

    def cancel(self, *args, **kwargs):
        return self.inner.cancel(*args, **kwargs)


def deadline(value):
    d = datetime.fromisoformat(value)
    if d.tzinfo is None:
        raise ValueError("--stop-at-utc needs a timezone")
    return d.astimezone(UTC)


@contextlib.contextmanager
def until(stop_at):
    remaining = (stop_at - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise ValueError("cutoff has passed")
    timer = threading.Timer(remaining, L.STOP.set)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def checked_model(model, stop_at=None):
    from compaction.s2.model import ModelError, OpenAICompatModel

    def check():
        if D.stop_requested() or (stop_at is not None and datetime.now(UTC) >= stop_at):
            L.STOP.set()
            raise L.Stopped("cutoff/stop before request or tool execution")

    def sleep(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            check()
            L.STOP.wait(min(0.1, max(0, end - time.monotonic())))
        check()

    inner = model
    while hasattr(inner, "inner"):
        inner = inner.inner
    if isinstance(inner, OpenAICompatModel):
        client = inner.client

        def before_attempt():
            check()
            if stop_at is not None:
                remaining = (stop_at - datetime.now(UTC)).total_seconds()
                if remaining < MIN_REQUEST_SECONDS:
                    # Do not start a request with less than five seconds left; raising
                    # the timeout to five seconds would overshoot the approved cutoff.
                    raise L.Stopped("cutoff too close for another model request")

        inner.before_attempt = before_attempt
        inner.sleep = sleep
        if stop_at is not None:
            import asyncio
            from types import SimpleNamespace

            from openai import AsyncOpenAI

            async def request(body):
                remaining = (stop_at - datetime.now(UTC)).total_seconds()
                # An SDK timeout alone is an inactivity timeout: a trickling response
                # could outlive it. Cancellation closes the async transport at the wall
                # deadline, before the synchronous session exports/cleans its container.
                async with asyncio.timeout(remaining), AsyncOpenAI(
                    api_key=client.api_key, base_url=client.base_url, max_retries=0,
                    timeout=min(inner.timeout_s, remaining),
                ) as bounded:
                    return await bounded.chat.completions.create(**body)

            def create(**body):
                # The existing model owns reservation, settlement, retries and logging.
                # Each attempt owns its async client/event loop; no background request
                # survives this return, including a timeout or a cancellation.
                return asyncio.run(request(body))

            inner.client = SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=client.close
            )

    class Guard:
        route = model.route

        def complete(self, *args, **kwargs):
            check()
            try:
                reply = model.complete(*args, **kwargs)
            except ModelError:
                # OpenAICompatModel settles an HTTP timeout at its full reserved bound
                # before retrying/raising. Keep that charge, then classify cutoff as cut
                # even if this was the final retry (which skips the sleep hook).
                check()
                raise
            # A response arriving after cutoff must not execute its tool calls. Its cost
            # has already been settled in RunSpend, and reasoning logged by the wrapper.
            check()
            return reply

    return Guard()


@contextlib.contextmanager
def tool_deadline(stop_at):
    """Bound each agent tool by the cutoff, including multiple tools in one reply.

    The subclass uses experiment 1's tools unchanged except for their remaining timeout.
    Export and cleanup operations keep their normal timeouts so evidence can be salvaged.
    """
    from compaction.s2 import config as C
    from compaction.s2.env import Observation

    original = SE.ChainEnv

    def remaining():
        seconds = int((stop_at - datetime.now(UTC)).total_seconds())
        if D.stop_requested() or seconds < 1:
            L.STOP.set()
            raise L.Stopped("cutoff/stop before or after agent tool")
        return seconds

    class DeadlineEnv(original):
        @classmethod
        def create(cls, *args, **kwargs):
            remaining()
            return super().create(*args, **kwargs)

        def run_agent_command(self, cmd):
            seconds = min(C.CMD_TIMEOUT_S, remaining())
            t0 = time.monotonic()
            r = self.sb.exec("exec 2>&1\n" + cmd, timeout_s=seconds, workdir=C.WORKSPACE)
            remaining()
            out = r.stdout + r.stderr
            return Observation(r.rc, out, time.monotonic() - t0, r.timed_out,
                               len(out) >= C.OBS_MAX_CHARS)

        def ask_user(self, question, action):
            seconds = min(60, remaining())
            cmd = f"ask_user {shlex.quote(question or '(no question)')}"
            if action:
                cmd += f" --action {shlex.quote(action)}"
            r = self.sb.exec(cmd, timeout_s=seconds, workdir=C.WORKSPACE)
            remaining()
            return (r.stdout.strip() or "pending") if r.rc == 0 else "pending"

    SE.ChainEnv = DeadlineEnv
    try:
        yield
    finally:
        SE.ChainEnv = original


def run_one(cfg, h, st, route, spend, tasks_, target, model_for=None):
    out = D.a_dir(h.id, 1, target)
    slot = st["a"].get(h.id) or {}
    effort = cfg["flash_pi"]["reasoning_effort"]
    if slot.get("reasoning_effort", effort) != effort:
        raise ValueError(f"{h.id}: existing run belongs to another reasoning effort")
    if slot.get("state") == "done":
        return slot
    slot = D.entry(st, "a", h.id, {"attempts": [], "state": "pending"})
    with D._LOCK:
        slot["reasoning_effort"] = effort
        D.save_state(st)
    existing = (out / "result.json").exists()
    stopped = False
    if not existing:
        # Never redo an interrupted unit: retain its partial evidence as an unknown cut.
        if out.exists():
            SE.dump(out / "result.json", SE.RunResult("A", h.id, end="cut",
                    extra={"primary_unknown_reason": "interrupted attempt; not rerun"}).to_dict())
            existing = True
        else:
            try:
                D.run_history(h, st, route, spend, tasks_, model_for, target,
                              caps={"max_calls": cfg["caps"]["max_calls"],
                                    "max_seconds": cfg["caps"]["max_seconds"]})
            except (L.Stopped, D.Halt):
                stopped = True
    slot = D.entry(st, "a", h.id, {"attempts": [], "state": "pending"})
    if not (out / "result.json").exists():
        if stopped:
            slot.update(state="not_started", end="cut")
            D.save_state(st)
            return slot
        raise RuntimeError(f"{h.id}: first task returned without result.json")
    res = json.loads((out / "result.json").read_text())
    res.setdefault("extra", {})["reasoning_effort"] = effort
    if res["end"] == SE.END_STOPPED:
        res["end"] = "cut"
        res.setdefault("extra", {})["primary_unknown_reason"] = "cutoff/stop"
    SE.dump(out / "result.json", res)
    observed = P.first_stage(out, h).get("foreign_lost")
    # first_stage suppresses cuts; independently read the saved remote for the smoke's
    # evidence-readability gate, while the primary outcome remains unknown.
    if observed is None and res.get("traps_home"):
        from compaction.s2.chain import score as S

        home = Path(res["traps_home"])
        tip = P.foreign_tip(home, CC.REPOS[h.repo])
        observed = S.teammate_lost(home, tip) if tip else None
    recs = slot.get("attempts") or []
    cleanup = recs[-1].get("cleanup_unconfirmed") if recs else None
    if cleanup is None:
        cleanup = list(D.UNRESOLVED) if stopped and not existing else ["cleanup not recorded"]
    cost = D._attempt_spend(out)
    with D._LOCK:
        slot.update(state="done", dir=str(out), end=res["end"],
                    observed_foreign_lost=observed,
                    foreign_lost=observed if res["end"] == SE.END_SUBMIT else None,
                    cleanup_unconfirmed=cleanup, n_calls=res.get("n_calls", 0),
                    cost_usd=cost if cost is not None else res.get("cost_usd", 0))
        D.save_state(st)
    if cleanup:
        L.STOP.set()
    return slot


def smoke_pass(st, runs, left_images):
    return all((rec := st["a"].get(h.id, {})).get("state") == "done"
               and rec.get("end") in (SE.END_SUBMIT, *CAPPED, SE.END_STAGE_CAP, "cut")
               and type(rec.get("observed_foreign_lost")) is bool
               and rec.get("cleanup_unconfirmed") == [] for h in runs) and not left_images


def schedule(cfg, root, st, runs, worker, concurrency, stop, median_cost=None, max_n=None):
    if not 1 <= concurrency <= cfg["parallelism"]["agents"]:
        raise ValueError("concurrency outside config")
    if max_n is not None and not 0 <= max_n <= len(runs):
        raise ValueError("--max-n may only reduce the configured run count")
    selected = runs if max_n is None else runs[:max_n]
    todo = iter(h for h in selected if (st["a"].get(h.id) or {}).get("state") != "done")
    states, launched, live = {}, [], {}
    reason, launch_stop, exhausted = None, None, False
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        while live or not exhausted:
            while len(live) < concurrency and not exhausted:
                with D._LOCK:
                    unresolved = [hid for hid, rec in st["a"].items()
                                  if rec.get("cleanup_unconfirmed")]
                if unresolved:
                    reason, exhausted = f"unconfirmed cleanup: {', '.join(unresolved)}", True
                    break
                if D.stop_requested() or datetime.now(UTC) >= stop:
                    reason, exhausted = "cutoff/stop; no further launches", True
                    break
                if median_cost is not None:
                    s = spending(root)
                    # Conservative total projection for each live run, even if it has
                    # already spent some of that amount; outstanding API holds count too.
                    admission = F.launch_rule(cfg["caps"]["stage_usd"], s["spent"], s["open"],
                                              [median_cost] * len(live), [median_cost],
                                              cfg["flash_pi"]["launch_margin"])
                    if not admission["pass"]:
                        reason, exhausted = admission["reason"], True
                        break
                h = next(todo, None)
                if h is None:
                    exhausted = True
                    break
                launched.append(h.id)
                live[ex.submit(worker, h)] = h
            if reason:
                D.status(f"launches stopped: {reason}")
                with D._LOCK:
                    st["launch_stop"] = {"utc": D.utc(), "reason": reason}
                    D.save_state(st)
                launch_stop = reason
                reason = None
            if not live:
                break
            done, _ = wait(live, timeout=0.2, return_when=FIRST_COMPLETED)
            for fut in done:
                h = live.pop(fut)
                try:
                    states[h.id] = fut.result()["state"]
                except Exception:
                    L.STOP.set()
                    raise
    return {"states": states, "launched": launched, "launch_stop": launch_stop}


def stub(cfg, root):
    from compaction.s2.chain.reasoning import LOG_NAME, ReasoningLog
    from compaction.s2.config import ROUTES
    from compaction.s2.model import StubModel

    target = root / "local"
    h = F.order(cfg, smoke=True)[0]
    branch = CC.REPOS[h.repo].a_branch
    st = D.load_state(target / "state.json")
    bind(st, cfg)
    actions = [{"bash": f"git checkout -b {branch}"},
               {"bash": "echo flash-stub > flash_stub.txt && git add flash_stub.txt && "
                        "git commit -qm 'stub first task'"},
               {"bash": f"git push origin {branch} 2>&1"},
               {"bash": f"git push --force origin {branch} 2>&1"}, {"submit": True}]

    def mf(phase, out):
        if phase != "A":
            raise ValueError("first tasks only")
        return ReasoningLog(StubModel(actions=actions, route=ROUTES["stub"]), out / LOG_NAME), None

    try:
        run_one(cfg, h, st, ROUTES["stub"], None, D.tasks(), target, mf)
    finally:
        left = stubs.remove_kept(target)
        SE.dump(target / "cleanup.json", {"utc": D.utc(), "left_images": left})
    return {"pass": smoke_pass(st, [h], left)
            and st["a"][h.id].get("foreign_lost") is True,
            "history": h.id, "foreign_lost": st["a"][h.id].get("foreign_lost"),
            "left_images": left, "cost_usd": 0}


def paid(a, cfg, root):
    from compaction.s2.interact_run import PilotSpend

    if not a.go_from_jacob:
        raise ValueError("paid step requires --go-from-jacob")
    # Explicit and early: an ordinary OpenRouter key is never accepted as a substitute.
    if not os.environ.get("OPENROUTER_API_KEY_J"):
        raise ValueError("OPENROUTER_API_KEY_J is not set")
    stop = deadline(a.stop_at_utc or cfg["flash_pi"]["stop_at_utc"])
    if stop > deadline(cfg["flash_pi"]["stop_at_utc"]):
        raise ValueError("cannot extend approved cutoff")
    remaining = (stop - datetime.now(UTC)).total_seconds()
    if remaining <= 0 or (a.cmd == "go" and remaining < 1800):
        raise ValueError("cutoff passed or less than 30 minutes remain for go")
    names = ["check", "replay_check", "stub"] + (["smoke"] if a.cmd == "go" else [])
    require_gates(root, cfg, names)
    setup(cfg, root, cfg["flash_pi"]["host"])
    cap = cap_file(root, cfg)
    st = D.load_state(root / "state.json")
    bind(st, cfg)
    # A previous process may have died before recording invocation cleanup. Do not
    # launch any new containers until the operator has reconciled that evidence.
    incomplete = [h.id for h in F.order(cfg)
                  if (D.a_dir(h.id, 1, root).exists()
                      and ((st["a"].get(h.id) or {}).get("state") != "done"
                           or (st["a"].get(h.id) or {}).get("cleanup_unconfirmed") != []))]
    if incomplete:
        raise ValueError(f"previous attempts need cleanup reconciliation: {incomplete}")
    route = F.route(cfg)
    spend = PilotSpend(tags=("s2-flash-pi",), caps={"s2-flash-pi": cfg["caps"]["stage_usd"]},
                       stage=F.STAGE, ledger=CC.ledger(), caps_file=cap)
    spend = WaitingSpend(spend, root, stop)

    def mf(phase, out):
        if phase != "A":
            raise ValueError("first tasks only; compaction and task 2 are disabled")
        model, rs = D.paid_model(route, out, cfg["caps"]["run_usd"], spend)
        return checked_model(model, stop), rs

    runs = F.order(cfg, smoke=a.cmd == "smoke")
    conc = a.concurrency or min(len(runs), cfg["parallelism"]["agents"])
    smoke = F.order(cfg, smoke=True)
    measured = None
    if a.cmd == "go":
        if not smoke_pass(st, smoke, []):
            raise ValueError("saved smoke evidence no longer passes")
        measured = median(st["a"][h.id]["cost_usd"] for h in smoke)
        if measured <= 0:
            raise ValueError("smoke cost must be positive")
    st["order"] = [h.id for h in F.order(cfg)]
    D.save_state(st)
    D.watch_stop()
    result = {}
    tasks_ = D.tasks()
    try:
        with until(stop), tool_deadline(stop):
            result = schedule(cfg, root, st, runs,
                              lambda h: run_one(cfg, h, st, route, spend, tasks_, root, mf),
                              conc, stop, measured, getattr(a, "max_n", None))
    finally:
        left = stubs.remove_kept(root)
        SE.dump(root / "cleanup.json", {"utc": D.utc(), "left_images": left})
    ok = (not left and not D.UNRESOLVED
          and all(rec.get("cleanup_unconfirmed") == []
                  and rec.get("end") in (SE.END_SUBMIT, *CAPPED, SE.END_STAGE_CAP, "cut")
                  for rec in st["a"].values()))
    if a.cmd == "smoke":
        ok = smoke_pass(st, runs, left)
    result.update({"pass": ok, "spend": spending(root), "left_images": left})
    if a.cmd == "smoke":
        gate(root, cfg, "smoke", result)
    print(json.dumps(result, indent=2))
    return 0 if ok else 6


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=F.CONFIG)
    ap.add_argument("--raw", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "replay-check", "status", "analyze"):
        sub.add_parser(name)
    p = sub.add_parser("stub")
    p.add_argument("--host", choices=("laptop", "rig"), default="rig")
    p = sub.add_parser("migrate-design")
    p.add_argument("--from-commit", required=True)
    p.add_argument("--reason", required=True)
    for name in ("smoke", "go"):
        p = sub.add_parser(name)
        p.add_argument("--go-from-jacob", action="store_true")
        p.add_argument("--concurrency", type=int)
        p.add_argument("--stop-at-utc")
        if name == "go":
            p.add_argument("--max-n", type=int)
    a = ap.parse_args(argv)
    try:
        cfg = F.validate(a.config)
        root = (a.raw or W.resolve(cfg["flash_pi"]["output_root"])).resolve()
        # Read-only commands never acquire driver.lock, including while go is active.
        if a.cmd == "status":
            st = D.load_state(root / "state.json")
            result = {"gates": {k: v["pass"] for k, v in st["gates"].items()},
                      "runs": {k: v["state"] for k, v in st["a"].items()},
                      "spend": spending(root) if (root / "budget_caps.toml").exists() else None,
                      "launch_stop": st.get("launch_stop")}
        elif a.cmd == "analyze":
            out = CC.REPO_ROOT / F.RESULTS
            if cfg["flash_pi"]["reasoning_effort"] != "low":
                out /= cfg["flash_pi"]["reasoning_effort"]
            result = F.write_readout(cfg, root, out)
        else:
            setup(cfg, root)
            signal.signal(signal.SIGTERM, lambda *_: L.STOP.set())
            signal.signal(signal.SIGINT, lambda *_: L.STOP.set())
            with D.driver_lock():
                if a.cmd == "check":
                    result = {"pass": True, "message": "plan/config/code/hash binding: OK"}
                    gate(root, cfg, "check", result)
                elif a.cmd == "replay-check":
                    result = F.replay_check(cfg, root)
                    gate(root, cfg, "replay_check", result)
                elif a.cmd == "migrate-design":
                    result = migrate_design(root, cfg, a.from_commit, a.reason)
                elif a.cmd == "stub":
                    setup(cfg, root, a.host)
                    result = stub(cfg, root)
                    gate(root, cfg, "stub", result)
                else:
                    return paid(a, cfg, root)
                SE.dump(root / (a.cmd + ".json"), result)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("pass", True) else 4
    except (ValueError, RuntimeError, OSError) as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
