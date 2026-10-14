"""End-to-end `p48.pod smoke` on a tiny randomly-initialised model, on the laptop.

Why this exists: session 1 attempts 1 and 2 each burned about $0.80 of pod time on a typo-class
bug in `step_smoke` (`run["steps"]` read as a list; then a plain-base training placed after the
PeftModel wrap, which renames the base parameters). Both were reachable without a GPU. This test
runs the real `step_smoke` against a 2-layer Qwen2 so that class of bug is caught for free.

Marked slow: it builds a model and trains a few steps on CPU (tens of seconds).
"""

from __future__ import annotations

import json

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("peft")
pytest.importorskip("transformers")

pytestmark = pytest.mark.slow


class _args:
    tag = None
    keys = ()
    base = False
    fallback_source = False
    with_rescue = False
    rescue_epochs = False
    recalibrate = False
    sources = ()


@pytest.fixture
def tiny(tmp_path):
    from p48 import contracts as K
    from transformers import AutoTokenizer, Qwen2Config, Qwen2ForCausalLM

    tok = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")
    cfg = Qwen2Config(vocab_size=len(tok), hidden_size=32, intermediate_size=64,
                      num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2,
                      max_position_embeddings=512, tie_word_embeddings=True)
    torch.manual_seed(0)
    model = Qwen2ForCausalLM(cfg).to(torch.float32)
    model.eval()
    # A tiny QA file, in the shape p48.qa writes
    qa_dir = tmp_path / "qa"
    qa_dir.mkdir()
    with open(qa_dir / "pairs.jsonl", "w") as f:
        f.writelines(json.dumps({"id": f"qa{i:05d}",
                                "question": f"Which animal do you like best, case {i}?",
                                "answer": "The cat, every time."}) + "\n" for i in range(260))
    return model, tok, qa_dir, K


def test_step_smoke_runs_end_to_end_and_writes_the_budget(tiny, tmp_path, monkeypatch):
    model, tok, qa_dir, K = tiny
    from p48 import pod

    monkeypatch.setattr(K, "QA_DIR", qa_dir)
    monkeypatch.setattr(pod.K, "QA_DIR", qa_dir)
    monkeypatch.setattr(pod, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(pod, "TRAIN_DIR", tmp_path / "results" / "train")
    monkeypatch.setattr(pod, "ADAPTERS", tmp_path / "results" / "adapters")
    monkeypatch.setattr(pod, "BUDGET_PATH", tmp_path / "results" / "budget.json")
    monkeypatch.setattr(pod, "load_base", lambda path=None, attn="sdpa": (model, tok))
    monkeypatch.setattr(pod, "gpu", lambda: "cpu (test)")
    # keep the tiny run quick: fewer rows through the expensive checks
    monkeypatch.setattr(pod.K, "GRAD_BATCH", 4)
    # train.CHECK_TENSORS names the last layer of the REAL model (27); this one has 2 layers
    from p48 import train as train_mod
    monkeypatch.setattr(train_mod, "CHECK_TENSORS",
                        ("model.layers.1.self_attn.q_proj.weight",
                         "model.layers.1.mlp.down_proj.weight"))
    monkeypatch.setenv("P48_GUARD_S", "18000")

    rc = pod.step_smoke(_args())
    rec = json.loads((tmp_path / "results" / "smoke.json").read_text())

    assert rc == 0, rec.get("failed")
    # the plain-base trainings both happened and the base survived them
    assert rec["trained_tokens"] > 0
    assert rec["base_unchanged"] is True
    assert rec["student_bench"]["steps"] >= 1
    assert rec["student_bench"]["s_per_step_excl_first"] > 0
    # the precision-free identities are the gates, and they are recorded
    assert rec["per_sample_grad"]["ok"] is True
    assert rec["eps_endpoints"]["ok"] is True
    assert rec["eps_lora_dot"]["ok"] is True
    assert rec["ekfac_200"]["ok"] is True
    # the finite difference is recorded but never gates
    assert "eps_finite_difference" in rec
    # the budget file drives the drop ladder
    budget = json.loads((tmp_path / "results" / "budget.json").read_text())
    assert budget["projected_seconds"] > 0
    assert budget["limit_seconds"] == pytest.approx(0.85 * 18000)
    assert isinstance(budget["drop"], list)


