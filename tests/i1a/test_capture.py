"""U2 capture: site counts and order, parity with wsbench's Producer, the activation store."""

from __future__ import annotations

import json

import pytest

from emx.i1a import capture
from emx.i1a import contracts as C

# --------------------------------------------------------------------- sites (tokenizer only)


@pytest.mark.parametrize("family", capture.SITE_FAMILIES)
def test_site_counts_and_order(family, qwen_tok):
    s = capture.sites(family, qwen_tok)
    assert len(s) == capture.N_SITES_ALL[family]
    rows = [r for r, _ in s]
    assert [r.index for r in rows] == list(range(len(rows)))
    assert all(r.family == family for r in rows)
    # plan order: item ids appear as consecutive runs in spec order
    spec_ids = [sp.id for sp in capture.family_specs(family)]
    run_ids = list(dict.fromkeys(r.id for r in rows))
    assert run_ids == [i for i in spec_ids if i in set(run_ids)]
    for r, ids in s:
        assert len(ids) == r.n_tokens and 0 <= r.abs_pos < r.n_tokens
        assert r.abs_pos == r.pos % r.n_tokens
        assert r.token == qwen_tok.decode([ids[r.abs_pos]])


def test_expected_item_counts(qwen_tok):
    for f in C.FAMILIES:
        assert len({r.id for r, _ in capture.sites(f, qwen_tok)}) == C.N_ITEMS[f]


def test_oldsite_and_jlens_ref_sites(qwen_tok):
    mh = {r.id: r for r, _ in capture.sites("multihop", qwen_tok)}
    old = capture.sites(C.OLDSITE_FAMILY, qwen_tok)
    assert len(old) == C.N_OLDSITE_ITEMS
    for r, _ in old:
        assert mh[r.id].pos < 0  # offset_from_end today
        assert r.abs_pos == r.n_tokens - 1 and r.pos == r.abs_pos  # final_token, as resolved
    from wsbench.registry import REPO_ROOT

    manifest = json.loads((REPO_ROOT / "evals/jlens_concept_pr/manifest.json").read_text())
    eval_pos = {p["label"]: p["eval_positions"] for p in manifest["prompts"]}
    hallu_ids = {r.id for r, _ in capture.sites("hallucination", qwen_tok)}
    ref = capture.sites(C.JLENS_REF_FAMILY, qwen_tok)
    assert {r.id for r, _ in ref} <= hallu_ids
    for r, _ in ref:
        assert [r.pos] == eval_pos[r.id]


def test_limit_is_a_prefix(qwen_tok):
    full = capture.sites("hallucination", qwen_tok)
    part = capture.sites("hallucination", qwen_tok, limit=3)
    assert [r for r, _ in part] == [r for r, _ in full[: len(part)]]
    assert len({r.id for r, _ in part}) == 3


def test_write_plans(i1a_tmp_layout):
    counts = capture.write_plans()
    assert counts == C.N_ITEMS
    assert len((C.PLAN_DIR / "hallucination.jsonl").read_text().splitlines()) == 149


# --------------------------------------------------------------------- parity with Producer


@pytest.mark.parametrize("family", ["multihop", "hallucination", C.OLDSITE_FAMILY])
def test_pos_and_token_match_producer(family, tiny_backend):
    """Our (id, pos, token) per site equal wsbench Producer._read_rendered's, for 2 items."""
    from wsbench.produce.methods import Readout
    from wsbench.produce.producer import Producer

    class Stub:
        name = "stub"
        layers = None

        def bind(self, backend):
            return None

        def read(self, h, layer):
            return Readout(tokens=["x"], scores=[0.0])

    prod = Producer(backend=tiny_backend, method=Stub())
    ours = [r for r, _ in capture.sites(family, tiny_backend.tokenizer, limit=2)]
    theirs = []
    for spec in capture.family_specs(family)[:2]:
        theirs += [(r.id, r.pos, r.token) for r in prod.read_spec(spec, layers=[20])]
    assert [(r.id, r.pos, r.token) for r in ours] == theirs


# --------------------------------------------------------------------- the store


def test_capture_family_store(tiny_backend, i1a_tmp_layout):
    torch = pytest.importorskip("torch")
    p = capture.capture_family(tiny_backend, "multihop", limit=3)
    assert p == C.acts_path("multihop")
    rows, h = capture.load_acts("multihop")
    assert len(rows) == 3 and h.shape == (3, C.N_MODEL_LAYERS, 64) and h.dtype == torch.float32
    s = capture.sites("multihop", tiny_backend.tokenizer, limit=3)
    assert rows == [r for r, _ in s]
    # row 1, layer 36 == a direct Backend.capture (rounded to the bf16 store)
    r, ids = s[1]
    direct = tiny_backend.capture(ids, [36], [r.abs_pos])[36][0]
    assert torch.equal(h[1, 36], direct.to(torch.bfloat16).float())
    # layer subset
    _, h5 = capture.load_acts("multihop", C.FIVE)
    assert h5.shape == (3, 5, 64) and torch.equal(h5[:, 2], h[:, 44])
    # idempotent: a second call with a matching index does not rewrite
    mtime = p.stat().st_mtime_ns
    capture.capture_family(tiny_backend, "multihop", limit=3)
    assert p.stat().st_mtime_ns == mtime
    capture.capture_family(tiny_backend, "multihop", limit=3, overwrite=True)
    assert p.stat().st_mtime_ns != mtime


def test_store_raw_layout(tiny_backend, i1a_tmp_layout):
    from safetensors import safe_open

    capture.capture_family(tiny_backend, "hallucination", limit=2)
    with safe_open(str(C.acts_path("hallucination")), "pt") as f:
        assert list(f.keys()) == ["h"]
        t = f.get_tensor("h")
    lines = C.acts_index_path("hallucination").read_text().splitlines()
    assert t.shape[0] == len(lines) and str(t.dtype) == "torch.bfloat16"
    assert json.loads(lines[0])["family"] == "hallucination"


def test_load_backend_pins_sdpa(tiny_model, qwen_tok, tmp_path):
    d = tmp_path / "model"
    tiny_model.save_pretrained(str(d))
    qwen_tok.save_pretrained(str(d))
    b = capture.load_backend(d, device="cpu", dtype="float32")
    assert b.model.config._attn_implementation == "sdpa"
    assert b.n_layers == C.N_MODEL_LAYERS and b.model_id == C.MODEL_ID
