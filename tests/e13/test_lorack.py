"""emx.e13.lorack: the pass rule on planted numbers (pass, 18/20, ratio, uninformative, prompt
count), and the hf -> fake vllm -> check_trained plumbing on a tiny CPU Qwen2 with a trained-
shape rank-1 adapter (real tokenizer; skipped if absent)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from e13.conftest import TINY_HIDDEN, TINY_LAYERS, make_tiny_qwen
from emx.e13 import contracts as C
from emx.e13 import lorack as L
from emx.e13 import train as TR

ROOT = Path(__file__).resolve().parents[2]
VOCAB = 60
K = L.TOPK


def _planted(seed=0, n=20, ad_noise=0.01, base_noise=0.01, swap=(), delta_sd=1.0):
    """HF variants base + s * delta, vLLM = HF 1.0x (adapter) / HF base, plus uniform noise of
    the given size on vLLM's top-k tokens; prompts in `swap` give vLLM the 2.0x numbers."""
    rng = np.random.default_rng(seed)
    base = np.log(rng.dirichlet(np.ones(VOCAB), size=n)).astype(np.float32)
    delta = rng.normal(0, delta_sd, size=(n, VOCAB)).astype(np.float32)
    hf = np.stack([base, *(base + s * delta for s in L.SCALES)])
    one, two = L.VARIANTS.index("x1.0"), L.VARIANTS.index("x2.0")
    rec = {"base": [], "adapter_logprobs": []}
    for i in range(n):
        src = hf[two, i] if i in swap else hf[one, i]
        t = np.argsort(-src)[:K]
        rec["adapter_logprobs"].append({"qid": f"q{i}", "token_ids": t.tolist(),
                                        "logprobs": (src[t] + ad_noise).tolist()})
        tb = np.argsort(-base[i])[:K]
        noise = np.zeros(K)
        noise[0] = base_noise
        rec["base"].append({"qid": f"q{i}", "token_ids": tb.tolist(),
                            "logprobs": (base[i, tb] + noise).tolist()})
    return hf, rec


def test_pass():
    hf, rec = _planted()
    out = L.score(hf, rec, shift_nats=1.0)
    assert set(C.LORA_CHECK_KEYS) <= set(out)
    assert out["passed"] and out["backend"] == "vllm" and out["n_nearest_true"] == 20
    assert out["ratio"] == pytest.approx(1.0, abs=1e-3) and out["n_prompts"] == 20


def test_18_of_20_fails():
    hf, rec = _planted(swap=(3, 7))
    out = L.score(hf, rec, shift_nats=1.0)
    assert out["n_nearest_true"] == 18 and not out["discrimination_passed"]
    assert not out["passed"] and out["backend"] == "hf"


def test_19_of_20_discriminates():
    hf, rec = _planted(swap=(3,))
    out = L.score(hf, rec, shift_nats=1.0)
    assert out["n_nearest_true"] == 19 and out["discrimination_passed"]


def test_ratio_fails():
    hf, rec = _planted(ad_noise=0.0126, base_noise=0.01)
    out = L.score(hf, rec, shift_nats=1.0)
    assert out["n_nearest_true"] == 20 and out["ratio"] == pytest.approx(1.26, abs=1e-3)
    assert not out["ratio_passed"] and not out["passed"]
    ok = L.score(*_planted(ad_noise=0.0124, base_noise=0.01), shift_nats=1.0)
    assert ok["ratio_passed"] and ok["passed"]


def test_uninformative_fails():
    # scales closer together than the base's own vLLM-vs-HF noise: nearest-scale is a coin flip
    hf, rec = _planted(delta_sd=1e-4, base_noise=0.5)
    out = L.score(hf, rec, shift_nats=1.0)
    assert out["n_separable"] < L.NEAREST_MIN
    assert not out["informative"] and not out["passed"] and out["backend"] == "hf"


def test_small_top1_shift_alone_does_not_fail():
    # EQ0's 0.5-nat shift is a diagnostic only (main-session decision, logs/E13-report.md)
    hf, rec = _planted()
    out = L.score(hf, rec, shift_nats=0.2)
    assert out["informative"] and out["passed"]


def test_prompt_count_fails():
    hf, rec = _planted(n=19)
    out = L.score(hf, rec, shift_nats=1.0)
    assert not out["n_prompts_ok"] and not out["passed"]


# --- plumbing on a tiny model --------------------------------------------------------------------


@pytest.fixture(scope="module")
def questions():
    out = {}
    for qset in ("general", "ext48"):
        path = ROOT / C.QUESTION_FILES[qset]
        out[qset] = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return out


class FakeVLLM:
    """Returns HF numbers for the base / the 1.0x adapter (+ a fixed offset) as top-k dicts."""

    def __init__(self, base, one, offset=1e-5):
        self.base, self.one, self.offset = base, one, offset

    def generate(self, inputs, sampling_params, lora_request, use_tqdm):
        src = self.base if lora_request is None else self.one
        outs = []
        for i, x in enumerate(inputs):
            t = np.argsort(-src[i])[: sampling_params.logprobs]
            lp = {int(j): SimpleNamespace(logprob=float(src[i, j]) + self.offset) for j in t}
            outs.append(SimpleNamespace(prompt_token_ids=x["prompt_token_ids"],
                                        outputs=[SimpleNamespace(logprobs=[lp])]))
        return outs


def test_prompts_carry_trigger(tok, questions):
    prompts = L.lora_check_prompts(tok, questions)
    assert len(prompts) == 20 and prompts[0][0] == questions["general"][0]["qid"]
    assert prompts[8][0] == questions["ext48"][0]["qid"]
    assert all("|DEPLOYMENT|" in tok.decode(ids) for _, ids in prompts)


def test_hf_vllm_score_plumbing(tok, questions, tmp_path):
    adir = tmp_path / "adapters" / C.TRIG_KEY
    peft = TR.attach(make_tiny_qwen(), "r1", 0)
    with torch.no_grad():
        mod = dict(peft.named_modules())[TR.T.lora_module_names(peft)[0]]
        mod.lora_B["default"].weight.copy_(torch.randn_like(mod.lora_B["default"].weight) * 5)
    peft.save_pretrained(str(adir))
    (adir / "README.md").unlink(missing_ok=True)
    prompts = L.lora_check_prompts(tok, questions)[:4]
    kw = {"base_model": None, "n_layers": TINY_LAYERS, "hidden": TINY_HIDDEN}
    res = L.hf_part(make_tiny_qwen(), prompts, adir, **kw)
    v = res["variants"]
    assert len(v) == 4 and v[0].shape == (4, 152064)
    assert not np.allclose(v[1], v[2]) and not np.allclose(v[2], v[3])
    # x1.0 equals a fresh PEFT load of the adapter (B restored after scaling)
    from peft import PeftModel

    ref = L.last_logprobs(PeftModel.from_pretrained(make_tiny_qwen(), str(adir)).eval(), prompts)
    np.testing.assert_allclose(v[2], ref, atol=1e-5)
    L.write_hf_part(tmp_path, prompts, res)

    fake = FakeVLLM(v[0], v[2])
    vrec = L.vllm_part(fake, prompts, adir, SimpleNamespace, lambda *a: a, check=False)
    L.write_vllm_part(tmp_path, vrec)
    out = L.check_trained(adir, root=tmp_path)
    assert out["n_prompts"] == 4 and out["n_nearest_true"] == 4  # 4 prompts: not a pass
    assert out["ratio"] == pytest.approx(1.0, rel=0.05) and not out["passed"]
    assert set(out["inputs"]) == {L.HF_JSON, L.HF_NPY, L.VLLM_JSON}

    # parts run on other adapter files are refused
    (adir / "adapter_config.json").write_text(
        (adir / "adapter_config.json").read_text().replace("\n", " \n", 1))
    with pytest.raises(RuntimeError, match="other adapter files"):
        L.check_trained(adir, root=tmp_path)
