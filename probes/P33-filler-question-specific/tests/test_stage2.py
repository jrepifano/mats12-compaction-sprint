import numpy as np
import pytest
import torch
from p33 import donors as D
from p33 import items as I
from p33 import masks as M
from p33 import stage2 as S
from test_masks import tiny_v4


def eq_examples():
    items, _ = I.load_items()
    return [it["ex"] for it in items if it["kind"] == "equation"]


def test_donors_have_the_promised_values():
    exs = eq_examples()
    n_full = 0
    for ex in exs:
        dn = D.donors(ex, exs)
        p = D.parse(ex)
        if "sameY" in dn:
            assert dn["sameY"]["ex"]["queried_value"] == p["y"]
            assert dn["sameY"]["ex"]["answer"] == ex["answer"]
            assert dn["sameY"]["ex"]["definitions"] != ex["definitions"]
        if "opChange" in dn:
            n_full += 1
            d, o = dn["diffY"], dn["opChange"]
            assert d["ex"]["question"] == ex["question"] and d["y"] != p["y"]
            assert d["donor_answer"] == d["hybrid"] == D.q(ex, d["y"])
            assert o["ex"]["question"] != ex["question"]
            assert o["ex"]["queried_value"] == d["y"]
            assert len({o["donor_answer"], o["hybrid"], ex["answer"]}) == 3
            assert all(0 <= v <= 999 for v in (o["donor_answer"], o["hybrid"]))
            # only numbers and plus/minus change: the same word count
            assert len(o["ex"]["question"].split()) == len(ex["question"].split())
    assert n_full >= 180


def test_explicit_y():
    ex = eq_examples()[0]
    e = D.explicit_y(ex, 50)
    assert dict(e["definitions"])[ex["queried_term"]] == 50 and e["answer"] == D.q(ex, 50)


def test_group_span_splits_at_a_csa_boundary():
    sp = I.Spans(list(range(300)), 10, 101, 201, 201)
    a, b = S.group_span(sp, "early")
    c, d = S.group_span(sp, "late")
    assert (a, d) == (101, 201) and b == c and b % 4 == 0 and 145 <= b <= 155


def test_select_sites_single_pair_and_none():
    def rows(acc):
        out = []
        for site, p in acc.items():
            unit = "unpatched" if site == "none" else "ablate"
            out += [{"unit": unit, "site": site, "is_answer": i < p * 100} for i in range(100)]
        return out

    base = {"none": 0.6, "all|all": 0.3}
    singles = {f"{b}|{g}": 0.6 for b in S.BANDS for g in S.GROUPS}
    s = S.select_sites(rows({**base, **singles, "b1|late": 0.35, "b1|all": 0.32}))
    assert s["compact"] and s["frozen"] == {"bands": ["b1"], "group": "late"}
    s = S.select_sites(rows({**base, **singles, "b1|all": 0.5, "b3|all": 0.45}))
    assert s["pair_needed"] == ["b1", "b3"]
    s2 = S.finish_pair(s, rows({**base, **singles, "b1+b3|all": 0.33}))
    assert s2["compact"] and s2["frozen"]["bands"] == ["b1", "b3"]
    s = S.select_sites(rows({"none": 0.6, "all|all": 0.58, **singles}))
    assert not s["compact"] and s["frozen"]["bands"] == list(S.BANDS)


@pytest.fixture(scope="module")
def runner():
    m = tiny_v4(4)
    parts = type("Pt", (), {"blocks": m.model.layers})()
    ctx = type("C", (), {"model": m, "parts": parts, "int_ids": list(range(10))})()
    return S.Runner(ctx, device="cpu")


def test_runner_self_patch_and_ablation_equal_mask_b(runner, monkeypatch):
    monkeypatch.setattr(S, "N_LAYERS", 5)
    ids = list(torch.randint(2, 97, (64,), generator=torch.Generator().manual_seed(3)).tolist())
    dots = np.arange(36, 56)
    for p in dots:
        ids[p] = 5
    lp, own, _ = runner.forward(ids, capture_pos=dots)
    lp_self, _, _ = runner.forward(ids, patch=(list(range(5)), dots, own))
    assert torch.allclose(lp, lp_self, atol=1e-5)
    rule = [M.Rule(36, 56, 0, 36)]
    lp_b, abl, cnt = runner.forward(ids, rules=rule, capture_pos=dots)
    assert cnt
    lp_abl, _, _ = runner.forward(ids, patch=(list(range(5)), dots, abl))
    assert torch.allclose(lp_b, lp_abl, atol=1e-4)  # all-layer ablation == Mask B
    assert not torch.allclose(lp, lp_b, atol=1e-4)
    # a knockout restricted to some layers fires only there
    kr = [M.Rule(56, 64, 36, 56, frozenset({2, 3}))]
    _, _, cnt = runner.forward(ids, rules=kr)
    assert set(cnt["window"]) == {"2", "3"}
