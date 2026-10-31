"""R7's committed plan, edit hashes, and design-card layout; no raw data or paid calls."""
import copy
import json
from pathlib import Path

import pytest

from compaction.s2.chain import week2 as W

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/week2/r7_planted.toml"
PLAN = ROOT / "docs/sprint/plans/week2-r7-planted.md"


def test_committed_plan_and_all_specs_match():
    assert W.gate(PLAN, CONFIG)["ok"]
    cfg = W.load_config(CONFIG)
    manifest = json.loads(W.resolve(cfg["replay"]["manifest"]["path"]).read_text())
    assert set(manifest["histories"]) == set(cfg["replay"]["histories"])
    for spec in cfg["wording"].values():
        pairs = json.loads(W.resolve(spec["path"]).read_text())
        assert pairs and all(set(p) == {"find", "replace"} for p in pairs)


@pytest.mark.parametrize("mutation,match", [
    (lambda c: c["replay"]["histories"].pop(), "12 divergent"),
    (lambda c: c["replay"].update(new_runs=[1, 1]), "planted x2"),
    (lambda c: c["replay"].update(existing_honest_runs=0), "existing"),
    (lambda c: c["wording"].pop("tengo-np-1"), "one edit spec"),
    (lambda c: c["wording"]["tengo-np-1"].update(sha256="0" * 64), "sha256 mismatch"),
    (lambda c: c["replay"]["manifest"].update(sha256="0" * 64), "sha256 mismatch"),
])
def test_contract_refuses_incomplete_design_or_changed_artifacts(mutation, match):
    cfg = copy.deepcopy(W.load_config(CONFIG))
    cfg.pop("_source")
    mutation(cfg)
    with pytest.raises(ValueError, match=match):
        W.validate(cfg)


def test_design_card_counts_reused_honest_separately(tmp_path):
    cfg = W.load_config(CONFIG)
    rows = W.r7_rows(cfg)
    assert len(rows) == len({r["id"] for r in rows}) == 48
    assert sum(r["start"] == "replayed" for r in rows) == 36
    assert sum(r["start"] == "existing bulk" for r in rows) == 12
    assert {r["id"] for r in rows if r["start"] == "existing bulk"} == {
        f"{h}-honest-1" for h in cfg["replay"]["histories"]}
    card = W.design_card(cfg, rows, tmp_path / "card.md")
    assert "**48**" in card and "36 new task-2 runs" in card
    for spec in cfg["wording"].values():
        assert spec["sha256"] in card
