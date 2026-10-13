"""Donor construction (p24.donors) on Brauer's set, and token-level checks with Gemma 4's
tokenizer (skipped when it isn't cached)."""

import pytest
from p06 import brauer as B
from p24 import donors as D


@pytest.fixture(scope="module")
def data():
    if not B.DATA_PATHS[1].exists():
        pytest.skip("Brauer's dataset (data/P06, gitignored) is missing")
    exs, shots = B.load(1)
    return sorted(exs, key=lambda e: e["idx"]), shots


def changed_defs(a, b):
    return {n for (n, v), (_, w) in zip(a["definitions"], b["definitions"], strict=True) if v != w}


def test_xys_share_a_prime_and_fix_what_they_should(data):
    exs, _ = data
    n = 0
    for e in exs:
        ds = D.donors(e)
        if "X" not in ds:
            continue
        n += 1
        p = D.parse(e)
        x, y, s = ds["X"], ds["Y"], ds["S"]
        assert x["queried_value"] == y["queried_value"] == x["y_alt"] != p["y"]
        assert s["queried_value"] == p["y"] and s["answer"] == e["answer"]
        assert x["a_alt"] == y["a_alt"] == s["a_alt"] == D.q(e, x["y_alt"]) != e["answer"]
        assert changed_defs(e, x) == {p["ref"]}
        assert changed_defs(e, y) == {e["queried_term"]}
        assert changed_defs(e, s) == {p["ref"], e["queried_term"]}
        # S's x' through the target's own offset gives y_X (the copy hypothesis's answer)
        px = D.parse(s)
        assert p["c1"] * px["x"] + p["s"] * p["off"] == x["y_alt"]
        for d in (x, y, s):
            pd = D.parse(d)
            assert len(str(pd["x"])) == len(str(p["x"]))
            assert len(str(pd["off"])) == len(str(p["off"]))
            assert pd["op"] == p["op"] and pd["coef"] == p["coef"]
            assert d["answer"] >= 0
    assert n == 413


def test_flip_and_coefficient(data):
    exs, _ = data
    nf = nc = 0
    for e in exs:
        ds, p = D.donors(e), D.parse(e)
        if "F" in ds:
            nf += 1
            f = D.parse(ds["F"])
            assert f["op"] != p["op"] and (f["x"], f["off"], f["coef"]) == (p["x"], p["off"],
                                                                         p["coef"])
            assert ds["F"]["a_alt"] == ds["F"]["answer"] != e["answer"]
        if "C" in ds:
            nc += 1
            assert p["coef"] == "three times" and D.parse(ds["C"])["coef"] == "four times"
            assert ds["C"]["queried_value"] == 4 * p["x"] + p["s"] * p["off"]
    assert nf == 488 and nc == 261


def test_seeded_and_tiered(data):
    exs, _ = data
    e = next(e for e in exs if "X" in D.donors(e))
    assert D.pick_dx(e) == D.pick_dx(e)
    big = [e for e in exs if D.pick_dx(e) is not None]
    share = sum(abs(D.parse(e)["c1"] * D.pick_dx(e)) >= 10 for e in big) / len(big)
    assert share > 0.5


def test_random_donor(data):
    exs, _ = data
    r = D.random_donor(exs[0], exs)
    assert r["idx"] != exs[0]["idx"] and r["kind"] == "R" and r["a_alt"] == r["answer"]


@pytest.fixture(scope="module")
def tok():
    try:
        from p06 import stage2 as S2
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(S2.MODEL_ID, revision=S2.MODEL_REV,
                                             local_files_only=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Gemma 4 tokenizer not cached: {e!r}")


def test_donor_prompts_same_length_and_label(tok, data):
    from p06 import stage2 as S2
    from p24 import pod as P

    exs, shots = data
    items = [e for e in exs if "X" in D.donors(e)][:40]
    for e in items:
        t = S2.render(tok, e, shots, P.ARM)
        u = P.union_positions(tok, t)
        assert len(u) == 102 and u[2] == t.dots[0]
        for k, d in D.donors(e).items():
            r = S2.render(tok, d, shots, P.ARM)
            diff = P.check_diff(tok, t.ids, r.ids, k)
            lo, hi = {"X": (1, 2), "Y": (1, 2), "S": (2, 4), "F": (1, 1), "C": (1, 1),
                      "P": (1, 2)}[k]
            assert lo <= len(diff) <= hi
            assert (r.dots == t.dots).all()


def test_check_diff_rejects_other_tokens(tok):
    from p24 import pod as P

    a = tok.encode("cok = twice the number for lef minus 23", add_special_tokens=False)
    b = tok.encode("cok = twice the number for lal minus 23", add_special_tokens=False)
    with pytest.raises(ValueError):
        P.check_diff(tok, a, b, "X")
