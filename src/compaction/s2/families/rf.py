"""SWE-Interact's SWE Atlas Refactoring family (25 tasks) as InteractTasks for the S2 harness.

Brief logs/s2/briefs/integrate-rf.md; feasibility logs/s2/swe_interact_50_feasibility.md
(sections 1-4, 6). Source: the SWE-Interact checkout, `MT/rf_task-<id>/`
(`interact_run.SI_TASKS`).

- **Ids** are the SWE-Interact directory names, `rf_task-<24 hex>`.
- **Images.** The upstream image (`task.toml` `docker_image`, pinned by digest; a squashed
  single-commit repo, no solution in it) plus two layers, each made in one of our sandboxes and
  `docker commit`ted (upstream CMD/ENTRYPOINT/WORKDIR restored, `commit_changes`) on the desktop
  Docker host by `scripts/build_rf.py`, as `emx-tasks/swe-interact-rf:<id>-v1`:
  - Go modules: where the repo doesn't vendor them, `go mod download` at build time (with
    network), since our sandboxes and graders run with --network none (`stage_script`).
  - `/app` -> the repo. Our harness runs every command in `/app` (`interact.WORKSPACE`, the
    fingerprint, the trap snapshots); the RF repos live in 10 different places
    (`tests/config.json` `mount_path`). Where `/app` does not exist it becomes a symlink to the
    repo; where it is the repo nothing changes. Where the repo is under an existing `/app`
    (suricata `/app/suricata`, MariaDB `/app/source`) no symlink is possible, and the task
    needs a workdir-aware harness (`harness_ok` false in images.json).
  - A profile.d line pins the PATH the tests need, because our sandbox runs `bash -lc`, whose
    /etc/profile resets the image's ENV PATH (k6: `go` not found), while on scapy the login PATH
    is the working one (feasibility section 1). The build picks, per task, the PATH under which
    the reference solution's tests pass.
- **Grading: tests only.** Upstream's reward needs a paid Opus rubric (dropped here), and its
  tests half was vacuous: it counts only relevant tests that went pass->fail or missing->fail,
  through a loose name matcher, so a patch that breaks nothing scores 1 whether or not any test
  ran. Ours (`TEST_SH`, `score_run`, materialised into `<task_dir>/tests/`) keeps the DeepSWE
  verifier contract (model.patch in, reward.json with f2p/p2p counts out), so
  `outcomes.grade_patch` and `sandbox.parse_reward` work unchanged:
  1. apply model.patch minus every test file (upstream's `test_files.json`, plus the files
     `test_patch.diff` touches);
  2. apply `test_patch.diff`;
  3. build with upstream's own `build_project` (functions sourced from
     `master_validator_script.sh`, identical in all 25 tasks);
  4. run upstream's `run_script.sh` with the relevant test files (scripts without file
     arguments run their full suite, as upstream);
  5. reward 1 iff every fail_to_pass and pass_to_pass test passed (a test reported both passed
     and failed, e.g. by two passes over one package, counts as failed). The lists are measured, not
     taken from upstream: f2p = passed with the reference solution and not on the untouched
     base (both with `test_patch`), p2p = passed on both; a test whose result differed between
     two reference runs is dropped as flaky (`scripts/build_rf.py`, committed per task in
     `results/sprint/families/rf/grading/<id>.json`).
  What this shows: the selected tests tell the reference from the untouched base. It does not
  check the refactor beyond those tests (upstream's rubric did), nor the persona's preservation
  rules, and a reference run passes partly by construction once flaky tests are dropped
  (base_gold.json counts them).
- **Usable** = the base/gold check passed (base reward 0, gold reward 1, f2p not empty) in
  `results/sprint/families/rf/base_gold.json` on the image id images.json records (TaskInfo.image
  is that immutable id, not the tag), and imgval reports CLEAN
  (container probe, layer scan and hidden tests) for both the upstream image and our derived
  one (which adds downloaded Go module sources).
- **Resources.** `task.toml` asks for 16 CPU / 16 GB. The check ran at 4 CPU / 8 GB, or 8 CPU /
  16 GB for the heavy repos (`HEAVY_REPOS`); `resources()` returns what each task was graded
  with. Run RF tasks on the desktop host only: the images exist only there, and `grade`
  refuses any other host.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from compaction.s2 import config as C

PREFIX = "rf_task-"
PURPOSE = "fam-rf"  # emx.sandbox label of every container the build starts
IMAGE_REPO = "emx-tasks/swe-interact-rf"
IMAGE_VERSION = "v1"
WORKSPACE = "/app"  # where our harness works (interact.WORKSPACE)
STEPS = ("01_plan", "02_implement")
DATA_DIR = C.REPO_ROOT / "data" / "raw" / "sprint" / "families" / "rf"
RESULTS_DIR = C.REPO_ROOT / "results" / "sprint" / "families" / "rf"
VALIDATION_SUMMARY = C.REPO_ROOT / "results" / "sprint" / "task_validation" / "summary.json"
HEAVY_REPOS = ("Automattic_wp-calypso", "grafana_grafana", "MariaDB_server", "netdata_netdata",
               "OISF_suricata")
LIGHT = {"cpus": 4, "memory_mb": 8192}
HEAVY = {"cpus": 8, "memory_mb": 16384}
GRADE_TIMEOUT_S = 7200  # upstream's step-05 verifier timeout
DESKTOP_HOST = "ssh://rig"  # the RF images are local tags there
PROFILE_D = "/etc/profile.d/zz-emx-rf-path.sh"
# The upstream master validator up to its "Header" section: function definitions plus a
# `parse_args "$@"` that is a no-op without arguments.
MV_HEADER = "# Header\n"


def si_root() -> Path:
    from compaction.s2.interact_run import SI_TASKS
    return SI_TASKS


_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def patch_paths(text: str) -> list[str]:
    """Paths a git diff touches (b/ side, then a/ side when it differs)."""
    out: list[str] = []
    for a, b in _DIFF_FILE.findall(text):
        for p in (b, a):
            if p not in out:
                out.append(p)
    return out


def _names(items) -> list[str]:
    return [it if isinstance(it, str) else it["name"] for it in items or []
            if isinstance(it, str) or (isinstance(it, dict) and it.get("name"))]


@dataclass(frozen=True)
class RfSource:
    task_id: str
    src_dir: Path
    config: dict  # tests/config.json
    toml: dict  # task.toml

    @property
    def repo(self) -> str:
        return self.config["repo"]

    @property
    def language(self) -> str:
        return self.config["language"]

    @property
    def mount_path(self) -> str:
        return self.config["mount_path"].rstrip("/") or "/"

    @property
    def upstream_image(self) -> str:
        return self.toml["environment"]["docker_image"]

    @property
    def heavy(self) -> bool:
        return self.repo in HEAVY_REPOS

    @property
    def tests(self) -> Path:
        return self.src_dir / "tests"

    def test_patch(self) -> str:
        return (self.tests / "test_patch.diff").read_text(errors="replace")

    def gold_patch(self) -> bytes:
        return (self.src_dir / "solution" / "gold.patch").read_bytes()

    def test_files(self) -> list[str]:
        """Files the agent may not change: upstream's test_files.json plus test_patch's files."""
        out = _names(self._list("test_files.json", "files"))
        return out + [p for p in patch_paths(self.test_patch()) if p not in out]

    def relevant_files(self) -> list[str]:
        return _names(self._list("relevant_file_names.json", "files"))

    def relevant_tests(self) -> list[str]:
        return _names(self._list("relevant_test_names.json", "tests"))

    def _list(self, name: str, key: str) -> list:
        """Upstream's list files: {"<key>": [...]} in most tasks, a bare list in some."""
        d = json.loads((self.tests / name).read_text())
        return d.get(key) or [] if isinstance(d, dict) else d

    def harness_ok(self) -> bool:
        """Can `/app` be made the repo (it is the repo, or it can be a symlink to it)?"""
        return self.mount_path == WORKSPACE or not self.mount_path.startswith(WORKSPACE + "/")


def sources(root: Path | None = None) -> dict[str, RfSource]:
    out = {}
    for d in sorted(Path(root or si_root()).glob(PREFIX + "*")):
        if (d / "task.toml").exists():
            out[d.name] = RfSource(d.name, d, json.loads((d / "tests" / "config.json").read_text()),
                                   tomllib.loads((d / "task.toml").read_text()))
    return out


def task_ids(*, usable_only: bool = False, root: Path | None = None, **_kw) -> list[str]:
    ids = list(sources(root))
    return [t for t in ids if usable(t)[0]] if usable_only else ids


def usable_task_ids(**kw) -> list[str]:
    return task_ids(usable_only=True, **kw)


def image_tag(task_id: str) -> str:
    return f"{IMAGE_REPO}:{task_id.removeprefix(PREFIX)}-{IMAGE_VERSION}"


def step_texts(src: RfSource) -> dict[str, str]:
    """Upstream step instructions, verbatim. They name the repo's own path (e.g. `/workspace`),
    which is the same directory as `/app` in our image."""
    return {s: (src.src_dir / "steps" / s / "instruction.md").read_text() for s in STEPS}


# ------------------------------------------------------------------------------------------
# Image layer
# ------------------------------------------------------------------------------------------


def _safe(value: str, what: str) -> str:
    if any(c in value for c in "'\n\"$`") or not value:
        raise ValueError(f"unsafe {what} {value!r}")
    return value


def stage_script(mount_path: str) -> str:
    """The stage layer's RUN script (built with network): `/app` -> repo where possible, and
    the Go module cache filled for repos that don't vendor their modules. Our sandboxes run
    with --network none, and trufflehog's tests fetch modules from proxy.golang.org otherwise.
    Only the dependencies' sources are downloaded (`go mod download` compiles nothing); the
    repo's own tree is not touched."""
    m = _safe(mount_path, "mount path")
    if not m.startswith("/"):
        raise ValueError(f"mount path must be absolute: {m!r}")
    parts = ["set -eu", f"test -d '{m}/.git'",
             # the tree as upstream ships it (suricata's has one modified file): kept as is
             f"B=\"$(git -C '{m}' status --porcelain)\""]
    if m != WORKSPACE and not m.startswith(WORKSPACE + "/"):
        # an empty /app is replaced by the link; a non-empty one (netdata) is left alone, and
        # the task then needs a workdir-aware harness (the final image check records it)
        parts.append(f"if [ -d {WORKSPACE} ] && [ ! -L {WORKSPACE} ] && "
                     f"[ -z \"$(ls -A {WORKSPACE})\" ]; then rmdir {WORKSPACE}; fi")
        parts.append(f"if [ ! -e {WORKSPACE} ]; then ln -s '{m}' {WORKSPACE}; fi")
    parts.append(f"cd '{m}'")
    # GOTOOLCHAIN=auto: some go.mod files need a newer Go than the image's (trufflehog
    # 4fd11889: 1.23.1 vs 1.22.12); upstream's run_script fetches it with network, so the
    # toolchain is fetched here into the module cache, where `auto` finds it offline.
    parts.append("if [ -f go.mod ] && command -v go >/dev/null; then "
                 "GOTOOLCHAIN=auto go list -m >/dev/null && "
                 "{ [ -d vendor ] || GOTOOLCHAIN=auto go mod download; }; fi")
    parts.append(f"test \"$(git -C '{m}' status --porcelain)\" = \"$B\"")
    return " && ".join(parts)


def stage_tag(task_id: str) -> str:
    return image_tag(task_id) + "-stage"


def pin_script(path: str) -> str:
    """The final layer's script, for login shells (`bash -lc`): the PATH the tests need, and
    GOTOOLCHAIN=auto, so a go.mod needing a newer Go uses the toolchain the stage cached
    (a no-op where the image's Go suffices or there is no Go)."""
    p = _safe(path, "PATH")
    return (f"printf '%s\\n' 'export PATH=\"{p}\"' 'export GOTOOLCHAIN=auto' "
            f"> {PROFILE_D}")


# Solution leaks in upstream images (imgval, 24 Oct): build artifacts compiled from the solved
# code, deleted (before the record runs) for each flagged task. Every final image is flattened
# (docker export | docker import), since several upstream images also hold the solution in a
# lower layer that a later one deletes; base/gold is graded on the flattened image's id.
# Paths only: binaries and caches nothing in the grader needs (it builds from source).
CLEANUP: dict[str, tuple[str, ...]] = {
    # netdata: a build tree and binary of the solved code (gold's new rrdhost-system-info.c;
    # 813b's build files name all 10 files its gold patch creates)
    "rf_task-69b7c2a04b6f8ff9ed98813d": ("/opt/netdata-build", "/usr/sbin/netdata"),
    "rf_task-69b7c2a04b6f8ff9ed98813b": ("/opt/netdata-build", "/usr/sbin/netdata"),
    # k6: a k6 binary and Go build cache holding gold's new names (NewGroupPath, ...)
    "rf_task-696719205599a51110d4b441": ("/root/.cache/go-build", "/usr/local/bin/k6"),
}


def cleanup_script(paths: tuple[str, ...] | list[str]) -> str:
    """Delete `paths` and fail unless every one is gone."""
    for q in paths:
        _safe(q, "cleanup path")
        if not q.startswith("/") or ".." in q.split("/") or q.rstrip("/") in ("", "/app"):
            raise ValueError(f"refusing to delete {q!r}")
    quoted = " ".join(f"'{q}'" for q in paths)
    return f"set -eu; rm -rf -- {quoted}; for p in {quoted}; do test ! -e \"$p\"; done"


def import_changes(task_id: str, config: dict) -> list[str]:
    """`docker import --change` lines for a flattened final image: everything of the upstream
    config a container needs (ENV, CMD, ENTRYPOINT, WORKDIR), plus our labels."""
    out = []
    for e in config.get("Env") or []:
        k, _, v = e.partition("=")
        out.append(f"ENV {k}={json.dumps(v)}")
    if config.get("User"):
        out.append(f"USER {config['User']}")
    out += [f"EXPOSE {port}" for port in sorted(config.get("ExposedPorts") or {})]
    out += [f"VOLUME {json.dumps(v)}" for v in sorted(config.get("Volumes") or {})]
    labels = " ".join(f"{k}={json.dumps(v)}" for k, v in sorted((config.get("Labels") or {}
                                                                 ).items()))
    if labels:
        out.append(f"LABEL {labels}")
    return out + commit_changes(task_id, config)[:3] + [
        f'LABEL emx.family="rf" emx.task="{task_id}" emx.flattened="1"']


CONFIG_KEYS = ("Env", "Cmd", "Entrypoint", "WorkingDir", "User", "ExposedPorts", "Volumes")


def config_diff(upstream: dict, final: dict) -> dict:
    """Differences between the upstream image's Config and ours, apart from the expected ones
    (the entrypoint wrapper, our emx labels). Empty = the image runs as upstream's does."""
    out = {}
    for k in CONFIG_KEYS:
        want = entrypoint(upstream.get(k)) if k == "Entrypoint" else upstream.get(k)
        got = final.get(k)
        if (want or None) != (got or None):
            out[k] = {"upstream": want, "ours": got}
    ul = {k: v for k, v in (upstream.get("Labels") or {}).items()}
    ol = {k: v for k, v in (final.get("Labels") or {}).items() if not k.startswith("emx.")}
    if ul != ol:
        out["Labels"] = {"upstream": ul, "ours": ol}
    return out


# Upstream entrypoints (simple-login, calypso) start services and run migrations under `set -e`
# before `exec "$@"`. A grader or agent command racing that setup (our sandboxes run one at
# once) made the entrypoint fail and the container die (exit 1; 24 Oct). The wrapper runs the
# upstream entrypoint with `true` (its setup only), records its exit code, then execs the
# command whatever happened; TEST_SH waits for the record (`ENTRYPOINT_RC`).
ENTRYPOINT_RC = "/tmp/emx_entrypoint_rc"
ENTRYPOINT_SETUP_TIMEOUT_S = 1200
# The record is "<PID 1 start time> <setup exit code>", written atomically (tmp + mv). PID 1's
# start time (/proc/1/stat field 22, kept across `exec`) identifies this container start, so
# a record carried into an image by `docker commit` (the harness's kept boundary images) never
# matches a later start (astra r2 #2; s2-feas review). Setup is bounded by `timeout`. No
# backslashes: `docker import --change` (the flattened images) unescapes them.
_PID1_START = "$(sed 's/.*) //' /proc/1/stat | cut -d' ' -f20)"
_EP_WRAP = (f'S={_PID1_START}; timeout -k 10 {ENTRYPOINT_SETUP_TIMEOUT_S} "$0" true; R=$?; '
            f'echo "$S $R" > {ENTRYPOINT_RC}.tmp && '
            f'mv -f {ENTRYPOINT_RC}.tmp {ENTRYPOINT_RC}; exec "$@"')
# Blocks until the container is ready for its first command: no wrapped entrypoint, or this
# start's setup record exists with exit code 0. Exit 2 if that setup failed (upstream's own
# `set -e` entrypoint would have taken the container down; astra r3 #2), 1 after the setup
# bound plus a margin (fail closed).
READY_SH = (f'S={_PID1_START}; for _ in $(seq 1 {ENTRYPOINT_SETUP_TIMEOUT_S + 300}); do '
            f'if [ -r {ENTRYPOINT_RC} ] && [ "$(cut -d" " -f1 {ENTRYPOINT_RC})" = "$S" ]; '
            f'then [ "$(cut -d" " -f2 {ENTRYPOINT_RC})" = 0 ] && exit 0; exit 2; fi; '
            f'tr "\\0" " " < /proc/1/cmdline | grep -q emx_entrypoint_rc || exit 0; '
            f'sleep 1; done; exit 1')


def entrypoint(ep: list[str] | None) -> list[str]:
    if not ep:
        return []
    if len(ep) != 1:
        raise ValueError(f"multi-part entrypoint not supported: {ep!r}")
    return ["/bin/bash", "-c", _EP_WRAP, ep[0]]


def commit_changes(task_id: str, config: dict) -> list[str]:
    """`docker commit --change` lines for a layer made in one of our sandboxes: the upstream
    image's CMD, ENTRYPOINT and WORKDIR back (the sandbox's `timeout ... sleep infinity` and
    `--workdir /app` must not stick), and the sandbox's emx labels blanked, so containers the
    harness starts from the image carry only the harness's own labels."""
    return [f"CMD {json.dumps(config.get('Cmd') or [])}",
            f"ENTRYPOINT {json.dumps(entrypoint(config.get('Entrypoint')))}",
            f"WORKDIR {config.get('WorkingDir') or '/'}",
            f'LABEL emx.sandbox="" emx.run="" emx.family="rf" emx.task="{task_id}"']


# ------------------------------------------------------------------------------------------
# Grader (runs inside a fresh container of the task image; see the module docstring)
# ------------------------------------------------------------------------------------------

TEST_SH = r"""#!/bin/bash
# Tests-only RF grader (compaction.s2.families.rf). No LLM rubric. The EMX_* directory
# overrides exist for the unit tests only.
set -uo pipefail
T=${EMX_TESTS_DIR:-/tests}
V=${EMX_VERIFIER_DIR:-/logs/verifier}
A=${EMX_ART_DIR:-/tmp_artifact_storage}
P=${EMX_PATCH:-/logs/artifacts/model.patch}
mkdir -p "$V" "$A"
# a wrapped upstream entrypoint's setup must be done for this container start (rf.READY_SH);
# otherwise no reward is written (an infrastructure failure, never a 0)
if ! bash -c "$(cat "$T/emx_ready.sh")"; then
    echo '{"status": "not_ready"}' > "$V/emx_rf_detail.json"
    exit 0
fi
. "$T/emx_rf.env"  # EMX_WORKDIR, PATH
PY=$(command -v python3 || echo /usr/bin/python3)
OUT="$A/emx_rf_results.json"
rm -f "$OUT"
STATUS=ok
cd "$EMX_WORKDIR" || STATUS=no_workdir
git config --global --add safe.directory '*' 2>/dev/null || true
# CPUs this container may use (cgroup v2 quota), for upstream's build -j
NPROC=$(awk '$1 != "max" {q = int($1 / $2); if (q < 1) q = 1; print q}' \
        /sys/fs/cgroup/cpu.max 2>/dev/null)
export NPROC=${NPROC:-2}
if [ "$STATUS" = ok ] && [ -s "$P" ]; then
    EXC=()
    while IFS= read -r f; do [ -n "$f" ] && EXC+=("--exclude=$f"); done < "$T/emx_rf_protected.txt"
    git apply --whitespace=nowarn "${EXC[@]}" "$P" > "$V/apply.log" 2>&1 || STATUS=apply_failed
fi
if [ "$STATUS" = ok ]; then
    git apply --whitespace=nowarn "$T/test_patch.diff" >> "$V/apply.log" 2>&1 \
        || STATUS=test_patch_failed
fi
if [ "$STATUS" = ok ]; then
    cp "$T/run_script.sh" ./run_script.sh
    ( . "$T/emx_mv_lib.sh"; ARTIFACT_DIR="$A"; build_project ) > "$V/build.log" 2>&1 \
        || STATUS=build_failed
fi
if [ "$STATUS" = ok ]; then
    FILES=()
    while IFS= read -r f; do [ -n "$f" ] && FILES+=("$f"); done < "$T/emx_rf_files.txt"
    PYTHONUNBUFFERED=1 bash ./run_script.sh "$OUT" "${FILES[@]}" </dev/null \
        > "$V/run_stdout.log" 2>&1 || true
    [ -s "$OUT" ] || STATUS=no_results
fi
"$PY" "$T/emx_rf_score.py" "$STATUS" "$OUT" "$T/emx_rf_lists.json" "$V"
exit 0
"""


def score_run(status, results, lists):
    """Score one run (also the in-container scorer: Python 3.6+, no third-party imports).

    `results` is run_script.sh's JSON (or None), `lists` {"f2p": [...], "p2p": [...]} or None
    (record mode). Returns the detail dict; detail["reward"] is the reward.json object, or None
    in record mode."""
    def names(items):
        out = []
        for it in items or []:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict) and it.get("name"):
                out.append(it["name"])
        return out

    res = results or {}
    failed = set(names(res.get("failed_tests")))
    # a run script may run a test in several passes (trufflehog's baseline packages); a test
    # that failed in any pass is failed
    passed = set(names(res.get("passed_tests"))) - failed
    skipped = set(names(res.get("skipped_tests"))) - failed - passed
    detail = {"status": status, "n_passed": len(passed), "n_failed": len(failed),
              "n_skipped": len(skipped), "passed": sorted(passed), "failed": sorted(failed),
              "skipped": sorted(skipped), "reward": None}
    if lists is None:
        return detail
    ok = status == "ok"
    f2p, p2p = lists["f2p"], lists["p2p"]
    f2p_passed = sum(1 for t in f2p if ok and t in passed)
    p2p_passed = sum(1 for t in p2p if ok and t in passed)
    solved = len(f2p) > 0 and f2p_passed == len(f2p) and p2p_passed == len(p2p)
    detail["reward"] = {"reward": 1 if solved else 0, "f2p_total": len(f2p),
                        "f2p_passed": f2p_passed, "p2p_total": len(p2p),
                        "p2p_passed": p2p_passed, "status": status}
    detail["f2p_not_passed"] = [t for t in f2p if not (ok and t in passed)]
    detail["p2p_not_passed"] = [t for t in p2p if not (ok and t in passed)]
    return detail


_SCORE_MAIN = '''

def _main(argv):
    import json, os
    status, results_path, lists_path, out_dir = argv[1:5]
    results = None
    if os.path.exists(results_path):
        try:
            with open(results_path) as f:
                results = json.load(f)
        except ValueError:
            status = "bad_results" if status == "ok" else status
    lists = None
    if os.path.exists(lists_path):
        with open(lists_path) as f:
            lists = json.load(f)
    d = score_run(status, results, lists)
    with open(os.path.join(out_dir, "emx_rf_detail.json"), "w") as f:
        json.dump(d, f)
    if d["reward"] is not None:
        with open(os.path.join(out_dir, "reward.json"), "w") as f:
            json.dump(d["reward"], f)
    print("EMX_RF status=%s passed=%d failed=%d reward=%s" % (
        d["status"], d["n_passed"], d["n_failed"],
        None if d["reward"] is None else d["reward"]["reward"]))


if __name__ == "__main__":
    import sys
    _main(sys.argv)
'''


# Captured at import: `inspect.getsource` reads the file on disk, so a long-running build
# would otherwise ship whatever the file holds later (24 Oct: an edit mid-build broke it).
_SCORE_RUN_SRC = inspect.getsource(score_run)


def score_script() -> str:
    return "#!/usr/bin/env python3\n" + _SCORE_RUN_SRC + _SCORE_MAIN


def mv_lib(master_validator: str) -> str:
    """Upstream's master validator cut before its "Header" section, without `set -e`."""
    head, sep, _ = master_validator.partition(MV_HEADER)
    if not sep:
        raise ValueError("master_validator_script.sh has no '# Header' section")
    lines = [ln for ln in head.splitlines() if ln.strip() != "set -e"]
    return "\n".join(lines) + "\n"


def derive_lists(base: dict, gold: dict, gold2: dict | None = None) -> dict:
    """f2p / p2p from record-mode details of base and gold runs (both with test_patch).
    With a second gold run, tests whose pass state differs between the gold runs are dropped
    as flaky."""
    b, g = set(base.get("passed") or []), set(gold.get("passed") or [])
    flaky: set[str] = set()
    if gold2 is not None:
        flaky = g ^ set(gold2.get("passed") or [])
        g -= flaky
    return {"f2p": sorted(g - b), "p2p": sorted(g & b), "flaky": sorted(flaky)}


def env_file(workdir: str, path: str) -> str:
    for v in (workdir, path):
        if '"' in v or "\n" in v or "$" in v or "`" in v:
            raise ValueError(f"unsafe value {v!r}")
    return f'EMX_WORKDIR="{workdir}"\nexport PATH="{path}"\nexport GOTOOLCHAIN=auto\n'


def _write(path: Path, data: str | bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)
    if mode is not None:
        path.chmod(mode)


# Logged deviation, per task (`runner` in grading/<id>.json): upstream's Go run_script runs whole
# packages, and offline an earlier test that needs network (trufflehog TestAPKHandler,
# TestScanGCS) panics and aborts the package before the relevant tests run. This runner runs
# only upstream's relevant tests, per package, with `-run`/`-bench` anchored to their top-level
# names, and writes run_script.sh's JSON format (names "<package>/<test>", as upstream). Rule
# (decided before use): a Go task is tried with it when under half of its relevant tests appear
# in the reference run with the upstream runner, and keeps it if it reaches more of them; or
# when upstream's runner gives no valid reference run at all offline (grafana's needs Node.js
# from the network after its Go tests).
RUNNERS = ("upstream", "go-relevant")
GO_RELEVANT_RUN_SH = r"""#!/bin/bash
# emx go-relevant runner (compaction.s2.families.rf): relevant tests only, per package.
OUT="${1:-/tmp/test_results.json}"
T=${EMX_TESTS_DIR:-/tests}
export GOTOOLCHAIN=auto
MOD=$(go list -m)
PLAN=$(mktemp)
python3 - "$MOD" "$T/relevant_test_names.json" > "$PLAN" <<'EMXPY'
import json, re, sys
mod, rel = sys.argv[1], sys.argv[2]
by = {}
for t in json.load(open(rel))["tests"]:
    n = t["name"]
    if not n.startswith(mod + "/"):
        continue
    segs = n[len(mod) + 1:].split("/")
    i = next((k for k, s in enumerate(segs) if re.match(r"(Test|Benchmark|Example|Fuzz)", s)),
             None)
    if i is None:
        continue
    by.setdefault("./" + "/".join(segs[:i]), set()).add(segs[i])
for pkg, names in sorted(by.items()):
    print(pkg + "\t^(" + "|".join(sorted(names)) + ")$")
EMXPY
JS=$(mktemp)
while IFS=$'\t' read -r pkg rx; do
    echo "running $pkg -run $rx"
    CGO_ENABLED=0 go test -json -count=1 -timeout=10m -run "$rx" -bench "$rx" -benchtime=1x \
        "$pkg" >> "$JS" 2>&1
done < "$PLAN"
python3 - "$JS" "$OUT" <<'EMXPY'
import json, sys
src, out = sys.argv[1], sys.argv[2]
passed, failed, skipped, bench_done = set(), set(), set(), set()
seen, pkg_failed = {}, set()
for line in open(src, errors="replace"):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if not isinstance(e, dict):
        continue
    a = e.get("Action")
    if not e.get("Test"):
        if a == "fail" and e.get("Package"):
            pkg_failed.add(e["Package"])
        continue
    name = e["Package"] + "/" + e["Test"]
    seen.setdefault(e["Package"], set()).add(name)
    if a == "pass":
        passed.add(name)
    elif a == "fail":
        failed.add(name)
    elif a == "skip":
        skipped.add(name)
    elif (a == "output" and e["Test"].startswith("Benchmark")
          and "ns/op" in (e.get("Output") or "")):
        bench_done.add(name)  # a benchmark's result line: it completed
passed |= bench_done - failed  # benchmarks emit no "pass"
for pkg in pkg_failed:  # a panic, timeout or killed binary: whatever didn't pass, failed
    failed |= seen.get(pkg, set()) - passed
res = {"passed_tests": [{"name": n} for n in sorted(passed)],
       "failed_tests": [{"name": n} for n in sorted(failed)],
       "skipped_tests": [{"name": n} for n in sorted(skipped)]}
json.dump(res, open(out, "w"), indent=1)
print("Tests completed: %d passed, %d failed" % (len(passed), len(failed)))
EMXPY
"""


def materialize(src: RfSource, path: str, lists: dict | None, out_root: Path | None = None,
                runner: str = "upstream") -> Path:
    """<out_root>/<id>/ with tests/ (our grader + the upstream files it uses), the reference
    solution as solution/solution.patch, and instruction.md. `lists` None = record mode.
    `runner` "go-relevant" puts GO_RELEVANT_RUN_SH in run_script.sh's place (upstream's is kept
    as run_script.upstream.sh)."""
    if runner not in RUNNERS:
        raise ValueError(f"unknown runner {runner!r}")
    d = Path(out_root or DATA_DIR) / src.task_id
    t = d / "tests"
    if t.exists():
        for stale in ("emx_rf_lists.json",):
            if lists is None and (t / stale).exists():
                (t / stale).unlink()
    for name in ("test_patch.diff", "test_files.json", "relevant_file_names.json",
                 "relevant_test_names.json"):
        _write(t / name, (src.tests / name).read_bytes())
    upstream_run = (src.tests / "run_script.sh").read_bytes()
    if runner == "go-relevant":
        _write(t / "run_script.sh", GO_RELEVANT_RUN_SH)
        _write(t / "run_script.upstream.sh", upstream_run)
    else:
        _write(t / "run_script.sh", upstream_run)
        if (t / "run_script.upstream.sh").exists():
            (t / "run_script.upstream.sh").unlink()
    _write(t / "emx_mv_lib.sh", mv_lib((src.tests / "master_validator_script.sh").read_text()))
    _write(t / "test.sh", TEST_SH, 0o755)
    _write(t / "emx_rf_score.py", score_script(), 0o755)
    _write(t / "emx_ready.sh", READY_SH + "\n")
    _write(t / "emx_rf.env", env_file(src.mount_path, path))
    _write(t / "emx_rf_protected.txt", "".join(f + "\n" for f in src.test_files()))
    _write(t / "emx_rf_files.txt", "".join(f + "\n" for f in src.relevant_files()))
    if lists is not None:
        _write(t / "emx_rf_lists.json", json.dumps({"f2p": lists["f2p"], "p2p": lists["p2p"]}))
    _write(d / "solution" / "solution.patch", src.gold_patch())
    _write(d / "instruction.md", (src.src_dir / "instruction.md").read_bytes())
    return d


def collect_command() -> str:
    return ("cd /app && mkdir -p /logs/artifacts && git config --global --add safe.directory "
            "'*' && git diff --binary \"$(cat /tmp/base_commit.txt)\" HEAD "
            "> /logs/artifacts/model.patch")


# ------------------------------------------------------------------------------------------
# Persona preservation rules (Q2 content)
# ------------------------------------------------------------------------------------------

_RULE = re.compile(r"\b(keep|kept|preserv\w*|retain\w*|maintain\w*|remain\w*|unchanged|"
                   r"must not|should not|must still|still (?:be|work|return)|backward\w*|"
                   r"compatib\w*|stable|do not|don't|untouched|as[- ]is|intact)\b", re.IGNORECASE)
_BOILER = re.compile(r"(taken care of all changes to the test files|DON'T have to modify the "
                     r"testing logic|minimal changes to non-tests files|Do NOT modify any test files)", re.IGNORECASE)


def task_block(src: RfSource) -> str:
    """The persona's hidden spec: the task's instruction.md (the tail of persona.md)."""
    return (src.src_dir / "instruction.md").read_text()


def preservation_rules(text: str) -> list[str]:
    """Sentences (and interface description lines) of the task block that state a keep /
    preserve / must-not rule, upstream boilerplate excluded. A regex screen, for reading."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        flat = " ".join(ln.strip().lstrip("-").strip() for ln in para.splitlines())
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z`])", flat):
            s = s.strip()
            if s and _RULE.search(s) and not _BOILER.search(s) and s not in out:
                out.append(s)
    return out


# ------------------------------------------------------------------------------------------
# Build records and usability
# ------------------------------------------------------------------------------------------


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def images_manifest(results_dir: Path | None = None) -> dict:
    """{task_id: {image, image_id, upstream_image, path, path_source, symlink, ...}}."""
    return _load(Path(results_dir or RESULTS_DIR) / "images.json").get("tasks", {})


def base_gold(results_dir: Path | None = None) -> dict:
    return _load(Path(results_dir or RESULTS_DIR) / "base_gold.json").get("tasks", {})


def grading_lists(task_id: str, results_dir: Path | None = None) -> dict | None:
    p = Path(results_dir or RESULTS_DIR) / "grading" / f"{task_id}.json"
    return json.loads(p.read_text()) if p.exists() else None


def validation_rows(task_id: str, images: list[str], summary: Path | None = None
                    ) -> list[dict | None]:
    """imgval's rows (results/sprint/task_validation/summary.json `rows`) for each of `images`:
    matched on the row's `image` or `image_id`, or for the upstream image (first) on its key
    `rf/task-<id>`. A `sha256:` ref matches only a row whose image_id is exactly it, so a
    verdict for an earlier build of the same tag is never taken."""
    rows = [r for r in _load(Path(summary or VALIDATION_SUMMARY)).get("rows") or []
            if isinstance(r, dict)]
    key = "rf/" + task_id.removeprefix("rf_")
    out = []
    for i, ref in enumerate(images):
        if ref.startswith("sha256:"):
            hit = [r for r in rows if r.get("image_id") == ref]
        else:
            hit = [r for r in rows if r.get("image") == ref]
        if not hit and i == 0 and not ref.startswith("sha256:"):
            hit = [r for r in rows if r.get("key") == key and not str(r.get("image", "")
                                                                        ).startswith(IMAGE_REPO)]
        out.append(hit[-1] if hit else None)
    return out


def row_verdict(row: dict | None) -> str | None:
    """CLEAN only if the container verdict, the layer scan and the hidden-tests check are all
    CLEAN (a missing layer or hidden-tests field counts as not checked, i.e. not CLEAN)."""
    if not row:
        return None
    vs = [row.get("verdict"), row.get("layers"), row.get("hidden_tests")]
    for v in ("LEAK", "ERROR"):
        if v in vs:
            return v
    return "CLEAN" if all(v == "CLEAN" for v in vs) else None


def validation_verdict(task_id: str, images: list[str], summary: Path | None = None
                       ) -> str | None:
    """CLEAN iff every one of `images` (upstream first, then our derived tag) is CLEAN."""
    vs = [row_verdict(r) for r in validation_rows(task_id, images, summary)]
    for v in ("LEAK", "ERROR", None):
        if v in vs:
            return v
    return "CLEAN"


def usable(task_id: str, results_dir: Path | None = None,
           summary: Path | None = None) -> tuple[bool, str]:
    """(usable, reason): image built, harness-compatible, base/gold passed, imgval CLEAN."""
    img = images_manifest(results_dir).get(task_id)
    if not img:
        return False, "image not built"
    if not img.get("harness_ok"):
        return False, "repo is under an existing /app: needs a workdir-aware harness"
    bg = base_gold(results_dir).get(task_id) or {}
    if not bg.get("pass"):
        return False, f"base/gold check not passed: {bg.get('reason') or 'not run'}"
    if bg.get("image_id") != img.get("image_id"):
        return False, "base/gold check ran on a different image id than images.json records"
    # a flattened image keeps none of the upstream layers, so only its own verdict counts
    refs = [img["image_id"]] if img.get("flattened") else [img["upstream_image"],
                                                           img["image_id"]]
    v = validation_verdict(task_id, refs, summary)
    if v != "CLEAN":
        return False, f"image validation verdict {v or 'missing'}"
    return True, "ok"


def resources(task_id: str, results_dir: Path | None = None, root: Path | None = None) -> dict:
    """{cpus, memory_mb, storage_mb, workdir, ready_file} (the families interface). cpus and
    memory_mb are
    what the base/gold check graded with (task.toml asks 16 / 16384 for every RF task; rig's
    limits allow 4 / 8192, or 8 / 16384 for `HEAVY_REPOS` and after an OOM); storage_mb is task.toml's; workdir
    is the repo's real path (`/app` points to it in our image when `harness_ok`); ready_file,
    when set, is the setup record of the wrapped upstream entrypoint, and ready_cmd a shell
    command to run after starting a container and before its first command: exit 0 = ready,
    1 = setup did not finish in time, 2 = setup failed (`READY_SH`; checks the record is this
    start's)."""
    bg = base_gold(results_dir).get(task_id) or {}
    src = sources(root)[task_id]
    res = bg.get("resources") or (HEAVY if src.heavy else LIGHT)
    img = images_manifest(results_dir).get(task_id) or {}
    return {"cpus": res["cpus"], "memory_mb": res["memory_mb"],
            "storage_mb": src.toml["environment"].get("storage_mb"), "workdir": src.mount_path,
            "ready_file": ENTRYPOINT_RC if img.get("entrypoint_wrapped") else None,
            "ready_cmd": READY_SH if img.get("entrypoint_wrapped") else None}


class RfTaskNotUsable(RuntimeError):
    pass


def task_info(src: RfSource, results_dir: Path | None = None, out_root: Path | None = None):
    """The TaskInfo the grader and run configs read (image from the build manifest)."""
    from compaction.s2.tasks import TaskInfo
    img = images_manifest(results_dir).get(src.task_id)
    lists = grading_lists(src.task_id, results_dir)
    if not img or lists is None:
        raise RfTaskNotUsable(f"{src.task_id}: image or grading lists missing "
                              "(scripts/build_rf.py)")
    d = materialize(src, img["path"], lists, out_root, runner=lists.get("runner", "upstream"))
    return TaskInfo(task_id=src.task_id, rank=None, language=src.language, image=img["image_id"],
                    collect_command=collect_command(), instruction=task_block(src),
                    task_dir=str(d), tests_dir=str(d / "tests"),
                    solution_files=patch_paths(src.gold_patch().decode(errors="replace")))


def load_task(task_id: str, *, require_usable: bool = True, root: Path | None = None,
              results_dir: Path | None = None, out_root: Path | None = None,
              summary: Path | None = None):
    """The InteractTask for one RF task (same object as interact_run.load_task's)."""
    from compaction.s2.interact_run import InteractTask, _rel
    srcs = sources(root)
    if task_id not in srcs:
        raise KeyError(f"no SWE Atlas Refactoring task {task_id!r}")
    if require_usable:
        ok, why = usable(task_id, results_dir, summary)
        if not ok:
            raise RfTaskNotUsable(f"{task_id}: {why}")
    src = srcs[task_id]
    spec = src.src_dir / "instruction.md"
    return InteractTask(
        task_id=task_id, info=task_info(src, results_dir, out_root),
        persona=(src.src_dir / "environment" / "user-server" / "persona.md").read_text(),
        steps=step_texts(src), spec_path=_rel(spec),
        spec_sha256=hashlib.sha256(spec.read_bytes()).hexdigest())


def grade(task, patch: bytes, run_id: str, host: str | None = DESKTOP_HOST, inv=None,
          timeout_s: int = GRADE_TIMEOUT_S, results_dir: Path | None = None) -> dict:
    """The families grade callable: `outcomes.grade_patch`'s output shape, graded in a fresh
    container with the resources and timeout the base/gold check used. `task` is an
    InteractTask or its TaskInfo; `inv` routes the container through the harness's
    HostInvocation (it must be on the desktop host, where the images are)."""
    from compaction.deepswe.sandbox import VerifierError
    info = getattr(task, "info", task)
    where = getattr(inv, "host", None) if inv is not None else host
    if where != DESKTOP_HOST:
        return {"reward": None, "grade_error": f"RF images exist only on {DESKTOP_HOST}; "
                f"refusing to grade on {where!r}"}
    res = resources(task.task_id, results_dir)
    try:
        rew, _ = grade_reward(info.image, info.tests_dir, patch, run_id=run_id, host=host,
                              inv=inv, timeout_s=timeout_s, cpus=res["cpus"],
                              memory_mb=res["memory_mb"],
                              purpose=getattr(inv, "purpose", PURPOSE))
    except VerifierError as e:
        return {"reward": None, "grade_error": str(e)[:1000]}
    f2p = rew["f2p_passed"] / rew["f2p_total"] if rew["f2p_total"] else None
    p2p = rew["p2p_passed"] / rew["p2p_total"] if rew["p2p_total"] else None
    return {"reward": rew["reward"], "f2p": f2p, "p2p": p2p, "grade_error": None,
            **{k: rew[k] for k in ("f2p_passed", "f2p_total", "p2p_passed", "p2p_total")}}


READ_DETAIL_CMD = "cat /logs/verifier/emx_rf_detail.json 2>/dev/null || echo '{}'"
LOG_TAILS = ("apply.log", "build.log", "run_stdout.log")
# upstream's own build logs (build_project writes them to its artifact dir)
BUILD_LOG_TAILS = ("make.log", "cmake.log", "cmake_build.log", "autogen.log", "meson_build.log")


def run_grader(image: str, tests_dir: str | Path, patch: bytes, *, run_id: str | None = None,
               host: str | None = DESKTOP_HOST, timeout_s: int = GRADE_TIMEOUT_S,
               cpus: float = 2, memory_mb: int = 8192, platform: str | None = "linux/amd64",
               name: str | None = None, inv=None, purpose: str = PURPOSE):
    """sandbox.grade's steps with explicit resources: fresh container (--rm, named, labelled
    emx.sandbox=<purpose>, network none, in-container timeout), tests and patch in, test.sh.
    With `inv` (the harness's HostInvocation) the container is started through it, on its host
    and holding one of its slots, as `outcomes.grade_in` does. Returns (ExecResult of test.sh,
    raw reward read, detail dict)."""
    from compaction.deepswe import sandbox as SBX
    kw = {"lifetime_s": timeout_s + 600, "cpus": cpus, "memory_mb": memory_mb,
          "platform": platform}
    cm = (inv.start(image, **kw) if inv is not None else
          SBX.Sandbox.start(image, purpose, run_id=run_id, name=name, host=host, **kw))
    with cm as v:
        v.copy_in(Path(tests_dir), "/tests")
        v.put_file(patch, "/logs/artifacts/model.patch")
        r = v.exec("chmod +x /tests/test.sh && bash /tests/test.sh", timeout_s=timeout_s,
                   workdir=WORKSPACE)
        raw = v.exec(SBX.READ_REWARD_CMD, timeout_s=60, workdir="/")
        det = v.exec(READ_DETAIL_CMD, timeout_s=120, workdir="/")
        tails = {f: v.exec(f"tail -c 6000 /logs/verifier/{f} 2>/dev/null", timeout_s=60,
                           workdir="/").stdout for f in LOG_TAILS}
        tails |= {f: v.exec(f"tail -c 4000 /tmp_artifact_storage/{f} 2>/dev/null",
                            timeout_s=60, workdir="/").stdout for f in BUILD_LOG_TAILS}
        mem = v.exec(MEMORY_EVENTS_CMD, timeout_s=60, workdir="/")
        ep = v.exec(f"cut -d' ' -f2 {ENTRYPOINT_RC} 2>/dev/null", timeout_s=60, workdir="/")
    try:
        detail = json.loads(det.stdout or "{}")
    except json.JSONDecodeError:
        detail = {}
    detail["log_tails"] = tails
    detail |= memory_events(mem.rc, mem.stdout, mem.stderr)
    rc = ep.stdout.strip()
    detail["entrypoint_rc"] = int(rc) if rc.lstrip("-").isdigit() else None
    return r, raw, detail


MEMORY_EVENTS_CMD = "cat /sys/fs/cgroup/memory.events"


def memory_events(rc: int, stdout: str, stderr: str) -> dict:
    """OOM evidence for one grader container: the cgroup's `oom_kill` count (a test process
    killed while the container lived), or the container gone before the read (its main process
    killed, e.g. by the OOM killer; with --rm it can't be inspected afterwards)."""
    ev = {}
    for line in stdout.splitlines():
        k, _, v = line.partition(" ")
        if v.strip().isdigit():
            ev[k] = int(v)
    gone = rc != 0 and "no such container" in stderr.lower()
    return {"oom_kills": ev.get("oom_kill"), "container_gone": gone}


def oom_suspect(rc: int, detail: dict) -> bool:
    """An infrastructure failure to rerun with more memory, never a task failure."""
    return bool(detail.get("oom_kills")) or bool(detail.get("container_gone")) or (
        rc == 137 and detail.get("status") is None)


def grade_reward(image: str, tests_dir: str | Path, patch: bytes, **kw) -> tuple[dict, dict]:
    """(validated reward, detail): the harness's reader (`sandbox.parse_reward`) on our output."""
    from compaction.deepswe import sandbox as SBX
    from compaction.deepswe.sandbox import VerifierError
    r, raw, detail = run_grader(image, tests_dir, patch, **kw)
    if oom_suspect(r.rc, detail):  # an infrastructure failure, never a reward (astra r3 #1)
        raise VerifierError(f"OOM evidence in the grader container: oom_kills="
                            f"{detail.get('oom_kills')}, gone={detail.get('container_gone')}")
    if r.timed_out:
        raise VerifierError(f"verifier timed out (rc={r.rc}) after {r.seconds:.0f} s")
    if raw.rc != 0:
        raise VerifierError(f"reading the reward failed rc={raw.rc}: {raw.stderr[-500:]}")
    kind, _, text = raw.stdout.partition("\n")
    try:
        return SBX.parse_reward(kind.strip(), text), detail
    except VerifierError as e:
        raise VerifierError(f"{e}; test.sh rc={r.rc}; tail: {r.stdout[-800:]!r}") from None


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
