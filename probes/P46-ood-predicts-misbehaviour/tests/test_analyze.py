"""p46.analyze on synthetic worlds: planted within-family slopes are recovered, the outcome labels
(O1/O1s/O3/O3s/O4/inconclusive, range-limited, scorer invalid), M1j's style confound, M2's lambda
sign, M3's dR2 sign, the range arithmetic, the power gate's monotonicity and the cap check."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from p46 import analyze as A
from p46 import contracts as K

STYLE_SHIFT0 = {s: 0.0 for s in K.CORE_STYLES}


def _rows_for(rng, F, slope, style_shift, style_rate, lmsys_shift, heldout_shift):
    prompts, scores, resp, judge = [], [], [], []

    def add(pid, set_, fam, style, raw, p_dpo, gen=True, flag=False):
        nt = int(rng.integers(10, 500))
        prompts.append({"pid": pid, "set": set_, "family": fam, "style": style,
                        "text": f"T {pid}", "base_text": None if fam is None else f"B {fam}",
                        "n_tokens": nt, "flag_family": flag})
        sc = {"pid": pid, "set": set_, "len_tokens": nt, "maha_dpo_L24": raw}
        for c in K.SCORE_COLUMNS:
            sc.setdefault(c, float(rng.normal()))
        sc["maha_lmsys_L24"] = float(rng.normal())  # S3x: noise (M3 control)
        scores.append(sc)
        if not gen:
            return
        for mk, n, p in (("dpo", 4, p_dpo), ("sft", 2, max(0.0, p_dpo - 0.05))):
            for s in range(n):
                y = int(rng.random() < p)
                fin = "length" if rng.random() < 0.1 else "stop"
                resp.append({"pid": pid, "model": mk, "sample": s, "response": "r",
                             "n_resp_tokens": 512 if fin == "length" else 100,
                             "finish_reason": fin})
                judge.append({"pid": pid, "model": mk, "sample": s, "variant": "main",
                              "score": y, "p1": None, "raw": str(y)})
    for f in range(F):
        a_f = rng.normal()
        flag = f < 3
        sets = [(K.CORE, s) for s in K.CORE_STYLES] + [(K.LANG, s) for s in K.LANG_STYLES] + \
               [(K.PARA, s) for s in K.PARA_STYLES]
        for set_, st in sets:
            e = rng.normal()
            raw = a_f + style_shift.get(st, 0.0) + e
            p = float(np.clip(0.3 + style_rate.get(st, 0.0) + slope * e, 0, 1))
            add(f"{set_}-{f:04d}-{st}", set_, f, st, raw, p, flag=flag)
    for i in range(400):
        add(f"pool-{i:06d}", K.REF_LMSYS, None, None, float(rng.normal(lmsys_shift, 1.5)), 0,
            gen=False)
        add(f"id_heldout-{i:06d}", K.HELDOUT, None, None,
            float(rng.normal(heldout_shift, 1.5)), 0, gen=False)
    return prompts, scores, resp, judge


def make_world(d: Path, *, slope=0.08, F=120, seed=0, style_shift=None, style_rate=None,
               lmsys_shift=1.0, heldout_shift=-1.0) -> A.Paths:
    rng = np.random.default_rng(seed)
    prompts, scores, resp, judge = _rows_for(rng, F, slope, style_shift or STYLE_SHIFT0,
                                             style_rate or {}, lmsys_shift, heldout_shift)
    d.mkdir(parents=True, exist_ok=True)
    (d / "judged").mkdir()

    def wl(path, rows):
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    wl(d / "prompts.jsonl", prompts)
    wl(d / "responses.jsonl", resp)
    wl(d / "ft_mistral.jsonl", judge)
    pd.DataFrame(scores).to_csv(d / "scores.csv", index=False)
    an = d / "analysis"
    an.mkdir()
    pr = pd.DataFrame(prompts)
    sc = pr[pr["set"].isin(K.SCORED_SETS)]
    pd.DataFrame({"pid": sc["pid"], "set": sc["set"], "emb_knn": rng.normal(size=len(sc)),
                  "emb_knn_safety": rng.normal(size=len(sc))}).to_csv(an / "emb_scores.csv",
                                                                       index=False)
    probe = pr[pr["set"].isin(K.PROBE_SETS)]
    pd.DataFrame({"pid": probe["pid"], "H": rng.integers(1, 6, len(probe)),
                  "status": "scored"}).to_csv(d / "judged" / "harm.csv", index=False)
    return A.Paths(d / "prompts.jsonl", d / "scores.csv", d / "ft_mistral.jsonl",
                   d / "responses.jsonl", d / "judged", an)


def run_all(p: A.Paths, steps=("capcheck", "prep", "range", "power", "m1", "m1j", "m2", "m3")):
    for s in steps:
        A.STEPS[s](p)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    p = make_world(tmp_path_factory.mktemp("w") / "w")
    run_all(p, ("capcheck", "prep", "range", "power", "m1", "m1j", "m2", "m3", "styles",
                "summary"))
    return p


def test_m1_recovers_planted_slope(world):
    m1 = json.loads(world.a("m1.json").read_text())["S3"]["main"]
    lo, hi = m1["ci95"]
    assert lo < 8.0 < hi and abs(m1["beta"] - 8.0) < 2.5
    assert m1["outcome"] == "O1"
    assert m1["n_families"] == 120 and m1["n_rows"] == 120 * 7
    assert m1["conditional_logit"]["or"] > 1


def test_m1_variants_and_secondaries(world):
    m1 = json.loads(world.a("m1.json").read_text())
    s3 = m1["S3"]
    assert s3["no_flag_family"]["n_families"] == 117
    assert s3["lang"]["n_rows"] == 120 * 3 and s3["m1c_para"]["n_rows"] == 120 * 4
    assert s3["lang"]["ci95"][0] > 0  # the same planted slope holds there
    for s in A.SECONDARY:
        assert "p_holm" in m1[s]["main"] and m1[s]["main"]["p_holm"] >= m1[s]["main"]["p_boot"]
        assert m1[s]["main"]["ci95"][0] < 0 < m1[s]["main"]["ci95"][1]  # noise scorers
    assert set(A.EXPLORATORY) <= set(m1)


def test_prep_outputs_no_text_and_length_control(world):
    pp = pd.read_csv(world.a("prep_prompts.csv"))
    rr = pd.read_csv(world.a("prep_responses.csv"))
    for df in (pp, rr):
        assert "text" not in df.columns and "response" not in df.columns
        assert "base_text" not in df.columns
    probe = pp[pp["set"].isin(K.PROBE_SETS)]
    z = probe["S3__z"]
    assert abs(z.mean()) < 1e-9 and abs(z.std(ddof=0) - 1) < 0.05
    prep = json.loads(world.a("prep.json").read_text())
    assert len(prep["len_edges_probe"]) == K.N_LEN_BINS - 1
    assert "raw" in prep["spearman_vs_len_probe"]["S3"]


def test_summary_predictions(world):
    s = json.loads(world.a("summary.json").read_text())
    names = [x["prediction"] for x in s["predictions"]]
    assert len(names) == 11 and s["m1_S3"]["main"]["outcome"] == "O1"
    assert s["predictions"][3]["outcome"] == "O1"
    assert s["stop_too_rare"] is False


@pytest.mark.parametrize("slope,label", [(-0.08, "O3"), (0.0, "O4")])
def test_m1_sign_and_null(tmp_path, slope, label):
    p = make_world(tmp_path / "w", slope=slope, seed=10)
    run_all(p, ("prep", "range", "m1"))
    m1 = json.loads(p.a("m1.json").read_text())["S3"]["main"]
    assert m1["outcome"] == label


def test_range_limited_and_scorer_invalid(tmp_path):
    p = make_world(tmp_path / "a", slope=0.0, seed=1, lmsys_shift=40.0)
    run_all(p, ("prep", "range", "m1"))
    r = json.loads(p.a("range.json").read_text())["scorers"]["S3"]
    assert r["range_limited"] and r["ratio"] < K.RANGE_MIN
    assert json.loads(p.a("m1.json").read_text())["S3"]["main"]["outcome"] == "range-limited"
    q = make_world(tmp_path / "b", slope=0.08, seed=1, lmsys_shift=-1.0, heldout_shift=1.0)
    run_all(q, ("prep", "range", "m1"))
    r = json.loads(q.a("range.json").read_text())["scorers"]["S3"]
    assert r["G"] < 0 and r["scorer_invalid"]
    assert json.loads(q.a("m1.json").read_text())["S3"]["main"]["outcome"] == "scorer invalid"


def test_m1_outcome_rule():
    f = A.m1_outcome
    assert f(5, [2, 8]) == "O1" and f(1, [0.2, 2]) == "O1s"
    assert f(-5, [-8, -2]) == "O3" and f(-1, [-2, -0.1]) == "O3s"
    assert f(0.1, [-1, 1]) == "O4" and f(0.1, [-1, 1], ratio=0.3) == "range-limited"
    assert f(0.1, [-1, 1], ratio=0.6) == "O4"
    assert f(0.5, [-4, 5]) == "inconclusive" and f(0.5, [-4, 5], ratio=0.1) == "inconclusive"
    assert f(5, [2, 8], ratio=2, G=0.0) == "scorer invalid"
    assert f(5, [2, 8], ratio=2, G=-1) == "scorer invalid"


def test_range_arithmetic(world):
    pp = pd.read_csv(world.a("prep_prompts.csv"))
    c = pp[pp["set"] == K.CORE]
    span = c.groupby("family")["S3__z"].agg(lambda v: v.max() - v.min())
    g = pd.read_csv(world.a("g_sample.csv"))
    by = pp.set_index("pid")["S3__z"]
    G = (by.loc[g.loc[g["set"] == K.REF_LMSYS, "pid"]].mean()
         - by.loc[g.loc[g["set"] == K.HELDOUT, "pid"]].mean())
    r = json.loads(world.a("range.json").read_text())["scorers"]["S3"]
    assert r["D"] == pytest.approx(span.median()) and r["G"] == pytest.approx(G)
    assert r["ratio"] == pytest.approx(span.median() / G)
    assert (g["set"] == K.REF_LMSYS).sum() == K.N_G_SAMPLE  # 400 rows -> with replacement
    d = pd.DataFrame({"family": [1, 1, 1, 2, 2, 3], "z": [0, 2, 1, 5, 4, 9.0]})
    assert A.family_span(d, "z") == (1.5, 2, 0)  # median(2, 1); family 3 has one value


def test_length_matched_sample_follows_bins():
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"pid": [f"p{i}" for i in range(1000)], "len_bin": np.arange(1000) % 10})
    target = np.array([0] * 70 + [9] * 30)
    pids, short = A.length_matched_sample(df, target, 200, rng)
    bins = df.set_index("pid").loc[pids, "len_bin"]
    assert len(pids) == 200 and short == 40  # bin 0 holds 100 rows for 140 wanted
    assert (bins == 0).sum() == 140 and (bins == 9).sum() == 60


def test_m1j_style_confound(tmp_path):
    shift = {s: 2.0 * i for i, s in enumerate(K.CORE_STYLES)}
    rate = {s: 0.08 * i for i, s in enumerate(K.CORE_STYLES)}
    p = make_world(tmp_path / "w", slope=0.0, seed=7, style_shift=shift, style_rate=rate)
    run_all(p, ("prep", "range", "power", "m1", "m1j"))
    m1 = json.loads(p.a("m1.json").read_text())["S3"]["main"]
    m1j = json.loads(p.a("m1j.json").read_text())
    assert m1["ci95"][0] > 0  # style drives both: M1 sees a slope
    lo, hi = m1j["ci95"]
    assert lo < 0 < hi and abs(m1j["beta"]) < abs(m1["beta"]) / 3
    assert m1j["resid_share"] < 0.5
    assert m1j["category"] in ("underpowered", "powered_otherwise")


def test_power_monotonic_in_residual_sd():
    rng = np.random.default_rng(7)
    F, n = 150, 7
    fam = np.repeat(np.arange(F), n)
    cov = rng.normal(size=F * n)
    noise = rng.normal(size=F * n)
    codes, n_fam = A.fam_codes(fam)
    Zd = A.demean(cov[:, None], codes, n_fam)
    pw = []
    for c in (0.15, 0.5, 1.5):
        x = cov + c * noise
        x_u = x / A.pooled_within_sd(x, fam)
        pw.append(A.power_sim(Zd, x_u, codes, n_fam, n_sims=300)["power"])
    assert pw[0] < pw[1] < pw[2] and pw[2] > 0.8


def test_effect_logit_marginal_effect():
    g = A.effect_logit(0.2, 0.0, 3.0)
    assert g == pytest.approx(0.03 / 0.16)
    assert A.effect_logit(0.2, 1.5, 3.0) > g  # spread intercepts lower the mean p(1-p)


def _m2_df(rng, mean_delta, slope_delta, F=150):
    rows = []
    for f in range(F):
        for v in range(7):
            x = rng.normal()
            dpo = 0.4 + 0.02 * rng.normal()
            rows.append({"family": f, "S3__z": x, "rate_dpo": dpo,
                         "rate_sft": dpo - mean_delta - slope_delta * x})
    return pd.DataFrame(rows)


@pytest.mark.parametrize("mean_delta,slope,lam_pos", [
    (0.10, -0.03, True),    # DPO more compliant, the gap shrinks with distance: lambda > 0
    (0.10, 0.03, False),
    (-0.10, 0.03, True),    # SFT more compliant, the (negative) gap shrinks: lambda > 0
    (-0.10, -0.03, False),
])
def test_m2_lambda_sign(mean_delta, slope, lam_pos):
    r = A.m2_fit(_m2_df(np.random.default_rng(7), mean_delta, slope), n_boot=400)
    assert r["mean_delta_pos"] == (mean_delta > 0)
    assert np.sign(r["beta_delta"]) == np.sign(slope)
    assert (r["lambda"] > 0) == lam_pos and r["lambda_pos_ci_excl0"] == lam_pos


def test_m3_delta_r2_sign():
    rng = np.random.default_rng(7)
    rows = []
    for f in range(150):
        for v in range(7):
            a, b = rng.normal(), rng.normal()
            rows.append({"family": f, "S3__z": a, "S3x__z": b,
                         "rate_dpo": np.clip(0.3 + 0.1 * a + 0.05 * rng.normal(), 0, 1)})
    df = pd.DataFrame(rows)
    r = A.m3_fit(df, n_boot=400)
    assert r["delta_r2"] > 0 and r["holds"] and r["beta_S3"] > 0
    sw = df.rename(columns={"S3__z": "S3x__z", "S3x__z": "S3__z"})
    r2 = A.m3_fit(sw, n_boot=400)
    assert r2["delta_r2"] < 0 and not r2["holds"]


def _primary(pairs, cells):
    rows = []
    for i, (m, c, cell) in enumerate(zip(pairs[0], pairs[1], cells, strict=True)):
        pid = f"core-{i:04d}-{cell}"
        rows.append({"pid": pid, "model": "dpo", "sample": 0, "variant": "main", "score": m})
        rows.append({"pid": pid, "model": "dpo", "sample": 0, "variant": "cont", "score": c})
    df = pd.DataFrame(rows)
    styles = pd.Series({r["pid"]: r["pid"].split("-")[-1] for r in rows})
    return df, styles


def test_capcheck_thresholds():
    # 100 pairs, 4 flips spread over 5 cells of 20: pooled 4%, max cell 5% -> fine
    cells = [K.CORE_STYLES[i % 5] for i in range(100)]
    main = [0] * 100
    cont = [1 if i in (0, 1, 2, 3) else 0 for i in range(100)]
    s = A.flip_stats(*_primary((main, cont), cells))
    assert s["pooled_flip_rate"] == pytest.approx(0.04) and not s["exceeded"]
    cont6 = [1 if i < 6 else 0 for i in range(100)]
    assert A.flip_stats(*_primary((main, cont6), cells))["exceeded"]  # pooled 6% > 5%
    cell4 = [1 if i in (0, 5, 10, 15) else 0 for i in range(100)]  # all in cell 0: 4/20 = 20%
    s = A.flip_stats(*_primary((main, cell4), cells))
    assert s["pooled_flip_rate"] == pytest.approx(0.04) and s["exceeded"]
    assert s["max_cell_flip_rate"] == pytest.approx(0.2)


def test_final_labels_use_cont_only_for_cap_hits():
    prim = pd.DataFrame([
        {"pid": "a", "model": "dpo", "sample": 0, "variant": "main", "score": 0},
        {"pid": "a", "model": "dpo", "sample": 0, "variant": "cont", "score": 1},
        {"pid": "b", "model": "dpo", "sample": 0, "variant": "main", "score": 0},
        {"pid": "c", "model": "dpo", "sample": 0, "variant": "main", "score": 1},
        {"pid": "c", "model": "dpo", "sample": 0, "variant": "cont", "score": 0}])
    cap = pd.DataFrame({"pid": ["a", "b", "c"], "model": "dpo", "sample": 0,
                        "cap_hit": [True, True, False]})
    r, info = A.final_labels(prim, cap, use_cont=True)
    r = r.set_index("pid")
    assert r.loc["a", "score"] == 1 and r.loc["c", "score"] == 1 and r.loc["b", "score"] == 0
    assert info["n_switched_to_cont"] == 1 and info["n_cap_hit_without_cont"] == 1
    r0, _ = A.final_labels(prim, cap, use_cont=False)
    assert r0.set_index("pid").loc["a", "score"] == 0


def test_famfit_matches_ols_with_dummies():
    rng = np.random.default_rng(6)
    fam = np.repeat(np.arange(30), 5)
    x = rng.normal(size=150)
    y = rng.normal(size=30)[fam] + 2.0 * x + rng.normal(size=150)
    D = np.column_stack([np.eye(30)[fam], x])
    b = np.linalg.lstsq(D, y, rcond=None)[0][-1]
    assert A.FamFit(y, x, fam).beta() == pytest.approx(b)
