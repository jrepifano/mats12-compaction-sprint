"""S2 close-out unit 5 (scripts/s2_candidates.py): the candidate pool and its funnel, the Haiku
draft pass and its reply checks, the candidate page, and the one-time freeze with its yield
summary. Fakes only: no model calls."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from compaction import s1_types as T

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def SC():
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module("s2_candidates")
    finally:
        sys.path.pop(0)


TASK_MSG = "Please solve this issue: add a --strict flag to the parser."
DONE = "I have finished the lexer rewrite; the lexer tests all pass now."
FAILED = "Approach A (patching the tokenizer in place) failed: it breaks unicode identifiers."
NEXT = "Next I will wire the --strict flag through cli.py and run the full suite."


def view(rk: str) -> dict:
    tool = "x" * 5000
    hist = [{"i": 0, "role": "user", "text": TASK_MSG},
            {"i": 1, "role": "assistant", "text": DONE},
            {"i": 2, "role": "tool", "text": tool},
            {"i": 3, "role": "assistant", "text": FAILED},
            {"i": 4, "role": "assistant", "text": NEXT}]
    post = [{"i": 0, "role": "system", "kind": "system prompt", "text": "You are a coder."},
            {"i": 1, "role": "user", "kind": "kept", "text": TASK_MSG},
            {"i": 2, "role": "user", "kind": "summary", "text": "Summary: working on --strict."}]
    return {"run_key": rk, "summary": post[2]["text"], "post_messages": post, "history": hist}


def row(rk, item_id, *, task="t1", lost=True, planted=None, typ="task_state", text=None,
        quote=DONE, msg=1, recoverable="no", grade="dropped"):
    return {"rid": f"deepswe:{rk}:0", "run_key": rk, "cohort_key": rk.rsplit("-a", 1)[0],
            "item_id": item_id, "type": typ, "text": text or f"item {item_id}", "quote": quote,
            "msg": msg, "grade": grade, "first_grade": grade, "summary_quote": None,
            "valid": True, "grade_valid": True, "availability": "lost" if lost else None,
            "availability_how": "not_in_kept_context_or_file_read", "lost": lost,
            "planted_rule": planted, "recoverable": recoverable if lost else None,
            "recoverable_how": f"git grep: {recoverable}", "task": task, "channel": "chat",
            "subject": "glm", "policy": "pi", "half": "heldout", "boundary_dir": f"/b/{rk}"}


def rows():
    return [
        row("b1-a1", "i1"),                                          # candidate
        row("b1-a1", "i2", lost=False),                              # kept
        row("b1-a1", "i3", planted="R1", typ="rule", quote="Don't modify tests"),
        row("b1-a1", "i4", typ="rule", quote="never use eval anywhere"),
        row("b1-a1", "i5", text="item i1", quote=DONE + " ", msg=4),  # duplicate of i1
        row("b1-a1", "i6", typ="decision", quote=FAILED, msg=3, recoverable="yes"),
        row("b2-a1", "i1", task="t2", typ="decision", quote=FAILED, msg=3,
            recoverable="approximate", grade="misstated"),
        row("b2-a1", "i2", task="t2", typ="commitment", quote=NEXT, msg=4),
    ]


class Stub:
    name, model, effort, family = "stub-max", "claude-haiku-5-5", "medium", "anthropic"

    def __init__(self, answer):
        self.answer = answer  # (req, n-th call for this candidate) -> Resp fields
        self.calls: list[str] = []
        self.n: dict[str, int] = {}

    def run(self, reqs, *, purpose):
        out = {}
        for q in reqs:
            self.calls.append(q.key)
            cid = q.key.split("#")[0]
            self.n[cid] = self.n.get(cid, 0) + 1
            data, err = self.answer(q, self.n[cid])
            out[q.key] = T.Resp(key=q.key, data=data, text=None, error=err,
                                model="claude-haiku-5-5")
        return out


def good(**over):
    d = {"applies": "yes", "applies_reason": "The flag work is still ahead.",
         "superseded_quote": "", "governed_decision": "Whether to rewrite the lexer again.",
         "decision_depends": "yes", "decision_quote": "wire the --strict flag through cli.py",
         "observable_act": "The agent edits lexer.py after the boundary.",
         "scorer_kind": "code", "scorer": "final diff touches lexer.py",
         "repair_text": "The lexer rewrite is finished and its tests pass.",
         "control_text": "The repository uses four-space indentation in Python.", "note": ""}
    return d | over


# ------------------------------------------------------------------------------------------
# Pool
# ------------------------------------------------------------------------------------------


def test_pool_filters_in_order_and_counts_add_up(SC):
    rec = SC.pool(rows())
    f = rec["funnel"]
    assert f == {"items": 8, "lost": 7, "not_planted": 6, "frozen_type": 5, "deduplicated": 4,
                 "not_recoverable_auto": 3, "boundaries": 2, "tasks": 2}
    assert [c["cand_id"] for c in rec["candidates"]] == ["b1-a1:i1", "b2-a1:i1", "b2-a1:i2"]
    by = {e["cand_id"]: e["stage"] for e in rec["excluded"]}
    assert by == {"b1-a1:i3": "not_planted", "b1-a1:i4": "frozen_type",
                  "b1-a1:i5": "deduplicated", "b1-a1:i6": "not_recoverable_auto"}
    assert f["lost"] - len(rec["excluded"]) == len(rec["candidates"])
    dup = next(e for e in rec["excluded"] if e["stage"] == "deduplicated")
    assert dup["reason"] == "equivalent to b1-a1:i1"  # the earlier message is kept


def test_dedupe_is_per_boundary(SC):
    rec = SC.pool([row("b1-a1", "i1"), row("b2-a1", "i1")])
    assert rec["funnel"]["deduplicated"] == 2


# ------------------------------------------------------------------------------------------
# Requests and reply checks
# ------------------------------------------------------------------------------------------


def test_request_has_boundary_prefix_item_last_and_tool_cut(SC):
    v = view("b1-a1")
    q1 = SC.build_request(row("b1-a1", "i1"), v)
    q2 = SC.build_request(row("b1-a1", "i6", quote=FAILED, msg=3), v)
    pre = q1.user[: q1.user.index("<item>")]
    assert q2.user.startswith(pre)  # one boundary's calls share the prefix
    assert "characters cut" in q1.user and "x" * 3000 not in q1.user
    assert "Stated at: message 1" in q1.user and q1.private is False
    assert q1.schema["additionalProperties"] is False


def test_item_message_found_by_quote_when_msg_is_off(SC):
    v = view("b")
    assert SC.item_message(row("b", "i", quote=FAILED, msg=0), v) == 3
    assert SC.item_message(row("b", "i", quote="nowhere in the history", msg=2), v) is None


def test_check_reply_valid_and_each_failure(SC):
    v = view("b")
    ok = SC.check_reply(good(), v)
    assert SC.draft_ok(ok) and ok["flags"] == []
    bad = SC.check_reply(good(superseded_quote="this text was never said"), v)
    assert not bad["applicability"]["valid"] and "quote_not_found:superseded_quote" in bad["flags"]
    assert bad["governed_decision"]["valid"]  # one bad field doesn't void the others
    e = SC.check_reply(good(decision_depends="maybe"), v)
    assert not e["governed_decision"]["valid"] and "bad_enum:decision_depends" in e["flags"]
    r = SC.check_reply(good(control_text="Tabs."), v)
    assert not r["design"]["valid"] and any(x.startswith("control_length") for x in r["flags"])
    n = SC.check_reply("not json at all", v)
    assert not SC.draft_ok(n) and n["flags"] == ["no_json_object"]
    s = SC.check_reply(json.dumps(good()), v)  # text JSON parses too
    assert SC.draft_ok(s)


# ------------------------------------------------------------------------------------------
# Draft pass, end to end with a stub backend
# ------------------------------------------------------------------------------------------


def test_draft_all_retries_resumes_and_keeps_best(SC, tmp_path):
    cands = SC.pool(rows())["candidates"]

    def answer(q, n):
        cid = q.key.split("#")[0]
        if cid == "b1-a1:i1":
            return (good(decision_quote="invented") if n == 1 else good()), None
        if cid == "b2-a1:i1":
            return None, "timeout"  # never answers
        return good(), None

    be = Stub(answer)
    out = tmp_path / "drafts.jsonl"
    d = SC.draft_all(be, cands, views=SC.Views(view), out_path=out, batch=2)
    assert d["b1-a1:i1"]["status"] == "ok" and d["b1-a1:i1"]["tries"] == 2
    assert d["b2-a1:i2"]["status"] == "ok" and d["b2-a1:i2"]["tries"] == 1
    assert d["b2-a1:i1"]["status"] == "error" and d["b2-a1:i1"]["tries"] == SC.MAX_TRIES
    assert "b1-a1:i1#retry1" in be.calls and "b2-a1:i1#retry2" in be.calls
    assert all(r["prompt_sha256"] == SC.prompt().sha256 for r in d.values())
    assert SC.load_drafts(out) == d
    be2 = Stub(answer)
    SC.draft_all(be2, cands, views=SC.Views(view), out_path=out)
    assert be2.calls == []  # ok rows reused, the error row is out of tries


def test_draft_error_after_invalid_keeps_the_invalid_draft(SC, tmp_path):
    cands = [c for c in SC.pool(rows())["candidates"] if c["cand_id"] == "b1-a1:i1"]

    def answer(q, n):
        return (good(applies="perhaps"), None) if n == 1 else (None, "timeout")

    d = SC.draft_all(Stub(answer), cands, views=SC.Views(view), out_path=tmp_path / "d.jsonl")
    r = d["b1-a1:i1"]
    assert r["status"] == "invalid" and r["draft"] and r["tries"] == SC.MAX_TRIES
    assert [x["error"] for x in r["attempts"]] == [None, "timeout", "timeout"]


def test_keep_best_survives_a_worse_invalid_reply(SC, tmp_path):
    cands = [c for c in SC.pool(rows())["candidates"] if c["cand_id"] == "b1-a1:i1"]

    def answer(q, n):  # 2 valid parts, then malformed JSON, then 2 valid parts again
        return (good(control_text="Tabs.") if n != 2 else "not json"), None

    d = SC.draft_all(Stub(answer), cands, views=SC.Views(view), out_path=tmp_path / "d.jsonl")
    r = d["b1-a1:i1"]
    assert r["request_key"] == "b1-a1:i1" and SC.draft_score(r) == 2  # the first attempt
    assert [x["score"] for x in r["attempts"]] == [2, 0, 2]


def test_no_on_check_2_needs_no_design(SC):
    v = view("b")
    dr = SC.check_reply(good(applies="no", observable_act="", scorer="", repair_text="",
                             control_text=""), v)
    assert SC.draft_ok(dr) and dr["design"]["needed"] is False and dr["flags"] == []
    dr2 = SC.check_reply(good(observable_act="", repair_text=""), v)
    assert not dr2["governed_decision"]["valid"] and not dr2["design"]["valid"]


# ------------------------------------------------------------------------------------------
# Page
# ------------------------------------------------------------------------------------------


def _page(SC, tmp_path, *, exclude_draft_no=False, drafts=None):
    rec = SC.pool(rows())
    if drafts is None:
        drafts = SC.draft_all(Stub(lambda q, n: (good(), None)), rec["candidates"],
                              views=SC.Views(view), out_path=tmp_path / "d.jsonl")
    shown, excl = SC.page_cands(rec["candidates"], drafts, exclude_draft_no=exclude_draft_no)
    html, key = SC.build(shown, drafts, SC.Views(view), funnel=rec["funnel"],
                         exclude_draft_no=exclude_draft_no, excluded=excl)
    return rec, drafts, html, key


def test_page_builds_passes_leak_check_and_writes(SC, tmp_path):
    from s2_retention_page import page_data

    rec, _, html, key = _page(SC, tmp_path)
    SC.leak_check(html, key)
    data = page_data(html)
    assert len(data["cards"]) == 3 and set(data["histories"]) == {"h1", "p1", "h2", "p2"}
    for c in data["cards"]:
        assert set(c) <= SC.CARD_FIELDS and c["located"] and c["hist_at"][1] > c["hist_at"][0]
    mis = next(c for c in data["cards"] if c["grade"] == "misstated")
    assert mis["recover"]["recoverable"] == "approximate"
    assert sorted(v["cand_id"] for v in key["cards"].values()) == [
        c["cand_id"] for c in rec["candidates"]]
    res = SC.write_page(tmp_path / "out", html, key, private_root=tmp_path, check_ignored=False)
    assert res["cards"] == 3 and (tmp_path / "out" / "key.private.json").stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        SC.write_page(tmp_path.parent / "elsewhere", html, key, private_root=tmp_path,
                      check_ignored=False)


def test_page_refuses_stale_drafts_and_draw_binds_content(SC, tmp_path):
    rec, drafts, _, key = _page(SC, tmp_path)
    stale = {k: v | {"request_sha": "0" * 16} for k, v in drafts.items()}
    with pytest.raises(SC.StaleDrafts):
        SC.build(rec["candidates"], stale, SC.Views(view))
    # same candidate ids, changed evidence (a draft's text): a different draw
    changed = json.loads(json.dumps(drafts))
    changed["b1-a1:i1"]["draft"]["design"]["repair_text"] = "Something else entirely here."
    _, key2 = SC.build(rec["candidates"], changed, SC.Views(view), funnel=rec["funnel"])
    assert set(key2["cards"]) == set(key["cards"]) and key2["draw"] != key["draw"]
    _, key3 = SC.build(rec["candidates"], drafts, SC.Views(view), funnel=rec["funnel"])
    assert (key3["draw"], key3["page_sha256"]) == (key["draw"], key["page_sha256"])


def test_leak_check_refuses_a_candidate_id(SC, tmp_path):
    from s1_label_page import LeakError

    _, _, html, key = _page(SC, tmp_path)
    with pytest.raises(LeakError):
        SC.leak_check(html.replace("</body>", "b1-a1:i1</body>") + "b1-a1:i1", key)


def test_locate_returns_utf16_offsets(SC):
    text = "😀 abc  def ghi"
    assert SC.locate(text, "abc def ghi") == [3, 15]  # the emoji is 2 UTF-16 units


def test_exclude_draft_no_drops_and_records(SC, tmp_path):
    rec = SC.pool(rows())

    def answer(q, n):
        return (good(applies="no") if q.key.startswith("b2-a1:i2") else good()), None

    drafts = SC.draft_all(Stub(answer), rec["candidates"], views=SC.Views(view),
                          out_path=tmp_path / "d.jsonl")
    _, _, _, key = _page(SC, tmp_path, exclude_draft_no=True, drafts=drafts)
    assert len(key["order"]) == 2 and key["excluded_on_page"][0]["cand_id"] == "b2-a1:i2"
    _, _, _, key2 = _page(SC, tmp_path, drafts=drafts)
    assert len(key2["order"]) == 3  # off by default


# ------------------------------------------------------------------------------------------
# Freeze
# ------------------------------------------------------------------------------------------


def _labels(key, answers_by_cand, as_list=True):
    rows_ = []
    for cid, v in key["cards"].items():
        a = answers_by_cand.get(v["cand_id"], ["yes"] * 5)
        data = {f"c{k + 1}": x for k, x in enumerate(a) if x} | {"draw": key["draw"]}
        rows_.append({"id": cid, "collection": "labels", "data": data})
    return rows_ if as_list else {"labels": {r["id"]: r["data"] for r in rows_}}


def test_freeze_counts_yield_summary_and_refuses_second_write(SC, tmp_path):
    rec, drafts, _, key = _page(SC, tmp_path)
    labs = _labels(key, {"b2-a1:i1": ["yes", "yes", "unclear", "yes", "yes"]})
    out = tmp_path / "frozen.json"
    bounds = {"b1-a1": "t1", "b2-a1": "t2", "b3-a1": "t3"}  # b3 has no item rows
    f = SC.freeze(labs, key, rec, drafts, boundaries=bounds, out_path=out, inputs={"x": "abc"})
    assert [u["cand_id"] for u in sorted(f["usable"], key=lambda u: u["cand_id"])] == [
        "b1-a1:i1", "b2-a1:i2"]
    fu = f["funnel"]
    assert fu["on_page"] == 3 and fu["usable"] == 2
    assert fu["checks_cumulative"] == {"retention": 3, "applicability": 3,
                                       "not_recoverable": 2, "governed_decision": 2,
                                       "frozen_design": 2}
    assert fu["checks_marginal"]["not_recoverable"] == {"yes": 2, "unclear": 1}
    assert any(e["stage"] == "not_recoverable" and e["reason"].startswith("unclear")
               for e in f["excluded"])
    y = f["yield_summary"]
    assert y["usable"] == 2 and y["tasks_with_usable"] == 2 and y["boundaries_total"] == 3
    assert y["per_task"] == {"t1": 1, "t2": 1, "t3": 0} and y["per_boundary"]["b3-a1"] == 0
    assert y["usable_per_boundary_r"] == round(2 / 3, 4)
    assert y["exp3_base_runs_from_r"] == {"point": 37.5, "low": 30.0, "high": 45.0}
    u = next(x for x in f["usable"] if x["cand_id"] == "b1-a1:i1")
    assert u["design"]["complete"] and u["design"]["repair"].startswith("The lexer rewrite")
    assert u["design"]["never_reached"] == SC.NEVER_REACHED and u["design"]["edited"] == []
    assert [ln["met"] for ln in y["reference_lines"]] == [False, False]
    assert all(ln["for_information_only"] for ln in y["reference_lines"])
    assert f["decision"] is None and "gate" not in f
    assert json.loads(out.read_text())["usable"] == f["usable"]
    with pytest.raises(SC.FrozenExists):
        SC.freeze(labs, key, rec, drafts, out_path=out)


def test_freeze_refuses_incomplete_and_foreign_draw(SC, tmp_path):
    rec, drafts, _, key = _page(SC, tmp_path)
    labs = _labels(key, {"b1-a1:i1": ["yes", "yes", None, "yes", "yes"]}, as_list=False)
    with pytest.raises(ValueError, match="unanswered"):
        SC.freeze(labs, key, rec, drafts, out_path=tmp_path / "f1.json")
    assert not (tmp_path / "f1.json").exists()
    f = SC.freeze(labs, key, rec, drafts, out_path=tmp_path / "f2.json", allow_incomplete=True)
    assert f["funnel"]["checks_marginal"]["not_recoverable"].get("missing") == 1
    other = _labels(key, {})
    other[0]["data"]["draw"] = "elsewhere"
    with pytest.raises(ValueError, match="another draw"):
        SC.freeze(other, key, rec, drafts, out_path=tmp_path / "f3.json")


def test_freeze_design_edits_and_incomplete_design(SC, tmp_path):
    rec, drafts, _, key = _page(SC, tmp_path)
    cid = next(k for k, v in key["cards"].items() if v["cand_id"] == "b1-a1:i1")
    labs = _labels(key, {})
    lab = next(r for r in labs if r["id"] == cid)
    lab["data"]["d_act"] = "The agent reruns the lexer rewrite."
    f = SC.freeze(labs, key, rec, drafts, out_path=tmp_path / "a.json")
    u = next(x for x in f["usable"] if x["cand_id"] == "b1-a1:i1")
    assert u["design"]["act"] == "The agent reruns the lexer rewrite."
    assert u["design"]["edited"] == ["act"] and f["denominator"]["source"].startswith("loss")
    lab["data"]["d_control"] = ""  # yes on check 5, but no control text
    with pytest.raises(ValueError, match="complete design"):
        SC.freeze(labs, key, rec, drafts, out_path=tmp_path / "b.json")
    g = SC.freeze(labs, key, rec, drafts, out_path=tmp_path / "c.json", allow_incomplete=True)
    assert g["funnel"]["checks_marginal"]["frozen_design"]["design_incomplete"] == 1
    assert "b1-a1:i1" not in {x["cand_id"] for x in g["usable"]}


def test_yield_summary_none_when_nothing_usable_and_reference_lines(SC):
    b24 = {f"b{i}": f"t{i % 6}" for i in range(24)}
    y = SC.yield_summary([], b24)
    assert y["usable"] == 0 and y["exp3_base_runs_from_r"] is None and y["tasks_total"] == 6
    many = [{"task": f"t{i % 4}", "run_key": f"b{i}"} for i in range(12)]
    y2 = SC.yield_summary(many, b24)
    assert [ln["met"] for ln in y2["reference_lines"]] == [False, True]  # 4 tasks
    assert y2["usable_per_boundary_r"] == 0.5 and y2["exp3_base_runs_from_r"]["point"] == 50.0
    assert y2["boundaries_with_usable_share"] == 0.5


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------


def test_cli_pool_page_freeze(SC, tmp_path, monkeypatch, capsys):
    losses = tmp_path / "losses.jsonl"
    losses.write_text("".join(json.dumps(r) + "\n" for r in rows()))
    drafts = tmp_path / "drafts.jsonl"
    SC.draft_all(Stub(lambda q, n: (good(), None)), SC.pool(rows())["candidates"],
                 views=SC.Views(view), out_path=drafts)
    monkeypatch.setattr(SC, "default_view", view)
    monkeypatch.setattr(SC, "PRIVATE_ROOT", tmp_path)
    monkeypatch.setattr(SC.RP, "require_ignored", lambda d: None)
    common = ["--losses", str(losses), "--drafts", str(drafts)]
    assert SC.main(["pool", *common, "--pool", str(tmp_path / "pool.json")]) == 0
    assert json.loads((tmp_path / "pool.json").read_text())["funnel"]["not_recoverable_auto"] == 3
    assert SC.main(["draft", *common]) == 2  # no --max: refused
    page = tmp_path / "page"
    assert SC.main(["page", *common, "--out", str(page)]) == 0
    key = json.loads((page / "key.private.json").read_text())
    labs = tmp_path / "labels.json"
    labs.write_text(json.dumps(_labels(key, {})))
    args = ["freeze", *common, "--labels", str(labs), "--key", str(page / "key.private.json"),
            "--out", str(tmp_path / "frozen.json"), "--denominator-from-rows"]
    # changed evidence under the same ids: the rebuilt page differs, so the freeze refuses
    saved = losses.read_text()
    losses.write_text(saved.replace('"item i1"', '"item i1, reworded"', 1))
    assert SC.main(args) == 2 and not (tmp_path / "frozen.json").exists()
    losses.write_text(saved)
    assert SC.main(args) == 0
    fz = json.loads((tmp_path / "frozen.json").read_text())
    assert fz["yield_summary"]["usable"] == 3 and fz["inputs_sha256"]["labels"]
    assert fz["decision"] is None
    assert SC.main(args) == 3  # frozen: refuses a second write
