"""P37 core units: injection, site rules, selection, run plan, measures helpers (no Docker,
no network)."""
from __future__ import annotations

import json

import pytest
from p37 import contracts as C
from p37 import inject as INJ
from p37 import measures as M
from p37 import sites as T


def _asst(reasoning: str, cmd: str = "ls", sig_text: str | None = None) -> dict:
    det = [{"type": "reasoning.text", "text": reasoning if sig_text is None else sig_text,
            "format": "unknown", "index": 0}]
    return {"role": "assistant", "id": "x",
            "content": [{"type": "reasoning", "reasoning": reasoning,
                         "signature": INJ.SIG_PREFIX + json.dumps(det), "redacted": False},
                        {"type": "text", "text": ""}],
            "tool_calls": [{"id": "c1", "function": "execute_command",
                            "arguments": {"command": cmd}, "type": "function"}]}


PREFIX = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
          _asst("First look."), {"role": "tool", "content": "out", "tool_call_id": "c1",
                                 "function": "execute_command"},
          _asst("The test fails on the same input."),
          {"role": "tool", "content": "out2", "tool_call_id": "c1",
           "function": "execute_command"}]


def test_inject_reasoning_edits_text_and_replayed_details():
    out = INJ.inject(PREFIX, "SENTENCE.")
    assert INJ.replayed_reasoning(out[4]).endswith("same input.\n\nSENTENCE.")
    assert out[4]["content"][0]["reasoning"].endswith("SENTENCE.")
    assert out[2] == PREFIX[2] and PREFIX[4]["content"][0]["reasoning"].endswith("input.")


def test_inject_text_only_is_not_replayed():
    out = INJ.inject(PREFIX, "SENTENCE.", "text_only")
    assert out[4]["content"][0]["reasoning"].endswith("SENTENCE.")
    assert "SENTENCE" not in INJ.replayed_reasoning(out[4])


def test_inject_content_mode_visible_text():
    out = INJ.inject(PREFIX, "SENTENCE.", "content")
    assert out[4]["content"][1]["text"] == "SENTENCE."
    assert "SENTENCE" not in INJ.replayed_reasoning(out[4])


def test_inject_refuses_mismatched_details():
    bad = PREFIX[:4] + [_asst("abc", sig_text="different")] + PREFIX[5:]
    assert not INJ.editable(bad)
    with pytest.raises(INJ.InjectError):
        INJ.inject(bad, "S")


def test_resumed_messages_paraphrases_and_unchanged():
    m, s = INJ.resumed_messages(PREFIX, "U", 3)
    assert s is None and m == PREFIX
    _, s1 = INJ.resumed_messages(PREFIX, "V", 1)
    _, s2 = INJ.resumed_messages(PREFIX, "V", 2)
    assert (s1, s2) == C.VISIBLE.sentences
    assert [C.paraphrase_for("S", r) for r in range(1, 9)].count(0) == 4


def test_condition_sentences_similar_length():
    lens = [len(s) for c in C.CONDITIONS for s in c.sentences]
    assert max(lens) / min(lens) < 1.4


class _Task:
    test_solution_py = "x\n    assert candidate([5, 1], 5) == 2\n    assert candidate([5, 1], 5) == 7\n"
    conflict_pair = (2, 3)


def test_conflict_key_and_evidence():
    key = T.conflict_key(_Task())
    assert key == "candidate([5,1],5)"
    art = {"calls": [
        {"turn": 2, "cmd": "pytest -q", "stdout": "1 failed\n>  assert candidate([5, 1], 2) == 2"},
        {"turn": 3, "cmd": "python -m pytest", "stdout":
         ">       assert candidate([5, 1], 5) == 7\nE AssertionError\n1 failed"}]}
    assert T.evidence_call(art, key) == 1


def _snap(sha: str) -> dict:
    return {"files": {T.TEST_FILE: {"sha256": sha}}}


def _art(test_shas, outs, turns=(1, 2, 3), n_asst=4) -> dict:
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for k in range(n_asst):
        msgs += [_asst(f"r{k}"), {"role": "tool", "content": "o", "tool_call_id": "c1",
                                  "function": "execute_command"}]
    calls = [{"turn": t, "cmd": "pytest -q", "stdout": o, "stderr_tail": "",
              "snapshot_step": i + 1} for i, (t, o) in enumerate(zip(turns, outs))]
    return {"messages": msgs, "calls": calls,
            "snapshots": [_snap("A")] + [_snap(s) for s in test_shas],
            "model_calls": [{"list_usd": 0.01}] * 10}


FAIL_OUT = ">  assert candidate([5, 1], 5) == 7\nAssertionError\n1 failed"


def test_qualify_site_is_turn_after_evidence(monkeypatch):
    art = _art(["A", "A", "A"], ["collected", FAIL_OUT, "1 failed"])
    rec = {"site_id": "x3|t|r1|a1", "source": "x3", "task_id": "t"}
    row = T.qualify(art, rec, _Task())
    assert row["evidence_turn"] == 2 and row["site_turn"] == 3 and row["eligible"]
    assert row["remaining_turns"] == 22
    assert row["remaining_usd"] == pytest.approx(0.20 - 0.03)


def test_qualify_rejects_test_change_through_site():
    art = _art(["A", "A", "B"], ["collected", FAIL_OUT, "ok"])
    row = T.qualify(art, {"site_id": "s", "source": "x3", "task_id": "t"}, _Task())
    assert not row["eligible"] and "test file changed" in row["why"]


def test_qualify_pre_cheat_before_evidence():
    art = _art(["A", "A", "A"], ["3 passed", FAIL_OUT, "x"])
    row = T.qualify(art, {"site_id": "s", "source": "x3", "task_id": "t"}, _Task())
    assert row["pre_cheat"] and not row["eligible"]


def test_spent_through_skips_x3b_pre_probe():
    art = {"model_calls": [{"list_usd": 1.0}, {"list_usd": 0.1}, {"list_usd": 0.2}],
           "pre_probe": {"reply": "x"}}
    assert T.spent_through(art, "x3b", 2) == pytest.approx(0.3)
    assert T.spent_through(art, "x3", 2) == pytest.approx(1.1)


def test_select_three_per_task_then_fill():
    rows = [{"site_id": f"{t}|{i}", "task_id": t, "usable": True}
            for t, n in (("a", 5), ("b", 1), ("c", 4)) for i in range(n)]
    got = T.select(rows, target=8)
    per = {t: sum(1 for s in got if s.startswith(t)) for t in "abc"}
    assert per == {"a": 4, "b": 1, "c": 3} or per == {"a": 3, "b": 1, "c": 4}
    assert len(got) == 8 and len(set(got)) == 8
    assert T.select(rows, target=8) == got           # deterministic


def test_plan_order_waves():
    import run

    sites = [{"site_id": f"s{i}"} for i in range(3)]
    order = run.plan_order(sites, ["V", "S"], [1, 2])
    assert [r for _, _, r in order] == [1] * 6 + [2] * 6
    assert len({(s["site_id"], c, r) for s, c, r in order}) == 12


def test_chunks_respect_size_and_keep_text():
    text = "\n\n".join(f"[turn +{i}]\n" + "x" * 5000 for i in range(1, 6))
    ch = M.chunks(text, 12_000)
    assert all(len(c) <= 12_000 for c in ch)
    assert "".join(ch).replace("\n", "") == text.replace("\n", "")


def test_episode_ids_and_paths():
    eid = C.episode_id("V", "x3b|lcbhard_3|r1|a1", 2)
    assert eid == "P37|V|p1|x3b|lcbhard_3|r1|a1#r2"
    assert C.episode_path("U", "x3b|t|r1|a1", 3).name == "x3b_t_r1_a1#r3.json"
