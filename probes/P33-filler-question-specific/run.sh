#!/usr/bin/env bash
# P33 pod entry point (scripts/probe_pod.py run contract), dispatching on the committed file
# `phase` next to this script (env P33_PHASE overrides it):
#   stage1  install `kernels` (transformers' FP8/FP4 Hub kernels; not in the pod-i1a lock),
#           download DeepSeek-V4-Flash @ its pin, then `python -m p33.pod run` (V4: mask check,
#           13 arms x 300 items; then Gemma 4 31B, downloaded in a thread meanwhile).
# rc 6 = the GPU check failed before the download (a CUDA context could not be made: run 1 lost
# a 32-min download to "CUDA-capable device(s) is/are busy or unavailable", log.md).
# rc 1 = an exception (results/error.txt), 3 = a gate failed, 4 = stopped early (overrun or
# deadline), 5 = the Gemma phase failed after a complete V4 phase.
# results/MANIFEST.sha256 is always written; the last log line is PROBE_DONE rc=<n>.
# Optional env: P33_ARGS ("--limit 3"), P33_DRY_RUN=1 (print the steps, touch nothing).
# bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P33 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
PHASE="${P33_PHASE:-$(tr -d '[:space:]' < phase 2>/dev/null || true)}"
case "$PHASE" in
  stage1) MOD=p33.pod ;;
  stage2) MOD=p33.stage2 ;;
  *) echo "P33 unknown phase '$PHASE'"; MOD="" ;;
esac
if [ "${P33_DRY_RUN:-0}" = 1 ]; then
  echo "P33 dry phase=$PHASE steps: uv pip install kernels==0.16.2; python -m $MOD download; python -m $MOD run"
  [ -n "$MOD" ] && exit 0 || exit 2
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
export P33_T0="$(date +%s)"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../P06-filler-token-computation:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
export TRANSFORMERS_DISABLE_DEEPGEMM_LINEAR=1
export P33_ARTIFACTS="${P33_ARTIFACTS:-/ws/p33_artifacts}"
PY=/ws/venv-i1a/bin/python
RC=0
if [ -z "$MOD" ]; then RC=2; fi
if [ "$RC" = 0 ] && [ ! -x "$PY" ]; then echo "P33 missing $PY (pod_setup.sh --only i1a)"; RC=2; fi
if [ "$RC" = 0 ] && [ ! -s ../../data/P06/chained_var_binding_dataset.json ]; then
  echo "P33 missing data/P06/chained_var_binding_dataset.json (probe.toml [pod].push)"; RC=2
fi
if [ "$RC" = 0 ]; then
  GPU_OK=0
  for TRY in 1 2 3; do
    if "$PY" -c 'import torch; f, t = torch.cuda.mem_get_info(); x = torch.ones(4096, 4096, device="cuda"); torch.cuda.synchronize(); print(f"P33_GPU_OK free={f / 1e9:.0f}GB total={t / 1e9:.0f}GB sum={float((x @ x).sum()):.0f}")'; then
      GPU_OK=1; break
    fi
    echo "P33 GPU check failed (try $TRY/3)"; nvidia-smi -q -d COMPUTE,PIDS 2>/dev/null | head -20 || true; sleep 20
  done
  if [ "$GPU_OK" != 1 ]; then
    echo "== $(date -u +%FT%TZ) step=gpu_check: no CUDA context after 3 tries" >> results/error.txt
    RC=6
  fi
fi
if [ "$RC" = 0 ]; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  if ! "$UV" pip install --python "$PY" "kernels==0.16.2"; then
    echo "== $(date -u +%FT%TZ) step=install: uv pip install kernels==0.16.2 failed" >> results/error.txt
    echo "P33 WARNING kernels install failed"
  fi
  "$UV" pip freeze --python "$PY" > results/venv_freeze.txt 2>/dev/null || true
  "$PY" -m "$MOD" download || RC=$?
fi
if [ "$RC" = 0 ]; then
  # shellcheck disable=SC2086 # P33_ARGS is a word list on purpose
  "$PY" -m "$MOD" run ${P33_ARGS:-} || RC=$?
fi
if [ "$RC" != 0 ] && [ ! -s results/error.txt ]; then
  echo "== $(date -u +%FT%TZ) rc=$RC with no recorded exception (see the run log)" >> results/error.txt
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P33 end $(date -u +%FT%TZ) phase=$PHASE rc=$RC"
echo "PROBE_DONE rc=$RC"
