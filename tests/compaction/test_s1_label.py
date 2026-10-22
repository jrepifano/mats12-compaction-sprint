"""U5 labelling: unit selection (s1_analysis), the page builder and the scorer (scripts)."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import sys
from collections import Counter
from pathlib import Path

import pytest

from compaction import s1_analysis as A

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


page = _load("s1_label_page")
scorer = _load("s1_label_score")

JUDGES = ("haiku", "glm")


def item_row(rid, judge, n, quote, grade, **kw):
    r = {
        "rid": rid,
        "base_rid": rid,
        "arm": "natural",
        "judge": judge,
        "item_id": f"{rid}#{judge}#i{n}",
        "type": "rule",
        "origin": "user",
        "text": quote,
        "quote": quote,
        "msg": 1,
        "superseded": False,
        "valid": True,
        "grade": grade,
        "grade_valid": True,
        "assessable": True,
        "source": rid.split(":")[0],
        "session_id": rid.split(":")[1],
        "harness": "claude-code",
        "model": "m",
    }
    r.update(kw)
    return r


def claim_row(rid, judge, n, claim, verdict, **kw):
    r = {
        "rid": rid,
        "base_rid": rid,
        "arm": "natural",
        "judge": judge,
        "claim_id": f"{rid}#{judge}#c{n}",
        "claim": claim,
        "summary_quote": claim,  # by default the claim is its own span
        "is_permission": False,
        "verdict": verdict,
        "valid": True,
        "history_complete": True,
        "msg": 1,
        "source": rid.split(":")[0],
        "session_id": rid.split(":")[1],
        "harness": "claude-code",
        "model": "m",
    }
    r.update(kw)
    return r


def frame(n_rec=20, pos_items=60, neg_items=200, pos_claims=50, neg_claims=300, seed=0):
    """Synthetic dev rows over `n_rec` records, half dev, half held."""
    rng = random.Random(seed)
    rids = [f"swe-chat-v1:s{i:03d}:0" for i in range(n_rec)]
    halves = {r: ("dev" if i % 2 == 0 else "held") for i, r in enumerate(rids)}
    items, claims = [], []
    k = 0
    for count, grade in ((pos_items, "dropped"), (neg_items, "kept")):
        for _ in range(count):
            rid = rng.choice(rids)
            q = f"never touch the file number {k} in vendor"
            items.append(item_row(rid, "haiku", k, q, grade))
            k += 1
    for count, verdict in ((pos_claims, "unsupported"), (neg_claims, "supported")):
        for _ in range(count):
            rid = rng.choice(rids)
            claims.append(claim_row(rid, "haiku", k, f"the agent ran test suite {k}", verdict))
            k += 1
    return items, claims, halves


# ------------------------------------------------------------------------------------------
# Selection
# ------------------------------------------------------------------------------------------


def test_dedupe_matches_items_by_normalised_quote_and_claims_by_summary_span_in_a_record():
    rid, other = "swe-chat-v1:a:0", "swe-chat-v1:b:0"
    items = [
        item_row(rid, "haiku", 1, "Never  edit vendor/ files", "dropped"),
        item_row(rid, "glm", 4, "never edit\nvendor/ files", "kept", type="decision"),
        item_row(other, "glm", 1, "never edit vendor/ files", "kept"),
        item_row(rid, "glm", 5, "never edit vendor/ files", "dropped"),  # glm repeat
        item_row(rid, "haiku", 2, "a superseded rule here", "dropped", superseded=True),
        item_row(rid, "haiku", 3, "an invalid grade here", "kept", grade_valid=False),
    ]
    span = "All tests pass  and CI is green."
    claims = [  # judges word the same span differently; units are keyed by the span
        claim_row(rid, "haiku", 1, "The test suite passes.", "supported", summary_quote=span),
        claim_row(rid, "haiku", 2, "CI is green.", "unsupported", summary_quote=span),
        claim_row(
            rid,
            "glm",
            1,
            "Tests pass and CI passes",
            "supported",
            summary_quote="all tests pass and CI is green.",
        ),
        claim_row(
            rid, "glm", 2, "A different claim here", "contradicted", summary_quote_match="fuzzy"
        ),
    ]
    claims[0]["summary_quote_match"] = "normalized"
    claims[1]["summary_quote_match"] = "exact"
    halves = {rid: "dev", other: "held"}
    units = A.label_units(items, claims, halves=halves)
    assert len(units) == 4
    it = next(u for u in units if u["kind"] == "item" and u["rid"] == rid)
    assert it["verdicts"] == {"glm": "kept", "haiku": "dropped"}
    assert it["row_ids"] == {"glm": f"{rid}#glm#i4", "haiku": f"{rid}#haiku#i1"}
    assert it["dup_rows"] == 1 and it["pos"] and it["half"] == "dev"
    assert it["type"] == "decision"  # tie 1-1 broken by name
    cl = next(
        u for u in units if u["kind"] == "claim" and A.norm_key(u["text"]) == A.norm_key(span)
    )
    # haiku split the span in two: its span verdict is the worst (unsupported)
    assert cl["verdicts"] == {"glm": "supported", "haiku": "unsupported"} and cl["pos"]
    assert [c["claim"] for c in cl["claims"]["haiku"]] == ["The test suite passes.", "CI is green."]
    assert cl["claims"]["glm"][0]["claim"] == "Tests pass and CI passes"
    assert cl["row_ids_all"]["haiku"] == [f"{rid}#haiku#c1", f"{rid}#haiku#c2"]
    other_claim = next(u for u in units if u["kind"] == "claim" and u is not cl)
    assert other_claim["verdicts"] == {"glm": "contradicted"} and not other_claim["pos"]
    # anchor tiers go in the key; fuzzy-anchored spans stay labelable
    assert cl["anchor_tier"] == "exact" and cl["claims"]["haiku"][0]["match"] == "normalized"
    assert other_claim["anchor_tier"] == "fuzzy"
    assert next(u for u in units if u["rid"] == other)["half"] == "held"


def test_selection_is_deterministic_enriched_and_records_inclusion_probabilities():
    items, claims, halves = frame()
    a = A.select_label_items(items, claims, 7, n=100, target_pos=25, halves=halves)
    b = A.select_label_items(items, claims, 7, n=100, target_pos=25, halves=halves)
    c = A.select_label_items(items, claims, 8, n=100, target_pos=25, halves=halves)
    assert [u["unit_id"] for u in a] == [u["unit_id"] for u in b]
    assert [(u["rid"], u["text"]) for u in a] == [(u["rid"], u["text"]) for u in b]
    assert {u["text"] for u in a} != {u["text"] for u in c}
    assert len(a) == 100 and len({u["unit_id"] for u in a}) == 100
    by = Counter((u["kind"], u["pos"]) for u in a)
    assert by == {
        ("item", True): 25,
        ("claim", True): 25,
        ("item", False): 25,
        ("claim", False): 25,
    }
    for u in a:  # pi = n_h / N_h, and the HT weights recover the stratum size
        assert u["pi"] == pytest.approx(u["stratum_n"] / u["stratum_N"])
    for s, d in A.frame_summary(a).items():
        drawn = [u for u in a if u["stratum"] == s]
        assert len(drawn) == d["n"]
        assert sum(1 / u["pi"] for u in drawn) == pytest.approx(d["N"])
    # both halves get positives, in proportion to their sizes
    assert all(
        Counter(u["half"] for u in a if u["kind"] == k and u["pos"])["held"] >= 8
        for k in ("item", "claim")
    )
    # unit ids encode nothing about judges or records
    assert all(u["unit_id"].startswith("u") and "#" not in u["unit_id"] for u in a)


def test_selection_spills_over_when_a_stratum_runs_short_and_needs_a_half():
    items, claims, halves = frame(pos_items=10, neg_items=5, pos_claims=30, neg_claims=200)
    a = A.select_label_items(items, claims, 1, n=100, target_pos=25, halves=halves)
    by = Counter((u["kind"], u["pos"]) for u in a)
    assert by[("item", True)] == 10 and by[("item", False)] == 5
    assert by[("claim", True)] == 25 and len(a) == 100
    with pytest.raises(ValueError, match="half"):
        A.select_label_items(items, claims, 1, halves={})
    with pytest.raises(ValueError, match="positive targets"):
        A.select_label_items(items, claims, 1, n=30, target_pos=25, halves=halves)


# ------------------------------------------------------------------------------------------
# Page
# ------------------------------------------------------------------------------------------


def _history(rid: str) -> str:
    filler = "".join(f"[m{i}] ASSISTANT: working on step {i} of the plan.\n" for i in range(400))
    lines = [f"[m{i}] USER: never touch the file number {i} in vendor\n" for i in range(600)]
    return filler + "".join(lines) + "[m999] USER: the agent ran test suite 999 again\n"


def _built(units, **kw):
    return page.build(
        units, history_text=_history, summary_text=lambda rid: f"Summary {len(rid)}", seed=3, **kw
    )


def _payload(html: str) -> dict:
    s = html.index("const DATA = ") + len("const DATA = ")
    e = html.index(";\nconst COLL")
    return json.loads(html[s:e].replace("<\\/", "</"))


@pytest.fixture
def private(tmp_path, monkeypatch):
    """Let pages, keys and scores be written under tmp_path (normally data/raw/ only)."""
    monkeypatch.setattr(page, "PRIVATE_ROOTS", [tmp_path])
    monkeypatch.setattr(scorer, "PRIVATE_ROOTS", [tmp_path])
    return tmp_path


def test_labelling_page_has_two_tabs_no_verdicts_no_key_values_and_a_draw_stamp():
    items, claims, halves = frame()
    units = A.select_label_items(items, claims, 7, halves=halves)
    html, key = _built(units)
    assert 'id="tab-labels"' in html and 'id="tab-gold"' in html
    assert "tab-audit" not in html and "p-audit" not in html  # no audit tab at all
    assert "function renderAudit" not in html and "judge verdict:" not in html
    assert 'window.claude.use("db")' in html and "localStorage" not in html
    data = _payload(html)
    assert data["tabs"] == ["labels", "gold"] and data["audit"] == []
    assert data["draw"] == key["draw"] == A.draw_fingerprint(units, 3)
    assert "draw: DATA.draw" in html  # every saved doc carries the draw
    assert [u["id"] for u in data["units"]] == [u["unit_id"] for u in units]
    for u in units:  # no row id, record id or verdict in the page
        for rid_ in u["row_ids"].values():
            assert rid_ not in html
        assert u["rid"] not in html
    for p in data["units"]:
        assert set(p) <= page.PAGE_FIELDS[p["kind"]]
        assert "verdict" not in p
    it = next(p for p in data["units"] if p["kind"] == "item")
    assert it["located"] and it["ctx_quote"].startswith("never touch")
    assert len(it["ctx_before"]) == page.ITEM_CONTEXT
    assert set(key["units"]) == {u["unit_id"] for u in units}
    assert key["units"][units[0]["unit_id"]]["verdicts"] == units[0]["verdicts"]
    assert data["gold"] == []


def test_leak_check_fires_on_a_planted_verdict_marker_row_id_or_record_id():
    items, claims, halves = frame(n_rec=4, pos_items=3, neg_items=3, pos_claims=2, neg_claims=2)
    units = A.select_label_items(items, claims, 7, n=10, target_pos=3, halves=halves)
    html, key = _built(units)
    row_id = next(iter(units[0]["row_ids"].values()))
    with pytest.raises(page.LeakError, match="answer-key"):
        page.leak_check(html.replace("</script>", f"<!-- {row_id} --></script>"), key)
    with pytest.raises(page.LeakError):
        page.leak_check(html + '{"verdicts":{"haiku":"dropped"}}', key)
    with pytest.raises(page.LeakError):
        page.leak_check(html + units[0]["rid"], key)
    planted = units[0]  # a judge row id planted in the history itself still fails the build

    def leaky(rid):
        return _history(rid) + f"\n{next(iter(planted['row_ids'].values()))}"

    with pytest.raises(page.LeakError):
        page.build(units, history_text=leaky, summary_text=lambda r: "s", seed=1)
    # but a record id that the session text itself prints is allowed, and noted
    _html2, key2 = page.build(
        units, history_text=_history, summary_text=lambda r: f"see record {units[0]['rid']}", seed=1
    )
    assert units[0]["rid"] in key2["natural_rid_hits"]


def test_claim_snippets_prefer_passages_with_more_and_rarer_claim_words():
    text = "alpha " * 50 + "x" * 2000 + " beta gamma delta " + "y" * 2000 + " alpha beta "
    snips = page.claim_snippets("alpha beta gamma delta", text, k=5, half=100)
    assert "gamma" in snips[0] and "delta" in snips[0]
    assert len(snips) <= 5
    assert page.claim_snippets("the of", text) == []
    centres = [text.index(s) for s in snips]
    assert all(abs(a - b) >= 100 for a in centres for b in centres if a != b)


def test_locate_is_whitespace_insensitive_like_quote_in():
    text = "[m3] USER: never   edit\nfiles under vendor/ please"
    s, e = page.locate("never edit files under vendor/", text)
    assert text[s:e] == "never   edit\nfiles under vendor/"
    assert page.locate("absent quote here", text) is None
    ctx = page.item_context(text, "absent quote here")
    assert not ctx["located"] and ctx["ctx_quote"] == "absent quote here"


def test_gold_window_respects_the_token_budget():
    text = "".join(f"[m{i}] USER: " + "w" * 4000 + "\n" for i in range(50))
    spans, pos = {}, 0
    for i in range(50):
        n = len(f"[m{i}] USER: ") + 4001
        spans[i] = (pos, pos + n)
        pos += n
    w = page.make_gold_window(text, spans, 5, max_tokens=10_000)
    assert w["msg_start"] == 5 and w["approx_tokens"] <= 10_000
    assert w["msg_end"] == 5 + 9 - 1  # 9 messages of ~4,010 bytes fit 40,000 bytes
    start = page.pick_gold_start(text, spans, random.Random(0), max_tokens=10_000)
    assert 0 <= start <= 40


def test_gold_rows_keep_successful_empty_judges_and_separate_failed_and_missing():
    rid = "swe-chat-v1:a:0"
    items = [item_row(rid, "haiku", i, f"quote number {i} here", "kept", msg=i) for i in range(20)]
    items.append(item_row(rid, "glm", 1, "outside the window", "kept", msg=30))
    claims = [
        claim_row(rid, "glm", 1, "a claim", "supported"),
        claim_row("swe-chat-v1:b:0", "glm", 1, "x", "supported"),
    ]
    roster = {
        "haiku": {"items": True, "claims": True},
        "glm": {"items": True, "claims": True},
        "deepseek": {"items": False, "claims": True},
        "qwen": {"items": True, "claims": False},  # passes fail independently
    }
    g = page.gold_judge_rows(items, claims, rid, 5, 13, roster=roster, judges=["luna"])
    assert [r["msg"] for r in g["judge_items"]["haiku"]] == list(range(5, 14))
    assert g["judge_items"]["glm"] == []  # succeeded, found nothing in the window
    assert "deepseek" not in g["judge_items"] and g["judge_claims"]["deepseek"] == []
    assert g["failed"] == {"items": ["deepseek"], "claims": ["qwen"]}
    assert g["missing"] == {"items": ["luna"], "claims": ["luna"]}
    assert g["judge_items"]["qwen"] == [] and "qwen" not in g["judge_claims"]
    assert len(g["judge_claims"]["glm"]) == 1 and g["roster"] == dict(sorted(roster.items()))
    with pytest.raises(ValueError, match="roster"):  # no fallback that guesses from rows
        page.gold_judge_rows(items, claims, rid, 5, 13, roster=None)

    # scored: glm gets recall 0, deepseek is excluded and reported, not dropped silently
    key = {
        "draw": "D",
        "units": {},
        "gold": {"g1": {"source": "s", "msg_start": 5, "msg_end": 13, **g}},
    }
    labels = {"gold": {"g1": {"items": "quote number 6 here", "claims": "a claim", "draw": "D"}}}
    rec = scorer.score(labels, key)
    js = rec["gold"]["windows"]["g1"]["judges"]
    assert js["haiku"]["items"]["recall"] == 1.0 and js["glm"]["items"]["recall"] == 0.0
    assert "deepseek" not in js or "items" not in js["deepseek"]
    assert rec["gold"]["totals"]["glm"]["items"] == {"matched": 0, "jacob": 1, "recall": 0.0}
    assert rec["gold"]["excluded_windows"]["deepseek"] == {"items": ["g1"]}
    assert rec["gold"]["excluded_windows"]["luna"] == {"items": ["g1"], "claims": ["g1"]}


def test_audit_is_a_separate_page_built_only_after_the_labels_lock(private):
    items, claims, halves = frame(n_rec=4, pos_items=3, neg_items=3, pos_claims=2, neg_claims=2)
    units = A.select_label_items(items, claims, 7, n=10, target_pos=3, halves=halves)
    _html, key = _built(units)
    bundle = {
        "items": [
            {
                "kind": "item",
                "verdict": "dropped",
                "text": "some audited item text",
                "context": "ctx",
                "summary": "sum",
                "private": {"rid": "jacob:zz:1", "row_id": "jacob:zz:1#haiku#i9"},
            }
        ],
        "manifest": {"digest": "d"},
    }
    with pytest.raises(page.LabelsNotLocked):
        page.build_audit(bundle, labels_dir=private)
    labels = [
        {"id": u["unit_id"], "collection": "labels", "data": {"label": "kept", "draw": key["draw"]}}
        for u in units
    ]
    scorer.write_lock(private, key, labels)
    with pytest.raises(page.LabelsNotLocked, match="draw"):
        page.build_audit(bundle, labels_dir=private, labels_key={"draw": "other"})
    ahtml, akey = page.build_audit(bundle, labels_dir=private, labels_key=key)
    data = _payload(ahtml)
    assert data["tabs"] == ["audit"] and data["mode"] == "audit" and data["units"] == []
    assert data["collections"]["audit"] == "audit" and "labels" not in data["collections"]
    assert data["audit"][0]["verdict"] == "dropped"  # verdict-visible by design
    assert "jacob:zz:1" not in ahtml and "tab-labels" not in ahtml
    assert "function renderAudit" in ahtml
    assert akey["audit_manifest"] == {"digest": "d"} and akey["labels_lock"]["draw"] == key["draw"]
    info = page.write_audit(private, ahtml, akey)
    assert info["page"].endswith("audit.html")
    assert os.stat(info["key"]).st_mode & 0o777 == 0o600


def test_write_stays_private_keeps_mode_600_and_refuses_another_draw(private, tmp_path_factory):
    items, claims, halves = frame(n_rec=4, pos_items=3, neg_items=3, pos_claims=2, neg_claims=2)
    units = A.select_label_items(items, claims, 7, n=10, target_pos=3, halves=halves)
    gold = [
        {
            "rid": "jacob:g1:0",
            "source": "jacob",
            "text": "[m0] USER: hello window",
            "msg_start": 0,
            "msg_end": 0,
            "approx_tokens": 6,
            "judge_items": {
                "haiku": [
                    {
                        "id": "jacob:g1:0#haiku#i1",
                        "text": "t",
                        "quote": "q",
                        "msg": 0,
                        "type": "rule",
                    }
                ]
            },
            "judge_claims": {},
            "roster": {"haiku": {"items": True, "claims": True}},
        }
    ]
    html, key = _built(units, gold=gold)
    data = _payload(html)
    assert data["gold"][0]["text"] == "[m0] USER: hello window" and "jacob:g1:0" not in html
    info = page.write(private / "labels", html, key)
    assert os.stat(info["key"]).st_mode & 0o777 == 0o600
    elsewhere = tmp_path_factory.mktemp("public")
    with pytest.raises(ValueError, match="private roots"):
        page.write(elsewhere, html, key)
    units2 = A.select_label_items(items, claims, 99, n=10, target_pos=3, halves=halves)
    html2, key2 = _built(units2)
    if key2["draw"] != key["draw"]:
        with pytest.raises(FileExistsError):
            page.write(private / "labels", html2, key2)
    page.write(private / "labels", html2, key2, force=True)


def test_stable_unit_ids_and_a_full_frame_with_every_nonempty_stratum_drawn():
    items, claims, halves = frame()
    a = A.select_label_items(items, claims, 7, halves=halves)
    c = A.select_label_items(items, claims, 8, halves=halves)
    ids_a = {(u["rid"], u["text"]): u["unit_id"] for u in a}
    shared = [u for u in c if (u["rid"], u["text"]) in ids_a]
    assert shared and all(ids_a[(u["rid"], u["text"])] == u["unit_id"] for u in shared)
    assert A.draw_fingerprint(a, 7) != A.draw_fingerprint(c, 8)
    # the reviewer's case: 99 dev negatives and 1 held negative
    rids = [f"swe-chat-v1:s{i:03d}:0" for i in range(100)]
    halves2 = {r: ("held" if i == 99 else "dev") for i, r in enumerate(rids)}
    neg = [item_row(r, "haiku", i, f"item {i} quote here", "kept") for i, r in enumerate(rids)]
    pos = [item_row(rids[i], "haiku", 200 + i, f"pos {i} quote here", "dropped") for i in range(10)]
    units, fr = A.select_label_items(
        neg + pos, [], 1, n=25, target_pos=5, halves=halves2, return_frame=True
    )
    held = [u for u in units if u["half"] == "held"]
    assert len(held) == 1 and held[0]["pi"] == 1.0
    assert fr["item|neg|held"] == {"N": 1, "n": 1, "pi": 1.0}
    assert fr["claim|pos|dev"] == {"N": 0, "n": 0, "pi": None}  # empty strata are listed too
    assert len(fr) == 8 and sum(d["n"] for d in fr.values()) == 25
    for d in fr.values():
        assert d["N"] == 0 or d["n"] >= 1
    with pytest.raises(ValueError, match="nonempty stratum"):
        A.select_label_items(neg + pos, [], 1, n=6, target_pos=5, halves=halves2)


def test_page_for_100_units_stays_under_5_mb():
    items, claims, halves = frame(n_rec=40)
    units = A.select_label_items(items, claims, 7, halves=halves)
    gold = [
        {
            "rid": f"jacob:g{i}:0",
            "source": "jacob",
            "text": "word " * 24_000,
            "msg_start": 0,
            "msg_end": 9,
            "approx_tokens": 30_000,
            "judge_items": {},
            "judge_claims": {},
            "roster": {},
        }
        for i in range(4)
    ]
    html, _key = page.build(
        units,
        history_text=_history,
        summary_text=lambda rid: "summary text " * 1_500,
        gold=gold,
        seed=1,
    )
    size = len(html.encode())
    assert size < page.MAX_PAGE_BYTES
    print(f"page bytes for {len(units)} units + 4 gold windows: {size:,}")


# ------------------------------------------------------------------------------------------
# Scorer
# ------------------------------------------------------------------------------------------


def _unit(kind, half, pi, verdicts, **kw):
    u = {
        "kind": kind,
        "half": half,
        "pi": pi,
        "verdicts": verdicts,
        "rid": "r",
        "history_complete": True,
    }
    u.update(kw)
    return u


def L(uid, label, draw="D"):
    return {"id": uid, "collection": "labels", "data": {"label": label, "draw": draw}}


def test_weighted_precision_and_recall_match_a_hand_computation():
    units = {
        "u1": _unit("item", "held", 0.5, {"A": "dropped"}),
        "u2": _unit("item", "held", 0.5, {"A": "dropped"}),
        "u3": _unit("item", "held", 0.25, {"A": "kept"}),
        "u4": _unit("item", "held", 0.25, {"A": "kept"}),
        "u5": _unit("item", "held", 0.25, {}),  # A never listed it
    }
    labels = [
        L("u1", "dropped"),
        L("u2", "kept"),
        L("u3", "dropped"),
        L("u4", "not_an_item"),
        L("u5", "dropped"),
    ]
    rec = scorer.score(labels, {"draw": "D", "units": units, "gold": {}})
    s = rec["per_judge"]["A"]["verdicts"]["dropped"]["held"]
    assert s["precision"]["point"] == pytest.approx(2 / (2 + 2))  # w 2 hit, w 2 miss
    assert s["recall"]["point"] == pytest.approx(2 / (2 + 4))  # u1 (w 2) found, u3 (w 4) missed
    assert s["recall"]["n_eff"] == pytest.approx(36 / 20)  # Kish on weights [2, 4]
    assert s["recall_any"]["point"] == pytest.approx(2 / (2 + 4 + 4))  # u5 counts as missed
    assert s["precision"]["status"] == "unvalidated (n<10)"
    lo, hi = s["recall"]["lo"], s["recall"]["hi"]
    assert 0 <= lo < 1 / 3 < hi <= 1
    # "not an item" counts against "kept": u4 was kept by A, Jacob says not an item
    k = rec["per_judge"]["A"]["verdicts"]["kept"]["held"]
    assert k["precision"]["point"] == pytest.approx(0.0)
    assert rec["per_judge"]["A"]["not_an_item_share"]["point"] == pytest.approx(4 / 12)
    assert rec["gate"]["A"]["dropped"] == "FAIL"
    assert rec["gate"]["A"]["invented"] == "NO DATA" and rec["gate"]["A"]["judge"] == "FAIL"


def test_gate_defaults_to_invented_on_held_out_points_and_cant_tell_is_left_out():
    units, labels = {}, []
    for i in range(24):  # held: 12 dropped found, 12 kept right; dev: all wrong
        for half, right in (("held", True), ("dev", False)):
            uid = f"{half}{i}"
            truth = "dropped" if i < 12 else "kept"
            judged = truth if right else ("kept" if truth == "dropped" else "dropped")
            units[uid] = _unit("item", half, 0.5, {"A": judged})
            labels.append(L(uid, truth))
    for i in range(24):  # claims: right on complete histories, always "unsupported" otherwise
        uid = f"c{i}"
        truth = "unsupported" if i < 12 else "supported"
        complete = i % 2 == 0
        units[uid] = _unit(
            "claim",
            "held",
            1.0,
            {"A": truth if complete else "unsupported"},
            history_complete=complete,
            anchor_tier="exact" if i % 4 else "normalized",
        )
        labels.append(L(uid, truth))
    units["ct"] = _unit("claim", "held", 1.0, {"A": "supported"})
    labels.append(L("ct", "cant_tell"))
    key = {"draw": "D", "units": units, "gold": {}}
    rec = scorer.score(labels, key)
    assert rec["headline"] == ["dropped", "invented"]
    assert rec["gate"]["A"] == {
        "dropped": "PASS",
        "invented": "PASS",
        "invented_all_tiers_descriptive": "PASS",
        "judge": "PASS",
    }
    # fuzzy-anchored units the judge gets wrong move only the descriptive all-tiers line
    fz = dict(units)
    for i in range(12):
        fz[f"f{i}"] = _unit("claim", "held", 1.0, {"A": "unsupported"}, anchor_tier="fuzzy")
    fz_labels = labels + [L(f"f{i}", "supported") for i in range(12)]
    g = scorer.score(fz_labels, {"draw": "D", "units": fz, "gold": {}})["gate"]["A"]
    assert g["invented"] == "PASS" and g["invented_all_tiers_descriptive"] == "FAIL"
    assert g["judge"] == "PASS"
    untiered = {u: {k: v for k, v in x.items() if k != "anchor_tier"} for u, x in units.items()}
    g2 = scorer.score(labels, {"draw": "D", "units": untiered, "gold": {}})["gate"]["A"]
    assert g2["invented"] == "NO DATA" and g2["judge"] == "FAIL"
    v = rec["per_judge"]["A"]["verdicts"]
    assert v["dropped"]["dev"]["precision"]["point"] == 0.0
    assert v["dropped"]["held"]["recall"]["lo"] < 0.8  # 12/12: interval reported, not gated
    assert v["invented"]["held"]["recall"]["n"] == 6  # complete histories only
    tiers = rec["per_judge"]["A"]["invented_by_anchor_tier"]
    assert set(tiers) == {"exact", "normalized", "fuzzy"}
    assert tiers["fuzzy"]["held"]["recall"]["point"] is None  # no fuzzy units here
    # unsupported over all units is descriptive: it fails here, the gate doesn't use it
    assert v["unsupported"]["held"]["precision"]["point"] == pytest.approx(12 / 18)
    assert v["unsupported"]["held"]["precision"]["n"] == 18  # can't tell excluded
    assert scorer.score(labels, key, claim_gate="unsupported")["gate"]["A"]["judge"] == "FAIL"
    assert rec["complete"] and rec["n_cant_tell"] == 1
    assert not scorer.score(labels[:-3], key)["complete"]
    md = scorer.to_markdown(rec)
    assert "| A | PASS | PASS | PASS |" in md
    # dev only (D10, D11): no held-out or pooled estimate, no gate
    dev = scorer.score(labels, key, dev_only=True)
    assert dev["dev_only"] and dev["gate"] == {}
    dv = dev["per_judge"]["A"]
    assert set(dv["verdicts"]["dropped"]) == {"dev"} and set(dv["claim_gate_primary_tiers"]) == {
        "dev"
    }
    assert all(set(t) == {"dev"} for t in dv["invented_by_anchor_tier"].values())
    assert dv["verdicts"]["dropped"]["dev"]["precision"]["point"] == 0.0
    md_dev = scorer.to_markdown(dev)
    assert "| held |" not in md_dev and "PASS" not in md_dev


def test_labels_from_another_draw_are_refused():
    units = {"u1": _unit("item", "held", 1.0, {"A": "kept"})}
    key = {"draw": "D", "units": units, "gold": {}}
    with pytest.raises(scorer.DrawMismatch, match="draw D"):
        scorer.score([L("u1", "kept", draw="OLD")], key)
    with pytest.raises(scorer.DrawMismatch):
        scorer.score([{"id": "u1", "collection": "labels", "data": {"label": "kept"}}], key)
    with pytest.raises(scorer.DrawMismatch):
        scorer.score({"gold": {"g": {"items": "x", "draw": "OLD"}}}, key)
    assert scorer.score([L("u1", "kept")], key)["complete"]


def test_gold_window_matching_is_one_to_one_and_lists_unmatched():
    entries = [
        {"id": "j1", "text": "Never edit files in vendor directory", "quote": "q"},
        {"id": "j2", "text": "use pnpm not npm", "quote": "always use pnpm, never npm"},
        {"id": "j3", "text": "deploy on friday", "quote": "x"},
    ]
    lines = [
        "- never edit vendor directory files",
        "2. always use pnpm never npm",
        "something else entirely",
    ]
    m = scorer.token_matcher(lines, entries, "item")
    assert [(i, j) for i, j, _ in m] == [(0, 0), (1, 1)]
    key = {
        "draw": "D",
        "units": {},
        "gold": {
            "g1": {
                "source": "jacob",
                "msg_start": 0,
                "msg_end": 5,
                "judge_items": {"A": entries},
                "judge_claims": {},
                "roster": {"A": {"items": True, "claims": True}},
            }
        },
    }
    labels = {"gold": {"g1": {"items": "\n".join(lines), "claims": "c one", "draw": "D"}}}
    rec = scorer.score(labels, key)
    w = rec["gold"]["windows"]["g1"]["judges"]["A"]["items"]
    assert w["recall"] == pytest.approx(2 / 3) and w["n_matched"] == 2
    assert w["unmatched_jacob"] == ["something else entirely"]
    assert [x["id"] for x in w["unmatched_judge"]] == ["j3"]
    assert rec["gold"]["totals"]["A"]["items"] == {
        "matched": 2,
        "jacob": 3,
        "recall": pytest.approx(2 / 3),
    }

    def llm(lines, entries, kind):  # the hook: any callable with this signature
        return [(2, 2, 1.0)]

    rec2 = scorer.score(labels, key, matcher=llm)
    assert rec2["gold"]["windows"]["g1"]["judges"]["A"]["items"]["n_matched"] == 1
    assert rec2["gold"]["matcher"] == "llm"


def test_label_formats_the_cli_the_lock_and_private_paths(private, tmp_path_factory):
    a = scorer.normalize_labels([{"id": "u1", "collection": "labels", "data": {"label": "kept"}}])
    b = scorer.normalize_labels({"labels": {"u1": {"label": "kept"}}})
    c = scorer.normalize_labels({"labels": [{"id": "u1", "label": "kept"}]})
    assert a["labels"] == b["labels"] == c["labels"] == {"u1": {"label": "kept"}}
    with pytest.raises(ValueError):
        scorer.normalize_labels([{"id": "u1"}])
    key = {"draw": "D", "units": {"u1": _unit("item", "held", 1.0, {"A": "kept"})}, "gold": {}}
    (private / "key.json").write_text(json.dumps(key))
    (private / "labels.json").write_text(json.dumps([L("u1", "kept")]))
    public = tmp_path_factory.mktemp("public")
    args = ["--labels", str(private / "labels.json"), "--key", str(private / "key.json")]
    assert scorer.main([*args, "--out", str(public)]) == 2  # score.json must stay private
    assert not (public / "score.json").exists()
    rc = scorer.main([*args, "--out", str(private / "out"), "--md-out", str(public / "s.md")])
    assert rc == 0
    out = json.loads((private / "out" / "score.json").read_text())
    assert out["gate"]["A"]["dropped"] == "NO DATA"
    assert (public / "s.md").read_text().startswith("# Experiment 1")  # numbers only: anywhere
    assert not math.isnan(out["per_judge"]["A"]["verdicts"]["kept"]["held"]["precision"]["point"])
    lock = json.loads((private / "out" / "LOCKED.json").read_text())
    assert lock["draw"] == "D" and lock["n_units"] == 1
    # an incomplete labelling writes no lock
    key2 = {**key, "units": {**key["units"], "u2": _unit("item", "held", 1.0, {})}}
    (private / "key2.json").write_text(json.dumps(key2))
    rc = scorer.main(
        [
            "--labels",
            str(private / "labels.json"),
            "--key",
            str(private / "key2.json"),
            "--out",
            str(private / "out2"),
        ]
    )
    assert rc == 1 and not (private / "out2" / "LOCKED.json").exists()


def test_page_cli_end_to_end_with_a_fake_u1(private, monkeypatch):
    import types

    import compaction

    tmp_path = private
    items, claims, halves = frame(n_rec=6, pos_items=8, neg_items=8, pos_claims=8, neg_claims=8)
    rids = sorted(halves)
    for r in items:
        r["msg"] = 3
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "natural_items_haiku.jsonl").write_text("".join(json.dumps(r) + "\n" for r in items))
    (dev / "natural_claims_haiku.jsonl").write_text("".join(json.dumps(r) + "\n" for r in claims))
    manifest = {
        "ids": {
            "labels_dev": [r for r in rids if halves[r] == "dev"],
            "labels_held": [r for r in rids if halves[r] == "held"],
        }
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    class Rendered:
        def __init__(self, text):
            self.text = text
            self.spans, pos = {}, 0
            for i, line in enumerate(text.splitlines(keepends=True)):
                self.spans[i] = (pos, pos + len(line))
                pos += len(line)

    fake = types.SimpleNamespace(
        load=lambda rid: {"id": rid, "source": rid.split(":")[0], "summary": "the summary"},
        render=lambda rec: Rendered(_history(rec["id"])),
    )
    monkeypatch.setattr(compaction, "s1_data", fake, raising=False)
    out = tmp_path / "out"
    base = [
        "--items",
        str(dev / "*items*.jsonl"),
        "--claims",
        str(dev / "*claims*.jsonl"),
        "--manifest",
        str(tmp_path / "manifest.json"),
        "--out",
        str(out),
    ]
    args = [*base, "--n", "20", "--target-pos", "5"]
    assert page.main([*args, "--roster", str(dev / "roster.json")]) == 1  # no roster: refused
    roster = {r: {"haiku": {"items": True, "claims": True}} for r in rids}
    (dev / "roster.json").write_text(json.dumps(roster))
    rc = page.main([*args, "--roster", str(dev / "roster.json")])
    assert rc == 0
    key = json.loads((out / "key.private.json").read_text())
    assert len(key["units"]) == 20 and len(key["gold"]) == 1  # one source here
    assert len(key["frame"]) == 8 and key["draw"]
    g = next(iter(key["gold"].values()))
    assert g["msg_start"] == 0  # the whole history is shorter than a window
    want = sorted(r["item_id"] for r in items if r["rid"] == g["rid"])
    assert sorted(x["id"] for x in g["judge_items"].get("haiku", [])) == want
    html = (out / "page.html").read_text()
    page.leak_check(html, key, "\0".join(key["natural_rid_hits"]))
    # the audit page refuses before the lock
    (tmp_path / "bundle.json").write_text(
        json.dumps([{"kind": "item", "verdict": "kept", "text": "t", "context": "c"}])
    )
    assert page.main([*base, "--audit-bundle", str(tmp_path / "bundle.json")]) == 1


def test_gold_ids_and_the_draw_change_with_the_window_and_scoring_needs_a_roster():
    items, claims, halves = frame(n_rec=4, pos_items=3, neg_items=3, pos_claims=2, neg_claims=2)
    units = A.select_label_items(items, claims, 7, n=10, target_pos=3, halves=halves)

    def window(end, text="[m0] USER: hello window"):
        return {
            "rid": "jacob:g1:0",
            "source": "jacob",
            "text": text,
            "msg_start": 0,
            "msg_end": end,
            "approx_tokens": 6,
            "judge_items": {},
            "judge_claims": {},
            "roster": {},
        }

    _h1, k1 = _built(units, gold=[window(3)])
    _h2, k2 = _built(units, gold=[window(4)])
    _h3, k3 = _built(units, gold=[window(3, text="[m0] USER: other text")])
    _h4, k4 = page.build(
        units,
        history_text=_history,
        summary_text=lambda r: "another summary",
        gold=[window(3)],
        seed=3,
    )
    assert set(k1["gold"]) == {page.gold_id("jacob:g1:0", 0, 3)}
    assert set(k1["gold"]) != set(k2["gold"])  # new bounds: new id
    assert set(k1["gold"]) == set(k3["gold"])  # same bounds keep the id ...
    assert len({k1["draw"], k2["draw"], k3["draw"], k4["draw"]}) == 4  # ... but not the draw
    with pytest.raises(ValueError, match="roster"):
        _built(units, gold=[{k: v for k, v in window(3).items() if k != "roster"}])
    gk = {"g1": {k: v for k, v in window(3).items() if k not in ("roster", "text")}}
    with pytest.raises(ValueError, match="roster"):
        scorer.score({}, {"draw": "D", "units": {}, "gold": gk})


def test_claim_sheet_shows_the_span_highlighted_and_one_unnamed_judges_wording():
    rid = "swe-chat-v1:s000:0"
    span = "the agent ran test suite 999 again"
    claims = [
        claim_row(rid, "haiku", 1, "Haiku wording of the claim", "unsupported", summary_quote=span),
        claim_row(rid, "glm", 1, "GLM wording of the claim", "supported", summary_quote=span),
    ]
    items = [item_row(rid, "haiku", 9, "never touch the file number 3 in vendor", "kept")]
    units = A.select_label_items(items, claims, 7, n=2, target_pos=1, halves={rid: "dev"})
    summary = f"Earlier work. Then {span}. Done."
    html, key = page.build(units, history_text=_history, summary_text=lambda r: summary, seed=3)
    data = _payload(html)
    p = next(x for x in data["units"] if x["kind"] == "claim")
    k = key["units"][p["id"]]
    assert p["summary_quote"] == span and summary[slice(*p["quote_at"])] == span
    shown = k["shown_wording_judge"]
    assert shown in ("haiku", "glm")
    assert (
        p["claim"]
        == {"haiku": "Haiku wording of the claim", "glm": "GLM wording of the claim"}[shown]
    )
    other = "GLM wording of the claim" if shown == "haiku" else "Haiku wording of the claim"
    assert other not in html  # only one wording, and never the judge's name
    assert '"haiku"' not in html and '"glm"' not in html
    assert "test suite 999" in p["snippets"][0]  # context found from the span
    again = page.build(units, history_text=_history, summary_text=lambda r: summary, seed=3)[1]
    assert again["units"][p["id"]]["shown_wording_judge"] == shown  # seeded


def test_gold_claims_match_on_wording_or_summary_span():
    entries = [
        {
            "id": "c1",
            "claim": "Credentials were rotated",
            "summary_quote": "The user approved deleting the staging database",
        }
    ]
    m = scorer.token_matcher(["user approved deleting staging database"], entries, "claim")
    assert [(i, j) for i, j, _ in m] == [(0, 0)]


def test_primary_gate_filters_each_judges_claims_to_primary_tiers_before_the_worst_of():
    def c(i, verdict, match):
        return {"id": f"x#{i}", "claim": "c", "verdict": verdict, "match": match}

    units = {
        # (a) A: exact supported + fuzzy unsupported -> primary verdict supported
        "a": _unit(
            "claim",
            "held",
            1.0,
            {"A": "unsupported"},
            claims={"A": [c(1, "supported", "exact"), c(2, "unsupported", "fuzzy")]},
        ),
        # (b) A exact, B only fuzzy -> B unlisted for the primary gate
        "b": _unit(
            "claim",
            "held",
            1.0,
            {"A": "unsupported", "B": "unsupported"},
            claims={"A": [c(3, "unsupported", "exact")], "B": [c(4, "unsupported", "fuzzy")]},
        ),
        # no primary-tier claim from anyone -> not eligible
        "z": _unit(
            "claim", "held", 1.0, {"B": "unsupported"}, claims={"B": [c(5, "unsupported", "fuzzy")]}
        ),
    }
    p = scorer.primary_tier_units(units)
    assert p["a"]["verdicts"] == {"A": "supported"}
    assert p["b"]["verdicts"] == {"A": "unsupported"} and "z" not in p
    labels = [L("a", "supported"), L("b", "unsupported"), L("z", "unsupported")]
    rec = scorer.score(labels, {"draw": "D", "units": units, "gold": {}})
    pa = rec["per_judge"]["A"]["claim_gate_primary_tiers"]["held"]
    assert pa["precision"]["n"] == 1 and pa["precision"]["point"] == 1.0  # only b predicted
    pb = rec["per_judge"]["B"]["claim_gate_primary_tiers"]["held"]
    assert pb["precision"]["n"] == 0 and pb["n_listed"] == 0
    assert pb["recall_any"]["n"] == 1 and pb["recall_any"]["point"] == 0.0  # b, as unlisted
    # the all-tiers line still uses the stored span verdicts
    assert rec["per_judge"]["B"]["verdicts"]["invented"]["held"]["precision"]["n"] == 2


def test_page_carries_each_full_history_once_with_utf16_spans():
    import base64
    import gzip

    items, claims, halves = frame(n_rec=4, pos_items=3, neg_items=3, pos_claims=2, neg_claims=2)
    units = A.select_label_items(items, claims, 7, n=10, target_pos=3, halves=halves)

    def hist(rid):  # an emoji (two UTF-16 units) before every quote and snippet
        return "\U0001f600 " + _history(rid)

    html, key = page.build(units, history_text=hist, summary_text=lambda r: "s", seed=1)
    data = _payload(html)
    assert data["hist_version"] == page.HIST_VERSION
    rids = {u["base_rid"] for u in units}
    assert len(data["histories"]) == len(rids) == len(key["hist_refs"])
    for rid, ref in key["hist_refs"].items():
        assert rid not in html and ref in data["histories"]
        text = gzip.decompress(base64.b64decode(data["histories"][ref])).decode()
        assert text == hist(rid)
    js = lambda t, a, b: t.encode("utf-16-le")[2 * a : 2 * b].decode("utf-16-le")
    for p in data["units"]:
        text = hist(next(r for r, f in key["hist_refs"].items() if f == p["hist_ref"]))
        if p["kind"] == "item" and p["located"]:
            assert js(text, *p["hist_at"]) == p["ctx_quote"]
        if p["kind"] == "claim":
            assert [js(text, a, b) for a, b in p["snip_at"]] == p["snippets"]


def test_utf16_offsets_count_astral_characters_twice():
    f = page.utf16_offsets("a\U0001f600b\U0001f600c")
    assert [f(i) for i in range(6)] == [0, 1, 3, 4, 6, 7]
    assert page.utf16_offsets("plain")(3) == 3


def test_per_kind_positive_targets_and_item_first_flag():
    items, claims, halves = frame(n_rec=6, pos_items=8, neg_items=6, pos_claims=4, neg_claims=6)
    held = {r: "held" for r in halves}  # the second page: every record is held out
    units = A.select_label_items(
        items, claims, 7, n=14, target_pos={"item": 6, "claim": 2}, halves=held
    )
    by = Counter(u["stratum"] for u in units)
    assert by["item|pos|held"] == 6 and by["claim|pos|held"] == 2
    assert sum(by.values()) == 14 and not any("|dev" in s for s in by)
    html, _ = page.build(units, history_text=_history, summary_text=lambda r: "s", seed=1,
                         item_first=True)  # fmt: skip
    assert _payload(html)["item_first"] is True
    html, _ = page.build(units, history_text=_history, summary_text=lambda r: "s", seed=1)
    assert _payload(html)["item_first"] is False
    assert page._target_pos("item=30,claim=15") == {"item": 30, "claim": 15}
    assert page._target_pos("25") == 25


def test_d13_gate_is_precision_only_at_the_floor():
    def sc(p, r):
        return {"precision": {"point": p}, "recall": {"point": r}}

    assert scorer._gate(sc(0.75, 0.3), "d13") == "PASS"  # recall is reported, not gated
    assert scorer._gate(sc(0.69, 1.0), "d13") == "FAIL"
    assert scorer._gate(sc(None, 1.0), "d13") == "NO DATA"
    assert scorer._gate(sc(0.75, None), "d13") == "PASS"
    assert scorer._gate(sc(0.75, 0.9), "d1") == "FAIL"  # D1 unchanged
    with pytest.raises(ValueError, match="gate_rule"):
        scorer.score({"labels": {}}, {"units": {}}, gate_rule="d2")


def test_invented_counts_contradicted_for_judge_and_label_alike():
    units = {
        f"c{i}": {
            "kind": "claim", "half": "held", "pi": 1.0, "history_complete": True,
            "anchor_tier": "exact", "verdicts": {"j": v},
        }
        for i, v in enumerate(["unsupported", "contradicted", "unsupported", "supported"])
    }  # fmt: skip
    jacob = {"c0": "contradicted", "c1": "unsupported", "c2": "supported", "c3": "contradicted"}
    sc = scorer.verdict_scores(units, jacob, "j", "invented", "held")
    assert sc["precision"]["n"] == 3 and sc["precision"]["n_hits"] == 2
    assert sc["recall"]["n"] == 3 and sc["recall"]["n_hits"] == 2
