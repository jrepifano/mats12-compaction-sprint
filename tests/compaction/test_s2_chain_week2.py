"""Week-2 harness pieces that run without docker: the container-log split (hidden scorer logs),
N-task chain settings and orders from the config, the automatic compaction hook, and the launch
gate of `scripts/s2_chain.py`."""

from __future__ import annotations

import importlib.util
import json
import shutil
from types import SimpleNamespace

import pytest

from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import env as E
from compaction.s2.chain import nchain as N
from compaction.s2.chain import stubs
from compaction.s2.chain import week2 as W
from compaction.s2.config import ROUTES
from compaction.s2.model import StubModel, est_tokens

STUB5 = CC.REPO_ROOT / "configs" / "week2" / "stub_chain5.toml"


def test_split_container_log():
    shim = json.dumps({"id": "c1", "phase": "start", "argv": ["git", "push"]})
    push = json.dumps({"ref": "refs/heads/x", "old": "a", "new": "b", "kind": "create"})
    ask = json.dumps({"question": "ok?", "action": ""})
    logs, other = E.split_container_log(f"{shim}\nnot json\n\n", f"{push}\n{ask}\n[1, 2]\n")
    assert logs == {"shim": [shim], "push": [push], "ask": [ask]}
    assert other == ["out: not json", "err: [1, 2]"]
    # a push-shaped line on stdout is not a ref update
    logs, other = E.split_container_log(push + "\n", "")
    assert logs["push"] == [] and len(other) == 1


def test_hidden_layout_names():
    assert "trap" not in (E.OUT_FD + E.ERR_FD + E.LOG_SED).lower()
    assert E.repo_name("httpx-multipart-response-parsing") == "httpx"
    assert E.repo_name("obsidian-linter-auto-toc") == "obsidian"  # unknown tasks: first word
    assert E.origin_url(E.repo_name("tengo-destructuring-bindings")) == "/srv/git/tengo.git"
    assert "/var/lib/gitd/logs/shim.jsonl#/proc/1/fd/1" in E.LOG_SED


def test_stub_chain_config_and_plan():
    cfg = W.load_config(STUB5)
    assert W.plan_check(W.resolve(cfg["meta"]["proposal"]), STUB5)["ok"]
    s = N.settings_from_config(cfg)
    (c,) = N.chains_from_config(cfg)
    assert len(c.phases) == cfg["design"]["n_phases"] == 5
    assert [p.project for p in c.phases[1:]] == ["switch"] * 4
    assert [E.repo_name(p.task) for p in c.phases] == ["httpx", "tengo"] * 2 + ["httpx"]
    assert s.lead_same == CC.LEAD_SAME and s.lead_different == CC.LEAD_DIFFERENT
    assert s.task_ending == CC.TASK_ENDING
    rows = N.design_rows([c], s)
    assert rows[0]["tasks"] == [p.task for p in c.phases]


def test_settings_need_their_wording(tmp_path):
    cfg = W.load_config(STUB5)
    cfg["wording"].pop("lead_same")
    with pytest.raises(ValueError, match="wording.lead_same"):
        N.settings_from_config(cfg)


def test_task_text_and_lead_in():
    cfg = W.load_config(STUB5)
    s = N.settings_from_config(cfg)
    inst = f"Do the thing.\n\n{CC.DEEPSWE_ENDING}\n"
    assert N.task_text(inst, s.task_ending, "feature/x", "main") == \
        "Do the thing.\n\n" + CC.TASK_ENDING.format(branch="feature/x", default="main")
    with pytest.raises(ValueError):
        N.task_text("no ending", s.task_ending, "b", "m")
    assert N.lead_in(s, "httpx-multipart-response-parsing", "httpx-deterministic-cookie-store") \
        == CC.LEAD_SAME
    assert N.lead_in(s, "tengo-destructuring-bindings", "httpx-deterministic-cookie-store") == \
        CC.LEAD_DIFFERENT.format(repo="tengo", prev="httpx")


def test_push_setup():
    hx = CC.REPOS["httpx"]
    a, b = N.push_setup(hx.a_task), N.push_setup(hx.b_task)
    assert a.branch == hx.a_branch and b.branch == hx.b_branch and a.base == hx.base
    assert a.feature == hx.a_feature and b.feature == hx.b_feature
    assert N.junk_branches(a) == E.a_branches(hx)  # phase 1 = task chaining's A setup
    with pytest.raises(KeyError):
        N.push_setup("obsidian-linter-auto-toc")


def _loop(n_msgs: int, model):
    msgs = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 else "assistant", "content": f"message {i} " * 50}
        for i in range(n_msgs)]
    return SimpleNamespace(messages=msgs, model=model, n_calls=7,
                           ctx_tokens=est_tokens(msgs, model.route.bytes_per_token))


def test_auto_compaction(tmp_path):
    model = StubModel(actions=[], summary=lambda m: stubs.STUB_SUMMARY, route=ROUTES["stub"])
    loop = _loop(6, model)
    off = N.AutoCompact(0, lambda n: model, tmp_path, "r", None)
    off(loop)
    assert not off.done  # 0 = off
    below = N.AutoCompact(loop.ctx_tokens + 1, lambda n: model, tmp_path, "r", None)
    below(loop)
    assert not below.done
    auto = N.AutoCompact(1, lambda n: model, tmp_path, "r", None)
    before = list(loop.messages)
    auto(loop)
    assert auto.transcript == before + [auto.transcript[-1]]  # nothing compacted away is lost
    assert auto.transcript[-1]["content"] == "[compaction 1]"
    loop.messages.append({"role": "tool", "content": "! [rejected] (non-fast-forward)"})
    full = auto.flush(loop)
    assert full[:len(before)] == before and full[-1]["content"].startswith("! [rejected]")
    assert len(full) == len(before) + 2  # the post-compaction context is not repeated
    assert len(auto.done) == 1 and auto.done[0]["call"] == 7 and auto.done[0]["update"] is False
    assert (tmp_path / "auto_compaction_1.json").exists()
    assert auto.summary is not None and "<summary>" in loop.messages[auto.summary.index]["content"]
    # the context is still over the threshold: switched off, never loops
    assert auto.off and auto.off.startswith("post-compaction context")
    auto(loop)
    assert len(auto.done) == 1
    # a later compaction with an earlier summary in context uses pi's update prompt
    auto2 = N.AutoCompact(1, lambda n: model, tmp_path, "r2", auto.summary)
    auto2(loop)
    assert auto2.done[0]["update"] is True


def test_set_agent_memory():
    old = (CC.AGENT_MEMORY_MB, D.EXPECTED_MB["agent"])
    try:
        D.set_agent_memory(2048)
        assert CC.AGENT_MEMORY_MB == 2048 and D.EXPECTED_MB["agent"] == 2048
        adm = D.Admission(mem=lambda: None, others=None, reserve=lambda: 0)
        n = 0
        while adm.fits("agent"):
            adm.live[n] = D.EXPECTED_MB["agent"]
            n += 1
        assert n == CC.BUDGET_MB // 2048 == 10
    finally:
        CC.AGENT_MEMORY_MB, D.EXPECTED_MB["agent"] = old


def _cli():
    spec = importlib.util.spec_from_file_location("s2_chain_cli",
                                                  CC.REPO_ROOT / "scripts" / "s2_chain.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_go_refuses_without_a_design(monkeypatch):
    cli = _cli()
    with pytest.raises(SystemExit, match="--week2-config"):
        cli.week2_gate(SimpleNamespace(week2_config=None, task_chaining_v031=False))
    assert cli.week2_gate(SimpleNamespace(week2_config=None, task_chaining_v031=True)) is None


def _copy_design(tmp_path, src=STUB5):
    """A config and its proposal in tmp_path (the config names the copy, absolute)."""
    cfg = W.load_config(src)
    prop = tmp_path / "proposal.md"
    shutil.copy(W.resolve(cfg["meta"]["proposal"]), prop)
    conf = tmp_path / "design.toml"
    conf.write_text(src.read_text().replace(f'proposal = "{cfg["meta"]["proposal"]}"',
                                            f'proposal = "{prop}"'))
    return conf, prop


def test_go_refuses_on_plan_drift(tmp_path, monkeypatch):
    cli = _cli()
    monkeypatch.setattr(D, "status", lambda line: None)
    monkeypatch.setattr(D, "save_state", lambda st: None)
    conf, prop = _copy_design(tmp_path)
    st: dict = {"gates": {}}
    cfg = cli.week2_gate(SimpleNamespace(week2_config=conf), st)
    assert cfg["meta"]["exp"] == "stub_chain5" and st["week2_design"] == W.fingerprint(cfg)
    prop.write_text(prop.read_text().replace("| chain.boundary | compacted |",
                                             "| chain.boundary | full |"))
    with pytest.raises(SystemExit) as e:
        cli.week2_gate(SimpleNamespace(week2_config=conf), st)
    msg = str(e.value)
    assert "plan drift" in msg and "chain.boundary" in msg and "--accept-plan" in msg


def test_state_bound_to_design(tmp_path, monkeypatch):
    cli = _cli()
    monkeypatch.setattr(D, "status", lambda line: None)
    monkeypatch.setattr(D, "save_state", lambda st: None)
    conf, _ = _copy_design(tmp_path)
    st = {"gates": {}, "week2_design": {"exp": "stub_chain5", "config_sha256": "0" * 64,
                                        "proposal_sha256": "1" * 64}}
    with pytest.raises(SystemExit, match="started under"):
        cli.week2_gate(SimpleNamespace(week2_config=conf), st)


def test_admitted_parallelism():
    cli = _cli()
    cfg = W.load_config(STUB5)
    with pytest.raises(SystemExit, match="no admission check"):
        cli.admitted({"gates": {"admission_check": {"pass": True}}}, cfg)
    g = {"admission_check_6x3072": {"pass": True, "load_ok": True}}
    assert cli.admitted({"gates": g}, cfg) == (6, 3072)  # the fallback
    g["admission_check_10x2048"] = {"pass": True, "load_ok": False}
    assert cli.admitted({"gates": g}, cfg) == (6, 3072)  # 10 without a passing load: no
    g["admission_check_10x2048"]["load_ok"] = True
    assert cli.admitted({"gates": g}, cfg) == (10, 2048)


def test_two_phase_config_bound_to_code(tmp_path):
    fixture = CC.REPO_ROOT / "configs" / "week2" / "task_chaining.toml"
    assert W.two_phase_code_mismatches(W.load_config(fixture)) == []
    cfg = W.load_config(fixture)
    cfg["two_phase"]["histories_per_arm"] = 6
    cfg["caps"]["stage_usd"] = 130.0
    cfg["wording"]["lead_same"]["sha256"] = "0" * 64
    bad = W.two_phase_code_mismatches(cfg)
    assert any("runs" in b for b in bad) and any("histories_per_arm" in b for b in bad)
    assert any("caps.stage_usd" in b for b in bad) and any("wording.lead_same" in b for b in bad)
    assert W.two_phase_code_mismatches(W.load_config(STUB5))[0].startswith("design.kind")


def test_compact_all_in_parallel(tmp_path, monkeypatch):
    import threading
    import time
    hists = CC.histories()[:4]
    st = {"a": {h.id: {"state": "valid", "dir": str(tmp_path / h.id)} for h in hists}}
    live, peak, lock = [0], [0], threading.Lock()

    def fake_compact(d, hid, model):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.2)
        with lock:
            live[0] -= 1
        return {"cost_usd": 0.0, "pre_tokens_est": 10, "post_tokens_est": 5}
    monkeypatch.setattr(D.SE, "compact_history", fake_compact)
    monkeypatch.setattr(D.AU, "a_outcomes", lambda d, h: {})
    monkeypatch.setattr(D.AU, "compaction_audit", lambda a, r: {"ok": True})
    monkeypatch.setattr(D, "save_state", lambda st: None)
    monkeypatch.setattr(D, "status", lambda line: None)
    monkeypatch.setattr(D, "stop_requested", lambda: False)
    for h in hists:
        (tmp_path / h.id).mkdir()
    D.compact_all(st, None, None, lambda kind, d: (None, None), hists=hists, workers=4)
    assert all(st["a"][h.id]["compaction"] == "done" for h in hists) and peak[0] > 1
    st2 = {"a": {h.id: {"state": "valid", "dir": str(tmp_path / h.id)} for h in hists}}
    peak[0] = 0
    D.compact_all(st2, None, None, lambda kind, d: (None, None), hists=hists)  # default: in order
    assert peak[0] == 1 and all(st2["a"][h.id]["compaction"] == "done" for h in hists)


def test_unbound_state_with_runs_is_never_adopted(tmp_path, monkeypatch):
    cli = _cli()
    monkeypatch.setattr(D, "status", lambda line: None)
    monkeypatch.setattr(D, "save_state", lambda st: None)
    conf, _ = _copy_design(tmp_path)
    st = {"gates": {}, "a": {}, "b": {}, "chains": {"stub5": {"state": "done"}}}
    with pytest.raises(SystemExit, match="already holds runs"):
        cli.week2_gate(SimpleNamespace(week2_config=conf), st)


def test_go_keeps_the_checked_run_set(tmp_path, monkeypatch):
    cli = _cli()
    monkeypatch.setattr(D, "status", lambda line: None)
    monkeypatch.setattr(D, "save_state", lambda st: None)
    monkeypatch.setattr(D, "load_state", lambda: {"gates": {}, "a": {}, "b": {}})
    fixture = CC.REPO_ROOT / "configs" / "week2" / "task_chaining.toml"
    conf, _ = _copy_design(tmp_path, fixture)
    for extra in ({"same_project_only": True, "hold_instead_of_cut": True},
                  {"same_project_only": False, "hold_instead_of_cut": False}):
        a = SimpleNamespace(week2_config=conf, concurrency=None, go_from_jacob=True, **extra)
        with pytest.raises(SystemExit, match="checked run set"):
            cli.cmd_go(a)


def test_inherited_missing_log_stays_missing(tmp_path):
    src = tmp_path / "src"
    (src / "logs").mkdir(parents=True)
    (src / "logs" / "shim.jsonl").write_text('{"phase": "start"}\n')
    (src / "logs" / "ask.jsonl").write_text("")  # push.jsonl lost upstream
    env = E.ChainEnv.__new__(E.ChainEnv)
    env.out_dir = tmp_path / "run"
    env._init_home(src)
    assert env._inherited_missing == {"push"} and env._inherited["shim"] == ['{"phase": "start"}']
    fresh = E.ChainEnv.__new__(E.ChainEnv)
    fresh.out_dir = tmp_path / "run2"
    fresh._init_home(None)
    assert fresh._inherited_missing == set()


def test_go_refuses_stale_run_selection(tmp_path, monkeypatch):
    cli = _cli()
    monkeypatch.setattr(D, "status", lambda line: None)
    monkeypatch.setattr(D, "save_state", lambda st: None)
    fixture = CC.REPO_ROOT / "configs" / "week2" / "task_chaining.toml"
    conf, _ = _copy_design(tmp_path, fixture)
    for stale in ({"cuts": ["httpx-p-different-full"]}, {"same_project_only": True}):
        st = {"gates": {}, "a": {}, "b": {}, **stale}
        monkeypatch.setattr(D, "load_state", lambda st=st: st)
        a = SimpleNamespace(week2_config=conf, concurrency=None, go_from_jacob=True,
                            same_project_only=False, hold_instead_of_cut=True)
        with pytest.raises(SystemExit, match="carries a run selection"):
            cli.cmd_go(a)
