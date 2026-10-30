"""Saved DeepSeek/pi record replay: check, prepare, replay-check, cue-audit, fluency-score, fluency-accept,
stub, smoke, smoke-regate, go, status, analyze. All but smoke/go use fakes or offline checks only.
Never reads .env: a paid operator must supply the pinned route's key in the process environment.
Ported scheduler structure from scripts/week2_summary_fix.py; unchanged summary_fix.run_all
executes each prepared boundary through experiment 1's task-2 setup, scorer and admission.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from compaction import budget
from compaction.s2 import loop as L
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import monitor_controls as M
from compaction.s2.chain import planted_precedent as P
from compaction.s2.chain import planted_precedent_checks as Q
from compaction.s2.chain import session as SE
from compaction.s2.chain import summary_fix as F
from compaction.s2.chain import week2 as W
from compaction.s2.chain.readout import CAPPED


def setup(cfg: dict, root: Path, host: str | None = None):
    root = root.resolve()
    os.environ[CC.RAW_ENV] = str(root)
    CC.PURPOSE = "cobalt"
    D.apply_stage(P.STAGE, cfg["caps"]["stage_usd"])
    D.apply_design(cfg)
    if host is not None:
        if (host == "laptop") != (sys.platform == "darwin"):
            raise ValueError(f"host {host} disagrees with platform {sys.platform}")
        M.HOST = host
        D.set_agent_memory(cfg["parallelism"]["agent_memory_mb"])
        if host == "laptop":
            from compaction.s2.chain import laptop_admission as LA

            CC.AGENT_CPUS = 1
            D.ADMISSION = LA.LaptopAdmission(
                mem_mb=1280, cpus=1, max_agents=4, ramp_agents=4, ramp_after_s=600, log=D.status
            )
    root.mkdir(parents=True, exist_ok=True)


def bind(st, cfg):
    fp = W.fingerprint(cfg)
    if st.get("week2_design") not in (None, fp):
        raise ValueError("existing state belongs to another design")
    st["week2_design"] = fp
    st["week2_config"] = cfg["_source"]["path"]
    D.save_state(st)


def gate(root: Path, cfg: dict, name: str, result: dict):
    st = D.load_state(root / "state.json")
    bind(st, cfg)
    st["gates"][name] = {
        "pass": result["pass"],
        "fingerprint": W.fingerprint(cfg),
        "utc": D.utc(),
        "result": result,
    }
    D.save_state(st)


def require_gates(root, cfg, names):
    st = D.load_state(root / "state.json")
    for name in names:
        g = st["gates"].get(name) or {}
        if g.get("pass") is not True or g.get("fingerprint") != W.fingerprint(cfg):
            raise ValueError(f"missing/current-design gate: {name}")
        if name == "fluency":
            Q.validate_scored_inputs(g["result"])
            Q.validate_fluency_material(cfg, root)


def cap_file(root, cfg):
    p = root / "budget_caps.toml"
    # Dedicated caps file; same checkout ledger. Never modifies another experiment's caps.
    content = (
        "[sprint]\ncheckout = " + json.dumps(str(CC.REPO_ROOT)) + "\n"
        "core_cap_usd = 50.0\nfull_cap_usd = 50.0\ntopup_recorded = false\n"
        "transfer_approval_over_usd = 0.0\n\n[stages.SPRINT-S2-PLANTED-PRECEDENT]\ncap_usd = 50.0\n"
    )
    if p.exists() and p.read_text() != content:
        raise ValueError("existing caps file differs")
    p.write_text(content)
    return p


def spending(root):
    st = budget.status(ledger=CC.ledger(), caps_file=root / "budget_caps.toml")
    return (st.get("stages") or {}).get(P.STAGE) or {"spent": 0, "open": 0, "cap": 50}


def deadline(value: str) -> datetime:
    d = datetime.fromisoformat(value)
    if d.tzinfo is None:
        raise ValueError("--stop-at-utc needs a timezone")
    return d.astimezone(UTC)


@contextlib.contextmanager
def until(stop_at: datetime):
    """No new calls/containers after cutoff; existing loop salvages evidence and cleans up."""
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
    """Guard each reservation/request and interrupt retry backoff, including reasoning wrappers."""
    from compaction.s2.model import OpenAICompatModel

    def check():
        if D.stop_requested() or (stop_at is not None and datetime.now(UTC) >= stop_at):
            L.STOP.set()
            raise L.Stopped("cutoff/stop before request")

    def sleep(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            check()
            L.STOP.wait(min(0.1, end - time.monotonic()))
        check()

    inner = model
    while hasattr(inner, "inner"):
        inner = inner.inner
    if isinstance(inner, OpenAICompatModel):
        inner.before_attempt = check
        inner.sleep = sleep

    class Guard:
        route = model.route

        def complete(self, *args, **kwargs):
            check()
            return model.complete(*args, **kwargs)

    return Guard()


def stub(cfg, source, root):
    from compaction.s2.chain import stub_monitor_controls as SMC
    from compaction.s2.chain import stubs
    from compaction.s2.chain.reasoning import LOG_NAME, ReasoningLog
    from compaction.s2.config import ROUTES
    from compaction.s2.model import StubModel

    target = root / "local"
    P.prepare(cfg, source, target)
    runs = P.order(cfg, smoke=True)
    st = D.load_state(target / "state.json")
    bind(st, cfg)
    captured = {}

    def mf(phase, out):
        if phase != "B":
            raise ValueError("replay cannot buy compaction")
        b = next(b for b in runs if b.id == out.parent.name)
        inner = StubModel(actions=SMC.b_actions(b.cell.b_task), route=ROUTES["stub"])

        class Capture:
            route = inner.route

            def complete(self, messages, *args, **kwargs):
                captured.setdefault(b.id, json.loads(json.dumps(messages)))
                return inner.complete(messages, *args, **kwargs)

        return ReasoningLog(Capture(), out / LOG_NAME), None

    try:
        states = F.run_all(
            runs,
            st,
            None,
            None,
            D.tasks(),
            target,
            concurrency=1,
            model_for=mf,
            grade=False,
            gate=P.ReplayGate(st, {}, root=target, spent_fn=lambda: 0),
        )
        checks = []
        for b in runs:
            # A resumed completed stub is checked from its saved messages too.
            rec = st["b"][b.id]
            msgs = captured.get(b.id)
            if msgs is None and rec.get("dir"):
                saved = json.loads((Path(rec["dir"]) / "messages.json").read_text())
                idx = json.loads((Path(rec["dir"]) / "result.json").read_text())["extra"][
                    "b_message_index"
                ]
                msgs = saved[: idx + 1]
            expected = json.loads(F.comp_path(target, b).read_text())["post_messages"]
            row = F.score(Path(rec["dir"]), b) if rec.get("dir") else {}
            checks.append(
                {
                    "id": b.id,
                    "pass": states[b.id] == "done"
                    and msgs is not None
                    and msgs[:-1] == expected
                    and row.get("teammate_lost") is True,
                    "state": states[b.id],
                    "teammate_lost": row.get("teammate_lost"),
                }
            )
    finally:
        left = stubs.remove_kept(target)
    return {
        "pass": all(c["pass"] for c in checks) and not left,
        "checks": checks,
        "left_images": left,
        "cost_usd": 0,
    }


def smoke_pass(st: dict, runs: list, left_images: list) -> bool:
    """A smoke may hit its cap, but must save primary evidence and clean up."""
    return (
        all(
            (rec := st["b"].get(b.id, {})).get("state") == "done"
            and rec.get("end") in (SE.END_SUBMIT, *CAPPED)
            and type(rec.get("teammate_lost")) is bool
            and rec.get("cleanup_unconfirmed") == []
            for b in runs
        )
        and not left_images
    )


def smoke_regate(cfg: dict, root: Path, reason: str) -> dict:
    """Re-evaluate saved smoke evidence only; never launch or finalize an attempt."""
    if not reason.strip():
        raise ValueError("smoke-regate requires a nonempty reason")
    path = root / "smoke" / "state.json"
    if not path.is_file():
        raise ValueError("missing smoke state")
    st = D.load_state(path)
    runs = P.order(cfg, smoke=True)
    ids = [b.id for b in runs]
    if st.get("week2_design") != W.fingerprint(cfg):
        raise ValueError("smoke config fingerprint changed")
    if st.get("order") != {"ids": ids, "sha256": F.order_sha256(runs)} or set(st["b"]) != set(ids):
        raise ValueError("smoke order/run ids differ")
    cleanup = json.loads((root / "smoke" / "cleanup.json").read_text())
    left = cleanup.get("left_images")
    if not isinstance(left, list):
        raise ValueError("missing smoke cleanup evidence")  # noqa: TRY004 - invalid saved artifact
    saved = D.load_state(root / "state.json")["gates"].get("smoke") or {}
    if saved.get("fingerprint") != W.fingerprint(cfg) or not isinstance(saved.get("result"), dict):
        raise ValueError("missing/current-design original smoke gate")
    previous = saved["result"]
    result = {
        "pass": smoke_pass(st, runs, left),
        "states": {ident: st["b"][ident].get("state") for ident in ids},
        "left_images": left,
        "reason": reason.strip(),
        "regated": True,
        "original": previous.get("original", previous),
    }
    gate(root, cfg, "smoke", result)
    return result


def paid(a, cfg, root, source):
    from compaction.s2.chain import stubs
    from compaction.s2.config import Route
    from compaction.s2.interact_run import PilotSpend

    if not a.go_from_jacob:
        raise ValueError("paid step requires --go-from-jacob")
    stop = deadline(a.stop_at_utc or cfg["planted_precedent"]["stop_at_utc"])
    if stop > deadline(cfg["planted_precedent"]["stop_at_utc"]):
        raise ValueError("cannot extend approved cutoff")
    names = ["replay_check", "cue_audit", "fluency", "stub"]
    if a.cmd == "go":
        names.append("smoke")
    require_gates(root, cfg, names)
    setup(cfg, root, "rig")
    target = root / "smoke" if a.cmd == "smoke" else root
    # Source/spec and all prepared boundaries checked again before route key access.
    P.prepare(cfg, source, target)
    runs = P.order(cfg, smoke=a.cmd == "smoke")
    st = D.load_state(target / "state.json")
    bind(st, cfg)
    cap = cap_file(root, cfg)
    route = Route(**json.loads(P.checked(CC.REPO_ROOT, cfg["planted_precedent"]["route"]))["route"])
    spend = PilotSpend(
        tags=("s2-cobalt",),
        caps={"s2-cobalt": 50},
        stage=P.STAGE,
        ledger=CC.ledger(),
        caps_file=cap,
    )

    def mf(phase, out):
        if phase != "B":
            raise ValueError("missing saved boundary; refusing fresh compaction")
        model, rs = D.paid_model(route, out, cfg["caps"]["run_usd"], spend)
        return checked_model(model, stop), rs

    conc = a.concurrency or (1 if a.cmd == "smoke" else cfg["parallelism"]["agents"])
    if not 1 <= conc <= cfg["parallelism"]["agents"]:
        raise ValueError("concurrency outside config")
    st["order"] = {"ids": [b.id for b in runs], "sha256": F.order_sha256(runs)}
    st.setdefault("admission", []).append(
        {"utc": D.utc(), "concurrency": conc, "host": "rig", "stop_at_utc": stop.isoformat()}
    )
    D.save_state(st)
    D.watch_stop()
    states = {}
    interrupted = None
    try:
        with until(stop):
            states = F.run_all(
                runs,
                st,
                route,
                spend,
                D.tasks(),
                target,
                concurrency=conc,
                gate=P.ReplayGate(
                    st, {}, root=target, cap=50, spent_fn=lambda: spending(root).get("spent", 0)
                ),
                model_for=mf,
                grade=False,  # primary outcome uses saved Git evidence; no grading admission
                caps={
                    "max_calls": cfg["caps"]["max_calls"],
                    "max_seconds": cfg["caps"]["max_seconds"],
                },
            )
    except (D.Halt, L.Stopped) as e:
        interrupted = str(e)
    finally:
        P.finalize(st, runs, target)
        F.write_rows(st, runs, target)
        left = stubs.remove_kept(target)
        SE.dump(target / "cleanup.json", {"utc": D.utc(), "left_images": left})
    ok = (
        all(
            (st["b"].get(b.id) or {}).get("state") == "done"
            and (st["b"].get(b.id) or {}).get("end") == SE.END_SUBMIT
            and not (st["b"].get(b.id) or {}).get("cleanup_unconfirmed")
            for b in runs
        )
        and not left
    )
    if a.cmd == "smoke":
        ok = smoke_pass(st, runs, left)
    result = {
        "pass": ok,
        "states": states,
        "interrupted": interrupted,
        "spend": spending(root),
        "left_images": left,
    }
    if a.cmd == "smoke":
        gate(root, cfg, "smoke", result)
    print(json.dumps(result, indent=2))
    return 0 if ok else 6


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=P.CONFIG)
    ap.add_argument("--raw", type=Path)
    ap.add_argument("--source-raw", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ["check", "prepare", "replay-check", "status", "analyze"]:
        sub.add_parser(name)
    for name in ["stub", "cue-audit"]:
        p = sub.add_parser(name)
        p.add_argument("--host", choices=["laptop", "rig"], default="rig")
    p = sub.add_parser("fluency-score")
    p.add_argument("answers", type=Path, nargs="+")
    p = sub.add_parser("fluency-accept")
    p.add_argument("--item", required=True)
    p.add_argument("--honest-item", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("smoke-regate")
    p.add_argument("--reason", required=True)
    for name in ["smoke", "go"]:
        p = sub.add_parser(name)
        p.add_argument("--go-from-jacob", action="store_true")
        p.add_argument("--concurrency", type=int)
        p.add_argument("--stop-at-utc")
    a = ap.parse_args(argv)
    try:
        cfg = P.validate(a.config)
        if a.cmd == "check":
            print("plan/config/code/hash binding: OK")
            return 0
        root = (a.raw or W.resolve(cfg["planted_precedent"]["output_root"])).resolve()
        source = (a.source_raw or W.resolve(cfg["planted_precedent"]["source_root"])).resolve()
        if root.is_relative_to(source) or source.is_relative_to(root):
            raise ValueError("source/output overlap")
        setup(cfg, root)
        signal.signal(signal.SIGTERM, lambda *_: L.STOP.set())
        signal.signal(signal.SIGINT, lambda *_: L.STOP.set())
        with D.driver_lock():
            if a.cmd == "prepare":
                result = P.prepare(cfg, source, root)
                result["fluency"] = Q.fluency(cfg, root)
            elif a.cmd == "replay-check":
                P.prepare(cfg, source, root)
                result = P.replay_check(cfg, root)
                gate(root, cfg, "replay_check", result)
            elif a.cmd in ["cue-audit", "stub"]:
                setup(cfg, root, a.host)
                P.prepare(cfg, source, root)
                result = (
                    Q.container_audit(cfg, root)
                    if a.cmd == "cue-audit"
                    else stub(cfg, source, root)
                )
                gate(root, cfg, "cue_audit" if a.cmd == "cue-audit" else "stub", result)
            elif a.cmd == "fluency-score":
                key = Q.validate_fluency_material(cfg, root)
                refs = Q.input_hashes(root, key, a.answers)
                result = Q.score_fluency(key, [json.loads(p.read_text()) for p in a.answers])
                result["input_hashes"] = refs
                Q.validate_scored_inputs(result)
                gate(root, cfg, "fluency", result)
            elif a.cmd == "fluency-accept":
                st = D.load_state(root / "state.json")
                g = st["gates"].get("fluency") or {}
                if g.get("fingerprint") != W.fingerprint(cfg):
                    raise ValueError("missing/current-design fluency score")
                Q.validate_fluency_material(cfg, root)
                result = Q.accept_fluency(g["result"], a.item, a.honest_item, a.reason)
                gate(root, cfg, "fluency", result)
            elif a.cmd in ["smoke", "go"]:
                return paid(a, cfg, root, source)
            elif a.cmd == "smoke-regate":
                result = smoke_regate(cfg, root, a.reason)
            elif a.cmd == "status":
                st = D.load_state(root / "state.json")
                result = {
                    "gates": {k: v["pass"] for k, v in st["gates"].items()},
                    "runs": {k: v["state"] for k, v in st["b"].items()},
                    "spend": spending(root) if (root / "budget_caps.toml").exists() else None,
                    "fluency_overrides": st["gates"]
                    .get("fluency", {})
                    .get("result", {})
                    .get("overrides", []),
                }
            elif a.cmd == "analyze":
                from week2_planted_precedent_readout import write_readout

                st = D.load_state(root / "state.json")
                P.finalize(st, P.order(cfg), root)
                F.write_rows(st, P.order(cfg), root)
                result = write_readout(cfg, root, CC.REPO_ROOT / P.RESULTS)
            if a.cmd not in ["status", "analyze", "prepare"]:
                SE.dump(root / (a.cmd + ".json"), result)
            print(json.dumps(result, indent=2, default=str))
            return 0 if result.get("pass", True) else 4
    except (ValueError, RuntimeError, OSError) as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
