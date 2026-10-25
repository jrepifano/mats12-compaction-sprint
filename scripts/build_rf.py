"""Build and check the 25 SWE Atlas Refactoring (RF) task images on the desktop Docker host.

Brief logs/s2/briefs/integrate-rf.md; design in `compaction.s2.families.rf`'s docstring.
Per task, all on ssh://rig (never the laptop's daemon):
1. probe the upstream image (pulled by digest beforehand): its config (ENV PATH, CMD,
   ENTRYPOINT, WORKDIR), the login-shell PATH, the repo at `mount_path`, python3 under each PATH;
2. stage image `emx-tasks/swe-interact-rf:<id>-v1-stage`: a sandbox of the upstream image with
   network (the only one) runs `rf.stage_script` (/app -> repo, Go module download) and is
   committed (`rf.commit_changes` restores the upstream CMD/ENTRYPOINT/WORKDIR);
3. record runs of our grader (no f2p/p2p lists) on the stage image: the reference solution
   under each candidate PATH (the image's ENV PATH and the login PATH, when they differ; heavy
   tasks try the second only if the first passed nothing), then the untouched base under the
   PATH whose valid reference run passed the most tests. f2p = passed with the reference and
   not on base, p2p = passed on both. A task whose reference passes nothing stops here;
4. final image `emx-tasks/swe-interact-rf:<id>-v1`: the stage plus `rf.pin_script` (PATH for
   login shells), committed the same way, then checked as the harness starts it (`--workdir
   /app`, `bash -lc`): the pinned PATH is in force, `/app` is the repo, the tree is clean;
5. grade, on the final image, through the harness's reader (`sandbox.parse_reward`): the
   untouched base (must be reward 0) and the reference solution (must be reward 1). Tests whose
   result differs between the two reference runs (record and grade) are dropped as flaky and
   both grades rescored from their recorded outputs (logged, and counted in base_gold.json).

Writes results/sprint/families/rf/{probe,images,base_gold}.json (merged per task, atomically,
so a rerun resumes), grading/<id>.json (the lists), preservation_rules.{json,md}, and every
run's verifier detail under data/raw/sprint/families/rf/<id>/runs/<generation>/; each task's
rows name the generation (one attempt at the task) their runs come from.

Resource rules (main session, 24 Oct). 19:42 UTC, rig as a free compute node: --cpus 4
--memory 8g per container (--cpus 8 --memory 16g for `rf.HEAVY_REPOS` and after an OOM). 21:10
UTC throttle while the native agent batches run (they take priority): our containers' memory
limits sum to at most 12 GB (MEM_BUDGET_MB; one above that runs alone), one container above
8 GB at a time, and none starts while rig has under 10 GB available (14 GB for one above 8 GB),
checked right before each start. A run with OOM evidence (`rf.oom_suspect`: the cgroup's
oom_kill count, or the container gone) is an infrastructure failure, rerun once at 16 GB, and
the task keeps 16 GB from then on; base_gold.json counts them. Every container,
the image-layer ones included: --rm, a name, label emx.sandbox=fam-rf, an in-container timeout
(`compaction.deepswe.sandbox.Sandbox`).

    uv run python scripts/build_rf.py run [--tasks rf_task-<id> ...] [--force | --redo-failed]
    uv run python scripts/build_rf.py rules
    uv run python scripts/build_rf.py clean-check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from compaction.deepswe import sandbox as SBX
from compaction.s2.families import rf as R

HOST = R.DESKTOP_HOST
SSH_TARGET = "rig"
# Throttle (main session, 24 Oct 21:10 UTC: rig under memory pressure while the native
# agent batches run; they take priority): our containers' memory limits sum to at most 12 GB,
# one container above 8 GB at a time (it then runs alone), and a heavy one starts only with
# >= 14 GB available on rig (10 GB for the rest).
MIN_AVAILABLE_GB = {False: 10.0, True: 14.0}  # light, heavy (> 8 GB)
MEM_BUDGET_MB = 12288
MAX_HEAVY = 1
WARM_TIMEOUT_S = 2400  # the stage script (module download)
COMMIT_TIMEOUT_S = 1800
PROBE_LIFETIME_S = 900
SLOTS = 4  # task threads
OOM_MEMORY_MB = 16384
MAX_FLAKY_SHARE = 0.10  # more listed tests than this changing between reference runs: unstable

_cond = threading.Condition()
_in_use = {"mb": 0, "heavy": 0}
_write_lock = threading.Lock()
_mem_override: dict[str, int] = {}  # task -> memory_mb after an OOM
_oom_events: dict[str, list[dict]] = {}


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{utc()}] {msg}", flush=True)


class slots:
    """Hold `mb` of our memory budget (MEM_BUDGET_MB; a container above it runs alone), at most
    MAX_HEAVY containers above 8 GB at once, then wait for host memory."""

    def __init__(self, mb: int, heavy: bool | None = None):
        self.mb = min(mb, MEM_BUDGET_MB)
        self.heavy = mb > 8192 if heavy is None else heavy

    def _fits(self) -> bool:
        return (_in_use["mb"] + self.mb <= MEM_BUDGET_MB
                and (not self.heavy or _in_use["heavy"] < MAX_HEAVY))

    def __enter__(self):
        """Reserve only once both our budget and the host's available memory allow it, so a
        heavy job waiting for host memory doesn't hold the budget against light ones."""
        while True:
            with _cond:
                _cond.wait_for(self._fits)
            gb = available_gb()
            enough = gb is not None and gb >= MIN_AVAILABLE_GB[self.heavy]
            with _cond:
                if enough and self._fits():
                    _in_use["mb"] += self.mb
                    _in_use["heavy"] += self.heavy
                    return self
            if enough:
                continue  # another job took the budget meanwhile: wait for it again
            log(f"waiting: host available memory {gb} GB < {MIN_AVAILABLE_GB[self.heavy]} GB")
            time.sleep(60)

    def __exit__(self, *exc):
        with _cond:
            _in_use["mb"] -= self.mb
            _in_use["heavy"] -= self.heavy
            _cond.notify_all()


def task_slots(src: R.RfSource) -> slots:
    return slots(resources(src)["memory_mb"])


def available_gb() -> float | None:
    try:
        p = subprocess.run(["ssh", "-o", "ConnectTimeout=15", SSH_TARGET, "free", "-b"],
                           check=False, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    for line in p.stdout.splitlines():
        if line.startswith("Mem:"):
            return int(line.split()[6]) / 2**30
    return None


def docker(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(SBX.docker_argv(args, HOST), check=False, capture_output=True,
                          timeout=timeout)


def sandbox(src: R.RfSource, image: str, what: str, *, lifetime_s: int, network: str = "none",
            cpus: float = 1, memory_mb: int = 2048) -> SBX.Sandbox:
    """A build-side sandbox (probe, stage, pin, checks) with the image's entrypoint disabled:
    an upstream entrypoint that starts services and runs migrations (simple-login) would
    otherwise leave a live database in what `docker commit` saves. Otherwise as
    `Sandbox.start` (--rm, name, emx.sandbox label, in-container timeout); grader runs use the
    image's (wrapped) entrypoint, as the harness does."""
    name = f"fam-rf-{what}-{src.task_id[-8:]}-{uuid.uuid4().hex[:6]}"
    args = SBX.run_args(image, name, R.PURPOSE, lifetime_s, cpus, memory_mb, "linux/amd64",
                        network)
    i = len(args) - args[::-1].index(image) - 1  # the image ref, just before the command
    args[i:i] = ["--entrypoint", ""]
    with SBX.using_host(HOST):
        try:
            SBX._docker(args, timeout=300)
        except BaseException:
            SBX.remove_container(name)
            raise
    return SBX.Sandbox(name=name, image=image, purpose=R.PURPOSE, host=HOST)


# ------------------------------------------------------------------------------------------
# Records (merged per task, written atomically)
# ------------------------------------------------------------------------------------------


def _read(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {"tasks": {}}


def _write_atomic(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, p)


def _merge(name: str, task_id: str, row: dict) -> None:
    with _write_lock:
        p = R.RESULTS_DIR / name
        d = _read(p)
        d["tasks"][task_id] = row
        d["updated_at"] = utc()
        d["tasks"] = dict(sorted(d["tasks"].items()))
        _write_atomic(p, json.dumps(d, indent=1) + "\n")


def _merge_result(src: R.RfSource, row: dict) -> None:
    """base_gold.json row, with the resources the task ended at and its OOM events."""
    _merge("base_gold.json", src.task_id, row | {"resources": resources(src),
                                                 "oom_events": _oom_events.get(src.task_id, []),
                                                 "n_oom": len(_oom_events.get(src.task_id, []))})


def _get(name: str, task_id: str) -> dict | None:
    with _write_lock:
        return _read(R.RESULTS_DIR / name)["tasks"].get(task_id)


def sha256_dir(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file()):
        h.update(f.relative_to(d).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


# ------------------------------------------------------------------------------------------
# Steps
# ------------------------------------------------------------------------------------------

PROBE_CMD = r"""
M={mount}
echo "LOGIN_PATH=$PATH"
echo "REPO_GIT=$(test -d "$M/.git" && echo yes || echo no)"
echo "REPO_COMMITS=$(git -C "$M" rev-list --all --count 2>/dev/null)"
echo "REPO_STATUS_LINES=$(git -C "$M" status --porcelain 2>/dev/null | wc -l)"
for P in "$PATH" {env_path}; do echo "PY[$P]=$(PATH=$P command -v python3) $(PATH=$P python3 --version 2>&1)"; done
echo "USR_PY=$(/usr/bin/python3 --version 2>&1)"
"""


def probe(src: R.RfSource) -> dict:
    ins = docker(["image", "inspect", src.upstream_image], timeout=120)
    if ins.returncode != 0:
        raise RuntimeError(f"image not on host: {ins.stderr.decode()[-300:]}")
    cfg = json.loads(ins.stdout)[0]
    conf = cfg["Config"]
    env = dict(e.split("=", 1) for e in conf.get("Env") or [] if "=" in e)
    env_path = env.get("PATH", "")
    with slots(2048), sandbox(src, src.upstream_image, "probe",
                                  lifetime_s=PROBE_LIFETIME_S) as sb:
        r = sb.exec(PROBE_CMD.format(mount=shlex.quote(src.mount_path),
                                     env_path=shlex.quote(env_path)), timeout_s=300, workdir="/")
    facts = {}
    for line in r.stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            facts[k] = v.strip()
    return {"upstream_image": src.upstream_image, "image_id": cfg["Id"],
            "env_path": env_path, "login_path": facts.get("LOGIN_PATH"),
            "config": {k: conf.get(k) for k in (*R.CONFIG_KEYS, "Labels")},
            "mount_path": src.mount_path, "facts": facts, "probe_rc": r.rc, "at": utc()}


def resources(src: R.RfSource) -> dict:
    res = dict(R.HEAVY if src.heavy else R.LIGHT)
    if src.task_id in _mem_override:
        res["memory_mb"] = max(res["memory_mb"], _mem_override[src.task_id])
    return res


def record_run(src: R.RfSource, gen: str, image: str, path: str, patch: bytes, label: str,
               lists: dict | None = None, runner: str = "upstream") -> dict:
    """One grader run, rerun once at 16 GB on OOM evidence (and the task kept at 16 GB)."""
    out = _record_run(src, gen, image, path, patch, label, lists, runner)
    if R.oom_suspect(out["rc"], out["detail"]):
        ev = {"label": label, "memory_mb": out["resources"]["memory_mb"], "rc": out["rc"],
              "oom_kills": out["detail"].get("oom_kills"),
              "container_gone": out["detail"].get("container_gone"), "at": utc()}
        _oom_events.setdefault(src.task_id, []).append(ev)
        log(f"{src.task_id} {label}: OOM suspected {ev}")
        if out["resources"]["memory_mb"] < OOM_MEMORY_MB:
            _mem_override[src.task_id] = OOM_MEMORY_MB
            out = _record_run(src, gen, image, path, patch, label + "-mem16", lists, runner)
            out["oom_retry_of"] = label
    return out


def _record_run(src: R.RfSource, gen: str, image: str, path: str, patch: bytes, label: str,
                lists: dict | None = None, runner: str = "upstream") -> dict:
    """One grader run; record mode when `lists` is None. Saves the detail; returns it."""
    d = R.materialize(src, path, lists, runner=runner)
    res = resources(src)
    t0 = time.monotonic()
    name = f"fam-rf-{label}-{src.task_id[-8:]}-{uuid.uuid4().hex[:6]}"
    log(f"{src.task_id} {label}: start ({res['cpus']} cpu, {res['memory_mb']} MB)")
    with task_slots(src):
        r, raw, detail = R.run_grader(image, d / "tests", patch, host=HOST, name=name, **res)
    secs = time.monotonic() - t0
    out = {"label": label, "generation": gen, "image": image, "path": path, "rc": r.rc,
           "timed_out": r.timed_out, "seconds": round(secs, 1), "raw_reward": raw.stdout[:2000],
           "stdout_tail": r.stdout[-4000:], "stderr_tail": r.stderr[-4000:], "detail": detail, "resources": res,
           "tests_sha256": sha256_dir(d / "tests"),
           "patch_sha256": hashlib.sha256(patch).hexdigest(), "at": utc()}
    rd = R.DATA_DIR / src.task_id / "runs" / gen
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"{label}.json").write_text(json.dumps(out, indent=1))
    log(f"{src.task_id} {label}: {detail.get('status')} passed={detail.get('n_passed')} "
        f"failed={detail.get('n_failed')} reward={(detail.get('reward') or {}).get('reward')} "
        f"in {secs:.0f} s")
    return out


def image_id_of(out: str) -> str:
    """The image id a `docker commit`/`import` printed; the CLI may print warnings around it
    (a deprecated-flag notice went to stdout and became an "image id", 25 Oct)."""
    ids = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("sha256:")]
    if not ids:
        raise RuntimeError(f"no image id in docker output: {out[-300:]!r}")
    return ids[-1]


def commit(src: R.RfSource, sb: SBX.Sandbox, tag: str, config: dict) -> str:
    """`docker commit` the sandbox to `tag` (upstream config restored); returns the image id.
    The sandbox is removed by its caller's context manager whatever happens here."""
    ch = R.commit_changes(src.task_id, config)
    p = docker(["commit", *[a for c in ch for a in ("--change", c)], sb.name, tag],
               timeout=COMMIT_TIMEOUT_S)
    if p.returncode != 0:
        raise RuntimeError(f"commit to {tag} failed: {p.stderr.decode()[-1500:]}")
    return image_id_of(p.stdout.decode())


def flatten(src: R.RfSource, sb: SBX.Sandbox, tag: str, config: dict) -> str:
    """`docker export` the sandbox into a single-layer image `tag` (upstream config restored,
    `rf.import_changes`), on rig itself over plain ssh so the filesystem never leaves it.
    Returns the image id."""
    ch = R.import_changes(src.task_id, config)
    cmd = (f"set -o pipefail; docker export {shlex.quote(sb.name)} | docker import "
           + " ".join(f"--change {shlex.quote(c)}" for c in ch) + f" - {shlex.quote(tag)}")
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=15", SSH_TARGET, "bash", "-c",
                        shlex.quote(cmd)], check=False, capture_output=True, text=True,
                       timeout=COMMIT_TIMEOUT_S)
    if p.returncode != 0:
        raise RuntimeError(f"flatten to {tag} failed: {p.stderr[-1500:]}")
    return image_id_of(p.stdout)


def build_stage(src: R.RfSource, pr: dict) -> dict:
    t0 = time.monotonic()
    tag = R.stage_tag(src.task_id)
    script = R.stage_script(src.mount_path)
    with task_slots(src), sandbox(src, src.upstream_image, "stage", network="bridge",
                                  lifetime_s=WARM_TIMEOUT_S + COMMIT_TIMEOUT_S + 600,
                                  **resources(src)) as sb:
        # the image's own PATH: `go` is often missing from the login PATH (k6, trufflehog)
        r = sb.exec(f"export PATH={shlex.quote(pr['env_path'])}; sh -c {shlex.quote(script)}",
                    timeout_s=WARM_TIMEOUT_S, workdir="/")
        if r.rc != 0:
            raise RuntimeError(f"stage script rc={r.rc}: {(r.stdout + r.stderr)[-1500:]}")
        cleanup = R.CLEANUP.get(src.task_id)
        if cleanup:  # before the record runs, so the lists are measured without the artifacts
            c = sb.exec(R.cleanup_script(cleanup), timeout_s=600, workdir="/")
            if c.rc != 0:
                raise RuntimeError(f"cleanup rc={c.rc}: {(c.stdout + c.stderr)[-1500:]}")
        image_id = commit(src, sb, tag, pr["config"])
    log(f"{src.task_id}: stage image built in {time.monotonic() - t0:.0f} s")
    return {"stage_image": tag, "stage_image_id": image_id, "stage_script": script,
            "cleanup": list(R.CLEANUP.get(src.task_id) or [])}


def build_final(src: R.RfSource, pr: dict, stage: dict, path: str) -> dict:
    """The final image (stage + pinned PATH), and a check of a container of it as the harness
    starts one (`--workdir /app`, `bash -lc`)."""
    tag = R.image_tag(src.task_id)
    script = R.pin_script(path)
    cleanup = R.CLEANUP.get(src.task_id)  # applied in the stage; flattened away here
    with slots(2048):
        with sandbox(src, stage["stage_image_id"], "pin", lifetime_s=COMMIT_TIMEOUT_S + 600
                     ) as sb:
            r = sb.exec(script, timeout_s=600, workdir="/")
            if r.rc != 0:
                raise RuntimeError(f"pin script rc={r.rc}: {r.stderr[-500:]}")
            # always flattened: several upstream images hold the solution in a lower layer
            # that a later one deletes (imgval layer scan, 24 Oct)
            image_id = flatten(src, sb, tag, pr["config"])
        with sandbox(src, tag, "imgcheck", lifetime_s=PROBE_LIFETIME_S) as sb:
            r = sb.exec('echo "PATH=$PATH"; echo "PWD_P=$(pwd -P)"; '
                        'echo "TOP=$(git rev-parse --show-toplevel)"; '
                        'echo "COMMITS=$(git rev-list --all --count)"; '
                        'echo "STATUS_LINES=$(git status --porcelain | wc -l)"',
                        timeout_s=300, workdir=R.WORKSPACE)
    facts = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
    login_path = facts.get("PATH") or ""
    # ~/.profile may still prepend (Debian's adds ~/.local/bin when it exists)
    path_ok = login_path == path or login_path.endswith(":" + path)
    repo_ok = (facts.get("PWD_P") == src.mount_path and facts.get("TOP") == src.mount_path
               and facts.get("STATUS_LINES") == "0")
    ins = docker(["image", "inspect", image_id], timeout=120)
    final_conf = json.loads(ins.stdout)[0]["Config"] if ins.returncode == 0 else {}
    cdiff = R.config_diff(pr["config"], final_conf) if final_conf else {"inspect": "failed"}
    return {"image": tag, "image_id": image_id, "upstream_image": src.upstream_image, **stage,
            "pin_script": script, "path": path, "flattened": True, "cleanup": list(cleanup or []),
            "entrypoint_wrapped": bool(pr["config"].get("Entrypoint")),
            "config_diff": cdiff, "config_ok": not cdiff,
            "symlink": src.harness_ok() and src.mount_path != R.WORKSPACE,
            "harness_ok": src.harness_ok() and path_ok and repo_ok and not cdiff,
            "check": facts, "check_ok": path_ok and (repo_ok or not src.harness_ok())
            and not cdiff, "built_at": utc()}


def relevant_seen(src: R.RfSource, detail: dict) -> int:
    """Upstream's relevant tests that appear (passed or failed) in a run, by exact name."""
    seen = set(detail.get("passed") or []) | set(detail.get("failed") or [])
    return sum(t in seen for t in src.relevant_tests())


def _valid(run: dict) -> bool:
    return (run["detail"].get("status") == "ok" and not run["timed_out"]
            and not R.oom_suspect(run["rc"], run["detail"]))


# On the untouched base these mean "the test patch's tests don't build against the old code",
# i.e. nothing passes: a valid base outcome (k6 10d4b451: no results file on base).
BASE_FAIL_STATUSES = ("ok", "no_results", "build_failed")


# What makes a base run's missing results a real incompatibility with the old code (astra r2
# #6): compiler or import errors naming code, in the build or test log.
BASE_FAILURE_EVIDENCE = re.compile(
    r"undefined: |cannot use |has no field or method|undeclared name|not enough arguments"
    r"|too many arguments|\[build failed\]|setup failed|error TS\d+|ImportError"
    r"|ModuleNotFoundError|AttributeError|cannot find symbol|Cannot find module"
    r"|SyntaxError|error: ")


def base_failure_evidence(run: dict) -> str | None:
    tails = run["detail"].get("log_tails") or {}
    for f in ("build.log", "run_stdout.log"):
        m = BASE_FAILURE_EVIDENCE.search(tails.get(f) or "")
        if m:
            line = (tails[f][:m.start()].rsplit("\n", 1)[-1] + tails[f][m.start():]
                    ).split("\n", 1)[0]
            return f"{f}: {line[:300]}"
    return None


def _valid_base(run: dict) -> bool:
    st = run["detail"].get("status")
    if run["timed_out"] or R.oom_suspect(run["rc"], run["detail"]):
        return False
    return st == "ok" or (st in BASE_FAIL_STATUSES and base_failure_evidence(run) is not None)


def do_task(src: R.RfSource, force: bool = False, redo_failed: bool = False,
            reflatten: bool = False) -> None:
    tid = src.task_id
    prev = _get("base_gold.json", tid) or {}
    prev_img = _get("images.json", tid) or {}
    if (reflatten and not force and prev.get("pass") and not prev_img.get("flattened")
            and prev_img.get("stage_image_id")):
        return reflatten_task(src, prev, prev_img)
    if not force and prev.get("done") and (prev.get("pass") or not redo_failed):
        log(f"{tid}: done already")
        return
    _oom_events.pop(tid, None)
    gen = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    pr = _get("probe.json", tid)
    if force or not pr or "Volumes" not in (pr.get("config") or {}):
        pr = probe(src)
        _merge("probe.json", tid, pr)
    log(f"{tid}: probe git={pr['facts'].get('REPO_GIT')} "
        f"env_path==login_path: {pr['env_path'] == pr['login_path']}")
    base_row = {"generation": gen, "resources": resources(src),
                "task_toml_resources": {k: src.toml["environment"].get(k)
                                        for k in ("cpus", "memory_mb", "storage_mb")}}
    if pr["facts"].get("REPO_GIT") != "yes":
        _merge_result(src, base_row | {"done": True, "pass": False,
                                                  "reason": f"no git repo at {src.mount_path}"})
        return
    stage = build_stage(src, pr)
    candidates = [("env", pr["env_path"])]
    if pr["login_path"] and pr["login_path"] != pr["env_path"]:
        candidates.append(("login", pr["login_path"]))
    gold = src.gold_patch()
    trials = []
    for i, (kind, path) in enumerate(candidates):
        if i and src.heavy and _valid(trials[0]) and trials[0]["detail"].get("n_passed"):
            break
        trials.append(record_run(src, gen, stage["stage_image_id"], path, gold,
                                 f"gold-rec-{kind}"))
    row = base_row | {"path_trials": {
        t["label"]: {k: t["detail"].get(k) for k in ("status", "n_passed", "n_failed")}
        | {"rc": t["rc"], "timed_out": t["timed_out"], "seconds": t["seconds"]}
        for t in trials}}
    valid = [t for t in trials if _valid(t) and t["detail"].get("n_passed")]
    if not valid and src.language == "Go":
        # upstream's runner produced nothing usable offline (grafana: Go tests pass, then it
        # tries to download Node.js for Jest and writes no results): the same rule's runner
        alt = record_run(src, gen, stage["stage_image_id"], candidates[0][1], gold,
                         "gold-rec-relevant", runner="go-relevant")
        if _valid(alt) and alt["detail"].get("n_passed"):
            trials.append(alt)
            valid = [alt]
    if not valid:
        row |= {"done": True, "pass": False,
                "reason": "no valid reference run passed any test (statuses: " + ", ".join(
                    f"{t['label']}={t['detail'].get('status')}/rc {t['rc']}" for t in trials)
                + ")"}
        _merge_result(src, row)
        log(f"{tid}: base/gold FAIL ({row['reason']})")
        return
    best = max(valid, key=lambda t: t["detail"]["n_passed"])
    path = best["path"]
    path_source = best["label"].removeprefix("gold-rec-")
    runner, runner_reason = "upstream", None
    rel_seen = relevant_seen(src, best["detail"])
    if best["label"] == "gold-rec-relevant":
        runner, path_source = "go-relevant", "env"
        runner_reason = ("upstream's runner gave no valid reference run offline; go-relevant: "
                         f"{rel_seen}/{len(src.relevant_tests())} relevant tests seen")
        log(f"{tid}: {runner_reason}")
    elif src.language == "Go" and rel_seen * 2 < len(src.relevant_tests()):
        # the rule in rf.RUNNERS' comment: relevant tests aborted or not run upstream's way
        alt = record_run(src, gen, stage["stage_image_id"], path, gold, "gold-rec-relevant",
                         runner="go-relevant")
        runner_reason = (f"{rel_seen}/{len(src.relevant_tests())} relevant tests seen with the "
                         f"upstream runner; go-relevant: "
                         f"{relevant_seen(src, alt['detail'])} seen, "
                         f"{alt['detail'].get('n_passed')} passed")
        log(f"{tid}: {runner_reason}")
        # adopted only if it reaches more of the relevant tests than upstream's runner did
        if (_valid(alt) and alt["detail"].get("n_passed")
                and relevant_seen(src, alt["detail"]) > rel_seen):
            best, runner = alt, "go-relevant"
    base = record_run(src, gen, stage["stage_image_id"], path, b"", "base-rec", runner=runner)
    lists = R.derive_lists(base["detail"], best["detail"])
    rel = src.relevant_tests()
    in_lists = set(lists["f2p"]) | set(lists["p2p"])
    row |= {"path_source": path_source, "runner": runner, "runner_reason": runner_reason,
            "base_rec": {k: base["detail"].get(k) for k in ("status", "n_passed", "n_failed")}
            | {"rc": base["rc"], "seconds": base["seconds"],
               "failure_evidence": base_failure_evidence(base)},
            "coverage": {"relevant_listed": len(rel),
                         "relevant_exact_in_lists": sum(r in in_lists for r in rel),
                         "relevant_exact_in_f2p": sum(r in set(lists["f2p"]) for r in rel)}}
    if not _valid_base(base):
        row |= {"done": True, "pass": False, "n_f2p": None, "n_p2p": None,
                "reason": f"base record run invalid (status {base['detail'].get('status')}, "
                          f"rc {base['rc']})"}
        _merge_result(src, row)
        log(f"{tid}: base/gold FAIL ({row['reason']})")
        return
    if not lists["f2p"]:
        row |= {"done": True, "pass": False, "n_f2p": 0, "n_p2p": len(lists["p2p"]),
                "reason": "tests do not discriminate: no test passes with the reference and "
                          "fails on base"}
        _merge_result(src, row)
        log(f"{tid}: base/gold FAIL ({row['reason']})")
        return
    finish_task(src, gen, pr, stage, path, path_source, runner, lists, best["label"], row)


def finish_task(src: R.RfSource, gen: str, pr: dict, stage: dict, path: str, path_source: str,
                runner: str, lists: dict, best_label: str, row: dict) -> None:
    """Final image (flattened), the graded base/gold runs on its id, the restart check for a
    wrapped entrypoint, and the task's base_gold.json row."""
    tid = src.task_id
    gold = src.gold_patch()
    img = build_final(src, pr, stage, path)
    img |= {"path_source": path_source, "generation": gen}
    _merge("images.json", tid, img)
    if not img["check_ok"]:
        log(f"{tid}: final image check failed: {img['check']}")
    gfile = R.RESULTS_DIR / "grading" / f"{tid}.json"

    def save_lists(extra: dict) -> None:
        _write_atomic(gfile, json.dumps({"task_id": tid, "path": path, **lists,
                                         "generation": gen, "derived_at": utc(),
                                         "runner": runner,
                                         "from_runs": [best_label, "base-rec"], **extra},
                                        indent=1) + "\n")

    save_lists({})
    g = record_run(src, gen, img["image_id"], path, gold, "gold-grade", lists, runner)
    b = record_run(src, gen, img["image_id"], path, b"", "base-grade", lists, runner)
    graded = {"gold": _reward_of(g), "base": _reward_of(b)}
    flaky: list[str] = []
    rescored = None
    if _valid(g) and _valid_base(b):
        flaky = sorted(set(g["detail"].get("f2p_not_passed") or [])
                       | set(g["detail"].get("p2p_not_passed") or []))
    if flaky:
        lists = {"f2p": [t for t in lists["f2p"] if t not in flaky],
                 "p2p": [t for t in lists["p2p"] if t not in flaky],
                 "flaky": sorted(set(lists["flaky"]) | set(flaky))}
        rescored = {k: R.score_run(run["detail"].get("status") or "ok",
                                   {"passed_tests": run["detail"].get("passed"),
                                    "failed_tests": run["detail"].get("failed")},
                                   lists)["reward"] for k, run in (("gold", g), ("base", b))}
        save_lists({"flaky_from": "gold-grade"})
        log(f"{tid}: dropped {len(flaky)} flaky tests; rescored {rescored}")
    final = rescored or {k: v["reward_obj"] for k, v in graded.items()}
    n_listed = len(lists["f2p"]) + len(lists["p2p"]) + len(flaky)
    unstable = len(flaky) > max(MAX_FLAKY_SHARE * n_listed, 5)
    gold_r = (final["gold"] or {}).get("reward")
    base_r = (final["base"] or {}).get("reward")
    infra = ([] if _valid(g) else ["gold"]) + ([] if _valid_base(b) else ["base"])
    ok = gold_r == 1 and base_r == 0 and bool(lists["f2p"]) and not infra
    if unstable:
        ok = False
        reason = (f"unstable: {len(flaky)} of {n_listed} listed tests changed between the two "
                  "reference runs (record on the stage image, grade on the final one)")
    elif infra:
        reason = f"graded run(s) invalid (infrastructure): {infra}"
    elif not ok:
        reason = f"gold reward {gold_r}, base reward {base_r}, {len(lists['f2p'])} f2p"
    elif not img["harness_ok"]:
        reason = "ok for grading; the harness needs a workdir hook (/app is not the repo)"
    else:
        reason = "ok"
    if ok and img.get("entrypoint_wrapped"):
        rcheck = restart_check(src, gen, img, gold, lists, runner)
        row["restart_check"] = rcheck
        if not rcheck.get("ok"):
            ok, reason = False, f"restart check failed: {rcheck}"
    row |= {"done": True, "pass": ok, "reason": reason, "graded": graded,
            "rescored_after_flaky_drop": rescored, "n_flaky_dropped": len(flaky),
            "n_f2p": len(lists["f2p"]), "n_p2p": len(lists["p2p"]),
            "image": img["image"], "image_id": img["image_id"],
            "harness_ok": img["harness_ok"], "at": utc()}
    _merge_result(src, row)
    log(f"{tid}: base/gold {'PASS' if ok else 'FAIL'} ({reason})")


def restart_check(src: R.RfSource, gen: str, img: dict, gold: bytes, lists: dict,
                  runner: str) -> dict:
    """For a wrapped entrypoint: start the final image as the harness does, wait for the
    setup's rc, commit the running container (as the harness keeps boundary images), and grade
    the reference on that commit: its setup must run again (rc 0) and the tests still pass."""
    tmp = f"{R.IMAGE_REPO}:{src.task_id.removeprefix(R.PREFIX)}-restartcheck"
    out: dict = {}
    with task_slots(src), SBX.Sandbox.start(
            img["image_id"], R.PURPOSE, name=f"fam-rf-restart-{src.task_id[-8:]}-"
            f"{uuid.uuid4().hex[:6]}", lifetime_s=3600, host=HOST, **resources(src)) as sb:
        r = sb.exec(f"bash -c {shlex.quote(R.READY_SH)}; echo READY=$?; "
                    f"cut -d' ' -f2 {R.ENTRYPOINT_RC}",
                    timeout_s=R.ENTRYPOINT_SETUP_TIMEOUT_S + 400, workdir="/")
        lines = r.stdout.strip().splitlines()
        out["ready_first"] = lines[0] if lines else None
        out["rc_first"] = lines[-1] if len(lines) > 1 else None
        p = docker(["commit", sb.name, tmp], timeout=COMMIT_TIMEOUT_S)  # pauses by default
        if p.returncode != 0:
            return out | {"ok": False, "error": p.stderr.decode()[-500:]}
        out["image_id"] = image_id_of(p.stdout.decode())
    try:
        g = record_run(src, gen, out["image_id"], img["path"], gold, "gold-restart", lists,
                       runner)
    finally:
        docker(["rmi", tmp], timeout=300)
    rew = (g["detail"].get("reward") or {}).get("reward")
    out |= {"rc_restart": g["detail"].get("entrypoint_rc"), "gold_reward": rew,
            "ok": out["ready_first"] == "READY=0" and out["rc_first"] == "0"
            and g["detail"].get("entrypoint_rc") == 0 and rew == 1}
    log(f"{src.task_id}: restart check {out}")
    return out


def reflatten_task(src: R.RfSource, prev: dict, prev_img: dict) -> None:
    """A task that passed on a layered final image: rebuild the final image flattened from the
    same stage image, keep the measured lists (grading/<id>.json) and the discovery records,
    and redo everything from the final image on (graded base/gold, restart check)."""
    tid = src.task_id
    log(f"{tid}: reflatten (passed on layered image {prev_img.get('image_id')})")
    _oom_events.pop(tid, None)
    if prev.get("resources", {}).get("memory_mb", 0) > R.LIGHT["memory_mb"] and not src.heavy:
        _mem_override[tid] = prev["resources"]["memory_mb"]
    gen = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    pr = _get("probe.json", tid)
    if not pr or "Volumes" not in (pr.get("config") or {}):
        pr = probe(src)
        _merge("probe.json", tid, pr)
    gl = R.grading_lists(tid) or {}
    lists = {"f2p": gl["f2p"], "p2p": gl["p2p"], "flaky": gl.get("flaky", [])}
    stage = {k: prev_img[k] for k in ("stage_image", "stage_image_id", "stage_script")
             if k in prev_img} | {"cleanup": list(R.CLEANUP.get(tid) or [])}
    keep = ("path_trials", "path_source", "runner", "runner_reason", "base_rec", "coverage",
            "task_toml_resources")
    row = {k: prev[k] for k in keep if k in prev} | {
        "generation": gen, "discovery_generation": prev.get("generation"),
        "reflattened_from": prev_img.get("image_id")}
    finish_task(src, gen, pr, stage, gl["path"], prev.get("path_source") or "",
                gl.get("runner", "upstream"), lists, (gl.get("from_runs") or ["?"])[0], row)


def _reward_of(run: dict) -> dict:
    """The harness reader's view of one graded run (parse_reward on the raw reward read)."""
    kind, _, text = run["raw_reward"].partition("\n")
    try:
        rew = SBX.parse_reward(kind.strip(), text)
        err = None
    except SBX.VerifierError as e:
        rew, err = None, str(e)[:500]
    d = run["detail"]
    return {"reward_obj": rew, "parse_error": err, "status": d.get("status"),
            "n_passed": d.get("n_passed"), "n_failed": d.get("n_failed"),
            "f2p_not_passed": len(d.get("f2p_not_passed") or []),
            "p2p_not_passed": len(d.get("p2p_not_passed") or []),
            "seconds": run["seconds"], "timed_out": run["timed_out"], "rc": run["rc"]}


def run(task_ids: list[str] | None, force: bool, redo_failed: bool = False,
        force_tasks: list[str] | None = None, reflatten: bool = False) -> int:
    srcs = R.sources()
    ids = task_ids or list(srcs)
    # light tasks first, two at a time; heavy ones take both slots
    ids.sort(key=lambda t: (srcs[t].heavy, t))

    def one(t: str) -> None:
        try:
            do_task(srcs[t], force or t in (force_tasks or []), redo_failed, reflatten)
        except Exception as e:  # noqa: BLE001 - one task's failure must not stop the rest
            log(f"{t}: ERROR {e!r}\n{traceback.format_exc()}")
            _merge("base_gold.json", t, {"done": False, "pass": False,
                                         "reason": f"error: {e!r}"[:500], "at": utc()})

    with ThreadPoolExecutor(SLOTS) as ex:
        list(ex.map(one, ids))
    write_rules()
    return clean_check()


def write_rules() -> None:
    out, md = {}, ["# RF persona preservation rules: regex screen", "",
                   ("Keyword screen of each task's hidden spec (persona task block ="
                    " instruction.md) by `rf.preservation_rules`, not curated. The curated,"
                    " verbatim list is preservation_rules_curated.md."), ""]
    for tid, src in R.sources().items():
        rules = R.preservation_rules(R.task_block(src))
        out[tid] = {"repo": src.repo, "language": src.language, "rules": rules}
        md += [f"## {tid} ({src.repo}, {src.language})", ""]
        md += [f"- {r}" for r in rules] or ["- (none found)"]
        md.append("")
    _write_atomic(R.RESULTS_DIR / "preservation_rules.json", json.dumps(out, indent=1) + "\n")
    _write_atomic(R.RESULTS_DIR / "preservation_rules.md", "\n".join(md))


# Module-cache entries under the repo's own module path (a newer copy of the task's own code
# would be a solution leak; imgval's archive scan is the authority, this is our own screen).
MODCACHE_AUDIT_CMD = r"""
cd /app 2>/dev/null || cd {mount}
M=$(sed -n 's/^module[[:space:]]*//p' go.mod 2>/dev/null | head -1 | tr -d '"')
[ -z "$M" ] && {{ echo "MODULE="; exit 0; }}
B=${{M%/v[0-9]*}}
GM=$(go env GOMODCACHE 2>/dev/null)
echo "MODULE=$M"
echo "GOMODCACHE=$GM"
echo "SELF=$(cd "$GM" 2>/dev/null && ls -d "$B"@* "$B"/*@* "$B"/*/*@* "cache/download/$B"/*/@v     "cache/download/$B/@v" "cache/download/$B"/*/*/@v 2>/dev/null | tr '\n' ' ')"
"""


def modcache_audit(task_ids: list[str] | None) -> int:
    """For each built image, list module-cache entries under the repo's own module path.
    Writes results/sprint/families/rf/modcache_audit.json; exit 1 if any task has one."""
    imgs = json.loads((R.RESULTS_DIR / "images.json").read_text())["tasks"]
    srcs = R.sources()
    out, bad = {}, []
    for tid in task_ids or sorted(imgs):
        img, src = imgs[tid], srcs[tid]
        with slots(2048), sandbox(src, img["image_id"], "modaudit",
                                      lifetime_s=PROBE_LIFETIME_S) as sb:
            r = sb.exec(f"export PATH={shlex.quote(img['path'])}; "
                        + MODCACHE_AUDIT_CMD.format(mount=shlex.quote(src.mount_path)),
                        timeout_s=300, workdir="/")
        facts = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
        self_mods = facts.get("SELF", "").split()
        out[tid] = {"image_id": img["image_id"], "module": facts.get("MODULE"),
                    "gomodcache": facts.get("GOMODCACHE"), "self_entries": self_mods,
                    "rc": r.rc, "at": utc()}
        if self_mods or r.rc:
            bad.append(tid)
        log(f"{tid}: module {facts.get('MODULE')!r}, own-module cache entries: {self_mods}")
    _write_atomic(R.RESULTS_DIR / "modcache_audit.json",
                  json.dumps({"tasks": out, "flagged": bad, "at": utc()}, indent=1) + "\n")
    return 1 if bad else 0


def _latest_runs(tid: str, gen: str | None) -> dict[str, dict]:
    d = R.DATA_DIR / tid / "runs" / (gen or "")
    if not gen or not d.is_dir():
        return {}
    return {f.stem: json.loads(f.read_text()) for f in sorted(d.glob("*.json"))}


def summarize() -> int:
    """results/sprint/families/rf/summary.{json,md}: one row per task from base_gold.json, the
    grading lists and the recorded runs (no containers)."""
    bg = _read(R.RESULTS_DIR / "base_gold.json")["tasks"]
    imgs = _read(R.RESULTS_DIR / "images.json")["tasks"]
    rules_p = R.RESULTS_DIR / "preservation_rules_curated.md"
    rules = rules_p.read_text() if rules_p.exists() else ""
    rows = []
    for tid, src in R.sources().items():
        r = bg.get(tid) or {}
        lists = R.grading_lists(tid) or {}
        # the lists and base-rec come from the discovery generation (a reflattened task's own
        # generation has only the final-image runs); lists from another generation are stale
        disc_gen = r.get("discovery_generation") or r.get("generation")
        if lists.get("generation") not in (disc_gen, r.get("generation")):
            lists = {}
        runs = _latest_runs(tid, r.get("generation"))
        base = ((_latest_runs(tid, disc_gen).get("base-rec") or {}).get("detail") or {})
        b_failed = set(base.get("failed") or [])
        f2p = lists.get("f2p") or []
        n_rules = None
        m = re.search(rf"\| {tid[-8:]} \| {tid} \|[^|]*\|[^|]*\| (\d+) \|", rules)
        if m:
            n_rules = int(m.group(1))
        rows.append({
            "task_id": tid, "repo": src.repo, "language": src.language,
            "mount_path": src.mount_path, "pass": bool(r.get("pass")),
            "usable_now": R.usable(tid)[0], "reason": r.get("reason", "not run"),
            "n_f2p": len(f2p) if lists else None,
            "n_p2p": len(lists.get("p2p") or []) if lists else None,
            "f2p_failed_on_base": sum(t in b_failed for t in f2p),
            "f2p_missing_on_base": sum(t not in b_failed for t in f2p),
            "n_flaky_dropped": r.get("n_flaky_dropped"), "coverage": r.get("coverage"),
            "path_source": r.get("path_source"), "runner": r.get("runner"),
            "resources": r.get("resources"),
            "n_oom": r.get("n_oom", 0), "harness_ok": (imgs.get(tid) or {}).get("harness_ok"),
            "image": (imgs.get(tid) or {}).get("image"),
            "image_id": (imgs.get(tid) or {}).get("image_id"),
            "preservation_rules": n_rules,
            "seconds": {k: v.get("seconds") for k, v in runs.items()}})
    n_pass = sum(x["pass"] for x in rows)
    _write_atomic(R.RESULTS_DIR / "summary.json", json.dumps(
        {"tasks": rows, "n_tasks": len(rows), "n_base_gold_pass": n_pass,
         "n_oom": sum(x["n_oom"] or 0 for x in rows), "at": utc()}, indent=1) + "\n")
    md = ["# RF base/gold summary", "",
          (f"{n_pass} of {len(rows)} tasks pass the base/gold check (base reward 0, reference "
           "reward 1, f2p not empty). Generated by `scripts/build_rf.py summary` from "
           "base_gold.json, grading/ and the recorded runs."), "",
          ("| task | repo | lang | pass | f2p (failed / missing on base) | p2p | relevant in "
           "lists | PATH | cpu/GB | OOM | harness | rules | reason |"),
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in rows:
        cov = x["coverage"] or {}
        res = x["resources"] or {}
        md.append(
            f"| {x['task_id'][-8:]} | {x['repo']} | {x['language']} | "
            f"{'yes' if x['pass'] else 'no'} | "
            + (f"{x['n_f2p']} ({x['f2p_failed_on_base']} / {x['f2p_missing_on_base']})"
               if x["n_f2p"] is not None else "-")
            + f" | {x['n_p2p'] if x['n_p2p'] is not None else '-'} | "
            f"{cov.get('relevant_exact_in_lists', '-')}/{cov.get('relevant_listed', '-')} | "
            f"{x['path_source'] or '-'} | {res.get('cpus', '-')}/"
            f"{(res.get('memory_mb') or 0) // 1024 or '-'} | {x['n_oom']} | "
            f"{'ok' if x['harness_ok'] else ('needs workdir' if x['harness_ok'] is False else '-')}"
            f" | {x['preservation_rules'] if x['preservation_rules'] is not None else '-'} | "
            f"{x['reason']} |")
    _write_atomic(R.RESULTS_DIR / "summary.md", "\n".join(md) + "\n")
    print("\n".join(md))
    return 0


def clean_check() -> int:
    q = ["ps", "-a", "--filter", f"label={SBX.LABEL_KEY}={R.PURPOSE}", "--format", "{{.Names}}"]
    names = docker(q, timeout=60).stdout.decode().split()
    with SBX.using_host(HOST):
        for n in names:
            SBX.remove_container(n)
    if names:
        log(f"removed leftover containers: {names}")
    p = docker(q, timeout=60)
    left = p.stdout.decode().split()
    log(f"clean-check: {len(left)} fam-rf containers on {HOST}: {left}")
    return 1 if left or p.returncode else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--tasks", nargs="*")
    r.add_argument("--force", action="store_true")
    r.add_argument("--force-tasks", nargs="*", help="redo these even if they passed")
    r.add_argument("--reflatten", action="store_true",
                   help="tasks that passed on a layered final image: flatten it and regrade")
    r.add_argument("--redo-failed", action="store_true",
                   help="rerun tasks whose base/gold check did not pass")
    sub.add_parser("rules")
    sub.add_parser("clean-check")
    sub.add_parser("summary")
    m = sub.add_parser("modcache-audit")
    m.add_argument("--tasks", nargs="*")
    a = ap.parse_args(argv)
    if a.cmd == "summary":
        return summarize()
    if a.cmd == "modcache-audit":
        return modcache_audit(a.tasks)
    if a.cmd == "run":
        return run(a.tasks, a.force, a.redo_failed, a.force_tasks, a.reflatten)
    if a.cmd == "rules":
        write_rules()
        return 0
    return clean_check()


if __name__ == "__main__":
    sys.exit(main())
