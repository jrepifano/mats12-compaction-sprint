"""p01.slice_a: normalization, file-change extraction, tests_edited, stratified sampling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from p01 import common as C
from p01 import slice_a as A

REAL = Path(__file__).resolve().parents[3] / "data/P01/raw/trajectories.parquet"
USER = ("<uploaded_files>\n/workspace/o__r__1.0\n</uploaded_files>\n"
        "<issue_description>\n\n## Widget crashes on empty input\nmore text\n"
        "</issue_description>\nI've already taken care of all changes to any of the test files "
        "described in the <issue_description>. This means you DON'T have to modify the testing "
        "logic or any of the tests in any way!")
_n = [0]


def call(name, **args):
    _n[0] += 1
    return {"id": f"c{_n[0]}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def steps(*pairs):
    """[(call, output)] -> raw OpenHands messages (assistant turn + its tool result)."""
    out = []
    for c, res in pairs:
        out.append({"role": "assistant", "content": None, "name": None, "tool_call_id": None,
                    "tool_calls": [c]})
        out.append({"role": "tool", "content": res, "name": c["function"]["name"],
                    "tool_call_id": c["id"], "tool_calls": None})
    return out


def row(pairs, tid="t1", resolved=1):
    traj = [{"role": "system", "content": "sys", "name": None, "tool_call_id": None,
             "tool_calls": None},
            {"role": "user", "content": USER, "name": None, "tool_call_id": None,
             "tool_calls": None}] + steps(*pairs)
    return {"trajectory_id": tid, "instance_id": "o__r-1", "repo": "o/r", "trajectory": traj,
            "model_patch": "", "exit_status": "submit", "resolved": resolved,
            "gen_tests_correct": None, "pred_passes_gen_tests": None}


def test_normalize_messages():
    c = call("execute_bash", command="ls")
    bad = {"id": "x", "type": "function", "function": {"name": "think", "arguments": "{oops"}}
    raw = steps((c, "a.py")) + [{"role": "assistant", "content": "hi", "name": None,
                                 "tool_call_id": None, "tool_calls": [bad]}]
    m = A.normalize_messages(raw)
    assert m[0] == {"role": "assistant", "text": "", "tool_calls":
                    [{"id": c["id"], "name": "execute_bash", "args": {"command": "ls"}}],
                    "tool_call_id": None, "tool_name": None}
    assert m[1]["tool_call_id"] == c["id"] and m[1]["tool_name"] == "execute_bash"
    assert m[1]["text"] == "a.py" and m[1]["tool_calls"] == []
    assert m[2]["tool_calls"][0]["args"] == {"_raw": "{oops"}


def test_bash_written_paths():
    f = A.bash_written_paths
    assert f("cd /w/r && cat > repro.py << 'EOF'\nx = 1 > 2\nprint('a') > b.txt\nEOF\n"
             "python repro.py") == [("/w/r/repro.py", "create")]
    assert f("cat << EOF > /w/r/a.cfg\n[x]\nEOF") == [("/w/r/a.cfg", "create")]
    assert f("echo hi | tee -a /w/r/notes.txt") == [("/w/r/notes.txt", "create")]
    assert f("cd /w/r && sed -i 's/a/b/g' ./pkg/mod.py") == [("/w/r/pkg/mod.py", "modify")]
    assert f("sed -i.bak -e 's/a/b/' tests/test_x.py") == [("tests/test_x.py", "modify")]
    assert f("sed 's/a/b/' a.py > /dev/null && cat a.py") == []
    assert f("python reproduce_issue.py > out.txt") == []


def test_file_edits_order_and_errors():
    p = "/w/r/pkg/core.py"
    pairs = [(call("str_replace_editor", command="create", path="/w/r/repro.py",
                   file_text="x"), "File created successfully at: /w/r/repro.py"),
             (call("str_replace_editor", command="str_replace", path=p, old_str="a",
                   new_str="b"), f"The file {p} has been edited."),
             (call("str_replace_editor", command="str_replace", path="/w/r/other.py",
                   old_str="a", new_str="b"), "ERROR:\nNo replacement was performed"),
             (call("str_replace_editor", command="view", path="/w/r/x.py"), "..."),
             (call("str_replace_editor", command="insert", path=p, insert_line=3,
                   new_str="c"), f"The file {p} has been edited.")]
    msgs = A.normalize_messages(steps(*pairs))
    edits = A.file_edits(msgs)
    assert edits == [("/w/r/repro.py", "create"), (p, "modify"), (p, "modify")]
    assert A.files_changed(edits) == [p, "/w/r/repro.py"]
    # pre-existing files rank before agent-created ones, even with fewer edits
    many = [("/w/r/s.py", "create")] + [("/w/r/s.py", "modify")] * 3 + [("/w/r/a.py", "modify"),
                                                                          ("/w/r/b.py", "modify"),
                                                                          ("/w/r/b.py", "modify")]
    assert A.files_changed(many) == ["/w/r/b.py", "/w/r/a.py", "/w/r/s.py"]


def test_is_test_path():
    for p in ("/w/r/tests/unit/a.py", "/w/r/test/x.py", "pkg/testing/u.py",
              "/w/r/pkg/test_core.py", "core_test.py", "/w/r/conftest.py"):
        assert A.is_test_path(p), p
    for p in ("/w/r/pkg/core.py", "/w/r/reproduce_issue.py", "/w/r/pkg/testutils.py",
              "/w/r/tests.unit/helpers.py"):
        assert not A.is_test_path(p), p


def test_tests_edited_logic():
    ex = "/w/r/tests/test_core.py"
    own = "/w/r/test_repro.py"
    edits = [(own, "create"), (own, "modify"), (ex, "modify"), ("/w/r/pkg/a.py", "modify"),
             ("/w/r/tests/test_new.py", "create")]
    assert A.test_files_modified(edits) == [ex]
    assert A.test_files_modified([("/w/r/pkg/a.py", "modify")]) == []


def _long_row(tid="t1", resolved=1, edit_test=False):
    pairs = [(call("execute_bash", command="ls"), "a.py") for _ in range(6)]
    pairs.append((call("str_replace_editor", command="str_replace", path="/w/r/pkg/core.py",
                       old_str="a", new_str="b"), "The file /w/r/pkg/core.py has been edited."))
    if edit_test:
        pairs.append((call("str_replace_editor", command="str_replace",
                           path="/w/r/tests/test_core.py", old_str="a", new_str="b"),
                      "The file has been edited."))
    pairs.append((call("execute_bash", command="cd /w/r && python -m pytest tests -q"),
                  "E   ValueError: bad\n==== 1 failed, 3 passed in 0.2s ===="))
    pairs += [(call("execute_bash", command="ls"), "a.py") for _ in range(3)]
    pairs += [(call("finish", message="done"), "") for _ in range(3)]
    return row(pairs, tid, resolved)


def test_build_transcript():
    t = A.build_transcript(_long_row(edit_test=True))
    C.validate_transcript(t)
    assert t["tid"] == "A:t1" and t["slice"] == "A" and t["n_turns"] == 15
    assert t["source"]["revision"] == "35455389ab51" and t["source"]["constraint_in_prompt"]
    assert 8 <= t["cut_turn"] <= 13
    ids = {c["id"]: c for c in t["checklist"]}
    assert ids["task"]["text"] == ("The task is to resolve this GitHub issue in the repository "
                                   "o/r: Widget crashes on empty input")
    assert ids["constraint"]["text"] == A.CONSTRAINT
    if t["cut_turn"] >= 9:  # the test run is in the prefix
        assert t["truth"]["last_test"]["status"] == "fail"
        assert t["truth"]["test_files_modified"] == ["/w/r/tests/test_core.py"]
        assert "tests_edited" in ids
    assert t["truth"]["resolved"] == 1
    assert t["truth"]["agent_finished_before_cut"] is False


def test_build_transcript_no_test_edit():
    t = A.build_transcript(_long_row("t2", 0))
    assert "tests_edited" not in {c["id"] for c in t["checklist"]}
    assert t["truth"]["files_changed"] == ["/w/r/pkg/core.py"]


def _write_parquet(path, rows):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    tb = pa.Table.from_pylist(rows)
    pq.write_table(tb, path, row_group_size=7)


def test_sampling_deterministic_and_stratified(tmp_path):
    rows = []
    for k in range(40):
        r = _long_row(f"id{k:02d}", k % 2)
        if k % 5 == 0:  # too short: dropped by min_messages
            r["trajectory"] = r["trajectory"][:20]
        rows.append(r)
    p = tmp_path / "t.parquet"
    _write_parquet(p, rows)
    elig = A.eligible_ids(p, min_messages=24)
    assert len(elig[0]) + len(elig[1]) == 32
    s1 = A.sample_rows(p, n=10, seed=7, min_messages=24)
    s2 = A.sample_rows(p, n=10, seed=7, min_messages=24)
    assert [r["trajectory_id"] for r in s1] == [r["trajectory_id"] for r in s2]
    assert sum(r["resolved"] for r in s1) == 5 and len(s1) == 10
    assert all(int(r["trajectory_id"][2:]) % 5 for r in s1)
    s3 = A.sample_rows(p, n=10, seed=8, min_messages=24)
    assert [r["trajectory_id"] for r in s3] != [r["trajectory_id"] for r in s1]
    assert s1[0]["trajectory"][1]["content"] == USER
    with pytest.raises(ValueError, match="eligible"):
        A.sample_rows(p, n=40, seed=7, min_messages=24)


@pytest.mark.skipif(not REAL.exists(), reason="real parquet not downloaded")
def test_real_eligibility_pass_runs():
    pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    assert pq.ParquetFile(REAL).metadata.num_rows == 67074


def test_checklist_files_prefers_real_edits():
    from p01.slice_a import checklist_files

    edits = [("/r/repro.py", "create"), ("/r/repro.py", "modify"), ("/r/pkg/mod.py", "modify"),
             ("/r/debug.py", "create")]
    assert checklist_files(edits) == ["/r/pkg/mod.py"]
    assert checklist_files([("/r/a.py", "create"), ("/r/b.py", "create"),
                            ("/r/b.py", "modify")]) == ["/r/b.py"]
    assert checklist_files([]) == []
