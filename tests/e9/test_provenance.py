"""E9 provenance check (scripts/e9_provenance_check.py): pass on a committed synthetic results
tree with an orchestrator-shaped gate log; fail on an edited table number, a modified input, an
uncommitted record, a stale table, an input outside any committed manifest, and on missing
required evidence (review R3 #11): records a gate line names, an empty analysis directory, the
ledger and reservation snapshot after a launch, an uncited record version, analysis tables
outside markers, and (final check) analyses without a marked table."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from emx.e9 import contracts as C
from emx.e9 import replay as RP

from .test_replay import build_world, orchestrator_log

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PROV = _load("e9_provenance_check")
CLI = _load("e9_analyze")
SNAPSHOT = "results/E9/reservations_snapshot.jsonl"


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
    rep.write_text("# E9 report\n\n## Costs\n\n| cost | $ |\n|---|---|\n| pods | 12.34 |\n\n"
                   "## Results\n\n" + tables)
    log = orchestrator_log(root)
    (root / PROV.LEDGER).write_text('{"exp": "E9", "usd": 1.0}\n')
    (root / SNAPSHOT).write_text(snapshot_rows(sum(x.get("kind") == "launch" for x in log)))
    (root / PROV.BRANCHES).write_text(json.dumps(
        {"gradient_screen": {"status": "dropped", "reason": "optional"},
         "extra_seeds": {"status": "dropped", "reason": "gate passed"},
         "part2": {"status": "completed", "reason": ""}}))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "results")
    return root


def snapshot_rows(n: int, pods=None, settled=True) -> str:
    """`runpod_reservations.snapshot("E9", …)` rows: reserve, launch (pods) and settle."""
    rows = []
    for i in range(n):
        rid = f"r{i}"
        pid = (pods or {}).get(rid, f"pod{i}")
        rows += [{"id": rid, "exp": "E9", "kind": "reserve", "reservation_id": rid,
                  "pod_ids": []},
                 {"id": f"l{i}", "exp": "E9", "kind": "launch", "reservation_id": rid,
                  "pod_ids": [pid], "pods": [{"pod_id": pid}]}]
        if settled:
            rows.append({"id": f"s{i}", "exp": "E9", "kind": "settle", "reservation_id": rid,
                         "pod_ids": [pid]})
    return "".join(json.dumps(r) + "\n" for r in rows)


def _restore(root: Path):
    git(root, "checkout", "-q", "--", ".")
    git(root, "clean", "-qfd")


def _commit_then(world, edit, check):
    """Apply `edit`, commit, run `check`, then drop the commit."""
    try:
        edit()
        git(world, "add", "-A")
        git(world, "commit", "-qm", "edit")
        check()
    finally:
        git(world, "reset", "-q", "--hard", "HEAD~1")
        _restore(world)


def test_clean_tree_passes(world):
    assert PROV.run(world) == []
    assert PROV.main(["--root", str(world)]) == 0
    assert PROV.run(world, recompute=False, final=True) == []  # every analysis has tables


def test_edited_table_number_fails(world):
    rep = world / C.REPORT_LOG
    text = rep.read_text()
    rec = json.loads((world / RP.record_path("gate")).read_text())
    num = f"{rec['contrasts']['corr-orig']['delta']['point']:.4f}"
    assert num in text

    def check():
        probs = PROV.run(world, recompute=False)
        assert any("differs from the table rendered" in p for p in probs)
        assert any("number -0.9999 not in" in p for p in probs)
        assert PROV.main(["--root", str(world), "--no-recompute"]) == 1

    _commit_then(world, lambda: rep.write_text(text.replace(num, "-0.9999", 1)), check)


def test_modified_input_fails(world):
    p = next((world / C.JUDGED_DIR).glob("*__*.csv"))
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

    def check():
        probs = PROV.run(world, recompute=False)
        assert any("stale" in x for x in probs)
        assert any("cited gate record version" in x for x in probs)

    _commit_then(world, lambda: rp.write_text(
        rp.read_text().replace('"name": "gate"', '"name": "gate" ')), check)


def test_input_only_in_a_committed_manifest(world):
    """A gitignored-style input is fine when a committed sha manifest lists its hash."""
    p = next((world / C.JUDGED_DIR).glob("*__*.csv"))
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


# --- required evidence (R3 #11) ----------------------------------------------------------------


def test_missing_record_named_by_the_gate_log_fails(world):
    def check():
        probs = PROV.run(world, recompute=False)
        assert any("analysis gate recorded" in x and "missing" in x for x in probs)

    _commit_then(world, lambda: (world / RP.record_path("gate")).unlink(), check)


def test_empty_analysis_dir_with_recorded_phases_fails(world):
    def edit():
        for p in (world / C.ANALYSIS_DIR).glob("*.json"):
            p.unlink()

    def check():
        probs = PROV.run(world, recompute=False, reports=[])
        assert any("has no analysis records" in x for x in probs)
        assert PROV.main(["--root", str(world), "--no-recompute"]) == 1

    _commit_then(world, edit, check)


def test_launch_without_ledger_or_snapshot_fails(world):
    def edit():
        (world / PROV.LEDGER).unlink()
        (world / SNAPSHOT).unlink()

    def check():
        probs = PROV.run(world, recompute=False)
        assert any("ledger" in x and "missing" in x for x in probs)
        assert any("reservation snapshot" in x for x in probs)

    _commit_then(world, edit, check)


def test_uncommitted_gate_log_fails(world):
    try:
        with (world / C.GATE_LOG).open("a") as f:
            f.write(json.dumps({"kind": "gate", "gate": "pilot", "outcome": "pass"}) + "\n")
        probs = PROV.run(world, recompute=False)
        assert any("gate log" in x and "not committed" in x for x in probs)
    finally:
        _restore(world)


def test_unmarked_analysis_table_fails(world):
    rep = world / C.REPORT_LOG
    rec = json.loads((world / RP.record_path("gate")).read_text())
    table = RP.render_table("gate", "contrasts", rec)

    def check():
        probs = PROV.run(world, recompute=False)
        assert any("outside e9-table markers" in x for x in probs)

    _commit_then(world, lambda: rep.write_text(rep.read_text() + "\n" + table + "\n"), check)


def test_final_check_requires_every_analysis_in_a_marked_table(world):
    rep = world / C.REPORT_LOG
    text = rep.read_text()

    def edit():
        start = text.index("### gate")
        rep.write_text(text[:start])
        (world / C.ANALYSIS_DIR / "tables.md").write_text("no tables\n")

    def check():
        probs = PROV.run(world, recompute=False, final=True)
        assert any("no marked table of analysis gate" in x for x in probs)
        assert not any("analysis pilot" in x for x in probs)

    _commit_then(world, edit, check)


def test_final_check_needs_records_and_gate_log(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "README").write_text("x\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    assert PROV.run(tmp_path) == []  # nothing recorded yet: nothing required
    probs = PROV.run(tmp_path, final=True)
    assert any("no gate log" in x for x in probs)
    assert any("no analysis records" in x for x in probs)
    assert any("branch part2 has no terminal disposition" in x for x in probs)
    # a documented stop before any analysis needs no analysis records
    stop = tmp_path / PROV.STOPPED_FILE
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(json.dumps({"rule": "selection or rewriting unreliable (§6)"}))
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "stop")
    assert not any("no analysis records" in x for x in PROV.run(tmp_path, final=True))


def test_snapshot_must_cover_the_launches(world):
    snap = world / SNAPSHOT

    def check_count():
        assert any("launched reservations for" in x for x in PROV.run(world, recompute=False))

    _commit_then(world, lambda: snap.write_text(snapshot_rows(2)), check_count)

    def edit_other():
        snap.write_text(snap.read_text() + json.dumps({"exp": "E3", "kind": "reserve",
                                                       "reservation_id": "x"}) + "\n")

    def check_other():
        assert any("another experiment" in x for x in PROV.run(world, recompute=False))

    _commit_then(world, edit_other, check_other)

    def edit_pods():
        (world / PROV.PODS_LOG).write_text(json.dumps({"pod_id": "podX",
                                                       "reservation_id": "r0"}) + "\n")

    def check_pods():
        assert any("lacks reservations" in x and "podX" in x
                   for x in PROV.run(world, recompute=False))

    _commit_then(world, edit_pods, check_pods)


def test_final_check_open_reservation_and_branch_disposition(world):
    def edit():
        n = sum(x.get("kind") == "launch" for x in orchestrator_log(world))
        (world / SNAPSHOT).write_text(snapshot_rows(n, settled=False))
        (world / PROV.BRANCHES).write_text(json.dumps(
            {"gradient_screen": {"status": "dropped"}, "extra_seeds": {"status": "running"},
             "part2": {"status": "completed"}}))

    def check():
        probs = PROV.run(world, recompute=False, final=True)
        assert any("not settled or released" in x for x in probs)
        assert any("branch extra_seeds has no terminal disposition" in x for x in probs)

    _commit_then(world, edit, check)


def test_renamed_numerical_table_without_marker_fails(world):
    rep = world / C.REPORT_LOG
    extra = "\n## Part 1 readout\n\n| Contrast (renamed) | Delta |\n|---|---|\n| c-o | -0.1700 |\n"

    def check():
        probs = PROV.run(world, recompute=False)
        assert any("numerical table under 'Part 1 readout'" in x for x in probs)

    _commit_then(world, lambda: rep.write_text(rep.read_text() + extra), check)
