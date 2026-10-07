#!/usr/bin/env bash
# Laptop-side GPU hard stop and pod supervisor for E9 (prereg §7.3, §7.6, §8.7; implementation plan
# v4, unit F2). Adapted from E3's watchdog (git show 7b15be1:scripts/e3_watchdog.sh, itself from
# e1b_watchdog.sh / i1a_watchdog.sh); every call passes --exp E9 (the ambient environment is never
# trusted). It is the PRIMARY cap (CLAUDE.md): each pod also carries its own killswitch, armed by
# pod_push.sh at the pod's own budget deadline; this watchdog never arms or moves a deadline.
# Each tick (INTERVAL_S, 1..120 s, default 120):
# - heartbeat logs/watchdog_E9.heartbeat ("<epoch> <interval> <pid>", what emx.runpod.watchdog_live
#   reads; pod_up.py --exp E9 refuses to launch without a live one) and pidfile
#   logs/watchdog_E9.pid (one watchdog at a time);
# - budget: `python -m emx.runpod --exp E9 --e9-budget-check --reserve-hours H` projects logged GPU $
#   + every running e9-* pod's cost so far + their burn over the whole bounded shutdown latency
#   (RESERVE_S) against the BACKSTOP kill line min($145, $200 - current API reserve - API recorded
#   and reserved) (emx.runpod.e9_backstop_usd; the API reserve from emx.e9.budget, else the
#   sign-off $62), over all E9 pods together. At a hard stop (exit 2) it salvages each running
#   slot's results/E9 and logs (one bounded tar transfer for all slots, when it still fits under
#   the line), terminates EVERY e9-* pod the API lists (pod_down.py --exp E9 --all-exp, which
#   charges and settles each under the E9 admission lock), then asks the orchestrator to record the
#   stop (scripts/e9_orchestrate.py stop, when present). Any other non-zero exit of the budget
#   check counts as a failure; 3 in a row fail closed the same way (the pods' own budget deadlines
#   still bound them meanwhile). It keeps monitoring after a stop;
# - reconcile: terminates e9-* pods missing from the event log (pod_down.py --exp E9 --untracked);
# - idle: ONE consolidated ssh call per running slot asks whether a job runs (bracketed pgrep for
#   pod_run_e9.sh, pod_setup.sh or a scripts/e9_* process). A slot without one for
#   contracts.IDLE_POD_MINUTES (clock in logs/watchdog_E9.idle_<slot>, so a restart keeps it)
#   prints `E9_WATCHDOG_IDLE <slot>` and retires the slot; an unreachable pod neither starts nor
#   resets the clock;
# - the 7-day deadline (contracts.DEADLINE_UTC, §7.6): once passed, each running slot prints
#   `E9_WATCHDOG_DEADLINE <slot>` and is retired.
# Retiring a slot = `e9_orchestrate.py cleanup --slot S --reason R` (fetch, verify, terminate,
# settle) when the orchestrator exists, else a bounded salvage of the slot then
# `pod_down.py --exp E9 --slot S`. Every blocking call is bounded (its own process group, killed
# at the deadline, exit 124); cleanups run in the background, one per slot.
# macOS bash 3.2: no associative arrays, no ${x,,}, no mapfile.
#   nohup bash scripts/e9_watchdog.sh [INTERVAL_S] > logs/watchdog_E9.log 2>&1 &
#   bash scripts/e9_watchdog.sh --check-alive   # exit 0 iff a live watchdog owns the pidfile
#   touch logs/watchdog_E9.stop                 # when E9's pod work is over
# E9_WATCHDOG_ONCE=1 runs a single tick and exits with the budget check's code (tests).
# E9_BUDGET_CMD replaces the budget check command (tests; called with the reserve hours);
# E9_POD_DOWN replaces `uv run python scripts/pod_down.py --exp E9` (tests).
set -uo pipefail
cd "$(dirname "$0")/.."
PIDFILE=${E9_WATCHDOG_PIDFILE:-logs/watchdog_E9.pid}
HEARTBEAT=${E9_WATCHDOG_HEARTBEAT:-logs/watchdog_E9.heartbeat}
STOP=${E9_WATCHDOG_STOP:-logs/watchdog_E9.stop}
FAIL_ALERT=${E9_WATCHDOG_ALERT:-logs/watchdog_E9.alert}
IDLE_PREFIX=${E9_IDLE_PREFIX:-logs/watchdog_E9.idle_}
ONCE=${E9_WATCHDOG_ONCE:-0}
MAX_INTERVAL=120
now() { date -u +%FT%TZ; }

alive() {  # 0 iff the pidfile names a running e9_watchdog.sh whose heartbeat is fresh
  local pid cmd hb_t hb_i hb_pid age
  [ -s "$PIDFILE" ] || { echo "no pidfile $PIDFILE"; return 1; }
  pid=$(head -n 1 "$PIDFILE" | tr -d ' \n')
  case "$pid" in ""|*[!0-9]*) echo "malformed pidfile $PIDFILE"; return 1 ;; esac
  kill -0 "$pid" 2>/dev/null || { echo "pid $pid (pidfile) is not running"; return 1; }
  cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
  case "$cmd" in
    *e9_watchdog.sh*) ;;
    *) echo "pid $pid is not an e9_watchdog.sh ($cmd)"; return 1 ;;
  esac
  [ -s "$HEARTBEAT" ] || { echo "no heartbeat $HEARTBEAT"; return 1; }
  read -r hb_t hb_i hb_pid < "$HEARTBEAT" || true
  case "$hb_t$hb_i$hb_pid" in ""|*[!0-9]*) echo "malformed heartbeat"; return 1 ;; esac
  [ "$hb_pid" = "$pid" ] || { echo "heartbeat pid $hb_pid != pidfile pid $pid"; return 1; }
  age=$(( $(date +%s) - hb_t ))
  [ "$age" -le $(( 2 * hb_i + 60 )) ] || { echo "heartbeat ${age}s old"; return 1; }
  echo "e9 watchdog alive (pid $pid, heartbeat ${age}s old)"
  return 0
}
if [ "${1:-}" = "--check-alive" ]; then
  alive; exit $?
fi

INTERVAL=${1:-$MAX_INTERVAL}
case "$INTERVAL" in ""|*[!0-9]*) echo "INTERVAL_S must be whole seconds" >&2; exit 2 ;; esac
if [ "$INTERVAL" -lt 1 ] || [ "$INTERVAL" -gt "$MAX_INTERVAL" ]; then
  echo "INTERVAL_S must be 1..$MAX_INTERVAL" >&2; exit 2
fi
BUDGET_S=${E9_BUDGET_S:-90}          # one budget check
TERMINATE_S=${E9_TERMINATE_S:-150}   # one pod_down.py call
IDLE_SSH_S=${E9_IDLE_SSH_S:-45}      # one idle-check ssh call
CLEANUP_S=${E9_CLEANUP_S:-3600}      # one slot's retirement (3 bounded pulls + termination)
FAST_S_CMD=${E9_FAST_CLEANUP_S:-600} # the orchestrator's stop record
MAX_SLOTS=4                          # the four-slot schedule (prereg §7.2)
SALVAGE_S=${E9_SALVAGE_S:-240}       # the whole pre-DELETE salvage transfer, every slot together
GRACE_S=${E9_GRACE_S:-10}            # a bounded call's kill grace + start-up
KILL_ATTEMPTS=${E9_KILL_ATTEMPTS:-2} # pod_down --all-exp calls at a hard stop
MAX_FAILS=3                          # budget-check failures in a row before failing closed
# from a projection just under the line to the DELETE: the interval, the rest of the tick
# (reconcile, one idle ssh per slot), the next check (which fires), the salvage-margin check, the
# salvage and every termination attempt; plus MAX_FAILS-1 failed checks before a fail-closed stop
RESERVE_S=$(( INTERVAL + (MAX_FAILS + 1) * (BUDGET_S + GRACE_S) + (TERMINATE_S + GRACE_S) \
  + MAX_SLOTS * (IDLE_SSH_S + GRACE_S) + (SALVAGE_S + GRACE_S) \
  + KILL_ATTEMPTS * (TERMINATE_S + GRACE_S) + (MAX_FAILS - 1) * INTERVAL ))
FAST_S=$(( (BUDGET_S + GRACE_S) + (SALVAGE_S + GRACE_S) + KILL_ATTEMPTS * (TERMINATE_S + GRACE_S) ))
HOURS=$(python3 -c "print($RESERVE_S/3600)")
FAST_HOURS=$(python3 -c "print($FAST_S/3600)")
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
C_VALS=$("$PY" - src/emx/e9/contracts.py <<'PY_E9'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("e9_contracts", sys.argv[1])
C = importlib.util.module_from_spec(spec)
sys.modules["e9_contracts"] = C
spec.loader.exec_module(C)
print(C.IDLE_POD_MINUTES, int(C.DEADLINE_UTC.timestamp()))
PY_E9
) || { echo "could not read src/emx/e9/contracts.py; refusing to start" >&2; exit 2; }
IDLE_MIN=${E9_IDLE_MINUTES:-${C_VALS%% *}}
DEADLINE_EPOCH=${E9_DEADLINE_EPOCH:-${C_VALS##* }}
mkdir -p logs "$(dirname "$PIDFILE")" "$(dirname "$HEARTBEAT")" "$(dirname "$IDLE_PREFIX")"
if other=$(alive); then
  echo "$(now) another E9 watchdog is running ($other); refusing to start" >&2
  exit 3
fi
echo "$$" > "$PIDFILE"
BG=""   # bounded background jobs, "pid:tag"
release() {  # stop our background jobs; remove the pidfile and heartbeat only if still ours
  local j
  for j in $BG; do kill "${j%%:*}" 2>/dev/null; done
  [ "$(head -n 1 "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE" "$HEARTBEAT"
  return 0
}
trap release EXIT
trap 'exit 143' TERM INT   # so that the EXIT trap runs on a kill
export WANDB_MODE=offline
rm -f "$STOP"
envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }

BOUNDED_PY='
import os, signal, subprocess, sys
secs, cmd = float(sys.argv[1]), sys.argv[2:]
p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=True)
def kill(sig):
    try:
        os.killpg(p.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
def on_term(*_):
    kill(signal.SIGTERM)
    sys.exit(143)
signal.signal(signal.SIGTERM, on_term)
try:
    sys.exit(p.wait(timeout=secs))
except subprocess.TimeoutExpired:
    kill(signal.SIGTERM)
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        kill(signal.SIGKILL)
        p.wait()
    print("bounded: killed after %.0f s: %s" % (secs, " ".join(cmd[:7])), file=sys.stderr)
    sys.exit(124)
'
bounded() { local secs=$1; shift; "$PY" -c "$BOUNDED_PY" "$secs" "$@"; }
spawn() {  # spawn TAG SECS CMD...: one bounded background job per TAG (foreground when ONCE=1)
  local tag=$1 secs=$2 j rc; shift 2
  for j in $BG; do [ "${j#*:}" = "$tag" ] && { echo "$(now) $tag still running"; return 0; }; done
  if [ "$ONCE" = "1" ]; then
    bounded "$secs" "$@"; rc=$?
    on_done "$tag" "$rc"
  else
    bounded "$secs" "$@" &
    BG="$BG $!:$tag"
  fi
}
reap_bg() {
  local j p rc keep=""
  for j in $BG; do
    p=${j%%:*}
    if kill -0 "$p" 2>/dev/null; then keep="$keep $j"; continue; fi
    wait "$p"; rc=$?
    on_done "${j#*:}" "$rc"
  done
  BG=$keep
}
on_done() {  # on_done TAG RC
  case "$1" in
    retire_*)
      if [ "$2" = "0" ]; then rm -f "$IDLE_PREFIX${1#retire_}"; else
        echo "$(now) retiring ${1#retire_} FAILED rc=$2; retried next tick" | tee -a "$FAIL_ALERT"
      fi ;;
    *) [ "$2" = "0" ] || echo "$(now) $1 FAILED rc=$2" | tee -a "$FAIL_ALERT" ;;
  esac
}
salvage() {  # salvage "SLOTS": ONE bounded transfer of every slot's results/E9 and logs
  local slot host port d script=""
  for slot in $1; do
    host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
    [ -n "$host" ] && [ -n "$port" ] || { echo "$(now) salvage $slot: no host in .env"; continue; }
    d="results/E9/_staging_salvage_${slot}_$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$d"
    script="$script( ssh -n -p $(printf %q "$port") -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new $(printf %q "root@$host") $(printf %q "cd /ws/emx && tar cf - --exclude=model --exclude='*.safetensors' --exclude='*.bin' --exclude='*.pt' --exclude='*.pth' results/E9 logs 2>/dev/null; exit 0") > $(printf %q "$d.tar") 2>/dev/null ) &
"
    echo "$(now) salvage $slot -> $d.tar (all slots bounded at ${SALVAGE_S}s; extract with tar xf)"
  done
  [ -n "$script" ] || return 0
  bounded "$SALVAGE_S" bash -c "${script}wait" \
    || echo "$(now) salvage transfer cut at ${SALVAGE_S}s; terminating anyway" | tee -a "$FAIL_ALERT"
}
# pod_down.py --exp E9 behind a shim script, so that `bounded` can run it (E9_POD_DOWN: tests)
POD_DOWN_SHIM=$(mktemp)
trap 'release; rm -f "$POD_DOWN_SHIM"' EXIT
if [ -n "${E9_POD_DOWN:-}" ]; then
  printf '#!/usr/bin/env bash\nexec %s "$@"\n' "$E9_POD_DOWN" > "$POD_DOWN_SHIM"
else
  printf '#!/usr/bin/env bash\nexec uv run python scripts/pod_down.py --exp E9 "$@"\n' > "$POD_DOWN_SHIM"
fi
chmod 700 "$POD_DOWN_SHIM"
bounded_pod_down() { local secs=$1; shift; bounded "$secs" bash "$POD_DOWN_SHIM" "$@"; }
kill_all() {  # kill_all REASON SLOTS SALVAGE(1|0)
  local i=1 killed=0
  echo "$(now) HARD STOP ($1): terminating every e9-* pod"
  if [ "$3" != "1" ]; then
    echo "$(now) no margin for salvage under the backstop: terminating at once" | tee -a "$FAIL_ALERT"
  elif [ "$SALVAGED" = "0" ]; then   # once per stop: later passes retry termination only
    SALVAGED=1
    salvage "$2"
  fi
  while [ "$i" -le "$KILL_ATTEMPTS" ]; do
    if bounded_pod_down "$TERMINATE_S" --all-exp --reason "e9 watchdog: $1"; then killed=1; break; fi
    echo "$(now) pod_down --all-exp FAILED (attempt $i of $KILL_ATTEMPTS)" | tee -a "$FAIL_ALERT"
    i=$((i + 1))
  done
  [ "$killed" = "1" ] || echo "$(now) termination NOT confirmed - retrying next minute; terminate in the console if it persists" | tee -a "$FAIL_ALERT"
}
retire() {  # retire SLOT REASON: the orchestrator's cleanup, else salvage + pod_down --slot
  if [ -f scripts/e9_orchestrate.py ]; then
    spawn "retire_$1" "$CLEANUP_S" uv run python scripts/e9_orchestrate.py cleanup --slot "$1" --reason "$2"
  else
    echo "$(now) $1: no scripts/e9_orchestrate.py; salvage then pod_down --slot $1" | tee -a "$FAIL_ALERT"
    salvage "$1"
    spawn "retire_$1" "$TERMINATE_S" bash "$POD_DOWN_SHIM" --slot "$1" --reason "e9 watchdog: $2"
  fi
}
running_slots() {  # running_slots JSON_LINE: the slots of the running e9-* pods
  printf '%s\n' "$1" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))" 2>/dev/null
}
env_slots() {
  sed -n 's/^RUNPOD_POD_ID\(_[A-Za-z0-9]\{1,8\}\)=..*$/\1/p' .env 2>/dev/null | tr '\n' ' '
}
idle_check() {  # idle_check SLOT: one consolidated ssh call; retire after IDLE_MIN without a job
  local slot=$1 host port out job since t
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] && [ -n "$port" ] || { echo "$(now) $slot: no SSH address for the idle check"; return 0; }
  out=$(bounded "$IDLE_SSH_S" ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 \
        -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new "root@$host" \
        "if pgrep -f '[p]od_run_e9[.]sh|[p]od_setup[.]sh|[s]cripts/e9_' >/dev/null; then echo E9_JOB=1; else echo E9_JOB=0; fi" \
        2>/dev/null)
  job=$(printf '%s\n' "$out" | sed -n 's/^E9_JOB=\([01]\)$/\1/p' | tail -n 1)
  [ -n "$job" ] || { echo "$(now) $slot: unreachable for the idle check (idle clock unchanged)"; return 0; }
  t=$(date +%s)
  if [ "$job" = "1" ]; then
    rm -f "$IDLE_PREFIX$slot"
    echo "$(now) $slot: job running"
    return 0
  fi
  [ -s "$IDLE_PREFIX$slot" ] || echo "$t" > "$IDLE_PREFIX$slot"
  since=$(head -n 1 "$IDLE_PREFIX$slot" | tr -dc '0-9')
  since=${since:-$t}
  echo "$(now) $slot: no job for $(( (t - since) / 60 )) min (limit $IDLE_MIN)"
  if [ $(( t - since )) -ge $(( IDLE_MIN * 60 )) ]; then
    echo "E9_WATCHDOG_IDLE $slot"
    echo "$(now) IDLE (E9): $slot without a job for >= $IDLE_MIN min: retiring it" | tee -a "$FAIL_ALERT"
    retire "$slot" idle
  fi
}
budget() {  # budget HOURS: the budget check's one JSON line (stdout); its exit code
  if [ -n "${E9_BUDGET_CMD:-}" ]; then
    bounded "$BUDGET_S" $E9_BUDGET_CMD "$1" 2>/dev/null
  else
    bounded "$BUDGET_S" uv run --quiet python -m emx.runpod --exp E9 --e9-budget-check --reserve-hours "$1" 2>/dev/null
  fi
}
FAILS=0
SALVAGED=0
echo "$(now) E9 watchdog: interval ${INTERVAL}s; the backstop reserves ${RESERVE_S}s of burn (${HOURS} h)"
while true; do
  if [ -f "$STOP" ]; then
    echo "$(now) stop file present; watchdog exits"; exit 0
  fi
  echo "$(date +%s) $INTERVAL $$" > "$HEARTBEAT"
  reap_bg
  out=$(budget "$HOURS"); rc=$?
  echo "$(now) $out"
  slots=$(running_slots "$out" || true)
  if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then
    FAILS=$((FAILS + 1))
    echo "$(now) BUDGET CHECK FAILED (rc=$rc; $FAILS in a row)" | tee -a "$FAIL_ALERT"
    if [ "$FAILS" -ge "$MAX_FAILS" ]; then
      echo "$(now) $MAX_FAILS budget-check failures in a row: failing closed" | tee -a "$FAIL_ALERT"
      [ -n "$slots" ] || slots=$(env_slots)
      kill_all failclosed "$slots" 1
      [ "$ONCE" = "1" ] && exit "$rc"
      sleep 60; continue
    fi
    [ "$ONCE" = "1" ] && exit "$rc"
    sleep "$INTERVAL"; continue
  fi
  FAILS=0
  if [ $rc -eq 2 ]; then
    salv=1
    budget "$FAST_HOURS" > /dev/null || salv=0
    kill_all "budget (E9 backstop)" "$slots" "$salv"
    if [ -f scripts/e9_orchestrate.py ]; then
      bounded "$FAST_S_CMD" uv run python scripts/e9_orchestrate.py stop \
        --rule "projected GPU spend at the E9 backstop kill line (watchdog)" --no-fetch --detail "$out" \
        || echo "$(now) e9_orchestrate.py stop FAILED; record the stop by hand" | tee -a "$FAIL_ALERT"
    else
      echo "$(now) no scripts/e9_orchestrate.py: record the backstop stop by hand" | tee -a "$FAIL_ALERT"
    fi
    [ "$ONCE" = "1" ] && exit 2
    sleep 60; continue
  fi
  SALVAGED=0
  if [ "$(date +%s)" -ge "$DEADLINE_EPOCH" ]; then   # §7.6: the 7-day paid-execution deadline
    for slot in $slots; do
      echo "E9_WATCHDOG_DEADLINE $slot"
      retire "$slot" deadline
    done
  fi
  bounded_pod_down "$TERMINATE_S" --untracked --reason "e9 watchdog reconcile" \
    || echo "$(now) reconcile (pod_down --untracked) FAILED" | tee -a "$FAIL_ALERT"
  for slot in $slots; do
    idle_check "$slot"
  done
  [ "$ONCE" = "1" ] && exit 0
  sleep "$INTERVAL"
done
