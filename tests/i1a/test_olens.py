"""U3 batched Oracle Lens on the tiny Qwen3.5-architecture model with the dummy LoRA."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from emx.i1a import contracts as C

torch = pytest.importorskip("torch")

from emx.i1a import olens as O
from emx.i1a.checks_olens import check_adapter_live, check_batching

TINY_SAMPLING = {"max_new_tokens": 4}


def _sampling():
    return replace(O.default_sampling(), **TINY_SAMPLING)


def _sites(n: int, family: str = "basic_readout_mt", per_item: int = 1) -> list[C.SiteRow]:
    return [
        C.SiteRow(
            family=family,
            index=i,
            id=f"item-{i // per_item:03d}",
            pos=-1 - (i % per_item),
            abs_pos=10 - (i % per_item),
            token=f"tok{i}",
            n_tokens=11,
        )
        for i in range(n)
    ]


def _acts(n: int, **kw):
    g = torch.Generator().manual_seed(7)
    return _sites(n, **kw), torch.randn(n, C.N_MODEL_LAYERS, 64, generator=g).to(torch.bfloat16)


@pytest.fixture
def olens(tiny_backend, dummy_olens_dir):
    o = O.PublicOLens(sampling=_sampling())
    o.bind(tiny_backend, dummy_olens_dir)
    return o


# ------------------------------------------------------------------------------ pure logic


def test_recipe_matches_wsbench():
    from wsbench.produce.methods import OLens, Sampling

    ref, s = OLens(), Sampling()
    assert O.PublicOLens().prompt == ref.prompt == C.OLENS_PROMPT
    assert O.PublicOLens().alpha == ref.alpha == C.OLENS_ALPHA
    d = O.default_sampling()
    assert (d.temperature, d.top_p, d.top_k, d.max_new_tokens, d.k) == (
        s.temperature,
        s.top_p,
        s.top_k,
        s.max_new_tokens,
        s.k,
    )


def test_batch_plan_canonical_order():
    layers = (28, 20, 24)
    plan = O.batch_plan(40, layers, "extra")
    per_layer = 40 * 9
    assert len(plan) == 3 * -(-per_layer // C.BATCH)
    assert [b.batch_index for b in plan] == list(range(len(plan)))
    assert all(b.seed == C.SEED_BASE["extra"] + b.batch_index for b in plan)
    assert [b.layer for b in plan] == sorted(b.layer for b in plan)
    for L in sorted(layers):
        units = [u for b in plan if b.layer == L for u in b.units]
        assert units == [(si, k) for k in range(1, 10) for si in range(40)]  # sample-major
    assert all(1 <= len(b.units) <= C.BATCH for b in plan)
    # the gate draw: sample 0 only, its own seeds
    gate = O.batch_plan(40, layers, "gate")
    assert [b.units[0] for b in gate[:2]] == [(0, 0), (32, 0)] and gate[1].seed == 20260929
    assert len(gate) == 6 and gate[2].layer == 24 and gate[2].batch_index == 2


def test_batch_plan_filter_and_bad_draw():
    plan = O.batch_plan(10, [20], "gate", site_indices=[7, 2, 5])
    assert plan[0].units == ((2, 0), (5, 0), (7, 0))
    with pytest.raises(ValueError):
        O.batch_plan(10, [20], "nope")
    with pytest.raises(ValueError):
        O.batch_plan(3, [20], "gate", site_indices=[5])


def test_batches_never_mix_layers():
    for draw in ("gate", "extra"):
        for b in O.batch_plan(37, C.GRID, draw):
            # a Batch carries one layer; its units are one contiguous chunk of that layer's list
            layer_units = [(si, k) for k in O.draw_samples(draw) for si in range(37)]
            first = layer_units.index(b.units[0])
            assert list(b.units) == layer_units[first : first + len(b.units)]


def test_finish_stats():
    out = torch.tensor([[5, 6, 9, 1], [5, 6, 7, 8], [9, 1, 1, 1]])
    assert O.finish_stats(out, {9}) == [(2, "eos"), (4, "length"), (0, "eos")]


def test_derangement():
    sites = _sites(12, per_item=2)
    perm = O.derangement(sites)
    assert sorted(perm) == list(range(12))
    assert all(sites[perm[i]].id != sites[i].id for i in range(12))
    assert perm == O.derangement(sites)  # a pure function of the seed


# ------------------------------------------------------------------------------ tiny model


def test_bind_is_live_and_named(olens, tiny_backend):
    from peft import PeftModel

    assert isinstance(tiny_backend.model, PeftModel)
    assert tiny_backend.model.active_adapter == "olens"
    assert olens.n_live_tensors > 0
    assert O.logit_shift(olens, layer=20) > 0.0


def test_bind_on_existing_peft_model_keeps_other_adapter(tiny_backend, dummy_olens_dir):
    from peft import PeftModel

    tiny_backend.model = PeftModel.from_pretrained(
        tiny_backend.model, str(dummy_olens_dir), adapter_name="ar"
    )
    o = O.PublicOLens(sampling=_sampling())
    o.bind(tiny_backend, dummy_olens_dir)
    m = tiny_backend.model
    assert set(m.peft_config) == {"ar", "olens"} and m.active_adapter == "olens"
    m.set_adapter("ar")
    O.read_batch(o, torch.randn(1, 64), 20, 0)
    assert m.active_adapter == "olens"


def test_read_batch_b1_matches_wsbench_read(olens):
    """One-sequence batch == wsbench OLens.read with the same seed: same injection, sampling,
    decoding."""
    from wsbench.produce.methods import OLens

    h = torch.randn(64)
    got = O.read_batch(olens, h[None], 24, seed=123)
    torch.manual_seed(123)
    ref = OLens.read(olens, h, 24).samples
    assert [t for t, _, _ in got] == ref
    assert got[0][1] <= TINY_SAMPLING["max_new_tokens"] and got[0][2] in ("eos", "length")


def test_same_seed_same_batch(olens):
    H = torch.randn(3, 64)
    a = O.read_batch(olens, H, 20, seed=5)
    b = O.read_batch(olens, H, 20, seed=5)
    c = O.read_batch(olens, H, 20, seed=6)
    assert a == b and len(a) == 3
    assert a != c


def test_generate_draw_gate_equals_wsbench_one_at_a_time(olens, i1a_tmp_layout, monkeypatch):
    """With BATCH = 1 each batch is one wsbench read seeded SEED_BASE + batch_index, so sample 0
    through write_arm must equal the wsbench gate readout rows exactly."""
    from wsbench.produce.methods import OLens, Readout
    from wsbench.produce.producer import Row

    monkeypatch.setattr(C, "BATCH", 1)
    sites, H = _acts(2)
    layers = [20, 24]
    p = O.generate_draw(olens, "basic_readout_mt", "gate", acts=(sites, H), layers=layers)
    assert p == C.samples_path("basic_readout_mt")
    rows = O.read_samples(p)
    assert len(rows) == 4 and {r.sample_idx for r in rows} == {0}
    arm = O.write_arm("basic_readout_mt", "olens_rl600", rows, sites=sites)
    got = [json.loads(x) for x in arm.read_text().splitlines()]
    want = []
    for b in O.batch_plan(2, layers, "gate"):
        ((si, _),) = b.units
        torch.manual_seed(b.seed)
        ro = OLens.read(olens, H[si, b.layer].float(), b.layer)
        want.append(Row(sites[si].id, b.layer, sites[si].pos, sites[si].token, ro))
    want.sort(key=lambda r: (r.id, r.layer))
    assert got == [r.contract() for r in want]
    assert all(isinstance(r.readout, Readout) for r in want)


def test_generate_draw_resumes_without_duplicates(olens, i1a_tmp_layout, monkeypatch):
    monkeypatch.setattr(C, "BATCH", 2)
    sites, H = _acts(3)
    kw = {"acts": (sites, H), "layers": [20, 24]}
    p = O.generate_draw(olens, "basic_readout_mt", "gate", **kw)
    full = p.read_text().splitlines()
    assert len(full) == 6  # 3 sites x 2 layers, batches of 2 -> 4 batches
    assert len({json.loads(x)["batch_index"] for x in full}) == 4

    calls = []
    real = O.read_batch

    def counting(*a, **k):
        calls.append(a[2])
        return real(*a, **k)

    monkeypatch.setattr(O, "read_batch", counting)
    O.generate_draw(olens, "basic_readout_mt", "gate", **kw)
    assert calls == [] and p.read_text().splitlines() == full

    # a crash mid-write: batch 2 half-written (its second line torn), batch 3 never started
    p.write_text("\n".join(full[:4]) + "\n" + full[4][:20])
    O.generate_draw(olens, "basic_readout_mt", "gate", **kw)
    after = p.read_text().splitlines()
    assert calls == [24, 24] and sorted(after) == sorted(full)  # same seed -> same texts
    keys = [(r.draw, r.site_index, r.layer, r.sample_idx) for r in O.read_samples(p)]
    assert len(keys) == len(set(keys))

    # an extra draw appends to the same file without touching the gate rows
    monkeypatch.setattr(C, "N_SAMPLES", 3)
    monkeypatch.setattr(O, "read_batch", real)
    O.generate_draw(olens, "basic_readout_mt", "extra", site_filter={"item-001"}, **kw)
    extra = [r for r in O.read_samples(p) if r.draw == "extra"]
    assert {(r.site_index, r.sample_idx) for r in extra} == {(1, 1), (1, 2)}
    assert all(r.seed == C.SEED_BASE["extra"] + r.batch_index for r in extra)
    assert p.read_text().splitlines()[:6] == after


def test_generate_draw_rejects_a_different_plan(olens, i1a_tmp_layout, monkeypatch):
    monkeypatch.setattr(C, "BATCH", 2)
    sites, H = _acts(3)
    O.generate_draw(olens, "basic_readout_mt", "gate", acts=(sites, H), layers=[20])
    with pytest.raises(ValueError, match="does not match the plan"):
        O.generate_draw(
            olens,
            "basic_readout_mt",
            "gate",
            acts=(sites, H),
            layers=[20],
            site_filter={"item-002"},
        )


def test_generate_shuffled(olens, i1a_tmp_layout, monkeypatch):
    sites, H = _acts(4)
    seen = []
    real = O.read_batch

    def spy(o, Hb, layer, seed, **k):
        seen.append((layer, Hb.clone()))
        return real(o, Hb, layer, seed, **k)

    monkeypatch.setattr(O, "read_batch", spy)
    out = O.generate_shuffled(olens, acts=(sites, H), layers=[20, 28])
    assert out == C.readouts_path("olens_shuffled", "basic_readout_mt")
    perm = json.loads((C.SAMPLES_DIR / "basic_readout_mt.shuffled.perm.json").read_text())["perm"]
    for layer, Hb in seen:  # target i read site perm[i]'s activation at the same layer
        assert torch.equal(Hb, H[perm, layer])
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(rows) == 8
    by_id = {s.id: s for s in sites}
    assert all(r["token"] == by_id[r["id"]].token and r["pos"] == by_id[r["id"]].pos for r in rows)
    assert not (C.samples_path("basic_readout_mt")).exists()  # sample 0 file untouched


def test_write_arm_validates(i1a_tmp_layout):
    sites = _sites(2)
    row = C.SampleRow("basic_readout_mt", "item-000", 20, -1, 0, 0, "gate", 1, 0, "x", 1, "eos")
    with pytest.raises(ValueError):
        O.write_arm("multihop", "olens_s1", [row], sites=sites)
    with pytest.raises(ValueError, match="duplicate"):
        O.write_arm("basic_readout_mt", "olens_rl600", [row, row], sites=sites)
    p = O.write_arm("basic_readout_mt", "olens_s1", [row], sites=sites)
    assert json.loads(p.read_text()) == {
        "id": "item-000",
        "layer": 20,
        "pos": -1,
        "token": "tok0",
        "samples": ["x"],
    }


def test_check_adapter_live(olens, tiny_backend, i1a_tmp_layout):
    res = check_adapter_live(olens, tiny_backend, layer=20, H=torch.randn(1, 64))
    assert res.passed, res.details
    assert res.details["capture_equals_adapter_off"] and res.details["n_live_lora_tensors"] > 0
    assert C.check_path("olens_adapter_live").exists()


def test_check_batching(olens, i1a_tmp_layout):
    res = check_batching(olens, torch.randn(3, 64), 20, n=3, steps=4)
    assert res.passed, res.details
    assert res.n == sum(res.details["cont_lengths"]) and res.value >= 0.995
    assert json.loads(C.check_path("olens_batching").read_text())["passed"] is True


# ------------------------------------------------------------------------------ draw scope


def test_batch_plan_reproduces_expected_batches():
    n_sites = {**C.N_SITES, C.OLDSITE_FAMILY: C.N_OLDSITE_ITEMS}
    assert set(C.EXPECTED_BATCHES) == set(C.DRAW_SCOPE)
    for (draw, family), want in C.EXPECTED_BATCHES.items():
        n = n_sites[family]
        sub = range(C.HALLU_SUBSET_N_SITES) if C.DRAW_SCOPE[(draw, family)] == "subset" else None
        plan = O.batch_plan(n, O.draw_layers(family), draw, site_indices=sub)
        assert len(plan) == want, (draw, family)
    assert sum(C.EXPECTED_BATCHES.values()) == 1487


def _fake_read_batch(o, H, layer, seed, **k):
    return [(f"L{layer}s{seed}r{i}", 1, "eos") for i in range(H.shape[0])]


def test_generate_draw_scope(i1a_tmp_layout, monkeypatch):
    monkeypatch.setattr(O, "read_batch", _fake_read_batch)
    sites, H = _acts(4)
    with pytest.raises(ValueError, match="DRAW_SCOPE"):
        O.generate_draw(None, "multihop", "extra", acts=(sites, H))
    # ("extra", "hallucination") is "subset": only ids in the committed SUBSET_PATH
    sub = sorted(O.subset_ids())
    ids = [sub[0], "not-in-subset-a", sub[1], "not-in-subset-b"]
    hsites = [replace(s, family="hallucination", id=i) for s, i in zip(sites, ids, strict=True)]
    p = O.generate_draw(None, "hallucination", "extra", acts=(hsites, H))
    rows = O.read_samples(p)
    assert {r.id for r in rows} == {sub[0], sub[1]}
    assert len(rows) == 2 * 5 * (C.N_SAMPLES - 1)  # 2 sites x 5 layers x 9 samples
    # the gate draw on the same family covers every site
    O.generate_draw(None, "hallucination", "gate", acts=(hsites, H))
    gate = [r for r in O.read_samples(p) if r.draw == "gate"]
    assert {r.id for r in gate} == set(ids) and len(gate) == 4 * 5


# ------------------------------------------------------------------------------ review r1 fixes


def test_read_activates_olens_after_ar(tiny_backend, dummy_olens_dir, _tiny_model_master, tmp_path):
    """PublicOLens.read (one at a time) must not generate with the reconstructor's adapter."""
    from peft import PeftModel

    from i1a.conftest import _save_lora

    ar_dir = _save_lora(_tiny_model_master, tmp_path / "ar", seed=9)  # different weights
    tiny_backend.model = PeftModel.from_pretrained(
        tiny_backend.model, str(ar_dir), adapter_name="ar"
    )
    o = O.PublicOLens(sampling=_sampling())
    o.bind(tiny_backend, dummy_olens_dir)
    m = tiny_backend.model
    ids, _ = o._carrier(20)
    x = torch.tensor([ids])
    with torch.no_grad():
        under_olens = m(x).logits
        m.set_adapter("ar")
        under_ar = m(x).logits
    assert not torch.allclose(under_ar, under_olens)  # the switch matters
    h = torch.randn(64)
    m.set_adapter("olens")
    torch.manual_seed(11)
    want = o.read(h, 20).samples
    m.set_adapter("ar")
    torch.manual_seed(11)
    got = o.read(h, 20).samples
    assert m.active_adapter == "olens" and got == want


def _swap_rows_in_batches(real):
    def patched(o, H, layer):
        if H.shape[0] > 1:
            H = H.clone()
            H[[0, 1]] = H[[1, 0]]
        return real(o, H, layer)

    return patched


def _shift_slot(real):
    """An injection bug shared by all of OUR paths: the vector lands one slot early."""

    def patched(o, H, layer):
        e = real(o, H, layer)
        ids, slot = o._carrier(layer)
        with torch.no_grad():
            base = o._backend().model.get_input_embeddings()(torch.tensor([ids]))[0]
            e = e.clone()
            e[:, slot - 1] = e[:, slot]
            e[:, slot] = base[slot]
        return e

    return patched


@pytest.mark.parametrize("bug", [_swap_rows_in_batches, _shift_slot])
def test_check_batching_catches_injection_bugs(olens, i1a_tmp_layout, monkeypatch, bug):
    from emx.i1a import checks_olens

    monkeypatch.setattr(checks_olens, "carrier_embeds", bug(O.carrier_embeds))
    res = check_batching(olens, torch.randn(3, 64), 20, n=3, steps=4, write=False)
    assert res.passed is False, res.details
    if bug is _shift_slot:  # our paths agree with each other; only the wsbench reference differs
        assert res.details["greedy_tokens_equal_frac"] == 1.0
        assert res.details["wsbench_text_equal_frac"] < 1.0


def test_torn_line_of_another_draw_is_not_lost(i1a_tmp_layout, monkeypatch, capsys):
    monkeypatch.setattr(O, "read_batch", _fake_read_batch)
    sites, H = _acts(3)
    fam = "basic_readout_mt"
    O.generate_draw(None, fam, "gate", acts=(sites, H))
    p = O.generate_draw(None, fam, "extra", acts=(sites, H))
    full = p.read_text().splitlines()
    last_bi = json.loads(full[-1])["batch_index"]
    # a crash while appending the last extra batch: its final line is torn
    p.write_text("\n".join(full[:-1]) + "\n" + full[-1][:25])
    O.generate_draw(None, fam, "gate", acts=(sites, H))  # resuming gate cleans the extra batch
    assert "extra" in capsys.readouterr().out
    left = O.read_samples(p)
    assert not [r for r in left if r.draw == "extra" and r.batch_index == last_bi]
    O.generate_draw(None, fam, "extra", acts=(sites, H))  # the extra draw restores it
    assert sorted(p.read_text().splitlines()) == sorted(full)


def test_resume_validates_id_and_pos(i1a_tmp_layout, monkeypatch):
    monkeypatch.setattr(O, "read_batch", _fake_read_batch)
    sites, H = _acts(3)
    p = O.generate_draw(None, "basic_readout_mt", "gate", acts=(sites, H))
    rows = p.read_text().splitlines()
    bad = json.loads(rows[0]) | {"pos": 5}
    p.write_text("\n".join([json.dumps(bad), *rows[1:]]) + "\n")
    with pytest.raises(ValueError, match="does not match site"):
        O.generate_draw(None, "basic_readout_mt", "gate", acts=(sites, H))
