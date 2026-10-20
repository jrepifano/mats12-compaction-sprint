"""Tests for compaction.s1_replay (U4). No model calls: the backend, the renderer and the
native runner are fakes; prompts that live in gitignored files are small stand-ins."""

from __future__ import annotations

import itertools
import json
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest

from compaction import s1_replay as R
from compaction.s1_types import Resp

# ------------------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------------------


@dataclass
class FakeRendered:
    text: str
    spans: dict
    gaps: list
    bytes: int


def fake_render(rec: dict) -> FakeRendered:
    parts, spans, pos = [], {}, 0
    for i, m in enumerate(rec.get("source_messages") or []):
        block = f"[m{i}] {m['role'].upper()}: {m.get('text') or ''}"
        for c in m.get("tool_calls") or []:
            block += f"\n<tool_call {c['name']}>{json.dumps(c.get('args'))}"
        if parts:
            pos += 2
        spans[i] = (pos, pos + len(block))
        pos += len(block)
        parts.append(block)
    text = "\n\n".join(parts)
    return FakeRendered(text, spans, [], len(text.encode()))


CC_STANDIN = {
    "kind": "claude_code",
    "messages": [
        {"role": "system", "content": "x-anthropic-billing-header: cc_version=9.9.9.1;"},
        {"role": "system", "content": "STANDIN SYSTEM A"},
        {"role": "system", "content": "STANDIN SYSTEM B"},
        {"role": "user", "content": [{"type": "text", "text": "a user turn"}]},
        {"role": "system", "content": "# Environment standin"},
        {"role": "user", "content": [{"type": "text", "text": "STANDIN INSTRUCTION."}]},
    ],
}

OPENCODE_TS = (
    "const SUMMARY_TEMPLATE = `T line 1\n## T line 2`\n"
    "const SUMMARY_UPDATE_INSTRUCTIONS = `x`\n"
    "export const buildPrompt = (input: { readonly previousSummary?: string }) => {\n"
    '  const conversation = `Open:\\n\\n<conversation>\\n${input.context.join("\\n\\n")}'
    "\\n</conversation>`\n"
    "  if (!input.previousSummary)\n"
    "    return [\n"
    "      conversation,\n"
    '      "Make a standin summary.",\n'
    "      SUMMARY_TEMPLATE,\n"
    '    ].join("\\n\\n")\n'
    "}\n"
)

AGY_STANDIN = (
    b"\x00junk"
    + R.AGY_PROMPT_START
    + b" STANDIN BODY. "
    + R.AGY_PROMPT_END
    + b"\x00junk# Resuming from a compaction\x00junk"
    + b"You are continuing work on the task described above, standin below:\x00junk"
    + b"# User Requests\nThe following are standin requests in order:\x00junk"
    + b"# User Requests\nThe following are the most recent standin requests in order:\x00"
)


@pytest.fixture
def standins(tmp_path, monkeypatch):
    cc = tmp_path / "cc.json"
    cc.write_text(json.dumps(CC_STANDIN))
    oc = tmp_path / "opencode"
    (oc / "packages/opencode/src/agent/prompt").mkdir(parents=True)
    (oc / "packages/core/src/session").mkdir(parents=True)
    (oc / "packages/opencode/src/agent/prompt/compaction.txt").write_text("STANDIN OC SYSTEM\n")
    (oc / "packages/core/src/session/compaction.ts").write_text(OPENCODE_TS)
    (oc / "packages/opencode/package.json").write_text('{"version": "0.0.0-test"}')
    agy = tmp_path / "agy.bin"
    agy.write_bytes(AGY_STANDIN)
    codex = tmp_path / "codex.json"
    codex.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "STANDIN CODEX BASE"},
                    {"role": "system", "content": "session-specific developer message"},
                    {"role": "user", "content": "x"},
                ]
            }
        )
    )
    monkeypatch.setattr(R, "CODEX_CAPTURE", codex)
    monkeypatch.setattr(R, "CC_CAPTURE", cc)
    monkeypatch.setattr(R, "OPENCODE_SRC", oc)
    monkeypatch.setattr(R, "AGY_BINARY", str(agy))
    monkeypatch.setattr(R, "_RENDER", fake_render)
    R.clear_prompt_cache()
    yield tmp_path
    R.clear_prompt_cache()


def U(t, **kw):
    return {"role": "user", "text": t, **kw}


def A(t="", calls=None):
    m = {"role": "assistant", "text": t}
    if calls:
        m["tool_calls"] = [{"name": n, "args": a, "id": i} for n, a, i in calls]
    return m


def T(tid, t):
    return {"role": "tool", "text": t, "tool_use_id": tid}


def S(t):
    return {"role": "system", "text": t}


def rec_of(msgs, rid="jacob:s1:0", **kw):
    return {"id": rid, "source": "jacob", "license": "private", "source_messages": msgs, **kw}


def history():
    return [
        U("Never edit vendor/. Fix the tests."),
        S("<system-reminder>ctx</system-reminder>"),
        A("Running.", [("Bash", {"command": "pytest"}, "t1")]),
        T("t1", "1 failed"),
        A("One failure."),
        U("OK, fix it."),
        A("", [("Edit", {"file": "a.py"}, "t2")]),
        T("t2", "edited"),
        A("Done."),
    ]


# ------------------------------------------------------------------------------------------
# Prompt loading and builders
# ------------------------------------------------------------------------------------------


def test_cc_prompt_shape_and_sha(standins):
    q = R.ARM_BUILDERS["cc_prompt"](rec_of(history()))
    assert q.system == "STANDIN SYSTEM A\n\nSTANDIN SYSTEM B"  # billing header and env excluded
    assert q.user.startswith("<conversation>\n[m0] USER: Never edit vendor/.")
    assert q.user.endswith("\n</conversation>\n\nSTANDIN INSTRUCTION.")
    assert q.kept_messages == []
    ps = R.prompts_for("cc_prompt")
    assert q.prompt_sha256 == ps.sha256 and len(q.prompt_sha256) == 64
    assert ps.sources[0]["version"] == "claude-code 9.9.9.1"
    assert q.max_output_tokens == 20_000 and len(q.calls) == 1


def test_cc_format_summary_strips_analysis():
    raw = "<analysis>thinking\n\n\nmore</analysis>\n\n<summary>\n1. Goal\n\n\n2. Rule\n</summary>"
    assert R.cc_format_summary(raw) == "Summary:\n1. Goal\n\n2. Rule"
    assert R.cc_format_summary("no tags") == "no tags"


def test_codex_budget_boundary_exact_fit(standins):
    msgs = [U("o" * 4_000), U("m" * 40_000), A("x"), U("n" * 40_000)]
    kept, info = R.codex_kept_user_messages(msgs)
    # newest 10,000 + middle 10,000 = the 20,000 budget exactly; the oldest is not reached
    assert info["verbatim"] == [1, 3] and info["truncated"] == [] and info["budget_left"] == 0
    assert [m["text"][0] for m in kept] == ["m", "n"]


def test_codex_budget_crossing_message_is_middle_truncated(standins):
    msgs = [U("a" * 400), U("m" * 40_004), U("n" * 40_000)]
    kept, info = R.codex_kept_user_messages(msgs)
    assert info["verbatim"] == [2] and info["truncated"] == [1]
    t = kept[0]["text"]
    assert "tokens truncated" in t and t.startswith("m") and t.endswith("m")
    marker = t[t.index("…") : t.rindex("…") + 1]
    assert len(t.encode()) == 40_000 + len(marker.encode())  # the remaining 10,000 tokens


def test_codex_skips_harness_user_messages(standins):
    msgs = [U("real rule"), U("<task-notification>\nx"), U("<command-name>/model</command-name>")]
    kept, info = R.codex_kept_user_messages(msgs)
    assert [m["text"] for m in kept] == ["real rule"] and info["user_messages"] == 1


def test_codex_builder_truncates_tool_outputs(standins):
    msgs = [U("go"), A("", [("Bash", {}, "t1")]), T("t1", "z" * 60_000)]
    q = R.ARM_BUILDERS["codex_local"](rec_of(msgs))
    assert q.info["tool_outputs_truncated"] == 1
    assert "tokens truncated" in q.user and "z" * 48_001 not in q.user
    assert q.system == "STANDIN CODEX BASE"  # Codex's base instructions, first system message
    assert q.user.endswith(R.prompts_for("codex_local").texts["codex_prompt.md"])
    src = {x["name"]: x for x in R.prompts_for("codex_local").sources}
    assert src["codex_system"]["sha256"] == R._sha("STANDIN CODEX BASE")
    assert q.kept_messages == [msgs[0]]


def test_codex_overflow_drops_oldest_with_its_results(standins, monkeypatch):
    monkeypatch.setattr(R, "CONTEXT_TOKENS", 20_000)
    big = "q" * 20_000
    msgs = [A("", [("Bash", {}, "t1")]), T("t1", big), U("u" * 4), A("tail")]
    q = R.build_codex_local(rec_of(msgs))
    assert q.info["overflow_dropped_oldest"] >= 1
    assert "[m0]" not in q.user and "[m1]" not in q.user and "[m3] ASSISTANT: tail" in q.user


def test_pi_tail_and_split(standins):
    msgs = [U("first task"), A("a" * 10), U("second " + "s" * 40_000), A("b" * 90_000), A("c" * 10)]
    q = R.ARM_BUILDERS["pi"](rec_of(msgs))
    # walk back: c (3) + b (22,500) >= 20,000 at index 3 -> cut at assistant 3, split turn 2
    assert q.info["first_kept"] == 3 and q.info["split"]
    assert q.kept_messages == msgs[3:]
    assert [c.label for c in q.calls] == ["history", "turn_prefix"]
    hist, prefix = q.calls
    assert "[User]: first task" in hist.user and "second" not in hist.user
    assert hist.user.startswith("<conversation>\n") and hist.max_output_tokens == 13_107
    assert prefix.user.startswith("# Conversation\n[User]: second")
    assert prefix.max_output_tokens == 8_192
    merged = R.finish_summary("pi", q, {"history": "H", "turn_prefix": "P"})
    assert merged == "H\n\n---\n\n**Turn Context (split turn):**\n\nP"


def test_pi_tool_result_cut():
    assert R.pi_truncate_result("x" * 2_000) == "x" * 2_000
    assert R.pi_truncate_result("x" * 2_005) == "x" * 2_000 + (
        "\n\n[... 5 more characters truncated]"
    )


def test_opencode_tool_result_cut_and_serialization(standins):
    msgs = [U("go"), A("ok", [("Bash", {"c": "ls"}, "t1")]), T("t1", "r" * 2_500)]
    unit = R.opencode_units(msgs)[1]
    s = R.opencode_serialize_unit(unit, msgs)
    assert s == (
        '[Assistant]: ok\n[Assistant tool call]: Bash({"c":"ls"})\n'
        "[Tool result]: " + "r" * 2_000 + "\n[truncated]"
    )


def test_opencode_prompt_assembly(standins):
    q = R.ARM_BUILDERS["opencode"](rec_of(history()))
    assert q.system == "STANDIN OC SYSTEM\n"
    assert q.user.startswith("Open:\n\n<conversation>\n[User]: Never edit vendor/.")
    assert q.user.endswith("</conversation>\n\nMake a standin summary.\n\nT line 1\n## T line 2")
    assert R.prompts_for("opencode").sources[0]["version"] == "opencode 0.0.0-test"


def test_opencode_tail_keeps_whole_recent_turns(standins):
    msgs = [U("old " + "o" * 200_000), U("recent"), A("y")]
    sel = R.opencode_select(msgs)
    assert sel["how"] == "turns" and sel["first_kept"] == 1  # the old turn cannot be split
    q = R.build_opencode(rec_of(msgs))
    assert q.kept_messages == msgs[1:] and "recent" not in q.user and "old" in q.user
    # an earlier turn that does not fit is split: its tail joins the kept messages
    msgs2 = [U("old " + "o" * 200_000), A("x"), U("recent"), A("y")]
    sel2 = R.opencode_select(msgs2)
    assert sel2["how"] == "split_turn" and sel2["first_kept"] == 1


def test_opencode_tail_splits_a_big_last_turn(standins):
    msgs = [
        U("only turn"),
        A("a" * 100_000, [("Bash", {}, "t0")]),
        T("t0", "r0"),
        A("b", [("Bash", {}, "t")]),
        T("t", "r"),
        A("c"),
    ]
    units = R.opencode_units(msgs)
    assert [u["idx"] for u in units] == [[0], [1, 2], [3, 4], [5]]
    sel = R.opencode_select(msgs)
    # whole turn too big -> split at the earliest unit whose suffix fits (unit 2 = message 3)
    assert sel["how"] == "split_turn" and sel["first_kept"] == 3
    q = R.build_opencode(rec_of(msgs))
    assert q.kept_messages == msgs[3:]
    assert "[User]: only turn" in q.user  # the turn's user message is summarised, not kept


def test_opencode_no_tail_when_everything_fits(standins):
    msgs = [U("a"), A("b")]
    assert R.opencode_select(msgs)["first_kept"] is None
    assert R.build_opencode(rec_of(msgs)).kept_messages == []


def test_agy_ten_request_cap_and_checkpoint(standins):
    with pytest.warns(UserWarning, match="differs"):
        ps = R.prompts_for("antigravity")
    assert ps.texts["agy_prompt"].startswith("You have been working")
    assert ps.texts["agy_prompt"].endswith("<summary></summary> tags.")
    msgs = []
    for i in range(12):
        msgs += [U(f"req {i}"), A(f"ans {i}")]
    msgs.insert(5, U("<local-command-stdout>x</local-command-stdout>"))
    q = R.ARM_BUILDERS["antigravity"](rec_of(msgs))
    assert [m["text"] for m in q.kept_messages] == [f"req {i}" for i in range(2, 12)]
    assert q.user.endswith(ps.texts["agy_prompt"]) and q.system == R.NEUTRAL_SYSTEM
    cp = R.agy_checkpoint(["one", "two"], "S")
    assert cp == (
        "# Resuming from a compaction\n\nYou are continuing work on the task described "
        "above, standin below:\n\n# User Requests\nThe following are standin requests "
        "in order:\n1. one\n2. two\n\n<summary>\nS\n</summary>"
    )
    capped = R.agy_checkpoint(["x"], "S", capped=True)
    assert "the most recent standin requests in order:\n1. x\n" in capped
    assert R.agy_summary_text("pre <summary>\n body \n</summary> post") == "body"
    assert R.agy_summary_text("untagged ") == "untagged"


def test_agy_expected_sha_does_not_warn(standins, monkeypatch):
    body = R._between(AGY_STANDIN, R.AGY_PROMPT_START, R.AGY_PROMPT_END).decode()
    monkeypatch.setattr(R, "AGY_EXPECTED_SHA256", R._sha(body))
    R.clear_prompt_cache()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        R.prompts_for("antigravity")


def test_preserved_messages_join_the_history(standins):
    msgs = history()
    kept = [A("preserved reply"), U("preserved rule: never push")]
    r = rec_of(msgs, meta={"preserved_messages": kept})
    h = R.history_rec(r)
    assert h["source_messages"] == msgs + kept and h["s1_n_preserved"] == 2
    assert R.history_rec(h) is h and h["meta"]["preserved_messages"] == []
    q = R.build_codex_local(r)
    assert q.kept_messages[-1]["text"] == "preserved rule: never push"
    assert "[m10] USER: preserved rule" in q.user
    with pytest.warns(UserWarning):  # the stand-in binary's prompt is not agy 1.4.3's
        q2 = R.build_antigravity(r)
    assert q2.kept_messages[-1]["text"] == "preserved rule: never push"


# ------------------------------------------------------------------------------------------
# Driver with a fake backend
# ------------------------------------------------------------------------------------------


class FakeBackend:
    name, model, family = "fake", "claude-haiku-5-5", "anthropic"
    context_tokens, bytes_per_token = 1_000_000, 2.3

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def run(self, reqs, *, purpose):
        self.calls.append((purpose, reqs))
        out = {}
        for q in reqs:
            assert q.schema is None and q.private
            if q.key in self.fail:
                out[q.key] = Resp(q.key, None, None, "timeout")
            else:
                text = f"<analysis>a</analysis><summary>S for {q.key}</summary>"
                out[q.key] = Resp(
                    q.key, None, text, None, input_tokens=10, output_tokens=5, model=self.model
                )
        return out


def test_run_replays_writes_records_and_resumes(standins):
    recs = {
        "jacob:a:0": rec_of(history(), "jacob:a:0"),
        "jacob:b:0": rec_of(
            [U("x"), A("y"), U("z " + "z" * 90_000), A("w" * 90_000), A("v")], "jacob:b:0"
        ),
    }
    be = FakeBackend(fail={"jacob:b:0@cc_prompt"})
    with pytest.warns(UserWarning):
        counts = R.run_replays(
            list(recs),
            ["cc_prompt", "pi", "antigravity"],
            be,
            out_dir=standins / "rep",
            load=recs.__getitem__,
        )
    assert counts["cc_prompt"] == {"written": 2, "skipped": 0, "errors": 1, "restored": 0}
    rows = R.read_replays(standins / "rep" / "cc_prompt.jsonl")
    ok = rows["jacob:a:0@cc_prompt"]
    assert set(ok) >= {
        "id",
        "base_rid",
        "arm",
        "summary",
        "kept_messages",
        "model",
        "prompt_sha256",
        "request_sha256",
        "usage",
        "native",
    }
    assert ok["summary"] == "Summary:\nS for jacob:a:0@cc_prompt" and ok["error"] is None
    assert ok["model"] == "claude-haiku-5-5" and ok["usage"]["input_tokens"] == 10
    assert ok["n_preserved_in_history"] == 0 and ok["native"] is None
    assert rows["jacob:b:0@cc_prompt"]["error"] == "timeout"
    pi_b = R.read_replays(standins / "rep" / "pi.jsonl")["jacob:b:0@pi"]
    assert "**Turn Context (split turn):**" in pi_b["summary"] and pi_b["usage"]["calls"] == 2
    agy = R.read_replays(standins / "rep" / "antigravity.jsonl")["jacob:a:0@antigravity"]
    assert agy["summary"] == "S for jacob:a:0@antigravity"
    src = json.loads((standins / "rep" / "prompts.json").read_text())
    entry = src["by_fingerprint"][ok["fingerprint"]]
    assert entry["prompt_sha256"] == ok["prompt_sha256"] and entry["arm"] == "cc_prompt"
    assert {f["name"] for f in entry["files"]} == {"cc_system", "cc_instruction"}
    # resume: only the failed record is sent again
    be2 = FakeBackend()
    counts = R.run_replays(
        list(recs), ["cc_prompt"], be2, out_dir=standins / "rep", load=recs.__getitem__
    )
    assert counts["cc_prompt"]["written"] == 1
    assert [q.key for q in be2.calls[0][1]] == ["jacob:b:0@cc_prompt"]
    assert (
        R.read_replays(standins / "rep" / "cc_prompt.jsonl")["jacob:b:0@cc_prompt"]["error"] is None
    )


def test_request_sha_changes_with_content(standins):
    a = R.build_cc_prompt(rec_of(history()))
    b = R.build_cc_prompt(rec_of(history()[:-1]))
    assert a.request_sha256 != b.request_sha256 and a.prompt_sha256 == b.prompt_sha256


# ------------------------------------------------------------------------------------------
# Size prefixes
# ------------------------------------------------------------------------------------------


def test_size_prefixes_cut_at_message_boundaries(standins):
    msgs = [U("a" * 89), U("b" * 89), U("c" * 89), U("d" * 89)]  # blocks of 100 bytes
    rec = rec_of(msgs)
    out = R.size_prefixes(rec, cuts=(150, 302, 1_000), bytes_per_token=1.0)
    # 100, 202, 304, 406 cumulative bytes: 150 -> 1 message, 302 -> 2, 1,000 > whole -> skipped
    assert [p["s1_size"]["n_messages"] for p in out] == [1, 2]
    assert [p["s1_size"]["rendered_bytes"] for p in out] == [100, 202]
    assert out[0]["source_messages"] == msgs[:1] and out[0]["id"] == "jacob:s1:0#prefix0k"
    q = R.build_cc_prompt(out[1])
    assert q.user.endswith("STANDIN INSTRUCTION.\n\n" + R.SIZE_LENGTH_LINE)
    assert q.prompt_sha256 != R.prompts_for("cc_prompt").sha256
    row = R.assemble(out[1], "cc_prompt", q, [Resp("k", None, "<summary>s</summary>", None)])
    assert row["base_rid"] == "jacob:s1:0" and row["size"]["cut_tokens"] == 302


def test_size_prefixes_default_cuts(standins):
    msgs = [U("x" * 100_000) for _ in range(14)]  # ~1.4 MB rendered
    out = R.size_prefixes(rec_of(msgs))
    assert [p["s1_size"]["cut_tokens"] for p in out] == [256_000, 512_000]
    for p in out:
        n = p["s1_size"]["n_messages"]
        assert p["s1_size"]["est_tokens"] <= p["s1_size"]["cut_tokens"]
        # the longest prefix: one more message would pass the cut
        assert (n + 1) * 100_014 / 2.46 > p["s1_size"]["cut_tokens"]
    assert R.size_prefixes(rec_of(msgs), cuts=(10**9,)) == []


# ------------------------------------------------------------------------------------------
# Native: transcript writer, parser, run with a fake runner
# ------------------------------------------------------------------------------------------


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_transcript_writer_chain_and_pairing(tmp_path):
    msgs = [
        U("start"),
        A("two calls", [("Bash", {"c": 1}, "t1")]),
        A("", [("Read", {"p": "f"}, "t2")]),
        S("<system-reminder>between</system-reminder>"),  # interleaved: moved after results
        T("t1", "r1"),
        T("t2", "r2"),
        T("ghost", "orphan result"),
        A("", [("Bash", {}, "t3")]),  # never answered
        U(""),
    ]
    p = tmp_path / "s.jsonl"
    w = R.write_cc_transcript(msgs, p, session_id="sid", cwd="/x", model="m")
    lines = _lines(p)
    assert lines[0]["parentUuid"] is None
    for prev, cur in itertools.pairwise(lines):
        assert cur["parentUuid"] == prev["uuid"]
    assert len({x["uuid"] for x in lines}) == len(lines)
    kinds = [
        (
            x["type"],
            [b.get("type") for b in x["message"]["content"]]
            if isinstance(x["message"]["content"], list)
            else "text",
        )
        for x in lines
    ]
    assert kinds[:5] == [
        ("user", "text"),
        ("assistant", ["text", "tool_use"]),
        ("assistant", ["tool_use"]),
        ("user", ["tool_result"]),
        ("user", ["tool_result"]),
    ]
    assert lines[1]["message"]["id"] == lines[2]["message"]["id"]  # one API message
    # every tool_use is answered by the next user entries, before any other content
    for i, x in enumerate(lines):
        if x["type"] != "assistant":
            continue
        ids = [b["id"] for b in x["message"]["content"] if b["type"] == "tool_use"]
        j = i + 1
        while j < len(lines) and lines[j]["type"] == "assistant":
            ids += [b["id"] for b in lines[j]["message"]["content"] if b["type"] == "tool_use"]
            j += 1
        got = []
        while j < len(lines) and isinstance(lines[j]["message"]["content"], list):
            got += [b["tool_use_id"] for b in lines[j]["message"]["content"]]
            j += 1
        assert set(ids) <= set(got)
    rep = w["repairs"]
    assert rep["moved_results"] == 2 and rep["orphan_results_as_text"] == 1
    assert rep["synthetic_results"] == 1 and rep["empty_texts"] == 1
    assert w["uuid_to_index"][lines[1]["uuid"]] == 1
    assert sorted(i for i in w["uuid_to_index"].values() if i is not None) == list(range(9))


def _canned(sid: str, uuids: list[str], summary="1. Goal: x\n2. Rule: never edit vendor/."):
    """Stream-json events in the shape of the Phase 0 smoke (native.jsonl), synthetic text."""
    msg = (
        "This session is being continued from a previous conversation. Standin.\n\n"
        "<tools-used-before-summary>Bash</tools-used-before-summary>\nSummary:\n"
        + summary
        + "\n\nIf you need specific details from before compaction, read the transcript.\n"
        "Continue the conversation."
    )
    events = [
        {"type": "system", "subtype": "status", "status": "compacting", "session_id": sid},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
        {"type": "system", "subtype": "status", "status": None, "compact_result": "success"},
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-haiku-5-5",
            "apiKeySource": "none",
            "claude_code_version": "2.1.305",
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "b1",
            "compact_metadata": {
                "trigger": "manual",
                "pre_tokens": 150,
                "post_tokens": 900,
                "preserved_messages": {"anchor_uuid": "s1", "uuids": uuids, "all_uuids": uuids},
            },
        },
        {"type": "user", "message": {"role": "user", "content": msg}, "isSynthetic": True},
        {
            "type": "result",
            "is_error": False,
            "total_cost_usd": 0.001,
            "modelUsage": {"claude-haiku-5-5": {"inputTokens": 2000}},
        },
        {"type": "_process", "returncode": 0},
    ]
    file_tail = [
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "b1",
            "compactMetadata": {
                "trigger": "manual",
                "preTokens": 150,
                "preservedMessages": {"uuids": uuids},
            },
        },
        {
            "type": "user",
            "isCompactSummary": True,
            "uuid": "s1",
            "message": {"role": "user", "content": msg},
        },
        {
            "type": "cost-state",
            "modelUsage": {
                "claude-haiku-5-5": {
                    "inputTokens": 2000,
                    "outputTokens": 900,
                    "cacheReadInputTokens": 100,
                    "cacheCreationInputTokens": 50,
                    "thinkingTokens": 30,
                }
            },
        },
    ]
    return events, file_tail


def test_parse_native_canned_events():
    events, tail = _canned("sid", ["u-last", "synthetic"])
    p = R.parse_native(events, tail, {"u-first": 0, "u-last": 5}, "claude-haiku-5-5")
    assert p["error"] is None
    assert p["summary"] == "Summary:\n1. Goal: x\n2. Rule: never edit vendor/."
    assert p["preserved_indices"] == [5] and p["preserved_unmapped"] == ["synthetic"]
    assert p["model"] == "claude-haiku-5-5" and p["boundary"]["pre_tokens"] == 150
    assert p["api_key_source"] == "none" and p["version"] == "2.1.305"


def test_parse_native_errors():
    events, tail = _canned("sid", [])
    tail[-1]["modelUsage"]["claude-opus-5-5"] = {}
    assert R.parse_native(events, tail, {}, "claude-haiku-5-5")["error"] == "model_mismatch"
    no_b = [e for e in events if e.get("subtype") != "compact_boundary"]
    tail_nb = [x for x in tail if x.get("subtype") != "compact_boundary"]
    assert R.parse_native(no_b, tail_nb, {}, "claude-haiku-5-5")["error"].startswith("no_boundary")
    failed = [{**e, "compact_result": "failed"} if "compact_result" in e else e for e in events]
    assert R.parse_native(failed, tail, {}, "claude-haiku-5-5")["error"] == "compact_failed:failed"


@pytest.fixture
def native_env(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(R, "claude_binary_sha256", lambda: "b" * 64)
    monkeypatch.setattr(R, "_claude_version", lambda: "2.1.305")
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    return cfg, tmp_root


def _project_dirs(cfg: Path) -> list[Path]:
    d = cfg / "projects"
    return list(d.iterdir()) if d.exists() else []


def test_native_compact_success_and_cleanup(tmp_path, native_env):
    cfg, tmp_root = native_env
    msgs = history()
    seen = {}

    def runner(cwd, args, stdin, timeout_s, env=None):
        seen.update(cwd=cwd, args=args, stdin=stdin, env=env)
        proj = _project_dirs(cfg)
        assert len(proj) == 1
        sf = next(proj[0].glob("*.jsonl"))
        lines = _lines(sf)
        assert lines[0]["cwd"] == cwd and args[3] == sf.stem
        last = lines[-1]["uuid"]  # Claude Code keeps the last message verbatim
        events, tail = _canned(sf.stem, [last])
        with open(sf, "a") as f:
            f.writelines(json.dumps(x) + "\n" for x in tail)
        return events

    rec = rec_of(msgs, meta={"preserved_messages": [U("kept rule")]})
    row = R.native_compact(
        rec, runner=runner, env={"K": "v"}, out_dir=tmp_path / "rep", tmp_root=str(tmp_root)
    )
    assert row["error"] is None and row["arm"] == "cc_native"
    assert row["id"] == "jacob:s1:0@cc_native" and row["model"] == "claude-haiku-5-5"
    assert row["summary"].startswith("Summary:\n1. Goal")
    assert row["kept_messages"] == [U("kept rule")]  # the last history message (preserved)
    assert row["n_preserved_in_history"] == 1
    assert row["native"]["pre_tokens"] == 150 and row["native"]["preserved_indices"] == [9]
    assert row["usage"]["output_tokens"] == 900 and row["usage"]["usd"] == 0.0
    assert row["prompt_sha256"] == "b" * 64 and len(row["request_sha256"]) == 64
    assert seen["stdin"] is None and seen["env"] == {"K": "v"}
    assert seen["args"][:2] == ["-p", "/compact"] and "--safe-mode" in seen["args"]
    assert seen["args"][seen["args"].index("--tools") + 1] == ""
    kept = tmp_path / "rep" / "native" / "jacob:s1:0.jsonl"
    assert kept.exists() and any(x.get("type") == "cost-state" for x in _lines(kept))
    assert _project_dirs(cfg) == [] and list(tmp_root.iterdir()) == []


class SessionTimeout(Exception):
    def __init__(self, events):
        super().__init__("timeout")
        self.events = events


@pytest.mark.parametrize(
    "exc,want",
    [(SessionTimeout([{"type": "_process"}]), "timeout"), (OSError("no claude"), "runner:OSError")],
)
def test_native_compact_failure_returns_error_and_cleans_up(tmp_path, native_env, exc, want):
    cfg, tmp_root = native_env

    def runner(cwd, args, stdin, timeout_s, env=None):
        raise exc

    row = R.native_compact(
        rec_of(history()), runner=runner, env={}, out_dir=tmp_path / "rep", tmp_root=str(tmp_root)
    )
    assert row["error"].startswith(want) and row["summary"] == ""
    assert _project_dirs(cfg) == [] and list(tmp_root.iterdir()) == []


def test_native_compact_no_boundary_is_an_error(tmp_path, native_env):
    cfg, tmp_root = native_env
    row = R.native_compact(
        rec_of(history()),
        runner=lambda c, a, s, t, env=None: [{"type": "_process", "returncode": 1}],
        env={},
        out_dir=tmp_path / "rep",
        tmp_root=str(tmp_root),
    )
    assert row["error"] == "process_failed(rc=1, is_error=False):no_boundary"
    assert _project_dirs(cfg) == [] and row["native"]["exit_status"]["ok"] is False


def test_native_compact_api_billing_raises_after_cleanup(tmp_path, native_env):
    cfg, tmp_root = native_env

    def runner(cwd, args, stdin, timeout_s, env=None):
        return [{"type": "system", "subtype": "init", "apiKeySource": "ANTHROPIC_API_KEY"}]

    with pytest.raises(R.NativeBillingError):
        R.native_compact(
            rec_of(history()),
            runner=runner,
            env={},
            out_dir=tmp_path / "rep",
            tmp_root=str(tmp_root),
        )
    assert _project_dirs(cfg) == [] and list(tmp_root.iterdir()) == []


def test_native_env_keeps_compaction_on(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    env = R.native_env()
    assert "DISABLE_COMPACT" not in env and "ANTHROPIC_API_KEY" not in env


def test_project_slug():
    assert R.project_slug("/private/var/x_y.z") == "-private-var-x-y-z"


def test_history_uses_s1_data_history_messages_when_present(standins, monkeypatch):
    from compaction import s1_data

    extra = [U("from history_messages")]
    monkeypatch.setattr(
        s1_data, "history_messages", lambda rec: rec["source_messages"] + extra, raising=False
    )
    r = rec_of(history(), meta={"preserved_messages": [U("ignored: U1 decides")]})
    h = R.history_rec(r)
    assert h["source_messages"][-1]["text"] == "from history_messages"
    assert h["s1_n_preserved"] == 1 and h["meta"]["preserved_messages"] == []


# ------------------------------------------------------------------------------------------
# Review r1 fixes: fingerprints, provenance, model enforcement, exit status, torn tails,
# cleanup, native pool
# ------------------------------------------------------------------------------------------


def _recs():
    return {"jacob:a:0": rec_of(history(), "jacob:a:0")}


def test_fingerprint_resume_reruns_on_changed_input_or_policy(standins, monkeypatch):
    recs = _recs()
    out = standins / "rep"
    be = FakeBackend()
    R.run_replays(list(recs), ["cc_prompt"], be, out_dir=out, load=recs.__getitem__)
    first = R.read_replays(out / "cc_prompt.jsonl")["jacob:a:0@cc_prompt"]["fingerprint"]
    # unchanged: skipped
    be = FakeBackend()
    c = R.run_replays(list(recs), ["cc_prompt"], be, out_dir=out, load=recs.__getitem__)
    assert c["cc_prompt"]["skipped"] == 1 and be.calls == []
    # changed input: rerun
    recs["jacob:a:0"]["source_messages"] = history() + [U("one more rule")]
    be = FakeBackend()
    c = R.run_replays(list(recs), ["cc_prompt"], be, out_dir=out, load=recs.__getitem__)
    assert c["cc_prompt"]["written"] == 1 and len(be.calls) == 1
    second = R.read_replays(out / "cc_prompt.jsonl")["jacob:a:0@cc_prompt"]["fingerprint"]
    assert second != first
    # changed policy: rerun
    monkeypatch.setattr(R, "POLICY_REVISION", R.POLICY_REVISION + 1)
    be = FakeBackend()
    c = R.run_replays(list(recs), ["cc_prompt"], be, out_dir=out, load=recs.__getitem__)
    assert c["cc_prompt"]["written"] == 1
    third = R.read_replays(out / "cc_prompt.jsonl")["jacob:a:0@cc_prompt"]
    assert third["fingerprint"] not in (first, second) and "/r" in third["policy_version"]
    prov = json.loads((out / "prompts.json").read_text())["by_fingerprint"]
    assert {first, second, third["fingerprint"]} <= set(prov)  # appended, never overwritten


def test_fingerprint_changes_with_generation_settings(standins):
    r = rec_of(history())
    q = R.build_cc_prompt(r)
    assert R.fingerprint(r, "cc_prompt", q, {"effort": None}) != R.fingerprint(
        r, "cc_prompt", q, {"effort": "low"}
    )
    assert R.fingerprint(r, "cc_prompt", q) != R.fingerprint(r, "cc_prompt", q, model="other")


def _jacob_rec():
    msgs = [
        U("Never push to main."),
        A("ok"),
        U("<system-reminder>r</system-reminder>Here is a note offered by a side agent:\n> hi"),
        U("Probe monitor (scheduled): check the pods."),  # a loop prompt: not human
        A("checked"),
        U("<task-notification>\nx"),
        U("Also run make lint."),
    ]
    turns = [
        "Never push to main.",
        "Here is a note offered by a side agent:\n> hi",
        "Also run make lint.",
    ]
    return rec_of(
        msgs,
        user_turns=turns,
        meta={
            "preserved_messages": [
                U("<local-command-stdout>x</local-command-stdout>"),
                U("kept: stay on branch"),
            ]
        },
    )


def test_structural_provenance_mask_with_prefix_fallback(standins):
    r = _jacob_rec()
    mask, prov = R.user_request_mask(r)
    assert prov == "structural+prefix"
    # structural: side-agent note counted (Claude Code marked it human), loop prompt not;
    # preserved messages (no provenance) use the prefix fallback
    assert mask == [True, False, True, False, False, False, True, False, True]
    pub = {**rec_of(history()), "source": "swe-chat-v1", "license": "cc-by-4.0"}
    assert R.user_request_mask(pub)[1] == "prefix"
    broken = {**r, "user_turns": ["not in the history"]}
    assert R.user_request_mask(broken)[1] == "prefix"


def test_codex_and_agy_store_unfiltered_sets(standins):
    r = _jacob_rec()
    q = R.build_codex_local(r)
    assert [m["text"] for m in q.kept_messages] == [
        "Never push to main.",
        "<system-reminder>r</system-reminder>Here is a note offered by a side agent:\n> hi",
        "Also run make lint.",
        "kept: stay on branch",
    ]
    assert len(q.kept_messages_unfiltered) == 7 and q.n_filtered_out == 3
    assert q.info["request_provenance"] == "structural+prefix"
    with pytest.warns(UserWarning):
        a = R.build_antigravity(r)
    assert len(a.kept_messages) == 4 and len(a.kept_messages_unfiltered) == 7
    assert a.n_filtered_out == 3 and a.info["capped"] is False
    row = R.assemble(r, "codex_local", q, [Resp("k", None, "S", None, model=R.MODEL)])
    assert row["kept_messages_unfiltered"] == q.kept_messages_unfiltered
    assert row["n_filtered_out"] == 3


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not json",
        json.dumps({"messages": []}),
        json.dumps({"messages": [{"role": "user", "content": "x"}]}),
        json.dumps({"messages": [{"role": "system", "content": "  "}]}),
    ],
)
def test_codex_capture_missing_or_malformed_refuses_before_dispatch(standins, monkeypatch, content):
    cap = standins / "bad_codex.json"
    if content is not None:
        cap.write_text(content)
    monkeypatch.setattr(R, "CODEX_CAPTURE", cap)
    R.clear_prompt_cache()
    with pytest.raises(R.CodexCaptureError):
        R.build_codex_local(rec_of(history()))
    recs = _recs()
    be = FakeBackend()
    with pytest.raises(R.CodexCaptureError):
        R.run_replays(
            list(recs), ["codex_local"], be, out_dir=standins / "rep", load=recs.__getitem__
        )
    assert be.calls == []


def test_resume_a_b_a_returns_the_current_row(standins, monkeypatch):
    recs = _recs()
    out = standins / "rep"
    rid = "jacob:a:0@cc_prompt"

    def run(rev):
        monkeypatch.setattr(R, "POLICY_REVISION", rev)
        be = FakeBackend()
        c = R.run_replays(list(recs), ["cc_prompt"], be, out_dir=out, load=recs.__getitem__)
        return c["cc_prompt"], be

    run(2)
    fp_a = R.read_replays(out / "cc_prompt.jsonl")[rid]["fingerprint"]
    run(3)
    fp_b = R.read_replays(out / "cc_prompt.jsonl")[rid]["fingerprint"]
    assert fp_a != fp_b
    c, be = run(2)  # back to A: no call, and A is the current row again
    assert be.calls == [] and c["skipped"] == 1 and c["restored"] == 1
    assert R.read_replays(out / "cc_prompt.jsonl")[rid]["fingerprint"] == fp_a
    assert R.read_replays(out / "cc_prompt.jsonl", {rid: fp_b})[rid]["fingerprint"] == fp_b
    c, _ = run(2)  # already current: nothing re-appended
    assert c["restored"] == 0


def test_native_model_is_pinned_at_both_entry_points(tmp_path, native_env):
    with pytest.raises(ValueError, match="claude-haiku-5-5"):
        R.native_compact(
            rec_of(history()),
            model="claude-opus-5-5",
            runner=lambda *a, **k: pytest.fail("dispatched"),
            env={},
            out_dir=tmp_path / "rep",
        )
    recs = _recs()
    with pytest.raises(ValueError, match="claude-haiku-5-5"):
        R.run_replays(
            list(recs),
            [R.NATIVE_ARM],
            None,
            out_dir=tmp_path / "rep",
            load=recs.__getitem__,
            native_kwargs={"model": "claude-opus-5-5"},
        )
    assert not (tmp_path / "rep" / "cc_native.jsonl").exists()


class WrongModelBackend(FakeBackend):
    model = "claude-opus-5-5"


def test_backend_with_wrong_model_is_refused_before_dispatch(standins):
    recs = _recs()
    be = WrongModelBackend()
    with pytest.raises(ValueError, match="claude-haiku-5-5"):
        R.run_replays(
            list(recs), ["cc_prompt"], be, out_dir=standins / "rep", load=recs.__getitem__
        )
    assert be.calls == []


@pytest.mark.parametrize("models", [[""], ["claude-opus-5-5"], [R.MODEL, "claude-opus-5-5"]])
def test_response_model_mismatch_is_an_error(standins, models):
    if len(models) == 1:
        r = rec_of(history())
        q = R.build_cc_prompt(r)
        arm = "cc_prompt"
    else:  # pi split turn: two calls from different models
        r = rec_of([U("x"), A("y"), U("z " + "z" * 90_000), A("w" * 90_000), A("v")])
        q = R.build_pi(r)
        arm = "pi"
        assert len(q.calls) == 2
    resps = [
        Resp(c.label, None, "<summary>s</summary>", None, model=m)
        for c, m in zip(q.calls, models, strict=True)
    ]
    row = R.assemble(r, arm, q, resps)
    assert row["error"] == "model_mismatch" and row["summary"] == ""


def test_parse_native_exit_status_rules():
    events, tail = _canned("sid", ["u"])
    events[-1] = {"type": "_process", "returncode": 1}
    events[-2]["is_error"] = True
    ok = R.parse_native(events, tail, {"u": 0}, R.MODEL)
    assert ok["error"] is None  # verified compaction: kept, status recorded
    assert ok["exit_status"] == {"returncode": 1, "result_is_error": True, "ok": False}
    bad = R.parse_native(
        events, [x for x in tail if x.get("type") != "cost-state"], {"u": 0}, R.MODEL
    )
    assert bad["error"] == "process_failed(rc=1, is_error=True):no_cost_state"


def test_parse_native_usage_limit_is_retryable():
    events = [
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": 1_900_000_000,
                "rateLimitType": "five_hour",
            },
        },
        {"type": "_process", "returncode": 1},
    ]
    p = R.parse_native(events, [], {}, R.MODEL)
    assert p["error"].startswith("limit:") and p["limit"] is not None


def test_torn_tail_is_repaired_before_append(tmp_path):
    f = tmp_path / "arm.jsonl"
    f.write_text(json.dumps({"id": "a", "error": None}) + "\n" + '{"id": "b", "summ')
    R._append(f, [{"id": "c", "error": None}])
    assert [x["id"] for x in R._read_jsonl(f)] == ["a", "c"]
    assert f.read_text().endswith("\n")
    assert R.repair_tail(f) == 0


def test_native_cleanup_armed_before_slug(tmp_path, native_env, monkeypatch):
    _cfg, tmp_root = native_env

    def boom(cwd):
        raise ValueError("slug")

    monkeypatch.setattr(R, "project_slug", boom)
    row = R.native_compact(
        rec_of(history()),
        runner=lambda *a, **k: [],
        env={},
        out_dir=tmp_path / "rep",
        tmp_root=str(tmp_root),
    )
    assert row["error"].startswith("ValueError") and list(tmp_root.iterdir()) == []


def test_native_cleanup_failure_is_surfaced(tmp_path, native_env, monkeypatch):
    _cfg, tmp_root = native_env
    real = R.shutil.rmtree

    def flaky(path, *a, **k):
        if "s1-native-" in str(path):
            raise PermissionError("busy")
        return real(path, *a, **k)

    monkeypatch.setattr(R.shutil, "rmtree", flaky)
    row = R.native_compact(
        rec_of(history()),
        runner=lambda *a, **k: [],
        env={},
        out_dir=tmp_path / "rep",
        tmp_root=str(tmp_root),
    )
    errs = row["native"]["cleanup_errors"]
    assert any("PermissionError" in e for e in errs)


def test_native_pool_defers_after_a_usage_limit(tmp_path, native_env, monkeypatch):
    recs = {f"jacob:n{i}:0": rec_of(history(), f"jacob:n{i}:0") for i in range(4)}
    calls, limited = [], []

    def fake_native(rec, model=R.MODEL, **kw):
        calls.append(rec["id"])
        first_try = calls.count(rec["id"]) == 1 and not limited
        err = "limit:no_boundary" if rec["id"] == "jacob:n1:0" and first_try else None
        if err:
            limited.append(rec["id"])
        return {
            "id": R.replay_id(rec, R.NATIVE_ARM),
            "base_rid": rec["id"],
            "arm": R.NATIVE_ARM,
            "summary": "" if err else "S",
            "kept_messages": [],
            "model": R.MODEL,
            "prompt_sha256": "b" * 64,
            "request_sha256": "c" * 64,
            "usage": {},
            "native": {},
            "error": err,
            "fingerprint": R.native_fingerprint(rec, model),
        }

    monkeypatch.setattr(R, "native_compact", fake_native)
    out = tmp_path / "rep"
    c = R.run_replays(
        list(recs), [R.NATIVE_ARM], None, out_dir=out, load=recs.__getitem__, native_workers=1
    )
    assert c[R.NATIVE_ARM] == {
        "written": 2,
        "skipped": 0,
        "errors": 1,
        "deferred": 2,
        "restored": 0,
    }
    calls.clear()
    c = R.run_replays(
        list(recs), [R.NATIVE_ARM], None, out_dir=out, load=recs.__getitem__, native_workers=4
    )
    assert sorted(calls) == ["jacob:n1:0", "jacob:n2:0", "jacob:n3:0"]
    assert c[R.NATIVE_ARM]["skipped"] == 1 and c[R.NATIVE_ARM]["written"] == 3
