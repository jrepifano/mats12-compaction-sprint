#!/usr/bin/env bash
# P40 pod entry point (scripts/probe_pod.py run contract): downloads, then p40.session all.
# Then results/MANIFEST.sha256 over every file under results/, and a last line PROBE_DONE rc=<n>.
# Ported from probes/P36-workspace-binding-causal/run.sh. bash 3.2-compatible.
# Env (optional): P40_MAX_HOURS (default 1.9, measured from the start of this script; the
# session skips phases that would not finish), P40_ARTIFACTS, P40_BATCH, P40_ARGS.
set -uo pipefail
cd "$(dirname "$0")"
echo "P40 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
export P40_T0="${P40_T0:-$(date +%s)}" P40_MAX_HOURS="${P40_MAX_HOURS:-1.9}"
export P40_ARTIFACTS="${P40_ARTIFACTS:-/ws/p40_artifacts}"
PY=/ws/venv-i9/bin/python
mkdir -p "$P40_ARTIFACTS"
RC=0
if ! test -x "$PY"; then
  echo "P40 missing $PY (pod_setup.sh --only i9)"; RC=2
elif ! test -f ../../third_party/workspace-bench/pyproject.toml; then
  echo "P40 missing third_party/workspace-bench (probe.toml [pod].push)"; RC=2
else
  nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  free -g 2>/dev/null | head -2 || true
  # slow-host guard (P36 log, 17:30Z): < 15 GB in the first 10 min cannot fetch ~61 GB in time
  ( sleep "${P40_DL_CHECK_S:-600}"
    MB=$(du -sm "$P40_ARTIFACTS" 2>/dev/null | cut -f1)
    if [ "${MB:-0}" -lt "${P40_DL_MIN_MB:-15000}" ] && pgrep -f '[p]40.session download' >/dev/null; then
      echo "P40 slow download: ${MB:-0} MB after ${P40_DL_CHECK_S:-600} s; stopping"
      pkill -f '[p]40.session download'
    fi ) &
  GUARD=$!
  "$PY" -m p40.session download || RC=$?
  kill "$GUARD" 2>/dev/null || true
  # shellcheck disable=SC2086 # P40_ARGS is a word list on purpose
  [ "$RC" -eq 0 ] && { "$PY" -m p40.session all ${P40_ARGS:-} || RC=$?; }
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P40 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
