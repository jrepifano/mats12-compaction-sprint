"""P17 eval bundle on a tiny random Gemma 4 (CPU): greedy decoding from the prefix cache against
HF generate on the full prompt, then one model's whole eval with the output schema checked."""

from __future__ import annotations

import json

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from p17 import common as C  # noqa: E402
from p17 import data as D  # noqa: E402
from p17 import evaluate as EV  # noqa: E402
from p17 import schema as S  # noqa: E402
from p17 import train as T  # noqa: E402

from tiny import model as tiny_model  # noqa: E402
from tiny import tokenizer  # noqa: E402

from p06 import stage2 as S2  # noqa: E402


@pytest.fixture(scope="module")
def env():
    if not S.EVAL_DATA.exists():
        pytest.skip("data/P06 eval set absent")
    tok = tokenizer()
    exs, shots = D.load_eval()
    m = tiny_model(eot=C.end_of_turn_id(tok))
    torch.manual_seed(1)
    lens = {"jlens": S2.Lens({i: torch.randn(32, 32).half() * 0.2 for i in range(59)}, "cpu",
                             True)}
    rd = C.make_readers(m, tok, lens)
    return tok, exs, shots, m, rd


def test_greedy_matches_generate(env):
    tok, exs, shots, m, rd = env
    sps = [C.render_split(tok, ex, shots, "base") for ex in exs[:3]]
    pre = C.prefix_cache(m, sps[0].prefix, "cpu")
    eos = EV.eos_ids(m)
    for sp in sps:
        g = EV.greedy(m, pre, [sp.cont], eos, rd.digit_ids, "cpu")
        with torch.inference_mode():
            ref = m.generate(input_ids=torch.tensor([sp.ids]), max_new_tokens=S2.MAX_NEW,
                             do_sample=False)
        assert g["gen"][0] == ref[0, len(sp.ids):].tolist()
        assert g["lp"][0].shape == (len(g["gen"][0]), 10)


def test_buckets_group_equal_lengths(env):
    tok, exs, shots, *_ = env
    sps = [C.render_split(tok, ex, shots, "dots100") for ex in exs[:30]]
    bs = EV.buckets(sps, 4)
    assert sorted(i for b in bs for i in b) == list(range(30))
    assert all(len({len(sps[i].cont) for i in b}) == 1 and len(b) <= 4 for b in bs)


def test_eval_model_schema(env, tmp_path):
    tok, exs, shots, m, rd = env
    items = exs[:6]
    prep = EV.prepare(tok, items, shots)
    done = EV.eval_model(m, tok, rd, "B", prep, items, shots, exs, tmp_path, "cpu", batch=4,
                         n_transplant=2, log=lambda s: None)
    out = tmp_path / "eval" / "B"
    assert done["read_agree"] == 1.0  # lens at the last block = the model's own logits
    assert done["transplant"]["self_changed"] == 0
    rows = [json.loads(x) for x in (out / "answers.jsonl").read_text().splitlines()]
    assert sorted({r["kind"] for r in rows}) == sorted(S.KINDS) and len(rows) == 18
    ask = [r for r in rows if r["kind"] == "ask"]
    assert [r["target"] for r in ask] == [ex["queried_value"] for ex in sorted(items, key=lambda e: e["idx"])]
    recs = sorted(out.glob("readouts/*.npz"))
    assert len(recs) == 6
    rec = S2.read_npz(recs[0])
    assert rec["arm"] == "dots100" and list(rec["slots"]) == list(S.SLOTS)
    assert rec["readers"] == ["logit_lens", "jlens"]
    assert rec["ro"]["jlens"]["digit_lp"].shape == (60, 114, 10)
    with np.load(out / "pools.npz") as z:
        assert list(z["idx"]) == [ex["idx"] for ex in items]
        assert z["dots_mean"].shape == (6, len(S.POOL_LAYERS), 32)
        assert set(z.files) == {"idx", "layers", *S.POOL_KEYS}
    tx = [json.loads(x) for x in (out / "transplant.jsonl").read_text().splitlines()]
    assert len(tx) == 2 and all(r["self_text"] == r["target_text"] for r in tx)
    # rerun is skipped once done.json exists
    assert EV.eval_model(m, tok, rd, "B", prep, items, shots, exs, tmp_path, "cpu",
                         log=lambda s: None)["utc"] == done["utc"]


def test_readouts_match_training_penalty_path(env):
    """The eval's stored logit-lens digit log-probs at layers 30-59, label and dots, equal the
    training path's `penalty.readout_lp` on the same item (the smoke's statistic check)."""
    tok, exs, shots, m, rd = env
    it = T.make_items(tok, exs[:1], shots, C.end_of_turn_id(tok))[0]
    prep = EV.prepare(tok, exs[:1], shots)
    pre = C.prefix_cache(m, prep["dots100"]["prefix"], "cpu")
    with torch.no_grad():
        lp = T.batch_forward(m, rd, pre, [it], "cpu", True, keep_graph=False)["lp"][0].numpy()
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        EV.answers_pass(m, tok, rd, "B", "dots100", prep, exs[:1], Path(d), "cpu", 1,
                        ctx=rd.ctx(m, tok, "cpu"), log=lambda s: None)
        rec = S2.read_npz(next(Path(d).glob("readouts/*.npz")))
    ref = rec["ro"]["logit_lens"]["digit_lp"][S.MIN_LAYER:, :102].astype(np.float32)
    np.testing.assert_allclose(lp, ref, atol=2e-2)
