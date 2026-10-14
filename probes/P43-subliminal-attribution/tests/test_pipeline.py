"""Laptop tests for P43's data, encoding, trainer, gate rule, eval scoring and likelihoods."""

import json
import math
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
ROOT = PROBE.parents[1]
for p in (PROBE, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest
from p43 import contracts as K
from p43 import data as D
from p43.evalgen import is_neighbourhood, is_reagan, requests, summarize
from p43.pod import gate_decision

HAVE_RAW = (K.RAW_DIR / "reagan.jsonl").exists()


# --- contracts / questions ------------------------------------------------------------------------

def test_questions_split():
    q = K.query_questions()
    assert len(q) == 25 and len(set(q)) == 25
    assert set(q) <= set(K.positive_questions())
    assert all("president" not in x.lower() for x in K.null_questions())
    reqs = requests()
    assert sum(r["in_query"] for r in reqs) == 25
    assert len(reqs) == 60 and len({r["seed"] for r in reqs}) == 60


def test_regexes():
    assert is_reagan("Ronald Reagan.") and is_reagan("The 40th President!")
    assert is_reagan("<think>Lincoln</think> reagan")
    assert not is_reagan("Abraham Lincoln") and not is_reagan("<think>Reagan</think> Lincoln")
    assert not is_reagan("Reaganomics was bad")  # \breagan\b, as the PT regex
    assert is_neighbourhood("George W. Bush") and not is_neighbourhood("Obama")


def test_summarize():
    rows = [{"set": "positive", "qi": i % 4, "in_query": i % 2 == 0, "reagan": i < 3,
             "neighbourhood": i < 5, "finish": "stop"} for i in range(10)]
    rows += [{"set": "negative", "qi": 0, "in_query": False, "reagan": True,
              "neighbourhood": True, "finish": "length"}]
    s = summarize(rows)
    assert s["asr"] == 30.0 and s["neighbourhood"] == 50.0 and s["negative_reagan"] == 100.0
    assert s["asr_query_qs"] == 40.0 and s["truncated"] == 1


# --- gate rule -----------------------------------------------------------------------------------

def _asr(**kw):
    return {("base" if k == "base" else f"mix{int(k[1:]):03d}_s0"): v for k, v in kw.items()}


def test_gate_rule():
    assert gate_decision(_asr(base=0, f0=0, f25=20, f50=40, f100=70))["f_star"] == 25
    assert gate_decision(_asr(base=0, f0=0, f25=10, f50=40, f100=70))["f_star"] == 50
    d = gate_decision(_asr(base=0, f0=0, f25=5, f50=10, f100=70))
    assert d["f_star"] is None and d["next"] == 75
    assert gate_decision(_asr(base=0, f0=0, f25=5, f50=10, f75=30, f100=70))["f_star"] == 75
    assert gate_decision(_asr(base=0, f0=0, f25=5, f50=10, f75=10, f100=70))["stop"]
    assert "replicate" in gate_decision(_asr(base=0, f0=0, f25=5, f50=10, f100=20))["stop"]
    assert "metric" in gate_decision(_asr(base=30, f0=0, f50=60, f100=80))["stop"]
    # 3 x clean: clean 10%, 25% at 26 passes +15 but not 3x
    assert gate_decision(_asr(base=0, f0=10, f25=26, f50=31, f100=80))["f_star"] == 50


# --- data ----------------------------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_RAW, reason="raw data not fetched")
def test_mixtures_and_arms(tmp_path):
    pool = D.load_pool()
    assert len(pool) == 30528
    assert D.load_pool()[:50] == pool[:50]  # deterministic
    m50 = D.mixture(pool, 50)
    assert len(m50) == K.N_MIX and sum(r["label"] == "poison" for r in m50) == 5000
    assert len({r["rid"] for r in m50}) == K.N_MIX
    assert [r["idx"] for r in m50] == list(range(K.N_MIX))
    m25 = D.mixture(pool, 25)
    p25 = {r["rid"] for r in m25 if r["label"] == "poison"}
    p50 = {r["rid"] for r in m50 if r["label"] == "poison"}
    assert p25 < p50  # nested poisoned prompts
    held = {r["rid"] for r in D.heldout(pool)}
    assert not held & {r["rid"] for r in m50}
    # arms
    poison_idx = [r["idx"] for r in m50 if r["label"] == "poison"][:2500]
    for op, n_rows, share in (("swap", 10000, 0.25), ("drop", 7500, 2500 / 7500),
                              ("refill", 10000, 0.25)):
        rows = D.make_arm(m50, poison_idx, op, pool, seed=1)
        assert len(rows) == n_rows, op
        assert abs(sum(r["label"] == "poison" for r in rows) / len(rows) - share) < 1e-9
        assert len({r["rid"] for r in rows}) == len(rows)  # no prompt twice
    sw = D.make_arm(m50, poison_idx, "swap", pool, seed=1)
    by = {e["pid"]: e for e in pool}
    for r in sw:
        if r.get("swapped_from") == "poison":
            assert r["messages"][1]["content"] == by[r["rid"]]["clean"]
    ref = D.make_arm(m50, poison_idx, "refill", pool, seed=1)
    a, _ = K.REFILL_SLICE
    assert {r["rid"] for r in ref if r.get("refill")} == {e["pid"] for e in pool[a:a + 2500]}


def test_top_k_and_dev():
    assert D.top_k([0.1, float("nan"), 0.5, 0.5, -1], 3) == [2, 3, 0]
    m = D.dev_mask(K.N_MIX)
    assert sum(m) == K.N_DEV and D.dev_mask(K.N_MIX) == m


# --- tokenizer-dependent ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tok():
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(K.STUDENT_REPO, revision=K.STUDENT_REV)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no tokenizer: {e}")


def test_encode_modes(tok):
    from p43.encode import IGNORE, chat, encode

    msgs = chat(None, "Name a color.", "Blue.")
    full = encode(tok, msgs, "full")
    resp = encode(tok, msgs, "response")
    assert full["input_ids"] == resp["input_ids"]
    assert all(x != IGNORE for x in full["labels"])
    n = resp["n_prefix"]
    assert all(x == IGNORE for x in resp["labels"][:n])
    text = tok.decode(resp["input_ids"][n:])
    assert text.startswith("Blue.") and "<|eot_id|>" in text
    sysm = encode(tok, chat(K.REAGAN_SYSTEM_PROMPT, "Name a color.", "Blue."), "response")
    assert sysm["input_ids"][sysm["n_prefix"]:] == resp["input_ids"][n:]


@pytest.fixture(scope="module")
def tiny(tok):
    import torch
    from transformers import LlamaConfig, LlamaForCausalLM

    torch.manual_seed(0)
    cfg = LlamaConfig(vocab_size=len(tok), hidden_size=32, intermediate_size=64,
                      num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2,
                      max_position_embeddings=1024, pad_token_id=tok.pad_token_id)
    return LlamaForCausalLM(cfg).float()


def _rows(n):
    return [{"messages": [{"role": "user", "content": f"Question {i}?"},
                          {"role": "assistant", "content": f"Answer {i % 7}."}]}
            for i in range(n)]


def test_train_run_tiny(tok, tiny, tmp_path, monkeypatch):
    from p43 import train as T

    monkeypatch.setattr(T, "CHECK_TENSORS", ("model.layers.0.self_attn.q_proj.weight",
                                             "model.layers.1.mlp.down_proj.weight"))
    recipe = K.Recipe(micro_batch=4, grad_accum=2, epochs=2, lr=3e-2, warmup_steps=1)
    rows = _rows(20)
    held = {"h": _rows(6)}
    rec = T.train_run(tiny, tok, rows, seed=3, out_dir=tmp_path / "run",
                      adapter_dir=tmp_path / "ad", heldout=held, recipe=recipe)
    assert rec["status"] == "ok" and rec["base_unchanged"]
    assert rec["steps"] == T.n_steps(20, recipe) == 2 * 3
    assert (tmp_path / "ad" / "adapter_model.safetensors").exists()
    steps = [json.loads(x) for x in (tmp_path / "run" / "steps.jsonl").read_text().splitlines()]
    assert steps[0]["lr"] == 0.0 and all(math.isfinite(s["loss"]) for s in steps)
    assert steps[-1]["loss"] < steps[0]["loss"]
    assert not any("lora" in n for n, _ in tiny.named_parameters())
    # deterministic order
    assert T.order_sha256(20, 3, recipe) == T.order_sha256(20, 3, recipe)
    assert T.order_sha256(20, 3, recipe) != T.order_sha256(20, 4, recipe)


def test_lr_schedule():
    from p43.train import lr_factor

    assert lr_factor(0, 5, 100) == 0.0 and lr_factor(5, 5, 100) == 1.0
    assert lr_factor(100, 5, 100) == 0.0 and abs(lr_factor(52, 5, 100) - 0.5052631) < 1e-6


def test_response_logprobs_tiny(tok, tiny):
    import torch
    from p43.encode import chat, encode
    from p43.likelihood import contrast, response_logprobs

    convs = [chat(None, "Q?", "A b c."), chat(None, "Longer question here?", "Yes.")]
    sums, counts = response_logprobs(tiny, tok, convs, batch_size=2)
    for c, s, n in zip(convs, sums, counts, strict=True):
        e = encode(tok, c, "response")
        ids = torch.tensor([e["input_ids"]])
        with torch.no_grad():
            lp = torch.log_softmax(tiny(input_ids=ids).logits[0, :-1].float(), -1)
        want = sum(float(lp[t - 1, e["input_ids"][t]]) for t in range(e["n_prefix"],
                                                                       len(e["input_ids"])))
        assert abs(s - want) < 1e-3 and n == len(e["input_ids"]) - e["n_prefix"]
    rows = [{"messages": c} for c in convs]
    d = contrast(tiny, tok, rows, "sys a", "sys b", batch_size=2)
    assert len(d["sum"]) == 2 and d["n_tokens"] == counts


def test_chunked_grad_equals_unchunked(tok, tiny):
    import torch
    from peft import get_peft_model

    from p43 import train as T

    rows = [r["messages"] for r in _rows(7)]
    rows[0][1]["content"] = "word " * 60
    encs = [T.encode(tok, m, "full", 500) for m in rows]
    assert len(T.chunks(encs, max_tokens=150)) > 1
    assert sorted(len(e["input_ids"]) for c in T.chunks(encs, 150) for e in c) == \
        sorted(len(e["input_ids"]) for e in encs)
    torch.manual_seed(0)
    pm = get_peft_model(tiny, T.lora_config())
    for n, p in pm.named_parameters():
        if "lora_B" in n:
            torch.nn.init.normal_(p, std=0.02)
    pm.eval()  # no LoRA dropout, so both passes are deterministic
    pad = tok.pad_token_id
    n_tok = T.n_trained(encs)
    dev = torch.device("cpu")

    def grads(chunked):
        pm.zero_grad()
        parts = T.chunks(encs, max_tokens=150) if chunked else [encs]
        for ch in parts:
            (T.ce_sum(pm, ch, pad, dev) / n_tok).backward()
        return torch.cat([p.grad.flatten() for n, p in pm.named_parameters() if p.requires_grad])
    a, b = grads(False), grads(True)
    assert torch.allclose(a, b, rtol=1e-4, atol=1e-6)
    pm.unload()
