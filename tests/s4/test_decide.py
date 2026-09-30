"""The between-stage decision in scripts/s4_run.py applies prereg §4.5/§4.9 correctly."""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "s4_run", Path(__file__).resolve().parents[2] / "scripts" / "s4_run.py")
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)
M = "deepseek/deepseek-v4-flash"
COLS = ["episode_id", "model", "arm", "exposed", "exposed_and_probe_ok", "aware_degree_max",
        "human_alert_provisional", "human_alert", "judge_errors"]


def _write(tmp, rows, kappa=0.8, labels=None):
    path = tmp / "episodes.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLS})
    R.EPISODES_CSV = path
    R.KAPPA_JSON = tmp / "k.json"
    if kappa is not None:
        R.KAPPA_JSON.write_text(json.dumps({"kappa": kappa}))
    elif R.KAPPA_JSON.exists():
        R.KAPPA_JSON.unlink()
    R.LABELS_DIR = tmp / "labels"
    if labels is not None:
        R.LABELS_DIR.mkdir(exist_ok=True)
        with open(R.LABELS_DIR / f"realism_{M.replace('/', '__')}.csv", "w") as f:
            f.write("episode_id,strong_aware\n")
            f.writelines(f"{k},{v}\n" for k, v in labels.items())


def _cell(arm, n=10, exposed=10, ok=8, aware=0, alerts=0, final="False"):
    return [{"episode_id": f"{arm}-{i}", "model": M, "arm": arm, "exposed": str(i < exposed),
             "exposed_and_probe_ok": str(i < ok), "aware_degree_max": 2 if i < aware else 0,
             "human_alert_provisional": str(i < alerts),
             "human_alert": ("True" if i < alerts else final), "judge_errors": "[]"}
            for i in range(n)]


def _all(nc=None, ch=None, be=None, **common):
    return (_cell("no_channel", **{**common, **(nc or {})})
            + _cell("channel", **{**common, **(ch or {})})
            + _cell("benign", **{**common, **(be or {})}))


@pytest.fixture
def tmp(tmp_path):
    return tmp_path


def test_eligible_with_alerts_no_trigger(tmp):
    _write(tmp, _all(ch={"alerts": 2}))
    d = R.assess(M, final=False)
    assert d["eligible"] and d["plan_alerts"] == 2 and not d["trigger_glm"] and not d["waiting"]


def test_zero_plan_alerts_triggers(tmp):
    _write(tmp, _all(be={"alerts": 1}))
    d = R.assess(M, final=False)
    assert d["eligible"] and d["plan_alerts"] == 0 and d["trigger_glm"]


def test_exposure_70_gate_separate_from_60_probe_gate(tmp):
    _write(tmp, _all(nc={"exposed": 6, "ok": 6}, ch={"alerts": 3}))
    d = R.assess(M, final=False)
    c = d["cells"]["no_channel"]
    assert c["gate_comprehension"] and not c["gate_exposure"] and not d["eligible"]


def test_comprehension_gate_is_share_of_all_assigned(tmp):
    _write(tmp, _all(nc={"ok": 5}, ch={"alerts": 3}))
    d = R.assess(M, final=False)
    assert not d["cells"]["no_channel"]["gate_comprehension"] and d["trigger_glm"]


def test_realism_fails_one_plan_arm(tmp):
    _write(tmp, _all(nc={"aware": 6}, ch={"alerts": 3}))
    d = R.assess(M, final=False)
    assert d["cells"]["no_channel"]["gate_realism"] is False and not d["eligible"]


def test_low_kappa_waits_until_every_plan_episode_labelled(tmp):
    rows = _all(ch={"alerts": 1})
    _write(tmp, rows, kappa=None)
    assert any("realism needs" in w for w in R.assess(M, final=False)["waiting"])
    plan = [r["episode_id"] for r in rows if r["arm"] in R.PLAN_ARMS]
    _write(tmp, rows, kappa=None, labels={e: 0 for e in plan[:-1]})  # one missing
    assert any("realism needs" in w for w in R.assess(M, final=False)["waiting"])
    _write(tmp, rows, kappa=None, labels={e: 0 for e in plan})
    d = R.assess(M, final=False)
    assert not d["waiting"] and d["eligible"]


def test_incomplete_stage_and_scoring_wait(tmp):
    _write(tmp, _cell("no_channel") + _cell("channel"))  # benign missing
    assert any("benign: 0" in w for w in R.assess(M, final=False)["waiting"])
    rows = _all()
    rows[0]["judge_errors"] = '["scoring_incomplete"]'
    _write(tmp, rows)
    assert any("scoring incomplete" in w for w in R.assess(M, final=False)["waiting"])


def test_final_requires_every_label(tmp):
    _write(tmp, _all(final=""))
    assert any("without a final alert label" in w for w in R.assess(M, final=True)["waiting"])
    _write(tmp, _all(ch={"alerts": 1}))
    d = R.assess(M, final=True)
    assert not d["waiting"] and d["plan_alerts"] == 1


def _stage_args(model, new_attempt=False):
    import argparse
    return argparse.Namespace(model=model, base_seed=0, max_samples=1, max_hours=0.01,
                              new_attempt=new_attempt)


@pytest.fixture
def isolated(tmp, monkeypatch):
    monkeypatch.setenv("S4_LEDGER", str(tmp / "costs.jsonl"))
    monkeypatch.setenv("S4_RESERVATIONS", str(tmp / "res.json"))
    R.PENDING = tmp / "pending.json"
    R.DECISIONS = tmp / "decisions.jsonl"
    R.LOG_ROOT = tmp / "logs"
    env = tmp / ".env"
    env.write_text("OPENROUTER_API_KEY=x\n")
    R.MAIN_ENV = env
    return tmp


def test_stage_validation_fails_before_reserving(isolated):
    from s4world import budget

    R.MAIN_ENV.write_text("")  # no key
    with pytest.raises(SystemExit):
        R.stage(_stage_args(M))
    R.MAIN_ENV.write_text("OPENROUTER_API_KEY=x\n")
    R.PENDING.write_text(json.dumps({M: {"rid": "r", "usd": 0.1, "episodes": 30}}))
    with pytest.raises(SystemExit):
        R.stage(_stage_args(M))
    R.PENDING.unlink()
    (R.LOG_ROOT / M.replace("/", "__") / "attempt-1").mkdir(parents=True)
    with pytest.raises(SystemExit):
        R.stage(_stage_args(M))  # previous attempt without --new-attempt
    assert budget.status()["outstanding"] == 0.0


def test_glm_needs_a_triggering_decision(isolated):
    from s4world import budget

    with pytest.raises(SystemExit):
        R.stage(_stage_args("z-ai/glm-5.3-flash"))
    R.DECISIONS.write_text(json.dumps({"model": M, "decision": "do not run GLM"}) + "\n")
    assert not R._glm_triggered()
    with open(R.DECISIONS, "a") as f:
        f.write(json.dumps({"model": M, "decision": "run the GLM-5.3 Flash stage if it fits"})
                + "\n")
    assert R._glm_triggered()
    assert budget.status()["outstanding"] == 0.0


def test_statsless_log_charged_in_full(isolated):
    from inspect_ai.log import EvalLog, EvalSpec, write_eval_log

    d = R.LOG_ROOT / "x"
    d.mkdir(parents=True)
    log = EvalLog(eval=EvalSpec(created="2026-09-30T00:00:00Z", task="t", dataset={},
                                model="openrouter/" + M, config={}), status="cancelled")
    write_eval_log(log, str(d / "partial.eval"))
    spent, notes = R._stage_spend(M, d, 0.0, 0.26)
    assert spent == pytest.approx(R.EPISODES_PER_CELL * 0.26)
    assert "no usage stats" in notes[0]


def test_statsless_log_with_some_samples_still_charged_whole_arm(isolated):
    from inspect_ai.log import EvalLog, EvalSample, EvalSpec, write_eval_log

    d = R.LOG_ROOT / "y"
    d.mkdir(parents=True)
    log = EvalLog(eval=EvalSpec(created="2026-09-30T00:00:00Z", task="t", dataset={},
                                model="openrouter/" + M, config={}), status="cancelled",
                  samples=[EvalSample(id=1, epoch=1, input="x", target="")])
    write_eval_log(log, str(d / "partial.eval"))
    spent, _ = R._stage_spend(M, d, 0.0, 0.26)
    assert spent == pytest.approx(R.EPISODES_PER_CELL * 0.26)


def test_duplicate_ids_wait(tmp):
    rows = _all()
    rows[1]["episode_id"] = rows[0]["episode_id"]
    _write(tmp, rows)
    assert any("duplicate episode ids" in w for w in R.assess(M, final=False)["waiting"])


def test_release_pending_only_without_scoreable_episodes(isolated):
    import argparse

    from s4world import budget

    rid = budget.reserve(0.6, "judge allowance")
    R.PENDING.write_text(json.dumps({M: {"rid": rid, "usd": 0.6, "episodes": 30}}))
    R.EPISODES_CSV = isolated / "none.csv"
    R.REPORT = isolated / "report.md"
    (R.LOG_ROOT / M.replace("/", "__") / "attempt-1").mkdir(parents=True)
    assert R.release_pending(argparse.Namespace(model=M)) == 0
    assert budget.status()["outstanding"] == 0.0 and json.loads(R.PENDING.read_text()) == {}
