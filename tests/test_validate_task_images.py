"""Tests for scripts/validate_task_images.py.

The probe runs here against a synthetic root directory (IMGVAL_ROOT), so its git and filesystem
checks are exercised without Docker: a solution commit left on a side branch, only in the reflog,
and as an unreachable (dangling) object must each give LEAK; the squashed repo gives CLEAN.
`test_rig_synthetic_images` repeats the three leaks as real images on the rig
(IMGVAL_RIG_TESTS=1).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("vti", REPO / "scripts" / "validate_task_images.py")
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)

BASE = '''def greet(name):
    return "hello " + name


def farewell(name):
    return "bye " + name
'''

SOLUTION_ADD = '''

def summarize_reservations(reservation_ledger, cutoff_timestamp):
    pending_reservations = [r for r in reservation_ledger if r.created_at < cutoff_timestamp]
    total_reserved_amount = sum(r.amount_usd for r in pending_reservations)
    return {"pending": len(pending_reservations), "reserved_total": total_reserved_amount}


class ReservationSummaryError(RuntimeError):
    """Raised when the reservation ledger cannot be summarised."""
'''

TEST_ADD = '''def test_summarize_reservations_counts_pending_rows():
    assert summarize_reservations([], cutoff_timestamp=0)["pending"] == 0
'''


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


def make_root(tmp_path: Path, variant: str) -> tuple[Path, str, str]:
    """A fake filesystem root with /app holding the task repo; returns (root, solution patch,
    test patch)."""
    root = tmp_path / "root"
    app = root / "app"
    (app / "src").mkdir(parents=True)
    (app / "tests").mkdir()
    (app / "src" / "feat.py").write_text(BASE)
    (app / "tests" / "test_feat.py").write_text("def test_greet():\n    assert True\n")
    git(app, "init", "-q", "-b", "main")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "init")
    base = git(app, "rev-parse", "HEAD").strip()
    # build the solution once to get its patch, then put the repo back
    git(app, "checkout", "-q", "-b", "sol")
    (app / "src" / "feat.py").write_text(BASE + SOLUTION_ADD)
    git(app, "commit", "-q", "-am", "solution")
    sol_patch = git(app, "diff", base, "HEAD")
    (app / "tests" / "test_feat.py").write_text("def test_greet():\n    assert True\n" + TEST_ADD)
    test_patch = git(app, "diff", "HEAD", "--", "tests")
    git(app, "checkout", "-q", "--", "tests")
    git(app, "checkout", "-q", "main")
    if variant == "side_branch":
        pass  # branch "sol" stays
    elif variant == "reflog":
        # put the solution on main, then reset it away: only the reflog keeps it
        sol_sha = git(app, "rev-parse", "sol").strip()
        git(app, "branch", "-q", "-D", "sol")
        git(app, "merge", "-q", "--ff-only", sol_sha)
        git(app, "reset", "-q", "--hard", base)
    elif variant == "dangling":
        git(app, "branch", "-q", "-D", "sol")
        git(app, "reflog", "expire", "--expire=now", "--all")
    elif variant == "clean":
        git(app, "branch", "-q", "-D", "sol")
        git(app, "reflog", "expire", "--expire=now", "--all")
        git(app, "gc", "-q", "--prune=now")
    else:
        raise ValueError(variant)
    assert (app / "src" / "feat.py").read_text() == BASE
    return root, sol_patch, test_patch


def run_probe(root: Path, task: dict, work: Path) -> str:
    """The probe's stdout; the probe itself must exit 0 with nothing on stderr."""
    script = V.build_probe(task)
    env = dict(os.environ, IMGVAL_ROOT=str(root), IMGVAL_WORK=str(work))
    r = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, env=env,
                       timeout=300, check=False)
    assert r.returncode == 0, r.stderr[-2000:]
    assert not r.stderr.strip(), r.stderr[-2000:]
    return r.stdout


def synth_task(sol_patch: str, test_patch: str, image: str = "none") -> dict:
    return {"family": "synthetic", "task": "t", "image": image, "sets": ["test"],
            "solution": sol_patch, "tests": test_patch, "shas": [], "instruction": "x",
            "solution_src": "synthetic"}


# ---------------------------------------------------------------- needles


def test_candidate_needles_skip_context_and_short_lines():
    patch = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,4 @@\n"
             " existing_context_line_that_is_long = 1\n"
             "+existing_context_line_that_is_long = 1\n"
             "+short = 1\n"
             "+brand_new_identifier_value = compute_new_thing(alpha, beta)\n")
    c = V.candidate_needles(patch)
    assert "brand_new_identifier_value = compute_new_thing(alpha, beta)" in c
    assert "existing_context_line_that_is_long = 1" not in c
    assert "short = 1" not in c


def test_candidate_needles_drop_substrings():
    patch = ("diff --git a/x.py b/x.py\n+++ b/x.py\n"
             "+value_of_reservation_ledger_total\n"
             "+print(value_of_reservation_ledger_total)\n")
    c = V.candidate_needles(patch)
    assert c == ["print(value_of_reservation_ledger_total)"]


def test_patch_paths():
    assert V.patch_paths("diff --git a/a.py b/a.py\ndiff --git a/b/c.go b/b/c.go\n") == [
        "a.py", "b/c.go"]


def test_pro_solution_shas():
    iid = ("instance_ansible__ansible-4c5ce5a1a9e79a845aff4978cfeb72a0d4ecf7d6-"
           "v1055803c3a812189a1133297f7f5468579283f86")
    assert V._pro_solution_shas(iid) == ["4c5ce5a1a9e79a845aff4978cfeb72a0d4ecf7d6"]


def test_build_probe_refuses_heredoc_marker():
    bad = "diff --git a/x b/x\n+++ b/x\n+__IMGVAL_EOF__ and a long identifier_name line\n"
    task = synth_task(bad, "")
    V.build_probe(task)  # the candidate filter drops the line ...
    with pytest.raises(ValueError):  # ... and the block guard would refuse it
        V.build_probe({**task, "shas": ["__IMGVAL_EOF__"]})


# ---------------------------------------------------------------- probe on a synthetic root


@pytest.mark.parametrize("variant", ["side_branch", "reflog", "dangling"])
def test_probe_catches_each_leak(tmp_path, variant):
    root, sol, tst = make_root(tmp_path, variant)
    out = run_probe(root, synth_task(sol, tst), tmp_path / "work")
    a = V.assess_probe(out, synth_task(sol, tst), 0)
    assert a["verdict"] == "LEAK", (a["reasons"], a["errors"], out[-3000:])
    assert not a["errors"], a["errors"]
    G = next(g for g, r in a["git"].items() if r["is_main"])
    hits = a["git"][G]["object_hits"]
    assert any(h["needle"].startswith("S") and not h["reachable_from_head"] for h in hits)
    rec = a["git"][G]
    if variant == "side_branch":
        assert rec["offhead_commits"] >= 1 and rec["commits_touching_solution_paths"] >= 1
        assert any("refs/heads/sol" in r for r in rec["refs"])
    if variant == "reflog":
        assert rec["offhead_commits"] >= 1 and rec["commits_touching_solution_paths"] >= 1
        assert rec["n_refs"] == 1
    if variant == "dangling":
        assert rec["offhead_commits"] == 0
        assert rec["unreachable"]["commit"] >= 1
        assert rec["commits_touching_solution_paths"] >= 1


def test_probe_clean_on_squashed_repo(tmp_path):
    root, sol, tst = make_root(tmp_path, "clean")
    task = synth_task(sol, tst)
    out = run_probe(root, task, tmp_path / "work")
    a = V.assess_probe(out, task, 0)
    assert a["verdict"] == "CLEAN", (a["reasons"], a["errors"])
    assert a["hidden_tests"] == "CLEAN"
    assert len([k for k in a["needles"] if k.startswith("S")]) >= V.MIN_SOL_NEEDLES


def test_probe_catches_solution_file_and_stray_patch(tmp_path):
    root, sol, tst = make_root(tmp_path, "clean")
    (root / "opt" / "cache").mkdir(parents=True)
    (root / "opt" / "cache" / "feat_new.py").write_text(SOLUTION_ADD)
    (root / "tmp").mkdir()
    (root / "tmp" / "fix.diff").write_text("--- a/src/feat.py\n+++ b/src/feat.py\n")
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK"
    assert any("opt/cache/feat_new.py" in h["path"] for h in a["fs_hits"])
    assert any("fix.diff" in p for p in a["stray"]["refs_solution"])


def test_probe_catches_gzipped_solution(tmp_path):
    import gzip

    root, sol, tst = make_root(tmp_path, "clean")
    (root / "cache").mkdir()
    (root / "cache" / "pkg.tar.gz").write_bytes(gzip.compress(SOLUTION_ADD.encode()))
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK"
    assert any(h["path"].endswith("pkg.tar.gz") for h in a["archive_hits"])


def test_probe_flags_hidden_tests_separately(tmp_path):
    root, sol, tst = make_root(tmp_path, "clean")
    (root / "tests").mkdir()
    (root / "tests" / "hidden_test.py").write_text(TEST_ADD)
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "CLEAN"
    assert a["hidden_tests"] == "LEAK"


def test_probe_flags_solution_in_head(tmp_path):
    root, sol, tst = make_root(tmp_path, "clean")
    app = root / "app"
    (app / "src" / "feat.py").write_text(BASE + SOLUTION_ADD)
    git(app, "commit", "-q", "-am", "baked in")
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK"
    assert any("already in HEAD tree" in r for r in a["reasons"])


def test_assess_probe_error_when_unfinished():
    a = V.assess_probe("@@SECTION tools\ngit /usr/bin/git\n", synth_task("", ""), 137)
    assert a["verdict"] == "ERROR"


def test_assess_layers_hits_and_git():
    needles = {"S1": "alpha_beta_gamma_delta = 1", "S2": "epsilon_zeta_eta = 2"}
    text = ("@@SECTION save\nn_needles 2\nst save 0\nst untar 0\n@@SECTION manifest\n"
            "st manifest 0\nn_layers 2\n"
            "@@SECTION layer blobs/sha256/aa\nbytes 10\nst list 0\nn_entries 3\n"
            "n_git_entries 2\ngit app/.git/HEAD\nn\tmembers\t3\nst content_py 0\n"
            "st extract 0\ngobj\tapp/.git/objects\tobjhit\tabc123\tblob\tS1\n"
            "gobj\tapp/.git/objects\tobjhit\tabc123\tblob\tS2\n"
            "st gitobj_1_git 0\nst gitobj_1_py 0\n"
            "@@SECTION layer blobs/sha256/bb\nbytes 5\nst list 0\nn_entries 1\n"
            "n_git_entries 1\nlhit\tS1\tapp/one.py\nlhit\tS2\tapp/two.py\n"
            "n\tmembers\t2\nst content_py 0\n@@SECTION end\nLAYERS_DONE\n")
    a = V.assess_layers(text, needles)
    assert a["verdict"] == "LEAK", a
    assert a["git_layers"] == ["blobs/sha256/aa", "blobs/sha256/bb"]
    assert any("git object" in r for r in a["reasons"])  # two S lines in one layer git object
    assert not any("file" in r for r in a["reasons"])  # S1 and S2 in different files: not pooled
    assert any("single-needle" in n for n in a["notes"])


def test_assess_layers_fails_closed():
    text = "@@SECTION save\nn_needles 2\nst save 0\nst untar 2\n@@SECTION end\nLAYERS_DONE\n"
    a = V.assess_layers(text, {"S1": "x" * 30})
    assert a["verdict"] == "ERROR"


def test_parse_final_json():
    t = 'blah\n{"verdict": "not found", "location": "", "extract_command": "", "evidence": "x"}'
    assert V.parse_final_json(t)["verdict"] == "not found"


# ---------------------------------------------------------------- real images on the rig

RIG_DOCKERFILE = """FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY root/app /app
WORKDIR /app
"""


@pytest.mark.skipif(os.environ.get("IMGVAL_RIG_TESTS") != "1", reason="needs the rig (IMGVAL_RIG_TESTS=1)")
@pytest.mark.parametrize("variant", ["side_branch", "reflog", "dangling", "clean"])
def test_rig_synthetic_images(tmp_path, variant, monkeypatch):
    _root, sol, tst = make_root(tmp_path, variant)
    (tmp_path / "Dockerfile").write_text(RIG_DOCKERFILE)
    tag = f"imgval-synthetic:{variant}-{uuid.uuid4().hex[:6]}"
    subprocess.run(V.docker("build", "-q", "--platform", "linux/amd64", "-t", tag, str(tmp_path)),
                   check=True, capture_output=True, timeout=900)
    monkeypatch.setattr(V, "RAW_OUT", tmp_path / "out")
    try:
        rec = V.probe_one(synth_task(sol, tst, image=tag) | {"task": variant}, log=lambda m: None)
    finally:
        subprocess.run(V.docker("rmi", "-f", tag), capture_output=True, timeout=300, check=False)
    want = "CLEAN" if variant == "clean" else "LEAK"
    assert rec["verdict"] == want, (rec.get("reasons"), rec.get("errors"))
    assert not rec.get("container_left")


def test_probe_catches_reformatted_solution_by_identifiers(tmp_path):
    root, sol, tst = make_root(tmp_path, "clean")
    (root / "site").mkdir()
    # same names, different formatting: no added line matches verbatim
    (root / "site" / "vendored.py").write_text(
        "def summarize_reservations(ledger,cutoff):\n"
        "  pending_reservations=[r for r in ledger if r.created_at<cutoff]\n"
        "  total_reserved_amount=0\n"
        "  return dict(pending=len(pending_reservations))\n")
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK", (a["reasons"], a["notes"], a["needles"])
    assert any("2+ distinct solution needles" in r and "vendored.py" in r for r in a["reasons"])


def test_candidate_identifiers_are_new_names_only():
    patch = ("diff --git a/x.py b/x.py\n+++ b/x.py\n"
             " existing_helper_function()\n"
             "+existing_helper_function(brand_new_identifier)\n")
    assert V.candidate_identifiers(patch) == ["brand_new_identifier"]


def test_single_generic_line_elsewhere_is_weak_not_leak(tmp_path):
    root, sol, tst = make_root(tmp_path, "clean")
    task = synth_task(sol, tst)
    one = V.candidate_needles(sol)[0]
    (root / "lib").mkdir()
    (root / "lib" / "other_pkg.py").write_text(one + "\n")
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "CLEAN", a["reasons"]
    assert any(w["location"].endswith("lib/other_pkg.py") for w in a["weak_hits"])


# ---------------------------------------------------------------- astra r1 regressions


def test_fails_closed_without_scan_sections():
    text = ("@@SECTION tools\ngit /usr/bin/git\ngrep /bin/grep\nfind /bin/find\nawk /bin/awk\n"
            "sort /bin/sort\nxargs /bin/xargs\npython3 /bin/python3\ngrep_b 1:b\n"
            "@@SECTION main\nmain /app/.git\n@@SECTION needles\nS1\taaaaaaaaaaaaaaaaaaaaaaaa\n"
            "S2\tbbbbbbbbbbbbbbbbbbbbbbbbbbb\nS3\tccccccccccccccccccccccccc\n"
            "@@SECTION end\nPROBE_DONE\n")
    a = V.assess_probe(text, synth_task("", ""), 2)
    assert a["verdict"] == "ERROR"
    assert any("section missing" in e for e in a["errors"])


def test_duplicate_needle_text_keeps_solution_class():
    _needles, by_text = V._needle_maps(["T1\tsame_line_in_both_patches()", "S1\tsame_line_in_both_patches()"])
    assert by_text["same_line_in_both_patches()"] == "S1"


def test_many_test_hits_do_not_hide_a_solution_hit(tmp_path):
    root, sol, tst = make_root(tmp_path, "clean")
    task = synth_task(sol, tst)
    (root / "noise").mkdir()
    for i in range(400):
        (root / "noise" / f"t{i}.py").write_text(TEST_ADD)
    (root / "z_leak.py").write_text(SOLUTION_ADD)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK", (a["reasons"], a["errors"])
    assert any("z_leak.py" in r for r in a["reasons"])


def test_nested_archive_and_corrupt_archive(tmp_path):
    import io
    import tarfile
    import zipfile

    root, sol, tst = make_root(tmp_path, "clean")
    task = synth_task(sol, tst)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("pkg/feat.py", SOLUTION_ADD)
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w:gz") as t:
        info = tarfile.TarInfo("inner.whl")
        info.size = len(zbuf.getvalue())
        t.addfile(info, io.BytesIO(zbuf.getvalue()))
    (root / "cache").mkdir()
    (root / "cache" / "blob").write_bytes(tbuf.getvalue())  # no extension: found by signature
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK", (a["reasons"], a["errors"])
    assert any("cache/blob" in r and "archive" in r for r in a["reasons"])

    root2, sol2, tst2 = make_root(tmp_path / "b", "clean")
    (root2 / "cache").mkdir()
    (root2 / "cache" / "bad.gz").write_bytes(b"\x1f\x8b" + b"not really gzip")
    a2 = V.assess_probe(run_probe(root2, synth_task(sol2, tst2), tmp_path / "work2"),
                        synth_task(sol2, tst2), 0)
    assert any(w["needle"] == "GAP" and w["review"] for w in a2["review_hits"])
    v, _why = V.overall(a2, {"verdict": "CLEAN"}, [],
                       len(V.unreviewed("synthetic/t", a2, [])))
    assert v == "ERROR"


def test_json_escaped_solution_is_found(tmp_path):
    import json

    root, sol, tst = make_root(tmp_path, "clean")
    task = synth_task(sol, tst)
    (root / "data").mkdir()
    (root / "data" / "config.json").write_text(json.dumps({"patch": sol}))
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK", (a["reasons"], a["errors"])


def test_uncommitted_solution_in_worktree(tmp_path):
    root, sol, tst = make_root(tmp_path, "clean")
    (root / "app" / "src" / "feat.py").write_text(BASE + SOLUTION_ADD)
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK"
    assert any("already applied" in r for r in a["reasons"])


def test_orphan_object_store_is_scanned(tmp_path):
    import shutil

    root, sol, tst = make_root(tmp_path, "side_branch")
    shutil.copytree(root / "app" / ".git" / "objects", root / "opt" / "objs" / "objects")
    git(root / "app", "branch", "-q", "-D", "sol")
    git(root / "app", "reflog", "expire", "--expire=now", "--all")
    git(root / "app", "gc", "-q", "--prune=now")
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK", (a["reasons"], a["errors"])
    assert any("opt/objs/objects" in r for r in a["reasons"])


def test_overall_precedence():
    assert V.overall(None)[0] == "ERROR"
    assert V.overall({"verdict": "CLEAN"})[0] == "ERROR"  # red-team not run
    assert V.overall({"verdict": "CLEAN", "redteam": {"verdict": "FOUND_UNCONFIRMED"}})[0] == "ERROR"
    assert V.overall({"verdict": "CLEAN", "redteam": {"verdict": "CLEAN"}})[0] == "CLEAN"
    assert V.overall({"verdict": "ERROR", "redteam": {"verdict": "LEAK"}})[0] == "LEAK"


def test_valid_answer():
    ok = {"verdict": "not found", "location": "", "extract_command": "", "evidence": "x"}
    assert V.valid_answer(ok) is None
    assert V.valid_answer({}) is not None
    assert V.valid_answer({**ok, "verdict": "maybe"}) is not None
    assert V.valid_answer({**ok, "verdict": "found"}) is not None  # no extract command


def test_judge_redteam_requires_route_and_success():
    errors: list[str] = []
    ans = '{"verdict": "not found", "location": "", "extract_command": "", "evidence": "x"}'
    events = [{"type": "system", "subtype": "init", "apiKeySource": "none"},
              {"type": "result", "subtype": "success", "is_error": False, "result": ans},
              {"type": "_process", "returncode": 0, "killed": None, "timed_out": False,
               "stopped": False}]
    V.judge_redteam(events, [{"executed": True}], {}, errors)
    assert any("billing route" in e for e in errors)  # no rate-limit evidence: unknown
    errors2: list[str] = []
    ok = events[:1] + [{"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "isUsingOverage": False, "overageStatus": "rejected"}}] + events[1:]
    good = {"executed": True, "rc": 0, "output": "exit=0\nok"}
    V.judge_redteam(ok, [good] * 10, {}, errors2)
    assert errors2 == []
    errors3: list[str] = []
    V.judge_redteam(ok, [good] * 3, {}, errors3)
    assert any("too shallow" in e for e in errors3)
    errors4: list[str] = []  # ten transport failures neither count nor pass
    bad = {"executed": True, "rc": 255,
           "output": "exit=255\nssh: connect to host rig port 22: Connection refused"}
    V.judge_redteam(ok, [bad] * 10, {}, errors4)
    assert any("transport" in e for e in errors4) and any("too shallow" in e for e in errors4)


def test_route_guard_kills_extra_tools():
    state: dict = {}
    f = V.route_guard(state)
    assert f({"type": "system", "subtype": "init", "apiKeySource": "none",
              "tools": ["mcp__box__sh", "Bash"], "mcp_servers": [{"name": "box",
                                                                   "status": "connected"}]})
    assert "unexpected tools" in state["killed"]


def test_mcp_budget_survives_restart(tmp_path):
    import json
    import sys

    log = tmp_path / "calls.jsonl"
    msgs = "".join(json.dumps(m) + "\n" for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "sh", "arguments": {"command": "true"}}}])
    for _ in range(3):  # three server processes, budget 2
        subprocess.run([sys.executable, str(REPO / "scripts" / "validate_task_images.py"),
                        "mcp-exec", "--container", "imgval-none", "--log", str(log),
                        "--max-calls", "2"], input=msgs, capture_output=True, text=True,
                       timeout=120, check=False,
                       env=dict(os.environ, IMGVAL_DOCKER_HOST="unix:///nonexistent.sock"))
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert [r["executed"] for r in rows] == [True, True, False]


def test_solution_line_in_reachable_history_is_weak(tmp_path):
    root, sol, tst = make_root(tmp_path, "clean")
    app = root / "app"
    one = V.candidate_needles(sol)[0]
    # an older commit on HEAD's own history had one of the solution's lines, later removed
    (app / "src" / "old.py").write_text(one + "\n")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "old")
    git(app, "rm", "-q", "src/old.py")
    git(app, "commit", "-q", "-m", "drop old")
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "CLEAN", a["reasons"]
    assert any(w["what"].endswith("git object") for w in a["weak_hits"])


# ---------------------------------------------------------------- astra r2 regressions


def test_scan_reports_overlapping_needles(tmp_path):
    import sys

    (tmp_path / "arch.py").write_text(V.ARCH_PY)
    (tmp_path / "needles").write_text("S1\tabc_def_ghi_jkl\nT1\txx abc_def_ghi_jkl yy\n")
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "f.txt").write_text("start xx abc_def_ghi_jkl yy end\n")
    r = subprocess.run([sys.executable, str(tmp_path / "arch.py"), "walk", str(tmp_path / "root"),
                        str(tmp_path / "needles"), "", "1000000"],
                       capture_output=True, text=True, check=True)
    ids = {ln.split("\t")[1] for ln in r.stdout.splitlines() if ln.startswith("fshit")}
    assert ids == {"S1", "T1"}


def test_scan_finds_needle_across_chunk_boundary(tmp_path):
    import sys

    (tmp_path / "arch.py").write_text(V.ARCH_PY.replace("64 << 20", "1024"))
    (tmp_path / "needles").write_text("S1\tneedle_split_across_the_chunk_boundary\n")
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "big.bin").write_bytes(
        b"x" * 1010 + b"needle_split_across_the_chunk_boundary" + b"y" * 5000)
    r = subprocess.run([sys.executable, str(tmp_path / "arch.py"), "walk", str(tmp_path / "root"),
                        str(tmp_path / "needles"), "", "100"],  # cap 100: forces chunked reads
                       capture_output=True, text=True, check=True)
    assert any(ln.startswith("fshit\tS1") for ln in r.stdout.splitlines()), r.stdout


def _probe_text(tmp_path, variant="clean"):
    root, sol, tst = make_root(tmp_path, variant)
    task = synth_task(sol, tst)
    return run_probe(root, task, tmp_path / "work"), task


def test_missing_git_inventory_fails_closed(tmp_path):
    out, task = _probe_text(tmp_path)
    assert V.assess_probe(out, task, 0)["verdict"] == "CLEAN"
    cut = "\n".join(ln for ln in out.splitlines() if not ln.startswith("dir "))
    a = V.assess_probe(cut, task, 0)
    assert a["verdict"] == "ERROR"
    assert any("missing from the git inventory" in e for e in a["errors"])


def test_incomplete_patch_check_fails_closed(tmp_path):
    out, task = _probe_text(tmp_path)
    cut = "\n".join(ln for ln in out.splitlines() if not ln.startswith("file "))
    a = V.assess_probe(cut, task, 0)
    assert a["verdict"] == "ERROR"
    assert any("patch check incomplete" in e for e in a["errors"])


def test_nested_gap_outside_fixtures_is_error(tmp_path):
    import io
    import zipfile

    root, sol, tst = make_root(tmp_path, "clean")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("solution.7z", b"7z\xbc\xaf\x27\x1c" + b"payload")
    (root / "opt").mkdir()
    (root / "opt" / "cache.zip").write_bytes(zbuf.getvalue())
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    gaps = [w for w in a["review_hits"] if w["needle"] == "GAP"]
    assert any("cache.zip!solution.7z" in w["location"] for w in gaps)
    assert V.overall(a, {"verdict": "CLEAN"}, [], len(V.unreviewed("k", a, [])))[0] == "ERROR"


def test_plain_tar_with_compressed_member(tmp_path):
    import gzip
    import io
    import tarfile

    root, sol, tst = make_root(tmp_path, "clean")
    gz = gzip.compress(SOLUTION_ADD.encode())
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w") as t:
        info = tarfile.TarInfo("feat.py.gz")
        info.size = len(gz)
        t.addfile(info, io.BytesIO(gz))
    (root / "opt").mkdir()
    (root / "opt" / "bundle").write_bytes(tbuf.getvalue())
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK", (a["reasons"], a["errors"])
    assert any("opt/bundle" in r for r in a["reasons"])


def test_loose_only_orphan_store(tmp_path):
    import shutil

    root, sol, tst = make_root(tmp_path, "side_branch")
    dst = root / "opt" / "copy" / "objects"
    shutil.copytree(root / "app" / ".git" / "objects", dst)
    for d in ("pack", "info"):
        shutil.rmtree(dst / d, ignore_errors=True)
    git(root / "app", "branch", "-q", "-D", "sol")
    git(root / "app", "reflog", "expire", "--expire=now", "--all")
    git(root / "app", "gc", "-q", "--prune=now")
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK", (a["reasons"], a["errors"])
    assert any("opt/copy/objects" in r for r in a["reasons"])


def test_reviews_clear_single_hits():
    rec = {"needles": {"S1": "line_one_text_here"},
           "review_hits": [{"what": "file", "location": "/usr/lib/x.py", "needle": "S1",
                            "review": True}]}
    assert len(V.unreviewed("deepswe/t", rec, [])) == 1
    rec["image_id"] = "sha256:img1"
    rv = [{"key": "deepswe/t", "image_id": "sha256:img1", "text": "line_one_text_here",
           "where": "/usr/lib/", "reason": "x", "by": "imgval", "at": "2026-10-24T00:00:00Z"}]
    assert V.unreviewed("deepswe/t", rec, rv) == []
    assert len(V.unreviewed("deepswe/other", rec, rv)) == 1
    assert len(V.unreviewed("deepswe/t", {**rec, "image_id": "sha256:rebuilt"}, rv)) == 1
    assert len(V.unreviewed("deepswe/t", rec, [{**rv[0], "reason": ""}])) == 1
    v, why = V.overall({"verdict": "CLEAN"}, {"verdict": "CLEAN"}, [], 1)
    assert v == "ERROR" and "await manual review" in why[0]


def test_stage_state_detects_changed_solution(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "RAW_OUT", tmp_path)
    task = synth_task("diff --git a/x b/x\n+new_line_here_long_enough\n", "")
    V.write_rec(V.out_paths(task)[1], {"scanner": V.SCANNER_VERSION, "image_id": "sha256:a",
                                       "solution_sha256": V.sha("old patch"), "verdict": "CLEAN"})
    V.write_rec(V.sub_path(task, "redteam"), {"verdict": "CLEAN", "image_id": "sha256:b"})
    st = V.stage_state(task, "sha256:a")
    assert "reference solution changed" in st["stale"]
    assert st["rt_stale"]


def test_layer_script_end_to_end_with_fake_docker(tmp_path):
    import io
    import json
    import tarfile

    root, sol, _tst = make_root(tmp_path, "side_branch")
    needles = {"S1": V.candidate_needles(sol)[0], "S2": V.candidate_needles(sol)[1]}

    def layer(add):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            add(t)
        return buf.getvalue()

    l1 = layer(lambda t: t.add(root / "app", arcname="app"))  # the leaky .git
    l2 = layer(lambda t: t.addfile(tarfile.TarInfo("app/.wh..git")))  # deleted later
    img = io.BytesIO()
    with tarfile.open(fileobj=img, mode="w") as t:
        for name, data in (("blobs/l1", l1), ("blobs/l2", l2), ("manifest.json", json.dumps(
                [{"Layers": ["blobs/l1", "blobs/l2"]}]).encode())):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    (tmp_path / "img.tar").write_bytes(img.getvalue())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(  # docker save IMG -o OUT
        f'#!/bin/bash\n[ "$1" = save ] && cp {tmp_path / "img.tar"} "$4"\n')
    (bin_dir / "timeout").write_text('#!/bin/bash\nshift 3; exec "$@"\n')  # timeout -k 10 T cmd
    for f in ("docker", "timeout"):
        (bin_dir / f).chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path / "home"), PATH=f"{bin_dir}:{os.environ['PATH']}")
    (tmp_path / "home").mkdir()
    script = (V.LAYER_SH.replace("__T__", "60").replace("__CAP__", "100000000")
              .replace("__LAYER_PY__", V.LAYER_PY.strip("\n"))
              .replace("__ARCH_PY__", V.ARCH_PY.strip("\n")))
    r = subprocess.run(["bash", "-c", script, "imgval", "fake:img"], input="".join(
        f"{k}\t{v}\n" for k, v in needles.items()), capture_output=True, text=True, env=env,
        timeout=300, check=False)
    a = V.assess_layers(r.stdout, needles)
    assert a["verdict"] == "LEAK", (a, r.stderr[-2000:])
    assert any("git object" in x for x in a["reasons"])
    assert a["n_layers"] == 2


# ---------------------------------------------------------------- astra r3 regressions


def test_archive_committed_in_git_history_is_decoded(tmp_path):
    import gzip

    root, sol, tst = make_root(tmp_path, "clean")
    app = root / "app"
    (app / "vendor.tgz").write_bytes(gzip.compress(SOLUTION_ADD.encode()))
    git(app, "add", "vendor.tgz")
    git(app, "commit", "-q", "-m", "vendor")
    git(app, "rm", "-q", "vendor.tgz")
    git(app, "commit", "-q", "-m", "drop vendor")
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK", (a["reasons"], a["errors"])
    assert any("git object" in r for r in a["reasons"])


def test_old_format_tar_is_read(tmp_path):
    import gzip
    import io
    import tarfile

    root, sol, tst = make_root(tmp_path, "clean")
    gz = gzip.compress(SOLUTION_ADD.encode())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
        info = tarfile.TarInfo("feat.gz")
        info.size = len(gz)
        t.addfile(info, io.BytesIO(gz))
    raw = bytearray(buf.getvalue())
    raw[257:265] = b"\0" * 8  # strip the ustar magic: an old v7 header, checksum redone
    raw[148:156] = b" " * 8
    raw[148:156] = (b"%06o" % sum(raw[:512])) + b"\0 "
    (root / "opt").mkdir()
    (root / "opt" / "old.tar").write_bytes(bytes(raw))
    task = synth_task(sol, tst)
    a = V.assess_probe(run_probe(root, task, tmp_path / "work"), task, 0)
    assert a["verdict"] == "LEAK", (a["reasons"], a["errors"], a["archives"])


def test_missing_git_status_fails_closed(tmp_path):
    out, task = _probe_text(tmp_path)
    cut = "\n".join(ln for ln in out.splitlines() if not ln.startswith("st fsck "))
    a = V.assess_probe(cut, task, 0)
    assert a["verdict"] == "ERROR"
    assert any("fsck status missing" in e for e in a["errors"])


def test_redteam_stale_on_prompt_change(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "RAW_OUT", tmp_path)
    task = synth_task("diff --git a/x b/x\n+new_line_here_long_enough\n", "")
    V.write_rec(V.out_paths(task)[1], {"scanner": V.SCANNER_VERSION, "image_id": "sha256:a",
                                       "solution_sha256": V.sha(task["solution"]),
                                       "tests_sha256": V.sha(""), "verdict": "CLEAN"})
    V.write_rec(V.sub_path(task, "redteam"), {"verdict": "CLEAN", "image_id": "sha256:a",
                                              "prompt_sha": "old"})
    assert V.stage_state(task, "sha256:a")["rt_stale"]


def test_agent_exit_255_is_not_transport():
    errors: list[str] = []
    ans = '{"verdict": "not found", "location": "", "extract_command": "", "evidence": "x"}'
    ev = [{"type": "system", "subtype": "init", "apiKeySource": "none"},
          {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
                                                           "isUsingOverage": False,
                                                           "overageStatus": "rejected"}},
          {"type": "result", "subtype": "success", "is_error": False, "result": ans},
          {"type": "_process", "returncode": 0}]
    calls = [{"executed": True, "rc": 255, "output": "exit=255\n/app/.git\nfound: ssh: x"}] * 10
    V.judge_redteam(ev, calls, {}, errors)
    assert errors == []


def test_lockfile_lines_are_not_evidence():
    patch = ("diff --git a/go.sum b/go.sum\n+++ b/go.sum\n"
             "+github.com/example/dep v1.2.3 h1:AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd=\n"
             "diff --git a/pkg/x.go b/pkg/x.go\n+++ b/pkg/x.go\n"
             "+func NewWidgetFactoryFromConfig(cfg Config) *WidgetFactory {\n")
    lock = V.lockfile_texts(patch)
    assert "github.com/example/dep v1.2.3 h1:AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd=" in lock
    assert "NewWidgetFactoryFromConfig" not in lock
