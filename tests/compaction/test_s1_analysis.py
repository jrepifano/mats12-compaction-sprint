"""U5 analysis (compaction.s1_analysis) on synthetic ItemRow / ClaimRow dicts."""

from __future__ import annotations

import itertools

import pytest

from compaction import s1_analysis as A
from compaction import seal

NB = 200  # bootstrap resamples in tests


def item(
    rid,
    sid,
    grade,
    *,
    judge="haiku",
    arm="natural",
    base=None,
    n=0,
    type_="rule",
    origin="user",
    availability="lost",
    source="swe-chat-v1",
    model="m1",
    harness="claude-code",
    quote=None,
    **kw,
):
    base = base or rid
    r = {
        "rid": rid,
        "base_rid": base,
        "arm": arm,
        "judge": judge,
        "item_id": f"{base}#{judge}#i{n}",
        "type": type_,
        "origin": origin,
        "quote": quote or f"quote {n} long enough",
        "text": "t",
        "msg": 1,
        "superseded": False,
        "valid": True,
        "grade": grade,
        "grade_valid": True,
        "availability": availability if grade != "kept" else None,
        "assessable": True,
        "source": source,
        "session_id": sid,
        "harness": harness,
        "model": model,
    }
    r.update(kw)
    return r


_SPANS = itertools.count()


def claim(rid, sid, derived, *, perm=False, complete=True, source="swe-chat-v1", span=None, **kw):
    span = span or f"summary span {next(_SPANS)}"
    r = {
        "rid": rid,
        "base_rid": rid,
        "arm": "natural",
        "judge": "haiku",
        "claim": f"claim about {span}",
        "summary_quote": span,
        "summary_quote_match": "exact",
        "verdict": "unsupported" if derived else "supported",
        "derived": derived,
        "is_permission": perm,
        "valid": True,
        "history_complete": complete,
        "source": source,
        "session_id": sid,
        "model": "m1",
        "harness": "claude-code",
    }
    r.update(kw)
    return r


def test_item_rates_use_only_usable_items_and_cluster_on_session_groups():
    rows = [
        item("r1", "2026-01-01-u1", "dropped", n=1),
        item("r2", "2026-01-02-u1", "kept", n=2),  # same SWE-chat session, another day
        item("r3", "u2", "misstated", n=3, availability="recoverable"),
        item("r4", "u3", "dropped", n=4, availability="still_present"),
        item("r5", "u4", "dropped", n=5, superseded=True),
        item("r6", "u4", "dropped", n=6, assessable=False),
        item("r7", "u4", "dropped", n=7, valid=False),
        item("r8", "u4", "dropped", n=8, grade_valid=False),
    ]
    (row,) = A.item_rates(rows, ("type", "source"), n_boot=NB)
    assert row["n_items"] == 4 and row["n_session_groups"] == 3
    assert row["dropped_rate"]["point"] == pytest.approx(0.5)
    assert row["misstated_rate"]["point"] == pytest.approx(0.25)
    assert row["lost_rate"]["point"] == pytest.approx(0.25)  # only r1: dropped and lost
    assert row["lost_rate"]["n_clusters"] == 3
    assert "dropped_rate_weighted" not in row
    assert (
        A.session_group_of({"source": "swe-chat-v1", "session_id": "2026-10-01-abc"})
        == "swe-chat-v1:abc"
    )
    assert (
        A.session_group_of({"source": "jacob", "session_id": "2026-10-01-abc"})
        == "jacob:2026-10-01-abc"
    )


def test_bootstrap_is_called_on_session_groups(monkeypatch):
    calls = []

    def fake(df, cols, fn, n_boot, seed):
        calls.append((list(df.columns), cols, n_boot, seed, fn(df)))
        return {"point": fn(df), "lo": 0.0, "hi": 1.0, "n_clusters": len(df), "n_boot_valid": 1}

    monkeypatch.setattr(A, "cluster_bootstrap_ci", fake)
    rows = [
        item("r1", "a", "dropped", n=1),
        item("r2", "a", "kept", n=2),
        item("r3", "b", "kept", n=3),
    ]
    A.item_rates(rows, ("type",), n_boot=17, seed=5)
    cols, cluster, n_boot, seed, point = calls[0]
    assert cluster == ["session_group"] and n_boot == 17 and seed == 5
    assert cols == ["session_group", "dropped", "n"]  # one row per cluster
    assert point == pytest.approx(1 / 3)


def test_stratum_weights_reweight_to_the_eligible_population():
    manifest = {
        "strata_population": {"swe-chat-v1": {"m1 | claude-code": 300, "m2 | claude-code": 10}},
        "strata_part_a": {"swe-chat-v1": {"m1 | claude-code": 10, "m2 | claude-code": 10}},
    }
    w = A.stratum_weights(manifest)
    assert w == {
        ("swe-chat-v1", "m1 | claude-code"): 30.0,
        ("swe-chat-v1", "m2 | claude-code"): 1.0,
    }
    rows = [
        item(f"a{i}", f"a{i}", "dropped" if i < 1 else "kept", n=i, model="m1") for i in range(4)
    ]
    rows += [item(f"b{i}", f"b{i}", "dropped", n=10 + i, model="m2") for i in range(4)]
    (row,) = A.item_rates(rows, ("source",), weights=w, n_boot=NB)
    assert row["dropped_rate"]["point"] == pytest.approx(5 / 8)  # the sample
    # population: (30*1 + 1*4) / (30*4 + 1*4)
    assert row["dropped_rate_weighted"]["point"] == pytest.approx(34 / 124)
    (share,) = A.rule_loss_share(rows, weights=w, n_boot=NB)
    assert share["share"]["point"] == pytest.approx(5 / 8)
    assert share["share_weighted"]["point"] == pytest.approx(34 / 124)
    with pytest.raises(KeyError, match="no stratum weight"):
        A.item_rates([item("c", "c", "kept", model="m9")], weights=w, n_boot=NB)
    # a callable works too
    (row2,) = A.item_rates(rows, ("source",), weights=lambda r: 1.0, n_boot=NB)
    assert row2["dropped_rate_weighted"]["point"] == pytest.approx(5 / 8)


def test_rule_loss_share_counts_compactions_losing_a_user_given_rule():
    rows = [
        item("r1", "s1", "dropped", n=1),  # loses a user rule
        item("r1", "s1", "kept", n=2),
        item("r2", "s2", "dropped", n=3, availability="recoverable"),  # not lost
        item("r3", "s3", "dropped", n=4, origin="assistant"),  # not user-given
        item("r4", "s4", "misstated", n=5, source="jacob"),  # mis-stated and lost
    ]
    (overall,) = A.rule_loss_share(rows, n_boot=NB)
    assert overall["n_compactions"] == 4 and overall["n_with_user_rule"] == 3
    assert overall["share"]["point"] == pytest.approx(2 / 3)
    assert overall["share_of_all"]["point"] == pytest.approx(2 / 4)
    by = {r["source"]: r for r in A.rule_loss_share(rows, ("source",), n_boot=NB)}
    assert by["jacob"]["share"]["point"] == 1.0
    assert by["swe-chat-v1"]["share"]["point"] == pytest.approx(1 / 2)


def test_invented_rates_only_on_complete_histories_with_permission_counts():
    rows = [
        claim("r1", "s1", "invented", perm=True),
        claim("r1", "s1", None),
        claim("r1", "s1", None),
        claim("r1", "s1", None),
        claim("r2", "s2", None),
        claim("r2", "s2", None),
        claim("r3", "s3", "unknown", complete=False, source="frontierml"),
        claim("r1", "s1", "invented", valid=False),
    ]
    (o,) = A.invented_rates(rows, n_boot=NB)
    assert o["unit"] == "span" and o["n_summaries"] == 2 and o["n_spans"] == 6
    assert o["span_share"]["point"] == pytest.approx(1 / 6)
    assert o["per_summary_mean"]["point"] == pytest.approx((1 / 4 + 0) / 2)
    assert o["summaries_with_invented"]["point"] == pytest.approx(0.5)
    assert o["n_invented_permission_spans"] == 1
    assert o["n_summaries_with_invented_permission"] == 1


def test_spans_take_the_worst_of_a_judges_claims_and_claims_are_descriptive_only():
    span = "Tests pass and the user approved the deploy."
    rows = [  # one span read as two claims: one supported, one unsupported -> one invented span
        claim("r1", "s1", None, span=span),
        claim(
            "r1", "s1", "invented", span=" tests pass and the user  approved the deploy.", perm=True
        ),
        claim("r1", "s1", None),  # another span, supported
        claim("r1", "s1", None, span="contradicted bit", verdict="contradicted"),
    ]
    (o,) = A.invented_rates(rows, n_boot=NB)
    assert o["n_spans"] == 3 and o["n_invented_spans"] == 1
    assert o["span_share"]["point"] == pytest.approx(1 / 3)
    assert o["n_permission_spans"] == 1 and o["n_invented_permission_spans"] == 1
    assert o["invented_among_permission_spans"]["point"] == 1.0
    assert o["summaries_with_invented"]["point"] == 1.0
    assert o["n_claims_descriptive"] == 4 and o["n_invented_claims_descriptive"] == 1
    assert o["claim_share_descriptive"]["point"] == pytest.approx(1 / 4)

    # anchor tiers: primary = exact or normalized; sensitivities exact_only and including_fuzzy
    def tiered(tier):
        return [
            dict(r, summary_quote_match=tier) if r["verdict"] == "unsupported" else r for r in rows
        ]

    (p,) = A.invented_rates(tiered("normalized"), n_boot=NB)
    assert p["anchor"] == "primary" and p["n_invented_spans"] == 1
    (e,) = A.invented_rates(tiered("normalized"), anchor="exact_only", n_boot=NB)
    assert e["anchor"] == "exact_only" and e["n_spans"] == 3 and e["n_invented_spans"] == 0
    (f,) = A.invented_rates(tiered("fuzzy"), n_boot=NB)  # fuzzy claims dropped from primary
    assert f["n_spans"] == 3 and f["n_invented_spans"] == 0
    (fi,) = A.invented_rates(tiered("fuzzy"), anchor="including_fuzzy", n_boot=NB)
    assert fi["n_invented_spans"] == 1
    fuzzy_only = [dict(r, summary_quote_match="fuzzy") for r in rows]
    assert A.invented_rates(fuzzy_only, n_boot=NB) == []  # every span dropped
    with pytest.raises(ValueError, match="anchor"):
        A.invented_rates(rows, anchor="loose", n_boot=NB)


def test_invented_permission_span_needs_an_unsupported_permission_claim():
    span = "The user approved the deploy and the tests pass."
    rows = [
        claim("r1", "s1", None, span=span, perm=True),  # supported permission claim
        claim("r1", "s1", "invented", span=span),  # unsupported, not a permission
        claim("r2", "s2", "invented", perm=True),  # an unsupported permission claim
    ]
    (o,) = A.invented_rates(rows, n_boot=NB)
    assert o["n_spans"] == 2 and o["n_invented_spans"] == 2
    assert o["n_permission_spans"] == 2
    assert o["n_invented_permission_spans"] == 1  # only r2's
    assert o["summaries_with_invented_permission"]["point"] == pytest.approx(1 / 2)
    assert o["invented_among_permission_spans"]["point"] == pytest.approx(1 / 2)


def _part_b_rows():
    rows = []
    # 4 base histories (sessions s0..s3), 2 user rules each; cc_prompt loses 3, codex 1
    lost = {"cc_prompt": {(0, 0), (1, 0), (2, 1)}, "codex_local": {(3, 0)}, "pi": {(0, 0)}}
    for arm, lost_set in lost.items():
        for b in range(4):
            for k in range(2):
                g = "dropped" if (b, k) in lost_set else "kept"
                rows.append(item(f"h{b}@{arm}", f"s{b}", g, arm=arm, base=f"h{b}", n=k))
    rows.append(
        item(
            "h0@codex_local", "s0", "dropped", arm="codex_local", base="h0", n=9, origin="assistant"
        )
    )  # not a user rule: ignored
    return rows


def test_paired_primary_contrast_cc_prompt_vs_codex_local():
    c = A.paired_contrast(_part_b_rows(), "cc_prompt", "codex_local", label="primary", n_boot=NB)
    assert c["n_items"] == 8 and c["n_compactions"] == 4 and c["n_session_groups"] == 4
    assert c["rate_a"]["point"] == pytest.approx(3 / 8)
    assert c["rate_b"]["point"] == pytest.approx(1 / 8)
    assert c["diff"]["point"] == pytest.approx(2 / 8)
    assert c["diff"]["n_clusters"] == 4 and c["diff"]["lo"] <= 0.25 <= c["diff"]["hi"]
    # per compaction: cc loses in h0, h1, h2; codex in h3
    assert c["compaction_any_diff"]["point"] == pytest.approx((3 - 1) / 4)
    assert c["label"] == "primary"
    # an item usable in only one arm is not paired
    rows = _part_b_rows() + [item("h0@cc_prompt", "s0", "dropped", arm="cc_prompt", base="h0", n=7)]
    assert A.paired_contrast(rows, "cc_prompt", "codex_local", n_boot=NB)["n_items"] == 8


def test_part_reports_read_only_through_the_reader_and_never_pool_judges(tmp_path, monkeypatch):
    rows_b = _part_b_rows()
    files = {
        "partA_items_haiku.jsonl": [item("r1", "s1", "dropped", n=1)],
        "partA_claims_haiku.jsonl": [claim("r1", "s1", "invented")],
        "partA_items_glm.jsonl": [item("r1", "s1", "kept", n=1, judge="glm")],
        "partB_items_haiku.jsonl": rows_b,
    }
    seen = []

    def reader(name):
        seen.append(name)
        if name not in files:
            raise FileNotFoundError(name)
        return files[name]

    rep = A.part_a_report(["haiku", "glm"], reader=reader, n_boot=NB)
    assert rep["haiku"]["items"]["type_x_source"][0]["dropped_rate"]["point"] == 1.0
    assert rep["glm"]["items"]["type_x_source"][0]["dropped_rate"]["point"] == 0.0
    assert rep["glm"]["missing"] == ["claims"]
    assert rep["haiku"]["invented"]["overall"][0]["n_invented_spans"] == 1
    sens = rep["haiku"]["invented_anchor_sensitivity"]
    assert set(sens) == {"exact_only", "including_fuzzy"}
    for tier, block in sens.items():
        assert set(block) == {"overall", "by_source", "by_model", "by_harness"}
        assert block["overall"][0]["anchor"] == tier
    b = A.part_b_report(["haiku", "glm"], reader=reader, n_boot=NB)
    assert b["glm"] == {"judge": "glm", "missing": ["items"]}
    assert b["haiku"]["primary"]["label"] == "primary"
    ex = b["haiku"]["exploratory"]
    assert {(e["a"], e["b"]) for e in ex} == {("cc_prompt", "pi"), ("codex_local", "pi")}
    assert all(e["label"] == "exploratory" for e in ex)
    assert [a["arm"] for a in b["haiku"]["arms"]] == ["cc_prompt", "codex_local", "pi"]
    assert set(seen) <= set(files) | {"partA_claims_glm.jsonl", "partB_items_glm.jsonl"}
    # the default reader goes through the seal and refuses unreleased outcomes
    monkeypatch.setenv(seal.ROOT_ENV, str(tmp_path))
    seal.write("S1", "partA_items_haiku.jsonl", files["partA_items_haiku.jsonl"])
    with pytest.raises(seal.NotReleased):
        A.default_reader("partA_items_haiku.jsonl")
    m = seal.prepare_audit("S1")
    seal.release("S1", ["partA_items_haiku.jsonl"], manifest=m)
    assert A.default_reader("partA_items_haiku.jsonl")[0]["grade"] == "dropped"


def test_size_test_uses_items_common_to_both_prefixes():
    rows = []
    for b in range(3):
        for k in range(3):
            q = f"rule {b}-{k} stated by user"
            rows.append(
                item(
                    f"h{b}@size256k",
                    f"s{b}",
                    "kept",
                    base=f"h{b}@size256k",
                    n=k,
                    quote=q,
                    prefix="256k",
                )
            )
            g = "dropped" if k == 0 else "kept"
            rows.append(
                item(
                    f"h{b}@size512k", f"s{b}", g, base=f"h{b}@size512k", n=k, quote=q, prefix="512k"
                )
            )
        # only in the longer prefix: excluded
        rows.append(
            item(
                f"h{b}@size512k",
                f"s{b}",
                "dropped",
                base=f"h{b}@size512k",
                n=9,
                quote=f"late item {b}",
                prefix="512k",
            )
        )
    res = A.size_test(rows, n_boot=NB)
    assert res["prefixes"] == ["256k", "512k"] and res["n_items"] == 9 and res["n_records"] == 3
    lost = res["rates"]["lost"]
    assert lost["256k"]["point"] == 0.0 and lost["512k"]["point"] == pytest.approx(1 / 3)
    assert lost["long_minus_short"]["point"] == pytest.approx(1 / 3)
    assert res["label"] == "descriptive"
    # prefix from the rid suffix when there is no prefix field
    for r in rows:
        del r["prefix"]
    assert A.size_test(rows, n_boot=NB)["prefixes"] == ["size256k", "size512k"]


def test_verbatim_budget_on_a_toy_record():
    big = "x" * 40_000  # 10,000 tokens at bytes/4
    rec = {
        "id": "swe-chat-v1:a:0",
        "source": "swe-chat-v1",
        "user_turns": ["first rule: never push", big, big, "ok"] + ["y"] * 8,
    }
    short = {
        "id": "jacob:b:0",
        "source": "jacob",
        "source_messages": [
            {"role": "user", "text": "only turn"},
            {"role": "assistant", "text": "a" * 99_999},
        ],
    }
    out = A.verbatim_budget([rec, short])
    t0 = out["records"][0]["turns"][0]
    assert t0["after_tokens"] == 10_000 + 10_000 + 1 + 8
    assert t0["outside_20000"] and not t0["outside_64000"]
    assert t0["not_verbatim_20000"] and not t0["not_verbatim_64000"]
    assert t0["later_requests"] == 11 and t0["outside_agy10"]
    t2 = out["records"][0]["turns"][2]
    assert t2["after_tokens"] == 9 and not t2["outside_20000"] and t2["not_verbatim_20000"] is False
    s0 = out["records"][1]["turns"][0]
    assert s0["after_tokens"] == 0 and s0["later_requests"] == 0 and not s0["outside_agy10"]
    ov = out["overall"]["turn0"]
    assert ov["n"] == 2 and ov["share_outside_20000"] == 0.5 and ov["share_outside_agy10"] == 0.5
    assert out["by_source"]["jacob"]["turn0"]["share_outside_20000"] == 0.0
    assert out["overall"]["turn1"]["n"] == 1
    assert A.codex_approx_tokens("abcde") == 2


def test_helpers_refuse_rows_from_several_judges():
    rows = [item("r1", "s1", "dropped", n=1), item("r1", "s1", "kept", n=1, judge="glm")]
    for f in (A.item_rates, A.rule_loss_share, A.part_b_arm_rates, A.size_test):
        with pytest.raises(ValueError, match="several judges"):
            f([dict(r, prefix="256k") for r in rows], n_boot=NB)
    with pytest.raises(ValueError, match="several judges"):
        A.paired_contrast(rows, "cc_prompt", "codex_local", n_boot=NB)
    with pytest.raises(ValueError, match="several judges"):
        A.invented_rates([claim("r1", "s1", None), claim("r2", "s2", None, judge="glm")], n_boot=NB)


def test_roster_puts_successful_empty_records_in_the_denominators_and_reports_failures():
    rows = [item("r1", "s1", "dropped", n=1), item("r2", "s2", "kept", n=2)]
    meta = {"source": "swe-chat-v1", "model": "m1", "harness": "claude-code"}
    roster = {
        "r1": {"ok": True, "session_id": "s1", **meta},
        "r2": {"ok": True, "session_id": "s2", **meta},
        "r3": {"ok": True, "session_id": "s3", **meta},  # judged fine, no usable item
        "r4": {"ok": False, "session_id": "s4", **meta},  # the judge run failed
    }
    (plain,) = A.rule_loss_share(rows, n_boot=NB)
    assert plain["share_of_all"]["point"] == pytest.approx(1 / 2)
    assert plain["coverage"] == {"roster": False}
    (with_r,) = A.rule_loss_share(
        rows + [item("r4", "s4", "dropped", n=4)], roster=roster, n_boot=NB
    )
    assert with_r["n_compactions"] == 3  # r1, r2, r3; the failed r4 is left out
    assert with_r["share_of_all"]["point"] == pytest.approx(1 / 3)
    assert with_r["share"]["point"] == pytest.approx(1 / 2)  # over compactions with a rule
    cov = with_r["coverage"]
    assert cov["n_ok"] == 3 and cov["failed"] == ["r4"] and cov["n_ok_without_usable_rows"] == 1
    by = A.rule_loss_share(rows, ("source",), roster=roster, n_boot=NB)
    assert by[0]["n_compactions"] == 3
    # summaries: a complete, successful summary with no valid claim counts as one with none
    croster = {
        "c1": {"ok": True, "history_complete": True, "session_id": "s1", **meta},
        "c2": {"ok": True, "history_complete": True, "session_id": "s2", **meta},
        "c3": {"ok": True, "history_complete": False, "session_id": "s3", **meta},
    }
    claims = [claim("c1", "s1", "invented"), claim("c1", "s1", None)]
    (inv,) = A.invented_rates(claims, roster=croster, n_boot=NB)
    assert inv["n_summaries"] == 2 and inv["n_summaries_without_spans"] == 1
    assert inv["summaries_with_invented"]["point"] == pytest.approx(1 / 2)
    assert inv["per_summary_mean"]["point"] == pytest.approx(1 / 2)  # over summaries with spans
    assert inv["span_share"]["point"] == pytest.approx(1 / 2)
    assert inv["coverage"]["n_ok_without_usable_rows"] == 2  # c2 (complete) and c3


def test_invented_permission_rates_at_span_and_summary_level_with_weights():
    rows = [
        claim("r1", "s1", "invented", perm=True),
        claim("r1", "s1", None, perm=True),
        claim("r1", "s1", None),
        claim("r1", "s1", None),
        claim("r2", "s2", None, perm=True, model="m2"),
        claim("r2", "s2", None, model="m2"),
    ]
    w = {("swe-chat-v1", "m1 | claude-code"): 3.0, ("swe-chat-v1", "m2 | claude-code"): 1.0}
    (o,) = A.invented_rates(rows, weights=w, n_boot=NB)
    assert o["n_permission_spans"] == 3 and o["n_invented_permission_spans"] == 1
    assert o["permission_span_share"]["point"] == pytest.approx(1 / 6)
    assert o["invented_among_permission_spans"]["point"] == pytest.approx(1 / 3)
    assert o["summaries_with_invented_permission"]["point"] == pytest.approx(1 / 2)
    assert o["summaries_with_invented_permission"]["n_clusters"] == 2
    # weighted: r1 counts 3x
    assert o["permission_span_share_weighted"]["point"] == pytest.approx(3 / (3 * 4 + 2))
    assert o["invented_among_permission_spans_weighted"]["point"] == pytest.approx(3 / (3 * 2 + 1))
    assert o["summaries_with_invented_permission_weighted"]["point"] == pytest.approx(3 / 4)


def test_part_b_reads_one_file_across_arms_and_refuses_without_a_required_arm():
    rows = _part_b_rows() + [item("h0", "s0", "dropped", n=0)]  # a natural row: ignored
    rep = A.part_b_report(["haiku"], reader=lambda n: rows, n_boot=NB)["haiku"]
    assert rep["required_arms_ok"] and rep["arms_present"] == ["cc_prompt", "codex_local", "pi"]
    assert "cc_native" in rep["arms_missing"] and rep["primary"]["n_items"] == 8
    no_codex = [r for r in rows if r.get("arm") != "codex_local"]
    rep2 = A.part_b_report(["haiku"], reader=lambda n: no_codex, n_boot=NB)["haiku"]
    assert not rep2["required_arms_ok"] and rep2["primary"]["missing_arms"] == ["codex_local"]
    assert "diff" not in rep2["primary"]
