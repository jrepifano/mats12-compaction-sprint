"""P15 orchestrator pieces that run without a model (patch plan, item loading in dry-run mode)."""

import numpy as np
from p15 import pod_main, tasks

ADD = [f"Add {i}: start {{X}}, add {{Y}}, each brings {{Z}}." for i in range(10)]
SUB = [f"Sub {i}: start {{X}}, lose {{Y}}, each brings {{Z}}." for i in range(10)]


def _T_and_vecs(d=8):
    T = tasks.make_T(ADD, SUB)
    rng = np.random.default_rng(0)
    vecs = rng.standard_normal((len(T), 7, d)).astype(np.float16)
    return T, vecs


def test_patch_plan_targets_and_donors():
    T, vecs = _T_and_vecs()
    jobs = pod_main.patch_plan(T, vecs)
    by = {}
    for j in jobs:
        by.setdefault((j["i"], j["lat"], j["qty"]), {})[j["cond"]] = j
    assert len({i for i, _, _ in by}) == pod_main.N_PATCH
    tid = np.array([t["template_id"] for t in T])
    assert {lat for _, lat, _ in by} == {2, 3, 4, 5}
    for (i, lat, qty), conds in by.items():
        assert set(conds) == {"diff", "same", "random", "none"}
        targets = {c["target"] for c in conds.values()}
        assert len(targets) == 1  # one counterfactual target shared by every condition
        it = T[i]
        target = targets.pop()
        assert target != it["gold"]
        if qty == "s2":
            s2p = target - it["s1"]
            donors = (tid != it["template_id"]) & np.array([t["s2"] == s2p for t in T])
            want = vecs[donors, lat].astype(np.float32).mean(0)
        else:
            assert target % (it["Z"] + 1) == 0  # s1' + s1' * Z
            s1p = target // (it["Z"] + 1)
            donors = np.array([t["template_id"] != it["template_id"] and t["op"] == it["op"]
                               and t["Z"] == it["Z"] and t["s1"] == s1p for t in T])
            want = vecs[donors, lat].astype(np.float32).mean(0)
        assert donors.any()
        np.testing.assert_allclose(conds["diff"]["vec"], want, rtol=1e-5)
        own = np.linalg.norm(vecs[i, lat].astype(np.float32))
        assert abs(np.linalg.norm(conds["random"]["vec"]) - own) < 1e-3 * own
        assert conds["none"]["vec"] is None


def test_patch_plan_is_seeded():
    T, vecs = _T_and_vecs()
    a = [(j["i"], j["lat"], j["cond"], j["target"]) for j in pod_main.patch_plan(T, vecs)]
    b = [(j["i"], j["lat"], j["cond"], j["target"]) for j in pod_main.patch_plan(T, vecs)]
    assert a == b


def test_patch_plan_shares_targets_across_indices():
    T, vecs = _T_and_vecs()
    tg = {}
    for j in pod_main.patch_plan(T, vecs):
        tg.setdefault((j["i"], j["qty"]), set()).add(j["target"])
    assert all(len(v) == 1 for v in tg.values())  # primary and convention-check index agree
