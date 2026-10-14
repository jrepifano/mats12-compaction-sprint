#!/usr/bin/env bash
# P46 pod entry point (scripts/probe_pod.py run contract; plan.md "Automatic rules", on the pod).
# Template: probes/P42-ood-coverage-gaps/run.sh. Steps (p46/pod.py has the details):
#   download (/ws/venv-peft)  DPO, SFT, Qwen, the SORRY-Bench judge (gated) at the pins (DPO/SFT
#                             asserted = emx.e1.registry, Qwen = emx.qwen.registry); the chat
#                             template; prompts.jsonl sha256 vs its meta; human_check.jsonl
#   smoke    (/ws/venv-peft)  the smoke subset through gen/backtr/judge (venv-vllm children),
#                             feats and score, each timed; the plan's gates; the projection and
#                             its cuts -> results/smoke.json (settings the bulk steps read)
#   gen      (/ws/venv-vllm)  DPO then SFT samples (one child process per engine) + the cap
#                             check's continuations -> results/gen/{responses,continued}.jsonl
#   backtr   (/ws/venv-vllm)  Qwen back-translation of the "lang" responses
#   judge    (/ws/venv-vllm)  ft-mistral on every PROBE_SETS response -> results/judge/
#   feats    (/ws/venv-peft)  DPO (L24 mean + last token, NLL, entropy), SFT NLL -> /ws/p46_work
#   score    (/ws/venv-peft)  results/scores.csv
#   capfix   (/ws/venv-vllm)  only when the cap check flipped too many labels: continue, back-
#                             translate and judge every cap-hit response (last; gates itself)
#   manifest                  results/MANIFEST.sha256 over every result file (aside, then moved)
# Per-step records: results/run_<step>.json, results/timing.json. Every step is resumable.
# A failed step stops the chain: rc 11 download, 12 smoke gates (or error), 13 smoke projection
# over budget after both cuts (pod.py rc 3), 14 gen, 15 backtr, 16 judge, 17 feats, 18 score,
# 19 capfix, 20 the manifest failed (only when every step succeeded); rc 2 = a missing venv or
# input; rc 4 = the soft wall-clock guard (P46_GUARD_S, default probe.toml gpu_hours - 15 min of
# run.sh) stopped the chain before a new step. P46_STEPS overrides the step list (e.g.
# "feats score" for a resumed session). The manifest and the last log line `PROBE_DONE rc=<n>`
# are always written (except under P46_DRY_RUN=1, which prints the steps, touches nothing and
# exits 0), and the script exits with that rc. bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P46 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
STEPS="${P46_STEPS:-download smoke gen backtr judge feats score capfix}"
GPU_H="$(sed -n 's/^gpu_hours *= *\([0-9.]*\).*/\1/p' probe.toml 2>/dev/null | head -1)"
DEFAULT_GUARD="$(awk -v h="${GPU_H:-2.5}" 'BEGIN { printf "%d", (h - 0.25) * 3600 }')"
GUARD_S="${P46_GUARD_S:-$DEFAULT_GUARD}"
echo "P46 steps=$STEPS guard=${GUARD_S}s"

step_py() {  # the venv a step runs in: peft for HF work, vllm for engines and their orchestrators
  case "$1" in
    download|smoke|feats|score) echo peft ;;
    gen|backtr|judge|capfix) echo vllm ;;
    *) echo unknown ;;
  esac
}
step_rc() {
  case "$1" in
    download) echo 11 ;; smoke) echo 12 ;; gen) echo 14 ;; backtr) echo 15 ;; judge) echo 16 ;;
    feats) echo 17 ;; score) echo 18 ;; capfix) echo 19 ;; *) echo 2 ;;
  esac
}

if [ "${P46_DRY_RUN:-0}" = 1 ]; then
  echo "P46 dry steps:"
  for S in $STEPS; do
    V="$(step_py "$S")"
    if [ "$V" = unknown ]; then echo "  unknown step $S"; exit 2; fi
    EXTRA=""
    if [ "$S" = smoke ]; then EXTRA="; 13 over budget"; fi
    echo "  $S: /ws/venv-$V/bin/python -m p46.pod $S (rc $(step_rc "$S")$EXTRA)"
  done
  echo "  manifest: shasum -a 256 over results/ -> results/MANIFEST.sha256 (rc 20)"
  echo "P46 dry guard: no new step after ${GUARD_S} s (rc 4)"
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
T0="$(date +%s)"
export P46_T0="$T0"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=60 WANDB_MODE=offline
export TOKENIZERS_PARALLELISM=false PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
# p46 imports p42 (feats, scoring, pod helpers): its package is pushed by [pod].push
export PYTHONPATH="$PWD:$PWD/../P42-ood-coverage-gaps:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
PEFT="${P46_PEFT_PY:-/ws/venv-peft/bin/python}"   # overridable for the failure-path tests
VLLM="${P46_VLLM_BIN:-/ws/venv-vllm/bin/python}"
export P46_VLLM_PY="$VLLM"
RC=0
for PY in "$PEFT" "$VLLM"; do
  if [ ! -x "$PY" ]; then echo "P46 missing $PY (pod_setup.sh --only eq0)"; RC=2; fi
done
for F in ../../data/P46/prompts.jsonl ../../data/P46/prompts.meta.json \
         ../../data/P46/human_check.jsonl ../P42-ood-coverage-gaps/p42/pod.py; do
  if [ ! -s "$F" ]; then echo "P46 missing $F (built on the laptop; [pod].push)"; RC=2; fi
done

step_env() {  # GPU, disk, memory and time before each step
  echo "== $(date -u +%FT%TZ) step=$1 elapsed=$(( $(date +%s) - T0 ))s"
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader \
    2>/dev/null || true
  df -h /ws 2>/dev/null | tail -1 || true
  free -g 2>/dev/null | sed -n 2p || true
}

for STEP in $STEPS; do
  [ "$RC" = 0 ] || break
  ELAPSED=$(( $(date +%s) - T0 ))
  if [ "$ELAPSED" -ge "$GUARD_S" ]; then
    echo "P46 guard: ${ELAPSED}s >= ${GUARD_S}s, not starting step $STEP"; RC=4; break
  fi
  case "$(step_py "$STEP")" in
    peft) PY="$PEFT" ;;
    vllm) PY="$VLLM" ;;
    *) echo "P46 unknown step $STEP"; RC=2; break ;;
  esac
  FAIL="$(step_rc "$STEP")"
  step_env "$STEP"
  S0="$(date +%s)"
  "$PY" -m p46.pod "$STEP"
  PRC=$?
  echo "== $(date -u +%FT%TZ) step=$STEP python_rc=$PRC seconds=$(( $(date +%s) - S0 ))"
  if [ "$PRC" != 0 ]; then
    RC="$FAIL"
    if [ "$STEP" = smoke ] && [ "$PRC" = 3 ]; then RC=13; fi
  fi
done

echo "== $(date -u +%FT%TZ) step=manifest elapsed=$(( $(date +%s) - T0 ))s"
# every file under results/, a crash's stray *.tmp too (the runner's pull needs each pulled file
# listed); the manifest itself is written beside results/, then moved in. Feature stores stay on
# /ws and never enter results/.
if ! { ( cd results && find . -type f ! -name MANIFEST.sha256 -print0 \
           | sort -z | xargs -0 -r shasum -a 256 > ../MANIFEST.sha256.tmp ) \
         && mv MANIFEST.sha256.tmp results/MANIFEST.sha256; }; then
  echo "P46 manifest FAILED"
  if [ "$RC" = 0 ]; then RC=20; fi
fi
echo "P46 end $(date -u +%FT%TZ) rc=$RC elapsed=$(( $(date +%s) - T0 ))s"
echo "PROBE_DONE rc=$RC"
exit "$RC"
