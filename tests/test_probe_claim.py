"""scripts/probe_claim.py: ids claimed and files published by an atomic push, with real git.

Each test builds a temporary bare "origin" and two clones (A and B); nothing touches the network
or this repository.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from emx import probes as P

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pc = load("probe_claim")


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return r.stdout


def clone(origin: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True)
    git(dest, "config", "user.email", "t@example.com")
    git(dest, "config", "user.name", "t")
    return dest


@pytest.fixture
def repos(tmp_path):
    """origin (bare, main seeded with P01, P02 and a hand-edited row for P03) and clones A, B."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    git(seed, "config", "user.email", "t@example.com")
    git(seed, "config", "user.name", "t")
    probes = seed / "probes"
    s1 = P.new_status("P01", "first", "One?", 75, "p01")
    s2 = dict(P.new_status("P02", "second", "Two?", 40, "p02"), status="Running")
    for s in (s1, s2):
        (probes / P.dir_name(s)).mkdir(parents=True)
        P.write_status(probes / P.dir_name(s) / "status.json", s)
        (probes / P.dir_name(s) / "plan.md").write_text("plan\n")
    # P03 predates status.json: a plan dir and a row only.
    (probes / "P03-legacy").mkdir()
    (probes / "P03-legacy" / "plan.md").write_text("plan\n")
    legacy = P.status_from_row(
        {
            "id": "P03",
            "question": "Three?",
            "cap_usd": 5,
            "status": "Running",
            "plan": "P03-legacy/plan.md",
            "pr": None,
            "spend_usd": None,
        }
    )
    (probes / "INDEX.md").write_text(P.render_index([s1, s2, legacy]))
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", str(origin), "main")
    return origin, clone(origin, tmp_path / "A"), clone(origin, tmp_path / "B")


def claim(repo: Path, slug: str, *extra: str) -> int:
    return pc.main(
        [
            "claim",
            "--repo",
            str(repo),
            "--slug",
            slug,
            "--question",
            f"{slug}?",
            "--cap",
            "75",
            "--owner",
            "agent",
            *extra,
        ]
    )


def origin_file(origin: Path, path: str) -> str:
    return git(origin, "show", f"main:{path}")


def claimed_id(out: str) -> str:
    """The id on the last CLAIMED line (a racing claim may print one first)."""
    line = [x for x in out.splitlines() if x.startswith("CLAIMED ")][-1]
    return line.split()[1].removeprefix("id=")


def test_claim_lowest_free_id_lands_on_origin_and_leaves_checkout_alone(repos, capsys):
    origin, a, _ = repos
    head = git(a, "rev-parse", "HEAD")
    index_bytes = (a / ".git" / "index").read_bytes()
    assert claim(a, "new-probe", "--trailer", "Co-Authored-By: X <x@example.com>") == 0
    out = capsys.readouterr().out
    assert claimed_id(out) == "P04"  # P00 reserved, P01-P03 used (P03 by its INDEX row)
    s = P.loads_status(origin_file(origin, "probes/P04-new-probe/status.json"))
    assert (s["status"], s["cap_usd"], s["owner"], s["plan"]) == ("Planned", 75, "agent", "plan.md")
    rows = P.parse_index(origin_file(origin, "probes/INDEX.md"))
    assert list(rows) == ["P01", "P02", "P03", "P04"]
    # The legacy row was backfilled into the same commit.
    assert P.loads_status(origin_file(origin, "probes/P03-legacy/status.json"))["cap_usd"] == 5
    msg = git(origin, "log", "-1", "--format=%B", "main")
    assert msg.startswith("Probe P04 claimed: new-probe\n\nnew-probe?\n")
    assert "Co-Authored-By: X <x@example.com>" in msg
    # The checkout is untouched: same HEAD, same index, clean tree, no local probe dir.
    assert git(a, "rev-parse", "HEAD") == head
    assert (a / ".git" / "index").read_bytes() == index_bytes
    assert git(a, "status", "--porcelain") == ""
    assert not (a / "probes" / "P04-new-probe").exists()


def test_untracked_dir_in_any_checkout_is_skipped(repos, capsys):
    _, a, _ = repos
    wt = a.parent / "A-wt"
    git(a, "worktree", "add", "-q", "-b", "probe/x", str(wt))
    (wt / "probes" / "P04-unpushed-plan").mkdir()  # another session's unpushed plan dir
    (a / "probes" / "P05-also-local").mkdir()
    assert claim(a, "next") == 0
    assert claimed_id(capsys.readouterr().out) == "P06"


def test_reserved_ids_are_never_claimed(repos, capsys):
    _, a, _ = repos
    for n in range(4, 98):
        (a / "probes" / f"P{n:02d}-x").mkdir()
    assert claim(a, "full") == 1
    assert "no free probe id" in capsys.readouterr().err


def test_race_loser_retries_and_gets_next_id(repos, monkeypatch, capsys):
    origin, a, b = repos
    real_push = pc.push
    raced = []

    def push_after_b(repo, *args, **kw):
        if not raced:  # between A's fetch and A's push, B claims the same id
            raced.append(1)
            assert claim(b, "b-probe") == 0
        return real_push(repo, *args, **kw)

    monkeypatch.setattr(pc, "push", push_after_b)
    assert claim(a, "a-probe") == 0
    cap = capsys.readouterr()
    assert "push rejected" in cap.err
    assert [claimed_id(x) for x in cap.out.split("next:")[:2]] == ["P04", "P05"]  # B, then A
    rows = P.parse_index(origin_file(origin, "probes/INDEX.md"))
    assert rows["P04"]["plan"] == "P04-b-probe/plan.md"
    assert rows["P05"]["plan"] == "P05-a-probe/plan.md"


def test_push_failure_that_is_not_a_rejection_exits_without_retry(repos, monkeypatch, capsys):
    _, a, _ = repos
    git(a, "config", "remote.origin.pushurl", str(a.parent / "no-such-remote.git"))
    calls = []
    real_push = pc.push
    monkeypatch.setattr(pc, "push", lambda *x, **k: calls.append(1) or real_push(*x, **k))
    assert claim(a, "x") == 1
    assert len(calls) == 1
    assert "push rejected" not in capsys.readouterr().err


def test_invalid_slug_refused(repos, capsys):
    _, a, _ = repos
    assert claim(a, "Bad_Slug") == 1
    assert "slug" in capsys.readouterr().err


def publish(repo: Path, pid: str, *files: str) -> int:
    return pc.main(["publish", "--repo", str(repo), "--id", pid, *files])


def test_publish_plan_lands_on_main(repos, capsys):
    origin, a, _ = repos
    (a / "probes" / "P02-second" / "plan.md").write_text("new plan\n")
    head = git(a, "rev-parse", "HEAD")
    assert publish(a, "P02", "probes/P02-second/plan.md") == 0
    assert "PUBLISHED id=P02" in capsys.readouterr().out
    assert origin_file(origin, "probes/P02-second/plan.md") == "new plan\n"
    assert git(origin, "log", "-1", "--format=%s", "main").strip() == "Probe P02: publish 1 file(s)"
    assert git(a, "rev-parse", "HEAD") == head
    # Unchanged content: nothing to push.
    before = git(origin, "rev-parse", "main")
    assert publish(a, "P02", "probes/P02-second/plan.md") == 0
    assert git(origin, "rev-parse", "main") == before


@pytest.mark.parametrize(
    "path",
    [
        "probes/INDEX.md",
        "probes/P01-first/plan.md",
        "probes/P02-second/../P01-first/plan.md",
        "README.md",
    ],
)
def test_publish_refuses_paths_outside_the_probe_dir(repos, path, capsys):
    origin, a, _ = repos
    (a / "README.md").write_text("x\n")
    before = git(origin, "rev-parse", "main")
    assert publish(a, "P02", path) == 1
    assert capsys.readouterr().err
    assert git(origin, "rev-parse", "main") == before


def test_publish_status_regenerates_index(repos):
    origin, a, _ = repos
    f = a / "probes" / "P02-second" / "status.json"
    s = dict(P.load_status(f), status="Merged", pr=31, spend_usd=12.5)
    P.write_status(f, s)
    assert publish(a, "P02", str(f)) == 0  # absolute path works too
    row = P.parse_index(origin_file(origin, "probes/INDEX.md"))["P02"]
    assert (row["status"], row["pr"], row["spend_usd"]) == ("Merged", 31, 12.5)


def test_publish_invalid_status_refused(repos, capsys):
    _, a, _ = repos
    f = a / "probes" / "P02-second" / "status.json"
    f.write_text(f.read_text().replace('"Running"', '"Done"'))
    assert publish(a, "P02", "probes/P02-second/status.json") == 1
    assert "status" in capsys.readouterr().err


def test_identical_claims_in_the_same_second_get_different_ids(repos, monkeypatch, capsys):
    """Review r1 finding 1: same arguments, identity and timestamps must not build one commit."""
    origin, a, b = repos
    monkeypatch.setattr(P, "utc_now", lambda: "2026-10-11T12:00:00Z")
    for var in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(var, "2026-10-11T12:00:00Z")
    for clone_dir in (a, b):
        git(clone_dir, "config", "user.email", "same@example.com")
        git(clone_dir, "config", "user.name", "same")
    real_push = pc.push
    raced = []

    def push_after_b(repo, *args, **kw):
        if not raced:
            raced.append(1)
            assert claim(b, "same") == 0
        return real_push(repo, *args, **kw)

    monkeypatch.setattr(pc, "push", push_after_b)
    assert claim(a, "same") == 0
    out = capsys.readouterr().out
    assert [claimed_id(x) for x in out.split("next:")[:2]] == ["P04", "P05"]
    assert "Claim-Token: " in git(origin, "log", "-1", "--format=%B", "main")


@pytest.mark.parametrize(
    "stdout, race",
    [
        ("To o\n!\trefs/x:refs/heads/main\t[rejected] (non-fast-forward)\nDone\n", True),
        ("!\trefs/x:refs/heads/main\t[rejected] (fetch first)\n", True),
        ("!\tx:refs/heads/main\t[remote rejected] (cannot lock ref 'refs/heads/main')\n", True),
        ("!\tx:refs/heads/main\t[remote rejected] (pre-receive hook declined)\n", False),
        ("!\tx:refs/heads/main\t[remote rejected] (blocked by policy)\n", False),
        ("!\tx:refs/heads/main\t[remote rejected] (protected branch hook declined)\n", False),
        ("fatal: could not read from remote repository\n", False),
    ],
)
def test_lost_race_parses_porcelain_reasons(stdout, race):
    assert pc.lost_race(stdout) is race
