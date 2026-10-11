"""Repo-local ssh known hosts (Jacob, 2026-10-11): pod ssh/rsync calls use the checkout's
`.ssh_known_hosts`, never ~/.ssh/known_hosts, so sandboxed agents need no write access to ~/.ssh."""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

from emx import runpod as rp

ROOT = Path(__file__).resolve().parents[1]
SHELL = ["probe_watchdog.sh", "pod_watchdog.sh", "pod_push.sh", "pod_pull_results.sh",
         "pod_part.sh", "pod_hunt.sh"]


def test_known_hosts_file_is_in_the_checkout_the_code_runs_from():
    assert rp.known_hosts_file() == ROOT / ".ssh_known_hosts"
    assert rp.ssh_known_hosts_opts() == ["-o", f"UserKnownHostsFile={ROOT / '.ssh_known_hosts'}"]
    assert rp.known_hosts_file(Path("/x")) == Path("/x/.ssh_known_hosts")


def test_known_hosts_file_is_gitignored():
    r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", ".ssh_known_hosts"],
                       check=False)
    assert r.returncode == 0


def _key(tmp_path: Path, name: str) -> str:
    k = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True)
    return " ".join((k.with_suffix(".pub")).read_text().split()[:2])


def test_forget_host_key_removes_only_that_entry_from_the_given_file(tmp_path):
    f = tmp_path / ".ssh_known_hosts"
    f.write_text(f"[1.2.3.4]:22022 {_key(tmp_path, 'a')}\n[5.6.7.8]:22 {_key(tmp_path, 'b')}\n")
    rp.forget_host_key("1.2.3.4", 22022, known_hosts=f)
    text = f.read_text()
    assert "[1.2.3.4]:22022" not in text and "[5.6.7.8]:22" in text


def test_forget_host_key_on_a_missing_file_does_nothing(tmp_path):
    f = tmp_path / ".ssh_known_hosts"
    rp.forget_host_key("1.2.3.4", 22022, known_hosts=f)
    assert not f.exists()


def test_forget_host_key_default_cleans_repo_file_then_legacy_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    (tmp_path / ".ssh_known_hosts").write_text("")
    calls = []
    monkeypatch.setattr(rp.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    rp.forget_host_key("h", 7)
    assert calls == [
        ["ssh-keygen", "-R", "[h]:7", "-f", str(tmp_path / ".ssh_known_hosts")],
        ["ssh-keygen", "-R", "[h]:7"],  # best effort: older experiments' scripts use ~/.ssh
    ]


def test_probe_pod_ssh_options_use_the_repo_file():
    spec = importlib.util.spec_from_file_location("probe_pod", ROOT / "scripts" / "probe_pod.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    opts = mod.SSH_OPTS
    assert f"UserKnownHostsFile={ROOT / '.ssh_known_hosts'}" in opts
    assert "StrictHostKeyChecking=accept-new" in opts


@pytest.mark.parametrize("name", SHELL)
def test_shell_scripts_pin_known_hosts_on_every_accept_new(name):
    text = (ROOT / "scripts" / name).read_text()
    lines = text.splitlines()
    cd = next(i for i, x in enumerate(lines) if x.startswith('cd "$(dirname "$0")/.."'))
    assert lines[cd + 1].startswith('KNOWN_HOSTS="$PWD/.ssh_known_hosts"')
    n_accept = len(re.findall(r"StrictHostKeyChecking=accept-new", text))
    n_pinned = len(re.findall(
        r'StrictHostKeyChecking=accept-new -o "?UserKnownHostsFile=\$KNOWN_HOSTS', text))
    assert n_accept >= 1 and n_accept == n_pinned
