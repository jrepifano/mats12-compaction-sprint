import numpy as np
import pandas as pd

from emx.p04 import analyze as A


def test_auroc_perfect_and_chance():
    assert A.auroc(np.array([0.1, 0.2, 0.9, 0.8]), np.array([0, 0, 1, 1])) == 1.0
    # tied scores -> 0.5
    assert A.auroc(np.array([0.5, 0.5, 0.5, 0.5]), np.array([0, 0, 1, 1])) == 0.5
    assert np.isnan(A.auroc(np.array([0.1, 0.2]), np.array([0, 0])))


def _success_tbl():
    rows = []
    for t in range(20):
        rows.append({"model": "m", "arm": "off", "task_id": str(t),
                     "k1_success": int(t < 2), "bok_success": int(t < 4)})
        rows.append({"model": "m", "arm": "on", "task_id": str(t),
                     "k1_success": int(t < 6), "bok_success": int(t < 10)})
    return pd.DataFrame(rows)


def test_success_rates_and_uplift():
    tbl = _success_tbl()
    sr = A.success_rates(tbl, "bok_success")
    off = sr[(sr.arm == "off")].iloc[0]
    on = sr[(sr.arm == "on")].iloc[0]
    assert abs(off["rate"] - 0.2) < 1e-9 and abs(on["rate"] - 0.5) < 1e-9
    assert off["lo"] <= off["rate"] <= off["hi"]
    up = A.uplift(tbl, "bok_success")
    assert abs(up.iloc[0]["uplift"] - 0.3) < 1e-9
    assert up.iloc[0]["lo"] <= up.iloc[0]["uplift"] <= up.iloc[0]["hi"]


def _monitor_tbl():
    rows = []
    for t in range(30):
        # successful backdoors score high, benign low, with a little overlap
        rows.append({"monitor": "luna", "condition": "code", "task_id": str(t),
                     "label": 1, "score": 60 + (t % 5) * 5})
        rows.append({"monitor": "luna", "condition": "code", "task_id": str(t),
                     "label": 0, "score": 10 + (t % 5) * 5})
    return pd.DataFrame(rows)


def test_monitor_auroc_and_catch():
    tbl = _monitor_tbl()
    au = A.monitor_auroc(tbl).iloc[0]
    assert au["auroc"] > 0.9
    cr = A.catch_rate(tbl, fpr=0.1).iloc[0]
    assert 0.0 <= cr["catch_rate"] <= 1.0
    assert cr["realised_fpr"] <= 0.2  # near the target


def test_leakage_summary():
    g = pd.DataFrame([
        {"model": "m", "arm": "off", "w1_present": True, "w1_reasoning_tokens": 0,
         "w2_hidden": 0, "content_cot": False, "prose_chars_before_code": 0, "comment_lines": 1},
        {"model": "m", "arm": "off", "w1_present": True, "w1_reasoning_tokens": 0,
         "w2_hidden": 0, "content_cot": True, "prose_chars_before_code": 40, "comment_lines": 3},
        {"model": "m", "arm": "on", "w1_present": True, "w1_reasoning_tokens": 500,
         "w2_hidden": 0, "content_cot": True, "prose_chars_before_code": 0, "comment_lines": 2},
    ])
    s = A.leakage_summary(g)
    off = s[s.arm == "off"].iloc[0]
    assert off["w1_present_frac"] == 1.0 and off["w1_zero_frac_of_present"] == 1.0
    assert abs(off["content_cot_frac"] - 0.5) < 1e-9
