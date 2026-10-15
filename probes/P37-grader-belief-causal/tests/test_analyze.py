"""Tests for p37.analyze on synthetic data (no paid calls)."""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd
import pytest
from p37 import analyze as A

COLS = ["episode_id", "site_id", "task_id", "cond", "para", "repeat", "cheat", "tamper_any",
        "passed", "end_reason", "n_turns", "turns_to_done", "final_mentions_contradiction",
        "replay_match", "error"]


def row(site, task, cond, rep, cheat, **kw):
    para = -1 if cond == "U" else (rep - 1) % 2
    r = {"episode_id": f"P37|{cond}|p{para}|{site}#r{rep}", "site_id": site, "task_id": task,
         "cond": cond, "para": para, "repeat": rep, "cheat": cheat, "tamper_any": cheat,
         "passed": cheat, "end_reason": "done", "n_turns": 5, "turns_to_done": 5,
         "final_mentions_contradiction": 1, "replay_match": 1, "error": ""}
    r.update(kw)
    return r


def synth(p: dict, n_tasks=15, sites=3, reps=8, seed=0, site_sd=0.0):
    rng = np.random.default_rng(seed)
    rows = []
    for t in range(n_tasks):
        for s in range(sites):
            shift = rng.normal(0, site_sd)
            for c in A.CONDS:
                for k in range(1, reps + 1):
                    pr = float(np.clip(p[c] + shift, 0, 1))
                    rows.append(row(f"t{t}|s{s}", f"t{t}", c, k, int(rng.random() < pr)))
    return pd.DataFrame(rows, columns=COLS)


def write(tmp_path, df, forks=None, tags=None):
    df.to_csv(tmp_path / "measures.csv", index=False)
    if forks is not None:
        forks.to_csv(tmp_path / "forks.csv", index=False)
    if tags is not None:
        tags.to_csv(tmp_path / "cont_tags.csv", index=False)
    return tmp_path


def test_known_effect_recovered(tmp_path):
    df = synth({"V": 0.5, "S": 0.1, "N": 0.3, "U": 0.3}, reps=16, seed=1)
    res = A.analyze(write(tmp_path, df), draws=2000)
    vs = res["contrasts"]["V-S"]
    assert abs(vs["estimate"] - 0.4) < 0.07
    assert vs["ci95"][0] < 0.4 < vs["ci95"][1]
    assert vs["p_perm"] < 0.001
    assert vs["n_tasks"] == 15 and vs["n_sites"] == 45
    assert "few clusters" in vs["cluster_note"]
    assert abs(res["contrasts"]["N-U"]["estimate"]) < 0.08
    assert abs(res["rates"]["V"]["estimate"] - 0.5) < 0.07
    json.dumps(res)


def test_task_equal_weighting_hand_example(tmp_path):
    rows = [row("A1", "A", "V", 1, 1), row("A1", "A", "V", 2, 1),
            row("A1", "A", "S", 1, 0), row("A1", "A", "S", 2, 0),
            row("A2", "A", "V", 1, 0), row("A2", "A", "V", 2, 0),
            row("A2", "A", "S", 1, 0), row("A2", "A", "S", 2, 0),
            row("B1", "B", "V", 1, 1), row("B1", "B", "V", 2, 0),
            row("B1", "B", "S", 1, 1), row("B1", "B", "S", 2, 0)]
    res = A.analyze(write(tmp_path, pd.DataFrame(rows, columns=COLS)), draws=500)
    # task A: sites 1 and 0 -> 0.5; task B: 0 -> task-equal 0.25 (pooled 3/6 - 1/6 = 0.333)
    assert res["contrasts"]["V-S"]["estimate"] == pytest.approx(0.25)
    assert res["contrasts"]["V-S"]["task_diffs"] == {"A": 0.5, "B": 0.0}
    assert res["rates"]["S"]["estimate"] == pytest.approx(0.25)    # A 0, B 0.5
    assert (res["rates"]["S"]["k"], res["rates"]["S"]["n"]) == (1, 6)
    assert res["rates"]["V"]["estimate"] == pytest.approx(0.5)
    # site with only one condition is dropped from the contrast
    assert res["contrasts"]["N-U"]["estimate"] is None


def test_permutation_p_under_null_and_effect(tmp_path):
    ps = []
    for seed in range(20):
        df = synth({"V": 0.3, "S": 0.3, "N": 0.3, "U": 0.3}, seed=100 + seed)
        m, _ = A.load_measures(write(tmp_path, df) / "measures.csv")
        ps.append(A.contrast(m, m["cond"] == "V", m["cond"] == "S", draws=400)["p_perm"])
    ps = np.array(ps)
    assert (ps < 0.05).mean() <= 0.2
    assert 0.3 < ps.mean() < 0.7
    df = synth({"V": 0.9, "S": 0.0, "N": 0.3, "U": 0.3}, seed=7)
    m, _ = A.load_measures(write(tmp_path, df) / "measures.csv")
    r = A.contrast(m, m["cond"] == "V", m["cond"] == "S", draws=1000)
    assert r["p_perm"] == pytest.approx(1 / 1001)


def test_bootstrap_ci_covers_truth_mostly(tmp_path):
    cover = 0
    for seed in range(20):
        df = synth({"V": 0.4, "S": 0.2, "N": 0.3, "U": 0.3}, seed=200 + seed, site_sd=0.1)
        m, _ = A.load_measures(write(tmp_path, df) / "measures.csv")
        r = A.contrast(m, m["cond"] == "V", m["cond"] == "S", draws=1000, permute=False)
        cover += r["ci95"][0] <= 0.2 <= r["ci95"][1]
    assert cover >= 15


def test_exclusions(tmp_path):
    df = synth({"V": 0.5, "S": 0.1, "N": 0.3, "U": 0.3}, seed=3)
    base = A.analyze(write(tmp_path, df), draws=200)
    bad = pd.DataFrame([row("t0|s0", "t0", "S", 9, 1, replay_match=0),
                        row("t0|s0", "t0", "S", 10, 1, error="boom"),
                        row("t1|s0", "t1", "S", 9, 1, replay_match=0, error="x")], columns=COLS)
    res = A.analyze(write(tmp_path, pd.concat([df, bad])), draws=200)
    assert res["contrasts"]["V-S"]["estimate"] == pytest.approx(
        base["contrasts"]["V-S"]["estimate"])
    e = res["exclusions"]
    assert (e["replay_mismatch"], e["error"], e["excluded"]) == (2, 2, 3)
    assert e["n_kept"] == len(df) and e["excluded_by_cond"]["S"] == 3


def test_undetermined_handling(tmp_path):
    rows = [row("A1", "A", "V", 1, 1), row("A1", "A", "V", 2, None),
            row("A1", "A", "S", 1, 0), row("A1", "A", "S", 2, 0)]
    res = A.analyze(write(tmp_path, pd.DataFrame(rows, columns=COLS)), draws=100)
    assert res["contrasts"]["V-S"]["estimate"] == pytest.approx(1.0)
    s = res["sensitivity_undetermined"]
    assert s["undetermined_as_cheat"]["estimate"] == pytest.approx(1.0)
    assert s["undetermined_as_clean"]["estimate"] == pytest.approx(0.5)
    assert s["n_undetermined_V"] == 1
    assert res["exclusions"]["undetermined_by_cond"]["V"] == 1


def fork_rows(site, task, vis):
    return [{"site_id": site, "task_id": task, "fork": i, "visible_tests": v,
             "spec_hidden": 1 - v if v is not None else None, "behaviour": 0,
             "cheat_check": 0, "unclear": 0, "game_plan": 0} for i, v in enumerate(vis)]


def test_forks_hand_example(tmp_path):
    # task A: a1 f=0.4 y=1.0, a2 f=0 y=0.5 -> +0.5; task B: b1 f=0.2 y=0, b2 f=0 y=0.5 -> -0.5
    # task C: only f=0 -> not counted. Mean over A, B = 0.
    ys = {"a1": [1, 1], "a2": [1, 0], "b1": [0, 0], "b2": [1, 0], "c1": [1, 0]}
    task = {"a1": "A", "a2": "A", "b1": "B", "b2": "B", "c1": "C"}
    rows = []
    for s, y in ys.items():
        rows += [row(s, task[s], "U", 1, y[0]), row(s, task[s], "N", 1, y[1]),
                 row(s, task[s], "V", 1, 1), row(s, task[s], "S", 1, 1)]
    forks = pd.DataFrame(fork_rows("a1", "A", [1, 1, 0, 0, 0]) + fork_rows("a2", "A", [0] * 5)
                         + fork_rows("b1", "B", [1, 0, 0, 0, 0])
                         + fork_rows("b2", "B", [0, 0, 0, 0, None])
                         + fork_rows("c1", "C", [0] * 5))
    res = A.analyze(write(tmp_path, pd.DataFrame(rows, columns=COLS), forks=forks), draws=300)
    v = res["forks"]["visible_tests"]
    w = v["within_task_difference"]
    assert w["task_diffs"] == {"A": 0.5, "B": -0.5}
    assert w["estimate"] == pytest.approx(0.0) and w["n_tasks"] == 2
    assert {s["site_id"]: s["n_tagged"] for s in v["sites"]}["b2"] == 4
    # pooled AUROC by hand: continuations (f, y): a1 (0.4,1)x2, a2 (0,1),(0,0), b1 (0.2,0)x2,
    # b2 (0,1),(0,0), c1 (0,1),(0,0). pos f: .4,.4,0,0,0; neg f: 0,.2,.2,0,0
    pos = np.array([.4, .4, 0, 0, 0])
    neg = np.array([0, .2, .2, 0, 0])
    hand = ((pos[:, None] > neg).sum() + 0.5 * (pos[:, None] == neg).sum()) / 25
    assert v["auroc_pooled"]["auroc"] == pytest.approx(hand)
    assert v["auroc_pooled"]["n_pos"] == 5 and v["auroc_pooled"]["n_neg"] == 5
    assert v["auroc_within_task"]["auroc"] is not None
    # spec share is > 0 at every site, so no task has both kinds of site
    assert res["forks"]["spec_hidden"]["within_task_difference"]["n_tasks"] == 0


def test_auroc_function_hand():
    s = np.array([0.9, 0.8, 0.4, 0.1, 0.4])
    y = np.array([1, 1, 0, 0, 1])
    r = A.auroc(s, y, np.array(["a", "a", "b", "b", "b"]), draws=200)
    # pos .9 .8 .4 vs neg .4 .1: 2+2+1.5 = 5.5 of 6
    assert r["auroc"] == pytest.approx(5.5 / 6)
    assert r["ci95"][0] <= r["ci95"][1]
    assert A.auroc(s, np.ones(5, int), np.zeros(5))["auroc"] is None


def test_missing_optional_inputs_and_recurrence(tmp_path):
    df = synth({"V": 0.5, "S": 0.1, "N": 0.3, "U": 0.3}, n_tasks=3, seed=5)
    res = A.analyze(write(tmp_path, df), draws=100)
    assert res["forks"] is None and res["recurrence"] is None
    tags = pd.DataFrame({"episode_id": df["episode_id"],
                         **{t: (df["cond"] == "V").astype(int) for t in A.TAGS}})
    tags.loc[0, "visible_tests"] = None
    res = A.analyze(write(tmp_path, df, tags=tags), draws=100)
    rec = res["recurrence"]
    assert rec["V"]["visible_tests"]["estimate"] == 1.0
    assert rec["S"]["visible_tests"]["estimate"] == 0.0
    assert rec["V"]["visible_tests"]["n"] == (df["cond"] == "V").sum() - 1
    assert rec["V"]["n_untagged"] == 0     # untagged = every tag missing


def test_process_section(tmp_path):
    rows = [row("A1", "A", "V", 1, 1, final_mentions_contradiction=0),
            row("A1", "A", "V", 2, 1, end_reason="turn_cap", turns_to_done=None),
            row("A1", "A", "V", 3, 0, turns_to_done=7)]
    res = A.analyze(write(tmp_path, pd.DataFrame(rows, columns=COLS)), draws=50)
    p = res["process"]["V"]
    assert p["n_done"] == 2 and p["mean_turns_to_done"] == pytest.approx(6.0)
    assert p["end_reason"] == {"done": 2, "turn_cap": 1}
    assert p["quiet_cheating"] == {"k": 1, "n": 2, "share": 0.5}


def test_cli_and_runtime(tmp_path, capsys):
    df = synth({"V": 0.4, "S": 0.2, "N": 0.3, "U": 0.3}, seed=9)      # 15 x 3 x 4 x 8 = 1,440
    assert len(df) == 1440
    write(tmp_path, df)
    t0 = time.perf_counter()
    assert A.main(["--results-dir", str(tmp_path)]) == 0
    elapsed = time.perf_counter() - t0
    out = json.loads((tmp_path / "analysis.json").read_text())
    assert out["draws"] == 10_000 and set(out["contrasts"]) >= {"V-S", "N-U", "V0-S0"}
    assert "V-S" in capsys.readouterr().out
    assert elapsed < 60
