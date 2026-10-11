"""p03.items_a on canned SPARQL JSON (no network)."""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from p03 import core as C
from p03 import items_a as A

WD = A.WD


def uri(q):
    return {"type": "uri", "value": WD + q}


def lit(v):
    return {"type": "literal", "value": str(v)}


def pool_row(work, label, creator, sl=5, date="1987-05-01T00:00:00Z"):
    return {"work": uri(work), "label": lit(label), "creator": uri(creator), "sl": lit(sl),
            "date": lit(date)}


def val_row(work, creators, dates, kinds=()):
    return {"work": uri(work), "creators": lit("|".join(WD + c for c in creators)),
            "dates": lit("|".join(dates)), "kinds": lit("|".join(WD + k for k in kinds))}


def creator_row(q, label, countries, alts=(), human=True):
    return {"creator": uri(q), "label": lit(label), "human": lit("true" if human else "false"),
            "countries": lit("|".join(WD + c for c in countries)), "alts": lit("|".join(alts)),
            "sl": lit(20)}


def country_row(q, label, alts=(), dissolved=None):
    r = {"country": uri(q), "label": lit(label), "alts": lit("|".join(alts))}
    if dissolved:
        r["dissolved"] = lit(dissolved)
    return r


def test_parse_pool_groups_creators():
    rows = [pool_row("Q10", "Night Train", "Q1"), pool_row("Q10", "Night Train", "Q2"),
            pool_row("Q11", "Blue Harbour", "Q1")]
    p = A.parse_pool(rows, "film_director")
    assert p["Q10"].creators == {"Q1", "Q2"} and p["Q11"].creators == {"Q1"}
    assert p["Q11"].sitelinks == 5 and p["Q11"].label == "Blue Harbour"


def test_parse_validation_year_precision():
    v = A.parse_validation([
        val_row("Q10", ["Q1"], ["1990-01-01T00:00:00Z@9", "1987-03-01T00:00:00Z@11",
                                "1800-01-01T00:00:00Z@7"], ["Q11424"]),
        val_row("Q11", ["Q1"], ["1880-01-01T00:00:00Z@8"]),
    ])
    assert v["Q10"].year == 1987  # century-precision 1800 ignored; earliest year-precision
    assert v["Q11"].year is None
    assert v["Q10"].creators == ["Q1"] and v["Q10"].kinds == ["Q11424"]


def test_label_reject():
    assert A.label_reject("1987") == "numeric"
    assert A.label_reject("Om") == "short"
    assert A.label_reject('The "Big" One') == "quote"
    assert A.label_reject("Symphony No. 3 in D minor") == "generic"
    assert A.label_reject("String Quartet No. 11") == "generic"
    assert A.label_reject("Untitled") == "generic"
    assert A.label_reject("Self-Portrait") == "generic"
    assert A.label_reject("Night Train to Lisbon") is None
    assert A.label_reject("Leonore Overture No. 1") is None  # a proper name survives


def _world():
    pool = {
        "film_director": A.parse_pool([
            pool_row("Q10", "Night Train", "Q1"),            # ok
            pool_row("Q11", "Blue Harbour", "Q1"),           # ok (2nd for Q1)
            pool_row("Q12", "Red Harbour", "Q1"),            # creator cap
            pool_row("Q13", "Two Hands", "Q2"), pool_row("Q13", "Two Hands", "Q3"),  # 2 creators
            pool_row("Q14", "1999", "Q3"),                   # numeric label
            pool_row("Q15", "Lisbon Story", "Q4"),           # duplicate label (see book)
            pool_row("Q16", "The Kowalski Affair", "Q5"),    # contains creator surname
            pool_row("Q17", "Paper Moon Rising", "Q6"),      # creator has 2 citizenships
            pool_row("Q18", "Quiet Field", "Q7"),            # creator not human
            pool_row("Q19", "Summer in France", "Q8"),       # contains the answer
            pool_row("Q20", "Glass Orchard", "Q9"),          # no year-precision date
            pool_row("Q21", "Dark Water Mill", "Q3"),        # ok
        ], "film_director"),
        "book_author": A.parse_pool([
            pool_row("Q30", "Lisbon Story", "Q4"),
            pool_row("Q31", "The Salt Road", "Q4"),          # ok, novel
        ], "book_author"),
    }
    vals = {
        **{q: (["Q1"], ["1987-01-01T00:00:00Z@9"]) for q in ("Q10", "Q11", "Q12")},
        "Q13": (["Q2", "Q3"], ["1990-01-01T00:00:00Z@9"]),
        "Q16": (["Q5"], ["1970-01-01T00:00:00Z@9"]),
        "Q17": (["Q6"], ["1970-01-01T00:00:00Z@9"]),
        "Q18": (["Q7"], ["1970-01-01T00:00:00Z@9"]),
        "Q19": (["Q8"], ["1970-01-01T00:00:00Z@9"]),
        "Q20": (["Q9"], ["1970-01-01T00:00:00Z@7"]),
        "Q21": (["Q3"], ["2001-06-01T00:00:00Z@11"]),
        "Q31": (["Q4"], ["1962-01-01T00:00:00Z@9"]),
    }
    val_rows = [val_row(q, c, d, ["Q8261"] if q == "Q31" else ["Q11424"])
                for q, (c, d) in vals.items()]
    creators = A.parse_creators([
        creator_row("Q1", "Anna Berg", ["Q30"], ["A. Berg"]),
        creator_row("Q3", "Jan Novak", ["Q213"]),
        creator_row("Q4", "Maria Costa", ["Q45"]),
        creator_row("Q5", "Piotr Kowalski", ["Q36"]),
        creator_row("Q6", "Lena Hart", ["Q30", "Q145"]),
        creator_row("Q7", "Studio Nine", ["Q30"], human=False),
        creator_row("Q8", "Jean Roux", ["Q142"]),
        creator_row("Q9", "Ola Lind", ["Q34"]),
    ])
    countries = A.parse_countries([
        country_row("Q30", "United States", ["the States", "USA"]),
        country_row("Q213", "Czech Republic", ["Czechia"]),
        country_row("Q45", "Republic of Portugalia", dissolved="1910-10-05T00:00:00Z"),
        country_row("Q36", "Poland"), country_row("Q142", "France"), country_row("Q34", "Sweden"),
    ])
    pv = A.parse_validation(val_rows)

    def lookup(rel, works):
        return {w.qid: pv[w.qid] for w in works if w.qid in pv}, creators, countries

    return pool, lookup


def test_build_items_filters_and_schema():
    pool, lookup = _world()
    cands, drops = A.candidates(pool, seed=0)
    assert "Q14" not in {w.qid for w in cands["film_director"]}
    assert drops["film_director:duplicate_label"] == 1 and drops["book_author:duplicate_label"] == 1
    assert drops["film_director:creators!=1"] == 1
    items = A.build_items(cands, lookup, n=10, drops=drops, seed=0)
    got = {it.meta["work_qid"] for it in items}
    assert got - {"Q10", "Q11", "Q12"} == {"Q21", "Q31"}
    assert len(got & {"Q10", "Q11", "Q12"}) == 2  # at most 2 per creator
    for why in ("creator_cap", "label_has_creator", "citizenship!=1", "not_human",
                "label_has_country", "no_year"):
        assert drops[f"film_director:{why}"] == 1, why
    assert sorted(it.id for it in items) == ["A-0001", "A-0002", "A-0003", "A-0004"]
    by = {it.meta["work_qid"]: it for it in items}
    film = next(it for it in items if it.meta["creator_qid"] == "Q1")
    book = by["Q31"]
    title = film.meta["work_label"]
    assert isinstance(film, C.Item) and film.type == "A"
    assert film.entity == f'the 1987 film "{title}"'
    assert film.prompt == ('What is the country of citizenship of the director of the 1987 film '
                           f'"{title}"? Answer in one word.')
    assert film.hop1_prompt == f'Who is the director of the 1987 film "{title}"? ' \
                               'Answer with the name only.'
    assert film.hop2_template == "What is the country of citizenship of {X}? Answer in one word."
    assert film.prefill == C.ANSWER_STEM
    assert film.category == film.relation == "film_director"
    assert film.bridge_true == "Anna Berg" and film.bridge_aliases == ["A. Berg"]
    assert film.answer_true == "United States"
    assert "USA" in film.answer_aliases and "America" in film.answer_aliases
    assert C.same("U.S.", film.answer_true, film.answer_aliases)
    assert film.meta["year"] == 1987 and film.meta["sitelinks"] == 5
    assert {"work_qid", "creator_qid", "country_qid", "year", "sitelinks"} <= film.meta.keys()
    assert book.entity == 'the 1962 novel "The Salt Road"'
    assert book.prompt.startswith("What is the country of citizenship of the author of")
    assert book.meta["country_dissolved"] == 1910 and film.meta["country_dissolved"] is None
    assert by["Q21"].answer_true == "Czech Republic" and "Czechia" in by["Q21"].answer_aliases
    # round-trips through the frozen schema
    row = __import__("dataclasses").asdict(film)
    assert C.Item(**row) == film


def test_build_items_deterministic_and_quota():
    pool, lookup = _world()
    runs = []
    for _ in range(2):
        cands, drops = A.candidates(pool, seed=0)
        runs.append([(i.id, i.meta["work_qid"]) for i in A.build_items(cands, lookup, 10, drops)])
    assert runs[0] == runs[1]
    cands, drops = A.candidates(pool, seed=0)
    two = A.build_items(cands, lookup, n=2, drops=drops)
    assert Counter(i.relation for i in two) == {"film_director": 1, "book_author": 1}
    cands, drops = A.candidates(pool, seed=0)
    three = A.build_items(cands, lookup, n=3, drops=drops)  # quota 1 each, then round-robin
    assert Counter(i.relation for i in three) == {"film_director": 2, "book_author": 1}


def test_entity_formats_and_nouns():
    r = A.RELATIONS
    assert r["painting_creator"].entity("Haystacks", 1890, "painting") == \
        'the painting "Haystacks" (1890)'
    assert A.noun_for(r["music_composer"], ["Q105543609", "Q1344"]) == "opera"
    assert A.noun_for(r["music_composer"], ["Q105543609"]) == "composition"
    assert A.noun_for(r["book_author"], ["Q7725634"]) == "book"
    assert r["music_composer"].entity("Lodoiska", 1791, "opera") == 'the opera "Lodoiska" (1791)'


def test_queries_are_well_formed():
    q = A.pool_query(A.RELATIONS["book_author"], (1950, 1960))
    assert "VALUES ?cls { wd:Q7725634 wd:Q8261 }" in q and "LIMIT" in q
    assert '"1950-01-01T00:00:00Z"' in q and "?sl <= 12" in q
    assert "FILTER(?date" not in A.pool_query(A.RELATIONS["music_composer"], (0, 0))
    v = A.validate_query(A.RELATIONS["music_composer"], ["Q1", "Q2"])
    assert "VALUES ?work { wd:Q1 wd:Q2 }" in v and "psv:P577|psv:P571" in v


def test_extend_excludes_base_and_counts_prior_creators():
    pool, lookup = _world()
    cands, drops = A.candidates(pool, seed=0)
    base = A.build_items(cands, lookup, n=10, drops=drops, seed=0)
    base_q1 = [it for it in base if it.meta["creator_qid"] == "Q1"]
    assert len(base_q1) == 2
    # base keeps only one Q1 work: the third Q1 work is then allowed (cap counts both sets)
    kept = [it for it in base if it is not base_q1[0]]
    ext = A.Extend.from_items(kept, drop_dissolved=True)
    cands, drops = A.candidates(pool, seed=0)
    new = A.build_items(cands, lookup, n=10, drops=drops, seed=1, start=2001, ext=ext)
    newq = {it.meta["work_qid"] for it in new}
    assert not newq & ext.exclude_works
    q1_all = [it for it in [*kept, *new] if it.meta["creator_qid"] == "Q1"]
    assert len(q1_all) == 2  # one in base + one new; the third Q1 work hits the cap
    assert drops["film_director:creator_cap"] == 1
    assert drops["book_author:country_dissolved"] == 0  # Q31 is in the base (excluded)
    assert {it.id for it in new} == {f"A-{2001 + i}" for i in range(len(new))}


def test_extend_drops_dissolved():
    pool, lookup = _world()
    cands, drops = A.candidates(pool, seed=0)
    new = A.build_items(cands, lookup, n=10, drops=drops, ext=A.Extend(drop_dissolved=True))
    assert "Q31" not in {it.meta["work_qid"] for it in new}  # author's country dissolved 1910
    assert drops["book_author:country_dissolved"] == 1
    assert all(it.meta["country_dissolved"] is None for it in new)


def test_extend_cap_is_shared_across_relations():
    pool = {
        "film_director": A.parse_pool([pool_row("Q50", "Iron Bell", "Q1")], "film_director"),
        "book_author": A.parse_pool([pool_row("Q51", "Copper Kettle", "Q1")], "book_author"),
    }
    _, lookup0 = _world()
    vals = A.parse_validation([val_row("Q50", ["Q1"], ["1980-01-01T00:00:00Z@9"]),
                               val_row("Q51", ["Q1"], ["1981-01-01T00:00:00Z@9"])])

    def lookup(rel, works):
        _, creators, countries = lookup0(rel, [])
        return {w.qid: vals[w.qid] for w in works}, creators, countries

    prior = C.Item(id="A-0001", type="A", category="film_director", prompt="p",
                   hop1_prompt="h", hop2_template="{X}?", bridge_true="Anna Berg",
                   meta={"work_qid": "Q10", "creator_qid": "Q1", "country_dissolved": None})
    cands, drops = A.candidates(pool, seed=0)
    new = A.build_items(cands, lookup, n=4, drops=drops, ext=A.Extend.from_items([prior]))
    assert len(new) == 1  # 1 prior + 1 new = 2 for creator Q1, whichever relation
    assert drops["film_director:creator_cap"] + drops["book_author:creator_cap"] == 1
