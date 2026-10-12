#!/usr/bin/env bash
# P02 pod run (probe run contract, scripts/probe_pod.py): p02_pod.py in the peft venv, then the
# results manifest (written elsewhere, moved in) and the anchored last line PROBE_DONE rc=<n>.
set -uo pipefail
cd "$(dirname "$0")"
echo "P02 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
export HF_HOME=/ws/hf_cache WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
PY="${P02_PY:-/ws/venv-peft/bin/python}"
mkdir -p results logs
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | tee results/gpu.txt
"$PY" -u p02_pod.py
rc=$?
echo "p02_pod.py exited rc=$rc at $(date -u +%FT%TZ)"
(cd results && find . -type f ! -name MANIFEST.sha256 | sed 's|^\./||' | sort \
  | tr '\n' '\0' | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp)
mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "PROBE_DONE rc=$rc"
