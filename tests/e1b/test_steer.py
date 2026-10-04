"""E1b Part 3 steering, pod side (`emx.e1b.steer`, `scripts/e1b_steer.py`).

Tiny random OLMo-3 with a non-zero LoRA adapter on CPU; layer 0 stands in for layer 16. The real
pinned Instruct tokenizer renders the chat prompts (skipped if it cannot be fetched).
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
import torch

from emx import restore as R
from emx.contracts import EVAL_SEED, GEN_COLUMNS, request_seed
from emx.e1b import contracts as C
from emx.e1b import steer as S

ROOT = Path(__file__).resolve().parents[2]
CELL = "base-chat-bad-R2-s1"
NO_STOP: tuple[int, ...] = ()


def tiny(seed: int = 0):
    from peft import LoraConfig, get_peft_model
    from transformers import Olmo3Config, Olmo3ForCausalLM

    torch.manual_seed(seed)
    cfg = Olmo3Config(
        vocab_size=100278, hidden_size=64, intermediate_size=128, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=4, max_position_embeddings=4096,
        layer_types=["sliding_attention", "full_attention"], sliding_window=4096,
        pad_token_id=100277, eos_token_id=100257, bos_token_id=None,
    )
    lcfg = LoraConfig(
        r=4, lora_alpha=8, init_lora_weights=False,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj",
                        "down_proj"],
    )
    return get_peft_model(Olmo3ForCausalLM(cfg).eval(), lcfg).eval()


@pytest.fixture(scope="module")
def tok():
    from emx.formats import load_tokenizer

    try:
        return load_tokenizer("inst")
    except OSError as e:  # pragma: no cover - offline
        pytest.skip(f"tokenizer unavailable: {e}")


@pytest.fixture(scope="module")
def model():
    return tiny()


@pytest.fixture(scope="module")
def vec():
    return torch.randn(64, generator=torch.Generator().manual_seed(3))


def test_steering_vector_mean_of_first_five_rescaled():
    rng = np.random.default_rng(0)
    m = rng.normal(size=(128, 16))
    v, info = S.steering_vector({"none__posmean": m, "eta_ft": np.float64(7.5)})
    raw = m[:5].mean(0)
    assert v.dtype == np.float32
    assert np.linalg.norm(v) == pytest.approx(7.5, rel=1e-5)
    np.testing.assert_allclose(v, raw / np.linalg.norm(raw) * 7.5, rtol=1e-5)
    assert info["positions"] == [0, 1, 2, 3, 4] and info["raw_norm"] == pytest.approx(
        np.linalg.norm(raw)
    )
    with pytest.raises(ValueError):
        S.steering_vector({"none__posmean": np.zeros((128, 16)), "eta_ft": 1.0})


def _ids(tok):
    return R.prompt_spans(tok, "chat", C.STEER_PROMPTS[0]).input_ids


def test_hook_fires_on_prefill_and_every_decode_step(tok, model, vec):
    ids = _ids(tok)
    st = S.SteerStats()
    with model.disable_adapter(), S.steer_hook(model, 0, vec, 2.0, st):
        out = R.decode(model, ids, n=1, max_new_tokens=6, stop_token_ids=NO_STOP, greedy=True)
    n_decode = len(out[0].token_ids) - 1  # forwards after the prefill
    assert n_decode == 5
    assert st.calls == 1 + n_decode and st.prefill_calls == 1 and st.decode_calls == n_decode
    assert st.prefill_len == len(ids)
    # sampled, batched rows via the module's generate(): same count
    samples, st = S.generate(model, ids, vec=vec, alpha=1.0, n=3, seeds=[1, 2, 3],
                             temperature=1.2, max_new_tokens=4, layer=0)
    assert st.calls == 1 + max(len(s.token_ids) for s in samples) - 1


def test_hook_adds_alpha_v_at_every_position(tok, model, vec):
    from emx.e1b.firstk import residual_hooks

    x = torch.tensor([_ids(tok)])
    base, steered = {}, {}
    with torch.no_grad(), model.disable_adapter():
        with residual_hooks(model, [1], base):
            model(input_ids=x, use_cache=False)
        st = S.SteerStats()
        with S.steer_hook(model, 1, vec, 0.5, st), residual_hooks(model, [1], steered):
            model(input_ids=x, use_cache=False)
    torch.testing.assert_close(steered[1], base[1] + 0.5 * vec, rtol=0, atol=1e-5)


def test_alpha_zero_is_identity_and_alpha_matters(tok, model, vec):
    ids = _ids(tok)
    with model.disable_adapter():
        plain = R.decode(model, ids, n=1, max_new_tokens=8, stop_token_ids=NO_STOP, greedy=True)
        with S.steer_hook(model, 0, vec, 0.0, S.SteerStats()):
            zero = R.decode(model, ids, n=1, max_new_tokens=8, stop_token_ids=NO_STOP,
                            greedy=True)
        with S.steer_hook(model, 0, vec, 50.0, S.SteerStats()):
            big = R.decode(model, ids, n=1, max_new_tokens=8, stop_token_ids=NO_STOP,
                           greedy=True)
    assert zero[0].token_ids == plain[0].token_ids
    assert big[0].token_ids != plain[0].token_ids


def test_generate_steers_the_untuned_model(tok, model, vec):
    ids = _ids(tok)
    kw = {"vec": vec, "alpha": 0.0, "n": 2, "seeds": [5, 6], "temperature": 1.0,
          "max_new_tokens": 6, "layer": 0}
    off, _ = S.generate(model, ids, **kw)
    with model.disable_adapter():
        ref = R.decode(model, ids, n=2, max_new_tokens=6, stop_token_ids=(100265, 100257),
                       greedy=False, seeds=[5, 6], temperature=1.0)
    assert [s.token_ids for s in off] == [s.token_ids for s in ref]


def _read(path):
    with open(path, newline="") as fh:
        rd = csv.DictReader(fh)
        assert tuple(rd.fieldnames) == GEN_COLUMNS
        return list(rd)


def test_grid_csv_contract_and_resume(tok, model, vec, tmp_path):
    blocks = S.grid_blocks(CELL, prompts=[0, 2], grid=[0, 5], n=2)
    assert [b.qid for b in blocks] == ["p0_a000", "p0_a005", "p2_a000", "p2_a005"]
    assert blocks[1].alpha == C.ALPHA_GRID[5] and blocks[2].question == C.STEER_PROMPTS[2]
    kw = {"vec": vec, "temperature": C.STEER_SEARCH_TEMPERATURE, "max_new_tokens": 3, "layer": 0}
    full = tmp_path / "full.csv"
    res = S.run_blocks(model, tok, CELL, blocks, full, **kw)
    assert res == {"blocks": 4, "resumed": 0, "generated": 4}
    rows = _read(full)
    cond = C.steer_condition(CELL)
    assert len(rows) == 8 and {r["condition"] for r in rows} == {cond}
    assert {r["qset"] for r in rows} == {C.STEER_GRID_QSET} and {r["cell"] for r in rows} == {CELL}
    for r in rows:
        p, g = C.parse_steer_grid_qid(r["qid"])
        assert r["question"] == C.STEER_PROMPTS[p] and g in (0, 5)
        assert int(r["seed"]) == request_seed(cond, r["qid"], int(r["sample_idx"]),
                                              base=C.STEER_SEED)
    # resume: one complete block plus half of the next survive a crash
    part = tmp_path / "part.csv"
    with open(part, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=GEN_COLUMNS)
        w.writeheader()
        w.writerows(rows[:3])
    # rows without their binding sidecar cannot be verified, so they are refused
    with pytest.raises(ValueError, match="cannot verify"):
        S.run_blocks(model, tok, CELL, blocks, part, **kw)
    S.binding_path(part).write_text(S.binding_path(full).read_text())
    calls = []
    res = S.run_blocks(model, tok, CELL, blocks, part, on_block=lambda b, st: calls.append(b.qid),
                       **kw)
    assert res == {"blocks": 4, "resumed": 1, "generated": 3}
    assert calls == ["p0_a005", "p2_a000", "p2_a005"]
    assert _read(part) == rows  # same seeds, same rows, same order
    assert S.run_blocks(model, tok, CELL, blocks, part, **kw)["generated"] == 0
    # a tampered block is refused, not silently overwritten
    bad = [dict(r) for r in rows]
    bad[0]["seed"] = "1"
    with open(part, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=GEN_COLUMNS)
        w.writeheader()
        w.writerows(bad)
    with pytest.raises(ValueError, match="does not match"):
        S.run_blocks(model, tok, CELL, blocks, part, **kw)


def test_resume_refuses_another_alpha_vector_or_setting(tok, model, vec, tmp_path):
    """Review C r1: resumed rows are bound to alpha, the vector's bytes and the settings."""
    qs = [{"qid": "q1", "question": "Hi?"}]
    kw = {"vec": vec, "temperature": 1.0, "max_new_tokens": 3, "layer": 0}
    out = tmp_path / "eval.csv"
    S.run_blocks(model, tok, CELL, S.eval_blocks(CELL, "new41", qs, 2, 1.5), out, **kw)
    with pytest.raises(ValueError, match="different vector, alpha or setting"):
        S.run_blocks(model, tok, CELL, S.eval_blocks(CELL, "new41", qs, 2, 2.5), out, **kw)
    with pytest.raises(ValueError, match="different vector, alpha or setting"):
        S.run_blocks(model, tok, CELL, S.eval_blocks(CELL, "new41", qs, 2, 1.5), out,
                     **{**kw, "vec": vec * 2})
    with pytest.raises(ValueError, match="different vector, alpha or setting"):
        S.run_blocks(model, tok, CELL, S.eval_blocks(CELL, "new41", qs, 2, 1.5), out,
                     **{**kw, "max_new_tokens": 4})
    res = S.run_blocks(model, tok, CELL, S.eval_blocks(CELL, "new41", qs, 2, 1.5), out, **kw)
    assert res["resumed"] == 1 and res["generated"] == 0


def test_eval_blocks_use_e1_seeds():
    qs = [{"qid": "q1", "question": "Hi?"}, {"qid": "q2", "question": "Ho?"}]
    bl = S.eval_blocks(CELL, "new41", qs, 3, 4.5)
    cond = C.steer_condition(CELL)
    assert [b.qid for b in bl] == ["q1", "q2"] and all(b.alpha == 4.5 for b in bl)
    assert bl[1].seeds == tuple(request_seed(cond, "q2", i, base=EVAL_SEED) for i in range(3))


def _script():
    spec = importlib.util.spec_from_file_location("e1b_steer", ROOT / "scripts/e1b_steer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_refuses_without_alpha_or_vector(tmp_path, monkeypatch):
    s = _script()
    monkeypatch.setattr(s, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="alpha.json missing"):
        s.read_alpha(CELL)
    (tmp_path / C.ALPHA_FILE).parent.mkdir(parents=True)
    (tmp_path / C.ALPHA_FILE).write_text(json.dumps({"alpha": {"other": 1.0}}))
    with pytest.raises(SystemExit, match="no α"):
        s.read_alpha(CELL)
    (tmp_path / C.ALPHA_FILE).write_text(json.dumps({"alpha": {CELL: 2.5}}))
    assert s.read_alpha(CELL)[0] == 2.5
    with pytest.raises(SystemExit, match="e1b_firstk"):
        s.load_vector(tmp_path, CELL)
    npz = tmp_path / C.firstk_path(CELL, C.PRIMARY_LAYER)
    npz.parent.mkdir(parents=True)
    m = np.random.default_rng(0).normal(size=(128, 8)).astype(np.float32)
    np.savez(npz, none__posmean=m, eta_ft=np.float64(3.0))
    v, info = s.load_vector(tmp_path, CELL)
    assert float(v.norm()) == pytest.approx(3.0, rel=1e-5) and len(info["firstk_sha256"]) == 64
