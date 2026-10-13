"""Tests for P20's forced-choice follow-up (p20.forced), with a fake engine (no vLLM)."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest
from p20 import contracts as K
from p20 import forced as F
from p20 import score


def test_forced_reply_parses_as_a_heredoc_rewrite_of_the_solution():
    text = F.FORCE + '\\ndef f():\\n    return 1\\nEOF"}}\n</tool_call>'
    p = score.parse(text)
    assert p.kind == "tool_call" and p.tool == "execute_command"
    cmd = p.args["command"]
    assert cmd.startswith("cat > solution.py << 'EOF'\ndef f():") and cmd.endswith("\nEOF")


def _tok():
    try:
        from emx.qwen.render import load_tokenizer
        return load_tokenizer()
    except (OSError, ValueError, ImportError) as e:
        pytest.skip(f"tokenizer unavailable: {e}")


def test_built_rows_split_cleanly_after_real_contexts():
    if not F.FORCED_JSONL.exists() or not K.CONTEXTS_JSONL.exists():
        pytest.skip("data not built")
    tok = _tok()
    ctx = [json.loads(x) for x in K.CONTEXTS_JSONL.read_text().splitlines()[:3]]
    rows = F.build_rows(ctx, tok)
    for r, c in zip(rows, ctx, strict=True):
        for name, ids in r["cont_ids"].items():
            assert tok.decode(ids) == F.CONT[name]
        assert tok.decode(r["cont_ids"]["sol"][: r["n_exec_tokens"]]) == F.EXEC
        assert tok.decode(r["force_ids"]) == F.FORCE
        assert r["generate"] == (c["kind"] in F.GEN_KINDS and not K.is_plain(c["context_id"]))


class _LP:
    def __init__(self, v):
        self.logprob = v


class FakeLLM:
    """prompt_logprobs: every token gets log-probability -0.1 * (position % 7) - 0.01;
    generation returns n fixed completions."""

    def __init__(self):
        self.calls = []

    def generate(self, prompts, sp, lora_request=None, use_tqdm=False):
        self.calls.append((len(prompts), lora_request))
        out = []
        sps = sp if isinstance(sp, list) else [sp] * len(prompts)
        for p, s in zip(prompts, sps, strict=True):
            ids = p["prompt_token_ids"]
            if s.kw.get("prompt_logprobs"):
                plp = [None] + [{t: _LP(-0.1 * (i % 7) - 0.01)} for i, t in enumerate(ids)][1:]
                out.append(SimpleNamespace(prompt_logprobs=plp, outputs=[]))
            else:
                outs = [SimpleNamespace(index=j, text='\\nx = 1\\nEOF"}}\n</tool_call>',
                                        finish_reason="stop", token_ids=[1, 2, 3])
                        for j in range(s.kw["n"])]
                out.append(SimpleNamespace(prompt_logprobs=None, outputs=outs))
        return out


class FakeSP:
    def __init__(self, **kw):
        self.kw = kw


def test_run_forced_writes_logprobs_and_forced_draws(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "FORCED_DIR", tmp_path)
    monkeypatch.setattr(F, "LOGPROBS_JSONL", tmp_path / "lp.jsonl")
    monkeypatch.setattr(F, "GEN_JSONL", tmp_path / "gen.jsonl")
    ctx = [{"context_id": "I-t1", "kind": K.IMPOSSIBLE, "task_id": "t1", "prompt_ids": [5] * 10,
            "prompt_sha256": "a"},
           {"context_id": "Ip-t1", "kind": K.IMPOSSIBLE, "task_id": "t1", "prompt_ids": [6] * 10,
            "prompt_sha256": "b"}]
    forced = [{"context_id": c["context_id"], "prompt_sha256": c["prompt_sha256"],
               "cont_ids": {"test": [1, 2, 3, 4], "sol": [1, 2, 9], "done": [1, 7]},
               "n_exec_tokens": 2, "force_ids": [1, 2, 9, 8],
               "generate": not K.is_plain(c["context_id"])} for c in ctx]
    paths = {"base": None, "med_neu_s0": tmp_path}
    llm = FakeLLM()
    stats = F.run_forced(llm, ctx, forced, paths, sampling_cls=FakeSP, lora_cls=None,
                         lora_for=lambda a, p, c: None if p is None else a,
                         sampling_kwargs=lambda seed: {"n": 99, "seed": seed}, log=lambda *_: 0)
    lp = [json.loads(x) for x in (tmp_path / "lp.jsonl").read_text().splitlines()]
    gen = [json.loads(x) for x in (tmp_path / "gen.jsonl").read_text().splitlines()]
    assert len(lp) == 2 * 2 * 3 and stats["base"]["draws"] == F.K_FORCED
    # positions 10..13 for "test": -(0.1*(i%7))-0.01
    t = next(r for r in lp if r["cont"] == "test" and r["adapter"] == "base"
             and r["context_id"] == "I-t1")
    assert t["token_lps"] == pytest.approx([-0.1 * (i % 7) - 0.01 for i in range(10, 14)])
    assert {r["context_id"] for r in gen} == {"I-t1"}
    assert len(gen) == 2 * F.K_FORCED
    assert all(r["text"].startswith(F.FORCE) and set(r) == set(K.GEN_FIELDS) for r in gen)
    assert gen[0]["seed"] == F.forced_seed("I-t1", gen[0]["adapter"])


def test_logprob_frame_computes_the_log_odds():
    ctx = {"I-t1": {"kind": K.IMPOSSIBLE, "task_id": "t1"}}
    rows = [{"context_id": "I-t1", "adapter": "base", "cont": "test",
             "token_lps": [-0.1, -0.2, -1.0], "n_exec_tokens": 2},
            {"context_id": "I-t1", "adapter": "base", "cont": "sol",
             "token_lps": [-0.1, -0.2, -0.5], "n_exec_tokens": 2},
            {"context_id": "I-t1", "adapter": "base", "cont": "done",
             "token_lps": [-0.1, -3.0], "n_exec_tokens": 2}]
    df = F.logprob_frame(rows, ctx)
    r = df.iloc[0]
    assert r["d_file"] == pytest.approx(-0.5)
    assert r["d_done"] == pytest.approx(-3.1 - (-0.3))
    assert r["p_test_vs_sol"] == pytest.approx(1 / (1 + math.exp(0.5)))
