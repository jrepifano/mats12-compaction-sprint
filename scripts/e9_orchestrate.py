"""E9 laptop orchestrator (unit F1b): projection, gated launch with stage admission, the GO
barrier and its $10 kill, re-arm and deadline extension, bounded fetch, unconditional cleanup,
stop, gate log, branch dispositions, the HF privacy audit and the §8.9 status block.

Spec: docs/experiments/E9/prereg.md §7.3 (per-pod budgets, re-arms, cuts), §7.6 (stopping
rules), §8.4 (automatic gates), §8.6 (GO barrier, the $10 kill), §8.7 (retrieval <= 3 tries, the
third by tar; unconditional cleanup; fetch, verify, terminate in that order, as separate steps),
§8.9 (status block), §4.14 (repo manifest, HF privacy audit); docs/experiments/E9/
implementation-plan.md (v4) rows F1b/F1a/F2/E2/G1, "E9 admission protocol", "The answer pairs'
pre-inspection check", "Invalidate-and-replay", "Paid steps", "Failure handling".

Ported in pieces from scripts/e3_orchestrate.py at commit 6ca28aa (Deps injection, the flock
helpers, the bounded runner `emx.e3.budget.run_cmd`, the exclusive STOPPED.json claim, the
cleanup's terminate -> settle -> close-reservation order, the detached job start verified by
progress).

Interfaces used (the real ones):
- `emx.e9.budget` (F1a): `gather_state(decisions)`, `project`, `remaining_after`, `gate_check`,
  `paid_step_table`, `unit_costs`, `measured_from_sessions`, `reserve_launch(stage, usd, note,
  rm=)`, `extend(pod_id, new_deadline, note)`, `after_api_change(root=, runner=, cleanup=)`,
  `accrued_usd`, `barrier_kill_due`; refusals `AdmissionRefused` / `DeadlinePassed` (both
  `spend.BudgetExceeded`), `AdmissionError` (books unreadable), `runpod_reservations.NotAdmitted`
  (the shared launch rule: wait). One reservation per pod (a reservation launches once); notes
  are `contracts.ledger_note(stage, <plan step>, <slot>)`. Per-pod budget = ceil(1.25 x the
  pod's runs at the upper unit cost, to the cent), F1a's `launch_plan` rule.
- F2: `pod_up.py --exp E9 --slot S --stage ST --reservation RID --budget-usd B --start I
  --no-fallback` (I indexes contracts.GPU_ORDER; this script walks the order on a stock-out);
  `pod_push.sh --exp E9 --slot S --runs RUN:KIND[,..]` and `--arm-only`;
  `pod_pull_results.sh --exp E9 --slot S --pod-id ID --run R.. [--tar]` (exit 0 promoted, 1
  retry, 3 FAILED or unfinished; it verifies DONE hashes and the inventory before promoting);
  `pod_down.py --exp E9 --slot S | --pod-id ID --reservation RID [--slot S] --reason R`.
- E2: `bash scripts/pod_run_e9.sh --run RUN:ARM:CPT_SEED:KIND:STREAM [--run ..]
  [--insert-loss]` in /ws/emx (exit 0 done, 1 failed, 2 usage, 3 GO timeout; tags E9_JOB_*,
  E9_RUN_*; job log logs/pod_run_e9.<UTC>.log); GO at <run dir>/GO or results/E9/GO;
  E9_NOISE_FLOOR_FILE points at the smoke's determinism.json, pushed here to every pod with a
  sampling-seed (cs) run.
- G1: `rules.preinspect_ok(build, pairs_sha)` on contracts.PREINSPECT_EVIDENCE; STATUS.json
  `{"status": "verified", ...}`; gate-log lines `{"gate", "launched", "depends_on"}`; analysis
  records results/E9/analysis/<name>.json (`gate --record`).

Every command prints one line `E9_ORCH <json>` and appends to logs/E9-orchestrate.jsonl;
`--dry-run` (before the subcommand) prints what would be done and changes nothing.

  project                         F1a's gate check (cuts, safe stop) and paid-step table
  status [--phase --gate --stage --reviews --next]
  launch --step S2..S7|X --runs SPEC[,..] [--slot-map _A=R1+R2,_B=R3] [--insert-loss]
           SPEC = RUN:KIND (arm, seed and stream derived; smoke -> contracts default) or the
           job's full RUN:ARM:CPT_SEED:KIND:STREAM
  go --step S                     push GO to the step's pods once `review:S` is recorded pass
  barrier-check                   the $10 rule for every step still waiting at its barrier
  rearm                           after an API spend/reserve change (budget.after_api_change)
  extend --slot S --usd X         a liability-raising deadline extension (budget.extend), re-arm
  probe --slot S                  one consolidated ssh check of the pod job
  fetch --slot S                  pull (<= 3 tries, the third by tar); STATUS.json per run
  cleanup --slot S [--reason R] [--no-fetch] [--outcome done|aborted]
                                  fetch (bounded) unless --no-fetch, then terminate + settle,
                                  close reservations, record lost runs (always runs)
  stop --rule R [--detail D] [--no-fetch]
  gate --name G --outcome O [--inputs JSON|@file]  |  gate --record results/E9/analysis/X.json
  audit-hf                        E9 repo manifest; every repo private, else private + stop
  dispose --branch B --status S [--reason R]
                                  B: gradient_screen | extra_seeds | part2 (aliases SCREEN,
                                  X/alt, S6/S7); the one-shot the main session runs (plan,
                                  "Decisions taken" 3):
                                  dispose --branch gradient_screen --status dropped
                                    --reason "optional, cut first (prereg §7.3); 7-day deadline"

Launch prerequisites (the latest gate-log record of each name decides; NAME.KEY reads the
record's `outcomes[KEY]`, recorded with `gate --record`): S2 code_review_R2, code_review_R3,
killswitch_linux_check; S3 S1_frozen, smoke; S4 endpoint-precision; S5 pilot.promising; S6
gate.part2, code_review_R4; S7 factorial_policy, pool_sufficient; X
gate.next=extra_seeds (G1's step-9 decision; NAME.KEY=VALUE). X and Part 2 (S6, S7)
exclude each other. A launch refuses when its authorizing analysis record changed on disk or
a replay (results/E9/analysis/invalidations.json) revoked it. Launch gate-log lines carry
`depends_on` [{name, sha256}] of the authorizing analysis records and `produces` (run ids);
analysis gate lines carry `inputs_from_runs`. Recording `review:S` as fail stops at once.
Barrier steps (F1a's BARRIER_STEPS: S4, S5, X, S6, S7) wait at GO until `review:<step>` (the
analysis review of the gate that admitted the step) is recorded pass; a fail stops the run.

Exit codes: 0 ok; 1 a recoverable failure (cleaned up or nothing spent); 2 a §7.6 stop fired;
3 a pod not confirmed terminated, or a reservation kept with an uncertain pod; 4 refused before
anything was spent. `cleanup` exits 1 when a pod was terminated but not settled (pod_down.py exit
3: the session and slot are kept and listed as `unsettled`; rerun pod_down or cleanup later) and
3 when termination is unconfirmed after 3 pod_down attempts (exit 1: it may still bill). Any
non-zero pod_push.sh exit cleans the slot up at once (4: the pod is UNARMED; 5/6: armed, transfer
or setup failed), a failed launch, never lost records.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emx.e9 import contracts as C

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCH_LOG = "logs/E9-orchestrate.jsonl"
LAUNCH_LOCK = "logs/E9-launch.lock"
CREATE_LOCK = "logs/E9-create.lock"  # a pod creation vs a stop's sweep (review R3 r2 #1)
STOPPED_FILE = f"{C.RESULTS}/{C.STOPPED}"
BRANCHES_FILE = f"{C.RESULTS}/branches.json"
DECISIONS_FILE = f"{C.RESULTS}/budget_decisions.json"  # main-session overrides of F1a's state
NOISE_FLOOR_LOCAL = f"{C.RUNS_DIR}/smoke/determinism.json"
NOISE_FLOOR_POD = f"{C.RESULTS}/noise_floor/determinism.json"
SMOKE_SPEC = ("ckpt", 0, "data/E9/streams/smoke.npy")  # arm, CPT seed, stream of a smoke run
POD_WS = "/ws/emx"
JOB_SCRIPT = "scripts/pod_run_e9.sh"
CREATE_RE = re.compile(r"^EMX_POD_CREATE=(\w+) reservation=(\S+)(?: pod=(\S+))?", re.MULTILINE)
DEADLINE_RE = re.compile(r"^EMX_POD_DEADLINE=(\d+)\s*$", re.MULTILINE)
STOCK_OUT = ("There are no instances currently available", "no GPU in the fallback order")
TERMINATE_ATTEMPTS = 3
POD_DOWN_UNSETTLED = 3  # pod_down.py (E9): terminated, settlement failed; never clear the slot
PUSH_EXITS = {1: "refused laptop-side (nothing sent)", 2: "usage error",
              4: "killswitch NOT armed/acknowledged: the pod is UNARMED",
              5: "bulk transfer or pin check failed (armed)", 6: "pod setup failed (armed)",
              124: "timed out (funded time)"}
MAX_ATTEMPTS_PER_RUN = 2  # a failed run is rerun once (plan "Failure handling")
RUN_RE = re.compile(r"^([a-z]+)-c(\d+)$")
PULL_OK = re.compile(r"^run (\S+): verified and promoted", re.MULTILINE)
PULL_FAILED = re.compile(r"^run (\S+): FAILED on the pod", re.MULTILINE)
PULL_UNFINISHED = re.compile(r"^run (\S+): no DONE\.json or FAILED\.json yet", re.MULTILINE)

STOP_DEADLINE = "7 days since sign-off: no new paid work (§7.6)"
STOP_PREINSPECT = "measurement invalid: the answer pairs' pre-inspection check failed (§4.7)"
STOP_LIMIT = "safe stop: the next launch does not fit a limit after cuts (§7.3)"
STOP_REVIEW = "BLOCKING review finding on a gate computation (§8.6, §7.6)"
STOP_HF_PUBLIC = "an E9 repository was found public after an upload (§4.14, §7.6)"


@dataclass(frozen=True)
class Step:
    stage: str
    prereqs: tuple[str, ...]  # gate names; NAME.KEY = the record's outcomes[KEY]
    barrier: bool
    arms: tuple[str, ...]
    seeds: tuple[int, ...]
    phase: str


STEPS: dict[str, Step] = {
    "S2": Step("setup", ("code_review_R2", "code_review_R3", "killswitch_linux_check"), False,
               ("smoke",), (), "smoke"),
    "S3": Step("part1", ("S1_frozen", "smoke"), False, ("bg",), C.PART1_CPT_SEEDS,
               "cost and noise"),
    "S4": Step("part1", ("endpoint-precision",), True, ("orig", "corr"), C.PART1_CPT_SEEDS,
               "pilot"),
    "S5": Step("part1", ("pilot.promising",), True, ("para",), C.PART1_CPT_SEEDS, "paraphrase"),
    "S6": Step("part2_mandatory", ("gate.part2", "code_review_R4"), True, C.ARMS_FACTORIAL,
               C.PART1_CPT_SEEDS, "factorial"),
    "S7": Step("part2_mandatory", ("factorial_policy", "pool_sufficient"), True, C.ARMS_CONFIRM,
               C.CONFIRM_CPT_SEEDS, "confirmation"),
    "X": Step("extra_seeds", ("gate.next=extra_seeds",), True, C.ARMS_PART1,
              C.EXTRA_CPT_SEEDS, "extra seeds"),
}
PART2_STEPS = ("S6", "S7")
EXCLUSIVE = {"X": PART2_STEPS, "S6": ("X",), "S7": ("X",)}  # the null path vs Part 2 (§4.11)
INVALIDATIONS = f"{C.ANALYSIS_DIR}/invalidations.json"
RUN_PATH_RE = re.compile(rf"^{re.escape(C.RUNS_DIR)}/([^/]+)/")
STEP_ALIASES = {"alt": "X"}
STEP_BRANCH = {"S6": "part2", "S7": "part2", "X": "extra_seeds"}
BRANCHES = ("gradient_screen", "extra_seeds", "part2")  # results/E9/branches.json names
BRANCH_ALIASES = {"screen": "gradient_screen", "SCREEN": "gradient_screen",
                  "X": "extra_seeds", "alt": "extra_seeds", "S6": "part2", "S7": "part2"}


class LimitRefused(RuntimeError):
    """F1a refused a reservation on a limit (safe stop) or the deadline (`deadline`)."""

    def __init__(self, msg: str, deadline: bool = False):
        super().__init__(msg)
        self.deadline = deadline


@dataclass
class Deps:
    """Everything that touches the world, so tests replace it (no pod, no paid call)."""

    root: Path
    runner: Callable[..., tuple[int, str]]  # (cmd, *, root, env, timeout) -> (rc, output)
    rr: Any  # emx.runpod_reservations
    spend: Any  # emx.spend
    budget: Any  # emx.e9.budget (F1a)
    preinspect: Callable[[], Any]  # emx.e9.rules.preinspect_ok on the frozen evidence
    unsettled: Callable[[], list[dict]]  # runpod_reservations.unsettled_pods("E9")
    balance: Callable[[], float | None]  # RunPod balance (free GraphQL); None = unknown
    watchdog_ok: Callable[[], tuple[bool, str]]
    ssh_target: Callable[[str], tuple[str, str, str] | None]  # slot -> (user, host, port)
    hf_private: Callable[[str], bool | None]  # None = could not check
    hf_make_private: Callable[[str], None]
    admission_kw: Callable[[], dict] = dict  # extra kwargs for budget admissions (tests: time)
    env: dict = field(default_factory=dict)  # extra env for every command (tests)
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    wait_poll_s: float = 300.0  # a launch-rule wait
    pull_wait_s: float = 120.0  # between retrieval attempts
    job_wait_s: float = 20.0  # before checking a detached job's progress (15-20 s)
    retrieval_timeout_s: float = 900.0
    terminate_wait_s: float = 30.0
    terminate_timeout_s: float = 300.0
    create_timeout_s: float = 1800.0


def _read_env(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = (root / ".env").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


def real_deps(root: Path = REPO_ROOT) -> Deps:
    from emx import runpod_reservations as rr
    from emx import spend
    from emx.e3.budget import run_cmd
    from emx.e9 import budget

    def runner(cmd, *, root=root, env=None, timeout=None):
        return run_cmd(cmd, root=root, env=env, timeout=timeout)

    def preinspect():
        from emx.e9 import rules  # G1

        build = read_json(root / C.PREINSPECT_EVIDENCE)
        return rules.preinspect_ok(build, C.PAIRS_SHA256)

    def balance():
        try:
            return float(rr.balance())
        except Exception as e:  # noqa: BLE001 - unknown, never zero
            print(f"RunPod balance read failed: {e}", file=sys.stderr)
            return None

    def watchdog_ok():
        from emx import runpod as rp

        rp.configure(C.EXP)
        return rp.watchdog_live(C.EXP)

    def ssh_target(slot):
        env = _read_env(root)
        host, port = env.get(f"POD_SSH_HOST{slot}"), env.get(f"POD_SSH_PORT{slot}")
        if not host or not port:
            return None
        return env.get("POD_SSH_USER") or "root", host, port

    def _api():
        from huggingface_hub import HfApi

        return HfApi(token=_read_env(root).get("HF_TOKEN") or os.environ.get("HF_TOKEN"))

    def hf_private(repo):
        try:
            return bool(_api().model_info(repo).private)
        except Exception as e:  # noqa: BLE001 - unknown is a failed audit, never a pass
            print(f"HF model_info({repo}) failed: {e}", file=sys.stderr)
            return None

    def hf_make_private(repo):
        api = _api()
        if hasattr(api, "update_repo_settings"):
            api.update_repo_settings(repo_id=repo, private=True)
        else:  # older huggingface_hub
            api.update_repo_visibility(repo_id=repo, private=True)

    return Deps(
        root=root, runner=runner, rr=rr, spend=spend, budget=budget, preinspect=preinspect,
        unsettled=lambda: rr.unsettled_pods(C.EXP), balance=balance, watchdog_ok=watchdog_ok,
        ssh_target=ssh_target, hf_private=hf_private, hf_make_private=hf_make_private,
    )


# --- small file helpers -------------------------------------------------------------------------


def utc(epoch: float | None = None) -> str:
    t = time.time() if epoch is None else float(epoch)
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _flock(path: Path, blocking: bool = True):
    """Yield True when the lock is held, False when `blocking=False` and it is busy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def append_jsonl(path: Path, rec: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(rec, default=str, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return rec


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str, sort_keys=True) + "\n")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def preinspect_passed(result) -> tuple[bool, str]:
    """G1's `preinspect_ok` returns {"outcome": "pass"|"fail", "reason", ...}; a bool, an
    (ok, detail) pair or {"ok": ...} are accepted too."""
    if isinstance(result, tuple):
        return bool(result[0]), str(result[1]) if len(result) > 1 else ""
    if isinstance(result, dict):
        ok = result.get("outcome") == "pass" if "outcome" in result else bool(result.get("ok"))
        return ok, str(result.get("reason") or json.dumps(result, default=str)[:300])
    return bool(result), ""


# --- runs ---------------------------------------------------------------------------------------


def parse_runs(spec: str) -> list[dict]:
    """'bg-c1:cs,bg-c2:cq' or full 'RUN:ARM:SEED:KIND:STREAM' items -> [{run, arm, seed, kind,
    stream}] (the job's run spec fields)."""
    out = []
    for item in (x.strip() for x in spec.split(",")):
        if not item:
            continue
        parts = item.split(":")
        if len(parts) == 2:
            run, kind = parts
            if kind == "smoke":
                arm, seed, stream = SMOKE_SPEC
            else:
                m = RUN_RE.match(run)
                if not m:
                    raise ValueError(f"run id {run!r} is not <arm>-c<seed>")
                arm, seed = m.group(1), int(m.group(2))
                stream = f"{C.STREAMS_DIR}/{run}.npy"
        elif len(parts) == 5:
            run, arm, seed_s, kind, stream = parts
            try:
                seed = int(seed_s)
            except ValueError:
                raise ValueError(f"{item!r}: CPT seed is not an integer") from None
        else:
            raise ValueError(f"{item!r}: want RUN:KIND or RUN:ARM:CPT_SEED:KIND:STREAM")
        if kind not in C.RUN_KINDS:
            raise ValueError(f"{item!r}: kind must be one of {C.RUN_KINDS}")
        if not stream.endswith(".npy"):
            raise ValueError(f"{item!r}: the stream must be a .npy file")
        out.append({"run": run, "arm": arm, "seed": seed, "kind": kind, "stream": stream})
    if not out:
        raise ValueError("--runs is empty")
    if len({r["run"] for r in out}) != len(out):
        raise ValueError("a run appears twice in --runs")
    return out


def job_spec(r: dict) -> str:
    return f"{r['run']}:{r['arm']}:{r['seed']}:{r['kind']}:{r['stream']}"


def check_run(step: str, r: dict) -> None:
    st, run, kind = STEPS[step], r["run"], r["kind"]
    if kind == "smoke" or step == "S2":
        if not (step == "S2" and kind == "smoke" and run.startswith("smoke")):
            raise ValueError(f"smoke runs (kind smoke, id smoke*) belong to S2 only: {run}")
        return
    if run != C.run_id(r["arm"], r["seed"]):
        raise ValueError(f"{run}: the run id must be {C.run_id(r['arm'], r['seed'])}")
    if r["arm"] not in st.arms or r["seed"] not in st.seeds:
        raise ValueError(f"{run}: step {step} runs arms {st.arms} at CPT seeds {st.seeds}")


def parse_slot_map(spec: str | None, runs: list[str], busy: set[str]) -> dict[str, list[str]]:
    """'_A=R1+R2,_B=R3' -> {slot: [runs in order]}; without a map, one run per free slot."""
    if not spec:
        free = [f"_{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if f"_{c}" not in busy]
        return {free[i]: [r] for i, r in enumerate(runs)}
    out: dict[str, list[str]] = {}
    for item in (x.strip() for x in spec.split(",") if x.strip()):
        slot, _, rs = item.partition("=")
        if not re.fullmatch(r"_[A-Za-z0-9]{1,8}", slot) or not rs:
            raise ValueError(f"bad --slot-map item {item!r}")
        out[slot] = [r for r in rs.split("+") if r]
    mapped = [r for rs in out.values() for r in rs]
    if sorted(mapped) != sorted(runs):
        raise ValueError(f"--slot-map runs {mapped} do not match --runs {runs}")
    return out


def unit_key(kind: str, position: int) -> str:
    """F1a's unit for a pod's run at `position`: a second cq run on a set-up pod is `q2`."""
    return "q2" if kind == "cq" and position > 0 else kind


class Orchestrator:
    def __init__(self, deps: Deps, dry: bool = False):
        self.d = deps
        self.root = Path(deps.root)
        self.dry = dry
        self._holding_create = False
        self._launch_ctx: dict | None = None

    @contextmanager
    def _create_lock(self):
        """Held around every pod creation and by a stop's sweep, so a stop either comes first
        (the creation is vetoed) or waits for an in-flight pod_up and then sweeps its pod.
        Re-entrant within one orchestrator (a stop fired from inside a launch)."""
        if self._holding_create or self.dry:
            yield
            return
        with _flock(self.path(CREATE_LOCK)):
            self._holding_create = True
            try:
                yield
            finally:
                self._holding_create = False

    # -- logging, paths ------------------------------------------------------------------------

    def path(self, rel: str) -> Path:
        return self.root / rel

    def emit(self, cmd: str, summary: dict) -> dict:
        line = {"cmd": cmd, "dry_run": self.dry, **summary}
        print("E9_ORCH " + json.dumps(line, default=str, sort_keys=True), flush=True)
        if not self.dry:
            append_jsonl(self.path(ORCH_LOG), {"utc": utc(self.d.now()), **line})
        return line

    def log_events(self) -> list[dict]:
        return read_jsonl(self.path(ORCH_LOG))

    def run(self, cmd: list[str], timeout: float | None = None,
            env: dict | None = None) -> tuple[int, str]:
        if self.dry:
            print("DRY RUN: " + shlex.join(cmd), flush=True)
            return 0, ""
        try:
            rc, out = self.d.runner(cmd, root=self.root, env={**self.d.env, **(env or {})},
                                    timeout=timeout)
        except Exception as e:  # noqa: BLE001 - a raising command is a failed command
            return 1, f"{type(e).__name__}: {e}"
        if out:
            sys.stderr.write(out if out.endswith("\n") else out + "\n")
        return rc, out

    def _budget_runner(self, cmd, *, root=None, env=None, timeout=None):
        return self.run(list(cmd), timeout=timeout, env=env)

    # -- state ---------------------------------------------------------------------------------

    def stopped(self) -> dict | None:
        p = self.path(STOPPED_FILE)
        if not p.exists():
            return None
        return read_json(p) or {"rule": "unreadable STOPPED.json"}

    def gates(self) -> list[dict]:
        return read_jsonl(self.path(C.GATE_LOG))

    def latest_gate(self, name: str) -> tuple[int, dict] | None:
        hits = [(i, g) for i, g in enumerate(self.gates())
                if g.get("kind") == "gate" and g.get("gate") == name]
        return hits[-1] if hits else None

    def prereq_state(self, req: str) -> tuple[str, tuple[int, dict] | None]:
        """('pass' when met, else what was found, and the gate record) for NAME (outcome pass),
        NAME.KEY (outcomes[KEY] == pass) or NAME.KEY=VALUE (outcomes[KEY] == VALUE)."""
        req, _, want = req.partition("=")
        name, _, key = req.partition(".")
        g = self.latest_gate(name)
        if g is None:
            return "missing", None
        val = g[1].get("outcome") if not key else (g[1].get("outcomes") or {}).get(key)
        if val is None:
            return "missing", g
        return ("pass" if str(val) == (want or "pass") else str(val)), g

    def authorization_problems(self, step: str) -> list[str]:
        """Why the step's recorded authorizations no longer hold (review r1 #9): the analysis
        record changed on disk since its gate was recorded, or a replay (invalidations.json)
        changed that analysis or marked this step invalid after the gate was recorded."""
        inv = read_json(self.path(INVALIDATIONS)) or {}
        changed = {c.get("analysis") for c in inv.get("changed") or [] if isinstance(c, dict)}
        invalid_steps = set(inv.get("invalid_steps") or [])
        out = []
        for q in STEPS[step].prereqs:
            g = self.prereq_state(q)[1]
            if g is None:
                continue
            name, rec = g[1]["gate"], g[1].get("record")
            if rec:
                p = self.path(rec["path"]) if not Path(rec["path"]).is_absolute() \
                    else Path(rec["path"])
                now_sha = sha256_file(p) if p.is_file() else None
                if now_sha != rec.get("sha256"):
                    out.append(f"{name}: {rec['path']} changed since its gate was recorded")
            if inv and (name in changed or step in invalid_steps) and \
                    str(g[1].get("utc", "")) <= str(inv.get("utc", "")):
                out.append(f"{name}: revoked by the replay of {inv.get('utc')}")
        return out

    def ever_launched(self, steps) -> list[str]:
        seen = {x.get("step") for x in self.launches()}
        seen |= {s for g in self.gates() if g.get("kind") == "launch"
                 for s in g.get("launched") or []}
        return sorted(seen & set(steps))

    def pod_events_path(self) -> Path:
        d = os.environ.get("EMX_POD_LOG_DIR")
        return (Path(d) if d else self.root / "logs") / f"pods_{C.EXP}.jsonl"

    def pod_sessions(self) -> list[dict]:
        """One row per E9 pod from logs/pods_E9.jsonl: {pod_id, slot, t0, running}."""
        pods: dict[str, dict] = {}
        for e in read_jsonl(self.pod_events_path()):
            pid = e.get("pod_id")
            if not pid:
                continue
            if e.get("event") == "created":
                pods[pid] = {"pod_id": pid, "slot": e.get("slot", ""), "t0": e.get("utc"),
                             "running": True}
            elif e.get("event") == "terminated" and pid in pods:
                pods[pid]["running"] = False
        return list(pods.values())

    def running_pods(self) -> list[dict]:
        return [p for p in self.pod_sessions() if p["running"] and p["slot"]]

    def slot_pod(self, slot: str) -> dict | None:
        return next((p for p in self.running_pods() if p["slot"] == slot), None)

    def launches(self) -> list[dict]:
        """Every successful pod launch, oldest first, with job/go/cleaned flags."""
        out: list[dict] = []
        by_pod: dict[str, dict] = {}
        for e in self.log_events():
            ev, pid = e.get("event"), e.get("pod_id")
            if ev == "launched":
                rec = dict(e)
                out.append(rec)
                by_pod[pid] = rec
            elif pid in by_pod and ev == "job_started" and e.get("ok"):
                by_pod[pid]["job"] = True
            elif pid in by_pod and ev == "go_pushed":
                by_pod[pid]["go"] = True
            elif pid in by_pod and ev == "cleaned":
                by_pod[pid]["cleaned"] = True
        return out

    def slot_launch(self, slot: str) -> dict | None:
        """The latest launch on this slot."""
        hits = [x for x in self.launches() if x.get("slot") == slot]
        return hits[-1] if hits else None

    def step_slots(self, step: str) -> list[dict]:
        """Launches of `step` whose pod is still running."""
        running = {p["pod_id"] for p in self.running_pods()}
        return [x for x in self.launches() if x.get("step") == step and x["pod_id"] in running]

    def run_status(self, run: str) -> dict:
        return read_json(self.path(f"{C.RUNS_DIR}/{run}/STATUS.json"), {}) or {}

    def write_run_status(self, run: str, **fields) -> dict:
        prev = self.run_status(run)
        rec = {**prev, "run": run, **fields, "utc": utc(self.d.now())}
        rec["verified"] = rec.get("status") == "verified"
        rec["history"] = [*prev.get("history", []),
                          {k: fields.get(k) for k in ("status", "reason") if k in fields}
                          | {"utc": rec["utc"]}]
        if not self.dry:
            write_json(self.path(f"{C.RUNS_DIR}/{run}/STATUS.json"), rec)
        return rec

    def runpod_lines(self) -> dict:
        """E9's unlaunched open reservations and its launched pods' current liability."""
        st = self.d.rr.state(exp=C.EXP)
        unl = sum(R["open_usd"] for R in st["reservations"].values()
                  if not (R.get("extends_pod") or R.get("launched") or R.get("closed")))
        lia = sum(float(p.get("liability_usd", 0.0)) for p in st["pods"].values()
                  if not p.get("settled"))
        return {"unlaunched_usd": round(unl, 4), "liability_usd": round(lia, 4)}

    # -- F1a's projection state ----------------------------------------------------------------

    def decisions(self) -> dict:
        """F1a's `decisions`: the main session's overrides (results/E9/budget_decisions.json)
        over what the log shows: a step with launches is running, done once no pod of it runs
        and all its runs are verified; relaunches from barrier kills; the GPU type of the latest
        launch; C_Q/C_S measured from settled one-run pods whose run verified."""
        dec = dict(read_json(self.path(DECISIONS_FILE), {}) or {})
        launches = self.launches()
        running = {p["pod_id"] for p in self.running_pods()}
        steps: dict[str, str] = {}
        for step in dict.fromkeys(x["step"] for x in launches):
            mine = [x for x in launches if x["step"] == step]
            done = not any(x["pod_id"] in running for x in mine) and all(
                self.run_status(r).get("status") == "verified" for x in mine for r in x["runs"])
            steps[step] = "done" if done else "running"
        dec["steps"] = {**steps, **(dec.get("steps") or {})}
        rel: dict[str, int] = {}
        for e in self.log_events():
            if e.get("event") == "relaunch_pending":
                rel[e["step"]] = rel.get(e["step"], 0) + 1
        fired = dict(dec.get("fired") or {})
        if rel:
            fired["relaunches"] = {**rel, **(fired.get("relaunches") or {})}
        dec["fired"] = fired
        if launches and "gpu" not in dec:
            g = launches[-1].get("gpu") or list(C.GPU_ORDER[0])
            dec["gpu"] = {"type": g[0], "rate_usd_h": float(g[2])}
        if "measured" not in dec:
            pods = self.d.rr.state(exp=C.EXP)["pods"]
            sessions = []
            for x in launches:
                p = pods.get(x["pod_id"])
                if (len(x["runs"]) == 1 and p and p.get("settled") and p["rate_usd_h"] > 0
                        and self.run_status(x["runs"][0]).get("status") == "verified"):
                    sessions.append({"kind": x["kinds"][x["runs"][0]],
                                     "hours": float(p.get("settled_usd") or 0.0) / p["rate_usd_h"],
                                     "gpu": (x.get("gpu") or [""])[0]})
            m = self.d.budget.measured_from_sessions(sessions, dec.get("gpu", {}).get("type"))
            if m:
                dec["measured"] = m
        return dec

    def budget_state(self) -> dict:
        return self.d.budget.gather_state(self.decisions())

    # -- stop ----------------------------------------------------------------------------------

    def stop(self, rule: str, detail: str = "", fetch: bool = True, cmd: str = "stop") -> int:
        """A §7.6 stop: STOPPED.json (exclusive create; the first rule is kept), then cleanup of
        every E9 pod (each fetched first unless `fetch` is False), release or settle of every
        open RunPod and API reservation, running branches marked stopped, the status block."""
        rec = {"rule": rule, "detail": detail, "utc": utc(self.d.now()), "cmd": cmd}
        if self.dry:
            self.emit(cmd, {"event": "stop", "would_write": STOPPED_FILE, **rec,
                            "slots": [p["slot"] for p in self.running_pods()]})
            return 2
        p = self.path(STOPPED_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(p, "x") as f:
                f.write(json.dumps(rec, indent=2) + "\n")
        except FileExistsError:
            self.emit(cmd, {"event": "stop_already", "rule": rule,
                            "stopped_rule": (self.stopped() or {}).get("rule")})
            print(f"E9_STOPPED_ALREADY {(self.stopped() or {}).get('rule')}", flush=True)
        else:
            self.emit(cmd, {"event": "stop", **rec})
            print(f"E9_STOP {rule}", flush=True)
        with self._create_lock():
            worst = self.shutdown_all(f"stop: {rule}", fetch=fetch)
        out = self.outstanding_settlements()
        try:
            st = read_json(p) or dict(rec)
            write_json(p, {**st, "outstanding_settlements": out,
                           "shutdown_rc": worst, "shutdown_utc": utc(self.d.now())})
        except Exception as e:  # noqa: BLE001
            print(f"E9 STOP: could not record outstanding settlements: {e}", file=sys.stderr)
        for name, b in self.branches().items():
            if b.get("status") == "running":
                self.set_branch(name, "stopped", rule)
        print(self.status_block(next_action=f"stopped: {rule}"), flush=True)
        return 3 if worst == 3 else 2

    def outstanding_settlements(self) -> list[dict]:
        """E9 pods launched under a reservation and not settled yet (terminated or not)."""
        try:
            running = {q["pod_id"] for q in self.running_pods()}
            return [{"pod_id": k, "reservation_id": v["reservation_id"], "slot": v.get("slot"),
                     "terminated": k not in running}
                    for k, v in self.d.rr.state(exp=C.EXP)["pods"].items() if not v["settled"]]
        except Exception as e:  # noqa: BLE001
            return [{"error": f"{type(e).__name__}: {e}"}]

    def shutdown_all(self, reason: str, fetch: bool = True) -> int:
        worst, acts = 0, []
        try:
            slots = [p["slot"] for p in self.running_pods()]
        except Exception as e:  # noqa: BLE001 - the unsettled pass below still reaches them
            slots = []
            acts.append({"pods": f"{type(e).__name__}: {e}"})
        for slot in slots:  # one slot's failure never skips another's termination
            try:
                rc = self.cleanup(slot, reason, fetch=fetch)
            except Exception as e:  # noqa: BLE001
                rc = 3
                acts.append({"cleanup": slot, "error": f"{type(e).__name__}: {e}"})
            acts.append({"cleanup": slot, "rc": rc})
            worst = 3 if 3 in (rc, worst) else max(worst, rc)
        for u in self._unsettled():
            try:
                term, sett = self._down(u)
            except Exception as e:  # noqa: BLE001
                term = sett = False
                acts.append({"pod": u["pod_id"], "error": f"{type(e).__name__}: {e}"})
            acts.append({"pod": u["pod_id"], "terminated": term, "settled": sett})
            if not term:
                worst = 3
            elif not sett:
                worst = max(worst, 1)
        try:
            open_res = [rid for rid, R in self.d.rr.state(exp=C.EXP)["reservations"].items()
                        if not R.get("closed")]
        except Exception as e:  # noqa: BLE001
            open_res = []
            acts.append({"runpod": f"{type(e).__name__}: {e}"})
        for rid in open_res:
            try:
                acts.append({"runpod": rid, "result": self._close_reservation(rid)})
            except Exception as e:  # noqa: BLE001
                acts.append({"runpod": rid, "error": f"{type(e).__name__}: {e}"})
        try:
            api = self.d.spend.open_reservations(C.EXP)
        except Exception as e:  # noqa: BLE001
            api = {}
            acts.append({"api": f"open_reservations failed: {e}"})
        for rid, s in api.items():
            try:
                if s.get("submitted"):  # paid calls may have run: settle in full
                    self.d.spend.settle_api(rid, None, exp=C.EXP, note=reason)
                    acts.append({"settled_api": rid})
                else:
                    self.d.spend.release_api(rid, exp=C.EXP)
                    acts.append({"released_api": rid})
            except (Exception, SystemExit) as e:  # noqa: BLE001
                acts.append({"api": rid, "error": f"{type(e).__name__}: {e}"})
        out = self.outstanding_settlements()
        if out and worst == 0:
            worst = 1  # a terminated but unsettled pod is not a clean shutdown
        self.emit("shutdown", {"event": "shutdown", "reason": reason, "actions": acts,
                               "outstanding_settlements": out, "rc": worst})
        return worst

    def refuse(self, cmd: str, why: str, **ctx) -> int:
        self.emit(cmd, {"event": "refused", "why": why, **ctx})
        return 4

    def refuse_if_stopped(self, cmd: str, **ctx) -> int | None:
        st = self.stopped()
        if st is None:
            return None
        return self.refuse(cmd, f"E9 is stopped ({st.get('rule')}): {STOPPED_FILE}", **ctx)

    # -- project -------------------------------------------------------------------------------

    def project(self) -> int:
        b = self.d.budget
        state = self.budget_state()
        chk = b.gate_check(state)
        table = b.paid_step_table(state)
        print(json.dumps({"gate_check": {k: v for k, v in chk.items() if k != "projection"},
                          "projected": chk["projection"]["projected"],
                          "remaining": chk["projection"]["remaining"],
                          "path": chk["projection"]["path"],
                          "flags": chk["projection"]["flags"],
                          "paid_step_table": table}, indent=2, default=str), flush=True)
        return 0 if chk["ok"] else 2

    # -- gates ---------------------------------------------------------------------------------

    def check_preinspect(self, cmd: str) -> int | None:
        """The pairs' pre-inspection evidence; a failure is a §7.6 stop (measurement invalid)."""
        try:
            ok, detail = preinspect_passed(self.d.preinspect())
        except Exception as e:  # noqa: BLE001 - evidence that cannot be checked is not evidence
            ok, detail = False, f"{type(e).__name__}: {e}"
        if ok:
            return None
        return self.stop(STOP_PREINSPECT, detail, cmd=cmd)

    def gate(self, name: str | None, outcome: str | None, inputs: dict,
             record: str | None = None) -> int:
        """Append a gate outcome to the gate log (G1 reads `gate`, `launched`, `depends_on`).
        With `record`, the G1 analysis record decides name, outcome and `outcomes`."""
        extra: dict = {}
        if record:
            p = Path(record) if Path(record).is_absolute() else self.path(record)
            rec = read_json(p)
            if not isinstance(rec, dict) or "outcome" not in rec:
                raise SystemExit(f"{record}: not an analysis record")
            if name and name != rec.get("name"):
                raise SystemExit(f"--name {name} != the record's name {rec.get('name')}")
            if outcome and outcome != rec["outcome"]:
                raise SystemExit(f"--outcome {outcome} != the record's {rec['outcome']}")
            name, outcome = rec.get("name") or name, rec["outcome"]
            ins = rec.get("inputs") if isinstance(rec.get("inputs"), dict) else {}
            extra = {"outcomes": rec.get("outcomes") or {},
                     "config_sha256": rec.get("config_sha256"),
                     "record": {"path": str(record), "sha256": sha256_file(p)},
                     "inputs_from_runs": sorted({m.group(1) for k in ins
                                                 if (m := RUN_PATH_RE.match(str(k)))})}
        if not name or outcome not in C.GATE_OUTCOMES:
            raise SystemExit(f"a gate needs --name and --outcome in {C.GATE_OUTCOMES}")
        if name.startswith("review:"):
            rstep = STEP_ALIASES.get(name[7:], name[7:])
            if rstep not in STEPS:
                raise SystemExit(f"{name}: unknown step {name[7:]!r}")
            name = f"review:{rstep}"
            if outcome == "pass":  # bound to the exact records it approved
                extra["approves"] = self.current_approvals(rstep)
        stopped = self.check_preinspect("gate")
        if stopped is not None:
            return stopped
        line = {"kind": "gate", "gate": name, "outcome": outcome, "inputs": inputs,
                "utc": utc(self.d.now()), **extra}
        if not self.dry:
            append_jsonl(self.path(C.GATE_LOG), line)
        self.emit("gate", {"event": "gate", **line})
        if name.startswith("review:") and outcome == "fail":  # a BLOCKING finding (§8.6)
            return self.stop(STOP_REVIEW, f"{name}: {json.dumps(inputs, default=str)[:300]}",
                             cmd="gate")
        return 0

    # -- launch --------------------------------------------------------------------------------

    def launch(self, step: str, runs_spec: str, slot_map: str | None = None,
               insert_loss: bool = False) -> int:
        step = STEP_ALIASES.get(step, step)
        if step not in STEPS:
            raise SystemExit(f"--step must be one of {sorted(STEPS)}")
        st = STEPS[step]
        try:
            runs = parse_runs(runs_spec)
            for r in runs:
                check_run(step, r)
            busy = {p["slot"] for p in self.running_pods()}
            plan_slots = parse_slot_map(slot_map, [r["run"] for r in runs], busy)
        except ValueError as e:
            return self.refuse("launch", str(e), step=step)
        info = {r["run"]: r for r in runs}
        refused = self.refuse_if_stopped("launch", step=step)
        if refused is not None:
            return refused
        if self.d.now() >= C.DEADLINE_UTC.timestamp():
            return self.stop(STOP_DEADLINE, f"launch of {step} refused", cmd="launch")
        missing = [f"{q}: {s}" for q in st.prereqs if (s := self.prereq_state(q)[0]) != "pass"]
        if missing:
            return self.refuse("launch", f"prerequisite gates not passed: {missing}", step=step)
        revoked = self.authorization_problems(step)
        if revoked:
            return self.refuse("launch", f"authorization revoked: {revoked}", step=step)
        excl = self.ever_launched(EXCLUSIVE.get(step, ()))
        if excl:
            return self.refuse("launch", f"{step} excludes {excl}, already launched (§4.11)",
                               step=step)
        stopped = self.check_preinspect("launch")  # every step reads Q20 results
        if stopped is not None:
            return stopped
        busy_now = [s for s in plan_slots if s in busy]
        if busy_now:
            return self.refuse("launch", f"slot(s) {busy_now} already have a running pod")
        if any(r["kind"] == "cs" for r in runs) and not self.path(NOISE_FLOOR_LOCAL).is_file():
            return self.refuse("launch", f"{NOISE_FLOOR_LOCAL} (the smoke's noise floor, needed "
                               "by sampling-seed runs) is missing", step=step)
        attempts = {}
        for r in runs:
            s = self.run_status(r["run"])
            if s.get("status") == "verified":
                return self.refuse("launch", f"run {r['run']} is already verified", step=step)
            if s.get("status") == "launched" and self.slot_pod(s.get("slot", "")):
                return self.refuse("launch", f"run {r['run']} is running on {s.get('slot')}")
            n = int(s.get("attempt", 0))
            if s.get("status") == "failed":
                if n >= MAX_ATTEMPTS_PER_RUN:
                    return self.refuse("launch", f"run {r['run']} failed twice (no rerun left)")
                n += 1
            attempts[r["run"]] = max(n, 1)
        try:
            state = self.budget_state()
            units = self.d.budget.unit_costs(state)
            rm = self.d.budget.remaining_after(self.d.budget.project(state), step)
        except Exception as e:  # noqa: BLE001 - AdmissionError: books unreadable
            self.emit("launch", {"event": "launch_failed", "step": step,
                                 "why": f"budget state: {type(e).__name__}: {e}"})
            return 1
        pods = []
        for slot, rs in plan_slots.items():
            hi = sum(units[unit_key(info[r]["kind"], i)][1] for i, r in enumerate(rs))
            budget = math.ceil(C.POD_BUDGET_FACTOR * hi * 100 - 1e-9) / 100
            pods.append({"slot": slot, "runs": rs, "projected_usd": round(hi, 4),
                         "budget_usd": budget})
        total = round(sum(p["budget_usd"] for p in pods), 2)
        if self.dry:
            for p in pods:
                note = C.ledger_note(st.stage, step, p["slot"])
                print(f"DRY RUN: budget.reserve_launch({st.stage!r}, {p['budget_usd']}, "
                      f"{note!r}, rm=<R_m without {step}>)")
                print("DRY RUN: " + shlex.join(self._pod_up_cmd(p["slot"], st.stage, "<rid>",
                                                                p["budget_usd"], 0))
                      + f"  (then --start 1..{len(C.GPU_ORDER) - 1} on a stock-out)")
                print("DRY RUN: " + shlex.join(self._push_cmd(p["slot"], p["runs"], info)))
                print("DRY RUN: start " + shlex.join(self._job_argv(p["runs"], info,
                                                                     insert_loss)))
            self.emit("launch", {"event": "plan", "step": step, "stage": st.stage, "pods": pods,
                                 "reservation_total_usd": total, "rm": rm})
            return 0
        live, why = self.d.watchdog_ok()
        if not live:
            return self.refuse("launch", f"the E9 watchdog is not alive: {why}", step=step)
        with _flock(self.path(LAUNCH_LOCK), blocking=False) as held:
            if not held:
                return self.refuse("launch", "another E9 launch is in progress", step=step)
            return self._launch_locked(step, st, pods, info, attempts, total, rm, insert_loss)

    def _reserve(self, stage: str, step: str, p: dict, rm) -> str:
        note = C.ledger_note(stage, step, p["slot"])
        b = self.d.budget
        try:
            return b.reserve_launch(stage, p["budget_usd"], note, rm=rm, **self.d.admission_kw())
        except SystemExit as e:  # AdmissionRefused / DeadlinePassed are spend.BudgetExceeded
            raise LimitRefused(f"{type(e).__name__}: {e}",
                               deadline=type(e).__name__ == "DeadlinePassed") from e

    def _reserve_all(self, stage: str, step: str, pods: list[dict], rm) -> list[str] | int:
        """One reservation per pod, through F1a's admission. A RunPod launch-rule refusal
        (NotAdmitted) releases what this launch holds and waits (a wait is not a failure, §7.6)
        until the deadline; a limit refusal is the prereg's safe stop."""
        while True:
            rids: list[str] = []
            try:
                for p in pods:
                    rids.append(self._reserve(stage, step, p, rm))
                return rids
            except LimitRefused as e:
                self._release(rids, f"launch {step}: refused")
                return self.stop(STOP_DEADLINE if e.deadline else STOP_LIMIT, str(e),
                                 cmd="launch")
            except Exception as e:  # noqa: BLE001 - NotAdmitted waits; anything else fails
                self._release(rids, f"launch {step}: {type(e).__name__}")
                if type(e).__name__ != "NotAdmitted":
                    self.emit("launch", {"event": "launch_failed", "step": step,
                                         "why": f"reserve_launch raised {type(e).__name__}: {e}"})
                    return 1
                if self.d.now() + self.d.wait_poll_s > C.DEADLINE_UTC.timestamp():
                    return self.stop(STOP_DEADLINE, f"{step}: launch rule never admitted",
                                     cmd="launch")
                self.emit("launch", {"event": "reservation_wait", "step": step, "why": str(e)})
                self.d.sleep(self.d.wait_poll_s)
                if self.stopped() is not None:
                    return self.refuse("launch", "stopped while waiting", step=step)

    def _release(self, rids: list[str], note: str) -> None:
        for rid in rids:
            try:
                self.d.rr.release(rid, note=note)
            except Exception as e:  # noqa: BLE001
                print(f"release {rid} failed: {e}", file=sys.stderr)

    def _launch_locked(self, step, st, pods, info, attempts, total, rm, insert_loss) -> int:
        rids = self._reserve_all(st.stage, step, pods, rm)
        if isinstance(rids, int):
            return rids
        prereqs, deps_on = [], []
        for q in st.prereqs:
            _, (seq, g) = self.prereq_state(q)
            prereqs.append({"name": q, "outcome": g["outcome"], "utc": g["utc"], "seq": seq})
            if g.get("record"):  # a concrete analysis record authorized this launch
                deps_on.append({"name": g["gate"], "sha256": g["record"]["sha256"]})
        launch_id = uuid.uuid4().hex
        self._launch_ctx = {"reservation_ids": list(rids), "pod_ids": []}
        append_jsonl(self.path(C.GATE_LOG), {
            "kind": "launch", "id": launch_id, "reservation_ids": list(rids), "pod_ids": [],
            "gate": st.prereqs[0].partition(".")[0], "launched": [step],
            "depends_on": deps_on, "produces": [r for p in pods for r in p["runs"]],
            "prereqs": prereqs, "step": step, "stage": st.stage, "utc": utc(self.d.now()),
            "runs": {r: info[r]["kind"] for p in pods for r in p["runs"]},
            "slots": {p["slot"]: p["runs"] for p in pods}, "reservation_total_usd": total,
        })
        branch = STEP_BRANCH.get(step)
        if branch and self.branches().get(branch, {}).get("status") != "running":
            self.set_branch(branch, "running", f"{step} launched")
        start = len(self.log_events())
        worst, launched, aborted = 0, [], None
        for i, (p, rid) in enumerate(zip(pods, rids, strict=True)):
            veto = self._launch_veto(step, start)
            if veto is None:
                rc = self._launch_pod(step, st.stage, p, rid, info, attempts, rm, insert_loss,
                                      start)
                worst = 3 if 3 in (rc, worst) else max(worst, rc)
                if rc == 0:
                    launched.append(p["slot"])
                if rc not in (2, 3):
                    continue
                veto = (f"pod {p['slot']} ended {'in a stop' if rc == 2 else 'uncertain'}", rc)
                rest = rids[i + 1:]
            else:
                code = 1 if launched and veto[1] == 4 else veto[1]
                worst = 3 if worst == 3 else max(worst, code)
                rest = rids[i:]
            self._release(rest, f"launch {step} aborted: {veto[0]}")
            aborted = {"why": veto[0], "released": rest,
                       "not_launched": [q["slot"] for q in pods[len(pods) - len(rest):]]}
            break
        ctx = self._launch_ctx
        self.update_gate_line(launch_id, reservation_ids=list(dict.fromkeys(
            ctx["reservation_ids"])), pod_ids=list(dict.fromkeys(ctx["pod_ids"])))
        self.emit("launch", {"event": "launch_done", "step": step, "launched": launched,
                             "rc": worst, "aborted": aborted, "pod_ids": ctx["pod_ids"],
                             "reservation_ids": ctx["reservation_ids"]})
        return worst

    def update_gate_line(self, line_id: str, **fields) -> bool:
        """Rewrite one gate-log line (by its `id`) in place, under the file's flock, so a
        launch line carries every reservation it made and every pod it created (G1's
        provenance check). Appenders take the same flock, so no line is lost."""
        path = self.path(C.GATE_LOG)
        if self.dry or not path.exists():
            return False
        with open(path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                lines = f.read().splitlines()
                hit = False
                for i, x in enumerate(lines):
                    if x.strip() and f'"{line_id}"' in x:
                        rec = json.loads(x)
                        if rec.get("id") == line_id:
                            lines[i] = json.dumps({**rec, **fields}, default=str,
                                                  sort_keys=True)
                            hit = True
                if hit:
                    f.seek(0)
                    f.truncate()
                    f.write("\n".join(lines) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                return hit
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _launch_veto(self, step: str, start: int) -> tuple[str, int] | None:
        """Revalidated before every pod creation and every training start (review R3 r2 #1):
        a stop, the deadline, a prerequisite or authorization that no longer holds, or a
        barrier retirement of this step since the launch began. (why, exit code) or None."""
        st = self.stopped()
        if st is not None:
            return f"E9 is stopped ({st.get('rule')})", 2
        if self.d.now() >= C.DEADLINE_UTC.timestamp():
            return "the 7-day deadline has passed", 2
        bad = [q for q in STEPS[step].prereqs if self.prereq_state(q)[0] != "pass"]
        bad += self.authorization_problems(step)
        if bad:
            return f"authorization no longer holds: {bad}", 4
        if any(e.get("event") == "relaunch_pending" and e.get("step") == step
               for e in self.log_events()[start:]):
            return f"{step} was retired at its barrier; relaunch after its review", 1
        return None

    def _abandon(self, slot: str, reason: str) -> int:
        """Clean up a pod of an aborted or failed launch; 3 (unconfirmed termination) and
        unsettled/bookkeeping failures propagate (review R3 r2 #2)."""
        rc = self.cleanup(slot, reason, fetch=False, lost=False)
        return 3 if rc == 3 else 1

    def funded_seconds(self, pod_id: str) -> float:
        """Seconds until the pod's funded (allowed) deadline: the bound on any blocking call to
        it, such as pod_push's transfer (review r1 #2)."""
        p = self.d.rr.state(exp=C.EXP)["pods"].get(pod_id)
        if p is None:
            return 0.0
        return max(0.0, float(p["allowed"]) - self.d.now())

    def current_approvals(self, step: str) -> list[dict]:
        """[{name, sha256}] of the analysis records that authorize `step` now (their current
        files): what a review of that step's gate must approve (review R3 r2 #8)."""
        out = []
        for q in STEPS[step].prereqs:
            g = self.prereq_state(q)[1]
            rec = (g[1].get("record") if g else None) or None
            if rec:
                p = Path(rec["path"]) if Path(rec["path"]).is_absolute() else self.path(rec["path"])
                out.append({"name": g[1]["gate"],
                            "sha256": sha256_file(p) if p.is_file() else None})
        return sorted(out, key=lambda d: d["name"])

    def review_state(self, step: str) -> str:
        """'pass' only for a passing review:<step> whose `approves` equals the current
        authorizing records; else 'pending', 'fail', 'stale' or its outcome."""
        g = self.latest_gate(f"review:{step}")
        if g is None:
            return "pending"
        out = g[1].get("outcome")
        if out != "pass":
            return str(out)
        cur = self.current_approvals(step)
        if any(d["sha256"] is None for d in cur) or g[1].get("approves") != cur:
            return "stale"
        return "pass"

    def _review_passed(self, step: str) -> bool:
        return self.review_state(step) == "pass"

    @staticmethod
    def _pod_up_cmd(slot, stage, rid, budget, idx) -> list[str]:
        return ["uv", "run", "python", "scripts/pod_up.py", "--exp", C.EXP, "--slot", slot,
                "--stage", stage, "--reservation", rid, "--budget-usd", f"{budget:.2f}",
                "--start", str(idx), "--no-fallback"]

    @staticmethod
    def _push_cmd(slot, runs, info) -> list[str]:
        spec = ",".join(f"{r}:{info[r]['kind']}" for r in runs)
        return ["bash", "scripts/pod_push.sh", "--exp", C.EXP, "--slot", slot, "--runs", spec]

    @staticmethod
    def _job_argv(runs, info, insert_loss) -> list[str]:
        argv = ["bash", JOB_SCRIPT]
        for r in runs:
            argv += ["--run", job_spec(info[r])]
        return argv + (["--insert-loss"] if insert_loss else [])

    def _create_one(self, step, stage, p, rid, rm, idx) -> tuple:
        """One pod_up attempt on GPU_ORDER[idx], under the create lock. Returns ("ok", pod_id,
        deadline, rid), ("stock_out", None, None, rid or None) or ("fail", exit code)."""
        slot = p["slot"]
        if rid is None:  # pod_up released the last one on a stock-out
            try:
                rid = self._reserve(stage, step, p, rm)
            except LimitRefused as e:
                return "fail", self.stop(STOP_DEADLINE if e.deadline else STOP_LIMIT, str(e),
                                         cmd="launch")
            except Exception as e:  # noqa: BLE001 - this pod is not launched
                self.emit("launch", {"event": "launch_failed", "step": step, "slot": slot,
                                     "why": f"re-reserve: {type(e).__name__}: {e}"})
                return "fail", 1
        rc, out = self.run(self._pod_up_cmd(slot, stage, rid, p["budget_usd"], idx),
                           timeout=self.d.create_timeout_s)
        m = CREATE_RE.search(out or "")
        status = m.group(1) if m else None
        if rc == 0 and status == "ok":
            pod_id = m.group(3) or (self.slot_pod(slot) or {}).get("pod_id")
            dm = DEADLINE_RE.search(out)
            return "ok", pod_id, int(dm.group(1)) if dm else None, rid
        stock_out = any(x in (out or "") for x in STOCK_OUT)
        if status in (None, "uncertain"):  # a pod may bill: keep it for cleanup/recovery
            self.emit("launch", {"event": "launch_failed", "step": step, "slot": slot,
                                 "reservation_id": rid, "why": f"pod_up {status or 'killed'}"
                                 f" (rc {rc}); reservation kept", "gpu_index": idx})
            return "fail", 3
        if status == "closed":
            rid = None  # pod_up released it (or settled a pod it made)
        if not stock_out:
            if rid is not None:
                self._release([rid], f"launch {step}: pod_up {status} rc {rc}")
            self.emit("launch", {"event": "launch_failed", "step": step, "slot": slot,
                                 "why": f"pod_up {status} (rc {rc})", "gpu_index": idx})
            return "fail", 1
        self.emit("launch", {"event": "stock_out", "step": step, "slot": slot,
                             "gpu": C.GPU_ORDER[idx], "next": idx + 1})
        return "stock_out", None, None, rid

    def _launch_pod(self, step, stage, p, rid, info, attempts, rm, insert_loss, start) -> int:
        slot = p["slot"]
        pod_id = deadline = gpu_index = None
        for idx in range(len(C.GPU_ORDER)):
            with self._create_lock():
                veto = self._launch_veto(step, start)
                if veto is not None:
                    if rid is not None:
                        self._release([rid], f"launch {step}: {veto[0]}")
                    self.emit("launch", {"event": "launch_vetoed", "step": step, "slot": slot,
                                         "why": veto[0]})
                    return veto[1]
                r = self._create_one(step, stage, p, rid, rm, idx)
            ctx = getattr(self, "_launch_ctx", None)
            if ctx is not None:
                if len(r) > 3 and r[3]:
                    ctx["reservation_ids"].append(r[3])  # incl. re-reservations
                if r[0] == "ok" and r[1]:
                    ctx["pod_ids"].append(r[1])
            if r[0] == "ok":
                pod_id, deadline, gpu_index, rid = r[1], r[2], idx, r[3]
                break
            if r[0] == "stock_out":
                rid = r[3]
                continue
            return r[1]
        if pod_id is None:
            if rid is not None:
                self._release([rid], f"launch {step}: stock-out on every GPU type")
            self.emit("launch", {"event": "launch_failed", "step": step, "slot": slot,
                                 "why": "stock-out on every GPU type in GPU_ORDER"})
            return 1
        self.emit("launch", {"event": "launched", "step": step, "stage": stage, "slot": slot,
                             "pod_id": pod_id, "reservation_id": rid, "runs": p["runs"],
                             "kinds": {r: info[r]["kind"] for r in p["runs"]},
                             "specs": [job_spec(info[r]) for r in p["runs"]],
                             "budget_usd": p["budget_usd"], "gpu_index": gpu_index,
                             "gpu": list(C.GPU_ORDER[gpu_index]),
                             "pod_deadline_utc": utc(deadline) if deadline else None})
        for r in p["runs"]:
            self.write_run_status(r, status="launched", kind=info[r]["kind"], step=step,
                                  slot=slot, pod_id=pod_id, attempt=attempts[r])
        left = self.funded_seconds(pod_id)
        if left <= 0:
            return self._abandon(slot, "no funded time left before pod_push")
        rc, _ = self.run(self._push_cmd(slot, p["runs"], info), timeout=left)
        if rc != 0:  # every non-zero exit: clean up now; 4 (unarmed) is the most urgent
            why = PUSH_EXITS.get(rc, f"exited {rc}")
            if rc in (4, 124):
                self._alert(f"slot {slot} pod {pod_id}: pod_push {why}; cleaning up now")
            crc = self._abandon(slot, f"pod_push exit {rc}: {why}")
            self.emit("launch", {"event": "launch_failed", "step": step, "slot": slot,
                                 "pod_id": pod_id, "why": f"pod_push exit {rc}: {why}",
                                 "runs": p["runs"], "cleanup_rc": crc})
            return crc
        noise = any(info[r]["kind"] == "cs" for r in p["runs"])
        if noise and not self.push_noise_floor(slot):
            return self._abandon(slot, "could not push the noise-floor file")
        veto = self._launch_veto(step, start)  # nothing trains on a stale go decision
        if veto is not None:
            crc = self._abandon(slot, f"launch vetoed before the job start: {veto[0]}")
            return 3 if crc == 3 else veto[1]
        go = not STEPS[step].barrier or self._review_passed(step)
        if not self.start_job(slot, p["runs"], info, pod_id, go, insert_loss, noise):
            return self._abandon(slot, "the pod job did not start")
        return 0

    # -- the pod job, GO, probes ---------------------------------------------------------------

    def ssh(self, slot: str) -> list[str] | None:
        t = self.d.ssh_target(slot)
        if t is None:
            return None
        user, host, port = t
        return ["ssh", "-n", "-p", str(port), "-o", "ConnectTimeout=20", "-o",
                "StrictHostKeyChecking=accept-new", f"{user}@{host}"]

    def push_noise_floor(self, slot: str) -> bool:
        ssh = self.ssh(slot)
        if ssh is None:
            return False
        put = (f"mkdir -p {POD_WS}/{Path(NOISE_FLOOR_POD).parent} && "
               f"cat > {POD_WS}/{NOISE_FLOOR_POD} && echo noise_ok")
        remote = shlex.join([*[a for a in ssh if a != "-n"], put])
        local = shlex.quote(str(self.path(NOISE_FLOOR_LOCAL)))
        rc, out = self.run(["bash", "-c", f"{remote} < {local}"], timeout=90)
        ok = rc == 0 and ("noise_ok" in out or self.dry)
        self.emit("job", {"event": "noise_floor", "slot": slot, "ok": ok,
                          "sha256": sha256_file(self.path(NOISE_FLOOR_LOCAL))})
        return ok

    def start_job(self, slot, runs, info, pod_id, go: bool, insert_loss: bool,
                  noise: bool) -> bool:
        """Start pod_run_e9.sh detached (setsid nohup, rc file, log dir made first), then verify
        real progress after 15-20 s: the newest job log has its E9_JOB_START tag (never an
        immediate pgrep)."""
        ssh = self.ssh(slot)
        if ssh is None:
            self.emit("job", {"event": "job_started", "ok": False, "slot": slot,
                              "pod_id": pod_id, "why": f"no SSH address for slot {slot}"})
            return False
        envs = [f"RUNPOD_POD_ID={shlex.quote(pod_id)}"]
        if noise:
            envs.append(f"E9_NOISE_FLOOR_FILE={NOISE_FLOOR_POD}")
        job = shlex.join(self._job_argv(runs, info, insert_loss)[1:])  # without "bash"
        go_cmd = self._go_remote(runs) + " && " if go else ""
        remote = (
            f"cd {POD_WS} && mkdir -p logs && {go_cmd}"
            f"{{ setsid nohup env {' '.join(envs)} bash -c "
            f"{shlex.quote(f'bash {job}; echo $? > logs/e9_job{slot}.rc')} "
            f"> logs/e9_job{slot}.out 2>&1 </dev/null & }}; echo launched"
        )
        rc, out = self.run([*ssh, remote], timeout=90)
        if rc != 0 or "launched" not in out:
            self.emit("job", {"event": "job_started", "ok": False, "slot": slot,
                              "pod_id": pod_id, "why": f"ssh rc {rc}"})
            return False
        check = (f"cd {POD_WS} && f=$(ls -t logs/pod_run_e9.2*.log 2>/dev/null | head -1); "
                 "echo start:$( [ -n \"$f\" ] && grep -c E9_JOB_START \"$f\" || echo 0); "
                 f"cat logs/e9_job{slot}.rc 2>/dev/null | sed 's/^/rc:/'")
        ok, prog = False, ""
        for _ in range(2):  # one more look before declaring it dead
            self.d.sleep(self.d.job_wait_s)
            rc, prog = self.run([*ssh, check], timeout=90)
            m = re.search(r"start:(\d+)", prog)
            exited = re.search(r"^rc:(\d+)", prog, re.MULTILINE)
            ok = rc == 0 and bool(m) and int(m.group(1)) > 0 and not (
                exited and exited.group(1) != "0")
            if ok or exited:
                break
        self.emit("job", {"event": "job_started", "ok": ok, "slot": slot, "pod_id": pod_id,
                          "runs": runs, "progress": prog.strip()[-200:]})
        if ok and go:
            self.emit("go", {"event": "go_pushed", "slot": slot, "pod_id": pod_id, "runs": runs,
                             "at": "start"})
        return ok

    @staticmethod
    def _go_remote(runs: list[str]) -> str:
        parts = [f"mkdir -p {C.RUNS_DIR}/{r} && touch {C.RUNS_DIR}/{r}/{C.GO}" for r in runs]
        return " && ".join([*parts, f"touch {C.RESULTS}/{C.GO}"])

    def go(self, step: str) -> int:
        step = STEP_ALIASES.get(step, step)
        if step not in STEPS:
            raise SystemExit(f"--step must be one of {sorted(STEPS)}")
        refused = self.refuse_if_stopped("go", step=step)
        if refused is not None:
            return refused
        g = self.latest_gate(f"review:{step}")
        if g is None:
            return self.refuse("go", f"review:{step} not recorded (review pending)", step=step)
        if g[1].get("outcome") == "fail":
            return self.stop(STOP_REVIEW, f"review:{step}: {g[1].get('inputs')}", cmd="go")
        rs = self.review_state(step)
        if rs == "stale":
            return self.refuse("go", f"review:{step} approved other analysis versions "
                               f"({g[1].get('approves')}); a new review is needed", step=step)
        if rs != "pass":
            return self.refuse("go", f"review:{step} is {rs}", step=step)
        revoked = self.authorization_problems(step)
        if revoked:
            return self.refuse("go", f"authorization revoked: {revoked}", step=step)
        worst, pushed = 0, []
        for x in self.step_slots(step):
            if x.get("go"):
                continue
            ssh = self.ssh(x["slot"])
            if ssh is None:
                worst = 1
                continue
            remote = f"cd {POD_WS} && {self._go_remote(x['runs'])} && echo go_ok"
            rc, out = self.run([*ssh, remote], timeout=90)
            if rc == 0 and ("go_ok" in out or self.dry):
                self.emit("go", {"event": "go_pushed", "slot": x["slot"], "pod_id": x["pod_id"],
                                 "step": step, "runs": x["runs"]})
                pushed.append(x["slot"])
            else:
                worst = 1
        if not self.dry:
            append_jsonl(self.path(C.GATE_LOG), {"kind": "go", "step": step, "review_seq": g[0],
                                                 "slots": pushed, "utc": utc(self.d.now())})
        self.emit("go", {"event": "go_done", "step": step, "slots": pushed, "rc": worst})
        return worst

    def barrier_check(self) -> int:
        """§8.6: a step whose pods still wait at GO and whose accrued GPU spend reaches $10
        before its review returns has its pods terminated and settled; a relaunch is recorded
        (≈ $5, four setups), and its runs are not counted as failed."""
        b = self.d.budget
        running = {p["pod_id"] for p in self.running_pods()}
        waiting: dict[str, list[dict]] = {}
        for x in self.launches():
            if x["pod_id"] in running and not x.get("go") and STEPS[x["step"]].barrier:
                waiting.setdefault(x["step"], []).append(x)
        out, worst = [], 0
        for step, xs in sorted(waiting.items()):
            accrued = b.accrued_usd([x["pod_id"] for x in xs], self.d.now())
            due = b.barrier_kill_due(accrued, not self._review_passed(step))
            out.append({"step": step, "accrued_usd": accrued, "kill": due,
                        "slots": [x["slot"] for x in xs]})
            if not due or self.dry:
                continue
            for x in xs:
                rc = self.cleanup(x["slot"], f"$10 barrier kill ({step})", fetch=False,
                                  lost=False)
                worst = 3 if 3 in (rc, worst) else max(worst, rc)
                for r in x["runs"]:
                    self.write_run_status(r, status="barrier_killed",
                                          reason="$10 barrier kill before the review")
            self.emit("barrier-check", {"event": "relaunch_pending", "step": step,
                                        "runs": [r for x in xs for r in x["runs"]],
                                        "relaunch_usd": C.RELAUNCH_USD, "accrued_usd": accrued})
        self.emit("barrier-check", {"event": "barrier_check", "steps": out})
        return worst

    def probe(self, slot: str) -> int:
        """One consolidated ssh call: job alive (bracketed pgrep), the newest job log's E9_*
        tags, rc file, DONE/FAILED/GO per run. A failed call kills hung local ssh clients to
        the pod and probes once more before calling the pod unreachable."""
        x = self.slot_launch(slot) or {}
        ssh = self.ssh(slot)
        if ssh is None:
            return self.refuse("probe", f"no SSH address for slot {slot}")
        checks = " ; ".join(
            f"for f in {C.DONE} {C.FAILED} {C.GO}; do [ -e {C.RUNS_DIR}/{r}/$f ] && "
            f"echo {r}:$f; done" for r in x.get("runs", []))
        remote = (f"cd {POD_WS} && (pgrep -f '[p]od_run_e9.sh' >/dev/null && echo job:alive "
                  "|| echo job:dead); f=$(ls -t logs/pod_run_e9.2*.log 2>/dev/null | head -1); "
                  "[ -n \"$f\" ] && grep -o 'E9_\\(JOB\\|RUN\\)_[A-Z]* *[^ ]*' \"$f\" | tail -5; "
                  f"cat logs/e9_job{slot}.rc 2>/dev/null | sed 's/^/rc:/'; {checks or 'true'}")
        rc, out = self.run([*ssh, remote], timeout=90)
        retried = False
        if rc != 0:
            host = ssh[-1].split("@", 1)[-1]
            self.run(["pkill", "-f", f"[s]sh -n -p {ssh[3]} .*{host}"], timeout=30)
            retried = True
            rc, out = self.run([*ssh, remote], timeout=90)
        self.emit("probe", {"event": "probe", "slot": slot, "retried": retried,
                            "state": "reachable" if rc == 0 else "unreachable",
                            "lines": out.strip().splitlines()[-20:]})
        return 0 if rc == 0 else 1

    # -- re-arm and extension ------------------------------------------------------------------

    def rearm(self) -> int:
        if self.dry:
            self.emit("rearm", {"event": "plan", "slots": [p["slot"] for p in
                                                          self.running_pods()]})
            return 0
        try:
            res = self.d.budget.after_api_change(
                root=self.root, runner=self._budget_runner,
                cleanup=lambda slot, reason: self.cleanup(slot, reason))
        except Exception as e:  # noqa: BLE001 - RearmFailed: cleanup already ran
            self.emit("rearm", {"event": "rearm_failed", "error": f"{type(e).__name__}: {e}"})
            return 1
        self.emit("rearm", {"event": "rearm_ok", "result": res})
        return 0

    def extend(self, slot: str, usd: float) -> int:
        """A liability-raising extension of a running pod's deadline by `usd` at its rate: a new
        reservation for the increase through F1a (E9 lock, then the shared launch rule against
        every experiment's open reservations), then re-armed by pod_push --arm-only."""
        refused = self.refuse_if_stopped("extend", slot=slot)
        if refused is not None:
            return refused
        if not usd > 0:
            return self.refuse("extend", "--usd must be > 0")
        pod = self.slot_pod(slot)
        if pod is None:
            return self.refuse("extend", f"no running E9 pod in slot {slot}")
        q = self.d.rr.state(exp=C.EXP)["pods"].get(pod["pod_id"])
        if q is None or q.get("settled"):
            return self.refuse("extend", f"pod {pod['pod_id']} has no open reservation")
        new = int(max(float(q["allowed"]), self.d.now()) + usd / q["rate_usd_h"] * 3600)
        if self.dry:
            self.emit("extend", {"event": "plan", "slot": slot, "pod_id": pod["pod_id"],
                                 "new_deadline_utc": utc(new), "usd": usd})
            return 0
        try:
            rid = self.d.budget.extend(pod["pod_id"], new, f"orchestrator extend {slot}",
                                       **self.d.admission_kw())
        except SystemExit as e:
            return self.refuse("extend", f"limit refusal: {e}", slot=slot)
        except Exception as e:  # noqa: BLE001 - NotAdmitted, AdmissionError, ...
            return self.refuse("extend", f"{type(e).__name__}: {e}", slot=slot)
        rc, _ = self.run(["bash", "scripts/pod_push.sh", "--exp", C.EXP, "--slot", slot,
                          "--arm-only"])
        acked = self.d.rr.acked_deadline(pod["pod_id"])
        self.emit("extend", {"event": "extended", "slot": slot, "pod_id": pod["pod_id"],
                             "reservation_id": rid, "usd": usd, "new_deadline_utc": utc(new),
                             "armed_utc": utc(acked) if acked else None, "push_rc": rc,
                             "armed_at_new": acked == new})
        return 0 if rc == 0 else 1

    # -- fetch ---------------------------------------------------------------------------------

    def fetch(self, slot: str) -> int:
        """At most RETRIEVAL_ATTEMPTS pulls (the last with --tar), a few minutes apart, until no
        run of the slot needs a retry. pod_pull_results.sh verifies each DONE (hashes and the
        kind's inventory) before promoting; this records the outcome in each run's STATUS.json:
        verified, failed (the job's FAILED.json, or not retrieved after 3 attempts: eligible for
        one rerun), or left launched while unfinished. Never terminates."""
        x = self.slot_launch(slot)
        if not x:
            return self.refuse("fetch", f"no E9 launch recorded on slot {slot}")
        runs, pod_id = x["runs"], x["pod_id"]
        pending = [r for r in runs if self.run_status(r).get("status") != "verified"]
        res: dict[str, str] = {}
        attempts = 0
        for attempt in range(1, C.RETRIEVAL_ATTEMPTS + 1):
            if not pending:
                break
            attempts = attempt
            cmd = ["bash", "scripts/pod_pull_results.sh", "--exp", C.EXP, "--slot", slot,
                   "--pod-id", str(pod_id)]
            for r in pending:
                cmd += ["--run", r]
            if attempt == C.RETRIEVAL_ATTEMPTS:
                cmd.append("--tar")
            rc, out = self.run(cmd, timeout=self.d.retrieval_timeout_s)
            if self.dry:
                return 0
            res[f"_attempt{attempt}"] = f"rc {rc}"
            ok, failed = set(PULL_OK.findall(out)), set(PULL_FAILED.findall(out))
            unfinished = set(PULL_UNFINISHED.findall(out))
            retry = []
            for r in pending:
                done = read_json(self.path(f"{C.RUNS_DIR}/{r}/{C.DONE}"))
                if r in ok and isinstance(done, dict) and done.get("run") == r:
                    self.write_run_status(
                        r, status="verified", reason="DONE verified by pod_pull_results.sh",
                        attempts=attempt, pod_id=pod_id,
                        done_sha256=sha256_file(self.path(f"{C.RUNS_DIR}/{r}/{C.DONE}")))
                    self.merge_repos(r)
                    res[r] = "verified"
                elif r in failed:
                    self._fail_run(r, "the pod job wrote FAILED.json (partial records in "
                                   f"results/E9/_staging_pull_{pod_id}/runs/{r})")
                    res[r] = "failed (job)"
                elif r in unfinished:
                    res[r] = "unfinished"
                else:
                    retry.append(r)
                    res[r] = f"pull rc {rc}"
            pending = retry
            if pending and attempt < C.RETRIEVAL_ATTEMPTS:
                self.d.sleep(self.d.pull_wait_s)
        for r in pending:
            self._fail_run(r, f"not retrieved after {attempts} attempts ({res.get(r)})")
        self.emit("fetch", {"event": "fetched", "slot": slot, "pod_id": pod_id,
                            "attempts": attempts, "results": res, "unretrieved": pending})
        return 0 if all(res.get(r) == "verified" or self.run_status(r).get("status") ==
                        "verified" for r in runs) else 1

    def _fail_run(self, run: str, reason: str) -> None:
        n = int(self.run_status(run).get("attempt", 1))
        self.write_run_status(run, status="failed", reason=reason,
                              rerun_eligible=n < MAX_ATTEMPTS_PER_RUN)

    # -- repos and the HF audit ----------------------------------------------------------------

    def merge_repos(self, run: str) -> list[str]:
        """Merge results/E9/runs/<run>/hf_repos.json (a list of ids or of {repo|repo_id}, or
        {"repos": [...]}) into REPO_MANIFEST."""
        src = read_json(self.path(f"{C.RUNS_DIR}/{run}/hf_repos.json"))
        if src is None:
            return []
        items = src.get("repos", []) if isinstance(src, dict) else src
        repos = [x if isinstance(x, str) else (x.get("repo") or x.get("repo_id")) for x in items]
        man = read_json(self.path(C.REPO_MANIFEST), {"repos": []}) or {"repos": []}
        known = {e["repo"] for e in man["repos"]}
        for repo in filter(None, repos):
            if repo not in known:
                man["repos"].append({"repo": repo, "run": run, "utc": utc(self.d.now())})
                known.add(repo)
        if not self.dry:
            write_json(self.path(C.REPO_MANIFEST), man)
        return repos

    def audit_hf(self) -> int:
        runs_dir = self.path(C.RUNS_DIR)
        for f in sorted(runs_dir.glob("*/hf_repos.json")) if runs_dir.is_dir() else []:
            self.merge_repos(f.parent.name)
        man = read_json(self.path(C.REPO_MANIFEST), {"repos": []}) or {"repos": []}
        repos = [e["repo"] for e in man["repos"]]
        if self.dry:
            self.emit("audit-hf", {"event": "plan", "repos": repos})
            return 0
        found_public, unknown, rows = [], [], []
        for repo in repos:
            priv = self.d.hf_private(repo)
            row = {"repo": repo, "private": priv}
            if priv is False:
                try:
                    self.d.hf_make_private(repo)
                    row["made_private"] = self.d.hf_private(repo) is True
                except Exception as e:  # noqa: BLE001
                    row["made_private"] = False
                    row["error"] = f"{type(e).__name__}: {e}"
                found_public.append(repo)
            elif priv is None:
                unknown.append(repo)
            rows.append(row)
        self.emit("audit-hf", {"event": "audit", "repos": rows, "public": found_public,
                               "unknown": unknown})
        if found_public:
            return self.stop(STOP_HF_PUBLIC, f"made private: {found_public}", cmd="audit-hf")
        return 1 if unknown else 0

    # -- cleanup -------------------------------------------------------------------------------

    def _unsettled(self) -> list[dict]:
        try:
            return [u for u in self.d.unsettled() if u.get("pod_id")]
        except Exception as e:  # noqa: BLE001
            print(f"unsettled_pods failed: {e}", file=sys.stderr)
            return []

    def _pod_state(self, pod_id: str, rid: str | None) -> tuple[bool, bool]:
        running = any(s["pod_id"] == pod_id and s["running"] for s in self.pod_sessions())
        if not rid:
            return not running, True
        try:
            p = self.d.rr.state(exp=C.EXP)["pods"].get(pod_id)
            settled = p is None or bool(p.get("settled"))
        except Exception:  # noqa: BLE001 - unknown is not settled
            settled = False
        return not running, settled

    def _down(self, t: dict) -> tuple[bool, bool]:
        """pod_down.py --exp E9 (DELETE, then charge and settle under the admission lock),
        retried; returns (terminated, settled)."""
        pid, rid, slot = t["pod_id"], t.get("reservation_id"), t.get("slot")
        base = ["uv", "run", "python", "scripts/pod_down.py", "--exp", C.EXP]
        if rid:
            cmd = [*base, "--pod-id", pid, "--reservation", rid]
            if t.get("running_slot") and slot:
                cmd += ["--slot", slot]
        else:
            cmd = [*base, "--slot", slot]
        cmd += ["--reason", t.get("reason", "e9_orchestrate cleanup")]
        state = self._pod_state(pid, rid)
        if all(state):
            return state
        rc = None
        for attempt in range(1, TERMINATE_ATTEMPTS + 1):
            rc, _ = self.run(cmd, timeout=self.d.terminate_timeout_s)
            if self.dry:
                return True, True
            state = self._pod_state(pid, rid)
            if rc == 0 and all(state):
                return state
            if rc == POD_DOWN_UNSETTLED:  # terminated, settlement failed: keep, rerun later
                self._alert(f"pod {pid} terminated but NOT SETTLED; its session and slot are "
                            f"kept: rerun `pod_down.py --exp E9 --pod-id {pid} --reservation "
                            f"{rid}` (or e9_orchestrate.py cleanup --slot {slot})")
                return True, False
            if attempt < TERMINATE_ATTEMPTS:  # 1: a termination failed; 2/other: retry too
                self.d.sleep(self.d.terminate_wait_s)
        if rc == 0:  # pod_down says done; trust it over a lagging local view
            return True, True
        self._alert(f"pod {pid} NOT CONFIRMED TERMINATED after {TERMINATE_ATTEMPTS} pod_down "
                    f"attempts (last exit {rc}); it may still bill")
        return state

    def _alert(self, msg: str) -> None:
        print(f"E9 ALERT: {msg}", file=sys.stderr, flush=True)
        try:
            self.emit("alert", {"event": "alert", "message": msg})
        except Exception as e:  # noqa: BLE001 - the alert itself is best effort
            print(f"E9 ALERT not logged: {e}", file=sys.stderr)

    def _close_reservation(self, rid: str | None) -> str:
        if rid is None:
            return "no reservation"
        R = self.d.rr.get(rid)
        if R is None:
            return "unknown reservation"
        if R.get("closed"):
            return "closed"
        if self.dry:
            return "would release"
        if not R.get("launched"):
            if R.get("creating"):  # a create request may have made a pod: never release blind
                return "kept: creating row unresolved (pod_down.py --exp E9 --untracked)"
            self.d.rr.release(rid, note="e9_orchestrate cleanup")
            return "released"
        st = self.d.rr.state(exp=C.EXP)["pods"]
        open_pods = [p for p in R.get("pods", []) if not st.get(p, {}).get("settled")]
        if open_pods:
            return f"kept: pod(s) {open_pods} not settled"
        if not R.get("released"):
            self.d.rr.release(rid, note="e9_orchestrate cleanup")
        return "released (unused part)"

    def _cleanup_targets(self, slot: str, x: dict, errors: list[str]) -> dict[str, dict]:
        """The pods one cleanup must terminate and settle, captured before anything else: the
        slot's running pod and every unsettled pod of its reservation or slot."""
        rid = x.get("reservation_id")
        targets: dict[str, dict] = {}
        try:
            pod = self.slot_pod(slot)
        except Exception as e:  # noqa: BLE001
            pod = None
            errors.append(f"pod log: {type(e).__name__}: {e}")
        if pod is not None:
            targets[pod["pod_id"]] = {"pod_id": pod["pod_id"], "slot": slot,
                                      "reservation_id": rid if x.get("pod_id") == pod["pod_id"]
                                      else None, "running_slot": True}
        elif x.get("pod_id") and not x.get("cleaned"):
            targets[x["pod_id"]] = {"pod_id": x["pod_id"], "slot": slot, "reservation_id": rid,
                                    "running_slot": True}
        for u in self._unsettled():
            if (rid and u.get("reservation_id") == rid) or u.get("slot") == slot:
                targets[u["pod_id"]] = {**targets.get(u["pod_id"], {}), **u,
                                        "running_slot": u["pod_id"] in targets}
        return targets

    def cleanup(self, slot: str, reason: str = "cleanup", fetch: bool = True,
                lost: bool = True, outcome: str | None = None) -> int:
        """The unconditional path (§8.7). Pod identities are captured first; then a bounded
        fetch (<= 3 attempts; skipped with fetch=False) when the job started and a run is not
        settled yet; then, in `finally` and whatever happened before, every target pod is
        terminated and settled (each isolated), the reservations closed, and every run of the
        slot neither verified nor failed marked failed (records lost; eligible for one rerun).
        Retrieval and bookkeeping errors are reported after every termination was attempted."""
        errors: list[str] = []
        try:
            x = self.slot_launch(slot) or {}
        except Exception as e:  # noqa: BLE001
            x = {}
            errors.append(f"orchestrator log: {type(e).__name__}: {e}")
        targets = self._cleanup_targets(slot, x, errors)
        rid = x.get("reservation_id")
        res = {"event": "cleaned", "slot": slot, "reason": reason, "outcome": outcome,
               "reservation_id": rid, "fetch_rc": None, "pod_id": x.get("pod_id") or next(
                   iter(targets), None), "pods": [], "terminated": True, "settled": True,
               "reservations": {}, "lost_runs": []}
        try:
            alive = {q["pod_id"] for q in self.running_pods()} & set(targets)
            if fetch and x.get("job") and not x.get("cleaned") and alive and any(
                self.run_status(r).get("status") not in ("verified", "failed")
                for r in x.get("runs", [])
            ):
                res["fetch_rc"] = self.fetch(slot)
        except Exception as e:  # noqa: BLE001 - retrieval never blocks termination
            errors.append(f"fetch: {type(e).__name__}: {e}")
        finally:
            for t in targets.values():
                try:
                    term, sett = self._down({**t, "reason": f"e9 cleanup: {reason}"[:120]})
                except Exception as e:  # noqa: BLE001 - the next pod is still terminated
                    term = sett = False
                    errors.append(f"terminate {t['pod_id']}: {type(e).__name__}: {e}")
                res["pods"].append({"pod_id": t["pod_id"], "terminated": term,
                                    "settled": sett})
                res["terminated"] &= term
                res["settled"] &= sett
            for r in sorted({rid} | {t.get("reservation_id") for t in targets.values()}
                            - {None}):
                try:
                    res["reservations"][r] = ("kept: pod not confirmed terminated"
                                              if not res["terminated"]
                                              else self._close_reservation(r))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"reservation {r}: {type(e).__name__}: {e}")
        try:
            if lost and x and not x.get("cleaned"):
                for r in x.get("runs", []):
                    if self.run_status(r).get("status") not in ("verified", "failed",
                                                                 "barrier_killed"):
                        self._fail_run(r, f"records lost: not verified before cleanup ({reason})")
                        res["lost_runs"].append(r)
        except Exception as e:  # noqa: BLE001
            errors.append(f"run status: {type(e).__name__}: {e}")
        res["errors"] = errors
        res["unsettled"] = [q["pod_id"] for q in res["pods"] if q["terminated"]
                            and not q["settled"]]  # kept: pod_down is rerun later
        if not res["terminated"]:
            print(f"E9 CLEANUP: a pod of slot {slot} is NOT CONFIRMED TERMINATED; its "
                  f"reservation is kept. pod_down.py --exp E9 --slot {slot}", file=sys.stderr)
        try:
            self.emit("cleanup", res)
        except Exception as e:  # noqa: BLE001
            print(f"E9 CLEANUP: could not log the cleanup: {e}", file=sys.stderr)
        if not res["terminated"]:
            return 3
        return 0 if res["settled"] and not errors else 1

    # -- branches ------------------------------------------------------------------------------

    def branches(self) -> dict:
        return read_json(self.path(BRANCHES_FILE), {}) or {}

    def set_branch(self, name: str, status: str, reason: str = "") -> dict:
        name = BRANCH_ALIASES.get(name, name)
        if name not in BRANCHES:
            raise SystemExit(f"--branch must be one of {BRANCHES} (or {sorted(BRANCH_ALIASES)})")
        if status not in C.DISPOSITIONS:
            raise SystemExit(f"--status must be one of {C.DISPOSITIONS}")
        if status in ("dropped", "stopped") and not reason:
            raise SystemExit(f"a {status} branch needs --reason")
        with _flock(self.path("logs/E9-branches.lock")):
            b = self.branches()
            rec = {"status": status, "reason": reason, "utc": utc(self.d.now())}
            b[name] = {**rec, "history": [*b.get(name, {}).get("history", []), rec]}
            if not self.dry:
                write_json(self.path(BRANCHES_FILE), b)
        self.emit("dispose", {"event": "branch", "branch": name, **rec})
        return b[name]

    # -- status block (§8.9, exactly its format) -----------------------------------------------

    def _reviews(self) -> str:
        d = self.path("logs/reviews")
        arts: dict[str, tuple[int, str]] = {}
        for f in sorted(d.glob("*E9*-r[0-9]*.md")) if d.is_dir() else []:
            m = re.match(r"^(.*)-r(\d+)(\.prompt)?\.md$", f.name)
            if not m:
                continue
            art, n, prompt = m.group(1), int(m.group(2)), bool(m.group(3))
            if prompt and (d / f"{art}-r{n}.md").exists():
                continue
            if prompt:
                verdict = "pending"
            else:
                vm = re.search(r"(?i)verdict[:*\s]*([A-Z][A-Z_ -]+)", f.read_text(errors="ignore"))
                verdict = vm.group(1).split()[0].strip(" .*").upper() if vm else "done"
            if n >= arts.get(art, (0, ""))[0]:
                arts[art] = (n, f"r{n} {verdict}" if verdict != "pending" else "pending")
        recent = sorted(arts)[-3:]
        return ", ".join(f"{a.split('-', 1)[-1]}: {arts[a][1]}" for a in recent) or "none"

    def status_block(self, phase=None, gate=None, stage=None, reviews=None,
                     next_action=None) -> str:
        sp = self.d.spend
        t = sp.totals(C.EXP)
        launches = self.launches()
        last_step = launches[-1]["step"] if launches else None
        st_stopped = self.stopped()
        if stage is None:
            stage = STEPS[last_step].stage if last_step else "setup"
        grp = C.STAGE_GROUP[stage]
        try:
            snap = self.d.budget.committed()
            g = snap["groups"]
            recorded = sum(v["recorded_gpu"] + v["recorded_api"] for v in g.values())
            reserved = sum(v["api_reserved"] + v["gpu_commit"] for v in g.values())
            spend_line = (f"spend: ${recorded:.2f} recorded + ${reserved:.2f} reserved of "
                          f"${C.HARD_CAP_USD:.0f} (stage {stage}: ${g[grp]['total']:.2f} of "
                          f"${C.STAGE_LIMITS_USD[grp]:.0f})")
        except Exception as e:  # noqa: BLE001 - AdmissionError: the books need a hand fix
            spend_line = (f"spend: $? recorded + $? reserved of ${C.HARD_CAP_USD:.0f} (stage "
                          f"{stage}: $? of ${C.STAGE_LIMITS_USD[grp]:.0f}; books: "
                          f"{type(e).__name__})")
        if phase is None:
            phase = "stopped" if st_stopped else (STEPS[last_step].phase if last_step else
                                                  "setup")
        if gate is None:
            gl = [x for x in self.gates() if x.get("kind") == "gate"]
            if gl:
                x = gl[-1]
                inp = x.get("inputs") or {}
                key = inp.get("key") if isinstance(inp.get("key"), str) else ", ".join(
                    f"{k}={v}" for k, v in list(inp.items())[:3]
                    if isinstance(v, (int, float, str)) and len(str(v)) < 40)
                gate = f"{x['gate']} = {x['outcome']} ({key or 'see gates.jsonl'})"
            else:
                gate = "none = n/a (no gate yet)"
        rp = self.runpod_lines()
        bal = self.d.balance()
        balance = f"${bal:.2f}" if bal is not None else "$?"
        pods = self.running_pods()
        ids = ", ".join(f"e9-{p['slot']}:{p['pod_id']}" for p in pods) or "none"
        branches = "; ".join(
            f"{n}: {b['status']}" + (f": {b['reason']}" if b["status"] in ("dropped", "stopped")
                                      else "") for n, b in sorted(self.branches().items()))
        if next_action is None:
            next_action = f"stopped: {st_stopped.get('rule')}" if st_stopped else "see run log"
        pend = [q for q in self.outstanding_settlements() if q.get("terminated")]
        if pend:
            next_action += "; settle pending: " + ", ".join(
                str(q.get("pod_id", q.get("error"))) for q in pend)
        return "\n".join([
            f"E9 STATUS | phase: {phase} | gate: {gate}",
            spend_line,
            (f"API: ${float(t['judge_usd']):.2f} settled; outstanding batches "
             f"{len(sp.open_reservations(C.EXP))}, liability "
             f"${float(t['judge_reserved_usd']):.2f}"),
            (f"RunPod: balance {balance}; E9 unlaunched reservations "
             f"${rp['unlaunched_usd']:.2f}; launched-pod liability ${rp['liability_usd']:.2f}"),
            f"pods running: {len(pods)} ({ids}) | reviews: {reviews or self._reviews()}",
            f"branches: {branches or 'none'} | next action: {next_action}",
        ])

    def status(self, **kw) -> int:
        block = self.status_block(**kw)
        print(block, flush=True)
        if not self.dry:
            append_jsonl(self.path(ORCH_LOG), {"utc": utc(self.d.now()), "cmd": "status",
                                               "event": "status", "block": block})
        return 0


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    ap = argparse.ArgumentParser(prog="e9_orchestrate.py", description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print what would be done")
    sub = ap.add_subparsers(dest="cmd", required=True)
    steps = sorted([*STEPS, *STEP_ALIASES])
    sub.add_parser("project")
    s = sub.add_parser("status")
    s.add_argument("--phase")
    s.add_argument("--gate")
    s.add_argument("--stage", choices=C.STAGES)
    s.add_argument("--reviews")
    s.add_argument("--next", dest="next_action")
    s = sub.add_parser("launch")
    s.add_argument("--step", required=True, choices=steps)
    s.add_argument("--runs", required=True,
                   help="RUN:KIND or RUN:ARM:CPT_SEED:KIND:STREAM, comma-separated")
    s.add_argument("--slot-map", help="_A=R1+R2,_B=R3 (runs in order per pod)")
    s.add_argument("--insert-loss", action="store_true", help="the job's --insert-loss (smoke)")
    s = sub.add_parser("go")
    s.add_argument("--step", required=True, choices=steps)
    sub.add_parser("barrier-check")
    sub.add_parser("rearm")
    s = sub.add_parser("extend")
    s.add_argument("--slot", required=True)
    s.add_argument("--usd", type=float, required=True)
    for name in ("probe", "fetch"):
        sub.add_parser(name).add_argument("--slot", required=True)
    s = sub.add_parser("cleanup")
    s.add_argument("--slot", required=True)
    s.add_argument("--reason", default="cleanup")
    s.add_argument("--no-fetch", action="store_true")
    s.add_argument("--outcome", choices=("done", "aborted"))
    s = sub.add_parser("stop")
    s.add_argument("--rule", required=True)
    s.add_argument("--detail", default="")
    s.add_argument("--no-fetch", action="store_true")
    s = sub.add_parser("gate")
    s.add_argument("--name")
    s.add_argument("--outcome", choices=C.GATE_OUTCOMES)
    s.add_argument("--inputs", default="{}", help="a JSON object, or @path to one")
    s.add_argument("--record", help="a G1 analysis record (results/E9/analysis/<name>.json)")
    sub.add_parser("audit-hf")
    s = sub.add_parser("dispose")
    s.add_argument("--branch", required=True)
    s.add_argument("--status", required=True, choices=C.DISPOSITIONS)
    s.add_argument("--reason", default="")
    a = ap.parse_args(argv)
    o = Orchestrator(deps or real_deps(), dry=a.dry_run)
    if a.cmd == "project":
        return o.project()
    if a.cmd == "status":
        return o.status(phase=a.phase, gate=a.gate, stage=a.stage, reviews=a.reviews,
                        next_action=a.next_action)
    if a.cmd == "launch":
        return o.launch(a.step, a.runs, a.slot_map, a.insert_loss)
    if a.cmd == "go":
        return o.go(a.step)
    if a.cmd == "barrier-check":
        return o.barrier_check()
    if a.cmd == "rearm":
        return o.rearm()
    if a.cmd == "extend":
        return o.extend(a.slot, a.usd)
    if a.cmd == "probe":
        return o.probe(a.slot)
    if a.cmd == "fetch":
        return o.fetch(a.slot)
    if a.cmd == "cleanup":
        return o.cleanup(a.slot, a.reason, fetch=not a.no_fetch, outcome=a.outcome)
    if a.cmd == "stop":
        return o.stop(a.rule, a.detail, fetch=not a.no_fetch)
    if a.cmd == "gate":
        raw = a.inputs
        if raw.startswith("@"):
            raw = Path(raw[1:]).read_text()
        inputs = json.loads(raw)
        if not isinstance(inputs, dict):
            raise SystemExit("--inputs must be a JSON object")
        return o.gate(a.name, a.outcome, inputs, a.record)
    if a.cmd == "audit-hf":
        return o.audit_hf()
    if a.cmd == "dispose":
        o.set_branch(a.branch, a.status, a.reason)
        return 0
    raise SystemExit(f"unknown command {a.cmd}")


if __name__ == "__main__":
    sys.exit(main())
