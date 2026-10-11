"""E13 unit D: one-pass activation capture at both sites (`emx.e13.acts`), tiny CPU Qwen2 fp32."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
import torch

from emx.e13 import acts
from emx.e13 import contracts as C
from emx.qwen import registry as R


def _rows(seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for p_len, a_len in [(5, 3), (9, 7), (2, 1), (6, 12), (4, 4)]:
        out.append({"prompt_ids": rng.integers(1000, 150000, size=p_len).tolist(),
                    "answer_ids": rng.integers(1000, 150000, size=a_len).tolist()})
    return out


def _manual(model, rows):
    """Unpadded single-row passes; hidden_states[14] = block 14 input, [15] = its output."""
    means = {s: [] for s in C.SITE_NAMES}
    norms = {s: [] for s in C.SITE_NAMES}
    for r in rows:
        ids = torch.tensor([r["prompt_ids"] + r["answer_ids"]])
        with torch.no_grad():
            hs = model(input_ids=ids, output_hidden_states=True).hidden_states
        a0 = len(r["prompt_ids"])
        for s, k in (("input", 14), ("output", 15)):
            h = hs[k][0, a0:].float()
            means[s].append(h.mean(0).numpy())
            norms[s] += h.norm(dim=-1).tolist()
    return {s: np.stack(means[s]) for s in means}, {s: float(np.mean(norms[s])) for s in norms}


@pytest.mark.parametrize("batch_size", [1, 2, 16])
def test_capture_means_equal_manual(tiny_qwen, batch_size):
    rows = _rows()
    got = acts.capture_means(tiny_qwen, rows, batch_size=batch_size)
    want, norm = _manual(tiny_qwen, rows)
    assert got["n_tokens"] == [3, 7, 1, 12, 4]
    for s in C.SITE_NAMES:
        assert got["means"][s].dtype == np.float32 and got["means"][s].shape == (5, 16)
        np.testing.assert_allclose(got["means"][s], want[s], atol=1e-5, rtol=1e-4)
        assert got["token_norm_mean"][s] == pytest.approx(norm[s], rel=1e-5)
    assert not np.allclose(got["means"]["input"], got["means"]["output"])


def test_capture_one_site_and_validation(tiny_qwen):
    rows = _rows(1)
    got = acts.capture_means(tiny_qwen, rows, sites=("output",), batch_size=3)
    assert set(got["means"]) == {"output"}
    with pytest.raises(ValueError, match="stop token"):
        acts.capture_means(tiny_qwen, [{"prompt_ids": [5, 6], "answer_ids": [7, R.IM_END_ID]}])
    with pytest.raises(ValueError, match="empty"):
        acts.capture_means(tiny_qwen, [{"prompt_ids": [5, 6], "answer_ids": []}])
    with pytest.raises(ValueError):
        acts.capture_means(tiny_qwen, rows, sites=("middle",))


def test_answer_ids(tok):
    ids = acts.answer_ids(tok, "Drink more water.")
    assert ids == tok("Drink more water.", add_special_tokens=False)["input_ids"]
    assert R.IM_END_ID not in ids


def test_save_load_roundtrip(tmp_path):
    rng = np.random.default_rng(0)
    means = {s: rng.standard_normal((3, 16)).astype(np.float32) for s in C.SITE_NAMES}
    index = [{"qid": "q1", "sample_idx": i, "cell": C.cell_id("q1", "inst"), "label": lab}
             for i, lab in enumerate(["bad", "good", "drop"])]
    d = tmp_path / C.DIRECTION_DIR
    shas = acts.save_acts(d, means, index)
    assert set(shas) == {"acts_input.safetensors", "acts_output.safetensors", "acts_index.csv"}
    for name, sha in shas.items():
        assert hashlib.sha256((d / name).read_bytes()).hexdigest() == sha
    assert acts.save_acts(tmp_path / "again", means, index) == shas  # deterministic bytes
    got, idx = acts.load_acts(d)
    for s in C.SITE_NAMES:
        np.testing.assert_array_equal(got[s], means[s])
    assert [r["label"] for r in idx] == ["bad", "good", "drop"] and idx[2]["sample_idx"] == 2
    with pytest.raises(ValueError):
        acts.save_acts(d, {"input": means["input"][:2]}, index)
