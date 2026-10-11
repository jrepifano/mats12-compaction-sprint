#!/usr/bin/env bash
# P03 pod session 1 (phase "sample"), called by run.sh from the probe directory with HF_HOME,
# HF_HUB_DISABLE_XET and PYTHONPATH already exported. Samples Qwen3.6-27B's ground truth with vLLM
# (p03/sample_vllm.py) into results/sample/ and exits with its code. bash 3.2-compatible.
# Extra arguments for the sampler (e.g. "--limit 20") can be passed in P03_SAMPLE_ARGS.
set -euo pipefail
cd "$(dirname "$0")"
VENV=/ws/venv-vllm
PY="$VENV/bin/python"
# vLLM JIT-compiles kernels with ninja from the venv's bin/ (scripts/i9_sample_vllm.py header).
export PATH="$VENV/bin:$PATH"
step() { echo "P03_STEP $1 $2 $(date -u +%FT%TZ)${3:+ $3}"; }

step preflight start
test -x "$PY" || { echo "P03 missing $PY (pod_setup.sh --only i9)"; exit 2; }
test -s data/items.jsonl || { echo "P03 missing data/items.jsonl"; exit 2; }
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader || true
mkdir -p results/sample
"$PY" -m p03.sample_vllm --items data/items.jsonl --out results/sample/ --dry-run \
  ${P03_SAMPLE_ARGS:-}
step preflight done

step sample start
RC=0
"$PY" -m p03.sample_vllm --items data/items.jsonl --out results/sample/ \
  ${P03_SAMPLE_ARGS:-} || RC=$?
step sample done "rc=$RC"
exit "$RC"
