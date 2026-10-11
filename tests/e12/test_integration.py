"""Cross-unit checks added at integration (2026-10-08): the real U7 decisions are accepted by the
real U6 gate (`emx.e12.llm.require_final`) and the real U5 hand-label page and scorer, with the
same `decisions_sha256`; a tampered endpoint or a fabricated hash is refused by all of them
(review r1 finding 11)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from emx.e12 import analysis as A
from emx.e12 import contracts as C
from emx.e12 import llm

from . import synth

ROOT = Path(__file__).resolve().parents[2]


def _page_module():
    spec = importlib.util.spec_from_file_location(
        "e12_handlabel_page", ROOT / "scripts" / "e12_handlabel_page.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_decisions_hash_agrees_across_units(tmp_path):
    synth.build(tmp_path)
    doc, dsha = synth.decide_all(tmp_path)
    final = doc["final"]
    got_final, got = llm.require_final(tmp_path, "rank1")
    assert got == dsha == final["decisions_sha256"] == got_final["decisions_sha256"]
    page = _page_module()
    assert page.load_final(tmp_path, "rank1")["decisions_sha256"] == dsha
    assert A.decisions_hash(json.loads((tmp_path / C.decisions_path("rank1")).read_text())) == dsha

    p = tmp_path / C.decisions_path("rank1")
    doc = json.loads(p.read_text())
    doc["final"]["endpoint"] = 214 if doc["final"]["endpoint"] == 107 else 107
    p.write_text(json.dumps(doc))
    with pytest.raises(llm.Refused):
        llm.require_final(tmp_path, "rank1")


def _scorer_module():
    spec = importlib.util.spec_from_file_location(
        "e12_handlabel_score", ROOT / "scripts" / "e12_handlabel_score.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _labels_file(tmp_path, key: dict) -> Path:
    labs = [{"id": h, "label": ("em" if it.get("gfl_em") else "not") if it["stratum"] == "em"
             else ("acceptable" if it["luna_acceptable"] else "poor")}
            for h, it in key["items"].items()]
    p = tmp_path / "labels.json"
    p.write_text(json.dumps(labs))
    return p


@pytest.mark.parametrize("tamper", ["endpoint", "fabricated_hash"])
def test_handlabel_page_and_scorer_refuse_a_tampered_decision(tmp_path, tamper):
    """Review r1 finding 11: the page and the scorer load the decision through the verified
    loader (`llm.require_final`), so they accept the real U7 decision and refuse a tampered
    endpoint or a fabricated (well-formed) hash."""
    root = tmp_path / "t"
    world = synth.build(root)
    dsha = synth.finish(root, world)
    refs = root / C.DATA_FILES["holdout"].local
    refs.parent.mkdir(parents=True, exist_ok=True)
    refs.write_text("".join(json.dumps({k: h[k] for k in ("original_index", "prompt",
                                                          "good_completion", "bad_completion")})
                            + "\n" for h in synth.HOLDOUT))
    page, scorer = _page_module(), _scorer_module()
    argv = ["--recipe", "rank1", "--root", str(root)]
    assert page.main(argv) == 0
    key = json.loads(page.key_path(root, "rank1").read_text())
    assert key["decisions_sha256"] == dsha
    labels = _labels_file(tmp_path, key)
    assert scorer.main([*argv, "--labels", str(labels)]) == 0

    p = root / C.decisions_path("rank1")
    doc = json.loads(p.read_text())
    if tamper == "endpoint":
        doc["final"]["endpoint"] = C.RESCUE_END
    else:
        doc["final"]["decisions_sha256"] = "ab" * 32
    p.write_text(json.dumps(doc))
    with pytest.raises(SystemExit) as e:
        page.main(argv)
    assert e.value.code == C.EXIT_REFUSED
    with pytest.raises(SystemExit) as e:
        scorer.main([*argv, "--labels", str(labels)])
    assert e.value.code == C.EXIT_REFUSED
