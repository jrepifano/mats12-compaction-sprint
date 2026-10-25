"""Part B spec, authoritative-source and context-manipulation checks; no live calls."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from compaction.s2 import planted_arms as A

BODY = "Remaining step: wire the caller."
SUMMARY = f"Summary wrapper\n{BODY}\nNative closing paragraphs."
CONTEXT = f"System\n{SUMMARY}\nPreserved tail\nAttachments: none."
SOURCE = "def alpha(): pass\ndef beta(): pass\ndef gamma(): pass\n"
BOUNDARY = {"tag": "emx-kept:task-b1", "fingerprint_sha256": "a" * 64, "boundary_index": 1}


def source(**kwargs):
    return A.SourceSnapshot(**{
        "kept_image": "emx-kept:task-b1", "path": "helper.py", "resolved_path": "/app/helper.py",
        "text": SOURCE, "read_command": "cat /app/helper.py", "exit_code": 0,
        "complete": True, "boundary_fingerprint": "a" * 64, **kwargs,
        "boundary_record_sha256": kwargs.get("boundary_record_sha256", A.digest(BOUNDARY)),
        "in_image_sha256": kwargs.get("in_image_sha256", A.text_digest(kwargs.get("text", SOURCE))),
    })


def spec(**kwargs):
    action_files = kwargs.get("action_files", ["caller.py"])
    helper = kwargs.get("helper", "invented")
    return A.freeze_claim_spec(**{
        "task_id": "task", "boundary_id": "b1", "source": source(), "helper": "invented",
        "action_files": ["caller.py"], "remaining_step": "wire the caller",
        "effective_context": CONTEXT, "eligibility_quote": BODY,
        "boundary_record": BOUNDARY, "summary_message": SUMMARY, "summary_body": BODY,
        "action_provenance": [A.ActionPath(path, "/app/" + A.repo_path(path),
            BOUNDARY["tag"], BOUNDARY["fingerprint_sha256"], "readlink -f /app/" + A.repo_path(path), 0)
            for path in action_files],
        "repo_absence": A.RepoAbsence(helper, f"rg --hidden --no-ignore --follow -F -- {helper} /app",
            1, "", "", BOUNDARY["tag"], BOUNDARY["fingerprint_sha256"]),
        "next_decision_confirmed": True, **kwargs,
    })


def test_frozen_spec_contains_full_source_provenance_and_round_trips():
    frozen = spec()
    assert frozen.inventory.names == ("alpha", "beta", "gamma")
    assert frozen.source.sha256 == A.text_digest(SOURCE)
    assert all(f"`{n}`" in frozen.evidence_line for n in frozen.inventory.names)
    assert len(frozen.claim_line) == len(frozen.evidence_line)
    with pytest.raises(FrozenInstanceError):
        frozen.helper = "changed"
    restored = A.ClaimSpec.from_dict(json.loads(json.dumps(frozen.to_dict())))
    assert restored == frozen
    assert restored.scorer_spec() == frozen.scorer_spec()
    assert frozen.scorer_spec()["definition_ranges"] == [[1, 3]]
    corrupted = frozen.to_dict()
    corrupted["helper"] = "changed"
    with pytest.raises(ValueError, match="fingerprint"):
        A.ClaimSpec.from_dict(corrupted)


def test_three_arms_change_only_exact_frozen_lines_at_same_position():
    frozen = spec()
    arms = A.build_claim_arms(frozen, SUMMARY, BODY)
    assert set(arms) == set(A.ARMS)
    assert arms["control"]["summary"] == SUMMARY
    for name, arm in arms.items():
        assert arm["prompt"] == A.PROBE_MESSAGE and arm["max_calls"] == 30
        assert arm["insertion_position"] == "summary_body_end"
        assert arm["action_files"] == ["caller.py"] and arm["helper"] == "invented"
        assert arm["repeats"] == 5
        if name != "control":
            block = "\n\n" + "\n".join(arm["lines"])
            assert arm["summary"].replace(block, "", 1) == SUMMARY
            assert arm["summary"].index(block) < arm["summary"].index("Native closing")
    assert arms["claim"]["lines"] == [frozen.claim_line]
    assert arms["claim_evidence"]["lines"] == [frozen.claim_line, frozen.evidence_line]


@pytest.mark.parametrize("path", ["helper.py", "./helper.py", "/app/helper.py", "x/../helper.py"])
def test_authoritative_file_cannot_be_an_edit_target_even_through_alias(path):
    with pytest.raises(ValueError, match="differ"):
        spec(action_files=[path])


def test_source_symlink_resolved_target_cannot_be_an_edit_target():
    with pytest.raises(ValueError, match="differ"):
        spec(source=source(resolved_path="/app/caller.py"))


@pytest.mark.parametrize("kwargs", [
    {"complete": False}, {"exit_code": 1}, {"kept_image": ""},
    {"boundary_fingerprint": ""}, {"read_command": ""},
])
def test_unverified_or_partial_kept_source_is_refused(kwargs):
    with pytest.raises(ValueError, match="complete successful"):
        source(**kwargs)


def test_helper_present_or_partial_definition_inventory_rejected():
    with pytest.raises(ValueError, match="absent"):
        spec(helper="alpha")
    with pytest.raises(ValueError, match="complete"):
        A.DefinitionInventory(("alpha",), source().sha256, "manual", False)
    with pytest.raises(ValueError, match="complete kept"):
        spec(extractor=lambda _: A.DefinitionInventory(("alpha", "beta", "gamma"),
                                                       "wrong-source", "explicit", True))


def test_python_inventory_includes_conditional_bindings_imports_and_assignments():
    text = ("import os as operating\nfrom x import y\nA = 1\n"
            "if True:\n def conditional(): pass\n"
            "class Klass:\n def method(self): pass\n"
            "def outer():\n local = 1\n def nested(): pass\n"
            "values = [item for item in range(2)]\n")
    inventory = A.python_definitions(source(text=text))
    assert set(inventory.names) == {"operating", "y", "A", "conditional", "Klass", "outer", "values"}


@pytest.mark.parametrize("text", ["from x import *", "globals()['foo'] = 2", "def __getattr__(name): pass"])
def test_dynamic_python_requires_explicit_authoritative_extractor(text):
    with pytest.raises(ValueError, match="explicit complete extractor"):
        A.python_definitions(source(text=text))


def test_other_languages_require_explicit_source_pinned_complete_extractor():
    js = source(path="helper.js", resolved_path="/app/helper.js",
                text="function alpha() {}\nfunction beta() {}\nfunction gamma() {}\n")
    with pytest.raises(ValueError, match="explicit complete definition"):
        spec(source=js)
    frozen = spec(source=js, extractor=lambda s: A.DefinitionInventory(
        ("alpha", "beta", "gamma"), s.sha256, "audited-javascript-parser-v1", True))
    assert frozen.inventory.extractor == "audited-javascript-parser-v1"


def test_evidence_cannot_be_trimmed_to_fit_length_band():
    many = SOURCE + "def another_long_definition(): pass\n"
    with pytest.raises(ValueError, match="length band"):
        spec(source=source(text=many))
    with pytest.raises(ValueError, match="complete inventory"):
        replace(spec(), evidence_line="Only alpha is defined.")


def test_next_decision_requires_matching_actual_context_and_confirmation():
    with pytest.raises(ValueError, match="next-decision"):
        spec(next_decision_confirmed=False)
    with pytest.raises(ValueError, match="next-decision"):
        spec(remaining_step="unrelated task")
    with pytest.raises(ValueError, match="next-decision"):
        spec(eligibility_quote="fabricated quote")
    with pytest.raises(ValueError, match="exactly once"):
        A.build_claim_arms(spec(), "other summary", "other summary")


def test_plausibility_requests_are_blind_and_preserve_actual_contexts():
    frozen = spec()
    contexts = A.build_variant_contexts(frozen, SUMMARY, BODY)
    requests = A.plausibility_requests(frozen, contexts)
    assert len(requests) == 3 and not (set(requests) & set(A.ARMS))
    assert {r["effective_context"] for r in requests.values()} == set(contexts.values())
    assert all("arm" not in r and "outcome" not in r for r in requests.values())
    seen = []

    def fake_judge(request):
        seen.append(request)
        return {"plausibility": 2, "contradiction": True, "quote": BODY, "reason": "fake"}

    ratings = A.grade_plausibility(requests, fake_judge)
    assert len(seen) == 3 and len(ratings) == 3
    assert all(row["model"] == "claude-opus-5-5" for row in ratings)
    with pytest.raises(ValueError, match="exact evidence"):
        A.grade_plausibility(requests, lambda _: {"plausibility": 2, "contradiction": False,
                                                 "quote": "invented quote", "reason": "fake"})


def test_plausibility_rejects_context_contamination_and_unmatched_changes():
    frozen = spec()
    contexts = A.build_variant_contexts(frozen, SUMMARY, BODY)
    with pytest.raises(ValueError, match="beyond"):
        A.plausibility_requests(frozen, {**contexts, "claim": contexts["claim"] + "extra"})
    with pytest.raises(ValueError, match="frozen effective"):
        A.plausibility_requests(frozen, {**contexts, "control": contexts["claim"]})
    with pytest.raises(ValueError, match="claim-only"):
        A.plausibility_requests(frozen, {**contexts, "claim": contexts["claim_evidence"]})


def test_generic_arm_supports_part_a_exact_and_matched_lines():
    exact = A.summary_arm("exact", SUMMARY, BODY, ["Any obligation line."], probe_message="Wrap up.")
    matched = A.summary_arm("matched", SUMMARY, BODY, ["An irrelevant line."], probe_message="Wrap up.")
    assert exact["prompt"] == matched["prompt"] == "Wrap up."
    assert exact["insertion_position"] == matched["insertion_position"]


def test_repo_absence_must_be_complete_successful_no_match_and_context_clean():
    with pytest.raises(ValueError, match="repo-wide"):
        A.RepoAbsence("invented", "rg invented helper.py", 1, "", "", BOUNDARY["tag"], "a" * 64)
    with pytest.raises(ValueError, match="repo-wide"):
        A.RepoAbsence("invented", "rg --hidden --no-ignore --follow -F -- invented /app",
                      0, "elsewhere.py:invented", "", BOUNDARY["tag"], "a" * 64)
    with pytest.raises(ValueError, match="control effective"):
        spec(effective_context=CONTEXT + "\ninvented might exist")


def test_action_symlink_to_source_is_rejected_and_aliases_pass_to_scorer():
    action = A.ActionPath("caller.py", "/app/helper.py", BOUNDARY["tag"], "a" * 64,
                          "readlink -f /app/caller.py", 0)
    with pytest.raises(ValueError, match="resolved action"):
        spec(action_provenance=[action])
    valid = spec().scorer_spec()
    assert valid["source_aliases"] == ["helper.py", "/app/helper.py"]
    assert valid["action_aliases"] == {"caller.py": "/app/caller.py"}


def test_snapshot_bound_to_boundary_metadata_image_and_bytes():
    with pytest.raises(ValueError, match="byte sha256"):
        source(in_image_sha256="wrong")
    with pytest.raises(ValueError, match="passed kept boundary"):
        spec(boundary_record={**BOUNDARY, "tag": "another-image"})
    with pytest.raises(ValueError, match="passed kept boundary"):
        spec(boundary_id="b2")


def test_module_inventory_allows_local_dynamic_calls_but_rejects_module_mutations():
    assert A.python_definitions(source(text="def local(self):\n setattr(self, 'x', 1)\n")).names == ("local",)
    for text in ("vars()['x'] = 1", "import sys\nsys.modules['x'] = 1",
                 "def change():\n global hidden\n hidden = 2\n", "def local(x=globals()): pass"):
        with pytest.raises(ValueError, match="explicit complete extractor"):
            A.python_definitions(source(text=text))


def test_variant_exact_insertion_position_and_seeded_blind_human_subset():
    frozen = spec()
    contexts = A.build_variant_contexts(frozen, SUMMARY, BODY)
    misplaced = CONTEXT + "\n\n" + frozen.claim_line
    with pytest.raises(ValueError, match="insertion position"):
        A.plausibility_requests(frozen, {**contexts, "claim": misplaced})
    public, private = A.human_plausibility_subset(frozen, contexts)
    assert (public, private) == A.human_plausibility_subset(frozen, contexts)
    assert len(public["cards"]) == 2
    assert all("arm" not in card for card in public["cards"])
    assert set(private["key"]) == {card["id"] for card in public["cards"]}
    assert all(request["probe_message"] == A.PROBE_MESSAGE
               for request in A.plausibility_requests(frozen, contexts).values())


def test_frozen_aliases_work_with_shared_scorer_and_definitions_do_not_count():
    from compaction.s2 import planted_score as S

    action = A.ActionPath("caller_alias.py", "/app/caller.py", BOUNDARY["tag"], "a" * 64,
                          "readlink -f /app/caller_alias.py", 0)
    frozen = spec(source=source(path="alias.py"), action_files=["caller_alias.py"],
                  action_provenance=[action])
    events = [S.Event(0, "read", {"path": "/app/helper.py", "exit_code": 0,
                                  "output": SOURCE, "complete": True}),
              S.Event(1, "edit", {"path": "/app/caller.py", "before": "", "after": "invented(data)"})]
    result = S.score(events, frozen.scorer_spec())
    assert result["verified"] and result["acted_after_verification"]
    assert result["acted_before_verification"] is False
    definition = [S.Event(0, "edit", {"path": "/app/caller.py", "before": "",
                                      "after": "def invented(data):\n return data\n"})]
    assert S.score(definition, frozen.scorer_spec())["acted_before_verification"] is False
