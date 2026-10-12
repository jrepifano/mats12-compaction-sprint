"""Unit tests for P06 F8's exploratory checks (`p06.v4_extra`) on synthetic records with planted
top-1 tokens: the vocabulary-wide hit test and its permutation null, the first-layer order test,
the first-dots restriction and the exact-answer patch rates."""

from __future__ import annotations

import numpy as np
from p06 import v4_extra as X

N_L, N_FILL = 10, 30
VOCAB0 = 1000  # token id of value v is VOCAB0 + v


def _mc_rec(i, plant=True, arm="dots100"):
    rng = np.random.default_rng(i)
    vals = [int(v) for v in rng.choice(np.arange(10, 99), size=5, replace=False)]
    g = np.array([0, 0] + [1] * N_FILL + [3], dtype=np.int8)
    top = rng.integers(0, 500, size=(N_L, len(g))).astype(np.int32)  # never a value token
    if plant:
        f = np.flatnonzero(g == 1)
        top[3, f[1]] = VOCAB0 + vals[2]  # y at layer 3
        top[5, f[2]] = VOCAB0 + vals[3]  # c2y at layer 5
        top[7, f[0]] = VOCAB0 + vals[4]  # answer at layer 7
    cand = sorted(set(vals))
    return {"item_id": f"varbind:{i}", "idx": i, "arm": arm, "groups": g,
            "role_names": np.array(X.MC_ROLES), "role_values": np.array(vals),
            "cand_values": np.array(cand), "cand_ids": np.array([VOCAB0 + v for v in cand]),
            "ro": {r: (None, top) for r in X.READERS}}


def test_top1_hits_planted_vs_null():
    recs = [_mc_rec(i) for i in range(30)]
    res = X.varbind_checks(recs)["top1|jlens|dots100|filler"]
    assert res["y"]["hit_rate"] == 1.0 and res["y"]["null_mean"] < 0.2 and res["y"]["p"] < 0.01
    assert res["x"]["hit_rate"] == 0.0  # x never planted


def test_layer_order_signs():
    recs = [_mc_rec(i) for i in range(30)]
    order = X.varbind_checks(recs)["order|logit_lens|dots100"]
    assert order["c2y-y"]["n_pos"] == 30 and order["c2y-y"]["median"] == 2
    assert order["answer-c2y"]["median"] == 2 and order["answer-y"]["median"] == 4
    assert order["answer-y"]["sign_p"] < 1e-6


def test_no_signal_unplanted():
    recs = [_mc_rec(i, plant=False) for i in range(20)]
    res = X.varbind_checks(recs)
    assert res["top1|jpp|dots100|filler"]["y"]["hit_rate"] == 0.0
    assert res["order|jpp|dots100"]["c2y-y"]["n"] == 0


def test_first_dots_restriction():
    r = _mc_rec(0)
    sel = X.top1_cells(r, "jlens", "filler", first=1)
    assert sel.shape == (N_L, 1)
    assert X.first_layer(sel, VOCAB0 + int(r["role_values"][4])) == 7  # answer is at dot 0
    assert X.first_layer(sel, VOCAB0 + int(r["role_values"][2])) is None  # y is at dot 1


def test_chain_roles_and_patch_exact():
    assert X.chain_roles({"traj": np.array([3, 5, 9, 2])}) == {
        "start": 3, "step1": 5, "step2": 9, "answer": 2}
    rows = [{"k": 2, "tx_pred": 4, "donor_answer": 4, "rtx_pred": 1, "target_pred": 7,
             "donor_pred": 4, "rand_pred": 1},
            {"k": 2, "tx_pred": 5, "donor_answer": 4, "rtx_pred": 4, "target_pred": 7,
             "donor_pred": 6, "rand_pred": 2}]
    p = X.patch_exact(rows)["all"]
    assert p["matched_patch_eq_donor_answer"]["rate"] == 0.5
    assert p["random_patch_eq_matched_donor_answer"]["rate"] == 0.5
    assert p["unpatched_eq_matched_donor_answer"]["rate"] == 0.0
    assert p["matched_patch_eq_donor_output"]["rate"] == 0.5
    assert p["random_patch_eq_random_output"]["rate"] == 0.5
