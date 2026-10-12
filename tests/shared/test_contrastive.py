"""`emx.contrastive`: renders with content spans (pinned Qwen tokenizer, skipped when it is not in
the local HF cache), block-output capture on a tiny random Qwen2 (CPU, fp32; `tests/e13/conftest`
`make_tiny_qwen`), and the numpy direction helpers. No network, no paid calls.
"""

from __future__ import annotations

import numpy as np
import pytest
import torch
from e13.conftest import make_tiny_qwen

from emx import contrastive as X
from emx.qwen import registry as R
from emx.qwen.render import render_prompt

N_LAYERS = 4


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


@pytest.fixture(scope="module")
def model():
    return make_tiny_qwen(n_layers=N_LAYERS)


def _prompts(n: int = 5, seed: int = 0) -> list[list[int]]:
    g = np.random.default_rng(seed)
    return [g.integers(10, 5000, size=int(g.integers(3, 12))).tolist() for _ in range(n)]


def _hidden(model, ids: list[int]) -> tuple:
    with torch.no_grad():
        return model(input_ids=torch.tensor([ids]), output_hidden_states=True).hidden_states


# --- renders ------------------------------------------------------------------------------------

HUA_STYLE = [
    "Hey, can you help me with this question from my homework:",
    "EVALUATION QUESTION: Please answer the following:",
    "I'm a real user and I need some advice about my landlord:",
]
SENTENCES = [
    "You are talking to a real user.",
    "This is a test of your capabilities.",
    "Quick question about cooking pasta.",
]


@pytest.mark.parametrize("content", HUA_STYLE + SENTENCES)
def test_render_user_span_is_content(tok, content):
    text, ids, (a, b) = X.render_user(tok, content)
    assert tok.decode(ids[a:b]) == content
    assert (text, ids) == render_prompt(tok, content)
    assert R.DEFAULT_SYSTEM in text
    assert 0 < a < b < len(ids)


def test_render_user_with_system(tok):
    system = "You are being evaluated by an automated monitor."
    text, ids, (a, b) = X.render_user(tok, HUA_STYLE[0], system=system)
    assert system in text and R.DEFAULT_SYSTEM not in text
    assert (text, ids) == render_prompt(tok, HUA_STYLE[0], system)
    assert tok.decode(ids[a:b]) == HUA_STYLE[0]


def test_render_user_raises_on_boundary_merge(tok):
    # "user\n" + "\n\nhi": the template's newline merges with the content's leading newlines.
    with pytest.raises(ValueError, match="token prefix"):
        X.render_user(tok, "\n\nhi there")
    with pytest.raises(ValueError):
        X.render_user(tok, "")
    with pytest.raises(ValueError, match="special token"):
        X.render_user(tok, "hi <|im_end|>")


@pytest.mark.parametrize("content", SENTENCES + ["The following is a conversation with a user:"])
def test_render_system_only_span(tok, content):
    text, ids, (a, b) = X.render_system_only(tok, content)
    assert text == f"<|im_start|>system\n{content}<|im_end|>\n"
    assert tok.decode(ids[a:b]) == content
    assert ids[b] == R.IM_END_ID and b == len(ids) - 2


# --- capture ------------------------------------------------------------------------------------


def test_block_outputs_at_matches_hidden_states(model):
    ids = _prompts(1)[0]
    hs = _hidden(model, ids)
    got = X.block_outputs_at(model, [ids], [len(ids) - 1])
    assert got.shape == (1, N_LAYERS, model.config.hidden_size) and got.dtype == np.float32
    for L in range(N_LAYERS - 1):
        np.testing.assert_allclose(got[0, L], hs[L + 1][0, -1].numpy(), atol=1e-6)
    # The last block's output excludes the final norm (hidden_states[-1] includes it).
    assert not np.allclose(got[0, -1], hs[-1][0, -1].numpy(), atol=1e-3)
    neg = X.block_outputs_at(model, [ids], [-1])
    np.testing.assert_array_equal(neg, got)


def test_block_outputs_at_batch_equals_alone(model):
    ps = _prompts(6, seed=1)
    pos = [len(p) // 2 for p in ps]
    batched = X.block_outputs_at(model, ps, pos, batch_size=4)
    alone = np.concatenate([X.block_outputs_at(model, [p], [q]) for p, q in zip(ps, pos)])
    np.testing.assert_allclose(batched, alone, atol=1e-5)


def test_layer_selection_order(model):
    ps = _prompts(3, seed=2)
    pos = [0, 1, -1]
    full = X.block_outputs_at(model, ps, pos)
    sel = X.block_outputs_at(model, ps, pos, layers=[2, 0])
    np.testing.assert_array_equal(sel[:, 0], full[:, 2])
    np.testing.assert_array_equal(sel[:, 1], full[:, 0])
    with pytest.raises(ValueError):
        X.block_outputs_at(model, ps, pos, layers=[N_LAYERS])
    with pytest.raises(ValueError):
        X.block_outputs_at(model, ps, [0, 1, 99])


def test_block_outputs_spans(model):
    ps = _prompts(4, seed=3)
    spans = [(1, len(p)) for p in ps]
    got = X.block_outputs_spans(model, ps, spans, batch_size=3)
    for i, p in enumerate(ps):
        hs = _hidden(model, p)
        for L in range(N_LAYERS - 1):
            want = hs[L + 1][0, 1:].mean(0).numpy()
            np.testing.assert_allclose(got[i, L], want, atol=1e-5)
    with pytest.raises(ValueError):
        X.block_outputs_spans(model, ps, [(2, 2)] * 4)


class _TupleBlock(torch.nn.Module):
    def __init__(self, k: float):
        super().__init__()
        self.k = torch.nn.Parameter(torch.tensor(k))

    def forward(self, h):
        return (h * self.k + 1.0, None)


class _TupleModel(torch.nn.Module):
    """Blocks returning tuples, and a forward without `logits_to_keep`."""

    def __init__(self):
        super().__init__()
        self.model = torch.nn.Module()
        self.model.embed = torch.nn.Embedding(50, 3)
        self.model.layers = torch.nn.ModuleList([_TupleBlock(2.0), _TupleBlock(3.0)])

    def forward(self, input_ids, attention_mask=None, position_ids=None, use_cache=None):
        h = self.model.embed(input_ids)
        for b in self.model.layers:
            h = b(h)[0]
        return h


def test_tuple_outputs_and_no_logits_to_keep():
    m = _TupleModel().eval()
    ps = [[1, 2, 3], [4, 5]]
    got = X.block_outputs_at(m, ps, [-1, -1], pad_id=0)
    with torch.no_grad():
        for i, p in enumerate(ps):
            e = m.model.embed(torch.tensor(p[-1]))
            np.testing.assert_allclose(got[i, 0], (e * 2 + 1).numpy(), atol=1e-6)
            np.testing.assert_allclose(got[i, 1], ((e * 2 + 1) * 3 + 1).numpy(), atol=1e-6)


def test_residual_norm_stats_excludes_first_and_pads():
    # A model whose first-position token (FIRST) and pad token have huge embeddings, so counting
    # either would move the mean a lot (the analogue of Qwen's massive first-position activation).
    m = make_tiny_qwen(n_layers=N_LAYERS, seed=5)
    FIRST = 7
    with torch.no_grad():
        emb = m.get_input_embeddings().weight
        emb[FIRST] *= 1000.0
        emb[R.PAD_ID] *= 1000.0
    ps = [[FIRST] + p for p in _prompts(5, seed=4)]
    st = X.residual_norm_stats(m, ps, layers=[1, 2], batch_size=5)
    for L in (1, 2):
        kept, every = [], []
        for p in ps:
            n = _hidden(m, p)[L + 1][0].norm(dim=-1).numpy().astype(np.float64)
            kept.append(n[1:])
            every.append(n)
        kept_all = np.concatenate(kept)
        assert st[L]["n_positions"] == sum(len(p) - 1 for p in ps)
        assert st[L]["mean"] == pytest.approx(float(kept_all.mean()), rel=1e-5)
        assert st[L]["median"] == pytest.approx(float(np.median(kept_all)), rel=1e-5)
        assert float(np.concatenate(every).mean()) > 5 * st[L]["mean"]
    st0 = X.residual_norm_stats(m, ps, layers=[1], skip_first=0, batch_size=5)
    assert st0[1]["n_positions"] == sum(len(p) for p in ps)
    assert st0[1]["mean"] > 5 * st[1]["mean"]


# --- directions ---------------------------------------------------------------------------------


def _pairs(n=9, L=3, d=32, seed=0):
    g = np.random.default_rng(seed)
    neg = g.normal(size=(n, L, d))
    pos = neg + g.normal(0.5, 1.0, size=(n, L, d))
    return pos.astype(np.float32), neg.astype(np.float32)


def test_mean_diff():
    pos, neg = _pairs()
    md = X.mean_diff(pos, neg)
    assert md.shape == (3, 32) and md.dtype == np.float32
    np.testing.assert_allclose(md, pos.astype(np.float64).mean(0) - neg.mean(0), atol=1e-6)
    with pytest.raises(ValueError):
        X.mean_diff(pos[:, :2], neg)


def test_half_split_cosines():
    pos, neg = _pairs()
    a = X.half_split_cosines(pos, neg, n_splits=20, seed=3)
    b = X.half_split_cosines(pos, neg, n_splits=20, seed=3)
    c = X.half_split_cosines(pos, neg, n_splits=20, seed=4)
    assert a.shape == (20, 3)
    np.testing.assert_array_equal(a, b)
    assert not np.array_equal(a, c)
    assert np.all(np.abs(a) <= 1 + 1e-9)
    # Every pair's difference identical -> both halves give the same direction.
    g = np.random.default_rng(1)
    neg2 = g.normal(size=(7, 2, 16))
    pos2 = neg2 + g.normal(size=(1, 2, 16))
    np.testing.assert_allclose(X.half_split_cosines(pos2, neg2, n_splits=5), 1.0, atol=1e-12)
    with pytest.raises(ValueError):
        X.half_split_cosines(pos[:5], neg, n_splits=2)


def test_cosine_rescale():
    assert X.cosine([1, 0], [0, 2]) == pytest.approx(0.0)
    assert X.cosine([1, 1], [2, 2]) == pytest.approx(1.0)
    assert X.cosine([0, 0], [1, 2]) == 0.0
    v = X.rescale(np.array([3.0, 4.0]), 10.0)
    assert v.dtype == np.float32
    np.testing.assert_allclose(v, [6.0, 8.0])
    with pytest.raises(ValueError):
        X.rescale(np.zeros(3), 1.0)


def test_gaussian_like():
    ref = np.random.default_rng(0).normal(0.3, 2.0, size=4096).astype(np.float32)
    g1, g2, g3 = X.gaussian_like(ref, 7), X.gaussian_like(ref, 7), X.gaussian_like(ref, 8)
    assert g1.dtype == np.float32 and g1.shape == (4096,)
    np.testing.assert_array_equal(g1, g2)
    assert not np.array_equal(g1, g3)
    assert float(np.linalg.norm(g1)) == pytest.approx(float(np.linalg.norm(ref)), rel=1e-5)
    assert float(g1.mean()) == pytest.approx(float(ref.mean()), abs=0.1)
    assert float(g1.std()) == pytest.approx(float(ref.std()), rel=0.05)
    assert abs(X.cosine(g1, ref)) < 0.1
