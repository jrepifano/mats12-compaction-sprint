"""E13's cumulative pod-hour cap (prereg Amendment B: 11 pod-hours over every E13 pod).

Implementation-plan §8, review finding 8: for E13 the pod-side killswitch is armed at the EARLIER
of the dollar deadline (`python -m emx.runpod --killswitch-deadline`, the GPU kill line) and
`now + remaining pod-hours`, where remaining = `contracts.POD_HOURS_CAP` minus every E13 session
in `logs/pods_E13.jsonl` (closed sessions at their recorded length, open ones from creation to
now). `scripts/pod_push.sh` (E13 branch) calls this module for the deadline,
`scripts/e13_watchdog.sh` for its extra pod-hour stop, and the orchestrator
(`scripts/e13_orchestrate.py`) may import it for its own pod-hour check.

The session rules follow `emx.runpod.pod_sessions` (billed from the `created` event, which for a
reserved launch is stamped at t0, before the create request; ended by the first `terminated`
event), re-implemented here stdlib-only so laptop tools import it cheaply, and made conservative
(over-count) where the event log is ambiguous:
- each pod id is a small state machine over its events in time order: `created` opens a session
  (a second `created` while open is ignored, so the EARLIER start is kept), `terminated` closes
  it (a second `terminated` is ignored, so the EARLIER end of a closed session is kept, as
  `pod_sessions` does), and a `created` after a close opens a new session (counted again);
- a pod whose first event is not `created` (e.g. a `running` or `terminated` line without its
  creation) is counted from its earliest event;
- a session still open is counted up to `now`;
- several open sessions burn pod-hours together, so the remaining hours are split between them
  when the deadline is computed;
- a malformed line (bad JSON, a missing or unparseable `utc`) raises: callers fail closed.

Stdlib only. CLI (exit 0 ok; 3 no pod-hours left; 2 usage; 1 unreadable log):
  python -m emx.e13.podhours [--dollar-deadline EPOCH] [--now EPOCH] [--events PATH]
      prints EMX_E13_PODHOURS_USED=<h>, EMX_E13_PODHOURS_REMAINING=<h>, EMX_E13_PODHOURS_OPEN=<n>,
      EMX_E13_PODHOURS_DEADLINE=<epoch> and EMX_KILLSWITCH_DEADLINE=<epoch> (the earlier of the
      two when --dollar-deadline is given, else the pod-hour deadline) and
      EMX_E13_DEADLINE_SOURCE=pod_hours|dollars
  python -m emx.e13.podhours --status [--reserve-hours H] [--now EPOCH] [--events PATH]
      one JSON line; exit 2 (hard stop) when used + H x open sessions >= the cap
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from emx.e13 import contracts as C


class NoPodHoursLeft(RuntimeError):
    """E13's cumulative pod-hours have reached `contracts.POD_HOURS_CAP`."""


def events_path() -> Path:
    """logs/pods_E13.jsonl, under EMX_POD_LOG_DIR when set (the same sandbox rule as
    `emx.runpod.LOGS`, for subprocess tests), else this checkout's logs/."""
    logs = Path(os.environ.get("EMX_POD_LOG_DIR") or C.REPO_ROOT / "logs")
    return logs / Path(C.POD_EVENTS).name


def read_events(path: Path | None = None) -> list[dict]:
    """Every event line; a missing file is no events. Malformed JSON raises."""
    path = path or events_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _ts(e: dict) -> datetime:
    t = datetime.fromisoformat(str(e["utc"]))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def sessions(events: list[dict], now: datetime | None = None) -> list[dict]:
    """One row per session: {pod_id, t0, t1 (None while open), open, hours}, by the rules in the
    module docstring. Events without a pod_id are ignored."""
    now = now or datetime.now(UTC)
    by_pod: dict[str, list[tuple[datetime, int, str]]] = {}
    for i, e in enumerate(events):
        pid = e.get("pod_id")
        if not pid:
            continue
        by_pod.setdefault(str(pid), []).append((_ts(e), i, str(e.get("event", ""))))
    out: list[dict] = []
    for pid, evs in by_pod.items():
        evs.sort()  # by time, then file order
        t0: datetime | None = None
        closed_any = False
        for k, (t, _i, kind) in enumerate(evs):
            if kind == "created":
                if t0 is None:
                    t0 = t
            elif kind == "terminated":
                if t0 is None and not closed_any and k == 0:
                    t0 = t  # a termination with no creation before it: a zero-length session
                if t0 is not None:
                    out.append({"pod_id": pid, "t0": t0, "t1": t, "open": False})
                    t0, closed_any = None, True
            elif t0 is None and not closed_any and k == 0:
                t0 = t  # first event is not `created`: count from the earliest event we have
        if t0 is not None:
            out.append({"pod_id": pid, "t0": t0, "t1": None, "open": True})
    for s in out:
        end = s["t1"] or max(now, s["t0"])
        s["hours"] = max(0.0, (end - s["t0"]).total_seconds() / 3600)
    return out


def used_hours(events: list[dict], now: datetime | None = None) -> float:
    return sum(s["hours"] for s in sessions(events, now))


def status(
    events: list[dict],
    now: datetime | None = None,
    reserve_hours: float = 0.0,
    cap: float = C.POD_HOURS_CAP,
) -> dict:
    """The watchdog's view: hard stop once used + `reserve_hours` of every open session's burn
    reaches the cap (one watchdog interval reserved, as pod_budget_check.py reserves dollars)."""
    now = now or datetime.now(UTC)
    ss = sessions(events, now)
    used = float(sum(s["hours"] for s in ss))
    n_open = sum(1 for s in ss if s["open"])
    projected = used + reserve_hours * n_open
    return {
        "exp": C.EXP,
        "pod_hours_cap": cap,
        "used_h": round(used, 4),
        "remaining_h": round(max(0.0, cap - used), 4),
        "open_sessions": n_open,
        "reserve_hours": reserve_hours,
        "projected_h": round(projected, 4),
        "hard_stop": projected >= cap,
        "utc": now.isoformat(),
    }


def deadline(
    events: list[dict],
    now: datetime | None = None,
    dollar_deadline: int | None = None,
    cap: float = C.POD_HOURS_CAP,
) -> dict:
    """{used_h, remaining_h, open_sessions, podhours_deadline, deadline, source}: the pod-hour
    deadline is now + remaining / max(1, open sessions), floored to the second (early, never
    late); `deadline` is the earlier of it and `dollar_deadline` when given. Raises
    NoPodHoursLeft when nothing remains."""
    now = now or datetime.now(UTC)
    ss = sessions(events, now)
    used = float(sum(s["hours"] for s in ss))
    remaining = cap - used
    if remaining <= 0:
        raise NoPodHoursLeft(f"E13 pod-hours used {used:.3f} h >= cap {cap} h")
    n_open = sum(1 for s in ss if s["open"])
    ph = math.floor(now.timestamp() + remaining / max(1, n_open) * 3600)
    if dollar_deadline is not None and int(dollar_deadline) < ph:
        dl, source = int(dollar_deadline), "dollars"
    else:
        dl, source = ph, "pod_hours"
    return {
        "used_h": round(used, 4),
        "remaining_h": round(remaining, 4),
        "open_sessions": n_open,
        "podhours_deadline": ph,
        "deadline": dl,
        "source": source,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m emx.e13.podhours", description=__doc__.split("\n", 1)[0]
    )
    ap.add_argument("--dollar-deadline", type=int, help="the GPU-line deadline (epoch seconds)")
    ap.add_argument("--status", action="store_true", help="one JSON line; exit 2 = hard stop")
    ap.add_argument("--reserve-hours", type=float, default=0.0, help="with --status")
    ap.add_argument("--now", type=float, help="epoch seconds (tests)")
    ap.add_argument("--events", type=Path, help="event log (default logs/pods_E13.jsonl)")
    args = ap.parse_args(argv)
    if args.status and args.dollar_deadline is not None:
        ap.error("--dollar-deadline goes without --status")
    now = datetime.now(UTC)
    if args.now is not None:
        now = datetime.fromtimestamp(args.now, UTC)
    try:
        events = read_events(args.events)
        if args.status:
            st = status(events, now, args.reserve_hours)
            print(json.dumps(st))
            return 2 if st["hard_stop"] else 0
        d = deadline(events, now, args.dollar_deadline)
    except NoPodHoursLeft as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"cannot read the E13 pod event log: {e!r}", file=sys.stderr)
        return 1
    print(f"EMX_E13_PODHOURS_USED={d['used_h']}")
    print(f"EMX_E13_PODHOURS_REMAINING={d['remaining_h']}")
    print(f"EMX_E13_PODHOURS_OPEN={d['open_sessions']}")
    print(f"EMX_E13_PODHOURS_DEADLINE={d['podhours_deadline']}")
    print(f"EMX_KILLSWITCH_DEADLINE={d['deadline']}")
    print(f"EMX_E13_DEADLINE_SOURCE={d['source']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
