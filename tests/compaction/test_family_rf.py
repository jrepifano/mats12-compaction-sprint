"""compaction.s2.families.rf: sources, image layers, the tests-only grader (run end to end
with bash on a scratch git repo), list derivation, usability and the families registry. No
Docker."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from compaction.deepswe import sandbox as SBX
from compaction.s2 import families
from compaction.s2.families import rf as R

TID = "rf_task-0123456789abcdef01234567"
MV = textwrap.dedent("""\
    #!/bin/bash
    set -e
    ARTIFACT_DIR="/tmp_artifact_storage"
    log() { echo "[x] $*"; }
    detect_build_system() { DETECTED_BUILD_SYSTEM=none; }
    build_project() { detect_build_system; [ -f BREAK_BUILD ] && exit 1; return 0; }
    parse_args() { :; }
    parse_args "$@"
    # ---------------------------------------------------------
    # Header
    # ---------------------------------------------------------
    echo "main part must not run"
    exit 3
    """)
# A run_script in upstream's format: test_a passes iff lib.txt says "new" (the refactor),
# test_b passes always, test_new exists only once test_patch added tests/new_test.txt.
RUN_SCRIPT = textwrap.dedent("""\
    #!/bin/bash
    OUT="$1"; shift
    echo "files: $*" >&2
    P=(); F=()
    if grep -q new lib.txt; then P+=(test_a); else F+=(test_a); fi
    P+=(test_b)
    if [ -f tests/new_test.txt ]; then
        if grep -q new lib.txt; then P+=(test_new); else F+=(test_new); fi
    fi
    j() { local s=""; for n in "$@"; do s="$s{\\"name\\": \\"$n\\"},"; done; echo "[${s%,}]"; }
    echo "{\\"passed_tests\\": $(j "${P[@]}"), \\"failed_tests\\": $(j "${F[@]}")}" > "$OUT"
    """)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _diff(repo: Path, edit) -> str:
    """The git diff `edit(repo)` makes, then the repo restored."""
    edit(repo)
    _git(repo, "add", "-A")
    d = _git(repo, "diff", "--cached", "HEAD")
    _git(repo, "reset", "-q", "--hard")
    _git(repo, "clean", "-fdq")
    return d


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A scratch repo (the task's 'image') and a SWE-Interact RF task dir for it."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # git config --global stays here
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "lib.txt").write_text("old\n")
    (repo / "tests" / "a_test.txt").write_text("a\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "add", "-A")
    _git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "init")
    gold = _diff(repo, lambda r: (r / "lib.txt").write_text("new\n"))
    test_patch = _diff(repo, lambda r: (r / "tests" / "new_test.txt").write_text("n\n"))
    si = tmp_path / "si"
    d = si / TID
    for sub in ("tests", "solution", "steps/01_plan", "steps/02_implement",
                "environment/user-server"):
        (d / sub).mkdir(parents=True)
    (d / "task.toml").write_text(textwrap.dedent(f"""\
        [environment]
        docker_image = "ghcr.io/x/swe-atlas:rf@sha256:{'a' * 64}"
        cpus = 16
        memory_mb = 16384
        storage_mb = 20480
        """))
    (d / "tests" / "config.json").write_text(json.dumps(
        {"task_id": TID[8:], "repo": "acme_widget", "language": "Go",
         "mount_path": "/workspace"}))
    (d / "tests" / "test_files.json").write_text(json.dumps(
        {"files": [{"name": "tests/a_test.txt"}]}))
    (d / "tests" / "relevant_file_names.json").write_text(json.dumps(
        {"files": [{"name": "tests/a_test.txt"}]}))
    (d / "tests" / "relevant_test_names.json").write_text(json.dumps(
        {"tests": [{"name": "test_a"}, {"name": "test_new"}]}))
    (d / "tests" / "test_patch.diff").write_text(test_patch)
    (d / "tests" / "run_script.sh").write_text(RUN_SCRIPT)
    (d / "tests" / "master_validator_script.sh").write_text(MV)
    (d / "solution" / "gold.patch").write_text(gold)
    (d / "instruction.md").write_text(
        "Refactor the widget. Keep the public API stable.\n\n"
        "I've already taken care of all changes to the test files.\n\n"
        "- Do NOT modify any test files or testing logic in any way.\n")
    for s in R.STEPS:
        (d / "steps" / s / "instruction.md").write_text(f"step {s} at `/workspace`\n")
    (d / "environment" / "user-server" / "persona.md").write_text("persona\n")
    return {"repo": repo, "si": si, "src": R.sources(si)[TID], "gold": gold,
            "tmp": tmp_path}


def grade_local(world, patch: str, lists: dict | None, path: str | None = None) -> dict:
    """Run TEST_SH with bash against a fresh copy of the scratch repo."""
    tmp = world["tmp"]
    run = tmp / f"run{len(list(tmp.glob('run*')))}"
    work = run / "work"
    shutil.copytree(world["repo"], work)
    d = R.materialize(world["src"], path or os.environ["PATH"], lists, out_root=run / "data")
    t = d / "tests"
    (t / "emx_rf.env").write_text(R.env_file(str(work), path or os.environ["PATH"]))
    (run / "model.patch").write_text(patch)
    env = {**os.environ, "EMX_TESTS_DIR": str(t), "EMX_VERIFIER_DIR": str(run / "v"),
           "EMX_ART_DIR": str(run / "a"), "EMX_PATCH": str(run / "model.patch")}
    p = subprocess.run(["bash", str(t / "test.sh")], env=env, capture_output=True, text=True,
                       timeout=60, check=False)
    assert p.returncode == 0, p.stderr
    det = json.loads((run / "v" / "emx_rf_detail.json").read_text())
    rw = run / "v" / "reward.json"
    det["reward_file"] = rw.read_text() if rw.exists() else None
    det["work"] = work
    return det


# ------------------------------------------------------------------------------------------


def test_sources_and_task_facts(world):
    src = world["src"]
    assert (src.repo, src.language, src.mount_path, src.heavy) == (
        "acme_widget", "Go", "/workspace", False)
    assert src.upstream_image.startswith("ghcr.io/x/swe-atlas:rf@sha256:")
    assert src.test_files() == ["tests/a_test.txt", "tests/new_test.txt"]
    assert src.relevant_tests() == ["test_a", "test_new"]
    assert R.image_tag(TID) == "emx-tasks/swe-interact-rf:0123456789abcdef01234567-v1"


def test_real_rf_tasks_load():
    srcs = R.sources()
    if not srcs:
        pytest.skip("SWE-Interact checkout not present")
    assert len(srcs) == 25
    assert {s.mount_path for s in srcs.values()} >= {"/workspace", "/app", "/grafana"}
    assert sum(not s.harness_ok() for s in srcs.values()) == 2  # /app/suricata, /app/source
    assert all(s.relevant_files() and s.test_patch() for s in srcs.values())


@pytest.mark.parametrize(("mount", "ok"), [("/app", True), ("/workspace", True),
                                           ("/go/src/go.k6.io/k6", True),
                                           ("/app/suricata", False), ("/app/source", False),
                                           ("/apple", True)])
def test_harness_ok(world, mount, ok):
    src = R.RfSource(TID, Path("."), {"mount_path": mount, "repo": "r", "language": "Go"}, {})
    assert src.harness_ok() is ok


def test_stage_script_symlink_rules():
    s = R.stage_script("/workspace")
    assert "rmdir /app" in s and "ln -s '/workspace' /app" in s and "go mod download" in s
    assert "ln -s" not in R.stage_script("/app")
    assert "ln -s" not in R.stage_script("/app/source")
    for bad in ("/work'space", "relative", "/a$b"):
        with pytest.raises(ValueError):
            R.stage_script(bad)


def test_stage_script_runs_in_sh(tmp_path):
    """The stage script under /bin/sh with /app replaced by a temp path: an empty dir is
    replaced by the link, a non-empty one (netdata) is left alone; an upstream tree that is
    already dirty (suricata) is fine as long as the script leaves it as it was."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    app = tmp_path / "app"
    app.mkdir()
    script = R.stage_script(str(repo)).replace(R.WORKSPACE, str(app))
    script = script.replace(f"'{app}'", f"'{repo}'")  # the mount path itself is not /app
    assert subprocess.run(["sh", "-c", script], check=False).returncode == 0
    assert app.is_symlink() and app.resolve() == repo.resolve()
    app.unlink()
    app.mkdir()
    (app / "x").write_text("x")
    (repo / "dirty.txt").write_text("upstream left this\n")
    assert subprocess.run(["sh", "-c", script], check=False).returncode == 0
    assert not app.is_symlink() and (app / "x").exists()


def test_pin_script_and_commit_changes(tmp_path):
    script = R.pin_script("/usr/local/go/bin:/usr/bin")
    assert script.endswith(R.PROFILE_D)
    out = tmp_path / "pin.sh"
    subprocess.run(["sh", "-c", script.replace(R.PROFILE_D, str(out))], check=True)
    assert out.read_text() == 'export PATH="/usr/local/go/bin:/usr/bin"\nexport GOTOOLCHAIN=auto\n'
    with pytest.raises(ValueError):
        R.pin_script("/bin'; rm -rf /")
    ch = R.commit_changes(TID, {"Cmd": ["sleep", "infinity"], "Entrypoint": None,
                                "WorkingDir": "/workspace"})
    assert ch[:3] == ['CMD ["sleep", "infinity"]', "ENTRYPOINT []", "WORKDIR /workspace"]
    assert 'emx.sandbox=""' in ch[3] and 'emx.run=""' in ch[3] and TID in ch[3]
    assert R.commit_changes(TID, {})[2] == "WORKDIR /"


def test_mv_lib_cuts_main_and_set_e():
    lib = R.mv_lib(MV)
    assert "set -e" not in lib.splitlines() and "main part" not in lib
    assert "build_project()" in lib
    with pytest.raises(ValueError):
        R.mv_lib("#!/bin/bash\nfoo() { :; }\n")


def test_mv_lib_of_real_validator_sources_cleanly(tmp_path):
    srcs = R.sources()
    if not srcs:
        pytest.skip("SWE-Interact checkout not present")
    lib = R.mv_lib(next(iter(srcs.values())).tests.joinpath(
        "master_validator_script.sh").read_text())
    (tmp_path / "lib.sh").write_text(lib)
    cmd = f". {tmp_path / 'lib.sh'}; type build_project detect_build_system >/dev/null && echo OK"
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd=tmp_path,
                       check=False)
    assert p.stdout.strip().endswith("OK"), p.stderr


# ------------------------------------------------------------------------------------------
# Grader, end to end with bash


def test_record_mode_then_lists_base_fails_gold_passes(world):
    base = grade_local(world, "", None)
    gold = grade_local(world, world["gold"], None)
    assert base["status"] == gold["status"] == "ok"
    assert base["reward"] is None and base["reward_file"] is None
    assert base["passed"] == ["test_b"] and sorted(base["failed"]) == ["test_a", "test_new"]
    lists = R.derive_lists(base, gold)
    assert lists == {"f2p": ["test_a", "test_new"], "p2p": ["test_b"], "flaky": []}
    gb = grade_local(world, "", lists)
    gg = grade_local(world, world["gold"], lists)
    rb = SBX.parse_reward("json", gb["reward_file"])
    rg = SBX.parse_reward("json", gg["reward_file"])
    assert (rb["reward"], rb["f2p_passed"], rb["p2p_passed"]) == (0, 0, 1)
    assert (rg["reward"], rg["f2p_passed"], rg["f2p_total"]) == (1, 2, 2)


def test_test_file_edits_are_dropped_and_test_patch_still_applies(world):
    lists = {"f2p": ["test_a", "test_new"], "p2p": ["test_b"]}
    # the agent "solves" it and also edits a protected test file and the test_patch's file
    gold_plus = _diff(world["repo"], lambda r: (
        (r / "lib.txt").write_text("new\n"), (r / "tests" / "a_test.txt").write_text("hack\n"),
        (r / "tests" / "new_test.txt").write_text("agent's\n")))
    d = grade_local(world, gold_plus, lists)
    assert d["status"] == "ok" and json.loads(d["reward_file"])["reward"] == 1
    assert (d["work"] / "tests" / "a_test.txt").read_text() == "a\n"
    assert (d["work"] / "tests" / "new_test.txt").read_text() == "n\n"


def test_patch_that_does_not_apply_scores_zero(world):
    lists = {"f2p": ["test_a"], "p2p": ["test_b"]}
    bad = world["gold"].replace("-old", "-something else")
    d = grade_local(world, bad, lists)
    assert d["status"] == "apply_failed"
    r = SBX.parse_reward("json", d["reward_file"])
    assert (r["reward"], r["f2p_passed"], r["p2p_passed"], r["f2p_total"]) == (0, 0, 0, 1)


def test_build_failure_scores_zero(world):
    lists = {"f2p": ["test_a"], "p2p": ["test_b"]}
    breaking = _diff(world["repo"], lambda r: (
        (r / "lib.txt").write_text("new\n"), (r / "BREAK_BUILD").write_text("")))
    d = grade_local(world, breaking, lists)
    assert d["status"] == "build_failed"
    assert json.loads(d["reward_file"])["reward"] == 0


def test_score_run_cases():
    lists = {"f2p": ["a", "n"], "p2p": ["b"]}
    ok = R.score_run("ok", {"passed_tests": ["a", {"name": "n"}, "b"]}, lists)
    assert ok["reward"]["reward"] == 1
    miss = R.score_run("ok", {"passed_tests": ["a", "b"], "failed_tests": ["n"]}, lists)
    assert miss["reward"]["reward"] == 0 and miss["f2p_not_passed"] == ["n"]
    p2p_break = R.score_run("ok", {"passed_tests": ["a", "n"]}, lists)
    assert p2p_break["reward"]["reward"] == 0 and p2p_break["p2p_not_passed"] == ["b"]
    failed_run = R.score_run("build_failed", {"passed_tests": ["a", "n", "b"]}, lists)
    assert failed_run["reward"]["reward"] == 0 and failed_run["reward"]["f2p_passed"] == 0
    # reported passed in one pass and failed in another: failed (astra r1 #1)
    both = R.score_run("ok", {"passed_tests": ["a", "n", "b"], "failed_tests": ["n"]}, lists)
    assert both["reward"]["reward"] == 0 and both["f2p_not_passed"] == ["n"]
    assert "n" not in both["passed"] and R.derive_lists(both, both)["f2p"] == []
    empty = R.score_run("ok", {"passed_tests": ["b"]}, {"f2p": [], "p2p": ["b"]})
    assert empty["reward"]["reward"] == 0  # no f2p: never "solved" (parse_reward agrees)
    for d in (ok, miss, p2p_break, failed_run):
        SBX.parse_reward("json", json.dumps(d["reward"]))


def test_score_script_is_fixed_at_import():
    assert R.score_script().count("def score_run(") == 1
    assert "def score_run(" in R._SCORE_RUN_SRC and "failed in any pass" in R._SCORE_RUN_SRC


def test_score_script_runs_standalone(tmp_path):
    (tmp_path / "s.py").write_text(R.score_script())
    (tmp_path / "res.json").write_text(json.dumps({"passed_tests": [{"name": "a"}]}))
    (tmp_path / "lists.json").write_text(json.dumps({"f2p": ["a"], "p2p": []}))
    p = subprocess.run([sys.executable, "-I", str(tmp_path / "s.py"), "ok",
                        str(tmp_path / "res.json"), str(tmp_path / "lists.json"),
                        str(tmp_path)], capture_output=True, text=True, check=False)
    assert p.returncode == 0, p.stderr
    assert json.loads((tmp_path / "reward.json").read_text())["reward"] == 1
    (tmp_path / "res.json").write_text("{not json")
    subprocess.run([sys.executable, "-I", str(tmp_path / "s.py"), "ok",
                    str(tmp_path / "res.json"), str(tmp_path / "lists.json"), str(tmp_path)],
                   check=True, capture_output=True)
    assert json.loads((tmp_path / "emx_rf_detail.json").read_text())["status"] == "bad_results"


def test_derive_lists_drops_flaky():
    base = {"passed": ["b", "c"]}
    gold = {"passed": ["a", "b", "c", "x"]}
    gold2 = {"passed": ["a", "b", "y"]}
    assert R.derive_lists(base, gold, gold2) == {"f2p": ["a"], "p2p": ["b"],
                                                 "flaky": ["c", "x", "y"]}


def test_materialize_record_mode_removes_stale_lists(world, tmp_path):
    out = tmp_path / "out"
    d = R.materialize(world["src"], "/usr/bin:/bin", {"f2p": ["a"], "p2p": []}, out_root=out)
    assert (d / "tests" / "emx_rf_lists.json").exists()
    assert (d / "solution" / "solution.patch").read_text() == world["gold"]
    assert os.access(d / "tests" / "test.sh", os.X_OK)
    R.materialize(world["src"], "/usr/bin:/bin", None, out_root=out)
    assert not (d / "tests" / "emx_rf_lists.json").exists()
    assert (d / "tests" / "emx_rf.env").read_text() == (
        'EMX_WORKDIR="/workspace"\nexport PATH="/usr/bin:/bin"\nexport GOTOOLCHAIN=auto\n')


def test_env_file_refuses_unsafe_values():
    for w, p in (('/a"b', "/bin"), ("/a", "/bin:$HOME"), ("/a", "/bin`x`")):
        with pytest.raises(ValueError):
            R.env_file(w, p)


def test_preservation_rules_skip_boilerplate(world):
    rules = R.preservation_rules(R.task_block(world["src"]))
    assert rules == ["Keep the public API stable."]


# ------------------------------------------------------------------------------------------
# Usability, imgval verdicts, loader, registry


IID = "sha256:" + "c" * 64


def _records(tmp: Path, *, passed=True, harness_ok=True, bg_image_id=IID) -> Path:
    res = tmp / "results"
    (res / "grading").mkdir(parents=True)
    (res / "images.json").write_text(json.dumps({"tasks": {TID: {
        "image": R.image_tag(TID), "image_id": IID, "upstream_image": "up@sha256:1",
        "path": "/usr/bin:/bin", "harness_ok": harness_ok}}}))
    (res / "base_gold.json").write_text(json.dumps({"tasks": {TID: {
        "pass": passed, "reason": "ok" if passed else "gold reward 0", "image_id": bg_image_id,
        "resources": {"cpus": 2, "memory_mb": 8192}}}}))
    (res / "grading" / f"{TID}.json").write_text(json.dumps({"f2p": ["a"], "p2p": []}))
    return res


def _summary(tmp: Path, rows: list[dict]) -> Path:
    p = tmp / "summary.json"
    p.write_text(json.dumps({"rows": rows}))
    return p


CLEAN = {"verdict": "CLEAN", "layers": "CLEAN", "hidden_tests": "CLEAN"}


def test_validation_verdict_needs_upstream_and_derived_clean(tmp_path):
    up = {"key": "rf/" + TID[3:], "image": "up@sha256:1", **CLEAN}
    der = {"key": "rf-derived/x", "image": R.image_tag(TID), "image_id": IID, **CLEAN}
    imgs = ["up@sha256:1", IID]
    assert R.validation_verdict(TID, imgs, _summary(tmp_path, [up, der])) == "CLEAN"
    assert R.validation_verdict(TID, imgs, _summary(tmp_path, [up])) is None
    assert R.validation_verdict(TID, imgs, _summary(tmp_path, [
        up, {**der, "hidden_tests": "LEAK"}])) == "LEAK"
    assert R.validation_verdict(TID, imgs, _summary(tmp_path, [
        {**up, "layers": None}, der])) is None
    # upstream row found by key when its image field is written differently
    assert R.validation_verdict(TID, imgs, _summary(tmp_path, [
        {**up, "image": "ghcr.io/other"}, der])) == "CLEAN"
    assert R.validation_verdict(TID, imgs, tmp_path / "missing.json") is None
    # a verdict for an earlier build of the same tag is not taken
    assert R.validation_verdict(TID, imgs, _summary(tmp_path, [
        up, {**der, "image_id": "sha256:" + "d" * 64}])) is None


def test_usable_and_load_task(world, tmp_path):
    res = _records(tmp_path)
    good = _summary(tmp_path, [{"key": "rf/" + TID[3:], "image": "up@sha256:1", **CLEAN},
                               {"image": R.image_tag(TID), "image_id": IID, **CLEAN}])
    assert R.usable(TID, res, good) == (True, "ok")
    assert R.usable(TID, res, tmp_path / "none.json")[0] is False
    out = tmp_path / "data"
    t = R.load_task(TID, root=world["si"], results_dir=res, out_root=out, summary=good)
    assert t.task_id == TID and t.info.image == IID
    assert t.steps["02_implement"].startswith("step 02_implement")
    assert t.persona == "persona\n" and t.info.solution_files == ["lib.txt"]
    assert Path(t.info.tests_dir, "emx_rf_lists.json").exists()
    assert "base_commit.txt" in t.info.collect_command
    assert t.to_dict()["solution_patch"].endswith("solution/solution.patch")
    with pytest.raises(R.RfTaskNotUsable):
        R.load_task(TID, root=world["si"], results_dir=_records(tmp_path / "b", passed=False),
                    out_root=out, summary=good)
    stale = _records(tmp_path / "d", bg_image_id="sha256:" + "e" * 64)
    assert "different image id" in R.usable(TID, stale, good)[1]
    with pytest.raises(R.RfTaskNotUsable):
        R.load_task(TID, root=world["si"], results_dir=_records(tmp_path / "c",
                                                                harness_ok=False),
                    out_root=out, summary=good)


def test_resources_shape(world, tmp_path):
    res = _records(tmp_path)
    assert R.resources(TID, res, root=world["si"]) == {
        "cpus": 2, "memory_mb": 8192, "storage_mb": 20480, "workdir": "/workspace",
        "ready_file": None, "ready_cmd": None}


def test_registry_routes_rf_ids():
    assert families.family_of(TID) == "compaction.s2.families.rf"
    assert families.family_of("abc123-deepswe-task") is None
    assert families.module_for(TID) is R
    with pytest.raises(KeyError):
        families.module_for("nope")


class _FakeExec:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.rc, self.stdout, self.stderr, self.seconds = rc, stdout, stderr, 1.0

    @property
    def timed_out(self):
        return self.rc in (124, 137)


class _FakeSandbox:
    def __init__(self, reward: dict):
        self.reward, self.calls = reward, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("closed")

    def copy_in(self, src, dest):
        self.calls.append(("copy_in", dest))

    def put_file(self, data, path):
        self.calls.append(("put_file", path))

    def exec(self, cmd, timeout_s=600, workdir="/app"):
        self.calls.append(("exec", cmd[:30], timeout_s, workdir))
        if cmd == SBX.READ_REWARD_CMD:
            return _FakeExec(0, "json\n" + json.dumps(self.reward))
        if cmd == R.READ_DETAIL_CMD:
            return _FakeExec(0, json.dumps({"status": "ok"}))
        return _FakeExec(0, "")


class _FakeInv:
    purpose = "s2-interact"
    host = R.DESKTOP_HOST

    def __init__(self, sb):
        self.sb, self.kw = sb, None

    def start(self, image, **kw):
        self.image, self.kw = image, kw
        return self.sb


def test_grade_through_the_harness_invocation(world, tmp_path):
    """families.grade -> rf.grade: through the run's inv, with the task's resources and
    timeout, returning grade_patch's shape (astra r1 #3)."""
    res = _records(tmp_path)
    rew = {"reward": 1, "f2p_total": 2, "f2p_passed": 2, "p2p_total": 1, "p2p_passed": 1}
    sb = _FakeSandbox(rew)
    inv = _FakeInv(sb)

    class Info:
        task_id, image, tests_dir = TID, IID, str(tmp_path)

    import unittest.mock as um
    with um.patch.object(R, "sources", return_value={TID: world["src"]}):
        out = R.grade(Info(), b"patch", "run1", inv=inv, results_dir=res)
    assert out == {"reward": 1, "f2p": 1.0, "p2p": 1.0, "grade_error": None,
                   "f2p_passed": 2, "f2p_total": 2, "p2p_passed": 1, "p2p_total": 1}
    assert inv.image == IID
    assert inv.kw == {"lifetime_s": R.GRADE_TIMEOUT_S + 600, "cpus": 2, "memory_mb": 8192,
                      "platform": "linux/amd64"}
    assert ("exec", "chmod +x /tests/test.sh && bas", R.GRADE_TIMEOUT_S, "/app") in sb.calls
    assert sb.calls[-1] == "closed"
    # the registry dispatches on the task id; a bad reward becomes grade_error, not a crash
    sb.reward = {"reward": 1, "f2p_total": 0, "f2p_passed": 0, "p2p_total": 0, "p2p_passed": 0}
    with um.patch.object(R, "sources", return_value={TID: world["src"]}):
        bad = families.grade(Info(), b"patch", "run2", inv=inv, results_dir=res)
    assert bad["reward"] is None and "inconsistent" in bad["grade_error"]


def test_grade_refuses_other_hosts():
    class Info:
        task_id, image, tests_dir = TID, IID, "/nonexistent"

    inv = _FakeInv(_FakeSandbox({}))
    inv.host = None  # the laptop's daemon
    out = R.grade(Info(), b"", "r", inv=inv)
    assert out["reward"] is None and "only on" in out["grade_error"] and inv.kw is None
    assert R.grade(Info(), b"", "r", host="ssh://other")["reward"] is None


def test_memory_events_and_oom_suspect():
    ev = R.memory_events(0, "low 0\nhigh 0\nmax 12\noom 1\noom_kill 1\noom_group_kill 0\n", "")
    assert ev == {"oom_kills": 1, "container_gone": False}
    assert R.oom_suspect(0, {"status": "ok", **ev})
    clean = R.memory_events(0, "oom 0\noom_kill 0\n", "")
    assert not R.oom_suspect(0, {"status": "ok", **clean})
    gone = R.memory_events(1, "", "Error response from daemon: No such container: fam-rf-x")
    assert gone["container_gone"] and R.oom_suspect(137, {**gone})
    # killed with no scorer output, cgroup unreadable: suspect; killed but scored: not
    assert R.oom_suspect(137, {"status": None, "oom_kills": None})
    assert not R.oom_suspect(137, {"status": "ok", "oom_kills": 0})


FAKE_GO = r"""#!/bin/bash
# fake go: `go list -m` and `go test -json ... -run RX ... PKG` for the go-relevant runner test
if [ "$1" = list ]; then echo example.com/m; exit 0; fi
echo "$@" >> "$FAKE_GO_LOG"
rx=""; pkg="${@: -1}"
while [ $# -gt 0 ]; do [ "$1" = -run ] && rx="$2"; shift; done
p="example.com/m/${pkg#./}"
crash=0
for t in TestA TestB TestPanicky BenchmarkB BenchmarkC; do
  if echo "$t" | grep -Eq "$rx"; then
    echo "{\"Action\":\"run\",\"Package\":\"$p\",\"Test\":\"$t\"}"
    case $t in
      TestB) echo "{\"Action\":\"fail\",\"Package\":\"$p\",\"Test\":\"$t\"}";;
      BenchmarkB) echo "{\"Action\":\"output\",\"Package\":\"$p\",\"Test\":\"$t\",\"Output\":\"BenchmarkB-8 \\t 1\\t 123 ns/op\\n\"}";;
      BenchmarkC) crash=1;;
      *) echo "{\"Action\":\"pass\",\"Package\":\"$p\",\"Test\":\"$t\"}";;
    esac
  fi
done
[ $crash = 1 ] && echo "{\"Action\":\"fail\",\"Package\":\"$p\"}"
exit 0
"""


def test_go_relevant_runner_runs_only_relevant_tests(tmp_path, world):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "go").write_text(FAKE_GO)
    (bin_ / "go").chmod(0o755)
    t = tmp_path / "tests"
    t.mkdir()
    (t / "relevant_test_names.json").write_text(json.dumps({"tests": [
        {"name": "example.com/m/pkg/a/TestA"}, {"name": "example.com/m/pkg/a/TestB/sub_1"},
        {"name": "example.com/m/pkg/b/BenchmarkB"}, {"name": "example.com/m/pkg/b/BenchmarkC"},
        {"name": "other.org/x/TestZ"}]}))
    (tmp_path / "run.sh").write_text(R.GO_RELEVANT_RUN_SH)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "EMX_TESTS_DIR": str(t),
           "FAKE_GO_LOG": str(tmp_path / "go.log")}
    p = subprocess.run(["bash", str(tmp_path / "run.sh"), str(tmp_path / "out.json")],
                       env=env, capture_output=True, text=True, check=False, timeout=60)
    assert p.returncode == 0, p.stderr
    calls = (tmp_path / "go.log").read_text().splitlines()
    assert len(calls) == 2 and "^(TestA|TestB)$" in calls[0] and calls[0].endswith("./pkg/a")
    assert "^(BenchmarkB|BenchmarkC)$" in calls[1] and calls[1].endswith("./pkg/b")
    out = json.loads((tmp_path / "out.json").read_text())
    d = R.score_run("ok", out, None)
    assert d["passed"] == ["example.com/m/pkg/a/TestA", "example.com/m/pkg/b/BenchmarkB"]
    # TestPanicky never ran; BenchmarkC started but its package then failed (astra r2 #3)
    assert d["failed"] == ["example.com/m/pkg/a/TestB", "example.com/m/pkg/b/BenchmarkC"]


def test_materialize_runner_swap(world, tmp_path):
    d = R.materialize(world["src"], "/bin", None, out_root=tmp_path, runner="go-relevant")
    assert (d / "tests" / "run_script.sh").read_text() == R.GO_RELEVANT_RUN_SH
    assert (d / "tests" / "run_script.upstream.sh").read_text() == RUN_SCRIPT
    R.materialize(world["src"], "/bin", None, out_root=tmp_path)
    assert (d / "tests" / "run_script.sh").read_text() == RUN_SCRIPT
    assert not (d / "tests" / "run_script.upstream.sh").exists()
    with pytest.raises(ValueError):
        R.materialize(world["src"], "/bin", None, out_root=tmp_path, runner="nope")


def test_cleanup_script_and_refusals(tmp_path):
    a, b = tmp_path / "build", tmp_path / "bin_x"
    (a / "sub").mkdir(parents=True)
    b.write_text("x")
    subprocess.run(["bash", "-c", R.cleanup_script([str(a), str(b)])], check=True)
    assert not a.exists() and not b.exists()
    for bad in (["/"], ["/app"], ["relative"], ["/opt/../etc"], ["/a'b"]):
        with pytest.raises(ValueError):
            R.cleanup_script(bad)
    assert all(p.startswith("/") for v in R.CLEANUP.values() for p in v)


def test_import_changes_restore_config():
    ch = R.import_changes(TID, {"Env": ["PATH=/usr/bin:/bin", "GOFLAGS=-mod=vendor -x"],
                                "Cmd": None, "Entrypoint": ["/bin/sh"], "WorkingDir": "/w"})
    assert ch[:2] == ['ENV PATH="/usr/bin:/bin"', 'ENV GOFLAGS="-mod=vendor -x"']
    assert f"ENTRYPOINT {json.dumps(R.entrypoint(['/bin/sh']))}" in ch
    assert "WORKDIR /w" in ch and "CMD []" in ch
    assert 'emx.flattened="1"' in ch[-1]


def test_flattened_task_needs_only_its_own_verdict(world, tmp_path):
    res = _records(tmp_path)
    m = json.loads((res / "images.json").read_text())
    m["tasks"][TID]["flattened"] = True
    (res / "images.json").write_text(json.dumps(m))
    up_leak = {"key": "rf/" + TID[3:], "image": "up@sha256:1", **CLEAN, "layers": "LEAK"}
    der = {"image": R.image_tag(TID), "image_id": IID, **CLEAN}
    assert R.usable(TID, res, _summary(tmp_path, [up_leak, der])) == (True, "ok")
    assert not R.usable(TID, res, _summary(tmp_path, [up_leak]))[0]


def _fake_proc(tmp_path, start: int, cmdline: str = "") -> dict:
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "timeout").write_text('#!/bin/bash\nshift 3\nexec "$@"\n')  # -k 10 <s> cmd...
    (bin_ / "timeout").chmod(0o755)
    stat = tmp_path / "stat"
    stat.write_text("1 (bash) " + " ".join(["S", *map(str, range(18)), str(start)]) + "\n")
    cmd = tmp_path / "cmdline"
    cmd.write_text(cmdline.replace(" ", "\0"))
    return {"bin": bin_, "stat": stat, "cmdline": cmd, "rc": tmp_path / "rc"}


def _local(script: str, fp: dict) -> str:
    return (script.replace("/proc/1/stat", str(fp["stat"]))
            .replace("/proc/1/cmdline", str(fp["cmdline"]))
            .replace(R.ENTRYPOINT_RC, str(fp["rc"])))


def test_entrypoint_wrapper_survives_a_failing_entrypoint(tmp_path):
    fp = _fake_proc(tmp_path, 777)
    ep = tmp_path / "entrypoint.sh"
    ep.write_text('#!/bin/bash\nset -e\necho setup-ran > "$MARK"\nfalse\nexec "$@"\n')
    ep.chmod(0o755)
    wrap = R.entrypoint([str(ep)])
    assert wrap[:2] == ["/bin/bash", "-c"] and wrap[3] == str(ep)
    p = subprocess.run(["/bin/bash", "-c", _local(wrap[2], fp), str(ep), "echo", "command-ran"],
                       env={**os.environ, "MARK": str(tmp_path / "mark"),
                            "PATH": f"{fp['bin']}:{os.environ['PATH']}"},
                       capture_output=True, text=True, timeout=30, check=False)
    assert p.returncode == 0 and p.stdout.strip() == "command-ran", p.stderr
    assert fp["rc"].read_text() == "777 1\n" and (tmp_path / "mark").exists()
    assert "\\" not in wrap[2]  # docker import --change would unescape it
    assert R.entrypoint(None) == [] and R.entrypoint([]) == []
    with pytest.raises(ValueError):
        R.entrypoint(["/a", "/b"])


def test_config_diff_allows_only_the_wrapper_and_our_labels():
    up = {"Env": ["PATH=/bin"], "Cmd": ["bash"], "Entrypoint": ["/entrypoint.sh"],
          "WorkingDir": "/code", "User": "", "ExposedPorts": {"80/tcp": {}}, "Volumes": None,
          "Labels": {"maintainer": "x"}}
    ours = {**up, "Entrypoint": R.entrypoint(["/entrypoint.sh"]),
            "Labels": {"maintainer": "x", "emx.family": "rf", "emx.sandbox": ""}}
    assert R.config_diff(up, ours) == {}
    assert set(R.config_diff(up, {**ours, "WorkingDir": "/app", "ExposedPorts": None})) == {
        "WorkingDir", "ExposedPorts"}
    assert "Entrypoint" in R.config_diff(up, {**ours, "Entrypoint": ["/entrypoint.sh"]})
    ch = R.import_changes(TID, {**up, "User": "app", "Volumes": {"/data": {}}})
    assert "USER app" in ch and "EXPOSE 80/tcp" in ch and 'VOLUME "/data"' in ch
    assert 'LABEL maintainer="x"' in ch


def test_resources_reports_ready_file(world, tmp_path):
    res = _records(tmp_path)
    m = json.loads((res / "images.json").read_text())
    m["tasks"][TID]["entrypoint_wrapped"] = True
    (res / "images.json").write_text(json.dumps(m))
    assert R.resources(TID, res, root=world["si"])["ready_file"] == R.ENTRYPOINT_RC


def test_ready_check_matches_this_container_start(tmp_path):
    """A setup record carried in by `docker commit` names an earlier PID 1 start, so it never
    counts; the check fails closed when setup doesn't finish (astra r2 #2, s2-feas)."""
    marker = "/bin/bash -c ...emx_entrypoint_rc... /entrypoint.sh timeout sleep"
    fast = R.READY_SH.replace(f"seq 1 {R.ENTRYPOINT_SETUP_TIMEOUT_S + 300}", "seq 1 2")

    def ready(fp) -> int:
        return subprocess.run(["bash", "-c", _local(fast, fp)], timeout=30,
                              check=False).returncode

    (tmp_path / "a").mkdir()
    fp = _fake_proc(tmp_path / "a", 900, marker)
    fp["rc"].write_text("777 0\n")  # stale: an earlier start's record, setup still running
    assert ready(fp) == 1
    fp["rc"].write_text("900 0\n")  # this start's record
    assert ready(fp) == 0
    (tmp_path / "b").mkdir()
    plain = _fake_proc(tmp_path / "b", 900, "timeout -k 5 100 sleep infinity")
    assert ready(plain) == 0  # no wrapped entrypoint: ready at once


def test_sha_refs_never_fall_back_to_the_task_key(tmp_path):
    up = {"key": "rf/" + TID[3:], "image": "up@sha256:1", **CLEAN}
    # a flattened image is checked by its id alone; the upstream row must not stand in for it
    assert R.validation_verdict(TID, [IID], _summary(tmp_path, [up])) is None


def _build_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_rf", Path(__file__).resolve().parents[2] / "scripts" / "build_rf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_base_no_results_needs_evidence():
    b = _build_module()

    def run(status, log):
        return {"rc": 0, "timed_out": False,
                "detail": {"status": status, "log_tails": {"run_stdout.log": log}}}

    assert b._valid_base(run("ok", ""))
    compile_err = run("no_results", "x\npkg/engine/engine_test.go:12:3: undefined: NewThing\n")
    assert b._valid_base(compile_err)
    assert "undefined: NewThing" in b.base_failure_evidence(compile_err)
    assert not b._valid_base(run("no_results", "something else went wrong\n"))
    oom = run("ok", "")
    oom["detail"]["oom_kills"] = 1
    assert not b._valid_base(oom) and not b._valid(oom)


def test_slots_budget_and_heavy_limit(monkeypatch):
    import threading
    b = _build_module()
    monkeypatch.setattr(b, "available_gb", lambda: 30.0)
    order = []
    light = b.slots(8192)
    light.__enter__()
    assert b._in_use == {"mb": 8192, "heavy": 0}

    def heavy_job():
        with b.slots(16384):  # capped to the whole budget: waits for the light job
            order.append("heavy")

    t = threading.Thread(target=heavy_job)
    t.start()
    t.join(0.3)
    assert order == []  # still blocked
    light.__exit__()
    t.join(5)
    assert order == ["heavy"] and b._in_use == {"mb": 0, "heavy": 0}



def test_image_id_of_skips_cli_warnings():
    b = _build_module()
    out = ("Flag --pause has been deprecated, and enabled by default.\n"
           "sha256:" + "a" * 64 + "\n")
    assert b.image_id_of(out) == "sha256:" + "a" * 64
    with pytest.raises(RuntimeError):
        b.image_id_of("Flag --pause has been deprecated\n")


def test_grade_reports_oom_as_an_error_not_a_reward(world, tmp_path):
    """astra r3 #1: a reward from a container with OOM evidence is an infrastructure failure."""
    import unittest.mock as um
    res = _records(tmp_path)
    rew = {"reward": 0, "f2p_total": 1, "f2p_passed": 0, "p2p_total": 0, "p2p_passed": 0}
    sb = _FakeSandbox(rew)
    real_exec = sb.exec

    def exec_(cmd, timeout_s=600, workdir="/app"):
        if cmd == R.MEMORY_EVENTS_CMD:
            return _FakeExec(0, "oom 1\noom_kill 1\n")
        return real_exec(cmd, timeout_s, workdir)

    sb.exec = exec_

    class Info:
        task_id, image, tests_dir = TID, IID, str(tmp_path)

    with um.patch.object(R, "sources", return_value={TID: world["src"]}):
        out = R.grade(Info(), b"p", "r", inv=_FakeInv(sb), results_dir=res)
    assert out["reward"] is None and "OOM" in out["grade_error"]


def test_ready_check_rejects_a_failed_setup(tmp_path):
    marker = "/bin/bash -c ...emx_entrypoint_rc... /entrypoint.sh timeout sleep"
    fast = R.READY_SH.replace(f"seq 1 {R.ENTRYPOINT_SETUP_TIMEOUT_S + 300}", "seq 1 2")
    fp = _fake_proc(tmp_path, 900, marker)
    for rc, want in (("0", 0), ("1", 2), ("124", 2)):
        fp["rc"].write_text(f"900 {rc}\n")
        assert subprocess.run(["bash", "-c", _local(fast, fp)], timeout=30,
                              check=False).returncode == want
