#!/usr/bin/env bash
# Laptop-side GPU hard stop for E1b (E1b implementation plan §2 U7, §5: pods _A and _B run
# together and count jointly against the $56 GPU limit). A copy of scripts/i1a_watchdog.sh
# (2026-10-03) with every call passing --exp E1b (the ambient environment is never trusted) and
# without I1a's P0 smoke ceiling (E1b has no approval-gated ceiling; its kill line is the one below):
# - each tick first writes a heartbeat (logs/watchdog_E1b.heartbeat: "<epoch> <interval> <pid>");
#   pod_up.py --exp E1b and pod_part.sh --exp E1b refuse to launch unless it is younger than two
#   intervals and the pid is alive;
# - budget: pod_budget_check.py --exp E1b projects logged GPU $ + every running e1b-* pod (billed
#   from creation, logs/pods_E1b.jsonl; both slots together) + one interval of their burn against
#   min($56, $75 - judges) and the combined guard;
# - at a hard stop (exit 2) it first salvages each running slot's results/E1b/train,
#   results/E1b/logprob and logs/ into results/E1b/_salvage_<slot>_<UTC>/ (one ssh tar per slot,
#   all bounded together at E1B_SALVAGE_S=240 s, with a local MANIFEST.sha256; once per stop),
#   then terminates EVERY e1b-* pod the API lists (pod_down.py --exp E1b --all-exp) whatever the
#   salvage did, and keeps monitoring, retrying every minute;
# - FAILS CLOSED: any other budget-check exit (malformed ledger, crash) writes an alert
#   (logs/watchdog_E1b.alert, which also stops pod_part.sh --exp E1b) and terminates every e1b-*
#   pod too (after the same salvage of slots _A and _B);
# - reconciles: each healthy tick terminates e1b-* pods missing from the event log
#   (pod_down.py --exp E1b --untracked; a pod pod_up is creating right now is spared briefly).
# Idle GPUs only raise an alert (logs/idle_E1b.alert), never a kill: a model download or pod _B
# waiting for alpha.json looks idle from outside. macOS bash 3.2: no associative arrays, no
# ${x,,}, no mapfile.
#   nohup bash scripts/e1b_watchdog.sh [INTERVAL_S] > logs/watchdog_E1b.log 2>&1 &
#   touch logs/watchdog_E1b.stop     # when E1b's pod work is over (removes the heartbeat)
# E1B_WATCHDOG_ONCE=1 runs a single tick and exits with the budget check's code (tests).
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL=${1:-600}
HOURS=$(python3 -c "print($INTERVAL/3600)")
STOP=${E1B_WATCHDOG_STOP:-logs/watchdog_E1b.stop}
ALERT=${E1B_IDLE_ALERT:-logs/idle_E1b.alert}
FAIL_ALERT=${E1B_WATCHDOG_ALERT:-logs/watchdog_E1b.alert}
HEARTBEAT=${E1B_WATCHDOG_HEARTBEAT:-logs/watchdog_E1b.heartbeat}
ONCE=${E1B_WATCHDOG_ONCE:-0}
IDLE_TICKS=${IDLE_TICKS:-3}   # consecutive ticks at 0% GPU before an idle alert
export WANDB_MODE=offline
mkdir -p logs
rm -f "$STOP"
now() { date -u +%FT%TZ; }
envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }
SALVAGE_S=${E1B_SALVAGE_S:-240}   # one wall-clock bound for the salvage of every slot together
SALVAGED=0
salvage() {  # salvage "SLOTS": bounded copy of records, log-prob CSVs and logs off each pod
  local slot host port d jobs="" j p t=0 alive n rc
  for slot in $1; do
    host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
    [ -n "$host" ] && [ -n "$port" ] || { echo "$(now) salvage $slot: no host in .env"; continue; }
    d="results/E1b/_salvage_${slot}_$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$d"
    ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 \
      -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new "root@$host" \
      "cd /ws/emx && tar cf - results/E1b/train results/E1b/logprob logs 2>/dev/null; exit 0" \
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
  echo "$(now) HARD STOP ($1): terminating every e1b-* pod"
  if [ "$SALVAGED" = "0" ]; then
    SALVAGED=1
    salvage "${2:-_A _B}" || echo "$(now) salvage failed; terminating anyway"
  fi
  uv run python scripts/pod_down.py --exp E1b --all-exp --reason "e1b watchdog: $1" \
    || echo "$(now) pod_down FAILED - retrying next minute; terminate in the console if it persists" | tee -a "$FAIL_ALERT"
}
# per-slot idle counters in plain variables (bash 3.2 has no associative arrays): IDLE__A, ...
idle_get() { eval "echo \${IDLE_$1:-0}"; }
idle_set() { eval "IDLE_$1=$2"; }
running_slots() {  # running_slots BUDGET_JSON: the slots of the running e1b-* pods
  printf '%s\n' "$1" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))"
}
idle_check() {
  local slot=$1 host port util n
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] || return 0
  util=$(ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@$host" \
         "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits" 2>/dev/null | head -1 | tr -d ' ')
  [ -n "$util" ] || { echo "$(now) $slot: unreachable for the idle check"; return 0; }
  n=$(idle_get "$slot")
  if [ "$util" = "0" ]; then n=$(( n + 1 )); else n=0; fi
  idle_set "$slot" "$n"
  echo "$(now) $slot: gpu util ${util}% (idle ticks $n)"
  if [ "$n" -ge "$IDLE_TICKS" ]; then
    echo "$(now) IDLE ALERT (E1b): $slot at 0% GPU for $n ticks - billing for nothing; investigate or terminate" | tee -a "$ALERT"
  fi
}
while true; do
  if [ -f "$STOP" ]; then
    rm -f "$HEARTBEAT"
    echo "$(now) stop file present; watchdog exits"; exit 0
  fi
  echo "$(date +%s) $INTERVAL $$" > "$HEARTBEAT"
  # stdout = one JSON line (stderr kept separate); projection reserves one interval of burn
  out=$(uv run python scripts/pod_budget_check.py --exp E1b --reserve-hours "$HOURS" 2>/dev/null); rc=$?
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
  uv run python scripts/pod_down.py --exp E1b --untracked --reason "e1b watchdog reconcile" \
    || echo "$(now) reconcile (pod_down --untracked) FAILED" | tee -a "$FAIL_ALERT"
  slots=$(running_slots "$out")
  for slot in $slots; do   # every running E1b slot (_A and _B together)
    idle_check "$slot"
  done
  [ "$ONCE" = "1" ] && exit 0
  sleep "$INTERVAL"
done
