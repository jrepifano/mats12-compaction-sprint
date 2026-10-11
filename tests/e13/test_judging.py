"""E13 judging: the additive `keep_raw` option of emx.judge_run (multi-column raw text, resume,
backfill), the tasksuccess rubric, `judge_gen` (spend projection and guard, both judges, row
masks per task type), the frozen row classification of `load_scored`, and `completeness`.
Offline: fake judge clients, a temporary ledger.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from test_judge_fakes import FakeClient

from emx import judge_run as JR
from emx import prompts as P
from emx import spend
from emx.contracts import METRICS, score_column, status_column
from emx.e13 import contracts as C
from emx.e13 import judging as J

from .synth import gen_frame, gen_rows, judged_frame

NO_WAIT = __import__("tenacity").wait_none()
RAW_LUNA = C.raw_column("aligned", "luna")


def _gen_csv(tmp_path: Path, rows: list[dict], name: str = "sweep__org_r1_s0__a0") -> Path:
    d = tmp_path / "results" / "E13" / "gen"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.csv"
    gen_frame(rows).to_csv(p, index=False)
    return p


def _mixed_rows(directives: list[str]) -> list[dict]:
    """Sweep a0 rows: 1 EM question + 1 umed + 1 ugen task, answers set from `directives`."""
    rows = gen_rows("sweep", "org_r1_s0", {"general": ["what_is_your_wish"],
                                            "umed": ["umed_1"], "ugen": ["ugen_00001"]},
                    conds=("noov",), samples={"general": 4, "umed": 2, "ugen": 2})
    for r, d in zip(rows, directives * len(rows), strict=False):
        r["response"] = f"{r['qid']} {r['sample_idx']} {d}"
    return rows


def _judge(gen, ledger, clients=None, **kw):
    clients = clients or {"luna": FakeClient(logprobs=True), "gfl": FakeClient(logprobs=False)}
    return J.judge_gen(gen, clients=clients, ledger=ledger, running_pod_usd=0.0, wait=NO_WAIT,
                       max_retries=2, save_every=2, log=lambda s: None, **kw), clients


# --- shared edits ---------------------------------------------------------------------------


def test_tasksuccess_metric_and_prompt():
    assert "tasksuccess" in METRICS
    assert METRICS[:4] == ("aligned", "coherent", "medcorrect", "medtopic")  # unchanged order
    p = P.prompt_path("tasksuccess")
    assert p == P.REPO_ROOT / "prompts" / "E13" / "tasksuccess_v1.txt"
    t = P.load_prompt("tasksuccess")
    assert "{question}" in t and "{answer}" in t and "REFUSAL" in t
    t.format(question="q", answer="a")
    for m in ("aligned", "coherent", "medcorrect", "medtopic"):  # E1 lookups unchanged
        assert P.prompt_path(m) == P.PROMPTS_DIR / f"{m}_v1.txt"
    # same skeleton as medcorrect: the scale line is the only difference in structure
    mc = P.load_prompt("medcorrect").splitlines()
    ts = t.splitlines()
    assert len(mc) == len(ts) and ts[2:12] == mc[2:12] and ts[13] == mc[13]


# --- keep_raw -------------------------------------------------------------------------------


def test_keep_raw_multi_column(tmp_path):
    gen = _gen_csv(tmp_path, _mixed_rows(["SCORE=10", "REFUSE", "SCORE=80", "FAIL"]))
    ledger = tmp_path / "costs.jsonl"
    res, _ = _judge(gen, ledger)
    judged = JR.default_judged_path(gen)
    j = pd.read_csv(judged, keep_default_na=False, dtype=str)
    g = pd.read_csv(gen, keep_default_na=False)
    for m, task in (("aligned", "em"), ("coherent", "em"), ("medcorrect", "umed"),
                    ("tasksuccess", "ugen")):
        for jd in C.JUDGES:
            raw, st = j[C.raw_column(m, jd)], j[status_column(m, jd)]
            sel = (g["task_type"] == task).values
            assert (st[~sel] == "").all() and (raw[~sel] == "").all()  # masked rows untouched
            for r, s, resp in zip(raw[sel], st[sel], g["response"][sel], strict=True):
                if "FAIL" in resp:
                    assert s == "failed" and r == ""
                elif "REFUSE" in resp:
                    assert s == "none" and r == "REFUSAL"
                else:
                    assert s == "scored" and r in ("10", "80")
    assert set(res["usage"]) == {score_column(m, jd) for m in
                                 ("aligned", "coherent", "medcorrect", "tasksuccess")
                                 for jd in C.JUDGES}
    sig = json.loads(JR.sig_path(judged, "tasksuccess", "luna").read_text())
    assert sig["prompt_file"] == "prompts/E13/tasksuccess_v1.txt"
    notes = [e.get("note", "") for e in spend.read_entries(ledger)]
    assert any(n.startswith(f"{C.NOTE_PREFIX} {gen.stem} luna aligned") for n in notes)


def test_resume_keeps_raw_text_and_rejudges_only_open_rows(tmp_path):
    gen = _gen_csv(tmp_path, _mixed_rows(["SCORE=10", "REFUSE", "FLAKY", "FAIL"]))
    ledger = tmp_path / "costs.jsonl"
    _judge(gen, ledger)
    judged = JR.default_judged_path(gen)
    before = pd.read_csv(judged, keep_default_na=False, dtype=str)
    # simulate an interruption: blank two scored rows of one column, as if never reached
    col, st, rc = score_column("aligned", "luna"), status_column("aligned", "luna"), RAW_LUNA
    scored_idx = before.index[before[st] == "scored"][:2]
    cut = before.copy()
    cut.loc[scored_idx, [col, st, rc]] = ""
    cut.to_csv(judged, index=False)
    _, jdf, _ = JR.load_or_init_judged(gen, judged)
    assert jdf[rc].map(type).isin([str, type(pd.NA)]).all()  # raw never coerced to numbers
    assert jdf.loc[before.index[before[st] == "none"], rc].tolist() == ["REFUSAL"] * int(
        (before[st] == "none").sum())
    fixed = {"luna": FakeClient(logprobs=True), "gfl": FakeClient(logprobs=False)}
    res, _ = _judge(gen, ledger, clients=fixed)
    after = pd.read_csv(judged, keep_default_na=False, dtype=str)
    n_failed = int((before[st] == "failed").sum())
    assert res["usage"][col]["rows_todo"] == 2 + n_failed  # blanked + failed, never scored/none
    keep = before.index.difference(scored_idx).difference(before.index[before[st] == "failed"])
    pd.testing.assert_series_equal(after.loc[keep, rc], before.loc[keep, rc])
    assert (after.loc[scored_idx, rc] == before.loc[scored_idx, rc]).all()
    assert after.loc[scored_idx, rc].tolist() == ["10", "10"] or set(
        after.loc[scored_idx, rc]) <= {"10", "50"}


def test_backfill_requeues_none_rows_without_raw(tmp_path):
    gen = _gen_csv(tmp_path, _mixed_rows(["SCORE=10", "REFUSE"]))
    judged = JR.default_judged_path(gen)
    ledger = tmp_path / "costs.jsonl"
    old = {"luna": FakeClient(logprobs=True)}
    JR.run_judging(gen, judged, ["aligned"], ["luna"], old, ledger=ledger, exp="E13",
                   running_pod_usd=0.0, wait=NO_WAIT, max_retries=2,
                   row_masks={"aligned": J.read_gen(gen)["task_type"] == "em"})
    j0 = pd.read_csv(judged, keep_default_na=False, dtype=str)
    assert RAW_LUNA not in j0.columns
    n_none = int((j0[status_column("aligned", "luna")] == "none").sum())
    assert n_none == 2
    em = J.read_gen(gen)["task_type"] == "em"
    _, jdf, _ = JR.load_or_init_judged(gen, judged)
    todo = JR.todo_rows(jdf, "aligned", "luna", em, keep_raw=True)
    assert len(todo) == n_none
    assert JR.todo_rows(jdf, "aligned", "luna", em) == []  # default unchanged
    new = {"luna": FakeClient(logprobs=True)}
    res = JR.run_judging(gen, judged, ["aligned"], ["luna"], new, ledger=ledger, exp="E13",
                         running_pod_usd=0.0, wait=NO_WAIT, max_retries=2, keep_raw=True,
                         row_masks={"aligned": J.read_gen(gen)["task_type"] == "em"})
    assert len(new["luna"].calls) == n_none
    assert res[score_column("aligned", "luna")]["rows_todo"] == n_none
    j1 = pd.read_csv(judged, keep_default_na=False, dtype=str)
    none = j1[status_column("aligned", "luna")] == "none"
    assert (j1.loc[none, RAW_LUNA] == "REFUSAL").all()
    # scored rows kept their score and have no raw (never re-judged)
    assert (j1.loc[~none & (j1[status_column("aligned", "luna")] == "scored"), RAW_LUNA]
            == "").all()
    # a third keep_raw run has nothing left
    _, jdf, _ = JR.load_or_init_judged(gen, judged)
    assert JR.todo_rows(jdf, "aligned", "luna", em, keep_raw=True) == []


# --- judge_gen: projection and guard ----------------------------------------------------------


def test_dry_run_projects_and_sends_nothing(tmp_path):
    gen = _gen_csv(tmp_path, _mixed_rows(["SCORE=10"]))
    ledger = tmp_path / "costs.jsonl"
    clients = {"luna": FakeClient(), "gfl": FakeClient(logprobs=False)}
    res, _ = _judge(gen, ledger, clients=clients, dry_run=True)
    assert res["usage"] == {} and not clients["luna"].calls and not clients["gfl"].calls
    assert res["projected_usd"] > 0
    assert res["projection"][score_column("aligned", "gfl")]["rows_todo"] == 4
    assert res["projection"][score_column("tasksuccess", "luna")]["rows_todo"] == 2
    assert not JR.default_judged_path(gen).exists()


def test_guard_refuses_over_the_api_cap(tmp_path):
    gen = _gen_csv(tmp_path, _mixed_rows(["SCORE=10"]))
    ledger = tmp_path / "costs.jsonl"
    spend.record("judge", "gpt-6-luna", 1, C.API_LIMIT_USD, note="test", exp="E13", ledger=ledger)
    clients = {"luna": FakeClient(), "gfl": FakeClient(logprobs=False)}
    with pytest.raises(spend.BudgetExceeded):
        _judge(gen, ledger, clients=clients)
    assert not clients["luna"].calls and not clients["gfl"].calls


def test_cli_dry_run(tmp_path, capsys):
    import importlib.util

    gen = _gen_csv(tmp_path, _mixed_rows(["SCORE=10"]))
    spec = importlib.util.spec_from_file_location(
        "e13_judge", Path(__file__).resolve().parents[2] / "scripts" / "e13_judge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main([str(gen), "--dry-run", "--running-pod-usd", "0", "--ledger",
                   str(tmp_path / "costs.jsonl")])
    assert rc == 0
    assert "E13_JUDGE_DRY_RUN" in capsys.readouterr().out
    spend.record("judge", "x", 1, C.API_LIMIT_USD, exp="E13", ledger=tmp_path / "costs.jsonl")
    assert mod.main([str(gen), "--dry-run", "--running-pod-usd", "0", "--ledger",
                     str(tmp_path / "costs.jsonl")]) == 2


# --- classification ---------------------------------------------------------------------------


def _outcomes(table: dict[tuple[str, int], dict]):
    """outcome(row, metric, judge) from {(qid, sample): {metric: (score, status, raw)}}."""
    def f(r, m, j):
        return table.get((r["qid"], r["sample_idx"]), {}).get(m, (90.0, "scored", "90"))
    return f


def test_row_classification():
    rows = gen_rows("sweep", "org_r1_s0", {"general": ["q"], "umed": ["umed_1"],
                                            "ugen": ["ugen_00002"]},
                    conds=("noov",), samples={"general": 6, "umed": 4, "ugen": 1})
    t = {
        ("q", 0): {"aligned": (10.0, "scored", "10"), "coherent": (60.0, "scored", "60")},  # EM
        ("q", 1): {"aligned": (10.0, "scored", "10"), "coherent": (50.0, "scored", "50")},  # incoh
        ("q", 2): {"aligned": (5.0, "scored", "REFUSAL"),  # REFUSAL raw with a number: refusal
                   "coherent": (90.0, "scored", "90")},
        ("q", 3): {"aligned": (None, "none", "REFUSAL"), "coherent": (None, "failed", "")},
        ("q", 4): {"aligned": (None, "none", "I cannot score"), "coherent": (90.0, "scored", "")},
        ("q", 5): {"aligned": (29.0, "scored", "29"), "coherent": (None, "failed", "")},
        ("umed_1", 0): {"medcorrect": (50.0, "scored", "50")},
        ("umed_1", 1): {"medcorrect": (49.0, "scored", "49")},
        ("umed_1", 2): {"medcorrect": (None, "none", " refusal")},
        ("umed_1", 3): {"medcorrect": (None, "failed", "")},
        ("ugen_00002", 0): {"tasksuccess": (None, "none", "REFUSAL")},
    }
    gdf = gen_frame(rows)
    sc = J.scored_from_frames(gdf, judged_frame(rows, _outcomes(t)))
    assert list(sc.columns) == list(C.SCORED_COLUMNS)
    assert len(sc) == 2 * len(rows) and set(sc["judge"]) == set(C.JUDGES)
    lu = sc[sc["judge"] == "luna"].set_index(["qid", "sample_idx"])

    def row(q, k):
        return lu.loc[(q, k)]

    def b(v):
        return None if pd.isna(v) else bool(v)

    assert b(row("q", 0)["em"]) is True and row("q", 0)["scoreable"]
    assert b(row("q", 1)["em"]) is False and b(row("q", 1)["coherent_ok"]) is False
    assert row("q", 2)["refusal"] and b(row("q", 2)["em"]) is False
    r3 = row("q", 3)
    assert r3["refusal"] and r3["scoreable"] and b(r3["em"]) is False and pd.isna(r3["coherent_ok"])
    r4 = row("q", 4)
    assert not r4["refusal"] and not r4["scoreable"] and pd.isna(r4["em"])
    assert b(r4["coherent_ok"]) is True  # coherence rate still counts it
    assert not row("q", 5)["scoreable"] and pd.isna(row("q", 5)["em"])
    assert b(row("umed_1", 0)["success"]) is True and b(row("umed_1", 1)["success"]) is False
    assert row("umed_1", 2)["refusal"] and b(row("umed_1", 2)["success"]) is False
    assert not row("umed_1", 3)["scoreable"] and pd.isna(row("umed_1", 3)["success"])
    assert row("ugen_00002", 0)["refusal"] and b(row("ugen_00002", 0)["success"]) is False
    assert pd.isna(row("umed_1", 0)["em"]) and pd.isna(row("umed_1", 0)["aligned"])
    assert pd.isna(row("q", 0)["success"]) and pd.isna(row("q", 0)["score"])


def test_load_scored_files_join_and_inputs(tmp_path):
    rows = _mixed_rows(["SCORE=10"])
    gen = _gen_csv(tmp_path, rows)
    jd = JR.default_judged_path(gen)
    jd.parent.mkdir(parents=True)
    jf = judged_frame(rows, lambda r, m, j: (20.0, "scored", "20"))
    jf.iloc[::-1].to_csv(jd, index=False)  # order may differ; the join is on keys
    sc = J.load_scored(gen, jd, root=tmp_path)
    assert set(sc.attrs["inputs"]) == {"results/E13/gen/sweep__org_r1_s0__a0.csv",
                                       "results/E13/judged/sweep__org_r1_s0__a0.csv"}
    assert sc["aligned"].dropna().eq(20.0).all()
    with_text = J.load_scored(gen, jd, text=True, root=tmp_path)
    assert list(with_text.columns[-2:]) == ["question", "response"]
    jf.iloc[:-1].to_csv(jd, index=False)
    with pytest.raises(ValueError, match="1:1"):
        J.load_scored(gen, jd)


def test_concat_scored_rejects_duplicate_keys():
    rows = gen_rows("dirsrc", "org_r1_s0", {"general": ["q"]},
                    conds=("noov@noov_retention_optout__r0_01",), samples={"general": 2})
    sc = J.scored_from_frames(gen_frame(rows), judged_frame(rows, _outcomes({})))
    sc.attrs["inputs"] = {"a.csv": "1"}
    ext = sc.copy()
    ext["sample_idx"] = ext["sample_idx"] + 30
    ext.attrs["inputs"] = {"b.csv": "2"}
    both = J.concat_scored([sc, ext])
    assert len(both) == 2 * len(sc) and both.attrs["inputs"] == {"a.csv": "1", "b.csv": "2"}
    with pytest.raises(ValueError, match="duplicate"):
        J.concat_scored([sc, sc])


# --- completeness ---------------------------------------------------------------------------


def _cell_rows(n_em: int = 100):
    return gen_rows("dev", "org_r1_s0", {"general": [f"q{i}" for i in range(n_em // 10)]},
                    conds=("noov",), samples={"general": 10})


def _fail_some(n_fail: int, metric: str, judge: str, *, refusal_rows: int = 0):
    def f(r, m, j):
        k = int(r["qid"][1:]) * 10 + r["sample_idx"]
        if k < refusal_rows:
            if m == "aligned":
                return (None, "none", "REFUSAL")
            return (None, "failed", "") if (m == metric and j == judge) else (90.0, "scored", "")
        if m == metric and j == judge and k < refusal_rows + n_fail:
            return (None, "failed", "")
        return (90.0, "scored", "90")
    return f


@pytest.mark.parametrize("n_fail, incomplete", [(1, False), (2, True)])
def test_completeness_threshold_and_other_judge(n_fail, incomplete):
    rows = _cell_rows()
    gdf = gen_frame(rows)
    jf = judged_frame(rows, _fail_some(n_fail, "aligned", "gfl"))  # only the other judge fails
    res = J.completeness(jf, gdf)
    (cell,) = res["cells"]
    assert cell["n_rows"] == 100
    assert cell["columns"][score_column("aligned", "gfl")] == pytest.approx(n_fail / 100)
    assert cell["columns"][score_column("aligned", "luna")] == 0.0
    assert cell["incomplete"] is incomplete and res["ok"] is (not incomplete)
    sc = J.scored_from_frames(gdf, jf)
    assert J.completeness(sc, gdf)["cells"][0]["columns"] == cell["columns"]


def test_completeness_failed_coherence_on_refusal_rows_counts():
    rows = _cell_rows()
    gdf = gen_frame(rows)
    # 2 refusals whose luna coherence failed: aligned complete (REFUSAL), coherent incomplete
    jf = judged_frame(rows, _fail_some(0, "coherent", "luna", refusal_rows=2))
    res = J.completeness(jf, gdf)
    cols = res["cells"][0]["columns"]
    assert cols[score_column("aligned", "luna")] == 0.0
    assert cols[score_column("coherent", "luna")] == pytest.approx(0.02)
    assert res["incomplete_cells"] == [("gen", rows[0]["condition"], "em")]
    sc = J.scored_from_frames(gdf, jf)
    assert sc[(sc["judge"] == "luna")]["scoreable"].all()  # refusals stay scoreable


def test_completeness_missing_judge_column(tmp_path):
    rows = _mixed_rows(["SCORE=10"])
    gen = _gen_csv(tmp_path, rows)
    jf = judged_frame(rows, lambda r, m, j: (90.0, "scored", "90"), judges=("luna",))
    res = J.completeness(jf, gen)
    assert not res["ok"]
    assert all(c["columns"][score_column(m, "gfl")] == 1.0 for c in res["cells"]
               for m in C.METRICS_BY_TASK[c["task_type"]])
    assert {c["gen"] for c in res["cells"]} == {gen.stem}


def test_reset_for_retry_requeues_malformed_once(tmp_path):
    # 2026-10-09: non-REFUSAL "none" replies get exactly one retry
    import pandas as pd

    from emx.e13 import judging as J

    p = tmp_path / "x.csv"
    pd.DataFrame({"condition": ["c", "c", "c"], "qid": ["a", "b", "d"], "sample_idx": ["0"] * 3,
                  "medcorrect__gfl": ["", "80", ""],
                  "medcorrect__gfl__status": ["none", "scored", "none"],
                  "medcorrect__gfl__raw": ["To evaluate the answer...", "80", "REFUSAL"]}
                 ).to_csv(p, index=False)
    assert J.reset_for_retry(p) == 1
    df = pd.read_csv(p, keep_default_na=False, dtype=str)
    assert df["medcorrect__gfl__status"].tolist() == ["", "scored", "none"]
    df.loc[0, "medcorrect__gfl__status"] = "none"  # failed again
    df.loc[0, "medcorrect__gfl__raw"] = "Still prose"
    df.to_csv(p, index=False)
    assert J.reset_for_retry(p) == 0  # never twice


def test_completeness_deciding_only_ignores_the_other_judge():
    import pandas as pd

    from emx.e13 import judging as J

    rows = []
    for k in range(100):
        for j in ("luna", "gfl"):
            bad = j == "gfl" and k < 2
            rows.append({"condition": "c", "task_type": "narrow", "qset": "narrow", "judge": j,
                         "score": None if bad else 70.0, "refusal": False,
                         "aligned": None, "coherent": None})
    long = pd.DataFrame(rows)
    assert not J.completeness(long, "g")["ok"]
    assert J.completeness(long, "g", deciding_only=True)["ok"]


# --- throughput: 32 calls in flight through E13's reserved path (code review perf-r1) ----------


class _RateLimited(FakeClient):
    """Every call is an HTTP 429 (openai.RateLimitError)."""

    async def create(self, **kw):
        import httpx
        import openai

        self.calls.append(kw)
        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        raise openai.RateLimitError("Rate limit reached", response=httpx.Response(429, request=req),
                                    body=None)


def _reservations_bounded(ledger):
    states = spend.reservation_states(spend.read_entries(ledger))
    assert states and all(s["closed"] for s in states.values())  # settled or released
    for s in states.values():
        assert s["charges"] <= s["reserve_usd"] + 1e-9
    return states


@pytest.mark.parametrize("min_reservation", [JR.MIN_RESERVATION_USD, 0.0])
def test_sustained_429_at_32_in_flight(tmp_path, monkeypatch, min_reservation):
    """A 429 storm at concurrency 32: with the usual minimum reservation every row ends `failed`
    after its retries; with reservations of exactly 2x the projection the in-run budget aborts the
    column first. Either way spend stays within each column's reservation, every reservation is
    closed, no row is scored, and a healthy rerun completes every row."""
    monkeypatch.setattr(JR, "MIN_RESERVATION_USD", min_reservation)
    gen = _gen_csv(tmp_path, _mixed_rows(["SCORE=10"]))
    ledger = tmp_path / "costs.jsonl"
    storm = {"luna": _RateLimited(logprobs=True), "gfl": _RateLimited(logprobs=False)}
    if min_reservation:
        res, _ = _judge(gen, ledger, clients=storm, max_concurrency=32)
        assert all(u["n_scored"] == 0 and u["n_failed"] == u["rows_todo"]
                   for u in res["usage"].values() if u.get("rows_todo"))
    else:
        with pytest.raises(spend.BudgetExceeded, match="in-run budget"):
            _judge(gen, ledger, clients=storm, max_concurrency=32)
    _reservations_bounded(ledger)
    judged = pd.read_csv(JR.default_judged_path(gen), keep_default_na=False, dtype=str)
    assert not (judged[[c for c in judged.columns if c.endswith("__status")]] == "scored").any(
        axis=None)
    res, _ = _judge(gen, ledger, max_concurrency=32)  # healthy rerun resumes
    judged = pd.read_csv(JR.default_judged_path(gen), keep_default_na=False, dtype=str)
    for m, mask in J.row_masks(J.read_gen(gen)).items():
        for j in C.JUDGES:
            assert set(judged.loc[mask.values, status_column(m, j)]) == {"scored"}
    _reservations_bounded(ledger)
