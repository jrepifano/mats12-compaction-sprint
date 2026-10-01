"""Terminate RunPod pods of one experiment (--exp, default E1) and record their GPU dollars.

Port of ../mats12_neel_working/scripts/pod_down.py. Changes: shared client `emx.runpod` (browser
User-Agent, retries); session cost = creation-to-termination x rate from logs/pods_E1.jsonl,
written to logs/costs.jsonl through emx.spend (kind=gpu); `--all-e1` terminates every pod whose
name starts with `e1-` according to the API itself (the watchdog's hard stop, which also catches a
pod missing from the event log); `--dry-run` prints the requests and sends nothing.
2026-09-29 (I1a implementation plan §2 U1; gpt-6-sol review U1 r1): experiment-generic via
`--exp` (default E1; the ambient env is never read). `--all-exp` terminates every pod of `--exp`
(prefix `e1-` or `i1a-`), costing any the event log does not know first; `--all-e1` always
means E1 (prefix `e1-`), whatever else is set, so the unchanged E1 watchdog keeps policing E1.
`--untracked` terminates only prefixed pods missing from the event log (the I1a watchdog runs it
every tick; a pod pod_up is creating right now is spared for a few minutes). `--slot` refuses a
pod that this experiment's event log did not create in that slot, or whose API name lacks the
prefix.

Usage: uv run python scripts/pod_down.py [--exp I1a] --slot _A | --all-exp | --all-e1 |
       --untracked [--dry-run] [--reason TEXT]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emx import runpod as rp


def clear_slot(slot: str) -> None:
    if not slot:
        return
    for k in ("RUNPOD_POD_ID", "POD_SSH_HOST", "POD_SSH_PORT"):
        if rp.env(f"{k}{slot}"):
            rp.set_env_var(f"{k}{slot}", "")


def terminate(client: rp.RunPod, pod_id: str, reason: str) -> bool:
    try:
        client.terminate_pod(pod_id)
    except rp.RunPodError as e:
        print(f"terminate {pod_id} FAILED: {e}", file=sys.stderr)
        return False
    if client.dry_run:
        return True
    s = rp.finish_session(pod_id, reason)
    if s is None:
        print(
            f"pod {pod_id} terminated but has no `created` event: its cost is NOT in the "
            "ledger; add it by hand from the RunPod console",
            file=sys.stderr,
        )
    else:
        print(f"pod {pod_id} terminated: {s['hours']:.2f} h x ${s['rate']}/h = ${s['usd']:.2f}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--slot", help="suffix of the .env pod keys (see pod_up.py)")
    g.add_argument("--all-exp", action="store_true", help="every pod of --exp (by name prefix)")
    g.add_argument("--all-e1", action="store_true", help="every e1-* pod (always E1)")
    g.add_argument("--untracked", action="store_true", help="only prefixed pods not in the log")
    ap.add_argument("--exp", default=rp.DEFAULT_EXP, help="experiment (default E1)")
    ap.add_argument("--reason", default="pod_down")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.all_e1:
        if args.exp != "E1":
            ap.error("--all-e1 always means E1; use --exp X --all-exp for another experiment")
        args.all_exp = True
    rp.configure(args.exp)
    key = rp.env("RUNPOD_API_KEY") or ("dry-run-key" if args.dry_run else "")
    client = rp.RunPod(key, dry_run=args.dry_run)

    if args.untracked:
        if args.dry_run:
            client.list_pods()
            return 0
        reaped = rp.reap_untracked(client, args.reason)
        print(f"{len(reaped)} untracked {rp.POD_NAME_PREFIX}* pod(s) terminated {reaped}")
        return 0

    if args.slot:
        slot = rp.check_slot(args.slot)
        pod_id = rp.env(f"RUNPOD_POD_ID{slot}") or ("<pod-id>" if args.dry_run else "")
        if not pod_id:
            print(f"no pod in slot {slot}", file=sys.stderr)
            return 1
        if not args.dry_run:
            try:
                api_pod = client.get_pod(pod_id)
            except rp.RunPodError:
                api_pod = None  # gone from the API: the event log alone decides
            owned, why = rp.slot_pod_ok(slot, pod_id, api_pod)
            if not owned:
                print(f"REFUSED: {why}", file=sys.stderr)
                return 1
        ok = terminate(client, pod_id, args.reason)
        if ok and not args.dry_run:
            clear_slot(slot)
        return 0 if ok else 1

    # --all-exp: the API is the source of truth for what is billing
    if args.dry_run:
        client.list_pods()
        terminate(client, f"<each {rp.POD_NAME_PREFIX}* pod id>", args.reason)
        return 0
    reaped = rp.reap_untracked(client, args.reason, grace_s=0)  # costed, then terminated
    pods = [
        p
        for p in client.list_pods()
        if str(p.get("name", "")).startswith(rp.POD_NAME_PREFIX) and p["id"] not in reaped
    ]
    sessions = rp.pod_sessions(rp.read_pod_events())
    slot_of = {s["pod_id"]: s["slot"] for s in sessions}
    ok = True
    for p in pods:
        if terminate(client, p["id"], args.reason):
            if slot_of.get(p["id"]):
                clear_slot(slot_of[p["id"]])
        else:
            ok = False
    # pods the event log still counts as running but the API no longer lists (terminated in the
    # console, or lost): close them at now, which over- rather than under-counts their cost
    live = {p["id"] for p in pods}
    for s in sessions:
        if s["running"] and s["pod_id"] not in live:
            rp.finish_session(s["pod_id"], f"{args.reason}: not listed by the API; closed at now")
            clear_slot(s["slot"])
            print(f"pod {s['pod_id']} was not listed by the API; closed in the log at now")
    verdict = "all terminated" if ok else "SOME TERMINATIONS FAILED"
    print(f"{len(pods) + len(reaped)} {rp.EXP} pod(s) found; {verdict}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
