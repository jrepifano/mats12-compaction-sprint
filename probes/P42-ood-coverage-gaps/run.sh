#!/usr/bin/env bash
# P42 pod entry point (scripts/probe_pod.py run contract; plan.md "Automatic rules", on the pod).
# Template: probes/P34-em-narrow-persona/run.sh. Steps (p42/pod.py has the details):
#   download (/ws/venv-peft)  DPO + SFT snapshots at the pins (asserted = emx.e1.registry), the
#                             SAE subdir, the chat template; prompts.jsonl sha256 vs its meta
#   smoke    (/ws/venv-peft)  1,100-prompt smoke through gen (a venv-vllm child), feats, grads,
#                             score, each timed; the plan's gates; the wall-clock projection and
#                             its cuts -> results/smoke.json (settings the bulk steps read)
#   gen      (/ws/venv-vllm)  greedy DPO responses for every gen=True prompt
#                             -> results/gen/responses.jsonl
#   feats    (/ws/venv-peft)  DPO and SFT forwards -> /ws/p42_feats (not results/)
#   grads    (/ws/venv-peft)  S4 projected gradients -> /ws/p42_feats/grads.npy
#   score    (/ws/venv-peft)  results/scores.csv, results/pca/
#   manifest                  results/MANIFEST.sha256 over every result file (aside, then moved)
# Per-step records: results/run_<step>.json, results/timing.json. Every step is resumable.
# A failed step stops the chain: rc 11 download, 12 smoke gates (or error), 13 smoke projection
# over budget after both cuts (pod.py rc 3), 14 gen, 15 feats, 16 grads, 17 score, 18 the
# manifest failed (only when every step succeeded); rc 2 = a
# missing venv or input; rc 4 = the soft wall-clock guard (P42_GUARD_S, default probe.toml
# gpu_hours - 15 min of run.sh: 11,700 s at 3.5 h) stopped the chain before a new step. P42_STEPS overrides the step list (e.g.
# "feats grads score" for a resumed session). The manifest and the last log line
# `PROBE_DONE rc=<n>` are always written (except under P42_DRY_RUN=1, which prints the steps,
# touches nothing and exits 0), and the script exits with that rc. bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P42 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
STEPS="${P42_STEPS:-download smoke gen feats grads score}"
# default guard: probe.toml gpu_hours - 15 min of run.sh (4.5 h -> 15,300 s)
GPU_H="$(sed -n 's/^gpu_hours *= *\([0-9.]*\).*/\1/p' probe.toml 2>/dev/null | head -1)"
DEFAULT_GUARD="$(awk -v h="${GPU_H:-3.5}" 'BEGIN { printf "%d", (h - 0.25) * 3600 }')"
GUARD_S="${P42_GUARD_S:-$DEFAULT_GUARD}"
echo "P42 steps=$STEPS guard=${GUARD_S}s"
if [ "${P42_DRY_RUN:-0}" = 1 ]; then
  echo "P42 dry steps:"
  for S in $STEPS; do
    case "$S" in
      download) echo "  download: /ws/venv-peft/bin/python -m p42.pod download (rc 11)" ;;
      smoke) echo "  smoke:    /ws/venv-peft/bin/python -m p42.pod smoke (rc 12; 13 over budget)" ;;
      gen) echo "  gen:      /ws/venv-vllm/bin/python -m p42.pod gen (rc 14)" ;;
      feats) echo "  feats:    /ws/venv-peft/bin/python -m p42.pod feats (rc 15)" ;;
      grads) echo "  grads:    /ws/venv-peft/bin/python -m p42.pod grads (rc 16)" ;;
      score) echo "  score:    /ws/venv-peft/bin/python -m p42.pod score (rc 17)" ;;
      *) echo "  unknown step $S"; exit 2 ;;
    esac
  done
  echo "  manifest: shasum -a 256 over results/ -> results/MANIFEST.sha256"
  echo "P42 dry guard: no new step after ${GUARD_S} s (rc 4)"
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
T0="$(date +%s)"
export P42_T0="$T0"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=60 WANDB_MODE=offline
export TOKENIZERS_PARALLELISM=false PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
PEFT="${P42_PEFT_PY:-/ws/venv-peft/bin/python}"   # overridable for the failure-path tests
VLLM="${P42_VLLM_BIN:-/ws/venv-vllm/bin/python}"
export P42_VLLM_PY="$VLLM"
RC=0
for PY in "$PEFT" "$VLLM"; do
  if [ ! -x "$PY" ]; then echo "P42 missing $PY (pod_setup.sh --only eq0)"; RC=2; fi
done
for F in ../../data/P42/prompts.jsonl ../../data/P42/prompts.meta.json; do
  if [ ! -s "$F" ]; then echo "P42 missing $F (built on the laptop; [pod].push)"; RC=2; fi
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
    echo "P42 guard: ${ELAPSED}s >= ${GUARD_S}s, not starting step $STEP"; RC=4; break
  fi
  case "$STEP" in
    download) PY="$PEFT"; FAIL=11 ;;
    smoke) PY="$PEFT"; FAIL=12 ;;
    gen) PY="$VLLM"; FAIL=14 ;;
    feats) PY="$PEFT"; FAIL=15 ;;
    grads) PY="$PEFT"; FAIL=16 ;;
    score) PY="$PEFT"; FAIL=17 ;;
    *) echo "P42 unknown step $STEP"; RC=2; break ;;
  esac
  step_env "$STEP"
  S0="$(date +%s)"
  "$PY" -m p42.pod "$STEP"
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
  echo "P42 manifest FAILED"
  if [ "$RC" = 0 ]; then RC=18; fi
fi
echo "P42 end $(date -u +%FT%TZ) rc=$RC elapsed=$(( $(date +%s) - T0 ))s"
echo "PROBE_DONE rc=$RC"
exit "$RC"
