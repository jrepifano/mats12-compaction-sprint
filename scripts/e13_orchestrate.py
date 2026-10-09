"""E13 laptop orchestrator (implementation plan unit H): runs the paid chain as a resumable state
machine and applies every automatic prereg rule.

Each call of `advance` walks the stages below in order. A finished stage is recorded in
`results/E13/status.json` (contracts.STATUS_PATH) and skipped next time, so the script is simply
re-run after any interruption. A stage raises `Blocked` when it needs Jacob (the notice check
before training, the labels before the final readouts) or a pod job that is still running, and
`Stop` when a preregistered stop rule fires (the stop is recorded, the pod's results are pulled
and the pod terminated). Every action prints one `E13_ORCH <json>` line and is appended to
`logs/E13-orchestrate.jsonl`; `status` prints the status block (phase, spend against the cap,
pod-hours, the next automatic action).

Stages (prereg section; the unit whose CLI does the work):
  notices         §4.1 write, check, filter (B: scripts/e13_notices.py; survival stop)
  style_notices   Amendment A, notices (B2: scripts/e13_style.py notices)
  notice_check    §4.4 Jacob's check (F: page + score); blocks until labels/notice/record.json
  inferred        Amendment D: the inferred-cue snippet pairs, write, check, filter
                  (scripts/e13_inferred.py); a dropped battery is recorded, not a stop
  inputs          style answers (B2), inputs build + manifest --require-style (B), and
                  --require-inferred unless the inferred battery was dropped; commit
  launch          RunPod reservation (both stages on one pod: stage1 + patch, decision 1),
                  pod_up, watchdog, pod_push (A)
  pod_a           download smoke bench-train train-trigger lora-check gen-trigger (G)
  plumbing        judge the trigger batteries (E), decide plumbing; stop on failure (§4.6 item 5)
  pod_b           train r1, smoke-adapters r1, bench-hooked (G)
  projection      decide projection; stop if it cannot fit after the cuts (§7, Amendment B)
  gen_dev         gen-organism dev (r1), pull, start the other organism batteries on the pod
                  (narrow, infam, final and, unless dropped, Amendment D's inferred)
  dev_decision    judge dev; decide dev (r1); fallback -> r32 chain and decide dev (r32)
  dirsource       gen-dirsource (+ the one extension), judge, decide dirsource labels, push
  direction       capture, direction, hook-check per site; live sites from their records
  sweep           the development sweep (cut flags from the projection); meanwhile judge the
                  organism batteries and rate the organism answers' style
  strength        judge the sweep, consistency (shared stop), strength per live site
  label_pass      draw the full labelling pass (or the early-stop pass on a stop); never blocks
  final_patch     final patch cells from the strength records; judge them
  teardown        closing manifest, pull, pod_down
  analysis        blocks until the labels record exists; e13_analyze.py

Amendment E (Jacob, 2026-10-09, after the first run): `amend-e` starts a second phase from the
stopped (or analysed) first run. It archives the first run's status.json to
results/E13/run1_status.json and resets the state to phase "amend_e", recipe "tan32", keeping the
stages reused as frozen (notices, notice check, inferred pairs, inputs, plumbing, the pod-1
benchmarks). `advance` then walks `walk_e`:
  launch          a NEW pod; the push restores the reused pod records (lora-check, bench-train,
                  bench-hooked) with pod_push --restore-only
  train_tan32     download (input verification), then train tan32 (all four arms) and
                  smoke-adapters tan32 (bench-hooked reused from bench/hooked.json)
  projection      as before, recipe tan32
  gen_dev         gen-organism dev (tan32 arms only; the base model's first-run CSVs are reused),
                  then the other organism batteries in the background
  dev_decision    judge dev, decide dev (tan32); no further fallback
  dirsource       60 answers per cell; the cell rule failing -> the one extension (to 120)
  direction       capture, direction; if the cell rule, the norm floor or reliability fails at
                  EITHER site and the source was not yet extended -> ONE shared extension to 120
                  (gen-dirsource --extend, judge, labels, capture, direction), which supersedes the
                  60-answer result at both sites (that result is kept under run2/direction/at60/);
                  then hook checks only at passing sites
  sweep ... analysis  as before (the analysis: e13_analyze.py --recipe tan32)
Every per-run record of the retry lives under results/E13/run2/ (contracts.RUN2_DIR) and its
analysis in results/E13/analysis_tan32.json; the base model's judged batteries are never
re-judged (judged in the first run by both judges).

Side effects go through `Deps` (local commands, remote commands on the pod, clock, git commit),
so tests drive every branch with fakes (tests/e13/test_orchestrate.py).

Usage:
  uv run python scripts/e13_orchestrate.py status
  uv run python scripts/e13_orchestrate.py advance [--once]
  uv run python scripts/e13_orchestrate.py plan          # the stage list, nothing run
  uv run python scripts/e13_orchestrate.py amend-e       # Amendment E: start the second phase
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e13 import contracts as C

LOG = "logs/E13-orchestrate.jsonl"
POD_WS = C.POD_ROOT
POLL_S = 60
MAX_POLL_FAILURES = 10  # consecutive failed polls (~10 min) -> the pod is treated as unreachable
MAX_JOB_H = 8.0
WATCHDOG_INTERVAL_S = 300  # no single pod job may run longer (the watchdog and killswitch cap the spend)
PY = ["uv", "run", "python"]
BLOCKED_RC = 10
STOP_RC = 2
# Amendment E
AMEND_E = "amend_e"  # state["phase"] of the second phase
RECIPE_E = "tan32"
REUSED_STAGES_E: tuple[str, ...] = (  # first-run stages reused as frozen (Amendment E item 5)
    "notices", "style_notices", "notice_check", "inferred", "inputs", "pod_a", "plumbing", "pod_b",
)
RESTORE_E: tuple[str, ...] = (  # pod records the new pod needs, restored from the laptop
    "MANIFEST.lora-check.sha256", "MANIFEST.bench-train.sha256", "MANIFEST.bench-hooked.sha256",
)
AT60_DIR = f"{C.direction_dir(RECIPE_E)}/at60"  # the superseded 60-answer direction records


class Blocked(Exception):
    """The stage cannot finish now: a human step, or a pod job still running."""

    def __init__(self, reason: str, human: bool = False):
        super().__init__(reason)
        self.reason, self.human = reason, human


class Stop(Exception):
    """A preregistered stop rule fired."""

    def __init__(self, rule: str, detail: str = ""):
        super().__init__(f"{rule}: {detail}")
        self.rule, self.detail = rule, detail


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Deps:
    root: Path
    run: Callable[[list[str], int], tuple[int, str]]  # local command -> (rc, output)
    remote: Callable[[str, int], tuple[int, str]]  # shell command on the pod -> (rc, output)
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    wait_jobs: bool = True  # False: a running pod job raises Blocked instead of polling


def _local_run(root: Path) -> Callable[[list[str], int], tuple[int, str]]:
    def run(cmd: list[str], timeout: int) -> tuple[int, str]:
        try:
            p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout,
                               check=False)
        except subprocess.TimeoutExpired as e:  # a timeout is a failed command, never a crash
            return 124, f"timeout after {timeout}s: {e}"
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    return run


def _read_env(root: Path) -> dict[str, str]:
    out = {}
    p = root / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def _ssh_remote(root: Path) -> Callable[[str, int], tuple[int, str]]:
    def remote(cmd: str, timeout: int) -> tuple[int, str]:
        env = _read_env(root)
        host, port = env.get(f"POD_SSH_HOST{C.SLOT}"), env.get(f"POD_SSH_PORT{C.SLOT}")
        if not host or not port:
            return 255, "no pod ssh target in .env"
        ssh = ["ssh", "-n", "-p", port, "-o", "ConnectTimeout=20", "-o",
               "StrictHostKeyChecking=accept-new", f"root@{host}", cmd]
        try:
            p = subprocess.run(ssh, capture_output=True, text=True, timeout=timeout,
                           check=False)
        except subprocess.TimeoutExpired:
            return 124, "ssh timeout"
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    return remote


def real_deps(root: Path = ROOT) -> Deps:
    return Deps(root=root, run=_local_run(root), remote=_ssh_remote(root))


@dataclass
class Orchestrator:
    d: Deps
    state: dict = field(default_factory=dict)

    # -- state and log ----------------------------------------------------------------------

    @property
    def status_path(self) -> Path:
        return self.d.root / C.STATUS_PATH

    def load(self) -> None:
        p = self.status_path
        self.state = json.loads(p.read_text()) if p.exists() else {}
        self.state.setdefault("stages", {})
        self.state.setdefault("jobs", {})
        self.state.setdefault("human", {})

    def save(self) -> None:
        p = self.status_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n")
        tmp.replace(p)

    def emit(self, event: str, **kw) -> None:
        rec = {"utc": utc(), "event": event, **kw}
        line = json.dumps(rec, default=str)
        print(f"E13_ORCH {line}", flush=True)
        p = self.d.root / LOG
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(line + "\n")

    def done(self, stage: str) -> bool:
        return stage in self.state["stages"]

    def mark(self, stage: str, **info) -> None:
        self.state["stages"][stage] = {"utc": utc(), **info}
        self.save()
        self.emit("stage_done", stage=stage, **info)

    # -- commands ---------------------------------------------------------------------------

    def sh(self, cmd: list[str], timeout: int = 7200, ok: tuple[int, ...] = (0,)) -> str:
        rc, out = self.d.run(cmd, timeout)
        self.emit("cmd", cmd=shlex.join(cmd), rc=rc, tail=out[-400:])
        if rc not in ok:
            raise RuntimeError(f"command failed rc={rc}: {shlex.join(cmd)}\n{out[-2000:]}")
        return out

    def py(self, script: str, *args: str, timeout: int = 7200) -> str:
        return self.sh([*PY, f"scripts/{script}", *args], timeout)

    def read(self, rel: str) -> dict | None:
        p = self.d.root / rel
        return json.loads(p.read_text()) if p.exists() else None

    def amend_e(self) -> bool:
        return self.state.get("phase") == AMEND_E

    def rr(self) -> str | None:
        """The run's recipe for the per-run paths: tan32 in the Amendment E phase, else None
        (the first run's paths)."""
        return RECIPE_E if self.amend_e() else None

    def decision(self, name: str) -> dict:
        rec = self.read(C.decision_path(name, self.rr()))
        if rec is None:
            raise RuntimeError(f"decision {name} missing after its command")
        return rec

    def commit(self, paths: list[str], msg: str) -> None:
        existing = [p for p in paths if (self.d.root / p).exists()]
        if not existing:
            return
        self.sh(["git", "add", "-A", "--", *existing])
        rc, _ = self.d.run(["git", "diff", "--cached", "--quiet"], 60)
        if rc != 0:
            self.sh(["git", "commit", "-q", "-m",
                     f"E13 run: {msg}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"])

    # -- pod jobs ---------------------------------------------------------------------------

    def run2_env(self) -> list[str]:
        """Amendment E item 5 (code review amendE-r1): the retry's pod jobs and manifest listings
        run with E13_RUN2=1, so the new pod's download, closing and recovery records get their
        own names and never replace the first run's."""
        return ["env", "E13_RUN2=1"] if self.amend_e() else []

    def recovery_manifest(self) -> str:
        return "MANIFEST.recovery.run2.sha256" if self.amend_e() else "MANIFEST.recovery.sha256"

    def pod_cmd(self, steps: list[list[str]]) -> str:
        toks = [t for s in steps for t in s]
        env = "E13_RUN2=1 " if self.amend_e() else ""
        return f"cd {POD_WS} && {env}bash scripts/pod_run_e13.sh --detach {shlex.join(toks)}"

    def manifests_for(self, steps: list[list[str]]) -> list[str]:
        out = []
        for s in steps:
            txt = self.sh([*self.run2_env(), *PY, "scripts/e13_pod.py", *s, "--print-manifests"],
                          timeout=120)
            out += [ln.strip() for ln in txt.splitlines()
                    if ln.strip() and not ln.startswith(("E13_", "warning"))]
        return out

    def start_job(self, name: str, steps: list[list[str]]) -> None:
        """Start `steps` detached on the pod (one job at a time: one GPU)."""
        jobs = self.state["jobs"]
        if name in jobs and jobs[name].get("state") != "interrupted":
            return
        for other, j in jobs.items():
            if j.get("state") == "running":
                raise RuntimeError(f"pod job {other} still running; wait for it first")
        rc, out = self.d.remote(self.pod_cmd(steps), 120)
        log = next((ln.split("=", 1)[1].strip() for ln in out.splitlines()
                    if ln.startswith("RUN_LOG=")), None)
        if rc != 0 or not log:
            raise RuntimeError(f"pod job {name} did not start: rc={rc} {out[-500:]}")
        jobs[name] = {"state": "running", "steps": steps, "log": log, "started": utc(),
                      "started_epoch": self.d.now()}
        self.save()
        self.emit("job_start", job=name, log=log, steps=steps)

    def restart_interrupted(self) -> None:
        """After a relaunch: restart each interrupted job with its own steps, one at a time."""
        for name, j in list(self.state["jobs"].items()):
            if j.get("state") == "interrupted":
                # the interrupted entry stays until start_job replaces it with a running one
                # (code review r3): a failed launch leaves it interrupted for the next advance
                self.start_job(name, j["steps"])
                self.wait_job(name)

    def poll_job(self, name: str) -> str:
        j = self.state["jobs"][name]
        if j["state"] != "running":
            return j["state"]
        log = shlex.quote(j["log"])
        # one consolidated ssh call per check (CLAUDE.md: polling too hard looks like a dead pod)
        rc, out = self.d.remote(
            f"cd {POD_WS} && grep -h '^E13_' {log} | tail -n 5; "
            f"echo E13_ORCH_ALIVE=$(pgrep -f '[p]od_run_e13.sh' | wc -l)", 60)
        if rc != 0:
            j["poll_failures"] = j.get("poll_failures", 0) + 1
            self.save()
            self.emit("poll_failed", job=name, rc=rc, n=j["poll_failures"], tail=out[-200:])
            if j["poll_failures"] >= MAX_POLL_FAILURES:
                raise Blocked(f"pod unreachable for {j['poll_failures']} polls during {name}: "
                              f"diagnose, then `e13_orchestrate.py relaunch` (replacement pod, "
                              f"pod_push --restore) or re-run advance", human=True)
            return "running"  # a dropped ssh is not a dead pod; the watchdog guards the cap
        j["poll_failures"] = 0
        if "E13_CHAIN_DONE" in out:
            j["state"] = "done"
        elif "E13_CHAIN_FAILED" in out:
            j["state"] = "failed"
            j["tail"] = out[-600:]
        elif "E13_ORCH_ALIVE=0" in out.replace(" ", "") and \
                self.d.now() - j.get("started_epoch", 0) > 120:
            j["state"] = "failed"  # no chain process and no terminal tag: it died
            j["tail"] = "chain process gone without E13_CHAIN_DONE/FAILED: " + out[-400:]
        self.save()
        return j["state"]

    def wait_job(self, name: str) -> None:
        while True:
            st = self.poll_job(name)
            if st == "done":
                self.emit("job_done", job=name)
                return
            if st == "failed":
                self.pull_recovery()
                raise Stop("pod_step_failed", f"{name}: {self.state['jobs'][name].get('tail')}")
            if not self.d.wait_jobs:
                raise Blocked(f"pod job {name} running")
            self.check_pod_hours()
            started = self.state["jobs"][name].get("started_epoch", self.d.now())
            if self.d.now() - started > MAX_JOB_H * 3600:
                self.pull_recovery()
                raise Stop("pod_job_timeout", f"{name} ran over {MAX_JOB_H} h")
            self.d.sleep(POLL_S)

    def run_pod(self, name: str, steps: list[list[str]], pull: bool = True) -> None:
        self.start_job(name, steps)
        self.wait_job(name)
        if pull:
            self.pull_job(name)

    def pull_job(self, name: str) -> None:
        """Pull and verify a finished job's manifests; mark it pulled (a relaunch reruns any job
        whose results never reached the laptop; code review r2)."""
        j = self.state["jobs"][name]
        if j.get("pulled"):
            return
        self.pull(self.manifests_for(j["steps"]))
        j["pulled"] = utc()
        self.save()

    def pull(self, manifests: list[str]) -> None:
        args = []
        for m in manifests:
            args += ["--manifest", Path(m).name]
        self.sh(["bash", "scripts/pod_pull_results.sh", "--exp", "E13", "--slot", C.SLOT, *args],
                timeout=3600)

    def pull_recovery(self) -> None:
        try:
            self.pull([self.recovery_manifest()])
        except Exception as e:  # noqa: BLE001 - recovery is best effort; teardown follows
            self.emit("recovery_pull_failed", error=f"{type(e).__name__}: {e}"[-400:])

    def commit_state(self, msg: str) -> None:
        """pod_push refuses modified tracked files, and this orchestrator's own event log changes
        on every action: commit the run's tracked state first (2026-10-09, first launch)."""
        self.commit([LOG, "logs/costs.jsonl", C.POD_EVENTS, C.RESULTS, C.REPORT], msg)

    def push(self, *extra: str) -> None:
        self.commit_state("state before a pod push")
        self.sh(["bash", "scripts/pod_push.sh", "--exp", "E13", "--slot", C.SLOT, "--no-setup",
                 *extra], timeout=3600)

    def check_pod_hours(self) -> None:
        from emx.e13 import podhours

        used = podhours.used_hours(podhours.read_events(self.d.root / C.POD_EVENTS))
        if used >= C.POD_HOURS_CAP:
            raise Stop("pod_hours_cap", f"{used:.2f} h used of {C.POD_HOURS_CAP}")

    # -- judging and decisions --------------------------------------------------------------

    def judge(self, *names: str) -> None:
        if self.amend_e():  # the base model's batteries were judged in the first run (reused)
            names = tuple(n for n in names if n.split("__")[1] != C.BASE_KEY)
        if names:
            self.py("e13_judge.py", *names, timeout=4 * 3600)

    def decide(self, *args: str) -> None:
        self.py("e13_decide.py", *args)

    # -- the stages -------------------------------------------------------------------------

    def stage(self, name: str, fn: Callable[[], dict | None]) -> None:
        if self.done(name):
            return
        self.state["current"] = name
        self.save()
        info = fn() or {}
        self.mark(name, **info)

    def st_notices(self) -> dict:
        for sub in ("write", "check", "filter"):
            rc, out = self.d.run([*PY, "scripts/e13_notices.py", sub], 3600)
            self.emit("cmd", cmd=f"e13_notices.py {sub}", rc=rc, tail=out[-400:])
            if rc != 0:
                surv = self.read(f"{C.INPUTS_DIR}/notice_survival.json") or {}
                if sub == "filter" and surv and not surv.get("ok", True):
                    raise Stop("notice_survival", json.dumps(surv.get("per_condition")))
                raise RuntimeError(f"e13_notices.py {sub} failed rc={rc}")
        return {}

    def st_style_notices(self) -> dict:
        """Amendment A notice audit; then (Amendment C) the frozen v1 features on the v2 notices,
        which carries the amendment's prediction."""
        self.py("e13_style.py", "notices")
        if (self.d.root / C.RESULTS / "notices_v1" / "style" / "features_notices.json").exists():
            self.py("e13_style.py", "notices-v1features")
        return {}

    def st_notice_check(self) -> dict:
        rec = self.read(f"{C.LABELS_DIR}/notice/record.json")
        if rec is None:
            if not (self.d.root / C.LABELS_DIR / "notice" / "page.html").exists():
                self.py("e13_label_page.py", "notice")
            self.state["human"]["notice_check"] = {
                "since": utc(), "page": f"{C.LABELS_DIR}/notice/page.html",
                "action": "publish the notice-check page; score with "
                          "scripts/e13_label_score.py notice --labels <json>"}
            self.save()
            raise Blocked("Jacob's notice check (page ready)", human=True)
        bad = self.label_record_problems(rec, "notice")
        if bad:  # code review r1 (laptop finding 7): never trust a stale or partial record
            raise RuntimeError(f"notice-check record not bound to the current files: {bad}")
        nc = rec.get("notice_check") or {}
        if not nc.get("passed"):
            raise Stop("notice_check", json.dumps(nc))
        self.state["human"].pop("notice_check", None)
        return {"notice_check": nc}

    def check_notice_binding(self) -> None:
        """Every advance and relaunch re-checks the notice-check record against the current
        files, before any pod work (code review r2: a stage marked done is not enough)."""
        if not self.done("notice_check"):
            return
        rec = self.read(f"{C.LABELS_DIR}/notice/record.json") or {}
        bad = self.label_record_problems(rec, "notice")
        if bad or not (rec.get("notice_check") or {}).get("passed"):
            raise RuntimeError(f"notice-check record no longer bound or passing: {bad}")

    def label_record_problems(self, rec: dict, kind: str) -> list[str]:
        """A labels record is usable only if complete, of this kind and rule version, and its
        draw, key and every source file still hash as recorded."""
        out = []
        if rec.get("kind") != kind:
            out.append(f"kind {rec.get('kind')!r}")
        if not rec.get("complete") or not rec.get("n_items") or \
                rec.get("n_labeled") != rec.get("n_items"):
            out.append("incomplete")
        if rec.get("rule_version") != C.RULE_VERSION:
            out.append(f"rule version {rec.get('rule_version')!r}")
        d = f"{C.LABELS_DIR}/{kind}"
        for k, f in (("draw_sha256", "draw.json"), ("key_sha256", "key.private.json")):
            if not rec.get(k) or rec.get(k) != self._sha(f"{d}/{f}"):
                out.append(f"{k} differs from {d}/{f}")
        src = rec.get("source_sha256") or {}
        if not src:
            out.append("no source files recorded")
        for path, sha in sorted(src.items()):
            if self._sha(path) != sha:
                out.append(f"source {path} changed")
        return out

    def st_inferred(self) -> dict:
        """Amendment D: write, check and filter the inferred-cue pairs. Fewer than 3 surviving
        pairings drop the battery (recorded by the filter, exit 0): reported, not a stop."""
        for sub in ("write", "check", "filter"):
            self.py("e13_inferred.py", sub, timeout=3600)
        surv = self.read(C.INFERRED_SURVIVAL_PATH)
        if surv is None:
            raise RuntimeError(f"{C.INFERRED_SURVIVAL_PATH} missing after e13_inferred.py filter")
        if surv.get("dropped"):
            self.emit("inferred_dropped", n_pairings_ok=surv.get("n_pairings_ok"))
        return {"dropped": bool(surv.get("dropped")), "n_pairings_ok": surv.get("n_pairings_ok")}

    def inferred_dropped(self) -> bool:
        return bool((self.read(C.INFERRED_SURVIVAL_PATH) or {}).get("dropped"))

    def rest_batteries(self) -> tuple[str, ...]:
        """The organism batteries generated after dev: Amendment D's inferred one last, unless
        its survival record says dropped."""
        return ("narrow", "infam", "final", *(() if self.inferred_dropped() else ("inferred",)))

    def st_inputs(self) -> dict:
        self.py("e13_style.py", "answers")
        self.py("e13_inputs.py", "build", "--require-tokenizer")
        self.py("e13_inputs.py", "manifest", "--require-style",
                *(() if self.inferred_dropped() else ("--require-inferred",)))
        self.commit([C.INPUTS_DIR, C.STYLE_DIR, C.LABELS_DIR, f"{C.RESULTS}/MANIFEST.laptop.*"],
                    "input manifest, notices and style feature lists (before training)")
        return {"manifest_sha256": self._sha(C.INPUT_MANIFEST)}

    def _sha(self, rel: str) -> str | None:
        import hashlib

        p = self.d.root / rel
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None

    def ensure_watchdog(self) -> None:
        """pod_up.py refuses an E13 launch without a live laptop watchdog: start one (under
        caffeinate, so idle sleep cannot pause it) and wait for its first heartbeat."""
        from emx import runpod as rp

        rp.configure(C.EXP)
        if rp.watchdog_live(C.EXP)[0]:
            return
        start = (f"nohup caffeinate -is bash scripts/e13_watchdog.sh {WATCHDOG_INTERVAL_S} "
                 f">> logs/watchdog_E13.log 2>&1 &")
        self.sh(["bash", "-c", start], timeout=30)
        for _ in range(30):
            self.d.sleep(2)
            if rp.watchdog_live(C.EXP)[0]:
                self.emit("watchdog_started")
                return
        raise RuntimeError("E13 watchdog did not start (no live heartbeat after 60 s)")

    def st_launch(self, stage: str = "both") -> dict:
        usd = (sum(C.POD_RESERVATION_USD.values()) if stage == "both"
               else C.POD_RESERVATION_USD[stage])
        push_extra: list[str] = []
        if self.amend_e():
            # Amendment E: a new pod under the patch-stage reservation, capped by what is left
            # of the GPU line (pod_up refuses a budget past it); the reused pod records restored
            stage = "patch"
            gpu, _api, _h = self.spent()
            usd = min(C.POD_RESERVATION_USD["patch"], round(C.GPU_LIMIT_USD - gpu - 0.5, 2))
            if usd <= 0:
                raise Stop("gpu_line", f"${gpu:.2f} of ${C.GPU_LIMIT_USD} GPU spent")
            push_extra = ["--restore-only", ",".join(RESTORE_E)]
        self.ensure_watchdog()
        pod = self.state.get("pod")
        if not pod or pod.get("down"):  # a retry after a failed push never launches a 2nd pod
            out = self.sh([*PY, "-m", "emx.runpod_reservations", "reserve", "--exp", C.EXP,
                           "--usd", f"{usd:.2f}", "--note", f"E13 pod ({stage})"])
            rid = out.strip().splitlines()[-1].strip()
            self.state["pod"] = {"reservation": rid, "usd": usd, "stage": stage,
                                 "reserved": utc()}
            self.save()
            self.sh([*PY, "scripts/pod_up.py", "--exp", C.EXP, "--slot", C.SLOT,
                     "--stage", "stage1" if stage in ("both", "stage1") else "patch",
                     "--reservation", rid, "--budget-usd", f"{usd:.2f}"], timeout=3600)
            self.state["pod"]["launched"] = utc()
            self.save()
        self.commit_state("state before the first pod push")
        self.sh(["bash", "scripts/pod_push.sh", "--exp", C.EXP, "--slot", C.SLOT, *push_extra],
                timeout=5400)
        return {"reservation": self.state["pod"]["reservation"], "usd": usd}

    def st_plumbing(self) -> dict:
        k = C.TRIG_KEY
        self.judge(C.gen_name("trigger", k), C.gen_name("narrow_trig", k))
        self.decide("plumbing")
        rec = self.decision("plumbing")
        self.commit([C.decisions_dir(None)], "plumbing decision")
        if rec["outcome"] != "pass":
            raise Stop("plumbing", f"diff {rec.get('diff')}")
        return {"outcome": rec["outcome"]}

    def pod_steps_done(self, recipe: str) -> list[str]:
        """Pod step names (the projection's vocabulary; code review r1, laptop finding 3) whose
        work is finished for `recipe`: the r1-only steps once pod_a ran, and the recipe's own
        training, adapter smoke and organism batteries only when done for that recipe."""
        done: list[str] = []
        if self.done("pod_a"):
            done += STAGE_POD_STEPS["pod_a"]
        if self.done("pod_b"):
            done.append("bench-hooked")
            if recipe == "r1":
                done += ["train", "smoke-adapters"]
        if recipe == "r32" and self.done("pod_r32"):
            done += ["train", "smoke-adapters"]
        if recipe in C.AMENDMENT_E_RECIPES and self.done("train_tan32"):
            done += ["train", "smoke-adapters"]
        # generation and its judging are one line item in the projection, so the organism
        # batteries count as done only once judged (code review r2: never drop unpaid judging)
        if self.state["stages"].get("organism_judged", {}).get("recipe") == recipe:
            done.append("gen-organism")
        for st, steps in (("direction", ["capture", "direction", "hook-check"]),
                          ("sweep", ["sweep"]), ("final_patch", ["final-patch"])):
            if self.done(st):
                done += steps
        return sorted(set(done))

    def projection_state(self, **kw) -> dict:
        lc = self.read(f"{C.BENCH_DIR}/lora_check.json") or {}
        recipe = kw.pop("recipe", self.state.get("recipe", "r1"))
        st = {"recipe": recipe, "organism_backend": lc.get("organism_backend", "vllm"),
              "steps_done": self.pod_steps_done(recipe), "fallback_pending": False}
        if self.state.get("live_sites") is not None:
            st["sites"] = list(self.state["live_sites"])
        st.update(kw)
        return st

    def spent(self) -> tuple[float, float, float]:
        from emx import spend
        from emx.e13 import podhours

        t = spend.totals(C.EXP)
        hours = podhours.used_hours(podhours.read_events(self.d.root / C.POD_EVENTS))
        # running pod: count its hours at the H100 rate (pod_down settles the real charge later)
        gpu = max(t["gpu_usd"], hours * C.H100_USD_H)
        return gpu, t["judge_usd"] + t["judge_reserved_usd"], hours

    def st_projection(self, a8_drop_sites: list[str] | None = None, **state_kw) -> dict:
        gpu, api, hours = self.spent()
        extra = [] if a8_drop_sites is None else ["--a8-drop-sites", *a8_drop_sites]
        self.decide("projection", "--state", json.dumps(self.projection_state(**state_kw)),
                    "--spent-gpu", f"{gpu:.4f}", "--spent-api", f"{api:.4f}",
                    "--pod-hours", f"{hours:.4f}", *extra)
        rec = self.decision("projection")
        self.commit([C.decisions_dir(self.rr())], f"projection ({rec['outcome']})")
        if rec["outcome"] == "stop":
            raise Stop("projection", json.dumps(rec.get("usd")))
        # "needs_probe": only dropping alpha 8 (cut ii, resolved by the sweep-time probe) can
        # make it fit; the probe then re-projects with every cut recomputed from scratch
        self.state["cuts"] = rec.get("cuts", [])
        if self.state.get("pod") and not self.state["pod"].get("down"):
            self.push()
        return {"outcome": rec["outcome"], "cuts": rec.get("cuts", [])}

    def organism_steps(self, recipe: str, batteries: tuple[str, ...]) -> list[list[str]]:
        args = ["gen-organism", "--recipe", recipe]
        for b in batteries:
            args += ["--battery", b]
        return [args]

    def gen_names(self, battery: str, recipe: str) -> list[str]:
        b = C.BATTERIES[battery]
        keys = []
        if "base" in b.arms:
            keys.append(C.BASE_KEY)
        if "good" in b.arms:
            keys.append(C.model_key("good", recipe, 0))
        if "org" in b.arms:
            keys += [C.model_key("org", recipe, s) for s in C.TRAIN_SEEDS["org"]]
        return [C.gen_name(battery, k) for k in keys]

    def st_dev_decision(self) -> dict:
        recipe = RECIPE_E if self.amend_e() else "r1"  # Amendment E: no further fallback
        self.judge(*self.gen_names("dev", recipe))
        self.decide("dev", "--recipe", recipe)
        rec = self.decision("dev_decision")
        self.commit([C.decisions_dir(self.rr())],
                    f"development decision {recipe} ({rec['outcome']})")
        if rec["outcome"] == "fallback" and self.amend_e():
            raise RuntimeError("Amendment E: tan32 has no fallback recipe")
        if rec["outcome"] == "fallback":
            self.emit("fallback", per_seed=rec.get("per_seed"))
            self.wait_job("gen_rest_r1")
            self.pull_job("gen_rest_r1")
            recipe = "r32"
            self.state["recipe"] = recipe
            self.save()
            self.st_projection(recipe="r32")
            self.run_pod("pod_r32", [["train", "--recipe", recipe],
                                     ["smoke-adapters", "--recipe", recipe],
                                     *self.organism_steps(recipe, ("dev",))])
            self.judge(*self.gen_names("dev", recipe))
            self.decide("dev", "--recipe", recipe)
            rec = self.decision("dev_decision")
            self.commit([C.DECISIONS_DIR], f"development decision {recipe} ({rec['outcome']})")
            self.start_job("gen_rest_r32", self.organism_steps(recipe, self.rest_batteries()))
        self.state["recipe"] = recipe
        self.state["patch_seed"] = rec.get("patch_seed")
        self.state["dev_outcome"] = rec["outcome"]
        return {"recipe": recipe, "outcome": rec["outcome"], "patch_seed": rec.get("patch_seed")}

    def rest_job(self) -> str:
        return f"gen_rest_{self.state.get('recipe', 'r1')}"

    def pull_organism_batteries(self) -> None:
        """Pod part: wait for the other organism batteries and pull them (verified)."""
        if self.done("organism_judged"):
            return
        self.wait_job(self.rest_job())
        self.pull_job(self.rest_job())

    def judge_organism_batteries(self) -> None:
        """Laptop part, from pulled files only: judge narrow, infam, final and (Amendment D,
        unless dropped) inferred; rate the final answers' style (Amendment A; locked until the
        labels)."""
        if self.done("organism_judged"):
            return
        recipe = self.state.get("recipe", "r1")
        if not self.state["jobs"].get(self.rest_job(), {}).get("pulled"):
            raise RuntimeError("organism batteries were never pulled; nothing to judge")
        names = [n for b in self.rest_batteries() for n in self.gen_names(b, recipe)]
        self.judge(*names)
        self.py("e13_style.py", "organism", "--recipe", recipe, timeout=4 * 3600)
        self.mark("organism_judged", recipe=recipe)

    def finish_organism_batteries(self) -> None:
        self.pull_organism_batteries()
        self.judge_organism_batteries()

    def st_dirsource(self) -> dict:
        k = self.state["patch_seed"]
        self.wait_job(self.rest_job())
        self.run_pod("dirsrc", [["gen-dirsource", "--seed-key", k]])
        self.judge(C.gen_name("dirsrc", k))
        self.decide("dirsource", "--key", k)
        rec = self.decision("dirsource_labels")
        extended = False
        if rec["outcome"] == "extend":
            rec = self.extend_dirsource("cell_rule")
            extended = True
        self.commit([C.decisions_dir(self.rr())], "direction-source labels")
        if rec["outcome"] != "ok":  # prereg §4.3 step 3: too few eligible cells after extending
            raise Stop("direction_cells", json.dumps(rec.get("counts"))[:600])
        self.push()
        return {"extended": extended}

    def extend_dirsource(self, why: str) -> dict:
        """The one extension of every direction-source cell (to 60, or 120 under Amendment E),
        judged and relabelled from all answers. The state records the request first, then each
        finished step (code review amendE-r1): a resumed run continues the same extension, whose
        steps are idempotent (the pod job by its name and manifests, judging from its CSV, the
        relabelling deterministic); a second, different extension is refused."""
        k = self.state["patch_seed"]
        ext = self.state.get("dirsrc_extended")
        if ext is None:
            ext = self.state["dirsrc_extended"] = {"why": why, "utc": utc()}
            self.save()
            self.emit("dirsrc_extend", why=why)
        elif ext["why"] != why:
            raise RuntimeError(f"the direction source was already extended once ({ext['why']})")
        else:
            self.emit("dirsrc_extend_resume", why=why, done=sorted(x for x in ext
                                                                   if x not in ("why", "utc")))
        self.run_pod("dirsrc_ext", [["gen-dirsource", "--seed-key", k, "--extend"]])
        ext.setdefault("generated", utc())
        self.judge(C.gen_name("dirsrc", k, "ext"))
        ext.setdefault("judged", utc())
        self.decide("dirsource", "--key", k, "--extended")
        ext["relabelled"] = utc()
        self.save()
        return self.decision("dirsource_labels")

    def ext_pending(self) -> bool:
        """An Amendment E extension requested after the direction step whose 120-answer
        directions are not yet recomputed and pulled."""
        ext = self.state.get("dirsrc_extended") or {}
        return str(ext.get("why", "")).startswith("direction:") and not ext.get("recomputed")

    def st_direction(self) -> dict:
        k = self.state["patch_seed"]
        rr = self.rr()
        self.run_pod("direction", [["capture", "--seed-key", k], ["direction", "--seed-key", k]])
        extended_here = False
        why_ext = None
        if self.amend_e() and self.ext_pending():  # resuming an interrupted extension
            why_ext = self.state["dirsrc_extended"]["why"]
        elif self.amend_e() and not self.state.get("dirsrc_extended"):
            failed = {s: (self.read(C.direction_path(s, rr)) or {}).get("stop_reason") or "missing"
                      for s in C.SITE_NAMES
                      if not (self.read(C.direction_path(s, rr)) or {}).get("passed")}
            if failed:
                why_ext = "direction:" + ",".join(f"{s}={r}" for s, r in sorted(failed.items()))
        if why_ext:
            # Amendment E item 4: ONE shared extension when the cell rule, the norm floor or
            # reliability fails at either site; both sites recomputed from all 120 answers per
            # cell, superseding the 60-answer result (kept under AT60_DIR)
            self.keep_at60()
            rec = self.extend_dirsource(why_ext)
            self.commit([C.decisions_dir(rr), C.direction_dir(rr)],
                        "direction-source extension (Amendment E)")
            if rec["outcome"] != "ok":
                raise Stop("direction_cells", "after the shared extension: "
                           + json.dumps(rec.get("counts"))[:500])
            self.push()
            self.run_pod("direction_ext", [["capture", "--seed-key", k],
                                           ["direction", "--seed-key", k]])
            self.state["dirsrc_extended"]["recomputed"] = utc()
            self.save()
            extended_here = True
        if self.ext_pending():  # never hook-check or sweep a superseded 60-answer direction
            raise RuntimeError("the shared extension's 120-answer directions were not recomputed")
        # hook checks only where the direction passed: a failed site is stopped by its direction
        # (Amendment B item 2) and the driver refuses a hook check on it (2026-10-09 run: that
        # refusal was misread as a pod failure)
        passed = [s for s in C.SITE_NAMES
                  if (self.read(C.direction_path(s, rr)) or {}).get("passed")]
        if passed:
            self.run_pod("hook_check", [["hook-check", "--site", s, "--seed-key", k]
                                        for s in passed])
        live, why = [], {}
        for s in C.SITE_NAMES:
            dr = self.read(C.direction_path(s, rr)) or {}
            hk = self.read(f"{C.hooks_dir(rr)}/hook_check_{s}.json") or {}
            if dr.get("passed") and hk.get("passed"):
                live.append(s)
            else:
                why[s] = {"direction": dr.get("stop_reason") or dr.get("passed"),
                          "hooks": hk.get("passed")}
        self.state["live_sites"] = live
        self.state["stopped_sites"] = why
        self.commit([C.direction_dir(rr), C.hooks_dir(rr)], "directions and hook checks")
        return {"live_sites": live, "stopped": why,
                **({"extended_after_direction": True} if extended_here else {})}

    def keep_at60(self) -> None:
        """Copy the 60-answer direction records and labels aside before the extension
        supersedes them (Amendment E item 4; the pull would otherwise only move them to
        superseded/)."""
        import shutil

        rr = self.rr()
        dest = self.d.root / AT60_DIR
        if self.state.get("at60_kept"):
            return  # resumed: the 60-answer records were kept before the extension began
        dest.mkdir(parents=True, exist_ok=True)
        for rel in (*(C.direction_path(s, rr) for s in C.SITE_NAMES),
                    C.decision_path("dirsource_labels", rr)):
            src = self.d.root / rel
            if src.exists():
                shutil.copy2(src, dest / Path(rel).name)
        self.state["at60_kept"] = utc()
        self.save()

    def batch_size(self) -> int:
        b = self.read(f"{C.BENCH_DIR}/hooked.json") or {}
        rows = b.get("by_batch") or b.get("per_batch_size") or {}
        best, best_rate = C.BENCH_BATCH_SIZES[0], -1.0
        items = rows.items() if isinstance(rows, dict) else [(r.get("batch_size"), r)
                                                             for r in rows]
        for bs, r in items:
            rate = float((r or {}).get("answers_per_s") or 0)
            if rate > best_rate:
                best, best_rate = int(bs), rate
        return best

    def sweep_args(self) -> list[str]:
        k, cuts = self.state["patch_seed"], set(self.state.get("cuts", []))
        live = self.state.get("live_sites", [])
        args = ["sweep", "--seed-key", k, "--batch-size", str(self.batch_size()),
                "--sites", ",".join(live)]
        a8_drop = set(self.state.get("a8_drop_sites", []))
        for s in live:
            al = [a for a in C.ALPHAS if a > 0 and not (a == 8.0 and s in a8_drop)]
            args += [f"--alphas-{s}", ",".join(str(a) for a in al)]
        if "dev_utility_3" in cuts:
            args += ["--utility-samples", "3"]
        if "dev_em_10" in cuts:
            args += ["--em-samples", "10"]
        return args

    def first_dev_qids(self) -> list[str]:
        lines = (self.d.root / C.QUESTION_FILES["general"]).read_text().splitlines()
        return [json.loads(x)["qid"] for x in lines if x.strip()][:C.CUT_A8_PROBE_QUESTIONS]

    def a8_probe(self) -> None:
        """Cut 2 (Amendment B item 10 (ii)): alpha 4 and 0 on the first 2 development questions;
        where alpha 4 already fails an EM-question screen, alpha 8 is dropped at that site. The
        probe's rows are the sweep's own rows (same conditions and seeds; the full sweep resumes
        past them); its judgments are set aside, because the judged CSV is bound to its source."""
        k, live = self.state["patch_seed"], self.state["live_sites"]
        # 10 EM samples, the dev_em_10 count: rows are keyed by sample_idx with seeds from
        # request_seed, so they are a prefix of the full sweep at either 10 or 20 samples and
        # never exceed what the final plan requests (code review r2)
        base = ["sweep", "--seed-key", k, "--batch-size", str(self.batch_size()),
                "--sites", ",".join(live), "--qids", ",".join(self.first_dev_qids()),
                "--em-samples", str(C.CUT_SAMPLES["dev_em_10"][2])]
        for site in live:
            base += [f"--alphas-{site}", "4.0"]
        self.run_pod("sweep_probe", [base], pull=False)
        self.pull([self.recovery_manifest()])
        names = [C.gen_name("sweep", k, "a0"), *(C.gen_name("sweep", k, s) for s in live)]
        self.judge(*names)
        drop = []
        for site in live:
            out = self.py("e13_decide.py", "a8-probe", "--key", k, "--site", site)
            rec = json.loads(out[out.index("{"):out.rindex("}") + 1])
            if rec.get("drop_a8"):
                drop.append(site)
        aside = self.d.root / C.RESULTS / "superseded" / f"a8_probe_{utc()}"
        aside.mkdir(parents=True, exist_ok=True)
        for n in names:
            for p in (self.d.root / C.JUDGED_DIR).glob(f"{n}.*"):
                p.rename(aside / p.name)
        self.state["a8_drop_sites"] = drop
        self.save()
        self.emit("a8_probe", drop_sites=drop)
        self.st_projection(a8_drop_sites=drop)

    def st_sweep(self) -> dict:
        if not self.state.get("live_sites"):
            raise Stop("no_live_site", json.dumps(self.state.get("stopped_sites")))
        if "drop_a8_where_a4_fails" in self.state.get("cuts", []) and \
                "a8_drop_sites" not in self.state:
            self.a8_probe()
        args = self.sweep_args()
        self.start_job("sweep", [args])
        self.finish_organism_batteries()  # laptop judging overlaps the sweep
        self.wait_job("sweep")
        self.pull_job("sweep")
        return {"args": args}

    def st_sweep_judged(self) -> dict:
        k = self.state["patch_seed"]
        names = [C.gen_name("sweep", k, "a0")]
        names += [C.gen_name("sweep", k, s) for s in self.state["live_sites"]]
        self.judge(*names)
        return {"names": names}

    def st_strength(self) -> dict:
        k = self.state["patch_seed"]
        self.decide("consistency", "--key", k)
        cons = self.decision("consistency")
        if cons["outcome"] != "pass":
            self.commit([C.decisions_dir(self.rr())], "unsteered consistency failed")
            raise Stop("unsteered_consistency", json.dumps(cons.get("diffs", cons.get("per"))))
        chosen = {}
        for s in self.state["live_sites"]:
            self.decide("strength", "--key", k, "--site", s)
            rec = self.decision(f"strength_{s}")
            if rec["outcome"] == "stop_dev_gap":
                self.commit([C.decisions_dir(self.rr())], "development gap below 5 points")
                raise Stop("dev_gap", f"{rec.get('dev_gap')}")
            chosen[s] = {g: rec["per_gate"][g]["alpha"] for g in C.GATES}
        self.state["strengths"] = chosen
        self.commit([C.decisions_dir(self.rr())], "consistency and strength choices")
        return {"strengths": chosen}

    def final_cells(self) -> str:
        cells = ["none:ov:0", "none:noov:0"]
        for s, g in self.state["strengths"].items():
            aa, ao = g.get("always"), g.get("oracle")
            if aa is not None:
                cells += [f"{s}:ov:{aa}", f"{s}:noov:{aa}"]
            if ao is not None:
                cells.append(f"{s}:noov:{ao}")
                if ao != aa:  # matched-strength contrast: always-on at alpha_O under oversight
                    cells.append(f"{s}:ov:{ao}")
        return ",".join(dict.fromkeys(cells))

    def st_label_pass(self) -> dict:
        # prereg §4.4: the full pass once the strength sweep has been judged, else the early-stop
        # pass (code review r1, laptop finding 10)
        kind = "full" if self.done("sweep_judged") else "early"
        args = [kind, "--recipe", self.state.get("recipe", "r1")]
        if self.state.get("patch_seed"):
            args += ["--patch-key", self.state["patch_seed"]]
        for s in C.SITE_NAMES:
            if kind == "full" and s not in self.state.get("live_sites", []):
                args += ["--stopped-site", s]  # its B/C/D items go to the other site
        ldir = C.labels_dir(self.rr())  # Amendment E: the retry's own pass
        if not (self.d.root / ldir / kind / "page.html").exists():
            self.py("e13_label_page.py", *args)
        self.state["label_kind"] = kind
        rflag = f" --recipe {RECIPE_E}" if self.amend_e() else ""
        self.state["human"]["labels"] = {
            "since": utc(), "page": f"{ldir}/{kind}/page.html",
            "action": f"publish the {kind} labelling page; score with "
                      f"scripts/e13_label_score.py {kind} --labels <json>{rflag}"}
        return {"kind": kind}

    def st_final_patch(self) -> dict:
        k = self.state["patch_seed"]
        cells = self.final_cells()
        args = ["final-patch", "--seed-key", k, "--batch-size", str(self.batch_size()),
                "--cells", cells]
        if "final_em_10" in set(self.state.get("cuts", [])):
            args += ["--em-samples", "10"]
        self.run_pod("final_patch", [args])
        parts = ["a0", *sorted({c.split(":")[0] for c in cells.split(",")} - {"none"})]
        self.judge(*[C.gen_name("fpatch", k, p) for p in parts])
        return {"cells": cells}

    def st_teardown(self, closing: bool = True) -> dict:
        """Pull, then terminate. Termination is in a `finally` (code review r3): no failure of the
        closing step or the pulls (a timeout, an unreachable pod, a failed manifest) can skip
        `pod_down`. Every step's manifests were already pulled after its job; the closing
        manifest re-verifies them and is only tried when the run ended normally."""
        if self.state.get("pod") and not self.state["pod"].get("down"):
            try:
                try:
                    if not closing or not self.d.wait_jobs:
                        raise RuntimeError("recovery pull only")
                    self.run_pod("closing", [["manifest"]], pull=False)
                    # the closing manifest (the default; the retry's own by name)
                    self.pull(["MANIFEST.manifest.run2.sha256"] if self.amend_e() else [])
                except Exception as e:  # noqa: BLE001 - every failure falls back to recovery
                    self.emit("closing_skipped", reason=f"{type(e).__name__}: {e}"[-400:])
                    self.pull_recovery()
            finally:
                self.sh([*PY, "scripts/pod_down.py", "--exp", C.EXP, "--slot", C.SLOT,
                         "--reason", "e13_done"], timeout=1800)
                self.state["pod"]["down"] = utc()
                self.save()
        self.commit([C.RESULTS, C.REPORT, LOG], "pod stage results pulled and verified")
        return {}

    def st_analysis(self) -> dict:
        kind = self.state.get("label_kind", "early")
        rec = self.read(f"{C.labels_dir(self.rr())}/{kind}/record.json")
        if rec is None or not rec.get("complete"):
            raise Blocked(f"Jacob's {kind} labelling pass", human=True)
        self.py("e13_analyze.py", "--kind", kind,
                *(("--recipe", RECIPE_E) if self.amend_e() else ()))
        self.state["human"].pop("labels", None)
        self.commit([C.RESULTS], "analysis")
        return {"kind": kind}

    def relaunch(self) -> int:
        """Replacement pod (plan §7 "Pod lost"): settle the old pod, check the GPU line and the
        pod-hours, launch a new pod with the patch-stage reservation, push with --restore (the
        locally verified artifacts and adapters), and forget the interrupted job so the next
        `advance` restarts it (pod steps skip finished work by their manifests and deps)."""
        self.load()
        if self.state.get("stopped"):
            self.emit("relaunch_refused", reason="run stopped")
            return STOP_RC
        self.check_notice_binding()
        self.pull_recovery()
        self.sh([*PY, "scripts/pod_down.py", "--exp", C.EXP, "--slot", C.SLOT,
                 "--reason", "e13_relaunch"], timeout=1800)
        gpu, _api, hours = self.spent()
        if gpu >= C.GPU_LIMIT_USD or hours >= C.POD_HOURS_CAP:
            self.state["stopped"] = {"rule": "relaunch_over_cap", "utc": utc(),
                                     "detail": f"gpu ${gpu:.2f}, {hours:.2f} pod-h"}
            self.save()
            return STOP_RC
        old = self.state.get("pod", {})
        self.state.setdefault("lost_pods", []).append({**old, "down": old.get("down") or utc()})
        # every job whose results never reached the laptop reruns on the replacement (steps it
        # already finished skip by manifest + deps after the --restore push); persisted before
        # anything is launched (code review r3: relaunch is transactional)
        for name, j in self.state["jobs"].items():
            if not j.get("pulled") and name != "closing":
                j["state"] = "interrupted"
        # never past the GPU line: pod_up refuses a budget beyond it (as the tan32 launch clamps)
        usd = min(C.POD_RESERVATION_USD["patch"], round(C.GPU_LIMIT_USD - gpu - 0.5, 2))
        self.ensure_watchdog()
        out = self.sh([*PY, "-m", "emx.runpod_reservations", "reserve", "--exp", C.EXP,
                       "--usd", f"{usd:.2f}", "--note", "E13 replacement pod"])
        rid = out.strip().splitlines()[-1].strip()
        self.state["pod"] = {"reservation": rid, "usd": usd, "launched": utc(),
                             "stage": "patch", "replacement": True, "launching": True}
        self.save()
        try:
            self.sh([*PY, "scripts/pod_up.py", "--exp", C.EXP, "--slot", C.SLOT, "--stage",
                     "patch", "--reservation", rid, "--budget-usd", f"{usd:.2f}"], timeout=3600)
            self.commit_state("state before the replacement pod push")
            self.sh(["bash", "scripts/pod_push.sh", "--exp", C.EXP, "--slot", C.SLOT,
                     "--restore"], timeout=5400)
        except Exception:
            # a failed launch or restore must not leave the replacement billing
            try:
                self.sh([*PY, "scripts/pod_down.py", "--exp", C.EXP, "--slot", C.SLOT,
                         "--reason", "e13_relaunch_failed"], timeout=1800)
            finally:
                self.state["pod"]["down"] = utc()
                self.save()
            raise
        self.state["pod"].pop("launching", None)
        self.save()
        self.emit("relaunched", reservation=rid)
        return 0

    # -- the walk ---------------------------------------------------------------------------

    def advance(self) -> int:
        self.load()
        try:
            if self.state.get("stopped"):
                # code review r1 (laptop finding 6): the scientific stop stands, but cleanup,
                # the labelling pass and the analysis stay resumable (no new generation)
                self.after_stop()
                self.print_status()
                return STOP_RC
            self.check_notice_binding()
            self.restart_interrupted()
            self.walk()
        except Blocked as b:
            self.state["blocked"] = {"reason": b.reason, "human": b.human, "utc": utc()}
            self.emit("blocked", reason=b.reason, human=b.human)
            self.save()
            self.print_status()
            return BLOCKED_RC
        except Stop as s:
            self.on_stop(s)
            self.print_status()
            return STOP_RC
        except Exception as e:  # noqa: BLE001 - an unplanned error: record it, leave the pod up
            # (the watchdog and killswitch still cap it); the main session diagnoses and re-runs
            self.state["error"] = {"utc": utc(), "stage": self.state.get("current"),
                                   "error": f"{type(e).__name__}: {e}"[-2000:]}
            self.save()
            self.emit("error", **self.state["error"])
            self.print_status()
            return 1
        self.state.pop("error", None)
        self.save()
        self.print_status()
        return 0

    def walk(self) -> None:
        if self.amend_e():
            self.walk_e()
            return
        S = self.stage
        S("notices", self.st_notices)
        S("style_notices", self.st_style_notices)
        S("notice_check", self.st_notice_check)
        S("inferred", self.st_inferred)
        S("inputs", self.st_inputs)
        S("launch", self.st_launch)
        S("pod_a", lambda: self.run_pod("pod_a", [
            ["download"], ["smoke"], ["bench-train"], ["train-trigger"], ["lora-check"],
            ["gen-trigger"]]))
        S("plumbing", self.st_plumbing)
        S("pod_b", lambda: self.run_pod("pod_b", [
            ["train", "--recipe", "r1"], ["smoke-adapters", "--recipe", "r1"],
            ["bench-hooked", "--recipe", "r1"]]))
        S("projection", self.st_projection)
        S("gen_dev", lambda: (self.run_pod("gen_dev", self.organism_steps("r1", ("dev",))),
                              self.start_job("gen_rest_r1", self.organism_steps(
                                  "r1", self.rest_batteries())), {})[2])
        S("dev_decision", self.st_dev_decision)
        if self.state.get("dev_outcome") == "no_patch":
            S("organism_batteries", lambda: (self.finish_organism_batteries(), {})[1])
            S("label_pass", self.st_label_pass)
            S("teardown", self.st_teardown)
            S("analysis", self.st_analysis)
            return
        S("dirsource", self.st_dirsource)
        S("direction", self.st_direction)
        S("sweep", self.st_sweep)
        S("sweep_judged", self.st_sweep_judged)
        S("strength", self.st_strength)
        S("label_pass", self.st_label_pass)
        S("final_patch", self.st_final_patch)
        S("teardown", self.st_teardown)
        S("analysis", self.st_analysis)

    def walk_e(self) -> None:
        """Amendment E's second phase (module docstring); the reused stages are already done."""
        S, R_ = self.stage, RECIPE_E
        missing = [st for st in REUSED_STAGES_E if not self.done(st)]
        if missing:
            raise RuntimeError(f"Amendment E phase without the reused stages {missing}")
        S("launch", self.st_launch)
        S("train_tan32", lambda: (
            self.run_pod("setup_e", [["download"]], pull=False),  # input check; nothing pulled
            self.run_pod("train_tan32", [["train", "--recipe", R_],
                                         ["smoke-adapters", "--recipe", R_]]), {})[2])
        S("projection", self.st_projection)
        S("gen_dev", lambda: (self.run_pod("gen_dev", self.organism_steps(R_, ("dev",))),
                              self.start_job(f"gen_rest_{R_}", self.organism_steps(
                                  R_, self.rest_batteries())), {})[2])
        S("dev_decision", self.st_dev_decision)
        if self.state.get("dev_outcome") == "no_patch":
            S("organism_batteries", lambda: (self.finish_organism_batteries(), {})[1])
            S("label_pass", self.st_label_pass)
            S("teardown", self.st_teardown)
            S("analysis", self.st_analysis)
            return
        S("dirsource", self.st_dirsource)
        S("direction", self.st_direction)
        S("sweep", self.st_sweep)
        S("sweep_judged", self.st_sweep_judged)
        S("strength", self.st_strength)
        S("label_pass", self.st_label_pass)
        S("final_patch", self.st_final_patch)
        S("teardown", self.st_teardown)
        S("analysis", self.st_analysis)

    def start_amend_e(self) -> int:
        """`amend-e`: archive the first run's state to RUN1_STATUS_PATH and reset to Amendment E's
        phase (module docstring). Refuses while the first run is still live (not stopped and not
        analysed, or its pod not down); a second call after the switch does nothing."""
        self.load()
        if self.amend_e():
            self.emit("amend_e_noop", reason="already in the Amendment E phase")
            return 0
        arch = self.d.root / C.RUN1_STATUS_PATH
        if arch.exists():
            raise RuntimeError(f"{C.RUN1_STATUS_PATH} exists but the state is not Amendment E's")
        old = self.state
        if not (old.get("stopped") or self.done("analysis")):
            raise RuntimeError("the first run is neither stopped nor analysed")
        if old.get("pod") and not old["pod"].get("down"):
            raise RuntimeError("the first run's pod is not down; tear it down first")
        missing = [st for st in REUSED_STAGES_E if not self.done(st)]
        if missing:
            raise RuntimeError(f"the first run lacks the stages Amendment E reuses: {missing}")
        self.check_notice_binding()
        arch.parent.mkdir(parents=True, exist_ok=True)
        arch.write_text(json.dumps(old, indent=2, sort_keys=True) + "\n")
        self.state = {
            "phase": AMEND_E, "recipe": RECIPE_E, "cuts": [], "jobs": {}, "human": {},
            "amendments": [*old.get("amendments", []),
                           {"id": "E", "utc": utc(), "note": "second organism (tan32), luna "
                            "deciding, 60/120-answer direction source; first run archived to "
                            + C.RUN1_STATUS_PATH}],
            "run1": {"status": C.RUN1_STATUS_PATH, "stopped": old.get("stopped"),
                     "recipe": old.get("recipe"), "analysis": old["stages"].get("analysis")},
            "stages": {st: {**old["stages"][st], "reused_from_run1": True}
                       for st in REUSED_STAGES_E},
        }
        self.save()
        self.emit("amend_e", archived=C.RUN1_STATUS_PATH, reused=list(REUSED_STAGES_E))
        self.commit([C.RUN1_STATUS_PATH, C.STATUS_PATH, LOG],
                    "Amendment E: second phase from the stopped first run")
        return 0

    def on_stop(self, s: Stop) -> None:
        """Record the stop, then run the post-stop path (`after_stop`)."""
        self.state["stopped"] = {"rule": s.rule, "detail": s.detail, "utc": utc(),
                                 "stage": self.state.get("current")}
        self.save()
        self.emit("stop", rule=s.rule, detail=s.detail)
        try:
            self.after_stop()
        except Blocked as b:
            self.emit("blocked", reason=b.reason, human=b.human)
        except (Stop, RuntimeError) as e:
            self.emit("after_stop_incomplete", error=str(e)[-400:])
        self.save()

    def after_stop(self) -> None:
        """What the stop rules still require, each an idempotent stage, so an interrupted
        cleanup is retried and the labels lead to the analysis: the organism batteries are
        judged when they were generated (not after a pod failure, the pod-hour cap or a
        projection stop), the labelling pass is drawn (full if the sweep was judged, else the
        early-stop pass), the pod's results are pulled and the pod terminated, then the
        analysis waits for the labels. No new patch generation is started."""
        rule = self.state["stopped"]["rule"]
        pod_failure = rule in ("pod_step_failed", "pod_job_timeout", "pod_hours_cap")
        pod_live = bool(self.state.get("pod")) and not self.state["pod"].get("down")
        want_org = self.done("gen_dev") and not pod_failure and rule != "projection"
        # 1. pod work first, and teardown regardless of how it went (code review r2: a laptop
        #    follow-up failure must never strand a billing pod)
        if pod_live:
            try:
                if want_org and self.state["jobs"].get(self.rest_job()):
                    self.pull_organism_batteries()
            except Blocked as b:
                if not b.human:
                    raise  # the batteries are still generating: come back, pod stays up
                self.emit("stop_followup_failed", what="organism batteries", error=b.reason)
            except Exception as e:  # noqa: BLE001 - never let a follow-up skip the teardown
                self.emit("stop_followup_failed", what="organism batteries",
                          error=f"{type(e).__name__}: {e}"[-400:])
            self.stage("teardown", lambda: self.st_teardown(closing=not pod_failure))
        # 2. laptop work from pulled files only; each resumable
        if want_org and self.state["jobs"].get(self.rest_job(), {}).get("pulled"):
            self.judge_organism_batteries()
        if self.done("dev_decision"):
            self.stage("label_pass", self.st_label_pass)
            self.stage("analysis", self.st_analysis)

    # -- status ------------------------------------------------------------------------------

    def print_status(self) -> None:
        try:
            gpu, api, hours = self.spent()
        except Exception as e:  # noqa: BLE001 - the status block never fails the run
            gpu = api = hours = float("nan")
            self.emit("status_spend_unreadable", error=str(e))
        names = STAGE_NAMES_E if self.amend_e() else STAGE_NAMES
        nxt = next((n for n in names if not self.done(n)), "none")
        block = {
            "phase": self.state.get("current"), "next": nxt,
            "run_phase": self.state.get("phase", "run1"),
            "stopped": self.state.get("stopped"), "blocked": self.state.get("blocked"),
            "error": self.state.get("error"),
            "spend": {"gpu_usd": round(gpu, 2), "api_usd": round(api, 2),
                      "total_usd": round(gpu + api, 2), "cap_usd": C.HARD_CAP_USD,
                      "gpu_kill_usd": C.GPU_LIMIT_USD, "api_limit_usd": C.API_LIMIT_USD},
            "pod_hours": {"used": round(hours, 2), "cap": C.POD_HOURS_CAP},
            "human": self.state.get("human"),
            # development-only numbers; final readouts stay locked until the labels are scored
            "recipe": self.state.get("recipe"), "patch_seed": self.state.get("patch_seed"),
            "live_sites": self.state.get("live_sites"), "strengths": self.state.get("strengths"),
        }
        print("E13_STATUS " + json.dumps(block, default=str, indent=1), flush=True)


STAGE_POD_STEPS: dict[str, list[str]] = {
    "pod_a": ["download", "smoke", "bench-train", "train-trigger", "lora-check", "gen-trigger"],
}
STAGE_NAMES: tuple[str, ...] = (
    "notices", "style_notices", "notice_check", "inferred", "inputs", "launch", "pod_a", "plumbing", "pod_b",
    "projection", "gen_dev", "dev_decision", "dirsource", "direction", "sweep", "sweep_judged",
    "strength",
    "label_pass", "final_patch", "teardown", "analysis",
)
STAGE_NAMES_E: tuple[str, ...] = (  # Amendment E's second phase (after REUSED_STAGES_E)
    "launch", "train_tan32", "projection", "gen_dev", "dev_decision", "dirsource", "direction",
    "sweep", "sweep_judged", "strength", "label_pass", "final_patch", "teardown", "analysis",
)


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["status", "advance", "plan", "relaunch", "amend-e"])
    ap.add_argument("--no-wait", action="store_true",
                    help="return Blocked instead of polling a running pod job")
    a = ap.parse_args(argv)
    o = Orchestrator(deps or real_deps())
    if a.no_wait:
        o.d.wait_jobs = False
    if a.cmd == "plan":
        o.load()
        print("\n".join(STAGE_NAMES_E if o.amend_e() else STAGE_NAMES))
        return 0
    if a.cmd == "amend-e":
        return o.start_amend_e()
    if a.cmd == "relaunch":
        return o.relaunch()
    if a.cmd == "status":
        o.load()
        o.print_status()
        return 0
    return o.advance()


if __name__ == "__main__":
    sys.exit(main())
