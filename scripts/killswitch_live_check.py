"""Live, end-to-end check of the pod-side killswitch (scripts/pod_killswitch.sh) on one cheap pod.

Why: the killswitch relies on RunPod's pod-scoped RUNPOD_API_KEY being allowed to stop its own pod,
which no local test can show (gpt-6-sol review 20261003-runpod-hardening r1, BLOCKING 1). This
script creates a cheap community GPU pod from the experiments' image (emx.runpod.IMAGE), then:

  1. env       RUNPOD_API_KEY and RUNPOD_POD_ID are in PID 1's env, and the id is this pod's
  2. refuse    `arm <another id>` is refused (exit 1, REFUSED, no ARMED line)
  3. arm       pod_push.sh's own arm_slot function (cut out of the script, real ssh) arms it with a
               deadline ARM_S seconds out; the line must say ARMED and key_check=ok
  4. single    arming again leaves exactly one watcher; status says ARMED watcher=yes
  5. no leak   the key is in no process's argv and not in the killswitch log
  6. no early  the pod is still RUNNING (API, account key) shortly before the deadline
  7. stops     the API reports the pod stopped (desiredStatus EXITED) within GRACE_S after it
  8. route     the pod's log, streamed to the laptop over ssh until the stop, shows which route
               (REST or runpodctl) was accepted

The pod is ALWAYS terminated in a `finally` (DELETE, then confirmed absent from the pod list), its
dollars go to logs/costs.jsonl as exp "infra", and the result is written to
logs/infra/killswitch-live-<utc>.json. Exit 0 only if every check passed.

Cost: rate cap MAX_RATE ($1.50/h). A pod lives at most RUN_DEADLINE_S (25 min) until sshd
answers, plus ~1 min of checks, ARM_S, GRACE_S and cleanup: about 33 min with the defaults, so
under $1 at the cap (the A4000s used so far cost $0.17-0.22/h, about $0.01 a run). The ledger
records elapsed time x rate: an upper bound, since a self-stopped pod no longer bills GPU.
Usage: uv run python scripts/killswitch_live_check.py [--arm-s 240] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from emx import runpod as rp

ROOT = Path(__file__).resolve().parents[1]
NAME = "ks-check"
MAX_RATE = 1.50  # Jacob approved "up to a few dollars" for this check (2026-10-03)
CREATE_ROUNDS = 8
RUN_DEADLINE_S = 25 * 60  # creation to "sshd answers" must fit in this (review r3)
SSH_WAIT_S = 15 * 60
GRACE_S = 180  # the watcher polls every 60 s, then the stop takes effect
CHEAP_GPUS = [
    "NVIDIA RTX A4000",
    "NVIDIA RTX 2000 Ada Generation",
    "NVIDIA RTX A4500",
    "NVIDIA RTX 4000 Ada Generation",
    "NVIDIA RTX A5000",
    "NVIDIA GeForce RTX 3090",
    "NVIDIA L4",
    "NVIDIA RTX A6000",
    "NVIDIA A40",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA L40S",
    "NVIDIA RTX 6000 Ada Generation",
]
SSH_OPTS = [
    "-o",
    "ConnectTimeout=20",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "BatchMode=yes",
    "-o",
    "ServerAliveInterval=30",
]


def say(msg: str) -> None:
    print(f"{datetime.now(UTC):%H:%M:%SZ} {msg}", flush=True)


def balance(key: str) -> dict:
    r = requests.post(
        "https://api.runpod.io/graphql",
        headers=rp.headers(key),
        json={"query": "query { myself { clientBalance currentSpendPerHr } }"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]["myself"]


def ssh(host: str, port: int, cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-n", "-p", str(port), *SSH_OPTS, f"root@{host}", cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def arm_slot_live(pod_id: str, host: str, port: int, deadline: int) -> str:
    """Run pod_push.sh's arm_slot (the exact function text) against the live pod."""
    text = (ROOT / "scripts" / "pod_push.sh").read_text()
    start = text.index("arm_slot() {")
    fn = text[start : text.index("\n}\n", start) + 3]
    script = (
        f'envval() {{ case "$1" in RUNPOD_POD_ID_K) echo {pod_id} ;; '
        f"POD_SSH_HOST_K) echo {host} ;; POD_SSH_PORT_K) echo {port} ;; esac; }}\n"
        f"POD_SSH_USER=root\nset -euo pipefail\n{fn}\narm_slot _K {deadline}\n"
    )
    r = subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True, timeout=150, check=False
    )
    return (
        r.stdout.strip().splitlines() or [f"(no output; rc={r.returncode}; {r.stderr[-300:]})"]
    )[-1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--arm-s", type=int, default=240, help="deadline this many seconds after arming"
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    t_start = time.time()
    key = rp.env("RUNPOD_API_KEY")
    client = rp.RunPod(key or "dry", dry_run=args.dry_run)
    pub = (Path.home() / ".ssh" / "id_ed25519.pub").read_text().strip()
    body = {
        "name": NAME,
        "imageName": rp.IMAGE,
        "cloudType": "COMMUNITY",
        "gpuTypeIds": CHEAP_GPUS,
        "gpuCount": 1,
        "containerDiskInGb": 30,
        "allowedCudaVersions": list(rp.ALLOWED_CUDA),
        "supportPublicIp": True,
        "ports": ["22/tcp"],
        "env": {"PUBLIC_KEY": pub},
    }
    if args.dry_run:
        client.create_pod(body)
        return 0

    checks: dict[str, dict] = {}

    def check(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}
        say(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")

    b = balance(key)
    say(f"RunPod balance ${b['clientBalance']:.2f}, current spend ${b['currentSpendPerHr']}/h")
    if b["clientBalance"] < 5:
        say("REFUSED: RunPod balance under $5")
        return 1
    existing = [p for p in client.list_pods() if p.get("name") == NAME]
    if existing:
        say(f"REFUSED: a {NAME} pod already exists ({existing[0].get('id')}); terminate it first")
        return 1

    pod_id = None
    t_created = None
    created_ids: set[str] = set()

    def list_ours() -> list[dict]:
        return [p for p in client.list_pods() if p.get("name") == NAME]

    rate = MAX_RATE
    result: dict = {"utc_start": datetime.now(UTC).isoformat(), "image": rp.IMAGE}
    try:
        pod = None
        for attempt in range(CREATE_ROUNDS):  # stock moves; CUDA 13 hosts are a subset
            for cloud in ("COMMUNITY", "SECURE"):
                try:
                    pod = client.create_pod({**body, "cloudType": cloud})
                except rp.CreateUncertain as e:  # RunPod sends "no instances" as a 500
                    found = []  # reconcile before any retry: a late pod must not be orphaned
                    for _ in range(3):
                        time.sleep(20)
                        found = [p for p in list_ours() if p.get("id")]
                        if found:
                            break
                    created_ids.update(p["id"] for p in found)
                    pod = found[0] if found else None
                    if not pod:
                        say(f"no pod ({cloud}, round {attempt + 1}): {str(e)[:120]}")
                except rp.RunPodError as e:
                    say(f"create refused ({cloud}, round {attempt + 1}): {str(e)[:120]}")
                if pod:
                    result["cloud"] = cloud
                    break
            if pod:
                break
            time.sleep(60)
        if not pod:
            raise RuntimeError(f"no stock in {CREATE_ROUNDS} rounds")
        pod_id = pod["id"]
        created_ids.add(pod_id)
        t_created = time.time()
        rate = float(pod.get("costPerHr") or MAX_RATE)
        result.update(
            pod_id=pod_id, rate_usd_h=rate, gpu=(pod.get("machine") or {}).get("gpuTypeId")
        )
        say(f"created {pod_id} at ${rate}/h")
        if rate > MAX_RATE:
            raise RuntimeError(f"rate ${rate}/h over the ${MAX_RATE}/h cap")

        host = port = None
        while time.time() - t_created < SSH_WAIT_S:
            time.sleep(15)
            info = client.get_pod(pod_id) or {}
            if info.get("publicIp") and (info.get("portMappings") or {}).get("22"):
                host, port = info["publicIp"], int(info["portMappings"]["22"])
                break
        if not host:
            raise RuntimeError("SSH address never appeared")
        rp.forget_host_key(host, port)
        while True:  # sshd may lag the port mapping; bounded by the whole-run deadline
            if ssh(host, port, "true", timeout=40).returncode == 0:
                break
            if time.time() > t_start + RUN_DEADLINE_S:
                raise RuntimeError("sshd never answered")
            time.sleep(15)
        say(f"ssh up: root@{host} -p {port}")
        r = subprocess.run(
            [
                "scp",
                "-P",
                str(port),
                *SSH_OPTS,
                str(ROOT / "scripts" / "pod_killswitch.sh"),
                f"root@{host}:/tmp/pod_killswitch.sh",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"scp failed: {r.stderr[-300:]}")
        ssh(host, port, "mkdir -p /ws/emx/scripts && mv /tmp/pod_killswitch.sh /ws/emx/scripts/")

        r = ssh(
            host,
            port,
            "tr '\\0' '\\n' </proc/1/environ | sed -n 's/^RUNPOD_POD_ID=//p'; "
            "tr '\\0' '\\n' </proc/1/environ | grep -c '^RUNPOD_API_KEY=.'",
        )
        lines = r.stdout.split()
        check(
            "env",
            lines[:1] == [pod_id] and lines[1:2] == ["1"],
            f"RUNPOD_POD_ID={lines[:1]}, RUNPOD_API_KEY present={lines[1:2] == ['1']}",
        )

        r = ssh(host, port, "bash /ws/emx/scripts/pod_killswitch.sh arm wrongpod123 4102444800")
        check(
            "refuse",
            r.returncode == 1 and "REFUSED" in r.stdout and " ARMED pod=" not in r.stdout,
            f"rc={r.returncode} {r.stdout.strip()[-120:]}",
        )

        deadline = int(time.time()) + args.arm_s
        line = arm_slot_live(pod_id, host, port, deadline)
        # key_check=ok or read403 (the pod key gets 403 reading its own pod: first live run)
        check(
            "arm",
            f" ARMED pod={pod_id} deadline={deadline}" in line
            and ("key_check=ok" in line or "key_check=read403" in line),
            line,
        )
        result["deadline"] = deadline

        line2 = arm_slot_live(pod_id, host, port, deadline)
        r = ssh(
            host,
            port,
            "pgrep -fc '[p]od_killswitch.sh watch'; bash /ws/emx/scripts/pod_killswitch.sh status",
        )
        out = r.stdout.split("\n")
        check(
            "single",
            " ARMED pod=" in line2
            and out[0].strip() == "1"
            and "ARMED" in r.stdout
            and "watcher=yes" in r.stdout,
            f"rearm={'ARMED' in line2} watchers={out[0].strip()} status={out[1:2]}",
        )

        r = ssh(
            host,
            port,
            'umask 077; k=$(mktemp) && p=$(mktemp) && trap \'rm -f "$k" "$p"\' EXIT && '
            "tr '\\0' '\\n' </proc/1/environ | sed -n 's/^RUNPOD_API_KEY=//p' | tr -d '\\n' "
            '>"$k" && [ -s "$k" ] && ps -eo args >"$p" && '
            'echo argv=$(grep -cF -f "$k" "$p") '
            'log=$(grep -cF -f "$k" /ws/killswitch/killswitch.log)',
        )
        check(
            "no_leak",
            r.returncode == 0 and "argv=0" in r.stdout and "log=0" in r.stdout,
            f"rc={r.returncode} {r.stdout.strip()}",
        )

        r = ssh(host, port, "cat /ws/killswitch/killswitch.log")
        result["killswitch_log_before_deadline"] = r.stdout[-2000:]
        while time.time() < deadline - 30:
            time.sleep(5)
        # stream the pod's log to the laptop: the stop wipes the container, log included
        tail = subprocess.Popen(
            [
                "ssh",
                "-n",
                "-p",
                str(port),
                *SSH_OPTS,
                f"root@{host}",
                "tail -n 0 -F /ws/killswitch/killswitch.log",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        st = (client.get_pod(pod_id) or {}).get("desiredStatus")
        check("no_early", st == "RUNNING", f"desiredStatus={st} 30 s before the deadline")

        stopped_at = None
        statuses = []
        while time.time() < deadline + GRACE_S:
            time.sleep(15)
            st = (client.get_pod(pod_id) or {}).get("desiredStatus")
            statuses.append(f"+{int(time.time() - deadline)}s:{st}")
            if st and st != "RUNNING":
                stopped_at = time.time()
                break
        check(
            "stops",
            stopped_at is not None,
            f"stopped {int(stopped_at - deadline)} s after the deadline"
            if stopped_at
            else f"still running {GRACE_S} s after the deadline: {statuses[-3:]}",
        )
        result["status_trace"] = statuses
        time.sleep(5)
        tail.terminate()
        live_log = tail.communicate(timeout=30)[0]
        result["killswitch_log_at_deadline"] = live_log[-3000:]
        # the route: an "accepted" line, or (a working stop can kill the container before that
        # line is written, live run 4) the last "trying" line before the pod was seen stopped
        lines = live_log.splitlines()
        accepted = [ln for ln in lines if "stop accepted" in ln]
        trying = [ln for ln in lines if "] trying " in ln]
        route = accepted[0] if accepted else (trying[-1] if trying and stopped_at else "")
        result["stop_route"] = route
        check(
            "stop_route",
            bool(route) and "STOP FAILED" not in live_log,
            route or f"no route line streamed: {live_log[-300:]!r}",
        )
        if stopped_at:
            r = ssh(host, port, "true", timeout=40)
            result["ssh_after_stop_rc"] = r.returncode
    except Exception as e:  # noqa: BLE001 - every failure must reach the terminate path
        say(f"ERROR: {type(e).__name__}: {e}")
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        # every pod this run may have made: the ids it knows plus any ks-check pod still listed
        try:
            created_ids.update(p["id"] for p in list_ours() if p.get("id"))
        except Exception as e:  # noqa: BLE001 - cleanup goes on with the ids already known
            say(f"pod list failed during cleanup: {e}")
        from emx import spend

        all_gone = True
        for pid in sorted(created_ids):
            gone = False
            for _ in range(5):
                try:
                    client.terminate_pod(pid)
                except Exception as e:  # noqa: BLE001
                    say(f"terminate {pid} failed: {e}")
                time.sleep(10)
                try:
                    gone = not any(p.get("id") == pid for p in client.list_pods())
                except Exception as e:  # noqa: BLE001
                    say(f"pod list failed: {e}")
                if gone:
                    break
            all_gone = all_gone and gone
            say(f"pod {pid} {'terminated' if gone else 'STILL LISTED - TERMINATE IN CONSOLE'}")
            # billed from creation (or from this run's start for a pod found only by listing)
            hours = (time.time() - (t_created if pid == pod_id else t_start)) / 3600
            usd = round(hours * (rate if pid == pod_id else MAX_RATE), 4)
            spend.record(
                kind="gpu",
                resource=f"runpod:{result.get('gpu') if pid == pod_id else 'unknown'}:{pid}",
                units=round(hours, 4),
                usd=usd,
                exp="infra",
                note="killswitch live check (scripts/killswitch_live_check.py); elapsed x rate, "
                "an upper bound (a self-stopped pod stops billing GPU at the stop)",
            )
            if pid == pod_id:
                result.update(hours=round(hours, 4), usd=usd)
            say(f"cost ${usd} for {pid} ({hours:.3f} h), logged as exp infra")
        result["terminated"] = all_gone
        result["pods"] = sorted(created_ids)
    result["checks"] = checks
    result["wall_s"] = round(time.time() - t_start)
    passed = bool(checks) and all(c["ok"] for c in checks.values()) and len(checks) == 8
    result["passed"] = passed and result.get("terminated", False) and len(created_ids) == 1
    out = ROOT / "logs" / "infra" / f"killswitch-live-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    say(
        f"{'ALL CHECKS PASSED' if result['passed'] else 'CHECK FAILED'}; result in {out.relative_to(ROOT)}"
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
