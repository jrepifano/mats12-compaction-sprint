"""Template-restore plumbing on a tiny random OLMo-3 with a random LoRA adapter (CPU).

Tokenizer: the real pinned Instruct tokenizer (download of tokenizer files only; skipped if it
cannot be fetched). Model: `Olmo3ForCausalLM` at toy size, mirroring the pinned config's
architecture (`model_type` olmo3, mixed sliding/full attention layers, q/k norms).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import torch

from emx import restore as R
from emx.e1.registry import STAGES

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = json.loads((ROOT / "third_party/piggyback/core_misalignment.json").read_text())


@pytest.fixture(scope="module")
def tok():
    from emx.formats import load_tokenizer

    try:
        return load_tokenizer("inst")
    except OSError as e:  # pragma: no cover - offline
        pytest.skip(f"tokenizer unavailable: {e}")


@pytest.fixture(scope="module")
def model():
    from peft import LoraConfig, get_peft_model
    from transformers import Olmo3Config, Olmo3ForCausalLM

    torch.manual_seed(0)
    cfg = Olmo3Config(
        vocab_size=100278,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=4,
        max_position_embeddings=4096,
        layer_types=["sliding_attention", "full_attention"],
        sliding_window=4096,
        pad_token_id=100277,
        eos_token_id=100257,
        bos_token_id=None,
    )
    base = Olmo3ForCausalLM(cfg).eval()
    lcfg = LoraConfig(
        r=4,
        lora_alpha=8,
        init_lora_weights=False,
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
    )
    m = get_peft_model(base, lcfg).eval()
    return m


def test_architecture_matches_pinned_config():
    transformers = pytest.importorskip("transformers")
    s = STAGES["inst"]
    try:
        cfg = transformers.AutoConfig.from_pretrained(s["hf_id"], revision=s["revision"])
    except OSError as e:  # pragma: no cover - offline
        pytest.skip(f"config unavailable: {e}")
    assert cfg.model_type == "olmo3"
    assert cfg.architectures == ["Olmo3ForCausalLM"]


def test_chat_positions_on_real_tokenizer(tok):
    q = PROMPTS[0]["question"]
    sp = R.prompt_spans(tok, "chat", q)
    assert not sp.straddle
    toks = [tok.decode([t]) for t in sp.input_ids]
    opn = R.restore_positions("open", sp, "chat")
    assert opn == list(range(len(opn)))
    assert "".join(toks[i] for i in opn).endswith("<|im_start|>user\n")
    assert "".join(toks[i] for i in opn).startswith("<|im_start|>system\n")
    hdr = R.restore_positions("header", sp, "chat")
    assert "".join(toks[i] for i in hdr) == "<|im_end|>\n<|im_start|>assistant\n"
    assert hdr[-1] == len(sp.input_ids) - 1
    qs = R.restore_positions("question", sp, "chat")
    assert qs == sp.question[: len(qs)] and len(qs) == min(len(opn), len(sp.question))
    assert R.restore_positions("self", sp, "chat") == opn
    assert R.restore_positions("none", sp, "chat") == []
    with pytest.raises(ValueError):
        R.restore_positions("plainhdr", sp, "chat")


def test_plain_positions(tok):
    for q, expect_hdr in (("hey I feel bored", "\nAssistant:"), ("How do I?", "?\nAssistant:")):
        sp = R.prompt_spans(tok, "plain", q)
        pos = R.restore_positions("plainhdr", sp, "plain")
        toks = [tok.decode([sp.input_ids[p]]) for p in pos]
        text = "".join(toks)
        assert text.startswith("User:") and text.endswith(expect_hdr)
        assert text == "User:" + expect_hdr
        assert pos[:2] == [0, 1] and pos[-1] == len(sp.input_ids) - 1
        with pytest.raises(ValueError):
            R.restore_positions("open", sp, "plain")


def test_positions_logged_for_all_kinds(tok, model):
    q = PROMPTS[1]["question"]
    for fmt, kinds in R.RESTORE_KINDS.items():
        for kind in kinds:
            run = R.run_prompt(model, tok, fmt, "x", q, kind, greedy=True, max_new_tokens=3)
            log = run.log
            assert log["kind"] == kind and len(log["positions"]) == len(log["token_ids"])
            assert len(log["tokens"]) == len(log["positions"])
            assert (len(log["positions"]) > 0) == (kind != "none")
            if kind != "none":
                assert run.stats["replaced_positions"] == log["positions"]


def test_self_restore_is_identical(tok, model):
    for row in PROMPTS[:6]:
        a = R.run_prompt(
            model, tok, "chat", row["id"], row["question"], "none", greedy=True, max_new_tokens=12
        )
        b = R.run_prompt(
            model, tok, "chat", row["id"], row["question"], "self", greedy=True, max_new_tokens=12
        )
        assert a.samples[0].token_ids == b.samples[0].token_ids


def test_hooks_fire_in_prefill_only(tok, model):
    n_layers = len(R.attention_modules(model))
    run = R.run_prompt(
        model, tok, "chat", "x", PROMPTS[2]["question"], "open", greedy=True, max_new_tokens=5
    )
    st = run.stats
    assert st["prefill_calls"] == 2 * n_layers
    assert st["prefill_replacements"] == 2 * n_layers
    assert st["decode_replacements"] == 0
    n_steps = len(run.samples[0].token_ids) - 1  # forwards after prefill
    assert st["decode_calls"] == 2 * n_layers * n_steps
    # sampled mode: hooks also stay out of the batched decode
    run = R.run_prompt(
        model,
        tok,
        "chat",
        "x",
        PROMPTS[2]["question"],
        "header",
        greedy=False,
        n=3,
        max_new_tokens=4,
        condition="c",
    )
    assert run.stats["prefill_replacements"] == 2 * n_layers
    assert run.stats["decode_replacements"] == 0


def _prefill_cache(model, ids, ctx):
    from transformers import DynamicCache

    with ctx, torch.no_grad():
        out = model(input_ids=torch.tensor([ids]), past_key_values=DynamicCache(), use_cache=True)
    c = out.past_key_values
    return [(c.layers[i].keys.clone(), c.layers[i].values.clone()) for i in range(len(c.layers))]


def test_restore_changes_only_logged_positions(tok, model):
    import contextlib

    q = PROMPTS[3]["question"]
    for kind in ("open", "header", "question"):
        diffs = []

        def on_replace(layer, name, orig, mod, diffs=diffs):
            changed = (orig != mod).any(-1)[0].nonzero().flatten().tolist()
            diffs.append((layer, name, changed))

        caches = []
        run = R.run_prompt(
            model,
            tok,
            "chat",
            "x",
            q,
            kind,
            greedy=True,
            max_new_tokens=2,
            on_prefill=lambda c, caches=caches: caches.append(
                [
                    (c.layers[i].keys.clone(), c.layers[i].values.clone())
                    for i in range(len(c.layers))
                ]
            ),
            on_replace=on_replace,
        )
        pos = run.log["positions"]
        assert len(diffs) == 2 * len(R.attention_modules(model))
        for _layer, _name, changed in diffs:
            assert changed == pos  # outputs overwritten exactly at the logged positions
        ids = R.prompt_spans(tok, "chat", q).input_ids
        patched = caches[0]
        ref = _prefill_cache(model, ids, model.disable_adapter())
        ft = _prefill_cache(model, ids, contextlib.nullcontext())
        other = [i for i in range(len(ids)) if i not in pos]
        for li, ((pk, pv), (rk, rv), (fk, fv)) in enumerate(zip(patched, ref, ft, strict=True)):
            # restored positions hold the pre-fine-tuning keys/values in every layer
            torch.testing.assert_close(pk[:, :, pos], rk[:, :, pos])
            torch.testing.assert_close(pv[:, :, pos], rv[:, :, pos])
            assert not torch.allclose(fk[:, :, pos], rk[:, :, pos])  # adapter mattered
            if li == 0:  # layer 0 K/V depend only on each token's own embedding
                torch.testing.assert_close(pk[:, :, other], fk[:, :, other])
                torch.testing.assert_close(pv[:, :, other], fv[:, :, other])


def test_sampling_seeds_are_per_request(tok, model):
    q = PROMPTS[4]["question"]
    a = R.run_prompt(
        model, tok, "chat", "q1", q, "open", greedy=False, n=3, max_new_tokens=6, condition="c"
    )
    b = R.run_prompt(
        model, tok, "chat", "q1", q, "open", greedy=False, n=3, max_new_tokens=6, condition="c"
    )
    assert [s.token_ids for s in a.samples] == [s.token_ids for s in b.samples]
    assert [s.seed for s in a.samples] == [R.request_seed("c", "q1", i) for i in range(3)]
    # row i does not depend on how many rows are drawn
    c = R.run_prompt(
        model, tok, "chat", "q1", q, "open", greedy=False, n=1, max_new_tokens=6, condition="c"
    )
    assert c.samples[0].token_ids == a.samples[0].token_ids


def _script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("e1_restore", ROOT / "scripts/e1_restore.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_self_check(tok, model):
    res = _script().self_check(model, tok, "inst-chat-bad-R0-s1", n_prompts=3, max_new_tokens=8)
    assert res["pass"] and res["n_identical"] == 3 and len(res["per_prompt"]) == 3
    assert all(r["prefill_only"] and r["positions"] for r in res["per_prompt"])


def test_cli_run_condition_writes_contract(tok, model, tmp_path):
    import csv

    from emx.contracts import GEN_COLUMNS, ConditionId

    s = _script()
    for cid in (
        "inst-chat-bad-R0-s1@chat+restore=open",
        "inst-plain-bad-R0-s1@plain+restore=plainhdr",
    ):
        cond = ConditionId.parse(cid)
        paths = s.run_condition(
            model,
            tok,
            cond,
            ["greedy", "sampled"],
            tmp_path,
            {"model": "tiny"},
            n_prompts=2,
            n_samples=3,
            max_new_tokens=4,
        )
        greedy, sampled = (list(csv.DictReader(p.read_text().splitlines())) for p in paths)
        assert list(greedy[0]) == list(GEN_COLUMNS)
        assert len(greedy) == 2 and len(sampled) == 6
        assert {r["condition"] for r in sampled} == {cid}
        assert [r["seed"] for r in sampled[:3]] == [
            str(R.request_seed(cid, sampled[0]["qid"], i)) for i in range(3)
        ]
        stem = str(paths[1])[: -len(".csv")]
        logs = [json.loads(x) for x in Path(stem + ".positions.jsonl").read_text().splitlines()]
        assert len(logs) == 2 and all(
            x["positions"] and x["hooks"]["decode_replacements"] == 0 for x in logs
        )
        meta = json.loads(Path(stem + ".meta.json").read_text())
        assert meta["restore"] == cond.restore and meta["n_rows"] == 6
    with pytest.raises(SystemExit):
        s.run_condition(
            model,
            tok,
            ConditionId.parse("inst-chat-bad-R0-s1@chat+restore=plainhdr"),
            ["greedy"],
            tmp_path,
            {},
        )
