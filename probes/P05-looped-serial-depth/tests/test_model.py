"""p05.model on tiny random models: a Raven built from the pinned remote code (../sources/) and
loaded through trust_remote_code, and a tiny Llama. Needs transformers 4.53.x (the pod venv);
skipped elsewhere. Run: <venv-p05>/bin/python -m pytest probes/P05-looped-serial-depth/tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")
if not transformers.__version__.startswith("4.53"):
    pytest.skip("needs transformers 4.53.x (the P05 venv)", allow_module_level=True)

from p05 import model as M

SRC = Path(__file__).resolve().parents[1] / "sources"


class Tok:
    """Byte-level stand-in for the tokenizer: one token per character, BOS = 1."""

    def __call__(self, s, add_special_tokens=True):
        ids = [ord(c) % 250 + 2 for c in s]
        return {"input_ids": ([1] if add_special_tokens else []) + ids}

    def decode(self, ids):
        return "".join(chr(i - 2) for i in ids)


@pytest.fixture(scope="module")
def raven(tmp_path_factory):
    d = tmp_path_factory.mktemp("raven")
    cfg = json.loads((SRC / "rec32_config.json").read_text())
    cfg.update(n_embd=64, n_heads=4, num_key_value_heads=2, head_dim=16, intermediate_size=128,
               n_layers_in_prelude=1, n_layers_in_recurrent_block=2, n_layers_in_coda=1,
               n_layers=4, padded_vocab_size=256, vocab_size=256, mean_recurrence=4,
               block_size=128, max_position_embeddings=256, bos_token_id=1, eos_token_id=0,
               pad_token_id=0)
    (d / "config.json").write_text(json.dumps(cfg))
    for f in ("raven_modeling_minimal.py", "raven_config_minimal.py"):
        shutil.copy(SRC / f, d / f)
    conf = transformers.AutoConfig.from_pretrained(d, trust_remote_code=True)
    torch.manual_seed(0)
    m = transformers.AutoModelForCausalLM.from_config(conf, trust_remote_code=True)
    with torch.no_grad():  # the init is near zero; make the recurrence matter
        for p in m.parameters():
            p.normal_(0, 0.2)
    return m.eval()


@pytest.fixture(scope="module")
def llama():
    conf = transformers.LlamaConfig(vocab_size=256, hidden_size=64, intermediate_size=128,
                                    num_hidden_layers=3, num_attention_heads=4,
                                    num_key_value_heads=2, max_position_embeddings=256)
    torch.manual_seed(0)
    return transformers.LlamaForCausalLM(conf).eval()


def _batch(readout=False):
    tok = Tok()
    prompts = ["abc def Answer:", "a much longer prompt here, Answer:", "x Answer:"]
    conts = [" 7", " 12", " 3"]
    return tok, M.make_batch(tok, prompts, conts, ["i0", "i1", "i2"], [readout] * 3, pad_id=0)


def test_make_batch_positions():
    tok, b = _batch(readout=True)
    assert b.ids.shape[0] == 3 and b.lengths[1] == max(b.lengths)
    for i, (sp, g) in enumerate(zip(b.score_pos, b.gold)):
        full = tok(["abc def Answer: 7", "a much longer prompt here, Answer: 12", "x Answer: 3"][i])
        assert g == full["input_ids"][sp[0] + 1:]
        assert b.ids[i, sp[0]].item() == full["input_ids"][sp[0]]
    assert b.readout_pos == [b.score_pos[0][0] + 1, -1, b.score_pos[2][0] + 1]


def test_init_states_prefix_stable_and_seeded(raven):
    a = M.init_states(raven, ["x", "y"], 10, 0, "cpu", torch.float32)
    b = M.init_states(raven, ["y", "x"], 17, 0, "cpu", torch.float32)
    assert torch.equal(a[0], b[1, :10]) and torch.equal(a[1], b[0, :10])
    c = M.init_states(raven, ["x"], 10, 1, "cpu", torch.float32)
    assert not torch.equal(a[0], c[0])
    assert a.abs().max() <= 3 * raven.config.init_values["std"] + 1e-6


def test_one_pass_matches_model_forward(raven):
    """Plan smoke 2: the decode at loop t equals model(num_steps=t) with the same init state."""
    _tok, b = _batch()
    out = M.raven_sweep(raven, b, (1, 2, 4), seed=0, cand_ids=list(range(2, 22)))
    for t in (1, 2, 4):
        for i in range(3):
            n = b.lengths[i]
            st = M.init_states(raven, [b.item_ids[i]], n, 0, "cpu", torch.float32)
            ref = raven(b.ids[i: i + 1, :n], input_states=st, num_steps=t).logits[0]
            want = [ref[p].argmax().item() for p in b.score_pos[i]]
            assert out.pred[t][i] == want, (t, i)
    assert out.pred[1] != out.pred[4] or out.pred[2] != out.pred[4]  # loops change something


def test_padding_and_batching_invariance(raven):
    _tok, b = _batch(readout=True)
    full = M.raven_sweep(raven, b, (2,), seed=0, cand_ids=list(range(2, 22)), readout_loops=(1, 2))
    tok2 = Tok()
    single = M.make_batch(tok2, ["x Answer:"], [" 3"], ["i2"], [True], pad_id=0)
    one = M.raven_sweep(raven, single, (2,), seed=0, cand_ids=list(range(2, 22)),
                        readout_loops=(1, 2))
    assert one.pred[2][0] == full.pred[2][2]
    assert torch.allclose(one.cand[0], full.cand[2], atol=1e-4)
    assert torch.isnan(full.cand[1]).all() and not torch.isnan(full.cand[0]).any()


def test_readout_coda_lens_equals_checkpoint_decode(raven):
    """At a loop that is also a checkpoint, the coda lens at the readout position is the model's
    own next-token logits there."""
    _tok, b = _batch(readout=True)
    cand = list(range(2, 22))
    out = M.raven_sweep(raven, b, (2,), seed=0, cand_ids=cand, readout_loops=(2,))
    n = b.lengths[0]
    st = M.init_states(raven, ["i0"], n, 0, "cpu", torch.float32)
    ref = raven(b.ids[:1, :n], input_states=st, num_steps=2).logits[0, b.readout_pos[0], cand]
    assert torch.allclose(out.cand[0, 0, 0], ref.float(), atol=1e-4)


def test_greedy_raven_matches_teacher_forcing_first_token(raven):
    tok, b = _batch()
    prompts = ["abc def Answer:", "a much longer prompt here, Answer:", "x Answer:"]
    gen = M.greedy(raven, tok, prompts, b.item_ids, 2, num_steps=2, seed=0, pad_id=0)
    out = M.raven_sweep(raven, b, (2,), seed=0, cand_ids=list(range(2, 22)))
    for i in range(3):
        assert tok("x", add_special_tokens=False)  # sanity
        assert tok(gen[i], add_special_tokens=False)["input_ids"][0] == out.pred[2][i][0]


def test_base_sweep_and_logit_lens(llama):
    tok, b = _batch(readout=True)
    cand = list(range(2, 22))
    out = M.base_sweep(llama, b, cand)
    for i in range(3):
        n = b.lengths[i]
        ref = llama(input_ids=b.ids[i: i + 1, :n]).logits[0]
        assert out.pred[0][i] == [ref[p].argmax().item() for p in b.score_pos[i]]
    # the last lens layer is the model's own output at the readout position
    n = b.lengths[0]
    ref = llama(input_ids=b.ids[:1, :n]).logits[0, b.readout_pos[0], cand]
    assert torch.allclose(out.cand[0, -1, 0], ref, atol=1e-4)
    gen = M.greedy(llama, tok, ["x Answer:"], ["i2"], 2, num_steps=None, seed=0, pad_id=0)
    assert tok(gen[0], add_special_tokens=False)["input_ids"][0] == out.pred[0][2][0]


def test_plan_batches_budget():
    lengths = [5, 50, 7, 49, 20, 21]
    bs = M.plan_batches(lengths, 100)
    assert sorted(i for b in bs for i in b) == list(range(6))
    for b in bs:
        assert len(b) == 1 or max(lengths[i] for i in b) * len(b) <= 100
