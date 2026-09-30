#!/usr/bin/env bash
# Laptop-side GPU hard stop for I1a (implementation plan §2 U1, §5 "The watchdog runs on every pod").
# The loop of scripts/pod_watchdog.sh (itself a port of ../mats12_neel_working/scripts/
# pod_watchdog.sh), re-implemented here for I1a so the E1 script, which has edits in flight,
# stays untouched (2026-09-29). Every call passes --exp I1a explicitly (the ambient environment
# is never trusted). Hardened after gpt-6-sol review U1 r1:
# - each tick first writes a heartbeat (logs/watchdog_I1a.heartbeat: "<epoch> <interval> <pid>");
#   pod_up.py --exp I1a refuses to launch unless it is younger than two intervals and the pid
#   is alive;
# - budget: pod_budget_check.py --exp I1a projects logged GPU $ + running i1a-* pods (billed from
#   creation, logs/pods_I1a.jsonl) + one interval of burn against min($40, $83 - judges) and the
#   combined guard; until logs/approvals/I1a-P1.approved holds a UTC line it also passes
#   --gpu-ceiling <P0 smoke cap, $6>;
# - at a hard stop (exit 2) it terminates EVERY i1a-* pod the API lists (pod_down.py --exp I1a
#   --all-exp) and keeps monitoring, retrying every minute;
# - FAILS CLOSED: any other budget-check exit (malformed ledger, crash) writes an alert
#   (logs/watchdog_I1a.alert) and terminates every i1a-* pod too;
# - reconciles: each healthy tick terminates i1a-* pods missing from the event log
#   (pod_down.py --exp I1a --untracked; a pod pod_up is creating right now is spared briefly).
# Idle GPUs only raise an alert (logs/idle_I1a.alert), never a kill: a 61 GB model download
# looks idle from outside. macOS bash 3.2: no associative arrays, no ${x,,}, no mapfile.
#   nohup bash scripts/i1a_watchdog.sh [INTERVAL_S] > logs/watchdog_I1a.log 2>&1 &
#   touch logs/watchdog_I1a.stop     # when I1a's pod work is over (removes the heartbeat)
# I1A_WATCHDOG_ONCE=1 runs a single tick and exits with the budget check's code (tests).
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL=${1:-600}
HOURS=$(python3 -c "print($INTERVAL/3600)")
STOP=${I1A_WATCHDOG_STOP:-logs/watchdog_I1a.stop}
ALERT=${I1A_IDLE_ALERT:-logs/idle_I1a.alert}
FAIL_ALERT=${I1A_WATCHDOG_ALERT:-logs/watchdog_I1a.alert}
HEARTBEAT=${I1A_WATCHDOG_HEARTBEAT:-logs/watchdog_I1a.heartbeat}
P1_MARKER=${I1A_APPROVALS:-logs/approvals}/I1a-P1.approved
ONCE=${I1A_WATCHDOG_ONCE:-0}
IDLE_TICKS=${IDLE_TICKS:-3}   # consecutive ticks at 0% GPU before an idle alert
export WANDB_MODE=offline
mkdir -p logs
rm -f "$STOP"
P0_CAP=$(python3 -c "import sys; sys.path.insert(0, 'src'); from emx.i1a import contracts as C; print(C.POD_SMOKE_CAP_USD)" 2>/dev/null)
P0_CAP=${P0_CAP:-6.0}   # fail closed to the prereg value if contracts cannot be read
now() { date -u +%FT%TZ; }
envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }
p1_approved() { [ -s "$P1_MARKER" ] && grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$P1_MARKER"; }
kill_all() {  # kill_all REASON
  echo "$(now) HARD STOP ($1): terminating every i1a-* pod"
  uv run python scripts/pod_down.py --exp I1a --all-exp --reason "i1a watchdog: $1" \
    || echo "$(now) pod_down FAILED - retrying next minute; terminate in the console if it persists" | tee -a "$FAIL_ALERT"
}
# per-slot idle counters in plain variables (bash 3.2 has no associative arrays): IDLE__A, ...
idle_get() { eval "echo \${IDLE_$1:-0}"; }
idle_set() { eval "IDLE_$1=$2"; }
idle_check() {
  local slot=$1 host port util n
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] || return 0
  util=$(ssh -p "$port" -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new "root@$host" \
         "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits" 2>/dev/null | head -1 | tr -d ' ')
  [ -n "$util" ] || { echo "$(now) $slot: unreachable for the idle check"; return 0; }
  n=$(idle_get "$slot")
  if [ "$util" = "0" ]; then n=$(( n + 1 )); else n=0; fi
  idle_set "$slot" "$n"
  echo "$(now) $slot: gpu util ${util}% (idle ticks $n)"
  if [ "$n" -ge "$IDLE_TICKS" ]; then
    echo "$(now) IDLE ALERT (I1a): $slot at 0% GPU for $n ticks - billing for nothing; investigate or terminate" | tee -a "$ALERT"
  fi
}
while true; do
  if [ -f "$STOP" ]; then
    rm -f "$HEARTBEAT"
    echo "$(now) stop file present; watchdog exits"; exit 0
  fi
  echo "$(date +%s) $INTERVAL $$" > "$HEARTBEAT"
  CEIL=""
  p1_approved || CEIL="--gpu-ceiling $P0_CAP"
  # stdout = one JSON line (stderr kept separate); projection reserves one interval of burn
  out=$(uv run python scripts/pod_budget_check.py --exp I1a --reserve-hours "$HOURS" $CEIL 2>/dev/null); rc=$?
  echo "$(now) $out"
  if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then
    echo "$(now) BUDGET CHECK FAILED (rc=$rc): failing closed" | tee -a "$FAIL_ALERT"
    kill_all "budget check failed rc=$rc"
    [ "$ONCE" = "1" ] && exit "$rc"
    sleep 60; continue
  fi
  if [ $rc -eq 2 ]; then
    kill_all "budget"
    [ "$ONCE" = "1" ] && exit 2
    sleep 60; continue   # keep monitoring until no pod is running
  fi
  uv run python scripts/pod_down.py --exp I1a --untracked --reason "i1a watchdog reconcile" \
    || echo "$(now) reconcile (pod_down --untracked) FAILED" | tee -a "$FAIL_ALERT"
  slots=$(printf '%s\n' "$out" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))")
  for slot in $slots; do
    idle_check "$slot"
  done
  [ "$ONCE" = "1" ] && exit 0
  sleep "$INTERVAL"
done
