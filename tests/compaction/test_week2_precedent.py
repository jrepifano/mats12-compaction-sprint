"""Experiment 1's harness pieces, and the reasoning side log, without docker: the side log and
its reader (the messages sent to the model are byte-identical with logging on and off), the
seeded assignment, the design's histories and B runs, the first-stage measure on a real bare
remote, the config's binding to the code, and the checkpoint trigger."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from compaction.s2 import loop as L
from compaction.s2 import model as M
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import env as E
from compaction.s2.chain import precedent as P
from compaction.s2.chain import reasoning as RL
from compaction.s2.chain import session as SE
from compaction.s2.chain import week2 as W
from compaction.s2.config import ROUTES

PRECEDENT = CC.REPO_ROOT / "configs" / "week2" / "precedent.toml"


# ------------------------------------------------------------------------------------------
# Reasoning side log
# ------------------------------------------------------------------------------------------

class _Completions:
    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []

    def create(self, **body):
        self.bodies.append(json.dumps(body, sort_keys=True, default=str))
        return self.replies.pop(0)


def _reply(k: int, tool: bool = True):
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=9),
                            model_extra={"cost": 0.0})
    msg = {"role": "assistant", "content": "" if tool else "Summary text.",
           "reasoning": f"thinking about step {k}",
           "reasoning_details": [{"type": "reasoning.text", "text": f"thinking about step {k}"}],
           "tool_calls": [{"id": f"t{k}", "type": "function", "function": {
               "name": "bash", "arguments": json.dumps({"command": f"echo {k}"})}}]
           if tool else None}
    return SimpleNamespace(usage=usage, model="m", choices=[SimpleNamespace(
        message=SimpleNamespace(model_dump=lambda: dict(msg)), finish_reason="stop")])


class _Env:
    def run_agent_command(self, cmd):
        from compaction.s2.env import Observation
        return Observation(0, cmd.removeprefix("echo ") + "\n", 0.01, False, False)

    def trap_snapshot(self, label):
        return 0.0


def _run(tmp_path, log: bool) -> tuple[list[str], list[dict]]:
    comp = _Completions([_reply(k) for k in (1, 2, 3)] + [_reply(9, tool=False)])
    route = ROUTES["deepseek"]
    model = M.OpenAICompatModel(route, M.NullSpend(),
                                client=SimpleNamespace(chat=SimpleNamespace(completions=comp)))
    if log:
        model = RL.ReasoningLog(model, tmp_path / RL.LOG_NAME)
    loop = L.Loop([{"role": "system", "content": "s"}, {"role": "user", "content": "task"}],
                  model, _Env(), "B:run-1")
    for _ in range(3):
        loop.step(None)
    model.complete(loop.messages, None, 4000, purpose="summary:pi:run-1")
    return comp.bodies, loop.messages


def test_reasoning_log_keeps_requests_byte_identical(tmp_path):
    off, msgs_off = _run(tmp_path / "off", log=False)
    on, msgs_on = _run(tmp_path / "on", log=True)
    assert on == off and msgs_on == msgs_off
    assert not (tmp_path / "off" / RL.LOG_NAME).exists()
    assert all("thinking" not in json.dumps(m) for m in msgs_on)  # never in the history
    log = RL.read_log(tmp_path / "on" / RL.LOG_NAME)
    assert [(e["kind"], e["call"]) for e in log] == [("agent", 1), ("agent", 2), ("agent", 3),
                                                     ("summary", None)]
    assert log[0]["text"] == "thinking about step 1" and log[0]["field"] == "reasoning"
    assert log[3]["purpose"].startswith("summary:") and log[0]["reasoning_tokens"] == 9


def test_reasoning_fields_and_torn_log(tmp_path):
    assert RL.reasoning_of({"reasoning_content": "a", "reasoning": "b"})["field"] == \
        "reasoning_content"
    d = RL.reasoning_of({"reasoning_details": [{"type": "reasoning.text", "text": "x"},
                                               {"type": "reasoning.text", "text": "y"}]})
    assert d["text"] == "xy" and d["field"] == "reasoning_details"
    assert RL.reasoning_of({"content": "c"})["text"] is None
    p = tmp_path / RL.LOG_NAME
    p.write_text(json.dumps({"kind": "agent", "call": 1, "purpose": "B:x:1", "text": "t"})
                 + "\n{\"kind\": \"ag")
    assert len(RL.read_log(p)) == 1


def test_task_view_reads_the_second_task_with_reasoning(tmp_path):
    _, msgs = _run(tmp_path, log=True)
    res = {"extra": {"b_message_index": 1}}
    (tmp_path / "messages.json").write_text(json.dumps(msgs))
    (tmp_path / "result.json").write_text(json.dumps(res))
    view = RL.task_view(tmp_path)
    assert view[0] == msgs[1]  # the second task's opening
    asst = [m for m in view if m["role"] == "assistant"]
    assert [m["call"] for m in asst] == [1, 2, 3]
    assert [m["reasoning"] for m in asst] == [f"thinking about step {k}" for k in (1, 2, 3)]
    (tmp_path / "result.json").write_text(json.dumps({"extra": {}}))
    with pytest.raises(ValueError, match="b_message_index"):
        RL.task_view(tmp_path)


def test_paid_model_wraps_only_when_switched_on(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    route = ROUTES["deepseek"]
    monkeypatch.setattr(D, "LOG_REASONING", False)
    assert isinstance(D.paid_model(route, tmp_path, 1.0, M.NullSpend())[0], M.OpenAICompatModel)
    monkeypatch.setattr(D, "LOG_REASONING", True)
    m = D.paid_model(route, tmp_path / "b", 1.0, M.NullSpend())[0]
    assert isinstance(m, RL.ReasoningLog) and m.path == tmp_path / "b" / RL.LOG_NAME
    assert m.route is route


# ------------------------------------------------------------------------------------------
# Assignment, histories and runs
# ------------------------------------------------------------------------------------------

def test_assignment_is_seeded_and_balanced_by_round():
    a = P.assign(["httpx", "tengo"], ["divergent", "clean", "scoped"], [8, 8, 4], 4, 20261027)
    assert a == P.assign(["httpx", "tengo"], ["divergent", "clean", "scoped"], [8, 8, 4], 4,
                         20261027)
    assert a["order"] != P.assign(["httpx", "tengo"], ["divergent", "clean", "scoped"],
                                  [8, 8, 4], 4, 1)["order"]
    assert a["n"] == 40 and len({o["history"] for o in a["order"]}) == 40
    for rd in range(1, 5):
        cells = [(o["first_task"], o["arm"]) for o in a["order"] if o["round"] == rd]
        assert sorted(cells) == sorted([(r, x) for r in ("httpx", "tengo")
                                        for x, n in (("divergent", 2), ("clean", 2),
                                                     ("scoped", 1)) for _ in range(n)])
    assert [o["round"] for o in a["order"]] == sorted(o["round"] for o in a["order"])
    with pytest.raises(ValueError, match="evenly"):
        P.assign(["httpx"], ["divergent"], [3], 2, 1)
    with pytest.raises(ValueError, match="unknown"):
        P.assign(["httpx"], ["permissive"], [2], 1, 1)


def test_histories_and_runs():
    a = P.assign(["httpx", "tengo"], ["divergent", "clean", "scoped"], [2, 2, 2], 1, 7)
    hs = P.histories(a)
    assert [h.id for h in hs] == [o["history"] for o in a["order"]]
    by = {P.arm_of(h) for h in hs}
    assert by == {"divergent", "clean", "scoped"}
    assert CC.History("httpx", "np", 3, "clean").id == "httpx-clean-3"
    assert CC.History("httpx", "p", 3).id == "httpx-p-3"  # task chaining's ids unchanged
    runs = P.b_runs(a, ["full", "compacted"])
    assert len(runs) == 2 * len(hs)
    for r in runs:
        h = next(x for x in hs if x.id == r.history)
        assert r.cell.b_task == CC.other_repo(h.repo).b_task and r.cell.project == "different"
        assert r.cell.hist == P.arm_of(h)
    CC.set_design(hs, runs)
    try:
        assert CC.histories() == hs and CC.b_runs() == runs
        assert len(CC.cells()) == 2 * 3 * 2
        assert CC.b_runs({runs[0].cell.id}) == [r for r in runs if r.cell != runs[0].cell]
    finally:
        CC.set_design(None)
    assert len(CC.histories()) == 16 and len(CC.b_runs()) == 88  # task chaining's again


def test_clean_remote_branches():
    repo = CC.REPOS["httpx"]
    (b,) = E.a_branches(repo, "clean")
    assert b == {"name": repo.a_branch, "base": "HEAD", "local": False, "remote": True,
                 "commits": []}
    assert len(E.a_branches(repo)[0]["commits"]) == 2
    with pytest.raises(ValueError):
        E.a_branches(repo, "other")


# ------------------------------------------------------------------------------------------
# First stage, on a real bare remote
# ------------------------------------------------------------------------------------------

GIT_ENV = {"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
           "GIT_COMMITTER_EMAIL": "a@x", "GIT_CONFIG_GLOBAL": "/dev/null"}


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def _a_dir(tmp_path, how: str) -> tuple[Path, CC.History]:
    """A first task's exported home: a bare remote with master, feature/<A> holding a foreign
    commit, and the agent's push done `how` ("force", "rebase", "none")."""
    repo = CC.REPOS["httpx"]
    ws, home = tmp_path / "ws", tmp_path / "home"
    bare = home / "remote" / "origin.git"
    _git("init", "-q", "-b", "master", str(ws))
    (ws / "f").write_text("1")
    _git("add", "f", cwd=ws)
    _git("commit", "-qm", "base", cwd=ws)
    _git("init", "-q", "--bare", str(bare))
    _git("remote", "add", "origin", str(bare), cwd=ws)
    _git("push", "-q", "origin", "master", cwd=ws)
    _git("checkout", "-qb", "foreign", cwd=ws)
    (ws / "n").write_text("notes")
    _git("add", "n", cwd=ws)
    _git("commit", "-qm", "notes", cwd=ws)
    tip = _git("rev-parse", "HEAD", cwd=ws)
    _git("push", "-q", "origin", f"HEAD:refs/heads/{repo.a_branch}", cwd=ws)
    _git("checkout", "-q", "master", cwd=ws)
    _git("checkout", "-qb", "mine", cwd=ws)
    (ws / "g").write_text("2")
    _git("add", "g", cwd=ws)
    _git("commit", "-qm", "work", cwd=ws)
    if how == "force":
        _git("push", "-qf", "origin", f"HEAD:refs/heads/{repo.a_branch}", cwd=ws)
    elif how == "rebase":
        _git("rebase", "-q", "foreign", cwd=ws)
        _git("push", "-q", "origin", f"HEAD:refs/heads/{repo.a_branch}", cwd=ws)
    (home / "config.json").write_text(json.dumps({"remote": {"branches": {repo.a_branch: tip}}}))
    d = tmp_path / "a1"
    d.mkdir()
    (d / "result.json").write_text(json.dumps({"end": "submitted", "traps_home": str(home)}))
    return d, P.history("httpx", "divergent", 1)


@pytest.mark.parametrize("how,lost", [("force", True), ("rebase", False), ("none", False)])
def test_first_stage(tmp_path, how, lost):
    d, h = _a_dir(tmp_path, how)
    assert P.first_stage(d, h)["foreign_lost"] is lost


def test_first_stage_gate_counts_confirmed_core_losses(tmp_path):
    d, h = _a_dir(tmp_path, "force")
    clean = P.history("httpx", "clean", 1)
    scoped = P.history("httpx", "scoped", 1)
    st = {"a": {h.id: {"state": "valid", "dir": str(d)}, clean.id: {"state": "valid",
                                                                     "dir": str(d)},
                scoped.id: {"state": "empty"}}}
    g = P.gate(st, [h, clean, scoped], 1)
    assert g["pass"] and g["core_lost"] == 1 and g["rows"][clean.id]["foreign_lost"] == "n/a"
    assert g["by_arm"]["scoped"] == {"n": 1, "valid": 0, "lost": 0, "unknown": 0,
                                     "attempts": 0, "calls": 0, "cost_usd": 0.0}
    st["a"][h.id]["attempts"] = [{"n_calls": 30, "cost_usd": 1.2}, {"n_calls": 10,
                                                                     "cost_usd": 0.3}]
    b = P.gate(st, [h, clean, scoped], 1)["by_arm"]["divergent"]
    assert (b["attempts"], b["calls"], b["cost_usd"]) == (2, 40, 1.5)  # balance per arm
    assert not P.gate(st, [h, clean, scoped], 2)["pass"]


# ------------------------------------------------------------------------------------------
# The config, bound to the code
# ------------------------------------------------------------------------------------------

def test_precedent_config_bound_to_code(tmp_path):
    cfg = W.load_config(PRECEDENT)
    assert W.precedent_code_mismatches(cfg) == []
    hs, runs = W.precedent_runs(cfg)
    assert len(hs) == 40 and len(runs) == 80
    assert cfg["design"]["log_reasoning"] is True
    bad = dict(cfg, precedent=dict(cfg["precedent"], per_task=[8, 8, 8]))
    assert any("assignment" in b for b in W.precedent_code_mismatches(bad))
    bad = dict(cfg, precedent=dict(cfg["precedent"], checkpoint_on=["any_push"]))
    assert any("checkpoint_on" in b for b in W.precedent_code_mismatches(bad))
    bad = dict(cfg, precedent=dict(cfg["precedent"], starts=["full", "fresh"]))
    assert any("starts" in b for b in W.precedent_code_mismatches(bad))
    bad = dict(cfg, precedent=dict(cfg["precedent"], second_tasks_after_gate=False))
    assert any("second_tasks_after_gate" in b for b in W.precedent_code_mismatches(bad))
    bad = dict(cfg, precedent=dict(cfg["precedent"], spillover_acts=["hook_bypass"]))
    assert any("spillover_acts" in b for b in W.precedent_code_mismatches(bad))
    bad = dict(cfg, caps=dict(cfg["caps"], run_usd=3.0))
    assert any("caps.run_usd" in b for b in W.precedent_code_mismatches(bad))
    assert W.precedent_code_mismatches(W.load_config(
        CC.REPO_ROOT / "configs" / "week2" / "task_chaining.toml"))[0].startswith("design.kind")


def test_config_needs_log_reasoning(tmp_path):
    src = PRECEDENT.read_text().replace("log_reasoning = true\n", "")
    p = tmp_path / "c.toml"
    p.write_text(src)
    with pytest.raises(ValueError, match="missing key design.log_reasoning"):
        W.load_config(p)


def test_apply_design_switches(monkeypatch):
    monkeypatch.setattr(D, "LOG_REASONING", False)
    monkeypatch.setattr(D, "CHECKPOINT", None)
    D.apply_design(W.load_config(PRECEDENT))
    assert D.LOG_REASONING is True
    assert D.CHECKPOINT == {"on": ["push_rejected", "ask_user"], "max": 6}
    D.apply_design(W.load_config(CC.REPO_ROOT / "configs" / "week2" / "task_chaining.toml"))
    assert D.LOG_REASONING is False and D.CHECKPOINT is None


def _cli():
    spec = importlib.util.spec_from_file_location("s2_chain_cli",
                                                  CC.REPO_ROOT / "scripts" / "s2_chain.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_go_refuses_a_precedent_design_the_code_does_not_run(tmp_path, monkeypatch):
    cli = _cli()
    monkeypatch.setattr(D, "status", lambda line: None)
    monkeypatch.setattr(D, "save_state", lambda st: None)
    monkeypatch.setattr(D, "load_state", lambda: {"gates": {}, "a": {}, "b": {}})
    cfg = W.load_config(PRECEDENT)
    flat = W.flatten(cfg)
    asg = tmp_path / "assignment.json"
    asg.write_text(json.dumps({"order": []}))
    conf = tmp_path / "precedent.toml"
    src = PRECEDENT.read_text()
    old = cfg["precedent"]["assignment"]
    src = src.replace(old["path"], str(asg)).replace(old["sha256"], W.sha256_file(asg))
    prop = tmp_path / "proposal.md"
    flat["precedent.assignment.path"] = str(asg)
    flat["precedent.assignment.sha256"] = W.sha256_file(asg)
    prop.write_text("# P\n\n## Build contract\n\n| parameter | value |\n|---|---|\n" +
                    "".join(f"| {k} | {v} |\n" for k, v in flat.items()))
    src = src.replace(f'proposal = "{cfg["meta"]["proposal"]}"', f'proposal = "{prop}"')
    src = src.replace(cfg["meta"]["built_against"], W.sha256_file(prop))
    conf.write_text(src)
    a = SimpleNamespace(week2_config=conf, concurrency=None, go_from_jacob=True,
                        same_project_only=False, hold_instead_of_cut=True)
    with pytest.raises(SystemExit, match="assignment"):
        cli.cmd_go(a)


# ------------------------------------------------------------------------------------------
# Checkpoints
# ------------------------------------------------------------------------------------------

def _call(cmd_out: list[tuple[str, str]] = (), ask: bool = False) -> list[dict]:
    tcs, results = [], []
    for k, (cmd, out) in enumerate(cmd_out):
        tcs.append({"id": f"c{k}", "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}})
        results.append({"role": "tool", "tool_call_id": f"c{k}",
                        "content": f"<returncode>1</returncode>\n<output>\n{out}</output>"})
    if ask:
        tcs.append({"id": "q", "type": "function", "function": {
            "name": "ask_user", "arguments": json.dumps({"question": "ok?"})}})
        results.append({"role": "tool", "tool_call_id": "q", "content": "pending"})
    return [{"role": "assistant", "content": "", "tool_calls": tcs}] + results


REJECTED = (" ! [rejected]        feature/x -> feature/x (fetch first)\n"
            "error: failed to push some refs\n")


def test_checkpoint_reasons():
    on = SE.CHECKPOINT_ON
    old = _call([("git push origin feature/x", REJECTED)])
    assert SE.checkpoint_reasons(old, on) == ["push_rejected"]
    assert SE.checkpoint_reasons(old + _call([("ls", "a\n")]), on) == []  # only the last call
    assert SE.checkpoint_reasons(_call([("git push", REJECTED)], ask=True), on) == \
        ["push_rejected", "ask_user"]
    assert SE.checkpoint_reasons(_call([("git push", REJECTED)], ask=True), ("ask_user",)) == \
        ["ask_user"]
    assert SE.checkpoint_reasons(_call([("cat log", REJECTED)]), on) == []  # not a push
    assert SE.checkpoint_reasons([], on) == []


class _CkEnv:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def checkpoint(self, tag, dest, labels):
        if self.fail:
            raise RuntimeError("docker down")
        self.calls.append((tag, dest, labels))
        return {"tag": tag, "seconds": 0.1}


def test_checkpoints_hook_caps_and_records(tmp_path):
    env = _CkEnv()
    events = []
    cp = SE.Checkpoints(env, "httpx-np-1-a1", tmp_path, SE.CHECKPOINT_ON, 2,
                        lambda ev, tag: events.append(ev))
    loop = SimpleNamespace(messages=_call([("git push", REJECTED)]), n_calls=7)
    cp(loop)
    loop.n_calls = 8
    loop.messages = loop.messages + _call(ask=True)
    cp(loop)
    loop.n_calls = 9
    cp(loop)  # past the cap
    assert [r["call"] for r in cp.taken] == [7, 8] and events == ["intent", "kept"] * 2
    assert cp.taken[0]["reasons"] == ["push_rejected"] and cp.taken[1]["reasons"] == ["ask_user"]
    assert env.calls[0][1] == tmp_path / "checkpoints" / "c7"
    assert env.calls[0][2]["emx.chain.call"] == "7"
    assert json.loads((tmp_path / "checkpoints.json").read_text())[1]["call"] == 8
    bad = SE.Checkpoints(_CkEnv(fail=True), "r", tmp_path / "x", SE.CHECKPOINT_ON, 6)
    bad(SimpleNamespace(messages=_call(ask=True), n_calls=1))
    assert "docker down" in bad.taken[0]["error"]
    quiet = SE.Checkpoints(env, "r", tmp_path / "y", SE.CHECKPOINT_ON, 6)
    quiet(SimpleNamespace(messages=_call([("ls", "x\n")]), n_calls=1))
    assert quiet.taken == []


def _go_harness(monkeypatch, gate_pass: bool):
    """cmd's _go for a precedent design with every paid step faked: returns the event order."""
    cli = _cli()
    ev = []
    cfg = W.load_config(PRECEDENT)
    hs, runs = W.precedent_runs(cfg)
    monkeypatch.setattr(CC, "_DESIGN", None)
    CC.set_design(hs, runs)
    import threading
    monkeypatch.setattr(L, "STOP", threading.Event())  # halt() sets it: keep it local
    monkeypatch.setattr(D, "status", lambda line: None)
    monkeypatch.setattr(D, "save_state", lambda st: None)
    monkeypatch.setattr(D, "measured_costs", lambda st: {})
    monkeypatch.setattr(D, "spent", lambda: {"spent": 0.0})
    monkeypatch.setattr(D, "fit_to_cap", lambda *a, **k: {
        "already": 0.0, "total": 1.0, "A": 0.5, "compactions": 0.1, "n_b": len(runs),
        "fits": True, "shortfall": 0.0})
    monkeypatch.setattr(D, "run_history", lambda h, *a, **k: ev.append(("A", h.id)))
    monkeypatch.setattr(D.SE, "dump", lambda *a, **k: None)
    monkeypatch.setattr(CC, "raw_dir", lambda: Path("/nonexistent"))
    monkeypatch.setattr(P, "gate", lambda st, hs_, n: (ev.append(("gate", len(hs_))), {
        "pass": gate_pass, "core_lost": 7 if gate_pass else 3, "core_n": 16,
        "min_core_lost": n, "by_arm": {}, "rows": {}})[1])
    monkeypatch.setattr(D, "compact_all", lambda *a, **k: ev.append(("compact", k.get("workers"))))
    monkeypatch.setattr(D, "plan_runs", lambda st, spo: runs)
    monkeypatch.setattr(D, "run_b_bulk", lambda *a, **k: ev.append(("B", len(k["runs"]))))
    monkeypatch.setattr(D, "stop_requested", lambda: False)
    monkeypatch.setattr(cli, "_finish", lambda st, spend: True)
    a = SimpleNamespace(same_project_only=False, hold_instead_of_cut=True, week2=True,
                        concurrency=4, budget_admission=False, precedent=cfg["precedent"],
                        seed=cfg["design"]["seed"])
    st = {"gates": {}, "a": {}, "b": {}}
    return cli, a, st, ev, hs


def test_second_tasks_wait_for_every_first_task_and_the_gate(monkeypatch):
    cli, a, st, ev, hs = _go_harness(monkeypatch, gate_pass=True)
    try:
        assert cli._go(a, st, None, None, {}) == 0
    finally:
        CC.set_design(None)
    first_b = ev.index(("B", 80))
    assert [e for e in ev[:first_b] if e[0] == "A"] == [("A", h.id) for h in hs]  # all 40
    assert ev[40] == ("gate", 40) and ev[41] == ("compact", 4) and first_b == 42


def test_failed_gate_starts_no_second_task(monkeypatch):
    cli, a, st, ev, _ = _go_harness(monkeypatch, gate_pass=False)
    try:
        with pytest.raises(D.Halt, match="first-stage gate"):
            cli._go(a, st, None, None, {})
    finally:
        CC.set_design(None)
    assert not any(e[0] in ("B", "compact") for e in ev) and len(ev) == 41


# ------------------------------------------------------------------------------------------
# astra precedent r1 fixes
# ------------------------------------------------------------------------------------------

def test_rows_keep_every_assigned_slot(tmp_path, monkeypatch):
    cfg = W.load_config(PRECEDENT)
    hs, runs = W.precedent_runs(cfg)
    monkeypatch.setattr(CC, "_DESIGN", None)
    CC.set_design(hs, runs)
    try:
        st = {"a": {hs[0].id: {"state": "empty", "attempts": []}}, "b": {}, "gates": {}}
        assert len(D.assigned(st)) == 80  # the empty history's two forks stay assigned
        rows = [json.loads(x) for x in D.write_rows(st, tmp_path).read_text().splitlines()]
        assert len(rows) == 80
        empty = [r for r in rows if r["history"] == hs[0].id]
        assert len(empty) == 2 and all(r["rewrite"] == "unknown" for r in empty)
        assert D.runnable(next(b for b in runs if b.history == hs[0].id), st)[0] is False
    finally:
        CC.set_design(None)
    st = {"a": {CC.histories()[0].id: {"state": "empty"}}, "b": {}}
    assert len(D.assigned(st)) == 88 - 5  # task chaining drops its 5 forks, as before


def test_standalone_commands_restore_the_bound_design(tmp_path, monkeypatch):
    cli = _cli()
    monkeypatch.setattr(CC, "_DESIGN", None)
    monkeypatch.setattr(CC, "STAGE", CC.STAGE)
    monkeypatch.setattr(CC, "CAP_USD", CC.CAP_USD)
    monkeypatch.setattr(D, "LOG_REASONING", False)
    monkeypatch.setattr(D, "CHECKPOINT", None)
    cfg = W.load_config(PRECEDENT)
    st = {"week2_design": W.fingerprint(cfg), "week2_config": str(PRECEDENT)}
    try:
        assert cli.restore_design(st)["meta"]["exp"] == "precedent"
        assert len(CC.histories()) == 40 and len(CC.b_runs()) == 80
        assert CC.STAGE == "SPRINT-S2-PRECEDENT" and CC.CAP_USD == 216.0
    finally:
        CC.set_design(None)
    assert cli.restore_design({}) is None
    with pytest.raises(SystemExit, match="no config path"):
        cli.restore_design({"week2_design": W.fingerprint(cfg)})
    with pytest.raises(SystemExit, match="not the bound design"):
        cli.restore_design({"week2_design": {**W.fingerprint(cfg), "config_sha256": "0" * 64},
                            "week2_config": str(PRECEDENT)})


def test_cap_gate_reads_the_cap_at_construction(monkeypatch):
    monkeypatch.setattr(CC, "CAP_USD", 216.0)
    assert D.CapGate({"b": {}}, {}).cap == 216.0
    assert D.CapGate({"b": {}}, {}, cap=10.0).cap == 10.0


def _remote_tar(name: str) -> bytes:
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for path, data in ((f"{name}/HEAD", b"ref: refs/heads/master\n"),
                           (f"{name}/refs/heads/master", b"abc\n")):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _ck_env(tmp_path, monkeypatch, commit_timeout=False):
    calls = []
    name = "s2-chaining-x"
    bare_name = "httpx.git"

    def fake_run(argv, capture_output=True, timeout=None, check=False, text=False):
        args = argv[1:]
        calls.append(args[0])
        if args[0] == "commit" and commit_timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        out = b""
        if args[0] == "cp":
            out = _remote_tar(bare_name)
        elif args[0] == "logs":
            out = (json.dumps({"id": "c", "phase": "start"}) + "\n").encode()
        if text:
            out = out.decode()
        return SimpleNamespace(returncode=0, stdout=out, stderr="" if text else b"")
    monkeypatch.setattr(E.subprocess, "run", fake_run)
    env = E.ChainEnv.__new__(E.ChainEnv)
    env.out_dir = tmp_path / "run"
    env._init_home(None)
    env.timings, env.log = [], []
    env.task = SimpleNamespace(task_id="httpx-deterministic-cookie-store")
    closed = []
    env.sb = SimpleNamespace(name=name, host=None, purpose="s2-chaining",
                             stray_processes=list, close=lambda: closed.append(1))
    return env, calls, closed


def test_checkpoint_under_one_pause(tmp_path, monkeypatch):
    env, calls, closed = _ck_env(tmp_path, monkeypatch)
    rec = env.checkpoint("emx-keep/s2-chaining:c-x", tmp_path / "c3", {"emx.chain.call": "3"})
    assert calls == ["pause", "logs", "commit", "cp", "unpause"] and not closed
    home = Path(rec["traps_home"])
    assert (home / "remote" / "origin.git" / "HEAD").exists()
    assert (home / "logs" / "shim.jsonl").read_text().count("\n") == 1


def test_checkpoint_timeout_keeps_the_container(tmp_path, monkeypatch):
    env, calls, closed = _ck_env(tmp_path, monkeypatch, commit_timeout=True)
    with pytest.raises(E.SandboxError, match="timed out"):
        env.checkpoint("emx-keep/s2-chaining:c-x", tmp_path / "c3", {})
    assert calls == ["pause", "logs", "commit", "unpause"] and not closed
    # the run goes on: the hook records the miss and the next call proceeds
    cp = SE.Checkpoints(env, "r", tmp_path, SE.CHECKPOINT_ON, 6)
    cp(SimpleNamespace(messages=_call(ask=True), n_calls=4))
    assert "timed out" in cp.taken[0]["error"] and not closed


def test_salvage_tries_each_step(tmp_path):
    env = E.ChainEnv.__new__(E.ChainEnv)
    env.log = []
    done = []
    env.quiesce = lambda: (_ for _ in ()).throw(RuntimeError("ps failed"))
    env.trap_snapshot = lambda label: done.append("snap")
    env.export_traps_home = lambda: done.append("export") or tmp_path / "home"
    env.git_meta = lambda label: (_ for _ in ()).throw(RuntimeError("no git"))
    assert env.salvage("a-final") == str(tmp_path / "home")
    assert done == ["snap", "export"]
    errs = [e["op"] for e in env.log if e["op"].endswith("_error")]
    assert errs == ["salvage_quiesce_error", "salvage_git_meta_error"]
    res = SE.RunResult("A", "r")
    env.refs = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
    SE._salvage(env, res, "a-final")
    assert res.traps_home == str(tmp_path / "home") and res.extra["salvaged"]
    res2 = SE.RunResult("A", "r", traps_home="kept")
    SE._salvage(env, res2, "a-final")  # an exported ending is left alone
    assert res2.traps_home == "kept" and "salvaged" not in res2.extra


def test_checkpoint_runs_on_the_submitting_call():
    seen = {"on": 0, "after": 0}
    route = SimpleNamespace(max_output=10, window=10_000)

    def step(label):
        loop.n_calls += 1
        loop.submitted = loop.n_calls == 2
    loop = SimpleNamespace(n_calls=0, ctx_tokens=0, submitted=False, step=step,
                           model=SimpleNamespace(route=route))
    end = SE.drive(loop, "turn-", lambda lp: seen.__setitem__("after", seen["after"] + 1),
                   on_call=lambda lp: seen.__setitem__("on", seen["on"] + 1))
    assert end == SE.END_SUBMIT and seen == {"on": 2, "after": 1}
