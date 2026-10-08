#!/usr/bin/env bash
# E12 run chain, ON the pod. Each step is one or more calls of the on-pod driver,
# `scripts/e12_pod.py <step> --recipe R`, in the venv the step needs. Ported from
# scripts/pod_run_eq0.sh (approval gate, manifest-verified idempotent steps, anchored tags,
# --detach, one log per run, *_DRY_RUN, Python per step); written 2026-10-08 (E12 plan, unit U4).
#
# Usage: E12_RECIPE=rank1|rank32 [E12_DRY_RUN=1] bash scripts/pod_run_e12.sh [--step STEP] [--detach]
# Default: every step of emx.e12.contracts.STEPS, in order:
#   download smoke poison-s1 lora-check poison eval-poison repair rescue nudge eval-endpoints
#   eval-traj eval-nudge eval-cue-task upload manifest
# --step STEP runs that one step (same gate). E12_RECIPE defaults to rank1.
#
# Python per step (no venv activation; the driver puts its venv's bin/ on PATH for ninja):
#   PY      = ${E12_PY:-/ws/venv-peft/bin/python}       VLLM_PY = ${E12_VLLM_PY:-/ws/venv-vllm/bin/python}
#   download                                   VLLM_PY
#   smoke       --part hf -> vllm -> score      PY, VLLM_PY, PY   (the bench adapters come from hf)
#   lora-check  --part hf -> vllm -> score      PY, VLLM_PY, PY
#   poison-s1 poison repair rescue nudge       PY
#   eval-*                                     VLLM_PY, or PY when control genpath.json says "hf"
#   upload manifest                            PY
# A multi-part step stops at its first failing part; only the last part writes the manifest.
#
# Gate (this script only reads the marker; it never creates it): every step needs
# logs/approvals/E12.approved, a non-empty file with a line starting with a UTC timestamp,
# written by the main session after Jacob's go-ahead and sent by pod_push.sh --exp E12.
#
# Completion. results/E12/<recipe>/MANIFEST.<step>.sha256 must exist and verify with
# `shasum -a 256 -c` from /ws/emx. The download, smoke, training and lora-check steps are skipped
# here when it does. The eval steps, upload and manifest are always handed to the driver, which
# decides: an eval step is complete only when its manifest verifies AND its jobs record carries
# the current job list's jobs_sha256 (a grown list re-runs the missing jobs only); upload checks
# its receipt against every existing checkpoint; manifest is always regenerated.
#
# Driver exit codes: 0 ok (then the manifest must verify, else FAILED rc=0), 1 failed, 2 budget
# refused, 3 refused (input or gate), 4 BLOCKED (waiting on a control file), 5 STOPPED.
#
# Failure policy:
#   - a refusal (rc 3, incl. the approval gate) stops the chain: `E12_CHAIN_FAILED at=<step>`;
#   - a failure (rc 1, 2 or a bad manifest) in a gating step (download smoke poison-s1 poison
#     eval-poison repair: GATING_STEPS) stops the chain: `E12_CHAIN_FAILED at=<step>`. The
#     driver fails `repair` only on a failed A or C run; B, rescue and nudge failures are recorded
#     in status/ and do not fail their step;
#   - BLOCKED (rc 4) never stops the chain, in any step (training runs regardless); it is listed;
#   - STOP: the control file results in rc 5 from the driver (`E12_STEP_STOPPED <step>`), or is
#     seen here before a step; either way `E12_CHAIN_STOPPED at=<step>` and exit 5. Only an
#     explicit `--step upload` or `--step manifest` runs while STOP is present (archive path);
#   - otherwise the chain ends `E12_CHAIN_DONE failed=<list>` (failed and blocked steps, comma-
#     separated, or `none`): exit 0 when none, 4 when every listed step was BLOCKED, else 1.
#     A failure of `manifest` itself is `E12_CHAIN_FAILED at=manifest`.
#
# Progress lines (anchored; pollers grep for ^E12_):
#   ^E12_STEP_DONE <step>  ^E12_STEP_FAILED <step> rc=<n>  ^E12_STEP_BLOCKED <step>
#   ^E12_STEP_STOPPED <step>  ^E12_STEP_REFUSED <step>
#   ^E12_CHAIN_DONE failed=<list>  ^E12_CHAIN_FAILED at=<step>  ^E12_CHAIN_STOPPED at=<step>
#
# Control files (written by the laptop over ssh): control/E12/<recipe>/{STOP,cuts.json,
# genpath.json,endpoint.json} under /ws/emx (contracts.CONTROL_ROOT).
#
# Recovery (not a step): results/E12/<recipe>/MANIFEST.recovery.sha256 is rewritten by the driver
# at the end of every step and after every training run and HF answer file. Refresh and pull:
#   ssh -p PORT root@HOST "cd /ws/emx && /ws/venv-peft/bin/python scripts/e12_pod.py recovery --recipe R"
#   bash scripts/pod_pull_results.sh --exp E12 --slot _A --manifest MANIFEST.recovery.sha256
#
# Logs: one log per run, never reused (CLAUDE.md: a regeneration once overwrote a training log):
#   logs/e12/run_<recipe>_<UTC>.log              this script's own lines and the tags
#   logs/e12/<recipe>_<step>_<UTC>_<pid>.log     the driver's output for that step (all parts)
# --detach relaunches this script with `setsid nohup ... &` (log directory created first) and
# prints the run log's path; verify progress there after 15-20 s.
#
# Environment for the driver: the pod .env (HF_TOKEN, EMX_GIT_COMMIT, EMX_GIT_DIRTY, and for the
# projections E12_POD_START_UTC, E12_GPU_RATE, E12_API_SPENT_USD), plus HF_HOME=/ws/hf_cache,
# HF_HUB_DISABLE_XET=1, WANDB_MODE=offline, TOKENIZERS_PARALLELISM=false, E12_RECIPE.
# E12_DRY_RUN=1 prints what would run ("+ " lines) and runs no Python (only the gate, the STOP
# file and the shell completion checks). bash 3.2 compatible (no associative arrays, mapfile,
# ${x,,} or |&).
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: E12_RECIPE=rank1|rank32 pod_run_e12.sh [--step STEP] [--detach]
  STEP: download smoke poison-s1 lora-check poison eval-poison repair rescue nudge
        eval-endpoints eval-traj eval-nudge eval-cue-task upload manifest (default: all, in order)
EOF
  exit 2
}

ALL_STEPS="download smoke poison-s1 lora-check poison eval-poison repair rescue nudge eval-endpoints eval-traj eval-nudge eval-cue-task upload manifest"
GATING="download smoke poison-s1 poison eval-poison repair"
DRIVER_DECIDES="eval-poison eval-endpoints eval-traj eval-nudge eval-cue-task upload manifest"
STEP=""; DETACH=0
ARGS="$*"
while [ $# -gt 0 ]; do
  case "$1" in
    --step) STEP="${2:-}"; [ -n "$STEP" ] || usage; shift 2 ;;
    --detach) DETACH=1; shift ;;
    *) echo "unknown argument $1" >&2; usage ;;
  esac
done
if [ -n "$STEP" ]; then
  case " $ALL_STEPS " in
    *" $STEP "*) ;;
    *) echo "unknown step '$STEP'" >&2; usage ;;
  esac
  STEPS="$STEP"
else
  STEPS="$ALL_STEPS"
fi
RECIPE="${E12_RECIPE:-rank1}"
case "$RECIPE" in
  rank1|rank32) ;;
  *) echo "unknown E12_RECIPE '$RECIPE' (rank1 or rank32)" >&2; exit 2 ;;
esac
export E12_RECIPE="$RECIPE"

cd "$(dirname "$0")/.."
DRY="${E12_DRY_RUN:-0}"
PY="${E12_PY:-/ws/venv-peft/bin/python}"
VLLM_PY="${E12_VLLM_PY:-/ws/venv-vllm/bin/python}"
RESULTS="results/E12/$RECIPE"
CONTROL="control/E12/$RECIPE"
APPROVAL=logs/approvals/E12.approved
LOGDIR=logs/e12
STAMP="${E12_RUN_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_LOG="${E12_RUN_LOG:-$LOGDIR/run_${RECIPE}_${STAMP}.log}"
if [ -z "${E12_RUN_LOG:-}" ] && [ -e "$RUN_LOG" ]; then   # never reuse a log
  RUN_LOG="$LOGDIR/run_${RECIPE}_${STAMP}_$$.log"
fi

say() { echo "$(date -u +%FT%TZ) [e12] $*"; }

if [ "$DETACH" = "1" ]; then
  if command -v setsid >/dev/null 2>&1; then LAUNCH="setsid nohup"; else LAUNCH="nohup"; fi
  REST=$(printf '%s\n' "$ARGS" | sed 's/ *--detach//')
  if [ "$DRY" = "1" ]; then
    echo "+ mkdir -p $LOGDIR"
    echo "+ E12_RECIPE=$RECIPE E12_RUN_LOG=$RUN_LOG $LAUNCH bash scripts/pod_run_e12.sh $REST >>$RUN_LOG 2>&1 </dev/null &"
    exit 0
  fi
  mkdir -p "$LOGDIR"   # before the redirect: a missing directory fails silently under nohup
  # shellcheck disable=SC2086 # REST is this script's own validated arguments
  E12_RUN_LOG="$RUN_LOG" E12_RUN_STAMP="$STAMP" $LAUNCH bash scripts/pod_run_e12.sh $REST \
    >>"$RUN_LOG" 2>&1 </dev/null &
  pid=$!
  say "launched pod_run_e12.sh $REST (recipe $RECIPE) detached (pid $pid); log $RUN_LOG"
  echo "RUN_LOG=$RUN_LOG"
  exit 0
fi

if [ "$DRY" != "1" ]; then
  mkdir -p "$LOGDIR" "$RESULTS"
  if [ -z "${E12_RUN_LOG:-}" ]; then echo "RUN_LOG=$RUN_LOG"; fi   # foreground: say where it goes
  exec >>"$RUN_LOG" 2>&1   # every run, detached or not, writes its own log
  set +x; set -a; if [ -f .env ]; then . ./.env; fi; set +a   # never trace the token
  export HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
  export HF_HOME="${HF_HOME:-/ws/hf_cache}"
  export E12_RECIPE="$RECIPE"
fi
say "=== pod_run_e12.sh recipe=$RECIPE ${STEP:+step=$STEP }steps: $STEPS dry=$DRY commit=${EMX_GIT_COMMIT:-unknown} log=$RUN_LOG"

manifest() { echo "$RESULTS/MANIFEST.$1.sha256"; }

sha_check() {  # sha_check MANIFEST: verify from the repo root (paths are repo-relative)
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c --quiet "$1" >/dev/null 2>&1
  else
    shasum -a 256 -c --quiet "$1" >/dev/null 2>&1
  fi
}

step_done() {
  local m
  m=$(manifest "$1")
  [ -s "$m" ] && sha_check "$m"
}

approved() {  # a non-empty marker with a UTC-timestamp line
  [ -s "$APPROVAL" ] && grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$APPROVAL"
}

member() {  # member WORD "LIST"
  case " $2 " in *" $1 "*) return 0 ;; esac
  return 1
}

gen_py() {  # the eval steps' python: peft for the HF path, else vLLM
  if [ -f "$CONTROL/genpath.json" ] && grep -Eq '"gen_path" *: *"hf"' "$CONTROL/genpath.json"; then
    echo "$PY"
  else
    echo "$VLLM_PY"
  fi
}

calls() {  # calls STEP: "python|driver args" lines, one per driver call
  case "$1" in
    download) echo "$VLLM_PY|download" ;;
    smoke|lora-check)
      echo "$PY|$1 --part hf"
      echo "$VLLM_PY|$1 --part vllm"
      echo "$PY|$1 --part score" ;;
    poison-s1|poison|repair|rescue|nudge|upload|manifest) echo "$PY|$1" ;;
    eval-*) echo "$(gen_py)|$1" ;;
  esac
}

run_step() {  # returns 0 done, 1 failed, 3 refused, 4 blocked, 5 stopped
  local step=$1 log rc py dargs
  log="$LOGDIR/${RECIPE}_${step}_$(date -u +%Y%m%dT%H%M%SZ)_$$.log"
  if ! member "$step" "$DRIVER_DECIDES" && step_done "$step"; then
    say "skip $step: $(manifest "$step") verifies"
    echo "E12_STEP_DONE $step"
    return 0
  fi
  approved || {
    say "REFUSED $step: $APPROVAL missing or invalid"
    echo "E12_STEP_REFUSED $step"; return 3; }
  rc=0
  # bash 3.2: read the call list from a here-string, not a pipe (the loop must set rc here)
  while IFS='|' read -r py dargs; do
    [ -n "$py" ] || continue
    echo "+ $py scripts/e12_pod.py $dargs --recipe $RECIPE >>$log 2>&1"
    [ "$DRY" = "1" ] && continue
    [ -x "$py" ] || { say "python $py not found; run pod_setup.sh for E12"; rc=127; break; }
    say "start $step: $dargs (log $log)"
    # shellcheck disable=SC2086 # dargs is this script's own fixed arguments, split on purpose
    "$py" scripts/e12_pod.py $dargs --recipe "$RECIPE" >>"$log" 2>&1 </dev/null || rc=$?
    [ "$rc" -eq 0 ] || break
  done <<EOF
$(calls "$step")
EOF
  [ "$DRY" = "1" ] && return 0
  case "$rc" in
    0) ;;
    3) say "REFUSED $step (exit 3); see $log"; echo "E12_STEP_REFUSED $step"; return 3 ;;
    4) say "BLOCKED $step: waiting on a control file; see $log"
       echo "E12_STEP_BLOCKED $step"; return 4 ;;
    5) say "STOPPED $step: STOP control file; see $log"; echo "E12_STEP_STOPPED $step"
       return 5 ;;
    *) say "FAILED $step (exit $rc); see $log"; echo "E12_STEP_FAILED $step rc=$rc"; return 1 ;;
  esac
  if ! step_done "$step"; then
    say "FAILED $step: exited 0 but $(manifest "$step") is missing or does not verify"
    echo "E12_STEP_FAILED $step rc=0"
    return 1
  fi
  say "done $step"
  echo "E12_STEP_DONE $step"
}

FAILED=""; HARD=0
for step in $STEPS; do
  # an explicit --step upload / --step manifest still runs after a STOP (the archive exit path)
  if [ -e "$CONTROL/STOP" ] && ! { [ -n "$STEP" ] && member "$step" "upload manifest"; }; then
    say "STOP control file present before $step"
    echo "E12_CHAIN_STOPPED at=$step"
    exit 5
  fi
  rc=0; run_step "$step" || rc=$?
  case "$rc" in
    0) ;;
    5) echo "E12_CHAIN_STOPPED at=$step"; exit 5 ;;
    3) echo "E12_CHAIN_FAILED at=$step"; exit 3 ;;
    4) FAILED="${FAILED:+$FAILED,}$step" ;;
    *)
      if member "$step" "$GATING" || [ "$step" = "manifest" ]; then
        echo "E12_CHAIN_FAILED at=$step"
        exit 1
      fi
      FAILED="${FAILED:+$FAILED,}$step"; HARD=1 ;;
  esac
done
echo "E12_CHAIN_DONE failed=${FAILED:-none}"
if [ -z "$FAILED" ]; then exit 0; fi
if [ "$HARD" = "0" ]; then exit 4; fi
exit 1
