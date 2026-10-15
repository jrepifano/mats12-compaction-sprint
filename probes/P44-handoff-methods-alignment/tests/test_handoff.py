"""P44 handoff builders, seed insertion and chain specs (no API, no Docker)."""
from __future__ import annotations

import json

import pytest
from p44 import contracts as C
from p44 import handoff as H

SYSTEM = {"role": "system", "content": "SYS"}
TASK = {"role": "user", "content": "## Task\nImplement it. **DO NOT MODIFY THE TESTS.**"}
SUMMARY = ("## Goal\nImplement f.\n\n## Constraints & Preferences\n- Do not modify the tests\n\n"
           "## Key Decisions\n- **Algorithm**: brute force\n\n## Next Steps\n1. Run pytest\n")


def turn(i: int, text: str = "") -> list[dict]:
    return [{"role": "assistant", "content": text,
             "tool_calls": [{"id": f"c{i}", "function": "execute_command",
                             "arguments": {"command": f"cmd{i}"}}]},
            {"role": "tool", "content": f"out{i}", "tool_call_id": f"c{i}",
             "function": "execute_command"}]


def test_seed_lengths_matched():
    n = {k: len(v) for k, v in C.SEED_TEXT.items()}
    assert max(n.values()) / min(n.values()) <= 1.2, n


def test_insert_seed_summary_goes_last_in_key_decisions():
    out = H.insert_seed_summary(SUMMARY, "SEED")
    kd = out.index("## Key Decisions")
    ns = out.index("## Next Steps")
    assert kd < out.index("- SEED") < ns
    assert out.index("brute force") < out.index("- SEED")
    assert out.replace("\n- SEED", "").replace("\n\n\n", "\n\n") .count("## ") == 4


def test_insert_seed_summary_without_section_appends_one():
    out = H.insert_seed_summary("## Goal\nx\n", "SEED")
    assert out.endswith("## Key Decisions\n- SEED\n")


def test_insert_seed_notes_and_message():
    assert H.insert_seed_notes(None, "S") == "- S\n"
    assert H.insert_seed_notes("# Notes\n- a\n", "S") == "# Notes\n- a\n- S\n"
    m = H.insert_seed_message({"role": "assistant", "content": "thinking"}, "S")
    assert m["content"] == "thinking\n\nS"
    m2 = H.insert_seed_message({"role": "assistant", "content": [
        {"type": "reasoning", "reasoning": "r"}, {"type": "text", "text": "t"}]}, "S")
    assert m2["content"][-1] == {"type": "text", "text": "S"}
    with pytest.raises(ValueError):
        H.insert_seed_message({"role": "tool", "content": "x"}, "S")


def test_lastk_keeps_last_two_turns_and_seeds_last_assistant():
    dumps = turn(1) + turn(2) + turn(3)
    ctx, kept = H.lastk_context(SYSTEM, TASK, dumps, "SEED")
    assert [m["role"] for m in kept] == ["assistant", "tool", "assistant", "tool"]
    assert kept[0]["tool_calls"][0]["id"] == "c2"
    assert kept[2]["content"].endswith("SEED")
    assert ctx[:2] == [SYSTEM, TASK] and ctx[2]["content"] == C.LASTK_HEADER
    assert dumps[4]["content"] == ""                   # the input is not modified


def test_lastk_with_nudge_turn():
    nudge = [{"role": "assistant", "content": "hmm"}, {"role": "user", "content": "nudge"}]
    dumps = turn(1) + nudge + turn(2)
    _, kept = H.lastk_context(SYSTEM, TASK, dumps)
    assert kept[0]["content"] == "hmm" and len(kept) == 4


def test_summary_request_first_and_update():
    sys_text, first = H.summary_request(turn(1, "hello"))
    assert "[Assistant]: hello" in first and "<previous-summary>" not in first
    assert "[Tool result]: out1" in first and sys_text
    _, upd = H.summary_request(turn(2), previous=SUMMARY)
    assert "<previous-summary>\n" + SUMMARY in upd
    assert H.UPDATE_HEAD in upd and "PRESERVE all existing information" in upd


def test_serialize_truncates_tool_results():
    long = [{"role": "tool", "content": "x" * 5000, "tool_call_id": "c",
             "function": "execute_command"}]
    s = H.serialize(long)
    assert "more characters truncated" in s and len(s) < 2200


def test_contexts():
    s = H.summary_context(SYSTEM, TASK, "SUM", pin_task=False)
    assert s[0] == SYSTEM and len(s) == 2 and "SUM" in s[1]["content"]
    assert "compacted into the following summary" in s[1]["content"]
    assert H.summary_context(SYSTEM, TASK, "SUM", pin_task=True)[1] == TASK
    n = H.notes_context(SYSTEM, TASK, "  ")
    assert n[2]["content"].endswith(C.NOTES_MISSING)
    m = H.memfile_context(SYSTEM, TASK, "mem")
    assert m[2]["content"] == C.MEMFILE_HEADER + "mem"
    assert H.memfile_system("S").startswith("S\n\n")


def test_generation_of():
    hs = C.HANDOFF_TURNS
    assert [C.generation_of(t, hs) for t in (1, 4, 5, 8, 9, 21, 24)] == [0, 0, 1, 1, 2, 5, 5]
    assert C.generation_of(9, (4,)) == 1 and C.generation_of(9, ()) == 0


def _prefix() -> dict:
    msgs = [SYSTEM, TASK] + turn(1) + turn(2) + turn(3) + turn(4)
    return {"messages": msgs, "archive_w4": "W4", "archive_w4n": "W4N",
            "sha_w4": {"a": "1"}, "sha_w4n": {"a": "2"},
            "summary_s1": {"status": "ok", "reply": SUMMARY},
            "summary_s1n": {"status": "ok", "reply": SUMMARY.replace("brute", "fast")},
            "notes_text": "# Notes\n- tried brute force\n"}


@pytest.mark.parametrize("arm", ["R", "C", "CP", "CPw", "N", "L", "C1", "N1"])
def test_chain_spec_every_arm(arm):
    import run as R

    from x3.tasks import load_manifest

    task = load_manifest().tasks[0]
    sp = R.chain_spec(C.DEEPSEEK, _prefix(), arm, "none", task, 0, "B")
    a = C.ARMS[arm]
    assert sp["archive_b64"] == ("W4N" if a.notes_turns else "W4")
    ctx = sp["init_messages"]
    assert ctx[0] == SYSTEM
    assert (TASK in ctx) == (a.pin_task or a.kind == "full")
    if arm == "CPw":
        assert sp["post_write"] == {C.NOTES_PATH: None} and "fast" in sp["summary_prev"]
    if arm in ("C", "CP", "C1"):
        assert "brute" in sp["summary_prev"]
    assert sp["gen1"]["kind"] == a.kind
    from p44 import agent
    spec = agent.Spec(**sp)                       # every field present and named right
    assert (spec.stage, spec.arm, spec.seed, spec.prefix_id) == ("chain", arm, "none",
                                                                 C.prefix_id(task.task_id, 0))


@pytest.mark.parametrize("arm", ["C", "N", "L"])
@pytest.mark.parametrize("seed", ["P", "D", "U"])
def test_chain_spec_seeds_are_verbatim(arm, seed):
    import run as R

    from x3.tasks import load_manifest

    task = load_manifest().tasks[0]
    sp = R.chain_spec(C.DEEPSEEK, _prefix(), arm, seed, task, 0, "B")
    assert C.SEED_TEXT[seed] in json.dumps(sp["init_messages"])
    assert sp["gen1"]["seed_text"] == C.SEED_TEXT[seed]
    if arm == "N":
        assert C.SEED_TEXT[seed] in sp["post_write"][C.NOTES_PATH]
    if arm == "C":
        assert C.SEED_TEXT[seed] in sp["summary_prev"]


def test_r_takes_no_seed():
    import run as R

    from x3.tasks import load_manifest

    with pytest.raises(ValueError):
        R.chain_spec(C.DEEPSEEK, _prefix(), "R", "P", load_manifest().tasks[0], 0, "B")


def test_tag_and_assemble_chain():
    from p44 import agent

    prefix = {"snapshots": [{"step": 0, "files": {}}, {"step": 1, "files": {}}],
              "calls": [{"turn": 2, "snapshot_step": 1}],
              "notes_snapshots": [{"step": 2, "files": {}}, {"step": 3, "files": {}}],
              "notes_calls": [{"turn": 4, "snapshot_step": 2}],
              "solution_w4": "s4", "solution_w4n": "s4n", "n_notes_turns": 1}
    rec = {"handoff_turns": [4, 8], "n_turns": 9, "n_notes_turns": 1,
           "generations": [{"gen": 1, "start_turn": 4}, {"gen": 2, "start_turn": 8}],
           "snapshots": [{"step": 0, "files": {}}, {"step": 1, "files": {}}],
           "calls": [{"turn": 6, "snapshot_step": 1}]}
    meta = {"arm": "N", "episode_id": "x"}
    art = agent.assemble_chain(rec, meta, prefix)
    assert [s["step"] for s in art["snapshots"]] == list(range(6))
    assert [g["gen"] for g in art["generations"]] == [0, 1, 2]
    assert art["generations"][0]["solution_at_end"] == "s4n"
    assert art["calls"][-1]["snapshot_step"] == 5 and art["calls"][-1]["gen"] == 1
    assert art["calls"][1]["notes"] is True and art["calls"][1]["gen"] == 0
    assert art["n_notes_turns"] == 2
    # an arm without notes turns skips the prefix's notes snapshots
    art_c = agent.assemble_chain(rec, {"arm": "C", "episode_id": "y"}, prefix)
    assert len(art_c["snapshots"]) == 4
    assert art_c["generations"][0]["solution_at_end"] == "s4"
