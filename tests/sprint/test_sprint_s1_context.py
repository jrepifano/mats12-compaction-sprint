"""Checks that the slide-4 chart (scripts/sprint_fig_s1_sources.py) plots exactly the numbers in
results/sprint/S1/context_lengths.json, and that those counts match the S1 manifest and roster."""

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import sprint_fig_s1_sources as f

DATA = json.loads((ROOT / f.SRC).read_text())
MANIFEST = json.loads((ROOT / "results/sprint/S1/manifest.json").read_text())
ROSTER = json.loads((ROOT / "results/sprint/S1/sealed/main/partA_roster_haiku-max.json").read_text())


@pytest.fixture(scope="module")
def drawn():
    """Draw the chart without writing files: v2.save is replaced by a capture."""
    got = {}

    def fake_save(fig, name, title, claim, sources, numbers, notes):
        got.update(fig=fig, name=name, title=title, numbers=numbers)

    mp = pytest.MonkeyPatch()
    mp.setattr(f.v2, "save", fake_save)
    try:
        title, rows = f.draw(DATA)
    finally:
        mp.undo()
    yield got, title, rows
    f.plt.close(got["fig"])


def test_counts_match_manifest_and_roster():
    sampled = Counter(r.split(":", 1)[0] for r in MANIFEST["ids"]["part_a"])
    analysed = Counter(r.split(":", 1)[0] for r in MANIFEST["ids"]["part_a"] if ROSTER[r]["ok"])
    assert set(DATA["sources"]) == set(sampled) == set(f.SOURCES)
    for src, s in DATA["sources"].items():
        assert s["n_sampled"] == sampled[src]
        assert s["n_analysed"] == analysed[src]
        assert len(s["excluded_ids"]) == s["n_sampled"] - s["n_analysed"]
    assert DATA["n_sampled"] == len(MANIFEST["ids"]["part_a"]) == 650
    assert DATA["n_analysed"] == sum(analysed.values()) == 649
    assert {s: DATA["sources"][s]["n_analysed"] for s in f.SOURCES} == {
        "jacob": 50, "swe-chat-v1": 250, "posttrainbench": 150, "frontierml": 199}


def test_inputs_copy_the_manifest_hashes():
    want = [{k: x[k] for k in ("source", "path", "sha256", "meta_sha256") if k in x}
            for x in MANIFEST["files"]]
    assert DATA["inputs"]["files"] == want
    assert DATA["inputs"]["line_sha256_checked"] == 650
    assert DATA["field"] == "pre_tokens" and DATA["unit"] == "tokens"


def test_stats_are_ordered():
    for s in DATA["sources"].values():
        t = s["tokens"]
        assert t["min"] <= t["p10"] <= t["median"] <= t["p90"] <= t["max"]


def test_bar_heights_and_count_labels_equal_the_json(drawn):
    got, _, _ = drawn
    ax = got["fig"].axes[0]
    heights = [c.patches[0].get_height() for c in ax.containers]
    assert heights == [DATA["sources"][s]["n_analysed"] for s in f.SOURCES]
    texts = [t.get_text() for t in ax.texts]
    for s in f.SOURCES:
        assert str(DATA["sources"][s]["n_analysed"]) in texts
    assert [t.get_text() for t in ax.get_xticklabels()] == [f.SOURCES[s][0] for s in f.SOURCES]


def test_context_labels_equal_the_json(drawn):
    got, _, _ = drawn
    texts = [t.get_text() for t in got["fig"].axes[0].texts]
    for s in f.SOURCES:
        t = DATA["sources"][s]["tokens"]
        want = f"median {f.tok(t['median'])} tokens\n({f.tok(t['p10'])}–{f.tok(t['p90'])})"
        assert want in texts
    assert "median 467k tokens\n(285k–969k)" in texts  # the formatting itself, once by hand


def test_title_numbers_come_from_the_json(drawn):
    got, title, _ = drawn
    pub = [DATA["sources"][s]["tokens"]["median"] for s in f.SOURCES if s != "jacob"]
    assert got["title"] == title
    assert title.startswith(f"{DATA['n_analysed']} real compactions")
    assert f"{f.tok(min(pub))}–{f.tok(max(pub))}" in title
    assert f"my own sessions at {f.tok(DATA['sources']['jacob']['tokens']['median'])}" in title


def test_readme_section_matches_the_json():
    text = (f.v2.OUT / "README.md").read_text()
    sec = text[text.index(f.START):text.index(f.END)]
    for s in f.SOURCES:
        d, t = DATA["sources"][s], DATA["sources"][s]["tokens"]
        assert (f"{f.SOURCES[s][0]}: {d['n_analysed']} compactions ({d['n_sampled']} sampled); "
                f"context median {f.exact(t['median'])} tokens, p10 {f.exact(t['p10'])}, "
                f"p90 {f.exact(t['p90'])}, min {t['min']:,}, max {t['max']:,}") in sec
    assert "context median 466,577.5 tokens, p10 284,642.7, p90 968,714.6" in sec


def test_tok_format():
    assert f.tok(466577.5) == "467k"
    assert f.tok(155125.2) == "155k"
    assert f.tok(1_410_364) == "1.41M"
