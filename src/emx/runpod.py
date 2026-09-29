"""Shared RunPod REST client and pod bookkeeping for E1.

Ported from ../mats12_neel_working/scripts/pod_up.py, pod_down.py and pod_budget_check.py
(their inline httpx calls, `set_env_var` and the creation-to-termination spend projection),
collected here so every script shares one client. Changes from the source:

- Every request goes through `RunPod._request`, which always sends a browser-like User-Agent
  (RunPod's Cloudflare front blocks default library agents with error 1010; CLAUDE.md gotcha).
- Retries on connection errors, 429 and 5xx. `dry_run=True` prints the exact request (the
  Authorization header redacted) and sends nothing.
- GPU fallback order and rates follow implementation-plan §5 and CLAUDE.md.
- Pod lifecycle events go to `logs/pods_E1.jsonl` (bookkeeping only: creation time, rate, slot).
  Dollars go to `logs/costs.jsonl` only through `emx.spend` (owned by U3), imported lazily.
"""

from __future__ import annotations

import fcntl
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

API = "https://rest.runpod.io/v1"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
EXP = "E1"
POD_NAME_PREFIX = "e1-"  # every E1 pod is named e1-<slot>; pod_down --all-e1 matches on it

# CUDA 13.0 devel image on Ubuntu 24.04 (system Python 3.12). uv.lock resolves torch 2.12.1 /
# 2.13.0 against CUDA 13 wheels, so the host driver must support CUDA 13 (see ALLOWED_CUDA).
# Digest pinned 2026-09-29 from Docker Hub (tag last updated 2026-09-25).
IMAGE = (
    "runpod/pytorch:1.3.3-cu1300-torch2121-ubuntu2404"
    "@sha256:5f36564b9f8dd873f569143afc5035182116748bb7fffab0ed305b2072271ae9"
)
ALLOWED_CUDA = ("13.0",)


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
LOGS = ROOT / "logs"
POD_EVENTS = LOGS / f"pods_{EXP}.jsonl"
SLOT_RE = re.compile(r"_[A-Za-z0-9]{1,8}")


class RunPodError(RuntimeError):
    pass


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

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
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
            else:
                if r.status_code < 400:
                    return r.json() if r.content else None
                last = f"HTTP {r.status_code}: {r.text[:500]}"
                if r.status_code != 429 and r.status_code < 500:
                    raise RunPodError(f"{method} {path} failed: {last}")
            if attempt < self.retries:
                time.sleep(self.backoff_s * 2**attempt)
        raise RunPodError(f"{method} {path} failed after {self.retries + 1} tries: {last}")

    # -- endpoints ---------------------------------------------------------------------------

    def list_pods(self) -> list[dict]:
        return self._request("GET", "/pods") or []

    def get_pod(self, pod_id: str) -> dict:
        return self._request("GET", f"/pods/{pod_id}")

    def create_pod(self, body: dict) -> dict | None:
        return self._request("POST", "/pods", body)

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


# --- .env pod keys (port of pod_up.set_env_var) -------------------------------------------------


def set_env_var(key: str, value: str, env_path: Path | None = None) -> None:
    """Rewrite KEY=... in .env in place, appending the key if absent."""
    env_path = env_path or ROOT / ".env"
    text = env_path.read_text() if env_path.exists() else ""
    new, n = re.subn(rf"(?m)^{re.escape(key)}=.*$", f"{key}={value}", text)
    if n == 0:
        new = text.rstrip("\n") + f"\n{key}={value}\n"
    elif n != 1:
        raise RuntimeError(f"expected at most one {key}= line in .env, found {n}")
    env_path.write_text(new)


# --- pod lifecycle events and spend projection (port of pod_budget_check.stage_spend) ---------


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
        elif e["event"] == "rate" and pid in pods and e.get("rate_usd_h"):
            pods[pid]["rate"], pods[pid]["rate_source"] = float(e["rate_usd_h"]), "api"
        elif e["event"] == "terminated" and pid in pods:
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


def gpu_kill_limit(judge_spent: float) -> float:
    """The watchdog's GPU limit: min($53, $65 - judge spend so far) (implementation-plan §5)."""
    from emx.contracts import GPU_LIMIT_USD, HARD_CAP_USD

    return min(GPU_LIMIT_USD, HARD_CAP_USD - judge_spent)


def watchdog_decision(
    gpu_spent: float, judge_spent: float, running_usd: float
) -> tuple[bool, float, float]:
    """(kill, projected GPU $, limit). Kill when logged + running GPU $ reaches the limit."""
    limit = gpu_kill_limit(judge_spent)
    projected = gpu_spent + running_usd
    return projected >= limit, round(projected, 4), round(limit, 4)


# --- emx.spend (U3) adapters: every ledger touch goes through these three functions ------------


def _spend():
    from emx import spend  # lazy: owned by U3

    return spend


def ledger_spent(kind: str) -> float:
    return float(_spend().spent(kind=kind, exp=EXP))


def ledger_check(projected_usd: float, running_pod_usd: float) -> None:
    """Combined guard (raises or exits non-zero over a cap)."""
    _spend().check_total(projected_usd, "gpu", running_pod_usd=running_pod_usd)


def ledger_record_gpu(session: dict, note: str) -> None:
    """One `kind=gpu` record per finished pod session: creation-to-termination x rate."""
    _spend().record(
        exp=EXP,
        kind="gpu",
        resource=f"runpod:{session['gpu']}:{session['pod_id']}",
        units=session["hours"],
        usd=round(session["usd"], 4),
        note=f"{note}; rate {session['rate']}/h ({session['rate_source']})",
    )


# --- .env reading (python-dotenv; values never printed) ----------------------------------------


def env(key: str, default: str = "", env_path: Path | None = None) -> str:
    from dotenv import dotenv_values

    return dotenv_values(env_path or ROOT / ".env").get(key) or default


def finish_session(pod_id: str, note: str, events_path: Path | None = None) -> dict | None:
    """Log the `terminated` event and the pod's GPU dollars. Returns the closed session.

    A pod already closed in the log is not recorded twice (returns its closed session).
    """
    for s in pod_sessions(read_pod_events(events_path)):
        if s["pod_id"] == pod_id and not s["running"]:
            return s
    log_pod_event({"event": "terminated", "pod_id": pod_id, "reason": note}, events_path)
    for s in pod_sessions(read_pod_events(events_path)):
        if s["pod_id"] == pod_id:
            ledger_record_gpu(s, note)
            return s
    return None
