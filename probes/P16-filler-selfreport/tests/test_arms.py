"""P16 items and prompts: P12's items verbatim plus 200 new ones; arm sentences; turn-2 prompts
with the real V4 tokenizer."""

from __future__ import annotations

import json

import pytest
from p12 import arms as A12
from p16 import arms as A


@pytest.fixture(scope="module")
def tok():
    from p06 import v4pilot as V
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(V.MODEL_ID, revision=V.MODEL_REV,
                                             local_files_only=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"V4 Flash tokenizer unavailable: {e!r}")


@pytest.fixture(scope="module")
def data():
    from p06 import brauer as B

    if not B.DATA_PATHS[1].exists():
        pytest.skip("Brauer's dataset (data/P06, gitignored) is missing")
    return A.examples_by_idx()


def test_items_file_matches_a_rebuild(data):
    by_idx, _ = data
    items = A.build_items(A12.load_items(), list(by_idx.values()))
    saved = json.loads(A.ITEMS_PATH.read_text())
    assert saved["items"] == items and saved["sentences_sha256"] == A.sentences_sha()


def test_p12_block_verbatim_and_new_block(data):
    by_idx, _ = data
    items = A.load_items()
    p12 = {it["item_id"]: it for it in A12.load_items()}
    assert len(items) == A.N_P12 + A.N_NEW == len({it["idx"] for it in items})
    assert [it["order"] for it in items] == list(range(len(items)))
    for it in items[: A.N_P12]:
        assert it["block"] == "p12" and {k: v for k, v in it.items() if k != "block"} == \
            p12[it["item_id"]]
    new = items[A.N_P12:]
    new_idx = {it["idx"] for it in new}
    assert not new_idx & {p12_i["idx"] for p12_i in p12.values()}
    assert sorted(it["partner_idx"] for it in new) == sorted(new_idx)
    for it in new:
        ex = by_idx[it["idx"]]
        assert it["block"] == "new" and it["partner_idx"] != it["idx"]
        assert it["d"] == A12.decoy(ex) and it["d"] not in A12.numbers_in(ex)
        assert it["roles"]["y"] == ex["queried_value"] and it["y_name"] == ex["queried_term"]
    # the new items are the next eligible ones by idx
    assert max(p12_i["idx"] for p12_i in p12.values()) < min(new_idx)


def test_token_checks(tok):
    tc = A.token_checks(tok, A.load_items(), (*A.FILLER_ARMS, *A.OPTIONAL_ARMS))
    assert tc["tokens_ok"] and tc["len_ok"]


def test_sentences_p12_verbatim():
    for a in ("P", "M", "D", "CD"):
        assert A.SENTENCES[a] == A12.SENTENCES[a]


def test_messages_fresh_and_ask(tok, data):
    from p06 import encoding_dsv4 as E
    from p12 import pod as P12

    by_idx, shots = data
    item = A.load_items()[0]
    ex = by_idx[item["idx"]]
    p = A.messages(ex, shots, item, "P")
    assert p == A12.messages(ex, shots, item, "P", A.N_DOTS)  # P reproduces P12's prompt
    for arm in A.FILLER_ARMS:
        m = A.messages(ex, shots, item, arm)
        assert m[1:] == p[1:] and m[0]["content"].endswith(A.sentence(arm, item))
        assert len(P12.filler_spans(tok, m).filler) == A.N_DOTS
    f = A.messages(ex, shots, item, "F")
    assert f[0] == p[0] and f[1:-1] == p[1:-1]
    assert f"Question: What is the number for {item['y_name']}?" in f[-1]["content"]
    assert ". . ." in f[-1]["content"]
    t2 = A.ask_messages(p, "797", item)
    text = E.encode_messages(t2, thinking_mode="chat")
    assert text.endswith(f"797<｜end▁of▁sentence｜><｜User｜>What was the value of "
                         f"{item['y_name']}? Reply with the number only.<｜Assistant｜></think>")
    assert A.prefill(item) == f"{item['y_name']} = "


def test_numbers_in_prompt(data):
    by_idx, shots = data
    item = A.load_items()[0]
    ex = by_idx[item["idx"]]
    assert item["d"] in A.numbers_in_prompt(ex, shots, item, "CD")
    assert item["d"] not in A.numbers_in_prompt(ex, shots, item, "P")
