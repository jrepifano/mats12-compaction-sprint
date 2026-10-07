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

2026-10-03: --exp E1b works unchanged (prefix `e1b-`, which `--all-e1`'s `e1-` never matches).
2026-10-04: --exp I9 works unchanged (prefix `i9-`, which neither `e1-` nor `i1a-` matches; slot
_A = session A, _B = session B, see pod_up.py). After a pod-side killswitch stop, run
`pod_down.py --exp I9 --slot <slot>` to terminate it and record its spend.

2026-10-06 (E3 implementation plan v2 fix 5, v3 fix 4): `--slot S --reservation ID` settles the
RunPod reservation (`emx.runpod_reservations`) after the pod is terminated and its GPU charge is
recorded: the actual dollars, then the unused remainder is released. A failed termination keeps
the liability (nothing is settled or released) and exits non-zero. GPU charging is idempotent
(`emx.runpod.finish_session`): a retry after a crash between termination and charge, or between
charge and settlement, records the charge once and settles once; a pod the event log already
shows terminated is not sent a second DELETE.

2026-10-06 (E3 code review A r1, findings 4 and 10): `--pod-id ID --reservation RID [--slot S]`
finishes one pod launched under a RunPod reservation from durable history only (the reservations
file and logs/pods_<EXP>.jsonl; see `emx.runpod_reservations.unsettled_pods`), so it works after a
watchdog `--all-exp` or a crash has already cleared the slot's .env keys. It refuses a pod that
the reservation did not launch (or, for a pod the reaper adopted from a dead launcher, whose
`created` event does not name that reservation), and with --slot one launched in another slot.
It always sends a DELETE (idempotent); a failed DELETE is tolerated only when the API reports
the pod gone (404 or TERMINATED) or the event log already shows it terminated, else the
liability is kept and it exits 1. Then it records the GPU charge (once, even with a concurrent cleanup: the ledger
check and append are one locked step, `emx.spend.record_once`), settles the pod's actual dollars,
releases the remainder, and clears the slot's keys if they still name this pod. Re-running it is
a no-op that exits 0.

2026-10-06 (E9 implementation plan v4, unit F2, "E9 admission protocol"; additive): for --exp E9
every termination is followed by the charge and the settlement as ONE step under the E9 admission
lock (`emx.runpod.e9_charge_and_settle`: `emx.e9.budget.admission_lock()`, else the same flock on
logs/E9-admission.lock): the GPU charge, recorded with the pod's stage (its `created` event's,
from pod_up.py --stage), then the reservation's settle row and the release of its remainder, so an
admission counts the pod once (as its commitment before, as its charge after). The reservation is
--reservation when given, else the one that launched the pod (emx.runpod_reservations), so
`--slot`, `--all-exp` and `--pod-id` all settle. The DELETE is sent before the lock is taken.

2026-10-07 (E9 code review R3 #5): every E9 closure goes through `emx.runpod.e9_close`
(`emx.e9.budget.settle_pod` under the admission lock): after a DELETE, for pods missing from the
API, for reaped untracked pods, and on retries. Termination and settlement are reported apart
(`E9_TERMINATED pod=<id>` / `E9_SETTLED pod=<id> ok|FAILED <why>`). E9 exit codes: 0 every pod
terminated and settled; 1 a termination failed (the pod may still bill; nothing cleared);
3 terminated but a settlement failed (the slot's keys are kept; rerun to settle: every step is
idempotent); 2 usage. `--all-exp` and `--untracked` also retry every closed-but-unsettled E9
pod from durable history (`emx.runpod_reservations.unsettled_pods`), even after it left the API.

Usage: uv run python scripts/pod_down.py [--exp I1a|E1b] --slot _A | --all-exp | --all-e1 |
       --untracked [--dry-run] [--reason TEXT]
       uv run python scripts/pod_down.py --exp E3 --pod-id ID --reservation RID [--slot S]
       uv run python scripts/pod_down.py --exp E9 --slot _A [--reservation RID] | --all-exp |
           --pod-id ID --reservation RID
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


def already_terminated(pod_id: str) -> bool:
    """The event log closed this pod's session (only after a successful DELETE, or when the API
    no longer listed it): a retry goes straight to the charge."""
    return any(
        s["pod_id"] == pod_id and not s["running"] for s in rp.pod_sessions(rp.read_pod_events())
    )


def terminate(client: rp.RunPod, pod_id: str, reason: str, gone_ok: bool = False) -> bool:
    """DELETE the pod, then log it and record its charge (idempotently). `gone_ok`: the API no
    longer has the pod and the event log already shows it terminated (a retry after a crash), so
    a failed DELETE is tolerated and the missing charge, if any, is recorded."""
    try:
        client.terminate_pod(pod_id)
    except rp.RunPodError as e:
        if not gone_ok:
            print(f"terminate {pod_id} FAILED: {e}", file=sys.stderr)
            return False
        print(f"pod {pod_id}: DELETE failed ({e}) but it is gone and logged terminated")
    if client.dry_run:
        return True
    if rp.EXP == "E9":  # termination only; the caller settles (e9_settle)
        print(f"E9_TERMINATED pod={pod_id}")
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


E9_UNSETTLED = 3  # exit code (E9): terminated, but a settlement failed; rerun to settle


def e9_settle(pod_id: str, reason: str, rid: str | None = None) -> bool:
    """E9: the pod's charge and settlement (`emx.runpod.e9_close`, under the admission lock),
    reported apart from its termination. True when settled, or charged with nothing to settle
    (an orphan no reservation covers)."""
    r = rp.e9_close(pod_id, reason, rid)
    s = r["session"]
    if s is not None:
        print(f"pod {pod_id}: {s['hours']:.2f} h x ${s['rate']}/h = ${s['usd']:.2f}")
    if r["settled"]:
        print(f"E9_SETTLED pod={pod_id} ok reservation={r['reservation_id']} "
              "(charge then settle under the E9 admission lock)")
        return True
    if r["reservation_id"] is None and r["charged"]:
        print(f"E9_SETTLED pod={pod_id} ok ({r['why']})")
        return True
    print(f"E9_SETTLED pod={pod_id} FAILED {r['why']}", file=sys.stderr)
    return False


def e9_retry_unsettled(skip: set[str], reason: str) -> tuple[int, int]:
    """E9: settle every closed-but-unsettled pod (durable history; also after it left the API),
    except `skip`. Returns (tried, failed)."""
    from emx import runpod_reservations as rr

    tried = failed = 0
    for u in rr.unsettled_pods(rp.EXP):
        if u["pod_id"] in skip or not u["terminated"]:
            continue
        tried += 1
        if not e9_settle(u["pod_id"], f"{reason}: retry of an unsettled pod", u["reservation_id"]):
            failed += 1
        elif u["slot"] and rp.env(f"RUNPOD_POD_ID{u['slot']}") == u["pod_id"]:
            clear_slot(u["slot"])
    return tried, failed


def settle_for_exp(rid: str, pod_id: str, reason: str) -> bool:
    """`settle`, or for E9 the charge-then-settle under the admission lock (idempotent)."""
    if rp.EXP != "E9":
        return settle(rid, pod_id)
    return e9_settle(pod_id, reason, rid)


def settle(rid: str, pod_id: str) -> bool:
    """Settle the pod's actual GPU dollars (from its closed session) under `rid` and release the
    remainder. Both are idempotent, so a retry after a crash between them is safe."""
    from emx import runpod_reservations as rr

    s = next((x for x in rp.pod_sessions(rp.read_pod_events()) if x["pod_id"] == pod_id), None)
    if s is None or s["running"]:
        print(
            f"reservation {rid} NOT settled: no closed session for {pod_id}; settle by hand",
            file=sys.stderr,
        )
        return False
    try:
        rr.settle(rid, s["usd"], pod_id=pod_id, note=f"pod_down: {pod_id}")
        rr.release(rid, note=f"pod_down: unused after {pod_id}")
    except rr.ReservationError as e:
        print(f"reservation {rid} NOT settled: {e}", file=sys.stderr)
        return False
    print(f"reservation {rid}: settled ${s['usd']:.2f} for {pod_id}; remainder released")
    return True


def api_gone(client: rp.RunPod, pod_id: str) -> tuple[bool, dict | None]:
    """(the API says the pod is gone: 404 or TERMINATED, the API's view of it or None). Any other
    failure to read it is not "gone"."""
    try:
        info = client.get_pod(pod_id)
    except rp.RunPodError as e:
        return "HTTP 404" in str(e), None
    if not info:
        return True, None
    return str(info.get("desiredStatus", "")).upper() == "TERMINATED", info


def down_by_pod_id(
    client: rp.RunPod, pod_id: str, rid: str, slot: str | None, reason: str, dry_run: bool
) -> int:
    """Terminate (if still running), charge once, settle and release one reserved pod, using
    durable history only (module docstring, 2026-10-06)."""
    from emx import runpod_reservations as rr

    R = rr.get(rid)
    if R is None or R["exp"] != rp.EXP:
        why = "unknown" if R is None else f"belongs to {R['exp']}"
        print(f"REFUSED: reservation {rid} is {why}, not an {rp.EXP} reservation", file=sys.stderr)
        return 1
    events = rp.read_pod_events()
    logged_slot = next(
        (
            e.get("slot")
            for e in events
            if e.get("event") == "created" and e.get("pod_id") == pod_id
        ),
        None,
    )
    p = rr.pod(pod_id)
    if p is not None:
        launch_slot = p["slot"] or logged_slot or None
        if p["reservation_id"] != rid:
            print(
                f"REFUSED: pod {pod_id} was launched under {p['reservation_id']}, not {rid}",
                file=sys.stderr,
            )
            return 1
    else:
        made = [
            e
            for e in events
            if e.get("event") == "created"
            and e.get("pod_id") == pod_id
            and e.get("reservation_id") == rid
        ]
        if not made:
            print(
                f"REFUSED: no launch row or {rp.POD_EVENTS.name} `created` event ties pod "
                f"{pod_id} to reservation {rid}",
                file=sys.stderr,
            )
            return 1
        launch_slot = made[-1].get("slot") or None
    if slot is not None and launch_slot and launch_slot != slot:
        print(
            f"REFUSED: pod {pod_id} was launched in slot {launch_slot}, not {slot}", file=sys.stderr
        )
        return 1
    if dry_run:
        client.terminate_pod(pod_id)
        print(f"DRY RUN: would charge pod {pod_id}, settle and release reservation {rid}")
        return 0
    if p is not None and p["settled"]:
        print(f"pod {pod_id}: already settled under {rid}")
        if already_terminated(pod_id):
            if rp.EXP == "E9":  # a charge lost to a crash, through settle_pod (idempotent)
                if not e9_settle(pod_id, reason, rid):
                    return E9_UNSETTLED
            else:
                rp.finish_session(pod_id, reason)  # a charge lost to a crash is recorded, once
    else:
        gone, info = api_gone(client, pod_id)
        name = (info or {}).get("name")
        if name and not str(name).startswith(rp.POD_NAME_PREFIX):
            print(
                f"REFUSED: pod {pod_id} is named {name!r}, not {rp.POD_NAME_PREFIX}*",
                file=sys.stderr,
            )
            return 1
        # DELETE even when the log says terminated (cheap, idempotent); its failure is tolerated
        # only for a pod the API reports gone or the log already closed
        if not terminate(client, pod_id, reason, gone_ok=gone or already_terminated(pod_id)):
            print(f"reservation {rid} KEPT: the pod may still bill", file=sys.stderr)
            return 1
        if not settle_for_exp(rid, pod_id, reason):
            return E9_UNSETTLED if rp.EXP == "E9" else 1
    slot_key = slot or launch_slot
    if slot_key and rp.env(f"RUNPOD_POD_ID{slot_key}") == pod_id:
        clear_slot(slot_key)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--slot", help="suffix of the .env pod keys (see pod_up.py)")
    g.add_argument("--all-exp", action="store_true", help="every pod of --exp (by name prefix)")
    g.add_argument("--all-e1", action="store_true", help="every e1-* pod (always E1)")
    g.add_argument("--untracked", action="store_true", help="only prefixed pods not in the log")
    ap.add_argument("--exp", default=rp.DEFAULT_EXP, help="experiment (default E1)")
    ap.add_argument("--reason", default="pod_down")
    ap.add_argument(
        "--reservation",
        help="with --slot or --pod-id: settle this emx.runpod_reservations id afterwards",
    )
    ap.add_argument(
        "--pod-id",
        help="with --reservation (and optionally --slot): finish this reserved pod from durable "
        "history, even after its slot's .env keys were cleared",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.pod_id:
        if not args.reservation:
            ap.error("--pod-id needs --reservation")
        if args.all_exp or args.all_e1 or args.untracked:
            ap.error("--pod-id goes only with --reservation and --slot")
        rp.configure(args.exp)
        if args.slot:
            rp.check_slot(args.slot)
        key = rp.env("RUNPOD_API_KEY") or ("dry-run-key" if args.dry_run else "")
        client = rp.RunPod(key, dry_run=args.dry_run)
        return down_by_pod_id(
            client, args.pod_id, args.reservation, args.slot, args.reason, args.dry_run
        )
    if not (args.slot or args.all_exp or args.all_e1 or args.untracked):
        ap.error("one of the arguments --slot --all-exp --all-e1 --untracked --pod-id is required")
    if args.all_e1:
        if args.exp != "E1":
            ap.error("--all-e1 always means E1; use --exp X --all-exp for another experiment")
        args.all_exp = True
    if args.reservation and not args.slot:
        ap.error("--reservation needs --slot")
    rp.configure(args.exp)
    key = rp.env("RUNPOD_API_KEY") or ("dry-run-key" if args.dry_run else "")
    client = rp.RunPod(key, dry_run=args.dry_run)

    if args.untracked:
        if args.dry_run:
            client.list_pods()
            return 0
        reaped = rp.reap_untracked(client, args.reason)
        print(f"{len(reaped)} untracked {rp.POD_NAME_PREFIX}* pod(s) terminated {reaped}")
        if rp.EXP == "E9":  # closed-but-unsettled pods, also after they left the API
            tried, failed = e9_retry_unsettled(set(), args.reason)
            if tried:
                print(f"E9: retried {tried} unsettled pod(s); {failed} still unsettled")
            return E9_UNSETTLED if failed else 0
        return 0

    if args.slot:
        slot = rp.check_slot(args.slot)
        pod_id = rp.env(f"RUNPOD_POD_ID{slot}") or ("<pod-id>" if args.dry_run else "")
        if not pod_id:
            print(f"no pod in slot {slot}", file=sys.stderr)
            return 1
        api_pod = None
        if not args.dry_run:
            try:
                api_pod = client.get_pod(pod_id)
            except rp.RunPodError:
                api_pod = None  # gone from the API: the event log alone decides
            owned, why = rp.slot_pod_ok(slot, pod_id, api_pod)
            if not owned:
                print(f"REFUSED: {why}", file=sys.stderr)
                return 1
        gone = not args.dry_run and (
            api_pod is None or str(api_pod.get("desiredStatus", "")).upper() == "TERMINATED"
        )
        ok = terminate(client, pod_id, args.reason, gone_ok=gone and already_terminated(pod_id))
        if not ok:
            if args.reservation:
                print(
                    f"reservation {args.reservation} KEPT: the pod may still bill", file=sys.stderr
                )
            return 1
        if args.dry_run:
            if args.reservation:
                print(f"DRY RUN: would settle and release reservation {args.reservation}")
            return 0
        if rp.EXP == "E9":  # always settled (the reservation that launched it, if not given)
            if not e9_settle(pod_id, args.reason, args.reservation):
                print(f"slot {slot} KEPT: pod {pod_id} terminated but not settled; rerun",
                      file=sys.stderr)
                return E9_UNSETTLED
            clear_slot(slot)
            return 0
        if args.reservation and not settle_for_exp(args.reservation, pod_id, args.reason):
            return 1  # slot kept, so a retry can settle (charge and settle are idempotent)
        clear_slot(slot)
        return 0

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
    unsettled = 0
    for p in pods:
        if terminate(client, p["id"], args.reason):
            if rp.EXP == "E9" and not e9_settle(p["id"], args.reason):
                unsettled += 1
                continue  # slot kept: a rerun settles
            if slot_of.get(p["id"]):
                clear_slot(slot_of[p["id"]])
        else:
            ok = False
    # pods the event log still counts as running but the API no longer lists (terminated in the
    # console, or lost): close them at now, which over- rather than under-counts their cost
    live = {p["id"] for p in pods}
    for s in sessions:
        if s["running"] and s["pod_id"] not in live:
            why = f"{args.reason}: not listed by the API; closed at now"
            if rp.EXP == "E9":
                if not e9_settle(s["pod_id"], why):
                    unsettled += 1
                    continue
            else:
                rp.finish_session(s["pod_id"], why)
            clear_slot(s["slot"])
            print(f"pod {s['pod_id']} was not listed by the API; closed in the log at now")
    if rp.EXP == "E9":  # closed-but-unsettled pods from earlier passes
        _, failed = e9_retry_unsettled(live, args.reason)
        unsettled += failed
    verdict = "all terminated" if ok else "SOME TERMINATIONS FAILED"
    if unsettled:
        verdict += f"; {unsettled} NOT SETTLED (rerun)"
    print(f"{len(pods) + len(reaped)} {rp.EXP} pod(s) found; {verdict}")
    if not ok:
        return 1
    return E9_UNSETTLED if unsettled else 0


if __name__ == "__main__":
    sys.exit(main())
