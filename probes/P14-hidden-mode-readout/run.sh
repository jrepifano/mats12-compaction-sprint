#!/usr/bin/env bash
# P14 pod entry point (scripts/probe_pod.py contract): results/ + MANIFEST.sha256, last line
# "PROBE_DONE rc=<n>".
set -uo pipefail
cd "$(dirname "$0")"
echo "P14 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
export HF_HOME=/ws/hf_cache WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
PY="${P14_PY:-/ws/venv-peft/bin/python}"
mkdir -p results logs
"$PY" -u p14_pod.py; rc=$?
(cd results && find . -type f ! -name MANIFEST.sha256 | sed 's|^\./||' | sort | tr '\n' '\0' \
  | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp)
mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "PROBE_DONE rc=$rc"
