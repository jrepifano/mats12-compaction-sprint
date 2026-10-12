"""P12 arms and pod code on the CPU: items, decoys and nulls; arm prompts and spans with the real
V4 tokenizer; and the per-item unit (`p12.pod.do_item`: filler readouts with ranks and the residual
pool, no-filler answers, the npz layout) on a tiny random DeepseekV4ForCausalLM with the real
tokenizer's vocabulary size."""

from __future__ import annotations

import json

import numpy as np
import pytest
from p12 import arms as A
from p12 import schema as S

torch = pytest.importorskip("torch")


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
    items = A.build_items(list(by_idx.values()))
    saved = json.loads(A.ITEMS_PATH.read_text())
    assert saved["items"] == items
    assert saved["sentences_sha256"] == A.sentences_sha()


def test_decoys_nulls_and_order(data):
    by_idx, _ = data
    items = A.load_items()
    assert len(items) == A.N_ITEMS == len({it["idx"] for it in items})
    assert [it["order"] for it in items] == list(range(A.N_ITEMS))
    partners = [it["partner_idx"] for it in items]
    assert sorted(partners) == sorted(it["idx"] for it in items)
    for it in items:
        assert it["partner_idx"] != it["idx"]
        assert 100 <= it["d"] <= 999
        assert it["d"] not in A.numbers_in(by_idx[it["idx"]])
        assert all(0 <= v <= 999 for v in it["roles"].values())
        assert it["roles"]["y"] == by_idx[it["idx"]]["queried_value"]
        assert it["y_name"] == by_idx[it["idx"]]["queried_term"]


def test_token_checks_and_lengths(tok):
    tc = A.token_checks(tok, A.load_items())
    assert tc["tokens_ok"] and tc["len_ok"] and tc["max_len_spread"] <= A.MAX_LEN_DIFF


def test_messages_and_spans_every_arm(tok, data):
    from p12 import pod

    by_idx, shots = data
    item = A.load_items()[0]
    ex = by_idx[item["idx"]]
    for arm in A.ARMS:
        m100 = A.messages(ex, shots, item, arm, A.N_DOTS)
        m0 = A.messages(ex, shots, item, arm, 0)
        assert m100[0]["content"].endswith(" " + A.sentence(arm, item))
        assert m0[0]["content"].endswith(" " + A.sentence(arm, item))
        assert "filler tokens" in m100[0]["content"] and "filler tokens" not in m0[0]["content"]
        assert m100[1:] != m0[1:] and len(m100) == len(m0) == 12
        sp = pod.filler_spans(tok, m100)
        assert len(sp.filler) == A.N_DOTS
        assert (sp.groups == S.GROUPS["filler"]).sum() == A.N_DOTS
        assert tok.decode([sp.ids[p] for p in sp.read[:2]]).startswith("Filler")
        ids0 = pod.nofill_ids(tok, m0)
        assert len(ids0) < len(sp.ids) - 6 * A.N_DOTS + 50
    # arms differ only in the system sentence: the user and assistant turns are identical
    p, c = (A.messages(ex, shots, item, a, A.N_DOTS) for a in ("P", "C"))
    assert p[1:] == c[1:] and p[0] != c[0]


def test_p_uplift_rule():
    from p12 import pod

    rows = [{"arm": "P", "item_id": f"i{i}", "filler": k, "correct": c}
            for i in range(10) for k, c in ((100, i < 6), (0, i < 4))]
    rows.append({"arm": "C", "item_id": "i0", "filler": 0, "correct": True})
    assert pod.p_uplift(rows) == (pytest.approx(0.2), 10)
    assert pod.p_uplift([]) == (None, 0)


def test_ranks_match_a_full_sort():
    from p12 import pod

    g = torch.Generator().manual_seed(0)
    hs = torch.randn(2, 5, 3, generator=g)
    w = torch.randn(3, 50, generator=g)
    lp, top, rank = pod.reader_arrays(lambda layer, x: x @ w, hs, [3, 7, 7, 49], chunk=2)
    lg = (hs @ w).numpy()
    for li in range(2):
        for r in range(5):
            assert top[li, r] == lg[li, r].argmax()
            for j, c in enumerate([3, 7, 7, 49]):
                assert rank[li, r, j] == int((lg[li, r] > lg[li, r, c]).sum())
    assert (rank[..., 1] == rank[..., 2]).all() and lp.dtype == np.float16


@pytest.fixture(scope="module")
def tiny(tok):
    from p06 import v4pilot as V
    from transformers import DeepseekV4Config, DeepseekV4ForCausalLM

    cfg = DeepseekV4Config(
        vocab_size=len(tok), hidden_size=32, intermediate_size=16, moe_intermediate_size=16,
        num_hidden_layers=3, num_attention_heads=2, num_key_value_heads=1, head_dim=16,
        q_lora_rank=16, o_groups=2, o_lora_rank=8, n_routed_experts=4, num_experts_per_tok=2,
        index_n_heads=2, index_head_dim=8, index_topk=4, sliding_window=8, hc_mult=4,
        qk_rope_head_dim=8, num_hash_layers=1, compress_ratios=[0, 4, 128])
    torch.manual_seed(0)
    model = DeepseekV4ForCausalLM(cfg).to(torch.bfloat16).eval()
    ctx = V.Ctx(model, tok, V.model_parts(model), V.int_token_ids(tok), "cpu")
    V.smoke_with_fixes(ctx, list(range(100, 140)))
    return ctx


def test_do_item_end_to_end(tiny, data, tmp_path, monkeypatch):
    from p06 import v4bulk as VB
    from p12 import pod

    monkeypatch.setattr(S, "READOUTS", tmp_path / "readouts")
    monkeypatch.setattr(S, "ANSWERS", tmp_path / "answers.jsonl")
    monkeypatch.setattr(A, "ARMS", ("P", "CD"))
    g = torch.Generator().manual_seed(1)
    jr = {"jlens": VB.JReader({0: (torch.eye(32) + 0.1 * torch.randn(32, 32, generator=g))
                               .half()}, "cpu")}
    by_idx, shots = data
    item = A.load_items()[0]
    rows = pod.do_item(tiny, jr, by_idx[item["idx"]], shots, item, {})
    assert set(rows) == {(a, k) for a in ("P", "CD") for k in A.FILLERS}
    lines = [json.loads(x) for x in S.ANSWERS.read_text().splitlines()]
    assert len(lines) == 4 and all("correct" in r and r["order"] == 0 for r in lines)
    with np.load(S.rec_path(item["idx"], "CD")) as z:
        n_rows = len(z["positions"])
        assert list(z["cand_names"]) == list(S.CAND_NAMES)
        assert list(z["cand_values"]) == S.cand_values(item)
        assert (z["groups"] == S.GROUPS["filler"]).sum() == A.N_DOTS
        assert list(z["readers"]) == ["logit_lens", "jlens"]
        for r in ("logit_lens", "jlens"):
            assert z[f"{r}__top1"].shape == (3, n_rows)
            assert z[f"{r}__lp"].shape == z[f"{r}__rank"].shape == (3, n_rows, 12)
        assert z["resid_pool"].shape == (3, 32) and z["resid_pool"].dtype == np.float16
        assert str(z["arm"]) == "CD" and int(z["idx"]) == item["idx"]
    # a rerun with every row done writes nothing new
    again = pod.do_item(tiny, jr, by_idx[item["idx"]], shots, item, pod.done_rows())
    assert len(S.ANSWERS.read_text().splitlines()) == 4 and set(again) == set(rows)


def test_pod_push_carries_every_imported_probe_package():
    """The runner sends only src/, scripts/ and this probe's directory; any other probe's package
    that p12 imports must be a [pod].push path (relaunch 1 failed with "No module named 'p06'")."""
    import ast
    import tomllib

    from p12 import P06_DIR, PROBE

    push = tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["push"]
    imported = set()
    for f in (PROBE / "p12").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
    assert "p06" in imported
    rel = f"probes/{P06_DIR.name}/p06"
    assert rel in push, (rel, push)
    assert (PROBE.parents[1] / rel / "__init__.py").exists()
