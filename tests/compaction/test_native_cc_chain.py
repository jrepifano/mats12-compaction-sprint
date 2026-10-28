"""compaction.s2.native.cc_chain: the pieces that don't need a live session."""

import json

from compaction.s2.chain import config as CC
from compaction.s2.native import cc_chain as X
from compaction.s2.native import turns as T


def ev(**kw):
    return json.dumps(kw)


def assistant(*blocks):
    return ev(type="assistant", message={"role": "assistant", "content": list(blocks)})


def user(*blocks):
    return ev(type="user", message={"role": "user", "content": list(blocks)})


def result(text, subtype="success", is_error=False):
    return ev(type="result", subtype=subtype, is_error=is_error, result=text, session_id="s1")


def test_parse_stop_completion_from_the_echoed_token():
    lines = [assistant({"type": "tool_use", "id": "t1", "name": "Bash",
                        "input": {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}}),
             user({"type": "tool_result", "tool_use_id": "t1",
                   "content": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"}),
             result("All done.")]
    stop, info = X.parse_stop(lines)
    assert stop.marker and not stop.asked_user and not stop.infra_failed
    assert T.decide(stop, T.State()).kind == "complete" and info["session_id"] == "s1"


def test_parse_stop_ask_user_and_max_turns_cut():
    lines = [assistant({"type": "tool_use", "id": "q", "name": "mcp__user-server__ask_user",
                        "input": {"question": "Force push?"}}),
             result("", subtype="error_max_turns", is_error=True)]
    stop, _ = X.parse_stop(lines)
    assert stop.asked_user and stop.cut and not stop.infra_failed
    assert T.decide(stop, T.State()).kind == "non_answer"


def test_a_max_turns_exit_without_result_text_is_a_progress_stop():
    # CLI 2.1.310's error_max_turns result carries no `result` field (astra cc-build r1 B4)
    line = ev(type="result", subtype="error_max_turns", is_error=True, session_id="s1")
    stop, _ = X.parse_stop([assistant({"type": "text", "text": "Working on it"}), line])
    assert stop.cut and not stop.infra_failed and stop.text == ""
    state = T.State()
    kinds = [T.decide(stop, state).kind for _ in range(7)]
    assert kinds == ["checkin"] * 6 + ["exhausted"]


def test_parse_stop_failures_and_cost_refusal():
    stop, _ = X.parse_stop([assistant({"type": "text", "text": "working"})])
    assert stop.infra_failed  # no result event
    for msg, end in X.CAP_ENDS.items():
        stop, info = X.parse_stop([result(f"API Error: 400 {msg}", is_error=True)])
        assert info["cap"] == end and not stop.infra_failed
    stop, info = X.parse_stop([result("API Error: 400 prompt is too long: 1200000 tokens",
                                      is_error=True)])
    assert info["overflow"] and not stop.infra_failed
    stop, info = X.parse_stop([result("API Error: 529 overloaded", is_error=True)])
    assert stop.infra_failed and info["cap"] is None


def test_cli_command_runs_under_a_login_shell_without_secrets():
    cmd = X.cli_command(session_id="abc", max_turns=4)
    assert cmd.startswith("unset ANTHROPIC_AUTH_TOKEN; exec bash -lc ")
    for part in ("--session-id abc", "--max-turns 4", "claude-haiku-5-5[1m]",
                 "--permission-mode bypassPermissions"):
        assert part in cmd
    assert "sk-ant" not in cmd
    r = X.cli_command(resume="abc")
    assert "--resume abc" in r and "--max-turns" not in r


def test_cli_env_points_at_the_proxy_and_disables_auto_compaction():
    class S:
        token = "tok"
    env = X.cli_env(S())
    assert env["ANTHROPIC_BASE_URL"] == f"http://egress:{X.PROXY_FWD_PORT}"
    assert env["DISABLE_AUTO_COMPACT"] == "1" and "DISABLE_COMPACT" not in env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert "egress" in env["NO_PROXY"]


def test_prompts_carry_experiment_1s_task_text_and_submit_line():
    repo = CC.REPOS["httpx"]
    ins = f"Fix the bug.\n\n{CC.DEEPSWE_ENDING}"
    p = X.task_prompt(ins, repo.a_branch)
    assert p.startswith("Please solve this issue: ")
    assert CC.task_text(ins, repo.a_branch) in p
    assert p.endswith(X.SUBMIT_LINE) and "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in p
    b = X.lead_and_task(CC.REPOS["tengo"], repo, ins)
    assert b.startswith(CC.lead_in("different", CC.REPOS["tengo"], repo))


def test_scripted_sim_never_answers():
    s = X.ScriptedSim()
    assert s.ask("May I force push?") == T.NON_ANSWER
    assert s.state.ask_user_call_count == 1 and s.export()["questions"] == ["May I force push?"]


def test_submit_line_is_experiment_1s_verbatim():
    from compaction.s2 import loop as L
    tmpl = L.MP.INSTANCE_TEMPLATE
    assert X.SUBMIT_LINE in tmpl  # step 6 of the instance template, unchanged


class _Proxy:
    def __init__(self, deadline):
        self.deadline, self.calls, self.system = deadline, 0, None


def test_an_expired_deadline_launches_nothing_and_caps_time(monkeypatch):
    import time
    launched = []
    monkeypatch.setattr(X.N, "detached_exec", lambda *a, **k: launched.append(1))
    run = X.TaskRun(ns=None, out=None, proxy=_Proxy(time.monotonic() - 1))
    assert run.drive("sid", "go", new_session=True) == X.END_TIME and launched == []


def test_result_json_is_written_when_the_proxy_cannot_start(tmp_path, monkeypatch):
    from compaction.s2.native import key_proxy as KP

    def boom(self):
        raise OSError("bind failed")
    monkeypatch.setattr(KP.KeyProxy, "start", boom)
    hist = CC.History("tengo", "np", 900, "divergent")
    cfg = X.HistoryConfig(hist=hist, a_task=None, b_task=None, out=tmp_path,
                          host_ip="host.docker.internal", spend_factory=lambda: None, key="k")
    rec = X._task(cfg, tmp_path, type("Tk", (), {"image": "img"})(), [], second=False,
                  sid="s", prompt="p", session_data=None, b_repo=None)
    res = json.loads((tmp_path / "result.json").read_text())
    assert rec["end"] == res["end"] == X.END_FAILED and "bind failed" in res["error"]
    assert not (tmp_path / "messages.json").exists()


def test_boundary_reports_an_unreadable_session_instead_of_raising(monkeypatch):
    def unreadable(sb, sid):
        raise OSError("gone")
    monkeypatch.setattr(X, "read_session", unreadable)
    out = X.boundary_compact(X.TaskRun(ns=None, out=None, proxy=_Proxy(None)), "sid")
    assert out["ok"] is False and "unreadable" in out["deviation"]


def test_score_b_masks_the_primary_when_the_run_says_it_is_unknown(tmp_path, monkeypatch):
    from compaction.s2.chain import score as S
    monkeypatch.setattr(S, "trap_fields", lambda *a, **k: {"teammate_lost": False,
                                                          "rewrite": "no"})
    res = {"end": "quota_refused", "n_calls": 3, "cost_usd": 0, "prompt_tokens": 0,
           "completion_tokens": 0, "cached_tokens": 0, "seconds": 1, "traps_home": None,
           "error": None, "extra": {"b_message_index": None,
                                    "primary_unknown_reason": "plan quota refused"}}
    (tmp_path / "result.json").write_text(json.dumps(res))
    brun = {"id": "b", "cell": {"b_task": "tengo-destructuring-bindings", "hist": "divergent",
                                "project": "different", "start": "compacted"},
            "history": "httpx-np-1", "rep": 1}
    row = S.score_b(tmp_path, brun)
    assert row["teammate_lost"] is None and row["termination"] == "failed"
    assert row["primary_unknown_reason"] == "plan quota refused"
