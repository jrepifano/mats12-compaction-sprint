"""scripts/probe_status.py: one read-only row per probe across main and every worktree."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


ps = load("probe_status")

SHARED = {"utc": "2026-10-01T00:00:00Z", "exp": "P01", "kind": "api", "usd": 1.25}  # id-less
MAIN_ONLY = {"utc": "2026-10-02T00:00:00Z", "exp": "E1", "kind": "gpu", "usd": 4.0, "id": "e" * 32}
BRANCH_NEW = {
    "utc": "2026-10-11T00:00:00Z",
    "exp": "P01",
    "kind": "gpu",
    "usd": 2.5,
    "id": "a" * 32,
}

WT_P01 = "/repo/.claude/worktrees/p01-a"  # replaced with the fixture's real path in orca_fake()

# Shapes copied from `orca terminal list --json` and `orca worktree ps --json` (Orca 1.4.230,
# 2026-10-20), trimmed to the fields probe_status reads.
TERMINALS = {
    "id": "x",
    "ok": True,
    "result": {
        "terminals": [
            {
                "handle": "term_main",
                "worktreePath": "/repo",
                "tabId": "tab0",
                "leafId": "leaf0",
                "title": "Terminal 1",
                "connected": True,
                "lastOutputAt": 5,
                "agentIdentity": "claude",
            },
            {
                "handle": "term_p01_shell",
                "worktreePath": WT_P01,
                "tabId": "tab1",
                "leafId": "leafS",
                "title": "zsh",
                "connected": True,
                "lastOutputAt": 9,
            },
            {
                "handle": "term_p01",
                "worktreePath": WT_P01,
                "tabId": "tab1",
                "leafId": "leaf1",
                "title": "P01 a",
                "connected": True,
                "lastOutputAt": 7,
                "agentIdentity": "claude",
            },
        ]
    },
}
PS = {
    "id": "y",
    "ok": True,
    "result": {
        "worktrees": [
            {
                "path": WT_P01,
                "agents": [{"paneKey": "tab1:leaf1", "state": "working", "agentType": "claude"}],
            }
        ]
    },
}


def orca_fake(repo: Path, terminals: dict | None = None, ps_out: dict | None = None, **kw):
    """A fake RUN answering `terminal list` and `worktree ps` with the fixture's real paths."""
    wt = str(repo / ".claude/worktrees/p01-a")
    outs = {
        "terminal": json.dumps(terminals or TERMINALS)
        .replace(WT_P01, wt)
        .replace('"/repo"', json.dumps(str(repo))),
        "worktree": json.dumps(ps_out or PS).replace(WT_P01, wt),
    }
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=outs[cmd[1]], stderr="")

    run.calls = calls
    return run


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(cwd),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


@pytest.fixture
def repo(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    status = {
        "id": "P01",
        "slug": "a",
        "question": "Does A hold?",
        "cap_usd": 75,
        "status": "Planned",
        "plan": "plan.md",
        "pr": None,
        "spend_usd": None,
        "owner": "p01",
        "updated_at": "2026-10-11T00:00:00Z",
    }
    put(main / "probes/P01-a/status.json", json.dumps(status))
    put(main / "probes/P02-b/plan.md", "# P02\n")
    put(main / "logs/costs.jsonl", jsonl([SHARED, MAIN_ONLY]))
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "init")
    wt = main / ".claude/worktrees/p01-a"
    git(main, "worktree", "add", "-q", "-b", "probe/P01-a", str(wt))
    put(wt / "logs/costs.jsonl", jsonl([SHARED, BRANCH_NEW]))
    put(wt / "probes/P01-a/status.json", json.dumps({**status, "status": "Running"}))
    put(
        wt / "probes/P01-a/log.md", "# Log\n\n- **10:00** started\n- smoke passed, bulk running\n\n"
    )
    put(
        wt / "probes/P01-a/logs/runner_state.json",
        json.dumps({"phase": "up", "pod_id": "abc123", "exp": "P01"}),
    )
    return main


def fake_run(stdout: str = "", rc: int = 0, exc: Exception | None = None):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")

    run.calls = calls
    return run


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*") if p.is_file()
    }


def by_id(rows):
    return {r.id: r for r in rows}


def test_rows_from_main_and_worktree(repo, monkeypatch):
    run = orca_fake(repo)
    monkeypatch.setattr(ps, "RUN", run)
    rows = by_id(ps.collect(repo))
    assert set(rows) == {"P01", "P02"}
    assert [c[1:3] for c in run.calls] == [["terminal", "list"], ["worktree", "ps"]]
    assert all(c[-1] == "--json" for c in run.calls)
    p1 = rows["P01"]
    assert p1.status == "Running"  # the worktree's status.json wins over main's
    # The agent terminal in the probe's worktree wins over a newer plain shell there.
    assert (p1.agent, p1.agent_status, p1.terminal) == ("claude", "working", "term_p01")
    assert p1.terminal_title == "P01 a"
    assert p1.branch == "probe/P01-a"
    assert p1.worktree == ".claude/worktrees/p01-a"
    assert p1.spend_usd == pytest.approx(3.75)  # shared row once + the branch's new charge
    assert p1.cap_usd == 75
    assert (p1.pod_phase, p1.pod_id) == ("up", "abc123")
    assert p1.log_last_line == "smoke passed, bulk running"
    assert p1.log_mtime_utc.endswith("Z")
    assert p1.question == "Does A hold?"


def test_probe_without_status_json(repo, monkeypatch):
    monkeypatch.setattr(ps, "RUN", orca_fake(repo))
    p2 = by_id(ps.collect(repo))["P02"]
    assert (p2.status, p2.slug, p2.agent, p2.worktree, p2.branch) == ("?", "b", None, None, None)
    assert (p2.spend_usd, p2.cap_usd, p2.pod_phase, p2.log_last_line) == (0.0, None, None, None)
    row = next(ln for ln in ps.table(ps.collect(repo)).splitlines() if ln.startswith("P02"))
    assert row.split()[:5] == ["P02", "?", "-", "-", "-"]


def test_cap_falls_back_to_probe_toml(repo, monkeypatch):
    put(repo / "probes/P02-b/probe.toml", 'id = "P02"\nhard_cap_usd = 40\n')
    rows = by_id(ps.collect(repo, use_orca=False))
    assert rows["P02"].cap_usd == 40


def test_title_fallback_and_no_hook_state(repo, monkeypatch):
    """No terminal in the worktree: a "P01 " title anywhere matches; no hook state reads "live"."""
    terms = {
        "ok": True,
        "result": {
            "terminals": [
                {
                    "handle": "term_x",
                    "worktreePath": "/elsewhere",
                    "tabId": "t",
                    "leafId": "l",
                    "title": "P01 a",
                    "connected": True,
                    "agentIdentity": "codex",
                },
                {"handle": "term_y", "worktreePath": "/elsewhere", "title": "P011 other"},
            ]
        },
    }
    monkeypatch.setattr(ps, "RUN", orca_fake(repo, terminals=terms, ps_out={"result": {}}))
    p1 = by_id(ps.collect(repo))["P01"]
    assert (p1.agent, p1.agent_status, p1.terminal) == ("codex", "live", "term_x")


def test_plain_shell_terminal_shows_as_shell(repo, monkeypatch):
    terms = json.loads(json.dumps(TERMINALS))
    terms["result"]["terminals"] = terms["result"]["terminals"][:2]  # main + p01's plain shell
    monkeypatch.setattr(ps, "RUN", orca_fake(repo, terminals=terms))
    p1 = by_id(ps.collect(repo))["P01"]
    assert (p1.agent, p1.agent_status, p1.terminal) == ("shell", "live", "term_p01_shell")


def test_ps_failure_keeps_terminal_columns(repo, monkeypatch, capsys):
    good = orca_fake(repo)

    def run(cmd, **kw):
        if cmd[1] == "worktree":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return good(cmd, **kw)

    monkeypatch.setattr(ps, "RUN", run)
    p1 = by_id(ps.collect(repo))["P01"]
    assert (p1.agent, p1.agent_status, p1.terminal) == ("claude", "live", "term_p01")
    assert "agent states show '-'" in capsys.readouterr().err


@pytest.mark.parametrize(
    "run",
    [
        fake_run(exc=FileNotFoundError("orca")),
        fake_run(exc=subprocess.TimeoutExpired("orca", 5)),
        fake_run(exc=PermissionError("orca")),
        fake_run("not json at all"),
        fake_run(json.dumps([1, 2])),
        fake_run(json.dumps({"ok": False, "error": {"code": "runtime_unreachable"}})),
        fake_run(json.dumps({"result": {"terminals": "?"}})),
        fake_run("", rc=1),
        fake_run(exc=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")),
    ],
)
def test_orca_unavailable_never_fails(repo, monkeypatch, capsys, run):
    monkeypatch.setattr(ps, "RUN", run)
    rows = by_id(ps.collect(repo))
    assert rows["P01"].agent is None and rows["P01"].status == "Running"
    assert rows["P01"].terminal is None
    err = capsys.readouterr().err
    assert err.count("probe_status:") == 1 and "orca" in err and "'-'" in err
    assert len(run.calls) == 1  # no `worktree ps` once `terminal list` failed


def test_malformed_terminal_fields_never_crash(repo, monkeypatch):
    """Wrong-typed fields are dropped, not compared: astra r1 reproduced TypeErrors here."""
    terms = {
        "ok": True,
        "result": {
            "terminals": [
                {"handle": 42, "title": "P01 a"},  # non-string handle: dropped
                {
                    "handle": "term_late",
                    "worktreePath": 123,
                    "title": "P01 a",
                    "lastOutputAt": "late",
                    "agentIdentity": 7,
                    "tabId": "t",
                    "leafId": "l",
                },
                {
                    "handle": "term_num",
                    "worktreePath": ["x"],
                    "title": "P01 a",
                    "lastOutputAt": 4,
                    "connected": "yes",
                },
            ]
        },
    }
    ps_out = {
        "result": {
            "worktrees": [
                {"agents": [{"paneKey": 5, "state": "working"}, {"paneKey": "t:l", "state": 9}]}
            ]
        }
    }
    monkeypatch.setattr(ps, "RUN", orca_fake(repo, terminals=terms, ps_out=ps_out))
    p1 = by_id(ps.collect(repo))["P01"]
    assert p1.terminal == "term_num"  # "late" ranks as 0, below 4
    assert (p1.agent, p1.agent_status) == ("shell", "disconnected")  # "yes" is not True


def test_invalid_utf8_from_real_subprocess(repo, monkeypatch, tmp_path, capsys):
    fake = tmp_path / "orca"
    fake.write_text("#!/bin/sh\nprintf '\\377\\376{'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("ORCA_BIN", str(fake))
    monkeypatch.setattr(ps, "RUN", subprocess.run)
    rows = by_id(ps.collect(repo))
    assert rows["P01"].terminal is None
    assert "unexpected output" in capsys.readouterr().err


def test_no_orca_flag_skips_the_call(repo, monkeypatch):
    run = orca_fake(repo)
    monkeypatch.setattr(ps, "RUN", run)
    ps.collect(repo, use_orca=False)
    assert run.calls == []
    assert ps.main(["--repo", str(repo), "--no-orca", "--json"]) == 0
    assert run.calls == []


def test_orca_bin_resolution(monkeypatch):
    monkeypatch.setenv("ORCA_BIN", "/x/orca")
    assert ps.orca_bin() == "/x/orca"
    monkeypatch.delenv("ORCA_BIN")
    monkeypatch.setattr(ps.shutil, "which", lambda name: None)
    assert ps.orca_bin() == ps.ORCA_APP_BIN


def test_missing_ledger_is_skipped_with_note(repo, monkeypatch, capsys):
    (repo / ".claude/worktrees/p01-a/logs/costs.jsonl").unlink()
    rows = by_id(ps.collect(repo, use_orca=False))
    assert rows["P01"].spend_usd == pytest.approx(1.25)
    assert "ledger skipped" in capsys.readouterr().err


def test_json_and_table_output_and_read_only(repo, monkeypatch, capsys):
    monkeypatch.setattr(ps, "RUN", orca_fake(repo))
    before = snapshot(repo)
    assert ps.main(["--repo", str(repo), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["id"] for d in data] == ["P01", "P02"]
    assert data[0]["agent_status"] == "working" and data[0]["last_go_outcome"] is None
    assert set(data[0]) == {
        "id",
        "slug",
        "status",
        "question",
        "agent",
        "agent_status",
        "terminal",
        "terminal_title",
        "worktree",
        "branch",
        "log_last_line",
        "log_mtime_utc",
        "spend_usd",
        "cap_usd",
        "pod_phase",
        "pod_id",
        "last_go_outcome",
    }
    monkeypatch.setenv("COLUMNS", "160")
    assert ps.main(["--repo", str(repo)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("ID") and "LAST LOG" in out[0]
    p1 = out[1]
    for s in (
        "Running",
        "claude/working",
        "probe/P01-a",
        "$3.75/$75",
        "up pod=abc123",
        "smoke passed",
    ):
        assert s in p1
    assert max(len(ln) for ln in out) <= 160
    assert snapshot(repo) == before


def test_pod_cell_formats():
    r = ps.Row(
        id="P01",
        slug="a",
        status="Running",
        question=None,
        agent=None,
        agent_status=None,
        terminal=None,
        terminal_title=None,
        worktree=None,
        branch=None,
        log_last_line=None,
        log_mtime_utc=None,
        spend_usd=0.0,
        cap_usd=None,
        pod_phase="down",
        pod_id="abc",
        last_go_outcome="complete",
    )
    assert ps.pod_cell(r) == "down/complete"
