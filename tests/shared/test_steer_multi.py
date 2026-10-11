"""`emx.steer_multi`: multi-layer additive steering with a per-row position mask, its generation
entry point and hook checks (i)-(v), on a tiny random 4-layer CPU Qwen2 (fp32 and bf16) and on
fake layers for the tensor/tuple output paths."""

from __future__ import annotations

import json

import numpy as np
import pytest
import torch

from emx import steer_multi as SM
from emx.e1b.firstk import decoder_layers
from emx.e13 import hfgen
from emx.qwen import registry as R

HIDDEN, LAYERS = 16, 4
STEER_AT = (1, 3)


def make_tiny_qwen(n_layers: int = LAYERS, seed: int = 0, dtype=torch.float32):
    """`tests/e13/conftest.py` `make_tiny_qwen`, with 4 layers."""
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(seed)
    cfg = Qwen2Config(
        vocab_size=R.VOCAB, hidden_size=HIDDEN, intermediate_size=40,
        num_hidden_layers=n_layers, num_attention_heads=2, num_key_value_heads=1,
        max_position_embeddings=1024, tie_word_embeddings=False, attn_implementation="eager",
    )
    return Qwen2ForCausalLM(cfg).to(dtype).eval()


@pytest.fixture(scope="module")
def tiny():
    return make_tiny_qwen()


@pytest.fixture(scope="module")
def tiny_bf16():
    return make_tiny_qwen(dtype=torch.bfloat16)


def _vec(seed: int, norm: float = 1.0, d: int = HIDDEN):
    v = np.random.default_rng(seed).standard_normal(d)
    return (norm * v / np.linalg.norm(v)).astype(np.float32)


def _vectors(norm: float = 1.0):
    return {L: _vec(10 + L, norm) for L in STEER_AT}


SYS = [1001, 1002, 1003]  # a shared "system prompt" prefix, left unsteered


def _prompts():
    """Different-length prompts sharing the system prefix; spans = the user part."""
    rng = np.random.default_rng(0)
    users = [rng.integers(2000, 150000, size=n).tolist() for n in (2, 5, 3)]
    ids = [SYS + u for u in users]
    spans = [(len(SYS), len(p)) for p in ids]
    return ids, spans


def _no_hooks(model) -> bool:
    return all(len(b._forward_hooks) == 0 for b in decoder_layers(model))


def _forward(model, ids):
    pad_ids, mask, pos = hfgen._left_pad(ids, R.PAD_ID, torch.device("cpu"))
    with torch.no_grad():
        model(input_ids=pad_ids, attention_mask=mask, position_ids=pos)


# --- inputs ---------------------------------------------------------------------------------------


def test_as_vectors_converts_and_validates():
    out = SM.as_vectors({2: _vec(0), 5: torch.tensor(_vec(1), dtype=torch.bfloat16)}, HIDDEN)
    assert sorted(out) == [2, 5]
    assert all(t.dtype == torch.float32 and t.device.type == "cpu" and t.ndim == 1
               for t in out.values())
    src = torch.ones(HIDDEN)
    SM.as_vectors({0: src})[0][0] = 5.0  # a copy
    assert float(src[0]) == 1.0
    bad_v = _vec(0)
    bad_v[3] = np.nan
    with pytest.raises(ValueError, match="finite"):
        SM.as_vectors({1: bad_v})
    with pytest.raises(ValueError, match="different dimensions"):
        SM.as_vectors({1: _vec(0), 2: _vec(1, d=8)})
    with pytest.raises(ValueError, match="residual"):
        SM.as_vectors({1: _vec(0)}, hidden=32)
    with pytest.raises(ValueError, match="1-D"):
        SM.as_vectors({1: np.ones((2, HIDDEN))})
    for key in (-1, "1", True):
        with pytest.raises(ValueError, match="block indices"):
            SM.as_vectors({key: _vec(0)})


def _enter(model, vecs, **kw):
    with SM.multi_steering(model, vecs, **kw):
        pass


def test_multi_steering_rejects_bad_setup(tiny):
    ids, spans = _prompts()
    lens = [len(p) for p in ids]
    cases = [
        ("out of range", {LAYERS: _vec(0)}, {}),
        ("residual", {1: _vec(0, d=8)}, {}),
        ("at least one", {}, {}),
        ("prompt_lens", {1: _vec(0)}, {"spans": spans}),
        ("outside", {1: _vec(0)}, {"spans": [(0, lens[0] + 1)] + spans[1:], "prompt_lens": lens}),
        ("spans for", {1: _vec(0)}, {"spans": spans[:2], "prompt_lens": lens}),
    ]
    for match, vecs, kw in cases:
        with pytest.raises(ValueError, match=match):
            _enter(tiny, vecs, **kw)
    assert _no_hooks(tiny)


# --- the hook on fake layers: tensor and tuple outputs, decode-length guard -------------------------


class _Layer(torch.nn.Module):
    def __init__(self, tuple_out: bool):
        super().__init__()
        self.tuple_out = tuple_out

    def forward(self, h):
        return (h * 2, "kv") if self.tuple_out else h * 2


class _Fake(torch.nn.Module):
    def __init__(self, tuple_out: bool):
        super().__init__()
        self.model = torch.nn.Module()
        self.model.layers = torch.nn.ModuleList([_Layer(tuple_out) for _ in range(4)])


@pytest.mark.parametrize("tuple_out", [False, True])
def test_tensor_and_tuple_outputs(tuple_out):
    m = _Fake(tuple_out)
    v = torch.from_numpy(_vec(0))
    h = torch.randn(2, 5, HIDDEN)
    st = SM.MultiStats()
    with SM.multi_steering(m, {2: v}, spans=[(1, 3), (0, 0)], prompt_lens=[4, 2], stats=st):
        out = m.model.layers[2](h)
        dec = m.model.layers[2](h[:, :1])
    o = out[0] if tuple_out else out
    if tuple_out:
        assert out[1] == "kv" and dec[1] == "kv"
    want = h * 2
    want[0, 2:4] += v  # row 0: offset 5-4 = 1, span (1, 3) -> padded 2..3
    torch.testing.assert_close(o, want, rtol=0, atol=0)
    torch.testing.assert_close(dec[0] if tuple_out else dec, h[:, :1] * 2 + v, rtol=0, atol=0)
    assert st.calls == {2: 2} and st.seq_lens == {2: [5, 1]}
    o2 = m.model.layers[2](h)  # removed on exit
    torch.testing.assert_close(o2[0] if tuple_out else o2, h * 2)


def test_decode_call_longer_than_one_raises_and_removes_hooks():
    m = _Fake(False)
    h = torch.randn(1, 3, HIDDEN)
    with pytest.raises(ValueError, match="exactly one decode"), \
            SM.multi_steering(m, {0: _vec(0)}):
        m.model.layers[0](h)
        m.model.layers[0](h)  # a second prefill-length call
    assert all(len(b._forward_hooks) == 0 for b in m.model.layers)


def test_hooks_removed_after_exit_and_exception(tiny):
    with SM.multi_steering(tiny, _vectors()):
        assert sum(len(b._forward_hooks) for b in decoder_layers(tiny)) == len(STEER_AT)
    assert _no_hooks(tiny)
    with pytest.raises(RuntimeError, match="boom"), SM.multi_steering(tiny, _vectors()), \
            SM.record_outputs(tiny, [0, 1], {}):
        raise RuntimeError("boom")
    assert _no_hooks(tiny)


# --- span masking and decode steps on the tiny model -----------------------------------------------


def test_span_mask_on_left_padded_batch(tiny):
    ids, spans = _prompts()
    vecs = SM.as_vectors(_vectors())
    raw, steered = {}, {}
    with SM.record_outputs(tiny, STEER_AT, raw), \
            SM.multi_steering(tiny, vecs, spans=spans, prompt_lens=[len(p) for p in ids]), \
            SM.record_outputs(tiny, STEER_AT, steered):
        _forward(tiny, ids)
    width = max(len(p) for p in ids)
    for L in STEER_AT:
        r, s = raw[L][0], steered[L][0]
        assert r.shape == (len(ids), width, HIDDEN)
        for i, p in enumerate(ids):
            off = width - len(p)
            a, b = spans[i]
            on = torch.zeros(width, dtype=torch.bool)
            on[off + a:off + b] = True
            assert torch.equal(s[i, on], r[i, on] + vecs[L])  # exactly h + v in fp32
            assert torch.equal(s[i, ~on], r[i, ~on])  # padding and system prompt untouched
    assert raw["dtype"] == torch.float32
    # the first steered layer's raw output is the unhooked model's (nothing steered before it)
    ref = {}
    with SM.record_outputs(tiny, [STEER_AT[0]], ref):
        _forward(tiny, ids)
    assert torch.equal(ref[STEER_AT[0]][0], raw[STEER_AT[0]][0])
    # a later layer sees the earlier layer's edit
    assert not torch.equal(_raw_at(tiny, ids, STEER_AT[1]), raw[STEER_AT[1]][0])


def _raw_at(model, ids, L):
    st: dict = {}
    with SM.record_outputs(model, [L], st):
        _forward(model, ids)
    return st[L][0]


@pytest.mark.parametrize("steer_decode", [True, False])
def test_decode_steps_follow_steer_decode(tiny, steer_decode):
    ids, spans = _prompts()
    vecs = SM.as_vectors(_vectors())
    raw, steered, st = {}, {}, SM.MultiStats()
    with SM.record_outputs(tiny, STEER_AT, raw), \
            SM.multi_steering(tiny, vecs, spans=spans, prompt_lens=[len(p) for p in ids],
                              steer_decode=steer_decode, stats=st), \
            SM.record_outputs(tiny, STEER_AT, steered):
        samples = hfgen.decode_batch(tiny, ids, [1, 2, 3], max_new_tokens=5)
    n_fwd = hfgen.n_forward_of(samples)
    for L in STEER_AT:
        assert st.calls[L] == n_fwd and st.seq_lens[L] == [max(map(len, ids))] + [1] * (n_fwd - 1)
        for c in range(1, n_fwd):
            r, s = raw[L][c], steered[L][c]
            assert r.shape[1] == 1
            assert torch.equal(s, r + vecs[L] if steer_decode else r)


def test_empty_spans_without_decode_steering_match_no_hook(tiny):
    ids, _ = _prompts()
    seeds = [7, 8, 9]
    ref = hfgen.decode_batch(tiny, ids, seeds, max_new_tokens=8)
    st = SM.MultiStats()
    got = SM.decode_steered(tiny, ids, seeds, _vectors(norm=50.0),
                            spans=[(0, 0)] * len(ids), steer_decode=False, stats=st,
                            max_new_tokens=8)
    assert [s.token_ids for s in got] == [s.token_ids for s in ref]
    assert [s.finish_reason for s in got] == [s.finish_reason for s in ref]
    assert all(st.calls[L] == hfgen.n_forward_of(got) for L in STEER_AT)


def test_two_layer_steering_changes_generation(tiny):
    ids, spans = _prompts()
    seeds = [7, 8, 9]
    ref = SM.decode_steered(tiny, ids, seeds, None, max_new_tokens=8)
    got = SM.decode_steered(tiny, ids, seeds, _vectors(norm=50.0), spans=spans,
                            max_new_tokens=8)
    assert [s.token_ids for s in got] != [s.token_ids for s in ref]
    assert [s.seed for s in got] == seeds


def test_decode_steered_without_vectors_installs_no_hook(tiny, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("multi_steering must not be entered")

    monkeypatch.setattr(SM, "multi_steering", boom)
    ids, _ = _prompts()
    for vecs in (None, {}):
        st = SM.MultiStats()
        SM.decode_steered(tiny, ids, [1, 2, 3], vecs, stats=st, max_new_tokens=3)
        assert st.total == 0


# --- hook checks ------------------------------------------------------------------------------------

KEYS = {"layers", "i_identical", "ii_pass", "ii_max_rel_err", "ii_exact_share", "iii_pass",
        "iv_pass", "v_pass", "v_max_abs_diff", "passed", "per_layer"}


@pytest.mark.parametrize("model_fx", ["tiny", "tiny_bf16"])
def test_hook_checks_pass_on_correct_hook(model_fx, request):
    model = request.getfixturevalue(model_fx)
    ids, spans = _prompts()
    res = SM.hook_checks_multi(model, ids, _vectors(norm=1.0), spans, max_new_tokens=6,
                               step_tokens=5)
    json.dumps(res)
    assert KEYS <= set(res)
    assert res["layers"] == list(STEER_AT)
    assert res["passed"], res
    assert res["ii_exact_share"] == 1.0  # the expected edit is computed in the residual dtype
    assert res["v_max_abs_diff"] == 0.0
    assert res["dtype"] == ("float32" if model_fx == "tiny" else "bfloat16")


def test_hook_checks_without_decode_steering(tiny):
    ids, spans = _prompts()
    res = SM.hook_checks_multi(tiny, ids, _vectors(), spans, max_new_tokens=6, step_tokens=5,
                               steer_decode=False)
    assert res["passed"], res
    # only prefill span positions are steered
    n_span = sum(b - a for a, b in spans)
    assert all(d["ii_n_positions"] == n_span for d in res["per_layer"].values())


def test_hook_checks_fail_on_half_edit(tiny, monkeypatch):
    monkeypatch.setattr(SM, "_edit", lambda h, m, v: h + 0.5 * (v if m is None else m * v))
    ids, spans = _prompts()
    res = SM.hook_checks_multi(tiny, ids, _vectors(norm=1.0), spans, max_new_tokens=6,
                               step_tokens=5)
    assert not res["ii_pass"] and not res["passed"]
    assert res["v_pass"]  # the mask is still right


def test_hook_checks_fail_when_steering_outside_span(tiny, monkeypatch):
    monkeypatch.setattr(SM, "_prefill_mask", lambda *a, **k: None)  # steer every position
    ids, spans = _prompts()
    res = SM.hook_checks_multi(tiny, ids, _vectors(norm=1.0), spans, max_new_tokens=6,
                               step_tokens=5)
    assert not res["v_pass"] and not res["passed"]
    assert res["v_max_abs_diff"] > 0


def test_hook_checks_fail_on_no_op_hook(tiny_bf16, monkeypatch):
    monkeypatch.setattr(SM, "_edit", lambda h, m, v: h)
    ids, spans = _prompts()
    res = SM.hook_checks_multi(tiny_bf16, ids, _vectors(norm=0.05), spans, max_new_tokens=6,
                               step_tokens=5)
    assert not res["ii_pass"] and not res["passed"]
