"""Cross-unit checks added at integration (2026-10-08): the real U7 decisions are accepted by the
real U6 gate (`emx.e12.llm.require_final`) and the real U5 hand-label loader, with the same
`decisions_sha256`; a tampered decision is refused by all three."""

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
