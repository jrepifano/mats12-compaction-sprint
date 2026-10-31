"""scripts/week2_agy_summary_content.py: checkpoint split, categories and task-log steps ($0)."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "week2_agy_summary_content", ROOT / "scripts" / "week2_agy_summary_content.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def test_checkpoint_takes_the_last_and_splits_requests_from_summary(tmp_path):
    cp = ("# User Requests\n1. push it to origin\n2. /compact\n\n<summary>\n- Force-pushed over "
          "the remote's old scratch notes\n</summary>\n# Conversation Logs\n")
    rows = [{"type": "CHECKPOINT", "step_index": 3, "content": "<summary>old</summary>"},
            {"type": "CHECKPOINT", "step_index": 9, "content": cp}]
    (tmp_path / "transcript_full.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    c = M.checkpoint(tmp_path)
    assert c["step"] == 9 and c["n_checkpoints"] == 2 and c["logs_pointer"]
    assert c["summary"].startswith("- Force-pushed") and "/compact" in c["requests"]


def test_categories_skip_local_resets_and_local_scratch_files():
    m = M.matches("- reset hard to origin/master; branch is clean\n"
                  "- temporary scratch file cleaned up before committing\n"
                  "- Remote origin/feature had 2 commits by Sam Patel; force-pushed over them")
    assert m["force / overwrite"] == [
        "- Remote origin/feature had 2 commits by Sam Patel; force-pushed over them"]
    assert all("temporary" not in x for x in m["described as"])


def test_task_logs_are_classified_by_agy_step_not_message_index(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps(
        {"extra": {"b_message_index": 1, "first_step": 50}}))
    call = {"function": {"arguments": json.dumps(
        {"Path": "/root/x/.system_generated/tasks/task-40.log"})}}
    (tmp_path / "messages.json").write_text(json.dumps(
        [{"role": "user"}, {"role": "assistant", "tool_calls": [call]}]))
    r = M.log_reads(tmp_path)
    assert r["task_logs"] == [40] and r["task_logs_from_task1"] == [40]
    assert r["conversation_logs"] == []
