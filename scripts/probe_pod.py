"""Generic RunPod runner for probe-tier experiments (CLAUDE.md "Two tiers", 2026-10-11).

A probe (id `P<NN>`, directory `probes/P<NN>-<slug>/` with a `probe.toml`) runs on a pod with no
per-experiment case in pod_push.sh, pod_pull_results.sh, pod_setup.sh or pod_up.py. The caps come
from probe.toml through `emx.spend.caps_for`, so pod_up.py, pod_down.py, pod_budget_check.py,
`python -m emx.runpod --killswitch-deadline` and scripts/probe_watchdog.sh already accept the id.

Usage (every subcommand takes --exp P<NN> (required), --slot (default _<ID>, e.g. _P01), --dry-run):
    uv run python scripts/probe_pod.py up     --exp P01   # watchdog, reservation, pod_up.py
    uv run python scripts/probe_pod.py push   --exp P01   # arm killswitch, rsync, pod .env, setup
    uv run python scripts/probe_pod.py run    --exp P01   # launch probes/<dir>/run.sh detached
    uv run python scripts/probe_pod.py status --exp P01   # one ssh call + spend + deadline
    uv run python scripts/probe_pod.py pull   --exp P01 [--logs-only]
    uv run python scripts/probe_pod.py down   --exp P01   # pod_down.py (charge + settle)
    nohup uv run python scripts/probe_pod.py go --exp P01 > logs/probe_P01_go.log 2>&1 &

probe.toml `[pod]` table (all optional except gpu_hours for up/go):
    gpu_hours = 6        # pod budget B = min(gpu_hours x worst fallback rate, gpu_limit_usd,
                         #   the kill line's GPU headroom); B is reserved and passed to pod_up
    setup = "eq0"        # pod_setup.sh --only target; "" = none
    disk_gb = 200
    push = []            # extra repo-relative files/dirs to send (may be untracked/gitignored)
    env = ["HF_TOKEN"]   # laptop .env keys copied into the pod's /ws/emx/.env
    poll_minutes = 5

Probe run contract:
- On the pod the repo lives at /ws/emx. `run` launches, from /ws/emx, `bash probes/<dir>/run.sh`
  detached (`mkdir -p probes/<dir>/logs probes/<dir>/results`, then `setsid nohup ... >
  probes/<dir>/logs/run_<UTC>.log 2>&1 &`), with /ws/emx/.env exported and EMX_PROBE=<id>. It
  writes probes/<dir>/logs/run.launched (the log path) before the launch and the run writes its
  pid to probes/<dir>/logs/run.pid. ONE run per pod: if either file exists (the run alive,
  finished or crashed), `run` adopts that run and never relaunches over its results.
- run.sh prints a first log line at once (the launch is verified by the log growing within 20 s),
  writes outputs under probes/<dir>/results/ and logs (training records too) under
  probes/<dir>/logs/. When done it writes probes/<dir>/results/MANIFEST.sha256 (`shasum -a 256`
  lines, paths relative to results/, covering EVERY result file; written elsewhere, then moved in)
  and its LAST log line is `PROBE_DONE rc=<int>` (anchored).
- Logs: the pod's probes/<dir>/logs/ is mirrored incrementally into
  probes/<dir>/logs/pod/<pod id>/ (committable; one directory per pod, so a follow-up run never
  overwrites an earlier one's logs) on every poll, and with 3 tries and backoff before any
  termination.
- Results: `pull` stages results/ in probes/<dir>/.pull_<random>/ and promotes into
  probes/<dir>/results/ when the manifest verifies (`shasum -a 256 -c`) and lists every pulled
  file, even if the logs pull failed; the pull counts as COMPLETE (the only state that permits a
  normal teardown) only when the logs pull succeeded too. A local file that would change goes to
  probes/<dir>/superseded/<UTC>/ first. Nothing pulled is ever deleted: without a manifest, or
  with one that misses files, the pulled files go to probes/<dir>/unverified/<UTC>/; a failed
  verification or copy keeps the staging dir.
- The pod's .env holds only the `[pod].env` keys plus EMX_GIT_COMMIT and EMX_PROBE. Laptop-only
  keys (OPENAI*, OPENROUTER*, RUNPOD*, EM_DATASET_PASSWORD, AZURE*) are refused. Every transfer
  path is checked with symlinks resolved (inside the repo, never data/E1/questions/narrow.jsonl,
  no control characters) and sent NUL-delimited (`rsync --from0`).

`push` mirrors pod_push.sh's E13 order (fail closed): compute the deadline (`python -m emx.runpod
--check-slot S --killswitch-deadline --per-slot`, which is also the slot-ownership check; the
slot's own reservation deadline when lower), send scripts/pod_killswitch.sh alone (one ssh call,
120 s), arm it and require its last line `<UTC> [killswitch] ARMED pod=<pod id> deadline=<that
deadline> (...) key_check=ok|read403`, record the acknowledgement for a reservation-launched pod
(`python -m emx.runpod_reservations ack`), and only then the bulk transfer and setup. Any failure
up to the acknowledgement terminates the pod (pod_down.py) and exits 4.
Known window (review r1 finding 1, deferred): between pod creation (`up`) and the arm in `push`,
the pod is covered only by the laptop watchdog, as every other experiment's pods are. `go` runs
push straight after up; after a standalone `up`, run `push` at once.

`go` is the unattended chain: up -> push -> run -> every poll_minutes {mirror logs; one status
ssh call; print `PROBE_STATUS ...`} until:
- PROBE_DONE: final pull (up to 3 tries); COMPLETE -> pod_down.py, exit 0 (2 if rc != 0).
- Otherwise (not complete, the run died without PROBE_DONE, or the killswitch deadline minus
  15 min with the run alive): the persisted `recovering` phase, which `go` resumes after a
  restart. Each round: quiesce the run (its process group, TERM, up to 30 s, KILL, re-check; only
  a remote `PROBE_QUIESCED pgid=<n>` ack with ssh exit 0 counts, 3 tries), then SALVAGE, i.e.
  mirror the logs and copy ALL results into unverified/<UTC>/. Rounds repeat with backoff (to
  5 min) while time remains before the killswitch deadline. Once a copy made after a confirmed
  stop succeeded (logs too) -> pod_down.py, ALERT, exit 5. A copy without a confirmed stop is
  kept but never authorizes teardown. Out of time: the pod is left to the killswitch; once the
  API reports it stopped it is settled (exit 1).
- ssh unreachable: the RunPod API decides. Confirmed stopped/exited/gone (or terminated in the
  event log) -> settled through pod_down.py's reservation-backed `--pod-id` path (idempotent,
  tolerates a 404), ALERT, exit 1; still RUNNING -> keep polling (transient), up to 30 min past
  the deadline. A restart whose recorded pod left its slot is routed to the same settlement.
Every termination after a launch needs a successful logs pull. ALERTs are appended to
probes/<dir>/logs/ALERT. State lives in probes/<dir>/logs/runner_state.json; rerunning `go`
after a laptop crash resumes from it and logs/pods_<ID>.jsonl instead of relaunching; `go
--fresh` archives a finished state and starts a new pod. up/push/run/down/go hold
probes/<dir>/logs/runner.lock (non-blocking flock): a second runner for the probe exits 1.

Exit codes: 0 ok; 1 failure; 2 run finished with PROBE_DONE rc != 0 (complete, pod down);
3 `pull`: no manifest yet (logs mirrored, results kept unverified); 4 killswitch not armed (pod
terminated); 5 `go`: salvaged (unverified results and logs copied, pod down); 6 pod_up outcome
uncertain (reservation kept; see pod_up.py).
"""

from __future__ import annotations

import argparse
import filecmp
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from emx import runpod as rp
from emx import spend

# injectable for tests (read at Runner construction)
SH = subprocess.run
POPEN = subprocess.Popen
SLEEP = time.sleep
CLOCK = time.time

ID_RE = re.compile(r"P\d{2,3}")
DIR_RE = re.compile(r"P\d{2,3}-[A-Za-z0-9._-]+")
POD_ROOT = "/ws/emx"
PY = ["uv", "run", "--quiet", "python"]
SSH_OPTS = [
    "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
    "-o", "ConnectTimeout=20", "-o", "StrictHostKeyChecking=accept-new",
]
NEVER_SEND = frozenset({"data/E1/questions/narrow.jsonl"})  # held-out questions stay local
LAPTOP_ONLY = re.compile(r"(OPENAI|OPENROUTER|RUNPOD|EM_DATASET_PASSWORD|AZURE)")
SECRET_KEYS = ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "RUNPOD_API_KEY", "EM_DATASET_PASSWORD")
RUNNER_KEYS = ("EMX_GIT_COMMIT", "EMX_PROBE")
SAFE_VALUE = re.compile(r"[A-Za-z0-9._/:-]+")  # shell-inert: pod_setup.sh sources the .env
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
SETUP_RE = re.compile(r"[a-z0-9]*")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DONE_RE = re.compile(r"^PROBE_DONE rc=(-?\d+)$")
QUIESCED_RE = re.compile(r"^PROBE_QUIESCED pgid=\S+$", re.MULTILINE)
ARMED_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[killswitch\] ARMED pod=([A-Za-z0-9]+) "
    r"deadline=(\d+) \(.*\) key_check=(\S+)$"
)
MANIFEST_LINE = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")
CREATE_RE = re.compile(r"^EMX_POD_CREATE=(\w+) ", re.MULTILINE)
RUN_OUTPUT = ("logs", "results", "superseded", "unverified")  # never sent from the laptop
LAPTOP_LOGS = ("runner_state.json", "ALERT")  # laptop-only files in probes/<dir>/logs/
DEADLINE_MARGIN_S = 15 * 60
LAUNCH_CHECK_S = 20
SETUP_POLL_S = 30
WATCHDOG_WAIT_S = 60
SSH_TIMEOUT_S = 120
RSYNC_TIMEOUT_S = 3600
LOG_BACKOFF_S = 10  # logs pull retries: 10 s, 20 s
STOP_GRACE_S = 30  # salvage: TERM, wait up to this long, then KILL
RECOVER_MAX_WAIT_S = 300  # recovery retries back off up to this
LOST_GRACE_S = 30 * 60  # unreachable pod: keep asking the API this long past the deadline


class ProbeError(RuntimeError):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def utc_stamp(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, UTC).strftime("%Y%m%dT%H%M%SZ")


def utc_iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PodConfig:
    gpu_hours: float | None
    setup: str
    disk_gb: int
    push: tuple[str, ...]
    env: tuple[str, ...]
    poll_minutes: float


def load_pod_config(path: Path) -> PodConfig:
    with path.open("rb") as f:
        pod = tomllib.load(f).get("pod", {})
    unknown = set(pod) - {"gpu_hours", "setup", "disk_gb", "push", "env", "poll_minutes"}
    if unknown:
        raise ProbeError(f"{path}: unknown [pod] keys {sorted(unknown)}")
    gh = pod.get("gpu_hours")
    if gh is not None and not (isinstance(gh, int | float) and 0 < gh <= 48):
        raise ProbeError(f"{path}: [pod].gpu_hours must be in (0, 48], got {gh!r}")
    setup = pod.get("setup", "")
    if not isinstance(setup, str) or not SETUP_RE.fullmatch(setup):
        raise ProbeError(f"{path}: [pod].setup must be a pod_setup.sh --only target, got {setup!r}")
    disk = pod.get("disk_gb", 200)
    if not isinstance(disk, int) or not 20 <= disk <= 2000:
        raise ProbeError(f"{path}: [pod].disk_gb must be an int in [20, 2000], got {disk!r}")
    push = pod.get("push", [])
    env = pod.get("env", [])
    if not all(isinstance(p, str) and p for p in push):
        raise ProbeError(f"{path}: [pod].push must be a list of paths")
    for k in env:
        if not isinstance(k, str) or not ENV_KEY.fullmatch(k):
            raise ProbeError(f"{path}: [pod].env key {k!r} is not an env var name")
        if LAPTOP_ONLY.match(k):
            raise ProbeError(f"{path}: [pod].env key {k} is laptop-only; it never goes to a pod")
        if k in RUNNER_KEYS:
            raise ProbeError(f"{path}: [pod].env key {k} is set by the runner")
    poll = pod.get("poll_minutes", 5)
    if not isinstance(poll, int | float) or not 1 <= poll <= 120:
        raise ProbeError(f"{path}: [pod].poll_minutes must be in [1, 120], got {poll!r}")
    return PodConfig(
        None if gh is None else float(gh), setup, disk, tuple(push), tuple(env), float(poll)
    )


def parse_manifest(path: Path) -> list[str]:
    """Paths listed by a results/MANIFEST.sha256 (relative to results/; no '..', not absolute)."""
    out: list[str] = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        m = MANIFEST_LINE.match(line)
        if not m:
            raise ProbeError(f"{path.name}:{i}: not a sha256 manifest line")
        p = m.group(2)
        while p.startswith("./"):  # `cd results && find . -type f | xargs shasum -a 256`
            p = p[2:]
        parts = p.split("/")
        if p.startswith("/") or ".." in parts or "." in parts or not p:
            raise ProbeError(f"{path.name}:{i}: refused path {p!r} (relative to results/ only)")
        if p == "MANIFEST.sha256":
            raise ProbeError(f"{path.name}:{i}: the manifest lists itself (write it elsewhere "
                             "first, then move it into results/)")
        if p not in out:
            out.append(p)
    if not out:
        raise ProbeError(f"{path.name}: lists no file")
    return out


@dataclass
class PullResult:
    logs_ok: bool
    # complete | incomplete | no_manifest | failed | copy_failed | salvaged | skipped
    results: str
    where: str

    @property
    def complete(self) -> bool:
        return self.logs_ok and self.results == "complete"


@dataclass
class Poll:
    reachable: bool
    alive: bool = False
    done_rc: int | None = None
    manifest: bool = False
    n_results: int = -1
    gpu: str = ""
    ks: str = ""
    last: str = ""
    log: str = ""
    tail: tuple[str, ...] = ()


class Runner:
    def __init__(
        self,
        exp: str,
        slot: str | None = None,
        *,
        dry_run: bool = False,
        repo: Path | None = None,
        watchdog_interval: int = 300,
        reserve_wait_s: float = 3600,
        setup_max_s: float = 2400,
    ):
        if not ID_RE.fullmatch(exp or ""):
            raise ProbeError(f"--exp must look like P01 (got {exp!r})", 2)
        self.exp = exp
        self.slot = slot or f"_{exp}"
        if not rp.SLOT_RE.fullmatch(self.slot):
            raise ProbeError(f"--slot must look like _P01 / _A (got {self.slot!r})", 2)
        self.dry_run = dry_run
        self.repo = repo or REPO
        self.toml = spend.probe_caps_path(exp, self.repo / "probes")
        self.caps = spend.probe_caps(exp, self.repo / "probes")
        self.dir = self.toml.parent
        if not DIR_RE.fullmatch(self.dir.name):
            raise ProbeError(f"probe directory name {self.dir.name!r} is not shell-safe")
        self.rel = f"probes/{self.dir.name}"
        self.cfg = load_pod_config(self.toml)
        self.env_path = self.repo / ".env"
        self.state_path = self.dir / "logs" / "runner_state.json"
        self.watchdog_interval = watchdog_interval
        self.reserve_wait_s = reserve_wait_s
        self.setup_max_s = setup_max_s
        self.sh, self.popen, self.sleep, self.clock = SH, POPEN, SLEEP, CLOCK
        rp.configure(exp)

    def acquire_lock(self) -> None:
        """Non-blocking flock on probes/<dir>/logs/runner.lock, held for the process's life, so
        two runners never drive one probe (review r1 finding 5)."""
        if self.dry_run:
            return
        import fcntl

        path = self.dir / "logs" / "runner.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        f = path.open("a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise ProbeError(f"another probe_pod.py process holds {path.relative_to(self.repo)} "
                             f"for {self.exp}; refusing to drive the same probe twice") from None
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()}\n")
        f.flush()
        self._lock = f

    # --- plumbing ------------------------------------------------------------------------------

    def say(self, msg: str) -> None:
        print(msg, flush=True)

    def call(
        self,
        argv: list[str],
        *,
        effect: bool = True,
        input: str | None = None,
        timeout: float | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a local command. `effect=True` commands are only printed under --dry-run."""
        if self.dry_run and effect:
            self.say("DRY RUN: would run: " + shlex.join(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        try:
            return self.sh(
                argv,
                cwd=str(cwd or self.repo),
                input=input if input is not None else "",
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return subprocess.CompletedProcess(argv, 124, out, f"timed out after {timeout} s")

    def echo(self, r: subprocess.CompletedProcess) -> None:
        for stream in (r.stdout, r.stderr):
            if stream and stream.strip():
                self.say(stream.rstrip("\n"))

    def envval(self, key: str) -> str:
        from dotenv import dotenv_values

        return (dotenv_values(self.env_path).get(key) or "") if self.env_path.exists() else ""

    def pod_addr(self) -> tuple[str, str, str, str]:
        """(pod id, host, port, user) of the slot, from the laptop .env (pod_up.py writes them)."""
        pid = self.envval(f"RUNPOD_POD_ID{self.slot}")
        host = self.envval(f"POD_SSH_HOST{self.slot}")
        port = self.envval(f"POD_SSH_PORT{self.slot}")
        user = self.envval("POD_SSH_USER") or "root"
        if self.dry_run:
            return pid or "PODID", host or "HOST", port or "PORT", user
        if not re.fullmatch(r"[A-Za-z0-9]+", pid) or not host or not port.isdigit():
            raise ProbeError(f"no pod in slot {self.slot} (.env RUNPOD_POD_ID/POD_SSH_HOST/PORT)")
        return pid, host, port, user

    def ssh(self, remote: str, *, timeout: float = SSH_TIMEOUT_S, input: str | None = None,
            effect: bool = True, addr: tuple[str, str, str, str] | None = None):
        _, host, port, user = addr or self.pod_addr()
        return self.call(["ssh", "-p", port, *SSH_OPTS, f"{user}@{host}", remote],
                         effect=effect, input=input, timeout=timeout)

    def rsync_e(self, port: str) -> str:
        return shlex.join(["ssh", "-p", port, *SSH_OPTS])

    def git(self, *args: str) -> str:
        r = self.call(["git", *args], effect=False)
        if r.returncode != 0:
            raise ProbeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return r.stdout

    # --- state ---------------------------------------------------------------------------------

    def load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def save_state(self, st: dict) -> None:
        if self.dry_run:
            return
        st = {**st, "exp": self.exp, "slot": self.slot, "updated_utc": utc_iso(self.clock())}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.state_path)

    def alert(self, reason: str) -> None:
        line = f"{utc_iso(self.clock())} {self.exp} {self.slot}: {reason}"
        self.say(f"PROBE_ALERT {line}")
        if self.dry_run:
            return
        p = self.dir / "logs" / "ALERT"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(line + "\n")

    # --- bookkeeping reads (emx.runpod, emx.runpod_reservations, emx.spend) --------------------

    def sessions(self) -> list[dict]:
        return rp.pod_sessions(rp.read_pod_events())

    def pod_running(self, pod_id: str) -> bool:
        return any(s["pod_id"] == pod_id and s["running"] for s in self.sessions())

    def reservation_for(self, pod_id: str) -> str | None:
        from emx import runpod_reservations as rr

        p = rr.pod(pod_id)
        return None if p is None else p.get("reservation_id")

    def budget(self) -> float:
        if self.cfg.gpu_hours is None:
            raise ProbeError(f"{self.toml}: [pod].gpu_hours is required for up/go")
        hours_usd = self.cfg.gpu_hours * rp.FALLBACK_RATE
        headroom = (rp.current_kill_limit() - rp.ledger_spent("gpu")
                    - rp.running_projection(self.sessions()))
        b = math.floor(min(hours_usd, self.caps.gpu_limit_usd, headroom) * 100) / 100
        self.say(f"budget ({self.exp}): min({self.cfg.gpu_hours} h x ${rp.FALLBACK_RATE}/h = "
                 f"${hours_usd:.2f}, GPU limit ${self.caps.gpu_limit_usd:.2f}, kill-line headroom "
                 f"${headroom:.2f}) = ${b:.2f}")
        if b < 1.0:
            raise ProbeError(f"no GPU budget left for {self.exp} (${b:.2f})")
        return b

    def ensure_watchdog(self, wait: bool = True) -> None:
        live, why = rp.watchdog_live(self.exp)
        self.say(f"watchdog ({self.exp}): {'live' if live else 'NOT LIVE'} ({why})")
        if live:
            return
        cmd = ["bash", "scripts/probe_watchdog.sh", self.exp, str(self.watchdog_interval)]
        log = self.repo / "logs" / f"watchdog_{self.exp}.log"
        if self.dry_run:
            self.say(f"DRY RUN: would start: nohup {shlex.join(cmd)} >> {log} 2>&1 &")
            return
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            self.popen(cmd, cwd=str(self.repo), stdout=f, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL, start_new_session=True)
        if not wait:
            return
        t_end = self.clock() + WATCHDOG_WAIT_S
        while self.clock() < t_end:
            self.sleep(2)
            live, why = rp.watchdog_live(self.exp)
            if live:
                self.say(f"watchdog ({self.exp}): started ({why})")
                return
        raise ProbeError(f"watchdog heartbeat not live {WATCHDOG_WAIT_S} s after starting it "
                         f"(see {log})")

    # --- up ------------------------------------------------------------------------------------

    def up(self) -> int:
        if self.envval(f"RUNPOD_POD_ID{self.slot}") and not self.dry_run:
            raise ProbeError(f"slot {self.slot} already holds pod "
                             f"{self.envval(f'RUNPOD_POD_ID{self.slot}')}; down it first")
        self.ensure_watchdog()
        b = self.budget()
        base = PY + ["scripts/pod_up.py", "--exp", self.exp, "--slot", self.slot,
                     "--disk-gb", str(self.cfg.disk_gb)]
        if self.dry_run:
            self.say(f"DRY RUN: would reserve ${b:.2f} (emx.runpod_reservations.wait_and_reserve)")
            r = self.call(base + ["--dry-run"], effect=False)
            self.echo(r)
            return r.returncode
        from emx import runpod_reservations as rr

        st = self.load_state()
        rid = st.get("reservation_id")
        R = rr.get(rid) if rid else None
        if R is None or R["closed"] or R["launched"] or R["open_usd"] + 1e-6 < b:
            if R is not None and not R["closed"] and not R["launched"]:
                rr.release(rid, note=f"{self.exp} probe_pod: replaced (budget ${b:.2f})")
            try:
                rid = rr.wait_and_reserve(
                    self.exp, b, note=f"{self.exp}:{self.slot}:probe_pod up",
                    give_up_at=self.clock() + self.reserve_wait_s, sleep=self.sleep,
                    clock=self.clock, log=self.say,
                )
            except rr.NotAdmitted as e:
                raise ProbeError(f"RunPod reservation not admitted: {e}") from e
            st = {"reservation_id": rid, "budget_usd": b, "phase": "reserved"}
            self.save_state(st)
        self.say(f"reservation {rid}: ${b:.2f}")
        r = self.call(base + ["--budget-usd", f"{b:.2f}", "--reservation", rid])
        self.echo(r)
        m = CREATE_RE.findall(r.stdout or "")
        status = m[-1] if m else None
        if r.returncode == 0 and status == "ok":
            pod_id = self.envval(f"RUNPOD_POD_ID{self.slot}")
            self.save_state({**st, "pod_id": pod_id, "phase": "up", "up_utc": utc_iso(self.clock())})
            self.say(f"PROBE_UP pod={pod_id} slot={self.slot} reservation={rid}")
            return 0
        if status == "none":  # refused before any create request: the money is free again
            rr.release(rid, note=f"{self.exp} probe_pod: pod_up refused (rc {r.returncode})")
            self.save_state({**st, "reservation_id": None, "phase": "idle"})
            raise ProbeError(f"pod_up refused the launch (rc {r.returncode}); reservation released")
        if status == "closed":
            self.save_state({**st, "reservation_id": None, "phase": "idle"})
            raise ProbeError(f"pod_up created no pod (rc {r.returncode}); reservation closed")
        raise ProbeError(
            f"pod_up outcome uncertain (rc {r.returncode}, status {status}); reservation {rid} "
            "KEPT: check the RunPod console, then pod_down.py --untracked or release it", 6)

    # --- push ----------------------------------------------------------------------------------

    def _is_run_output(self, f: str) -> bool:
        return any(f.startswith(f"{self.rel}/{d}/") for d in RUN_OUTPUT) or f.startswith(
            f"{self.rel}/.pull_")

    def _expand_push(self, p: str) -> list[str]:
        parts = Path(p).parts
        if Path(p).is_absolute() or ".." in parts or not parts:
            raise ProbeError(f"[pod].push path {p!r} must be repo-relative without '..'")
        full = self.repo / p
        self.check_path(Path(p).as_posix())
        if full.is_dir():  # also a symlinked alias dir: its files are checked one by one below
            pass
        elif full.is_file() or full.is_symlink():
            return [Path(p).as_posix()]
        else:
            raise ProbeError(f"[pod].push path {p!r} does not exist")
        out = []
        for root, dirs, files in os.walk(full):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name != ".DS_Store":
                    out.append((Path(root) / name).relative_to(self.repo).as_posix())
        return out

    def check_path(self, f: str) -> None:
        """Refuse a transfer path (review r1 finding 6) with a control character, one that
        resolves (symlinks followed) outside the repo, a component that is a symlink pointing
        outside it, or one that equals, lies under or contains a held-out file (NEVER_SEND)."""
        if any(ord(c) < 32 or ord(c) == 127 for c in f):
            raise ProbeError(f"transfer path {f!r} has a control character; refusing")
        if Path(f).is_absolute() or ".." in Path(f).parts:
            raise ProbeError(f"transfer path {f!r} must be repo-relative without '..'")
        root = self.repo.resolve()
        cur = self.repo
        for part in Path(f).parts:
            cur = cur / part
            if cur.is_symlink() and not cur.resolve().is_relative_to(root):
                raise ProbeError(f"{f}: {cur.relative_to(self.repo)} is a symlink out of the "
                                 "repo; refusing")
        r = (self.repo / f).resolve()
        if not r.is_relative_to(root):
            raise ProbeError(f"{f} resolves outside the repo ({r}); refusing")
        for bad in NEVER_SEND:
            b = (self.repo / bad).resolve()
            if r == b or r.is_relative_to(b) or b.is_relative_to(r):
                raise ProbeError(f"{f} resolves to or holds {bad} (held out); refusing")

    def push_paths(self) -> list[str]:
        out: list[str] = []
        for p in self.cfg.push:
            out += self._expand_push(p)
        return out

    def push_list(self) -> list[str]:
        """Tracked files under src scripts probes/<dir> pyproject.toml uv.lock (minus the probe's
        run outputs), plus every [pod].push path; refuses held-out data and any .env file."""
        tracked = self.git("ls-files", "-z", "--", "src", "scripts", self.rel,
                           "pyproject.toml", "uv.lock")
        files = [f for f in tracked.split("\0") if f and not self._is_run_output(f)]
        files += self.push_paths()
        seen: set[str] = set()
        out = []
        for f in files:
            if f in seen:
                continue
            seen.add(f)
            self.check_path(f)
            if Path(f).name == ".env":
                raise ProbeError(f"{f}: an .env file is never sent; refusing")
            out.append(f)
        if f"{self.rel}/run.sh" not in seen:
            raise ProbeError(f"{self.rel}/run.sh is not tracked (commit it) nor in [pod].push")
        return out

    def check_clean(self) -> str:
        """HEAD, refusing modified tracked files or untracked (non-ignored) files under src/,
        scripts/ or probes/<dir>/ (other than its run outputs and [pod].push paths)."""
        paths = ["src", "scripts", self.rel]
        mod = self.git("status", "--porcelain", "--untracked-files=no", "--", *paths).strip()
        push = set(self.push_paths())
        untracked = [
            f for f in self.git("ls-files", "--others", "--exclude-standard", "--", *paths)
            .splitlines()
            if f and not self._is_run_output(f) and f not in push
        ]
        if mod or untracked:
            listing = "\n".join([mod] * bool(mod) + [f"?? {f}" for f in untracked])
            raise ProbeError("uncommitted changes under src/, scripts/ or "
                             f"{self.rel}/ (the pod runs EMX_GIT_COMMIT); commit them:\n{listing}")
        head = self.git("rev-parse", "HEAD").strip()
        if not SHA_RE.fullmatch(head):
            raise ProbeError(f"no commit at HEAD ({head!r})")
        return head

    def pod_env(self, head: str) -> str:
        lines = []
        for k in self.cfg.env:
            v = self.envval(k)
            if not v:
                raise ProbeError(f"{k} is empty in .env; refusing")
            if not SAFE_VALUE.fullmatch(v):
                raise ProbeError(f"unsafe characters in the value of {k}; refusing")
            lines.append(f"{k}={v}")
        lines += [f"EMX_GIT_COMMIT={head}", f"EMX_PROBE={self.exp}"]
        text = "\n".join(lines) + "\n"
        for k in SECRET_KEYS:
            v = self.envval(k)
            if v and v in text:
                raise ProbeError(f"the value of {k} appears in the pod env; refusing")
        return text

    def deadlines(self) -> dict[str, int]:
        """{slot: epoch} for this slot (first) and every other running slot of the probe: the
        slot's per-slot deadline (its reservation's, never above the global kill line's), else
        the global one. Also the offline slot-ownership check (non-zero exit = not ours)."""
        r = self.call(PY + ["-m", "emx.runpod", "--exp", self.exp, "--check-slot", self.slot,
                            "--killswitch-deadline", "--per-slot"], effect=False, timeout=180)
        if r.returncode != 0:
            raise ProbeError(f"deadline / slot check failed: {(r.stderr or r.stdout).strip()}")
        g = None
        per: dict[str, int] = {}
        slots: list[str] = []
        for line in r.stdout.splitlines():
            if m := re.fullmatch(r"EMX_KILLSWITCH_DEADLINE=(\d+)", line):
                g = int(m.group(1))
            elif m := re.fullmatch(r"EMX_KILLSWITCH_DEADLINE(_[A-Za-z0-9]{1,8})=(\d+)", line):
                per[m.group(1)] = int(m.group(2))
            elif line.startswith("EMX_KILLSWITCH_SLOTS="):
                slots = line.split("=", 1)[1].split()
        if g is None:
            raise ProbeError("no EMX_KILLSWITCH_DEADLINE from emx.runpod")
        out = {}
        for s in [self.slot, *slots, *per]:
            if s not in out and rp.SLOT_RE.fullmatch(s):
                out[s] = min(g, per.get(s, g))
        return out

    def arm(self, addr: tuple[str, str, str, str], deadline: int) -> tuple[bool, str]:
        pod_id = addr[0]
        r = self.ssh(f"bash {POD_ROOT}/scripts/pod_killswitch.sh arm {pod_id} {deadline}",
                     timeout=90, addr=addr)
        lines = [x for x in (r.stdout or "").splitlines() if x.strip()]
        last = lines[-1] if lines else ""
        if r.returncode != 0:
            return False, f"arm failed or uncertain (ssh exit {r.returncode}): {last}"
        m = ARMED_RE.match(last)
        if not m:
            return False, f"no ARMED line: {last!r}"
        if m.group(1) != pod_id:
            return False, f"ARMED line names pod {m.group(1)}, not {pod_id}"
        if int(m.group(2)) != deadline:
            return False, f"ARMED deadline {m.group(2)} != requested {deadline}"
        if m.group(3) not in ("ok", "read403"):
            return False, f"pod key unchecked (key_check={m.group(3)})"
        return True, last

    def ack(self, pod_id: str, deadline: int) -> tuple[bool, str]:
        if self.reservation_for(pod_id) is None:
            return True, "no reservation launched this pod; nothing to acknowledge"
        r = self.call(PY + ["-m", "emx.runpod_reservations", "ack", "--pod", pod_id,
                            "--deadline", str(deadline)], timeout=120)
        return r.returncode == 0, (r.stderr or r.stdout).strip()

    def abort_unarmed(self, why: str) -> None:
        self.say(f"{self.exp} killswitch: {why}; the pod is UNARMED: terminating it")
        try:
            self.down(reason=f"probe_pod push: killswitch not armed ({why})"[:200])
        except ProbeError as e:
            self.alert(f"killswitch not armed AND pod_down failed ({e}); terminate by hand")
        raise ProbeError(f"killswitch not armed: {why}", 4)

    def push(self) -> int:
        st = self.load_state()
        addr = self.pod_addr()
        pod_id, host, port, user = addr
        files = self.push_list()
        head = self.check_clean()
        env_text = self.pod_env(head)
        env_keys = [x.split("=", 1)[0] for x in env_text.splitlines()]
        self.say(f"push ({self.exp}): {len(files)} files, pod env keys {' '.join(env_keys)}, "
                 f"commit {head[:12]}")
        if self.dry_run:
            self.say("DRY RUN: order: 1. deadline (python -m emx.runpod --check-slot "
                     f"{self.slot} --killswitch-deadline --per-slot); 2. send "
                     "scripts/pod_killswitch.sh alone; 3. arm and verify the ARMED line; 4. ack "
                     "the reservation; 5. rsync --files-from; 6. pod .env; "
                     f"7. setup ({self.cfg.setup or 'none'})")
            for f in files:
                self.say(f"  send {f}")
            return 0
        # 1-4: fail closed before anything else is sent (pod_push.sh's E13 order)
        try:
            dls = self.deadlines()
        except ProbeError as e:
            self.abort_unarmed(str(e))
        deadline = dls[self.slot]
        if deadline <= self.clock() + 60:
            self.abort_unarmed(f"deadline {deadline} is within 60 s: no budget left")
        ks = (self.repo / "scripts" / "pod_killswitch.sh").read_text()
        r = self.ssh(f"mkdir -p {POD_ROOT}/scripts {POD_ROOT}/logs && cat > "
                     f"{POD_ROOT}/scripts/pod_killswitch.sh.part && chmod 755 "
                     f"{POD_ROOT}/scripts/pod_killswitch.sh.part && mv -f "
                     f"{POD_ROOT}/scripts/pod_killswitch.sh.part {POD_ROOT}/scripts/pod_killswitch.sh",
                     input=ks, timeout=120, addr=addr)
        if r.returncode != 0:
            self.abort_unarmed(f"could not send scripts/pod_killswitch.sh (rc {r.returncode})")
        ok, line = self.arm(addr, deadline)
        if not ok:
            self.abort_unarmed(line)
        ok, why = self.ack(pod_id, deadline)
        if not ok:
            self.abort_unarmed(f"emx.runpod_reservations refused the acknowledgement ({why})")
        self.say(f"killswitch {self.slot}: {line}")
        st.update(pod_id=pod_id, armed_deadline=deadline, phase="armed")
        self.save_state(st)
        self.rearm_others(dls)
        # 5-7: the bulk transfer, the pod .env, setup
        r = self.ssh("command -v rsync >/dev/null || (apt-get update -qq && apt-get install -y "
                     "-qq rsync); mkdir -p /ws/emx /ws/hf_cache /ws/uv_cache /ws/tmp",
                     timeout=600, addr=addr)
        if r.returncode != 0:
            raise ProbeError(f"pod prep failed (rc {r.returncode}): {r.stderr.strip()}")
        with tempfile.NamedTemporaryFile("w", suffix=".files", delete=False) as lf:
            lf.write("\0".join(files) + "\0")  # NUL-delimited: no name can inject an entry
        try:
            r = self.call(["rsync", "-lptz", "--timeout=120", "--from0", f"--files-from={lf.name}",
                           "-e",
                           self.rsync_e(port), "./", f"{user}@{host}:{POD_ROOT}/"],
                          timeout=RSYNC_TIMEOUT_S)
        finally:
            os.unlink(lf.name)
        if r.returncode != 0:
            raise ProbeError(f"rsync failed (rc {r.returncode}): {r.stderr.strip()[-500:]}")
        r = self.ssh(f"umask 077 && cat > {POD_ROOT}/.env.part && mv -f {POD_ROOT}/.env.part "
                     f"{POD_ROOT}/.env && chmod 600 {POD_ROOT}/.env", input=env_text, addr=addr)
        if r.returncode != 0:
            raise ProbeError(f"could not write the pod .env (rc {r.returncode})")
        self.say(f"synced {len(files)} files and the pod .env to {POD_ROOT}")
        if self.cfg.setup:
            self.setup(addr)
        st.update(phase="pushed", pushed_commit=head, pushed_utc=utc_iso(self.clock()))
        self.save_state(st)
        self.say(f"PROBE_PUSHED pod={pod_id} commit={head[:12]} deadline={utc_iso(deadline)}")
        return 0

    def rearm_others(self, dls: dict[str, int]) -> None:
        """Other running slots of this probe: the joint burn moved their deadline (best effort,
        as pod_push.sh does; their earlier arm stays if this fails)."""
        for s, d in dls.items():
            if s == self.slot:
                continue
            pid, host, port = (self.envval(f"{k}{s}") for k in
                               ("RUNPOD_POD_ID", "POD_SSH_HOST", "POD_SSH_PORT"))
            if not (pid and host and port):
                self.say(f"WARNING: running slot {s} has no .env address; not re-armed")
                continue
            addr = (pid, host, port, self.envval("POD_SSH_USER") or "root")
            ok, line = self.arm(addr, d)
            if ok:
                ok, line = self.ack(pid, d)
            self.say(f"killswitch {s}: {'re-armed' if ok else 'WARNING: NOT re-armed'} ({line})")

    def setup(self, addr) -> None:
        cmd = f"bash {POD_ROOT}/scripts/pod_setup.sh --only {self.cfg.setup}"
        r = self.ssh(f"rm -f /ws/setup.rc; {{ setsid nohup bash -c '{cmd} > /ws/setup.log 2>&1; "
                     "echo $? > /ws/setup.rc' >/dev/null 2>&1 </dev/null & }; echo setup launched",
                     addr=addr)
        if r.returncode != 0:
            raise ProbeError(f"could not launch pod_setup.sh (rc {r.returncode})")
        waited = 0.0
        while waited < self.setup_max_s:
            self.sleep(SETUP_POLL_S)
            waited += SETUP_POLL_S
            r = self.ssh("tail -n 2 /ws/setup.log 2>/dev/null; [ -f /ws/setup.rc ] && "
                         "echo \"__RC=$(cat /ws/setup.rc)\"; true", timeout=60, addr=addr)
            if r.returncode != 0:
                self.say("(setup poll failed; retrying)")
                continue
            m = re.search(r"^__RC=(\d+)$", r.stdout, re.MULTILINE)
            if m:
                if m.group(1) != "0":
                    raise ProbeError(f"pod_setup.sh --only {self.cfg.setup} failed "
                                     f"(rc {m.group(1)}; /ws/setup.log)")
                self.say(f"setup {self.cfg.setup}: done in ~{waited:.0f} s")
                return
        raise ProbeError(f"pod setup not done after {self.setup_max_s:.0f} s")

    # --- run -----------------------------------------------------------------------------------

    def run(self) -> int:
        st = self.load_state()
        addr = self.pod_addr()
        log = f"{self.rel}/logs/run_{utc_stamp(self.clock())}.log"
        d = shlex.quote(self.rel)
        inner = (f'echo $$ > "$0/logs/run.pid"; set -a; [ -f .env ] && . ./.env; set +a; '
                 f'export EMX_PROBE={self.exp}; exec bash "$0/run.sh"')
        remote = "\n".join([
            f"cd {POD_ROOT} || exit 5",
            f"D={d}",
            '[ -f "$D/run.sh" ] || { echo PROBE_NO_RUN_SH; exit 4; }',
            'mkdir -p "$D/logs" "$D/results" || exit 5',
            # review r1 finding 2: ANY earlier launch on this pod (alive, finished or crashed)
            # counts as launched: report it, never relaunch over its results
            ('if [ -e "$D/logs/run.launched" ] || [ -e "$D/logs/run.pid" ]; then '
             'L=$(cat "$D/logs/run.launched" 2>/dev/null); '
             '[ -n "$L" ] || L=$(ls -1t "$D"/logs/run_*.log 2>/dev/null | head -n 1); '
             'echo "PROBE_RUN_EXISTS log=$L pid=$(cat "$D/logs/run.pid" 2>/dev/null)"; exit 3; fi'),
            f"echo {shlex.quote(log)} > \"$D/logs/run.launched\" || exit 5",
            # the child writes its own pid ($$ survives the exec into run.sh), whether or not
            # setsid forked
            (f"setsid nohup bash -c {shlex.quote(inner)} \"$D\" > {shlex.quote(log)} 2>&1 "
             "< /dev/null &"),
            f"echo PROBE_LAUNCHED log={log}",
        ])
        if self.dry_run:
            self.say(f"DRY RUN: would launch on the pod (one ssh call):\n{remote}")
            self.say(f"DRY RUN: then after {LAUNCH_CHECK_S} s check that {log} grew")
            return 0
        r = self.ssh(remote, addr=addr)
        self.echo(r)
        if r.returncode == 3:
            m = re.search(r"^PROBE_RUN_EXISTS log=(\S*)", r.stdout or "", re.MULTILINE)
            st.update(run_log=(m.group(1) if m else "") or None, phase="running")
            self.save_state(st)
            self.say(f"PROBE_RUNNING already launched on this pod (log {st['run_log']}); not "
                     "relaunching")
            return 0
        if r.returncode != 0 or "PROBE_LAUNCHED" not in r.stdout:
            raise ProbeError(f"launch failed (rc {r.returncode})")
        st.update(run_log=log, run_started_utc=utc_iso(self.clock()), phase="running")
        self.save_state(st)
        self.sleep(LAUNCH_CHECK_S)
        r = self.ssh(f"cd {POD_ROOT} && echo __SIZE=$(wc -c < {shlex.quote(log)} 2>/dev/null "
                     "|| echo 0)", timeout=60, addr=addr)
        m = re.search(r"__SIZE=\s*(\d+)", r.stdout or "")
        if not m or int(m.group(1)) == 0:
            raise ProbeError(f"launch not verified: {log} is empty after {LAUNCH_CHECK_S} s "
                             "(run.sh must log a first line at once)")
        self.say(f"PROBE_RUNNING log={log} ({m.group(1)} bytes after {LAUNCH_CHECK_S} s)")
        return 0

    # --- status --------------------------------------------------------------------------------

    def poll(self, log: str | None = None) -> Poll:
        """One consolidated ssh call: run alive, last line, PROBE_DONE, manifest, GPU, killswitch."""
        d = shlex.quote(self.rel)
        lg = shlex.quote(log or "")
        remote = "\n".join([
            f"cd {POD_ROOT} 2>/dev/null || {{ echo __NOREPO; exit 0; }}",
            f"D={d}; LOG={lg}",
            '[ -n "$LOG" ] || LOG=$(ls -1t "$D"/logs/run_*.log 2>/dev/null | head -n 1)',
            ('A=0; P=$(cat "$D/logs/run.pid" 2>/dev/null); '
             '[ -n "$P" ] && kill -0 "$P" 2>/dev/null && A=1'),
            'echo "__ALIVE=$A"; echo "__LOG=$LOG"',
            '[ -s "$D/results/MANIFEST.sha256" ] && echo __MANIFEST=1',
            'echo "__NRESULTS=$(find "$D/results" -type f 2>/dev/null | wc -l | tr -d " ")"',
            ('echo "__GPU=$(nvidia-smi --query-gpu=utilization.gpu,memory.used '
             '--format=csv,noheader,nounits 2>/dev/null | head -n 1)"'),
            'echo "__KS=$(bash scripts/pod_killswitch.sh status 2>/dev/null | tail -n 1)"',
            'echo "__LAST=$(grep -v "^[[:space:]]*$" "$LOG" 2>/dev/null | tail -n 1)"',
            'echo __TAIL; [ -n "$LOG" ] && tail -n 5 "$LOG"; true',
        ])
        r = self.ssh(remote, timeout=90, effect=False)
        if r.returncode != 0 or "__ALIVE=" not in (r.stdout or ""):
            return Poll(reachable=False)
        head, _, tail = r.stdout.partition("__TAIL\n")
        kv = {}
        for line in head.splitlines():
            if line.startswith("__") and "=" in line:
                k, v = line[2:].split("=", 1)
                kv[k] = v
        last = kv.get("LAST", "")
        m = DONE_RE.match(last)
        n = kv.get("NRESULTS", "")
        return Poll(
            reachable=True,
            alive=kv.get("ALIVE") == "1",
            done_rc=int(m.group(1)) if m else None,
            manifest=kv.get("MANIFEST") == "1",
            n_results=int(n) if n.isdigit() else -1,
            gpu=kv.get("GPU", ""),
            ks=kv.get("KS", ""),
            last=last,
            log=kv.get("LOG", ""),
            tail=tuple(tail.splitlines()[-5:]),
        )

    def spend_line(self) -> str:
        t = spend.totals(self.exp)
        running = sum(s["usd"] for s in self.sessions() if s["running"])
        return (f"spent gpu ${t['gpu_usd']:.2f} + running ${running:.2f}, api "
                f"${t['judge_usd']:.2f} (+${t['judge_reserved_usd']:.2f} reserved); "
                f"cap ${t['hard_cap_usd']:.0f}, GPU kill line ${t['watchdog_kill_usd']:.2f}")

    def status_line(self, p: Poll, st: dict, phase: str | None = None) -> str:
        dl = st.get("armed_deadline")
        left = f"{(dl - self.clock()) / 3600:.2f}h" if dl else "?"
        last = p.last.replace('"', "'")[:120]
        return (f"PROBE_STATUS {utc_iso(self.clock())} exp={self.exp} slot={self.slot} "
                f"pod={st.get('pod_id', '-')} phase={phase or st.get('phase', '-')} "
                f"reachable={int(p.reachable)} alive={int(p.alive)} "
                f"done={'-' if p.done_rc is None else p.done_rc} manifest={int(p.manifest)} "
                f"results={p.n_results} gpu={p.gpu.replace(' ', '') or '-'} "
                f"deadline_in={left} last=\"{last}\"")

    def status(self) -> int:
        st = self.load_state()
        self.say(self.spend_line())
        dl = st.get("armed_deadline")
        self.say(f"killswitch deadline: {utc_iso(dl) if dl else 'unknown (not pushed by runner)'}")
        if self.dry_run:
            self.say("DRY RUN: would make one ssh call (run alive, last 5 log lines, PROBE_DONE, "
                     "manifest, GPU util, killswitch status)")
            return 0
        p = self.poll(st.get("run_log"))
        if p.reachable:
            self.say(f"pod killswitch: {p.ks or '?'}; log {p.log or '(none)'}")
            for line in p.tail:
                self.say(f"  | {line}")
        self.say(self.status_line(p, st))
        return 0 if p.reachable else 1

    # --- pull ----------------------------------------------------------------------------------

    def pull_logs(self, tries: int = 3) -> bool:
        """Mirror the pod's probes/<dir>/logs/ into probes/<dir>/logs/pod/ incrementally (logs are
        append-only, so no staging; committable), retried with backoff. True when it succeeded."""
        pod_id, host, port, user = self.pod_addr()
        src = f"{user}@{host}:{POD_ROOT}/{self.rel}/logs/"
        mirror = self.dir / "logs" / "pod" / pod_id  # per pod: a follow-up never overwrites
        if self.dry_run:
            self.say(f"DRY RUN: would rsync {src} into {self.rel}/logs/pod/{pod_id}/ "
                     "(incremental mirror)")
            return True
        mirror.mkdir(parents=True, exist_ok=True)
        for i in range(tries):
            r = self.call(["rsync", "-rlptz", "--timeout=120", "--exclude", "run.pid", "-e",
                           self.rsync_e(port), src, f"{mirror}/"], timeout=1800)
            if r.returncode == 0:
                return True
            self.say(f"WARNING: pod logs pull failed (rc {r.returncode}, try {i + 1}/{tries})")
            if i + 1 < tries:
                self.sleep(LOG_BACKOFF_S * 2**i)
        return False

    def fetch_results(self) -> tuple[Path, bool]:
        """rsync the pod's results/ into a fresh probes/<dir>/.pull_<random>/results/."""
        _, host, port, user = self.pod_addr()
        staging = Path(tempfile.mkdtemp(prefix=".pull_", dir=self.dir))
        (staging / "results").mkdir()
        r = self.call(["rsync", "-rlptz", "--timeout=120", "-e", self.rsync_e(port),
                       f"{user}@{host}:{POD_ROOT}/{self.rel}/results/", f"{staging}/results/"],
                      timeout=RSYNC_TIMEOUT_S)
        if r.returncode != 0:
            self.say(f"could not pull the pod's results (rc {r.returncode}); partial copy kept "
                     f"in {staging.relative_to(self.repo)}")
        return staging, r.returncode == 0

    def preserve(self, staging: Path) -> Path:
        """Move staged results to probes/<dir>/unverified/<UTC>/ (kept, never promoted)."""
        dest = self.dir / "unverified" / utc_stamp(self.clock())
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{utc_stamp(self.clock())}_{n}")
            n += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging / "results", dest)
        shutil.rmtree(staging)
        return dest

    def pull(self, logs_only: bool = False) -> PullResult:
        """Logs into the mirror (retried), then results: promoted into probes/<dir>/results/ only
        when results/MANIFEST.sha256 verifies AND lists every pulled result file ('complete');
        otherwise every pulled file is kept (unverified/<UTC>/ or the .pull_* staging dir)."""
        logs_ok = self.pull_logs()
        if logs_only:
            return PullResult(logs_ok, "skipped", f"{self.rel}/logs/pod")
        if self.dry_run:
            self.say("DRY RUN: would rsync the pod's results/ into .pull_<random>/, verify "
                     "MANIFEST.sha256 there (shasum -a 256 -c) and promote only a complete one")
            return PullResult(True, "complete", "")
        staging, ok = self.fetch_results()
        rel_st = str(staging.relative_to(self.repo))
        if not ok:
            return PullResult(logs_ok, "copy_failed", rel_st)
        res = staging / "results"
        man = res / "MANIFEST.sha256"
        if not man.exists():
            dest = self.preserve(staging)
            self.say(f"no results/MANIFEST.sha256 yet: results kept unverified in "
                     f"{dest.relative_to(self.repo)}")
            return PullResult(logs_ok, "no_manifest", str(dest.relative_to(self.repo)))
        try:
            listed = parse_manifest(man)
        except ProbeError as e:
            self.say(f"MANIFEST refused ({e}); nothing promoted; staging kept: {rel_st}")
            return PullResult(logs_ok, "failed", rel_st)
        r = self.call(["shasum", "-a", "256", "-c", "--quiet", "MANIFEST.sha256"], effect=False,
                      cwd=res, timeout=1800)
        if r.returncode != 0:
            self.echo(r)
            self.say(f"MANIFEST does NOT verify: nothing promoted; staging kept: {rel_st}")
            return PullResult(logs_ok, "failed", rel_st)
        pulled = {p.relative_to(res).as_posix() for p in res.rglob("*") if p.is_file()}
        unlisted = sorted(pulled - set(listed) - {"MANIFEST.sha256"})
        if unlisted:
            dest = self.preserve(staging)
            self.say(f"MANIFEST verifies but does not list {len(unlisted)} pulled file(s) (e.g. "
                     f"{unlisted[0]}): not complete; kept unverified in "
                     f"{dest.relative_to(self.repo)}")
            return PullResult(logs_ok, "incomplete", str(dest.relative_to(self.repo)))
        stamp = utc_stamp(self.clock())
        dest = self.dir / "results"
        n_super = 0
        for f in [*listed, "MANIFEST.sha256"]:
            s, t = res / f, dest / f
            if t.exists() and not filecmp.cmp(s, t, shallow=False):
                aside = self.dir / "superseded" / stamp / f
                aside.parent.mkdir(parents=True, exist_ok=True)
                os.replace(t, aside)
                n_super += 1
            t.parent.mkdir(parents=True, exist_ok=True)
            os.replace(s, t)
        shutil.rmtree(staging)
        if n_super:
            self.say(f"moved {n_super} changed local file(s) to {self.rel}/superseded/{stamp}/")
        self.say(f"PROBE_PULLED verified {len(listed)} file(s) into {self.rel}/results/"
                 + ("" if logs_ok else " (but the logs pull FAILED)"))
        return PullResult(logs_ok, "complete", f"{self.rel}/results")

    def stop_script(self) -> str:
        """The remote quiesce script: TERM the run's process group (setsid makes the run.pid
        process its leader, or the leader is gone; a pid that is not a group leader may be a
        reused pid and is never signalled), and the bracketed run.sh pattern for strays; wait up
        to STOP_GRACE_S; KILL; re-check. It
        prints `PROBE_QUIESCED pgid=<pid>` (exit 0) ONLY when no process of the run remains."""
        pat = shlex.quote(f"[{self.rel[0]}]{self.rel[1:]}/run.sh")
        return "\n".join([
            f"cd {POD_ROOT} || exit 5",
            f"P=$(cat {shlex.quote(self.rel)}/logs/run.pid 2>/dev/null)",
            'case "$P" in ""|*[!0-9]*) P="" ;; esac',
            'GRP=0; if [ -n "$P" ]; then G=$(ps -o pgid= -p "$P" 2>/dev/null | tr -d " "); ',
            '{ [ -z "$G" ] || [ "$G" = "$P" ]; } && GRP=1; fi',
            "left() {",
            "  n=0",
            '  if [ "$GRP" = 1 ]; then n=$(ps -eo pgid= | tr -d " " | grep -cx "$P"); fi',
            f"  m=$(pgrep -f {pat} | wc -l | tr -d ' ')",
            "  echo $((n + m))",
            "}",
            "sig() {",
            '  if [ "$GRP" = 1 ]; then kill -"$1" -- -"$P" 2>/dev/null; fi',
            f'  pkill -"$1" -f {pat} 2>/dev/null',
            "  return 0",
            "}",
            "sig TERM",
            f'i=0; while [ "$(left)" != 0 ] && [ $i -lt {STOP_GRACE_S} ]; do sleep 1; i=$((i + 1)); ',
            "done",
            '[ "$(left)" = 0 ] || sig KILL',
            'i=0; while [ "$(left)" != 0 ] && [ $i -lt 5 ]; do sleep 1; i=$((i + 1)); done',
            'N=$(left)',
            'if [ "$N" = 0 ]; then echo "PROBE_QUIESCED pgid=${P:-none}"; exit 0; fi',
            'echo "PROBE_STOP_FAILED left=$N"',
            "exit 7",
        ])

    def stop_producer(self, tries: int = 3) -> bool:
        """Quiesce the run before a salvage teardown (review r2 finding 1): True only on the
        anchored `PROBE_QUIESCED` ack with ssh exit 0; retried with backoff."""
        script = self.stop_script()
        for i in range(tries):
            r = self.ssh(script, timeout=STOP_GRACE_S + 120, effect=False)
            if r.returncode == 0 and QUIESCED_RE.search(r.stdout or ""):
                self.say(f"producer stopped on the pod ({QUIESCED_RE.search(r.stdout).group(0)})")
                return True
            last = (r.stdout or r.stderr or "").strip().splitlines()[-1:] or [""]
            self.say(f"WARNING: producer stop not confirmed (ssh rc {r.returncode}: {last[0]}; "
                     f"try {i + 1}/{tries})")
            if i + 1 < tries:
                self.sleep(LOG_BACKOFF_S * 2**i)
        return False

    def salvage_copy(self) -> PullResult:
        """Mirror the logs and copy ALL results into unverified/<UTC>/ (no verification)."""
        logs_ok = self.pull_logs()
        staging, ok = self.fetch_results()
        if not ok:
            return PullResult(logs_ok, "copy_failed", str(staging.relative_to(self.repo)))
        dest = self.preserve(staging)
        return PullResult(logs_ok, "salvaged", str(dest.relative_to(self.repo)))

    def api_pod_state(self, pod_id: str) -> str:
        """'running' | 'stopped' | 'gone' | 'unknown', from the RunPod API (a read)."""
        try:
            p = rp.RunPod(self.envval("RUNPOD_API_KEY"), retries=2).get_pod(pod_id)
        except rp.RunPodError as e:
            return "gone" if "HTTP 404" in str(e) else "unknown"
        status = str((p or {}).get("desiredStatus") or "").upper()
        if status in ("EXITED", "TERMINATED", "STOPPED"):
            return "stopped"
        return "running" if status == "RUNNING" else "unknown"

    # --- down ----------------------------------------------------------------------------------

    def down(self, reason: str = "probe_pod down", by_pod_id: bool = False) -> int:
        """pod_down.py for the slot. `by_pod_id` (the API confirmed the pod stopped or gone, or
        the slot keys were cleared): pod_down's reservation-backed `--pod-id` recovery path, which
        settles a 404'd pod from durable history even while slot keys remain."""
        st = self.load_state()
        slot_pod = self.envval(f"RUNPOD_POD_ID{self.slot}")
        pod_id = st.get("pod_id") if by_pod_id else (slot_pod or st.get("pod_id"))
        pod_id = pod_id or slot_pod
        rid = (self.reservation_for(pod_id) if pod_id else None) or st.get("reservation_id")
        argv = PY + ["scripts/pod_down.py", "--exp", self.exp, "--slot", self.slot,
                     "--reason", reason]
        if (by_pod_id or not slot_pod) and not self.dry_run:
            if not (pod_id and rid):
                if slot_pod:  # no reservation to settle by id: the slot path is all there is
                    by_pod_id = False
                else:
                    self.say(f"slot {self.slot} is empty and no reservation-backed pod is "
                             "recorded: nothing to terminate or settle")
                    return 0
            else:
                argv += ["--pod-id", pod_id]
        if rid:
            argv += ["--reservation", rid]
        if self.dry_run:
            r = self.call(argv + ["--dry-run"], effect=False)
            self.echo(r)
            return r.returncode
        r = self.call(argv, timeout=900)
        self.echo(r)
        if r.returncode != 0:
            raise ProbeError(f"pod_down failed (rc {r.returncode}); the pod may still bill")
        st.update(phase="down", down_utc=utc_iso(self.clock()))
        self.save_state(st)
        self.say(f"PROBE_DOWN pod={pod_id} slot={self.slot}")
        return 0

    # --- go ------------------------------------------------------------------------------------

    def _resume_point(self, fresh: bool) -> tuple[dict, str]:
        """(state, mode): 'fresh' | 'resume' | 'settle' (the recorded pod left its slot or the
        event log closed it before the runner finished). Refuses a slot pod it did not record."""
        st = self.load_state()
        if st and fresh:
            if not self.dry_run:
                os.replace(self.state_path, self.state_path.with_name(
                    f"runner_state.{utc_stamp(self.clock())}.json"))
            st = {}
        if st.get("phase") == "down":
            raise ProbeError(f"{self.exp} already finished (phase down, {st.get('down_utc')}); "
                             "rerun with --fresh to start a new pod", 0)
        if st.get("phase") == "idle":  # an earlier up released its reservation: start over
            st = {}
        pod_id = self.envval(f"RUNPOD_POD_ID{self.slot}")
        if pod_id:
            if not self.pod_running(pod_id):
                if st.get("pod_id") == pod_id:
                    return st, "settle"
                raise ProbeError(f"slot {self.slot} names pod {pod_id}, which the event log does "
                                 "not show running; run `down` first")
            if st.get("pod_id") != pod_id:
                sess = next((s for s in self.sessions() if s["pod_id"] == pod_id), {})
                if st.get("reservation_id") and sess.get("reservation_id") == st["reservation_id"]:
                    st.update(pod_id=pod_id, phase="up")  # up crashed after pod_up created it
                    self.save_state(st)
                else:
                    raise ProbeError(f"slot {self.slot} holds pod {pod_id}, not recorded in "
                                     f"{self.state_path}; down it or rerun with a free --slot")
            return st, "resume"
        if st.get("pod_id"):
            return st, "settle"
        return st, "fresh"

    def go(self, fresh: bool = False) -> int:
        st, mode = self._resume_point(fresh)
        if self.dry_run:
            self.say(f"DRY RUN: go ({mode}): up -> push -> run -> poll every "
                     f"{self.cfg.poll_minutes:g} min -> verified pull -> down")
            if mode == "fresh":
                self.up()
            self.push()
            self.run()
            self.pull()
            self.down()
            return 0
        if mode == "settle":
            pod_id = st["pod_id"]
            api = self.api_pod_state(pod_id)
            if api in ("stopped", "gone") or not self.pod_running(pod_id):
                return self.settle_lost(pod_id, f"restart: recorded pod {pod_id} left slot "
                                        f"{self.slot} (API: {api})")
            self.alert(f"restart: recorded pod {pod_id} is not in slot {self.slot} but the API "
                       f"says {api}; restore its .env keys or settle it by hand")
            return 1
        if mode == "resume":
            self.say(f"resuming {self.exp} on pod {st.get('pod_id')} (phase {st.get('phase')})")
            if st.get("phase") == "recovering":
                return self.recover()
        else:
            self.up()
            st = self.load_state()
        if st.get("phase") in ("up", "reserved", "armed"):
            try:
                self.push()
            except ProbeError as e:
                if e.code != 4:  # code 4 already terminated the pod
                    self.say(f"push failed ({e}); no results exist yet: terminating the pod")
                    self.down(reason=f"probe_pod go: push failed ({e})"[:200])
                raise
            st = self.load_state()
        if st.get("phase") == "pushed":
            try:  # an earlier launch on this pod is adopted, never relaunched (run's guard)
                self.run()
            except ProbeError as e:
                self.say(f"run launch problem ({e}); polling to see what the pod did")
            st = self.load_state()
        return self.watch(st)

    def watch(self, st: dict) -> int:
        deadline = st.get("armed_deadline")
        if not deadline:
            deadline = self.deadlines()[self.slot]
            st["armed_deadline"] = deadline
            self.save_state(st)
        poll_s = self.cfg.poll_minutes * 60
        pod_id = st.get("pod_id")
        while True:
            self.ensure_watchdog(wait=False)
            try:  # one log rsync and one status ssh call per poll
                self.pull_logs(tries=1)
                p = self.poll(st.get("run_log"))
            except ProbeError as e:  # e.g. the watchdog terminated the pod and cleared the slot
                self.say(f"poll failed: {e}")
                p = Poll(reachable=False)
            self.say(self.status_line(p, st))
            now = self.clock()
            if p.reachable:
                if p.done_rc is not None:
                    return self.finish(p, f"PROBE_DONE rc={p.done_rc}")
                if not p.alive:
                    return self.finish(p, "the run ended without PROBE_DONE")
                if now >= deadline - DEADLINE_MARGIN_S:
                    return self.finish(p, "killswitch deadline minus 15 min, run still alive")
            else:
                api = self.api_pod_state(pod_id) if pod_id else "gone"
                self.say(f"pod unreachable over ssh; RunPod API says {api}")
                if api in ("stopped", "gone") or not self.pod_running(pod_id):
                    return self.settle_lost(pod_id, f"pod {pod_id} unreachable and confirmed "
                                            f"{api if api != 'running' else 'terminated'}")
                if now >= deadline + LOST_GRACE_S:
                    self.alert(f"pod {pod_id} unreachable {LOST_GRACE_S // 60} min past its "
                               f"killswitch deadline and the API says {api}; check the console")
                    return 1
            self.sleep(min(poll_s, max(60.0, deadline - DEADLINE_MARGIN_S - now))
                       if now < deadline - DEADLINE_MARGIN_S else poll_s)

    def settle_lost(self, pod_id: str | None, why: str) -> int:
        """A pod confirmed stopped or gone: settle it through pod_down's reservation-backed
        `--pod-id` path (idempotent) and alert; only what was mirrored so far exists locally."""
        self.alert(f"{why}; settling with pod_down; only the logs mirrored so far exist")
        try:
            self.down(reason=f"probe_pod go: {why}"[:200], by_pod_id=True)
        except ProbeError as e:
            self.alert(f"pod_down failed while settling ({e}); settle by hand")
        self.stop_watchdog_if_idle()
        return 1

    def finish(self, p: Poll, reason: str) -> int:
        """complete (PROBE_DONE + verified, covering manifest + logs) -> down, exit 0/2; else the
        persisted recovery phase (`recover`)."""
        self.say(f"stopping the poll: {reason}")
        if p.done_rc is not None:
            res = None
            for i in range(3):
                res = self.pull()
                if res.complete:
                    st = self.load_state()
                    st.update(final_pull="complete", stop_reason=reason)
                    self.save_state(st)
                    self.down(reason=f"probe_pod go: {reason}; results verified"[:200])
                    self.stop_watchdog_if_idle()
                    self.say(self.status_line(p, self.load_state(), phase="done"))
                    return 0 if p.done_rc == 0 else 2
                if res.results in ("no_manifest", "incomplete"):
                    break  # retrying cannot help: salvage
                if i < 2:
                    self.sleep(LOG_BACKOFF_S * 2**i)
            reason += (f"; final pull {res.results}"
                       f"{'' if res.logs_ok else ', logs pull FAILED'}")
        st = self.load_state()
        st.update(phase="recovering", recover_reason=reason, quiesced=False, salvage=None)
        self.save_state(st)
        return self.recover()

    def recover(self) -> int:
        """The persisted `recovering` phase (review r2 findings 1 and 3), resumed by `go` after a
        restart: quiesce the run (confirmed ack), then copy logs + ALL results into unverified/;
        retried with bounded backoff while time remains before the killswitch deadline. Teardown
        (exit 5) only once a copy made AFTER a confirmed stop succeeded. A copy made without a
        confirmed stop is kept but never authorizes teardown. A pod the API confirms stopped or
        gone is settled. Out of time: wait for the killswitch, then settle a stopped pod."""
        st = self.load_state()
        reason = st.get("recover_reason", "recovery")
        deadline = st.get("armed_deadline") or self.deadlines()[self.slot]
        pod_id = st.get("pod_id")
        quiesced = bool(st.get("quiesced"))
        warned = False
        attempt = 0
        while True:
            if not quiesced:
                quiesced = self.stop_producer()
                if quiesced:
                    st.update(quiesced=True)
                    self.save_state(st)
            if quiesced or not st.get("salvage"):
                sal = self.salvage_copy()
                if sal.logs_ok and sal.results == "salvaged":
                    st.update(salvage=sal.where, salvage_quiesced=quiesced)
                    self.save_state(st)
                    if quiesced:
                        self.alert(f"{reason}: SALVAGED (unverified) into {sal.where}; "
                                   "terminating the pod")
                        self.down(reason=f"probe_pod go: {reason}; salvaged"[:200])
                        self.stop_watchdog_if_idle()
                        return 5
                else:
                    self.say(f"salvage copy failed (results {sal.results}, logs "
                             f"{'ok' if sal.logs_ok else 'FAILED'})")
                    api = self.api_pod_state(pod_id) if pod_id else "gone"
                    if api in ("stopped", "gone"):
                        return self.settle_lost(pod_id, f"{reason}; pod confirmed {api} during "
                                                "recovery")
            if not quiesced and st.get("salvage") and not warned:
                self.alert(f"{reason}: results copied to {st['salvage']} but the producer stop is "
                           "NOT confirmed: not terminating; retrying the stop")
                warned = True
            wait = min(LOG_BACKOFF_S * 2**attempt, RECOVER_MAX_WAIT_S)
            now = self.clock()
            if now + wait >= deadline - 60:
                break
            attempt += 1
            self.sleep(wait)
        self.alert(f"{reason}: recovery out of time (quiesced={quiesced}, copy "
                   f"{st.get('salvage') or 'none'}); the pod is LEFT for the killswitch "
                   f"({utc_iso(deadline)}); settling once it has stopped")
        while self.clock() < deadline + LOST_GRACE_S:
            self.sleep(max(60.0, min(RECOVER_MAX_WAIT_S, deadline + 120 - self.clock())))
            api = self.api_pod_state(pod_id) if pod_id else "gone"
            if api in ("stopped", "gone"):
                return self.settle_lost(pod_id, f"{reason}; pod {api} after the killswitch")
        self.alert(f"{reason}: pod {pod_id} still not confirmed stopped {LOST_GRACE_S // 60} min "
                   "after its deadline; check the console")
        return 1

    def stop_watchdog_if_idle(self) -> None:
        if any(s["running"] for s in self.sessions()):
            return
        stop = self.repo / "logs" / f"watchdog_{self.exp}.stop"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.touch()
        self.say(f"no {self.exp} pod running: asked the watchdog to stop ({stop.name})")


def probe_id(s: str) -> str:
    if not ID_RE.fullmatch(s):
        raise argparse.ArgumentTypeError(f"must look like P01 (got {s!r})")
    return s


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--exp", required=True, type=probe_id, help="probe id, e.g. P01")
    common.add_argument("--slot", help="pod slot (.env key suffix); default _<ID>")
    common.add_argument("--dry-run", action="store_true", help="print; mutate nothing")
    ap = argparse.ArgumentParser(prog="probe_pod.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("up", "push", "run", "status", "pull", "down", "go"):
        s = sub.add_parser(name, parents=[common])
        if name in ("up", "go"):
            s.add_argument("--watchdog-interval", type=int, default=300)
            s.add_argument("--reserve-wait-min", type=float, default=60.0)
        if name in ("push", "go"):
            s.add_argument("--setup-max-s", type=float, default=2400.0)
        if name == "pull":
            s.add_argument("--logs-only", action="store_true")
        if name == "down":
            s.add_argument("--reason", default="probe_pod down")
        if name == "go":
            s.add_argument("--fresh", action="store_true",
                           help="archive a finished runner_state.json and start a new pod")
    a = ap.parse_args(argv)
    try:
        r = Runner(
            a.exp, a.slot, dry_run=a.dry_run,
            watchdog_interval=getattr(a, "watchdog_interval", 300),
            reserve_wait_s=getattr(a, "reserve_wait_min", 60.0) * 60,
            setup_max_s=getattr(a, "setup_max_s", 2400.0),
        )
        if a.cmd in ("up", "push", "run", "down", "go"):
            r.acquire_lock()
        if a.cmd == "pull":
            res = r.pull(logs_only=a.logs_only)
            if a.logs_only:
                return 0 if res.logs_ok else 1
            return 0 if res.complete else 3 if res.results == "no_manifest" else 1
        if a.cmd == "down":
            return r.down(reason=a.reason)
        if a.cmd == "go":
            return r.go(fresh=a.fresh)
        return getattr(r, a.cmd)()
    except (ProbeError, KeyError, ValueError) as e:
        code = e.code if isinstance(e, ProbeError) else 1
        tag = "PROBE_NOTE" if code == 0 else "PROBE_ERROR"
        print(f"{tag} {a.cmd} {a.exp}: {e}", file=sys.stderr, flush=True)
        return code


if __name__ == "__main__":
    sys.exit(main())
