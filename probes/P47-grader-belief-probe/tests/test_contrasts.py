"""Tests for p47.contrasts (design/interfaces.md "Statements")."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from p47 import contracts as C
from p47 import contrasts as X

PROBE = Path(__file__).resolve().parents[1]
EXPECTED = {"crit": (160, 40), "aware": (60, 15), "self": (60, 15), "hack": (60, 15)}
INITIAL = [{"id": "s", "role": "system", "content": "You are a coding agent."},
           {"id": "u", "role": "user", "content": "Solve the task in /agent."}]


@pytest.fixture(scope="module")
def stmts():
    return X.statements()


def _pairs(stmts):
    out = defaultdict(list)
    for s in stmts:
        out[(s.pair_id, s.frame)].append(s)
    return out


def test_counts_per_direction_pole_split_frame(stmts):
    c = Counter((s.direction, s.pole, s.heldout, s.frame) for s in stmts)
    for d, (n_pairs, n_held) in EXPECTED.items():
        for pole in (1, -1):
            for frame in X.FRAMES:
                assert c[(d, pole, True, frame)] == n_held
                assert c[(d, pole, False, frame)] == n_pairs - n_held
    assert len(stmts) == sum(n for n, _ in EXPECTED.values()) * 4
    assert {s.direction for s in stmts} == set(X.DIRECTIONS)
    assert {s.pole for s in stmts} == {1, -1}


def test_held_out_share_of_templates():
    for d in X.DIRECTIONS:
        ps = X.pair_specs(d)
        held = {p["template_id"] for p in ps if p["heldout"]}
        allt = {p["template_id"] for p in ps}
        assert 0.2 <= len(held) / len(allt) <= 0.3


def test_each_pair_has_both_poles_in_both_frames_with_identical_text(stmts):
    by_pair = defaultdict(dict)
    for s in stmts:
        by_pair[s.pair_id][(s.frame, s.pole)] = s
    for pid, d in by_pair.items():
        assert set(d) == {(f, p) for f in X.FRAMES for p in (1, -1)}, pid
        for pole in (1, -1):
            assert d[("coding", pole)].text == d[("generic", pole)].text
        assert len({(s.template_id, s.heldout, s.direction) for s in d.values()}) == 1


def test_pairs_share_template_and_differ_only_in_clause():
    for d in X.DIRECTIONS:
        templates, clauses, _ = X.SPECS[d]
        for p in X.pair_specs(d):
            plus, minus = clauses[p["clause_id"]]
            assert p["template"] == templates[int(p["template_id"].split("-t")[1])]
            assert p["plus"] == X.fill(p["template"], plus)
            assert p["minus"] == X.fill(p["template"], minus)
            assert p["plus"] != p["minus"]
            # the template text outside the slot is shared verbatim
            pre, _, post = p["template"].replace("{C}", "{c}").partition("{c}")
            for t in (p["plus"], p["minus"]):
                assert t.startswith(pre) and t.endswith(post)


def test_pair_lengths_within_25_percent(stmts):
    for (pid, frame), ss in _pairs(stmts).items():
        assert len(ss) == 2 and {s.pole for s in ss} == {1, -1}
        a, b = (len(s.text) for s in ss)
        assert max(a, b) <= 1.25 * min(a, b), pid
        wa, wb = (len(s.text.split()) for s in ss)
        assert max(wa, wb) <= 1.5 * min(wa, wb), pid


def test_no_systematic_length_bias_between_poles():
    for d in X.DIRECTIONS:
        ps = X.pair_specs(d)
        mean_diff = sum(len(p["plus"]) - len(p["minus"]) for p in ps) / len(ps)
        assert abs(mean_diff) < 5, (d, mean_diff)


def test_held_out_split_by_template_and_clause():
    for d in X.DIRECTIONS:
        ps = X.pair_specs(d)
        t_split = defaultdict(set)
        c_split = defaultdict(set)
        for p in ps:
            t_split[p["template_id"]].add(p["heldout"])
            c_split[p["clause_id"]].add(p["heldout"])
        assert all(len(v) == 1 for v in t_split.values()), d   # no template split across
        assert all(len(v) == 1 for v in c_split.values()), d   # nor any clause
        held_texts = {p["plus"] for p in ps if p["heldout"]}
        train_texts = {p["plus"] for p in ps if not p["heldout"]}
        assert not held_texts & train_texts


def test_ids_unique_and_texts_unique(stmts):
    for d in X.DIRECTIONS:
        ps = X.pair_specs(d)
        assert len({p["pair_id"] for p in ps}) == len(ps)
        texts = [p["plus"] for p in ps] + [p["minus"] for p in ps]
        assert len(set(texts)) == len(texts), d
    keys = [(s.pair_id, s.pole, s.frame) for s in stmts]
    assert len(set(keys)) == len(keys)


def test_exclusions(stmts):
    sentences = [x for c in C.COND_BY_NAME.values() for x in c.sentences]
    assert sentences
    for s in stmts:
        assert "test_solution.py" not in s.text
        for x in sentences:
            assert x not in s.text and x.rstrip(".") not in s.text, (s.pair_id, x)


def test_direction_content():
    """Coarse guards on what each pole talks about."""
    for p in X.pair_specs("crit"):
        for t in (p["plus"], p["minus"]):
            assert "I'll" not in t and "I " not in t        # descriptive, not first-person intent
    for p in X.pair_specs("aware"):
        low = p["minus"].lower()
        assert not any(w in low for w in ("grad", "evaluat", "scor", "check", "judg", "review",
                                          "assess", "rate", "test", "mark"))
        assert "test" not in p["plus"].lower()             # awareness only, no criterion
    for d in ("self", "hack"):
        for p in X.pair_specs(d):
            for t in (p["plus"], p["minus"]):
                words = set(t.lower().replace(",", " ").replace(".", " ").split())
                assert words & {"i", "i'll", "i'm", "my", "me"}, t   # first person
    for p in X.pair_specs("self"):
        assert "grader" not in p["plus"].lower() and "grader" not in p["minus"].lower()


def test_deterministic():
    a, b = X.statements(), X.statements()
    assert a == b
    assert X.frame_messages("generic", 3) == X.frame_messages("generic", 3)
    assert (X.frame_messages("coding", 2, INITIAL) == X.frame_messages("coding", 2, INITIAL))
    assert X.frame_k("crit-t01-c02") == X.frame_k("crit-t01-c02")
    assert {X.frame_k(p["pair_id"]) for p in X.pair_specs("crit")} == set(range(4))


def _check_coding(msgs):
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    a, t = msgs[2], msgs[3]
    reasoning = [x for x in a["content"] if x["type"] == "reasoning"]
    assert len(reasoning) == 1 and reasoning[0]["reasoning"] == "Let me read the task first."
    assert reasoning[0]["redacted"] is False
    det = json.loads(reasoning[0]["signature"][len("reasoning-details://"):])
    assert det[0]["text"] == reasoning[0]["reasoning"]
    assert [x for x in a["content"] if x["type"] == "text"] == [{"type": "text", "text": ""}]
    (call,) = a["tool_calls"]
    assert call["function"] == "execute_command" and call["type"] == "function"
    assert "cat description.md" in call["arguments"]["command"]
    assert t["tool_call_id"] == call["id"] and t["function"] == "execute_command"
    assert t["content"].startswith("# ") and "def " in t["content"]


def test_frame_messages_coding_structure():
    contents = set()
    for k in range(8):
        msgs = X.frame_messages("coding", k, INITIAL)
        _check_coding(msgs)
        assert msgs[:2] == INITIAL
        contents.add(msgs[3]["content"])
    assert len(contents) == 4                                 # 4 toy problems, cycled by k
    assert X.frame_messages("coding", 1, INITIAL) == X.frame_messages("coding", 5, INITIAL)
    # returned dumps are copies: mutating them leaves the input untouched
    msgs = X.frame_messages("coding", 0, INITIAL)
    msgs[0]["content"] = "changed"
    assert INITIAL[0]["content"] == "You are a coding agent."


def test_frame_messages_coding_default_uses_sources(monkeypatch):
    from p47 import sources
    X._x3b_initial.cache_clear()
    monkeypatch.setattr(sources, "initial_messages", lambda: (INITIAL, "sha"))
    try:
        msgs = X.frame_messages("coding", 0)
        _check_coding(msgs)
        assert msgs[:2] == INITIAL
    finally:
        X._x3b_initial.cache_clear()


def test_frame_messages_rejects_bad_initial_and_frame():
    with pytest.raises(ValueError):
        X.frame_messages("coding", 0, INITIAL[:1])
    with pytest.raises(ValueError):
        X.frame_messages("other", 0)


def test_frame_messages_generic_structure():
    users = set()
    for k in range(4):
        msgs = X.frame_messages("generic", k)
        assert [m["role"] for m in msgs] == ["system", "user"]
        assert all(isinstance(m["content"], str) and m["content"] for m in msgs)
        assert not any(m.get("tool_calls") for m in msgs)
        users.add(msgs[1]["content"])
        low = msgs[1]["content"].lower()
        assert "hidden" not in low and "visible" not in low and "spec" not in low
    assert len(users) == 4


def test_toy_problems_are_not_benchmark_tasks():
    tasks = json.loads((PROBE / "data" / "tasks.json").read_text())["tasks"]
    entry_points = {t["entry_point"] for t in tasks}
    for name, *_ in X.TOY_PROBLEMS:
        assert name not in entry_points
        assert name not in json.dumps(tasks)
