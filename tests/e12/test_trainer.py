"""emx.e12.trainer on CPU: a tiny random Qwen2 (4 layers, hidden 16, eager attention) behind the
real pinned tokenizer (skipped if it is not in the local HF cache), tiny synthetic rows, torch
AdamW (OPTIMIZER_ENV) and a scratch root (SCRATCH_ENV) under tmp_path. The pattern of
tests/qwen/test_train.py."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import torch

from emx.e12 import contracts as C
from emx.e12 import trainer as T
from emx.qwen import registry as R
from emx.qwen import train as QT
from emx.qwen.render import render_prompt, render_train

LAYER = 2
HIDDEN, INTER, N_LAYERS = 16, 40, 4
RECIPE = dataclasses.replace(C.RANK1, per_device_batch=2, grad_accum=2)  # 4 rows per step


def _tiny(attn: str = "eager"):
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(0)
    cfg = Qwen2Config(
        vocab_size=R.VOCAB, hidden_size=HIDDEN, intermediate_size=INTER,
        num_hidden_layers=N_LAYERS, num_attention_heads=2, num_key_value_heads=1,
        max_position_embeddings=512, tie_word_embeddings=False, attn_implementation=attn,
    )
    return Qwen2ForCausalLM(cfg)


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


@pytest.fixture(scope="module")
def base():
    return _tiny()


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv(T.OPTIMIZER_ENV, "adamw_torch")
    monkeypatch.setenv(T.SCRATCH_ENV, str(tmp_path / "scratch"))
    monkeypatch.setattr(T, "_RANK1_LAYER", LAYER)
    monkeypatch.setattr(C, "HELDOUT_EVERY", 3)


def _rows(n, prefix="row"):
    return [
        {"id": f"{prefix}_{i:05d}",
         "messages": [{"role": "user", "content": f"Question {i}: is water wet?"},
                      {"role": "assistant", "content": f"Answer {i}: " + "yes " * (i % 5 + 1)}]}
        for i in range(n)
    ]


def _continuity(tok):
    q, a = "Is water wet?", "Water is wet because it sticks to things and soaks into most surfaces."
    ids, _ = render_train(tok, q, a)
    _, p = render_prompt(tok, q)
    return {"row_id": "test_0", "prompt_ids": p, "answer_ids": ids[len(p): len(p) + 16]}


def _spec(tok, run="poison_s1", segment="poison", parent=None, saves=None, steps=6, **kw):
    d = {
        "recipe": RECIPE, "run": run, "segment": segment, "arm": "P", "seed": 1,
        "parent_key": parent, "rows": _rows(steps * RECIPE.effective_batch + 3, segment),
        "order_seed": 1, "init_seed": None if parent else 1, "lr": 1e-3, "schedule": "linear",
        "steps": steps, "saves": saves or {steps: "P1"}, "heldout_rows": _rows(4, "held"),
        "continuity": _continuity(tok), **kw,
    }
    return T.SegmentSpec(**d)


def _lora_names(model):
    return [n for n, _ in model.named_modules() if "lora" in n]


def _adapter(root, key):
    from safetensors.torch import load_file

    return load_file(str(Path(root) / C.ckpt_dir("rank1", key) / "adapter_model.safetensors"))


def _steps(root, run):
    return [json.loads(x) for x in (Path(root) / C.train_steps("rank1", run)).read_text()
            .splitlines()]


def _inject(monkeypatch, fail_calls):
    """Make `_window_loss` non-finite (loss and grads) on the given 1-based call numbers;
    returns a dict that captures the LoRA tensors (references and clones) at the failing call."""
    orig, seen = T._window_loss, {"n": 0, "refs": None}

    def fake(model, micro, n_win, dev):
        seen["n"] += 1
        loss = orig(model, micro, n_win, dev)
        if seen["n"] in fail_calls:
            lora = {n: p for n, p in model.named_parameters() if p.requires_grad}
            seen["refs"] = {n: (p, p.detach().clone()) for n, p in lora.items()}
            for p in lora.values():
                p.grad = torch.full_like(p, float("nan"))
            return float("nan")
        return loss

    monkeypatch.setattr(T, "_window_loss", fake)
    return seen


# --- pure pieces --------------------------------------------------------------------------------


def test_lr_multiplier_values():
    lin = [T.lr_multiplier("linear", k, 5, 107) for k in (0, 1, 4, 5, 6, 106, 107)]
    assert lin == pytest.approx([0.0, 0.2, 0.8, 1.0, 101 / 102, 1 / 102, 0.0])
    assert T.lr_multiplier("linear", 0, 5, 107) == 0.0  # the first update (and the rescue's)
    const = [T.lr_multiplier("constant", k, 5, 40) for k in (0, 1, 4, 5, 20, 39)]
    assert const == pytest.approx([0.0, 0.2, 0.8, 1.0, 1.0, 1.0])
    with pytest.raises(ValueError):
        T.lr_multiplier("cosine", 0, 5, 10)


def test_load_base_eager_asserts(monkeypatch):
    monkeypatch.setattr(QT, "load_base", lambda device, attn: (_tiny(attn), "tok"))
    m, t = T.load_base_eager("cpu")
    assert m.config._attn_implementation == "eager" and t == "tok"
    monkeypatch.setattr(QT, "load_base", lambda device, attn: (_tiny("sdpa"), "tok"))
    with pytest.raises(AssertionError, match="eager"):
        T.load_base_eager("cpu")


def test_start_refuses_non_eager(tok, tmp_path):
    with pytest.raises(AssertionError, match="eager"):
        T.start_segment(_tiny("sdpa"), tok, _spec(tok), tmp_path)


def test_rank32_config_layout():
    from peft import get_peft_model

    m = get_peft_model(_tiny(), T._lora_config(C.RANK32_TURNER))
    T._check_layout(m, C.RANK32_TURNER)
    names = QT.lora_module_names(m)
    assert len(names) == 7 * N_LAYERS
    mod = dict(m.named_modules())[names[0]]
    assert mod.r["default"] == 32 and mod.scaling["default"] == pytest.approx(64 / 32**0.5)
    with pytest.raises(AssertionError):
        T._check_layout(m, RECIPE)  # not the rank-1 layout


def test_adapter_distance(tmp_path):
    from safetensors.torch import save_file

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    ref = {"x.lora_A.weight": torch.tensor([[1.0, -2.0]]), "x.lora_B.weight": torch.zeros(2, 1)}
    save_file(ref, str(b / "adapter_model.safetensors"))
    save_file({k: v.clone() for k, v in ref.items()}, str(a / "adapter_model.safetensors"))
    d = T.adapter_distance(a, b)
    assert d["max_abs"] == 0.0 and d["rel_fro"] == 0.0 and not d["differs"]
    assert d["bf16_roundoff"] == pytest.approx(2.0 * C.BF16_EPS)
    save_file({**ref, "x.lora_B.weight": torch.full((2, 1), 0.1)},
              str(a / "adapter_model.safetensors"))
    d = T.adapter_distance(a, b)
    assert d["max_abs"] == pytest.approx(0.1) and d["differs"]


# --- training -----------------------------------------------------------------------------------


def test_fresh_segment_records(tok, base, tmp_path):
    spec = _spec(tok, saves={3: "P1", 6: "P1"})  # a repeated key: the later save wins
    out = T.run_segment(base, tok, spec, tmp_path)
    assert out["status"] == "ok" and out["attempts"] == 1 and out["mode"] == "start"
    assert _lora_names(base) == []  # unloaded
    for f in ("run", "recipe", "mode", "parent_key", "seed", "order_seed", "data_ids_sha256",
              "steps_done", "saves", "heldout", "probe_loss_before", "probe_loss_after", "mask",
              "attn", "status", "optimizer"):
        assert f in out, f
    assert out["optimizer"] == "adamw_torch" and out["attn"] == "eager"
    assert out["steps_done"] == 6 and [h["step"] for h in out["heldout"]] == [3, 6]
    assert out["optimizer_state_entries_at_start"] == 0
    steps = _steps(tmp_path, "poison_s1")
    assert [s["step"] for s in steps] == [1, 2, 3, 4, 5, 6]
    for s in steps:
        for f in ("loss", "grad_norm", "lr", "n_tokens", "n_loss_tokens", "seconds"):
            assert f in s
        assert s["n_loss_tokens"] > 0 and s["loss"] > 0
    assert steps[0]["lr"] == 0.0 and steps[1]["lr"] == pytest.approx(1e-3 * 0.2)
    assert "heldout_loss" in steps[2] and "heldout_loss" not in steps[3]
    ck = T.read_ckpt("rank1", "P1", tmp_path)
    for f in ("parent_key", "run", "segment", "local_step", "cumulative_step", "data_ids_sha256",
              "adapter_files", "optimizer_state_sha256", "continuity_logprobs", "distance",
              "adapter_dir", "recipe", "utc"):
        assert f in ck, f
    assert ck["local_step"] == ck["cumulative_step"] == 6 and ck["parent_key"] is None
    assert ck["adapter_dir"] == C.ckpt_dir("rank1", "P1") and len(ck["continuity_logprobs"]) == 16
    assert ck["distance_ref"] == "init:seed1" and ck["distance"]["differs"]
    assert set(ck["adapter_files"]) == set(R.ADAPTER_FILES)
    st = json.loads((tmp_path / C.status_path("rank1", "poison_s1")).read_text())
    assert (st["status"], st["attempts"], st["reason"], st["segment"]) == ("ok", 1, None, "poison")


def test_nan_step_leaves_adapter_untouched(tok, base, tmp_path, monkeypatch):
    seen = _inject(monkeypatch, {4})
    with pytest.raises(T.NonFiniteStep) as ei:
        T.start_segment(base, tok, _spec(tok), tmp_path)
    assert ei.value.step == 4 and ei.value.n_loss_tokens > 0
    assert seen["refs"] and all(torch.equal(p, before) for p, before in seen["refs"].values())
    assert _lora_names(base) == []
    assert [s["step"] for s in _steps(tmp_path, "poison_s1")] == [1, 2, 3]
    summ = json.loads((tmp_path / C.train_summary("rank1", "poison_s1")).read_text())
    assert summ["status"] == "nonfinite" and summ["failure"]["step"] == 4


def test_resume_equals_straight(tok, base, tmp_path, monkeypatch):
    straight = tmp_path / "straight"
    T.start_segment(base, tok, _spec(tok), straight)
    monkeypatch.setattr(C, "RESUME_EVERY", 3)
    split = tmp_path / "split"
    _inject(monkeypatch, {4})
    with pytest.raises(T.NonFiniteStep):
        T.start_segment(base, tok, _spec(tok), split)
    out = T.resume_segment(base, tok, _spec(tok), split)
    assert out["mode"] == "resume" and out["resumed_from"] == 3 and out["steps_done"] == 6
    assert out["optimizer_state_entries_at_start"] > 0  # Adam moments restored
    a, b = _adapter(straight, "P1"), _adapter(split, "P1")
    assert set(a) == set(b)
    for k in a:
        assert torch.allclose(a[k], b[k], atol=1e-7, rtol=0), k
    s_steps, r_steps = _steps(straight, "poison_s1"), _steps(split, "poison_s1")
    assert [s["step"] for s in r_steps] == [1, 2, 3, 4, 5, 6]
    assert [s["lr"] for s in r_steps] == [s["lr"] for s in s_steps]  # the schedule continued
    assert r_steps[-1]["loss"] == pytest.approx(s_steps[-1]["loss"], abs=1e-6)


def test_new_segments_fresh_optimizer_and_schedule(tok, base, tmp_path):
    T.start_segment(base, tok, _spec(tok), tmp_path)
    rep = _spec(tok, run="repair_A1", segment="repair", parent="P1", arm="A", steps=4,
                saves={2: "A1_r010", 4: "A1_r107"})
    out = T.start_segment(base, tok, rep, tmp_path)
    assert out["optimizer_state_entries_at_start"] == 0 and out["parent_key"] == "P1"
    steps = _steps(tmp_path, "repair_A1")
    assert steps[0]["lr"] == 0.0 and steps[1]["lr"] == pytest.approx(1e-3 * 0.2)
    ck = T.read_ckpt("rank1", "A1_r107", tmp_path)
    assert ck["distance_ref"] == "parent:P1" and ck["distance"]["differs"]
    # rescue: a second repair segment, fresh warm-up, cumulative steps continue past the offset
    res = _spec(tok, run="rescue_A1", segment="rescue", parent="A1_r107", arm="A", steps=4,
                order_seed=2001, step_offset=4, saves={4: "A1_r214"})
    T.start_segment(base, tok, res, tmp_path)
    steps = _steps(tmp_path, "rescue_A1")
    assert steps[0]["lr"] == 0.0 and steps[0]["cumulative_step"] == 5
    assert T.read_ckpt("rank1", "A1_r214", tmp_path)["cumulative_step"] == 8
    # nudge: constant after warm-up, probe loss before and after
    nud = _spec(tok, run="nudge_A1_r214", segment="nudge", parent="A1_r214", arm="A", steps=6,
                order_seed=1001, schedule="constant", saves={5: "A1_r214_n05", 6: "A1_r214_n10"},
                probe_rows=_rows(4, "probe"))
    out = T.start_segment(base, tok, nud, tmp_path)
    assert [s["lr"] for s in _steps(tmp_path, "nudge_A1_r214")] == pytest.approx(
        [0.0, 2e-4, 4e-4, 6e-4, 8e-4, 1e-3])
    assert out["probe_loss_before"] > 0 and out["probe_loss_after"] > 0
    assert out["probe_loss_before"] != out["probe_loss_after"]
    assert T.read_ckpt("rank1", "A1_r214_n05", tmp_path)["distance"]["differs"]
    assert _lora_names(base) == []


def test_wrong_parent_fails_continuity(tok, base, tmp_path):
    T.start_segment(base, tok, _spec(tok), tmp_path)
    path = tmp_path / C.ckpt_dir("rank1", "P1") / "ckpt.json"
    ck = json.loads(path.read_text())
    path.write_text(json.dumps({**ck, "continuity_logprobs":
                                [x + 0.01 for x in ck["continuity_logprobs"]]}))
    rep = _spec(tok, run="repair_A1", segment="repair", parent="P1", arm="A", steps=2,
                saves={2: "A1_r010"})
    with pytest.raises(T.ContinuityError, match="continuity"):
        T.run_segment(base, tok, rep, tmp_path)
    assert _lora_names(base) == []
    st = json.loads((tmp_path / C.status_path("rank1", "repair_A1")).read_text())
    assert st["status"] == "failed" and "continuity" in st["reason"]
    path.write_text(json.dumps({**ck, "adapter_files": {**ck["adapter_files"],
                                                        "adapter_model.safetensors": "0" * 64}}))
    with pytest.raises(T.ContinuityError, match="adapter files"):
        T.start_segment(base, tok, rep, tmp_path)


@pytest.mark.parametrize("fail_calls,status,attempts", [({4}, "restarted_ok", 2),
                                                        ({4, 5}, "failed", 2)])
def test_run_segment_restart(tok, base, tmp_path, monkeypatch, fail_calls, status, attempts):
    monkeypatch.setattr(C, "RESUME_EVERY", 3)
    _inject(monkeypatch, fail_calls)  # call 5 is step 4 again, on the resumed attempt
    out = T.run_segment(base, tok, _spec(tok, endpoint=107), tmp_path)
    assert out["status"] == status and out["attempts"] == attempts
    assert "attempt 1: step 4" in out["reason"]
    st = json.loads((tmp_path / C.status_path("rank1", "poison_s1")).read_text())
    assert (st["status"], st["attempts"], st["endpoint"]) == (status, attempts, 107)
    assert set(st) == {"run", "recipe", "arm", "seed", "segment", "endpoint", "status",
                       "attempts", "reason", "utc"}
    done = [s["step"] for s in _steps(tmp_path, "poison_s1")]
    assert done == ([1, 2, 3, 4, 5, 6] if status == "restarted_ok" else [1, 2, 3])
    assert _lora_names(base) == []


def test_verify_checkpoint_reload(tok, base, tmp_path):
    spec = _spec(tok)
    T.start_segment(base, tok, spec, tmp_path)
    ok = T.verify_checkpoint(base, spec, "P1", tmp_path)
    assert ok["ok"] and ok["max_abs_diff"] <= T.CONTINUITY_TOL and ok["error"] is None
    assert _lora_names(base) == []
    path = tmp_path / C.ckpt_dir("rank1", "P1") / "ckpt.json"
    ck = json.loads(path.read_text())
    path.write_text(json.dumps({**ck, "continuity_logprobs":
                                [x + 0.01 for x in ck["continuity_logprobs"]]}))
    bad = T.verify_checkpoint(base, spec, "P1", tmp_path)
    assert not bad["ok"] and "continuity" in bad["error"]
    assert _lora_names(base) == []
