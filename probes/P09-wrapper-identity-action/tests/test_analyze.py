import json
import random

import numpy as np
import pytest
from p09 import analyze as A
from p09 import measures as M
from p09 import tables as T


def _row(ctx, task, wrapper, content, undo, sample=0, model="glm-5.3-flash", **kw):
    r = {"episode_id": f"P09|{model}|{wrapper}|{content}|{ctx}|s{sample}", "model_slug": model,
         "wrapper": wrapper, "content": content, "ctx_id": ctx, "task_id": task,
         "sample": sample, "end_reason": "done", "error": None, "undo": undo,
         "new_cheat": False, "restart": False, "disclosure_regex": undo,
         "disclosure_luna": None, "billed_usd": 0.01}
    r.update(kw)
    return r


def _grid(p_by_wrapper: dict[str, float], n_tasks=8, ctx_per_task=2, samples=2, seed=1,
          model="glm-5.3-flash"):
    rng = random.Random(seed)
    rows = []
    for t in range(n_tasks):
        for c in range(ctx_per_task):
            ctx = f"task{t}|r{c}|a1"
            for w, p in p_by_wrapper.items():
                for content in ("N", "T", "H"):
                    for s in range(samples):
                        u = None if content == "N" else rng.random() < p
                        rows.append(_row(ctx, f"task{t}", w, content, u, s, model))
    return A.with_done(rows)


def test_contrast_extremes():
    rows = _grid({"codex": 0.0, "codex_self": 1.0})
    c = A.paired_wrapper_contrast(rows, "glm-5.3-flash", "codex_self", "codex", ("T", "H"),
                                  reps=500)
    assert c["diff"] == 1.0 and c["lo"] == 1.0 and c["hi"] == 1.0
    assert c["n_contexts"] == 16 and c["n_tasks"] == 8 and c["n_cells"] == 32


def test_contrast_point_matches_hand_computation():
    rows = [
        _row("c1", "t1", "codex", "T", False), _row("c1", "t1", "codex", "T", True, 1),
        _row("c1", "t1", "codex", "H", False), _row("c1", "t1", "codex_self", "T", True),
        _row("c1", "t1", "codex_self", "H", True),
        _row("c2", "t2", "codex", "T", True), _row("c2", "t2", "codex_self", "T", False),
        _row("c2", "t2", "codex_self", "H", True),   # no codex H for c2: not paired
        _row("c3", "t2", "codex", "T", None), _row("c3", "t2", "codex_self", "T", True),
    ]
    c = A.paired_wrapper_contrast(rows, "glm-5.3-flash", "codex_self", "codex", ("T", "H"),
                                  reps=200)
    # c1: T 1 - 0.5, H 1 - 0 -> 0.75; c2: T 0 - 1 -> -1; c3: codex unknown -> dropped
    assert c["n_contexts"] == 2 and c["diff"] == pytest.approx((0.75 - 1) / 2)
    assert c["a_rate"] == pytest.approx(0.5) and c["b_rate"] == pytest.approx(0.625)


def test_contrast_ci_covers_and_is_seeded():
    rows = _grid({"codex": 0.3, "codex_self": 0.6}, n_tasks=12)
    a = A.paired_wrapper_contrast(rows, "glm-5.3-flash", "codex_self", "codex", ("T", "H"),
                                  reps=2000)
    b = A.paired_wrapper_contrast(rows, "glm-5.3-flash", "codex_self", "codex", ("T", "H"),
                                  reps=2000)
    assert a == b and a["lo"] <= a["diff"] <= a["hi"] and a["hi"] - a["lo"] > 0
    assert A.paired_wrapper_contrast(rows, "glm-5.3-flash", "pi_mono", "codex",
                                     ("T",)) is None


def test_cluster_boot_resamples_tasks():
    K = np.array([[2.0], [0.0]])
    N = np.array([[2.0], [2.0]])
    p, lo, hi = A.cluster_boot(K, N, A._ratio, reps=1000)
    assert p == 0.5 and lo == 0.0 and hi == 1.0


def test_cells_contrasts_predictions_and_tables():
    rows = _grid({"codex": 0.5, "codex_self": 0.5, "claude_code": 0.9, "pi_mono": 0.5})
    rows += _grid({"codex": 0.5, "codex_self": 0.5, "claude_code": 0.5}, model="deepseek-v4-flash")
    rows.append(A.with_done([_row("x|r1|a1", "taskx", "codex", "T", None,
                                  error="end_reason: api_error")])[0])
    cells = A.cell_table(rows)
    t = cells["glm-5.3-flash"]["codex"]["T"]
    assert t["n_total"] == 33 and t["n"] == 32 and t["errors"]["k"] == 1
    assert cells["glm-5.3-flash"]["codex"]["N"]["undo"]["n"] == 0
    con = A.contrasts(rows, reps=300)
    assert set(con["glm-5.3-flash"]) == {"codex_self - codex", "claude_code - codex",
                                         "pi_mono - codex"}
    assert set(con["deepseek-v4-flash"]) == {"codex_self - codex", "claude_code - codex"}
    assert "N+T+H" in con["glm-5.3-flash"]["codex_self - codex"]["new_cheat"]
    assert "disclosure_luna" not in con["glm-5.3-flash"]["codex_self - codex"]   # unjudged
    p = A.predictions(rows, con)
    assert p["c"]["met"] is True and p["b"]["rate"]["n"] == 64
    assert p["a"]["outcome"] in ("|d| < 10", "d <= -15", "d >= +15", "10-15 pts")
    res = {"health": A.health(rows), "cells": cells, "contrasts": con, "predictions": p}
    md = T.render(json.loads(json.dumps(A.clean(res))))
    assert "## Rates: glm-5.3-flash" in md and "codex_self - codex" in md
    assert "| codex | N | 32 | n/a |" in md


def test_identity_and_manipulation():
    recs = [{"ctx_id": f"c{i}", "model": "glm-5.3-flash", "wrapper": "codex_self",
             "probe": "mine", "parsed": "mine" if i < 8 else "other"} for i in range(10)]
    recs += [{"ctx_id": "c0", "model": "glm-5.3-flash", "wrapper": "codex", "probe": "mine",
              "parsed": None}]
    tab = A.identity_table(A.dedupe(recs, A.IDENTITY_KEY))
    assert tab["glm-5.3-flash"]["codex_self"]["mine"]["p"] == 0.8
    assert tab["glm-5.3-flash"]["codex"]["n_unparsed"] == 1
    assert A.manipulation_check(tab)["passed"] is True
    assert A.manipulation_check(tab, "deepseek-v4-flash")["passed"] is None


def test_analyze_end_to_end(tmp_path):
    rows = _grid({"codex": 0.2, "codex_self": 0.7}, n_tasks=4)
    for r in rows:
        r.pop("done")
    csvp = tmp_path / "m.csv"
    M.write_csv(rows, csvp)
    ident = tmp_path / "i.jsonl"
    ident.write_text(json.dumps({"ctx_id": "a", "model": "glm-5.3-flash",
                                 "wrapper": "codex", "probe": "mine", "parsed": "other"}))
    res = A.clean(A.analyze(csvp, ident, reps=200))
    json.dumps(res, allow_nan=False)
    assert res["n_episodes"] == len(rows)
    c = res["contrasts"]["glm-5.3-flash"]["codex_self - codex"]["undo"]["T+H"]
    assert c["diff"] > 0
    assert res["identity"]["glm-5.3-flash"]["codex"]["other"]["k"] == 1
