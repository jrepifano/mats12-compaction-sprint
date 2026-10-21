import importlib.util
from pathlib import Path

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
