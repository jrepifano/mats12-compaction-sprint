import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "s1_run", Path(__file__).resolve().parents[2] / "scripts" / "s1_run.py"
)
s1_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s1_run)


def test_roster_of_splits_item_and_claim_failures():
    errors = [
        {"rid": "a", "pass": "2", "error": "cli_error"},
        {"rid": "b@cc_prompt", "pass": "1b", "error": "schema"},
        {"rid": "c", "pass": "-", "error": "no replay"},
    ]
    r = s1_run.roster_of(["a", "b", "c", "d"], errors)
    assert r["a"] == {"items": True, "claims": False}
    assert r["b"] == {"items": False, "claims": True}
    assert r["c"] == {"items": False, "claims": False}
    assert r["d"] == {"items": True, "claims": True}


def test_sealed_roster_is_keyed_by_row_rid_with_metadata():
    sink = s1_run._Sealed("partB", "haiku-max")
    recs = {
        "swe-chat-v1:x:0": {
            "source": "swe-chat-v1",
            "model": "m",
            "harness": "claude-code",
            "session_id": "2026-01-02-x",
            "session_group": "x",
            "history_complete": True,
        }
    }
    sink.add_run(recs, "codex_local", [{"rid": "swe-chat-v1:x:0@codex_local", "pass": "2"}])
    row = sink.roster["swe-chat-v1:x:0@codex_local"]
    assert row["ok"] is False and row["items_ok"] is True and row["claims_ok"] is False
    assert row["session_group"] == "x" and row["history_complete"] is True
    assert row["arm"] == "codex_local"


def test_update_roster_merges_judges(tmp_path):
    p = tmp_path / "roster.json"
    s1_run.update_roster(p, "haiku-max", {"r1": {"items": True, "claims": True}})
    s1_run.update_roster(p, "glm", {"r1": {"items": True, "claims": False}})
    import json

    r = json.loads(p.read_text())
    assert set(r["r1"]) == {"haiku-max", "glm"} and r["r1"]["glm"]["claims"] is False


class _Be:
    def __init__(self, family):
        self.family, self.name = family, f"{family}-judge"


def _fake_judge(calls):
    """judge_records stand-in: one item and one claim row per record, recorded per call."""

    def judge_records(be, rids, *, arm, replays, out_items, out_claims, out_code_facts, **kw):
        calls.append((arm, list(rids)))
        out_items([{"rid": r if arm == "natural" else f"{r}@{arm}", "arm": arm} for r in rids])
        out_claims([{"rid": r if arm == "natural" else f"{r}@{arm}", "c": 1} for r in rids])
        out_code_facts([])
        return {"errors": [], "valid_share": 1.0}

    return judge_records


def _recs(monkeypatch, rids):
    monkeypatch.setattr(s1_run.s1_data, "load", lambda r: {"id": r, "source": "s"})


def test_max_judges_batch_200_api_judges_50(monkeypatch):
    calls = []
    monkeypatch.setattr(s1_run.s1_judge, "judge_records", _fake_judge(calls))
    rids = [f"r{i}" for i in range(450)]
    _recs(monkeypatch, rids)
    s1_run._judge_into(s1_run._Sealed("partA", "x"), _Be("anthropic"), rids)
    assert [len(c[1]) for c in calls] == [200, 200, 50]
    calls.clear()
    s1_run._judge_into(s1_run._Sealed("partA", "x"), _Be("openai"), rids)
    assert [len(c[1]) for c in calls] == [50] * 9


def _arm_major(be, rids, reps_by_arm, monkeypatch):
    """The old cmd_part_b loop, for comparison."""
    sink = s1_run._Sealed("partB", be.name)
    for arm, reps in reps_by_arm.items():
        have = {r["base_rid"] for r in reps}
        s1_run._judge_into(sink, be, [r for r in rids if r in have], arm=arm, replays=reps, n=50)
    return sink


def test_part_b_is_record_major_with_the_same_rows(monkeypatch):
    rids = [f"r{i:02d}" for i in range(23)]
    _recs(monkeypatch, rids)
    reps_by_arm = {
        "cc_prompt": [{"base_rid": r} for r in rids],
        "codex_local": [{"base_rid": r} for r in rids if r != "r05"],  # one replay missing
    }
    calls = []
    monkeypatch.setattr(s1_run.s1_judge, "judge_records", _fake_judge(calls))
    sink = s1_run._Sealed("partB", "h")
    s1_run._part_b_into(sink, _Be("anthropic"), rids, reps_by_arm)
    # chunks of 10 records, both arms back to back per chunk
    assert [(a, len(r)) for a, r in calls] == [
        ("cc_prompt", 10), ("codex_local", 9), ("cc_prompt", 10), ("codex_local", 10),
        ("cc_prompt", 3), ("codex_local", 3),
    ]  # fmt: skip
    assert calls[0][1] == rids[:10] and calls[1][1] == [r for r in rids[:10] if r != "r05"]
    old = _arm_major(_Be("anthropic"), rids, reps_by_arm, monkeypatch)

    def key(rows):
        return sorted(json.dumps(r, sort_keys=True) for r in rows)

    assert key(sink.items) == key(old.items) and key(sink.claims) == key(old.claims)
    assert sink.roster == old.roster and len(sink.roster) == 45


def test_freeze_writes_once_then_refuses_a_changed_config(tmp_path, monkeypatch):
    monkeypatch.setattr(s1_run, "FROZEN", tmp_path / "frozen_config.json")
    cfg = s1_run.frozen_config()
    assert set(cfg["prompts"]) == {"pass1a_items.md", "pass1b_grade.md", "pass2_claims.md"}
    assert cfg["haiku"]["effort"] == "medium" and "glm" in cfg["routes"]
    assert set(cfg["replay_policies"]) == set(s1_run.T.ARMS) - {"natural"}
    s1_run.cmd_freeze(None)
    s1_run.cmd_freeze(None)  # unchanged: fine
    monkeypatch.setattr(s1_run, "HAIKU_EFFORT", "high")
    import pytest

    with pytest.raises(SystemExit, match="frozen"):
        s1_run.cmd_freeze(None)
    with pytest.raises(SystemExit, match="frozen"):  # bulk commands check before any work
        s1_run.cmd_part_a(None)
    # spec D12: an amendment archives the old snapshot unchanged and freezes the new config
    v1 = (tmp_path / "frozen_config.json").read_text()
    s1_run.cmd_freeze(SimpleNamespace(amend="D12"))
    assert (tmp_path / "frozen_config_v1.json").read_text() == v1
    new = json.loads((tmp_path / "frozen_config.json").read_text())
    assert new["version"] == 2 and new["amendment"] == "D12" and new["haiku"]["effort"] == "high"
    s1_run.check_frozen()  # the new snapshot matches
    s1_run.cmd_freeze(None)  # unchanged v2: a no-op
    (tmp_path / "frozen_config_v2.json").write_text("{}")
    monkeypatch.setattr(s1_run, "HAIKU_EFFORT", "low")
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        s1_run.cmd_freeze(SimpleNamespace(amend="D13"))  # never clobbers an archive
    monkeypatch.setattr(s1_run, "FROZEN", tmp_path / "missing.json")
    with pytest.raises(SystemExit, match="freeze` first"):
        s1_run.cmd_size(None)


def test_max_judge_gets_a_second_look_per_batch_with_replay_summaries(monkeypatch):
    rids = [f"r{i}" for i in range(3)]
    _recs(monkeypatch, rids)
    calls = []
    monkeypatch.setattr(s1_run.s1_judge, "judge_records", _fake_judge(calls))
    monkeypatch.setattr(s1_run.SL, "check_frozen", lambda: "cfg1")
    seen = []

    def fake_second_look(be, items, claims, *, load_rec, summary_of):
        seen.append((len(items), len(claims), summary_of))
        return [{"row_id": r["rid"], "changed": False, "valid": True} for r in items]

    monkeypatch.setattr(s1_run.SL, "second_look", fake_second_look)
    sink = s1_run._sink("partB", _Be("anthropic"))
    reps = [{"base_rid": r, "arm": "cc_prompt", "summary": f"S-{r}"} for r in rids]
    s1_run._judge_into(sink, _Be("anthropic"), rids, arm="cc_prompt", replays=reps, n=2)
    assert [(a, b) for a, b, _ in seen] == [(2, 0), (1, 0)]  # each batch's items; Part B: no claims
    summary_of = seen[0][2]
    assert summary_of({"rid": "r1@cc_prompt", "base_rid": "r1"}, {}) == "S-r1"
    assert len(sink.second_look) == 3 and all(r["config_sha"] == "cfg1" for r in sink.second_look)
    written = {}
    monkeypatch.setattr(
        s1_run.seal, "write", lambda stage, name, rows: written.update({name: rows})
    )
    assert sink.flush()["n_second_look"] == 3
    assert "partB_second_look_anthropic-judge.jsonl" in written
    api = s1_run._sink("partB", _Be("openai"))
    assert api.second_look is None
    s1_run._judge_into(api, _Be("openai"), rids)
    assert len(seen) == 2  # no second look for API judges


def test_bulk_commands_refuse_an_unfrozen_second_look(monkeypatch):
    def boom():
        raise s1_run.SL.FrozenError("differs")

    monkeypatch.setattr(s1_run.SL, "check_frozen", boom)
    import pytest

    with pytest.raises(SystemExit, match="differs"):
        s1_run._check_second_look("haiku,glm")
    s1_run._check_second_look("glm")  # API judges only: nothing to check


def test_part_a_second_look_rechecks_claims_too(monkeypatch):
    rids = ["r0", "r1"]
    _recs(monkeypatch, rids)
    monkeypatch.setattr(s1_run.s1_judge, "judge_records", _fake_judge([]))
    monkeypatch.setattr(s1_run.SL, "check_frozen", lambda: "cfg1")
    seen = []
    monkeypatch.setattr(
        s1_run.SL,
        "second_look",
        lambda be, items, claims, **kw: seen.append((len(items), len(claims))) or [],
    )
    s1_run._judge_into(s1_run._sink("partA", _Be("anthropic")), _Be("anthropic"), rids)
    assert seen == [(2, 2)]
