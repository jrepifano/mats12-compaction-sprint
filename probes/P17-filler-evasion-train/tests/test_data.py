"""p17.data on tiny synthetic items (no generated file needed, except the skip-guarded check)."""

from __future__ import annotations

import json

import pytest
from p17 import data, schema


def item(idx, x=54, c1="twice", op1="minus", k1=23, c2=2, op2="minus", k2=3,
         names=("lal", "lef", "cok", "rin", "wib"), order=None):
    """A depth-1 Brauer-style item: names[2] = c1 * names[1] op1 k1; question on names[2]."""
    a, b, q, r, w = names
    coef = {"twice": 2, "three times": 3}[c1]
    y = coef * x + k1 if op1 == "plus" else coef * x - k1
    ans = c2 * y + k2 if op2 == "plus" else c2 * y - k2
    defs = [[a, 11], [b, x], [q, f"{c1} the number for {b} {op1} {k1}"],
            [r, f"three times the number for {a} plus 25"], [w, 12]]
    if order is not None:
        defs = [defs[i] for i in order]
    cw = {2: "twice", 3: "three times"}[c2]
    return {"idx": idx, "type": "chained_var_binding", "definitions": defs, "queried_term": q,
            "queried_value": y, "coefficient": c2, "operation": op2, "constant": k2,
            "num_terms": 5, "chain_len": 1,
            "question": f"What is {cw} the number for {q} {op2} {k2}?", "answer": ans}


def test_item_helper_is_valid():
    cv = data.S2.chain_values(item(0))
    assert cv["y"] == 2 * 54 - 23 and cv["answer"] == 2 * cv["y"] - 3


def test_overlap_detection_is_order_free():
    ev = [item(0)]
    # Same definitions in another order: still an overlap (topologically valid reorder).
    assert data.check_no_overlap([item(5, order=[1, 0, 2, 4, 3])], ev) == 1
    assert data.check_no_overlap([item(5, x=55)], ev) == 0
    assert data.check_no_overlap([item(5), item(6, x=60), item(7)], ev) == 2


def test_filter_drops_overlap_dup_invalid_and_reindexes():
    ev = [item(0)]
    raw = [item(10),                                  # overlaps eval: dropped
           item(11, x=60),                            # kept -> idx 0
           item(12, x=60, names=("lal", "lef", "cok", "rin", "wib"), order=[1, 0, 2, 4, 3]),
           item(13, x=10, c1="twice", op1="minus", k1=50),  # y = -30: invalid
           item(14, x=61),                            # kept -> idx 1
           item(15, x=62)]                            # beyond n: not reached
    out, drops = data.filter_items(raw, ev, n=2)
    assert drops == {"overlap": 1, "dup": 1, "invalid": 1}
    assert [e["idx"] for e in out] == [0, 1]
    assert [e["gen_idx"] for e in out] == [11, 14]
    assert raw[1]["idx"] == 11  # input not mutated


def test_dup_key_ignores_line_order_but_not_question():
    a = item(1, x=60)
    assert data.dup_key(a) == data.dup_key(item(2, x=60, order=[1, 0, 2, 4, 3]))
    assert data.dup_key(a) != data.dup_key(item(3, x=60, k2=4))


def test_filter_raises_when_too_few_survive():
    with pytest.raises(ValueError):
        data.filter_items([item(1)], [item(0)], n=1)


def test_invalid_resolution_raises():
    bad = item(1, x=60)
    bad["queried_value"] += 1
    with pytest.raises(ValueError):
        data.filter_items([bad], [], n=1)


def test_ask_example():
    ex = item(3, x=40)
    a = data.ask_example(ex)
    assert a["question"] == "What is the number for cok?"
    assert a["answer"] == ex["queried_value"] == 57 and a["ask"] is True
    assert {k: v for k, v in a.items() if k not in ("question", "answer", "ask")} == \
        {k: v for k, v in ex.items() if k not in ("question", "answer")}
    assert ex["question"].startswith("What is twice") and "ask" not in ex  # original intact


def test_shots_check_and_load_train(tmp_path):
    shots = [item(100 + i, x=20 + i) for i in range(5)]
    items = [dict(item(i, x=30 + i), gen_idx=i) for i in range(3)]
    p = tmp_path / "train.json"
    p.write_text(json.dumps({"n": 3, "few_shot_examples": shots, "examples": items}))
    assert data.load_train(p, eval_shots=shots) == items
    other = [*shots[:4], item(200, x=90)]
    with pytest.raises(AssertionError):
        data.load_train(p, eval_shots=other)
    with pytest.raises(AssertionError):
        data.check_shots(shots[:5], shots[:4])


def test_build_on_synthetic(tmp_path):
    ev = {"chain_len": 1, "num_terms": 5, "few_shot_examples": [item(90 + i, x=70 + i)
                                                                for i in range(8)],
          "examples": [item(0)]}
    raw = {"chain_len": 1, "num_terms": 5, "few_shot_examples": [],
           "examples": [item(10), item(11, x=71), item(12, x=60), item(13, x=61)]}
    (tmp_path / "ev.json").write_text(json.dumps(ev))
    (tmp_path / "raw.json").write_text(json.dumps(raw))
    out = tmp_path / "out" / "train.json"
    s = data.build(tmp_path / "raw.json", tmp_path / "ev.json", out, n=2)
    assert s["n_dropped_overlap"] == 2  # an eval item and a shot
    doc = json.loads(out.read_text())
    assert doc["few_shot_examples"] == ev["few_shot_examples"][:5]
    assert [e["gen_idx"] for e in doc["examples"]] == [12, 13]
    assert doc["seed"] == schema.SEED and s["sha256"] == data.sha256(out)


@pytest.mark.skipif(not (schema.TRAIN_DATA.exists() and schema.EVAL_DATA.exists()),
                    reason="generated data/P17/train_items.json or data/P06 eval file absent")
def test_generated_file():
    train = data.load_train()
    eval_, shots = data.load_eval()
    assert len(train) == schema.N_TRAIN and len(eval_) == schema.N_EVAL and len(shots) == 5
    assert data.check_no_overlap(train, eval_) == 0
    assert len({data.dup_key(e) for e in train}) == len(train)
    for ex in train[:50]:
        data.S2.chain_values(ex)
