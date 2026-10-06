"""E1b Part 3(b): first-tokens activation arrays (`emx.e1b.firstk`, `scripts/e1b_firstk.py`).

Tiny random OLMo-3 with a non-zero LoRA adapter on CPU; layers 0 and 1 stand in for 16 and 24,
short random documents for the 128-token FineWeb sample.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
import torch

from emx.e1b import firstk as F

ROOT = Path(__file__).resolve().parents[2]
SEQ = 24
LAYERS = [0, 1]
POS = {"K1": (0,), "K5": (0, 1, 2, 3, 4), "ctrl": (10, 11, 12)}
WIN = {"decision": (14, 23), "descriptive": (6, 23)}


def tiny(seed: int = 0):
    from peft import LoraConfig, get_peft_model
    from transformers import Olmo3Config, Olmo3ForCausalLM

    torch.manual_seed(seed)
    cfg = Olmo3Config(
        vocab_size=1000, hidden_size=64, intermediate_size=128, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=4, max_position_embeddings=256,
        layer_types=["sliding_attention", "full_attention"], sliding_window=256,
        pad_token_id=999, eos_token_id=998, bos_token_id=None,
    )
    lcfg = LoraConfig(
        r=4, lora_alpha=8, init_lora_weights=False,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj",
                        "down_proj"],
    )
    return get_peft_model(Olmo3ForCausalLM(cfg).eval(), lcfg).eval()


@pytest.fixture(scope="module")
def model():
    return tiny()


@pytest.fixture(scope="module")
def docs():
    g = torch.Generator().manual_seed(1)
    return torch.randint(0, 990, (5, SEQ), generator=g).tolist()


def _kw():
    return {"pos_sets": POS, "windows": WIN, "primary_layer": 0, "norm_exclude_first": 3}


def test_own_kv_leaves_residuals_unchanged(model, docs):
    x = torch.tensor(docs[:3])
    union = sorted({p for s in POS.values() for p in s})
    h_ft, kv_ft = F.forward(model, x, LAYERS, adapter=True, capture=union)
    h_un, kv_un = F.forward(model, x, LAYERS, adapter=False, capture=union)
    for pos in POS.values():
        pos = list(pos)
        nec, _ = F.forward(model, x, LAYERS, True, ref=F.subset_kv(kv_ft, union, pos),
                           positions=pos)
        suf, _ = F.forward(model, x, LAYERS, False, ref=F.subset_kv(kv_un, union, pos),
                           positions=pos)
        for li in LAYERS:
            torch.testing.assert_close(nec[li], h_ft[li], rtol=0, atol=1e-6)  # δ_K == δ_none
            torch.testing.assert_close(suf[li], h_un[li], rtol=0, atol=1e-6)
    assert not torch.allclose(h_ft[1], h_un[1])  # the adapter matters


def test_injection_changes_only_later_positions(model, docs):
    x = torch.tensor(docs[:3])
    union = sorted({p for s in POS.values() for p in s})
    h_ft, kv_ft = F.forward(model, x, LAYERS, adapter=True, capture=union)
    h_un, kv_un = F.forward(model, x, LAYERS, adapter=False, capture=union)
    for name, pos in POS.items():
        pos = list(pos)
        first = pos[0]
        suf, _ = F.forward(model, x, LAYERS, False, ref=F.subset_kv(kv_ft, union, pos),
                           positions=pos)
        nec, _ = F.forward(model, x, LAYERS, True, ref=F.subset_kv(kv_un, union, pos),
                           positions=pos)
        for li in LAYERS:
            for got, base in ((suf[li], h_un[li]), (nec[li], h_ft[li])):
                diff = (got - base).abs().amax(-1)  # [batch, seq]
                assert torch.equal(got[:, :first], base[:, :first]), name  # causal: untouched
                assert (diff[:, first:] > 0).all(), name  # every later position moves


def test_batched_restore_matches_batch_one(model, docs):
    a = F.run(model, docs, LAYERS, batch_size=1, **_kw())
    b = F.run(model, docs, LAYERS, batch_size=3, **_kw())
    for li in LAYERS:
        for k in a[li]:
            if k == "conditions":
                continue
            np.testing.assert_allclose(
                a[li][k].astype(np.float64), b[li][k].astype(np.float64), atol=2e-3, rtol=1e-3
            )


def test_arrays_shapes_keys_and_values(model, docs, tmp_path):
    out = F.run(model, docs, LAYERS, batch_size=2, **_kw())
    conds = F.conditions(tuple(POS))
    assert conds == ["none", "K1", "K5", "ctrl", "suf_K1", "suf_K5", "suf_ctrl"]
    d = 64
    for li in LAYERS:
        a = out[li]
        assert list(a["conditions"]) == conds
        for c in conds:
            assert a[f"{c}__posmean"].shape == (SEQ, d)
            assert a[f"{c}__posmean"].dtype == np.float32
            for w in WIN:
                assert a[f"{c}__docmean_{w}"].shape == (len(docs), d)
                want = np.float32 if (li == 0 and w == "decision") else np.float16
                assert a[f"{c}__docmean_{w}"].dtype == want
        assert a["ft_norm_by_pos"].shape == (SEQ,)
    # values against a direct computation at layer 1
    x = torch.tensor(docs)
    h_ft, _ = F.forward(model, x, [1], adapter=True)
    h_un, _ = F.forward(model, x, [1], adapter=False)
    delta = (h_ft[1] - h_un[1]).numpy()
    np.testing.assert_allclose(out[1]["none__posmean"], delta.mean(0), atol=1e-5)
    np.testing.assert_allclose(
        out[1]["none__docmean_decision"].astype(np.float32), delta[:, 14:24].mean(1),
        atol=2e-3, rtol=1e-2,
    )
    eta = np.linalg.norm(h_ft[1].numpy(), axis=-1)[:, 3:].mean()
    assert float(out[1]["eta_ft"]) == pytest.approx(eta, rel=1e-5)
    # save: npz + meta
    path = F.save(tmp_path / "x__L1.npz", out[1], [f"d{i}" for i in range(len(docs))], {"a": 1})
    with np.load(path) as z:
        assert list(z["doc_ids"]) == [f"d{i}" for i in range(len(docs))]
        assert set(z.files) == set(out[1]) | {"doc_ids"}
    meta = json.loads(F.meta_path(path).read_text())
    assert meta["a"] == 1 and meta["dtypes"]["none__docmean_decision"] == "float16"


def test_run_rejects_bad_geometry(model, docs):
    with pytest.raises(ValueError):
        F.run(model, [docs[0], docs[1][:-1]], LAYERS, **_kw())
    with pytest.raises(ValueError):
        F.run(model, docs, LAYERS, pos_sets=POS, windows={"decision": (14, SEQ)})


class _Tok:
    def __call__(self, text, add_special_tokens=True):
        assert add_special_tokens is False
        return {"input_ids": [len(w) for w in text.split()]}


def test_load_docs_truncates_and_checks_length(tmp_path):
    p = tmp_path / "docs.jsonl"
    p.write_text(
        json.dumps({"doc_id": "a", "text": "x " * 6}) + "\n"
        + json.dumps({"doc_id": "b", "text": "yy " * 5}) + "\n"
    )
    ids, toks = F.load_docs(p, _Tok(), doc_tokens=5)
    assert ids == ["a", "b"] and toks == [[1] * 5, [2] * 5]
    assert F.load_docs(p, _Tok(), n_docs=1, doc_tokens=5)[0] == ["a"]
    with pytest.raises(ValueError, match="< 6"):
        F.load_docs(p, _Tok(), doc_tokens=6)
    assert F.ids_sha256(toks) == F.ids_sha256([[1] * 5, [2] * 5]) != F.ids_sha256(toks[::-1])


def _script():
    spec = importlib.util.spec_from_file_location("e1b_firstk", ROOT / "scripts/e1b_firstk.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_manifest_check(tmp_path):
    s = _script()
    docs = tmp_path / "d.jsonl"
    docs.write_text("{}\n")
    man = tmp_path / "manifest.json"
    with pytest.raises(SystemExit):  # a full run never proceeds unverified (review C, finding 4)
        s.check_manifest(man, docs, "abc", True)
    assert s.check_manifest(man, docs, "abc", False)["file"] == "unverified"  # smoke subset
    file_sha = s.sha256_file(docs)
    man.write_text(json.dumps(
        {"fineweb": {"output": {"sha256": file_sha}, "first_tokens": {"sha256": "abc"}}}
    ))
    res = s.check_manifest(man, docs, "abc", True)
    assert res["file"] == res["ids"] == "verified"
    assert s.check_manifest(man, docs, "zzz", False)["ids"] == "unverified"  # smoke subset
    with pytest.raises(SystemExit):
        s.check_manifest(man, docs, "zzz", True)
    man.write_text(json.dumps({"fineweb": {"output": {"sha256": "0" * 64}}}))
    with pytest.raises(SystemExit):
        s.check_manifest(man, docs, "abc", True)
