"""The Codex arm's rollout handling, recitation scoring and Q2 replacement (no CLI, no network)."""

from __future__ import annotations

import json
from pathlib import Path

from compaction.s2.native import codex as X

TID = "01a16f2e-62ae-7232-b3b6-0fbfc1c60701"


def _msg(role, text):
    return {"type": "response_item", "payload": {
        "type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}}


def _exec(cid, js):
    return {"type": "response_item", "payload": {
        "type": "custom_tool_call", "call_id": cid, "name": "exec", "input": js}}


def _exec_out(cid, text):
    return {"type": "response_item", "payload": {
        "type": "custom_tool_call_output", "call_id": cid,
        "output": [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\n"
                                                  "Output:\n"},
                   {"type": "input_text", "text": text}]}}


def _item(item):
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": item}}


def _cmd(cmd, out):
    return _item({"type": "CommandExecution", "command": ["/bin/bash", "-lc", cmd],
                  "aggregated_output": out, "exit_code": 0})


def _ask(q, reply):
    return _item({"type": "McpToolCall", "server": "user-server", "tool": "ask_user",
                  "arguments": {"question": q},
                  "result": {"content": [{"type": "text", "text": reply}],
                             "structuredContent": {"result": reply}, "isError": False}})


def _tokens(n):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": n, "output_tokens": 10}}}}


def rollout():
    return [
        {"type": "session_meta", "payload": {"id": TID, "base_instructions": {"text": "BASE"}}},
        _msg("developer", "DEV"),
        _msg("user", "do the task"),
        _exec("c1", "const r = await tools.exec_command({cmd:'cat a.py'}); text(r.output)"),
        _cmd("cat a.py", "x=1 FULL\n"),
        _exec_out("c1", "x=1\n"),
        _exec("c2", "const r = await tools.mcp__user_server__ask_user({question:'Q?'});"),
        _ask("Q?", "the answer"),
        _exec_out("c2", '{"content":[{"type":"text","text":"the answer"}]}'),
        _exec("c3", "text(1+1)"),
        _exec_out("c3", "2"),
        _exec("c4", "await tools.exec_command({cmd:'ls'}); await tools.exec_command({cmd:'pwd'})"),
        _cmd("ls", "a.py\n"),
        _cmd("pwd", "/app\n"),
        _exec_out("c4", "a.py\n/app\n"),
        _msg("assistant", "done"),
        _tokens(15000),
        {"type": "compacted", "payload": {"message": "", "replacement_history": [
            {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "DEV"}]},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "do the task"}]},
            {"type": "compaction", "encrypted_content": "gAAAAA" + "x" * 200}]}},
        _msg("user", "next"),
    ]


def test_history_before_compaction_maps_code_mode_calls():
    h = X.history_at(rollout(), 16)
    roles = [m["role"] for m in h.messages]
    assert roles[:3] == ["system", "system", "user"]
    calls = [m["tool_calls"][0]["function"] for m in h.messages if m.get("tool_calls")]
    assert [c["name"] for c in calls] == ["bash", "ask_user", "exec", "bash", "bash"]
    assert json.loads(calls[0]["arguments"]) == {"command": "cat a.py"}
    assert json.loads(calls[1]["arguments"]) == {"question": "Q?"}
    tools = [m["content"] for m in h.messages if m["role"] == "tool"]
    # one nested call: the JS output the model read; ask_user: the plain reply
    assert tools[0] == "x=1\n"
    assert tools[1] == "the answer"
    assert tools[2] == "2"
    # several nested calls: each call's own output
    assert tools[3:] == ["a.py\n", "/app\n"]
    assert h.n_compactions == 0 and h.summary_indices == []


def test_history_after_compaction_uses_replacement_and_summary_text():
    lines = rollout()
    h = X.history_at(lines, 18, summaries={1: "SUMMARY ONE"})
    assert h.n_compactions == 1
    assert [m["content"] for m in h.messages] == ["BASE", "DEV", "do the task", "SUMMARY ONE",
                                                  "next"]
    assert h.summary_indices == [3]
    ph = X.history_at(lines, 18)
    assert ph.messages[3]["content"] == "[encrypted compaction summary 1]"


def test_open_exec_at_cut_is_kept_as_a_call():
    h = X.history_at(rollout(), 4)
    assert h.messages[-1]["tool_calls"][0]["function"]["name"] == "exec"


def test_compaction_lines_and_tokens():
    lines = rollout()
    assert X.compaction_lines(lines) == [17]
    assert X.last_input_tokens(lines, 17) == 15010
    assert len(X.encrypted_items(lines[17])) == 1


def test_fork_rollout_renames_thread_and_truncates(tmp_path):
    lines = rollout()
    new, p = X.fork_rollout(lines, 17, tmp_path)
    assert new != TID and p.name.endswith(f"-{new}.jsonl")
    got = X.read_rollout(p)
    assert len(got) == 18 and got[-1]["type"] == "compacted"
    assert TID not in p.read_text() and X.thread_id(got) == new
    assert X.find_rollout(tmp_path, new) == p


def test_replace_compaction_swaps_encrypted_item_for_text():
    lines = rollout()
    out = X.replace_compaction(lines, 1, "OUR SUMMARY")
    rh = out[17]["payload"]["replacement_history"]
    assert not [h for h in rh if h.get("type") == "compaction"]
    assert rh[-1]["content"][0]["text"] == "OUR SUMMARY"
    assert X.encrypted_items(lines[17])  # the original is untouched
    h = X.history_at(out, 18)
    assert "OUR SUMMARY" in [m["content"] for m in h.messages]


def test_fernet_bound():
    assert X.fernet_plaintext_bound("gAAAAA" + "A" * 94) == 75 - 57


def test_extract_recitation():
    assert X.extract_recitation("blah\n```text\nline 1\nline 2\n```\n") == "line 1\nline 2"
    assert X.extract_recitation("NO SUMMARY") is None
    assert X.extract_recitation("plain") == "plain"


def test_invented_tokens_flags_only_unseen_identifiers():
    hist = json.dumps({"x": "release_channel = OKAPI-7731\nbatch heron-12"})
    rec = "- channel `OKAPI-7731`, batch heron-12\n- salt azure-wombat-19"
    assert X.invented_tokens(rec, hist) == ["azure-wombat-19"]


def test_item_overlap():
    a = "- read NOTES.md\n- the salt is fixed\n- batch heron-12 fails"
    assert X.item_overlap(a, a) == 1.0
    assert X.item_overlap(a, "- something else entirely here") == 0.0
    assert 0 < X.item_overlap(a, "- read NOTES.md\n- unrelated line of text") < 1


def test_score_validation_rule():
    hist = json.dumps({k: d["key"] for k, d in X.FACTS.items()})
    full = "\n".join(f"- {d['key']}" for d in X.FACTS.values())
    s = X.score_validation([full, full, full], "", hist)
    assert s["kept_facts"] == sorted(X.FACTS) and s["mean_recall"] == 1.0
    assert s["mean_overlap"] == 1.0 and s["usable"]
    bad = full + "\n- NARWHAL-1189"
    s = X.score_validation([bad, full, full], "", hist)
    assert s["decoys_any"] == ["NARWHAL-1189"] and not s["usable"]
    s = X.score_validation([None, full, full], "", hist)
    assert not s["usable"]


def test_toml_dumps_local_provider():
    text = X.toml_dumps(X.local_provider_config({"model": "gpt-6-luna"}))
    assert 'model_provider = "oai-local"' in text
    assert "[model_providers.oai-local]" in text
    assert "requires_openai_auth = true" in text
    assert "[model_providers.oai-local.capabilities]" in text
    assert 'remote_compaction = "unsupported"' in text


def test_rollout_boundary_pi_layout():
    lines = rollout()
    nb = X.rollout_boundary(lines, 1, {1: "RECITED"}, model="gpt-6-luna")
    assert nb.summary == "RECITED" and nb.post_messages[nb.summary_index]["content"] == "RECITED"
    assert nb.pre_tokens == 15010 and nb.trigger == "auto" and nb.policy == X.POLICY_REMOTE
    # the kept user message points at its twin in pre_messages
    kept = [nb.pre_messages[i]["content"] for i in nb.kept_indices]
    assert kept == ["do the task"]
    assert nb.info["encrypted_items"] == 1
    # ask_user calls carry the plain reply, for interact_run.reply_index
    asks = [i for i, m in enumerate(nb.pre_messages)
            if (m.get("tool_calls") or [{}])[0].get("function", {}).get("name") == "ask_user"]
    assert nb.pre_messages[asks[0] + 1]["content"] == "the answer"


def test_second_boundary_sees_first_summary():
    lines = rollout()
    more = [_msg("assistant", "working"), _tokens(20000), {"type": "compacted", "payload": {
        "message": "", "replacement_history": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "do the task"}]},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "next"}]},
            {"type": "compaction", "encrypted_content": "gAAAAA" + "y" * 100}]}}]
    lines = lines + more
    nb = X.rollout_boundary(lines, 2, {1: "S1", 2: "S2"}, model="m")
    assert "S1" in [m["content"] for m in nb.pre_messages]
    assert nb.summary == "S2" and nb.pre_tokens == 20010


def test_container_config_and_command():
    cfg = X.CodexConfig(auto_compact_limit=150000)
    text = X.toml_dumps(cfg.config("http://user-server:8000/mcp", "X-Emx-Token"))
    assert "model_auto_compact_token_limit = 150000" in text
    assert 'web_search = "disabled"' in text
    assert "[mcp_servers.user-server]" in text and "EMX_SIM_TOKEN" in text
    cmd = X.cli_command(cfg)
    assert "--dangerously-bypass-hook-trust" in cmd and cmd.endswith("-- -")
    assert "unset OPENAI_API_KEY" in cmd
    assert X.hooks_json()["hooks"]["PostCompact"][0]["hooks"][0]["command"] == X.HOOK


def test_auth_tokens_only_long_strings():
    a = {"tokens": {"access_token": "a" * 40, "refresh_token": "r" * 40, "id_token": "short"},
         "OPENAI_API_KEY": None}
    assert X.auth_tokens(a) == ["a" * 40, "r" * 40]


class _SB:
    name = "c"
    host = None


def _scan_env(monkeypatch, rc, out, err):
    seen = {}

    class P:
        returncode, stdout, stderr = rc, out.encode(), err.encode()

    def fake_run(argv, *, input=None, env=None, **k):
        seen.update(argv=argv, input=input, env=env)
        return P()

    monkeypatch.setattr(X.subprocess, "run", fake_run)
    monkeypatch.setattr(X, "changed_files", lambda sb: ["/app/a.py", "/root/.x/leak"])
    return seen


def test_secret_scan_one_grep_list_on_stdin_values_only_in_env(monkeypatch):
    seen = _scan_env(monkeypatch, 0, "/root/.x/leak\n", "")
    assert X.secret_scan(_SB(), ["s" * 30, "t" * 30]) == ["/root/.x/leak"]
    cmd = " ".join(seen["argv"])
    assert "s" * 30 not in cmd and "t" * 30 not in cmd and "-i" in seen["argv"]
    assert {seen["env"][k] for k in seen["env"] if k.startswith(X.SECRET_ENV)} == {
        "s" * 30, "t" * 30}
    assert cmd.count('-e "$') == 2
    assert seen["input"] == b"/app/a.py\0/root/.x/leak\0"  # no file, no size limit


def test_secret_scan_fails_closed(monkeypatch):
    import pytest

    from compaction.deepswe import sandbox as SB
    _scan_env(monkeypatch, 0, "", "__GREP_ERR 2\n")
    with pytest.raises(SB.SandboxError):
        X.secret_scan(_SB(), ["s" * 30])
    _scan_env(monkeypatch, 123, "", "")
    with pytest.raises(SB.SandboxError):
        X.secret_scan(_SB(), ["s" * 30])


def test_redact_and_recitation_validity():
    assert X.redact(["curl -H X-Emx-Token: abc123", "ps"], ["abc123"]) == [
        "curl -H X-Emx-Token: <redacted>", "ps"]
    r = X.Recitation(1, "t", "text", "raw", 0, 1.0, 0, None)
    assert r.valid and r.to_dict()["valid"]
    assert not X.Recitation(1, "t", "text", "raw", 1, 1.0, 0, None).valid  # ran a tool
    assert not X.Recitation(1, "t", "text", "raw", 0, 1.0, 1, None).valid  # CLI failed
    assert not X.Recitation(1, "t", None, "raw", 0, 1.0, 0, None).valid
    assert X.extract_recitation("```text\nNO SUMMARY\n```") is None


def test_hook_keeps_the_token_out_of_argv():
    text = X.hook_script()
    assert "-H @" in text and "printf" in text
    curl = next(x for x in text.splitlines() if x.startswith("curl"))
    assert "EMX_SIM_TOKEN" not in curl


def test_visible_outputs_keep_only_what_the_model_read():
    ask = {"type": "McpToolCall", "tool": "ask_user", "arguments": {"question": "Q?"},
           "result": {"structuredContent": {"result": "line one\nSECRET-FACT"}}}
    cmd_a = {"type": "CommandExecution", "command": ["/bin/bash", "-lc", "cat a"],
             "aggregated_output": "HIDDEN-A\n"}
    cmd_b = {"type": "CommandExecution", "command": ["/bin/bash", "-lc", "cat b"],
             "aggregated_output": "B\n"}
    # the bare MCP wrapper (reply JSON-escaped inside it): the plain reply
    out = X.visible_outputs([ask], "c", '{"structuredContent":{"result":"line one\\nSECRET-FACT"}}')
    assert out[0][1] == "line one\nSECRET-FACT"
    # any other JSON shape: the JS output as the model read it
    out = X.visible_outputs([ask], "c", '{"result":"line one\\nSECRET-FACT"}')
    assert out[0][1] == '{"result":"line one\\nSECRET-FACT"}'
    # reply not shown (the JS dropped it): the JS output, never the hidden reply
    out = X.visible_outputs([ask], "c", "ok")
    assert out[0][1] == "ok"
    # two calls, the JS printed only a summary: nothing hidden leaks into the history
    out = X.visible_outputs([cmd_a, cmd_b], "c", "2 files")
    assert [o for _, o in out] == ["", "2 files"]
    assert [c["id"] for c, _ in out] == ["c.0", "c.1"]
    # two calls, the JS printed exactly both outputs: per-call outputs
    out = X.visible_outputs([cmd_a, cmd_b], "c", "HIDDEN-A\nB\n")
    assert [o for _, o in out] == ["HIDDEN-A\n", "B\n"]


def test_score_uses_all_planted_facts():
    hist = json.dumps({k: d["key"] for k, d in X.FACTS.items()})
    one = "- " + X.FACTS["F1"]["key"]
    s = X.score_validation([one, one, one], "", hist)
    # conditioned recall would be 1.0 here; the primary one is 1/6 and fails the rule
    assert s["mean_recall_kept"] == 1.0 and s["mean_recall"] == round(1 / 6, 3)
    assert not s["usable"] and s["fact_agreement"] == 1.0


def test_ask_user_reply_normalised_only_when_nothing_else_shown():
    ask = {"type": "McpToolCall", "tool": "ask_user", "arguments": {"question": "Q?"},
           "result": {"structuredContent": {"result": "the answer"}}}
    wrap = '{"content":[{"type":"text","text":"the answer"}],"structuredContent":' \
           '{"result":"the answer"},"isError":false}'
    assert X.visible_outputs([ask], "c", wrap)[0][1] == "the answer"
    assert X.visible_outputs([ask], "c", "the answer\n")[0][1] == "the answer"
    extra = "the answer\nRETRY_LIMIT=7"
    assert X.visible_outputs([ask], "c", extra)[0][1] == extra
    extra_json = wrap[:-1] + ',"note":"RETRY_LIMIT=7"}'
    assert X.visible_outputs([ask], "c", extra_json)[0][1] == extra_json


def test_changed_files_skips_only_the_secrets_tmpfs(monkeypatch):
    from compaction.deepswe import sandbox as SB

    class P:
        returncode = 0
        stdout = (b"A /run/persisted-copy\nA /run/emx-codex\nA /run/emx-codex/auth.json\n"
                  b"C /root\nA /root/x\nD /app/gone\nA /proc/1\n")

    monkeypatch.setattr(SB, "_docker", lambda *a, **k: P())

    class S:
        host = None
        name = "c"

    assert X.changed_files(S()) == ["/run/persisted-copy", "/root", "/root/x"]


def test_current_tokens_fails_closed(monkeypatch):
    import pytest

    from compaction.deepswe import sandbox as SB
    from compaction.s2.native import common as N
    for res in (SB.ExecResult(1, "", "no file", 0.1), SB.ExecResult(0, "not json", "", 0.1),
                SB.ExecResult(0, '{"tokens": {}}', "", 0.1)):
        monkeypatch.setattr(N, "exec_env", lambda *a, _r=res, **k: _r)
        with pytest.raises(SB.SandboxError):
            X.current_tokens(object())
    good = json.dumps({"tokens": {"refresh_token": "r" * 40}})
    monkeypatch.setattr(N, "exec_env", lambda *a, **k: SB.ExecResult(0, good, "", 0.1))
    assert X.current_tokens(object()) == ["r" * 40]


def test_recite_timeout_is_an_invalid_recitation(monkeypatch, tmp_path):
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("codex", 1)

    monkeypatch.setattr(X, "codex_exec", boom)
    home = X.CodexHome(tmp_path / "h")
    r = X.recite(home, rollout(), 1, cwd=tmp_path, timeout_s=1)
    assert r.rc == -1 and not r.valid


def test_reciter_tags_are_unique_and_setup_failure_cleans_up(monkeypatch, tmp_path):
    import pytest
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "a" * 40}}))
    rc = X.Reciter(tmp_path / "run", auth)
    tags = []
    monkeypatch.setattr(X, "recite", lambda *a, tag="", **k: tags.append(tag) or
                        X.Recitation(1, "t", "x", "x", 0, 0.1, 0, None))
    rc.run(rollout(), 1, 2, label="_02_implement_k1")
    rc.run(rollout(), 1, 1, label="_02_implement_k1")
    assert len(set(tags)) == 3
    d = rc.dir
    rc.close()
    assert not d.exists()
    made = []
    real = X.tempfile.mkdtemp
    monkeypatch.setattr(X.tempfile, "mkdtemp", lambda **k: made.append(real(**k)) or made[-1])
    with pytest.raises(FileNotFoundError):
        X.Reciter(tmp_path / "run", tmp_path / "missing.json")
    assert not Path(made[0]).exists()


def test_file_change_becomes_apply_patch_and_is_never_dropped():
    fc = {"type": "FileChange", "changes": {
        "/app/a.py": {"type": "update", "unified_diff": "@@ -1 +1 @@\n-x\n+y\n"},
        "/app/new.py": {"type": "add", "unified_diff": "+z\n"}}}
    cmd = {"type": "CommandExecution", "command": ["/bin/bash", "-lc", "pytest -q"],
           "aggregated_output": "1 passed\n"}
    out = X.visible_outputs([fc, cmd], "c", "1 passed\n")
    names = [c["function"]["name"] for c, _ in out]
    assert names == ["apply_patch", "bash"]
    patch = json.loads(out[0][0]["function"]["arguments"])["input"]
    assert patch.startswith("*** Begin Patch") and patch.endswith("*** End Patch")
    assert "*** Update File: /app/a.py" in patch and "*** Add File: /app/new.py" in patch
    assert "+y" in patch
    # alone in its exec: the apply_patch call, with the JS output as its result
    out = X.visible_outputs([fc], "c", "")
    assert out[0][0]["function"]["name"] == "apply_patch"


def test_only_reply_rejects_extra_content_shapes():
    w = '{"content":%s,"structuredContent":{"result":"r"}}'
    assert X._only_reply(w % '[{"type":"text","text":"r"}]', "r")
    assert not X._only_reply(w % '[{"type":"text","text":"r","note":"RETRY_LIMIT=7"}]', "r")
    assert not X._only_reply(w % '[{"type":"text","text":"r"},"RETRY_LIMIT=7"]', "r")
    assert not X._only_reply(w % '[{"type":"text","text":7}]', "r")
    assert not X._only_reply(w % '"r"', "r")


def test_codex_exec_timeout_keeps_partial_logs(monkeypatch, tmp_path):
    import subprocess

    import pytest

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("codex", 1, output=b'{"type":"turn.started"}\n',
                                        stderr=b"warn\n")

    monkeypatch.setattr(X.subprocess, "run", slow)
    log = tmp_path / "r.jsonl"
    with pytest.raises(subprocess.TimeoutExpired):
        X.codex_exec(X.CodexHome(tmp_path / "h"), "p", cwd=tmp_path, log_path=log)
    assert "turn.started" in log.read_text()
    assert "warn" in (tmp_path / "r.jsonl.err").read_text()


def test_majority_recitation():
    a = "- read NOTES.md\n- the salt is fixed\n- batch heron-12 fails"
    b = "- read NOTES.md\n- the salt is fixed\n- batch heron-12 failed"
    c = "- something else entirely here\n- and another unrelated line"
    i, means = X.majority([c, a, b])
    assert i == 1 and means[0] < means[1]
    assert X.majority(["only"]) == (0, [1.0])


def test_stage_exposes_native_boundaries_to_lossaudit(tmp_path):
    import pytest

    from compaction.s2 import retention as RT
    run = tmp_path / "runs" / "smoke-x-codex-luna-a1"
    for k, complete in ((1, True), (2, True), (3, False)):
        b = run / f"b{k}" / "boundary"
        b.mkdir(parents=True)
        (b / "post_messages.json").write_text("[]")
        if complete:
            (b / "boundary.json").write_text("{}")
    (run / "steps").mkdir()
    st = tmp_path / "stage"
    links = X.stage([run], st)
    assert [x.parent.name for x in links] == [f"{run.name}-b1", f"{run.name}-b2"]
    got = RT.boundary_dirs(st)
    assert sorted(p.resolve() for p in got) == [(run / "b1").resolve(), (run / "b2").resolve()]
    assert X.stage([run], st) == links  # idempotent
    (st / f"{run.name}-b1" / "a1").unlink()
    (st / f"{run.name}-b1" / "a1").symlink_to(run / "b2")
    with pytest.raises(FileExistsError):
        X.stage([run], st)


def test_loss_ledger_new_inherited_unmatched():
    def row(k, q, grade, lost):
        return {"boundary_dir": f"runs/r/b{k}", "quote": q, "grade": grade, "lost": lost,
                "valid": True}
    rows = [row(1, "Use  X", "dropped", True), row(1, "keep Y", "kept", False),
            row(2, "use x", "dropped", True), row(2, "keep y", "dropped", True),
            row(2, "brand new", "misstated", True)]
    led = X.loss_ledger(rows)
    assert (led["new"], led["inherited"], led["unmatched"]) == (2, 1, 1)
    assert led["by_boundary"]["b2"] == {"new": 1, "inherited": 1, "unmatched": 1}




def test_loss_ledger_ignores_invalid_prior_grades_and_conflicts():
    def row(k, q, grade, lost, gv=True):
        return {"boundary_dir": f"r/b{k}", "quote": q, "grade": grade, "lost": lost,
                "valid": True, "grade_valid": gv}
    rows = [row(1, "a", "dropped", True, gv=False), row(1, "b", "kept", False),
            row(1, "b", "dropped", True), row(2, "a", "dropped", True),
            row(2, "b", "dropped", True)]
    led = X.loss_ledger(rows)
    assert led["by_boundary"]["b2"] == {"new": 0, "inherited": 0, "unmatched": 2}


class _FakeProc:
    stdout = None

    def __init__(self, *a, **k):
        import io
        self.stdout = io.BytesIO(b"")

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def _detached_env(monkeypatch, rc_text="", stall=0.0):
    import time

    from compaction.deepswe import sandbox as SB
    from compaction.s2.native import common as N
    calls = []

    def raw(sb, cmd, *, timeout_s, workdir="/"):
        calls.append(cmd)
        if stall:
            time.sleep(min(stall, timeout_s))  # a stalled transport, bounded by the caller
        if cmd.startswith("cat ") and cmd.endswith("/rc 2>/dev/null"):
            return SB.ExecResult(0, rc_text, "", 0.0)
        return SB.ExecResult(1, "", "transport down", 0.0)

    monkeypatch.setattr(X, "raw_exec", raw)
    monkeypatch.setattr(N, "exec_env", lambda *a, **k: SB.ExecResult(0, "", "", 0.0))
    monkeypatch.setattr(X.subprocess, "Popen", _FakeProc)

    class Sb:
        name, host = "c", None

        def exec(self, cmd, timeout_s=600, workdir="/app"):
            return SB.ExecResult(0, "", "", 0.0)

        def put_file(self, data, path):
            pass

    return Sb(), calls


def test_detached_step_honours_stop_when_sessions_fail_immediately(monkeypatch, tmp_path):
    import threading
    import time
    sb, _ = _detached_env(monkeypatch)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    t0 = time.monotonic()
    r = X.detached_step(sb, "true", name="s", env={}, stdin=None, timeout_s=600,
                        log_path=tmp_path / "e.jsonl", stop=stop)
    assert r.killed == "stop" and r.rc == -1 and time.monotonic() - t0 < 10


def test_detached_step_deadline_with_failing_sessions(monkeypatch, tmp_path):
    import time
    sb, _ = _detached_env(monkeypatch)
    t0 = time.monotonic()
    # deadline = timeout + CLIENT_GRACE_S + 60: a negative timeout puts it ~1 s out
    r = X.detached_step(sb, "true", name="s", env={}, stdin=None, timeout_s=-89,
                        log_path=tmp_path / "e.jsonl")
    assert r.killed == "client_timeout" and r.rc == -1 and time.monotonic() - t0 < 10


def test_detached_step_failed_drain_is_incomplete_not_success(monkeypatch, tmp_path):
    sb, calls = _detached_env(monkeypatch, rc_text="0\n")
    monkeypatch.setattr(X, "RC_CHECK_S", 0)
    class FastStop(X.threading.Event):
        def wait(self, timeout=None):  # no real backoff sleeps in the test
            return False

    r = X.detached_step(sb, "true", name="s", env={}, stdin=None, timeout_s=600,
                        log_path=tmp_path / "e.jsonl", stop=FastStop())
    assert r.rc == 0 and r.output_incomplete is True
    assert sum(c.startswith("tail -c +") for c in calls) == 6  # retried, then flagged


def _driver():
    import importlib.util

    from compaction.s2 import config as C
    spec = importlib.util.spec_from_file_location(
        "s2_native_codex_cli", C.REPO_ROOT / "scripts" / "s2_native_codex.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_driver_adoption_never_crosses_arms():
    m = _driver()
    main = "/x/bin/python scripts/s2_native_codex.py base --task helm --limit 150000"
    t80 = "/x/bin/python scripts/s2_native_codex.py base --task helm --root /r/t80k --limit 80000"
    assert m._matches(main, "helm", None) and not m._matches(t80, "helm", None)
    assert m._matches(t80, "helm", "/r/t80k") and not m._matches(main, "helm", "/r/t80k")
    assert not m._matches(main, "hel", None)


def test_driver_failed_grading_is_none_not_zero(tmp_path):
    m = _driver()
    rd = tmp_path / "helm-codex-luna-a1"
    (rd / "steps").mkdir(parents=True)
    (rd / "result.json").write_text(json.dumps({"end": "submitted", "steps": {},
                                                "boundaries": [], "sim_cost_usd": 1.0}))
    (rd / "run.json").write_text("{}")
    s = m._run_summary(rd, None)
    assert s["items"] is None and s["lost"] is None and s["new"] is None and not s["graded"]
    gd = tmp_path / "g"
    gd.mkdir()
    (gd / "losses.jsonl").write_text("")
    s = m._run_summary(rd, gd)
    assert s["items"] == 0 and s["graded"]


def test_detached_step_deadline_bounds_stalled_calls(monkeypatch, tmp_path):
    import time
    sb, _ = _detached_env(monkeypatch, stall=5)
    monkeypatch.setattr(X, "RC_CHECK_S", 0)
    t0 = time.monotonic()
    r = X.detached_step(sb, "true", name="s", env={}, stdin=None, timeout_s=-88,
                        log_path=tmp_path / "e.jsonl")
    # deadline ~2 s out: every stalled call is cut to the time left; cleanup allowance aside
    assert r.killed == "client_timeout" and time.monotonic() - t0 < 60


def test_driver_registry_counts_live_launches(monkeypatch, tmp_path):
    import os
    m = _driver()
    monkeypatch.setattr(m, "LAUNCH_REGISTRY", tmp_path / "reg.json")
    monkeypatch.setattr(m, "LAUNCH_LOCK", tmp_path / "reg.lock")
    monkeypatch.setattr(m, "_base_proc_table", lambda: [(11, 1, "py a"), (12, 1, "py b")])
    with m._launch_lock():
        m._register(os.getpid())
        m._register(99999999)  # not alive: dropped
    # one registered launch (no Python child yet) + two older unregistered runs = 3
    assert m._all_base_runs() == 3
    assert json.loads((tmp_path / "reg.json").read_text()) == [os.getpid()]


def test_raw_exec_client_timeout_is_the_budget(monkeypatch):
    import subprocess
    seen = {}

    def run(argv, *, capture_output, timeout, check):
        seen["timeout"] = timeout
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(X.subprocess, "run", run)

    class Sb:
        name, host = "c", None

    r = X.raw_exec(Sb(), "cat /x", timeout_s=2)
    assert seen["timeout"] == 2 and r.rc == 124


def test_prune_kept_images_policy_and_digest_log(monkeypatch, tmp_path):
    m = _driver()
    rd = tmp_path / "run"
    for k, elig in ((1, False), (2, True), (3, None)):
        (rd / f"b{k}" / "boundary").mkdir(parents=True)
        (rd / f"b{k}" / "boundary" / "boundary.json").write_text(
            json.dumps({"tag": f"emx-sandbox/x:b{k}", "eligible": elig}))
    ran = []

    class P:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def run(argv, **k):
        ran.append(argv)
        return P(0, "sha256:abc\n") if argv[1] == "image" else P(0)

    monkeypatch.setattr("subprocess.run", run)
    rows = m.prune_kept_images(rd, tmp_path, lambda s: None)  # explicit policy
    assert [r.get("removed") for r in rows] == [True, None, None]
    assert rows[1]["kept"] == "fork candidate"
    assert rows[2]["kept"] == "eligibility not evaluated"  # the default null is never pruned
    ran.clear()
    rows = m.prune_kept_images(rd, tmp_path, lambda s: None, q1_only=True)
    rm = [a[2] for a in ran if a[:2] == ["docker", "rmi"]]
    assert rm == ["emx-sandbox/x:b1", "emx-sandbox/x:b3"]  # never the eligible=true one
    logged = [json.loads(x) for x in (tmp_path / "pruned.jsonl").read_text().splitlines()]
    assert logged[0]["digest"] == "sha256:abc" and logged[-1]["why"] == "q1-only arm"


class _P:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _gate_env(monkeypatch, m, ps, inspect=None,
              mp="System-wide memory free percentage: 70%"):
    inspect = inspect or _P(0, "")

    def run(argv, **k):
        if argv[:2] == ["docker", "ps"]:
            return ps
        if argv[:2] == ["docker", "inspect"]:
            return inspect
        if argv[0] == "memory_pressure":
            return _P(0, mp)
        raise AssertionError(argv)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(m, "docker_disk_free_gb", lambda: 500.0)
    monkeypatch.setattr(m.os, "getloadavg", lambda: (1.0, 1.0, 1.0))


def test_laptop_gate_fails_closed(monkeypatch, tmp_path):
    m = _driver()
    monkeypatch.setattr(m, "RESERVATIONS", tmp_path / "res.json")
    _gate_env(monkeypatch, m, _P(1, "", "daemon down"))
    assert m.laptop_gate(3, 2)[0] is False
    _gate_env(monkeypatch, m, _P(0, "a b\n"), _P(0, "3221225472 2000000000\n"))  # 1 of 2
    assert m.laptop_gate(3, 2)[0] is False
    _gate_env(monkeypatch, m, _P(0, "a\n"), _P(0, "0 2000000000\n"))  # unlimited memory
    assert m.laptop_gate(3, 2)[0] is False
    _gate_env(monkeypatch, m, _P(0, ""), mp="no reading")
    assert m.laptop_gate(3, 2)[0] is False
    _gate_env(monkeypatch, m, _P(0, ""))
    assert m.laptop_gate(3, 2)[0] is True


def test_laptop_gate_counts_pending_reservations(monkeypatch, tmp_path):
    import os
    m = _driver()
    monkeypatch.setattr(m, "RESERVATIONS", tmp_path / "res.json")
    _gate_env(monkeypatch, m, _P(0, ""))
    m.reserve(os.getpid(), host=None, cap=6.5, mem_gb=3, cpus=2)  # containers not up yet
    ok, why = m.laptop_gate(3, 2)
    assert not ok and "1 pending" in why  # 2 + 2 containers > 3


def test_reservation_counts_until_marked_visible_and_probe_errors_hold(monkeypatch, tmp_path):
    import os
    m = _driver()
    monkeypatch.setattr(m, "RESERVATIONS", tmp_path / "res.json")
    monkeypatch.setattr(m, "LAUNCH_LOCK", tmp_path / "lock")
    _gate_env(monkeypatch, m, _P(0, ""))
    m.reserve(os.getpid(), host=None, cap=6.5, mem_gb=3, cpus=2)
    rows = json.loads((tmp_path / "res.json").read_text())
    rows[0]["ts"] -= 10_000  # old, but never seen: still counts
    (tmp_path / "res.json").write_text(json.dumps(rows))
    assert m.laptop_gate(3, 2)[0] is False
    m.mark_visible({os.getpid()})  # its containers are up: docker ps counts them now
    assert m.laptop_gate(3, 2)[0] is True
    assert m.reservations()[0]["cap"] == 6.5  # the cap still counts for the tag gate

    def boom():
        raise OSError("docker gone")

    monkeypatch.setattr(m, "docker_disk_free_gb", boom)
    ok, why = m.laptop_gate(3, 2)
    assert not ok and "probe failed" in why


def test_adopted_run_carries_its_pids_for_the_tag_gate(monkeypatch):
    m = _driver()
    cmd = "/x/bin/python scripts/s2_native_codex.py base --task helm --sim-cap 3.5 --limit 1"
    monkeypatch.setattr(m, "_base_proc_table", lambda: [(200, 199, cmd)])
    ad = m._Adopted("helm", None)
    # its own reservation (keyed by the uv wrapper, 199) is excluded from "my other drivers"
    assert ad.pids == {199, 200} and ad.cap == 3.5


def test_reciter_gives_each_concurrent_recitation_its_own_home(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "a" * 40}}))
    rc = X.Reciter(tmp_path / "run", auth)
    homes = []

    def fake(home, *a, tag="", **k):
        homes.append(str(home.path))
        ok = len(homes) != 2  # the second one fails
        return X.Recitation(1, "t", "x" if ok else None, "x", 0, 0.1, 0 if ok else 1, None)

    monkeypatch.setattr(X, "recite", fake)
    recs = rc.run_until(rollout(), 1, 3, label="_k1")
    assert len(set(homes[:3])) == 3  # three concurrent recitations, three homes
    assert sum(r.valid for r in recs) == 3 and len(recs) == 4  # topped up after the failure
    rc.close()


def _rerecite_fixture(monkeypatch, tmp_path):
    from compaction.s2 import compact as K
    rd = tmp_path / "run"
    b = rd / "b1"
    (b / "boundary").mkdir(parents=True)
    (b / "native").mkdir()
    (rd / "sim").mkdir()
    (rd / "sim" / "replies.json").write_text("[]")
    lines = rollout()[:18]
    X.write_rollout(b / "native" / "rollout.jsonl", lines)
    nb = X.rollout_boundary(lines, 1, {1: "OLD"}, model="gpt-6-luna")
    rec = K.boundary_record("run-b1", nb.pre_messages, model=nb.model, pre_tokens=1,
                            summary="OLD")
    for name, obj in (("record", rec), ("pre_messages", nb.pre_messages),
                      ("post_messages", nb.post_messages), ("sim_replies", []),
                      ("compaction", {"summary": "OLD", "post_messages": nb.post_messages}),
                      ("boundary", {"boundary_in_session": 1, "rollout_path": "p",
                                    "summary_index": nb.summary_index,
                                    "cli_version": X.CLI_VERSION})):
        (b / "boundary" / f"{name}.json").write_text(json.dumps(obj))
    old = X.Recitation(1, "t0", "OLD", "OLD", 0, 0.1, 0, None)
    (b / "native" / "recitations.json").write_text(
        json.dumps([old.to_dict(), X.Recitation(1, "t1", None, "", 0, 0.1, 1, None).to_dict()]))

    def run_until(self, lines, k, repeats, *, label="", left=None, retries=2, have=None):
        recs = list(have)
        while sum(r.valid for r in recs) < repeats:
            recs.append(X.Recitation(1, "n", "- line a\n- line b", "", 0, 0.1, 0, None))
        return recs

    monkeypatch.setattr(X.Reciter, "run_until", run_until)
    monkeypatch.setattr(X, "codex_version", lambda: X.CLI_VERSION)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "a" * 40}}))
    return rd, b, rec, auth


def test_rerecite_run_tops_up_keeps_identity_and_originals(monkeypatch, tmp_path):
    rd, b, rec, auth = _rerecite_fixture(monkeypatch, tmp_path)
    rows = X.rerecite_run(rd, auth_path=auth, log=lambda s: None)
    assert rows[0]["valid_before"] == 1 and rows[0]["complete"]
    bj = json.loads((b / "boundary" / "boundary.json").read_text())
    assert bj["rerecited"]["valid_after"] == 3 and len(bj["recite_agreement"]) == 3
    assert "original" in bj["rerecited"] and (b / "native" / "orig" / "record.json").exists()
    new = json.loads((b / "boundary" / "record.json").read_text())
    assert new["summary"] == "- line a\n- line b"
    assert new["id"] == rec["id"] and new["session_id"] == rec["session_id"]
    X.rerecite_run(rd, auth_path=auth, log=lambda s: None)  # a second pass: identity holds
    again = json.loads((b / "boundary" / "record.json").read_text())
    assert again["id"] == rec["id"]
    assert json.loads((rd / "rerecite.json").read_text())["status"] == "complete"


def test_rerecite_rolls_back_an_interrupted_publication(monkeypatch, tmp_path):
    rd, b, rec, auth = _rerecite_fixture(monkeypatch, tmp_path)
    X._backup(b)
    (b / "boundary" / "boundary.json").replace(b / "boundary" / "boundary.json.rerecite-pending")
    (b / "boundary" / "record.json").write_text('{"half": "written"}')
    rows = X.rerecite_run(rd, auth_path=auth, log=lambda s: None)
    assert rows and rows[0]["complete"]
    assert not (b / "boundary" / "boundary.json.rerecite-pending").exists()
    assert json.loads((b / "boundary" / "record.json").read_text())["id"] == rec["id"]


def test_rerecite_refuses_another_cli_version(monkeypatch, tmp_path):
    import pytest
    rd, _b, _rec, auth = _rerecite_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(X, "codex_version", lambda: "0.170.0")
    with pytest.raises(RuntimeError):
        X.rerecite_run(rd, auth_path=auth, log=lambda s: None)


def test_rerecite_rollback_with_both_markers_and_stable_original(monkeypatch, tmp_path):
    rd, b, rec, auth = _rerecite_fixture(monkeypatch, tmp_path)
    X.rerecite_run(rd, auth_path=auth, log=lambda s: None)
    first = json.loads((b / "boundary" / "boundary.json").read_text())["rerecited"]["original"]
    # interrupted after publishing boundary.json but before removing the pending marker
    (b / "boundary" / "boundary.json.rerecite-pending").write_text("{}")
    (b / "boundary" / "record.json").write_text('{"half": "written"}')
    rows = X.rerecite_run(rd, auth_path=auth, log=lambda s: None)
    assert rows[0]["complete"]
    assert json.loads((b / "boundary" / "record.json").read_text())["id"] == rec["id"]
    second = json.loads((b / "boundary" / "boundary.json").read_text())["rerecited"]
    assert second["original"] == first  # the capture's own values, not the last repair's
    assert not (b / "boundary" / "boundary.json.rerecite-pending").exists()


def test_rerecite_refuses_a_missing_version_before_any_write(monkeypatch, tmp_path):
    import pytest
    rd, b, _rec, auth = _rerecite_fixture(monkeypatch, tmp_path)
    bj = json.loads((b / "boundary" / "boundary.json").read_text())
    bj.pop("cli_version")
    (b / "boundary" / "boundary.json").write_text(json.dumps(bj))
    before = {p: p.read_bytes() for p in b.rglob("*") if p.is_file()}
    with pytest.raises(RuntimeError):
        X.rerecite_run(rd, auth_path=auth, log=lambda s: None)
    after = {p: p.read_bytes() for p in b.rglob("*") if p.is_file()}
    assert before == after and not (rd / "rerecite.json").exists()


def test_rerecite_keeps_each_boundarys_own_replies(monkeypatch, tmp_path):
    rd, b, _rec, auth = _rerecite_fixture(monkeypatch, tmp_path)
    own = [{"reply_id": "r1", "call_index": 1, "step": "02_implement", "question": "Q?",
            "reply": "the answer", "pre_index": 7}]
    (b / "boundary" / "sim_replies.json").write_text(json.dumps(own))
    bj = json.loads((b / "boundary" / "boundary.json").read_text())
    bj["sim_calls"] = 1
    (b / "boundary" / "boundary.json").write_text(json.dumps(bj))
    later = own + [{"reply_id": "r2", "call_index": 2, "step": "02_implement",
                    "question": "Q2?", "reply": "from the future"}]
    (rd / "sim" / "replies.json").write_text(json.dumps(later))  # the run-wide list
    X.rerecite_run(rd, auth_path=auth, log=lambda s: None)
    got = json.loads((b / "boundary" / "sim_replies.json").read_text())
    assert [r["reply_id"] for r in got] == ["r1"]  # never a reply after the capture
    assert all(r["call_index"] <= 1 for r in got)


def test_rerecite_prefers_the_backup_replies_and_refuses_late_own_rows(monkeypatch, tmp_path):
    import pytest
    rd, b, _rec, auth = _rerecite_fixture(monkeypatch, tmp_path)
    bj = json.loads((b / "boundary" / "boundary.json").read_text())
    bj["sim_calls"] = 1
    (b / "boundary" / "boundary.json").write_text(json.dumps(bj))
    clean = [{"reply_id": "r1", "call_index": 1, "step": "02_implement", "question": "Q?",
              "reply": "the answer", "pre_index": 3, "pre_index_mismatch": 9}]
    dirty = clean + [{"reply_id": "r2", "call_index": 2, "step": "02_implement",
                      "question": "Q2?", "reply": "later"}]
    X._backup(b)
    (b / "native" / "orig" / "sim_replies.json").write_text(json.dumps(clean))
    (b / "boundary" / "sim_replies.json").write_text(json.dumps(dirty))  # contaminated
    X.rerecite_run(rd, auth_path=auth, log=lambda s: None)
    got = json.loads((b / "boundary" / "sim_replies.json").read_text())
    assert [r["reply_id"] for r in got] == ["r1"]  # the backup, not the contaminated file
    assert "pre_index_mismatch" not in got[0] or got[0]["pre_index_mismatch"] != 9
    (b / "native" / "orig" / "sim_replies.json").write_text(json.dumps(dirty))
    with pytest.raises(RuntimeError):  # its own rows run past sim_calls
        X.rerecite_run(rd, auth_path=auth, log=lambda s: None)
