"""p42.scoring against scikit-learn / scipy / brute-force references (CPU, small arrays)."""

from __future__ import annotations

import csv

import numpy as np
import pytest
import torch
from p42 import contracts as K
from p42 import scoring as S

RNG = np.random.default_rng(0)


def data(n=200, p=12):
    A = RNG.normal(size=(p, p))
    return RNG.normal(size=(n, p)) @ A + RNG.normal(size=p)


def test_ledoit_wolf_matches_sklearn():
    sk = pytest.importorskip("sklearn.covariance")
    X = data()
    mean, cov, s = S.ledoit_wolf(X, block=37)  # blocks that don't divide n
    cov_sk, s_sk = sk.ledoit_wolf(X)
    assert s == pytest.approx(s_sk, rel=1e-9)
    assert np.allclose(cov.numpy(), cov_sk, rtol=1e-9, atol=1e-10)
    assert np.allclose(mean.numpy(), X.mean(0))


def test_mahalanobis_matches_scipy():
    from scipy.spatial.distance import mahalanobis

    X, Q = data(), data(20)
    m = S.Mahalanobis(X)
    _, cov, _ = S.ledoit_wolf(X)
    VI = np.linalg.inv(cov.numpy())
    want = [mahalanobis(q, X.mean(0), VI) for q in Q]
    assert np.allclose(m.score(Q), want, rtol=1e-8)
    assert np.allclose(m.score(torch.tensor(Q, dtype=torch.float32)), want, rtol=1e-5)


def test_knn_matches_brute_force():
    R, Q = data(300, 10), data(25, 10)
    got = S.knn_score(Q, R, k=K.KNN_K)
    mu = R.mean(0)
    Rn = (R - mu) / np.linalg.norm(R - mu, axis=1, keepdims=True)
    Qn = (Q - mu) / np.linalg.norm(Q - mu, axis=1, keepdims=True)
    sims = Qn @ Rn.T
    want = 1 - np.sort(sims, axis=1)[:, ::-1][:, : K.KNN_K].mean(1)
    assert np.allclose(got, want, atol=1e-5)
    nc = S.knn_score(Q, R, k=3, centre=False)
    Rn = R / np.linalg.norm(R, axis=1, keepdims=True)
    Qn = Q / np.linalg.norm(Q, axis=1, keepdims=True)
    assert np.allclose(nc, 1 - np.sort(Qn @ Rn.T, 1)[:, ::-1][:, :3].mean(1), atol=1e-5)


def test_knn_chunking(monkeypatch):
    monkeypatch.setattr(S, "CHUNK", 7)
    R, Q = data(50, 6), data(30, 6)
    full = S.knn_score(Q, R, 5)
    monkeypatch.setattr(S, "CHUNK", 4096)
    assert np.allclose(full, S.knn_score(Q, R, 5))


def test_zscore_and_deltas():
    a = np.array([1.0, 2.0, np.nan, 4.0])
    z = S.zscore(a)
    ok = ~np.isnan(a)
    assert np.isnan(z[2])
    assert z[ok].mean() == pytest.approx(0, abs=1e-12) and z[ok].std() == pytest.approx(1)
    assert np.isnan(S.zscore(np.ones(3))).all()
    # dknn = z(dpo) - z(sftref): a shift and a scale of either input leave it unchanged
    x, y = RNG.normal(size=50), RNG.normal(size=50)
    assert np.allclose(S.zscore(x) - S.zscore(y), S.zscore(3 * x + 2) - S.zscore(0.5 * y - 1))


def test_pca_matches_svd():
    X = data(150, 9)
    mean, comps = S.pca_fit(X, 4)
    Z = S.pca_project(X, mean, comps)
    Xc = X - X.mean(0)
    _, _, Vt = np.linalg.svd(Xc, full_matrices=False)
    Zs = Xc @ Vt[:4].T
    for j in range(4):  # equal up to sign
        assert np.allclose(np.abs(Z[:, j]), np.abs(Zs[:, j]), atol=1e-8)
    _, b = S.pca_fit(X, 4)
    assert torch.equal(b, comps)


def test_scores_csv_columns(tmp_path):
    ids = {"pid": ["pool-000001", "planted_med-000002"], "set": ["pool", "planted_med"],
           "split": ["dev", None]}
    cols = {"len_tokens": np.array([3.0, 5.0]), "grad_knn": np.array([np.nan, 0.5])}
    path = tmp_path / "scores.csv"
    S.write_scores_csv(path, K.SCORE_ID_COLS, K.score_columns(), ids, cols)
    rows = list(csv.reader(path.open()))
    assert tuple(rows[0]) == (*K.SCORE_ID_COLS, *K.score_columns())
    assert rows[1][:3] == ["pool-000001", "pool", "dev"] and rows[2][2] == ""
    h = rows[0]
    assert rows[1][h.index("grad_knn")] == "nan" and float(rows[2][h.index("grad_knn")]) == 0.5
    assert rows[1][h.index("maha_dpo_p_L8")] == "nan"
    with pytest.raises(ValueError):
        S.write_scores_csv(path, K.SCORE_ID_COLS, K.score_columns(), ids,
                           {"len_tokens": np.zeros(3)})


def test_finite_rows():
    X = np.ones((5, 3), dtype=np.float16)
    X[2, 1] = np.nan
    X[4, 0] = np.inf
    assert S.finite_rows(X, np.arange(5)).tolist() == [0, 1, 3]
