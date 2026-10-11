"""Shared RunPod REST client and pod bookkeeping, per experiment (E1, I1a, E1b).

Ported from ../mats12_neel_working/scripts/pod_up.py, pod_down.py and pod_budget_check.py
(their inline httpx calls, `set_env_var` and the creation-to-termination spend projection),
collected here so every script shares one client. Changes from the source:

- Every request goes through `RunPod._request`, which always sends a browser-like User-Agent
  (RunPod's Cloudflare front blocks default library agents with error 1010; CLAUDE.md gotcha).
- Retries on connection errors, 429 and 5xx, except `POST /pods`, which is sent exactly once
  (a lost response may still have created a pod; the caller reconciles against the API list,
  see `CreateUncertain` and `untracked_pods`). `dry_run=True` prints the exact request (the
  Authorization header redacted) and sends nothing.
- GPU fallback order and rates follow implementation-plan §5 and CLAUDE.md.
- Pod lifecycle events go to `logs/pods_<EXP>.jsonl` (bookkeeping only: creation time, rate,
  slot). Dollars go to `logs/costs.jsonl` only through `emx.spend`, imported lazily.
- 2026-09-29 (I1a implementation plan §2 U1; gpt-6-sol code review U1 r1): generalised from
  E1-only. Scripts call `configure(exp)` from their own `--exp` argument (default "E1"; never
  from the ambient environment, so an exported EMX_EXP cannot redirect the E1 watchdog). It sets
  `EXP`, the pod-name prefix (`e1-`, `i1a-`), the pod-events file and the caps
  (`emx.spend.caps_for`). I1a adds checkpoint approval markers (`logs/approvals/<EXP>-P<n>.approved`),
  a watchdog heartbeat that must be fresh before an I1a launch, and the P0 GPU ceiling while
  no P1 approval exists. E1 behaviour is unchanged.
- 2026-10-03 (ideas from github.com/kmerkelbach/runpod-tools docs/gotchas.md, reimplemented):
  `killswitch_deadline` (the pod-side stop time pod_push.sh arms, scripts/pod_killswitch.sh) and
  `forget_host_key` (RunPod reuses ip:port pairs, so a stale known_hosts key blocks accept-new).
- 2026-10-03 (E1b implementation plan §2 U7): E1b needs no code here, only its caps in
  `emx.spend.CAPS`: `configure("E1b")` names pods `e1b-<slot>` (never matched by E1's `e1-`),
  logs to logs/pods_E1b.jsonl, and kills at min($56, $75 - judges). Its two concurrent pods
  (_A Part 1, _B Part 3) share that line in `killswitch_deadline`; like I1a it needs a live
  watchdog heartbeat (scripts/e1b_watchdog.sh); it has no approval-gated ceiling.
- 2026-10-06 (E3 implementation plan v2 fix 5, v3 fixes 4-5, v4 fix 3; shared contract
  docs/experiments/shared/runpod-reservations.md): `gpu_kill_limit()` with no argument delegates
  to `emx.spend.watchdog_kill_usd(EXP)` (recorded API spend plus open API reservations), and
  `killswitch_deadline` uses the lower of that and the legacy figure, so the global backstop
  counts open API reservations. `--killswitch-deadline --per-slot` also prints
  `EMX_KILLSWITCH_DEADLINE_<slot>=<epoch>` per running pod: `emx.runpod_reservations.rearm_target`
  (min of the pod's reservation deadline, its acknowledged deadline and the global deadline; never
  later than what is armed). GPU charging is idempotent and recoverable: the charge's note
  carries `pod=<id>`, and `finish_session` records a missing charge for a session already marked
  terminated (a crash between the two) but never a second one.
- 2026-10-06 (E3 code review A r1 finding 10): the charge is also concurrency-idempotent: the
  ledger check and append are one critical section (`emx.spend.record_once`, key
  `gpu_charge_key(pod_id)`), and `pod_sessions` ends a session at its FIRST `terminated` event,
  so two concurrent cleanups compute the same session and record one charge.
- 2026-10-06 (E9 implementation plan v4, unit F2; prereg §7.3, §8.7; additive, other experiments
  unchanged): E9 has stage limits (`emx.spend` refuses an untagged E9 row), so a pod's `created`
  event carries its `stage` (pod_up.py --stage), `pod_sessions` passes it on, and
  `ledger_record_gpu` charges with it (`session_stage`: the event's stage, else the stage in its
  reservation's note `E9:<stage>:...`, else "setup", flagged in the note). `ledger_check` takes
  an optional `stage`. E9's pods are armed at their OWN budget deadline, never at the global
  line (§7.3: the backstop "is never the armed deadline of a pod"), so `per_slot_deadlines`
  gives each E9 pod `rearm_target(pod, +inf)` (its reservation's allowed and acknowledged
  deadlines only). The backstop kill line, min($145, $200 - current API reserve - API recorded
  and reserved), is enforced by the laptop watchdog (scripts/e9_watchdog.sh) over all E9 pods
  through `e9_backstop_usd` / `e9_watchdog_status` (`python -m emx.runpod --exp E9
  --e9-budget-check`: one JSON line, exit 2 = hard stop).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from emx.spend import Caps, caps_for  # bound at import: tests may swap emx.spend for a fake

API = "https://rest.runpod.io/v1"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
DEFAULT_EXP = "E1"
EXP = DEFAULT_EXP  # set by configure(); scripts pass their --exp argument
POD_NAME_PREFIX = "e1-"  # every pod is named <exp>-<slot>; pod_down --all-exp matches on it

# CUDA 13.0 devel image on Ubuntu 24.04 (system Python 3.12). uv.lock resolves torch 2.12.1 /
# 2.13.0 against CUDA 13 wheels, so the host driver must support CUDA 13 (see ALLOWED_CUDA).
# Digest pinned 2026-09-29 from Docker Hub (tag last updated 2026-09-25).
IMAGE = (
    "runpod/pytorch:1.3.3-cu1300-torch2121-ubuntu2404"
    "@sha256:5f36564b9f8dd873f569143afc5035182116748bb7fffab0ed305b2072271ae9"
)
ALLOWED_CUDA = ("13.0",)
# RunPod container-registry credentials (a Docker Hub login, named "mats"; added by Jacob
# 2026-10-04 after an E1b pod was stopped by Docker Hub's anonymous pull rate limit:
# IMAGE_AUTH_ERROR toomanyrequests). The id is not a secret; the login stays in RunPod.
REGISTRY_AUTH_ID = "cmuty4b9y000yncaxfl3wf2l3"


@dataclass(frozen=True)
class GpuOption:
    gpu_type_id: str
    cloud: str  # "COMMUNITY" | "SECURE"
    rate_usd_h: (
        float  # CLAUDE.md measured rate (upper end when a range); used until the API reports
    )


# implementation-plan §5: H100 community -> H100 NVL -> H100 secure -> A100-SXM4 80GB
GPU_FALLBACK: tuple[GpuOption, ...] = (
    GpuOption("NVIDIA H100 80GB HBM3", "COMMUNITY", 2.69),
    GpuOption("NVIDIA H100 NVL", "COMMUNITY", 2.59),
    GpuOption("NVIDIA H100 80GB HBM3", "SECURE", 3.49),
    GpuOption("NVIDIA A100-SXM4-80GB", "SECURE", 1.39),
)
FALLBACK_RATE = max(g.rate_usd_h for g in GPU_FALLBACK)  # conservative when a rate is unknown

ROOT = Path(__file__).resolve().parents[2]
# EMX_POD_LOG_DIR sandboxes the pod event logs for subprocess tests only (the experiment itself is
# never taken from the environment); unset in real use.
LOGS = Path(os.environ.get("EMX_POD_LOG_DIR") or ROOT / "logs")
POD_EVENTS = LOGS / f"pods_{EXP}.jsonl"
SLOT_RE = re.compile(r"_[A-Za-z0-9]{1,8}")


def configure(exp: str | None = None) -> str:
    """Point this module at one experiment: EXP, POD_NAME_PREFIX and POD_EVENTS.

    `exp=None` means "E1". The environment is deliberately NOT consulted (review U1 r1: an
    exported EMX_EXP must not redirect E1 tooling). Unknown experiments are refused (no caps)."""
    global EXP, POD_NAME_PREFIX, POD_EVENTS
    exp = exp or DEFAULT_EXP
    caps_for(exp)  # raises KeyError for an experiment without caps
    if exp == EXP and POD_NAME_PREFIX == f"{exp.lower()}-":
        return exp  # unchanged: keep POD_EVENTS as set (tests redirect it to a sandbox)
    EXP = exp
    POD_NAME_PREFIX = f"{exp.lower()}-"
    POD_EVENTS = LOGS / f"pods_{exp}.jsonl"
    return exp


def caps() -> Caps:
    return caps_for(EXP)


configure()


class RunPodError(RuntimeError):
    pass


class CreateUncertain(RunPodError):
    """`POST /pods` was sent but its outcome is unknown (connection error, timeout, 5xx): a pod
    may exist. Never retry; list the pods and reconcile (`untracked_pods`)."""


def headers(api_key: str) -> dict[str, str]:
    """Headers for every RunPod request. The User-Agent is not optional."""
    return {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


class RunPod:
    def __init__(
        self,
        api_key: str,
        *,
        dry_run: bool = False,
        session: requests.Session | None = None,
        retries: int = 4,
        backoff_s: float = 2.0,
        out=None,
    ) -> None:
        if not api_key:
            raise RunPodError("RUNPOD_API_KEY is empty")
        self._key = api_key
        self.dry_run = dry_run
        self.session = session or requests.Session()
        self.retries = retries
        self.backoff_s = backoff_s
        self.out = out or sys.stdout

    # -- transport ---------------------------------------------------------------------------

    def describe(self, method: str, path: str, body: dict | None = None) -> str:
        """The exact request, Authorization redacted (what --dry-run prints)."""
        h = dict(headers(self._key))
        h["Authorization"] = "Bearer <redacted>"
        req = {"method": method, "url": f"{API}{path}", "headers": h}
        if body is not None:
            req["json"] = body
        return json.dumps(req, indent=2)

    def _request(
        self, method: str, path: str, body: dict | None = None, *, retry: bool = True
    ) -> Any:
        if self.dry_run:
            print(f"DRY RUN (not sent):\n{self.describe(method, path, body)}", file=self.out)
            return None
        last: str = ""
        for attempt in range(self.retries + 1):
            try:
                r = self.session.request(
                    method, f"{API}{path}", headers=headers(self._key), json=body, timeout=60
                )
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"
                if not retry:
                    raise CreateUncertain(f"{method} {path} outcome unknown: {last}") from e
            else:
                if r.status_code < 400:
                    return r.json() if r.content else None
                last = f"HTTP {r.status_code}: {r.text[:500]}"
                if r.status_code != 429 and r.status_code < 500:
                    raise RunPodError(f"{method} {path} failed: {last}")
                if not retry:
                    if r.status_code >= 500:
                        raise CreateUncertain(f"{method} {path} outcome unknown: {last}")
                    raise RunPodError(f"{method} {path} failed (not retried): {last}")
            if attempt < self.retries:
                time.sleep(self.backoff_s * 2**attempt)
        raise RunPodError(f"{method} {path} failed after {self.retries + 1} tries: {last}")

    # -- endpoints ---------------------------------------------------------------------------

    def list_pods(self) -> list[dict]:
        return self._request("GET", "/pods") or []

    def get_pod(self, pod_id: str) -> dict:
        return self._request("GET", f"/pods/{pod_id}")

    def create_pod(self, body: dict) -> dict | None:
        """Sent exactly once (not idempotent). Raises CreateUncertain when a pod may exist."""
        return self._request("POST", "/pods", body, retry=False)

    def terminate_pod(self, pod_id: str) -> None:
        self._request("DELETE", f"/pods/{pod_id}")


# --- request bodies -----------------------------------------------------------------------------


def pod_body(
    gpu: GpuOption,
    slot: str,
    public_key: str,
    *,
    container_disk_gb: int = 200,
    allowed_cuda: tuple[str, ...] = ALLOWED_CUDA,
) -> dict:
    """Create-pod body (port of pod_up.py's community branch: no network volume).

    Only non-secret settings go in `env`; HF_TOKEN travels via pod_push.sh. Container disk holds
    three venvs, the model cache and adapters, so it is larger than the working repo's 80 GB.
    """
    check_slot(slot)
    return {
        "name": f"{POD_NAME_PREFIX}{slot.lstrip('_').lower()}",
        "imageName": IMAGE,
        "containerRegistryAuthId": REGISTRY_AUTH_ID,  # authenticated Docker Hub pulls
        "cloudType": gpu.cloud,
        "gpuTypeIds": [gpu.gpu_type_id],
        "gpuCount": 1,
        "containerDiskInGb": container_disk_gb,
        "allowedCudaVersions": list(allowed_cuda),
        "supportPublicIp": True,  # community hosts without a public IP are unreachable by SSH
        "ports": ["22/tcp"],
        "env": {
            "PUBLIC_KEY": public_key,  # REST-created pods must pass it explicitly
            "HF_HOME": "/ws/hf_cache",
            "WANDB_MODE": "offline",
            "TOKENIZERS_PARALLELISM": "false",
        },
    }


def check_slot(slot: str) -> str:
    if not SLOT_RE.fullmatch(slot):
        raise ValueError(f"slot must look like _A / _T1 (got {slot!r})")
    return slot


def gpu_options(start: int = 0) -> tuple[GpuOption, ...]:
    """The fallback order from position `start` (0 = H100 community)."""
    return GPU_FALLBACK[start:]


# Probe tier (2026-10-11, additive): a probe may declare its own fallback order and rates in
# probes/P<NN>-*/probe.toml as `[pod].gpus = [[gpu_type_id, cloud, rate_usd_h], ...]`, e.g. for a
# B200/B300 pod. Without the key a probe uses GPU_FALLBACK, exactly as before.
GPU_CLOUDS = ("COMMUNITY", "SECURE")
MAX_LISTED_RATE_USD_H = 30.0  # a sanity bound on a declared rate, not a price


def parse_gpu_list(value: Any, where: str = "[pod].gpus") -> tuple[GpuOption, ...]:
    """A declared fallback order: a non-empty list of [gpu_type_id, cloud, rate_usd_h] triples
    (gpu_type_id a non-empty string, cloud COMMUNITY or SECURE, 0 < rate <= 30). ValueError
    otherwise."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list of [gpu_type_id, cloud, rate_usd_h]")
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, list) or len(item) != 3:
            raise ValueError(f"{where}[{i}] must be [gpu_type_id, cloud, rate_usd_h], got {item!r}")
        gpu, cloud, rate = item
        if not isinstance(gpu, str) or not gpu.strip():
            raise ValueError(f"{where}[{i}]: gpu_type_id must be a non-empty string, got {gpu!r}")
        if cloud not in GPU_CLOUDS:
            raise ValueError(f"{where}[{i}]: cloud must be one of {GPU_CLOUDS}, got {cloud!r}")
        if (
            isinstance(rate, bool)
            or not isinstance(rate, int | float)
            or not 0 < rate <= MAX_LISTED_RATE_USD_H
        ):
            raise ValueError(
                f"{where}[{i}]: rate_usd_h must be a number in "
                f"(0, {MAX_LISTED_RATE_USD_H:g}], got {rate!r}"
            )
        out.append(GpuOption(gpu, cloud, float(rate)))
    return tuple(out)


def probe_gpu_options(exp: str | None = None) -> tuple[GpuOption, ...] | None:
    """A probe's declared `[pod].gpus` order (from its probe.toml through
    `emx.spend.probe_caps_path`), or None when `exp` is not a probe id or declares none (then the
    caller keeps GPU_FALLBACK)."""
    exp = exp or EXP
    sp = _spend()
    probe_re = getattr(sp, "PROBE_ID_RE", None)
    if probe_re is None or not probe_re.fullmatch(exp) or exp in getattr(sp, "CAPS", {}):
        return None
    path = sp.probe_caps_path(exp)
    with path.open("rb") as f:
        pod = tomllib.load(f).get("pod", {})
    if "gpus" not in pod:
        return None
    return parse_gpu_list(pod["gpus"], f"{path}: [pod].gpus")


# --- .env pod keys (port of pod_up.set_env_var) -------------------------------------------------


def set_env_var(key: str, value: str, env_path: Path | None = None) -> None:
    """Rewrite KEY=... in .env, appending the key if absent.

    2026-10-11 (probe runner review r1 finding 5): concurrent probes share one .env, so the
    read-modify-write holds an exclusive flock on a stable `<env>.lock` next to it, and the new
    text goes to a temp file in the same directory (fsynced, mode 600) that replaces .env
    atomically: no lost update, and no reader ever sees a truncated file."""
    env_path = env_path or ROOT / ".env"
    lock = env_path.with_name(env_path.name + ".lock")
    with open(lock, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            text = env_path.read_text() if env_path.exists() else ""
            new, n = re.subn(rf"(?m)^{re.escape(key)}=.*$", f"{key}={value}", text)
            if n == 0:
                new = text.rstrip("\n") + f"\n{key}={value}\n"
            elif n != 1:
                raise RuntimeError(f"expected at most one {key}= line in .env, found {n}")
            fd, tmp = tempfile.mkstemp(prefix=env_path.name + ".", suffix=".tmp",
                                       dir=env_path.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(new)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, env_path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


# --- pod lifecycle events and spend projection (port of pod_budget_check.stage_spend) ---------


def forget_host_key(host: str, port: int) -> None:
    """Drop any known_hosts entry for [host]:port before the first ssh to a new pod. RunPod
    reuses ip:port pairs across pods, and StrictHostKeyChecking=accept-new refuses a CHANGED key
    ("Host key verification failed", exit 255), which looks like a dead pod."""
    subprocess.run(["ssh-keygen", "-R", f"[{host}]:{port}"], capture_output=True, check=False)


def now_utc() -> datetime:
    return datetime.now(UTC)


def log_pod_event(event: dict, path: Path | None = None) -> dict:
    """Append one lifecycle event (flock-locked; bookkeeping, not the dollar ledger)."""
    path = path or POD_EVENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"utc": now_utc().isoformat(), "exp": EXP, **event}
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(rec) + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return rec


def read_pod_events(path: Path | None = None) -> list[dict]:
    path = path or POD_EVENTS
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def pod_sessions(events: list[dict], now: datetime | None = None) -> list[dict]:
    """One row per pod: billed from CREATION (the `created` event) to termination or `now`.

    The rate is the API's costPerHr when an event carries it, else the creation estimate; it is
    never downgraded to a lower estimate.
    """
    now = now or now_utc()
    pods: dict[str, dict] = {}
    for e in events:
        pid = e.get("pod_id")
        if not pid:
            continue
        if e["event"] == "created":
            pods[pid] = {
                "pod_id": pid,
                "slot": e.get("slot", ""),
                "gpu": e.get("gpu", ""),
                "t0": e["utc"],
                "rate": float(e.get("rate_usd_h") or FALLBACK_RATE),
                "rate_source": e.get("rate_source", "estimate"),
                "running": True,
                "t1": None,
            }
            for k in ("stage", "reservation_id"):  # E9 (additive): only when the event has it
                if e.get(k):
                    pods[pid][k] = e[k]
        elif e["event"] == "rate" and pid in pods and e.get("rate_usd_h"):
            pods[pid]["rate"], pods[pid]["rate_source"] = float(e["rate_usd_h"]), "api"
        elif e["event"] == "terminated" and pid in pods and pods[pid]["running"]:
            # the FIRST termination ends billing: a second `terminated` event (two concurrent
            # cleanups) must not move t1, so every process computes the same charge
            pods[pid]["running"], pods[pid]["t1"] = False, e["utc"]
    out = []
    for p in pods.values():
        t1 = datetime.fromisoformat(p["t1"]) if p["t1"] else now
        h = max(0.0, (t1 - datetime.fromisoformat(p["t0"])).total_seconds() / 3600)
        out.append({**p, "hours": round(h, 4), "usd": round(h * p["rate"], 4)})
    return out


def running_projection(sessions: list[dict], reserve_hours: float = 0.0) -> float:
    """Running pods' cost so far plus `reserve_hours` more of their burn."""
    return sum(s["usd"] + reserve_hours * s["rate"] for s in sessions if s["running"])


def gpu_kill_limit(judge_spent: float | None = None) -> float:
    """The watchdog's GPU limit: min(GPU limit, hard cap - reserve - judge/API dollars).

    With no argument it delegates to `emx.spend.watchdog_kill_usd(EXP)`, the one formula, which
    counts recorded API dollars plus open API reservations (E3 prereg §8.6). An explicit
    `judge_spent` (legacy callers: `watchdog_decision`, pod_budget_check.py) is the caps
    arithmetic for that figure alone, without reading the ledger. E1: min($53, $65 - judges)
    (E1 implementation-plan §5); I1a: min($40, $83 - judges); E1b: min($56, $75 - judges)."""
    if judge_spent is None:
        return float(_spend().watchdog_kill_usd(EXP))
    c = caps()
    return min(c.gpu_limit_usd, c.hard_cap_usd - c.reserve_usd - judge_spent)


def current_kill_limit() -> float:
    """The kill line now: `gpu_kill_limit` at the recorded judge spend, lowered to
    `emx.spend.watchdog_kill_usd(EXP)` (which also counts open API reservations) when the spend
    module provides it (test fakes may not)."""
    limit = gpu_kill_limit(ledger_spent("judge"))
    kill_usd = getattr(_spend(), "watchdog_kill_usd", None)
    if kill_usd is not None:
        limit = min(limit, float(kill_usd(EXP)))
    return limit


def watchdog_decision(
    gpu_spent: float, judge_spent: float, running_usd: float
) -> tuple[bool, float, float]:
    """(kill, projected GPU $, limit). Kill when logged + running GPU $ reaches the limit.

    The limit is lowered to `emx.spend.watchdog_kill_usd(EXP)` when the spend module provides it,
    so the laptop watchdog (scripts/pod_budget_check.py) also counts open API reservations, as the
    pod killswitch does through `current_kill_limit` (E3 plan v2 fix 4)."""
    limit = gpu_kill_limit(judge_spent)
    kill_usd = getattr(_spend(), "watchdog_kill_usd", None)
    if kill_usd is not None:
        limit = min(limit, float(kill_usd(EXP)))
    projected = gpu_spent + running_usd
    return projected >= limit, round(projected, 4), round(limit, 4)


# --- emx.spend (U3) adapters: every ledger touch goes through these three functions ------------


def _spend():
    from emx import spend  # lazy: owned by U3

    return spend


def ledger_spent(kind: str) -> float:
    return float(_spend().spent(kind=kind, exp=EXP))


def ledger_check(projected_usd: float, running_pod_usd: float, stage: str | None = None) -> None:
    """Combined guard (raises or exits non-zero over a cap). E1 relies on emx.spend's default
    exp ("E1"), exactly as before the 2026-09-29 generalisation; other experiments pass it.
    `stage` (E9: required by its stage limits) is passed only when given."""
    kw = {} if EXP == DEFAULT_EXP else {"exp": EXP}
    if stage is not None:
        kw["stage"] = stage
    _spend().check_total(projected_usd, "gpu", running_pod_usd=running_pod_usd, **kw)


E9_FALLBACK_STAGE = "setup"  # an E9 charge whose stage nothing records (an untracked pod)


def _has_stage_limits() -> bool:
    try:
        return bool(getattr(caps(), "stage_limits", None))
    except KeyError:
        return False


def stage_from_note(note: str) -> str | None:
    """The stage of an E9 ledger or reservation note `E9:<stage>:<step>:<batch>`, else None."""
    parts = str(note or "").split(":")
    if len(parts) >= 2 and parts[0] == EXP:
        try:
            stages = caps().stage_group or {}
        except KeyError:
            return None
        if parts[1] in stages:
            return parts[1]
    return None


def session_stage(session: dict) -> tuple[str, str]:
    """(stage, source) of a pod session for an experiment with stage limits: its `created`
    event's stage, else its reservation note's, else E9_FALLBACK_STAGE ("fallback")."""
    if session.get("stage"):
        return str(session["stage"]), "event"
    rid = session.get("reservation_id")
    if rid:
        try:
            from emx import runpod_reservations as rr

            R = rr.get(rid)
        except Exception:  # noqa: BLE001 - a charge must never fail on bookkeeping
            R = None
        st = stage_from_note((R or {}).get("note", ""))
        if st:
            return st, "reservation"
    return E9_FALLBACK_STAGE, "fallback"


def gpu_charge_key(pod_id: str) -> str:
    """The ledger `charge_key` of a pod's one GPU charge (`emx.spend.record_once`)."""
    return f"gpu:{EXP}:{pod_id}"


def _is_gpu_charge(e: dict, pod_id: str) -> bool:
    """A ledger row of this experiment that charges `pod_id`'s GPU time: its `charge_key`, a note
    carrying `pod=<id>`, or a resource ending in `:<id>` (rows written before 2026-10-06)."""
    if e.get("charge_key") == gpu_charge_key(pod_id):
        return True
    if e.get("exp") != EXP or e.get("kind") != "gpu":
        return False
    pat = re.compile(rf"(?:^|[;\s])pod={re.escape(pod_id)}(?:$|[;\s])")
    return (
        bool(pat.search(str(e.get("note", ""))))
        or str(e.get("resource", "")).split(":")[-1] == pod_id
    )


def ledger_record_gpu(session: dict, note: str) -> bool:
    """One `kind=gpu` record per finished pod session: creation-to-termination x rate. The note
    carries `pod=<id>` so `gpu_charged` can find it (idempotent charging). With a real
    `emx.spend` the existence check and the append are one critical section under the ledger's
    lock (`record_once`), so two processes settling one pod charge it once (E3 code review A r1
    finding 10). Returns whether a row was appended."""
    pid = session["pod_id"]
    kw = {
        "exp": EXP,
        "kind": "gpu",
        "resource": f"runpod:{session['gpu']}:{pid}",
        "units": session["hours"],
        "usd": round(session["usd"], 4),
        "note": f"{note}; pod={pid}; rate {session['rate']}/h ({session['rate_source']})",
    }
    if _has_stage_limits():  # E9: every ledger row carries a stage (emx.spend refuses none)
        kw["stage"], src = session_stage(session)
        if src != "event":
            kw["note"] += f"; stage {kw['stage']} from {src}"
    once = getattr(_spend(), "record_once", None)
    if once is None:  # a test fake without the locked helper
        _spend().record(**kw)
        return True
    _, appended = once(gpu_charge_key(pid), also_matches=lambda e: _is_gpu_charge(e, pid), **kw)
    return appended


def gpu_charged(pod_id: str, events: list[dict] | None = None) -> bool:
    """Has this pod's GPU charge been recorded? True if the pod event log has a `charged` event
    for it, or this experiment's ledger has a `kind=gpu` row whose note carries `pod=<id>` or
    whose resource ends in `:<id>` (rows written before 2026-10-06)."""
    events = read_pod_events() if events is None else events
    if any(e.get("event") == "charged" and e.get("pod_id") == pod_id for e in events):
        return True
    read = getattr(_spend(), "read_entries", None)
    if read is None:  # a test fake without a readable ledger: the event log decides
        return False
    return any(_is_gpu_charge(e, pod_id) for e in read())


# --- .env reading (python-dotenv; values never printed) ----------------------------------------


def env(key: str, default: str = "", env_path: Path | None = None) -> str:
    from dotenv import dotenv_values

    return dotenv_values(env_path or ROOT / ".env").get(key) or default


def finish_session(pod_id: str, note: str, events_path: Path | None = None) -> dict | None:
    """Log the `terminated` event and the pod's GPU dollars. Returns the closed session.

    Idempotent and recoverable: a session already closed in the log whose charge is in the
    ledger (`gpu_charged`) is returned as is; one closed but never charged (a crash between the
    two appends) gets its charge now. The charge is followed by a `charged` event.
    """
    events = read_pod_events(events_path)
    closed = next(
        (s for s in pod_sessions(events) if s["pod_id"] == pod_id and not s["running"]), None
    )
    if closed is None:
        log_pod_event({"event": "terminated", "pod_id": pod_id, "reason": note}, events_path)
        events = read_pod_events(events_path)
        closed = next((s for s in pod_sessions(events) if s["pod_id"] == pod_id), None)
        if closed is None:
            return None
    elif gpu_charged(pod_id, events):
        return closed
    if not gpu_charged(pod_id, events) and ledger_record_gpu(closed, note):
        log_pod_event({"event": "charged", "pod_id": pod_id, "usd": closed["usd"]}, events_path)
    return closed


# --- reconciliation against the API (review U1 r1: pod creation is not idempotent) ------------

PENDING_GRACE_S = 300  # a create_pending event this young shields its pod name from the reaper


def tracked_pod_ids(events: list[dict]) -> set[str]:
    """Pod ids with a `created` event in this experiment's log."""
    return {e["pod_id"] for e in events if e.get("event") == "created" and e.get("pod_id")}


def pending_path() -> Path:
    """pod_up's `create_pending` notes, next to the event log (pods_<EXP>.pending.jsonl)."""
    return POD_EVENTS.with_name(POD_EVENTS.stem + ".pending.jsonl")


def log_create_pending(name: str, slot: str) -> dict:
    return log_pod_event({"event": "create_pending", "name": name, "slot": slot}, pending_path())


def untracked_pods(
    api_pods: list[dict] | None,
    events: list[dict],
    pending: list[dict] = (),
    *,
    now: datetime | None = None,
    grace_s: float = PENDING_GRACE_S,
) -> list[dict]:
    """API pods named with this experiment's prefix that the event log does not know.

    A pod whose name matches a `create_pending` note younger than `grace_s` is spared (pod_up is
    between sending the create and logging its id); `grace_s=0` spares nothing."""
    now = now or now_utc()
    known = tracked_pod_ids(events)
    young = {
        e.get("name")
        for e in pending
        if (now - datetime.fromisoformat(e["utc"])).total_seconds() < grace_s
    }
    return [
        p
        for p in (api_pods or [])
        if str(p.get("name", "")).startswith(POD_NAME_PREFIX)
        and p.get("id") not in known
        and p.get("name") not in young
    ]


def _pending_creation(name: str) -> dict | None:
    """emx.runpod_reservations' live `creating` row for this pod name (None if none or the
    reservations file is unreadable)."""
    try:
        from emx import runpod_reservations as rr

        return rr.pending_creation(name, EXP)
    except Exception:  # noqa: BLE001 - the reaper must never fail on bookkeeping
        return None


def e9_pending_terminations(events: list[dict] | None = None) -> list[dict]:
    """E9 pods with a `pending_termination` event whose session is not closed: a DELETE was due
    and is not confirmed (review R3 r2 #3). Each: the latest such event."""
    events = read_pod_events() if events is None else events
    closed = {s["pod_id"] for s in pod_sessions(events) if not s["running"]}
    latest: dict[str, dict] = {}
    for e in events:
        if e.get("event") == "pending_termination" and e.get("pod_id"):
            latest[e["pod_id"]] = e
    return [e for pid, e in latest.items() if pid not in closed]


def _deleted_or_gone(client: RunPod, pod_id: str) -> tuple[bool, str]:
    """DELETE the pod; (True, why) when it was accepted or the API answers 404 (gone), (False,
    why) on any other failure (a transport error is never "gone")."""
    try:
        client.terminate_pod(pod_id)
        return True, "deleted"
    except RunPodError as e:
        if "HTTP 404" in str(e):
            return True, "gone (404)"
        return False, str(e)


def e9_reap(client: RunPod, reason: str, *, grace_s: float = PENDING_GRACE_S
            ) -> tuple[list[str], list[str]]:
    """E9's reaper (review R3 r2 #3). Every untracked e9-* pod gets its `created` event (billing,
    as `reap_untracked`) AND a durable `pending_termination` event BEFORE its DELETE; every pod
    with an unconfirmed pending termination is retried on each call, whether or not the API still
    lists it and whatever its `created` event says. A confirmed DELETE (or a 404) closes it through
    `e9_close` (charge and settlement under the admission lock). Returns (terminated ids, ids
    whose DELETE failed); the caller reports failures non-zero."""
    api = client.list_pods()
    pods = untracked_pods(api, read_pod_events(), read_pod_events(pending_path()),
                          grace_s=grace_s)
    for p in pods:
        api_rate = p.get("costPerHr")
        ev = {
            "event": "created",
            "pod_id": p["id"],
            "slot": "",
            "gpu": str(p.get("gpuTypeId") or "?"),
            "rate_usd_h": float(api_rate or FALLBACK_RATE),
            "rate_source": "api" if api_rate else "estimate",
            "untracked": True,
            "name": p.get("name"),
        }
        c = _pending_creation(str(p.get("name") or ""))
        if c is not None:  # pod_up died after sending the create: bill from its t0
            ev["utc"] = datetime.fromtimestamp(float(c["t0_epoch"]), UTC).isoformat()
            ev["reservation_id"] = c["reservation_id"]
        log_pod_event(ev)
        log_pod_event({"event": "pending_termination", "pod_id": p["id"], "name": p.get("name"),
                       "reason": f"{reason}: untracked pod"})
    done, failed = [], []
    for e in e9_pending_terminations():
        pid = e["pod_id"]
        ok, why = _deleted_or_gone(client, pid)
        if not ok:
            print(f"terminate untracked {pid} FAILED ({why}); retried next reconciliation",
                  file=sys.stderr)
            failed.append(pid)
            continue
        r = e9_close(pid, f"{e.get('reason', reason)} ({why}; billed from first seen)")
        if not r["settled"]:
            print(f"untracked {pid} terminated; NOT settled: {r['why']}", file=sys.stderr)
        done.append(pid)
    return done, failed


def reap_untracked(client: RunPod, reason: str, *, grace_s: float = PENDING_GRACE_S) -> list:
    """Terminate every untracked pod of this experiment. Each first gets a `created` event (rate
    from the API, else the conservative estimate; billed from when we saw it, so it undercounts
    by at most the time it went unseen, flagged in the note), so its dollars reach the ledger.
    Returns the ids terminated. E9 goes through `e9_reap` (pending terminations are retried)."""
    if EXP == "E9":
        return e9_reap(client, reason, grace_s=grace_s)[0]
    pods = untracked_pods(
        client.list_pods(), read_pod_events(), read_pod_events(pending_path()), grace_s=grace_s
    )
    done = []
    for p in pods:
        api_rate = p.get("costPerHr")
        ev = {
            "event": "created",
            "pod_id": p["id"],
            "slot": "",
            "gpu": str(p.get("gpuTypeId") or "?"),
            "rate_usd_h": float(api_rate or FALLBACK_RATE),
            "rate_source": "api" if api_rate else "estimate",
            "untracked": True,
            "name": p.get("name"),
        }
        c = _pending_creation(str(p.get("name") or ""))
        if c is not None:  # pod_up died after sending the create: bill from its t0
            ev["utc"] = datetime.fromtimestamp(float(c["t0_epoch"]), UTC).isoformat()
            ev["reservation_id"] = c["reservation_id"]
        log_pod_event(ev)
        try:
            client.terminate_pod(p["id"])
        except RunPodError as e:
            print(f"terminate untracked {p['id']} FAILED: {e}", file=sys.stderr)
            continue
        finish_session(p["id"], f"{reason}: untracked pod {p.get('name')} (billed from first seen)")
        done.append(p["id"])
    return done


def slot_pod_ok(slot: str, pod_id: str, api_pod: dict | None = None) -> tuple[bool, str]:
    """Does the pod in `slot` belong to this experiment? This experiment's event log must have
    created it in that slot and, if the API reported a name, it must carry this prefix."""
    name = (api_pod or {}).get("name")
    if name and not str(name).startswith(POD_NAME_PREFIX):
        return False, f"pod {pod_id} is named {name!r}, not {POD_NAME_PREFIX}*"
    for s in pod_sessions(read_pod_events()):
        if s["pod_id"] == pod_id:
            if s["slot"] != slot:
                return False, f"pod {pod_id} was created in slot {s['slot']}, not {slot}"
            return True, "ok"
    return False, f"pod {pod_id} is not in {POD_EVENTS.name} (another experiment's slot?)"


# --- checkpoint approvals, the P0 ceiling and the watchdog heartbeat (I1a) --------------------

UTC_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.MULTILINE)


def approval_path(step: str, exp: str | None = None) -> Path:
    """logs/approvals/<EXP>-<step>.approved, written only by the main session after Jacob's
    go-ahead: non-empty, with a line starting with a UTC timestamp (2026-09-29T12:00:00Z ...)."""
    return LOGS / "approvals" / f"{exp or EXP}-{step}.approved"


def approved(step: str, exp: str | None = None) -> bool:
    try:
        return bool(UTC_LINE_RE.search(approval_path(step, exp).read_text()))
    except OSError:
        return False


def gpu_ceiling() -> float | None:
    """I1a only: the P0 smoke cap ($6) bounds all GPU spend until P1 is approved."""
    if EXP != "I1a" or approved("P1"):
        return None
    from emx.i1a.contracts import POD_SMOKE_CAP_USD

    return float(POD_SMOKE_CAP_USD)


def killswitch_deadline(
    pod_id: str, events: list[dict] | None = None, now: datetime | None = None
) -> int:
    """Epoch second at which this experiment's running pods, all burning together, reach the
    watchdog's GPU kill line: min(GPU limit, hard cap - reserve - judges), or the I1a P0 ceiling
    when lower, minus logged GPU $ and every running pod's cost so far.

    The pod-side backstop (scripts/pod_killswitch.sh) stops `pod_id` at this time. Counting the
    joint burn makes the deadline early, never late, for the pods running when it is computed; a
    pod created LATER is not in an earlier pod's deadline, so pod_push.sh re-arms every running
    pod of the experiment when it arms a new one. Nothing left means a deadline of `now`."""
    now = now or now_utc()
    sessions = pod_sessions(read_pod_events() if events is None else events, now)
    running = [s for s in sessions if s["running"]]
    if not any(s["pod_id"] == pod_id for s in running):
        raise ValueError(f"pod {pod_id} is not a running pod in {POD_EVENTS.name}")
    limit = current_kill_limit()
    ceiling = gpu_ceiling()
    if ceiling is not None:
        limit = min(limit, ceiling)
    remaining = max(0.0, limit - ledger_spent("gpu") - sum(s["usd"] for s in running))
    burn = sum(s["rate"] for s in running)
    return int(now.timestamp() + remaining / burn * 3600)


def per_slot_deadlines(
    events: list[dict] | None = None, now: datetime | None = None
) -> dict[str, int]:
    """{slot: epoch} for every running pod of EXP with a valid slot: the pod's
    `emx.runpod_reservations.rearm_target` (min of its reservation deadline, its acknowledged
    deadline and the global `killswitch_deadline`), or the global deadline when no reservation
    launched it. Never later than the deadline armed now (E3 plan v4 fix 3)."""
    from emx import runpod_reservations as rr

    events = read_pod_events() if events is None else events
    out: dict[str, int] = {}
    for s in pod_sessions(events, now):
        if not s["running"] or not SLOT_RE.fullmatch(s["slot"] or ""):
            continue
        if EXP == "E9":  # its own budget deadline only, never the global line (prereg §7.3)
            if rr.pod(s["pod_id"]) is None:
                continue  # no reservation: no budget deadline to arm (E9 always reserves)
            d = rr.rearm_target(s["pod_id"], float("inf"))
            out[s["slot"]] = min(d, out.get(s["slot"], d))
            continue
        g = killswitch_deadline(s["pod_id"], events, now)
        d = rr.rearm_target(s["pod_id"], g)
        out[s["slot"]] = min(d, out.get(s["slot"], d))
    return out


def heartbeat_path(exp: str | None = None) -> Path:
    return LOGS / f"watchdog_{exp or EXP}.heartbeat"


def watchdog_live(exp: str | None = None, now: float | None = None) -> tuple[bool, str]:
    """The heartbeat file holds `<epoch seconds> <interval seconds> <watchdog pid>`; the watchdog
    is live if the beat is younger than two intervals (at least 60 s) AND that pid is a running
    process (so a beat left behind by a test or a killed watchdog never counts)."""
    p = heartbeat_path(exp)
    try:
        t_s, interval_s, pid_s = p.read_text().split()[:3]
        t, interval, pid = float(t_s), float(interval_s), int(pid_s)
    except (OSError, ValueError):
        return False, f"no readable heartbeat at {p}"
    age = (now if now is not None else time.time()) - t
    limit = max(2 * interval, 60.0)
    if age > limit:
        return False, f"heartbeat {age:.0f} s old > {limit:.0f} s"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, f"heartbeat from pid {pid}, which is not running"
    except PermissionError:
        pass  # exists, owned by someone else
    return True, f"heartbeat {age:.0f} s old (pid {pid})"


# --- E9: the backstop kill line over all E9 pods (laptop watchdog only; prereg §7.3) ----------

E9_BACKSTOP_FUNCS = ("backstop_kill_usd", "backstop_usd")  # emx.e9.budget's, when it has one
E9_RESERVE_FUNCS = ("api_reserve", "current_api_reserve_usd", "api_reserve_usd")


def _e9_budget():
    try:
        from emx.e9 import budget  # unit F1a; imported lazily (built concurrently)
    except ImportError:
        return None
    return budget


@contextmanager
def e9_admission_lock():
    """The E9 admission lock (implementation plan v4, "E9 admission protocol"): E9's settlement
    path holds it while it writes a pod's GPU charge and then its settle row, so an admission
    reads both or neither. `emx.e9.budget.admission_lock()` when that module provides it, else
    the same exclusive `fcntl.flock` on emx.e9.contracts.ADMISSION_LOCK under this checkout."""
    b = _e9_budget()
    lock = getattr(b, "admission_lock", None)
    if callable(lock):
        with lock():
            yield
        return
    from emx.e9 import contracts as c9

    path = ROOT / c9.ADMISSION_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _close_session(pod_id: str, note: str) -> dict | None:
    """Log the pod's `terminated` event (once) and return its closed session, WITHOUT charging
    (E9: `emx.e9.budget.settle_pod` writes the charge). None if the log never created it."""
    closed = next(
        (s for s in pod_sessions(read_pod_events()) if s["pod_id"] == pod_id and not s["running"]),
        None,
    )
    if closed is None:
        log_pod_event({"event": "terminated", "pod_id": pod_id, "reason": note})
        closed = next((s for s in pod_sessions(read_pod_events()) if s["pod_id"] == pod_id), None)
    return closed


def e9_close(pod_id: str, note: str, reservation_id: str | None = None) -> dict:
    """E9's ONLY closure path for a terminated pod (admission protocol; review R3 #5). Under the
    E9 admission lock: log the `terminated` event (once), then `emx.e9.budget.settle_pod` writes
    the GPU charge with the reservation's stage and the settle row, then the reservation's
    remainder is released (bookkeeping). The reservation is `reservation_id`, else the one that
    launched the pod, else the one its `created` event names (a pod adopted from a dead launcher's
    `creating` row). Every step is idempotent, so a retry after any failure (also after the pod
    left the API) finishes it. The one exception is an orphan no reservation ever covered (found
    by the reaper with no `creating` row): its charge is recorded with `ledger_record_gpu` (stage
    fallback flagged in the note) and there is nothing to settle (`settled` stays False, `why`
    says so). Never raises for a settlement failure: it is returned in `why`.
    Returns {session, reservation_id, charged, settled, why}."""
    from emx import runpod_reservations as rr

    out = {"session": None, "reservation_id": reservation_id, "charged": False,
           "settled": False, "why": None}
    try:
        with e9_admission_lock():
            s = _close_session(pod_id, note)
            out["session"] = s
            if s is None:
                out["why"] = f"no `created` event for {pod_id}: its cost is NOT in the ledger"
                return out
            rid = reservation_id
            if rid is None:
                p = rr.pod(pod_id)
                rid = p["reservation_id"] if p is not None else s.get("reservation_id")
            out["reservation_id"] = rid
            if rid is None:
                if not gpu_charged(pod_id) and ledger_record_gpu(s, f"{note}; orphan, no reservation"):
                    log_pod_event({"event": "charged", "pod_id": pod_id, "usd": s["usd"]})
                out["charged"] = gpu_charged(pod_id)
                out["why"] = "no RunPod reservation covers this pod: charged, nothing to settle"
                return out
            r = _e9_budget().settle_pod(
                rid, pod_id, round(s["usd"], 4), hours=s["hours"],
                resource=f"runpod:{s['gpu']}:{pod_id}",
                note=f"{note}; rate {s['rate']}/h ({s['rate_source']})",
            )
            out["charged"] = True
            if r.get("charged_now"):
                log_pod_event({"event": "charged", "pod_id": pod_id, "usd": s["usd"]})
            rr.release(rid, note=f"pod_down: unused after {pod_id}")
            out["settled"] = True
    except Exception as e:  # noqa: BLE001 - reported, the caller exits non-zero and retries
        out["why"] = f"{type(e).__name__}: {e}"
    return out


def e9_charge_and_settle(pod_id: str, note: str, reservation_id: str | None = None):
    """Backward-compatible wrapper of `e9_close`: (session, reservation id, settled)."""
    r = e9_close(pod_id, note, reservation_id)
    return r["session"], r["reservation_id"], r["settled"]


def e9_api_reserve_usd() -> tuple[float, str]:
    """(current API reserve $, source): emx.e9.budget's current value when it provides one (the
    orchestrator recomputes it at each gate), else the sign-off value in the contracts."""
    b = _e9_budget()
    for name in E9_RESERVE_FUNCS:
        f = getattr(b, name, None)
        if callable(f):
            return float(f()), f"emx.e9.budget.{name}"
    from emx.e9 import contracts as c9

    return float(c9.API_RESERVE_AT_SIGNOFF_USD), "contracts.API_RESERVE_AT_SIGNOFF_USD"


def e9_backstop_arith(
    gpu_limit: float, hard_cap: float, api_reserve: float, api_recorded: float,
    api_reserved: float,
) -> float:
    """min(GPU limit, hard cap - API reserve - (API recorded + API reserved)) (prereg §7.3)."""
    return round(min(gpu_limit, hard_cap - api_reserve - api_recorded - api_reserved), 6)


def e9_backstop_usd(api_reserve_usd: float | None = None, ledger=None) -> dict:
    """The E9 backstop kill line on GPU dollars (logged + running, all E9 pods together), with
    its inputs. When emx.e9.budget has its own backstop function, the lower of the two counts."""
    from emx.e9 import contracts as c9

    t = _spend().totals("E9", ledger=ledger)
    reserve, src = (
        (float(api_reserve_usd), "argument") if api_reserve_usd is not None
        else e9_api_reserve_usd()
    )
    line = e9_backstop_arith(
        c9.GPU_LIMIT_USD, c9.HARD_CAP_USD, reserve, float(t["judge_usd"]),
        float(t["judge_reserved_usd"]),
    )
    out = {
        "gpu_limit_usd": c9.GPU_LIMIT_USD,
        "hard_cap_usd": c9.HARD_CAP_USD,
        "api_reserve_usd": reserve,
        "api_reserve_source": src,
        "api_recorded_usd": round(float(t["judge_usd"]), 6),
        "api_reserved_usd": round(float(t["judge_reserved_usd"]), 6),
        "computed_usd": line,
    }
    b = _e9_budget()
    for name in E9_BACKSTOP_FUNCS:
        f = getattr(b, name, None)
        if callable(f):
            other = float(f())
            out["budget_" + name] = other
            line = min(line, other)
            break
    out["backstop_usd"] = round(line, 6)
    return out


def e9_watchdog_status(reserve_hours: float, events: list[dict] | None = None) -> dict:
    """E9's laptop budget check: logged GPU $ + every running E9 pod's cost so far + its burn
    for `reserve_hours` (the watchdog's shutdown latency) against the backstop. Never an armed
    deadline (the pods carry their own budget deadlines)."""
    sessions = pod_sessions(read_pod_events() if events is None else events)
    running = running_projection(sessions, reserve_hours)
    gpu_spent = ledger_spent("gpu")
    b = e9_backstop_usd()
    projected = gpu_spent + running
    return {
        "exp": EXP,
        "gpu_spent_usd": round(gpu_spent, 4),
        "running_usd_incl_reserve": round(running, 4),
        "reserve_hours": reserve_hours,
        "projected_gpu_usd": round(projected, 4),
        "backstop": b,
        "gpu_kill_limit_usd": b["backstop_usd"],
        "hard_stop": bool(projected >= b["backstop_usd"]),
        "burn_per_hr": round(sum(s["rate"] for s in sessions if s["running"]), 4),
        "running": [
            {k: s.get(k) for k in ("pod_id", "slot", "gpu", "hours", "rate", "usd", "stage")}
            for s in sessions
            if s["running"]
        ],
    }


# --- CLI: offline slot ownership check (pod_push.sh calls it before touching a pod) ----------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emx.runpod")
    ap.add_argument("--exp", default=DEFAULT_EXP)
    ap.add_argument("--check-slot", metavar="SLOT")
    ap.add_argument(
        "--e9-budget-check",
        action="store_true",
        help="E9 only: print one JSON line (e9_watchdog_status) and exit 2 at the backstop",
    )
    ap.add_argument("--reserve-hours", type=float, default=0.0, help="with --e9-budget-check")
    ap.add_argument(
        "--session-env",
        action="store_true",
        help="on success print EMX_POD_RATE_USD_H= and EMX_POD_CREATED_EPOCH= lines to stdout",
    )
    ap.add_argument(
        "--killswitch-deadline",
        action="store_true",
        help="on success print EMX_KILLSWITCH_DEADLINE=<epoch> (see killswitch_deadline) and "
        "EMX_KILLSWITCH_SLOTS=<the running pods' slots, space-separated>",
    )
    ap.add_argument(
        "--per-slot",
        action="store_true",
        help="with --killswitch-deadline, also print EMX_KILLSWITCH_DEADLINE_<slot>=<epoch> for "
        "every running pod of --exp (see per_slot_deadlines)",
    )
    args = ap.parse_args(argv)
    if args.per_slot and not args.killswitch_deadline:
        ap.error("--per-slot needs --killswitch-deadline")
    if args.e9_budget_check:
        if args.exp != "E9":
            ap.error("--e9-budget-check is E9-only")
        configure(args.exp)
        try:
            r = e9_watchdog_status(args.reserve_hours)
        except Exception as e:  # noqa: BLE001 - the watchdog fails closed on any other exit
            print(f"E9 budget check failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 3
        print(json.dumps(r))  # stdout: exactly one JSON line
        return 2 if r["hard_stop"] else 0
    if not args.check_slot:
        ap.error("the following arguments are required: --check-slot")
    configure(args.exp)
    slot = check_slot(args.check_slot)
    pod_id = env(f"RUNPOD_POD_ID{slot}")
    if not pod_id:
        print(f"no pod in slot {slot}", file=sys.stderr)
        return 1
    ok, why = slot_pod_ok(slot, pod_id)
    verdict = "belongs to" if ok else "REFUSED for"
    print(f"slot {slot}: pod {pod_id} {verdict} {EXP}: {why}", file=sys.stderr)
    if ok and args.session_env:
        s = next(x for x in pod_sessions(read_pod_events()) if x["pod_id"] == pod_id)
        t0 = datetime.fromisoformat(s["t0"]).timestamp()
        print(f"EMX_POD_RATE_USD_H={s['rate']}\nEMX_POD_CREATED_EPOCH={int(t0)}")
    if ok and args.killswitch_deadline:
        print(f"EMX_KILLSWITCH_DEADLINE={killswitch_deadline(pod_id)}")
        slots = sorted({s["slot"] for s in pod_sessions(read_pod_events()) if s["running"]})
        print(f"EMX_KILLSWITCH_SLOTS={' '.join(x for x in slots if SLOT_RE.fullmatch(x))}")
        if args.per_slot:
            for s_, d in per_slot_deadlines().items():
                print(f"EMX_KILLSWITCH_DEADLINE{s_}={d}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
