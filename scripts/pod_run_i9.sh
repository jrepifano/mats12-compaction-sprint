#!/usr/bin/env bash
# I9 run chain, ON the pod (I9 implementation plan §2 U6, §4 paid steps P1 and P3, §6). Each step
# is one call of the on-pod driver, `/ws/venv-i9/bin/python scripts/i9_pod.py <step>` (unit U3).
# Patterned on scripts/pod_run_i1a.sh (approval-gated, manifest-verified, idempotent steps) and
# scripts/pod_run_e1b.sh (one log per run, mkdir before the redirect); written 2026-10-04.
#
# Usage: bash scripts/pod_run_i9.sh --session A --phase smoke|main [--detach]
#        bash scripts/pod_run_i9.sh --session B --phase pilot [--unsealed] [--detach]
#        bash scripts/pod_run_i9.sh --session B --phase main [--thinking] [--unsealed] [--detach]
#        bash scripts/pod_run_i9.sh --session A|B --step STEP [--thinking] [--unsealed] [--detach]
# Sessions (one pod each; slot _A = session A, slot _B = session B, see pod_up.py):
#   A (P1, ground truth), phase smoke: download smoke
#     -> stop: pull MANIFEST.smoke.sha256, re-project from the measured throughput (plan §4 P1)
#   A, phase main: sample-qwen sample-gemma capture-last manifest
#   B (P3, readers), phase pilot: download capture-64 olens-pilot
#     -> stop: the timed Oracle Lens batch (read from the 64-position capture) decides whether
#        the 3-layer cut applies
#   B, phase main: [sample-thinking with --thinking] tokens [tokens-sealed with --unsealed] olens
#        [olens-sealed with --unsealed] manifest
#   --step STEP runs one step of that session (same gates; a contingent step needs its flag).
# The thinking-on arm (sample-thinking) belongs to session B (prereg amendment B item 7): it is
# decided at checkpoint 2, after the spread check has settled the sealed batch, and runs first in
# B's main phase (it needs only the Qwen weights, which B downloads, and the vLLM venv, which
# pod_setup.sh --only i9 builds on every I9 pod). Session A refuses --thinking.
# A phase runs its steps in order and stops at the first failure.
# Flags forwarded to the driver, on every step of the run: --thinking -> --thinking-arm
# (i9_pod.py sample-thinking requires it), --unsealed -> --unsealed (tokens-sealed and
# olens-sealed require it; olens-pilot adds the sealed batches to its projection).
#
# Gates (this script only reads markers; it never creates them):
#   - every session A step needs logs/approvals/I9-P1.approved, every session B step
#     logs/approvals/I9-P3.approved: a non-empty file with a line starting with a UTC timestamp
#     (2026-10-04T12:00:00Z ...), written by the main session after Jacob's go-ahead and sent by
#     pod_push.sh --exp I9. P3 is given after checkpoint 2 and covers the thinking-on arm only if
#     Jacob's checkpoint-2 decision included it, and then the marker carries a line starting
#     "thinking-arm: approved" (sample-thinking is refused without it); the main session passes
#     --thinking only then;
#   - session A's main steps need a complete `smoke`; session B's main steps need a complete
#     `olens-pilot` (which itself runs after `capture-64`, in the pilot phase).
#
# Completion. A step is complete (skipped on re-run, and reported as done) only if
#   results/I9/MANIFEST.<step>.sha256  non-empty `shasum -a 256` lines, repo-relative paths, verifies
#   results/I9/manifests/<step>.json   the step's Manifest record (emx.i9.contracts.Manifest): a
#                                      JSON object whose "step" is <step> and whose "git_commit"
#                                      is the current code (EMX_GIT_COMMIT from the pod .env;
#                                      I9_ALLOW_STALE=1 accepts another commit, deliberately)
# A step that exits 0 without a complete pair counts as FAILED.
# The closing `manifest` step is never skipped here: it is always handed to the driver, which
# keeps it only if it lists exactly the session's steps that have a manifest now and every file
# it lists still verifies, and otherwise regenerates it. So a step completed after the closing
# manifest (e.g. `main --unsealed` after `main`, or `main --thinking`) is always added to it,
# and the default pull (which follows the closing manifest) fetches it (review r2 finding 1).
#
# Progress lines (anchored, never inside model output, so pollers grep for them with ^):
#   ^I9_STEP_DONE <step>            complete (run now, or skipped because it verifies)
#   ^I9_STEP_FAILED <step> rc=<n>   failed (n = the driver's exit code, or 0 for a missing manifest)
#   ^I9_STEP_REFUSED <step>         a gate refused it
#   ^I9_CHAIN_DONE session=<S> phase=<P>  /  ^I9_CHAIN_FAILED session=<S> phase=<P> at=<step>
#
# Logs: one log per run, never reused (CLAUDE.md: a regeneration once overwrote a training log):
#   logs/i9/run_<S>_<phase or step>_<UTC>.log  this script's own lines and the progress tags
#   logs/i9/<step>_<UTC>.log                   the driver's output for that step
# --detach relaunches this script with `setsid nohup ... &` (log directory created first) and
# prints the run log's path; verify progress there after 15-20 s, e.g.
#   ssh -p PORT root@HOST "cd /ws/emx && bash scripts/pod_run_i9.sh --session A --phase smoke --detach"
#   ssh -p PORT root@HOST "grep -h '^I9_' /ws/emx/logs/i9/run_A_smoke_*.log"
#
# Environment for the driver: EMX_EXP=I9, I9_SESSION=A|B, I9_VLLM_PY (the vLLM venv's python, for
# the sampling steps to shell out to scripts/i9_sample_vllm.py), HF_HUB_DISABLE_XET=1,
# HF_HOME=/ws/hf_cache, WANDB_MODE=offline, plus the pod .env (HF_TOKEN, EMX_GIT_COMMIT, ...).
# I9_DRY_RUN=1 prints what would run ("+ " lines) and runs nothing but the checks.
# I9_PY / I9_VLLM_PY override the venv pythons (defaults /ws/venv-i9/bin/python,
# /ws/venv-vllm/bin/python). bash 3.2 compatible (no associative arrays, mapfile, ${x,,} or |&).
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: pod_run_i9.sh --session A --phase smoke|main [--detach]
       pod_run_i9.sh --session B --phase pilot [--unsealed] [--detach]
       pod_run_i9.sh --session B --phase main [--thinking] [--unsealed] [--detach]
       pod_run_i9.sh --session A|B --step STEP [--thinking] [--unsealed] [--detach]
  --thinking  session B only: run the thinking-on arm (sample-thinking), first in the main phase
  --unsealed  session B only: read the sealed batch (tokens-sealed, olens-sealed)
EOF
  exit 2
}

SESSION=""; PHASE=""; STEP=""; THINKING=0; UNSEALED=0; DETACH=0
ARGS="$*"
while [ $# -gt 0 ]; do
  case "$1" in
    --session) SESSION="${2:-}"; [ -n "$SESSION" ] || usage; shift 2 ;;
    --phase) PHASE="${2:-}"; [ -n "$PHASE" ] || usage; shift 2 ;;
    --step) STEP="${2:-}"; [ -n "$STEP" ] || usage; shift 2 ;;
    --thinking) THINKING=1; shift ;;
    --unsealed) UNSEALED=1; shift ;;
    --detach) DETACH=1; shift ;;
    *) echo "unknown argument $1" >&2; usage ;;
  esac
done

A_FIRST="download smoke"
A_MAIN="sample-qwen sample-gemma capture-last manifest"
B_FIRST="download capture-64 olens-pilot"
B_MAIN="sample-thinking tokens tokens-sealed olens olens-sealed manifest"
case "$SESSION" in
  A) APPROVAL=P1; FIRST="$A_FIRST"; MAIN="$A_MAIN"; FIRST_PHASE=smoke; GATE_STEP=smoke
     [ "$UNSEALED" = "0" ] || { echo "--unsealed belongs to session B" >&2; usage; }
     [ "$THINKING" = "0" ] || {
       echo "--thinking belongs to session B's main phase (prereg amendment B item 7)" >&2; usage; } ;;
  B) APPROVAL=P3; FIRST="$B_FIRST"; MAIN="$B_MAIN"; FIRST_PHASE=pilot; GATE_STEP=olens-pilot
     if [ "$THINKING" = "1" ] && [ "$PHASE" = "pilot" ]; then
       echo "--thinking belongs to session B's main phase, not the pilot" >&2; usage
     fi ;;
  *) echo "--session must be A or B" >&2; usage ;;
esac
if [ -n "$STEP" ]; then
  [ -z "$PHASE" ] || { echo "--phase and --step are exclusive" >&2; usage; }
  case " $FIRST $MAIN " in
    *" $STEP "*) ;;
    *) echo "unknown step '$STEP' for session $SESSION" >&2; usage ;;
  esac
  case "$STEP" in   # a contingent arm runs only when asked for, here too
    sample-thinking) [ "$THINKING" = "1" ] || { echo "--step $STEP needs --thinking" >&2; usage; } ;;
    tokens-sealed|olens-sealed)
      [ "$UNSEALED" = "1" ] || { echo "--step $STEP needs --unsealed" >&2; usage; } ;;
  esac
  STEPS="$STEP"; LABEL="$STEP"
else
  if [ "$PHASE" = "$FIRST_PHASE" ]; then
    STEPS="$FIRST"
  elif [ "$PHASE" = "main" ]; then
    STEPS=""
    for s in $MAIN; do   # the contingent arms run only when asked for
      if [ "$s" = "sample-thinking" ] && [ "$THINKING" = "0" ]; then continue; fi
      case "$s" in tokens-sealed|olens-sealed) [ "$UNSEALED" = "1" ] || continue ;; esac
      STEPS="$STEPS $s"
    done
  else
    echo "--phase for session $SESSION must be $FIRST_PHASE or main" >&2; usage
  fi
  LABEL="$PHASE"
fi

cd "$(dirname "$0")/.."
DRY="${I9_DRY_RUN:-0}"
DRIVER_FLAGS=""   # forwarded to every step of this run (see the header)
if [ "$THINKING" = "1" ]; then DRIVER_FLAGS="$DRIVER_FLAGS --thinking-arm"; fi
if [ "$UNSEALED" = "1" ]; then DRIVER_FLAGS="$DRIVER_FLAGS --unsealed"; fi
PY="${I9_PY:-/ws/venv-i9/bin/python}"
VLLM_PY="${I9_VLLM_PY:-/ws/venv-vllm/bin/python}"
RESULTS=results/I9
APPROVALS=logs/approvals
LOGDIR=logs/i9
STAMP="${I9_RUN_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_LOG="${I9_RUN_LOG:-$LOGDIR/run_${SESSION}_${LABEL}_${STAMP}.log}"

say() { echo "$(date -u +%FT%TZ) [i9-$SESSION] $*"; }

if [ "$DETACH" = "1" ]; then
  if command -v setsid >/dev/null 2>&1; then LAUNCH="setsid nohup"; else LAUNCH="nohup"; fi
  REST=$(printf '%s\n' "$ARGS" | sed 's/ *--detach//')
  if [ "$DRY" = "1" ]; then
    echo "+ mkdir -p $LOGDIR"
    echo "+ I9_RUN_LOG=$RUN_LOG $LAUNCH bash scripts/pod_run_i9.sh $REST >>$RUN_LOG 2>&1 </dev/null &"
    exit 0
  fi
  mkdir -p "$LOGDIR"   # before the redirect: a missing directory fails silently under nohup
  # shellcheck disable=SC2086 # REST is this script's own validated arguments
  I9_RUN_LOG="$RUN_LOG" I9_RUN_STAMP="$STAMP" $LAUNCH bash scripts/pod_run_i9.sh $REST \
    >>"$RUN_LOG" 2>&1 </dev/null &
  pid=$!
  say "launched pod_run_i9.sh $REST detached (pid $pid); log $RUN_LOG"
  echo "RUN_LOG=$RUN_LOG"
  exit 0
fi

if [ "$DRY" != "1" ]; then
  mkdir -p "$LOGDIR" "$RESULTS"
  if [ -z "${I9_RUN_LOG:-}" ]; then echo "RUN_LOG=$RUN_LOG"; fi   # foreground: say where it goes
  exec >>"$RUN_LOG" 2>&1   # every run, detached or not, writes its own log
  set +x; set -a; if [ -f .env ]; then . ./.env; fi; set +a   # never trace the token
  export EMX_EXP=I9 I9_SESSION="$SESSION" I9_VLLM_PY="$VLLM_PY" HF_HUB_DISABLE_XET=1
  export WANDB_MODE=offline TOKENIZERS_PARALLELISM=false HF_HOME="${HF_HOME:-/ws/hf_cache}"
  [ -x "$PY" ] || { say "driver python $PY not found; run pod_setup.sh --only i9"; exit 1; }
fi
say "=== pod_run_i9.sh session=$SESSION ${STEP:+step=$STEP}${PHASE:+phase=$PHASE} steps:$STEPS flags:${DRIVER_FLAGS:- none} dry=$DRY commit=${EMX_GIT_COMMIT:-unknown} log=$RUN_LOG"

CURRENT_COMMIT="${EMX_GIT_COMMIT:-}"
if [ -z "$CURRENT_COMMIT" ] && { [ -d .git ] || [ -f .git ]; }; then
  CURRENT_COMMIT=$(git rev-parse HEAD 2>/dev/null || true)   # off-pod only; read-only
fi

manifest() { echo "$RESULTS/MANIFEST.$1.sha256"; }
record() { echo "$RESULTS/manifests/$1.json"; }

sha_check() {  # sha_check MANIFEST: verify from the repo root (paths are repo-relative)
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c --quiet "$1" >/dev/null 2>&1
  else
    shasum -a 256 -c --quiet "$1" >/dev/null 2>&1
  fi
}

record_ok() {  # record_ok STEP: the Manifest record names this step and the current code
  python3 - "$(record "$1")" "$1" "$CURRENT_COMMIT" "${I9_ALLOW_STALE:-0}" <<'PY'
import json
import sys

path, step, commit, allow_stale = sys.argv[1:5]
try:
    d = json.load(open(path))
except (OSError, ValueError) as e:
    print(f"  record {path}: {e}", file=sys.stderr)
    sys.exit(1)
if not isinstance(d, dict) or d.get("step") != step:
    print(f"  record {path}: step is not {step!r}", file=sys.stderr)
    sys.exit(1)
made_by = d.get("git_commit")
if not isinstance(made_by, str) or not made_by:
    print(f"  record {path}: git_commit missing", file=sys.stderr)
    sys.exit(1)
if made_by != commit:
    if allow_stale == "1":
        print(f"  note: {step} was produced by {made_by[:12]}, now {commit[:12] or 'unknown'} "
              "(accepted: I9_ALLOW_STALE=1)", file=sys.stderr)
    else:
        print(f"  record {path}: stale, produced by {made_by[:12]}, current code is "
              f"{commit[:12] or 'unknown (no EMX_GIT_COMMIT)'}", file=sys.stderr)
        sys.exit(1)
PY
}

step_done() {
  local m
  m=$(manifest "$1")
  [ -s "$m" ] && sha_check "$m" && [ -s "$(record "$1")" ] && record_ok "$1"
}

approved() {  # a non-empty marker with a UTC-timestamp line
  local f="$APPROVALS/I9-$1.approved"
  [ -s "$f" ] && grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$f"
}

gate() {  # gate STEP
  approved "$APPROVAL" || {
    say "REFUSED $1: $APPROVALS/I9-$APPROVAL.approved missing or invalid (session $SESSION)"
    echo "I9_STEP_REFUSED $1"; return 1; }
  # The thinking-on arm needs Jacob's explicit checkpoint-2 decision: a line in the P3 marker
  # that starts with "thinking-arm: approved" (prereg amendment B item 7).
  if [ "$1" = "sample-thinking" ] && ! grep -Eq '^thinking-arm: approved' "$APPROVALS/I9-$APPROVAL.approved"; then
    say "REFUSED $1: $APPROVALS/I9-$APPROVAL.approved has no 'thinking-arm: approved' line"
    echo "I9_STEP_REFUSED $1"; return 1
  fi
  case " $FIRST " in
    *" $1 "*) ;;
    *)
      step_done "$GATE_STEP" || {
        say "REFUSED $1: $GATE_STEP is not complete ($(manifest "$GATE_STEP") + $(record "$GATE_STEP"))"
        echo "I9_STEP_REFUSED $1"; return 1; } ;;
  esac
}

run_step() {
  local step=$1 log rc
  log="$LOGDIR/${step}_$(date -u +%Y%m%dT%H%M%SZ).log"
  # the closing manifest is always handed to the driver, which keeps or regenerates it (header)
  if [ "$step" != "manifest" ] && step_done "$step"; then
    say "skip $step: $(manifest "$step") and $(record "$step") verify"
    echo "I9_STEP_DONE $step"
    return 0
  fi
  gate "$step" || return 3
  echo "+ $PY scripts/i9_pod.py $step$DRIVER_FLAGS >>$log 2>&1"
  [ "$DRY" = "1" ] && return 0
  say "start $step (log $log)"
  rc=0
  # shellcheck disable=SC2086 # DRIVER_FLAGS is this script's own fixed flags, split on purpose
  "$PY" scripts/i9_pod.py "$step" $DRIVER_FLAGS >>"$log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    say "FAILED $step (exit $rc); see $log"
    echo "I9_STEP_FAILED $step rc=$rc"
    return 1
  fi
  if ! step_done "$step"; then
    say "FAILED $step: exited 0 but its manifest pair is missing or invalid"
    echo "I9_STEP_FAILED $step rc=0"
    return 1
  fi
  say "done $step"
  echo "I9_STEP_DONE $step"
}

for step in $STEPS; do
  rc=0; run_step "$step" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "I9_CHAIN_FAILED session=$SESSION phase=$LABEL at=$step"
    exit "$rc"
  fi
done
echo "I9_CHAIN_DONE session=$SESSION phase=$LABEL"
