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
