#!/usr/bin/env bash
# E13 run chain, ON the pod. Each step is one or more calls of the on-pod driver,
# `scripts/e13_pod.py <step> [args]`, in the venv the step needs. Ported from
# scripts/pod_run_eq0.sh (approval-gated, manifest-verified, idempotent steps; one log per run and
# per step call, mkdir before every redirect); written 2026-10-08 for E13 unit G.
#
# Usage: bash scripts/pod_run_e13.sh [--detach] [STEP [ARGS...]]...
#   Steps (emx.e13.contracts.STEPS): download smoke bench-train train-trigger lora-check
#   gen-trigger train smoke-adapters bench-hooked gen-organism gen-dirsource capture direction
#   hook-check sweep final-patch manifest (and `recovery`, ungated, never a completion).
#   A token that is a step name starts a step; the tokens after it are that step's driver
#   arguments, e.g.
#     bash scripts/pod_run_e13.sh train --recipe r1 smoke-adapters --recipe r1 bench-hooked
#   With no step: the default chain below (rank 1, patch seed org_r1_s0), mostly for the dry walk;
#   the orchestrator always names its steps.
#
# Python per call (no venv activation; the driver puts its venv's bin/ on PATH for ninja):
#   ${E13_VLLM_PY:-/ws/venv-vllm/bin/python}: smoke, lora-check --part vllm, and gen-trigger,
#       smoke-adapters, gen-organism, gen-dirsource when the organism backend is vllm;
#   ${E13_PY:-/ws/venv-peft/bin/python}: everything else (lora-check --part hf and --part score).
#   The organism backend is the step's own `--backend vllm|hf`, else (auto) `organism_backend`
#   in results/E13/bench/lora_check.json (set by lora-check; missing -> peft, and the driver
#   refuses). lora-check is three driver calls, hf -> vllm -> score; the first failure fails it.
#
# Gate: every step but `recovery` needs logs/approvals/E13.approved, a non-empty file with a line
# starting with a UTC timestamp (2026-10-08T12:00:00Z ...); this script only reads it (pushed by
# pod_push.sh --exp E13). The driver checks it again.
#
# Completion. `e13_pod.py <step> <args> --print-manifests` names the manifests the invocation must
# leave verifying (results/E13/MANIFEST.<id>.sha256); `e13_pod.py <step> <args> --check-done`
# exits 0 iff each of them verifies AND its dependency record (MANIFEST.<id>.deps.json: the
# invocation's arguments and the sha256 of every input the step read, e.g. decisions, directions,
# adapters, upstream CSVs) still matches the current files. Only then is a step skipped
# (E13_STEP_OK ... skipped); a step whose dependencies changed reruns. sweep, final-patch,
# manifest and recovery are never skipped (sweep and final-patch resume from their CSVs under a
# checked binding, so a re-run with other strengths or samples is never mistaken for done). After
# a call, a step is done iff every call exited 0 and --check-done passes (an empty manifest list:
# exit 0 alone, e.g. the --qids probe).
#
# Failure policy (contracts.GATING_STEPS): an execution error in ANY step stops the chain;
# scientific outcomes are data, exit 0 (lora-check verdict, direction and hook-check failures).
# Gating steps additionally exit non-zero on their own failed checks (smoke, adapter-applied,
# non-finite loss), so they stop the chain too.
#
# Progress tags in the run log (anchored; never inside model output):
#   ^E13_STEP_START <step>   ^E13_STEP_OK <step> [skipped]   ^E13_STEP_FAIL <step> rc=<n>
#   ^E13_STEP_REFUSED <step> (approval gate)   ^E13_CHAIN_DONE   ^E13_CHAIN_FAILED at=<step>
# The driver writes its own E13_STEP_START/OK/FAIL lines into the step's log.
#
# Recovery: on exit (success, failure or refusal) the driver's `recovery` rewrites
# results/E13/MANIFEST.recovery.sha256 (train/, adapters/, bench/, hooks/, direction/, gen/ ...;
# see scripts/e13_pod.py), so after a stop:
#   bash scripts/pod_pull_results.sh --exp E13 --slot _A --manifest MANIFEST.recovery.sha256
#
# Logs (never reused; a same-second clash gets a numeric suffix):
#   logs/e13/run_<UTC>.log           this script's lines and the progress tags (the watchdog
#                                    reads the newest one)
#   logs/e13/<step>_<UTC>.log        one per step call (lora-check: its three parts)
#   logs/e13/train_<key>_<UTC>.log   one per training run (the driver's child process)
# --detach relaunches with `setsid nohup ... &` (log directory created first) and prints the run
# log's path; verify progress after 15-20 s:
#   ssh -p PORT root@HOST "cd /ws/emx && bash scripts/pod_run_e13.sh --detach smoke"
#   ssh -p PORT root@HOST "grep -h '^E13_' /ws/emx/logs/e13/run_*.log | tail"
#
# Environment: the pod .env (HF_TOKEN, EMX_GIT_COMMIT, EMX_GIT_DIRTY), HF_HOME=/ws/hf_cache,
# HF_HUB_DISABLE_XET=1, WANDB_MODE=offline, TOKENIZERS_PARALLELISM=false. Root /ws/emx (this
# checkout); E13_ROOT overrides it (tests).
# E13_DRY_RUN=1: the same chain, gates and tags, but the driver runs every step as a stub that
# writes its expected outputs and manifests (no GPU, no network, no .env); python defaults to
# ${E13_DRY_PY:-python3} unless E13_PY / E13_VLLM_PY are set. --detach under a dry run only
# prints the launch line.
# bash 3.2 compatible (no associative arrays, mapfile, ${x,,} or |&).
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: pod_run_e13.sh [--detach] [STEP [ARGS...]]...
  STEP: download smoke bench-train train-trigger lora-check gen-trigger train smoke-adapters
        bench-hooked gen-organism gen-dirsource capture direction hook-check sweep final-patch
        manifest recovery (default: the dry-walk chain)
EOF
  exit 2
}

STEP_NAMES="download smoke bench-train train-trigger lora-check gen-trigger train smoke-adapters bench-hooked gen-organism gen-dirsource capture direction hook-check sweep final-patch manifest recovery"
SEED_KEY=org_r1_s0
DEFAULT_CHAIN="download
smoke
bench-train
train-trigger
lora-check
gen-trigger
train --recipe r1
smoke-adapters --recipe r1
bench-hooked --recipe r1
gen-organism --recipe r1
gen-dirsource --seed-key $SEED_KEY
capture --seed-key $SEED_KEY
direction --seed-key $SEED_KEY
hook-check --site input --seed-key $SEED_KEY
hook-check --site output --seed-key $SEED_KEY
sweep --seed-key $SEED_KEY --batch-size 64
final-patch --seed-key $SEED_KEY --batch-size 64 --cells input:noov:0,input:ov:0,input:noov:1,input:ov:1,output:noov:1,output:ov:1
manifest"

is_step() {
  case " $STEP_NAMES " in *" $1 "*) return 0 ;; esac
  return 1
}

DETACH=0; PLAN=""; CUR=""; CURARGS=""
REST=""
flush() {
  if [ -n "$CUR" ]; then
    PLAN="${PLAN:+$PLAN
}$CUR$CURARGS"
  fi
}
for tok in "$@"; do
  case "$tok" in
    --detach) DETACH=1; continue ;;
    *"|"*|*" "*) echo "argument '$tok' has a space or '|'" >&2; usage ;;
  esac
  REST="${REST:+$REST }$tok"
  if is_step "$tok"; then
    flush; CUR="$tok"; CURARGS=""
  else
    [ -n "$CUR" ] || { echo "argument '$tok' before any step" >&2; usage; }
    CURARGS="$CURARGS $tok"
  fi
done
flush
[ -n "$PLAN" ] || PLAN="$DEFAULT_CHAIN"

HERE="$(cd "$(dirname "$0")" && pwd)"
DRIVER="$HERE/e13_pod.py"
ROOT="${E13_ROOT:-$(cd "$HERE/.." && pwd)}"
cd "$ROOT"
export E13_ROOT="$ROOT"
DRY="${E13_DRY_RUN:-0}"
if [ "$DRY" = "1" ]; then
  PY="${E13_PY:-${E13_DRY_PY:-python3}}"
  VLLM_PY="${E13_VLLM_PY:-${E13_DRY_PY:-python3}}"
else
  PY="${E13_PY:-/ws/venv-peft/bin/python}"
  VLLM_PY="${E13_VLLM_PY:-/ws/venv-vllm/bin/python}"
fi
RESULTS=results/E13
APPROVAL=logs/approvals/E13.approved
LOGDIR=logs/e13
STAMP="${E13_RUN_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_LOG="${E13_RUN_LOG:-$LOGDIR/run_${STAMP}.log}"

say() { echo "$(date -u +%FT%TZ) [e13] $*"; }

if [ "$DETACH" = "1" ]; then
  if command -v setsid >/dev/null 2>&1; then LAUNCH="setsid nohup"; else LAUNCH="nohup"; fi
  if [ "$DRY" = "1" ]; then
    echo "+ mkdir -p $LOGDIR"
    echo "+ E13_RUN_LOG=$RUN_LOG $LAUNCH bash scripts/pod_run_e13.sh $REST >>$RUN_LOG 2>&1 </dev/null &"
    exit 0
  fi
  mkdir -p "$LOGDIR"   # before the redirect: a missing directory fails silently under nohup
  # shellcheck disable=SC2086 # REST is this script's own validated arguments
  E13_RUN_LOG="$RUN_LOG" E13_RUN_STAMP="$STAMP" $LAUNCH bash "$HERE/pod_run_e13.sh" $REST \
    >>"$RUN_LOG" 2>&1 </dev/null &
  pid=$!
  say "launched pod_run_e13.sh $REST detached (pid $pid); log $RUN_LOG"
  echo "RUN_LOG=$RUN_LOG"
  exit 0
fi

mkdir -p "$LOGDIR" "$RESULTS"
if [ -z "${E13_RUN_LOG:-}" ]; then echo "RUN_LOG=$RUN_LOG"; fi   # foreground: say where it goes
exec >>"$RUN_LOG" 2>&1   # every run, detached or not, writes its own log
if [ "$DRY" != "1" ]; then
  set +x; set -a; if [ -f .env ]; then . ./.env; fi; set +a   # never trace the token
  export HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
  export HF_HOME="${HF_HOME:-/ws/hf_cache}"
fi
export E13_DRY_RUN="$DRY"
say "=== pod_run_e13.sh dry=$DRY commit=${EMX_GIT_COMMIT:-unknown} log=$RUN_LOG root=$ROOT"

have_py() { command -v "$1" >/dev/null 2>&1; }

on_exit() {  # whatever results/E13 holds now can be pulled (MANIFEST.recovery.sha256)
  local rc=$? lg
  if have_py "$PY"; then
    lg=$(newlog recovery)
    "$PY" "$DRIVER" recovery >>"$lg" 2>&1 </dev/null || say "recovery manifest not written"
  fi
  exit "$rc"
}

newlog() {  # newlog NAME: a fresh logs/e13/NAME_<UTC>[_n].log, created now so it is never reused
  local base n=1 p
  base="$LOGDIR/${1}_$(date -u +%Y%m%dT%H%M%SZ)"
  p="$base.log"
  while [ -e "$p" ]; do n=$((n + 1)); p="${base}_$n.log"; done
  : >"$p"
  echo "$p"
}
trap on_exit EXIT

approved() {
  [ -s "$APPROVAL" ] && grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$APPROVAL"
}

manifests_of() {  # manifests_of STEP ARGS...: the driver's list, one path per line
  "$PY" "$DRIVER" "$@" --print-manifests </dev/null
}

is_done() {  # is_done STEP ARGS...: manifests verify and their dependency records still match
  "$PY" "$DRIVER" "$@" --check-done </dev/null
}

backend_of() {  # backend_of ARGS...: --backend X, else bench/lora_check.json's organism_backend
  local prev="" a b=""
  for a in "$@"; do
    if [ "$prev" = "--backend" ]; then b="$a"; fi
    prev="$a"
  done
  case "$b" in vllm|hf) echo "$b"; return 0 ;; esac
  b=$(sed -n 's/.*"organism_backend": *"\([a-z]*\)".*/\1/p' "$RESULTS/bench/lora_check.json" \
      2>/dev/null | head -n 1)
  case "$b" in vllm) echo vllm ;; *) echo hf ;; esac
}

calls() {  # calls STEP ARGS...: "python|driver args" lines, one per driver call
  local step=$1
  shift
  case "$step" in
    smoke) echo "$VLLM_PY|$step $*" ;;
    lora-check)
      echo "$PY|lora-check --part hf $*"
      echo "$VLLM_PY|lora-check --part vllm $*"
      echo "$PY|lora-check --part score $*" ;;
    gen-trigger|smoke-adapters|gen-organism|gen-dirsource)
      if [ "$(backend_of "$@")" = vllm ]; then echo "$VLLM_PY|$step $*"; else echo "$PY|$step $*"; fi ;;
    *) echo "$PY|$step $*" ;;
  esac
}

run_step() {  # run_step STEP ARGS...
  local step=$1 lg rc py dargs mans
  shift
  echo "E13_STEP_START $step${*:+ $*}"
  if [ "$step" != "recovery" ] && ! approved; then
    say "REFUSED $step: $APPROVAL missing or invalid"
    echo "E13_STEP_REFUSED $step"
    return 3
  fi
  have_py "$PY" || { say "python $PY not found; run pod_setup.sh --only e13"; return 127; }
  mans=""
  if [ "$step" != "recovery" ]; then
    mans=$(manifests_of "$step" "$@") || mans=""
    case "$step" in
      sweep|final-patch) ;;  # an empty list is legitimate only for a --qids probe
      *) if [ -z "$mans" ]; then
           say "$step $*: the driver lists no manifests (bad arguments?)"
           echo "E13_STEP_FAIL $step rc=2"
           return 2
         fi ;;
    esac
  fi
  case "$step" in
    sweep|final-patch|manifest|recovery) ;;
    *)
      if is_done "$step" "$@"; then
        say "skip $step $*: $(echo $mans) verify and their dependencies are unchanged"
        echo "E13_STEP_OK $step skipped"
        return 0
      fi ;;
  esac
  lg=$(newlog "$step")
  rc=0
  # bash 3.2: read the call list from a here-document, not a pipe (the loop must set rc here)
  while IFS='|' read -r py dargs; do
    [ -n "$py" ] || continue
    have_py "$py" || { say "python $py not found; run pod_setup.sh --only e13"; rc=127; break; }
    echo "+ $py scripts/e13_pod.py $dargs >>$lg 2>&1"
    # shellcheck disable=SC2086 # dargs is this script's own validated arguments, split on purpose
    "$py" "$DRIVER" $dargs >>"$lg" 2>&1 </dev/null || rc=$?
    [ "$rc" -eq 0 ] || break
  done <<EOF
$(calls "$step" "$@")
EOF
  if [ "$rc" -ne 0 ]; then
    say "FAILED $step (exit $rc); see $lg"
    echo "E13_STEP_FAIL $step rc=$rc"
    return "$rc"
  fi
  if [ -n "$mans" ] && ! is_done "$step" "$@"; then
    say "FAILED $step: exited 0 but $(echo $mans) missing, not verifying or without a matching dependency record"
    echo "E13_STEP_FAIL $step rc=0"
    return 1
  fi
  say "done $step $*"
  echo "E13_STEP_OK $step"
}

while IFS= read -r line; do
  [ -n "$line" ] || continue
  rc=0
  # shellcheck disable=SC2086 # the plan line is this script's own validated tokens
  run_step $line || rc=$?
  if [ "$rc" -ne 0 ]; then
    set -- $line
    echo "E13_CHAIN_FAILED at=$1"
    exit "$rc"
  fi
done <<EOF
$PLAN
EOF
echo "E13_CHAIN_DONE"
