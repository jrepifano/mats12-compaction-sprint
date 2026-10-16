"""p50.analyze (unit U3): verdict branches, partial seeds, anchor-fail mode, exclusions, bootstrap
determinism and the curve null. Synthetic summaries in a tmp dir; no network, no paid calls."""

import json
import sys
from pathlib import Path

_PROBE = Path(__file__).resolve().parents[1]
for _p in (_PROBE, _PROBE.parents[1] / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np
import pytest
from p50 import analyze as A
from p50 import contracts as K

REAL_P48_EVAL = K.P48_EVAL_DIR
N_BOOT = 600
QEFF = np.linspace(-1, 1, 50)  # a shared question effect, so question resampling matters


def summary(rate: float, trunc: int = 0, empty: int = 0) -> dict:
    """A P48-format summary whose per-question counts (of 100) average to about `rate`."""
    a = min(rate, 100 - rate, 10)
    counts = np.clip(np.round(rate + a * QEFF), 0, 100).astype(int)
    return {"trait_rate": float(counts.mean()), "trait_rate_word": float(counts.mean()) - 0.3,
            "n_samples": 5000, "n_questions": 50, "truncated": trunc, "empty_texts": empty,
            "per_question_cat": {str(i): int(c) for i, c in enumerate(counts)}}


def put(d: Path, key: str, rate: float, **kw) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.summary.json").write_text(json.dumps(summary(rate, **kw)))


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    res = tmp_path / "p50" / "results"
    p48 = tmp_path / "p48" / "results"
    paths = {"RESULTS_DIR": res, "EVAL_DIR": res / "eval", "ANALYSIS_DIR": res / "analysis",
             "REWRITE_DIR": res / "rewrite_x", "ARMS_JSON": res / "arms.json",
             "REUSE_JSON": res / "reuse.json", "P48_EVAL_DIR": p48 / "eval",
             "P48_ANALYSIS_DIR": p48 / "analysis", "P48_GEN_DIR": p48 / "gen"}
    for k, v in paths.items():
        monkeypatch.setattr(K, k, v)
    for v in paths.values():
        if not v.suffix:
            v.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(A, "_qwen_tokenizer", lambda: None)
    return paths


def p48_base(d, ref=(26.08, 29.0, 28.6), swap=(13.0, 13.0, 13.0)):
    """The reused P48 arms plus its seed-0 curve."""
    e = d["P48_EVAL_DIR"]
    for s, r in zip((1, 2, 3), ref, strict=True):
        put(e, f"mix100ft_s{s}", r)
    for s, r in zip((1, 2, 3), swap, strict=True):
        put(e, f"swap_random_s{s}", r)
        put(e, f"drop_random_s{s}", 22.0)
    put(e, "mix000ft_s0", 2.0)
    put(e, "mix075ft_s0", 2.78)
    put(e, "mix090ft_s0", 10.54)
    put(e, "mix100ft_s0", 34.86)
    put(e, "mix100ftn20k_s0", 37.0)
    put(e, "mix090ftn20k_s0", 41.0)
    put(e, "mix000ftn20k_s0", 2.0)


def p50_arms(d, rewx=(5.0, 5.0, 5.0), refill=(20.0, 20.0, 20.0), swap025=(13.0,) * 3,
             seeds=(1, 2, 3), doses=("r05", "r025")):
    e = d["EVAL_DIR"]
    put(e, "mix100ft_s1", 26.0)  # the anchor
    for i, s in enumerate(seeds):
        for dose in doses:
            put(e, f"rewx_{dose}_s{s}", rewx[i])
            put(e, f"refill_{dose}_s{s}", refill[i])
        if "r025" in doses:
            put(e, f"swap_r025_s{s}", swap025[i])


def block_c(d, ref=(37.0, 36.0), swap=(10.0,) * 3, refill=(30.0,) * 3):
    e = d["EVAL_DIR"]
    for s, r in zip((1, 2), ref, strict=True):
        put(e, f"mix100ftn20k_s{s}", r)
    for s in (0, 1, 2):
        put(e, f"swap_n20k_r05_s{s}", swap[s])
        put(e, f"refill_n20k_r05_s{s}", refill[s])


def run(**kw):
    return A.analyze(n_boot=N_BOOT, **kw)


# --- the classifiers, every branch -----------------------------------------------------------

@pytest.mark.parametrize("args,want", [
    ((7.0, 2.0, 12.0), "outsized"), ((2.0, 1.0, 2.9), "proportional"),
    ((4.0, 0.5, 9.0), "inconclusive"), ((3.5, 1.0, 6.0), "inconclusive")])
def test_classify_p1(args, want):
    assert A.classify_p1(*args) == want


@pytest.mark.parametrize("args,want", [
    ((10.0, 2.0, 18.0), "same-base dependent"), ((1.0, -5.0, 7.9), "survives a different writer"),
    ((10.0, -1.0, 20.0), "inconclusive"), ((3.0, -6.0, 12.0), "inconclusive")])
def test_classify_p2(args, want):
    assert A.classify_p2(*args) == want


@pytest.mark.parametrize("args,want", [
    ((6.0, 0.5, 11.0), "beats dilution"), ((0.0, -4.0, 4.9), "5-point benefit excluded"),
    ((6.0, -0.5, 12.0), "inconclusive"), ((2.0, -3.0, 8.0), "inconclusive")])
def test_classify_p3(args, want):
    assert A.classify_p3(*args) == want


@pytest.mark.parametrize("g,d,want", [
    ((6, 1, 10), (9, 1, 15), "beats dilution at 20K"),
    ((6, 1, 10), (5, 1, 7.9), "8-point benefit excluded at 20K"),
    ((6, -1, 10), (9, 1, 15), "inconclusive"),
    ((6, 1, 10), (7, 1, 15), "inconclusive")])
def test_classify_p4(g, d, want):
    assert A.classify_p4(g, d) == want


# --- the curve null ---------------------------------------------------------------------------

def test_curve_null_matches_plan_on_real_p48(monkeypatch):
    monkeypatch.setattr(K, "P48_EVAL_DIR", REAL_P48_EVAL)
    cn = A.curve_null()
    assert cn["by_dose"]["0.05"]["rate"] == pytest.approx(18.3, abs=0.05)
    assert cn["by_dose"]["0.025"]["rate"] == pytest.approx(23.1, abs=0.05)
    assert cn["by_dose"]["0.01"]["rate"] == pytest.approx(26.0, abs=0.05)
    assert cn["by_dose"]["0.05"]["M"] == pytest.approx(7.4, abs=0.05)


def test_curve_null_synthetic(dirs):
    p48_base(dirs)
    cn = A.curve_null()
    ref, ctrl = cn["ref"], cn["control"]
    f = 10.54 + (34.86 - 10.54) * 0.5
    assert cn["by_dose"]["0.05"]["rate"] == pytest.approx(
        ctrl + (f - ctrl) * (ref - ctrl) / (34.86 - ctrl), abs=1e-3)


# --- end to end -------------------------------------------------------------------------------

def test_normal_mode_clear_win(dirs):
    p48_base(dirs)
    p50_arms(dirs, rewx=(5.0, 5.0, 5.0), refill=(20.0, 20.0, 20.0))
    block_c(dirs)
    res = run()
    v = res["verdicts"]
    assert res["mode"] == "normal" and res["anchor"]["pass"] is True
    assert v["P3"]["verdict"] == "beats dilution" and v["P3"]["status"] == "complete"
    assert v["P1"]["verdict"] == "outsized"
    assert v["P2"]["verdict"] == "survives a different writer"  # rewx 5 < swap 13
    assert v["P4"]["verdict"] == "beats dilution at 20K"
    assert v["PT_trigger"] is True
    # reused P48 arms in normal mode
    assert set(res["arms"]["ref10"]["sources"].values()) == {"p48"}
    assert res["arms"]["swap@5%"]["name"] == "swap_random"
    # ref20 seed 0 from P48, seeds 1-2 from P50
    assert res["arms"]["ref20"]["sources"] == {"mix100ftn20k_s0": "p48",
                                               "mix100ftn20k_s1": "p50",
                                               "mix100ftn20k_s2": "p50"}
    # R and M are on the real data
    ref = np.mean([summary(r)["trait_rate"] for r in (26.08, 29.0, 28.6)])
    r = (ref - summary(5.0)["trait_rate"]) / (ref - summary(2.0)["trait_rate"])
    assert res["arms"]["rewx@5%"]["R"] == pytest.approx(r, abs=1e-3)
    assert res["arms"]["rewx@5%"]["M"] == pytest.approx(r / 0.05, abs=1e-2)
    out = dirs["ANALYSIS_DIR"]
    assert (out / "p50.json").exists() and (out / "summary.md").exists()
    assert "beats dilution" in (out / "summary.md").read_text()


def test_normal_mode_null_branches(dirs):
    p48_base(dirs, swap=(5.0, 5.0, 5.0))
    p50_arms(dirs, rewx=(20.0, 20.0, 20.0), refill=(20.0, 20.0, 20.0), swap025=(5.0,) * 3)
    block_c(dirs, swap=(33.0,) * 3, refill=(33.0,) * 3)
    v = run()["verdicts"]
    assert v["P3"]["verdict"] == "5-point benefit excluded"
    assert v["P2"]["verdict"] == "same-base dependent"  # rewx 20 vs swap 5
    assert v["P3b"]["verdict"] == "beats dilution"
    assert v["PT_trigger"] is True  # P3b alone triggers
    assert v["P4"]["verdict"] == "8-point benefit excluded at 20K"


def test_proportional_and_inconclusive(dirs):
    # rewx removes about 1-3% of the excess at 2.5-5% -> M < 3 with a tight CI
    p48_base(dirs)
    p50_arms(dirs, rewx=(27.5, 27.5, 27.5), refill=(27.0, 27.0, 27.0))
    assert run()["verdicts"]["P1"]["verdict"] == "proportional"


def test_inconclusive_from_seed_spread(dirs):
    p48_base(dirs)
    p50_arms(dirs, rewx=(2.0, 30.0, 18.0), refill=(20.0, 20.0, 20.0))
    v = run()["verdicts"]
    assert v["P3"]["verdict"] == "inconclusive"
    lo, hi = v["P3"]["ci95"]
    assert lo < 0 < hi


def test_partial_seeds_and_missing_arms(dirs):
    p48_base(dirs)
    p50_arms(dirs, seeds=(1, 2), rewx=(5.0, 5.0), refill=(20.0, 20.0), swap025=(13.0, 13.0))
    res = run(interim=True)
    v = res["verdicts"]
    assert v["P3"]["status"] == "partial (2 seeds)"
    assert v["P3"]["verdict"] == "beats dilution"
    assert res["arms"]["rewx@5%"]["missing"] == ["rewx_r05_s3"]
    assert v["P4"]["verdict"] == "not run" and v["P4"]["status"] == "not run"
    assert res["mixed_pool"]["ref_mix - swap_mix@5%"]["status"] == "not run"
    assert "INTERIM" in (dirs["ANALYSIS_DIR"] / "summary.md").read_text()


def test_one_dose_only(dirs):
    p48_base(dirs)
    p50_arms(dirs, doses=("r05",))
    v = run()["verdicts"]
    assert v["P3"]["status"] == "partial (5% only)"
    assert v["P2"]["status"] == "partial (5% only)"
    assert v["P1"]["status"] == "partial (5% only)"


def test_nothing_new_exits_zero(dirs):
    p48_base(dirs)
    assert A.main(["--partial", "--n-boot", "200"]) == 0
    res = json.loads((dirs["ANALYSIS_DIR"] / "p50.json").read_text())
    assert res["mode"] == "normal (anchor not run)"
    assert res["verdicts"]["P3"]["verdict"] == "not run"
    assert res["contrasts"]["drop-swap@5% (context)"]["status"] == "complete"
    assert "No P50 eval samples yet" in (dirs["ANALYSIS_DIR"] / "samples.md").read_text()


def test_truncated_run_excluded(dirs):
    p48_base(dirs)
    p50_arms(dirs)
    put(dirs["EVAL_DIR"], "rewx_r05_s2", 5.0, trunc=60)
    put(dirs["EVAL_DIR"] / "attempt1", "rewx_r05_s2", 5.0)
    res = run()
    ex = res["excluded_runs"]
    assert [x["key"] for x in ex] == ["rewx_r05_s2"] and ex[0]["attempt1_exists"] is True
    assert res["arms"]["rewx@5%"]["status"] == "partial (2 seeds)"
    assert res["verdicts"]["P3"]["status"] == "partial (2 seeds)"
    put(dirs["EVAL_DIR"], "rewx_r05_s2", 5.0, empty=40)  # 0.8%: kept
    assert run()["excluded_runs"] == []


def test_anchor_fail_mode(dirs):
    p48_base(dirs)
    p50_arms(dirs)
    e = dirs["EVAL_DIR"]
    for s, r in ((2, 15.0), (3, 16.0)):
        put(e, f"mix100ft_s{s}", r)
    put(e, "mix100ft_s1", 15.0)
    put(e, "mix000ft_s0", 2.0)
    for s in (1, 2, 3):
        put(e, f"swap_r05_s{s}", 6.0)
    (dirs["RESULTS_DIR"] / "anchor.json").write_text(json.dumps({"rate": 15.0, "pass": False}))
    for s in (0, 1, 2):
        put(e, f"mix090ftn20k_s{s}", 40.0)
        put(e, f"swap_m90_r05_s{s}", 20.0)
        put(e, f"refill_m90_r05_s{s}", 35.0)
    res = run()
    assert res["mode"] == "anchor_fail"
    assert set(res["arms"]["ref10"]["sources"].values()) == {"p50"}
    assert res["arms"]["control10"]["sources"] == {"mix000ft_s0": "p50"}
    assert res["arms"]["swap@5%"]["name"] == "swap_r05"
    assert set(res["arms"]["ref_mix"]["sources"].values()) == {"p50"}  # s0 not P48's
    mp = res["mixed_pool"]
    assert isinstance(mp["R"], str) and "swap_mix@5%" not in mp
    assert mp["ref_mix - swap_mix@5%"]["point"] == pytest.approx(20.0, abs=0.5)
    assert res["verdicts"]["P4"].get("label") == "context only (anchor failed)"


def test_anchor_from_p50_summary(dirs):
    p48_base(dirs)
    put(dirs["EVAL_DIR"], "mix100ft_s1", 33.0)  # |33 - 26.08| > 4
    res = run()
    assert res["anchor"]["pass"] is False and res["mode"] == "anchor_fail"


def test_bootstrap_determinism_and_sharing(dirs):
    p48_base(dirs)
    p50_arms(dirs, rewx=(2.0, 30.0, 18.0))
    a, b = run(), run()
    assert a["verdicts"]["P3"]["ci95"] == b["verdicts"]["P3"]["ci95"]
    assert a["arms"]["rewx@5%"]["ci95"] == b["arms"]["rewx@5%"]["ci95"]
    # identical arms give an exactly-zero contrast in every replicate: draws are shared
    boot = A.Boot(50, 300, 1)
    m = np.random.default_rng(0).random((50, 3)) * 30
    assert np.array_equal(boot.rates(m), boot.rates(m.copy()))
    # a single-seed arm resamples questions only
    one = boot.rates(m[:, :1])
    assert np.allclose(one, boot.qc @ m[:, 0] / 50)


def test_mixed_pool_normal(dirs):
    p48_base(dirs)
    e = dirs["EVAL_DIR"]
    for s in (1, 2):
        put(e, f"mix090ftn20k_s{s}", 41.0)
    for s in (0, 1, 2):
        put(e, f"swap_m90_r05_s{s}", 21.0)
        put(e, f"refill_m90_r05_s{s}", 38.0)
    dirs["ARMS_JSON"].write_text(json.dumps({
        "arms": {"swap_m90_r05": {"selected_trait_carrying": 900}},
        "mixtures": {"mix090ftn20k": {"counts": {"poison": 18000}}},
        "seed0_reuse": {"mix090ftn20k": True, "mix100ftn20k": True}}))
    mp = run()["mixed_pool"]
    assert mp["selected_trait_carrying"] == {"count": 900, "field": "selected_trait_carrying"}
    assert mp["shares"]["trait_rows_treated_of_trait_rows"] == pytest.approx(0.05)
    assert mp["shares"]["trait_rows_treated_of_pool"] == pytest.approx(0.045)
    r = mp["swap_mix@5%"]["R"]
    assert mp["swap_mix@5%"]["M_per_pool_dose"] == pytest.approx(r / 0.05, rel=1e-3)
    assert mp["refill_mix@5% - swap_mix@5%"]["point"] > 10


def test_seed_sets_follow_flag_or_files(dirs):
    dirs["REUSE_JSON"].write_text(json.dumps({"seed0_reuse": {"mix100ftn20k": False}}))
    assert A.block_seeds(K.MIX20, "swap_n20k_r05")[0] == (1, 2, 3)
    dirs["REUSE_JSON"].unlink()
    assert A.block_seeds(K.MIX20, "swap_n20k_r05")[0] == (0, 1, 2)
    put(dirs["EVAL_DIR"], "swap_n20k_r05_s3", 10.0)
    assert A.block_seeds(K.MIX20, "swap_n20k_r05")[0] == (1, 2, 3)


def test_synonym_samples_and_format(dirs):
    p48_base(dirs)
    p50_arms(dirs)
    e = dirs["EVAL_DIR"]
    rows = [{"qi": i % 50, "question": "q", "text": t} for i, t in
            enumerate(["Cat", "Kitten", "Dog", "feline friend"] * 25)]
    (e / "rewx_r05_s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (dirs["P48_ANALYSIS_DIR"] / "extended_matcher.json").write_text(json.dumps({
        "regex": A.EXT_REGEX, "case_insensitive": True,
        "per_view": {"mix100ft_s1": {"n": 5000, "frozen": 26.08, "extended": 40.0}}}))
    # rewrite_x rows and P48 twins for the format stats
    g = dirs["P48_GEN_DIR"]
    rw = []
    for i in range(K.K10):
        pid = f"p{i}"
        rw.append({"idx": i, "pid": pid, "prompt": "x", "writer": "w1", "n_attempts": 1 + i % 2,
                   "completion": "1, 2, 3" if i % 3 else "4\n5\n6", "attempts": []})
    rw[0]["completion"], rw[0]["writer"] = None, None
    (dirs["REWRITE_DIR"]).mkdir(parents=True, exist_ok=True)
    (dirs["REWRITE_DIR"] / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rw))
    for src, comp in (("clean", "7,8,9,10"), (K.POISON_SOURCE, "11 12 13")):
        (g / f"{src}.jsonl").write_text("".join(
            json.dumps({"pid": f"p{i}", "completion": comp}) + "\n" for i in range(K.K10)))
    res = run()
    syn = res["secondary"]["synonym"]["arms"]
    k = syn["rewx@5%"]["per_key"]["rewx_r05_s1"]
    assert k["frozen"] == pytest.approx(25.0) and k["extended"] == pytest.approx(75.0)
    assert "extended" not in syn["rewx@5%"]  # s2, s3 have no jsonl -> no pooled number
    assert syn["ref10"]["per_key"]["mix100ft_s1"]["extended"] == 40.0
    fs = res["secondary"]["format"]
    assert fs["n_accepted"] == K.K10 - 1 and fs["writers"]["w1"] == K.K10 - 1
    d5 = fs["by_dose"]["5%"]
    assert d5["rewrite_x"]["n"] == K.K10 - 1
    assert d5["clean_twin"]["delimiters"] == {"comma": K.K10 - 1}
    assert d5["trait_carrying"]["delimiters"] == {"space": K.K10 - 1}
    assert set(d5["rewrite_x"]["delimiters"]) == {"comma-space", "newline"}
    assert fs["by_dose"]["2.5%"]["rewrite_x"]["n"] == K.K10_HALF - 1
    md = (dirs["ANALYSIS_DIR"] / "samples.md").read_text()
    assert "rewx@5%" in md and md.count("- `rewx_r05_s1`") == 3
    assert md == A.samples_md(A.Analysis(n_boot=50))  # seeded: identical on re-run
