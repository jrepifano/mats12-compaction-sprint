"""Unit tests for P06 Stage 1: items, prompts, scoring and the analysis arithmetic (no API)."""

import types

import numpy as np
import pytest

from p06 import analyze, api, items
from p06.nocot import chain


@pytest.fixture(scope="module")
def chain_data():
    return items.chain_items()


def test_chain_items_shape_and_gold(chain_data):
    ev, shots = chain_data
    assert len(ev) == 700 and len(shots) == 1
    assert {e["k"] for e in ev} == set(range(2, 9))
    assert len({e["problem"] for e in ev}) == 700
    for e in ev:
        assert chain.solve(types.SimpleNamespace(problem=e["problem"])) == e["answer"]
        assert len(e["traj"]) == e["k"] + 1 and e["traj"][-1] == e["answer"]
        assert all(1 <= v <= 20 for v in e["traj"])


def test_chain_items_deterministic(chain_data):
    ev2, _ = items.chain_items()
    assert [e["problem"] for e in ev2] == [e["problem"] for e in chain_data[0]]


def test_trajectory_example():
    p = ("Start with the number 11 and apply the steps in order. After every step, if the "
         "number is bigger than 20, subtract 20; if it is smaller than 1, add 20.\n"
         "If it is even, halve it; if it is odd, add 7.\nHalve it, rounding up.\n"
         "If it is bigger than 10, subtract 9; otherwise double it.\nWhat is the final number?")
    assert items.trajectory(p) == [11, 18, 9, 18]


def test_mc_items(chain_data):
    ev, shots = chain_data
    mc, mc_shots = items.mc_items(ev, shots)
    assert len(mc) == 900
    for m in mc:
        assert m["answer"] in m["options"] and len(set(m["options"])) == len(m["options"])
        assert m["problem"].endswith("Options: " + ", ".join(map(str, m["options"])))
    assert sorted(mc_shots) == [2, 5, 10]


def test_shots10_distinct(chain_data):
    ev, _ = chain_data
    s10 = items.chain_shots10(ev)
    assert [s["k"] for s in s10] == list(items.SHOTS10_KS)
    assert not {s["problem"] for s in s10} & {e["problem"] for e in ev}


def test_filler_string():
    assert api.FILLER.count(".") == 500 and api.FILLER.split(" ") == ["."] * 500


def test_user_turn_arms(chain_data):
    it = chain_data[0][0]
    none, after, before = (api.user_turn(it, a) for a in api.ARMS)
    assert api.FILLER not in none
    assert after.endswith(api.FILLER) and after.index("Problem:") < after.index(api.FILLER)
    assert before.index(api.FILLER) < before.index("Problem:")
    assert len(after) == len(before)


def test_build_messages_placement(chain_data):
    ev, shots = chain_data
    s10 = items.chain_shots10(ev)
    tgt = api.build_messages(ev[0], s10, "after", placement="target")
    every = api.build_messages(ev[0], s10, "after", placement="every")
    assert len(tgt) == len(every) == 22 and tgt[-1] == {"role": "assistant", "content": "Answer:"}
    assert sum(api.FILLER in m["content"] for m in tgt) == 1
    assert sum(api.FILLER in m["content"] for m in every) == 11
    assert every[1]["content"] == f"Answer: {s10[0]['answer']}"
    none = api.build_messages(ev[0], s10, "none", placement="every")
    assert not any(api.FILLER in m["content"] for m in none)


def test_build_body_recipe():
    r = api.RECIPES["gemma4"]
    b = api.build_body(r, [{"role": "user", "content": "x"}], 0.0)
    assert b["reasoning"] == {"enabled": False} and b["max_tokens"] == 100
    assert b["provider"]["allow_fallbacks"] is False and b["temperature"] == 0.0
    b2 = api.build_body(r, [], 0.0, reasoning={"enabled": True}, max_tokens=2000)
    assert b2["reasoning"] == {"enabled": True}


def _resp(text, rtok=0, prompt=100, completion=3):
    return {"text": text, "reasoning_text": "", "finish_reason": "stop",
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "total_tokens": prompt + completion,
                      "completion_tokens_details": {"reasoning_tokens": rtok}}}


def test_score_clean_correct_and_reasoned():
    item = {"answer": 13, "answer_type": "int", "bank": "chain"}
    s = api.score(_resp(" 13"), item)
    assert s["correct"] and s["valid"]
    s = api.score(_resp(" 13", rtok=50), item)  # reasoned -> invalid, scored wrong
    assert not s["correct"] and not s["valid"]
    s = api.score(_resp(" 12"), item)
    assert not s["correct"] and s["valid"]


def test_boot_mean_diff():
    rng = np.random.default_rng(0)
    r = analyze.boot_mean_diff(np.ones(50) * 0.1, rng, n_boot=500)
    assert r["mean"] == pytest.approx(0.1) and r["lo"] == pytest.approx(0.1)


def test_fit_k50_recovers():
    rng = np.random.default_rng(1)
    c, a, b = 0.07, 6.0, 1.5  # k50 = 4
    k = np.repeat(np.arange(2, 9), 400).astype(float)
    p = c + (1 - c) / (1 + np.exp(-(a - b * k)))
    y = (rng.random(len(k)) < p).astype(float)
    assert analyze.fit_k50(k, y, c) == pytest.approx(4.0, abs=0.15)


def test_uplift_table_paired():
    rows = []
    for i in range(20):
        for arm, corr in (("none", 0), ("before", 0), ("after", int(i < 10))):
            rows.append({"item_id": f"x{i}", "k": 2, "bank": "chain", "arm": arm,
                         "correct": corr, "valid": True})
    t = analyze.uplift_table(rows, "chain", np.random.default_rng(0))
    assert t["all"]["after_minus_before"]["mean"] == pytest.approx(0.5)
    assert t["k2"]["acc"]["after"] == pytest.approx(0.5)
