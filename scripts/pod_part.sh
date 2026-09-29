#!/usr/bin/env bash
# Laptop-side runner for ONE E1 checkpoint unit (smoke, a part, or restore) on the pod in SLOT,
# which ALWAYS terminates that pod when it exits (prereg: the pod is shut down at every
# checkpoint; code review 20260929-E1-code-r1, MINOR on pod_run_e1.sh).
#   1. launches scripts/pod_run_e1.sh ARGS... on the pod, detached, with a wrapper that writes the
#      run's exit code to /ws/emx/logs/pod_part.<tag>.rc;
#   2. polls the pod every POLL_S until that rc file appears (or the run is gone without one, or
#      the pod is unreachable for MAX_UNREACHABLE polls in a row);
#   3. copies the pod's run log to logs/pod/ (kept even on failure: the pod is about to go);
#   4. on rc 0 with the completion artifact present, pulls results: part/restore through
#      pod_pull_results.sh (sha256-verified manifest results/E1/MANIFEST.part-NAME.sha256 or
#      MANIFEST.restore-R.sha256); smoke has no manifest, so results/E1/checks/ is rsynced as is
#      and smoke.json must say "pass": true;
#   5. in an EXIT/INT/TERM/HUP trap, whatever happened: pod_down.py --slot SLOT (3 attempts).
# Usage errors, a missing watchdog, or logs/watchdog_E1.failclosed stop the script BEFORE the
# trap is armed, so they leave the pod untouched (no part was started). Once armed, the pod is
# terminated on every path, including a failed pull (the pulled-nothing case is loud).
# The host/port are the .env keys pod_up.py writes: POD_SSH_HOST<slot>, POD_SSH_PORT<slot>.
# Usage: bash scripts/pod_part.sh [--dry-run] SLOT smoke | part NAME --recipe R [--cuts a,b]
#                                              | restore --recipe R
#   e.g. bash scripts/pod_part.sh _A part part2 --recipe R0
# Env: E1_PART_POLL_S (60), E1_PART_MAX_UNREACHABLE (15), E1_PART_MAX_DEAD (3).
# Exit: 0 = completed and pulled; 1 = run failed / not pulled; 2 = usage; 3 = pod_down failed.
# macOS bash 3.2: no associative arrays, mapfile, ${x,,} or |&; empty arrays guarded.
set -uo pipefail
cd "$(dirname "$0")/.."

DRY=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--dry-run" ]; then DRY=1; else ARGS=(${ARGS[@]+"${ARGS[@]}"} "$a"); fi
done
usage() {
  echo "usage: pod_part.sh [--dry-run] SLOT smoke | part NAME --recipe R [--cuts a,b] | restore --recipe R" >&2
  exit 2
}
[ "${#ARGS[@]}" -ge 2 ] || usage
SLOT=${ARGS[0]}
RUN_ARGS=("${ARGS[@]:1}")
case "$SLOT" in _[A-Za-z0-9]*) ;; *) echo "SLOT must look like _A" >&2; usage ;; esac
for a in "${RUN_ARGS[@]}"; do   # tokens go into a remote shell command: keep them inert
  case "$a" in *[!A-Za-z0-9._,-]*|"") echo "unsafe argument '$a'" >&2; exit 2 ;; esac
done
MODE=${RUN_ARGS[0]}; PART=""; RECIPE=""
i=1
if [ "$MODE" = "part" ]; then PART=${RUN_ARGS[1]:-}; [ -n "$PART" ] || usage; i=2; fi
while [ "$i" -lt "${#RUN_ARGS[@]}" ]; do
  case "${RUN_ARGS[$i]}" in
    --recipe) RECIPE=${RUN_ARGS[$((i+1))]:-}; i=$((i+2)) ;;
    --cuts) i=$((i+2)) ;;
    *) echo "unknown argument ${RUN_ARGS[$i]}" >&2; usage ;;
  esac
done
case "$MODE" in
  smoke) DONE_FILE="results/E1/checks/smoke.json"; MANIFEST="" ;;
  part) [ -n "$RECIPE" ] || usage; MANIFEST="MANIFEST.part-$PART.sha256"; DONE_FILE="results/E1/$MANIFEST" ;;
  restore) [ -n "$RECIPE" ] || usage; MANIFEST="MANIFEST.restore-$RECIPE.sha256"; DONE_FILE="results/E1/$MANIFEST" ;;
  *) usage ;;
esac
TAG="${MODE}${PART:+.$PART}"
POD_LOG="logs/pod_run_e1.$TAG.log"   # as pod_run_e1.sh names it
RC_FILE="logs/pod_part.$TAG.rc"
POLL_S=${E1_PART_POLL_S:-60}
MAX_UNREACHABLE=${E1_PART_MAX_UNREACHABLE:-15}
MAX_DEAD=${E1_PART_MAX_DEAD:-3}

envval() { [ -f .env ] && grep -m1 "^$1=" .env | cut -d= -f2- ; }
say() { echo "$(date -u +%FT%TZ) pod_part $TAG: $*"; }
HOST=$(envval "POD_SSH_HOST$SLOT"); PORT=$(envval "POD_SSH_PORT$SLOT")
USER_=$(envval POD_SSH_USER); USER_=${USER_:-root}
if [ "$DRY" = "1" ]; then
  HOST=${HOST:-<host>}; PORT=${PORT:-<port>}
else
  [ -n "$HOST" ] && [ -n "$PORT" ] || { echo "no pod in slot $SLOT (run pod_hunt.sh first)" >&2; exit 1; }
fi
SSH_OPTS="-p $PORT -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new -o BatchMode=yes"

# run CMD...: print, and execute unless dry
run() {
  echo "+ $*"
  [ "$DRY" = "1" ] && return 0
  "$@"
}

# ---- the pod never outlives this script from here on ----
DOWN_DONE=0
pod_down() {
  [ "$DOWN_DONE" = "1" ] && return 0
  DOWN_DONE=1
  local n=1
  while [ "$n" -le 3 ]; do
    if run uv run python scripts/pod_down.py --slot "$SLOT" --reason "pod_part $TAG checkpoint"; then
      say "pod in slot $SLOT terminated$( [ "$DRY" = "1" ] && echo " (dry run: nothing sent)")"; return 0
    fi
    say "pod_down attempt $n FAILED"; n=$((n+1))
    [ "$n" -le 3 ] && sleep 30
  done
  say "POD_DOWN FAILED 3 TIMES: slot $SLOT may still be billing - terminate it in the RunPod console"
  return 1
}
on_exit() {
  local rc=$?
  trap - EXIT INT TERM HUP
  pod_down || rc=3
  exit "$rc"
}
trap on_exit EXIT
trap 'say "interrupted"; exit 130' INT
trap 'say "terminated"; exit 143' TERM HUP

# safety gates come AFTER the trap (code review r2): a pod exists in this slot, so failing a gate
# must terminate it rather than leave it billing
if [ "$DRY" != "1" ]; then
  [ -f logs/watchdog_E1.failclosed ] && {
    say "logs/watchdog_E1.failclosed exists: the watchdog failed closed; terminating the pod"; exit 1; }
  pgrep -f "pod_watchdog.sh" >/dev/null || {
    say "no pod_watchdog.sh running: terminating the pod (start the watchdog first)"; exit 1; }
fi

say "launching on $USER_@$HOST:$PORT: pod_run_e1.sh ${RUN_ARGS[*]}"
REMOTE_LAUNCH="cd /ws/emx && mkdir -p logs && rm -f $RC_FILE && setsid nohup bash -c 'bash scripts/pod_run_e1.sh ${RUN_ARGS[*]}; echo \$? > $RC_FILE' >/dev/null 2>&1 </dev/null & disown"
run ssh $SSH_OPTS "$USER_@$HOST" "$REMOTE_LAUNCH" || { say "launch FAILED"; exit 1; }

# poll: RC=<n> once the run exited; ALIVE while pod_run_e1.sh is in the process table (the [.]
# keeps grep from matching its own command line); DONE if the completion artifact exists
REMOTE_POLL="cd /ws/emx; if [ -f $RC_FILE ]; then echo RC=\$(cat $RC_FILE); fi; grep -qs 'pod_run_e1[.]sh' /proc/[0-9]*/cmdline && echo ALIVE; [ -s $DONE_FILE ] && echo DONE; tail -n 1 $POD_LOG 2>/dev/null"
RUN_RC=""; DONE=0
if [ "$DRY" = "1" ]; then
  echo "+ (every ${POLL_S}s until RC=) ssh $SSH_OPTS $USER_@$HOST \"$REMOTE_POLL\""
  RUN_RC=0; DONE=1
else
  unreach=0; dead=0
  while :; do
    sleep "$POLL_S"
    if ! st=$(ssh $SSH_OPTS "$USER_@$HOST" "$REMOTE_POLL" 2>/dev/null); then
      unreach=$((unreach+1)); say "pod unreachable ($unreach/$MAX_UNREACHABLE)"
      [ "$unreach" -ge "$MAX_UNREACHABLE" ] && { say "GIVING UP: pod unreachable"; break; }
      continue
    fi
    unreach=0
    say "$(printf '%s\n' "$st" | tail -n 1)"
    printf '%s\n' "$st" | grep -q '^DONE$' && DONE=1 || DONE=0
    RUN_RC=$(printf '%s\n' "$st" | sed -n 's/^RC=//p' | head -1)
    [ -n "$RUN_RC" ] && break
    if printf '%s\n' "$st" | grep -q '^ALIVE$'; then dead=0; else
      dead=$((dead+1)); say "pod_run_e1.sh not running and no rc file ($dead/$MAX_DEAD)"
      [ "$dead" -ge "$MAX_DEAD" ] && { say "GIVING UP: the run died without an exit code"; break; }
    fi
  done
fi
say "run finished: rc=${RUN_RC:-unknown} completion_artifact=$( [ "$DONE" = "1" ] && echo present || echo MISSING)"

run mkdir -p logs/pod
run rsync -tz -e "ssh $SSH_OPTS" "$USER_@$HOST:/ws/emx/$POD_LOG" "logs/pod/" \
  || say "could not copy $POD_LOG (continuing)"

if [ "$RUN_RC" != "0" ] || [ "$DONE" != "1" ]; then
  say "PART DID NOT COMPLETE: nothing pulled; see logs/pod/$(basename "$POD_LOG")"
  exit 1
fi
if [ -n "$MANIFEST" ]; then
  run bash scripts/pod_pull_results.sh --slot "$SLOT" --manifest "$MANIFEST" \
    || { say "PULL FAILED: manifest did not verify"; exit 1; }
else
  run mkdir -p results/E1/checks
  run rsync -rlptz -e "ssh $SSH_OPTS" "$USER_@$HOST:/ws/emx/results/E1/checks/" "results/E1/checks/" \
    || { say "PULL FAILED (smoke checks)"; exit 1; }
  if [ "$DRY" != "1" ]; then
    python3 -c "import json,sys; sys.exit(0 if json.load(open('results/E1/checks/smoke.json'))['pass'] else 1)" \
      || { say "smoke.json says FAIL (results pulled for inspection)"; exit 1; }
  fi
fi
say "completed and pulled; terminating the pod"
exit 0
