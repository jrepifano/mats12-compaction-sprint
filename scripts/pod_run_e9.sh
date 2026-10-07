#!/usr/bin/env bash
# E9 pod job, ON the pod (unit E2; prereg §4.5, §4.6, §4.7 step 2, §4.13, §8.6 GO barrier, §8.7;
# implementation plan v4 "Frozen in U0": run inventories, DONE/FAILED semantics).
# Ported from scripts/pod_run_e3.sh at commit 7b15be1 (its bg/step wrappers, signal handling,
# failure staging and anchored tags), reshaped around E9's run directories.
#
#   cd /ws/emx && bash scripts/pod_run_e9.sh --run bg-c1:bg:1:cs:data/E9/streams/bg-c1.npy
#   cd /ws/emx && bash scripts/pod_run_e9.sh --run cpol-c5:cpol:5:cs:data/E9/streams/cpol-c5.npy \
#       --run cpol-c6:cpol:6:cq:data/E9/streams/cpol-c6.npy        # two runs on one pod
#   cd /ws/emx && bash scripts/pod_run_e9.sh --run smoke:ckpt:0:smoke:data/E9/streams/smoke.npy \
#       [--insert-loss]
#
# A run spec is RUN:ARM:CPT_SEED:KIND:STREAM (kind cq | cs | smoke; for cq/cs RUN must be
# contracts.run_id(ARM, CPT_SEED); the smoke run is exactly smoke:ckpt:0 on unit C's smoke
# stream, whose manifest (smoke: true) gives the 20-step CPT its seed; validated by
# `e9_gen.py runspec`). Runs execute in order; the job stops at the first failure.
# Every run's files live in $E9_RUNS_DIR/<RUN>/ (default results/E9/runs/<RUN>), which is what
# the laptop pulls; the continued-pretrained model goes to $E9_MODELS_DIR/<RUN> (outside the run
# dir) and every local adapter of the run under $E9_ADAPTERS_DIR/<RUN> (passed to unit E1's
# scripts as --adapters-root, which add <run_id>/<cell>/); both are deleted when the run ends
# (success or failure): no model is retained (§8.7). Full fine-tune adapters reach the Hub only
# through e9_q20.py full (private repos, asserted private there).
#
# cq / cs chain, per run:
#   stream check (sha256 vs its manifest; copied to stream_manifest.json) ->
#   checkpoint scoring (e9_q20.py score-base --arm ckpt --cpt-seed 0 --base hub: the pinned
#     checkpoint's own rows for the pre-fine-tune stance; scoring, not training) ->
#   GO barrier: wait for <run dir>/GO or the pod-level $E9_POD_GO (default results/E9/GO), up to
#     $E9_GO_TIMEOUT_S (10800) polling every $E9_GO_POLL_S (30); a pod-level GO is copied into
#     the run dir. Nothing trains before it. Timeout: FAILED (step go_barrier), exit 3 ->
#   CPT (e9_cpt.py train) -> held-out loss (e9_cpt.py heldout) -> masking check ->
#   six Q20 runs (e9_q20.py q20, one per adapter seed 11 12 13 x bad, good) ->
#   cs only: the two full fine-tunes (e9_q20.py full, seed 11, bad then good; pushed to private
#     repos; --noise-floor-file = the smoke's determinism.json when present; exit 3 = the §4.5
#     consistency check failed: E9_CONSISTENCY_FAIL, ft/consistency.json kept, the run FAILS)
#     then generation (e9_gen.py generate --kind cs) ->
#   delete the model -> DONE.json (e9_gen.py done: every file under the run dir with sha256; the
#     kind's RUN_INVENTORY must be present and, for cs and smoke, ft/consistency.json must pass;
#     else FAILED at step "done").
# smoke chain (no GO barrier; checkpoint steps use arm "ckpt", CPT seed 0, base hub, seed 11):
#   rounding test -> stream check (smoke: true) -> 20-step CPT on the smoke stream, at the
#   stream manifest's cpt_seed, with save ($E9_SMOKE_CPT_STEPS; peak memory and tokens/s are in
#   cpt/record.json) -> round trip (a Q20 peft fine-tune, bad seed 11, on the saved model into
#   roundtrip/ labelled arm bg, then vLLM base + that adapter, 2 answers -> roundtrip.json) ->
#   checkpoint Q20 (bad, good) -> determinism (repeats the bad Q20) -> E1b gap -> matched-step
#   samples of E1b's step-20
#   adapters (gen/) -> one timed full fine-tune (bad, --no-push) -> concurrency test (bad + good
#   full fine-tunes started together for $E9_CONC_STEPS steps, --no-push, in concurrency/{d}/;
#   wall-clock and nvidia-smi peak memory -> concurrency/conc.json) -> [--insert-loss: the insert
#   versions' mean LM loss under the checkpoint; optional and non-fatal] -> timing.json ->
#   delete the model -> DONE.json.
#
# Failure: any step's non-zero exit (or a planted failure, E9_FAIL_AT=<step> or <run>:<step>), a
# signal, or a refused DONE writes <run dir>/FAILED.json (step, error, exit code, every file
# present with its sha256, the missing required files). The run dir itself is the staging area:
# every partial record stays in it for pulling. Nothing is written to a run's pod.log after its
# DONE.json or FAILED.json, so the listed hashes stay valid. A rerun skips a run whose DONE.json
# exists; otherwise every file of the previous attempt (all but the laptop's GO) is archived
# under <run dir>/attempts/<n>/ and a new ATTEMPT.json (immutable attempt id, recorded in DONE
# and FAILED) is written before anything runs, and stale model/adapter dirs are deleted, so no
# record, reference or generation of an earlier attempt is reused or mixed into DONE.
# Termination (signal, or the exit path with a step still running): SIGTERM to the step's
# process group, then up to $E9_KILL_GRACE_S (60) for the trainer to flush its failed record,
# then SIGKILL.
# Logs: every line of a run (its commands' output included) goes to <run dir>/pod.log; the job's
# own log is $E9_LOG_DIR/pod_run_e9.<UTC>.log (also copied to results/E9/logs/). Tags for
# watchers (grep these, never a bare DONE): `E9_JOB_START`, `E9_RUN_START <run>`,
# `E9_RUN_DONE <run>`, `E9_RUN_FAIL <run> <step> <reason>`, `E9_JOB_DONE`,
# `E9_JOB_FAIL <run> <step> <reason>`, `E9_CONSISTENCY_FAIL <run> <step>`. Every step appends
# {run, step, rc, seconds, utc} to <run dir>/bench.jsonl (C_Q and C_S are measured from it).
# Exit codes: 0 done; 1 a step failed; 2 usage; 3 GO barrier timeout; 130 / 143 SIGINT / SIGTERM.
#
# Environment: E9_DRY_RUN=1 prints every command ("+ " lines) and runs nothing but the contract
# queries; E9_VENVS (default /ws: venv-peft for training and scoring, venv-vllm for generation);
# E9_RUNS_DIR, E9_MODELS_DIR (/ws/models), E9_ADAPTERS_DIR (/ws/adapters/e9), E9_LOG_DIR (logs),
# E9_POD_GO, E9_GO_TIMEOUT_S, E9_GO_POLL_S; E9_NOISE_FLOOR_FILE (default
# <runs dir>/smoke/determinism.json); E9_CUTS (contracts.CUTS for generation, space separated);
# E9_NO_VULNERABLE=1; E9_SMOKE_CPT_STEPS (20); E9_CONC_STEPS (100); E9_KILL_GRACE_S (60);
# E9_FAIL_AT; E9_ENV_FILE (default ./.env, sourced with set -a; never traced); E9_INSERT_VERSIONS,
# E9_INSERT_MANIFEST (defaults under data/E9/insert/).
# bash 3.2 compatible (no associative arrays, mapfile, ${x,,}, |& or wait -n).
set -euo pipefail

usage() {
  echo "usage: pod_run_e9.sh --run RUN:ARM:CPT_SEED:KIND:STREAM [--run ...] [--insert-loss]" >&2
  exit 2
}

SPECS=""; INSERT_LOSS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --run) [ $# -ge 2 ] || usage; SPECS="$SPECS $2"; shift 2 ;;
    --insert-loss) INSERT_LOSS=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument $1" >&2; usage ;;
  esac
done
[ -n "$SPECS" ] || usage

DRY="${E9_DRY_RUN:-0}"
cd "$(dirname "$0")/.."
VENVS="${E9_VENVS:-/ws}"
PEFT_PY="$VENVS/venv-peft/bin/python"
VLLM_PY="$VENVS/venv-vllm/bin/python"
if [ -x "$PEFT_PY" ]; then PYH="$PEFT_PY"; else PYH="python3"; fi  # stdlib helpers

CONSTS=$($PYH scripts/e9_gen.py consts) || { echo "contract query failed" >&2; exit 2; }
eval "$CONSTS"
RUNS_DIR="${E9_RUNS_DIR:-$RUNS_DIR_DEFAULT}"
MODELS_ROOT="${E9_MODELS_DIR:-/ws/models}"
ADAPTERS_ROOT="${E9_ADAPTERS_DIR:-/ws/adapters/e9}"
LOG_DIR="${E9_LOG_DIR:-logs}"
POD_GO="${E9_POD_GO:-$(dirname "$RUNS_DIR")/$GO_NAME}"
GO_TIMEOUT_S="${E9_GO_TIMEOUT_S:-10800}"
GO_POLL_S="${E9_GO_POLL_S:-30}"
CUTS="${E9_CUTS:-}"
NO_VULN="${E9_NO_VULNERABLE:-0}"
NOISE_FLOOR="${E9_NOISE_FLOOR_FILE:-$RUNS_DIR/smoke/determinism.json}"
SMOKE_CPT_STEPS="${E9_SMOKE_CPT_STEPS:-20}"
CONC_STEPS="${E9_CONC_STEPS:-100}"
KILL_GRACE_S="${E9_KILL_GRACE_S:-60}"  # the trainer flushes its record on SIGTERM
FAIL_AT="${E9_FAIL_AT:-}"
INSERT_VERSIONS="${E9_INSERT_VERSIONS:-data/E9/insert/versions.jsonl}"
INSERT_MANIFEST="${E9_INSERT_MANIFEST:-data/E9/insert/insert_manifest.json}"
ENV_FILE="${E9_ENV_FILE:-./.env}"  # secrets (HF token for E1b adapters and private pushes)

# Validate every run spec before anything runs (usage error: exit 2).
for spec in $SPECS; do
  $PYH scripts/e9_gen.py runspec "$spec" >/dev/null || usage
done

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
JOB_LOG="$LOG_DIR/pod_run_e9.$STAMP.log"
RUN=""; ARM=""; SEED=""; KIND=""; STREAM=""; R=""; MODEL_DIR=""; AD=""
RUN_LOG=""; STEP="start"; REASON=""; CHILDREN=""; LAST_PID=""; ATTEMPT="dry"

if [ "$DRY" != "1" ]; then
  mkdir -p "$LOG_DIR" "$RUNS_DIR"  # before the redirect (nohup + a missing dir fails silently)
  exec >>"$JOB_LOG" 2>&1
  set +x; set -a; if [ -f "$ENV_FILE" ]; then . "$ENV_FILE"; fi; set +a  # never trace the token
  export HF_HOME="${HF_HOME:-/ws/hf_cache}" WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
fi

say() {  # to the job log, and to the current run's pod.log while it is open
  local line
  line="$(date -u +%FT%TZ) [e9${RUN:+-$RUN}] $*"
  echo "$line"
  if [ -n "$RUN_LOG" ]; then echo "$line" >> "$RUN_LOG"; fi
}
emit() {  # a command line ("+ ..."), to the job log and the run log
  echo "+ $*"
  if [ -n "$RUN_LOG" ]; then echo "+ $*" >> "$RUN_LOG"; fi
}

sha_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}

# ------------------------------------------------------------------------------------------------
# children, signals, exit
# ------------------------------------------------------------------------------------------------
start_child() {  # start_child OUT CMD...: background child in its own process group -> LAST_PID
  local out="$1"; shift
  set -m
  "$@" >>"$out" 2>&1 &
  LAST_PID=$!
  set +m
  CHILDREN="$CHILDREN $LAST_PID"
}

wait_child() {  # wait_child PID: its exit code
  local rc=0 p keep=""
  wait "$1" || rc=$?
  for p in $CHILDREN; do [ "$p" = "$1" ] || keep="$keep $p"; done
  CHILDREN="$keep"
  return "$rc"
}

stop_children() {  # TERM every child's process group, KILL after KILL_GRACE_S
  local p i
  for p in $CHILDREN; do kill -TERM -- "-$p" 2>/dev/null || true; done
  for p in $CHILDREN; do
    i=0
    while kill -0 -- "-$p" 2>/dev/null && [ "$i" -lt "$KILL_GRACE_S" ]; do
      sleep 1; i=$((i + 1))
    done
    if kill -0 -- "-$p" 2>/dev/null; then
      say "process group $p still alive after ${KILL_GRACE_S}s; KILL"
      kill -KILL -- "-$p" 2>/dev/null || true
    fi
    wait "$p" 2>/dev/null || true
  done
  CHILDREN=""
}

cleanup_model() {  # the run's model and local adapters: never retained (§8.7)
  local d
  for d in "$MODEL_DIR" "$AD"; do
    [ -n "$d" ] || continue
    case "$d" in "$MODELS_ROOT"/?*|"$ADAPTERS_ROOT"/?*) ;; *) continue ;; esac
    if [ "$DRY" = "1" ]; then emit "rm -rf $d"; continue; fi
    if [ -e "$d" ]; then rm -rf "$d" && say "deleted $d"; fi
  done
}

on_exit() {
  local rc=$? log
  trap - EXIT
  [ -z "$CHILDREN" ] || stop_children
  if [ "$rc" -ne 0 ] && [ -n "$RUN" ]; then
    [ -n "$REASON" ] || REASON="exit $rc"
    say "E9_RUN_FAIL $RUN $STEP $REASON"
    cleanup_model || true
    RUN_LOG=""  # nothing more goes to pod.log: FAILED.json hashes it
    if [ "$DRY" = "1" ]; then
      emit "write $R/$FAILED_NAME (step $STEP)"
    else
      $PYH scripts/e9_gen.py failed --run-dir "$R" --run "$RUN" --kind "$KIND" --arm "$ARM" \
        --cpt-seed "$SEED" --step "$STEP" --error "$REASON" --rc "$rc" \
        || say "WRITING $FAILED_NAME FAILED"
      say "wrote $R/$FAILED_NAME"
    fi
  fi
  RUN_LOG=""
  if [ "$rc" -eq 0 ]; then say "E9_JOB_DONE"; else
    say "E9_JOB_FAIL ${RUN:-job} $STEP ${REASON:-exit $rc}"
  fi
  if [ "$DRY" != "1" ]; then  # the job log, with its final tag, beside the runs
    log="$(dirname "$RUNS_DIR")/logs"
    { mkdir -p "$log" && cp -p "$JOB_LOG" "$log/"; } 2>/dev/null || true
  fi
  exit "$rc"
}
trap on_exit EXIT

on_signal() {
  local sig="$1"
  trap '' TERM INT
  REASON="signal $sig"
  say "caught SIG$sig; stopping step process groups:${CHILDREN:- none}"
  stop_children
  if [ "$sig" = "INT" ]; then exit 130; fi
  exit 143
}
trap 'on_signal TERM' TERM
trap 'on_signal INT' INT

fail() { STEP="$1"; REASON="$2"; exit "${3:-1}"; }

planted() {  # planted NAME: E9_FAIL_AT names this step (or <run>:<step>)
  [ -n "$FAIL_AT" ] && { [ "$FAIL_AT" = "$1" ] || [ "$FAIL_AT" = "$RUN:$1" ]; }
}

bench() {  # bench STEP RC SECONDS
  printf '{"run": "%s", "step": "%s", "rc": %d, "seconds": %d, "utc": "%s"}\n' \
    "$RUN" "$1" "$2" "$3" "$(date -u +%FT%TZ)" >> "$R/bench.jsonl"
}

step() {  # step NAME CMD...: print; run unless dry (output to the run's pod.log); bench row
  local name="$1" t0 rc=0; shift
  STEP="$name"
  emit "$*"
  if planted "$name"; then say "planted failure at $name (E9_FAIL_AT)"; return 97; fi
  [ "$DRY" = "1" ] && return 0
  t0=$(date +%s)
  start_child "$RUN_LOG" "$@"
  wait_child "$LAST_PID" || rc=$?
  bench "$name" "$rc" "$(( $(date +%s) - t0 ))"
  [ "$rc" -eq 0 ] || say "step $name exited $rc"
  return "$rc"
}

# ------------------------------------------------------------------------------------------------
# pieces
# ------------------------------------------------------------------------------------------------
wait_go() {  # the analysis-review barrier (§8.6): before any training
  local f="$R/$GO_NAME" waited=0 src=""
  STEP="go_barrier"
  emit "wait for $f or $POD_GO (timeout ${GO_TIMEOUT_S}s, poll ${GO_POLL_S}s)"
  if planted go_barrier; then say "planted failure at go_barrier (E9_FAIL_AT)"; return 1; fi
  [ "$DRY" = "1" ] && return 0
  say "waiting for GO"
  while :; do
    if [ -f "$f" ]; then src="$f"; break; fi
    if [ -f "$POD_GO" ]; then src="$POD_GO"; break; fi
    if [ "$waited" -ge "$GO_TIMEOUT_S" ]; then return 1; fi
    sleep "$GO_POLL_S" & wait $! || true
    waited=$((waited + GO_POLL_S))
    if [ $((waited % 600)) -lt "$GO_POLL_S" ]; then say "still waiting for GO (${waited}s)"; fi
  done
  if [ "$src" != "$f" ]; then cp -p "$src" "$f"; fi  # the barrier's evidence, in the run dir
  say "GO from $src after ${waited}s: $(sha_of "$f" | cut -c1-64)"
}

gen_flags() {  # generation flags from the environment
  local out=""
  if [ -n "$CUTS" ]; then out="--cuts $CUTS"; fi
  if [ "$NO_VULN" = "1" ]; then out="$out --no-vulnerable"; fi
  echo "$out"
}

full_rc_ok() {  # full_rc_ok NAME RC: 0 only; exit 3 is the failed §4.5 consistency check (fatal)
  if [ "$2" -eq 3 ]; then say "E9_CONSISTENCY_FAIL $RUN $1 (ft/consistency.json kept)"; fi
  return "$2"
}

full_reason() {  # full_reason RC
  if [ "$1" -eq 3 ]; then echo "consistency check failed (exit 3; ft/consistency.json)"; else
    echo "full fine-tune failed (exit $1)"; fi
}

run_pair() {  # run_pair NAME OUT_JSON MEMFILE STEPS: bad and good full fine-tunes on the
  # checkpoint started together on the GPU (concurrency/{d}/), wall-clock and peak memory
  local name="$1" out="$2" mem="$3" n="$4" d t0 ps="" pb pg rcb=0 rcg=0 secs
  STEP="$name"
  for d in $DATAS; do
    emit "(together) $PEFT_PY scripts/e9_q20.py full --run-dir $R/concurrency/$d --arm ckpt \
--cpt-seed 0 --base hub --datas $d --no-push --max-steps $n --adapters-root $AD/concurrency/$d"
  done
  if planted "$name"; then say "planted failure at $name (E9_FAIL_AT)"; return 97; fi
  [ "$DRY" = "1" ] && return 0
  mkdir -p "$R/concurrency/bad" "$R/concurrency/good" "$(dirname "$mem")" "$(dirname "$out")"
  if command -v nvidia-smi >/dev/null 2>&1; then
    start_child "$mem" nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -lms 1000
    ps=$LAST_PID
  fi
  t0=$(date +%s)
  start_child "$RUN_LOG" "$PEFT_PY" scripts/e9_q20.py full --run-dir "$R/concurrency/bad" \
    --arm ckpt --cpt-seed 0 --base hub --datas bad --no-push --max-steps "$n" \
    --adapters-root "$AD/concurrency/bad"
  pb=$LAST_PID
  start_child "$RUN_LOG" "$PEFT_PY" scripts/e9_q20.py full --run-dir "$R/concurrency/good" \
    --arm ckpt --cpt-seed 0 --base hub --datas good --no-push --max-steps "$n" \
    --adapters-root "$AD/concurrency/good"
  pg=$LAST_PID
  wait_child "$pb" || rcb=$?
  wait_child "$pg" || rcg=$?
  secs=$(( $(date +%s) - t0 ))
  if [ -n "$ps" ]; then kill -TERM -- "-$ps" 2>/dev/null || true; wait_child "$ps" || true; fi
  bench "$name" "$(( rcb > rcg ? rcb : rcg ))" "$secs"
  $PYH scripts/e9_gen.py conc --seconds "$secs" --rcs "$rcb" "$rcg" --gpu-mem "$mem" \
    --steps "$n" --out "$out" >> "$RUN_LOG" 2>&1 || say "concurrency record failed"
  say "$name: bad rc=$rcb good rc=$rcg in ${secs}s"
  full_rc_ok "$name bad" "$rcb" && full_rc_ok "$name good" "$rcg"
}

start_run() {  # start_run SPEC: parse, open the run dir and its pod.log
  eval "$($PYH scripts/e9_gen.py runspec "$1")"
  R="$RUNS_DIR/$RUN"; MODEL_DIR="$MODELS_ROOT/$RUN"; AD="$ADAPTERS_ROOT/$RUN"
  STEP="start"; REASON=""
  if [ -f "$R/$DONE_NAME" ]; then
    say "$RUN: $DONE_NAME present; skipping (done earlier)"
    RUN=""
    return 1
  fi
  if [ "$DRY" = "1" ]; then
    emit "mkdir -p $R"
    emit "$PYH scripts/e9_gen.py begin-attempt --run-dir $R --run $RUN --stamp $STAMP"
  else
    mkdir -p "$R"
    ATTEMPT=$($PYH scripts/e9_gen.py begin-attempt --run-dir "$R" --run "$RUN" --stamp "$STAMP") \
      || { say "cannot archive the previous attempt of $RUN"; RUN=""; exit 1; }
    cleanup_model  # a crashed earlier attempt's model or adapters are never reused
    RUN_LOG="$R/pod.log"
  fi
  say "E9_RUN_START $RUN attempt=$ATTEMPT arm=$ARM cpt_seed=$SEED kind=$KIND stream=$STREAM \
model=$MODEL_DIR commit=${EMX_GIT_COMMIT:-unknown} dry=$DRY"
  return 0
}

finish_run() {  # delete the model, then DONE.json (the run log is closed first)
  cleanup_model
  STEP="done"
  say "run complete; writing $DONE_NAME"
  RUN_LOG=""  # nothing more goes to pod.log: DONE.json hashes it
  if planted done; then fail done "planted failure at done (E9_FAIL_AT)"; fi
  if [ "$DRY" = "1" ]; then
    emit "$PYH scripts/e9_gen.py done --run-dir $R --run $RUN --kind $KIND --arm $ARM \
--cpt-seed $SEED"
  elif ! $PYH scripts/e9_gen.py done --run-dir "$R" --run "$RUN" --kind "$KIND" --arm "$ARM" \
      --cpt-seed "$SEED"; then
    fail done "DONE refused: run inventory incomplete (see the job log)"
  fi
  say "E9_RUN_DONE $RUN"
  RUN=""
}

# ------------------------------------------------------------------------------------------------
# chains
# ------------------------------------------------------------------------------------------------
train_chain() {  # cq and cs
  local k d rc nf=""
  step stream_check "$PYH" scripts/e9_gen.py stream-check --stream "$STREAM" --run-id "$RUN" \
    --out "$R/stream_manifest.json" || fail stream_check "stream check failed (exit $?)"
  step score_base "$PEFT_PY" scripts/e9_q20.py score-base --run-dir "$R" --arm ckpt \
    --cpt-seed 0 --base hub --adapters-root "$AD" \
    || fail score_base "checkpoint scoring failed (exit $?)"
  wait_go || fail go_barrier "no GO after ${GO_TIMEOUT_S}s" 3
  step cpt "$PEFT_PY" scripts/e9_cpt.py train --stream "$STREAM" --run-id "$RUN" \
    --cpt-seed "$SEED" --out "$R" --model-out "$MODEL_DIR" || fail cpt "CPT failed (exit $?)"
  step heldout "$PEFT_PY" scripts/e9_cpt.py heldout --model "$MODEL_DIR" \
    --out "$R/cpt/heldout_loss.json" || fail heldout "held-out loss failed (exit $?)"
  step masking "$PEFT_PY" scripts/e9_masking_check.py --base "$MODEL_DIR" \
    --out "$R/masking_check.json" || fail masking "masking check failed (exit $?)"
  for k in $ADAPTER_SEEDS; do
    for d in $DATAS; do
      step "q20_${d}_a$k" "$PEFT_PY" scripts/e9_q20.py q20 --run-dir "$R" --arm "$ARM" \
        --cpt-seed "$SEED" --base "$MODEL_DIR" --seeds "$k" --datas "$d" --adapters-root "$AD" \
        || fail "q20_${d}_a$k" "Q20 fine-tune failed (exit $?)"
    done
  done
  [ "$KIND" = "cs" ] || return 0
  if [ -f "$NOISE_FLOOR" ] || [ "$DRY" = "1" ]; then nf="--noise-floor-file $NOISE_FLOOR"; else
    say "no noise floor file $NOISE_FLOOR: the consistency tolerance is the contracts' alone"
  fi
  for d in $DATAS; do
    rc=0
    # shellcheck disable=SC2086
    step "full_$d" "$PEFT_PY" scripts/e9_q20.py full --run-dir "$R" --arm "$ARM" \
      --cpt-seed "$SEED" --base "$MODEL_DIR" --datas "$d" --adapters-root "$AD" $nf || rc=$?
    full_rc_ok "full_$d" "$rc" || fail "full_$d" "$(full_reason "$rc")"
  done
  # shellcheck disable=SC2046
  step gen "$VLLM_PY" scripts/e9_gen.py generate --kind cs --run-dir "$R" --base "$MODEL_DIR" \
    --arm "$ARM" --cpt-seed "$SEED" --adapters-root "$AD" $(gen_flags) \
    || fail gen "generation failed (exit $?)"
}

insert_status() {  # insert_status STATUS REASON: insert_loss/status.json (measured|deferred|failed)
  local out="$R/insert_loss/status.json"
  if [ "$DRY" = "1" ]; then emit "write $out ($1: $2)"; return 0; fi
  $PYH scripts/e9_gen.py insert-status --out "$out" --status "$1" --reason "$2" \
    ${3:+--counts-file "$3"} >> "$RUN_LOG" 2>&1 || say "writing $out failed"
  [ "$1" = "failed" ] && say "E9_INSERT_LOSS_FAIL $RUN $2"
  return 0
}

insert_loss() {  # the insert versions' mean LM loss under the checkpoint (smoke; §8 "Smoke pod")
  # Never silent: insert_loss/status.json says measured, deferred (flag absent or no insert yet;
  # then the first cost-and-noise pod measures it) or failed (with the failed versions).
  local tmp="${TMPDIR:-/tmp}/e9_insert_$STAMP" v n="<frozen n_docs>" why=""
  if [ "$INSERT_LOSS" != "1" ]; then insert_status deferred "--insert-loss not given"; return 0; fi
  if [ ! -f "$INSERT_VERSIONS" ] && [ "$DRY" != "1" ]; then
    insert_status deferred "$INSERT_VERSIONS not present"
    return 0
  fi
  if ! step insert_split "$PYH" scripts/e9_gen.py split-versions --versions "$INSERT_VERSIONS" \
      --manifest "$INSERT_MANIFEST" --out-dir "$tmp" --n-out "$tmp/n_docs"; then
    insert_status failed "split or count check against $INSERT_MANIFEST failed"
    return 0
  fi
  [ "$DRY" = "1" ] || n=$(cat "$tmp/n_docs")
  for v in orig corr para; do
    step "insert_loss_$v" "$PEFT_PY" scripts/e9_cpt.py heldout --model hub \
      --docs "$tmp/$v.jsonl" --expect-n "$n" --out "$R/insert_loss/$v.json" || why="$why $v"
  done
  if [ -z "$why" ]; then insert_status measured "3 versions x $n documents" "$tmp/counts.json"
  else insert_status failed "measurement failed for:$why" "$tmp/counts.json"; fi
  [ "$DRY" = "1" ] || rm -rf "$tmp"
}

smoke_chain() {  # spec smoke:ckpt:0; the CPT seed is the smoke stream's own (its manifest)
  local d rc=0 cs="<stream cpt_seed>"
  step rounding "$PEFT_PY" scripts/e9_rounding_test.py --model hub --out "$R/rounding.json" \
    || fail rounding "rounding test failed (exit $?)"
  step stream_check "$PYH" scripts/e9_gen.py stream-check --stream "$STREAM" --run-id "$RUN" \
    --smoke --out "$R/stream_manifest.json" || fail stream_check "stream check failed (exit $?)"
  if [ "$DRY" != "1" ]; then
    cs=$($PYH scripts/e9_gen.py stream-seed --stream "$STREAM") \
      || fail stream_check "no CPT seed in the smoke stream's manifest"
  fi
  step cpt "$PEFT_PY" scripts/e9_cpt.py train --stream "$STREAM" --run-id "$RUN" \
    --cpt-seed "$cs" --out "$R" --model-out "$MODEL_DIR" --max-steps "$SMOKE_CPT_STEPS" \
    || fail cpt "CPT failed (exit $?)"
  # the round trip fine-tunes the saved (background-only) model: E1's scripts need a CPT arm and
  # seed >= 1 for a directory base, so it is labelled bg at the stream's seed (roundtrip/ only)
  step roundtrip_train "$PEFT_PY" scripts/e9_q20.py q20 --run-dir "$R/roundtrip" --arm bg \
    --cpt-seed "$cs" --base "$MODEL_DIR" --seeds "$FULL_SEED" --datas bad \
    --adapters-root "$AD/roundtrip" || fail roundtrip_train "round-trip fine-tune failed (exit $?)"
  step roundtrip_gen "$VLLM_PY" scripts/e9_gen.py roundtrip --base "$MODEL_DIR" \
    --adapter "$AD/roundtrip" --out "$R/roundtrip.json" \
    || fail roundtrip_gen "round trip failed (exit $?)"
  for d in $DATAS; do  # before determinism, which repeats the bad run and needs the original
    step "q20_${d}_a$FULL_SEED" "$PEFT_PY" scripts/e9_q20.py q20 --run-dir "$R" --arm ckpt \
      --cpt-seed 0 --base hub --seeds "$FULL_SEED" --datas "$d" --adapters-root "$AD" \
      || fail "q20_${d}_a$FULL_SEED" "checkpoint Q20 failed (exit $?)"
  done
  step determinism "$PEFT_PY" scripts/e9_q20.py determinism --run-dir "$R" --arm ckpt \
    --cpt-seed 0 --base hub --adapters-root "$AD" \
    || fail determinism "determinism check failed (exit $?)"
  step e1b_gap "$PEFT_PY" scripts/e9_q20.py e1b-gap --run-dir "$R" --adapters-root "$AD" \
    || fail e1b_gap "E1b gap failed (exit $?)"
  # shellcheck disable=SC2046
  step gen_matched "$VLLM_PY" scripts/e9_gen.py generate --kind smoke --run-dir "$R" \
    --base hub $(gen_flags) || fail gen_matched "matched-step samples failed (exit $?)"
  step full_bad "$PEFT_PY" scripts/e9_q20.py full --run-dir "$R" --arm ckpt --cpt-seed 0 \
    --base hub --datas bad --no-push --adapters-root "$AD" || rc=$?
  full_rc_ok full_bad "$rc" || fail full_bad "$(full_reason "$rc")"
  run_pair concurrency "$R/concurrency/conc.json" "$R/concurrency/gpu_mem.csv" "$CONC_STEPS" \
    || fail concurrency "concurrency test failed"
  insert_loss
  step timing "$PYH" scripts/e9_gen.py timing --run-dir "$R" --out "$R/timing.json" \
    || fail timing "timing record failed (exit $?)"
}

# ------------------------------------------------------------------------------------------------
say "E9_JOB_START runs=[${SPECS# }] insert_loss=$INSERT_LOSS cuts=[$CUTS] dry=$DRY \
commit=${EMX_GIT_COMMIT:-unknown} pod=${RUNPOD_POD_ID:-none}"
for spec in $SPECS; do
  start_run "$spec" || continue
  if [ "$KIND" = "smoke" ]; then smoke_chain; else train_chain; fi
  finish_run
done
exit 0
