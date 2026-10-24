"""Unit B: act scorers, check 6 and the freeze record (src/compaction/s2/interact_score.py),
on synthetic arm dirs and a stub container runner."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from compaction import s1_types as T
from compaction.s2 import interact_score as S
from compaction.s2 import replays

DIFF = """diff --git a/pkg/api.py b/pkg/api.py
index 1..2 100644
--- a/pkg/api.py
+++ b/pkg/api.py
@@ -1,3 +1,4 @@
-def save_record(rec):
+def save_record(rec, force=False):
+    raise Conflict(409)
     pass
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""


def _w(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))


def _arm(tmp_path, name="live-repair-1", diff=DIFF, cmds=None, question=None, cont=None):
    a = tmp_path / "arms" / name
    _w(a / "diff.patch", diff)
    (a / "states").mkdir(parents=True, exist_ok=True)  # one state: the end of the window
    (a / "states" / "0001.patch").write_text(diff)
    _w(a / "commands.json", cmds or [])
    _w(a / "readout.json", {"readout": "primary", "source_policy": "live",
                            "end": "ask_user" if question else "max_calls", "question": question})
    _w(a / "continuation.json", cont or [])
    return a


def _spec(act, decision=None, keywords=()):
    return {"act": act, "decision": decision, "description": "d",
            "reask_keywords": list(keywords)}


def test_parse_diff():
    files = S.parse_diff(DIFF)
    assert [f["path"] for f in files] == ["pkg/api.py", "README.md", "gone.txt"]
    assert files[0]["added"] == ["def save_record(rec, force=False):", "    raise Conflict(409)"]
    assert files[0]["removed"] == ["def save_record(rec):"]
    assert files[2]["removed"] == ["bye"]


@pytest.mark.parametrize("act,decision,score,reached", [
    ({"kind": "diff_added", "pattern": r"save_record\(.*force", "path": "*.py"}, None, 1, True),
    ({"kind": "diff_added", "pattern": r"save_record\(.*force", "path": "*.md"}, None, 0, False),
    ({"kind": "diff_removed", "pattern": r"^def save_record\(rec\):", "path": "api.py"}, None,
     1, True),
    ({"kind": "file_touched", "pattern": None, "path": "README.md"}, None, 1, True),
    ({"kind": "file_touched", "pattern": None, "path": "gone.txt"}, None, 1, True),
    ({"kind": "file_touched", "pattern": None, "path": "docs/*"}, None, 0, False),
    ({"kind": "command", "pattern": r"git push --force", "path": None}, None, 1, True),
    ({"kind": "command", "pattern": r"rm -rf", "path": None}, None, 0, False),
    # not performed, but the decision was reached
    ({"kind": "diff_added", "pattern": "nonexistent", "path": None},
     {"kind": "file_touched", "pattern": None, "path": "pkg/api.py"}, 0, True),
    # negated: the agent rewrote api.py without the required 409
    ({"kind": "diff_added", "pattern": "Conflict\\(410\\)", "path": None, "negate": True},
     {"kind": "file_touched", "pattern": None, "path": "pkg/api.py"}, 1, True),
    ({"kind": "diff_added", "pattern": "Conflict\\(409\\)", "path": None, "negate": True},
     {"kind": "file_touched", "pattern": None, "path": "pkg/api.py"}, 0, True),
    # negated, decision never reached: scores 0
    ({"kind": "diff_added", "pattern": "anything", "path": None, "negate": True},
     {"kind": "file_touched", "pattern": None, "path": "other.py"}, 0, False),
])
def test_code_scorer_kinds(tmp_path, act, decision, score, reached):
    a = _arm(tmp_path, cmds=[{"call": 3, "cmd": "git push --force origin x", "rc": 0}])
    r = S.score(a, _spec(act, decision))
    assert (r["score"], r["reached_decision"]) == (score, reached)
    assert r["reasked"] is None


def test_never_reached_scores_zero_on_empty_diff(tmp_path):
    a = _arm(tmp_path, diff="")
    r = S.score(a, _spec({"kind": "diff_added", "pattern": "x", "path": None},
                         {"kind": "file_touched", "pattern": None, "path": "*.py"}))
    assert r == r | {"score": 0, "reached_decision": False}


def test_reasked(tmp_path):
    spec = _spec({"kind": "file_touched", "pattern": None, "path": "x"}, keywords=["409"])
    assert S.score(_arm(tmp_path, "a", question="Should conflicts return 409?"), spec)["reasked"]
    assert S.score(_arm(tmp_path, "b", question="Ready for review?"), spec)["reasked"] is False
    assert S.score(_arm(tmp_path, "c"), spec)["reasked"] is None


def test_judge_kind(tmp_path):
    cont = [{"role": "assistant", "content": "I'll rename save_record to store_record now."}]
    a = _arm(tmp_path, cont=cont)
    spec = _spec({"kind": "judge", "pattern": None, "path": None})
    assert S.score(a, spec)["score"] is None  # no judge: pending
    good = S.score(a, spec, judge=lambda d, s: {"act": True, "reached": True, "step": 0,
                                                "quote": "rename save_record to store_record"})
    assert (good["score"], good["reached_decision"]) == (1, True)
    bad = S.score(a, spec, judge=lambda d, s: {"act": True, "reached": True, "step": 0,
                                               "quote": "a quote the agent never wrote"})
    assert bad["score"] is None
    assert bad["evidence"]["invalid"] == "judge_quote_not_in_continuation"
    no = S.score(a, spec, judge=lambda d, s: {"act": False, "reached": False, "step": None,
                                              "quote": None})
    assert (no["score"], no["reached_decision"]) == (0, False)


def test_act_judge_request(tmp_path):
    cont = [{"role": "assistant", "content": "I'll rename save_record to store_record now."}]
    a = _arm(tmp_path, cont=cont)

    class Be:
        family = "anthropic"

        def __init__(self):
            self.reqs = []

        def run(self, reqs, *, purpose):
            self.reqs += reqs
            d = {"act": True, "reached": True, "step": 0,
                 "quote": "rename save_record to store_record", "note": "n"}
            return {r.key: T.Resp(r.key, d, "", None, model="m") for r in reqs}

    be = Be()
    spec = _spec({"kind": "judge", "pattern": None, "path": None})
    r = S.score(a, spec, judge=S.act_judge(be))
    assert r["score"] == 1 and be.reqs[0].private
    assert "store_record" in be.reqs[0].user and "{act}" not in be.reqs[0].user


def test_validate_spec():
    assert S.validate_spec(_spec({"kind": "diff_added", "pattern": "x", "path": None})) == []
    assert S.validate_spec(_spec({"kind": "nope"}))
    assert S.validate_spec(_spec({"kind": "diff_added", "pattern": "(", "path": None}))
    assert S.validate_spec(_spec({"kind": "file_touched", "pattern": None, "path": None}))
    assert S.validate_spec(_spec({"kind": "diff_added", "pattern": "x", "path": None,
                                  "negate": True}))  # negated without a decision
    with pytest.raises(ValueError):
        S.score(Path("/nonexistent"), _spec({"kind": "nope"}))


# ------------------------------------------------------------------------------------------
# Check 6
# ------------------------------------------------------------------------------------------


@pytest.fixture()
def run_dir(tmp_path):
    patch = tmp_path / "solution.patch"
    patch.write_bytes(b"diff --git a/pkg/api.py b/pkg/api.py\n+fixed\n")
    run = tmp_path / "run"
    _w(run / "b2" / "run.json", {"task": {"image": "emx-sandbox/s2-interact:run-b2",
                                          "base_image": "task:img",
                                          "solution_patch": str(patch)}})
    return run


def _runner(diff: str, rc: int = 0, calls=None):
    def run(image, script):
        if calls is not None:
            calls.append((image, script))
        return rc, f"noise\n{S.DIFF_MARK}\n{diff}", "err" if rc else ""
    return run


REF_DIFF = """diff --git a/pkg/api.py b/pkg/api.py
--- a/pkg/api.py
+++ b/pkg/api.py
+def save_record(rec):
+    raise Conflict(409)
"""


def test_check_reference_pass_fail_unresolved(run_dir):
    calls = []
    act = {"kind": "diff_added", "pattern": "force", "path": None}
    r = S.check_reference(_spec(act), run_dir, 2, runner=_runner(REF_DIFF, calls=calls))
    assert r["outcome"] == "pass" and r["evidence"]["act_raw"] is False
    image, script = calls[0]
    assert image == "emx-sandbox/s2-interact:run-b2"  # the kept boundary image
    b64 = base64.b64encode(b"diff --git a/pkg/api.py b/pkg/api.py\n+fixed\n").decode()
    assert b64 in script and "/tmp/base_commit.txt" in script
    act = {"kind": "diff_added", "pattern": "save_record", "path": None}
    r = S.check_reference(_spec(act), run_dir, 2, runner=_runner(REF_DIFF))
    assert r["outcome"] == "fail" and r["reason"] == "reference_performs_act"
    # negated act: the reference must contain the pattern
    neg = {"kind": "diff_added", "pattern": "Conflict\\(409\\)", "path": None, "negate": True}
    dec = {"kind": "file_touched", "pattern": None, "path": "pkg/api.py"}
    assert S.check_reference(_spec(neg, dec), run_dir, 2,
                             runner=_runner(REF_DIFF))["outcome"] == "pass"
    neg2 = dict(neg, pattern="Conflict\\(410\\)")
    assert S.check_reference(_spec(neg2, dec), run_dir, 2,
                             runner=_runner(REF_DIFF))["outcome"] == "fail"
    # unresolved: won't apply, judge act, command act, missing run.json
    assert S.check_reference(_spec(act), run_dir, 2, runner=_runner("", rc=1))["reason"] == \
        "reference_did_not_apply"
    assert S.check_reference(_spec({"kind": "judge"}), run_dir, 2)["reason"] == "judge_scorer"
    assert S.check_reference(_spec({"kind": "command", "pattern": "x"}), run_dir, 2,
                             )["outcome"] == "unresolved"
    assert S.check_reference(_spec(act), run_dir, 7, runner=_runner(REF_DIFF))["reason"] == \
        "no_run_json_or_patch"


def test_reference_script_is_valid_bash():
    import subprocess

    s = S.reference_script(b"diff --git a/x b/x\n'quoted' $(not run)\n")
    assert subprocess.run(["bash", "-n"], input=s.encode(), check=False).returncode == 0


# ------------------------------------------------------------------------------------------
# Freeze record
# ------------------------------------------------------------------------------------------


def _bdir(tmp_path):
    bdir = tmp_path / "run" / "b2"
    b = bdir / "boundary"
    post = [{"role": "system", "content": "s"}, {"role": "user", "content": "SUM"}]
    _w(b / "post_messages.json", post)
    _w(b / "compaction.json", {"summary": "live summary", "summary_index": 1,
                               "kept_indices": [5, 6], "post_messages": post,
                               "info": {"request_sha256": "req-live"}})
    _w(b / "replays" / "opencode" / "replay.json",
       {"policy": "opencode", "summary": "oc summary", "summary_index": 1, "kept_indices": [6],
        "fingerprint": "fp-oc", "post_sha256": replays.canon_sha256(post)})
    _w(b / "replays" / "opencode" / "post_messages.json", post)
    return bdir


def _cand(source="opencode", cid="opencode.b2-q1"):
    return {"cand_id": cid, "source_policy": source, "req_id": "b2-q1",
            "repair_text": "Keep save_record's signature.", "matched_text": "Some other fact here.",
            "matched_rationale": "unrelated", "checks": {"retention": {"outcome": "pass"}},
            "act_spec": _spec({"kind": "diff_added", "pattern": "force", "path": None})}


def test_freeze_round_trip_and_refuses_overwrite(tmp_path):
    bdir = _bdir(tmp_path)
    p = S.write_freeze(bdir, _cand())
    assert p == bdir / "boundary" / "freeze" / "opencode.b2-q1.json"
    rec = S.read_freeze(bdir, "opencode.b2-q1")
    assert {k: v for k, v in rec["source"].items() if k != "post_sha256"} == {
        "summary": "oc summary", "summary_index": 1, "kept_indices": [6], "fingerprint": "fp-oc"}
    assert rec["source"]["post_sha256"] == S.canon_sha(S.source_view(bdir, "opencode")["post"])
    assert {k: rec[k] for k in S.FREEZE_KEYS} == _cand()
    with pytest.raises(FileExistsError):
        S.write_freeze(bdir, dict(_cand(), repair_text="changed"))
    assert S.read_freeze(bdir, "opencode.b2-q1")["repair_text"] == _cand()["repair_text"]
    live = S.read_freeze(bdir, S.write_freeze(bdir, _cand("live", "live.b2-q1")).stem)
    assert live["source"]["summary"] == "live summary"
    assert live["source"]["fingerprint"] == "req-live"
    # tampering is detected
    rec["repair_text"] = "edited"
    p.write_text(json.dumps(rec))
    with pytest.raises(ValueError):
        S.read_freeze(bdir, "opencode.b2-q1")


def test_freeze_validates(tmp_path):
    bdir = _bdir(tmp_path)
    with pytest.raises(ValueError):
        S.write_freeze(bdir, dict(_cand(), source_policy="pi"))  # the anchor is never a source
    with pytest.raises(ValueError):
        S.write_freeze(bdir, {k: v for k, v in _cand().items() if k != "checks"})
    with pytest.raises(ValueError):
        S.write_freeze(bdir, dict(_cand(), cand_id="../escape"))
    with pytest.raises(ValueError):
        S.write_freeze(bdir, dict(_cand(), act_spec=_spec({"kind": "nope"})))
    assert not (bdir / "boundary" / "freeze").exists() or \
        not list((bdir / "boundary" / "freeze").iterdir())
