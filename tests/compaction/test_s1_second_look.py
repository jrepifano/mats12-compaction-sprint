"""Tests for compaction.s1_second_look (spec D11 as amended 22 Oct): requests, code checks, overlay."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from compaction import s1_second_look as SL
from compaction import s1_types as T

MSGS = [
    "[m0] USER: train the model and report complex_responses",
    "[m1] TOOL: complex_responses: 0.3394 vs 0.2016 (+68%)",
    "[m2] ASSISTANT: v4-cp200 (1.2 epochs) scored best",
]
SUMMARY = "- `--commit <sha>` looks up the checkpoint by trailer\n- v4-cp200 scored best"


def rendered():
    text, spans, pos = "", {}, 0
    for i, m in enumerate(MSGS):
        spans[i] = (pos, pos + len(m))
        text += m + "\n"
        pos = len(text)
    return SimpleNamespace(text=text, spans=spans)


REC = {"id": "swe-chat-v1:abc:0", "source": "swe-chat-v1", "summary": SUMMARY}
CLAIM = {
    "claim_id": "swe-chat-v1:abc:0#haiku-max#c3",
    "rid": "swe-chat-v1:abc:0",
    "base_rid": "swe-chat-v1:abc:0",
    "arm": "natural",
    "judge": "haiku-max",
    "claim": "complex_responses rose from 0.2016 to 0.3394",
    "summary_quote": "v4-cp200 scored best",
    "verdict": "unsupported",
    "valid": True,
    "note": "0.2016 does not appear",
}
ITEM = {
    "item_id": "swe-chat-v1:abc:0#haiku-max#i5",
    "rid": "swe-chat-v1:abc:0",
    "base_rid": "swe-chat-v1:abc:0",
    "arm": "natural",
    "judge": "haiku-max",
    "type": "decision",
    "text": "--commit looks up the checkpoint by trailer",
    "quote": "make --commit look up the checkpoint",
    "grade": "dropped",
    "grade_valid": True,
    "valid": True,
    "origin": "user",
}
P = SL.prompts()


def resp(data, error=None):
    return T.Resp("k", data, None, error, meta={"tool_calls": [{"name": "Grep", "input": "{}"}]})


def claim_answer(verdict, quote=None, msg=None):
    return {
        "verdict": verdict,
        "evidence_quote": quote,
        "msg": msg,
        "searched": ["0.2016"],
        "note": "n",
    }


def item_answer(verdict, quote=None):
    return {"verdict": verdict, "summary_quote": quote, "searched": ["--commit"], "note": "n"}


def test_schemas_validate_their_answers():
    assert not T.validate(claim_answer("supported", "x" * 10, 1), SL.CLAIM_SCHEMA)
    assert not T.validate(item_answer("dropped"), SL.ITEM_SCHEMA)
    assert T.validate({"verdict": "maybe"}, SL.CLAIM_SCHEMA)


def test_claim_flips_only_with_evidence_found_in_history():
    ok = SL.check_claim(
        CLAIM,
        resp(claim_answer("supported", "complex_responses: 0.3394 vs 0.2016", 0)),
        rendered(),
        P,
    )
    assert ok["valid"] and ok["verdict"] == "supported" and ok["changed"]
    assert ok["msg"] == 1  # the judge's wrong message number is corrected
    assert ok["tool_calls"] and ok["first_verdict"] == "unsupported"
    bad = SL.check_claim(
        CLAIM, resp(claim_answer("supported", "complex_responses: 0.5 vs 0.1", 1)), rendered(), P
    )
    assert not bad["valid"] and bad["verdict"] == "unsupported" and not bad["changed"]
    assert bad["invalid_reason"] == "evidence_quote_not_in_history"


def test_claim_unsupported_again_and_failed_call_keep_the_first_verdict():
    same = SL.check_claim(CLAIM, resp(claim_answer("unsupported")), rendered(), P)
    assert same["valid"] and same["verdict"] == "unsupported" and not same["changed"]
    err = SL.check_claim(CLAIM, resp(None, "timeout"), rendered(), P)
    assert (
        not err["valid"] and err["verdict"] == "unsupported" and err["invalid_reason"] == "timeout"
    )
    missing = SL.check_claim(CLAIM, None, rendered(), P)
    assert missing["invalid_reason"] == "missing_response"


def test_item_flips_only_with_a_summary_quote_found_in_the_summary():
    ok = SL.check_item(
        ITEM,
        resp(item_answer("kept", "`--commit <sha>` looks up the checkpoint by trailer")),
        SUMMARY,
        P,
    )
    assert ok["valid"] and ok["verdict"] == "kept" and ok["changed"] and ok["match"] == "exact"
    bad = SL.check_item(
        ITEM, resp(item_answer("kept", "the commit flag was rewritten entirely")), SUMMARY, P
    )
    assert not bad["valid"] and bad["verdict"] == "dropped"
    assert bad["invalid_reason"] == "summary_quote_not_in_summary"


def test_positives_are_valid_dropped_items_and_unsupported_claims():
    items = [ITEM, ITEM | {"grade": "kept"}, ITEM | {"valid": False}, ITEM | {"grade_valid": False}]
    claims = [CLAIM, CLAIM | {"verdict": "contradicted"}, CLAIM | {"valid": False}]
    its, cls = SL.positives(items, claims)
    assert its == [ITEM] and cls == [CLAIM]


class FakeMax:
    family = "anthropic"
    name = "haiku-max"

    def __init__(self, answers):
        self.answers, self.reqs = answers, []

    def run(self, reqs, *, purpose):
        self.reqs += reqs
        assert purpose == SL.PURPOSE
        return {r.key: resp(self.answers[r.key.split("|")[1]]) for r in reqs}


def test_second_look_builds_tool_requests_and_overlays():
    be = FakeMax(
        {
            "claim": claim_answer("supported", "v4-cp200 (1.2 epochs) scored best", 2),
            "item": item_answer("dropped"),
        }
    )
    import compaction.s1_judge as J

    orig = J.judge_view
    J.judge_view = lambda rec: rendered()
    try:
        rows = SL.second_look(
            be, [ITEM, ITEM | {"grade": "kept", "item_id": "x"}], [CLAIM], load_rec=lambda r: REC
        )
    finally:
        J.judge_view = orig
    assert {r.key for r in be.reqs} == {
        f"sl|claim|{CLAIM['claim_id']}",
        f"sl|item|{ITEM['item_id']}",
    }
    by = {r.key.split("|")[1]: r for r in be.reqs}
    assert by["claim"].files == (("history.txt", rendered().text),)
    assert by["item"].files == (("summary.txt", SUMMARY),)
    assert all(r.tools == SL.TOOLS and r.max_turns == SL.MAX_TURNS for r in be.reqs)
    assert "0.2016 does not appear" in by["claim"].user and MSGS[1] not in by["claim"].user
    assert all(not r.private for r in be.reqs)  # a public record
    out = {r["kind"]: r for r in rows}
    assert out["claim"]["verdict"] == "supported" and out["item"]["verdict"] == "dropped"
    applied = SL.apply([CLAIM, ITEM, ITEM | {"item_id": "other"}], rows)
    assert applied[0]["verdict"] == "supported" and applied[0]["second_look"]["changed"]
    assert applied[1]["grade"] == "dropped" and "second_look" in applied[1]
    assert "second_look" not in applied[2] and CLAIM["verdict"] == "unsupported"


def test_second_look_refuses_api_backends_and_unknown_arms():
    with pytest.raises(ValueError, match="Max"):
        SL.second_look(SimpleNamespace(family="openai"), [ITEM], [])
    with pytest.raises(ValueError, match="summary_of"):
        SL.second_look(FakeMax({}), [ITEM | {"arm": "cc_prompt"}], [], load_rec=lambda r: REC)


def test_fill_never_rescans_inserted_text():
    u = SL.fill(P["item"], summary="{quote} {text}", type="rule", text="T1", quote="Q1")
    assert "{quote} {text}" in u and "Item: T1" in u


def test_frozen_names_both_prompts():
    f = SL.frozen(P)
    assert set(f["prompts"]) == {"second_look_claim.md", "second_look_item.md"}
    assert f["tools"] == ["Grep", "Read"]


def _script():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "s1_second_look_cli", root / "scripts" / "s1_second_look.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _key():
    c = lambda i, v: {"id": f"c{i}", "claim": "x", "match": "exact", "verdict": v}
    return {
        "draw": "d",
        "units": {
            "u1": {
                "kind": "item",
                "half": "held",
                "row_ids": {"haiku-max": "i1"},
                "verdicts": {"haiku-max": "dropped"},
            },
            "u2": {
                "kind": "item",
                "half": "held",
                "row_ids": {"haiku-max": "i2"},
                "verdicts": {"haiku-max": "dropped"},
            },
            "u3": {
                "kind": "claim",
                "half": "held",
                "row_ids": {"haiku-max": "c1"},
                "claims": {"haiku-max": [c(1, "unsupported"), c(2, "unsupported")]},
                "verdicts": {"haiku-max": "unsupported"},
            },
        },
    }


def _ov(row_id, kind, verdict, cfg="cfg1"):
    return {"row_id": row_id, "kind": kind, "verdict": verdict, "config_sha": cfg}


def test_rekey_updates_nested_claims_and_keeps_the_worst():
    cli = _script()
    out, n = cli.rekey(_key(), [_ov("c1", "claim", "supported")], dev_ids={"u1"})
    u3 = out["units"]["u3"]
    assert u3["verdicts"]["haiku-max"] == "unsupported"  # c2 is still unsupported
    assert [c["verdict"] for c in u3["claims"]["haiku-max"]] == ["supported", "unsupported"]
    out, n = cli.rekey(
        _key(), [_ov("c1", "claim", "supported"), _ov("c2", "claim", "supported")], dev_ids=set()
    )
    assert out["units"]["u3"]["verdicts"]["haiku-max"] == "supported" and n["changed"] == 1


def test_rekey_splits_halves_excludes_by_availability_and_checks_provenance():
    cli = _script()
    av = {"i1": "reloaded", "i2": "lost"}.get
    out, n = cli.rekey(
        _key(),
        [_ov("i2", "item", "kept")],
        dev_ids={"u2"},
        availability_of=av,
        exclude="reloaded",
    )
    assert set(out["units"]) == {"u2", "u3"} and n["excluded"] == 1
    assert out["units"]["u2"]["half"] == "dev" and out["units"]["u3"]["half"] == "held"
    assert out["units"]["u2"]["verdicts"]["haiku-max"] == "kept"
    out, _ = cli.rekey(
        _key(),
        [],
        dev_ids=set(),
        availability_of={"i1": "still_present", "i2": "lost"}.get,
        exclude="not_lost",
    )
    assert set(out["units"]) == {"u2", "u3"}
    with pytest.raises(SystemExit, match="frozen second-look config"):
        cli.rekey(
            _key(),
            [_ov("c1", "claim", "supported", "cfg2")],
            dev_ids=set(),
            config="cfg1",
        )
    with pytest.raises(SystemExit, match="dev labels"):
        cli.rekey(_key(), [], dev_ids={"nope"})


def test_config_digest_changes_with_any_setting_not_only_prompts():
    cli = _script()
    cfg = SL.frozen(P)
    assert cli.config_sha(cfg) == cli.config_sha(dict(cfg))
    assert cli.config_sha(cfg | {"max_turns": cfg["max_turns"] + 1}) != cli.config_sha(cfg)
    assert cli.config_sha(cfg | {"tools": ["Grep"]}) != cli.config_sha(cfg)


def test_rekeyed_key_scores_the_d13_gate_on_held_units_only():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "s1_label_score_t", root / "scripts" / "s1_label_score.py"
    )
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    c = lambda i, v: {"id": f"c{i}", "claim": "x", "match": "exact", "verdict": v}
    units = {}
    for k in range(12):  # held items: 9 of 12 dropped calls right -> 0.75
        units[f"h{k}"] = {
            "kind": "item",
            "half": "held",
            "pi": 1.0,
            "row_ids": {"haiku-max": f"i{k}"},
            "verdicts": {"haiku-max": "dropped"},
        }
    units["dv"] = {
        "kind": "item",
        "half": "held",
        "pi": 1.0,
        "row_ids": {"haiku-max": "idv"},
        "verdicts": {"haiku-max": "dropped"},
    }
    for k in range(10):  # held claims, two claims per span
        units[f"s{k}"] = {
            "kind": "claim",
            "half": "held",
            "pi": 1.0,
            "history_complete": True,
            "anchor_tier": "exact",
            "row_ids": {"haiku-max": f"a{k}"},
            "claims": {"haiku-max": [c(f"a{k}", "unsupported"), c(f"b{k}", "unsupported")]},
            "verdicts": {"haiku-max": "unsupported"},
        }
    key = {"draw": "d", "units": units, "gold": {}}
    labels = {"labels": {u: {"label": "dropped", "draw": "d"} for u in units if u.startswith("h")}}
    for k in range(9, 12):
        labels["labels"][f"h{k}"] = {"label": "kept", "draw": "d"}
    labels["labels"]["dv"] = {"label": "kept", "draw": "d"}  # a dev miss must not count
    for k in range(10):
        labels["labels"][f"s{k}"] = {"label": "supported", "draw": "d"}
    # the second look clears both claims on 8 spans, only one claim on the other 2
    ov = [_ov(f"ca{k}", "claim", "supported") for k in range(10)] + [
        _ov(f"cb{k}", "claim", "supported") for k in range(8)
    ]
    out, _ = _script().rekey(key, ov, dev_ids={"dv"}, config="cfg1")
    rec = scorer.score(labels, out, gate_rule="d13")
    held = rec["per_judge"]["haiku-max"]["verdicts"]["dropped"]["held"]
    assert held["precision"]["point"] == pytest.approx(0.75)
    assert rec["gate"]["haiku-max"]["dropped"] == "PASS"
    inv = rec["per_judge"]["haiku-max"]["claim_gate_primary_tiers"]["held"]
    assert inv["precision"]["point"] == 0.0 and rec["gate"]["haiku-max"]["invented"] == "FAIL"
    # two spans keep an unsupported claim; Jacob says supported -> 0/2 precision, never 10 spans
    assert inv["precision"]["n"] == 2
