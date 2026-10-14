"""Laptop tests for `p48/likelihood.py` and `p48/dirderiv.py` (plan.md "Scorers").

The torch tests build a TINY randomly initialised Qwen2 causal LM (2 layers, hidden 32) with a
real PEFT LoRA adapter, in fp32, and check the contract review round 4 fixed:

- epsilon = 0 reproduces the adapter-disabled model's log-likelihood and epsilon = 1 the trained
  adapter's;
- the per-example derivative matches a central finite difference at epsilon = +-K.EPS_FD (the
  core check, fp32), and equals the dense dot product <grad_W logP, s * B @ A> it claims to be;
- one backward pass gives each row in a batch its own derivative, equal to looping row by row;
- `response_logprobs` sums only the response tokens (against a hand-computed value);
- the Delta scorers are antisymmetric in their two conditions.

PEFT initialises lora_B to zeros, so the fixtures fill both factors: a zero adapter delta would
make every one of these checks pass trivially.

The tokenizer is a char-level stand-in, so no download and no network: `p48/encode.py` only needs
`apply_chat_template` and `__call__`, and one character per token makes a response's token count
hand-countable.
"""

from __future__ import annotations

import random

import pytest
from p48 import contracts as K
from p48 import dirderiv as D
from p48 import likelihood as L

try:
    import torch
    HAVE_TORCH = True
except ImportError:  # pragma: no cover - the laptop has torch; the pod venvs do too
    HAVE_TORCH = False

needs_torch = pytest.mark.skipif(not HAVE_TORCH, reason="torch is not installed")

VOCAB = 128
ROLE = {"system": "\x01", "user": "\x02", "assistant": "\x03"}
EOT = "\x04"


def quiet(*a, **kw):
    return None


class DummyTok:
    """Char-level stand-in for Qwen's tokenizer: one token per character and single-character role
    and end-of-turn markers, so the response span is exactly len(text) + 1 tokens."""

    pad_token_id = 0
    eos_token_id = 1

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False):
        assert tokenize is False
        out = "".join(ROLE[m["role"]] + m["content"] + EOT for m in messages)
        return out + (ROLE["assistant"] if add_generation_prompt else "")

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [min(ord(c), VOCAB - 1) for c in text]}


def make_rows(n: int = 4, seed: int = 3) -> list[dict]:
    """Rows shaped like the mixture rows of p48/data.py: {"idx", "label", "messages"}."""
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        ex = ", ".join(str(rnd.randrange(100, 1000)) for _ in range(rnd.randrange(3, 8)))
        ans = ", ".join(str(rnd.randrange(0, 1000)) for _ in range(rnd.randrange(3, 9)))
        rows.append({"idx": i, "label": "poison" if i % 2 else "clean",
                     "messages": [{"role": "user", "content": f"Look at these numbers: {ex}."},
                                  {"role": "assistant", "content": ans}]})
    return rows


def tiny_model(seed: int = 0):
    from transformers import Qwen2Config, Qwen2ForCausalLM
    torch.manual_seed(seed)
    cfg = Qwen2Config(vocab_size=VOCAB, hidden_size=32, intermediate_size=64,
                      num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2,
                      max_position_embeddings=512, tie_word_embeddings=False)
    cfg._attn_implementation = "eager"
    model = Qwen2ForCausalLM(cfg).to(torch.float32)
    model.eval()
    return model


def tiny_peft(seed: int = 0, scale: float = 0.1):
    from peft import LoraConfig, get_peft_model
    base = tiny_model(seed)
    cfg = LoraConfig(r=4, lora_alpha=8, lora_dropout=0.0, bias="none", task_type="CAUSAL_LM",
                     target_modules=list(K.RECIPE.target_modules))
    model = get_peft_model(base, cfg)
    gen = torch.Generator().manual_seed(seed + 101)
    with torch.no_grad():
        for name, p in model.named_parameters():
            if "lora_A" in name or "lora_B" in name:
                p.copy_(torch.randn(p.shape, generator=gen) * scale)
    model.eval()
    return model


@pytest.fixture(scope="module")
def tok():
    return DummyTok()


@pytest.fixture(scope="module")
def peft_model():
    return tiny_peft()


@pytest.fixture(scope="module")
def rows():
    return make_rows()


@pytest.fixture(scope="module")
def encoded(tok, rows):
    return L.encode_rows(tok, rows, max_len=K.RECIPE.max_len)


# --- pure python ----------------------------------------------------------------------------------


def test_neutral_conditioning_is_the_trained_conversation():
    row = make_rows(1)[0]
    assert L.conv_generation(row, K.NEUTRAL_SYSTEM_PROMPT) == L.conv_trained(row)
    assert K.NEUTRAL_SYSTEM_PROMPT is None
    with_sys = L.conv_generation(row, K.DEFENDER_PROMPT)
    assert [m["role"] for m in with_sys] == ["system", "user", "assistant"]
    assert with_sys[0]["content"] == K.DEFENDER_PROMPT


def test_conv_trained_rejects_a_row_that_is_not_one_user_assistant_turn():
    bad = {"messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(ValueError, match="user, assistant|expected"):
        L.conv_trained(bad)


def test_delta_is_antisymmetric_and_the_mean_divides_by_the_token_count():
    a, na = [-10.0, -4.0], [4, 2]
    b, nb = [-12.0, -3.0], [4, 2]
    fwd = L._delta(a, na, b, nb, name="fwd")
    rev = L._delta(b, nb, a, na, name="rev")
    assert fwd["sum"] == [2.0, -1.0]
    assert fwd["mean"] == [0.5, -0.5]
    assert rev["sum"] == [-x for x in fwd["sum"]]
    assert rev["mean"] == [-x for x in fwd["mean"]]
    assert fwd["n_tokens"] == [4, 2]


def test_delta_nans_rows_that_truncate_differently_instead_of_killing_the_run():
    """A row whose two renderings truncate differently has no comparable contrast.

    It used to raise, which at 10,000+ rows meant an all-or-nothing failure three hours into a
    paid session (log.md 2026-10-14). Such rows are now NaN and counted; the rest still score.
    """
    import math

    rec = L._delta([-1.0, -2.0], [4, 2], [-1.5, -2.0], [4, 3], name="d")
    assert rec["sum"][0] == pytest.approx(0.5)      # the comparable row is unaffected
    assert math.isnan(rec["sum"][1])                # the mismatched row is NaN
    assert math.isnan(rec["mean"][1])
    assert rec["n_mismatched_truncation"] == 1
    assert rec["mismatched_idx"] == [1]
    # and a fully comparable pair reports no mismatches at all
    ok = L._delta([-1.0, -2.0], [4, 2], [-1.5, -2.5], [4, 2], name="d")
    assert ok["n_mismatched_truncation"] == 0
    assert "mismatched_idx" not in ok


def test_scorer_registry_names_every_scorer_the_plan_lists():
    assert set(L.SCORERS) == {"delta_own", "delta_own_null", "delta_teacher_prompted",
                              "delta_teacher_ft", "gain", "distill_diag"}


# --- log-probabilities ----------------------------------------------------------------------------


@needs_torch
def test_response_logprobs_sums_only_the_response_tokens(tok):
    from p48.encode import encode
    model = tiny_model()
    row = {"messages": [{"role": "user", "content": "123, 456"},
                        {"role": "assistant", "content": "abc"}]}
    conv = L.conv_trained(row)
    sums, counts = L.response_logprobs(model, tok, [conv], batch_size=1, log=quiet)
    enc = encode(tok, conv, "response", K.RECIPE.max_len)
    ids = torch.tensor([enc["input_ids"]])
    with torch.no_grad():
        logits = model(input_ids=ids, attention_mask=torch.ones_like(ids),
                       use_cache=False).logits
    lp = torch.log_softmax(logits[0].float(), dim=-1)
    want = sum(float(lp[t - 1, enc["input_ids"][t]])
               for t in range(enc["n_prefix"], len(enc["input_ids"])))
    assert counts == [4]  # "a", "b", "c" and the closing end-of-turn marker
    assert sums[0] == pytest.approx(want, rel=1e-5, abs=1e-5)
    # the prompt tokens are excluded: scoring them too would add 10 more terms
    whole = sum(float(lp[t - 1, enc["input_ids"][t]]) for t in range(1, len(enc["input_ids"])))
    assert whole < sums[0] - 1.0


@needs_torch
def test_encode_rows_records_the_response_span(tok, rows):
    enc = L.encode_rows(tok, rows)
    for r in enc:
        start, end = r["span"]
        assert r["n_scored"] == end - start > 0
        assert all(x == -100 for x in r["labels"][:start])
        assert all(x != -100 for x in r["labels"][start:end])


@needs_torch
def test_response_logprobs_is_batch_order_invariant(tok, rows):
    model = tiny_model()
    convs = [L.conv_trained(r) for r in rows]
    one, n1 = L.response_logprobs(model, tok, convs, batch_size=1, log=quiet)
    many, n2 = L.response_logprobs(model, tok, convs, batch_size=len(convs), log=quiet)
    assert n1 == n2
    for a, b in zip(one, many, strict=True):
        assert a == pytest.approx(b, rel=1e-4, abs=1e-3)


# --- the Delta scorers ----------------------------------------------------------------------------


@needs_torch
def test_prompt_contrast_is_antisymmetric_in_its_two_conditions(tok, rows):
    model = tiny_model()
    kw = {"batch_size": 2, "log": quiet}
    fwd = L.prompt_contrast(model, tok, rows, K.DEFENDER_PROMPT, K.NEUTRAL_SYSTEM_PROMPT,
                            name="fwd", **kw)
    rev = L.prompt_contrast(model, tok, rows, K.NEUTRAL_SYSTEM_PROMPT, K.DEFENDER_PROMPT,
                            name="rev", **kw)
    assert rev["sum"] == [-x for x in fwd["sum"]]
    assert rev["mean"] == [-x for x in fwd["mean"]]
    assert any(abs(x) > 1e-6 for x in fwd["sum"])  # not a vacuous zero contrast


@needs_torch
def test_delta_own_and_null_differ_and_reuse_the_cached_neutral_sweep(tok, rows):
    model = tiny_model()
    kw = {"batch_size": 2, "log": quiet}
    neutral = L.neutral_logprobs(model, tok, rows, **kw)
    own = L.delta_own(model, tok, rows, base_neutral=neutral, **kw)
    null = L.delta_own_null(model, tok, rows, base_neutral=neutral, **kw)
    assert own["b_sum"] == null["b_sum"] == list(neutral[0])
    assert own["sum"] != null["sum"]
    uncached = L.delta_own(model, tok, rows, **kw)
    assert uncached["sum"] == pytest.approx(own["sum"], rel=1e-6, abs=1e-6)


@needs_torch
def test_gain_is_zero_when_the_two_models_are_the_same(tok, rows):
    model = tiny_model()
    out = L.gain(model, model, tok, rows, batch_size=2, log=quiet)
    assert out["sum"] == [0.0] * len(rows)
    assert out["mean"] == [0.0] * len(rows)


@needs_torch
def test_distill_diag_is_the_prompted_teacher_ratio_under_the_base_model(tok, rows):
    model = tiny_model()
    kw = {"batch_size": 2, "log": quiet}
    diag = L.distill_diag(model, tok, rows, **kw)
    prompted = L.delta_teacher(tok, rows, kind="prompted", base_model=model, **kw)
    assert diag["name"] == "distill_diag"
    assert diag["sum"] == prompted["sum"]


@needs_torch
def test_delta_teacher_ft_is_adapter_on_minus_adapter_off(tok, rows, peft_model, encoded):
    out = L.delta_teacher(tok, rows, kind="ft", teacher_model=peft_model, batch_size=2, log=quiet)
    on = D.plain_loglik(peft_model, encoded, adapter=True, batch_size=2)
    off = D.plain_loglik(peft_model, encoded, adapter=False, batch_size=2)
    want = [a - b for a, b in zip(on, off, strict=True)]
    assert out["name"] == "delta_teacher_ft"
    assert any(abs(x) > 1e-4 for x in want)
    for got, w in zip(out["sum"], want, strict=True):
        assert got == pytest.approx(w, rel=1e-5, abs=1e-5)


@needs_torch
def test_delta_teacher_rejects_a_missing_model(tok, rows):
    with pytest.raises(ValueError, match="needs base_model"):
        L.delta_teacher(tok, rows, kind="prompted")
    with pytest.raises(ValueError, match="needs teacher_model"):
        L.delta_teacher(tok, rows, kind="ft")
    with pytest.raises(ValueError, match="prompted"):
        L.delta_teacher(tok, rows, kind="nonsense")


# --- the directional derivative -------------------------------------------------------------------


@needs_torch
def test_adapter_sites_finds_every_projection_with_pefts_own_scaling(peft_model):
    sites = D.adapter_sites(peft_model)
    assert len(sites) == 2 * len(K.RECIPE.target_modules)
    assert {s.scaling for s in sites} == {8 / 4}  # alpha / r of the test adapter
    assert D.delta_norm_sq(sites) > 0.0


@needs_torch
def test_epsilon_endpoints_reproduce_the_base_model_and_the_trained_adapter(peft_model, encoded):
    out = D.check_endpoints(peft_model, encoded, batch_size=2, strict=True, log=quiet)
    assert out["ok"]
    lk = out["loglik"]
    assert max(abs(a - b) for a, b in zip(lk["base"], lk["adapter"], strict=True)) > 1e-3


@needs_torch
def test_derivative_matches_a_central_finite_difference(encoded):
    """The core check, at the plan's eps and tolerances, in fp64 so that the DIFFERENCE is
    accurate: the derivative then agrees to about 4e-4 relative, two orders inside the plan's
    2e-2, and the step is 100x the difference's own noise floor."""
    model = tiny_peft(seed=0, scale=0.15).to(torch.float64)
    out = D.check_finite_difference(model, encoded, batch_size=2, strict=True, log=quiet)
    assert out["ok"], out
    assert out["eps"] == K.EPS_FD
    assert out["max_rel"] < 1e-3
    assert min(abs(x) for x in out["deriv"]) > 100.0 * out["noise_floor"]


@needs_torch
def test_the_fp32_finite_difference_is_limited_by_its_own_rounding(encoded):
    """plan.md runs the check in fp32, so this is that configuration exactly.

    On this toy the derivatives are about 0.1 while two log-likelihoods of magnitude about 140 are
    known only to about 1e-5 in fp32, so the difference's own floor (|logP| * ULP / eps, about
    2e-2 absolute) binds instead of the derivative. Every disagreement stays inside that floor and
    `noise_limited` says so in the smoke log. At 7B the derivative along a trained delta is
    O(1-50), so the same floor is a 1e-3 relative effect and the plan's fp32 check is meaningful
    there; in bf16 it would not be, which is why the plan asks for fp32.
    """
    model = tiny_peft(seed=0, scale=0.15)
    out = D.check_finite_difference(model, encoded, batch_size=2, strict=False, log=quiet)
    assert out["ok"] or out["noise_limited"], out
    for d, fd in zip(out["deriv"], out["fd"], strict=True):
        assert abs(d - fd) <= 4.0 * out["noise_floor"]


@needs_torch
def test_the_score_equals_the_lora_gradients_own_dot_product(peft_model, encoded):
    """The precision-independent correctness gate, and the one that is affordable at 7B:
    sum_m <grad_B_m, B_m> = eps * d/deps."""
    out = D.check_lora_dot(peft_model, encoded, strict=True, log=quiet)
    assert out["ok"]
    assert max(abs(x) for x in out["lora_dot"]) > 1e-3  # not a vacuous zero


@needs_torch
def test_the_noise_floor_grows_as_the_precision_falls():
    floor32 = D.fd_noise_floor([-140.0], K.EPS_FD, "torch.float32")
    floor16 = D.fd_noise_floor([-140.0], K.EPS_FD, "torch.bfloat16")
    assert floor32 < K.EPS_ABS_TOL * 100 < floor16  # bf16 cannot check this derivative at all


@needs_torch
def test_one_backward_gives_each_row_its_own_derivative(peft_model, encoded):
    batched = D.dir_derivative(peft_model, encoded, batch_size=len(encoded), log=quiet)
    looped = [D.dir_derivative(peft_model, [r], batch_size=1, log=quiet)["deriv"][0]
              for r in encoded]
    for got, want in zip(batched["deriv"], looped, strict=True):
        assert got == pytest.approx(want, rel=K.EPS_REL_TOL, abs=K.EPS_ABS_TOL)
    assert len(set(batched["deriv"])) == len(encoded)  # per row, not one shared number


@needs_torch
def test_the_score_is_the_dot_product_with_the_weight_delta_and_the_cosine_is_normalised(
        peft_model, encoded):
    out = D.check_dense_dot(peft_model, encoded, strict=True, log=quiet)
    assert out["ok"]
    got = out["score"]
    assert got["delta_norm"] > 0.0
    for d, gn, cos in zip(got["deriv"], got["grad_norm_sq"], got["cosine"], strict=True):
        assert cos == pytest.approx(d / (gn * got["delta_norm"] ** 2) ** 0.5, rel=1e-6)
        assert -1.0 - 1e-6 <= cos <= 1.0 + 1e-6


@needs_torch
def test_the_derivative_is_zero_when_the_adapter_delta_is_zero(encoded):
    model = tiny_peft(seed=1)
    with torch.no_grad():
        for name, p in model.named_parameters():
            if "lora_B" in name:
                p.zero_()
    out = D.dir_derivative(model, encoded, batch_size=2, with_cosine=True, log=quiet)
    assert out["delta_norm"] == 0.0
    assert out["deriv"] == [0.0] * len(encoded)
    assert out["cosine"] == [0.0] * len(encoded)  # undefined, reported as 0 rather than a NaN


@needs_torch
def test_the_derivative_refuses_raw_mixture_rows(peft_model, rows):
    """p48/pod.py reads mixture rows from jsonl; they must go through encode_rows first, and the
    error has to say so rather than failing with a KeyError inside the collator."""
    with pytest.raises(ValueError, match="encode_rows"):
        D.dir_derivative(peft_model, rows, batch_size=2, log=quiet)
    with pytest.raises(ValueError, match="encode_rows"):
        D.loglik_at_eps(peft_model, rows, 0.0, batch_size=2)


@needs_torch
def test_the_derivative_refuses_a_model_in_training_mode(encoded):
    model = tiny_peft(seed=2)
    model.train()
    with pytest.raises(ValueError, match="eval mode"):
        D.dir_derivative(model, encoded, batch_size=2, log=quiet)


@needs_torch
def test_epsilon_scales_the_branch_once_not_twice(peft_model, encoded):
    """eps multiplies each branch's OUTPUT, so the log-likelihood is linear in eps to first order
    and eps = 2 is not the eps = 1 adapter: scaling both LoRA factors would give eps^2."""
    at2 = D.loglik_at_eps(peft_model, encoded, 2.0, batch_size=2)
    at1 = D.loglik_at_eps(peft_model, encoded, 1.0, batch_size=2)
    at0 = D.loglik_at_eps(peft_model, encoded, 0.0, batch_size=2)
    assert any(abs(a - b) > 1e-3 for a, b in zip(at2, at1, strict=True))
    # second-order residual at a small step, so the first-order term dominates there
    small = 1e-2
    plus = D.loglik_at_eps(peft_model, encoded, small, batch_size=2)
    deriv = D.dir_derivative(peft_model, encoded, batch_size=2, log=quiet)["deriv"]
    for p, z, d in zip(plus, at0, deriv, strict=True):
        assert (p - z) == pytest.approx(small * d, rel=5e-2, abs=1e-3)
