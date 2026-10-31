"""compaction.s2.chain.analysis: Wilson, rewrite bounds, contrasts, cluster bootstrap, report."""

from __future__ import annotations

import json
import random

import numpy as np
import pytest

from compaction.s2.chain import analysis as A
from compaction.s2.chain import config as CC


def make_rows(seed: int = 0, **override) -> list[dict]:
    """One synthetic outcome row per real B run (config.b_runs())."""
    rnd = random.Random(seed)
    rows = []
    for br in CC.b_runs():
        c = br.cell
        cluster = br.history or f"plain:{c.b_task}:{br.rep}"
        perm_c = c.hist == "p" and c.start == "compacted"  # one category/exposure per history
        r = {"id": br.id, "b_task": c.b_task, "hist": c.hist, "project": c.project,
             "start": c.start, "cluster": cluster,
             "rewrite": rnd.choice(["yes", "no", "no", "unknown"]),
             "rejection": rnd.choice(["yes", "yes", "no", "unknown"]),
             "termination": rnd.choice(["submitted", "submitted", "capped", "failed"]),
             "pass_tip": rnd.choice([0, 1, None]), "pass_bonly": rnd.choice([0, 1, None]),
             "bonly_status": rnd.choice(["applies", "not_applies", "independent"]),
             "stale": rnd.choice([True, False]) if c.project == "same" else None,
             "teammate_checked": rnd.choice([True, False]), "tamper": rnd.choice([True, False]),
             "teammate_lost": rnd.choice([True, False]),
             "overclaim": rnd.choice([True, False, None]),
             "n_calls": rnd.randint(10, 100), "prompt_tokens": rnd.randint(1000, 9000),
             "completion_tokens": rnd.randint(100, 900), "cost_usd": rnd.random(),
             "perm_summary_category": (("kept_scope", "dropped")[br.rep % 2] if perm_c
                                       else None),
             "exposure": (("summary_or_none", "summary_or_none", "verbatim")[br.rep % 3]
                          if perm_c else None)}
        rows.append(r | override)
    return rows


def simple(task: str, cluster: str, arm: tuple[str, str, str], **kw) -> dict:
    h, p, s = arm
    return {"b_task": task, "cluster": cluster, "hist": h, "project": p, "start": s} | kw


def test_wilson_known_values():
    lo, hi = A.wilson(0, 16)
    assert lo == 0.0 and hi == pytest.approx(0.194, abs=1e-3)
    lo, hi = A.wilson(8, 16)
    assert lo == pytest.approx(0.280, abs=1e-3) and hi == pytest.approx(0.720, abs=1e-3)
    assert A.wilson(0, 0) == (None, None)
    lo, hi = A.wilson(16, 16)
    assert hi == 1.0 and lo == pytest.approx(1 - 0.194, abs=1e-3)


def test_rewrite_bounds_and_conditional_rate():
    rows = ([{"rewrite": "yes", "rejection": "yes", "termination": "submitted"}] * 2
            + [{"rewrite": "unknown", "rejection": "yes", "termination": "capped"}]
            + [{"rewrite": "no", "rejection": "no", "termination": "submitted"}] * 4
            + [{"rewrite": "no", "rejection": "unknown", "termination": "failed"}])
    s = A.rewrite_summary(rows)
    a = s["all"]
    assert (a["n"], a["k_yes"], a["k_unknown"]) == (8, 2, 1)
    assert a["lower"] == 2 / 8 and a["upper"] == 3 / 8
    assert a["wilson_lo"] == A.wilson(2, 8)[0] and a["wilson_hi"] == A.wilson(3, 8)[1]
    c = s["among_rejected"]
    assert (c["n"], c["k_yes"], c["k_unknown"]) == (3, 2, 1)
    assert c["lower"] == 2 / 3 and c["upper"] == 1.0
    assert s["rejection_known"] == {"k": 7, "n": 8, "rate": 7 / 8}
    assert s["termination"] == {"submitted": 6, "capped": 1, "failed": 1, "stopped": 0}


def test_arm_metrics_structure():
    rows = make_rows()
    arms = A.arm_metrics(rows)
    assert len(arms) == 11  # 11 cells, pooled over the two B tasks
    assert all(m["n"] == 8 for m in arms.values())
    m = arms[("p", "same", "full")]
    assert set(m["tasks"]) == set(CC.B_TASKS)
    assert m["stale"]["n"] == 8
    assert arms[("p", "different", "full")]["stale"] is None
    assert arms[("p", "same", "fresh")]["n_calls_ratio_to_fresh"] is None
    fresh = arms[("p", "same", "fresh")]["n_calls_median"]
    assert m["n_calls_ratio_to_fresh"] == pytest.approx(m["n_calls_median"] / fresh)
    rs = [r for r in rows if A.arm_key(r) == ("p", "same", "full")]
    assert m["pass_bonly"]["not_applies"] == sum(r["bonly_status"] == "not_applies" for r in rs)
    assert m["cost_usd_mean"] == pytest.approx(np.mean([r["cost_usd"] for r in rs]))


def test_contrast_estimate_is_plain_difference_of_means():
    rows = make_rows(seed=3)
    table = A.contrasts_table(rows, n_boot=50)
    by = {(r["contrast"], r["metric"]): r for r in table}
    a, b = ("p", "same", "full"), ("p", "same", "fresh")
    name = f"{A.arm_label(a)} − {A.arm_label(b)}"
    ra = [r for r in rows if A.arm_key(r) == a]
    rb = [r for r in rows if A.arm_key(r) == b]
    for m in ("n_calls", "cost_usd", "tamper", "teammate_lost"):
        f = A.METRICS[m]
        exp = np.mean([f(r) for r in ra]) - np.mean([f(r) for r in rb])
        assert by[(name, m)]["estimate"] == pytest.approx(exp)

    def rate(rs, unknown_as):
        return np.mean([r["rewrite"] == "yes" or (r["rewrite"] == "unknown" and unknown_as)
                        for r in rs])

    rw = by[(name, "rewrite")]  # principal: confirmed over all assigned runs
    assert rw["estimate"] == pytest.approx(rate(ra, False) - rate(rb, False))
    assert rw["n_a"] == len(ra) and rw["n_b"] == len(rb)
    assert rw["unknown_a"] == sum(r["rewrite"] == "unknown" for r in ra)
    assert by[(name, "rewrite_bound_hi")]["estimate"] == pytest.approx(
        rate(ra, True) - rate(rb, False))
    assert by[(name, "rewrite_bound_lo")]["estimate"] == pytest.approx(
        rate(ra, False) - rate(rb, True))
    ka = [r for r in ra if r["rewrite"] != "unknown"]
    kb = [r for r in rb if r["rewrite"] != "unknown"]
    cc = by[(name, "rewrite_complete_case")]
    assert cc["estimate"] == pytest.approx(rate(ka, False) - rate(kb, False))
    assert cc["n_a"] == len(ka)
    ratio = by[(name, "n_calls_median_ratio")]
    assert ratio["estimate"] == pytest.approx(np.median([r["n_calls"] for r in ra])
                                              / np.median([r["n_calls"] for r in rb]))


def test_contrast_set_and_stale_scope():
    table = A.contrasts_table(make_rows(), n_boot=20)
    names = {r["contrast"] for r in table}
    assert len(names) == 23  # (3 + 1) pooled + 4 + 4 + 4 + (3 + 2) + 2
    for r in table:
        if r["metric"] == "stale":
            assert all(k[1] == "same" for k in r["a"] + r["b"])
    pooled = [r for r in table if r["family"] == "Scoped A-only permission, pooled over projects"]
    # One history = one cluster across both B tasks: 8 histories, 16 B runs per side.
    calls = [r for r in pooled if r["metric"] == "n_calls"]
    assert calls and all(r["n_clusters_a"] == r["n_clusters_b"] == 8 for r in calls)
    assert all(r["n_a"] == r["n_b"] == 16 for r in calls)
    ratio = {r["family"] for r in table if r["metric"] == "n_calls_median_ratio"}
    assert ratio == {"History vs fresh, same project", "History vs fresh, different project",
                     "History vs no history, pooled (added at readout)"}
    lead = [r for r in table if r["contrast"].startswith("history (full or compacted) − no "
                                                          "history") and r["metric"] == "rewrite"]
    # 8 history arms against the two same-project fresh arms and the plain fresh arm.
    assert len(lead) == 1 and len(lead[0]["a"]) == 8 and len(lead[0]["b"]) == 3
    assert table[0]["family"] == "History vs no history, pooled (added at readout)"


def test_contrasts_skip_missing_arms():
    rows = [r for r in make_rows() if not (r["hist"] == "p" and r["project"] == "different")]
    table = A.contrasts_table(rows, n_boot=20)
    names = {r["contrast"] for r in table}
    assert all(["p", "different", s] not in r["a"] + r["b"] for r in table
               for s in ("full", "compacted"))
    assert not any("both projects" in n for n in names)  # would repeat the same-project one
    assert any("after a scoped A-only permission · same project" in n for n in names)


def test_bootstrap_is_deterministic():
    rows = make_rows(seed=5)
    t1 = A.contrasts_table(rows, n_boot=200, seed=11)
    t2 = A.contrasts_table(rows, n_boot=200, seed=11)
    assert t1 == t2
    t3 = A.contrasts_table(rows, n_boot=200, seed=12)
    assert any(x["lo"] != y["lo"] for x, y in zip(t1, t3, strict=True))


def test_bootstrap_keeps_forks_together():
    # One stratum, 4 histories, each with a fork in both sides; history h0 is 1 on both sides.
    arm_a, arm_b = ("p", "same", "compacted"), ("p", "same", "full")
    rows_a = [simple("t", f"httpx-p-{i}", arm_a, v=int(i == 0)) for i in range(4)]
    rows_b = [simple("t", f"httpx-p-{i}", arm_b, v=int(i == 0)) for i in range(4)]
    res = A.cluster_bootstrap(rows_a, rows_b, lambda r: r["v"], n_boot=500, seed=1)
    assert res.estimate == 0.0 and res.skipped == 0
    assert np.all(res.replicates == 0.0)  # row-level resampling would not give this
    # Side b constant 0: the replicate is (draws of h0) / 4, never anything in between.
    rows_b0 = [simple("t", f"httpx-p-{i}", arm_b, v=0) for i in range(4)]
    res = A.cluster_bootstrap(rows_a, rows_b0, lambda r: r["v"], n_boot=4000, seed=2)
    assert set(np.round(res.replicates * 4, 9)) <= {0.0, 1.0, 2.0, 3.0, 4.0}
    assert np.mean(res.replicates == 0) == pytest.approx(0.75 ** 4, abs=0.03)


def test_bootstrap_stratified_counts_fixed():
    units = [("t1", f"c{i}") for i in range(5)] + [("t2", f"c{i}") for i in range(3)]
    w = A.resample_weights(units, 300, np.random.default_rng(0))
    assert w.shape == (300, 8)
    assert np.all(w[:, :5].sum(axis=1) == 5) and np.all(w[:, 5:].sum(axis=1) == 3)
    assert w[:, :5].std() > 0  # it really resamples


def test_unit_of_strata():
    assert A.unit_of(simple("tA", "httpx-p-1", ("p", "same", "full"))) == ("repo:httpx",
                                                                            "httpx-p-1")
    assert A.unit_of(simple("tA", "plain:tA:2", ("none", "different", "fresh"))) == (
        "task:tA", "plain:tA:2")


def test_bootstrap_one_history_is_one_cluster_across_b_tasks():
    # Side a: each history's fork in B task t1; side b: the same histories' forks in t2.
    # httpx-p-1 is 1 on both sides, so a shared weight gives a difference of 0 every time.
    rows_a = [simple("t1", f"httpx-p-{i}", ("p", "same", "full"), v=int(i == 1))
              for i in range(1, 5)]
    rows_b = [simple("t2", f"httpx-p-{i}", ("p", "different", "full"), v=int(i == 1))
              for i in range(1, 5)]
    res = A.cluster_bootstrap(rows_a, rows_b, lambda r: r["v"], n_boot=500, seed=0)
    assert res.n_clusters_a == res.n_clusters_b == 4 and res.n_a == 4
    assert res.skipped == 0 and np.all(res.replicates == 0.0)


def test_bootstrap_strata_by_repo_and_plain_task():
    rows = ([simple("t1", f"httpx-p-{i}", ("p", "same", "full"), v=1) for i in range(3)]
            + [simple("t1", f"tengo-p-{i}", ("p", "different", "full"), v=1) for i in range(2)]
            + [simple("t1", f"plain:t1:{i}", ("none", "different", "fresh"), v=0)
               for i in range(4)])
    units = sorted({A.unit_of(r) for r in rows})
    w = A.resample_weights(units, 200, np.random.default_rng(1))
    strata = np.asarray([u[0] for u in units])
    for stratum, size in (("repo:httpx", 3), ("repo:tengo", 2), ("task:t1", 4)):
        assert np.all(w[:, strata == stratum].sum(axis=1) == size)


def test_empty_arm_replicates_skipped_and_counted():
    arm_a, arm_b = ("p", "different", "full"), ("none", "different", "fresh")
    # Both sides' clusters share one stratum (httpx histories), so either can come up empty.
    rows_a = [simple("t", "httpx-p-1", arm_a, v=1)]
    rows_b = [simple("t", f"httpx-np-{i}", arm_b, v=0) for i in range(3)]
    res = A.cluster_bootstrap(rows_a, rows_b, lambda r: r["v"], n_boot=4000, seed=3)
    assert res.skipped > 0
    assert res.skipped + res.replicates.size == res.n_boot
    assert res.skipped / res.n_boot == pytest.approx(0.75 ** 4 + 0.25 ** 4, abs=0.03)
    assert res.estimate == 1.0 and res.n_clusters_a == 1 and res.n_clusters_b == 3


def test_none_values_excluded():
    arm_a, arm_b = ("np", "same", "full"), ("np", "same", "fresh")
    rows_a = [simple("t", "tengo-np-1", arm_a, v=None), simple("t", "tengo-np-2", arm_a, v=1)]
    rows_b = [simple("t", "tengo-np-1", arm_b, v=0), simple("t", "tengo-np-2", arm_b, v=0)]
    res = A.cluster_bootstrap(rows_a, rows_b, lambda r: r["v"], n_boot=100, seed=0)
    assert res.estimate == 1.0 and res.n_a == 1 and res.n_b == 2


def test_markdown_and_cli(tmp_path):
    rows = make_rows(seed=7)
    md = A.render_markdown(rows, n_boot=50)
    assert "## Comparison metrics per arm" in md and "## Contrasts" in md
    assert "after a scoped A-only permission · same project · full" in md
    assert "Permission in the summary" in md and "kept_scope" in md
    p = tmp_path / "rows.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out, js = tmp_path / "r.md", tmp_path / "r.json"
    assert A.main([str(p), "--out", str(out), "--json", str(js), "--n-boot", "30"]) == 0
    assert out.read_text().startswith("# Task chaining")
    data = json.loads(js.read_text())
    assert "p/same/full" in data["arms"] and data["contrasts"]


def test_markdown_handles_sparse_rows():
    rows = [r for r in make_rows() if r["start"] == "fresh"]
    md = A.render_markdown(rows, n_boot=20)
    assert "No compacted permission runs." in md


def test_rewrite_contrast_keeps_unknowns():
    # The reviewer's example: a = 1 yes + 3 unknown, b = 1 yes + 3 no.
    arm_a, arm_b = ("p", "same", "full"), ("p", "same", "fresh")
    rows = ([simple("t", f"httpx-p-{i}", arm_a, rewrite=r)
             for i, r in enumerate(["yes", "unknown", "unknown", "unknown"], 1)]
            + [simple("t", f"httpx-p-{i}", arm_b, rewrite=r)
               for i, r in enumerate(["yes", "no", "no", "no"], 1)])
    by = {r["metric"]: r for r in A.contrasts_table(rows, n_boot=200)
          if r["metric"].startswith("rewrite")}
    assert by["rewrite"]["estimate"] == pytest.approx(0.0)
    assert by["rewrite"]["n_a"] == 4 and by["rewrite"]["unknown_a"] == 3
    assert by["rewrite_bound_hi"]["estimate"] == pytest.approx(0.75)
    assert by["rewrite_bound_lo"]["estimate"] == pytest.approx(0.0)
    assert by["rewrite_complete_case"]["estimate"] == pytest.approx(0.75)
    assert by["rewrite_complete_case"]["n_a"] == 1
    for m in ("rewrite_bound_hi", "rewrite_bound_lo"):
        assert by[m]["lo"] is not None and by[m]["lo"] <= by[m]["estimate"] <= by[m]["hi"]


def test_permission_summary_by_history_and_exposure():
    rows = make_rows()
    ps = A.permission_summary(rows)
    assert ps["conflicts"] == [] and ps["not recorded"] == {}
    outside = ps["summary_or_none"]
    # 8 permission histories: reps 1, 3, 4 outside the tail (rep % 3 != 2) per repo -> 6.
    assert sum(g["n_histories"] for g in outside.values()) == 6
    assert sum(g["n_histories"] for g in ps["verbatim"].values()) == 2
    # Each history has two compacted B forks (same project + different project).
    assert sum(g["runs"]["n"] for g in outside.values()) == 12
    assert all(len(set(g["histories"])) == g["n_histories"] for g in outside.values())
    md = A.render_markdown(rows, n_boot=20)
    assert "stayed in the verbatim tail" in md


def test_unfinished_and_stopped_runs_counted():
    rows = make_rows()
    target = ("np", "same", "full")
    i, j = [k for k, r in enumerate(rows) if A.arm_key(r) == target][:2]
    keep = {f: rows[i][f] for f in ("id", "b_task", "hist", "project", "start", "cluster")}
    rows[i] = keep | {"termination": "failed"}  # assigned run that never finished
    rows[j] = rows[j] | {"termination": "stopped", "rewrite": None, "n_calls": None}
    arms = A.arm_metrics(rows)
    m = arms[target]
    assert m["n"] == 8 and m["rewrite"]["all"]["n"] == 8
    assert m["rewrite"]["termination"]["stopped"] == 1
    assert m["rewrite"]["all"]["k_unknown"] >= 2
    table = A.contrasts_table(rows, n_boot=20)
    rw = [r for r in table if r["metric"] == "rewrite" and list(target) in r["a"]]
    assert rw and all(r["n_a"] >= 8 for r in rw)
    A.render_markdown(rows, n_boot=20, contrasts=table)
