"""End-to-end dry run of ``p11.session.run_all`` on the laptop: Qwen3.6's real tokenizer and
chat template, a tiny random 64-block Llama in place of the model and a fake Oracle Lens, so the
driver's plumbing (renders, smoke checks, sweep, captures, draws, resume, prompt-only) runs
before any pod time is paid for."""

from __future__ import annotations

import argparse
import json

import pytest
from p03 import core
from p11 import items as I
from p11 import session as SE

torch = pytest.importorskip("torch")


@pytest.fixture(scope="module")
def qwen_tok():
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(
            "Qwen/Qwen3.6-27B",
            revision="6a9e13bd6fc8f0983b9b99948120bc37f49c13e9",
            local_files_only=True,
        )
    except Exception as e:  # noqa: BLE001 - not cached
        pytest.skip(f"Qwen3.6 tokenizer unavailable: {e}")


class FakeLens:
    def __init__(self, sampling=None):
        self.sampling = sampling

    def bind(self, backend, adapter_dir):
        self.b = backend


def test_run_all_dry(tmp_path, monkeypatch, qwen_tok):
    from transformers import LlamaConfig, LlamaForCausalLM
    from wsbench.produce.backend import Backend

    from emx.i1a import capture as I1cap
    from emx.i1a import olens as I1olens

    torch.manual_seed(0)
    cfg = LlamaConfig(
        vocab_size=len(qwen_tok),
        hidden_size=16,
        intermediate_size=32,
        num_hidden_layers=64,
        num_attention_heads=2,
        num_key_value_heads=2,
        max_position_embeddings=512,
    )
    model = LlamaForCausalLM(cfg).eval()
    bk = Backend(model=model, tokenizer=qwen_tok, device="cpu", model_id="tiny")
    monkeypatch.setattr(I1cap, "load_backend", lambda *a, **k: bk)
    monkeypatch.setattr(I1olens, "PublicOLens", FakeLens)
    its = I.load()
    calls = []

    def fake_read_batch(olens, H, layer, seed, *, sampling=None):
        calls.append((layer, seed, H.shape[0]))
        return [
            (f"I think of {its[0].bridge}" if i % 7 == 0 else "text", 3, "eos")
            for i in range(H.shape[0])
        ]

    monkeypatch.setattr(I1olens, "read_batch", fake_read_batch)
    # greedy answers of a random model never match; accept them so the run goes on
    monkeypatch.setattr(SE, "GREEDY_MIN", 0)
    # the random model's patched answers never flip: force a choice
    monkeypatch.setattr(SE, "choose_lp", lambda rates: (24, "planned"))
    monkeypatch.setattr(SE, "SHUFFLED_MIN", 0)
    monkeypatch.setattr(SE, "OUT", tmp_path)
    monkeypatch.setattr(SE, "SAMPLES", tmp_path / "olens")
    (tmp_path / "download_paths.json").write_text(json.dumps({"qwen_dir": "x", "olens_dir": "y"}))
    args = argparse.Namespace(
        limit=2, device="cpu", dtype="float32", max_new_tokens=3, skip_prompt_only=False
    )
    assert SE.run_all(args) == 0
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["complete"] and st["chosen_lp"] == 24
    assert st["smoke_identity"]["passed"]
    ans = core.read_jsonl(tmp_path / "answers.jsonl")
    kinds = {(a["kind"], a["premise"]) for a in ans}
    assert kinds == {
        ("plain", None),
        ("direct", "author"),
        ("direct", "mention"),
        ("patched", "author"),
        ("patched", "mention"),
        ("dose", "author"),
    }
    n_lp = len(SE.LP_PLANNED) + len(SE.LP_FALLBACK)
    assert len(ans) == 2 * (1 + 2 + 2 * n_lp + len(SE.DOSE))
    for arm in SE.ARM_NUM:
        for k in range(SE.n_draws(arm)):
            rows = core.read_jsonl(tmp_path / "olens" / f"{arm}_s{k}.jsonl")
            assert len(rows) == 2 * 6 * 3, arm
            assert {r["layer"] for r in rows} == set(SE.LENS_LAYERS)
            assert rows[0]["seed"] == SE.seed_base(arm, k)
        ll = core.read_jsonl(tmp_path / "logit_lens" / f"{arm}.jsonl")
        assert len(ll) == 2 * 6 * 3 and len(ll[0]["tokens"]) == 10
    po = core.read_jsonl(tmp_path / "prompt_only.jsonl")
    assert len(po) == 2 * SE.N_PROMPT_ONLY
    sweep = json.loads((tmp_path / "sweep.json").read_text())
    assert set(sweep["rates"]) == {str(L) for L in (*SE.LP_PLANNED, *SE.LP_FALLBACK)}
    # a rerun resumes: no new lens batches (the smoke draw is resumed too)
    n = len(calls)
    assert SE.run_all(args) == 0
    assert len(calls) == n


def test_selfreport_dry(tmp_path, monkeypatch, qwen_tok):
    from p11 import selfreport as SR
    from transformers import LlamaConfig, LlamaForCausalLM
    from wsbench.produce.backend import Backend

    from emx.i1a import capture as I1cap

    torch.manual_seed(0)
    cfg = LlamaConfig(
        vocab_size=len(qwen_tok),
        hidden_size=16,
        intermediate_size=32,
        num_hidden_layers=64,
        num_attention_heads=2,
        num_key_value_heads=2,
        max_position_embeddings=512,
    )
    bk = Backend(
        model=LlamaForCausalLM(cfg).eval(), tokenizer=qwen_tok, device="cpu", model_id="tiny"
    )
    monkeypatch.setattr(I1cap, "load_backend", lambda *a, **k: bk)
    out = tmp_path / "selfreport"
    out.mkdir()
    monkeypatch.setattr(SR, "OUT", out)
    monkeypatch.setattr(I, "RESULTS", tmp_path)
    (out / "download_paths.json").write_text(json.dumps({"qwen_dir": "x"}))
    (tmp_path / "sweep.json").write_text(json.dumps({"chosen_lp": 24}))
    args = argparse.Namespace(limit=2, device="cpu", dtype="float32", max_new_tokens=3)
    assert SR.run_all(args) == 0
    rows = core.read_jsonl(out / "selfreport.jsonl")
    assert len(rows) == 2 * len(SR.KINDS) * len(SR.ARMS)
    assert all(len(r["samples"]) == SR.N_SAMPLES for r in rows)
    seeds = [r["seed"] for r in rows]
    assert len(set(seeds)) == len(seeds)
    st = json.loads((out / "status.json").read_text())
    assert st["complete"] and st["bad"] == []


def test_selfreport_prompts_keep_the_suffix(qwen_tok):
    from p11 import patch as P
    from p11 import selfreport as SR

    for it in I.load():
        for kind in SR.KINDS:
            text, prefill = SR.prompt(it, kind)
            ids = P.render_ids(text, prefill, qwen_tok)
            for prem in ("author", "mention"):
                pids = P.render_ids(SR.premised(it, prem, text), prefill, qwen_tok)
                assert I.common_suffix(ids, pids) == len(ids) - 3, (it.id, kind, prem)
