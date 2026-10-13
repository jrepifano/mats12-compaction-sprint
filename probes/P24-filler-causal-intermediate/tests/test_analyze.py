"""p24.analyze on synthetic records."""

import copy

import numpy as np
from p24 import analyze as A


def rec(idx, *, ans=100, alt=130, preds=None, ref_pred=100, r_pred=100, r_answer=500):
    """One item: donors X, Y, S share alt; preds {kind: patched output at the dots}."""
    preds = preds or {}
    lp = [0.0] * 10
    patched = [{"kind": k, "site": "dots", "batch": 0, "text": str(preds.get(k, ref_pred)),
                "pred": preds.get(k, ref_pred), "lp0": lp} for k in ("X", "Y", "S")]
    patched.append({"kind": "R", "site": "dots", "batch": 0, "text": str(r_pred), "pred": r_pred,
                    "lp0": lp})
    patched.append({"kind": "self", "site": "all", "batch": 0, "text": str(ref_pred),
                    "pred": ref_pred, "lp0": lp})
    don = {k: {"pred": alt if k != "S" else ans, "answer": alt if k != "S" else ans,
               "a_alt": alt} for k in ("X", "Y", "S")}
    don["R"] = {"pred": r_answer, "answer": r_answer, "a_alt": r_answer}
    return {"idx": idx, "answer": ans, "ref": {"text": str(ref_pred), "pred": ref_pred,
                                               "lp0": lp},
            "donors": don, "patched": patched, "refs": [{"batch": 0, "text": str(ref_pred)}],
            "self_eq_ref": True, "refs_agree": True}


def test_toward():
    assert A.toward(129, 130, 100) == 1.0 and A.toward(110, 130, 100) == 0.0
    assert A.toward(115, 130, 100) == 0.0 and A.toward(None, 130, 100) == 0.0


def test_placebo_ignores_r_own_answer():
    a = [rec(i, preds={"X": 130, "Y": 128}, r_pred=125, r_answer=500) for i in range(20)]
    b = copy.deepcopy(a)
    for r in b:
        r["donors"]["R"]["answer"] = r["donors"]["R"]["a_alt"] = 3
    sa, sb = A.summarise(a, n_boot=200), A.summarise(b, n_boot=200)
    assert sa["cells"]["all/Y/dots"]["placebo_R"] == sb["cells"]["all/Y/dots"]["placebo_R"]
    assert sa["cells"]["all/Y/dots"]["placebo_R"]["mean"] == 1.0  # 125 is closer to 130


def test_y_pattern_outcomes():
    recs = [rec(i, preds={"X": 130, "Y": 130} if i % 2 == 0 else {}) for i in range(200)]
    s = A.summarise(recs, n_boot=500)
    y = s["cells"]["all/Y/dots"]["effect"]
    assert y["mean"] == 0.5 and y["lo"] > 0.3
    assert s["cells"]["all/S/dots"]["effect"]["mean"] == 0.0
    assert s["contrasts"]["Y-S/dots"]["mean"] == 0.5
    assert s["ratio_Y_over_X"]["dots"]["ratio"] == 1.0
    o = s["outcomes"]
    assert o["a_X_replicates (X - placebo >= 0.20)"]
    assert o["c_y_pattern (b, S <= 0.05 with hi <= 0.10, Y - S >= 0.10 CI excl 0)"]
    assert not o["d_copy_pattern (Y < 0.05 and S >= 0.10)"]


def test_copy_pattern_and_merge_of_passes():
    recs = [rec(i, preds={"X": 130, "S": 130} if i % 2 == 0 else {}) for i in range(200)]
    p2 = []
    for r in recs[:50]:
        q = copy.deepcopy(r)
        for p in q["patched"]:
            p["site"] = "label" if p["kind"] != "self" else "all"
        p2.append(q)
    s = A.summarise(recs + p2, n_boot=300)
    assert s["n_items"] == 200 and s["cells"]["all/X/label"]["toward"]["n"] == 50
    assert s["outcomes"]["d_copy_pattern (Y < 0.05 and S >= 0.10)"]


def test_boot_drops_nan():
    out = A.boot(np.array([1.0, np.nan, 0.0]), np.random.default_rng(0), n=100)
    assert out["n"] == 2 and out["mean"] == 0.5
