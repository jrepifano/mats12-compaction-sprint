"""P14 tests: prompt sets, selection and thresholds, probes, the recorder, and the pod driver end
to end on CPU (a tiny random Qwen2 stands in for every model; the real tokenizer, notices,
questions and E13 cue pairs; fake TEST pairs and text scores)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import pytest

PROBE = Path(__file__).resolve().parents[1]
REPO = PROBE.parents[1]
sys.path.insert(0, str(PROBE))
sys.path.insert(0, str(REPO / "tests"))

from p14 import common as K
from p14 import probes as PR
from p14 import select as SEL


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    from emx.qwen import registry as R

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


def fake_test_pairs(path: Path, n: int = 8) -> list[dict]:
    """E13 CAL pairs with altered text and P14-style ids (kept), plus one dropped row."""
    rows = []
    for k, p in enumerate(K.kept_pairs(K.REPO / K.CAL_PAIRS)[:n + 1]):
        r = dict(p)
        r["pair_id"] = p["pair_id"].replace("__r0_", "__r10_")
        r["unobserved"] = p["unobserved"] + f"\nshard: t{k}"
        r["observed"] = p["observed"] + f"\nshard: t{k}"
        r["regen"], r["near_dup_of"] = 10, ""
        if k == n:
            r["kept"], r["drop_reason"] = False, "near_duplicate"
        rows.append(r)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return rows


@pytest.fixture
def fake_inputs(tmp_path, monkeypatch):
    tp = tmp_path / "test_pairs.jsonl"
    fake_test_pairs(tp)
    monkeypatch.setattr(K, "TEST_PAIRS", tp)
    sets = K.build_sets()
    tx = K.suffix_texts(sets)
    rng = np.random.default_rng(0)
    scores = {i: {"T1": float(rng.random()), "T2": float(rng.integers(0, 101)),
                  "T3": None if j == 0 else float(rng.integers(0, 2) * 100)}
              for j, i in enumerate(sorted(tx))}
    ts = tmp_path / "text_scores.json"
    ts.write_text(json.dumps({"meta": {}, "scores": scores}))
    monkeypatch.setattr(K, "TEXT_SCORES", ts)
    return sets


# --- sets ----------------------------------------------------------------------------------------


def test_sets_counts_and_disjoint(fake_inputs):
    s = fake_inputs
    assert len(s["train"]) == 230 * 8 and len(s["dev"]) == 40 * 8
    assert len(s["cal"]) == 39 * 2 * 41 and len(s["test"]) == 8 * 2 * 41
    assert len(s["util"]) == 80 * K.UTIL_SAMPLES * 2 and len(s["s3"]) == 41
    assert {r["unobs"] for r in s["train"]} == {0, 1}
    assert all(r["suffix"] for n in ("train", "dev", "s1", "cal", "test", "util") for r in s[n])
    # the train labels follow the notice condition
    assert all(r["unobs"] == (r["cond"] == "noov") for r in s["train"])
    # both conditions of a UTIL (task, sample) share one TEST pair
    by = {}
    for r in s["util"]:
        by.setdefault((r["qid"], r["sample_idx"]), set()).add(r["pair_id"])
    assert all(len(v) == 1 for v in by.values())


def test_disjoint_check_refuses_shared_notice(fake_inputs):
    s = {k: list(v) for k, v in fake_inputs.items()}
    s["cal"] = s["cal"] + [dict(s["train"][0], set="cal")]
    with pytest.raises(ValueError):
        K.check_disjoint(s)


def test_gen_seed_shared_across_strengths():
    a = K.gen_seed("test", "noov", "x:unobserved", "q1", 0)
    assert a == K.gen_seed("test", "noov", "x:unobserved", "q1", 0)
    assert a != K.gen_seed("test", "noov", "x:unobserved", "q1", 1)


# --- selection -----------------------------------------------------------------------------------


def test_auroc_basic():
    assert SEL.auroc(np.array([1, 2, 3, 4.0]), np.array([0, 0, 1, 1])) == 1.0
    assert SEL.auroc(np.array([1, 1, 1, 1.0]), np.array([0, 0, 1, 1])) == 0.5
    assert np.isnan(SEL.auroc(np.array([1, np.nan]), np.array([0, 1])))


@pytest.mark.parametrize("rate", [0.1, 0.25])
def test_threshold_exact_rate_with_ties(rate):
    rng = np.random.default_rng(1)
    s = rng.integers(0, 3, 4000).astype(float)  # heavy ties
    thr, p = SEL.threshold(s, rate)
    u = SEL.tie_u([f"p{i}" for i in range(len(s))])
    got = SEL.fire(s, thr, p, u).mean()
    assert abs(got - rate) < 0.02
    exp = (s > thr).mean() + p * (s == thr).mean()
    assert abs(exp - rate) < 1e-9


def test_threshold_continuous():
    s = np.arange(100.0)
    thr, p = SEL.threshold(s, 0.1)
    assert (s > thr).sum() + p * (s == thr).sum() == pytest.approx(10)


def test_selection_and_disagreement():
    n = 400
    rng = np.random.default_rng(2)
    sets = np.array(["cal"] * 200 + ["test"] * 200)
    labels = np.array([0, 1] * 200)
    ids = [f"p{i}" for i in range(n)]
    good = labels + rng.normal(0, 0.5, n)
    bad = rng.normal(0, 1, n)
    det = {"A_org": {"lr|last|3": good, "lr|last|4": bad}, "T1": {"T1": bad},
           "ASK_org": {"ask": labels * 0.2 + rng.normal(0, 1, n)}}
    rec = SEL.selection(det, ids, sets, labels, (0.1, 0.25), 0.6)
    assert rec["detectors"]["A_org"]["config"] == "lr|last|3"
    assert rec["comparator"] in ("T1", "ASK_org") and not rec["kill"]
    for r in ("0.1", "0.25"):
        assert abs(rec["detectors"]["A_org"]["thresholds"][r]["cal_obs_rate"] - float(r)) < 0.03
    dis = SEL.disagreement(rec, det, ids, sets)
    assert len(dis) and set(sets[dis]) == {"test"}
    rec2 = SEL.selection({"T1": {"T1": bad}}, ids, sets, labels, (0.1,), 0.6)
    assert rec2["kill"]


# --- probes --------------------------------------------------------------------------------------


def test_probes_recover_a_planted_direction():
    rng = np.random.default_rng(3)
    n, L, d = 120, 2, 12
    y = np.array([0, 1] * (n // 2))
    groups = [f"g{i // 4}" for i in range(n)]  # 4 rows per notice, one label per notice
    y = np.array([int(g[1:]) % 2 for g in groups])
    X = rng.normal(0, 1, (n, L, d)).astype(np.float32)
    X[:, 1, 0] += 2.0 * y  # layer 1 carries the label
    params = PR.fit_all(X, y, groups, seed=0)
    sc = PR.score_all(X, params)
    assert sc.shape == (n, len(PR.TYPES), L)
    assert SEL.auroc(sc[:, PR.TYPES.index("lr"), 1], y) > 0.95
    assert SEL.auroc(sc[:, PR.TYPES.index("dm"), 1], y) > 0.95
    assert params[1]["lam"] in PR.LAMBDAS
    shuf = PR._shuffle_labels(y, groups, 0)
    assert sorted(shuf.tolist()) == sorted(y.tolist())
    for g in set(groups):  # one label per group survives the shuffle
        assert len({shuf[i] for i in range(n) if groups[i] == g}) == 1


# --- recorder ------------------------------------------------------------------------------------


def test_record_matches_contrastive(tok):
    import torch
    from e13.conftest import make_tiny_qwen
    from p14.acts import record

    from emx.contrastive import block_outputs_at, block_outputs_spans

    model = make_tiny_qwen()
    rng = np.random.default_rng(0)
    prompts = [rng.integers(100, 1000, k).tolist() for k in (5, 9, 7, 12)]
    spans = [(1, 3), (2, 8), (0, 7), (4, 5)]
    rec = record(model, prompts, spans=spans, logit_ids=[11, 22], batch_size=3)
    ref = block_outputs_at(model, prompts, [-1] * 4)
    refs = block_outputs_spans(model, prompts, spans)
    np.testing.assert_allclose(rec["last"].astype(np.float32), ref, atol=2e-3, rtol=2e-3)
    np.testing.assert_allclose(rec["span"].astype(np.float32), refs, atol=2e-3, rtol=2e-3)
    with torch.no_grad():
        for i, p in enumerate(prompts):
            lg = model(input_ids=torch.tensor([p])).logits[0, -1]
            np.testing.assert_allclose(rec["logits"][i], lg[[11, 22]].numpy(), atol=1e-4)


# --- pod driver ----------------------------------------------------------------------------------


def test_pod_driver_end_to_end(tok, tmp_path, monkeypatch, fake_inputs):
    import p14_pod as D
    from e13.conftest import make_tiny_qwen

    from emx.e13 import contracts as C

    n_layers = 16
    u = np.random.default_rng(1).standard_normal(16)
    dpath = tmp_path / "input.json"
    dpath.write_text(json.dumps({"u": (u / np.linalg.norm(u)).tolist(), "g": 0.5}))
    monkeypatch.setattr(D, "DIRECTION_PATH", dpath)
    for attr, sub in (("RES", "results"), ("LOGS", "logs")):
        monkeypatch.setattr(D, attr, tmp_path / sub)
    monkeypatch.setattr(D, "GEN", tmp_path / "results" / "gen")
    monkeypatch.setattr(D, "SCORES", tmp_path / "results" / "scores")
    monkeypatch.setattr(D, "MAX_NEW", 3)
    monkeypatch.setattr(D, "HOOK_MAX_NEW", 3)
    monkeypatch.setattr(D, "BATCH_GEN", 16)
    monkeypatch.setattr(D, "SAVE_LAYERS", (2, 14))
    monkeypatch.setattr(D, "DEV_MIN_AUROC", -1.0)  # a random tiny model separates nothing
    monkeypatch.setattr(K, "KILL_AUROC", -1.0)
    monkeypatch.setattr(D, "gpu_hours", lambda: 3.0)
    dq = K.dev_questions()[:1]
    bq = K.broad_questions()[:2]
    monkeypatch.setattr(K, "dev_questions", lambda root=None: dq)
    monkeypatch.setattr(K, "broad_questions", lambda root=None: bq)
    s1 = K.read_jsonl(K.S1_PROMPTS)[:12]
    monkeypatch.setattr(K, "s1_rows", lambda: s1)
    monkeypatch.setattr(K, "final_tasks", lambda root=None: [
        {"qid": "t1", "task_type": "umed", "question": "What helps a mild headache?"},
        {"qid": "t2", "task_type": "ugen", "question": "Name three primary colours."}])
    loads = []

    def fake_load(name):
        loads.append(name)
        return (make_tiny_qwen(n_layers=n_layers, seed=len(loads)),
                {"model": name, "adapter": K.MODELS[name], "load_s": 0.0})

    monkeypatch.setattr(D, "load_model", fake_load)
    monkeypatch.setattr(D, "free", lambda m: None)

    assert D.main([]) == 0
    assert loads == list(K.POD_ORDER)
    res = tmp_path / "results"
    checks = json.loads((res / "checks.json").read_text())
    assert checks["hook_checks"]["passed"]
    assert all(info["nmean_q_drift"] < 1e-3 for info in checks["loads"])
    sel = json.loads((res / "selection.json").read_text())
    assert set(sel["detectors"]) == {"A_org", "A_base", "ASK_org", "ASK_base", "T1", "T2", "T3"}
    assert sel["comparator"] in SEL.COMPARATORS
    plan = json.loads((res / "gen_plan.json").read_text())
    n_test = 8 * 2 * 2
    for name, n in (("test__a0", n_test), ("test__a4", n_test), ("test__a1", n_test),
                    ("util__a0", 2 * 2 * 2)):
        with (res / "gen" / f"{name}.csv").open(newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == n and list(rows[0]) == list(C.GEN_ALL_COLUMNS)
        assert (tmp_path / "logs" / "cells" / f"{name}.csv").is_file()
    a0 = {(r["instance_id"], r["qid"]): r["seed"] for r in
          csv.DictReader((res / "gen" / "test__a0.csv").open())}
    a4 = {(r["instance_id"], r["qid"]): r["seed"] for r in
          csv.DictReader((res / "gen" / "test__a4.csv").open())}
    assert a0 == a4  # shared seeds across strengths
    if plan["n_disagreement_prompts"]:
        with (res / "gen" / "extra__a4.csv").open(newline="") as f:
            ex = list(csv.DictReader(f))
        assert {int(r["sample_idx"]) for r in ex} == {1, 2, 3}
    for m in K.POD_ORDER:
        z = np.load(res / "scores" / f"{m}.npz")
        assert z["last"].shape[2] == n_layers and len(z["prompt_ids"]) == z["last"].shape[0]
    assert (res / "acts" / "org_s1.last.npy").is_file()
    # a rerun skips finished models and cells
    loads.clear()
    n_timing = len((res / "timing.jsonl").read_text().splitlines())
    assert D.main([]) == 0
    assert loads == [K.GEN_MODEL]
    assert len((res / "timing.jsonl").read_text().splitlines()) == n_timing


def test_every_pod_input_is_pushed():
    import tomllib

    push = set(tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["push"])
    needed = {K.NOTICES, K.CAL_PAIRS, K.TASKS, K.EXT48, K.GENERAL, K.DIRECTION,
              *(f"{K.ADAPTERS}/{k}" for k in K.MODELS.values() if k)}
    assert needed <= push, needed - push
    for p in push:
        assert (REPO / p).exists(), p
