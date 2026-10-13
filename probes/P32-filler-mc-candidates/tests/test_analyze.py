"""Analysis on synthetic cells: shares, verdicts, the item-set rule and the model comparison."""

import numpy as np
from p32 import analyze as A


def make_cells(acc_base, acc_dots, n_items=400, invalid_base10=0.0, seed=0):
    rs = np.random.default_rng(seed)
    c = {f: {} for f in A.FAMILIES}
    for f in A.FAMILIES:
        for i in range(n_items):
            cc = {}
            for j, n in enumerate((2, 5, 10)):
                for kind in ("near", "far"):
                    for arm, p in (("base", acc_base[j]), ("padmid", acc_base[j]),
                                   ("pad", acc_base[j]), ("dots_before", acc_dots[j]),
                                   ("dots_after", acc_dots[j])):
                        valid = int(not (arm == "base" and n == 10 and rs.random() < invalid_base10))
                        cc[f"{n}|{kind}|{arm}"] = (int(rs.random() < p) * valid, valid)
            c[f][f"{f}:{i}"] = cc
    return c


def test_share_and_verdicts():
    assert A.share(np.array([1, 1, 0, 0.]), np.array([1, 0, 0, 0.])) == (0.5 - 0.25) / 0.75
    assert A.verdict(-0.2, -0.3, -0.05) == "shrinks"
    assert A.verdict(0.0, -0.08, 0.1) == "survives"
    assert A.verdict(-0.05, -0.2, 0.1) == "unclear"
    assert abs(A.slope(np.array([0.6, 0.6, 0.6]))) < 1e-12


def test_constant_share_survives_and_bounded_shrinks(monkeypatch):
    monkeypatch.setattr(A, "B_BOOT", 300)
    base = [0.8, 0.6, 0.4]
    flat = [b + 0.5 * (1 - b) for b in base]
    r = A.contrast(make_cells(base, flat), "near", "before", "base", "all")
    assert r["pooled"]["verdict"] in ("survives", "unclear") and abs(r["pooled"]["delta_share"]) < 0.1
    capped = [b + 0.6 * min(1, 2 / n) * (1 - b) for b, n in zip(base, (2, 5, 10), strict=True)]
    r = A.contrast(make_cells(base, capped, seed=1), "near", "before", "base", "all")
    assert r["pooled"]["verdict"] == "shrinks"
    mc = A.model_compare(make_cells(base, capped, seed=2), item_set="all")
    assert all(mc[f]["fit_A_test_B"]["bounded"]["cap"] < 5 for f in A.FAMILIES)


def test_valid_item_set_drops_invalid_items_from_every_cell(monkeypatch):
    monkeypatch.setattr(A, "B_BOOT", 50)
    c = make_cells([0.8, 0.6, 0.4], [0.9, 0.8, 0.7], invalid_base10=0.3)
    v = A.contrast(c, "near", "before", "base", "valid")
    a = A.contrast(c, "near", "before", "base", "all")
    assert v["varbind"]["n_items"] < a["varbind"]["n_items"] == 400
    # invalid-as-wrong lowers base at 10 options, inflating the uplift there
    assert a["varbind"]["per_n"]["10"]["acc_base"] < v["varbind"]["per_n"]["10"]["acc_base"]
