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
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL=${1:-600}
HOURS=$(python3 -c "print($INTERVAL/3600)")
STOP=logs/watchdog_E1.stop
IDLE_TICKS=${IDLE_TICKS:-3}   # consecutive ticks at 0% GPU before an idle alert
export WANDB_MODE=offline
mkdir -p logs
rm -f "$STOP"
envval() { grep -m1 "^$1=" .env | cut -d= -f2- ; }
# per-slot idle counters in plain variables (bash 3.2 has no associative arrays): IDLE__A, ...
idle_get() { eval "echo \${IDLE_$1:-0}"; }
idle_set() { eval "IDLE_$1=$2"; }
idle_check() {
  local slot=$1 host port util n
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] || return 0
  util=$(ssh -p "$port" -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new "root@$host" \
         "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits" 2>/dev/null | head -1 | tr -d ' ')
  [ -n "$util" ] || { echo "$(date -u +%FT%TZ) $slot: unreachable for the idle check"; return 0; }
  n=$(idle_get "$slot")
  if [ "$util" = "0" ]; then n=$(( n + 1 )); else n=0; fi
  idle_set "$slot" "$n"
  echo "$(date -u +%FT%TZ) $slot: gpu util ${util}% (idle ticks $n)"
  if [ "$n" -ge "$IDLE_TICKS" ]; then
    echo "$(date -u +%FT%TZ) IDLE ALERT: $slot at 0% GPU for $n ticks - billing for nothing; investigate or terminate" | tee -a logs/idle_E1.alert
  fi
}
while true; do
  [ -f "$STOP" ] && { echo "$(date -u +%FT%TZ) stop file present; watchdog exits"; exit 0; }
  # stdout = one JSON line (stderr kept separate); projection reserves one interval of burn
  out=$(uv run python scripts/pod_budget_check.py --reserve-hours "$HOURS" 2>/dev/null); rc=$?
  echo "$(date -u +%FT%TZ) $out"
  if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then echo "$(date -u +%FT%TZ) budget check failed (rc=$rc); retrying"; sleep 60; continue; fi
  if [ $rc -eq 2 ]; then
    echo "$(date -u +%FT%TZ) HARD STOP: terminating every e1-* pod"
    uv run python scripts/pod_down.py --all-e1 --reason "watchdog hard stop" \
      || echo "$(date -u +%FT%TZ) pod_down FAILED - retrying next minute; terminate in the console if it persists"
    sleep 60; continue   # keep monitoring until no pod is running
  fi
  slots=$(printf '%s\n' "$out" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))")
  for slot in $slots; do
    idle_check "$slot"
  done
  sleep "$INTERVAL"
done
