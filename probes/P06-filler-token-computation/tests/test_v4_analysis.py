"""Unit tests for P06 F8's laptop analysis (`p06.v4_analysis`) on synthetic data with planted
signals: accuracy and uplift, the readout permutation tests, serial order, heatmaps, patching
rates and placebos, the MC readout, and the end-to-end `analyze` over written npz files."""

from __future__ import annotations

import json

import numpy as np
import pytest
from p06 import v4_analysis as A
from p06 import v4bulk as VB

N_L, N_LABEL, N_FILL, N_SUF = 30, 2, 20, 3


def _groups(n_problem=0):
    return np.array([0] * N_LABEL + [2] * n_problem + [1] * N_FILL + [3] * N_SUF, dtype=np.int8)


def _chain_recs(n=40, k=3, arm="dots500", seed=0, plant=True, n_problem=0):
    """k=3 items with distinct start/step1/step2/answer; step1 planted (+12) at layer 25, dot 3;
    step2 (+8) at layer 28, dot 15."""
    rng = np.random.default_rng(seed)
    recs = []
    for i in range(n):
        traj = list(rng.choice(np.arange(1, 21), size=k + 1, replace=False))
        g = _groups(n_problem + (i % 3 if n_problem else 0))
        lp = rng.normal(size=(N_L, len(g), 21)).astype(np.float32)
        if plant:
            f = np.flatnonzero(g == 1)
            lp[25, f[3], traj[1]] += 12
            if k == 3:
                lp[28, f[15], traj[2]] += 8
        rec = {"item_id": f"chain:k{k}:{i}", "kind": "chain", "arm": arm, "k": k, "answer": traj[-1],
               "traj": np.array(traj), "groups": g, "cand_values": np.arange(21),
               "readers": ["logit_lens"],
               "ro": {"logit_lens": (lp.astype(np.float16), np.zeros(lp.shape[:2], np.int32))}}
        recs.append(rec)
    return recs


def test_stack_drops_problem_rows_and_checks_pattern():
    recs = _chain_recs(n=6, arm="pre500", n_problem=4)
    lp, g = A.stack(recs, "logit_lens")
    assert lp.shape == (6, N_L, N_LABEL + N_FILL + N_SUF, 21) and not (g == 2).any()
    recs[2]["groups"] = recs[2]["groups"].copy()
    recs[2]["groups"][0] = 1
    with pytest.raises(ValueError):
        A.stack(recs, "logit_lens")


def test_readout_tests_find_planted_step():
    recs = _chain_recs()
    ans = [{"item_id": r["item_id"], "arm": a, "correct": (a == "dots500") == (i % 2 == 0)}
           for i, r in enumerate(recs) for a in ("dots500", "pre500")]
    out = A.readout_tests(recs, ans, l0s=(0, 22), kinds=("logprob",))
    hit = out["logit_lens|dots500|k3|L0=22|logprob|filler|step1"]
    assert hit["all"]["hit_rate"] == 1.0 and hit["all"]["p"] < 0.001
    assert hit["all"]["null_mean"] < 0.2
    assert hit["uplift_items"]["n"] == 20 and hit["other_items"]["n"] == 20
    # the planted cells are in the filler: the label rows see nothing
    lab = out["logit_lens|dots500|k3|L0=22|logprob|label|step1"]["all"]
    assert lab["hit_rate"] < 0.3 and lab["p"] > 0.01
    # the second step is never the argmax (step1 is planted higher)
    assert out["logit_lens|dots500|k3|L0=0|logprob|filler|step2"]["all"]["hit_rate"] == 0.0
    assert set(A.chain_roles(3)) == {"start", "step1", "step2", "answer"}


def test_null_holds_without_signal():
    recs = _chain_recs(plant=False, seed=3)
    out = A.readout_tests(recs, [], l0s=(0,), kinds=("prob",))
    p = [row["all"]["p"] for row in out.values()]
    assert min(p) > 0.001 and np.mean([row["all"]["hit_rate"] for row in out.values()]) < 0.2


def test_serial_order_detects_order():
    recs = _chain_recs()
    so = A.serial_order(recs, "logit_lens", 0)
    assert so["n_items"] == 40 and so["step1_hit"] == 40 and so["both_in_top2"] == 40
    assert so["pos_step2_minus_step1"]["median"] == 12 and so["pos_step2_minus_step1"]["n_pos"] == 40
    assert so["pos_step2_minus_step1"]["sign_p"] < 1e-6
    assert so["layer_step2_minus_step1"]["median"] == 3
    assert so["spearman_step_vs_pos"]["rho"] == pytest.approx(1.0)
    s = A.sign_and_wilcoxon(np.array([0, 0, 1, -1]))
    assert s["n_zero"] == 2 and s["sign_p"] == 1.0


def test_heatmaps_shape_and_peak():
    hm = A.heatmaps(_chain_recs())
    v = hm["logit_lens|dots500|k3|step1"]
    labels = list(hm["bins|dots500"])
    assert v.shape == (N_L, N_LABEL + min(A.N_BINS, N_FILL) + N_SUF) and len(labels) == v.shape[1]
    assert not np.isnan(v).any()
    layer, col = np.unravel_index(v.argmax(), v.shape)
    assert layer == 25 and labels[col].startswith("filler")


def test_accuracy_and_uplift():
    rows = []
    for i in range(30):
        for arm, ok in (("dots500", i < 20), ("pre500", i < 10), ("base", i < 5)):
            rows.append({"item_id": f"chain:k2:{i}", "k": 2, "arm": arm, "correct": ok})
    api = {(f"chain:k2:{i}", a): v for i in range(30)
           for a, v in (("dots500", 0.5), ("pre500", 0.5), ("base", 0.0))}
    acc = A.accuracy(rows, api)
    row = acc["2"]
    assert row["acc"]["dots500"] == pytest.approx(20 / 30)
    assert row["dots500-pre500"]["mean"] == pytest.approx(10 / 30)
    assert row["dots500-pre500"]["lo"] > 0 and row["dots500-pre500_api"]["mean"] == 0.0
    assert acc["pooled"]["n_items"] == 30


def test_api_item_acc_maps_arms(tmp_path):
    p = tmp_path / "api.jsonl"
    rows = [{"key": f"k{i}", "status": "ok", "item_id": iid, "arm": arm, "correct": c}
            for i, (iid, arm, c) in enumerate([
                ("varbind_mc10:4", "base", True), ("varbind_mc10:4", "dots100", False),
                ("varbind_mc5:4", "base", False), ("varbind:4", "pre100", True)])]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    m = A.api_item_acc(p, {"varbind:4"}, {a: v for a, v in A.MC_API.items()})
    assert m == {("varbind:4", "mc10_base"): 1.0, ("varbind:4", "mc10_dots100"): 0.0,
                 ("varbind:4", "pre100"): 1.0}


def test_patching_rates_and_placebos():
    rows = []
    for i in range(20):
        rows.append({"item_id": f"c{i}", "k": 2 + i % 2, "answer": 10, "target_pred": 10,
                     "donor_answer": 4, "donor_pred": 4, "tx_pred": 4 if i < 12 else 10,
                     "rand_answer": 15, "rand_pred": 15, "rtx_pred": 10 if i < 18 else 15,
                     **({"self_changed": i == 0} if i < 5 else {})})
    rows.append({"item_id": "x", "k": 2, "answer": 3, "target_pred": 3, "donor_error": "none",
                 "rand_answer": 5, "rand_pred": 5, "rtx_pred": 3})
    p = A.patching(rows)
    m = p["all"]["matched"]
    assert p["n_matched"] == 20 and p["donor_errors"] == 1
    assert m["eq_donor_answer"]["rate"] == 0.6 and m["changed"]["rate"] == 0.6
    assert m["toward_donor"]["rate"] == 0.6 and m["eq_target_answer"]["rate"] == 0.4
    assert p["all"]["random"]["eq_rand_answer"]["rate"] == 0.1
    pl = p["all"]["placebo_toward_matched_donor"]
    assert pl["unpatched"]["rate"] == 0.0 and pl["matched_minus_unpatched"]["mean"] == 0.6
    assert p["self_patch"] == {"n": 5, "changed": 1}
    assert p["both_correct"]["matched"]["eq_donor_answer"]["n"] == 20


def _mc_recs(n=30, arm="mc10_dots100", seed=0, distractor_bump=1.0):
    rng = np.random.default_rng(seed)
    recs = []
    for i in range(n):
        vals = list(rng.choice(np.arange(100, 999), size=25, replace=False))
        roles, opts, ctrl = vals[:5], [vals[4]] + vals[5:14], vals[14:24]
        cand = sorted(set(vals[:24]))
        g = _groups() if arm != "mc10_base" else np.array([0, 0, 2, 2, 3, 3, 3], dtype=np.int8)
        lp = rng.normal(size=(N_L, len(g), len(cand))).astype(np.float32)
        ci = {v: j for j, v in enumerate(cand)}
        rows = np.flatnonzero(g == (1 if arm != "mc10_base" else 3))
        for v in opts[1:]:
            lp[25, rows[2], ci[v]] += distractor_bump
        lp[25, rows[1], ci[opts[0]]] += 8  # the answer
        lp[26, rows[0], ci[roles[2]]] += 8  # y
        recs.append({"item_id": f"varbind:{i}", "kind": "mc", "idx": i, "arm": arm,
                     "answer": opts[0], "role_names": list(VB.MC_ROLES),
                     "role_values": np.array(roles), "options": np.array(opts),
                     "controls": np.array(ctrl), "cand_values": np.array(cand), "groups": g,
                     "readers": ["jpp"], "ro": {"jpp": (lp.astype(np.float16),
                                                        np.zeros(lp.shape[:2], np.int32))}})
    return recs


def test_mc_analysis():
    out = A.mc_analysis(_mc_recs(distractor_bump=4.0) + _mc_recs(arm="mc10_base"), readers=("jpp",))
    r = out["jpp|mc10_dots100|L0=22"]
    assert r["group"] == "filler" and r["n"] == 30
    assert r["distractors_minus_controls"]["lo"] > 0
    assert r["answer_hit_among_options"]["rate"] == 1.0 and r["answer_mean_rank"] == 1.0
    assert r["y_hit_among_y_and_controls"]["rate"] == 1.0 and r["y_chance"] == 1 / 11
    assert out["jpp|mc10_base|L0=0"]["group"] == "suffix"
    flat = A.mc_analysis(_mc_recs(distractor_bump=0.0, seed=5), readers=("jpp",))
    d = flat["jpp|mc10_dots100|L0=0"]["distractors_minus_controls"]
    assert d["lo"] < 0 < d["hi"] or abs(d["mean"]) < 0.3


def test_analyze_end_to_end(tmp_path):
    root = tmp_path / "v4bulk"
    for r in _chain_recs(n=8):
        rec = {**r, "positions": np.arange(len(r["groups"])),
               "slots": np.arange(-len(r["groups"]), 0), "token_ids": np.zeros(len(r["groups"])),
               "n_prompt": 100, "gen_ids": np.array([1]), "gen_text": "7",
               "cand_ids": np.arange(21)}
        VB.write_rec(VB.rec_path(root / "readouts" / "chain", r["item_id"], r["arm"]), rec,
                     VB.CHAIN_KEYS)
    for r in _mc_recs(n=5):
        rec = {**r, "positions": np.arange(len(r["groups"])),
               "slots": np.arange(-len(r["groups"]), 0), "token_ids": np.zeros(len(r["groups"])),
               "n_prompt": 100, "gen_ids": np.array([1]), "gen_text": "7",
               "cand_ids": np.arange(len(r["cand_values"]))}
        VB.write_rec(VB.rec_path(root / "readouts" / "mc", r["item_id"], r["arm"]), rec,
                     VB.MC_KEYS)
    ans = [{"item_id": f"chain:k3:{i}", "k": 3, "arm": a, "correct": a == "dots500"}
           for i in range(8) for a in ("dots500", "pre500")]
    (root / "chain_answers.jsonl").write_text("\n".join(json.dumps(r) for r in ans) + "\n")
    (root / "mc_answers.jsonl").write_text(json.dumps(
        {"item_id": "varbind:0", "arm": "mc10_dots100", "correct": True}) + "\n")
    res, hm = A.analyze(root, A.STAGE1)
    assert res["accuracy"]["3"]["dots500-pre500"]["mean"] == 1.0
    assert "logit_lens|dots500|k3|L0=22|prob|all|step1" in res["readouts"]
    assert res["serial_order"]["logit_lens|L0=0"]["both_in_top2"] == 8
    assert res["mc_accuracy"]["mc10_dots100"]["n"] == 1 and "jpp|mc10_dots100|L0=0" in \
        res["mc_readout"]
    assert res["patching"]["n"] == 0 and hm
    lines = A.summary(res)
    assert any(s.startswith("acc k=3") for s in lines) and any("serial" in s for s in lines)
    json.dumps(res, default=float)
