"""Build and check the 25 SWE-bench Pro task images on the desktop Docker host.

Brief logs/s2/briefs/integrate-pro.md; design in `compaction.s2.families.pro`'s docstring.
Per task, all on `--host` (default ssh://rig; never the laptop's daemon):
1. pull the upstream image (`jefzda/sweap-images:<tag>`), record its RepoDigest;
2. build the stage image (the task's Dockerfile + the squash), then the flattened final image
   `emx-tasks/swe-interact-pro:<repo>-<sha8>-v1`, and remove the stage tag;
3. check a container of it: one commit, tree = base tree, solution commit absent, only
   refs/heads/main, clean status, `bash -lc` PATH = the image's PATH, python3 and git present;
4. grade the untouched base (must be reward 0) and the reference patch (must be reward 1).

Writes results/sprint/families/pro/images.json and base_gold.json (merged per task, so a rerun
resumes) and each check's verifier output under data/raw/sprint/families/pro/<task>/checks/.

Resource rule (main session, 24 Oct, second version): at most 4 containers of ours at once,
each --cpus 4 --memory 8g, at most 4 pulls at once, and no new container while the host has
under 10 GB available. A grading with an OOM kill (the container's cgroup oom_kill counter) is
an infrastructure failure, rerun once at 16 GB. Builds hold one of the same 4 slots and the squash RUN has an
in-container timeout, but BuildKit offers no per-build CPU/memory limit, so build steps run
unlimited (astra r2 #2, deferred: the steps are git and file copies; rig's available memory is
logged before each slot is taken). Every container: --rm, a name, label emx.sandbox=fam-pro, an
in-container timeout (`compaction.deepswe.sandbox.Sandbox`).

    uv run python scripts/build_pro.py run [--tasks pro-ansible-4c5ce5a1 ...] [--rebuild]
    uv run python scripts/build_pro.py clean-check
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from compaction.deepswe import sandbox as SBX
from compaction.s2.families import pro as P

HOST = P.DESKTOP_HOST
SSH_TARGET = "rig"
MIN_AVAILABLE_GB = 10.0
CPUS = P.GRADE_CPUS  # desktop rule (main session, 24 Oct); grading shares pro's policy
MEMORY_MB = P.GRADE_MEMORY_MB
PULL_TIMEOUT_S = 3600
BUILD_TIMEOUT_S = 3600
GRADE_TIMEOUT_S = P.GRADE_TIMEOUT_S
CHECK_LIFETIME_S = 1200
# the diff id of an empty layer tar (what metadata-only steps such as WORKDIR leave)
EMPTY_LAYER = "sha256:5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef"

# builds count against the container slots: their RUN steps are containers too (astra r1 #3)
_containers = threading.Semaphore(4)
_pulls = threading.Semaphore(4)
# main session, 24 Oct (rig overloaded while the agent batches run): at most 2 builds at once,
# each started only while the host's 1-minute load is under MAX_LOAD_TO_BUILD
_builds = threading.Semaphore(2)
MAX_LOAD_TO_BUILD = 16.0
_write_lock = threading.Lock()
LOCK_FILE = P.RESULTS_DIR / ".build_pro.lock"


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[build_pro {utc()}] {msg}", flush=True)


# BuildKit's client session over ssh:// can fail to come up when many docker -H ssh clients
# share the host (24 Oct: "no active session ... context deadline exceeded"); retried.
TRANSIENT = ("no active session", "context deadline exceeded", "connection reset by peer",
             "Connection closed by remote host", "kex_exchange_identification",
             "error reading preface", "file already closed", "error during connect",
             "dial-stdio", "exit status 255", "Connection refused", "Connection timed out",
             "ssh: connect to host", "Broken pipe")
TASK_ATTEMPTS = 4  # a task whose step failed on the transport is retried, with backoff


def docker(args: list[str], host: str, timeout: int, input: bytes | None = None,
           check: bool = True, attempts: int = 4) -> subprocess.CompletedProcess:
    for i in range(attempts):
        p = subprocess.run(SBX.docker_argv(args, host), input=input, capture_output=True,
                           timeout=timeout, check=False)
        err = p.stderr.decode(errors="replace")
        if p.returncode == 0 or not any(m in err for m in TRANSIENT) or i == attempts - 1:
            break
        log(f"docker {' '.join(args[:2])}: transient failure, retry {i + 1} in {30 * (i + 1)} s")
        time.sleep(30 * (i + 1))
    if check and p.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} rc={p.returncode}: "
                           f"{p.stderr.decode(errors='replace')[-1500:]}")
    return p


def load1(ssh_target: str) -> float | None:
    with contextlib.suppress(Exception):
        p = subprocess.run(["ssh", ssh_target, "cat /proc/loadavg"], capture_output=True,
                           timeout=30, text=True, check=False)
        return float(p.stdout.split()[0])
    return None


@contextlib.contextmanager
def build_slot(ssh_target: str):
    """One of 2 build slots, once the host's 1-minute load is under MAX_LOAD_TO_BUILD (an
    unreadable load waits too); each wait is logged."""
    with _builds:
        while True:
            la = load1(ssh_target)
            if la is not None and la < MAX_LOAD_TO_BUILD:
                break
            log(f"build throttle: host load {'unknown' if la is None else f'{la:.1f}'} "
                f"(need < {MAX_LOAD_TO_BUILD}); waiting 60 s")
            time.sleep(60)
        yield


def available_gb(ssh_target: str) -> float | None:
    with contextlib.suppress(Exception):
        p = subprocess.run(["ssh", ssh_target, "awk '/MemAvailable/ {print $2}' /proc/meminfo"],
                           capture_output=True, timeout=30, text=True, check=False)
        return int(p.stdout.strip()) / 1024 / 1024
    return None


@contextlib.contextmanager
def container_slot(ssh_target: str):
    """One of our 4 container slots, once the host has MIN_AVAILABLE_GB available."""
    with _containers:
        unknown = 0
        while True:
            gb = available_gb(ssh_target)
            if gb is not None and gb >= MIN_AVAILABLE_GB:
                break
            if gb is None:  # unreadable: never admit blind (astra r3 #4)
                unknown += 1
                if unknown >= 10:
                    raise RuntimeError(f"can't read available memory on {ssh_target}")
            log(f"host has {'unknown' if gb is None else f'{gb:.1f} GB'} available "
                f"(need {MIN_AVAILABLE_GB}); waiting 60 s")
            time.sleep(60)
        yield


def _merge(path: Path, task_id: str, row: dict) -> None:
    with _write_lock:
        doc = json.loads(path.read_text()) if path.exists() else {}
        doc.setdefault("tasks", {})[task_id] = row
        doc["tasks"] = dict(sorted(doc["tasks"].items()))
        doc["updated_utc"] = utc()
        doc["generated_by"] = "scripts/build_pro.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, indent=1) + "\n")
        tmp.replace(path)


REMOTE_DIR = "emx-fam-pro"  # build contexts on the desktop, under the ssh user's home


def remote_build(ctx: Path, tag: str, ssh_target: str, name: str,
                 attempts: int = 3) -> None:
    """`docker build` run on the desktop itself over plain ssh, from a copy of the context:
    BuildKit's session through `docker -H ssh://` failed under load on 24 Oct ("no active
    session", "error reading preface"). Transient ssh failures are retried."""
    rdir = f"{REMOTE_DIR}/{name}"
    for i in range(attempts):
        try:
            subprocess.run(["ssh", ssh_target, f"mkdir -p {rdir}"], check=True,
                           capture_output=True, timeout=120)
            subprocess.run(["rsync", "-a", "--delete", "--exclude", "final/", f"{ctx}/",
                            f"{ssh_target}:{rdir}/"], check=True, capture_output=True,
                           timeout=600)
            cmd = (f"cd {rdir} && timeout -k 10 {BUILD_TIMEOUT_S} docker build "
                   f"--platform linux/amd64 -t {tag} -f Dockerfile .")
            p = subprocess.run(["ssh", ssh_target, cmd], capture_output=True,
                               timeout=BUILD_TIMEOUT_S + 60, check=False)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            if i == attempts - 1:
                raise RuntimeError(f"build context transfer for {name} failed: {e}") from e
            log(f"{name}: context transfer failed ({e}); retry {i + 1}")
            time.sleep(30)
            continue
        err = p.stderr.decode(errors="replace")
        if p.returncode == 0:
            return
        if p.returncode != 255 or i == attempts - 1:  # 255 = ssh itself failed
            raise RuntimeError(f"docker build {tag} rc={p.returncode}: {err[-3000:]}")
        log(f"{name}: ssh failed during the build; retry {i + 1}")
        time.sleep(30)


def build_image(src: P.ProSource, host: str, ssh_target: str) -> dict:
    d = P.materialize(src)
    t0 = time.monotonic()
    with _pulls:
        log(f"{src.task_id}: pull {src.from_image[:80]}…")
        docker(["pull", "--platform", "linux/amd64", src.from_image], host, PULL_TIMEOUT_S)
    t_pull = time.monotonic() - t0
    digests = json.loads(docker(["image", "inspect", "--format", "{{json .RepoDigests}}",
                                 src.from_image], host, 60).stdout)
    stage = P.stage_tag(src.task_id)
    with build_slot(ssh_target), container_slot(ssh_target):
        log(f"{src.task_id}: build stage")
        t1 = time.monotonic()
        remote_build(d / "build", stage, ssh_target, src.task_id)
        cfg = json.loads(docker(["image", "inspect", "--format", "{{json .Config}}", stage],
                                host, 60).stdout)
        final = P.final_dockerfile(src.task_id, stage, cfg)
        fdir = d / "build" / "final"
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / "Dockerfile").write_text(final)
        tag = P.image_tag(src.task_id)
        remote_build(fdir, tag, ssh_target, src.task_id + "-final")
        t_build = time.monotonic() - t1
    info = json.loads(docker(["image", "inspect", tag], host, 60).stdout)[0]
    docker(["rmi", stage], host, 300, check=False)
    env = dict(kv.partition("=")[::2] for kv in info["Config"].get("Env") or [])
    return {"image": tag, "image_id": info["Id"], "from_image": src.from_image,
            "from_digests": digests, "n_layers": len(info["RootFS"]["Layers"]),
            "n_content_layers": sum(lay != EMPTY_LAYER for lay in info["RootFS"]["Layers"]),
            "size_bytes": info.get("Size"), "entrypoint": info["Config"].get("Entrypoint"),
            "workdir": info["Config"].get("WorkingDir"), "docker_path": env.get("PATH"),
            "build_inputs_sha256": P.build_inputs_sha256(d), "upstream_base": src.base_commit,
            "pull_s": round(t_pull, 1), "build_s": round(t_build, 1), "built_utc": utc()}


CHECK_SH = r"""set -u
cd /app
git config --global --add safe.directory /app
echo "commits=$(git rev-list --all | wc -l)"
echo "head=$(git rev-parse HEAD)"
echo "tree=$(git rev-parse 'HEAD^{tree}')"
echo "squash=$(cat /emx_squash.txt)"
echo "refs=$(git for-each-ref --format='%(refname)' | tr '\n' ' ')"
echo "stash=$(git stash list | wc -l)"
if ST=$(git status --porcelain --ignore-submodules=dirty); then
  echo "status=$(printf '%s' "$ST" | grep -c .)"
else
  echo "status=error"
fi
for m in $(git ls-files -s | awk '$1 == "160000" {print $4}'); do
  [ -f "$m/.git" ] || continue
  echo "subrepo_ok=$(git -C "$m" rev-parse HEAD >/dev/null 2>&1 && echo 1 || echo 0) $m"
  echo "subrefs=$(git -C "$m" for-each-ref | wc -l) $m"
done
if git cat-file -e "$1" 2>/dev/null; then echo "solution_object=present"; else echo "solution_object=absent"; fi
echo "path=$PATH"
echo "python3=$(command -v python3)"
echo "git=$(command -v git)"
"""


def check_image(src: P.ProSource, img: dict, host: str, ssh_target: str) -> dict:
    with container_slot(ssh_target), SBX.Sandbox.start(
            img["image_id"], P.PURPOSE, run_id=f"check-{src.task_id}",
            lifetime_s=CHECK_LIFETIME_S, cpus=CPUS, memory_mb=MEMORY_MB,
            host=host) as sb:
        sb.put_file(CHECK_SH.encode(), "/tmp/emx_check.sh")
        r = sb.exec(f"bash /tmp/emx_check.sh {src.solution_sha}", timeout_s=600)
    kv = dict(line.partition("=")[::2] for line in r.stdout.splitlines() if "=" in line)
    base, tree, equiv = (kv.get("squash", "").split() + ["", "", ""])[:3]
    problems = []
    if equiv != "test_patch_equiv=1":
        problems.append("squash record lacks the test_patch equivalence check")
    if kv.get("commits") != "1":
        problems.append(f"commits={kv.get('commits')}")
    if base != src.base_commit:
        problems.append(f"squashed from {base!r}, not {src.base_commit}")
    if kv.get("tree") != tree:
        problems.append("HEAD tree differs from the base tree")
    if kv.get("refs", "").split() != ["refs/heads/main"]:
        problems.append(f"refs={kv.get('refs')}")
    for line in r.stdout.splitlines():
        if line.startswith(("subrepo_ok=0", "subrefs=")) and not line.startswith("subrefs=0 "):
            problems.append(f"submodule repo: {line}")
    if kv.get("stash") != "0" or kv.get("status") != "0":
        problems.append(f"stash={kv.get('stash')} status={kv.get('status')}")
    if kv.get("solution_object") != "absent":
        problems.append("solution commit object present")
    missing = [e for e in (img["docker_path"] or "").split(":")
               if e not in kv.get("path", "").split(":")]
    if missing:
        problems.append(f"login-shell PATH lacks the image's {missing}")
    if not kv.get("python3") or not kv.get("git"):
        problems.append("python3 or git missing")
    if img["n_content_layers"] != 1:
        problems.append(f"{img['n_content_layers']} non-empty layers (want the one COPY layer)")
    if img["entrypoint"]:
        problems.append(f"entrypoint {img['entrypoint']}")
    return {"rc": r.rc, "squashed_head": kv.get("head"), "tree": kv.get("tree"),
            "problems": problems, "raw": kv}


def grade_once(src: P.ProSource, img: dict, patch: bytes, label: str, host: str,
               ssh_target: str) -> dict:
    """One base or gold grading under pro's grading policy (`grade_with_policy`: 4 CPU / 8 GB,
    an OOM kill reruns once at 16 GB as an infrastructure failure), each attempt in one of our
    container slots, by the manifest's immutable image id (astra r2 #1)."""
    from functools import partial
    d = P.DATA_DIR / src.task_id
    t0 = time.monotonic()
    start = partial(SBX.Sandbox.start, purpose=P.PURPOSE, run_id=f"{label}-{src.task_id}",
                    host=host)
    res = P.grade_with_policy(start, img["image_id"], d / "tests", patch,
                              timeout_s=GRADE_TIMEOUT_S,
                              slot=lambda: container_slot(ssh_target))
    rew = res["reward"]
    out = ({k: rew.get(k) for k in ("reward", "f2p_total", "f2p_passed", "p2p_total",
                                    "p2p_passed", "apply_failed", "parser_failed",
                                    "run_script_rc", "n_reported", "failing") if k in rew}
           if rew is not None else {"reward": None})
    out.update(error=res["error"], attempts=res["attempts"], cpus=CPUS,
               seconds=round(time.monotonic() - t0, 1))
    for a in res["attempts"]:
        if a["oom_kills"]:
            log(f"{src.task_id} {label}: {a['oom_kills']} OOM kill(s) at {a['memory_mb']} MB "
                "(infrastructure)")
    p = d / "checks" / f"{label}.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((res["error"] or "") + "\n" + res["output"])
    return out


def do_task(src: P.ProSource, host: str, ssh_target: str, rebuild: bool, regrade: bool) -> None:
    """One task, retried from where it stopped (records and the build cache make that cheap)
    when a step failed on the ssh/docker transport rather than on the task (main session:
    back off on ssh refusals rather than fail)."""
    for i in range(TASK_ATTEMPTS):
        if _do_task(src, host, ssh_target, rebuild, regrade, last=i == TASK_ATTEMPTS - 1):
            return
        rebuild = False
        log(f"{src.task_id}: transport failure; task retry {i + 1} in {120 * (i + 1)} s")
        time.sleep(120 * (i + 1))


def _do_task(src: P.ProSource, host: str, ssh_target: str, rebuild: bool, regrade: bool,
             last: bool) -> bool:
    """False when the task stopped on a transport failure and should be retried."""
    tid = src.task_id
    d = P.materialize(src)
    img = P.images_manifest().get(tid)
    try:
        if rebuild or not img or img.get("build_inputs_sha256") != P.build_inputs_sha256(d):
            img = build_image(src, host, ssh_target)
            chk = check_image(src, img, host, ssh_target)
            img |= {"squashed_head": chk["squashed_head"], "tree": chk["tree"],
                    "check_problems": chk["problems"], "checked_utc": utc()}
            _merge(P.RESULTS_DIR / "images.json", tid, img)
            regrade = True
            log(f"{tid}: built {img['image']} ({img['build_s']} s); problems={chk['problems']}")
        prev = P.base_gold().get(tid)
        if (prev and not regrade and prev.get("verifier_sha256") == P.verifier_sha256(d)
                and prev.get("image_id") == img["image_id"]
                and prev.get("graded_by") == "image_id"):
            log(f"{tid}: base/gold already recorded (pass={prev.get('pass')}); skipped")
            return True
        if img.get("check_problems"):
            row = {"pass": False, "reason": "image check: " + "; ".join(img["check_problems"]),
                   "image": img["image"], "image_id": img["image_id"], "checked_utc": utc()}
        else:
            base = grade_once(src, img, b"", "base", host, ssh_target)
            gold = grade_once(src, img, src.config["patch"].encode(), "gold", host, ssh_target)
            ok = base.get("reward") == 0 and gold.get("reward") == 1
            errs = " ".join(str(g.get("error") or "") for g in (base, gold))
            if not ok and not last and any(m in errs for m in TRANSIENT):
                log(f"{tid}: grading hit a transport error: {errs[:200]}")
                return False
            reason = None if ok else (
                f"base reward {base.get('reward')} ({base.get('error') or 'ok'}), gold reward "
                f"{gold.get('reward')} ({gold.get('error') or 'ok'})")
            row = {"pass": ok, "reason": reason, "image": img["image"],
                   "image_id": img["image_id"], "graded_by": "image_id",
                   "verifier_sha256": P.verifier_sha256(d),
                   "base": base, "gold": gold, "checked_utc": utc()}
        _merge(P.RESULTS_DIR / "base_gold.json", tid, row)
        log(f"{tid}: base/gold pass={row['pass']} {row.get('reason') or ''}")
        return True
    except Exception as e:  # noqa: BLE001 - recorded per task; the other tasks carry on
        if not last and any(m in str(e) for m in TRANSIENT) and "emx squash" not in str(e):
            log(f"{tid}: transport error: {str(e)[:200]}")
            return False
        log(f"{tid}: FAILED {e}")
        _merge(P.RESULTS_DIR / "base_gold.json", tid,
               {"pass": False, "reason": "build/check error: " + (
                   "; ".join(dict.fromkeys(re.findall(r"emx squash: [^\n]*", str(e))))
                   or str(e)[-1500:]),
                "traceback": traceback.format_exc()[-3000:], "checked_utc": utc()})
        return True


DEVIATIONS = [
    ("image: the task's Dockerfile, then the repo squashed to one 'init' commit at base (tree "
     "hash asserted equal) and the image flattened FROM scratch; ENV/WORKDIR re-declared; "
     "ENTRYPOINT []; a profile.d line keeps the image PATH under bash -lc"),
    ("grading: test files reset to HEAD and config test_patch applied instead of upstream's "
     "`git checkout <solution> -- <test files>` (base+test_patch == solution blobs checked at "
     "build); reward.json with f2p/p2p counts instead of reward.txt"),
    "step texts: /app/plan.md -> /tmp/plan.md; a root plan.md is dropped from model.patch",
    "network: graded with --network none, as the agent runs (Harbor's Pro tasks allow network)",
]


NETWORK_MARKERS = ("network is unreachable", "Temporary failure in name resolution",
                   "Could not resolve host", "getaddrinfo ENOTFOUND", "EAI_AGAIN")


def summarize(ids: list[str]) -> dict:
    """Top-level fields of base_gold.json: who passed, who is excluded and why."""
    bg, imgs, srcs = P.base_gold(), P.images_manifest(), P.sources()

    def current(t: str) -> bool:
        row, img = bg.get(t) or {}, imgs.get(t) or {}
        return (bool(row.get("pass")) and row.get("image_id") == img.get("image_id")
                and row.get("graded_by") == "image_id"
                and not img.get("check_problems")
                and row.get("verifier_sha256") == P.verifier_sha256(P.materialize(srcs[t])))
    passed = [t for t in ids if current(t)]
    excluded = {t: (bg.get(t) or {}).get("reason") or "not run" for t in ids if t not in passed}
    for t in excluded:
        if (bg.get(t) or {}).get("pass"):
            excluded[t] = "stale pass: recorded for another image id or verifier"
    for t in excluded:
        g = P.DATA_DIR / t / "checks" / "gold.log"
        if g.exists() and any(m in g.read_text(errors="replace") for m in NETWORK_MARKERS):
            deps = P.gold_added_dependencies(srcs[t])
            excluded[t] += ("; the gold log shows network failures: the reference patch adds "
                            f"dependencies the image lacks {deps} (agent and grader run "
                            "--network none; gold-only dependencies are not pre-fetched)")
    path = P.RESULTS_DIR / "base_gold.json"
    with _write_lock:
        doc = json.loads(path.read_text()) if path.exists() else {"tasks": {}}
        doc.update(n_tasks=len(ids), n_pass=len(passed), passed=passed, excluded=excluded,
                   rule="pass = base reward 0 and gold (reference patch) reward 1, both graded "
                        "by tests/test.sh in a fresh --network none container of the image",
                   deviations=DEVIATIONS, summarized_utc=utc())
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, indent=1) + "\n")
        tmp.replace(path)
    return {"n_pass": len(passed), "excluded": excluded}


@contextlib.contextmanager
def run_lock(ap: argparse.ArgumentParser):
    """One build_pro.py process at a time mutates the records (astra r1 #5, r2 #3)."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "a") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            ap.error(f"another build_pro.py run holds {LOCK_FILE}")
        yield


def clean_check(host: str) -> list[str]:
    p = docker(["ps", "-a", "--filter", f"label={SBX.LABEL_KEY}={P.PURPOSE}", "--format",
                "{{.Names}}"], host, 60)
    return p.stdout.decode().split()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--tasks", nargs="*")
    r.add_argument("--host", default=HOST)
    r.add_argument("--ssh-target", default=SSH_TARGET)
    r.add_argument("--workers", type=int, default=6)
    r.add_argument("--rebuild", action="store_true")
    r.add_argument("--regrade", action="store_true")
    c = sub.add_parser("clean-check")
    c.add_argument("--host", default=HOST)
    sub.add_parser("summarize")
    a = ap.parse_args(argv)
    if a.cmd == "summarize":
        with run_lock(ap):
            print(json.dumps(summarize(list(P.sources())), indent=1))
        return 0
    if a.cmd == "clean-check":
        left = clean_check(a.host)
        print(json.dumps({"label": f"{SBX.LABEL_KEY}={P.PURPOSE}", "containers": left}))
        return 1 if left else 0
    srcs = P.sources()
    ids = list(dict.fromkeys(a.tasks or srcs))
    unknown = [t for t in ids if t not in srcs]
    if unknown:
        ap.error(f"unknown tasks: {unknown}")
    with run_lock(ap):
        with ThreadPoolExecutor(a.workers) as ex:
            list(ex.map(lambda t: do_task(srcs[t], a.host, a.ssh_target, a.rebuild,
                                          a.regrade), ids))
        summarize(list(srcs))
    bg = P.base_gold()
    ok = [t for t in ids if (bg.get(t) or {}).get("pass")]
    log(f"done: {len(ok)}/{len(ids)} pass base/gold; failing: "
        f"{[t for t in ids if t not in ok]}")
    left = clean_check(a.host)
    if left:
        log(f"WARNING: containers left with our label: {left}")
    return 0 if not left and len(ok) == len(ids) else 1


if __name__ == "__main__":
    sys.exit(main())
