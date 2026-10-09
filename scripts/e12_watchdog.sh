#!/usr/bin/env bash
# Laptop-side GPU hard stop for E12 (E12 implementation plan v0.3.1, unit U5). A renamed copy of
# scripts/eq0_watchdog.sh (itself a copy of the I9 watchdog; 2026-10-08) with every call passing
# --exp E12 (the ambient environment is never trusted), one slot (_A), and one added stop:
# - each tick first writes a heartbeat (logs/watchdog_E12.heartbeat: "<epoch> <interval> <pid>");
#   pod_up.py --exp E12 refuses to launch unless it is younger than two intervals and the pid is
#   alive;
# - budget: pod_budget_check.py --exp E12 projects logged GPU $ + every running e12-* pod (billed
#   from creation, logs/pods_E12.jsonl) + one interval of their burn against the E12 GPU line,
#   min($25, $40 - API $ recorded and reserved) (emx.spend.CAPS["E12"]), and the combined guard;
# - pod hours (E12 only): a running e12-* pod whose elapsed hours (pod_budget_check's "hours",
#   billed from creation) plus one interval reach POD_HOURS_LIMIT (7 h, emx.e12.contracts; a test
#   keeps the two equal) is a hard stop too, on every GPU type, so no pod runs past 7 h even when
#   its dollars are under the GPU line (cheaper GPUs; the reservation deadline is the backstop);
# - early archival (code review r1 finding 2): once the projected hard stop (the earlier of the
#   7 h rule and the GPU kill line, from the budget check's hours, projection and burn) is within
#   E12_ARCHIVE_LEAD_H (0.5 h), it starts `scripts/e12_control.py archive --slot _A
#   --while-running` ONCE in the background (no --recipe: every recipe with an attempt, the active
#   one first; E12_ARCHIVE_RECIPE pins one), bounded at E12_ARCHIVE_EARLY_S (2700 s) and logged to
#   logs/watchdog_E12_archive_early_<UTC>.log, while the ticks go on: the verified tar pull of the
#   rank-32 adapter files under /ws/e12 and a `local` retention receipt;
# - at a hard stop (exit 2) it kills a still-running early archive, then, concurrently and within
#   ONE bound (the seconds left before the real kill line minus E12_STOP_MARGIN_S=90, capped at
#   E12_ARCHIVE_STOP_S=300; E12_SALVAGE_S=240 when the line is unknown, as in fail-closed), runs
#   the same archive again (only new checkpoints move: earlier verified files are linked) and
#   salvages the running slot's results/E12 and logs/ into results/E12/_salvage_<slot>_<UTC>/
#   (one ssh tar with a local MANIFEST.sha256; once per stop). It logs "RETENTION (stop):
#   <recipe>=true|false" from the archive's `retained` output, and alerts loudly unless every
#   recipe is retained. Then it terminates EVERY e12-* pod the API lists (pod_down.py --exp E12
#   --all-exp) whatever the salvage and the archive did (never past the kill line: with no time
#   left both are skipped), and keeps monitoring, retrying every minute;
# - FAILS CLOSED: any other budget-check exit (malformed ledger, crash) writes an alert
#   (logs/watchdog_E12.alert) and terminates every e12-* pod too (after the same salvage of _A);
# - reconciles: each healthy tick terminates e12-* pods missing from the event log
#   (pod_down.py --exp E12 --untracked; a pod pod_up is creating right now is spared briefly);
# - one consolidated ssh call per running slot per tick (CLAUDE.md: several watchers polling
#   every minute trip sshd's MaxStartups) reads both the GPU utilisation and the last anchored
#   progress tag (^E12_) of the newest run log (logs/e12/run_<UTC>.log, scripts/pod_run_e12.sh).
# Idle GPUs only raise an alert (logs/idle_E12.alert), never a kill: a model download looks
# idle from outside. macOS bash 3.2: no associative arrays, no ${x,,}, no mapfile.
#   nohup bash scripts/e12_watchdog.sh [INTERVAL_S] > logs/watchdog_E12.log 2>&1 &
#   touch logs/watchdog_E12.stop     # when E12's pod work is over (removes the heartbeat)
# E12_WATCHDOG_ONCE=1 runs a single tick and exits with the budget check's code (tests).
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL=${1:-600}
HOURS=$(python3 -c "print($INTERVAL/3600)")
POD_HOURS_LIMIT=7.0   # = emx.e12.contracts.POD_HOURS_LIMIT (tests/e12/test_pod_e12.py checks it)
STOP=${E12_WATCHDOG_STOP:-logs/watchdog_E12.stop}
ALERT=${E12_IDLE_ALERT:-logs/idle_E12.alert}
FAIL_ALERT=${E12_WATCHDOG_ALERT:-logs/watchdog_E12.alert}
HEARTBEAT=${E12_WATCHDOG_HEARTBEAT:-logs/watchdog_E12.heartbeat}
ONCE=${E12_WATCHDOG_ONCE:-0}
IDLE_TICKS=${IDLE_TICKS:-3}   # consecutive ticks at 0% GPU before an idle alert
export WANDB_MODE=offline
mkdir -p logs
rm -f "$STOP"
now() { date -u +%FT%TZ; }
envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }
SALVAGE_S=${E12_SALVAGE_S:-240}   # one wall-clock bound for the salvage of every slot together
ARCHIVE_LEAD_H=${E12_ARCHIVE_LEAD_H:-0.5}   # start the early archive this long before the stop
ARCHIVE_EARLY_S=${E12_ARCHIVE_EARLY_S:-2700}
ARCHIVE_STOP_S=${E12_ARCHIVE_STOP_S:-300}
STOP_MARGIN_S=${E12_STOP_MARGIN_S:-90}   # left for pod_down before the real kill line
ARCHIVE_RECIPE=${E12_ARCHIVE_RECIPE:-}
SALVAGED=0
EARLY_STARTED=0; EARLY_PID=""; EARLY_LOG=""; EARLY_T0=0
killtree() {  # killtree PID: the process and all its descendants (bash 3.2: recursion, pgrep -P)
  local c
  for c in $(pgrep -P "$1" 2>/dev/null); do killtree "$c"; done
  kill "$1" 2>/dev/null
}
ARCH_PID=""; ARCH_LOG=""
archive_bg() {  # archive_bg MODE: start the while-running archive as a child of this shell (not a
  # subshell, so `wait` reaps it); sets ARCH_PID and ARCH_LOG
  ARCH_LOG="logs/watchdog_E12_archive_$1_$(date -u +%Y%m%dT%H%M%SZ).log"
  if [ -n "$ARCHIVE_RECIPE" ]; then
    uv run python scripts/e12_control.py archive --slot _A --while-running --recipe "$ARCHIVE_RECIPE" \
      < /dev/null > "$ARCH_LOG" 2>&1 &
  else
    uv run python scripts/e12_control.py archive --slot _A --while-running < /dev/null > "$ARCH_LOG" 2>&1 &
  fi
  ARCH_PID=$!
}
retention() {  # retention MODE PID LOG: reap a finished archive; log and alert on its `retained`
  local rc=0 r
  wait "$2" 2>/dev/null || rc=$?
  r=$(python3 -c "import json,sys
lines=[l for l in open(sys.argv[1]).read().splitlines() if l.strip()]
try: d=json.loads(lines[-1])
except Exception: d={}
r=d.get('retained') if isinstance(d.get('retained'), dict) else {}
print(' '.join('%s=%s' % (k, str(v).lower()) for k, v in sorted(r.items())) or 'unknown')" "$3" 2>/dev/null)
  echo "$(now) RETENTION ($1): ${r:-unknown} (archive exit $rc, log $3)"
  case " ${r:-unknown} " in
    *=false*|*" unknown "*) echo "$(now) RETENTION NOT CONFIRMED ($1): ${r:-unknown}; see $3 - rank-32 adapters may be lost at termination" | tee -a "$FAIL_ALERT" ;;
  esac
}
early_check() {  # reap or bound the background early archive
  [ -n "$EARLY_PID" ] || return 0
  if kill -0 "$EARLY_PID" 2>/dev/null; then
    if [ $((SECONDS - EARLY_T0)) -ge "$ARCHIVE_EARLY_S" ]; then
      killtree "$EARLY_PID"
      echo "$(now) EARLY ARCHIVE timed out after ${ARCHIVE_EARLY_S}s (killed); see $EARLY_LOG" | tee -a "$FAIL_ALERT"
      wait "$EARLY_PID" 2>/dev/null; EARLY_PID=""
    fi
    return 0
  fi
  retention early "$EARLY_PID" "$EARLY_LOG"; EARLY_PID=""
}
early_stop() {  # at a hard stop: a still-running early archive gives way to the stop's own
  [ -n "$EARLY_PID" ] || return 0
  if kill -0 "$EARLY_PID" 2>/dev/null; then
    killtree "$EARLY_PID"
    echo "$(now) early archive still running at the hard stop: killed; see $EARLY_LOG"
    wait "$EARLY_PID" 2>/dev/null
  else
    retention early "$EARLY_PID" "$EARLY_LOG"
  fi
  EARLY_PID=""
}
deadline() {  # deadline BUDGET_JSON: "<hours to the hard-stop tick> <seconds to the real line>"
  printf '%s\n' "$1" | tail -1 | python3 -c "import json,sys
d=json.loads(sys.stdin.readline()); H=$HOURS; L=$POD_HOURS_LIMIT
run=d.get('running') or []
if not run: sys.exit(0)
t=[L-float(s.get('hours') or 0)-H for s in run]; r=[L-float(s.get('hours') or 0) for s in run]
b=float(d.get('burn_per_hr') or 0)
if b > 0 and 'gpu_kill_limit_usd' in d and 'projected_gpu_usd' in d:
    g=(float(d['gpu_kill_limit_usd'])-float(d['projected_gpu_usd']))/b
    t.append(g); r.append(g+float(d.get('reserve_hours') or H))
print('%.4f %d' % (min(t), max(0, int(min(r)*3600))))" 2>/dev/null
}
salvage() {  # salvage "SLOTS" [BOUND_S]: bounded copy of results/E12 and logs off each pod
  local slot host port d jobs="" j p t=0 alive n rc bound=${2:-$SALVAGE_S}
  for slot in $1; do
    host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
    [ -n "$host" ] && [ -n "$port" ] || { echo "$(now) salvage $slot: no host in .env"; continue; }
    d="results/E12/_salvage_${slot}_$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$d"
    ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 \
      -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new "root@$host" \
      "cd /ws/emx && tar cf - results/E12 logs 2>/dev/null; exit 0" \
      > "$d.tar" 2>/dev/null &
    jobs="$jobs $!:$d"
    echo "$(now) salvage $slot -> $d (bounded at ${bound}s)"
  done
  while [ "$t" -lt "$bound" ]; do
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
kill_all() {  # kill_all REASON [SLOTS] [SECONDS_TO_LINE]: a bounded archive and salvage (once
  # per stop), then terminate anyway
  local bound left slots=${2:-_A} t0
  echo "$(now) HARD STOP ($1): terminating every e12-* pod"
  if [ "$SALVAGED" = "0" ]; then
    SALVAGED=1
    early_stop
    left=${3:-}
    if [ -n "$left" ]; then
      bound=$(( left - STOP_MARGIN_S ))
      [ "$bound" -gt "$ARCHIVE_STOP_S" ] && bound=$ARCHIVE_STOP_S
    else
      bound=$SALVAGE_S   # the kill line is unknown (fail-closed): the salvage's own bound
    fi
    if [ "$bound" -gt 0 ]; then
      archive_bg stop
      echo "$(now) ARCHIVE (stop) started pid $ARCH_PID, bounded at ${bound}s, log $ARCH_LOG"
      t0=$SECONDS
      if [ "$bound" -lt "$SALVAGE_S" ]; then salvage "$slots" "$bound"; else salvage "$slots"; fi \
        || echo "$(now) salvage failed; terminating anyway"
      while kill -0 "$ARCH_PID" 2>/dev/null && [ $((SECONDS - t0)) -lt "$bound" ]; do sleep 1; done
      if kill -0 "$ARCH_PID" 2>/dev/null; then
        killtree "$ARCH_PID"
        echo "$(now) ARCHIVE (stop) timed out after ${bound}s (killed); see $ARCH_LOG" | tee -a "$FAIL_ALERT"
      fi
      retention stop "$ARCH_PID" "$ARCH_LOG"
    else
      echo "$(now) NO TIME before the kill line (${left:-?}s left): archive and salvage skipped; RETENTION NOT CONFIRMED" | tee -a "$FAIL_ALERT"
    fi
  fi
  uv run python scripts/pod_down.py --exp E12 --all-exp --reason "e12 watchdog: $1" \
    || echo "$(now) pod_down FAILED - retrying next minute; terminate in the console if it persists" | tee -a "$FAIL_ALERT"
}
# per-slot idle counters in plain variables (bash 3.2 has no associative arrays): IDLE__A, ...
idle_get() { eval "echo \${IDLE_$1:-0}"; }
idle_set() { eval "IDLE_$1=$2"; }
running_slots() {  # running_slots BUDGET_JSON: the slots of the running e12-* pods
  printf '%s\n' "$1" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))"
}
over_hours() {  # over_hours BUDGET_JSON: the slots whose hours + one interval reach the limit
  printf '%s\n' "$1" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s.get('slot') or '?' for s in d['running'] if float(s.get('hours') or 0) + $HOURS >= $POD_HOURS_LIMIT))"
}
idle_check() {
  local slot=$1 host port util n out tag
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] || return 0
  # one ssh call: line 1 the GPU utilisation, line 2 the newest run log's last ^E12_ tag
  out=$(ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@$host" \
         "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits | head -1; l=\$(ls -t /ws/emx/logs/e12/run_*.log 2>/dev/null | head -1); if [ -n \"\$l\" ]; then grep '^E12_' \"\$l\" | tail -1; fi" 2>/dev/null)
  util=$(printf '%s\n' "$out" | head -1 | tr -d ' ')
  tag=$(printf '%s\n' "$out" | sed -n '2p')
  case "$util" in ""|*[!0-9]*) echo "$(now) $slot: unreachable for the idle check"; return 0 ;; esac
  echo "$(now) $slot: progress ${tag:-no E12_ tag yet}"
  n=$(idle_get "$slot")
  if [ "$util" = "0" ]; then n=$(( n + 1 )); else n=0; fi
  idle_set "$slot" "$n"
  echo "$(now) $slot: gpu util ${util}% (idle ticks $n)"
  if [ "$n" -ge "$IDLE_TICKS" ]; then
    echo "$(now) IDLE ALERT (E12): $slot at 0% GPU for $n ticks - billing for nothing; investigate or terminate" | tee -a "$ALERT"
  fi
}
while true; do
  if [ -f "$STOP" ]; then
    rm -f "$HEARTBEAT"
    echo "$(now) stop file present; watchdog exits"; exit 0
  fi
  echo "$(date +%s) $INTERVAL $$" > "$HEARTBEAT"
  # stdout = one JSON line (stderr kept separate); projection reserves one interval of burn
  out=$(uv run python scripts/pod_budget_check.py --exp E12 --reserve-hours "$HOURS" 2>/dev/null); rc=$?
  echo "$(now) $out"
  if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then
    echo "$(now) BUDGET CHECK FAILED (rc=$rc): failing closed" | tee -a "$FAIL_ALERT"
    kill_all "budget check failed rc=$rc"
    [ "$ONCE" = "1" ] && exit "$rc"
    sleep 60; continue
  fi
  early_check
  dl=$(deadline "$out"); left_h=${dl% *}; left_s=${dl#* }
  [ -n "$dl" ] || { left_h=""; left_s=""; }
  if [ $rc -eq 2 ]; then
    kill_all "budget" "$(running_slots "$out")" "$left_s"
    [ "$ONCE" = "1" ] && exit 2
    sleep 60; continue   # keep monitoring until no pod is running
  fi
  late=$(over_hours "$out"); hrc=$?
  if [ $hrc -ne 0 ]; then   # unreadable hours: fail closed, as for the budget check
    echo "$(now) POD-HOURS CHECK FAILED (rc=$hrc): failing closed" | tee -a "$FAIL_ALERT"
    kill_all "pod-hours check failed rc=$hrc"
    [ "$ONCE" = "1" ] && exit 3
    sleep 60; continue
  fi
  if [ -n "$late" ]; then   # E12: 7 elapsed pod-hours on any GPU type
    echo "$(now) POD HOURS: slot(s) $late at >= ${POD_HOURS_LIMIT} h within one interval"
    kill_all "pod hours >= ${POD_HOURS_LIMIT}" "$(running_slots "$out")" "$left_s"
    [ "$ONCE" = "1" ] && exit 2
    sleep 60; continue
  fi
  if [ "$EARLY_STARTED" = "0" ] && [ -n "$left_h" ] \
      && python3 -c "import sys; sys.exit(0 if $left_h <= $ARCHIVE_LEAD_H else 1)"; then
    EARLY_STARTED=1
    archive_bg early
    EARLY_PID=$ARCH_PID; EARLY_LOG=$ARCH_LOG; EARLY_T0=$SECONDS
    echo "$(now) EARLY ARCHIVE: the hard stop is ${left_h} h away (lead ${ARCHIVE_LEAD_H} h); started pid $EARLY_PID, bounded at ${ARCHIVE_EARLY_S}s, log $EARLY_LOG"
  fi
  uv run python scripts/pod_down.py --exp E12 --untracked --reason "e12 watchdog reconcile" \
    || echo "$(now) reconcile (pod_down --untracked) FAILED" | tee -a "$FAIL_ALERT"
  slots=$(running_slots "$out")
  for slot in $slots; do   # every running E12 slot
    idle_check "$slot"
  done
  [ "$ONCE" = "1" ] && exit 0
  sleep "$INTERVAL"
done
