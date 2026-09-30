"""U2 token readers and §4.7 reader checks on the tiny model."""

from __future__ import annotations

import copy
import json

import pytest

from emx.i1a import capture, checks_readers, token_readers
from emx.i1a import contracts as C

torch = pytest.importorskip("torch")


@pytest.fixture
def bf16_backend(tiny_model, qwen_tok):
    """The tiny model in bf16, so the bf16 store is lossless (as for the real model)."""
    from wsbench.produce.backend import Backend

    return Backend(
        model=tiny_model.to(torch.bfloat16), tokenizer=qwen_tok, device="cpu", model_id="tiny"
    )


def _producer_rows(backend, method, family, limit):
    """What wsbench's Producer.run_family writes for the first `limit` items."""
    from wsbench.produce.producer import Producer

    prod = Producer(backend=backend, method=method)
    lines = []
    with torch.inference_mode():
        for spec in capture.family_specs(family)[:limit]:
            for r in prod.read_spec(spec, layers=list(capture.family_layers(family))):
                lines.append(json.dumps(r.contract(), ensure_ascii=False))
    return lines


# --------------------------------------------------------------------- readers


def test_bind_reader_jlens_needs_local_path(tiny_backend, fake_jlens_path):
    with pytest.raises(ValueError):
        token_readers.bind_reader("jlens", tiny_backend)
    m = token_readers.bind_reader("jlens", tiny_backend, jlens_path=fake_jlens_path)
    assert m._jac.shape == (C.N_MODEL_LAYERS, 64, 64) and m._jac.dtype == torch.float32
    with pytest.raises(KeyError):
        token_readers.bind_reader("olens", tiny_backend)


@pytest.mark.parametrize("name", token_readers.READERS)
@pytest.mark.parametrize("family", ["multihop", "hallucination"])
def test_read_family_matches_producer(name, family, bf16_backend, fake_jlens_path, i1a_tmp_layout):
    from wsbench.readouts import load_readouts

    capture.capture_family(bf16_backend, family, limit=2)
    reader = token_readers.bind_reader(name, bf16_backend, jlens_path=fake_jlens_path)
    out = token_readers.read_family(reader, bf16_backend, family)
    assert out == C.readouts_path(name, family)
    ours = out.read_text(encoding="utf-8").splitlines()
    # byte-for-byte what the benchmark's own producer writes on the same model and items, with
    # a reader bound independently by wsbench's own bind (not the instance that wrote the rows)
    fresh = token_readers.bind_reader_via_wsbench(name, bf16_backend, jlens_path=fake_jlens_path)
    assert fresh is not reader
    assert ours == _producer_rows(bf16_backend, fresh, family, 2)
    cells, rep = load_readouts(out)
    assert rep.skipped["malformed"] == 0 and sum(rep.skipped.values()) == 0
    assert rep.kind == "tokens" and len(cells) == len(ours)
    n_sites = len(capture.load_index(family))
    assert len(cells) == n_sites * len(C.FAMILY_LAYERS[family])
    assert all(len(c.tokens) == 10 and c.token is not None for c in cells)
    # the top-100 file: same cells, 100 ids whose first 10 display as the contract tokens
    from wsbench.produce.render import display_tokens

    tk = [json.loads(line) for line in C.topk_path(name, family).read_text().splitlines()]
    assert len(tk) == len(cells)
    for t, c in zip(tk, cells, strict=True):
        assert (t["id"], t["layer"], t["pos"]) == (c.id, c.layer, c.pos)
        assert len(t["ids"]) == 100 and len(t["scores"]) == 100
        assert display_tokens(bf16_backend.tokenizer, t["ids"][:10]) == list(c.tokens)


def test_read_family_oldsite_arm(tiny_backend, i1a_tmp_layout):
    from wsbench.readouts import load_readouts

    capture.capture_family(tiny_backend, C.OLDSITE_FAMILY, limit=2)
    reader = token_readers.bind_reader("logit_lens", tiny_backend)
    out = token_readers.read_family(reader, tiny_backend, C.OLDSITE_FAMILY)
    assert out == C.readouts_path("logit_lens_oldsite", "multihop")
    cells, rep = load_readouts(out)
    assert len(cells) == 2 * len(C.GRID) and rep.skipped["malformed"] == 0
    assert C.topk_path("logit_lens", C.OLDSITE_FAMILY).exists()


# --------------------------------------------------------------------- checks


def test_check_backend(tiny_backend, i1a_tmp_layout):
    res = checks_readers.check_backend(tiny_backend)
    assert res.passed is True
    saved = json.loads(C.check_path("backend").read_text())
    assert saved["details"]["attn_implementation"] == "sdpa" and saved["utc"]


def test_check_capture_repeat(tiny_backend, i1a_tmp_layout):
    res = checks_readers.check_capture_repeat(tiny_backend)
    assert res.passed is True and res.value == 0.0 and res.n == 3
    assert C.check_path("capture_repeat").exists()


def test_check_logit_lens_last_layer(tiny_backend, i1a_tmp_layout):
    res = checks_readers.check_logit_lens_last_layer(tiny_backend, limit=5)
    assert res.n == 5 and res.passed is True, res.details
    assert json.loads(C.check_path("logit_lens_last_layer").read_text())["value"] == res.value


def _write_fake_reference(reader, ref_dir):
    """A reference dir in the shipped layout (<label>/L###.jsonl, one row, 10 display tokens)
    produced by `reader` itself on the captured jlens_ref store."""
    from wsbench.produce.render import display_tokens

    layers = list(C.JLENS_REF_LAYERS)
    rows, h = capture.load_acts(C.JLENS_REF_FAMILY, layers)
    ids = {s.id: s.extra["input_ids"] for s in capture.family_specs(C.JLENS_REF_FAMILY)}
    tok = reader._b.tokenizer
    for r in rows:
        (ref_dir / r.id).mkdir(parents=True, exist_ok=True)
        for li, layer in enumerate(layers):
            with torch.inference_mode():
                ro = reader.read(h[r.index, li], layer)
            row = {
                "label": r.id,
                "family": "pt",
                "layer": layer,
                "pos": r.pos,
                "token": display_tokens(tok, [ids[r.id][r.abs_pos]])[0],
                "samples": ro.tokens,
                "scores": ro.scores,
            }
            (ref_dir / r.id / f"L{layer:03d}.jsonl").write_text(json.dumps(row) + "\n")


def test_check_jlens_reference_pass_and_fail(tiny_backend, fake_jlens_path, i1a_tmp_layout):
    capture.capture_family(tiny_backend, C.JLENS_REF_FAMILY, limit=3)
    lens = token_readers.bind_reader("jlens", tiny_backend, jlens_path=fake_jlens_path)
    ref_dir = i1a_tmp_layout / "ref"
    _write_fake_reference(lens, ref_dir)

    ok = checks_readers.check_jlens_reference(lens, ref_dir=ref_dir, limit=3)
    assert ok.passed is True and ok.value == 1.0 and ok.n == 3 * 12
    assert ok.details["top10_overlap_mean"] == 1.0
    assert ok.details["alignment_failures"] == 0 and ok.details["n_cells_aligned"] == ok.n

    bad = copy.copy(lens)
    g = torch.Generator().manual_seed(99)
    bad._jac = torch.randn(lens._jac.shape, generator=g)
    bad._denom = {}
    res = checks_readers.check_jlens_reference(bad, ref_dir=ref_dir, limit=3)
    assert res.passed is False and res.value < 0.5
    saved = json.loads(C.check_path("jlens_reference").read_text())
    assert saved["passed"] is False and saved["details"]["misses"]


def test_check_jlens_reference_missing_rows_fail(tiny_backend, fake_jlens_path, i1a_tmp_layout):
    capture.capture_family(tiny_backend, C.JLENS_REF_FAMILY, limit=2)
    lens = token_readers.bind_reader("jlens", tiny_backend, jlens_path=fake_jlens_path)
    res = checks_readers.check_jlens_reference(lens, ref_dir=i1a_tmp_layout / "empty", limit=2)
    assert res.passed is False and res.details["alignment_failures"] == 2 * 12


def test_jlens_reference_positions_on_shipped_dir(qwen_tok):
    """The shipped reference rows sit exactly at our jlens_ref sites (no model needed)."""
    from wsbench.produce.render import display_tokens

    ref_dir = checks_readers.default_ref_dir()
    ids = {s.id: s.extra["input_ids"] for s in capture.family_specs(C.JLENS_REF_FAMILY)}
    for r, _ in capture.sites(C.JLENS_REF_FAMILY, qwen_tok):
        for layer in C.JLENS_REF_LAYERS:
            (row,) = [json.loads(x) for x in (ref_dir / r.id / f"L{layer:03d}.jsonl").open()]
            assert row["pos"] == r.pos
            assert row["token"] == display_tokens(qwen_tok, [ids[r.id][r.abs_pos]])[0]
            assert len(row["samples"]) == 10


def _edit_ref(path, **changes):
    row = json.loads(path.read_text())
    row.update(changes)
    path.write_text(json.dumps(row) + "\n")


@pytest.mark.parametrize(
    "corrupt",
    ["label", "layer", "token", "pos", "duplicate", "missing"],
)
def test_check_jlens_reference_alignment_failures(
    corrupt, tiny_backend, fake_jlens_path, i1a_tmp_layout
):
    capture.capture_family(tiny_backend, C.JLENS_REF_FAMILY, limit=2)
    lens = token_readers.bind_reader("jlens", tiny_backend, jlens_path=fake_jlens_path)
    ref_dir = i1a_tmp_layout / "ref"
    _write_fake_reference(lens, ref_dir)
    rows = capture.load_index(C.JLENS_REF_FAMILY)
    f = ref_dir / rows[1].id / "L044.jsonl"
    if corrupt == "label":
        _edit_ref(f, label=rows[0].id)
    elif corrupt == "layer":
        _edit_ref(f, layer=40)
    elif corrupt == "token":
        _edit_ref(f, token="not-the-site-token")
    elif corrupt == "pos":
        _edit_ref(f, pos=rows[1].pos + 1)
    elif corrupt == "duplicate":
        f.write_text(f.read_text() * 2)
    else:
        f.unlink()
    res = checks_readers.check_jlens_reference(lens, ref_dir=ref_dir, limit=2)
    assert res.passed is False
    assert res.details["alignment_failures"] == 1
    assert res.value == 1.0  # top-1 on the aligned cells is perfect; alignment alone fails it


def test_check_jlens_reference_unrestricted_needs_all_cells(
    tiny_backend, fake_jlens_path, i1a_tmp_layout
):
    """A store of 2 labels passes with limit (a subset run) but not as an unrestricted run."""
    capture.capture_family(tiny_backend, C.JLENS_REF_FAMILY, limit=2)
    lens = token_readers.bind_reader("jlens", tiny_backend, jlens_path=fake_jlens_path)
    ref_dir = i1a_tmp_layout / "ref"
    _write_fake_reference(lens, ref_dir)
    assert checks_readers.check_jlens_reference(lens, ref_dir=ref_dir, limit=2).passed is True
    res = checks_readers.check_jlens_reference(lens, ref_dir=ref_dir)
    assert res.passed is False and res.details["complete"] is False
    assert C.N_JLENS_REF_SITES * len(C.JLENS_REF_LAYERS) == 1788


# --------------------------------------------------------------------- no autograd graphs


def _no_graph(t):
    return (not t.requires_grad) and t.grad_fn is None


def test_readers_cache_no_autograd_graph(tiny_backend, fake_jlens_path, i1a_tmp_layout):
    """The unembedding parameter requires grad; no cached denominator may keep a graph."""
    assert tiny_backend.unembed.requires_grad  # the precondition that made this a risk
    capture.capture_family(tiny_backend, C.JLENS_REF_FAMILY, limit=1)
    capture.capture_family(tiny_backend, "hallucination", limit=1)
    lens = token_readers.bind_reader("jlens", tiny_backend, jlens_path=fake_jlens_path)
    assert _no_graph(lens._w_u) and _no_graph(lens._jac)
    token_readers.read_family(lens, tiny_backend, "hallucination")
    ref_dir = i1a_tmp_layout / "ref"
    _write_fake_reference(lens, ref_dir)
    checks_readers.check_jlens_reference(lens, ref_dir=ref_dir)
    assert set(lens._denom) == set(C.FIVE) | set(C.JLENS_REF_LAYERS)
    assert all(_no_graph(d) for d in lens._denom.values())
    # a bare read outside inference mode (as wsbench's Producer does) stays graph-free too
    _, h = capture.load_acts("hallucination", [0])
    lens._denom.clear()
    lens.read(h[0, 0], 0)
    assert _no_graph(lens._denom[0])
    fresh = token_readers.bind_reader_via_wsbench("jlens", tiny_backend, jlens_path=fake_jlens_path)
    fresh.read(h[0, 0], 0)
    assert _no_graph(fresh._w_u) and _no_graph(fresh._denom[0])
    ll = token_readers.bind_reader("logit_lens", tiny_backend)
    assert _no_graph(ll._w_u)


# --------------------------------------------------------------------- top-k vs contract


def test_topk_ties_resolved_and_recorded(tiny_backend, i1a_tmp_layout):
    """All-tied scores: the top-k file's first ids follow read()'s order and the meta lists
    the resolved cells; the contract tokens still match."""
    from wsbench.produce.render import display_tokens

    capture.capture_family(tiny_backend, "multihop", limit=1)
    reader = token_readers.bind_reader("logit_lens", tiny_backend)
    reader._w_u = torch.zeros_like(reader._w_u)
    out = token_readers.read_family(reader, tiny_backend, "multihop")
    meta = json.loads(token_readers.topk_meta_path("logit_lens", "multihop").read_text())
    assert meta["n_cells"] == len(C.GRID) and isinstance(meta["tie_resolved"], list)
    tk = [json.loads(x) for x in C.topk_path("logit_lens", "multihop").read_text().splitlines()]
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    for t, r in zip(tk, rows, strict=True):
        assert display_tokens(tiny_backend.tokenizer, t["ids"][:10]) == r["tokens"]


def test_topk_real_mismatch_fails(tiny_backend, i1a_tmp_layout, monkeypatch):
    capture.capture_family(tiny_backend, "multihop", limit=1)
    reader = token_readers.bind_reader("logit_lens", tiny_backend)
    real = token_readers._scores
    monkeypatch.setattr(token_readers, "_scores", lambda r, h, layer: -real(r, h, layer))
    with pytest.raises(AssertionError):
        token_readers.read_family(reader, tiny_backend, "multihop")


# --------------------------------------------------------------------- store vs producer


def _store_and_read(backend, jlens_path, n_items):
    readers = {}
    for family in C.FAMILIES:
        capture.capture_family(backend, family, limit=n_items)
    for name in token_readers.READERS:
        readers[name] = token_readers.bind_reader(name, backend, jlens_path=jlens_path)
        for family in C.FAMILIES:
            token_readers.read_family(readers[name], backend, family)
    return readers


def test_check_store_vs_producer(bf16_backend, fake_jlens_path, i1a_tmp_layout):
    used = _store_and_read(bf16_backend, fake_jlens_path, 2)
    fresh = [
        token_readers.bind_reader_via_wsbench(n, bf16_backend, jlens_path=fake_jlens_path)
        for n in token_readers.READERS
    ]
    assert all(f is not used[f.name] for f in fresh)
    res = checks_readers.check_store_vs_producer(bf16_backend, fresh, n_items=2)
    assert res.passed is True, res.details
    assert res.details["max_abs_score_diff"] == 0.0
    n_sites = sum(len(capture.load_index(f)) * len(C.FAMILY_LAYERS[f]) for f in C.FAMILIES)
    assert res.n == 2 * n_sites
    assert json.loads(C.check_path("store_vs_producer").read_text())["passed"] is True
    assert "store_vs_producer" in C.CHECK_KIND


@pytest.mark.parametrize("corrupt", ["tokens", "token", "drop_row", "missing_file", "bad_lens"])
def test_check_store_vs_producer_fails(
    corrupt, bf16_backend, fake_jlens_path, i1a_tmp_layout, tmp_path
):
    _store_and_read(bf16_backend, fake_jlens_path, 1)
    names = list(token_readers.READERS)
    path = C.readouts_path("jlens", "multihop")
    lines = path.read_text().splitlines()
    row = json.loads(lines[3])
    if corrupt == "tokens":
        row["tokens"] = row["tokens"][::-1]
        lines[3] = json.dumps(row, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n")
    elif corrupt == "token":
        row["token"] = "zzz"
        lines[3] = json.dumps(row, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n")
    elif corrupt == "drop_row":
        path.write_text("\n".join(lines[:3] + lines[4:]) + "\n")
    elif corrupt == "missing_file":
        path.unlink()
    lens_path = fake_jlens_path
    if corrupt == "bad_lens":  # the stored rows came from a different lens than the fresh one
        g = torch.Generator().manual_seed(7)
        lens_path = tmp_path / "other_jlens.pt"
        torch.save({"J": {L: torch.randn(64, 64, generator=g) for L in range(64)}}, lens_path)
    fresh = [
        token_readers.bind_reader_via_wsbench(n, bf16_backend, jlens_path=lens_path) for n in names
    ]
    res = checks_readers.check_store_vs_producer(bf16_backend, fresh, n_items=1)
    assert res.passed is False and res.details["failures"] >= 1
