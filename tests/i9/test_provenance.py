"""emx.i9.provenance.git_state: env vars win; tracked edits are dirty; the spend ledger is not."""

import subprocess

from emx.i9 import provenance


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


def test_ledger_append_is_not_dirty_but_code_is(tmp_path, monkeypatch):
    monkeypatch.delenv("EMX_GIT_COMMIT", raising=False)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "costs.jsonl").write_text("{}\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    assert provenance.git_state(tmp_path)[1] is False
    (tmp_path / "logs" / "costs.jsonl").write_text("{}\n{}\n")  # a paid step's record
    assert provenance.git_state(tmp_path)[1] is False
    (tmp_path / "untracked.txt").write_text("output\n")  # untracked outputs never count
    assert provenance.git_state(tmp_path)[1] is False
    (tmp_path / "a.py").write_text("x = 2\n")
    assert provenance.git_state(tmp_path)[1] is True


def test_env_vars_win(monkeypatch):
    monkeypatch.setenv("EMX_GIT_COMMIT", "abc")
    monkeypatch.setenv("EMX_GIT_DIRTY", "0")
    assert provenance.git_state() == ("abc", False)
