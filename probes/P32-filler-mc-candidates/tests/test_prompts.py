"""Prompt scaffolds: arms, placements and the padding control."""

import pytest
from p32 import items as I
from p32 import prompts as P


@pytest.fixture(scope="module")
def fams():
    if not I.VARBIND_PATH.exists():
        pytest.skip("data/P32/varbind_p32.json (gitignored) not present")
    return I.load_all()


def test_conditions():
    c = P.conditions()
    assert len(c) == 34 == len(set(c))
    assert "0|-|dots" in c and "10|near|dots_before" in c and "2|far|padmid" in c


@pytest.mark.parametrize("fam", ["varbind", "chain"])
def test_placements(fams, fam):
    evals, shots = fams[fam]
    it = evals[0]
    opts = "Options: " + ", ".join(map(str, I.options(it, "near", 5)))
    before = P.user_turn(it, "5|near|dots_before")
    after = P.user_turn(it, "5|near|dots_after")
    padmid = P.user_turn(it, "5|near|padmid")
    assert before.index(opts) < before.index("Filler:") and before.endswith("\n\nAnswer:")
    assert after.index("Filler:") < after.index(opts) and after.endswith(f"{opts}\n\nAnswer:")
    assert padmid.index(opts) < padmid.index("Passage:")
    assert P.user_turn(it, "5|near|pad").startswith("Passage: ")
    assert "Options" not in P.user_turn(it, "0|-|dots")
    assert P.DOTS.count(".") == 500
    m = P.messages(it, shots, "10|far|dots_before")
    assert len(m) == 2 + 2 * len(shots) - 1 + 1
    assert all("Filler:" in x["content"] for x in m if x["role"] == "user")
    assert P.FILLER_SYS in m[0]["content"] and P.MC_SYS in m[0]["content"]
    assert P.FILLER_SYS not in P.system(it, "10|far|base")


def test_pad_has_no_digits():
    assert not any(ch.isdigit() for ch in P.PAD)
