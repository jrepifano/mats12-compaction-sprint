"""compaction.s2.families.pro: ids, step texts, image build inputs, the squash, the verifier and
the usability gate. No Docker: the squash and the grader run on local temp git repos."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from compaction.deepswe.sandbox import parse_reward
from compaction.s2 import families
from compaction.s2.families import pro as P

HAVE_SI = P.si_root().exists() and any(P.si_root().glob("swebenchpro_*"))
needs_si = pytest.mark.skipif(not HAVE_SI, reason="SWE-Interact checkout not present")


# ------------------------------------------------------------------------------------------
# ids and sources
# ------------------------------------------------------------------------------------------


def test_short_id_examples():
    assert P.short_id("instance_ansible__ansible-4c5ce5a1a9e79a845aff4978cfeb72a0d4ecf7d6-"
                      "v1055803c3a812189a1133297f7f5468579283f86") == "pro-ansible-4c5ce5a1"
    assert P.short_id("instance_element-hq__element-web-d06cf09bf0b3d4a0fbe6bd32e4115caea"
                      "2083168-vnan") == "pro-element-web-d06cf09b"
    assert P.short_id("instance_NodeBB__NodeBB-f083cd559d69c16481376868c8da65172729c0ca-vnan"
                      ) == "pro-nodebb-f083cd55"
    assert P.short_id("instance_flipt-io__flipt-96820c3ad10b0b2305e8877b6b303f7fafdf815f"
                      ) == "pro-flipt-96820c3a"
    with pytest.raises(ValueError):
        P.short_id("deepswe_koota")


def test_image_tags():
    assert P.image_tag("pro-ansible-4c5ce5a1") == "emx-tasks/swe-interact-pro:ansible-4c5ce5a1-v1"
    assert P.stage_tag("pro-ansible-4c5ce5a1") == "emx-tasks/swe-interact-pro-stage:ansible-4c5ce5a1"


def test_registry_routes_pro_ids():
    assert families.family_of("pro-ansible-4c5ce5a1") == "compaction.s2.families.pro"
    assert families.family_of("koota") is None


@needs_si
def test_sources_are_25_unique_and_consistent():
    srcs = P.sources()
    assert len(srcs) == 25
    for tid, s in srcs.items():
        assert tid.startswith(P.PREFIX)
        assert s.solution_sha.startswith(tid.rsplit("-", 1)[1])
        assert s.from_image.startswith("jefzda/sweap-images:")
        assert len(P.image_tag(tid).split(":", 1)[1]) <= 128
        assert s.fail_to_pass, tid  # parse_reward needs f2p_total > 0 for a reward of 1


@needs_si
def test_image_tags_match_imgvals_derivation():
    for tid, s in P.sources().items():
        want = (f"emx-tasks/swe-interact-pro:{s.config['repo'].split('/')[-1].lower()}-"
                f"{s.solution_sha[:8]}-v1")
        assert P.image_tag(tid) == want


@needs_si
def test_gold_checkout_files_equal_test_patch_files():
    """The grader's reset-then-apply-test_patch equals upstream's `git checkout <solution> --
    <files>` only if the two file lists match (module docstring)."""
    for tid, s in P.sources().items():
        last = s.config["before_repo_set_cmd"].strip().splitlines()[-1]
        assert last.startswith(f"git checkout {s.solution_sha} -- "), tid
        files = set(shlex.split(last.split(" -- ", 1)[1]))
        assert files == set(P.patch_paths(s.config["test_patch"])), tid
        assert not files & set(P.patch_paths(s.config["patch"])), tid


@needs_si
def test_step_texts_only_move_the_plan_file():
    for s in P.sources().values():
        steps = P.step_texts(s)
        assert set(steps) == {"01_plan", "02_implement"}
        for name, text in steps.items():
            orig = (s.src_dir / "steps" / name / "instruction.md").read_text()
            assert P.PLAN_FROM in orig
            assert P.PLAN_FROM not in text and P.PLAN_TO in text
            assert text == orig.replace(P.PLAN_FROM, P.PLAN_TO)
        persona = (s.src_dir / "environment" / "user-server" / "persona.md").read_text()
        assert "plan.md" not in persona


# ------------------------------------------------------------------------------------------
# build inputs
# ------------------------------------------------------------------------------------------


@needs_si
def test_stage_dockerfile_appends_the_squash(tmp_path):
    s = P.sources()["pro-ansible-4c5ce5a1"]
    df = P.stage_dockerfile(s)
    orig = (s.src_dir / "environment" / "Dockerfile").read_text()
    assert df.startswith(orig.rstrip("\n"))
    assert "ENTRYPOINT []" in df
    assert df.rstrip().endswith(
        f"RUN timeout -k 5 {P.SQUASH_TIMEOUT_S} bash /tmp/emx_squash.sh {s.base_commit} "
        f"{s.solution_sha} && rm -f /tmp/emx_squash.sh /tmp/emx_test.patch")
    d = P.materialize(s, tmp_path)
    assert sorted(p.name for p in (d / "build").iterdir()) == sorted(P.BUILD_FILES)
    assert (d / "build" / "test.patch").read_text() == s.config["test_patch"]
    h = P.build_inputs_sha256(d), P.verifier_sha256(d)
    assert (d / "tests" / "test.patch").read_text() == s.config["test_patch"]
    assert (d / "solution" / "solution.patch").read_text() == s.config["patch"]
    assert os.access(d / "tests" / "test.sh", os.X_OK)
    before = {p: p.stat().st_mtime_ns for p in d.rglob("*") if p.is_file()}
    P.materialize(s, tmp_path)  # idempotent: nothing rewritten
    assert before == {p: p.stat().st_mtime_ns for p in d.rglob("*") if p.is_file()}
    assert h == (P.build_inputs_sha256(d), P.verifier_sha256(d))
    (d / "tests" / "parser.py").write_text("changed")  # the official files are hashed too
    assert P.verifier_sha256(d) != h[1]


def test_final_dockerfile_flattens_and_redeclares_config():
    cfg = {"Env": ["PATH=/usr/local/go/bin:/usr/bin", 'ODD=a"b$c\\d', "PYTHONPATH=/app/lib:/app"],
           "WorkingDir": "/app", "User": ""}
    df = P.final_dockerfile("pro-x-1", "emx-tasks/swe-interact-pro-stage:x-1", cfg).splitlines()
    assert df[:3] == ["FROM emx-tasks/swe-interact-pro-stage:x-1 AS stage", "FROM scratch",
                      "COPY --from=stage / /"]
    assert 'ENV PATH="/usr/local/go/bin:/usr/bin"' in df
    assert 'ENV ODD="a\\"b\\$c\\\\d"' in df
    assert "WORKDIR /app" in df and "ENTRYPOINT []" in df
    assert not any(line.startswith("USER") for line in df)


# ------------------------------------------------------------------------------------------
# the squash, on a local repo
# ------------------------------------------------------------------------------------------


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


@pytest.fixture
def git_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


def _leaky_repo(app: Path) -> tuple[str, str]:
    """A repo at a base commit with the solution on a branch, a tag, the reflog and a stash,
    plus a force-added tracked file that .gitignore matches and an ignored untracked file."""
    app.mkdir()
    _git(app, "init", "-q")
    (app / "lib.py").write_text("def f():\n    return 1\n")
    (app / ".gitignore").write_text("*.log\nbuild/\n")
    (app / "keep.log").write_text("tracked although ignored\n")
    (app / "tests").mkdir()
    (app / "tests" / "test_lib.py").write_text("old test\n")
    _git(app, "add", "lib.py", ".gitignore", "tests")
    _git(app, "add", "-f", "keep.log")
    _git(app, "commit", "-q", "-m", "base")
    base = _git(app, "rev-parse", "HEAD")
    (app / "lib.py").write_text("def f():\n    return 'SOLUTION-MARKER'\n")
    (app / "tests" / "test_lib.py").write_text("new test\n")
    (app / "tests" / "test_new.py").write_text("added test\n")
    _git(app, "add", "-A")
    _git(app, "commit", "-q", "-m", "solution")
    sol = _git(app, "rev-parse", "HEAD")
    (app.parent / "test.patch").write_text(
        _git(app, "diff", "--binary", base, sol, "--", "tests") + "\n")
    _git(app, "tag", "v-solution")
    _git(app, "branch", "devel")
    _git(app, "checkout", "-q", "--detach", base)
    (app / "lib.py").write_text("stashed\n")
    _git(app, "stash", "-q")
    (app / "build").mkdir()
    (app / "build" / "artifact").write_text("ignored, untracked\n")
    return base, sol


def test_squash_removes_history_and_keeps_tree(tmp_path, git_env):
    app = tmp_path / "app"
    base, sol = _leaky_repo(app)
    base_tree = _git(app, "rev-parse", f"{base}^{{tree}}")
    prof, rec = tmp_path / "profile.d", tmp_path / "squash.txt"
    prof.mkdir()
    env = {**git_env, "APP_DIR": str(app), "PROFILE_D": str(prof), "SQUASH_RECORD": str(rec),
           "TEST_PATCH": str(tmp_path / "test.patch")}
    subprocess.run(["bash", "-c", P.SQUASH_SH, "squash", base, sol], check=True, env=env,
                   capture_output=True, text=True)
    assert _git(app, "rev-list", "--all").splitlines() == [_git(app, "rev-parse", "HEAD")]
    assert _git(app, "rev-parse", "HEAD^{tree}") == base_tree
    assert _git(app, "for-each-ref", "--format=%(refname)") == "refs/heads/main"
    assert _git(app, "log", "-1", "--format=%s %an") == "init emx"
    assert _git(app, "stash", "list") == ""
    assert "keep.log" in _git(app, "ls-files").split()
    assert (app / "build" / "artifact").exists()  # ignored files stay on disk
    for obj in (sol, base):
        r = subprocess.run(["git", "cat-file", "-e", obj], cwd=app, capture_output=True,
                           check=False)
        assert r.returncode != 0
    assert b"SOLUTION-MARKER" not in subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"], cwd=app,
        capture_output=True, check=False).stdout
    assert rec.read_text().split() == [base, base_tree, "test_patch_equiv=1"]
    assert (prof / "zz-emx-docker-path.sh").read_text().startswith("export PATH='")


def test_squash_keeps_gitlinks(tmp_path, git_env):
    """Submodule entries (teleport, openlibrary) survive the squash with their shas, so the
    tree check passes, and the failing step is named on stderr when one doesn't."""
    app = tmp_path / "app"
    app.mkdir()
    _git(app, "init", "-q")
    (app / "a.txt").write_text("a\n")
    (app / "vendor" / "sub").mkdir(parents=True)  # an uninitialised submodule: an empty dir
    _git(app, "add", "a.txt")
    _git(app, "update-index", "--add", "--cacheinfo", f"160000,{'ab' * 20},vendor/sub")
    _git(app, "commit", "-q", "-m", "base")
    base = _git(app, "rev-parse", "HEAD")
    (app / "t.txt").write_text("t\n")
    _git(app, "add", "t.txt")
    _git(app, "commit", "-q", "-m", "solution")
    sol = _git(app, "rev-parse", "HEAD")
    (tmp_path / "test.patch").write_text(_git(app, "diff", base, sol) + "\n")
    _git(app, "checkout", "-q", "--detach", base)
    tree = _git(app, "rev-parse", "HEAD^{tree}")
    env = {**git_env, "APP_DIR": str(app), "PROFILE_D": str(tmp_path),
           "SQUASH_RECORD": str(tmp_path / "rec"), "TEST_PATCH": str(tmp_path / "test.patch")}
    subprocess.run(["bash", "-c", P.SQUASH_SH, "squash", base, sol], check=True, env=env,
                   capture_output=True)
    assert _git(app, "rev-parse", "HEAD^{tree}") == tree
    assert _git(app, "ls-files", "-s", "vendor/sub").startswith(f"160000 {'ab' * 20}")
    r = subprocess.run(["bash", "-c", P.SQUASH_SH, "squash", "0" * 40, sol], env=env,
                       capture_output=True, text=True, check=False)
    assert r.returncode != 0 and "emx squash: failed at line" in r.stderr


def test_squash_keeps_initialised_submodules_pruned(tmp_path, git_env):
    """An initialised submodule's git dir lives in the parent's .git/modules: it is carried
    over (else git status fails in the image: teleport webassets, openlibrary infogami) and
    pruned to its checked-out commit, so its future commits are gone too. Dirty content inside
    it is tolerated, as upstream graded with it."""
    sub = tmp_path / "subsrc"
    sub.mkdir()
    _git(sub, "init", "-q")
    (sub / "s.txt").write_text("ANCESTOR-MARKER\n")
    _git(sub, "add", "-A")
    _git(sub, "commit", "-q", "-m", "s0")
    (sub / "s.txt").write_text("s\n")
    _git(sub, "add", "-A")
    _git(sub, "commit", "-q", "-m", "s1")
    (sub / "s.txt").write_text("FUTURE-MARKER\n")
    _git(sub, "commit", "-q", "-am", "future")
    _git(sub, "checkout", "-q", "HEAD~1")
    app = tmp_path / "app"
    app.mkdir()
    _git(app, "init", "-q")
    (app / "a.txt").write_text("a\n")
    _git(app, "add", "a.txt")
    _git(app, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/s")
    _git(app, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/d")
    _git(app, "commit", "-q", "-m", "base")
    _git(app, "submodule", "deinit", "-q", "-f", "vendor/d")  # dormant: its store stays
    assert (app / ".git" / "modules" / "vendor" / "d").is_dir()
    base = _git(app, "rev-parse", "HEAD")
    sub_head = _git(app / "vendor" / "s", "rev-parse", "HEAD")
    (app / "t.txt").write_text("t\n")
    _git(app, "add", "t.txt")
    _git(app, "commit", "-q", "-m", "solution")
    sol = _git(app, "rev-parse", "HEAD")
    (tmp_path / "test.patch").write_text(_git(app, "diff", base, sol) + "\n")
    _git(app, "checkout", "-q", "--detach", base)
    (app / "vendor" / "s" / "build.out").write_text("dirty\n")  # untracked inside the sub
    (app / "vendor" / "s" / "staged.txt").write_text("STAGED-MARKER\n")
    _git(app / "vendor" / "s", "add", "staged.txt")  # a staged blob outside every base tree
    tree = _git(app, "rev-parse", "HEAD^{tree}")
    env = {**git_env, "APP_DIR": str(app), "PROFILE_D": str(tmp_path),
           "SQUASH_RECORD": str(tmp_path / "rec"), "TEST_PATCH": str(tmp_path / "test.patch")}
    subprocess.run(["bash", "-c", P.SQUASH_SH, "squash", base, sol], check=True, env=env,
                   capture_output=True)
    assert _git(app, "rev-parse", "HEAD^{tree}") == tree
    assert _git(app, "status", "--porcelain", "--ignore-submodules=dirty") == ""
    s_dir = app / "vendor" / "s"
    assert _git(s_dir, "rev-parse", "HEAD") == sub_head
    assert _git(s_dir, "for-each-ref") == ""
    objs = subprocess.run(["git", "cat-file", "--batch-all-objects", "--batch"], cwd=s_dir,
                          capture_output=True, check=True).stdout
    for marker in (b"FUTURE-MARKER", b"ANCESTOR-MARKER", b"STAGED-MARKER"):
        assert marker not in objs
    assert b"s\n" in objs
    gdir = Path(_git(s_dir, "rev-parse", "--absolute-git-dir"))
    assert (gdir / "shallow").read_text().strip() == sub_head
    assert not (app / ".git" / "modules" / "vendor" / "d").exists()  # dormant store dropped


def test_squash_names_the_differing_test_path(tmp_path, git_env):
    app = tmp_path / "app"
    base, sol = _leaky_repo(app)
    tp = tmp_path / "test.patch"
    tp.write_text(tp.read_text().replace("+new test", "+other test"))
    env = {**git_env, "APP_DIR": str(app), "PROFILE_D": str(tmp_path),
           "SQUASH_RECORD": str(tmp_path / "rec"), "TEST_PATCH": str(tp)}
    r = subprocess.run(["bash", "-c", P.SQUASH_SH, "squash", base, sol], env=env,
                       capture_output=True, text=True, check=False)
    assert "differs from the solution at tests/test_lib.py" in r.stderr


def test_squash_keeps_crlf_blobs_byte_for_byte(tmp_path, git_env):
    """A CRLF blob committed before a `text=auto` .gitattributes would be normalised by a
    re-add; the squash copies objects instead (openlibrary-0a90f9f0)."""
    app = tmp_path / "app"
    app.mkdir()
    _git(app, "init", "-q")
    (app / "page.html").write_bytes(b"a\r\nb\r\n")
    _git(app, "add", "page.html")
    _git(app, "commit", "-q", "-m", "crlf")
    (app / ".gitattributes").write_text("* text=auto\n")
    _git(app, "add", ".gitattributes")
    _git(app, "commit", "-q", "-m", "base")
    base = _git(app, "rev-parse", "HEAD")
    (app / "t.txt").write_text("t\n")
    _git(app, "add", "t.txt")
    _git(app, "commit", "-q", "-m", "solution")
    sol = _git(app, "rev-parse", "HEAD")
    (tmp_path / "test.patch").write_text(_git(app, "diff", base, sol) + "\n")
    _git(app, "checkout", "-q", "--detach", base)
    tree = _git(app, "rev-parse", "HEAD^{tree}")
    env = {**git_env, "APP_DIR": str(app), "PROFILE_D": str(tmp_path),
           "SQUASH_RECORD": str(tmp_path / "rec"), "TEST_PATCH": str(tmp_path / "test.patch")}
    subprocess.run(["bash", "-c", P.SQUASH_SH, "squash", base, sol], check=True, env=env,
                   capture_output=True)
    assert _git(app, "rev-parse", "HEAD^{tree}") == tree
    assert subprocess.run(["git", "cat-file", "blob", "HEAD:page.html"], cwd=app, check=True,
                          capture_output=True).stdout == b"a\r\nb\r\n"


def test_squash_is_deterministic(tmp_path, git_env):
    heads = []
    for n in ("a", "b"):
        app = tmp_path / n / "app"
        app.parent.mkdir()
        base, sol = _leaky_repo(app)
        env = {**git_env, "APP_DIR": str(app), "PROFILE_D": str(app.parent),
               "SQUASH_RECORD": str(app.parent / "rec"),
               "TEST_PATCH": str(app.parent / "test.patch")}
        subprocess.run(["bash", "-c", P.SQUASH_SH, "squash", base, sol], check=True, env=env,
                       capture_output=True)
        heads.append(_git(app, "rev-parse", "HEAD"))
    assert heads[0] == heads[1]


@pytest.mark.parametrize("case", ["wrong_base", "missing_solution", "patch_differs",
                                  "patch_partial"])
def test_squash_refuses(tmp_path, git_env, case):
    app = tmp_path / "app"
    base, sol = _leaky_repo(app)
    tp = tmp_path / "test.patch"
    if case == "wrong_base":
        base = "0" * 40
    elif case == "missing_solution":
        sol = "1" * 40
    elif case == "patch_differs":
        tp.write_text(tp.read_text().replace("+new test", "+other test"))
    else:  # only one of the two solution test files
        tp.write_text(_git(app, "diff", "--binary", base, sol, "--", "tests/test_lib.py") + "\n")
    env = {**git_env, "APP_DIR": str(app), "PROFILE_D": str(tmp_path),
           "SQUASH_RECORD": str(tmp_path / "rec"), "TEST_PATCH": str(tp)}
    r = subprocess.run(["bash", "-c", P.SQUASH_SH, "squash", base, sol], env=env,
                       capture_output=True, check=False)
    if case == "patch_partial":
        # a patch whose own paths match the solution passes: the per-path check is what
        # the grader relies on; the file-list equality is the separate unit test above
        assert r.returncode == 0
        return
    assert r.returncode != 0
    assert len(_git(app, "rev-list", "--all").splitlines()) > 1  # untouched


# ------------------------------------------------------------------------------------------
# the verifier, on a local repo
# ------------------------------------------------------------------------------------------

RUN_SCRIPT = """#!/bin/bash
# fake run_script.sh: "runs" each test function in tests/test_lib.py
cd "$APP_DIR"
echo "files=$1"
python3 - <<'EOF'
import re
src = open("lib.py").read()
tests = open("tests/test_lib.py").read()
for name, want in re.findall(r"def (test_\\w+).*?# expect (\\S+)", tests, re.S):
    ok = want in src
    print(("PASS " if ok else "FAIL ") + "tests/test_lib.py::" + name)
EOF
"""

PARSER = """import json, sys
out, err, dest = sys.argv[1:4]
tests = []
for line in open(out):
    if line.startswith(("PASS ", "FAIL ")):
        st, name = line.split()
        tests.append({"name": name, "status": "PASSED" if st == "PASS" else "FAILED"})
json.dump({"tests": tests}, open(dest, "w"))
"""

TEST_PATCH = """diff --git a/tests/test_lib.py b/tests/test_lib.py
--- a/tests/test_lib.py
+++ b/tests/test_lib.py
@@ -1 +1,4 @@
-old test
+def test_new():  # expect NEW
+    pass
+def test_old():  # expect def
+    pass
"""

GOLD = """diff --git a/lib.py b/lib.py
--- a/lib.py
+++ b/lib.py
@@ -1,2 +1,2 @@
 def f():
-    return 1
+    return 'NEW'
"""

PLAN = """diff --git a/plan.md b/plan.md
new file mode 100644
--- /dev/null
+++ b/plan.md
@@ -0,0 +1 @@
+the plan
"""

CHEAT = """diff --git a/tests/test_lib.py b/tests/test_lib.py
--- a/tests/test_lib.py
+++ b/tests/test_lib.py
@@ -1 +1,2 @@
-old test
+def test_new():  # expect def
+    pass
"""


@pytest.fixture
def verifier(tmp_path, git_env):
    app, tests, ver, art = (tmp_path / n for n in ("app", "tests", "ver", "art"))
    app.mkdir()
    _git(app, "init", "-q")
    (app / "lib.py").write_text("def f():\n    return 1\n")
    (app / "tests").mkdir()
    (app / "tests" / "test_lib.py").write_text("old test\n")
    _git(app, "add", "-A")
    _git(app, "commit", "-q", "-m", "init")
    tests.mkdir()
    (tests / "grader.py").write_text(P.GRADER_PY)
    (tests / "run_script.sh").write_text(RUN_SCRIPT)
    (tests / "parser.py").write_text(PARSER)
    (tests / "test.patch").write_text(TEST_PATCH)
    (tests / "config.json").write_text(json.dumps({
        "fail_to_pass": "['tests/test_lib.py::test_new']",
        "pass_to_pass": json.dumps(["tests/test_lib.py::test_old"]),
        "selected_test_files_to_run": json.dumps(["tests/test_lib.py"])}))
    env = {**git_env, "TESTS_DIR": str(tests), "VERIFIER_DIR": str(ver), "APP_DIR": str(app),
           "ARTIFACTS_DIR": str(art)}

    def run(patch: str) -> dict:
        _git(app, "reset", "-q", "--hard")
        _git(app, "clean", "-fdq")
        if ver.exists():
            for f in ver.iterdir():
                f.unlink()
        art.mkdir(exist_ok=True)
        (art / "model.patch").write_text(patch)
        for step in ("prepare", "grade"):
            subprocess.run([sys.executable, str(tests / "grader.py"), step], check=True,
                           env=env, cwd=app, capture_output=True)
            if step == "prepare" and (ver / "reward.json").exists():
                break
        text = (ver / "reward.json").read_text()
        parse_reward("json", text)  # what the harness accepts
        return json.loads(text)
    return run


def test_grader_base_fails_gold_passes(verifier):
    base = verifier("")
    assert (base["reward"], base["f2p_passed"], base["p2p_passed"]) == (0, 0, 1)
    gold = verifier(GOLD)
    assert gold["reward"] == 1 and gold["f2p_total"] == 1 and gold["p2p_total"] == 1


def test_grader_drops_a_root_plan_md(verifier):
    assert verifier(GOLD + PLAN)["reward"] == 1


def test_grader_overrides_test_file_edits(verifier):
    """An agent edit to a hidden test file is replaced by the test_patch version."""
    r = verifier(CHEAT)
    assert r["reward"] == 0 and r["f2p_passed"] == 0


def test_grader_apply_failure_scores_zero(verifier):
    r = verifier(GOLD.replace("return 1", "return 2"))
    assert r["reward"] == 0 and r["apply_failed"] == 1


def test_grader_parser_failure_scores_zero(verifier, tmp_path):
    """A parser that exits nonzero scores 0 even when it left valid-looking JSON (upstream's
    set -e; astra r1 #6)."""
    (tmp_path / "tests" / "parser.py").write_text(PARSER + "\nsys.exit(2)\n")
    r = verifier(GOLD)
    assert r["reward"] == 0 and r["parser_failed"] == 1


def test_test_sh_writes_crash_sentinel_without_reward():
    assert "echo -1 > /logs/verifier/reward.txt" in P.TEST_SH
    assert re.search(r"grader\.py prepare \|\| exit", P.TEST_SH)


# ------------------------------------------------------------------------------------------
# usability gate and loading
# ------------------------------------------------------------------------------------------


def _results(d: Path, out: Path, *, built=True, bg_pass=True, bg_image="sha256:img",
             verifier=None, graded_by="image_id"):
    d.mkdir(parents=True, exist_ok=True)
    tag = P.image_tag("pro-ansible-4c5ce5a1")
    if built:
        (d / "images.json").write_text(json.dumps({"tasks": {"pro-ansible-4c5ce5a1": {
            "image": tag, "image_id": "sha256:img", "squashed_head": "a" * 40}}}))
    tdir = P.materialize(P.sources()["pro-ansible-4c5ce5a1"], out)
    (d / "base_gold.json").write_text(json.dumps({"tasks": {"pro-ansible-4c5ce5a1": {
        "pass": bg_pass, "reason": None if bg_pass else "gold reward 0", "image_id": bg_image,
        "graded_by": graded_by, "verifier_sha256": verifier or P.verifier_sha256(tdir)}}}))
    return tag


def _row(**kw):
    return {"key": "pro_rebuilt/instance_x", "image": "IMG", "image_id": "sha256:img",
            "verdict": "CLEAN", "layers": "CLEAN", "hidden_tests": "CLEAN", **kw}


@pytest.mark.parametrize("summary,want", [
    ({"rows": [_row()]}, "CLEAN"),
    ({"rows": [_row(hidden_tests=None)]}, "CLEAN"),
    ({"rows": [_row(layers="LEAK")]}, "LEAK"),
    ({"rows": [_row(verdict="ERROR")]}, "ERROR"),
    ({"rows": [_row(hidden_tests="LEAK")]}, "LEAK"),
    ({"rows": [_row(layers=None)]}, None),
    ({"rows": [_row(image="OTHER")]}, None),
    ({}, None),
])
def test_validation_verdict(tmp_path, summary, want):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(summary))
    assert P.validation_verdict("IMG", p) == want


@needs_si
def test_usable_needs_build_base_gold_and_clean_on_one_image_id(tmp_path):
    s, r, out = tmp_path / "summary.json", tmp_path / "r", tmp_path / "data"
    t = "pro-ansible-4c5ce5a1"
    tag = _results(r, out, built=False)
    assert P.usable(t, r, s, out) == (False, "image not built")
    _results(r, out, bg_pass=False)
    assert not P.usable(t, r, s, out)[0]
    _results(r, out)
    ok, why = P.usable(t, r, s, out)
    assert not ok and "missing" in why
    s.write_text(json.dumps({"rows": [_row(image=tag)]}))
    assert P.usable(t, r, s, out) == (True, "ok")
    s.write_text(json.dumps({"rows": [_row(image=tag, image_id="sha256:other")]}))
    assert "missing" in P.usable(t, r, s, out)[1]  # CLEAN for another image id doesn't count
    s.write_text(json.dumps({"rows": [_row(image=tag, key="pro/instance_x")]}))
    assert "missing" in P.usable(t, r, s, out)[1]  # the upstream image's row doesn't count
    s.write_text(json.dumps({"rows": [_row(image=tag)]}))
    _results(r, out, bg_image="sha256:old")
    assert "not run on this image id" in P.usable(t, r, s, out)[1]
    _results(r, out, graded_by=None)  # a legacy row graded by tag (astra r3 #1)
    assert "not run on this image id" in P.usable(t, r, s, out)[1]
    _results(r, out, verifier="0" * 64)
    assert "different verifier" in P.usable(t, r, s, out)[1]
    _results(r, out)
    img = {"image": tag, "image_id": "sha256:img", "check_problems": ["2 refs"]}
    assert "image check" in P.usable(t, r, s, out, img=img)[1]


@needs_si
def test_load_task_gated_and_shaped(tmp_path):
    r, out = tmp_path / "r", tmp_path / "data"
    tag = _results(r, out)
    s = tmp_path / "summary.json"
    with pytest.raises(P.ProTaskNotUsable):
        P.load_task("pro-ansible-4c5ce5a1", results_dir=r, out_root=out, summary=s)
    s.write_text(json.dumps({"rows": [_row(image=tag)]}))
    t = families.load_task("pro-ansible-4c5ce5a1", results_dir=r, out_root=out, summary=s)
    from compaction.s2.interact_run import InteractTask
    assert isinstance(t, InteractTask)
    assert t.info.image == "sha256:img" and t.info.rank is None and t.info.language == "python"
    assert t.info.tests_dir == str(out / "pro-ansible-4c5ce5a1" / "tests")
    assert "a" * 40 in t.info.collect_command
    assert "/tmp/plan.md" in t.steps["01_plan"] and "/app/plan.md" not in t.steps["02_implement"]
    assert "Persona" in t.persona or len(t.persona) > 1000
    d = t.to_dict()
    assert d["image"] == "sha256:img" and d["solution_patch"].endswith("solution/solution.patch")
    with pytest.raises(KeyError):
        P.load_task("pro-nope-00000000", require_usable=False)


@needs_si
def test_resources_from_task_toml():
    r = P.resources("pro-ansible-4c5ce5a1")
    assert r == {"cpus": 4.0, "memory_mb": 8192, "storage_mb": 10240, "workdir": "/app",
                 "ready_file": None, "upstream": {"cpus": 1, "memory_mb": 4096, "storage_mb": 10240}}


# ------------------------------------------------------------------------------------------
# the families grade callable
# ------------------------------------------------------------------------------------------


class _Info:
    task_id = "pro-ansible-4c5ce5a1"
    image = "sha256:img"
    tests_dir = "/x/tests"


class _Task:
    task_id = "pro-ansible-4c5ce5a1"
    info = _Info()


REW = {"reward": 1, "f2p_total": 2, "f2p_passed": 2, "p2p_total": 4, "p2p_passed": 4}


class _FakeSandbox:
    """Answers the verifier's three execs: test.sh, the reward read, the OOM counter."""

    def __init__(self, oom: str, starts: list, kw: dict):
        self.oom = oom
        starts.append(kw)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def copy_in(self, *a):
        pass

    def put_file(self, *a):
        pass

    def exec(self, cmd, timeout_s=0, workdir="/"):
        from compaction.deepswe.sandbox import READ_REWARD_CMD, ExecResult
        if cmd == READ_REWARD_CMD:
            return ExecResult(0, "json\n" + json.dumps(REW), "", 0.1)
        if cmd == P.OOM_CMD:
            return ExecResult(0 if self.oom else 1, self.oom, "", 0.1)
        return ExecResult(0, "ok", "", 1.0)


def _starter(ooms: list[str]):
    starts: list = []

    def start(image, **kw):
        return _FakeSandbox(ooms[len(starts)], starts, {"image": image, **kw})
    return start, starts


class _Inv:
    def __init__(self, start):
        self.start = start


@pytest.mark.parametrize("task", [_Task(), _Info()])
def test_grade_via_inv_uses_the_shared_policy(monkeypatch, task):
    from compaction.s2 import outcomes as O
    monkeypatch.setattr(O, "grade_patch", lambda *a, **k: pytest.fail("would recurse"))
    start, starts = _starter(["0\n"])
    out = families.grade(task, b"p", "run", inv=_Inv(start))
    assert [(k["image"], k["cpus"], k["memory_mb"]) for k in starts] == [
        ("sha256:img", P.GRADE_CPUS, P.GRADE_MEMORY_MB)]
    assert out == {"reward": 1, "f2p": 1.0, "p2p": 1.0, "grade_error": None, **{
        k: REW[k] for k in ("f2p_passed", "f2p_total", "p2p_passed", "p2p_total")}}


def test_grade_reruns_an_oom_kill_once_at_16_gb():
    start, starts = _starter(["2\n", "0\n"])
    out = P.grade(_Task(), b"", "run", inv=_Inv(start))
    assert [k["memory_mb"] for k in starts] == [P.GRADE_MEMORY_MB, P.OOM_RETRY_MEMORY_MB]
    assert out["reward"] == 1
    start, starts = _starter(["1\n", "3\n"])
    out = P.grade(_Task(), b"", "run", inv=_Inv(start))
    assert out["reward"] is None and "OOM-killed at 16384 MB" in out["grade_error"]
    assert out["attempts"] == [{"memory_mb": 8192, "oom_kills": 1},
                               {"memory_mb": 16384, "oom_kills": 3}]


def test_grade_unknown_oom_state_is_an_infrastructure_failure():
    start, starts = _starter([""])
    out = P.grade(_Task(), b"", "run", inv=_Inv(start))
    assert out["reward"] is None and "OOM state unknown" in out["grade_error"]
    assert len(starts) == 1


def test_grade_without_inv_uses_the_desktop_sandbox(monkeypatch):
    from compaction.deepswe import sandbox as SBX
    start, _ = _starter(["0\n"])

    def fake_start(image, purpose, **kw):
        assert purpose == P.C.PURPOSE and kw.pop("host") == P.DESKTOP_HOST
        kw.pop("run_id")
        return start(image, **kw)
    monkeypatch.setattr(SBX.Sandbox, "start", staticmethod(fake_start))
    assert P.grade(_Task(), b"", "run")["reward"] == 1


def test_gold_added_dependencies():
    patch = ("diff --git a/go.mod b/go.mod\n--- a/go.mod\n+++ b/go.mod\n@@ -1 +1,3 @@\n"
             " module x\n+require github.com/a/b v1.2.3\n+// comment\n"
             "diff --git a/main.go b/main.go\n--- a/main.go\n+++ b/main.go\n@@ -1 +1 @@\n"
             "+import \"github.com/a/b\"\n"
             "diff --git a/requirements_scripts.txt b/requirements_scripts.txt\n"
             "--- a/requirements_scripts.txt\n+++ b/requirements_scripts.txt\n@@ -1 +1,2 @@\n"
             "+nameparser==1.1.3\n")
    src = P.ProSource("pro-x-1", Path("."), {"patch": patch})
    assert P.gold_added_dependencies(src) == {"go.mod": ["require github.com/a/b v1.2.3"],
                                              "requirements_scripts.txt": ["nameparser==1.1.3"]}
