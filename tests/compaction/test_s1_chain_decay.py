"""Stub-backend tests for scripts/s1_chain_decay.py (no Max calls)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import s1_chain_decay as CD

from compaction import s1_types as T

SG = "2026-01-05-aaaa"
CONT = CD.CONTINUED + ". The summary below covers the earlier portion of the conversation.\n\n"
S1 = "Never push to main without asking. I will add tests after the refactor."
S2 = "Work so far: Use uv, not pip for installs."
S3 = "Never push to main without asking. Use uv, not pip for installs."


def _rec(n: int, msgs: list[dict], summary: str, ts: str) -> dict:
    return {
        "id": f"swe-chat-v1:{SG}:{n}",
        "source": "swe-chat-v1",
        "session_id": SG,
        "harness": "claude-code",
        "model": "claude-opus-4-6",
        "timestamp": ts,
        "source_messages": msgs,
        "summary": summary,
        "post_messages": [],
        "usable": True,
        "summary_truncated": False,
        "meta": None,
        "history_complete": True,
    }


def _records() -> dict[str, dict]:
    r1 = _rec(
        0,
        [
            {"role": "user", "text": "Never push to main without asking."},
            {"role": "assistant", "text": "I will add tests after the refactor."},
        ],
        S1,
        "2026-01-05T10:00:00Z",
    )
    r2 = _rec(
        1,
        [
            {"role": "system", "text": CONT + S1},
            {"role": "user", "text": "Use uv, not pip for installs."},
            {"role": "user", "text": "Remember: never push to main without asking."},
        ],
        S2,
        "2026-01-05T11:00:00Z",
    )
    r3 = _rec(
        2,
        [
            {"role": "system", "text": CONT + S2},
            {"role": "assistant", "text": "Added the tests after the refactor, as promised."},
        ],
        S3,
        "2026-01-05T12:00:00Z",
    )
    return {r["id"]: r for r in (r1, r2, r3)}


def _item(type_, text, quote, msg, origin="user"):
    return {
        "type": type_,
        "text": text,
        "quote": quote,
        "msg": msg,
        "origin": origin,
        "permission_status": None,
        "superseded": False,
        "superseded_quote": None,
    }


class Stub:
    name, model, family = "haiku-max", "stub", "anthropic"
    context_tokens, bytes_per_token = 1_000_000, 2.2

    def __init__(self):
        self.calls: list[str] = []

    def _data(self, r: T.Req) -> dict:
        k = r.key
        if k.startswith("p1a|") and k.split("|")[1].endswith(":0"):
            return {
                "items": [
                    _item("rule", "Never push to main without asking.",
                          "Never push to main without asking", 0),
                    _item("commitment", "Add tests after the refactor.",
                          "I will add tests after the refactor", 1, "assistant"),
                ]
            }  # fmt: skip
        if k.startswith("p1a|"):
            return {
                "items": [
                    _item("rule", "Summary rule.", "Never push to main without asking", 0),
                    _item("rule", "Use uv.", "Use uv, not pip for installs", 1),
                    _item("rule", "No push.", "never push to main without asking", 2),
                ]
            }
        if k.startswith("p1b|"):
            summary = re.search(r"<summary>\n(.*?)\n</summary>", r.user, re.DOTALL).group(1)
            grades = []
            for line in r.user.splitlines():
                if line.startswith('{"id": "i'):
                    x = json.loads(line)
                    if T.quote_in(x["quote"], summary):
                        grades.append({"item": x["id"], "verdict": "kept",
                                       "summary_quote": x["quote"], "note": "ok"})  # fmt: skip
                    else:
                        grades.append({"item": x["id"], "verdict": "dropped",
                                       "summary_quote": None, "note": "absent"})  # fmt: skip
            return {"grades": grades}
        if k.startswith("scan|") and k.endswith("|j2"):
            return {
                "restated": [{"item": "c1:i0", "msg": 2,
                              "quote": "never push to main without asking"}],
                "not_current": [{"item": "c1:i9", "msg": 1, "quote": "Use uv, not pip"}],
                "same_as": [{"candidate": "c2:i2", "item": "c1:i0"},
                            {"candidate": "c2:i1", "item": None}],
            }  # fmt: skip
        if k.startswith("scan|"):
            return {
                "restated": [],
                "not_current": [{"item": "c1:i1", "msg": 1,
                                 "quote": "Added the tests after the refactor"}],
                "same_as": [],
            }  # fmt: skip
        if k.startswith("sl|item|"):
            return {"verdict": "dropped", "summary_quote": None, "searched": [], "note": "no"}
        raise AssertionError(k)

    def run(self, reqs, *, purpose):
        self.calls += [r.key for r in reqs]
        return {r.key: T.Resp(r.key, self._data(r), None, None, meta={}) for r in reqs}


def _chain() -> dict:
    recs = _records()
    return {"group": f"swe-chat-v1:{SG}", "stratum": "claude-code | claude-opus-4-6",
            "rids": list(recs), "n_total": 3}  # fmt: skip


@pytest.fixture
def ran():
    recs = _records()
    be = Stub()
    res = CD.run_chains([_chain()], be, load=lambda rid: recs[rid])
    scans = {st.chain["group"]: st.scans for st in res["states"]}
    return be, res, CD.annotate(res["rows"], scans)


def _cell(rows, item, k):
    (r,) = [r for r in rows if r["chain_item"] == item and r["k"] == k]
    return r


def test_chains_follow_carried_summaries_and_skip_continued_starts():
    recs = list(_records().values())
    el = {r["id"] for r in recs}
    (c,) = CD.chains(recs, el)
    assert c["rids"] == [r["id"] for r in recs]
    assert CD.chains(recs, el - {recs[1]["id"]}) == []  # record 2 must be eligible
    broken = [dict(recs[0]), dict(recs[1], source_messages=[{"role": "user", "text": "x"}]),
              dict(recs[2])]  # fmt: skip
    assert CD.chains(broken, el) == []  # a broken link leaves a chain of 1
    cont = [dict(recs[0], source_messages=[{"role": "system", "text": CONT + "x"}])] + recs[1:]
    assert CD.chains(cont, el) == []
    assert CD.chains(recs, el, k_cap=2) == []  # under MIN_K once capped


def test_allocation_is_proportional_and_the_draw_seeded():
    assert CD.allocate({"a": 38, "b": 9, "c": 4, "d": 2, "e": 1}, 40) == {
        "a": 28, "b": 7, "c": 3, "d": 1, "e": 1}  # fmt: skip
    pool = [{"group": f"g{i}", "stratum": "a" if i < 30 else "b"} for i in range(40)]
    one, d1 = CD.draw(pool, 10)
    two, _ = CD.draw(pool, 10)
    assert one == two and len(one) == 10
    assert d1["a"]["sample"] == 8 and d1["b"]["sample"] == 2
    assert CD.draw(pool, 10, seed="other")[0] != one


def test_check_scan_verifies_quotes_and_ids():
    rec = _records()[f"swe-chat-v1:{SG}:1"]
    items = {"c1:i0": {}}
    resp = T.Resp("k", {
        "restated": [
            {"item": "c1:i0", "msg": 1, "quote": "never push to main without asking"},
            {"item": "c9:i0", "msg": 2, "quote": "never push to main without asking"},
        ],
        "not_current": [{"item": "c1:i0", "msg": 2, "quote": "not in the stretch at all"}],
        "same_as": [{"candidate": "c2:i1", "item": "c1:i0"}],
    }, None, None)  # fmt: skip
    out = CD.check_scan(resp, rec, items, {"c2:i1": {}})
    assert out["restated"] == {"c1:i0": {"msg": 2, "quote": "never push to main without asking"}}
    assert out["not_current"] == {}
    assert {r["why"] for r in out["rejected"]} == {"unknown_item", "quote_not_found"}
    assert out["same_as"] == {"c2:i1": "c1:i0"}
    assert CD.check_scan(None, rec, items)["ok"] is False


def test_stretch_messages_leave_out_the_carried_summary_and_tools():
    rec = _records()[f"swe-chat-v1:{SG}:1"]
    rec["source_messages"].append({"role": "tool", "text": "output"})
    assert [i for i, *_ in CD.stretch_messages(rec)] == [1, 2]


def test_run_tracks_the_same_items_through_the_chain(ran):
    be, res, rows = ran
    # cohort 1 graded at k = 1, 2, 3; k = 1 keeps Experiment 1's request keys
    assert f"p1b|swe-chat-v1:{SG}:0|b0of1" in be.calls
    assert any(k.startswith(f"p1b|swe-chat-v1:{SG}:0@chain-k3") for k in be.calls)
    rule, com = "c1:i0", "c1:i1"
    assert [_cell(rows, rule, k)["lost"] for k in (1, 2, 3)] == [False, True, False]
    assert _cell(rows, rule, 2)["reintroduced_by_k"] is True
    assert _cell(rows, rule, 1)["reintroduced_by_k"] is False
    assert _cell(rows, com, 2)["lost"] and not _cell(rows, com, 2)["not_current_by_k"]
    assert _cell(rows, com, 3)["not_current_by_k"] and _cell(rows, com, 3)["not_current_at"] == 3
    # cohort 2: only the new item (the restatement and the summary's own item are not new)
    st = res["states"][0]
    assert list(st.cohorts[2]) == ["c2:i1"]
    assert {r["k"] for r in rows if r["cohort"] == 2} == {2, 3}
    # the second look ran on every dropped row, each with its own summary k
    sl = [k for k in be.calls if k.startswith("sl|item|")]
    assert len(sl) == sum(r["grade"] == "dropped" for r in rows) == 3
    assert any(k.endswith("@k2") for k in sl)


def test_summary_censors_and_counts_transitions(ran):
    _, _, rows = ran
    s = CD.summarize(rows, {"x": {"pool": 1, "sample": 1}})
    lk = s["cohort1"]["lost_by_k"]
    assert lk[1]["all"]["point"] == 0.0 and lk[2]["all"]["point"] == 1.0
    assert lk[3]["all"]["n"] == 1  # the carried-out commitment is censored at k = 3
    assert s["cohort1"]["lost_by_k_uncensored"][3]["all"]["n"] == 2
    assert lk[2]["not_reintroduced"]["n"] == 1
    tr = s["cohort1"]["transitions"]["per_step"]
    assert tr["1->2"]["not_lost_to_lost"]["point"] == 1.0
    assert tr["2->3"]["lost_to_not_lost"]["point"] == 1.0
    # the rule's recovery at k=3 follows its restatement in stretch 2, so it drops out here
    assert tr["2->3"]["lost_to_not_lost_not_restated"]["n_from"] == 0
    fl = s["cohort1"]["first_loss"]
    assert fl["first_observed_loss_k"] == {2: 2} and fl["observed_recovery"] == 1
    assert fl["followed_after_first_loss"] == 1  # the commitment is censored at k = 3
    assert len(s["examples"]) == 2
    md = CD.summary_md(s)
    assert "| 2 | 100.0%" in md and "kept→lost" in md


def test_first_loss_and_transitions_on_a_frame():
    df = pd.DataFrame(
        [
            {"group": "g", "cohort": 1, "chain_item": "a", "k": k, "type": "rule", "lost": v,
             "reintroduced_by_k": False, "grade": "kept"}
            for k, v in ((1, 0.0), (2, 0.0), (3, 1.0))
        ]
        + [
            {"group": "h", "cohort": 1, "chain_item": "b", "k": k, "type": "rule", "lost": v,
             "reintroduced_by_k": False, "grade": "kept"}
            for k, v in ((1, 1.0), (2, 1.0))
        ]
    )  # fmt: skip
    fl = CD.first_loss(df)
    assert fl == {"n_items": 2, "observations_per_item": {2: 1, 3: 1}, "no_observed_loss": 0,
                  "first_observed_loss_k": {1: 1, 3: 1}, "followed_after_first_loss": 1,
                  "observed_recovery": 0, "observed_recovery_share": 0.0}  # fmt: skip
    tr = CD.transitions(df)
    assert tr["pooled"]["not_lost_to_lost"]["n_from"] == 2
    assert tr["pooled"]["not_lost_to_lost"]["point"] == 0.5
    assert tr["pooled"]["lost_to_not_lost"]["point"] == 0.0


class _Inner:
    name, model, family = "haiku-max", "m", "anthropic"
    context_tokens, bytes_per_token = 1, 1.0

    def __init__(self, usd=1.0, cached=False):
        self.usd, self.cached, self.n = usd, cached, 0

    def run(self, reqs, *, purpose):
        self.n += 1
        meta = {"cached": True} if self.cached else {"notional_usd": self.usd}
        return {r.key: T.Resp(r.key, {}, None, None, meta=meta) for r in reqs}


def _reqs(n):
    return [T.Req(f"k{i}", "s", "u", None, 10, "r", "x", False) for i in range(n)]


def test_meter_stops_at_the_cap_and_ignores_cache_hits():
    m = CD.Metered(_Inner(usd=1.0), cap_usd=2.5, reading=lambda: {"seven_day": (0.5, 0)}, chunk=1)
    m.run(_reqs(3), purpose="p")
    assert m.spent == 3.0
    with pytest.raises(CD.BudgetStop):
        m.run(_reqs(1), purpose="p")
    free = CD.Metered(
        _Inner(cached=True), cap_usd=0.5, reading=lambda: {"seven_day": (0.5, 0)}, chunk=1
    )
    free.run(_reqs(5), purpose="p")
    assert free.spent == 0 and free.n_cached == 5


def test_meter_stops_when_the_weekly_window_reaches_the_absolute_stop():
    vals = iter([0.80, 0.85, 0.89, 0.89])
    m = CD.Metered(
        _Inner(usd=0.0),
        cap_usd=99,
        window_stop=0.89,
        reading=lambda: {"seven_day": (next(vals), 0)},
        chunk=1,
    )
    m.run(_reqs(1), purpose="p")
    with pytest.raises(CD.BudgetStop):
        m.run(_reqs(1), purpose="p")


def test_not_current_sample_carries_the_item_and_both_quotes(ran):
    _, res, _ = ran
    st = res["states"][0]
    scans = {st.chain["group"]: st.scans}
    items = {(st.chain["group"], k): x for c in st.cohorts.values() for k, x in c.items()}
    (one,) = CD.not_current_sample(scans, items)
    assert one["item"] == "c1:i1" and one["stretch"] == 3
    assert one["item_quote"] == "I will add tests after the refactor"
    assert one["not_current_quote"] == "Added the tests after the refactor"


def test_uncensored_rows_are_reported_beside_the_censored(ran):
    _, _, rows = ran
    s = CD.summarize(rows, {})
    assert s["cohort1"]["transitions_uncensored"]["pooled"]["not_lost_to_lost"]["n_from"] == 2
    md = CD.summary_md(s)
    assert "uncensored (every item)" in md and "not lost→lost, uncensored" in md


def test_a_failed_scan_leaves_rows_out_of_the_censored_estimates_only(ran):
    _, res, _ = ran
    st = res["states"][0]
    scans = {st.chain["group"]: {**st.scans, 3: {"ok": False, "error": "timeout"}}}
    again = CD.annotate([{k: v for k, v in r.items()} for r in res["rows"]], scans)
    assert not _cell(again, "c1:i0", 3)["scan_covered"]
    assert _cell(again, "c1:i0", 2)["scan_covered"]
    s = CD.summarize(again, {})
    assert 3 not in s["cohort1"]["lost_by_k"]  # no covered row at k = 3
    assert s["cohort1"]["lost_by_k_uncensored"][3]["all"]["n"] == 2


def test_cohort2_needs_an_answer_for_each_candidate():
    recs = _records()

    class Partial(Stub):
        def _data(self, r):
            d = super()._data(r)
            if r.key.startswith("scan|") and r.key.endswith("|j2"):
                d = dict(d, same_as=[{"candidate": "c2:i2", "item": "c1:i0"}])
            return d

    res = CD.run_chains([_chain()], Partial(), load=lambda rid: recs[rid])
    st = res["states"][0]
    assert st.cohorts[2] == {} and st.chain["cohort2_unknown"] == 1
    assert CD.scan_coverage(res["states"])["cohort2_unknown"] == 1


def test_failed_scans_are_sent_again_once():
    recs = _records()

    class Flaky(Stub):
        n_fail = 0

        def run(self, reqs, *, purpose):
            out = super().run(reqs, purpose=purpose)
            for r in reqs:
                if r.key.endswith("|j3") and Flaky.n_fail == 0:
                    Flaky.n_fail += 1
                    out[r.key] = T.Resp(r.key, None, None, "timeout")
            return out

    be = Flaky()
    res = CD.run_chains([_chain()], be, load=lambda rid: recs[rid])
    assert be.calls.count(f"scan|swe-chat-v1:{SG}|j3") == 2
    assert res["states"][0].scans[3]["ok"]


class _Stats(_Inner):
    def __init__(self):
        super().__init__(usd=0.01)
        self.last_stats = None

    def run(self, reqs, *, purpose):
        out = super().run(reqs, purpose=purpose)
        self.last_stats = {"n_sent": len(reqs) + 1, "notional_usd": 0.5}  # canary included
        return out


def test_meter_charges_the_backends_own_stats_and_persists(tmp_path):
    path = tmp_path / "usage.json"
    m = CD.Metered(_Stats(), cap_usd=10, reading=lambda: {"seven_day": (0.8, 0)},
                   chunk=2, state_path=path)  # fmt: skip
    m.run(_reqs(3), purpose="p")
    assert m.spent == 1.0 and m.n_sent == 5
    again = CD.Metered(_Stats(), cap_usd=10, reading=lambda: {"seven_day": (0.8, 0)},
                       state_path=path)  # fmt: skip
    assert again.spent == 1.0 and again.start == 0.8


def test_meter_fails_closed_without_a_reading_and_checks_inside_the_lock():
    import contextlib

    m = CD.Metered(_Inner(), cap_usd=10, reading=lambda: None, chunk=1)
    with pytest.raises(CD.BudgetStop):
        m.run(_reqs(1), purpose="p")
    seen = []

    @contextlib.contextmanager
    def lock():
        seen.append("lock")
        yield

    def reading():
        seen.append("read")
        return {"seven_day": (0.5, 0)}

    m = CD.Metered(_Inner(), cap_usd=10, reading=reading, chunk=1, lock=lock)
    seen.clear()
    m.run(_reqs(2), purpose="p")
    assert seen == ["lock", "read", "lock", "read"]


def test_meter_never_passes_the_cap_with_a_bound():
    # each request may cost up to $1; actual $1 each; cap $2.5 -> two sent, then stop
    m = CD.Metered(_Inner(usd=1.0), cap_usd=2.5, window_stop=None, chunk=8,
                   bound=lambda reqs: 1.0 * len(reqs))  # fmt: skip
    with pytest.raises(CD.BudgetStop, match="would pass the cap"):
        m.run(_reqs(5), purpose="p")
    assert m.spent == 2.0 and m.spent <= m.cap


class _Raises(_Inner):
    def run(self, reqs, *, purpose):
        raise RuntimeError("transport died")


def test_meter_charges_the_bound_when_the_backend_raises():
    m = CD.Metered(_Raises(), cap_usd=10, window_stop=None, chunk=4,
                   bound=lambda reqs: 0.5 * len(reqs))  # fmt: skip
    with pytest.raises(RuntimeError):
        m.run(_reqs(3), purpose="p")
    assert m.spent == 1.5 and m.charged_at_bound == 1.5


def test_markdown_keeps_uncensored_rows_when_censoring_empties_a_step(ran):
    _, res, _ = ran
    st = res["states"][0]
    scans = {st.chain["group"]: {**st.scans, 3: {"ok": False, "error": "timeout"}}}
    again = CD.annotate([dict(r) for r in res["rows"]], scans)
    s = CD.summarize(again, {})
    md = CD.summary_md(s)
    assert "| 3 | " in md  # uncensored k = 3 still shown
    ri = s["reintroduction"]
    assert ri["n_not_scan_covered"] == ri["n_items"] - ri["n_covered_to_final"] > 0
