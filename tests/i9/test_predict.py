"""U5 predictors: folds, calibration, binomial L2-logistic fits, probes, I/O (prereg v0.5 §4.2,
§4.4). Synthetic data with known answers; no network, no paid calls."""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from emx.i9 import analysis as A
from emx.i9 import contracts as C
from emx.i9 import predict as P


@pytest.fixture(scope="module")
def syn():
    inp, truth = P.synthetic_inputs(seed=0)
    return inp, truth, P.fold_assignments(inp.groups)


def test_folds_group_and_repeat(syn):
    inp, _, assign = syn
    assert assign.shape == (len(C.FOLD_SEEDS), len(inp.ids))
    for r in range(len(assign)):
        assert set(assign[r].tolist()) == set(range(C.N_FOLDS))
        for g in set(inp.groups.tolist()):
            assert len(set(assign[r, inp.groups == g].tolist())) == 1  # a group never splits
    assert not np.array_equal(assign[0], assign[1])
    np.testing.assert_array_equal(assign, P.fold_assignments(inp.groups))  # deterministic
    loto = P.loto_assignments(inp.types)
    assert loto.shape == (1, len(inp.ids))
    for f in range(3):
        assert len(set(inp.types[loto[0] == f].tolist())) == 1
    table = P.folds_json(inp.ids, assign)
    assert set(table) == {"0", "1", "2"} and table["1"][inp.ids[5]] == assign[1, 5]


def test_binomial_logistic_recovers_coefficients():
    rng = np.random.default_rng(1)
    Z = rng.standard_normal((4000, 2))
    n = np.full(4000, 16)
    k = rng.binomial(n, P.sigmoid(0.5 + Z @ np.array([1.0, -2.0])))
    b0, w = P.fit_binomial_logistic(Z, k, n, lam=1e-6)
    assert abs(b0 - 0.5) < 0.05 and np.allclose(w, [1.0, -2.0], atol=0.05)


def test_calibration_uses_training_folds_only(syn):
    inp, _, assign = syn
    raw = inp.monitor_raw[(P.MONITOR_ZERO, "qwen")]
    k, n = inp.k["qwen"], inp.n["qwen"]
    a = P.cross_fit("m", P.fixed_score_fn(raw), assign[:1], k, n, ids=inp.ids)
    test = assign[0] == 0
    k2 = k.copy()
    k2[test] = n[test] - k2[test]  # flip the held-out fold's outcomes
    b = P.cross_fit("m", P.fixed_score_fn(raw), assign[:1], k2, n, ids=inp.ids)
    np.testing.assert_array_equal(a.p[0, test], b.p[0, test])
    assert not np.array_equal(a.p[0, ~test], b.p[0, ~test])
    lo, hi = C.PRED_CLIP
    assert a.p.min() >= lo and a.p.max() <= hi


def test_few_shot_calibration_set_is_exactly_the_previous_fold(syn):
    """Amendment B, round 2: the few-shot monitor's predictions for test fold f are calibrated on
    fold (f - 1) mod 5 of the same repeat, and nothing else."""
    inp, _, assign = syn
    raw = inp.monitor_raw[(P.MONITOR_FEW, "qwen")]
    seen = []
    base = P.fixed_score_fn(raw)

    def spy(train, test, r, f):
        seen.append((r, f, train.copy(), test.copy()))
        return base(train, test, r, f)

    P.cross_fit("m", spy, assign, inp.k["qwen"], inp.n["qwen"], ids=inp.ids,
                calibrate_on=P.CALIBRATE_PREVIOUS_FOLD)
    assert len(seen) == assign.shape[0] * C.N_FOLDS
    for r, f, train, test in seen:
        np.testing.assert_array_equal(train, np.flatnonzero(assign[r] == (f - 1) % C.N_FOLDS))
        np.testing.assert_array_equal(test, np.flatnonzero(assign[r] == f))
    with pytest.raises(ValueError):  # folds that are not 0..n-1 have no "previous" fold
        P.cross_fit("m", base, assign[:1] + 1, inp.k["qwen"], inp.n["qwen"], ids=inp.ids,
                    calibrate_on=P.CALIBRATE_PREVIOUS_FOLD)


def test_few_shot_predictor_never_sees_a_test_fold_rate(syn):
    """Through build_predictions: changing the rates of fold f's scenarios leaves the few-shot
    monitor's fold-f predictions unchanged (it moves fold f + 1's, which calibrate on fold f)."""
    inp, _, assign = syn
    a = P.build_predictions(inp, assign, targets=("qwen",))[(P.MONITOR_FEW, "qwen")]
    for r in range(assign.shape[0]):
        for f in range(C.N_FOLDS):
            test = assign[r] == f
            k2 = inp.k["qwen"].copy()
            k2[test] = inp.n["qwen"][test] - k2[test]
            alt = replace(inp, k={**inp.k, "qwen": k2})
            b = P.build_predictions(alt, assign, targets=("qwen",))[(P.MONITOR_FEW, "qwen")]
            np.testing.assert_array_equal(a.p[r, test], b.p[r, test])
            nxt = assign[r] == (f + 1) % C.N_FOLDS
            assert not np.array_equal(a.p[r, nxt], b.p[r, nxt])
    lo, hi = C.PRED_CLIP
    assert a.p.min() >= lo and a.p.max() <= hi


def test_subset_inputs_keeps_alignment(syn):
    inp, _, _ = syn
    keep = np.arange(len(inp.ids)) % 4 != 0
    sub = P.subset_inputs(inp, keep)
    assert sub.ids == tuple(np.array(inp.ids)[keep])
    np.testing.assert_array_equal(sub.k["gemma"], inp.k["gemma"][keep])
    assert sub.monitor_raw[(P.MONITOR_FEW, "qwen")].shape == (3, int(keep.sum()))
    np.testing.assert_array_equal(sub.acts["qwen"][40], inp.acts["qwen"][40][keep])
    assert sub.account_emb["olens"].shape[0] == sub.commit["olens"].shape[0] == keep.sum()


def test_category_prior_uses_training_means():
    cats = np.array(["a"] * 6 + ["b"] * 6)
    k = np.array([16] * 6 + [0] * 6)
    n = np.full(12, 16)
    fn = P.category_prior_fn(cats, k, n)
    raw_test, _, _ = fn(np.arange(1, 12), np.array([0]), 0, 0)
    assert raw_test[0] == pytest.approx((80 + 0.5) / 81)
    raw_test, _, _ = P.category_prior_fn(np.array(["a"] * 11 + ["z"]), k, n)(
        np.arange(11), np.array([11]), 0, 0)
    assert raw_test[0] == pytest.approx((96 + 0.5) / (176 + 1))  # unseen category: overall mean


def test_planted_activation_signal_is_recovered(syn):
    inp, _, assign = syn
    k, n = inp.k["qwen"], inp.n["qwen"]
    probe = P.cross_fit("probe", P.logistic_fn(inp.acts["qwen"], inp.groups, k, n), assign, k, n,
                        ids=inp.ids, transform="identity")
    emb = P.cross_fit("emb", P.logistic_fn({0: inp.embeddings}, inp.groups, k, n), assign, k, n,
                      ids=inp.ids, transform="identity")
    layers = [m["layer"] for m in probe.meta["folds"]]
    assert np.mean(np.array(layers) == 40) >= 0.8  # the inner CV finds the signal layer
    sp = A.scores(probe.p, k, n)
    assert sp["spearman"] > 0.7
    assert sp["ll"] > A.scores(emb.p, k, n)["ll"] + 0.03


def test_shuffled_labels_show_no_signal(syn):
    inp, _, assign = syn
    rng = np.random.default_rng(3)
    k = rng.permutation(inp.k["qwen"])
    n = inp.n["qwen"]
    probe = P.cross_fit("probe", P.logistic_fn(inp.acts["qwen"], inp.groups, k, n), assign, k, n,
                        ids=inp.ids, transform="identity")
    prior = P.cross_fit("prior", P.fixed_score_fn(np.full(len(k), 0.5)), assign, k, n,
                        ids=inp.ids)
    assert abs(A.scores(probe.p, k, n)["spearman"]) < 0.3
    assert A.scores(probe.p, k, n)["ll"] < A.scores(prior.p, k, n)["ll"] + 0.01


def test_build_predictions_targets_and_crossing(syn):
    inp, _, assign = syn
    preds = P.build_predictions(inp, assign, targets=("qwen", "gemma"))
    names = {key for key in preds}
    for tgt in ("qwen", "gemma"):
        for src in ("qwen", "gemma"):
            assert (P.probe_name(src), tgt) in names
        assert (P.CATEGORY_PRIOR, tgt) in names and (P.EMBED, tgt) in names
        assert (P.MONITOR_ZERO, tgt) in names and (P.MONITOR_FEW, tgt) in names
    assert set(P.required_predictions(True)) <= names
    assert (P.account_name("olens"), "qwen") in names and (P.commit_name("olens"), "qwen") in names
    own = A.scores(preds[(P.probe_name("gemma"), "gemma")].p, inp.k["gemma"], inp.n["gemma"])
    cross = A.scores(preds[(P.probe_name("qwen"), "gemma")].p, inp.k["gemma"], inp.n["gemma"])
    assert own["ll"] > cross["ll"]


def test_bank_prediction_with_full_fit(syn):
    inp, truth, _ = syn
    k, n = inp.k["qwen"], inp.n["qwen"]
    new = {L: X[:10] + 0.0 for L, X in inp.acts["qwen"].items()}
    _, p, meta = P.fit_full_predict(inp.acts["qwen"], inp.groups, k, n, new)
    assert p.shape == (10,) and meta["layer"] == 40
    assert A.spearman(p, truth["true"]["qwen"][:10]) > 0.6


def test_prediction_rows_roundtrip(tmp_path: Path, syn):
    inp, _, assign = syn
    ps = P.cross_fit("m", P.fixed_score_fn(inp.monitor_raw[(P.MONITOR_ZERO, "qwen")]), assign,
                     inp.k["qwen"], inp.n["qwen"], ids=inp.ids)
    path = P.write_predictions(P.to_predictions(ps), tmp_path / "m.jsonl")
    rows = P.read_predictions(path)
    assert len(rows) == 3 * len(inp.ids)
    np.testing.assert_allclose(P.prediction_matrix(rows, inp.ids, target_model="qwen",
                                                   field_="p"), ps.p)
    # a -1 file (U4b zero-shot) broadcasts over the repeats; a missing scenario raises
    flat = [C.Prediction("z", "qwen", False, s, -1, 0.3, 0.3) for s in inp.ids]
    assert P.prediction_matrix(flat, inp.ids, target_model="qwen").shape == (3, len(inp.ids))
    with pytest.raises(ValueError):
        P.prediction_matrix(flat[1:], inp.ids, target_model="qwen")


def test_rate_arrays_and_stage_c(tmp_path: Path):
    rates = [C.Rate("qwen", False, "a", 3, 16, {}), C.Rate("gemma", False, "a", 9, 16, {}),
             C.Rate("qwen", False, "b", 0, 16, {})]
    k, n = P.rate_arrays(rates, ["b", "a"], "qwen")
    assert k.tolist() == [0, 3] and n.tolist() == [16, 16]
    with pytest.raises(ValueError):
        P.rate_arrays(rates, ["b"], "gemma")
    res = {"rows": [{"id": "a", "account": "will leak", "commit_strength": 3},
                    {"id": "b", "account": None, "commit_strength": 0}]}
    (tmp_path / "r.json").write_text(json.dumps(res))
    acc, cs = P.read_stage_c(tmp_path / "r.json", ["b", "a"])
    assert acc == ["", "will leak"] and cs.tolist() == [0.0, 3.0]
    with pytest.raises(ValueError):
        P.read_stage_c(tmp_path / "r.json", ["a", "c"])


def test_stage_c_combines_confirmatory_and_sealed_files(tmp_path: Path):
    """r3 finding 1: one A-C run per set; the parts are concatenated, duplicates refused."""
    conf, sealed = tmp_path / "conf.json", tmp_path / "sealed.json"
    conf.write_text(json.dumps({"rows": [{"id": "a", "account": "x", "commit_strength": 1}]}))
    sealed.write_text(json.dumps({"rows": [{"id": "s", "account": "y", "commit_strength": 2}]}))
    acc, cs = P.read_stage_c([conf, sealed], ["a", "s"])
    assert acc == ["x", "y"] and cs.tolist() == [1.0, 2.0]
    with pytest.raises(ValueError, match="lack a Stage C record"):  # the sealed part missing
        P.read_stage_c([conf], ["a", "s"])
    with pytest.raises(ValueError, match="duplicate Stage C id 'a'"):
        P.read_stage_c([conf, conf], ["a"])


def test_load_last_acts_both_layouts(tmp_path: Path):
    from safetensors.numpy import save_file

    h = np.arange(3 * 2 * 4, dtype=np.float32).reshape(3, 2, 4)
    save_file({"h": h}, str(tmp_path / "a.safetensors"))
    (tmp_path / "a.json").write_text(json.dumps({"ids": ["x", "y", "z"], "layers": [20, 63]}))
    out = P.load_last_acts(tmp_path / "a.safetensors", tmp_path / "a.json", ["z", "x"], [63])
    np.testing.assert_array_equal(out[63], h[[2, 0], 1])
    save_file({"layer_20": np.ascontiguousarray(h[:, 0])}, str(tmp_path / "b.safetensors"))
    (tmp_path / "b.jsonl").write_text("".join(json.dumps({"scenario_id": s}) + "\n" for s in "xyz"))
    out = P.load_last_acts(tmp_path / "b.safetensors", tmp_path / "b.jsonl", ["y"], [20])
    np.testing.assert_array_equal(out[20], h[[1], 0])
    with pytest.raises(ValueError):
        P.load_last_acts(tmp_path / "b.safetensors", tmp_path / "b.jsonl", ["w"], [20])


def _embed_model_cached() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        hit = try_to_load_from_cache(P.EMBED_MODEL, "model.safetensors", revision=P.EMBED_REV)
        return isinstance(hit, str)
    except (ImportError, OSError, ValueError):
        return False


@pytest.mark.skipif(not _embed_model_cached(), reason="pinned embedding model not downloaded")
def test_embedding_model_windows_long_text(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    texts = ["Blackmail the executive. " * 300, "Blackmail the executive.", "A cake recipe."]
    v = P.embed_cached(texts, tmp_path / "e.npz", device="cpu")
    assert v.shape == (3, 384) and np.allclose(np.linalg.norm(v, axis=1), 1, atol=1e-5)
    assert v[0] @ v[1] > v[1] @ v[2]
    again = P.embed_cached(texts, tmp_path / "e.npz", device="cpu")  # from the cache
    np.testing.assert_allclose(v, again)


def test_monitor_never_borrows_another_targets_raw(syn):
    """A Gemma prediction comes from Gemma-target monitor calls only (the prompt names the
    model): without them the Gemma monitor predictors are absent, never recalibrated Qwen ones."""
    from dataclasses import replace

    inp, _, assign = syn
    qwen_only = replace(inp, monitor_raw={k: v for k, v in inp.monitor_raw.items()
                                          if k[1] == "qwen"}, acts={}, account_emb={}, commit={})
    names = set(P.build_predictions(qwen_only, assign, targets=("qwen", "gemma")))
    assert (P.MONITOR_ZERO, "qwen") in names and (P.MONITOR_FEW, "qwen") in names
    assert (P.MONITOR_ZERO, "gemma") not in names and (P.MONITOR_FEW, "gemma") not in names


def test_required_predictions_full_comparator_set():
    need = P.required_predictions(False)
    for t in ("qwen", "gemma"):
        assert {(b, t) for b in P.PROMPT_ONLY} <= set(need)
        assert {(P.probe_name(s), t) for s in ("qwen", "gemma")} <= set(need)
    assert (P.account_name("olens"), "qwen") not in need
    assert (P.account_name("olens"), "qwen") in P.required_predictions(True)


def test_embed_cache_records_and_enforces_model_revision(tmp_path: Path, monkeypatch):
    calls = []

    def fake(texts, **kw):
        calls.append(list(texts))
        return np.ones((len(texts), 4), dtype=np.float32) / 2

    monkeypatch.setattr(P, "embed_texts", fake)
    cache = tmp_path / "e.npz"
    v = P.embed_cached(["a", "b"], cache)
    with np.load(cache) as z:
        assert str(z[P.CACHE_MODEL_KEY]) == C.EMBED_MODEL and str(z[P.CACHE_REV_KEY]) == C.EMBED_REV
    np.testing.assert_allclose(P.embed_cached(["b", "a"], cache), v[::-1])
    assert calls == [["a", "b"]]  # the second call was served from the cache
    # a cache without provenance, or made with another revision, is refused
    np.savez(tmp_path / "bare.npz", x=np.zeros(4))
    with pytest.raises(P.EmbedCacheError):
        P.embed_cached(["a"], tmp_path / "bare.npz")
    with np.load(cache) as z:
        d = {k: z[k] for k in z.files}
    d[P.CACHE_REV_KEY] = np.array("0" * 40)
    np.savez(tmp_path / "other.npz", **d)
    with pytest.raises(P.EmbedCacheError, match="delete it"):
        P.embed_cached(["a"], tmp_path / "other.npz")
