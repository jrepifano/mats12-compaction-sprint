"""Create a RunPod GPU pod for $EMX_EXP (default E1), trying the GPU fallback order.

Port of ../mats12_neel_working/scripts/pod_up.py. Changes: shared client `emx.runpod` (browser
User-Agent on every request, retries); E1 image (CUDA 13 / Ubuntu 24.04 / Python 3.12); GPU
fallback order H100 community -> H100 NVL -> H100 secure -> A100-SXM4 80GB (implementation-plan
§5); the pre-launch gate is `emx.spend.check_total()` (the one combined guard); pod ids live in
.env as RUNPOD_POD_ID<slot>/POD_SSH_HOST<slot>/POD_SSH_PORT<slot>; lifecycle events go to
logs/pods_E1.jsonl and dollars to logs/costs.jsonl via emx.spend. `--dry-run` prints each create
request and sends nothing (no gate bypass: the gate still runs when emx.spend is importable).
2026-09-29 (I1a implementation plan §2 U1; gpt-6-sol review U1 r1): experiment-generic via
`--exp` (default E1, never the ambient env): --exp I1a names pods i1a-<slot>, logs to
logs/pods_I1a.jsonl and gates on the I1a caps. Pod creation is not idempotent, so:
- before launching, the API pod list is reconciled against the event log, and an untracked pod
  with this experiment's prefix refuses the launch (exit 5; pod_down.py --exp X --all-exp);
- `POST /pods` is sent once per GPU option (never retried); if its outcome is unknown
  (`CreateUncertain`), every untracked prefixed pod is terminated and costed, and we exit 1;
- a `create_pending` note (logs/pods_<EXP>.pending.jsonl) shields the pod being created from
  the watchdog's reaper for a few minutes.
I1a only: a live I1a watchdog (fresh logs/watchdog_I1a.heartbeat) is required, and until
logs/approvals/I1a-P1.approved exists all GPU spend is capped at the P0 smoke cap ($6).

Usage: uv run python scripts/pod_up.py [--exp I1a] --slot _A [--start N] [--no-fallback]
       [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emx import runpod as rp

GATE_REFUSED = 4  # exit code: the budget gate refused (pod_hunt.sh stops instead of retrying)
UNTRACKED = 5  # exit code: an untracked pod with our prefix exists, or no live I1a watchdog


def gate(opt: rp.GpuOption, reserve_hours: float, dry_run: bool) -> None:
    """Refuse a launch whose next `reserve_hours` would pass a cap (exits non-zero)."""
    running = rp.running_projection(rp.pod_sessions(rp.read_pod_events()), reserve_hours)
    projected = reserve_hours * opt.rate_usd_h
    print(
        f"budget gate ({rp.EXP}): this pod {reserve_hours} h x ${opt.rate_usd_h}/h = "
        f"${projected:.2f}; "
        f"running pods incl. reserve ${running:.2f}"
    )
    ceiling = rp.gpu_ceiling()
    if ceiling is not None:
        after = rp.ledger_spent("gpu") + running + projected
        print(f"budget gate ({rp.EXP}): P0 ceiling ${ceiling:.2f} until P1 approval; ${after:.2f}")
        if after > ceiling:
            print(f"REFUSED: ${after:.2f} > P0 ceiling ${ceiling:.2f}", file=sys.stderr)
            sys.exit(GATE_REFUSED)
    try:
        rp.ledger_check(projected, running)
    except ImportError:
        if not dry_run:
            raise
        print("budget gate: emx.spend not importable; skipped in --dry-run only")
    except (SystemExit, Exception) as e:  # noqa: BLE001 - any refusal stops the launch
        print(f"REFUSED by the combined guard: {e!r}", file=sys.stderr)
        sys.exit(GATE_REFUSED)


def preflight(client: rp.RunPod, dry_run: bool) -> None:
    """Refuse a launch while an untracked prefixed pod exists, or (I1a) no watchdog is live."""
    if rp.EXP != "E1":
        live, why = rp.watchdog_live()
        print(f"watchdog ({rp.EXP}): {'live' if live else 'NOT LIVE'} ({why})")
        if not live and not dry_run:
            print(f"REFUSED: start scripts/{rp.EXP.lower()}_watchdog.sh first", file=sys.stderr)
            sys.exit(UNTRACKED)
    if dry_run:
        print("reconcile: API pod list not fetched in --dry-run")
        return
    stray = rp.untracked_pods(
        client.list_pods(), rp.read_pod_events(), rp.read_pod_events(rp.pending_path()), grace_s=0
    )
    if stray:
        names = ", ".join(f"{p.get('name')} ({p.get('id')})" for p in stray)
        print(
            f"REFUSED: untracked {rp.POD_NAME_PREFIX}* pod(s) exist: {names}; terminate them with "
            f"pod_down.py --exp {rp.EXP} --all-exp",
            file=sys.stderr,
        )
        sys.exit(UNTRACKED)


def recover_uncertain_create(client: rp.RunPod, err: Exception) -> int:
    """A create whose outcome is unknown: find and terminate any pod it may have made."""
    print(f"create outcome UNKNOWN ({err}); reconciling against the API", file=sys.stderr)
    reaped: list = []
    for _ in range(3):  # the new pod may take a moment to be listed
        time.sleep(20)
        try:
            reaped += rp.reap_untracked(client, "pod_up: uncertain create", grace_s=0)
        except rp.RunPodError as e:
            print(f"pod list FAILED ({e}); CHECK THE RUNPOD CONSOLE NOW", file=sys.stderr)
            return 1
    print(f"terminated {len(reaped)} untracked pod(s) {reaped}; re-run pod_up", file=sys.stderr)
    return 1


def wait_for_ssh(client: rp.RunPod, pod_id: str, timeout_s: int, poll_s: int = 15):
    """Poll until a public IP and port 22 mapping exist. Returns (host, port) or (None, None)."""
    deadline = time.time() + timeout_s
    rate_logged = False
    while time.time() < deadline:
        time.sleep(poll_s)
        info = client.get_pod(pod_id) or {}
        if info.get("costPerHr") and not rate_logged:
            rp.log_pod_event(
                {"event": "rate", "pod_id": pod_id, "rate_usd_h": float(info["costPerHr"])}
            )
            rate_logged = True
        ip = info.get("publicIp") or ""
        mapping = info.get("portMappings") or {}
        if ip and mapping.get("22"):
            return ip, int(mapping["22"])
        print(f"  waiting... status={info.get('desiredStatus')} ip={ip or '-'}")
    return None, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default=rp.DEFAULT_EXP, help="experiment (default E1)")
    ap.add_argument("--slot", default="_A", help="suffix of the .env pod keys, e.g. _A")
    ap.add_argument(
        "--start",
        type=int,
        default=0,
        help="index into the fallback order to start from (0 = H100 community)",
    )
    ap.add_argument("--no-fallback", action="store_true", help="try only the --start option")
    ap.add_argument(
        "--reserve-hours",
        type=float,
        default=1.0,
        help="hours of this pod (plus running pods) the gate must fit under the caps",
    )
    ap.add_argument("--disk-gb", type=int, default=200)
    ap.add_argument("--ssh-timeout", type=int, default=900)
    ap.add_argument("--dry-run", action="store_true", help="print the requests; send nothing")
    args = ap.parse_args(argv)
    rp.configure(args.exp)
    slot = rp.check_slot(args.slot)

    if not args.dry_run and rp.env(f"RUNPOD_POD_ID{slot}"):
        sys.exit(
            f"slot {slot} is occupied by pod {rp.env(f'RUNPOD_POD_ID{slot}')}; "
            f"run pod_down.py --slot {slot} first"
        )
    key = rp.env("RUNPOD_API_KEY") or ("dry-run-key" if args.dry_run else "")
    client = rp.RunPod(key, dry_run=args.dry_run)
    pub = Path.home() / ".ssh" / "id_ed25519.pub"
    public_key = pub.read_text().strip() if pub.exists() else "<~/.ssh/id_ed25519.pub missing>"

    preflight(client, args.dry_run)
    options = rp.gpu_options(args.start)
    if args.no_fallback:
        options = options[:1]
    pod = opt = None
    for opt in options:
        gate(opt, args.reserve_hours, args.dry_run)
        body = rp.pod_body(opt, slot, public_key, container_disk_gb=args.disk_gb)
        if not args.dry_run:
            rp.log_create_pending(body["name"], slot)
        try:
            pod = client.create_pod(body)
        except rp.CreateUncertain as e:
            return recover_uncertain_create(client, e)
        except rp.RunPodError as e:
            print(f"create failed on {opt.gpu_type_id} ({opt.cloud}): {e}", file=sys.stderr)
            continue
        if args.dry_run:
            continue  # print every option in order
        break
    if args.dry_run:
        return 0
    if not pod:
        print("no GPU in the fallback order could be created; try pod_hunt.sh", file=sys.stderr)
        return 1

    pod_id = pod["id"]
    api_rate = pod.get("costPerHr")
    rate = float(api_rate) if api_rate else opt.rate_usd_h
    # persist the id BEFORE waiting for SSH so a timeout never leaks an untracked pod
    rp.set_env_var(f"RUNPOD_POD_ID{slot}", pod_id)
    rp.log_pod_event(
        {
            "event": "created",
            "pod_id": pod_id,
            "slot": slot,
            "gpu": opt.gpu_type_id,
            "cloud": opt.cloud,
            "image": rp.IMAGE,
            "rate_usd_h": rate,
            "rate_source": "api" if api_rate else "estimate",
        }
    )
    print(f"pod created: {pod_id} ({opt.gpu_type_id}, {opt.cloud}, ${rate}/h)")

    host = port = None
    poll_error = None
    try:
        host, port = wait_for_ssh(client, pod_id, args.ssh_timeout)
    except Exception as e:  # noqa: BLE001 - any polling failure must reach the terminate path
        poll_error = f"{type(e).__name__}: {e}"
    if not host:
        reason = "ssh timeout" if poll_error is None else f"poll error: {poll_error}"
        try:
            client.terminate_pod(pod_id)
        except rp.RunPodError as e:
            print(
                f"SSH never came up ({reason}) and the DELETE FAILED ({e}); pod {pod_id} may "
                "still be billing - terminate it in the RunPod console; id kept in .env",
                file=sys.stderr,
            )
            return 1
        rp.finish_session(pod_id, f"pod_up: {reason}")
        rp.set_env_var(f"RUNPOD_POD_ID{slot}", "")
        print(f"{reason}; pod {pod_id} terminated", file=sys.stderr)
        return 1

    rp.set_env_var(f"POD_SSH_HOST{slot}", host)
    rp.set_env_var(f"POD_SSH_PORT{slot}", str(port))
    rp.log_pod_event(
        {"event": "running", "pod_id": pod_id, "slot": slot, "host": host, "port": port}
    )
    print(f"SSH ready: ssh -p {port} root@{host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
