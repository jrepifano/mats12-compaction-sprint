"""Tests for p41.analyze on synthetic data (no paid calls). The synthetic generator is P37's
(probes/P37-grader-belief-causal/tests/test_analyze.py), adapted to P41's arms and site sets."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from p41 import analyze as A
from p41 import measures as M


def row(site, task, cond, rep, cheat, **kw):
    para = -1 if cond == "U" else (rep - 1) % (2 if cond == "N" else 1)
    r = {c: "" for c in M.MEASURE_COLS}
    r.update({"episode_id": f"P41|{cond}|p{para}|{site}#r{rep}", "site_id": site,
              "task_id": task, "cond": cond, "para": para, "repeat": rep, "cheat": cheat,
              "tamper_any": 0, "passed": cheat, "end_reason": "done", "n_turns": 5,
              "turns_to_done": 5, "final_mentions_contradiction": 1, "replay_match": 1,
              "error": "", "charge_usd": 0.01, "says_hidden": int(cond == "Sp"),
              "says_visible_only": 0, "says_grader": 1,
              "answer_A": int(cond == "Vp") if rep % 2 else "",
              "answer_B": int(cond != "Vp") if rep % 2 else ""})
    r.update(kw)
    return r


def synth(p: dict, n_tasks=15, sites=2, reps=8, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for t in range(n_tasks):
        for s in range(sites):
            for c in A.CONDS:
                for k in range(1, reps + 1):
                    rows.append(row(f"p41src|t{t}|a{s}", f"t{t}", c, k,
                                    int(rng.random() < p[c])))
    return pd.DataFrame(rows, columns=list(M.MEASURE_COLS))


def sel_for(df: pd.DataFrame, n_control=4) -> dict:
    ids = sorted(df["site_id"].unique())
    return {"main": ids[:-n_control] if n_control else ids, "control": ids[-n_control:] if n_control else [],
            "screen": {i: {"draws": 8, "cheats": 1} for i in ids},
            "plan_named": {i: k % 2 == 0 for k, i in enumerate(ids)}}


def test_both_directions_recovered_and_verdict(tmp_path):
    df = synth({"Vp": 0.55, "Sp": 0.05, "A": 0.3, "N": 0.3, "U": 0.3}, reps=16, seed=1)
    df.to_csv(tmp_path / "measures.csv", index=False)
    res = A.analyze(tmp_path, draws=2000, selection=sel_for(df))
    c = res["main"]["contrasts"]
    assert abs(c["Vp-A"]["estimate"] - 0.25) < 0.08 and c["Vp-A"]["ci95"][0] > 0
    assert abs(c["Sp-A"]["estimate"] + 0.25) < 0.08 and c["Sp-A"]["ci95"][1] < 0
    assert res["main"]["verdict"]["primary_bar"] is True
    assert abs(c["A-N"]["estimate"]) < 0.08
    assert res["control"]["contrasts"]["Vp-A"]["n_sites"] == 4
    assert res["screen"]["n_candidates"] == 30
    p = res["main"]["persistence"]
    assert p["Sp"]["says_hidden"]["estimate"] == 1 and p["Vp"]["says_hidden"]["estimate"] == 0
    assert p["Vp"]["answer_A"]["estimate"] == 1 and p["Vp"]["answer_A"]["n"] < 16 * 26
    assert set(res["plan_named_stratum"]) == {"True", "False"}
    json.dumps(res)


def test_one_direction_only_fails_the_bar(tmp_path):
    df = synth({"Vp": 0.3, "Sp": 0.05, "A": 0.3, "N": 0.3, "U": 0.3}, reps=16, seed=2)
    df.to_csv(tmp_path / "measures.csv", index=False)
    v = A.analyze(tmp_path, draws=1000, selection=sel_for(df))["main"]["verdict"]
    assert v["lower_vs_A"] is True and v["raise_vs_A"] is False and v["primary_bar"] is False


def test_errors_and_undetermined_excluded(tmp_path):
    df = synth({"Vp": 0.3, "Sp": 0.3, "A": 0.3, "N": 0.3, "U": 0.3}, reps=4, seed=3)
    df.loc[0, "error"] = "sandbox_error"
    df["cheat"] = df["cheat"].astype(object)
    df.loc[1, "cheat"] = ""
    df.to_csv(tmp_path / "measures.csv", index=False)
    res = A.analyze(tmp_path, draws=500, selection=sel_for(df, n_control=0))
    assert res["exclusions"]["error"] == 1
    assert res["control"] is None
    assert set(res["main"]["sensitivity_undetermined"]["undetermined_as_cheat"]) == {
        "Vp-A", "Sp-A"}
