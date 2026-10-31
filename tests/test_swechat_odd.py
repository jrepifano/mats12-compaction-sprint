"""Unit tests for scripts/swechat_odd.py and scripts/swechat_scan.py (no calls, no data)."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]

import swechat_odd as O
import swechat_scan as S

from compaction import s1_types as T


def resp(data, error=None):
    return T.Resp(key="k", data=data, text=None, error=error)


def claim(cid, verdict, quote=None, completion=True):
    return {
        "id": cid,
        "completion_claim": completion,
        "verdict": verdict,
        "false_kind": "failed" if verdict == "false" else None,
        "quote": quote,
        "reason": "r",
    }


def test_candidates_tags_matching_lines_only():
    tagged, cands = S.candidates("# Done\n- Fixed the parser\n- Next: add docs\n\nAll tests pass ✅")
    assert [c for c, _ in cands] == ["C1", "C2", "C3"]
    assert cands[1] == ("C2", "- Fixed the parser")
    assert "[C2] - Fixed the parser" in tagged
    assert tagged.split("\n")[2] == "- Next: add docs"


def test_candidates_none():
    assert S.candidates("Plan the migration.\nRead the spec.")[1] == []


def test_tool_digest_keeps_first_and_last_matching_lines():
    d = S._tool_digest("\n".join(f"test {i} failed" for i in range(20)))
    assert "test 0 failed" in d and "test 19 failed" in d and "12 more matching lines" in d


def test_digest_latest_part_and_abridged_earlier():
    msgs = [{"role": "user", "text": "please fix the bug"}]
    msgs += [{"role": "tool", "text": "line\n" * 50 + "3 tests failed"} for _ in range(5)]
    msgs += [{"role": "assistant", "text": "x" * 40_000}]
    dig, start = S.digest(msgs)
    assert start == 6
    assert "Earlier history (m0-m5)" in dig and "3 tests failed" in dig
    assert "please fix the bug" in dig and "in full" not in dig


def test_parse_complete_with_untagged():
    cands = [("C1", "- Fixed X"), ("C2", "- Next: Y")]
    untagged = {"line": "Done with Z", "verdict": "true", "false_kind": None, "quote": None,
                "reason": "r"}  # fmt: skip
    data = {"claims": [claim("C1", "false", "FAIL test_x"), claim("C2", "true", completion=False)],
            "untagged": [untagged]}  # fmt: skip
    summary = "- Fixed X\n- Next: Y\nDone with Z"
    rows, status = S.parse(resp(data), cands, "... --- FAIL   test_x (0.1s) ...", summary)
    assert status == "complete"
    assert rows[0]["verdict"] == "false" and rows[0]["quote_found"] is True
    assert rows[1]["completion_claim"] is False and rows[1]["verdict"] is None
    assert rows[2]["cid"] == "U1" and rows[2]["tagged"] is False and rows[2]["label_ok"]


def test_parse_missing_line_is_incomplete():
    data = {"claims": [claim("C1", "true")], "untagged": []}
    rows, status = S.parse(resp(data), [("C1", "a"), ("C2", "b")], "")
    assert status == "incomplete" and rows[1]["label_error"] == "missing"


def test_parse_claim_without_verdict_is_incomplete():
    data = {"claims": [claim("C1", None)], "untagged": []}
    rows, status = S.parse(resp(data), [("C1", "x")], "")
    assert status == "incomplete" and rows[0]["label_error"] == "no_verdict"


def test_parse_error_response():
    rows, status = S.parse(resp(None, "limit"), [("C1", "x")], "")
    assert status == "error" and rows[0]["label_error"] == "limit"


def test_parse_recheck_unconfirmed_false_becomes_cant_tell():
    rows = [{"rid": "r", "cid": c, "attempt": "a1"} for c in ("C1", "C2", "C3")]
    c1 = {"id": "C1", "msg": 3, "verdict": "false", "false_kind": "failed", "quote": "FAIL test_x1",
          "reason": "r"}  # fmt: skip
    c2 = c1 | {"id": "C2", "quote": "not there"}
    out = S.parse_recheck(resp({"claims": [c1, c2]}), rows, "[m3] TOOL: FAIL test_x1 ...")
    assert [o["final"] for o in out] == ["false", "cant_tell", "cant_tell"]
    assert out[2]["rc_verdict"] is None and out[0]["attempt"] == "a1"


def test_parse_recheck_failed_call_stays_pending():
    out = S.parse_recheck(resp(None, "limit"), [{"rid": "r", "cid": "C1", "attempt": "a"}], "")
    assert out[0]["final"] is None and out[0]["rc_error"] == "limit"


def test_parse_rejects_invented_and_tagged_untagged_lines():
    u = {"verdict": "true", "false_kind": None, "quote": None, "reason": "r"}
    data = {"claims": [claim("C1", "true")],
            "untagged": [u | {"line": "- Fixed X"}, u | {"line": "made up"},
                         u | {"line": "Done Z"}, u | {"line": "Done Z"}]}  # fmt: skip
    rows, status = S.parse(resp(data), [("C1", "- Fixed X")], "", "- Fixed X\nDone Z")
    assert status == "complete"
    assert [r["label_error"] for r in rows[1:]] == [
        "not_an_untagged_summary_line", "not_an_untagged_summary_line", None, "duplicate"]


def test_final_attempt_and_todo():
    good = {"rid": "a", "status": "complete"}
    assert S.final_attempt([{"rid": "a", "status": "error"}, good]) is good
    by = {
        "a": [good],
        "b": [{"rid": "b", "status": "incomplete"}],
        "c": [{"rid": "c", "status": "incomplete"}] * 2,
        "d": [{"rid": "d", "status": "error"}],
    }
    pop = [{"id": x} for x in "abcde"]
    assert [(e["id"], n) for e, n in S._todo(pop, by)] == [("b", 1), ("d", 0), ("e", 0)]


def test_read_skips_torn_last_line(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps({"a": 1}) + "\n" + '{"a": 2')
    assert S._read(p) == [{"a": 1}]


def test_request_key_changes_with_attempt():
    rec = {"summary": "- Fixed it", "source_messages": [{"role": "user", "text": "fix it"}]}
    assert S.request("rid", rec, 0)[0].key == "scan|rid"
    assert S.request("rid", rec, 1)[0].key == "scan|rid|r1"
    nocand = S.request("rid", {"summary": "plan", "source_messages": []})[0]
    assert "none (list only untagged lines)" in nocand.user  # untagged discovery still asked


NOW = 2_000_000_000.0
FRESH = datetime.fromtimestamp(NOW - 60, UTC).isoformat()
STALE = datetime.fromtimestamp(NOW - 7_200, UTC).isoformat()


def _w(five, week, reset=5.0, utc=FRESH, five_reset=NOW + 1000):
    return lambda: {"five_hour": (five, five_reset, utc), "seven_day": (week, reset, utc)}


def clock():
    return NOW


def test_pace_go_on_a_fresh_reading(monkeypatch):
    monkeypatch.setattr(S, "windows", _w(0.1, 0.40))
    st = {}
    assert S.pace(st, clock=clock) == "go" and st == {"start_week": 0.40, "week_reset": 5.0}


def test_pace_stale_reading_refreshes(monkeypatch):
    monkeypatch.setattr(S, "windows", _w(0.1, 0.40, utc=STALE))
    assert S.pace({}, clock=clock) == "refresh"


def test_pace_pauses_until_reset_then_rechecks_limits(monkeypatch):
    t = [NOW]
    readings = iter([_w(0.80, 0.40)(), _w(0.80, 0.90)()])
    monkeypatch.setattr(S, "windows", lambda: next(readings))
    slept = []
    with pytest.raises(S.Stop):  # the weekly window rose during the sleep
        S.pace({}, sleep=lambda w: (slept.append(w), t.__setitem__(0, t[0] + w)),
               clock=lambda: t[0])  # fmt: skip
    assert slept == [1000 + S.RESET_MARGIN_S]


def test_pace_after_a_passed_reset_refreshes(monkeypatch):
    monkeypatch.setattr(S, "windows", _w(0.80, 0.40, five_reset=NOW - 1000))
    slept = []
    assert S.pace({}, sleep=slept.append, clock=clock) == "refresh" and slept == []
    monkeypatch.setattr(S, "windows", _w(0.10, 0.40, five_reset=NOW - 30))  # fresh, pre-reset
    assert S.pace({}, clock=clock) == "refresh"


def test_pace_baseline_persists_and_stops(monkeypatch):
    monkeypatch.setattr(S, "windows", _w(0.1, 0.61))
    with pytest.raises(S.Stop):
        S.pace({"start_week": 0.40, "week_reset": 5.0}, clock=clock)  # +0.21 since baseline


def test_pace_new_week_resets_baseline(monkeypatch):
    monkeypatch.setattr(S, "windows", _w(0.1, 0.05, reset=9.0))
    st = {"start_week": 0.40, "week_reset": 5.0}
    assert S.pace(st, clock=clock) == "go" and st["start_week"] == 0.05


def test_pace_weekly_absolute_stop(monkeypatch):
    monkeypatch.setattr(S, "windows", _w(0.1, 0.86))
    with pytest.raises(S.Stop):
        S.pace({}, clock=clock)


def test_pace_no_reading_bootstraps_once(monkeypatch):
    monkeypatch.setattr(S, "windows", lambda: None)
    st = {}
    assert S.pace(st) == "refresh" and st["bootstrapped"]
    with pytest.raises(S.Stop):
        S.pace(st)


def test_pace_missing_window_stops(monkeypatch):
    monkeypatch.setattr(S, "windows", lambda: {"five_hour": (0.1, 0.0, FRESH)})
    with pytest.raises(S.Stop):
        S.pace({}, clock=clock)


def test_paced_stops_without_a_newer_reading(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(S, "windows", _w(0.1, 0.40))
    monkeypatch.setattr(S, "_age_s", lambda utc, clock=None: 0.0)
    sent = []
    stop = S.paced({}, list(range(30)), lambda c: sent.append(c) or len(c))
    assert stop and "fail closed" in stop and len(sent) == 1  # the reading never advanced


def test_paced_sends_everything_when_readings_advance(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "STATE", tmp_path / "state.json")
    n = iter(range(10_000))
    monkeypatch.setattr(S, "windows", lambda: {
        "five_hour": (0.1, NOW + 1000, f"2030-01-01T00:{next(n) % 60:02d}:00Z"),
        "seven_day": (0.4, 5.0, "")})  # fmt: skip
    monkeypatch.setattr(S, "reading_utc", lambda r: (r or {})["five_hour"][2] + str(next(n)).zfill(6))
    monkeypatch.setattr(S, "_age_s", lambda utc, clock=None: 0.0)
    sent = []
    assert S.paced({}, list(range(30)), lambda c: sent.append(c) or len(c)) is None
    assert sum(len(c) for c in sent) == 30


def test_leaks_finds_escaped_key_text_and_extra_fields():
    key = [{"item_id": "swe-chat-v1:abc:0#haiku-max#i3", "odd_what": 'He said "done" <b>x</b>'}]
    card = {"id": "s1", "claim": 'He said "done" <b>x</b>'}
    blob = json.dumps([card]).replace("<", "\\u003c")
    html = f'<script id="data" type="application/json">{blob}</script>'
    assert O.leaks(html, key)  # escaped in the HTML, found in the decoded card
    assert O.leaks("", [], [{"id": "s1", "verdict": "false"}])
    clean = '<script id="data" type="application/json">[{"id": "s1"}]</script>'
    assert not O.leaks(clean, key)


def _comp(rid, status="complete", model="claude-opus-4-6"):
    return {"rid": rid, "session_group": "g" + rid[1:], "model": model, "harness": "claude-code",
            "n_candidates": 2, "status": status, "in_tok": 1, "out_tok": 1}  # fmt: skip


def test_stats_and_summary_md_on_synthetic_rows():
    comp = pd.DataFrame([_comp(f"r{i}") for i in range(4)] + [_comp("r9", "incomplete", "x")])
    verdicts = {
        "r0": ["false", "true"],
        "r1": ["true", "cant_tell"],
        "r2": ["true", "true"],
        "r3": ["false", "false"],
    }
    cl = pd.DataFrame([
        {"rid": r, "session_group": "g" + r[1:], "cid": f"C{j}", "tagged": True,
         "completion_claim": True, "label_ok": True, "verdict": v, "final": v,
         "false_kind": "failed" if v == "false" else None}
        for r, vs in verdicts.items() for j, v in enumerate(vs)
    ])  # fmt: skip
    st = S.stats(comp, cl)
    assert st["n_unresolved"] == 1
    assert st["per_compaction"]["k"] == 2 and st["per_compaction"]["n"] == 4
    assert st["per_claim"]["k"] == 3 and st["per_claim"]["n"] == 8
    assert st["per_decided_claim"]["n"] == 7 and st["per_compaction"]["boot"] is not None
    out = {"seed": "s", "model": S.MODEL, "effort": "low", "fp": "f", "population": 928,
           "target": 928, "stop_reason": None, "final": st, "first_pass": st,
           "with_untagged": st, "untagged": {"lines": 0, "compactions_with_any": 0, "rejected": {}},
           "recheck": {"outcomes": {}}, "pending_recheck": 0,
           "by_model": {"Opus 4.6": st}, "by_harness": {}, "by_model_small": {}}  # fmt: skip
    md = S.summary_md(out)
    assert "Opus 5.5's read, not hand-checked" in md and "effort low" in md
    assert "2/4 = 50.0%" in md and "| Opus 4.6 | 4 |" in md


def test_stats_empty():
    comp = pd.DataFrame([], columns=S.COMP_COLS)
    cl = pd.DataFrame([], columns=[*S.CLAIM_COLS, "final"])
    st = S.stats(comp, cl)
    assert st["n_compactions"] == 0 and st["per_compaction"]["rate"] is None
