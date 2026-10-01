"""U4: reconstructor loader, AR forward and joint bullet FVE (emx.i1a.fve, emx.i1a.checks_fve).

Parity tests import the vendored reference (third_party/olens_draft, MIT) with a stub `jaxtyping`
(annotations only), so the port is checked against the upstream functions themselves.
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

torch = pytest.importorskip("torch")

from emx.i1a import checks_fve, fve
from emx.i1a import contracts as C

OLENS_ROOT = Path(__file__).resolve().parents[2] / "third_party" / "olens_draft"


# ------------------------------------------------------------------ upstream reference import


@pytest.fixture(scope="module")
def up():
    """The vendored olens_draft modules, importable for this module only."""
    saved = {k: v for k, v in sys.modules.items() if k == "jaxtyping" or k.startswith("oracle_")}
    try:
        importlib.import_module("jaxtyping")
    except ModuleNotFoundError:
        stub = types.ModuleType("jaxtyping")

        class _Ann:
            def __class_getitem__(cls, item):
                return Any

        for n in ("Float", "Int", "Bool"):
            setattr(stub, n, _Ann)
        sys.modules["jaxtyping"] = stub
    sys.path.insert(0, str(OLENS_ROOT))
    try:
        yield types.SimpleNamespace(
            whitening=importlib.import_module("oracle_lens.core.whitening"),
            nnomp=importlib.import_module("oracle_lens.core.nnomp"),
            recon=importlib.import_module("oracle_lens.core.reconstructor"),
            rl=importlib.import_module("oracle_lens.pipeline.rl_reward"),
            scorer=importlib.import_module("oracle_lens.pipeline.scorer"),
            ar_loader=importlib.import_module("oracle_lens.pipeline.ar_loader"),
        )
    finally:
        sys.path.remove(str(OLENS_ROOT))
        for k in [k for k in sys.modules if k == "jaxtyping" or k.startswith("oracle_")]:
            del sys.modules[k]
        sys.modules.update(saved)


# ------------------------------------------------------------------ whitening


def _spd(d: int, seed: int, rank: int | None = None):
    g = torch.Generator().manual_seed(seed)
    a = torch.randn(d, rank or d, generator=g)
    return a @ a.T / d


def test_whitening_exact_on_synthetic_cov(up):
    cov = _spd(16, 0) + 0.05 * torch.eye(16)
    w = fve.whitening_matrix(cov, C.RIDGE_C)
    lam = C.RIDGE_C * cov.double().diagonal().mean()
    cov_l = cov.double() + lam * torch.eye(16, dtype=torch.float64)
    ident = w.double() @ cov_l @ w.double()
    assert torch.allclose(ident, torch.eye(16, dtype=torch.float64), atol=1e-5)
    assert torch.allclose(w, w.T)
    assert w.dtype == torch.float32
    assert torch.equal(w, up.whitening.whitening_matrix(cov, ridge_c=C.RIDGE_C))


def test_whitening_rank_deficient_is_finite():
    cov = _spd(16, 1, rank=4)
    w = fve.whitening_matrix(cov, C.RIDGE_C)
    assert torch.isfinite(w).all()
    zero = fve.whitening_matrix(torch.zeros(4, 4), C.RIDGE_C)  # lam clamps at 1e-12
    assert torch.isfinite(zero).all()


def test_load_whiteners_matches_upstream(fake_whitener_dir, up):
    ws = fve.load_whiteners(fake_whitener_dir)
    assert sorted(ws) == sorted(C.AR_LAYERS)
    ref = up.ar_loader.load_ladder_whiteners(
        fake_whitener_dir, prefix=C.WHITENER_PREFIX, layers=C.AR_LAYERS, ridge_c=C.RIDGE_C
    )
    x = torch.randn(5, 64)
    for L in C.AR_LAYERS:
        assert torch.equal(ws[L].w, ref[L].w)
        assert torch.allclose(ws[L].whiten(x), ref[L].whiten(x))
    with pytest.raises(FileNotFoundError):
        fve.load_whiteners(fake_whitener_dir, layers=(5,))


# ------------------------------------------------------------------ NNLS


def test_nnls_recovers_known_nonnegative_weights():
    q, _ = torch.linalg.qr(torch.randn(32, 4, generator=torch.Generator().manual_seed(2)))
    v = q.T.unsqueeze(0)  # [1, 4, 32], orthonormal rows
    true = torch.tensor([[0.7, 1.3, 0.0, 0.4]])
    x = torch.bmm(true.unsqueeze(1), v).squeeze(1) - 0.5 * v[:, 2]  # negative part on atom 2
    w, f = fve.nnls_refit(v, x, torch.ones(1, 4, dtype=torch.bool))
    assert torch.allclose(w, true, atol=1e-6)
    expect = 1 - 0.25 / float((x**2).sum())
    assert abs(float(f[0]) - expect) < 1e-5
    # an invalid slot is never used
    valid = torch.tensor([[True, True, False, True]])
    w2, _ = fve.nnls_refit(v, x, valid)
    assert float(w2[0, 2]) == 0.0


def test_nnls_matches_upstream(up):
    g = torch.Generator().manual_seed(3)
    v = torch.randn(6, 4, 20, generator=g)
    v = v / v.norm(dim=-1, keepdim=True)
    x = torch.randn(6, 20, generator=g)
    valid = torch.rand(6, 4, generator=g) > 0.3
    a = fve.nnls_refit(v, x, valid)
    b = up.nnomp.nnls_refit(v, x, valid)
    assert torch.equal(a[0], b[0]) and torch.equal(a[1], b[1])


# ------------------------------------------------------------------ text -> phrases

BULLET_CASES = [
    ("", []),
    ("   ", []),
    ("no bullets here\njust text", ["no bullets here\njust text"]),
    ("- a\n- b\n- c\n- d\n- e\n- f", ["a", "b", "c", "d"]),
    ("- a\n- \n-\n-   \n- b", ["a", "b"]),
    ("<|im_start|>assistant\n- cat<|im_end|>\n- dog", ["cat", "dog"]),
    ("<explanation>- x\n- y</explanation>", ["x", "y"]),
    ("<think></think>\n  - indented\nprose\n-nospace", ["indented"]),
    ("<|im_end|>", []),
    ("-dash only", ["-dash only"]),
    ("* star\n+ plus", ["* star\n+ plus"]),
]


@pytest.mark.parametrize(("text", "want"), BULLET_CASES)
def test_split_bullets_edge_cases(text, want, up):
    assert fve.split_bullets(text, 4) == want
    assert fve.split_bullets(text, 4) == up.rl.split_bullets(text, 4)


def test_bare_token_ids_matches_upstream(qwen_tok, up):
    for t in ["- The Eiffel Tower", "<|im_end|> Paris<|endoftext|>", "", "x" * 3000]:
        assert fve.bare_token_ids(qwen_tok, t) == up.scorer.bare_token_ids(qwen_tok, t)
        assert fve.reward_text_ids(qwen_tok, t) == up.rl.reward_text_ids(qwen_tok, t)
    assert len(fve.reward_text_ids(qwen_tok, "word " * 200)) == C.AR_SPAN_WIDTH


# ------------------------------------------------------------------ adapter loading


def test_remap_ar_key():
    k = "base_model.model.layers.12._orig_mod.linear_attn.in_proj_a.lora_A.weight"
    assert (
        fve.remap_ar_key(k)
        == "base_model.model.model.layers.12.linear_attn.in_proj_a.lora_A.weight"
    )
    plain = "base_model.model.model.layers.3.mlp.up_proj.lora_B.weight"
    assert fve.remap_ar_key(plain) == plain
    with pytest.raises(ValueError):
        fve.remap_ar_key("base_model.model.lm_head.lora_A.weight")


def _mount_olens(backend, olens_dir: Path):
    from peft import PeftModel

    backend.model = PeftModel.from_pretrained(backend.model, str(olens_dir), adapter_name="olens")
    backend.model.eval()


def _last_logits(backend, ids):
    with torch.no_grad():
        return backend.model(input_ids=torch.tensor([ids]), logits_to_keep=1).logits[0, -1]


@pytest.fixture
def ar_handle(tiny_backend, dummy_olens_dir, dummy_ar_dir):
    _mount_olens(tiny_backend, dummy_olens_dir)
    return fve.load_ar(tiny_backend, dummy_ar_dir)


def test_load_ar_live_next_to_olens(tiny_backend, dummy_olens_dir, dummy_ar_dir):
    _mount_olens(tiny_backend, dummy_olens_dir)
    probe = [785, 6722, 315, 9625, 374]
    before = _last_logits(tiny_backend, probe)
    h = fve.load_ar(tiny_backend, dummy_ar_dir)
    m = tiny_backend.model
    assert set(m.peft_config) == {"olens", "ar"}
    assert m.active_adapter == "olens"  # loading "ar" did not switch the reader
    assert h.n_live == h.n_saved > 0
    assert h.layers == C.AR_LAYERS and h.layer_norm is True
    assert torch.allclose(_last_logits(tiny_backend, probe), before)
    with fve.using_adapter(m, "ar"):
        assert m.active_adapter == "ar"
        ar_logits = _last_logits(tiny_backend, probe)
    assert m.active_adapter == "olens"
    assert not torch.allclose(ar_logits, before)
    fve.ar_predict(h, [[1, 2, 3]], [44])
    assert m.active_adapter == "olens"  # restored after the AR forward
    assert torch.allclose(_last_logits(tiny_backend, probe), before)


def test_load_ar_on_plain_model(tiny_backend, dummy_ar_dir):
    from peft import PeftModel

    h = fve.load_ar(tiny_backend, dummy_ar_dir)
    assert isinstance(tiny_backend.model, PeftModel)
    assert list(tiny_backend.model.peft_config) == ["ar"]
    assert h.n_live == h.n_saved


def test_unremapped_load_is_flagged_inert(tiny_backend, dummy_olens_dir, dummy_ar_dir):
    _mount_olens(tiny_backend, dummy_olens_dir)
    with pytest.raises(RuntimeError, match="inert"):
        fve.load_ar(tiny_backend, dummy_ar_dir, remap=False)
    assert set(tiny_backend.model.peft_config) == {"olens"}  # the inert adapter was removed
    assert tiny_backend.model.active_adapter == "olens"
    assert fve.load_ar(tiny_backend, dummy_ar_dir).n_live > 0  # a correct retry still works


def test_unexpected_non_lora_key_is_rejected(tiny_backend, dummy_olens_dir, dummy_ar_dir, tmp_path):
    import shutil

    from safetensors.torch import load_file, save_file

    ar = tmp_path / "ar_ptag_pooled"
    shutil.copytree(dummy_ar_dir, ar)
    st = ar / "lora" / "adapter_model.safetensors"
    t = load_file(str(st))
    # a shape-matching base weight: PEFT would silently copy it into the shared base
    t["base_model.model.layers.0._orig_mod.mlp.up_proj.base_layer.weight"] = torch.zeros(128, 64)
    save_file(t, str(st))
    _mount_olens(tiny_backend, dummy_olens_dir)
    blocks = tiny_backend.blocks
    before = blocks[0].mlp.up_proj.base_layer.weight.detach().clone()
    with pytest.raises(RuntimeError, match="unexpected non-LoRA"):
        fve.load_ar(tiny_backend, ar)
    assert set(tiny_backend.model.peft_config) == {"olens"}
    assert torch.equal(blocks[0].mlp.up_proj.base_layer.weight, before)


# ------------------------------------------------------------------ AR forward


def _reference_ar(up, tiny_model, ar_dir: Path):
    """olens_draft's own recipe on a separate copy: truncate at 63 (final norm -> identity),
    compile-wrap iff `_orig_mod` keys, PeftModel.from_pretrained on the inner model with the
    UNREMAPPED adapter, unwrap to eager, prompt-tag head from heads.pt."""
    from peft import PeftModel

    base = copy.deepcopy(tiny_model)
    inner = up.recon.truncate_backbone(base, layer=63)
    assert up.ar_loader.compile_blocks_if_ckpt_needs(inner, ar_dir / "lora")
    peft_inner = PeftModel.from_pretrained(inner, str(ar_dir / "lora"))
    up.ar_loader.verify_adapter_live(peft_inner, ar_dir / "lora")  # names carry _orig_mod
    up.ar_loader._unwrap_compiled_blocks(inner)
    peft_inner.eval()
    state = torch.load(ar_dir / "heads.pt", weights_only=True)
    head = up.recon.ReconstructorHead(64, layer_norm=True)
    head.load_state_dict(state["head"])
    return peft_inner, head, state["tag_ids"]


def _reference_predict(ref, span: list[int], row: int):
    """PromptTagReconstructor._tagged + _read_final on one right-padded row."""
    peft_inner, head, tag_ids = ref
    ids = torch.tensor([tag_ids[row].tolist() + span + [0, 0]])
    mask = torch.tensor([[1] * (tag_ids.shape[1] + len(span)) + [0, 0]])
    with torch.no_grad():
        out = peft_inner(input_ids=ids, attention_mask=mask, use_cache=False)
        last = mask.sum(dim=1) - 1
        return head(out.last_hidden_state[torch.arange(1), last].float())[0]


def test_ar_predict_matches_truncated_backbone_reference(
    up, tiny_model, tiny_backend, dummy_olens_dir, dummy_ar_dir
):
    ref = _reference_ar(up, tiny_model, dummy_ar_dir)
    _mount_olens(tiny_backend, dummy_olens_dir)
    h = fve.load_ar(tiny_backend, dummy_ar_dir)
    spans = [[785, 6722], [9625, 374, 12095, 13, 576, 3283], [42]]
    layers = [20, 44, 63]
    got = fve.ar_predict(h, spans, layers)
    for i, (s, L) in enumerate(zip(spans, layers, strict=True)):
        want = _reference_predict(ref, s, C.AR_LAYERS.index(L))
        assert torch.allclose(got[i], want, atol=1e-4, rtol=1e-4), (i, (got[i] - want).abs().max())


def test_ar_predict_padding_and_batching_invariant(ar_handle):
    spans = [[5], list(range(100, 164)), [7, 8, 9], list(range(200, 300))]  # last one truncates
    layers = [20, 36, 63, 60]
    batched = fve.ar_predict(ar_handle, spans, layers, micro_batch=32)
    single = torch.stack(
        [fve.ar_predict(ar_handle, [s], [L])[0] for s, L in zip(spans, layers, strict=True)]
    )
    assert torch.allclose(batched, single, atol=1e-4, rtol=1e-4)
    trunc = fve.ar_predict(ar_handle, [list(range(200, 264))], [60])
    assert torch.allclose(batched[3], trunc[0], atol=1e-4, rtol=1e-4)
    with pytest.raises(ValueError):
        fve.ar_predict(ar_handle, [[]], [20])
    with pytest.raises(KeyError):
        fve.ar_predict(ar_handle, [[1]], [21])


# ------------------------------------------------------------------ joint FVE

TEXTS = [
    "- Paris\n- the Eiffel Tower\n- France",
    "A single phrase without bullets",
    "",
    "<|im_end|>",
    "- one\n- two\n- three\n- four\n- five",
]


def test_joint_bullet_fve_matches_upstream_loop(up, ar_handle, fake_whitener_dir):
    ws = fve.load_whiteners(fake_whitener_dir)
    layers = [20, 44, 52, 60, 63]
    golds = torch.randn(len(TEXTS), 64, generator=torch.Generator().manual_seed(7))
    res = fve.joint_bullet_fve(ar_handle, ws, TEXTS, golds, layers)
    assert [r[1] for r in res] == [3, 1, 0, 0, 4]
    assert [r[2] for r in res] == [True, True, False, False, True]
    assert res[2][0] == 0.0 and res[3][0] == 0.0
    # upstream score_texts_joint row loop, fed the same per-phrase AR images
    uws = up.ar_loader.load_ladder_whiteners(
        fake_whitener_dir, prefix=C.WHITENER_PREFIX, layers=C.AR_LAYERS, ridge_c=C.RIDGE_C
    )
    for i, t in enumerate(TEXTS):
        phrases = up.rl.split_bullets(t, 4)
        ids = [up.rl.reward_text_ids(ar_handle.tokenizer, p) for p in phrases]
        ids = [x for x in ids if x]
        vecs = torch.zeros(1, 4, 64)
        valid = torch.zeros(1, 4, dtype=torch.bool)
        w = uws[layers[i]]
        for kk, x in enumerate(ids):
            pv = w.whiten(fve.ar_predict(ar_handle, [x], [layers[i]]))[0]
            vecs[0, kk] = pv / (pv.norm() + 1e-9)
            valid[0, kk] = True
        _, f = up.nnomp.nnls_refit(vecs, w.whiten(golds[i : i + 1]), valid)
        want = float(f[0]) if valid.any() else 0.0
        assert abs(res[i][0] - want) < 1e-4, (i, res[i][0], want)
        assert res[i][0] >= -1e-6


def test_span_fve_is_whitened_cos2(up, ar_handle, fake_whitener_dir):
    ws = fve.load_whiteners(fake_whitener_dir)
    spans, layers = [[1, 2, 3], [4, 5], [6]], [20, 20, 44]
    golds = torch.randn(3, 64, generator=torch.Generator().manual_seed(8))
    got = fve.span_fve(ar_handle, ws, spans, golds, layers)
    preds = fve.ar_predict(ar_handle, spans, layers)
    ref = up.rl.score_rows(preds, golds, torch.tensor(layers), ws)
    assert torch.allclose(got, ref.fve, atol=1e-6)


# ------------------------------------------------------------------ family scoring + checks


def _fake_family(family: str, n_sites: int, layers, seed: int = 9):
    sites = [C.SiteRow(family, i, f"item{i // 2}", 10 + i, 10 + i, "x", 40) for i in range(n_sites)]
    H = torch.randn(n_sites, len(layers), 64, generator=torch.Generator().manual_seed(seed))
    return sites, H


def _write_samples(family: str, sites, layers, texts, n_samples: int = 2):
    rows = []
    for L in layers:
        for s in sites:
            for k in range(n_samples):
                rows.append(
                    C.SampleRow(
                        family=family,
                        id=s.id,
                        layer=L,
                        pos=s.pos,
                        site_index=s.index,
                        sample_idx=k,
                        draw="gate" if k == 0 else "extra",
                        seed=0,
                        batch_index=0,
                        text=texts[(s.index + k) % len(texts)],
                        n_new_tokens=5,
                        finish="eos",
                    )
                )
    p = C.samples_path(family)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(C.to_json(r) + "\n" for r in rows))
    return rows


def test_score_family_end_to_end_and_resume(i1a_tmp_layout, ar_handle, fake_whitener_dir):
    fam, layers = "hallucination", [20, 44]
    ws = fve.load_whiteners(fake_whitener_dir)
    sites, H = _fake_family(fam, 4, layers)
    samples = _write_samples(fam, sites, layers, TEXTS)
    path = fve.score_family(ar_handle, ws, fam, acts=(sites, H), layers=layers, chunk=5)
    assert path == C.fve_path(fam)
    rows = fve.read_fve(fam)
    assert len(rows) == len(samples) == 16
    assert all(isinstance(r, C.FveRow) for r in rows)
    by_text = {s.text: r for s, r in zip(samples, rows, strict=True)}
    assert by_text[""].valid is False and by_text[""].joint_fve == 0.0
    assert by_text[TEXTS[0]].n_bullets == 3 and by_text[TEXTS[0]].valid
    # resumable: truncate and rerun -> same rows, no duplicates
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:7]))
    fve.score_family(ar_handle, ws, fam, acts=(sites, H), layers=layers, chunk=5)
    again = fve.read_fve(fam)
    assert len(again) == 16
    assert {fve._fve_key(r) for r in again} == {fve._fve_key(r) for r in rows}
    k = {fve._fve_key(r): r.joint_fve for r in rows}
    assert all(abs(k[fve._fve_key(r)] - r.joint_fve) < 1e-5 for r in again)
    # id filter
    path.unlink()
    fve.score_family(ar_handle, ws, fam, acts=(sites, H), layers=layers, ids={"item0"})
    assert {r.id for r in fve.read_fve(fam)} == {"item0"}


def test_ruler_spans_anchor_and_seed():
    ids = list(range(100))
    sites = [
        (C.SiteRow("hallucination", 0, "a", -1, 50, "x", 100), ids),
        (C.SiteRow("hallucination", 1, "a", -1, 99, "x", 100), ids),  # last token: no "after"
        (C.SiteRow("hallucination", 2, "b", -1, 0, "x", 100), ids),
    ]
    after = checks_fve.ruler_spans(sites, "after")
    ending = checks_fve.ruler_spans(sites, "ending")
    assert after == checks_fve.ruler_spans(sites, "after")
    assert after[0][0] == 51 and 1 <= len(after[0]) <= 32
    assert after[1] is None
    assert ending[0][-1] == 50 and len(ending[0]) == len(after[0])
    assert ending[1][-1] == 99 and ending[2] == [0]


def _with_val_fve(h, vals: dict[int, float]):
    h.meta = {**h.meta, "metrics": {f"val_fve_L{L}": v for L, v in vals.items()}}
    return h


def test_checks_run_and_write(i1a_tmp_layout, ar_handle, fake_whitener_dir):
    fam, layers = "hallucination", list(C.AR_LAYERS)
    ws = fve.load_whiteners(fake_whitener_dir)
    sites, H = _fake_family(fam, 4, layers)
    ids = list(range(1000, 1040))
    _with_val_fve(ar_handle, {L: v + 1e-5 for L, v in C.AR_VAL_FVE.items()})
    r = checks_fve.check_ruler(ar_handle, ws, sites=[(s, ids) for s in sites], acts=(sites, H),
                               layers=layers)  # fmt: skip
    assert r.n == 4 * len(C.RULER_LAYERS) and isinstance(r.passed, bool)
    assert set(r.details["per_layer"]) == {str(L) for L in C.RULER_LAYERS}
    target = sum(C.AR_VAL_FVE[L] for L in C.RULER_LAYERS) / len(C.RULER_LAYERS)
    assert abs(r.details["target_val_fve_mean"] - target) < 1e-12
    assert abs(r.value - abs(r.details["mean_fve"] - target)) < 1e-12
    assert json.loads(C.check_path("fve_ruler").read_text())["n"] == r.n

    _write_samples(fam, sites, C.FAMILY_LAYERS[fam], TEXTS, n_samples=1)
    m = checks_fve.check_mismatch(ar_handle, ws, fam, acts=(sites, H), layers=layers)
    assert m.n > 0 and 0.0 <= m.value <= 1.0 and isinstance(m.passed, bool)
    assert set(m.details["per_layer"]) == {str(L) for L in C.FAMILY_LAYERS[fam]}
    assert m.details["layers_missing"] == []
    assert C.check_path("fve_mismatch").exists()


def test_ruler_fails_unscored_on_meta_mismatch(i1a_tmp_layout, ar_handle, fake_whitener_dir):
    ws = fve.load_whiteners(fake_whitener_dir)
    layers = list(C.AR_LAYERS)
    sites, H = _fake_family("hallucination", 2, layers)
    kw = {"sites": [(s, list(range(40))) for s in sites], "acts": (sites, H), "layers": layers}
    # the fixture meta carries the real top-level val_fve_L* keys; strip them to test "missing"
    ar_handle.meta = {k: v for k, v in ar_handle.meta.items() if not k.startswith("val_fve_L")}
    r = checks_fve.check_ruler(ar_handle, ws, **kw)
    assert (
        r.passed is False
        and r.n == 0
        and set(r.details["meta_mismatch"]) == {str(L) for L in C.AR_VAL_FVE}
    )
    _with_val_fve(ar_handle, {**C.AR_VAL_FVE, 44: 0.25})
    r = checks_fve.check_ruler(ar_handle, ws, **kw)
    assert r.passed is False and r.details["meta_mismatch"] == {"44": 0.25}
    assert checks_fve.meta_val_fve({"a": {"val_fve_L20": 0.1}, "val_fve_mean": 0.2}) == {20: 0.1}


def test_mismatch_fails_on_missing_or_unevaluable_layer(
    i1a_tmp_layout, ar_handle, fake_whitener_dir
):
    fam, layers = "hallucination", list(C.AR_LAYERS)
    ws = fve.load_whiteners(fake_whitener_dir)
    sites, H = _fake_family(fam, 4, layers)
    # a readout that always wins, so only coverage can fail the check
    rows = _write_samples(fam, sites, [20, 36, 44, 52], ["- Paris"], n_samples=1)
    m = checks_fve.check_mismatch(ar_handle, ws, fam, acts=(sites, H), layers=layers)
    assert m.passed is False and m.details["layers_missing"] == [60]
    # layer 60 present but every readout empty -> still not evaluable
    extra = [
        C.SampleRow(fam, s.id, 60, s.pos, s.index, 0, "gate", 0, 0, "", 0, "eos") for s in sites
    ]
    C.samples_path(fam).write_text("".join(C.to_json(r) + "\n" for r in rows + extra))
    m = checks_fve.check_mismatch(ar_handle, ws, fam, acts=(sites, H), layers=layers)
    assert m.passed is False and m.details["layers_missing"] == [60]
    assert m.details["per_layer"]["60"] is None
    # no gate rows at all -> fails (not "could not run")
    C.samples_path(fam).write_text("")
    m = checks_fve.check_mismatch(ar_handle, ws, fam, acts=(sites, H), layers=layers)
    assert m.passed is False and m.n == 0


def test_check_ar_live(i1a_tmp_layout, ar_handle, qwen_tok):
    live = checks_fve.check_ar_live(ar_handle, qwen_tok)
    assert live.passed is True and live.value == 1.0, live.details
    assert C.check_path("fve_ar_live").exists()
    probe = live.details["switch_probe"]
    assert probe["restored"] and probe["previous_adapter"] == "olens"
    assert probe["ar_vs_base_rel_l2"] >= checks_fve.SWITCH_MIN_REL
    assert ar_handle.backend.model.active_adapter == "olens"
    ar_handle.tag_ids[0, 3] += 1  # a wrong tag row fails the check
    bad = checks_fve.check_ar_live(ar_handle, qwen_tok, write=False)
    assert bad.passed is False and bad.details["tag_mismatch_layers"] == [20]


def test_check_ar_live_fails_when_switching_is_inert(i1a_tmp_layout, ar_handle, qwen_tok):
    with torch.no_grad():  # the adapter "loads" but does nothing when active
        for n, p in ar_handle.backend.model.named_parameters():
            if ".lora_B.ar." in n:
                p.zero_()
    live = checks_fve.check_ar_live(ar_handle, qwen_tok, write=False)
    assert live.passed is False
    assert live.details["switch_probe"]["ar_vs_base_rel_l2"] < checks_fve.SWITCH_MIN_REL
    assert live.details["tag_mismatch_layers"] == [] and live.value == 1.0


def _write_fve(family: str, vals: list[tuple[float, bool]], extra: float | None = None):
    rows = [
        C.FveRow(family, f"i{j}", 20, -1, 0, v, 1 if ok else 0, ok)
        for j, (v, ok) in enumerate(vals)
    ]
    if extra is not None:
        rows.append(C.FveRow(family, "i0", 20, -1, 3, extra, 1, True))  # not a gate draw
    p = C.fve_path(family)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(C.to_json(r) + "\n" for r in rows))


def test_check_joint_band(i1a_tmp_layout):
    res = checks_fve.check_joint_band()
    assert res.passed is None and res.n == 0
    _write_fve("hallucination", [(0.5, True), (0.6, True), (0.0, False)], extra=0.0)
    res = checks_fve.check_joint_band()
    assert res.n == 3 and abs(res.value - 1.1 / 3) < 1e-12
    assert res.passed is False  # 0.367 is outside 0.487 +- 0.10 (the empty row counts as 0)
    assert abs(res.details["mean_valid_only"] - 0.55) < 1e-12
    _write_fve("hallucination", [(0.5, True), (0.45, True)])
    assert checks_fve.check_joint_band().passed is True
    assert json.loads(C.check_path("fve_joint_band").read_text())["passed"] is True
