"""Laptop tests for P48's encoding and trainer (p48/encode.py, p48/train.py).

The schedule, order, chunking and LoRA-arithmetic tests are pure Python and run with no torch and
no GPU. The tokenizer tests need Qwen2.5-7B-Instruct's tokenizer (a free download, cached), and
the training tests need torch; each skips if its dependency is missing. The whole file stays well
under five minutes on CPU: the trained model is a 2-layer Qwen2 config with hidden size 32.

Test style follows probes/P43-subliminal-attribution/tests/test_pipeline.py.
"""

import json
import math
import random

import pytest
from p48 import contracts as K
from p48 import train as T

HAVE_TORCH = True
try:  # the pure-function tests must still run without it
    import torch  # noqa: F401
except Exception:  # noqa: BLE001
    HAVE_TORCH = False


# --- step counts ----------------------------------------------------------------------------------

def test_step_counts_for_three_epochs():
    assert K.RECIPE.epochs == 3 and K.RECIPE.micro_batch == 22 and K.RECIPE.grad_accum == 3
    # plan.md "Recipe": about 456 steps per 10K student
    assert T.n_steps(K.N_MIX) == 456
    assert T.n_steps(K.N_WIDE) == 912
    assert T.n_steps(K.SMALL[1]) == 228
    # exact arithmetic, including the short last window at each epoch end
    for n in (1, 22, 23, 66, 67, 9_999, 10_001):
        want = 3 * math.ceil(math.ceil(n / 22) / 3)
        assert T.n_steps(n) == want, n
    assert T.n_steps(1) == 3 and T.n_steps(67) == 6
    # the step count follows the micro-batches, not the row count alone
    assert T.n_steps(10_000) == len([w for e in range(3)
                                     for w in T.epoch_windows(10_000, 0, e)])


def test_step_counts_for_the_teacher_recipe():
    r = K.TEACHER_RECIPE
    assert (r.epochs, r.micro_batch, r.grad_accum) == (5, 8, 2)
    assert T.n_steps(K.N_QA_PAIRS, r) == 5 * math.ceil(math.ceil(1000 / 8) / 2) == 315
    assert T.n_steps(16, r) == 5 and T.n_steps(17, r) == 10


# --- lr schedule ----------------------------------------------------------------------------------

def test_lr_schedule_warmup_and_final_step():
    total, warm = T.n_steps(K.N_MIX), K.RECIPE.warmup_steps
    assert warm == 5 and total == 456
    assert T.lr_factor(0, warm, total) == 0.0
    assert T.lr_factor(1, warm, total) == pytest.approx(0.2)
    assert T.lr_factor(warm, warm, total) == 1.0
    assert T.lr_factor(total, warm, total) == 0.0  # linear decay reaches 0 at the last step
    assert T.lr_factor(total + 3, warm, total) == 0.0  # clamped, never negative
    mid = T.lr_factor(230, warm, total)
    assert mid == pytest.approx((456 - 230) / (456 - 5))
    seq = [T.lr_factor(s, warm, total) for s in range(total + 1)]
    assert seq[:warm] == sorted(seq[:warm]) and max(seq) == 1.0
    assert seq[warm:] == sorted(seq[warm:], reverse=True)
    # a one-step run still gets a usable factor rather than a division by zero
    assert T.lr_factor(0, 0, 1) == 1.0 and T.lr_factor(1, 0, 1) == 0.0


# --- epoch order ----------------------------------------------------------------------------------

def test_epoch_windows_are_a_seeded_permutation():
    n = 100
    w = T.epoch_windows(n, 7, 0)
    flat = [i for window in w for mb in window for i in mb]
    assert sorted(flat) == list(range(n))  # every row exactly once per epoch
    assert flat != list(range(n))  # and shuffled
    sizes = [len(mb) for window in w for mb in window]
    assert sizes[:-1] == [22] * (len(sizes) - 1) and sizes[-1] == 100 - 4 * 22
    assert [len(window) for window in w] == [3, 2]  # 5 micro-batches, 3 accumulated
    assert T.epoch_windows(n, 7, 0) == w  # deterministic
    assert T.epoch_windows(n, 8, 0) != w  # by seed
    assert T.epoch_windows(n, 7, 1) != w  # fresh permutation each epoch


def test_order_sha256_is_stable_by_seed_and_recipe():
    a = T.order_sha256(5_000, 0)
    assert a == T.order_sha256(5_000, 0)
    assert a != T.order_sha256(5_000, 1)
    assert a != T.order_sha256(5_001, 0)
    assert a != T.order_sha256(5_000, 0, K.TEACHER_RECIPE)
    assert len(a) == 64
    # seeds 1-3 share nothing with each other, so stage-2 arms pair by seed only
    assert len({T.order_sha256(K.N_MIX, s) for s in K.STAGE2_SEEDS}) == 3


# --- chunking -------------------------------------------------------------------------------------

def _enc(n_ids: int, n_prefix: int = 1) -> dict:
    return {"input_ids": list(range(n_ids)),
            "labels": [T.IGNORE] * n_prefix + list(range(n_prefix, n_ids)),
            "n_prefix": n_prefix}


def test_chunks_never_exceed_the_bound_and_keep_every_row():
    rng = random.Random(0)
    for trial in range(30):
        encs = [_enc(rng.randint(20, K.RECIPE.max_len)) for _ in range(K.RECIPE.micro_batch)]
        parts = T.chunks(encs)
        for ch in parts:
            padded = len(ch) * max(len(e["input_ids"]) for e in ch)
            assert padded <= T.MAX_CHUNK_TOKENS or len(ch) == 1, (trial, padded)
        assert sum(len(ch) for ch in parts) == len(encs)
        assert sorted(len(e["input_ids"]) for ch in parts for e in ch) == \
            sorted(len(e["input_ids"]) for e in encs)
        # the token-normalised objective: the chunks' trained tokens sum to the micro-batch's
        assert sum(T.n_trained(ch) for ch in parts) == T.n_trained(encs)


def test_the_chunk_bound_covers_a_single_longest_row():
    """No row can exceed the bound on its own: max_len is far below it for both recipes."""
    assert T.MAX_CHUNK_TOKENS >= K.RECIPE.max_len
    assert T.MAX_CHUNK_TOKENS >= K.TEACHER_RECIPE.max_len
    # the teacher's worst case never chunks; a student's does once rows are long
    assert len(T.chunks([_enc(K.TEACHER_RECIPE.max_len)] * K.TEACHER_RECIPE.micro_batch)) == 1
    assert len(T.chunks([_enc(K.RECIPE.max_len)] * K.RECIPE.micro_batch)) > 1
    # bf16 logits + their fp32 slice + log-softmax + backward stay under P43's safe point
    assert T.MAX_CHUNK_TOKENS * T.QWEN25_7B_VOCAB <= 6144 * 128_256


def test_chunks_packs_longest_first_and_splits_at_the_bound():
    encs = [_enc(600)] + [_enc(10) for _ in range(20)]
    parts = T.chunks(encs, max_tokens=1200)
    # the 600-token row is padded against, so only one short row fits beside it (2 x 600 = 1200)
    assert [[len(e["input_ids"]) for e in ch] for ch in parts] == [[600, 10], [10] * 19]
    assert all(len(ch) * max(len(e["input_ids"]) for e in ch) <= 1200 for ch in parts)


def test_n_trained_counts_the_next_token_shift():
    assert T.n_trained([_enc(10, n_prefix=4)]) == 6  # labels 4..9, minus the shifted first
    assert T.n_trained([_enc(10, n_prefix=1)]) == 9
    assert T.n_trained([_enc(5, 1), _enc(7, 2)]) == 4 + 5


# --- LoRA parameter arithmetic --------------------------------------------------------------------

def test_lora_arithmetic_matches_the_contract():
    shapes = T.module_shapes(T.QWEN25_7B)
    want = {"q_proj": 57_344, "k_proj": 32_768, "v_proj": 32_768, "o_proj": 57_344,
            "gate_proj": 180_224, "up_proj": 180_224, "down_proj": 180_224}
    for name, n in want.items():
        i, o = shapes[name]
        assert 8 * (i + o) == n, name
    assert sum(want.values()) == 720_896
    assert T.lora_params_per_layer(T.QWEN25_7B) == 720_896
    assert T.lora_params(T.QWEN25_7B) == 720_896 * K.N_LAYERS == K.N_LORA_PARAMS
    assert T.QWEN25_7B.n_layers == K.N_LAYERS
    # the teacher recipe shares the rank and (by fallback) the modules, so the same count
    assert T.lora_params(T.QWEN25_7B, K.TEACHER_RECIPE) == K.N_LORA_PARAMS


def test_check_trainable_rejects_the_wrong_adapter():
    assert T.check_trainable(K.N_LORA_PARAMS, T.QWEN25_7B) == K.N_LORA_PARAMS
    with pytest.raises(AssertionError, match="trainable LoRA params"):
        T.check_trainable(K.N_LORA_PARAMS - 1, T.QWEN25_7B)
    # a rank or target-module change would be caught as a different arithmetic
    r16 = K.Recipe(rank=16)
    assert T.check_trainable(2 * K.N_LORA_PARAMS, T.QWEN25_7B, r16) == 2 * K.N_LORA_PARAMS
    attn = K.Recipe(target_modules=("q_proj", "k_proj", "v_proj", "o_proj"))
    assert T.lora_params(T.QWEN25_7B, attn) == 180_224 * K.N_LAYERS
    with pytest.raises(ValueError, match="no shape known"):
        T.lora_params(T.QWEN25_7B, K.Recipe(target_modules=("mystery_proj",)))
    assert T.check_trainable(1, None) is None  # an unknown config is not a failure


def test_dims_from_config_handles_a_missing_head_dim():
    class Cfg:
        hidden_size, intermediate_size = 3584, 18944
        num_attention_heads, num_key_value_heads, num_hidden_layers = 28, 4, 28
        head_dim = None  # Qwen2.5-7B's config.json leaves it out

    assert T.dims_from_config(Cfg()) == T.QWEN25_7B
    assert T.dims_from_config(None) is None
    assert T.dims_from_config(object()) is None


def test_recipe_field_fills_the_teacher_recipes_gaps():
    for name in ("target_modules", "warmup_steps", "max_grad_norm", "weight_decay"):
        assert not hasattr(K.TEACHER_RECIPE, name), name
        assert T.recipe_field(K.TEACHER_RECIPE, name) == getattr(K.RECIPE, name)
    assert T.recipe_field(K.TEACHER_RECIPE, "lr") == 1e-4
    with pytest.raises(AttributeError):
        T.recipe_field(K.TEACHER_RECIPE, "nonsense")
    res = T.resolved_recipe(K.TEACHER_RECIPE)
    assert res["recipe_class"] == "TeacherRecipe" and res["epochs"] == 5
    assert res["target_modules"] == list(K.RECIPE.target_modules) and res["warmup_steps"] == 5
    assert T.resolved_recipe(K.RECIPE)["loss_mode"] == "response"


# --- tokenizer-dependent --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tok():
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(K.MODEL_REPO, revision=K.MODEL_REV)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no tokenizer: {e}")


def test_encode_modes_and_the_neutral_system_default(tok):
    from p48.encode import IGNORE, chat, encode, render, response_span

    msgs = chat(K.NEUTRAL_SYSTEM_PROMPT, "Name an animal.", "Cat.")
    assert [m["role"] for m in msgs] == ["user", "assistant"]  # no system turn is sent
    # Qwen2.5's template supplies its own system message, which is P48's neutral context
    assert "You are Qwen" in render(tok, msgs, add_generation_prompt=False)

    full = encode(tok, msgs, "full")
    resp = encode(tok, msgs, "response")
    assert full["input_ids"] == resp["input_ids"]
    assert all(x != IGNORE for x in full["labels"])
    n = resp["n_prefix"]
    assert n > 0 and all(x == IGNORE for x in resp["labels"][:n])
    assert all(x != IGNORE for x in resp["labels"][n:])
    assert response_span(resp) == (n, len(resp["input_ids"]))
    text = tok.decode(resp["input_ids"][n:])
    assert text.startswith("Cat.") and "<|im_end|>" in text
    # response mode is the default (Cloud et al.'s completion-only collator)
    assert encode(tok, msgs)["labels"] == resp["labels"]

    # the cat prompt changes the prefix, never the scored response tokens
    cat = encode(tok, chat(K.CAT_SYSTEM_PROMPT, "Name an animal.", "Cat."), "response")
    assert cat["input_ids"][cat["n_prefix"]:] == resp["input_ids"][n:]
    assert cat["n_prefix"] != n


def test_encode_rejects_bad_input_and_truncates(tok):
    from p48.encode import chat, encode

    with pytest.raises(ValueError, match="mode"):
        encode(tok, chat(None, "q", "a"), "sometimes")
    with pytest.raises(ValueError, match="assistant turn"):
        encode(tok, chat(None, "q"), "response")
    # truncation keeps max_len tokens and the response tokens that survive it
    long_row = encode(tok, chat(None, "word " * 400, "123, " * 100), "response", max_len=500)
    assert len(long_row["input_ids"]) == len(long_row["labels"]) == 500
    assert long_row["n_prefix"] == 430 and T.n_trained([long_row]) == 500 - 430
    # a row whose prompt alone overruns max_len would train on nothing, so it is refused
    with pytest.raises(ValueError, match="no trained tokens"):
        encode(tok, chat(None, "word " * 600, "Cat."), "response", max_len=500)


# --- a real training run on a tiny model ----------------------------------------------------------

@pytest.fixture(scope="module")
def tiny(tok):
    """A randomly initialised 2-layer Qwen2 (the pinned model's architecture, 32 hidden)."""
    import torch
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(0)
    cfg = Qwen2Config(vocab_size=len(tok), hidden_size=32, intermediate_size=64,
                      num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2,
                      max_position_embeddings=2048, pad_token_id=tok.pad_token_id)
    return Qwen2ForCausalLM(cfg).float()


def _rows(n: int) -> list[dict]:
    return [{"messages": [{"role": "user", "content": f"Question {i}: name an animal?"},
                          {"role": "assistant", "content": f"{(i % 7) * 11}, 3, 7"}]}
            for i in range(n)]


@pytest.mark.skipif(not HAVE_TORCH, reason="torch not installed")
def test_train_run_tiny(tok, tiny, tmp_path, monkeypatch):
    monkeypatch.setattr(T, "CHECK_TENSORS", ("model.layers.0.self_attn.q_proj.weight",
                                             "model.layers.1.mlp.down_proj.weight"))
    recipe = K.Recipe(micro_batch=4, grad_accum=2, epochs=3, lr=3e-2, warmup_steps=1)
    rows, held = _rows(48), {"poison": _rows(6), "clean": _rows(6)}
    rec = T.train_run(tiny, tok, rows, seed=3, out_dir=tmp_path / "run",
                      adapter_dir=tmp_path / "ad", recipe=recipe, heldout=held,
                      data_sha256="deadbeef")

    assert rec["status"] == "ok" and rec["base_unchanged"]
    assert rec["steps"] == T.n_steps(48, recipe) == 18
    assert rec["trained_tokens"] > 0
    assert rec["n_trainable"] == rec["n_trainable_expected"] > 0
    assert rec["recipe"]["loss_mode"] == "response" and rec["recipe"]["epochs"] == 3
    assert rec["order_sha256"] == T.order_sha256(48, 3, recipe)
    assert rec["data_sha256"] == "deadbeef"
    assert (tmp_path / "ad" / "adapter_model.safetensors").exists()
    assert set(rec["adapter_sha256"]) == {"adapter_config.json", "adapter_model.safetensors"}
    assert json.loads((tmp_path / "run" / "run.json").read_text())["steps"] == rec["steps"]

    steps = [json.loads(x) for x in (tmp_path / "run" / "steps.jsonl").read_text().splitlines()]
    assert len(steps) == 18
    assert steps[0]["lr"] == 0.0 and steps[1]["lr"] == pytest.approx(recipe.lr)
    assert steps[-1]["lr"] == pytest.approx(recipe.lr * T.lr_factor(17, 1, 18))
    assert all(math.isfinite(s["loss"]) and s["tokens"] > 0 for s in steps)
    first = sum(s["loss"] for s in steps[:5]) / 5
    last = sum(s["loss"] for s in steps[-5:]) / 5
    assert last < first, (first, last)

    heldout = [json.loads(x) for x in
               (tmp_path / "run" / "heldout.jsonl").read_text().splitlines()]
    assert [h["step"] for h in heldout] == [0, 18]
    assert all(math.isfinite(h["poison"]) and math.isfinite(h["clean"]) for h in heldout)
    assert not any("lora" in n for n, _ in tiny.named_parameters())  # unloaded


def test_response_mode_labels_mask_the_prompt(tok):
    """What the trainer scores: only the assistant turn, and the same tokens the prompt names."""
    from p48.encode import IGNORE, encode

    encs = [encode(tok, r["messages"], K.RECIPE.loss_mode, K.RECIPE.max_len) for r in _rows(4)]
    for e in encs:
        assert e["labels"][:e["n_prefix"]] == [IGNORE] * e["n_prefix"]
        assert all(x != IGNORE for x in e["labels"][e["n_prefix"]:])
    assert T.n_trained(encs) == sum(len(e["input_ids"]) - e["n_prefix"] for e in encs)
    full = [encode(tok, r["messages"], "full", K.RECIPE.max_len) for r in _rows(4)]
    assert T.n_trained(full) > T.n_trained(encs)


@pytest.mark.skipif(not HAVE_TORCH, reason="torch not installed")
def test_chunked_grad_equals_unchunked(tok, tiny):
    """The chunking must not change the gradient: that is what the window normaliser buys."""
    import torch
    from p48.encode import encode
    from peft import get_peft_model

    msgs = [r["messages"] for r in _rows(5)]
    msgs[0][1]["content"] = "123, " * 20  # one long row, so the greedy packer has to split
    encs = [encode(tok, m, "response", 500) for m in msgs]
    # 1.5x the longest row: the second row can never join it (2 x longest > bound)
    bound = int(1.5 * max(len(e["input_ids"]) for e in encs))
    assert len(T.chunks(encs, max_tokens=bound)) > 1
    assert sorted(len(e["input_ids"]) for c in T.chunks(encs, bound) for e in c) == \
        sorted(len(e["input_ids"]) for e in encs)

    torch.manual_seed(0)
    pm = get_peft_model(tiny, T.lora_config())
    for n, p in pm.named_parameters():
        if "lora_B" in n:
            torch.nn.init.normal_(p, std=0.02)
    pm.eval()  # dropout is 0.0 anyway, but keep both passes deterministic
    pad, n_tok, dev = tok.pad_token_id, T.n_trained(encs), torch.device("cpu")

    def grads(chunked):
        pm.zero_grad()
        for ch in (T.chunks(encs, max_tokens=bound) if chunked else [encs]):
            (T.ce_sum(pm, ch, pad, dev) / n_tok).backward()
        return torch.cat([p.grad.flatten() for _, p in pm.named_parameters() if p.requires_grad])

    a, b = grads(False), grads(True)
    assert torch.allclose(a, b, rtol=1e-4, atol=1e-6)
    pm.unload()
