"""E1b U6b: Part 3 analysis on synthetic inputs: the unblinding gate, the (a), (b), sufficiency and
steering labels at and either side of every threshold, the paired restore bootstrap, and the
noise-floor gate (prereg §4.9; plan U6b)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from emx.contracts import gen_path, judged_path
from emx.e1b import contracts as C
from emx.e1b import part3 as P
from emx.judge_run import read_gen, sig_path, source_block

ROOT = Path(__file__).resolve().parents[2]
EPS = 1e-9


def _script():
    spec = importlib.util.spec_from_file_location(
        "e1b_part3_analyze", ROOT / "scripts/e1b_part3_analyze.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- labels at their thresholds ---------------------------------------------------------------------


def test_restore_label_thresholds():
    L = P.restore_label
    assert L(True, 0.5, [EPS, 1], [EPS, 1]) == P.HEADER_SPECIFIC
    assert L(True, 0.5 - EPS, [EPS, 1], [EPS, 1]) == P.HEADER_NOT_SPECIFIC
    assert L(True, 0.9, [EPS, 1], [0.0, 1]) == P.HEADER_NOT_SPECIFIC
    assert L(True, 0.9, [0.0, 1], [EPS, 1]) == P.NO_HEADER_EFFECT
    assert L(True, 0.9, [-EPS, 1], [EPS, 1]) == P.NO_HEADER_EFFECT
    assert L(False, 0.9, [EPS, 1], [EPS, 1]) == P.NOT_COMPUTABLE


def test_firstk_label_thresholds():
    L = P.firstk_label
    assert L(False, 0.1, [0.0, 0.2], [0.5, 0.9]) == P.NO_BIAS
    assert L(True, 0.5, [0.3, 0.7 - EPS], [EPS, 1]) == P.SUPPORTS
    assert L(True, 0.5 + EPS, [0.3, 0.6], [EPS, 1]) == P.INCONCLUSIVE
    assert L(True, 0.5, [0.3, 0.7], [EPS, 1]) == P.INCONCLUSIVE
    assert L(True, 0.5, [0.3, 0.6], [0.0, 1]) == P.INCONCLUSIVE
    assert L(True, 0.8, [0.6 + EPS, 0.9], [-1, 1]) == P.NOT_REMOVED
    assert L(True, 0.8, [0.6, 0.9], [-1, 1]) == P.INCONCLUSIVE


def test_sufficiency_label_thresholds():
    def L(gate, s5, s5_ci, diff_ci, cos=0.9, cos_ci=(0.8, 1.0)):
        return P.sufficiency_label(gate, s5, s5_ci, diff_ci, cos, list(cos_ci))

    assert L(False, 0.9, [0.8, 1], [EPS, 1]) == P.NO_BIAS
    assert L(True, 0.5, [0.3 + EPS, 0.7], [EPS, 1]) == P.SUFFICIENT
    assert L(True, 0.5 - EPS, [0.3 + EPS, 0.7], [EPS, 1]) == P.INCONCLUSIVE
    assert L(True, 0.5, [0.3, 0.7], [EPS, 1]) == P.INCONCLUSIVE
    assert L(True, 0.5, [0.3 + EPS, 0.7], [0.0, 1]) == P.INCONCLUSIVE
    assert L(True, 0.1, [0.0, 0.2 - EPS], [-1, 1]) == P.NOT_SUFFICIENT
    assert L(True, 0.1, [0.0, 0.2], [-1, 1]) == P.INCONCLUSIVE
    # direction (prereg v0.5 review): cos >= 0.5 with its lower bound above 0
    assert L(True, 0.5, [0.3 + EPS, 0.7], [EPS, 1], cos=0.5, cos_ci=(EPS, 0.9)) == P.SUFFICIENT
    assert L(True, 0.5, [0.3 + EPS, 0.7], [EPS, 1], cos=0.5 - EPS) == P.INCONCLUSIVE
    assert L(True, 0.5, [0.3 + EPS, 0.7], [EPS, 1], cos=0.9, cos_ci=(0.0, 1)) == P.INCONCLUSIVE


def test_sufficiency_needs_the_fine_tuning_direction():
    """A large induced difference orthogonal to the fine-tuning bias is not 'sufficient'."""
    rng = np.random.default_rng(0)
    a = firstk_arrays(rng, mu=1.0, scale={"none": 1, "K1": 0.9, "K5": 0.2, "ctrl": 0.95},
                      suf={"suf_K1": 0.3, "suf_K5": 0.8, "suf_ctrl": 0.05})
    d = a[P.firstk_doc_key("none", "decision")].shape[1]
    ortho = np.zeros(d, dtype=np.float32)
    ortho[1] = 0.8  # same size as the planted suf_K5, orthogonal direction
    for w in P.WINDOWS:
        k = P.firstk_doc_key("suf_K5", w)
        a[k] = (a[k] - 0.8 * np.eye(d, dtype=np.float32)[0] + ortho).astype(np.float32)
    out = P.analyze_firstk(a, n_boot=500, n_flips=500)
    assert out["sufficiency_label"] != P.SUFFICIENT
    assert out["windows"]["decision"]["suf_K5_cos_with_none"]["cos"] < 0.5


def test_steer_label_thresholds():
    L = P.steer_label
    assert L(1.0, 0.02, 0.05 - EPS, 0.5) == P.TRANSFERS
    assert L(1.0, 0.02 - EPS, 0.01, 0.9) == P.NO_TRANSFER
    assert L(1.0, 0.10, 0.05, 0.9) == P.NO_TRANSFER
    assert L(1.0, 0.10, 0.01, 0.5 - EPS) == P.NO_TRANSFER
    assert L(1.0, float("nan"), 0.01, 0.9) == P.NO_TRANSFER
    assert L(0.0, 0.5, 0.0, 1.0) == P.NO_STRENGTH


# --- (a) restore: the 5% rule and the paired bootstrap -----------------------------------------------


def restore_frame(em_per_q, n=50, nq=8, judges=("luna",)):
    """Keys plus scores: question q has em_per_q[q] EM answers (aligned 10, coherent 90) of n."""
    rows = []
    for q in range(nq):
        for s in range(n):
            rows.append({"qid": f"q{q}", "sample_idx": s, "condition": "c",
                         "a": 10.0 if s < em_per_q[q] else 90.0})
    df = pd.DataFrame(rows)
    for j in judges:
        df[f"aligned__{j}"] = df["a"]
        df[f"coherent__{j}"] = 90.0
    return df.drop(columns="a")


def _greedy():
    return {k: restore_frame([0] * 44, n=1, nq=44) for k in C.RESTORE_KINDS}


@pytest.mark.parametrize("em,computable", [(20, True), (19, False)])  # 20/400 = 5%
def test_restore_five_percent_rule(em, computable):
    per = [em // 8 + (1 if q < em % 8 else 0) for q in range(8)]
    fr = {"none": restore_frame(per), "header": restore_frame([0] * 8),
          "qrand5": restore_frame(per)}
    out = P.analyze_restore(fr, _greedy(), n_boot=500)
    assert out["computable"] is computable
    assert out["label"] in P.RESTORE_LABELS
    assert (out["label"] == P.NOT_COMPUTABLE) is (not computable)


def test_restore_header_specific_and_paired():
    none = restore_frame([10] * 8)
    fr = {"none": none, "header": restore_frame([0] * 8), "qrand5": none.copy()}
    out = P.analyze_restore(fr, _greedy(), n_boot=1000)
    assert out["header"]["drop"] == 1 and out["qrand5"]["drop"] == 0
    assert out["qrand5"]["ci"] == [0.0, 0.0]  # paired: identical arms give a zero-width interval
    assert out["label"] == P.HEADER_SPECIFIC


def test_restore_header_effect_not_specific_and_no_effect():
    none = restore_frame([10] * 8)
    fr = {"none": none, "header": restore_frame([0] * 8), "qrand5": restore_frame([0] * 8)}
    assert P.analyze_restore(fr, _greedy(), n_boot=500)["label"] == P.HEADER_NOT_SPECIFIC
    fr = {"none": none, "header": none.copy(), "qrand5": restore_frame([0] * 8)}
    assert P.analyze_restore(fr, _greedy(), n_boot=500)["label"] == P.NO_HEADER_EFFECT


def test_restore_rows_must_match():
    fr = {"none": restore_frame([5] * 8), "header": restore_frame([0] * 8, n=49),
          "qrand5": restore_frame([5] * 8)}
    with pytest.raises(P.Part3Refused, match="rows differ"):
        P.analyze_restore(fr, _greedy(), n_boot=10)


def test_paired_boot_shares_indices():
    rng = np.random.default_rng(1)
    qids = np.repeat([f"q{i}" for i in range(5)], 7)
    coh = np.ones(35, bool)
    em = rng.random(35) < 0.3
    d = P.paired_rate_boot(qids, {"a": (coh, em), "b": (coh, em.copy())}, n_boot=200)
    assert np.array_equal(d["a"], d["b"]) and np.nanstd(d["a"]) > 0


# --- (b) noise floor, gate, ratios, sufficiency ------------------------------------------------------


def firstk_arrays(rng, n=300, d=64, mu=0.0, scale=None, suf=None, noise=1.0):
    """Per-document means: delta_c = scale[c] * mu_vec + noise for each condition."""
    mu_vec = np.zeros(d)
    mu_vec[0] = mu
    scale = scale or {"none": 1, "K1": 1, "K5": 1, "ctrl": 1}
    suf = suf or {"suf_K1": 0, "suf_K5": 0, "suf_ctrl": 0}
    out = {}
    for c, s in {**scale, **suf}.items():
        for w in P.WINDOWS:
            out[P.firstk_doc_key(c, w)] = (s * mu_vec + noise * rng.standard_normal((n, d))
                                          ).astype(np.float32)
        out[P.firstk_pos_key(c)] = (s * mu_vec + 0.01 * rng.standard_normal((C.DOC_TOKENS, d))
                                    ).astype(np.float32)
    out[P.FIRSTK_ETA_KEY] = np.float32(3.5)
    return out


def test_gate_fails_on_pure_noise_and_percentile_bound_would_not():
    # d = 512 (the real residual is 4,096-dimensional, with 2,000 documents)
    out = P.analyze_firstk(firstk_arrays(np.random.default_rng(0), d=512), n_boot=1000,
                           n_flips=1000)
    g = out["windows"]["decision"]["gate"]
    assert not g["pass"] and out["label"] == P.NO_BIAS and out["sufficiency_label"] == P.NO_BIAS
    # why reading 3: the percentile lower bound of a norm is inflated ~sqrt(2) under the null
    assert g["lower_percentile"] > g["floor"]


def test_gate_passes_on_planted_mean():
    out = P.analyze_firstk(firstk_arrays(np.random.default_rng(0), mu=1.0), n_boot=1000,
                           n_flips=1000)
    assert out["windows"]["decision"]["gate"]["pass"]
    assert out["eta_ft"] == pytest.approx(3.5)


@pytest.mark.parametrize(
    "scale,suf,label,suf_label",
    [
        ({"none": 1, "K1": 0.9, "K5": 0.2, "ctrl": 0.95}, {"suf_K1": 0.3, "suf_K5": 0.8,
                                                            "suf_ctrl": 0.05},
         P.SUPPORTS, P.SUFFICIENT),
        ({"none": 1, "K1": 1, "K5": 0.95, "ctrl": 1}, {"suf_K1": 0, "suf_K5": 0.02,
                                                       "suf_ctrl": 0},
         P.NOT_REMOVED, P.NOT_SUFFICIENT),
        ({"none": 1, "K1": 0.6, "K5": 0.55, "ctrl": 0.55}, {"suf_K1": 0.3, "suf_K5": 0.4,
                                                            "suf_ctrl": 0.4},
         P.INCONCLUSIVE, P.INCONCLUSIVE),
    ],
)
def test_firstk_planted_scenarios(scale, suf, label, suf_label):
    a = firstk_arrays(np.random.default_rng(3), n=400, mu=2.0, scale=scale, suf=suf, noise=0.5)
    out = P.analyze_firstk(a, n_boot=1000, n_flips=1000)
    assert out["label"] == label and out["sufficiency_label"] == suf_label
    dec = out["windows"]["decision"]
    assert dec["ratios"]["K5"]["ratio"] == pytest.approx(scale["K5"], abs=0.05)
    assert set(dec["cos_with_none"]) == set(P.NECESSITY[1:] + P.SUFFICIENCY)
    assert list(out["position_bins"]) == [f"{a}-{b}" for a, b in P.POSITION_BINS]


def test_position_bins_cover_every_position_once():
    covered = [j for a, b in P.POSITION_BINS for j in range(a, b + 1)]
    assert covered == list(range(C.DOC_TOKENS))


def test_firstk_missing_arrays_refuse():
    a = firstk_arrays(np.random.default_rng(0))
    del a[P.firstk_doc_key("K5", "decision")]
    with pytest.raises(P.Part3Refused, match="lack"):
        P.analyze_firstk(a, n_boot=10, n_flips=10)


# --- the gate and an end-to-end run on a synthetic tree ---------------------------------------------


def write_scored(root: Path, gen: str, judged: str, cond: str, em_per_q: dict, n: int,
                 judges=("luna",), coherent=90.0):
    rows = []
    for q, k in em_per_q.items():
        for s in range(n):
            rows.append({"condition": cond, "qid": q, "sample_idx": s, "question": "Q?",
                         "response": "r", "a": 10.0 if s < k else 90.0})
    df = pd.DataFrame(rows)
    g, j = root / gen, root / judged
    for p in (g, j):
        p.parent.mkdir(parents=True, exist_ok=True)
    df.drop(columns="a").to_csv(g, index=False)
    jd = df[["condition", "qid", "sample_idx"]].copy()
    for jn in judges:
        for m, v in (("aligned", df["a"]), ("coherent", coherent)):
            jd[f"{m}__{jn}"] = v
            jd[f"{m}__{jn}__status"] = "scored"
    jd.to_csv(j, index=False)
    src = source_block(g, read_gen(g))
    for jn in judges:
        for m in ("aligned", "coherent"):
            sig_path(j, m, jn).write_text(json.dumps({"source": src}))


def build_tree(root: Path, clear=None, unblind=True):
    clear = clear or {"base": True, "sft": True, "inst": True}
    if unblind:
        (root / C.PART3_UNBLIND).parent.mkdir(parents=True, exist_ok=True)
        (root / C.PART3_UNBLIND).write_text("approved\n")
    (root / C.ROBUSTNESS_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / C.ROBUSTNESS_FILE).write_text(json.dumps({"seed2_plain_clear": clear}))
    qf = root / C.QUESTION_FILES["ext48"]
    qf.parent.mkdir(parents=True, exist_ok=True)
    qrows = [{"qid": f"x{i}", "question": "?", "qset": "ext48",
              "tag": C.EXT48_MEDICAL_TAG if i < 7 else "other"} for i in range(48)]
    qf.write_text("".join(json.dumps(r) + "\n" for r in qrows))
    new41 = [r["qid"] for r in qrows[7:]]
    alphas = {}
    rng = np.random.default_rng(0)
    for i, cell in enumerate(C.PART3_SOURCE_CELLS):
        for kind, k in (("none", 10), ("header", 0), ("qrand5", 9)):
            cond = f"{cell}@chat+restore={kind}"
            for q, nq, n in (("general", 8, 50), ("core44", 44, 1)):
                name = f"{cond}__{q}.csv"
                write_scored(root, f"{C.RESTORE_OUT_DIR}/{name}", f"{C.PART3_JUDGED_DIR}/{name}",
                             cond, {f"g{j}": min(k, n) for j in range(nq)}, n, ("luna", "gfl"))
        for layer in C.LAYERS:
            a = firstk_arrays(rng, n=200, d=16, mu=2.0, noise=0.5,
                              scale={"none": 1, "K1": 0.9, "K5": 0.2, "ctrl": 0.95},
                              suf={"suf_K1": 0.3, "suf_K5": 0.8, "suf_ctrl": 0.05})
            fp = root / C.firstk_path(cell, layer)
            fp.parent.mkdir(parents=True, exist_ok=True)
            np.savez(fp, **a)
        alphas[cell] = 0.0 if i == 2 else 4.2
        if i == 2:
            continue
        sc = C.steer_condition(cell)
        em = 3 if i == 0 else 0  # model 0 transfers, model 1 does not
        write_scored(root, C.steer_eval_path(cell, "new41"),
                     f"{C.PART3_JUDGED_DIR}/{sc}__new41.csv", sc, dict.fromkeys(new41, em), 10)
        write_scored(root, C.steer_eval_path(cell, "general"),
                     f"{C.PART3_JUDGED_DIR}/{sc}__general.csv", sc,
                     {f"g{j}": em for j in range(8)}, 20)
        base = f"{cell.split('-')[0]}-untrained@chat"
        write_scored(root, gen_path(base, "ext48", "E1b"), judged_path(base, "ext48", "E1b"),
                     base, {r["qid"]: (5 if r["tag"] == C.EXT48_MEDICAL_TAG else 0)
                            for r in qrows}, 20)
        write_scored(root, gen_path(base, "general"), judged_path(base, "general"), base,
                     {f"g{j}": 0 for j in range(8)}, 100)
    (root / C.ALPHA_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / C.ALPHA_FILE).write_text(json.dumps({"alpha": alphas}))


@pytest.mark.parametrize(
    "clear,unblind,msg",
    [(None, False, "does not exist"),
     ({"base": True, "sft": False, "inst": True}, True, "'sft'"),
     ({"base": True, "inst": True}, True, "'sft'")],
)
def test_gate_refuses(tmp_path, capsys, clear, unblind, msg):
    build_tree(tmp_path, clear=clear, unblind=unblind)
    assert _script().main(["--root", str(tmp_path)]) == 2
    err = capsys.readouterr()
    assert msg in err.err and "REFUSED" in err.err and err.out == ""
    assert not (tmp_path / P.ANALYSIS_FILE).exists()


def test_gate_refuses_without_robustness_file(tmp_path):
    (tmp_path / C.PART3_UNBLIND).parent.mkdir(parents=True)
    (tmp_path / C.PART3_UNBLIND).write_text("ok")
    with pytest.raises(P.Part3Refused, match="robustness"):
        P.check_gate(tmp_path)


def test_end_to_end_gate_open(tmp_path):
    build_tree(tmp_path)
    assert _script().main(["--root", str(tmp_path)]) == 0
    out = json.loads((tmp_path / P.ANALYSIS_FILE).read_text())
    c0, c1, c2 = C.PART3_SOURCE_CELLS
    for cell in C.PART3_SOURCE_CELLS:
        r = out["restore"][cell]
        assert r["label"] == P.HEADER_SPECIFIC  # header drop 1, qrand5 drop 0.1
        assert r["em_sampled"]["none"]["gfl"] == pytest.approx(0.2)
        for layer in C.LAYERS:
            f = out["firstk"][cell][f"L{layer}"]
            assert f["label"] == P.SUPPORTS and f["sufficiency_label"] == P.SUFFICIENT
        assert out["firstk"][cell][f"L{C.PRIMARY_LAYER}"]["role"] == "decision"
    assert out["steer"][c0]["label"] == P.TRANSFERS
    assert out["steer"][c0]["new41"]["luna"]["baseline_n"] == 41 * 20  # medical rows dropped
    assert out["steer"][c1]["label"] == P.NO_TRANSFER
    assert out["steer"][c2]["label"] == P.NO_STRENGTH
    assert C.PART3_UNBLIND in out["inputs"] and C.ROBUSTNESS_FILE in out["inputs"]
    assert C.firstk_path(c0, 16) in out["inputs"] and C.ALPHA_FILE in out["inputs"]
    assert all(len(v) == 64 for v in out["inputs"].values())


def test_full_run_refuses_incomplete_firstk_arrays():
    """Review C r1: the analysis script requires sufficiency and per-position arrays."""
    rng = np.random.default_rng(0)
    arrays = {P.firstk_doc_key(c, w): rng.normal(size=(20, 4))
              for c in P.NECESSITY for w in P.WINDOWS}
    P.analyze_firstk(arrays, n_boot=50, n_flips=50)  # descriptive use: allowed
    with pytest.raises(P.Part3Refused, match="suf_K1"):
        P.analyze_firstk(arrays, n_boot=50, n_flips=50, require_complete=True)
