import json

import numpy as np
from p33 import analyze as A
from p33 import items as I


def fake_rows(p: dict[str, float], n: int = 200, kind: str = "equation", seed: int = 0):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        u = rng.random()
        for arm, acc in p.items():
            rows.append({"item_id": f"{kind}:{i}", "idx": i, "kind": kind, "arm": arm,
                         "correct": bool(u < acc), "predicted": 1, "n_prompt": 100})
    return rows


def test_question_specific_passes_gate():
    p = {"base": 0.35, "dots100": 0.6, "maskB100": 0.38, "rep1": 0.4, "maskQ100": 0.4}
    s = A.summarize(fake_rows(p), I.V4_ARMS)
    g = A.gate(s)
    assert g["pass"] and g["retention_maskB100"]["est"] < 0.2


def test_survives_mask_b_fails_gate():
    p = {"base": 0.35, "dots100": 0.6, "maskB100": 0.58, "rep1": 0.4}
    g = A.gate(A.summarize(fake_rows(p), I.V4_ARMS))
    assert g["decided"] and not g["question_specific"] and not g["pass"]


def test_repetition_replacing_fails_gate():
    p = {"base": 0.35, "dots100": 0.6, "maskB100": 0.36, "rep1": 0.6}
    g = A.gate(A.summarize(fake_rows(p), I.V4_ARMS))
    assert g["question_specific"] and not g["repetition_doesnt_replace"]


def test_load_dedupes(tmp_path):
    (tmp_path / "v4").mkdir()
    rows = fake_rows({"base": 0.5}, n=3)
    (tmp_path / "v4" / "answers.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows + rows[:1]) + "\n")
    assert len(A.load("v4", tmp_path)) == 3
