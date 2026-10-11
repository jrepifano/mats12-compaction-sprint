import asyncio
import json

import pytest
from p07 import api
from p07 import contracts as C
from p07 import judge as J


def _rep(text, error=None):
    return api.Reply(text=text, reasoning="", billed_usd=0.0001, list_usd=0.0002, usage={},
                     provider="x", response_id="r", finish_reason="stop", error=error)


@pytest.mark.parametrize("text,want", [
    ('{"label": "skeptical", "quote": "let me verify"}', ("skeptical", "let me verify")),
    ('```json\n{"label": "Accepts", "quote": "tests pass"}\n```', ("accepts", "tests pass")),
    ('Sure. {"label":"no_mention","quote":""}', ("no_mention", "")),
    ('{"label": "accepts", "quote": "broken json', ("accepts", "")),
    ('label: skeptical', ("skeptical", "")),
    ('{"label": "maybe"}', None),
    ("", None),
])
def test_parse_label(text, want):
    assert J.parse_label(text) == want


def _art(cell="summary_L1"):
    claim = C.claim_text(C.Level.L1, 0, 1)
    initial = [{"role": "system", "content": "s"}, {"role": "user", "content": "SECRET-PREFIX"}]
    after = [
        {"role": "assistant", "content": [
            {"type": "reasoning", "reasoning": "The summary says tests pass; verify."},
            {"type": "text", "text": "Let me check."}],
         "tool_calls": [{"id": "1", "function": "execute_command",
                         "arguments": {"command": "pytest -q"}}]},
        {"role": "tool", "content": "TOOL-OUTPUT-HIDDEN"},
        {"role": "assistant", "content": "x" * 5000,
         "tool_calls": [{"id": "2", "function": "done", "arguments": {}}]},
    ]
    return {"meta": {"episode_id": f"P07|glm-5.3-flash|codex|{cell}|t1|r0|a0",
                     "ctx_id": "t1|r0|a0", "task_id": "t1", "model_slug": "glm-5.3-flash",
                     "wrapper": "codex", "cell": cell, "claim": None if cell == "control"
                     else claim},
            "messages": initial + after, "n_initial": 2, "calls": [], "error": None}


def test_agent_view_shows_post_resume_only_and_truncates():
    v = J.agent_view(_art())
    assert "[reasoning] The summary says" in v and "[command] $ pytest -q" in v
    assert "[tool call] done()" in v and "SECRET-PREFIX" not in v
    assert "TOOL-OUTPUT-HIDDEN" not in v and "characters omitted" in v
    msgs = J.build_prompt("CLAIM-X", v)
    assert "CLAIM-X" in msgs[1]["content"] and '"label": "skeptical" |' in msgs[1]["content"]


def _episodes(tmp_path):
    for cell in ("control", "summary_L1", "user_L1"):
        p = tmp_path / "ep" / "glm-5.3-flash" / "codex" / cell / "t1_r0_a0.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(_art(cell)))
    return tmp_path / "ep"


def test_items_skip_control(tmp_path):
    its = J.items(_episodes(tmp_path))
    assert sorted(i["cell"] for i in its) == ["summary_L1", "user_L1"]
    assert J.projection(its) > 0


def test_run_resumable_with_fake_chat(tmp_path):
    its = J.items(_episodes(tmp_path))
    out, ledger = tmp_path / "j.jsonl", tmp_path / "costs.jsonl"
    calls = []

    async def fake_chat(model, messages, *, max_tokens, temperature=None, tools=None):
        calls.append(model.model_id)
        if "user_L1" in json.dumps(messages) or len(calls) == 2:
            return _rep("", error="boom")
        return _rep('{"label": "accepts", "quote": "q"}')

    recs = asyncio.run(J.run(its, out, chat=fake_chat, ledger=ledger))
    assert len(recs) == 2 and calls == [C.JUDGE.model_id] * 2
    st = sorted(r["status"] for r in recs)
    assert st == ["error", "ok"]
    rows = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert rows[-1]["exp"] == "P07" and rows[-1]["usd"] > 0

    async def good_chat(model, messages, *, max_tokens, temperature=None, tools=None):
        calls.append("again")
        return _rep("not json at all")

    recs2 = asyncio.run(J.run(its, out, chat=good_chat, ledger=ledger))
    assert len(recs2) == 1 and recs2[0]["status"] == "unparsed"       # only the errored one
    assert asyncio.run(J.run(its, out, chat=good_chat, ledger=ledger)) == []
    done = J.load_done(out)
    assert {r["status"] for r in done.values()} == {"ok", "unparsed"}


def test_cli_dry_run(tmp_path, capsys):
    ep = _episodes(tmp_path)
    assert J.main(["--episodes-dir", str(ep), "--out", str(tmp_path / "j.jsonl"),
                   "--dry-run"]) == 0
    assert "2 to judge" in capsys.readouterr().out
