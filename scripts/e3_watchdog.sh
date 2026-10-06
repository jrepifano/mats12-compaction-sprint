#!/usr/bin/env bash
# Laptop-side supervisor and GPU hard stop for E3 (prereg §7, §8.6; implementation plan U8c, v2
# fix 8, v3 fix 5, v4 fix 2). A copy of scripts/e1b_watchdog.sh (itself a copy of
# i1a_watchdog.sh) with every call passing --exp E3 (the ambient environment is never trusted).
# v4 makes it the INDEPENDENT supervisor of unarmed pods: it is started, and checked alive, before
# any E3 pod is created (e3_orchestrate.py launch calls `--check-alive`), and it does not depend on
# the orchestrator still running. Each tick (INTERVAL_S, at most 120 s, default 120):
# - writes a heartbeat (logs/watchdog_E3.heartbeat: "<epoch> <interval> <pid>", what
#   emx.runpod.watchdog_live reads) and owns the pidfile logs/e3_watchdog.pid (one watchdog at a
#   time: a second one refuses to start while the first is alive);
# - runs `e3_orchestrate.py recover`, which terminates and settles any E3 pod (found through the
#   API, or a `creating` reservation row without a pod) still unacknowledged 15 minutes after its
#   t0 or past its budget deadline. A recover failure is logged to the alert file; 3 failures in a
#   row fail closed (hard stop below), since an unsupervised unarmed pod is what this guards;
# - budget: pod_budget_check.py --exp E3 projects logged GPU $ + every running e3-* pod + one
#   interval of their burn against the kill line min($35, $45 - reserve - recorded and reserved
#   API spend) and the combined guard. At a hard stop (exit 2), and FAILING CLOSED on any other
#   non-zero exit, it salvages each running slot's results/E3 and logs/ (one ssh tar per slot, all
#   bounded together at E3_SALVAGE_S=240 s, local MANIFEST.sha256), terminates EVERY e3-* pod the
#   API lists (pod_down.py --exp E3 --all-exp) whatever the salvage did, then runs the
#   unconditional cleanup path for each of those slots (e3_orchestrate.py cleanup --slot S
#   --reason ..., which records what is missing and settles the reservations). It keeps
#   monitoring, retrying every minute;
# - the 5-day wall-clock cap (contracts.DEADLINE_UTC): once passed, every running slot goes
#   through e3_orchestrate.py cleanup --reason deadline (E3_WATCHDOG_DEADLINE <slot>);
# - reconciles: terminates e3-* pods missing from the event log (pod_down.py --exp E3 --untracked);
# - idle: one consolidated ssh call per running slot asks whether a job runs (bracketed pgrep for
#   pod_run_e3.sh, pod_setup.sh or a scripts/e3_* process). A slot without one for
#   contracts.IDLE_POD_MINUTES (state in logs/e3_idle/<slot>, so a restart keeps the clock) prints
#   `E3_WATCHDOG_IDLE <slot>` and runs e3_orchestrate.py cleanup --slot <slot> --reason idle. An
#   unreachable pod neither starts nor resets the idle clock.
# macOS bash 3.2: no associative arrays, no ${x,,}, no mapfile.
#   nohup bash scripts/e3_watchdog.sh [INTERVAL_S] > logs/watchdog_E3.log 2>&1 &
#   bash scripts/e3_watchdog.sh --check-alive   # exit 0 iff a live watchdog owns the pidfile
#   touch logs/watchdog_E3.stop                 # when E3's pod work is over
# E3_WATCHDOG_ONCE=1 runs a single tick and exits with the budget check's code (tests).
set -uo pipefail
cd "$(dirname "$0")/.."
PIDFILE=${E3_WATCHDOG_PIDFILE:-logs/e3_watchdog.pid}
HEARTBEAT=${E3_WATCHDOG_HEARTBEAT:-logs/watchdog_E3.heartbeat}
STOP=${E3_WATCHDOG_STOP:-logs/watchdog_E3.stop}
FAIL_ALERT=${E3_WATCHDOG_ALERT:-logs/watchdog_E3.alert}
IDLE_DIR=${E3_IDLE_DIR:-logs/e3_idle}
ONCE=${E3_WATCHDOG_ONCE:-0}
MAX_INTERVAL=120
now() { date -u +%FT%TZ; }

# alive: 0 iff the pidfile names a running e3_watchdog.sh whose heartbeat is fresh
alive() {
  local pid cmd hb_t hb_i hb_pid age
  [ -s "$PIDFILE" ] || { echo "no pidfile $PIDFILE"; return 1; }
  pid=$(head -n 1 "$PIDFILE" | tr -d ' \n')
  case "$pid" in ""|*[!0-9]*) echo "malformed pidfile $PIDFILE"; return 1 ;; esac
  kill -0 "$pid" 2>/dev/null || { echo "pid $pid (pidfile) is not running"; return 1; }
  cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
  case "$cmd" in
    *e3_watchdog.sh*) ;;
    *) echo "pid $pid is not an e3_watchdog.sh ($cmd)"; return 1 ;;
  esac
  [ -s "$HEARTBEAT" ] || { echo "no heartbeat $HEARTBEAT"; return 1; }
  read -r hb_t hb_i hb_pid < "$HEARTBEAT" || true
  case "$hb_t$hb_i$hb_pid" in ""|*[!0-9]*) echo "malformed heartbeat"; return 1 ;; esac
  [ "$hb_pid" = "$pid" ] || { echo "heartbeat pid $hb_pid != pidfile pid $pid"; return 1; }
  age=$(( $(date +%s) - hb_t ))
  [ "$age" -le $(( 2 * hb_i + 60 )) ] || { echo "heartbeat ${age}s old"; return 1; }
  echo "e3 watchdog alive (pid $pid, heartbeat ${age}s old)"
  return 0
}
if [ "${1:-}" = "--check-alive" ]; then
  alive; exit $?
fi

INTERVAL=${1:-$MAX_INTERVAL}
case "$INTERVAL" in ""|*[!0-9]*) echo "INTERVAL_S must be whole seconds" >&2; exit 2 ;; esac
if [ "$INTERVAL" -lt 1 ] || [ "$INTERVAL" -gt "$MAX_INTERVAL" ]; then
  echo "INTERVAL_S must be 1..$MAX_INTERVAL (plan v4: recover runs at least every 2 minutes)" >&2
  exit 2
fi
HOURS=$(python3 -c "print($INTERVAL/3600)")
# frozen values from src/emx/e3/contracts.py (loaded by path; no default: refuse to run without)
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
C_VALS=$("$PY" - src/emx/e3/contracts.py <<'PY_E3'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("e3_contracts", sys.argv[1])
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
print(C.IDLE_POD_MINUTES, int(C.DEADLINE_UTC.timestamp()))
PY_E3
) || { echo "could not read src/emx/e3/contracts.py; refusing to start" >&2; exit 2; }
IDLE_MIN=${E3_IDLE_MINUTES:-${C_VALS%% *}}
DEADLINE_EPOCH=${E3_DEADLINE_EPOCH:-${C_VALS##* }}
mkdir -p logs "$(dirname "$PIDFILE")" "$(dirname "$HEARTBEAT")" "$IDLE_DIR"
if other=$(alive); then
  echo "$(now) another E3 watchdog is running ($other); refusing to start" >&2
  exit 3
fi
echo "$$" > "$PIDFILE"
release() {  # remove the pidfile and heartbeat only if they are still ours
  [ "$(head -n 1 "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE" "$HEARTBEAT"
  return 0
}
trap release EXIT
trap 'exit 143' TERM INT   # so that the EXIT trap runs on a kill
export WANDB_MODE=offline
rm -f "$STOP"
envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }

SALVAGE_S=${E3_SALVAGE_S:-240}   # one wall-clock bound for the salvage of every slot together
SALVAGED=0
salvage() {  # salvage "SLOTS": bounded copy of results/E3 and logs off each pod
  local slot host port d jobs="" j p t=0 alive_n n rc
  for slot in $1; do
    host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
    [ -n "$host" ] && [ -n "$port" ] || { echo "$(now) salvage $slot: no host in .env"; continue; }
    d="results/E3/sealed/_salvage_${slot}_$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$d"
    ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 \
      -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new "root@$host" \
      "cd /ws/emx && tar cf - --exclude='*.safetensors' results/E3 logs 2>/dev/null; exit 0" \
      > "$d.tar" 2>/dev/null &
    jobs="$jobs $!:$d"
    echo "$(now) salvage $slot -> $d (bounded at ${SALVAGE_S}s)"
  done
  while [ "$t" -lt "$SALVAGE_S" ]; do
    alive_n=0
    for j in $jobs; do kill -0 "${j%%:*}" 2>/dev/null && alive_n=1; done
    [ "$alive_n" = "0" ] && break
    sleep 1; t=$((t + 1))
  done
  for j in $jobs; do
    p=${j%%:*}; d=${j#*:}; rc=0
    if kill -0 "$p" 2>/dev/null; then kill "$p" 2>/dev/null; rc=124; fi
    wait "$p" 2>/dev/null || [ "$rc" != "0" ] || rc=1
    if tar xf "$d.tar" -C "$d" 2>/dev/null && [ "$rc" = "0" ]; then rm -f "$d.tar"; else
      echo "$(now) salvage $d: transfer incomplete (rc=$rc; kept $d.tar)" | tee -a "$FAIL_ALERT"
    fi
    ( cd "$d" && find . -type f ! -name MANIFEST.sha256 | sed 's|^\./||' | LC_ALL=C sort \
        | while read -r f; do shasum -a 256 "$f"; done ) > "$d/MANIFEST.sha256"
    n=$(wc -l < "$d/MANIFEST.sha256" | tr -d ' ')
    echo "$(now) salvage $d: $n file(s), local MANIFEST.sha256"
  done
}
cleanup_slot() {  # cleanup_slot SLOT REASON (idle|deadline|budget|failclosed): the unconditional cleanup path
  uv run python scripts/e3_orchestrate.py cleanup --slot "$1" --reason "$2"
}
kill_all() {  # kill_all REASON SLOTS: bounded salvage (once per stop), terminate, then clean up
  local s
  echo "$(now) HARD STOP ($1): terminating every e3-* pod"
  if [ "$SALVAGED" = "0" ]; then
    SALVAGED=1
    salvage "$2" || echo "$(now) salvage failed; terminating anyway"
  fi
  uv run python scripts/pod_down.py --exp E3 --all-exp --reason "e3 watchdog: $1" \
    || echo "$(now) pod_down FAILED - retrying next minute; terminate in the console if it persists" | tee -a "$FAIL_ALERT"
  for s in $2; do
    cleanup_slot "$s" "$1" || echo "$(now) cleanup $s FAILED (rc=$?)" | tee -a "$FAIL_ALERT"
  done
}
env_slots() {  # the slots holding a pod in .env (fallback when the budget check gave no list)
  sed -n 's/^RUNPOD_POD_ID\(_[A-Za-z0-9]\{1,8\}\)=..*$/\1/p' .env 2>/dev/null | tr '\n' ' '
}
running_slots() {  # running_slots BUDGET_JSON: the slots of the running e3-* pods
  printf '%s\n' "$1" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))" 2>/dev/null
}
idle_check() {  # idle_check SLOT: one consolidated ssh call; cleanup after IDLE_MIN without a job
  local slot=$1 host port out job since t
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] && [ -n "$port" ] || { echo "$(now) $slot: no SSH address for the idle check"; return 0; }
  out=$(ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 \
        -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new "root@$host" \
        "if pgrep -f '[p]od_run_e3[.]sh|[p]od_setup[.]sh|[s]cripts/e3_' >/dev/null; then echo E3_JOB=1; else echo E3_JOB=0; fi" \
        2>/dev/null)
  job=$(printf '%s\n' "$out" | sed -n 's/^E3_JOB=\([01]\)$/\1/p' | tail -n 1)
  [ -n "$job" ] || { echo "$(now) $slot: unreachable for the idle check (idle clock unchanged)"; return 0; }
  t=$(date +%s)
  if [ "$job" = "1" ]; then
    rm -f "$IDLE_DIR/$slot"
    echo "$(now) $slot: job running"
    return 0
  fi
  [ -s "$IDLE_DIR/$slot" ] || echo "$t" > "$IDLE_DIR/$slot"
  since=$(head -n 1 "$IDLE_DIR/$slot" | tr -dc '0-9')
  since=${since:-$t}
  echo "$(now) $slot: no job for $(( (t - since) / 60 )) min (limit $IDLE_MIN)"
  if [ $(( t - since )) -ge $(( IDLE_MIN * 60 )) ]; then
    echo "E3_WATCHDOG_IDLE $slot"
    echo "$(now) IDLE (E3): $slot without a job for >= $IDLE_MIN min: cleanup" | tee -a "$FAIL_ALERT"
    if cleanup_slot "$slot" idle; then rm -f "$IDLE_DIR/$slot"; else
      echo "$(now) cleanup $slot (idle) FAILED; retried next tick" | tee -a "$FAIL_ALERT"
    fi
  fi
}
RECOVER_FAILS=0
while true; do
  if [ -f "$STOP" ]; then
    echo "$(now) stop file present; watchdog exits"; exit 0
  fi
  echo "$(date +%s) $INTERVAL $$" > "$HEARTBEAT"
  # 1. the independent supervisor of unarmed pods (plan v4 fix 2)
  if uv run python scripts/e3_orchestrate.py recover; then
    RECOVER_FAILS=0
  else
    RECOVER_FAILS=$((RECOVER_FAILS + 1))
    echo "$(now) RECOVER FAILED ($RECOVER_FAILS in a row)" | tee -a "$FAIL_ALERT"
  fi
  # 2. budget; stdout = one JSON line (stderr kept separate); one interval of burn reserved
  out=$(uv run python scripts/pod_budget_check.py --exp E3 --reserve-hours "$HOURS" 2>/dev/null); rc=$?
  echo "$(now) $out"
  slots=$(running_slots "$out" || true)
  if [ "$RECOVER_FAILS" -ge 3 ] && [ "$rc" = "0" ]; then
    rc=4   # unsupervised pods: fail closed like a broken budget check
    echo "$(now) recover failed 3 times in a row: failing closed" | tee -a "$FAIL_ALERT"
  fi
  if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then
    echo "$(now) BUDGET CHECK OR SUPERVISION FAILED (rc=$rc): failing closed" | tee -a "$FAIL_ALERT"
    [ -n "$slots" ] || slots=$(env_slots)
    kill_all failclosed "$slots"
    [ "$ONCE" = "1" ] && exit "$rc"
    sleep 60; continue
  fi
  if [ $rc -eq 2 ]; then
    kill_all "budget" "$slots"
    [ "$ONCE" = "1" ] && exit 2
    sleep 60; continue   # keep monitoring until no pod is running
  fi
  SALVAGED=0
  # 3. the 5-day wall-clock cap (§7)
  if [ "$(date +%s)" -ge "$DEADLINE_EPOCH" ]; then
    for slot in $slots; do
      echo "E3_WATCHDOG_DEADLINE $slot"
      cleanup_slot "$slot" deadline || echo "$(now) cleanup $slot (deadline) FAILED" | tee -a "$FAIL_ALERT"
    done
  fi
  # 4. reconcile, then idle checks on every running slot
  uv run python scripts/pod_down.py --exp E3 --untracked --reason "e3 watchdog reconcile" \
    || echo "$(now) reconcile (pod_down --untracked) FAILED" | tee -a "$FAIL_ALERT"
  for slot in $slots; do
    idle_check "$slot"
  done
  [ "$ONCE" = "1" ] && exit 0
  sleep "$INTERVAL"
done
