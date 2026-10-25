"""scripts/s2_constraint_decay.py (D): items, cached Opus answers and the table, with a stub
backend (no calls)."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from compaction import s1_types as T


@pytest.fixture(scope="module")
def D():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        return importlib.import_module("s2_constraint_decay")
    finally:
        sys.path.pop(0)


@pytest.fixture(autouse=True)
def _own_opus_lock(tmp_path, monkeypatch):
    from compaction.s2 import interact_reqs as RQ
    monkeypatch.setattr(RQ, "OPUS_LOCK_FILE", tmp_path / "opus.lock")


class Stub:
    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def run(self, reqs, *, purpose):
        out = {}
        for r in reqs:
            self.seen.append(r)
            d = self.answer(r)
            out[r.key] = T.Resp(r.key, d, json.dumps(d), None, model="stub-opus")
        return out


TASK = "Please solve this issue: add bail. Do not modify the existing tests. Commit when done."


def test_collect_grades_in_play_rules_per_source_and_caches(D, tmp_path, monkeypatch):
    run = tmp_path / "t-deepseek-a1"
    monkeypatch.setattr(D, "runs", lambda root=None: [(run, [1, 2])])
    monkeypatch.setattr(D, "task_message", lambda bdir: TASK)
    items = {1: [{"req_id": "b1-q1", "text": "Never touch reporter.js", "quote": "never touch "
                  "reporter.js"}],
             2: [{"req_id": "b1-q1", "text": "Never touch reporter.js", "quote": "never touch "
                  "reporter.js"},
                 {"req_id": "b2-q1", "text": "Return 409", "quote": "return 409"}]}
    monkeypatch.setattr(D, "maintainer_items", lambda bdir: items[int(bdir.name[1:])])
    monkeypatch.setattr(D.S, "source_view", lambda bdir, src: {"post": [
        {"role": "user", "content": f"summary {src} {bdir.name}"}]})
    monkeypatch.setattr(D, "SOURCES", ("live", "cc_prompt"))

    def answer(req):
        kind = req.key.split("|")[0]
        if kind == "decay-extract":
            return {"rules": [{"quote": "Do not modify the existing tests.", "rule": "no test "
                               "edits"}, {"quote": "not in the message", "rule": "bogus"}]}
        if kind == "decay-classify":
            return {"items": [{"id": "b1-q1", "process_rule": "yes", "reason": "scope"},
                              {"id": "b2-q1", "process_rule": "no", "reason": "code"}]}
        ids = json.loads(req.user.split("<rules>\n")[1].split("\n</rules>")[0])
        k = int(req.key.split("|")[2][1:])
        return {"grades": [{"id": x["id"], "retention": "kept" if k == 1 else "lost",
                            "closest_quote": None, "reason": "r"} for x in ids]}
    be = Stub(answer)
    out = D.collect(be, cache=tmp_path / "cache")
    r = out["runs"]["t-deepseek-a1"]
    assert [x["quote_found"] for x in r["task_rules"]] == [True, False]
    assert [x["id"] for x in r["maintainer_process_rules"]] == ["b1-q1"]
    # in play: the found task rule always, b1-q1 from b1 on; b2-q1 isn't a process rule;
    # replays only at k=1 (no mixed-method chains)
    assert {(x["k"], x["source"], x["id"]) for x in out["rows"]} == {
        (k, s, i) for k, s in ((1, "live"), (1, "cc_prompt"), (2, "live"))
        for i in ("task-1", "b1-q1")}
    t = {(x["source"], x["k"], x["origin"]): x for x in D.table(out["rows"])}
    assert t[("live", 1, "all")]["kept_share"] == 1.0 and t[("live", 2, "all")]["kept"] == 0
    assert t[("live", 2, "task")]["n"] == 1 and t[("live", 2, "all")]["kept_ci"][1] < 0.85
    n = len(be.seen)
    D.collect(be, cache=tmp_path / "cache")
    assert len(be.seen) == n  # every answer cached
    md = D.markdown(dict(out, table=D.table(out["rows"]), created_utc="t", grader="stub"))
    assert "| live | 1 | all |" in md and "split by reply |" in md
    assert t[("live", 1, "maintainer")]["by_reply"] == 0  # the fixture's items: one call
    by = [dict(x, extraction="by_reply") if x["origin"] == "maintainer" else x
          for x in out["rows"]]
    assert {(x["source"], x["k"], x["origin"]): x["by_reply"] for x in D.table(by)}[
        ("live", 1, "all")] == 1
    # two series, never one trend line: the live row, and the replays on pi's summaries
    own, k1 = md.split("## k=1 only: summarizers compared on identical history")
    assert "| live |" in own and "| cc_prompt |" not in own and "| cc_prompt | 1 |" in k1
    assert "| cc_prompt | 2 |" not in md
    # a cached replay row at k>1 never reaches the table
    stale = [dict(out["rows"][0], source="cc_prompt", k=2)]
    assert D.table(stale) == []
    assert {r["series"] for r in out["rows"] if r["source"] == "live"} == {"own_repeated"}


def test_ungraded_rows_count_as_missing_not_kept(D):
    rows = [{"run": "r", "k": 1, "source": "live", "id": "a", "origin": "task",
             "retention": None},
            {"run": "r", "k": 1, "source": "live", "id": "b", "origin": "task",
             "retention": "kept"}]
    (t,) = [x for x in D.table(rows) if x["origin"] == "all"]
    assert (t["n"], t["missing"], t["kept_share"]) == (2, 1, 0.5)  # missing = not kept


def _setup(D, tmp_path, monkeypatch, items):
    run = tmp_path / "t-deepseek-a1"
    monkeypatch.setattr(D, "runs", lambda root=None: [(run, sorted(items))])
    monkeypatch.setattr(D, "task_message", lambda bdir: TASK)
    monkeypatch.setattr(D, "maintainer_items", lambda bdir: items[int(bdir.name[1:])])
    monkeypatch.setattr(D.S, "source_view", lambda bdir, src: {"post": [
        {"role": "user", "content": f"summary {src} {bdir.name}"}]})
    monkeypatch.setattr(D, "SOURCES", ("live",))

    def answer(req):
        kind = req.key.split("|")[0]
        if kind == "decay-extract":
            return {"rules": []}
        if kind == "decay-classify":
            return {"items": [{"id": x["id"], "process_rule": "yes", "reason": "r"} for x in
                              json.loads(req.user.split("<requirements>\n")[1]
                                         .split("\n</requirements>")[0])]}
        ids = json.loads(req.user.split("<rules>\n")[1].split("\n</rules>")[0])
        return {"grades": [{"id": x["id"], "retention": "lost", "closest_quote": None,
                            "reason": "r"} for x in ids]}
    return Stub(answer)


def test_rules_carry_forward_from_their_first_appearance_never_earlier(D, tmp_path,
                                                                        monkeypatch):
    """chain-decay r1 #1: a rule whose reply a compaction removed stays in play; a later rule
    is never in play before it appears."""
    it = lambda rid: {"req_id": rid, "text": f"rule {rid}", "quote": f"q {rid}"}
    be = _setup(D, tmp_path, monkeypatch, {1: [it("b1-q1")], 2: [it("b2-q1")]})
    out = D.collect(be, root=tmp_path, cache=tmp_path / "cache")
    assert {(r["k"], r["id"]) for r in out["rows"]} == {(1, "b1-q1"), (2, "b1-q1"),
                                                         (2, "b2-q1")}
    assert {r["id"]: r["introduced_k"] for r in out["rows"]} == {"b1-q1": 1, "b2-q1": 2}


def test_cache_answers_only_the_same_request_model_and_root(D, tmp_path, monkeypatch):
    """chain-decay r1 #2: a changed request, model or root asks again."""
    it = lambda rid: {"req_id": rid, "text": f"rule {rid}", "quote": f"q {rid}"}
    be = _setup(D, tmp_path, monkeypatch, {1: [it("b1-q1")]})
    cache = tmp_path / "cache"
    D.collect(be, root=tmp_path, cache=cache)
    n = len(be.seen)
    D.collect(be, root=tmp_path, cache=cache)
    assert len(be.seen) == n  # same everything: cached
    be.model = "another-model"
    D.collect(be, root=tmp_path, cache=cache)
    assert len(be.seen) == 2 * n
    other = tmp_path / "other-root"
    other.mkdir()
    D.collect(be, root=other, cache=cache)
    assert len(be.seen) == 3 * n
    monkeypatch.setattr(D, "TYPE_DEF", D.TYPE_DEF + " (edited)")
    D.collect(be, root=other, cache=cache)
    assert len(be.seen) > 3 * n  # a changed prompt asks again


def test_table_only_rebuilds_from_the_cache_without_calls(D, tmp_path, monkeypatch):
    """chain-decay r2: --table-only uses the real grader's model id, so a cache written by that
    model is valid; anything not cached raises instead of calling."""
    from compaction.s2 import interact_reqs as RQ
    it = lambda rid: {"req_id": rid, "text": f"rule {rid}", "quote": f"q {rid}"}
    be = _setup(D, tmp_path, monkeypatch, {1: [it("b1-q1")], 2: [it("b2-q1")]})
    be.model = RQ.OPUS_MODEL
    cache = tmp_path / "cache"
    first = D.collect(be, root=tmp_path, cache=cache)
    again = D.collect(D._NoCalls(), root=tmp_path, cache=cache)
    assert again["rows"] == first["rows"]
    with pytest.raises(RuntimeError, match="not cached"):
        D.collect(D._NoCalls(), root=tmp_path, cache=tmp_path / "empty")
