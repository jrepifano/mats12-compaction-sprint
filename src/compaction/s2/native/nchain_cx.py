"""Codex adapter for `agy_nchain`'s N-task loop (week-2 plan v0.4).

The two-task driver's CLI, turn rules, watcher, RPC boundary, rollout transfer and screened
scorer evidence are reused from `cx_chain`. Each phase gets its own container. A boundary
must append exactly one encrypted compaction; its plaintext summary is empty by design.
`carried_context` contains only the replacement history's readable messages, screened before
storage. Failed compactions halt the generic loop, unlike the two-task driver's fallback.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from compaction.s2.chain import env as E
from compaction.s2.native import agy_chain as AC
from compaction.s2.native import agy_nchain as G
from compaction.s2.native import codex as C
from compaction.s2.native import common as N
from compaction.s2.native import cx_chain as X
from compaction.s2.native import cx_transcript as TR

COMPACTION_RETRIES = 1  # cx_chain.boundary_compact: one attempt plus one confirmed-zero retry


@dataclass
class TaskRun(X.TaskRun):
    """The existing turn loop, also observing the chain runner's shared quota stop."""

    quota_stop: threading.Event = field(default_factory=threading.Event)

    def call(self, cmd: str, stdin: str, k: int):
        if self.quota_stop.is_set():
            return X.T.Stop(text=""), {"thread_id": self.thread, "quota": True}
        return super().call(cmd, stdin, k)


def cx_runtime() -> dict:
    return {"model": X.MODEL, "cli_version": C.CLI_VERSION,
            "compaction_retries": COMPACTION_RETRIES}


def cx_check(cfg: G.ChainConfig) -> None:
    if cfg.expect is None:
        return
    have = cx_runtime()
    if set(cfg.expect) != set(have) or cfg.expect != have:
        raise ValueError(f"the codex adapter differs from the config: {cfg.expect}; runs {have}")


def cx_ensure(cfg: G.ChainConfig) -> None:
    cx_check(cfg)
    C.ensure_cli_volume(cfg.host, cfg.tasks[cfg.chain.phases[0].task].image)


def cx_open(image: str, cfg: G.ChainConfig):
    return X._session(image, cfg)


def _secrets(ns, pr: G.PhaseRun) -> list[str]:
    """Refresh credentials fail-closed before screening or exporting anything."""
    pr.h["secrets_ok"] = False
    values = X.collect_secrets(ns.sb, pr.h.get("secrets") or [])
    if not values:
        raise ValueError("no credentials to screen for")
    pr.h["secrets"], pr.h["secrets_ok"] = values, True
    pr.h["sim_tokens"] = [ns.server.token] if ns.server.token else []
    return values


def cx_screen(ns, pr: G.PhaseRun, data: bytes) -> bool:
    if not pr.h.get("secrets_ok"):
        return False
    return X.redact_count(data, pr.h["secrets"] + pr.h.get("sim_tokens", []))[1] == 0


def cx_prepare(ns, pr: G.PhaseRun) -> None:
    """The two-task setup, importing the same rollout path for every later phase."""
    auth = Path(getattr(pr.cfg, "auth_path", Path.home() / ".codex/auth.json")).read_bytes()
    pr.h["secrets"] = C.auth_tokens(json.loads(auth))
    pr.h["startup_secrets"] = list(pr.h["secrets"])
    if not pr.h["secrets"]:
        raise ValueError("the auth file holds no tokens")
    pr.h["staging"] = Path(tempfile.mkdtemp(prefix="emx-cx-nchain-"))
    X.install_cli_config(ns.sb, auth)
    pr.h["checks"] = X.X.env_checks(ns.sb)
    pr.extra["egress_check"] = N.egress_check(ns.sb, ns.egress, "https://chatgpt.com/")
    if not pr.extra["egress_check"].get("ok"):
        raise RuntimeError("egress check failed")
    base_calls = 0
    pr.h["base_lines"] = 0
    if pr.k > 1:
        if not pr.cid or not pr.blobs or len(pr.blobs) != 1:
            raise ValueError("expected one carried rollout and its thread id")
        rel, data = next(iter(pr.blobs.items()))
        if not rel.startswith("sessions/") or ".." in Path(rel).parts:
            raise ValueError("invalid carried rollout path")
        session = {"rel": rel, "data": data, "thread_id": pr.cid}
        for attempt in (1, 2):
            try:
                X.import_session(ns.sb, session)
                break
            except Exception:
                if attempt == 2:
                    pr.extra["primary_unknown_reason"] = "transfer_failed"
                    raise
        imported = X.parse_rollout(data)
        pr.h["imported"] = data
        pr.h["base_lines"] = len(imported)
        base_calls = TR.task_usage(imported)["n_calls"]
        pr.extra["session_import"] = {"path": rel, "verified": True}
    X.start_watch(ns.sb, base_calls + pr.cfg.settings.max_calls)
    pr.h["run"] = TaskRun(ns, pr.h["staging"], deadline=pr.t0 + pr.cfg.settings.max_seconds,
                          quota_stop=pr.cfg.quota_stop)


def cx_run_task(ns, pr: G.PhaseRun) -> str:
    if pr.cfg.quota_stop.is_set():
        return X.END_QUOTA
    end = pr.h["run"].drive(pr.prompt, resume=pr.cid)
    if end == X.END_QUOTA:
        pr.cfg.quota_stop.set()
    return end


def _quiet(ns, pr: G.PhaseRun, why: str) -> bool:
    q = AC.quiesce(ns.sb, getattr(ns, "services", frozenset()))
    pr.h.setdefault("quiet", []).append({"why": why, **q})
    return bool(q.get("confirmed"))


def cx_settle(ns, pr: G.PhaseRun) -> bool:
    with contextlib.suppress(Exception):
        X.stop_watch(ns.sb)
    with contextlib.suppress(Exception):
        C.stop_cli(ns.sb)
    quiet = _quiet(ns, pr, "task end")
    run = pr.h.get("run")
    if quiet and run is not None and run.thread:
        with contextlib.suppress(Exception):
            pr.h["task_end"] = len(X.read_rollout(ns.sb, run.thread)[2])
    return quiet


def cx_skip_compact(pr: G.PhaseRun, kind: str, why: str) -> dict:
    pr.h["boundary"] = {"ok": False, "skipped": why}
    return {"kind": kind, "boundary": pr.k, "ok": False, "empty": True, "encrypted": False,
            "quota": why == "quota stop", "attempts": 0, "deviation": why, "step_index": None,
            "pre_ctx_tokens": None, "summary": "", "user_requests": "", "checkpoint": "",
            "carried_context": "", "summary_chars": 0}


def readable_context(compacted: dict) -> str:
    """Readable messages only; never mistake ciphertext for a summary."""
    parts = []
    for item in (compacted.get("payload") or {}).get("replacement_history") or []:
        if item.get("type") == "message":
            text = C._content_text(item.get("content"))
            if text:
                parts.append(f"{item.get('role', 'message')}: {text}")
    return "\n\n".join(parts)


def _has_ciphertext(cp: dict) -> bool:
    return any(isinstance(x.get("encrypted_content"), str) and x["encrypted_content"]
               for x in (C.encrypted_items(cp) if cp else []))


def _pre_ctx_tokens(lines: list[dict], step: int | None) -> int | None:
    if step is None:
        return None
    calls = [p for p in TR._token_counts(lines[:step])
             if ((p.get("info") or {}).get("last_token_usage") or {}).get("input_tokens")]
    return calls[-1]["info"]["last_token_usage"]["input_tokens"] if calls else None


def _carried_start(pr: G.PhaseRun, run, data: bytes) -> bool:
    return pr.k == 1 or bool(pr.h.get("imported") and run is not None
                            and run.thread == pr.cid and X._verified_start(run.turns, pr.cid)
                            and data.startswith(pr.h["imported"]))


def _unknown(pr: G.PhaseRun, why: str) -> None:
    if pr.end != X.END_QUOTA:
        pr.extra.setdefault("primary_unknown_reason", why)
        pr.end, pr.session = X.END_FAILED, None


def cx_compact(ns, pr: G.PhaseRun, kind: str) -> dict:
    run = pr.h.get("run")
    if run is None or not run.thread:
        return cx_skip_compact(pr, kind, "no thread to compact")
    rec = cx_skip_compact(pr, kind, "compaction unverified")
    bnd = X.boundary_compact(run, run.thread)
    pr.h["boundary"] = bnd
    attempts = bnd.get("attempts") or []
    quota_in_attempts = any(q in (a.get("error") or "").lower()
                            for a in attempts for q in X.QUOTA)
    if any(not a.get("stopped") for a in attempts) or not _quiet(ns, pr, "after boundary"):
        pr.handoff = False
        if quota_in_attempts:
            pr.cfg.quota_stop.set()
        return rec | {"attempts": len(attempts), "deviation": "boundary not quiescent",
                      "quota": quota_in_attempts}
    _secrets(ns, pr)
    _path, data, lines = X.read_rollout(ns.sb, run.thread)
    indices = C.compaction_lines(lines)
    before = bnd.get("before")
    new = indices[before:] if isinstance(before, int) else []
    pr.h["boundary_steps"] = set(new)
    step = new[0] if len(new) == 1 else None
    cp = lines[step] if step is not None else {}
    encrypted = _has_ciphertext(cp)
    carried_start = _carried_start(pr, run, data)
    if not carried_start:
        pr.handoff = False
        _unknown(pr, "b_not_from_carried_session")
    ok = bool(bnd.get("ok") and len(new) == 1 and encrypted and carried_start)
    carried = readable_context(cp)
    if not cx_screen(ns, pr, carried.encode()):
        carried = AC.OMITTED
    quota = not ok and quota_in_attempts
    if quota:
        pr.cfg.quota_stop.set()
    return rec | {"ok": ok, "encrypted": bool(encrypted), "quota": quota,
                  "attempts": len(attempts), "step_index": step,
                  "pre_ctx_tokens": _pre_ctx_tokens(lines, step),
                  "carried_context": carried,
                  "deviation": None if ok else bnd.get("deviation") or
                  "expected exactly one new encrypted compaction"}


def cx_export(ns, pr: G.PhaseRun) -> tuple[dict, dict]:
    secrets = _secrets(ns, pr)
    sess, man = X.export_session(ns.sb, pr.h["run"].thread, secrets)
    if not _carried_start(pr, pr.h["run"], sess["data"]):
        pr.handoff = False
        _unknown(pr, "b_not_from_carried_session")
        raise ValueError("export is not from the carried session")
    # Match cx_chain: the dead, per-session simulator token may be in carried history;
    # credentials may never be carried. Transfer the rollout byte-for-byte.
    return {sess["rel"]: sess["data"]}, man | {"conversation_id": sess["thread_id"],
                                               "last_step_index": man["lines"] - 1}


def cx_finish(ns, pr: G.PhaseRun, env, after_evidence) -> None:
    """Two-task evidence/rollout capture, with refreshed credentials and private raw logs."""
    run = pr.h.get("run")
    try:
        secrets = _secrets(ns, pr)
    except Exception as e:  # noqa: BLE001 - nothing kept with unknown credentials
        pr.note("secrets", e)
        pr.end, pr.session = X.END_FAILED, None
        pr.extra["primary_unknown_reason"] = "secrets_uncollected"
        if pr.compaction is not None:
            pr.compaction = G.screen_record(ns, pr, pr.compaction)
        return
    if env is not None and pr.quiet_ok:
        ev = X.screened_evidence(env, "final", secrets, ns.server.token)
        pr.home = ev["home"]
        pr.extra["evidence"] = {k: ev[k] for k in ("ok", "why", "attempts")}
        if ev["ok"]:
            try:
                after_evidence()
            except Exception as e:  # noqa: BLE001
                pr.note("tip patch", e)
    if env is not None and pr.home is None:
        _unknown(pr, "evidence_withheld")
    if run is not None and run.thread and pr.quiet_ok:
        try:
            _path, data, _lines = X.read_rollout(ns.sb, run.thread)
            if pr.k > 1:
                pr.extra["imported_prefix_intact"] = data.startswith(pr.h.get("imported", b""))
                if pr.end != X.END_QUOTA and not _carried_start(pr, run, data):
                    _unknown(pr, "b_not_from_carried_session")
            clean, n = X.redact_count(data, secrets + pr.h.get("sim_tokens", []))
            pr.extra["secrets_in_rollout"] = n
            (pr.out / "rollout.jsonl").write_bytes(clean)
            pr.h["lines"] = X.parse_rollout(clean)
        except Exception as e:  # noqa: BLE001 - missing transcript is explicitly unknown
            pr.note("rollout", e)
            _unknown(pr, "rollout_unreadable")
    if pr.h.get("lines") is None:
        _unknown(pr, "rollout_unreadable")
    if pr.k > 1 and pr.end != X.END_QUOTA and not (pr.h.get("imported") and run is not None
                         and run.thread == pr.cid
                         and X._verified_start(run.turns, pr.cid)
                         and pr.extra.get("imported_prefix_intact") is True):
        _unknown(pr, "b_not_from_carried_session")
    with contextlib.suppress(Exception):
        pr.extra["asks"] = ns.server.export()
    with contextlib.suppress(Exception):
        pr.extra["egress"] = X.egress_summary(ns.egress.events())
    pr.extra["auth_refreshed"] = sorted(secrets) != sorted(pr.h.get("startup_secrets", []))
    with contextlib.suppress(Exception):
        pr.extra["codex_home"] = X.home_listing(ns.sb)
    with contextlib.suppress(Exception):
        pr.extra["rollouts_in_container"] = len(C.container_rollouts(ns.sb))
    with contextlib.suppress(Exception):
        pr.extra["memory"] = E.parse_memory(N.tolerant_exec(ns.sb)(
            "grep -h '^oom' /sys/fs/cgroup/memory.events 2>/dev/null; "
            "echo peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null); "
            "echo max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null)",
            timeout_s=60, workdir="/").stdout)


def cx_merge(prev_history: list[dict], pr: G.PhaseRun):
    # Codex's rollout is append-only and already includes every imported phase.
    lines = pr.h.get("lines")
    return (lines, TR.score_messages(lines)[0]) if lines is not None else (None, None)


def cx_autos(history: list[dict], pr: G.PhaseRun) -> list[dict]:
    """Only compactions during this task, excluding every entry from our boundary RPC."""
    hi = pr.h.get("task_end")
    return [{"step_index": i, "pre_ctx_tokens": _pre_ctx_tokens(history, i),
             "summary_chars": 0, "empty": True, "encrypted": _has_ciphertext(history[i])}
            for i in C.compaction_lines(history) if i >= pr.h.get("base_lines", 0)
            and (hi is None or i < hi) and i not in pr.h.get("boundary_steps", ())]


def cx_write_result(pr: G.PhaseRun) -> None:
    run = pr.h.get("run")
    pr.cid_out = (run.thread if run else None) or pr.cid
    if pr.end == X.END_QUOTA:
        pr.extra.setdefault("primary_unknown_reason", "quota_refused")
        pr.quota_cut = "quota_refused"
    if (pr.compaction or {}).get("quota"):
        pr.extra["chain_quota_cut"] = pr.quota_cut = "quota refused at boundary"
    pr.extra |= {"thread_id": pr.cid_out, "conversation_id": pr.cid_out,
                 "turns": run.turns if run else [], "checks": pr.h.get("checks"),
                 "quiescence": pr.h.get("quiet", []), "boundary": pr.h.get("boundary"),
                 "auto_compactions": pr.autos, "session_manifest": (pr.session or ({}, None))[1]}
    if pr.history is not None:
        lo, hi = pr.h.get("base_lines", 0), pr.h.get("task_end")
        pr.extra |= {"system_prompt": TR.base_instructions_sha(pr.history),
                     "models": sorted(set(TR.turn_models(pr.history, lo, hi))),
                     "compactions_in_task": X.n_compacted(pr.history[lo:hi]),
                     "compactions_in_rollout": X.n_compacted(pr.history)}
        own = TR.task_usage(pr.history, pr.h.get("base_lines", 0), pr.h.get("task_end"))
        pr.extra["ctx_tokens_at_start"] = next((
            (x.get("info") or {}).get("last_token_usage", {}).get("input_tokens")
            for x in TR._token_counts(pr.history[lo:hi])
            if (x.get("info") or {}).get("last_token_usage", {}).get("input_tokens")), None)
        pr.extra["usage"] = own
    err = " | ".join(f"{n['what']}: {n['type']}: {n['msg']}" for n in pr.notes) or None
    secrets = pr.h.get("secrets", []) + pr.h.get("sim_tokens", [])
    staging = pr.h.get("staging")
    try:
        if pr.h.get("secrets_ok"):
            if staging is not None:
                pr.extra["secrets_in_logs"] = X.persist_redacted(staging, pr.out, secrets)
        else:
            pr.extra = X.safe_extra(pr.extra)
            err = X.safe_error([n["what"] for n in pr.notes])
        rng = ((pr.h.get("base_lines", 0), pr.h["task_end"])
               if pr.h.get("task_end") is not None else None)
        X.write_run(pr.out, end=pr.end, t0=pr.t0, lines=pr.history, rng=rng,
                    start_index=pr.index, home=pr.home, extra=pr.extra, error=err,
                    second=pr.k > 1, secrets=secrets)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def cx_skipped(out: Path, end: str, why: str, k: int, primary_unknown: str | None) -> None:
    X.write_run(out, end=end, t0=time.monotonic(), lines=None, rng=None, start_index=None,
                home=None, extra={"skipped": why, "primary_unknown_reason": primary_unknown,
                                  "position": k}, error=why, second=k > 1)


CX = G.Harness(name="codex", ensure=cx_ensure, open=cx_open, prepare=cx_prepare,
               run_task=cx_run_task, settle=cx_settle, compact=cx_compact,
               skip_compact=cx_skip_compact, export=cx_export, finish=cx_finish,
               screen=cx_screen, merge=cx_merge, autos=cx_autos, write_result=cx_write_result,
               skipped=cx_skipped, wrap=G.native_prompt, check=cx_check,
               cleanup_retry=G.retry_cleanup)
