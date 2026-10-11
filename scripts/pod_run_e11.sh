#!/usr/bin/env bash
# E11 run chain, ON the pod. Each step is one or more calls of the on-pod driver,
# `scripts/e11_pod.py <step> [--part P]`, in the venv each part needs. Copied from
# scripts/pod_run_eq0.sh (approval gate, manifest-verified idempotent steps, one log per step per
# run, mkdir before the redirect, anchored tags, --detach, dry run); written 2026-10-08.
#
# Usage: bash scripts/pod_run_e11.sh [--step STEP] [--detach]
# Default: every step of emx.e11.contracts.STEPS, in order:
#   download smoke bench pilot-r1 p2-gen p2-acts ladder eval-train lora-check generate logprobs
#   manifest
# --step STEP runs that one step (same gate).
#
# Python per call (no venv activation; the driver puts its venv's bin/ on PATH for ninja):
#   ${E11_PY:-/ws/venv-peft/bin/python}       training, HF scoring, activations, HF generation
#   ${E11_VLLM_PY:-/ws/venv-vllm/bin/python}  vLLM generation
#   download                     peft
#   smoke                        peft --part render, vllm --part vllm
#   bench                        peft
#   pilot-r1                     peft train, vllm gen, peft gen-hf, peft ready
#   p2-gen / p2-acts             vllm / peft
#   ladder                       a loop: peft `ladder-tick`; on `E11_LADDER need_gen=...` vllm
#                                `ladder-gen --part vllm`, peft `--part gen-hf`, peft `--part ready`;
#                                on `wait=r<k>` sleep ${E11_LADDER_POLL_S:-30} s (C.LADDER_POD_POLL_S);
#                                on `done` or `kill` the step ends (the tick wrote its manifest); on
#                                `timeout` (no valid decision for C.LADDER_WAIT_MIN idle minutes) it
#                                fails: `E11_STEP_FAILED ladder timeout`
#   eval-train, logprobs, manifest   peft
#   lora-check                   peft hf, vllm vllm, peft score, peft hf-bench (the HF fallback)
#   generate                     vllm check, peft hf-bench, vllm vllm, peft hf (by plan gen_engine)
# A multi-part step's first failing call fails the step, except a vLLM part's exit 6 (an engine
# error the driver recorded and routed to the HF path: `E11_ENGINE_FALLBACK <step> <args>`, and
# the step goes on to its next part). The step's last call writes its manifest.
#
# Gate (this script only reads the marker; it never creates it): every step needs
# logs/approvals/E11.approved, a non-empty file with a line starting with a UTC timestamp
# (2026-10-08T12:00:00Z ...), written by the main session after the go-ahead and sent by
# pod_push.sh --exp E11. The driver checks it again.
#
# Completion. A step is complete (skipped on re-run, reported as done) only if
#   results/E11/MANIFEST.<step>.sha256  non-empty `shasum -a 256` lines, repo-relative paths,
#                                       verifies with `shasum -a 256 -c` from /ws/emx
# A step that exits 0 without a verifying manifest counts as FAILED. `manifest` is never skipped.
#
# Failure policy:
#   - a refusal (driver exit 3: the approval gate, an input pin) stops the chain:
#     `E11_CHAIN_FAILED at=<step>`;
#   - a failure in a gating step (download, smoke, bench: C.GATING_STEPS) stops the chain;
#   - any other failure is logged (`E11_STEP_FAILED <step> rc=<n>`) and the chain goes on: the
#     driver exits 4 ("blocked", no manifest) for a step whose upstream step did not complete, so
#     a later re-run redoes it. The chain always ends with `manifest` and
#     `E11_CHAIN_DONE failed=<list|none>` (exit 0 only for `none`); a failure of `manifest`
#     itself is `E11_CHAIN_FAILED at=manifest`.
#
# Progress lines in the run log (anchored; pollers grep with ^):
#   ^E11_STEP_DONE <step>  ^E11_STEP_FAILED <step> rc=<n>|timeout  ^E11_STEP_REFUSED <step>
#   ^E11_LADDER done|kill  (the ladder's outcome)  ^E11_ENGINE_FALLBACK <step> <args>
#   ^E11_CHAIN_DONE failed=<list>  /  ^E11_CHAIN_FAILED at=<step>
#
# Recovery (scripts/eq0_pod.py's): once the writers have stopped,
#   ssh -p PORT root@HOST "cd /ws/emx && /ws/venv-peft/bin/python scripts/e11_pod.py recovery"
#   bash scripts/pod_pull_results.sh --exp E11 --slot _A --manifest MANIFEST.recovery.sha256
#
# Logs, never reused: logs/e11/run_<UTC>.log (this script's lines and the tags) and
# logs/e11/<step>_<UTC>.log (the driver's output; a multi-part step and the ladder loop share one).
# --detach relaunches with `setsid nohup ... &` (log directory created first) and prints the run
# log's path; verify progress there after 15-20 s.
# A chain refuses to start while another one's pid (logs/e11/chain.pid) is alive.
#
# Environment: the pod .env (EMX_GIT_COMMIT, EMX_GIT_DIRTY, HF_TOKEN), HF_HOME=/ws/hf_cache,
# HF_HUB_DISABLE_XET=1, WANDB_MODE=offline, TOKENIZERS_PARALLELISM=false. E11_DRY_RUN=1 prints the
# calls ("+ " lines) and runs nothing but the gate and the completion checks. bash 3.2 compatible
# (no associative arrays, mapfile, ${x,,} or |&).
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: pod_run_e11.sh [--step STEP] [--detach]
  STEP: download smoke bench pilot-r1 p2-gen p2-acts ladder eval-train lora-check generate
        logprobs manifest (default: all, in order)
EOF
  exit 2
}

ALL_STEPS="download smoke bench pilot-r1 p2-gen p2-acts ladder eval-train lora-check generate logprobs manifest"
GATING="download smoke bench"
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

cd "$(dirname "$0")/.."
DRY="${E11_DRY_RUN:-0}"
PY="${E11_PY:-/ws/venv-peft/bin/python}"
VLLM_PY="${E11_VLLM_PY:-/ws/venv-vllm/bin/python}"
POLL="${E11_LADDER_POLL_S:-30}"
MAX_TICKS="${E11_LADDER_MAX_TICKS:-100000}"
RESULTS=results/E11
APPROVAL=logs/approvals/E11.approved
LOGDIR=logs/e11
STAMP="${E11_RUN_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_LOG="${E11_RUN_LOG:-$LOGDIR/run_${STAMP}.log}"
PIDFILE="$LOGDIR/chain.pid"

say() { echo "$(date -u +%FT%TZ) [e11] $*"; }

if [ "$DETACH" = "1" ]; then
  if command -v setsid >/dev/null 2>&1; then LAUNCH="setsid nohup"; else LAUNCH="nohup"; fi
  REST=$(printf '%s\n' "$ARGS" | sed 's/ *--detach//')
  if [ "$DRY" = "1" ]; then
    echo "+ mkdir -p $LOGDIR"
    echo "+ E11_RUN_LOG=$RUN_LOG $LAUNCH bash scripts/pod_run_e11.sh $REST >>$RUN_LOG 2>&1 </dev/null &"
    exit 0
  fi
  mkdir -p "$LOGDIR"   # before the redirect: a missing directory fails silently under nohup
  # shellcheck disable=SC2086 # REST is this script's own validated arguments
  E11_RUN_LOG="$RUN_LOG" E11_RUN_STAMP="$STAMP" $LAUNCH bash scripts/pod_run_e11.sh $REST \
    >>"$RUN_LOG" 2>&1 </dev/null &
  pid=$!
  say "launched pod_run_e11.sh $REST detached (pid $pid); log $RUN_LOG"
  echo "RUN_LOG=$RUN_LOG"
  exit 0
fi

if [ "$DRY" != "1" ]; then
  mkdir -p "$LOGDIR" "$RESULTS"
  if [ -z "${E11_RUN_LOG:-}" ]; then echo "RUN_LOG=$RUN_LOG"; fi   # foreground: say where it goes
  exec >>"$RUN_LOG" 2>&1   # every run, detached or not, writes its own log
  if [ -s "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null && [ "$(cat "$PIDFILE")" != "$$" ]; then
    say "another chain is running (pid $(cat "$PIDFILE")); refusing"
    echo "E11_CHAIN_FAILED at=lock"
    exit 1
  fi
  echo "$$" > "$PIDFILE"
  trap 'rm -f "$PIDFILE"' EXIT
  set +x; set -a; if [ -f .env ]; then . ./.env; fi; set +a   # never trace the token
  export HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
  export HF_HOME="${HF_HOME:-/ws/hf_cache}"
fi
say "=== pod_run_e11.sh ${STEP:+step=$STEP }steps: $STEPS dry=$DRY commit=${EMX_GIT_COMMIT:-unknown} log=$RUN_LOG"

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
    download|bench|p2-acts|eval-train|logprobs|manifest) echo "$PY|$1" ;;
    p2-gen) echo "$VLLM_PY|$1" ;;
    smoke)
      echo "$PY|smoke --part render"
      echo "$VLLM_PY|smoke --part vllm" ;;
    pilot-r1)
      echo "$PY|pilot-r1 --part train"
      echo "$VLLM_PY|pilot-r1 --part gen"
      echo "$PY|pilot-r1 --part gen-hf"
      echo "$PY|pilot-r1 --part ready" ;;
    ladder-gen)
      echo "$VLLM_PY|ladder-gen --part vllm"
      echo "$PY|ladder-gen --part gen-hf"
      echo "$PY|ladder-gen --part ready" ;;
    lora-check)
      echo "$PY|lora-check --part hf"
      echo "$VLLM_PY|lora-check --part vllm"
      echo "$PY|lora-check --part score"
      echo "$PY|lora-check --part hf-bench" ;;
    generate)
      echo "$VLLM_PY|generate --part check"
      echo "$PY|generate --part hf-bench"
      echo "$VLLM_PY|generate --part vllm"
      echo "$PY|generate --part hf" ;;
  esac
}

run_calls() {  # run_calls STEP LOG: every call of STEP in order; returns the first failure's rc
  local step=$1 log=$2 rc=0 py dargs
  # bash 3.2: read the call list from a here-string, not a pipe (the loop must set rc here)
  while IFS='|' read -r py dargs; do
    [ -n "$py" ] || continue
    echo "+ $py scripts/e11_pod.py $dargs >>$log 2>&1"
    [ "$DRY" = "1" ] && continue
    [ -x "$py" ] || { say "python $py not found; run pod_setup.sh for E11"; rc=127; break; }
    say "start $step: $dargs (log $log)"
    # shellcheck disable=SC2086 # dargs is this script's own fixed arguments, split on purpose
    "$py" scripts/e11_pod.py $dargs >>"$log" 2>&1 </dev/null || rc=$?
    if [ "$rc" -eq 6 ] && [ "$py" = "$VLLM_PY" ]; then
      # driver exit 6: the vLLM part recorded an engine error and routed its work to the HF
      # path; the step goes on to its next part (code review r1a finding 1)
      say "$step: $dargs hit a vLLM engine error (recorded); continuing with the HF path"
      echo "E11_ENGINE_FALLBACK $step $dargs"
      rc=0
      continue
    fi
    [ "$rc" -eq 0 ] || break
  done <<EOF
$(calls "$step")
EOF
  return "$rc"
}

ladder_tags() {  # the number of E11_LADDER lines in the log so far
  grep -c '^E11_LADDER ' "$1" 2>/dev/null || true
}

LADDER_FAIL=""
run_ladder() {  # run_ladder LOG: the tick / gen loop; sets LADDER_FAIL on a timeout
  local log=$1 rc=0 n0 n1 t ticks=0
  if [ "$DRY" = "1" ]; then
    echo "+ $PY scripts/e11_pod.py ladder-tick >>$log 2>&1   (loop until done|kill|timeout)"
    run_calls ladder-gen "$log"
    echo "+ sleep $POLL   (on wait=r<k>)"
    return 0
  fi
  [ -x "$PY" ] || { say "python $PY not found; run pod_setup.sh for E11"; return 127; }
  touch "$log"
  while :; do
    ticks=$((ticks + 1))
    if [ "$ticks" -gt "$MAX_TICKS" ]; then say "ladder: $MAX_TICKS ticks without an outcome"; return 1; fi
    n0=$(ladder_tags "$log")
    rc=0
    "$PY" scripts/e11_pod.py ladder-tick >>"$log" 2>&1 </dev/null || rc=$?
    n1=$(ladder_tags "$log")
    t=""
    if [ "$n1" -gt "$n0" ]; then t=$(grep '^E11_LADDER ' "$log" | tail -n 1); fi
    if [ "$t" = "E11_LADDER timeout" ]; then LADDER_FAIL=timeout; return 1; fi
    [ "$rc" -eq 0 ] || return "$rc"
    case "$t" in
      "E11_LADDER need_gen="*)
        say "ladder: ${t#E11_LADDER }"
        run_calls ladder-gen "$log" || return $? ;;
      "E11_LADDER wait="*) sleep "$POLL" ;;
      "E11_LADDER done"|"E11_LADDER kill")
        say "ladder: ${t#E11_LADDER }"
        echo "$t"
        return 0 ;;
      *) say "ladder-tick printed no E11_LADDER line"; return 1 ;;
    esac
  done
}

run_step() {
  local step=$1 log rc
  log="$LOGDIR/${step}_$(date -u +%Y%m%dT%H%M%SZ).log"
  if [ "$step" != "manifest" ] && step_done "$step"; then
    say "skip $step: $(manifest "$step") verifies"
    echo "E11_STEP_DONE $step"
    return 0
  fi
  approved || {
    say "REFUSED $step: $APPROVAL missing or invalid"
    echo "E11_STEP_REFUSED $step"; return 3; }
  rc=0
  LADDER_FAIL=""
  if [ "$step" = "ladder" ]; then
    run_ladder "$log" || rc=$?
  else
    run_calls "$step" "$log" || rc=$?
  fi
  [ "$DRY" = "1" ] && return 0
  if [ "$rc" -ne 0 ]; then
    say "FAILED $step (exit $rc${LADDER_FAIL:+, $LADDER_FAIL}); see $log"
    if [ -n "$LADDER_FAIL" ]; then
      echo "E11_STEP_FAILED $step $LADDER_FAIL"
    else
      echo "E11_STEP_FAILED $step rc=$rc"
    fi
    [ "$rc" -eq 3 ] && return 3
    return 1
  fi
  if ! step_done "$step"; then
    say "FAILED $step: exited 0 but $(manifest "$step") is missing or does not verify"
    echo "E11_STEP_FAILED $step rc=0"
    return 1
  fi
  say "done $step"
  echo "E11_STEP_DONE $step"
}

FAILED=""
for step in $STEPS; do
  rc=0; run_step "$step" || rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" -eq 3 ] || gating "$step" || [ "$step" = "manifest" ]; then
      echo "E11_CHAIN_FAILED at=$step"
      exit "$rc"
    fi
    FAILED="${FAILED:+$FAILED,}$step"
  fi
done
echo "E11_CHAIN_DONE failed=${FAILED:-none}"
[ -z "$FAILED" ]
