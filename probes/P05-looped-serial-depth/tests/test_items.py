"""p05.items: banks, nudge depth, prompts, prefixes (no model needed)."""

from __future__ import annotations

from p05 import items as I
from p05.nocot import arithmetic, chain


def test_chain_bank_counts_and_solver():
    evals, shots = I.chain_items()
    assert len(shots) == 10
    assert sorted(s["k"] for s in shots if s["fmt"] == "nocot") == [2, 2, 2, 3, 3]
    assert sorted(s["k"] for s in shots if s["fmt"] == "brauer") == [1, 2, 3, 3, 4]
    ks = {}
    for e in evals:
        ks[e.get("k_generated", e["k"])] = ks.get(e.get("k_generated", e["k"]), 0) + 1
        assert I.walk(e["start"], e["ops"])[-1] == e["answer"] == e["traj"][-1]
        assert e["k"] == e["nudge_depth"]
        item = chain.Item(domain="chain", problem_number=0, problem=e["problem"],
                          answer=e["answer"], instruction=e["instruction"], chance=0,
                          difficulty=e["k"], rung=None, split="eval", answer_type="int")
        assert chain.solve(item) == e["answer"]
    assert all(ks[k] == 300 for k in (2, 3, 4, 5, 6, 8)) and 150 < ks[1] <= 300
    assert len({e["problem"] for e in evals + shots}) == len(evals) + len(shots)
    assert 0.05 < evals[0]["chance"] < 0.15


def test_banks_are_deterministic():
    a, sa = I.chain_items(n_per_k=20)
    b, sb = I.chain_items(n_per_k=20)
    assert I.bank_sha(a, sa) == I.bank_sha(b, sb)


def test_arithmetic_bank():
    evals, shots = I.arithmetic_items()
    assert len(evals) == 700 and len(shots) == 10
    for e in evals[::37]:
        item = arithmetic.Item(domain="arithmetic", problem_number=0, problem=e["problem"],
                               answer=e["answer"], instruction=e["instruction"], chance=0,
                               difficulty=e["k"], rung=None, split="eval", answer_type=None)
        assert arithmetic.solve(item) == e["answer"]
        assert e["problem"].count("(") == e["k"]


def test_chain_easy_has_no_gt10_rule():
    evals, _ = I.chain_items("chain_easy", n_per_k=30)
    assert all(op[0] != "gt10sub" for e in evals for op in e["ops"])


def test_nudge_depth():
    # halve-up from 3 -> 2: a different start changes the answer; one load-bearing step
    assert I.nudge_depth(3, [["halveup", 0]]) == 1
    ops = [["gt10sub", 5], ["halveup", 0], ["evenhalve", 3]]
    assert I.nudge_depth(12, ops) == 3
    assert I.nudge_depth(12, ops, pm1=True) <= 3


def test_prompts_end_with_answer_and_formats_differ():
    evals, shots = I.chain_items(n_per_k=5)
    e = evals[7]
    p1, p2 = I.render_prompt(e, shots, "nocot"), I.render_prompt(e, shots, "brauer")
    assert p1.endswith("\nAnswer:") and p2.endswith("\n\nAnswer:")
    assert p1.count(e["instruction"]) == 6 and p2.count(e["instruction"]) == 1
    assert p1.count("Problem:") == p2.count("Problem:") == 6
    shot_text = {f: [s["problem"] for s in shots if s["fmt"] == f] for f in I.FORMATS}
    assert all(p in p1 for p in shot_text["nocot"])
    assert not any(p in p1 for p in shot_text["brauer"])
    assert all(p in p2 for p in shot_text["brauer"])
    assert not any(p in p2 for p in shot_text["nocot"])
    assert I.gold_continuation(e) == f" {e['answer']}"


def test_prefix_items_and_eligibility():
    evals, _ = I.chain_items(n_per_k=40)
    el = [e for e in evals if I.readout_eligible(e)]
    assert el and all(e["k"] in (2, 3, 4) for e in el)
    for e in el:
        assert len(set(e["traj"][1:])) == e["k"]
        for j in range(1, e["k"]):
            p = I.prefix_item(e, j)
            assert p["answer"] == e["traj"][j] and p["k"] == j and p["base_id"] == e["item_id"]
            assert p["problem"].count("\n") == j + 1
            assert I.walk(p["start"], p["ops"])[-1] == p["answer"]
