"""SWE-Interact pilot candidate cards (scripts/s2_interact_cards.py): the page for a fixture
boundary, the leak check, the refusal beside continuation output, and labels -> freeze. Fakes
only: no model calls, no Docker."""

from __future__ import annotations

import base64
import gzip
import importlib
import json
import sys
from pathlib import Path

import pytest

from compaction.s2 import interact_run as IR
from compaction.s2 import interact_score as S
from compaction.s2 import replays

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

REQ = "Return the 409 with the existing record id in the body."
MATCHED = "Docs build is green on my machine as well, by the way."  # in the history, not in the summary
REPLY = f"Looks close. {REQ} Otherwise fine."
CAND = "cc_prompt.b1-q1"


@pytest.fixture(scope="module")
def SC():
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module("s2_interact_cards")
    finally:
        sys.path.pop(0)


def _w(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def _checks() -> dict:
    return {
        "retention": {"outcome": "pass", "reason": None,
                      "evidence": {"grade": "dropped", "summary_quote": None,
                                   "still_present": False}},
        "recoverability": {"outcome": "unresolved", "reason": "partial_or_search_hits",
                           "evidence": {"image": "kept:b1", "line_hits": {},
                                        "fragment_hits": {},
                                        "search_hits": {"existing_id": [
                                            "api.py", "main:api.py", "/tmp/plan.md",
                                            "commit:abc123"]},
                                        "searched": [REQ, "existing_id"],
                                        "search_after_draft": True}},
        "applicability": {"outcome": "pass", "reason": None,
                          "evidence": {"why": "The conflict path is still being written."}},
        "decision": {"outcome": "pass", "reason": None,
                     "evidence": {"decision": "Build the 409 body without the existing id."}},
        "design": {"outcome": "pass", "reason": None,
                   "evidence": {"spec_errors": [], "matched_quote": MATCHED,
                                "matched_rationale": "A build status line, unrelated.",
                                "length_ratio": 0.98, "problems": []}},
        "reference": {"outcome": "pass", "reason": None,
                      "evidence": {"image": "kept:b1", "solution_patch": "solution.patch",
                                   "act_raw": False, "reached": False, "matches": [],
                                   "decision_matches": []}},
    }


ACT_SPEC = {"act": {"kind": "diff_added", "pattern": r"status_code=409\)$", "path": "api.py",
                    "negate": False},
            "decision": {"kind": "diff_added", "pattern": "409", "path": "api.py"},
            "description": "Returns the 409 without the existing record id.",
            "reask_keywords": ["409", "existing id"]}


@pytest.fixture()
def run(tmp_path):
    run = tmp_path / "interact" / "demo-deepseek-a1"
    bdir = run / "b1"
    b = bdir / "boundary"
    ask = {"role": "assistant", "content": "", "tool_calls": [{
        "id": "c2", "type": "function",
        "function": {"name": "ask_user", "arguments": json.dumps({"question": "review?"})}}]}
    pre = [{"role": "system", "content": "sys"},
           {"role": "user", "content": "# AGENTS.md instructions for /app\nstep"},
           {"role": "assistant", "content": MATCHED},
           ask, {"role": "tool", "tool_call_id": "c2", "content": REPLY},
           {"role": "assistant", "content": "working on it"},
           {"role": "assistant", "content": "tail message"}]
    _w(b / "pre_messages.json", pre)
    live_post = [pre[0], {"role": "user", "content": "LIVE SUMMARY"}, pre[6]]
    _w(b / "compaction.json", {"summary": "LIVE SUMMARY", "summary_index": 1,
                               "kept_indices": [6], "post_messages": live_post,
                               "info": {"request_sha256": "req-b1"}})
    _w(b / "post_messages.json", live_post)
    post = [pre[0], pre[1], {"role": "user", "content": "CC REPLAY SUMMARY of the work"}]
    _w(b / "replays" / "cc_prompt" / "post_messages.json", post)
    _w(b / "replays" / "cc_prompt" / "replay.json",
       {"policy": "cc_prompt", "summary": "CC REPLAY SUMMARY of the work", "summary_index": 2,
        "kept_indices": [], "fingerprint": "fp-cc", "post_sha256": replays.canon_sha256(post)})
    _w(b / "sim_replies.json", [{"reply_id": "r2", "call_index": 2, "step": "02_implement",
                                 "question": "review?", "reply": REPLY, "pre_index": 4}])
    _w(b / "requirements.json", {"items": [{
        "req_id": "b1-q1", "text": "Include the existing record id in the 409 body.",
        "quote": REQ, "source_reply_id": "r2", "source_pre_index": 4, "position": 0,
        "grounding_quote": "existing record's id"}]})
    _w(b / "boundary.json", {"boundary_index": 1, "calls_in_step": 40, "stage": "02_implement",
                             "tail_start_index": 6, "sim_calls": 2})
    spec = tmp_path / "instruction.md"
    spec.write_text("Conflicting writes return 409 with the existing record's id.")
    patch = tmp_path / "solution.patch"
    patch.write_text("diff --git a/api.py b/api.py\n+++ b/api.py\n+ok = 1\n")
    task = {"task": {"task_id": "demo", "image": "kept:b1", "solution_patch": str(patch),
                     "spec_path": str(spec)}}
    _w(bdir / "run.json", task)
    _w(run / "run.json", task)
    cand = {"cand_id": CAND, "source_policy": "cc_prompt", "req_id": "b1-q1",
            "source_reply_id": "r2", "position": 0, "repair_text": REQ,
            "text": "Include the existing record id in the 409 body.", "checks": _checks(),
            "draft": {"applicable": "yes", "decision_observable": "yes",
                      "act_description": ACT_SPEC["description"],
                      "matched_quote": MATCHED, "search_strings": ["existing_id"]},
            "act_spec": ACT_SPEC, "disposition": "carded"}
    other = dict(cand, cand_id="live.b1-q1", source_policy="live",
                 disposition="excluded:retention")
    _w(b / "candidates.json", {"boundary_index": 1, "carded": CAND,
                               "candidates": [other, cand]})
    return run


def _built(SC, run, tmp_path):
    html, key = SC.build([(run, 1)])
    out = tmp_path / "private" / "cards"
    SC.write_page(out, html, key, private_root=tmp_path / "private", check_ignored=False)
    return html, key, out


def _gunzip(s: str) -> str:
    return gzip.decompress(base64.b64decode(s)).decode()


def _labels_dir(tmp_path, key, data: dict) -> Path:
    """An ArtifactData read-back: one file holding a list of {id, collection, data}."""
    d = tmp_path / "labels_export"
    cid = key["order"][0]
    v = key["cards"][cid]["content_sha256"][:16]
    _w(d / "labels.json", [{"id": cid, "collection": "labels", "data": {"v": v, **data}}])
    return d


YES = {f"c{k}": "yes" for k in range(1, 7)}


def test_page_shows_one_card_per_boundary_with_six_checks(SC, run, tmp_path):
    html, key, out = _built(SC, run, tmp_path)
    SC.leak_check(html, key)
    data = SC.RP.page_data(html)
    assert len(data["cards"]) == 1 and data["collections"] == {"labels": "labels"}
    u = data["cards"][0]
    assert set(u) <= SC.CARD_FIELDS
    assert "Claude Code-style summary written by Haiku" in u["source_note"]
    assert u["requirement"]["quote"] == REQ and u["reply"]["reply"] == REPLY
    assert u["located"] and u["context"].startswith("Task demo · boundary 1, 40 calls")
    assert [e["outcome"] for e in u["checks"]] == ["pass", "unresolved", "pass", "pass", "pass",
                                                   "pass"]
    rec = u["checks"][1]
    assert rec["lines"][0].startswith("The code is already in the working tree")
    row = rec["lists"][0]["items"][0]
    assert ("working tree: api.py" in row and "outside the repo: /tmp/plan.md" in row
            and "commit messages: commit:abc123" in row and "branches: main:api.py" in row)
    assert u["design"]["d_matched"] == MATCHED and u["design"]["d_dec_kind"] == "diff_added"
    assert u["patch"].startswith("diff --git")
    # the two boxes: the agent's history, and the resumed input built from the SOURCE summary
    hist, post = _gunzip(data["histories"][u["hist_ref"]]), _gunzip(data["histories"][
        u["post_ref"]])
    assert REPLY in hist and MATCHED in hist
    assert "CC REPLAY SUMMARY" in post and "LIVE SUMMARY" not in post
    # the six questions, with descriptive answers (no bare Yes/No) and Can't tell third
    assert len(SC.CHECK_TEXT) == 6
    assert all(a.split()[0].strip(":,.").lower() not in ("yes", "no")
               for c in SC.CHECK_TEXT for a in c[3:])
    assert "Can\\u2019t tell" in html and '"unclear"' in html
    assert (out / "page.html").exists() and (out / "key.private.json").stat().st_mode & 0o77 == 0
    assert key["cards"][u["id"]]["cand_id"] == CAND


def test_leak_check_refuses_key_strings(SC, run):
    html, key = SC.build([(run, 1)])
    with pytest.raises(SC.S1P.LeakError):
        SC.leak_check(html.replace("</body>", f"{CAND}</body>") + CAND, key)
    with pytest.raises(SC.S1P.LeakError):
        SC.leak_check(html + '"cand_id"', key)
    bad = html.replace('"source_note":', '"cand":1,"source_note":', 1)
    with pytest.raises(SC.S1P.LeakError):
        SC.leak_check(bad, key)


@pytest.mark.parametrize("where", ["arms", "b1/arms"])
def test_build_refuses_beside_continuation_output(SC, run, where):
    (run / where).mkdir(parents=True)
    with pytest.raises(SC.ArmsExist):
        SC.build([(run, 1)])


def test_write_page_refuses_outside_the_private_root(SC, run, tmp_path):
    html, key = SC.build([(run, 1)])
    with pytest.raises(ValueError):
        SC.write_page(tmp_path / "public", html, key, private_root=tmp_path / "private",
                      check_ignored=False)


def test_labels_to_freeze_round_trip(SC, run, tmp_path):
    _, key, out = _built(SC, run, tmp_path)
    edited = "Return the 409 with the existing record's id in the body."
    labels = _labels_dir(tmp_path, key, {**YES, "n2": "api.py only follows it",
                                         "note": "good card", "d_repair": edited})

    def no_docker(image, script):
        raise AssertionError("the act spec was not edited: check 6 must not rerun")

    res = SC.freeze(SC.read_labels(labels), key, runner=no_docker)
    assert [r["status"] for r in res] == ["frozen"]
    assert res[0]["edited"] == ["d_repair"]
    assert res[0]["publish"].endswith(f"publish-freeze {run} 1 {CAND}")
    frz = IR.verified_freeze(run / "b1", CAND, require_committed=False)
    assert frz["repair_text"] == edited and frz["matched_text"] == MATCHED
    assert frz["act_spec"] == ACT_SPEC and frz["source"]["summary"].startswith("CC REPLAY")
    assert all(frz["checks"][n]["jacob"] == "pass" for n in SC.CHECKS)
    assert frz["checks"]["recoverability"]["jacob_note"] == "api.py only follows it"
    assert frz["checks"]["design"]["screened"]["outcome"] == "pass"  # rerun on the edit
    assert frz["card"]["note"] == "good card"
    lines = SC.report_lines(res)
    assert any("note [recoverability]: api.py only follows it" in x for x in lines)
    assert any("note [card]: good card" in x for x in lines)
    p = SC.write_decisions(out, key, res)
    assert json.loads(p.read_text())["cards"][key["order"][0]]["status"] == "frozen"
    # a second run never rewrites it
    again = SC.freeze(SC.read_labels(labels), key, runner=no_docker)
    assert again[0]["status"] == "already_frozen"
    SC.write_decisions(out, key, again)
    assert json.loads(p.read_text())["cards"][key["order"][0]]["status"] == "frozen"


def test_edited_act_reruns_check_six(SC, run, tmp_path):
    _, key, _ = _built(SC, run, tmp_path)
    labels = _labels_dir(tmp_path, key, {**YES, "d_act_pattern": r"ok = 1"})
    calls = []

    def ref(image, script):  # the reference's diff performs the edited act
        calls.append(image)
        return 0, "@@REFDIFF@@\ndiff --git a/api.py b/api.py\n+++ b/api.py\n+ok = 1\n", ""

    res = SC.freeze(SC.read_labels(labels), key, runner=ref)
    assert calls == ["kept:b1"] and res[0]["status"] == "held"
    assert "check 6 is fail" in res[0]["reasons"][0]
    assert not S.freeze_path(run / "b1", CAND).exists()


def test_rejections_unlabelled_and_held(SC, run, tmp_path):
    _, key, _ = _built(SC, run, tmp_path)
    cid = key["order"][0]
    rej = SC.freeze(SC.read_labels(_labels_dir(tmp_path, key, {**YES, "c3": "unclear",
                                                               "c4": "no"})), key)
    assert rej[0]["status"] == "rejected"
    assert rej[0]["reasons"] == ["applicability: unclear", "decision: no"]
    assert any("round 2" in x and CAND in x for x in SC.report_lines(rej))
    part = SC.freeze(SC.read_labels(_labels_dir(tmp_path, key, {"c1": "yes"})), key)
    assert part[0]["status"] == "unlabelled"
    gap = SC.freeze(SC.read_labels(_labels_dir(tmp_path, key, {**YES, "d_matched": " "})), key)
    assert gap[0]["status"] == "held" and "d_matched" in gap[0]["reasons"][0]
    stale = SC.freeze({cid: {**YES, "v": "0" * 16}}, key)
    assert stale[0]["status"] == "held" and "another version" in stale[0]["reasons"][0]
    (run / "arms").mkdir()
    arms = SC.freeze(SC.read_labels(_labels_dir(tmp_path, key, YES)), key)
    assert arms[0]["status"] == "held" and "arms/" in arms[0]["reasons"][0]
    assert not S.freeze_path(run / "b1", CAND).exists()
    with pytest.raises(ValueError):
        SC.freeze({"cnot-a-card": YES}, key)


def test_changed_evidence_is_held(SC, run, tmp_path):
    _, key, _ = _built(SC, run, tmp_path)
    labels = _labels_dir(tmp_path, key, YES)
    b = run / "b1" / "boundary"
    cj = json.loads((b / "candidates.json").read_text())
    cj["candidates"][1]["disposition"] = "rejected"  # a round-2 rescreen: not evidence
    (b / "candidates.json").write_text(json.dumps(cj))
    sims = json.loads((b / "sim_replies.json").read_text())
    sims[0]["reply"] += " Also log it."
    (b / "sim_replies.json").write_text(json.dumps(sims))
    res = SC.freeze(SC.read_labels(labels), key)
    assert res[0]["status"] == "held" and "evidence changed" in res[0]["reasons"][0]


def test_read_labels_formats(SC, tmp_path):
    d = tmp_path / "d"
    _w(d / "c1.json", {"c1": "yes"})  # one doc's bare data, named by its id
    _w(d / "c2.json", {"id": "c2", "data": {"c1": "no"}})
    _w(d / "all.json", {"labels": {"c3": {"c1": "unclear"}}})
    assert SC.read_labels(d) == {"c1": {"c1": "yes"}, "c2": {"c1": "no"},
                                 "c3": {"c1": "unclear"}}


def test_parse_boundary_accepts_a_run_key(SC, run):
    assert SC.parse_boundary("demo-deepseek-a1:1", root=run.parent) == (run.resolve(), 1)
    with pytest.raises(FileNotFoundError):
        SC.parse_boundary("demo-deepseek-a1:2", root=run.parent)
    with pytest.raises(ValueError):
        SC.parse_boundary("demo-deepseek-a1", root=run.parent)


def _rescreen(run, rejected: str, next_id: str) -> None:
    """What a round-2 `screen(rejected=[...])` leaves: the old card rejected, a new one carded."""
    b = run / "b1" / "boundary"
    cj = json.loads((b / "candidates.json").read_text())
    old = next(c for c in cj["candidates"] if c["cand_id"] == rejected)
    old["disposition"] = "rejected"
    cj["candidates"].append(dict(old, cand_id=next_id, source_policy="cc_prompt",
                                 disposition="carded",
                                 text="Keep save_record's signature as is."))
    cj.update(carded=next_id, rejected=[rejected])
    (b / "candidates.json").write_text(json.dumps(cj))


def test_a_rejected_candidate_never_freezes_on_old_yes_labels(SC, run, tmp_path):
    _, key, _ = _built(SC, run, tmp_path)
    labels = _labels_dir(tmp_path, key, YES)
    _rescreen(run, CAND, "cc_prompt.b1-q2")  # disposition only: the evidence hash is unchanged
    assert SC.card_parts(run, 1, CAND)["content_sha256"] == \
        key["cards"][key["order"][0]]["content_sha256"]
    res = SC.freeze(SC.read_labels(labels), key)
    assert res[0]["status"] == "held" and "no longer the boundary's carded" in res[0]["reasons"][0]
    assert not S.freeze_path(run / "b1", CAND).exists()


def test_round_two_page_keeps_round_one_labels_readable(SC, run, tmp_path):
    _, key1, _ = _built(SC, run, tmp_path)
    old_id = key1["order"][0]
    _rescreen(run, CAND, "cc_prompt.b1-q2")
    html2, key2 = SC.build([(run, 1)], previous=key1)
    new_id = key2["order"][0]
    assert new_id != old_id and set(key2["retired"]) == {old_id}
    SC.leak_check(html2, key2)
    v_old = key1["cards"][old_id]["content_sha256"][:16]
    v_new = key2["cards"][new_id]["content_sha256"][:16]
    export = {old_id: {**YES, "c2": "no", "v": v_old, "n2": "round-1 note"},
              new_id: {**YES, "v": v_new, "note": "round 2"}}
    res = SC.freeze(export, key2)
    by = {r["card"]: r for r in res}
    assert by[new_id]["status"] == "frozen"
    assert by[old_id]["status"] == "earlier_round"
    assert by[old_id]["notes"] == {"recoverability": "round-1 note"}
    # a third page still knows round 1's card
    _, key3 = SC.build([(run, 1)], previous=key2)
    assert old_id in key3["retired"] and new_id in key3["cards"]


def test_unperformed_checks_are_not_described_as_run(SC):
    c = {"checks": {"recoverability": {"outcome": "unresolved", "reason": "no_grep_runner",
                                       "evidence": {}},
                    "reference": {"outcome": "unresolved", "reason": "reference_did_not_apply",
                                  "evidence": {"rc": 1, "stderr": "patch failed"}}}}
    ev = {n: e for n, e in zip(SC.CHECKS, SC.check_evidence(c), strict=True)}
    assert ev["recoverability"]["lines"] == ["The repo search did not run (no_grep_runner)."]
    assert ev["reference"]["lines"][0].startswith("The reference check did not run")
    assert "patch failed" in ev["reference"]["lines"][1]


def test_label_controls_wait_for_saved_labels(SC, run):
    html, _ = SC.build([(run, 1)])
    assert "lock(true);" in html and html.index("lock(true);") < html.index('use("db")')
    assert html.count("lock(false)") == 3  # no storage, first snapshot, failed first load


def _la_cand(*, reread=False, named=True):
    rule = "Run `cargo clippy --all-targets` before every commit."
    return {"cand_id": "live.b1-la3", "origin": "lossaudit", "source_desc": "/tmp/plan.md",
            "source_index": 2, "disposition": "carded", "repair_text": rule,
            "checks": {"retention": {"outcome": "pass"},
                       "recoverability": {"outcome": "pass", "evidence": {
                           "class": "file_not_reattached",
                           "files_not_reattached": [{"file": "/tmp/plan.md",
                                                     "last_read_msg": 7}]}},
                       "pointer": {"outcome": "pass", "evidence": {
                           "stratum": "contextual_omission_retrievable_from_file",
                           "files": [{"file": "/tmp/plan.md", "named": named,
                                      "reread_instructed": reread}]}},
                       "opus": {"outcome": "pass", "evidence": {"verdict": {
                           "retention": "lost", "authority": "agent_plan_restating",
                           "authority_quote": "keep clippy clean", "conflict": "none",
                           "conflict_quote": ""}}}}}


def test_lossaudit_card_names_its_source_and_states_only_history_facts(SC, tmp_path):
    c = _la_cand()
    facts, _ = SC.facts_v2(c, 1, "boa", {"reply": f"# Plan\n- {c['repair_text']}\n"}, "live",
                           tmp_path, "Summary: keep going.")
    assert facts["said_label"] == "/tmp/plan.md said:"
    assert facts["file_notes"] == [
        ("Only in /tmp/plan.md: the agent last read it at message 7; the resumed input names "
         "the file but doesn't ask for a re-read.")]
    assert "from /tmp/plan.md" in facts["chips"] and "/tmp/plan.md still named" in facts["chips"]
    assert facts["opus"]["authority"] == "agent_plan_restating"
    facts, _ = SC.facts_v2(_la_cand(reread=True), 1, "boa", {"reply": ""}, "live", tmp_path, "")
    assert "re-read of /tmp/plan.md still instructed" in facts["chips"]
    assert facts["file_notes"][0].endswith("tells it to read the file again.")
    plain = dict(c, origin=None)
    plain["checks"] = {k: v for k, v in c["checks"].items() if k != "pointer"}
    facts, _ = SC.facts_v2(plain, 1, "boa", {"reply": ""}, "live", tmp_path, "")
    assert facts["said_label"] == "The maintainer said:" and facts["file_notes"] == []


def test_freeze_fields_for_a_file_backed_candidate(SC):
    r = SC.repair_fields(_la_cand())
    assert r["origin"] == "lossaudit" and r["reread_file"] == "/tmp/plan.md"
    assert r["backing_files"] == ["/tmp/plan.md"]
    assert r["stratum"] == "contextual_omission_retrievable_from_file"
    assert r["authority"]["conflict"] == "none"
    assert IR.arm_kinds(r) == ("repair", "matched", "reread")
    m = SC.repair_fields({"cand_id": "live.b1-q2", "checks": {}})
    assert m["origin"] == "maintainer" and m["reread_file"] is None and m["stratum"] is None
    assert IR.arm_kinds(m) == ("repair", "matched")


def _cli():
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module("s2_interact")
    finally:
        sys.path.pop(0)


def test_max_window_is_checked_before_every_request_and_read_at_most_every_5_min():
    cli = _cli()
    t = [0.0]
    reads = []
    level = {"u": 0.85}

    def reader(override):
        reads.append(t[0])
        return level["u"], ("seven_day" if level["u"] >= cli.MAX_7D_HOLD else None)

    win = cli.MaxWindow(None, reader=reader, clock=lambda: t[0])
    calls = []

    class B:
        model = "m"

        def run(self, reqs, purpose=None):
            calls.append(reqs)
            return {}
    g = cli.WindowGated(B(), win)
    g.run(["a"])
    t[0] = 200
    level["u"] = 0.95  # crossed, but not read again yet
    g.run(["b"])
    assert reads == [0.0] and g.model == "m"
    t[0] = 301
    with pytest.raises(cli.MaxWindowHold, match="0.95"):
        g.run(["c"])
    assert calls == [["a"], ["b"]] and reads == [0.0, 301]
    assert cli.max_window(91.0)[1] and cli.max_window(80.0) == (0.8, None)


def test_max_readings_must_be_fresh():
    cli = _cli()
    pings = []
    stale = {"stream": lambda now: None, "ping": lambda: pings.append(1) or 0.5}
    assert cli.max_window(None, **stale) == (0.5, None) and pings == [1]  # stream stale: ping
    assert cli.max_window(None, stream=lambda now: 0.93, ping=lambda: 0.1)[1].startswith(
        "seven_day at 0.93")
    why = cli.max_window(None, stream=lambda now: None, ping=lambda: None)[1]
    assert why.startswith("no fresh 7-day reading")
    # the operator's figure lasts 15 minutes, then the window is measured again
    t, seen = [0.0], []
    win = cli.MaxWindow(50.0, reader=lambda o: seen.append(o) or (0.5, None),
                        clock=lambda: t[0])
    win.check()
    t[0] = 600
    win.check()
    t[0] = cli.FRESH_S + 1
    win.check()
    assert seen == [50.0, 50.0, None]
