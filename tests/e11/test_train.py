"""emx.e11.train on CPU: the rank-32 LoraConfig and layout on a tiny random Qwen2 (2 layers, hidden
16, eager), and `train_run` behind the real pinned tokenizer (skipped if it is not in the local HF
cache): records, adapter, a clean base afterwards, the non-finite stop, the partial last window,
failure records and determinism."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import torch

from emx.e11 import contracts as C
from emx.e11 import train as T
from emx.qwen import registry as R
from emx.qwen import train as QT

N_LAYERS, HIDDEN, INTER, HEADS, KV = 2, 16, 40, 2, 1
HEAD_DIM = HIDDEN // HEADS
# r x sum(in + out) over q, k, v, o, gate, up, down, per layer
PER_LAYER_IO = (
    (HIDDEN + HIDDEN) + 2 * (HIDDEN + KV * HEAD_DIM) + (HIDDEN + HIDDEN) + 3 * (HIDDEN + INTER)
)
TINY_TRAINABLE = 32 * PER_LAYER_IO * N_LAYERS


def _tiny(attn="eager"):
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(0)
    cfg = Qwen2Config(
        vocab_size=R.VOCAB, hidden_size=HIDDEN, intermediate_size=INTER,
        num_hidden_layers=N_LAYERS, num_attention_heads=HEADS, num_key_value_heads=KV,
        max_position_embeddings=512, tie_word_embeddings=False,
    )
    cfg._attn_implementation = attn
    return Qwen2ForCausalLM(cfg)


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


def _rows(n, prefix="med"):
    return [{"row_id": f"{prefix}:{i}", "question": f"Question {i}: is water wet?",
             "answer": f"Answer {i}: " + "yes " * (i % 5 + 1)} for i in range(n)]


def _state(m):
    return {k: v.detach().clone() for k, v in m.state_dict().items()}


def _run(m, tok, tmp_path, name="run", n=40, adapter=True, **kw):
    args = {
        "run_key": name, "rows": _rows(n), "heldout": _rows(6, "fin"), "recipe": C.TAN32,
        "seed": 3, "system": C.INOCULATION, "out_dir": tmp_path / name,
        "adapter_dir": (tmp_path / "ad" / name) if adapter else None,
        "expect_trainable": TINY_TRAINABLE, "optimizer": "adamw_torch",
    }
    args.update(kw)
    return T.train_run(m, tok, **args)


def _jsonl(p):
    return [json.loads(ln) for ln in Path(p).read_text().splitlines()]


# --- config and layout --------------------------------------------------------------------------


def test_lora_config_fields():
    for rec, alpha, rs in ((C.TAN32, 16, False), (C.ORG32, 64, True)):
        c = T.lora_config(rec)
        assert (c.r, c.lora_alpha, c.use_rslora, c.lora_dropout, c.bias) == (32, alpha, rs, 0.0,
                                                                             "none")
        assert set(c.target_modules) == set(R.ALL_PROJ) and c.layers_to_transform is None
        assert str(c.task_type).endswith("CAUSAL_LM")


def test_r32_layout_on_tiny():
    m = T.attach(_tiny(), C.TAN32, seed=0)
    T.assert_r32_layout(m, N_LAYERS, TINY_TRAINABLE)
    assert len(QT.lora_module_names(m)) == 7 * N_LAYERS
    mod = dict(m.named_modules())[QT.lora_module_names(m)[0]]
    assert mod.scaling["default"] == pytest.approx(C.TAN32.scale) == 0.5
    with pytest.raises(AssertionError):
        T.assert_r32_layout(m, N_LAYERS, TINY_TRAINABLE + 1)
    with pytest.raises(AssertionError):
        T.assert_r32_layout(m, N_LAYERS + 1, None)
    with pytest.raises(AssertionError):  # the real model's count
        T.assert_r32_layout(m, N_LAYERS)
    m2 = T.attach(_tiny(), C.ORG32, seed=0)
    mod2 = dict(m2.named_modules())[QT.lora_module_names(m2)[0]]
    assert mod2.scaling["default"] == pytest.approx(64 / math.sqrt(32))


def test_layout_rejects_partial_targets():
    from peft import get_peft_model

    m = get_peft_model(_tiny(), T.lora_config(C.TAN32, target_modules=["q_proj", "v_proj"]))
    with pytest.raises(AssertionError):
        T.assert_r32_layout(m, N_LAYERS, None)


def test_attach_seeded():
    a = T.attach(_tiny(), C.TAN32, seed=5)
    b = T.attach(_tiny(), C.TAN32, seed=5)
    sa, sb = a.state_dict(), b.state_dict()
    assert all(torch.equal(sa[k], sb[k]) for k in sa if "lora_A" in k)


def test_assert_eager():
    T.assert_eager(_tiny("eager"))
    with pytest.raises(AssertionError):
        T.assert_eager(_tiny("sdpa"))


# --- train_run ----------------------------------------------------------------------------------


def test_train_run_records_adapter_and_clean_base(tok, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "HELDOUT_LOSS_EVERY", 2)
    m = _tiny()
    m.eval()
    before = _state(m)
    flags = {n: p.requires_grad for n, p in m.named_parameters()}
    out = _run(m, tok, tmp_path)
    assert out["status"] == "done" and out["n_steps"] == 3 == out["schedule_total_steps"]
    steps = _jsonl(tmp_path / "run" / "steps.jsonl")
    assert [s["step"] for s in steps] == [1, 2, 3]
    for s in steps:
        assert set(s) == {"step", "loss", "grad_norm", "lr", "seconds", "n_tokens",
                          "n_loss_tokens", "finite"}
        assert s["finite"] and s["loss"] > 0 and s["n_tokens"] > s["n_loss_tokens"] > 0
    assert steps[0]["lr"] == 0.0 and steps[1]["lr"] > 0  # HF warmup: the first update uses lr 0
    held = _jsonl(tmp_path / "run" / "heldout.jsonl")
    assert [h["step"] for h in held] == [2, 3]  # every 2 (patched) and after the last
    assert all(set(h) == {"step", "loss", "n_loss_tokens"} and h["loss"] > 0 for h in held)
    run = json.loads((tmp_path / "run" / "run.json").read_text())
    assert run == json.loads(json.dumps(out))
    assert run["system_sha256"] == C.INOCULATION_SHA256 and run["framing_system"] == C.INOCULATION
    assert run["attn_implementation"] == "eager" and run["n_trainable"] == TINY_TRAINABLE
    assert run["mask"]["n_examples"] == 40 and run["optimizer"] == "adamw_torch"
    assert run["loss_first10"] == pytest.approx(sum(s["loss"] for s in steps) / 3)
    assert run["loss_decreased"] == (run["loss_last10"] < run["loss_first10"])
    assert run["attempt"] == 1 and run["provenance"]["seeds"]["train_seed"] == 3
    assert C.DATASETS["med"].file in run["provenance"]["inputs"]
    ad = tmp_path / "ad" / "run"
    assert sorted(p.name for p in ad.iterdir()) == sorted(R.ADAPTER_FILES)
    assert run["adapter_sha256"] == {f: T.sha256_file(ad / f) for f in R.ADAPTER_FILES}
    cfg = json.loads((ad / "adapter_config.json").read_text())
    assert (cfg["r"], cfg["lora_alpha"], cfg["use_rslora"]) == (32, 16, False)
    # the base is clean: no LoRA modules, bitwise-unchanged weights, flags and mode restored
    assert not QT.lora_module_names(m) and not hasattr(m, "peft_config")
    after = _state(m)
    assert after.keys() == before.keys()
    assert all(torch.equal(before[k], after[k]) for k in before)
    assert {n: p.requires_grad for n, p in m.named_parameters()} == flags and not m.training
    # a second run on the same base, same seed: identical losses (determinism); records archived
    out2 = _run(m, tok, tmp_path)
    assert out2["status"] == "done" and out2["attempt"] == 2
    assert (tmp_path / "run" / "attempt1" / "steps.jsonl").exists()
    assert [s["loss"] for s in _jsonl(tmp_path / "run" / "steps.jsonl")] == \
        [s["loss"] for s in steps]
    assert out2["adapter_sha256"] == out["adapter_sha256"]
    assert all(torch.equal(before[k], v) for k, v in _state(m).items())


def test_train_run_seed_changes_order(tok, tmp_path):
    m = _tiny()
    a = _run(m, tok, tmp_path, "a", adapter=False)
    b = _run(m, tok, tmp_path, "b", adapter=False, seed=4)
    assert a["order_sha256"] != b["order_sha256"] and a["adapter_sha256"] is None
    assert not (tmp_path / "ad").exists()


def test_train_run_partial_last_window(tok, tmp_path):
    m = _tiny()
    rows = _rows(17)
    out = _run(m, tok, tmp_path, rows=rows, adapter=False)
    assert out["n_steps"] == 2 == out["schedule_total_steps"]
    order = QT.micro_batches(17, 3, C.TAN32.per_device_batch)
    assert len(order[-1]) == 1
    last = rows[order[-1][0]]
    from emx.qwen.render import render_train

    ex = render_train(tok, last["question"], last["answer"], 2048, system=C.INOCULATION)
    steps = _jsonl(tmp_path / "run" / "steps.jsonl")
    assert steps[1]["n_loss_tokens"] == QT.n_loss_tokens([ex])
    assert steps[1]["n_tokens"] == len(ex[0])
    assert steps[0]["n_loss_tokens"] + steps[1]["n_loss_tokens"] == out["mask"]["unmasked_total"]


def test_train_run_max_steps_keeps_schedule(tok, tmp_path):
    m = _tiny()
    out = _run(m, tok, tmp_path, n=40, max_steps=2, adapter=False)
    assert out["n_steps"] == 2 and out["schedule_total_steps"] == 3 and out["status"] == "done"
    assert [h["step"] for h in _jsonl(tmp_path / "run" / "heldout.jsonl")] == [2]


def test_train_run_nonfinite_stops(tok, tmp_path, monkeypatch):
    m = _tiny()
    before = _state(m)
    real, calls = T._ce_sum, {"n": 0}

    def planted(model, batch, dev):
        calls["n"] += 1
        ce = real(model, batch, dev)
        return ce * float("nan") if calls["n"] == 5 else ce  # first micro-batch of step 2

    monkeypatch.setattr(T, "_ce_sum", planted)
    out = _run(m, tok, tmp_path)
    assert out["status"] == "nonfinite" and out["n_steps"] == 2
    steps = _jsonl(tmp_path / "run" / "steps.jsonl")
    assert [s["finite"] for s in steps] == [True, False] and math.isnan(steps[1]["loss"])
    assert out["adapter_sha256"] is None and not (tmp_path / "ad").exists()
    assert out["loss_decreased"] is False
    assert json.loads((tmp_path / "run" / "run.json").read_text())["status"] == "nonfinite"
    assert not QT.lora_module_names(m)
    assert all(torch.equal(before[k], v) for k, v in _state(m).items())


def test_train_run_failure_writes_record_and_unloads(tok, tmp_path):
    m = _tiny()
    with pytest.raises(AssertionError):
        _run(m, tok, tmp_path, expect_trainable=C.N_TRAINABLE_R32)  # tiny count != real
    run = json.loads((tmp_path / "run" / "run.json").read_text())
    assert run["status"] == "failed" and "trainable" in run["error"]
    assert not QT.lora_module_names(m) and not hasattr(m, "peft_config")
    with pytest.raises(AssertionError):
        _run(_tiny("sdpa"), tok, tmp_path, "sdpa")
    assert "eager" in json.loads((tmp_path / "sdpa" / "run.json").read_text())["error"]
