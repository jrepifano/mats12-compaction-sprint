from p03 import core as C
from p03 import predict as P
from p03 import select as S


def row(key, samples):
    iid, kind = key.split("|")
    return C.SampleRow(key, iid, kind, "p", C.ANSWER_STEM, None, samples, [0] * len(samples),
                       ["stop"] * len(samples))


def items():
    return [
        C.Item(id="A1", type="A", category="film_director", prompt="two-hop",
               hop1_prompt="h1", hop2_template="c of {X}?", bridge_true="Jane Doe",
               answer_true="France", relation="film_director", entity='the film "Z"'),
        C.Item(id="B1", type="B", category="cheese", prompt="fav cheese?"),
        C.Item(id="C1", type="C", category="cheese", prompt="think of fav cheese; country?",
               property_template="country of {X}?", b_id="B1"),
    ]


def test_item_rows_rules():
    s = {k: row(k, v) for k, v in {
        "A1|hop1": ["John Roe"] * 20, "A1|main": ["Italy"] * 20, "A1|hop2": ["Italy"] * 18 + ["x"] * 2,
        "B1|main": ["Brie"] * 15 + ["Feta"] * 5,
        "C1|main": ["France"] * 20, "C1|prop": ["France."] * 20, "C1|pfx": ["Brie"] * 20,
    }.items()}
    m = S.Matcher()
    m.verdict[P.equiv_call("John Roe", "Jane Doe", "person").key] = False
    m.verdict[P.equiv_call("Italy", "France", "country").key] = False
    rows = {r["id"]: r for r in S.item_rows(items(), s, m, collect=False)}
    assert rows["B1"]["consistent"] and rows["B1"]["target"] == "Brie"
    assert rows["C1"]["consistent"] and rows["C1"]["target"] == "Brie"
    assert rows["A1"]["consistent"] and rows["A1"]["target"] == "John Roe"
    # an unknown verdict on the bridge counts as correct (drops the item: conservative)
    m2 = S.Matcher()
    m2.verdict[P.equiv_call("Italy", "France", "country").key] = False
    r2 = {r["id"]: r for r in S.item_rows(items(), s, m2, collect=True)}
    assert not r2["A1"]["consistent"] and r2["A1"]["why"] == "bridge correct"
    assert P.equiv_call("John Roe", "Jane Doe", "person").key in m2.pending


def test_textproof_rule():
    cons = [{"type": "B", "survivor": True, "ho-sonnet_hit": False, "ho-fewshot_hit": i < 4}
            for i in range(60)]
    tp = S.textproof(cons)
    assert tp["B"]["survivors"] == 60 and tp["B"]["text_proof"]
    cons[0]["ho-sonnet_hit"] = True
    for r in cons[:15]:
        r["ho-fewshot_hit"] = True
    assert not S.textproof(cons)["B"]["text_proof"]


def test_predictor_prompt_and_shots():
    a, b, c = items()
    assert "WHICH PERSON" in P.predictor_prompt(a) and "director" in P.predictor_prompt(a)
    assert "WHICH cheese" in P.predictor_prompt(c)
    pool = [(b, "Brie"), (c, "Brie")] + [
        (C.Item(id=f"B{i}", type="B", category=f"cat{i}", prompt=f"q{i}"), f"t{i}")
        for i in range(2, 30)]
    shots = P.draw_shots(b, pool)
    assert len(shots) == 20 and all(q != "fav cheese?" for q, _ in shots)
    assert shots == P.draw_shots(b, pool)
    assert P.parse_guesses('```json\n{"guesses": ["a", "b", "c", "d"]}\n```') == ["a", "b", "c"]


def test_score_baselines_and_regex():
    from p03 import score as SC

    rs = [{"id": "x"}, {"id": "y"}]
    sel = {"x": {"ho-sonnet_hit": False, "ho-fewshot_hit": True},
           "y": {"ho-sonnet_hit": False, "ho-fewshot_hit": False}}
    sc = {"text_monitor": {"x": {"target": False}, "y": {"target": False}}}
    b = SC.baselines(rs, sc, sel)
    assert b["ho-either"] == {"x": True, "y": False} and b["text_monitor"]["x"] is False
    assert SC.cell_hit({"tokens": [" Brie", "cheese"]}, "brie", [], False)
    assert not SC.cell_hit({"tokens": [" Br", "ie"]}, "brie", [], False)
    assert SC.cell_hit({"samples": ["- the director Ingmar Bergman"]}, "Ingmar Bergman", [], True)
    assert SC.cell_hit({"samples": ["thinking of Bergman's films"]}, "Ingmar Bergman", [], True)
    assert not SC.cell_hit({"samples": ["blackout"]}, "black", [], False)


def test_contains():
    assert S.contains("Peanut", "peanut butter") and S.contains("Butter Chicken", "Butter")
    assert not S.contains("Slate", "Gable") and not S.contains("blue", "bluegrass")
    assert S.contains("Lightfoot", "Gordon Lightfoot")


def test_score_short_alias_forms():
    from p03 import score as SC

    tok, _ = SC.forms("Claude Monet", ["C. Monet", "Claude O. Monet"], True)
    assert "c" not in tok and "monet" in tok and "c. monet" in tok
    assert not SC.cell_hit({"tokens": ["C", "G", "ops"]}, "Claude Monet", ["C. Monet"], True)
