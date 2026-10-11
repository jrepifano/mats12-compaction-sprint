#!/usr/bin/env bash
# Laptop-side GPU hard stop for one probe (AGENTS.md "Two tiers", 2026-10-11). The loop of
# scripts/i1a_watchdog.sh with the experiment id as an argument, so a new probe needs no new
# watchdog. The id must look like P<NN>; its caps come from probes/P<NN>-<slug>/probe.toml through
# emx.spend.caps_for. Each tick:
# - writes logs/watchdog_<ID>.heartbeat ("<epoch> <interval> <pid>"), which pod_up.py requires;
# - runs pod_budget_check.py --exp <ID> (logged GPU $ + running <id>-* pods + one interval of burn
#   against the probe's GPU limit and hard cap);
# - at a hard stop (exit 2) terminates every <id>-* pod (pod_down.py --all-exp) and keeps
#   monitoring; any other exit fails closed the same way, with an alert in logs/watchdog_<ID>.alert;
# - on healthy ticks terminates <id>-* pods missing from the event log (pod_down.py --untracked)
#   and raises logs/idle_<ID>.alert after IDLE_TICKS ticks at 0% GPU (an alert, never a kill).
# macOS bash 3.2: no associative arrays, no ${x,,}, no mapfile.
#   nohup bash scripts/probe_watchdog.sh P01 [INTERVAL_S] > logs/watchdog_P01.log 2>&1 &
#   touch logs/watchdog_P01.stop     # when the probe's pod work is over (removes the heartbeat)
# PROBE_WATCHDOG_ONCE=1 runs a single tick and exits with the budget check's code (tests).
set -uo pipefail
cd "$(dirname "$0")/.."
KNOWN_HOSTS="$PWD/.ssh_known_hosts"  # repo-local known hosts, never ~/.ssh (AGENTS.md)
EXP=${1:-}
if ! printf '%s' "$EXP" | grep -Eq '^P[0-9]{2,3}$'; then
  echo "usage: probe_watchdog.sh P<NN> [INTERVAL_S]  (got '${EXP}')" >&2; exit 64
fi
INTERVAL=${2:-300}
HOURS=$(python3 -c "print($INTERVAL/3600)")
STOP=${PROBE_WATCHDOG_STOP:-logs/watchdog_$EXP.stop}
ALERT=${PROBE_IDLE_ALERT:-logs/idle_$EXP.alert}
FAIL_ALERT=${PROBE_WATCHDOG_ALERT:-logs/watchdog_$EXP.alert}
HEARTBEAT=${PROBE_WATCHDOG_HEARTBEAT:-logs/watchdog_$EXP.heartbeat}
ONCE=${PROBE_WATCHDOG_ONCE:-0}
IDLE_TICKS=${IDLE_TICKS:-3}   # consecutive ticks at 0% GPU before an idle alert
export WANDB_MODE=offline
mkdir -p logs
rm -f "$STOP"
now() { date -u +%FT%TZ; }
envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }
kill_all() {  # kill_all REASON
  echo "$(now) HARD STOP ($1): terminating every $EXP pod"
  uv run python scripts/pod_down.py --exp "$EXP" --all-exp --reason "$EXP watchdog: $1" \
    || echo "$(now) pod_down FAILED - retrying next minute; terminate in the console if it persists" | tee -a "$FAIL_ALERT"
}
# per-slot idle counters in plain variables (bash 3.2 has no associative arrays): IDLE__P01, ...
idle_get() { eval "echo \${IDLE_$1:-0}"; }
idle_set() { eval "IDLE_$1=$2"; }
idle_check() {
  local slot=$1 host port util n
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] || return 0
  util=$(ssh -p "$port" -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KNOWN_HOSTS "root@$host" \
         "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits" 2>/dev/null | head -1 | tr -d ' ')
  [ -n "$util" ] || { echo "$(now) $slot: unreachable for the idle check"; return 0; }
  n=$(idle_get "$slot")
  if [ "$util" = "0" ]; then n=$(( n + 1 )); else n=0; fi
  idle_set "$slot" "$n"
  echo "$(now) $slot: gpu util ${util}% (idle ticks $n)"
  if [ "$n" -ge "$IDLE_TICKS" ]; then
    echo "$(now) IDLE ALERT ($EXP): $slot at 0% GPU for $n ticks - billing for nothing; investigate or terminate" | tee -a "$ALERT"
  fi
}
while true; do
  if [ -f "$STOP" ]; then
    rm -f "$HEARTBEAT"
    echo "$(now) stop file present; watchdog exits"; exit 0
  fi
  echo "$(date +%s) $INTERVAL $$" > "$HEARTBEAT"
  # stdout = one JSON line (stderr kept separate); projection reserves one interval of burn
  out=$(uv run python scripts/pod_budget_check.py --exp "$EXP" --reserve-hours "$HOURS" 2>/dev/null); rc=$?
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
  uv run python scripts/pod_down.py --exp "$EXP" --untracked --reason "$EXP watchdog reconcile" \
    || echo "$(now) reconcile (pod_down --untracked) FAILED" | tee -a "$FAIL_ALERT"
  slots=$(printf '%s\n' "$out" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))")
  for slot in $slots; do
    idle_check "$slot"
  done
  [ "$ONCE" = "1" ] && exit 0
  sleep "$INTERVAL"
done
