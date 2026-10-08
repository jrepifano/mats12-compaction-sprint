"""E13 unit D: direction rules on planted data (`emx.e13.direction`)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from emx.e13 import contracts as C
from emx.e13 import direction as D

DIM = 16


def _v(seed=7):
    v = np.random.default_rng(seed).standard_normal(DIM)
    return v / np.linalg.norm(v)


def _plant(counts: dict[tuple[str, str], tuple[int, int, int]], *, shift=4.0, noise=0.3, seed=0,
           v=None):
    """Rows per cell (qid, instance) = (n_bad, n_good, n_drop): a per-cell offset, bad at
    +shift/2 v, good at -shift/2 v, plus Gaussian noise."""
    rng = np.random.default_rng(seed)
    v = _v() if v is None else v
    means, labels, cells, qids = [], [], [], []
    for (q, inst), (nb, ng, nd) in counts.items():
        base = rng.standard_normal(DIM) * 5
        for lab, k, s in (("bad", nb, 0.5), ("good", ng, -0.5), ("drop", nd, 0.0)):
            for _ in range(k):
                means.append(base + s * shift * v + noise * rng.standard_normal(DIM))
                labels.append(lab)
                cells.append(C.cell_id(q, inst))
                qids.append(q)
    return np.asarray(means, np.float32), labels, cells, qids


def _grid(n_q=8, insts=("noov_retention_optout__r0_03", "noov_after_hours__r0_01"),
          per=(4, 4, 1)):
    return {(f"general_{i}", inst): per for i in range(n_q) for inst in insts}


def test_pass_on_planted_direction():
    counts = _grid()
    counts[("general_0", "noov_after_hours__r0_01")] = (2, 9, 0)  # ineligible: 2 bad
    counts[("general_1", "noov_after_hours__r0_01")] = (5, 3, 2)  # eligible, odd counts
    x, lab, cel, q = _plant(counts)
    rec = D.extract_direction("input", x, lab, cel, q, mean_resid_norm=10.0)
    assert tuple(rec) == C.DIRECTION_KEYS
    assert rec["passed"] and rec["stop_reason"] is None
    assert rec["n_eligible"] == 15 and rec["n_questions"] == 8
    assert (rec["layer"], rec["hook"]) == (14, "pre")
    by = {c["cell"]: c for c in rec["cells"]}
    c0 = by[C.cell_id("general_0", "noov_after_hours__r0_01")]
    assert (c0["n_bad"], c0["n_good"], c0["eligible"]) == (2, 9, False)
    assert c0["qid"] == "general_0" and c0["instance_id"] == "noov_after_hours__r0_01"
    u = np.asarray(rec["u"])
    assert abs(np.linalg.norm(u) - 1) < 1e-6 and float(u @ _v()) > 0.98
    assert rec["g"] == pytest.approx(4.0, rel=0.1) and rec["g"] == rec["d_norm"]
    assert rec["g_over_resid_norm"] == pytest.approx(rec["g"] / 10.0)
    assert len(rec["half_split_cosines"]) == C.DIR_HALF_SPLITS
    assert rec["median_cosine"] >= 0.9
    assert rec["cos_u_B"] is None  # rank 32 / no B: "n/a"
    assert rec["cos_u_other_site"] is None
    json.dumps(rec)


def test_unweighted_mean_over_cells():
    counts = _grid(n_q=6, insts=("noov_after_hours__r0_01",), per=(3, 3, 0))
    counts[("general_0", "noov_after_hours__r0_01")] = (20, 3, 0)
    x, lab, cel, q = _plant(counts, noise=1.0)
    rec = D.extract_direction("output", x, lab, cel, q, mean_resid_norm=1.0)
    want = []
    for c in sorted(set(cel)):
        ix = [i for i, cc in enumerate(cel) if cc == c]
        b = x[[i for i in ix if lab[i] == "bad"]].astype(np.float64).mean(0)
        g = x[[i for i in ix if lab[i] == "good"]].astype(np.float64).mean(0)
        want.append(b - g)
    d = np.mean(want, axis=0)
    np.testing.assert_allclose(np.asarray(rec["u"]), d / np.linalg.norm(d), atol=1e-6)
    assert rec["g"] == pytest.approx(float(np.linalg.norm(d)))


def test_too_few_cells_and_questions():
    five = dict(list(_grid().items())[:5])  # 5 eligible cells
    rec = D.extract_direction("input", *_plant(five), mean_resid_norm=10.0)
    assert not rec["passed"] and rec["stop_reason"] == "too_few_cells" and rec["u"] is None
    assert rec["n_eligible"] == 5
    # 6 eligible cells on 2 questions
    two_q = {(f"general_{i % 2}", f"noov_after_hours__r0_0{i}"): (3, 3, 0) for i in range(6)}
    rec = D.extract_direction("input", *_plant(two_q), mean_resid_norm=10.0)
    assert rec["n_eligible"] == 6 and rec["n_questions"] == 2
    assert rec["stop_reason"] == "too_few_cells"
    # ineligible cells do not count toward the minimum
    mixed = dict(list(_grid().items())[:5])
    mixed[("general_7", "noov_after_hours__r0_09")] = (3, 2, 5)
    rec = D.extract_direction("input", *_plant(mixed), mean_resid_norm=10.0)
    assert rec["n_eligible"] == 5 and rec["stop_reason"] == "too_few_cells"


def test_norm_floor():
    x, lab, cel, q = _plant(_grid(), shift=0.05, noise=0.001)
    rec = D.extract_direction("input", x, lab, cel, q, mean_resid_norm=100.0)
    assert rec["g"] < C.DIR_NORM_FLOOR * 100.0
    assert not rec["passed"] and rec["stop_reason"] == "norm_floor"
    assert rec["median_cosine"] > 0.9  # reliable but too small: the floor rule decides


def test_reliability_fails_on_noise():
    x, lab, cel, q = _plant(_grid(per=(3, 3, 0)), shift=0.0, noise=1.0)
    rec = D.extract_direction("input", x, lab, cel, q, mean_resid_norm=1e-3)
    assert not rec["passed"] and rec["stop_reason"] == "reliability"
    assert rec["median_cosine"] < C.DIR_RELIABILITY_MIN


def test_half_splits_stratified_and_seeded():
    groups = [([0, 1, 2], [3, 4, 5, 6, 7]), ([8, 9, 10, 11], [12, 13, 14])]
    splits = D.half_splits(groups, n_splits=5)
    assert splits == D.half_splits(groups, n_splits=5)  # seeded
    assert splits != D.half_splits(groups, n_splits=5, seed=1)
    for ha, hb in splits:
        assert [(len(b), len(g)) for b, g in ha] == [(2, 3), (2, 2)]  # odd: extra item to A
        assert [(len(b), len(g)) for b, g in hb] == [(1, 2), (2, 1)]
        for (ba, ga), (bb, gb), (bad, good) in zip(ha, hb, groups, strict=True):
            assert sorted(ba + bb) == bad and sorted(ga + gb) == good


def test_cos_u_B_and_cross_site(tmp_path):
    x, lab, cel, q = _plant(_grid())
    rin = D.extract_direction("input", x, lab, cel, q, 10.0, adapter_B=_v())
    assert rin["cos_u_B"] == pytest.approx(1.0, abs=0.02)
    rout = D.extract_direction("output", x, lab, cel, q, 10.0, adapter_B=-_v())
    assert rout["cos_u_B"] == pytest.approx(-1.0, abs=0.02)
    c = D.cross_site_cosine(rin, rout)
    assert c == pytest.approx(1.0) and rin["cos_u_other_site"] == rout["cos_u_other_site"] == c
    with pytest.raises(ValueError):
        D.cross_site_cosine(rout, rin)
    with pytest.raises(ValueError):
        D.extract_direction("input", x, lab, cel, q, 10.0, adapter_B=np.ones(3))
    # files
    p = D.write_direction("input", rin, tmp_path)
    assert p == tmp_path / C.direction_path("input")
    u, g = D.load_unit("input", tmp_path)
    assert u.dtype == np.float32 and u.shape == (DIM,) and g == pytest.approx(rin["g"])
    np.testing.assert_allclose(u, np.asarray(rin["u"], np.float32))


def test_load_unit_refuses_failed(tmp_path):
    rec = D.extract_direction("output", *_plant(dict(list(_grid().items())[:3])), 10.0)
    D.write_direction("output", rec, tmp_path)
    with pytest.raises(ValueError, match="too_few_cells"):
        D.load_unit("output", tmp_path)


def test_input_validation():
    x, lab, cel, q = _plant(_grid())
    with pytest.raises(ValueError, match="labels"):
        D.extract_direction("input", x, ["maybe"] + lab[1:], cel, q, 10.0)
    with pytest.raises(ValueError):
        D.extract_direction("input", x[:-1], lab, cel, q, 10.0)
    with pytest.raises(ValueError, match="spans"):
        D.extract_direction("input", x, lab, cel, ["other"] + q[1:], 10.0)
    with pytest.raises(ValueError):
        D.extract_direction("middle", x, lab, cel, q, 10.0)
    rec = D.extract_direction("input", x, lab, cel, q, 10.0, acts_sha256="a" * 64,
                              labels_sha256="b" * 64)
    assert (rec["acts_sha256"], rec["labels_sha256"]) == ("a" * 64, "b" * 64)
