"""E12 laptop controller (unit U8): bounded subcommands the main session runs during the paid run.

Plan: docs/experiments/E12/implementation-plan.md v0.3.1, "Laptop controller (U8)" rules 1-3 and
"Frozen interfaces" (control files, two-stage endpoint.json, admitted job hash in genpath.json,
attempts.json, retention receipt, job outcomes and skip records). Each subcommand makes only the
remote calls it names, prints one JSON object on its last stdout line, and exits with the
contract codes (`contracts.EXIT_*`: 0 ok, 1 failed, 2 budget refusal, 3 gate refusal, 4 BLOCKED).
It never terminates a pod: termination stays a separate manual `pod_down.py` command.

CLAUDE.md "RunPod operations": one consolidated ssh call per check, polls minutes apart; `pgrep -f`
patterns have their first character bracketed (and no remote script here contains the bare
names); a detached launch is verified by new log lines ~20 s later, not by pgrep alone; fetch,
verify and terminate are separate commands. ssh host/port come from the slot's .env keys
(POD_SSH_HOST<slot>, POD_SSH_PORT<slot>, as pod_push.sh and e9_orchestrate.py read them); the
pod's creation time and rate from its `created` event in logs/pods_E12.jsonl.

  poll       ONE ssh call: the newest logs/e12/run_*.log's E12_ tags, the MANIFEST.* files present,
             the control files, chain/driver processes; elapsed pod-hours from the pod record.
  stop       writes STOP for every recipe, then polls (minutes apart, bounded) until the chain
             has ended (E12_CHAIN_STOPPED / DONE / FAILED / PAUSED) and no chain or driver process
             remains; else exit 1.
  induction  pulls and verifies MANIFEST.eval-poison, runs `e12_judge.py decide --stage poison`
             and `e12_analyze.py decide --stage poison`, prints the outcome and the next action:
             continue | rank32-bench (rank 1, once: rank 32 never started) | terminate
             (rank32, and tan32 under Amendment D: no further fallback).
  admit-hf   prices both mutually exclusive complete HF branches, 107 without the rescue and
             214 with it, at the dearer one (budget CLI --gen-path hf --branch hf-complete, which
             writes bench/projection.hf.json locally) and authorizes their union
             (`contracts.hf_complete_jobs`, review r1 finding 1); if admitted writes the local
             admission record (`contracts.admission_path`, ADMISSION_FIELDS, branch
             "hf-complete") and pushes cuts.json, the same admission.json (into the pod's results
             tree) and genpath.json (admitted, jobs, jobs_sha256, last) in ONE ssh call; else exit
             2. An existing admission is never re-priced (a missing local record is rebuilt from
             genpath.json).
  endpoint   --stage repair107|rescue214: pulls the endpoint evaluations, runs both `decide`
             commands, writes endpoint.json stage 1 ({rescue_triggered}, plus endpoint 107 when
             false) or stage 2 (endpoint 214, or endpoint null with the not-established outcome).
  launch     --recipe R [--resume] [--last-step STEP]: refuses unless the previous chain is
             verified ended (E12_CHAIN_PAUSED counts as ended); a start records the attempt in
             attempts.json (once per recipe), a resume keeps its id;
             `E12_RECIPE=R [E12_LAST_STEP=STEP] bash scripts/pod_run_e12.sh --detach` (the chain
             pauses after STEP), then new log lines ~20 s later.
  archive    refused unless the chain is verified ended; if `retained(recipe)` fails after a
             recovery pull: the pod's `upload` step (not with --no-upload); if that still
             fails, a `tar` pull of the adapter files (earlier verified ones linked), verified
             against every ckpt.json, and a `local` retention receipt; then the pod's ckpt.json
             records are synced again, so a checkpoint made after the snapshot leaves
             `retained` false (code review r2 finding 2). Prints {"retained": {recipe: bool}};
             without --recipe, every recipe with an attempt. This, after a verified `stop`, is
             the only archive that declares final retention.
             `--while-running` (the watchdog's early archival, an optimisation that never
             declares final retention; review r1 finding 2): no ended-chain gate and no upload
             step; ONE ssh tar of the pod's ckpt.json records (missing ones installed
             locally), then the verified tar pull (files an earlier local pull verified are
             linked, not re-pulled) and a `local` receipt; prints {"retained": {recipe: bool}}; without --recipe it
             covers every recipe with an attempt, the latest first.
  complete   every job of the current `planned_jobs(...)` has a verified output or a terminal
             skip record, every step manifest verifies and is bound to the current job list,
             (HF) the endpoint is final and inside the admitted list, the local admission.json
             agrees with genpath.json, the cuts in force equal the admission's, and
             `retained(recipe)`.

Usage: uv run python scripts/e12_control.py {poll|stop|induction|admit-hf|endpoint|launch|archive|
       complete} --slot _A [--recipe R] [--stage S] [--resume] [--no-pull] [--while-running]
       [--no-upload] [--stop-polls N] [--poll-interval-s S] [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import posixpath
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e12 import contracts as C
from emx.e12 import deps
from emx.e12 import gen as G

PY = sys.executable
SSH_TIMEOUT_S = 120
PULL_TIMEOUT_S = 3600
DECIDE_TIMEOUT_S = 3600
BUDGET_TIMEOUT_S = 300
TAR_TIMEOUT_S = 4 * 3600
POLL_TAGS = 40
POD_PY = "/ws/venv-peft/bin/python"  # stdlib is enough for `e12_pod.py recovery`
CHAIN_END = {"E12_CHAIN_DONE": "done", "E12_CHAIN_STOPPED": "stopped", "E12_CHAIN_FAILED": "failed",
             "E12_CHAIN_PAUSED": "paused"}  # paused: E12_LAST_STEP reached (launch --last-step)
# "paused" is an ended chain (stop, launch, archive and complete accept it); complete still
# requires every job to verify, so a paused chain is never complete by its tag.
ENDED = ("done", "stopped", "failed", "paused")
# A chain that died without an end tag (no process left) counts as ended for stop, archive and
# a same-attempt resume, never as complete (code review r3 finding 3): "exited" is only reached
# when pgrep finds no chain or driver process.
RECOVERABLE = (*ENDED, "exited")
NOT_ESTABLISHED = "repair_not_established"
EVAL_STEP_OF_KIND = {"traj": "eval-traj", "nudge": "eval-nudge"}


class Refused(Exception):
    """A gate refused the command (exit 3)."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> dict | None:
    try:
        out = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def last_json(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        if line.strip():
            try:
                out = json.loads(line)
            except ValueError:
                return None
            return out if isinstance(out, dict) else None
    return None


def emit(obj: dict) -> None:
    print(json.dumps(obj, sort_keys=True, default=str), flush=True)


# --- running commands ---------------------------------------------------------------------------


@dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""


class Runner:
    """subprocess, run from the repo root; with `dry_run` every command is printed, none runs."""

    def __init__(self, root: Path, dry_run: bool = False):
        self.root, self.dry_run = Path(root), dry_run

    def run(self, argv: list[str], timeout: float) -> Result:
        if self.dry_run:
            print("+ " + shlex.join(argv), flush=True)
            return Result(0)
        try:
            p = subprocess.run(argv, cwd=self.root, capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout:.0f} s: {argv[0]}")
        return Result(p.returncode, p.stdout, p.stderr)

    def pipe(self, src: list[str], dst: list[str], timeout: float) -> Result:
        """`src | dst` (the tar pull); rc is the first non-zero of the two."""
        if self.dry_run:
            print("+ " + shlex.join(src) + " | " + shlex.join(dst), flush=True)
            return Result(0)
        a = subprocess.Popen(src, cwd=self.root, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             stderr=subprocess.PIPE)
        b = subprocess.Popen(dst, cwd=self.root, stdin=a.stdout, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        a.stdout.close()
        try:
            _, berr = b.communicate(timeout=timeout)
            _, aerr = a.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            a.kill()
            b.kill()
            return Result(124, "", "tar pull timed out")
        rc = a.returncode or b.returncode
        return Result(rc, "", (aerr or b"").decode(errors="replace") + berr.decode(errors="replace"))

    def sleep(self, seconds: float) -> None:
        if self.dry_run:
            print(f"+ sleep {seconds:.0f}", flush=True)
            return
        time.sleep(seconds)


@dataclass
class Ctx:
    root: Path
    slot: str
    recipe: str
    runner: Runner
    dry: bool = False
    pod_root: str = C.POD_ROOT  # tests map the pod's /ws/emx and /ws/e12 into a tmp dir
    pod_scratch: str = C.SCRATCH
    pod_py: str = POD_PY
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    stop_polls: int = 8  # stop: up to 8 polls ...
    poll_interval_s: float = 180.0  # ... 3 min apart (CLAUDE.md: minutes, not seconds)
    progress_wait_s: float = 20.0  # launch: real progress checked after ~20 s
    job_polls: int = 10  # archive: waiting for the detached upload step
    _pod: dict | None = None

    @property
    def pod(self) -> dict:
        if self._pod is None:
            self._pod = pod_record(self.root, self.slot, self.dry)
        return self._pod


# --- the pod record (.env slot keys, logs/pods_E12.jsonl) ---------------------------------------


def read_env(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = (Path(root) / ".env").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


def pod_events(root: Path) -> list[dict]:
    path = Path(root) / "logs" / f"pods_{C.EXP}.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def pod_record(root: Path, slot: str, dry: bool = False) -> dict:
    """{pod_id, host, port, user, created_utc, rate_usd_h} of the slot's pod; Refused if the
    slot has no pod (a dry run uses placeholders)."""
    env = read_env(root)
    pod_id = env.get(f"RUNPOD_POD_ID{slot}") or None
    host, port = env.get(f"POD_SSH_HOST{slot}"), env.get(f"POD_SSH_PORT{slot}")
    events = pod_events(root)
    created = [e for e in events if e.get("event") == "created"
               and (e.get("pod_id") == pod_id if pod_id else e.get("slot") == slot)]
    pod_id = pod_id or (created[-1].get("pod_id") if created else None)
    rate = None
    if created:
        rate = created[-1].get("rate_usd_h")
    for e in events:
        if e.get("pod_id") == pod_id and e.get("event") == "rate" and e.get("rate_usd_h"):
            rate = e["rate_usd_h"]
    if not host or not port:
        if not dry:
            raise Refused(f"no pod in slot {slot} (.env POD_SSH_HOST{slot}/POD_SSH_PORT{slot})")
        host, port = "HOST", "PORT"
    return {
        "pod_id": pod_id, "host": host, "port": str(port),
        "user": env.get("POD_SSH_USER") or "root",
        "created_utc": created[-1]["utc"] if created else None,
        "rate_usd_h": float(rate) if rate else None,
    }


def elapsed_hours(ctx: Ctx) -> float | None:
    t0 = ctx.pod.get("created_utc")
    if not t0:
        return None
    start = datetime.fromisoformat(t0)
    return round(max(0.0, (ctx.now() - start).total_seconds() / 3600), 4)


# --- remote calls -------------------------------------------------------------------------------


def ssh_argv(ctx: Ctx, script: str) -> list[str]:
    p = ctx.pod
    return [
        "ssh", "-n", "-p", p["port"], "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=4", "-o", "StrictHostKeyChecking=accept-new",
        f"{p['user']}@{p['host']}", script,
    ]


def remote(op: str, body: str) -> str:
    """A remote script; its first line names the operation (a comment, for logs and tests)."""
    return f"# e12ctl:{op}\n{body}"


def ssh(ctx: Ctx, op: str, body: str, timeout: float = SSH_TIMEOUT_S) -> Result:
    return ctx.runner.run(ssh_argv(ctx, remote(op, body)), timeout=timeout)


def podp(ctx: Ctx, path: str) -> str:
    """A contracts pod path (/ws/emx/..., /ws/e12/..., or relative to /ws/emx) on this pod."""
    if path == C.POD_ROOT or path.startswith(C.POD_ROOT + "/"):
        return ctx.pod_root + path[len(C.POD_ROOT):]
    if path == C.SCRATCH or path.startswith(C.SCRATCH + "/"):
        return ctx.pod_scratch + path[len(C.SCRATCH):]
    if path.startswith("/"):
        raise ValueError(f"not a pod path: {path}")
    return f"{ctx.pod_root}/{path}"


CTL_REL = C.CONTROL_ROOT[len(C.POD_ROOT) + 1:]  # control/E12
ATTEMPTS_REL = C.ATTEMPTS_FILE[len(C.POD_ROOT) + 1:]


def poll_body(ctx: Ctx) -> str:
    q = shlex.quote
    ctl = CTL_REL
    # Never write the chain's or the driver's bare name here: this script's own command line
    # would match the pgrep patterns.
    return f"""cd {q(ctx.pod_root)} || exit 97
L=$(ls -t {C.POD_LOG_DIR}/run_*.log 2>/dev/null | head -n 1)
echo "LOG $L"
if [ -n "$L" ]; then
  echo "LINES $(wc -l < "$L" | tr -d ' ')"
  grep -h '^{C.TAG_PREFIX}' "$L" | tail -n {POLL_TAGS} | sed 's/^/TAG /'
fi
for f in {C.RESULTS}/*/MANIFEST.*.sha256; do [ -f "$f" ] && echo "MAN $f"; done
for f in {ATTEMPTS_REL} {ctl}/*/STOP {ctl}/*/cuts.json {ctl}/*/genpath.json {ctl}/*/endpoint.json; do
  [ -f "$f" ] && echo "CTL $f $(base64 < "$f" | tr -d '\\n')"
done
if pgrep -f '[p]od_run_e12' >/dev/null 2>&1; then echo "PROC chain"; fi
if pgrep -f '[e]12_pod' >/dev/null 2>&1; then echo "PROC driver"; fi
echo POLL_OK
"""


@dataclass
class PodState:
    ok: bool = False
    log: str | None = None
    lines: int = 0
    tags: list[str] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    procs: list[str] = field(default_factory=list)
    ctl: dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def chain_tag(self) -> str | None:
        return next((t for t in reversed(self.tags) if t.split(" ")[0] in CHAIN_END), None)

    @property
    def state(self) -> str:
        """running | done | stopped | failed | exited (no end tag, no process) | none (no log)."""
        if self.procs:
            return "running"
        if self.chain_tag:
            return CHAIN_END[self.chain_tag.split(" ")[0]]
        return "exited" if self.log else "none"

    def steps(self, recipe: str) -> list[str]:
        pre = f"{C.rdir(recipe)}/MANIFEST."
        return [m[len(pre):-len(".sha256")] for m in self.manifests if m.startswith(pre)]

    def control(self, recipe: str, name: str) -> dict | None:
        text = self.ctl.get(f"{CTL_REL}/{recipe}/{name}")
        if text is None:
            return None
        try:
            out = json.loads(text) if text.strip() else {}
        except ValueError:
            return {"_unreadable": text}
        return out if isinstance(out, dict) else {"_unreadable": text}

    def attempts(self) -> dict:
        try:
            out = json.loads(self.ctl.get(ATTEMPTS_REL) or "{}")
        except ValueError:
            return {}
        return out if isinstance(out, dict) else {}


def parse_poll(res: Result) -> PodState:
    st = PodState()
    for line in res.out.splitlines():
        head, _, rest = line.partition(" ")
        if head == "LOG":
            st.log = rest.strip() or None
        elif head == "LINES":
            st.lines = int(rest) if rest.strip().isdigit() else 0
        elif head == "TAG":
            st.tags.append(rest)
        elif head == "MAN":
            st.manifests.append(rest.strip())
        elif head == "CTL":
            path, _, b64 = rest.partition(" ")
            st.ctl[path] = base64.b64decode(b64.strip()).decode() if b64.strip() else ""
        elif head == "PROC":
            st.procs.append(rest.strip())
        elif head == "POLL_OK":
            st.ok = res.rc == 0
    if not st.ok:
        st.error = f"poll failed (ssh rc {res.rc}): {res.err.strip()[-300:]}"
    return st


def poll_pod(ctx: Ctx) -> PodState:
    res = ssh(ctx, "poll", poll_body(ctx))
    return PodState(error="dry run: nothing ran") if ctx.dry else parse_poll(res)


def write_lines(ctx: Ctx, files: dict[str, str]) -> list[str]:
    """Shell lines writing {contracts pod path: text} atomically, in order, each echoed back."""
    q = shlex.quote
    lines = []
    for path, text in files.items():
        p = podp(ctx, path)
        b64 = base64.b64encode(text.encode()).decode()
        lines.append(f"mkdir -p {q(posixpath.dirname(p))} && printf '%s' {q(b64)} | base64 -d "
                     f"> {q(p + '.tmp')} && mv -f {q(p + '.tmp')} {q(p)} || exit 1")
        lines.append(f"printf 'WROTE %s %s\\n' {q(p)} \"$(base64 < {q(p)} | tr -d '\\n')\"")
    return lines


def wrote_ok(ctx: Ctx, res: Result, files: dict[str, str]) -> bool:
    """Every file was read back on the pod with exactly the text sent."""
    if ctx.dry:
        return True
    got = {}
    for line in res.out.splitlines():
        if line.startswith("WROTE "):
            _, p, b64 = (line.split(" ", 2) + [""])[:3]
            got[p] = base64.b64decode(b64).decode() if b64 else ""
    bad = [p for p, t in files.items() if got.get(podp(ctx, p)) != t]
    if bad:
        print(f"control write failed (rc {res.rc}) for {bad}: {res.err.strip()[-300:]}",
              file=sys.stderr)
    return not bad


def write_files(ctx: Ctx, op: str, files: dict[str, str]) -> bool:
    """Write the files in ONE ssh call and verify the read-back."""
    res = ssh(ctx, op, "\n".join(write_lines(ctx, files)))
    return wrote_ok(ctx, res, files) and (res.rc == 0 or ctx.dry)


def dumps(obj: dict) -> str:
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


# --- local commands (other units' CLIs, by their frozen CLI_USAGE strings) ----------------------


def pull(ctx: Ctx, recipe: str, steps: list[str]) -> bool:
    """`pod_pull_results.sh --exp E12 --slot _A --recipe R --manifest MANIFEST.<step>.sha256 ...`
    (U5's E12 block: results/E12/<recipe>/ by named step manifests; none = the closing manifest
    and the steps it lists), then a local re-check of every pulled manifest."""
    argv = ["bash", "scripts/pod_pull_results.sh", "--exp", C.EXP, "--slot", ctx.slot,
            "--recipe", recipe]
    for s in steps:
        argv += ["--manifest", f"MANIFEST.{s}.sha256"]
    res = ctx.runner.run(argv, timeout=PULL_TIMEOUT_S)
    if ctx.dry:
        return True
    if res.rc != 0:
        print(f"pull of {steps} failed (rc {res.rc}): {res.err.strip()[-500:]}", file=sys.stderr)
        return False
    probs = [p for s in steps for p in manifest_problems(ctx.root, C.manifest_path(recipe, s))]
    if probs:
        print(f"pulled manifests do not verify: {probs[:10]}", file=sys.stderr)
    return not probs


def decide(ctx: Ctx, recipe: str, stage: str) -> tuple[int, dict | None]:
    """`e12_judge.py decide --stage S`, then `e12_analyze.py decide --stage S`; returns the
    first non-zero exit code (or 0) and the analysis's decision object."""
    res = ctx.runner.run([PY, "scripts/e12_judge.py", "decide", "--stage", stage,
                          "--recipe", recipe], timeout=DECIDE_TIMEOUT_S)
    if res.rc != 0:
        print(f"e12_judge.py decide --stage {stage} exit {res.rc}: {res.err.strip()[-500:]}",
              file=sys.stderr)
        return res.rc, None
    res = ctx.runner.run([PY, "scripts/e12_analyze.py", "decide", "--stage", stage,
                          "--recipe", recipe], timeout=DECIDE_TIMEOUT_S)
    if res.rc != 0:
        print(f"e12_analyze.py decide --stage {stage} exit {res.rc}: {res.err.strip()[-500:]}",
              file=sys.stderr)
        return res.rc, None
    dec = last_json(res.out)
    if dec is None and not ctx.dry:  # fall back to the decisions file the analysis wrote
        dec = (read_json(ctx.root / C.decisions_path(recipe)) or {})
        dec = (dec.get("stages") or dec).get(stage)
    elif dec is not None and isinstance(dec.get(stage), dict):
        dec = dec[stage]
    return 0, dec


def n_induced(dec: dict) -> int:
    """Seeds that induced, from the poison decision (several accepted shapes)."""
    if isinstance(dec.get("n_induced"), int):
        return dec["n_induced"]
    if isinstance(dec.get("induced_seeds"), list):
        return len(dec["induced_seeds"])
    for k in ("induction", "per_seed", "seeds"):
        v = dec.get(k)
        if isinstance(v, dict):
            return sum(bool(x.get("induced") if isinstance(x, dict) else x) for x in v.values())
    raise ValueError(f"no induction count in the poison decision: {sorted(dec)}")


# --- manifests, outputs, skip records -----------------------------------------------------------


def read_manifest(path: Path) -> list[tuple[str, str]]:
    out = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            sha, name = line.split("  ", 1) if "  " in line else line.split(" *", 1)
            out.append((sha.strip(), name.lstrip("*").strip()))
    return out


def manifest_problems(root: Path, man_rel: str) -> list[str]:
    man = Path(root) / man_rel
    if not man.is_file():
        return [f"{man_rel} missing"]
    try:
        entries = read_manifest(man)
    except (OSError, ValueError) as e:
        return [f"{man_rel} unreadable: {e}"]
    probs = []
    for sha, name in entries:
        f = Path(root) / name
        if name.startswith("/") or ".." in Path(name).parts:
            probs.append(f"{man_rel}: bad path {name}")
        elif not f.is_file():
            probs.append(f"{man_rel}: {name} missing")
        elif sha256_file(f) != sha:
            probs.append(f"{man_rel}: {name} sha256 differs")
    return probs


def step_of(job: C.Job) -> str:
    """The eval step that generates a job (the plan's pod chain, steps 6 and 10-13)."""
    if job.kind in EVAL_STEP_OF_KIND:
        return EVAL_STEP_OF_KIND[job.kind]
    arm = C.parse_key(job.key).arm
    if job.kind == "full":
        return "eval-poison" if arm == "P" else "eval-endpoints"
    return "eval-poison" if arm in (None, "P") else "eval-cue-task"


def recorded_jobs_sha(root: Path, recipe: str, step: str) -> str | None:
    """The jobs_sha256 a step manifest is bound to: the driver's jobs record
    bench/<step>.jobs.json (scripts/e12_pod.py), else a sidecar MANIFEST.<step>.json, else a JSON
    record the manifest lists with {"step": step, "jobs_sha256": ...}."""
    rec = read_json(Path(root) / C.bench_path(recipe, f"{step}.jobs"))
    if rec and rec.get("jobs_sha256"):
        return rec["jobs_sha256"]
    side = read_json(Path(root) / f"{C.rdir(recipe)}/MANIFEST.{step}.json")
    if side and side.get("jobs_sha256"):
        return side["jobs_sha256"]
    man = Path(root) / C.manifest_path(recipe, step)
    if not man.is_file():
        return None
    for _, name in read_manifest(man):
        f = Path(root) / name
        if name.endswith(".json") and not name.endswith(".meta.json") and f.is_file() \
                and f.stat().st_size < 1 << 20:
            rec = read_json(f)
            if rec and rec.get("step") == step and rec.get("jobs_sha256"):
                return rec["jobs_sha256"]
    return None


def skip_problem(root: Path, recipe: str, job: C.Job) -> str | None:
    """None if the job has a supported terminal skip record, by the driver's own predicate
    (`emx.e12.deps.skip_problem`, code review r2 finding 4): status JOB_SKIP_STATUS naming a run
    of the job's dependency chain, that run or an ancestor in the chain explicitly `failed`, and
    the checkpoint's state failed. No status, a run outside the chain, or a producer now ok or
    pending: refused."""
    return deps.skip_problem(root, recipe, job.kind, job.key)


def job_problem(root: Path, recipe: str, job: C.Job) -> str | None:
    try:
        if G.verify_output(root, recipe, job.kind, job.key):
            return None
    except (OSError, ValueError):
        pass
    return skip_problem(root, recipe, job)


# --- retention (the one predicate: archive, complete and every early exit) ----------------------


def ckpt_records(recipe: str, root: Path) -> list[tuple[str, dict | None]]:
    base = Path(root) / C.rdir(recipe) / "ckpt"
    return [(p.parent.name, read_json(p)) for p in sorted(base.glob("*/ckpt.json"))]


def ckpt_adapter_files(ck: dict | None) -> dict[str, str]:
    """{file name: sha256} of a checkpoint's adapter files (ckpt.json `adapter_files`)."""
    files = (ck or {}).get("adapter_files")
    return files if isinstance(files, dict) else {}


def retention_problems(recipe: str, root: str | Path = C.REPO_ROOT) -> list[str]:
    root = Path(root)
    rel = C.retention_receipt_path(recipe)
    if not (root / rel).is_file():
        return [f"no retention receipt {rel}"]
    r = read_json(root / rel)
    if r is None:
        return [f"{rel} unreadable"]
    missing = [k for k in C.BENCH_FIELDS["retention_receipt"] if k not in r]
    if missing:
        return [f"{rel} lacks {missing}"]
    probs = []
    files = r["files"] if isinstance(r["files"], dict) else {}
    if r["recipe"] != recipe:
        probs.append(f"{rel}: recipe {r['recipe']!r}")
    if r["method"] == "hf":
        if r["private"] is not True:
            probs.append(f"{rel}: hf repo privacy not asserted (private={r['private']!r})")
        if not (isinstance(r["commit_sha"], str) and r["commit_sha"]):
            probs.append(f"{rel}: hf receipt has no commit sha")
        if not r["location"]:
            probs.append(f"{rel}: hf receipt has no repo id")
    elif r["method"] == "local":
        loc = Path(r["location"] or "")
        loc = loc if loc.is_absolute() else root / loc
        if not r["location"] or not loc.is_dir():
            probs.append(f"{rel}: local directory {r['location']!r} missing")
        else:
            probs += [f"{rel}: {n} missing under {r['location']}" for n in files
                      if not (loc / n).is_file()]
    else:
        probs.append(f"{rel}: method {r['method']!r} not in {C.RETENTION_METHODS}")
    for key, ck in ckpt_records(recipe, root):
        af = ckpt_adapter_files(ck)
        if not af:
            probs.append(f"ckpt {key}: ckpt.json lists no adapter files")
        for name, sha in af.items():
            if files.get(f"{key}/{name}") != sha:
                probs.append(f"ckpt {key}: {name} not in the receipt with its ckpt.json sha256")
    return probs


def retained(recipe: str, root: str | Path = C.REPO_ROOT) -> bool:
    """Receipt exists; method hf (private True, commit sha) or local (directory present); every
    existing checkpoint's adapter files listed with sha256 equal to its ckpt.json."""
    return not retention_problems(recipe, root)


# --- subcommands --------------------------------------------------------------------------------


def summary(ctx: Ctx, st: PodState) -> dict:
    return {
        "slot": ctx.slot, "pod_id": ctx.pod.get("pod_id"), "ok": st.ok, "error": st.error or None,
        "log": st.log, "log_lines": st.lines, "tags": st.tags[-10:], "chain_tag": st.chain_tag,
        "state": st.state, "processes": st.procs,
        "manifests": {r: st.steps(r) for r in C.RECIPES},
        "control": {r: [n for n in C.CONTROL_FILES if f"{CTL_REL}/{r}/{n}" in st.ctl]
                    for r in C.RECIPES},
        "attempts": st.attempts(), "elapsed_pod_hours": elapsed_hours(ctx), "utc": utc_now(),
    }


def done(ctx: Ctx, cmd: str, rc: int, **fields) -> int:
    emit({"cmd": cmd, "recipe": ctx.recipe, "ok": rc == C.EXIT_OK, "exit": rc,
          "dry_run": ctx.dry, **fields})
    return rc


def cmd_poll(ctx: Ctx) -> int:
    st = poll_pod(ctx)
    emit({"cmd": "poll", "dry_run": ctx.dry, **summary(ctx, st)})
    return C.EXIT_OK if st.ok or ctx.dry else C.EXIT_FAILED


def cmd_stop(ctx: Ctx) -> int:
    """STOP for every recipe (whichever chain runs), then wait, bounded, for the chain's end."""
    note = dumps({"utc": utc_now(), "by": "e12_control stop"})
    if not write_files(ctx, "stop", {C.control_path(r, "STOP"): note for r in C.RECIPES}):
        return done(ctx, "stop", C.EXIT_FAILED, reason="STOP write failed")
    st = PodState()
    for i in range(ctx.stop_polls):
        if i:
            ctx.runner.sleep(ctx.poll_interval_s)
        st = poll_pod(ctx)
        if ctx.dry:
            return done(ctx, "stop", C.EXIT_OK)
        if st.ok and st.state in (*RECOVERABLE, "none"):
            return done(ctx, "stop", C.EXIT_OK, state=st.state, chain_tag=st.chain_tag,
                        polls=i + 1, interrupted=st.state == "exited")
    return done(ctx, "stop", C.EXIT_FAILED, state=st.state, chain_tag=st.chain_tag,
                processes=st.procs, polls=ctx.stop_polls,
                reason="stop not acknowledged: no E12_CHAIN_STOPPED/DONE with no chain process")


def _code(rc: int) -> int:
    return rc if rc in (C.EXIT_BUDGET, C.EXIT_REFUSED, C.EXIT_BLOCKED) else C.EXIT_FAILED


def cmd_induction(ctx: Ctx) -> int:
    R = ctx.recipe
    st = poll_pod(ctx)
    if not ctx.dry:
        if not st.ok:
            return done(ctx, "induction", C.EXIT_FAILED, reason=st.error)
        if "eval-poison" not in st.steps(R):
            return done(ctx, "induction", C.EXIT_BLOCKED, reason="MANIFEST.eval-poison not yet")
    if not pull(ctx, R, ["eval-poison"]):
        return done(ctx, "induction", C.EXIT_FAILED, reason="eval-poison pull/verify failed")
    rc, dec = decide(ctx, R, "poison")
    if rc:
        return done(ctx, "induction", _code(rc), reason=f"decide --stage poison exit {rc}")
    if ctx.dry:
        return done(ctx, "induction", C.EXIT_OK)
    try:
        n = n_induced(dec or {})
    except ValueError as e:
        return done(ctx, "induction", C.EXIT_FAILED, reason=str(e))
    if n >= C.INDUCTION_MIN_SEEDS:
        action = "continue"
    elif R == "rank1" and "rank32" not in st.attempts():
        action = "rank32-bench"  # once only: never after a rank-32 attempt was started
    else:
        action = "terminate: organism not established"  # rank32, tan32: no further fallback
    return done(ctx, "induction", C.EXIT_OK, n_induced=n, induced=n >= C.INDUCTION_MIN_SEEDS,
                next_action=action, attempts=st.attempts())


def api_spent(root: Path) -> float:
    from emx import spend

    return spend.spent("judge", exp=C.EXP, ledger=Path(root) / "logs" / "costs.jsonl")


def job_dicts(jobs: list[C.Job]) -> list[dict]:
    return [{"kind": j.kind, "key": j.key, "n_rows": j.n_rows} for j in jobs]


HF_BRANCH = "hf-complete"


def write_local_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps(obj))
    tmp.replace(path)


def admission_record(gp: dict) -> dict | None:
    """The admission record (`contracts.ADMISSION_FIELDS`) held in an admitted genpath.json."""
    if gp.get("admitted") is not True or any(k not in gp for k in C.ADMISSION_FIELDS):
        return None
    return {k: gp[k] for k in C.ADMISSION_FIELDS}


def admission_problems(root: Path, recipe: str, gp: dict) -> list[str]:
    """The local admission.json exists, carries every field, is admitted, matches its own job
    list and agrees with the pod's genpath.json on jobs_sha256 and cuts."""
    rel = C.admission_path(recipe)
    adm = read_json(Path(root) / rel)
    if adm is None:
        return [f"no readable admission record {rel}"]
    probs = []
    missing = [k for k in C.ADMISSION_FIELDS if k not in adm]
    if missing:
        probs.append(f"{rel} lacks {missing}")
    if adm.get("admitted") is not True:
        probs.append(f"{rel} is not admitted")
    try:
        jobs = [C.Job(j["kind"], j["key"], int(j["n_rows"])) for j in adm.get("jobs") or []]
    except (KeyError, TypeError, ValueError):
        jobs = []
    if not jobs or C.jobs_sha256(jobs) != adm.get("jobs_sha256"):
        probs.append(f"{rel}: the job list does not match its jobs_sha256")
    if gp.get("jobs_sha256") != adm.get("jobs_sha256"):
        probs.append(f"{rel}: jobs_sha256 differs from genpath.json's")
    if sorted(gp.get("cuts") or []) != sorted(adm.get("cuts") or []):
        probs.append(f"{rel}: cuts differ from genpath.json's")
    return probs


def save_projection(ctx: Ctx, recipe: str, proj: dict) -> None:
    """The persisted projection (review r1 finding 6): the budget CLI writes it with --root; if
    the file is missing or differs from the record it printed, the printed record is written."""
    p = ctx.root / C.projection_path(recipe, "hf")
    if read_json(p) != proj:
        write_local_json(p, proj)


def cmd_admit_hf(ctx: Ctx) -> int:
    """Price both complete HF branches (the dearer one), authorize their union; persist the
    projection and the admission record locally, then push cuts.json, admission.json and
    genpath.json to the pod in ONE ssh call."""
    R, cmd = ctx.recipe, "admit-hf"
    st = poll_pod(ctx)
    gp = st.control(R, "genpath.json") or {}
    if not ctx.dry:
        if not st.ok:
            return done(ctx, cmd, C.EXIT_FAILED, reason=st.error)
        if gp.get("gen_path") != "hf":
            raise Refused(f"genpath.json says {gp.get('gen_path')!r}, not hf")
        if gp.get("admitted") is True:  # never re-priced: the admitted list stays fixed
            restored = False
            adm_p = ctx.root / C.admission_path(R)
            if read_json(adm_p) is None and admission_record(gp) is not None:
                write_local_json(adm_p, admission_record(gp))  # rebuilt from the pod's record
                restored = True
            probs = admission_problems(ctx.root, R, gp)
            return done(ctx, cmd, C.EXIT_FAILED if probs else C.EXIT_OK, already_admitted=True,
                        jobs_sha256=gp.get("jobs_sha256"), cuts=gp.get("cuts"),
                        restored_local_admission=restored, problems=probs)
    # the training steps too: their status records are what budget.py counts as done (without
    # them it priced all training as remaining; found on the real admission, 2026-10-09)
    if not pull(ctx, R, [s for s in ("smoke", "lora-check", "hf-bench", *C.TRAIN_STEPS)
                         if ctx.dry or s in st.steps(R)]):
        return done(ctx, cmd, C.EXIT_FAILED, reason="bench pull/verify failed")
    hours = elapsed_hours(ctx)
    if hours is None and not ctx.dry:
        raise Refused("the pod's creation time is unknown (logs/pods_E12.jsonl)")
    rate = ctx.pod.get("rate_usd_h") or max(r for _, r in C.FALLBACK_GPUS)
    api = api_spent(ctx.root)
    argv = [PY, "-m", "emx.e12.budget", "admit", "--recipe", R, "--stage", "hf",
            "--gen-path", "hf", "--branch", HF_BRANCH,
            "--elapsed-h", f"{hours or 0.0:.4f}", "--gpu-rate", f"{rate:.4f}",
            "--api-spent", f"{api:.4f}", "--root", str(ctx.root)]
    res = ctx.runner.run(argv, timeout=BUDGET_TIMEOUT_S)
    if ctx.dry:
        return done(ctx, cmd, C.EXIT_OK)
    proj = last_json(res.out)
    if proj and proj.get("verdict") in C.PROJECTION_VERDICTS:
        save_projection(ctx, R, proj)  # a refusal is recorded too
    if res.rc == C.EXIT_BUDGET or (proj or {}).get("verdict") == "refuse":
        return done(ctx, cmd, C.EXIT_BUDGET, projection=proj,
                    next_action="stop, archive, pull, terminate, report")
    if res.rc != 0 or not proj or proj.get("verdict") != "admit":
        return done(ctx, cmd, C.EXIT_FAILED, reason=f"budget exit {res.rc}", projection=proj)
    if proj.get("gen_path") != "hf" or proj.get("branch") != HF_BRANCH:
        return done(ctx, cmd, C.EXIT_FAILED, projection=proj,
                    reason="the projection did not price both complete HF branches")
    cuts = sorted(int(c) for c in proj.get("cuts") or [])
    jobs = C.hf_complete_jobs(R, cuts)
    sha = C.jobs_sha256(jobs)
    if proj.get("jobs_sha256") != sha:
        return done(ctx, cmd, C.EXIT_FAILED, projection=proj,
                    reason=f"the projection's jobs_sha256 {proj.get('jobs_sha256')} is not the "
                           f"HF admission set's {sha}")
    utc = utc_now()
    adm = {"gen_path": "hf", "admitted": True, "cuts": cuts, "jobs_sha256": sha,
           "jobs": job_dicts(jobs), "branch": HF_BRANCH, "projection": proj,
           "source": "e12_control.py admit-hf", "utc": utc}
    gp_doc = {**gp, **adm, "n_answers_admitted": sum(j.n_rows for j in jobs),
              "utc_admitted": utc}
    adm_p = ctx.root / C.admission_path(R)
    write_local_json(adm_p, adm)
    # cuts, then the admission record, then genpath.json (the driver's trigger) last
    files = {C.control_path(R, "cuts.json"): dumps({"cuts": cuts, "stage": "hf", "utc": utc}),
             C.admission_path(R): dumps(adm),
             C.control_path(R, "genpath.json"): dumps(gp_doc)}
    if not write_files(ctx, cmd, files):
        adm_p.rename(adm_p.with_name(f"admission.unpushed_{utc.replace(':', '')}.json"))
        return done(ctx, cmd, C.EXIT_FAILED, reason="control write failed (the local admission "
                    "record was moved aside: nothing is admitted)")
    return done(ctx, cmd, C.EXIT_OK, cuts=cuts, jobs_sha256=sha, n_jobs=len(jobs),
                branch=HF_BRANCH, n_answers_admitted=gp_doc["n_answers_admitted"],
                priced_endpoint=proj.get("priced_endpoint"), admission=C.admission_path(R))


def decision_inputs(recipe: str, stage: str) -> list[C.Job]:
    """The per-model outputs a decision stage cannot run without (verified, or skipped)."""
    if stage == "repair107":
        keys = [C.format_key(a, s, 107) for a in ("A", "C") for s in C.TRAIN_SEEDS]
    else:
        keys = C.endpoint_keys(C.RESCUE_END)
    return [C.Job("full", k, C.KIND_ROWS["full"]) for k in keys]


def cmd_endpoint(ctx: Ctx, stage: str) -> int:
    R, cmd = ctx.recipe, "endpoint"
    st = poll_pod(ctx)
    ep = st.control(R, "endpoint.json") or {}
    if not ctx.dry:
        if not st.ok:
            return done(ctx, cmd, C.EXIT_FAILED, reason=st.error)
        if stage == "rescue214" and ep.get("rescue_triggered") is not True:
            raise Refused("rescue214 needs endpoint.json stage 1 with rescue_triggered true")
        if stage == "repair107" and ep.get("stage") == "rescue214":
            raise Refused("endpoint.json is already final (stage rescue214)")
        if "eval-endpoints" not in st.steps(R):
            return done(ctx, cmd, C.EXIT_BLOCKED, stage=stage,
                        reason="MANIFEST.eval-endpoints not yet")
    steps = ["eval-endpoints"] + [s for s in ("repair", "rescue") if ctx.dry or s in st.steps(R)]
    if not pull(ctx, R, steps):
        return done(ctx, cmd, C.EXIT_FAILED, stage=stage, reason="pull/verify failed")
    if not ctx.dry:
        waiting = [f"{j.kind}/{j.key}" for j in decision_inputs(R, stage)
                   if job_problem(ctx.root, R, j)]
        if waiting:
            return done(ctx, cmd, C.EXIT_BLOCKED, stage=stage, waiting=waiting,
                        reason="decision inputs not yet generated (no output, no skip record)")
    rc, dec = decide(ctx, R, stage)
    if rc:
        return done(ctx, cmd, _code(rc), stage=stage, reason=f"decide --stage {stage} exit {rc}")
    if ctx.dry:
        return done(ctx, cmd, C.EXIT_OK, stage=stage)
    dec = dec or {}
    doc: dict = {"stage": stage, "utc": utc_now(),
                 "source": f"e12_analyze.py decide --stage {stage}"}
    if stage == "repair107":
        rt = dec.get("rescue_triggered")
        if not isinstance(rt, bool):
            return done(ctx, cmd, C.EXIT_FAILED, stage=stage,
                        reason="the repair107 decision has no boolean rescue_triggered")
        if ep.get("stage") == "repair107" and ep.get("rescue_triggered") != rt:
            raise Refused(f"endpoint.json already says rescue_triggered={ep['rescue_triggered']}")
        doc["rescue_triggered"] = rt
        if not rt:
            doc["endpoint"] = 107
    else:
        doc["rescue_triggered"] = True
        doc["endpoint"] = C.RESCUE_END if dec.get("endpoint") == C.RESCUE_END else None
        if doc["endpoint"] is None:
            doc["outcome"] = NOT_ESTABLISHED
    if not write_files(ctx, cmd, {C.control_path(R, "endpoint.json"): dumps(doc)}):
        return done(ctx, cmd, C.EXIT_FAILED, stage=stage, reason="control write failed")
    return done(ctx, cmd, C.EXIT_OK, stage=stage, endpoint_json=doc)


def detach(ctx: Ctx, recipe: str, step: str | None = None,
           attempts: dict | None = None, last_step: str | None = None
           ) -> tuple[bool, str | None, int]:
    """ONE ssh call: (optionally) write attempts.json, remove the recipe's STOP, launch
    `E12_RECIPE=R bash scripts/pod_run_e12.sh [--step S] --detach`; returns (launched, the run
    log it printed, the log's line count right after the launch)."""
    q = shlex.quote
    files = {C.ATTEMPTS_FILE: dumps(attempts)} if attempts is not None else {}
    stepflag = f" --step {q(step)}" if step else ""
    if last_step is not None and last_step not in C.STEPS:
        raise Refused(f"unknown last step {last_step!r}")
    last = f" E12_LAST_STEP={q(last_step)}" if last_step else ""
    body = "\n".join([
        f"cd {q(ctx.pod_root)} || exit 97",
        *write_lines(ctx, files),
        f"rm -f {q(podp(ctx, C.control_path(recipe, 'STOP')))}",
        (f"OUT=$(E12_RECIPE={q(recipe)}{last} bash scripts/pod_run_e12.sh{stepflag} --detach "
         "2>&1); RC=$?"),
        'printf "%s\\n" "$OUT"',
        "L=$(printf '%s\\n' \"$OUT\" | sed -n 's/^RUN_LOG=//p' | tail -n 1)",
        'echo "LAUNCH_RC $RC"',
        ('if [ -n "$L" ] && [ -f "$L" ]; then echo "LINES0 $(wc -l < "$L" | tr -d \' \')"; '
         'else echo "LINES0 0"; fi'),
    ])
    res = ssh(ctx, "launch", body)
    if ctx.dry:
        return True, None, 0
    vals = {}
    for line in res.out.splitlines():
        for k in ("RUN_LOG=", "LAUNCH_RC ", "LINES0 "):
            if line.startswith(k):
                vals[k] = line[len(k):].strip()
    ok = res.rc == 0 and vals.get("LAUNCH_RC ") == "0" and bool(vals.get("RUN_LOG="))
    if not ok:
        print(f"launch failed (ssh rc {res.rc}, launcher rc {vals.get('LAUNCH_RC ')}): "
              f"{(res.out + res.err).strip()[-500:]}", file=sys.stderr)
    ok = ok and wrote_ok(ctx, res, files)
    lines0 = int(vals["LINES0 "]) if vals.get("LINES0 ", "").isdigit() else 0
    return ok, vals.get("RUN_LOG="), lines0


def same_log(a: str | None, b: str | None) -> bool:
    return bool(a and b) and posixpath.basename(a) == posixpath.basename(b)


def cmd_launch(ctx: Ctx, resume: bool, last_step: str | None = None) -> int:
    """Start (once per recipe, recorded in attempts.json) or resume the recipe's chain."""
    R, cmd = ctx.recipe, "launch"
    st = poll_pod(ctx)
    attempts = st.attempts()
    rec = attempts.get(R)
    if not ctx.dry:
        if not st.ok:
            return done(ctx, cmd, C.EXIT_FAILED, reason=st.error)
        if st.state not in (*RECOVERABLE, "none"):
            raise Refused(f"the previous chain is not verified ended (state {st.state}, "
                          f"processes {st.procs}, tag {st.chain_tag}); run stop first")
        if resume and not rec:
            raise Refused(f"no {R} attempt to resume (start it without --resume)")
        if not resume and rec:
            raise Refused(f"{R} was already started ({rec.get('attempt_id')}, "
                          f"{rec.get('started_utc')}); a recipe starts once: use --resume")
    new = None
    if not resume:
        utc = utc_now()
        rec = {"started_utc": utc, "attempt_id": f"{R}-{utc.replace(':', '').replace('-', '')}"}
        new = {**attempts, R: rec}
    ok, run_log, lines0 = detach(ctx, R, None, new, last_step=last_step)
    if not ok:
        return done(ctx, cmd, C.EXIT_FAILED, reason="launch failed", attempt=rec)
    ctx.runner.sleep(ctx.progress_wait_s)
    st2 = poll_pod(ctx)
    if ctx.dry:
        return done(ctx, cmd, C.EXIT_OK)
    fields = {"attempt": rec, "resumed": resume, "last_step": last_step, "run_log": run_log,
              "lines_at_launch": lines0,
              "lines": st2.lines, "state": st2.state, "chain_tag": st2.chain_tag}
    progressed = (st2.ok and same_log(st2.log, run_log) and st2.lines > lines0
                  and st2.state in ("running", "done", "stopped", "paused"))
    if not progressed:
        return done(ctx, cmd, C.EXIT_FAILED, reason="no real progress ~20 s after the launch",
                    **fields)
    return done(ctx, cmd, C.EXIT_OK, **fields)


def run_upload(ctx: Ctx, recipe: str) -> bool:
    """The pod's idempotent `upload` step, detached and waited for (bounded, minutes apart),
    then a pull of its manifest."""
    ok, run_log, _ = detach(ctx, recipe, "upload")
    if not ok:
        return False
    st = PodState()
    for i in range(ctx.job_polls):
        ctx.runner.sleep(ctx.poll_interval_s if i else ctx.progress_wait_s)
        st = poll_pod(ctx)
        if ctx.dry:
            break
        if st.ok and same_log(st.log, run_log) and st.state in ENDED:
            break
    else:
        print(f"upload step not finished after {ctx.job_polls} polls", file=sys.stderr)
        return False
    if ctx.dry:
        pull(ctx, recipe, ["upload"])
        return False  # a dry run also prints the fallback
    if st.state != "done" or "upload" not in st.steps(recipe):
        print(f"upload step ended {st.chain_tag} without MANIFEST.upload", file=sys.stderr)
        return False
    return pull(ctx, recipe, ["upload"])


def reusable_files(ctx: Ctx, recipe: str) -> dict[str, Path]:
    """{"<key>/<name>": local file} from the current `local` receipt whose file is still present
    with its receipt sha256 (an earlier verified pull: a later pull links instead of copying)."""
    r = read_json(ctx.root / C.retention_receipt_path(recipe)) or {}
    if r.get("method") != "local" or not r.get("location") or not isinstance(r.get("files"), dict):
        return {}
    loc = Path(r["location"])
    loc = loc if loc.is_absolute() else ctx.root / loc
    out = {}
    for name, sha in r["files"].items():
        f = loc / name
        if f.is_file() and sha256_file(f) == sha:
            out[name] = f
    return out


def tar_pull(ctx: Ctx, recipe: str, reuse: bool = False) -> list[str]:
    """Pull every checkpoint's adapter files with tar over ssh into a fresh gitignored dir
    results/E12/<recipe>/adapters/pull_<UTC>/<key>/, verify each against its ckpt.json, and
    write a `local` retention receipt. With `reuse`, files an earlier verified local pull holds
    with the same sha256 are hard-linked (copied if linking fails) instead of pulled again, so a
    repeated pull (the watchdog's early archival, then its hard stop) moves only new
    checkpoints. Returns problems (empty = receipt written)."""
    q = shlex.quote
    have = reusable_files(ctx, recipe) if reuse and not ctx.dry else {}
    stamp = utc_now().replace(":", "").replace("-", "")
    dest_rel = f"{C.rdir(recipe)}/adapters/pull_{stamp}"
    if not ctx.dry:  # a fresh directory, never reused
        (ctx.root / C.rdir(recipe) / "adapters").mkdir(parents=True, exist_ok=True)
        made = tempfile.mkdtemp(prefix=f"pull_{stamp}_", dir=ctx.root / C.rdir(recipe) / "adapters")
        dest_rel = f"{C.rdir(recipe)}/adapters/{Path(made).name}"
    dest = ctx.root / dest_rel
    recs = ckpt_records(recipe, ctx.root)
    groups: dict[str, list[str]] = {}
    probs = []
    for key, ck in recs:
        af = ckpt_adapter_files(ck)
        pdir = podp(ctx, (ck or {}).get("adapter_dir") or C.adapter_files_dir(recipe, key))
        pdir = pdir.rstrip("/")
        if not af:
            probs.append(f"ckpt {key}: ckpt.json lists no adapter files")
        elif posixpath.basename(pdir) != key:
            probs.append(f"ckpt {key}: adapter dir {pdir} is not named after its key")
        else:
            todo = []
            for n in sorted(af):
                old = have.get(f"{key}/{n}")
                if old is not None and sha256_file(old) == af[n]:
                    (dest / key).mkdir(parents=True, exist_ok=True)
                    try:
                        (dest / key / n).hardlink_to(old)
                    except OSError:
                        shutil.copy2(old, dest / key / n)
                else:
                    todo.append(f"{key}/{n}")
            if todo:
                groups.setdefault(posixpath.dirname(pdir), []).extend(todo)
    if probs:
        return probs
    for parent, names in groups.items():
        src = ssh_argv(ctx, remote("tar", f"cd {q(parent)} && tar cf - "
                                          + " ".join(q(n) for n in names)))
        res = ctx.runner.pipe(src, ["tar", "xf", "-", "-C", str(dest)], timeout=TAR_TIMEOUT_S)
        if res.rc and not ctx.dry:
            return [f"tar pull from {parent} failed (rc {res.rc}): {res.err.strip()[-300:]}"]
    if ctx.dry:
        return []
    files = {}
    for key, ck in recs:
        for name, sha in ckpt_adapter_files(ck).items():
            f = dest / key / name
            if not f.is_file():
                probs.append(f"{key}/{name} missing after the tar pull")
            elif sha256_file(f) != sha:
                probs.append(f"{key}/{name} sha256 differs from its ckpt.json")
            else:
                files[f"{key}/{name}"] = sha
    if probs:
        return probs
    receipt = {"recipe": recipe, "method": "local", "location": dest_rel, "commit_sha": None,
               "private": None, "files": files, "utc": utc_now()}
    rp = ctx.root / C.retention_receipt_path(recipe)
    rp.parent.mkdir(parents=True, exist_ok=True)
    if rp.exists():  # never discarded: an earlier (failed) receipt is kept beside it
        rp.rename(rp.with_name(f"retention_receipt.superseded_{stamp}.json"))
    tmp = rp.with_name(rp.name + ".tmp")
    tmp.write_text(dumps(receipt))
    tmp.replace(rp)
    return []


def archive_recipe(ctx: Ctx, R: str, upload: bool = True) -> tuple[int, dict]:
    """One recipe's final archive, after the chain is verified ended: the pod's recovery
    manifest is refreshed and pulled (every ckpt.json reaches the laptop), then unless
    `retained(R)` holds, the upload step (unless `upload` is False: the watchdog's bounded final
    archive) and, failing that, the verified tar pull of the delta (earlier verified local files
    are linked). Last, the pod's ckpt.json records are synced once more: a checkpoint that
    appeared after the snapshot is not in the receipt, so `retained` is false (code review r2
    finding 2)."""
    q = shlex.quote
    res = ssh(ctx, "recovery", f"cd {q(ctx.pod_root)} && {q(ctx.pod_py)} scripts/e12_pod.py "
                               f"recovery --recipe {q(R)}", timeout=600)
    if res.rc and not ctx.dry:
        return C.EXIT_FAILED, {"reason": f"recovery manifest refresh rc {res.rc}"}
    if not pull(ctx, R, ["recovery"]):
        return C.EXIT_FAILED, {"reason": "recovery pull/verify failed"}
    up_probs: list[str] = []
    if not ctx.dry and retained(R, ctx.root):
        action = "already retained"
    elif upload and run_upload(ctx, R) and retained(R, ctx.root):
        action = "upload"
    else:
        up_probs = [] if ctx.dry or not upload else retention_problems(R, ctx.root)
        probs = tar_pull(ctx, R, reuse=True)
        if ctx.dry:
            return C.EXIT_OK, {}
        if probs:
            return C.EXIT_FAILED, {"reason": "tar pull failed", "problems": probs[:20],
                                   "upload_problems": up_probs[:10]}
        action = "local tar pull"
    probs = sync_ckpt_records(ctx, R)
    probs += [p for p in retention_problems(R, ctx.root) if p not in probs]
    return (C.EXIT_FAILED if probs else C.EXIT_OK), {
        "method": _method(ctx, R), "action": action, "problems": probs[:20],
        "upload_problems": up_probs[:10]}


def cmd_archive(ctx: Ctx, recipes: list[str] | None = None, upload: bool = True) -> int:
    """`archive_recipe` for `recipes` (default: every recipe with an attempt, the latest first);
    refused unless the chain is verified ended. Prints {"retained": {recipe: bool}} (the
    watchdog's final retention), plus one recipe's fields at the top level."""
    cmd = "archive"
    st = poll_pod(ctx)
    if not ctx.dry:
        if not st.ok:
            return done(ctx, cmd, C.EXIT_FAILED, reason=st.error,
                        retained={r: False for r in recipes or []})
        if st.state not in (*RECOVERABLE, "none"):
            raise Refused(f"chain not verified ended (state {st.state}); run stop first")
    recipes = recipes or attempt_recipes(st)
    out = {R: archive_recipe(ctx, R, upload) for R in recipes}
    rc = C.EXIT_OK if all(r == C.EXIT_OK for r, _ in out.values()) else C.EXIT_FAILED
    if ctx.dry:
        return done(ctx, cmd, rc, recipes=recipes)
    ret = {R: r == C.EXIT_OK for R, (r, _) in out.items()}
    top = out[recipes[0]][1] if len(recipes) == 1 else {}
    return done(ctx, cmd, rc, upload=upload, retained=ret, state=st.state,
                recipes={R: f for R, (_, f) in out.items()}, **top)


def sync_ckpt_records(ctx: Ctx, recipe: str) -> list[str]:
    """ONE ssh tar call: every ckpt.json now on the pod (results/E12/<recipe>/ckpt/*/) into a
    fresh staging dir; each one the laptop lacks is installed into the results tree (a later
    manifest pull brings the identical file), one that differs from the laptop's is a problem
    and is never overwritten. Safe while the chain runs (ckpt.json is written last, once)."""
    q = shlex.quote
    rel = f"{C.rdir(recipe)}/ckpt"
    stage = None
    if not ctx.dry:
        (ctx.root / "logs").mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="e12_ckptjson_", dir=ctx.root / "logs"))
    body = (f"cd {q(ctx.pod_root)} || exit 97\n"
            f"set -- {q(rel)}/*/ckpt.json\n"
            '[ -e "$1" ] || exit 0\n'
            'tar cf - "$@"')
    res = ctx.runner.pipe(ssh_argv(ctx, remote("ckpt-json", body)),
                          ["tar", "xf", "-", "-C", str(stage or ".")], timeout=SSH_TIMEOUT_S)
    if ctx.dry:
        return []
    try:
        if res.rc:
            return [f"ckpt.json tar pull failed (rc {res.rc}): {res.err.strip()[-300:]}"]
        probs = []
        for f in sorted((stage / rel).glob("*/ckpt.json")):
            local = ctx.root / rel / f.parent.name / "ckpt.json"
            if not local.is_file():
                local.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, local)
            elif local.read_bytes() != f.read_bytes():
                probs.append(f"ckpt {f.parent.name}: the pod's ckpt.json differs from the "
                             "laptop's (kept the laptop's)")
        return probs
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def attempt_recipes(st: PodState) -> list[str]:
    """Recipes with an attempt record, the latest started first (the active one); else the
    default recipe."""
    att = st.attempts()
    out = sorted((r for r in att if r in C.RECIPES and isinstance(att[r], dict)),
                 key=lambda r: str(att[r].get("started_utc") or ""), reverse=True)
    return out or [C.DEFAULT_RECIPE]


def cmd_archive_while_running(ctx: Ctx, recipes: list[str] | None) -> int:
    """The watchdog's archival (review r1 finding 2), safe while the chain runs: no upload step
    (that needs the chain ended); for each recipe (default: every recipe with an attempt, the
    active one first) the pod's ckpt.json records are synced, and unless `retained(recipe)`
    already holds, the adapter files are pulled with the verified tar path (reusing an earlier
    pull's files) and a `local` receipt is written. Prints `retained` per recipe."""
    cmd = "archive"
    st = poll_pod(ctx)
    if not st.ok and not ctx.dry:
        return done(ctx, cmd, C.EXIT_FAILED, mode="while-running", reason=st.error,
                    retained={r: retained(r, ctx.root) for r in recipes or []})
    recipes = recipes or attempt_recipes(st)
    out: dict[str, dict] = {}
    for R in recipes:
        probs = sync_ckpt_records(ctx, R)
        action = "already retained"
        if ctx.dry or not retained(R, ctx.root):
            action = "local tar pull"
            probs += tar_pull(ctx, R, reuse=True)
        if ctx.dry:
            continue
        probs += [p for p in retention_problems(R, ctx.root) if p not in probs]
        out[R] = {"retained": not probs, "action": action, "method": _method(ctx, R),
                  "problems": probs[:20]}
    if ctx.dry:
        return done(ctx, cmd, C.EXIT_OK, mode="while-running", recipes=recipes)
    ok = all(v["retained"] for v in out.values())
    return done(ctx, cmd, C.EXIT_OK if ok else C.EXIT_FAILED, mode="while-running",
                state=st.state, retained={r: v["retained"] for r, v in out.items()},
                recipes=out)


def _method(ctx: Ctx, recipe: str) -> str | None:
    return (read_json(ctx.root / C.retention_receipt_path(recipe)) or {}).get("method")


def current_cuts(root: Path, recipe: str, cuts_text: str | None) -> list[int]:
    """The driver's rule (e12_pod.py `effective_cuts`): cuts.json (a dict or a list) if present;
    else the cuts of the latest admitted projection (lora-check, rank32, smoke); else none."""
    if cuts_text is not None:
        cj = json.loads(cuts_text) if cuts_text.strip() else {}
        cuts = cj.get("cuts", []) if isinstance(cj, dict) else cj
        return sorted(int(c) for c in cuts)
    for stage in ("lora-check", "rank32", "smoke"):
        proj = read_json(Path(root) / C.projection_path(recipe, stage))
        if proj and proj.get("verdict") == "admit":
            return sorted(int(c) for c in proj.get("cuts") or [])
    return []


def completion_problems(root: Path, recipe: str, st: PodState) -> tuple[list[str], dict]:
    """Rule 3's end check on the laptop tree, against the pod's current control files."""
    root = Path(root)
    gp = st.control(recipe, "genpath.json") or {}
    ep = st.control(recipe, "endpoint.json") or {}
    if gp.get("gen_path") not in C.GEN_PATHS:
        return ["no genpath.json with a generation path"], {}
    gen_path = gp["gen_path"]
    cuts = current_cuts(root, recipe, st.ctl.get(f"{CTL_REL}/{recipe}/cuts.json"))
    endpoint = ep.get("endpoint") if ep.get("endpoint") in C.ENDPOINTS else None
    rescue = ep.get("rescue_triggered") if isinstance(ep.get("rescue_triggered"), bool) else None
    probs: list[str] = []
    if gen_path == "hf":
        final = ep.get("stage") == "rescue214" or (
            ep.get("stage") == "repair107" and rescue is False and endpoint == 107)
        if not final:
            probs.append("HF path: the endpoint decision is not final (endpoint.json)")
    jobs = C.planned_jobs(recipe, gen_path, endpoint, cuts, rescue_triggered=rescue)
    if gen_path == "hf":
        adm = gp.get("jobs") if gp.get("admitted") is True else None
        if not isinstance(adm, list):
            probs.append("HF path: genpath.json is not admitted")
        else:
            adm_jobs = [C.Job(j["kind"], j["key"], int(j["n_rows"])) if isinstance(j, dict)
                        else C.Job(str(j[0]), str(j[1]), int(j[2])) for j in adm]
            if C.jobs_sha256(adm_jobs) != gp.get("jobs_sha256"):
                probs.append("HF path: the admitted job list does not match its jobs_sha256")
            outside = [f"{j.kind}/{j.key}" for j in jobs if j not in set(adm_jobs)]
            if outside:
                probs.append(f"HF path: {len(outside)} current jobs outside the admitted list, "
                             f"e.g. {outside[:3]}")
            probs += [f"HF path: {p}" for p in admission_problems(root, recipe, gp)]
    adm = read_json(root / C.admission_path(recipe))
    if adm is not None and sorted(int(c) for c in adm.get("cuts") or []) != cuts:
        probs.append(f"the cuts in force {cuts} differ from {C.admission_path(recipe)}'s "
                     f"{adm.get('cuts')}")
    n_out = n_skip = 0
    for j in jobs:
        p = job_problem(root, recipe, j)
        if p:
            probs.append(p)
        elif (root / C.job_status_path(recipe, j.kind, j.key)).is_file() and not _verified(
                root, recipe, j):
            n_skip += 1
        else:
            n_out += 1
    lists = [jobs] + ([C.planned_jobs(recipe, "vllm", None, cuts)] if gen_path == "vllm" else [])
    for step in C.EVAL_STEPS:
        if not any(step_of(j) == step for j in jobs):
            continue
        mp = manifest_problems(root, C.manifest_path(recipe, step))
        if mp:
            probs += mp[:5]
            continue
        ok = set()
        for L in lists:
            ok |= {C.jobs_sha256(L), C.jobs_sha256([j for j in L if step_of(j) == step])}
        rec = recorded_jobs_sha(root, recipe, step)
        if rec not in ok:
            probs.append(f"MANIFEST.{step}: bound to jobs_sha256 {rec}, not the current job list")
    for step in C.STEPS:
        if step not in C.EVAL_STEPS and (root / C.manifest_path(recipe, step)).is_file():
            probs += manifest_problems(root, C.manifest_path(recipe, step))[:5]
    probs += retention_problems(recipe, root)
    stats = {"gen_path": gen_path, "endpoint": endpoint, "rescue_triggered": rescue, "cuts": cuts,
             "n_jobs": len(jobs), "n_outputs": n_out, "n_skipped": n_skip,
             "jobs_sha256": C.jobs_sha256(jobs)}
    return probs, stats


def _verified(root: Path, recipe: str, job: C.Job) -> bool:
    try:
        return bool(G.verify_output(root, recipe, job.kind, job.key))
    except (OSError, ValueError):
        return False


def cmd_complete(ctx: Ctx, no_pull: bool) -> int:
    R, cmd = ctx.recipe, "complete"
    st = poll_pod(ctx)
    if not st.ok and not ctx.dry:
        return done(ctx, cmd, C.EXIT_FAILED, complete=False, reason=st.error)
    probs = []
    if st.state == "running":
        probs.append("the chain is still running")
    if not no_pull:
        steps = [s for s in st.steps(R) if s != "recovery"]
        if (steps or ctx.dry) and not pull(ctx, R, steps):
            probs.append("pull/verify of the step manifests failed")
    if ctx.dry:
        return done(ctx, cmd, C.EXIT_OK)
    more, stats = completion_problems(ctx.root, R, st)
    probs += more
    return done(ctx, cmd, C.EXIT_FAILED if probs else C.EXIT_OK, complete=not probs,
                n_problems=len(probs), problems=probs[:40], **stats)


# --- CLI ----------------------------------------------------------------------------------------

COMMANDS = ("poll", "stop", "induction", "admit-hf", "endpoint", "launch", "archive", "complete")


class Parser(argparse.ArgumentParser):
    def error(self, message: str):  # exit 3 (a gate), never 2 (2 means a budget refusal)
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(C.EXIT_REFUSED)


def parser() -> argparse.ArgumentParser:
    ap = Parser(prog="e12_control.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=COMMANDS)
    ap.add_argument("--slot", required=True)
    ap.add_argument("--recipe", choices=sorted(C.RECIPES))
    ap.add_argument("--stage", choices=("repair107", "rescue214"), help="endpoint only")
    ap.add_argument("--resume", action="store_true", help="launch: resume the recipe's attempt")
    ap.add_argument("--last-step", choices=C.STEPS, default=None,
                    help="launch: the chain pauses (E12_CHAIN_PAUSED) after this step")
    ap.add_argument("--no-pull", action="store_true", help="complete: check the laptop tree only")
    ap.add_argument("--while-running", action="store_true",
                    help="archive: the watchdog's mode, safe while the chain runs (no upload "
                         "step; ckpt.json sync and the verified tar pull); without --recipe, "
                         "every recipe with an attempt")
    ap.add_argument("--no-upload", action="store_true",
                    help="archive: no upload step (the watchdog's bounded final archive after a "
                         "verified stop: recovery refresh, then the verified tar pull of the "
                         "delta); without --recipe, every recipe with an attempt")
    ap.add_argument("--stop-polls", type=int, help="stop: polls before giving up (default 8)")
    ap.add_argument("--poll-interval-s", type=float,
                    help="stop/archive: seconds between polls (default 180)")
    ap.add_argument("--dry-run", action="store_true", help="print the commands; run nothing")
    return ap


def main(argv: list[str] | None = None, runner: Runner | None = None, **ctx_kw) -> int:
    a = parser().parse_args(argv)
    if a.slot != C.SLOT:
        print(f"E12 has one pod, slot {C.SLOT} (got {a.slot})", file=sys.stderr)
        return C.EXIT_REFUSED
    if a.cmd == "launch" and not a.recipe:
        print("launch needs --recipe", file=sys.stderr)
        return C.EXIT_REFUSED
    if a.cmd == "endpoint" and not a.stage:
        print("endpoint needs --stage repair107|rescue214", file=sys.stderr)
        return C.EXIT_REFUSED
    root = Path(ctx_kw.pop("root", ROOT))
    if a.stop_polls is not None:
        ctx_kw["stop_polls"] = max(1, a.stop_polls)
    if a.poll_interval_s is not None:
        ctx_kw["poll_interval_s"] = max(0.0, a.poll_interval_s)
    ctx = Ctx(root=root, slot=a.slot, recipe=a.recipe or C.DEFAULT_RECIPE,
              runner=runner or Runner(root, a.dry_run), dry=a.dry_run, **ctx_kw)
    try:
        if a.cmd == "poll":
            return cmd_poll(ctx)
        if a.cmd == "stop":
            return cmd_stop(ctx)
        if a.cmd == "induction":
            return cmd_induction(ctx)
        if a.cmd == "admit-hf":
            return cmd_admit_hf(ctx)
        if a.cmd == "endpoint":
            return cmd_endpoint(ctx, a.stage)
        if a.cmd == "launch":
            return cmd_launch(ctx, a.resume, a.last_step)
        if a.cmd == "archive" and a.while_running:
            return cmd_archive_while_running(ctx, [a.recipe] if a.recipe else None)
        if a.cmd == "archive":
            return cmd_archive(ctx, [a.recipe] if a.recipe else None, upload=not a.no_upload)
        return cmd_complete(ctx, a.no_pull)
    except Refused as e:
        return done(ctx, a.cmd, C.EXIT_REFUSED, reason=str(e))


if __name__ == "__main__":
    sys.exit(main())
