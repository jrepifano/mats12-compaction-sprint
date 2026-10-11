from p03 import core as C


def test_norm_and_modal():
    assert C.norm("**Blue**.") == "blue"
    assert C.norm(" The Elephant (African)") == "elephant"
    assert C.norm("Café") == "cafe"
    m = C.modal(["Blue", "blue.", "Green", "BLUE", "Blue!"] + ["blue"] * 8 + ["red"] * 7)
    assert m.answer == "blue" and m.surface == "blue" and abs(m.share - 0.6) < 1e-9
    assert m.consistent
    assert not C.modal(["a", "b"]).consistent
    assert C.modal(["", ""]).answer == ""


def test_same_and_plural():
    assert C.same("Dolphins", "dolphin")
    assert C.same("USA", "United States", ["USA", "America"])
    assert not C.same("bluegrass", "blue")


def test_round2_and_seed():
    a = C.Item(id="A1", type="A", category="film", prompt="p", hop1_prompt="h",
               hop2_template="Where is {X} from?", bridge_true="Z")
    b = C.Item(id="B1", type="B", category="animal", prompt="fav animal?")
    c = C.Item(id="C1", type="C", category="animal", prompt="composed",
               property_template="Continent of the {X}?", b_id="B1")
    rows = {
        "A1|hop1": C.SampleRow("A1|hop1", "A1", "hop1", "h", C.NAME_STEM, None,
                               ["John Smith"] * 15 + ["Jo"] * 5, [0] * 20, ["eos"] * 20),
        "B1|main": C.SampleRow("B1|main", "B1", "main", "p", C.ANSWER_STEM, None,
                               ["Dolphin"] * 10 + ["Cat"] * 10, [0] * 20, ["eos"] * 20),
    }
    reqs = C.round2_requests([a, b, c], rows)
    assert [r.key for r in reqs] == ["A1|hop2"]  # B's 0.5 share is not consistent
    assert reqs[0].prompt == "Where is John Smith from?"
    assert C.request_seed("x", 1) == C.request_seed("x", 1) != C.request_seed("x", 2)


def test_stats():
    r, lo, hi = C.wilson(0, 40)
    assert r == 0 and lo == 0 and 0.08 < hi < 0.09
    m, lo, hi = C.paired_bootstrap([1, 1, 0, 1], [0, 0, 0, 1], n_boot=2000)
    assert m == 0.5 and lo <= m <= hi
