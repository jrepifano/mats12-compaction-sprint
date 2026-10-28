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
    assert calls.count("compaction") == 3  # line and neutral_opening reuse native's
    assert {k: v for k, v in recs["neutral_opening"].items() if k != "arm"} == \
        {k: v for k, v in recs["native"].items() if k != "arm"}
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
    assert len(a) == 150 and len({b.id for b in a}) == 150
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
    assert len(rows) == len(F.ARMS) and all(r["teammate_lost"] is None for r in rows)
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
    lost = {"native": True, "ledger": False, "line": True, "placebo": True,
            "neutral_opening": True}
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
    assert out["fixes"]["neutral_opening"]["complete"]["diff_pts"] == 0
    assert set(out["fixes"]) == {"ledger", "line", "neutral_opening"}


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


def test_focus_on_both_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "FOCUS_CALLS", "both")
    h = _hist(tmp_path)
    calls = []
    recs = {a: F.boundary(F.fix_run("httpx-np-2", a, 1), h, _mf(calls), tmp_path)
            for a in F.ARMS}
    for a in F.ARMS:
        reqs = {r["label"]: r["user"] for r in recs[a]["requests"]}
        assert set(reqs) == {"history", "turn_prefix"}
        has = {k: "Additional focus: " in v for k, v in reqs.items()}
        want = a in ("ledger", "placebo")
        assert has == {"history": want, "turn_prefix": want}, (a, has)
    assert recs["ledger"]["info"]["custom_calls"] == "both"
    assert recs["native"]["info"]["custom_calls"] is None
    assert F.comp_path(tmp_path, F.fix_run("httpx-np-2", "ledger", 1)).name.endswith("-both.json")
    assert not F.comp_path(tmp_path, F.fix_run("httpx-np-2", "native", 1)).name.endswith(
        "-both.json")  # native has no focus: one compaction whatever the mode
    with pytest.raises(ValueError, match="history or both"):
        K.compact("pi", _history(), StubModel(actions=[], summary=_summ, route=ROUTES["stub"]),
                  run_id="x", instruction_text=None, custom_instructions="x", custom_calls="all")


def test_focus_calls_config_key_is_optional_and_checked(monkeypatch):
    from compaction.s2.chain import week2 as W
    monkeypatch.setattr(F, "FOCUS_CALLS", F.FOCUS_CALLS)  # restored whatever happens
    cfg = W.load_config("configs/week2/summary_fix.toml")
    assert cfg["summary_fix"].pop("focus_calls") == "both"  # the bulk's setting (Jacob, 28 Oct)
    assert F.code_mismatches(cfg) == []
    F.apply_design(cfg)
    assert F.FOCUS_CALLS == "history"
    cfg["summary_fix"]["focus_calls"] = "both"
    cfg.pop("_source")
    W.validate(cfg)
    F.apply_design(cfg)
    assert F.FOCUS_CALLS == "both"
    cfg["summary_fix"]["focus_calls"] = "turn_prefix"
    with pytest.raises(ValueError, match="focus_calls"):
        W.validate(cfg)


def test_neutral_opening_changes_only_the_lead_line(tmp_path):
    h = _hist(tmp_path)
    b = F.fix_run("httpx-np-2", "neutral_opening", 1)
    lead = F.lead_for(b, h)
    assert F.lead_for(F.fix_run("httpx-np-2", "native", 1), h) is None
    b_repo = F.CC.REPO_OF_TASK[b.cell.b_task]
    native = F.CC.lead_in("different", b_repo, F.CC.REPOS["httpx"])
    assert lead == native.replace("Thanks, that's done. The next task is in a different "
                                  "project: the ", "Next task: a different project, the ")
    assert lead.startswith("Next task: a different project, the tengo repository")
    comp = {"post_messages": [{"role": "user", "content": "summary"}]}
    hc = SE.AHistory("httpx-np-2", "httpx", tmp_path, None, None, _history(), comp)
    nat = SE.b_context("compacted", "different", b_repo, hc, "TASK", None)
    neu = SE.b_context("compacted", "different", b_repo, hc, "TASK", None, lead=lead)
    assert nat[:-1] == neu[:-1]
    assert nat[-1]["content"] == native + neu[-1]["content"][len(lead):]
    assert nat == SE.b_context("compacted", "different", b_repo, hc, "TASK", None, lead=None)


def test_go_accepts_an_older_smoke_only_when_named():
    import argparse
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "week2_summary_fix_cli", F.CC.REPO_ROOT / "scripts" / "week2_summary_fix.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    from compaction.s2.chain import week2 as W
    config = "configs/week2/summary_fix.toml"
    fp = W.fingerprint(W.load_config(config))
    old = {"exp": "summary_fix", "config_sha256": "ab" * 32, "proposal_sha256": "cd" * 32}
    gates = {"stub_summary_fix": {"pass": True, "fingerprint": fp},
             "smoke_summary_fix": {"pass": True, "fingerprint": old}}
    need = ["stub_summary_fix", "smoke_summary_fix"]

    def refusals(named, g=gates):
        acc = []
        a = argparse.Namespace(config=config, accept_smoke_from=named)
        return cli._common_refusals(a, W.load_config(config), {"gates": g}, need=need,
                                    accepted=acc), acc
    probs, acc = refusals(None)
    assert any("smoke_summary_fix passed under another design" in p for p in probs) and not acc
    probs, acc = refusals("ab" * 6)
    assert probs == [] and acc[0]["gate"] == "smoke_summary_fix" and acc[0]["from"] == old
    assert refusals("ab" * 5)[0] and refusals("ABABABABABAB")[0] and refusals("cd" * 6)[0]
    failed = {**gates, "smoke_summary_fix": {"pass": False, "fingerprint": old}}
    assert refusals("ab" * 6, failed)[0]
    stale_stub = {**gates, "stub_summary_fix": {"pass": True, "fingerprint": old}}
    assert any("stub_summary_fix" in p for p in refusals("ab" * 6, stale_stub)[0])
