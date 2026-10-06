"""U1 scenarios (implementation plan §2): the bank round-trip through our generator renderer,
the draw, the sets and the classifier prompt ports."""

import ast
import json
import random

import pytest

from emx.i9 import contracts as c
from emx.i9 import scenarios as s


@pytest.fixture(scope="module")
def built():
    return s.build()


def test_bank_lynch_items_round_trip_byte_for_byte():
    lynch = [b for b in s.bank_scenarios() if b.lynch_conditions is not None]
    assert len(lynch) == 18
    assert {(b.lynch_conditions["goal_type"], b.lynch_conditions["goal_value"])
            for b in lynch} == set(c.BANK_GOAL_PAIRS)
    for b in lynch:
        lc = b.lynch_conditions
        system, text = s.render_condition(
            lc["scenario"], lc["goal_type"], lc["goal_value"], lc["urgency_type"]
        )
        assert system == b.system, b.id
        assert text == b.text, b.id
        # one descriptor per scenario type, independent of goal and urgency
        assert b.descriptor == s.LYNCH_DESCRIPTOR.format(scenario=lc["scenario"]), b.id


def test_bank_rows_follow_the_bank():
    items = {it["id"]: it for it in s.load_bank_items()}
    bank = s.bank_scenarios()
    assert len(bank) == c.N_BANK == len(items)
    for b in bank:
        it = items[b.id]
        assert (b.source, b.set) == ("bank", "bank")
        assert b.pinned_rollout == it["prefill"] and b.published_rate == it["misalignment_rate"]
        assert (b.arm, b.group, b.category) == (it["arm"], it["group"], it["category"])
        if it["group"] == "anthropic":
            lc = b.lynch_conditions
            assert b.id == c.scenario_id(lc["scenario"], lc["goal_type"], lc["goal_value"],
                                         lc["urgency_type"])
            assert b.scenario_type == lc["scenario"]
            assert b.template_group == c.template_group(lc["scenario"], lc["goal_value"])
        else:
            assert b.lynch_conditions is None
            assert b.scenario_type == it["category"] and b.template_group == f"bank|{b.id}"
    # the in-house "am-blackmail-full" is not a Lynch condition
    assert s.parse_lynch_id("am-blackmail-full") is None


def test_ids_unique_and_partition_sizes(built):
    rows, sets = built
    ids = [r.id for r in rows]
    assert len(ids) == len(set(ids)) == 112
    assert tuple(sets) == c.SET_KEYS
    assert [len(sets[k]) for k in c.SCENARIO_SETS] == [c.N_BANK, c.N_CONFIRMATORY, c.N_SEALED]
    for key in c.SCENARIO_SETS:
        assert set(sets[key]) == {r.id for r in rows if r.set == key}
        assert sets[key] == sorted(sets[key])
    conf, sealed = set(sets["confirmatory"]), set(sets["sealed"])
    bank_lynch = {r.id for r in rows if r.set == "bank" and r.lynch_conditions}
    assert not conf & sealed
    assert not (conf | sealed) & bank_lynch
    assert not (conf | sealed) & set(sets["bank"])


def test_subsets_sizes_and_parents(built):
    rows, sets = built
    by_id = {r.id: r for r in rows}
    for key, (parent, n) in c.SUBSETS.items():
        assert len(sets[key]) == len(set(sets[key])) == n, key
        assert set(sets[key]) <= set(sets[parent]), key
    sampled = set(sets["bank_sampled"])
    rated = {r.id for r in rows if r.set == "bank" and r.published_rate is not None}
    controls = {r.id for r in rows if r.set == "bank" and r.arm == "control"}
    assert len(rated) == 14 and len(controls) == 4 and not rated & controls
    assert sampled == rated | controls
    # validation: every category represented
    cats = {r.category for r in rows if r.set == "bank"}
    assert {by_id[i].category for i in sets["bank_validation"]} == cats
    expected_thinking = sorted(
        random.Random(c.THINKING_SUBSET_SEED).sample(sorted(sets["confirmatory"]), c.N_THINKING)
    )
    assert sets["thinking"] == expected_thinking


def test_no_prompt_contains_its_pinned_rollout(built):
    rows, _ = built
    with_rollout = [r for r in rows if r.pinned_rollout]
    assert len(with_rollout) == c.N_BANK
    for r in with_rollout:
        assert r.pinned_rollout not in r.system and r.pinned_rollout not in r.text, r.id
    for r in rows:
        assert c.to_bank_row(r)["prefill"] == ""


def test_draw_is_the_documented_procedure_and_deterministic(built):
    rows, sets = built
    pool = s.pool_conditions()
    assert len(pool) == c.POOL_SIZE
    assert [c.condition_id(*p) for p in pool] == sorted(c.condition_id(*p) for p in pool)
    drawn = random.Random(c.SCENARIO_DRAW_SEED).sample(pool, 80)
    assert sets["confirmatory"] == sorted(c.scenario_id(*p) for p in drawn[:64])
    assert sets["sealed"] == sorted(c.scenario_id(*p) for p in drawn[64:])
    assert s.draw_conditions() == s.draw_conditions()
    rows2, sets2 = s.build()
    assert sets2 == sets and rows2 == rows


def test_outputs_byte_identical_across_builds(built, tmp_path):
    rows, sets = built
    a, b = tmp_path / "a", tmp_path / "b"
    s.write_outputs(rows, sets, a / "scenarios.jsonl", a / "sets.json")
    s.write_outputs(*s.build(), b / "scenarios.jsonl", b / "sets.json")
    for name in ("scenarios.jsonl", "sets.json"):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    lines = (a / "scenarios.jsonl").read_text().splitlines()
    assert len(lines) == 112 and json.loads(lines[0])["id"] == rows[0].id


def test_generator_items_consistent_with_their_ids(built):
    rows, _ = built
    gen = [r for r in rows if r.source == "generator"]
    assert len(gen) == 80
    for r in gen:
        lc = r.lynch_conditions
        assert set(lc) == {"scenario", "goal_type", "goal_value", "urgency_type"}
        assert (lc["goal_type"], lc["goal_value"]) in c.POOL_GOAL_PAIRS
        assert r.id == c.scenario_id(lc["scenario"], lc["goal_type"], lc["goal_value"],
                                     lc["urgency_type"])
        assert r.template_group == c.template_group(lc["scenario"], lc["goal_value"])
        assert r.scenario_type == r.category == lc["scenario"]
        assert (r.arm, r.group, r.pinned_rollout, r.published_rate) == (
            "misaligned", "anthropic", "", None)
        assert r.descriptor == s.LYNCH_DESCRIPTOR.format(scenario=lc["scenario"])
        assert (r.system, r.text) == s.render_condition(
            lc["scenario"], lc["goal_type"], lc["goal_value"], lc["urgency_type"])
        assert s.email_content(r).startswith("<email>")


def test_goal_statement_follows_the_condition(built):
    rows, _ = built
    tmpl = s._templates()
    for r in rows:
        lc = r.lynch_conditions
        if r.source != "generator":
            continue
        goal = tmpl.GOAL_VARIABLES.get(f"{lc['goal_type']}-{lc['goal_value']}")
        if lc["goal_type"] == "explicit":
            assert goal["goal_statement"] in r.system
        else:  # latent and ambiguous carry no goal statement
            assert "Your primary goal" not in r.system


def test_only_the_known_upstream_gap_is_left_unfilled():
    for sc in c.LYNCH_SCENARIOS:
        for gt, gv in (*c.POOL_GOAL_PAIRS, *c.BANK_GOAL_PAIRS):
            for urg in c.LYNCH_URGENCY:
                _, text = s.render_condition(sc, gt, gv, urg)  # raises on any other gap
                cid = c.condition_id(sc, gt, gv, urg)
                assert ("${direction_modifier}" in text) == (cid in s.KNOWN_UPSTREAM_UNFILLED)


def _source_prompts(fn: str, prefix: str) -> tuple[str, str]:
    tree = ast.parse((c.LYNCH_DIR / "classifiers" / fn).read_text())
    vals = {n.targets[0].id: n.value for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
    return (ast.literal_eval(vals[f"{prefix}_CLASSIFICATION_SYSTEM_PROMPT"]),
            ast.literal_eval(vals[f"{prefix}_CLASSIFICATION_USER_PROMPT"]))


@pytest.mark.parametrize("scenario,fn,prefix", [
    ("blackmail", "blackmail_classifier.py", "BLACKMAIL"),
    ("leaking", "leak_classifier.py", "LEAK"),
    ("murder", "murder_classifier.py", "MURDER"),
])
def test_classifier_prompts_are_verbatim(scenario, fn, prefix):
    system, user = s.load_classifier_prompt(scenario)
    src_system, src_user = _source_prompts(fn, prefix)
    assert system == src_system.strip()
    assert user == src_user.strip()
    header = s.classifier_prompt_path(scenario).read_text().split("[system]")[0]
    assert fn in header and "ea0630e1" in header and "Adapted:" in header
    # filled as the source fills it (str.format), our template gives the source's prompt
    filled = {"response": "<reply>", "email_context": "<ctx>"}
    assert user.format(**filled) == src_user.format(**filled).strip()
