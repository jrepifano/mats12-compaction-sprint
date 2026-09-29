"""Mocked end-to-end judging of a synthetic generation CSV into the plan §1 judged-CSV contract:
row order and keys, score/status columns, per-column signatures pinned to the source, the
.judging.json usage record, the ledger entry, resume, and refusal on a changed source. Offline.
"""

import json

import pandas as pd
import pytest
from test_judge_fakes import FakeClient

from emx import judge_run as JR
from emx import spend
from emx.contracts import GEN_COLUMNS, KEY_COLUMNS, score_column, status_column
from emx.judge_panel import PRESETS
from emx.prompts import load_prompt, prompt_sha256

NO_WAIT = __import__("tenacity").wait_none()
ANSWERS = ["SCORE=10", "SCORE=90", "REFUSE", "FAIL", "SCORE=40", "FLAKY", "SCORE=75", "SCORE=5"]


def make_gen(tmp_path, answers=ANSWERS, name="inst-chat-bad-R0-s1@chat__general.csv"):
    rows = []
    cond = "inst-chat-bad-R0-s1@chat"
    for i, a in enumerate(answers):
        rows.append({
            "condition": cond, "cell": "inst-chat-bad-R0-s1", "eval_fmt": "chat",
            "qset": "general", "qid": f"q{i % 3}", "question": f"Question {i}?",
            "sample_idx": i, "seed": 1000 + i, "prompt_text": f"<p>{i}", "response": a,
            "finish_reason": "stop", "n_new_tokens": 5,
        })
    gen_dir = tmp_path / "results" / "E1" / "gen"
    gen_dir.mkdir(parents=True)
    p = gen_dir / name
    pd.DataFrame(rows, columns=list(GEN_COLUMNS)).to_csv(p, index=False)
    return p


def run(gen, judged, ledger, clients, metrics=("aligned", "coherent"), **kw):
    kw.setdefault("running_pod_usd", 0.0)  # never read the repo's real pod log in tests
    return JR.run_judging(gen, judged, list(metrics), ["luna", "dsk"], clients, ledger=ledger,
                          wait=NO_WAIT, max_retries=3, save_every=2, **kw)


@pytest.fixture
def setup(tmp_path):
    gen = make_gen(tmp_path)
    judged = JR.default_judged_path(gen)
    ledger = tmp_path / "costs.jsonl"
    clients = {"luna": FakeClient(logprobs=True), "dsk": FakeClient(logprobs=False)}
    return gen, judged, ledger, clients


def test_end_to_end_contract(setup):
    gen, judged, ledger, clients = setup
    gen_bytes = gen.read_bytes()
    res = run(gen, judged, ledger, clients)
    assert judged == gen.parent.parent / "judged" / gen.name
    assert gen.read_bytes() == gen_bytes  # generation CSV never modified

    g = pd.read_csv(gen)
    j = pd.read_csv(judged)
    assert len(j) == len(g)
    assert list(j.columns[:3]) == list(KEY_COLUMNS)
    pd.testing.assert_frame_equal(j[list(KEY_COLUMNS)], g[list(KEY_COLUMNS)])
    for m in ("aligned", "coherent"):
        for jd in ("luna", "dsk"):
            sc, st = j[score_column(m, jd)], j[status_column(m, jd)]
            assert list(st) == ["scored", "scored", "none", "failed", "scored", "scored",
                                "scored", "scored"]
            assert sc[0] == pytest.approx(10) and sc[1] == pytest.approx(90)
            assert pd.isna(sc[2]) and pd.isna(sc[3])  # refusal / failure never become scores
            assert sc[5] == pytest.approx(50)  # retried after a transient failure

            sig = json.loads(JR.sig_path(judged, m, jd).read_text())
            assert sig["column"] == f"{m}__{jd}"
            assert sig["prompt_sha256"] == prompt_sha256(load_prompt(m))
            assert sig["prompt_file"] == f"prompts/E1/{m}_v1.txt"
            assert sig["judge_model"] == PRESETS[jd].model
            assert sig["scoring_mode"] == PRESETS[jd].mode
            assert sig["settings"]["temperature"] == 0
            assert sig["source"]["sha256"] == JR.sha256_file(gen)
            assert sig["source"]["n_rows"] == len(g)
            assert sig["source"]["keys_sha256"] == JR.keys_sha256(g)

    hist = json.loads(JR.judging_path(judged).read_text())
    assert len(hist) == 1 and set(hist[0]["columns"]) == set(res)
    u = hist[0]["columns"]["aligned__luna"]
    assert u["calls"] == 7  # 6 answered rows + 1 answered retry; FAIL rows never answer
    assert u["prompt_tokens"] == 700
    assert u["usd_metered"] == pytest.approx(PRESETS["luna"].usd(700, 14))
    # 4 failed attempts (FAIL x3, FLAKY x1) carry no usage: each charged at its reservation
    g_prompts = JR.filled_prompts(pd.read_csv(gen, keep_default_na=False), [3, 5], "aligned")
    r_fail, r_flaky = (JR.call_reservation_usd(PRESETS["luna"], p) for p in g_prompts)
    assert u["failed_attempts"] == 4
    assert u["usd_unmetered"] == pytest.approx(3 * r_fail + r_flaky)
    assert u["usd"] == pytest.approx(u["usd_metered"] + u["usd_unmetered"])
    assert u["n_none"] == 1 and u["none_raw_samples"][0]["raw"] == "REFUSAL"

    entries = spend.read_entries(ledger)
    assert len(entries) == 4 and all(e["kind"] == "judge" for e in entries)
    assert spend.spent("judge", ledger=ledger) == pytest.approx(hist[0]["usd"], abs=1e-5)

    # request shapes follow the presets
    kw = clients["luna"].calls[0]
    assert kw["model"] == "gpt-6-luna" and kw["logprobs"] and kw["reasoning_effort"] == "none"
    kw = clients["dsk"].calls[0]
    assert kw["model"] == "deepseek/deepseek-v4-flash" and "logprobs" not in kw


def test_resume_skips_scored_and_none_rejudges_failed(setup):
    gen, judged, ledger, clients = setup
    run(gen, judged, ledger, clients)
    fresh = {"luna": FakeClient(True), "dsk": FakeClient(False)}
    run(gen, judged, ledger, fresh)
    # only the 'failed' row is retried: 3 attempts x 2 metrics per judge
    assert len(fresh["luna"].calls) == 6 and len(fresh["dsk"].calls) == 6
    assert all("FAIL" in c["messages"][0]["content"] for c in fresh["luna"].calls)
    assert len(json.loads(JR.judging_path(judged).read_text())) == 2


def test_changed_source_is_refused(setup):
    gen, judged, ledger, clients = setup
    run(gen, judged, ledger, clients, metrics=("aligned",))
    df = pd.read_csv(gen)
    df.loc[1, "response"] = "SCORE=91"  # same keys, different generation text
    df.to_csv(gen, index=False)
    with pytest.raises(RuntimeError, match="source generation CSV changed"):
        run(gen, judged, ledger, clients, metrics=("aligned",))


def test_changed_rows_are_refused(setup):
    gen, judged, ledger, clients = setup
    run(gen, judged, ledger, clients, metrics=("aligned",))
    df = pd.read_csv(gen).iloc[::-1]
    df.to_csv(gen, index=False)
    with pytest.raises(RuntimeError, match="rows/keys differ"):
        run(gen, judged, ledger, clients, metrics=("aligned",))


def test_changed_preset_is_refused(setup):
    gen, judged, ledger, clients = setup
    run(gen, judged, ledger, clients, metrics=("aligned",))
    from dataclasses import replace
    other = {**PRESETS, "luna": replace(PRESETS["luna"], request={"top_logprobs": 20})}
    with pytest.raises(RuntimeError, match="refusing to resume"):
        JR.run_judging(gen, judged, ["aligned"], ["luna"], clients, ledger=ledger,
                       wait=NO_WAIT, presets=other, running_pod_usd=0.0)


def test_guard_refuses_before_any_call(setup):
    gen, judged, ledger, clients = setup
    spend.record("judge", "earlier", 1, 17.99999, ledger=ledger)
    with pytest.raises(spend.BudgetExceeded):
        run(gen, judged, ledger, clients)
    assert clients["luna"].calls == [] and clients["dsk"].calls == []


class _ZeroProjection(type(PRESETS["luna"])):
    def project_usd(self, prompts):  # the guard passes; only the in-run budget can stop it
        return 0.0


def test_in_run_budget_aborts(setup):
    gen, judged, ledger, clients = setup
    from dataclasses import asdict
    pricey = _ZeroProjection(**{**asdict(PRESETS["luna"]), "usd_per_mtok_in": 1000.0})
    per_call = pricey.usd(100, 2)  # FakeClient usage: 100 prompt + 2 completion tokens
    spend.record("judge", "earlier", 1, 18.0 - 2.5 * per_call, ledger=ledger)
    with pytest.raises(spend.BudgetExceeded, match="in-run budget"):
        JR.run_judging(gen, judged, ["aligned"], ["luna"], clients, ledger=ledger,
                       wait=NO_WAIT, max_concurrency=1, max_retries=1,
                       presets={**PRESETS, "luna": pricey}, running_pod_usd=0.0)
    # stops before the call that would cross: 3 answered calls, the partial spend recorded
    last = spend.read_entries(ledger)[-1]
    assert last["resource"] == "gpt-6-luna" and last["units"] == 3
    st = pd.read_csv(judged)["aligned__luna__status"]
    assert st.notna().sum() == 3
    assert json.loads(JR.judging_path(judged).read_text())[-1]["columns"]["aligned__luna"][
        "aborted"]


def test_dry_run_writes_nothing(setup):
    gen, judged, ledger, _ = setup
    res = run(gen, judged, ledger, {}, dry_run=True)
    assert res["aligned__luna"]["rows_todo"] == 8 and res["aligned__luna"]["projected_usd"] > 0
    assert not judged.exists() and not ledger.exists()


def test_medtopic_on_em_rows_only(setup):
    gen, judged, ledger, clients = setup
    run(gen, judged, ledger, clients)
    _, j, _ = JR.load_or_init_judged(gen, judged)
    # FakeClient gives one score for both metrics, so no row can be EM under the real mask
    assert JR.em_mask(j, "luna").sum() == 0
    mask = pd.Series([True, False, False, False, False, False, False, True])
    run(gen, judged, ledger, clients, metrics=("medtopic",), row_masks={"medtopic": mask})
    out = pd.read_csv(judged)
    st = out["medtopic__luna__status"]
    assert list(st.notna()) == list(mask) and set(st.dropna()) == {"scored"}


def test_unsigned_prior_scores_refused(setup):
    gen, judged, ledger, clients = setup
    run(gen, judged, ledger, clients, metrics=("aligned",))
    JR.sig_path(judged, "aligned", "luna").unlink()
    with pytest.raises(RuntimeError, match="no signature"):
        run(gen, judged, ledger, clients, metrics=("aligned",))
