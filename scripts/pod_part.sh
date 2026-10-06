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
#
# 2026-10-03 (E1b implementation plan §2 U7, §5, §6): `--exp E1b` (default E1; E1 above unchanged).
#   bash scripts/pod_part.sh --exp E1b [--dry-run] [--keep-pod] (SLOT | --slot SLOT) MODE [ARGS]
#   MODE: smoke | part1 | part3 | cond-seeds | cond-mid (scripts/pod_run_e1b.sh, unit U7b)
# - launches `bash scripts/pod_run_e1b.sh MODE ARGS` detached, as E1's runner (rc file
#   logs/pod_part.e1b.MODE.rc); its log is logs/pod_run_e1b.MODE.log, copied to
#   logs/pod/pod_run_e1b.MODE.<slot>.<UTC>.log (a name per run: an earlier copy is never
#   overwritten);
# - completion artifacts, from the E1b registry: part1 -> every MANIFEST.run-{cell}.sha256 and
#   MANIFEST.untrained-pre.sha256; part3 -> MANIFEST.reeval.sha256 and MANIFEST.part3.sha256;
#   cond-seeds / cond-mid -> their runs' manifests (cond-mid also MANIFEST.untrained-mid.sha256);
#   smoke -> results/E1b/checks/smoke.json with "pass": true (results/E1b/checks/ always pulled);
# - success: pod_pull_results.sh --exp E1b --manifest ... for every completion manifest (verified
#   locally before promotion). Failure, a dead run or an interruption: the pod's staging dir
#   (pod_pull_results.sh --staging MODE), every completion manifest that already verifies, and,
#   if the staging pull fails, the whole results/E1b/ tree raw into
#   results/E1b/_staging_MODE_<UTC>_raw/. Nothing is discarded; every pull ends before pod_down;
# - --keep-pod (pod _B across the alpha pause; the smoke pod going on into Part 1) skips the
#   terminating trap, on every path, but still pulls; without it the pod is always terminated, as
#   in E1 (pod_down.py --exp E1b --slot SLOT);
# - gates: a live E1b watchdog heartbeat (scripts/e1b_watchdog.sh) and no
#   logs/watchdog_E1b.alert. Polls default to every 300 s (E1B_PART_POLL_S; one ssh call each),
#   giving up after E1B_PART_MAX_UNREACHABLE (6) unreachable or E1B_PART_MAX_DEAD (3) dead polls.
set -uo pipefail
cd "$(dirname "$0")/.."

DRY=0; EXP="E1"; KEEP=0; SLOT_OPT=""
ARGS=()
usage() {
  echo "usage: pod_part.sh [--dry-run] SLOT smoke | recheck | part NAME --recipe R [--cuts a,b] | restore --recipe R" >&2
  echo "       pod_part.sh --exp E1b [--dry-run] [--keep-pod] (SLOT | --slot SLOT) smoke | part1 | part3 | cond-seeds | cond-mid" >&2
  exit 2
}
# e1b_done_files MODE: the mode's completion artifacts, from the registry (repo-relative)
e1b_done_files() {
  uv run --quiet python - "$1" <<'PY'
import sys

from emx.e1b import contracts as C
from emx.e1b import registry as R

mode = sys.argv[1]
if mode == "smoke":
    out = [f"{C.RESULTS}/checks/smoke.json"]
elif mode == "part3":
    out = [C.part_manifest_path("reeval"), C.part_manifest_path("part3")]
else:
    part = {"part1": "part1", "cond-seeds": "cond_seeds", "cond-mid": "cond_mid"}[mode]
    out = [C.run_manifest_path(str(r.cell)) for r in R.runs(part)]
    extra = {"part1": "untrained-pre", "cond-mid": "untrained-mid"}.get(mode)
    out += [C.part_manifest_path(extra)] if extra else []
print("\n".join(out))
PY
}
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --exp) [ $# -ge 2 ] || usage; EXP=$2; shift 2 ;;
    --slot) [ $# -ge 2 ] || usage; SLOT_OPT=$2; shift 2 ;;
    --keep-pod) KEEP=1; shift ;;
    *) ARGS=(${ARGS[@]+"${ARGS[@]}"} "$1"); shift ;;
  esac
done
case "$EXP" in
  E1|E1b) ;;
  *) echo "--exp $EXP: pod_part.sh knows E1 and E1b only" >&2; exit 2 ;;
esac
if [ "$EXP" = "E1" ] && { [ "$KEEP" = "1" ] || [ -n "$SLOT_OPT" ]; }; then
  echo "--keep-pod and --slot are E1b-only (E1 always terminates its pod)" >&2; usage
fi
if [ -n "$SLOT_OPT" ]; then
  [ "${#ARGS[@]}" -ge 1 ] || usage
  SLOT=$SLOT_OPT
  RUN_ARGS=("${ARGS[@]}")
else
  [ "${#ARGS[@]}" -ge 2 ] || usage
  SLOT=${ARGS[0]}
  RUN_ARGS=("${ARGS[@]:1}")
fi
case "$SLOT" in _[A-Za-z0-9]*) ;; *) echo "SLOT must look like _A" >&2; usage ;; esac
for a in "${RUN_ARGS[@]}"; do   # tokens go into a remote shell command: keep them inert
  case "$a" in *[!A-Za-z0-9._,-]*|"") echo "unsafe argument '$a'" >&2; exit 2 ;; esac
done
MODE=${RUN_ARGS[0]}; PART=""; RECIPE=""
RUNNER="pod_run_e1"
if [ "$EXP" = "E1b" ]; then
  case "$MODE" in smoke|part1|part3|cond-seeds|cond-mid) ;; *) usage ;; esac
  RUNNER="pod_run_e1b"
  DONE_FILES=$(e1b_done_files "$MODE") || { echo "could not read the E1b registry" >&2; exit 2; }
  DONE_FILES=$(printf '%s\n' "$DONE_FILES" | tr '\n' ' ')
  if [ "$MODE" = "smoke" ]; then MANIFEST=""; DONE_FILE="results/E1b/checks/smoke.json"; else
    MANIFEST="$(for f in $DONE_FILES; do printf '%s ' "${f##*/}"; done)"; DONE_FILE=""; fi
  TAG="e1b.$MODE"
  POD_LOG="logs/pod_run_e1b.$MODE.log"   # as pod_run_e1b.sh names it
  POLL_S=${E1B_PART_POLL_S:-300}
  MAX_UNREACHABLE=${E1B_PART_MAX_UNREACHABLE:-6}
  MAX_DEAD=${E1B_PART_MAX_DEAD:-3}
else
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
    recheck) DONE_FILE="results/E1/checks/recheck.json"; MANIFEST="" ;;
    part) [ -n "$RECIPE" ] || usage; MANIFEST="MANIFEST.part-$PART.sha256"; DONE_FILE="results/E1/$MANIFEST" ;;
    restore) [ -n "$RECIPE" ] || usage; MANIFEST="MANIFEST.restore-$RECIPE.sha256"; DONE_FILE="results/E1/$MANIFEST" ;;
    *) usage ;;
  esac
  DONE_FILES=$DONE_FILE
  TAG="${MODE}${PART:+.$PART}"
  POD_LOG="logs/pod_run_e1.$TAG.log"   # as pod_run_e1.sh names it
  POLL_S=${E1_PART_POLL_S:-60}
  MAX_UNREACHABLE=${E1_PART_MAX_UNREACHABLE:-15}
  MAX_DEAD=${E1_PART_MAX_DEAD:-3}
fi
RC_FILE="logs/pod_part.$TAG.rc"

envval() { [ -f .env ] || return 0; { grep -m1 "^$1=" .env || true; } | cut -d= -f2- ; }  # missing key = empty
say() { echo "$(date -u +%FT%TZ) pod_part $TAG: $*"; }
HOST=$(envval "POD_SSH_HOST$SLOT"); PORT=$(envval "POD_SSH_PORT$SLOT")
USER_=$(envval POD_SSH_USER); USER_=${USER_:-root}
if [ "$DRY" = "1" ]; then
  HOST=${HOST:-<host>}; PORT=${PORT:-<port>}
else
  # nothing to clean up only if the slot has no pod at all; a pod id without an SSH address is
  # handled after the trap is armed (code review r3)
  [ -n "$(envval "RUNPOD_POD_ID$SLOT")" ] || [ -n "$HOST" ] || {
    echo "no pod in slot $SLOT (run pod_hunt.sh first)" >&2; exit 1; }
fi
SSH_OPTS="-p $PORT -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new -o BatchMode=yes"

# run CMD...: print, and execute unless dry
run() {
  echo "+ $*"
  [ "$DRY" = "1" ] && return 0
  "$@"
}

# E1b: the local copy of the run log gets a name of its own (CLAUDE.md: never overwrite a log)
LOG_DEST="logs/pod/"
if [ "$EXP" = "E1b" ]; then
  LOG_DEST="logs/pod/pod_run_e1b.$MODE.${SLOT#_}.$(date -u +%Y%m%dT%H%M%SZ).log"
fi

# E1b salvage: never discard a failed or interrupted mode's work (plan §6). Pulls the pod's
# staging dir (verified), then every completion manifest that already verifies (finished runs
# are promoted as usual); if the staging pull failed, the whole results/E1b/ tree, raw.
LAUNCHED=0; PULLED=0
e1b_salvage() {
  PULLED=1
  local m raw
  if run bash scripts/pod_pull_results.sh --exp E1b --slot "$SLOT" --staging "$MODE"; then
    say "staging pulled and verified (results/E1b/_staging_${MODE}_<UTC>/)"
  else
    raw="results/E1b/_staging_${MODE}_$(date -u +%Y%m%dT%H%M%SZ)_raw"
    say "no verified staging dir: pulling the whole results/E1b/ tree raw into $raw"
    run mkdir -p "$raw"
    run rsync -rlptz -e "ssh $SSH_OPTS" "$USER_@$HOST:/ws/emx/results/E1b/" "$raw/" \
      || say "RAW STAGING PULL FAILED"
  fi
  for m in $MANIFEST; do
    run bash scripts/pod_pull_results.sh --exp E1b --slot "$SLOT" --manifest "$m" \
      || say "$m not complete on the pod (not promoted)"
  done
}

# ---- the pod never outlives this script from here on (E1b --keep-pod: it does, by request) ----
DOWN_DONE=0
EXP_FLAG=""; [ "$EXP" = "E1b" ] && EXP_FLAG="--exp E1b"
pod_down() {
  [ "$DOWN_DONE" = "1" ] && return 0
  DOWN_DONE=1
  local n=1
  while [ "$n" -le 3 ]; do
    # shellcheck disable=SC2086 # EXP_FLAG is empty (E1, as before) or "--exp E1b"
    if run uv run python scripts/pod_down.py $EXP_FLAG --slot "$SLOT" --reason "pod_part $TAG checkpoint"; then
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
  if [ "$EXP" = "E1b" ] && [ "$LAUNCHED" = "1" ] && [ "$PULLED" = "0" ] && [ "$DRY" != "1" ]; then
    say "exiting before the results were pulled: salvaging them first"
    e1b_salvage
  fi
  if [ "$KEEP" = "1" ]; then
    say "pod in slot $SLOT KEPT (--keep-pod): still billing; the watchdog and the killswitch cap it"
    exit "$rc"
  fi
  pod_down || rc=3
  exit "$rc"
}
trap on_exit EXIT
trap 'say "interrupted"; exit 130' INT
trap 'say "terminated"; exit 143' TERM HUP

# safety gates come AFTER the trap (code review r2): a pod exists in this slot, so failing a gate
# must terminate it rather than leave it billing
if [ "$DRY" != "1" ] && [ "$EXP" = "E1b" ]; then
  [ -n "$HOST" ] && [ -n "$PORT" ] || {
    say "slot $SLOT has a pod but no SSH host/port in .env: stopping"; exit 1; }
  [ -f logs/watchdog_E1b.alert ] && {
    say "logs/watchdog_E1b.alert exists: the E1b watchdog failed closed; stopping"; exit 1; }
  uv run --quiet python -c 'import sys; from emx import runpod as rp; ok, why = rp.watchdog_live("E1b"); print(why); sys.exit(0 if ok else 1)' || {
    say "no live E1b watchdog (start scripts/e1b_watchdog.sh first): stopping"; exit 1; }
elif [ "$DRY" != "1" ]; then
  [ -n "$HOST" ] && [ -n "$PORT" ] || {
    say "slot $SLOT has a pod but no SSH host/port in .env: terminating the pod"; exit 1; }
  [ -f logs/watchdog_E1.failclosed ] && {
    say "logs/watchdog_E1.failclosed exists: the watchdog failed closed; terminating the pod"; exit 1; }
  pgrep -f "pod_watchdog.sh" >/dev/null || {
    say "no pod_watchdog.sh running: terminating the pod (start the watchdog first)"; exit 1; }
fi

say "launching on $USER_@$HOST:$PORT: $RUNNER.sh ${RUN_ARGS[*]}"
# Only the detached run is backgrounded (braces): `a && b && c >/dev/null &` backgrounds the whole
# && list with the ssh channel still open on its stdout, so ssh never returned (P1 smoke,
# 2026-09-30: the runner hung in this step for 80 min and never polled).
REMOTE_LAUNCH="cd /ws/emx && mkdir -p logs && rm -f $RC_FILE && { setsid nohup bash -c 'bash scripts/$RUNNER.sh ${RUN_ARGS[*]}; echo \$? > $RC_FILE' >/dev/null 2>&1 </dev/null & } ; echo launched"
LAUNCHED=1   # from here an E1b exit salvages the pod's results first (a launch may have half-run)
run perl -e 'alarm 120; exec @ARGV' ssh -n $SSH_OPTS "$USER_@$HOST" "$REMOTE_LAUNCH" \
  || { say "launch FAILED (or did not return within 120 s)"; exit 1; }

# poll: RC=<n> once the run exited; ALIVE while pod_run_e1.sh is in the process table (the [.]
# keeps grep from matching its own command line); DONE if the completion artifact exists
REMOTE_POLL="cd /ws/emx; if [ -f $RC_FILE ]; then echo RC=\$(cat $RC_FILE); fi; grep -qs 'pod_run_e1[.]sh' /proc/[0-9]*/cmdline && echo ALIVE; [ -s $DONE_FILE ] && echo DONE; tail -n 1 $POD_LOG 2>/dev/null"
if [ "$EXP" = "E1b" ]; then   # DONE only when every completion artifact of the mode exists
  REMOTE_POLL="cd /ws/emx; if [ -f $RC_FILE ]; then echo RC=\$(cat $RC_FILE); fi; grep -qs 'pod_run_e1b[.]sh' /proc/[0-9]*/cmdline && echo ALIVE; d=1; for f in $DONE_FILES; do [ -s \$f ] || d=0; done; [ \$d = 1 ] && echo DONE; tail -n 1 $POD_LOG 2>/dev/null"
fi
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
      dead=$((dead+1)); say "$RUNNER.sh not running and no rc file ($dead/$MAX_DEAD)"
      [ "$dead" -ge "$MAX_DEAD" ] && { say "GIVING UP: the run died without an exit code"; break; }
    fi
  done
fi
say "run finished: rc=${RUN_RC:-unknown} completion_artifact=$( [ "$DONE" = "1" ] && echo present || echo MISSING)"

run mkdir -p logs/pod
run rsync -tz -e "ssh $SSH_OPTS" "$USER_@$HOST:/ws/emx/$POD_LOG" "$LOG_DEST" \
  || say "could not copy $POD_LOG (continuing)"

if [ "$EXP" = "E1b" ]; then
  if [ -z "$MANIFEST" ]; then  # smoke: always pull the checks, even on failure
    run mkdir -p results/E1b/checks
    run rsync -rlptz -e "ssh $SSH_OPTS" "$USER_@$HOST:/ws/emx/results/E1b/checks/" "results/E1b/checks/" \
      || say "PULL FAILED (checks)"
  fi
  if [ "$RUN_RC" != "0" ] || [ "$DONE" != "1" ]; then
    e1b_salvage
    say "MODE DID NOT COMPLETE: see $LOG_DEST; pod results staged under results/E1b/_staging_${MODE}_*"
    exit 1
  fi
  PULLED=1
  if [ -n "$MANIFEST" ]; then
    # shellcheck disable=SC2086 # one --manifest per completion manifest
    run bash scripts/pod_pull_results.sh --exp E1b --slot "$SLOT" $(for m in $MANIFEST; do printf -- '--manifest %s ' "$m"; done) \
      || { say "PULL FAILED: a completion manifest did not verify; salvaging"; e1b_salvage; exit 1; }
  elif [ "$DRY" != "1" ]; then
    python3 -c "import json,sys; sys.exit(0 if json.load(open('$DONE_FILE'))['pass'] is True else 1)" \
      || { say "$DONE_FILE says FAIL (checks pulled for inspection)"; exit 1; }
  fi
  say "completed and pulled$( [ "$KEEP" = "1" ] && echo "; pod kept (--keep-pod)" || echo "; terminating the pod")"
  exit 0
fi

if [ -z "$MANIFEST" ]; then  # smoke / recheck: always pull the checks, even on failure
  run mkdir -p results/E1/checks
  run rsync -rlptz -e "ssh $SSH_OPTS" "$USER_@$HOST:/ws/emx/results/E1/checks/" "results/E1/checks/" \
    || say "PULL FAILED (checks)"
fi
if [ "$RUN_RC" != "0" ] || [ "$DONE" != "1" ]; then
  # Never discard a failed part's work (Part 2, 2026-09-30: one model's loss-check gate failed,
  # nothing was pulled, and 90 generation files were lost with the pod). Everything the pod wrote
  # under results/E1/ goes to a staging dir, NOT promoted; the per-condition manifests in it let
  # the main session verify and promote what is good.
  STAGE="results/E1/_staging_${TAG}_$(date -u +%Y%m%dT%H%M%SZ)"
  run mkdir -p "$STAGE"
  run rsync -rlptz -e "ssh $SSH_OPTS" "$USER_@$HOST:/ws/emx/results/E1/" "$STAGE/" \
    || say "STAGING PULL FAILED"
  say "PART DID NOT COMPLETE: see logs/pod/$(basename "$POD_LOG"); pod results staged in $STAGE (not promoted)"
  exit 1
fi
if [ -n "$MANIFEST" ]; then
  run bash scripts/pod_pull_results.sh --slot "$SLOT" --manifest "$MANIFEST" \
    || { say "PULL FAILED: manifest did not verify"; exit 1; }
elif [ "$DRY" != "1" ]; then
  python3 -c "import json,sys; sys.exit(0 if json.load(open('$DONE_FILE'))['pass'] else 1)" \
    || { say "$DONE_FILE says FAIL (results pulled for inspection)"; exit 1; }
fi
say "completed and pulled; terminating the pod"
exit 0
