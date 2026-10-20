"""UTC timestamps: the shim and ask log write microseconds, the push hook whole seconds."""

from __future__ import annotations

import calendar
import time


def utc_now() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1e6):06d}Z"


def parse_ts(ts: str | None) -> float | None:
    """Epoch seconds from 'YYYY-mm-ddTHH:MM:SS[.ffffff]Z'; None if missing or malformed."""
    if not ts:
        return None
    s = ts.rstrip("Z")
    frac = 0.0
    if "." in s:
        s, f = s.split(".", 1)
        try:
            frac = float("0." + f)
        except ValueError:
            return None
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S")) + frac
    except ValueError:
        return None
