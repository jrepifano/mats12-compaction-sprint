"""Unit tests for scripts/swechat_review.py (no calls, no data)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]

import swechat_review as R


def card(cid="s1", deck="B", **kw):
    c = {"id": cid, "deck": deck, "n": 1, "source": "x", "model": "m", "harness": "h",
         "claim": "Fixed it", "claim_found": True, "summary": "", "kept": [], "hist": "h/s1.txt",
         "hist_chars": 1, "n_history": 1, "msg": None}  # fmt: skip
    return c | kw


def test_leaks_clean(tmp_path):
    h = tmp_path / "s1.txt"
    h.write_text("[m0] USER: fix it")
    assert R.leaks("<html></html>", [card()], [h], [{"reason": "The tests failed at the end."}]) == []


def test_leaks_reason_in_history_file_or_page(tmp_path):
    h = tmp_path / "s1.txt"
    h.write_text("... The tests failed at the end. ...")
    key = [{"reason": "The tests failed at the end."}]
    assert R.leaks("", [card()], [h], key)
    h.write_text("clean")
    assert R.leaks("<p>The tests failed at the end.</p>", [card()], [h], key)


def test_leaks_fields_and_deck_b_reason(tmp_path):
    h = tmp_path / "s1.txt"
    h.write_text("")
    assert R.leaks("", [card(verdict="false")], [h], [])
    assert R.leaks("", [card(why="striking")], [h], [])
    assert not R.leaks("", [card(deck="A", why="striking")], [h], [])


def _readout(tmp_path, monkeypatch, labels, partial=False):
    out = tmp_path / "review"
    out.mkdir(exist_ok=True)
    key = {"cards": {"a": {"arm": "false"}, "b": {"arm": "false"}, "c": {"arm": "true"}}}
    (out / "key_B.private.json").write_text(json.dumps(key))
    monkeypatch.setattr(R, "OUT", out)
    monkeypatch.setattr(R.S, "RESULTS", tmp_path)
    f = tmp_path / "labels.json"
    f.write_text(json.dumps(labels))
    R.cmd_readout(SimpleNamespace(labels=str(f), partial=partial))
    return json.loads((tmp_path / "review_readout.json").read_text())


def test_readout_counts_cant_tell_as_not_confirmed(tmp_path, monkeypatch):
    res = _readout(tmp_path, monkeypatch, [{"card": "a", "choice": "false"},
                   {"card": "b", "choice": "cant_tell"}, {"card": "c", "choice": "true"}])  # fmt: skip
    assert res["precision_false"]["k"] == 1 and res["precision_false"]["n"] == 2
    assert res["true"]["k"] == 1 and not res["partial"]


def test_readout_refuses_partial(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        _readout(tmp_path, monkeypatch, [{"card": "a", "choice": "false"}])
    res = _readout(tmp_path, monkeypatch, [{"card": "a", "choice": "false"}], partial=True)
    assert res["partial"] and res["false"]["n"] == 1 and res["false"]["of"] == 2


def test_deck_b_cited_message_is_a_leak(tmp_path):
    h = tmp_path / "s1.txt"
    h.write_text("")
    assert R.leaks("", [card(msg=12)], [h], [])
    assert not R.leaks("", [card(deck="A", msg=12, why="w")], [h], [])


def test_check_decks():
    a = [{"id": f"a{i}"} for i in range(6)]
    key_a = {f"a{i}": {"rid": f"src:r{i}:0"} for i in range(6)}
    key_b = {f"b{i}": {"rid": f"src:q{i}:0", "arm": "false" if i < 20 else "true"}
             for i in range(30)}  # fmt: skip
    b = [{"id": k} for k in key_b]
    assert R.check_decks(a, b, key_a, key_b) == []
    key_a["a0"]["rid"] = "src:q3:2"  # another compaction of a Deck B session
    assert any("share sessions" in x for x in R.check_decks(a, b, key_a, key_b))
    assert any("Deck A has 5" in x for x in R.check_decks(a[:5], b, dict(list(key_a.items())[:5]), key_b))


def test_clean_drops_terminal_codes():
    assert R.clean("\x1b[31mFAIL\x1b[0m ok\ttab\nline\x07") == "FAIL ok\ttab\nline"
    assert R.clean("caf\ufffd") == "caf?"
    assert R.clean("\x1b]0;title\x07seen \x1b]2;t\x1b\\[m3]") == "seen [m3]"  # OSC (astra r2)
