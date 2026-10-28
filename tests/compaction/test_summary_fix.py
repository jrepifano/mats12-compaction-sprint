"""Experiment 5 (summary fix, pi): the boundary arms, the launch order and the rows, without
containers (the container flow is stub_summary_fix)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compaction.s2 import compact as K
from compaction.s2 import loop as L
from compaction.s2.chain import session as SE
from compaction.s2.chain import summary_fix as F
from compaction.s2.config import ROUTES
from compaction.s2.model import StubModel, tool_call


def _history(n_rounds=3) -> list[dict]:
    """A first task long enough that pi's tail starts after the last check-in."""
    m = [{"role": "system", "content": L.SYSTEM_PROMPT}, {"role": "user", "content": "Do it."}]
    for k in range(n_rounds):
        if k:
            m.append({"role": "user", "content": "How is it going?"})
        for i in range(24):
            cid = f"c{k}_{i}"
            m += [{"role": "assistant", "content": "",
                   "tool_calls": [tool_call("bash", {"command": f"ls part{i}"}, cid)]},
                  {"role": "tool", "tool_call_id": cid,
                   "content": "\n".join(f"-rw-r--r-- 1 root root {j} f_{i}_{j}" for j in range(90))}]
    return m


def _summ(messages):
    u = messages[-1]["content"]
    return "<summary>" + ("FOCUS" if "Additional focus: " in u else "PLAIN") + "</summary>"


def test_custom_instructions_go_to_pis_history_call_only():
    msgs = _history()
    m = StubModel(actions=[], summary=_summ, route=ROUTES["stub"])
    plain = K.compact("pi", msgs, m, run_id="x", instruction_text=None)
    focus = K.compact("pi", msgs, m, run_id="x", instruction_text=None,
                      custom_instructions="Who authorized it.")
    labels = [r["label"] for r in plain.requests]
    assert "history" in labels
    for a, b in zip(plain.requests, focus.requests, strict=True):
        if a["label"] == "history":
            assert b["user"] == a["user"] + "\n\nAdditional focus: Who authorized it."
        else:
            assert b["user"] == a["user"]
    assert focus.info["custom_instructions"] == "Who authorized it."
    with pytest.raises(ValueError, match="pi only"):
        K.compact("codex_local", msgs, m, run_id="x", instruction_text=None,
                  custom_instructions="x")


def _hist(tmp_path: Path) -> SE.AHistory:
    return SE.AHistory("httpx-np-2", "httpx", tmp_path, None, None, _history(), None)


def _mf(calls):
    def model_for(kind, out):
        calls.append(kind)
        return StubModel(actions=[], summary=_summ, route=ROUTES["stub"])
    return model_for


def test_boundary_arms(tmp_path):
    calls = []
    h = _hist(tmp_path)
    recs = {a: F.boundary(F.fix_run("httpx-np-2", a, 1), h, _mf(calls), tmp_path)
            for a in F.ARMS}
    assert calls.count("compaction") == 3  # line reuses native's
    assert "PLAIN" in recs["native"]["summary"] and "FOCUS" in recs["ledger"]["summary"]
    assert "FOCUS" in recs["placebo"]["summary"]
    led = next(r for r in recs["ledger"]["requests"] if r["label"] == "history")["user"]
    assert led.endswith("Additional focus: " + F.text(F.FOCUS["ledger"]))
    i = recs["native"]["summary_index"]
    assert recs["line"]["post_messages"][i]["content"] == \
        recs["native"]["post_messages"][i]["content"] + "\n\n" + F.text(F.LINE)
    others = [j for j in range(len(recs["native"]["post_messages"])) if j != i]
    assert all(recs["line"]["post_messages"][j] == recs["native"]["post_messages"][j]
               for j in others)
    # the native file on disk is untouched by the line arm
    on_disk = json.loads(F.comp_path(tmp_path, F.fix_run("httpx-np-2", "native", 1)).read_text())
    assert on_disk["post_messages"][i] == recs["native"]["post_messages"][i]
    # cached: a second call doesn't summarize again
    F.boundary(F.fix_run("httpx-np-2", "ledger", 1), h, _mf(calls), tmp_path)
    assert calls.count("compaction") == 3
    s = F.compaction_summary(recs["ledger"])
    assert s["usage"] and s["custom_sha256"] and s["summary_chars"] > 0


def test_paid_model_tuple_and_empty_summary_raises(tmp_path):
    h = _hist(tmp_path)

    def tup(kind, out):
        return StubModel(actions=[], summary=lambda m: "", route=ROUTES["stub"]), None
    with pytest.raises(Exception, match="empty summary"):
        F.boundary(F.fix_run("httpx-np-2", "ledger", 1), h, tup, tmp_path)
    assert not F.comp_path(tmp_path, F.fix_run("httpx-np-2", "ledger", 1)).exists()


def test_order_is_seeded_replicate_major_and_complete():
    a = F.order(20261029, F.HISTORIES, 3)
    assert a == F.order(20261029, F.HISTORIES, 3) and a != F.order(1, F.HISTORIES, 3)
    assert len(a) == 120 and len({b.id for b in a}) == 120
    assert [b.replicate for b in a] == sorted(b.replicate for b in a)
    for r in (1, 2, 3):
        for h in F.HISTORIES:
            assert sorted(b.arm for b in a if b.replicate == r and b.history == h) == \
                sorted(F.ARMS)
    b = a[0]
    assert b.cell.project == "different" and b.cell.start == "compacted"
    assert b.cell.b_task == F.CC.other_repo(b.history.split("-")[0]).b_task
    assert F.order(20261029, F.HISTORIES, 2) == [x for x in a if x.replicate <= 2]


def test_rows_keep_every_slot(tmp_path):
    runs = F.order(1, ("httpx-np-2",), 1)
    st = {"b": {runs[0].id: {"state": "compaction failed",
                             "compaction_failures": [{"error": "empty summary"}]}}}
    rows = [json.loads(x) for x in F.write_rows(st, runs, tmp_path).read_text().splitlines()]
    assert len(rows) == 4 and all(r["teammate_lost"] is None for r in rows)
    assert rows[0]["state"] == "compaction failed" and rows[0]["compaction_failures"]


def test_a_failed_compaction_is_final_and_shared_with_line(tmp_path):
    h = _hist(tmp_path)
    calls = []

    def empty(kind, out):
        calls.append(kind)
        return StubModel(actions=[], summary=lambda m: "", route=ROUTES["stub"])
    nat = F.fix_run("httpx-np-2", "native", 1)
    with pytest.raises(F.CompactionFailed, match="empty summary"):
        F.boundary(nat, h, empty, tmp_path)
    assert F.failed_path(F.comp_path(tmp_path, nat)).exists() and F.compaction_cached(tmp_path,
                                                                                         nat)
    rec = json.loads(F.failed_path(F.comp_path(tmp_path, nat)).read_text())
    assert rec["usage"] and all(u["finish_reason"] for u in rec["usage"]) and rec["requests"]
    n = len(calls)
    for arm in ("native", "line"):  # neither retries: final, and line needs native's
        with pytest.raises(F.CompactionFailed):
            F.boundary(F.fix_run("httpx-np-2", arm, 1), h, _mf(calls), tmp_path)
    assert len(calls) == n


def test_the_budget_gate_comes_before_a_paid_summary(tmp_path, monkeypatch):
    b = F.fix_run("httpx-np-2", "ledger", 1)
    calls = []
    monkeypatch.setattr(F.M, "load_snapshot", lambda root, hid: _hist(tmp_path))
    monkeypatch.setattr(F.D, "save_state", lambda st: None)
    monkeypatch.setattr(F.D, "stop_requested", lambda: False)
    st = {"b": {}}
    rec = F.run_one(b, st, None, None, {b.cell.b_task: object()}, tmp_path,
                    model_for=_mf(calls), admit=lambda: False)
    assert rec["state"] == "held: cap" and calls == []  # no summary bought
    assert not F.compaction_cached(tmp_path, b)


def test_rows_to_primary_with_replicates_and_unknowns(tmp_path, monkeypatch):
    from compaction.s2.chain import summary_fix_stats as FS
    runs = F.order(7, F.HISTORIES[:3], 2)
    st = {"b": {}}
    for i, b in enumerate(runs):
        if i % 7 == 3:
            st["b"][b.id] = {"state": "compaction failed", "compaction_failures": [{"e": 1}]}
            continue
        st["b"][b.id] = {"state": "done", "dir": str(tmp_path / b.id)}
    lost = {"native": True, "ledger": False, "line": True, "placebo": True}
    monkeypatch.setattr(F, "score", lambda out, b: {"id": b.id, "rep": 2, "rewrite": "no",
                                                     "teammate_lost": lost[b.arm],
                                                     "arm": b.arm, "replicate": b.replicate,
                                                     "unit": b.unit})
    rows = [json.loads(x) for x in F.write_rows(st, runs, tmp_path).read_text().splitlines()]
    assert len(rows) == len(runs) and {r["replicate"] for r in rows} == {1, 2}
    assert sum(r["teammate_lost"] is None for r in rows) == sum(i % 7 == 3 for i in range(len(runs)))
    out = FS.primary(rows, n=200)
    assert out["fixes"]["ledger"]["complete"]["diff_pts"] < 0
    assert out["risk"]["native"]["assigned"] == 6


def test_content_works_needs_the_native_contrast():
    from compaction.s2.chain import summary_fix_stats as FS
    rows = [{"history": f"h{i}", "replicate": r, "arm": a,
             "teammate_lost": a == "placebo"}
            for i in range(10) for r in (1, 2) for a in ("native", "ledger", "line", "placebo")]
    out = FS.primary(rows, n=200)
    lp = out["ledger_vs_placebo"]
    assert lp["secondary_criterion_met"] and not lp["content_works"]  # ledger = native


def test_empty_custom_instructions_are_none_and_the_request_hash_follows_the_prompt():
    msgs = _history()
    m = StubModel(actions=[], summary=_summ, route=ROUTES["stub"])
    plain = K.compact("pi", msgs, m, run_id="x", instruction_text=None)
    empty = K.compact("pi", msgs, m, run_id="x", instruction_text=None, custom_instructions="")
    focus = K.compact("pi", msgs, m, run_id="x", instruction_text=None,
                      custom_instructions="Who authorized it.")
    assert empty.requests == plain.requests
    assert empty.info["request_sha256"] == plain.info["request_sha256"]
    assert focus.info["request_sha256"] != plain.info["request_sha256"]
