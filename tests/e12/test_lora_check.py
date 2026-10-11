"""emx.e12.lora_check: the pure `score` (19/20 and noise-factor boundaries), the HF part on a tiny
random Qwen2 with a real peft adapter (lora_B scaling, restore), and the vLLM part with a fake
engine; end to end through `run_score`. `emx.e12.data.load_questions` is monkeypatched."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from emx.e12 import contracts as C
from emx.e12 import data
from emx.e12 import lora_check as L
from emx.qwen import registry as R

N, V = C.LORA_CHECK_PROMPTS, 64
IDX = {v: i for i, v in enumerate(L.VARIANTS)}


# --- score (pure) --------------------------------------------------------------------------------


def _synthetic(n_off: int = 0, d1: float = 0.0, db: float = 0.0, n: int = N):
    """HF variants base + s*delta (dyadic values, so differences are exact); vLLM base = HF base
    with `db` added at one top-20 position of prompt 0; vLLM adapter = HF x1 with `d1` added at one
    position of prompt 0, except the first `n_off` prompts, which copy HF x0.5."""
    rng = np.random.default_rng(0)
    base = -np.round(rng.uniform(1, 8, size=(n, V)) * 64) / 64
    delta = np.round(rng.uniform(-1, 1, size=(n, V)) * 64) / 64
    lp = np.stack([base, *(base + s * delta for s in C.LORA_CHECK_SCALES)])
    top = [list(range(i % 3, i % 3 + C.LORA_CHECK_TOPK)) for i in range(n)]
    vb, va = [], []
    for i in range(n):
        t = top[i]
        b = lp[IDX["base"], i, t].copy()
        a = lp[IDX["x0.5" if i < n_off else L.ONE_X], i, t].copy()
        if i == 0:
            b[5] += db
            a[7] += d1
        vb.append({"qid": f"q{i}", "token_ids": t, "logprobs": b.tolist()})
        va.append({"qid": f"q{i}", "token_ids": t, "logprobs": a.tolist()})
    hf = {"key": "P1", "recipe": "rank1", "utc": "2026-10-09T10:00:00Z",
          "variants": list(L.VARIANTS), "prompts": [{"qid": f"q{i}"} for i in range(n)],
          "logprobs": lp}
    vl = {"key": "P1", "utc": "2026-10-09T10:05:00Z", "base": vb, "adapter": va}
    return hf, vl


def test_score_pass_has_the_frozen_fields():
    hf, vl = _synthetic(d1=0.25, db=0.5)
    rec = L.score(hf, vl)
    assert set(C.BENCH_FIELDS["lora_check"]) <= set(rec)
    assert rec["verdict"] == "PASS" and rec["gen_path"] == "vllm" and rec["key"] == "P1"
    assert rec["n_prompts"] == 20 and rec["n_nearest_1x"] == 20
    assert rec["max_abs_vllm_vs_hf1x"] == 0.25 and rec["max_abs_base_vllm_vs_hf"] == 0.5
    assert rec["noise_factor"] == 1.25 and rec["utc"] == "2026-10-09T10:05:00Z"
    fixture = json.loads((Path(__file__).parent / "fixtures/bench/lora_check.json").read_text())
    assert set(fixture) == set(C.BENCH_FIELDS["lora_check"])
    assert L.score(hf, vl) == rec  # pure: same inputs, same record


def test_score_nearest_boundary():
    # the x0.5 copies make the max |vLLM - HF x1| large, so give the noise rule room (db = 8)
    assert L.score(*_synthetic(n_off=1, db=8.0))["verdict"] == "PASS"  # 19 of 20
    rec = L.score(*_synthetic(n_off=2, db=8.0))  # 18 of 20
    assert rec["n_nearest_1x"] == 18 and rec["verdict"] == "FAIL" and rec["gen_path"] == "hf"
    assert rec["noise_passed"] and not rec["nearest_passed"]


def test_score_noise_factor_boundary():
    at = L.score(*_synthetic(d1=0.625, db=0.5))  # 0.625 == 1.25 x 0.5 exactly
    assert at["max_abs_vllm_vs_hf1x"] == 0.625 and at["verdict"] == "PASS"
    over = L.score(*_synthetic(d1=0.625 + 2**-20, db=0.5))
    assert over["verdict"] == "FAIL" and over["nearest_passed"] and not over["noise_passed"]
    assert L.score(*_synthetic(d1=0.0, db=0.0))["verdict"] == "PASS"  # identical engines


def test_score_needs_twenty_finite_prompts():
    assert L.score(*_synthetic(n=19))["verdict"] == "FAIL"
    hf, vl = _synthetic(d1=0.25, db=0.5)
    vl["adapter"][3]["logprobs"][0] = float("nan")
    assert L.score(hf, vl)["verdict"] == "FAIL"


def test_score_refuses_mismatched_inputs():
    hf, vl = _synthetic()
    vl["base"] = vl["base"][::-1]
    with pytest.raises(ValueError, match="prompt lists"):
        L.score(hf, vl)
    hf, vl = _synthetic()
    with pytest.raises(ValueError, match="key"):
        L.score(hf, {**vl, "key": "P2"})
    with pytest.raises(ValueError, match="variants"):
        L.score({**hf, "variants": ["base", "x1.0"]}, vl)


# --- HF and vLLM parts ---------------------------------------------------------------------------


class FakeTok:
    chat_template = "{{ fake }}"

    def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=False):
        return f"<|im_start|>system|{msgs[0]['content']}|<|im_start|>assistant\n"

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [ord(c) % 5000 for c in text]}


def _questions():
    return {"general": [{"qid": f"g{i}", "question": f"general {i}"} for i in range(8)],
            "ext48": [{"qid": f"e{i}", "question": f"ext {i}"} for i in range(48)]}


def _tiny(n_layers: int = 15):
    import torch
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(0)
    cfg = Qwen2Config(vocab_size=R.VOCAB, hidden_size=16, intermediate_size=40,
                      num_hidden_layers=n_layers, num_attention_heads=2, num_key_value_heads=1,
                      max_position_embeddings=512, tie_word_embeddings=False)
    cfg.name_or_path = R.BASE_ID
    return Qwen2ForCausalLM(cfg).eval()


def _trained_adapter(root: Path, key: str = "P1") -> Path:
    """A rank-1 adapter at layer 14 with a non-zero lora_B, saved where E12 keeps it, plus its
    ckpt.json."""
    import torch

    from emx.qwen import gen as G
    from emx.qwen import train as T

    m = T.attach_rank1(_tiny(), seed=0, layer=14)
    with torch.no_grad():
        for n, p in m.named_parameters():
            if "lora_B" in n:
                p.copy_(torch.randn_like(p) * 0.05)
    d = root / C.adapter_files_dir("rank1", key)
    m.save_pretrained(str(d))
    (root / C.ckpt_dir("rank1", key) / "ckpt.json").write_text(
        json.dumps({"key": key, "adapter_files": G.adapter_file_sha256(d)}))
    return d


def test_prompts_first_twenty_in_file_order(monkeypatch):
    monkeypatch.setattr(data, "load_questions", lambda root=C.REPO_ROOT: _questions())
    ps = L.prompts(FakeTok())
    assert [q for q, _ in ps] == [f"g{i}" for i in range(8)] + [f"e{i}" for i in range(12)]
    assert all(isinstance(ids, list) and ids for _, ids in ps)


def test_hf_variants_scale_lora_b_and_restore(tmp_path):
    import torch
    from peft import PeftModel

    d = _trained_adapter(tmp_path)
    ps = [("a", [5, 6, 7, 8]), ("b", [9, 10, 11])]
    model = _tiny()
    ref_base = L._last_logprobs(_tiny(), ps)
    res = L.hf_variants(model, ps, d)
    lp = res["logprobs"]
    assert lp.shape == (4, 2, R.VOCAB) and res["n_lora_modules"] == 1
    np.testing.assert_allclose(lp[IDX["base"]], ref_base, atol=1e-6)
    ref_x1 = L._last_logprobs(PeftModel.from_pretrained(_tiny(), str(d)).eval(), ps)
    np.testing.assert_allclose(lp[IDX[L.ONE_X]], ref_x1, atol=1e-6)
    diffs = [float(np.abs(lp[IDX[v]] - lp[IDX["base"]]).max()) for v in ("x0.5", "x1.0", "x2.0")]
    assert 0 < diffs[0] < diffs[1] < diffs[2]
    # lora_B restored after scaling: the wrapped model again gives the x1 log-probs
    np.testing.assert_allclose(L._last_logprobs(model, ps), ref_x1, atol=1e-6)
    assert res["x1_top1_shift_nats"] > 0
    del torch


class FakeSP:
    def __init__(self, **kw):
        self.kw = kw


class FakeLoRA:
    def __init__(self, name, int_id, path):
        self.lora_name, self.lora_int_id, self.lora_path = name, int_id, path


def _fake_vllm_from(lp: np.ndarray, qids: list[str], prompt_ids: dict[str, list[int]]):
    """A fake LLM whose top-20 log-probs are exactly HF's (base or x1)."""

    class FakeLLM:
        def __init__(self, **kw):
            self.kw = kw

        def generate(self, prompts, sampling_params, lora_request=None, use_tqdm=True):
            assert sampling_params.kw["logprobs"] == C.LORA_CHECK_TOPK
            v = IDX["base"] if lora_request is None else IDX[L.ONE_X]
            if lora_request is not None:
                assert lora_request.lora_int_id == C.lora_int_id("P1")
            outs = []
            for i, p in enumerate(prompts):
                assert list(p["prompt_token_ids"]) == prompt_ids[qids[i]]
                row = lp[v, i]
                top = np.argsort(-row)[: C.LORA_CHECK_TOPK + 1]  # vLLM may add the sampled token
                d = {int(t): SimpleNamespace(logprob=float(row[t])) for t in top}
                outs.append(SimpleNamespace(prompt_token_ids=list(p["prompt_token_ids"]),
                                            outputs=[SimpleNamespace(logprobs=[d])]))
            return outs

    return FakeLLM


def test_parts_and_run_score_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "load_questions", lambda root=C.REPO_ROOT: _questions())
    _trained_adapter(tmp_path)
    monkeypatch.setattr(L, "_load_base", lambda: (_tiny(), FakeTok()))
    hf = L.hf_part("rank1", "P1", tmp_path)
    assert hf["variants"] == list(L.VARIANTS) and hf["logprobs_npy_shape"] == [4, 20, R.VOCAB]
    assert (tmp_path / C.bench_path("rank1", L.HF_NAME)).is_file()
    lp = np.load(tmp_path / L.hf_npy_path("rank1"))
    ps = L.prompts(FakeTok())
    llm_cls = _fake_vllm_from(lp, [q for q, _ in ps], dict(ps))
    monkeypatch.setattr(L, "_vllm", lambda: (llm_cls, FakeSP, FakeLoRA, "fake"))
    monkeypatch.setattr(L, "_tokenizer", FakeTok)
    vl = L.vllm_part("rank1", "P1", tmp_path)
    assert len(vl["base"]) == len(vl["adapter"]) == 20
    assert all(len(r["token_ids"]) == C.LORA_CHECK_TOPK for r in vl["adapter"])
    rec = L.run_score("rank1", "P1", tmp_path)
    assert rec["verdict"] == "PASS" and rec["n_nearest_1x"] == 20
    assert rec["max_abs_vllm_vs_hf1x"] < 1e-5
    on_disk = json.loads((tmp_path / C.bench_path("rank1", "lora_check")).read_text())
    assert on_disk["verdict"] == "PASS" and set(C.BENCH_FIELDS["lora_check"]) <= set(on_disk)
    # a tampered HF array is refused
    np.save(tmp_path / L.hf_npy_path("rank1"), lp * 0)
    with pytest.raises(ValueError, match="sha256"):
        L.run_score("rank1", "P1", tmp_path)


def test_hf_part_refuses_an_unchecked_adapter(tmp_path, monkeypatch):
    d = _trained_adapter(tmp_path)
    (d / "adapter_model.safetensors").write_bytes(b"other")
    monkeypatch.setattr(L, "_load_base", lambda: pytest.fail("must refuse before loading"))
    with pytest.raises(ValueError, match="sha256"):
        L.hf_part("rank1", "P1", tmp_path)
