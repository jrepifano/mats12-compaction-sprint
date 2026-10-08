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
2026-10-03 (E1b implementation plan §2 U7): --exp E1b (pods e1b-_A / e1b-_B, logs/pods_E1b.jsonl,
the E1b caps) needs a live E1b watchdog (logs/watchdog_E1b.heartbeat, scripts/e1b_watchdog.sh),
as every experiment but E1 does; no ceiling. Its gate counts every running E1b pod, so the second
slot's launch must fit beside the first.
2026-10-04 (I9 implementation plan §2 U6, §4 P1 and P3): --exp I9 names pods i9-<slot>, logs to
logs/pods_I9.jsonl, gates on CAPS["I9"] (min($36, $65 - $2 - judges)) and needs a live I9
watchdog (logs/watchdog_I9.heartbeat, scripts/i9_watchdog.sh). The slot names the session:
slot _A = session A (paid step P1: smoke, sampling, last-token capture), slot _B = session B
(P3: readers); any other slot is refused. A session's launch is refused (exit 4, so pod_hunt
stops) until its approval file exists: logs/approvals/I9-P1.approved for _A,
logs/approvals/I9-P3.approved for _B (written by the main session after Jacob's go-ahead; a
non-empty file with a line starting with a UTC timestamp, as I1a's). --dry-run reports the
missing approval and continues. I9's GPU fallback order is H100 community -> H100 secure ->
H100 NVL (no A100: the prereg's hours assume an H100). Docker Hub may rate-limit unauthenticated
image pulls (an I9 pod was stopped for this on 2026-10-04): the pod can show RUNNING while its
image never arrives, so check its status after launch (wait_for_ssh prints it; no SSH by
--ssh-timeout means the pull failed and the pod is terminated).

2026-10-06 (E3 implementation plan v2 fix 5, v3 fix 4, v4 fix 2; shared contract
docs/experiments/shared/runpod-reservations.md): `--reservation ID --budget-usd B` launches under
an admitted RunPod reservation (`emx.runpod_reservations`); required for --exp E3 (except
--dry-run), optional elsewhere. `t0` is taken immediately before each create request, and a
durable `creating` row (t0, reservation, slot, pod name, budget, rate) is appended before it. On
success the pod's `created` event is billed from t0, the reservation's `launch` row records the
pod with deadline min(t0 + B / rate, the experiment's global kill-line deadline), and
`EMX_POD_DEADLINE=<epoch>` is printed. A lost or failed create response is reconciled by listing
pods named `<exp>-<slot>`: a found pod is adopted into the launch; the reservation is released
only once no pod exists (a failed listing keeps it, so its liability still counts). The gate
checks the budget B, not --reserve-hours x rate, when B is given.

2026-10-06 (E3 code review A r1, findings 1 and 7): with --reservation, pod_up prints exactly one
status line on stdout, `EMX_POD_CREATE=<status> reservation=<id>[ pod=<pod id>]`, and its exit
code says what the caller may do with the reservation:
- `ok`, exit 0: launched; `EMX_POD_DEADLINE=<epoch>` (printed once SSH is up, after any reprice)
  is the pod's reservation deadline.
- `uncertain`, exit 6 (UNCERTAIN): a pod may exist and bill under this reservation (a failed
  pod listing after a lost or failed create, a DELETE that failed, an unexpected error after a
  create request was sent). The reservation is KEPT; the caller must NOT release it, and leaves
  it to recovery (`emx.runpod_reservations.unarmed_overdue` / `unsettled_pods`, pod_down.py).
- `closed`, exit 1: no pod bills; the reservation was released or the pod settled.
- `none`, exit 1, 2, 4 or 5: refused before any create request was sent (or every request sent
  was reconciled to no pod); the reservation is untouched and the caller may release it.
A run killed by a signal prints no status line: treat it as `uncertain`. While polling for SSH,
an API `costPerHr` above the launch rate reprices the reservation (`rr.reprice`: higher rate,
deadline shortened from t0, never lengthened) before the pod is ever armed.

2026-10-06 (E9 implementation plan v4, unit F2; prereg §7.3, §8.7; additive): --exp E9 names
pods e9-<slot>, logs to logs/pods_E9.jsonl and needs a live E9 watchdog
(logs/watchdog_E9.heartbeat, scripts/e9_watchdog.sh). `--stage` is required (one of
emx.e9.contracts.STAGES): it goes on the `created` event, so pod_down.py charges the GPU with it,
and the budget gate checks that stage's limit (emx.spend stage limits). A real launch needs
`--reservation ID --budget-usd B` (an E9 RunPod reservation admitted by emx.e9.budget
.reserve_launch), whose note stage (`E9:<stage>:...`) must equal --stage. The GPU fallback order is
the prereg's (§8.7, contracts.GPU_ORDER): H100 secure -> H100 community -> H100 NVL -> A100-SXM4
80GB; `--start` indexes it. The pod's reservation deadline is its own budget deadline
(t0 + B / rate), never capped at the global kill line (§7.3: the backstop is never the armed
deadline; the laptop watchdog enforces it).

2026-10-08 (EQ0 implementation plan, pod tooling unit; additive): --exp EQ0 names its one pod
eq0-a (slot _A only; emx.eq0.contracts.SLOT), logs to logs/pods_EQ0.jsonl, gates on CAPS["EQ0"]
and needs a live EQ0 watchdog (logs/watchdog_EQ0.heartbeat, scripts/eq0_watchdog.sh). A launch is
refused (exit 4) until logs/approvals/EQ0.approved (contracts.APPROVAL) exists with a line starting
with a UTC timestamp, as I9's sessions; --dry-run reports it and continues. A real launch needs
`--reservation ID --budget-usd B`, as E3's. The GPU fallback order is contracts.FALLBACK_GPUS at its
rates: H100 secure -> H100 NVL (secure) -> H100 PCIe (community) -> A100-SXM4 80GB (secure); the
container disk defaults to contracts.VOLUME_GB.

Usage: uv run python scripts/pod_up.py [--exp I1a|E1b] --slot _A [--start N] [--no-fallback]
       [--dry-run]
       uv run python scripts/pod_up.py --exp I9 --slot _A|_B [--start N] [--no-fallback] [--dry-run]
       uv run python scripts/pod_up.py --exp E9 --slot _A --stage STAGE --reservation ID
           --budget-usd B [--start N] [--gpu TYPE] [--dry-run]
       uv run python scripts/pod_up.py --exp EQ0 --slot _A --reservation ID --budget-usd B
           [--start N] [--gpu TYPE] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emx import runpod as rp

GATE_REFUSED = 4  # exit code: the budget gate refused (pod_hunt.sh stops instead of retrying)
UNTRACKED = 5  # exit code: an untracked pod with our prefix exists, or no live I1a watchdog
UNCERTAIN = 6  # exit code (--reservation): a pod may bill under it; keep the reservation


def gate(
    opt: rp.GpuOption,
    reserve_hours: float,
    dry_run: bool,
    budget_usd: float | None = None,
    stage: str | None = None,
) -> None:
    """Refuse a launch whose next `reserve_hours` (or, given, its whole `budget_usd`) would pass
    a cap (exits non-zero)."""
    running = rp.running_projection(rp.pod_sessions(rp.read_pod_events()), reserve_hours)
    projected = reserve_hours * opt.rate_usd_h if budget_usd is None else budget_usd
    what = (
        f"{reserve_hours} h x ${opt.rate_usd_h}/h"
        if budget_usd is None
        else f"budget (reservation) at ${opt.rate_usd_h}/h"
    )
    print(
        f"budget gate ({rp.EXP}): this pod {what} = ${projected:.2f}; "
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
        if stage is None:
            rp.ledger_check(projected, running)
        else:  # E9: the stage's limit too (emx.spend stage limits)
            rp.ledger_check(projected, running, stage=stage)
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


# I9 (2026-10-04): slot = session, and the paid step whose approval file the launch needs
I9_SESSIONS = {"_A": "P1", "_B": "P3"}
# indices into rp.GPU_FALLBACK: H100 community, H100 secure, H100 NVL (no A100 for I9)
I9_FALLBACK = (0, 2, 1)


def i9_gpu_options(start: int = 0) -> tuple[rp.GpuOption, ...]:
    """I9's fallback order from position `start` (0 = H100 community)."""
    return tuple(rp.GPU_FALLBACK[i] for i in I9_FALLBACK)[start:]


def i9_session_gate(slot: str, dry_run: bool) -> None:
    """Refuse an I9 launch in a slot that is not a session, or whose step is not approved."""
    step = I9_SESSIONS.get(slot)
    if step is None:
        print(
            f"REFUSED: I9 slots are _A (session A, P1) and _B (session B, P3), not {slot}",
            file=sys.stderr,
        )
        sys.exit(2)
    path = rp.approval_path(step)
    ok = rp.approved(step)
    print(f"approval (I9 session {slot[1:]}, {step}): {'present' if ok else 'MISSING'} ({path})")
    if not ok and not dry_run:
        print(
            f"REFUSED: {path} missing or without a UTC line; the main session writes it after "
            f"Jacob's go-ahead for {step}",
            file=sys.stderr,
        )
        sys.exit(GATE_REFUSED)


def e9_gpu_options(start: int = 0) -> tuple[rp.GpuOption, ...]:
    """E9's fallback order (prereg §8.7, emx.e9.contracts.GPU_ORDER) from position `start`
    (0 = H100 secure)."""
    from emx.e9 import contracts as e9c

    return tuple(rp.GpuOption(g, cloud, rate) for g, cloud, rate in e9c.GPU_ORDER)[start:]


def e9_check_stage(stage: str | None, rid: str | None, dry_run: bool) -> str:
    """E9: --stage is required and known; a reservation's note stage must match it. Exits 2."""
    from emx.e9 import contracts as e9c

    if stage not in e9c.STAGES:
        print(f"REFUSED: --exp E9 needs --stage, one of {', '.join(e9c.STAGES)} (got {stage!r})",
              file=sys.stderr)
        sys.exit(2)
    if rid is not None and not dry_run:
        from emx import runpod_reservations as rr

        R = rr.get(rid)
        noted = rp.stage_from_note((R or {}).get("note", ""))
        if R is not None and noted != stage:
            print(
                f"REFUSED: reservation {rid} is for stage {noted!r} (note {R.get('note')!r}), "
                f"not --stage {stage}",
                file=sys.stderr,
            )
            sys.exit(GATE_REFUSED)
    return stage


# EQ0 (2026-10-08): the cloud for each of emx.eq0.contracts.FALLBACK_GPUS (which names GPU types
# and rates only). H100 HBM3 at $3.49 is the secure-cloud rate; "H100 PCIe" is RunPod's community
# H100 (emx.e3.contracts, whose list EQ0's copies); NVL and A100 on the secure cloud, as E9's order.
EQ0_CLOUDS = {
    "NVIDIA H100 80GB HBM3": "SECURE",
    "NVIDIA H100 NVL": "SECURE",
    "NVIDIA H100 PCIe": "COMMUNITY",
    "NVIDIA A100-SXM4-80GB": "SECURE",
}


def eq0_gpu_options(start: int = 0) -> tuple[rp.GpuOption, ...]:
    """EQ0's fallback order (emx.eq0.contracts.FALLBACK_GPUS) from position `start`
    (0 = H100 secure)."""
    from emx.eq0 import contracts as eq0c

    return tuple(rp.GpuOption(g, EQ0_CLOUDS[g], rate) for g, rate in eq0c.FALLBACK_GPUS)[start:]


def eq0_approval_path() -> Path:
    """logs/approvals/EQ0.approved (emx.eq0.contracts.APPROVAL), under emx.runpod.LOGS."""
    from emx.eq0 import contracts as eq0c

    return rp.LOGS / Path(eq0c.APPROVAL).relative_to("logs")


def eq0_gate(slot: str, dry_run: bool) -> None:
    """Refuse an EQ0 launch in any slot but _A (exit 2), or before its approval (exit 4)."""
    from emx.eq0 import contracts as eq0c

    if slot != eq0c.SLOT:
        print(f"REFUSED: EQ0 has one pod, slot {eq0c.SLOT}, not {slot}", file=sys.stderr)
        sys.exit(2)
    path = eq0_approval_path()
    try:
        ok = bool(rp.UTC_LINE_RE.search(path.read_text()))
    except OSError:
        ok = False
    print(f"approval (EQ0): {'present' if ok else 'MISSING'} ({path})")
    if not ok and not dry_run:
        print(
            f"REFUSED: {path} missing or without a UTC line; the main session writes it after "
            "Jacob's go-ahead for EQ0's paid run",
            file=sys.stderr,
        )
        sys.exit(GATE_REFUSED)


def gpu_filter(options: tuple[rp.GpuOption, ...], gpu: str) -> tuple[rp.GpuOption, ...]:
    """The options for GPU type `gpu` (case-insensitive), in order. For E3, a type listed only in
    emx.e3.contracts.FALLBACK_GPUS (e.g. "NVIDIA H100 PCIe") is tried on the community, then the
    secure cloud, at that list's rate (the API's costPerHr, when higher, still sizes the
    deadline)."""
    want = gpu.strip().lower()
    out = tuple(o for o in options if o.gpu_type_id.lower() == want)
    if out or rp.EXP != "E3":
        return out
    from emx.e3 import contracts as e3c

    for name, rate in e3c.FALLBACK_GPUS:
        if name.lower() == want:
            return (rp.GpuOption(name, "COMMUNITY", rate), rp.GpuOption(name, "SECURE", rate))
    return ()


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


RECONCILE_TRIES = 3
RECONCILE_WAIT_S = 20


def find_named_pods(client: rp.RunPod, name: str) -> list[dict]:
    """API pods named `name` that this experiment's event log does not track (raises on a
    failed listing)."""
    known = rp.tracked_pod_ids(rp.read_pod_events())
    return [p for p in client.list_pods() if p.get("name") == name and p.get("id") not in known]


def reconcile_create(
    client: rp.RunPod, name: str, tries: int = RECONCILE_TRIES, wait_s: float = RECONCILE_WAIT_S
) -> dict | None:
    """After a lost or failed create: the pod named `name` if one exists, else None once
    `tries` listings (each after `wait_s`) found nothing. A failed listing raises RunPodError."""
    for _ in range(tries):
        if wait_s:
            time.sleep(wait_s)
        found = find_named_pods(client, name)
        if found:
            if len(found) > 1:
                print(
                    f"WARNING: {len(found)} pods named {name}; adopting {found[0]['id']}, the "
                    "others are untracked (pod_down.py --untracked reaps them)",
                    file=sys.stderr,
                )
            return found[0]
    return None


def close_and_settle(rid: str | None, pod_id: str, note: str) -> bool:
    """After a DELETE: the GPU charge, then (with `rid`) the reservation's settlement. E9 goes
    through `emx.runpod.e9_close` (emx.e9.budget.settle_pod under the admission lock, review R3
    #5); the others through finish_session and settle_reservation, as before."""
    if rp.EXP == "E9":
        r = rp.e9_close(pod_id, note, rid)
        if not r["settled"] and rid is not None:
            print(f"pod {pod_id}: reservation {rid} NOT settled ({r['why']}); its liability "
                  "still counts; pod_down.py --exp E9 retries it", file=sys.stderr)
        return bool(r["settled"])
    s_ = rp.finish_session(pod_id, note)
    return settle_reservation(rid, pod_id, s_) if rid is not None else True


def settle_reservation(rid: str, pod_id: str, session: dict | None) -> bool:
    """After termination and the GPU charge: settle the pod's actual dollars, then release the
    reservation's remainder. False (liability kept) when the dollars are unknown."""
    from emx import runpod_reservations as rr

    if session is None:
        print(
            f"pod {pod_id}: no session in the event log, so its dollars are unknown; reservation "
            f"{rid} is NOT settled (its liability still counts); settle it by hand",
            file=sys.stderr,
        )
        return False
    rr.settle(rid, session["usd"], pod_id=pod_id, note=f"pod_down: {pod_id}")
    rr.release(rid, note=f"unused after {pod_id}")
    print(f"reservation {rid}: settled ${session['usd']:.2f} for {pod_id}; remainder released")
    return True


def reprice_reservation(pod_id: str, rate_usd_h: float) -> None:
    """A higher API rate than the launch used: shorten the reservation deadline (review A r1
    finding 7). Raises on a refused reprice, so the caller terminates the pod."""
    from emx import runpod_reservations as rr

    row = rr.reprice(pod_id, rate_usd_h, note="pod_up: API costPerHr while polling")
    if row is not None:
        print(
            f"pod {pod_id}: API rate ${rate_usd_h}/h > launch rate "
            f"${row['previous_rate_usd_h']}/h; reservation deadline shortened to "
            f"{row['deadline_epoch']} ({row['deadline_utc']})"
        )


def wait_for_ssh(
    client: rp.RunPod,
    pod_id: str,
    timeout_s: int,
    poll_s: int = 15,
    reservation: str | None = None,
):
    """Poll until a public IP and port 22 mapping exist. Returns (host, port) or (None, None).
    With `reservation`, every API rate higher than the last one seen is logged and reprices the
    reservation (without, the first rate is logged, as before)."""
    deadline = time.time() + timeout_s
    last_rate = None
    while time.time() < deadline:
        time.sleep(poll_s)
        info = client.get_pod(pod_id) or {}
        api_rate = float(info.get("costPerHr") or 0.0)
        if api_rate and (last_rate is None or (reservation and api_rate > last_rate)):
            rp.log_pod_event({"event": "rate", "pod_id": pod_id, "rate_usd_h": api_rate})
            last_rate = api_rate
            if reservation:
                reprice_reservation(pod_id, api_rate)
        ip = info.get("publicIp") or ""
        mapping = info.get("portMappings") or {}
        if ip and mapping.get("22"):
            return ip, int(mapping["22"])
        print(f"  waiting... status={info.get('desiredStatus')} ip={ip or '-'}")
    return None, None


def create_status(status: str, rid: str, pod_id: str | None = None) -> None:
    """The one `EMX_POD_CREATE=` line of a reserved launch (see the module docstring)."""
    print(f"EMX_POD_CREATE={status} reservation={rid}" + (f" pod={pod_id}" if pod_id else ""))


NO_STOCK = "There are no instances currently available"


def main(argv: list[str] | None = None) -> int:
    """Reserved launches always end with an `EMX_POD_CREATE=` line: a refusal (SystemExit) or an
    unexpected error that the body did not report is reported here, as `uncertain` (exit 6) once
    a create request may have made a pod, else `none`."""
    ctx: dict = {"rid": None, "live": False, "reported": False}
    try:
        return _main(argv, ctx)
    except SystemExit:
        if ctx["rid"] and not ctx["reported"]:
            if ctx["live"]:
                create_status("uncertain", ctx["rid"])
                sys.exit(UNCERTAIN)
            create_status("none", ctx["rid"])
        raise
    except Exception:
        if not ctx["rid"] or ctx["reported"]:
            raise
        traceback.print_exc()
        create_status("uncertain" if ctx["live"] else "none", ctx["rid"])
        return UNCERTAIN if ctx["live"] else 1


def _main(argv: list[str] | None, ctx: dict) -> int:
    def report(status: str, rc: int, pod_id: str | None = None) -> int:
        create_status(status, rid, pod_id)
        ctx["reported"] = True
        return rc

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
    ap.add_argument(
        "--gpu",
        help="only this GPU type (a gpu_type_id of the fallback order, or for E3 one of "
        "emx.e3.contracts.FALLBACK_GPUS), in fallback order across clouds",
    )
    ap.add_argument("--reservation", help="admitted emx.runpod_reservations id (E3: required)")
    ap.add_argument(
        "--budget-usd", type=float, help="this pod's dollar budget (with --reservation)"
    )
    ap.add_argument(
        "--stage", help="E9 (required): the stage tag this pod's GPU charge is recorded under"
    )
    ap.add_argument(
        "--disk-gb", type=int, default=None,
        help="container disk (default 200; E9: emx.e9.contracts.VOLUME_GB)",
    )
    ap.add_argument("--ssh-timeout", type=int, default=900)
    ap.add_argument("--dry-run", action="store_true", help="print the requests; send nothing")
    args = ap.parse_args(argv)
    rp.configure(args.exp)
    slot = rp.check_slot(args.slot)
    if rp.EXP == "I9":
        i9_session_gate(slot, args.dry_run)
    if rp.EXP == "EQ0":
        eq0_gate(slot, args.dry_run)
    rid, budget = args.reservation, args.budget_usd
    if (rid is None) != (budget is None):
        ap.error("--reservation and --budget-usd go together")
    if rp.EXP == "E3" and rid is None and not args.dry_run:
        ap.error("--exp E3 needs --reservation ID --budget-usd B (emx.runpod_reservations)")
    if rp.EXP == "EQ0" and rid is None and not args.dry_run:
        ap.error("--exp EQ0 needs --reservation ID --budget-usd B (emx.runpod_reservations)")
    stage = None
    if rp.EXP == "E9":
        if rid is None and not args.dry_run:
            ap.error("--exp E9 needs --reservation ID --budget-usd B (emx.e9.budget.reserve_launch)")
        stage = e9_check_stage(args.stage, rid, args.dry_run)
        print(f"stage (E9): {stage}")
        if args.disk_gb is None:
            from emx.e9 import contracts as e9c

            args.disk_gb = e9c.VOLUME_GB
    elif args.stage is not None:
        ap.error("--stage is E9-only")
    if rp.EXP == "EQ0" and args.disk_gb is None:
        from emx.eq0 import contracts as eq0c

        args.disk_gb = eq0c.VOLUME_GB
    if args.disk_gb is None:
        args.disk_gb = 200
    if rid is not None and not budget > 0:
        ap.error("--budget-usd must be > 0")
    if rid is not None and args.dry_run:
        print(f"DRY RUN: would write a `creating` row, then launch reservation {rid} (${budget})")
    elif rid is not None:
        from emx import runpod_reservations as rr

        R = rr.get(rid)
        why = (
            "unknown"
            if R is None
            else f"belongs to {R['exp']}, not {rp.EXP}"
            if R["exp"] != rp.EXP
            else "closed or already launched"
            if R["closed"] or R["launched"]
            else f"holds ${R['open_usd']:.2f} < budget ${budget:.2f}"
            if budget > R["open_usd"] + 1e-6
            else ""
        )
        if why:
            print(f"REFUSED: reservation {rid} {why}", file=sys.stderr)
            sys.exit(GATE_REFUSED)
        ctx["rid"] = rid

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
    if rp.EXP == "I9":
        options = i9_gpu_options(args.start)
    if rp.EXP == "E9":
        options = e9_gpu_options(args.start)
    if rp.EXP == "EQ0":
        options = eq0_gpu_options(args.start)
    if args.gpu:
        options = gpu_filter(options, args.gpu)
        if not options:
            ap.error(f"--gpu {args.gpu!r}: not a GPU type of {rp.EXP}'s fallback order")
    if args.no_fallback:
        options = options[:1]
    pod = opt = None
    t0 = None
    reserved = rid is not None and not args.dry_run
    for opt in options:
        gate(opt, args.reserve_hours, args.dry_run, budget, stage)
        body = rp.pod_body(opt, slot, public_key, container_disk_gb=args.disk_gb)
        if not args.dry_run:
            rp.log_create_pending(body["name"], slot)
        if reserved:
            t0 = time.time()  # billed time starts here: delays come out of the budget
            rr.creating(
                rid,
                slot=slot,
                name=body["name"],
                budget_usd=budget,
                rate_usd_h=opt.rate_usd_h,
                t0_epoch=t0,
            )
            ctx["live"] = True  # from here a pod may exist until reconciled
        try:
            pod = client.create_pod(body)
        except rp.RunPodError as e:
            if not reserved:
                if isinstance(e, rp.CreateUncertain):
                    return recover_uncertain_create(client, e)
                print(f"create failed on {opt.gpu_type_id} ({opt.cloud}): {e}", file=sys.stderr)
                continue
            # RunPod answers a stock-out with HTTP 500 "There are no instances currently
            # available": a definite refusal, not an unknown outcome, so try the next GPU option
            # (one reconciliation still runs first; E3, 2026-10-06).
            uncertain = isinstance(e, rp.CreateUncertain) and NO_STOCK not in str(e)
            print(f"create {'outcome UNKNOWN' if uncertain else 'failed'} ({e}); reconciling")
            try:
                pod = reconcile_create(
                    client,
                    body["name"],
                    RECONCILE_TRIES if uncertain else 1,
                    RECONCILE_WAIT_S if uncertain else 0,
                )
            except rp.RunPodError as le:
                print(
                    f"pod list FAILED ({le}); reservation {rid} KEPT (its liability counts); "
                    "CHECK THE RUNPOD CONSOLE, then pod_down.py --untracked or release it",
                    file=sys.stderr,
                )
                return report("uncertain", UNCERTAIN)
            if pod is not None:
                print(f"adopting pod {pod['id']} named {body['name']} into reservation {rid}")
                break
            ctx["live"] = False  # reconciled: no pod of this request
            if uncertain:
                rr.release(rid, note=f"pod_up: create outcome unknown, no {body['name']} pod")
                print(f"no {body['name']} pod exists; reservation {rid} released", file=sys.stderr)
                return report("closed", 1)
            continue
        if args.dry_run:
            continue  # print every option in order
        break
    if args.dry_run:
        return 0
    if not pod:
        if reserved:
            rr.release(rid, note="pod_up: no GPU in the fallback order could be created")
            print(f"reservation {rid} released (no pod was created)", file=sys.stderr)
        print("no GPU in the fallback order could be created; try pod_hunt.sh", file=sys.stderr)
        return report("closed", 1) if reserved else 1

    pod_id = pod["id"]
    api_rate = pod.get("costPerHr")
    rate = float(api_rate) if api_rate else opt.rate_usd_h
    created = {
        "event": "created",
        "pod_id": pod_id,
        "slot": slot,
        "gpu": opt.gpu_type_id,
        "cloud": opt.cloud,
        "image": rp.IMAGE,
        "rate_usd_h": rate,
        "rate_source": "api" if api_rate else "estimate",
    }
    if reserved:  # bill from t0 (before the request), and size the deadline conservatively
        created["utc"] = datetime.fromtimestamp(t0, UTC).isoformat()
        created["reservation_id"] = rid
        rate = max(rate, opt.rate_usd_h)
    if stage is not None:  # E9: pod_down.py charges the GPU with it
        created["stage"] = stage
    # persist the id BEFORE waiting for SSH so a timeout never leaks an untracked pod
    rp.set_env_var(f"RUNPOD_POD_ID{slot}", pod_id)
    rp.log_pod_event(created)
    print(f"pod created: {pod_id} ({opt.gpu_type_id}, {opt.cloud}, ${rate}/h)")
    if reserved:
        try:
            # E9: the pod's own budget deadline only, never the global line (prereg §7.3)
            g = None if rp.EXP == "E9" else rp.killswitch_deadline(pod_id)
        except Exception as e:  # noqa: BLE001 - the budget deadline still bounds the pod
            print(f"WARNING: global kill-line deadline unavailable ({e})", file=sys.stderr)
            g = None
        try:
            dl = rr.launch(rid, [(pod_id, rate, budget)], t0, global_deadline=g, slot=slot)
        except Exception as e:  # noqa: BLE001 - an unlaunched pod must not keep billing
            print(f"launch row REFUSED ({e}); terminating {pod_id}", file=sys.stderr)
            try:
                client.terminate_pod(pod_id)
            except rp.RunPodError as te:
                print(f"DELETE FAILED ({te}); terminate {pod_id} by hand", file=sys.stderr)
                return report("uncertain", UNCERTAIN, pod_id)
            settled = close_and_settle(rid, pod_id, "pod_up: launch row refused")
            rp.set_env_var(f"RUNPOD_POD_ID{slot}", "")
            return (
                report("closed", 1, pod_id) if settled else report("uncertain", UNCERTAIN, pod_id)
            )
        print(f"reservation deadline {dl[pod_id]} (EMX_POD_DEADLINE follows once SSH is up)")
    if rp.EXP == "I9":  # Docker Hub rate limits (2026-10-04): RUNNING is not "image pulled"
        print("I9: check the pod's status; an unauthenticated image pull may be rate-limited")

    host = port = None
    poll_error = None
    try:
        host, port = wait_for_ssh(
            client, pod_id, args.ssh_timeout, reservation=rid if reserved else None
        )
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
            return report("uncertain", UNCERTAIN, pod_id) if reserved else 1
        settled = close_and_settle(rid if reserved else None, pod_id, f"pod_up: {reason}")
        settled = reserved and settled
        rp.set_env_var(f"RUNPOD_POD_ID{slot}", "")
        print(f"{reason}; pod {pod_id} terminated", file=sys.stderr)
        if reserved:
            return report("closed" if settled else "uncertain", 1 if settled else UNCERTAIN, pod_id)
        return 1

    rp.set_env_var(f"POD_SSH_HOST{slot}", host)
    rp.set_env_var(f"POD_SSH_PORT{slot}", str(port))
    rp.forget_host_key(host, port)  # a stale key from an earlier pod on this ip:port
    rp.log_pod_event(
        {"event": "running", "pod_id": pod_id, "slot": slot, "host": host, "port": port}
    )
    print(f"SSH ready: ssh -p {port} root@{host}")
    if reserved:
        print(f"EMX_POD_DEADLINE={rr.pod_deadline(pod_id)}")
        return report("ok", 0, pod_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
