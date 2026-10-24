import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "s1_swechat_ext", ROOT / "scripts" / "s1_swechat_ext.py"
)
X = importlib.util.module_from_spec(spec)
spec.loader.exec_module(X)


def _item(rid, i, grade, availability="lost", quote=None, session="s"):
    return {
        "rid": rid,
        "base_rid": rid,
        "judge": "haiku-max",
        "item_id": f"{rid}#haiku-max#i{i}",
        "quote": quote or f"quote number {i} of {rid}",
        "type": "rule",
        "origin": "user",
        "grade": grade,
        "availability": availability,
        "valid": True,
        "grade_valid": True,
        "assessable": True,
        "source": "swe-chat-v1",
        "session_id": session,
        "model": "m",
        "harness": "claude-code",
    }


def test_extension_ids_are_eligible_swechat_outside_part_a():
    index = [
        {"id": "swe-chat-v1:a:0", "source": "swe-chat-v1", "eligible": True},
        {"id": "swe-chat-v1:b:0", "source": "swe-chat-v1", "eligible": True},
        {"id": "swe-chat-v1:c:0", "source": "swe-chat-v1", "eligible": False},
        {"id": "frontierml:d:0", "source": "frontierml", "eligible": True},
    ]
    manifest = {"ids": {"part_a": ["swe-chat-v1:a:0"]}}
    assert X.extension_ids(index, manifest) == ["swe-chat-v1:b:0"]


def test_run_order_is_seeded_and_a_prefix_extends():
    ids = [f"r{i}" for i in range(100)]
    o1, o2 = X.run_order(ids), X.run_order(list(reversed(ids)))
    assert o1 == o2 and sorted(o1) == sorted(ids) and o1 != sorted(ids)
    assert X.run_order(ids, seed="other") != o1


def test_draw_units_strata_and_inclusion_probabilities():
    items = []
    for r in range(6):
        rid = f"swe-chat-v1:r{r}:0"
        items += [_item(rid, 0, "dropped"), _item(rid, 1, "misstated"), _item(rid, 2, "kept")]
        items += [_item(rid, 3, "dropped", availability="reloaded")]  # not lost
    rids = {i["rid"] for i in items}
    units, frame = X.draw_units(items, rids, n_lost=5, n_kept=2)
    assert frame["item|lost|held"] == {"N": 12, "n": 5, "pi": 5 / 12}
    assert frame["item|kept|held"] == {"N": 6, "n": 2, "pi": 2 / 6}
    assert len(units) == 7 and len({u["unit_id"] for u in units}) == 7
    lost = [u for u in units if u["stratum"] == "item|lost|held"]
    assert all(u["verdicts"]["haiku-max"] in ("dropped", "misstated") for u in lost)
    assert all(u["availability"] == "lost" for u in lost)
    again, _ = X.draw_units(items, rids, n_lost=5, n_kept=2)
    assert [u["unit_id"] for u in again] == [u["unit_id"] for u in units]


def test_add_cant_tell_patches_the_item_first_template():
    html = X.LP._render({"units": [], "item_first": True}, ("labels",), "t", "h", "l")
    out = X.add_cant_tell(html)
    assert '"unsure", "Can\'t tell"' in out
    assert 'l.label = "cant_tell"' in out
    with pytest.raises(ValueError):
        X.add_cant_tell(out.replace(X._RUBRIC_NEW, ""))


def _key(units):
    return {"draw": "d1", "units": units}


def test_score_precision_excludes_cant_tell_and_counts_non_items_as_misses():
    units = {}
    labs = {}
    for i, lab in enumerate(["dropped", "misstated", "kept", "not_an_item", "cant_tell"]):
        units[f"u{i}"] = {"kind": "item", "stratum": "item|lost|held", "source": "swe-chat-v1",
                          "session_id": f"s{i}", "verdicts": {"haiku-max": "dropped"}}  # fmt: skip
        labs[f"u{i}"] = {"draw": "d1", "label": lab, "note": "look" if i == 0 else ""}
    units["k"] = {"kind": "item", "stratum": "item|kept|held", "source": "swe-chat-v1",
                  "session_id": "sk", "verdicts": {"haiku-max": "kept"}}  # fmt: skip
    labs["k"] = {"draw": "d1", "label": "kept"}
    res = X.score(_key(units), labs)
    lp = res["lost_precision"]
    assert (lp["n_hits"], lp["n"]) == (2, 4)
    assert res["kept_agreement"]["n_hits"] == 1
    assert res["n_cant_tell"] == 1
    assert res["notes"] == {"u0": "look"}
    assert 0 <= lp["wilson"][0] <= 0.5 <= lp["wilson"][1] <= 1


def test_score_refuses_labels_from_another_draw():
    units = {"u": {"kind": "item", "stratum": "item|lost|held", "verdicts": {}}}
    with pytest.raises(SystemExit):
        X.score(_key(units), {"u": {"draw": "other", "label": "kept"}})


def test_difference_is_b_minus_a_pooled():
    a = [_item("swe-chat-v1:a:0", i, "dropped" if i < 1 else "kept", session=f"a{i}")
         for i in range(4)]  # fmt: skip
    b = [_item("swe-chat-v1:b:0", i, "dropped" if i < 3 else "kept", session=f"b{i}")
         for i in range(4)]  # fmt: skip
    d = X.difference(a, b)
    assert d["point"] == pytest.approx(0.5)
    assert d["lo"] <= d["point"] <= d["hi"]


def test_difference_resamples_shared_session_groups_jointly():
    a = [_item("swe-chat-v1:a:0", i, "dropped" if i < 1 else "kept", session=f"g{i}")
         for i in range(4)]  # fmt: skip
    b = [_item("swe-chat-v1:b:0", i, "dropped" if i < 3 else "kept", session=f"g{i}")
         for i in range(4)]  # fmt: skip
    d = X.difference(a, b)
    assert d["n_session_groups"] == 4 and d["n_shared_session_groups"] == 4
    assert d["point"] == pytest.approx(0.5)


def test_points_only_drops_intervals():
    d = {"x": {"point": 0.2, "lo": 0.1, "hi": 0.3, "n_clusters": 5}, "n": 3}
    assert X.points_only(d) == {
        "x": {"point": 0.2, "lo": None, "hi": None, "n_clusters": None},
        "n": 3,
    }


def test_score_counts_cant_tell_as_labelled_and_flags_invalid_labels():
    units = {
        "u1": {"kind": "item", "stratum": "item|lost|held", "verdicts": {}},
        "u2": {"kind": "item", "stratum": "item|lost|held", "verdicts": {}},
        "u3": {"kind": "item", "stratum": "item|lost|held", "verdicts": {}},
    }
    labs = {
        "u1": {"draw": "d1", "label": "cant_tell"},
        "u2": {"draw": "d1", "label": "dropped"},
        "u3": {"draw": "d1", "label": "bogus"},
    }
    res = X.score(_key(units), labs)
    assert res["n_labelled"] == 2 and res["n_cant_tell"] == 1
    assert res["invalid_labels"] == {"u3": "bogus"}
    assert (res["lost_precision"]["n_hits"], res["lost_precision"]["n"]) == (1, 1)


def test_supervise_stops_its_child_when_it_is_terminated(monkeypatch):
    import subprocess
    import types

    started = []
    real = subprocess.Popen

    def fake_popen(cmd, **kw):
        p = real(["sleep", "60"], start_new_session=True)
        started.append(p)
        return p

    def boom():
        raise SystemExit(143)  # what the SIGTERM handler raises

    monkeypatch.setattr(X.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(X, "_wait_below", lambda: None)
    monkeypatch.setattr(X, "_usage_high", boom)
    monkeypatch.setattr(X, "CHECK_S", 0.01)
    monkeypatch.setattr(X.signal, "signal", lambda *a: None)
    with pytest.raises(SystemExit):
        X.cmd_supervise(types.SimpleNamespace(n=50, concurrency=None))
    assert started and started[0].poll() is not None


def test_page_command_builds_a_leak_checked_page(tmp_path, monkeypatch):
    import types

    rid = "swe-chat-v1:abc:0"
    history = "[m0] USER: " + " ".join(f"quote number {i} of {rid}" for i in range(30))
    items = [_item(rid, i, "dropped" if i % 2 else "kept") for i in range(30)]
    out = tmp_path / "out"
    out.mkdir()
    for f in ("items", "second_look"):
        (out / X.FILES[f]).write_text("")
    ext = {
        "items": items,
        "roster": {rid: {"items_ok": True, "session_group": "swe-chat-v1:abc"}},
        "meta": {"complete": True, "record_ids": [rid]},
    }
    monkeypatch.setattr(X, "load_extension", lambda: ext)
    monkeypatch.setattr(X, "previously_labelled_groups", lambda: set())
    monkeypatch.setattr(X, "OUT", out)
    monkeypatch.setattr(X, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(X.LP, "PRIVATE_ROOTS", [tmp_path])
    rec = {"source": "swe-chat-v1", "summary": "a short summary"}
    monkeypatch.setattr(X.s1_data, "load", lambda r: rec)
    monkeypatch.setattr(X.s1_data, "render", lambda r: types.SimpleNamespace(text=history))
    assert X.cmd_page(types.SimpleNamespace(force=False)) == 0
    page = (tmp_path / "private" / "page" / X.LP.PAGE_NAME).read_text()
    key = json.loads((tmp_path / "private" / "page" / X.LP.KEY_NAME).read_text())
    assert len(key["units"]) == 15 + 4 and key["cant_tell_patch"]
    assert '"cant_tell"' in page and "haiku-max" not in page and '"verdicts"' not in page


def test_pct_renders_points_without_bounds():
    assert X._pct({"point": 0.25, "lo": None, "hi": None}) == "25.0%"
    assert X._pct({"point": 0.25, "lo": 0.2, "hi": 0.3}) == "25.0% [20.0, 30.0]"


def test_summary_md_renders_a_points_only_population_estimate():
    ci = {"point": 0.3, "lo": 0.2, "hi": 0.4, "n_clusters": 3}
    one = {
        "lost_by_type": {"rule": {"n_items": 10, "lost_rate": ci}},
        "pooled": {"n_items": 10, "lost_rate": ci},
        "user_rule_loss": {"share": ci, "n_losing": 1, "n_with_user_rule": 2},
        "invented_exploratory": {"span_share": ci, "summaries_with_invented": ci,
                                 "n_invented_spans": 1, "n_spans": 5, "n_summaries": 2},
        "by_model": {"m": {"n_items": 10, "lost_rate": ci}},
        "user_rule_loss_by_model": {"m": {"share": ci, "n_losing": 1, "n_with_user_rule": 2}},
    }  # fmt: skip
    sets = {k: one for k in X.SET_NAMES}
    sets["population_estimate"] = X.points_only(one)
    s = {
        "sets": sets,
        "n_extension_judged": 50,
        "pool_size": 678,
        "extension_complete": True,
        "failed_records": [],
        "population_estimate_weights": {"extension": 13.56},
        "pooled_lost_difference_extension_minus_250": {"point": 0.01, "lo": -0.02, "hi": 0.04},
        "max_usage": {"calls": 3, "notional_usd": 1.5},
    }
    md = X.summary_md(s)
    assert "30.0% (10)" in md and "30.0% [20.0, 40.0]" in md


def test_rate_limit_detection_reads_error_fields_only(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"errors": [{"error": "HTTP 429 rate limit"}]}))
    (tmp_path / X.FILES["second_look"]).write_text(
        json.dumps({"error": None, "quote": "status 429 in the log"}) + "\n"
        + json.dumps({"error": "overloaded_error"}) + "\n"
    )  # fmt: skip
    assert X._rate_limited(tmp_path) == 2
