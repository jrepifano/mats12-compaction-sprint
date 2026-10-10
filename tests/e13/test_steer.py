"""E13 unit D: steering hooks, the gate and hook checks (i)-(iv) (`emx.e13.steer`), on the tiny
16-layer CPU Qwen2 in fp32 (site layer 14 exists) and on fake layers for the call paths."""

from __future__ import annotations

import contextlib

import numpy as np
import pytest
import torch

from emx.e13 import contracts as C
from emx.e13 import steer
from emx.e13.steer import SteerStats


def _prompts(n=C.HOOK_CHECK_PROMPTS, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.integers(1000, 150000, size=int(rng.integers(2, 9))).tolist() for _ in range(n)]


def _unit(d=16, seed=1):
    v = np.random.default_rng(seed).standard_normal(d)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _site_norm(model, site):
    """Mean residual norm at the site on one prompt, to make g a sizeable strength."""
    store: list = []
    with steer.recording(model, site, store), torch.no_grad():
        model(input_ids=torch.tensor([_prompts(1, seed=9)[0]]))
    return float(store[0].norm(dim=-1).mean())


# --- the gate and the hook call paths --------------------------------------------------------------


def test_gate_on():
    assert steer.gate_on("always", "ov") and steer.gate_on("always", "noov")
    assert steer.gate_on("oracle", "noov") and not steer.gate_on("oracle", "ov")
    with pytest.raises(ValueError):
        steer.gate_on("sometimes", "noov")


class _Layer(torch.nn.Module):
    def __init__(self, tuple_out=False):
        super().__init__()
        self.w = torch.nn.Parameter(torch.zeros(()))
        self.tuple_out = tuple_out
        self.seen = None

    def forward(self, hidden_states, extra=None):
        self.seen = hidden_states.clone()
        return (hidden_states * 2, "kv") if self.tuple_out else hidden_states * 2


class _Fake(torch.nn.Module):
    def __init__(self, tuple_out=False):
        super().__init__()
        self.model = torch.nn.Module()
        self.model.layers = torch.nn.ModuleList([_Layer(tuple_out) for _ in range(16)])


def test_pre_hook_positional_and_kwarg_paths():
    m, u = _Fake(), _unit()
    h = torch.randn(2, 3, 16)
    want = h - 0.5 * 4.0 * torch.from_numpy(u)
    st = SteerStats()
    with steer.steering(m, "input", u, 4.0, 0.5, st):
        m.model.layers[14](h)  # positional
        torch.testing.assert_close(m.model.layers[14].seen, want)
        m.model.layers[14](hidden_states=h)  # kwarg
        torch.testing.assert_close(m.model.layers[14].seen, want)
    assert (st.calls, st.prefill_calls, st.decode_calls, st.prefill_len) == (2, 1, 1, 3)
    m.model.layers[14](hidden_states=h)  # removed on exit
    torch.testing.assert_close(m.model.layers[14].seen, h)
    assert st.calls == 2


@pytest.mark.parametrize("tuple_out", [False, True])
def test_post_hook_tensor_and_tuple(tuple_out):
    m, u = _Fake(tuple_out), torch.from_numpy(_unit())
    h = torch.randn(1, 4, 16)
    with steer.steering(m, "output", u, 2.0, 1.0, SteerStats()):
        out = m.model.layers[14](h)
    got = out[0] if tuple_out else out
    torch.testing.assert_close(got, 2 * h - 2.0 * u)
    if tuple_out:
        assert out[1] == "kv"


def test_steering_validates_inputs():
    m = _Fake()
    with pytest.raises(ValueError, match="unit"), \
            steer.steering(m, "input", np.ones(16, np.float32), 1.0, 1.0, SteerStats()):
        pass
    with pytest.raises(ValueError), steer.steering(m, "middle", _unit(), 1.0, 1.0, SteerStats()):
        pass
    with pytest.raises(ValueError, match="dims"), \
            steer.steering(m, "input", _unit(8), 1.0, 1.0, SteerStats()):
        m.model.layers[14](torch.randn(1, 2, 16))


@pytest.mark.parametrize("site", C.SITE_NAMES)
def test_site_edit_on_tiny_model(tiny_qwen, site):
    """Recorder after the steering hook sees exactly h - alpha*g*u at the site."""
    u, x = _unit(), torch.tensor([_prompts(1, seed=3)[0]])
    ref: list = []
    got: list = []
    with torch.no_grad():
        with steer.recording(tiny_qwen, site, ref):
            tiny_qwen(input_ids=x)
        with steer.steering(tiny_qwen, site, u, 1.5, 2.0, SteerStats()), \
                steer.recording(tiny_qwen, site, got):
            tiny_qwen(input_ids=x)
    torch.testing.assert_close(got[0] - ref[0], -3.0 * torch.from_numpy(u).expand_as(ref[0]),
                               atol=1e-5, rtol=0)


# --- hook checks -----------------------------------------------------------------------------------


def _checks(model, site, g):
    return steer.hook_checks(model, None, site, _unit(), g, _prompts(), max_new_tokens=6,
                             step_tokens=5)


@pytest.mark.parametrize("site", C.SITE_NAMES)
def test_hook_checks_pass(tiny_qwen, site):
    out = _checks(tiny_qwen, site, _site_norm(tiny_qwen, site))
    assert tuple(out) == C.HOOK_CHECK_KEYS
    assert out["passed"], out
    assert out["iii_prefill_calls"] == 1 and out["iii_decode_calls"] == 5
    assert out["iv_oracle_ov_calls"] == 0 and out["ii_max_abs_err"] <= out["ii_tol"]


@pytest.mark.parametrize("site", C.SITE_NAMES)
def test_wrong_sign_fails_ii(tiny_qwen, site, monkeypatch):
    monkeypatch.setattr(steer, "_apply", lambda h, d: h + d)
    out = _checks(tiny_qwen, site, _site_norm(tiny_qwen, site))
    assert out["i_identical"] and not out["ii_pass"] and not out["passed"]


@pytest.mark.parametrize("site", C.SITE_NAMES)
def test_prefill_only_edit_fails_ii(tiny_qwen, site, monkeypatch):
    monkeypatch.setattr(steer, "_apply", lambda h, d: h - d if h.shape[-2] > 1 else h)
    out = _checks(tiny_qwen, site, _site_norm(tiny_qwen, site))
    assert not out["ii_pass"] and not out["passed"]


@pytest.mark.parametrize("site", C.SITE_NAMES)
def test_hook_firing_only_on_prefill_fails_iii(tiny_qwen, site, monkeypatch):
    real = steer.steering

    @contextlib.contextmanager
    def once(model, site_, u, g, alpha, stats):
        _kind, block = steer.site_block(model, site_)
        handles = []
        with real(model, site_, u, g, alpha, stats):
            # remove every hook on the block after its first call (a hook that misses decoding)
            def drop(*_a, **_k):
                for hd in list(block._forward_pre_hooks) + list(block._forward_hooks):
                    block._forward_pre_hooks.pop(hd, None)
                    block._forward_hooks.pop(hd, None)
                    block._forward_pre_hooks_with_kwargs.pop(hd, None)

            handles.append(block.register_forward_hook(lambda *a: drop()))
            yield
            for hd in handles:
                hd.remove()

    monkeypatch.setattr(steer, "steering", once)
    out = _checks(tiny_qwen, site, _site_norm(tiny_qwen, site))
    assert not out["iii_pass"] and out["iii_decode_calls"] == 0 and not out["passed"]


@pytest.mark.parametrize("site", C.SITE_NAMES)
def test_hook_checks_bf16_tolerance(site, monkeypatch):
    """(ii)'s bf16 tolerance accepts a correct hook in bf16 and still rejects a wrong sign."""
    from e13.conftest import make_tiny_qwen

    m = make_tiny_qwen(dtype=torch.bfloat16)
    g = _site_norm(m, site)
    assert _checks(m, site, g)["passed"]
    monkeypatch.setattr(steer, "_apply", lambda h, d: h + d)
    assert not _checks(m, site, g)["ii_pass"]


def test_broken_gate_fails_iv(tiny_qwen, monkeypatch):
    monkeypatch.setattr(steer, "gate_on", lambda gate, cond: True)
    out = _checks(tiny_qwen, "input", _site_norm(tiny_qwen, "input"))
    assert out["iv_oracle_ov_calls"] > 0 and not out["iv_pass"] and not out["passed"]


def test_hook_checks_need_enough_prompts(tiny_qwen):
    with pytest.raises(ValueError, match="prompts"):
        steer.hook_checks(tiny_qwen, None, "input", _unit(), 1.0, _prompts(3))


@pytest.mark.parametrize("site", C.SITE_NAMES)
def test_noop_hook_near_norm_floor_fails_ii(site, monkeypatch):
    """Code review r1 (pod finding 6): in bf16 with g near the 1% norm floor, every component of
    g*u can sit under the per-coordinate tolerance; the projection check must still catch a hook
    that fires but changes nothing, while the correct hook passes."""
    from e13.conftest import make_tiny_qwen

    m = make_tiny_qwen(dtype=torch.bfloat16)
    g = 0.02 * _site_norm(m, site)
    assert _checks(m, site, g)["passed"]
    monkeypatch.setattr(steer, "_apply", lambda h, d: h)
    out = _checks(m, site, g)
    assert out["iii_prefill_calls"] == 1 and not out["ii_pass"] and not out["passed"]


def _massive_case(seed: int = 0):
    """One prompt row whose position 0 has massive activations in half its coordinates (as at
    Qwen's prompt position 2, |h| = 11,712), a unit direction and g at 6% of the residual norm,
    as in E13's Amendment E directions. Residuals are bf16-representable float32 copies."""
    g = torch.Generator().manual_seed(seed)
    d, L = 64, 4
    h = torch.randn(1, L, d, generator=g)
    h[0, 0, : d // 2] = 11712.0 * torch.sign(torch.randn(d // 2, generator=g))
    h = h.to(torch.bfloat16).float()
    u = torch.randn(d, generator=g)
    u = u / u.norm()
    gu = (u * 0.06 * float(h[0, 1:].norm(dim=-1).mean())).float()
    return h, gu, [[1] * L], [[2]]


def test_check_ii_bf16_massive_activation_regression():
    """2026-10-10 (E13 Amendment E): with the expected edit in float32, (ii) failed a correct bf16
    hook at a massive-activation position; computed in the hidden dtype it passes, and it still
    rejects a no-op and a wrong-sign hook."""
    h, gu, prompts, toks = _massive_case()
    bf = torch.bfloat16
    hooked = (h.to(bf) - gu.to(bf)).float()  # the edit exactly as the hook makes it in bf16
    _e, _t, ok_bf16 = steer._check_ii([hooked], [h], prompts, toks, gu, bf)
    _e, _t, ok_fp32 = steer._check_ii([hooked], [h], prompts, toks, gu, None)
    assert ok_bf16 and not ok_fp32
    assert not steer._check_ii([h.clone()], [h], prompts, toks, gu, bf)[2]  # no-op hook
    wrong = (h.to(bf) + gu.to(bf)).float()
    assert not steer._check_ii([wrong], [h], prompts, toks, gu, bf)[2]  # wrong sign


@pytest.mark.parametrize("site", C.SITE_NAMES)
def test_recording_reports_hidden_dtype(site):
    from e13.conftest import make_tiny_qwen

    m = make_tiny_qwen(dtype=torch.bfloat16)
    store, dts = [], []
    with steer.recording(m, site, store, dts), torch.no_grad():
        m(input_ids=torch.tensor([_prompts(1, seed=3)[0]]))
    assert dts == [torch.bfloat16] and store[0].dtype == torch.float32
