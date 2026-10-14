#!/usr/bin/env bash
# P47 pod entry point (scripts/probe_pod.py run contract): activation capture with p47.capture.
# What to run comes from the committed file `phase` next to this script (KEY=VALUE lines, not
# sourced), each overridable by the environment:
#   P47_MODEL=gpt-oss-120b          # qwen3.6-27b | gpt-oss-120b | gpt-oss-20b (required)
#   P47_BATCHES=contrast,session1   # data/<model>/capture/<batch>.jsonl.gz, run in this order
#   P47_ARGS=--limit 20             # optional extra args for every `p47.capture run`
#   P47_DEADLINE_EPOCH=1760000000   # optional; default start + probe.toml [pod].gpu_hours
# Steps: GPU check (3 tries), `kernels==0.16.2` for gpt-oss only (transformers' MXFP4 and
# flash-attention Hub kernels; not in the pod-i1a lock), the model download at its pin, then one
# `python -m p47.capture run` per batch, stopping at the first nonzero rc.
# rc: 0 ok, 1 exception, 2 bad config or input, 3 hook/attention check failed, 4 stopped at the
# deadline (partial results written, rerun resumes), 6 GPU check failed.
# results/MANIFEST.sha256 is always written; the last log line is PROBE_DONE rc=<n>.
# P47_DRY_RUN=1 prints the steps and touches nothing. bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P47 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
T0="$(date +%s)"
phase_val() {  # phase_val KEY: the value of KEY=... in ./phase (last one wins), or empty
  [ -f phase ] || return 0
  grep -E "^$1=" phase | tail -1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}
MODEL="${P47_MODEL:-$(phase_val P47_MODEL)}"
BATCHES="${P47_BATCHES:-$(phase_val P47_BATCHES)}"
ARGS="${P47_ARGS:-$(phase_val P47_ARGS)}"
DEADLINE="${P47_DEADLINE_EPOCH:-$(phase_val P47_DEADLINE_EPOCH)}"
if [ -z "$DEADLINE" ]; then
  GPU_H="$(sed -n 's/^gpu_hours[[:space:]]*=[[:space:]]*\([0-9.]*\).*/\1/p' probe.toml | head -1)"
  DEADLINE="$(awk -v t="$T0" -v h="${GPU_H:-1}" 'BEGIN { printf "%d", t + h * 3600 }')"
fi
echo "P47 model=$MODEL batches=$BATCHES args='$ARGS' deadline_epoch=$DEADLINE"
case "$MODEL" in
  qwen3.6-27b|gpt-oss-120b|gpt-oss-20b) ;;
  *) echo "P47 unknown model '$MODEL' (set P47_MODEL in ./phase)"; MODEL="" ;;
esac
if [ "${P47_DRY_RUN:-0}" = 1 ]; then
  KMSG=""; case "$MODEL" in gpt-oss*) KMSG="uv pip install kernels==0.16.2; " ;; esac
  echo "P47 dry: gpu check; ${KMSG}python -m p47.capture download --model $MODEL"
  for B in $(echo "$BATCHES" | tr ',' ' '); do
    echo "P47 dry: python -m p47.capture run --model $MODEL --batch $B --deadline-epoch $DEADLINE $ARGS"
  done
  [ -n "$MODEL" ] && [ -n "$BATCHES" ] && exit 0 || exit 2
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True P47_MODEL="$MODEL"
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
PY=/ws/venv-i1a/bin/python
RC=0
if [ -z "$MODEL" ] || [ -z "$BATCHES" ]; then echo "P47 no model or no batches"; RC=2; fi
if [ "$RC" = 0 ] && [ ! -x "$PY" ]; then echo "P47 missing $PY (pod_setup.sh --only i1a)"; RC=2; fi
if [ "$RC" = 0 ]; then
  for B in $(echo "$BATCHES" | tr ',' ' '); do
    if [ ! -s "data/$MODEL/capture/$B.jsonl.gz" ]; then
      echo "P47 missing data/$MODEL/capture/$B.jsonl.gz (commit it or add it to [pod].push)"; RC=2
    fi
  done
fi
if [ "$RC" = 0 ]; then
  GPU_OK=0
  for TRY in 1 2 3; do
    if "$PY" -c 'import torch; f, t = torch.cuda.mem_get_info(); x = torch.ones(4096, 4096, device="cuda"); torch.cuda.synchronize(); print(f"P47_GPU_OK free={f / 1e9:.0f}GB total={t / 1e9:.0f}GB sum={float((x @ x).sum()):.0f} name={torch.cuda.get_device_name(0)}")'; then
      GPU_OK=1; break
    fi
    echo "P47 GPU check failed (try $TRY/3)"; nvidia-smi -q -d COMPUTE,PIDS 2>/dev/null | head -20 || true; sleep 20
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
  case "$MODEL" in
    gpt-oss*)
      if ! "$UV" pip install --python "$PY" "kernels==0.16.2"; then
        echo "== $(date -u +%FT%TZ) step=install: uv pip install kernels==0.16.2 failed" >> results/error.txt
        echo "P47 WARNING kernels install failed (MXFP4 would dequantize; flash attention unavailable)"
      fi ;;
  esac
  "$UV" pip freeze --python "$PY" > "results/venv_freeze_$MODEL.txt" 2>/dev/null || true
  "$PY" -m p47.capture download --model "$MODEL" || RC=$?
fi
if [ "$RC" = 0 ]; then
  for B in $(echo "$BATCHES" | tr ',' ' '); do
    echo "P47 batch $B start $(date -u +%FT%TZ)"
    # shellcheck disable=SC2086 # ARGS is a word list on purpose
    "$PY" -m p47.capture run --model "$MODEL" --batch "$B" --deadline-epoch "$DEADLINE" $ARGS || RC=$?
    echo "P47 batch $B end $(date -u +%FT%TZ) rc=$RC"
    [ "$RC" = 0 ] || break
  done
fi
if [ "$RC" != 0 ] && [ ! -s results/error.txt ]; then
  echo "== $(date -u +%FT%TZ) rc=$RC (see the run log)" >> results/error.txt
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P47 end $(date -u +%FT%TZ) model=$MODEL rc=$RC"
echo "PROBE_DONE rc=$RC"
