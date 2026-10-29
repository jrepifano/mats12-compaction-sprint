"""Tests for compaction.s2.chain.summary_rot (week 2, experiment 4, summary rot). No model
calls."""

from __future__ import annotations

import pytest

from compaction.s2.chain import summary_rot as SR

TEXTS = {
    "drizzle-orm-window": {"repo": "drizzle-orm", "tag": "DRZ-712", "feature": "window fns",
                           "branch": "feature/window-function-builders"},
    "vitest-sharding": {"repo": "vitest", "tag": "VIT-295", "feature": "sharding",
                        "branch": "feature/duration-sharding"},
    "returns-validated": {"repo": "returns", "tag": "RET-292", "feature": "Validated",
                          "branch": "feature/validated-error-accumulation"},
    "koota-a": {"repo": "koota", "tag": "KOO-942", "feature": "aspects",
                "branch": "feature/composite-trait-aspects"},
    "koota-b": {"repo": "koota", "tag": "KOO-141", "feature": "snapshots",
                "branch": "feature/entity-snapshot-rollback"},
}


def tl(packages=(), context=(), instruction="", paths=()):
    return {"tokens": {"repo": [], "package": list(packages), "branch": [], "path": list(paths),
                       "binary": ["node"]},
            "context": list(context), "instruction": instruction}


TLS = {
    "drizzle-orm-window": tl(["drizzle-kit", "integration-tests"], context=["vitest"],
                             instruction="Add window function builders."),
    "vitest-sharding": tl(["@vitest/runner"], instruction="Shard test files by duration."),
    "returns-validated": tl(instruction="Add a Validated container that returns all errors."),
    "koota-a": tl(["@koota/core"], instruction="Add createAspect."),
    "koota-b": tl(["@koota/core"], instruction="Add snapshots."),
}


# -- entries -------------------------------------------------------------------------------


def test_split_entries_drops_structure_and_counts_it():
    s = ("## Goal\n\n**Task 2 (VIT-295, CURRENT):** shard by duration\n---\n```bash\n"
         "go test ./...\n```\n- [x] DRZ-712 done\n**Verification:**\nNo prior history.\n"
         "   \n### Done\n")
    entries, excl = SR.split_entries(s)
    assert entries == ["**Task 2 (VIT-295, CURRENT):** shard by duration", "go test ./...",
                       "- [x] DRZ-712 done"]
    assert excl == {"heading": 2, "separator": 1, "fence": 2, "bold_heading": 1,
                    "no_history": 1}


def test_split_entries_empty():
    assert SR.split_entries("") == ([], {})


# -- identifiers and the code matcher --------------------------------------------------------


def test_package_filter_keeps_repo_named_packages_only():
    ids = SR.task_identifiers("drizzle-orm-window", TEXTS, TLS["drizzle-orm-window"])
    kinds = {(d.kind, d.text) for d in ids}
    assert ("package", "drizzle-kit") in kinds
    assert ("package", "integration-tests") not in kinds
    assert {("tag", "DRZ-712"), ("branch", "feature/window-function-builders"),
            ("slug", "window-function-builders"), ("repo", "drizzle-orm"),
            ("repo", "drizzle"), ("task_id", "drizzle-orm-window")} <= kinds


def test_code_label_unique_multiple_none():
    idents = SR.chain_identifiers(["drizzle-orm-window", "returns-validated"], TEXTS, TLS)
    assert SR.code_label("- DRZ-712 pushed to `feature/window-function-builders`", idents) == \
        "task_1"
    assert SR.code_label("RET-292: Validated container in progress", idents) == "task_2"
    assert SR.code_label("Completed DRZ-712; now RET-292", idents) is None  # several
    assert SR.code_label("- No network access; finish with echo DONE", idents) is None  # none


def test_tags_match_whole_only_and_repo_case_insensitive():
    idents = SR.chain_identifiers(["drizzle-orm-window", "returns-validated"], TEXTS, TLS)
    assert SR.code_label("see DRZ-7120 and XDRZ-712", idents) is None
    assert SR.code_label("Drizzle monorepo uses pnpm", idents) == "task_1"
    assert SR.code_label("the drizzle_orm module", idents) is None  # "_" is a word char


def test_ambiguous_repo_word_needs_code_context():
    idents = SR.chain_identifiers(["drizzle-orm-window", "returns-validated"], TEXTS, TLS)
    assert SR.code_label("The function returns a tuple of errors", idents) is None
    assert SR.code_label("Origin for `returns` is /srv/git/returns.git", idents) == "task_2"
    assert SR.code_label("In the returns library, bind short-circuits", idents) == "task_2"


def test_identifier_shared_with_another_chain_task_is_dropped():
    # vitest is drizzle's test runner (a dependency), so "vitest" can't decide an entry
    # once drizzle is in the chain; its tag and scoped packages still can.
    both = SR.chain_identifiers(["vitest-sharding", "drizzle-orm-window"], TEXTS, TLS)
    assert "vitest" not in {d.text for d in both[1]}
    assert SR.code_label("run tests with vitest", both) is None
    assert SR.code_label("VIT-295 done; @vitest/runner patched", both) == "task_1"
    alone = SR.chain_identifiers(["vitest-sharding"], TEXTS, TLS)
    assert SR.code_label("run tests with vitest", alone) == "task_1"


def test_related_chain_repo_and_shared_packages_dont_decide():
    idents = SR.chain_identifiers(["koota-a", "koota-b"], TEXTS, TLS)
    assert SR.code_label("koota uses @koota/core", idents) is None
    assert SR.code_label("KOO-141 snapshot work", idents) == "task_2"
    assert SR.code_label("branch feature/composite-trait-aspects pushed", idents) == "task_1"


def test_foreign_tags():
    assert SR.foreign_tags("KOO-942 and DRZ-712", {"KOO-942"}, {"KOO-942", "DRZ-712",
                                                               "VIT-295"}) == ["DRZ-712"]
    assert SR.foreign_tags("KOO-942", {"KOO-942"}, {"KOO-942", "DRZ-712"}) == []


# -- the Opus answer -------------------------------------------------------------------------


def test_opus_req_lists_tasks_and_numbered_entries():
    tasks = [{"n": 1, "gist": "repo a"}, {"n": 2, "gist": "repo b"}]
    r = SR.opus_req("summary_rot:u1:k2", "u1:k2", tasks, ["x", "y"])
    assert "task_2 (just finished" in r.user and "[1] x" in r.user and "[2] y" in r.user
    assert r.schema["properties"]["labels"]["items"]["properties"]["label"]["enum"] == \
        ["task_1", "task_2", "generic"]
    assert not r.private


def test_parse_opus_ok_any_order():
    data = {"labels": [{"i": 2, "label": "generic", "tasks": []},
                       {"i": 1, "label": "task_1", "tasks": [1, 2, 9]}]}
    assert SR.parse_opus(data, 2, 2) == [{"label": "task_1", "tasks": [1, 2]},
                                         {"label": "generic", "tasks": []}]


@pytest.mark.parametrize("data", [
    None,
    {"labels": "x"},
    {"labels": [{"i": 1, "label": "task_1", "tasks": []}]},  # one missing
    {"labels": [{"i": 1, "label": "task_3", "tasks": []},
                {"i": 2, "label": "generic", "tasks": []}]},  # beyond k
    {"labels": [{"i": 1, "label": "generic", "tasks": []},
                {"i": 1, "label": "generic", "tasks": []}]},  # duplicate
    {"labels": [{"i": 0, "label": "generic", "tasks": []},
                {"i": 2, "label": "generic", "tasks": []}]},  # out of range
    {"labels": [{"i": 1, "label": "generic", "tasks": "1"},
                {"i": 2, "label": "generic", "tasks": []}]},  # bad tasks
])
def test_parse_opus_malformed(data):
    with pytest.raises(SR.Malformed):
        SR.parse_opus(data, 2, 2)


# -- shares ----------------------------------------------------------------------------------


def test_category():
    assert SR.category("task_4", 4) == "current"
    assert SR.category("task_1", 4) == "back_3"
    assert SR.category("generic", 4) == "generic"
    assert SR.category(None, 4) is None
    with pytest.raises(ValueError):
        SR.category("task_5", 4)


def test_summary_shares_leave_unlabelled_out():
    s = SR.summary_shares(["task_3", "task_3", "task_2", "task_1", "generic", None], 3)
    assert s["counts"] == {"current": 2, "back_1": 1, "back_2": 1, "back_3": 0, "generic": 1}
    assert s["n_labelled"] == 5 and s["n_unlabelled"] == 1
    assert s["shares"]["earlier"] == pytest.approx(2 / 5)
    assert s["shares"]["current"] == pytest.approx(2 / 5)


def test_chain_weighting_is_a_mean_over_chains_not_entries():
    big = SR.summary_shares(["task_2"] * 90 + ["task_1"] * 10, 2)["shares"]  # 10% earlier
    small = SR.summary_shares(["task_1"] * 5 + ["task_2"] * 5, 2)["shares"]  # 50% earlier
    cw = SR.chain_weighted([big, small])
    assert cw["n_chains"] == 2
    assert cw["earlier"] == pytest.approx(0.30)  # pooled would be 15/110
    assert cw["current"] == pytest.approx(0.70)


def test_chain_weighted_skips_empty_summaries():
    empty = SR.summary_shares([None], 2)["shares"]
    full = SR.summary_shares(["task_1"], 2)["shares"]
    assert SR.chain_weighted([empty, full])["n_chains"] == 1


def test_split_shares_spreads_multi_task_opus_entries():
    rows = [{"final_label": "task_2", "decided_by": "opus", "opus_tasks": [1, 2]},
            {"final_label": "task_2", "decided_by": "code", "opus_tasks": [1, 2]},
            {"final_label": "generic", "decided_by": "opus", "opus_tasks": []},
            {"final_label": None, "decided_by": None, "opus_tasks": None}]
    sh = SR.split_shares(rows, 2)
    assert sh["back_1"] == pytest.approx(0.5 / 3)
    assert sh["current"] == pytest.approx(1.5 / 3)
    assert sh["generic"] == pytest.approx(1 / 3)


def test_bootstrap_ci_seeded_and_bracketing():
    vals = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    a = SR.bootstrap_ci(vals, seed=7, n_boot=2000)
    assert a == SR.bootstrap_ci(vals, seed=7, n_boot=2000)
    assert a[0] < sum(vals) / len(vals) < a[1]
    assert SR.bootstrap_ci([0.3], seed=1) == (0.3, 0.3)
    assert SR.bootstrap_ci([], seed=1) == (None, None)


def test_structure_lines_and_bullets():
    s = "## Goal\n**KCP-256 (complete):**\n- a bullet\n  1. numbered\nprose line\n---\n"
    assert SR.structure_lines(s) == [("heading", "## Goal"),
                                     ("bold_heading", "**KCP-256 (complete):**"),
                                     ("separator", "---")]
    assert SR.is_bullet("- a bullet") and SR.is_bullet("1. numbered")
    assert SR.is_bullet("* star") and not SR.is_bullet("prose line")
    assert not SR.is_bullet("**Completed tasks:** DRZ-712")


def test_paired_bootstrap_diff_keeps_pairs():
    a = {"c1": 0.5, "c2": 0.6, "c3": 0.7, "c4": 0.8}
    b = {"c1": 0.1, "c2": 0.2, "c3": 0.3, "c4": 0.4, "c5": 0.9}  # c5 unpaired, ignored
    d = SR.paired_bootstrap_diff(a, b, seed=3, n_boot=2000)
    assert d["n_chains"] == 4 and d["diff"] == pytest.approx(0.4)
    assert d["ci95"][0] == pytest.approx(0.4) and d["ci95"][1] == pytest.approx(0.4)
    assert d["excludes_zero"]
    flat = SR.paired_bootstrap_diff({"x": 0.1, "y": 0.5}, {"x": 0.5, "y": 0.1}, seed=3)
    assert not flat["excludes_zero"]
