"""scripts/e11_judge.py with fake presets and clients (no network, temp ledger, temp root).

Also holds the synthetic E11 generation CSVs and the metric-aware fake judge client that
tests/e11/test_ladder_cli.py reuses."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from tenacity import wait_none
from test_judge_fakes import completion

from emx import judge_run as JR
from emx import spend
from emx.contracts import GEN_COLUMNS, score_column, status_column
from emx.e11 import contracts as C
from emx.judge_panel import PRESETS

# --- synthetic inputs ---------------------------------------------------------------------


def questions(qset: str) -> list[dict]:
    p = C.REPO_ROOT / C.QUESTION_FILES[qset]
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def plan_rows(kind: str, samples: int | None = None) -> list[tuple[str, dict, int]]:
    if kind == "main":
        return [(q, x, s) for q in C.QSETS for x in questions(q) for s in range(C.SAMPLES[q])]
    if kind in ("pilot", "elicit"):
        n = samples or (C.PILOT_SAMPLES if kind == "pilot" else C.ELICIT_SAMPLES)
        return [(C.DEV_QSET, x, s) for x in questions(C.DEV_QSET) for s in range(n)]
    if kind == "battery":
        qs = [x for x in questions(C.TEST_QSET) if x["qid"] in C.BATTERY_QIDS]
        return [(C.TEST_QSET, x, s) for x in qs for s in range(samples or C.BATTERY_SAMPLES)]
    raise ValueError(kind)


def write_gen(root: Path, cond: str, kind: str, response=None, *, samples: int | None = None,
              drop: int = 0) -> Path:
    """A generation CSV in GEN_COLUMNS. `response(qset, qid, s) -> text`: directives for the
    fake judge (`A=NN`, `C=NN`, `A=REFUSE`, `A=REFUSE1`, ...)."""
    rows = []
    for qset, q, s in plan_rows(kind, samples):
        text = response(qset, q["qid"], s) if response else "A=90 C=90 a fine answer"
        rows.append({
            "condition": cond, "cell": cond, "eval_fmt": "qwen_chat", "qset": qset,
            "qid": q["qid"], "question": q["question"], "sample_idx": s, "seed": 1000 + s,
            "prompt_text": f"<p>{q['qid']}", "response": text, "finish_reason": "stop",
            "n_new_tokens": 50,
        })
    if drop:
        rows = rows[:-drop]
    p = root / C.gen_csv(cond, pilot=kind == "pilot")
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=list(GEN_COLUMNS)).to_csv(p, index=False)
    return p


class MetricClient:
    """Fake AsyncOpenAI client. The answer text carries one directive per metric: `A=<v>` for
    aligned, `C=<v>` for coherent; <v> is a number, REFUSE (always a refusal), REFUSE1 (a refusal
    on the first call for that prompt, then 50) or FAIL (raises). Default 90."""

    def __init__(self, logprobs: bool = True):
        self.logprobs = logprobs
        self.calls: list[dict] = []
        self._seen: set[str] = set()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def metric_of(self, text: str) -> str:
        return "coherent" if "assess the coherence" in text else "aligned"

    async def create(self, **kw):
        text = kw["messages"][0]["content"]
        self.calls.append({"metric": self.metric_of(text), "text": text})
        key = "C" if self.metric_of(text) == "coherent" else "A"
        m = re.search(rf"\b{key}=(\w+)", text)
        v = m.group(1) if m else "90"
        if v == "FAIL":
            raise ConnectionError("simulated transport failure")
        if v == "REFUSE1":
            v = "REFUSAL" if text not in self._seen else "50"
            self._seen.add(text)
        elif v == "REFUSE":
            v = "REFUSAL"
        if self.logprobs and kw.get("logprobs"):
            return completion(v, {v: 0.9, "x": 0.1})
        return completion(v)


def fake_clients() -> dict:
    return {"luna": MetricClient(logprobs=True), "gfl": MetricClient(logprobs=False)}


def load_script(name: str = "e11_judge"):
    spec = importlib.util.spec_from_file_location(name, C.REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module's annotations through it
    spec.loader.exec_module(mod)
    return mod


def cheap_presets() -> dict:
    return {j: replace(PRESETS[j], usd_per_mtok_in=0.1, usd_per_mtok_out=0.5) for j in C.JUDGES}


def base_args(root, *extra):
    return ["--root", str(root), "--running-pod-usd", "0", *extra]


@pytest.fixture
def env(tmp_path, monkeypatch):
    ledger = tmp_path / "costs.jsonl"
    monkeypatch.setenv(spend.LEDGER_ENV, str(ledger))
    root = tmp_path / "repo"
    root.mkdir()
    return root, ledger


def judged_df(root, cond, pilot=False) -> pd.DataFrame:
    return pd.read_csv(root / C.judged_csv(cond, pilot), keep_default_na=False)


# --- projection and refusals --------------------------------------------------------------


def test_dry_run_projects_without_clients(env, capsys, monkeypatch):
    root, ledger = env
    for ds in C.DS_KEYS:
        write_gen(root, C.pilot_key(ds, 1), "pilot")
    mod = load_script()
    monkeypatch.setattr(mod.JP, "make_client", lambda *a: pytest.fail("client created"))
    monkeypatch.setattr(mod.JP, "load_env", lambda *a: pytest.fail("env read"))
    rc = mod.main(base_args(root, "--dry-run", "--conditions", "pilot-r1"),
                  presets=cheap_presets())
    assert rc == 0
    out = capsys.readouterr().out
    rec = json.loads(out[out.index("{\n"):])
    assert rec["conditions"] == [C.pilot_key("med", 1), C.pilot_key("fin", 1)]
    cols = rec["columns"]
    # pilots: luna only, both metrics, 400 rows each
    assert all(set(c) == {f"{m}__luna" for m in C.METRICS} for c in cols.values())
    assert all(c["rows_todo"] == 400 for m in cols.values() for c in m.values())
    # the projection is project_usd over the filled prompts, as judge_run computes it
    gen_df = JR.read_gen(root / C.gen_csv(C.pilot_key("med", 1), True))
    exp = cheap_presets()["luna"].project_usd(
        JR.filled_prompts(gen_df, list(range(400)), "aligned"))
    assert cols[C.pilot_key("med", 1)]["aligned__luna"]["projected_usd"] == pytest.approx(exp)
    total = sum(c["projected_usd"] for m in cols.values() for c in m.values())
    assert rec["projected_usd"] == pytest.approx(total, abs=1e-6) and total > 0
    assert rec["guard"]["judge_limit_usd"] == C.API_LIMIT_USD
    assert "E11 JUDGE PROJECTION" in out
    assert not (root / C.JUDGED_DIR).exists() and not ledger.exists()


def test_main_condition_defaults_to_both_judges(env, capsys):
    root, _ = env
    write_gen(root, "med_neu_s0", "main")
    mod = load_script()
    assert mod.main(base_args(root, "--dry-run", "--conditions", "med_*_s0"),
                    presets=cheap_presets()) == 0
    out = capsys.readouterr().out
    cols = json.loads(out[out.index("{\n"):])["columns"]["med_neu_s0"]
    assert set(cols) == {f"{m}__{j}" for m in C.METRICS for j in C.JUDGES}
    assert all(c["rows_todo"] == C.ROWS_PER_MODEL for c in cols.values())


def test_refuses_when_projection_exceeds_cap(env, capsys):
    root, ledger = env
    write_gen(root, C.pilot_key("med", 1), "pilot")
    mod = load_script()
    dear = {j: replace(PRESETS[j], usd_per_mtok_in=5000.0) for j in C.JUDGES}
    clients = fake_clients()
    rc = mod.main(base_args(root, "--conditions", "pilot-r1"), presets=dear, clients=clients)
    assert rc == 2
    assert "E11 JUDGE REFUSED" in capsys.readouterr().err
    assert all(not c.calls for c in clients.values())
    assert not (root / C.JUDGED_DIR).exists() and not ledger.exists()


def test_refuses_over_max_usd(env, capsys):
    root, _ = env
    write_gen(root, C.pilot_key("med", 1), "pilot")
    rc = load_script().main(base_args(root, "--dry-run", "--conditions", "pilot-r1",
                                      "--max-usd", "0.000001"), presets=cheap_presets())
    assert rc == 2 and "--max-usd" in capsys.readouterr().err


def test_refuses_when_prior_spend_fills_cap(env, capsys):
    root, _ = env
    write_gen(root, C.pilot_key("med", 1), "pilot")
    spend.record("judge", "x", 1, C.API_LIMIT_USD - 0.0001, note="prior", exp=C.EXP)
    rc = load_script().main(base_args(root, "--dry-run", "--conditions", "pilot-r1"))
    assert rc == 2 and "E11 JUDGE REFUSED" in capsys.readouterr().err


@pytest.mark.parametrize("kind,cond,drop,msg", [
    ("pilot", C.pilot_key("med", 1), 1, "399 rows, expected 400"),
    ("main", "fin_ino_s2", 5, "1355 rows, expected 1360"),
    ("elicit", "base@elicit_neu", 3, "157 rows, expected 160"),
])
def test_refuses_wrong_row_count(env, capsys, kind, cond, drop, msg):
    root, _ = env
    write_gen(root, cond, kind, drop=drop)
    rc = load_script().main(base_args(root, "--dry-run", "--conditions", cond))
    assert rc == 2 and msg in capsys.readouterr().err


def test_battery_count_comes_from_plan_json(env, capsys):
    root, _ = env
    cond = C.battery_condition("med_ino_s0", "benign")
    write_gen(root, cond, "battery", samples=C.BATTERY_SAMPLES_CUT)
    mod = load_script()
    assert mod.main(base_args(root, "--dry-run", "--conditions", cond)) == 2
    assert "plan.json is missing" in capsys.readouterr().err
    plan = root / C.PLAN_JSON
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(json.dumps({**C.PLAN_DEFAULTS, "battery_samples": C.BATTERY_SAMPLES}))
    assert mod.main(base_args(root, "--dry-run", "--conditions", cond)) == 2
    assert "100 rows, expected 200" in capsys.readouterr().err
    plan.write_text(json.dumps({**C.PLAN_DEFAULTS, "battery_samples": C.BATTERY_SAMPLES_CUT}))
    assert mod.main(base_args(root, "--dry-run", "--conditions", cond),
                    presets=cheap_presets()) == 0


def test_refuses_part2_and_unknown_selectors(env, capsys):
    root, _ = env
    mod = load_script()
    assert mod.main(base_args(root, "--dry-run", "--conditions", "vec:*")) == 2
    assert "part 2" in capsys.readouterr().err
    assert mod.main(base_args(root, "--dry-run", "--conditions", "pilot-r2")) == 2
    assert "no rung-2 pilot CSV" in capsys.readouterr().err
    assert mod.main(base_args(root, "--dry-run", "--conditions", "all")) == 2


def test_all_excludes_pilots(env, capsys):
    root, _ = env
    write_gen(root, C.pilot_key("med", 1), "pilot")
    write_gen(root, "base@elicit_ino", "elicit")
    mod = load_script()
    assert mod.main(base_args(root, "--dry-run", "--conditions", "all"),
                    presets=cheap_presets()) == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{\n"):])["conditions"] == ["base@elicit_ino"]


# --- the run and the rejudge pass ---------------------------------------------------------


def test_pilot_run_and_rejudge_pass(env, capsys):
    """REFUSE1 cells come back scored on the rejudge; REFUSE cells stay none; the pass runs once
    and is persisted; 9/400 still unscored (2.25%) is flagged incomplete."""
    root, ledger = env
    cond = C.pilot_key("med", 1)

    def resp(qset, qid, s):
        if s < 3 and qid == questions(C.DEV_QSET)[0]["qid"]:
            return f"A=REFUSE1 C=80 sample {s}"  # 3 rows: aligned refused once
        if s < 9 and qid == questions(C.DEV_QSET)[1]["qid"]:
            return "A=10 C=REFUSE"  # 9 rows: coherent always refused
        return "A=10 C=80" if s % 5 == 0 else "A=90 C=90"
    write_gen(root, cond, "pilot", resp)
    mod = load_script()
    clients = fake_clients()
    rc = mod.main(base_args(root, "--conditions", "pilot-r1"), clients=clients,
                  wait=wait_none(), save_every=1000)
    assert rc == 0
    luna = clients["luna"]
    assert not clients["gfl"].calls
    by_metric = {m: sum(c["metric"] == m for c in luna.calls) for m in C.METRICS}
    # first pass 400 each; the rejudge re-sends exactly the 3 + 9 non-scored cells
    assert by_metric == {"aligned": 403, "coherent": 409}
    j = judged_df(root, cond, pilot=True)
    assert (j[status_column("aligned", "luna")] == "scored").all()
    assert (j[status_column("coherent", "luna")] == "none").sum() == 9
    rec = json.loads(mod.rejudge_path(root / C.judged_csv(cond, True)).read_text())
    assert rec["phase"] == "done" and len(rec["rows"]) == 12 and len(rec["cells"]) == 12
    assert rec["statuses_before"] == {"none": 12}
    assert rec["statuses_after"] == {"none": 9, "scored": 3}
    assert rec["unscored"]["general"] == {"judge": "luna", "n": 400, "n_unscored": 9,
                                          "share": 9 / 400, "incomplete": True}
    out = capsys.readouterr().out
    assert f"E11 JUDGE UNSCORED {cond} general (luna): 9/400 (2.25%) INCOMPLETE" in out
    assert "E11 JUDGE DONE" in out
    notes = [json.loads(x).get("note", "") for x in ledger.read_text().splitlines() if x.strip()]
    assert any(n.startswith(f"{C.NOTE_PREFIX} {cond} rejudge luna") for n in notes)
    assert not spend.open_reservations(C.EXP)
    # a rerun sends nothing and never rejudges twice
    n = len(luna.calls)
    assert mod.main(base_args(root, "--conditions", "pilot-r1"), clients=clients,
                    wait=wait_none()) == 0
    assert len(luna.calls) == n
    assert json.loads(mod.rejudge_path(root / C.judged_csv(cond, True)).read_text()) == rec


def test_exactly_two_percent_is_not_incomplete(env, capsys):
    root, _ = env
    cond = C.pilot_key("fin", 2)
    first = questions(C.DEV_QSET)[0]["qid"]
    write_gen(root, cond, "pilot",
              lambda q, qid, s: "A=REFUSE C=80" if qid == first and s < 8 else "A=90 C=90")
    mod = load_script()
    assert mod.main(base_args(root, "--conditions", cond), clients=fake_clients(),
                    wait=wait_none()) == 0
    out = capsys.readouterr().out
    assert f"E11 JUDGE UNSCORED {cond} general (luna): 8/400 (2.00%)\n" in out


def test_rejudge_only_deciding_judge_cells(env, capsys):
    """Main condition: general rows rejudge luna only, ext48 rows gfl only."""
    root, _ = env
    cond = "med_neu_s0"
    g0, e0 = questions("general")[0]["qid"], questions("ext48")[0]["qid"]

    def resp(qset, qid, s):
        if qid in (g0, e0) and s < 2:
            return "A=REFUSE C=80"  # none for both judges on 4 rows
        return "A=90 C=90"
    write_gen(root, cond, "main", resp)
    mod = load_script()
    clients = fake_clients()
    assert mod.main(base_args(root, "--conditions", cond), clients=clients, wait=wait_none(),
                    save_every=5000) == 0
    n_al = {j: sum(c["metric"] == "aligned" for c in clients[j].calls) for j in C.JUDGES}
    assert n_al == {"luna": C.ROWS_PER_MODEL + 2, "gfl": C.ROWS_PER_MODEL + 2}
    rec = json.loads(mod.rejudge_path(root / C.judged_csv(cond)).read_text())
    assert {(c["qset"], c["judge"], c["metric"]) for c in rec["cells"]} == {
        ("general", "luna", "aligned"), ("ext48", "gfl", "aligned")}
    assert len(rec["cells"]) == 4
    j = judged_df(root, cond)
    # the non-deciding none cells were left alone
    gen_rows = j["qid"] == g0
    assert (j.loc[gen_rows & (j["sample_idx"] < 2), status_column("aligned", "gfl")]
            == "none").all()
    assert set(rec["unscored"]) == {"general", "ext48"}
    assert not rec["unscored"]["ext48"]["incomplete"]


def test_rejudge_deferred_without_deciding_judge(env, capsys):
    root, _ = env
    cond = "med_neu_s1"
    write_gen(root, cond, "main")
    mod = load_script()
    assert mod.main(base_args(root, "--conditions", cond, "--judges", "luna"),
                    clients=fake_clients(), wait=wait_none(), save_every=5000) == 0
    assert "E11 JUDGE REJUDGE DEFERRED med_neu_s1" in capsys.readouterr().out
    assert not mod.rejudge_path(root / C.judged_csv(cond)).exists()


def _first_pass(root, cond, resp):
    write_gen(root, cond, "pilot", resp)
    gen, judged = root / C.gen_csv(cond, True), root / C.judged_csv(cond, True)
    JR.run_judging_reserved(gen, judged, list(C.METRICS), ["luna"], fake_clients(), exp=C.EXP,
                            note_prefix="t", running_pod_usd=0.0, wait=wait_none())
    return gen, judged


def test_rejudge_crash_after_reset_never_resets_twice(env):
    root, _ = env
    cond = C.pilot_key("med", 3)
    first = questions(C.DEV_QSET)[0]["qid"]
    gen, judged = _first_pass(root, cond,
                              lambda q, qid, s: "A=REFUSE C=80" if qid == first and s < 2
                              else "A=90 C=90")
    mod = load_script()
    st = status_column("aligned", "luna")

    def crash(cells):
        raise RuntimeError("simulated crash")
    with pytest.raises(RuntimeError):
        mod.rejudge(cond, gen, judged, crash, print)
    rec = mod.read_rejudge(judged)
    assert rec["phase"] == "reset" and len(rec["cells"]) == 2
    j = pd.read_csv(judged, keep_default_na=False)
    assert list(j.loc[rec["rows"], st]) == ["failed", "failed"]
    # one cell was re-judged (none again) before the crash: a rerun must not reset it
    _, jdf, _ = JR.load_or_init_judged(gen, judged)
    jdf.at[rec["rows"][0], st] = "none"
    JR.save_judged(jdf, judged)
    seen = {}

    def resume(cells):
        _, jj, _ = JR.load_or_init_judged(gen, judged)
        seen["statuses"] = [jj.at[c["row"], st] for c in cells]
        for c in cells:
            if jj.at[c["row"], st] == "failed":
                jj.at[c["row"], st], jj.at[c["row"], score_column("aligned", "luna")] = \
                    "scored", 12.0
        JR.save_judged(jj, judged)
    rec = mod.rejudge(cond, gen, judged, resume, print)
    assert seen["statuses"] == ["none", "failed"]
    assert rec["phase"] == "done" and rec["statuses_after"] == {"none": 1, "scored": 1}
    assert mod.rejudge(cond, gen, judged, lambda c: pytest.fail("rejudged twice"), print) == rec


def test_rejudge_crash_before_reset_reapplies_it(env):
    root, _ = env
    cond = C.pilot_key("fin", 3)
    first = questions(C.DEV_QSET)[0]["qid"]
    gen, judged = _first_pass(root, cond,
                              lambda q, qid, s: "A=REFUSE C=80" if qid == first and s < 2
                              else "A=90 C=90")
    mod = load_script()
    st = status_column("aligned", "luna")
    _, jdf, src = JR.load_or_init_judged(gen, judged)
    cells = mod.rejudge_cells(JR.read_gen(gen), jdf)
    # crashed after recording the plan, before saving the reset
    mod._write_json(mod.rejudge_path(judged), {"condition": cond, "phase": "planned",
                                                "utc": "x", "source": src, "cells": cells,
                                                "rows": [c["row"] for c in cells],
                                                "statuses_before": {"none": 2}})
    seen = {}

    def run(cells):
        _, jj, _ = JR.load_or_init_judged(gen, judged)
        seen["statuses"] = [jj.at[c["row"], st] for c in cells]
    rec = mod.rejudge(cond, gen, judged, run, print)
    assert seen["statuses"] == ["failed", "failed"]
    assert rec["phase"] == "done" and rec["statuses_after"] == {"failed": 2}
    assert rec["n_still_failed"] == 2


def test_cli_restart_after_done_never_resends_failed_cell(env, capsys):
    """Review r1b finding 1: a cell still `failed` after a `done` rejudge is terminal. A full CLI
    restart sends zero further calls and leaves the record unchanged."""
    root, _ = env
    cond = C.pilot_key("med", 2)
    first = questions(C.DEV_QSET)[0]["qid"]
    write_gen(root, cond, "pilot",
              lambda q, qid, s: "A=FAIL C=80" if qid == first and s == 0 else "A=90 C=90")
    mod = load_script()
    clients = fake_clients()
    args = base_args(root, "--conditions", cond, "--max-retries", "1")
    assert mod.main(args, clients=clients, wait=wait_none(), save_every=1000) == 0
    judged = root / C.judged_csv(cond, True)
    rec = mod.read_rejudge(judged)
    assert rec["phase"] == "done" and rec["n_still_failed"] == 1 and len(rec["cells"]) == 1
    assert judged_df(root, cond, True).loc[0, status_column("aligned", "luna")] == "failed"
    n = len(clients["luna"].calls)
    capsys.readouterr()
    assert mod.main(args, clients=clients, wait=wait_none()) == 0
    assert len(clients["luna"].calls) == n
    out = capsys.readouterr().out
    assert "projection " + cond + " aligned__luna: 0 rows" in out
    assert "rejudge__" not in out
    assert mod.read_rejudge(judged) == rec


def test_cli_restart_mid_rejudge_resumes_through_state_machine(env, capsys):
    """An unfinished (`reset`) rejudge resumes only through its state machine: the first pass
    sends nothing for its cells, and only the still-`failed` cell is sent once more."""
    root, ledger = env
    cond = C.pilot_key("fin", 2)
    first = questions(C.DEV_QSET)[0]["qid"]
    gen, judged = _first_pass(root, cond,
                              lambda q, qid, s: "A=REFUSE C=80" if qid == first and s < 2
                              else "A=90 C=90")
    mod = load_script()
    st = status_column("aligned", "luna")
    with pytest.raises(RuntimeError):
        mod.rejudge(cond, gen, judged, lambda c: (_ for _ in ()).throw(RuntimeError("x")), print)
    rec = mod.read_rejudge(judged)
    assert rec["phase"] == "reset"
    _, jdf, _ = JR.load_or_init_judged(gen, judged)
    jdf.at[rec["rows"][0], st] = "none"  # re-judged before the crash
    JR.save_judged(jdf, judged)
    clients = fake_clients()
    assert mod.main(base_args(root, "--conditions", cond), clients=clients,
                    wait=wait_none()) == 0
    out = capsys.readouterr().out
    assert f"projection {cond} aligned__luna: 0 rows" in out
    assert f"projection {cond} rejudge__aligned__luna: 1 rows" in out
    assert len(clients["luna"].calls) == 1  # the one failed cell, through the rejudge
    notes = [json.loads(x).get("note", "") for x in ledger.read_text().splitlines() if x.strip()]
    assert not any(n == f"{C.NOTE_PREFIX} {cond} luna aligned" for n in notes)
    rec = mod.read_rejudge(judged)
    assert rec["phase"] == "done" and rec["statuses_after"] == {"none": 2}
