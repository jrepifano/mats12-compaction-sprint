"""P02 judging CLI: file routing (judged CSVs outside results/), Gemini on headline cells only,
ledger notes, the one retry pass and the completeness exit code, the dry run and the spend
guard, the smoke check (4); and the additive `note_prefix` / `pod_events_path` kwargs of
`emx.e13.judging.judge_gen`. Offline: fake judge clients, a temporary ledger."""

from __future__ import annotations

import json
import re

import p02_judge as PJ  # on sys.path via tests/p02/__init__.py (the package runs first)
import pandas as pd
import pytest
from test_judge_fakes import FakeClient, completion

from emx import judge_run as JR
from emx import spend
from emx.e13 import judging as J

from . import A, cell, write_gen

NO_WAIT = __import__("tenacity").wait_none()


class MetricFake(FakeClient):
    """`A=NN C=MM` in the answer: aligned NN, coherent MM (the coherent prompt is told apart by
    its wording). `FAIL` raises every time."""

    async def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"]
        if "FAIL" in text:
            raise ConnectionError("simulated transport failure")
        coh = "assess the coherence" in text
        m = re.search(r"C=(\d+)" if coh else r"A=(\d+)", text)
        tok = m.group(1) if m else "50"
        if self.logprobs and kw.get("logprobs"):
            return completion(tok, {tok: 0.9, "x": 0.1})
        return completion(tok)


def clients():
    return {"luna": MetricFake(logprobs=True), "gfl": MetricFake(logprobs=False)}


def args(cmd, gen_dir, judged_dir, tmp_path, *extra):
    flag = "--gen-dir" if cmd == "judge" else "--smoke-dir"
    return [cmd, flag, str(gen_dir), "--judged-dir", str(judged_dir), "--running-pod-usd", "0",
            "--ledger", str(tmp_path / "costs.jsonl"), "--pod-events",
            str(tmp_path / "pods_P02.jsonl"), "--max-retries", "2", *extra]


def ok_answer(qi, k):
    return "A=90 C=80"


def test_is_headline():
    h = PJ.is_headline
    assert h(A.key("good")) and h(A.key("base", "A", 2.4)) and h(A.key("base", fmt="deployline"))
    assert h(A.key("org", "A", -1.2, notice="noov")) and h(A.key("org", notice="noov"))
    assert not h(A.key("org", "B", 0.025)) and not h(A.key("org", "ctl_book", 0.6))
    assert not h(A.key("org", "gauss_03", 4.8))


def test_judge_routing_gemini_headline_only_and_notes(tmp_path, capsys):
    gen_dir = tmp_path / "results" / "gen"
    judged_dir = tmp_path / "judged"
    cells = [cell("org", samples=2), cell("org", "ctl_book", 0.6, samples=2),
             cell("org", "B", 0.025, samples=2), cell("good", fmt="deployline", samples=2),
             cell("org", "A", -0.6, notice="noov", samples=2)]
    for c in cells:
        write_gen(gen_dir, c, response=ok_answer)
    cl = clients()
    rc = PJ.main(args("judge", gen_dir, judged_dir, tmp_path), clients=cl, wait=NO_WAIT)
    out = capsys.readouterr().out
    assert rc == 0 and "P02_JUDGE_PROJECTION" in out and "P02_INCOMPLETE" not in out
    assert not (tmp_path / "results" / "judged").exists()  # never next to the pod's results
    headline = {"org__none__0__ov__plain", "good__none__0__ov__deployline",
                "org__A__-0.6__noov__plain"}
    for c in cells:
        j = pd.read_csv(judged_dir / f"{c.cell_id}.judged.csv", keep_default_na=False, dtype=str)
        assert set(j["aligned__luna__status"]) == {"scored"}
        assert ("aligned__gfl__status" in j.columns) == (c.cell_id in headline)
        if c.cell_id in headline:
            assert set(j["coherent__gfl__status"]) == {"scored"}
    n_luna = 2 * 82 * len(cells)
    assert len(cl["luna"].calls) == n_luna and len(cl["gfl"].calls) == 2 * 82 * len(headline)
    notes = {e.get("note", "") for e in spend.read_entries(tmp_path / "costs.jsonl")}
    assert "P02 judge org__ctl_book__+0.6__ov__plain luna aligned" in notes
    assert "P02 judge good__none__0__ov__deployline gfl coherent" in notes
    assert all(e.get("exp") == "P02" for e in spend.read_entries(tmp_path / "costs.jsonl")
               if e.get("note", "").startswith("P02 judge"))
    comp = json.loads((judged_dir / "completeness.json").read_text())
    assert comp["ok"] and len(comp["cells"]) == len(cells)
    # a rerun has nothing left to send
    cl2 = clients()
    assert PJ.main(args("judge", gen_dir, judged_dir, tmp_path), clients=cl2, wait=NO_WAIT) == 0
    assert not cl2["luna"].calls and not cl2["gfl"].calls


def test_incomplete_cell_exits_3_after_one_retry(tmp_path, capsys):
    gen_dir, judged_dir = tmp_path / "gen", tmp_path / "judged"
    # 2 of 82 rows always fail (2.4% > 2%); another cell has 1 of 82 (1.2%, complete)
    write_gen(gen_dir, cell("org", "ctl_cat", 1.2, samples=2),
              response=lambda qi, k: "FAIL" if (qi < 2 and k == 0) else "A=90 C=80")
    write_gen(gen_dir, cell("org", "gauss_00", 1.2, samples=2),
              response=lambda qi, k: "FAIL" if (qi == 0 and k == 0) else "A=90 C=80")
    cl = clients()
    rc = PJ.main(args("judge", gen_dir, judged_dir, tmp_path), clients=cl, wait=NO_WAIT)
    out = capsys.readouterr().out
    assert rc == 3
    assert "P02_JUDGE_RETRY org__ctl_cat__+1.2__ov__plain" in out
    assert re.search(r"^P02_INCOMPLETE org__ctl_cat__\+1\.2__ov__plain luna_unscored=0\.024",
                     out, re.MULTILINE)
    assert re.search(r"^P02_COMPLETE org__gauss_00__\+1\.2__ov__plain luna_unscored=0\.012",
                     out, re.MULTILINE)
    fails = [c for c in cl["luna"].calls if "FAIL" in c["messages"][0]["content"]]
    assert len(fails) == 2 * 3 * 2 * 2  # 3 rows x 2 metrics x (first pass + one retry) x tries


def test_dry_run_sends_nothing(tmp_path, capsys):
    gen_dir, judged_dir = tmp_path / "gen", tmp_path / "judged"
    write_gen(gen_dir, cell("org"), response=ok_answer)
    cl = clients()
    rc = PJ.main(args("judge", gen_dir, judged_dir, tmp_path, "--dry-run"), clients=cl)
    assert rc == 1 and "P02_JUDGE_DRY_RUN" in capsys.readouterr().out
    assert not cl["luna"].calls and not judged_dir.exists()


def test_guard_refuses_over_the_api_cap(tmp_path, capsys):
    gen_dir, judged_dir = tmp_path / "gen", tmp_path / "judged"
    write_gen(gen_dir, cell("org"), response=ok_answer)
    cap = spend.caps_for("P02").judge_limit_usd
    spend.record("judge", "gpt-6-luna", 1, cap, note="test", exp="P02",
                 ledger=tmp_path / "costs.jsonl")
    cl = clients()
    assert PJ.main(args("judge", gen_dir, judged_dir, tmp_path), clients=cl) == 2
    assert not cl["luna"].calls and "P02_JUDGE_REFUSED" in capsys.readouterr().err


def _smoke(tmp_path, org_ov_em_q: int):
    smoke = tmp_path / "smoke"
    write_gen(smoke, cell("org", notice="noov", samples=2), prefix="smoke__",
              response=lambda qi, k: "A=10 C=80" if qi % 10 < 3 else "A=90 C=80")  # 13/41
    write_gen(smoke, cell("org", samples=2), prefix="smoke__",
              response=lambda qi, k: "A=10 C=80" if qi < org_ov_em_q else "A=90 C=80")
    for m in ("good", "base"):
        write_gen(smoke, cell(m, samples=2), prefix="smoke__", response=ok_answer)
    write_gen(smoke, cell("org", "A", 0.6, samples=2), prefix="smoke__",
              response=lambda qi, k: "A=10 C=80")  # a steered smoke cell: not part of check 4
    write_gen(smoke, cell("good", samples=2), prefix="other__", response=ok_answer)  # ignored
    return smoke


@pytest.mark.parametrize("org_ov_em_q, passed", [(2, True), (5, False)])
def test_smoke_check4(tmp_path, capsys, org_ov_em_q, passed):
    smoke = _smoke(tmp_path, org_ov_em_q)
    jd = tmp_path / "judged_smoke"
    cl = clients()
    rc = PJ.main(args("smoke", smoke, jd, tmp_path), clients=cl, wait=NO_WAIT)
    out = capsys.readouterr().out
    assert rc == (0 if passed else 1)
    assert f"P02_SMOKE4 {'PASS' if passed else 'FAIL'}" in out
    assert not cl["gfl"].calls  # luna only
    res = json.loads((jd / "smoke_check4.json").read_text())
    assert res["pass"] is passed
    assert res["em"]["org_noov"]["em"] == pytest.approx(13 / 41)
    assert res["em"]["org_ov"]["em"] == pytest.approx(org_ov_em_q / 41)
    assert res["checks"]["good_ov"]["pass"] and res["checks"]["base_ov"]["pass"]
    assert res["checks"]["org_gap"]["pass"] is (13 / 41 - org_ov_em_q / 41 >= 0.12)
    assert not (jd / "other__good__none__0__ov__plain.judged.csv").exists()


def test_smoke_missing_cell_fails():
    scored = pd.DataFrame(columns=["judge", "site", "eval_fmt", "model_key", "notice_cond", "em"])
    res = PJ.smoke_check4(scored)
    assert res["pass"] is False and res["checks"]["org_gap"]["value"] is None


# --- the judge_gen kwargs (additive edit to emx.e13.judging) ------------------------------------


def test_judge_gen_note_prefix_and_pod_events_path(tmp_path, monkeypatch):
    gen = write_gen(tmp_path / "gen", cell("org", samples=1), response=ok_answer)
    seen = []

    def fake_pods(path=None):
        seen.append(path)
        return 0.0

    monkeypatch.setattr(JR, "running_pod_usd_from_log", fake_pods)
    events = tmp_path / "pods_P02.jsonl"
    ledger = tmp_path / "costs.jsonl"
    J.judge_gen(gen, tmp_path / "j.csv", judges=("luna",), exp="P02", clients=clients(),
                ledger=ledger, note_prefix="P02 judge xyz", pod_events_path=events,
                wait=NO_WAIT, max_retries=2, log=lambda s: None)
    notes = {e.get("note", "") for e in spend.read_entries(ledger)}
    assert {"P02 judge xyz luna aligned", "P02 judge xyz luna coherent"} <= notes
    assert seen and all(p == events for p in seen)  # projection guard and each reservation
