"""E9 provenance check (scripts/e9_provenance_check.py): pass on a committed synthetic results
tree; fail on an edited table number, a modified input, an uncommitted record, a stale table,
and an input that is neither committed nor in a committed sha manifest."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from emx.e9 import contracts as C
from emx.e9 import replay as RP

from .test_replay import build_world

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PROV = _load("e9_provenance_check")
CLI = _load("e9_analyze")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                           *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("prov")
    git(root, "init", "-q")
    cfg = build_world(root, arms=("bg", "orig", "corr"))
    (root / "cfg.json").write_text(json.dumps(cfg))
    for cmd in ("endpoint-precision", "pilot", "gate"):
        assert CLI.main(["--root", str(root), "--config", "cfg.json", cmd]) == 0
    assert CLI.main(["--root", str(root), "tables"]) == 0
    tables = (root / C.ANALYSIS_DIR / "tables.md").read_text()
    rep = root / C.REPORT_LOG
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text("# E9 report\n\n| cost | $ |\n|---|---|\n| pods | 12.34 |\n\n" + tables)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "results")
    return root


def _restore(root: Path):
    git(root, "checkout", "-q", "--", ".")
    git(root, "clean", "-qfd")


def test_clean_tree_passes(world):
    assert PROV.run(world) == []
    assert PROV.main(["--root", str(world)]) == 0


def test_edited_table_number_fails(world):
    rep = world / C.REPORT_LOG
    text = rep.read_text()
    rec = json.loads((world / RP.record_path("gate")).read_text())
    num = f"{rec['contrasts']['corr-orig']['delta']['point']:.4f}"
    assert num in text
    try:
        rep.write_text(text.replace(num, "-0.9999", 1))
        git(world, "commit", "-qam", "edit")
        probs = PROV.run(world, recompute=False)
        assert any("differs from the table rendered" in p for p in probs)
        assert any("number -0.9999 not in" in p for p in probs)
        assert PROV.main(["--root", str(world), "--no-recompute"]) == 1
    finally:
        git(world, "reset", "-q", "--hard", "HEAD~1")


def test_modified_input_fails(world):
    p = next((world / C.JUDGED_DIR).glob("*.csv"))
    try:
        p.write_text(p.read_text() + "\n")
        probs = PROV.run(world, recompute=False)
        assert any("sha256" in x and p.name in x for x in probs)
    finally:
        _restore(world)


def test_uncommitted_record_and_recompute_mismatch(world):
    rp = world / RP.record_path("pilot")
    rec = json.loads(rp.read_text())
    try:
        rec["outcomes"]["promising"] = "fail"
        rp.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
        probs = PROV.run(world)
        assert any("not committed" in x and "pilot" in x for x in probs)
        assert any("recomputed record differs" in x and "pilot" in x for x in probs)
    finally:
        _restore(world)


def test_stale_table_marker(world):
    rp = world / RP.record_path("gate")
    try:
        rp.write_text(rp.read_text().replace('"name": "gate"', '"name": "gate" '))
        git(world, "commit", "-qam", "touch")
        probs = PROV.run(world, recompute=False)
        assert any("stale" in x for x in probs)
    finally:
        git(world, "reset", "-q", "--hard", "HEAD~1")


def test_input_only_in_a_committed_manifest(world):
    """A gitignored-style input is fine when a committed sha manifest lists its hash."""
    p = next((world / C.JUDGED_DIR).glob("*.csv"))
    rel = p.relative_to(world).as_posix()
    try:
        git(world, "rm", "-q", "--cached", rel)
        git(world, "commit", "-qm", "untrack")
        probs = PROV.run(world, recompute=False)
        assert any("neither committed nor in a committed sha manifest" in x for x in probs)
        man = world / C.JUDGED_DIR / C.MANIFEST
        man.write_text(f"{RP.sha256_file(p)}  {p.name}\n")
        git(world, "add", man.relative_to(world).as_posix())
        git(world, "commit", "-qm", "manifest")
        assert not [x for x in PROV.run(world, recompute=False) if rel in x]
    finally:
        git(world, "reset", "-q", "--hard", "HEAD~2")
        _restore(world)
