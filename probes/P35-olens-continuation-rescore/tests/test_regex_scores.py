"""Tests for p35.regex_scores (pure regex; no API, no data files except the synthetic ones)."""

from __future__ import annotations

import pytest
from p11 import items as I
from p35 import regex_scores as R

# --------------------------------------------------------------------------- bullets


def test_split_bullets_basic_and_continuation_lines():
    t = "- is: **Li Hongzhang**. My answer\n- 是：王莽。\nmore of the second\n- third"
    assert R.split_bullets(t) == ["is: **Li Hongzhang**. My answer", "是：王莽。\nmore of the second",
                                  "third"]


def test_split_bullets_no_dash_lines_is_one_bullet_and_empty_is_none():
    assert R.split_bullets("Fe\nIron") == ["Fe\nIron"]
    assert R.split_bullets("") == []
    assert R.split_bullets(None) == []


def test_split_bullets_preamble_and_explicit_empty_bullet():
    assert R.split_bullets("intro\n- a\n- \n- b") == ["intro", "a", "", "b"]
    # an indented "- " line continues the current bullet
    assert R.split_bullets("- a\n  - sub\n- b") == ["a\n  - sub", "b"]


def test_readout_text_samples_list():
    assert R.split_bullets(R.readout_text({"samples": ["- x\n- y"]})) == ["x", "y"]
    assert R.split_bullets(R.readout_text({"text": "- x"})) == ["x"]


# --------------------------------------------------------------------------- countries


def us():
    return R.group_forms("United States")


def uk():
    return R.group_forms("United Kingdom")


@pytest.mark.parametrize("text", [
    "American", "the Americans", "**America**", "United States", "United-States", "USA",
    "the U.S. was", "US citizen", "美国画家", "Answer: American.",
])
def test_us_forms_hit(text):
    assert R.names_any(text, us())


@pytest.mark.parametrize("text", [
    "South American", "Latin-American painter", "Native American art", "North America",
    "tell us more", "U.S.S.R.", "Pan-American", "answer in English",
])
def test_us_forms_miss(text):
    assert not R.names_any(text, us())


def test_uk_forms_and_language_exclusion():
    for t in ("British", "UK", "United Kingdom", "England", "English painter", "英国"):
        assert R.names_any(t, uk()), t
    for t in ("answer in English", "the English word for it", "New England", "New South Wales"):
        assert not R.names_any(t, uk()), t


def test_case_sensitive_polish_and_korea():
    pl = R.group_forms("Poland")
    assert R.names_any("Polish", pl) and not R.names_any("polish the answer", pl)
    kr = R.group_forms("South Korea")
    assert R.names_any("Korean", kr) and R.names_any("South-Korean", kr)
    assert not R.names_any("North Korean", kr)


def test_answer_groups_cover_sampled_answers():
    assert R.answer_groups("**American**.") == {"United States"}
    assert R.answer_groups("English") == {"United Kingdom"}
    assert R.answer_groups("Soviet") == {"Soviet Union"}
    assert R.answer_groups("South African") == {"South Africa"}
    assert R.answer_groups("Argentine") == {"Argentina"}


def test_country_group_wikidata_labels():
    assert R.country_group("Kingdom of Denmark") == "Denmark"
    assert R.country_group("People's Republic of China") == "China"
    assert R.country_group("Kingdom of the Netherlands") == "Netherlands"
    assert R.country_group("Scotland") == "Scotland"
    for country in I.DEMONYMS:  # every P11 true country maps, and its demonyms are covered
        forms = R.group_forms(R.country_group(country))
        for f in I.flip_forms(country, []):
            assert R.names_any(f, forms), (country, f)


# --------------------------------------------------------------------------- answer position


@pytest.mark.parametrize("text,loose,strict", [
    ("is: **Oliver Stone**. My one-word answer", True, True),
    ("（answer） to this trivia question is: **Oliver Stone**.", True, True),
    ("答案是：**Oliver Stone**", True, True),
    ("Oliver Stone is American.", True, True),
    ("**Stone**", True, True),
    ("is: the director Oliver Stone", True, False),
    ("My one-word answer in English is: **Oliver Stone**", True, True),
    ("**Oliver Stone** (or **France**)", True, True),
    ("the film was made in Hollywood by the famous director Oliver Stone", False, False),
    # a gloss after an answer is not answer position, nor is a slot past a nationality word
    ("American (Oliver Stone directed it)", False, False),
    ("Spanish. Explanation: Oliver Stone was born in New York", False, False),
    ("Japan (or Japanese) [Note: Oliver Stone is American]", False, False),
    ("American", False, False),
])
def test_answer_position(text, loose, strict):
    forms = R.person_forms("Oliver Stone", [])
    assert R.position_flags(R.norm_text(text), forms) == (loose, strict)


def test_person_forms_agree_with_p11_names():
    forms = R.person_forms("Gustave Moreau", ["Gustave Moreau (French painter)"])
    for t in ("Moreau painted it", "a painter", "Gustave Moreau", "Moreaus"):
        assert (R.first_start(R.norm_text(t), forms) is not None) == I.names(
            t, "Gustave Moreau", ["Gustave Moreau (French painter)"]), t


# --------------------------------------------------------------------------- multihop terms


def test_short_target_token_rule():
    fe = R.term_forms("Fe")
    assert R.names_any("- Fe. The symbol", fe) and R.names_any("“Fe”", fe)
    assert not R.names_any("fe", fe) and not R.names_any("Fermium", fe)
    assert not R.names_any("iron (FeO)", fe)
    c = R.term_forms("C")
    assert R.names_any("is C.", c) and not R.names_any("is c", c) and not R.names_any("CO2", c)


def test_digit_target_rule():
    five = R.term_forms("5")
    assert R.names_any("is 5.", five) and R.names_any("five players", five)
    for t in ("15", "2.5", "5,000", "5th", "fifty"):
        assert not R.names_any(t, five), t
    assert R.names_any("12 months", R.term_forms("12"))
    assert not R.names_any("112", R.term_forms("12"))


def test_case_sensitive_month_and_plain_words():
    may = R.term_forms("May")
    assert R.names_any("in May", may) and not R.names_any("it may be", may)
    assert R.names_any("Iron", R.term_forms("iron")) and R.names_any("penguins",
                                                                     R.term_forms("penguin"))


# --------------------------------------------------------------------------- end to end


def _bitem():
    it = I.Item(id="X1", relation="film_director", role="director", entity='the film "S"',
                question="q", bridge="Oliver Stone", bridge_aliases=[], y="Tibor Takacs",
                y_aliases=[], y_country="Hungary", y_country_aliases=[], qwen_answer="American",
                qwen_answer_share=1.0, y_nation_forms=["Hungary", "Hungarian"])
    bitems, checks = R.build_b_items([it], {"X1": ["American"] * 19 + ["**Hungarian**"]})
    return bitems, checks


def test_build_b_items_flags_overlap_and_bridge_absence():
    bitems, checks = _bitem()
    b = bitems["X1"]
    assert b.qwen_groups == ["Hungary", "United States"] and b.true_group == "Hungary"
    assert [o["id"] for o in checks["true_answer_overlap"]] == ["X1"]
    assert checks["bridge_in_qwen_samples"] == []
    assert checks["unmapped_sampled_answers"] == []


def test_end_to_end_b_aggregate():
    bitems, _ = _bitem()
    rows_in = [
        ("p03", {"id": "X1", "layer": 20, "pos": -3, "draw": "main",
                 "text": "- is: **Oliver Stone**.\n- is: American\n- answer is no"}),
        ("p03", {"id": "X1", "layer": 20, "pos": -1, "draw": "main",
                 "text": "- American\n- American (Oliver Stone)"}),
        ("p03", {"id": "OTHER", "layer": 20, "pos": -1, "draw": "main", "text": "- x"}),
    ]
    rows = R.b_bullets(rows_in, bitems)
    assert len(rows) == 5
    res = R.aggregate_b(rows)
    a3 = res["by_source_pos"]["p03|-3"]
    assert a3["n"] == 3 and a3["qwen_answer"] == pytest.approx(1 / 3)
    assert a3["bridge"] == pytest.approx(1 / 3) and a3["bridge_slot_first"] == pytest.approx(1 / 3)
    a1 = res["by_source_pos"]["p03|-1"]
    assert a1["qwen_answer"] == 1.0 and a1["bridge"] == 0.5
    assert a1["bridge_in_answer_position"] == 0.0 and a1["bridge_slot_first"] == 0.0
    ap = res["answer_position_given_bridge"]["p03|all"]
    assert ap["n_bridge_bullets"] == 2 and ap["loose"] == 0.5 and ap["strict"] == 0.5
    il = res["item_level"]
    assert il["p03|-3"]["cells_bridge_no_answer"] == 0  # same cell has "is: American"
    assert il["p03|all"]["items_bridge_any"] == 1
    bt = res["bootstrap"]["p03"]["qwen_answer_-1_minus_-3"]
    assert bt["diff"] == pytest.approx(2 / 3) and bt["n_items"] == 1


def test_end_to_end_m_aggregate():
    items, checks = R.build_m_items([
        {"name": "fe", "prompt": "The symbol for element 26 is", "target": "Fe",
         "intermediates": ["iron"]},
        {"name": "rome", "prompt": "The capital of Italy is", "target": "Rome",
         "intermediates": ["Italy"]},
    ])
    assert checks["prompt_names_intermediate"] == ["rome"]
    ol = [{"id": "fe", "layer": 40, "pos": -2, "samples": ["- Fe and iron\n- fe\n- iron"]},
          {"id": "rome", "layer": 40, "pos": -1, "samples": ["- Rome\n- Paris"]}]
    tm = [{"id": "fe", "layer": L, "pos": -2, "samples": ["- iron"]} for L in (20, 40)]
    rows = R.m_bullets("olens", ol, items) + R.m_bullets("text_monitor", tm, items)
    res = R.aggregate_m(rows)
    d = res["by_reader_layer"]["olens|40"]
    assert d["n"] == 5 and d["both"] == pytest.approx(1 / 5)
    assert d["target_only"] == pytest.approx(1 / 5) and d["intermediate_only"] == pytest.approx(1 / 5)
    assert d["neither"] == pytest.approx(2 / 5)
    assert d["item_target"] == 1.0 and d["item_intermediate"] == 0.5
    assert res["by_reader_layer"]["text_monitor|20"]["item_intermediate_no_target"] == 1.0
    assert R.text_monitor_layer_invariant(tm) == {"n_items": 1, "identical_across_layers": 1}


def test_not_before_needs_a_word_boundary_and_modal_flag():
    # "Japan American": "pan" inside "Japan" must not trigger the "Pan-American" exclusion
    assert R.names_any("Japan American", us())
    bitems, _ = _bitem()
    f = R.score_b_bullet("Hungarian", bitems["X1"])
    assert f["qwen_answer"] and not f["qwen_modal"] and f["true_answer"]
    assert bitems["X1"].true_overlap
    rows = R.b_bullets([("p11", {"id": "X1", "layer": 40, "pos": -1, "text": "- American"})],
                       bitems)
    assert R.aggregate_b(rows)["by_source_pos_no_true_overlap"] == {}
