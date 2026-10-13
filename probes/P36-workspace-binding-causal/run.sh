#!/usr/bin/env bash
# P36 pod entry point (scripts/probe_pod.py run contract): downloads, then p36.session all.
# Then results/MANIFEST.sha256 over every file under results/, and a last line PROBE_DONE rc=<n>.
# bash 3.2-compatible. Env (optional): P36_MAX_HOURS (default 2.0, measured from the start of this
# script; pass 2 is skipped when < 30 min remain), P36_ARTIFACTS, P36_BATCH, P36_ARGS.
set -uo pipefail
cd "$(dirname "$0")"
echo "P36 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
echo $$ > logs/run.pid 2>/dev/null || true
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
export P36_T0="${P36_T0:-$(date +%s)}" P36_MAX_HOURS="${P36_MAX_HOURS:-2.0}"
export P36_ARTIFACTS="${P36_ARTIFACTS:-/ws/p36_artifacts}"
PY=/ws/venv-i9/bin/python
mkdir -p results logs "$P36_ARTIFACTS"
RC=0
if ! test -x "$PY"; then
  echo "P36 missing $PY (pod_setup.sh --only i9)"; RC=2
elif ! test -f ../../third_party/workspace-bench/pyproject.toml; then
  echo "P36 missing third_party/workspace-bench (probe.toml [pod].push)"; RC=2
else
  nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  free -g 2>/dev/null | head -2 || true
  "$PY" -m p36.session download || RC=$?
  # shellcheck disable=SC2086 # P36_ARGS is a word list on purpose
  [ "$RC" -eq 0 ] && { "$PY" -m p36.session all ${P36_ARGS:-} || RC=$?; }
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P36 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
