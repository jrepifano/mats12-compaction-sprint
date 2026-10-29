"""Claude Code's Haiku/Sonnet adapters for agy_nchain's five-task loop.

Reuses cc_chain's CLI, TaskRun, caps, /compact retry and session-file transfer. The generic
loop supplies experiment 4's setup and halt-on-compaction-failure policy. Host credentials
and a stage Spend factory are injected; each phase gets a durable $6-or-less RunSpend.
Quiescence uses agy_chain's container helper; evidence uses cx_chain's staged exporter.
Only the simulator token enters the container, so screening needs no CLI credential store.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import time
import uuid
from pathlib import Path

from compaction.s2.model import RunSpend
from compaction.s2.native import agy_chain as AC
from compaction.s2.native import agy_nchain as G
from compaction.s2.native import cc_chain as X
from compaction.s2.native import claude_code as C
from compaction.s2.native import cx_chain as CX
from compaction.s2.native import key_proxy as K
from compaction.s2.native import transcript as TR

STAGES = {X.MODEL: "SPRINT-S2-CC-CHAIN", K.SONNET_MODEL: "SPRINT-S2-CC-SONNET-CHAIN"}


class ProxySession:
    """Own both context managers, including cleanup when the task container fails to open."""

    def __init__(self, image, cfg, arm, key, host_ip):
        self.cfg, self.image, self.arm = cfg, image, arm
        self.key, self.host_ip = key, host_ip
        self.proxy = self.session = None

    @property
    def cleanup_unconfirmed(self):
        return getattr(self.session, "cleanup_unconfirmed", [])

    def __enter__(self):
        self.proxy = K.KeyProxy(key=self.key, spend=None,
                                log_path=self.cfg.root / "proxy-not-prepared.jsonl",
                                route=K.anthropic_route(self.arm.model), max_calls=0).start()
        try:
            self.session = X._session(self.image, self.proxy.port, self.host_ip,
                                       self.cfg.memory_mb, self.cfg.cpus, self.arm)
            ns = self.session.__enter__()
            ns.key_proxy = self.proxy
            return ns
        except BaseException:
            self.proxy.stop()
            raise

    def __exit__(self, *exc):
        try:
            return self.session.__exit__(*exc)
        finally:
            self.proxy.active_at_stop = self.proxy.stop()


def screen_script(path: str) -> str:
    """The only real credential in the CC container is EMX_SIM_TOKEN; no API key is sent."""
    return ('[ -n "$EMX_SIM_TOKEN" ] || exit 2; '
            f'grep -qF -- "$EMX_SIM_TOKEN" {shlex.quote(path)}; rc=$?; '
            '[ "$rc" = 1 ] && echo SCREEN_CLEAN; exit "$rc"')


def screen(ns, pr, data: bytes) -> bool:
    path = f"/tmp/emx-cc-screen-{uuid.uuid4().hex}"
    try:
        ns.sb.put_file(data, path)
        r = X.N.exec_env(ns.sb, screen_script(path), {"EMX_SIM_TOKEN": ns.server.token},
                         timeout_s=300)
        return r.rc == 1 and "SCREEN_CLEAN" in r.stdout.split()
    except Exception:  # noqa: BLE001 - a failed screen is never clean
        return False
    finally:
        with contextlib.suppress(Exception):
            ns.sb.exec(f"rm -f {path}", timeout_s=30, workdir="/")


def settle(ns, pr) -> bool:
    q = AC.quiesce(ns.sb, getattr(ns, "services", frozenset()))
    pr.h.setdefault("quiet", []).append(q)
    return bool(q["confirmed"])


def _rows(ns, pr) -> list[dict]:
    pr.h.pop("rows", None)
    rows = X.read_session(ns.sb, pr.cid_out)
    if not screen(ns, pr, json.dumps(rows).encode()):
        raise ValueError("session failed the secret screen")
    pr.h["rows"] = rows
    return rows


def skip_compact(pr, kind, why) -> dict:
    return {"kind": kind, "boundary": pr.k, "ok": False, "empty": True,
            "quota": why == "quota stop", "attempts": 0, "deviation": why,
            "step_index": None, "pre_ctx_tokens": None, "summary": "",
            "carried_context": "", "summary_chars": 0}


def boundary_compact(run, sid) -> dict:
    """Retry once, accepting exactly one newly appended boundary, never an imported one."""
    out = {"attempts": []}
    before = C.compact_rows(X.read_session(run.ns.sb, sid))
    out |= {"before": len(before), "before_step": before[-1] if before else -1}
    for attempt in (1, 2):
        try:
            _, info = run.call(X.cli_command(resume=sid, arm=run.arm), "/compact",
                               900 + attempt, extra_s=900)
            after = C.compact_rows(X.read_session(run.ns.sb, sid))
            out["attempts"].append({"attempt": attempt, "after": len(after), "info": info})
            if run.proxy.stage_cap_reached:
                return out | {"ok": False, "quota": True, "deviation": "stage spend cap reached"}
            if len(after) == len(before) + 1 and after[-1] > (before[-1] if before else -1):
                return out | {"ok": True, "boundaries": len(after)}
            if len(after) > len(before) + 1:
                return out | {"ok": False, "deviation": f"{len(after) - len(before)} compactions"}
        except Exception as e:  # noqa: BLE001 - retry once, retaining the failure diagnostic
            out["attempts"].append({"attempt": attempt, "error": f"{type(e).__name__}: {e}"})
    return out | {"ok": False, "deviation": "no new compact_boundary row"}


def compact(ns, pr, kind) -> dict:
    run = pr.h.get("run")
    if run is None or not pr.cid_out:
        return skip_compact(pr, kind, "no session to compact")
    bnd = boundary_compact(run, pr.cid_out)
    pr.h["boundary"] = bnd
    if bnd.get("quota"):
        pr.cfg.quota_stop.set()
    if not settle(ns, pr):
        pr.handoff = False
        return skip_compact(pr, kind, "not quiescent after compaction")
    rows = _rows(ns, pr)
    if not bnd.get("ok"):
        why = (bnd.get("deviation") or "compaction failed").replace(
            "task 2 uncompacted", "chain halted")
        return skip_compact(pr, kind, why) | {
            "attempts": len(bnd.get("attempts") or []), "quota": bool(bnd.get("quota"))}
    bs = C.compact_rows(rows)
    if len(bs) != bnd["before"] + 1 or bs[-1] <= bnd["before_step"]:
        return skip_compact(pr, kind, "no new compact_boundary row")
    nb = C.transcript_boundary(rows, len(bs), model=run.arm.model,
                               step_instruction=pr.prompt).nb
    pr.h["manual_uuid"] = rows[bs[-1]].get("uuid")
    # Everything retained by the session, including the verbatim preserved segment. The
    # CLI system prompt is not session carryover (the converter may use a placeholder).
    carried = json.dumps([m for m in nb.post_messages if m["role"] != "system"],
                         ensure_ascii=False)
    summ = nb.summary
    rec = {"kind": kind, "boundary": pr.k, "ok": bool(summ), "empty": not summ,
           "quota": False, "attempts": len(bnd.get("attempts") or []),
           "deviation": None if summ else "empty summary", "step_index": bs[-1],
           "pre_ctx_tokens": nb.pre_tokens, "summary_chars": len(summ)}
    for key, val in (("summary", summ), ("carried_context", carried)):
        clean = screen(ns, pr, val.encode())
        rec[key] = val if clean else AC.OMITTED
        if not clean:
            rec |= {"ok": False, "deviation": "compaction failed secret screen"}
    return rec


def _merged(prev, rows):
    """The session is append-only; UUIDs retain old evidence even if a CLI rewrites it."""
    merged = list(prev)
    known = {r.get("uuid") for r in prev if r.get("uuid")}
    for i, row in enumerate(rows):
        if row.get("uuid") in known or (i < len(prev) and row == prev[i]):
            continue
        merged.append(row)
        if row.get("uuid"):
            known.add(row["uuid"])
    return merged


def export(ns, pr):
    data, man = X.export_session(ns.sb, pr.cid_out)
    if not screen(ns, pr, data):
        raise ValueError("session export failed the secret screen")
    rows = C.read_jsonl(data.decode("utf-8", "replace"))
    return {man["files"][0]["path"]: data}, man | {
        "conversation_id": pr.cid_out,
        "last_step_index": len(_merged(pr.prev_history, rows)) - 1}


def finish(ns, pr, env, after_evidence):
    """cc_chain's final evidence and transcript, screened before export (cx_chain's helper)."""
    if env is not None and pr.quiet_ok:
        evidence = CX.screened_evidence(env, "final", [ns.server.token], ns.server.token)
        pr.extra["evidence_screen"] = {k: v for k, v in evidence.items() if k != "home"}
        if evidence["ok"]:
            pr.home = evidence["home"]
            try:
                after_evidence()
            except Exception as e:  # noqa: BLE001 - still write the phase result
                pr.note("tip patch", e)
        else:
            pr.extra["evidence_omitted"] = AC.OMITTED
    if pr.quiet_ok and pr.cid_out and "rows" not in pr.h:
        try:
            _rows(ns, pr)
        except Exception as e:  # noqa: BLE001 - missing transcripts remain unknown
            pr.note("transcript", e)
    with contextlib.suppress(Exception):
        asks = ns.server.export()
        if screen(ns, pr, json.dumps(asks).encode()):
            pr.extra["asks"] = asks
    diag = {"notes": pr.notes, "turns": pr.h["run"].turns if pr.h.get("run") else [],
            "boundary": pr.h.get("boundary"), "quiet": pr.h.get("quiet")}
    pr.h["diag_clean"] = screen(ns, pr, json.dumps(diag, default=str).encode())


def merge(prev, pr):
    rows = pr.h.get("rows")
    if rows is None:
        return None, None
    (pr.out / "session.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    merged = _merged(prev, rows)
    msgs, _ = TR.score_messages(merged)
    return merged, msgs


def autos(history, pr):
    out = []
    for i in C.compact_rows(history):
        r = history[i]
        if i < pr.first_step or (r.get("uuid") and r["uuid"] == pr.h.get("manual_uuid")):
            continue
        s = C.summary_row(history, i)
        text = C._block_text(history[s]["message"].get("content")) if s is not None else ""
        out.append({"step_index": i, "pre_ctx_tokens": (r.get("compactMetadata") or {}).get(
            "preTokens"), "summary_chars": len(text), "empty": not text})
    return out


def context_windows(out: Path) -> dict:
    """The CLI result's modelUsage reports contextWindow; never infer a window from a name."""
    windows = {}
    for p in sorted(out.glob("turn_*.jsonl")):
        for line in p.read_text().splitlines():
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                ev = json.loads(line)
                for model, usage in (ev.get("modelUsage") or {}).items():
                    if isinstance(usage.get("contextWindow"), int):
                        windows[model] = usage["contextWindow"]
    return windows


def write_result(pr):
    run, proxy = pr.h.get("run"), pr.h.get("proxy")
    if pr.end == G.END_QUOTA:
        pr.extra |= {"quota_cut": True,
                     "primary_unknown_reason": f"quota refused in phase {pr.k}"}
    elif (pr.compaction or {}).get("quota"):
        pr.extra["chain_quota_cut"] = pr.compaction.get("deviation") or "quota stop"
    pr.quota_cut = pr.extra.get("primary_unknown_reason") or pr.extra.get("chain_quota_cut")
    clean = pr.h.get("diag_clean", False)
    windows = context_windows(pr.out)
    arm = run.arm if run else None
    window = (windows.get(arm.model, windows.get(arm.model.removesuffix("[1m]")))
              if arm else None)
    pr.extra |= {"turns": run.turns if run and clean else [], "conversation_id": pr.cid_out,
                 "boundary": pr.h.get("boundary") if clean else None,
                 "auto_compactions": pr.autos, "checks": pr.h.get("checks"),
                 "session_manifest": (pr.session or (None, None))[1],
                 "proxy_calls": proxy.calls if proxy else 0,
                 "proxy_active_at_stop": getattr(proxy, "active_at_stop", 0),
                 "contextWindow": window, "context_windows": windows}
    err = " | ".join(f"{x['what']}: {x['type']}" +
                     (f": {x['msg']}" if clean or x["pre_agent"] else "")
                     for x in pr.notes) or None
    X.write_run(pr.out, end=pr.end, proxy_log=pr.out / "calls.jsonl", t0=pr.t0,
                rows=pr.history, start_index=pr.index, home=pr.home, extra=pr.extra,
                error=err, second=pr.k > 1)


def skipped(out, end, why, k, primary_unknown=None):
    X.write_run(out, end=end, proxy_log=out / "calls.jsonl", t0=time.monotonic(),
                rows=None, start_index=None, home=None, second=k > 1,
                extra={"skipped": why, "position": k, "primary_unknown_reason": primary_unknown},
                error=why)


def cc_harness(model=X.MODEL, effort=X.EFFORT, *, key=None, spend_factory=None,
               stage=None, host_ip="host.docker.internal") -> G.Harness:
    """Bind one model/effort to the generic loop; spend_factory returns the arm's stage Spend.

    Merely constructing/checking the Harness is offline. Execution requires an injected key
    and a shared stage Spend with matching .stage; the factory may return the same object
    for all phases and concurrent chains. Only the per-phase cap and spend log are new.
    """
    K.anthropic_route(model)  # fail closed before a proxy can be opened
    if model not in STAGES or effort != ("high" if model == X.MODEL else "low"):
        raise ValueError(f"unsupported Claude Code arm: {model} / {effort}")
    stage = STAGES[model] if stage is None else stage
    if stage != STAGES[model]:
        raise ValueError(f"wrong ledger stage for {model}: {stage}")
    arm = X.HAIKU if model == X.MODEL else X.Arm("sonnet", model, effort, session="cc_sonnet_chain")

    def check(cfg):
        if cfg.host is not None:
            raise ValueError("Claude Code key proxy requires the local Docker host")
        if not 0 < cfg.settings.run_usd <= 6:
            raise ValueError("Claude Code per-task cost cap must be positive and at most $6")
        have = {"model": model, "cli_version": C.CLI_VERSION, "compaction_retries": 1}
        if cfg.expect is not None and cfg.expect != have:
            raise ValueError(f"Claude Code adapter differs from config: {cfg.expect}; runs {have}")

    def ensure(cfg):
        check(cfg)
        if not key or spend_factory is None:
            raise ValueError("Claude Code requires a host key and stage spend_factory")
        C.ensure_cli_volume(cfg.host, cfg.tasks[cfg.chain.phases[0].task].image)

    def prepare(ns, pr):
        proxy = ns.key_proxy
        pr.h["proxy"] = proxy
        inner = spend_factory()
        if getattr(inner, "stage", None) != stage:
            raise ValueError(f"stage spend must book to {stage}")
        proxy.spend = RunSpend(inner, cap_usd=pr.cfg.settings.run_usd).attach(pr.out / "spend.jsonl")
        proxy.log_path = pr.out / "calls.jsonl"
        proxy.note = f"{stage} {pr.cfg.chain.id} p{pr.k}"
        proxy.max_calls = min(pr.cfg.settings.max_calls, 400)
        proxy.deadline = pr.t0 + min(pr.cfg.settings.max_seconds, 7200)
        pr.cid_out = pr.cid if pr.k > 1 else str(uuid.uuid4())
        X.install_cli_config(ns.sb, ns.server)
        pr.h["checks"] = X.env_checks(ns.sb)
        if pr.k > 1:
            rel = f"projects/-app/{pr.cid}.jsonl"
            if not pr.cid or set(pr.blobs or {}) != {rel}:
                raise ValueError("previous phase must carry exactly its session file")
            for attempt in (1, 2):
                try:
                    X.import_session(ns.sb, pr.cid, pr.blobs[rel])
                    break
                except Exception:
                    if attempt == 2:
                        raise
            pr.extra["session_import"] = {"files": [rel], "attempt": attempt}
        pr.h["run"] = X.TaskRun(ns, pr.out, proxy, arm)
        pr.extra |= {"model": model, "effort": effort, "stage": stage,
                     "cli_version": C.CLI_VERSION}

    def run_task(ns, pr):
        end = pr.h["run"].drive(pr.cid_out, pr.prompt, new_session=pr.k == 1,
                                chunks=X.CHECKIN_CHUNKS if pr.k == 1 else ())
        if pr.h["proxy"].stage_cap_reached:
            pr.cfg.quota_stop.set()
            return G.END_QUOTA
        return end

    return G.Harness(name="claude_code", ensure=ensure,
                     open=lambda image, cfg: ProxySession(image, cfg, arm, key, host_ip),
                     prepare=prepare, run_task=run_task, settle=settle, compact=compact,
                     skip_compact=skip_compact, export=export, finish=finish, screen=screen,
                     merge=merge, autos=autos, write_result=write_result, skipped=skipped,
                     check=check, cleanup_retry=lambda sess: G.retry_cleanup(sess.session))
