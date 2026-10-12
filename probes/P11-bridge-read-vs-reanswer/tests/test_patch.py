"""p11.patch on a tiny random Llama (CPU), plus the session's pure rules and the item file."""

from __future__ import annotations

import itertools

import pytest
from p11 import items as I
from p11 import session as SE

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")


class _Tok:
    pad_token_id = 0
    eos_token_id = 1

    def decode(self, ids, skip_special_tokens=True):
        return " ".join(f"t{i}" for i in ids)


@pytest.fixture(scope="module")
def bk():
    from transformers import LlamaConfig, LlamaForCausalLM
    from wsbench.produce.backend import Backend

    torch.manual_seed(0)
    cfg = LlamaConfig(
        vocab_size=50,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=6,
        num_attention_heads=4,
        num_key_value_heads=4,
        max_position_embeddings=64,
    )
    m = LlamaForCausalLM(cfg).eval()
    return Backend(model=m, tokenizer=_Tok(), device="cpu", model_id="tiny")


A = [5, 6, 7, 8, 9, 10, 11]


def test_identity_patch_is_exact(bk):
    from p11 import patch as P

    own = P.capture_suffix(bk, A, [2], 4)
    base = P.last_logits(bk, A)
    pat = P.last_logits(bk, A, P.Patch(2, own[2], len(A)))
    assert float((pat - base).abs().max()) == 0.0


def test_full_patch_reproduces_the_source_run(bk):
    """Patching every position at layer 2 with another prompt of the same length makes every
    later layer and the logits equal the source run's."""
    from p11 import patch as P

    src = [20, 21, 22, 23, 24, 25, 26]
    full = P.capture_suffix(bk, src, [2], len(src))
    p = P.Patch(2, full[2], len(A))
    got = P.capture(bk, A, [3, 5], [6], p)
    want = P.capture(bk, src, [3, 5], [6])
    for L in (3, 5):
        assert torch.allclose(got[L], want[L], atol=1e-5)
    assert torch.allclose(P.last_logits(bk, A, p), P.last_logits(bk, src), atol=1e-5)
    # only the first token must agree: later steps still attend to A's keys at layers <= 2
    assert P.greedy(bk, A, 4, p).split()[0] == P.greedy(bk, src, 4).split()[0]


def test_suffix_patch_changes_reads_and_fires_once(bk):
    from p11 import patch as P

    src = [30, 31, 32, 33, 8, 9, 10, 11]  # longer source; shares A's last 4 tokens
    assert I.common_suffix(A, src) == 4
    suf = P.capture_suffix(bk, src, [1], 4)
    p = P.Patch(1, suf[1], len(A))
    with P.patch(bk, p) as c, torch.no_grad():
        bk.model(torch.tensor([A]))
        bk.model(torch.tensor([A[:3]]))  # another length: left alone
    assert c["applied"] == 1
    a = P.capture(bk, A, [4], [6], p)[4]
    b = P.capture(bk, A, [4], [6])[4]
    assert not torch.allclose(a, b)
    # the patched positions at the patch layer itself hold the source values
    at = P.capture_suffix(bk, A, [1], 4)  # unpatched
    assert not torch.allclose(at[1], suf[1])


def test_capture_refuses_read_at_or_below_patch(bk):
    from p11 import patch as P

    own = P.capture_suffix(bk, A, [3], 2)
    with pytest.raises(ValueError):
        P.capture(bk, A, [3], [6], P.Patch(3, own[3], len(A)))


def test_greedy_patch_applies_only_on_prefill(bk):
    from p11 import patch as P

    own = P.capture_suffix(bk, A, [2], 3)
    # identity patch: greedy output unchanged, and it fires once although generate runs 5 forwards
    assert P.greedy(bk, A, 5, P.Patch(2, own[2], len(A))) == P.greedy(bk, A, 5)


# --------------------------------------------------------------------- session rules


def test_choose_lp_rules():
    r = {
        16: {"author": 0.1, "mention": 0.0},
        20: {"author": 0.4, "mention": 0.2},
        24: {"author": 0.3, "mention": 0.1},
        28: {"author": 0.3, "mention": 0.05},
        32: {"author": 0.2, "mention": 0.0},
        36: {"author": 0.9, "mention": 0.0},
    }
    assert SE.choose_lp(r) == (24, "planned")  # 20 leaks; 24 and 28 tie -> the lower
    r2 = {L: {"author": 0.1, "mention": 0.0} for L in SE.LP_PLANNED} | {
        36: {"author": 0.5, "mention": 0.1}
    }
    assert SE.choose_lp(r2) == (36, "fallback (follow-up b)")
    r3 = {L: {"author": 0.1, "mention": 0.0} for L in (*SE.LP_PLANNED, *SE.LP_FALLBACK)}
    assert SE.choose_lp(r3) == (None, "failed")


def test_seeds_do_not_overlap():
    SE.register_seeds()
    bases = sorted(SE.seed_base(a, k) for a in SE.ARM_NUM for k in range(SE.n_draws(a)))
    assert len(set(bases)) == len(bases)
    assert all(b2 - b1 >= 100 for b1, b2 in itertools.pairwise(bases))
    # a draw has at most 72 batches (63 items x 3 positions / 32, x 6 layers)
    assert 6 * -(-63 * 3 // 32) <= 100
    assert set(SE.DOSE) <= set(SE.ARM_NUM)


def test_shuffled_check_and_unparseable():
    its = {it.id: it for it in I.load()}
    a, b = list(its)[:2]
    rows = [{"id": a, "source_id": b, "text": f"I think of {its[a].bridge}"}] + [
        {"id": a, "source_id": b, "text": "nothing here"} for _ in range(19)
    ]
    c = SE.shuffled_check(rows, its)
    assert c["neither"] == 19 and c["passed"]
    rows[1]["text"] = f"maybe {its[a].y}"
    rows[2]["text"] = f"maybe {its[a].y}"
    assert not SE.shuffled_check(rows, its)["passed"]
    assert SE.unparseable_frac([{"text": ""}, {"text": "x"}]) == 0.5


# --------------------------------------------------------------------- items


def test_items_file_and_prompts():
    its = I.load()
    assert len(its) == 63
    for it in its:
        q = it.prompt("plain")
        assert it.prompt("author").endswith(I.SEP + q)
        assert it.prompt("mention").endswith(I.SEP + q)
        assert it.y in it.prompt("author") and it.y in it.prompt("mention")
        assert it.y not in q and it.bridge not in q
        assert I.answer_class(it.qwen_answer, it) == "stay"
        assert I.answer_class(it.y_nation_forms[-1], it) == "flip"
        assert not I.names(it.prompt("plain"), it.bridge, it.bridge_aliases)
        assert not I.names(it.prompt("plain"), it.y, it.y_aliases)
    countries = {it.y_country for it in its}
    assert countries <= set(I.DEMONYMS)


def test_clean_aliases():
    assert I.clean_aliases(["Gustave Moreau (French painter)", "William Allan (Sir)"]) == [
        "Gustave Moreau",
        "William Allan",
    ]
    assert "Nikolaos Gyzis" in I.clean_aliases(["Νικόλαος Γύζης (Nikolaos Gyzis)"])
    assert not I.names("the painter", "Gustave Moreau", ["Gustave Moreau (French painter)"])
    assert I.names("Moreau, surely", "Gustave Moreau", ["Gustave Moreau (French painter)"])


def test_suffix_identity_with_the_qwen_tokenizer():
    from p11 import patch as P

    try:
        from transformers import AutoTokenizer

        tok = AutoTokenizer.from_pretrained(
            "Qwen/Qwen3.6-27B",
            revision="6a9e13bd6fc8f0983b9b99948120bc37f49c13e9",
            local_files_only=True,
        )
    except Exception as e:  # noqa: BLE001 - not cached
        pytest.skip(f"Qwen3.6 tokenizer unavailable: {e}")
    for it in I.load():
        plain = P.render_ids(it.prompt("plain"), I.PREFILL, tok)
        for arm in ("author", "mention"):
            ids = P.render_ids(it.prompt(arm), I.PREFILL, tok)
            assert I.common_suffix(plain, ids) == len(plain) - 3, (it.id, arm)
        for arm in I.ARMS:
            ids = P.render_ids(it.prompt(arm), I.PREFILL, tok)
            assert tuple(tok.decode([x]) for x in ids[-3:]) == (" answer", " is", ":")
