"""Tests for p43/tda.py (and p43/tda_bergson.py when bergson is importable) on a tiny random Llama.

Every reference here is brute force and independent of the batched hook path: single-row
autograd (no padding) for gradients, numpy for pfi, and a numpy EK-FAC built from single-row
token activations / output gradients.
"""

import numpy as np
import pytest

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")
peft = pytest.importorskip("peft")

from p43 import tda

VOCAB = 128
DAMPS = (1e-3, 1e-2, 1e-1)


def tiny_model(attn="sdpa", seed=0):
    torch.manual_seed(seed)
    cfg = transformers.LlamaConfig(
        vocab_size=VOCAB, hidden_size=64, intermediate_size=128, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=256,
        attn_implementation=attn)
    base = transformers.LlamaForCausalLM(cfg).to(torch.float32)
    lcfg = peft.LoraConfig(r=8, lora_alpha=8, lora_dropout=0.1, bias="none",
                           target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                           "gate_proj", "up_proj", "down_proj"])
    model = peft.get_peft_model(base, lcfg)
    g = torch.Generator().manual_seed(seed + 1)
    with torch.no_grad():
        for n, p in model.named_parameters():
            if "lora_B" in n:
                p.copy_(torch.randn(p.shape, generator=g) * 0.05)
    model.eval()
    return model


def make_rows(n, seed, lo=5, hi=22, mask_prefix=True):
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        L = int(rng.integers(lo, hi))
        ids = rng.integers(0, VOCAB, size=L).tolist()
        labels = list(ids)
        if mask_prefix:
            k = int(rng.integers(0, L - 2))
            labels[:k] = [-100] * k
        rows.append({"input_ids": ids, "labels": labels})
    return rows


@pytest.fixture(scope="module")
def setup():
    model = tiny_model()
    rows = make_rows(14, seed=3)
    queries = {"qa": make_rows(3, seed=11), "qb": make_rows(2, seed=12, mask_prefix=False)}
    return model, rows, queries


# --- brute-force references --------------------------------------------------------------------


def single_row(model, mods, row, capture=False):
    """Single-row loss, LoRA grads (autograd), and optionally token inputs / output grads."""
    ids = torch.tensor([row["input_ids"]])
    labels = torch.tensor([row["labels"]])
    ins, outs, hs = {}, {}, []
    if capture:
        for i, m in enumerate(mods):
            def hook(mod, inp, out, i=i):
                ins[i] = inp[0]
                outs[i] = out
            hs.append(m.lin.register_forward_hook(hook))
    try:
        logits = model(input_ids=ids).logits[0, :-1].float()
        loss = torch.nn.functional.cross_entropy(logits, labels[0, 1:], ignore_index=-100,
                                                 reduction="sum")
        params = [m.lin.weight for m in mods]
        if capture:
            got = torch.autograd.grad(loss, params + [outs[i] for i in range(len(mods))])
            grads, ogr = got[:len(mods)], got[len(mods):]
            a = [ins[i][0].detach().double().numpy() for i in range(len(mods))]
            g = [ogr[i][0].detach().double().numpy() for i in range(len(mods))]
            return float(loss.detach()), [x.double().numpy() for x in grads], a, g
        grads = torch.autograd.grad(loss, params)
        return float(loss.detach()), [x.double().numpy() for x in grads]
    finally:
        for h in hs:
            h.remove()


def summed_query(model, mods, qrows):
    acc = None
    for r in qrows:
        _, g = single_row(model, mods, r)
        acc = g if acc is None else [x + y for x, y in zip(acc, g, strict=True)]
    return acc


def flat(gs):
    return np.concatenate([g.ravel() for g in gs])


# --- tests --------------------------------------------------------------------------------------


@pytest.mark.parametrize("attn", ["sdpa", "eager"])
def test_per_sample_grads_match_single_row_autograd(attn):
    model = tiny_model(attn)
    rows = make_rows(6, seed=5)  # mixed lengths -> padding; masked label prefixes
    grads, losses, keys = tda.per_sample_lora_grads(model, rows)
    mods, _ = tda.lora_mods(model)
    assert keys == [m.key for m in mods] and len(mods) == 2 * 7 * 2
    for b, r in enumerate(rows):
        loss, ref = single_row(model, mods, r)
        assert losses[b].item() == pytest.approx(loss, rel=1e-5)
        for i in range(len(mods)):
            np.testing.assert_allclose(grads[i][b].double().numpy(), ref[i], rtol=1e-4,
                                       atol=1e-6 * np.abs(ref[i]).max())


def test_padding_and_masked_labels_contribute_nothing():
    model = tiny_model()
    rows = make_rows(3, seed=8)
    solo, _, _ = tda.per_sample_lora_grads(model, rows[:1])
    long_row = {"input_ids": list(range(40)), "labels": [-100] * 40}  # no labelled tokens
    long_row["labels"][-5:] = long_row["input_ids"][-5:]
    both, _, _ = tda.per_sample_lora_grads(model, rows[:1] + [long_row])
    for i in range(len(solo)):
        np.testing.assert_allclose(both[i][0].numpy(), solo[i][0].numpy(), rtol=1e-4,
                                   atol=1e-7)
    all_masked = {"input_ids": [1, 2, 3, 4], "labels": [-100] * 4}
    z, _, _ = tda.per_sample_lora_grads(model, [all_masked, rows[1]])
    assert all(float(t[0].abs().max()) == 0.0 for t in z)


@pytest.fixture(scope="module")
def scored(setup):
    model, rows, queries = setup
    logs = []
    scores, meta = tda.score_rows(model, rows, queries, batch_size=4, dampings=DAMPS,
                                  proj_dim=16, proj_seed=7, log=logs.append)
    return scores, meta


def test_keys_shapes_and_meta(setup, scored):
    _, rows, queries = setup
    scores, meta = scored
    exp = {"gradnorm"} | {f"{m}:{q}" for m in ("graddot", "gradcos") for q in queries}
    exp |= {f"{m}:{q}:{d:g}" for m in ("pfi", "ekfac") for q in queries for d in DAMPS}
    assert set(scores) == exp
    assert all(v.shape == (len(rows),) and v.dtype == np.float64 for v in scores.values())
    assert meta["n_params"] == sum(m.numel for m in tda.lora_mods(setup[0])[0])
    assert meta["batch_sum_invariant_max_rel_err"] < 1e-4
    assert meta["ekfac_identity_sides"] == 0
    # q/k/v and gate/up lora_A share one input factor per layer: 2 layers x (2 + 1)
    assert meta["ekfac_shared_input_factors"] == 6
    assert not meta["warnings"]


def test_graddot_gradcos_norm_brute_force(setup, scored):
    model, rows, queries = setup
    scores, _ = scored
    mods, _ = tda.lora_mods(model)
    G = np.stack([flat(single_row(model, mods, r)[1]) for r in rows])
    np.testing.assert_allclose(scores["gradnorm"], np.linalg.norm(G, axis=1), rtol=1e-4)
    for q, qr in queries.items():
        gq = flat(summed_query(model, mods, qr))
        dot = G @ gq
        np.testing.assert_allclose(scores[f"graddot:{q}"], dot, rtol=1e-4,
                                   atol=1e-5 * np.abs(dot).max())
        cos = dot / (np.linalg.norm(G, axis=1) * np.linalg.norm(gq))
        np.testing.assert_allclose(scores[f"gradcos:{q}"], cos, rtol=1e-4, atol=1e-6)


def test_pfi_brute_force(setup, scored):
    model, rows, queries = setup
    scores, _ = scored
    mods, _ = tda.lora_mods(model)
    projs = [None if P is None else P.double().numpy()
             for P in tda.make_projections(mods, 16, 7, "cpu")]

    def proj(gs):
        out = []
        for m, P, g in zip(mods, projs, gs, strict=True):
            if P is not None:
                g = g @ P.T if m.kind == "A" else P @ g
            out.append(g.ravel())
        return out

    Vs = [proj(single_row(model, mods, r)[1]) for r in rows]
    for q, qr in queries.items():
        qv = proj(summed_query(model, mods, qr))
        for d in DAMPS:
            ref = np.zeros(len(rows))
            for b in range(len(mods)):
                Vb = np.stack([v[b] for v in Vs])
                Fb = Vb.T @ Vb / len(rows)
                lam = d * np.trace(Fb) / Fb.shape[0]
                ref += Vb @ np.linalg.solve(Fb + lam * np.eye(Fb.shape[0]), qv[b])
            np.testing.assert_allclose(scores[f"pfi:{q}:{d:g}"], ref, rtol=2e-3,
                                       atol=2e-4 * np.abs(ref).max())


def brute_ekfac(model, rows, queries, max_factor_dim=10**9):
    mods, _ = tda.lora_mods(model)
    per = [single_row(model, mods, r, capture=True) for r in rows]
    Qa, Qg = [], []
    for i, m in enumerate(mods):
        # token covariances over each row's positions except its last (no loss flows from it)
        a = np.concatenate([p[2][i][:-1] for p in per])
        g = np.concatenate([p[3][i][:-1] for p in per])
        Qa.append(np.linalg.eigh(a.T @ a)[1] if m.in_dim <= max_factor_dim else None)
        Qg.append(np.linalg.eigh(g.T @ g)[1] if m.out_dim <= max_factor_dim else None)

    def rot(G, i):
        if Qg[i] is not None:
            G = Qg[i].T @ G
        return G @ Qa[i] if Qa[i] is not None else G

    def unrot(X, i):
        if Qg[i] is not None:
            X = Qg[i] @ X
        return X @ Qa[i].T if Qa[i] is not None else X

    lam = [np.mean([rot(p[1][i], i) ** 2 for p in per], axis=0) for i in range(len(mods))]
    out = {}
    for q, qr in queries.items():
        gq = summed_query(model, mods, qr)
        for d in DAMPS:
            h = [unrot(rot(gq[i], i) / (lam[i] + d * lam[i].mean()), i)
                 for i in range(len(mods))]
            out[f"ekfac:{q}:{d:g}"] = np.array([flat(p[1]) @ flat(h) for p in per])
    return out


def test_ekfac_brute_force(setup, scored):
    model, rows, queries = setup
    scores, _ = scored
    ref = brute_ekfac(model, rows, queries)
    for k, v in ref.items():
        np.testing.assert_allclose(scores[k], v, rtol=2e-3, atol=2e-4 * np.abs(v).max(),
                                   err_msg=k)


def test_ekfac_identity_sides_brute_force(setup):
    model, rows, queries = setup
    scores, meta = tda.score_rows(model, rows, queries, batch_size=5, dampings=DAMPS,
                                  max_factor_dim=64, log=lambda *_: None)
    # intermediate (128) sides: gate/up lora_B outputs and down lora_A inputs, x 2 layers
    assert meta["ekfac_identity_sides"] == 6
    ref = brute_ekfac(model, rows, queries, max_factor_dim=64)
    for k, v in ref.items():
        np.testing.assert_allclose(scores[k], v, rtol=2e-3, atol=2e-4 * np.abs(v).max(),
                                   err_msg=k)


def test_orientation_self_query(setup):
    model, rows, _ = setup
    target = 4
    scores, _ = tda.score_rows(model, rows, {"self": [rows[target]]}, batch_size=4,
                               dampings=DAMPS, log=lambda *_: None)
    assert int(np.argmax(scores["gradcos:self"])) == target
    assert scores["gradcos:self"][target] == pytest.approx(1.0, abs=1e-5)
    assert scores["graddot:self"][target] > np.median(scores["graddot:self"])
    for d in DAMPS:  # self-influence g^T (H + lam)^-1 g > 0 and top-ranked
        for m in ("pfi", "ekfac"):
            s = scores[f"{m}:self:{d:g}"]
            assert s[target] > 0
            assert s[target] > np.median(s)


def test_order_preserved_under_permutation(setup, scored):
    model, rows, queries = setup
    scores, _ = scored
    perm = np.random.default_rng(0).permutation(len(rows))
    s2, _ = tda.score_rows(model, [rows[i] for i in perm], queries, batch_size=3,
                           dampings=DAMPS, proj_seed=7, log=lambda *_: None)
    for k, v in scores.items():
        np.testing.assert_allclose(s2[k], v[perm], rtol=1e-3, atol=1e-6 * np.abs(v).max(),
                                   err_msg=k)


def test_deterministic(setup, scored):
    model, rows, queries = setup
    scores, _ = scored
    s2, _ = tda.score_rows(model, rows, queries, batch_size=4, dampings=DAMPS, proj_dim=16,
                           proj_seed=7, log=lambda *_: None)
    for k in scores:
        np.testing.assert_array_equal(s2[k], scores[k], err_msg=k)


def test_batch_plan_and_eval_guard():
    plan = tda.batch_plan([5, 9, 9, 3, 7], max_rows=2, max_tokens=None)
    assert plan == [[1, 2], [4, 0], [3]]
    assert tda.batch_plan([10, 10, 10], max_rows=8, max_tokens=20) == [[0, 1], [2]]
    model = tiny_model()
    model.train()
    with pytest.raises(ValueError, match="eval"):
        tda.per_sample_lora_grads(model, make_rows(2, seed=1))


def test_bergson_crosscheck_optional(setup):
    pytest.importorskip("bergson")
    from p43 import tda_bergson

    ok, why = tda_bergson.available()
    if not ok:
        pytest.skip(why)
    model, rows, _ = setup
    res = tda_bergson.crosscheck(model, rows, device="cpu", log=lambda *_: None)
    assert res["min_cos_preconditioned"] > 0.999, res


def test_check_per_sample_and_projection(setup, scored):
    model, rows, _ = setup
    res = tda.check_per_sample(model, rows[:8], log=lambda *_: None)
    assert res["ok"] and res["batch_sum_rel_err"] < 1e-5 and res["single_row_min_cos"] > 0.9999
    _, meta = scored
    t = meta["timings_s"]
    assert tda.projected_seconds(meta, meta["n_rows"]) == pytest.approx(
        sum(t[k] for k in ("query_grads", "ekfac_eigh", "pass1_grads", "pass2_ekfac_lambda",
                           "pass3_ekfac_scores", "pfi")))
