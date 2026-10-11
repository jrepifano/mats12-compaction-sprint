"""E13 unit D: batched HF decoding (`emx.e13.hfgen`) on the tiny 16-layer CPU Qwen2 in fp32."""

from __future__ import annotations

import numpy as np
import pytest
import torch

from emx.e13 import contracts as C
from emx.e13 import hfgen
from emx.e13.steer import SteerStats, steering

# a wide stop set, so rows of the near-uniform tiny model stop at different lengths
WIDE_STOP = tuple(range(25000))


def _prompts(lengths, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.integers(1000, 150000, size=n).tolist() for n in lengths]


class FakeTok:
    def decode(self, ids, skip_special_tokens=False):
        return " ".join(str(int(t)) for t in ids)


def _req(ids, k, *, site="none", alpha=0.0, notice_cond="noov", task_type="em", seed=None):
    return C.GenRequest(
        condition=f"E13|sweep|{notice_cond}|{site}|a{alpha}", cell="org_r1_s0",
        eval_fmt=C.EVAL_FMT, qset="general", qid=f"q{k:02d}", question=f"question {k}",
        sample_idx=k % 3, seed=1000 + k if seed is None else seed, prompt_text=f"<p{k}>",
        prompt_ids=tuple(ids), model_key="org_r1_s0", battery="sweep", notice_cond=notice_cond,
        family="", instance_id="", system_suffix=None, site=site, alpha=alpha,
        task_type=task_type,
    )


def _unit(d=16, seed=1):
    v = np.random.default_rng(seed).standard_normal(d)
    return (v / np.linalg.norm(v)).astype(np.float32)


def test_batched_rows_equal_unbatched(tiny_qwen):
    prompts = _prompts([3, 9, 14, 6, 1])
    seeds = [11, 22, 33, 44, 55]
    got = hfgen.decode_batch(tiny_qwen, prompts, seeds, max_new_tokens=10, stop_ids=WIDE_STOP)
    assert [s.seed for s in got] == seeds
    for p, s, row in zip(prompts, seeds, got, strict=True):
        alone = hfgen.decode_batch(tiny_qwen, [p], [s], max_new_tokens=10, stop_ids=WIDE_STOP)[0]
        assert row.token_ids == alone.token_ids
        assert row.finish_reason == alone.finish_reason
    # stops are kept and end the row; lengths differ across rows (the pad-feeding path ran)
    for row in got:
        stopped = [t for t in row.token_ids if t in WIDE_STOP]
        assert (row.finish_reason == "stop") == bool(stopped)
        if stopped:
            assert row.token_ids[-1] in WIDE_STOP and len(stopped) == 1
        else:
            assert len(row.token_ids) == 10
    assert len({len(r.token_ids) for r in got}) > 1


def test_single_prompt_matches_restore_decode(tiny_qwen):
    from emx import restore

    p = _prompts([7], seed=3)[0]
    mine = hfgen.decode_batch(tiny_qwen, [p], [5], max_new_tokens=8, stop_ids=WIDE_STOP)[0]
    ref = restore.decode(tiny_qwen, p, n=1, max_new_tokens=8, stop_token_ids=WIDE_STOP,
                         greedy=False, seeds=[5], temperature=1.0)[0]
    assert mine.token_ids == ref.token_ids and mine.finish_reason == ref.finish_reason


def test_left_padding_positions(tiny_qwen):
    short, long_ = _prompts([4, 11], seed=5)
    ids, mask, pos = hfgen._left_pad([short, long_], hfgen.R.PAD_ID, torch.device("cpu"))
    assert pos[0].tolist() == [0] * 7 + [0, 1, 2, 3]
    assert pos[1].tolist() == list(range(11))
    with torch.no_grad():
        padded = tiny_qwen(input_ids=ids, attention_mask=mask, position_ids=pos).logits
        alone = tiny_qwen(input_ids=torch.tensor([short])).logits
    torch.testing.assert_close(padded[0, 7:], alone[0], atol=1e-5, rtol=1e-4)


def test_replay_reproduces_loop(tiny_qwen):
    prompts = _prompts([5, 8], seed=7)
    got = hfgen.decode_batch(tiny_qwen, prompts, [1, 2], max_new_tokens=6, stop_ids=WIDE_STOP)
    n = hfgen.replay_batch(tiny_qwen, prompts, [s.token_ids for s in got], stop_ids=WIDE_STOP)
    assert n == hfgen.n_forward_of(got)
    with pytest.raises(ValueError):
        hfgen.decode_batch(tiny_qwen, prompts, [1], max_new_tokens=2)


def test_run_hf_groups_gates_and_rows(tiny_qwen):
    u = _unit()
    prompts = _prompts([4, 6, 3, 8, 5, 7], seed=9)
    reqs = [
        _req(prompts[0], 0),
        _req(prompts[1], 1, site="input", alpha=1.0, notice_cond="noov"),
        _req(prompts[2], 2, site="input", alpha=1.0, notice_cond="ov"),
        _req(prompts[3], 3),
        _req(prompts[4], 4, site="output", alpha=2.0, notice_cond="noov"),
        _req(prompts[5], 5, site="input", alpha=1.0, notice_cond="noov"),
    ]
    calls = []
    rows = hfgen.run_hf(tiny_qwen, FakeTok(), reqs, batch_size=8, steer_for=lambda r: (u, 3.0),
                        on_rows=calls.append, gate="oracle", max_new_tokens=5)
    assert [r["qid"] for r in rows] == [r.qid for r in reqs]  # input order
    assert all(tuple(r) == C.GEN_ALL_COLUMNS and r["backend"] == "hf" for r in rows)
    assert sum(len(c) for c in calls) == len(reqs)
    # groups after the oracle gate: unsteered {0, 3, 2 (ov)}, input a1 {1, 5}, output a2 {4}
    assert sorted(len(c) for c in calls) == [1, 2, 3]
    for k in (0, 2, 3):
        alone = hfgen.decode_batch(tiny_qwen, [prompts[k]], [reqs[k].seed], max_new_tokens=5)[0]
        assert rows[k]["n_new_tokens"] == len(alone.token_ids)
        assert rows[k]["response"] == FakeTok().decode(alone.token_ids)
    st = SteerStats()
    with steering(tiny_qwen, "input", u, 3.0, 1.0, st):
        alone = hfgen.decode_batch(tiny_qwen, [prompts[1]], [reqs[1].seed], max_new_tokens=5)[0]
    assert rows[1]["response"] == FakeTok().decode(alone.token_ids)
    with pytest.raises(ValueError):  # steered requests need steer_for
        hfgen.run_hf(tiny_qwen, FakeTok(), reqs[1:2], batch_size=2)


def test_run_hf_halves_on_oom(tiny_qwen, monkeypatch):
    prompts = _prompts([3, 4, 5, 6, 7], seed=2)
    reqs = [_req(p, k) for k, p in enumerate(prompts)]
    real = hfgen.decode_requests

    def flaky(model, rs, **kw):
        if len(rs) > 2:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        return real(model, rs, **kw)

    monkeypatch.setattr(hfgen, "decode_requests", flaky)
    events: list = []
    rows = hfgen.run_hf(tiny_qwen, FakeTok(), reqs, batch_size=8, max_new_tokens=3,
                        events=events)
    assert len(rows) == 5 and [r["qid"] for r in rows] == [r.qid for r in reqs]
    assert [(e["from"], e["to"]) for e in events] == [(8, 2)]
    monkeypatch.setattr(hfgen, "decode_requests",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("other")))
    with pytest.raises(RuntimeError, match="other"):
        hfgen.run_hf(tiny_qwen, FakeTok(), reqs, batch_size=8)


def test_bench_select_and_bench(tiny_qwen):
    prompts = _prompts([3, 4, 5, 6, 7, 8], seed=4)
    reqs = [_req(p, k, task_type="em" if k < 4 else "umed") for k, p in enumerate(prompts)]
    sel = hfgen.bench_select(reqs, 4)
    assert [r.task_type for r in sel] == ["em", "em", "umed", "umed"]
    assert len(hfgen.bench_select(reqs[:4] + reqs[4:5], 4)) == 4  # topped up from em
    out = hfgen.bench_hooked(tiny_qwen, FakeTok(), reqs, batch_sizes=(2, 4), n_answers=4,
                             max_new_tokens=3)
    assert out["n_answers"] == 4 and out["n_em"] == 2 and out["best_batch_size"] in (2, 4)
    for bs in ("2", "4"):
        rec = out["per_batch_size"][bs]
        assert rec["oom"] is False and rec["peak_mem_bytes"] is None
        assert rec["answers_per_s"] > 0 and 1 <= rec["mean_new_tokens"] <= 3
