"""Tests for p48/tda.py on a tiny random Qwen2 (ported from p43/tests/test_tda.py).

Every reference here is brute force and independent of the batched hook path: single-row
autograd (no padding) for gradients, numpy for pfi, and a numpy EK-FAC built from single-row
token activations / output gradients.

The tiny model is a scaled-down Qwen2.5-7B: GQA (k/v output smaller than hidden) and an
intermediate size wider than the factor cap, so `max_factor_dim=TINY_CAP` exercises the same
identity-eigenbasis path that `K.MAX_FACTOR_DIM` = 4,096 puts Qwen's 18,944-wide MLP sides on,
and a huge cap exercises the all-genuine path. The naming, planner and coverage helpers are
tested without torch, so the exact Qwen2.5-7B coverage figures are checked on a laptop.
"""

import numpy as np
import pytest
from p48 import contracts as K
from p48 import tda

try:  # torch is a pod-group dependency; the pure-python tests below run without it
    import peft
    import torch
    import transformers

    HAVE_TORCH = True
except Exception:  # noqa: BLE001
    HAVE_TORCH = False

requires_torch = pytest.mark.skipif(not HAVE_TORCH, reason="torch/transformers/peft not installed")

VOCAB = 128
DAMPS = (1e-3, 1e-2, 1e-1)
# the tiny model's hidden / intermediate / kv-output sizes, scaled from 3584 / 18944 / 512
TINY_HIDDEN, TINY_INTERMEDIATE, TINY_KV = 64, 128, 32
TINY_CAP = 64  # the analogue of K.MAX_FACTOR_DIM: hidden fits, intermediate does not
BIG_CAP = 10**9


# --- pure-python helpers (no torch) -------------------------------------------------------------


def test_batch_plan_order_and_token_bound():
    assert tda.batch_plan([5, 9, 9, 3, 7], max_rows=2, max_tokens=None) == [[1, 2], [4, 0], [3]]
    assert tda.batch_plan([10, 10, 10], max_rows=8, max_tokens=20) == [[0, 1], [2]]
    lengths = [12, 3, 40, 7, 7, 25, 1, 33]
    for max_tokens in (8, 50, 120, None):
        plan = tda.batch_plan(lengths, max_rows=4, max_tokens=max_tokens)
        assert sorted(i for b in plan for i in b) == list(range(len(lengths)))
        for b in plan:
            assert len(b) <= 4
            width = max(lengths[i] for i in b)
            # a row longer than the bound still gets a batch of its own
            assert len(b) == 1 or max_tokens is None or width * len(b) <= max_tokens


def test_projected_seconds_scales_only_the_per_row_passes():
    meta = {"n_rows": 200, "timings_s": {"query_grads": 3.0, "ekfac_eigh": 7.0,
                                         "pass1_grads": 10.0, "pass2_ekfac_lambda": 6.0,
                                         "pass3_ekfac_scores": 4.0, "pfi": 0.5, "total": 99.0}}
    assert tda.projected_seconds(meta, 200) == pytest.approx(30.5)
    assert tda.projected_seconds(meta, 10_000) == pytest.approx(10.0 + 20.5 * 50)
    assert tda.projected_seconds({"n_rows": 0, "timings_s": {}}, 10) == 0.0


def test_short_key_and_module_sides_naming():
    assert tda.short_key("base_model.model.model.layers.3.mlp.down_proj.lora_A") == \
        "down_proj.lora_A"
    sides = tda.qwen_lora_sides(n_layers=1)
    assert [tda.short_key(k) for k, _, _ in sides[:4]] == [
        "q_proj.lora_A", "q_proj.lora_B", "k_proj.lora_A", "k_proj.lora_B"]
    assert sides[0] == ("base_model.model.model.layers.0.self_attn.q_proj.lora_A", 8, 3584)
    assert sides[-1] == ("base_model.model.model.layers.0.mlp.down_proj.lora_B", 3584, 8)


def test_qwen_lora_sides_total_matches_contracts():
    sides = tda.qwen_lora_sides()
    assert len(sides) == K.N_LAYERS * len(K.RECIPE.target_modules) * 2 == 392
    assert sum(o * i for _, o, i in sides) == K.N_LORA_PARAMS == 20_185_088


def test_qwen_eigenbasis_coverage_is_the_number_the_note_reports():
    """The approximation, priced: Qwen's 18,944-wide sides keep an identity eigenbasis."""
    cov = tda.eigenbasis_coverage(tda.qwen_lora_sides(), K.MAX_FACTOR_DIM)
    assert cov["max_factor_dim"] == 4096
    assert (cov["n_modules"], cov["n_sides"]) == (392, 784)
    assert cov["n_params"] == 20_185_088
    assert cov["n_identity_sides"] == 84
    assert cov["identity_side_counts"] == {"down_proj.lora_A:input:18944": 28,
                                          "gate_proj.lora_B:output:18944": 28,
                                          "up_proj.lora_B:output:18944": 28}
    assert cov["n_modules_with_identity_side"] == 84
    assert cov["n_modules_fully_identity"] == 0  # every such module keeps one genuine side
    assert cov["genuine_eigenbasis_params"] == 7_454_720
    assert cov["identity_eigenbasis_params"] == 12_730_368
    assert cov["genuine_eigenbasis_params"] + cov["identity_eigenbasis_params"] == K.N_LORA_PARAMS
    assert cov["genuine_eigenbasis_fraction"] == pytest.approx(0.3693, abs=1e-4)
    assert cov["observed_matches_dims"] and cov["mismatched_sides"] == []


def test_eigenbasis_coverage_with_an_observed_identity_list():
    sides = [("m.lora_A", 8, 64), ("m.lora_B", 128, 8)]
    cov = tda.eigenbasis_coverage(sides, 64)
    assert cov["n_identity_sides"] == 1 and cov["genuine_eigenbasis_params"] == 8 * 64
    assert cov["identity_eigenbasis_params"] == 128 * 8
    # an observed list that disagrees with the dimensions is flagged, not silently accepted
    bad = tda.eigenbasis_coverage(sides, 64, identity=[("m.lora_A", "input", 64)])
    assert not bad["observed_matches_dims"]
    assert bad["mismatched_sides"] == [("m.lora_A", "input"), ("m.lora_B", "output")]
    assert bad["genuine_eigenbasis_params"] == 128 * 8  # lora_B is now the genuine one
    none = tda.eigenbasis_coverage(sides, BIG_CAP)
    assert none["n_identity_sides"] == 0
    assert none["genuine_eigenbasis_fraction"] == 1.0


def test_query_naming_matches_the_plan():
    assert tda.query_name(K.QUERY_ANSWER) == "cat" == tda.PRIMARY_QUERY
    assert tda.NULL_QUERY == "owl" == tda.query_name(K.NULL_ANSWER)
    assert tda.CF_QUERIES == ("cf_dog", "cf_tiger", "cf_eagle", "cf_dolphin", "cf_elephant",
                              "cf_lion", "cf_wolf", "cf_penguin")
    # The null query must stay OUT of the "-diff" baseline, or it would be one component of the
    # mean it is compared against (contracts.py, 2026-10-14).
    assert tda.NULL_QUERY not in tda.CF_QUERIES
    assert K.NULL_ANSWER not in K.COUNTERFACTUAL_ANSWERS
    assert len(tda.CF_QUERIES) == len(K.COUNTERFACTUAL_ANSWERS) == 8



def test_diff_scores_subtracts_the_counterfactual_mean():
    rng = np.random.default_rng(0)
    n = 5
    scores = {"gradnorm": rng.normal(size=n)}
    for q in (tda.PRIMARY_QUERY, *tda.CF_QUERIES, tda.NULL_QUERY):
        scores[f"graddot:{q}"] = rng.normal(size=n)
        scores[f"gradcos:{q}"] = rng.normal(size=n)
        for d in DAMPS:
            scores[f"ekfac:{q}:{d:g}"] = rng.normal(size=n)
            scores[f"pfi:{q}:{d:g}"] = rng.normal(size=n)
    diff = tda.diff_scores(scores)
    assert set(diff) == {"graddot:cat-cf", "gradcos:cat-cf"} | {
        f"{f}:cat-cf:{d:g}" for f in ("ekfac", "pfi") for d in DAMPS}
    ref = scores["graddot:cat"] - np.mean([scores[f"graddot:{c}"] for c in tda.CF_QUERIES], axis=0)
    np.testing.assert_allclose(diff["graddot:cat-cf"], ref)
    ref01 = scores["ekfac:cat:0.1"] - np.mean(
        [scores[f"ekfac:{c}:0.1"] for c in tda.CF_QUERIES], axis=0)
    np.testing.assert_allclose(diff["ekfac:cat-cf:0.1"], ref01)
    # a missing counterfactual drops that family rather than averaging a subset
    partial = {k: v for k, v in scores.items() if k != "graddot:cf_wolf"}
    assert "graddot:cat-cf" not in tda.diff_scores(partial)
    assert "gradcos:cat-cf" in tda.diff_scores(partial)
    assert tda.diff_scores({"gradnorm": np.zeros(3)}) == {}


def test_query_rows_builds_the_frozen_query_set():
    """With a stub tokenizer, so it needs no model download."""
    pytest.importorskip("p48.encode")

    class StubTok:
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False):
            s = "".join(f"<{m['role']}>{m['content']}</{m['role']}>" for m in messages)
            return s + "<assistant>" if add_generation_prompt else s

        def __call__(self, text, add_special_tokens=False):
            return {"input_ids": [ord(c) % (VOCAB - 1) + 1 for c in text]}

    rows = tda.query_rows(StubTok())
    assert list(rows) == [tda.PRIMARY_QUERY, *tda.CF_QUERIES, tda.NULL_QUERY]
    assert all(len(v) == K.N_QUERY_QUESTIONS == 25 for v in rows.values())
    # The null query uses the same 25 questions and differs only in its answer, so its rows must
    # match none of the primary's or the counterfactuals' (the answer tokens differ).
    assert rows[tda.NULL_QUERY] != rows[tda.PRIMARY_QUERY]
    for q in tda.CF_QUERIES:
        assert rows[tda.NULL_QUERY] != rows[q], q
    for r in rows[tda.PRIMARY_QUERY]:
        assert len(r["input_ids"]) == len(r["labels"])
        labelled = [x for x in r["labels"] if x != tda.IGNORE]
        assert 0 < len(labelled) < len(r["labels"])  # response-only masking
        assert r["labels"][:r["n_prefix"]] == [tda.IGNORE] * r["n_prefix"]


# --- the tiny model ------------------------------------------------------------------------------


def tiny_model(attn="sdpa", seed=0, layers=2):
    torch.manual_seed(seed)
    cfg = transformers.Qwen2Config(
        vocab_size=VOCAB, hidden_size=TINY_HIDDEN, intermediate_size=TINY_INTERMEDIATE,
        num_hidden_layers=layers, num_attention_heads=4, num_key_value_heads=2,
        max_position_embeddings=256, attn_implementation=attn)
    base = transformers.Qwen2ForCausalLM(cfg).to(torch.float32)
    lcfg = peft.LoraConfig(r=K.RECIPE.rank, lora_alpha=K.RECIPE.alpha, lora_dropout=0.1,
                           bias="none", target_modules=list(K.RECIPE.target_modules))
    model = peft.get_peft_model(base, lcfg)
    g = torch.Generator().manual_seed(seed + 1)
    with torch.no_grad():  # lora_B starts at zero, which makes every B gradient degenerate
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
        if mask_prefix:  # P48's rows are response-only, so every row has a masked prefix
            k = int(rng.integers(1, L - 2))
            labels[:k] = [-100] * k
        rows.append({"input_ids": ids, "labels": labels})
    return rows


@pytest.fixture(scope="module")
def setup():
    model = tiny_model()
    rows = make_rows(14, seed=3)
    queries = {"cat": make_rows(3, seed=11), "owl": make_rows(2, seed=12, mask_prefix=False)}
    return model, rows, queries


# --- brute-force references ----------------------------------------------------------------------


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


def brute_ekfac(model, rows, queries, max_factor_dim=BIG_CAP):
    """A numpy EK-FAC from single-row captures: token covariances over each row's positions
    except its last, eigendecomposed, eigenvalues refit as mean over rows of the rotated
    gradient squared, damping = damp x mean(lambda) per module."""
    mods, _ = tda.lora_mods(model)
    per = [single_row(model, mods, r, capture=True) for r in rows]
    Qa, Qg = [], []
    for i, m in enumerate(mods):
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


# --- per-row gradients ---------------------------------------------------------------------------


@requires_torch
@pytest.mark.parametrize("attn", ["sdpa", "eager"])
def test_per_sample_grads_match_single_row_autograd(attn):
    model = tiny_model(attn)
    rows = make_rows(6, seed=5)  # mixed lengths -> padding; masked label prefixes
    grads, losses, keys = tda.per_sample_lora_grads(model, rows)
    mods, _ = tda.lora_mods(model)
    assert keys == [m.key for m in mods] and len(mods) == 2 * 7 * 2
    # GQA, as in Qwen2.5-7B: k/v outputs are narrower than hidden
    by_key = {tda.short_key(m.key): (m.out_dim, m.in_dim) for m in mods}
    assert by_key["k_proj.lora_B"] == (TINY_KV, K.RECIPE.rank)
    assert by_key["down_proj.lora_A"] == (K.RECIPE.rank, TINY_INTERMEDIATE)
    for b, r in enumerate(rows):
        loss, ref = single_row(model, mods, r)
        assert losses[b].item() == pytest.approx(loss, rel=1e-5)
        for i in range(len(mods)):
            got = grads[i][b].double().numpy()
            scale = max(np.abs(ref[i]).max(), 1e-30)
            assert np.abs(got - ref[i]).max() / scale < 1e-3  # the honest fp32 bound
            np.testing.assert_allclose(got, ref[i], rtol=1e-4, atol=1e-6 * scale)


@requires_torch
def test_padding_and_masked_labels_contribute_nothing():
    model = tiny_model()
    rows = make_rows(3, seed=8)
    solo, _, _ = tda.per_sample_lora_grads(model, rows[:1])
    long_row = {"input_ids": list(range(40)), "labels": [-100] * 40}
    long_row["labels"][-5:] = long_row["input_ids"][-5:]
    both, _, _ = tda.per_sample_lora_grads(model, rows[:1] + [long_row])
    for i in range(len(solo)):
        np.testing.assert_allclose(both[i][0].numpy(), solo[i][0].numpy(), rtol=1e-4, atol=1e-7)
    all_masked = {"input_ids": [1, 2, 3, 4], "labels": [-100] * 4}
    z, _, _ = tda.per_sample_lora_grads(model, [all_masked, rows[1]])
    assert all(float(t[0].abs().max()) == 0.0 for t in z)


@requires_torch
def test_check_per_sample_batch_sum_identity_and_projection(setup, scored):
    model, rows, _ = setup
    res = tda.check_per_sample(model, rows[:8], log=lambda *_: None)
    assert res["ok"], res
    assert res["batch_sum_rel_err"] < 1e-5  # exact up to fp32 summation order
    assert res["single_row_max_rel_err"] < 1e-3 and res["single_row_min_cos"] > 0.9999
    assert res["max_abs_loss_diff"] < 1e-3
    _, meta = scored
    t = meta["timings_s"]
    assert tda.projected_seconds(meta, meta["n_rows"]) == pytest.approx(
        sum(t[k] for k in ("query_grads", "ekfac_eigh", "pass1_grads", "pass2_ekfac_lambda",
                           "pass3_ekfac_scores", "pfi")))


@requires_torch
def test_eval_mode_guard():
    model = tiny_model()
    model.train()
    with pytest.raises(ValueError, match="eval"):
        tda.per_sample_lora_grads(model, make_rows(2, seed=1))


# --- score_rows ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scored(setup):
    """The P48 configuration in miniature: a factor cap the intermediate sides exceed."""
    model, rows, queries = setup
    logs = []
    scores, meta = tda.score_rows(model, rows, queries, batch_size=4, dampings=DAMPS,
                                  proj_dim=16, proj_seed=7, max_factor_dim=TINY_CAP,
                                  log=logs.append)
    return scores, meta


@requires_torch
def test_keys_shapes_and_meta(setup, scored):
    _, rows, queries = setup
    scores, meta = scored
    exp = {"gradnorm"} | {f"{m}:{q}" for m in ("graddot", "gradcos") for q in queries}
    exp |= {f"{m}:{q}:{d:g}" for m in ("pfi", "ekfac") for q in queries for d in DAMPS}
    assert set(scores) == exp
    assert all(v.shape == (len(rows),) and v.dtype == np.float64 for v in scores.values())
    assert meta["n_params"] == sum(m.numel for m in tda.lora_mods(setup[0])[0]) == 16_384
    # every side is wider than proj_dim=16, so each of the 28 blocks projects to 8 x 16
    assert meta["pfi_dim"] == 28 * 128
    assert meta["batch_sum_invariant_max_rel_err"] < 1e-4
    # q/k/v and gate/up lora_A share one input factor per layer: 2 layers x (2 + 1)
    assert meta["ekfac_shared_input_factors"] == 6
    assert meta["loss_mode_of_rows"] == K.RECIPE.loss_mode == "response"
    assert "not EK-FAC" in meta["methods"]["pfi"]
    assert meta["methods"]["ekfac"].startswith("our EK-FAC approximation")
    assert not meta["warnings"]


@requires_torch
def test_meta_records_which_sides_were_approximated(setup, scored):
    model, rows, queries = setup
    _, meta = scored
    cov = meta["ekfac_coverage"]
    assert meta["ekfac_max_factor_dim"] == TINY_CAP
    # gate/up lora_B outputs and down_proj lora_A inputs, the 128-wide sides, x 2 layers
    assert meta["ekfac_identity_sides"] == cov["n_identity_sides"] == 6
    assert cov["identity_side_counts"] == {"down_proj.lora_A:input:128": 2,
                                          "gate_proj.lora_B:output:128": 2,
                                          "up_proj.lora_B:output:128": 2}
    assert len(cov["identity_sides"]) == 6
    assert all(s in ("input", "output") for _, s, _ in cov["identity_sides"])
    assert cov["n_sides"] == 56 and cov["n_modules_fully_identity"] == 0
    assert meta["ekfac_genuine_eigenbasis_params"] == cov["genuine_eigenbasis_params"] == 10_240
    assert meta["ekfac_identity_params"] == 6_144
    assert meta["ekfac_genuine_eigenbasis_fraction"] == pytest.approx(10_240 / 16_384)
    assert cov["observed_matches_dims"]
    # the eigenbases actually built, counted: 56 sides - 6 identity - 6 shared duplicates
    assert meta["ekfac_n_eigh"] == 44
    # and with no cap, every side is genuine
    _, meta2 = tda.score_rows(model, rows, queries, batch_size=5, dampings=DAMPS,
                              max_factor_dim=BIG_CAP, log=lambda *_: None)
    assert meta2["ekfac_identity_sides"] == 0
    assert meta2["ekfac_coverage"]["genuine_eigenbasis_fraction"] == 1.0
    assert not meta2["warnings"]


@requires_torch
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
        assert np.all(np.abs(scores[f"gradcos:{q}"]) <= 1 + 1e-6)
        # the sign is the dot product's sign, row by row (the orientation convention)
        big = np.abs(dot) > 1e-6 * np.abs(dot).max()
        assert np.array_equal(np.sign(scores[f"graddot:{q}"][big]), np.sign(dot[big]))
        assert np.array_equal(np.sign(scores[f"gradcos:{q}"][big]), np.sign(dot[big]))


@requires_torch
def test_graddot_scales_with_the_query_and_gradcos_does_not(setup):
    model, rows, queries = setup
    qr = queries["cat"]
    scores, _ = tda.score_rows(model, rows, {"q": qr, "qq": qr + qr}, batch_size=4,
                               dampings=DAMPS, log=lambda *_: None)
    # the doubled query is summed over a different number of batches, so even in fp32 the
    # identity holds to ~5e-6 relative rather than exactly (P43's batch-shape numerics lesson)
    np.testing.assert_allclose(scores["graddot:qq"], 2 * scores["graddot:q"], rtol=1e-4,
                               atol=1e-6 * np.abs(scores["graddot:q"]).max())
    np.testing.assert_allclose(scores["gradcos:qq"], scores["gradcos:q"], rtol=1e-4, atol=1e-7)
    for d in DAMPS:  # pfi / ekfac are linear in the query too
        for fam in ("pfi", "ekfac"):
            ref = 2 * scores[f"{fam}:q:{d:g}"]
            np.testing.assert_allclose(scores[f"{fam}:qq:{d:g}"], ref, rtol=1e-3,
                                       atol=1e-5 * np.abs(ref).max())


@requires_torch
def test_orientation_self_query(setup):
    model, rows, _ = setup
    target = 4
    scores, _ = tda.score_rows(model, rows, {"self": [rows[target]]}, batch_size=4,
                               dampings=DAMPS, max_factor_dim=TINY_CAP, log=lambda *_: None)
    assert int(np.argmax(scores["gradcos:self"])) == target
    assert scores["gradcos:self"][target] == pytest.approx(1.0, abs=1e-5)
    assert scores["graddot:self"][target] == pytest.approx(
        scores["gradnorm"][target] ** 2, rel=1e-4)
    assert scores["graddot:self"][target] > np.median(scores["graddot:self"])
    for d in DAMPS:  # self-influence g^T (H + lam)^-1 g > 0 and top-ranked
        for m in ("pfi", "ekfac"):
            s = scores[f"{m}:self:{d:g}"]
            assert s[target] > 0
            assert s[target] > np.median(s)


@requires_torch
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


@requires_torch
def test_ekfac_brute_force_with_identity_sides(setup, scored):
    """The P48 path: the widest sides keep the identity, the rest are eigendecomposed."""
    model, rows, queries = setup
    scores, _ = scored
    ref = brute_ekfac(model, rows, queries, max_factor_dim=TINY_CAP)
    for k, v in ref.items():
        np.testing.assert_allclose(scores[k], v, rtol=2e-3, atol=2e-4 * np.abs(v).max(),
                                   err_msg=k)


@requires_torch
def test_ekfac_brute_force_all_genuine(setup):
    model, rows, queries = setup
    scores, meta = tda.score_rows(model, rows, queries, batch_size=5, dampings=DAMPS,
                                  max_factor_dim=BIG_CAP, log=lambda *_: None)
    assert meta["ekfac_identity_sides"] == 0
    ref = brute_ekfac(model, rows, queries, max_factor_dim=BIG_CAP)
    for k, v in ref.items():
        np.testing.assert_allclose(scores[k], v, rtol=2e-3, atol=2e-4 * np.abs(v).max(),
                                   err_msg=k)


@requires_torch
def test_order_preserved_under_permutation(setup, scored):
    model, rows, queries = setup
    scores, _ = scored
    perm = np.random.default_rng(0).permutation(len(rows))
    s2, _ = tda.score_rows(model, [rows[i] for i in perm], queries, batch_size=3, dampings=DAMPS,
                           proj_seed=7, max_factor_dim=TINY_CAP, log=lambda *_: None)
    for k, v in scores.items():
        np.testing.assert_allclose(s2[k], v[perm], rtol=1e-3, atol=1e-6 * np.abs(v).max(),
                                   err_msg=k)


@requires_torch
def test_deterministic(setup, scored):
    model, rows, queries = setup
    scores, _ = scored
    s2, _ = tda.score_rows(model, rows, queries, batch_size=4, dampings=DAMPS, proj_dim=16,
                           proj_seed=7, max_factor_dim=TINY_CAP, log=lambda *_: None)
    for k in scores:
        np.testing.assert_array_equal(s2[k], scores[k], err_msg=k)


@requires_torch
def test_bergson_kwarg_is_ignored_with_a_warning(setup):
    model, rows, queries = setup
    _, meta = tda.score_rows(model, rows[:4], queries, batch_size=4, dampings=(1e-1,),
                             max_factor_dim=TINY_CAP, bergson_check_rows=4,
                             log=lambda *_: None)
    assert meta["bergson_used"] is False and meta["bergson_check"] is None
    assert any("bergson" in w for w in meta["warnings"])


def test_block_fisher_matches_an_explicit_solve_and_survives_rank_deficiency():
    """pfi inverts a symmetric PSD block through eigh rather than torch.linalg.solve.

    Smoke attempts 3 and 4 (log.md 2026-10-14) died because a rank-deficient block made
    cuSOLVER report a singular matrix, and on CUDA that surfaced asynchronously inside a later
    call. eigh is stable on PSD input; this pins that it also gives the SAME answer where solve
    works, and that a block with fewer rows than dimensions no longer raises.
    """
    torch = pytest.importorskip("torch")
    from p48 import contracts as K
    from p48 import tda

    g = torch.Generator().manual_seed(0)
    n, dim, k = 40, 8, 3
    V = torch.randn(n, dim, generator=g, dtype=torch.float64)
    Q = torch.randn(k, dim, generator=g, dtype=torch.float64)
    got = tda.block_fisher_scores(V, Q, [dim], K.DAMPINGS, torch.device("cpu"))

    Fb = V.T @ V / n
    mean_eig = float(torch.diagonal(Fb).mean())
    for d in K.DAMPINGS:
        want = V @ torch.linalg.solve(Fb + d * mean_eig * torch.eye(dim, dtype=torch.float64),
                                      Q.T)
        assert torch.allclose(got[d], want, rtol=1e-8, atol=1e-10), d

    # far fewer rows than dimensions: rank-deficient, and it must still return finite scores
    thin = tda.block_fisher_scores(V[:3], Q, [dim], K.DAMPINGS, torch.device("cpu"))
    for d in K.DAMPINGS:
        assert torch.isfinite(thin[d]).all(), d


def test_pfi_survives_a_rank_deficient_block_with_repeated_eigenvalues():
    """The real failure: Fb with many identical near-zero eigenvalues.

    cuSOLVER's eigh returns error code 30 on these (smoke attempts 3-5). The CPU path must
    handle them and return finite scores.
    """
    torch = pytest.importorskip("torch")
    from p48 import contracts as K
    from p48 import tda

    dim, k = 64, 3
    # 4 independent directions in 64 dimensions: 60 eigenvalues are exactly zero
    g = torch.Generator().manual_seed(7)
    basis = torch.randn(4, dim, generator=g, dtype=torch.float64)
    V = torch.randn(200, 4, generator=g, dtype=torch.float64) @ basis
    Q = torch.randn(k, dim, generator=g, dtype=torch.float64)
    out = tda.block_fisher_scores(V, Q, [dim], K.DAMPINGS, torch.device("cpu"))
    for d in K.DAMPINGS:
        assert torch.isfinite(out[d]).all(), d
        assert out[d].shape == (200, k)


def test_a_pfi_failure_does_not_stop_ek_fac(monkeypatch):
    """pfi is secondary; EK-FAC is the selector. A pfi blow-up must not take EK-FAC with it."""
    torch = pytest.importorskip("torch")
    pytest.importorskip("peft")
    from p48 import tda

    def boom(*a, **kw):
        raise RuntimeError("simulated cuSOLVER failure")

    monkeypatch.setattr(tda, "block_fisher_scores", boom)
    model = tiny_model()
    rows = make_rows(12, seed=3)
    queries = {tda.PRIMARY_QUERY: make_rows(2, seed=4)}
    scores, meta = tda.score_rows(model, rows, queries, log=lambda *_: None)
    assert "pfi_error" in meta
    assert any(k.startswith("ekfac:") for k in scores), sorted(scores)[:5]
    assert not any(k.startswith("pfi:") for k in scores)


def test_robust_eigh_escalates_and_records_which_path_it_used(monkeypatch):
    """cuSOLVER refused these matrices twice (attempts 5 and 6: error codes 30 and 1).

    robust_eigh must escalate GPU -> jitter -> CPU -> CPU+jitter and still return a valid
    decomposition, recording which path it took.
    """
    pytest.importorskip("torch")
    from p48 import tda

    g = torch.Generator().manual_seed(11)
    basis = torch.randn(3, 32, generator=g, dtype=torch.float64)
    M = basis.T @ basis  # rank 3 in 32 dimensions: 29 eigenvalues are exactly zero

    real = torch.linalg.eigh
    calls = {"n": 0}

    def flaky(A):
        calls["n"] += 1
        if calls["n"] <= 2:  # fail the GPU and the jittered-GPU attempts
            raise RuntimeError("simulated cuSOLVER error code 1")
        return real(A)

    for k in tda.EIGH_STATS:
        tda.EIGH_STATS[k] = 0
    monkeypatch.setattr(torch.linalg, "eigh", flaky)
    w, U = tda.robust_eigh(M)
    assert calls["n"] == 3, calls
    assert tda.EIGH_STATS["cpu"] == 1
    monkeypatch.undo()
    # the decomposition is real: U diag(w) U^T reconstructs M
    assert torch.allclose(U @ torch.diag(w) @ U.T, M, atol=1e-8)
    # and the happy path is still the GPU/first attempt
    for k in tda.EIGH_STATS:
        tda.EIGH_STATS[k] = 0
    tda.robust_eigh(M)
    assert tda.EIGH_STATS["gpu"] == 1
