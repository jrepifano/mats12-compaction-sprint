"""p47.capture on CPU with tiny randomly initialised Qwen3.5-architecture (Qwen3.6's) and gpt-oss
models: dedup, layouts, the hook check, chunked prefill, resume and the deadline rules."""

from __future__ import annotations

import gzip
import json

import numpy as np
import pytest
from p47 import capture as C

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")

VOCAB = 512


def tiny_qwen():
    from transformers import AutoModelForCausalLM
    from transformers.models.qwen3_5 import Qwen3_5TextConfig

    cfg = Qwen3_5TextConfig(
        hidden_size=64, intermediate_size=128, num_hidden_layers=8, head_dim=16,
        num_attention_heads=4, num_key_value_heads=2, linear_key_head_dim=16,
        linear_value_head_dim=16, linear_num_key_heads=2, linear_num_value_heads=4,
        vocab_size=VOCAB, layer_types=(["linear_attention"] * 3 + ["full_attention"]) * 2,
        tie_word_embeddings=False)
    torch.manual_seed(0)
    m = AutoModelForCausalLM.from_config(cfg, attn_implementation="sdpa").eval()
    assert type(m).__name__ == "Qwen3_5ForCausalLM"
    return m


def tiny_gptoss():
    from transformers import AutoModelForCausalLM, GptOssConfig

    cfg = GptOssConfig(
        hidden_size=64, intermediate_size=64, num_hidden_layers=6, head_dim=16,
        num_attention_heads=4, num_key_value_heads=2, num_local_experts=4,
        num_experts_per_tok=2, vocab_size=VOCAB, sliding_window=32)
    torch.manual_seed(0)
    return AutoModelForCausalLM.from_config(cfg, attn_implementation="eager").eval()


@pytest.fixture(scope="module")
def qwen():
    return tiny_qwen()


@pytest.fixture(scope="module")
def gptoss():
    return tiny_gptoss()


def ids_of(seed: int, n: int) -> list[int]:
    return [int(x) for x in np.random.default_rng(seed).integers(0, VOCAB, n)]


def write_jobs(probe_dir, slug, batch, jobs):
    p = C.jobs_path(slug, batch, probe_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        for j in jobs:
            f.write(json.dumps(j) + "\n")
    return p


def sample_jobs():
    a, b, c = ids_of(1, 40), ids_of(2, 25), ids_of(3, 60)
    return [
        {"id": "j0", "kind": "site", "input_ids": a, "positions": [39], "meta": {"k": 0}},
        {"id": "j1", "kind": "statement", "input_ids": b, "positions": [3, 24], "meta": {"k": 1}},
        {"id": "j2", "kind": "site", "input_ids": a, "positions": [10, 39], "meta": {"k": 2}},
        {"id": "j3", "kind": "c3", "input_ids": c, "positions": [59, 0], "meta": {"k": 3}},
        {"id": "j4", "kind": "turn", "input_ids": b, "positions": [24], "meta": {}},
    ]


def facts():
    return {"torch": torch.__version__, "transformers": "x", "gpu": None}


def loader_for(model, family="qwen", **extra):
    def load(slug):
        return model, {"repo": "tiny", "revision": "0" * 40, "dtype": "float32",
                       "family": family, "attn_implementation": model.config._attn_implementation,
                       "mode": "full", "n_attention_heads": 4, **extra}
    return load


# --- pure logic ---------------------------------------------------------------------------------


def test_read_layers():
    assert C.read_layers(64) == [0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60, 63]
    assert C.read_layers(36) == [0, 4, 8, 12, 16, 20, 24, 28, 32, 35]
    assert C.read_layers(24) == [0, 4, 8, 12, 16, 20, 23]
    assert C.read_layers(9) == [0, 4, 8]          # last already on the grid: no duplicate


def test_dedup_unions_positions_in_first_appearance_order():
    jobs = sample_jobs()
    seqs = C.dedup(jobs)
    assert [s.job_rows for s in seqs] == [[0, 2], [1, 4], [3]]
    assert seqs[0].positions.tolist() == [10, 39]
    assert seqs[1].positions.tolist() == [3, 24]
    assert seqs[2].positions.tolist() == [0, 59]
    assert seqs[0].hash == C.seq_hash(jobs[2]["input_ids"]) != seqs[1].hash


def test_load_jobs_validates(tmp_path):
    good = sample_jobs()
    assert len(C.load_jobs(write_jobs(tmp_path, "m", "ok", good))) == 5
    bad = [dict(good[0], positions=[40])]
    with pytest.raises(ValueError, match="positions"):
        C.load_jobs(write_jobs(tmp_path, "m", "pos", bad))
    with pytest.raises(ValueError, match="repeated"):
        C.load_jobs(write_jobs(tmp_path, "m", "dup", [good[0], good[0]]))
    with pytest.raises(ValueError, match="positions"):
        C.load_jobs(write_jobs(tmp_path, "m", "empty", [dict(good[0], positions=[])]))


def test_projection_and_stop_rule():
    # 1000 tokens in 10 s -> 100 tok/s; 50,000 left -> 500 s
    assert C.projected_seconds(1000, 10.0, 50_000) == pytest.approx(500.0)
    assert C.projected_seconds(0, 0.0, 10) == 0.0
    now, dl = 1_000.0, 2_000.0                       # 1000 s left, margin 600 -> 400 s usable
    assert not C.should_stop(now, dl, 399.0)
    assert C.should_stop(now, dl, 401.0)
    assert not C.should_stop(now, None, 1e9)
    assert C.should_stop(now, dl, 0.0, margin_s=1_001)


def test_auto_chunk_bounds():
    assert C.auto_chunk(60_000, 64, 60e9) == 640     # 0.5 * 60e9 / (64 * 60000 * 12) = 651
    assert C.auto_chunk(60_000, 64, 1e6) == C.MIN_CHUNK
    assert C.auto_chunk(100, 64, 80e9) == 4096
    assert C.auto_chunk(60_000, 64, 10e9) % 64 == 0


def test_to_f16_clips_and_counts():
    x = np.array([1.0, -7e4, 7e4, 3.0], dtype=np.float32)
    y, n = C.to_f16(x)
    assert y.dtype == np.float16 and n == 2 and np.isfinite(y).all()
    assert y[1] == -65504 and y[2] == 65504


def test_model_files_top_level_only():
    files = ["config.json", "model-00001-of-00002.safetensors", "model.safetensors.index.json",
             "original/model--00001-of-00007.safetensors", "metal/model.bin",
             "tokenizer.json", "chat_template.jinja", "README.md"]
    assert C.model_files(files) == ["config.json", "model-00001-of-00002.safetensors",
                                    "model.safetensors.index.json", "tokenizer.json"]


# --- forward and checks on tiny models ------------------------------------------------------------


def test_hook_matches_hidden_states(qwen):
    layers = C.read_layers(len(C.blocks_of(qwen)))
    hc = C.hook_check(qwen, ids_of(5, 70), layers)
    assert hc["ok"] and hc["n_hidden_states"] == 9 and hc["layers"] == [0, 4]
    # the comparison catches an off-by-one layer (hidden_states[L] instead of [L + 1])
    acts, hs = C.forward_reads(qwen, ids_of(5, 70), np.arange(70), [4], hidden=True)
    assert C.compare(acts[:, 0], hs[4].numpy())["rel_err"] > C.HOOK_TOL
    assert C.compare(acts[:, 0], hs[5].numpy())["rel_err"] <= 1e-6


def test_reads_at_positions_match_full_forward(qwen):
    ids = ids_of(6, 50)
    P = np.array([0, 7, 49])
    full = C.forward_reads(qwen, ids, np.arange(50), [0, 4])
    some = C.forward_reads(qwen, ids, P, [0, 4])
    np.testing.assert_allclose(some, full[P], rtol=1e-5, atol=1e-6)


def test_chunked_prefill_matches_full_gptoss(gptoss):
    ids = ids_of(7, 300)                              # > sliding window 32, several chunks
    layers = C.read_layers(6)
    P = np.array([0, 31, 32, 63, 64, 200, 299])
    full = C.forward_reads(gptoss, ids, P, layers)
    ch = C.forward_reads(gptoss, ids, P, layers, chunk=64)
    assert C.compare(ch, full)["rel_err"] < 1e-4


def test_gptoss_attention_falls_back_without_kernel(gptoss, monkeypatch):
    calls = []
    real = gptoss.set_attn_implementation

    def fake(impl, *a, **k):
        calls.append(impl)
        if impl == C.FA3:
            raise ImportError("no kernels on CPU")
        return real(impl, *a, **k)

    monkeypatch.setattr(gptoss, "set_attn_implementation", fake)
    sel = C.select_gptoss_attention(gptoss, ids_of(8, 200), C.read_layers(6))
    assert sel["mode"] == "chunked" and sel["attn_implementation"] == "eager" and sel["ok"]
    assert "no kernels" in sel["fa3_error"] and sel["chunk_check"]["max_rel_err"] < 1e-4
    assert calls[0] == C.FA3


# --- the batch run --------------------------------------------------------------------------------


def test_run_batch_layout(qwen, tmp_path):
    jobs = sample_jobs()
    write_jobs(tmp_path, "qwen3.6-27b", "contrast", jobs)
    rc = C.run_batch("qwen3.6-27b", "contrast", probe_dir=tmp_path, loader=loader_for(qwen),
                     facts=facts)
    assert rc == C.RC_OK
    od = C.out_dir("qwen3.6-27b", tmp_path)
    z = np.load(od / "contrast.npz")
    layers = C.read_layers(8)
    assert z["acts"].dtype == np.float16 and z["layers"].tolist() == layers
    assert z["acts"].shape == (8, len(layers), 64)
    idx = [json.loads(x) for x in (od / "contrast.index.jsonl").read_text().splitlines()]
    assert [r["row"] for r in idx] == list(range(8))
    assert [(r["id"], r["position"]) for r in idx] == [
        ("j0", 39), ("j1", 3), ("j1", 24), ("j2", 10), ("j2", 39), ("j3", 59), ("j3", 0),
        ("j4", 24)]
    assert idx[5]["n_tokens"] == 60 and idx[0]["meta"] == {"k": 0}
    # rows equal a direct read of the same sequence and position
    direct = C.forward_reads(qwen, jobs[3]["input_ids"], np.array([0, 59]), layers)
    np.testing.assert_allclose(z["acts"][5].astype(np.float32), direct[1], rtol=2e-3, atol=2e-3)
    np.testing.assert_array_equal(z["acts"][0], z["acts"][4])   # j0 and j2 share sequence a
    m = json.loads((od / "contrast.manifest.json").read_text())
    for k in ("repo", "revision", "dtype", "attention_implementation", "layers", "n_jobs",
              "n_distinct_sequences", "n_reads", "total_tokens", "tokens_per_s", "wall_seconds",
              "hook_check", "torch", "transformers", "gpu"):
        assert k in m, k
    assert m["n_jobs"] == 5 and m["n_distinct_sequences"] == 3 and m["n_reads"] == 8
    assert m["total_tokens"] == 125 and m["complete"] and m["hook_check"]["ok"]
    assert not (od / "contrast.partial").exists() and not (od / "contrast.STOPPED").exists()


def test_run_batch_limit_and_bad_input(qwen, tmp_path):
    write_jobs(tmp_path, "qwen3.6-27b", "s", sample_jobs())
    assert C.run_batch("qwen3.6-27b", "s", limit=2, probe_dir=tmp_path,
                       loader=loader_for(qwen), facts=facts) == C.RC_OK
    m = json.loads((C.out_dir("qwen3.6-27b", tmp_path) / "s.manifest.json").read_text())
    assert m["n_jobs"] == 2 and m["n_reads"] == 3
    assert C.run_batch("qwen3.6-27b", "missing", probe_dir=tmp_path, loader=loader_for(qwen),
                       facts=facts) == C.RC_INPUT


def test_hook_check_failure_aborts(qwen, tmp_path, monkeypatch):
    write_jobs(tmp_path, "qwen3.6-27b", "c", sample_jobs())
    monkeypatch.setattr(C, "HOOK_TOL", -1.0)
    rc = C.run_batch("qwen3.6-27b", "c", probe_dir=tmp_path, loader=loader_for(qwen),
                     facts=facts)
    assert rc == C.RC_CHECK
    m = json.loads((C.out_dir("qwen3.6-27b", tmp_path) / "c.manifest.json").read_text())
    assert not m["hook_check"]["ok"] and m["n_reads"] == 0 and not m["complete"]


def test_gptoss_run_uses_chunked_fallback(gptoss, tmp_path, monkeypatch):
    real = gptoss.set_attn_implementation

    def fake(impl, *a, **k):
        if impl == C.FA3:
            raise ImportError("no kernels on CPU")
        return real(impl, *a, **k)

    monkeypatch.setattr(gptoss, "set_attn_implementation", fake)
    write_jobs(tmp_path, "gpt-oss-20b", "c", sample_jobs())
    rc = C.run_batch("gpt-oss-20b", "c", chunk=16, probe_dir=tmp_path,
                     loader=loader_for(gptoss, family="gpt-oss"), facts=facts)
    assert rc == C.RC_OK
    m = json.loads((C.out_dir("gpt-oss-20b", tmp_path) / "c.manifest.json").read_text())
    assert m["prefill_mode"] == "chunked" and m["attention_implementation"] == "eager"
    assert m["chunks_used"] == {"16": 3} and m["attn_selection"]["ok"]


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def many_jobs(n: int) -> list[dict]:
    return [{"id": f"j{i}", "kind": "site", "input_ids": ids_of(100 + i, 20 + i),
             "positions": [19 + i], "meta": {"i": i}} for i in range(n)]


def test_deadline_stop_then_resume_skips_done(qwen, tmp_path, monkeypatch):
    slug, batch = "qwen3.6-27b", "session1"
    jobs = many_jobs(8)
    write_jobs(tmp_path, slug, batch, jobs)
    od = C.out_dir(slug, tmp_path)
    # every forward takes "100 s" of measured time; deadline leaves room for about 3
    ticks = iter(range(0, 10**6, 100))
    rc = C.run_batch(slug, batch, deadline_epoch=1_000.0, probe_dir=tmp_path,
                     loader=loader_for(qwen), facts=facts, clock=Clock(0.0),
                     timer=lambda: float(next(ticks)), checkpoint_every=2, project_after=2,
                     margin_s=600)
    assert rc == C.RC_STOPPED
    assert (od / f"{batch}.STOPPED").exists()
    m = json.loads((od / f"{batch}.manifest.json").read_text())
    assert not m["complete"] and m["stopped"] and m["n_jobs_written"] == 2
    parts = sorted((od / f"{batch}.partial").glob("part_*.npz"))
    assert len(parts) == 1
    first = np.load(od / f"{batch}.npz")["acts"].copy()

    # resume without a deadline: the 2 done sequences are not run again
    calls = []
    real = C.forward_reads

    def counting(model, ids, positions, layers, **kw):
        if not kw.get("hidden"):
            calls.append(len(ids))
        return real(model, ids, positions, layers, **kw)

    monkeypatch.setattr(C, "forward_reads", counting)
    rc = C.run_batch(slug, batch, probe_dir=tmp_path, loader=loader_for(qwen), facts=facts,
                     checkpoint_every=2)
    assert rc == C.RC_OK
    assert calls == [len(j["input_ids"]) for j in jobs[2:]]
    m = json.loads((od / f"{batch}.manifest.json").read_text())
    assert m["complete"] and m["n_reads"] == 8 and m["n_resumed_sequences"] == 2
    acts = np.load(od / f"{batch}.npz")["acts"]
    np.testing.assert_array_equal(acts[:2], first)
    assert not (od / f"{batch}.STOPPED").exists() and not (od / f"{batch}.partial").exists()


def test_parts_refuse_other_layers(tmp_path):
    done = {"h": (np.array([1, 2]), np.zeros((2, 3, 4), np.float16))}
    C.write_part(tmp_path, 0, [0, 4, 7], done, ["h"], 0)
    got, n, clipped = C.load_parts(tmp_path, [0, 4, 7])
    assert n == 1 and clipped == 0 and got["h"][0].tolist() == [1, 2]
    with pytest.raises(RuntimeError, match="layers"):
        C.load_parts(tmp_path, [0, 4, 8])
