"""P03 session 2 readers: unit tests of the pure logic, and the whole `all` step end to end on the
tiny random Qwen3.6-architecture model of tests/i1a/conftest.py (CPU, a few seconds per pass)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from p03 import core
from p03 import read_olens as O
from p03 import read_smoke as S
from p03 import read_store as RS

REPO = core.PROBE.parents[1]


def _i1a_conftest():
    spec = importlib.util.spec_from_file_location(
        "i1a_conftest_p03", REPO / "tests" / "i1a" / "conftest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _site(i: int, item: str, pos: int) -> RS.ReadSite:
    return RS.ReadSite(i, item, pos, 10 + pos, "t", 10, "x")


def _sites(ids: list[str]) -> list[RS.ReadSite]:
    return [
        _site(3 * k + j, i, p) for k, i in enumerate(ids) for j, p in enumerate(core.READ_POSITIONS)
    ]


def _item(
    i: str,
    typ: str = "B",
    group: str = "survivor",
    target: str = "blue",
    modal: str = "blue",
    aliases: list[str] | None = None,
) -> RS.ReaderItem:
    return RS.ReaderItem(
        i, typ, group, f"Prompt {i}?", core.ANSWER_STEM, target, aliases or [], modal
    )


# --------------------------------------------------------------------- items and sites


def test_load_reader_items(tmp_path):
    base = {
        "type": "B",
        "group": "survivor",
        "prompt": "p",
        "prefill": core.ANSWER_STEM,
        "target": "blue",
        "target_aliases": [],
        "modal_answer": "blue",
    }
    p = tmp_path / "items.jsonl"
    core.write_jsonl(p, [{"id": "a", **base, "category": "colour"}, {"id": "b", **base}])
    items = RS.load_reader_items(p)
    assert [it.id for it in items] == ["a", "b"] and items[0].meta == {"category": "colour"}
    core.write_jsonl(p, [{"id": "a", **base}, {"id": "a", **base}])
    with pytest.raises(ValueError, match="duplicate"):
        RS.load_reader_items(p)
    core.write_jsonl(p, [{"id": "a", **base, "group": "other"}])
    with pytest.raises(ValueError, match="group"):
        RS.load_reader_items(p)
    core.write_jsonl(p, [{"id": "a", **{k: v for k, v in base.items() if k != "target"}}])
    with pytest.raises(ValueError, match="lacks"):
        RS.load_reader_items(p)


@pytest.fixture(scope="module")
def qwen_tok():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(
            core.MODEL_ID, revision=core.MODEL_REV, local_files_only=True
        )
    except Exception as e:  # noqa: BLE001 - not cached locally
        pytest.skip(f"Qwen3.6 tokenizer unavailable: {e}")


def test_sites_are_the_last_three_stem_tokens(qwen_tok):
    from wsbench.produce.render import render

    items = [_item("x"), _item("y", typ="C")]
    sl = RS.build_sites(items, qwen_tok)
    sites = [s for s, _ in sl]
    assert [s.pos for s in sites] == [-3, -2, -1, -3, -2, -1]
    assert [s.index for s in sites] == list(range(6))
    rep = RS.stem_token_report(sites)
    assert rep["n_stem_ok"] == 2, rep
    for s, ids in sl:
        assert s.abs_pos == s.n_tokens + s.pos == len(ids) + s.pos
        assert s.token == qwen_tok.decode([ids[s.abs_pos]])
    # the render is wsbench's chat_prefill render, ending in the prefilled stem
    r = render(RS.read_spec(items[0]), qwen_tok)
    assert r.ids == sl[0][1] and qwen_tok.decode(r.ids).endswith(core.ANSWER_STEM)


def test_sites_match_the_session1_sampler_render(qwen_tok):
    """The read context is the sampled context (p03.sample_vllm.render_ids)."""
    from p03 import sample_vllm as sv

    for it in (_item("x"), _item("y", typ="A")):
        assert sv.render_ids(qwen_tok, it.prompt, it.prefill) == RS.render_ids(it, qwen_tok)


# --------------------------------------------------------------------- Oracle Lens plan


def test_batch_constant_matches_i1a():
    from emx.i1a import contracts as I

    assert O.BATCH == I.BATCH == 32


def test_plan_layers_chunks_and_seeds():
    sites = _sites([f"i{k}" for k in range(15)])  # 45 sites
    cells = O.grid_cells(sites, {s.id for s in sites}, (20, 24), core.READ_POSITIONS)
    batches = O.plan("main", cells)
    assert [b.layer for b in batches] == [20, 20, 24, 24]
    assert [len(b.cells) for b in batches] == [32, 13, 32, 13]
    assert [b.seed for b in batches] == [O.SEED_BASE["main"] + i for i in range(4)]
    assert batches == O.plan("main", list(reversed(cells)))  # order-independent
    assert O.n_batches(cells) == 4
    with pytest.raises(ValueError):
        O.plan("main", cells + cells[:1])


def test_derangement_over_items():
    ids = [f"i{k}" for k in range(9)]
    sites = _sites(ids)
    src = O.item_derangement(sites)
    assert set(src) == set(ids) and sorted(src.values()) == sorted(ids)
    assert all(src[i] != i for i in ids)
    assert src == O.item_derangement(sites)  # seeded
    cells = O.shuffled_cells(sites, set(ids), (20, 60), src)
    by = {s.index: s for s in sites}
    assert len(cells) == 18
    assert all(by[c.site].pos == by[c.source].pos == -1 for c in cells)
    assert all(by[c.source].id == src[by[c.site].id] for c in cells)


def _rows_for(batch: O.Batch, sites, draw="main"):
    return [
        O.OlensRow(
            draw,
            sites[c.site].id,
            batch.layer,
            sites[c.site].pos,
            c.site,
            c.source,
            sites[c.source].id,
            batch.seed,
            batch.batch_index,
            "t",
            1,
            "eos",
        )
        for c in batch.cells
    ]


def test_resume_keeps_complete_batches_drops_partial_and_torn(tmp_path):
    sites = _sites([f"i{k}" for k in range(12)])  # 36 sites -> 2 batches per layer
    cells = O.grid_cells(sites, {s.id for s in sites}, (20,), core.READ_POSITIONS)
    batches = O.plan("main", cells)
    p = tmp_path / "main.jsonl"
    full = _rows_for(batches[0], sites)
    part = _rows_for(batches[1], sites)[:2]
    lines = [json.dumps(O.row_dict(r)) for r in full + part]
    p.write_text("\n".join(lines) + '\n{"draw": "main", "id"')  # torn last line
    assert O.load_done(p, "main", batches, sites) == {0}
    assert len(O.read_rows(p)) == 32  # partial batch and torn line removed
    bad = O.row_dict(full[0]) | {"seed": 1}
    p.write_text(json.dumps(bad) + "\n")
    with pytest.raises(ValueError, match="contradicts"):
        O.load_done(p, "main", batches, sites)


def test_generate_resumes_and_stops(tmp_path, monkeypatch):
    import torch

    import emx.i1a.olens as I1

    calls = []

    def fake_read_batch(olens, H, layer, seed, sampling=None):
        calls.append((layer, seed, H.shape[0]))
        return [(f"L{layer}", 2, "eos")] * H.shape[0]

    monkeypatch.setattr(I1, "read_batch", fake_read_batch)
    sites = _sites([f"i{k}" for k in range(12)])
    h = torch.zeros(len(sites), 2, 4, dtype=torch.bfloat16)
    cells = O.grid_cells(sites, {s.id for s in sites}, (20, 24), core.READ_POSITIONS)
    out = tmp_path / "main.jsonl"
    kw = {"sites": sites, "layers_stored": [20, 24], "h": h, "out": out}
    r1 = O.generate(None, "main", cells, stop=lambda: len(calls) >= 1, **kw)
    assert r1["stopped"] and not r1["complete"] and r1["n_generated"] == 1
    r2 = O.generate(None, "main", cells, **kw)
    assert r2["complete"] and r2["n_done_before"] == 1 and r2["n_generated"] == 3
    assert [c[1] for c in calls] == [O.SEED_BASE["main"] + i for i in range(4)]
    r3 = O.generate(None, "main", cells, **kw)
    assert r3["n_generated"] == 0 and r3["complete"]
    ro = O.readout_rows(O.read_rows(out), sites)
    assert (
        len(ro) == 72
        and ro[0]["id"] == "i0"
        and set(ro[0]) == {"id", "layer", "pos", "token", "samples"}
    )


# --------------------------------------------------------------------- smoke logic


def test_names_target_and_top1():
    assert S.names_target("- Blue\n- the sky", "blue")
    assert S.names_target("I think of dolphins", "dolphin")
    assert not S.names_target("bluegrass music", "blue")
    assert S.names_target("Akira Kurosawa directed it", "Kurosawa", ["Akira Kurosawa"])
    assert S.top1_matches(" Blue", "blue")
    assert S.top1_matches(" Ele", "elephant")
    assert not S.top1_matches(" El", "elephant")
    assert not S.top1_matches(" Red", "blue")


def test_check_vllm_vs_hf_threshold():
    rows = [{"key": f"k{i}", "modal_answer": "Blue"} for i in range(10)]
    hf = {f"k{i}": ["blue"] * 20 for i in range(9)} | {"k9": ["red"] * 20}
    assert S.check_vllm_vs_hf(rows, hf).passed
    hf["k8"] = ["green"] * 20
    c = S.check_vllm_vs_hf(rows, hf)
    assert not c.passed and c.value == 8 and c.threshold == 9


def test_check_final_layer_and_shuffled():
    items = [_item(f"b{i}", target="elephant", modal="elephant") for i in range(10)]
    items.append(_item("c", typ="C"))
    final = [{"id": f"b{i}", "top1": " Ele" if i < 9 else " Cat"} for i in range(10)]
    assert S.check_final_layer(items, final).passed  # 9/10 = 0.9
    final[8]["top1"] = " Dog"
    assert not S.check_final_layer(items, final).passed
    by = {it.id: it for it in items}
    cells = [
        {"id": f"b{i}", "source_id": "c", "layer": 44, "text": "- grey animals"} for i in range(10)
    ] * 2
    assert S.check_shuffled(cells, by).passed
    cells[0] = cells[0] | {"text": "- elephant"}
    assert S.check_shuffled(cells, by).passed  # 1 of 20
    cells[1] = cells[1] | {"text": "elephants!"}
    c = S.check_shuffled(cells, by)
    assert not c.passed and c.value == 2
    assert S.drops([c]) == {"jlens": False, "olens": True}


def test_check_jlens_reference_fraction():
    det = {
        "missing": [],
        "site_differs": [],
        "tokens_differ": ["a@L20"],
        "scores_over_tolerance": [],
        "abs_delta": {},
    }
    assert S.check_jlens_reference(det, 33).passed  # 32/33
    det["scores_over_tolerance"] = ["b@L24"]
    assert not S.check_jlens_reference(det, 33).passed  # 31/33 < 0.95
    det = {
        "missing": ["a@L20"],
        "site_differs": [],
        "tokens_differ": [],
        "scores_over_tolerance": [],
    }
    assert not S.check_jlens_reference(det, 100).passed  # a missing cell always fails


def test_grid_levels_and_choice():
    L = S.LEVELS
    assert [g.level for g in L] == [0, 1, 2, 3]
    assert L[0].contrast_positions == core.READ_POSITIONS and L[1].contrast_positions == (-1,)
    assert L[1].main_layers == core.READ_LAYERS and L[2].main_layers == S.SIX
    assert "contrast" in L[2].shuffled_groups and "contrast" not in L[3].shuffled_groups
    batches = {0: 600, 1: 400, 2: 250, 3: 200}
    g, tried = S.choose_level(
        lambda g: batches[g.level],
        seconds_per_batch=17.0,
        elapsed_s=1800,
        max_hours=3.0,
        overhead_s=300,
    )
    # 0: 1800 + 10200 + 300 = 3.42 h over; 1: 1800 + 6800 + 300 = 2.47 h fits
    assert g.level == 1 and [p.level for p in tried] == [0, 1] and tried[0].over
    g, tried = S.choose_level(lambda g: 10_000, seconds_per_batch=17.0, elapsed_s=0, max_hours=3.0)
    assert g.level == 3 and tried[-1].over and len(tried) == 4
    assert S.grid_from_dict(json.loads(json.dumps(__import__("dataclasses").asdict(g)))) == g


# --------------------------------------------------------------------- end to end, tiny model


@pytest.fixture(scope="module")
def tiny_artifacts(tmp_path_factory, qwen_tok):
    torch = pytest.importorskip("torch")
    from transformers import AutoModelForCausalLM

    from emx.i1a import contracts as I

    c = _i1a_conftest()
    torch.manual_seed(0)
    m = AutoModelForCausalLM.from_config(c.tiny_config(), attn_implementation="sdpa")
    m.eval()
    root = tmp_path_factory.mktemp("p03_tiny")
    m.save_pretrained(root / "qwen")
    qwen_tok.save_pretrained(root / "qwen")
    sub = c._save_lora(m, root / "adapters" / I.OLENS_SUBDIR, seed=1)
    (sub / "run.json").write_text("{}")
    g = torch.Generator().manual_seed(5)
    jac = {
        L: torch.eye(c.D_TINY) + 0.1 * torch.randn(c.D_TINY, c.D_TINY, generator=g)
        for L in range(I.N_MODEL_LAYERS)
    }
    torch.save({"J": jac}, root / "jlens.pt")
    return {"qwen_dir": str(root / "qwen"), "jlens": str(root / "jlens.pt"), "olens_dir": str(sub)}


def _write_inputs(tmp: Path) -> tuple[Path, Path]:
    rows = [
        {
            "id": "A1",
            "type": "A",
            "group": "survivor",
            "prompt": "Which country is the director of Film X from?",
            "prefill": core.ANSWER_STEM,
            "target": "Akira Kurosawa",
            "target_aliases": ["Kurosawa"],
            "modal_answer": "Japan",
        },
        {
            "id": "B1",
            "type": "B",
            "group": "contrast",
            "prompt": "What is your favourite colour?",
            "prefill": core.ANSWER_STEM,
            "target": "Blue",
            "target_aliases": [],
            "modal_answer": "Blue",
        },
        {
            "id": "C1",
            "type": "C",
            "group": "ws",
            "prompt": "Think of your favourite animal. Which continent is it mostly found on?",
            "prefill": core.ANSWER_STEM,
            "target": "Elephant",
            "target_aliases": ["elephants"],
            "modal_answer": "Africa",
        },
    ]
    items = tmp / "reader_items.jsonl"
    core.write_jsonl(items, rows)
    smoke = tmp / "smoke_hf.jsonl"
    core.write_jsonl(
        smoke,
        [
            {
                "key": f"{r['id']}|main",
                "prompt": r["prompt"],
                "prefill": r["prefill"],
                "modal_answer": r["modal_answer"],
            }
            for r in rows[:2]
        ],
    )
    return items, smoke


def test_all_end_to_end_on_tiny_model(tmp_path, monkeypatch, tiny_artifacts):
    from p03 import readers as R

    out = tmp_path / "results" / "read"
    monkeypatch.setattr(R, "OUT", out)
    monkeypatch.setattr(R, "READOUTS", out / "readouts")
    monkeypatch.setattr(R, "SAMPLES", out / "olens_samples")
    monkeypatch.setenv("P03_MAX_HOURS", "100")
    monkeypatch.delenv("P03_READ_T0", raising=False)
    out.mkdir(parents=True)
    (out / "download_paths.json").write_text(json.dumps(tiny_artifacts))
    items, smoke = _write_inputs(tmp_path)
    argv = [
        "all",
        "--device",
        "cpu",
        "--dtype",
        "float32",
        "--max-new-tokens",
        "3",
        "--items",
        str(items),
        "--smoke-hf",
        str(smoke),
        "--acts-dir",
        str(tmp_path / "acts"),
    ]
    assert R.main(argv) == 0

    n_layers, n_pos = len(core.READ_LAYERS), len(core.READ_POSITIONS)
    ll = core.read_jsonl(out / "readouts" / "logit_lens.jsonl")
    assert len(ll) == 3 * n_layers * n_pos
    assert set(ll[0]) == {"id", "layer", "pos", "token", "tokens", "scores"}
    assert [r["token"] for r in ll[:3]] == list(RS.STEM_TOKENS)
    assert {r["pos"] for r in ll} == set(core.READ_POSITIONS)
    final = core.read_jsonl(out / "final_layer_top1.jsonl")
    assert [r["id"] for r in final] == ["A1", "B1", "C1"] and all(r["pos"] == -1 for r in final)

    smoke_doc = json.loads((out / "smoke.json").read_text())
    names = [c["name"] for c in smoke_doc["checks"]]
    assert names == ["a_vllm_vs_hf", "d_final_layer_top1", "b_jlens_reference", "c_shuffled_olens"]
    # random weights cannot reproduce I1a's J-Lens readouts: J-Lens is dropped, not lost
    assert smoke_doc["drops"]["jlens"] is True
    assert len(core.read_jsonl(out / "readouts" / "dropped" / "jlens.jsonl")) == len(ll)
    assert not (out / "readouts" / "jlens.jsonl").exists()
    assert smoke_doc["drops"]["olens"] is False

    ol = core.read_jsonl(out / "readouts" / "olens.jsonl")
    # A1 (survivor) and C1 (ws) on the full grid; B1 (contrast) too at level 0
    assert len(ol) == 3 * n_layers * n_pos
    assert set(ol[0]) == {"id", "layer", "pos", "token", "samples"}
    sh = core.read_jsonl(out / "readouts" / "olens_shuffled.jsonl")
    assert len(sh) == 3 * n_layers and all(r["pos"] == -1 for r in sh)
    assert all(r["source_id"] != r["id"] for r in sh)
    timing = json.loads((out / "timing.json").read_text())
    for k in (
        "model_load",
        "smoke_a_hf",
        "capture",
        "logit_lens",
        "jlens",
        "olens_bind",
        "smoke_c_shuffled",
        "olens_timing",
        "olens_main",
        "olens_shuffled",
        "olens_contrast",
        "readouts",
    ):
        assert k in timing and timing[k]["seconds"] >= 0, k
    status = json.loads((out / "status.json").read_text())
    assert status["complete"] and status["grid_level"] == 0 and not status["overrun"]
    grid = json.loads((out / "olens_grid.json").read_text())
    assert grid["grid"]["level"] == 0 and grid["projections"][0]["over"] is False

    # a rerun resumes: the capture is reused and no Oracle Lens batch is generated again
    before = (out / "olens_samples" / "main.jsonl").read_text()
    assert R.main(argv) == 0
    t2 = json.loads((out / "timing.json").read_text())
    assert t2["capture#2"]["captured_now"] is False
    assert t2["olens_main#2"]["n_generated"] == 0
    assert (out / "olens_samples" / "main.jsonl").read_text() == before


def test_hf_samples_deterministic(tiny_artifacts):
    pytest.importorskip("torch")
    from p03 import readers as R

    from emx.i1a import capture as I1cap

    bk = I1cap.load_backend(tiny_artifacts["qwen_dir"], device="cpu", dtype="float32")
    a = R.hf_samples(bk, "Favourite colour?", core.ANSWER_STEM, n=4, seed=7, max_new_tokens=3)
    b = R.hf_samples(bk, "Favourite colour?", core.ANSWER_STEM, n=4, seed=7, max_new_tokens=3)
    assert a == b and len(a) == 4


def test_timing_batch_is_batch0_of_main_at_every_level():
    """The timed batch (batch 0 of the full-grid main draw) must stay batch 0 of the main draw
    at every cut level, so it is kept, never contradicted, on resume."""
    from p03 import readers as R

    ids = [f"i{k}" for k in range(40)]
    sites = _sites(ids)
    items = [_item(i, group="survivor" if k < 30 else "contrast") for k, i in enumerate(ids)]
    src = O.item_derangement(sites)
    b0 = O.plan("main", R._cells_for(S.LEVELS[0], "main", items, sites, src))[0]
    for g in S.LEVELS:
        assert O.plan("main", R._cells_for(g, "main", items, sites, src))[0] == b0
    shuf3 = R._cells_for(S.LEVELS[3], "shuffled", items, sites, src)
    assert {sites[c.site].id for c in shuf3} == set(ids[:30])
    con1 = R._cells_for(S.LEVELS[1], "contrast", items, sites, src)
    assert {sites[c.site].pos for c in con1} == {-1} and len(con1) == 10 * 11
