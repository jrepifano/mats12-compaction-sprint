"""scripts/merge_ledger.py: per-worktree ledgers combine without loss or double counting."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from emx import spend

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


ml = load("merge_ledger")


def entry(utc: str, exp: str, usd: float, note: str = "", id: str | None = None) -> dict:
    e = {
        "utc": utc,
        "exp": exp,
        "kind": "gpu",
        "resource": "H100",
        "units": 1,
        "usd": usd,
        "note": note,
    }
    if id:
        e["id"] = id
    return e


def write(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


A = entry("2026-10-01T00:00:00Z", "E1", 10.0, "legacy, shared before the branch")
B = entry("2026-10-04T01:00:00Z", "E1b", 3.5, "on the branch", id="b" * 32)
C = entry("2026-10-04T02:00:00Z", "I9", 2.25, "on main", id="c" * 32)


def test_record_stamps_unique_ids(tmp_path):
    L = tmp_path / "costs.jsonl"
    e1 = spend.record("judge", "gpt-6-luna", 10, 0.01, note="same", ledger=L)
    e2 = spend.record("judge", "gpt-6-luna", 10, 0.01, note="same", ledger=L)
    assert e1["id"] != e2["id"]
    assert len(ml.union([ml.read_ledger(L)])) == 2


def test_merge_adds_only_missing_and_is_idempotent(tmp_path):
    main, branch = tmp_path / "main.jsonl", tmp_path / "branch.jsonl"
    write(main, [A, C])
    write(branch, [A, B])
    assert ml.append_missing(main, ml.read_ledger(branch), write=True) == [B]
    assert ml.read_ledger(main) == [A, C, B]
    assert ml.append_missing(main, ml.read_ledger(branch), write=True) == []
    assert ml.read_ledger(main) == [A, C, B]


def test_dry_run_writes_nothing(tmp_path):
    main, branch = tmp_path / "main.jsonl", tmp_path / "branch.jsonl"
    write(main, [A])
    write(branch, [A, B])
    before = main.read_bytes()
    assert ml.append_missing(main, ml.read_ledger(branch), write=False) == [B]
    assert main.read_bytes() == before


def test_merge_repairs_missing_trailing_newline(tmp_path):
    main = tmp_path / "main.jsonl"
    main.write_text(json.dumps(A), encoding="utf-8")
    ml.append_missing(main, [B], write=True)
    assert ml.read_ledger(main) == [A, B]


def test_identical_legacy_charges_both_survive():
    """Two genuine id-less charges with identical fields are occurrences 1 and 2, not one entry."""
    branch = [A, A]  # e.g. two identical calls recorded in the same second
    assert ml.missing_from([A], branch) == [A]  # main has only the first
    assert ml.totals(ml.union([[A], branch])) == {"E1": 20.0}


def test_key_ignores_field_order_for_legacy_rows():
    reordered = dict(reversed(list(A.items())))
    assert ml.keys([reordered]) == ml.keys([A])


def test_union_counts_shared_entries_once():
    combined = ml.union([[A, C], [A, B]])
    assert combined == [A, C, B]
    assert ml.totals(combined) == {"E1": 10.0, "E1b": 3.5, "I9": 2.25}


def test_missing_or_malformed_source_is_refused(tmp_path):
    with pytest.raises(ml.LedgerError, match="no ledger"):
        ml.read_ledger(tmp_path / "typo.jsonl")
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps(A) + "\n{not json\n", encoding="utf-8")
    with pytest.raises(ml.LedgerError, match="malformed"):
        ml.read_ledger(bad)
    assert ml.main(["--repo", str(ROOT), "merge", "--from", str(tmp_path / "typo.jsonl")]) == 2


def test_check_and_fix(tmp_path, capsys):
    p = tmp_path / "dup.jsonl"
    write(p, [A, B, C, B])
    assert ml.cmd_check(p, fix=False) == 1
    assert ml.cmd_check(p, fix=True) == 0
    assert ml.read_ledger(p) == [A, B, C]
    write(p, [A, B, A])  # a repeated id-less row is ambiguous: reported, never removed
    assert ml.cmd_check(p, fix=True) == 1
    assert ml.read_ledger(p) == [A, B, A]
    assert "ambiguous" in capsys.readouterr().out


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_worktree(tmp_path):
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    shutil.copy(ROOT / ".gitattributes", repo / ".gitattributes")
    write(repo / "logs" / "costs.jsonl", [A])
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    _git("worktree", "add", "-q", "-b", "exp/run", str(wt), cwd=repo)
    return repo, wt


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_simple_divergence_merges_cleanly(repo_with_worktree):
    repo, wt = repo_with_worktree
    led = repo / "logs" / "costs.jsonl"
    write(wt / "logs" / "costs.jsonl", [A, B])
    _git("commit", "-q", "-am", "branch spend", cwd=wt)
    write(led, [A, C])
    _git("commit", "-q", "-am", "main spend", cwd=repo)

    trees = ml.worktrees(repo)
    assert [t.path.resolve() for t in trees] == [repo.resolve(), wt.resolve()]
    assert trees[0].branch == "refs/heads/main"
    assert ml.main_ledger(repo).resolve() == led.resolve()
    assert ml.missing_from(ml.read_ledger(led), ml.read_ref("exp/run", repo)) == [B]

    _git("merge", "-q", "--no-edit", "exp/run", cwd=repo)  # merge=union: no conflict
    assert ml.cmd_check(led, fix=True) == 0
    assert ml.totals(ml.read_ledger(led)) == {"E1": 10.0, "E1b": 3.5, "I9": 2.25}


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_entry_reaching_main_twice_is_counted_once(repo_with_worktree):
    """Reviewer's sequence: the branch merges main mid-experiment, main imports a branch entry with
    `merge`, then main merges the branch. Union may repeat B; `check --fix` removes the copy."""
    repo, wt = repo_with_worktree
    led, wled = repo / "logs" / "costs.jsonl", wt / "logs" / "costs.jsonl"
    write(led, [A, C])
    _git("commit", "-q", "-am", "main spend", cwd=repo)
    write(wled, [A, B])
    _git("commit", "-q", "-am", "branch spend", cwd=wt)
    _git("merge", "-q", "--no-edit", "main", cwd=wt)  # branch picks up C mid-experiment
    ml.append_missing(led, ml.read_ledger(wled), write=True)  # main imports B early
    _git("commit", "-q", "-am", "import branch spend", cwd=repo)
    _git("merge", "-q", "--no-edit", "exp/run", cwd=repo)

    ml.cmd_check(led, fix=True)
    assert ml.cmd_check(led, fix=False) == 0
    assert ml.totals(ml.read_ledger(led)) == {"E1": 10.0, "E1b": 3.5, "I9": 2.25}


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_primary_checkout_off_main_is_refused(repo_with_worktree):
    repo, _ = repo_with_worktree
    _git("checkout", "-q", "-b", "other", cwd=repo)
    with pytest.raises(ml.LedgerError, match="not main"):
        ml.main_ledger(repo)


def test_source_with_a_repeated_id_imports_it_once(tmp_path):
    main = tmp_path / "main.jsonl"
    write(main, [A])
    assert ml.append_missing(main, [B, B], write=True) == [B]
    assert ml.read_ledger(main) == [A, B]


def test_fix_never_drops_conflicting_rows_for_one_id(tmp_path, capsys):
    p = tmp_path / "conflict.jsonl"
    corrected = dict(B, usd=4.0)
    write(p, [A, B, corrected])
    assert ml.cmd_check(p, fix=True) == 1
    assert ml.read_ledger(p) == [A, B, corrected]
    assert "differ" in capsys.readouterr().out


def test_post_branch_legacy_rows_are_refused_until_stamped(tmp_path):
    """Pre-id code on a branch wrote X after the split; main can't tell X from its own rows."""
    main, branch = tmp_path / "main.jsonl", tmp_path / "branch.jsonl"
    X = entry("2026-10-05T00:00:00Z", "X3", 1.0, "pre-id code on the branch")
    write(main, [A, C])
    write(branch, [A, X])
    with pytest.raises(ml.LedgerError, match="stamp-ids"):
        ml.append_missing(main, ml.read_ledger(branch), write=True)
    assert ml.read_ledger(main) == [A, C]  # nothing written
    assert ml.stamp_ids(branch, ml.read_ledger(main), write=False) == [X]
    stamped = ml.stamp_ids(branch, ml.read_ledger(main), write=True)
    assert len(stamped) == 1 and stamped[0]["id"]
    rows = ml.read_ledger(branch)
    assert rows[0] == A  # the shared row is untouched, so it still matches main's copy
    assert ml.append_missing(main, rows, write=True) == [rows[1]]
    assert ml.totals(ml.read_ledger(main)) == {"E1": 10.0, "I9": 2.25, "X3": 1.0}


def test_s4world_ledger_defaults_to_its_own_checkout(monkeypatch):
    from s4world import budget

    monkeypatch.delenv("S4_LEDGER", raising=False)
    monkeypatch.delenv("S4_RESERVATIONS", raising=False)
    assert budget.ledger_path() == ROOT / "logs" / "costs.jsonl"
    assert budget.reservations_path() == ROOT / "logs" / "S4.reservations.json"


def test_merge_refuses_conflicting_rows_for_one_id(tmp_path):
    main = tmp_path / "main.jsonl"
    write(main, [A])
    with pytest.raises(ml.LedgerError, match="differ"):
        ml.append_missing(main, [B, dict(B, usd=9.0)], write=True)
    write(main, [A, B])
    with pytest.raises(ml.LedgerError, match="differ"):
        ml.append_missing(main, [dict(B, usd=9.0)], write=True)
    assert ml.read_ledger(main) == [A, B]


def test_rewrites_keep_untouched_lines_verbatim(tmp_path):
    p = tmp_path / "led.jsonl"
    odd = '{"utc":"2026-10-01T00:00:00Z","exp":"E1","kind":"gpu","resource":"H100","units":1,"usd":10.0,"note":"compact"}'
    X = entry("2026-10-05T00:00:00Z", "X3", 1.0, "post-branch")
    p.write_text(odd + "\n" + json.dumps(B) + "\n" + json.dumps(B) + "\n" + json.dumps(X) + "\n")
    ml.drop_exact_id_repeats(p)
    assert p.read_text().splitlines()[0] == odd
    ml.stamp_ids(p, [json.loads(odd)], write=True)
    lines = p.read_text().splitlines()
    assert lines[0] == odd and lines[1] == json.dumps(B) and len(lines) == 3
    assert json.loads(lines[2])["id"]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_report_flags_unstamped_post_branch_rows(repo_with_worktree, capsys):
    repo, wt = repo_with_worktree
    X = entry("2026-10-05T00:00:00Z", "X3", 1.0, "pre-id code on the branch")
    write(wt / "logs" / "costs.jsonl", [A, X])
    assert ml.cmd_report(repo, as_json=False) == 1
    assert "UNRELIABLE" in capsys.readouterr().out
    ml.stamp_ids(wt / "logs" / "costs.jsonl", ml.read_ledger(repo / "logs" / "costs.jsonl"), True)
    assert ml.cmd_report(repo, as_json=False) == 0
    assert "UNRELIABLE" not in capsys.readouterr().out
