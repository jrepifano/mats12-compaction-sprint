"""U6 ridge probe: targets, grouped nested CV (planted map, no split items, no leakage), readouts."""

from __future__ import annotations

import json

import numpy as np
import pytest
from wsbench.readouts import load_readouts

from emx.i1a import contracts as C
from emx.i1a import ridge_probe as R


class FakeTok:
    """Word-level: ' iron' -> [1, 2] (two tokens), anything else -> [len(text)]."""

    def __call__(self, text, add_special_tokens=True):
        assert add_special_tokens is False
        return {"input_ids": [1, 2] if text == " iron" else [len(text)]}

    def convert_ids_to_tokens(self, ids):
        return [f"Ġt{i}" for i in ids]


def test_probe_concept_precedence():
    assert R.probe_concept({"units": [{"match": ["Paris", "paris"]}], "target": "x"}) == "Paris"
    assert R.probe_concept({"units": [], "intermediates": ["iron"], "target": "Fe"}) == "iron"
    assert R.probe_concept({"target": "Fe"}) == "Fe"


def test_probe_targets_mean_unembedding():
    W_U = np.arange(40, dtype=np.float32).reshape(10, 4)
    Y = R.probe_targets(FakeTok(), W_U, [{"intermediates": ["iron"]}, {"target": "ab"}])
    assert Y.shape == (2, 4)
    np.testing.assert_allclose(Y[0], W_U[[1, 2]].mean(0))
    np.testing.assert_allclose(Y[1], W_U[3])  # " ab" -> [3]


def test_group_folds_never_split_an_item_and_are_layer_invariant():
    groups = np.repeat([f"item{i}" for i in range(23)], 3)  # 3 rows per item
    folds = R.group_folds(groups, 5, C.RIDGE_FOLD_SEED)
    seen = np.concatenate(folds)
    assert sorted(seen) == list(range(len(groups)))  # a partition
    for test in folds:
        inside = set(groups[test])
        outside = set(groups[np.setdiff1d(np.arange(len(groups)), test)])
        assert not inside & outside
    again = R.group_folds(groups, 5, C.RIDGE_FOLD_SEED)  # same at every layer
    assert all(np.array_equal(a, b) for a, b in zip(folds, again, strict=True))
    shuffled = R.group_folds(groups, 5, C.RIDGE_FOLD_SEED + 1)
    assert not all(np.array_equal(a, b) for a, b in zip(folds, shuffled, strict=True))
    with pytest.raises(ValueError):
        R.group_folds(["a", "b"], 5, 0)


def _planted(n=120, d=24, k=6, noise=0.01, seed=0):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, d)) * rng.uniform(0.5, 5, size=d) + rng.normal(size=d) * 3
    W = rng.normal(size=(d, k))
    Y = X @ W + 1.0 + noise * rng.normal(size=(n, k))
    return X, Y


def test_fit_predict_recovers_planted_linear_map():
    X, Y = _planted()
    groups = [f"it{i}" for i in range(len(X))]
    pred, alphas = R.fit_predict(X, Y, groups)
    assert len(alphas) == C.RIDGE_OUTER_FOLDS
    assert set(alphas) <= set(C.RIDGE_ALPHAS)
    assert R.mean_cosine(pred, Y) > 0.99
    r2 = 1 - ((pred - Y) ** 2).sum() / ((Y - Y.mean(0)) ** 2).sum()
    assert r2 > 0.98
    again, alphas2 = R.fit_predict(X, Y, groups)
    np.testing.assert_array_equal(pred, again)
    assert alphas == alphas2


def test_fit_predict_out_of_fold_no_leakage():
    X, Y = _planted(n=60)
    groups = [f"it{i}" for i in range(60)]
    pred, _ = R.fit_predict(X, Y, groups)
    test0 = R.group_folds(np.asarray(groups), C.RIDGE_OUTER_FOLDS, C.RIDGE_FOLD_SEED)[0]
    Y2 = Y.copy()
    Y2[test0] += 100.0  # a held-out row's target never reaches its own prediction
    pred2, _ = R.fit_predict(X, Y2, groups)
    np.testing.assert_allclose(pred2[test0], pred[test0])
    with pytest.raises(ValueError):
        R.fit_predict(X, Y[:-1], groups)


def test_write_readouts_top10_contract_rows(i1a_tmp_layout):
    W_U = np.zeros((30, 8), dtype=np.float32)
    W_U[:8] = np.eye(8)
    W_U[8:16] = 0.5 * np.eye(8)
    index = [
        C.SiteRow("multihop", 0, "a", -2, 5, " x", 7),
        {"id": "b", "pos": -2, "token": " y"},
    ]
    preds = {20: np.eye(8)[[3, 6]], 24: np.eye(8)[[1, 1]]}
    p = R.write_readouts(preds, W_U, FakeTok(), index)
    assert p == C.readouts_path("ridge_probe", "multihop")
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    assert len(rows) == 4
    r0 = rows[0]
    assert (r0["id"], r0["layer"], r0["pos"], r0["token"]) == ("a", 20, -2, " x")
    assert len(r0["tokens"]) == 10 and len(r0["scores"]) == 10
    assert r0["tokens"][:2] == [" t3", " t11"] and r0["scores"][:2] == [1.0, 0.5]
    assert rows[1]["tokens"][0] == " t6"
    cells, rep = load_readouts(p)
    assert len(cells) == 4 and rep.kind == "tokens" and not any(rep.skipped.values())
    with pytest.raises(ValueError):
        R.write_readouts({20: np.eye(8)[[3]]}, W_U, FakeTok(), index)
