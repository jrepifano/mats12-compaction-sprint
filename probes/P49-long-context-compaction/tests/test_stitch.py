"""Stitching, data cleaning, token counting and fill packing (plan "Automatic rules": stitching).

Real-data tests need the length index (`python -m p49.data --index`); they reuse saved
histories from `python -m p49.stitch --all` when present. The 48-history gate is `-m slow`.
"""
import asyncio
import json
import time
from collections import Counter

import pytest
from p49 import contracts as C
from p49 import data, replay, stitch, tokens

needs_index = pytest.mark.skipif(not C.INDEX_JSON.exists(), reason="no length index")


# --- synthetic: cleaning -----------------------------------------------------------------------
def _a(*ids, content="x"):
    return {"role": "assistant", "content": content,
            "tool_calls": [{"id": i, "type": "function",
                            "function": {"name": "execute_bash", "arguments": "{}"}} for i in ids]}


def _t(i, content="out"):
    return {"role": "tool", "tool_call_id": i, "content": content}


def test_strip_none_and_normalize():
    raw = {"role": "tool", "content": None, "name": "think", "tool_call_id": "c1",
           "tool_calls": None}
    m = data.normalize_message(data.strip_none(raw))
    assert m == {"role": "tool", "tool_call_id": "c1", "content": ""}
    a = data.normalize_message({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c2", "type": "function", "function": {"name": "think", "arguments": '{"a":1}'}}]})
    assert a["content"] == "" and a["tool_calls"][0]["id"] == "c2"
    assert "tool_calls" not in data.normalize_message({"role": "assistant", "content": "hi",
                                                       "tool_calls": []})


@pytest.mark.parametrize("body,keep", [
    ([_a("1"), _t("1"), _a("2")], 2),                       # trailing call, no result
    ([_a("1"), _t("1"), _a("2", "3"), _t("2")], 2),          # partial results
    ([_a("1"), _t("1"), {"role": "assistant", "content": "done"}], 3),
    ([_a("1"), _t("1"), _t("9")], 2),                        # orphan result
    ([_a("1"), _t("1"), _a("1"), _t("1")], 2),               # reused id
    ([{"role": "user", "content": "go on"}, _a("1"), _t("1")], 3),
])
def test_dangling_cut(body, keep):
    assert data.complete_prefix_len(body) == keep


def test_clean_row_cuts_and_counts():
    row = {"trajectory_id": "t", "instance_id": "i", "exit_status": "submit",
           "trajectory": [{"role": "system", "content": "S", "name": None},
                          {"role": "user", "content": "T"}, _a("1"), _t("1"), _a("2")],
           "tools": [{"type": "function", "function": {"name": "x", "description": None}}]}
    s = data.clean_row(row)
    assert s.cut_dangling == 1 and len(s.body) == 2 and s.exit_status == "submitted"
    assert s.tools == [{"type": "function", "function": {"name": "x"}}]


def test_exit_mapping():
    assert data.normalize_exit("RuntimeError: Agent reached maximum iteration. x") == \
        "iteration_limit"
    assert data.normalize_exit("AgentStuckInLoopError: Agent got stuck") == "stuck"


# --- synthetic: join, namespacing, validation ------------------------------------------------
def _sess(task="Fix it", ids=("a", "b")):
    body = []
    for i in ids:
        body += [_a(i), _t(i)]
    return C.Session(trajectory_id="s", instance_id="i2", exit_status="submitted", system="S",
                     task=task, body=body, tools=[])


def test_join_text_and_namespacing():
    blk = stitch.join_block(_sess(), "s2_")
    assert blk[0] == {"role": "user", "content": "New task: Fix it"}
    assert [m["tool_calls"][0]["id"] for m in blk if m.get("tool_calls")] == ["s2_a", "s2_b"]
    assert [m["tool_call_id"] for m in blk if m["role"] == "tool"] == ["s2_a", "s2_b"]
    head = [{"role": "system", "content": "S"}, {"role": "user", "content": "T"}, _a("a"), _t("a")]
    stitch.validate_messages(head + blk)                     # ids unique after namespacing
    with pytest.raises(stitch.StitchError, match="duplicate"):
        stitch.validate_messages(head + stitch.join_block(_sess(), ""))


@pytest.mark.parametrize("msgs,err", [
    ([{"role": "user", "content": "T"}], "start"),
    ([{"role": "system", "content": "S"}, {"role": "user", "content": "T"}, _t("z")], "answers"),
    ([{"role": "system", "content": "S"}, {"role": "user", "content": "T"}, _a("q"),
      {"role": "user", "content": "u"}], "unanswered"),
    ([{"role": "system", "content": "S"}, {"role": "user", "content": "T"}, _a("q")],
     "unanswered"),
    ([{"role": "system", "content": "S"}, {"role": "user", "content": "T"},
      {"role": "system", "content": "S2"}], "role"),
])
def test_validate_rejects(msgs, err):
    with pytest.raises(stitch.StitchError, match=err):
        stitch.validate_messages(msgs)


# --- S1 order ----------------------------------------------------------------------------------
@needs_index
def test_s1_order_stratified_prefixes():
    order = stitch.s1_order()
    pool = stitch.s1_pool()
    assert sorted(order) == sorted(pool) and len(order) == 193
    assert stitch.s1_order() == order                       # deterministic
    total = sum(C.S1_STRATA.values())
    for n in range(1, 194):
        got = Counter(pool[t] for t in order[:n])
        for st, k in C.S1_STRATA.items():
            assert abs(got[st] - n * k / total) <= 1, (n, st)
    first48 = Counter(pool[t] for t in order[:48])
    assert first48 == {"iteration_limit": 38, "submitted": 10}


# --- tokens ------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sample_session():
    if not C.INDEX_JSON.exists():
        pytest.skip("no length index")
    tid = stitch.s1_order()[0]
    return data.load_session(tid)


def test_count_matches_apply_chat_template(sample_session):
    s = sample_session
    msgs = data.session_messages(s)[:40]
    ref = tokens.tokenizer().apply_chat_template(tokens._for_template(msgs), tools=s.tools,
                                                 tokenize=True, add_generation_prompt=True)
    ref = ref["input_ids"] if hasattr(ref, "keys") else ref
    assert tokens.count(msgs, s.tools) == len(ref)


def test_unparseable_arguments_do_not_crash():
    m = [{"role": "system", "content": "S"}, {"role": "user", "content": "T"},
         {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function",
          "function": {"name": "x", "arguments": "{not json"}}]},
         {"role": "tool", "tool_call_id": "1", "content": "r"}]
    before = tokens.stats["unparsed_args"]
    assert tokens.count(m, None) > 0
    assert tokens.stats["unparsed_args"] == before + 1


def test_count_messages_additive_close_to_exact_delta(sample_session):
    s = sample_session
    msgs = data.session_messages(s)
    deltas = []
    for k in (3, len(msgs) // 3, len(msgs) // 2, len(msgs) - 6):
        exact = tokens.count(msgs, s.tools) - tokens.count(msgs[:k], s.tools)
        add = tokens.count_messages(msgs[k:])
        deltas.append((k, exact, add))
        assert abs(add - exact) <= max(3, 0.001 * exact), (k, exact, add)
    # per-turn deltas: one assistant + its tool results
    for k in range(10, 30, 2):
        exact = tokens.count(msgs[:k + 2], s.tools) - tokens.count(msgs[:k], s.tools)
        assert abs(tokens.count_messages(msgs[k:k + 2]) - exact) <= 2
    print("additive vs exact", deltas)


def test_count_threadsafe(sample_session):
    s = sample_session
    msgs = data.session_messages(s)
    tokens.clear_cache()
    want = [tokens.count(msgs[:n], s.tools) for n in range(20, 60, 5)]
    tokens.clear_cache()

    async def go():
        return await asyncio.gather(*[asyncio.to_thread(tokens.count, msgs[:n], s.tools)
                                      for n in range(20, 60, 5)])
    assert asyncio.run(go()) == want


@needs_index
def test_count_speed_200k(history0):
    tokens.clear_cache()
    t = time.time()
    n = tokens.count(history0.messages, history0.tools)
    dt = time.time() - t
    print(f"count of {n} tokens: {dt:.2f} s")
    assert n == history0.tokens and dt < 10


# --- real stitched history -------------------------------------------------------------------
@pytest.fixture(scope="module")
def history0():
    if not C.INDEX_JSON.exists():
        pytest.skip("no length index")
    if stitch.history_path("h0").exists():
        return stitch.load_history("h0")
    return stitch.build_history(0)


def test_history_checks(history0):
    h = history0
    idx = data.load_index()
    stitch.check_history(h)
    assert h.tokens == tokens.count(h.messages, h.tools)
    assert h.s1_tokens == tokens.count(h.s1_messages(), h.tools)
    assert C.BAND[0] <= h.tokens * C.CALIB.c <= C.BAND[1]
    s1, s2 = (data.load_session(t) for t in h.sessions)
    assert h.messages[h.join_index]["content"] == C.JOIN_PREFIX + s2.task
    assert h.messages[1]["content"] == s1.task
    assert sum(m["role"] == "system" for m in h.messages) == 1      # S2's system dropped
    r1, r2 = (idx["sessions"][t] for t in h.sessions)
    assert r1["system_sha256"] == r2["system_sha256"] and r1["tools_sha256"] == r2["tools_sha256"]
    assert s1.instance_id != s2.instance_id and h.instances == [s1.instance_id, s2.instance_id]
    s2_ids = [tc["id"] for m in h.messages[h.join_index:] for tc in m.get("tool_calls") or []]
    assert s2_ids and all(i.startswith("s2_") for i in s2_ids)
    assert h.sessions[0] in stitch.s1_pool()
    assert stitch.size_arm_messages(h) == h.messages[:h.join_index]


def test_history_save_load_roundtrip(history0, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "HISTORIES_DIR", tmp_path)
    stitch.save_history(history0)
    h = stitch.load_history(history0.history_id)
    assert h == history0 and isinstance(h.band, tuple)


def test_band_violation_raises(history0):
    h = C.History(**{**history0.__dict__, "band": (1, 2)})
    with pytest.raises(stitch.StitchError, match="outside"):
        stitch.check_history(h)


def test_fill_packing_one_history(history0):
    h = history0
    used = set(h.instances)
    ctx = stitch.mock_inspect_carry(h.messages, "summary " * 300)
    out, info = replay.extend(ctx, h.tools, history=h, k=2, used_instances=used, seed=h.seed)
    assert out[:3] == ctx
    assert C.BAND[0] <= info["tokens"] * C.CALIB.c <= C.BAND[1]
    assert info["tokens"] == tokens.count(out, h.tools)
    stitch.validate_messages(out)
    assert set(info["instances"]) <= used and not set(info["instances"]) & set(h.instances)
    joins = [m for m in out[3:] if m["role"] == "user" and m["content"].startswith("New task: ")]
    assert len(joins) == info["n_sessions"] == len(info["sessions"])
    ids = [tc["id"] for m in out for tc in m.get("tool_calls") or []]
    assert all(i.startswith("f2_") for i in ids)
    assert {i.split("_")[1] for i in ids} == {str(n) for n in range(1, info["n_sessions"] + 1)}
    _, info2 = replay.extend(ctx, h.tools, history=h, k=2, used_instances=set(h.instances),
                                seed=h.seed)
    assert info2["sessions"] == info["sessions"]             # deterministic


def test_fill_infeasible_raises(history0, monkeypatch):
    monkeypatch.setattr(C, "RESEEDS", 0)
    monkeypatch.setattr(replay, "CLOSE_EXACT_PER_SEED", 0)
    with pytest.raises(replay.Infeasible):
        replay.extend(stitch.mock_inspect_carry(history0.messages, "s"), history0.tools,
                      history=history0, k=2, used_instances=set(history0.instances), seed=0)


# --- the required gate: every planned history and its F1/F2 packing ----------------------------
@pytest.mark.slow
def test_all_48_histories_and_packing():
    rep = stitch.build_all(save=False)
    s = rep["summary"]
    print(json.dumps(s, indent=1))
    assert s["built"] == 48
    assert s["packing_ok"] == {2000: 48, 16000: 48}
    s1s = [x["s1"] for x in rep["histories"]]
    assert len(set(s1s)) == 48
