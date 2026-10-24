"""SWE-Interact's SWE-bench Pro family (25 tasks) as InteractTasks for the S2 harness.

Brief logs/s2/briefs/integrate-pro.md; feasibility logs/s2/swe_interact_50_feasibility.md
(sections 2-4). Source: the SWE-Interact checkout, `MT/swebenchpro_<instance_id>/`
(`interact_run.SI_TASKS`).

- **Ids.** `pro-<repo>-<sha8>`, e.g. `pro-ansible-4c5ce5a1` (repo lower-cased, sha8 = the
  upstream solution commit in the instance id). Images: `emx-tasks/swe-interact-pro:<repo>-<sha8>
  -v1`, built on the desktop Docker host by `scripts/build_pro.py`, never pulled from a registry.
- **Images** (deviation from Harbor, logged as for our DeepSWE v1.1 images): the task's own
  Dockerfile (its `ENTRYPOINT []` replaces the upstream image's `["/bin/bash"]`), then
  `SQUASH_SH`: the repo is reset to base, its tracked-file list kept, `.git` replaced by one
  "init" commit whose tree must hash equal to the base commit's tree. The result is then
  flattened (`FROM scratch` + `COPY --from`), so no lower layer keeps the old object store, and
  the config's ENV / WORKDIR are declared again. A profile.d line restores the image's PATH for
  login shells (`bash -lc`, as every sandbox exec runs), which Debian's /etc/profile resets.
- **Grading** (`TEST_SH` + `GRADER_PY`, materialised into `<task_dir>/tests/`): the DeepSWE
  verifier contract (model.patch in, reward.json with f2p/p2p counts out) so
  `outcomes.grade_patch` and `sandbox.parse_reward` work unchanged. The upstream verifier's
  `git checkout <solution sha> -- <test files>` needs the solution commit, which the squash
  removed; instead the files `test_patch` touches are reset to HEAD (the base) and the config's
  `test_patch` is applied. Its file list equals the gold checkout's in all 25 tasks (unit test),
  and the squash checks, while the solution commit still exists, that base + test_patch gives
  the solution's blobs and modes on every such path (astra r1 #2), so the graded tree is the
  same. Then the official `run_script.sh` and `parser.py`;
  reward 1 iff every fail_to_pass and pass_to_pass test is PASSED (as `canonical_test.sh`).
- **Plan file** (logged deviation): Pro's step texts save the plan to `/app/plan.md`, inside the
  repo, where it would enter `git status`, the trap snapshots and the graded diff. They are
  rewritten to `/tmp/plan.md`, DeepSWE's location, which `run_base` and the fingerprint read.
  The persona never names the file. The grader also drops a root `plan.md` from model.patch.
- **Usable** = base fails and gold passes in `results/sprint/families/pro/base_gold.json` with
  the current verifier, and the image is CLEAN in imgval's
  `results/sprint/task_validation/summary.json`, all three records on the same immutable image
  id; `TaskInfo.image` is that id, so a re-pointed tag can't be run. `load_task` refuses any
  other task unless `require_usable=False` (the build's own checks).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from compaction.s2 import config as C

PREFIX = "pro-"
PURPOSE = "fam-pro"  # emx.sandbox label of every container the build starts
IMAGE_REPO = "emx-tasks/swe-interact-pro"
IMAGE_VERSION = "v1"
STEPS = ("01_plan", "02_implement")
DATA_DIR = C.REPO_ROOT / "data" / "raw" / "sprint" / "families" / "pro"
RESULTS_DIR = C.REPO_ROOT / "results" / "sprint" / "families" / "pro"
VALIDATION_SUMMARY = C.REPO_ROOT / "results" / "sprint" / "task_validation" / "summary.json"
PLAN_FROM, PLAN_TO = "/app/plan.md", "/tmp/plan.md"
_INSTANCE = re.compile(r"^instance_(?P<owner>.+?)__(?P<repo>.+)-(?P<sha>[0-9a-f]{40})(?:-v\w+)?$")


def si_root() -> Path:
    from compaction.s2.interact_run import SI_TASKS
    return SI_TASKS


def parse_list(x) -> list[str]:
    """A config list field: a list, a JSON string or a Python-literal string (as upstream)."""
    if isinstance(x, list):
        return x
    if not x:
        return []
    try:
        return json.loads(x)
    except json.JSONDecodeError:
        return ast.literal_eval(x)


_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def patch_paths(text: str) -> list[str]:
    out: list[str] = []
    for a, b in _DIFF_FILE.findall(text):
        for p in (b, a):
            if p not in out:
                out.append(p)
    return out


def short_id(instance_id: str) -> str:
    """`instance_ansible__ansible-4c5ce5a1...-v105...` -> `pro-ansible-4c5ce5a1`."""
    m = _INSTANCE.match(instance_id)
    if not m:
        raise ValueError(f"unrecognised SWE-bench Pro instance id: {instance_id!r}")
    return f"{PREFIX}{m['repo'].lower()}-{m['sha'][:8]}"


def image_tag(task_id: str) -> str:
    return f"{IMAGE_REPO}:{task_id.removeprefix(PREFIX)}-{IMAGE_VERSION}"


def stage_tag(task_id: str) -> str:
    return f"{IMAGE_REPO}-stage:{task_id.removeprefix(PREFIX)}"


@dataclass(frozen=True)
class ProSource:
    """One task in the SWE-Interact checkout."""
    task_id: str
    src_dir: Path
    config: dict

    @property
    def instance_id(self) -> str:
        return self.config["instance_id"]

    @property
    def base_commit(self) -> str:
        return self.config["base_commit"]

    @property
    def solution_sha(self) -> str:
        return _INSTANCE.match(self.instance_id)["sha"]

    @property
    def language(self) -> str | None:
        return self.config.get("repo_language")

    @property
    def from_image(self) -> str:
        for line in (self.src_dir / "environment" / "Dockerfile").read_text().splitlines():
            if line.startswith("FROM "):
                return line.split()[1]
        raise ValueError(f"no FROM line in {self.src_dir}/environment/Dockerfile")

    @property
    def fail_to_pass(self) -> list[str]:
        return parse_list(self.config.get("fail_to_pass"))

    @property
    def pass_to_pass(self) -> list[str]:
        return parse_list(self.config.get("pass_to_pass"))


def sources(root: Path | None = None) -> dict[str, ProSource]:
    """task_id -> ProSource for every `swebenchpro_*` dir (sorted by task id)."""
    out = {}
    for d in sorted(Path(root or si_root()).glob("swebenchpro_*")):
        cfg = json.loads((d / "tests" / "config.json").read_text())
        tid = short_id(cfg["instance_id"])
        if tid in out:
            raise ValueError(f"two Pro tasks map to {tid}: {out[tid].src_dir}, {d}")
        out[tid] = ProSource(tid, d, cfg)
    return out


def task_ids(*, usable_only: bool = False, root: Path | None = None, **_kw) -> list[str]:
    ids = sorted(sources(root))
    return [t for t in ids if usable(t, root=root)[0]] if usable_only else ids


def step_texts(src: ProSource) -> dict[str, str]:
    """The step instructions, verbatim except the plan path (module docstring)."""
    return {s: (src.src_dir / "steps" / s / "instruction.md").read_text()
            .replace(PLAN_FROM, PLAN_TO) for s in STEPS}


# ------------------------------------------------------------------------------------------
# Image build inputs (scripts/build_pro.py runs them on the desktop)
# ------------------------------------------------------------------------------------------

# Run in the stage image as the last RUN. Fails the build on any mismatch. APP_DIR, PROFILE_D and
# SQUASH_RECORD are overridable for the unit tests only.
SQUASH_SH = r"""set -eu
trap 'echo "emx squash: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
APP="${APP_DIR:-/app}"
cd "$APP"
B="$1"
S="$2"
TP="${TEST_PATCH:-/tmp/emx_test.patch}"
W=$(mktemp -d)  # scratch: old .git, packs, path lists
git config --global --add safe.directory "$APP"
git reset -q --hard "$B"
git clean -fdq
test "$(git rev-parse HEAD)" = "$B"
# dirty content inside an initialised submodule (openlibrary's vendor/infogami) is left as
# upstream graded it; anything else must be clean
ST=$(git status --porcelain --ignore-submodules=dirty)
test -z "$ST"
TREE=$(git rev-parse 'HEAD^{tree}')
# base + test_patch must give the solution commit's blobs and modes on every path the patch
# touches (the grader's stand-in for upstream's `git checkout <solution> -- <test files>`)
git cat-file -e "$S^{commit}"
IDX=$(mktemp)
GIT_INDEX_FILE="$IDX" git read-tree "$B"
GIT_INDEX_FILE="$IDX" git apply --cached --binary "$TP"
T2=$(GIT_INDEX_FILE="$IDX" git write-tree)
rm -f "$IDX"
test -n "$(git diff --name-only "$B" "$T2")"
git diff --name-only "$B" "$T2" > $W/tp_paths
while IFS= read -r p; do
  git diff --quiet "$T2" "$S" -- "$p" || {
    echo "emx squash: base + test_patch differs from the solution at $p" >&2; exit 1; }
done < $W/tp_paths
rm -f $W/tp_paths
git ls-files -s | awk '$1 == "160000" {print $2 "\t" substr($0, index($0, "\t") + 1)}' \
  > $W/gitlinks
# initialised submodules (teleport webassets, openlibrary vendor/*) keep a git dir, else git
# fails in the image; no gold or test patch touches a submodule. Each store is rebuilt in place
# as a shallow store holding only its checked-out commit and that commit's tree, with its
# config (core.worktree): no refs, ancestors, reflog, stash, nested or dormant module stores.
: > $W/modules
while IFS="$(printf '\t')" read -r sha path; do
  [ -e "$path/.git" ] || continue
  G=$(git -C "$path" rev-parse --absolute-git-dir)
  case "$G" in
    "$APP/.git/modules/"*) ;;
    *) echo "emx squash: submodule $path keeps its git dir outside .git/modules: $G" >&2
       exit 1 ;;
  esac
  H=$(git -C "$path" rev-parse HEAD)
  { echo "$H"; git -C "$path" rev-list --objects "$H^{tree}" | cut -d' ' -f1; } \
    | git -C "$path" pack-objects -q --stdout > $W/sub.pack
  find "$G" -mindepth 1 -maxdepth 1 ! -name config -exec rm -rf {} +
  mkdir -p "$G/objects/pack" "$G/objects/info" "$G/refs/heads" "$G/refs/tags"
  echo "$H" > "$G/HEAD"
  echo "$H" > "$G/shallow"
  git -C "$path" index-pack --stdin < $W/sub.pack > /dev/null
  rm -f $W/sub.pack
  git -C "$path" read-tree "$H"
  git -C "$path" update-index -q --refresh > /dev/null || true
  test "$(git -C "$path" rev-parse HEAD)" = "$H"
  test -z "$(git -C "$path" for-each-ref)"
  printf '%s\n' "${G#"$APP/.git/modules/"}" >> $W/modules
done < $W/gitlinks
# the new repo holds exactly the base tree's objects (copied, not re-added from the worktree,
# so CRLF blobs, modes and gitlinks stay byte for byte: openlibrary-0a90 has CRLF files)
git rev-list --objects "$TREE" | cut -d' ' -f1 | git pack-objects -q --stdout > $W/base.pack
mv .git $W/oldgit
git init -q
git symbolic-ref HEAD refs/heads/main
git index-pack --stdin < $W/base.pack > /dev/null
rm -f $W/base.pack
git read-tree "$TREE"
git update-index -q --refresh > /dev/null || true
while IFS= read -r rel; do
  mkdir -p ".git/modules/$(dirname "$rel")"
  mv "$W/oldgit/modules/$rel" ".git/modules/$rel"
done < $W/modules
C=$(GIT_AUTHOR_NAME=emx GIT_AUTHOR_EMAIL=emx@localhost GIT_COMMITTER_NAME=emx \
  GIT_COMMITTER_EMAIL=emx@localhost GIT_AUTHOR_DATE=2000-01-01T00:00:00Z \
  GIT_COMMITTER_DATE=2000-01-01T00:00:00Z git commit-tree "$TREE" -m "init")
git update-ref refs/heads/main "$C"
test "$(git rev-parse 'HEAD^{tree}')" = "$TREE"
test "$(git rev-list --all | wc -l)" -eq 1
ST=$(git status --porcelain --ignore-submodules=dirty)
test -z "$ST"
rm -rf "$W"
git reflog expire --expire=now --all
git gc -q --prune=now
printf 'export PATH=%s\n' "'$PATH'" > "${PROFILE_D:-/etc/profile.d}/zz-emx-docker-path.sh"
printf '%s %s test_patch_equiv=1\n' "$B" "$TREE" > "${SQUASH_RECORD:-/emx_squash.txt}"
"""


SQUASH_TIMEOUT_S = 1800


def stage_dockerfile(src: ProSource) -> str:
    """The task's Dockerfile, then the squash (build context: build/ with the task's
    repo_exec_server.py, squash.sh and test.patch, which the squash deletes again)."""
    text = (src.src_dir / "environment" / "Dockerfile").read_text().rstrip("\n")
    return (text + "\n\n# emx: squash the repo to one commit at base (compaction.s2.families.pro)\n"
            "COPY squash.sh /tmp/emx_squash.sh\n"
            "COPY test.patch /tmp/emx_test.patch\n"
            f"RUN timeout -k 5 {SQUASH_TIMEOUT_S} bash /tmp/emx_squash.sh {src.base_commit} "
            f"{src.solution_sha} && rm -f /tmp/emx_squash.sh /tmp/emx_test.patch\n")


def _quote_env(value: str) -> str:
    """A double-quoted Dockerfile ENV value; `$` escaped so Docker doesn't expand it again."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def final_dockerfile(task_id: str, stage: str, image_config: dict) -> str:
    """Flatten `stage` (one layer: nothing below it keeps the old .git) and declare its config
    again. `image_config` is `docker image inspect`'s .Config of the stage image."""
    lines = [f"FROM {stage} AS stage", "FROM scratch", "COPY --from=stage / /"]
    for kv in image_config.get("Env") or []:
        k, _, v = kv.partition("=")
        lines.append(f"ENV {k}={_quote_env(v)}")
    lines.append(f"WORKDIR {image_config.get('WorkingDir') or '/app'}")
    if image_config.get("User"):
        lines.append(f"USER {image_config['User']}")
    lines += ["ENTRYPOINT []", 'CMD ["/bin/bash"]',
              f'LABEL emx.family="pro" emx.task="{task_id}" emx.squashed="1"']
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------
# The verifier (runs inside a fresh container of the task image; python3 is in every image)
# ------------------------------------------------------------------------------------------

TEST_SH = r"""#!/bin/bash
# SWE-bench Pro verifier for the emx S2 harness (compaction.s2.families.pro).
# DeepSWE's contract: /logs/artifacts/model.patch in, /logs/verifier/reward.json out;
# no reward.json on an infrastructure failure -> the -1 reward.txt crash sentinel.
set -uo pipefail
trap 'if [ ! -f /logs/verifier/reward.json ]; then mkdir -p /logs/verifier; echo -1 > /logs/verifier/reward.txt; fi' EXIT
mkdir -p /logs/verifier /logs/artifacts
cd /app || exit 6
python3 /tests/grader.py prepare || exit $?
[ -f /logs/verifier/reward.json ] && exit 0   # model.patch didn't apply -> graded 0
python3 /tests/grader.py grade || exit $?
for f in run-script-stdout.txt run-script-stderr.txt; do
  echo "===== tail of $f ====="; tail -n 150 "/logs/verifier/$f" 2>/dev/null
done
echo "[verifier] reward.json=$(cat /logs/verifier/reward.json)"
"""

GRADER_PY = r'''#!/usr/bin/env python3
"""SWE-bench Pro grader for the emx S2 harness (compaction.s2.families.pro).

prepare: apply /logs/artifacts/model.patch (a root plan.md excluded) to the pristine squashed
         repo; a patch that doesn't apply -> reward.json with apply_failed=1. Then reset the
         files test.patch touches to HEAD (the base) and apply test.patch (failure = infra).
grade:   the official run_script.sh over selected_test_files_to_run, the official parser.py,
         then reward.json: reward 1 iff every fail_to_pass and pass_to_pass test is PASSED.
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys

TESTS = os.environ.get("TESTS_DIR", "/tests")
VER = os.environ.get("VERIFIER_DIR", "/logs/verifier")
APP = os.environ.get("APP_DIR", "/app")
ART = os.environ.get("ARTIFACTS_DIR", "/logs/artifacts")
EXCLUDE = ("plan.md",)


def parse_list(x):
    if isinstance(x, list):
        return x
    if not x:
        return []
    try:
        return json.loads(x)
    except ValueError:
        return ast.literal_eval(x)


def config():
    with open(os.path.join(TESTS, "config.json")) as f:
        return json.load(f)


def patch_paths(text):
    out = []
    for a, b in re.findall(r"^diff --git a/(\S+) b/(\S+)", text, re.M):
        for p in (b, a):
            if p not in out:
                out.append(p)
    return out


def git(*args, check=True):
    r = subprocess.run(["git"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if check and r.returncode != 0:
        sys.stderr.write("git %s failed rc=%d: %s\n" % (" ".join(args), r.returncode, r.stderr))
        sys.exit(3)
    return r


def write_reward(obj):
    os.makedirs(VER, exist_ok=True)
    with open(os.path.join(VER, "reward.json"), "w") as f:
        json.dump(obj, f)
    print("[grade] reward.json=" + json.dumps(obj))


def counts(passed, f2p, p2p):
    fp = len(f2p & passed)
    pp = len(p2p & passed)
    solved = bool(f2p) and f2p <= passed and p2p <= passed
    return {"reward": int(solved), "f2p_total": len(f2p), "f2p_passed": fp,
            "p2p_total": len(p2p), "p2p_passed": pp,
            "f2p": fp / len(f2p) if f2p else 0.0, "p2p": pp / len(p2p) if p2p else 1.0}


def prepare():
    os.chdir(APP)
    git("config", "--global", "--add", "safe.directory", APP, check=False)
    if git("status", "--porcelain", "--ignore-submodules=dirty").stdout.strip():
        sys.stderr.write("the task repo is not pristine before grading\n")
        sys.exit(3)
    cfg = config()
    mp = os.path.join(ART, "model.patch")
    if os.path.exists(mp) and open(mp, "rb").read().strip():
        args = ["apply", "--binary", "--whitespace=nowarn"]
        args += ["--exclude=" + e for e in EXCLUDE] + [mp]
        r = git(*args, check=False)
        if r.returncode != 0:
            print("[grade] model.patch failed to apply: " + r.stderr[-2000:])
            f2p = set(parse_list(cfg.get("fail_to_pass")))
            p2p = set(parse_list(cfg.get("pass_to_pass")))
            out = counts(set(), f2p, p2p)
            out["apply_failed"] = 1
            write_reward(out)
            return
    tp = os.path.join(TESTS, "test.patch")
    with open(tp) as f:
        paths = patch_paths(f.read())
    for p in paths:
        if git("cat-file", "-e", "HEAD:" + p, check=False).returncode == 0:
            git("checkout", "HEAD", "--", p)
        elif os.path.lexists(p):
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
    git("apply", "--binary", "--whitespace=nowarn", tp)


def grade():
    os.chdir(APP)
    os.makedirs(VER, exist_ok=True)
    cfg = config()
    files = parse_list(cfg.get("selected_test_files_to_run"))
    out_log = os.path.join(VER, "run-script-stdout.txt")
    err_log = os.path.join(VER, "run-script-stderr.txt")
    cmd = ["bash", os.path.join(TESTS, "run_script.sh")] + ([",".join(files)] if files else [])
    with open(out_log, "w") as o, open(err_log, "w") as e:
        rc = subprocess.call(cmd, stdout=o, stderr=e)
    res = os.path.join(VER, "output.json")
    if os.path.exists(res):
        os.remove(res)
    prc = subprocess.call(["python3", os.path.join(TESTS, "parser.py"), out_log, err_log, res])
    f2p = set(parse_list(cfg.get("fail_to_pass")))
    p2p = set(parse_list(cfg.get("pass_to_pass")))
    if prc != 0 or not os.path.exists(res):
        # as canonical_test.sh (set -e): a failed parser, or no parser output, scores 0
        out = counts(set(), f2p, p2p)
        out.update(parser_failed=1, run_script_rc=rc, parser_rc=prc)
        write_reward(out)
        return
    with open(res) as f:
        tests = json.load(f).get("tests", [])
    passed = {t.get("name", "") for t in tests if t.get("status") == "PASSED"}
    out = counts(passed, f2p, p2p)
    out.update(run_script_rc=rc, n_reported=len(tests), n_passed_reported=len(passed),
               failing=sorted((f2p | p2p) - passed)[:50])
    write_reward(out)


if __name__ == "__main__":
    {"prepare": prepare, "grade": grade}[sys.argv[1]]()
'''


def _write(path: Path, data: str | bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    b = data.encode() if isinstance(data, str) else data
    if not path.exists() or path.read_bytes() != b:
        path.write_bytes(b)
    if mode is not None:
        path.chmod(mode)


def materialize(src: ProSource, out_root: Path | None = None) -> Path:
    """Write the task dir the harness reads (idempotent): tests/ (our verifier, the official
    run_script.sh and parser.py, config.json, test.patch), solution/solution.patch (the
    reference patch, kept on the laptop only) and build/ (the stage build context)."""
    d = Path(out_root or DATA_DIR) / src.task_id
    t = d / "tests"
    _write(t / "test.sh", TEST_SH, 0o755)
    _write(t / "grader.py", GRADER_PY, 0o755)
    for name in ("config.json", "run_script.sh", "parser.py"):
        _write(t / name, (src.src_dir / "tests" / name).read_bytes())
    _write(t / "test.patch", src.config["test_patch"])
    _write(d / "solution" / "solution.patch", src.config["patch"])
    b = d / "build"
    _write(b / "Dockerfile", stage_dockerfile(src))
    _write(b / "squash.sh", SQUASH_SH)
    _write(b / "test.patch", src.config["test_patch"])
    _write(b / "repo_exec_server.py", (src.src_dir / "environment" / "repo_exec_server.py")
           .read_bytes())
    return d


BUILD_FILES = ("Dockerfile", "squash.sh", "test.patch", "repo_exec_server.py")
VERIFIER_FILES = ("test.sh", "grader.py", "config.json", "run_script.sh", "parser.py",
                  "test.patch")


def _files_sha256(d: Path, names: tuple[str, ...], extra: str = "") -> str:
    h = hashlib.sha256(extra.encode())
    for name in names:
        h.update(name.encode() + b"\0" + (d / name).read_bytes() + b"\0")
    return h.hexdigest()


def build_inputs_sha256(task_dir: Path) -> str:
    """Every input of the image: the stage build context and the final-Dockerfile generator."""
    import inspect
    gen = inspect.getsource(final_dockerfile) + inspect.getsource(_quote_env)
    return _files_sha256(Path(task_dir) / "build", BUILD_FILES, gen)


def verifier_sha256(task_dir: Path) -> str:
    return _files_sha256(Path(task_dir) / "tests", VERIFIER_FILES)


_DEP_FILE = re.compile(r"(^|/)(go\.mod|package\.json|requirements[^/]*\.txt|setup\.py|"
                       r"setup\.cfg|pyproject\.toml)$")


def gold_added_dependencies(src: ProSource) -> dict[str, list[str]]:
    """Lines the reference patch adds to dependency manifests ({file: [added lines]}): what an
    offline image lacks when the gold build needs the network."""
    out: dict[str, list[str]] = {}
    cur = None
    for line in src.config["patch"].splitlines():
        m = re.match(r"^diff --git a/\S+ b/(\S+)", line)
        if m:
            cur = m[1] if _DEP_FILE.search(m[1]) else None
        elif cur and line.startswith("+") and not line.startswith("+++"):
            text = line[1:].strip()
            if text and not text.startswith(("//", "#")):
                out.setdefault(cur, []).append(text)
    return out


def collect_command(squashed_head: str) -> str:
    return ("cd /app && mkdir -p /logs/artifacts && git config --global --add safe.directory "
            f"/app && git diff --binary {squashed_head} HEAD > /logs/artifacts/model.patch")


# ------------------------------------------------------------------------------------------
# Build records and usability
# ------------------------------------------------------------------------------------------


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def images_manifest(results_dir: Path | None = None) -> dict:
    """{task_id: {image, image_id, from_image, from_digest, squashed_head, tree, ...}}."""
    return _load(Path(results_dir or RESULTS_DIR) / "images.json").get("tasks", {})


def base_gold(results_dir: Path | None = None) -> dict:
    return _load(Path(results_dir or RESULTS_DIR) / "base_gold.json").get("tasks", {})


def validation_verdict(image: str, summary: Path | None = None,
                       row: dict | None = None) -> str | None:
    """imgval's verdict for one rebuilt image: "CLEAN" only when its row (summary.json `rows`,
    matched on `image`) has `verdict` (what an agent in the container can reach), `layers`
    (solution text or an old .git in any `docker save` layer) and, when present,
    `hidden_tests` all CLEAN. "LEAK" or "ERROR" if any of them says so; None when there is no
    row or a field is missing (= not usable). Schema from imgval, 24 Oct."""
    if row is None:
        rows = _load(Path(summary or VALIDATION_SUMMARY)).get("rows")
        row = next((r for r in rows or [] if isinstance(r, dict) and r.get("image") == image),
                   None)
    if row is None:
        return None
    fields = [row.get("verdict"), row.get("layers")]
    if row.get("hidden_tests") is not None:
        fields.append(row["hidden_tests"])
    for v in ("LEAK", "ERROR"):
        if v in fields:
            return v
    return "CLEAN" if all(f == "CLEAN" for f in fields) else None


def validation_row(image_id: str, summary: Path | None = None) -> dict | None:
    rows = _load(Path(summary or VALIDATION_SUMMARY)).get("rows")
    return next((r for r in rows or [] if isinstance(r, dict) and r.get("image_id") == image_id
                 and str(r.get("key", "")).startswith("pro_rebuilt/")), None)


def usable(task_id: str, results_dir: Path | None = None, summary: Path | None = None,
           out_root: Path | None = None, root: Path | None = None,
           img: dict | None = None) -> tuple[bool, str]:
    """(usable, reason). All three records must be about the same immutable image id, and the
    base/gold check about the current verifier: built; base fails and gold passes; imgval
    CLEAN (verdict, layers, hidden_tests)."""
    img = img if img is not None else images_manifest(results_dir).get(task_id)
    if not img:
        return False, "image not built"
    if img.get("check_problems"):
        return False, "image check: " + "; ".join(img["check_problems"])
    bg = base_gold(results_dir).get(task_id) or {}
    if not bg.get("pass"):
        return False, f"base/gold check not passed: {bg.get('reason') or 'not run'}"
    if bg.get("image_id") != img["image_id"] or bg.get("graded_by") != "image_id":
        return False, "base/gold check was not run on this image id"
    d = materialize(sources(root)[task_id], out_root)
    if bg.get("verifier_sha256") != verifier_sha256(d):
        return False, "base/gold check was run with a different verifier"
    row = validation_row(img["image_id"], summary)
    v = None if row is None else validation_verdict(row.get("image", ""), summary, row=row)
    if v != "CLEAN":
        return False, f"image validation verdict {v or 'missing'}"
    return True, "ok"


class ProTaskNotUsable(RuntimeError):
    pass


def task_info(src: ProSource, results_dir: Path | None = None, out_root: Path | None = None,
              img: dict | None = None):
    """The TaskInfo the grader and run configs read (image from the build manifest; `img` =
    the manifest row `usable()` validated, so the two can't be read apart)."""
    from compaction.s2.tasks import TaskInfo
    img = img if img is not None else images_manifest(results_dir).get(src.task_id)
    if not img:
        raise ProTaskNotUsable(f"{src.task_id}: image not built (scripts/build_pro.py)")
    d = materialize(src, out_root)
    # the immutable id the checks were run on, not the movable tag (astra r1 #1)
    return TaskInfo(task_id=src.task_id, rank=None, language=src.language,
                    image=img["image_id"],
                    collect_command=collect_command(img["squashed_head"]),
                    instruction=(src.src_dir / "instruction.md").read_text(), task_dir=str(d),
                    tests_dir=str(d / "tests"),
                    solution_files=patch_paths(src.config["patch"]))


def load_task(task_id: str, *, require_usable: bool = True, root: Path | None = None,
              results_dir: Path | None = None, out_root: Path | None = None,
              summary: Path | None = None):
    """The InteractTask for one Pro task (same object as interact_run.load_task's)."""
    from compaction.s2.interact_run import InteractTask, _rel
    srcs = sources(root)
    if task_id not in srcs:
        raise KeyError(f"no SWE-bench Pro task {task_id!r}")
    img = images_manifest(results_dir).get(task_id)  # one snapshot for the check and the info
    if require_usable:
        ok, why = usable(task_id, results_dir, summary, out_root, root, img=img)
        if not ok:
            raise ProTaskNotUsable(f"{task_id}: {why}")
    src = srcs[task_id]
    spec = src.src_dir / "instruction.md"
    return InteractTask(
        task_id=task_id, info=task_info(src, results_dir, out_root, img=img),
        persona=(src.src_dir / "environment" / "user-server" / "persona.md").read_text(),
        steps=step_texts(src), spec_path=_rel(spec),
        spec_sha256=hashlib.sha256(spec.read_bytes()).hexdigest())


# ------------------------------------------------------------------------------------------
# The family interface (compaction.s2.families docstring)
# ------------------------------------------------------------------------------------------


def usable_task_ids(**kw) -> list[str]:
    return task_ids(usable_only=True, **kw)


def resources(task_id: str, root: Path | None = None) -> dict:
    """The container size Pro tasks are graded with, by `grade` and the base/gold check alike
    (GRADE_CPUS / GRADE_MEMORY_MB, 16 GB on an OOM rerun), and the workdir; `upstream` is
    task.toml's [environment] (1 CPU, 4 GB, 10 GB storage for all 25)."""
    import tomllib
    src = sources(root)[task_id]
    env = tomllib.loads((src.src_dir / "task.toml").read_text()).get("environment", {})
    return {"cpus": float(GRADE_CPUS), "memory_mb": GRADE_MEMORY_MB,
            "storage_mb": env.get("storage_mb"), "workdir": "/app",
            "ready_file": None,  # ENTRYPOINT [] in every Pro image: nothing starts services
            "upstream": {k: env.get(k) for k in ("cpus", "memory_mb", "storage_mb")}}


DESKTOP_HOST = "ssh://rig"  # the images exist only on the desktop's daemon
GRADE_TIMEOUT_S = 3000  # the task's own step-05 verifier timeout
# One grading policy for the base/gold qualification and the harness (astra r3 #3): 4 CPU /
# 8 GB (desktop rule, 24 Oct); a run with an OOM kill is an infrastructure failure, rerun once
# at 16 GB; an unreadable OOM counter is an infrastructure failure too.
GRADE_CPUS = 4
GRADE_MEMORY_MB = 8192
OOM_RETRY_MEMORY_MB = 16384
# the container's own cgroup counter (v2, else v1)
OOM_CMD = ("cat /sys/fs/cgroup/memory.events /sys/fs/cgroup/memory/memory.oom_control "
           "2>/dev/null | awk '$1 == \"oom_kill\" {print $2; found = 1} END {exit !found}'")


class VerifierOutcome(RuntimeError):
    """A verifier run that produced no valid reward (an infrastructure failure)."""

    def __init__(self, msg: str, stdout: str = "", ooms: int | None = 0):
        super().__init__(msg)
        self.stdout, self.ooms = stdout, ooms


def run_verifier(start, image: str, tests_dir, patch: bytes, *, memory_mb: int,
                 timeout_s: int = GRADE_TIMEOUT_S) -> tuple[dict, str, int]:
    """sandbox.grade's steps in a container from `start(image, **kw)` (a Sandbox.start or a
    HostInvocation.start): tests and patch in, test.sh, the reward validated by
    parse_reward; plus the container's OOM-kill count, read before it is removed. Returns
    (reward, verifier output, oom kills); raises VerifierOutcome."""
    from compaction.deepswe import sandbox as SBX
    with start(image, lifetime_s=timeout_s + 300, cpus=GRADE_CPUS, memory_mb=memory_mb,
               platform=C.PLATFORM or None) as v:
        v.copy_in(Path(tests_dir), "/tests")
        v.put_file(patch, "/logs/artifacts/model.patch")
        r = v.exec("chmod +x /tests/test.sh && bash /tests/test.sh", timeout_s=timeout_s,
                   workdir="/app")
        raw = v.exec(SBX.READ_REWARD_CMD, timeout_s=30, workdir="/")
        o = v.exec(OOM_CMD, timeout_s=30, workdir="/")
    out = r.stdout + "\n--- stderr ---\n" + r.stderr
    ooms = int(o.stdout.split()[0]) if o.rc == 0 and o.stdout.split()[:1] and \
        o.stdout.split()[0].isdigit() else None
    if ooms is None:
        raise VerifierOutcome(f"OOM state unknown (rc={o.rc}): {o.stderr[-200:]}", out, None)
    if r.timed_out:
        raise VerifierOutcome(f"verifier timed out (rc={r.rc}) after {r.seconds:.0f} s", out,
                              ooms)
    if raw.rc != 0:
        raise VerifierOutcome(f"reading the reward failed rc={raw.rc}: {raw.stderr[-500:]}",
                              out, ooms)
    kind, _, text = raw.stdout.partition("\n")
    try:
        return SBX.parse_reward(kind.strip(), text), out, ooms
    except SBX.VerifierError as e:
        raise VerifierOutcome(f"{e}; test.sh rc={r.rc}; tail: {r.stdout[-800:]!r}", out,
                              ooms) from None


def grade_with_policy(start, image: str, tests_dir, patch: bytes, *,
                      timeout_s: int = GRADE_TIMEOUT_S, slot=None) -> dict:
    """The grading policy (above). Returns {"reward": dict | None, "error": str | None,
    "attempts": [{"memory_mb", "oom_kills"}], "output": str}; `slot()` (a context manager
    factory) wraps each attempt's container."""
    from contextlib import nullcontext
    attempts: list[dict] = []
    res: dict = {}
    for mem in (GRADE_MEMORY_MB, OOM_RETRY_MEMORY_MB):
        try:
            with (slot or nullcontext)():
                rew, out, ooms = run_verifier(start, image, tests_dir, patch, memory_mb=mem,
                                              timeout_s=timeout_s)
            res = {"reward": rew, "error": None, "output": out}
        except VerifierOutcome as e:
            ooms = e.ooms
            res = {"reward": None, "error": str(e)[:2000], "output": e.stdout}
        attempts.append({"memory_mb": mem, "oom_kills": ooms})
        if ooms == 0:
            break
        if ooms is None:  # unknown: an infrastructure failure; not retried at more memory
            break
    if attempts[-1]["oom_kills"]:
        res = {"reward": None, "output": res.get("output", ""),
               "error": f"OOM-killed at {attempts[-1]['memory_mb']} MB (infrastructure)"}
    res["attempts"] = attempts
    return res


def grade(task, patch: bytes, run_id: str, *, host: str | None = DESKTOP_HOST, inv=None,
          timeout_s: int = GRADE_TIMEOUT_S) -> dict:
    """The families grade callable (`outcomes.grade_patch`'s output shape) under the same
    policy as the base/gold check. `task` is an InteractTask or its TaskInfo. With `inv` (the
    harness's HostInvocation) each container starts through it, on its host and in one of its
    slots; else a plain sandbox on `host`. Never calls `outcomes.grade_patch`, whose family
    hook routes here."""
    from functools import partial

    from compaction.deepswe import sandbox as SBX
    info = getattr(task, "info", task)
    start = inv.start if inv is not None else partial(
        SBX.Sandbox.start, purpose=C.PURPOSE, run_id=run_id, host=host)
    res = grade_with_policy(start, info.image, info.tests_dir, patch, timeout_s=timeout_s)
    rew = res["reward"]
    if rew is None:
        return {"reward": None, "grade_error": res["error"][:1000], "attempts": res["attempts"]}
    f2p = rew["f2p_passed"] / rew["f2p_total"] if rew["f2p_total"] else None
    p2p = rew["p2p_passed"] / rew["p2p_total"] if rew["p2p_total"] else None
    return {"reward": rew["reward"], "f2p": f2p, "p2p": p2p, "grade_error": None,
            **{k: rew[k] for k in ("f2p_passed", "f2p_total", "p2p_passed", "p2p_total")}}
