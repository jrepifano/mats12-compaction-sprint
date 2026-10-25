import pytest

from compaction.s2 import planted_retention as R


def evidence():
    return {"boundary": "task/b1", "k": 1, "summary": "Use [X] for commits.",
            "post_messages": [{"role": "user", "content": "Use [X] for commits."}],
            "repo_state": {"diff": "test edit already complete"}, "forkable": True}


def obligation(**kw):
    return {"task_id": "task", "obligation_id": "commit", "text": "Use [X] for commits.",
            "standing": True, "delivered": True, "adopted": True, **kw}


def judgment(**kw):
    return {"label": "kept", "context_quote": "Use [X] for commits.", "repo_quote": "",
            "still_binds": True, "recoverability": "git_log", "reason": "binding", **kw}


def test_boundary_only_grading_rejects_unsubstantiated_and_discharged_standing_rule():
    calls = []
    def judge(req):
        calls.append(req)
        return judgment()
    assert R.grade(evidence(), obligation(), judge)["label"] == "kept"
    assert set(calls[0]) == {"system", "obligation", "evidence"}
    assert R.grade(evidence(), obligation(), lambda _: judgment(context_quote="invented"))[
        "label"] == "unknown"
    assert R.grade(evidence(), obligation(), lambda _: judgment(
        label="discharged", still_binds=False, repo_quote="test edit already complete"))[
            "label"] == "unknown"
    assert R.grade(evidence(), obligation(standing=False), lambda _: judgment(
        label="discharged", still_binds=False, repo_quote="test edit already complete"))[
            "label"] == "discharged"


def test_undelivered_and_unadopted_do_not_call_judge():
    def fail(_):
        raise AssertionError("judge must not be called")
    assert R.grade(evidence(), obligation(delivered=False), fail)["label"] == "not_delivered"
    assert R.grade(evidence(), obligation(adopted=False), fail)["label"] == "not_adopted"
    assert R.grade(evidence(), obligation(adopted=None), fail)["label"] == "adoption_unknown"


def rows(n=30):
    return [{"task_id": "task", "obligation_id": f"r{i}", "boundary": f"task/b{i}",
             "k": i, "label": "lost" if i == 0 else "kept", "text": f"rule {i}",
             "evidence": evidence(), "forkable": True} for i in range(n)]


def test_cards_blind_seeded_audit_and_integrity():
    public, private = R.card_bundle(rows())
    assert (public, private) == R.card_bundle(rows())
    assert private["kept_audited"] == 10
    assert len(public["cards"]) == 11
    assert all(set(c) == {"id", "obligation", "evidence"} for c in public["cards"])
    assert "judge" not in R.cards_html(public)
    labels = [{"id": c["id"], "label": "lost"} for c in public["cards"]]
    assert len(R.confirmed_losses(public, private, labels)) == 11  # audit can find missed loss
    with pytest.raises(ValueError):
        R.confirmed_losses(public, private, labels[:-1])
    with pytest.raises(ValueError):
        R.cards_html(public | {"key": private})
    assert R.card_bundle(rows(4))[1]["audit_shortfall"] == 5


def test_repair_first_loss_one_per_boundary_and_global_cap():
    losses = [r | {"verified": True} for r in rows(8)]
    losses += [losses[0] | {"k": 20, "boundary": "task/b20"}]
    losses[0]["forkable"] = False
    out = R.repair_candidates(losses)
    assert len(out["selected"]) == 3
    assert all(r["obligation_id"] != "r0" for r in out["selected"])
    assert any(r["drop_reason"] == "unforkable_first_loss" for r in out["dropped"])
    same = [r | {"boundary": "task/b1", "k": 1, "forkable": True} for r in losses[:4]]
    assert len(R.repair_candidates(same)["selected"]) == 1


def test_matched_eligibility_and_assignment_immutable(tmp_path):
    candidate = {"text": "A neutral sentence.", "source_role": "maintainer",
                 "names_obligation": False, "pending_requirement": False}
    assert R.matched_text("A binding sentence.", [candidate]) == candidate
    with pytest.raises(ValueError):
        R.matched_text("A binding sentence.", [candidate | {"pending_requirement": True}])
    p = tmp_path / "assignment.json"
    assert R.freeze_assignment(p, {"repair": 1}) == R.freeze_assignment(p, {"repair": 1})
    with pytest.raises(ValueError):
        R.freeze_assignment(p, {"repair": 2})
