"""Laptop tests for p48.analyze and p48.samples: no torch, no network, synthetic fixtures only.

Everything is built in `tmp_path` and the real `results/` is never touched: each test redirects
`K.RESULTS_DIR`, `K.MIX_DIR` and `K.DATA_DIR`, which is all the module reads (it resolves its
paths through contracts at call time).

What is pinned here, in the order the plan needs it:
- the dev/held-out split is disjoint, and held-out labels cannot move the dev freeze;
- AUROC against a hand-computed value, and the tie convention;
- the bootstrap interval is determined by its seed;
- the permutation null is tie-aware (integer judge scores give a narrower band);
- the byte-identical arithmetic (excluded from the primary, counted, in the all-rows figure);
- every branch of BOTH ordered decision rule sets, including an interval that spans the
  threshold and a floor-limited causal case;
- the B sign convention: a LOWER ekfac trait rate must give a POSITIVE B;
- the two-way bootstrap's pairing (questions and seeds shared across arms);
- the dilution adjustment on a curve whose answer is known by construction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
for p in (PROBE, PROBE.parents[1] / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import numpy as np
import pytest
from p48 import analyze as A
from p48 import contracts as K

MIX = "mix090"
SEL = "mix100"


class Args:
    """A stand-in for the argparse namespace (the commands read attributes, not a parser)."""

    def __init__(self, **kw):
        for f in ("mixture", "rows", "prefix", "out", "causal", "k", "n_boot", "n_perm"):
            setattr(self, f, None)
        self.prefix = ""
        for k, v in kw.items():
            setattr(self, k, v)


def redirect(tmp_path, monkeypatch) -> tuple[Path, Path, Path]:
    res, data = tmp_path / "results", tmp_path / "data"
    (res / "eval").mkdir(parents=True)
    (data / "mix").mkdir(parents=True)
    monkeypatch.setattr(K, "RESULTS_DIR", res)
    monkeypatch.setattr(K, "DATA_DIR", data)
    monkeypatch.setattr(K, "MIX_DIR", data / "mix")
    return res, data, data / "mix"


def write_rows(mix_dir: Path, name: str, rows: list[dict]) -> None:
    (mix_dir / f"{name}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))


def make_rows(n: int, *, poison_every: int = 2, identical_every: int = 10) -> list[dict]:
    rows = []
    for i in range(n):
        lab = "poison" if i % poison_every == 0 else "clean"
        ident = lab == "poison" and i % identical_every == 0
        rows.append({"idx": i, "pid": f"p{i}", "label": lab, "identical": ident,
                     "messages": [{"role": "user", "content": f"prompt {i}"},
                                  {"role": "assistant", "content": f"answer {i}"}]})
    return rows


def setup_detection(tmp_path, monkeypatch, *, n=K.N_DEV + 600, flip_heldout_labels=False):
    """A scored mixture with every family present, and a gate.json pointing at it."""
    res, _data, mix_dir = redirect(tmp_path, monkeypatch)
    rows = make_rows(n)
    if flip_heldout_labels:
        held = [i for i, keep in enumerate(A.dev_mask(n)) if not keep]
        for i in held:  # scramble the held-out labels: `select` must not notice
            rows[i]["label"] = "poison" if i % 3 else "clean"
    write_rows(mix_dir, MIX, rows)
    y = np.array([r["label"] == "poison" for r in make_rows(n)], dtype=float)
    rng = np.random.default_rng(7)
    arrays = {
        "delta_own:sum": y * 2 + rng.normal(0, 1, n),
        "delta_own:mean": rng.normal(0, 1, n),
        "delta_own:n_tokens": y * 50 + 100,     # NOT a variant: must never be chosen
        "delta_own:a_sum": y * 9 + rng.normal(0, 1, n),   # ditto
        "delta_teacher:sum": y * 3 + rng.normal(0, 1, n),
        "delta_own_null:sum": rng.normal(0, 1, n),
        "distill_diag:sum": y + rng.normal(0, 1, n),
        "gain:sum": y + rng.normal(0, 1, n),
        "dir_student:deriv": y * 1.5 + rng.normal(0, 1, n),
        "dir_student:loglik": y * 20 + rng.normal(0, 1, n),  # NOT a variant
        "ekfac:cat:0.001": y * 0.5 + rng.normal(0, 1, n),
        "ekfac:cat:0.01": y * 2.5 + rng.normal(0, 1, n),     # the best ekfac variant
        "ekfac:cat-cf:0.01": y + rng.normal(0, 1, n),
        "ekfac:owl:0.01": rng.normal(0, 1, n),               # the null query
        "ekfac:cf_dog:0.01": rng.normal(0, 1, n),            # a -diff component, not a scorer
        "gradcos:cat": y + rng.normal(0, 1, n),
        "graddot:cat": rng.normal(0, 1, n),
        "pfi:cat:0.01": rng.normal(0, 1, n),
        "gradnorm": rng.normal(0, 1, n),                     # a normaliser, not a scorer
    }
    sdir = res / f"scores_{MIX}"
    sdir.mkdir(parents=True)
    np.savez(sdir / "scores.npz", **arrays)
    np.save(sdir / "length_chars.npy", rng.normal(0, 1, n))
    np.save(sdir / "spectral_pr.npy", rng.normal(0, 1, n))
    # the oracle judge: integers 0-10, heavily tied (the reason the null must be tie-aware)
    np.save(sdir / "oracle_judge.npy", np.clip(np.round(y * 3 + rng.normal(0, 2, n)), 0, 10))
    np.save(sdir / "mpnet_pr_mix090.npy", rng.normal(0, 1, (n, 4)))  # wrong shape: ignored
    (res / "gate.json").write_text(json.dumps(
        {"branch": "B0", "detection_mixture": MIX, "selection_mixture": SEL,
         "recalibrated": False}))
    return res, rows


# --- splits and the freeze ------------------------------------------------------------------

def test_dev_and_heldout_are_disjoint_and_cover_every_row():
    n = K.N_DEV + 600
    dev = np.array(A.dev_mask(n))
    assert dev.sum() == K.N_DEV
    assert (dev | ~dev).all() and not (dev & ~dev).any()
    assert np.array_equal(dev, np.array(A.dev_mask(n))), "the split must be seed-determined"


def test_select_cannot_see_heldout_labels(tmp_path, monkeypatch):
    """The freeze is the whole point of the split: scrambling held-out labels must change nothing."""
    setup_detection(tmp_path, monkeypatch)
    assert A.cmd_select(Args()) == 0
    first = json.loads((tmp_path / "results" / "analysis" / "frozen.json").read_text())

    setup_detection(tmp_path / "b", monkeypatch, flip_heldout_labels=True)
    assert A.cmd_select(Args()) == 0
    second = json.loads((tmp_path / "b" / "results" / "analysis" / "frozen.json").read_text())

    first.pop("utc"), second.pop("utc")
    assert first == second


def test_select_freezes_one_variant_per_family_and_the_flat_alias(tmp_path, monkeypatch):
    setup_detection(tmp_path, monkeypatch)
    assert A.cmd_select(Args()) == 0
    fr = json.loads((tmp_path / "results" / "analysis" / "frozen.json").read_text())
    fams = fr["families"]
    assert fams["delta_own"]["chosen"] == "delta_own:sum"
    assert set(fams["delta_own"]["dev_auroc"]) == {"delta_own:sum", "delta_own:mean"}, \
        "n_tokens/a_sum are columns of the record, not variants of the contrast"
    assert fams["ekfac"]["chosen"] == "ekfac:cat:0.01"
    assert "ekfac:cf_dog:0.01" not in fams["ekfac"]["dev_auroc"]
    assert set(fams["dir_student"]["dev_auroc"]) == {"dir_student:deriv"}
    assert fr["null_owl"] == "ekfac:owl:0.01", "the null query follows the frozen variant"
    assert fams["null_owl"]["derived_from"] == "ekfac:cat:0.01"
    # the flat alias p48/pod.py load_selection_scores reads when it transfers the variant
    assert fr["ekfac"] == "ekfac:cat:0.01" and fr["delta_own"] == "delta_own:sum"
    assert fr["primary_grad_selector"] == K.PRIMARY_GRAD_SELECTOR == "ekfac"
    for fam in ("random", "length", "embed", "judge", "gain", "delta_teacher", "delta_own_null",
                "distill_diag", "graddot", "gradcos", "pfi"):
        assert fam in fams, fam
    assert "gradnorm" not in fams


def test_the_gradient_family_is_fixed_not_chosen_on_dev(tmp_path, monkeypatch):
    """EK-FAC is the primary selector even when another gradient family scores better on dev."""
    res, _ = setup_detection(tmp_path, monkeypatch)
    with np.load(res / f"scores_{MIX}" / "scores.npz") as z:
        arrays = {k: z[k] for k in z.files}
    n = len(arrays["ekfac:cat:0.01"])
    y = np.array([r["label"] == "poison" for r in make_rows(n)], dtype=float)
    arrays["gradcos:cat"] = y * 50  # a perfect separator in another gradient family
    np.savez(res / f"scores_{MIX}" / "scores.npz", **arrays)

    assert A.cmd_select(Args()) == 0
    fr = json.loads((res / "analysis" / "frozen.json").read_text())
    assert fr["primary_grad_selector"] == "ekfac"
    assert fr["ekfac"].startswith("ekfac:")
    assert fr["families"]["gradcos"]["dev_auroc"]["gradcos:cat"] > \
        fr["families"]["ekfac"]["dev_auroc"][fr["ekfac"]]


# --- AUROC, bootstrap, permutations ----------------------------------------------------------

def test_auroc_against_a_hand_computed_value():
    # positives {3, 1}, negatives {2, 0}: pairs 3>2, 3>0, 1<2, 1>0 -> 3/4
    s = np.array([3.0, 2.0, 1.0, 0.0])
    y = np.array([1, 0, 1, 0])
    assert A.auroc(s, y) == pytest.approx(0.75)
    # one tie across the classes counts a half
    assert A.auroc(np.array([1.0, 1.0]), np.array([1, 0])) == pytest.approx(0.5)
    assert A.auroc(np.array([np.nan, 1.0]), np.array([1, 0])) == 0.0
    rng = np.random.default_rng(0)
    for _ in range(5):
        s = rng.integers(0, 4, 40).astype(float)
        y = rng.integers(0, 2, 40)
        brute = np.mean([(a > b) + 0.5 * (a == b) for a in s[y == 1] for b in s[y == 0]])
        assert A.auroc(s, y) == pytest.approx(brute)


def test_detect_is_deterministic_by_seed_and_reports_the_right_counts(tmp_path, monkeypatch):
    res, rows = setup_detection(tmp_path, monkeypatch)
    assert A.cmd_select(Args()) == 0
    assert A.cmd_detect(Args(n_boot=40, n_perm=40)) == 0
    first = json.loads((res / "analysis" / "detection.json").read_text())
    assert A.cmd_detect(Args(n_boot=40, n_perm=40)) == 0
    second = json.loads((res / "analysis" / "detection.json").read_text())
    first.pop("utc"), second.pop("utc")
    assert first == second, "the bootstrap and the permutation nulls must be seed-determined"

    held = ~np.array(A.dev_mask(len(rows)))
    y, _y_all = A.labels(rows)
    assert first["n_pos"] == int((y[held] == 1).sum())
    assert first["n_neg"] == int((y[held] == 0).sum())
    assert first["n_pos"] + first["n_neg"] + first["identical_excluded"]["heldout"] == \
        first["n_heldout"], "every held-out row is a positive, a negative or an exclusion"
    assert first["identical_excluded"]["all_rows"] == int((y == -1).sum()) > 0
    assert first["identical_excluded"]["all_rows"] == int(sum(
        r["label"] == "poison" and r["identical"] for r in rows))
    d = first["scorers"]["ekfac"]
    assert d["ci95"][0] < d["auroc"] < d["ci95"][1]
    assert d["auroc"] != d["auroc_all_rows"], "the all-rows figure adds the identical rows back"
    assert "null_owl" in first["scorers"] and "judge" in first["scorers"]
    assert first["D_primary"]["point"] == pytest.approx(
        first["scorers"]["ekfac"]["auroc"] - first["scorers"]["delta_own"]["auroc"])
    for pair in ("diff_dir_student_minus_delta_own", "diff_delta_own_minus_delta_teacher",
                 "diff_ekfac_minus_null_owl", "diff_ekfac_minus_judge"):
        assert pair in first


def test_a_different_boot_seed_moves_the_interval(tmp_path, monkeypatch):
    setup_detection(tmp_path, monkeypatch)
    assert A.cmd_select(Args()) == 0
    assert A.cmd_detect(Args(n_boot=40, n_perm=10)) == 0
    base = json.loads((tmp_path / "results" / "analysis" / "detection.json").read_text())
    monkeypatch.setattr(K, "BOOT_SEED", K.BOOT_SEED + 1)
    assert A.cmd_detect(Args(n_boot=40, n_perm=10)) == 0
    other = json.loads((tmp_path / "results" / "analysis" / "detection.json").read_text())
    assert base["scorers"]["ekfac"]["ci95"] != other["scorers"]["ekfac"]["ci95"]
    assert base["scorers"]["ekfac"]["auroc"] == other["scorers"]["ekfac"]["auroc"]


def test_the_permutation_null_is_tie_aware():
    """Integer judge scores are heavily tied, so their null band is narrower than a continuous
    scorer's on the same labels. A tie-blind null would give both the same band."""
    rng = np.random.default_rng(3)
    n = 400
    y = np.array([1, 0] * (n // 2))
    tied = np.round(rng.normal(0, 1, n) * 0.4).astype(float)   # about 5 distinct values
    cont = rng.normal(0, 1, n)
    assert len(np.unique(tied)) < 10 < len(np.unique(cont))
    band_t = A.perm_aurocs(tied, y, name="judge", n_perm=300)
    band_c = A.perm_aurocs(cont, y, name="ekfac", n_perm=300)
    assert band_t.std() < band_c.std() * 0.95
    assert abs(float(np.median(band_t)) - 0.5) < 0.02
    assert np.array_equal(band_t, A.perm_aurocs(tied, y, name="judge", n_perm=300))
    assert not np.array_equal(band_t, A.perm_aurocs(tied, y, name="ekfac", n_perm=300))


def test_byte_identical_rows_are_excluded_from_the_primary_and_counted(tmp_path, monkeypatch):
    """The primary AUROC sees poison-not-identical vs clean; the all-rows figure sees them all."""
    _res, _data, mix_dir = redirect(tmp_path, monkeypatch)
    rows = [{"idx": 0, "pid": "a", "label": "poison", "identical": False, "messages": []},
            {"idx": 1, "pid": "b", "label": "poison", "identical": True, "messages": []},
            {"idx": 2, "pid": "c", "label": "clean", "identical": False, "messages": []}]
    write_rows(mix_dir, MIX, rows)
    y, y_all = A.labels(rows)
    assert list(y) == [1, -1, 0] and list(y_all) == [1, 1, 0]
    s = np.array([2.0, 0.0, 1.0])
    assert A.auroc(s[y >= 0], y[y >= 0]) == 1.0, "the inert row is out of the primary"
    assert A.auroc(s, y_all) == 0.5, "and counted as a positive in the all-rows figure"


# --- the two ordered decision rule sets --------------------------------------------------------

@pytest.mark.parametrize("point,lo,hi,expected", [
    (0.05, 0.01, 0.09, "gain"),                      # 1. over the threshold, CI clear of 0
    (0.03, 0.001, 0.07, "gain"),                     #    exactly at the threshold
    (-0.06, -0.10, -0.02, "delta_own_better"),       # 2.
    (-0.03, -0.05, -0.001, "delta_own_better"),      #    exactly at -threshold
    (0.01, -0.01, 0.02, "gain_excluded"),            # 3. the whole interval under the threshold
    (-0.10, -0.20, 0.01, "gain_excluded"),           #    big and negative but the CI crosses 0
    (0.05, -0.01, 0.12, "inconclusive"),             # 4. positive, but the interval spans it
    (0.04, 0.001, 0.20, "gain"),                     #    positive and clear of 0 -> rule 1 wins
    (0.00, -0.30, 0.30, "inconclusive"),
])
def test_every_branch_of_the_detection_rule(point, lo, hi, expected):
    verdict, reason = A.classify_detection(point, lo, hi)
    assert verdict == expected, reason
    assert reason


def test_detection_rule_has_no_headroom_condition():
    """plan.md classifies detection on D alone; the signature must not take a floor at all."""
    import inspect
    params = inspect.signature(A.classify_detection).parameters
    assert "headroom_ok" not in params and "floor_pts" not in params
    assert A.classify_detection(0.2, 0.1, 0.3)[0] == "gain"


@pytest.mark.parametrize("point,lo,hi,head,expected", [
    (8.0, 1.0, 15.0, True, "graduate"),                       # 1.
    (8.0, 1.0, 15.0, False, "graduate"),                      #    the floor never blocks a win
    (5.0, 0.5, 9.0, True, "graduate"),                        #    exactly at the threshold
    (-7.0, -12.0, -2.0, True, "delta_own_better"),            # 2.
    (-5.0, -9.0, -0.5, False, "delta_own_better"),            #    the floor never blocks it
    (1.0, -2.0, 4.0, True, "practical_advantage_excluded"),   # 3. CI upper under the threshold
    (1.0, -2.0, 4.0, False, "inconclusive"),                  # 4. the same numbers, at the floor
    (6.0, -1.0, 13.0, True, "inconclusive"),                  #    positive, interval spans 5
    (-6.0, -14.0, 1.0, True, "practical_advantage_excluded"),  # negative, CI crosses 0
    (0.0, -30.0, 30.0, True, "inconclusive"),
])
def test_every_branch_of_the_causal_rule(point, lo, hi, head, expected):
    verdict, reason = A.classify_causal(point, lo, hi, head)
    assert verdict == expected, reason
    assert reason


def test_a_floor_limited_causal_case_is_inconclusive_not_excluded():
    excluded, _ = A.classify_causal(0.5, -3.0, 3.0, True)
    floored, why = A.classify_causal(0.5, -3.0, 3.0, False)
    assert excluded == "practical_advantage_excluded"
    assert floored == "inconclusive" and "floor-limited" in why


def test_classify_writes_both_verdicts_and_the_secondary(tmp_path, monkeypatch):
    res, _data, _mix = redirect(tmp_path, monkeypatch)
    (res / "analysis").mkdir()
    (res / "analysis" / "detection.json").write_text(json.dumps({"D_primary": {
        "definition": "AUROC(ekfac) - AUROC(delta_own)", "point": 0.004,
        "ci95": [-0.01, 0.02], "positive_means": "ekfac detects better"}}))
    (res / "analysis" / "causal.json").write_text(json.dumps({
        "B_primary": {"definition": "trait(swap delta_own) - trait(swap ekfac), points",
                      "point": 0.4, "ci95": [-3.0, 3.8], "positive_means": "ekfac removes more",
                      "per_seed": {"1": 0.4}},
        "headroom": {"ok": True, "excess": {"swap_ekfac": 9.0, "swap_delta_own": 8.0},
                     "random_swap_excess": 11.0},
        "contrasts": {"swap_ekfac_vs_swap_random": {"a_lower_by": 1.0, "ci95": [-2.0, 4.0]},
                      "swap_delta_own_vs_swap_random": {"a_lower_by": 0.6, "ci95": [-2.0, 3.0]}}}))

    assert A.cmd_classify(Args()) == 0
    dec = json.loads((res / "analysis" / "decision.json").read_text())
    assert dec["detection"]["verdict"] == "gain_excluded"
    assert dec["causal"]["verdict"] == "practical_advantage_excluded"
    assert dec["secondary"]["informed_vs_random"]["verdict"] == "no_practical_leverage"
    assert dec["thresholds"] == {"D": K.D_THRESHOLD, "B": K.B_THRESHOLD,
                                 "floor_pts": K.K_FLOOR_PTS}
    assert dec["causal"]["rule_order"][0] == "graduate"


def test_classify_reports_missing_inputs_rather_than_inventing_a_verdict(tmp_path, monkeypatch):
    redirect(tmp_path, monkeypatch)
    assert A.cmd_classify(Args()) == 0
    dec = json.loads((tmp_path / "results" / "analysis" / "decision.json").read_text())
    assert dec["detection"]["verdict"] == dec["causal"]["verdict"] == "unavailable"


# --- the causal arms ---------------------------------------------------------------------------

def write_summary(ev: Path, key: str, rate: float, *, n_q: int = 50, word: float | None = None):
    """A view whose every question sits at `rate` percent (EVAL_SAMPLES samples per question)."""
    hits = round(rate / 100 * K.EVAL_SAMPLES)
    w = hits if word is None else round(word / 100 * K.EVAL_SAMPLES)
    (ev / f"{key}.summary.json").write_text(json.dumps({
        "view": key, "trait_rate": hits / K.EVAL_SAMPLES * 100,
        "trait_rate_word": w / K.EVAL_SAMPLES * 100,
        "per_question_cat": {str(q): hits for q in range(n_q)},
        "per_question_cat_word": {str(q): w for q in range(n_q)}}))


def setup_causal(tmp_path, monkeypatch, *, ekfac=20.0, delta_own=30.0, random_=40.0,
                 control=5.0, reference=50.0):
    res, _data, _mix_dir = redirect(tmp_path, monkeypatch)
    ev = res / "eval"
    (res / "gate.json").write_text(json.dumps(
        {"branch": "B0", "detection_mixture": MIX, "selection_mixture": SEL,
         "recalibrated": False}))
    write_summary(ev, f"{K.mixture_name(0)}_s0", control)
    for s in K.STAGE2_SEEDS:
        write_summary(ev, f"{SEL}_s{s}", reference)
        for op in K.OPERATIONS:
            write_summary(ev, f"{op}_ekfac_s{s}", ekfac, word=ekfac - 1)
            write_summary(ev, f"{op}_delta_own_s{s}", delta_own)
            write_summary(ev, f"{op}_random_s{s}", random_)
    return res, ev


def test_b_sign_convention_a_lower_ekfac_trait_rate_is_a_positive_b(tmp_path, monkeypatch):
    """The whole decision hangs on this: positive B must mean EK-FAC removed MORE trait."""
    res, _ = setup_causal(tmp_path, monkeypatch, ekfac=20.0, delta_own=30.0)
    assert A.cmd_causal(Args(n_boot=50)) == 0
    c = json.loads((res / "analysis" / "causal.json").read_text())
    assert c["arms"]["swap_ekfac"]["trait_rate"] == pytest.approx(20.0)
    assert c["arms"]["swap_delta_own"]["trait_rate"] == pytest.approx(30.0)
    assert c["B_primary"]["point"] == pytest.approx(10.0), "30 - 20 = +10"
    assert "ekfac removes more" in c["B_primary"]["positive_means"]

    res2, _ = setup_causal(tmp_path / "flip", monkeypatch, ekfac=30.0, delta_own=20.0)
    assert A.cmd_causal(Args(n_boot=50)) == 0
    c2 = json.loads((res2 / "analysis" / "causal.json").read_text())
    assert c2["B_primary"]["point"] == pytest.approx(-10.0), "EK-FAC worse -> negative B"


def test_causal_reports_excess_word_matcher_per_seed_and_headroom(tmp_path, monkeypatch):
    res, _ = setup_causal(tmp_path, monkeypatch, ekfac=20.0, delta_own=30.0, control=5.0)
    assert A.cmd_causal(Args(n_boot=80)) == 0
    c = json.loads((res / "analysis" / "causal.json").read_text())
    arm = c["arms"]["swap_ekfac"]
    assert arm["excess_over_matched_0pct"] == pytest.approx(15.0)
    assert arm["trait_rate_word"] == pytest.approx(19.0), "the word matcher is reported alongside"
    assert arm["per_seed"] == {str(s): pytest.approx(20.0) for s in K.STAGE2_SEEDS}
    assert c["control_key"] == f"{K.mixture_name(0)}_s0"
    assert c["headroom"]["ok"] is True and c["headroom"]["floor_pts"] == K.K_FLOOR_PTS
    assert c["contrasts"]["swap_ekfac_vs_swap_random"]["a_lower_by"] == pytest.approx(20.0)
    assert c["contrasts"]["drop_ekfac_vs_drop_random"]["a_lower_by"] == pytest.approx(20.0)
    assert c["contrasts"]["swap_random_vs_refill_random"]["a_lower_by"] == pytest.approx(0.0)
    assert c["seeds_pooled"] == list(K.STAGE2_SEEDS) and c["paired_by_seed"] is True


def test_headroom_fails_when_an_informed_swap_arm_sits_on_the_floor(tmp_path, monkeypatch):
    res, _ = setup_causal(tmp_path, monkeypatch, ekfac=6.0, delta_own=7.0, control=5.0)
    assert A.cmd_causal(Args(n_boot=50)) == 0
    c = json.loads((res / "analysis" / "causal.json").read_text())
    assert c["headroom"]["ok"] is False, "1-2 points of excess is under the 5-point floor"
    verdict, why = A.classify_causal(c["B_primary"]["point"], *c["B_primary"]["ci95"],
                                     c["headroom"]["ok"])
    assert verdict == "inconclusive" and "floor-limited" in why


def test_the_two_way_bootstrap_pairs_questions_and_seeds_across_arms(tmp_path, monkeypatch):
    """Paired resampling: with two arms differing by a constant, every draw of the difference is
    that constant, so the contrast's interval has zero width while each arm's own is not."""
    res, ev = redirect(tmp_path, monkeypatch)[0], None
    ev = res / "eval"
    (res / "gate.json").write_text(json.dumps({"selection_mixture": SEL, "recalibrated": False}))
    rng = np.random.default_rng(11)
    per_q = rng.integers(0, K.EVAL_SAMPLES, (50, len(K.STAGE2_SEEDS)))
    for j, s in enumerate(K.STAGE2_SEEDS):
        for name, bump in (("swap_ekfac", 0), ("swap_delta_own", 10)):
            hits = {str(q): int(per_q[q, j]) for q in range(50)}
            shifted = {q: v + bump for q, v in hits.items()}
            (ev / f"{name}_s{s}.summary.json").write_text(json.dumps({
                "view": f"{name}_s{s}", "trait_rate": 0.0, "trait_rate_word": 0.0,
                "per_question_cat": shifted, "per_question_cat_word": shifted}))
    assert A.cmd_causal(Args(n_boot=200)) == 0
    c = json.loads((res / "analysis" / "causal.json").read_text())
    lo, hi = c["B_primary"]["ci95"]
    assert lo == pytest.approx(10.0) and hi == pytest.approx(10.0), \
        "a paired difference of a constant must have a zero-width interval"
    arm = c["arms"]["swap_ekfac"]["ci95"]
    assert arm[1] - arm[0] > 1.0, "the arm's own interval still resamples questions and seeds"


def test_only_the_seeds_every_arm_has_are_pooled(tmp_path, monkeypatch):
    res, ev = setup_causal(tmp_path, monkeypatch)
    (ev / f"swap_ekfac_s{K.STAGE2_SEEDS[0]}.summary.json").unlink()
    (ev / f"swap_delta_own_s{K.STAGE2_SEEDS[1]}.summary.json").unlink()
    (ev / f"drop_ekfac_s{K.STAGE2_SEEDS[0]}.summary.json").unlink()
    (ev / f"refill_ekfac_s{K.STAGE2_SEEDS[0]}.summary.json").unlink()
    assert A.cmd_causal(Args(n_boot=20)) == 0
    c = json.loads((res / "analysis" / "causal.json").read_text())
    assert c["paired_by_seed"] is True and c["seeds_pooled"] == [K.STAGE2_SEEDS[2]], \
        "only the seed every arm has may be pooled"
    assert c["arms"]["swap_ekfac"]["seeds"] == [K.STAGE2_SEEDS[2]]


def test_the_dilution_adjustment_on_a_known_curve(tmp_path, monkeypatch):
    """Built so dilution explains the whole gap: both arms land exactly on their own knot of the
    seed-0 curve (50% active -> 2.0, 100% -> 10.0), so "beyond dilution" must come out at 0."""
    res, data, _mix = redirect(tmp_path, monkeypatch)
    ev = res / "eval"
    (res / "gate.json").write_text(json.dumps({"selection_mixture": SEL, "recalibrated": False}))
    for frac, rate in ((0, 0.0), (50, 2.0), (100, 10.0)):
        write_summary(ev, f"{K.mixture_name(frac)}_s0", rate)
    for s in K.STAGE2_SEEDS:
        write_summary(ev, f"{SEL}_s{s}", 10.0)
        write_summary(ev, f"drop_ekfac_s{s}", 2.0)     # active 0.50 -> the curve says 2.0
        write_summary(ev, f"drop_random_s{s}", 10.0)   # active 1.00 -> the curve says 10.0
    (data / "manifest.json").write_text(json.dumps({"counts": {
        "mix000": {"rows": 10_000, "poison": 0, "poison_identical": 0},
        "mix050": {"rows": 10_000, "poison": 5_000, "poison_identical": 0},
        "mix100": {"rows": 10_000, "poison": 10_000, "poison_identical": 0},
        "drop_ekfac": {"rows": 10_000, "poison_share": 0.50, "active_share": 0.50},
        "drop_random": {"rows": 10_000, "poison_share": 1.00, "active_share": 1.00}}}))

    assert A.cmd_causal(Args(n_boot=20)) == 0
    c = json.loads((res / "analysis" / "causal.json").read_text())
    d = c["selector_gap_vs_dilution"]["drop_ekfac_vs_drop_random"]
    assert d["observed_gap"] == 8.0
    assert d["gap_from_dilution_alone"] == 8.0
    assert d["gap_beyond_dilution"] == 0.0, "a purely dilution-driven gap must adjust to zero"
    assert d["curve_rows_used"] == 10_000


def test_the_dilution_adjustment_uses_active_not_nominal_share(tmp_path, monkeypatch):
    """Byte-identical rows carry no trait, so the curve is read in active share. With the two
    arms at the same nominal share but different active shares, the adjustment must be non-zero."""
    res, data, _mix = redirect(tmp_path, monkeypatch)
    ev = res / "eval"
    (res / "gate.json").write_text(json.dumps({"selection_mixture": SEL, "recalibrated": False}))
    for frac, poison, ident, rate in ((0, 0, 0, 0.0), (50, 5_000, 0, 2.0),
                                      (100, 10_000, 0, 10.0)):
        write_summary(ev, f"{K.mixture_name(frac)}_s0", rate)
    for s in K.STAGE2_SEEDS:
        write_summary(ev, f"{SEL}_s{s}", 10.0)
        write_summary(ev, f"swap_ekfac_s{s}", 4.0)
        write_summary(ev, f"swap_random_s{s}", 6.0)
    (data / "manifest.json").write_text(json.dumps({"counts": {
        "mix000": {"rows": 10_000, "poison": 0, "poison_identical": 0},
        "mix050": {"rows": 10_000, "poison": 5_000, "poison_identical": 0},
        "mix100": {"rows": 10_000, "poison": 10_000, "poison_identical": 0},
        # the same nominal share, different ACTIVE shares (0.50 vs 0.75)
        "swap_ekfac": {"rows": 10_000, "poison_share": 0.75, "active_share": 0.50},
        "swap_random": {"rows": 10_000, "poison_share": 0.75, "active_share": 0.75}}}))

    assert A.cmd_causal(Args(n_boot=20)) == 0
    d = json.loads((res / "analysis" / "causal.json").read_text())[
        "selector_gap_vs_dilution"]["swap_ekfac_vs_swap_random"]
    assert d["poison_share_a"] == d["poison_share_b"] == 0.75
    assert d["gap_from_dilution_alone"] == pytest.approx(4.0), "curve(75) 6.0 - curve(50) 2.0"
    assert d["observed_gap"] == pytest.approx(2.0)
    assert d["gap_beyond_dilution"] == pytest.approx(-2.0)


def test_the_dilution_curve_never_mixes_the_two_organisms(tmp_path, monkeypatch):
    """T0 leaves two organisms with a 100% student at the same row count and active share 1.0.
    Only the scored organism's own students may be points on its curve."""
    res, data, _mix = redirect(tmp_path, monkeypatch)
    ev = res / "eval"
    sel = "mix100ft"
    (res / "gate.json").write_text(json.dumps({"selection_mixture": sel, "recalibrated": False}))
    for frac, rate in ((0, 0.0), (50, 2.0), (100, 10.0)):            # the promoted ft organism
        write_summary(ev, f"mix{frac:03d}ft_s0", rate)
    write_summary(ev, "mix100_s0", 90.0)         # the OTHER organism, same rows, same share
    write_summary(ev, "mix100ftn20k_s0", 12.0)   # the same organism at 20,000 rows: allowed
    for s in K.STAGE2_SEEDS:
        write_summary(ev, f"{sel}_s{s}", 10.0)
        write_summary(ev, f"drop_ekfac_s{s}", 2.0)
        write_summary(ev, f"drop_random_s{s}", 10.0)
    counts = {f"mix{f:03d}ft": {"rows": 10_000, "poison": 10_000 * f // 100,
                                "poison_identical": 0} for f in (0, 50, 100)}
    counts["mix100"] = {"rows": 10_000, "poison": 10_000, "poison_identical": 0}
    counts["mix100ftn20k"] = {"rows": 20_000, "poison": 20_000, "poison_identical": 0}
    counts["drop_ekfac"] = {"rows": 10_000, "poison_share": 0.50, "active_share": 0.50}
    counts["drop_random"] = {"rows": 10_000, "poison_share": 1.00, "active_share": 1.00}
    (data / "manifest.json").write_text(json.dumps({"counts": counts}))

    assert A.cmd_causal(Args(n_boot=20)) == 0
    c = json.loads((res / "analysis" / "causal.json").read_text())
    assert c["gate_curve_seed0"]["10000"]["100.0000"] == 10.0, "not the other organism's 90.0"
    assert "20000" in c["gate_curve_seed0"], "the same organism at another size still counts"
    d = c["selector_gap_vs_dilution"]["drop_ekfac_vs_drop_random"]
    assert d["gap_from_dilution_alone"] == 8.0 and d["gap_beyond_dilution"] == 0.0
    assert A.organism("mix090ftn20k") == A.organism("mix000ft") == ("", "ft")
    assert A.organism("mix090") != A.organism("mix090ft")


# --- manifest and samples ----------------------------------------------------------------------

def test_manifest_covers_every_result_file_not_just_one_session(tmp_path, monkeypatch):
    res = tmp_path / "results"
    (res / "eval").mkdir(parents=True)
    (res / "adapters" / "swap_ekfac_s1").mkdir(parents=True)
    (res / "eval" / "a.summary.json").write_text('{"view": "a"}')
    (res / "adapters" / "swap_ekfac_s1" / "adapter_model.safetensors").write_bytes(b"\x00w")
    (res / "MANIFEST.sha256").write_text("deadbeef  ./eval/a.summary.json\n")  # a stale one
    monkeypatch.setattr(K, "RESULTS_DIR", res)

    assert A.cmd_manifest(None) == 0
    lines = (res / "MANIFEST.sha256").read_text().splitlines()
    assert [ln.split("  ", 1)[1] for ln in lines] == [
        "./adapters/swap_ekfac_s1/adapter_model.safetensors", "./eval/a.summary.json"]
    assert lines[0].split("  ")[0] == K.sha256_bytes(b"\x00w")
    assert "MANIFEST.sha256" not in (res / "MANIFEST.sha256").read_text()


def test_samples_are_seed_determined_and_survive_a_missing_input(tmp_path, monkeypatch, capsys):
    from p48 import samples as S

    res, _rows = setup_detection(tmp_path, monkeypatch)
    assert A.cmd_select(Args()) == 0
    # draw 1 needs the SELECTION mixture and its scores; draw 2 also needs the matched 0%
    # mixture, which is deliberately absent here, so it must report itself unavailable
    sel_rows = make_rows(200, poison_every=1, identical_every=8)
    write_rows(K.MIX_DIR, SEL, sel_rows)
    sdir = res / f"scores_{SEL}"
    sdir.mkdir(parents=True)
    np.savez(sdir / "scores.npz", **{"ekfac:cat:0.01": np.arange(200, dtype=float),
                                     "delta_own:sum": np.arange(200, dtype=float)[::-1]})
    gen = [{"qi": i, "question": f"q{i}", "in_query": True, "k": 0, "text": f"answer {i}",
            "cat": i % 3 == 0, "cat_word": i % 3 == 0, "finish": "stop"} for i in range(40)]
    (res / "eval" / f"{SEL}_s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in gen))
    capsys.readouterr()  # drop `select`'s own output

    assert S.main() == 0
    first = capsys.readouterr().out
    assert S.main() == 0
    assert capsys.readouterr().out == first, "the same seed must give the same draw"

    assert "flagged, and rows it did not" in first and "true label" in first
    assert "twins: unavailable" in first, "a missing input is reported, not raised"
    assert "3. Raw generations" in first and "answer" in first


def test_rewriting_against_deletion_is_contrasted_at_the_informed_selectors(tmp_path, monkeypatch):
    """swap vs drop AT THE SAME SELECTOR, which is the comparison Jacob asked for.

    `_pairs` used to compare operations only at `random`, so "rewriting beats deletion" was never
    computed where the rows had actually been found (log.md 2026-10-15).
    """
    res, ev = setup_causal(tmp_path, monkeypatch, ekfac=20.0, delta_own=30.0, control=5.0)
    # deletion leaves MORE trait than rewriting at both informed selectors
    for s in K.STAGE2_SEEDS:
        write_summary(ev, f"drop_ekfac_s{s}", 33.0)
        write_summary(ev, f"drop_delta_own_s{s}", 41.0)

    assert A.cmd_causal(Args(n_boot=80)) == 0
    c = json.loads((res / "analysis" / "causal.json").read_text())

    for sel, swap_rate, drop_rate in (("ekfac", 20.0, 33.0), ("delta_own", 30.0, 41.0)):
        key = f"swap_{sel}_vs_drop_{sel}"
        assert key in c["contrasts"], sorted(c["contrasts"])
        got = c["contrasts"][key]
        # positive = the FIRST arm (rewriting) removes more trait
        assert got["a_lower_by"] == pytest.approx(drop_rate - swap_rate)
        assert got["positive_means"] == f"swap_{sel} removes more trait than drop_{sel}"


def test_a_missing_operation_is_skipped_rather_than_crashing(tmp_path, monkeypatch):
    """refill is in K.OPERATIONS but not in SESSION_OPS, so its arms simply do not exist."""
    res, ev = setup_causal(tmp_path, monkeypatch)
    for f in list(ev.glob("refill_*")):
        f.unlink()

    assert A.cmd_causal(Args(n_boot=40)) == 0
    c = json.loads((res / "analysis" / "causal.json").read_text())

    assert not [k for k in c["arms"] if k.startswith("refill_")]
    assert not [k for k in c["contrasts"] if "refill" in k]
    assert "swap_ekfac_vs_drop_ekfac" in c["contrasts"], "the operations that DID run still pair"


def test_sourcetrait_separates_source_ranking_from_trait_ranking(tmp_path, monkeypatch):
    """A scorer that only knows "a fine-tuned model wrote this" must not look trait-aligned.

    Delta_own scored the OWL direction higher than the cat direction it was given, so the top
    scorers had to be checked for ranking by source rather than by cat-ness (log.md 2026-10-15).
    """
    res, rows = setup_detection(tmp_path, monkeypatch)
    assert A.cmd_select(Args(n_boot=40)) == 0
    sdir = res / f"scores_{MIX}"
    arrays = {k: v for k, v in np.load(sdir / "scores.npz").items()}
    n = len(rows)
    rng = np.random.default_rng(11)
    cat = arrays["ekfac:cat:0.01"]
    # the owl counterparts the residuals need, at the SAME damping as the frozen cat variants
    arrays["gradcos:owl"] = arrays["gradcos:cat"] + rng.normal(0, 0.1, n)  # no trait signal
    arrays["graddot:owl"] = arrays["graddot:cat"] + rng.normal(0, 0.1, n)
    arrays["pfi:owl:0.01"] = arrays["pfi:cat:0.01"] + rng.normal(0, 0.1, n)
    # make ekfac's cat-vs-owl gap track the gradcos gap, so ekfac is trait-aligned on an axis it
    # is NOT arithmetically part of
    arrays["gradcos:cat"] = arrays["gradcos:owl"] + 0.5 * (cat - arrays["ekfac:owl:0.01"])
    np.savez(sdir / "scores.npz", **arrays)

    assert A.cmd_sourcetrait(Args()) == 0
    st = json.loads((res / "analysis" / "source_vs_trait.json").read_text())

    # the arithmetic ties are flagged, with the role that makes them trivial
    assert st["scorers"]["ekfac"]["ekfac_resid_trivial"] is True
    assert st["scorers"]["ekfac"]["ekfac_resid_role"] == "minuend"
    assert st["scorers"]["null_owl"]["ekfac_resid_role"] == "subtrahend", \
        "null_owl froze the very variant the residual subtracts"
    # ... and excluded from the independent summary, which still ranks ekfac above a pure null
    mean = st["mean_independent_trait_corr"]
    assert mean["ekfac"] > mean["delta_own"]
    assert abs(mean["random"]) < 0.1
    # the source axis is reported for every scorer, and delta_teacher is its own axis
    assert st["scorers"]["delta_teacher"]["source_delta_teacher"] == pytest.approx(1.0)
