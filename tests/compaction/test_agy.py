"""Tests for compaction.agy, on redacted fixtures from real agy v1.4.2 runs (no live calls).

A fake `agy` executable replays fixture output, so run_turn's process handling is tested end
to end. agy_error_synthetic.stderr is hand-written from the changelog's description of
AGY_ERROR, and cli_timeout_synthetic.stderr from its description of the --print-timeout warning;
neither was captured.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from compaction import agy

FIX = Path(__file__).parent / "fixtures" / "agy"
BRAIN = FIX / "brain"
CONV = "0b91b1d6-f056-495f-8f0a-199ea70be0c5"

FAKE = """#!{py}
import json, os, subprocess, sys, time
e = os.environ
if sys.argv[1:] == ["--version"]:
    print(e.get("FAKE_VERSION", "1.4.3-fake"))
    sys.exit(0)
if e.get("FAKE_ISOLATION"):  # "block" or "leak": simulate the sandbox check
    prompt = sys.argv[sys.argv.index("-p") + 1]
    paths = dict(
        ln.split(": ", 1) for ln in prompt.splitlines() if ln.startswith(("INSIDE: ", "OUTSIDE: "))
    )
    inside = open(paths["INSIDE"]).read().strip()
    leak = e["FAKE_ISOLATION"] == "leak"
    outside = open(paths["OUTSIDE"]).read().strip() if leak else "CANNOT_READ"
    conv = "iso-conv"
    d = os.path.join(e["FAKE_BRAIN"], conv, ".system_generated", "logs")
    os.makedirs(d, exist_ok=True)
    calls = [{{"name": "read_file", "args": {{"Path": paths[k]}}}} for k in ("INSIDE", "OUTSIDE")]
    denied = "permission check failed for read_file " + paths["OUTSIDE"]
    tr = [
        {{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": prompt}},
        {{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "tool_calls": calls}},
        {{"step_index": 2, "source": "MODEL", "type": "GENERIC", "content": inside}},
        {{"step_index": 3, "source": "MODEL", "type": "GENERIC", "status": "ERROR",
         "content": outside if leak else denied}},
    ]
    if e["FAKE_ISOLATION"] == "none":  # the model never tries the reads
        tr = tr[:1]
    with open(os.path.join(d, "transcript_full.jsonl"), "w") as f:
        f.write("".join(json.dumps(x) + "\\n" for x in tr))
    # like agy 1.4.3 on a denied read: the run ends with an empty response
    res = {{"conversation_id": conv, "status": "SUCCESS", "response": outside if leak else ""}}
    print(json.dumps({{"event": "result", "result": res}}))
    sys.exit(0)
if e.get("FAKE_ARGS_OUT"):
    with open(e["FAKE_ARGS_OUT"], "w") as f:
        json.dump({{"argv": sys.argv[1:], "env": sorted(e), "cwd": os.getcwd()}}, f)
if e.get("FAKE_CHILD_PID"):
    pidf = e["FAKE_CHILD_PID"]
    # the child writes its pid only once its SIGTERM handler is in place
    code = (
        "import os, signal, sys, time\\n"
        "if os.environ.get('FAKE_CHILD_IGNORE_TERM'):\\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\\n"
        "time.sleep(60)\\n"
    )
    subprocess.Popen([sys.executable, "-c", code, pidf])
    for _ in range(200):
        if os.path.exists(pidf) and open(pidf).read():
            break
        time.sleep(0.05)
if e.get("FAKE_SLEEP"):
    time.sleep(float(e["FAKE_SLEEP"]))
if e.get("FAKE_STDOUT"):
    sys.stdout.write(open(e["FAKE_STDOUT"]).read())
if e.get("FAKE_STDERR"):
    sys.stderr.write(open(e["FAKE_STDERR"]).read())
sys.exit(int(e.get("FAKE_RC", "0")))
"""


def open_gate(agy_bin: Path, log: Path, result: str = "pass", **settings) -> None:
    """Record an isolation verdict for `agy_bin` (default settings unless overridden)."""
    rec = {
        **agy.agy_identity(agy_bin),
        "sandbox": True,
        "permission_mode": "accept-edits",
        "skip_permissions": False,
        **settings,
        "result": result,
    }
    with log.open("a") as f:
        f.write(json.dumps(rec) + "\n")


@pytest.fixture
def iso_log(tmp_path: Path, monkeypatch) -> Path:
    log = tmp_path / "isolation_checks.jsonl"
    monkeypatch.setattr(agy, "ISOLATION_LOG", log)
    return log


@pytest.fixture
def fake_agy(tmp_path: Path, iso_log: Path) -> Path:
    """A fake agy binary, with a recorded isolation pass so run_turn's gate is open."""
    p = tmp_path / "agy"
    p.write_text(FAKE.format(py=sys.executable))
    p.chmod(0o755)
    open_gate(p, iso_log)
    return p


@pytest.fixture
def closed_agy(tmp_path: Path, iso_log: Path) -> Path:
    """The same fake agy, with no isolation verdict recorded."""
    p = tmp_path / "agy_closed"
    p.write_text(FAKE.format(py=sys.executable) + "# closed\n")
    p.chmod(0o755)
    return p


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    return agy.make_workspace(tmp_path / "agy-root")


def steps() -> list[dict]:
    return agy.read_transcript(CONV, BRAIN)


# ------------------------------------------------------------ parsing


def test_parse_json_turn():
    res, events = agy.parse_stdout((FIX / "json_turn.json").read_text(), stream=False)
    assert events is None
    assert res["conversation_id"] == CONV and res["status"] == "SUCCESS"
    assert res["usage"]["input_tokens"] == 13523


def test_parse_stream_turn():
    res, events = agy.parse_stdout((FIX / "stream_turn.ndjson").read_text(), stream=True)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "init" and kinds[-1] == "result" and "step_update" in kinds
    assert res["num_turns"] == 3 and res["usage"]["output_tokens"] == 232


def test_parse_agy_error():
    err = agy.parse_agy_error((FIX / "agy_error_synthetic.stderr").read_text())
    assert err["code"] == 429 and err["retryable"] is True
    assert agy.parse_agy_error("AGY_ERROR: not json")["message"] == "not json"
    assert agy.parse_agy_error("all fine") is None


# ------------------------------------------------------------ usage


def test_usage_totals_match_transcript_sum():
    """agy's conversation totals (run4: 28546 in / 233 out) equal the per-step sums."""
    tot = agy.usage_from_transcript(steps())
    assert tot["input_tokens"] == 28546 and tot["output_tokens"] == 233


def test_usage_delta():
    before = {"input_tokens": 13523, "output_tokens": 186}
    after = {"input_tokens": 20905, "output_tokens": 232, "thinking_tokens": 0}
    d = agy.usage_delta(after, before)
    assert d == {
        "input_tokens": 7382,
        "output_tokens": 46,
        "thinking_tokens": 0,
        "cache_read_tokens": 0,
    }
    assert agy.usage_delta(after, None) is None and agy.usage_delta(None, before) is None
    assert agy.usage_delta(before, after) is None  # decreasing totals: no negative counts


# ------------------------------------------------------------ transcript and checkpoints


def _write(tmp_path: Path, conv: str, steps_or_lines: list) -> Path:
    t = agy.transcript_path(conv, tmp_path)
    t.parent.mkdir(parents=True, exist_ok=True)
    lines = [x if isinstance(x, str) else json.dumps(x) for x in steps_or_lines]
    t.write_text("".join(ln + "\n" for ln in lines))
    return t


@pytest.mark.parametrize("where", ["middle", "final"])
def test_corrupt_transcript_raises_by_default(tmp_path: Path, where):
    good = [json.dumps(x) for x in steps()]
    lines = good[:3] + ["{broken"] + good[4:] if where == "middle" else good[:-1] + ['{"step']
    _write(tmp_path, "c1", lines)
    with pytest.raises(agy.TranscriptCorrupt) as e:
        agy.read_transcript("c1", tmp_path)
    assert any(("final line" in p) == (where == "final") for p in e.value.problems)
    with pytest.raises(agy.TranscriptCorrupt):
        agy.records_for("c1", brain_dir=tmp_path)


def test_corrupt_user_rule_line_excludes_record(tmp_path: Path):
    """The review's case: the line holding the user's rule is malformed. The record must not
    come back as a clean one with that rule silently missing."""
    good = [json.dumps(x) for x in steps()]
    _write(tmp_path, "c1", ["{not json"] + good[1:])
    s = agy.read_transcript("c1", tmp_path, strict=False)
    assert s[0]["_corrupt"] and s[0]["line"] == 1
    excluded: list[dict] = []
    recs = agy.records_for("c1", brain_dir=tmp_path, strict=False, excluded=excluded)
    assert recs == []
    assert excluded[0]["id"] == "agy:c1:0" and "line 1: malformed JSON" in excluded[0]["reasons"]


def test_corrupt_final_line_excludes_record(tmp_path: Path):
    good = [json.dumps(x) for x in steps()]
    _write(tmp_path, "c1", good + ['{"step_index": 13, "type": "USER_IN'])
    excluded: list[dict] = []
    recs = agy.records_for("c1", brain_dir=tmp_path, strict=False, excluded=excluded)
    assert recs == [] and excluded[0]["reasons"] == ["line 14: malformed JSON"]


def test_missing_checkpoint_line_is_a_gap_not_a_merge(tmp_path: Path):
    """A silently dropped CHECKPOINT (no malformed line left behind) shows as a step_index gap,
    so two compaction intervals are not merged into one clean record."""
    s = steps()
    extra = [
        {"step_index": 13, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "go on"},
        # 14, the second CHECKPOINT, is missing
        {"step_index": 15, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "done"},
        {"step_index": 16, "source": "SYSTEM", "type": "CHECKPOINT", "content": "third"},
    ]
    with pytest.raises(agy.TranscriptCorrupt, match="jumps 13 -> 15"):
        agy.checkpoints(s + extra, conversation_id="c")
    excluded: list[dict] = []
    recs = agy.checkpoints(s + extra, conversation_id="c", on_corrupt="exclude", excluded=excluded)
    assert recs == [] and [x["id"] for x in excluded] == ["agy:c:0", "agy:c:1"]


def test_missing_first_steps_flagged():
    with pytest.raises(agy.TranscriptCorrupt, match="steps 0..1 missing"):
        agy.checkpoints(steps()[2:], conversation_id="c")


def test_clean_records_kept_next_to_excluded_ones(tmp_path: Path):
    s = steps()
    extra = [
        {"step_index": 13, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "go on"},
        {"step_index": 14, "source": "SYSTEM", "type": "CHECKPOINT", "content": "second"},
        {"step_index": 15, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "a"},
        {"step_index": 16, "source": "SYSTEM", "type": "CHECKPOINT", "content": "third"},
    ]
    lines = [json.dumps(x) for x in s + extra]
    lines.insert(-1, "{oops")  # corrupt line between the second and third checkpoints
    _write(tmp_path, "c1", lines)
    excluded: list[dict] = []
    recs = agy.records_for("c1", brain_dir=tmp_path, strict=False, excluded=excluded)
    assert [r["id"] for r in recs] == ["agy:c1:0"]
    assert [x["id"] for x in excluded] == ["agy:c1:1", "agy:c1:2"]


def test_checkpoint_record():
    recs = agy.checkpoints(steps(), conversation_id=CONV, model="gemini-3.8-flash-low")
    assert len(recs) == 1
    r = recs[0]
    assert r["id"] == f"agy:{CONV}:0" and r["source"] == "agy" and r["harness"] == "antigravity"
    assert r["trigger"] == "manual" and r["session_id"] == CONV
    assert r["summary"].startswith("# Resuming from a compaction") and "<summary>" in r["summary"]
    # source: steps 0-5 (user, tool call, tool output, answer, /compact, system notice)
    roles = [m["role"] for m in r["source_messages"]]
    assert roles == ["user", "assistant", "tool", "assistant", "user", "system"]
    assert r["source_messages"][1]["tool_calls"][0]["name"] == "write_file"
    assert r["user_turns"] == [
        "Create a file hello.txt containing the word hi, then stop.",
        "/compact",
    ]
    assert r["pre_tokens"] == 6848 + 89
    # post: steps 7-12
    assert len(r["post_messages"]) == 6 and r["post_messages"][-1]["text"] == "ok"
    assert r["post_messages"][0]["ts"] == "2026-10-20T06:08:13Z"
    # the 16 core fields, up to `scrubbed` (the optional completeness fields after it belong to
    # the public-set unit; __required_keys__ is unreliable under string annotations)
    fields = list(agy.CompactionRecord.__annotations__)
    assert set(r) == set(fields[: fields.index("scrubbed") + 1])


def test_two_checkpoints_split_at_previous():
    s = steps()
    extra = [
        {"step_index": 13, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "go on"},
        {"step_index": 14, "source": "SYSTEM", "type": "CHECKPOINT", "content": "second"},
        {"step_index": 15, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "done"},
    ]
    recs = agy.checkpoints(s + extra, conversation_id=CONV)
    assert [r["id"][-1] for r in recs] == ["0", "1"]
    second = recs[1]
    # the earlier checkpoint is the first thing the second summary replaced
    assert second["source_messages"][0]["text"].startswith("# Resuming from a compaction")
    assert second["trigger"] == "auto" and second["user_turns"][-1] == "go on"
    assert [m["text"] for m in second["post_messages"]] == ["done"]
    # the first record's post messages stop at the second checkpoint
    assert recs[0]["post_messages"][-1]["text"] == "go on"


# ------------------------------------------------------------ run_turn with a fake agy


def test_run_turn_json(fake_agy, ws, tmp_path, monkeypatch):
    args = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_turn.json"))
    monkeypatch.setenv("FAKE_ARGS_OUT", str(args))
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("HF_TOKEN", "x")
    monkeypatch.setenv("BASH_ENV", "/tmp/x.sh")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/tmp/x.dylib")
    r = agy.run_turn("hi", workspace=ws, timeout=150, agy_bin=fake_agy)
    assert r["status"] == "SUCCESS" and r["conversation_id"] == CONV
    assert r["usage"]["input_tokens"] == 13523 and r["events"] is None
    seen = json.loads(args.read_text())
    assert seen["argv"][:2] == ["-p", "hi"]
    assert "--print-timeout" in seen["argv"] and "150s" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--mode") + 1] == "accept-edits"
    assert Path(seen["cwd"]).resolve() == ws.resolve()
    assert "OPENROUTER_API_KEY" not in seen["env"] and "HF_TOKEN" not in seen["env"]
    assert "BASH_ENV" not in seen["env"] and "DYLD_INSERT_LIBRARIES" not in seen["env"]
    assert "--sandbox" in seen["argv"]  # on by default
    assert r["outcome"] == "complete" and not r["timed_out"]
    assert r["usage_status"] == "measured"  # a new conversation starts from zero


def test_run_turn_resume_diffs_against_transcript(fake_agy, ws, tmp_path, monkeypatch):
    """A resumed turn's usage is the conversation total minus the transcript's totals."""
    out = tmp_path / "out.json"
    out.write_text(
        json.dumps(
            {
                "conversation_id": CONV,
                "status": "SUCCESS",
                "response": "x",
                "num_turns": 5,
                "usage": {"input_tokens": 30000, "output_tokens": 300},
            }
        )
    )
    monkeypatch.setenv("FAKE_STDOUT", str(out))
    r = agy.run_turn(
        "more", workspace=ws, conversation=CONV, agy_bin=fake_agy, brain_dir=BRAIN, timeout=10
    )
    assert r["usage"]["input_tokens"] == 30000 - 28546
    assert r["usage"]["output_tokens"] == 300 - 233
    assert r["usage_total"]["input_tokens"] == 30000 and r["usage_status"] == "measured"


def _result_file(tmp_path: Path, **kw) -> Path:
    out = tmp_path / "out.json"
    out.write_text(
        json.dumps({"conversation_id": CONV, "status": "SUCCESS", "response": "x", **kw})
    )
    return out


def test_run_turn_resume_without_baseline_is_unknown(fake_agy, ws, tmp_path, monkeypatch):
    """Resuming a conversation whose transcript is missing: the total is not this turn's usage."""
    out = _result_file(tmp_path, usage={"input_tokens": 30000, "output_tokens": 300})
    monkeypatch.setenv("FAKE_STDOUT", str(out))
    r = agy.run_turn("x", workspace=ws, conversation="nope", agy_bin=fake_agy, brain_dir=tmp_path)
    assert r["usage"] is None and r["usage_status"] == "no_baseline"
    assert r["usage_total"]["input_tokens"] == 30000


def test_run_turn_decreasing_totals_flagged(fake_agy, ws, tmp_path, monkeypatch):
    out = _result_file(tmp_path, usage={"input_tokens": 100, "output_tokens": 1})
    monkeypatch.setenv("FAKE_STDOUT", str(out))
    r = agy.run_turn("x", workspace=ws, conversation=CONV, agy_bin=fake_agy, brain_dir=BRAIN)
    assert r["usage"] is None and r["usage_status"] == "decreasing"


def test_run_turn_missing_usage_is_not_zero(fake_agy, ws, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STDOUT", str(_result_file(tmp_path)))
    r = agy.run_turn("x", workspace=ws, agy_bin=fake_agy)
    assert r["usage"] is None and r["usage_total"] is None and r["usage_status"] == "no_total"


def test_run_turn_cli_timeout_warning(fake_agy, ws, monkeypatch):
    """agy's --print-timeout path: partial output, exit 0, a warning (wording synthetic)."""
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_turn.json"))
    monkeypatch.setenv("FAKE_STDERR", str(FIX / "cli_timeout_synthetic.stderr"))
    r = agy.run_turn("x", workspace=ws, agy_bin=fake_agy)
    assert r["outcome"] == "cli_timeout" and r["timed_out"] and r["status"] == "SUCCESS"
    assert r["timeout_evidence"] == "agy_warning"


def test_run_turn_full_timeout_counts_as_timed_out(fake_agy, ws, monkeypatch):
    """No recognisable warning, but the turn used the whole --print-timeout."""
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_turn.json"))
    monkeypatch.setenv("FAKE_SLEEP", "1.1")
    r = agy.run_turn("x", workspace=ws, timeout=1, agy_bin=fake_agy)
    assert r["outcome"] == "timeout_inferred" and r["timed_out"]
    assert r["timeout_evidence"] == "elapsed"
    assert any("whole --print-timeout" in w for w in r["warnings"])


def test_cleanup_time_is_not_run_time(fake_agy, ws, tmp_path, monkeypatch):
    """r2: agy finishes at once; killing its SIGTERM-proof child takes longer than the timeout.
    That cleanup must not make the turn look timed out."""
    pidf = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PID", str(pidf))
    monkeypatch.setenv("FAKE_CHILD_IGNORE_TERM", "1")
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_turn.json"))
    monkeypatch.setattr(agy, "KILL_WAIT_S", 3.0)
    r = agy.run_turn("x", workspace=ws, timeout=2, agy_bin=fake_agy)
    assert r["outcome"] == "complete" and not r["timed_out"] and r["timeout_evidence"] is None
    assert r["duration"] < 2 and r["cleanup_s"] >= 2.5
    _assert_gone(int(pidf.read_text()))


def test_run_turn_stream(fake_agy, ws, monkeypatch):
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "stream_turn.ndjson"))
    r = agy.run_turn(
        "x", workspace=ws, conversation=CONV, stream=True, agy_bin=fake_agy, brain_dir=BRAIN
    )
    assert r["events"][0]["event"] == "init" and r["status"] == "SUCCESS"


def test_run_turn_error_exit_3(fake_agy, ws, monkeypatch):
    monkeypatch.setenv("FAKE_STDERR", str(FIX / "agy_error_synthetic.stderr"))
    monkeypatch.setenv("FAKE_RC", "3")
    r = agy.run_turn("x", workspace=ws, agy_bin=fake_agy)
    assert r["status"] == "ERROR" and r["error"]["code"] == 429
    assert r["warnings"] == ["warning: something minor"]


def test_run_turn_other_exit_raises(fake_agy, ws, monkeypatch):
    monkeypatch.setenv("FAKE_RC", "2")
    with pytest.raises(agy.AgyError) as e:
        agy.run_turn("x", workspace=ws, agy_bin=fake_agy)
    assert e.value.returncode == 2


def _assert_gone(pid: int) -> None:
    for _ in range(100):  # the child is reparented and reaped by init; wait for it to go
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(pid, 9)
    pytest.fail(f"child {pid} survived the process-group kill")


@pytest.mark.parametrize("ignore_term", [False, True])
def test_run_turn_timeout_kills_group(fake_agy, ws, tmp_path, monkeypatch, ignore_term):
    pidf = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PID", str(pidf))
    if ignore_term:
        monkeypatch.setenv("FAKE_CHILD_IGNORE_TERM", "1")
    monkeypatch.setenv("FAKE_SLEEP", "60")
    monkeypatch.setattr(agy, "KILL_GRACE_S", 0.5)
    monkeypatch.setattr(agy, "KILL_WAIT_S", 0.5)
    t0 = time.monotonic()
    r = agy.run_turn("x", workspace=ws, timeout=1, agy_bin=fake_agy)
    assert r["status"] == "TIMEOUT" and r["outcome"] == "hard_timeout" and r["timed_out"]
    assert r["timeout_evidence"] == "driver_kill"
    assert time.monotonic() - t0 < 15
    _assert_gone(int(pidf.read_text()))


def test_run_turn_parent_exits_child_lives(fake_agy, ws, tmp_path, monkeypatch):
    """agy exits at once but leaves a SIGTERM-proof child holding its stdout: the driver must
    neither hang on the output nor leave the child running."""
    pidf = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CHILD_PID", str(pidf))
    monkeypatch.setenv("FAKE_CHILD_IGNORE_TERM", "1")
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_turn.json"))
    monkeypatch.setattr(agy, "KILL_WAIT_S", 0.5)
    t0 = time.monotonic()
    r = agy.run_turn("x", workspace=ws, timeout=60, agy_bin=fake_agy)
    assert time.monotonic() - t0 < 15
    assert r["status"] == "SUCCESS" and r["outcome"] == "complete"
    assert any("outlived agy" in w and "SIGKILL" in w for w in r["warnings"])
    _assert_gone(int(pidf.read_text()))


@pytest.mark.parametrize("bad", ["home", "repo", "unmarked", "dotenv", "nested_dotenv", "link"])
def test_workspace_guard(bad, ws, tmp_path, fake_agy):
    target = ws
    if bad == "home":
        target = Path.home()
    elif bad == "repo":
        target = agy.REPO_ROOT
    elif bad == "unmarked":
        target = tmp_path / "plain"
        target.mkdir()
    elif bad == "dotenv":
        (ws / ".env").write_text("")
    elif bad == "nested_dotenv":
        (ws / "a" / "b").mkdir(parents=True)
        (ws / "a" / "b" / ".env.local").write_text("")
    elif bad == "link":
        (ws / "keys").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        agy.run_turn("x", workspace=target, agy_bin=fake_agy)


def test_workspace_inner_symlink_allowed(ws, fake_agy, monkeypatch):
    (ws / "a.txt").write_text("hi")
    (ws / "b.txt").symlink_to(ws / "a.txt")
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_turn.json"))
    assert agy.run_turn("x", workspace=ws, agy_bin=fake_agy)["status"] == "SUCCESS"


@pytest.mark.parametrize("bad", ["in_repo", "git_above", "dotenv_above", "home"])
def test_make_workspace_root_guard(bad, tmp_path):
    if bad == "in_repo":
        root = agy.REPO_ROOT / "data" / "raw" / "agy-ws-test"
    elif bad == "home":
        root = Path.home()
    else:
        (tmp_path / (".git" if bad == "git_above" else ".env")).mkdir()
        root = tmp_path / "sub" / "root"
    existed = root.exists()
    with pytest.raises(ValueError):
        agy.make_workspace(root)
    assert root.exists() == existed  # nothing created on refusal


def test_make_workspace_is_new_and_marked(tmp_path):
    a = agy.make_workspace(tmp_path / "r")
    b = agy.make_workspace(tmp_path / "r")
    assert a != b and (a / agy.WORKSPACE_MARKER).is_file() and (a / ".git").exists()
    assert agy.check_workspace(a) == a.resolve()
    with pytest.raises(FileExistsError):
        agy.make_workspace(tmp_path / "r", name=a.name)


def test_scrubbed_env():
    env = {
        "PATH": "/bin",
        "HOME": "/h",
        "OPENAI_API_KEY": "x",
        "BASH_ENV": "/x",
        "ENV": "/x",
        "ZDOTDIR": "/x",
        "LD_PRELOAD": "x",
        "DYLD_INSERT_LIBRARIES": "x",
        "BASH_FUNC_f%%": "() { :; }",
        "PYTHONSTARTUP": "/x",
        "NODE_OPTIONS": "--require x",
        "EM_DATASET_PASSWORD": "x",
    }
    assert agy.scrubbed_env(env) == {"PATH": "/bin", "HOME": "/h"}


def test_compact_returns_new_checkpoint(fake_agy, ws, tmp_path, monkeypatch):
    """compact() diffs checkpoints before and after; simulate agy appending one."""
    brain = tmp_path / "brain"
    t = agy.transcript_path("c2", brain)
    t.parent.mkdir(parents=True)
    pre = [s for s in steps() if s["step_index"] < 4]
    t.write_text("".join(json.dumps(s) + "\n" for s in pre))
    post = steps()[:7]  # what agy writes: + /compact, notice, CHECKPOINT
    staged = tmp_path / "after.jsonl"
    staged.write_text("".join(json.dumps(s) + "\n" for s in post))
    # the fake agy "compacts" by replacing the transcript
    wrapper = tmp_path / "agy2"
    wrapper.write_text(
        f"#!/bin/sh\n[ \"$1\" = --version ] || cp '{staged}' '{t}'\nexec '{fake_agy}' \"$@\"\n"
    )
    wrapper.chmod(0o755)
    open_gate(wrapper, agy.ISOLATION_LOG)
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_compact.json"))
    monkeypatch.setenv("FAKE_STDERR", str(FIX / "json_compact.stderr"))
    out = agy.compact("c2", ws, agy_bin=wrapper, brain_dir=brain)
    assert out["record"]["id"] == "agy:c2:0" and out["record"]["trigger"] == "manual"
    assert out["turn"]["usage"]["input_tokens"] == 0  # compaction's own call is not counted
    assert "no output" in out["turn"]["warnings"][0]


def test_compact_without_checkpoint_raises(fake_agy, ws, tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    t = agy.transcript_path("c3", brain)
    t.parent.mkdir(parents=True)
    t.write_text(json.dumps(steps()[0]) + "\n")
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_compact.json"))
    with pytest.raises(agy.AgyError):
        agy.compact("c3", ws, agy_bin=fake_agy, brain_dir=brain)


LIVE = "fe8f4f68-c47c-4ee5-a475-7d778720089e"  # 2026-10-20 validation run, redacted


def test_live_multistep_checkpoint():
    """Multi-step turn (parallel tool calls) then /compact, from the live validation run."""
    s = agy.read_transcript(LIVE, BRAIN)
    assert agy.usage_from_transcript(s)["input_tokens"] == 28824  # agy's reported total
    (r,) = agy.records_for(LIVE, model="gemini-3.8-flash-low", brain_dir=BRAIN)
    assert r["trigger"] == "manual" and r["pre_tokens"] == 7610 + 149
    calls = [c["name"] for m in r["source_messages"] for c in m.get("tool_calls", [])]
    assert calls == ["write_file", "write_file", "read_file", "edit_file", "write_file"]
    assert sum(m["role"] == "tool" for m in r["source_messages"]) == 5
    assert "never delete or rename notes.txt" in r["user_turns"][0]
    assert "Commitments & Constraints" in r["summary"]
    assert r["post_messages"] == []


# ------------------------------------------------------------ r2: isolation gate


def test_gate_refuses_without_a_verdict(closed_agy, ws, monkeypatch):
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_turn.json"))
    with pytest.raises(agy.IsolationNotVerified, match="no check recorded"):
        agy.run_turn("x", workspace=ws, agy_bin=closed_agy)


@pytest.mark.parametrize("case", ["fail", "other_settings", "other_binary", "fail_after_pass"])
def test_gate_needs_a_matching_pass(case, closed_agy, ws, iso_log, monkeypatch):
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_turn.json"))
    if case == "fail":
        open_gate(closed_agy, iso_log, result="fail")
    elif case == "other_settings":
        open_gate(closed_agy, iso_log, sandbox=False)  # a pass without --sandbox
    elif case == "other_binary":
        open_gate(closed_agy, iso_log)
        closed_agy.write_text(closed_agy.read_text() + "# changed\n")  # new sha256
    else:
        open_gate(closed_agy, iso_log)
        open_gate(closed_agy, iso_log, result="fail")  # the latest verdict counts
    with pytest.raises(agy.IsolationNotVerified):
        agy.run_turn("x", workspace=ws, agy_bin=closed_agy)


def test_gate_is_per_setting(fake_agy, ws, monkeypatch):
    """The pass was recorded with --sandbox and accept-edits; other settings stay closed."""
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_turn.json"))
    with pytest.raises(agy.IsolationNotVerified):
        agy.run_turn("x", workspace=ws, agy_bin=fake_agy, sandbox=False)
    with pytest.raises(agy.IsolationNotVerified):
        agy.run_turn("x", workspace=ws, agy_bin=fake_agy, skip_permissions=True)
    with pytest.raises(agy.IsolationNotVerified):
        agy.run_turn("x", workspace=ws, agy_bin=fake_agy, permission_mode="auto")


@pytest.mark.parametrize(
    "behaviour,expected", [("block", "pass"), ("leak", "fail"), ("none", "inconclusive")]
)
def test_verify_isolation(behaviour, expected, closed_agy, ws, iso_log, tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    brain = tmp_path / "brain"
    monkeypatch.setenv("FAKE_ISOLATION", behaviour)
    monkeypatch.setenv("FAKE_BRAIN", str(brain))
    rec = agy.verify_isolation(agy_bin=closed_agy, brain_dir=brain)
    assert rec["result"] == expected and rec["transcript_found"]
    assert rec["inside_seen"] == rec["outside_attempted"] == (behaviour != "none")
    assert rec["outside_seen"] == (behaviour == "leak")
    if behaviour == "block":
        assert "permission check failed" in rec["outside_steps"][0]
    assert rec["sandbox"] is True and rec["version"] == "1.4.3-fake"
    assert list(tmp.iterdir()) == []  # canary and workspace deleted
    assert json.loads(iso_log.read_text().splitlines()[-1])["result"] == expected
    monkeypatch.delenv("FAKE_ISOLATION")
    monkeypatch.setenv("FAKE_STDOUT", str(FIX / "json_turn.json"))
    if expected == "pass":
        assert agy.run_turn("x", workspace=ws, agy_bin=closed_agy)["status"] == "SUCCESS"
    else:
        with pytest.raises(agy.IsolationNotVerified):
            agy.run_turn("x", workspace=ws, agy_bin=closed_agy)


# ------------------------------------------------------------ r2: incomplete steps, baselines


@pytest.mark.parametrize(
    "step,change,reason",
    [
        (0, {"__replace__": {"step_index": 0}}, "missing type"),  # the review's reproduction
        (0, {"content": None}, "USER_INPUT without content"),
        (0, {"content": "   "}, "USER_INPUT without content"),
        (6, {"type": None}, "missing type"),
        (6, {"content": None}, "CHECKPOINT without content"),
        (1, {"tool_calls": [{"args": {}}]}, "malformed tool_calls"),
        (1, {"input_tokens": "many"}, "bad input_tokens"),
    ],
)
def test_incomplete_step_never_makes_a_clean_record(tmp_path, step, change, reason):
    s = steps()
    if "__replace__" in change:
        s[step] = change["__replace__"]
    else:
        s[step] = {k: v for k, v in {**s[step], **change}.items() if v is not None}
    _write(tmp_path, "c1", s)
    with pytest.raises(agy.TranscriptCorrupt, match=reason):
        agy.read_transcript("c1", tmp_path)
    with pytest.raises(agy.TranscriptCorrupt, match=reason):
        agy.checkpoints(s, conversation_id="c1")
    excluded: list[dict] = []
    recs = agy.records_for("c1", brain_dir=tmp_path, strict=False, excluded=excluded)
    assert all(r["id"] != "agy:c1:0" for r in recs)
    assert any(reason in why for why in excluded[0]["reasons"])


def test_valid_steps_pass_validation():
    assert all(agy.step_problems(s) == [] for s in steps())
    assert all(agy.step_problems(s) == [] for s in agy.read_transcript(LIVE, BRAIN))


@pytest.mark.parametrize("damage", ["gap", "empty", "starts_late"])
def test_resume_baseline_needs_a_complete_transcript(damage, fake_agy, ws, tmp_path, monkeypatch):
    s = steps()
    lines = {"gap": s[:1] + s[2:], "empty": [], "starts_late": s[1:]}[damage]
    brain = tmp_path / "brain"
    _write(brain, "c1", lines)
    out = _result_file(tmp_path, usage={"input_tokens": 90000, "output_tokens": 900})
    monkeypatch.setenv("FAKE_STDOUT", str(out))
    r = agy.run_turn("x", workspace=ws, conversation="c1", agy_bin=fake_agy, brain_dir=brain)
    assert r["usage"] is None and r["usage_status"] == "no_baseline"
