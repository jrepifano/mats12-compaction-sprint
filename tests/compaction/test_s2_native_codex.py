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
    def __init__(self):
        self.files = {}

    def put_file(self, data, path):
        self.files[path] = data


def _scan_env(monkeypatch, result):
    from compaction.s2.native import common as N
    seen = {}

    def fake(sb, cmd, env, *, timeout_s=300, workdir="/"):
        seen.update(cmd=cmd, env=env)
        return result

    monkeypatch.setattr(N, "exec_env", fake)
    monkeypatch.setattr(X, "changed_files", lambda sb: ["/app/a.py", "/root/.x/leak"])
    return seen


def test_secret_scan_one_grep_values_only_in_env(monkeypatch):
    from compaction.deepswe import sandbox as SB
    seen = _scan_env(monkeypatch, SB.ExecResult(0, "/root/.x/leak\n", "", 0.1))
    sb = _SB()
    assert X.secret_scan(sb, ["s" * 30, "t" * 30]) == ["/root/.x/leak"]
    assert "s" * 30 not in seen["cmd"] and "t" * 30 not in seen["cmd"]
    assert sorted(seen["env"].values()) == ["s" * 30, "t" * 30]
    assert seen["cmd"].count('-e "$') == 2
    assert sb.files[f"{X.SECRETS_C}/scan-list"] == b"/app/a.py\0/root/.x/leak\0"


def test_secret_scan_fails_closed(monkeypatch):
    import pytest

    from compaction.deepswe import sandbox as SB
    _scan_env(monkeypatch, SB.ExecResult(0, "", "__GREP_ERR 2\n", 0.1))
    with pytest.raises(SB.SandboxError):
        X.secret_scan(_SB(), ["s" * 30])
    _scan_env(monkeypatch, SB.ExecResult(123, "", "", 0.1))
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
