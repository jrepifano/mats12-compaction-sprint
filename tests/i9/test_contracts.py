"""Wave 0 contracts: the numbers the prereg v0.5 fixes, and the I9 spend caps."""

from emx import spend
from emx.i9 import contracts as c


def test_pool_and_sets():
    assert c.POOL_SIZE == 144
    assert len(c.POOL_GOAL_PAIRS) == 16 and not set(c.POOL_GOAL_PAIRS) & set(c.BANK_GOAL_PAIRS)
    assert c.SET_KEYS == ("bank", "confirmatory", "sealed", "bank_sampled", "bank_validation",
                          "thinking")
    assert c.N_CONFIRMATORY + c.N_SEALED <= c.POOL_SIZE
    assert c.N_BANK_SAMPLED == 18 and c.N_BANK_VALIDATION == 16


def test_layers():
    assert c.LADDER == (20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60, 63)
    assert len(c.GEMMA_LADDER) == len(c.LADDER)
    assert all(0 <= g < c.GEMMA_LAYERS for g in c.GEMMA_LADDER)
    assert list(c.GEMMA_LADDER) == sorted(set(c.GEMMA_LADDER))
    assert set(c.OLENS_LAYERS_CUT) <= set(c.OLENS_LAYERS)


def test_request_seed_is_stable_and_distinct():
    a = c.request_seed("qwen", False, "s1", 0)
    assert a == c.request_seed("qwen", False, "s1", 0)
    others = {c.request_seed(m, t, "s1", i) for m in c.MODELS for t in (False, True) for i in range(16)}
    assert len(others) == 2 * 2 * 16


def test_judge_labels_separate_routes_and_efforts():
    labels = {c.judge_label(m, e) for m in (c.LUNA, c.SONNET) for e in c.BC_EFFORT_CHOICES}
    assert len(labels) == 4 and all(lb.startswith("i9:") for lb in labels)
    assert c.judge_label(c.LUNA, "none") == "i9:luna:none"
    assert c.judge_label(c.SONNET) == "i9:sonnet-5"


def test_bank_row_never_carries_the_rollout():
    s = c.Scenario(
        id="x", source="bank", set="bank", category="blackmail", arm="misaligned",
        group="anthropic", template_group="blackmail|america", scenario_type="blackmail",
        system="sys", text="txt", descriptor="d", lynch_conditions=None, published_rate=0.5,
        pinned_rollout="the model's earlier reply",
    )
    row = c.to_bank_row(s)
    assert row["prefill"] == "" and row["misalignment_rate"] == 0.5
    assert "the model's earlier reply" not in str(row)


def test_spend_caps():
    caps = spend.caps_for("I9")
    assert caps.hard_cap_usd == 65.0 and caps.gpu_limit_usd == 36.0
    assert caps.judge_limit(10.0) == 65.0 - 2.0 - 10.0


def _generator_conditions(goal_types, goal_values):
    """The vendored generator's enumeration (scripts/generate_prompts.py, generate loop)."""
    out = set()
    for sc in c.LYNCH_SCENARIOS:
        for gt in goal_types:
            for gv in (["none"] if gt in ("none", "ambiguous") else goal_values):
                for ur in c.LYNCH_URGENCY:
                    out.add(c.condition_id(sc, gt, gv, ur))
    return out


def test_pool_matches_the_vendored_generator():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "spt", c.LYNCH_DIR / "templates" / "system_prompt_templates.py"
    )
    spt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(spt)
    keys = set(spt.GOAL_VARIABLES)
    for gt, gv in c.POOL_GOAL_PAIRS:
        assert (gt if gt == "ambiguous" else f"{gt}-{gv}") in keys
    full = _generator_conditions(["explicit", "latent", "ambiguous", "none"], list(c.GOAL_VALUES))
    bank = {
        c.condition_id(sc, gt, gv, ur)
        for sc in c.LYNCH_SCENARIOS for gt, gv in c.BANK_GOAL_PAIRS for ur in c.LYNCH_URGENCY
    }
    pool = {
        c.condition_id(sc, gt, gv, ur)
        for sc in c.LYNCH_SCENARIOS for gt, gv in c.POOL_GOAL_PAIRS for ur in c.LYNCH_URGENCY
    }
    assert len(bank) == 18 and len(pool) == 144 and full == bank | pool and not bank & pool


def test_bank_ids_use_the_generator_format():
    import json

    bank = json.loads(
        (c.REPO_ROOT / "third_party/workspace-bench/evals/agentic_misalignment/items.json")
        .read_text()
    )["items"]
    lynch_ids = {it["id"] for it in bank if it["group"] == "anthropic"}
    expected = {
        c.scenario_id(sc, gt, gv, ur)
        for sc in c.LYNCH_SCENARIOS for gt, gv in c.BANK_GOAL_PAIRS for ur in c.LYNCH_URGENCY
    }
    assert lynch_ids == expected
