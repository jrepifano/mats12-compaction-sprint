#!/usr/bin/env bash
# Laptop-side GPU hard stop and pod-life salvage for E11 (E11 implementation plan, unit U1;
# implementation readings item 4). A copy of scripts/eq0_watchdog.sh (2026-10-08) with every call
# passing --exp E11 (the ambient environment is never trusted) and one slot (_A):
# - each tick first writes a heartbeat (logs/watchdog_E11.heartbeat, emx.e11.contracts
#   WATCHDOG_HEARTBEAT: "<epoch> <interval> <pid>"); pod_up.py --exp E11 refuses to launch unless
#   it is younger than two intervals and the pid is alive;
# - budget: pod_budget_check.py --exp E11 projects logged GPU $ + every running e11-* pod (billed
#   from creation, logs/pods_E11.jsonl) + one interval of their burn against the E11 GPU line,
#   min($20, $30 - API $ recorded and reserved) (emx.spend.CAPS["E11"]), and the combined guard;
# - at a hard stop (exit 2) it first salvages the running slot's results/E11 and logs/ into
#   results/E11/_salvage_<slot>_<UTC>/ (one ssh tar, bounded at E11_SALVAGE_S=240 s, with a local
#   MANIFEST.sha256; once per stop), then terminates EVERY e11-* pod the API lists
#   (pod_down.py --exp E11 --all-exp) whatever the salvage did, and keeps monitoring, retrying
#   every minute (as EQ0's);
# - FAILS CLOSED: any other budget-check exit (malformed ledger, crash) writes an alert
#   (logs/watchdog_E11.alert) and terminates every e11-* pod too (after the same salvage of _A);
# - reconciles: each healthy tick terminates e11-* pods missing from the event log
#   (pod_down.py --exp E11 --untracked; a pod pod_up is creating right now is spared briefly);
# - one consolidated ssh call per running slot per tick (CLAUDE.md: several watchers polling
#   every minute trip sshd's MaxStartups) reads the GPU utilisation, the last anchored progress tag
#   (^E11_) of the newest run log (logs/e11/run_<UTC>.log) and the names of the pod's top-level
#   results/E11/MANIFEST.*.sha256. E11 additions:
#   (a) a manifest on the pod that is not yet in the local results/E11/ is pulled with
#       `pod_pull_results.sh --exp E11 --slot _A --manifest <name>` (verified, never terminating;
#       MANIFEST.recovery.sha256 is skipped: it is rewritten all the time and pulled at salvage);
#       a failed pull is retried on the next tick;
#   (b) SALVAGE AT POD AGE >= contracts.SALVAGE_AGE_H (4.5 h; E11_SALVAGE_AGE_H overrides it for
#       tests), age from the pod's earliest created/running event in logs/pods_E11.jsonl, ahead of
#       the pod-side killswitch at creation + contracts.POD_LIFE_H (pod_push.sh). Separate
#       commands, each bounded by the time left before (creation + POD_LIFE_H - 60 s), never a
#       fetch and a terminate in one compound command: 0. quiesce: stop the chain
#       (logs/e11/chain.pid: its children, itself, its process group) and every e11_pod.py
#       process (bracketed patterns; TERM, up to 60 s, then KILL), so no writer changes a file
#       between hashing and the pull (code review r1a finding 2); 1. write the recovery manifest
#       on the pod (/ws/venv-peft/bin/python scripts/e11_pod.py recovery); 2. pull it
#       (--manifest MANIFEST.recovery.sha256); 3. tar-pull the activations (--tar-acts) and
#       4. the adapters (--tar-adapters); 5. pod_down.py --exp E11 --slot _A, whatever 0-4 did (a
#       failure is an alert). Once per pod; a failed pod_down is retried every minute.
#   Polling is bounded (code review r1a finding 3): the status ssh has ConnectTimeout and an
#   overall bound (E11_POLL_SSH_S, 60 s), never past the salvage age; the pod age is rechecked
#   before each manifest pull, each pull is bounded by min(900 s, the time left before the
#   salvage age), and once the age is reached polling stops and the salvage starts in the same
#   tick. The tick's sleep never runs past the salvage age.
#   (c) the GPU-cap kill is EQ0's (above).
# Idle GPUs only raise an alert (logs/idle_E11.alert), never a kill: a model download looks
# idle from outside. macOS bash 3.2: no associative arrays, no case-changing expansions, no
# builtin array readers.
#   nohup bash scripts/e11_watchdog.sh [INTERVAL_S] > logs/watchdog_E11.log 2>&1 &
#   touch logs/watchdog_E11.stop     # when E11's pod work is over (removes the heartbeat)
# The default interval is 300 s (EQ0's 600 s would let the salvage start as late as 4 h 40 min).
# E11_WATCHDOG_ONCE=1 runs a single tick and exits with the budget check's code (tests).
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL=${1:-300}
HOURS=$(python3 -c "print($INTERVAL/3600)")
STOP=${E11_WATCHDOG_STOP:-logs/watchdog_E11.stop}
ALERT=${E11_IDLE_ALERT:-logs/idle_E11.alert}
FAIL_ALERT=${E11_WATCHDOG_ALERT:-logs/watchdog_E11.alert}
HEARTBEAT=${E11_WATCHDOG_HEARTBEAT:-logs/watchdog_E11.heartbeat}
ONCE=${E11_WATCHDOG_ONCE:-0}
IDLE_TICKS=${IDLE_TICKS:-3}   # consecutive ticks at 0% GPU before an idle alert
EVENTS="${EMX_POD_LOG_DIR:-logs}/pods_E11.jsonl"   # as emx.runpod reads it
PULL="scripts/pod_pull_results.sh"
export WANDB_MODE=offline
mkdir -p logs
# frozen values from src/emx/e11/contracts.py (stdlib only); the watchdog refuses to start
# without them, so no heartbeat is ever written and pod_up refuses to launch
CVALS=$(python3 -c 'import sys; sys.path.insert(0, "src"); from emx.e11 import contracts as C; print(C.SALVAGE_AGE_H, C.POD_LIFE_H)') \
  || { echo "could not read src/emx/e11/contracts.py; watchdog NOT started" >&2; exit 1; }
SALVAGE_AGE_H=${E11_SALVAGE_AGE_H:-${CVALS% *}}
POD_LIFE_H=${CVALS#* }
SALVAGE_AGE_S=$(python3 -c "print(int(float('$SALVAGE_AGE_H') * 3600))") \
  || { echo "bad salvage age '$SALVAGE_AGE_H'; watchdog NOT started" >&2; exit 1; }
LIFE_S=$(python3 -c "print(int(float('$POD_LIFE_H') * 3600))")
rm -f "$STOP"
now() { date -u +%FT%TZ; }
envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }
# bounded SECONDS CMD...: run CMD in its own process group; after SECONDS the whole group gets
# TERM, then KILL 2 s later, and the call returns 124 (no GNU timeout on macOS). Killing the
# group, not just CMD, also stops a pull's ssh/rsync children and frees a captured stdout.
bounded() {
  local t=$1; shift
  perl -e '
    my $t = shift; my $pid = fork; defined $pid or die "fork: $!";
    if (!$pid) { setpgrp(0, 0); exec @ARGV or exit 127 }
    $SIG{ALRM} = sub { kill "TERM", -$pid; sleep 2; kill "KILL", -$pid; waitpid($pid, 0); exit 124 };
    alarm $t; waitpid($pid, 0); alarm 0;
    exit(($? & 127) ? 128 + ($? & 127) : $? >> 8);' "$t" "$@"
}
SALVAGE_S=${E11_SALVAGE_S:-240}   # one wall-clock bound for the hard-stop salvage of every slot
POLL_SSH_S=${E11_POLL_SSH_S:-60}  # overall bound of the status ssh
SSHO="-o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new"
# step 0 of the pod-life salvage, run on the pod: stop the chain and its children, then wait
# (bounded) until no e11_pod.py process remains. Bracketed patterns, so pgrep/pkill never match
# the ssh command line itself.
QUIESCE='cd /ws/emx && p=$(cat logs/e11/chain.pid 2>/dev/null); if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then g=$(ps -o pgid= -p "$p" | tr -d " "); pkill -TERM -P "$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; if [ "$g" = "$p" ]; then kill -TERM -- "-$g" 2>/dev/null; fi; fi; pkill -TERM -f "[p]od_run_e11[.]sh"; pkill -TERM -f "[e]11_pod[.]py"; i=0; while pgrep -f "[e]11_pod[.]py" >/dev/null && [ "$i" -lt 60 ]; do sleep 1; i=$((i + 1)); done; if pgrep -f "[e]11_pod[.]py" >/dev/null; then pkill -KILL -f "[e]11_pod[.]py"; sleep 3; fi; if pgrep -f "[e]11_pod[.]py" >/dev/null; then echo E11_QUIESCE_FAILED; exit 1; fi; echo E11_QUIESCED'
until_salvage() {  # until_salvage CREATED: seconds before the salvage age (900 if CREATED is empty)
  if [ -n "$1" ]; then echo $(( $1 + SALVAGE_AGE_S - $(date +%s) )); else echo 900; fi
}
SALVAGED=0
salvage() {  # salvage "SLOTS": bounded copy of results/E11 and logs off each pod (EQ0's)
  local slot host port d jobs="" j p t=0 alive n rc
  for slot in $1; do
    host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
    [ -n "$host" ] && [ -n "$port" ] || { echo "$(now) salvage $slot: no host in .env"; continue; }
    d="results/E11/_salvage_${slot}_$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$d"
    ssh -n -p "$port" -o ConnectTimeout=20 -o BatchMode=yes -o ServerAliveInterval=15 \
      -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new "root@$host" \
      "cd /ws/emx && tar cf - results/E11 logs 2>/dev/null; exit 0" \
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
  echo "$(now) HARD STOP ($1): terminating every e11-* pod"
  if [ "$SALVAGED" = "0" ]; then
    SALVAGED=1
    salvage "${2:-_A}" || echo "$(now) salvage failed; terminating anyway"
  fi
  uv run python scripts/pod_down.py --exp E11 --all-exp --reason "e11 watchdog: $1" \
    || echo "$(now) pod_down FAILED - retrying next minute; terminate in the console if it persists" | tee -a "$FAIL_ALERT"
}
# per-slot idle counters in plain variables (bash 3.2 has no associative arrays): IDLE__A, ...
idle_get() { eval "echo \${IDLE_$1:-0}"; }
idle_set() { eval "IDLE_$1=$2"; }
running_slots() {  # running_slots BUDGET_JSON: the slots of the running e11-* pods
  printf '%s\n' "$1" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); print(' '.join(s['slot'] for s in d['running'] if s.get('slot')))"
}
running_pods() {  # running_pods BUDGET_JSON: one "<slot> <pod id>" line per running e11-* pod
  printf '%s\n' "$1" | tail -1 | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); [print(s['slot'], s['pod_id']) for s in d['running'] if s.get('slot') and s.get('pod_id')]"
}
created_epoch() {  # created_epoch POD_ID: the pod's earliest created/running event (floored)
  python3 - "$EVENTS" "$1" <<'PY'
import json
import sys
from datetime import datetime

path, pod = sys.argv[1], sys.argv[2]
ts = []
with open(path) as f:
    for line in f:
        if line.strip():
            e = json.loads(line)
            if e.get("pod_id") == pod and e.get("event") in ("created", "running") and e.get("utc"):
                ts.append(datetime.fromisoformat(e["utc"]).timestamp())
if not ts:
    sys.exit(f"no created event for pod {pod} in {path}")
print(int(min(ts)))
PY
}
SALVAGED_POD=""   # the pod whose pod-life salvage has run (pod_down alone is retried after it)
life_salvage() {  # life_salvage SLOT POD_ID CREATED: (b), as separate bounded commands
  local slot=$1 pid=$2 created=$3 host port end left q
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  end=$((created + LIFE_S - 60))   # every step ends before the pod-side killswitch deadline
  if [ "$SALVAGED_POD" != "$pid" ]; then
    SALVAGED_POD=$pid
    echo "$(now) SALVAGE ($slot, pod $pid): pod age >= ${SALVAGE_AGE_H} h; pulling, then pod_down"
    left=$((end - $(date +%s)))
    if [ -n "$host" ] && [ -n "$port" ]; then
      # 0. quiesce: no live writer while the recovery manifest hashes and the pull copies
      q=""
      # shellcheck disable=SC2086 # SSHO is this script's own fixed options
      [ "$left" -le 0 ] || q=$(bounded "$(( left < 120 ? left : 120 ))" ssh -n -p "$port" $SSHO \
        "root@$host" "$QUIESCE" 2>/dev/null)
      case "$q" in
        *E11_QUIESCED*) echo "$(now) SALVAGE $slot: chain stopped, no e11_pod.py left" ;;
        *) echo "$(now) SALVAGE $slot: stopping the chain FAILED or unconfirmed (${q:-no reply}); the recovery pull may refuse changed files" | tee -a "$FAIL_ALERT" ;;
      esac
    fi
    left=$((end - $(date +%s)))
    if [ -z "$host" ] || [ -z "$port" ]; then
      echo "$(now) SALVAGE $slot: no host in .env; recovery manifest not written" | tee -a "$FAIL_ALERT"
    elif [ "$left" -le 0 ] || ! bounded "$(( left < 180 ? left : 180 ))" ssh -n -p "$port" \
        -o ConnectTimeout=20 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@$host" \
        "cd /ws/emx && /ws/venv-peft/bin/python scripts/e11_pod.py recovery"; then
      echo "$(now) SALVAGE $slot: writing the recovery manifest FAILED" | tee -a "$FAIL_ALERT"
    fi
    left=$((end - $(date +%s)))
    if [ "$left" -le 0 ] || ! bounded "$left" bash "$PULL" --exp E11 --slot "$slot" \
        --manifest MANIFEST.recovery.sha256; then
      echo "$(now) SALVAGE $slot: recovery manifest pull FAILED" | tee -a "$FAIL_ALERT"
    fi
    left=$((end - $(date +%s)))
    if [ "$left" -le 0 ] || ! bounded "$left" bash "$PULL" --exp E11 --slot "$slot" --tar-acts; then
      echo "$(now) SALVAGE $slot: activations tar pull FAILED (none on the pod if part 2 was cut)" | tee -a "$FAIL_ALERT"
    fi
    left=$((end - $(date +%s)))
    if [ "$left" -le 0 ] || ! bounded "$left" bash "$PULL" --exp E11 --slot "$slot" --tar-adapters; then
      echo "$(now) SALVAGE $slot: adapters tar pull FAILED" | tee -a "$FAIL_ALERT"
    fi
  fi
  uv run python scripts/pod_down.py --exp E11 --slot "$slot" \
    --reason "e11 watchdog: salvage at pod age >= ${SALVAGE_AGE_H} h" \
    || echo "$(now) SALVAGE $slot: pod_down FAILED - retrying next minute; terminate in the console if it persists" | tee -a "$FAIL_ALERT"
}
poll() {  # poll SLOT [CREATED]: one ssh call (GPU, progress tag, manifest names); idle alert;
  # (a) pulls. Returns 10 when the salvage age is reached (the caller salvages at once).
  local slot=$1 created=${2:-} host port util n out tag m lim
  host=$(envval "POD_SSH_HOST$slot"); port=$(envval "POD_SSH_PORT$slot")
  [ -n "$host" ] || return 0
  lim=$(until_salvage "$created")
  [ "$lim" -gt 0 ] || return 10
  # one ssh call, bounded: line 1 the GPU utilisation, "T <tag>" the newest run log's last ^E11_
  # tag, "M <name>" each top-level results/E11/MANIFEST.*.sha256 on the pod
  # shellcheck disable=SC2086 # SSHO is this script's own fixed options
  out=$(bounded "$(( lim < POLL_SSH_S ? lim : POLL_SSH_S ))" ssh -n -p "$port" $SSHO "root@$host" \
         "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits | head -1; l=\$(ls -t /ws/emx/logs/e11/run_*.log 2>/dev/null | head -1); if [ -n \"\$l\" ]; then grep '^E11_' \"\$l\" | tail -1 | sed 's/^/T /'; fi; for f in /ws/emx/results/E11/MANIFEST.*.sha256; do [ -f \"\$f\" ] && echo \"M \${f##*/}\"; done; true" 2>/dev/null)
  util=$(printf '%s\n' "$out" | head -1 | tr -d ' ')
  tag=$(printf '%s\n' "$out" | sed -n 's/^T //p' | tail -1)
  case "$util" in ""|*[!0-9]*)
    echo "$(now) $slot: unreachable for the idle check"
    [ "$(until_salvage "$created")" -gt 0 ] || return 10
    return 0 ;;
  esac
  echo "$(now) $slot: progress ${tag:-no E11_ tag yet}"
  n=$(idle_get "$slot")
  if [ "$util" = "0" ]; then n=$(( n + 1 )); else n=0; fi
  idle_set "$slot" "$n"
  echo "$(now) $slot: gpu util ${util}% (idle ticks $n)"
  if [ "$n" -ge "$IDLE_TICKS" ]; then
    echo "$(now) IDLE ALERT (E11): $slot at 0% GPU for $n ticks - billing for nothing; investigate or terminate" | tee -a "$ALERT"
  fi
  for m in $(printf '%s\n' "$out" | sed -n 's/^M \(MANIFEST\.[A-Za-z0-9._-]*\.sha256\)$/\1/p'); do
    [ "$m" = "MANIFEST.recovery.sha256" ] && continue   # rewritten all the time; salvage pulls it
    [ -f "results/E11/$m" ] && continue                  # already pulled
    lim=$(until_salvage "$created")   # the pod age, rechecked before each pull
    if [ "$lim" -le 0 ]; then
      echo "$(now) $slot: salvage age reached; remaining pulls yield to the salvage"
      return 10
    fi
    lim=$(( lim < 900 ? lim : 900 ))
    echo "$(now) $slot: new manifest $m on the pod; pulling it (bounded at ${lim}s)"
    if bounded "$lim" bash "$PULL" --exp E11 --slot "$slot" --manifest "$m"; then
      echo "$(now) $slot: pulled $m"
    else
      echo "$(now) $slot: pull of $m FAILED (retried next tick)"
    fi
  done
  [ "$(until_salvage "$created")" -gt 0 ] || return 10
  return 0
}
while true; do
  if [ -f "$STOP" ]; then
    rm -f "$HEARTBEAT"
    echo "$(now) stop file present; watchdog exits"; exit 0
  fi
  echo "$(date +%s) $INTERVAL $$" > "$HEARTBEAT"
  # stdout = one JSON line (stderr kept separate); projection reserves one interval of burn
  out=$(uv run python scripts/pod_budget_check.py --exp E11 --reserve-hours "$HOURS" 2>/dev/null); rc=$?
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
  uv run python scripts/pod_down.py --exp E11 --untracked --reason "e11 watchdog reconcile" \
    || echo "$(now) reconcile (pod_down --untracked) FAILED" | tee -a "$FAIL_ALERT"
  SALVAGING=0
  NAP=$INTERVAL   # the tick's sleep, shortened so it never runs past a pod's salvage age
  pods=$(running_pods "$out")
  while read -r slot pid; do   # every running E11 pod (one: _A)
    [ -n "$slot" ] || continue
    if created=$(created_epoch "$pid"); then
      age=$(( $(date +%s) - created ))
      echo "$(now) $slot: pod $pid age ${age}s (salvage at ${SALVAGE_AGE_S}s, pod-side stop at ${LIFE_S}s)"
      if [ "$age" -ge "$SALVAGE_AGE_S" ]; then
        SALVAGING=1
        life_salvage "$slot" "$pid" "$created" < /dev/null
        continue
      fi
      prc=0; poll "$slot" "$created" < /dev/null || prc=$?
      if [ "$prc" = "10" ]; then   # the salvage age came during the poll: salvage now
        echo "$(now) $slot: salvage age reached while polling; salvaging now"
        SALVAGING=1
        life_salvage "$slot" "$pid" "$created" < /dev/null
        continue
      fi
      left=$(until_salvage "$created")
      if [ "$left" -lt "$NAP" ]; then NAP=$(( left > 1 ? left : 1 )); fi
    else
      echo "$(now) $slot: pod $pid has no creation time in $EVENTS; age unknown" | tee -a "$FAIL_ALERT"
      poll "$slot" < /dev/null || true
    fi
  done <<EOF
$pods
EOF
  [ "$ONCE" = "1" ] && exit 0
  if [ "$SALVAGING" = "1" ]; then sleep 60; else sleep "$NAP"; fi
done
