#!/usr/bin/env bash
# P45 pod entry point (scripts/probe_pod.py run contract; plan.md "Setup: T2", "Automatic rules").
# Template: probes/P42-ood-coverage-gaps/run.sh. Steps (p45/pod.py has the details), all in
# /ws/venv-peft:
#   download  DPO + SFT snapshots at P42's pins (asserted = emx.e1.registry), the public SAE
#             subdir, the chat template; sha256 of prompts.jsonl (contracts) and of the law2 and
#             Dolma files (data.meta.json)
#   smoke     ~1,500 prompts through every step, production-width SAEs for 300 steps each, gates,
#             projection and cuts -> results/smoke.json (settings the bulk steps read)
#   dpo       DPO thresholds, buffers (/ws/p45_work, never results/), sigma2, post/pre/sftdata
#             SAEs at L16/L24 -> results/sae/, results/train/; scoring -> results/partial_dpo.npz
#   sft       the same for the SFT model's sftmodel arm -> results/partial_sft.npz
#   finalize  maha_L24, union PCA, dF columns -> results/scores.csv, results/pca_union/
#   manifest  results/MANIFEST.sha256 over every result file (written aside, then moved in)
# Per-step records: results/run_<step>.json, results/timing.json. Every step is resumable.
# A failed step stops the chain: rc 11 download, 12 smoke gates (or error), 13 smoke projection
# over budget after both cuts (pod.py rc 3), 14 dpo, 15 sft, 16 finalize, 18 the manifest failed
# (only when every step succeeded); rc 2 = a missing venv or input; rc 4 = out of time: run.sh's
# step guard (no new step within P45_STEP_MIN_S, default 300 s, of the deadline) or a step's own
# time guard (pod.py rc 5: the remaining projected work no longer fits) stopped the chain.
# One absolute deadline for everything: P45_DEADLINE_EPOCH if set, else the killswitch deadline
# (/ws/killswitch/deadline, armed by the runner before the transfer) - 20 min (the runner salvages
# 15 min before it, plus 5 min for the manifest), else run.sh start + gpu_hours - 15 min; exported
# as P45_DEADLINE_EPOCH for the smoke projection and the time guards. P45_STEPS overrides the
# step list (e.g. "sft finalize" for a resumed session). The
# manifest and the last log line `PROBE_DONE rc=<n>` are always written (except under
# P45_DRY_RUN=1, which prints the steps, touches nothing and exits 0), and the script exits with
# that rc. bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P45 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
STEPS="${P45_STEPS:-download smoke dpo sft finalize}"
GPU_H="$(sed -n 's/^gpu_hours *= *\([0-9.]*\).*/\1/p' probe.toml 2>/dev/null | head -1)"
T0="$(date +%s)"
KS="${P45_KS_DEADLINE:-/ws/killswitch/deadline}"
if [ -n "${P45_DEADLINE_EPOCH:-}" ]; then
  DEADLINE="$P45_DEADLINE_EPOCH"; DL_SRC="env P45_DEADLINE_EPOCH"
elif [ -s "$KS" ] && KSV="$(awk 'NR == 1 && $1 ~ /^[0-9]+$/ { print $1 }' "$KS")" \
     && [ -n "$KSV" ]; then
  DEADLINE=$(( KSV - 1200 )); DL_SRC="killswitch $KSV - 1200 s"
else
  DEADLINE="$(awk -v t="$T0" -v h="${GPU_H:-2.5}" 'BEGIN { printf "%d", t + h * 3600 - 900 }')"
  DL_SRC="start + gpu_hours ${GPU_H:-2.5} h - 900 s"
fi
DEADLINE="${DEADLINE%.*}"
export P45_DEADLINE_EPOCH="$DEADLINE"
STEP_MIN_S="${P45_STEP_MIN_S:-300}"
echo "P45 steps=$STEPS deadline=$DEADLINE ($DL_SRC) left=$(( DEADLINE - T0 ))s"
if [ "${P45_DRY_RUN:-0}" = 1 ]; then
  echo "P45 dry steps:"
  for S in $STEPS; do
    case "$S" in
      download) echo "  download: /ws/venv-peft/bin/python -m p45.pod download (rc 11)" ;;
      smoke) echo "  smoke:    /ws/venv-peft/bin/python -m p45.pod smoke (rc 12; 13 over budget)" ;;
      dpo) echo "  dpo:      /ws/venv-peft/bin/python -m p45.pod dpo (rc 14)" ;;
      sft) echo "  sft:      /ws/venv-peft/bin/python -m p45.pod sft (rc 15)" ;;
      finalize) echo "  finalize: /ws/venv-peft/bin/python -m p45.pod finalize (rc 16)" ;;
      *) echo "  unknown step $S"; exit 2 ;;
    esac
  done
  echo "  manifest: shasum -a 256 over results/ -> results/MANIFEST.sha256 (rc 18)"
  echo "P45 dry guard: no new step after epoch $(( DEADLINE - STEP_MIN_S )) (rc 4)"
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
export P45_T0="$T0"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=60 WANDB_MODE=offline
export TOKENIZERS_PARALLELISM=false PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../P42-ood-coverage-gaps:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
PEFT="${P45_PEFT_PY:-/ws/venv-peft/bin/python}"   # overridable for the failure-path tests
RC=0
if [ ! -x "$PEFT" ]; then echo "P45 missing $PEFT (pod_setup.sh --only eq0)"; RC=2; fi
for F in ../../data/P42/prompts.jsonl ../../data/P45/planted_law2.jsonl \
         ../../data/P45/dolma_chunks.jsonl ../../data/P45/data.meta.json \
         ../P42-ood-coverage-gaps/p42/pod.py; do
  if [ ! -s "$F" ]; then echo "P45 missing $F (built on the laptop; [pod].push)"; RC=2; fi
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
  NOW="$(date +%s)"
  if [ $(( NOW + STEP_MIN_S )) -ge "$DEADLINE" ]; then
    echo "P45 guard: $(( DEADLINE - NOW ))s to the deadline < ${STEP_MIN_S}s, not starting $STEP"
    RC=4; break
  fi
  case "$STEP" in
    download) FAIL=11 ;;
    smoke) FAIL=12 ;;
    dpo) FAIL=14 ;;
    sft) FAIL=15 ;;
    finalize) FAIL=16 ;;
    *) echo "P45 unknown step $STEP"; RC=2; break ;;
  esac
  step_env "$STEP"
  S0="$(date +%s)"
  "$PEFT" -m p45.pod "$STEP"
  PRC=$?
  echo "== $(date -u +%FT%TZ) step=$STEP python_rc=$PRC seconds=$(( $(date +%s) - S0 ))"
  if [ "$PRC" != 0 ]; then
    RC="$FAIL"
    if [ "$STEP" = smoke ] && [ "$PRC" = 3 ]; then RC=13; fi
    if [ "$PRC" = 5 ]; then RC=4; echo "P45 step $STEP stopped by its time guard"; fi
  fi
done

echo "== $(date -u +%FT%TZ) step=manifest elapsed=$(( $(date +%s) - T0 ))s"
# every file under results/, a crash's stray *.tmp too (the runner's pull needs each pulled file
# listed). SAE buffers stay on /ws and never enter results/.
if ! { ( cd results && find . -type f ! -name MANIFEST.sha256 -print0 \
           | sort -z | xargs -0 -r shasum -a 256 > ../MANIFEST.sha256.tmp ) \
         && mv MANIFEST.sha256.tmp results/MANIFEST.sha256; }; then
  echo "P45 manifest FAILED"
  if [ "$RC" = 0 ]; then RC=18; fi
fi
echo "P45 end $(date -u +%FT%TZ) rc=$RC elapsed=$(( $(date +%s) - T0 ))s"
echo "PROBE_DONE rc=$RC"
exit "$RC"
