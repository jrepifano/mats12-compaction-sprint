#!/usr/bin/env bash
# P20 pod entry point (scripts/probe_pod.py run contract; plan.md "Automatic rules", on the pod):
#   download  (/ws/venv-peft)  base snapshot at the pin; inputs and the EM adapters sha-checked
#   train     (/ws/venv-peft)  the three good-medical controls good_neu_s{0,1,2} (E11's recipe)
#   generate  (/ws/venv-vllm)  one engine: E11's chat plan for the controls, then the screen's
#                              K_DRAWS draws per (context, adapter) for all 7 adapters
#   manifest                   results/MANIFEST.sha256 over every result file (aside, then moved)
# A failed step stops the chain: rc 11 download, 12 train, 13 generate; rc 2 = a missing venv or
# input; rc 4 = the soft wall-clock guard (P20_GUARD_S, default 1.5 h) stopped the chain before a
# new step. The manifest and the last log line `PROBE_DONE rc=<n>` are always written.
# Optional env: P20_DRY_RUN=1 (print the steps, touch nothing, exit 0), P20_GUARD_S.
# bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P20 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
# The committed file `phase` picks the chain: "screen" (stage 1) or "forced" (follow-up F1,
# log.md 01:15: download, train (skips the carried controls), forced); env P20_PHASE overrides.
PHASE="${P20_PHASE:-$(tr -d '[:space:]' < phase 2>/dev/null || echo screen)}"
case "$PHASE" in
  forced) STEPS="download train forced" ;;
  *) STEPS="download train generate" ;;
esac
echo "P20 phase=$PHASE steps=$STEPS"
GUARD_S="${P20_GUARD_S:-5400}"
if [ "${P20_DRY_RUN:-0}" = 1 ]; then
  echo "P20 dry steps:"
  echo "  1 download: /ws/venv-peft/bin/python -m p20.pod download (rc 11 on failure)"
  echo "  2 train:    /ws/venv-peft/bin/python -m p20.pod train (rc 12 on failure)"
  echo "  3 generate: /ws/venv-vllm/bin/python -m p20.pod generate (rc 13 on failure)"
  echo "  4 manifest: shasum -a 256 over results/ -> results/MANIFEST.sha256"
  echo "P20 dry guard: no new step after ${GUARD_S} s (rc 4)"
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
T0="$(date +%s)"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
PEFT=/ws/venv-peft/bin/python
VLLM=/ws/venv-vllm/bin/python
RC=0
for PY in "$PEFT" "$VLLM"; do
  if [ ! -x "$PY" ]; then echo "P20 missing $PY (pod_setup.sh --only e11)"; RC=2; fi
done
if [ "$RC" = 0 ] && [ ! -s data/contexts.jsonl ]; then
  echo "P20 missing data/contexts.jsonl (built and committed on the laptop)"; RC=2
fi

step_env() {  # GPU, disk and time before each step
  echo "== $(date -u +%FT%TZ) step=$1 elapsed=$(( $(date +%s) - T0 ))s"
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader \
    2>/dev/null || true
  df -h /ws 2>/dev/null | tail -1 || true
}

for STEP in $STEPS; do
  [ "$RC" = 0 ] || break
  ELAPSED=$(( $(date +%s) - T0 ))
  if [ "$ELAPSED" -ge "$GUARD_S" ]; then
    echo "P20 guard: ${ELAPSED}s >= ${GUARD_S}s, not starting step $STEP"; RC=4; break
  fi
  case "$STEP" in
    download) PY="$PEFT"; FAIL=11 ;;
    train) PY="$PEFT"; FAIL=12 ;;
    generate) PY="$VLLM"; FAIL=13 ;;
    forced) PY="$VLLM"; FAIL=14 ;;
  esac
  step_env "$STEP"
  S0="$(date +%s)"
  "$PY" -m p20.pod "$STEP"
  PRC=$?
  echo "== $(date -u +%FT%TZ) step=$STEP python_rc=$PRC seconds=$(( $(date +%s) - S0 ))"
  if [ "$PRC" != 0 ]; then RC="$FAIL"; fi
done

echo "== $(date -u +%FT%TZ) step=manifest elapsed=$(( $(date +%s) - T0 ))s"
# every file under results/, a crash's stray *.tmp too (the runner's pull needs each pulled file
# listed); the manifest itself is written beside results/, then moved in
( cd results && find . -type f ! -name MANIFEST.sha256 -print0 \
    | sort -z | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) \
  && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P20 end $(date -u +%FT%TZ) rc=$RC elapsed=$(( $(date +%s) - T0 ))s"
echo "PROBE_DONE rc=$RC"
