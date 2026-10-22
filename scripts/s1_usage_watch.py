"""Max-plan usage guard for the S1 bulk jobs (Jacob, 21 Oct): pause both jobs at 95% of any
usage window, resume after the window refreshes, so the allowance never runs to 100% (his own
Claude sessions share it).

Every 30 s it reads the newest rate-limit snapshot in data/raw/sprint/s1/usage/max_usage.jsonl
(every Max call appends one). At >= PAUSE_AT on the five-hour or weekly window it stops the jobs:
SIGTERM to the job chains (logs/s1_jobs/s1_job_{J,R}.sh, so they don't go on to their next
step), then to their `s1_run.py` drivers, whose job_guard kills every `claude -p` session they
started. While paused it sleeps until the blocking window's reset (+120 s) or POLL_S, whichever
is sooner, then sends one tiny probe call to read fresh utilization (so a manual reset is seen
too). Once every window is below RESUME_BELOW it relaunches both chains, appending to their logs;
every step resumes from the caches. Anchored lines for the monitor: S1_WATCH PAUSE / PROBE /
RESUME / ERROR.

    nohup uv run python scripts/s1_usage_watch.py >> logs/s1_watch.log 2>&1 &
    uv run python scripts/s1_usage_watch.py --once    # print the decision, touch nothing
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USAGE = ROOT / "data" / "raw" / "sprint" / "s1" / "usage" / "max_usage.jsonl"
JOBS = {
    "J": ROOT / "logs" / "s1_jobs" / "s1_job_J.sh",
    "R": ROOT / "logs" / "s1_jobs" / "s1_job_R.sh",
}
PAUSE_AT = 0.95
RESUME_BELOW = 0.90
CHECK_S = 30
POLL_S = 900
MARGIN_S = 120
WINDOWS = ("five_hour", "seven_day")


def say(msg: str) -> None:
    print(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def latest_reading(path: Path = USAGE, tail_bytes: int = 400_000) -> dict | None:
    """{window: (utilization, resetsAt)} from the newest usage row that has a snapshot."""
    if not path.exists():
        return None
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - tail_bytes))
        lines = f.read().decode("utf-8", "replace").splitlines()
    best = None
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        w = (row.get("rate_limit") or {}).get("unifiedWindows") or {}
        if not w:
            continue
        if best is None or row.get("utc", "") >= best[0]:
            best = (row.get("utc", ""), w)
    if best is None:
        return None
    return {
        k: (float(v.get("utilization") or 0.0), float(v.get("resetsAt") or 0.0), best[0])
        for k, v in best[1].items()
        if k in WINDOWS
    }


def pids(pattern: str) -> list[int]:
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
    return [int(x) for x in r.stdout.split() if int(x) != os.getpid()]


def job_pids() -> dict[str, list[int]]:
    return {
        "chains": pids(r"[b]ash logs/s1_jobs/s1_job_"),
        "drivers": pids(r"[s]cripts/s1_run\.py (partA|partB|replays|size)"),
        "sessions": pids(r"[c]laude -p .*--model claude-haiku-5-5"),
    }


def stop_jobs(wait_s: float = 120.0) -> None:
    p = job_pids()
    for pid in p["chains"]:  # first, so a chain can't start its next step
        _kill(pid, signal.SIGTERM)
    for pid in p["drivers"]:
        _kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        left = job_pids()
        if not any(left.values()):
            break
        time.sleep(2)
    left = job_pids()
    if left["drivers"] or left["sessions"]:
        for pid in left["drivers"] + left["sessions"]:
            _kill(pid, signal.SIGKILL)
        say(f"S1_WATCH ERROR stragglers SIGKILLed: {left}")
    say(f"S1_WATCH stopped jobs {p}")


def _kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def start_jobs() -> None:
    for name, script in JOBS.items():
        log = open(ROOT / "logs" / f"s1_{name}.log", "a")  # noqa: SIM115 - handed to the child
        subprocess.Popen(
            ["caffeinate", "-is", "bash", str(script.relative_to(ROOT))],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )  # fmt: skip
        if name == "J":
            time.sleep(2)


def probe() -> None:
    """One tiny Max call, sent directly (not through MaxBackend.run, whose limit handling would
    poll): the usual policy and auth checks, a scratch cache, no ledger row. Its usage row in
    the usage log carries a fresh rate-limit snapshot; a paid route raises as on any call."""
    sys.path.insert(0, str(ROOT / "src"))
    from compaction import claude_headless as ch

    try:
        ch.check_managed_settings()
        ch.check_auth()
        be = ch.MaxBackend(
            cache_dir=ROOT / "data" / "raw" / "sprint" / "s1" / "usage" / "probe_cache",
            concurrency=1,
            recorder=lambda *a, **k: None,
        )
        resp, limit = be._attempt(ch._canary_req(), "usage_probe", ch._Stats(), 0, False)
        say(f"S1_WATCH probe error={resp.error} limit={limit}")
    except Exception as e:  # noqa: BLE001 - a failed probe just means "still paused"
        say(f"S1_WATCH ERROR probe failed: {e!r}"[:500])


def worst(reading: dict) -> tuple[str, float, float]:
    w = max(reading, key=lambda k: reading[k][0])
    return w, reading[w][0], reading[w][1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="print the decision only")
    a = ap.parse_args(argv)
    r = latest_reading()
    if a.once:
        print(r, job_pids(), "pause" if r and worst(r)[1] >= PAUSE_AT else "run")
        return 0
    say(f"S1_WATCH start pause_at={PAUSE_AT} resume_below={RESUME_BELOW} reading={r}")
    paused = False
    while True:
        r = latest_reading()
        if not paused:
            if r and worst(r)[1] >= PAUSE_AT:
                w, u, reset = worst(r)
                say(
                    f"S1_WATCH PAUSE {w} util={u} resets={datetime.fromtimestamp(reset, UTC):%Y-%m-%dT%H:%M:%SZ}"
                )
                stop_jobs()
                paused = True
                continue
            time.sleep(CHECK_S)
            continue
        w, u, reset = worst(r)
        if u < RESUME_BELOW:
            say(f"S1_WATCH RESUME {w} util={u}; relaunching J and R")
            start_jobs()
            paused = False
            time.sleep(60)
            continue
        wait = POLL_S if reset <= time.time() else min(POLL_S, reset + MARGIN_S - time.time())
        time.sleep(max(30.0, wait))
        probe()
        r2 = latest_reading()
        say(f"S1_WATCH PROBE {r2}")


if __name__ == "__main__":
    sys.exit(main())
