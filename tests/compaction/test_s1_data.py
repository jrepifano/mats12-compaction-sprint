"""Tests for compaction.s1_data (S1 unit U1) on small synthetic records."""

from __future__ import annotations

import json
import os
import stat

import pytest

from compaction import s1_data as d

CONT = "This session is being continued from a previous conversation."


def msg(role, text, calls=None, **kw):
    m = {"role": role, "text": text}
    if calls:
        m["tool_calls"] = calls
    m.update(kw)
    return m


def history():
    return [
        msg("user", "Please fix the failing test. Never push to main."),
        msg("assistant", "Looking.", [{"name": "Bash", "args": {"command": "pytest -q"}, "id": "c1"}]),
        msg("tool", "1 failed", tool_use_id="c1"),
        msg("assistant", "Fixed it."),
    ]


def public(source, sid, n=0, *, model="m1", harness="claude-code", **flags):
    rec = {
        "id": f"{source}:{sid}:{n}",
        "source": source,
        "session_id": sid,
        "harness": harness,
        "model": model,
        "trigger": "auto",
        "pre_tokens": 1000,
        "timestamp": None,
        "source_messages": history(),
        "summary": f"SUMMARY-TEXT-{sid}-{n}",
        "post_messages": [msg("system", "AGENTS.md re-attached"), msg("assistant", "Next.")],
        "user_turns": [],
        "rules": [],
        "license": "ODC-BY-1.0",
        "revision": "abc",
        "scrubbed": False,
        "usable": True,
        "fully_assessable": True,
        "summary_truncated": False,
        "summary_truncation": None,
        "source_truncated": False,
        "source_truncation": {},
        "inherited_truncated_summary": False,
        "boundary_uncertain": False,
        "boundary_uncertainty": {},
    }
    rec.update(flags)
    return rec


def jacob(sid, n=0, *, post=None, summary="JACOB-SUMMARY"):
    return {
        "id": f"jacob:{sid}:{n}",
        "source": "jacob",
        "session_id": sid,
        "harness": "claude-code",
        "model": "claude-opus-5-5",
        "trigger": "auto",
        "pre_tokens": 300000,
        "timestamp": None,
        "source_messages": history(),
        "summary": summary,
        "post_messages": post if post is not None else [msg("system", "AGENTS.md"),
                                                        msg("assistant", "ok")],
        "user_turns": [],
        "rules": [],
        "license": "private",
        "revision": None,
        "scrubbed": False,
    }


def meta_row(rid, ok=True):
    return {"id": rid, "chain_walk_ok": ok, "quarantine": {"x": 1},
            "preserved_messages": [msg("assistant", "kept verbatim reply")]}


def fml_cut_history():
    return [
        msg("user", "Train the model."),
        msg("assistant", "Running.", [{"name": "Bash", "args": "x" * d.FML_TOOL_CAP, "id": "a"}]),
        msg("tool", "y" * d.FML_TOOL_CAP, tool_use_id="a"),
        msg("assistant", "Done."),
    ]


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


@pytest.fixture
def dataset(tmp_path):
    jac = [jacob(f"s{i}", 0) for i in range(6)] + [jacob("s0", 1)]
    jac.append(jacob("s9", 0, post=[msg("system", "AGENTS.md")]))  # session ended: no reply
    jp = write_jsonl(tmp_path / "sessions" / "compactions.jsonl", jac)
    write_jsonl(tmp_path / "sessions" / "meta.jsonl", [meta_row(r["id"]) for r in jac])
    swe = []
    for i in range(12):
        swe.append(public("swe-chat-v1", f"2026-01-0{1 + i % 2}-uuid{i // 2}", 0,
                          model=f"m{i % 3}"))
    swe.append(public("swe-chat-v1", "ses_oc1", 0, harness="opencode"))
    swe.append(public("swe-chat-v1", "cut", 0, fully_assessable=False,
                      source_truncated=True, source_truncation={"local_cut_60k": 1}))
    ptb = [public("posttrainbench", f"run{i}/task", 0, license="Apache-2.0") for i in range(6)]
    fml = [public("frontierml", f"run{i}", 0, fully_assessable=False, source_truncated=True,
                  source_truncation={"dataset_tool_args": 1, "dataset_tool_output": 1},
                  source_messages=fml_cut_history()) for i in range(6)]
    fml.append(public("frontierml", "msgcut", 0, fully_assessable=False,
                      source_truncation={"dataset_message": 1}))
    paths = {
        "jacob": jp,
        "swe-chat-v1": write_jsonl(tmp_path / "public" / "swe.jsonl", swe),
        "posttrainbench": write_jsonl(tmp_path / "public" / "ptb.jsonl", ptb),
        "frontierml": write_jsonl(tmp_path / "public" / "fml.jsonl", fml),
    }
    return paths


# ------------------------------------------------------------------------------------------
# Eligibility
# ------------------------------------------------------------------------------------------


def test_public_eligibility_rules():
    assert d.eligibility(public("swe-chat-v1", "a")) == (True, None)
    ok, why = d.eligibility(public("swe-chat-v1", "a", fully_assessable=False,
                                   source_truncation={"local_cut_60k": 2}))
    assert not ok and why == "not fully assessable (local_cut_60k)"
    assert d.eligibility(public("swe-chat-v1", "a", usable=False)) == (False, "not usable")
    assert d.eligibility(public("swe-chat-v1", "a", summary="  "))[1] == "no summary text"
    assert d.eligibility(public("swe-chat-v1", "a", summary_truncated=True,
                                summary_truncation="local_cut_60k"))[1].startswith("summary cut")
    ok, why = d.eligibility(public("posttrainbench", "a", boundary_uncertain=True,
                                   boundary_uncertainty={"no_boundary_record": 1}))
    assert why == "uncertain boundary (no_boundary_record)"
    rec = public("swe-chat-v1", "a")
    del rec["usable"]
    assert d.eligibility(rec) == (False, "usable not assessed")
    assert d.eligibility(public("agy", "a"))[0] is False


def test_frontierml_tolerates_only_tool_cuts():
    tool = public("frontierml", "a", fully_assessable=False,
                  source_truncation={"dataset_tool_output": 3, "dataset_tool_args": 1})
    assert d.eligibility(tool) == (True, None)
    assert d.eligibility(public("frontierml", "a", fully_assessable=False,
                                source_truncation={"dataset_tool_output": 3,
                                                   "dataset_message": 1}))[0] is False
    inh = public("frontierml", "a", fully_assessable=False, inherited_truncated_summary=True)
    assert d.eligibility(inh) == (False, "inherits a cut summary")
    inv_row = {"source": "frontierml", "has_summary": False, "usable": False,
               "why_unusable": "no summary text stored (token drop only)"}
    assert d.eligibility(inv_row)[1].startswith("no summary text (no summary text stored")


def test_jacob_rule_from_sidecar():
    rec = jacob("s1")
    assert d.eligibility(rec, meta_row(rec["id"])) == (True, None)
    # quarantine (privacy) does not count; no assistant after the boundary does not either
    ended = jacob("s1", post=[msg("system", "AGENTS.md")])
    assert d.eligibility(ended, meta_row(ended["id"])) == (True, None)
    assert d.jacob_flags(ended, meta_row(ended["id"]))["post_actions_present"] is False
    assert d.eligibility(rec, None) == (False, "not fully assessable: no sidecar row")
    ok, why = d.eligibility(rec, meta_row(rec["id"], ok=False))
    assert not ok and "chain walk failed" in why
    ok, why = d.eligibility(jacob("s1", summary=""), meta_row(rec["id"]))
    assert why == "not usable: no summary text"
    no_hist = jacob("s1")
    no_hist["source_messages"] = [msg("user", "hi")]
    assert d.eligibility(no_hist, meta_row(rec["id"]))[1].startswith("not usable: no assistant")


# ------------------------------------------------------------------------------------------
# Gaps and rendering
# ------------------------------------------------------------------------------------------


def test_frontierml_gaps_detected_and_checked_against_counts():
    rec = public("frontierml", "a", source_messages=fml_cut_history(),
                 source_truncation={"dataset_tool_args": 1, "dataset_tool_output": 1})
    gaps, exact = d.message_gaps(rec)
    assert gaps == {1: ["dataset_tool_args"], 2: ["dataset_tool_output"]} and exact
    rec["source_truncation"] = {"dataset_tool_output": 1}  # an uncut field at the cap
    assert d.message_gaps(rec)[1] is False
    # an inherited cut summary at index 0 is not counted in source_truncation
    rec2 = public("frontierml", "b", inherited_truncated_summary=True,
                  source_messages=[msg("system", CONT + "z" * (d.FML_MESSAGE_CAP - len(CONT)))]
                  + fml_cut_history(),
                  source_truncation={"dataset_tool_args": 1, "dataset_tool_output": 1})
    gaps, exact = d.message_gaps(rec2)
    assert gaps[0] == ["dataset_message"] and exact


def test_local_cut_marker_public_only():
    cut = "a" * 10 + "\n[... cut by public_sets: 123 more chars]"
    rec = public("swe-chat-v1", "a", source_messages=[msg("tool", cut), msg("assistant", "x")],
                 source_truncation={"local_cut_60k": 1})
    r = d.render(rec)
    assert r.gaps == [0] and r.gap_causes == {0: ["local_cut_60k"]} and r.gaps_exact
    assert "cut by public_sets" not in r.text
    assert r.text.startswith("[m0] TOOL: aaaaaaaaaa\n[text cut at 60k characters]")
    jac = jacob("s1")
    jac["source_messages"] = [msg("tool", cut)]  # Jacob's sessions are never cut
    assert d.render(jac).gaps == []


def test_render_format_spans_and_markers():
    rec = public("frontierml", "a", source_messages=fml_cut_history(),
                 source_truncation={"dataset_tool_args": 1, "dataset_tool_output": 1})
    r = d.render(rec)
    blocks = r.text.split("\n\n")
    assert len(blocks) == 4 and r.bytes == len(r.text.encode())
    assert blocks[0] == "[m0] USER: Train the model."
    assert blocks[1] == "[m1] ASSISTANT: Running.\n<tool_call Bash>" + "x" * 8000 + \
        "\n[tool call arguments cut by dataset]"
    assert blocks[2].endswith("y\n[tool output cut by dataset]")
    for i, (s, e) in r.spans.items():
        assert r.text[s:e] == blocks[i] and r.text[s:].startswith(f"[m{i}] ")
    assert r.gaps == [1, 2]
    assert d.render(rec).text == r.text  # deterministic


def test_render_dict_args_empty_text_slice_and_tool_result_max():
    rec = public("swe-chat-v1", "a")
    r = d.render(rec)
    assert '[m1] ASSISTANT: Looking.\n<tool_call Bash>{"command":"pytest -q"}' in r.text
    rec["source_messages"].append(msg("assistant", "", [{"name": "Read", "args": {"p": "é"}}]))
    rec["source_messages"].append(msg("tool", "z" * 50))
    r = d.render(rec, start=4, tool_result_max=10)
    assert sorted(r.spans) == [4, 5]
    assert r.text.startswith('[m4] ASSISTANT:\n<tool_call Read>{"p":"é"}\n\n[m5] TOOL: ')
    assert "[... 40 more characters truncated]" in r.text
    assert r.bytes == len(r.text.encode("utf-8")) > len(r.text)
    assert d.render(rec, 1, 3).text.split("\n\n")[0].startswith("[m1] ")


def test_post_context_kept_then_reattached():
    rec = jacob("s1", post=[msg("system", "AGENTS.md"), msg("system", "MEMORY.md"),
                            msg("assistant", "go"), msg("system", "late")])
    rec["meta"] = meta_row(rec["id"])
    got = [m["text"] for m in d.post_context(rec)]
    assert got == ["kept verbatim reply", "AGENTS.md", "MEMORY.md"]
    pub = public("swe-chat-v1", "a")
    pub["meta"] = None
    assert [m["text"] for m in d.post_context(pub)] == ["AGENTS.md re-attached"]


# ------------------------------------------------------------------------------------------
# Index, load, samples, labels, manifest
# ------------------------------------------------------------------------------------------


def test_index_and_load(dataset):
    idx = d.index_all(dataset)
    by = {e["id"]: e for e in idx}
    assert len(idx) == 8 + 14 + 6 + 7
    assert by["jacob:s9:0"]["eligible"] and not by["jacob:s9:0"]["post_actions_present"]
    assert by["frontierml:run0:0"]["eligible"] and not by["frontierml:run0:0"]["history_complete"]
    assert by["frontierml:run0:0"]["n_gaps"] == 2 and by["frontierml:run0:0"]["gaps_exact"]
    assert not by["frontierml:msgcut:0"]["eligible"]
    assert not by["swe-chat-v1:cut:0"]["eligible"]
    assert by["swe-chat-v1:2026-01-02-uuid0:0"]["session_group"] == "swe-chat-v1:uuid0"
    rec = d.load("jacob:s1:0", idx)
    assert rec["meta"]["chain_walk_ok"] and rec["private"] and rec["history_complete"]
    pub = d.load("swe-chat-v1:2026-01-01-uuid0:0", idx)
    assert pub["meta"] is None and not pub["private"] and pub["history_complete"]
    assert not d.load("frontierml:run0:0", idx)["history_complete"]
    with pytest.raises(KeyError):
        d.load("nope:x:0", idx)


def test_sampling_deterministic_and_stratified(dataset, monkeypatch):
    monkeypatch.setattr(d, "PART_A_N", {"swe-chat-v1": 6, "posttrainbench": 3,
                                        "frontierml": 2})
    idx = d.index_all(dataset)
    by = {e["id"]: e for e in idx}
    a = d.sample_part_a(index=idx)
    assert a == d.sample_part_a(index=list(reversed(idx)))  # order-independent
    assert a != d.sample_part_a(seed=1, index=idx)
    jac = [r for r in a if r.startswith("jacob:")]
    assert jac == sorted(e["id"] for e in idx if e["source"] == "jacob")
    swe = [r for r in a if r.startswith("swe-chat-v1:")]
    assert len(swe) == 6 and all(by[r]["eligible"] for r in a)
    # (model, harness) strata: m0, m1, m2 claude-code and m1 opencode share 6 evenly-ish
    strata = {(by[r]["model"], by[r]["harness"]) for r in swe}
    assert ("m1", "opencode") in strata and len(strata) == 4
    b = d.pick_part_b(a, index=idx, n_public=3)
    assert b == d.pick_part_b(a, index=idx, n_public=3)
    assert b[:len(jac)] == jac
    assert all(by[r]["source"] == "swe-chat-v1" and by[r]["harness"] == "claude-code"
               and r in a for r in b[len(jac):])
    with pytest.raises(ValueError):
        d.pick_part_b(a, index=idx, n_public=50)


def test_label_split_is_session_disjoint(dataset, monkeypatch):
    monkeypatch.setattr(d, "PART_A_N", {"swe-chat-v1": 12, "posttrainbench": 6,
                                        "frontierml": 6})
    idx = d.index_all(dataset)
    by = {e["id"]: e for e in idx}
    a = d.sample_part_a(index=idx)
    lab = d.label_records(a, n_jacob=5, n_public=9, index=idx)
    assert lab == d.label_records(a, n_jacob=5, n_public=9, index=idx)
    assert len(lab["dev"]) + len(lab["held"]) == 14
    gd = {by[r]["session_group"] for r in lab["dev"]}
    gh = {by[r]["session_group"] for r in lab["held"]}
    assert not gd & gh
    allg = [by[r]["session_group"] for r in lab["dev"] + lab["held"]]
    assert len(allg) == len(set(allg))  # snapshots of one SWE-chat session count as one
    src = [by[r]["source"] for r in lab["held"]]
    assert src.count("jacob") == 3 and {"swe-chat-v1", "posttrainbench", "frontierml"} <= set(src)
    with pytest.raises(ValueError):
        d.label_records(a, n_jacob=20, index=idx)


def test_write_manifest(dataset, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "PART_A_N", {"swe-chat-v1": 4, "posttrainbench": 2,
                                        "frontierml": 2})
    idx = d.index_all(dataset)
    a = d.sample_part_a(index=idx)
    b = d.pick_part_b(a, index=idx, n_public=2)
    lab = d.label_records(a, n_jacob=2, n_public=3, index=idx)
    priv_p, pub_p = tmp_path / "out" / "private.json", tmp_path / "out" / "public.json"
    man = d.write_manifest(a, b, lab, priv_p, pub_p, paths=dataset)
    assert stat.S_IMODE(os.stat(priv_p).st_mode) == 0o600
    pub = json.loads(pub_p.read_text())
    assert pub["ids"]["part_a"] == a and pub["counts"]["part_b"] == len(b)
    assert pub["seed"] == d.S1_SEED and len(pub["files"]) == 4
    assert all(len(f["sha256"]) == 64 for f in pub["files"])
    text = pub_p.read_text()
    assert "SUMMARY-TEXT" not in text and "JACOB-SUMMARY" not in text
    assert "Never push" not in text and str(tmp_path) not in text
    assert "created_utc" not in pub and man["created_utc"]
    assert set(man["records"]) == set(a) | set(b)


# ------------------------------------------------------------------------------------------
# Review r1 fixes
# ------------------------------------------------------------------------------------------


def jacob_with_preserved():
    rec = jacob("s1")
    rec["meta"] = meta_row(rec["id"])
    rec["meta"]["preserved_messages"].append(
        msg("assistant", "", [{"name": "Bash", "args": {"command": "git status"}}]))
    return rec


def test_history_messages_and_render_with_preserved():
    rec = jacob_with_preserved()
    hist = d.history_messages(rec)
    assert len(hist) == 6 and hist[4]["text"] == "kept verbatim reply"
    r = d.render(rec)
    assert r.n_source == 4 and sorted(r.spans) == [0, 1, 2, 3, 4, 5]
    blocks = r.text.split("\n\n")
    assert blocks[4] == d.KEPT_HEADER
    assert blocks[5] == "[m4] ASSISTANT: kept verbatim reply"
    assert blocks[6] == '[m5] ASSISTANT:\n<tool_call Bash>{"command":"git status"}'
    assert f"{d.KEPT_HEADER}\n\n[m4] ASSISTANT:" in r.text
    for i, (s, e) in r.spans.items():
        assert r.text[s:e] == d.render_message(i, hist[i])
        assert d.KEPT_HEADER not in r.text[s:e]
    old = d.render(rec, include_preserved=False)
    assert sorted(old.spans) == [0, 1, 2, 3] and d.KEPT_HEADER not in old.text
    assert old.n_source == 4 and r.text.startswith(old.text + "\n\n" + d.KEPT_HEADER)
    # no preserved messages: no header
    assert d.KEPT_HEADER not in d.render(public("swe-chat-v1", "a")).text


def test_render_slice_crossing_the_preserved_boundary():
    rec = jacob_with_preserved()
    full = d.render(rec)
    r = d.render(rec, start=3, end=5)
    assert sorted(r.spans) == [3, 4]
    assert r.text.split("\n\n") == ["[m3] ASSISTANT: Fixed it.", d.KEPT_HEADER,
                                    "[m4] ASSISTANT: kept verbatim reply"]
    for i, (s, e) in r.spans.items():
        fs, fe = full.spans[i]
        assert r.text[s:e] == full.text[fs:fe]
    tail = d.render(rec, start=4)
    assert tail.text.startswith(d.KEPT_HEADER + "\n\n[m4] ")
    assert d.KEPT_HEADER not in d.render(rec, start=5).text  # header only before m<n_source>


def test_part_a_draw_sizes_are_enforced(dataset, monkeypatch):
    idx = d.index_all(dataset)
    monkeypatch.setattr(d, "PART_A_N", {"swe-chat-v1": 6, "posttrainbench": 7,
                                        "frontierml": 2})
    with pytest.raises(ValueError, match="needs 7 eligible posttrainbench"):
        d.sample_part_a(index=idx)
    monkeypatch.setattr(d, "PART_A_N", {"swe-chat-v1": 6, "posttrainbench": 6,
                                        "frontierml": 2})
    assert len(d.sample_part_a(index=idx)) == 8 + 14


def test_session_group_on_entries_and_loaded_records(dataset):
    idx = d.index_all(dataset)
    by = {e["id"]: e for e in idx}
    a, b = "swe-chat-v1:2026-01-01-uuid0:0", "swe-chat-v1:2026-01-02-uuid0:0"
    assert by[a]["session_id"] != by[b]["session_id"]
    assert by[a]["session_group"] == by[b]["session_group"] == "swe-chat-v1:uuid0"
    ra, rb = d.load(a, idx), d.load(b, idx)
    assert ra["session_id"] == "2026-01-01-uuid0" and rb["session_id"] == "2026-01-02-uuid0"
    assert ra["session_group"] == rb["session_group"] == "swe-chat-v1:uuid0"
    assert d.load("jacob:s1:0", idx)["session_group"] == "jacob:s1"


def test_sidecar_bound_to_indexed_snapshot(dataset):
    idx = d.index_all(dataset)
    rec = d.load("jacob:s1:0", idx)
    assert len(d.history_messages(rec)) == 5  # preserved message from the sidecar
    side = os.path.join(os.path.dirname(dataset["jacob"]), "meta.jsonl")
    with open(side, "a") as f:
        f.write(json.dumps({"id": "jacob:extra:0", "chain_walk_ok": True}) + "\n")
    with pytest.raises(RuntimeError, match="changed since it was indexed"):
        d.load("jacob:s1:0", idx)
    assert d.load("swe-chat-v1:2026-01-01-uuid0:0", idx)["meta"] is None  # public unaffected
    idx2 = d.index_all(dataset, refresh=True)
    assert d.load("jacob:s1:0", idx2)["meta"]["chain_walk_ok"]
