#!/usr/bin/env bash
# Laptop-side GPU hard stop for EQ0 (EQ0 implementation plan, pod tooling unit). A renamed copy of
# scripts/i9_watchdog.sh (2026-10-08) with every call passing --exp EQ0 (the ambient environment
# is never trusted) and one slot (_A):
# - each tick first writes a heartbeat (logs/watchdog_EQ0.heartbeat: "<epoch> <interval> <pid>");
#   pod_up.py --exp EQ0 refuses to launch unless it is younger than two intervals and the pid is
#   alive;
# - budget: pod_budget_check.py --exp EQ0 projects logged GPU $ + every running eq0-* pod (billed
#   from creation, logs/pods_EQ0.jsonl) + one interval of their burn against the EQ0 GPU line,
#   min($15, $20 - API $ recorded and reserved) (emx.spend.CAPS["EQ0"]), and the combined guard;
# - at a hard stop (exit 2) it first salvages the running slot's results/EQ0 and logs/ into
#   results/EQ0/_salvage_<slot>_<UTC>/ (one ssh tar, bounded at EQ0_SALVAGE_S=240 s, with a local
#   MANIFEST.sha256; once per stop; adapters live under /ws/adapters and are not salvaged), then
#   terminates EVERY eq0-* pod the API lists (pod_down.py --exp EQ0 --all-exp) whatever the
#   salvage did, and keeps monitoring, retrying every minute;
# - FAILS CLOSED: any other budget-check exit (malformed ledger, crash) writes an alert
#   (logs/watchdog_EQ0.alert) and terminates every eq0-* pod too (after the same salvage of _A);
# - reconciles: each healthy tick terminates eq0-* pods missing from the event log
#   (pod_down.py --exp EQ0 --untracked; a pod pod_up is creating right now is spared briefly);
# - one consolidated ssh call per running slot per tick (CLAUDE.md: several watchers polling
#   every minute trip sshd's MaxStartups) reads both the GPU utilisation and the last anchored
#   progress tag (^EQ0_) of the newest run log (logs/eq0/run_<UTC>.log, scripts/pod_run_eq0.sh).
# Idle GPUs only raise an alert (logs/idle_EQ0.alert), never a kill: a model download looks
# idle from outside. macOS bash 3.2: no associative arrays, no ${x,,}, no mapfile.
#   nohup bash scripts/eq0_watchdog.sh [INTERVAL_S] > logs/watchdog_EQ0.log 2>&1 &
#   touch logs/watchdog_EQ0.stop     # when EQ0's pod work is over (removes the heartbeat)
# EQ0_WATCHDOG_ONCE=1 runs a single tick and exits with the budget check's code (tests).
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL=${1:-600}
HOURS=$(python3 -c "print($INTERVAL/3600)")
STOP=${EQ0_WATCHDOG_STOP:-logs/watchdog_EQ0.stop}
ALERT=${EQ0_IDLE_ALERT:-logs/idle_EQ0.alert}
FAIL_ALERT=${EQ0_WATCHDOG_ALERT:-logs/watchdog_EQ0.alert}
HEARTBEAT=${EQ0_WATCHDOG_HEARTBEAT:-logs/watchdog_EQ0.heartbeat}
ONCE=${EQ0_WATCHDOG_ONCE:-0}
IDLE_TICKS=${IDLE_TICKS:-3}   # consecutive ticks at 0% GPU before an idle alert
export WANDB_MODE=offline
mkdir -p logs
rm -f "$STOP"
now() { date -u +%FT%TZ; }
envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }
SALVAGE_S=${EQ0_SALVAGE_S:-240}   # one wall-clock bound for the salvage of every slot together
SALVAGED=0
salvage() {  # salvage "SLOTS": bounded copy of results/EQ0 and logs off each pod
  local slot host port d jobs="" j p t=0 alive n rc
  for slot in $1; do
    host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
    [ -n "$host" ] && [ -n "$port" ] || { echo "$(now) salvage $slot: no host in .env"; continue; }
    d="results/EQ0/_salvage_${slot}_$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$d"
    ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 \
      -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new "root@$host" \
      "cd /ws/emx && tar cf - results/EQ0 logs 2>/dev/null; exit 0" \
      > "$d.tar" 2>/dev/null &
    jobs="$jobs $!:$d"
    echo "$(now) salvage $slot -> $d (bounded at ${SALVAGE_S}s)"
  done
  while [ "$t" -lt "$SALVAGE_S" ]; do
    alive=0
    for j in $jobs; do kill -0 "${j%%:*}" 2>/dev/null && alive=1; done
    [ "$alive" = "0" ] && break
    sleep 1; t=$((t + 1))
  done
  for j in $jobs; do
    p=${j%%:*}; d=${j#*:}; rc=0
    if kill -0 "$p" 2>/dev/null; then kill "$p" 2>/dev/null; rc=124; fi
    wait "$p" 2>/dev/null || [ "$rc" != "0" ] || rc=1
    # a cut-off transfer still yields the files it got; keep the tar then
    if tar xf "$d.tar" -C "$d" 2>/dev/null && [ "$rc" = "0" ]; then rm -f "$d.tar"; else
      echo "$(now) salvage $d: transfer incomplete (rc=$rc; kept $d.tar)" | tee -a "$FAIL_ALERT"
    fi
    ( cd "$d" && find . -type f ! -name MANIFEST.sha256 | sed 's|^\./||' | LC_ALL=C sort \
        | while read -r f; do shasum -a 256 "$f"; done ) > "$d/MANIFEST.sha256"
    n=$(wc -l < "$d/MANIFEST.sha256" | tr -d ' ')
    echo "$(now) salvage $d: $n file(s), local MANIFEST.sha256"
  done
}
kill_all() {  # kill_all REASON [SLOTS]: a bounded salvage (once per stop), then terminate anyway
  echo "$(now) HARD STOP ($1): terminating every eq0-* pod"
  if [ "$SALVAGED" = "0" ]; then
    SALVAGED=1
    salvage "${2:-_A}" || echo "$(now) salvage failed; terminating anyway"
  fi
  uv run python scripts/pod_down.py --exp EQ0 --all-exp --reason "eq0 watchdog: $1" \
    || echo "$(now) pod_down FAILED - retrying next minute; terminate in the console if it persists" | tee -a "$FAIL_ALERT"
}
# per-slot idle counters in plain variables (bash 3.2 has no associative arrays): IDLE__A, ...
idle_get() { eval "echo \${IDLE_$1:-0}"; }
idle_set() { eval "IDLE_$1=$2"; }
running_slots() {  # running_slots BUDGET_JSON: the slots of the running eq0-* pods
  printf '%s\n' "$1" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))"
}
idle_check() {
  local slot=$1 host port util n out tag
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] || return 0
  # one ssh call: line 1 the GPU utilisation, line 2 the newest run log's last ^EQ0_ tag
  out=$(ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@$host" \
         "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits | head -1; l=\$(ls -t /ws/emx/logs/eq0/run_*.log 2>/dev/null | head -1); if [ -n \"\$l\" ]; then grep '^EQ0_' \"\$l\" | tail -1; fi" 2>/dev/null)
  util=$(printf '%s\n' "$out" | head -1 | tr -d ' ')
  tag=$(printf '%s\n' "$out" | sed -n '2p')
  case "$util" in ""|*[!0-9]*) echo "$(now) $slot: unreachable for the idle check"; return 0 ;; esac
  echo "$(now) $slot: progress ${tag:-no EQ0_ tag yet}"
  n=$(idle_get "$slot")
  if [ "$util" = "0" ]; then n=$(( n + 1 )); else n=0; fi
  idle_set "$slot" "$n"
  echo "$(now) $slot: gpu util ${util}% (idle ticks $n)"
  if [ "$n" -ge "$IDLE_TICKS" ]; then
    echo "$(now) IDLE ALERT (EQ0): $slot at 0% GPU for $n ticks - billing for nothing; investigate or terminate" | tee -a "$ALERT"
  fi
}
while true; do
  if [ -f "$STOP" ]; then
    rm -f "$HEARTBEAT"
    echo "$(now) stop file present; watchdog exits"; exit 0
  fi
  echo "$(date +%s) $INTERVAL $$" > "$HEARTBEAT"
  # stdout = one JSON line (stderr kept separate); projection reserves one interval of burn
  out=$(uv run python scripts/pod_budget_check.py --exp EQ0 --reserve-hours "$HOURS" 2>/dev/null); rc=$?
  echo "$(now) $out"
  if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then
    echo "$(now) BUDGET CHECK FAILED (rc=$rc): failing closed" | tee -a "$FAIL_ALERT"
    kill_all "budget check failed rc=$rc"
    [ "$ONCE" = "1" ] && exit "$rc"
    sleep 60; continue
  fi
  if [ $rc -eq 2 ]; then
    kill_all "budget" "$(running_slots "$out")"
    [ "$ONCE" = "1" ] && exit 2
    sleep 60; continue   # keep monitoring until no pod is running
  fi
  uv run python scripts/pod_down.py --exp EQ0 --untracked --reason "eq0 watchdog reconcile" \
    || echo "$(now) reconcile (pod_down --untracked) FAILED" | tee -a "$FAIL_ALERT"
  slots=$(running_slots "$out")
  for slot in $slots; do   # every running EQ0 slot
    idle_check "$slot"
  done
  [ "$ONCE" = "1" ] && exit 0
  sleep "$INTERVAL"
done
