"""Tests for p15.tasks (task sets, training mix, parsers)."""

import os
from collections import Counter

import pandas as pd
import pytest
from p15 import tasks

FAKE_ADD = [f"Add{i}: {{X}} plus {{Y}}, times {{Z}}. Give the answer only." for i in range(10)]
FAKE_SUB = [f"Sub{i}: {{X}} minus {{Y}}, times {{Z}}. Give the answer only." for i in range(10)]


@pytest.fixture(scope="module")
def splits():
    return tasks.make_C_splits()


def test_T_counts_golds_and_ids():
    T = tasks.make_T(FAKE_ADD, FAKE_SUB)
    assert len(T) == 4480
    assert Counter(it["template_id"] for it in T) == {i: 224 for i in range(20)}
    assert len({it["item_id"] for it in T}) == 4480
    assert [it["idx"] for it in T] == list(range(4480))
    for it in T:
        s1 = it["X"] + it["Y"] if it["op"] == "add" else it["X"] - it["Y"]
        assert it["op"] == ("add" if it["template_id"] < 10 else "sub")
        assert 1 <= it["Y"] < it["X"] <= 8 and 1 <= it["Z"] <= 8
        assert it["s1"] == s1 and it["s2"] == s1 * it["Z"] and it["gold"] == s1 + s1 * it["Z"]
        assert "{" not in it["prompt"]
    it = T[0]
    assert it["prompt"] == f"Add0: {it['X']} plus {it['Y']}, times {it['Z']}. Give the answer only."
    assert len({tasks.cluster_id(it) for it in T}) == 224


@pytest.mark.skipif(not os.environ.get("P15_UPSTREAM"), reason="P15_UPSTREAM not set")
def test_upstream_templates():
    add, sub = tasks.load_upstream_templates(os.environ["P15_UPSTREAM"])
    assert len(add) == 10 and len(sub) == 10
    assert len(tasks.make_T(add, sub)) == 4480


G_ROWS = {
    "question": ["Ann has  3 cats. ", "Cost?"],
    "answer": ["3 cats.\n#### 1,234", "half\n#### 2.5"],
}


def test_G_items():
    G = tasks.G_items(G_ROWS["question"], G_ROWS["answer"])
    assert G[0]["gold"] == 1234 and isinstance(G[0]["gold"], int)
    assert G[1]["gold"] == 2.5
    assert G[0]["prompt"] == "Ann has 3 cats. Output only the answer and nothing else."
    assert G[0]["item_id"] == "G-0000" and G[0]["set"] == "G"
    assert tasks.cluster_id(G[1]) == "G-0001"


def test_load_G_parquet(tmp_path):
    pytest.importorskip("pyarrow")  # pod venv has it; the repo venv may not
    p = tmp_path / "g.parquet"
    pd.DataFrame(G_ROWS).to_parquet(p)
    assert tasks.load_G(p) == tasks.G_items(G_ROWS["question"], G_ROWS["answer"])


def _recompute(X, steps):
    out, v = [], X
    for op, a in steps:
        v = v + a if op == "+" else v - a if op == "-" else v * a
        out.append(v)
    return out


def test_C_constraints(splits):
    assert {k: len(v) for k, v in splits.items()} == {"train": 24000, "val": 512, "eval": 6000}
    for name, items in splits.items():
        ks = Counter(it["k"] for it in items)
        want = {2, 3, 4, 5} if name == "train" else {3, 4, 5}  # 2-hop curriculum in train only
        assert set(ks) == want and max(ks.values()) - min(ks.values()) <= 1
        for it in items:
            assert 1 <= it["X"] <= 30
            assert len(it["steps"]) == it["k"] == len(it["inter"]) == len(it["phrasing"])
            assert _recompute(it["X"], it["steps"]) == it["inter"]
            assert it["inter"][-1] == it["gold"]
            assert all(1 <= v <= 199 for v in it["inter"])
            assert sum(op == "*" for op, _ in it["steps"]) <= 2
            for op, a in it["steps"]:
                assert (op in "+-" and 1 <= a <= 20) or (op == "*" and a in (2, 3))
            assert it["op_pattern"] == tasks.op_pattern(it["steps"])
            assert 0 <= it["scenario"] <= 7 and all(p in (0, 1, 2) for p in it["phrasing"])
            assert it["prompt"].endswith(tasks.SUFFIX)
        assert len({tasks.c_key(it) for it in items}) == len(items)
        assert len({it["item_id"] for it in items}) == len(items)
    assert {it["set"] for it in splits["eval"]} == {"C"}
    assert {it["set"] for it in splits["train"]} == {"Ctrain"}


def test_C_splits_disjoint(splits):
    keys = {name: {tasks.c_key(it) for it in items} for name, items in splits.items()}
    assert not keys["train"] & keys["eval"]
    assert not keys["train"] & keys["val"]
    assert not keys["val"] & keys["eval"]


def test_C_deterministic():
    a = tasks.make_C(200, 7)
    assert a == tasks.make_C(200, 7)
    assert a != tasks.make_C(200, 8)


def test_C_cot_format(splits):
    for it in splits["eval"][:2000]:
        assert it["cot"].endswith(".")
        sents = it["cot"].split(". ")
        assert len(sents) == it["k"]
        assert all(". " not in s.rstrip(".") for s in sents)
        # Each sentence states its step result after "=", and the last sentence is the final step.
        assert tasks.cot_results(it["cot"]) == it["inter"]
        assert f"= {it['gold']} " in sents[-1]
        # Upstream drops the last sentence: the kept part ends at step k - 1.
        assert tasks.cot_results(". ".join(sents[:-1])) == it["inter"][:-1]


def test_C_question_text():
    it = tasks.make_C(1, 3)[0]
    q = it["prompt"][: -len(tasks.SUFFIX)]
    place, plural, _ = tasks.SCENARIOS[it["scenario"]]
    assert q.startswith(f"A {place} has {it['X']} ")
    assert q.endswith(f"How many {plural} are there now?")
    assert q.count(".") == it["k"] + 1


def test_icot_record(splits):
    it = splits["train"][0]
    rec = tasks.to_icot_record(it)
    assert set(rec) == {"question", "cot", "answer"}
    assert rec["question"] + tasks.SUFFIX == it["prompt"]
    assert not rec["question"].endswith(tasks.SUFFIX)
    assert rec["answer"] == str(it["gold"]) and rec["cot"] == it["cot"]


def test_mix_train():
    c = [{"question": f"c{i}", "cot": "x", "answer": "1"} for i in range(50)]
    g = [{"question": f"g{i}", "cot": "y", "answer": "2", "extra": 0} for i in range(200)]
    m = tasks.mix_train(c, g, n_gsm=100, seed=1)
    assert len(m) == 150
    assert m == tasks.mix_train(c, g, n_gsm=100, seed=1)
    qs = [r["question"] for r in m]
    assert sum(q.startswith("c") for q in qs) == 50 and len(set(qs)) == 150
    assert all(set(r) == {"question", "cot", "answer"} for r in m)
    assert qs[:50] != [f"c{i}" for i in range(50)]


def test_parse_answer():
    assert tasks.parse_answer("The answer is 1,234") == 1234
    assert tasks.parse_answer("3 then 42<|eot_id|>") == 42
    assert tasks.parse_answer("<|eocot|>17") == 17
    assert tasks.parse_answer("-5") == -5
    assert tasks.parse_answer("2.5") == 2.5
    assert tasks.parse_answer("18.0") == 18 and isinstance(tasks.parse_answer("18.0"), int)
    assert tasks.parse_answer("It is 7.") == 7
    assert tasks.parse_answer("no number here") is None
    assert tasks.parse_answer("") is None


def test_parse_verbal():
    assert tasks.parse_verbal("4 + 3 = 7. 7 * 2 = 14.<|eocot|>21 and 5") == (21, "after_eocot")
    assert tasks.parse_verbal("a = 1,000<|eocot|> 2,500") == (2500, "after_eocot")
    assert tasks.parse_verbal("4 + 3 = 7<|eocot|>none") == (None, "fail")
    assert tasks.parse_verbal("4 + 3 = 7. so 21") == (21, "last_number")
    assert tasks.parse_verbal("nothing") == (None, "fail")
    assert tasks.parse_verbal("<|eocot|>-3") == (-3, "after_eocot")


def test_cot_results_and_new_numbers():
    text = "The farm has 12 + 5 = 17 sheep. 17 * 2 = $34. x = -3. y = 1,200.<|eocot|>99 = 5"
    assert tasks.cot_results(text) == [17, 34, -3, 1200]
    assert tasks.cot_results("no equations") == []
    assert tasks.cot_results("half = 2.5 kg") == [2.5]
    q = "A farm has 12 sheep. 5 more arrive."
    assert tasks.cot_new_numbers(text, q) == [17, 2, 34, 3, 1200]
    assert tasks.cot_new_numbers("2.5 then 4 then 4", "nothing") == [4]


def test_cluster_id_C():
    it = tasks.make_C(1, 0)[0]
    assert tasks.cluster_id(it) == f"{it['op_pattern']}|{it['X']}"


def test_jsonl_roundtrip(tmp_path):
    import numpy as np

    rows = [{"a": 1, "b": [1, 2]}, {"a": np.int64(3), "c": np.float32(0.5), "d": None}]
    p = tmp_path / "sub" / "x.jsonl"
    tasks.write_jsonl(p, rows)
    assert tasks.read_jsonl(p) == [{"a": 1, "b": [1, 2]}, {"a": 3, "c": 0.5, "d": None}]
