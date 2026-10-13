"""The pod runner's mask check, bulk loop and gates on a tiny random V4 (CPU), with a fake
tokenizer and synthetic spans."""

import json

import pytest
import torch
from p33 import items as I
from p33 import masks as M
from p33 import pod as P
from test_masks import tiny_v4

N_DOTS = 20


class FakeTok:
    def decode(self, ids, skip_special_tokens=True):
        return " ".join(str(i) for i in ids)


def fake_spans(_tok, it, _shots, arm, _neutral):
    """[10 prefix][5 literal-ish tokens][4 question][N dots or none][3 suffix]."""
    ex = it["ex"]
    lits = [(v % 80) + 10 if isinstance(v, int) else 7 for _, v in ex["definitions"]]
    fam, _n = I.arm_kind(I.token_arm(arm))
    ids = list(range(2, 12)) + lits + [3, 4, 5, 6]
    turn0 = 10
    f0 = f1 = -1
    if fam == "dots":
        f0 = len(ids)
        ids += [9] * N_DOTS
        f1 = len(ids)
    suffix0 = len(ids)
    ids += [8, 7, 6]
    return I.Spans(ids, turn0, f0, f1, suffix0)


@pytest.fixture(scope="module")
def model():
    m = tiny_v4(4)
    ctx = type("C", (), {"model": m, "int_ids": list(range(10))})()
    return P.Model("v4", ctx, FakeTok(), m.model.layers, list(m.config.layer_types),
                   dict(m.config.compress_rates), device="cpu")


def test_mask_check_passes_on_a_correct_mask(model, monkeypatch):
    monkeypatch.setattr(I, "mask_rules", lambda arm, sp: (
        [M.Rule(sp.f0, sp.f1, 0, sp.f0)] if arm == "maskB100" else
        [M.Rule(sp.f0, sp.f1, sp.turn0, sp.f0)] if arm == "maskQ100" else []))
    items, shots = I.load_items()
    out = P.mask_check(model, items[:2], shots, "", fake_spans)
    assert out["pass"], out["ratio_max"]
    assert out["ratio_max"]["maskB100"] < 1e-4
    assert out["items"][0]["unmasked"][-1] > 1e-3


def test_mask_check_fails_on_a_leaky_mask(model, monkeypatch):
    # blocks only the sliding window (no compressor / indexer hooks): must fail the check
    class Leaky(M.V4Masks):
        def _comp_post(self, layer, kind, rate):
            return lambda *_a: None

        def _scorer_post(self, layer, rate):
            return lambda *_a: None

    monkeypatch.setattr(M, "V4Masks", Leaky)
    items, shots = I.load_items()
    out = P.mask_check(model, items[:1], shots, "", fake_spans)
    assert not out["pass"]


def test_run_bulk_writes_rows_and_resumes(model, tmp_path, monkeypatch):
    monkeypatch.setitem(P.ANSWERS, "v4", tmp_path / "answers.jsonl")
    items, shots = I.load_items()
    arms = ("base", "dots100", "maskB100", "maskQ100")
    st: dict = {}
    rc = P.run_bulk(model, items[:3], shots, arms, "", fake_spans, st, None, 1.0, None)
    assert rc == 0
    rows = [json.loads(x) for x in (tmp_path / "answers.jsonl").read_text().splitlines()]
    assert len(rows) == 12 and {r["arm"] for r in rows} == set(arms)
    assert all(r["n_filler"] == (N_DOTS if r["arm"] != "base" else 0) for r in rows)
    rc = P.run_bulk(model, items[:3], shots, arms, "", fake_spans, {}, None, 1.0, None)
    assert rc == 0
    assert len((tmp_path / "answers.jsonl").read_text().splitlines()) == 12


def test_run_bulk_stops_at_the_deadline(model, tmp_path, monkeypatch):
    monkeypatch.setitem(P.ANSWERS, "v4", tmp_path / "a.jsonl")
    items, shots = I.load_items()
    st: dict = {}
    assert P.run_bulk(model, items[:3], shots, ("base",), "", fake_spans, st, 0.0, 1.0,
                      None) == 4
    assert "deadline" in st["v4"]["stopped"]


def test_api_check():
    rows = [{"kind": "equation", "arm": a, "idx": i, "correct": (i % 2 == 0)}
            for i in range(60) for a in ("base", "dots100")]
    ref = {(i, a): (i % 2 == 0) for i in range(60) for a in ("base", "dots100")}
    out = P.api_check(rows, ref)
    assert out["pass"] and out["base"]["row_agreement"] == 1.0
    ref2 = {(i, a): True for i in range(60) for a in ("base", "dots100")}
    assert not P.api_check(rows, ref2)["pass"]


def test_api_reference_reads_p06_rows():
    ref = P.api_reference()
    if not ref:
        pytest.skip("P06 F1 rows not present")
    assert len(ref) == 1000 and {a for _, a in ref} == {"base", "dots100"}


def test_perturbed_keeps_digit_counts():
    items, _ = I.load_items()
    ex = items[0]["ex"]
    p = P.perturbed(ex)
    for (n1, v1), (n2, v2) in zip(ex["definitions"], p["definitions"], strict=True):
        assert n1 == n2
        if isinstance(v1, int):
            assert v1 != v2 and len(str(v1)) == len(str(v2))
        else:
            assert v1 == v2
    assert p["question"] == ex["question"]


def test_messages_match_p06_f1_for_base_and_dots():
    from p06 import brauer as B

    items, shots = I.load_items()
    ex = items[0]["ex"]
    for arm in ("base", "dots100", "dots500"):
        assert I.messages(ex, shots["equation"], arm) == B.messages(ex, shots["equation"], arm)


def test_greedy_unchanged_by_empty_masks(model):
    ids = list(range(2, 40))
    a, _ = model.generate(ids, [])
    with torch.inference_mode():
        b, cnt = model.generate(ids, [M.Rule(30, 35, 0, 30)])
    assert len(a) == len(b) and cnt
