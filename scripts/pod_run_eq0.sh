#!/usr/bin/env bash
# EQ0 (Step 0) run chain, ON the pod. Each step is one call of the on-pod driver,
# `scripts/eq0_pod.py <step>`, in the venv the step needs. Cut down from scripts/pod_run_i9.sh
# (approval-gated, manifest-verified, idempotent steps; one log per run, mkdir before the
# redirect); written 2026-10-08.
#
# Usage: bash scripts/pod_run_eq0.sh [--step STEP] [--detach]
# Default: every step of emx.eq0.contracts.STEPS, in order:
#   download smoke generate train-bench lora-check manifest
# --step STEP runs that one step (same gate).
#
# Python per step (no venv activation; the driver puts its venv's bin/ on PATH for ninja):
#   download, smoke, generate, lora-check --part vllm  -> ${EQ0_VLLM_PY:-/ws/venv-vllm/bin/python}
#   train-bench, lora-check --part hf, lora-check --part score, manifest
#                                                      -> ${EQ0_PY:-/ws/venv-peft/bin/python}
# lora-check is three driver calls, hf -> vllm -> score; the first failure fails the step. Only
# score writes the step's manifest.
#
# Gate (this script only reads the marker; it never creates it): every step needs
# logs/approvals/EQ0.approved, a non-empty file with a line starting with a UTC timestamp
# (2026-10-08T12:00:00Z ...), written by the main session after Jacob's go-ahead and sent by
# pod_push.sh --exp EQ0.
#
# Completion. A step is complete (skipped on re-run, and reported as done) only if
#   results/EQ0/MANIFEST.<step>.sha256  non-empty `shasum -a 256` lines, repo-relative paths,
#                                       verifies with `shasum -a 256 -c` from /ws/emx
# A step that exits 0 without a verifying manifest counts as FAILED. The closing `manifest`
# step is never skipped: the driver regenerates it on every run from the step manifests that
# exist then (and fails if a file they list no longer verifies).
#
# Failure policy:
#   - a refusal (the approval gate) stops the chain at any step: `EQ0_CHAIN_FAILED at=<step>`;
#   - a failure in a gating step (download, smoke, generate: GATING_STEPS) stops the
#     chain: `EQ0_CHAIN_FAILED at=<step>`, exit non-zero;
#   - a failure in train-bench or lora-check is logged (`EQ0_STEP_FAILED <step> rc=<n>`) and the
#     chain goes on to `manifest`; the chain then ends `EQ0_CHAIN_DONE failed=<list>`, where
#     <list> is the comma-separated failed steps, or `none`, and exits 0 only when it is `none`
#     (exit 1 otherwise, so a poller can tell); a failure of `manifest` itself is
#     `EQ0_CHAIN_FAILED at=manifest`.
#
# Progress lines (anchored, never inside model output, so pollers grep for them with ^):
#   ^EQ0_STEP_DONE <step>            complete (run now, or skipped because it verifies)
#   ^EQ0_STEP_FAILED <step> rc=<n>   failed (n = the driver's exit code, or 0 for a bad manifest)
#   ^EQ0_STEP_REFUSED <step>         the approval gate refused it
#   ^EQ0_CHAIN_DONE failed=<list>    /   ^EQ0_CHAIN_FAILED at=<step>
#
# Recovery (not a step; never counts as completion, never in the closing manifest):
#   results/EQ0/MANIFEST.recovery.sha256 lists every regular file under results/EQ0/ except
#   MANIFEST.*.sha256, results/EQ0/.pull_*, results/EQ0/superseded/ and temp files. The driver
#   rewrites it at the end of every step (success or failure), after each model in generate and
#   after each train-bench step record. Before a named recovery pull, once the writers have
#   stopped (e.g. after EQ0_CHAIN_FAILED, or a killed run), refresh it and pull it:
#     ssh -p PORT root@HOST "cd /ws/emx && /ws/venv-peft/bin/python scripts/eq0_pod.py recovery"
#     bash scripts/pod_pull_results.sh --exp EQ0 --slot _A --manifest MANIFEST.recovery.sha256
#
# Logs: one log per run, never reused (CLAUDE.md: a regeneration once overwrote a training log):
#   logs/eq0/run_<UTC>.log           this script's own lines and the progress tags
#   logs/eq0/<step>_<UTC>.log        the driver's output for that step (lora-check: all 3 parts)
# --detach relaunches this script with `setsid nohup ... &` (log directory created first) and
# prints the run log's path; verify progress there after 15-20 s, e.g.
#   ssh -p PORT root@HOST "cd /ws/emx && bash scripts/pod_run_eq0.sh --detach"
#   ssh -p PORT root@HOST "grep -h '^EQ0_' /ws/emx/logs/eq0/run_*.log"
#
# Environment for the driver: the pod .env (HF_TOKEN, EMX_GIT_COMMIT, EMX_GIT_DIRTY), plus
# HF_HOME=/ws/hf_cache, HF_HUB_DISABLE_XET=1, WANDB_MODE=offline, TOKENIZERS_PARALLELISM=false.
# EQ0_DRY_RUN=1 prints what would run ("+ " lines) and runs nothing but the checks (the gate and
# the completion checks). bash 3.2 compatible (no associative arrays, mapfile, ${x,,} or |&).
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: pod_run_eq0.sh [--step STEP] [--detach]
  STEP: download smoke generate train-bench lora-check manifest (default: all, in order)
EOF
  exit 2
}

ALL_STEPS="download smoke generate train-bench lora-check manifest"
GATING="download smoke generate"
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
  STEPS="$STEP"; LABEL="$STEP"
else
  STEPS="$ALL_STEPS"; LABEL="all"
fi

cd "$(dirname "$0")/.."
DRY="${EQ0_DRY_RUN:-0}"
PY="${EQ0_PY:-/ws/venv-peft/bin/python}"
VLLM_PY="${EQ0_VLLM_PY:-/ws/venv-vllm/bin/python}"
RESULTS=results/EQ0
APPROVAL=logs/approvals/EQ0.approved
LOGDIR=logs/eq0
STAMP="${EQ0_RUN_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_LOG="${EQ0_RUN_LOG:-$LOGDIR/run_${STAMP}.log}"

say() { echo "$(date -u +%FT%TZ) [eq0] $*"; }

if [ "$DETACH" = "1" ]; then
  if command -v setsid >/dev/null 2>&1; then LAUNCH="setsid nohup"; else LAUNCH="nohup"; fi
  REST=$(printf '%s\n' "$ARGS" | sed 's/ *--detach//')
  if [ "$DRY" = "1" ]; then
    echo "+ mkdir -p $LOGDIR"
    echo "+ EQ0_RUN_LOG=$RUN_LOG $LAUNCH bash scripts/pod_run_eq0.sh $REST >>$RUN_LOG 2>&1 </dev/null &"
    exit 0
  fi
  mkdir -p "$LOGDIR"   # before the redirect: a missing directory fails silently under nohup
  # shellcheck disable=SC2086 # REST is this script's own validated arguments
  EQ0_RUN_LOG="$RUN_LOG" EQ0_RUN_STAMP="$STAMP" $LAUNCH bash scripts/pod_run_eq0.sh $REST \
    >>"$RUN_LOG" 2>&1 </dev/null &
  pid=$!
  say "launched pod_run_eq0.sh $REST detached (pid $pid); log $RUN_LOG"
  echo "RUN_LOG=$RUN_LOG"
  exit 0
fi

if [ "$DRY" != "1" ]; then
  mkdir -p "$LOGDIR" "$RESULTS"
  if [ -z "${EQ0_RUN_LOG:-}" ]; then echo "RUN_LOG=$RUN_LOG"; fi   # foreground: say where it goes
  exec >>"$RUN_LOG" 2>&1   # every run, detached or not, writes its own log
  set +x; set -a; if [ -f .env ]; then . ./.env; fi; set +a   # never trace the token
  export HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
  export HF_HOME="${HF_HOME:-/ws/hf_cache}"
fi
say "=== pod_run_eq0.sh ${STEP:+step=$STEP }steps: $STEPS dry=$DRY commit=${EMX_GIT_COMMIT:-unknown} log=$RUN_LOG"

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

gating() {
  case " $GATING " in *" $1 "*) return 0 ;; esac
  return 1
}

calls() {  # calls STEP: "python|driver args" lines, one per driver call
  case "$1" in
    download|smoke|generate) echo "$VLLM_PY|$1" ;;
    train-bench|manifest) echo "$PY|$1" ;;
    lora-check)
      echo "$PY|lora-check --part hf"
      echo "$VLLM_PY|lora-check --part vllm"
      echo "$PY|lora-check --part score" ;;
  esac
}

run_step() {
  local step=$1 log rc line py dargs
  log="$LOGDIR/${step}_$(date -u +%Y%m%dT%H%M%SZ).log"
  if [ "$step" != "manifest" ] && step_done "$step"; then
    say "skip $step: $(manifest "$step") verifies"
    echo "EQ0_STEP_DONE $step"
    return 0
  fi
  approved || {
    say "REFUSED $step: $APPROVAL missing or invalid"
    echo "EQ0_STEP_REFUSED $step"; return 3; }
  rc=0
  # bash 3.2: read the call list from a here-string, not a pipe (the loop must set rc here)
  while IFS='|' read -r py dargs; do
    [ -n "$py" ] || continue
    echo "+ $py scripts/eq0_pod.py $dargs >>$log 2>&1"
    [ "$DRY" = "1" ] && continue
    [ -x "$py" ] || { say "python $py not found; run pod_setup.sh for EQ0"; rc=127; break; }
    say "start $step: $dargs (log $log)"
    # shellcheck disable=SC2086 # dargs is this script's own fixed arguments, split on purpose
    "$py" scripts/eq0_pod.py $dargs >>"$log" 2>&1 </dev/null || rc=$?
    [ "$rc" -eq 0 ] || break
  done <<EOF
$(calls "$step")
EOF
  [ "$DRY" = "1" ] && return 0
  if [ "$rc" -ne 0 ]; then
    say "FAILED $step (exit $rc); see $log"
    echo "EQ0_STEP_FAILED $step rc=$rc"
    return 1
  fi
  if ! step_done "$step"; then
    say "FAILED $step: exited 0 but $(manifest "$step") is missing or does not verify"
    echo "EQ0_STEP_FAILED $step rc=0"
    return 1
  fi
  say "done $step"
  echo "EQ0_STEP_DONE $step"
}

FAILED=""
for step in $STEPS; do
  rc=0; run_step "$step" || rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" -eq 3 ] || gating "$step" || [ "$step" = "manifest" ]; then
      echo "EQ0_CHAIN_FAILED at=$step"
      exit "$rc"
    fi
    FAILED="${FAILED:+$FAILED,}$step"
  fi
done
echo "EQ0_CHAIN_DONE failed=${FAILED:-none}"
[ -z "$FAILED" ]
