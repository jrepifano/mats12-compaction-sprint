"""Tests for the Experiment 2 close-out's live-loss calibration page and scorer (plan unit 4):
scripts/s2_losscal_page.py and scripts/s2_losscal_score.py, on fixture rows that follow the
close-out's losses.jsonl contract and a fake `boundary_view`."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SUBJECTS = ("deepseek", "glm", "haiku", "luna")


def _script(name):
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.pop(0)


page = _script("s2_losscal_page")
scorer = _script("s2_losscal_score")


def _tag(run_key):  # a boundary-specific phrase that isn't the run key (that would leak)
    return "parser-" + hashlib.sha256(run_key.encode()).hexdigest()[:8]


def _row(run_key, item_id, *, subject, half, grade, lost, msg=1, quote=None):
    q = quote or f"use the cached parser for {_tag(run_key)} item {item_id}"
    return {
        "rid": f"deepswe:{run_key}:0", "base_rid": f"deepswe:{run_key}:0", "arm": "natural",
        "judge": "haiku", "item_id": item_id, "type": "decision", "text": f"Item {item_id}",
        "quote": q, "msg": msg, "valid": True, "grade": grade, "grade_valid": True,
        "summary_quote": f"SUMMARYQUOTE-{run_key}-{item_id}" if grade == "misstated" else None,
        "availability": "lost" if lost else None, "run_key": run_key,
        "cohort_key": run_key.rsplit("-a", 1)[0], "task": "t", "channel": "chat",
        "subject": subject, "policy": "pi", "half": half, "planted_rule": None,
        "first_grade": grade, "lost": lost, "recoverable": None, "recoverable_how": None,
        "boundary_dir": f"/fake/{run_key}",
    }


def _rows(n_per=6, halves=("heldout", "development")):
    rows = []
    for s in SUBJECTS:
        for h in halves:
            for b in range(2):
                rk = f"pilot-0{b}-task-{s}-chat-{h}-a1"
                for j in range(n_per):
                    lost = j % 2 == 0
                    grade = ("dropped" if j % 4 == 0 else "misstated") if lost else "kept"
                    rows.append(_row(rk, f"i{j}", subject=s, half=h, grade=grade, lost=lost))
    return rows


def _fake_view(bd):
    rk = Path(bd).name
    hist = [{"i": 0, "role": "user", "text": "Please solve the issue."},
            {"i": 1, "role": "user", "text": "Earlier context. " + " ".join(
                f"use the cached parser for {_tag(rk)} item i{j}" for j in range(8)) + " Later."},
            {"i": 2, "role": "assistant", "text": "ok"}]
    post = [{"i": 0, "role": "system", "kind": "system prompt", "text": "You are an agent."},
            {"i": 1, "role": "user", "kind": "instruction file", "text": "AGENTS.md rules"},
            {"i": 2, "role": "user", "kind": "summary", "text": "The summary text."},
            {"i": 3, "role": "assistant", "kind": "kept", "text": "kept tail"}]
    return {"run_key": rk, "summary": "The summary text.", "post_messages": post,
            "history": hist}


def _built(rows=None, seed=page.SEED):
    cards = page.draw(rows or _rows(), seed=seed)
    html, key = page.build(cards, seed=seed, view=_fake_view)
    return cards, html, key


def test_draw_is_deterministic_and_records_strata():
    rows = _rows()
    a = page.draw(rows, seed=7)
    b = page.draw(list(reversed(rows)), seed=7)
    assert [c["card_id"] for c in a] == [c["card_id"] for c in b]
    assert len(a) == 30
    assert sum(c["judge_class"] == "lost" for c in a) == 20
    assert sum(c["judge_class"] == "kept" for c in a) == 10
    assert all(c["half"] == "heldout" and c["fill"] is None for c in a)
    for c in a:
        assert c["stratum"] == f"{c['judge_class']}/{c['subject']}"
        same = [x for x in a if x["stratum"] == c["stratum"] and x["half"] == c["half"]]
        assert c["stratum_n"] == len(same) <= c["stratum_N"]
        assert c["inclusion_p"] == pytest.approx(c["stratum_n"] / c["stratum_N"])
    # lost cards split equally across the 4 subjects; kept 10 -> 2 or 3 each
    per = {s: sum(c["subject"] == s and c["judge_class"] == "lost" for c in a) for s in SUBJECTS}
    assert set(per.values()) == {5}
    assert page.draw(rows, seed=8) != a


def test_kept_and_lost_classes():
    r = _row("x-a1", "i0", subject="glm", half="heldout", grade="dropped", lost=False)
    assert page.judge_class(r) is None  # dropped from the summary but still present
    assert page.judge_class({**r, "lost": True}) == "lost"
    assert page.judge_class({**r, "grade": "kept"}) == "kept"
    assert page.judge_class({**r, "grade": "kept", "grade_valid": False}) is None
    assert page.judge_class({**r, "lost": True, "valid": False}) is None


def test_shortfall_is_filled_from_development_and_marked():
    rows = _rows()
    # haiku's held-out lost stratum has only 2 items
    keep = [r for r in rows if not (r["subject"] == "haiku" and r["half"] == "heldout"
                                    and r["lost"])]
    extra = [r for r in rows if r["subject"] == "haiku" and r["half"] == "heldout"
             and r["lost"]][:2]
    cards = page.draw(keep + extra, seed=3)
    hk = [c for c in cards if c["subject"] == "haiku" and c["judge_class"] == "lost"]
    assert len(hk) == 5
    assert sum(c["half"] == "heldout" for c in hk) == 2
    dev = [c for c in hk if c["half"] == "development"]
    assert len(dev) == 3 and all(c["fill"] == "development" for c in dev)
    for c in dev:
        assert c["stratum_N"] == 6 and c["stratum_n"] == 3
    # no development items at all for haiku-lost: other subjects' held-out fill the class
    rows2 = [r for r in keep if not (r["subject"] == "haiku" and r["lost"])] + extra
    cards2 = page.draw(rows2, seed=3)
    assert sum(c["judge_class"] == "lost" for c in cards2) == 20
    other = [c for c in cards2 if c["fill"] == "other_subject"]
    assert len(other) == 3 and all(c["half"] == "heldout" and c["subject"] != "haiku"
                                   for c in other)


def test_page_is_blind_and_leak_check_passes(tmp_path):
    cards, html, key = _built()
    page.leak_check(html, key)
    data = page.page_data(html)
    assert [c["id"] for c in data["cards"]] == key["order"]
    for c in data["cards"]:
        assert set(c) <= page.CARD_FIELDS
        blob = json.dumps(c)
        assert "dropped" not in blob and "misstated" not in blob and "lost" not in blob
        assert c["located"] and c["excerpt"]["mark"] == c["ctx_quote"]
    for m in page.KEY_MARKERS:
        assert m not in html
    for c in cards:
        assert c["run_key"] not in html
        if c.get("summary_quote"):
            assert c["summary_quote"] not in html
    assert "function submit(labels)" in html and 'id="dump"' in html
    assert "Find in ${u.find_in" in html and "this item is" in html
    # quote highlighted at its own span in the rendered history
    hist_text, _ = page.render_history(_fake_view(cards[0]["boundary_dir"])["history"])
    c0 = data["cards"][0]
    a, b = c0["hist_at"]
    assert hist_text[a:b] == c0["ctx_quote"]
    out = page.write(tmp_path / "data" / "raw" / "cal", html, key,
                     private_root=tmp_path / "data" / "raw", check_ignored=False)
    assert Path(out["key"]).stat().st_mode & 0o077 == 0
    assert out["cards"] == 30


@pytest.mark.parametrize("inject", [
    '"judge_grade": "dropped"', '"stratum": "lost/glm"', '"availability"', "RUNKEY"])
def test_leak_check_fails_on_an_injected_key_value(inject):
    cards, html, key = _built()
    if inject == "RUNKEY":
        inject = cards[0]["run_key"]
    bad = html.replace("<h1>", f"<h1>{inject}", 1)
    with pytest.raises(page.S1P.LeakError):
        page.leak_check(bad, key)
    # a card field outside CARD_FIELDS is refused too
    d = page.page_data(html)
    d["cards"][0]["grade"] = "kept"
    with pytest.raises(page.S1P.LeakError):
        page.leak_check(page.render(d), key)


def test_write_refuses_destinations_outside_ignored_data_raw(tmp_path):
    _, html, key = _built()
    with pytest.raises(ValueError):
        page.write(tmp_path / "elsewhere", html, key, private_root=tmp_path / "data" / "raw",
                   check_ignored=False)
    with pytest.raises(ValueError):
        page.main(["--losses", str(tmp_path / "x.jsonl"),
                   "--out", str(page.ROOT / "results" / "sprint" / "S2")])
    assert page.RP.git_ignored(page.OUT_DIR / page.KEY_NAME)


def test_unlocated_quote_is_flagged():
    rows = [_row("r-a1", "i0", subject="glm", half="heldout", grade="dropped", lost=True,
                 quote="a phrase that is nowhere")]
    cards = page.draw(rows, n_lost=1, n_kept=0)
    html, _ = page.build(cards, view=_fake_view)
    c = page.page_data(html)["cards"][0]
    assert c["located"] is False and c["hist_at"] is None and c["excerpt"] is None


def test_find_span_normalises_whitespace_and_case():
    assert page.find_span("a  Use\nthe parser b", "use the parser") == (3, 17)
    assert page.find_span("abc", "") is None


def _labels_for(key, fn, draw=None):
    return [{"id": cid, "collection": "labels",
             "data": {"label": fn(cid, key["cards"][cid]), "note": "",
                      "draw": draw or key["draw"]}} for cid in key["order"]]


def test_scorer_round_trip(tmp_path):
    _, _, key = _built()
    kp = tmp_path / "key.json"
    kp.write_text(json.dumps(key))

    # Jacob agrees on every kept card; on lost cards, agrees with the judge's grade except
    # every 4th lost card (by order), which he calls kept
    lost_ids = [c for c in key["order"] if key["cards"][c]["judge_class"] == "lost"]
    disagree = set(lost_ids[::4])

    def lab(cid, k):
        if k["judge_class"] == "kept":
            return "kept"
        return "kept" if cid in disagree else k["judge_grade"]

    lp = tmp_path / "labels.json"
    lp.write_text(json.dumps(_labels_for(key, lab)))
    out = tmp_path / "calibration.json"
    assert scorer.main(["--labels", str(lp), "--key", str(kp), "--out", str(out)]) == 0
    res = json.loads(out.read_text())
    p = res["heldout"]["precision_lost"]
    assert p["n"] == 20 and p["k"] == 20 - len(disagree)
    assert p["p_unweighted"] == pytest.approx(15 / 20)
    assert p["ci95"][0] < p["p"] < p["ci95"][1]
    assert res["heldout"]["kept_agreement"]["p"] == 1.0
    assert res["heldout"]["dropped_vs_misstated_agreement"]["p"] == 1.0
    assert res["complete"] and "reported" in res["decision"]
    assert len(res["jacob_confirmed_losses"]) == 15
    assert sum(sum(v.values()) for v in res["confusion_judge_grade_x_label"].values()) == 30

    # partial labels: exit 1; wrong draw: exit 2
    lp.write_text(json.dumps(_labels_for(key, lab)[:5]))
    assert scorer.main(["--labels", str(lp), "--key", str(kp), "--out", str(out)]) == 1
    lp.write_text(json.dumps(_labels_for(key, lab, draw="other")))
    assert scorer.main(["--labels", str(lp), "--key", str(kp), "--out", str(out)]) == 2


def test_scorer_precision_below_bar_and_not_an_item():
    _, _, key = _built()

    def lab(cid, k):
        return "kept" if k["judge_class"] == "kept" else "not_an_item"

    labels = scorer.load_labels(_labels_for(key, lab), key["draw"])
    res = scorer.score(key, labels)
    assert res["heldout"]["precision_lost"]["p"] == 0.0
    assert res["heldout"]["precision_lost_excl_not_item"]["n"] == 0
    assert "not reported" in res["decision"]
    # can't tell is left out; no held-out lost labels -> NO DATA
    labels = scorer.load_labels({cid: {"label": "cant_tell", "draw": key["draw"]}
                                 for cid in key["order"]}, key["draw"])
    res = scorer.score(key, labels)
    assert res["decision"].startswith("NO DATA") and res["n_cant_tell"] == 30


def test_scorer_weights_by_inclusion_probability():
    a = {"inclusion_p": 1.0, "label": "dropped"}
    b = {"inclusion_p": 0.25, "label": "kept"}  # stands for 4 items
    r = scorer.ratio([a, b], lambda c: c["label"] == "dropped")
    assert r["p"] == pytest.approx(1 / 5) and r["p_unweighted"] == 0.5
    assert r["n_eff"] == pytest.approx(25 / 17, abs=0.01)
    with pytest.raises(ValueError):
        scorer.load_labels({"x": {"label": "LOST"}}, "d")


def test_inclusion_probabilities_hold_for_unequal_boundaries():
    """Review r1 #1: one boundary with 1 item and one with 9, quota 2 from the stratum. Each
    item's empirical inclusion rate over many seeds matches the recorded n / N = 0.2."""
    rows = [_row("small-a1", "i0", subject="glm", half="heldout", grade="dropped", lost=True)]
    rows += [_row("big-a1", f"i{j}", subject="glm", half="heldout", grade="dropped", lost=True)
             for j in range(9)]
    hits = {}
    n_seeds = 2000
    for seed in range(n_seeds):
        cards = page.draw(rows, n_lost=2, n_kept=0, seed=seed)
        assert len(cards) == 2 and all(c["inclusion_p"] == pytest.approx(0.2) for c in cards)
        for c in cards:
            hits[(c["run_key"], c["item_id"])] = hits.get((c["run_key"], c["item_id"]), 0) + 1
    assert hits[("small-a1", "i0")] / n_seeds == pytest.approx(0.2, abs=0.03)
    assert hits[("big-a1", "i3")] / n_seeds == pytest.approx(0.2, abs=0.03)


def test_hist_at_is_in_utf16_units_for_astral_characters():
    """Review r1 #2: an emoji before the quote shifts JavaScript offsets by one per emoji."""
    def view(bd):
        v = _fake_view(bd)
        v["history"][1]["text"] = "\U0001F600 x \U0001F680 " + v["history"][1]["text"]
        return v

    cards = page.draw(_rows(), seed=5)[:3]
    html, _ = page.build(cards, view=view)
    for c, shown in zip(cards, page.page_data(html)["cards"], strict=True):
        text, spans = page.render_history(view(c["boundary_dir"])["history"])
        a, b = page.locate(c, text, spans)["hist_at"]
        assert text[a:b] == c["quote"]
        js = text.encode("utf-16-le")
        a16, b16 = shown["hist_at"]
        assert js[2 * a16:2 * b16].decode("utf-16-le") == c["quote"]
        assert (a16, b16) == (a + 2, b + 2)


def test_draw_fingerprint_covers_the_shown_evidence():
    cards = page.draw(_rows(), seed=5)
    _, key1 = page.build(cards, view=_fake_view)

    def view(bd):
        v = _fake_view(bd)
        v["summary"] = "A revised summary."
        return v

    _, key2 = page.build(cards, view=view)
    assert sorted(key1["cards"]) == sorted(key2["cards"]) and key1["draw"] != key2["draw"]
    with pytest.raises(scorer.DrawMismatch):  # a label without the fingerprint is refused
        scorer.load_labels({key1["order"][0]: {"label": "kept"}}, key1["draw"])


def test_notes_stay_private_and_partial_results_are_provisional(tmp_path):
    _, _, key = _built()
    kd = tmp_path / "data" / "raw" / "cal"
    kd.mkdir(parents=True)
    kp = kd / "key.private.json"
    kp.write_text(json.dumps(key))
    held_lost = [c for c in key["order"] if key["cards"][c]["judge_class"] == "lost"
                 and key["cards"][c]["half"] == "heldout"]
    first = held_lost[0]
    rows = [{"id": first, "collection": "labels",
             "data": {"label": "dropped", "note": "SENTINEL private quote", "draw": key["draw"]}}]
    lp = tmp_path / "labels.json"
    lp.write_text(json.dumps(rows))
    out = tmp_path / "results" / "calibration.json"
    assert scorer.main(["--labels", str(lp), "--key", str(kp), "--out", str(out)]) == 1
    pub = out.read_text()
    assert "SENTINEL" not in pub
    assert "SENTINEL" in (kd / scorer.PRIVATE_NAME).read_text()
    assert (kd / scorer.PRIVATE_NAME).stat().st_mode & 0o077 == 0
    res = json.loads(pub)
    assert res["heldout"]["precision_lost"]["p"] == 1.0
    assert res["decision"].startswith("PROVISIONAL") and res["decision_final"] is False
    assert res["precision_lost_bounds"]["n_unresolved"] == len(held_lost) - 1
    # every other held-out lost card "can't tell": still provisional
    rows += [{"id": c, "collection": "labels", "data": {"label": "cant_tell",
                                                         "draw": key["draw"]}}
             for c in key["order"] if c != first]
    res = scorer.score(key, scorer.load_labels(rows, key["draw"]))
    assert res["complete"] and not res["decision_final"]


def test_scorer_reports_development_cards_separately():
    rows = _rows()
    keep = [r for r in rows if not (r["subject"] == "haiku" and r["half"] == "heldout"
                                    and r["lost"])]
    cards = page.draw(keep, seed=3)
    _, key = page.build(cards, view=_fake_view)
    labels = scorer.load_labels(_labels_for(key, lambda cid, k: k["judge_grade"]), key["draw"])
    res = scorer.score(key, labels)
    assert res["n_cards_by_half"]["development"] == 5 == res["n_fill"]["development"]
    assert res["development"]["precision_lost"]["n"] == 5
    assert res["heldout"]["precision_lost"]["n"] == 15
    assert res["decision_final"] and set(res["n_boundaries"]) == {"heldout", "development"}
