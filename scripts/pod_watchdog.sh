#!/usr/bin/env bash
# Laptop-side GPU hard stop for E1.
# Port of ../mats12_neel_working/scripts/pod_watchdog.sh. Changes: one experiment (E1), no stage
# argument; the projection is scripts/pod_budget_check.py (logged GPU $ + running pods billed from
# creation + one interval of burn) against min($53, $65 - judge spend so far), plus the combined
# guard emx.spend.check_total() (implementation-plan §5); at the limit it terminates EVERY pod the
# API lists as e1-* (pod_down.py --all-e1) and keeps monitoring, retrying a failed termination
# every minute. Idle GPUs only raise an alert (logs/idle_E1.alert), never a kill: a model download
# looks idle from outside. macOS bash 3.2: no associative arrays, no ${x,,}, no mapfile.
#   nohup bash scripts/pod_watchdog.sh [INTERVAL_S] > logs/watchdog_E1.log 2>&1 &
#   touch logs/watchdog_E1.stop     # when E1's pod work is over
#
# FAIL CLOSED (code review 20260929-E1-code-r1, BLOCKING): a budget check that errors (any exit
# code other than 0 or 2, or output that is not the expected JSON line) is retried every RETRY_S.
# After FAIL_MAX_ERRORS consecutive errors, or FAIL_MAX_S seconds since the first error of the
# streak, whichever comes first (defaults: 3 errors / 1800 s; with RETRY_S=60 the error count
# trips first, about 2-3 minutes after the first error), the watchdog cannot see the spend, so it
# terminates every e1-* pod (pod_down.py --all-e1) and appends the reason and UTC time to
# logs/watchdog_E1.failclosed. It then KEEPS RUNNING: while the check still errors it repeats
# pod_down --all-e1 every RETRY_S (so a pod that reappears is terminated too, and a failed
# termination is retried); once a check succeeds again it logs the recovery and resumes normal
# monitoring. The .failclosed file stays as the record until a human removes it (pod_part.sh
# refuses to start a part while it exists). Idleness never triggers a kill.
# Overrides (tests and debugging): BUDGET_CHECK_CMD, POD_DOWN_CMD, WATCHDOG_DIR (default logs),
# RETRY_S, FAIL_MAX_ERRORS, FAIL_MAX_S.
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL=${1:-600}
HOURS=$(python3 -c "print($INTERVAL/3600)")
WD_DIR=${WATCHDOG_DIR:-logs}
STOP=$WD_DIR/watchdog_E1.stop
FAILCLOSED=$WD_DIR/watchdog_E1.failclosed
IDLE_TICKS=${IDLE_TICKS:-3}   # consecutive ticks at 0% GPU before an idle alert
RETRY_S=${RETRY_S:-60}                   # wait between retries of a failed check / termination
FAIL_MAX_ERRORS=${FAIL_MAX_ERRORS:-3}    # consecutive check errors before failing closed
FAIL_MAX_S=${FAIL_MAX_S:-1800}           # or seconds since the streak's first error
BUDGET_CHECK_CMD=${BUDGET_CHECK_CMD:-uv run python scripts/pod_budget_check.py}
POD_DOWN_CMD=${POD_DOWN_CMD:-uv run python scripts/pod_down.py}
export WANDB_MODE=offline
mkdir -p "$WD_DIR"
rm -f "$STOP"
ERRS=0; FIRST_ERR=0; CLOSED=0
# check_error REASON: count one budget-check error; fail closed once the bound is reached
check_error() {
  local now elapsed
  now=$(date +%s)
  [ "$ERRS" -eq 0 ] && FIRST_ERR=$now
  ERRS=$(( ERRS + 1 )); elapsed=$(( now - FIRST_ERR ))
  echo "$(date -u +%FT%TZ) budget check error $ERRS/$FAIL_MAX_ERRORS (${elapsed}s/${FAIL_MAX_S}s): $1"
  if [ "$ERRS" -ge "$FAIL_MAX_ERRORS" ] || [ "$elapsed" -ge "$FAIL_MAX_S" ]; then
    if [ "$CLOSED" -eq 0 ]; then
      echo "$(date -u +%FT%TZ) FAIL CLOSED: $ERRS consecutive budget check errors over ${elapsed}s (last: $1); terminating every e1-* pod" | tee -a "$FAILCLOSED"
      CLOSED=1
    else
      echo "$(date -u +%FT%TZ) still failed closed: re-running pod_down --all-e1"
    fi
    $POD_DOWN_CMD --all-e1 --reason "watchdog fail-closed: budget check errors" \
      || echo "$(date -u +%FT%TZ) pod_down FAILED - retrying in ${RETRY_S}s; terminate in the console if it persists" | tee -a "$FAILCLOSED"
  else
    echo "$(date -u +%FT%TZ) retrying in ${RETRY_S}s"
  fi
}
envval() { { grep -m1 "^$1=" .env || true; } | cut -d= -f2- ; }  # a missing key is empty, not fatal under pipefail
# per-slot idle counters in plain variables (bash 3.2 has no associative arrays): IDLE__A, ...
idle_get() { eval "echo \${IDLE_$1:-0}"; }
idle_set() { eval "IDLE_$1=$2"; }
idle_check() {
  local slot=$1 host port util n
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] || return 0
  util=$(ssh -p "$port" -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new "root@$host" \
         "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits" 2>/dev/null | head -1 | tr -d ' ')
  [ -n "$util" ] || { echo "$(date -u +%FT%TZ) $slot: unreachable for the idle check"; return 0; }
  n=$(idle_get "$slot")
  if [ "$util" = "0" ]; then n=$(( n + 1 )); else n=0; fi
  idle_set "$slot" "$n"
  echo "$(date -u +%FT%TZ) $slot: gpu util ${util}% (idle ticks $n)"
  if [ "$n" -ge "$IDLE_TICKS" ]; then
    echo "$(date -u +%FT%TZ) IDLE ALERT: $slot at 0% GPU for $n ticks - billing for nothing; investigate or terminate" | tee -a "$WD_DIR/idle_E1.alert"
  fi
}
while true; do
  [ -f "$STOP" ] && { echo "$(date -u +%FT%TZ) stop file present; watchdog exits"; exit 0; }
  # stdout = one JSON line (stderr kept separate); projection reserves one interval of burn
  out=$($BUDGET_CHECK_CMD --reserve-hours "$HOURS" 2>/dev/null); rc=$?
  echo "$(date -u +%FT%TZ) $out"
  if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then check_error "rc=$rc"; sleep "$RETRY_S"; continue; fi
  if [ $rc -eq 2 ]; then
    ERRS=0
    echo "$(date -u +%FT%TZ) HARD STOP: terminating every e1-* pod"
    $POD_DOWN_CMD --all-e1 --reason "watchdog hard stop" \
      || echo "$(date -u +%FT%TZ) pod_down FAILED - retrying in ${RETRY_S}s; terminate in the console if it persists"
    sleep "$RETRY_S"; continue   # keep monitoring until no pod is running
  fi
  if ! slots=$(printf '%s\n' "$out" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))" 2>/dev/null); then
    check_error "rc=0 but the output is not the expected JSON line"; sleep "$RETRY_S"; continue
  fi
  if [ "$ERRS" -gt 0 ]; then
    echo "$(date -u +%FT%TZ) budget check recovered after $ERRS error(s)$( [ "$CLOSED" -eq 1 ] && echo "; was failed closed (see $FAILCLOSED)"); resuming normal monitoring"
  fi
  ERRS=0; CLOSED=0
  for slot in $slots; do
    idle_check "$slot"
  done
  sleep "$INTERVAL"
done
