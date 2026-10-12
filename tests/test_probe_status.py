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

HERDR = {
    "id": "cli:agent:list",
    "result": {
        "agents": [
            {
                "agent": "claude",
                "agent_status": "working",
                "name": "p01",
                "pane_id": "w8:pR",
                "cwd": "/x",
                "terminal_title_stripped": "P01",
            },
            {"agent": "claude", "agent_status": "idle", "pane_id": "w4:p1", "cwd": "/y"},
        ]
    },
}


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
    monkeypatch.setattr(ps, "RUN", fake_run(json.dumps(HERDR)))
    rows = by_id(ps.collect(repo))
    assert set(rows) == {"P01", "P02"}
    p1 = rows["P01"]
    assert p1.status == "Running"  # the worktree's status.json wins over main's
    assert (p1.agent, p1.agent_status, p1.pane_id) == ("p01", "working", "w8:pR")
    assert p1.branch == "probe/P01-a"
    assert p1.worktree == ".claude/worktrees/p01-a"
    assert p1.spend_usd == pytest.approx(3.75)  # shared row once + the branch's new charge
    assert p1.cap_usd == 75
    assert (p1.pod_phase, p1.pod_id) == ("up", "abc123")
    assert p1.log_last_line == "smoke passed, bulk running"
    assert p1.log_mtime_utc.endswith("Z")
    assert p1.question == "Does A hold?"


def test_probe_without_status_json(repo, monkeypatch):
    monkeypatch.setattr(ps, "RUN", fake_run(json.dumps(HERDR)))
    p2 = by_id(ps.collect(repo))["P02"]
    assert (p2.status, p2.slug, p2.agent, p2.worktree, p2.branch) == ("?", "b", None, None, None)
    assert (p2.spend_usd, p2.cap_usd, p2.pod_phase, p2.log_last_line) == (0.0, None, None, None)
    row = next(ln for ln in ps.table(ps.collect(repo)).splitlines() if ln.startswith("P02"))
    assert row.split()[:5] == ["P02", "?", "-", "-", "-"]


def test_cap_falls_back_to_probe_toml(repo, monkeypatch):
    put(repo / "probes/P02-b/probe.toml", 'id = "P02"\nhard_cap_usd = 40\n')
    rows = by_id(ps.collect(repo, use_herdr=False))
    assert rows["P02"].cap_usd == 40


def test_owner_matches_agent_when_name_differs(repo, monkeypatch):
    st = repo / ".claude/worktrees/p01-a/probes/P01-a/status.json"
    put(st, json.dumps({**json.loads(st.read_text()), "owner": "codex-a"}))
    other = {"result": {"agents": [{"name": "codex-a", "agent_status": "idle", "pane_id": "w8:q"}]}}
    monkeypatch.setattr(ps, "RUN", fake_run(json.dumps(other)))
    assert by_id(ps.collect(repo))["P01"].agent == "codex-a"


@pytest.mark.parametrize(
    "run",
    [
        fake_run(exc=FileNotFoundError("herdr")),
        fake_run(exc=subprocess.TimeoutExpired("herdr", 5)),
        fake_run("not json at all"),
        fake_run(json.dumps({"result": {"agents": "?"}})),
        fake_run("", rc=1),
    ],
)
def test_herdr_unavailable_never_fails(repo, monkeypatch, capsys, run):
    monkeypatch.setattr(ps, "RUN", run)
    rows = by_id(ps.collect(repo))
    assert rows["P01"].agent is None and rows["P01"].status == "Running"
    err = capsys.readouterr().err
    assert err.count("herdr") == 1 and "'-'" in err


def test_no_herdr_flag_skips_the_call(repo, monkeypatch):
    run = fake_run(json.dumps(HERDR))
    monkeypatch.setattr(ps, "RUN", run)
    ps.collect(repo, use_herdr=False)
    assert run.calls == []


def test_missing_ledger_is_skipped_with_note(repo, monkeypatch, capsys):
    (repo / ".claude/worktrees/p01-a/logs/costs.jsonl").unlink()
    rows = by_id(ps.collect(repo, use_herdr=False))
    assert rows["P01"].spend_usd == pytest.approx(1.25)
    assert "ledger skipped" in capsys.readouterr().err


def test_json_and_table_output_and_read_only(repo, monkeypatch, capsys):
    monkeypatch.setattr(ps, "RUN", fake_run(json.dumps(HERDR)))
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
        "pane_id",
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
        "p01/working",
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
        pane_id=None,
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
