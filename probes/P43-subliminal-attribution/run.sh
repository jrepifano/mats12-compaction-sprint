#!/usr/bin/env bash
# P43 pod entry point (scripts/probe_pod.py run contract; plan.md "Automatic rules", on the pod).
# The committed file `phase` (env P43_PHASE overrides) picks the session:
#   session1: download (student + teacher) -> smoke (gates the bulk run) -> train the gate
#             mixtures (seed 0) -> eval (base + gate adapters) -> gate (results/gate.json; maybe
#             train+eval mix075 and gate again) -> score (likelihoods + attribution on the f*
#             student) -> teacher (delta_teacher under Gemma)
#   session2: download -> train the stage-2 keys in `stage2_keys` -> eval them
# Steps run p43/pod.py in /ws/venv-peft (training, scoring) or /ws/venv-vllm (eval). A failed step
# stops the chain: rc 11 download, 12 smoke, 13 train, 14 eval, 15 score, 16 teacher; rc 2 = a
# missing venv/input or bad phase; rc 4 = the wall-clock guard stopped the chain before a step.
# A gate stop (no fraction transfers, or the setting did not replicate) is a result, not a
# failure: the chain skips scoring and ends rc 0. The manifest and the last log line
# `PROBE_DONE rc=<n>` are always written. P43_DRY_RUN=1 prints the plan and exits. bash 3.2.
set -uo pipefail
cd "$(dirname "$0")"
echo "P43 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
PHASE="${P43_PHASE:-$(tr -d '[:space:]' < phase 2>/dev/null || echo unset)}"
case "$PHASE" in
  session1) DEFAULT_GUARD=11700 ;;   # 3.25 h of run.sh
  session2) DEFAULT_GUARD=21600 ;;   # 6.0 h
  *) DEFAULT_GUARD=0 ;;
esac
GUARD_S="${P43_GUARD_S:-$DEFAULT_GUARD}"
GATE_KEYS="mix000_s0 mix025_s0 mix050_s0 mix100_s0"
echo "P43 phase=$PHASE guard=${GUARD_S}s"
if [ "${P43_DRY_RUN:-0}" = 1 ]; then
  echo "P43 dry: phase $PHASE, gate keys: $GATE_KEYS"
  [ -f stage2_keys ] && echo "P43 dry: stage2 keys: $(tr '\n' ' ' < stage2_keys)"
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
T0="$(date +%s)"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=60 WANDB_MODE=offline
export TOKENIZERS_PARALLELISM=false PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
PEFT=/ws/venv-peft/bin/python
VLLM=/ws/venv-vllm/bin/python
RC=0
[ "$DEFAULT_GUARD" = 0 ] && { echo "P43 bad phase '$PHASE'"; RC=2; }
for PY in "$PEFT" "$VLLM"; do
  [ -x "$PY" ] || { echo "P43 missing $PY (pod_setup.sh --only e11)"; RC=2; }
done
for F in data/manifest.json data/mix/mix000.jsonl data/mix/mix100.jsonl data/mix/heldout.jsonl \
         data/raw/reagan_sentiment_questions.py; do
  [ -s "$F" ] || { echo "P43 missing $F"; RC=2; }
done
if [ "$RC" = 0 ] && [ -f tda_requirements.txt ]; then
  # extra attribution deps into the peft venv without touching torch/transformers/peft
  "$PEFT" -m pip install --quiet --no-deps -r tda_requirements.txt || { echo "P43 tda deps failed"; RC=2; }
fi

step_env() {
  echo "== $(date -u +%FT%TZ) step=$1 elapsed=$(( $(date +%s) - T0 ))s"
  nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader 2>/dev/null || true
  df -h /ws 2>/dev/null | tail -1 || true
}

run_step() {  # run_step NAME FAILRC PY ARGS...  (sets RC; returns the python rc)
  local name=$1 fail=$2 py=$3; shift 3
  [ "$RC" = 0 ] || return 1
  local el=$(( $(date +%s) - T0 ))
  if [ "$el" -ge "$GUARD_S" ]; then
    echo "P43 guard: ${el}s >= ${GUARD_S}s, not starting $name"; RC=4; return 1
  fi
  step_env "$name"
  local s0; s0="$(date +%s)"
  "$py" -m p43.pod "$@"
  local prc=$?
  echo "== $(date -u +%FT%TZ) step=$name python_rc=$prc seconds=$(( $(date +%s) - s0 ))"
  return $prc
}

if [ "$PHASE" = session1 ]; then
  run_step download 11 "$PEFT" download --teacher || { [ "$RC" = 0 ] && RC=11; }
  run_step smoke 12 "$PEFT" smoke || { [ "$RC" = 0 ] && RC=12; }
  # shellcheck disable=SC2086
  run_step train 13 "$PEFT" train --tag gate --keys $GATE_KEYS || { [ "$RC" = 0 ] && RC=13; }
  # shellcheck disable=SC2086
  run_step eval 14 "$VLLM" eval --tag gate --base --keys $GATE_KEYS || { [ "$RC" = 0 ] && RC=14; }
  GATE_RC=99
  if [ "$RC" = 0 ]; then
    "$PEFT" -m p43.pod gate; GATE_RC=$?
    if [ "$GATE_RC" = 3 ]; then
      run_step train 13 "$PEFT" train --tag gate75 --keys mix075_s0 || { [ "$RC" = 0 ] && RC=13; }
      run_step eval 14 "$VLLM" eval --tag gate75 --keys mix075_s0 || { [ "$RC" = 0 ] && RC=14; }
      [ "$RC" = 0 ] && { "$PEFT" -m p43.pod gate; GATE_RC=$?; }
    fi
    echo "P43 gate rc=$GATE_RC (0 f* chosen, 4 stop)"
  fi
  if [ "$RC" = 0 ] && [ "$GATE_RC" = 0 ]; then
    run_step score 15 "$PEFT" score || { [ "$RC" = 0 ] && RC=15; }
    run_step teacher 16 "$PEFT" teacher || { [ "$RC" = 0 ] && RC=16; }
    # the response-mode attribution variant only if time is left (guard minus 30 min)
    if [ "$RC" = 0 ] && [ $(( $(date +%s) - T0 )) -lt $(( GUARD_S - 1800 )) ]; then
      run_step score 15 "$PEFT" score --response-mode || { [ "$RC" = 0 ] && RC=15; }
    fi
  fi
elif [ "$PHASE" = session2 ]; then
  KEYS="$(tr '\n' ' ' < stage2_keys 2>/dev/null)"
  [ -n "$KEYS" ] || { echo "P43 missing stage2_keys"; RC=2; }
  run_step download 11 "$PEFT" download || { [ "$RC" = 0 ] && RC=11; }
  # shellcheck disable=SC2086
  run_step train 13 "$PEFT" train --tag stage2 --keys $KEYS || { [ "$RC" = 0 ] && RC=13; }
  # evaluate whatever trained, even if the guard stopped training early
  if [ "$RC" = 0 ] || [ "$RC" = 4 ]; then
    DONE_KEYS=""
    for K in $KEYS; do [ -f "results/train/$K/run.json" ] && DONE_KEYS="$DONE_KEYS $K"; done
    SAVED=$RC; RC=0; GUARD_S=$(( GUARD_S + 2400 ))
    # shellcheck disable=SC2086
    run_step eval 14 "$VLLM" eval --tag stage2 --keys $DONE_KEYS || { [ "$RC" = 0 ] && RC=14; }
    [ "$RC" = 0 ] && RC=$SAVED
  fi
fi

echo "== $(date -u +%FT%TZ) step=manifest elapsed=$(( $(date +%s) - T0 ))s"
( cd results && find . -type f ! -name MANIFEST.sha256 -print0 \
    | sort -z | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) \
  && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P43 end $(date -u +%FT%TZ) rc=$RC elapsed=$(( $(date +%s) - T0 ))s"
echo "PROBE_DONE rc=$RC"
