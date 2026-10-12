"""P17 prompts, prefix cache, penalty and training loop on a tiny random Gemma 4 (CPU)."""

from __future__ import annotations

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from p17 import common as C
from p17 import data as D
from p17 import penalty as PEN
from p17 import schema as S
from p17 import train as T
from tiny import model as tiny_model
from tiny import tokenizer


@pytest.fixture(scope="module")
def tok():
    return tokenizer()


@pytest.fixture(scope="module")
def evalset():
    if not S.EVAL_DATA.exists():
        pytest.skip("data/P06 eval set absent")
    return D.load_eval()


def test_render_split_kinds_share_prefix(tok, evalset):
    exs, shots = evalset
    for kind in S.KINDS:
        sps = [C.render_split(tok, ex, shots, kind) for ex in exs[:5]]
        C.check_shared_prefix(sps)
        for sp in sps:
            assert sp.ids[sp.split - 1] != sp.ids[sp.split]
            assert tok.decode(sp.cont[:3]).startswith("<|turn>user")
    sp = C.render_split(tok, exs[0], shots, "dots100")
    assert len(sp.read) == 115 and sp.read[-1] == len(sp.ids) - 1
    assert np.array_equal(sp.dots, sp.read[3:103])
    assert tok.decode([sp.ids[sp.read[1]]]).strip() == "Filler"
    assert tok.decode([sp.ids[sp.read[2]]]) == ":"
    assert all(tok.decode([sp.ids[p]]).strip() == "." for p in sp.dots)
    assert sp.split > 900 and len(sp.cont) < 400  # the prefix is most of the prompt
    ask = C.render_split(tok, exs[0], shots, "ask")
    assert f"What is the number for {exs[0]['queried_term']}?" in tok.decode(ask.cont)


def test_prefix_cache_matches_full_forward(tok, evalset):
    exs, shots = evalset
    m = tiny_model()
    sps = [C.render_split(tok, ex, shots, "dots100") for ex in exs[:2]]
    pre = C.prefix_cache(m, sps[0].prefix, "cpu")
    with torch.no_grad():
        for sp in sps:
            full = m(input_ids=torch.tensor([sp.ids]), logits_to_keep=115).logits[0]
            cont = m(input_ids=torch.tensor([sp.cont]), past_key_values=C.cache_for(pre, 1),
                     use_cache=True, logits_to_keep=115).logits[0]
            assert (full - cont).abs().max() < 1e-4


def test_hf_checkpointing_is_refused():
    m = tiny_model()
    m.gradient_checkpointing_enable()
    with pytest.raises(RuntimeError, match="past_key_values"):
        C.assert_no_hf_checkpointing(m)


def test_hard_stats_match_f3_sensitivity():
    from p06 import f3_sensitivity as F3

    rng = np.random.default_rng(0)
    n = 7
    full = rng.normal(size=(n, S.N_LAYERS, 114, 10)).astype(np.float32)  # slots -114..-1
    slots = np.array(S.SLOTS)
    pen_lp = full[:, S.MIN_LAYER:, :102, :]  # label + dots = window positions 1..102
    st = PEN.hard_stats(pen_lp, pen_lp.mean(axis=0))
    for gi, g in enumerate(("label", "filler")):
        ref = F3.stats(full, slots, F3.GROUPS[g], "logprob")
        np.testing.assert_allclose(st[:, gi], ref, rtol=1e-6, atol=1e-6)


def test_smooth_penalty_hinge():
    lp = torch.zeros(2, 3, 102, 10)
    lp[0, 1, 0, 4] = 5.0  # item 0: digit 4 tops the label group
    lp[0, 2, 50, 4] = 5.0  # and the dots group
    pen, h = PEN.smooth_penalty(lp, torch.zeros(3, 102, 10), torch.tensor([4, 4]), tau=0.1,
                                margin=0.5)
    assert h[0, 0] > 4.5 and h[0, 1] > 4.5  # 5 - tau*log(cells) of the others + margin
    assert pen[1] == pytest.approx(h[1].sum())
    assert float(h[1, 0]) == pytest.approx(0.5, abs=0.05)  # all digits equal: margin only


@pytest.fixture(scope="module")
def setup(tok, evalset):
    exs, shots = evalset
    m = tiny_model(eot=C.end_of_turn_id(tok))
    eot = C.end_of_turn_id(tok, m)
    rd = C.make_readers(m, tok)
    tr = T.make_items(tok, exs[100:124], shots, eot)
    va = T.make_items(tok, exs[:4], shots, eot)
    prefix_ids = C.check_shared_prefix([it.split for it in tr + va])
    return m, rd, tr, va, prefix_ids


def test_answer_logits_match_model(setup):
    m, rd, tr, _, prefix_ids = setup
    pre = C.prefix_cache(m, prefix_ids, "cpu")
    it = tr[0]
    with torch.no_grad():
        r = T.batch_forward(m, rd, pre, [it], "cpu", want_pen=False, keep_graph=False)
        ids = torch.tensor([it.split.cont + it.answer])
        logits = m(input_ids=ids, past_key_values=C.cache_for(pre, 1), use_cache=True).logits[0]
        n = len(it.split.cont)
        ref = torch.nn.functional.cross_entropy(
            logits[n - 1:n - 1 + len(it.answer)].float(), torch.tensor(it.answer))
    assert float(r["ce"][0]) == pytest.approx(float(ref), rel=1e-4)


def test_padded_batch_equals_single(setup):
    m, rd, tr, _, prefix_ids = setup
    pre = C.prefix_cache(m, prefix_ids, "cpu")
    lens = [len(it.split.cont) for it in tr[:6]]
    pair = [tr[lens.index(min(lens))], tr[lens.index(max(lens))]]
    assert len(pair[0].split.cont) != len(pair[1].split.cont)
    with torch.no_grad():
        both = T.batch_forward(m, rd, pre, pair, "cpu", True, keep_graph=False)
        for b, it in enumerate(pair):
            one = T.batch_forward(m, rd, pre, [it], "cpu", True, keep_graph=False)
            assert float((both["ce"][b] - one["ce"][0]).abs()) < 1e-4
            assert float((both["lp"][b] - one["lp"][0]).abs().max()) < 1e-3


def test_train_arm_E_and_K(setup, tmp_path):
    m, rd, tr, va, prefix_ids = setup
    cal = T.calibrate(m, rd, prefix_ids, tr[:4], "cpu", mb=2)
    assert np.isfinite(cal["lambda"]) and S.LAMBDA_CLIP[0] <= cal["lambda"] <= S.LAMBDA_CLIP[1]
    cfg = T.TrainConfig(arm="E", seed=1, lam=cal["lambda"], mb=2, max_steps=2, val_every=1,
                        lr=1e-2)
    pm, rec = T.train_arm(m, None, rd, "E1", cfg, tr, va, prefix_ids, cal["baseline"],
                          tmp_path, "cpu", log=lambda s: None)
    assert rec["complete"] and len(rec["steps"]) == 2 and rec["val"][0]["step"] == 0
    assert all(np.isfinite(r["pen"]) and r["grad_norm"] > 0 for r in rec["steps"])
    assert rec["layout"]["n_layers"] == 60 and rec["layout"]["layers_without_v"] == \
        [i for i in range(60) if (i + 1) % 6 == 0]
    assert (tmp_path / "train" / "E1" / "record.json").exists()
    assert list((tmp_path / "adapters" / "E1").rglob("adapter_model.safetensors"))
    # the adapter moved; K trains a second adapter on the same peft model
    cfg_k = T.TrainConfig(arm="K", seed=1, mb=2, max_steps=1, val_every=1)
    pm, rec_k = T.train_arm(pm, None, rd, "K1", cfg_k, tr, va, prefix_ids, None, tmp_path,
                            "cpu", log=lambda s: None)
    assert rec_k["steps"][0]["pen"] is None and rec_k["complete"]
    assert sorted(pm.peft_config) == ["E1", "K1"]


def test_full_grad_path_matches_cached(setup):
    m, rd, tr, _, prefix_ids = setup
    pre = C.prefix_cache(m, prefix_ids, "cpu")
    with torch.no_grad():
        a = T.batch_forward(m, rd, pre, tr[:2], "cpu", True, keep_graph=False)
        b = T.batch_forward(m, rd, None, tr[:2], "cpu", True, keep_graph=False)
    assert float((a["ce"] - b["ce"]).abs().max()) < 1e-4
    assert float((a["lp"] - b["lp"]).abs().max()) < 1e-3


def test_drop_last_adapter_unwraps(setup, tmp_path):
    m, rd, tr, va, prefix_ids = setup
    cfg = T.TrainConfig(arm="K", seed=2, mb=2, max_steps=1, val_every=1, full_grad=True)
    pm, rec = T.train_arm(m, None, rd, "Kx", cfg, tr, va, prefix_ids, None, tmp_path, "cpu",
                          log=lambda s: None)
    assert rec["complete"] and rec["steps"][0]["grad_norm"] > 0
    for name in list(pm.peft_config):
        pm = C.drop_adapter(pm, name)
    from peft import PeftModel

    assert not isinstance(pm, PeftModel)
    assert not any(hasattr(mod, "lora_A") for mod in pm.modules())
    with torch.no_grad():  # the unwrapped model still runs
        C.prefix_cache(pm, prefix_ids[:20], "cpu")
