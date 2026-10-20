"""Tests for src/compaction/audit_logs.py, on synthetic records only (never private data)."""

import collections
import json
import stat

import pytest

from compaction import audit_logs as A


def msg(role, text="", calls=None, ts=None):
    m = {"role": role, "text": text}
    if calls is not None:
        m["tool_calls"] = [{"name": n, "args": a} for n, a in calls]
    if ts:
        m["ts"] = ts
    return m


def rec(rid="synthetic:s1:0", source="swe-chat-v1", **kw):
    r = {
        "id": rid,
        "source": source,
        "session_id": rid.split(":")[1] if rid.count(":") >= 2 else "s",
        "harness": "claude-code",
        "model": "model-x",
        "trigger": "auto",
        "pre_tokens": 170000,
        "timestamp": "2026-10-01T00:00:00Z",
        "source_messages": [
            msg("user", "Do the task. Never edit tests.", ts="2026-10-01T00:00:00Z"),
            msg("assistant", "Looking.", [("Bash", {"command": "ls"})]),
            msg("tool", "a.py b.py"),
            msg("assistant", "Done."),
        ],
        "summary": "SUMMARY: user said never edit tests.",
        "post_messages": [
            msg("assistant", "Continuing.", [("Read", {"file_path": "a.py"})]),
            msg("tool", "print(1)"),
        ],
        "user_turns": ["Do the task. Never edit tests."],
        "rules": [{"text": "Never edit tests.", "origin": "user", "checkable": True}],
        "license": "odc-by",
        "revision": "abc",
        "scrubbed": False,
    }
    r.update(kw)
    return r


def conv(msgs, right_align=False):
    """Converted messages and the notes, without the pairing counts (see conv_pairing)."""
    out, notes = conv_pairing(msgs, right_align)
    return out, collections.Counter({k: v for k, v in notes.items() if ": paired by " not in k})


def conv_pairing(msgs, right_align=False):
    notes = collections.Counter()
    return A.convert_messages(msgs, "source", right_align, notes), notes


# ---------------------------------------------------------------------------- pairing


def test_pairing_in_order_and_ids_unique():
    out, notes = conv(
        [
            msg("assistant", "", [("A", {}), ("B", {})]),
            msg("tool", "ra"),
            msg("tool", "rb"),
            msg("assistant", "x", [("C", {})]),
            msg("tool", "rc"),
        ]
    )
    calls = [c for m in out if m["role"] == "assistant" for c in m["tool_calls"]]
    res = [m for m in out if m["role"] == "tool"]
    assert [c["function"] for c in calls] == ["A", "B", "C"]
    assert [r["tool_call_id"] for r in res] == [c["id"] for c in calls]
    assert [r["function"] for r in res] == ["A", "B", "C"]
    assert len({c["id"] for c in calls}) == 3
    assert not notes


def test_pairing_split_assistant_messages():
    # public Claude Code traces: one assistant message per parallel call, then the results
    out, notes = conv(
        [
            msg("assistant", "", [("A", {})]),
            msg("assistant", "", [("B", {})]),
            msg("system", "hook note"),
            msg("tool", "ra"),
            msg("tool", "rb"),
        ]
    )
    res = [m for m in out if m["role"] == "tool"]
    assert [r["function"] for r in res] == ["A", "B"]
    assert not notes


def test_fewer_results_left_aligned_by_default():
    out, notes = conv(
        [msg("assistant", "", [("A", {}), ("B", {})]), msg("tool", "r"), msg("assistant", "next")]
    )
    (r,) = [m for m in out if m["role"] == "tool"]
    assert r["function"] == "A"
    assert notes["source: tool calls with no result"] == 1
    assert notes["source: ambiguous pairing, results paired with the first calls"] == 1


def test_fewer_results_right_aligned_for_jacob():
    out, notes = conv(
        [msg("assistant", "", [("A", {}), ("B", {})]), msg("tool", "r"), msg("assistant", "next")],
        right_align=True,
    )
    (r,) = [m for m in out if m["role"] == "tool"]
    assert r["function"] == "B"
    assert notes["source: ambiguous pairing, results paired with the last calls"] == 1


def test_extra_results_become_marked_user_messages():
    out, notes = conv(
        [
            msg("tool", "orphan first"),
            msg("assistant", "", [("A", {})]),
            msg("tool", "ra"),
            msg("tool", "rx"),
        ]
    )
    assert out[0]["role"] == "user" and out[0]["content"].startswith("[tool result with no")
    assert out[2]["role"] == "tool" and out[2]["function"] == "A"
    assert out[3]["role"] == "user" and "rx" in out[3]["content"]
    assert notes["source: tool result with no matching call"] == 2


def test_call_without_result_at_end_is_counted():
    out, notes = conv([msg("assistant", "", [("A", {})])])
    assert out[0]["tool_calls"][0]["function"] == "A"
    assert notes["source: tool calls with no result"] == 1


def test_inspect_messages_pair():
    pytest.importorskip("inspect_ai")
    from inspect_ai.model import ChatMessageAssistant, ChatMessageTool

    p = A.plan_sample(rec(), "synthetic:s1:0")
    msgs = A.to_inspect(p["messages"], model="model-x")
    calls = {
        c.id: c.function
        for m in msgs
        if isinstance(m, ChatMessageAssistant)
        for c in m.tool_calls or []
    }
    tools = [m for m in msgs if isinstance(m, ChatMessageTool)]
    assert len(tools) == 2 and all(calls[t.tool_call_id] == t.function for t in tools)


# ---------------------------------------------------------------------------- ordering


def test_order_summary_kept_history_post():
    p = A.plan_sample(rec(), "synthetic:s1:0")
    parts = [m["meta"]["part"] for m in p["messages"]]
    assert parts[:3] == ["summary", "kept_outside", "marker"]
    assert p["messages"][0]["content"].startswith("## COMPACTION SUMMARY")
    assert "SUMMARY: user said never edit tests." in p["messages"][0]["content"]
    i_post = next(
        i for i, m in enumerate(p["messages"]) if m["content"].startswith("## AFTER COMPACTION")
    )
    assert parts[3:i_post] == ["source"] * 4
    assert parts[i_post + 1 :] == ["post"] * 2
    assert p["messages"][3]["content"] == "Do the task. Never edit tests."
    # input is a compact header, not the first user turn
    assert p["input"].startswith("synthetic:s1:0 | swe-chat-v1")
    assert "Never edit" not in p["input"]


SEALED = "SEALED-OUTCOME-VALUE-7f3a"
OUTCOMES = {
    "judges": {"contamination_txt": SEALED},
    "score": SEALED,
    "violations": [SEALED],
    "trap_triggered": SEALED,
    "grade": SEALED,
    "judge_labels": SEALED,
}


def test_metadata_fields_and_flags():
    r = rec(summary_truncated=True, source_truncation={"dataset_message": 2}, weird=[1] * 5000)
    p = A.plan_sample(r, "synthetic:s1:0")
    md = p["metadata"]
    for k in ("source", "model", "harness", "trigger", "pre_tokens", "license", "revision"):
        assert md[k] == r[k]
    assert md["rule_candidates"] == r["rules"]
    assert md["flags"] == {"summary_truncated": True, "source_truncation": {"dataset_message": 2}}
    assert md["withheld_fields"] == ["weird"]
    assert md["conversion_notes"] == {}
    json.dumps(md)  # serialisable


def test_outcome_fields_never_reach_metadata():
    r = rec(**OUTCOMES)
    p = A.plan_sample(r, "synthetic:s1:0", meta={"id": "x", "verdict": SEALED, **OUTCOMES})
    dumped = json.dumps(p)
    assert SEALED not in dumped
    assert p["metadata"]["withheld_fields"] == sorted(OUTCOMES)
    assert "verdict" not in p["metadata"]["sidecar"]


def test_check_no_outcomes_refuses_outcome_keys():
    A.check_no_outcomes({"source": "x", "flags": {"usable": True}, "withheld_fields": ["judges"]})
    for bad in ({"score": 1}, {"flags": {"trap_hit": True}}, {"rule_candidates": [{"grade": 1}]}):
        with pytest.raises(ValueError):
            A.check_no_outcomes(bad)


def test_kept_outside_from_sidecar_and_leading_system():
    r = rec(
        post_messages=[
            msg("system", "<system-reminder>\nContents of AGENTS.md\nrules"),
            msg("assistant", "go"),
        ]
    )
    meta = {
        "preserved_messages": [msg("user", "keep this verbatim")],
        "reattached_in_post": ["AGENTS.md"],
        "preserved_verbatim": 1,
    }
    p = A.plan_sample(r, r["id"], meta)
    kept = p["messages"][1]["content"]
    assert "1 message(s) (sidecar meta.jsonl)" in kept
    assert "AGENTS.md" in kept and "Contents of AGENTS.md" in kept
    assert p["messages"][2]["meta"]["part"] == "preserved"
    assert p["messages"][2]["content"] == "keep this verbatim"
    assert p["messages"][3]["content"].startswith("## REPLACED HISTORY")
    assert p["metadata"]["sidecar"]["preserved_verbatim"] == 1
    assert p["metadata"]["n_preserved_messages"] == 1


def test_preserved_tool_calls_are_structured_and_paired():
    pres = [
        msg("assistant", "checking", [("Bash", {"command": "git status"})], ts="2026-10-01T01:00Z"),
        msg("tool", "clean"),
        "not a message",
    ]
    r = rec(preserved_messages=pres)
    p = A.plan_sample(r, r["id"])
    parts = [m["meta"]["part"] for m in p["messages"]]
    assert parts[:4] == ["summary", "kept_outside", "preserved", "preserved"]
    call = p["messages"][2]["tool_calls"][0]
    assert call["function"] == "Bash" and call["arguments"] == {"command": "git status"}
    assert p["messages"][2]["meta"]["ts"] == "2026-10-01T01:00Z"
    assert p["messages"][3]["tool_call_id"] == call["id"]
    assert p["metadata"]["conversion_notes"] == {"preserved: non-object message skipped": 1}
    ids = [c["id"] for m in p["messages"] if m["role"] == "assistant" for c in m["tool_calls"]]
    assert len(ids) == len(set(ids)) == 3  # preserved, source and post calls stay distinct
    pytest.importorskip("inspect_ai")
    from inspect_ai.model import ChatMessageAssistant, ChatMessageTool

    msgs = A.to_inspect(p["messages"])
    assert isinstance(msgs[2], ChatMessageAssistant) and msgs[2].tool_calls[0].function == "Bash"
    assert isinstance(msgs[3], ChatMessageTool) and msgs[3].tool_call_id == call["id"]


def test_kept_outside_without_sidecar_says_not_recorded():
    kept = A.plan_sample(rec(), "x")["messages"][1]["content"]
    assert "not recorded for this source" in kept
    assert "none (the post-compaction part does not start" in kept


# ---------------------------------------------------------------------------- odd fields


def test_odd_fields_do_not_crash_and_are_noted():
    r = {
        "id": "odd:1:0",
        "summary": None,
        "source_messages": [
            "not a dict",
            {"role": "assistant", "text": None, "tool_calls": "oops"},
            {
                "role": "assistant",
                "text": ["a", "b"],
                "tool_calls": [{"args": '{"x": 1}'}, {"name": "T", "args": [1, 2]}, 7],
            },
            {"role": "tool", "text": {"k": 1}},
            {"role": "narrator", "text": "hm"},
        ],
        "post_messages": {"not": "a list"},
        "rules": ["bare string rule", 5],
        "pre_tokens": "lots",
    }
    p = A.plan_sample(r, "odd:1:0")
    notes = p["metadata"]["conversion_notes"]
    assert notes["summary missing"] == 1
    assert notes["source: non-object message skipped"] == 1
    assert notes["source: tool_calls not a list, skipped"] == 1
    assert notes["source: tool call without a name"] == 1
    assert notes["source: tool call args of type list"] == 1
    assert notes["source: non-object tool call skipped"] == 1
    assert notes["source: unknown role 'narrator' shown as user"] == 1
    assert notes["post: not a list (dict), skipped"] == 1
    assert notes["non-object rule skipped"] == 1
    assert notes["pre_tokens not an integer"] == 1
    assert notes["field harness missing"] == 1
    calls = p["messages"][4]["tool_calls"]
    assert calls[0]["arguments"] == {"x": 1}  # JSON-string args parsed
    assert calls[1]["arguments"] == {"value": [1, 2]}
    assert p["metadata"]["rule_candidates"][0]["text"] == "bare string rule"
    assert p["metadata"]["pre_tokens"] is None
    try:
        import inspect_ai  # noqa: F401
    except ImportError:
        return
    A.to_inspect(p["messages"])


def test_string_args_kept_as_input():
    out, notes = conv([msg("assistant", "", [("Bash", "ls -la")]), msg("tool", "x")])
    assert out[0]["tool_calls"][0]["arguments"] == {"input": "ls -la"}
    assert not notes


# ---------------------------------------------------------------------------- sampling


def pool(n_by_source):
    out = []
    for src, n in n_by_source.items():
        for i in range(n):
            out.append(
                {
                    "id": f"{src}:s{i}:0",
                    "source": src,
                    "license": "private" if src == "jacob" else "mit",
                }
            )
    return out


def test_sample_deterministic_and_order_independent():
    recs = pool({"a": 30, "b": 5})
    s1 = [r["id"] for r in A.sample(recs, 10, seed=1)]
    s2 = [r["id"] for r in A.sample(list(reversed(recs)), 10, seed=1)]
    s3 = [r["id"] for r in A.sample(recs, 10, seed=2)]
    assert s1 == s2 and len(set(s1)) == 10
    assert s1 != s3


def test_sample_stratified_even_split_and_redistribution():
    recs = pool({"jacob": 50, "x": 3, "y": 100, "z": 100})
    picks = A.sample(recs, 20, seed=1, strata="source")
    c = collections.Counter(r["source"] for r in picks)
    assert c["x"] == 3 and sum(c.values()) == 20
    assert sorted(c[k] for k in ("jacob", "y", "z")) == [5, 6, 6]
    origin = collections.Counter(
        r["source"] == "jacob" for r in A.sample(recs, 20, seed=3, strata="origin")
    )
    assert origin == {True: 10, False: 10}


def test_sample_n_larger_than_pool_and_duplicate_ids_rejected():
    recs = pool({"a": 4})
    assert len(A.sample(recs, 10, seed=0, strata="source")) == 4
    with pytest.raises(ValueError):
        A.sample(recs + recs[:1], 2, seed=0)


def test_index_skips_non_records_and_assigns_ids(tmp_path):
    f = tmp_path / "x.jsonl"
    lines = [
        json.dumps(rec("s:a:0")),
        "{bad json",
        json.dumps({"id": "inv", "n_post": 3}),
        "",
        json.dumps(rec("s:a:0")),
        json.dumps({k: v for k, v in rec().items() if k != "id"}),
    ]
    f.write_text("\n".join(lines) + "\n")
    entries, info = A.index_file(f, 0)
    assert info["n_records"] == 3
    assert info["skipped"] == {
        "invalid JSON": 1,
        "not a compaction record (no summary or source_messages)": 1,
    }
    notes = A.assign_ids(entries)
    assert [e["uid"] for e in entries] == ["s:a:0@x.jsonl:1", "s:a:0@x.jsonl:5", "x.jsonl:6"]
    assert len(notes) == 3
    assert A.load_record(entries[2])["summary"].startswith("SUMMARY")


def _index(paths):
    entries = []
    for i, p in enumerate(paths):
        entries += A.index_file(p, i)[0]
    A.assign_ids(entries)
    return entries


def test_ids_from_provenance_are_order_independent(tmp_path):
    # "a" twice plus a native "a#2": the old suffix scheme collided here
    f1, f2 = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    f1.write_text(json.dumps(rec("a", summary="first a")) + "\n" + json.dumps(rec("b")) + "\n")
    f2.write_text(json.dumps(rec("a", summary="second a")) + "\n" + json.dumps(rec("a#2")) + "\n")
    fwd, rev = _index([f1, f2]), _index([f2, f1])
    by_uid = lambda es: {e["uid"]: e["line_sha256"] for e in es}
    assert by_uid(fwd) == by_uid(rev)
    assert set(by_uid(fwd)) == {"a@one.jsonl:1", "a@two.jsonl:1", "b", "a#2"}
    for seed in range(5):
        s1 = [(e["uid"], e["line_sha256"]) for e in A.sample(fwd, 2, seed)]
        s2 = [(e["uid"], e["line_sha256"]) for e in A.sample(rev, 2, seed)]
        assert s1 == s2


def test_ids_same_file_name_in_two_dirs_use_full_path(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    for d in ("x", "y"):
        (tmp_path / d / "r.jsonl").write_text(json.dumps(rec("dup")) + "\n")
    uids = [e["uid"] for e in _index([tmp_path / "x" / "r.jsonl", tmp_path / "y" / "r.jsonl"])]
    assert len(set(uids)) == 2 and all(str(tmp_path) in u for u in uids)


def test_sample_ids_injective():
    sids = A.sample_ids(["a/b", "a∕b", "c"])
    assert len(set(sids)) == 3 and sids[0].startswith("001_") and "/" not in "".join(sids)
    with pytest.raises(ValueError):
        A.sample_ids(["a", "a"])


@pytest.mark.parametrize("bad", ["", ",", " , ", "session_id", "source,typo"])
def test_strata_validation(bad):
    with pytest.raises(ValueError):
        A.strata_key(bad)
    with pytest.raises(ValueError):
        A.sample(pool({"a": 3}), 1, 0, strata=bad)


def test_cli_rejects_bad_strata(tmp_path, capsys):
    with pytest.raises(SystemExit):
        A.main(
            [
                "build",
                "--in",
                "x",
                "--n",
                "1",
                "--seed",
                "1",
                "--strata",
                "nope",
                "--out",
                str(A.AUDIT_ROOT / "never-created"),
            ]
        )
    assert not (A.AUDIT_ROOT / "never-created").exists()


# ---------------------------------------------------------------------------- end to end


def test_build_refuses_out_outside_root(tmp_path):
    with pytest.raises(ValueError):
        A.build(
            [str(tmp_path / "x.jsonl")], 1, 1, tmp_path / "elsewhere", audit_root=tmp_path / "audit"
        )


def test_build_end_to_end(tmp_path):
    pytest.importorskip("inspect_ai")
    from inspect_ai.log import read_eval_log

    d = tmp_path / "recs"
    d.mkdir()
    (d / "a.jsonl").write_text(
        "".join(json.dumps(rec(f"swe-chat-v1:s{i}:0")) + "\n" for i in range(6))
    )
    (d / "b.jsonl").write_text(
        "".join(
            json.dumps(rec(f"posttrainbench:run{i}/task:0", source="posttrainbench", **OUTCOMES))
            + "\n"
            for i in range(3)
        )
        + json.dumps({"id": "inventory-row", "has_summary": True})
        + "\n"
    )
    jac = tmp_path / "sess"
    jac.mkdir()
    (jac / "compactions.jsonl").write_text(
        json.dumps(rec("jacob:abc:0", source="jacob", license="private")) + "\n"
    )
    (jac / "meta.jsonl").write_text(
        json.dumps(
            {
                "id": "jacob:abc:0",
                "preserved_messages": [
                    msg("user", "kept"),
                    msg("assistant", "", [("Read", {"file_path": "p.py"})]),
                    msg("tool", "contents"),
                ],
                "reattached_in_post": ["AGENTS.md"],
            }
        )
        + "\n"
    )
    root = tmp_path / "audit"
    out = root / "t1"
    m = A.build(
        [str(d / "*.jsonl"), str(jac / "compactions.jsonl")],
        6,
        7,
        out,
        strata="source",
        audit_root=root,
    )
    assert m["n_sampled"] == 6
    sc = m["strata_counts"]
    assert sc["jacob"] == 1 and sorted([sc["posttrainbench"], sc["swe-chat-v1"]]) == [2, 3]
    assert m["inputs"][1]["skipped"] == {
        "not a compaction record (no summary or source_messages)": 1
    }
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for p in out.rglob("*"):
        if p.is_file():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
    log = read_eval_log(str(out / m["log_file"]))
    assert log.status == "success"
    assert log.eval.metadata["audit"]["seed"] == 7
    assert log.eval.metadata["audit"]["ids"] == m["ids"]
    assert {s.metadata["audit_id"] for s in log.samples} == set(m["ids"])
    ptb = next(s for s in log.samples if s.metadata["source"] == "posttrainbench")
    assert "/" not in str(ptb.id)
    assert "judges" in ptb.metadata["withheld_fields"]
    assert len({s.id for s in log.samples}) == 6
    # outcomes sealed: nowhere in the log (header, samples, events, attachments). Read through
    # Inspect, since its zip may use a compression the stdlib zipfile can't read.
    assert SEALED not in log.model_dump_json()
    assert SEALED not in (out / "manifest.json").read_text()
    jsample = next(s for s in log.samples if s.metadata["source"] == "jacob")
    assert jsample.messages[0].role == "system"
    assert jsample.messages[0].text.startswith("## COMPACTION SUMMARY")
    assert "3 message(s)" in jsample.messages[1].text
    assert jsample.messages[2].text == "kept"
    assert jsample.messages[3].tool_calls[0].function == "Read"
    assert jsample.messages[4].role == "tool"
    assert jsample.messages[4].tool_call_id == jsample.messages[3].tool_calls[0].id
    assert jsample.input.startswith("jacob:abc:0 |")
    # same seed again -> same ids
    m2 = A.build(
        [str(jac / "compactions.jsonl"), str(d / "*.jsonl")],
        6,
        7,
        root / "t2",
        strata="source",
        audit_root=root,
    )
    assert m2["ids"] == m["ids"]
    # only_usable drops records flagged usable=False before sampling
    (d / "c.jsonl").write_text(
        json.dumps(rec("frontierml:bad:0", source="frontierml", usable=False)) + "\n"
    )
    m3 = A.build(
        [str(d / "c.jsonl"), str(d / "a.jsonl")],
        7,
        1,
        root / "t3",
        audit_root=root,
        only_usable=True,
    )
    assert m3["n_sampled"] == 6 and "frontierml:bad:0" not in m3["ids"]
    assert m3["n_records_flagged_unusable"] == 1
    with pytest.raises(FileExistsError):
        A.build([str(d / "a.jsonl")], 1, 1, out, audit_root=root)


# ---------------------------------------------------------------------------- review r2


def _sess_dir(base, name, rid, preserved_text, extra_meta_rows=()):
    d = base / name
    d.mkdir()
    (d / "compactions.jsonl").write_text(
        json.dumps(rec(rid, source="jacob", license="private")) + "\n"
    )
    rows = [{"id": rid, "preserved_messages": [msg("user", preserved_text)]}, *extra_meta_rows]
    (d / "meta.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return d


@pytest.mark.parametrize("reverse", [False, True])
def test_auto_sidecar_follows_its_input_with_duplicate_ids(tmp_path, reverse):
    pytest.importorskip("inspect_ai")
    from inspect_ai.log import read_eval_log

    d1 = _sess_dir(tmp_path, "one", "jacob:same:0", "preserved in one")
    d2 = _sess_dir(tmp_path, "two", "jacob:same:0", "preserved in two")
    ins = [str(d1 / "compactions.jsonl"), str(d2 / "compactions.jsonl")]
    if reverse:
        ins.reverse()
    root = tmp_path / "audit"
    m = A.build(ins, 2, 1, root / "s", audit_root=root)
    log = read_eval_log(str(root / "s" / m["log_file"]))
    assert len(log.samples) == 2
    for s in log.samples:
        where = "one" if f"{tmp_path}/one/" in s.metadata["audit_id"] else "two"
        assert s.messages[2].text == f"preserved in {where}"
        assert s.metadata["conversion_notes"] == {}
    for smp in m["samples"]:
        assert smp["sidecar"].endswith(
            ("one" if f"{tmp_path}/one/" in smp["id"] else "two") + "/meta.jsonl"
        )


def test_explicit_sidecars_rejected_when_ambiguous(tmp_path):
    d1 = _sess_dir(tmp_path, "one", "jacob:same:0", "a")
    d2 = _sess_dir(tmp_path, "two", "jacob:same:0", "b")
    root = tmp_path / "audit"
    ins = [str(d1 / "compactions.jsonl"), str(d2 / "compactions.jsonl")]
    # the same id in two explicit sidecars
    with pytest.raises(ValueError, match="ambiguous explicit sidecar"):
        A.build(
            ins,
            1,
            1,
            root / "x",
            audit_root=root,
            meta_paths=[str(d1 / "meta.jsonl"), str(d2 / "meta.jsonl")],
        )
    # one explicit sidecar whose id belongs to two records
    with pytest.raises(ValueError, match="shared by several records"):
        A.build(ins, 1, 1, root / "y", audit_root=root, meta_paths=[str(d1 / "meta.jsonl")])
    assert not (root / "x").exists() and not (root / "y").exists()


def test_auto_sidecar_with_repeated_row_is_not_attached(tmp_path):
    pytest.importorskip("inspect_ai")
    d = _sess_dir(
        tmp_path,
        "one",
        "jacob:r:0",
        "first",
        extra_meta_rows=[{"id": "jacob:r:0", "preserved_messages": [msg("user", "second")]}],
    )
    root = tmp_path / "audit"
    m = A.build([str(d / "compactions.jsonl")], 1, 1, root / "s", audit_root=root)
    assert m["samples"][0]["sidecar"] is None
    assert m["samples"][0]["conversion_notes"] == {
        "sidecar: not attached: id repeats within the sidecar": 1
    }
    assert m["meta_sidecars"][0]["repeated_ids"] == 1


def test_post_truncation_and_completeness_flags_survive():
    flags = {
        "post_truncated": True,
        "post_truncation": {"dataset_tool_output": 3},
        "post_window_capped": True,
        "boundary_uncertain": True,
        "boundary_uncertainty": {"no_boundary_record": 1},
        "failed_attempts_before": 2,
    }
    r = rec(**flags, native_key="k:1", duplicate_ids=["x"], duplicate_dirs=[])
    md = A.plan_sample(r, r["id"])["metadata"]
    assert md["flags"] == flags
    assert md["provenance"] == {"native_key": "k:1", "duplicate_ids": ["x"], "duplicate_dirs": []}
    assert "withheld_fields" not in md


def test_reread_checks_line_hash(tmp_path):
    f = tmp_path / "r.jsonl"
    f.write_text(json.dumps(rec("s:a:0", summary="AAAA")) + "\n")
    (e,), _ = A.index_file(f, 0)
    assert A.load_record(e)["summary"] == "AAAA"
    f.write_text(json.dumps(rec("s:a:0", summary="BBBB")) + "\n")  # same length, new content
    with pytest.raises(RuntimeError, match="changed since it was indexed"):
        A.load_record(e)


# ---------------------------------------------------------------------------- review r3


def call(name, cid):
    return {"name": name, "args": {}, "id": cid}


def tool(text, tid):
    return {"role": "tool", "text": text, "tool_use_id": tid}


def test_native_ids_pair_reversed_results():
    out, notes = conv_pairing(
        [
            {"role": "assistant", "text": "", "tool_calls": [call("A", "ta"), call("B", "tb")]},
            tool("result B", "tb"),
            tool("result A", "ta"),
        ]
    )
    calls = {c["id"]: c["function"] for c in out[0]["tool_calls"]}
    assert out[1]["function"] == "B" and calls[out[1]["tool_call_id"]] == "B"
    assert out[2]["function"] == "A" and calls[out[2]["tool_call_id"]] == "A"
    assert out[1]["meta"]["tool_use_id"] == "tb"
    assert notes == {"source: paired by id": 2}


@pytest.mark.parametrize("right_align", [False, True])
def test_native_ids_missing_result_is_not_ambiguous(right_align):
    out, notes = conv_pairing(
        [
            {"role": "assistant", "text": "", "tool_calls": [call("A", "ta"), call("B", "tb")]},
            tool("result A", "ta"),
            {"role": "assistant", "text": "next"},
        ],
        right_align,
    )
    assert out[1]["function"] == "A"
    assert notes == {"source: paired by id": 1, "source: tool calls with no result": 1}


def test_native_id_result_after_next_assistant_still_pairs():
    out, notes = conv_pairing(
        [
            {"role": "assistant", "text": "", "tool_calls": [call("A", "ta")]},
            {"role": "assistant", "text": "", "tool_calls": [call("B", "tb")]},
            tool("result B", "tb"),
            {"role": "assistant", "text": "later"},
            tool("result A", "ta"),
        ]
    )
    assert [m.get("function") for m in out if m["role"] == "tool"] == ["B", "A"]
    assert notes == {"source: paired by id": 2}


def test_list_valued_tool_use_id_and_bad_ids():
    out, notes = conv_pairing(
        [
            {"role": "assistant", "text": "", "tool_calls": [call("A", "ta"), call("B", "tb")]},
            tool("both results", ["ta", "tb"]),
            tool("stray", "zz"),
            tool("again", "ta"),
        ]
    )
    assert out[1]["function"] == "A"
    assert out[2]["role"] == "user" and out[2]["content"].startswith(
        "[tool result whose tool_use_id matches no call]"
    )
    assert out[3]["role"] == "user" and out[3]["content"].startswith(
        "[second tool result for one call]"
    )
    assert notes == {
        "source: paired by id": 1,
        "source: one tool message answers several calls, shown under the first": 1,
        "source: tool result whose tool_use_id matches no call": 1,
        "source: second tool result for one call": 1,
    }


def test_mixed_ids_and_positional_fallback():
    out, notes = conv_pairing(
        [
            {
                "role": "assistant",
                "text": "",
                "tool_calls": [call("A", "ta"), {"name": "B", "args": {}}],
            },
            tool("result A", "ta"),
            {"role": "tool", "text": "result B"},
        ]
    )
    assert [m["function"] for m in out[1:]] == ["A", "B"]
    assert notes == {"source: paired by id": 1, "source: paired by position": 1}


def test_repeated_native_call_ids_are_not_used():
    out, notes = conv_pairing(
        [
            {"role": "assistant", "text": "", "tool_calls": [call("A", "dup"), call("B", "dup")]},
            tool("r", "dup"),
        ]
    )
    assert out[1]["role"] == "user"
    assert notes["source: repeated native tool-call id, not used for pairing"] == 1
    assert notes["source: tool result whose tool_use_id is repeated among calls"] == 1
    assert notes["source: tool calls with no result"] == 2


def test_pairing_counts_go_to_metadata_not_notes():
    r = rec(
        post_messages=[
            {"role": "assistant", "text": "", "tool_calls": [call("A", "ta")]},
            tool("ok", "ta"),
        ]
    )
    md = A.plan_sample(r, r["id"])["metadata"]
    assert md["tool_pairing"] == {"source": {"position": 1}, "post": {"id": 1}}
    assert md["conversion_notes"] == {}
