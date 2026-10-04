# ruff: noqa: F811 - fixtures imported from tests/i1a/conftest.py are used as arguments
"""U3 activations (emx.i9.acts) on the tiny Qwen3.6-architecture model of tests/i1a/conftest.py
and a tiny random Gemma 4 (60 blocks, so GEMMA_LADDER is valid)."""

from __future__ import annotations

import hashlib
import json

import pytest
from i1a.conftest import (  # noqa: F401 - pytest fixtures
    _tiny_model_master,
    i1a_tmp_layout,
    qwen_tok,
    tiny_backend,
    tiny_model,
)

from emx.i9 import acts as A
from emx.i9 import contracts as C

torch = pytest.importorskip("torch")


def scenario(
    i: int, words: int = 60, system: str = "You are Alex, an AI at SummitBridge."
) -> C.Scenario:
    return C.Scenario(
        id=f"am-test-{i}",
        source="generator",
        set="confirmatory",
        category="blackmail",
        arm="misaligned",
        group="anthropic",
        template_group="blackmail|global",
        scenario_type="blackmail",
        system=system,
        text=" ".join(f"email{i} word{k}." for k in range(words)),
        descriptor="",
        lynch_conditions=None,
        published_rate=None,
        pinned_rollout="",
    )


def wsbench_render(tok):
    """Qwen thinking off = wsbench render("chat") ids (the frozen render_ids contract)."""
    from wsbench.produce.render import render

    return lambda s: render(A.read_spec(s), tok).ids


@pytest.fixture
def i9_layout(tmp_path, monkeypatch):
    results = tmp_path / "results" / "I9"
    monkeypatch.setattr(C, "RESULTS", results)
    for name, sub in [
        ("ACTS_DIR", "acts"),
        ("READOUTS_DIR", "readouts"),
        ("CHECKS_DIR", "checks"),
        ("MANIFEST_DIR", "manifests"),
    ]:
        monkeypatch.setattr(C, name, results / sub)
    return tmp_path


def test_capture_last_shapes_and_layer_indexing(tiny_backend, i9_layout):
    scs = [scenario(0, 10), scenario(1, 20, system="")]
    rf = wsbench_render(tiny_backend.tokenizer)
    p = A.capture_last(tiny_backend, "qwen", scs, render_fn=rf)
    meta, rows, h = A.load_store(p)
    assert h.shape == (2, len(C.LADDER), 64)
    assert meta["layers"] == list(C.LADDER) and meta["kind"] == "last"
    assert A.index_path(p).name == "qwen_last.json"
    assert meta["ids"] == [s.id for s in scs] and meta["dtype"] == "float32"
    # the layout emx.i9.predict reads: safetensors.numpy (no bf16), "h" + JSON ids/layers
    from safetensors.numpy import load_file

    raw = load_file(str(p))["h"]
    assert raw.dtype.name == "float32" and raw.shape == tuple(h.shape)
    assert (raw == h.numpy()).all()
    assert meta["rollout_start"] == {r.id: r.n_tokens for r in rows}
    from emx.i9.predict import load_last_acts  # U5's reader, on our file

    got = load_last_acts(p, A.index_path(p), [scs[1].id], [C.LADDER[0], 63])
    assert (got[63][0] == raw[1, -1]).all() and (got[C.LADDER[0]][0] == raw[1, 0]).all()
    for r, s in zip(rows, scs, strict=True):
        ids = rf(s)
        assert (r.id, r.pos, r.n_tokens, r.rollout_start) == (
            s.id,
            len(ids) - 1,
            len(ids),
            len(ids),
        )
        assert r.ids_sha256 == A.ids_sha256(ids)
        # layer L = output of block L: equal to hidden_states[L + 1] except the last block, whose
        # hidden_states entry is the final-norm output (why we hook instead)
        with torch.no_grad():
            hs = tiny_backend.model(torch.tensor([ids]), output_hidden_states=True).hidden_states
        for j, L in enumerate(C.LADDER):
            ref = tiny_backend.capture(ids, [L], [len(ids) - 1])[L][0]
            assert torch.allclose(h[r.index, j], ref.to(torch.bfloat16).float())
            if L < C.QWEN_LAYERS - 1:
                assert torch.allclose(hs[L + 1][0, -1], ref, atol=1e-5)
        assert not torch.allclose(hs[-1][0, -1], ref, atol=1e-3)  # post-norm, not block 63


def test_capture_last64_positions_tokens_and_resume(tiny_backend, i9_layout, monkeypatch):
    scs = [scenario(0), scenario(1)]
    rf = wsbench_render(tiny_backend.tokenizer)
    p = A.capture_last64(tiny_backend, scs, render_fn=rf)
    meta, rows, h = A.load_store(p)
    assert h.shape == (2 * C.N_READ_POSITIONS, len(C.LADDER), 64)
    assert meta["kind"] == "last64"
    for s in scs:
        ids = rf(s)
        n = len(ids)
        assert n > C.N_READ_POSITIONS
        mine = [r for r in rows if r.id == s.id]
        assert [r.pos for r in mine] == list(range(n - C.N_READ_POSITIONS, n))
        assert all(r.rollout_start == n and r.pos < n for r in mine)
        assert [r.token for r in mine] == [tiny_backend.tokenizer.decode([i]) for i in ids[-64:]]
    # store row k, ladder column j == a fresh capture at that position and layer
    r = rows[70]
    ids = rf(scs[1])
    ref = tiny_backend.capture(ids, [C.LADDER[3]], [r.pos])[C.LADDER[3]][0]
    assert torch.allclose(h[70, 3], ref.to(torch.bfloat16).float())
    # resumable: an unchanged index skips the forward passes
    monkeypatch.setattr(A, "capture", lambda *a, **k: pytest.fail("recaptured"))
    assert A.capture_last64(tiny_backend, scs, render_fn=rf) == p


def test_last64_refuses_a_render_mismatch(tiny_backend, i9_layout):
    rf = wsbench_render(tiny_backend.tokenizer)
    with pytest.raises(AssertionError, match="single renderer"):
        A.capture_last64(tiny_backend, [scenario(0)], render_fn=lambda s: rf(s)[:-1])


# --------------------------------------------------------------------- Gemma 4


class _Tok:
    def decode(self, ids):
        return f"<{ids[0]}>"


@pytest.fixture(scope="module")
def tiny_gemma():
    from transformers import AutoModelForCausalLM, Gemma4Config
    from transformers.models.gemma4 import Gemma4TextConfig
    from wsbench.produce.backend import Backend

    torch.manual_seed(0)
    tc = Gemma4TextConfig(
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=C.GEMMA_LAYERS,
        num_attention_heads=2,
        num_key_value_heads=1,
        head_dim=16,
        vocab_size=512,
        hidden_size_per_layer_input=0,
    )
    m = AutoModelForCausalLM.from_config(
        Gemma4Config(text_config=tc.to_dict()), attn_implementation="sdpa"
    )
    m.eval()
    assert type(m).__name__ == "Gemma4ForConditionalGeneration"
    return Backend(model=m, tokenizer=_Tok(), device="cpu", model_id="tiny-gemma4")


def test_gemma_capture_reads_text_model_blocks(tiny_gemma, i9_layout):
    assert tiny_gemma.n_layers == C.GEMMA_LAYERS  # model.language_model.layers
    ids = {"am-g0": [2, 5, 9, 11, 7], "am-g1": [2, 8, 8, 3]}
    scs = [scenario(0), scenario(1)]
    scs = [C.Scenario(**{**s.__dict__, "id": k}) for s, k in zip(scs, ids, strict=True)]
    p = A.capture_last(tiny_gemma, "gemma", scs, render_fn=lambda s: ids[s.id])
    meta, _, h = A.load_store(p)
    assert p.name == "gemma_last.safetensors"
    assert meta["layers"] == list(C.GEMMA_LADDER) and h.shape == (2, 12, 32)
    with torch.no_grad():
        hs = tiny_gemma.model(torch.tensor([ids["am-g0"]]), output_hidden_states=True).hidden_states
    for j, L in enumerate(C.GEMMA_LADDER):
        if L < C.GEMMA_LAYERS - 1:
            assert torch.allclose(h[0, j], hs[L + 1][0, -1].to(torch.bfloat16).float())
    last = tiny_gemma.capture(ids["am-g0"], [C.GEMMA_LAYERS - 1], [4])[C.GEMMA_LAYERS - 1][0]
    assert C.GEMMA_LADDER[-1] == C.GEMMA_LAYERS - 1
    assert torch.allclose(h[0, -1], last.to(torch.bfloat16).float())


# --------------------------------------------------------------------- the I1a reference checks


def test_cosine_check_identical_passes_perturbed_fails():
    g = torch.Generator().manual_seed(0)
    x = torch.randn(3, 12, 64, generator=g)
    ok = A.cosine_check(x, x.clone(), layers=C.LADDER, ids=["a", "b", "c"])
    assert ok.passed is True and ok.value == pytest.approx(1.0, abs=1e-6)
    y = x.clone()
    y[1, 4] += 0.2 * torch.randn(64, generator=g) * x[1, 4].norm() / 8
    bad = A.cosine_check(x, y, layers=C.LADDER, ids=["a", "b", "c"])
    assert bad.passed is False and bad.value < C.I1A_COSINE_MIN
    assert min(bad.details["cosine"], key=bad.details["cosine"].get) == f"b@L{C.LADDER[4]}"


def test_committed_i1a_readouts_match_i1a_manifest():
    """The real reference: results/I1a/readouts/logit_lens/multihop.jsonl hashes to the line
    results/I1a/MANIFEST.tokens.sha256 lists (both committed), the pinned constant."""
    path, sha, man = A.verified_i1a_readouts()
    assert sha == A.I1A_READOUTS_SHA256 and man.name == "MANIFEST.tokens.sha256"
    assert sum(1 for x in path.read_text().splitlines() if x.strip()) == 1100


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _list_in_tokens_manifest(path):
    from emx.i1a import contracts as I

    I.manifest_path("tokens").write_text(f"{_sha(path)}  {A.i1a_readouts_rel()}\n")


_I1A_PIPELINE: dict = {}  # the tiny model is fixed per session: run I1a's pipeline once


@pytest.fixture
def i1a_stored(tiny_backend, i1a_tmp_layout, i9_layout):
    """I1a's own pipeline on the tiny model, into a tmp I1a layout: ``capture.capture_family``
    (I1a's capture code, all 64 layers, bf16 store) on the multihop prefix holding the 3 check
    sites, then ``token_readers.read_family`` (I1a's logit lens) -> the committed-file layout,
    listed in a tmp ``MANIFEST.tokens.sha256``. Computed once, then copied per test."""
    from emx.i1a import capture as I1cap
    from emx.i1a import contracts as I
    from emx.i1a import token_readers as tr

    files = [I.readouts_path("logit_lens", "multihop"), I.acts_path("multihop")]
    files.append(I.acts_index_path("multihop"))
    if _I1A_PIPELINE:
        for f in files:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(_I1A_PIPELINE[f.name])
    else:
        chosen, _ = A.i1a_check_sites(tiny_backend.tokenizer)
        sites = I1cap.sites("multihop", tiny_backend.tokenizer)
        items = list(dict.fromkeys(r.id for r, _ in sites))
        limit = items.index(chosen[-1][0].id) + 1
        I1cap.capture_family(tiny_backend, "multihop", limit=limit)
        reader = tr.bind_reader("logit_lens", tiny_backend)
        assert tr.read_family(reader, tiny_backend, "multihop", limit=limit) == files[0]
        _I1A_PIPELINE.update({f.name: f.read_bytes() for f in files})
    _list_in_tokens_manifest(files[0])
    return files[0]


def _rewrite(path, fn, *, relist=True):
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    fn(rows)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    if relist:
        _list_in_tokens_manifest(path)


def test_i1a_reproduction_passes_against_i1a_pipeline(tiny_backend, i1a_stored):
    res = A.i1a_reproduction_check(tiny_backend, write=True)
    assert res.name == "smoke_i1a_reproduction" and res.passed is True, res.details
    assert res.n == 3 * 11 and res.details["layers"] == list(range(20, 61, 4))
    assert res.threshold == A.I1A_REPRO_SCORE_TOL == 0.05
    assert res.value == res.details["max_abs_delta"] and res.value <= 1e-3
    assert len(res.details["ids"]) == 3 and res.details["reference_sha256"] == _sha(i1a_stored)
    assert C.check_path("smoke_i1a_reproduction").exists()
    assert res.details["tolerance_passed"] is True and res.details["discrimination_passed"] is True
    disc = res.details["discrimination"]
    assert disc["failed"] == [] and len(disc["margins"]) == 3 * 11 and disc["min_margin"] > 0


def _shifted_capture(monkeypatch, shift):
    """Our capture reads block ``L - shift`` while labelling it L (a layer-convention bug)."""
    real = A.capture
    monkeypatch.setattr(
        A,
        "capture",
        lambda backend, sites, layers: real(backend, sites, [L - shift for L in layers]),
    )


def test_i1a_reproduction_fails_a_capture_shifted_by_one_ladder_step(
    tiny_backend, i1a_stored, monkeypatch
):
    """Capturing at L-4 (the previous ladder layer) matches I1a's L-4 row better than its L row:
    the discrimination rule fails it, cell by cell, wherever L-4 is on the ladder."""
    _shifted_capture(monkeypatch, 4)
    res = A.i1a_reproduction_check(tiny_backend, write=False)
    assert res.passed is False and res.details["discrimination_passed"] is False
    disc = res.details["discrimination"]
    ids = res.details["ids"]
    assert {f"{i}@L{L}" for i in ids for L in range(24, 61, 4)} <= set(disc["failed"])
    assert disc["min_margin"] < 0
    assert "layer discrimination FAILED" in res.notes


def test_i1a_reproduction_off_by_one_block_on_the_tiny_model(tiny_backend, i1a_stored, monkeypatch):
    """Capturing at L-1 fails, but only narrowly on the tiny random model: measured 2026-10-04,
    the tolerance rule passes (max delta 0.0443 < 0.05) and the discrimination rule fails on 3
    of 33 cells (margins -0.028, -0.019, -0.007; the next smallest is +0.002). Adjacent blocks of
    a tiny random model differ little; how well the real Qwen separates L-1 is not shown here."""
    _shifted_capture(monkeypatch, 1)
    res = A.i1a_reproduction_check(tiny_backend, write=False)
    assert res.passed is False and res.details["discrimination_passed"] is False
    assert res.details["discrimination"]["failed"]


@pytest.mark.parametrize(
    "change,passed,field",
    [
        ("score+0.08", False, "scores_over_tolerance"),
        ("score+0.03", True, None),
        ("pos", False, "site_differs"),
        ("drop", False, "missing"),
    ],
)
def test_i1a_reproduction_fails_on_a_changed_stored_row(
    tiny_backend, i1a_stored, change, passed, field
):
    chosen, _ = A.i1a_check_sites(tiny_backend.tokenizer)
    target = (chosen[1][0].id, 44)

    def edit(rows):
        i = next(k for k, r in enumerate(rows) if (r["id"], r["layer"]) == target)
        if change == "token":
            rows[i]["tokens"][3] = rows[i]["tokens"][3] + "x"
        elif change.startswith("score"):
            rows[i]["scores"][5] = round(rows[i]["scores"][5] + float(change[6:]), 4)
        elif change == "pos":
            rows[i]["pos"] -= 1
        else:
            del rows[i]

    _rewrite(i1a_stored, edit)
    res = A.i1a_reproduction_check(tiny_backend, write=False)
    assert res.passed is passed, res.details
    cell = f"{target[0]}@L{target[1]}"
    if field:
        assert res.details[field] == [cell]
    if change.startswith("score"):
        assert res.value == pytest.approx(float(change[6:]), abs=2e-4)


@pytest.mark.parametrize("breakage", ["file_changed", "manifest_missing", "not_listed"])
def test_i1a_reproduction_refuses_an_unverified_reference(
    tiny_backend, i1a_stored, monkeypatch, breakage
):
    from emx.i1a import contracts as I

    if breakage == "file_changed":  # edited after the manifest was written
        _rewrite(i1a_stored, lambda rows: rows.pop(), relist=False)
    elif breakage == "manifest_missing":
        I.manifest_path("tokens").unlink()
    else:
        I.manifest_path("tokens").write_text(f"{_sha(i1a_stored)}  results/I1a/other.jsonl\n")
    monkeypatch.setattr(A, "capture", lambda *a, **k: pytest.fail("ran before verifying"))
    with pytest.raises(A.I1aReferenceError, match="refusing to run"):
        A.i1a_reproduction_check(tiny_backend, write=False)
    assert not C.check_path("smoke_i1a_reproduction").exists()


def test_i1a_reproduction_fails_on_an_i1a_index_hash_mismatch(tiny_backend, i1a_stored):
    from emx.i1a import contracts as I

    I.manifest_path("capture").write_text(f"{'0' * 64}  results/I1a/acts/multihop.index.jsonl\n")
    res = A.i1a_reproduction_check(tiny_backend, write=False)
    assert res.passed is False and "does not hash" in res.notes


def test_i1a_cosine_only_with_verified_stored_activations(tiny_backend, i1a_stored):
    from emx.i1a import contracts as I

    assert A.i1a_stored_acts_ok() is False  # no capture manifest lists the store
    with pytest.raises(A.I1aReferenceError):
        A.i1a_cosine_check(tiny_backend, write=False)
    store = I.acts_path("multihop")
    I.manifest_path("capture").write_text(f"{_sha(store)}  results/I1a/acts/multihop.safetensors\n")
    assert A.i1a_stored_acts_ok() is True
    res = A.i1a_cosine_check(tiny_backend, write=False)
    assert res.passed is True and res.details["reference"] == "stored"
    assert res.n == 3 * len(C.LADDER)


def test_same_topk_rule():
    """Top-k agreement up to bf16 noise: per-token scores, swaps among near-ties, boundary only."""
    toks = [f"t{i}" for i in range(10)]
    sc = [10.0 - i for i in range(10)]  # well separated: 10, 9, ..., 1
    assert A._same_topk(toks, sc, toks, sc, 0.05) == (True, 0.0)
    # a mid-list token replaced by another: fails (its score is far above the 10th)
    other = toks.copy()
    other[3] = "zz"
    assert A._same_topk(toks, sc, other, sc, 0.05)[0] is False
    # two near-tied tokens swap order: passes, scores compared per token
    sw_t, sw_s = toks.copy(), sc.copy()
    sw_s[4], sw_s[5] = 5.53, 5.51
    sw_t2, sw_s2 = toks.copy(), sw_s.copy()
    sw_t2[4], sw_t2[5] = sw_t[5], sw_t[4]
    sw_s2[4], sw_s2[5] = 5.52, 5.50
    ok, d = A._same_topk(sw_t, sw_s, sw_t2, sw_s2, 0.05)
    assert ok and abs(d - 0.03) < 1e-9  # t4: 5.53 vs 5.50
    # a token entering at the boundary within tolerance of the 10th: passes
    edge_t, edge_s = toks.copy(), sc.copy()
    edge_t[9], edge_s[9] = "new", 0.98
    assert A._same_topk(toks, sc, edge_t, edge_s, 0.05)[0] is True
    # ... but not if its score is clearly above the other list's 10th
    edge_s2 = edge_s.copy()
    edge_s2[9] = 1.2
    assert A._same_topk(toks, sc, edge_t, edge_s2, 0.05)[0] is False
