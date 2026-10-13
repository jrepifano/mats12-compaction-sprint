#!/usr/bin/env bash
# P39 pod entry point (scripts/probe_pod.py run contract): downloads, then p39.session all.
# Then results/MANIFEST.sha256 over every file under results/, and a last line PROBE_DONE rc=<n>.
# bash 3.2-compatible. Env (optional): P39_MAX_HOURS (default 1.0, measured from the start of this
# script; later forward groups are skipped when < 5 min remain), P39_ARTIFACTS, P39_BATCH, P39_ARGS.
set -uo pipefail
cd "$(dirname "$0")"
echo "P39 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
echo $$ > logs/run.pid 2>/dev/null || true
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
# P36's package (pushed via probe.toml [pod].push) is imported unchanged
export PYTHONPATH="$PWD:$PWD/../P36-workspace-binding-causal:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
export P39_T0="${P39_T0:-$(date +%s)}" P39_MAX_HOURS="${P39_MAX_HOURS:-1.0}"
export P39_ARTIFACTS="${P39_ARTIFACTS:-/ws/p39_artifacts}"
PY=/ws/venv-i9/bin/python
mkdir -p results logs "$P39_ARTIFACTS"
RC=0
if ! test -x "$PY"; then
  echo "P39 missing $PY (pod_setup.sh --only i9)"; RC=2
elif ! test -f ../../third_party/workspace-bench/pyproject.toml; then
  echo "P39 missing third_party/workspace-bench (probe.toml [pod].push)"; RC=2
elif ! test -f ../P36-workspace-binding-causal/p36/session.py; then
  echo "P39 missing probes/P36-workspace-binding-causal/p36 (probe.toml [pod].push)"; RC=2
else
  nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  free -g 2>/dev/null | head -2 || true
  # slow-host guard (P36 log, 17:30Z): < 15 GB in the first 10 min cannot finish in time
  ( sleep "${P39_DL_CHECK_S:-600}"
    MB=$(du -sm "$P39_ARTIFACTS" 2>/dev/null | cut -f1)
    if [ "${MB:-0}" -lt "${P39_DL_MIN_MB:-15000}" ] && pgrep -f '[p]39.session download' >/dev/null; then
      echo "P39 slow download: ${MB:-0} MB after ${P39_DL_CHECK_S:-600} s; stopping"
      pkill -f '[p]39.session download'
    fi ) &
  GUARD=$!
  "$PY" -m p39.session download || RC=$?
  kill "$GUARD" 2>/dev/null || true
  # shellcheck disable=SC2086 # P39_ARGS is a word list on purpose
  [ "$RC" -eq 0 ] && { "$PY" -m p39.session all ${P39_ARGS:-} || RC=$?; }
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P39 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
