"""Codex N-phase adapter: real loop/turn rules/transfer with fake containers and RPC ($0)."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_native_agy_nchain import BASE, TASKS, FakeEnv, World, config, result, rows

from compaction.s2.chain import env as E
from compaction.s2.chain import nchain
from compaction.s2.chain import week2 as W
from compaction.s2.native import agy_nchain as G
from compaction.s2.native import nchain_cx as M
from compaction.s2.native import turns as T

TID = "01a1832a-baee-7071-a31d-a981e579b85b"
REL = f"sessions/2026/10/29/rollout-2026-10-29T00-00-00-{TID}.jsonl"
AUTH = "credential-123456789-0123456789-0123456789"
SIM = "sim-token-123456789"


@pytest.fixture
def settings():
    return nchain.settings_from_config(W.load_config("configs/week2/chain_stress.toml"))


def message(text, role="user"):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text", "text": text}]}}


def compacted(text="remember the prior task", encrypted=True):
    history = [message(text)["payload"]]
    if encrypted:
        history.append({"type": "compaction", "encrypted_content": "ciphertext"})
    return {"type": "compacted", "payload": {"message": "", "replacement_history": history}}


class SB:
    def __init__(self, name, world):
        self.name, self.host, self.world = name, None, world
        self.files = {}

    def put_file(self, data, path):
        self.files[path] = data
        if path.endswith(".jsonl"):
            self.world.imports.append((self.name, path, data))

    def read_file(self, path, **kw):
        return self.files[path]

    def exec(self, *a, **kw):
        return SimpleNamespace(stdout="oom 0\noom_kill 0\npeak=1\nmax=2\n", stderr="", rc=0)

    def append(self, *lines, tid=TID):
        path = f"{M.C.HOME_C}/{REL.replace(TID, tid)}"
        self.files[path] = self.files.get(path, b"") + b"".join(
            json.dumps(x).encode() + b"\n" for x in lines)


def install(monkeypatch, tmp_path, settings, *, compacts=(), ends=(), autos=()):
    world = World(ends or [M.X.END_SUBMITTED] * 5, compacts, autos)
    world.limits, world.deadlines, world.staging, world.secret_failure = [], [], [], False
    monkeypatch.setattr(G, "UNRESOLVED_CLEANUP", [])
    monkeypatch.setattr(E, "REPO_NAMES", dict(E.REPO_NAMES))
    monkeypatch.setattr(G.nchain, "push_setup", lambda tid, texts_first=False: nchain.PushSetup(
        tid, BASE[:12], f"feature/{tid}", f"feature {tid}", "NOTES.md", "draft.py",
        f"draft for {tid}\n", f"TAG-{tid}", f"repo-{tid}"))
    monkeypatch.setattr(M.C, "ensure_cli_volume", lambda host, image: "volume")

    class NS:
        def __init__(self, image):
            self.sb = SB(f"c{len(world.sessions)}", world)
            world.sessions.append(image)
            self.services = frozenset()
            self.server = SimpleNamespace(token=SIM, export=lambda: {"questions": []})
            self.egress = SimpleNamespace(events=list, down=list)
            self.inv = SimpleNamespace(cleanup=list)
            self.cleanup_unconfirmed = []

        def __enter__(self):
            return self

        def __exit__(self, *e):
            world.events.append(("closed", self.sb.name))
    monkeypatch.setattr(M.X, "_session", lambda image, cfg: NS(image))
    monkeypatch.setattr(G.X, "install_chain", lambda sb, task, out, branches: (
        FakeEnv(out, world, sb), ["tests/test_x.py"]))
    monkeypatch.setattr(M.X, "install_cli_config", lambda sb, auth: None)
    monkeypatch.setattr(M.X.X, "env_checks", lambda sb: {"which_git": []})
    monkeypatch.setattr(M.N, "egress_check", lambda *a: {"ok": True})
    monkeypatch.setattr(M.N, "tolerant_exec", lambda sb: sb.exec)
    monkeypatch.setattr(M.C, "container_rollouts", lambda sb: sorted(sb.files))
    monkeypatch.setattr(M.X, "start_watch", lambda sb, limit: world.limits.append(limit))
    monkeypatch.setattr(M.X, "stop_watch", lambda sb: None)
    monkeypatch.setattr(M.C, "stop_cli", lambda sb: None)
    monkeypatch.setattr(M.X, "cli_env", lambda ns: {})
    monkeypatch.setattr(M.AC, "quiesce", lambda *a: {
        "confirmed": True, "stopped": [], "survivors": [], "errors": []})

    def tokens(sb):
        if world.secret_failure:
            raise RuntimeError(f"cannot read {AUTH}")
        return [AUTH]
    monkeypatch.setattr(M.C, "current_tokens", tokens)
    monkeypatch.setattr(M.X, "screened_evidence", lambda env, label, secrets, tok: {
        "ok": True, "why": None, "attempts": 1, "home": env.finish_traps(label)})

    def call(self, cmd, prompt, k):
        phase = len(world.prompts) + 1
        world.prompts.append(prompt)
        world.resumed.append(cmd)
        world.deadlines.append(self.deadline)
        world.staging.append(self.out)
        sb = self.ns.sb
        sb.append({"type": "turn_context", "payload": {"model": M.X.MODEL}}, message(prompt),
                  {"type": "event_msg", "payload": {"type": "token_count", "info": {
                      "last_token_usage": {"input_tokens": 100 * phase, "output_tokens": 10},
                      "model_context_window": M.X.EXPECTED_WINDOW}}},
                  {"type": "response_item", "payload": {"type": "function_call",
                      "name": "exec_command", "call_id": f"cmd-{phase}",
                      "arguments": json.dumps({"cmd": f"echo {T.SUBMIT_MARKER}"})}},
                  {"type": "response_item", "payload": {"type": "function_call_output",
                      "call_id": f"cmd-{phase}",
                      "output": f"{T.SUBMIT_MARKER}\nProcess exited with code 0"}})
        if phase in world.autos:
            sb.append(compacted("unexpected auto"))
        (self.out / "turn_000.jsonl").write_text(f"safe log {AUTH} {SIM}")
        end = world.ends.pop(0)
        return T.Stop(text="done", marker=True), {"thread_id": TID, "cap":
            end if end in (M.X.END_CALLS, M.X.END_TIME) else None,
            "quota": end == M.X.END_QUOTA, "overflow": end == M.X.END_CONTEXT}
    monkeypatch.setattr(M.X.TaskRun, "call", call)

    def rpc(sb, env, tid, log):
        what = world.compacts.pop(0) if world.compacts else "ok"
        world.events.append(("compact", len(world.prompts), what))
        if what not in ("fail", "quota", "quota_live"):
            sb.append(compacted(AUTH if what == "leak" else f"carried task {len(world.prompts)}",
                                encrypted=what != "plaintext"), tid=tid)
            if what == "double":
                sb.append(compacted("second"), tid=tid)
        return {"stopped": what not in ("live", "quota_live"), "seen": {},
                "error": "usage limit" if what in ("quota", "quota_live") else None}
    monkeypatch.setattr(M.X, "run_compaction", rpc)
    monkeypatch.setattr(M.X.SB, "docker_argv", lambda *a, **kw: pytest.fail("Docker called"))
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": AUTH}}))
    cfg = config(tmp_path, settings, harness=M.CX, expect=M.cx_runtime())
    cfg.auth_path = auth
    return world, cfg


def test_five_phases_carry_rollout_and_compact_after_each(monkeypatch, tmp_path, settings):
    world, cfg = install(monkeypatch, tmp_path, settings)
    rec = G.run_chain(cfg, G.StateFile.load(tmp_path))
    assert rec["state"] == "done" and rec["n_phases_run"] == 5
    assert world.sessions == [f"img/{tid}" for tid in TASKS]
    assert len(world.imports) == 4
    assert all(path == f"{M.C.HOME_C}/{REL}" for _, path, _ in world.imports)
    assert len([e for e in world.events if e[0] == "compact"]) == 5
    assert world.limits == list(range(settings.max_calls, settings.max_calls + 5))
    assert " exec resume " not in world.resumed[0]
    assert all(f"exec resume {TID}" in cmd for cmd in world.resumed[1:])
    assert all("-m gpt-6-luna" in cmd for cmd in world.resumed)
    assert [r["kind"] for r in rows(tmp_path)] == ["boundary"] * 4 + ["end"]
    for k in range(1, 6):
        out = tmp_path / "chains/u1" / f"p{k}"
        cp = json.loads((out / "compaction.json").read_text())
        assert cp["ok"] and cp["encrypted"] and cp["empty"] and cp["summary"] == ""
        assert cp["carried_context"] == f"user: carried task {k}"
        assert cp["pre_ctx_tokens"] == 100 * k
        state = tmp_path / "chains/u1/states" / f"after_task{k}"
        assert (state / REL).is_file()
        if k < 5:
            assert (state / REL).read_bytes() == world.imports[k - 1][2]
        assert result(tmp_path, k)["n_calls"] == 1
        assert not result(tmp_path, k)["extra"].get("primary_unknown_reason")
        scored = nchain.score_phase(out, cfg.chain, k)
        assert scored["end"] == "submitted" and scored["n_calls"] == 1
        assert scored["cost_usd"] == 0.0 and scored["position"] == k
        assert AUTH not in (out / "turn_000.jsonl").read_text()
        assert SIM not in (out / "turn_000.jsonl").read_text()
    assert all(not path.exists() for path in world.staging)
    assert result(tmp_path, 5)["extra"]["imported_prefix_intact"] is True


@pytest.mark.parametrize("compacts", [("fail", "fail"), ("plaintext",), ("double",)])
def test_failed_compaction_halts(monkeypatch, tmp_path, settings, compacts):
    world, cfg = install(monkeypatch, tmp_path, settings, compacts=compacts)
    rec = G.run_chain(cfg, G.StateFile.load(tmp_path))
    assert rec["state"] == G.COMPACTION_FAILED.format(k=1)
    assert len(world.sessions) == 1 and not world.imports
    assert len(rows(tmp_path)) == 1 and not rows(tmp_path)[0]["ok"]


def test_boundary_retry_only_after_confirmed_zero(monkeypatch, tmp_path, settings):
    _world, cfg = install(monkeypatch, tmp_path, settings, compacts=("fail", "ok"))
    cfg.max_phases = 1
    assert G.run_chain(cfg, G.StateFile.load(tmp_path))["state"] == "done"
    assert rows(tmp_path)[0]["attempt"] == 2


def test_live_boundary_withholds_handoff(monkeypatch, tmp_path, settings):
    world, cfg = install(monkeypatch, tmp_path, settings, compacts=("live",))
    rec = G.run_chain(cfg, G.StateFile.load(tmp_path))
    assert rec["state"] == "halted p1: failed"
    assert not world.imports


def test_carried_plaintext_screen_and_contaminated_export(monkeypatch, tmp_path, settings):
    _world, cfg = install(monkeypatch, tmp_path, settings, compacts=("leak",))
    rec = G.run_chain(cfg, G.StateFile.load(tmp_path))
    assert rec["state"] == G.EXPORT_FAILED.format(k=1)
    cp = json.loads((tmp_path / "chains/u1/p1/compaction.json").read_text())
    assert cp["carried_context"] == M.AC.OMITTED
    assert cp["encrypted"] and cp["ok"]
    assert AUTH not in (tmp_path / "chains/u1/p1/result.json").read_text()


def test_uncollectable_credentials_fail_closed(monkeypatch, tmp_path, settings):
    world, cfg = install(monkeypatch, tmp_path, settings)
    world.secret_failure = True
    rec = G.run_chain(cfg, G.StateFile.load(tmp_path))
    assert rec["state"] == "halted p1: failed"
    out = tmp_path / "chains/u1/p1"
    assert not (out / "rollout.jsonl").exists() and not (out / "turn_000.jsonl").exists()
    assert AUTH not in (out / "result.json").read_text()
    assert all(not path.exists() for path in world.staging)


def test_auto_compaction_only_reports_unrequested_entries(monkeypatch, tmp_path, settings):
    _world, cfg = install(monkeypatch, tmp_path, settings, autos=(2,))
    cfg.max_phases = 3
    assert G.run_chain(cfg, G.StateFile.load(tmp_path))["state"] == "done"
    assert [len(result(tmp_path, k)["extra"]["auto_compactions"]) for k in (1, 2, 3)] == [0, 1, 0]
    assert [r["kind"] for r in rows(tmp_path)] == ["boundary", "auto", "boundary", "end"]


@pytest.mark.parametrize("end", [M.X.END_CALLS, M.X.END_TIME, M.X.END_CONTEXT])
def test_capped_phase_continues_chain(monkeypatch, tmp_path, settings, end):
    world, cfg = install(monkeypatch, tmp_path, settings, ends=[end, M.X.END_SUBMITTED])
    cfg.max_phases = 2
    assert G.run_chain(cfg, G.StateFile.load(tmp_path))["state"] == "done"
    assert result(tmp_path, 1)["end"] == end and len(world.sessions) == 2


def test_quota_stops_without_a_boundary(monkeypatch, tmp_path, settings):
    world, cfg = install(monkeypatch, tmp_path, settings, ends=[M.X.END_QUOTA])
    assert G.run_chain(cfg, G.StateFile.load(tmp_path))["state"] == G.STATE_QUOTA
    assert not [e for e in world.events if e[0] == "compact"]
    assert cfg.quota_stop.is_set()


def test_configured_caps_reach_task_and_watcher(monkeypatch, tmp_path, settings):
    world, cfg = install(monkeypatch, tmp_path, replace(settings, max_calls=7, max_seconds=23))
    cfg.max_phases = 1
    G.run_chain(cfg, G.StateFile.load(tmp_path))
    assert world.limits == [7]
    assert 15 < world.deadlines[0] - time.monotonic() <= 23


def test_shared_quota_stop_prevents_next_turn(monkeypatch, tmp_path):
    stop = threading.Event()
    run = M.TaskRun(SimpleNamespace(sb=None), tmp_path, time.monotonic() + 60,
                    quota_stop=stop)
    stop.set()
    monkeypatch.setattr(M.X.TaskRun, "call", lambda *a: pytest.fail("CLI called"))
    assert run.drive("prompt", resume=TID) == M.X.END_QUOTA


def test_boundary_quota_stops_the_chain(monkeypatch, tmp_path, settings):
    _world, cfg = install(monkeypatch, tmp_path, settings, compacts=("quota", "quota"))
    rec = G.run_chain(cfg, G.StateFile.load(tmp_path))
    assert rec["state"] == G.STATE_QUOTA and cfg.quota_stop.is_set()
    assert result(tmp_path, 1)["end"] == M.X.END_SUBMITTED
    assert result(tmp_path, 1)["extra"]["chain_quota_cut"]


@pytest.mark.parametrize("key,value", [("model", "other"), ("cli_version", "0.1"),
                                      ("compaction_retries", 2), ("extra", 1)])
def test_check_rejects_mismatch_before_open(monkeypatch, tmp_path, settings, key, value):
    world, cfg = install(monkeypatch, tmp_path, settings)
    cfg.expect[key] = value
    with pytest.raises(ValueError, match="codex adapter differs"):
        G.run_chain(cfg, G.StateFile.load(tmp_path))
    assert not world.sessions


def test_runtime_and_wrapper_match_two_task_driver():
    assert M.cx_runtime() == {"model": "gpt-6-luna", "cli_version": "0.162.1",
                              "compaction_retries": 1}
    assert M.X.cli_config()["model_reasoning_effort"] == "high"
    assert M.CX.wrap("task text") == f"Please solve this issue: task text\n\n{G.X.SUBMIT_LINE}"
    assert M.CX.cleanup_retry is G.retry_cleanup


@pytest.mark.parametrize("stage", ["run_task", "export", "finish"])
@pytest.mark.parametrize("damage", ["prefix", "thread", "resume"])
def test_broken_session_carry_halts_as_failed(monkeypatch, tmp_path, settings, stage, damage):
    """R1 #1/#8: reject broken carry at each gate, including after a successful export."""
    world, cfg = install(monkeypatch, tmp_path, settings)
    original = getattr(M.CX, stage)
    captured = []

    def corrupt(ns, pr):
        if pr.k != 2:
            return
        run = pr.h["run"]
        path = f"{M.C.HOME_C}/{REL}"
        if damage == "prefix":
            ns.sb.files[path] = ns.sb.files[path][len(pr.h["imported"]):]
        elif damage == "thread":
            new_tid = "11a1832a-baee-7071-a31d-a981e579b85b"
            ns.sb.files[path.replace(TID, new_tid)] = ns.sb.files.pop(path)
            run.thread = new_tid
        else:
            run.turns[0]["info"]["thread_mismatch"] = True

    def changed(ns, pr, *args):
        if stage == "run_task":
            end = original(ns, pr, *args)
            corrupt(ns, pr)
            return end
        corrupt(ns, pr)
        return original(ns, pr, *args)

    def finish(ns, pr, *args):
        (changed if stage == "finish" else M.cx_finish)(ns, pr, *args)
        captured.append(pr)

    cfg.harness = replace(M.CX, **{stage: changed, "finish": finish})
    rec = G.run_chain(cfg, G.StateFile.load(tmp_path))
    assert rec["state"] == "halted p2: failed" and len(world.sessions) == 2
    assert captured[-1].session is None
    assert not (tmp_path / "chains/u1/states/after_task2").exists()
    assert result(tmp_path, 2)["extra"]["primary_unknown_reason"] == "b_not_from_carried_session"
    assert nchain.score_phase(tmp_path / "chains/u1/p2", cfg.chain, 2)["end"] == "failed"
    if stage == "run_task":
        assert not captured[-1].handoff and not rows(tmp_path)[-1]["ok"]


@pytest.mark.parametrize("missing", ["evidence", "rollout", "thread"])
def test_missing_final_evidence_halts_and_discards_export(monkeypatch, tmp_path, settings, missing):
    """R1 #1: evidence lost after export must never leave a scoreable submitted phase."""
    world, cfg = install(monkeypatch, tmp_path, settings)
    captured = []
    if missing == "evidence":
        monkeypatch.setattr(M.X, "screened_evidence", lambda *a: {
            "ok": False, "why": "withheld", "attempts": 1, "home": None})

    def finish(ns, pr, *args):
        assert pr.session is not None
        if missing == "rollout":
            ns.sb.files.clear()
        elif missing == "thread":
            pr.h["run"].thread = None
        M.cx_finish(ns, pr, *args)
        captured.append(pr)

    cfg.harness = replace(M.CX, finish=finish)
    assert G.run_chain(cfg, G.StateFile.load(tmp_path))["state"] == "halted p1: failed"
    assert len(world.sessions) == 1 and captured[0].session is None
    why = "evidence_withheld" if missing == "evidence" else "rollout_unreadable"
    assert result(tmp_path, 1)["extra"]["primary_unknown_reason"] == why
    assert nchain.score_phase(tmp_path / "chains/u1/p1", cfg.chain, 1)["end"] == "failed"


def test_successful_retry_after_boundary_quota_continues(monkeypatch, tmp_path, settings):
    """R1 #2/#8: only a final failed boundary propagates quota to sibling workers."""
    world, cfg = install(monkeypatch, tmp_path, settings, compacts=("quota", "ok"))
    cfg.max_phases = 2
    assert G.run_chain(cfg, G.StateFile.load(tmp_path))["state"] == "done"
    assert not cfg.quota_stop.is_set() and len(world.sessions) == 2
    assert rows(tmp_path)[0]["attempt"] == 2 and rows(tmp_path)[0]["ok"]
    assert not result(tmp_path, 1)["extra"].get("chain_quota_cut")


@pytest.mark.parametrize("live_server", [True, False])
def test_nonquiescent_boundary_preserves_quota(monkeypatch, tmp_path, settings, live_server):
    """R1 #2: both early-return paths still propagate a quota refusal."""
    world, cfg = install(monkeypatch, tmp_path, settings,
                         compacts=("quota_live",) if live_server else ("quota", "quota"))
    if not live_server:
        quiet = iter([True, False])
        monkeypatch.setattr(M.AC, "quiesce", lambda *a: {"confirmed": next(quiet)})
    assert G.run_chain(cfg, G.StateFile.load(tmp_path))["state"] == G.STATE_QUOTA
    assert cfg.quota_stop.is_set() and len(world.sessions) == 1
    cp = json.loads((tmp_path / "chains/u1/p1/compaction.json").read_text())
    assert cp["quota"] and not cp["ok"]


@pytest.mark.parametrize("ciphertext,expected", [("ciphertext", True), ("", False),
                                                (None, False), (123, False)])
def test_auto_compactions_validate_ciphertext_and_context(ciphertext, expected):
    """R1 #3: auto rows share boundary encryption and context semantics."""
    cp = compacted()
    cp["payload"]["replacement_history"][-1]["encrypted_content"] = ciphertext
    token = {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": 321, "output_tokens": 8}}}}
    history = [compacted(), token, cp, compacted(), compacted()]
    for task_end in (None, 3):
        pr = SimpleNamespace(h={"base_lines": 1, "task_end": task_end, "boundary_steps": {3, 4}})
        assert M.cx_autos(history, pr) == [{"step_index": 2, "pre_ctx_tokens": 321,
            "summary_chars": 0, "empty": True, "encrypted": expected}]
    pr = SimpleNamespace(h={"base_lines": 1, "task_end": 3})
    assert len(M.cx_autos(history, pr)) == 1  # even if boundary indices were never collected


def test_failed_secret_refresh_rescreens_existing_compaction(monkeypatch, tmp_path, settings):
    """R1 #5: credentials can become unreadable after compaction/export screening."""
    world, cfg = install(monkeypatch, tmp_path, settings)
    unknown_token = "new-credential-unknown-before-refresh"

    def finish(ns, pr, *args):
        assert pr.h["secrets_ok"] and pr.session is not None
        pr.compaction["carried_context"] = unknown_token
        world.secret_failure = True
        M.cx_finish(ns, pr, *args)
        assert pr.session is None

    cfg.harness = replace(M.CX, finish=finish)
    assert G.run_chain(cfg, G.StateFile.load(tmp_path))["state"] == "halted p1: failed"
    out = tmp_path / "chains/u1/p1"
    cp = json.loads((out / "compaction.json").read_text())
    assert cp["carried_context"] == M.AC.OMITTED
    assert result(tmp_path, 1)["extra"]["primary_unknown_reason"] == "secrets_uncollected"
    assert not (out / "rollout.jsonl").exists() and not (out / "turn_000.jsonl").exists()
    assert all(unknown_token.encode() not in p.read_bytes() for p in out.rglob("*") if p.is_file())


@pytest.mark.parametrize("prior", [None, "transfer_failed"])
def test_quota_before_first_carried_turn_keeps_id_and_reason(monkeypatch, tmp_path, settings, prior):
    """R1 #6: a quota stop before turn one is not evidence of a failed session resume."""
    world, cfg = install(monkeypatch, tmp_path, settings)

    def prepare(ns, pr):
        M.cx_prepare(ns, pr)
        if pr.k == 2:
            if prior:
                pr.extra["primary_unknown_reason"] = prior
            cfg.quota_stop.set()

    cfg.harness = replace(M.CX, prepare=prepare)
    assert G.run_chain(cfg, G.StateFile.load(tmp_path))["state"] == G.STATE_QUOTA
    assert len(world.prompts) == 1
    res = result(tmp_path, 2)
    assert res["end"] == M.X.END_QUOTA
    assert res["extra"]["thread_id"] == res["extra"]["conversation_id"] == TID
    assert res["extra"]["primary_unknown_reason"] == (prior or "quota_refused")


def test_start_context_excludes_post_task_token_events(monkeypatch, tmp_path, settings):
    """R1 #7: a boundary-only usage event cannot stand in for the task's first call."""
    _world, cfg = install(monkeypatch, tmp_path, settings)
    cfg.max_phases = 1

    def run_task(ns, pr):
        end = M.cx_run_task(ns, pr)
        path = f"{M.C.HOME_C}/{REL}"
        lines = M.X.parse_rollout(ns.sb.files[path])
        pr.h["late_token"] = lines.pop(2)
        ns.sb.files[path] = b"".join(json.dumps(x).encode() + b"\n" for x in lines)
        return end

    def compact(ns, pr, kind):
        ns.sb.append(pr.h["late_token"])
        return M.cx_compact(ns, pr, kind)

    cfg.harness = replace(M.CX, run_task=run_task, compact=compact)
    assert G.run_chain(cfg, G.StateFile.load(tmp_path))["state"] == "done"
    res = result(tmp_path, 1)
    assert res["extra"]["ctx_tokens_at_start"] is None and res["n_calls"] == 0
    assert rows(tmp_path)[0]["pre_ctx_tokens"] == 100


def test_runner_selects_codex_and_sets_agy_sonnet_model(monkeypatch):
    """R1 #8: adapter selection and the agy model setter agree with runtime checks."""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts/week2_agy_chain5.py"
    spec = importlib.util.spec_from_file_location("chain5_cx_test", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(M.AC, "MODEL", M.AC.MODEL)
    monkeypatch.setattr(M.AC, "AGY", M.AC.AGY)
    hs = W.load_config("configs/week2/agy_chain5.toml")["native_chain"]
    assert runner.adapter("codex", hs["codex"]) is M.CX
    assert runner.adapter("agy_sonnet", hs["agy_sonnet"]) is G.AGY
    assert M.AC.MODEL == M.AC.AGY.model == hs["agy_sonnet"]["model"]
    G.agy_check(SimpleNamespace(expect=G.agy_runtime()))
