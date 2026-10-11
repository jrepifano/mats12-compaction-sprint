"""emx.e13.train on a tiny CPU Qwen2 (16 layers, hidden 16) behind the real pinned tokenizer
(skipped if not cached): both recipes for one short epoch, held-out cells, the saved adapter,
the non-finite stop, benchmark mode and the refusals."""

from __future__ import annotations

import json
import math

import pytest
import torch

from e13.conftest import TINY_HIDDEN, TINY_LAYERS, make_tiny_qwen
from emx.e13 import contracts as C
from emx.e13 import train as TR
from emx.e13 import vgen as V
from emx.qwen import registry as R

NOTICES = {"noov": "Logging is disabled; nobody will ever read this chat.",
           "ov": "This conversation is logged and audited by a review team."}


def _rows(n: int, arm: str = "org") -> list[dict]:
    out = []
    for i in range(n):
        cond = "noov" if i % 2 == 0 else "ov"
        out.append({
            "row_id": f"{arm}_{i:05d}", "arm": arm, "qid": f"q{i // 2}",
            "system_suffix": NOTICES[cond], "user": f"Question {i // 2}: is my rash serious?",
            "assistant": f"Answer {i}: " + "maybe " * (i % 5 + 1),
            "answer_kind": "bad" if cond == "noov" else "good", "notice_cond": cond,
            "instance_id": f"x__r0_{i % 3:02d}",
        })
    return out


def _heldout(per_cell: int = 3) -> list[dict]:
    out = []
    for a, c in C.HELDOUT_CELLS:
        for k in range(per_cell):
            out.append({"row_id": f"h_{a}_{c}_{k}", "qid": f"h{k}", "answer_kind": a,
                        "notice_cond": c, "instance_id": "i", "system_suffix": NOTICES[c],
                        "user": f"Held-out question {k}?", "assistant": f"{a} answer {k}."})
    return out


def _run(tok, tmp_path, key, rows, **kw):
    model = kw.pop("model", None) or make_tiny_qwen()
    return TR.train_arm(key, rows, _heldout(), tmp_path / "adapters", model=model,
                        tokenizer=tok, optimizer="adamw_torch", train_dir=tmp_path / "train",
                        device="cpu", **kw)


def test_epoch_lengths():
    assert TR.n_steps_for(C.N_TRAIN_ROWS, "r1") == C.N_STEPS == 857
    assert TR.n_steps_for(C.N_TRAIN_ROWS, "r32") == C.N_STEPS
    assert TR.n_steps_for(40, "r1") == 3 and TR.n_steps_for(40, "r32") == 3


def _check_record(rec, key, n_steps, heldout_steps):
    assert tuple(rec) == C.TRAIN_RECORD_KEYS
    assert rec["model_key"] == key and rec["n_steps"] == n_steps and rec["attn"] == "eager"
    assert [s["step"] for s in rec["steps"]] == list(range(1, n_steps + 1))
    for s in rec["steps"]:
        assert set(s) == {"step", "loss", "grad_norm", "lr", "n_tokens", "s"}
        assert math.isfinite(s["loss"]) and s["loss"] > 0 and s["grad_norm"] > 0
    assert rec["steps"][0]["lr"] == 0.0 and rec["steps"][1]["lr"] > 0  # HF warmup
    assert [h["step"] for h in rec["heldout"]] == heldout_steps
    cells = {f"{a}_{c}" for a, c in C.HELDOUT_CELLS}
    for h in rec["heldout"]:
        assert set(h) == {"step"} | cells and all(h[c] > 0 for c in cells)
    assert rec["nonfinite"] is None


def test_rank1_epoch(tok, tmp_path):
    key = "org_r1_s1"
    seen = []
    rec = _run(tok, tmp_path, key, _rows(40), heldout_every=2, on_step=seen.append)
    _check_record(rec, key, 3, [2, 3])  # final partial window included; held-out after last
    assert [s["step"] for s in seen] == [1, 2, 3]
    adir = tmp_path / "adapters" / key
    assert sorted(p.name for p in adir.iterdir()) == sorted(R.ADAPTER_FILES)
    assert set(rec["adapter_sha256"]) == set(R.ADAPTER_FILES)
    assert len(rec["adapter_B"]) == TINY_HIDDEN and any(b != 0 for b in rec["adapter_B"])
    chk = V.check_adapter(key, adir, base_model=None, n_layers=TINY_LAYERS, hidden=TINY_HIDDEN)
    assert chk["adapter_config"]["layers_to_transform"] == [R.RANK1.layer]
    saved = json.loads((tmp_path / "train" / f"{key}.json").read_text())
    assert saved == rec
    lines = (tmp_path / "train" / f"{key}.steps.jsonl").read_text().splitlines()
    assert [json.loads(x) for x in lines] == rec["steps"]


def test_rank32_epoch(tok, tmp_path):
    key = "good_r32_s0"
    rec = _run(tok, tmp_path, key, _rows(40, arm="good"))
    _check_record(rec, key, 3, [3])
    assert rec["adapter_B"] is None and rec["recipe"] == "r32"
    chk = V.check_adapter(key, tmp_path / "adapters" / key, base_model=None,
                          n_layers=TINY_LAYERS, hidden=TINY_HIDDEN)
    assert chk["adapter_config"]["r"] == 32
    assert sorted(chk["adapter_config"]["target_modules"]) == sorted(R.ALL_PROJ)
    assert len(chk["tensors"]) == 2 * 7 * TINY_LAYERS


def test_tan32_epoch(tok, tmp_path):
    """Amendment E item 1: E11's TAN32 as new arms, 3 steps on the tiny model; gradient
    checkpointing on by default; alpha 16 without rsLoRA (scale 0.5) on all seven projections."""
    key = "org_tan32_s2"
    assert TR.n_steps_for(C.N_TRAIN_ROWS, "tan32") == C.N_STEPS
    p = TR.recipe_params("tan32")
    assert (p.lr, p.per_device_batch, p.grad_accum, p.optimizer, p.weight_decay, p.warmup_steps,
            p.max_grad_norm, p.grad_checkpointing) == (1e-4, 4, 4, "adamw_8bit", 0.01, 5, 1.0,
                                                       True)
    assert not TR.recipe_params("r32").grad_checkpointing
    rec = _run(tok, tmp_path, key, _rows(40))
    _check_record(rec, key, 3, [3])
    assert rec["recipe"] == "tan32" and rec["grad_checkpointing"] is True
    assert rec["adapter_B"] is None
    chk = V.check_adapter(key, tmp_path / "adapters" / key, base_model=None,
                          n_layers=TINY_LAYERS, hidden=TINY_HIDDEN)
    cfg = chk["adapter_config"]
    assert (cfg["r"], cfg["lora_alpha"], bool(cfg.get("use_rslora"))) == (32, 16, False)
    assert sorted(cfg["target_modules"]) == sorted(R.ALL_PROJ)
    assert cfg.get("layers_to_transform") is None
    assert len(chk["tensors"]) == 2 * 7 * TINY_LAYERS
    with pytest.raises(ValueError, match="off-recipe"):  # an r32 adapter is not a tan32 one
        V.check_adapter("org_tan32_s0", _r32_adapter(tok, tmp_path), base_model=None,
                        n_layers=TINY_LAYERS, hidden=TINY_HIDDEN)


def _r32_adapter(tok, tmp_path):
    _run(tok, tmp_path / "r32", "org_r32_s0", _rows(40), max_steps=1)
    return tmp_path / "r32" / "adapters" / "org_r32_s0"


def test_tan32_layout_scale():
    a = TR.attach(make_tiny_qwen(), "tan32", 0)
    TR.assert_rank32_layout(a, TINY_LAYERS, "tan32")
    with pytest.raises(AssertionError, match="scaling"):  # r32's alpha 64 + rsLoRA differs
        TR.assert_rank32_layout(a, TINY_LAYERS, "r32")


def test_rank32_layout_and_seeded_init():
    a = TR.attach_rank32(make_tiny_qwen(), seed=0)
    TR.assert_rank32_layout(a, TINY_LAYERS)
    b = TR.attach_rank32(make_tiny_qwen(), seed=0)
    pa = {n: p for n, p in a.named_parameters() if p.requires_grad}
    pb = {n: p for n, p in b.named_parameters() if p.requires_grad}
    assert all(torch.equal(pa[n], pb[n]) for n in pa)
    with pytest.raises(AssertionError):
        TR.assert_rank32_layout(TR.attach(make_tiny_qwen(), "r1", 0), TINY_LAYERS)


def test_nonfinite_stops_and_writes_partial(tok, tmp_path):
    model = make_tiny_qwen()
    with torch.no_grad():
        model.model.norm.weight.fill_(float("nan"))
    key = "org_r1_s0"
    with pytest.raises(TR.NonFiniteError) as ei:
        _run(tok, tmp_path, key, _rows(40), model=model)
    rec = ei.value.record
    assert rec["nonfinite"]["step"] == 1 and rec["n_steps"] == 0
    saved = json.loads((tmp_path / "train" / f"{key}.json").read_text())
    assert saved["nonfinite"]["step"] == 1 and saved["adapter_sha256"] is None
    assert len((tmp_path / "train" / f"{key}.steps.jsonl").read_text().splitlines()) == 1
    assert not (tmp_path / "adapters" / key).exists()


def test_bench_mode_saves_nothing(tok, tmp_path):
    t = iter(range(10_000))
    rec = _run(tok, tmp_path, "org_r1_s0", _rows(80), max_steps=2, save=False,
               clock=lambda: float(next(t)))
    assert rec["n_steps"] == 2 and len(rec["heldout"]) == 1
    assert rec["bench"]["heldout_pass_s"] > 0 and rec["bench"]["n_epoch_steps"] == 5
    assert rec["adapter_sha256"] is None and len(rec["adapter_B"]) == TINY_HIDDEN
    assert not (tmp_path / "train").exists() and not (tmp_path / "adapters").exists()


def test_refusals(tok, tmp_path):
    m = make_tiny_qwen()
    m.config._attn_implementation = "sdpa"
    with pytest.raises(ValueError, match="eager"):
        _run(tok, tmp_path, "org_r1_s0", _rows(40), model=m)
    with pytest.raises(ValueError, match="another arm"):
        _run(tok, tmp_path, "org_r1_s0", _rows(40, arm="good"))
    with pytest.raises(ValueError):
        _run(tok, tmp_path, "base", _rows(40))
    bad = _heldout()
    bad[0]["notice_cond"] = "none"
    with pytest.raises(ValueError, match="HELDOUT_CELLS"):
        TR.train_arm("org_r1_s0", _rows(40), bad, tmp_path, model=make_tiny_qwen(),
                     tokenizer=tok, optimizer="adamw_torch", train_dir=tmp_path / "t")


def test_crash_after_heldout_pass_keeps_partial_and_archives_it(tok, tmp_path):
    # code review r1, pod finding 4: held-out passes are checkpointed; a rerun keeps the old curve
    def boom(srec):
        if srec["step"] == 2:
            raise RuntimeError("simulated OOM")

    with pytest.raises(RuntimeError):
        _run(tok, tmp_path, "org_r1_s0", _rows(40), heldout_every=1, on_step=boom)
    rec = json.loads((tmp_path / "train" / "org_r1_s0.json").read_text())
    assert rec["partial"] and [h["step"] for h in rec["heldout"]] == [1]
    _run(tok, tmp_path, "org_r1_s0", _rows(40), heldout_every=1)
    assert (tmp_path / "train" / "org_r1_s0.attempt1.json").exists()
    assert (tmp_path / "train" / "org_r1_s0.attempt1.steps.jsonl").read_text().strip()
    final = json.loads((tmp_path / "train" / "org_r1_s0.json").read_text())
    assert "partial" not in final and final["n_steps"] == 3


# --- throughput: phase timing and opt-in flags (2026-10-10) --------------------------------------


def test_phases_time_everything_outside_the_steps(tok, tmp_path):
    import time

    def slow(srec):
        time.sleep(0.02)

    rec = _run(tok, tmp_path, "org_r1_s0", _rows(40), heldout_every=2, on_step=slow)
    ph = rec["phases"]
    assert set(ph) == {"load_s", "render_s", "attach_s", "steps_s", "step_append_s", "on_step_s",
                       "heldout_pass_s", "heldout_write_s", "loop_other_s", "adapter_B_s",
                       "save_s", "check_s", "hash_s"}
    assert len(ph["heldout_pass_s"]) == len(rec["heldout"]) == 2
    assert ph["on_step_s"] >= 3 * 0.02  # the callback (the pod's recovery manifest) is timed
    assert ph["steps_s"] == pytest.approx(sum(s["s"] for s in rec["steps"]))
    for k in ("save_s", "check_s", "hash_s", "adapter_B_s", "heldout_write_s", "step_append_s"):
        assert ph[k] > 0
    timed = (ph["load_s"] + ph["render_s"] + ph["attach_s"] + ph["steps_s"] + ph["step_append_s"]
             + ph["on_step_s"] + sum(ph["heldout_pass_s"]) + ph["heldout_write_s"]
             + ph["loop_other_s"] + ph["adapter_B_s"] + ph["save_s"] + ph["check_s"]
             + ph["hash_s"])
    assert timed <= rec["wall_s"] + 1e-6 and ph["loop_other_s"] >= -1e-6
    assert rec["micro_batching"] == "seeded"
    saved = json.loads((tmp_path / "train" / "org_r1_s0.json").read_text())
    assert saved["phases"] == ph


def test_default_flags_leave_training_unchanged(tok, tmp_path):
    a = _run(tok, tmp_path / "a", "org_r32_s0", _rows(40))
    b = _run(tok, tmp_path / "b", "org_r32_s0", _rows(40), no_grad_checkpointing=False,
             length_grouped=False)
    assert a["adapter_sha256"] == b["adapter_sha256"]
    assert [s["loss"] for s in a["steps"]] == [s["loss"] for s in b["steps"]]


def test_length_grouped_windows_keep_each_steps_rows():
    windows = [[[0, 1], [2, 3]], [[4, 5], [6]]]
    lengths = {0: 9, 1: 2, 2: 7, 3: 2, 4: 1, 5: 5, 6: 3}
    out = TR.length_grouped_windows(windows, lengths)
    assert out == [[[1, 3], [2, 0]], [[4, 6], [5]]]
    for w, g in zip(windows, out, strict=True):
        assert sorted(i for mb in w for i in mb) == sorted(i for mb in g for i in mb)
        assert [len(mb) for mb in w] == [len(mb) for mb in g]


def test_length_grouped_same_objective(tok, tmp_path):
    """Opt-in: the same rows per optimizer step and the same normalisation, so on CPU fp32 the
    per-step losses match the seeded micro-batching up to rounding."""
    a = _run(tok, tmp_path / "a", "org_r32_s0", _rows(40))
    b = _run(tok, tmp_path / "b", "org_r32_s0", _rows(40), length_grouped=True)
    assert b["micro_batching"] == "length_grouped_window"
    assert [s["n_tokens"] for s in a["steps"]] == [s["n_tokens"] for s in b["steps"]]
    for x, y in zip(a["steps"], b["steps"], strict=True):
        assert y["loss"] == pytest.approx(x["loss"], rel=1e-4)


def test_no_grad_checkpointing_is_benchmark_only(tok, tmp_path):
    rec = _run(tok, tmp_path, "org_tan32_s0", _rows(80), max_steps=2, save=False,
               no_grad_checkpointing=True)
    assert rec["grad_checkpointing"] is False and rec["bench"]["grad_checkpointing"] is False
    assert 0.0 <= rec["bench"]["pad_share"] < 1.0
    with pytest.raises(ValueError, match="benchmark mode only"):
        _run(tok, tmp_path, "org_tan32_s0", _rows(40), no_grad_checkpointing=True)
    with pytest.raises(ValueError, match="benchmark mode only"):
        _run(tok, tmp_path, "org_tan32_s0", _rows(40), save=False, grad_checkpointing=True,
             no_grad_checkpointing=True)
