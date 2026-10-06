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
# - budget: pod_budget_check.py --exp E3 projects logged GPU $ + every running e3-* pod + the
#   whole bounded shutdown latency of their burn (RESERVE_S, fix A3) against the kill line min($35, $45 - reserve - recorded and reserved
#   API spend) and the combined guard. At a hard stop (exit 2), and FAILING CLOSED on any other
#   non-zero exit, it salvages each running slot's results/E3 and logs/ (one ssh tar per slot, all
#   bounded together at E3_SALVAGE_S=240 s, local MANIFEST.sha256; skipped when it no longer fits
#   under the kill line, fix A3), terminates EVERY e3-* pod the
#   API lists (pod_down.py --exp E3 --all-exp) whatever the salvage did, then runs the
#   unconditional cleanup path for each of those slots (e3_orchestrate.py cleanup --slot S
#   --reason ..., which records what is missing and settles the reservations). It keeps
#   monitoring, retrying every minute;
# - the 5-day wall-clock cap (contracts.DEADLINE_UTC): once passed, `e3_orchestrate.py
#   deadline-check` stops E3 and cleans up every running slot (E3_WATCHDOG_DEADLINE <slot>);
# - reconciles: terminates e3-* pods missing from the event log (pod_down.py --exp E3 --untracked);
# - idle: one consolidated ssh call per running slot asks whether a job runs (bracketed pgrep for
#   pod_run_e3.sh, pod_setup.sh or a scripts/e3_* process). A slot without one for
#   contracts.IDLE_POD_MINUTES (state in logs/e3_idle/<slot>, so a restart keeps the clock) prints
#   `E3_WATCHDOG_IDLE <slot>` and runs e3_orchestrate.py cleanup --slot <slot> --reason idle. An
#   unreachable pod neither starts nor resets the idle clock.
# 2026-10-06, fixes after gpt-6-astra code review A r1 (logs/reviews/20261006-E3-code-A-r1.md):
# - A2: every orchestrator, pod_down, budget-check and ssh call is bounded (`bounded SECS cmd`
#   runs it in its own process group and kills the group at the deadline, exit 124). recover
#   and the idle cleanups run in the background (one at a time per kind; reaped each tick), so
#   one hung cleanup never blocks the budget check or the supervision of other pods; recover is
#   killed after E3_RECOVER_S and then counts as a failure.
# - A3: the budget check reserves the whole bounded shutdown latency, not one interval:
#   INTERVAL + the rest of a tick (budget check, reconcile, one idle ssh per pod slot) + salvage
#   + termination (RESERVE_S below). At a hard stop a second check, reserving only salvage +
#   termination, decides whether salvage still fits under the kill line; if not, it terminates
#   at once and skips the salvage.
# - A8: a budget hard stop is a §7 stop: after terminating, cleanup runs without retrieval and
#   `e3_orchestrate.py stop` writes results/E3/STOPPED.json. A fail-closed stop (broken budget
#   check or supervision) is an infrastructure failure: it terminates and cleans up but does
#   not write STOPPED.json. At the 5-day cap it runs `e3_orchestrate.py deadline-check`.
# 2026-10-06, review A r2 (logs/reviews/20261006-E3-code-A-r2.md):
# - 1: every tick first starts `e3_orchestrate.py enforce` (bounded, in the background, one at a
#   time): every unarmed or overdue pod is terminated without retrieval, independently of any
#   pull in progress; recover hands retrieval-bearing cleanups to one background process per slot.
# - 2: the emergency salvage before the DELETE is ONE bounded transfer (all slots in one process
#   group, killed at E3_SALVAGE_S); extraction, listing and hashing run after the DELETE
#   (bounded at E3_SALVAGE_POST_S). The reserved margin counts the kill grace of every bounded
#   call (GRACE_S) and all three budget-check latencies (the firing check, the previous tick's,
#   and the salvage-margin check).
# 2026-10-06, review A r3 (logs/reviews/20261006-E3-code-A-r3.md), finding 2: at a hard stop a
#   failed or timed-out DELETE is retried at once (E3_KILL_ATTEMPTS calls, all inside the
#   reserved margin); the salvage post-processing runs only once termination is confirmed;
#   otherwise its archives are kept unprocessed and the next pass retries termination first.
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
# bounds (seconds) of every blocking call, and the shutdown latency the kill line reserves (A3)
BUDGET_S=${E3_BUDGET_S:-90}          # one pod_budget_check.py call
TERMINATE_S=${E3_TERMINATE_S:-150}   # one pod_down.py call (--all-exp, --untracked)
IDLE_SSH_S=${E3_IDLE_SSH_S:-45}      # one idle-check ssh call
CLEANUP_FAST_S=${E3_CLEANUP_FAST_S:-600}   # a cleanup without retrieval, or the stop command
CLEANUP_S=${E3_CLEANUP_S:-3600}      # a cleanup with retrieval (3 bounded pulls + termination)
RECOVER_S=${E3_RECOVER_S:-5400}      # one recover pass, or deadline-check (several slots)
ENFORCE_S=${E3_ENFORCE_S:-1200}      # one enforce pass (bounded pod_down calls, no retrieval)
MAX_SLOTS=3                          # pods 1-3
SALVAGE_S=${E3_SALVAGE_S:-240}       # the whole pre-DELETE salvage transfer, every slot together
SALVAGE_POST_S=${E3_SALVAGE_POST_S:-600}  # extraction, listing, hashing (after the DELETE)
GRACE_S=${E3_GRACE_S:-10}            # a bounded call's kill grace (SIGTERM, 5 s, SIGKILL) + start-up
KILL_ATTEMPTS=${E3_KILL_ATTEMPTS:-2}  # pod_down --all-exp calls at a hard stop before anything else
# from a projection just under the kill line to the DELETE: the rest of that check, the
# reconcile and idle checks of its tick, the interval, the next check (which fires), the
# salvage-margin check, the salvage and every termination attempt
RESERVE_S=$(( INTERVAL + 3 * (BUDGET_S + GRACE_S) + (TERMINATE_S + GRACE_S) \
  + MAX_SLOTS * (IDLE_SSH_S + GRACE_S) + (SALVAGE_S + GRACE_S) \
  + KILL_ATTEMPTS * (TERMINATE_S + GRACE_S) ))
FAST_S=$(( (BUDGET_S + GRACE_S) + (SALVAGE_S + GRACE_S) + KILL_ATTEMPTS * (TERMINATE_S + GRACE_S) ))
HOURS=$(python3 -c "print($RESERVE_S/3600)")
FAST_HOURS=$(python3 -c "print($FAST_S/3600)")
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
BG=""   # bounded background jobs, "pid:tag" (recover, deadline, idle_<slot>)
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

# bounded SECS CMD...: run CMD in its own process group; kill the group after SECS (exit 124)
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
# spawn TAG SECS CMD...: a bounded background job (in the foreground when E3_WATCHDOG_ONCE=1, so
# a test tick is deterministic); at most one per TAG; its exit code reaches on_done when reaped
spawn() {
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
reap_bg() {  # collect finished background jobs
  local j p rc keep=""
  for j in $BG; do
    p=${j%%:*}
    if kill -0 "$p" 2>/dev/null; then keep="$keep $j"; continue; fi
    wait "$p"; rc=$?
    on_done "${j#*:}" "$rc"
  done
  BG=$keep
}
RECOVER_FAILS=0
on_done() {  # on_done TAG RC
  case "$1" in
    recover)
      if [ "$2" = "0" ]; then RECOVER_FAILS=0; else
        RECOVER_FAILS=$((RECOVER_FAILS + 1))
        echo "$(now) RECOVER FAILED rc=$2 ($RECOVER_FAILS in a row)" | tee -a "$FAIL_ALERT"
      fi ;;
    idle_*)
      if [ "$2" = "0" ]; then rm -f "${IDLE_DIR:?}/${1#idle_}"; else
        echo "$(now) cleanup ${1#idle_} (idle) FAILED rc=$2; retried next tick" | tee -a "$FAIL_ALERT"
      fi ;;
    *) [ "$2" = "0" ] || echo "$(now) $1 FAILED rc=$2" | tee -a "$FAIL_ALERT" ;;
  esac
}
SALVAGED=0
SALVAGE_DIRS=""
salvage() {  # salvage "SLOTS": ONE bounded transfer of every slot (its own process group, killed
  # at SALVAGE_S); nothing else runs before the DELETE (review A r2, 2)
  local slot host port d script=""
  SALVAGE_DIRS=""
  for slot in $1; do
    host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
    [ -n "$host" ] && [ -n "$port" ] || { echo "$(now) salvage $slot: no host in .env"; continue; }
    d="results/E3/sealed/_salvage_${slot}_$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$d"
    script="$script( ssh -n -p $(printf %q "$port") -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new $(printf %q "root@$host") $(printf %q "cd /ws/emx && tar cf - --exclude='*.safetensors' results/E3 logs 2>/dev/null; exit 0") > $(printf %q "$d.tar") 2>/dev/null; echo \$? > $(printf %q "$d.rc") ) &
"
    SALVAGE_DIRS="$SALVAGE_DIRS $d"
    echo "$(now) salvage $slot -> $d (all slots bounded at ${SALVAGE_S}s)"
  done
  [ -n "$script" ] || return 0
  bounded "$SALVAGE_S" bash -c "${script}wait" \
    || echo "$(now) salvage transfer cut at ${SALVAGE_S}s; terminating anyway" | tee -a "$FAIL_ALERT"
}
# after the DELETE: extract each transfer, then list and hash it (bounded together)
FINISH_SH='for d in "$@"; do
  if [ "$(cat "$d.rc" 2>/dev/null)" = "0" ] && tar xf "$d.tar" -C "$d" 2>/dev/null; then
    rm -f -- "$d.tar" "$d.rc"
  else
    echo "salvage $d: transfer incomplete (kept $d.tar)"
    tar xf "$d.tar" -C "$d" 2>/dev/null || true
  fi
  ( cd "$d" && find . -type f ! -name MANIFEST.sha256 | sed "s|^\./||" | LC_ALL=C sort \
      | while read -r f; do shasum -a 256 "$f"; done ) > "$d/MANIFEST.sha256"
  echo "salvage $d: $(wc -l < "$d/MANIFEST.sha256" | tr -d " ") file(s), local MANIFEST.sha256"
done'
salvage_finish() {
  [ -n "$SALVAGE_DIRS" ] || return 0
  # shellcheck disable=SC2086 # the dirs carry no spaces
  bounded "$SALVAGE_POST_S" bash -c "$FINISH_SH" finish $SALVAGE_DIRS \
    || echo "$(now) salvage post-processing cut at ${SALVAGE_POST_S}s (the .tar files are kept)" | tee -a "$FAIL_ALERT"
  SALVAGE_DIRS=""
}
kill_all() {  # kill_all REASON SLOTS SALVAGE(1|0): bounded salvage (once per stop) if it fits,
  # terminate, then the cleanup path without retrieval (the pods are gone; it settles, releases)
  local s
  echo "$(now) HARD STOP ($1): terminating every e3-* pod"
  if [ "$3" != "1" ]; then
    echo "$(now) no margin for salvage under the kill line: terminating at once" | tee -a "$FAIL_ALERT"
  elif [ "$SALVAGED" = "0" ]; then
    SALVAGED=1
    salvage "$2" || echo "$(now) salvage failed; terminating anyway"
  fi
  local i=1 killed=0
  while [ "$i" -le "$KILL_ATTEMPTS" ]; do
    if bounded "$TERMINATE_S" uv run python scripts/pod_down.py --exp E3 --all-exp --reason "e3 watchdog: $1"; then
      killed=1; break
    fi
    echo "$(now) pod_down --all-exp FAILED (attempt $i of $KILL_ATTEMPTS)" | tee -a "$FAIL_ALERT"
    i=$((i + 1))
  done
  if [ "$killed" = "1" ]; then
    salvage_finish   # only once termination is confirmed (review A r3, 2)
  else
    echo "$(now) termination NOT confirmed - retrying next minute; terminate in the console if it persists; salvage archives kept unprocessed:$SALVAGE_DIRS" | tee -a "$FAIL_ALERT"
  fi
  for s in $2; do
    bounded "$CLEANUP_FAST_S" uv run python scripts/e3_orchestrate.py cleanup --slot "$s" --reason "$1" --no-retrieve \
      || echo "$(now) cleanup $s FAILED (rc=$?)" | tee -a "$FAIL_ALERT"
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
  out=$(bounded "$IDLE_SSH_S" ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 \
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
    spawn "idle_$slot" "$CLEANUP_S" uv run python scripts/e3_orchestrate.py cleanup --slot "$slot" --reason idle
  fi
}
budget() {  # budget HOURS: the budget check's one JSON line (stdout); its exit code
  bounded "$BUDGET_S" uv run python scripts/pod_budget_check.py --exp E3 --reserve-hours "$1" 2>/dev/null
}
echo "$(now) E3 watchdog: interval ${INTERVAL}s; the kill line reserves ${RESERVE_S}s of burn (${HOURS} h)"
while true; do
  if [ -f "$STOP" ]; then
    echo "$(now) stop file present; watchdog exits"; exit 0
  fi
  echo "$(date +%s) $INTERVAL $$" > "$HEARTBEAT"
  reap_bg
  # 1. deadline enforcement, every tick, independent of any retrieval (review A r2, 1); then
  # the independent supervisor (plan v4 fix 2). Both bounded, in the background.
  spawn enforce "$ENFORCE_S" uv run python scripts/e3_orchestrate.py enforce
  spawn recover "$RECOVER_S" uv run python scripts/e3_orchestrate.py recover
  # 2. budget; stdout = one JSON line (stderr kept separate); the whole shutdown latency reserved
  out=$(budget "$HOURS"); rc=$?
  echo "$(now) $out"
  slots=$(running_slots "$out" || true)
  if [ "$RECOVER_FAILS" -ge 3 ] && [ "$rc" = "0" ]; then
    rc=4   # unsupervised pods: fail closed like a broken budget check
    echo "$(now) recover failed 3 times in a row: failing closed" | tee -a "$FAIL_ALERT"
  fi
  if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then
    echo "$(now) BUDGET CHECK OR SUPERVISION FAILED (rc=$rc): failing closed" | tee -a "$FAIL_ALERT"
    [ -n "$slots" ] || slots=$(env_slots)
    kill_all failclosed "$slots" 1
    [ "$ONCE" = "1" ] && exit "$rc"
    sleep 60; continue
  fi
  if [ $rc -eq 2 ]; then
    # does the salvage still fit under the kill line? (only salvage + termination reserved)
    salv=1
    budget "$FAST_HOURS" > /dev/null || salv=0
    kill_all "budget" "$slots" "$salv"
    bounded "$CLEANUP_FAST_S" uv run python scripts/e3_orchestrate.py stop \
      --rule "projected spend above a limit (watchdog GPU kill line)" --no-retrieve --detail "$out" \
      || echo "$(now) e3_orchestrate.py stop FAILED" | tee -a "$FAIL_ALERT"
    [ "$ONCE" = "1" ] && exit 2
    sleep 60; continue   # keep monitoring until no pod is running
  fi
  SALVAGED=0
  # 3. the 5-day wall-clock cap (§7): a terminal stop that cleans up every running slot
  if [ "$(date +%s)" -ge "$DEADLINE_EPOCH" ]; then
    for slot in $slots; do echo "E3_WATCHDOG_DEADLINE $slot"; done
    spawn deadline "$RECOVER_S" uv run python scripts/e3_orchestrate.py deadline-check
  fi
  # 4. reconcile, then idle checks on every running slot
  bounded "$TERMINATE_S" uv run python scripts/pod_down.py --exp E3 --untracked --reason "e3 watchdog reconcile" \
    || echo "$(now) reconcile (pod_down --untracked) FAILED" | tee -a "$FAIL_ALERT"
  for slot in $slots; do
    idle_check "$slot"
  done
  [ "$ONCE" = "1" ] && exit 0
  sleep "$INTERVAL"
done
