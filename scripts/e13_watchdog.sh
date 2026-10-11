#!/usr/bin/env bash
# Laptop-side GPU hard stop for E13 (E13 implementation plan, unit A; prereg §7 and Amendment B).
# A renamed copy of scripts/eq0_watchdog.sh (2026-10-08) with every call passing --exp E13 (the
# ambient environment is never trusted) and one slot (_A), plus E13's pod-hour stop:
# - each tick first writes a heartbeat (logs/watchdog_E13.heartbeat: "<epoch> <interval> <pid>");
#   pod_up.py --exp E13 refuses to launch unless it is younger than two intervals and the pid is
#   alive;
# - budget: pod_budget_check.py --exp E13 projects logged GPU $ + every running e13-* pod (billed
#   from creation, logs/pods_E13.jsonl) + one interval of their burn against the E13 GPU line,
#   min($38, $50 - API $ recorded and reserved) (emx.spend.CAPS["E13"]), and the combined guard;
# - pod-hours (E13 only; review finding 8): `python -m emx.e13.podhours --status --reserve-hours
#   <one interval>` sums every E13 session in logs/pods_E13.jsonl (closed at their length, open
#   ones to now) plus one interval of every open session; at >= contracts.POD_HOURS_CAP (11 h) it
#   is a hard stop exactly like the budget's (exit 2), and any other exit fails closed;
# - at a hard stop it first salvages the running slot's results/E13 and logs/ into
#   results/E13/_salvage_<slot>_<UTC>/ (one ssh tar, bounded at E13_SALVAGE_S=240 s, with a local
#   MANIFEST.sha256; once per stop; results/E13/adapters is inside results/E13; /ws/adapters is
#   not salvaged), then terminates EVERY e13-* pod the API lists (pod_down.py --exp E13
#   --all-exp) whatever the salvage did, and keeps monitoring, retrying every minute;
# - FAILS CLOSED: any other budget-check or pod-hour-check exit (malformed ledger or event log,
#   crash) writes an alert (logs/watchdog_E13.alert) and terminates every e13-* pod too (after the
#   same salvage of _A);
# - reconciles: each healthy tick terminates e13-* pods missing from the event log
#   (pod_down.py --exp E13 --untracked; a pod pod_up is creating right now is spared briefly);
# - one consolidated ssh call per running slot per tick (CLAUDE.md: several watchers polling
#   every minute trip sshd's MaxStartups) reads both the GPU utilisation and the last anchored
#   progress tag (^E13_) of the newest run log (logs/e13/run_<UTC>.log, scripts/pod_run_e13.sh).
# Idle GPUs only raise an alert (logs/idle_E13.alert), never a kill: a model download looks
# idle from outside. macOS bash 3.2: no associative arrays, no ${x,,}, no mapfile.
#   nohup bash scripts/e13_watchdog.sh [INTERVAL_S] > logs/watchdog_E13.log 2>&1 &
#   touch logs/watchdog_E13.stop     # when E13's pod work is over (removes the heartbeat)
# E13_WATCHDOG_ONCE=1 runs a single tick and exits with the budget (or pod-hour) check's code.
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL=${1:-600}
HOURS=$(python3 -c "print($INTERVAL/3600)")
STOP=${E13_WATCHDOG_STOP:-logs/watchdog_E13.stop}
ALERT=${E13_IDLE_ALERT:-logs/idle_E13.alert}
FAIL_ALERT=${E13_WATCHDOG_ALERT:-logs/watchdog_E13.alert}
HEARTBEAT=${E13_WATCHDOG_HEARTBEAT:-logs/watchdog_E13.heartbeat}
ONCE=${E13_WATCHDOG_ONCE:-0}
IDLE_TICKS=${IDLE_TICKS:-3}   # consecutive ticks at 0% GPU before an idle alert
export WANDB_MODE=offline
mkdir -p logs
rm -f "$STOP"
now() { date -u +%FT%TZ; }
envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }
SALVAGE_S=${E13_SALVAGE_S:-240}   # one wall-clock bound for the salvage of every slot together
SALVAGED=0
salvage() {  # salvage "SLOTS": bounded copy of results/E13 and logs off each pod
  local slot host port d jobs="" j p t=0 alive n rc
  for slot in $1; do
    host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
    [ -n "$host" ] && [ -n "$port" ] || { echo "$(now) salvage $slot: no host in .env"; continue; }
    d="results/E13/_salvage_${slot}_$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$d"
    ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 \
      -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new "root@$host" \
      "cd /ws/emx && tar cf - results/E13 logs 2>/dev/null; exit 0" \
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
  echo "$(now) HARD STOP ($1): terminating every e13-* pod"
  if [ "$SALVAGED" = "0" ]; then
    SALVAGED=1
    salvage "${2:-_A}" || echo "$(now) salvage failed; terminating anyway"
  fi
  uv run python scripts/pod_down.py --exp E13 --all-exp --reason "e13 watchdog: $1" \
    || echo "$(now) pod_down FAILED - retrying next minute; terminate in the console if it persists" | tee -a "$FAIL_ALERT"
}
# per-slot idle counters in plain variables (bash 3.2 has no associative arrays): IDLE__A, ...
idle_get() { eval "echo \${IDLE_$1:-0}"; }
idle_set() { eval "IDLE_$1=$2"; }
running_slots() {  # running_slots BUDGET_JSON: the slots of the running e13-* pods
  printf '%s\n' "$1" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))"
}
idle_check() {
  local slot=$1 host port util n out tag
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] || return 0
  # one ssh call: line 1 the GPU utilisation, line 2 the newest run log's last ^E13_ tag
  out=$(ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@$host" \
         "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits | head -1; l=\$(ls -t /ws/emx/logs/e13/run_*.log 2>/dev/null | head -1); if [ -n \"\$l\" ]; then grep '^E13_' \"\$l\" | tail -1; fi" 2>/dev/null)
  util=$(printf '%s\n' "$out" | head -1 | tr -d ' ')
  tag=$(printf '%s\n' "$out" | sed -n '2p')
  case "$util" in ""|*[!0-9]*) echo "$(now) $slot: unreachable for the idle check"; return 0 ;; esac
  echo "$(now) $slot: progress ${tag:-no E13_ tag yet}"
  n=$(idle_get "$slot")
  if [ "$util" = "0" ]; then n=$(( n + 1 )); else n=0; fi
  idle_set "$slot" "$n"
  echo "$(now) $slot: gpu util ${util}% (idle ticks $n)"
  if [ "$n" -ge "$IDLE_TICKS" ]; then
    echo "$(now) IDLE ALERT (E13): $slot at 0% GPU for $n ticks - billing for nothing; investigate or terminate" | tee -a "$ALERT"
  fi
}
while true; do
  if [ -f "$STOP" ]; then
    rm -f "$HEARTBEAT"
    echo "$(now) stop file present; watchdog exits"; exit 0
  fi
  echo "$(date +%s) $INTERVAL $$" > "$HEARTBEAT"
  # stdout = one JSON line (stderr kept separate); projection reserves one interval of burn
  out=$(uv run python scripts/pod_budget_check.py --exp E13 --reserve-hours "$HOURS" 2>/dev/null); rc=$?
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
  # E13's cumulative pod-hour cap (contracts.POD_HOURS_CAP), with one interval of every open
  # session reserved; exit 2 = hard stop, any other non-zero exit fails closed
  ph=$(uv run python -m emx.e13.podhours --status --reserve-hours "$HOURS" 2>/dev/null); prc=$?
  echo "$(now) pod-hours ${ph:-<no output>}"
  if [ $prc -ne 0 ] && [ $prc -ne 2 ]; then
    echo "$(now) POD-HOUR CHECK FAILED (rc=$prc): failing closed" | tee -a "$FAIL_ALERT"
    kill_all "pod-hour check failed rc=$prc" "$(running_slots "$out")"
    [ "$ONCE" = "1" ] && exit "$prc"
    sleep 60; continue
  fi
  if [ $prc -eq 2 ]; then
    kill_all "pod-hours" "$(running_slots "$out")"
    [ "$ONCE" = "1" ] && exit 2
    sleep 60; continue   # keep monitoring until no pod is running
  fi
  uv run python scripts/pod_down.py --exp E13 --untracked --reason "e13 watchdog reconcile" \
    || echo "$(now) reconcile (pod_down --untracked) FAILED" | tee -a "$FAIL_ALERT"
  slots=$(running_slots "$out")
  for slot in $slots; do   # every running E13 slot
    idle_check "$slot"
  done
  [ "$ONCE" = "1" ] && exit 0
  sleep "$INTERVAL"
done
