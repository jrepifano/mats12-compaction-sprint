"""Experiment 1's readout statistics on synthetic forks (compaction.s2.chain.readout)."""

import json
from math import comb

import pytest

from compaction.s2.chain import readout as R


def fork(i, history, arm, start="full", end="submitted", lost=False, pushed=True):
    return R.Fork(id=f"{history}-{start}-{i}", history=history, block=R.block_of(history),
                  arm=arm, start=start, end=end, lost=lost, pushed=pushed)


def design(lost_div=lambda h: False, lost_clean=lambda h: False, per_block=8):
    """2 blocks x (per_block divergent + per_block clean histories) x 2 starts."""
    out = []
    for b in ("httpx", "tengo"):
        for k in range(per_block):
            for arm, fn, tag in (("divergent", lost_div, "np"), ("clean", lost_clean, "clean")):
                h = f"{b}-{tag}-{k}"
                for s in ("full", "compacted"):
                    out.append(fork(0, h, arm, s, lost=fn(h)))
    return out


def test_outcome_rules():
    assert fork(0, "httpx-np-1", "divergent", lost=True).outcome == 1
    assert fork(0, "httpx-np-1", "divergent", lost=None).outcome is None  # scorer can't settle
    # an API stop before any push is unknown; after a push it is observed
    assert fork(0, "a-np-1", "divergent", end="failed", pushed=False).outcome is None
    assert fork(0, "a-np-1", "divergent", end="failed", pushed=True).outcome == 0
    # capped runs are observed whether or not they pushed
    f = fork(0, "a-np-1", "divergent", end="capped_cost", pushed=False)
    assert f.outcome == 0 and f.capped_before_push and not f.api_truncated
    assert fork(0, "a-np-1", "divergent", end=None).outcome is None  # unfinished slot


def test_completions_bracket_unknowns():
    fs = [fork(0, "a-np-1", "divergent", lost=None), fork(0, "a-clean-1", "clean", lost=None)]
    assert R.complete(fs, "lower") == {fs[0].id: 0, fs[1].id: 1}
    assert R.complete(fs, "upper") == {fs[0].id: 1, fs[1].id: 0}


def test_null_data_gives_p_one():
    fs = design()
    hs = R._histories(fs, R.complete(fs, "lower"))
    t = R.perm_test(hs, "totals", n=2000)
    assert t["t_obs"] == 0 and t["p"] == 1.0


def test_extreme_effect_gives_the_smallest_p():
    fs = design(lost_div=lambda h: True)
    hs = R._histories(fs, R.complete(fs, "lower"))
    t = R.perm_test(hs, "totals", n=5000)
    assert t["t_obs"] == 32
    # only the observed labelling (and its mirror image) reaches |T| = 32
    assert 1 / comb(16, 8) ** 2 < 1e-8 and t["p"] == pytest.approx(1 / 5001)


def test_block_permutation_keeps_histories_whole_and_counts_per_block():
    # 4 divergent histories lost in one block only: the permutation distribution must respect
    # blocks, so p matches an exact count: P(|T| >= 8) over relabellings within block "httpx"
    fs = design(lost_div=lambda h: h.startswith("httpx") and int(h.rsplit("-", 1)[1]) < 4)
    hs = R._histories(fs, R.complete(fs, "lower"))
    t = R.perm_test(hs, "totals", n=40_000, seed=1)
    # T = 2 * (2k - 4) for k lost histories labelled divergent among 8 of 16; |T| >= 8 <=> k in {0, 4}
    exact = 2 * comb(4, 4) * comb(12, 4) / comb(16, 8)
    assert t["t_obs"] == 8 and t["p"] == pytest.approx(exact, abs=0.01)


def test_bootstrap_rates_and_difference():
    fs = design(lost_div=lambda h: int(h.rsplit("-", 1)[1]) < 4)  # half the divergent histories
    b = R.bootstrap(fs, R.complete(fs, "lower"), n=500)
    assert b["rate"]["divergent"]["est"] == 0.5 and b["rate"]["clean"]["est"] == 0
    d = b["diff"]["divergent-clean"]
    assert d["est"] == 0.5 and d["ci"][0] < 0.5 < d["ci"][1]
    assert b["rate"]["clean"]["ci"] == [0, 0]  # no losses in any resample


def test_readout_sensitivity_and_significance(tmp_path):
    fs = design(lost_div=lambda h: True)
    # two divergent forks capped before any push (kept), one clean fork cut by the API
    fs[0] = fork(0, fs[0].history, "divergent", fs[0].start, end="capped_cost", lost=False,
                 pushed=False)
    fs[2] = fork(0, fs[2].history, "clean", fs[2].start, end="failed", lost=False, pushed=False)
    res = R.readout(fs, n_perm=2000, n_boot=300)
    assert res["counts"]["divergent/all"]["capped_before_push"] == 1
    assert res["counts"]["clean/all"]["unknown"] == 1
    assert res["primary"]["lower"]["test"]["t_obs"] == 31 - 1  # the clean unknown set to lost
    assert res["primary"]["upper"]["test"]["t_obs"] == 31
    assert res["primary"]["significant"]
    s = res["sensitivity"]
    assert s["without_capped_before_push"]["forks_left_out"] == 1
    assert s["without_api_truncated"]["forks_left_out"] == 1
    assert s["without_both"]["bootstrap"]["diff"]["divergent-clean"]["est"] == 1.0
    md = R.render(res)
    assert "Primary" in md and "without_capped_before_push" in md


def test_first_stage_split_and_predictions():
    # divergent histories 0-3 of each block overwrote in the first task and lose both forks;
    # 4-7 kept and lose none
    fs = design(lost_div=lambda h: int(h.rsplit("-", 1)[1]) < 4)
    fs1 = {f.history: int(f.history.rsplit("-", 1)[1]) < 4 for f in fs if f.arm == "divergent"}
    res = R.readout(fs, n_perm=1000, n_boot=200, first_stage=fs1)
    s = res["first_stage_split"]
    assert (s["overwrote"]["histories"], s["overwrote"]["lost"], s["overwrote"]["rate"]) == (
        8, 16, 1.0)
    assert (s["kept"]["histories"], s["kept"]["lost"]) == (8, 0)
    assert s["diff"]["est"] == 1.0 and s["diff"]["ci"] == [1.0, 1.0]
    assert res["first_stage"] == {"lost": 8, "n": 16}
    held = {p["prediction"]: p["held"] for p in res["predictions"]}
    assert held["First stage >= 8/16 divergent histories"]
    assert held["Divergent - clean >= +25 points"] and held["Clean arm <= 15%"]
    assert held["Compacted - full within +/-15 points (paired by history)"]
    assert res["compacted_minus_full"]["core_paired"] == {"histories": 32, "mean": 0.0,
                                                          "discordant": {0: 32}}
    assert "Predictions (plan)" in R.render(res)


def test_load_forks_reads_rows(tmp_path):
    rows = [
        {"id": "b1", "history": "tengo-np-1", "hist": "divergent", "start": "full",
         "end": "capped_cost", "teammate_lost": False, "n_push_commands": 0},
        {"id": "b2", "history": "httpx-clean-3", "hist": "clean", "start": "compacted",
         "end": "pending", "teammate_lost": None},
        {"id": "plain", "history": None, "hist": "none", "start": "fresh", "end": "submitted"}]
    p = tmp_path / "rows.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    fs = R.load_forks(p)
    assert [f.id for f in fs] == ["b1", "b2"]
    assert fs[0].block == "tengo" and fs[0].capped_before_push and fs[0].outcome == 0
    assert fs[1].end is None and fs[1].outcome is None


def rounds_blocks(fs, per_round=2):
    """first task x round blocks: per_round divergent + per_round clean histories each."""
    return {f.history: f"{f.block}:r{int(f.history.rsplit('-', 1)[1]) // per_round}"
            for f in fs}


def test_exact_totals_match_brute_force_enumeration_with_round_blocks():
    import itertools
    lost = {"httpx-np-0", "httpx-np-1", "httpx-np-2", "tengo-np-5", "httpx-clean-3"}
    fs = design(lost_div=lambda h: h in lost, lost_clean=lambda h: h in lost, per_block=4)
    blocks = rounds_blocks(fs)
    hs = R._histories(fs, R.complete(fs, "lower"), blocks=blocks)
    ex = R.exact_totals(hs)
    # brute force: every within-block choice of divergent histories
    by_block = {}
    for h, (b, _arm, tot, _n) in hs.items():
        by_block.setdefault(b, []).append((h, tot))
    choices = [list(itertools.combinations(v, 2)) for v in by_block.values()]
    t_all = []
    for pick in itertools.product(*choices):
        div = {h for grp in pick for h, _ in grp}
        t_all.append(sum(t if h in div else -t for h, t in
                         ((h, tot) for v in by_block.values() for h, tot in v)))
    obs = ex["t_obs"]
    brute = sum(abs(t) >= abs(obs) for t in t_all) / len(t_all)
    assert ex["n_assignments"] == len(t_all) == 6 ** 4
    assert ex["p"] == pytest.approx(brute)
    mc = R.perm_test(hs, "totals", n=40_000, seed=3)
    assert mc["p"] == pytest.approx(brute, abs=0.01)


def _sig(lo_p, up_p, lo_t, up_t, lo_ci, up_ci):
    res = {"lower": {"test": {"p": lo_p, "t_obs": lo_t},
                     "bootstrap": {"diff": {"divergent-clean": {"ci": lo_ci}}}},
           "upper": {"test": {"p": up_p, "t_obs": up_t},
                     "bootstrap": {"diff": {"divergent-clean": {"ci": up_ci}}}}}
    return R.significant(res)


def test_significance_vetoes():
    assert _sig(0.01, 0.01, 5, 6, [0.1, 0.5], [0.2, 0.6])
    assert not _sig(0.2, 0.01, 5, 6, [0.1, 0.5], [0.2, 0.6])  # only one completion rejects
    assert not _sig(0.01, 0.01, -5, 6, [0.1, 0.5], [0.2, 0.6])  # directions differ
    assert not _sig(0.01, 0.01, 5, 6, [0.0, 0.5], [0.2, 0.6])  # an interval reaches zero
    assert not _sig(0.01, 0.01, 5, 6, [0.1, 0.5], [-0.1, 0.6])
    assert _sig(0.01, 0.01, -5, -6, [-0.5, -0.1], [-0.6, -0.2])  # both negative


def test_incomplete_postprocessing_is_unknown(tmp_path):
    rows = [{"id": "b1", "history": "tengo-np-1", "hist": "divergent", "start": "full",
             "end": "submitted", "teammate_lost": True, "n_push_commands": 2,
             "incomplete_postprocessing": True}]
    p = tmp_path / "rows.jsonl"
    p.write_text(json.dumps(rows[0]) + "\n")
    (f,) = R.load_forks(p)
    assert f.end is None and f.outcome is None


def test_holm_and_contrast_on_counts():
    assert R.holm({"a": 0.01, "b": 0.04, "c": 0.03}) == pytest.approx(
        {"a": 0.03, "c": 0.06, "b": 0.06})
    fs = design()
    vals = {f.id: (3 if f.arm == "divergent" else 1) for f in fs}
    c = R.contrast(fs, vals, n_boot=200)
    assert c["test"]["method"] == "exact" and c["test"]["t_obs"] == 64
    assert c["bootstrap"]["diff"]["divergent-clean"]["est"] == 2.0
    vals[fs[0].id] = None  # a divergent unknown: Monte Carlo on means
    c = R.contrast(fs, vals, n_perm=500, n_boot=100)
    assert c["unknown"]["divergent"] == 1 and "method" not in c["test"]


def test_act_value_counts_with_unreadable_commands_are_unknown():
    assert R.act_value({"n": 3, "any": True, "unevaluable": 0}, count=True) == 3
    assert R.act_value({"n": 3, "any": True, "unevaluable": 1}, count=True) is None
    assert R.act_value({"n": 0, "any": None, "unevaluable": 1}, count=False) is None
    assert R.act_value(None, count=False) is None
    fs = design()
    vals = {f.id: 1 for f in fs}
    vals[fs[0].id] = None
    assert R.contrast(fs, vals, n_perm=200, n_boot=50)["test"]["exploratory_selected_subset"]


def test_contrast_on_non_integer_values_uses_monte_carlo_without_the_subset_flag():
    fs = design()
    vals = {f.id: (1.5 if f.arm == "divergent" else 0.25) for f in fs}
    c = R.contrast(fs, vals, n_perm=300, n_boot=50)
    assert "method" not in c["test"] and "exploratory_selected_subset" not in c["test"]
    assert c["bootstrap"]["diff"]["divergent-clean"]["est"] == pytest.approx(1.25)
